"""Closing alarms without closing prospects.

Runs against a real PostgreSQL: the behaviour is one conditional UPDATE and a
mock would only prove the SQL string had not changed.

Most of these guard one direction. Leaving an alarm open costs a cluttered
queue; closing a reply costs a person who was waiting to hear back, and that is
the error this sweep could make that cannot be taken back.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text
from titan.notify.operator import NotificationKind, record_notification
from titan.notify.task_expiry import (
    EXPIRABLE,
    EXPIRED,
    STALE_AFTER_DAYS,
    expire_stale_alarms,
)

pytestmark = pytest.mark.asyncio

NOW = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)


async def _task(session, workspace, *, kind: str, days_old: int) -> uuid.UUID:
    """Build the row through the production insert path.

    Not raw SQL: ``tasks`` carries several NOT NULL columns a hand-written
    INSERT has to keep in step with the model, and a fixture that drifts from
    what the system actually writes tests a row shape nobody produces.
    """
    note = await record_notification(
        session,
        workspace_id=workspace,
        kind=NotificationKind(kind),
        title=f"{kind} from {days_old} days ago",
        dedupe_key=f"{kind}:{uuid.uuid4()}",
    )
    assert note is not None
    # Aged afterwards. ``record_notification`` takes a ``now`` but spends it on
    # ``due_at`` only -- ``created_at`` comes from the column default -- so a
    # backdated call still produces a row created this second.
    await session.execute(
        text("UPDATE tasks SET created_at = :created WHERE id = :id"),
        {"created": NOW - dt.timedelta(days=days_old), "id": note.task_id},
    )
    await session.commit()
    return note.task_id


async def _status(session, task_id: uuid.UUID) -> str:
    return await session.scalar(
        text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}
    )


@pytest.mark.parametrize("kind", sorted(EXPIRABLE))
async def test_a_stale_machine_alarm_is_closed(db_session, workspace, kind) -> None:
    """Planted violation: a queue with no exit.

    Measured before this existed: 648 tasks, every one open, the oldest from
    16 August, and 607 of them the same campaign_stalled alarm. Nothing in the
    codebase had ever written a status other than "open".
    """
    task_id = await _task(db_session, workspace, kind=kind, days_old=STALE_AFTER_DAYS + 1)

    report = await expire_stale_alarms(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()

    assert report.expired >= 1
    assert await _status(db_session, task_id) == EXPIRED


@pytest.mark.parametrize(
    "kind",
    [
        NotificationKind.CLIENT_AGREED.value,
        NotificationKind.REPLY_NEEDS_READING.value,
        NotificationKind.REPLY_DECLINED.value,
        NotificationKind.APPROVAL_NEEDED.value,
        NotificationKind.CONVERSATION_ACTIVE.value,
    ],
)
async def test_a_person_is_never_closed_by_a_sweep(db_session, workspace, kind) -> None:
    """Planted violation: expire on age alone, ignoring kind.

    A reply is a statement about somebody waiting to hear back. It does not
    re-fire, and it does not stop being true because a week went by. Sixteen of
    these were sitting unread when this was written -- closing them would have
    made the backlog invisible instead of smaller.
    """
    task_id = await _task(db_session, workspace, kind=kind, days_old=365)

    await expire_stale_alarms(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()

    assert await _status(db_session, task_id) == "open"


async def test_a_recent_alarm_is_left_alone(db_session, workspace) -> None:
    """The window is a window. Today's stall is today's news."""
    task_id = await _task(
        db_session,
        workspace,
        kind=NotificationKind.CAMPAIGN_STALLED.value,
        days_old=STALE_AFTER_DAYS - 1,
    )

    await expire_stale_alarms(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()

    assert await _status(db_session, task_id) == "open"


async def test_an_already_closed_alarm_is_not_rewritten(db_session, workspace) -> None:
    """It runs hourly. A pass that re-touched every closed row would rewrite
    thousands of rows an hour to change nothing."""
    task_id = await _task(
        db_session,
        workspace,
        kind=NotificationKind.CAMPAIGN_STALLED.value,
        days_old=STALE_AFTER_DAYS + 5,
    )
    await expire_stale_alarms(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()

    second = await expire_stale_alarms(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()

    assert second.expired == 0, "a second pass has nothing left to close"
    assert await _status(db_session, task_id) == EXPIRED


async def test_the_backlog_is_reported_so_it_can_be_watched(
    db_session, workspace
) -> None:
    """It runs hourly against a queue of hundreds. A pass reporting only what
    it closed would leave no way to see the real backlog -- the human one."""
    await _task(
        db_session,
        workspace,
        kind=NotificationKind.CAMPAIGN_STALLED.value,
        days_old=STALE_AFTER_DAYS + 1,
    )
    await _task(
        db_session,
        workspace,
        kind=NotificationKind.REPLY_NEEDS_READING.value,
        days_old=30,
    )

    report = await expire_stale_alarms(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()

    assert report.expired >= 1
    assert report.still_open >= 1, "the reply is still waiting and must still count"


async def test_another_workspace_is_not_touched(db_session, workspace) -> None:
    """Raw SQL carries none of the ORM's workspace guard, so the predicate has
    to be written by hand -- and a sweep that crossed workspaces would close
    somebody else's alarms."""
    from titan.db.models import Workspace

    other = uuid.uuid4()
    db_session.add(
        Workspace(id=other, slug=f"other-{other.hex[:8]}", name="Other workspace")
    )
    await db_session.commit()
    theirs = await _task(
        db_session,
        other,
        kind=NotificationKind.CAMPAIGN_STALLED.value,
        days_old=STALE_AFTER_DAYS + 10,
    )

    await expire_stale_alarms(db_session, workspace_id=workspace, now=NOW)
    await db_session.commit()

    assert await _status(db_session, theirs) == "open"
