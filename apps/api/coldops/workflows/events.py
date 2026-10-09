"""The schedule that keeps the event stream current.

Every fifteen minutes, on the maintenance queue: it is bounded database work
that has to keep running while a crawl backlog fills the research lane, which
is when the stream is most worth reading. Each run re-reads a three-day window,
so a missed run -- or a weekend with the worker down -- costs nothing but
latency.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from coldops.workflows.queues import MAINTENANCE_QUEUE
    from coldops.workflows.types import ProjectEventsInput, ProjectEventsResult

#: Fourteen INSERT ... SELECTs over a three-day window. Minutes is generous.
PROJECT_TIMEOUT = timedelta(minutes=10)

#: Safe to retry: every insert is ON CONFLICT DO NOTHING on the source row.
PROJECT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
)

DEFAULT_CRON = "*/15 * * * *"


@workflow.defn(name="EventProjectionWorkflow")
class EventProjectionWorkflow:
    """Project the last few days of outcomes into ``events``. Scheduled by Temporal."""

    @workflow.run
    async def run(self, request: ProjectEventsInput) -> ProjectEventsResult:
        return await workflow.execute_activity(
            "project_events",
            request,
            task_queue=MAINTENANCE_QUEUE,
            start_to_close_timeout=PROJECT_TIMEOUT,
            retry_policy=PROJECT_RETRY,
            result_type=ProjectEventsResult,
        )


def event_projection_workflow_id(workspace_id: str) -> str:
    """One per workspace; two would only race to insert the same rows."""
    return f"event-projection::{workspace_id}"


__all__ = [
    "DEFAULT_CRON",
    "EventProjectionWorkflow",
    "event_projection_workflow_id",
]
