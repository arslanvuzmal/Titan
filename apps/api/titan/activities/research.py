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
    CloseResearchRunInput,
    RecordEventInput,
    ResearchLeadInput,
    ResearchOutcome,
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


#: Outcomes after which the lead is worth another pass later, so it returns to
#: the queue rather than being parked. Both mean "nobody reached a judgement
#: about this business" -- an operator stopped the work, or it broke. The
#: stale-run sweeper uses the same destination for the same reason.
_RETRYABLE_OUTCOMES = frozenset(
    {ResearchOutcome.CANCELLED.value, ResearchOutcome.FAILED.value}
)


@activity.defn(name="close_research_run")
async def close_research_run(request: CloseResearchRunInput) -> None:
    """Write the terminal status for a run that stopped short of analysis.

    Idempotent, and deliberately narrow in what it touches.

    **The run is always closed.** A second call finds a run that has already
    left ``running`` and returns without writing, so a workflow replay or an
    activity retry cannot overwrite the first verdict.

    **The lead is moved only if nothing else moved it.** Several activities
    downstream of the crawl park the lead themselves -- scoring rejects it,
    contact resolution sends it to manual review -- and those are better
    informed than this is. Writing over them here would make two writers of one
    field and lose the more specific reason. So the lead is touched only when it
    is still ``RESEARCHING``, which is exactly the case nobody else handled.
    """
    workspace_id = uuid.UUID(request.workspace_id)

    async with workspace_unit_of_work(workspace_id) as session:
        run = await session.get(ResearchRun, uuid.UUID(request.research_run_id))

        # Closing an already-closed run is refused -- writing a second outcome
        # over the first would lose the real one. Advancing the *lead* is not,
        # and used to sit below this return: on a retry that reached a
        # committed run, the run was correctly left alone and the lead was
        # silently left in RESEARCHING.
        #
        # RESEARCHING is absent from RESEARCHABLE_STATUSES, so such a lead is
        # invisible to every later cycle -- never re-planned, never drafted --
        # and find_stale_runs cannot see it either, because that looks for
        # leads whose run is still *open* and this one's is closed. Nothing was
        # watching for the combination: 145 leads on the live workspace,
        # researched over eight days, every one with a completed run and no
        # draft.
        if run is not None and run.status == "running":
            run.status = request.outcome
            run.finished_at = _now()
            if request.detail:
                run.failure_reason = request.detail[:500]

        # Still conditional on RESEARCHING, which is what protects the more
        # specific verdicts: scoring writes BELOW_THRESHOLD and contact
        # resolution writes manual review, and neither should be overwritten by
        # this general one.
        lead = await session.get(Lead, uuid.UUID(request.lead_id))
        if lead is not None and lead.status is LeadStatus.RESEARCHING:
            if request.outcome in _RETRYABLE_OUTCOMES:
                lead.status = LeadStatus.DISCOVERED
            else:
                lead.status = LeadStatus.RESEARCHED
            lead.status_reason = (request.detail or request.outcome)[:200]

    logger.info(
        "research run closed",
        extra={"research_run_id": request.research_run_id, "outcome": request.outcome},
    )


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

    # Anything short of full autopilot requires a human before queueing -- and
    # so does a campaign that was never opted in, even under autopilot. The mode
    # says what the system is permitted to do; the flag says whether this
    # campaign was actually handed that permission.
    return not (mode.allows(Capability.AUTO_APPROVE) and policy.auto_approve)


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
    "close_research_run",
    "open_research_run",
    "record_workflow_event",
    "requires_human_approval",
]
