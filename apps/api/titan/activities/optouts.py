"""Bring the website's opt-outs into Titan's suppression list.

The unsubscribe endpoint records a request and returns 200. That satisfies the
recipient and RFC 8058, and on its own it changes nothing here: the address is
still in the campaign, still eligible, and the next message goes out anyway.
Recording an opt-out and honouring it are two different things, and only the
second is the promise the footer makes.

**Titan pulls; nothing pushes to Titan.** The endpoint is on the public web and
this system is not, so the direction is chosen by what is reachable rather than
by preference -- and it happens to be the safer of the two, since nothing
outside can reach in to assert that somebody unsubscribed.

Idempotent by way of ``suppress``, which is keyed on the address. Re-reading the
whole list every run is the point: it costs one request and removes any
dependence on having seen an earlier one.
"""

from __future__ import annotations

import logging
import uuid

import httpx
from temporalio import activity

from titan.config import get_settings
from titan.db.enums import SuppressionReason
from titan.db.session import workspace_unit_of_work
from titan.delivery.suppression import is_suppressed, suppress
from titan.outreach import unsubscribe
from titan.workflows.types import PullOptOutsInput, PullOptOutsResult

logger = logging.getLogger(__name__)

SOURCE = "unsubscribe-endpoint"

#: The endpoint answers from a key-value store; a slow reply means it is down,
#: not busy, and the next run is a minute away.
TIMEOUT_SECONDS = 20


@activity.defn(name="pull_opt_outs")
async def pull_opt_outs(request: PullOptOutsInput) -> PullOptOutsResult:
    """Read every opt-out the site holds and suppress the ones we have not."""
    settings = get_settings()
    secret = settings.unsubscribe_secret
    if secret is None:
        return PullOptOutsResult(refused_reason="TITAN_UNSUBSCRIBE_SECRET is not set")

    base = str(settings.owner_portfolio_url).rstrip("/")
    workspace_id = uuid.UUID(request.workspace_id)

    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.get(
            f"{base}/api/unsubscribe",
            params={"list": "1"},
            headers={"Authorization": unsubscribe.bearer(secret)},
        )
    if response.status_code != 200:
        # Raised, not swallowed. An endpoint that has started refusing is a
        # system quietly mailing people who asked it to stop, and the retry
        # policy is what turns a transient failure into a recovered one.
        raise RuntimeError(
            f"opt-out list unavailable: {response.status_code} {response.text[:120]}"
        )

    payload = response.json()
    emails = [str(e).strip().lower() for e in (payload.get("emails") or []) if e]

    suppressed = 0
    async with workspace_unit_of_work(workspace_id) as session:
        for email in emails:
            if await is_suppressed(session, workspace_id=workspace_id, email=email):
                continue
            await suppress(
                session,
                workspace_id=workspace_id,
                email_or_domain=email,
                reason=SuppressionReason.UNSUBSCRIBE,
                source=SOURCE,
                detail={"via": "one-click or link on the portfolio"},
            )
            suppressed += 1

    if suppressed:
        logger.info(
            "honoured opt-outs collected by the website",
            extra={"found": len(emails), "newly_suppressed": suppressed},
        )
    return PullOptOutsResult(found=len(emails), suppressed=suppressed)


ALL_OPTOUT_ACTIVITIES = [pull_opt_outs]

__all__ = ["ALL_OPTOUT_ACTIVITIES", "SOURCE", "pull_opt_outs"]
