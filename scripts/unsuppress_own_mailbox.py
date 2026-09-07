"""Take our own mailbox off our own bounce list.

arslan@ was suppressed on 25 August as a hard bounce, which was true then --
the mailbox did not exist. It exists now: it is one of the five Titan sends
as, renamed from contact@ on 31 August.

This matters beyond tidiness. `titan warmup` gives a new mailbox a history by
sending real mail between the pool's own mailboxes, and a suppressed address
is refused like any other -- so the newest mailbox would be the one unable to
warm up.

Scoped to addresses at our own sending domain that are suppressed for
bouncing, and to nothing else. An unsubscribe is never touched: somebody
asking not to be written to is a different fact, and it does not expire.
"""

import asyncio
import sys

from sqlalchemy import delete, select

from titan.db.models import SenderIdentity, SuppressionEntry, Workspace
from titan.db.session import get_sessionmaker, workspace_unit_of_work


async def main() -> int:
    apply = "--apply" in sys.argv
    async with get_sessionmaker()() as session:
        ws = (
            await session.execute(select(Workspace).where(Workspace.slug == "titan"))
        ).scalar_one()
        ours = {
            e.lower()
            for (e,) in (
                await session.execute(
                    select(SenderIdentity.from_email).where(
                        SenderIdentity.workspace_id == ws.id,
                        SenderIdentity.is_active.is_(True),
                    )
                )
            ).all()
        }
        stale = (
            await session.execute(
                select(SuppressionEntry).where(
                    SuppressionEntry.workspace_id == ws.id,
                    SuppressionEntry.reason == "hard_bounce",
                    SuppressionEntry.normalized_value.in_(tuple(ours)),
                )
            )
        ).scalars().all()

        for entry in stale:
            print(f"  {entry.normalized_value}  suppressed {entry.created_at:%Y-%m-%d} ({entry.reason})")
        if not stale:
            print("  nothing to remove")
            return 0

        if apply:
            async with workspace_unit_of_work(ws.id) as write:
                await write.execute(
                    delete(SuppressionEntry).where(
                        SuppressionEntry.id.in_([e.id for e in stale])
                    )
                )
            print(f"\n{len(stale)} removed")
        else:
            print(f"\n{len(stale)} WOULD be removed. Re-run with --apply")
    return 0


raise SystemExit(asyncio.run(main()))
