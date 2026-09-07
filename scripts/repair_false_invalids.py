"""Undo INVALID verdicts that a resolver outage produced.

Five addresses were marked INVALID because a bulk MX lookup returned NXDOMAIN
for domains that are live and answering. INVALID is excluded from RECHECKABLE
-- the catch-up pass never revisits a conclusive answer -- so each of these was
a real business discarded permanently, with nothing downstream able to notice.

This re-resolves each one against a *live* MX lookup and writes what the
engine concludes now. It is not a blanket restore: a domain that really is
gone stays INVALID, because that verdict would be correct.

The reason it is safe to re-open these at all is that the cause is fixed --
reverify now treats two contradictory MX readings as "not checked" rather than
letting the negative one win. Without that, these would simply be re-broken by
the next bad minute.
"""

import asyncio
import datetime as dt
import sys

from sqlalchemy import select, update

from titan.db.enums import VerificationStatus, verification_permits_sending
from titan.db.models import ContactChannel, ContactVerification, Workspace
from titan.db.session import get_sessionmaker, workspace_unit_of_work
from titan.intelligence.bounce_risk import assess
from titan.intelligence.mx import check_mx, system_mx_resolver


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
                    ContactChannel.value_domain,
                ).where(
                    ContactChannel.workspace_id == ws.id,
                    ContactChannel.channel_type == "email",
                    ContactChannel.is_active.is_(True),
                    ContactChannel.verification_status == VerificationStatus.INVALID,
                )
            )
        ).all()

    print(f"{len(rows)} addresses currently INVALID\n")
    for channel_id, email, source, source_url, domain in rows:
        mx = check_mx(domain, resolver=system_mx_resolver)
        risk = assess(email=email, source=source, mx=mx, source_url=source_url)
        sendable = verification_permits_sending(risk.status, source)
        verdict = "restored" if risk.status is not VerificationStatus.INVALID else "still invalid"
        print(
            f"  {email:38.38s} mx={mx.status.value:22s} "
            f"-> {risk.status.value:22s} {verdict}"
        )
        if not apply or risk.status is VerificationStatus.INVALID:
            continue

        async with workspace_unit_of_work(ws.id) as write:
            await write.execute(
                update(ContactChannel)
                .where(ContactChannel.id == channel_id)
                .values(verification_status=risk.status)
            )
            detail = risk.as_verification_detail()
            detail["repair"] = "false_nxdomain_reverted"
            detail["mx"] = mx.as_verification_detail()
            write.add(
                ContactVerification(
                    workspace_id=ws.id,
                    channel_id=channel_id,
                    provider="bounce_risk",
                    result=risk.status,
                    mx_present=mx.can_receive_mail,
                    detail=detail,
                    verified_at=dt.datetime.now(dt.UTC),
                )
            )
    if not apply:
        print("\nre-run with --apply")
    return 0


raise SystemExit(asyncio.run(main()))
