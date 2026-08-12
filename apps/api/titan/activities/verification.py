"""Renewing what the send gate depends on.

Phase 6 made ``SenderIdentity.authorization_errors()`` expire a verification
after ``MAX_VERIFICATION_AGE``, which is what turned four hand-set booleans back
into a claim with a lifetime. That half is useless on its own: a gate that
closes and never reopens stops all sending fourteen days after the last time
somebody happened to write a timestamp. This is the other half.

**Flags are written from what DNS said, never from what they said before.** A
domain that has stopped publishing SPF loses ``spf_ok`` on the next run, and an
identity whose domain has been let go stops sending rather than continuing on a
check that was true a fortnight ago. That direction -- downgrade on evidence --
is the entire point, and it is why this runs on a schedule rather than once at
setup.

``domain_verified`` is set from :attr:`DomainAuth.sendable`, which means *this
domain is configured to send mail*: it resolves, it publishes SPF, it publishes
DMARC. It is deliberately **not** a claim of ownership. DNS cannot prove who
owns a domain, and a flag named ``verified`` that quietly meant "exists" is the
kind of overclaim this whole phase exists to remove. Ownership is established
out of band, by controlling the domain well enough to publish those records in
the first place.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid

from sqlalchemy import select
from temporalio import activity

from titan.db.models import SenderIdentity
from titan.db.session import workspace_unit_of_work
from titan.intelligence.sender_auth import DomainAuth, check_domain_auth
from titan.notify.operator import NotificationKind, record_notification
from titan.workflows.types import VerifySendersInput, VerifySendersResult

logger = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@activity.defn(name="verify_sender_identities")
async def verify_sender_identities(
    request: VerifySendersInput,
) -> VerifySendersResult:
    """Re-check every sending domain in a workspace and record what DNS says."""
    workspace_id = uuid.UUID(request.workspace_id)
    now = _now()

    async with workspace_unit_of_work(workspace_id) as session:
        identities = (await session.execute(select(SenderIdentity))).scalars().all()
        if not identities:
            return VerifySendersResult(checked=0)

        # Grouped by domain: twenty identities on one domain is one lookup, not
        # twenty. This is the exact shape of the production data that started
        # all of it.
        domains = sorted(
            {i.sending_domain.strip().lower() for i in identities if i.sending_domain}
        )
        results: dict[str, DomainAuth] = {}
        for domain in domains:
            # to_thread because the resolver is blocking, and an activity that
            # holds the event loop through twenty DNS timeouts starves every
            # other activity on the worker.
            results[domain] = await asyncio.to_thread(check_domain_auth, domain)
            activity.heartbeat(f"checked {domain}")

        newly_broken: list[SenderIdentity] = []
        passing = 0

        for identity in identities:
            auth = results.get((identity.sending_domain or "").strip().lower())
            if auth is None:
                continue

            was_usable = not identity.authorization_errors()

            identity.domain_verified = auth.sendable
            identity.spf_ok = auth.spf_ok
            identity.dmarc_ok = auth.dmarc_ok
            # Only ever set from a key that was actually found. An unknown
            # selector leaves this false, which is honest, and does not block
            # sending -- DomainAuth.sendable does not require it.
            identity.dkim_ok = auth.dkim_ok
            identity.last_verified_at = now

            if auth.sendable:
                passing += 1
            elif was_usable:
                # It was sending this morning and cannot tonight. That is worth
                # waking somebody for; a domain that was never configured is
                # not, or every unfinished setup pages the operator daily.
                newly_broken.append(identity)

            logger.info(
                "sender identity verified",
                extra={
                    "from_email": identity.from_email,
                    "domain": identity.sending_domain,
                    "sendable": auth.sendable,
                    "spf_ok": auth.spf_ok,
                    "dmarc_ok": auth.dmarc_ok,
                    "dkim_ok": auth.dkim_ok,
                    "dkim_conclusive": auth.dkim_conclusive,
                },
            )

        for identity in newly_broken:
            auth = results[(identity.sending_domain or "").strip().lower()]
            await record_notification(
                session,
                workspace_id=workspace_id,
                kind=NotificationKind.DELIVERABILITY_ALERT,
                title=f"{identity.from_email} can no longer send",
                description=(
                    f"Domain {identity.sending_domain} no longer passes the "
                    f"checks a receiver makes. Outreach from this address has "
                    f"stopped.\n\n"
                    f"resolves: {auth.resolved}\n"
                    f"SPF: {auth.spf_ok} ({auth.spf_record or 'no record'})\n"
                    f"DMARC: {auth.dmarc_ok} ({auth.dmarc_record or 'no record'})\n\n"
                    + "\n".join(f"- {note}" for note in auth.notes)
                ),
                lead_id=None,
                # One per identity per day: the condition persists until
                # somebody fixes DNS, and an alert per run is an alert nobody
                # reads by the third one.
                dedupe_key=f"sender-broken:{identity.id}:{now.date().isoformat()}",
                now=now,
            )

    logger.info(
        "sender verification complete",
        extra={
            "workspace_id": str(workspace_id),
            "checked": len(identities),
            "domains": len(domains),
            "passing": passing,
            "newly_broken": len(newly_broken),
        },
    )
    return VerifySendersResult(
        checked=len(identities),
        domains_resolved=len(domains),
        passing=passing,
        failing=len(identities) - passing,
        newly_broken=tuple(sorted(i.from_email for i in newly_broken)),
    )


ALL_VERIFICATION_ACTIVITIES = [verify_sender_identities]

__all__ = ["ALL_VERIFICATION_ACTIVITIES", "verify_sender_identities"]
