"""Growing a mailbox's volume without a person deciding each step.

Titan warmed its own sender identities and never touched the mailboxes that
actually send, whose ``max_email_per_day`` was a number somebody typed. The
intelligence and the sending lived in different systems.

The tests that matter are not the arithmetic of the ramp. They are the two ways
an automatic ramp does damage: climbing on a mailbox nothing has measured, and
climbing past what a human authorised.
"""

from __future__ import annotations

import datetime as dt
import itertools
import math

from titan.delivery.deliverability import MIN_SAMPLE_FOR_RATES, ReputationWindow
from titan.delivery.mailbox_ramp import (
    MIN_DAILY,
    WEEKLY_STEPS,
    decide,
    observe_ceiling,
    scheduled_share,
    summarise,
    week_index,
)

NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)


def clean(sent: int = 400) -> ReputationWindow:
    """Enough sends to be evidence, with nothing wrong.

    Bounces scale with the sample rather than being a fixed count. A hardcoded
    two bounces is 0.5% of four hundred and 4% of fifty -- the first is healthy
    and the second is twice the pause threshold, so a fixed number would make
    "clean" mean different things at different sample sizes.
    """
    return ReputationWindow(
        sent=sent, delivered=sent - sent // 200, hard_bounced=sent // 200, complained=0
    )


def unmeasured(sent: int = 10) -> ReputationWindow:
    return ReputationWindow(sent=sent, delivered=sent, hard_bounced=0, complained=0)


def bouncing(sent: int = 400) -> ReputationWindow:
    return ReputationWindow(sent=sent, delivered=sent - 80, hard_bounced=80, complained=0)


def ramp(**overrides):
    base = {
        "mailbox": "sales@example.test",
        "ceiling": 50,
        "current": 10,
        "first_send_at": NOW - dt.timedelta(days=7),
        "now": NOW,
        "evidence": clean(),
    }
    base.update(overrides)
    return decide(**base)


# ==========================================================================
# The bound. This is the test the feature exists to pass.
# ==========================================================================
def test_the_ramp_can_never_exceed_the_configured_ceiling() -> None:
    """Same bound as every other autonomous decision here: more conservative
    than the human's number, never more permissive. Wanting more volume than
    the ceiling is a request to change the ceiling."""
    for week in range(0, 30):
        decision = ramp(
            first_send_at=NOW - dt.timedelta(days=7 * week),
            current=50,
            evidence=clean(5000),
        )
        assert decision.target <= 50, f"week {week} exceeded the ceiling"

    # And from above it. The loop alone never exercises the clamp, because the
    # scheduled share is already bounded by the ceiling -- so a mailbox sitting
    # over its ceiling is the case that actually tests it.
    assert ramp(ceiling=30, current=200, evidence=clean(5000)).target == 30


def test_a_closed_mailbox_stays_closed() -> None:
    """A ceiling of zero is a human switching the mailbox off. The ramp must not
    read that as a small number to grow from."""
    assert ramp(ceiling=0, current=0).target == 0


def test_the_ramp_never_floors_a_live_mailbox_to_zero() -> None:
    """A small ceiling multiplied by an early share rounds to nothing, and a
    throttle that silently becomes a pause is the worst kind."""
    assert ramp(ceiling=2, current=2, first_send_at=None).target >= MIN_DAILY


# ==========================================================================
# Absence of evidence is not evidence of safety
# ==========================================================================
def test_an_unmeasured_mailbox_does_not_climb() -> None:
    """The asymmetry the module exists for. Four sends and no bounces has not
    earned more volume -- it has not been measured. Climbing here is how a cold
    domain reaches full volume with nothing having checked it."""
    decision = ramp(
        current=10,
        first_send_at=NOW - dt.timedelta(days=21),  # week 4 by the calendar
        evidence=unmeasured(4),
    )

    assert decision.target <= 10
    assert "sample floor" in decision.reason


def test_the_floor_is_the_one_the_rest_of_the_system_uses() -> None:
    """A ramp with its own private threshold would climb on evidence the
    delivery gate considers meaningless."""
    just_under = ramp(current=10, evidence=unmeasured(MIN_SAMPLE_FOR_RATES - 1))
    just_over = ramp(current=10, evidence=clean(MIN_SAMPLE_FOR_RATES))

    assert "sample floor" in just_under.reason
    assert just_over.target > just_under.target


def test_a_mailbox_that_has_never_sent_starts_at_the_first_step() -> None:
    decision = ramp(current=0, first_send_at=None, evidence=unmeasured(0))

    assert decision.week == 0
    assert decision.target == max(MIN_DAILY, round(WEEKLY_STEPS[0] * 50))


# ==========================================================================
# Negative evidence outranks everything
# ==========================================================================
def test_a_bouncing_mailbox_is_cut_not_held() -> None:
    """Holding a mailbox that is actively bouncing keeps sending at the volume
    that produced the bounces."""
    decision = ramp(current=40, evidence=bouncing())

    assert decision.direction == "down"
    assert decision.target < 40
    assert "cut on delivery evidence" in decision.reason


def test_a_cut_mailbox_keeps_enough_volume_to_recover() -> None:
    """Cut to zero and it produces no evidence, so it can never demonstrate
    recovery and stays cut forever."""
    assert ramp(current=40, evidence=bouncing()).target >= MIN_DAILY


def test_bad_evidence_beats_a_late_week() -> None:
    """Ordered, not scored. A mailbox six weeks in does not get to average its
    seniority against its bounce rate."""
    decision = ramp(
        current=50,
        first_send_at=NOW - dt.timedelta(days=70),
        evidence=bouncing(),
    )

    assert decision.direction == "down"


# ==========================================================================
# The climb itself
# ==========================================================================
def test_volume_climbs_week_by_week_and_arrives_at_the_ceiling() -> None:
    """A ramp that never reaches the configured number is a permanent reduction
    wearing warm-up's name."""
    targets = [
        ramp(
            current=1,
            first_send_at=NOW - dt.timedelta(days=7 * w),
            evidence=clean(),
        ).target
        for w in range(len(WEEKLY_STEPS))
    ]

    assert targets == sorted(targets), "the ramp went backwards"
    assert targets[-1] == 50, "the ramp never released the configured volume"
    assert targets[0] < 50, "the ramp started at full volume"


def test_volume_does_not_move_within_a_week() -> None:
    """Receivers judge a sender on a trend, and a limit that changes daily is a
    trend made of noise."""
    monday = ramp(current=18, first_send_at=NOW - dt.timedelta(days=7))
    thursday = ramp(current=18, first_send_at=NOW - dt.timedelta(days=10))

    assert monday.target == thursday.target


def test_a_mailbox_already_at_its_step_is_left_alone() -> None:
    decision = ramp(current=50, first_send_at=NOW - dt.timedelta(days=70))

    assert decision.direction == "hold"
    assert decision.changed is False


def test_lowering_the_ceiling_cuts_the_mailbox() -> None:
    """The provider's number is read fresh every run, so an operator who lowers
    it in the UI is obeyed on the next cycle rather than overwritten."""
    decision = ramp(ceiling=20, current=50, first_send_at=NOW - dt.timedelta(days=70))

    assert decision.target == 20


def test_week_index_counts_from_the_first_send_not_the_account_age() -> None:
    """An account configured in March and first used in July is new to
    receivers in July."""
    assert week_index(None, NOW) == 0
    assert week_index(NOW - dt.timedelta(days=6), NOW) == 0
    assert week_index(NOW - dt.timedelta(days=7), NOW) == 1
    assert week_index(NOW - dt.timedelta(days=400), NOW) >= len(WEEKLY_STEPS)


def test_the_share_never_exceeds_one() -> None:
    for week in range(0, 60):
        assert 0 < scheduled_share(week) <= 1.0


# ==========================================================================
# What an operator reads
# ==========================================================================
def test_the_summary_reports_what_moved_not_what_was_considered() -> None:
    decisions = [
        ramp(current=10),
        ramp(mailbox="b@example.test", current=50, first_send_at=NOW),
    ]
    out = summarise(decisions)

    assert "changed" in out
    assert "sales@example.test" in out


def test_nothing_to_ramp_says_so() -> None:
    assert summarise([]) == "no mailboxes to ramp"


def test_each_decision_explains_itself_in_its_own_terms() -> None:
    """A limit that changed for a reason nobody recorded is a limit nobody can
    argue with."""
    assert "sample floor" in ramp(evidence=unmeasured(3)).describe()
    assert "cut on delivery evidence" in ramp(current=40, evidence=bouncing()).describe()


# ---------------------------------------------------------------- the ceiling
#
# The ramp writes the provider's only volume field, so the ceiling cannot be
# read back from it. These are the tests for that, and the first one is the
# regression: without ``observe_ceiling`` the ramp consumed its own output and
# drove every mailbox to the floor within a week.


def _ratchet(days: int, *, remember: bool) -> list[int]:
    """Consecutive daily runs against a mailbox a human set to 50.

    ``remember=False`` reproduces the original defect, where ceiling and current
    were both read from ``message_per_day``.

    The evidence is deliberately thin. That is not a contrived case -- it is
    the state every new mailbox is in, and it was the live state when this was
    found: forty sends against a fifty-send floor, so no rate meant anything
    yet. The below-floor branch holds at ``min(current, scheduled)``, and it is
    that ``scheduled`` that silently became a share of the previous output.
    """
    limit = 50
    stored: int | None = None
    written: int | None = None
    history = [limit]
    for day in range(days):
        if remember:
            ceiling = observe_ceiling(
                observed=limit, stored_ceiling=stored, last_written=written
            )
            stored = ceiling
        else:
            ceiling = limit
        decision = decide(
            mailbox="m@example.com",
            ceiling=ceiling,
            current=limit,
            first_send_at=NOW - dt.timedelta(days=9),
            now=NOW + dt.timedelta(days=day),
            evidence=unmeasured(40),
        )
        if decision.changed:
            written = decision.target
        limit = decision.target
        history.append(limit)
    return history


def test_the_ramp_consumed_its_own_output_before_the_ceiling_was_remembered() -> None:
    """The defect, kept as a test so it cannot come back quietly.

    A mailbox a human set to 50 ends at the floor -- not because anything was
    wrong with it, but because each write became the next run's ceiling.
    """
    history = _ratchet(6, remember=False)

    assert history[0] == 50
    assert history[-1] == MIN_DAILY
    # Monotonically down, never once back up: recovery is impossible because
    # every share is a share of a ceiling that no longer exists.
    assert all(b <= a for a, b in itertools.pairwise(history))


def test_a_remembered_ceiling_holds_the_mailbox_at_its_scheduled_share() -> None:
    """The fix. Same mailbox, same evidence, same six runs."""
    history = _ratchet(6, remember=True)

    week_two_share = WEEKLY_STEPS[1]
    assert history[-1] == math.ceil(week_two_share * 50)
    # It steps once, to what week two allows, and then stays there.
    assert len(set(history[1:])) == 1


def test_first_sight_adopts_whatever_the_operator_configured() -> None:
    assert observe_ceiling(observed=50, stored_ceiling=None, last_written=None) == 50


def test_the_ramps_own_value_does_not_move_the_ceiling() -> None:
    assert (
        observe_ceiling(observed=18, stored_ceiling=50, last_written=18) == 50
    ), "reading back the ramp's own write must not lower what a human authorised"


def test_a_human_raising_the_limit_raises_the_ceiling() -> None:
    assert observe_ceiling(observed=80, stored_ceiling=50, last_written=18) == 80


def test_a_human_lowering_the_limit_lowers_the_ceiling() -> None:
    """Adopted in both directions.

    A person asking for less is the one instruction this module may never climb
    back over, so a reduction is a new ceiling rather than a number to grow out
    of.
    """
    assert observe_ceiling(observed=20, stored_ceiling=50, last_written=18) == 20


def test_a_ceiling_is_never_negative() -> None:
    assert observe_ceiling(observed=-5, stored_ceiling=None, last_written=None) == 0
