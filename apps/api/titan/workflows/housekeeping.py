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
    from titan.workflows.queues import MAINTENANCE_QUEUE
    from titan.workflows.types import (
        CheckVitalsInput,
        CheckVitalsResult,
        EraseExpiredDataInput,
        EraseExpiredDataResult,
        ExpireAlarmsInput,
        ExpireAlarmsResult,
        PingWatchdogInput,
        PingWatchdogResult,
        ReadmitLeadsInput,
        ReadmitLeadsResult,
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
            task_queue=MAINTENANCE_QUEUE,
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
            task_queue=MAINTENANCE_QUEUE,
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
                task_queue=MAINTENANCE_QUEUE,
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
                task_queue=MAINTENANCE_QUEUE,
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

        # Re-admission, first of the three additions and deliberately before
        # the retention pass: it *returns* leads to the pipeline, and a pass
        # that erased content before re-admitting would be judging a lead on a
        # body it had just emptied. Ordering matters more than it looks here.
        #
        # Swallowed like the rest. A campaign that could not be re-checked this
        # hour is re-checked next hour, and the sweep this pass exists for is
        # worth more than the promotion.
        try:
            readmitted: ReadmitLeadsResult = await workflow.execute_activity(
                "readmit_leads",
                ReadmitLeadsInput(workspace_id=request.workspace_id),
                start_to_close_timeout=TIMEOUT,
                task_queue=MAINTENANCE_QUEUE,
                retry_policy=RETRY,
                result_type=ReadmitLeadsResult,
            )
            if readmitted.promoted:
                workflow.logger.info(
                    "re-admitted %s lead(s) the gate has moved past; %s still waiting",
                    readmitted.promoted,
                    readmitted.remaining,
                )
        except Exception as error:
            workflow.logger.warning("lead re-admission skipped: %s", error)

        # Retention, after both repairs and before the vitals. Last of the
        # three because it is the only one that destroys anything: if a pass is
        # going to be cut short by a worker restart, the sweep and the re-check
        # are the ones worth having run.
        #
        # Swallowed like the others. A retention pass that cannot reach the
        # database must not be recorded as a housekeeping failure -- the drafts
        # it swept are still swept -- and the erasure is idempotent, so the
        # next hour simply does it.
        try:
            erased: EraseExpiredDataResult = await workflow.execute_activity(
                "erase_expired_data",
                EraseExpiredDataInput(workspace_id=request.workspace_id),
                start_to_close_timeout=TIMEOUT,
                task_queue=MAINTENANCE_QUEUE,
                retry_policy=RETRY,
                result_type=EraseExpiredDataResult,
            )
            if erased.leads_examined:
                workflow.logger.info(
                    "erased content for %s lead(s) past the retention window: "
                    "%s draft(s), %s page(s); %s kept because they replied",
                    erased.leads_examined,
                    erased.drafts_erased,
                    erased.pages_erased,
                    erased.kept_for_reply,
                )
        except Exception as error:
            workflow.logger.warning("retention pass skipped: %s", error)

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
                task_queue=MAINTENANCE_QUEUE,
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

        # The queue the alarms land in, swept after they are raised.
        #
        # After the vitals rather than before, so an alarm this pass has
        # just filed is never closed by the same pass that filed it. Only
        # machine alarms are touched; a reply is a person waiting, and the
        # sixteen sitting unread are exactly what this makes visible again.
        #
        # Swallowed like the rest. Housekeeping that repaired the pipeline
        # must not be recorded as failed because a tidy-up could not run.
        try:
            closed: ExpireAlarmsResult = await workflow.execute_activity(
                "expire_stale_alarms",
                ExpireAlarmsInput(workspace_id=request.workspace_id),
                start_to_close_timeout=TIMEOUT,
                task_queue=MAINTENANCE_QUEUE,
                retry_policy=RETRY,
                result_type=ExpireAlarmsResult,
            )
            if closed.expired:
                workflow.logger.info(
                    "closed %s stale alarm(s); %s task(s) still open",
                    closed.expired,
                    closed.still_open,
                )
        except Exception as error:
            workflow.logger.warning("alarm expiry skipped: %s", error)

        # Truly last, and the only step whose point is that it did *not* run.
        #
        # Every other guard in this workflow runs inside the process it is
        # watching, so none of them can report the one failure that has
        # actually happened: the machine being off. Nothing went out on 4, 5 or
        # 6 September and nobody knew until somebody looked. An external
        # watchdog notices the silence instead, and this is the sound it
        # listens for.
        #
        # After the repairs rather than before, so a pass that died halfway
        # through does not report a clean hour. Swallowed like the rest: a
        # watchdog that cannot be reached is a monitoring problem, and it must
        # not be recorded as a housekeeping failure.
        try:
            ping: PingWatchdogResult = await workflow.execute_activity(
                "ping_watchdog",
                PingWatchdogInput(),
                start_to_close_timeout=timedelta(seconds=30),
                task_queue=MAINTENANCE_QUEUE,
                # One attempt. The next pass is an hour away and a dead man's
                # switch tolerates a missed beat far better than it tolerates a
                # retry storm against a monitoring endpoint.
                retry_policy=RetryPolicy(maximum_attempts=1),
                result_type=PingWatchdogResult,
            )
            if not ping.pinged and ping.reason:
                workflow.logger.info("watchdog not pinged: %s", ping.reason)
        except Exception as exc:
            workflow.logger.warning("watchdog ping skipped: %s", str(exc)[:200])

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
