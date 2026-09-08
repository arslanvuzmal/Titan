"""The panel must never be empty of everything.

The operator opens this first thing in the morning, before any window has
opened, and the honest answer to "how much has gone today" is zero. A screen
whose every figure is zero reads as a broken system, and he read it that way
every morning -- correctly, because nothing on it said otherwise.

Yesterday's record is what makes an empty morning legible: nought so far, and
here is the day that just finished.
"""

from __future__ import annotations

import datetime as dt

import pytest
from titan.delivery import day_report

from tests.delivery.conftest import build_sendable

pytestmark = pytest.mark.asyncio


async def test_the_report_carries_yesterday_as_well_as_today(db_session, workspace):
    """Planted violation: report only the current day.

    Without this the panel is entirely zeroes every morning and there is
    nothing on it to tell a system that has not started from one that has
    stopped.
    """
    fixture = await build_sendable(db_session, workspace)
    yesterday = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)

    from titan.db.enums import MessageState
    from titan.db.models import Message
    from titan.db.session import workspace_unit_of_work

    async with workspace_unit_of_work(workspace) as session:
        message = await session.get(Message, fixture.message_id)
        message.state = MessageState.SENT
        message.sent_at = yesterday

    from titan.db.session import workspace_session

    async with workspace_session(workspace) as session:
        report = await day_report.build(
            session, workspace, now=dt.datetime.now(dt.UTC)
        )

    assert report.sent == 0, "nothing has gone today"
    assert report.previous_sent == 1, "but yesterday is on the screen"
    assert report.previous_date == yesterday.date()
