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
        CheckVitalsInput,
        CheckVitalsResult,
        ReleaseHeldInput,
        ReleaseHeldResult,
        ReopenStaleRunsInput,
        ReopenStaleRunsResult,
        ReverifyContactsInput,
        ReverifyContactsResult,
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
    """Reopen abandoned research runs, queue stranded drafts, take the pulse."""

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

        swept: SweepStrandedResult = await workflow.execute_activity(
            "sweep_stranded_drafts",
            request,
            start_to_close_timeout=TIMEOUT,
            retry_policy=RETRY,
            result_type=SweepStrandedResult,
        )

        # The higher-risk addresses, a few a day. After the sweep because the
        # sweep is what turns an approved draft into a queued message: releasing
        # first would put today's allowance behind a queue that has not been
        # built yet, and it would be tomorrow before any of it moved.
        #
        # Failure here is swallowed. This is a volume optimisation on top of a
        # working pipeline, and a housekeeping pass that repaired stranded
        # drafts must not be recorded as failed because a throttle could not
        # take its turn.
        try:
            release: ReleaseHeldResult = await workflow.execute_activity(
                "release_held_contacts",
                ReleaseHeldInput(workspace_id=request.workspace_id),
                start_to_close_timeout=TIMEOUT,
                retry_policy=RETRY,
                result_type=ReleaseHeldResult,
            )
            workflow.logger.info(
                "released %s held contacts, %s still held (%s)",
                release.released,
                release.held,
                release.reason,
            )
        except Exception as error:
            workflow.logger.warning("held-contact release skipped: %s", error)

        # The only guard that keeps the other guards' answers true, and until
        # now the only one with no schedule at all. Placed after the release
        # rather than before it on purpose: the release decides what may go out
        # today, and a re-check that withdrew an address *after* that decision
        # would leave a message queued to an address just found wanting. The
        # send gate would still refuse it -- it reads the channel live -- but a
        # queue full of messages that can never send is a queue nobody can read.
        #
        # Swallowed like the release, and for the same reason: this is a guard
        # on top of a working pipeline, and a pass that repaired stranded
        # drafts must not be recorded as failed because a mail server was slow.
        try:
            rechecked: ReverifyContactsResult = await workflow.execute_activity(
                "reverify_contacts",
                ReverifyContactsInput(workspace_id=request.workspace_id),
                start_to_close_timeout=TIMEOUT,
                retry_policy=RETRY,
                result_type=ReverifyContactsResult,
            )
            workflow.logger.info(
                "re-checked %s addresses, %s moved, %s withdrawn from sending (%s)",
                rechecked.checked,
                rechecked.changed,
                rechecked.downgraded,
                rechecked.reason or "ok",
            )
        except Exception as error:
            workflow.logger.warning("address re-check skipped: %s", error)

        # Last, and deliberately after both repairs: the vitals should describe
        # the pipeline as it stands once this pass has done what it can, not as
        # it was before. A runway alarm that fires on a backlog the sweep was
        # about to clear is an alarm nobody can act on.
        #
        # Its failure is swallowed. A health check that can stop the repairs is
        # a worse liability than one that occasionally misses an hour.
        try:
            vitals: CheckVitalsResult = await workflow.execute_activity(
                "check_pipeline_vitals",
                CheckVitalsInput(workspace_id=request.workspace_id),
                start_to_close_timeout=TIMEOUT,
                retry_policy=RETRY,
                result_type=CheckVitalsResult,
            )
            workflow.logger.info(
                "vitals: %s leads, %s sends of %s allowed, alarms %s",
                vitals.leads_in_hand,
                vitals.sends_today,
                vitals.daily_send_capacity,
                list(vitals.alarms) or "none",
            )
        except Exception as exc:
            workflow.logger.warning("vitals check failed: %s", str(exc)[:300])

        return swept


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
