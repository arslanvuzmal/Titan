"""The always-on campaign workflow.

:class:`titan.workflows.research.LeadResearchWorkflow` works one lead and stops.
Something has to decide *which* leads, *how many*, and *when* -- and keep doing
it without anybody starting it each morning. That is this.

Each cycle: ask the planner what to work on, start a research child per lead,
wait, repeat. The workflow body contains no I/O and no clock reads other than
``workflow.now()``, so it replays identically.

Three properties do most of the work here, and all three are the kind that look
optional until the day they are not:

**It continues as new.** A workflow that loops forever accumulates history
forever, and Temporal force-terminates a run that exceeds its event limit. For
an hourly orchestrator that is weeks away, not years, and it arrives with no
warning and no partial failure to notice first. Continuing as new on a bounded
cycle count keeps every run's history small and turns "runs forever" into
something actually true.

**Its children are abandoned, not terminated.** Continue-as-new *closes* the
parent run. The default parent-close policy would take every in-flight research
child with it -- including any sitting in the seven-day approval wait, which is
exactly the population that has already cost the most to produce. ABANDON lets
them finish alone.

**It never plans its own work.** Every decision about authorization, budget and
eligibility comes from the planner activity reading the database at execution
time. An orchestrator started weeks ago must not keep working a campaign that
was paused yesterday, and invariant 18 says the workflow may not be trusted to
know its own policy.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from titan.workflows.research import research_workflow_id
    from titan.workflows.types import (
        CampaignCycleInput,
        CampaignCyclePlan,
        CampaignOrchestratorInput,
        CycleVerdict,
        DiscoverActivityInput,
        DiscoverActivityResult,
        OrchestratorStatus,
        PauseSignal,
        PlannedLead,
        ResearchLeadInput,
    )

PLAN_TIMEOUT = timedelta(minutes=5)

#: Discovery talks to Google over the network and writes a batch of rows.
DISCOVER_TIMEOUT = timedelta(minutes=10)

#: Fewer retries than planning, and slower. Each attempt can spend money: a
#: search that succeeded at Google and then failed on the way home would be
#: charged again on retry. The activity's idempotency key covers the case where
#: the failure happened after its own write; this keeps the window small.
DISCOVER_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
    non_retryable_error_types=["ValueError"],
)

#: Top the lead pool up once it drops below this. Chosen to be a cycle's worth
#: of work rather than zero: discovering only after a campaign has stalled means
#: every empty pool costs a full idle interval before anything is done about it.
POOL_LOW_WATER_MARK = 10

#: The planner reads the database and writes next_action_at. Retried, but a
#: missing campaign or a malformed id will fail identically every time.
PLAN_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=4,
    non_retryable_error_types=["ValueError"],
)

#: How long a cycle waits when the campaign is not authorized. Longer than the
#: normal interval on purpose: a paused campaign is usually paused deliberately,
#: and polling it every hour fills the workflow history with the same answer.
UNAUTHORIZED_BACKOFF = timedelta(hours=6)


@workflow.defn(name="CampaignOrchestratorWorkflow")
class CampaignOrchestratorWorkflow:
    """Runs one campaign continuously until stopped."""

    def __init__(self) -> None:
        # Every field initialised here. A field that exists only after a signal
        # is what broke replay in the pre-0.2 workflows.
        self._state: str = "starting"
        self._cycles: int = 0
        self._leads_started: int = 0
        self._leads_discovered: int = 0
        self._last_verdict: str | None = None
        self._last_cycle_at: str | None = None
        self._next_cycle_at: str | None = None
        self._paused_reason: str | None = None
        self._stop_reason: str | None = None
        self._campaign_id: str = ""

    # --------------------------------------------------------------- signals
    @workflow.signal(name="pause")
    def pause(self, signal: PauseSignal) -> None:
        """Stop starting new work, without ending the workflow.

        Distinct from cancellation on purpose. A paused orchestrator keeps its
        counters, its schedule and its identity, so resuming is a signal rather
        than a restart -- and an operator who paused a campaign to investigate
        something does not have to remember how it was launched.
        """
        self._paused_reason = signal.reason or "paused by operator"

    @workflow.signal(name="resume")
    def resume(self) -> None:
        self._paused_reason = None

    @workflow.signal(name="stop")
    def stop(self, reason: str) -> None:
        self._stop_reason = reason or "stopped by operator"

    # --------------------------------------------------------------- queries
    @workflow.query(name="status")
    def status(self) -> OrchestratorStatus:
        return OrchestratorStatus(
            campaign_id=self._campaign_id,
            state=self._state,
            cycles_completed=self._cycles,
            leads_started=self._leads_started,
            leads_discovered=self._leads_discovered,
            last_verdict=self._last_verdict,
            last_cycle_at=self._last_cycle_at,
            next_cycle_at=self._next_cycle_at,
            paused_reason=self._paused_reason,
        )

    # ------------------------------------------------------------------ run
    @workflow.run
    async def run(self, request: CampaignOrchestratorInput) -> OrchestratorStatus:
        self._campaign_id = request.campaign_id
        # Carried across continue-as-new so a status query reports lifetime
        # totals instead of resetting to zero every time the run rolls over.
        self._cycles = request.cycles_completed
        self._leads_started = request.leads_started
        self._leads_discovered = request.leads_discovered
        self._paused_reason = request.paused_reason

        interval = timedelta(minutes=request.interval_minutes)
        cycles_this_run = 0

        while cycles_this_run < request.cycles_before_continue:
            if self._stop_reason is not None:
                self._state = "stopped"
                return self.status()

            if self._paused_reason is not None:
                # Block until an operator acts. Emphatically *not* a timed wait
                # that re-checks: a paused orchestrator has nothing to do, and
                # looping on the interval would count cycles, spend history and
                # -- with no timer to wait on -- spin as fast as the worker
                # could turn it, filling the event log in minutes.
                self._state = "paused"
                self._last_verdict = "paused"
                self._next_cycle_at = None
                await workflow.wait_condition(
                    lambda: self._paused_reason is None or self._stop_reason is not None
                )
                continue

            wait = await self._run_cycle(request, interval)

            # Counted only for a cycle that actually ran. A paused orchestrator
            # is not doing cycles, and reporting otherwise would make the status
            # query say it was working.
            cycles_this_run += 1
            self._cycles += 1

            self._next_cycle_at = (workflow.now() + wait).isoformat()
            self._state = "waiting"

            # Wake early on any signal rather than sleeping the interval out. An
            # operator pausing a campaign should not wait an hour for it to
            # notice, and a plain sleep would make the signal feel broken.
            await self._sleep_or_signal(wait)
            if self._stop_reason is not None:
                self._state = "stopped"
                return self.status()

        # Same input, fresh history. The counters travel; the event log does not.
        workflow.continue_as_new(
            CampaignOrchestratorInput(
                workspace_id=request.workspace_id,
                campaign_id=request.campaign_id,
                interval_minutes=request.interval_minutes,
                max_new_research=request.max_new_research,
                cycles_before_continue=request.cycles_before_continue,
                cycles_completed=self._cycles,
                leads_started=self._leads_started,
                leads_discovered=self._leads_discovered,
                paused_reason=self._paused_reason,
            )
        )

    # ------------------------------------------------------------- internals
    async def _run_cycle(
        self, request: CampaignOrchestratorInput, interval: timedelta
    ) -> timedelta:
        """One plan-and-dispatch pass. Returns how long to wait afterwards."""
        self._state = "planning"
        self._last_cycle_at = workflow.now().isoformat()
        cycle_key = f"{request.campaign_id}:{self._cycles}"

        plan = await self._plan(request, cycle_key)
        if plan is None:
            return interval

        self._last_verdict = plan.verdict

        if plan.verdict == CycleVerdict.NOT_AUTHORIZED.value:
            self._state = "not_authorized"
            return UNAUTHORIZED_BACKOFF

        # Top the pool up before it empties, and refill it when it already has.
        # Not attempted when the day's sends are spent: discovery costs money to
        # find leads that cannot be written to until tomorrow, by which point the
        # evidence is a day staler than it needed to be.
        if (
            plan.verdict != CycleVerdict.BUDGET_SPENT.value
            and plan.pool_remaining < POOL_LOW_WATER_MARK
        ):
            found = await self._discover(request, cycle_key)
            # Only re-plan when the campaign had nothing at all to do. With work
            # already in hand, the new leads keep until the next cycle, and
            # planning twice in one pass would dispatch more research than the
            # cycle's ceiling allows.
            if found and plan.verdict == CycleVerdict.NO_WORK_AVAILABLE.value:
                replanned = await self._plan(request, f"{cycle_key}:refill")
                if replanned is not None:
                    plan = replanned
                    self._last_verdict = plan.verdict

        if plan.verdict != CycleVerdict.READY.value:
            # BUDGET_SPENT and NO_WORK_AVAILABLE are both normal. The planner has
            # already recorded a notification for the stall case; the workflow's
            # job is simply to come back later.
            self._state = plan.verdict
            return interval

        self._state = "dispatching"
        for lead in plan.leads:
            if self._paused_reason is not None or self._stop_reason is not None:
                break
            await self._start_research(request, lead)

        return interval

    async def _plan(
        self, request: CampaignOrchestratorInput, cycle_key: str
    ) -> CampaignCyclePlan | None:
        """Ask the planner what to work on. None when it could not be asked.

        A planner that cannot run is not a reason to end the orchestrator: the
        database may be briefly unreachable, and a workflow that exits on the
        first bad minute has to be noticed and restarted by hand, which is the
        opposite of what this exists for.
        """
        try:
            return await workflow.execute_activity(
                "plan_campaign_cycle",
                CampaignCycleInput(
                    workspace_id=request.workspace_id,
                    campaign_id=request.campaign_id,
                    cycle_key=cycle_key,
                    max_new_research=request.max_new_research,
                ),
                start_to_close_timeout=PLAN_TIMEOUT,
                retry_policy=PLAN_RETRY,
                result_type=CampaignCyclePlan,
            )
        except ActivityError as exc:
            self._state = "plan_failed"
            self._last_verdict = "plan_failed"
            workflow.logger.warning("campaign planning failed: %s", str(exc)[:300])
            return None

    async def _discover(self, request: CampaignOrchestratorInput, cycle_key: str) -> int:
        """Search for new leads. Returns how many were created.

        Failure is swallowed on purpose. Discovery is a top-up, and a campaign
        with work in hand must keep dispatching it when Google is unreachable or
        the key has expired -- letting this propagate would let a third party's
        outage stop outreach that needed nothing from them. The refusal reason
        is logged, and the cases worth a person's attention (no targeting, empty
        results) are recorded as notifications by the activity itself.
        """
        self._state = "discovering"
        try:
            found: DiscoverActivityResult = await workflow.execute_activity(
                "discover_leads",
                DiscoverActivityInput(
                    workspace_id=request.workspace_id,
                    campaign_id=request.campaign_id,
                    idempotency_key=f"discover:{cycle_key}",
                    max_results=request.max_new_research,
                ),
                start_to_close_timeout=DISCOVER_TIMEOUT,
                retry_policy=DISCOVER_RETRY,
                result_type=DiscoverActivityResult,
            )
        except ActivityError as exc:
            workflow.logger.warning("lead discovery failed: %s", str(exc)[:300])
            return 0

        self._leads_discovered += found.leads_created
        if found.refused_reason is not None:
            workflow.logger.info("lead discovery refused: %s", found.refused_reason)
        return found.leads_created

    async def _start_research(
        self, request: CampaignOrchestratorInput, lead: PlannedLead
    ) -> None:
        """Start one research child, tolerating one that is already running."""
        try:
            await workflow.start_child_workflow(
                "LeadResearchWorkflow",
                ResearchLeadInput(
                    workspace_id=request.workspace_id,
                    campaign_id=request.campaign_id,
                    lead_id=lead.lead_id,
                    # Deterministic per lead per cycle: a replay of this cycle
                    # derives the same downstream idempotency keys and finds its
                    # own prior work rather than crawling and drafting again.
                    run_key=f"{request.campaign_id}:{lead.lead_id}:{self._cycles}",
                    seed_url=lead.seed_url,
                ),
                id=research_workflow_id(
                    request.workspace_id, request.campaign_id, lead.lead_id
                ),
                task_queue=workflow.info().task_queue,
                # The whole reason this workflow can continue-as-new safely.
                # Without ABANDON, every rollover would terminate the research
                # children still waiting on human approval -- the ones that have
                # already cost a crawl, a model call and an operator's attention.
                parent_close_policy=ParentClosePolicy.ABANDON,
            )
            self._leads_started += 1
        except Exception as exc:
            # Most often "already started", which is the ID-reuse policy doing
            # its job: this lead is already being researched and must not be
            # researched twice. Not an error, and not worth ending the cycle for.
            workflow.logger.info(
                "research child not started for %s: %s", lead.lead_id, str(exc)[:200]
            )

    async def _sleep_or_signal(self, wait: timedelta) -> bool:
        """Sleep, waking early if an operator signals. True if woken early."""
        try:
            await workflow.wait_condition(
                lambda: self._stop_reason is not None or self._paused_reason is not None,
                timeout=wait,
            )
        except TimeoutError:
            return False
        return True


def orchestrator_workflow_id(workspace_id: str, campaign_id: str) -> str:
    """Deterministic workflow ID.

    One orchestrator per campaign, enforced by Temporal's ID reuse policy rather
    than by a check somebody has to remember. Starting it twice is a no-op; two
    orchestrators on one campaign would each plan against the full daily budget
    and between them spend it twice.
    """
    return f"orchestrator::{workspace_id}::{campaign_id}"


__all__ = [
    "PLAN_RETRY",
    "PLAN_TIMEOUT",
    "UNAUTHORIZED_BACKOFF",
    "CampaignOrchestratorWorkflow",
    "orchestrator_workflow_id",
]
