"""Reconciling each sending mailbox's daily limit to what it has earned.

The decision itself is in :mod:`titan.delivery.mailbox_ramp` and is pure. This
is the part that talks to Smartlead and to the database: read the mailboxes, the
remembered ceilings and the delivery evidence, ask for a decision, and write
back only the limits that changed.

**The ceiling is remembered here, not read from the provider.** Smartlead has
one number per mailbox and this activity writes it, so reading the ceiling back
out of it would feed the ramp its own output -- see
:func:`titan.delivery.mailbox_ramp.observe_ceiling`. What the provider is asked
for is the *current* limit; whether that number represents a human's intent is
decided by comparing it against what this activity last wrote.

**Only mailboxes a campaign actually sends from.** An account that exists in
Smartlead but is attached to no campaign is not part of outreach, and one of
them is deliberately not part of outreach. Writing a daily limit to it would be
this system reaching into a mailbox nobody pointed it at.

**Nothing is written unless it changed.** A no-op POST is still a write to
somebody else's system, and a daily job that touches every mailbox whether or
not anything moved makes the audit trail useless for finding the day something
did.

**Fails soft, and says so.** A provider that cannot be reached leaves every
limit exactly as it found it. The alternative -- guessing at a safe number and
writing it -- would let an outage in Smartlead's API silently reconfigure
sending.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import select
from temporalio import activity

from titan.config import get_settings
from titan.db.models import MailboxRampState
from titan.db.session import workspace_unit_of_work
from titan.delivery import mailbox_ramp
from titan.delivery.deliverability import ReputationWindow
from titan.workflows.types import RampMailboxesInput, RampMailboxesResult

logger = logging.getLogger(__name__)

ALL_MAILBOX_RAMP_ACTIVITIES = ["ramp_mailboxes"]

#: Which system these mailboxes live in. Stored alongside the external id so a
#: second provider's numeric ids cannot collide with Smartlead's.
PROVIDER = "smartlead"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _first_send(row: dict[str, Any]) -> dt.datetime | None:
    """When this mailbox first sent, as far as the provider knows.

    Falls back to the account's creation time, and then to nothing. A mailbox
    whose age cannot be established is treated as new -- week 0 -- because the
    conservative reading of "I don't know how old this is" is "assume it is
    young", not "assume it has been running for months".
    """
    for key in ("first_sent_at", "created_at"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _evidence(analytics: dict[str, Any]) -> ReputationWindow:
    """Delivery outcomes as the reputation checks want them.

    ``delivered`` is derived as sent minus bounced rather than read: Smartlead
    reports what it sent and what came back, and there is no separate delivered
    count. Subtracting is the honest reading of the two numbers that exist.
    """
    sent = int(analytics.get("sent_count") or 0)
    bounced = int(analytics.get("bounce_count") or 0)
    complained = int(analytics.get("unsubscribed_count") or 0) + int(
        analytics.get("block_count") or 0
    )
    return ReputationWindow(
        sent=sent,
        delivered=max(0, sent - bounced),
        hard_bounced=bounced,
        complained=complained,
    )


@activity.defn(name="ramp_mailboxes")
async def ramp_mailboxes(request: RampMailboxesInput) -> RampMailboxesResult:
    """Move every sending mailbox one step along its ramp, if it has earned it."""
    from titan.providers.smartlead import SmartleadClient, SmartleadError

    settings = get_settings()
    if not settings.smartlead_api_key:
        return RampMailboxesResult(unavailable="no Smartlead API key is configured")

    workspace_id = uuid.UUID(request.workspace_id)
    client = SmartleadClient.from_settings(settings)
    now = _now()
    try:
        accounts = await client.list_email_accounts()

        # Evidence is per campaign, and a mailbox may serve several. Summed
        # across the campaigns it is attached to, because a mailbox's reputation
        # is a property of the mailbox rather than of any one campaign using it.
        #
        # The same pass collects which accounts any campaign sends from. An
        # account in neither set is not part of outreach and is left alone.
        totals: dict[str, int] = {}
        attached: set[str] = set()
        for campaign in await client.list_campaigns():
            try:
                stats = await client.campaign_analytics(int(campaign.id))
            except SmartleadError:
                continue
            for key in (
                "sent_count",
                "bounce_count",
                "unsubscribed_count",
                "block_count",
            ):
                totals[key] = totals.get(key, 0) + int(stats.get(key) or 0)
            try:
                for row in await client.campaign_email_accounts(int(campaign.id)):
                    if row.get("id") is not None:
                        attached.add(str(row["id"]))
            except SmartleadError:
                # Attachment could not be established for this campaign. Its
                # mailboxes stay out of the set, so the failure withholds a
                # write rather than causing one.
                logger.warning(
                    "could not read a campaign's mailboxes; they will not be ramped",
                    extra={"smartlead_campaign_id": str(campaign.id)},
                )

        evidence = _evidence(totals)
        sending = [row for row in accounts if str(row.get("id") or "") in attached]

        async with workspace_unit_of_work(workspace_id) as session:
            states = {
                state.external_id: state
                for state in (
                    await session.execute(
                        select(MailboxRampState).where(
                            MailboxRampState.provider == PROVIDER
                        )
                    )
                )
                .scalars()
                .all()
            }

            decisions = []
            for row in sending:
                external_id = str(row["id"])
                observed = int(row.get("message_per_day") or 0)
                state = states.get(external_id)
                ceiling = mailbox_ramp.observe_ceiling(
                    observed=observed,
                    stored_ceiling=state.ceiling if state else None,
                    last_written=state.last_written_limit if state else None,
                )
                decision = mailbox_ramp.decide(
                    mailbox=str(row.get("from_email") or external_id),
                    ceiling=ceiling,
                    current=observed,
                    first_send_at=_first_send(row),
                    now=now,
                    evidence=evidence,
                )
                decisions.append((row, state, decision))

            applied = 0
            for row, state, decision in decisions:
                external_id = str(row["id"])
                if state is None:
                    state = MailboxRampState(
                        workspace_id=workspace_id,
                        provider=PROVIDER,
                        external_id=external_id,
                        from_email=str(row.get("from_email") or ""),
                        ceiling=decision.ceiling,
                    )
                    session.add(state)
                else:
                    state.ceiling = decision.ceiling

                if not decision.changed or not request.apply:
                    continue
                try:
                    await client.set_daily_limit(int(external_id), decision.target)
                except (SmartleadError, ValueError, KeyError) as exc:
                    logger.warning(
                        "could not set a mailbox daily limit",
                        extra={
                            "mailbox": decision.mailbox,
                            "error_code": type(exc).__name__,
                        },
                    )
                    continue
                # Recorded only after the provider accepted it. Remembering a
                # write that failed would make the next run read the operator's
                # untouched number as the ramp's own and stop treating it as a
                # ceiling.
                state.last_written_limit = decision.target
                state.last_written_at = now
                applied += 1
    except Exception as exc:
        logger.warning(
            "mailbox ramp could not run; every limit stands",
            extra={"error_code": type(exc).__name__},
        )
        return RampMailboxesResult(unavailable=str(exc))
    finally:
        await client.aclose()

    made = [d for _, _, d in decisions]
    return RampMailboxesResult(
        considered=len(made),
        changed=applied,
        raised=sum(1 for d in made if d.direction == "up"),
        lowered=sum(1 for d in made if d.direction == "down"),
        detail=tuple(d.describe() for d in made),
    )


__all__ = ["ALL_MAILBOX_RAMP_ACTIVITIES", "PROVIDER", "ramp_mailboxes"]
