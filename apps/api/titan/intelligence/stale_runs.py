"""Research runs that will never finish, and the leads trapped behind them.

``ResearchRun`` was created with ``status="running"`` and, until now, nothing
in the repository ever wrote that column again. Not on success, not on failure.
Measured on the live workspace: **1,071 runs running, 0 completed**, 873 of them
older than six hours, the oldest started twelve days ago.

The run row is the smaller half of the problem. ``start_research`` also sets the
lead to ``RESEARCHING``, and ``RESEARCHABLE_STATUSES`` in the orchestrator is
``(DISCOVERED, RESEARCHED, QUALIFIED)`` -- ``RESEARCHING`` is not in it. So a
lead whose research workflow dies for any reason is not retried later; it is
never looked at again. 597 leads sit in that state today.

The workflow can be gone for entirely ordinary reasons: the worker was
restarted mid-run, the activity exhausted its retries, the machine slept. None
of those are conditions the lead did anything to deserve.

**``abandoned``, not ``failed``.** A failure is a diagnosis, and nobody made
one. All that is known here is that the run stopped reporting and the deadline
passed -- the crawl may well have succeeded and died before recording it.
Writing ``failed`` would claim knowledge the sweeper does not have, and would
poison exactly the rates loop 5 exists to measure.

**Its only opinion is about existence.** The lead goes back to ``DISCOVERED``
and the ordinary pipeline decides everything else: whether the campaign still
wants it, whether it scores, whether it is reachable. The sweeper never skips a
gate, because it never makes a judgement to skip one with.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import LeadStatus
from titan.db.models import Lead
from titan.db.models.research import ResearchRun

#: How long a run may stay open before it is presumed abandoned.
#:
#: A research pass is crawl plus deterministic analysis -- minutes, not hours.
#: Six hours is far beyond any legitimate run and still short enough that a
#: lead stranded by an overnight restart is back in the pipeline by morning.
#: The cost of being wrong is one repeated crawl; the cost of never sweeping is
#: a lead that is never contacted at all.
STALE_AFTER = dt.timedelta(hours=6)

#: How many to reopen in one pass.
#:
#: Bounded because every lead returned to DISCOVERED buys another crawl and
#: another analysis, and 873 of them arriving at once would spend a day's
#: budget in a minute. A backlog that built over twelve days can drain over
#: several passes.
DEFAULT_BATCH = 100


@dataclass(frozen=True, slots=True)
class StaleRun:
    run_id: uuid.UUID
    lead_id: uuid.UUID
    started_at: dt.datetime


async def find_stale_runs(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    now: dt.datetime,
    limit: int = DEFAULT_BATCH,
) -> list[StaleRun]:
    """Open runs past the deadline whose lead is still waiting on them.

    Both conditions are required. A run left open whose lead has since moved on
    -- reopened by an earlier sweep, or advanced by a path that did not close
    the run -- is stale bookkeeping, not a trapped lead, and returning that lead
    to DISCOVERED would research it a second time.

    Oldest first, so the leads that have waited longest are freed first.
    """
    rows = (
        await session.execute(
            select(ResearchRun.id, ResearchRun.lead_id, ResearchRun.started_at)
            .join(Lead, Lead.id == ResearchRun.lead_id)
            .where(
                ResearchRun.workspace_id == workspace_id,
                ResearchRun.status == "running",
                ResearchRun.started_at < now - STALE_AFTER,
                Lead.status == LeadStatus.RESEARCHING,
            )
            .order_by(ResearchRun.started_at)
            .limit(limit)
        )
    ).all()
    return [StaleRun(run_id=r[0], lead_id=r[1], started_at=r[2]) for r in rows]


async def reopen(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    now: dt.datetime,
    limit: int = DEFAULT_BATCH,
) -> list[StaleRun]:
    """Close the abandoned runs and put their leads back in the queue.

    Idempotent: the run leaves ``running`` in the same transaction the lead
    leaves ``RESEARCHING``, so a second pass selects neither.
    """
    stale = await find_stale_runs(
        session, workspace_id=workspace_id, now=now, limit=limit
    )
    for run in stale:
        age = now - run.started_at
        record = await session.get(ResearchRun, run.run_id)
        if record is not None:
            record.status = "abandoned"
            record.finished_at = now
            record.failure_reason = (
                f"no completion recorded {int(age.total_seconds() // 3600)}h "
                "after start; presumed abandoned and the lead returned to the queue"
            )
        lead = await session.get(Lead, run.lead_id)
        if lead is not None:
            lead.status = LeadStatus.DISCOVERED
            lead.status_reason = "research run abandoned; returned for another pass"
    return stale


__all__ = [
    "DEFAULT_BATCH",
    "STALE_AFTER",
    "StaleRun",
    "find_stale_runs",
    "reopen",
]
