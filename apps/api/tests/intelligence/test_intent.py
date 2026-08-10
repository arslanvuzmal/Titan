"""Telling agreement from rejection.

Pure rules, no I/O. This is the classifier an operator's trust rests on: it
decides which replies wake somebody up. Both errors are expensive, so the cases
below are mostly the ones that look like the opposite of what they are.
"""

from __future__ import annotations

import pytest
from titan.db.enums import ReplyClass
from titan.intelligence.intent import detect_intent


def verdict(body: str, subject: str = "Re: your booking page"):
    return detect_intent(subject, body)


# ---------------------------------------------------------------------------
# The substring trap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Not interested, thanks.",
        "We're not interested at this time.",
        "no thanks",
        "No, thank you.",
        "Thanks but we'll pass.",
        "Please don't contact us again.",
    ],
)
def test_rejections_are_not_read_as_agreement(body: str):
    """ "not interested" contains "interested".

    A naive substring check reports every one of these as a win. That is the
    single most likely way this classifier could be wrong, and the most
    embarrassing: an operator gets told a prospect agreed, opens the CRM, and
    finds a refusal.
    """
    result = verdict(body)

    assert result.is_negative, f"{body!r} read as {result.reply_class}"
    assert not result.is_positive


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Yes please, that sounds good.", ReplyClass.INTERESTED),
        ("Very interested - what would it cost?", ReplyClass.WANTS_PRICING),
        ("Happy to chat. When are you free?", ReplyClass.WANTS_CALL),
        ("Can you explain how that would work?", ReplyClass.WANTS_MORE_INFO),
        ("Let's do it.", ReplyClass.INTERESTED),
        ("How much do you charge for this?", ReplyClass.WANTS_PRICING),
    ],
)
def test_agreement_is_recognised(body: str, expected: ReplyClass):
    result = verdict(body)

    assert result.reply_class is expected
    assert result.is_positive


# ---------------------------------------------------------------------------
# Ambiguity
# ---------------------------------------------------------------------------


def test_a_mixed_reply_resolves_to_unknown_not_to_a_guess():
    """The case that justifies the conflict rule.

    Picking a side here either celebrates a rejection or buries a sale. Saying
    "a person needs to read this" is the only honest answer a regex can give,
    and it still notifies -- just not as agreement.
    """
    result = verdict(
        "Not interested in the SEO work, but very interested in the booking fix."
    )

    assert result.reply_class is ReplyClass.UNKNOWN
    assert result.confidence == 0.0
    # The signals survive so the ambiguity is diagnosable rather than mysterious.
    assert len(result.signals) >= 2
    assert result.needs_a_human


def test_an_unrecognised_reply_is_unknown_and_still_needs_a_human():
    """No rule firing is not evidence of disinterest.

    Defaulting silence to "declined" would file a real reply where nobody looks.
    """
    result = verdict("Got it, cheers.")

    assert result.reply_class is ReplyClass.UNKNOWN
    assert result.needs_a_human


def test_only_a_clean_rejection_skips_the_human():
    result = verdict("Not interested, thanks.")

    assert result.reply_class is ReplyClass.NOT_INTERESTED
    assert not result.needs_a_human


# ---------------------------------------------------------------------------
# Quoted text
# ---------------------------------------------------------------------------


def test_the_quoted_original_is_not_read_as_the_prospects_words():
    """Titan's own pitch is written to sound enthusiastic.

    Left in, every reply carries a glowing case for the offer underneath it, and
    the classifier reads Titan's enthusiasm as the prospect's. A one-word "No
    thanks." on top of a quoted pitch would come back as INTERESTED.
    """
    result = verdict(
        "No thanks.\n\n"
        "On Mon, 10 Aug 2026, Arslan wrote:\n"
        "> I would love to help - very interested in working with you,\n"
        "> and happy to jump on a call to discuss pricing.\n"
    )

    assert result.reply_class is ReplyClass.NOT_INTERESTED


def test_angle_quoted_text_is_trimmed():
    result = verdict("Not for us.\n> sounds good, let's do it\n> send pricing")

    assert not result.is_positive


def test_a_reply_that_is_only_quoted_text_still_gets_read():
    """Some mail clients top-quote everything.

    Trimming to nothing would return UNKNOWN on every message from those
    clients. Falling back to the full text is the lesser error.
    """
    result = verdict("> Are you interested?\n> Let me know")

    assert result.signals or result.reply_class is ReplyClass.UNKNOWN


# ---------------------------------------------------------------------------
# The classes that are neither yes nor no
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Not right now, check back with us next quarter.", ReplyClass.NOT_NOW),
        ("We already have an agency for this.", ReplyClass.OBJECTION),
        ("That's out of our budget.", ReplyClass.OBJECTION),
        ("I'm not the right person - I'll forward this on.", ReplyClass.WRONG_PERSON),
        ("Sam has left the company.", ReplyClass.WRONG_PERSON),
    ],
)
def test_the_middle_ground_is_classified_specifically(body: str, expected: ReplyClass):
    """A deferral is not a rejection, and an objection is answerable.

    Collapsing these into NOT_INTERESTED would suppress addresses that said
    "ask me in three months" -- discarding the warmest part of the pipeline.
    """
    result = verdict(body)

    assert result.reply_class is expected


def test_a_deferral_does_not_suppress_confidently_as_a_rejection():
    """NOT_NOW must not reach the suppression threshold as NOT_INTERESTED.

    Only NOT_INTERESTED suppresses, and this is the boundary that keeps
    "next quarter" out of the do-not-contact list.
    """
    result = verdict("Not right now - maybe next year.")

    assert result.reply_class is not ReplyClass.NOT_INTERESTED


def test_confidence_drops_when_several_classes_agree():
    """Two rules on the same side still agree on the direction.

    Confident enough to notify, not confident enough to be treated as a single
    clean read.
    """
    single = verdict("How much does it cost?")
    multiple = verdict("Very interested - how much does it cost, and can we talk?")

    assert single.confidence == 0.8
    assert multiple.confidence == 0.6
    assert multiple.is_positive


def test_subject_line_alone_can_carry_the_intent():
    result = detect_intent("Re: pricing - yes please, sounds good", "")

    assert result.is_positive


# ---------------------------------------------------------------------------
# The quoted excerpt
# ---------------------------------------------------------------------------
def test_the_verdict_quotes_the_sentence_it_fired_on():
    """An operator triaging a queue needs the words, not the rule name."""
    result = verdict(
        "Thanks for getting in touch.\n"
        "Could we book a call for sometime next week?\n"
        "Best, Sam"
    )

    assert result.reply_class is ReplyClass.WANTS_CALL
    assert result.excerpt == "Could we book a call for sometime next week?"


def test_the_excerpt_is_verbatim_not_paraphrased():
    """It becomes the CRM's record of what somebody said, so it must be true."""
    result = verdict("Honestly, we're not interested at this time.")

    assert result.excerpt is not None
    assert result.excerpt in "Honestly, we're not interested at this time."


def test_an_ambiguous_reply_quotes_the_positive_half():
    """The operator is deciding whether there is a sale in it.

    Showing them the rejection they already assumed helps nobody; showing them
    the sentence that might be worth answering is the whole reason to look.
    """
    result = verdict(
        "Not interested in the SEO work. But we are very interested in the "
        "booking fix you mentioned."
    )

    assert result.reply_class is ReplyClass.UNKNOWN
    assert result.excerpt is not None
    assert "booking fix" in result.excerpt


def test_an_unmatched_reply_has_no_excerpt():
    result = verdict("Got it, cheers.")

    assert result.reply_class is ReplyClass.UNKNOWN
    assert result.excerpt is None


def test_an_excerpt_never_swallows_an_unpunctuated_wall_of_text():
    from titan.intelligence.intent import MAX_EXCERPT_CHARS

    result = verdict("x " * 900 + "happy to chat " + "y " * 900)

    assert result.excerpt is not None
    assert len(result.excerpt) <= 2 * MAX_EXCERPT_CHARS + len("happy to chat")


def test_the_excerpt_is_drawn_from_the_reply_not_the_quoted_original():
    """Titan's own pitch is full of the language these rules look for."""
    result = verdict(
        "No thanks.\n"
        "\n"
        "On Monday, outreach@ours.test wrote:\n"
        "> Happy to chat whenever suits - shall we book a call?\n"
    )

    assert result.reply_class is ReplyClass.NOT_INTERESTED
    assert result.excerpt is not None
    assert "book a call" not in result.excerpt
