"""Telling the operator a reply arrived, and deciding how loudly.

The notification is a ``tasks`` row written in the same transaction as the reply
it describes. If the ingest rolls back so does the alert: an alert pointing at a
reply that is not there is worse than no alert.

Runs against a real PostgreSQL for the unique constraint on
``(workspace_id, dedupe_key)``, which is what stops an operator being paged
twice for one message.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from titan.db.enums import ReplyClass, SuppressionReason
from titan.db.models.compliance import SuppressionEntry
from titan.db.models.messaging import ReplyClassification as ReplyClassificationRow
from titan.db.models.ops import Task
from titan.db.session import workspace_unit_of_work
from titan.delivery.inbound import ingest_inbound
from titan.intelligence.replies import InboundMessage, ReplyKind
from titan.notify.operator import NotificationKind

from tests.delivery.conftest import build_sendable

pytestmark = pytest.mark.asyncio

REPLIED_AT = dt.datetime(2026, 8, 10, 9, 14, 22, tzinfo=dt.UTC)


def reply(from_email: str, body: str) -> InboundMessage:
    return InboundMessage(
        from_email=from_email,
        subject="Re: A broken button on your booking page",
        body_text=body,
        headers={"From": from_email},
    )


async def ingest(workspace, fixture, body: str, key: str, **kwargs):
    async with workspace_unit_of_work(workspace) as session:
        return await ingest_inbound(
            session,
            workspace_id=workspace,
            message=kwargs.pop("message", None) or reply(fixture.to_email, body),
            lead_id=fixture.lead_id,
            provider_inbound_id=key,
            received_at=REPLIED_AT,
            **kwargs,
        )


async def test_an_interested_reply_notifies_the_operator(db_session, workspace):
    """ "if a client agrees it should simply inform me"."""
    fixture = await build_sendable(db_session, workspace)

    result = await ingest(
        workspace,
        fixture,
        "Very interested - what would it cost?",
        "agreed-1@theirs.test",
    )

    assert result.intent is not None
    assert result.intent.is_positive
    assert result.notification is not None
    assert result.notification.kind is NotificationKind.CLIENT_AGREED

    async with workspace_unit_of_work(workspace) as session:
        task = (await session.execute(select(Task))).scalars().one()
        assert task.kind == NotificationKind.CLIENT_AGREED.value
        assert task.lead_id == fixture.lead_id
        assert task.status == "open"
        # Highest priority in the system: the only item whose value decays with
        # every hour it goes unread.
        assert task.priority == 100
        assert task.due_at is not None


async def test_the_stored_class_is_the_refined_intent_not_unknown(db_session, workspace):
    """The client history says what they asked for, not just "someone replied".

    Otherwise an operator has to open every reply to find the two that mattered.
    """
    fixture = await build_sendable(db_session, workspace)

    await ingest(
        workspace,
        fixture,
        "Happy to chat, when are you free?",
        "refined-1@theirs.test",
    )

    async with workspace_unit_of_work(workspace) as session:
        row = (await session.execute(select(ReplyClassificationRow))).scalars().one()
        assert row.reply_class is ReplyClass.WANTS_CALL


async def test_a_confident_rejection_suppresses_the_address(db_session, workspace):
    """So the next campaign does not approach somebody who already said no.

    ``SUPPRESSING_REPLY_CLASSES`` maps NOT_INTERESTED to a suppression reason;
    honouring it is what prevents the complaint that kills a sending domain.
    """
    fixture = await build_sendable(db_session, workspace)

    result = await ingest(
        workspace, fixture, "Not interested, thanks.", "declined-1@theirs.test"
    )

    assert result.reply_class is ReplyClass.NOT_INTERESTED
    assert result.suppressed is True
    assert result.notification is not None
    assert result.notification.kind is NotificationKind.REPLY_DECLINED

    async with workspace_unit_of_work(workspace) as session:
        entry = (await session.execute(select(SuppressionEntry))).scalars().one()
        assert entry.normalized_value == fixture.to_email
        assert entry.reason is SuppressionReason.NOT_INTERESTED


async def test_an_ambiguous_reply_never_suppresses(db_session, workspace):
    """Suppression is close to permanent and these are regexes.

    A mixed message resolves to UNKNOWN, and UNKNOWN must not be able to
    blocklist a prospect who was in fact saying yes to half the offer.
    """
    fixture = await build_sendable(db_session, workspace)

    result = await ingest(
        workspace,
        fixture,
        "Not interested in the SEO work, but very interested in the booking fix.",
        "ambiguous-1@theirs.test",
    )

    assert result.reply_class is ReplyClass.UNKNOWN
    assert result.suppressed is False
    assert result.notification is not None
    assert result.notification.kind is NotificationKind.REPLY_NEEDS_READING

    async with workspace_unit_of_work(workspace) as session:
        assert (await session.execute(select(SuppressionEntry))).scalars().all() == []


async def test_a_deferral_stops_the_sequence_but_keeps_the_lead(db_session, workspace):
    """ "check back next quarter" is the warmest part of the pipeline.

    Collapsing it into a rejection would suppress the addresses most likely to
    convert on the next cycle.
    """
    fixture = await build_sendable(db_session, workspace)

    result = await ingest(
        workspace,
        fixture,
        "Not right now - check back with us next quarter.",
        "deferral-1@theirs.test",
    )

    assert result.reply_class is ReplyClass.NOT_NOW
    assert result.sequence_stopped is True
    assert result.suppressed is False


async def test_a_bounce_does_not_notify(db_session, workspace):
    """An alert per bounce is how a channel becomes noise.

    Bounces are handled completely without anybody reading them. Reporting each
    one trains the operator to skim, and the reply that mattered gets skimmed
    past too. They surface as a *rate* in the weekly summary instead.
    """
    fixture = await build_sendable(db_session, workspace)

    result = await ingest(
        workspace,
        fixture,
        "",
        "quiet-dsn@mx.fixture-business.test",
        message=InboundMessage(
            from_email="mailer-daemon@mx.fixture-business.test",
            subject="Undeliverable",
            body_text="5.1.1 user unknown",
            content_type='multipart/report; report-type="delivery-status"',
        ),
        suppression_target=fixture.to_email,
    )

    assert result.kind is ReplyKind.BOUNCE
    assert result.notification is None
    async with workspace_unit_of_work(workspace) as session:
        assert (await session.execute(select(Task))).scalars().all() == []


async def test_a_complaint_does_notify(db_session, workspace):
    """The exception among the machine classes.

    Rare, serious, and the leading indicator of losing a sending domain.
    """
    fixture = await build_sendable(db_session, workspace)

    result = await ingest(
        workspace,
        fixture,
        "This is spam, I never signed up for this.",
        "complaint-1@theirs.test",
    )

    assert result.kind is ReplyKind.COMPLAINT
    assert result.notification is not None
    assert result.notification.kind is NotificationKind.DELIVERABILITY_ALERT


async def test_re_reading_a_reply_does_not_notify_twice(db_session, workspace):
    """An operator paged twice for one reply stops reading the pages."""
    fixture = await build_sendable(db_session, workspace)

    for _ in range(2):
        await ingest(
            workspace, fixture, "Yes please, sounds good.", "dedupe-notify@theirs.test"
        )

    async with workspace_unit_of_work(workspace) as session:
        assert len((await session.execute(select(Task))).scalars().all()) == 1


async def test_the_notification_rolls_back_with_a_failed_ingest(db_session, workspace):
    """Both land or neither does.

    A notification surviving a rolled-back ingest points an operator at a reply
    that does not exist, in a system whose entire value is that its records are
    trustworthy.
    """
    fixture = await build_sendable(db_session, workspace)

    with pytest.raises(RuntimeError, match="deliberate"):
        async with workspace_unit_of_work(workspace) as session:
            await ingest_inbound(
                session,
                workspace_id=workspace,
                message=reply(fixture.to_email, "Yes please, let's do it."),
                lead_id=fixture.lead_id,
                provider_inbound_id="rollback-1@theirs.test",
                received_at=REPLIED_AT,
            )
            raise RuntimeError("deliberate failure after ingest")

    async with workspace_unit_of_work(workspace) as session:
        assert (await session.execute(select(Task))).scalars().all() == []
