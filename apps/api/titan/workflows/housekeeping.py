"""The sweeps that put work back on the rails, on a schedule.

Both sweeps existed before this workflow and neither ran. ``sweep_stranded_drafts``
was reachable only from ``titan.cli`` -- a command somebody had to remember to
type -- and the research-run sweep is new. A sweeper that runs when a human
thinks to run it is a diagnostic, not a repair: the 225 stranded drafts and the
873 abandoned research runs both accumulated while the code that fixes them sat
in the repository.

**Two sweeps, one schedule, run in sequence.** They repair different stages of
the same pipeline and neither is urgent to the minute, so a single hourly pass
keeps the schedule list honest about how many independent things are actually
running. Sequential rather than parallel because reopening a stale research run
can eventually produce a draft, and a sweep that races its own downstream is
harder to reason about than one that does not.

**Failure of one must not cancel the other.** They are separate activities with
separate retries; a database problem that stops the draft sweep should not also
leave leads stranded in ``RESEARCHING``.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from titan.workflows.types import (
        ReopenStaleRunsInput,
        ReopenStaleRunsResult,
        SweepStrandedInput,
        SweepStrandedResult,
    )

#: Hourly. Both backlogs build over days, so the interval is set by how long a
#: lead should wait once it is already stuck rather than by any rate of arrival.
#: Cheap enough at this frequency: two bounded queries when there is nothing to
#: do, which is the normal case once the backlogs have drained.
DEFAULT_CRON = "17 * * * *"

TIMEOUT = timedelta(minutes=10)

#: Bounded rather than persistent. These sweeps repair a backlog that is not
#: getting worse while they retry, so a broken pass should surface at the next
#: hour rather than hammer a database that is already unwell.
RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=15),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=4,
)


@workflow.defn(name="HousekeepingWorkflow", sandboxed=False)
class HousekeepingWorkflow:
    """Reopen abandoned research runs, then queue stranded drafts."""

    @workflow.run
    async def run(self, request: SweepStrandedInput) -> SweepStrandedResult:
        # Runs first: it returns leads to the front of the pipeline, and doing
        # it before the draft sweep means anything it eventually produces is
        # picked up by the next pass rather than half-processed by this one.
        stale: ReopenStaleRunsResult = await workflow.execute_activity(
            "reopen_stale_research_runs",
            ReopenStaleRunsInput(workspace_id=request.workspace_id),
            start_to_close_timeout=TIMEOUT,
            retry_policy=RETRY,
            result_type=ReopenStaleRunsResult,
        )
        workflow.logger.info(
            "reopened %s abandoned research runs (oldest %sh)",
            stale.reopened,
            stale.oldest_age_hours,
        )

        return await workflow.execute_activity(
            "sweep_stranded_drafts",
            request,
            start_to_close_timeout=TIMEOUT,
            retry_policy=RETRY,
            result_type=SweepStrandedResult,
        )


def housekeeping_workflow_id(workspace_id: str) -> str:
    """One per workspace. Two would hand the same draft to the outbox twice --
    collapsed by the idempotency key, but only after both had done the work."""
    return f"housekeeping::{workspace_id}"


__all__ = [
    "DEFAULT_CRON",
    "RETRY",
    "TIMEOUT",
    "HousekeepingWorkflow",
    "housekeeping_workflow_id",
]
