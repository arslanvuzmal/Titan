"""Free the leads trapped behind research runs that never closed.

The finding: 1,071 research runs on the live workspace with status ``running``
and **none** with status ``completed`` -- 873 of them older than six hours, the
oldest twelve days old. The status column was written once, at creation, and
never again.

Behind each open run sits a lead in ``RESEARCHING``, a status the orchestrator's
``RESEARCHABLE_STATUSES`` does not include. Those leads were not delayed; they
were finished with. 597 of them.

This module decides *which* runs have plainly stopped and nothing else. The
lead goes back to ``DISCOVERED`` and the ordinary pipeline re-applies every
gate it would have applied anyway -- campaign eligibility, scoring, contact
verification, suppression. The sweeper skips nothing, because it decides
nothing.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from temporalio import activity

from titan.db.session import workspace_unit_of_work
from titan.intelligence.stale_runs import DEFAULT_BATCH, reopen
from titan.workflows.types import ReopenStaleRunsInput, ReopenStaleRunsResult

logger = logging.getLogger(__name__)


@activity.defn(name="reopen_stale_research_runs")
async def reopen_stale_research_runs(
    request: ReopenStaleRunsInput,
) -> ReopenStaleRunsResult:
    """Close abandoned runs and return their leads to the queue."""
    workspace_id = uuid.UUID(request.workspace_id)
    limit = request.limit or DEFAULT_BATCH
    now = dt.datetime.now(dt.UTC)

    async with workspace_unit_of_work(workspace_id) as session:
        stale = await reopen(session, workspace_id=workspace_id, now=now, limit=limit)

    if not stale:
        return ReopenStaleRunsResult(found=0, reopened=0)

    oldest = min(run.started_at for run in stale)
    age_hours = int((now - oldest).total_seconds() // 3600)
    logger.info(
        "reopened abandoned research runs",
        extra={
            "workspace_id": request.workspace_id,
            "reopened": len(stale),
            "oldest_age_hours": age_hours,
        },
    )
    return ReopenStaleRunsResult(
        found=len(stale), reopened=len(stale), oldest_age_hours=age_hours
    )


__all__ = ["reopen_stale_research_runs"]
