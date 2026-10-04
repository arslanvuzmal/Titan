"""The reply desk: every human reply, its suggested answer, and a way to send it.

Titan has drafted a suggested answer to every attributed reply since August
(``titan.delivery.inbound``), and stored each one with
``validation_passed=False`` so it could never reach the outbox without a person
reading it first. That rule was right and is kept. What was missing was the
other half: there was no way for that person to edit the draft, nothing would
approve a draft that failed validation, and nothing would have queued an
approved reply even if it had been approved. Every suggestion ever drafted is
still sitting there unsent.

Three operations, all explicit:

* **list** -- replies that need an answer, newest first, with what the person
  wrote (verbatim), how it was read, and the suggested draft;
* **edit** -- the operator's own words replace the suggestion. Checked for an
  unfilled blank (the drafter marks the price with one) and for the same
  prohibited rhetoric every message is held to;
* **send** -- records the approval and queues the reply *in the same thread*:
  In-Reply-To and References name their message, so it lands under it in their
  mail client rather than as a new email from a stranger. Sent from the mailbox
  that wrote to them first, when that one still exists.

A reply is not cold mail. It is marked ``kind: reply`` in the outbox payload,
and the outbox worker does not hold it behind the cold-mail placement gate --
somebody who wrote back is waiting for an answer, whatever the morning's seed
readings said. Suppression is still checked twice, here and at send.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.config import get_settings
from titan.db.enums import DraftStatus, MessageState, OutboxStatus
from titan.db.models import (
    ContactChannel,
    InboundMessage,
    Lead,
    Message,
    MessageApproval,
    MessageDraft,
    Organization,
    OutboxMessage,
    ReplyClassification,
    SenderIdentity,
)
from titan.delivery import sender_pool
from titan.delivery.suppression import is_suppressed
from titan.intelligence.message_validator import prohibited_content
from titan.intelligence.reply_drafter import BLANK
from titan.outreach import unsubscribe

#: The drafter's blank marker, up to its first brace: "[TODO: ".
_BLANK_PREFIX = BLANK.split("{", 1)[0]

#: Classes that never get an answer from here: the drafter writes none for
#: them, and silence is the right response.
_NO_ANSWER = {"not_interested", "unsubscribe", "complaint", "bounce", "automated"}

#: What the outbox payload says a reply is. Read by the outbox worker.
REPLY_KIND = "reply"


class DeskError(ValueError):
    """A request the desk refuses, with a sentence saying why."""


@dataclass(frozen=True, slots=True)
class DeskItem:
    draft_id: uuid.UUID
    draft_version: int
    lead_id: uuid.UUID
    business_name: str | None
    from_email: str
    received_at: dt.datetime
    their_subject: str | None
    their_words: str
    reply_class: str
    confidence: float
    subject: str
    body: str
    ready_to_send: bool
    status: str


async def waiting(
    session: AsyncSession, *, workspace_id: uuid.UUID, limit: int = 50
) -> list[DeskItem]:
    """Replies with a suggested answer that has not been sent or rejected."""
    rows = (
        await session.execute(
            select(MessageDraft, ReplyClassification, InboundMessage, Organization)
            .join(
                ReplyClassification,
                ReplyClassification.suggested_reply_draft_id == MessageDraft.id,
            )
            .join(
                InboundMessage,
                InboundMessage.id == ReplyClassification.inbound_message_id,
            )
            .join(Lead, Lead.id == MessageDraft.lead_id)
            .join(Organization, Organization.id == Lead.organization_id)
            .where(
                MessageDraft.workspace_id == workspace_id,
                MessageDraft.status.in_(
                    (DraftStatus.GENERATED, DraftStatus.AWAITING_APPROVAL)
                ),
            )
            .order_by(InboundMessage.received_at.desc())
            .limit(limit)
        )
    ).all()
    items: list[DeskItem] = []
    for draft, classification, inbound, org in rows:
        report = draft.validation_report or {}
        items.append(
            DeskItem(
                draft_id=draft.id,
                draft_version=draft.version,
                lead_id=draft.lead_id,
                business_name=org.display_name,
                from_email=inbound.from_email_normalized,
                received_at=inbound.received_at,
                their_subject=inbound.subject,
                their_words=(inbound.body_text or "")[:4000],
                reply_class=classification.reply_class.value,
                confidence=classification.confidence,
                subject=draft.subject,
                body=draft.body_text,
                ready_to_send=bool(draft.validation_passed)
                or bool(report.get("ready_to_send")),
                status=draft.status.value,
            )
        )
    return items


def check_reply_text(subject: str, body: str) -> None:
    """Refuse a reply that is not ready to go to a person.

    Not the full message validator: that one checks a cold first message --
    evidence for every claim, a pitch length band, a footer. A reply answers a
    question somebody asked and carries none of those. What it must not do is
    go out with a blank the drafter left for a human (the price, typically),
    or with the rhetoric no message may contain.
    """
    if not subject.strip():
        raise DeskError("the reply has no subject")
    if not body.strip():
        raise DeskError("the reply is empty")
    if _BLANK_PREFIX in body or _BLANK_PREFIX in subject:
        raise DeskError("the reply still has a blank to fill in (look for [TODO: ...])")
    violation = prohibited_content(f"{subject}\n{body}")
    if violation is not None:
        raise DeskError(
            f"the reply contains something no message may: {violation.detail}"
        )


async def _reply_draft(
    session: AsyncSession, *, workspace_id: uuid.UUID, draft_id: uuid.UUID
) -> tuple[MessageDraft, ReplyClassification, InboundMessage]:
    row = (
        await session.execute(
            select(MessageDraft, ReplyClassification, InboundMessage)
            .join(
                ReplyClassification,
                ReplyClassification.suggested_reply_draft_id == MessageDraft.id,
            )
            .join(
                InboundMessage,
                InboundMessage.id == ReplyClassification.inbound_message_id,
            )
            .where(MessageDraft.id == draft_id, MessageDraft.workspace_id == workspace_id)
        )
    ).first()
    if row is None:
        raise DeskError("no reply draft with that id")
    draft, classification, inbound = row
    if draft.status not in (DraftStatus.GENERATED, DraftStatus.AWAITING_APPROVAL):
        raise DeskError(f"this reply is {draft.status.value} and cannot be changed")
    return draft, classification, inbound


async def edit(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    draft_id: uuid.UUID,
    seen_version: int,
    subject: str,
    body: str,
) -> MessageDraft:
    """Replace the suggestion with the operator's own words, once they pass."""
    draft, _, _ = await _reply_draft(
        session, workspace_id=workspace_id, draft_id=draft_id
    )
    if draft.version != seen_version:
        raise DeskError(
            f"the reply changed since you opened it (you saw v{seen_version}, "
            f"it is now v{draft.version})"
        )
    check_reply_text(subject, body)
    draft.subject = subject.strip()[:500]
    draft.body_text = body.strip() + "\n"
    draft.body_html = None
    draft.validation_passed = True
    draft.validation_report = {
        **(draft.validation_report or {}),
        "source": "reply_desk",
        "edited_by_operator": True,
        "ready_to_send": True,
    }
    draft.status = DraftStatus.AWAITING_APPROVAL
    await session.flush()
    return draft


async def send(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    draft_id: uuid.UUID,
    seen_version: int,
    decided_by: uuid.UUID | None,
    actor_ip: str | None = None,
) -> OutboxMessage:
    """Approve the reply and queue it, threaded under their message."""
    draft, classification, inbound = await _reply_draft(
        session, workspace_id=workspace_id, draft_id=draft_id
    )
    if draft.version != seen_version:
        raise DeskError(
            f"the reply changed since you opened it (you saw v{seen_version}, "
            f"it is now v{draft.version})"
        )
    if classification.reply_class.value in _NO_ANSWER:
        raise DeskError(f"'{classification.reply_class.value}' replies are not answered")
    # Checked again even after an edit: the suggestion itself may be sent
    # unedited when it was complete, and must meet the same bar.
    check_reply_text(draft.subject, draft.body_text)

    channel = await session.get(ContactChannel, draft.contact_channel_id)
    if channel is None:
        raise DeskError("the lead has no address to reply to")
    suppressed = await is_suppressed(
        session, workspace_id=workspace_id, email=channel.normalized_value
    )
    if suppressed is not None:
        raise DeskError(f"the recipient is suppressed ({suppressed.reason.value})")

    sender = await _thread_sender(session, workspace_id, draft, inbound)
    if sender is None:
        raise DeskError("no mailbox can send this reply")

    # Signed, with the postal address, as every commercial message is. Added
    # only when missing, so an operator who wrote their own sign-off keeps it.
    settings = get_settings()
    body = draft.body_text.rstrip()
    address = (sender.mailing_address or "").strip()
    if address and address not in body:
        if settings.owner_name not in body:
            body += f"\n\n{settings.owner_name}"
        body += f"\n{address}"
    draft.body_text = body + "\n"
    # Every draft change before the approval, and none after it: each update
    # bumps the version, and the send gate refuses an approval that covers an
    # earlier version than the one going out.
    draft.status = DraftStatus.QUEUED
    await session.flush()

    seq = int(
        await session.scalar(
            select(func.count())
            .select_from(MessageApproval)
            .where(
                MessageApproval.draft_id == draft.id,
                MessageApproval.draft_version == draft.version,
            )
        )
        or 0
    )
    now = dt.datetime.now(dt.UTC)
    approval = MessageApproval(
        workspace_id=workspace_id,
        draft_id=draft.id,
        draft_version=draft.version,
        decision_seq=seq + 1,
        decision="approved",
        decided_by=decided_by,
        decided_at=now,
        reason="sent from the reply desk",
        actor_ip=actor_ip,
    )
    session.add(approval)
    await session.flush()

    dedupe = f"reply-{draft.id}"
    message = Message(
        workspace_id=workspace_id,
        draft_id=draft.id,
        lead_id=draft.lead_id,
        campaign_id=draft.campaign_id,
        sender_identity_id=sender.id,
        dedupe_key=dedupe,
        to_email=channel.value,
        to_email_normalized=channel.normalized_value,
        to_domain=channel.value_domain or "",
        from_email=sender.from_email,
        subject=draft.subject,
        state=MessageState.QUEUED,
        state_rank=0,
        provider=settings.email_provider,
    )
    session.add(message)
    await session.flush()

    outbox = OutboxMessage(
        workspace_id=workspace_id,
        message_id=message.id,
        draft_id=draft.id,
        approval_id=approval.id,
        campaign_id=draft.campaign_id,
        lead_id=draft.lead_id,
        sender_identity_id=sender.id,
        dedupe_key=dedupe,
        provider_idempotency_key=f"idem-{dedupe}",
        status=OutboxStatus.PENDING,
        to_email_normalized=channel.normalized_value,
        to_domain=channel.value_domain or "",
        next_attempt_at=now,
        payload={
            "kind": REPLY_KIND,
            "to_email": channel.value,
            "from_email": sender.from_email,
            "from_name": sender.from_name,
            "reply_to": sender.reply_to_email,
            "subject": draft.subject,
            "text_body": draft.body_text,
            "html_body": None,
            "headers": thread_headers(inbound),
            # The one-click header every send carries, replies included: the
            # send gate requires it, and an answer is still commercial mail.
            **unsubscribe.headers_for(sender, channel.value, settings),
        },
    )
    session.add(outbox)
    await session.flush()
    return outbox


def thread_headers(inbound: InboundMessage) -> dict[str, str]:
    """In-Reply-To and References, so the answer sits under their message.

    ``provider_inbound_id`` is their RFC 5322 Message-ID for mail read over
    IMAP. References carries what their message itself was replying to, when it
    said, so a client can rebuild the whole thread.
    """
    theirs = (inbound.provider_inbound_id or "").strip()
    if not theirs.startswith("<"):
        return {}
    earlier = str((inbound.raw_payload or {}).get("in_reply_to") or "").strip()
    references = " ".join(x for x in (earlier, theirs) if x.startswith("<"))
    return {"In-Reply-To": theirs, "References": references}


async def _thread_sender(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    draft: MessageDraft,
    inbound: InboundMessage,
) -> SenderIdentity | None:
    """The mailbox they wrote back to, so the conversation keeps one voice.

    Falls back to the campaign's pool when that mailbox is gone or inactive.
    """
    if inbound.in_reply_to_message_id is not None:
        original = await session.get(Message, inbound.in_reply_to_message_id)
        if original is not None and original.sender_identity_id is not None:
            sender = await session.get(SenderIdentity, original.sender_identity_id)
            if sender is not None and sender.is_active:
                return sender
    # cold_mail=False: the placement gate pauses cold mail, never an answer.
    # Without it every mailbox read as unavailable while no test inbox
    # existed, and the desk refused to send at all.
    slots = await sender_pool.load_slots(
        session,
        workspace_id,
        draft.campaign_id,
        now=dt.datetime.now(dt.UTC),
        cold_mail=False,
    )
    chosen = sender_pool.choose(slots).chosen_id
    if chosen is None:
        # Every usable mailbox is "full" -- but full of cold drafts that the
        # placement gate is holding, which count against a mailbox the moment
        # they are queued. One answer to one person is not volume; it goes
        # from the healthiest usable mailbox regardless, and the send-time
        # quota still counts it.
        usable = [s for s in slots if s.available]
        if usable:
            chosen = sorted(
                usable, key=lambda s: (-s.daily_limit, str(s.sender_identity_id))
            )[0].sender_identity_id
    return await session.get(SenderIdentity, chosen) if chosen else None


__all__ = [
    "REPLY_KIND",
    "DeskError",
    "DeskItem",
    "check_reply_text",
    "edit",
    "send",
    "thread_headers",
    "waiting",
]
