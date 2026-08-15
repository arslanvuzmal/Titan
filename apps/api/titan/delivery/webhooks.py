"""Provider webhook ingestion.

Three invariants live here:

* **12** -- a duplicate webhook must not produce a duplicate state transition.
  Enforced by ``UNIQUE(provider, provider_event_id)`` on ``provider_events``:
  the insert is attempted first, and a conflict means "already processed".
* **13** -- a delayed webhook must not regress final state. Enforced by
  ``state_rank``: an update only applies when the incoming rank is strictly
  higher, so a late ``sent`` cannot overwrite ``bounced``.
* **16** -- a bounce or complaint suppresses the address.

Ties on rank are broken by provider timestamp, and if those are equal too the
existing state wins, so processing order never changes the outcome.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from titan.config import Settings
from titan.db.enums import (
    DELIVERY_RANK,
    LeadStatus,
    MessageState,
    SuppressionReason,
)
from titan.db.models import Lead, Message, ProviderEvent
from titan.delivery.bounces import BounceKind, record_bounce
from titan.delivery.providers.base import EmailProvider, NormalizedEvent
from titan.delivery.providers.resend import ResendProvider
from titan.delivery.suppression import suppress

logger = logging.getLogger(__name__)

#: States that mean the recipient must never be contacted again.
_SUPPRESSING_STATES: dict[MessageState, SuppressionReason] = {
    MessageState.COMPLAINED: SuppressionReason.COMPLAINT,
    MessageState.UNSUBSCRIBED: SuppressionReason.UNSUBSCRIBE,
}


def verifier_for(provider_name: str, settings: Settings) -> EmailProvider | None:
    """The adapter that can verify this provider's webhooks, if any.

    Lives here rather than in the HTTP route because invariant 1 confines
    concrete email providers to the outbox worker and this module -- an API
    module holding a provider instance would have ``send()`` within reach, which
    is precisely the shape of the pre-0.2 defect that invariant exists to
    prevent. The route gets something that can only verify.

    A fixed table, not a dynamic import by name: a URL path segment must never
    be able to choose what code runs.

    Returns None when the named provider exists but is not configured, so the
    caller can tell an unfinished deployment from a bad signature.
    """
    builders: dict[str, Callable[[Settings], EmailProvider | None]] = {
        "resend": ResendProvider.for_webhook_verification,
    }
    builder = builders.get(provider_name.strip().lower())
    return builder(settings) if builder else None


def is_known_provider(provider_name: str) -> bool:
    """Whether this name is a provider at all, configured or not."""
    return provider_name.strip().lower() in {"resend"}


@dataclass(frozen=True, slots=True)
class WebhookOutcome:
    accepted: bool
    duplicate: bool = False
    state_changed: bool = False
    ignored_reason: str | None = None
    message_id: uuid.UUID | None = None


async def ingest_event(
    session: AsyncSession,
    provider: EmailProvider,
    payload: dict,
    *,
    signature_verified: bool,
    now: dt.datetime | None = None,
) -> WebhookOutcome:
    """Record and apply one provider event. Idempotent.

    The raw payload is stored before interpretation so a mis-parse can be
    replayed later without asking the provider to resend.
    """
    moment = now or dt.datetime.now(dt.UTC)

    event = provider.normalize_webhook(payload)
    if event is None:
        logger.info("ignoring unrecognised webhook event type")
        return WebhookOutcome(accepted=True, ignored_reason="unrecognised_event_type")

    message = await _find_message(session, event)
    if message is None:
        # Store it anyway: an event for an unknown message is worth keeping for
        # diagnosis, and discarding it would hide a real integration problem.
        await _store_event(
            session,
            event,
            workspace_id=None,
            message_id=None,
            signature_verified=signature_verified,
            received_at=moment,
            ignored_reason="no matching message",
        )
        return WebhookOutcome(accepted=True, ignored_reason="no_matching_message")

    inserted = await _store_event(
        session,
        event,
        workspace_id=message.workspace_id,
        message_id=message.id,
        signature_verified=signature_verified,
        received_at=moment,
    )
    if not inserted:
        # Invariant 12: the provider retried; nothing further to do.
        return WebhookOutcome(
            accepted=True,
            duplicate=True,
            message_id=message.id,
            ignored_reason="duplicate_provider_event_id",
        )

    changed = await _apply_state(session, message, event, moment)
    await _apply_side_effects(session, message, event, moment)
    return WebhookOutcome(accepted=True, state_changed=changed, message_id=message.id)


async def _find_message(session: AsyncSession, event: NormalizedEvent) -> Message | None:
    if event.provider_message_id:
        found = (
            await session.execute(
                select(Message).where(
                    Message.provider_message_id == event.provider_message_id
                )
            )
        ).scalar_one_or_none()
        if found is not None:
            return found
    if event.recipient:
        # Fall back to the most recent message to that address: some providers
        # omit the message id on certain event types.
        return (
            await session.execute(
                select(Message)
                .where(Message.to_email_normalized == event.recipient.strip().lower())
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return None


async def _store_event(
    session: AsyncSession,
    event: NormalizedEvent,
    *,
    workspace_id: uuid.UUID | None,
    message_id: uuid.UUID | None,
    signature_verified: bool,
    received_at: dt.datetime,
    ignored_reason: str | None = None,
) -> bool:
    """Insert the raw event. Returns False when it was already recorded."""
    if workspace_id is None:
        # provider_events is workspace-scoped; an orphan event has nowhere to
        # live, so it is logged rather than stored.
        logger.warning(
            "provider event with no matching workspace",
            extra={"provider_event_id": event.provider_event_id},
        )
        return False

    stmt = (
        pg_insert(ProviderEvent.__table__)  # type: ignore[arg-type]
        .values(
            workspace_id=workspace_id,
            provider=event.provider,
            provider_event_id=event.provider_event_id,
            event_type=event.event_type,
            message_id=message_id,
            provider_message_id=event.provider_message_id,
            occurred_at=event.occurred_at,
            received_at=received_at,
            signature_verified=signature_verified,
            raw_payload=event.raw,
            ignored=ignored_reason is not None,
            ignored_reason=ignored_reason,
        )
        .on_conflict_do_nothing(index_elements=["provider", "provider_event_id"])
        .returning(ProviderEvent.__table__.c.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _apply_state(
    session: AsyncSession,
    message: Message,
    event: NormalizedEvent,
    now: dt.datetime,
) -> bool:
    """Advance message state, never regress it (invariant 13)."""
    if event.state is None:
        return False
    incoming_rank = DELIVERY_RANK[event.state]

    if incoming_rank < message.state_rank:
        logger.info(
            "ignoring lower-ranked delivery event",
            extra={
                "message_id": str(message.id),
                "current": message.state.value,
                "incoming": event.state.value,
            },
        )
        return False

    if incoming_rank == message.state_rank:
        # Same rank: prefer the earlier provider timestamp so replays are
        # deterministic regardless of arrival order.
        if (
            message.state_event_at is not None
            and event.occurred_at >= message.state_event_at
        ):
            return False

    values: dict = {
        "state": event.state,
        "state_rank": incoming_rank,
        "state_event_at": event.occurred_at,
    }
    timestamp_column = {
        MessageState.DELIVERED: "delivered_at",
        MessageState.OPENED: "first_opened_at",
        MessageState.BOUNCED: "bounced_at",
        MessageState.COMPLAINED: "complained_at",
    }.get(event.state)
    if timestamp_column:
        values[timestamp_column] = event.occurred_at

    # Conditional UPDATE: two workers processing different events for the same
    # message concurrently cannot interleave into a regression.
    result = await session.execute(
        update(Message)
        .where(Message.id == message.id, Message.state_rank <= incoming_rank)
        .values(**values)
    )
    # CursorResult exposes rowcount; the Result protocol does not.
    return result.rowcount > 0  # type: ignore[attr-defined]


async def _apply_side_effects(
    session: AsyncSession,
    message: Message,
    event: NormalizedEvent,
    now: dt.datetime,
) -> None:
    """Suppression and lead-state consequences (invariants 15, 16)."""
    if event.state is None:
        return

    # Bounces go through titan.delivery.bounces, which is also where the IMAP
    # path sends them. Deciding here as well would be a second implementation of
    # the escalation rule, free to disagree with the first about when a mailbox
    # has refused often enough to give up on.
    if event.state is MessageState.BOUNCED:
        outcome = await record_bounce(
            session,
            workspace_id=message.workspace_id,
            to_email=message.to_email_normalized,
            kind=BounceKind.HARD if event.is_hard_bounce else BounceKind.SOFT,
            source=f"{event.provider}_webhook",
            source_reference=event.provider_event_id,
            message_id=message.id,
            lead_id=message.lead_id,
            now=now,
        )
        if outcome.suppressed:
            logger.info(
                "recipient suppressed from webhook",
                extra={
                    "message_id": str(message.id),
                    "reason": outcome.reason.value if outcome.reason else None,
                    "soft_bounce_count": outcome.soft_bounce_count,
                },
            )
        return

    reason = _SUPPRESSING_STATES.get(event.state)

    if reason is not None:
        await suppress(
            session,
            workspace_id=message.workspace_id,
            email_or_domain=message.to_email_normalized,
            reason=reason,
            source=f"{event.provider}_webhook",
            source_reference=event.provider_event_id,
            now=now,
        )
        await session.execute(
            update(Lead)
            .where(Lead.id == message.lead_id)
            .values(
                status=LeadStatus.SUPPRESSED,
                status_reason=f"{reason.value} via {event.provider} webhook",
            )
        )
        logger.info(
            "recipient suppressed from webhook",
            extra={"message_id": str(message.id), "reason": reason.value},
        )


async def record_reply(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    lead_id: uuid.UUID,
    replied_at: dt.datetime,
) -> None:
    """Stop all future outreach the moment a human replies (invariant 15).

    Sets ``replied_at`` unconditionally but only advances status from a
    non-terminal state, so a later reply cannot resurrect a suppressed lead.
    """
    await session.execute(
        update(Lead)
        .where(Lead.id == lead_id, Lead.workspace_id == workspace_id)
        .values(replied_at=replied_at)
    )
    await session.execute(
        update(Lead)
        .where(
            Lead.id == lead_id,
            Lead.workspace_id == workspace_id,
            Lead.status.notin_([LeadStatus.SUPPRESSED, LeadStatus.DISQUALIFIED]),
        )
        .values(status=LeadStatus.REPLIED)
    )


__all__ = [
    "WebhookOutcome",
    "ingest_event",
    "is_known_provider",
    "record_reply",
    "verifier_for",
]
