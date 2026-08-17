"""Giving Smartlead's sends a record in Titan's own model.

Smartlead delivers; Titan does not know it happened. The consequences are not
cosmetic:

* The CRM is a complete, working view over ``messages`` -- timeline, per-lead
  history, stats -- and shows nothing, because for real outreach that table is
  empty.
* ``record_bounce`` refuses to count a soft bounce it cannot tie to a send,
  which is the right rule and the reason four real bounces went unrecorded.
* ``_campaign_outcomes``, the health windows and the A/B decision all read
  ``messages``. They have been scoring an empty set and calling it evidence.

**These messages were composed here.** Every one of the seventy-two leads
Smartlead holds has drafts in this database -- they were researched, drafted,
validated and approved by Titan, then handed over for delivery. So a ``Message``
row is not a fabrication: ``draft_id`` points at the draft a person actually
approved. That is why this is a reconciliation and not an invention, and it is
why the NOT NULL on ``draft_id`` can be honoured rather than relaxed.

**What is not claimed.** The row records that Smartlead reported a send of this
draft to this address at this time. It does not claim the body Smartlead
transmitted is byte-identical to the draft -- Smartlead owns the rendering, and
follow-up steps in particular were templated there. ``provider_message_id``
carries Smartlead's ``stats_id`` so the two can always be compared.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import DELIVERY_RANK, DraftStatus, MessageState, Region, SubRegion
from titan.db.models import (
    Campaign,
    Lead,
    Message,
    MessageDraft,
    OrganizationLocation,
    SenderIdentity,
)
from titan.policy.schedule import local_time, resolve_timezone

#: Prefix for the dedupe key, so a reconciled row is identifiable as one and can
#: never collide with a key the outbox worker generates.
DEDUPE_PREFIX = "smartlead"


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    created: bool
    message_id: uuid.UUID | None = None
    skipped_reason: str | None = None


def dedupe_key(stats_id: str) -> str:
    """One message per Smartlead statistics row.

    Keyed on ``stats_id`` rather than on the event fingerprint: a row produces
    several events -- sent, opened, bounced -- and they are all facts about the
    same message. Keying on the fingerprint would create one message per event.
    """
    return f"{DEDUPE_PREFIX}:{stats_id}"


async def _draft_for(
    session: AsyncSession,
    *,
    lead_id: uuid.UUID,
    sequence_number: int | None,
    subject: str | None,
) -> MessageDraft | None:
    """The draft this send most likely delivered.

    Three attempts, narrowest first. The subject is tried before the step number
    because Smartlead reports the subject it actually used, whereas the step
    number is its own bookkeeping and is null on a fair share of rows.

    Returns None rather than guessing when nothing matches. A message attributed
    to the wrong draft would put one lead's approved wording against another's
    delivery record, and every downstream read -- the CRM timeline, the variant
    A/B test -- would inherit the error silently.
    """
    drafts = (
        (
            await session.execute(
                select(MessageDraft)
                .where(MessageDraft.lead_id == lead_id)
                .order_by(MessageDraft.created_at)
            )
        )
        .scalars()
        .all()
    )
    if not drafts:
        return None

    if subject:
        wanted = subject.strip().lower()
        for draft in drafts:
            if draft.subject.strip().lower() == wanted:
                return draft

    approved = [d for d in drafts if d.status is DraftStatus.APPROVED]
    if sequence_number and 1 <= sequence_number <= len(approved):
        return approved[sequence_number - 1]

    # Step 1 with no subject match: the first approved draft is the opener.
    return approved[0] if approved else None


async def _sender_for(
    session: AsyncSession, *, workspace_id: uuid.UUID, from_email: str | None
) -> SenderIdentity | None:
    """The sender identity this went out as.

    Falls back to any identity in the workspace when the named mailbox has no
    row -- Smartlead holds mailboxes Titan was never told about, and losing the
    whole send record over a missing sender row would trade a precise gap for a
    total one.

    **Verification is deliberately not required here.** ``domain_verified`` is a
    delivery gate: it decides whether this system may *send* as an address, and
    the outbox still enforces it. This function records mail that a different
    system already delivered. Refusing to write history because the sender is
    unverified would not un-send anything -- it would only mean the bounce that
    send produced could never be counted. Verified identities are still
    preferred, so the fallback lands on the best available rather than the
    oldest.
    """
    if from_email:
        found = (
            await session.execute(
                select(SenderIdentity).where(
                    SenderIdentity.workspace_id == workspace_id,
                    SenderIdentity.from_email == from_email.strip().lower(),
                )
            )
        ).scalar_one_or_none()
        if found is not None:
            return found

    return (
        await session.execute(
            select(SenderIdentity)
            .where(SenderIdentity.workspace_id == workspace_id)
            .order_by(SenderIdentity.domain_verified.desc(), SenderIdentity.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _local_frame(
    session: AsyncSession,
    *,
    lead: Lead,
    campaign_id: uuid.UUID,
    sent_at: dt.datetime,
) -> dict[str, object]:
    """When this send landed in the recipient's own day.

    The same resolution the outbox worker applies to Titan's own sends, reached
    through the same two functions rather than reimplemented -- a second answer
    to "what time was it for them" would disagree with the first exactly where
    the data is thinnest, which is where the learning query needs it most.

    Without this the column exists, is indexed, and holds nothing for the only
    mail the system has actually sent: the outbox stamps it, and everything real
    went out through Smartlead.

    Every field stays None when the clock cannot be resolved. Null reads as
    "unknown" to the learning query; midnight would read as a pile of messages
    sent at 3am, and be acted on.
    """
    empty: dict[str, object] = {
        "local_sent_hour": None,
        "local_sent_weekday": None,
        "sent_timezone": None,
    }

    recipient_timezone: str | None = None
    if lead.organization_id is not None:
        recipient_timezone = (
            await session.execute(
                select(OrganizationLocation.timezone)
                .where(
                    OrganizationLocation.organization_id == lead.organization_id,
                    OrganizationLocation.timezone.is_not(None),
                )
                .order_by(OrganizationLocation.is_primary.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    campaign = await session.get(Campaign, campaign_id)
    timezone = resolve_timezone(
        recipient_timezone,
        campaign.region if campaign else Region.UNSPECIFIED,
        campaign_subregion=campaign.sub_region if campaign else SubRegion.UNSPECIFIED,
    )
    local = local_time(sent_at, timezone)
    if local is None:
        return empty
    return {
        "local_sent_hour": local.hour,
        "local_sent_weekday": local.weekday(),
        "sent_timezone": timezone,
    }


async def reconcile_send(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    lead: Lead,
    stats_id: str,
    to_email: str,
    subject: str | None,
    sequence_number: int | None,
    sent_at: dt.datetime,
    from_email: str | None = None,
) -> ReconcileOutcome:
    """Record one Smartlead send as a Titan message. Idempotent on ``stats_id``."""
    key = dedupe_key(stats_id)
    existing = (
        await session.execute(
            select(Message).where(
                Message.workspace_id == workspace_id, Message.dedupe_key == key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return ReconcileOutcome(created=False, message_id=existing.id)

    draft = await _draft_for(
        session, lead_id=lead.id, sequence_number=sequence_number, subject=subject
    )
    if draft is None:
        return ReconcileOutcome(created=False, skipped_reason="no draft to attribute to")

    sender = await _sender_for(session, workspace_id=workspace_id, from_email=from_email)
    if sender is None:
        return ReconcileOutcome(created=False, skipped_reason="no sender identity")

    normalized = to_email.strip().lower()
    # Stamped at write time, from the moment the send actually happened rather
    # than from now: the recipient's local hour for a message sent last Thursday
    # is a fact about last Thursday.
    frame = await _local_frame(
        session, lead=lead, campaign_id=draft.campaign_id, sent_at=sent_at
    )
    message = Message(
        workspace_id=workspace_id,
        draft_id=draft.id,
        lead_id=lead.id,
        campaign_id=draft.campaign_id,
        sender_identity_id=sender.id,
        dedupe_key=key,
        to_email=to_email,
        to_email_normalized=normalized,
        to_domain=normalized.split("@", 1)[1] if "@" in normalized else "",
        from_email=sender.from_email,
        subject=subject or draft.subject,
        state=MessageState.SENT,
        state_rank=DELIVERY_RANK[MessageState.SENT],
        state_event_at=sent_at,
        sent_at=sent_at,
        provider="smartlead",
        provider_message_id=stats_id,
        **frame,  # type: ignore[arg-type]
    )
    session.add(message)
    await session.flush()
    return ReconcileOutcome(created=True, message_id=message.id)


__all__ = ["DEDUPE_PREFIX", "ReconcileOutcome", "dedupe_key", "reconcile_send"]
