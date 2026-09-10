"""The regression guard for the sequence that never advanced.

Not a test of ``plan_followup``'s branches -- ``test_sequencing.py`` covers
those. This covers the one input that was wrong in production for the system's
entire life: ``completed_step_numbers``.

``generate_draft`` never wrote ``sequence_step_id``, so
``FollowUpScheduler._plan_for`` built that set from a column that was always
NULL and always handed over an empty frozenset. Every branch below it then
behaved correctly on a false premise. Measured before the fix: 6,064 drafts
with no step, 5,000 of them superseded by the next re-draft of the same opener,
and 344 of 375 contacted leads holding exactly one delivered message.

The lesson these tests encode is that a correct decision procedure fed a
constant is a constant.
"""

from __future__ import annotations

import datetime as dt

from titan.intelligence.sequencing import (
    FollowUpContext,
    SkipReason,
    Step,
    plan_followup,
)

NOW = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)

#: The four steps every campaign is provisioned with.
STEPS = (
    Step(
        id="s1",
        step_number=1,
        delay_days=0,
        template_key="outreach_v2_step1",
        requires_new_evidence=False,
    ),
    Step(
        id="s2",
        step_number=2,
        delay_days=3,
        template_key="outreach_v2_followup1",
        requires_new_evidence=False,
    ),
    Step(id="s3", step_number=3, delay_days=4, template_key="outreach_v2_followup2"),
    Step(id="s4", step_number=4, delay_days=5, template_key="outreach_v2_followup3"),
)


def context(**overrides) -> FollowUpContext:
    base = {
        "now": NOW,
        "lead_status_is_terminal": False,
        "replied_at": None,
        "last_contacted_at": NOW - dt.timedelta(days=14),
        "followups_sent": 1,
        "max_followups": 3,
        "sequence_is_active": True,
        "steps": STEPS,
        "completed_step_numbers": frozenset({1}),
        "has_eligible_contact": True,
        "is_suppressed": False,
    }
    base.update(overrides)
    return FollowUpContext(**base)


# --------------------------------------------- the bug, stated as a test
def test_an_unrecorded_opener_makes_the_opener_due_forever() -> None:
    """Planted violation: leave ``sequence_step_id`` unwritten.

    This is the production defect exactly. A lead contacted a fortnight ago,
    whose delivered opener was never attributed to a step, is offered step one
    -- with ``delay_days=0``, so due the instant it is asked, every cycle, for
    as long as the lead exists.

    The assertion is deliberately of the broken behaviour: it documents what an
    empty ``completed`` set means, so that anyone tempted to stop writing the
    column again can see the cost in one line.
    """
    plan = plan_followup(context(completed_step_numbers=frozenset()))

    assert plan.due is True
    assert plan.step is not None and plan.step.step_number == 1, (
        "an empty completed set always resolves to remaining[0], which is the "
        "opener the recipient already received"
    )


def test_a_recorded_opener_advances_to_the_first_follow_up() -> None:
    """The fix, from the planner's side: record step one and step two follows."""
    plan = plan_followup(context(completed_step_numbers=frozenset({1})))

    assert plan.due is True
    assert plan.step is not None
    assert plan.step.step_number == 2
    assert plan.step.template_key == "outreach_v2_followup1"


def test_each_recorded_step_moves_to_the_next() -> None:
    """It has to keep working, not just advance once."""
    for completed, expected in (({1}, 2), ({1, 2}, 3), ({1, 2, 3}, 4)):
        plan = plan_followup(
            context(
                completed_step_numbers=frozenset(completed),
                # The ceiling is a separate rule with its own test below; this
                # one is about ordering.
                max_followups=10,
                followups_sent=len(completed),
            )
        )
        assert plan.step is not None, f"nothing due after {sorted(completed)}"
        assert plan.step.step_number == expected


def test_the_sequence_ends_rather_than_looping() -> None:
    """A lead that has had all four is finished, not back at the top."""
    plan = plan_followup(
        context(
            completed_step_numbers=frozenset({1, 2, 3, 4}),
            max_followups=10,
            followups_sent=4,
        )
    )

    assert plan.due is False
    assert plan.skip_reason is SkipReason.SEQUENCE_COMPLETE
    assert plan.next_action_at is None, "a finished lead must not be rescheduled"


# --------------------------------------------- the delay is measured from the send
def test_the_first_follow_up_waits_its_delay() -> None:
    """Recording the step must not make step two due immediately.

    Step two carries ``delay_days=3``, measured from the last contact. A lead
    written to yesterday is not owed anything today, and the plan says when
    rather than refusing outright.
    """
    plan = plan_followup(context(last_contacted_at=NOW - dt.timedelta(days=1)))

    assert plan.due is False
    assert plan.skip_reason is SkipReason.NOT_DUE_YET
    assert plan.next_action_at == NOW + dt.timedelta(days=2)


def test_the_campaign_ceiling_still_stops_it() -> None:
    """Planted violation: advance on the sequence alone.

    ``max_followups`` is the campaign's word on how much mail one business
    receives, and it is counted against messages sent rather than steps
    defined. A four-step sequence under a ceiling of three stops at three.
    """
    plan = plan_followup(
        context(completed_step_numbers=frozenset({1, 2, 3}), followups_sent=3)
    )

    assert plan.due is False
    assert plan.skip_reason is SkipReason.FOLLOWUP_LIMIT


def test_a_reply_ends_the_sequence_whatever_step_says() -> None:
    """The one rule that outranks all of this. Somebody answered."""
    plan = plan_followup(context(replied_at=NOW - dt.timedelta(days=1)))

    assert plan.due is False
    assert plan.skip_reason is SkipReason.REPLIED
    assert plan.next_action_at is None
