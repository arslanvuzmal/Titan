"""Leads parked under a bar that has since moved.

A lead's status is decided at the moment it is scored: at or above the
campaign's threshold it becomes ``QUALIFIED``, below it ``MANUAL_REVIEW``. The
score is a measurement and keeps its meaning; **the threshold is campaign
policy and moves**. When it moved from 70 to 55, every lead already parked
stayed parked, because nothing asks the question a second time.

``RESEARCHABLE_STATUSES`` does not include ``MANUAL_REVIEW``, so the planner
cannot see them either. The result is a bucket that only fills: 3,558 leads on
9 September, **3,412 of them at or above their own campaign's current gate**,
1,608 with a verified sendable address, and not one ever written to. Meanwhile
the same campaigns filed 457 notices reading *"Campaign has budget but no
eligible leads"*.

This is the same shape as three other faults found the same week -- evidence
measured once and quoted weeks later, an address chosen at research and never
reconsidered, a schedule judged on a prediction that had gone stale. A value
that was right when it was written, read later as though it still were.

**It only ever promotes out of MANUAL_REVIEW.** Nothing else. A lead that was
rejected, suppressed, replied to or archived stays where it is: those are
decisions about the business, not a comparison against a number that has since
changed, and re-admitting them would write to somebody who asked us not to.
"""

from __future__ import annotations

import dataclasses
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: How many leads one pass promotes.
#:
#: Bounded like every other housekeeping pass. The backlog is 3,412 and the
#: send gates gate what actually leaves, but promoting the lot in one statement
#: would hand the planner a step change it has never seen and make the cause of
#: any resulting surge impossible to read against the hourly cadence.
DEFAULT_BATCH = 250

_PROMOTE = text(
    """
    WITH due AS (
        SELECT l.id
          FROM leads l
          JOIN campaign_policies cp ON cp.campaign_id = l.campaign_id
         WHERE l.workspace_id = :ws
           AND l.status = 'manual_review'
           AND l.latest_score IS NOT NULL
           AND l.latest_score >= cp.min_lead_score
         ORDER BY l.latest_score DESC, l.id
         LIMIT :limit
    )
    UPDATE leads
       SET status = 'qualified'
      FROM due
     WHERE leads.id = due.id
    RETURNING leads.id
    """
)

_REMAINING = text(
    """
    SELECT count(*)
      FROM leads l
      JOIN campaign_policies cp ON cp.campaign_id = l.campaign_id
     WHERE l.workspace_id = :ws
       AND l.status = 'manual_review'
       AND l.latest_score IS NOT NULL
       AND l.latest_score >= cp.min_lead_score
    """
)


@dataclasses.dataclass(frozen=True, slots=True)
class ReadmissionReport:
    """What one pass promoted, and what is still waiting."""

    promoted: int
    #: Leads still parked above their own gate after this pass. Reported so the
    #: backlog draining is visible hour by hour rather than only when it ends.
    remaining: int


async def readmit(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    limit: int = DEFAULT_BATCH,
) -> ReadmissionReport:
    """Return leads that now clear their campaign's gate to the pipeline.

    Highest score first, so the best of a large backlog is worked before the
    marginal, and ``id`` as the tiebreak so a retry promotes the same leads
    rather than a different slice of an equal-scoring band.

    Compares against the campaign's threshold *live* rather than against a
    stored copy: the autonomy manager moves that number, and reading a snapshot
    would rebuild the very staleness this exists to remove.
    """
    promoted = (
        await session.execute(_PROMOTE, {"ws": workspace_id, "limit": max(0, limit)})
    ).rowcount or 0
    remaining = int(await session.scalar(_REMAINING, {"ws": workspace_id}) or 0)
    return ReadmissionReport(int(promoted), remaining)


__all__ = ["DEFAULT_BATCH", "ReadmissionReport", "readmit"]
