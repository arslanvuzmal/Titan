"""The watchdog pass: read every schedule, reinstall the stopped ones, say so.

The repair itself is three seconds of work and has been performed by hand five
times. What was missing every time was the noticing -- the fifth stall ran for
nine days while 509 validated drafts sat with nowhere to go and the estate sent
nothing for four of them.

**It must not heal silently.** A self-repair that leaves no trace converts a
visible outage into an invisible recurring fault, and the count of occurrences
is the entire argument for fixing the host underneath. So every repair files a
notification; a pass that finds nothing files none, because this runs four
times an hour and a watchdog that reports its own good health into the operator
queue is how the last 363 notifications went unread.

This is *not* :mod:`titan.autonomy.supervisor` work. It repairs the mechanism
that runs the repairs, and nothing else -- no campaign is paused, no sender
retired, no policy moved. Deciding that a schedule which should be advancing
is not is a judgement about plumbing, and it is the only judgement here.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from temporalio import activity

from titan.db.session import workspace_unit_of_work
from titan.notify.operator import NotificationKind, record_notification
from titan.workflows.schedule_health import HealResult, heal_wedged_schedules
from titan.workflows.types import HealSchedulesInput

logger = logging.getLogger(__name__)


async def heal_schedules_for(
    client: Any,
    *,
    workspace_id: uuid.UUID,
    task_queue: str,
    now: dt.datetime,
) -> HealResult:
    """Check the estate, repair what has stopped, and file what was repaired.

    The client is a parameter rather than a connection made in here so the two
    behaviours worth pinning -- that a repair is reported and that a quiet pass
    is not -- can be tested against a real database without a Temporal server.
    """
    result = await heal_wedged_schedules(
        client, workspace_id=workspace_id, task_queue=task_queue, now=now
    )
    if not result.healed and not result.attempted:
        return result

    async with workspace_unit_of_work(workspace_id) as session:
        for assessment in result.attempted:
            # A repair that did not land is the more urgent of the two, and it
            # gets its own wording. Reporting it as a restart would close the
            # ticket on a schedule that is still dead -- and the daily dedupe
            # would then suppress every later warning about it.
            name = assessment.schedule_id.split("::", 1)[0].removeprefix("titan-")
            await record_notification(
                session,
                workspace_id=workspace_id,
                kind=NotificationKind.PIPELINE_ALERT,
                title=f"The {name} schedule is stopped and would not restart",
                description=(
                    f"{assessment.reason}. "
                    "It was reinstalled in place -- the repair that has worked "
                    "before -- and did not pick its clock back up. This needs a "
                    "hand: delete the schedule and run `titan schedules` to "
                    "recreate it. "
                    "Until then the job is not running at all."
                ),
                dedupe_key=(
                    f"schedule-unfixable:{assessment.schedule_id}:"
                    f"{now.date().isoformat()}"
                ),
                priority=90,
                now=now,
            )
        for assessment in result.healed:
            # The job name, not the full schedule id: the id carries a
            # workspace uuid that makes every title unreadable at a glance,
            # and the title is the part anybody actually sees.
            name = assessment.schedule_id.split("::", 1)[0].removeprefix("titan-")
            await record_notification(
                session,
                workspace_id=workspace_id,
                kind=NotificationKind.PIPELINE_ALERT,
                title=f"The {name} schedule had stopped and was restarted",
                description=(
                    f"{assessment.reason}.\n\n"
                    "It was not paused and reported no error -- a wedged Temporal "
                    "schedule looks healthy from every angle except its own clock. "
                    "It has been reinstalled in place, which is the same repair a "
                    "deploy performs.\n\n"
                    "Worth counting rather than dismissing: this failure follows "
                    "the machine being asleep, and a host that stays on does not "
                    "produce it."
                ),
                # One a day per schedule. A stall persists until the repair
                # lands, and the pass that heals it and the pass that finds it
                # still behind can both be right -- at four passes an hour that
                # is ninety-six identical rows before anybody looks.
                dedupe_key=f"schedule-stalled:{assessment.schedule_id}:{now.date().isoformat()}",
                now=now,
            )
    return result


@activity.defn(name="heal_schedules")
async def heal_schedules(request: HealSchedulesInput) -> HealResult:
    """Activity wrapper: connect, then do the pass above."""
    from titan.workers.temporal_worker import connect

    client = await connect()
    return await heal_schedules_for(
        client,
        workspace_id=uuid.UUID(request.workspace_id),
        task_queue=activity.info().task_queue,
        now=dt.datetime.now(dt.UTC),
    )


ALL_SCHEDULE_HEALING_ACTIVITIES = [heal_schedules]

__all__ = [
    "ALL_SCHEDULE_HEALING_ACTIVITIES",
    "heal_schedules",
    "heal_schedules_for",
]
