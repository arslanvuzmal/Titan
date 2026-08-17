"""The schedule that keeps delivery evidence flowing in.

A cron workflow with no state between runs, for the same reason the ramp has
none: the fingerprints in the event table already record what has been seen, and
a cursor carried in workflow state would be a second answer to that question,
free to disagree with the first after any failure.

**Hourly.** More often than anything that consumes it needs, and deliberately
so. Two of the consequences are time-critical in one direction only: a reply
must stop the next follow-up, and a bounce must reach the suppression list
before the address is sent to again. Being an hour stale costs nothing; being a
day stale means a follow-up went out after someone answered.

Off the hour, because everything else here is, and a scheduler that fires six
jobs at :00 makes the provider's rate limit a shared resource none of them
account for.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from titan.workflows.types import (
        CollectRepliesInput,
        CollectRepliesResult,
        PollDeliveryEventsInput,
        PollDeliveryEventsResult,
    )

#: Generous: a campaign with a long history walks many pages, and each page is a
#: provider round trip.
POLL_TIMEOUT = timedelta(minutes=15)

#: Retried, because the activity is idempotent by construction -- every write is
#: an insert guarded by a unique fingerprint, so a retry after a partial run
#: re-reads what it already stored and writes nothing.
POLL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=4,
    non_retryable_error_types=["ValueError"],
)

#: Every hour at :25.
DEFAULT_CRON = "25 * * * *"


@workflow.defn(name="DeliveryEventPollWorkflow")
class DeliveryEventPollWorkflow:
    """Pull one workspace's delivery outcomes. Scheduled by Temporal."""

    @workflow.run
    async def run(self, request: PollDeliveryEventsInput) -> PollDeliveryEventsResult:
        result: PollDeliveryEventsResult = await workflow.execute_activity(
            "poll_delivery_events",
            request,
            start_to_close_timeout=POLL_TIMEOUT,
            retry_policy=POLL_RETRY,
            result_type=PollDeliveryEventsResult,
        )

        # Collecting the replies belongs here rather than on a schedule of its
        # own. It reads the same statistics rows this pass just read, so a
        # separate schedule would double the request count for the same answer,
        # and the two could drift apart -- one seeing a reply the other has not
        # reached yet, on a system where "a reply arrived" and "what the reply
        # said" drive different decisions.
        #
        # Failure is deliberately not propagated. The delivery poll above has
        # already succeeded and written suppressions and bounce consequences; a
        # Smartlead outage while fetching message bodies must not roll that back
        # or mark the run failed. The replies are still there next hour.
        try:
            await workflow.execute_activity(
                "collect_smartlead_replies",
                CollectRepliesInput(workspace_id=request.workspace_id),
                start_to_close_timeout=POLL_TIMEOUT,
                retry_policy=POLL_RETRY,
                result_type=CollectRepliesResult,
            )
        except Exception:
            workflow.logger.warning("reply collection failed; delivery poll stands")

        return result


def delivery_event_poll_workflow_id(workspace_id: str) -> str:
    """One schedule per workspace.

    Two would race on the same fingerprints. The unique constraint makes that
    safe rather than corrupting, but the loser spends a full run doing nothing,
    and the consequences -- suppression, reply recording -- would be attempted
    twice for no gain.
    """
    return f"delivery-events::{workspace_id}"


__all__ = [
    "DEFAULT_CRON",
    "POLL_RETRY",
    "POLL_TIMEOUT",
    "DeliveryEventPollWorkflow",
    "delivery_event_poll_workflow_id",
]
