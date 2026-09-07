"""Undo policy-page downgrades that matched the domain instead of the page.

The first version of the rule searched the whole URL, so a solicitor whose
domain contains "legal" -- clarkslegal.com, mueller.legal, burneylegal.co.uk --
was downgraded whatever page the address came from, most of them from their own
/contact/ page. 46 of the 141, and solicitors are one of the larger verticals
on this list.

Re-resolves every RISKY address under the corrected rule and restores the ones
that no longer raise the signal. Only that one signal is in scope: an address
held back for any other reason keeps its status, because nothing about those
was wrong.
"""

import asyncio
import datetime as dt
import sys

from sqlalchemy import select, update

from titan.db.enums import VerificationStatus
from titan.db.models import ContactChannel, ContactVerification, Workspace
from titan.db.session import get_sessionmaker, workspace_unit_of_work
from titan.intelligence.bounce_risk import assess


async def main() -> int:
    apply = "--apply" in sys.argv
    async with get_sessionmaker()() as session:
        ws = (
            await session.execute(select(Workspace).where(Workspace.slug == "titan"))
        ).scalar_one()
        rows = (
            await session.execute(
                select(
                    ContactChannel.id,
                    ContactChannel.normalized_value,
                    ContactChannel.source,
                    ContactChannel.source_url,
                ).where(
                    ContactChannel.workspace_id == ws.id,
                    ContactChannel.channel_type == "email",
                    ContactChannel.is_active.is_(True),
                    ContactChannel.verification_status == VerificationStatus.RISKY,
                )
            )
        ).all()

    restored, kept = [], []
    for channel_id, email, source, source_url in rows:
        risk = assess(email=email, source=source, source_url=source_url)
        if "policy_page_source" in {s.code for s in risk.signals}:
            kept.append(email)
            continue
        restored.append((channel_id, email, risk, source_url))

    print(f"{len(rows)} currently risky: {len(kept)} still policy-page, "
          f"{len(restored)} to restore\n")
    for _, email, risk, url in restored[:12]:
        print(f"  {email:36.36s} <- {url[:52]}")

    if apply:
        for channel_id, email, risk, source_url in restored:
            async with workspace_unit_of_work(ws.id) as write:
                await write.execute(
                    update(ContactChannel)
                    .where(ContactChannel.id == channel_id)
                    .values(verification_status=risk.status)
                )
                detail = risk.as_verification_detail()
                detail["repair"] = "policy_page_rule_matched_the_domain_not_the_page"
                detail["source_url"] = source_url
                write.add(
                    ContactVerification(
                        workspace_id=ws.id,
                        channel_id=channel_id,
                        provider="bounce_risk",
                        result=risk.status,
                        detail=detail,
                        verified_at=dt.datetime.now(dt.UTC),
                    )
                )
        print(f"\n{len(restored)} restored")
    else:
        print("\nre-run with --apply")
    return 0


raise SystemExit(asyncio.run(main()))
