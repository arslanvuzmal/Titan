"""The always-on campaign workflow, against a real Temporal test environment.

Time-skipping, so an hourly cycle and a rollover to a fresh run are exercised in
milliseconds rather than asserted about in a comment.

The planner is stubbed. What is under test is the *workflow body*: that it stops
when told, that it does not spend budget it has not got, that it survives a
planner outage, and -- the one that would otherwise only surface in production
weeks later -- that continuing as new does not kill the research children it
started.

**Why every test polls the status query rather than awaiting a result.** This
workflow is built never to finish: on reaching its cycle limit it continues as
new, and a result handle follows that chain forever. Awaiting it would hang
until the suite timeout, and the obvious fix -- signalling stop immediately --
races the dispatch loop and silently asserts against a workflow that stopped
before it did any work. Polling the query observes the run at a defined point,
then stops it deliberately.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from titan.workflows.orchestrator import (
    POOL_LOW_WATER_MARK,
    CampaignOrchestratorWorkflow,
    orchestrator_workflow_id,
)
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

WORKFLOW_TIMEOUT = 60.0
TASK_QUEUE = "titan-orchestrator-test"

WORKSPACE = str(uuid.uuid4())
CAMPAIGN = str(uuid.uuid4())

#: Far longer than any test's skipped horizon. A child that finishes cannot
#: demonstrate what happens to one still running when its parent rolls over, and
#: under time-skipping a short sleep is skipped the moment everything is idle.
CHILD_LIFETIME_SECONDS = 86_400 * 100


@workflow.defn(name="LeadResearchWorkflow")
class ResearchStub:
    """Stands in for the real research workflow, and stays running."""

    @workflow.run
    async def run(self, request: ResearchLeadInput) -> str:
        await asyncio.sleep(CHILD_LIFETIME_SECONDS)
        return request.lead_id


@dataclass
class Planner:
    """A configurable stub planner that records what it was asked."""

    plans: list[CampaignCyclePlan] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    #: Leading calls that should raise, to exercise a planner outage.
    failures: int = 0

    def next_plan(self) -> CampaignCyclePlan:
        if not self.plans:
            return CampaignCyclePlan(
                verdict=CycleVerdict.NO_WORK_AVAILABLE.value,
                pool_remaining=STOCKED,
            )
        # The last plan repeats once exhausted, so a test can say "then this
        # forever" without listing one plan per cycle.
        return self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]

    def activities(self) -> list:
        planner = self

        @activity.defn(name="plan_campaign_cycle")
        async def plan_campaign_cycle(request: CampaignCycleInput) -> CampaignCyclePlan:
            planner.calls.append(request.cycle_key)
            if planner.failures > 0:
                planner.failures -= 1
                raise RuntimeError("database unreachable")
            return planner.next_plan()

        return [plan_campaign_cycle]


@dataclass
class Discoverer:
    """A configurable stub for the discovery activity."""

    results: list[DiscoverActivityResult] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    failures: int = 0

    def next_result(self) -> DiscoverActivityResult:
        if not self.results:
            return DiscoverActivityResult(refused_reason="nothing configured")
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    def activities(self) -> list:
        discoverer = self

        @activity.defn(name="discover_leads")
        async def discover_leads(
            request: DiscoverActivityInput,
        ) -> DiscoverActivityResult:
            discoverer.calls.append(request.idempotency_key)
            if discoverer.failures > 0:
                discoverer.failures -= 1
                raise RuntimeError("places unreachable")
            return discoverer.next_result()

        return [discover_leads]


#: Above POOL_LOW_WATER_MARK, so a plan that says nothing about the pool does
#: not pull discovery into every unrelated test.
STOCKED = POOL_LOW_WATER_MARK * 3


def ready(*lead_ids: str, pool_remaining: int = STOCKED) -> CampaignCyclePlan:
    return CampaignCyclePlan(
        verdict=CycleVerdict.READY.value,
        leads=tuple(PlannedLead(lead_id=lead) for lead in lead_ids),
        remaining_budget=len(lead_ids),
        pool_remaining=pool_remaining,
    )


def make_input(**overrides) -> CampaignOrchestratorInput:
    base = {
        "workspace_id": WORKSPACE,
        "campaign_id": CAMPAIGN,
        "interval_minutes": 60,
        "max_new_research": 5,
        "cycles_before_continue": 2,
    }
    base.update(overrides)
    return CampaignOrchestratorInput(**base)


async def wait_until(
    env: WorkflowEnvironment,
    handle: WorkflowHandle,
    predicate: Callable[[OrchestratorStatus], bool],
    *,
    what: str,
    attempts: int = 40,
) -> OrchestratorStatus:
    """Advance the test clock a cycle at a time until the workflow gets there.

    ``env.sleep`` is what moves time. Automatic skipping only happens while the
    client is blocked on a workflow *result*, and this workflow never produces
    one -- it continues as new instead. A plain polling loop would therefore sit
    on the real clock while the workflow waited out an hour-long timer that
    never fired, and every assertion would time out with the run parked in
    'waiting'.
    """
    status: OrchestratorStatus | None = None
    for _ in range(attempts):
        # result_type is required when querying by name: without it the payload
        # arrives as a plain dict, and every attribute access below fails.
        status = await handle.query("status", result_type=OrchestratorStatus)
        if predicate(status):
            return status
        await env.sleep(timedelta(minutes=61))
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}; last status was {status}")


async def stop_and_collect(handle: WorkflowHandle) -> OrchestratorStatus:
    await handle.signal("stop", "test finished")
    return await asyncio.wait_for(handle.result(), timeout=WORKFLOW_TIMEOUT)


def worker_for(
    client: Client, planner: Planner, discoverer: Discoverer | None = None
) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[CampaignOrchestratorWorkflow, ResearchStub],
        activities=planner.activities() + (discoverer or Discoverer()).activities(),
    )


async def launch(client: Client, request: CampaignOrchestratorInput, tag: str):
    return await client.start_workflow(
        CampaignOrchestratorWorkflow.run,
        request,
        id=f"orch-{tag}-{uuid.uuid4().hex[:8]}",
        task_queue=TASK_QUEUE,
    )


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


# ==========================================================================
# Dispatch
# ==========================================================================


@pytest.mark.asyncio
async def test_a_ready_plan_starts_one_child_per_lead(env) -> None:
    planner = Planner(plans=[ready("lead-a", "lead-b")])

    async with worker_for(env.client, planner):
        handle = await launch(env.client, make_input(), "dispatch")
        status = await wait_until(
            env, handle, lambda s: s.leads_started >= 2, what="two children to start"
        )
        await stop_and_collect(handle)

    assert status.leads_started == 2
    assert planner.calls[0] == f"{CAMPAIGN}:0"


@pytest.mark.asyncio
async def test_an_unauthorized_campaign_starts_nothing(env) -> None:
    """A campaign paused yesterday must not be worked by an orchestrator
    started weeks ago. Authorization is read fresh each cycle, never carried."""
    planner = Planner(
        plans=[
            CampaignCyclePlan(
                verdict=CycleVerdict.NOT_AUTHORIZED.value,
                detail="campaign status is paused, not active",
            )
        ]
    )

    async with worker_for(env.client, planner):
        handle = await launch(env.client, make_input(), "unauth")
        status = await wait_until(
            env,
            handle,
            lambda s: s.last_verdict == CycleVerdict.NOT_AUTHORIZED.value,
            what="the unauthorized verdict",
        )
        await stop_and_collect(handle)

    assert status.leads_started == 0


@pytest.mark.asyncio
async def test_a_spent_budget_starts_nothing_but_keeps_cycling(env) -> None:
    """Budget exhaustion is normal, not a fault.

    The orchestrator has to come back after midnight rather than ending, or
    every campaign would need restarting by hand each morning.
    """
    planner = Planner(plans=[CampaignCyclePlan(verdict=CycleVerdict.BUDGET_SPENT.value)])

    async with worker_for(env.client, planner):
        handle = await launch(env.client, make_input(), "budget")
        status = await wait_until(
            env, handle, lambda s: s.cycles_completed >= 3, what="three cycles"
        )
        await stop_and_collect(handle)

    assert status.leads_started == 0
    assert status.last_verdict == CycleVerdict.BUDGET_SPENT.value


# ==========================================================================
# Continue-as-new
# ==========================================================================


@pytest.mark.asyncio
async def test_it_continues_as_new_and_carries_its_counters(env) -> None:
    """Without this, Temporal force-terminates the run once its history exceeds
    the event limit -- with no warning and no partial failure to notice first.

    ``cycles_before_continue=2`` means passing 2 can only have happened by
    rolling into a successor run that inherited the count.
    """
    planner = Planner(plans=[ready("lead-a")])

    async with worker_for(env.client, planner):
        handle = await launch(env.client, make_input(cycles_before_continue=2), "can")
        status = await wait_until(
            env, handle, lambda s: s.cycles_completed > 2, what="a rollover"
        )
        await stop_and_collect(handle)

    assert status.cycles_completed > 2


@pytest.mark.asyncio
async def test_continuing_as_new_does_not_kill_running_research(env) -> None:
    """The property that would otherwise fail silently in production.

    Continue-as-new *closes* the parent run, and the default parent-close policy
    takes every child with it. A research child sitting in the seven-day
    approval wait is the most expensive thing in the system: a crawl, a model
    call and an operator's attention already spent. ABANDON is what lets it
    survive its parent rolling over.
    """
    planner = Planner(plans=[ready("lead-abandon")])

    async with worker_for(env.client, planner):
        handle = await launch(env.client, make_input(cycles_before_continue=1), "abandon")
        await wait_until(env, handle, lambda s: s.cycles_completed > 1, what="a rollover")

        child = env.client.get_workflow_handle(
            f"research::{WORKSPACE}::{CAMPAIGN}::lead-abandon"
        )
        described = await child.describe()
        await stop_and_collect(handle)

    # TERMINATED here would mean the default close policy had been left in place.
    assert described.status is WorkflowExecutionStatus.RUNNING


# ==========================================================================
# Operator control
# ==========================================================================


@pytest.mark.asyncio
async def test_pause_stops_dispatch_without_ending_the_workflow(env) -> None:
    """Pausing is not cancelling, and it is not idling either.

    A paused orchestrator keeps its counters and identity, so resuming is a
    signal rather than remembering how it was launched. It must also genuinely
    *stop*: the first version of this waited on a condition that was already
    true, so pausing turned the cycle loop into a spin that counted a hundred
    and forty cycles in seconds and would have exhausted the workflow's event
    history in minutes -- the exact failure continue-as-new exists to prevent.
    """
    planner = Planner(plans=[ready("lead-a")])

    async with worker_for(env.client, planner):
        handle = await launch(env.client, make_input(cycles_before_continue=100), "pause")
        working = await wait_until(
            env, handle, lambda s: s.cycles_completed >= 1, what="the first cycle"
        )

        await handle.signal("pause", PauseSignal(reason="investigating bounces"))
        paused = await wait_until(
            env, handle, lambda s: s.paused_reason is not None, what="the pause to take"
        )

        # A week of skipped time. An orchestrator that kept cycling while paused
        # would rack up well over a hundred cycles here.
        for _ in range(12):
            await env.sleep(timedelta(hours=14))
            await asyncio.sleep(0)
        after = await handle.query("status", result_type=OrchestratorStatus)

        result = await stop_and_collect(handle)

    assert paused.paused_reason == "investigating bounces"
    assert working.cycles_completed >= 1
    # The counter is frozen: no work happened across a week of clock time.
    assert after.cycles_completed == paused.cycles_completed
    assert after.state == "paused"
    assert result.state == "stopped"


@pytest.mark.asyncio
async def test_resume_clears_the_pause(env) -> None:
    planner = Planner(plans=[ready("lead-resume")])

    async with worker_for(env.client, planner):
        handle = await launch(
            env.client, make_input(cycles_before_continue=100), "resume"
        )
        await handle.signal("pause", PauseSignal(reason="holding"))
        await wait_until(
            env, handle, lambda s: s.paused_reason is not None, what="the pause to take"
        )

        await handle.signal("resume")
        status = await wait_until(
            env,
            handle,
            lambda s: s.paused_reason is None and s.leads_started >= 1,
            what="work to resume",
        )
        await stop_and_collect(handle)

    assert status.leads_started >= 1


@pytest.mark.asyncio
async def test_stop_ends_the_workflow(env) -> None:
    planner = Planner(plans=[ready("lead-a")])

    async with worker_for(env.client, planner):
        handle = await launch(env.client, make_input(cycles_before_continue=100), "stop")
        result = await stop_and_collect(handle)

    assert result.state == "stopped"
    # Stopped well short of its hundred cycles.
    assert result.cycles_completed < 100


# ==========================================================================
# Resilience
# ==========================================================================


@pytest.mark.asyncio
async def test_a_planner_outage_does_not_end_the_orchestrator(env) -> None:
    """The database being briefly unreachable is not a reason to stop.

    An orchestrator that exits on the first bad minute has to be noticed and
    restarted by hand, which is the opposite of what it exists for.
    """
    planner = Planner(plans=[ready("lead-a")], failures=8)

    async with worker_for(env.client, planner):
        handle = await launch(
            env.client, make_input(cycles_before_continue=100), "outage"
        )
        # Survived the outage and went on to do real work afterwards.
        status = await wait_until(
            env, handle, lambda s: s.leads_started >= 1, what="recovery after the outage"
        )
        await stop_and_collect(handle)

    assert status.cycles_completed >= 2
    assert planner.failures == 0


@pytest.mark.asyncio
async def test_the_same_lead_is_not_researched_twice_concurrently(env) -> None:
    """Two cycles planning the same lead must not produce two crawls.

    The deterministic child workflow ID plus Temporal's ID-reuse policy is the
    mechanism; a second start raises and is absorbed rather than ending the
    cycle.
    """
    planner = Planner(plans=[ready("lead-dup")])

    async with worker_for(env.client, planner):
        handle = await launch(env.client, make_input(cycles_before_continue=100), "dup")
        status = await wait_until(
            env, handle, lambda s: s.cycles_completed >= 3, what="three cycles"
        )
        await stop_and_collect(handle)

    # Three cycles all planning the same lead; one child.
    assert status.leads_started == 1


@pytest.mark.asyncio
async def test_the_orchestrator_id_is_one_per_campaign(env) -> None:
    """Two orchestrators on one campaign would each plan against the full daily
    budget and between them spend it twice."""
    first = orchestrator_workflow_id(WORKSPACE, CAMPAIGN)
    second = orchestrator_workflow_id(WORKSPACE, CAMPAIGN)

    assert first == second
    assert orchestrator_workflow_id(WORKSPACE, str(uuid.uuid4())) != first


# ==========================================================================
# Discovery
# ==========================================================================
@pytest.mark.asyncio
async def test_a_low_pool_triggers_discovery(env) -> None:
    """Top up before the pool empties, not after.

    A campaign that discovers only once it has stalled has already lost the
    cycle it spent stalling.
    """
    planner = Planner(plans=[ready("lead-1", pool_remaining=POOL_LOW_WATER_MARK - 1)])
    discoverer = Discoverer(results=[DiscoverActivityResult(leads_created=12)])

    async with worker_for(env.client, planner, discoverer):
        handle = await launch(env.client, make_input(), "low-pool")
        await wait_until(
            env, handle, lambda s: s.leads_discovered >= 12, what="discovery"
        )
        status = await stop_and_collect(handle)

    assert discoverer.calls
    assert status.leads_discovered >= 12


@pytest.mark.asyncio
async def test_a_stocked_pool_does_not_trigger_discovery(env) -> None:
    """Discovery costs money per request; it is not run for its own sake."""
    planner = Planner(plans=[ready("lead-1")])
    discoverer = Discoverer()

    async with worker_for(env.client, planner, discoverer):
        handle = await launch(
            env.client, make_input(cycles_before_continue=100), "stocked"
        )
        await wait_until(
            env, handle, lambda s: s.cycles_completed >= 2, what="two cycles"
        )
        await stop_and_collect(handle)

    assert discoverer.calls == []


@pytest.mark.asyncio
async def test_an_empty_pool_discovers_then_replans_in_the_same_cycle(env) -> None:
    """Otherwise a stalled campaign waits a full interval on leads it already has."""
    planner = Planner(
        plans=[
            CampaignCyclePlan(
                verdict=CycleVerdict.NO_WORK_AVAILABLE.value, pool_remaining=0
            ),
            ready("found-1", "found-2"),
        ]
    )
    discoverer = Discoverer(results=[DiscoverActivityResult(leads_created=2)])

    async with worker_for(env.client, planner, discoverer):
        handle = await launch(env.client, make_input(), "refill")
        status = await wait_until(
            env, handle, lambda s: s.leads_started >= 2, what="research after refill"
        )
        await stop_and_collect(handle)

    assert status.leads_started == 2
    # The refill re-plan is a second call, distinguishable from the first.
    assert any(key.endswith(":refill") for key in planner.calls)


@pytest.mark.asyncio
async def test_discovery_that_finds_nothing_does_not_replan(env) -> None:
    """Re-planning against an unchanged pool would return the same verdict."""
    planner = Planner(
        plans=[
            CampaignCyclePlan(
                verdict=CycleVerdict.NO_WORK_AVAILABLE.value, pool_remaining=0
            )
        ]
    )
    discoverer = Discoverer(results=[DiscoverActivityResult(leads_created=0)])

    async with worker_for(env.client, planner, discoverer):
        handle = await launch(env.client, make_input(cycles_before_continue=100), "dry")
        await wait_until(
            env, handle, lambda s: s.cycles_completed >= 2, what="two cycles"
        )
        await stop_and_collect(handle)

    assert discoverer.calls
    assert not any(key.endswith(":refill") for key in planner.calls)


@pytest.mark.asyncio
async def test_a_spent_send_budget_does_not_spend_the_discovery_budget(env) -> None:
    """Leads found today could not be written to until tomorrow anyway, by which
    point the evidence is a day staler than it needed to be."""
    planner = Planner(
        plans=[
            CampaignCyclePlan(verdict=CycleVerdict.BUDGET_SPENT.value, pool_remaining=0)
        ]
    )
    discoverer = Discoverer()

    async with worker_for(env.client, planner, discoverer):
        handle = await launch(env.client, make_input(cycles_before_continue=100), "spent")
        await wait_until(
            env, handle, lambda s: s.cycles_completed >= 2, what="two cycles"
        )
        await stop_and_collect(handle)

    assert discoverer.calls == []


@pytest.mark.asyncio
async def test_a_discovery_outage_does_not_stop_dispatch(env) -> None:
    """A third party's outage must not stop outreach that needed nothing from it."""
    planner = Planner(plans=[ready("lead-1", pool_remaining=0)])
    discoverer = Discoverer(failures=99)

    async with worker_for(env.client, planner, discoverer):
        handle = await launch(env.client, make_input(), "outage")
        status = await wait_until(
            env, handle, lambda s: s.leads_started >= 1, what="research despite outage"
        )
        await stop_and_collect(handle)

    assert status.leads_started >= 1
    assert status.leads_discovered == 0


@pytest.mark.asyncio
async def test_the_discovered_count_survives_continue_as_new(env) -> None:
    """A lifetime total that resets on roll-over reads as a real number."""
    planner = Planner(plans=[ready("lead-1", pool_remaining=0)])
    discoverer = Discoverer(results=[DiscoverActivityResult(leads_created=3)])

    async with worker_for(env.client, planner, discoverer):
        handle = await launch(env.client, make_input(cycles_before_continue=2), "canc")
        status = await wait_until(
            env, handle, lambda s: s.cycles_completed >= 4, what="a roll-over"
        )
        await stop_and_collect(handle)

    # Four cycles at three leads each; nothing was reset by the roll-over.
    assert status.leads_discovered >= 12
