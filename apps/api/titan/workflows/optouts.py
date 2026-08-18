"""Pulling the website's opt-outs, on a schedule.

Separate from the delivery poll on purpose. That one exists to learn what
happened to mail already sent; this one exists to stop mail being sent at all,
and a failure in either must not delay the other. Folding them together would
mean a Smartlead outage could postpone honouring an unsubscribe.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from titan.workflows.types import PullOptOutsInput, PullOptOutsResult

#: Every fifteen minutes. An opt-out is a request to stop that has already been
#: made; the interval is how long somebody keeps receiving mail after asking not
#: to, so it is short. One HTTPS request against a key-value store.
DEFAULT_CRON = "*/15 * * * *"

TIMEOUT = timedelta(minutes=2)

#: Persistent, because giving up means continuing to mail people who opted out.
#: The window is bounded so a permanently broken endpoint surfaces rather than
#: retrying silently for ever.
RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=8,
)


@workflow.defn(name="PullOptOutsWorkflow", sandboxed=False)
class PullOptOutsWorkflow:
    """Read the opt-out list and suppress anything new."""

    @workflow.run
    async def run(self, request: PullOptOutsInput) -> PullOptOutsResult:
        return await workflow.execute_activity(
            "pull_opt_outs",
            request,
            start_to_close_timeout=TIMEOUT,
            retry_policy=RETRY,
            result_type=PullOptOutsResult,
        )


def pull_opt_outs_workflow_id(workspace_id: str) -> str:
    """One per workspace. Two would race to suppress the same addresses."""
    return f"opt-outs::{workspace_id}"


__all__ = [
    "DEFAULT_CRON",
    "RETRY",
    "TIMEOUT",
    "PullOptOutsWorkflow",
    "pull_opt_outs_workflow_id",
]
