"""Admit the 55-69 band into sending.

The gate sat at 70 while 3,481 scored leads sat between 55 and 69 -- the
"manual_review" band, which in practice meant "never looked at". They are not
low-quality: the band is where a lead lands when the evidence is real but
thinner than a top-decile prospect's, and most of them carry five or more
findings.

Lowering the gate does not lower the bar on the *address*: every campaign
keeps require_verified_email, so an address the verifier calls invalid still
cannot be mailed. This changes who is considered, not what is allowed out.
"""

import asyncio
import sys

from sqlalchemy import select

from titan.db.models import Campaign, CampaignPolicy, Workspace
from titan.db.session import get_sessionmaker

NEW_FLOOR = 55


async def main() -> int:
    apply = "--apply" in sys.argv
    async with get_sessionmaker()() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.slug == "titan"))
        ).scalar_one()
        rows = (
            await session.execute(
                select(CampaignPolicy, Campaign)
                .join(Campaign, Campaign.id == CampaignPolicy.campaign_id)
                .where(
                    CampaignPolicy.workspace_id == workspace.id,
                    Campaign.status == "active",
                    CampaignPolicy.min_lead_score > NEW_FLOOR,
                )
            )
        ).all()

        for policy, campaign in rows:
            print(f"  {campaign.name:38.38s} {policy.min_lead_score} -> {NEW_FLOOR}")
            policy.min_lead_score = NEW_FLOOR
            policy.version += 1

        if apply:
            await session.commit()
            print(f"\n{len(rows)} campaign policies lowered to {NEW_FLOOR}")
        else:
            await session.rollback()
            print(f"\n{len(rows)} WOULD be lowered. Re-run with --apply")
    return 0


raise SystemExit(asyncio.run(main()))
