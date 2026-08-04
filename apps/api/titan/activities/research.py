"""Research activities.

Everything the workflow cannot do itself lives here: database writes, the
browser worker call, model calls. Activities may be non-deterministic and may
be retried, so each one is **idempotent on an explicit key** supplied by the
workflow -- a retried activity must find its own prior work rather than
duplicate it.

Activities never receive a policy from their caller. They read campaign policy
from the database at execution time, which is what makes invariant 18 true even
if a start request were forged.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import select
from temporalio import activity

from titan.config import OperatingMode, Settings, get_settings
from titan.db.enums import LeadStatus
from titan.db.models import (
    CampaignPolicy,
    Lead,
    ResearchRun,
    WorkflowEvent,
    WorkflowRun,
    Workspace,
)
from titan.db.session import workspace_session, workspace_unit_of_work
from titan.policy.modes import Capability, resolve_mode
from titan.workflows.types import (
    RecordEventInput,
    ResearchLeadInput,
)

logger = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@activity.defn(name="open_research_run")
async def open_research_run(request: ResearchLeadInput) -> str:
    """Create (or find) the research run for this workflow execution.

    Idempotent on ``(workspace_id, idempotency_key)``: a retry after a crash
    between insert and acknowledgement returns the existing run rather than
    starting a second crawl.
    """
    workspace_id = uuid.UUID(request.workspace_id)
    key = f"{request.run_key}:run"

    async with workspace_unit_of_work(workspace_id) as session:
        existing = (
            await session.execute(
                select(ResearchRun).where(ResearchRun.idempotency_key == key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return str(existing.id)

        lead = await session.get(Lead, uuid.UUID(request.lead_id))
        if lead is None:
            raise ApplicationErrorCompat(f"lead {request.lead_id} not found")

        policy = (
            await session.execute(
                select(CampaignPolicy).where(
                    CampaignPolicy.campaign_id == uuid.UUID(request.campaign_id)
                )
            )
        ).scalar_one_or_none()

        run = ResearchRun(
            workspace_id=workspace_id,
            lead_id=lead.id,
            campaign_id=uuid.UUID(request.campaign_id),
            idempotency_key=key,
            workflow_id=activity.info().workflow_id,
            status="running",
            started_at=_now(),
            # The policy in force when the run started, so a later edit does
            # not silently rewrite how this run should be read.
            playbook_snapshot=(
                {
                    "operating_mode": policy.operating_mode.value,
                    "min_lead_score": policy.min_lead_score,
                    "require_verified_email": policy.require_verified_email,
                }
                if policy
                else {}
            ),
        )
        session.add(run)
        lead.status = LeadStatus.RESEARCHING
        await session.flush()
        return str(run.id)


@activity.defn(name="requires_human_approval")
async def requires_human_approval(request: ResearchLeadInput) -> bool:
    """Whether a draft needs an explicit human decision.

    Read from the workspace and campaign rows, never from the request. The
    effective mode is the minimum of process, workspace and campaign, so a
    campaign cannot grant itself autopilot.
    """
    settings = get_settings()
    workspace_id = uuid.UUID(request.workspace_id)

    async with workspace_session(workspace_id) as session:
        workspace = await session.get(Workspace, workspace_id)
        policy = (
            await session.execute(
                select(CampaignPolicy).where(
                    CampaignPolicy.campaign_id == uuid.UUID(request.campaign_id)
                )
            )
        ).scalar_one_or_none()

        if workspace is None or policy is None:
            # Fail closed: an unreadable policy means a human decides.
            return True

        process_mode = _process_mode(settings)
        mode = resolve_mode(process_mode, workspace.operating_mode, policy.operating_mode)

    # Anything short of full autopilot requires a human before queueing.
    return not mode.allows(Capability.AUTO_APPROVE)


def _process_mode(settings: Settings) -> OperatingMode:
    if not settings.production_sending_enabled:
        return OperatingMode.DRAFT_ONLY
    return OperatingMode.CONTROLLED_AUTOPILOT


@activity.defn(name="record_workflow_event")
async def record_workflow_event(request: RecordEventInput) -> None:
    """Append a workflow event, collapsing duplicates.

    ``UNIQUE(workflow_run_id, event_key)`` plus ON CONFLICT DO NOTHING is what
    makes a retried activity safe: the same logical event lands once, however
    many times the activity runs (mission section 5.1).
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    workspace_id = uuid.UUID(request.workspace_id)

    async with workspace_unit_of_work(workspace_id) as session:
        run = (
            await session.execute(
                select(WorkflowRun).where(WorkflowRun.workflow_id == request.workflow_id)
            )
        ).scalar_one_or_none()

        if run is None:
            run = WorkflowRun(
                workspace_id=workspace_id,
                workflow_id=request.workflow_id,
                workflow_type="LeadResearchWorkflow",
                task_queue="titan-research",
                started_at=_now(),
            )
            session.add(run)
            await session.flush()

        await session.execute(
            pg_insert(WorkflowEvent.__table__)  # type: ignore[arg-type]
            .values(
                workspace_id=workspace_id,
                workflow_run_id=run.id,
                event_key=request.event_key,
                event_type=request.event_type,
                sequence=request.sequence,
                occurred_at=_now(),
                activity_id=activity.info().activity_id,
                attempt=activity.info().attempt,
                detail=dict(request.detail),
            )
            .on_conflict_do_nothing(index_elements=["workflow_run_id", "event_key"])
        )


class ApplicationErrorCompat(Exception):
    """Raised for conditions a retry cannot fix.

    Named in the workflow's ``non_retryable_error_types`` so Temporal stops
    immediately rather than burning four attempts on a missing row.
    """


__all__ = [
    "ApplicationErrorCompat",
    "open_research_run",
    "record_workflow_event",
    "requires_human_approval",
]
