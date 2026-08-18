"""Temporal worker entrypoint.

Run as: ``python -m titan.workers.temporal_worker``

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

from titan.activities import delivery_events as delivery_event_activities
from titan.activities import discovery as discovery_activities
from titan.activities import mailbox_ramp as mailbox_ramp_activities
from titan.activities import optouts as optout_activities
from titan.activities import orchestration as orchestration_activities
from titan.activities import pipeline as pipeline_activities
from titan.activities import reporting as reporting_activities
from titan.activities import research as research_activities
from titan.activities import sender_health as sender_health_activities
from titan.activities import smartlead_replies as smartlead_reply_activities
from titan.activities import stranded as stranded_activities
from titan.activities import verification as verification_activities
from titan.config import get_settings
from titan.db.session import dispose_engine
from titan.observability.logging import configure_logging
from titan.runtime import configure_event_loop
from titan.workflows.delivery_events import DeliveryEventPollWorkflow
from titan.workflows.mailbox_ramp import MailboxRampWorkflow
from titan.workflows.optouts import PullOptOutsWorkflow
from titan.workflows.orchestrator import CampaignOrchestratorWorkflow
from titan.workflows.reporting import WeeklyReportWorkflow
from titan.workflows.research import LeadResearchWorkflow
from titan.workflows.sender_health import SenderHealthSnapshotWorkflow
from titan.workflows.verification import SenderVerificationWorkflow

logger = logging.getLogger("titan.workers.temporal")

RESEARCH_QUEUE = "titan-research"


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
            MailboxRampWorkflow,
        ],
        activities=[
            research_activities.open_research_run,
            research_activities.requires_human_approval,
            research_activities.record_workflow_event,
            *orchestration_activities.ALL_ORCHESTRATION_ACTIVITIES,
            *discovery_activities.ALL_DISCOVERY_ACTIVITIES,
            *reporting_activities.ALL_REPORTING_ACTIVITIES,
            *verification_activities.ALL_VERIFICATION_ACTIVITIES,
            *pipeline_activities.ALL_PIPELINE_ACTIVITIES,
            *stranded_activities.ALL_STRANDED_ACTIVITIES,
            *smartlead_reply_activities.ALL_SMARTLEAD_REPLY_ACTIVITIES,
            *optout_activities.ALL_OPTOUT_ACTIVITIES,
            delivery_event_activities.poll_delivery_events,
            mailbox_ramp_activities.ramp_mailboxes,
            sender_health_activities.capture_sender_health,
        ],
        # Bounded concurrency. An unbounded worker will happily start more
        # crawls than the browser worker can serve and then time out on all of
        # them.
        max_concurrent_activities=8,
        max_concurrent_workflow_tasks=32,
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
        async with worker:
            await stop.wait()
    finally:
        await dispose_engine()
        logger.info("temporal worker stopped cleanly")


if __name__ == "__main__":
    configure_event_loop()
    asyncio.run(main())
