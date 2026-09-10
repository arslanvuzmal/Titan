"""What the health report says the estate can send today.

The capacity line in the vitals report is not an independent estimate. Its
whole justification, written in a comment above the loop, is that it runs the
outbox worker's own recorded facts back through the outbox worker's own
function: *"Not a second opinion: the same computation over the same recorded
facts."*

It was a second opinion, because it left an argument out.

``adaptive_limits.daily_limit`` grants probation only to a blocked mailbox that
has been quiet for ``PROBATION_QUIET_DAYS``, and it learns that from
``days_since_bounce``. The activity never passed it, so the parameter defaulted
to None, which means "never bounced, or nobody looked" and grants nothing. A
mailbox the send gate was letting send its five was reported here as sending
zero.

These are the contract, not the activity: they pin *why* the argument is
load-bearing, so that dropping it again fails a test rather than quietly
understating the estate.
"""

from __future__ import annotations

import pytest
from titan.delivery.adaptive_limits import (
    PROBATION_QUIET_DAYS,
    PROBATION_VOLUME,
    daily_limit,
)
from titan.delivery.sender_health import SenderHealth

CONFIGURED = 50
BLOCKED = (SenderHealth.BLOCKED,) * 3


def test_omitting_the_argument_understates_a_recovering_mailbox() -> None:
    """Planted violation: call it the way the vitals activity used to.

    This is the bug, stated as an assertion. Both calls describe the same
    mailbox on the same day; only one of them was told when it last bounced.
    """
    silent = daily_limit(CONFIGURED, recent=BLOCKED).effective
    informed = daily_limit(
        CONFIGURED, recent=BLOCKED, days_since_bounce=PROBATION_QUIET_DAYS
    ).effective

    assert silent == 0, "a blocked mailbox with no bounce date earns nothing"
    assert informed == PROBATION_VOLUME
    assert informed > silent, (
        "the report and the gate disagree about the same mailbox whenever the "
        "report forgets to say when it last bounced"
    )


def test_a_quiet_blocked_mailbox_is_counted_as_sending() -> None:
    """The number the operator is actually waiting for.

    Probation exists so a blocked mailbox can send its way back out. A report
    that shows it at zero for the whole probation window hides the one signal
    that says recovery has started.
    """
    decision = daily_limit(
        CONFIGURED, recent=BLOCKED, days_since_bounce=PROBATION_QUIET_DAYS + 3
    )

    assert decision.effective == PROBATION_VOLUME


def test_a_mailbox_that_bounced_this_morning_earns_nothing() -> None:
    """Passing the argument is not the same as loosening the rule.

    The fix must not turn probation into a floor for every blocked mailbox --
    only for one that has gone quiet.
    """
    decision = daily_limit(CONFIGURED, recent=BLOCKED, days_since_bounce=0)

    assert decision.effective == 0


@pytest.mark.parametrize("days", [0, PROBATION_QUIET_DAYS - 1])
def test_the_quiet_window_is_a_threshold_not_a_gradient(days: int) -> None:
    """Anything inside the window earns nothing at all, not a fraction."""
    assert daily_limit(CONFIGURED, recent=BLOCKED, days_since_bounce=days).effective == 0


@pytest.mark.parametrize(
    "health",
    [SenderHealth.HEALTHY, SenderHealth.WATCH, SenderHealth.DEGRADED],
)
def test_a_mailbox_that_is_not_blocked_is_unaffected(health: SenderHealth) -> None:
    """Planted violation: apply the probation floor to everything.

    Probation is a remedy for one specific deadlock -- a blocked mailbox that
    cannot send its way back out -- and not a general floor. A healthy mailbox's
    number must not move because a bounce date is now being passed in.
    """
    with_date = daily_limit(
        CONFIGURED, recent=(health,) * 3, days_since_bounce=PROBATION_QUIET_DAYS + 30
    ).effective
    without = daily_limit(CONFIGURED, recent=(health,) * 3).effective

    assert with_date == without


def test_a_mailbox_that_never_bounced_is_still_given_nothing() -> None:
    """None has to keep meaning None.

    A blocked mailbox with no bounce behind it was blocked for some other
    reason -- failed authentication, or a complaint rate -- and neither is
    repaired by sending five more.
    """
    assert daily_limit(CONFIGURED, recent=BLOCKED, days_since_bounce=None).effective == 0
