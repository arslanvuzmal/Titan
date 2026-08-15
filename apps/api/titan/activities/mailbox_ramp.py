"""Reconciling each sending mailbox's daily limit to what it has earned.

The decision itself is in :mod:`titan.delivery.mailbox_ramp` and is pure. This
is the part that talks to Smartlead: read the mailboxes and the delivery
evidence, ask for a decision, and write back only the limits that changed.

**The ceiling is whatever a human last set, read fresh every run.** It is not
stored here and not remembered between runs, which is deliberate: a stored
ceiling would drift from the one an operator can see in the provider's UI, and
the number they can see is the one they think is true. Raising it there raises
the ramp's headroom; lowering it below the current limit cuts the mailbox on the
next run.

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
from typing import Any

from temporalio import activity

from titan.config import get_settings
from titan.delivery import mailbox_ramp
from titan.delivery.deliverability import ReputationWindow
from titan.workflows.types import RampMailboxesInput, RampMailboxesResult

logger = logging.getLogger(__name__)

ALL_MAILBOX_RAMP_ACTIVITIES = ["ramp_mailboxes"]


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

    client = SmartleadClient.from_settings(settings)
    now = _now()
    try:
        accounts = await client.list_email_accounts()

        # Evidence is per campaign, and a mailbox may serve several. Summed
        # across the campaigns it is attached to, because a mailbox's reputation
        # is a property of the mailbox rather than of any one campaign using it.
        totals: dict[str, int] = {}
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

        evidence = _evidence(totals)
        decisions = [
            mailbox_ramp.decide(
                mailbox=str(row.get("from_email") or row.get("id")),
                ceiling=int(row.get("message_per_day") or 0),
                current=int(row.get("message_per_day") or 0),
                first_send_at=_first_send(row),
                now=now,
                evidence=evidence,
            )
            for row in accounts
        ]

        applied = 0
        for row, decision in zip(accounts, decisions, strict=True):
            if not decision.changed or not request.apply:
                continue
            try:
                await client.set_daily_limit(int(row["id"]), decision.target)
                applied += 1
            except (SmartleadError, ValueError, KeyError) as exc:
                logger.warning(
                    "could not set a mailbox daily limit",
                    extra={
                        "mailbox": decision.mailbox,
                        "error_code": type(exc).__name__,
                    },
                )
    except Exception as exc:
        logger.warning(
            "mailbox ramp could not run; every limit stands",
            extra={"error_code": type(exc).__name__},
        )
        return RampMailboxesResult(unavailable=str(exc))
    finally:
        await client.aclose()

    return RampMailboxesResult(
        considered=len(decisions),
        changed=applied,
        raised=sum(1 for d in decisions if d.direction == "up"),
        lowered=sum(1 for d in decisions if d.direction == "down"),
        detail=tuple(d.describe() for d in decisions),
    )


__all__ = ["ALL_MAILBOX_RAMP_ACTIVITIES", "ramp_mailboxes"]
