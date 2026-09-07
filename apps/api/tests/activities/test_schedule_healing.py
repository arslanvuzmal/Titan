"""The watchdog pass, and its obligation to say what it did.

Runs against a real PostgreSQL, because the point of the test is the row it
writes: a self-repair that leaves no trace trades one silent failure for
another, and this stall has now happened five times without anybody being
told once.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from titan.activities.schedule_healing import heal_schedules_for
from titan.db.models.ops import Task

from tests.workflows.test_schedule_health import (
    FakeClient,
    described,
    estate,
)

pytestmark = pytest.mark.asyncio

QUEUE = "titan-research"
NOW = dt.datetime(2026, 9, 7, 13, 0, tzinfo=dt.UTC)


def wedged_estate(workspace: uuid.UUID) -> tuple[FakeClient, str]:
    """An estate with housekeeping's clock stopped a week ago."""
    housekeeping = f"titan-housekeeping::{workspace}"
    live = estate(workspace=workspace)
    live[housekeeping] = described((NOW - dt.timedelta(days=7),))
    return FakeClient(live), housekeeping


async def tasks_for(session, workspace: uuid.UUID) -> list[Task]:
    rows = await session.execute(
        select(Task).where(Task.workspace_id == workspace, Task.kind == "pipeline_alert")
    )
    return list(rows.scalars())


async def test_healing_a_schedule_tells_somebody(db_session, workspace) -> None:
    """Planted violation: repair the schedule and record nothing.

    This is the whole reason the watchdog is not simply a cron that reinstalls
    everything every hour. A stall that self-repairs silently is a recurring
    fault nobody can count, and the count is the argument for fixing the host
    it keeps happening on.
    """
    client, housekeeping = wedged_estate(workspace)

    result = await heal_schedules_for(
        client, workspace_id=workspace, task_queue=QUEUE, now=NOW
    )

    assert [a.schedule_id for a in result.healed] == [housekeeping]
    tasks = await tasks_for(db_session, workspace)
    assert len(tasks) == 1
    assert "housekeeping" in tasks[0].title
    assert "7 days" in (tasks[0].description or "")


async def test_a_healthy_estate_files_nothing(db_session, workspace) -> None:
    """Planted violation: file a task every pass.

    This runs every fifteen minutes. A watchdog that reported "checked seven,
    all fine" into the same queue as real alarms would bury them at four rows
    an hour -- which is exactly how 363 notifications went unread the first
    time.
    """
    client = FakeClient(estate(workspace=workspace))

    await heal_schedules_for(client, workspace_id=workspace, task_queue=QUEUE, now=NOW)

    assert await tasks_for(db_session, workspace) == []


async def test_the_same_stall_is_reported_once_a_day_not_once_a_pass(
    db_session, workspace
) -> None:
    """Planted violation: use a dedupe key that varies per pass.

    A wedged schedule stays wedged until the repair lands, and the repair is
    not always instant -- the pass that heals it and the pass that finds it
    still behind can both be right. Ninety-six identical rows a day is the
    failure mode the campaign-stall notice already guards against this way.
    """
    client, _ = wedged_estate(workspace)

    await heal_schedules_for(client, workspace_id=workspace, task_queue=QUEUE, now=NOW)
    await heal_schedules_for(
        client, workspace_id=workspace, task_queue=QUEUE, now=NOW + dt.timedelta(hours=1)
    )

    assert len(await tasks_for(db_session, workspace)) == 1
