"""What actually went out today, and what is left of the day.

Every other view in the CRM answers a question about the whole history: leads by
status, messages by state, outcomes over thirty days. None of them answers the
one an operator asks first thing in the morning and again at four -- *is it
sending?* -- and the absence had a real cost. On 31 August the estate spent the
morning at nine sends against a ceiling of twenty-six and nothing on any screen
said so; the shortfall was found by hand, in psql, after the day was half gone.

**A ceiling, not a forecast.** ``ceiling`` is what the gate would permit if
there were a message ready for every slot. It is not a prediction that the
number will be reached: a message also needs an open send window in the
recipient's timezone, and a queue to draw from. The gap between ``sent`` and
``ceiling`` is therefore a question, not a fault, and :attr:`DayReport.deferrals`
is where the answer is -- which is why they are reported together rather than
as separate screens.

**Capacity is not pooled.** ``remaining`` sums each mailbox's own headroom
rather than subtracting today's sends from the total. A mailbox that has spent
its five cannot borrow another's, so a single subtraction would advertise room
that no message could actually use.

**Health is read, not recomputed.** ``sender_health_snapshots`` is the record
the system keeps of its own verdicts: the daily activity writes one per sender,
and the outbox worker refreshes it inside the transaction of every send. Asking
it is therefore both current and *the same answer the gate used*, which a fresh
recomputation here would not guarantee -- two nearly-identical reputation
queries drifting apart is precisely the bug ``tests/invariants/
test_bounce_predicate.py`` exists to prevent, and adding a fourth copy to be
tidy would be adding the risk it polices. When a snapshot is older than today,
:attr:`MailboxDay.health_as_of` says so rather than passing it off as current.

**The ceiling is computed by the same functions that enforce it.**
:func:`titan.delivery.adaptive_limits.daily_limit` and
:func:`titan.delivery.deliverability.warmup_limit` are called here, not
reimplemented. A dashboard that models the throttle instead of asking it is a
dashboard that will one day be confidently wrong about why nothing is sending.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from titan.delivery import adaptive_limits, deliverability, sender_pool
from titan.delivery.bounces import COUNTS_AGAINST_REPUTATION
from titan.delivery.sender_health import SenderHealth

#: Outbox rows that still expect to send today or later. Shared with the pool
#: so "queued" means the same thing on this page as it does at selection.
IN_FLIGHT_STATUSES = sender_pool.UNRESOLVED_STATUSES

#: How many distinct deferral reasons to name before collapsing the rest.
#: Long enough to hold the four or five that actually recur -- a holiday, a
#: quiet-hours window, a blocked mailbox, a spent quota -- and short enough
#: that the section stays readable.
MAX_REASONS = 8


@dataclass(frozen=True, slots=True)
class MailboxDay:
    """One mailbox's day: what it sent, what it may send, and why."""

    sender_identity_id: str
    label: str
    from_email: str
    #: Messages this mailbox has sent since 00:00 UTC.
    sent: int
    #: What the gate would allow it today. Zero when it may not send at all.
    allowed: int
    #: The number a human configured. ``allowed`` never exceeds it.
    configured: int
    #: Outbox rows assigned to this mailbox and not yet resolved.
    queued: int
    health: str
    #: The date of the health snapshot behind ``health``. Older than today
    #: means nothing has sent from this mailbox today *and* the daily capture
    #: has not run yet -- worth showing rather than quietly presenting a stale
    #: verdict as a current one.
    health_as_of: dt.date | None
    warmup_day: int | None
    warmup_days: int
    #: The throttle's own sentence, or the reason the mailbox is excluded.
    note: str
    #: The classifier's words, when it had any. Empty for a healthy mailbox.
    reasons: tuple[str, ...] = ()

    @property
    def remaining(self) -> int:
        """Room left today. Never negative: a mailbox over its limit is full,
        not owed -- the same rule the pool applies to headroom."""
        return max(0, self.allowed - self.sent)

    @property
    def sending(self) -> bool:
        return self.allowed > 0


@dataclass(frozen=True, slots=True)
class Deferral:
    """One reason messages are waiting, and how many are waiting on it."""

    reason: str
    count: int
    #: The earliest moment any of them will be retried. None when the rows
    #: carry no next attempt, which should not happen and is not worth raising.
    next_attempt_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class DayReport:
    """Today, as the sending estate sees it."""

    as_of: dt.datetime
    #: The UTC date these numbers cover, matching ``quota_counters.window_date``
    #: and ``sender_health_snapshots.captured_on`` so the three agree about
    #: which day "today" is.
    window_date: dt.date

    sent: int
    ceiling: int
    delivered: int
    bounced: int
    complained: int
    #: Sends that failed outright today -- refused by the provider, or by the
    #: final authorization check. Distinct from a bounce, which is a message
    #: that left and came back.
    failed: int

    #: Outbox rows waiting, in any unresolved state.
    queued: int

    #: Sends per hour since midnight UTC, 24 buckets. A day's shape says
    #: something a total does not: a run that stops at 10:00 and a run spread
    #: to 17:00 are different situations with the same count.
    hourly: tuple[int, ...] = field(default_factory=lambda: (0,) * 24)

    mailboxes: tuple[MailboxDay, ...] = ()
    deferrals: tuple[Deferral, ...] = ()
    #: The day that just finished, so the panel is never entirely zeroes.
    #:
    #: The operator opens this before any send window has opened, when the
    #: honest answer for today is nought -- and a screen of nothing but zeroes
    #: reads as a broken system. He read it that way every morning, correctly,
    #: because nothing on it said otherwise. Yesterday is what makes an empty
    #: morning legible.
    previous_date: dt.date | None = None
    previous_sent: int = 0
    previous_bounced: int = 0

    @property
    def remaining(self) -> int:
        """Messages the estate could still send today.

        Summed per mailbox rather than ``ceiling - sent``: capacity does not
        pool, so a mailbox that has spent its allowance cannot lend the balance
        to one that has not.
        """
        return sum(box.remaining for box in self.mailboxes)

    @property
    def bounce_rate_today(self) -> float | None:
        """Today's bounce rate, or None when too little has been sent to say.

        Deliberately not floored at a sample size here: the caller decides how
        to render it, and today's rate is a tripwire rather than a verdict --
        the thirty-day window on the performance page is the number that
        governs anything.
        """
        return (self.bounced / self.sent) if self.sent else None


async def build(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    now: dt.datetime,
) -> DayReport:
    """Assemble today's report for one workspace.

    ``workspace_id`` is written into every statement rather than left to the
    session: these are raw ``text()`` queries, which do not pass through the
    ORM loader criteria that scope everything else, and row-level security is
    not load-bearing for the application role.
    """
    day_start = dt.datetime.combine(now.date(), dt.time.min, tzinfo=dt.UTC)
    params = {"workspace": workspace_id, "day_start": day_start}

    totals = (
        await session.execute(
            text(
                f"""
                SELECT
                  count(*) FILTER (WHERE sent_at >= :day_start)         AS sent,
                  count(*) FILTER (WHERE delivered_at >= :day_start)    AS delivered,
                  -- Hard and unknown, never soft. See
                  -- titan.delivery.bounces.COUNTS_AGAINST_REPUTATION.
                  count(*) FILTER (
                      WHERE bounced_at >= :day_start
                        AND {COUNTS_AGAINST_REPUTATION}
                  )                                                     AS bounced,
                  count(*) FILTER (WHERE complained_at >= :day_start)   AS complained
                  FROM messages
                 WHERE workspace_id = :workspace
                """
            ),
            params,
        )
    ).one()

    failed = int(
        await session.scalar(
            text(
                """
                SELECT count(*) FROM outbox_messages
                 WHERE workspace_id = :workspace
                   AND status = 'failed_permanent'
                   AND updated_at >= :day_start
                """
            ),
            params,
        )
        or 0
    )

    hourly = [0] * 24
    for hour, count in await session.execute(
        text(
            """
            SELECT EXTRACT(HOUR FROM sent_at AT TIME ZONE 'UTC')::int AS hour,
                   count(*)
              FROM messages
             WHERE workspace_id = :workspace
               AND sent_at >= :day_start
             GROUP BY 1
            """
        ),
        params,
    ):
        if 0 <= hour < 24:
            hourly[hour] = int(count)

    mailboxes = await _mailboxes(session, workspace_id, now=now, day_start=day_start)
    addresses = {box.sender_identity_id: box.from_email for box in mailboxes}

    deferrals = tuple(
        Deferral(
            reason=_readable(str(row.reason), addresses),
            count=int(row.count),
            next_attempt_at=row.next_attempt_at,
        )
        for row in await session.execute(
            text(
                """
                SELECT coalesce(blocked_reason, last_error, 'no reason recorded')
                                                    AS reason,
                       count(*)                     AS count,
                       min(next_attempt_at)         AS next_attempt_at
                  FROM outbox_messages
                 WHERE workspace_id = :workspace
                   AND status = ANY(CAST(:in_flight AS outbox_status[]))
                 GROUP BY 1
                 ORDER BY 2 DESC, 1
                 LIMIT :cap
                """
            ),
            {
                **params,
                "in_flight": [s.value for s in IN_FLIGHT_STATUSES],
                "cap": MAX_REASONS,
            },
        )
    )

    # The day that just finished. One indexed aggregate, and the difference
    # between a legible morning and a screen of zeroes.
    previous_start = day_start - dt.timedelta(days=1)
    yesterday = (
        await session.execute(
            text(
                f"""
                SELECT count(*) FILTER (WHERE sent_at IS NOT NULL)    AS sent,
                       -- Hard and unknown, never soft, exactly as today's
                       -- count is measured. A soft bounce is not reputation
                       -- damage, and showing yesterday's total beside today's
                       -- filtered one would put two different questions in
                       -- the same row -- which is what the first version of
                       -- this query did, and the invariant caught it.
                       --
                       -- Spelled out rather than interpolated from
                       -- COUNTS_AGAINST_REPUTATION so the guard in
                       -- tests/invariants/test_bounce_predicate.py can see it:
                       -- that test reads source text, and a constant is
                       -- invisible to it.
                       count(*) FILTER (
                           WHERE bounced_at IS NOT NULL
                             AND bounce_kind IS DISTINCT FROM 'soft'
                       )                                              AS bounced
                  FROM messages
                 WHERE workspace_id = :workspace
                   AND sent_at >= :previous_start
                   AND sent_at < :day_start
                """
            ),
            {
                "workspace": workspace_id,
                "previous_start": previous_start,
                "day_start": day_start,
            },
        )
    ).one()

    return DayReport(
        as_of=now,
        window_date=now.date(),
        previous_date=previous_start.date(),
        previous_sent=int(yesterday.sent or 0),
        previous_bounced=int(yesterday.bounced or 0),
        sent=int(totals.sent or 0),
        ceiling=sum(box.allowed for box in mailboxes),
        delivered=int(totals.delivered or 0),
        bounced=int(totals.bounced or 0),
        complained=int(totals.complained or 0),
        failed=failed,
        queued=sum(box.queued for box in mailboxes),
        hourly=tuple(hourly),
        mailboxes=mailboxes,
        deferrals=deferrals,
    )


def _readable(reason: str, addresses: dict[str, str]) -> str:
    """Put a mailbox's address where the gate wrote its id.

    The quota refusal reads ``sender daily quota of 5 reached for
    '9d38f7ad-4305-4663-a557-056851e2cb99'`` -- correct, and unreadable. An
    operator seeing it on a dashboard cannot tell which mailbox is full without
    going to the database, which is the errand this page exists to remove.

    Substituted here rather than fixed at the source deliberately. The message
    is built inside quota reservation, which runs per message on the send path
    and holds no sender row; resolving the address there would add a lookup to a
    hot path to improve a string. This function already has the mapping in hand,
    and the stored ``blocked_reason`` keeps the id -- the audit record stays
    exact, and only the reading of it changes.
    """
    for sender_id, email in addresses.items():
        if sender_id in reason:
            reason = reason.replace(f"'{sender_id}'", email).replace(sender_id, email)
    return reason


async def _mailboxes(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    now: dt.datetime,
    day_start: dt.datetime,
) -> tuple[MailboxDay, ...]:
    """Every sending identity, with today's allowance worked out for each."""
    rows = (
        await session.execute(
            text(
                f"""
                SELECT
                  si.id,
                  si.label,
                  si.from_email,
                  si.daily_send_limit,
                  si.is_active,
                  si.domain_verified,
                  si.spf_ok,
                  si.dkim_ok,
                  si.dmarc_ok,
                  si.last_verified_at,
                  -- Whichever is earlier, Titan's first send or the provider's
                  -- warm-up start. Read differently here than in the pool is
                  -- how a screen and a gate come to disagree about a ramp.
                  LEAST(
                    (SELECT min(m.sent_at) FROM messages m
                      WHERE m.workspace_id = :workspace
                        AND m.sender_identity_id = si.id
                        AND m.sent_at IS NOT NULL),
                    si.warmup_started_at
                  )                                             AS first_send_at,
                  (SELECT count(*) FROM messages m
                    WHERE m.workspace_id = :workspace
                      AND m.sender_identity_id = si.id
                      AND m.sent_at >= :day_start)              AS sent_today,
                  (SELECT count(*) FROM outbox_messages o
                    WHERE o.workspace_id = :workspace
                      AND o.sender_identity_id = si.id
                      AND o.status = ANY(CAST(:in_flight AS outbox_status[])))
                                                                AS queued,
                  (SELECT max(m.bounced_at) FROM messages m
                    WHERE m.workspace_id = :workspace
                      AND m.sender_identity_id = si.id
                      AND {COUNTS_AGAINST_REPUTATION})          AS last_bounce_at
                  FROM sender_identities si
                 WHERE si.workspace_id = :workspace
                 ORDER BY si.from_email
                """
            ),
            {
                "workspace": workspace_id,
                "day_start": day_start,
                "in_flight": [s.value for s in IN_FLIGHT_STATUSES],
            },
        )
    ).all()

    history = await _health_history(session, workspace_id, now=now)

    boxes: list[MailboxDay] = []
    for row in rows:
        recent, as_of, reasons = history.get(row.id, ((), None, ()))
        excluded = sender_pool.unavailable_reason(row)
        configured = int(row.daily_send_limit or 0)
        warmup = deliverability.warmup_limit(
            first_send_at=row.first_send_at, now=now, target=configured
        )
        last_bounce = row.last_bounce_at
        decision = adaptive_limits.daily_limit(
            configured,
            recent=recent,
            warmup_limit=warmup,
            days_since_bounce=(
                None if last_bounce is None else max(0, (now - last_bounce).days)
            ),
        )
        boxes.append(
            MailboxDay(
                sender_identity_id=str(row.id),
                label=row.label,
                from_email=row.from_email,
                sent=int(row.sent_today or 0),
                # An excluded mailbox is allowed nothing regardless of what the
                # throttle would say. The pool applies the same precedence, and
                # so does the gate: authentication is not negotiable by health.
                allowed=0 if excluded else decision.effective,
                configured=configured,
                queued=int(row.queued or 0),
                health=(recent[0] if recent else SenderHealth.UNKNOWN).value,
                health_as_of=as_of,
                warmup_day=(
                    None
                    if warmup is None
                    else deliverability.warmup_day(row.first_send_at, now) + 1
                ),
                warmup_days=deliverability.WARMUP_DAYS,
                note=excluded or decision.explain(),
                reasons=reasons,
            )
        )
    return tuple(boxes)


async def _health_history(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    now: dt.datetime,
) -> dict[uuid.UUID, tuple[tuple[SenderHealth, ...], dt.date | None, tuple[str, ...]]]:
    """Each sender's recent verdicts, newest first, with the newest one's date.

    ``adaptive_limits.daily_limit`` reads ``recent[0]`` as today's verdict and
    the rest as the recovery lookback, so the ordering is load-bearing. An
    unrecognised status string is dropped rather than raising: the column is
    deliberately not a database enum so the classifier's vocabulary can move,
    and a report is not the place a vocabulary change should surface as a 500.
    """
    out: dict[
        uuid.UUID, tuple[tuple[SenderHealth, ...], dt.date | None, tuple[str, ...]]
    ] = {}
    rows = await session.execute(
        text(
            """
            SELECT sender_identity_id, captured_on, status, reasons
              FROM sender_health_snapshots
             WHERE workspace_id = :workspace
               AND captured_on > :floor
             ORDER BY sender_identity_id, captured_on DESC
            """
        ),
        {
            "workspace": workspace_id,
            "floor": now.date()
            - dt.timedelta(days=adaptive_limits.RECOVERY_LOOKBACK_DAYS),
        },
    )
    for row in rows:
        try:
            status = SenderHealth(row.status)
        except ValueError:
            continue
        statuses, as_of, reasons = out.get(row.sender_identity_id, ((), None, ()))
        out[row.sender_identity_id] = (
            (*statuses, status),
            as_of or row.captured_on,
            reasons or tuple(str(r) for r in (row.reasons or ())),
        )
    return out


__all__ = [
    "IN_FLIGHT_STATUSES",
    "MAX_REASONS",
    "DayReport",
    "Deferral",
    "MailboxDay",
    "build",
]
