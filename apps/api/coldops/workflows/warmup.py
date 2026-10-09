"""The daily warm-up round.

One activity, on a schedule. Not created paused, for the same reason the
placement round is not: the only mail it sends goes to mailboxes the operator
holds credentials for, and the activity refuses outright unless warm-up is
switched on and has partners on another provider.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from coldops.workflows.types import WarmupRoundInput, WarmupRoundResult

#: SMTP for every planned message, then an IMAP pass over every participant.
ROUND_TIMEOUT = timedelta(minutes=30)

#: Sending is keyed on a deterministic Message-ID and skips anything already
#: delivered, so a retry cannot double a day's traffic. A pool conflict is a
#: configuration fault that a retry would only repeat.
ROUND_RETRY = RetryPolicy(
    initial_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=10),
    maximum_attempts=3,
    non_retryable_error_types=["PoolConflict", "NotAParticipant"],
)

#: 09:10 UTC daily. After the placement round has been read (~08:20), so a
#: morning's readings are never taken in the middle of warm-up, and early
#: enough in the European day that replies arrive in working hours.
DEFAULT_CRON = "10 9 * * *"


@workflow.defn(name="WarmupRoundWorkflow")
class WarmupRoundWorkflow:
    """Send today's warm-up and tend what arrived."""

    @workflow.run
    async def run(self, request: WarmupRoundInput) -> WarmupRoundResult:
        return await workflow.execute_activity(
            "run_warmup_round",
            request,
            start_to_close_timeout=ROUND_TIMEOUT,
            retry_policy=ROUND_RETRY,
            result_type=WarmupRoundResult,
        )


def warmup_round_workflow_id(workspace_id: str) -> str:
    """One per workspace: two would send the day's traffic twice."""
    return f"warmup-round::{workspace_id}"


__all__ = [
    "DEFAULT_CRON",
    "ROUND_RETRY",
    "ROUND_TIMEOUT",
    "WarmupRoundWorkflow",
    "warmup_round_workflow_id",
]
