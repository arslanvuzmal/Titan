"""The reply desk: see a reply, put your own words in, send it in the thread."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from coldops.db.enums import DraftStatus, OutboxStatus, ReplyClass, SuppressionReason
from coldops.db.models import (
    InboundMessage,
    MessageApproval,
    MessageDraft,
    OutboxMessage,
    ReplyClassification,
    SenderIdentity,
)
from coldops.db.session import get_sessionmaker
from coldops.delivery.outbox_worker import OutboxWorker
from coldops.delivery.providers.mock import MockEmailProvider
from coldops.delivery.suppression import suppress
from coldops.outreach import reply_desk
from coldops.outreach.reply_desk import DeskError, thread_headers
from sqlalchemy import select, update

from tests.delivery.conftest import NOW, build_sendable, sending_settings

pytestmark = [pytest.mark.integration]

THEIR_ID = "<CAF=reply-123@mail.gmail.com>"


async def _replied(
    session,
    workspace,
    *,
    reply_class=ReplyClass.WANTS_CALL,
    body=None,
    threaded=True,
    their_id=THEIR_ID,
):
    """A lead whose first message went out and who wrote back."""
    fixture = await build_sendable(session, workspace, suffix=uuid.uuid4().hex[:6])
    # The opener has gone; only the reply should be in the outbox from here.
    await session.execute(
        update(OutboxMessage)
        .where(OutboxMessage.id == fixture.outbox_id)
        .values(status=OutboxStatus.SENT)
    )
    inbound = InboundMessage(
        workspace_id=workspace,
        provider="imap",
        provider_inbound_id=their_id,
        in_reply_to_message_id=fixture.message_id if threaded else None,
        lead_id=fixture.lead_id,
        from_email_normalized=fixture.to_email,
        subject="Re: Your booking page",
        body_text="Sounds interesting. Could you call me on Tuesday morning?",
        received_at=NOW,
        raw_payload={"in_reply_to": "<opener-1@arslanvuzmallone.dev>"},
    )
    session.add(inbound)
    await session.flush()
    draft = MessageDraft(
        workspace_id=workspace,
        lead_id=fixture.lead_id,
        campaign_id=fixture.campaign_id,
        contact_channel_id=fixture.channel_id,
        idempotency_key=f"reply:{inbound.id}",
        status=DraftStatus.GENERATED,
        subject="Re: Your booking page",
        body_text=body
        or "Thanks for coming back to me. Tuesday at [TODO: a time] works.\n",
        claim_map=[],
        validation_report={"source": "reply_drafter", "ready_to_send": False},
        validation_passed=False,
        template_key=f"reply:{reply_class.value}",
    )
    session.add(draft)
    await session.flush()
    session.add(
        ReplyClassification(
            workspace_id=workspace,
            inbound_message_id=inbound.id,
            reply_class=reply_class,
            confidence=0.9,
            decided_by="rules",
            suggested_reply_draft_id=draft.id,
        )
    )
    await session.commit()
    return fixture, draft.id


# ------------------------------------------------------------------ listing
@pytest.mark.asyncio
async def test_a_reply_waits_on_the_desk_with_their_words(db_session, workspace) -> None:
    _, draft_id = await _replied(db_session, workspace)
    items = await reply_desk.waiting(db_session, workspace_id=workspace)
    item = next(i for i in items if i.draft_id == draft_id)
    assert item.reply_class == "wants_call"
    assert "Tuesday morning" in item.their_words
    assert item.ready_to_send is False


# ------------------------------------------------------------------ editing
@pytest.mark.asyncio
async def test_a_blank_left_for_a_person_cannot_be_sent(db_session, workspace) -> None:
    _, draft_id = await _replied(db_session, workspace)
    with pytest.raises(DeskError, match="blank"):
        await reply_desk.send(
            db_session,
            workspace_id=workspace,
            draft_id=draft_id,
            seen_version=1,
            decided_by=None,
        )


@pytest.mark.asyncio
async def test_an_edit_needs_the_version_the_operator_saw(db_session, workspace) -> None:
    _, draft_id = await _replied(db_session, workspace)
    with pytest.raises(DeskError, match="changed since you opened it"):
        await reply_desk.edit(
            db_session,
            workspace_id=workspace,
            draft_id=draft_id,
            seen_version=99,
            subject="Re: Your booking page",
            body="Tuesday at 10 works.",
        )


# ------------------------------------------------------------------ sending
async def _edit_and_send(session, workspace, draft_id):
    draft = await reply_desk.edit(
        session,
        workspace_id=workspace,
        draft_id=draft_id,
        seen_version=1,
        subject="Re: Your booking page",
        body="Tuesday at 10 works. I will ring the practice number.",
    )
    outbox = await reply_desk.send(
        session,
        workspace_id=workspace,
        draft_id=draft_id,
        seen_version=draft.version,
        decided_by=None,
    )
    await session.commit()
    return outbox


@pytest.mark.asyncio
async def test_a_sent_reply_is_threaded_signed_and_approved(
    db_session, workspace
) -> None:
    fixture, draft_id = await _replied(db_session, workspace)
    outbox = await _edit_and_send(db_session, workspace, draft_id)

    payload = outbox.payload
    assert payload["kind"] == "reply"
    assert payload["headers"]["In-Reply-To"] == THEIR_ID
    assert THEIR_ID in payload["headers"]["References"]
    assert payload["list_unsubscribe"]
    assert "12 Fictional Row" in payload["text_body"], "the postal address is added"
    approvals = (
        (
            await db_session.execute(
                select(MessageApproval).where(MessageApproval.draft_id == draft_id)
            )
        )
        .scalars()
        .all()
    )
    assert [a.decision for a in approvals] == ["approved"]
    draft = await db_session.get(MessageDraft, draft_id)
    assert draft.status is DraftStatus.QUEUED
    assert outbox.sender_identity_id == fixture.sender_id, "same mailbox as the opener"


@pytest.mark.asyncio
async def test_a_decline_is_not_answered(db_session, workspace) -> None:
    _, draft_id = await _replied(
        db_session,
        workspace,
        reply_class=ReplyClass.NOT_INTERESTED,
        body="Understood, thank you.\n",
    )
    with pytest.raises(DeskError, match="not answered"):
        await reply_desk.send(
            db_session,
            workspace_id=workspace,
            draft_id=draft_id,
            seen_version=1,
            decided_by=None,
        )


@pytest.mark.asyncio
async def test_a_suppressed_recipient_gets_nothing(db_session, workspace) -> None:
    fixture, draft_id = await _replied(
        db_session, workspace, body="Tuesday at 10 works.\n"
    )
    await suppress(
        db_session,
        workspace_id=workspace,
        email_or_domain=fixture.to_email,
        reason=SuppressionReason.UNSUBSCRIBE,
        source="test",
    )
    await db_session.commit()
    with pytest.raises(DeskError, match="suppressed"):
        await reply_desk.send(
            db_session,
            workspace_id=workspace,
            draft_id=draft_id,
            seen_version=1,
            decided_by=None,
        )


@pytest.mark.asyncio
async def test_the_worker_sends_a_short_re_reply_past_the_cold_mail_gates(
    db_session, workspace, monkeypatch
) -> None:
    """Placement gate on with no readings, a one-line body, a "Re:" subject:
    each would stop a cold message. None of them stops an answer."""
    fixture, draft_id = await _replied(db_session, workspace)
    # As production has them: a one-click template and the secret that signs it.
    await db_session.execute(
        update(SenderIdentity)
        .where(SenderIdentity.id == fixture.sender_id)
        .values(unsubscribe_url_template="https://arslanvuzmallone.com/u/{token}")
    )
    await db_session.commit()
    monkeypatch.setattr(
        reply_desk, "get_settings", lambda: sending_settings(unsubscribe_secret="s" * 32)
    )
    outbox = await _edit_and_send(db_session, workspace, draft_id)

    provider = MockEmailProvider()
    results = await OutboxWorker(
        provider,
        sending_settings(placement_gate_enabled=True),
        # The reply is queued for the real now, not the fixture's clock.
        now_fn=lambda: dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1),
    ).run_once()

    assert [r.outcome for r in results] == ["sent"], results
    assert provider.delivered_count == 1
    async with get_sessionmaker()() as s:
        row = await s.get(OutboxMessage, outbox.id)
        assert row.status is OutboxStatus.SENT


def test_thread_headers_need_a_real_message_id() -> None:
    inbound = InboundMessage(provider_inbound_id="sha256:abc", raw_payload={})
    assert thread_headers(inbound) == {}
    inbound = InboundMessage(provider_inbound_id=THEIR_ID, raw_payload={})
    assert thread_headers(inbound) == {"In-Reply-To": THEIR_ID, "References": THEIR_ID}


@pytest.mark.asyncio
async def test_a_reply_not_threaded_to_us_still_finds_a_mailbox_with_the_gate_on(
    db_session, workspace, monkeypatch
) -> None:
    """The bug that shipped: with the cold-mail gate on and no test inbox, the
    pool said no mailbox could send, and an answer was refused outright."""
    from coldops.delivery import sender_pool

    # Not threaded to one of our messages: the mailbox comes from the pool.
    fixture, draft_id = await _replied(
        db_session, workspace, body="Thanks, Tuesday works.\n", threaded=False
    )
    monkeypatch.setattr(
        sender_pool, "get_settings", lambda: sending_settings(placement_gate_enabled=True)
    )
    draft = await db_session.get(MessageDraft, draft_id)
    edited = await reply_desk.edit(
        db_session,
        workspace_id=workspace,
        draft_id=draft_id,
        seen_version=draft.version,
        subject="Re: Your booking page",
        body="Thanks, Tuesday works.",
    )
    outbox = await reply_desk.send(
        db_session,
        workspace_id=workspace,
        draft_id=draft_id,
        seen_version=edited.version,
        decided_by=None,
    )
    assert outbox.sender_identity_id == fixture.sender_id


@pytest.mark.asyncio
async def test_a_mailbox_full_of_held_cold_drafts_still_carries_an_answer(
    db_session, workspace
) -> None:
    """The second bug that shipped: arslan@ at 10 of 10, all of it cold drafts
    waiting behind the gate, and the one answer to a real person refused."""
    from coldops.db.models import SenderIdentity

    fixture, draft_id = await _replied(
        db_session, workspace, body="Thanks, Tuesday works.\n", threaded=False
    )
    await db_session.execute(
        update(SenderIdentity)
        .where(SenderIdentity.id == fixture.sender_id)
        .values(daily_send_limit=1)
    )
    # The opener's outbox row back to pending: one queued cold message fills
    # a mailbox whose limit is one.
    await db_session.execute(
        update(OutboxMessage)
        .where(OutboxMessage.id == fixture.outbox_id)
        .values(status=OutboxStatus.PENDING)
    )
    await db_session.commit()
    draft = await db_session.get(MessageDraft, draft_id)
    edited = await reply_desk.edit(
        db_session,
        workspace_id=workspace,
        draft_id=draft_id,
        seen_version=draft.version,
        subject="Re: Your booking page",
        body="Thanks, Tuesday works.",
    )
    outbox = await reply_desk.send(
        db_session,
        workspace_id=workspace,
        draft_id=draft_id,
        seen_version=edited.version,
        decided_by=None,
    )
    assert outbox.sender_identity_id == fixture.sender_id


def test_a_message_id_stored_without_brackets_still_threads() -> None:
    """How the collector actually stores them -- the bug that cancelled the
    first real reply sent from the desk."""
    inbound = InboundMessage(
        provider_inbound_id="CAPYiLBo@mail.gmail.com",
        raw_payload={"in_reply_to": "718f1d52@arslanvuzmallone.com"},
    )
    assert thread_headers(inbound) == {
        "In-Reply-To": "<CAPYiLBo@mail.gmail.com>",
        "References": "<718f1d52@arslanvuzmallone.com> <CAPYiLBo@mail.gmail.com>",
    }


@pytest.mark.asyncio
async def test_a_reply_to_an_unbracketed_message_id_is_sent_not_cancelled(
    db_session, workspace, monkeypatch
) -> None:
    """Exactly what happened to the first live reply: the collector's bare
    Message-ID, the cold-mail gate on, and the worker cancelling it under the
    cold rules (lead replied, no evidence, score)."""
    from coldops.db.models import SenderIdentity

    fixture, draft_id = await _replied(
        db_session, workspace, their_id="CAPYiLBo@mail.gmail.com"
    )
    await db_session.execute(
        update(SenderIdentity)
        .where(SenderIdentity.id == fixture.sender_id)
        .values(unsubscribe_url_template="https://arslanvuzmallone.com/u/{token}")
    )
    await db_session.commit()
    monkeypatch.setattr(
        reply_desk, "get_settings", lambda: sending_settings(unsubscribe_secret="s" * 32)
    )
    outbox = await _edit_and_send(db_session, workspace, draft_id)
    assert outbox.payload["headers"]["In-Reply-To"] == "<CAPYiLBo@mail.gmail.com>"

    provider = MockEmailProvider()
    results = await OutboxWorker(
        provider,
        sending_settings(placement_gate_enabled=True),
        now_fn=lambda: dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1),
    ).run_once()
    assert [r.outcome for r in results] == ["sent"], results
