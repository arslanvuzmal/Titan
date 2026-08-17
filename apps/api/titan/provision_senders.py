"""Give every campaign a mailbox to send from.

    python -m titan.provision_senders --apply

Twenty-three campaigns existed and twenty-two of them had an empty sending
pool. Every draft they produced was refused at the last step with "the campaign
has no sending mailboxes" -- after discovery had paid for the lead, the crawler
had audited the site, a model had composed the message and the validator had
passed it. All of that work, stopped by a missing row in a join table.

Two gaps, both from the same cause. ``campaign_senders`` replaced the single
``sender_identity_id`` column, and everything that *creates* a campaign still
sets the old column: the pool is what selection reads, so a campaign created
after the change starts with nowhere to send from. And a mailbox connected in
Smartlead is not a mailbox Titan knows about -- ``sales@`` was carrying half the
account's daily capacity and had no row here at all, so Titan could not use it
and could not report it missing.

**The mailbox list comes from Smartlead.** It is the system that actually holds
the connections, and a second list maintained here would be wrong the first time
one was added or removed there. ``projects@`` is excluded under the operator's
standing rule.

**Nothing here asserts a mailbox is safe to send from.** A new identity is
created unverified, with ``domain_verified`` false, and the existing daily
verification workflow is what proves SPF, DKIM and DMARC and flips it. The
outbox refuses an unverified sender, so a mailbox added here cannot send until
that check has actually run and passed.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from titan.config import get_settings
from titan.db.models import Campaign, CampaignSender, SenderIdentity, Workspace
from titan.db.session import dispose_engine, get_sessionmaker
from titan.outreach.smartlead_markets import excluded_mailboxes
from titan.providers.smartlead import SmartleadClient
from titan.provision_smartlead import FORBIDDEN_MAILBOXES
from titan.runtime import configure_event_loop


@dataclass(frozen=True, slots=True)
class Mailbox:
    from_email: str
    daily_limit: int


def mailboxes_from(accounts: list[dict[str, Any]]) -> list[Mailbox]:
    """The outreach mailboxes Smartlead holds, in a shape Titan can store.

    Filtered through the same helper the campaign attachment uses, so a mailbox
    Titan may not send from cannot arrive here by a different route.
    """
    allowed = set(excluded_mailboxes(accounts, forbidden=set(FORBIDDEN_MAILBOXES)))
    found: list[Mailbox] = []
    for account in accounts:
        identifier = account.get("id")
        address = str(account.get("from_email") or "").strip().lower()
        if identifier is None or int(identifier) not in allowed or not address:
            continue
        limit = account.get("message_per_day")
        found.append(
            Mailbox(
                from_email=address,
                daily_limit=int(limit) if isinstance(limit, int | float) else 50,
            )
        )
    return found


async def _ensure_identities(
    session: Any, *, workspace_id: uuid.UUID, mailboxes: list[Mailbox]
) -> list[str]:
    """One sender identity per outreach mailbox. Idempotent on the address."""
    notes: list[str] = []
    template = (
        await session.execute(
            select(SenderIdentity)
            .where(SenderIdentity.workspace_id == workspace_id)
            .order_by(SenderIdentity.domain_verified.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    for mailbox in mailboxes:
        existing = (
            await session.execute(
                select(SenderIdentity).where(
                    SenderIdentity.workspace_id == workspace_id,
                    SenderIdentity.from_email == mailbox.from_email,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            notes.append(f"  = {mailbox.from_email:<34} exists")
            continue

        local, _, domain = mailbox.from_email.partition("@")
        session.add(
            SenderIdentity(
                workspace_id=workspace_id,
                label=local,
                from_email=mailbox.from_email,
                from_name=(template.from_name if template else None),
                reply_to_email=mailbox.from_email,
                sending_domain=domain,
                # False on purpose. The daily verification workflow resolves the
                # real TXT records and flips this; asserting it here would let a
                # mailbox send on the strength of nothing but its address.
                domain_verified=False,
                mailing_address=(template.mailing_address if template else None),
                unsubscribe_mailto=(template.unsubscribe_mailto if template else None),
                daily_send_limit=mailbox.daily_limit,
                is_active=True,
            )
        )
        notes.append(f"  + {mailbox.from_email:<34} created, unverified")
    return notes


async def _attach_to_campaigns(
    session: Any, *, workspace_id: uuid.UUID
) -> tuple[int, int]:
    """Put every active mailbox in every campaign's pool.

    Selection sends through whichever mailbox has the most room left, so a
    campaign holding the whole pool spreads its batch instead of filling one
    mailbox and deferring the rest.
    """
    senders = list(
        (
            await session.execute(
                select(SenderIdentity).where(
                    SenderIdentity.workspace_id == workspace_id,
                    SenderIdentity.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    campaigns = list(
        (
            await session.execute(
                select(Campaign).where(
                    Campaign.workspace_id == workspace_id,
                    Campaign.target_geography.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    existing = {
        (row.campaign_id, row.sender_identity_id)
        for row in (
            await session.execute(
                select(CampaignSender).where(CampaignSender.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    }

    added = 0
    for campaign in campaigns:
        for sender in senders:
            if (campaign.id, sender.id) in existing:
                continue
            session.add(
                CampaignSender(
                    workspace_id=workspace_id,
                    campaign_id=campaign.id,
                    sender_identity_id=sender.id,
                )
            )
            added += 1
    return len(campaigns), added


async def provision(workspace_slug: str, *, apply: bool) -> int:
    settings = get_settings()
    if settings.smartlead_api_key is None:
        raise SystemExit("TITAN_SMARTLEAD_API_KEY is not configured")

    client = SmartleadClient.from_settings(settings)
    try:
        accounts = await client.list_email_accounts()
    finally:
        await client.aclose()
    mailboxes = mailboxes_from(accounts)

    async with get_sessionmaker()() as session, session.begin():
        workspace = (
            await session.execute(
                select(Workspace).where(Workspace.slug == workspace_slug)
            )
        ).scalar_one_or_none()
        if workspace is None:
            raise SystemExit(f"workspace {workspace_slug!r} does not exist")

        print(f"workspace: {workspace_slug} ({workspace.id})")
        print("dry run -- nothing written" if not apply else "applied")
        for mailbox in mailboxes:
            print(f"  mailbox {mailbox.from_email:<34} {mailbox.daily_limit}/day")

        if not apply:
            await session.rollback()
            return 0

        for note in await _ensure_identities(
            session, workspace_id=workspace.id, mailboxes=mailboxes
        ):
            print(note)
        await session.flush()
        campaigns, added = await _attach_to_campaigns(session, workspace_id=workspace.id)
        print(f"  pooled  {campaigns} campaigns, {added} new attachments")

    print()
    print("A mailbox created here is unverified and cannot send until the daily")
    print("verification workflow has proved its SPF, DKIM and DMARC records.")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="titan")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    code = await provision(args.workspace, apply=args.apply)
    await dispose_engine()
    return code


if __name__ == "__main__":
    configure_event_loop()
    raise SystemExit(asyncio.run(main()))
