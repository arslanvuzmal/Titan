"""The weekly report workflow.

Small on purpose: one activity, one result. The scheduling is Temporal's, set as
a cron expression when the workflow is started, rather than a loop of its own.

**Why cron and not another always-on loop.** The orchestrator has to be a
long-running workflow because it holds state between cycles -- counters, pause,
the children it started. A report holds nothing: each run reads the last seven
days from the database and finishes. Running it on a cron schedule means each
week is its own execution with its own history, its own retries and its own
visible success or failure, and there is no rollover to get right.

A cron workflow that fails does not stop the schedule; the next occurrence still
fires. That is the behaviour wanted here -- a database hiccup on Monday must not
end weekly reporting until somebody notices.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from titan.workflows.types import WeeklyReportInput, WeeklyReportResult

REPORT_TIMEOUT = timedelta(minutes=10)

#: Several attempts, widely spaced. The report is not urgent to the minute, and
#: the usual cause of failure is the database being briefly unavailable.
REPORT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=10),
    maximum_attempts=5,
    non_retryable_error_types=["ValueError"],
)

#: Monday 08:00 UTC. A report that arrives on Sunday night is read on Monday
#: anyway, and one that arrives mid-week competes with the work it describes.
DEFAULT_CRON = "0 8 * * 1"


@workflow.defn(name="WeeklyReportWorkflow")
class WeeklyReportWorkflow:
    """Produce one weekly report. Scheduled by Temporal, not by a loop here."""

    @workflow.run
    async def run(self, request: WeeklyReportInput) -> WeeklyReportResult:
        return await workflow.execute_activity(
            "generate_weekly_report",
            request,
            start_to_close_timeout=REPORT_TIMEOUT,
            retry_policy=REPORT_RETRY,
            result_type=WeeklyReportResult,
        )


def weekly_report_workflow_id(workspace_id: str) -> str:
    """One schedule per workspace.

    Two would deliver the same week twice -- the notification dedupe key would
    collapse them into one row, but the second execution would still be a
    schedule nobody meant to create and nobody would think to stop.
    """
    return f"weekly-report::{workspace_id}"


__all__ = [
    "DEFAULT_CRON",
    "REPORT_RETRY",
    "REPORT_TIMEOUT",
    "WeeklyReportWorkflow",
    "weekly_report_workflow_id",
]
