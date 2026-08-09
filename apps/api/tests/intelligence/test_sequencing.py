"""Follow-up scheduler tests.

Every test starts from a lead that legitimately *is* owed a follow-up, then
breaks exactly one thing -- so a passing test proves the specific rule fired
rather than some unrelated precondition happening to be absent.
"""

from __future__ import annotations

import datetime as dt

import pytest
from titan.intelligence.sequencing import (
    FollowUpContext,
    SkipReason,
    Step,
    plan_followup,
)

NOW = dt.datetime(2026, 8, 9, 12, 0, tzinfo=dt.UTC)

STEPS = (
    Step(id="s1", step_number=1, delay_days=3, template_key="followup_one"),
    Step(id="s2", step_number=2, delay_days=7, template_key="followup_two"),
)


def context(**overrides) -> FollowUpContext:
    base: dict = {
        "now": NOW,
        "lead_status_is_terminal": False,
        "replied_at": None,
        "last_contacted_at": NOW - dt.timedelta(days=5),
        "followups_sent": 0,
        "max_followups": 2,
        "sequence_is_active": True,
        "steps": STEPS,
        "completed_step_numbers": frozenset(),
        "has_eligible_contact": True,
        "is_suppressed": False,
        "draft_pending_for_next_step": False,
    }
    base.update(overrides)
    return FollowUpContext(**base)


# ==========================================================================
# The control case
# ==========================================================================
def test_a_lead_contacted_long_enough_ago_is_due_the_first_step() -> None:
    plan = plan_followup(context())

    assert plan.due is True
    assert plan.step is not None
    assert plan.step.step_number == 1
    assert plan.step.template_key == "followup_one"


def test_the_next_uncompleted_step_is_chosen() -> None:
    """Step 2 becomes next once step 1 has been sent."""
    plan = plan_followup(
        context(
            completed_step_numbers=frozenset({1}),
            followups_sent=1,
            last_contacted_at=NOW - dt.timedelta(days=8),
        )
    )

    assert plan.due is True
    assert plan.step is not None
    assert plan.step.step_number == 2


# ==========================================================================
# Invariant 15: a replied lead gets nothing further
# ==========================================================================
def test_a_reply_stops_the_sequence_permanently() -> None:
    """The gap this module closes: nothing used to stop at all, because
    nothing ever scheduled a second message."""
    plan = plan_followup(context(replied_at=NOW - dt.timedelta(hours=1)))

    assert plan.due is False
    assert plan.skip_reason is SkipReason.REPLIED
    # None, not a future time: a replied lead must never be reconsidered.
    assert plan.next_action_at is None


def test_a_reply_outranks_an_otherwise_perfectly_due_step() -> None:
    plan = plan_followup(
        context(replied_at=NOW, last_contacted_at=NOW - dt.timedelta(days=90))
    )
    assert plan.skip_reason is SkipReason.REPLIED


# ==========================================================================
# The other permanent stops
# ==========================================================================
@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"lead_status_is_terminal": True}, SkipReason.TERMINAL),
        ({"is_suppressed": True}, SkipReason.SUPPRESSED),
        ({"has_eligible_contact": False}, SkipReason.NO_ELIGIBLE_CONTACT),
        ({"last_contacted_at": None}, SkipReason.NEVER_CONTACTED),
        ({"sequence_is_active": False}, SkipReason.SEQUENCE_INACTIVE),
        ({"completed_step_numbers": frozenset({1, 2})}, SkipReason.SEQUENCE_COMPLETE),
        ({"draft_pending_for_next_step": True}, SkipReason.ALREADY_PENDING),
    ],
)
def test_each_condition_stops_the_followup(overrides, expected) -> None:
    plan = plan_followup(context(**overrides))

    assert plan.due is False
    assert plan.skip_reason is expected
    assert plan.step is None


def test_a_lead_with_no_first_message_is_not_followed_up() -> None:
    """A follow-up follows something. Without a delivered first message there
    is nothing to follow, and treating discovery as contact would mail people
    who were never contacted at all."""
    plan = plan_followup(context(last_contacted_at=None))

    assert plan.skip_reason is SkipReason.NEVER_CONTACTED
    assert plan.next_action_at is None


# ==========================================================================
# The campaign ceiling
# ==========================================================================
def test_the_campaign_followup_limit_is_respected() -> None:
    plan = plan_followup(context(followups_sent=2, max_followups=2))

    assert plan.due is False
    assert plan.skip_reason is SkipReason.FOLLOWUP_LIMIT


def test_lowering_the_limit_stops_a_lead_mid_sequence() -> None:
    """The ceiling counts messages already sent, not steps defined, so an
    operator tightening a live campaign takes effect immediately."""
    plan = plan_followup(
        context(followups_sent=1, max_followups=1, completed_step_numbers=frozenset({1}))
    )

    assert plan.due is False
    assert plan.skip_reason is SkipReason.FOLLOWUP_LIMIT


def test_a_limit_of_zero_permits_no_followup_at_all() -> None:
    assert plan_followup(context(max_followups=0)).due is False


# ==========================================================================
# Timing
# ==========================================================================
def test_a_step_is_not_due_before_its_delay_has_elapsed() -> None:
    plan = plan_followup(context(last_contacted_at=NOW - dt.timedelta(days=1)))

    assert plan.due is False
    assert plan.skip_reason is SkipReason.NOT_DUE_YET
    # Rescheduled rather than dropped: this one comes back.
    assert plan.next_action_at == NOW - dt.timedelta(days=1) + dt.timedelta(days=3)


def test_the_delay_is_measured_from_the_last_contact_not_from_discovery() -> None:
    contacted = NOW - dt.timedelta(days=3)
    plan = plan_followup(context(last_contacted_at=contacted))

    assert plan.due is True


def test_a_zero_day_delay_is_due_immediately() -> None:
    steps = (Step(id="s0", step_number=1, delay_days=0, template_key="same_day"),)
    plan = plan_followup(context(steps=steps, last_contacted_at=NOW))

    assert plan.due is True


def test_not_due_yet_is_temporary_but_a_reply_is_not() -> None:
    """The distinction the scanner depends on: one reschedules, one does not."""
    later = plan_followup(context(last_contacted_at=NOW))
    stopped = plan_followup(context(replied_at=NOW))

    assert later.next_action_at is not None
    assert stopped.next_action_at is None
