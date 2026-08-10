"""Writing the message, and writing the reply.

Pure: no database, no network, no model. The two properties worth most here are
that a composed message survives the real validator, and that a reply draft
which needs a human number *cannot* survive it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from titan.db.enums import ReplyClass
from titan.intelligence.composer import FALLBACK_GREETING, ComposerContext, compose
from titan.intelligence.message_validator import (
    MessageContext,
    ViolationCode,
    validate_message,
)
from titan.intelligence.reply_drafter import ReplyDraftContext, draft_reply

OWNER = "Arslan Vuzmal Lone"
PORTFOLIO = "https://arslanvuzmallone.dev"
ADDRESS = "House No. 440, Street 23, Block C, Sector B-17, Islamabad, 44000, Pakistan"


@dataclass
class FakeFinding:
    id: str = "finding-1"
    issue_type: str = "broken_primary_cta"
    title: str = "Booking button returns 404"
    page_url: str | None = "https://bellrose-dental.test/book"
    observed_value: str | None = "a 404"
    business_impact: str | None = "Anyone who clicks it cannot reach your booking form"
    recommended_solution: str | None = None


def context(**overrides) -> ComposerContext:
    base = {
        "org_domain": "bellrose-dental.test",
        "finding": FakeFinding(),
        "evidence_ids": ["ev-1"],
        "owner_name": OWNER,
        "portfolio_url": PORTFOLIO,
        "mailing_address": ADDRESS,
        "unsubscribe_url": f"{PORTFOLIO}/unsubscribe",
        "solution": "booking and follow-up automation",
        "variant_seed": "lead-1",
    }
    base.update(overrides)
    return ComposerContext(**base)


# ==========================================================================
# Composing the first message
# ==========================================================================


def test_a_composed_message_passes_the_real_validator():
    """The integration that matters.

    The validator is the last thing between a draft and a stranger's inbox, and
    it rejects most of what cold email reaches for: manufactured urgency, fear
    framing, flattery, fabricated metrics. A composer that writes good copy but
    trips those rules produces nothing sendable.
    """
    composed = compose(context())

    report = validate_message(
        MessageContext(
            subject=composed.subject,
            body=composed.body,
            claim_map=composed.claim_map,
            evidenced_finding_ids=frozenset({"finding-1"}),
            sender_name=OWNER,
            portfolio_url=PORTFOLIO,
            mailing_address=ADDRESS,
            unsubscribe_present=True,
        )
    )

    assert report.passed, [f"{v.code}: {v.detail}" for v in report.violations]


@pytest.mark.parametrize("seed", ["lead-1", "lead-2", "lead-7", "lead-9", "lead-42"])
def test_every_variant_passes_the_validator(seed: str):
    """Variation must not be able to produce an unsendable message.

    Registers are picked by hashing the lead, so a phrasing that trips a rule
    would only show up for the subset of leads that hash to it -- an intermittent
    failure with no pattern an operator could see.
    """
    composed = compose(context(variant_seed=seed))

    report = validate_message(
        MessageContext(
            subject=composed.subject,
            body=composed.body,
            claim_map=composed.claim_map,
            evidenced_finding_ids=frozenset({"finding-1"}),
            sender_name=OWNER,
            portfolio_url=PORTFOLIO,
            mailing_address=ADDRESS,
            unsubscribe_present=True,
        )
    )

    assert report.passed, [f"{v.code}: {v.detail}" for v in report.violations]


def test_the_same_lead_always_composes_identically():
    """An activity retry must not produce a second, differently worded draft.

    Register choice is a hash of the lead, not a random pick -- and not Python's
    ``hash()``, which is randomised per process and would change the "stable"
    answer on every worker restart.
    """
    first = compose(context(variant_seed="lead-stable"))
    second = compose(context(variant_seed="lead-stable"))

    assert first.body == second.body
    assert first.subject == second.subject
    assert first.variant == second.variant


def test_different_leads_get_genuinely_different_messages():
    """A hundred near-identical bodies leaving one domain in a day is the shape
    a spam filter is built to catch -- and it reads as a mail merge to whoever
    opens it."""
    bodies = {compose(context(variant_seed=f"lead-{n}")).body for n in range(40)}

    # Four registers, so forty leads should reach all of them.
    assert len(bodies) >= 4


def test_the_claim_map_quotes_the_body_verbatim():
    """The claim map and the sentence must be built together or they drift.

    Someone edits a template, the claim map still describes the old wording, and
    the validator passes a sentence that nothing actually supports.
    """
    for seed in ("lead-1", "lead-2", "lead-3", "lead-4"):
        composed = compose(context(variant_seed=seed))
        for entry in composed.claim_map:
            assert entry["sentence"] in composed.body


def test_a_follow_up_does_not_repeat_the_original_opening():
    """Re-sending the same observation to somebody who ignored it is how an
    automated sequence announces that nobody is reading the replies."""
    first = compose(context(step_number=0))
    followup = compose(context(step_number=1))

    assert first.body != followup.body
    assert followup.variant.endswith("step1")
    # Still evidenced: a follow-up that drops the claim map would be an
    # unsupported message, not a shorter one.
    for entry in followup.claim_map:
        assert entry["sentence"] in followup.body


def test_no_name_means_a_neutral_greeting_never_an_invented_one():
    """Invariant 6 reaches the greeting too.

    Guessing "Hi Sam" from ``sam@`` is a fabricated fact about a person, and
    ``sam@`` is as likely to be a shared mailbox as a human.
    """
    composed = compose(context(contact_first_name=None))

    assert composed.body.startswith(f"{FALLBACK_GREETING},")


def test_a_published_name_is_used_when_there_is_one():
    composed = compose(context(contact_first_name="Dana"))

    assert composed.body.startswith("Hi Dana,")


def test_an_unrecognised_issue_type_falls_back_to_the_findings_own_title():
    """The detector saw the page; a generic sentence about an unknown problem
    would be less true and less useful than what it wrote down."""
    finding = FakeFinding(issue_type="some_new_detector_output", title="Checkout 500s")

    composed = compose(context(finding=finding))

    assert "checkout 500s" in composed.body.lower()


# ==========================================================================
# Drafting a reply
# ==========================================================================


def reply_context(reply_class: ReplyClass, **overrides) -> ReplyDraftContext:
    base = {
        "reply_class": reply_class,
        "original_subject": "A broken step on bellrose-dental.test",
        "owner_name": OWNER,
        "solution": "booking and follow-up automation",
    }
    base.update(overrides)
    return ReplyDraftContext(**base)


def test_a_rejection_gets_no_draft_at_all():
    """The right response to "not interested" is silence.

    Putting a ready-to-send message in front of an operator at that moment
    invites the reply that turns a polite no into a spam complaint.
    """
    assert draft_reply(reply_context(ReplyClass.NOT_INTERESTED)) is None


def test_a_pricing_draft_cannot_pass_validation():
    """The safety property this whole module rests on.

    Titan has not seen enough to price the job, and a plausible-looking figure
    is the most expensive thing it could put in writing. The draft leaves a
    marked blank, the validator refuses it, and queue_message refuses drafts
    that failed validation -- so it is not a convention that a human fills this
    in, it is a mechanism.
    """
    draft = draft_reply(reply_context(ReplyClass.WANTS_PRICING))

    assert draft is not None
    assert draft.ready_to_send is False

    report = validate_message(
        MessageContext(
            subject=draft.subject,
            body=draft.body,
            claim_map=[],
            evidenced_finding_ids=frozenset(),
            sender_name=OWNER,
            portfolio_url=PORTFOLIO,
            mailing_address=ADDRESS,
            unsubscribe_present=True,
        )
    )

    assert not report.passed
    assert ViolationCode.PLACEHOLDER_LEFT in {v.code for v in report.violations}


def test_no_price_is_ever_invented():
    draft = draft_reply(reply_context(ReplyClass.WANTS_PRICING))

    assert draft is not None
    # No currency symbol and no bare figure anywhere in the draft.
    assert "$" not in draft.body
    assert "£" not in draft.body
    assert not any(token.strip(".,").isdigit() for token in draft.body.split())


def test_an_agreement_does_not_get_re_pitched():
    """They already said yes. Another paragraph of benefits can only lose it."""
    draft = draft_reply(reply_context(ReplyClass.INTERESTED))

    assert draft is not None
    assert draft.ready_to_send is True
    lowered = draft.body.lower()
    assert "i build" not in lowered
    assert "i work on" not in lowered


def test_a_call_request_uses_the_booking_link_when_there_is_one():
    with_link = draft_reply(
        reply_context(ReplyClass.WANTS_CALL, booking_url="https://cal.test/arslan")
    )
    without = draft_reply(reply_context(ReplyClass.WANTS_CALL))

    assert with_link is not None and without is not None
    assert "https://cal.test/arslan" in with_link.body
    # No link configured is not a reason to leave a blank: asking for times is a
    # complete, sendable answer.
    assert without.ready_to_send is True
    assert "times" in without.body.lower()


def test_an_ambiguous_reply_gets_a_scaffold_not_a_guess():
    """Guessing at an ambiguous reply is how a warm lead gets an answer to a
    question they did not ask."""
    draft = draft_reply(reply_context(ReplyClass.UNKNOWN))

    assert draft is not None
    assert draft.ready_to_send is False


def test_a_deferral_is_treated_as_a_yes_with_a_date_on_it():
    draft = draft_reply(reply_context(ReplyClass.NOT_NOW))

    assert draft is not None
    assert "check back" in draft.body.lower()
    # The timing is theirs to state, so it stays blank until a person reads it.
    assert draft.ready_to_send is False


def test_the_subject_is_not_double_prefixed():
    already = draft_reply(
        reply_context(ReplyClass.INTERESTED, original_subject="Re: your booking page")
    )

    assert already is not None
    assert already.subject == "Re: your booking page"
    assert already.subject.lower().count("re:") == 1


def test_every_draft_carries_a_rationale():
    """An operator reads these cold, out of context, hours later. A suggestion
    with no stated reasoning is one they have to reverse-engineer before they
    can trust it."""
    for reply_class in ReplyClass:
        draft = draft_reply(reply_context(reply_class))
        if draft is None:
            continue
        assert draft.rationale
        assert len(draft.rationale) > 30
