"""The daily round that answers whether our mail is being seen.

A cron workflow with one deliberate pause in the middle. It sends a probe from
each mailbox to a seed we own, sleeps while the filter makes up its mind, then
logs into the seeds and records which folder each probe is in.

**The sleep is the design, not an implementation detail.** Placement cannot be
read at send time. A message is in the inbox for a moment before it is moved,
so a round that checked immediately would report almost everything as landing
and would have reported the 26 September probes -- all six of which were in
spam -- as a clean sweep. An hour is the smallest gap that measures the filter
rather than the race.

Temporal sleeps durably, so the hour costs nothing: no worker is held, and a
deploy in the middle of the wait resumes it rather than losing the round.

**Not created paused**, unlike anything whose first unattended run reaches a
stranger. The only mail this sends goes to mailboxes we own, and the argument
in ``schedules.py`` applies with force here: a deployment that leaves
measurement switched off produces a system that looks healthy because nothing
is looking. That is precisely the state 944 unseen messages were sent in.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from titan.workflows.types import PlacementRoundInput, PlacementRoundResult

#: One SMTP round trip per mailbox, five mailboxes.
SEND_TIMEOUT = timedelta(minutes=5)

#: One IMAP login per seed, each walking a handful of folders. Generous because
#: a slow provider must not fail a round whose whole point is a daily reading.
READ_TIMEOUT = timedelta(minutes=15)

#: Retried, because both halves fail transiently -- an SMTP server refusing a
#: connection, an IMAP login rate-limited -- and both are idempotent. Sending
#: is keyed on the probe token so a retry cannot double-send, and recording a
#: folder is a write of the same value.
#:
#: ValueError is not retried: the only one either activity raises is a folder
#: outside ``FOLDERS``, which is a bug, and retrying would write it again.
ROUND_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=4,
    non_retryable_error_types=["ValueError"],
)

#: 07:20 UTC daily, and the timing is chosen against the sending day rather
#: than for tidiness. The probes go out before the day's outreach so they queue
#: behind nothing, and the read lands around 08:20 -- still before most of the
#: day's volume, so a mailbox that turned overnight is visible in the report
#: while there is a day left to act on it.
DEFAULT_CRON = "20 7 * * *"


@workflow.defn(name="PlacementRoundWorkflow")
class PlacementRoundWorkflow:
    """Send the probes, wait for the filters, record where they landed."""

    @workflow.run
    async def run(self, request: PlacementRoundInput) -> PlacementRoundResult:
        sent = await workflow.execute_activity(
            "send_placement_probes",
            request,
            start_to_close_timeout=SEND_TIMEOUT,
            retry_policy=ROUND_RETRY,
            result_type=PlacementRoundResult,
        )
        if sent.skipped:
            # Nothing to wait for. Returned rather than sleeping an hour to
            # report the same thing, so an unconfigured deployment finds out
            # in seconds instead of looking like a round in progress.
            return sent

        await workflow.sleep(timedelta(minutes=request.settle_minutes))

        read = await workflow.execute_activity(
            "read_placement_probes",
            request,
            start_to_close_timeout=READ_TIMEOUT,
            retry_policy=ROUND_RETRY,
            result_type=PlacementRoundResult,
        )
        # Both halves in one result: how many went out, and what became of
        # them. Reporting only the read would lose the case where four probes
        # were sent and one was found, which is the shape of a mailbox being
        # silently dropped rather than filed.
        return PlacementRoundResult(
            sent=sent.sent,
            recorded=read.recorded,
            folders=read.folders,
            skipped=read.skipped,
        )


def placement_round_workflow_id(workspace_id: str) -> str:
    """One schedule per workspace.

    Two would each send a full round, doubling the probe volume against every
    mailbox's daily cap while producing two readings of the same thing.
    """
    return f"placement-round::{workspace_id}"


__all__ = [
    "DEFAULT_CRON",
    "READ_TIMEOUT",
    "ROUND_RETRY",
    "SEND_TIMEOUT",
    "PlacementRoundWorkflow",
    "placement_round_workflow_id",
]
