"""A mailbox moved to a new provider starts the cold ramp again.

The live estate on 10 Oct 2026: five mailboxes sending since August, moved to
Google Workspace that morning (warmup_started_at set). The pool read the
*earlier* date and would have offered each its full limit the first day cold
mail was switched back on.
"""

from __future__ import annotations

import datetime as dt

import pytest
from coldops.db.models import Message, SenderIdentity
from coldops.db.session import get_sessionmaker
from coldops.delivery import sender_pool
from sqlalchemy import update

from .conftest import NOW

pytestmark = pytest.mark.integration


async def _limit(sendable, *, moved: dt.datetime | None) -> int:
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Message)
            .where(Message.id == sendable.message_id)
            .values(sent_at=NOW - dt.timedelta(days=60))
        )
        await s.execute(
            update(SenderIdentity)
            .where(SenderIdentity.id == sendable.sender_id)
            .values(warmup_started_at=moved)
        )
    async with get_sessionmaker()() as s:
        slots = await sender_pool.load_slots(
            s, sendable.workspace_id, sendable.campaign_id, now=NOW, cold_mail=False
        )
    [slot] = slots
    return slot.daily_limit


@pytest.mark.asyncio
async def test_two_months_of_history_is_warm(db_session, sendable) -> None:
    assert await _limit(sendable, moved=None) == 100


@pytest.mark.asyncio
async def test_a_move_yesterday_puts_it_back_at_the_start(db_session, sendable) -> None:
    from coldops.delivery.deliverability import warmup_limit

    moved = NOW - dt.timedelta(days=1)
    expected = warmup_limit(first_send_at=moved, now=NOW, target=100)
    assert expected is not None and expected < 100
    assert await _limit(sendable, moved=moved) == expected
