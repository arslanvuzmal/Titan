"""The daily mail to the operator, and the two ways it goes wrong.

Runs against a real PostgreSQL. The behaviours worth pinning are both about
what the report must *not* do: report twice on the same day, and become part
of the sending statistics it exists to describe.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select
from titan.activities.daily_report import send_daily_report_for
from titan.db.models import Message
from titan.db.models.ops import Task

from tests.delivery.conftest import build_sendable

pytestmark = pytest.mark.asyncio

LATE = dt.datetime(2026, 9, 7, 23, 30, tzinfo=dt.UTC)
NOON = dt.datetime(2026, 9, 7, 12, 0, tzinfo=dt.UTC)


class FakeMailer:
    """Records what would have gone out, and can be told to fail."""

    def __init__(self, fails: bool = False) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fails = fails

    async def __call__(self, *, to: str, subject: str, body: str) -> None:
        if self.fails:
            raise RuntimeError("smtp refused the connection")
        self.sent.append((to, subject, body))


class FakePinger:
    def __init__(self) -> None:
        self.pings = 0

    async def __call__(self) -> None:
        self.pings += 1


async def messages_in(session, workspace: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Message).where(Message.workspace_id == workspace)
        )
    ).scalar_one()


async def reports_filed(session, workspace: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.workspace_id == workspace, Task.dedupe_key.like("daily-report:%"))
        )
    ).scalar_one()


async def test_the_report_is_sent_once_a_day_not_once_an_hour(db_session, workspace):
    """Planted violation: drop the claim and send on every pass.

    The check runs hourly so it can fire the moment the quota is spent. If it
    did not claim the day first, the operator would get the same report
    twenty-four times, and would stop reading the first one.
    """
    await build_sendable(db_session, workspace)
    mailer, pinger = FakeMailer(), FakePinger()

    await send_daily_report_for(
        workspace_id=workspace, now=LATE, mailer=mailer, pinger=pinger
    )
    await send_daily_report_for(
        workspace_id=workspace,
        now=LATE + dt.timedelta(minutes=20),
        mailer=mailer,
        pinger=pinger,
    )

    assert len(mailer.sent) == 1
    assert await reports_filed(db_session, workspace) == 1


async def test_the_report_never_becomes_a_tracked_message(db_session, workspace):
    """Planted violation: send it through the outbox like any other mail.

    A report about the day's sending that was itself a tracked message would
    consume outreach quota, enter the bounce statistics, and distort the very
    numbers it reports. It goes over SMTP directly and leaves no row behind.
    """
    fixture = await build_sendable(db_session, workspace)
    before = await messages_in(db_session, workspace)
    mailer = FakeMailer()

    await send_daily_report_for(
        workspace_id=workspace, now=LATE, mailer=mailer, pinger=FakePinger()
    )

    assert len(mailer.sent) == 1
    assert await messages_in(db_session, workspace) == before
    assert str(fixture.lead_id)  # the fixture's own message is untouched


async def test_a_day_that_sent_nothing_still_produces_a_mail(db_session, workspace):
    """The half the operator asked for twice. A silent day is the one worth
    hearing about, and it is exactly the day a quota-triggered report would
    skip."""
    await build_sendable(db_session, workspace)
    mailer = FakeMailer()

    await send_daily_report_for(
        workspace_id=workspace, now=LATE, mailer=mailer, pinger=FakePinger()
    )

    _to, subject, _body = mailer.sent[0]
    assert "nothing" in subject.lower()


async def test_nothing_is_sent_before_the_day_is_over(db_session, workspace):
    """Planted violation: ignore day_is_over and mail on every pass."""
    await build_sendable(db_session, workspace)
    mailer = FakeMailer()

    await send_daily_report_for(
        workspace_id=workspace, now=NOON, mailer=mailer, pinger=FakePinger()
    )

    assert mailer.sent == []
    assert await reports_filed(db_session, workspace) == 0


async def test_a_failed_send_does_not_burn_the_day(db_session, workspace):
    """Planted violation: claim the day and never release it on failure.

    The claim has to be taken before the mail goes out, or a crash between
    sending and recording would report twice. But a claim that survives a
    failed send means the operator silently gets nothing that day -- the
    failure this whole feature exists to make impossible.
    """
    await build_sendable(db_session, workspace)
    failing = FakeMailer(fails=True)

    await send_daily_report_for(
        workspace_id=workspace, now=LATE, mailer=failing, pinger=FakePinger()
    )

    assert await reports_filed(db_session, workspace) == 0
    working = FakeMailer()
    await send_daily_report_for(
        workspace_id=workspace, now=LATE, mailer=working, pinger=FakePinger()
    )
    assert len(working.sent) == 1


async def test_the_healthcheck_is_pinged_only_after_the_mail_actually_went(
    db_session, workspace
):
    """Planted violation: ping regardless of the send's outcome.

    The ping is what tells the external watchdog Titan is alive. Pinging after
    a failed send would silence the one alarm that still works when Titan
    cannot mail anybody.
    """
    await build_sendable(db_session, workspace)
    pinger = FakePinger()

    await send_daily_report_for(
        workspace_id=workspace, now=LATE, mailer=FakeMailer(fails=True), pinger=pinger
    )
    assert pinger.pings == 0

    await send_daily_report_for(
        workspace_id=workspace, now=LATE, mailer=FakeMailer(), pinger=pinger
    )
    assert pinger.pings == 1
