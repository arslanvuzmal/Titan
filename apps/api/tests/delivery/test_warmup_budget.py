"""Daily volume is bounded by what the mailboxes can send, not by intent.

Three mailboxes at fifty a day is a hundred and fifty, and it stays a hundred
and fifty on their first morning, when warm-up will let each of them send five.
The gates were never wrong about this -- the per-mailbox warm-up ceiling refuses
the surplus at send time and always did -- so nothing was over-sending. What was
wrong is everything upstream of the gate: the allocator divided a hundred and
fifty between the campaigns, each campaign was told it had volume it did not
have, and the difference came back as deferrals rather than as a smaller plan.

So the distinction these tests pin down is between *what is left today*, which
falls as the day is spent, and *what today was ever worth*, which does not. A
daily budget divided by the first number shrinks every time the allocator runs.
"""

from __future__ import annotations

import datetime as dt
import uuid

from titan.delivery import deliverability as d
from titan.delivery.sender_pool import MailboxSlot, capacity, daily_ceiling

NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)


def slot(name: str, limit: int, committed: int, *, excluded: str | None = None):
    return MailboxSlot(
        sender_identity_id=uuid.UUID(int=abs(hash(name)) % (2**128)),
        label=name,
        from_email=f"{name}@example.com",
        daily_limit=limit,
        committed=committed,
        excluded_because=excluded,
    )


def warmed(target: int, day: int) -> int:
    """One mailbox's ceiling on a given day of its ramp."""
    first_send = NOW - dt.timedelta(days=day)
    limit = d.warmup_limit(first_send_at=first_send, now=NOW, target=target)
    return target if limit is None else min(target, limit)


# ==========================================================================
# The number a daily budget has to be divided by
# ==========================================================================
def test_the_ceiling_is_the_whole_day_not_what_is_left_of_it() -> None:
    """The distinction the allocator depends on. Half a day in, capacity has
    fallen and the day's worth has not."""
    slots = [slot("a", 50, 30), slot("b", 50, 20), slot("c", 50, 50)]

    assert capacity(slots) == 20 + 30 + 0
    assert daily_ceiling(slots) == 150


def test_the_ceiling_does_not_move_as_the_day_is_spent() -> None:
    """Dividing a daily budget by remaining capacity at noon would hand each
    campaign a limit below what it had already used that morning, and the limit
    would shrink again on every cycle."""
    fresh = [slot("a", 50, 0), slot("b", 50, 0)]
    spent = [slot("a", 50, 41), slot("b", 50, 39)]

    assert daily_ceiling(fresh) == daily_ceiling(spent) == 100
    assert capacity(fresh) != capacity(spent)


def test_an_unavailable_mailbox_contributes_nothing() -> None:
    """A mailbox missing DKIM will refuse every message at the gate. Counting
    its fifty is how a budget becomes fiction."""
    slots = [slot("a", 50, 0), slot("b", 50, 0, excluded="DKIM not in place")]

    assert daily_ceiling(slots) == 50


def test_an_empty_pool_has_no_ceiling_at_all() -> None:
    assert daily_ceiling([]) == 0


# ==========================================================================
# What warm-up actually costs on day one
# ==========================================================================
def test_three_fresh_mailboxes_are_worth_fifteen_a_day_not_a_hundred_and_fifty() -> None:
    """The number in the brief. Warm-up starts each mailbox at a tenth of its
    own target, so the first morning is fifteen sends, not a hundred and fifty."""
    day_one = warmed(50, day=0)
    pool = [slot(name, day_one, 0) for name in ("a", "b", "c")]

    assert day_one == 5
    assert daily_ceiling(pool) == 15


def test_the_ceiling_climbs_with_the_ramp_and_arrives_at_the_configured_number() -> None:
    """The bound has to lift on its own. One that never reached the configured
    figure would be a permanent reduction wearing warm-up's name."""
    ceilings = [
        daily_ceiling([slot(n, warmed(50, day), 0) for n in ("a", "b", "c")])
        for day in range(d.WARMUP_DAYS + 1)
    ]

    assert ceilings[0] == 15
    assert ceilings == sorted(ceilings), "the ramp went backwards"
    assert ceilings[-1] == 150, "warm-up never released the full configured volume"


def test_the_bound_only_ever_binds_downward() -> None:
    """A pool that can do more than a human approved does not get to. The
    configured limit is a ceiling and warm-up is a second one; the effective
    figure is the lower, never the higher."""
    configured = 150
    warming = daily_ceiling([slot(n, warmed(50, 0), 0) for n in ("a", "b", "c")])
    warmed_up = daily_ceiling([slot(n, 50, 0) for n in ("a", "b", "c")])
    generous = daily_ceiling([slot(n, 500, 0) for n in ("a", "b", "c")])

    assert min(configured, warming) == 15
    assert min(configured, warmed_up) == 150
    assert min(configured, generous) == configured
