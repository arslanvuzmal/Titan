"""A setback is a position, not a slide.

The cut was `current * SETBACK_SHARE` -- half of whatever the mailbox had.
Applied twice against the same evidence it halves an already-halved number, so
a problem that persists walks a mailbox down 18, 9, 4, 2, 1 regardless of how
severe it is.

Observed live: two runs against one 6.2% bounce rate took both mailboxes from
18 to 4, and the next would have reached 2. That defeats the reason
SETBACK_SHARE is not zero -- a mailbox sending one message a day produces no
evidence either, so it can never demonstrate recovery.
"""

from __future__ import annotations

import datetime as dt

from titan.delivery.deliverability import ReputationWindow
from titan.delivery.mailbox_ramp import SETBACK_SHARE, decide, scheduled_share

NOW = dt.datetime(2026, 8, 18, 9, 0, tzinfo=dt.UTC)
CONNECTED = dt.datetime(2026, 8, 5, 9, 0, tzinfo=dt.UTC)
CEILING = 50


def _bad_evidence() -> ReputationWindow:
    """The live shape: 81 sends, 5 bounces, 6.2%."""
    return ReputationWindow(sent=81, delivered=76, hard_bounced=5, complained=0)


def _cut(current: int) -> int:
    return decide(
        mailbox="outreach@example.com",
        ceiling=CEILING,
        current=current,
        first_send_at=CONNECTED,
        now=NOW,
        evidence=_bad_evidence(),
    ).target


def test_the_same_evidence_twice_does_not_cut_twice() -> None:
    """The whole bug. Feeding the previous decision back in must be stable."""
    first = _cut(18)
    second = _cut(first)

    assert second == first, f"compounded: {18} -> {first} -> {second}"


def test_it_still_cuts_the_first_time() -> None:
    """A stable setback is not the same as no setback."""
    week_allowance = scheduled_share(1) * CEILING

    assert _cut(18) < 18
    assert _cut(18) <= week_allowance * SETBACK_SHARE + 1


def test_it_never_raises_volume_on_a_mailbox_in_trouble() -> None:
    """A mailbox already below the setback level stays where it is. Bad
    evidence must never be a route to more sending."""
    assert _cut(4) <= 4
    assert _cut(1) <= 1


def test_it_never_reaches_zero() -> None:
    """A silent mailbox produces no evidence and could never demonstrate
    recovery, so it would stay cut for ever."""
    assert _cut(2) >= 1
    assert _cut(1) >= 1


def test_repeated_runs_converge_rather_than_decay() -> None:
    """Five runs against unchanged evidence. The old arithmetic reached 1."""
    value = 18
    for _ in range(5):
        value = _cut(value)

    assert value > 1, f"decayed to {value}"
