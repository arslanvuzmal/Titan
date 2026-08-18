"""Turning the crank.

Every workflow in this package was written to be scheduled. The weekly report
carries a cron expression and a workflow-id convention. Sender verification
carries both as well, and a docstring explaining why there must be exactly one
per workspace. The orchestrator carries an interval, a continue-as-new budget,
and a ``paused_reason`` that survives roll-over specifically so a human's pause
is not undone by the workflow staying alive.

**None of it was ever started.** The worker registers four workflows so they
*can* execute; nothing ever asks one to. Discover, research, draft and send all
connect, and measure connects to optimise -- the campaign manager runs inside
every planning cycle -- but the first link is a person typing a command. That is
what makes the loop an arc rather than a circle, and this module is the join.

Four properties matter more than the wiring:

**Re-installing must never restart what a human stopped.** The most likely time
to run this is a deploy, and the second most likely is during an incident, when
somebody has just paused a schedule. An installer that resets ``paused`` would
resurrect it. So installing updates the *spec* and never the *state*: a paused
schedule stays paused, and the outcome says so out loud rather than reporting
success.

**Missed occurrences are dropped, not caught up.** Temporal will happily backfill
every occurrence a stopped worker missed. Three missed weekly reports become
three reports; a weekend of missed verifications becomes a burst of DNS traffic
that looks like a scanner. The catch-up window is minutes, so a worker that was
down through an occurrence simply skips it -- a missed report is a nuisance, and
a thundering herd on restart is an outage.

**Overlap is skipped, never buffered.** Two verification runs on one workspace
write the same timestamps twice, which is harmless, and double the DNS traffic,
which is not. Buffering would queue the duplicate rather than dropping it, so
the herd arrives late instead of never.

**A campaign is started once and only while it is active.** The orchestrator is
an always-on workflow, not a schedule: starting a second one for the same
campaign would double its volume, which is why
``orchestrator_workflow_id`` exists. Starting one for a paused campaign
would override a human decision with a deploy.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from titan.db.enums import CampaignStatus
from titan.workflows.delivery_events import DEFAULT_CRON as POLL_CRON
from titan.workflows.delivery_events import delivery_event_poll_workflow_id
from titan.workflows.housekeeping import DEFAULT_CRON as HOUSEKEEPING_CRON
from titan.workflows.housekeeping import housekeeping_workflow_id
from titan.workflows.mailbox_ramp import DEFAULT_CRON as RAMP_CRON
from titan.workflows.mailbox_ramp import mailbox_ramp_workflow_id
from titan.workflows.optouts import DEFAULT_CRON as OPTOUT_CRON
from titan.workflows.optouts import pull_opt_outs_workflow_id
from titan.workflows.orchestrator import orchestrator_workflow_id
from titan.workflows.reporting import DEFAULT_CRON as REPORT_CRON
from titan.workflows.reporting import weekly_report_workflow_id
from titan.workflows.sender_health import DEFAULT_CRON as HEALTH_CRON
from titan.workflows.sender_health import sender_health_workflow_id
from titan.workflows.types import (
    CampaignOrchestratorInput,
    CaptureSenderHealthInput,
    PollDeliveryEventsInput,
    PullOptOutsInput,
    RampMailboxesInput,
    SweepStrandedInput,
    VerifySendersInput,
    WeeklyReportInput,
)
from titan.workflows.verification import DEFAULT_CRON as VERIFY_CRON
from titan.workflows.verification import sender_verification_workflow_id

logger = logging.getLogger(__name__)

#: How far back Temporal may reach to run an occurrence it missed. Deliberately
#: shorter than the shortest interval here, so a missed run is skipped rather
#: than fired late alongside the next one. See the module docstring.
CATCHUP_WINDOW = dt.timedelta(minutes=30)


class Outcome(StrEnum):
    """What installing one job actually did.

    ``LEFT_PAUSED`` is separate from ``UPDATED`` on purpose. Both mean the
    schedule now carries the right spec, but only one of them will ever fire,
    and an installer that reported them identically would let a paused schedule
    sit unnoticed behind a wall of green.
    """

    CREATED = "created"
    UPDATED = "updated"
    LEFT_PAUSED = "left_paused"
    ALREADY_RUNNING = "already_running"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """One recurring job: what runs, how often, and whether it starts stopped."""

    schedule_id: str
    workflow: str
    workflow_id: str
    cron: str
    arg: Any
    task_queue: str
    #: Created paused. For anything whose first unattended run has an effect
    #: outside the database, so installing the schedules is never itself the
    #: act that starts the work.
    starts_paused: bool = False
    note: str = ""


@dataclass(frozen=True, slots=True)
class OrchestratorStart:
    """One always-on campaign loop to start, if it is not already running."""

    workflow_id: str
    campaign_id: uuid.UUID
    arg: CampaignOrchestratorInput
    task_queue: str


@dataclass(frozen=True, slots=True)
class Applied:
    """What happened to one job, for an operator reading the output."""

    target: str
    outcome: Outcome
    detail: str = ""

    @property
    def will_run(self) -> bool:
        return self.outcome in (Outcome.CREATED, Outcome.UPDATED, Outcome.ALREADY_RUNNING)


# ==========================================================================
# Planning -- pure, and the part worth testing
# ==========================================================================
def plan_schedules(workspace_id: uuid.UUID, *, task_queue: str) -> list[ScheduledJob]:
    """The recurring jobs one workspace needs.

    Both read and write only Titan's own database and DNS. Neither sends
    anything: the report is a report, and verification re-checks records that
    already exist. So neither starts paused -- a deploy that leaves measurement
    switched off produces a system that looks healthy because nothing is
    looking.
    """
    ws = str(workspace_id)
    return [
        ScheduledJob(
            schedule_id=f"titan-weekly-report::{ws}",
            workflow="WeeklyReportWorkflow",
            workflow_id=weekly_report_workflow_id(ws),
            cron=REPORT_CRON,
            arg=WeeklyReportInput(workspace_id=ws),
            task_queue=task_queue,
            note="measures the week and writes the report",
        ),
        ScheduledJob(
            schedule_id=f"titan-mailbox-ramp::{ws}",
            workflow="MailboxRampWorkflow",
            workflow_id=mailbox_ramp_workflow_id(ws),
            cron=RAMP_CRON,
            arg=RampMailboxesInput(workspace_id=ws),
            task_queue=task_queue,
            note="grows each mailbox's daily volume as it earns it",
        ),
        ScheduledJob(
            schedule_id=f"titan-housekeeping::{ws}",
            workflow="HousekeepingWorkflow",
            workflow_id=housekeeping_workflow_id(ws),
            cron=HOUSEKEEPING_CRON,
            arg=SweepStrandedInput(workspace_id=ws),
            task_queue=task_queue,
            note="puts stranded drafts and abandoned research runs back on the rails",
        ),
        ScheduledJob(
            schedule_id=f"titan-opt-outs::{ws}",
            workflow="PullOptOutsWorkflow",
            workflow_id=pull_opt_outs_workflow_id(ws),
            cron=OPTOUT_CRON,
            arg=PullOptOutsInput(workspace_id=ws),
            task_queue=task_queue,
            note="honours unsubscribes the website collected",
        ),
        ScheduledJob(
            schedule_id=f"titan-delivery-events::{ws}",
            workflow="DeliveryEventPollWorkflow",
            workflow_id=delivery_event_poll_workflow_id(ws),
            cron=POLL_CRON,
            arg=PollDeliveryEventsInput(workspace_id=ws),
            task_queue=task_queue,
            note="pulls what happened to every send, so the rest can learn",
        ),
        ScheduledJob(
            schedule_id=f"titan-sender-health::{ws}",
            workflow="SenderHealthSnapshotWorkflow",
            workflow_id=sender_health_workflow_id(ws),
            cron=HEALTH_CRON,
            arg=CaptureSenderHealthInput(workspace_id=ws),
            task_queue=task_queue,
            note="records each sender's health so a trend exists to respond to",
        ),
        ScheduledJob(
            schedule_id=f"titan-sender-verification::{ws}",
            workflow="SenderVerificationWorkflow",
            workflow_id=sender_verification_workflow_id(ws),
            cron=VERIFY_CRON,
            arg=VerifySendersInput(workspace_id=ws),
            task_queue=task_queue,
            note="re-checks SPF, DKIM and DMARC before the claim goes stale",
        ),
    ]


def plan_orchestrators(
    workspace_id: uuid.UUID,
    campaigns: list[tuple[uuid.UUID, CampaignStatus]],
    *,
    task_queue: str,
    interval_minutes: int = 60,
    max_new_research: int = 25,
) -> list[OrchestratorStart]:
    """One always-on loop per *active* campaign.

    Draft, paused, completed and archived campaigns are omitted rather than
    started-and-immediately-stopped. A paused campaign is a human decision, and
    the orchestrator carries ``paused_reason`` across continue-as-new precisely
    so that decision survives; starting a fresh workflow would discard it by
    creating a new one that had never been told.
    """
    return [
        OrchestratorStart(
            workflow_id=orchestrator_workflow_id(str(workspace_id), str(campaign_id)),
            campaign_id=campaign_id,
            arg=CampaignOrchestratorInput(
                workspace_id=str(workspace_id),
                campaign_id=str(campaign_id),
                interval_minutes=interval_minutes,
                max_new_research=max_new_research,
            ),
            task_queue=task_queue,
        )
        for campaign_id, status in campaigns
        if status is CampaignStatus.ACTIVE
    ]


def summarise(applied: list[Applied]) -> str:
    """One line per job, then a count of what will actually run.

    The count is of jobs that *will run*, not jobs that were processed. Those
    two numbers differ exactly when something is paused, which is the case an
    operator most needs to notice.
    """
    if not applied:
        return "nothing to schedule"
    lines = [
        f"  {a.outcome.value:>16}  {a.target}" + (f"  ({a.detail})" if a.detail else "")
        for a in applied
    ]
    live = sum(1 for a in applied if a.will_run)
    failed = sum(1 for a in applied if a.outcome is Outcome.FAILED)
    tail = f"{live} of {len(applied)} will run"
    if failed:
        tail += f"; {failed} failed"
    return "\n".join(lines) + f"\n{tail}"


# ==========================================================================
# Applying -- the part that talks to Temporal
# ==========================================================================
async def install(client: Any, jobs: list[ScheduledJob]) -> list[Applied]:
    """Create each schedule, or update the spec of one that already exists.

    Never touches ``paused``. See the module docstring: the most likely moment
    to run this is a deploy, and the second most likely is an incident during
    which somebody has just pressed pause.
    """
    from temporalio.client import ScheduleAlreadyRunningError

    results: list[Applied] = []
    for job in jobs:
        try:
            await client.create_schedule(job.schedule_id, _schedule(job))
            results.append(Applied(job.schedule_id, Outcome.CREATED, job.note))
        except ScheduleAlreadyRunningError:
            results.append(await _update_existing(client, job))
        except Exception as exc:  # pragma: no cover - network shape varies
            logger.warning(
                "could not install schedule",
                extra={"schedule_id": job.schedule_id},
                exc_info=True,
            )
            results.append(Applied(job.schedule_id, Outcome.FAILED, str(exc)))
    return results


async def _update_existing(client: Any, job: ScheduledJob) -> Applied:
    """Bring an existing schedule's spec up to date, leaving its state alone."""
    from temporalio.client import ScheduleUpdate

    handle = client.get_schedule_handle(job.schedule_id)
    description = await handle.describe()
    was_paused = description.schedule.state.paused

    def _mutate(update: Any) -> ScheduleUpdate:
        schedule = _schedule(job)
        # Carry the live state across verbatim rather than rebuilding it. Only
        # the spec and the action are ours to change.
        schedule.state = update.description.schedule.state
        return ScheduleUpdate(schedule=schedule)

    await handle.update(_mutate)
    if was_paused:
        return Applied(
            job.schedule_id,
            Outcome.LEFT_PAUSED,
            description.schedule.state.note or "paused by hand; not resumed",
        )
    return Applied(job.schedule_id, Outcome.UPDATED, job.note)


async def start_orchestrators(
    client: Any, starts: list[OrchestratorStart]
) -> list[Applied]:
    """Start one always-on loop per campaign, treating "already running" as success.

    A second loop on the same campaign would double its volume, so the collision
    is the mechanism working rather than an error to report.
    """
    from temporalio.exceptions import WorkflowAlreadyStartedError

    results: list[Applied] = []
    for start in starts:
        try:
            await client.start_workflow(
                "CampaignOrchestratorWorkflow",
                start.arg,
                id=start.workflow_id,
                task_queue=start.task_queue,
            )
            results.append(Applied(start.workflow_id, Outcome.CREATED))
        except WorkflowAlreadyStartedError:
            results.append(
                Applied(start.workflow_id, Outcome.ALREADY_RUNNING, "one loop is enough")
            )
        except Exception as exc:  # pragma: no cover - network shape varies
            logger.warning(
                "could not start campaign orchestrator",
                extra={"workflow_id": start.workflow_id},
                exc_info=True,
            )
            results.append(Applied(start.workflow_id, Outcome.FAILED, str(exc)))
    return results


def _schedule(job: ScheduledJob) -> Any:
    """The Temporal schedule for one job.

    ``SKIP`` rather than a buffering policy: a duplicate occurrence should be
    dropped, not queued to arrive late. Buffering turns "the worker was busy"
    into "the worker will now do all of it at once", which is the failure this
    module exists to avoid.
    """
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec,
        ScheduleState,
    )

    return Schedule(
        action=ScheduleActionStartWorkflow(
            job.workflow,
            job.arg,
            id=job.workflow_id,
            task_queue=job.task_queue,
        ),
        spec=ScheduleSpec(cron_expressions=[job.cron]),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            catchup_window=CATCHUP_WINDOW,
        ),
        state=ScheduleState(
            note=job.note,
            paused=job.starts_paused,
        ),
    )


__all__ = [
    "CATCHUP_WINDOW",
    "Applied",
    "OrchestratorStart",
    "Outcome",
    "ScheduledJob",
    "install",
    "plan_orchestrators",
    "plan_schedules",
    "start_orchestrators",
    "summarise",
]
