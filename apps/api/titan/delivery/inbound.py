"""Acting on an inbound email, and recording that it arrived.

Joins :mod:`titan.intelligence.replies`, which decides what an arriving message
means, to ``record_reply`` and ``suppress``, which already existed and already
did the right thing. Nothing here re-implements either: this module only
decides which to call, and writes down what it decided.

The residual risk the verification report recorded -- "replies must be recorded
by hand, so invariant 15 protects only leads somebody entered" -- is what this
closes.

Collection is deliberately left to the caller. A webhook route, an IMAP poller
or an operator pasting a message all produce an :class:`InboundMessage`, and
all get identical treatment, so the rules cannot drift between intake paths.

**Recording is not optional.** ``inbound_messages`` and ``reply_classifications``
existed in the schema from the first migration and had no writer, so acting on
a reply left no trace of the reply itself: a suppressed address with nothing
saying who asked, and a lead marked ``replied`` with no reply attached. Every
ingest now writes the message and the classification in the caller's
transaction, before anything is suppressed. Either both land or neither does --
suppressing without the evidence that justified it is the one ordering that
must be impossible.

That record is also what makes re-delivery safe. ``provider_inbound_id`` is
unique per workspace, so a poller that re-reads a folder, a webhook that fires
twice, or an operator pasting the same message again all collapse to one row
and one set of effects.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from titan.config import get_settings
from titan.db.enums import DraftStatus, ReplyClass, SuppressionReason
from titan.db.models.lead import Lead
from titan.db.models.messaging import InboundMessage as InboundMessageRow
from titan.db.models.messaging import MessageDraft
from titan.db.models.messaging import ReplyClassification as ReplyClassificationRow
from titan.db.models.ops import Meeting
from titan.delivery.bounces import BounceKind, record_bounce
from titan.delivery.suppression import suppress
from titan.delivery.webhooks import record_reply
from titan.intelligence.contacts import normalize_email
from titan.intelligence.intent import IntentVerdict, detect_intent
from titan.intelligence.replies import (
    InboundMessage,
    ReplyClassification,
    ReplyKind,
    classify_reply,
    is_hard_bounce,
)
from titan.intelligence.reply_drafter import ReplyDraftContext, draft_reply
from titan.notify.operator import (
    NotificationKind,
    OperatorNotification,
    record_notification,
)

logger = logging.getLogger(__name__)

#: What each classification suppresses for. A bounce only appears here when it
#: is permanent; a soft bounce suppresses nothing.
_SUPPRESSION_REASONS: dict[ReplyKind, SuppressionReason] = {
    ReplyKind.UNSUBSCRIBE: SuppressionReason.UNSUBSCRIBE,
    ReplyKind.COMPLAINT: SuppressionReason.COMPLAINT,
    ReplyKind.BOUNCE: SuppressionReason.HARD_BOUNCE,
}

#: How a :class:`ReplyKind` -- what the envelope-level rules can prove -- maps to
#: the richer :class:`ReplyClass` the schema stores.
#:
#: ``HUMAN`` is absent on purpose: it is the one kind that carries an intent, and
#: it is resolved by :func:`titan.intelligence.intent.detect_intent` instead of
#: by a fixed mapping. Everything else is a machine speaking, where the envelope
#: is the whole answer.
_REPLY_CLASSES: dict[ReplyKind, ReplyClass] = {
    ReplyKind.AUTO: ReplyClass.AUTOMATED,
    ReplyKind.BOUNCE: ReplyClass.BOUNCE,
    ReplyKind.UNSUBSCRIBE: ReplyClass.UNSUBSCRIBE,
    ReplyKind.COMPLAINT: ReplyClass.COMPLAINT,
}

#: Which notification a resolved human reply produces.
_NOTIFICATION_FOR: dict[ReplyClass, NotificationKind] = {
    ReplyClass.INTERESTED: NotificationKind.CLIENT_AGREED,
    ReplyClass.WANTS_PRICING: NotificationKind.CLIENT_AGREED,
    ReplyClass.WANTS_CALL: NotificationKind.CLIENT_AGREED,
    ReplyClass.WANTS_MORE_INFO: NotificationKind.CLIENT_AGREED,
    ReplyClass.NOT_INTERESTED: NotificationKind.REPLY_DECLINED,
    ReplyClass.OBJECTION: NotificationKind.REPLY_NEEDS_READING,
    ReplyClass.NOT_NOW: NotificationKind.REPLY_NEEDS_READING,
    ReplyClass.WRONG_PERSON: NotificationKind.REPLY_NEEDS_READING,
    ReplyClass.REFERRAL: NotificationKind.REPLY_NEEDS_READING,
    ReplyClass.UNKNOWN: NotificationKind.REPLY_NEEDS_READING,
}

#: Only a *confident* rejection suppresses. ``SUPPRESSING_REPLY_CLASSES`` maps
#: NOT_INTERESTED to a suppression reason, and honouring it is what stops a
#: person who said no in one campaign from hearing from a different one next
#: month -- the behaviour that generates complaints and kills sending domains.
#:
#: But suppression is close to permanent, and these are regexes. An ambiguous
#: read ("not interested in the SEO work, but very interested in the booking
#: fix") resolves to UNKNOWN and suppresses nothing, so the expensive mistake
#: needs an unambiguous rejection to happen at all. The sequence stops either
#: way; this only decides whether *future* campaigns are blocked too.
MIN_CONFIDENCE_TO_SUPPRESS = 0.8

#: Bodies are stored for operator review, not archived wholesale. A mail loop or
#: a quoted thread can run to megabytes, and the classification only ever reads
#: the opening. Truncation is recorded in the row so a reader knows the text is
#: partial rather than assuming the sender stopped there.
MAX_STORED_BODY_CHARS = 64_000

_SUBJECT_COLUMN_LIMIT = 500
_EMAIL_COLUMN_LIMIT = 320


@dataclass(frozen=True, slots=True)
class IngestResult:
    classification: ReplyClassification
    sequence_stopped: bool
    suppressed: bool
    #: The ``inbound_messages`` row this message was recorded as. Set even for a
    #: duplicate, where it points at the row the first delivery created.
    inbound_message_id: uuid.UUID | None = None
    #: True when this message had already been ingested and nothing was applied
    #: a second time. Not an error: it is the normal result of re-reading a
    #: folder or a provider retrying a webhook.
    duplicate: bool = False
    #: What the person asked for. Only set on a human reply; a bounce has no
    #: intent and an out-of-office has not been read by anybody yet.
    intent: IntentVerdict | None = None
    #: Recorded in the same transaction, waiting to be pushed. The caller pushes
    #: it *after* committing -- see titan.notify.operator for why that order.
    notification: OperatorNotification | None = None
    #: Set when the reply asked for a call. The meeting is *proposed*, with no
    #: time on it -- see :func:`_record_meeting`.
    meeting_id: uuid.UUID | None = None

    @property
    def kind(self) -> ReplyKind:
        return self.classification.kind

    @property
    def reply_class(self) -> ReplyClass:
        """The stored class, resolved the same way the recorded row was."""
        if self.intent is not None:
            return self.intent.reply_class
        return _REPLY_CLASSES.get(self.classification.kind, ReplyClass.UNKNOWN)


def synthetic_inbound_id(message: InboundMessage) -> str:
    """A stable id for a message that arrived without one.

    Derived from sender, subject and body, so the *same* message presented
    twice collapses to one row. Two genuinely different replies with byte-identical
    text from the same person would also collapse -- accepted deliberately,
    because at that point they are indistinguishable from a duplicate delivery,
    and the sequence has already stopped on the first one either way. Dropping a
    second identical "yes" costs nothing; suppressing an address twice from one
    complaint corrupts the audit trail.
    """
    digest = hashlib.sha256(
        "\x00".join(
            (
                normalize_email(message.from_email or ""),
                message.subject or "",
                message.body_text or "",
            )
        ).encode("utf-8", "replace")
    ).hexdigest()
    return f"sha256:{digest}"


def _confidence(classification: ReplyClassification) -> float:
    """How much the deterministic rules actually established.

    Header evidence is near-certain: a mail system sets ``Auto-Submitted``
    deliberately and a human client does not. Wording evidence is weaker, and
    the human verdict is weakest of all -- it is reached by the *absence* of
    automation markers, which is evidence of nothing beyond "no rule fired".
    Recording that honestly is what lets a later model know it may overrule
    this without discarding a confident answer.
    """
    signals = classification.signals
    if any(s.startswith("header:") or s == "dsn_content_type" for s in signals):
        return 0.95
    if classification.kind is ReplyKind.HUMAN:
        return 0.5
    return 0.75


async def ingest_inbound(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    message: InboundMessage,
    lead_id: uuid.UUID | None,
    provider: str = "imap",
    provider_inbound_id: str | None = None,
    in_reply_to_message_id: uuid.UUID | None = None,
    suppression_target: str | None = None,
    received_at: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> IngestResult:
    """Record one inbound message and apply what it implies.

    ``lead_id`` may be None when the message could not be matched to a lead --
    a bounce for an address in two campaigns, say. Suppression still happens,
    because it is keyed on the address rather than on the lead; only the
    sequence stop needs to know which lead.

    ``suppression_target`` is the address the message is *about*, when that is
    not the address it came from. A bounce is the case that matters: the sender
    is ``MAILER-DAEMON@`` at the receiving host, and suppressing that would
    leave the mailbox that actually failed still in rotation while blocking a
    postmaster address nobody was writing to. The collector knows the real
    recipient -- from the DSN's ``Final-Recipient`` or from the original message
    it threaded against -- and passes it here. Defaults to the sender, which is
    correct for every human-originated class.

    ``provider_inbound_id`` should be whatever the collecting path can offer as
    a stable identity: an RFC 5322 ``Message-ID`` for IMAP, the provider's event
    id for a webhook. When it is absent one is derived from the content, so no
    intake path can skip deduplication by forgetting to pass it.

    Must be called inside a transaction the caller commits. Everything here --
    the record, the classification, the suppression, the sequence stop -- lands
    together or not at all.
    """
    moment = now or dt.datetime.now(dt.UTC)
    arrived = received_at or moment
    classification = classify_reply(message)

    inbound_id, is_new = await _record_message(
        session,
        workspace_id=workspace_id,
        message=message,
        lead_id=lead_id,
        provider=provider,
        provider_inbound_id=provider_inbound_id or synthetic_inbound_id(message),
        in_reply_to_message_id=in_reply_to_message_id,
        received_at=arrived,
    )

    if is_new and inbound_id is None:  # pragma: no cover - defensive
        # RETURNING gave us a row but no id, which cannot happen against a
        # healthy table. Raising rolls the transaction back, so the reply stays
        # unread and is retried, rather than being acted on with no record.
        raise RuntimeError("inbound message insert returned no id")

    if not is_new or inbound_id is None:
        # Already ingested. The effects below are not repeated: re-running
        # record_reply would move replied_at forward to the moment we happened
        # to re-read the folder, quietly rewriting when the person answered.
        logger.info(
            "inbound email already ingested; no effects re-applied",
            extra={
                "inbound_message_id": str(inbound_id) if inbound_id else None,
                "classification": classification.kind.value,
            },
        )
        return IngestResult(
            classification=classification,
            sequence_stopped=False,
            suppressed=False,
            inbound_message_id=inbound_id,
            duplicate=True,
        )

    # Intent is only asked of a human reply. Running it on a bounce or an
    # out-of-office would read the quoted original -- Titan's own pitch, written
    # to be persuasive -- and report the prospect as enthusiastic about it.
    intent: IntentVerdict | None = None
    if classification.kind is ReplyKind.HUMAN:
        intent = detect_intent(message.subject or "", message.body_text or "")
        reply_class = intent.reply_class
        stored_confidence = intent.confidence
        summary = intent.detail
    else:
        reply_class = _REPLY_CLASSES[classification.kind]
        stored_confidence = _confidence(classification)
        summary = classification.detail

    suggested_draft_id = await _suggest_reply(
        session,
        workspace_id=workspace_id,
        lead_id=lead_id,
        inbound_id=inbound_id,
        message=message,
        intent=intent,
    )

    meeting_id = await _record_meeting(
        session,
        workspace_id=workspace_id,
        lead_id=lead_id,
        inbound_id=inbound_id,
        message=message,
        intent=intent,
    )

    session.add(
        ReplyClassificationRow(
            workspace_id=workspace_id,
            inbound_message_id=inbound_id,
            reply_class=reply_class,
            confidence=stored_confidence,
            decided_by="rules",
            summary=summary,
            suggested_reply_draft_id=suggested_draft_id,
        )
    )

    suppressed = False
    reason = _SUPPRESSION_REASONS.get(classification.kind)

    # A bounce is decided by titan.delivery.bounces, which the webhook path also
    # calls. Both used to answer "suppress or not" for themselves, and a soft
    # bounce fell out of both -- correct the first time, wrong by the fifth. The
    # escalation rule now lives in one place and this defers to it, then falls
    # through to the ordinary notification and logging below.
    bounce_target = suppression_target or message.from_email
    is_bounce = classification.kind is ReplyKind.BOUNCE and bool(bounce_target)
    if is_bounce:
        outcome = await record_bounce(
            session,
            workspace_id=workspace_id,
            to_email=bounce_target,
            kind=(BounceKind.HARD if is_hard_bounce(classification) else BounceKind.SOFT),
            source="inbound_email",
            message_id=in_reply_to_message_id,
            lead_id=lead_id,
            detail={
                "signals": list(classification.signals),
                "subject": message.subject[:200],
                "inbound_message_id": str(inbound_id),
                "envelope_sender": normalize_email(message.from_email or ""),
            },
            now=moment,
        )
        suppressed = outcome.suppressed
        reason = outcome.reason

    should_suppress = classification.requires_suppression and not is_bounce

    # A confident "not interested" also suppresses, so a person who declined one
    # campaign is not approached by the next one. Ambiguity never does.
    if (
        not should_suppress
        and intent is not None
        and intent.reply_class is ReplyClass.NOT_INTERESTED
        and intent.confidence >= MIN_CONFIDENCE_TO_SUPPRESS
    ):
        should_suppress = True
        reason = SuppressionReason.NOT_INTERESTED

    target = suppression_target or message.from_email
    if should_suppress and reason is not None and target:
        await suppress(
            session,
            workspace_id=workspace_id,
            email_or_domain=target,
            reason=reason,
            source="inbound_email",
            detail={
                "signals": list(classification.signals)
                + list(intent.signals if intent else ()),
                "subject": message.subject[:200],
                "classification": classification.kind.value,
                "reply_class": reply_class.value,
                "inbound_message_id": str(inbound_id),
                "envelope_sender": normalize_email(message.from_email or ""),
            },
            now=moment,
        )
        suppressed = True

    stopped = False
    if classification.stops_the_sequence and lead_id is not None:
        # arrived, not moment: the sequence stopped when they wrote, not when we
        # got around to reading it. A poller that was down for a day would
        # otherwise record every reply as having landed the minute it restarted.
        await record_reply(
            session, workspace_id=workspace_id, lead_id=lead_id, replied_at=arrived
        )
        stopped = True

    notification = await _notify(
        session,
        workspace_id=workspace_id,
        message=message,
        lead_id=lead_id,
        inbound_id=inbound_id,
        classification=classification,
        intent=intent,
        now=moment,
    )

    logger.info(
        "inbound email classified",
        extra={
            "classification": classification.kind.value,
            "signals": list(classification.signals),
            "reply_class": reply_class.value,
            "sequence_stopped": stopped,
            "suppressed": suppressed,
            "lead_id": str(lead_id) if lead_id else None,
            "inbound_message_id": str(inbound_id),
            "notified": notification is not None,
            "meeting_id": str(meeting_id) if meeting_id else None,
        },
    )
    return IngestResult(
        classification=classification,
        sequence_stopped=stopped,
        suppressed=suppressed,
        inbound_message_id=inbound_id,
        intent=intent,
        notification=notification,
        meeting_id=meeting_id,
    )


async def _record_meeting(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    lead_id: uuid.UUID | None,
    inbound_id: uuid.UUID,
    message: InboundMessage,
    intent: IntentVerdict | None,
) -> uuid.UUID | None:
    """Open a meeting when somebody asks to talk.

    ``meetings`` is the last of the tables that had no writer, and this is the
    moment it is for: a reply reading ``WANTS_CALL`` is a lead that reached the
    stage the pipeline exists to reach. Without a row, that stage is visible
    only as a notification somebody has already ticked off, and a fortnight
    later nothing can answer "how many conversations did this campaign start".

    **No time is parsed, deliberately.** Replies name times in every form
    English allows -- "Tuesday afternoon", "the 3rd", "next week sometime",
    "after the bank holiday" -- and each is relative to a timezone, a working
    week and a calendar nobody here can see. A wrong ``scheduled_at`` does not
    read as a parsing failure; it reads as a confirmed appointment, and the cost
    lands on the operator who misses it and the prospect who was stood up. So
    the row is ``proposed`` with the request quoted verbatim in ``notes``, and a
    person puts the time in. ``duration_minutes`` is left null for the same
    reason: nobody agreed to thirty minutes.

    Idempotency is inherited rather than re-implemented. Everything here runs
    only when the inbound insert was new, which the unique constraint on
    ``(workspace_id, provider_inbound_id)`` decides -- so a re-read folder
    cannot open a second meeting. ``external_ref`` records which message opened
    this one, so the link survives into the CRM.
    """
    if lead_id is None or intent is None:
        return None
    if intent.reply_class is not ReplyClass.WANTS_CALL:
        return None

    request = intent.excerpt or (message.body_text or "").strip()[:300]
    row = Meeting(
        workspace_id=workspace_id,
        lead_id=lead_id,
        status="proposed",
        scheduled_at=None,
        duration_minutes=None,
        location_or_link=None,
        external_ref=f"inbound:{inbound_id}"[:255],
        notes=(
            f"Requested by {message.from_email or 'the recipient'} in reply to "
            f"{message.subject[:200]!r}.\n\n"
            f"They wrote: {request}\n\n"
            "No time has been set: the reply was not parsed for one. Confirm a "
            "slot with them and fill in scheduled_at."
        ),
    )
    session.add(row)
    await session.flush()
    return row.id


async def _suggest_reply(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    lead_id: uuid.UUID | None,
    inbound_id: uuid.UUID,
    message: InboundMessage,
    intent: IntentVerdict | None,
) -> uuid.UUID | None:
    """Draft a suggested response and return its id, if one is warranted.

    **Stored with ``validation_passed=False``, always, and deliberately.**
    ``queue_message`` refuses any draft that did not pass validation, so a reply
    physically cannot reach the outbox on the strength of this function alone. A
    first message is written from evidence a crawler gathered and can be
    approved on that basis; a reply is written into a conversation, where an
    automated system can commit its owner to a price, a scope or a deadline
    nobody agreed to. The flag is the mechanism that keeps a person in that loop
    rather than a convention that they should be.

    Returns None where no draft belongs: an unattributed message, or a clean
    rejection -- see :mod:`titan.intelligence.reply_drafter` for why silence is
    the correct response to the second.
    """
    if lead_id is None or intent is None:
        return None

    lead = await session.get(Lead, lead_id)
    if lead is None or lead.primary_contact_channel_id is None:
        # Without a channel there is nothing to attach a draft to. Rare, and not
        # worth failing the whole ingest over: the notification still fires and
        # an operator can reply by hand.
        return None

    draft = draft_reply(
        ReplyDraftContext(
            reply_class=intent.reply_class,
            original_subject=message.subject or "",
            owner_name=get_settings().owner_name,
            # No name is guessed from the address: "sam@" is as likely to be a
            # shared mailbox as a person (invariant 6).
            contact_first_name=None,
        )
    )
    if draft is None:
        return None

    row = MessageDraft(
        workspace_id=workspace_id,
        lead_id=lead_id,
        campaign_id=lead.campaign_id,
        contact_channel_id=lead.primary_contact_channel_id,
        idempotency_key=f"reply:{inbound_id}",
        status=DraftStatus.GENERATED,
        subject=draft.subject,
        body_text=draft.body,
        # Empty by design. A reply makes no evidenced claim about the
        # recipient's business; it answers what they asked. An empty claim map
        # is honest, where a fabricated one would let the draft look validated.
        claim_map=[],
        validation_report={
            "source": "reply_drafter",
            "reply_class": intent.reply_class.value,
            "ready_to_send": draft.ready_to_send,
            "rationale": draft.rationale,
            "requires_human_edit": not draft.ready_to_send,
        },
        validation_passed=False,
        template_key=f"reply:{intent.reply_class.value}",
    )
    session.add(row)
    await session.flush()
    return row.id


async def _notify(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    message: InboundMessage,
    lead_id: uuid.UUID | None,
    inbound_id: uuid.UUID,
    classification: ReplyClassification,
    intent: IntentVerdict | None,
    now: dt.datetime,
) -> OperatorNotification | None:
    """Tell the operator, when there is something worth telling them.

    Deliberately silent for the machine classes. A bounce and an out-of-office
    are handled completely and correctly without anybody reading them, and an
    alert per bounce is how a notification channel becomes background noise --
    at which point the one that mattered gets skimmed past too. Deliverability
    problems are reported in aggregate by the weekly report instead, where a
    bounce *rate* is visible in a way individual bounces never are.

    A complaint is the exception among machine-ish classes: it is rare, it is
    serious, and it is the leading indicator of losing a sending domain.
    """
    if classification.kind is ReplyKind.COMPLAINT:
        return await record_notification(
            session,
            workspace_id=workspace_id,
            kind=NotificationKind.DELIVERABILITY_ALERT,
            title=f"Spam complaint from {message.from_email}",
            description=(
                "A recipient reported outreach as spam. Complaints are the "
                "leading indicator of a sending domain being blocklisted; more "
                "than a handful in a week is worth pausing the campaign for.\n\n"
                f"Subject: {message.subject[:200]}"
            ),
            lead_id=lead_id,
            dedupe_key=f"complaint:{inbound_id}",
            now=now,
        )

    if classification.kind is not ReplyKind.HUMAN or intent is None:
        return None

    kind = _NOTIFICATION_FOR.get(intent.reply_class, NotificationKind.REPLY_NEEDS_READING)
    sender = message.from_email or "an unknown address"
    headline = {
        NotificationKind.CLIENT_AGREED: f"{sender} is interested",
        NotificationKind.REPLY_DECLINED: f"{sender} declined",
    }.get(kind, f"{sender} replied and needs a read")

    return await record_notification(
        session,
        workspace_id=workspace_id,
        kind=kind,
        title=f"{headline} - {intent.reply_class.value}",
        description=(
            f"Subject: {message.subject[:200]}\n"
            f"Read as: {intent.reply_class.value} "
            f"(confidence {intent.confidence:.2f}, {intent.detail})\n"
            f"Signals: {', '.join(intent.signals) or 'none'}\n"
            # The sentence the verdict was reached on, above the full body. An
            # operator triaging a queue reads the first three lines of each item
            # and decides; this puts the deciding sentence inside those lines
            # instead of somewhere in the quoted thread below.
            + (f"They wrote: {intent.excerpt}\n" if intent.excerpt else "")
            + f"\n{(message.body_text or '')[:1500]}"
        ),
        lead_id=lead_id,
        # Keyed on the message, not the lead: a second reply in the same thread
        # is a new thing to look at, and collapsing them would hide the follow-up
        # question after somebody had already ticked the first one off.
        dedupe_key=f"reply:{inbound_id}",
        now=now,
    )


async def _record_message(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    message: InboundMessage,
    lead_id: uuid.UUID | None,
    provider: str,
    provider_inbound_id: str,
    in_reply_to_message_id: uuid.UUID | None,
    received_at: dt.datetime,
) -> tuple[uuid.UUID | None, bool]:
    """Insert the inbound row, or report that it was already there.

    Returns ``(id, is_new)``. ``ON CONFLICT DO NOTHING`` rather than a
    read-then-write: two pollers reading the same folder at the same moment
    would both see no row and both insert, and the unique constraint would fail
    the whole transaction. Letting Postgres arbitrate makes the loser a
    duplicate instead of an error.
    """
    body = message.body_text or ""
    truncated = len(body) > MAX_STORED_BODY_CHARS

    inserted = await session.execute(
        pg_insert(InboundMessageRow.__table__)  # type: ignore[arg-type]
        .values(
            workspace_id=workspace_id,
            provider=provider[:40],
            provider_inbound_id=provider_inbound_id[:255],
            in_reply_to_message_id=in_reply_to_message_id,
            lead_id=lead_id,
            from_email_normalized=normalize_email(message.from_email or "")[
                :_EMAIL_COLUMN_LIMIT
            ],
            subject=(message.subject or "")[:_SUBJECT_COLUMN_LIMIT] or None,
            body_text=body[:MAX_STORED_BODY_CHARS],
            received_at=received_at,
            # Untrusted. Retained so a misclassification can be re-examined
            # against what actually arrived, never fed to a model as instruction.
            raw_payload={
                "headers": {
                    str(k)[:200]: str(v)[:2000]
                    for k, v in (message.headers or {}).items()
                },
                "content_type": message.content_type,
                "in_reply_to": message.in_reply_to,
                "body_truncated": truncated,
                "body_length": len(body),
            },
        )
        .on_conflict_do_nothing(
            index_elements=["workspace_id", "provider_inbound_id"],
        )
        .returning(InboundMessageRow.__table__.c.id)
    )
    new_id = inserted.scalar_one_or_none()
    if new_id is not None:
        return new_id, True

    existing = await session.execute(
        InboundMessageRow.__table__.select()  # type: ignore[attr-defined]
        .with_only_columns(InboundMessageRow.__table__.c.id)
        .where(
            InboundMessageRow.__table__.c.workspace_id == workspace_id,
            InboundMessageRow.__table__.c.provider_inbound_id
            == provider_inbound_id[:255],
        )
    )
    return existing.scalar_one_or_none(), False


__all__ = [
    "MAX_STORED_BODY_CHARS",
    "IngestResult",
    "ingest_inbound",
    "synthetic_inbound_id",
]
