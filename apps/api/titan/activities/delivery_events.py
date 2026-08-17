"""Collecting what happened to messages Smartlead sent, and acting on it.

The table, the enum and the fingerprint scheme for these events were all in the
repository and had never held a row, because they were built to receive
callbacks and nothing was configured to send them. Every message the system put
out went unmeasured: no bounce reached the suppression list, no reply stopped a
follow-up, and the deliverability checks scored an empty window.

This activity is the missing half. It pulls the outcomes instead of waiting to
be pushed them, records each one once, and -- only for events it has not seen
before -- hands them to the paths that already know what a bounce or a reply
means.

**The consequences are not reimplemented here.** ``record_bounce``, ``suppress``
and ``record_reply`` are the same functions the IMAP and webhook paths call. A
second implementation of "how many bounces before an address is given up on"
would be free to disagree with the first, and the disagreement would surface as
two addresses treated differently for the same reason.

**A full re-read every run.** Nothing tracks a cursor, because a cursor is a
second source of truth about what has been seen and the fingerprint already is
one. Re-reading a page whose events are all known writes nothing and costs one
request.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from titan.config import get_settings
from titan.db.enums import SmartleadEventType, SuppressionReason
from titan.db.models import Lead, Message, SmartleadWebhookEvent
from titan.db.session import workspace_unit_of_work
from titan.delivery.bounces import BounceKind, record_bounce
from titan.delivery.smartlead_events import DeliveryEvent, events_from_row
from titan.delivery.smartlead_reconcile import dedupe_key, reconcile_send
from titan.delivery.suppression import suppress
from titan.delivery.webhooks import record_reply
from titan.workflows.types import PollDeliveryEventsInput, PollDeliveryEventsResult

logger = logging.getLogger(__name__)

ALL_DELIVERY_EVENT_ACTIVITIES = ["poll_delivery_events"]

#: Rows per statistics request. Smartlead caps the page size; this is the cap.
PAGE_SIZE = 100

#: How many pages one run will walk before stopping, so a campaign with a very
#: long history cannot turn a scheduled job into an unbounded one. The next run
#: starts from the beginning again and the fingerprints make that cheap.
MAX_PAGES = 50

SOURCE = "smartlead_statistics_poll"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _store(
    session: AsyncSession,
    event: DeliveryEvent,
    *,
    workspace_id: uuid.UUID,
    lead_id: uuid.UUID | None,
    received_at: dt.datetime,
) -> bool:
    """Record one event. Returns False when it was already known."""
    stmt = (
        pg_insert(SmartleadWebhookEvent.__table__)  # type: ignore[arg-type]
        .values(
            workspace_id=workspace_id,
            event_fingerprint=event.fingerprint,
            raw_event_type=event.raw_event_type,
            event_type=event.event_type,
            smartlead_campaign_id=event.smartlead_campaign_id,
            normalized_email=event.normalized_email,
            lead_id=lead_id,
            occurred_at=event.occurred_at,
            received_at=received_at,
            # Polled, not pushed. There is no signature on a response to a
            # request this process made, and claiming one was verified would
            # make the column mean two different things.
            signature_verified=False,
            raw_payload=event.raw,
            ignored=lead_id is None,
            ignored_reason=None if lead_id else "no lead matches this address",
        )
        .on_conflict_do_nothing(index_elements=["event_fingerprint"])
        .returning(SmartleadWebhookEvent.__table__.c.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _apply(
    session: AsyncSession,
    event: DeliveryEvent,
    *,
    workspace_id: uuid.UUID,
    lead_id: uuid.UUID | None,
    now: dt.datetime,
    message_id: uuid.UUID | None = None,
) -> None:
    """What this event means for whether the address is contacted again."""
    if event.event_type is SmartleadEventType.BOUNCED:
        # Recorded as soft, though Smartlead does not say which it was. A soft
        # bounce that is really hard bounces again on the next send -- a new
        # statistics row, a new fingerprint -- and the existing escalation rule
        # suppresses it. Reading every bounce as hard would take the opposite
        # risk: one temporarily full mailbox, permanently given up on, with
        # nothing to distinguish it from an address that never existed.
        await record_bounce(
            session,
            workspace_id=workspace_id,
            to_email=event.normalized_email,
            kind=BounceKind.SOFT,
            source=SOURCE,
            source_reference=event.fingerprint,
            lead_id=lead_id,
            # Named explicitly. Without it record_bounce falls back to the most
            # recent message to the address, so a lead that bounced on two
            # sequence steps would stamp one message twice and leave the other
            # unmarked -- understating the soft-bounce count for exactly the
            # addresses bouncing most.
            message_id=message_id,
            now=now,
        )
        return

    if event.event_type is SmartleadEventType.UNSUBSCRIBED:
        await suppress(
            session,
            workspace_id=workspace_id,
            email_or_domain=event.normalized_email,
            reason=SuppressionReason.UNSUBSCRIBE,
            source=SOURCE,
            source_reference=event.fingerprint,
            now=now,
        )
        return

    if event.event_type is SmartleadEventType.REPLIED and lead_id is not None:
        # A response arrived. That is all this event knows: the statistics row
        # carries `reply_time` and no body, so nothing here can tell an
        # interested prospect from an out-of-office -- and the only reply this
        # workspace has ever received is an out-of-office.
        #
        # So the timestamp is recorded and the sequence is left running.
        # `collect_smartlead_replies` fetches the message itself, and
        # `ingest_inbound` stops the sequence when the class says a person is
        # actually there. Stopping here would halt outreach to everybody who
        # sets an autoresponder, and count each one as a success.
        await record_reply(
            session,
            workspace_id=workspace_id,
            lead_id=lead_id,
            replied_at=event.occurred_at,
            stops_sequence=False,
        )


async def _unstamped_message(
    session: AsyncSession, *, workspace_id: uuid.UUID, stats_id: str
) -> uuid.UUID | None:
    """Whether this send exists but has no bounce recorded against it.

    The question is asked of the ``messages`` row rather than of the event,
    because the message is what ``record_bounce`` stamps and what the soft-bounce
    count reads. Once stamped, this is False and the consequence cannot fire a
    second time -- which is what keeps an hourly full re-read from re-suppressing
    an address every hour.
    """
    if not stats_id:
        return None
    message = (
        await session.execute(
            select(Message).where(
                Message.workspace_id == workspace_id,
                Message.dedupe_key == dedupe_key(stats_id),
            )
        )
    ).scalar_one_or_none()
    if message is None or message.bounced_at is not None:
        return None
    return message.id


@activity.defn(name="poll_delivery_events")
async def poll_delivery_events(
    request: PollDeliveryEventsInput,
) -> PollDeliveryEventsResult:
    """Read every campaign's delivery outcomes and record the new ones."""
    from titan.providers.smartlead import SmartleadClient, SmartleadError

    settings = get_settings()
    if not settings.smartlead_api_key:
        return PollDeliveryEventsResult(unavailable="no Smartlead API key is configured")

    workspace_id = uuid.UUID(request.workspace_id)
    client = SmartleadClient.from_settings(settings)
    now = _now()
    rows_read = 0
    recorded = 0
    reconciled = 0
    healed = 0
    unattributed = 0
    by_type: dict[str, int] = {}

    try:
        campaigns = await client.list_campaigns()
        async with workspace_unit_of_work(workspace_id) as session:
            leads = {
                email: lead_id
                for lead_id, email in (
                    await session.execute(
                        select(Lead.id, Lead.smartlead_normalized_email).where(
                            Lead.smartlead_normalized_email.is_not(None)
                        )
                    )
                ).all()
                if email
            }

            for campaign in campaigns:
                campaign_id = str(campaign.id)
                for page in range(MAX_PAGES):
                    try:
                        rows, _total = await client.campaign_statistics(
                            int(campaign.id),
                            offset=page * PAGE_SIZE,
                            limit=PAGE_SIZE,
                        )
                    except SmartleadError as exc:
                        logger.warning(
                            "could not read a campaign's statistics",
                            extra={
                                "smartlead_campaign_id": campaign_id,
                                "error_code": type(exc).__name__,
                            },
                        )
                        break
                    if not rows:
                        break
                    rows_read += len(rows)

                    for row in rows:
                        events = events_from_row(row, campaign_id=campaign_id)
                        if not events:
                            continue
                        lead_id = leads.get(events[0].normalized_email)

                        # The send is reconciled into `messages` before its own
                        # events are applied, and the order is load-bearing: a
                        # bounce is only counted when it can be tied to a send,
                        # so a row carrying both would otherwise have its bounce
                        # discarded on the same pass that created the send.
                        message_id: uuid.UUID | None = None
                        sent = next(
                            (
                                e
                                for e in events
                                if e.event_type is SmartleadEventType.SENT
                            ),
                            None,
                        )
                        if lead_id is not None and sent is not None:
                            lead = await session.get(Lead, lead_id)
                            if lead is not None:
                                outcome = await reconcile_send(
                                    session,
                                    workspace_id=workspace_id,
                                    lead=lead,
                                    stats_id=str(row.get("stats_id") or ""),
                                    to_email=str(row.get("lead_email") or ""),
                                    subject=row.get("email_subject"),
                                    sequence_number=sent.sequence_number,
                                    sent_at=sent.occurred_at,
                                )
                                message_id = outcome.message_id
                                if outcome.created:
                                    reconciled += 1
                                elif outcome.skipped_reason:
                                    logger.info(
                                        "could not reconcile a Smartlead send",
                                        extra={
                                            "reason": outcome.skipped_reason,
                                            "stats_id": str(row.get("stats_id") or ""),
                                        },
                                    )

                        for event in events:
                            inserted = await _store(
                                session,
                                event,
                                workspace_id=workspace_id,
                                lead_id=lead_id,
                                received_at=now,
                            )
                            if not inserted:
                                # Already recorded -- but a consequence only
                                # ever fires on an event's first sighting, and
                                # a bounce first seen before its send existed
                                # was correctly declined and would never be
                                # retried. Re-apply exactly those, identified
                                # by the send still carrying no bounce stamp,
                                # so the ordering heals itself instead of
                                # needing a backfill.
                                if (
                                    lead_id is not None
                                    and event.event_type is SmartleadEventType.BOUNCED
                                    and (
                                        pending := await _unstamped_message(
                                            session,
                                            workspace_id=workspace_id,
                                            stats_id=str(row.get("stats_id") or ""),
                                        )
                                    )
                                    is not None
                                ):
                                    await _apply(
                                        session,
                                        event,
                                        workspace_id=workspace_id,
                                        lead_id=lead_id,
                                        now=now,
                                        message_id=pending,
                                    )
                                    healed += 1
                                continue
                            recorded += 1
                            by_type[event.event_type.value] = (
                                by_type.get(event.event_type.value, 0) + 1
                            )
                            if lead_id is None:
                                # Kept, not discarded: an event for an address
                                # no lead claims is evidence of a real send and
                                # of a real gap in attribution.
                                unattributed += 1
                                continue
                            await _apply(
                                session,
                                event,
                                workspace_id=workspace_id,
                                lead_id=lead_id,
                                now=now,
                                message_id=message_id,
                            )

                    if len(rows) < PAGE_SIZE:
                        break
    except Exception as exc:
        logger.warning(
            "delivery event poll could not complete",
            extra={"error_code": type(exc).__name__},
        )
        return PollDeliveryEventsResult(unavailable=str(exc))
    finally:
        await client.aclose()

    return PollDeliveryEventsResult(
        rows_read=rows_read,
        recorded=recorded,
        reconciled=reconciled,
        healed=healed,
        unattributed=unattributed,
        detail=tuple(f"{name}={count}" for name, count in sorted(by_type.items())),
    )


__all__ = [
    "ALL_DELIVERY_EVENT_ACTIVITIES",
    "MAX_PAGES",
    "PAGE_SIZE",
    "poll_delivery_events",
]
