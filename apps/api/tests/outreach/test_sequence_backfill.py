"""Attaching already-delivered drafts to the step they actually were.

Runs against a real PostgreSQL: the whole behaviour is one window function
joined to campaign policy, and a mock would only prove the SQL string had not
changed.

The expensive failure here is not leaving a draft unattached. It is attaching
the wrong step -- which makes the scheduler skip a message the business never
received, or re-send one it did.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text
from titan.db.enums import OutboxStatus
from titan.db.session import workspace_unit_of_work
from titan.outreach.provisioning import ensure_sequence
from titan.outreach.sequence_backfill import apply, survey

from tests.delivery.conftest import build_sendable

pytestmark = pytest.mark.asyncio


async def _deliver(session, outbox_id: uuid.UUID, *, minutes_ago: int) -> None:
    """Mark an outbox row as actually gone out, at a known moment."""
    await session.execute(
        text("UPDATE outbox_messages SET status = :s, sent_at = :t WHERE id = :id"),
        {
            "s": OutboxStatus.SENT.value,
            "t": dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago),
            "id": outbox_id,
        },
    )
    await session.commit()


async def _step_of(session, draft_id: uuid.UUID) -> int | None:
    return await session.scalar(
        text(
            "SELECT s.step_number FROM message_drafts d "
            "JOIN sequence_steps s ON s.id = d.sequence_step_id WHERE d.id = :id"
        ),
        {"id": draft_id},
    )


async def _with_sequence(workspace, campaign_id) -> None:
    async with workspace_unit_of_work(workspace) as session:
        await ensure_sequence(session, workspace_id=workspace, campaign_id=campaign_id)


async def test_a_delivered_opener_is_recorded_as_step_one(db_session, workspace):
    """Planted violation: leave the column NULL, as production did for 6,064 drafts.

    An empty ``completed`` set makes ``plan_followup`` resolve to step one with
    ``delay_days=0`` -- due immediately, every cycle, forever. 344 of 375
    contacted leads were held there.
    """
    fixture = await build_sendable(db_session, workspace, suffix="opener")
    await _with_sequence(workspace, fixture.campaign_id)
    await _deliver(db_session, fixture.outbox_id, minutes_ago=60)

    report = await apply(workspace)

    assert report.drafts_matched >= 1
    assert await _step_of(db_session, fixture.draft_id) == 1


async def test_position_comes_from_the_order_things_were_delivered(db_session, workspace):
    """Two messages to one lead are step one then step two, in send order.

    Ranked on ``sent_at`` rather than on draft creation: a lead carries
    thousands of superseded drafts and a handful of sent messages, and only the
    sent ones happened as far as the recipient is concerned.
    """
    first = await build_sendable(db_session, workspace, suffix="first")
    await _with_sequence(workspace, first.campaign_id)

    second = await build_sendable(
        db_session, workspace, suffix="second", to_email=first.to_email
    )
    # Same lead, so the window partitions them together.
    await db_session.execute(
        text(
            "UPDATE outbox_messages SET lead_id = :lead, campaign_id = :camp "
            "WHERE id = :id"
        ),
        {"lead": first.lead_id, "camp": first.campaign_id, "id": second.outbox_id},
    )
    await db_session.execute(
        text("UPDATE message_drafts SET campaign_id = :camp WHERE id = :id"),
        {"camp": first.campaign_id, "id": second.draft_id},
    )
    await db_session.commit()

    await _deliver(db_session, first.outbox_id, minutes_ago=120)
    await _deliver(db_session, second.outbox_id, minutes_ago=10)

    await apply(workspace)

    assert await _step_of(db_session, first.draft_id) == 1
    assert await _step_of(db_session, second.draft_id) == 2, (
        "the later delivery is the follow-up, whatever order the drafts were written"
    )


async def test_a_draft_that_never_sent_is_left_alone(db_session, workspace):
    """It was never a step. Only what reached somebody counts as one."""
    fixture = await build_sendable(db_session, workspace, suffix="unsent")
    await _with_sequence(workspace, fixture.campaign_id)
    # Deliberately not delivered.

    await apply(workspace)

    assert await _step_of(db_session, fixture.draft_id) is None


async def test_a_campaign_with_no_active_sequence_contributes_nothing(
    db_session, workspace
):
    """Planted violation: invent a step for a campaign that defines none.

    18 of 29 active campaigns had no sequence at all -- created by a path that
    never called ``ensure_sequence``. Their leads cannot be followed up until
    one exists, and manufacturing a step id here would be worse than waiting.
    """
    fixture = await build_sendable(db_session, workspace, suffix="nosequence")
    await _deliver(db_session, fixture.outbox_id, minutes_ago=30)

    await apply(workspace)

    assert await _step_of(db_session, fixture.draft_id) is None


async def test_the_survey_changes_nothing(db_session, workspace):
    """It is the thing an operator runs before deciding. It must be inert."""
    fixture = await build_sendable(db_session, workspace, suffix="dryrun")
    await _with_sequence(workspace, fixture.campaign_id)
    await _deliver(db_session, fixture.outbox_id, minutes_ago=45)

    report = await survey(workspace)

    assert report.drafts_matched >= 1, "it still has to report what it would do"
    assert report.applied is False
    assert await _step_of(db_session, fixture.draft_id) is None


async def test_running_it_twice_changes_nothing_the_second_time(db_session, workspace):
    """Only ever fills a NULL, so a nervous operator can run it again."""
    fixture = await build_sendable(db_session, workspace, suffix="twice")
    await _with_sequence(workspace, fixture.campaign_id)
    await _deliver(db_session, fixture.outbox_id, minutes_ago=15)

    await apply(workspace)
    again = await apply(workspace)

    assert again.is_noop, "a second pass has nothing left to attach"
    assert await _step_of(db_session, fixture.draft_id) == 1
