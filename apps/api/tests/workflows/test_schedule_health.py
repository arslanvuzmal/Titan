"""Deciding whether a schedule has stopped advancing.

A Temporal schedule that wedges does not report an error. It reports
``Paused: false``, no ``invalidScheduleError``, and a list of upcoming firing
times that are all in the past -- its internal clock simply stopped. That is a
harder thing to notice than a crash, and on this workspace it went unnoticed
five separate times; the fifth cost nine days and 509 unqueued drafts.

These tests fix the signature of that state, and -- more importantly -- the two
ways a watchdog for it does damage: healing a schedule that is merely a few
minutes late, and resuming one a person paused on purpose.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from titan.workflows.schedule_health import (
    ScheduleObservation,
    Verdict,
    assess,
    heal_wedged_schedules,
)

WS = uuid.UUID("11111111-1111-1111-1111-111111111111")
QUEUE = "titan-research"
NOW = dt.datetime(2026, 9, 7, 13, 0, tzinfo=dt.UTC)
HOURLY = "17 * * * *"
DAILY = "10 6 * * *"


def observation(**overrides) -> ScheduleObservation:
    """An hourly schedule, healthy, with ten firings ahead of it."""
    base = {
        "schedule_id": "titan-housekeeping::ws",
        "paused": False,
        "cron": HOURLY,
        "next_action_times": tuple(
            NOW + dt.timedelta(hours=n, minutes=17) for n in range(10)
        ),
    }
    base.update(overrides)
    return ScheduleObservation(**base)


# ==========================================================================
# The state that went unnoticed five times
# ==========================================================================
def test_a_schedule_whose_every_next_firing_is_in_the_past_is_wedged() -> None:
    """The observed signature, taken from the live schedule on 7 September.

    Not paused, no error reported, and ``futureActionTimes`` frozen a week
    back: ``2026-08-31T07:17Z`` through ``16:17Z`` while the clock said
    7 September. Nothing else in the description said anything was wrong.
    """
    frozen = observation(
        next_action_times=tuple(
            dt.datetime(2026, 8, 31, 7 + n, 17, tzinfo=dt.UTC) for n in range(10)
        )
    )

    assessment = assess(frozen, now=NOW)

    assert assessment.verdict is Verdict.WEDGED
    assert assessment.behind > dt.timedelta(days=6)


# ==========================================================================
# The two ways a watchdog does damage
# ==========================================================================
def test_a_schedule_running_a_few_minutes_late_is_not_wedged() -> None:
    """Planted violation: compare against ``now`` with no tolerance.

    The hourly jobs on this workspace are routinely a minute or two late --
    the worker restarts, a deploy lands, the machine is busy. A watchdog that
    called that wedged would reinstall all seven schedules on every pass, for
    ever, and the reinstall is not free: it rewrites the spec of a schedule
    that is working.
    """
    late = observation(
        next_action_times=(NOW - dt.timedelta(minutes=3),)
    )

    assert assess(late, now=NOW).verdict is Verdict.HEALTHY


def test_a_paused_schedule_is_never_wedged_however_far_behind() -> None:
    """Planted violation: drop the paused check and this fails.

    A paused schedule's clock stops -- that is what pausing means -- so it
    presents exactly like a wedged one and is the single most likely thing to
    be looking at during an incident. The installer already refuses to resume
    a paused schedule; the watchdog must not hand it work that makes it try.
    """
    paused_for_a_month = observation(
        paused=True,
        next_action_times=(NOW - dt.timedelta(days=30),),
    )

    assert assess(paused_for_a_month, now=NOW).verdict is Verdict.HEALTHY


def test_a_schedule_predicting_nothing_is_left_alone() -> None:
    """No firing times is no evidence, and no evidence is not a fault.

    The server offers none for a schedule with no further occurrences, and
    reading absence as breakage would reinstall on a guess.
    """
    silent = observation(next_action_times=())

    assert assess(silent, now=NOW).verdict is Verdict.HEALTHY


# ==========================================================================
# The tolerance comes from the job, not from a number typed here
# ==========================================================================
def test_a_daily_job_gets_the_slack_a_daily_job_needs() -> None:
    """Planted violation: use one tolerance for every schedule.

    The ramp fires once at 06:10 and its catch-up window is 23 hours, because
    a machine that sleeps through 06:10 should still run it that day. Judging
    it on the hourly tolerance would call it wedged every morning the laptop
    was shut, and reinstalling it would not have been the repair.
    """
    overnight = observation(
        cron=DAILY,
        next_action_times=(NOW - dt.timedelta(hours=8),),
    )

    assert assess(overnight, now=NOW).verdict is Verdict.HEALTHY
    assert assess(overnight, now=NOW + dt.timedelta(days=1)).verdict is Verdict.WEDGED


def test_the_verdict_says_how_far_behind_in_words_a_person_can_act_on() -> None:
    """The reason is written into a task nobody can query afterwards, so it
    has to carry the number rather than point at one."""
    frozen = observation(next_action_times=(NOW - dt.timedelta(days=9),))

    assert "9 days" in assess(frozen, now=NOW).reason


def test_the_reason_does_not_claim_more_than_was_actually_observed() -> None:
    """Planted violation: keep the wording written for the old rule.

    The first version judged the furthest firing and said so -- "every
    predicted firing is in the past". Reading the soonest instead made that
    sentence false without making any test fail, and it is the sentence the
    operator reads in the alert. A watchdog that reports a stall accurately
    and then describes it wrongly has traded one silent problem for a loud
    untrue one.
    """
    # The live shape: stopped at 14:17, read at 17:03, nine firings still
    # ahead of it.
    stopped = observation(
        next_action_times=tuple(
            dt.datetime(2026, 9, 7, 14 + n, 17, tzinfo=dt.UTC) for n in range(10)
        )
    )

    reason = assess(stopped, now=dt.datetime(2026, 9, 7, 17, 3, tzinfo=dt.UTC)).reason

    assert "every" not in reason.lower()
    assert "next" in reason.lower() and "3 hours" in reason


# ==========================================================================
# Healing: observing every schedule we own, and reinstalling only the wedged
# ==========================================================================
class FakeState:
    def __init__(self, paused: bool, note: str = "") -> None:
        self.paused = paused
        self.note = note


class FakeSchedule:
    def __init__(self, paused: bool = False, note: str = "") -> None:
        self.state = FakeState(paused, note)


class FakeInfo:
    def __init__(self, next_action_times) -> None:
        self.next_action_times = list(next_action_times)


class FakeDescription:
    def __init__(self, schedule: FakeSchedule, info: FakeInfo) -> None:
        self.schedule = schedule
        self.info = info


class FakeUpdateInput:
    def __init__(self, description: FakeDescription) -> None:
        self.description = description


class FakeHandle:
    def __init__(self, client, schedule_id: str) -> None:
        self._client = client
        self._id = schedule_id

    async def describe(self) -> FakeDescription:
        if self._id not in self._client.existing:
            raise KeyError(self._id)
        return self._client.existing[self._id]

    async def update(self, mutate) -> None:
        current = self._client.existing[self._id]
        mutate(FakeUpdateInput(current))
        self._client.updated.append(self._id)
        # A successful update revives the scheduler, which is what the server
        # does when the repair takes. `StubbornClient` below models the case
        # observed live where it does not.
        if self._client.repair_works:
            ahead = tuple(NOW + dt.timedelta(hours=n) for n in range(1, 11))
            self._client.existing[self._id] = FakeDescription(
                current.schedule, FakeInfo(ahead)
            )


class FakeClient:
    """Enough Temporal to observe schedules and reinstall the wedged ones."""

    repair_works = True

    def __init__(self, existing: dict[str, FakeDescription]) -> None:
        self.existing = existing
        self.updated: list[str] = []
        self.created: list[str] = []

    async def create_schedule(self, schedule_id: str, schedule):
        from temporalio.client import ScheduleAlreadyRunningError

        if schedule_id in self.existing:
            raise ScheduleAlreadyRunningError()
        self.created.append(schedule_id)

    def get_schedule_handle(self, schedule_id: str) -> FakeHandle:
        return FakeHandle(self, schedule_id)


def described(cron_times, *, paused: bool = False) -> FakeDescription:
    return FakeDescription(FakeSchedule(paused=paused), FakeInfo(cron_times))


def estate(workspace: uuid.UUID = WS, **overrides) -> dict[str, FakeDescription]:
    """Every schedule a workspace owns, all healthy unless overridden."""
    ahead = tuple(NOW + dt.timedelta(hours=n) for n in range(1, 11))
    live = {job.schedule_id: described(ahead) for job in _jobs(workspace)}
    for schedule_id, description in overrides.items():
        live[schedule_id] = description
    return live


def _jobs(workspace: uuid.UUID = WS):
    from titan.workflows.schedules import plan_schedules

    return plan_schedules(workspace, task_queue=QUEUE)


@pytest.mark.asyncio
async def test_only_the_wedged_schedule_is_reinstalled() -> None:
    """Planted violation: reinstall everything on every pass.

    Six of the seven are advancing. Rewriting their specs to repair the
    seventh is churn on working machinery, and it is how a watchdog turns
    into the thing that needs watching.
    """
    housekeeping = f"titan-housekeeping::{WS}"
    live = estate(**{housekeeping: described((NOW - dt.timedelta(days=7),))})
    client = FakeClient(live)

    result = await heal_wedged_schedules(client, workspace_id=WS, task_queue=QUEUE, now=NOW)

    assert client.updated == [housekeeping]
    assert [h.schedule_id for h in result.healed] == [housekeeping]


@pytest.mark.asyncio
async def test_an_estate_that_is_advancing_is_left_entirely_alone() -> None:
    """The normal case, and the one that runs every fifteen minutes for ever."""
    client = FakeClient(estate())

    result = await heal_wedged_schedules(client, workspace_id=WS, task_queue=QUEUE, now=NOW)

    assert client.updated == []
    assert result.healed == ()
    assert result.checked == len(_jobs())


@pytest.mark.asyncio
async def test_a_paused_schedule_is_not_reinstalled_however_far_behind() -> None:
    """Planted violation: drop the paused check and this fails.

    The end-to-end guard for the same rule the predicate holds. Worth having
    twice: the installer would leave it paused anyway, but a watchdog that
    keeps rewriting a schedule somebody paused during an incident is noise at
    the exact moment noise is most expensive.
    """
    ramp = f"titan-mailbox-ramp::{WS}"
    live = estate(**{ramp: described((NOW - dt.timedelta(days=30),), paused=True)})
    client = FakeClient(live)

    result = await heal_wedged_schedules(client, workspace_id=WS, task_queue=QUEUE, now=NOW)

    assert client.updated == []
    assert result.healed == ()


@pytest.mark.asyncio
async def test_one_unreadable_schedule_does_not_abandon_the_rest() -> None:
    """Planted violation: let a describe() failure escape.

    A watchdog is worth having only if it is the most reliable thing running.
    One schedule the server cannot describe must not stop the other six being
    checked -- that failure mode would have hidden this very bug.
    """
    housekeeping = f"titan-housekeeping::{WS}"
    live = estate(**{housekeeping: described((NOW - dt.timedelta(days=7),))})
    del live[f"titan-opt-outs::{WS}"]
    client = FakeClient(live)

    result = await heal_wedged_schedules(client, workspace_id=WS, task_queue=QUEUE, now=NOW)

    assert client.updated == [housekeeping]
    assert result.unreadable == 1


# ==========================================================================
# The loop that carries it, and why it is not itself a schedule
# ==========================================================================
def test_the_supervisor_is_not_installed_as_a_schedule() -> None:
    """Planted violation: add the supervisor to ``plan_schedules``.

    The whole point is that it survives the failure it repairs. A watchdog
    installed as a schedule is a watchdog that wedges the same way housekeeping
    did, on the same afternoon, and then nothing is watching anything.
    """
    planned = {job.workflow for job in _jobs()}

    assert "SupervisorWorkflow" not in planned


def test_the_supervisor_is_started_as_an_always_on_loop() -> None:
    """It belongs with the campaign orchestrators, which is the mechanism that
    demonstrably held: they cycled 232 times through the same window in which
    housekeeping's clock stopped and stayed stopped."""
    from titan.workflows.schedules import plan_supervisor

    start = plan_supervisor(WS, task_queue=QUEUE)

    assert start.workflow_id == f"supervisor::{WS}"
    assert start.task_queue == QUEUE


def test_the_supervisors_id_is_stable_so_a_second_one_cannot_start() -> None:
    """Two watchdogs would reinstall the same schedule twice and file the
    notification twice. The id is the guard, exactly as it is for campaigns."""
    from titan.workflows.schedules import plan_supervisor

    assert plan_supervisor(WS, task_queue=QUEUE).workflow_id == (
        plan_supervisor(WS, task_queue=QUEUE).workflow_id
    )


def test_the_daily_report_is_installed_as_a_schedule() -> None:
    """It is a cron job, unlike the watchdog: nothing depends on it surviving
    a wedged scheduler, and W25 now watches it like the other seven."""
    planned = {job.workflow for job in _jobs()}

    assert "DailyReportWorkflow" in planned


def test_the_daily_report_is_checked_hourly_not_daily() -> None:
    """Planted violation: schedule it once a day.

    "After sending all quota" is a condition, not a clock -- the send windows
    run from Sydney to Vancouver, so no fixed hour is reliably after the last
    send. A daily cron would report a day that had not finished, or finish
    hours after it did.
    """
    report = next(job for job in _jobs() if job.workflow == "DailyReportWorkflow")

    assert report.cron.split()[1] == "*", "the hour field must not be fixed"


def test_a_schedule_that_stopped_hours_ago_is_wedged_before_the_list_runs_out() -> None:
    """Planted violation: judge on the furthest predicted firing.

    Taken from the live schedule at 17:03 on 7 September, two hours after it
    stopped: soonest 14:17, furthest 23:17. Nine of the ten predictions were
    still in the future, so a rule reading the furthest called it healthy and
    the watchdog let it sit -- 13 supervisor cycles, nothing healed.

    The error was in the first version of this file, not in the watchdog: the
    only wedged schedule available to model was one frozen a *week* back,
    where every prediction had aged into the past, and the test encoded that
    accident as though it were the signature. A clock that stopped two hours
    ago looks identical to a healthy one under `max`, and it takes ten hours
    of an hourly schedule being dead before it does not.

    The soonest is the honest reading: a schedule that is running has its next
    firing ahead of it, and one whose clock has stopped watches that firing
    recede.
    """
    stopped_at_1417 = observation(
        next_action_times=tuple(
            dt.datetime(2026, 9, 7, 14 + n, 17, tzinfo=dt.UTC) for n in range(10)
        )
    )
    now = dt.datetime(2026, 9, 7, 17, 3, tzinfo=dt.UTC)

    assessment = assess(stopped_at_1417, now=now)

    assert assessment.verdict is Verdict.WEDGED
    assert assessment.behind > dt.timedelta(hours=2)


@pytest.mark.asyncio
async def test_a_repair_that_did_not_take_is_not_reported_as_healed() -> None:
    """Planted violation: trust the installer and report success blindly.

    Observed live on 7 September. Updating a wedged schedule's spec in place
    unwedged it at 13:24 and did nothing at all at 17:15 -- same call, same
    schedule, same code path. The pass still logged "reinstalling" and filed
    an alert saying the schedule "had stopped and was restarted", which was
    false, and the daily dedupe then suppressed any further alert.

    A watchdog reporting a repair it did not achieve is worse than one that
    stays quiet: it converts a visible stall into a closed ticket.
    """
    housekeeping = f"titan-housekeeping::{WS}"
    stuck = described((NOW - dt.timedelta(hours=3),))
    live = estate(**{housekeeping: stuck})

    class StubbornClient(FakeClient):
        """Accepts the update and changes nothing -- the observed behaviour."""

        repair_works = False

    client = StubbornClient(live)

    result = await heal_wedged_schedules(
        client, workspace_id=WS, task_queue=QUEUE, now=NOW
    )

    assert client.updated == [housekeeping], "it must still try"
    assert result.healed == (), "but it must not claim a repair that did not land"
    assert [a.schedule_id for a in result.attempted] == [housekeeping]


def test_a_result_from_before_the_field_existed_still_deserialises() -> None:
    """Planted violation: make `attempted` a required field.

    This is not hypothetical. `attempted` was added as required on 7 September
    and the running SupervisorWorkflow died on the spot: its history holds
    activity results in the old shape, and every replay after the deploy
    failed with `attempted Field required`. A failed workflow task retries for
    ever, so the watchdog was dead for fifteen hours -- during which
    housekeeping wedged with nothing watching it, nothing swept 446 stranded
    drafts, and the estate sent nothing the next morning.

    An always-on workflow replays its whole history on every worker restart,
    so any field added to an activity's result type must have a default. The
    repository already knew this -- the research workflow guards an added
    activity call behind `workflow.patched()` for the same reason.
    """
    from titan.workflows.schedule_health import HealResult

    before = {"checked": 7, "healed": [], "unreadable": 0}

    revived = HealResult(**before)

    assert revived.attempted == ()
    assert revived.checked == 7
