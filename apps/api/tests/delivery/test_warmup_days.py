"""Where each mailbox is on its warm-up ramp."""

from __future__ import annotations

import datetime as dt

import pytest
from coldops.cli import _warmup_days
from coldops.db.enums import MessageState
from coldops.db.models import Message, SenderIdentity
from coldops.delivery.deliverability import warmup_day
from sqlalchemy import select, update

from tests.delivery.conftest import build_sendable

pytestmark = pytest.mark.asyncio


async def test_an_inactive_sender_still_climbs_its_warm_up_ramp(db_session, workspace):
    """Warm-up is what a paused sender needs most; it must not sit at day zero."""
    fx = await build_sendable(db_session, workspace)
    started = dt.datetime.now(dt.UTC) - dt.timedelta(days=6)
    await db_session.execute(
        update(SenderIdentity)
        .where(SenderIdentity.id == fx.sender_id)
        .values(is_active=False, warmup_started_at=started)
    )
    await db_session.commit()
    address = (
        await db_session.execute(
            select(SenderIdentity.from_email).where(SenderIdentity.id == fx.sender_id)
        )
    ).scalar_one()

    days = await _warmup_days(workspace)

    assert days[address.lower()] == warmup_day(started, dt.datetime.now(dt.UTC))
    assert days[address.lower()] > 0


async def test_a_move_to_a_new_provider_restarts_the_ramp(db_session, workspace):
    """warmup_started_at (the move) beats a send from months before it."""
    fx = await build_sendable(db_session, workspace)
    long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=60)
    moved = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    await db_session.execute(
        update(Message)
        .where(Message.id == fx.message_id)
        .values(state=MessageState.SENT, sent_at=long_ago, state_rank=1)
    )
    await db_session.execute(
        update(SenderIdentity)
        .where(SenderIdentity.id == fx.sender_id)
        .values(warmup_started_at=moved)
    )
    await db_session.commit()
    address = (
        await db_session.execute(
            select(SenderIdentity.from_email).where(SenderIdentity.id == fx.sender_id)
        )
    ).scalar_one()

    days = await _warmup_days(workspace)

    assert days[address.lower()] == warmup_day(moved, dt.datetime.now(dt.UTC))
