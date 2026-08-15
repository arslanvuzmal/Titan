"""Soft bounce escalation, through both of the paths a bounce arrives by.

The hard-bounce half already worked and has its own tests. What is new is the
other half: that a 4.x.x does *not* suppress, that it holds the lead back
instead, and that the third one inside the window gives up -- writing
``SuppressionReason.REPEATED_SOFT_BOUNCE``, which until now no code had ever
written.

Both intake paths are exercised deliberately. They were two separate answers to
"what does a bounce mean", and the point of ``titan.delivery.bounces`` is that
there is now one.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select, update
from titan.db.enums import LeadStatus, SuppressionReason
from titan.db.models import Lead, Message
from titan.db.session import get_sessionmaker
from titan.delivery.bounces import (
    SOFT_BOUNCE_BACKOFF,
    SOFT_BOUNCES_TO_SUPPRESS,
    BounceKind,
    record_bounce,
)
from titan.delivery.suppression import is_suppressed

from .conftest import NOW, build_sendable

pytestmark = pytest.mark.integration


async def _bounce(
    workspace_id,
    *,
    to_email: str,
    kind: BounceKind,
    lead_id=None,
    message_id=None,
    now: dt.datetime = NOW,
):
    async with get_sessionmaker()() as s, s.begin():
        return await record_bounce(
            s,
            workspace_id=workspace_id,
            to_email=to_email,
            kind=kind,
            source="test",
            message_id=message_id,
            lead_id=lead_id,
            now=now,
        )


async def _sent_message(
    session,
    workspace_id,
    *,
    suffix: str,
    sent_at: dt.datetime,
    to_email: str | None = None,
):
    """A message Titan actually sent, which is what a bounce is about.

    ``to_email`` matters more than it looks: build_sendable mints a fresh
    address per call, and the soft-bounce counter counts per address. Leaving it
    to default would build three messages to three different people and then
    assert that one person had bounced three times.
    """
    fixture = await build_sendable(
        session, workspace_id, suffix=suffix, to_email=to_email
    )
    await session.execute(
        update(Message).where(Message.id == fixture.message_id).values(sent_at=sent_at)
    )
    await session.commit()
    return fixture


async def _lead(lead_id) -> Lead:
    async with get_sessionmaker()() as s:
        return (await s.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()


async def _message(message_id) -> Message:
    async with get_sessionmaker()() as s:
        return (
            await s.execute(select(Message).where(Message.id == message_id))
        ).scalar_one()


# ==========================================================================
# One soft bounce is not a verdict
# ==========================================================================
@pytest.mark.asyncio
async def test_a_single_soft_bounce_does_not_suppress(db_session, sendable) -> None:
    """A full mailbox is a bad afternoon, not a bad address. Suppressing here
    would throw away a lead over a temporary condition."""
    outcome = await _bounce(
        sendable.workspace_id,
        to_email=sendable.to_email,
        kind=BounceKind.SOFT,
        lead_id=sendable.lead_id,
        message_id=sendable.message_id,
    )

    assert outcome.suppressed is False
    assert outcome.soft_bounce_count == 1
    async with get_sessionmaker()() as s:
        assert (
            await is_suppressed(
                s, workspace_id=sendable.workspace_id, email=sendable.to_email
            )
            is None
        )


@pytest.mark.asyncio
async def test_a_soft_bounce_holds_the_lead_back(db_session, sendable) -> None:
    """ "Controlled retries" is the whole point: try again, but not tomorrow.
    Retrying into a full mailbox produces a second soft bounce and no
    information."""
    outcome = await _bounce(
        sendable.workspace_id,
        to_email=sendable.to_email,
        kind=BounceKind.SOFT,
        lead_id=sendable.lead_id,
        message_id=sendable.message_id,
    )

    assert outcome.retry_after == NOW + SOFT_BOUNCE_BACKOFF[0]
    lead = await _lead(sendable.lead_id)
    assert lead.next_action_at == outcome.retry_after
    assert lead.status is not LeadStatus.SUPPRESSED
    assert "soft bounce 1" in (lead.status_reason or "")


@pytest.mark.asyncio
async def test_the_backoff_lengthens(db_session, sendable) -> None:
    await _bounce(
        sendable.workspace_id,
        to_email=sendable.to_email,
        kind=BounceKind.SOFT,
        lead_id=sendable.lead_id,
        message_id=sendable.message_id,
    )
    second = await _sent_message(
        db_session,
        sendable.workspace_id,
        suffix="sb2",
        sent_at=NOW,
        to_email=sendable.to_email,
    )
    outcome = await _bounce(
        sendable.workspace_id,
        to_email=sendable.to_email,
        kind=BounceKind.SOFT,
        lead_id=sendable.lead_id,
        message_id=second.message_id,
    )

    assert outcome.soft_bounce_count == 2
    assert outcome.retry_after == NOW + SOFT_BOUNCE_BACKOFF[1]
    assert SOFT_BOUNCE_BACKOFF[1] > SOFT_BOUNCE_BACKOFF[0]


# ==========================================================================
# The third one gives up
# ==========================================================================
@pytest.mark.asyncio
async def test_the_third_soft_bounce_suppresses(db_session, sendable) -> None:
    """The first writer REPEATED_SOFT_BOUNCE has ever had."""
    ids = [sendable.message_id]
    for i in range(SOFT_BOUNCES_TO_SUPPRESS - 1):
        extra = await _sent_message(
            db_session,
            sendable.workspace_id,
            suffix=f"sb3-{i}",
            sent_at=NOW,
            to_email=sendable.to_email,
        )
        ids.append(extra.message_id)

    outcomes = []
    for message_id in ids:
        outcomes.append(
            await _bounce(
                sendable.workspace_id,
                to_email=sendable.to_email,
                kind=BounceKind.SOFT,
                lead_id=sendable.lead_id,
                message_id=message_id,
            )
        )

    assert [o.suppressed for o in outcomes] == [False, False, True]
    final = outcomes[-1]
    assert final.soft_bounce_count == SOFT_BOUNCES_TO_SUPPRESS
    assert final.reason is SuppressionReason.REPEATED_SOFT_BOUNCE

    async with get_sessionmaker()() as s:
        entry = await is_suppressed(
            s, workspace_id=sendable.workspace_id, email=sendable.to_email
        )
    assert entry is not None
    assert entry.reason is SuppressionReason.REPEATED_SOFT_BOUNCE

    lead = await _lead(sendable.lead_id)
    assert lead.status is LeadStatus.SUPPRESSED


@pytest.mark.asyncio
async def test_soft_bounces_outside_the_window_do_not_count(db_session, sendable) -> None:
    """A mailbox full once last quarter is not a pattern."""
    old = NOW - dt.timedelta(days=90)
    await _bounce(
        sendable.workspace_id,
        to_email=sendable.to_email,
        kind=BounceKind.SOFT,
        lead_id=sendable.lead_id,
        message_id=sendable.message_id,
        now=old,
    )
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Message)
            .where(Message.id == sendable.message_id)
            .values(bounced_at=old)
        )

    fresh = await _sent_message(
        db_session,
        sendable.workspace_id,
        suffix="sbold",
        sent_at=NOW,
        to_email=sendable.to_email,
    )
    outcome = await _bounce(
        sendable.workspace_id,
        to_email=sendable.to_email,
        kind=BounceKind.SOFT,
        lead_id=sendable.lead_id,
        message_id=fresh.message_id,
    )

    assert outcome.soft_bounce_count == 1, "a 90-day-old bounce was counted"
    assert outcome.suppressed is False


# ==========================================================================
# Hard bounces are unchanged
# ==========================================================================
@pytest.mark.asyncio
async def test_a_hard_bounce_still_suppresses_immediately(db_session, sendable) -> None:
    """The half that already worked must keep working."""
    outcome = await _bounce(
        sendable.workspace_id,
        to_email=sendable.to_email,
        kind=BounceKind.HARD,
        lead_id=sendable.lead_id,
        message_id=sendable.message_id,
    )

    assert outcome.suppressed is True
    assert outcome.reason is SuppressionReason.HARD_BOUNCE
    assert outcome.soft_bounce_count == 0
    assert (await _lead(sendable.lead_id)).status is LeadStatus.SUPPRESSED


# ==========================================================================
# The stamp
# ==========================================================================
@pytest.mark.asyncio
async def test_the_bounce_kind_is_recorded_on_the_message(db_session, sendable) -> None:
    """Without this nothing can be counted, and domain health has to treat every
    bounce as hard."""
    await _bounce(
        sendable.workspace_id,
        to_email=sendable.to_email,
        kind=BounceKind.SOFT,
        lead_id=sendable.lead_id,
        message_id=sendable.message_id,
    )

    message = await _message(sendable.message_id)
    assert message.bounce_kind == "soft"
    assert message.bounced_at is not None


@pytest.mark.asyncio
async def test_an_existing_bounce_timestamp_is_not_moved(db_session, sendable) -> None:
    """The webhook path sets bounced_at before calling in. Overwriting it would
    move the event to whenever the poller happened to read it."""
    earlier = NOW - dt.timedelta(hours=6)
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Message)
            .where(Message.id == sendable.message_id)
            .values(bounced_at=earlier)
        )

    await _bounce(
        sendable.workspace_id,
        to_email=sendable.to_email,
        kind=BounceKind.SOFT,
        lead_id=sendable.lead_id,
        message_id=sendable.message_id,
    )

    assert (await _message(sendable.message_id)).bounced_at == earlier


# ==========================================================================
# Attribution
# ==========================================================================
@pytest.mark.asyncio
async def test_an_unthreaded_bounce_finds_the_message_by_address(
    db_session, sendable
) -> None:
    """An inbound bounce is an ordinary email and does not always thread back.
    A mail server bounces what it was just handed, so the most recent send to
    that address is the right answer."""
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(Message)
            .where(Message.id == sendable.message_id)
            .values(sent_at=NOW - dt.timedelta(hours=1))
        )

    outcome = await _bounce(
        sendable.workspace_id,
        to_email=sendable.to_email,
        kind=BounceKind.SOFT,
        lead_id=sendable.lead_id,
        message_id=None,
    )

    assert outcome.attributed is True
    assert outcome.soft_bounce_count == 1
    assert (await _message(sendable.message_id)).bounce_kind == "soft"


@pytest.mark.asyncio
async def test_a_soft_bounce_for_an_address_we_never_wrote_to_is_not_counted(
    db_session, sendable
) -> None:
    """Backscatter: somebody forged our domain and the bounce came to us. It is
    not evidence about a send we made, because we made none."""
    outcome = await _bounce(
        sendable.workspace_id,
        to_email="stranger@nowhere.test",
        kind=BounceKind.SOFT,
        lead_id=None,
        message_id=None,
    )

    assert outcome.attributed is False
    assert outcome.soft_bounce_count == 0
    assert outcome.suppressed is False


@pytest.mark.asyncio
async def test_a_hard_bounce_still_suppresses_without_attribution(
    db_session, sendable
) -> None:
    """Not counting is not the same as not acting. A permanent failure is
    conclusive about the address whether or not we can name the message."""
    outcome = await _bounce(
        sendable.workspace_id,
        to_email="stranger@nowhere.test",
        kind=BounceKind.HARD,
        lead_id=None,
        message_id=None,
    )

    assert outcome.attributed is False
    assert outcome.suppressed is True
    async with get_sessionmaker()() as s:
        assert (
            await is_suppressed(
                s, workspace_id=sendable.workspace_id, email="stranger@nowhere.test"
            )
            is not None
        )


# ==========================================================================
# Isolation
# ==========================================================================
@pytest.mark.asyncio
async def test_another_workspace_soft_bounces_do_not_count(db_session, sendable) -> None:
    """The counter is a raw SQL aggregate, so it carries its own workspace
    predicate -- nothing about the session supplies one."""
    import uuid as _uuid

    from titan.db.models import Workspace

    other = Workspace(name="Other", slug=f"o-{_uuid.uuid4().hex[:12]}")
    db_session.add(other)
    await db_session.commit()
    try:
        theirs = await _sent_message(db_session, other.id, suffix="sbiso", sent_at=NOW)
        for _ in range(SOFT_BOUNCES_TO_SUPPRESS):
            await _bounce(
                other.id,
                to_email=theirs.to_email,
                kind=BounceKind.SOFT,
                message_id=theirs.message_id,
            )

        outcome = await _bounce(
            sendable.workspace_id,
            to_email=sendable.to_email,
            kind=BounceKind.SOFT,
            lead_id=sendable.lead_id,
            message_id=sendable.message_id,
        )
        assert outcome.soft_bounce_count == 1
        assert outcome.suppressed is False
    finally:
        from sqlalchemy import delete

        await db_session.rollback()
        await db_session.execute(delete(Workspace).where(Workspace.id == other.id))
        await db_session.commit()
