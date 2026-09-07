"""The loop that watches the schedules, and is deliberately not one of them.

Every recurring job in this system is a Temporal schedule, and that is the
right shape for all of them but one. A watchdog for wedged schedules cannot be
a schedule, because the failure it exists to catch is precisely the failure it
would then suffer -- housekeeping's clock stopped on 31 August and stayed
stopped for nine days, and a watchdog installed beside it would have stopped in
the same minute with nobody the wiser.

**So it is an always-on workflow, on the orchestrator's pattern.** Timer loop,
continue-as-new on a bounded cycle count, no schedule behind it. That is not a
guess about which mechanism is more reliable: through the same window in which
housekeeping died, the campaign orchestrators cycled 232 times without missing
one. Betting the healer on the mechanism that broke is the mistake; betting it
on the mechanism that held is the repair.

**Fifteen minutes.** Long enough that four passes an hour is nothing next to
the work the same worker is already doing, short enough that a stall costs less
than one housekeeping occurrence. The failure mode being closed is a nine-day
one, so the interval is not the interesting number -- being under an hour is.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from titan.workflows.schedule_health import HealResult
    from titan.workflows.types import HealSchedulesInput

#: Reading seven schedules and, at most, rewriting seven specs.
HEAL_TIMEOUT = timedelta(minutes=5)

CHECK_INTERVAL = timedelta(minutes=15)

#: Bounded, and small. A pass that cannot reach Temporal is a pass whose next
#: attempt is fifteen minutes away regardless, and hammering a server that is
#: already unwell is how a watchdog becomes the load.
HEAL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)

#: Same reasoning as the orchestrator: a workflow that loops for ever
#: accumulates history for ever, and Temporal force-terminates a run that
#: exceeds its event limit -- with no warning and no partial failure first.
#: At a quarter-hour cycle this is four days a run.
CYCLES_BEFORE_CONTINUE = 384


@workflow.defn(name="SupervisorWorkflow")
class SupervisorWorkflow:
    """Keep the recurring jobs recurring."""

    def __init__(self) -> None:
        self._cycles = 0
        self._healed = 0
        self._last_check_at: str | None = None

    @workflow.query(name="status")
    def status(self) -> dict:
        return {
            "cycles_completed": self._cycles,
            "schedules_healed": self._healed,
            "last_check_at": self._last_check_at,
        }

    @workflow.run
    async def run(self, request: HealSchedulesInput) -> None:
        while self._cycles < CYCLES_BEFORE_CONTINUE:
            self._last_check_at = workflow.now().isoformat()
            try:
                result: HealResult = await workflow.execute_activity(
                    "heal_schedules",
                    request,
                    start_to_close_timeout=HEAL_TIMEOUT,
                    retry_policy=HEAL_RETRY,
                    result_type=HealResult,
                )
                self._healed += len(result.healed)
                if result.healed:
                    workflow.logger.warning(
                        "reinstalled %s stopped schedule(s)", len(result.healed)
                    )
            except Exception as error:
                # Swallowed on purpose. This loop's only job is to still be
                # running the next time a schedule stops, and a watchdog that
                # exits on the first bad minute has to be noticed and restarted
                # by hand -- which is the errand it exists to remove.
                workflow.logger.warning("schedule check skipped: %s", error)

            self._cycles += 1
            await workflow.sleep(CHECK_INTERVAL)

        workflow.continue_as_new(request)
