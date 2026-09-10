"""Closing the alarms that describe a moment which has passed.

``tasks`` is write-only. :func:`titan.notify.operator.record_notification`
inserts with ``status="open"`` and nothing anywhere in the codebase has ever
written any other value -- 648 rows, every one of them open, the oldest from
16 August. 607 of those are ``campaign_stalled``.

That is not a backlog somebody fell behind on. It is a queue with no exit.

**What is safe to close, and what is not.** An alarm is a statement about a
moment: *this campaign had budget and nothing eligible, at 12:45 on Tuesday*.
It re-fires while it is still true -- the dedupe key on a stall is
``stalled:{campaign}:{date}``, so a campaign that is still stuck produces a
fresh row tomorrow -- which means an old one carries no information the new one
does not. Closing it loses nothing.

A reply is the opposite. It is a statement about a person who is waiting, it
does not re-fire, and it does not stop being true because a week went by. Those
are never touched here, and the split is the whole design: the expensive
failure is not an alarm left open, it is a prospect quietly closed because a
sweep could not tell the difference.

So this closes only the kinds that are self-repeating observations about the
machine, and only once they are old enough that a live instance would have
replaced them.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from titan.notify.operator import NotificationKind

logger = logging.getLogger(__name__)

#: The status a closed alarm carries.
#:
#: A new value, because none existed. Deliberately not "done": nobody did it,
#: and a queue that reports work as completed when it was actually abandoned is
#: worse than one that admits the difference.
EXPIRED = "expired"

#: Kinds that describe the machine and re-fire while they are still true.
#: Everything absent from this set is either a person or a decision, and is
#: never closed by a sweep.
EXPIRABLE: frozenset[str] = frozenset(
    {
        NotificationKind.CAMPAIGN_STALLED.value,
        NotificationKind.PIPELINE_ALERT.value,
        NotificationKind.DELIVERABILITY_ALERT.value,
        NotificationKind.WEEKLY_REPORT.value,
    }
)

#: How old before an alarm is stale.
#:
#: Longer than the daily dedupe window by a wide margin. A stall alarm is keyed
#: per campaign per day, so anything still wrong has produced six newer rows by
#: the time the seventh day closes this one -- the operator loses a duplicate,
#: never the only copy.
STALE_AFTER_DAYS = 7

_EXPIRE = text(
    """
    UPDATE tasks
       SET status = :expired, updated_at = :now
     WHERE workspace_id = :ws
       AND status = 'open'
       AND kind = ANY(:kinds)
       AND created_at < :cutoff
    """
)

_REMAINING = text(
    """
    SELECT count(*) FROM tasks
     WHERE workspace_id = :ws AND status = 'open'
    """
)


@dataclass(frozen=True, slots=True)
class ExpiryReport:
    expired: int = 0
    still_open: int = 0


async def expire_stale_alarms(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    now: dt.datetime,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> ExpiryReport:
    """Close machine alarms older than the window. Never touches a reply.

    Does not commit -- the caller owns the transaction, as everywhere else in
    this package.
    """
    cutoff = now - dt.timedelta(days=stale_after_days)
    result = await session.execute(
        _EXPIRE,
        {
            "expired": EXPIRED,
            "now": now,
            "ws": workspace_id,
            "kinds": sorted(EXPIRABLE),
            "cutoff": cutoff,
        },
    )
    remaining = await session.scalar(_REMAINING, {"ws": workspace_id}) or 0
    report = ExpiryReport(expired=result.rowcount or 0, still_open=int(remaining))
    if report.expired:
        logger.info(
            "closed %s stale alarm(s); %s task(s) still open",
            report.expired,
            report.still_open,
        )
    return report


__all__ = [
    "EXPIRABLE",
    "EXPIRED",
    "STALE_AFTER_DAYS",
    "ExpiryReport",
    "expire_stale_alarms",
]
