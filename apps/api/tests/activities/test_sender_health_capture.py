"""Giving each mailbox a history instead of only a present.

`sender_health.py` can already tell an improving sender from a degrading one --
`Trend` is one of its types, and it is documented as needing two points.
`sender_health_snapshots` has held the shape for that since 15 August and never
held a row, so the trend had nothing to compare and every judgement about a
mailbox came from whatever the last few hours looked like.

The properties worth asserting are that a day is a day (running twice does not
manufacture a second point), and that the ordering against the other daily jobs
is what makes the snapshot worth taking at all.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select
from titan.activities.sender_health import capture_sender_health
from titan.db.models import SenderHealthSnapshot
from titan.db.session import get_sessionmaker
from titan.workflows.types import CaptureSenderHealthInput

from tests.delivery.conftest import build_sendable


async def _capture(workspace) -> object:
    return await capture_sender_health(
        CaptureSenderHealthInput(workspace_id=str(workspace))
    )


async def _rows(workspace) -> int:
    async with get_sessionmaker()() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(SenderHealthSnapshot)
                .where(SenderHealthSnapshot.workspace_id == workspace)
            )
            or 0
        )


async def test_a_sender_gets_a_point_today(db_session, workspace) -> None:
    """The table existed and had never held a row."""
    await build_sendable(db_session, workspace, suffix="health1")
    await db_session.commit()

    result = await _capture(workspace)

    assert result.unavailable is None
    assert result.captured >= 1
    assert await _rows(workspace) >= 1


async def test_running_twice_in_a_day_refreshes_rather_than_appends(
    db_session, workspace
) -> None:
    """A day is a day.

    A retry after a partial run must not manufacture a trend out of two points
    for the same date -- that would read as movement where there was none.
    """
    await build_sendable(db_session, workspace, suffix="health2")
    await db_session.commit()

    await _capture(workspace)
    first = await _rows(workspace)
    await _capture(workspace)
    second = await _rows(workspace)

    assert first == second


async def test_the_snapshot_records_why_not_only_what(db_session, workspace) -> None:
    """A status with no reasons is a verdict nobody can check months later."""
    await build_sendable(db_session, workspace, suffix="health3")
    await db_session.commit()

    await _capture(workspace)

    async with get_sessionmaker()() as session:
        row = (
            (
                await session.execute(
                    select(SenderHealthSnapshot).where(
                        SenderHealthSnapshot.workspace_id == workspace
                    )
                )
            )
            .scalars()
            .first()
        )

    assert row is not None
    assert row.status
    assert row.captured_on == dt.datetime.now(dt.UTC).date()
    assert isinstance(row.reasons, list)
    # Denormalised on purpose: history that rewrites itself when an identity
    # changes domain is not history.
    assert row.sending_domain


async def test_a_workspace_with_no_senders_captures_nothing_and_says_so(
    workspace,
) -> None:
    result = await _capture(workspace)

    assert result.unavailable is None
    assert result.captured == 0


def test_the_snapshot_runs_between_verification_and_the_ramp() -> None:
    """The ten minutes either side are the whole design.

    Verification refreshes the SPF, DKIM and DMARC flags the snapshot records;
    the ramp reads health to decide volume. Capturing between them means the
    ramp acts on a snapshot taken after today's DNS check rather than on
    yesterday's -- and a mailbox that lost its records overnight is already
    marked before anything decides how much it may send.
    """
    from titan.workflows.mailbox_ramp import DEFAULT_CRON as ramp_cron
    from titan.workflows.sender_health import DEFAULT_CRON as health_cron
    from titan.workflows.verification import DEFAULT_CRON as verify_cron

    def minutes(cron: str) -> int:
        minute, hour = cron.split()[:2]
        return int(hour) * 60 + int(minute)

    assert minutes(verify_cron) < minutes(health_cron) < minutes(ramp_cron), (
        f"verification {verify_cron}, health {health_cron}, ramp {ramp_cron} "
        "are no longer in an order that makes the snapshot useful"
    )


def test_the_schedule_is_installed_at_all() -> None:
    """Built and never scheduled is indistinguishable from never built."""
    from titan.workflows.schedules import plan_schedules

    jobs = {j.workflow for j in plan_schedules(uuid.uuid4(), task_queue="q")}

    assert "SenderHealthSnapshotWorkflow" in jobs
