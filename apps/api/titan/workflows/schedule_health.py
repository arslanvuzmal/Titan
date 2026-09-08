"""Whether a schedule has stopped advancing, decided without talking to anyone.

A wedged Temporal schedule is the quietest failure this system produces. It
reports ``Paused: false``, carries no ``invalidScheduleError``, and its handle
answers ``describe`` perfectly well -- the only thing wrong is that every
firing time it predicts is already in the past, because its internal clock
stopped. Nothing raises, nothing retries, and nothing is written anywhere.

Housekeeping wedged this way five times. The fifth went unnoticed for nine
days, during which 509 validated drafts sat with nowhere to go and the estate
sent nothing for four of them. Each time the repair was the same three
seconds of work; each time the cost was in the noticing.

**Pure on purpose.** This module holds the judgement and nothing else: no
Temporal client, no database, no clock of its own. The caller observes, this
decides, the caller acts. That split is what makes the two dangerous cases --
healing a schedule that is merely late, and touching one a person paused --
testable without a server.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from titan.workflows.schedules import catchup_for, install, plan_schedules

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScheduleObservation:
    """What one schedule's description says, reduced to what matters here."""

    schedule_id: str
    paused: bool
    cron: str
    #: The firing times the scheduler currently predicts, soonest first. Empty
    #: when the server offered none.
    next_action_times: tuple[dt.datetime, ...]


class Verdict(StrEnum):
    HEALTHY = "healthy"
    WEDGED = "wedged"


@dataclass(frozen=True, slots=True)
class Assessment:
    """The verdict, and how far behind the schedule's clock had fallen."""

    schedule_id: str
    verdict: Verdict
    behind: dt.timedelta
    reason: str


def assess(observation: ScheduleObservation, *, now: dt.datetime) -> Assessment:
    """Decide whether this schedule has stopped advancing.

    Two refusals come before the arithmetic, and both matter more than it.

    A **paused** schedule's clock stops -- that is what pausing means -- so it
    presents exactly like a wedged one. It is also the thing most likely to be
    under a watchdog's nose during an incident, five minutes after somebody
    hit pause deliberately. Never ours to touch.

    A schedule predicting **no firings at all** is telling us nothing, and
    absence of evidence is not evidence of a fault. Reinstalling on it would
    be acting on a guess.
    """
    if observation.paused:
        return Assessment(
            observation.schedule_id,
            Verdict.HEALTHY,
            dt.timedelta(0),
            "paused by hand; not ours to restart",
        )
    if not observation.next_action_times:
        return Assessment(
            observation.schedule_id,
            Verdict.HEALTHY,
            dt.timedelta(0),
            "no firing times offered; nothing to judge",
        )

    # The *soonest* prediction, against the job's own catch-up window.
    #
    # The first version of this read the furthest, reasoning that a stopped
    # clock has every prediction behind it. That is true only once it has been
    # stopped for longer than the list spans -- ten hours, for an hourly job --
    # and the only wedged schedule available to model it on had been frozen a
    # week. Housekeeping stopped again at 14:17 and at 17:03 still had nine of
    # its ten firings in the future; the watchdog called it healthy through
    # thirteen consecutive cycles and healed nothing.
    #
    # A schedule that is running has its next firing ahead of it. One whose
    # clock has stopped watches that firing recede, and the catch-up window is
    # exactly the grace a late job is entitled to -- thirty minutes for an
    # hourly job, twenty-three hours for a daily one -- so it separates "a
    # minute behind" from "not coming" without a number invented here.
    tolerance = catchup_for(observation.cron)
    soonest = min(observation.next_action_times)
    behind = now - soonest
    if behind > tolerance:
        return Assessment(
            observation.schedule_id,
            Verdict.WEDGED,
            behind,
            f"its next firing was due {_readable(behind)} ago and has not "
            f"happened; the schedule's clock has stopped advancing",
        )
    return Assessment(observation.schedule_id, Verdict.HEALTHY, behind, "advancing")


def _readable(delta: dt.timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours >= 48:
        return f"{hours / 24:.0f} days"
    if hours >= 1:
        return f"{hours:.0f} hours"
    return f"{delta.total_seconds() / 60:.0f} minutes"


@dataclass(frozen=True, slots=True)
class HealResult:
    """What one pass looked at, and what it put back on the rails."""

    checked: int
    #: Schedules that were wedged and are now advancing again.
    healed: tuple[Assessment, ...]
    #: Schedules the server could not describe. Counted rather than raised:
    #: one unreadable schedule must not stop the others being checked.
    unreadable: int
    #: Schedules that were wedged, were reinstalled, and did *not* come back.
    #: Kept apart from `healed` because updating a schedule's spec in place
    #: unwedged housekeeping at 13:24 on 7 September and did nothing at all to
    #: it at 17:15 -- the same call on the same schedule. A watchdog that
    #: reports a repair it did not achieve turns a visible stall into a closed
    #: ticket, so the two outcomes are counted separately and only one of them
    #: is called healing.
    #:
    #: **Defaulted, and it has to be.** This type crosses a workflow boundary
    #: and an always-on workflow replays its entire history on every worker
    #: restart. Added as a required field on 7 September, it killed the
    #: SupervisorWorkflow outright: the history held results in the old shape,
    #: every replay failed with `attempted Field required`, and a failed
    #: workflow task retries for ever. The watchdog was dead for fifteen hours
    #: -- housekeeping wedged with nothing watching it, 446 drafts went
    #: unswept, and the estate sent nothing the following morning.
    attempted: tuple[Assessment, ...] = ()


async def heal_wedged_schedules(
    client: Any, *, workspace_id: uuid.UUID, task_queue: str, now: dt.datetime
) -> HealResult:
    """Look at every schedule this workspace owns and reinstall the stopped ones.

    Scoped to the jobs the planner knows about rather than to everything on
    the server. A schedule this deployment did not create is not ours to
    judge, and reinstalling one would rewrite it into a shape it never asked
    for.

    The repair is ``install`` -- the same call a deploy makes, which updates
    the spec in place and never resumes a paused schedule. Nothing here needs
    delete-and-recreate, and reaching for it would turn a watchdog into
    something that can lose a schedule's history.
    """
    jobs = plan_schedules(workspace_id, task_queue=task_queue)
    healed: list[Assessment] = []
    attempted: list[Assessment] = []
    unreadable = 0

    for job in jobs:
        try:
            description = await client.get_schedule_handle(job.schedule_id).describe()
            observation = ScheduleObservation(
                schedule_id=job.schedule_id,
                paused=description.schedule.state.paused,
                cron=job.cron,
                next_action_times=tuple(description.info.next_action_times),
            )
        except Exception:
            # Never fatal. A watchdog is worth having only if it is the most
            # reliable thing running, and one schedule the server cannot
            # describe is exactly the moment the other six matter most.
            logger.warning(
                "could not read schedule",
                extra={"schedule_id": job.schedule_id},
                exc_info=True,
            )
            unreadable += 1
            continue

        assessment = assess(observation, now=now)
        if assessment.verdict is not Verdict.WEDGED:
            continue

        logger.warning(
            "schedule has stopped advancing; reinstalling",
            extra={"schedule_id": job.schedule_id, "reason": assessment.reason},
        )
        await install(client, [job])

        # Read it back rather than trusting the write. The installer returns
        # success for an update the server accepted, which is not the same
        # claim as "the scheduler is running again", and the difference is the
        # whole value of this pass.
        if await _is_advancing(client, job, now=now):
            healed.append(assessment)
        else:
            logger.error(
                "schedule did not restart after being reinstalled",
                extra={"schedule_id": job.schedule_id},
            )
            attempted.append(assessment)

    return HealResult(len(jobs), tuple(healed), unreadable, tuple(attempted))


async def _is_advancing(client: Any, job: Any, *, now: dt.datetime) -> bool:
    """Whether the schedule has picked its clock back up since the repair."""
    try:
        description = await client.get_schedule_handle(job.schedule_id).describe()
    except Exception:
        return False
    observation = ScheduleObservation(
        schedule_id=job.schedule_id,
        paused=description.schedule.state.paused,
        cron=job.cron,
        next_action_times=tuple(description.info.next_action_times),
    )
    return assess(observation, now=now).verdict is not Verdict.WEDGED
