"""Queue the approved drafts nothing ever queued.

The finding: 225 drafts on the live workspace, approved and validated, with no
outbox row and no message. Mail that was researched, composed, validated and
authorised, and that would never have left, because the workflow that would
have queued it was no longer running when its approval arrived.

Every draft is handed to ``queue_message`` -- the same activity the workflow
calls, applying the same gates in the same order. This module chooses *which*
drafts to offer and nothing else, so a draft that should not go out is refused
by the same code that would have refused it on the original path.
"""

from __future__ import annotations

import logging
import uuid

from temporalio import activity

from titan.activities.pipeline import queue_message
from titan.db.session import workspace_session
from titan.delivery.stranded import DEFAULT_BATCH, find_stranded
from titan.workflows.types import (
    QueueActivityInput,
    SweepStrandedInput,
    SweepStrandedResult,
)

logger = logging.getLogger(__name__)


@activity.defn(name="sweep_stranded_drafts")
async def sweep_stranded_drafts(request: SweepStrandedInput) -> SweepStrandedResult:
    """Find approved drafts with nowhere to go, and give them somewhere."""
    workspace_id = uuid.UUID(request.workspace_id)
    limit = request.limit or DEFAULT_BATCH

    async with workspace_session(workspace_id) as session:
        stranded = await find_stranded(session, workspace_id=workspace_id, limit=limit)

    if not stranded:
        return SweepStrandedResult(found=0, queued=0, refused=0)

    queued = 0
    refused = 0
    reasons: dict[str, int] = {}
    for item in stranded:
        # The approval exists -- that is what APPROVED means -- but this path
        # does not carry its id. Passing None records "queued by the sweeper",
        # which is true, rather than attaching an approval this activity did
        # not read and cannot vouch for.
        result = await queue_message(
            QueueActivityInput(
                workspace_id=request.workspace_id,
                draft_id=str(item.draft_id),
                approval_id=None,
                idempotency_key=f"sweep:{item.draft_id}",
            )
        )
        if result.queued:
            queued += 1
        else:
            refused += 1
            for reason in result.refused_reasons:
                reasons[reason[:80]] = reasons.get(reason[:80], 0) + 1
        # Guarded: this activity is also run straight from the CLI against a
        # live backlog, where there is no activity context and heartbeating
        # raises. Losing the heartbeat outside Temporal costs nothing -- there
        # is no timeout to hold off -- and an unguarded call turns the operator
        # command into a crash partway through a batch.
        if activity.in_activity():
            activity.heartbeat(f"{queued} queued, {refused} refused")

    logger.info(
        "swept stranded approved drafts",
        extra={
            "workspace_id": request.workspace_id,
            "found": len(stranded),
            "queued": queued,
            "refused": refused,
            "reasons": reasons,
        },
    )
    return SweepStrandedResult(
        found=len(stranded),
        queued=queued,
        refused=refused,
        refused_reasons=tuple(sorted(reasons.items())),
    )


ALL_STRANDED_ACTIVITIES = [sweep_stranded_drafts]

__all__ = ["ALL_STRANDED_ACTIVITIES", "sweep_stranded_drafts"]
