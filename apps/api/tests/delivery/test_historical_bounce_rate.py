"""Two gates in series, and fixing one of them fixed nothing.

``adaptive_limits`` learned to grant a blocked mailbox five sends a day once it
had gone quiet -- and the queue still did not move. ``outreach@`` sat on 50
queued messages with the allowance live and every send refused by a *second*
gate, ``check_reputation``, which returned BLOCK on the same stale rate::

    deliverability: hard-bounce rate 5.32% over 94 sends

Both gates read the same evidence and only one of them had been taught to read
the date on it. An allowance that another check overrules is indistinguishable
at runtime from never having been granted, which is why these tests assert the
two agree rather than testing this gate alone.

The rate itself was honest: five hard bounces over 94 sends, all of them from
before address verification existed, the most recent nine days old.
"""

from __future__ import annotations

import pytest
from titan.delivery.adaptive_limits import PROBATION_QUIET_DAYS, daily_limit
from titan.delivery.deliverability import (
    BOUNCE_QUIET_DAYS,
    BOUNCE_RATE_PAUSE,
    MIN_SAMPLE_FOR_RATES,
    ReputationWindow,
    Severity,
    check_reputation,
)
from titan.delivery.sender_health import SenderHealth

#: The live mailbox, to the number: 5 hard bounces over 94 sends = 5.32%.
LIVE_SENT = 94
LIVE_BOUNCED = 5


def window(**overrides) -> ReputationWindow:
    base = {
        "sent": LIVE_SENT,
        "delivered": LIVE_SENT - LIVE_BOUNCED,
        "hard_bounced": LIVE_BOUNCED,
        "complained": 0,
    }
    return ReputationWindow(**{**base, **overrides})


def severities(w: ReputationWindow) -> set[Severity]:
    return {s.severity for s in check_reputation(w)}


def codes(w: ReputationWindow) -> set[str]:
    return {s.code for s in check_reputation(w)}


def test_the_live_case_no_longer_blocks() -> None:
    """outreach@ exactly as it stood: over the ceiling, quiet for nine days."""
    assert Severity.BLOCK not in severities(window(days_since_bounce=9))
    assert "bounce_rate_historical" in codes(window(days_since_bounce=9))


def test_a_mailbox_still_bouncing_still_blocks() -> None:
    """The distinction the whole change rests on. Yesterday's bounce is
    evidence about now."""
    assert Severity.BLOCK in severities(window(days_since_bounce=1))
    assert "bounce_rate_exceeded" in codes(window(days_since_bounce=1))


@pytest.mark.parametrize("days", [0, 1, BOUNCE_QUIET_DAYS - 1])
def test_the_quiet_period_has_to_have_elapsed(days: int) -> None:
    assert Severity.BLOCK in severities(window(days_since_bounce=days))


@pytest.mark.parametrize("days", [BOUNCE_QUIET_DAYS, BOUNCE_QUIET_DAYS + 60])
def test_past_the_quiet_period_it_is_a_warning(days: int) -> None:
    assert Severity.BLOCK not in severities(window(days_since_bounce=days))


def test_an_unmeasured_mailbox_gets_no_benefit_of_the_doubt() -> None:
    """``None`` is "never bounced, or nobody looked", and the second reading is
    the dangerous one. A bad rate with no bounce date is a mailbox nobody has
    measured -- it must keep its block, not inherit the quiet path.

    This is the same trap the probation allowance fell into and an existing
    test caught: defaulting the unknown case to the permissive branch.
    """
    assert Severity.BLOCK in severities(window(days_since_bounce=None))


def test_the_default_is_the_blocking_one() -> None:
    """Every other construction site in the codebase omits the new field, so
    the default decides what they do. It must be the conservative branch."""
    assert ReputationWindow(sent=94, delivered=89, hard_bounced=5, complained=0)


def test_a_stale_rate_is_still_reported() -> None:
    """Downgraded, not silenced. An operator must still be able to see that the
    mailbox is carrying a bad rate, or the recovery is invisible."""
    signals = check_reputation(window(days_since_bounce=9))

    assert signals, "the bad rate vanished from the report entirely"
    assert any("5.32%" in s.detail for s in signals)
    assert any("9 days" in s.detail for s in signals)


def test_a_clean_mailbox_is_untouched_however_long_it_has_been_quiet() -> None:
    """Being quiet is not a licence; there has to be a bad rate to forgive."""
    assert (
        severities(window(sent=200, delivered=200, hard_bounced=0, days_since_bounce=90))
        == set()
    )


def test_complaints_are_never_forgiven_by_time() -> None:
    """The argument for ageing out a bounce rate does not transfer.

    A bounce is a fact about an address, and a cleaned list stops producing
    them. A complaint is a person saying "this is spam" about the message and
    the sender -- and the reputational damage does not expire on a schedule.
    """
    quiet_but_complained = ReputationWindow(
        sent=200,
        delivered=200,
        hard_bounced=0,
        complained=2,  # 1%, ten times the pause threshold
        days_since_bounce=90,
    )

    assert Severity.BLOCK in severities(quiet_but_complained)


def test_below_the_sample_floor_nothing_is_said_either_way() -> None:
    """Unchanged, and asserted because the branch above it was rewritten."""
    tiny = ReputationWindow(
        sent=MIN_SAMPLE_FOR_RATES - 1,
        delivered=10,
        hard_bounced=10,
        complained=5,
        days_since_bounce=90,
    )

    assert check_reputation(tiny) == []


def test_a_rate_under_the_ceiling_is_not_promoted_to_a_block() -> None:
    """The historical branch sits in front of the BLOCK branch; it must not
    capture rates that were never blocking."""
    under = window(sent=200, hard_bounced=1, days_since_bounce=90)  # 0.5%

    assert under.bounce_rate < BOUNCE_RATE_PAUSE
    assert Severity.BLOCK not in severities(under)
    assert "bounce_rate_historical" not in codes(under)


# ------------------------------------------------------- the two gates agree
def test_both_gates_release_on_the_same_evidence() -> None:
    """The bug this file exists for: a granted allowance that another check
    overrules delivers nothing.

    Same mailbox, same day, both gates consulted. If either still refuses, the
    queue does not move -- so assert them together, not apart.
    """
    days = max(PROBATION_QUIET_DAYS, BOUNCE_QUIET_DAYS)

    allowance = daily_limit(50, recent=(SenderHealth.BLOCKED,), days_since_bounce=days)
    reputation = severities(window(days_since_bounce=days))

    assert allowance.effective > 0, "the limit gate would send nothing"
    assert Severity.BLOCK not in reputation, "the reputation gate would refuse"


def test_both_gates_hold_on_the_same_evidence() -> None:
    """And the converse, which matters just as much: a mailbox that bounced
    yesterday must be refused by both, not merely throttled by one."""
    allowance = daily_limit(50, recent=(SenderHealth.BLOCKED,), days_since_bounce=1)

    assert allowance.effective == 0
    assert Severity.BLOCK in severities(window(days_since_bounce=1))


def test_the_two_quiet_periods_are_the_same_number() -> None:
    """They are separate constants in separate modules governing one decision.
    If they drift, one gate opens while the other is shut and the symptom is
    silence -- a queue that does not move, with no error anywhere."""
    assert BOUNCE_QUIET_DAYS == PROBATION_QUIET_DAYS
