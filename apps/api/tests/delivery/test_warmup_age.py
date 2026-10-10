"""How old a mailbox is, as opposed to how long ColdOps has known about it.

Warm-up position was derived from ``min(messages.sent_at)`` -- ColdOps's own
record of having sent through a mailbox. That is a lower bound on how long it
has been building reputation, not the thing itself.

``sales@`` made the gap concrete: connected in Smartlead on 7 August with its
warm-up pool running from that day, no ``sender_identity`` row in ColdOps until
the 17th, so it was placed on day zero and allowed five messages a day. The
mailbox was ten days warm; only ColdOps's view of it was new.
"""

from __future__ import annotations

import datetime as dt

from coldops.delivery.deliverability import warmup_day, warmup_limit
from coldops.delivery.outbox_worker import _ramp_start

TOMORROW = dt.datetime(2026, 8, 18, 9, 0, tzinfo=dt.UTC)
CONNECTED = dt.datetime(2026, 8, 7, 9, 0, tzinfo=dt.UTC)
FIRST_TITAN_SEND = dt.datetime(2026, 8, 17, 9, 0, tzinfo=dt.UTC)


# ------------------------------------------------------------- the resolution


def test_a_provider_warm_up_before_the_first_send_counts() -> None:
    """A mailbox warming before ColdOps held a row for it is still warming."""
    assert _ramp_start(FIRST_TITAN_SEND, CONNECTED) == CONNECTED


def test_either_alone_is_enough() -> None:
    assert _ramp_start(None, CONNECTED) == CONNECTED
    assert _ramp_start(FIRST_TITAN_SEND, None) == FIRST_TITAN_SEND


def test_neither_is_the_previous_behaviour_exactly() -> None:
    """No evidence at all means day zero, which is what it meant before."""
    assert _ramp_start(None, None) is None
    assert warmup_day(None, TOMORROW) == 0


def test_a_move_to_a_new_provider_restarts_the_ramp() -> None:
    """The 10 Oct 2026 case: sending since August, moved to Google Workspace.

    The rule used to take the earlier date, which read five mailboxes new to
    Google's filters as two months warm -- the full cold ramp on the first day
    sending came back. The declared start wins.
    """
    moved = dt.datetime(2026, 10, 10, 6, 48, tzinfo=dt.UTC)
    august = dt.datetime(2026, 8, 12, 9, 0, tzinfo=dt.UTC)
    day_after = moved + dt.timedelta(days=1)

    assert _ramp_start(august, moved) == moved
    assert warmup_day(_ramp_start(august, moved), day_after) == 1
    assert (
        warmup_limit(first_send_at=_ramp_start(august, moved), now=day_after, target=50)
        < 10
    )


# ------------------------------------------------------------- what it buys


def test_the_live_case_moves_from_day_zero_to_day_eleven() -> None:
    """The number this exists for."""
    without = warmup_day(FIRST_TITAN_SEND, TOMORROW)
    with_provider = warmup_day(_ramp_start(FIRST_TITAN_SEND, CONNECTED), TOMORROW)

    assert without == 1
    assert with_provider == 11


def test_and_from_five_a_day_to_nineteen() -> None:
    """Against a ceiling of fifty. Still well short of it: this corrects the
    mailbox's position on the ramp, it does not skip the ramp."""
    without = warmup_limit(first_send_at=FIRST_TITAN_SEND, now=TOMORROW, target=50)
    corrected = warmup_limit(
        first_send_at=_ramp_start(FIRST_TITAN_SEND, CONNECTED), now=TOMORROW, target=50
    )

    assert without == 6
    assert corrected == 19
    assert corrected < 50, "a corrected mailbox is still warming, not finished"


def test_a_genuinely_new_mailbox_is_unaffected() -> None:
    """Connected today, so day zero either way. The correction is about
    mailboxes with history, and must not hand volume to one without."""
    today = TOMORROW - dt.timedelta(days=1)

    assert warmup_limit(first_send_at=today, now=today, target=50) == 5


def test_the_ceiling_still_binds() -> None:
    """However old a mailbox is, it never exceeds its configured limit."""
    ancient = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)

    assert warmup_limit(first_send_at=ancient, now=TOMORROW, target=18) is None
