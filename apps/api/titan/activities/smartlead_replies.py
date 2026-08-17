"""Collect replies from Smartlead and put them through Titan's inbound path.

Titan's inbound path is complete and has never run. ``ingest_inbound``
classifies a reply, halts the sequence when a human answered, suppresses on an
unsubscribe or a complaint, opens a meeting on a request to talk, notifies, and
records a ``reply_classification`` -- which is what the A/B test, campaign
health and the budget allocator all read. Its only intake is IMAP, IMAP is not
configured, and so every one of those reads a zero.

A zero read as evidence is worse than no reading at all. ``positive_replies``
of zero across every arm makes the A/B test permanently inconclusive, which is
survivable; ``replied_at`` counted without a class makes an out-of-office a
success, which is not.

**Nothing here classifies anything.** It fetches, translates and hands over.
Every decision -- what the reply means, whether to stop, whether to suppress,
whether a meeting exists -- stays in ``ingest_inbound``, so a reply collected
here and one collected over IMAP are treated identically. A second opinion
about what "not interested" means is the thing most worth not having.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from temporalio import activity

from titan.config import get_settings
from titan.db.models import Campaign, ContactChannel, Lead
from titan.db.session import workspace_unit_of_work
from titan.delivery.inbound import ingest_inbound
from titan.delivery.smartlead_replies import leads_with_replies, replies_from_history
from titan.providers.smartlead import SmartleadClient, SmartleadError
from titan.workflows.types import CollectRepliesInput, CollectRepliesResult

logger = logging.getLogger(__name__)

#: Recorded on every inbound row this path writes, so a reply collected here is
#: distinguishable afterwards from one collected over IMAP. They are treated
#: identically; being able to tell them apart is for diagnosing a gap in either.
PROVIDER = "smartlead"

#: Statistics rows per request. The endpoint pages, and a campaign that has been
#: running for months has thousands of rows.
PAGE_SIZE = 100


async def _lead_for(
    session: object, *, workspace_id: uuid.UUID, email: str
) -> uuid.UUID | None:
    """The lead this address belongs to, if Titan knows it.

    Matched through the lead's primary channel, which is the direct foreign
    key. None is a real answer -- Smartlead holds leads Titan never imported --
    and
    ``ingest_inbound`` handles it: suppression is keyed on the address and still
    happens, only the sequence stop needs to know which lead.
    """
    return (
        await session.execute(  # type: ignore[attr-defined]
            select(Lead.id)
            .join(ContactChannel, ContactChannel.id == Lead.primary_contact_channel_id)
            .where(
                Lead.workspace_id == workspace_id,
                ContactChannel.normalized_value == email.strip().lower(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


@activity.defn(name="collect_smartlead_replies")
async def collect_smartlead_replies(
    request: CollectRepliesInput,
) -> CollectRepliesResult:
    """One pass over every carrier campaign, ingesting replies not seen before."""
    settings = get_settings()
    if settings.smartlead_api_key is None:
        return CollectRepliesResult(refused_reason="no Smartlead API key is configured")

    workspace_id = uuid.UUID(request.workspace_id)

    async with workspace_unit_of_work(workspace_id) as session:
        carriers = [
            int(row)
            for row in (
                await session.execute(
                    select(Campaign.smartlead_campaign_id)
                    .where(
                        Campaign.workspace_id == workspace_id,
                        Campaign.smartlead_campaign_id.is_not(None),
                    )
                    .distinct()
                )
            ).scalars()
            if row is not None
        ]
    # The configured default is included as well, because it is where every
    # message sent before per-market routing existed went -- the replies to
    # those are the only history this workspace has, and no campaign row names
    # that carrier.
    #
    # **Only for a workspace that already routes to a carrier of its own.**
    # TITAN_SMARTLEAD_CAMPAIGN_ID is process-wide and names no owner, so
    # applying it unconditionally hands every workspace in the database the same
    # Smartlead campaign. Observed exactly that: a leftover test workspace runs
    # its own copy of this schedule, read the real workspace's carrier, and
    # ingested a stranger's reply as its own. The workspace guard cannot catch
    # it -- the write is correctly scoped, it is the *source* that belongs to
    # somebody else.
    if settings.smartlead_campaign_id is not None and carriers:
        carriers.append(int(settings.smartlead_campaign_id))
    carriers = sorted(set(carriers))
    if not carriers:
        return CollectRepliesResult(refused_reason="no carrier campaign is configured")

    client = SmartleadClient.from_settings(settings)
    seen = 0
    ingested = 0
    unmatched = 0
    try:
        for carrier in carriers:
            try:
                rows, _ = await client.campaign_statistics(carrier, limit=PAGE_SIZE)
            except SmartleadError as exc:
                logger.warning(
                    "could not read statistics for a carrier campaign",
                    extra={"carrier": carrier, "error": str(exc)[:200]},
                )
                continue

            for address in leads_with_replies(rows):
                # Guarded: this also runs straight from an operator command,
                # where there is no activity context and heartbeating raises
                # partway through a batch.
                if activity.in_activity():
                    activity.heartbeat(f"{ingested} ingested")
                try:
                    lead_row = await client.lead_by_email(address)
                except SmartleadError as exc:
                    # One address Smartlead cannot resolve must not end the pass
                    # for every other reply in the account.
                    logger.warning(
                        "could not look up a replying lead",
                        extra={"carrier": carrier, "error": str(exc)[:200]},
                    )
                    continue
                smartlead_lead_id = (lead_row or {}).get("id")
                if smartlead_lead_id is None:
                    continue
                try:
                    history = await client.lead_message_history(
                        carrier, int(smartlead_lead_id)
                    )
                except SmartleadError as exc:
                    logger.warning(
                        "could not read a lead's message history",
                        extra={"carrier": carrier, "error": str(exc)[:200]},
                    )
                    continue

                for reply in replies_from_history(history, fallback_from=address):
                    seen += 1
                    # One transaction per reply. Ingest halts sequences,
                    # suppresses addresses and opens meetings, and a batch that
                    # failed halfway would leave some of those applied and the
                    # rest not, with no record of where it stopped.
                    async with workspace_unit_of_work(workspace_id) as session:
                        lead_id = await _lead_for(
                            session,
                            workspace_id=workspace_id,
                            email=reply.message.from_email,
                        )
                        if lead_id is None:
                            unmatched += 1
                        result = await ingest_inbound(
                            session,
                            workspace_id=workspace_id,
                            message=reply.message,
                            lead_id=lead_id,
                            provider=PROVIDER,
                            provider_inbound_id=reply.provider_inbound_id,
                            received_at=reply.received_at,
                        )
                    if not result.duplicate:
                        ingested += 1
                        logger.info(
                            "ingested a reply Smartlead had and Titan did not",
                            extra={
                                "carrier": carrier,
                                "reply_class": getattr(result.reply_class, "value", None),
                                "lead_matched": lead_id is not None,
                            },
                        )
    finally:
        await client.aclose()

    return CollectRepliesResult(
        carriers=len(carriers), seen=seen, ingested=ingested, unmatched=unmatched
    )


ALL_SMARTLEAD_REPLY_ACTIVITIES = [collect_smartlead_replies]

__all__ = [
    "ALL_SMARTLEAD_REPLY_ACTIVITIES",
    "PROVIDER",
    "collect_smartlead_replies",
]
