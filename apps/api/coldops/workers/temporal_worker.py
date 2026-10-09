"""Temporal worker entrypoint.

Run as: ``python -m coldops.workers.temporal_worker``

The pre-0.2 repository defined four workflows and **registered none of them** --
`grep "Worker("` returned zero matches, so no workflow could ever execute (gap
analysis C-10). This module is the missing registration.

Task queues are separated by resource profile rather than by domain: browser
work is slow and memory-hungry, model work is latency-bound and rate-limited,
and database work is fast. Running them on one queue means a queue full of
crawls starves everything else.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from temporalio.client import Client
from temporalio.worker import Worker

from coldops.activities import claim_verification
from coldops.activities import daily_report as daily_report_activities
from coldops.activities import delivery_events as delivery_event_activities
from coldops.activities import discovery as discovery_activities
from coldops.activities import mailbox_ramp as mailbox_ramp_activities
from coldops.activities import optouts as optout_activities
from coldops.activities import orchestration as orchestration_activities
from coldops.activities import pipeline as pipeline_activities
from coldops.activities import placement as placement_activities
from coldops.activities import profile as profile_activities
from coldops.activities import readmission as readmission_activities
from coldops.activities import reporting as reporting_activities
from coldops.activities import research as research_activities
from coldops.activities import retention as retention_activities
from coldops.activities import reverification as reverification_activities
from coldops.activities import schedule_healing as schedule_healing_activities
from coldops.activities import sender_health as sender_health_activities
from coldops.activities import smartlead_replies as smartlead_reply_activities
from coldops.activities import stale_runs as stale_run_activities
from coldops.activities import stranded as stranded_activities
from coldops.activities import trickle as trickle_activities
from coldops.activities import verification as verification_activities
from coldops.activities import vitals as vitals_activities
from coldops.activities import warmup as warmup_activities
from coldops.config import get_settings
from coldops.db.session import dispose_engine
from coldops.observability.logging import configure_logging
from coldops.runtime import configure_event_loop
from coldops.workflows.daily_report import DailyReportWorkflow
from coldops.workflows.delivery_events import DeliveryEventPollWorkflow
from coldops.workflows.housekeeping import HousekeepingWorkflow
from coldops.workflows.mailbox_ramp import MailboxRampWorkflow
from coldops.workflows.optouts import PullOptOutsWorkflow
from coldops.workflows.orchestrator import CampaignOrchestratorWorkflow
from coldops.workflows.placement import PlacementRoundWorkflow
from coldops.workflows.reporting import WeeklyReportWorkflow
from coldops.workflows.research import LeadResearchWorkflow
from coldops.workflows.sender_health import SenderHealthSnapshotWorkflow
from coldops.workflows.supervisor import SupervisorWorkflow
from coldops.workflows.verification import SenderVerificationWorkflow
from coldops.workflows.warmup import WarmupRoundWorkflow

logger = logging.getLogger("coldops.workers.temporal")

from coldops.workflows.queues import MAINTENANCE_QUEUE, RESEARCH_QUEUE  # noqa: E402


async def connect() -> Client:
    settings = get_settings()
    # Pydantic data converter: workflow arguments are dataclasses, and the
    # default converter cannot round-trip them faithfully.
    from temporalio.contrib.pydantic import pydantic_data_converter

    return await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
    )


async def main() -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service="titan-temporal-worker",
        environment=settings.environment.value,
    )

    client = await connect()

    worker = Worker(
        client,
        task_queue=RESEARCH_QUEUE,
        # The orchestrator runs on the same queue as the research children it
        # starts, and passes its own task_queue down, so a deployment can never
        # end up with an orchestrator whose children have no worker to run them.
        workflows=[
            LeadResearchWorkflow,
            CampaignOrchestratorWorkflow,
            SenderHealthSnapshotWorkflow,
            WeeklyReportWorkflow,
            SenderVerificationWorkflow,
            DeliveryEventPollWorkflow,
            PullOptOutsWorkflow,
            HousekeepingWorkflow,
            MailboxRampWorkflow,
            PlacementRoundWorkflow,
            WarmupRoundWorkflow,
            SupervisorWorkflow,
            DailyReportWorkflow,
        ],
        activities=[
            research_activities.close_research_run,
            research_activities.open_research_run,
            research_activities.requires_human_approval,
            research_activities.record_workflow_event,
            *orchestration_activities.ALL_ORCHESTRATION_ACTIVITIES,
            *discovery_activities.ALL_DISCOVERY_ACTIVITIES,
            *reporting_activities.ALL_REPORTING_ACTIVITIES,
            *verification_activities.ALL_VERIFICATION_ACTIVITIES,
            *pipeline_activities.ALL_PIPELINE_ACTIVITIES,
            *profile_activities.ALL_PROFILE_ACTIVITIES,
            *stranded_activities.ALL_STRANDED_ACTIVITIES,
            *schedule_healing_activities.ALL_SCHEDULE_HEALING_ACTIVITIES,
            *daily_report_activities.ALL_DAILY_REPORT_ACTIVITIES,
            stale_run_activities.reopen_stale_research_runs,
            trickle_activities.release_held_contacts,
            *reverification_activities.ALL_REVERIFICATION_ACTIVITIES,
            *retention_activities.ALL_RETENTION_ACTIVITIES,
            *readmission_activities.ALL_READMISSION_ACTIVITIES,
            *vitals_activities.ALL_VITALS_ACTIVITIES,
            *smartlead_reply_activities.ALL_SMARTLEAD_REPLY_ACTIVITIES,
            *optout_activities.ALL_OPTOUT_ACTIVITIES,
            delivery_event_activities.poll_delivery_events,
            mailbox_ramp_activities.ramp_mailboxes,
            placement_activities.send_placement_probes,
            placement_activities.read_placement_probes,
            *warmup_activities.ALL_WARMUP_ACTIVITIES,
            sender_health_activities.capture_sender_health,
        ],
        # Bounded concurrency. An unbounded worker will happily start more
        # crawls than the browser worker can serve and then time out on all of
        # them.
        max_concurrent_activities=8,
        max_concurrent_workflow_tasks=32,
        graceful_shutdown_timeout=__import__("datetime").timedelta(seconds=30),
    )

    # A second worker, same process, for the repair passes.
    #
    # The activities are identical -- they are simply reachable on a queue the
    # crawl backlog cannot occupy. 281 research workflows started at once on 9
    # September left the hourly housekeeping pass queued behind them and it did
    # not run; the pass that repairs a saturated pipeline has to be able to run
    # while the pipeline is saturated, which is close to the only time it
    # matters.
    #
    # Concurrency of two, deliberately low. This is bounded database work and a
    # handful of address re-checks; the point is a lane that is always open,
    # not a second pool of capacity competing with the crawler for the same
    # machine.
    maintenance = Worker(
        client,
        task_queue=MAINTENANCE_QUEUE,
        activities=[
            *stranded_activities.ALL_STRANDED_ACTIVITIES,
            stale_run_activities.reopen_stale_research_runs,
            trickle_activities.release_held_contacts,
            *reverification_activities.ALL_REVERIFICATION_ACTIVITIES,
            *retention_activities.ALL_RETENTION_ACTIVITIES,
            *readmission_activities.ALL_READMISSION_ACTIVITIES,
            *vitals_activities.ALL_VITALS_ACTIVITIES,
            # Re-checks a claim against the live site before it is asserted.
            # On the maintenance queue, not research: it is a guard on the
            # pipeline rather than part of it, and it must keep running while
            # a crawl backlog saturates the research lane.
            *claim_verification.ALL_CLAIM_VERIFICATION_ACTIVITIES,
        ],
        max_concurrent_activities=2,
        graceful_shutdown_timeout=__import__("datetime").timedelta(seconds=30),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())

    logger.info(
        "temporal worker starting",
        extra={
            "task_queue": RESEARCH_QUEUE,
            "maintenance_queue": MAINTENANCE_QUEUE,
            "temporal_host": settings.temporal_host,
            "workflows": [
                "LeadResearchWorkflow",
                "CampaignOrchestratorWorkflow",
                "WeeklyReportWorkflow",
                "SenderVerificationWorkflow",
            ],
        },
    )

    try:
        async with worker, maintenance:
            await stop.wait()
    finally:
        await dispose_engine()
        logger.info("temporal worker stopped cleanly")


if __name__ == "__main__":
    configure_event_loop()
    asyncio.run(main())
