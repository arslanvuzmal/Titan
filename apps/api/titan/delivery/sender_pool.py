"""Which mailbox a message goes out from, when a campaign has several.

A campaign used to carry exactly one ``sender_identity_id``, so its ceiling was
one mailbox's daily limit -- fifty messages -- and the only way past it was to
raise that number. Raising it is the wrong move: fifty a day is not an arbitrary
setting, it is roughly where a cold-outreach mailbox stops looking like a person
and starts looking like a list. Three mailboxes at fifty is a hundred and fifty
a day where each one still behaves normally. One mailbox at a hundred and fifty
is a mailbox that gets filtered.

**Selection is by remaining headroom, not round-robin.** They look the same when
every mailbox is identical and diverge exactly when it matters. With one mailbox
on day two of warm-up (limit 6) beside two healthy ones (limit 50), round-robin
sends a third of the volume at the warming mailbox, which then defers two thirds
of what it was handed -- the campaign runs at the warming mailbox's pace instead
of the pool's. Picking the most-headroom mailbox each time degenerates to
round-robin when the mailboxes match and routes around the warming one when they
do not, without a special case for either.

**Headroom counts work already promised, not just work already done.** Queueing
two hundred messages in one batch reads ``sent_today = 0`` for every mailbox, so
a rule that looked only at sends would put all two hundred on whichever mailbox
sorted first and defer a hundred and fifty of them. Outbox rows that are still
pending count against their mailbox from the moment they are assigned.

**An unhealthy mailbox is excluded, not chosen and then refused.** That is the
whole reason a pool exists: a mailbox whose DKIM record was removed this morning
should cost the campaign its share of the volume, not the campaign's ability to
send at all. Every exclusion carries the reason, because a pool that silently
shrinks to one mailbox looks exactly like a pool that is working.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import OutboxStatus

#: Outbox rows that still expect to send. A row in one of these states has
#: already been promised to its mailbox and must count against it, or a large
#: batch assigns everything to whichever mailbox happens to sort first.
UNRESOLVED_STATUSES = (
    OutboxStatus.PENDING,
    OutboxStatus.LEASED,
    OutboxStatus.DEFERRED,
)

#: How a caller works out one mailbox's ceiling for today. A hook so a caller
#: holding a better answer -- the worker, which has a health snapshot -- can
#: supply one without this module reaching for it.
LimitResolver = Callable[[Any, dt.datetime], int]


@dataclass(frozen=True, slots=True)
class MailboxSlot:
    """One mailbox in a campaign's pool, and what it can still take today."""

    sender_identity_id: uuid.UUID
    label: str
    from_email: str
    #: Today's ceiling for this mailbox. Zero when it is excluded.
    daily_limit: int
    #: Sends already made today plus outbox rows already assigned and unsent.
    committed: int
    #: None when the mailbox may send. A sentence when it may not.
    excluded_because: str | None = None

    @property
    def available(self) -> bool:
        return self.excluded_because is None

    @property
    def headroom(self) -> int:
        """Messages this mailbox can still take today. Never negative -- a
        mailbox over its limit is full, not owed."""
        if not self.available:
            return 0
        return max(0, self.daily_limit - self.committed)


@dataclass(frozen=True, slots=True)
class Selection:
    """The chosen mailbox, or the reason there is not one."""

    slot: MailboxSlot | None
    #: Every slot considered, in the order they were ranked. Kept so a refusal
    #: can say which mailboxes were looked at and what was wrong with each.
    considered: tuple[MailboxSlot, ...] = ()

    @property
    def chosen_id(self) -> uuid.UUID | None:
        return self.slot.sender_identity_id if self.slot else None


def capacity(slots: list[MailboxSlot]) -> int:
    """How many more messages this pool can send today, in total."""
    return sum(s.headroom for s in slots)


def daily_ceiling(slots: list[MailboxSlot]) -> int:
    """The pool's whole-day volume, before anything is spent.

    Distinct from :func:`capacity`, and the difference matters wherever a daily
    budget is being divided. Capacity is what is *left*, so it falls as the day
    is spent; dividing a budget by it at noon would hand each campaign a smaller
    daily limit than it had already used that morning, and the limit would keep
    shrinking every time the allocator ran.

    This is stable across the day instead. Warm-up depends on the day number,
    not the clock, so the answer at 09:00 and at 16:00 is the same -- which is
    the property a daily allocation needs.
    """
    return sum(s.daily_limit for s in slots if s.available)


def choose(slots: list[MailboxSlot]) -> Selection:
    """The mailbox with the most room left today.

    Ties break on the sender id, so a pool of identical mailboxes still assigns
    deterministically -- two workers queueing the same message concurrently pick
    the same mailbox and the dedupe key does its job, rather than producing two
    rows that differ only in which mailbox they were assigned.
    """
    ranked = sorted(
        slots,
        key=lambda s: (-s.headroom, str(s.sender_identity_id)),
    )
    best = ranked[0] if ranked else None
    if best is None or best.headroom <= 0:
        return Selection(slot=None, considered=tuple(ranked))
    return Selection(slot=best, considered=tuple(ranked))


def describe_slot(slot: MailboxSlot) -> str:
    """One mailbox's state today, for a report or a refusal."""
    if slot.excluded_because:
        return f"unavailable: {slot.excluded_because}"
    return f"{slot.committed} of {slot.daily_limit} used today"


def describe(selection: Selection) -> str:
    """Why nothing was chosen, in terms of the individual mailboxes.

    "no sending capacity" is not a useful answer when the fix differs per
    mailbox: one is waiting for tomorrow, another is waiting for a DNS record.
    """
    if selection.slot is not None:
        slot = selection.slot
        return (
            f"{slot.label} <{slot.from_email}>, "
            f"{slot.headroom} of {slot.daily_limit} left today"
        )
    if not selection.considered:
        return "the campaign has no sending mailboxes configured"
    return "; ".join(
        f"{slot.label}: "
        + (slot.excluded_because or f"{slot.committed} of {slot.daily_limit} used")
        for slot in selection.considered
    )


async def load_slots(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID | None,
    *,
    now: dt.datetime,
    limit_for: LimitResolver | None = None,
) -> list[MailboxSlot]:
    """The campaign's pool, with each mailbox's remaining capacity today.

    Falls back to the campaign's own ``sender_identity_id`` when no pool rows
    exist, so a campaign configured before this table existed behaves exactly as
    it did -- a pool of one.

    ``campaign_id=None`` asks for every mailbox in the workspace instead, which
    is what a report wants: the question there is what the whole estate can
    send, not what one campaign may.

    ``workspace_id`` is written into the SQL rather than left to the session:
    ``workspace_session`` scopes ORM queries through loader criteria, which raw
    text does not go through, and RLS is not load-bearing for the application
    role. Every scoped raw query names its own workspace.
    """
    day_start = dt.datetime.combine(now.date(), dt.time.min, tzinfo=dt.UTC)
    rows = (
        await session.execute(
            text(
                """
                WITH pool AS (
                    SELECT cs.sender_identity_id
                      FROM campaign_senders cs
                     WHERE cs.workspace_id = :workspace
                       AND cs.campaign_id = :campaign
                    UNION
                    SELECT c.sender_identity_id
                      FROM campaigns c
                     WHERE c.workspace_id = :workspace
                       AND c.id = :campaign
                       AND c.sender_identity_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM campaign_senders x
                            WHERE x.workspace_id = :workspace
                              AND x.campaign_id = :campaign
                       )
                    UNION
                    -- The whole estate, when no campaign was named. The two
                    -- branches above match nothing in that case, so this is a
                    -- switch rather than an addition.
                    SELECT si2.id
                      FROM sender_identities si2
                     WHERE si2.workspace_id = :workspace
                       AND CAST(:campaign AS uuid) IS NULL
                )
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
                  -- The earlier of what Titan sent and when the provider says
                  -- the mailbox began warming. Titan's own history is a lower
                  -- bound on a mailbox's age, not its age: a mailbox connected
                  -- and warming for ten days before Titan held a row for it is
                  -- ten days warm. LEAST ignores nulls, so a sender with only
                  -- one of the two behaves exactly as it did before.
                  LEAST(
                    (SELECT min(m.sent_at) FROM messages m
                      WHERE m.workspace_id = :workspace
                        AND m.sender_identity_id = si.id
                        AND m.sent_at IS NOT NULL),
                    si.warmup_started_at
                  )                                          AS first_send_at,
                  (SELECT count(*) FROM messages m
                    WHERE m.workspace_id = :workspace
                      AND m.sender_identity_id = si.id
                      AND m.sent_at >= :day_start)            AS sent_today,
                  (SELECT count(*) FROM outbox_messages o
                    WHERE o.workspace_id = :workspace
                      AND o.sender_identity_id = si.id
                      AND o.status = ANY(CAST(:unresolved AS outbox_status[])))
                                                            AS in_flight
                  FROM pool
                  JOIN sender_identities si ON si.id = pool.sender_identity_id
                 WHERE si.workspace_id = :workspace
                 ORDER BY si.id
                """
            ),
            {
                "workspace": workspace_id,
                "campaign": campaign_id,
                "day_start": day_start,
                "unresolved": [s.value for s in UNRESOLVED_STATUSES],
            },
        )
    ).all()

    resolve = limit_for or _default_limit
    slots: list[MailboxSlot] = []
    for row in rows:
        excluded = _unavailable_reason(row)
        daily_limit = 0 if excluded else resolve(row, now)
        slots.append(
            MailboxSlot(
                sender_identity_id=row.id,
                label=row.label,
                from_email=row.from_email,
                daily_limit=daily_limit,
                committed=int(row.sent_today or 0) + int(row.in_flight or 0),
                excluded_because=excluded,
            )
        )
    return slots


def _unavailable_reason(row: Any) -> str | None:
    """Why this mailbox cannot send at all today.

    Deliberately the same conditions ``SenderIdentity.is_ready_to_send``
    enforces at the gate. Checking them here does not replace that check -- the
    gate still runs, and still refuses -- it stops the pool handing work to a
    mailbox that is certain to refuse it.
    """
    from titan.intelligence.sender_auth import is_stale

    if not row.is_active:
        return "mailbox is inactive"
    if not row.domain_verified:
        return "sending domain is not verified"
    if is_stale(row.last_verified_at):
        return "sending domain has not been re-verified recently"
    missing = [
        name
        for flag, name in (
            (row.spf_ok, "SPF"),
            (row.dkim_ok, "DKIM"),
            (row.dmarc_ok, "DMARC"),
        )
        if not flag
    ]
    if missing:
        return f"{', '.join(missing)} not in place"
    if int(row.daily_send_limit or 0) <= 0:
        return "no daily send limit configured"
    return None


def _default_limit(row: Any, now: dt.datetime) -> int:
    """Today's ceiling for one mailbox: the configured limit, capped by warm-up.

    Health is deliberately *not* applied here. The outbox worker computes it
    from a snapshot it writes in the same transaction as the send, and duplicating
    that read at queue time would give two different answers for the same mailbox
    minutes apart. Selection only needs the ordering to be right, and warm-up is
    the term that actually reorders a pool -- a warming mailbox has a tenth of
    the room, where a degraded one has a quarter and is usually degraded across
    the whole pool anyway. The worker still applies health at the gate.
    """
    from titan.delivery import deliverability

    configured = int(row.daily_send_limit or 0)
    warmup = deliverability.warmup_limit(
        first_send_at=row.first_send_at,
        now=now,
        target=configured,
    )
    return configured if warmup is None else min(configured, warmup)


__all__ = [
    "UNRESOLVED_STATUSES",
    "LimitResolver",
    "MailboxSlot",
    "Selection",
    "capacity",
    "choose",
    "daily_ceiling",
    "describe",
    "describe_slot",
    "load_slots",
]
