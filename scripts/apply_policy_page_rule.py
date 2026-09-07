"""Re-resolve stored addresses now that the page they came from counts.

The policy-page rule is new, so every address found before it existed was
admitted without it -- including 112 that are a lead's primary contact and
would be mailed on the next batch. At the measured 27.8% they would produce
roughly thirty bounces, which is more than enough to block every mailbox for
a month.

Local layers only. No probe, no DNS: the signal being applied is the stored
source_url, which is already in hand, so this costs nothing and touches
nobody's mail server.

A ContactVerification row is written for every address whose status moves,
because that is how a refusal gets explained in three months' time. The table
is append-only by trigger, so this can only add to the record.
"""

import asyncio
import collections
import datetime as dt
import sys

from sqlalchemy import select, update

from titan.db.enums import verification_permits_sending
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
                    ContactChannel.verification_status,
                ).where(
                    ContactChannel.workspace_id == ws.id,
                    ContactChannel.channel_type == "email",
                    ContactChannel.is_active.is_(True),
                )
            )
        ).all()

    moved: list[tuple[str, str, str]] = []
    outcomes: collections.Counter[str] = collections.Counter()

    for channel_id, email, source, source_url, before in rows:
        risk = assess(email=email, source=source, source_url=source_url)
        if risk.status is before:
            outcomes["unchanged"] += 1
            continue
        was = verification_permits_sending(before, source)
        now = verification_permits_sending(risk.status, source)
        if not (was and not now):
            # Only downgrades out of sendability are this pass's business. It
            # must never *raise* a status: every other layer was skipped, so a
            # local-only answer is weaker evidence than whatever is on file.
            outcomes["not a downgrade -- left alone"] += 1
            continue

        outcomes[f"{before.value} -> {risk.status.value}"] += 1
        moved.append((email, before.value, risk.status.value))

        if not apply:
            continue

        async with workspace_unit_of_work(ws.id) as write:
            await write.execute(
                update(ContactChannel)
                .where(ContactChannel.id == channel_id)
                .values(verification_status=risk.status)
            )
            detail = risk.as_verification_detail()
            detail["reassessment"] = "policy_page_rule"
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

    for name, n in outcomes.most_common():
        print(f"  {n:5d}  {name}")
    print(f"\n{len(moved)} addresses {'downgraded' if apply else 'WOULD be downgraded'}")
    for email, before, after in moved[:20]:
        print(f"  {email:52s} {before} -> {after}")
    if not apply:
        print("\nre-run with --apply")
    return 0


raise SystemExit(asyncio.run(main()))
