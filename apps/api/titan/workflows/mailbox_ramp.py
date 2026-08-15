"""The schedule that grows each mailbox's volume without anybody asking.

A cron workflow, like verification and the weekly report: it starts, does a
bounded amount of work, and finishes. There is no state to carry between runs --
the ceiling and the current limit are both read from the provider each time --
so an always-on loop would be carrying a timer and nothing else.

**Daily, though volume moves weekly.** The two are not the same thing. Stepping
up happens on a week boundary because receivers judge a sender on a trend and a
limit that moves daily is a trend made of noise. Stepping *down* has to be able
to happen the moment evidence turns, and evidence is only re-read when this
runs. A weekly job would leave a mailbox that started bouncing on a Tuesday at
full volume until the following Monday.

Early morning UTC and off the hour, matching verification: a mailbox whose
delivery record turned overnight is corrected before the day's sending starts
rather than halfway through it, and the ramp runs after verification so a
mailbox that lost its DNS records is already refused rather than being ramped.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from titan.workflows.types import RampMailboxesInput, RampMailboxesResult

#: One provider round trip per campaign plus one per mailbox written.
RAMP_TIMEOUT = timedelta(minutes=10)

#: Retried, because the provider's API fails transiently and the activity is
#: idempotent -- it writes a target computed from current state, so running it
#: twice lands on the same number. ValueError is not retried: a negative limit
#: is a bug upstream and retrying would just write it again.
RAMP_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=4,
    non_retryable_error_types=["ValueError"],
)

#: 06:10 UTC daily -- after verification at 05:40, so a mailbox whose domain
#: broke overnight has already been marked unsendable before the ramp looks at
#: it.
DEFAULT_CRON = "10 6 * * *"


@workflow.defn(name="MailboxRampWorkflow")
class MailboxRampWorkflow:
    """Move one workspace's mailboxes along their ramp. Scheduled by Temporal."""

    @workflow.run
    async def run(self, request: RampMailboxesInput) -> RampMailboxesResult:
        return await workflow.execute_activity(
            "ramp_mailboxes",
            request,
            start_to_close_timeout=RAMP_TIMEOUT,
            retry_policy=RAMP_RETRY,
            result_type=RampMailboxesResult,
        )


def mailbox_ramp_workflow_id(workspace_id: str) -> str:
    """One schedule per workspace.

    Two would read the same mailboxes and both write a limit. They would compute
    the same target, so the damage is bounded -- but the second write lands after
    the first and the provider's audit trail shows a change nobody made twice.
    """
    return f"mailbox-ramp::{workspace_id}"


__all__ = [
    "DEFAULT_CRON",
    "RAMP_RETRY",
    "RAMP_TIMEOUT",
    "MailboxRampWorkflow",
    "mailbox_ramp_workflow_id",
]
