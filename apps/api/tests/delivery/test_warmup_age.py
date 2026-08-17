"""How old a mailbox is, as opposed to how long Titan has known about it.

Warm-up position was derived from ``min(messages.sent_at)`` -- Titan's own
record of having sent through a mailbox. That is a lower bound on how long it
has been building reputation, not the thing itself.

``sales@`` made the gap concrete: connected in Smartlead on 7 August with its
warm-up pool running from that day, no ``sender_identity`` row in Titan until
the 17th, so it was placed on day zero and allowed five messages a day. The
mailbox was ten days warm; only Titan's view of it was new.
"""

from __future__ import annotations

import datetime as dt

from titan.delivery.deliverability import warmup_day, warmup_limit
from titan.delivery.outbox_worker import _earliest

TOMORROW = dt.datetime(2026, 8, 18, 9, 0, tzinfo=dt.UTC)
CONNECTED = dt.datetime(2026, 8, 7, 9, 0, tzinfo=dt.UTC)
FIRST_TITAN_SEND = dt.datetime(2026, 8, 17, 9, 0, tzinfo=dt.UTC)


# ------------------------------------------------------------- the resolution


def test_the_earlier_of_the_two_wins() -> None:
    """A mailbox warming before Titan held a row for it is still warming."""
    assert _earliest(FIRST_TITAN_SEND, CONNECTED) == CONNECTED


def test_either_alone_is_enough() -> None:
    """Both are optional and neither is authoritative on its own."""
    assert _earliest(None, CONNECTED) == CONNECTED
    assert _earliest(FIRST_TITAN_SEND, None) == FIRST_TITAN_SEND


def test_neither_is_the_previous_behaviour_exactly() -> None:
    """No evidence at all means day zero, which is what it meant before."""
    assert _earliest(None, None) is None
    assert warmup_day(None, TOMORROW) == 0


def test_it_can_only_move_a_mailbox_forward() -> None:
    """The safe direction for a value that decides sending volume.

    A provider date *later* than Titan's own first send must not push a mailbox
    back down the ramp -- that would cut volume on a mailbox with real history.
    """
    late = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)

    assert _earliest(FIRST_TITAN_SEND, late) == FIRST_TITAN_SEND


# ------------------------------------------------------------- what it buys


def test_the_live_case_moves_from_day_zero_to_day_eleven() -> None:
    """The number this exists for."""
    without = warmup_day(FIRST_TITAN_SEND, TOMORROW)
    with_provider = warmup_day(_earliest(FIRST_TITAN_SEND, CONNECTED), TOMORROW)

    assert without == 1
    assert with_provider == 11


def test_and_from_five_a_day_to_nineteen() -> None:
    """Against a ceiling of fifty. Still well short of it: this corrects the
    mailbox's position on the ramp, it does not skip the ramp."""
    without = warmup_limit(first_send_at=FIRST_TITAN_SEND, now=TOMORROW, target=50)
    corrected = warmup_limit(
        first_send_at=_earliest(FIRST_TITAN_SEND, CONNECTED), now=TOMORROW, target=50
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
