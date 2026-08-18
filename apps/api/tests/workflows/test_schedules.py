"""Closing the loop: the recurring jobs that make the system run itself.

Every workflow was already written to be scheduled and none of them were ever
started, so this is the join. The tests that matter are not "does it create a
schedule" -- they are the four ways an installer can quietly undo somebody's
decision: resurrecting a paused schedule, backfilling a burst of missed runs,
buffering overlapping runs instead of dropping them, and starting a second
always-on loop on a campaign that already has one.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from temporalio.client import ScheduleAlreadyRunningError, ScheduleOverlapPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from titan.db.enums import CampaignStatus
from titan.workflows import schedules
from titan.workflows.schedules import (
    Applied,
    Outcome,
    plan_orchestrators,
    plan_schedules,
    summarise,
)

QUEUE = "titan-research"
WS = uuid.UUID("11111111-1111-1111-1111-111111111111")


# ==========================================================================
# Fakes
# ==========================================================================
class FakeState:
    def __init__(self, paused: bool, note: str = "") -> None:
        self.paused = paused
        self.note = note


class FakeSchedule:
    def __init__(self, paused: bool = False, note: str = "", cron: str = "") -> None:
        self.state = FakeState(paused, note)
        self.cron = cron


class FakeDescription:
    def __init__(self, schedule: FakeSchedule) -> None:
        self.schedule = schedule


class FakeUpdateInput:
    def __init__(self, description: FakeDescription) -> None:
        self.description = description


class FakeHandle:
    def __init__(self, client, schedule_id: str) -> None:
        self._client = client
        self._id = schedule_id

    async def describe(self) -> FakeDescription:
        return FakeDescription(self._client.existing[self._id])

    async def update(self, mutate) -> None:
        current = self._client.existing[self._id]
        result = mutate(FakeUpdateInput(FakeDescription(current)))
        self._client.updated.append((self._id, result.schedule))
        self._client.existing[self._id] = result.schedule


class FakeClient:
    """Enough Temporal to exercise install() and start_orchestrators()."""

    def __init__(self, existing: dict | None = None, running: set | None = None) -> None:
        self.existing: dict[str, FakeSchedule] = existing or {}
        self.running: set[str] = running or set()
        self.created: list[tuple[str, object]] = []
        self.updated: list[tuple[str, object]] = []
        self.started: list[str] = []

    async def create_schedule(self, schedule_id: str, schedule):
        if schedule_id in self.existing:
            raise ScheduleAlreadyRunningError()
        self.existing[schedule_id] = schedule
        self.created.append((schedule_id, schedule))

    def get_schedule_handle(self, schedule_id: str) -> FakeHandle:
        return FakeHandle(self, schedule_id)

    async def start_workflow(self, workflow, arg, *, id: str, task_queue: str):
        if id in self.running:
            raise WorkflowAlreadyStartedError(id, str(workflow))
        self.running.add(id)
        self.started.append(id)


# ==========================================================================
# Planning
# ==========================================================================
def test_a_workspace_gets_a_report_and_a_verification_job() -> None:
    """Measure exists but was never scheduled -- that is what made the loop an
    arc."""
    jobs = plan_schedules(WS, task_queue=QUEUE)

    assert {j.workflow for j in jobs} == {
        "WeeklyReportWorkflow",
        "SenderVerificationWorkflow",
        "MailboxRampWorkflow",
        "DeliveryEventPollWorkflow",
        "SenderHealthSnapshotWorkflow",
        # Honouring an unsubscribe is not something to do only when a delivery
        # poll happens to succeed, so it has a schedule of its own.
        "PullOptOutsWorkflow",
    }
    assert all(j.task_queue == QUEUE for j in jobs)


def test_the_crons_come_from_the_workflows_not_from_here() -> None:
    """The workflows already declared when they should run. Restating the cron
    in the installer would let the two drift silently."""
    from titan.workflows.delivery_events import DEFAULT_CRON as poll_cron
    from titan.workflows.reporting import DEFAULT_CRON as report_cron
    from titan.workflows.sender_health import DEFAULT_CRON as health_cron
    from titan.workflows.verification import DEFAULT_CRON as verify_cron

    crons = {j.workflow: j.cron for j in plan_schedules(WS, task_queue=QUEUE)}

    assert crons["WeeklyReportWorkflow"] == report_cron
    assert crons["SenderVerificationWorkflow"] == verify_cron
    assert crons["DeliveryEventPollWorkflow"] == poll_cron
    assert crons["SenderHealthSnapshotWorkflow"] == health_cron


def test_the_delivery_event_poll_is_scheduled_at_all() -> None:
    """The whole reason the event table stayed empty.

    Everything downstream -- suppression, the reply that stops a follow-up, the
    deliverability window the ramp reads -- waits on outcomes arriving. Built
    and never scheduled is indistinguishable at runtime from never built, and
    that is precisely the state this was found in.
    """
    jobs = {j.workflow: j for j in plan_schedules(WS, task_queue=QUEUE)}

    assert "DeliveryEventPollWorkflow" in jobs
    assert jobs["DeliveryEventPollWorkflow"].arg.workspace_id == str(WS)


def test_measurement_is_not_installed_switched_off() -> None:
    """Neither job sends anything. A deploy that leaves measurement paused
    produces a system that looks healthy because nothing is looking."""
    assert all(not j.starts_paused for j in plan_schedules(WS, task_queue=QUEUE))


def test_schedule_ids_are_scoped_to_the_workspace() -> None:
    other = uuid.UUID("22222222-2222-2222-2222-222222222222")
    mine = {j.schedule_id for j in plan_schedules(WS, task_queue=QUEUE)}
    theirs = {j.schedule_id for j in plan_schedules(other, task_queue=QUEUE)}

    assert not (mine & theirs)


def test_only_active_campaigns_get_a_loop() -> None:
    """A paused campaign is a human decision. The orchestrator carries
    paused_reason across continue-as-new precisely so that decision survives; a
    fresh workflow would discard it."""
    campaigns = [
        (uuid.UUID(int=1), CampaignStatus.ACTIVE),
        (uuid.UUID(int=2), CampaignStatus.PAUSED),
        (uuid.UUID(int=3), CampaignStatus.DRAFT),
        (uuid.UUID(int=4), CampaignStatus.COMPLETED),
        (uuid.UUID(int=5), CampaignStatus.ARCHIVED),
    ]

    starts = plan_orchestrators(WS, campaigns, task_queue=QUEUE)

    assert [s.campaign_id for s in starts] == [uuid.UUID(int=1)]


def test_no_active_campaigns_starts_nothing() -> None:
    assert plan_orchestrators(WS, [], task_queue=QUEUE) == []


def test_a_campaigns_loop_id_is_stable() -> None:
    """Two runs must produce the same id, or the second start creates a second
    loop and the campaign spends its daily budget twice."""
    campaigns = [(uuid.UUID(int=7), CampaignStatus.ACTIVE)]

    first = plan_orchestrators(WS, campaigns, task_queue=QUEUE)[0]
    second = plan_orchestrators(WS, campaigns, task_queue=QUEUE)[0]

    assert first.workflow_id == second.workflow_id


# ==========================================================================
# The schedule Temporal actually receives
# ==========================================================================
@pytest.mark.asyncio
async def test_overlapping_runs_are_skipped_never_buffered() -> None:
    """Buffering turns "the worker was busy" into "the worker will now do all of
    it at once", which is the failure this module exists to avoid."""
    client = FakeClient()

    await schedules.install(client, plan_schedules(WS, task_queue=QUEUE))

    for _, schedule in client.created:
        assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP


@pytest.mark.asyncio
async def test_missed_occurrences_are_dropped_not_caught_up() -> None:
    """A weekend of missed verifications backfilled at once is a burst of DNS
    traffic that looks like a scanner."""
    client = FakeClient()

    await schedules.install(client, plan_schedules(WS, task_queue=QUEUE))

    for _, schedule in client.created:
        assert schedule.policy.catchup_window == schedules.CATCHUP_WINDOW
    assert schedules.CATCHUP_WINDOW < dt.timedelta(hours=1), (
        "the window must be shorter than the shortest interval, or a missed run "
        "fires alongside the next one"
    )


# ==========================================================================
# Installing
# ==========================================================================
@pytest.mark.asyncio
async def test_a_fresh_install_creates_every_schedule() -> None:
    client = FakeClient()

    jobs = plan_schedules(WS, task_queue=QUEUE)
    applied = await schedules.install(client, jobs)

    assert [a.outcome for a in applied] == [Outcome.CREATED] * len(jobs)
    assert len(client.created) == len(jobs)


@pytest.mark.asyncio
async def test_reinstalling_updates_the_spec_rather_than_failing() -> None:
    jobs = plan_schedules(WS, task_queue=QUEUE)
    client = FakeClient(existing={jobs[0].schedule_id: FakeSchedule()})

    applied = await schedules.install(client, jobs)

    assert applied[0].outcome is Outcome.UPDATED
    assert applied[1].outcome is Outcome.CREATED
    assert client.updated, "the existing schedule was not brought up to date"


@pytest.mark.asyncio
async def test_reinstalling_never_resumes_a_paused_schedule() -> None:
    """The failure this guards. The most likely moment to run the installer is a
    deploy; the second most likely is an incident, during which somebody has
    just pressed pause."""
    jobs = plan_schedules(WS, task_queue=QUEUE)
    paused = FakeSchedule(paused=True, note="paused during the bounce incident")
    client = FakeClient(existing={jobs[0].schedule_id: paused})

    applied = await schedules.install(client, jobs)

    assert applied[0].outcome is Outcome.LEFT_PAUSED
    assert "bounce incident" in applied[0].detail
    _, written = client.updated[0]
    assert written.state.paused is True, "the installer resumed a paused schedule"


@pytest.mark.asyncio
async def test_left_paused_is_not_reported_as_running() -> None:
    """A paused schedule carries the right spec and will never fire. Reporting
    the two identically lets it sit unnoticed behind a wall of green."""
    assert Applied("x", Outcome.LEFT_PAUSED).will_run is False
    assert Applied("x", Outcome.UPDATED).will_run is True
    assert Applied("x", Outcome.FAILED).will_run is False


@pytest.mark.asyncio
async def test_one_schedule_failing_does_not_abandon_the_rest() -> None:
    class Broken(FakeClient):
        async def create_schedule(self, schedule_id, schedule):
            if "weekly-report" in schedule_id:
                raise RuntimeError("namespace not found")
            await super().create_schedule(schedule_id, schedule)

    client = Broken()
    applied = await schedules.install(client, plan_schedules(WS, task_queue=QUEUE))

    assert applied[0].outcome is Outcome.FAILED
    assert applied[1].outcome is Outcome.CREATED, "the second job was abandoned"


# ==========================================================================
# Starting the loops
# ==========================================================================
@pytest.mark.asyncio
async def test_an_active_campaign_gets_its_loop_started() -> None:
    client = FakeClient()
    starts = plan_orchestrators(
        WS, [(uuid.UUID(int=1), CampaignStatus.ACTIVE)], task_queue=QUEUE
    )

    applied = await schedules.start_orchestrators(client, starts)

    assert applied[0].outcome is Outcome.CREATED
    assert len(client.started) == 1


@pytest.mark.asyncio
async def test_starting_a_loop_twice_is_not_an_error() -> None:
    """Two orchestrators on one campaign would each plan against the full daily
    budget and between them spend it twice. The collision is the mechanism
    working, not a failure to report."""
    starts = plan_orchestrators(
        WS, [(uuid.UUID(int=1), CampaignStatus.ACTIVE)], task_queue=QUEUE
    )
    client = FakeClient(running={starts[0].workflow_id})

    applied = await schedules.start_orchestrators(client, starts)

    assert applied[0].outcome is Outcome.ALREADY_RUNNING
    assert applied[0].will_run is True
    assert client.started == [], "a second loop was started on the same campaign"


# ==========================================================================
# What the operator reads
# ==========================================================================
def test_the_summary_counts_what_will_run_not_what_was_processed() -> None:
    """Those two numbers differ exactly when something is paused, which is the
    case an operator most needs to notice."""
    out = summarise(
        [
            Applied("a", Outcome.CREATED),
            Applied("b", Outcome.LEFT_PAUSED, "paused by hand"),
            Applied("c", Outcome.UPDATED),
        ]
    )

    assert "1 of 3 will run" not in out
    assert "2 of 3 will run" in out
    assert "left_paused" in out


def test_the_summary_calls_out_failures() -> None:
    out = summarise([Applied("a", Outcome.CREATED), Applied("b", Outcome.FAILED, "boom")])

    assert "1 failed" in out
    assert "boom" in out


def test_nothing_to_do_says_so() -> None:
    assert summarise([]) == "nothing to schedule"
