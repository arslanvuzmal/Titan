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
from titan.db.models import Lead, SmartleadWebhookEvent
from titan.db.session import workspace_unit_of_work
from titan.delivery.bounces import BounceKind, record_bounce
from titan.delivery.smartlead_events import DeliveryEvent, events_from_row
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
        # Invariant 15. A human answered; nothing further is sent to them by
        # anything this system schedules.
        await record_reply(
            session,
            workspace_id=workspace_id,
            lead_id=lead_id,
            replied_at=event.occurred_at,
        )


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
                        for event in events_from_row(row, campaign_id=campaign_id):
                            lead_id = leads.get(event.normalized_email)
                            inserted = await _store(
                                session,
                                event,
                                workspace_id=workspace_id,
                                lead_id=lead_id,
                                received_at=now,
                            )
                            if not inserted:
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
        unattributed=unattributed,
        detail=tuple(f"{name}={count}" for name, count in sorted(by_type.items())),
    )


__all__ = [
    "ALL_DELIVERY_EVENT_ACTIVITIES",
    "MAX_PAGES",
    "PAGE_SIZE",
    "poll_delivery_events",
]
