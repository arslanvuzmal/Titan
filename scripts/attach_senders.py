"""Give every active campaign the whole mailbox pool.

Campaigns get their senders at provisioning time, so a campaign created after
the last provisioning run has none -- and a campaign with no sender cannot
send at all, silently. Six active campaigns were in that state, and the two
renamed mailboxes were attached to nothing.

This reuses the provisioning module's own attach step rather than writing the
join rows here, so there is one definition of "which senders does a campaign
get" and this cannot drift from it.
"""

import asyncio
import sys

from titan.db.session import get_sessionmaker
from titan.db.models import Workspace
from titan.provision_senders import _attach_to_campaigns
from sqlalchemy import select


async def main() -> int:
    apply = "--apply" in sys.argv
    async with get_sessionmaker()() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.slug == "titan"))
        ).scalar_one()
        campaigns, added = await _attach_to_campaigns(session, workspace_id=workspace.id)
        if apply:
            await session.commit()
            print(f"{campaigns} campaigns considered, {added} sender links added")
        else:
            await session.rollback()
            print(f"{campaigns} campaigns considered, {added} links WOULD be added")
            print("re-run with --apply")
    return 0


raise SystemExit(asyncio.run(main()))
