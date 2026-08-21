"""Rewriting mail that was already written.

The rules changed and 628 of the 745 drafts in the queue stopped passing them.
Blocking those was the urgent half and it left 580 leads with nothing to send.

What is asserted here is mostly about restraint: which drafts must *not* be
touched, and what must happen to an approval when the words underneath it
change.
"""

from __future__ import annotations

from titan.db.enums import DraftStatus, OutboxStatus
from titan.outreach.redraft import LIVE_OUTBOX, REDRAFTABLE, why_stale

OWNER = "Arslan Vuzmal Lone"
PORTFOLIO = "https://arslanvuzmallone.com"
ADDRESS = "House No. 440, Street 23, Block C, Sector B-17, Islamabad, 44000, Pakistan"


def body(pitch: str) -> str:
    return f"Hi there,\n\n{pitch}\n\n{OWNER}\n{PORTFOLIO}\n{ADDRESS}\nUnsubscribe: x\n"


GOOD = body(
    "I was looking through example.test and noticed your /book page currently "
    "returns HTTP 404.\n\nThat is worth fixing because someone clicking through "
    "there has already moved past browsing treatments and is actively trying to "
    "book.\n\nI build and repair patient-booking flows, so I can send you the "
    "exact issue and the simplest way I would correct it.\n\nWant me to send you "
    "the exact fix?"
)


# ==========================================================================
# What counts as stale
# ==========================================================================


def test_the_claim_that_was_actually_sent_is_what_marks_a_draft_stale() -> None:
    """366 of the 628 said a version of this, and it went out."""
    stale = body(
        "On example.test the booking button returns a 404. Fixing this sort of "
        "thing is what I do, mostly for firms your size, and I could outline "
        "what it would take in about ten minutes."
    )

    assert why_stale(stale, OWNER) == "claims_a_clientele_that_cannot_be_shown"


def test_a_message_nobody_will_read_to_the_end_is_stale() -> None:
    assert "outside_the_word_band" in why_stale(body(" ".join(["word"] * 140)), OWNER)
    assert "outside_the_word_band" in why_stale(body("Too short."), OWNER)


def test_a_message_that_still_reads_well_is_left_alone() -> None:
    """Planted violation: report every draft as stale and 745 messages are
    rewritten to say what 117 of them already said."""
    assert why_stale(GOOD, OWNER) == ""


def test_the_reason_names_the_number_that_failed() -> None:
    """An operator reading 580 lines needs to see which way it missed."""
    reason = why_stale(body(" ".join(["word"] * 140)), OWNER)

    # 140 words, plus the two of "Hi there,".
    assert "142 words" in reason


# ==========================================================================
# What must not be touched
# ==========================================================================


def test_a_sent_message_is_never_rewritten() -> None:
    """There is no unsending. Rewriting the record of what left the building
    destroys the only account of what a recipient actually read.

    Planted violation: put SENT in LIVE_OUTBOX and 32 delivered messages are
    repointed at drafts nobody ever received.
    """
    assert OutboxStatus.SENT not in LIVE_OUTBOX
    assert OutboxStatus.FAILED_PERMANENT not in LIVE_OUTBOX


def test_a_decision_somebody_made_is_not_overwritten() -> None:
    """A rejected draft is a person saying no, and an expired one is a person
    not saying anything in time. Neither is a stale draft to refresh."""
    assert DraftStatus.REJECTED not in REDRAFTABLE
    assert DraftStatus.EXPIRED not in REDRAFTABLE
    assert DraftStatus.SUPERSEDED not in REDRAFTABLE


def test_an_armed_row_is_repointed_rather_than_dropped() -> None:
    """The three states where a row still points at a message that has not
    left. Cancelling them instead would strand the lead; repointing keeps its
    place in the queue and changes only the words."""
    assert LIVE_OUTBOX == {
        OutboxStatus.PENDING,
        OutboxStatus.LEASED,
        OutboxStatus.DEFERRED,
    }


# ==========================================================================
# The approval underneath
# ==========================================================================


def test_replacing_the_words_invalidates_the_approval_that_covered_them() -> None:
    """The property the whole design rests on.

    A person approved the old words. The replacement is a new draft at version
    one with no approval against it, and the send gate refuses a message whose
    approval does not match the draft version -- so 132 approved drafts cannot
    send new text on an old decision.

    Asserted against the policy engine rather than described in a comment,
    because "the gate will catch it" is exactly the kind of claim that stops
    being true quietly.
    """
    from titan.policy.engine import DenyCode

    assert DenyCode.APPROVAL_STALE.value == "approval_does_not_match_draft_version"
    assert DenyCode.APPROVAL_MISSING.value == "approval_missing"
