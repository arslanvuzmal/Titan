"""Today's sending, as the dashboard reports it.

The section this feeds exists because of a specific morning. On 31 August the
estate sat at nine sends against a ceiling of twenty-six, and no screen in the
CRM said so -- the shortfall was found by hand in psql after the day was half
gone. So what is pinned here is mostly the ways a "sent today" number can be
comfortingly wrong: capacity that pools when it does not, a mailbox blocked by
its own DNS reported as merely quiet, yesterday's sends leaking into today, and
a bounce rate of exactly zero on a day nothing has been sent.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import update
from titan.db.models import (
    Message,
    OutboxMessage,
    SenderHealthSnapshot,
    SenderIdentity,
)
from titan.delivery import day_report
from titan.delivery.day_report import DayReport, Deferral, MailboxDay
from titan.delivery.deliverability import WARMUP_DAYS

from tests.delivery.conftest import build_sendable

NOW = dt.datetime(2026, 8, 31, 17, 0, tzinfo=dt.UTC)


def box(name: str, *, sent: int, allowed: int, queued: int = 0) -> MailboxDay:
    return MailboxDay(
        sender_identity_id=str(uuid.uuid4()),
        label=name,
        from_email=f"{name}@example.test",
        sent=sent,
        allowed=allowed,
        configured=50,
        queued=queued,
        health="healthy",
        health_as_of=NOW.date(),
        warmup_day=None,
        warmup_days=WARMUP_DAYS,
        note="",
    )


def report(*boxes: MailboxDay, **kwargs) -> DayReport:
    defaults = dict(
        as_of=NOW,
        window_date=NOW.date(),
        sent=sum(b.sent for b in boxes),
        ceiling=sum(b.allowed for b in boxes),
        delivered=0,
        bounced=0,
        complained=0,
        failed=0,
        queued=sum(b.queued for b in boxes),
        mailboxes=boxes,
    )
    return DayReport(**{**defaults, **kwargs})


# ==========================================================================
# The arithmetic, which is where a dashboard lies most easily
# ==========================================================================
def test_capacity_does_not_pool_across_mailboxes() -> None:
    """The subtraction that would be wrong.

    Two mailboxes at five. One has spent all five, the other none. Total sent
    is five and the total ceiling is ten, so ``ceiling - sent`` says five are
    left -- and five are, but only from one mailbox. Write it as a single
    subtraction and the moment the spent mailbox overshoots, or a third joins
    that is blocked outright, the figure advertises room no message can use.
    """
    spent = box("spent", sent=5, allowed=5)
    fresh = box("fresh", sent=0, allowed=5)
    blocked = box("blocked", sent=0, allowed=0)

    day = report(spent, fresh, blocked)

    assert day.ceiling == 10
    assert day.sent == 5
    assert day.remaining == 5
    # The same number by coincidence here; the next test is where they diverge.
    assert day.remaining == day.ceiling - day.sent


def test_a_mailbox_over_its_allowance_is_full_rather_than_owed() -> None:
    """A limit lowered mid-day -- health dropping, or a warm-up step down --
    leaves a mailbox above its own ceiling. Negative headroom would be
    subtracted from a healthy mailbox's room and hide capacity that exists."""
    over = box("over", sent=9, allowed=5)
    fresh = box("fresh", sent=0, allowed=5)

    day = report(over, fresh)

    assert over.remaining == 0
    assert day.remaining == 5
    assert day.ceiling - day.sent == 1, "the naive subtraction, for contrast"


def test_an_empty_estate_reports_nothing_rather_than_failing() -> None:
    day = report()

    assert (day.sent, day.ceiling, day.remaining) == (0, 0, 0)
    assert day.bounce_rate_today is None


# ==========================================================================
# The bounce rate, which must not read as clean when it is unknown
# ==========================================================================
def test_a_day_with_no_sends_has_no_bounce_rate() -> None:
    """0% and "nothing sent yet" look identical on a dashboard and mean
    opposite things: one is a mailbox behaving, the other is a mailbox that has
    not been asked to. Returning 0.0 here is how a stalled run reads as a
    healthy one at eight in the morning."""
    assert report(box("idle", sent=0, allowed=5)).bounce_rate_today is None


def test_a_clean_day_reports_zero_rather_than_nothing() -> None:
    """The other half of the distinction. Once something has been sent, zero is
    a real measurement and must be shown as one."""
    day = report(box("busy", sent=20, allowed=26), bounced=0)

    assert day.bounce_rate_today == 0.0


def test_the_rate_is_todays_bounces_over_todays_sends() -> None:
    day = report(box("busy", sent=20, allowed=26), bounced=1)

    assert day.bounce_rate_today == pytest.approx(0.05)


# ==========================================================================
# Against the database
# ==========================================================================
@pytest.mark.asyncio
async def test_a_send_today_is_counted_and_placed_in_the_hour_it_happened(
    db_session, workspace
) -> None:
    """The headline number, and the shape behind it. A run that stops at 10:00
    and one spread to 17:00 have the same total and are different situations."""
    fixture = await build_sendable(db_session, workspace, suffix="dr1")
    sent_at = dt.datetime.combine(NOW.date(), dt.time(9, 30), tzinfo=dt.UTC)
    await db_session.execute(
        update(Message)
        .where(Message.id == fixture.message_id)
        .values(sent_at=sent_at, state="sent")
    )
    await db_session.commit()

    day = await day_report.build(db_session, workspace, now=NOW)

    assert day.sent == 1
    assert day.hourly[9] == 1
    assert sum(day.hourly) == 1
    assert [b.sent for b in day.mailboxes if b.sender_identity_id == str(
        fixture.sender_id
    )] == [1]


@pytest.mark.asyncio
async def test_yesterdays_sends_do_not_count_towards_today(
    db_session, workspace
) -> None:
    """The window is the UTC day, matching ``quota_counters.window_date`` and
    ``sender_health_snapshots.captured_on`` so the three agree about which day
    today is. A send at 23:59 yesterday belongs to yesterday."""
    fixture = await build_sendable(db_session, workspace, suffix="dr2")
    await db_session.execute(
        update(Message)
        .where(Message.id == fixture.message_id)
        .values(
            sent_at=dt.datetime.combine(
                NOW.date() - dt.timedelta(days=1), dt.time(23, 59), tzinfo=dt.UTC
            ),
            state="sent",
        )
    )
    await db_session.commit()

    day = await day_report.build(db_session, workspace, now=NOW)

    assert day.sent == 0
    assert sum(day.hourly) == 0


@pytest.mark.asyncio
async def test_a_mailbox_whose_authentication_lapsed_is_allowed_nothing(
    db_session, workspace
) -> None:
    """Authentication is not negotiable by health, and the reason has to say so.

    A mailbox with no DKIM record reported as "0 of 50 a day: health is
    unknown" sends an operator to look at the bounce rate. The fix is a DNS
    record, and only the exclusion reason points there.
    """
    fixture = await build_sendable(db_session, workspace, suffix="dr3")
    await db_session.execute(
        update(SenderIdentity)
        .where(SenderIdentity.id == fixture.sender_id)
        .values(dkim_ok=False)
    )
    await db_session.commit()

    day = await day_report.build(db_session, workspace, now=NOW)
    mine = [b for b in day.mailboxes if b.sender_identity_id == str(fixture.sender_id)]

    assert len(mine) == 1
    assert mine[0].allowed == 0
    assert mine[0].remaining == 0
    assert "DKIM" in mine[0].note
    assert day.ceiling == 0


@pytest.mark.asyncio
async def test_a_deferral_is_reported_in_the_gates_own_words(
    db_session, workspace
) -> None:
    """"Outside the send window" and "Mon 31 Aug is Late Summer Bank Holiday in
    GB" are the same refusal. Only the second tells an operator whether to act
    on it, and paraphrasing it here would throw away the one thing the deferral
    knows that the dashboard does not."""
    fixture = await build_sendable(db_session, workspace, suffix="dr4")
    reason = "outside_campaign_send_window: Mon 31 Aug is Late Summer Bank Holiday in GB"
    retry = NOW + dt.timedelta(hours=14)
    await db_session.execute(
        update(OutboxMessage)
        .where(OutboxMessage.id == fixture.outbox_id)
        .values(status="deferred", blocked_reason=reason, next_attempt_at=retry)
    )
    await db_session.commit()

    day = await day_report.build(db_session, workspace, now=NOW)

    assert day.deferrals == (Deferral(reason=reason, count=1, next_attempt_at=retry),)
    assert day.queued == 1


@pytest.mark.asyncio
async def test_deferrals_are_grouped_and_ordered_by_how_many_are_waiting(
    db_session, workspace
) -> None:
    """A list of a hundred identical sentences is not a reason, it is a log.
    The count is the part that says which blocker is worth clearing first."""
    first = await build_sendable(db_session, workspace, suffix="dr5a")
    second = await build_sendable(db_session, workspace, suffix="dr5b")
    third = await build_sendable(db_session, workspace, suffix="dr5c")
    common = "recipient_quiet_hours: local time for Europe/Dublin is inside quiet hours"
    rare = "deliverability: hard-bounce rate 3.06% over 98 sends"
    for outbox_id, why in (
        (first.outbox_id, common),
        (second.outbox_id, common),
        (third.outbox_id, rare),
    ):
        await db_session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == outbox_id)
            .values(status="deferred", blocked_reason=why)
        )
    await db_session.commit()

    day = await day_report.build(db_session, workspace, now=NOW)

    assert [(d.reason, d.count) for d in day.deferrals] == [(common, 2), (rare, 1)]


@pytest.mark.asyncio
async def test_a_deferral_with_no_recorded_reason_still_appears(
    db_session, workspace
) -> None:
    """Silence is the one state that must not be silent. A queue that is not
    moving and cannot say why is the situation an operator most needs to see,
    and dropping the row would leave the ``queued`` total unexplained."""
    fixture = await build_sendable(db_session, workspace, suffix="dr6")
    await db_session.execute(
        update(OutboxMessage)
        .where(OutboxMessage.id == fixture.outbox_id)
        .values(status="deferred", blocked_reason=None, last_error=None)
    )
    await db_session.commit()

    day = await day_report.build(db_session, workspace, now=NOW)

    assert [d.count for d in day.deferrals] == [1]
    assert day.deferrals[0].reason == "no reason recorded"


@pytest.mark.asyncio
async def test_another_workspaces_day_is_not_reported_as_this_ones(
    db_session, workspace, second_workspace
) -> None:
    """Every statement here is raw ``text()``, which does not pass through the
    ORM loader criteria that scope everything else, and row-level security is
    not load-bearing for the application role. So the scoping is written into
    each query, and this is what proves it."""
    mine = await build_sendable(db_session, workspace, suffix="dr7a")
    theirs = await build_sendable(db_session, second_workspace, suffix="dr7b")
    for message_id in (mine.message_id, theirs.message_id):
        await db_session.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(sent_at=NOW - dt.timedelta(hours=1), state="sent")
        )
    await db_session.commit()

    day = await day_report.build(db_session, workspace, now=NOW)

    assert day.sent == 1
    assert [b.sender_identity_id for b in day.mailboxes] == [str(mine.sender_id)]
    assert str(theirs.sender_id) not in {b.sender_identity_id for b in day.mailboxes}


@pytest.mark.asyncio
async def test_a_soft_bounce_today_does_not_count_against_the_day(
    db_session, workspace
) -> None:
    """Same rule as every other reputation query in the codebase, and the
    invariant test in tests/invariants/test_bounce_predicate.py polices it. A
    4.x.x is a real mailbox that was briefly unable to accept."""
    fixture = await build_sendable(db_session, workspace, suffix="dr8")
    await db_session.execute(
        update(Message)
        .where(Message.id == fixture.message_id)
        .values(
            sent_at=NOW - dt.timedelta(hours=2),
            bounced_at=NOW - dt.timedelta(hours=1),
            bounce_kind="soft",
            state="bounced",
        )
    )
    await db_session.commit()

    day = await day_report.build(db_session, workspace, now=NOW)

    assert day.bounced == 0
    assert day.bounce_rate_today == 0.0


@pytest.mark.asyncio
async def test_a_hard_bounce_today_counts(db_session, workspace) -> None:
    fixture = await build_sendable(db_session, workspace, suffix="dr9")
    await db_session.execute(
        update(Message)
        .where(Message.id == fixture.message_id)
        .values(
            sent_at=NOW - dt.timedelta(hours=2),
            bounced_at=NOW - dt.timedelta(hours=1),
            bounce_kind="hard",
            state="bounced",
        )
    )
    await db_session.commit()

    day = await day_report.build(db_session, workspace, now=NOW)

    assert day.bounced == 1
    assert day.bounce_rate_today == 1.0


@pytest.mark.asyncio
async def test_an_unrecognised_health_status_does_not_break_the_page(
    db_session, workspace
) -> None:
    """``sender_health_snapshots.status`` is deliberately a string rather than a
    database enum so the classifier's vocabulary can move without a migration.
    The cost of that choice is that a report has to survive reading a word it
    does not know, and a 500 on the overview is a worse outcome than a mailbox
    shown as ``unknown`` for one deploy."""
    fixture = await build_sendable(db_session, workspace, suffix="dr10")
    db_session.add(
        SenderHealthSnapshot(
            workspace_id=workspace,
            sender_identity_id=fixture.sender_id,
            sending_domain="example.test",
            captured_on=NOW.date(),
            status="convalescent",
        )
    )
    await db_session.commit()

    day = await day_report.build(db_session, workspace, now=NOW)

    assert [b.health for b in day.mailboxes] == ["unknown"]
