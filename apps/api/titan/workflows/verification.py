"""The schedule that keeps the send gate open.

A cron workflow, for the same reason the weekly report is one: this starts,
does a bounded amount of work, and finishes. An always-on loop would have to
carry its own timer, its own continue-as-new, and its own paused state to
achieve what a cron expression states in five characters.

**Daily, not fortnightly.** ``MAX_VERIFICATION_AGE`` is fourteen days, and
running the renewal at the same interval as the expiry would mean a single
missed occurrence stops every campaign. Running daily means thirteen
consecutive failures are needed before anything stops sending, and each of
those failures is visible in Temporal long before the fourteenth.

Early morning UTC on purpose: a domain that broke overnight is caught before
the day's sending starts rather than halfway through it.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from titan.workflows.types import VerifySendersInput, VerifySendersResult

#: Generous: the activity makes a DNS lookup per distinct domain, and a
#: resolver that is timing out takes the full timeout on each.
VERIFY_TIMEOUT = timedelta(minutes=10)

#: Retried, because DNS is exactly the kind of thing that fails for a minute
#: and then works. What must not happen is a transient resolver failure being
#: written down as "domain does not publish SPF" -- check_domain_auth already
#: refuses to draw that conclusion, so a retry here is safe.
VERIFY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=4,
    non_retryable_error_types=["ValueError"],
)

#: 05:40 UTC daily. Off the hour because every scheduler in the world fires on
#: the hour, and a DNS provider under a thundering herd is slower than one that
#: is not.
DEFAULT_CRON = "40 5 * * *"


@workflow.defn(name="SenderVerificationWorkflow")
class SenderVerificationWorkflow:
    """Re-verify one workspace's sending domains. Scheduled by Temporal."""

    @workflow.run
    async def run(self, request: VerifySendersInput) -> VerifySendersResult:
        return await workflow.execute_activity(
            "verify_sender_identities",
            request,
            start_to_close_timeout=VERIFY_TIMEOUT,
            retry_policy=VERIFY_RETRY,
            result_type=VerifySendersResult,
        )


def sender_verification_workflow_id(workspace_id: str) -> str:
    """One schedule per workspace.

    Two would race on the same rows and write the same timestamps twice, which
    is harmless, and would double the DNS traffic, which is not the sort of
    thing to discover from a rate limit.
    """
    return f"sender-verification::{workspace_id}"


__all__ = [
    "DEFAULT_CRON",
    "VERIFY_RETRY",
    "VERIFY_TIMEOUT",
    "SenderVerificationWorkflow",
    "sender_verification_workflow_id",
]
