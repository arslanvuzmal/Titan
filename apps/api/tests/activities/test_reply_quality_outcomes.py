"""A reply's *class* finally reaches the numbers the system tunes itself on.

``reply_classifications`` has been written since replies were first collected,
and nothing downstream ever read it. The campaign manager counted
``leads.replied_at``, so "not interested" and "send me pricing" were the same
observation, and a campaign earning volume on rejections looked like the best
performer in the workspace.

These run against a real database because the join is the substance: the class
lives two tables away from the lead, through ``inbound_messages``, and a query
that quietly matched nothing would show up as every campaign having zero
positive replies -- which is indistinguishable from a bad week.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import update
from titan.activities.orchestration import _campaign_outcomes
from titan.db.enums import LeadStatus, ReplyClass
from titan.db.models import InboundMessage, Lead, ReplyClassification
from titan.db.session import workspace_session

from tests.delivery.conftest import build_sendable

NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)


async def _outcomes(workspace_id, campaign_id) -> dict[str, int]:
    """Read the counts the way production does.

    Through :func:`workspace_session`, because the raw SQL carries its own
    ``workspace_id`` predicate and reads it from the session -- a bare session
    has none, and every count would silently come back zero.
    """
    async with workspace_session(workspace_id) as session:
        return await _campaign_outcomes(session, campaign_id, NOW)


async def _replied_lead(session, workspace_id, *, suffix: str, reply: ReplyClass | None):
    """A lead that answered, optionally with a classified reply."""
    fixture = await build_sendable(session, workspace_id, suffix=suffix)
    await session.execute(
        update(Lead)
        .where(Lead.id == fixture.lead_id)
        .values(replied_at=NOW - dt.timedelta(days=1), status=LeadStatus.REPLIED)
    )
    if reply is not None:
        inbound = InboundMessage(
            workspace_id=workspace_id,
            lead_id=fixture.lead_id,
            provider="mock",
            provider_inbound_id=f"inbound-{uuid.uuid4().hex[:10]}",
            from_email_normalized="someone@example.test",
            received_at=NOW - dt.timedelta(days=1),
            raw_payload={},
        )
        session.add(inbound)
        await session.flush()
        session.add(
            ReplyClassification(
                workspace_id=workspace_id,
                inbound_message_id=inbound.id,
                reply_class=reply,
                confidence=0.9,
                decided_by="rules",
            )
        )
    await session.commit()
    return fixture


@pytest.mark.asyncio
async def test_an_interested_reply_counts_as_positive(db_session, workspace) -> None:
    fixture = await _replied_lead(
        db_session, workspace, suffix="rq1", reply=ReplyClass.WANTS_PRICING
    )

    outcomes = await _outcomes(workspace, fixture.campaign_id)

    assert outcomes["replied"] == 1
    assert outcomes["positive_replies"] == 1


@pytest.mark.asyncio
async def test_a_rejection_is_a_reply_and_not_a_positive_one(
    db_session, workspace
) -> None:
    """The distinction the whole change rests on."""
    fixture = await _replied_lead(
        db_session, workspace, suffix="rq2", reply=ReplyClass.NOT_INTERESTED
    )

    outcomes = await _outcomes(workspace, fixture.campaign_id)

    assert outcomes["replied"] == 1
    assert outcomes["positive_replies"] == 0


@pytest.mark.asyncio
async def test_a_soft_no_is_not_counted_as_success(db_session, workspace) -> None:
    """``NOT_NOW`` is engagement and is not progress. Counting a polite decline
    as a win is the exact error the positive set exists to stop."""
    fixture = await _replied_lead(
        db_session, workspace, suffix="rq3", reply=ReplyClass.NOT_NOW
    )

    outcomes = await _outcomes(workspace, fixture.campaign_id)

    assert outcomes["replied"] == 1
    assert outcomes["positive_replies"] == 0


@pytest.mark.asyncio
async def test_an_unclassified_reply_is_not_assumed_good(db_session, workspace) -> None:
    """A reply the classifier never got to is not evidence of anything. Absent
    data must not read as a success, or an outage in classification would look
    like a very good week."""
    fixture = await _replied_lead(db_session, workspace, suffix="rq4", reply=None)

    outcomes = await _outcomes(workspace, fixture.campaign_id)

    assert outcomes["replied"] == 1
    assert outcomes["positive_replies"] == 0


@pytest.mark.asyncio
async def test_a_booked_meeting_is_counted(db_session, workspace) -> None:
    """The outcome the system exists to produce, visible to it for the first
    time."""
    fixture = await _replied_lead(
        db_session, workspace, suffix="rq5", reply=ReplyClass.WANTS_CALL
    )
    await db_session.execute(
        update(Lead)
        .where(Lead.id == fixture.lead_id)
        .values(status=LeadStatus.MEETING_BOOKED)
    )
    await db_session.commit()

    outcomes = await _outcomes(workspace, fixture.campaign_id)

    assert outcomes["meetings_booked"] == 1
    assert outcomes["positive_replies"] == 1


@pytest.mark.asyncio
async def test_a_lead_who_answered_twice_is_counted_once(db_session, workspace) -> None:
    """The denominator is leads, so the numerator must be too. Counting
    classifications would let one talkative lead outweigh an arm."""
    fixture = await _replied_lead(
        db_session, workspace, suffix="rq6", reply=ReplyClass.INTERESTED
    )
    second = InboundMessage(
        workspace_id=workspace,
        lead_id=fixture.lead_id,
        provider="mock",
        provider_inbound_id=f"inbound-{uuid.uuid4().hex[:10]}",
        from_email_normalized="someone@example.test",
        received_at=NOW,
        raw_payload={},
    )
    db_session.add(second)
    await db_session.flush()
    db_session.add(
        ReplyClassification(
            workspace_id=workspace,
            inbound_message_id=second.id,
            reply_class=ReplyClass.WANTS_CALL,
            confidence=0.9,
            decided_by="rules",
        )
    )
    await db_session.commit()

    outcomes = await _outcomes(workspace, fixture.campaign_id)

    assert outcomes["positive_replies"] == 1, "one lead, counted twice"
