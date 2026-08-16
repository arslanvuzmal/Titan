"""The schedule that gives each mailbox a history instead of only a present.

A cron workflow with no state between runs, like verification and the ramp: the
day's row is keyed on the date, so what has already been captured is a fact in
the database rather than something this has to remember.

**05:50 UTC, and the ten minutes on either side are the whole design.**
Verification runs at 05:40 and refreshes the SPF, DKIM and DMARC flags this
records; the mailbox ramp runs at 06:10 and reads health to decide volume.
Capturing between them means the ramp acts on a snapshot taken after today's DNS
check rather than on yesterday's, and that a mailbox which lost its records
overnight is already marked before anything decides how much it may send.

Daily rather than hourly. A trend is made of days -- a reputation window is
thirty of them -- and twenty-four points a day would be twenty-four samples of
the same number, making the history longer without making it say more.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from titan.workflows.types import (
        CaptureSenderHealthInput,
        CaptureSenderHealthResult,
    )

#: One query per sender plus one upsert. Generous for a workspace with many.
CAPTURE_TIMEOUT = timedelta(minutes=10)

#: Retried, because the write is an upsert keyed on the day: running it twice
#: refreshes the same row rather than appending a second point, so a retry
#: cannot manufacture a trend out of duplicates.
CAPTURE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=4,
    non_retryable_error_types=["ValueError"],
)

#: 05:50 UTC daily -- after verification at 05:40, before the ramp at 06:10.
DEFAULT_CRON = "50 5 * * *"


@workflow.defn(name="SenderHealthSnapshotWorkflow")
class SenderHealthSnapshotWorkflow:
    """Record today's health for one workspace's senders. Scheduled by Temporal."""

    @workflow.run
    async def run(self, request: CaptureSenderHealthInput) -> CaptureSenderHealthResult:
        return await workflow.execute_activity(
            "capture_sender_health",
            request,
            start_to_close_timeout=CAPTURE_TIMEOUT,
            retry_policy=CAPTURE_RETRY,
            result_type=CaptureSenderHealthResult,
        )


def sender_health_workflow_id(workspace_id: str) -> str:
    """One schedule per workspace.

    Two would race on the same day's row. The upsert makes that safe rather than
    corrupting, but the loser would spend a run recomputing what the winner just
    wrote.
    """
    return f"sender-health::{workspace_id}"


__all__ = [
    "CAPTURE_RETRY",
    "CAPTURE_TIMEOUT",
    "DEFAULT_CRON",
    "SenderHealthSnapshotWorkflow",
    "sender_health_workflow_id",
]
