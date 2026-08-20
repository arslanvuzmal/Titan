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
from titan.activities.research import requires_human_approval
from titan.db.enums import DraftStatus
from titan.db.session import workspace_session
from titan.delivery.stranded import DEFAULT_BATCH, find_stranded
from titan.workflows.types import (
    QueueActivityInput,
    ResearchLeadInput,
    SweepStrandedInput,
    SweepStrandedResult,
)

logger = logging.getLogger(__name__)


@activity.defn(name="sweep_stranded_drafts")
async def sweep_stranded_drafts(request: SweepStrandedInput) -> SweepStrandedResult:
    """Find drafts with nowhere to go, and give them somewhere.

    Two dead ends, and they are not the same. An ``APPROVED`` draft has had its
    decision made and is merely unqueued. One still ``AWAITING_APPROVAL`` has
    had no decision made at all, because the workflow that would have asked
    ended first -- 322 of them on the live workspace against 241 approved.

    So this does not approve anything. It asks the same
    ``requires_human_approval`` gate the workflow would have asked, from the
    same rows, and queues only what that gate says needs no person. A campaign
    that genuinely requires a human decision keeps its drafts.
    """
    workspace_id = uuid.UUID(request.workspace_id)
    limit = request.limit or DEFAULT_BATCH

    async with workspace_session(workspace_id) as session:
        stranded = await find_stranded(session, workspace_id=workspace_id, limit=limit)

    if not stranded:
        return SweepStrandedResult(found=0, queued=0, refused=0)

    queued = 0
    refused = 0
    reasons: dict[str, int] = {}
    # One answer per campaign rather than per draft. The gate reads the
    # workspace and the campaign policy, which do not change between two drafts
    # of the same campaign in the same pass, and there are hundreds of drafts
    # against twenty-three campaigns.
    auto_approves: dict[uuid.UUID, bool] = {}

    for item in stranded:
        if item.status is DraftStatus.AWAITING_APPROVAL:
            if item.campaign_id not in auto_approves:
                auto_approves[item.campaign_id] = not await requires_human_approval(
                    ResearchLeadInput(
                        workspace_id=request.workspace_id,
                        campaign_id=str(item.campaign_id),
                        lead_id=str(item.lead_id),
                        run_key=f"sweep:{item.draft_id}",
                    )
                )
            if not auto_approves[item.campaign_id]:
                # A person genuinely has to decide this one. Not a refusal to
                # record as a fault -- the draft is exactly where it belongs,
                # and the sweeper's job was only to find out whether anything
                # was still coming for it.
                refused += 1
                reasons["awaiting a human decision"] = (
                    reasons.get("awaiting a human decision", 0) + 1
                )
                continue
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
        "swept stranded drafts",
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
