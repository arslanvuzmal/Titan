"""Who actually hosts the mail for the list we send to.

The probe answers UNKNOWN for Google, Microsoft and the big filtering
front-ends, deliberately -- they accept every recipient at the door, so a
probe learns nothing. That is the right call, but it means the share of the
list behind those operators is a hard ceiling on what probing can ever
verify. Nobody had measured it.
"""

import asyncio
import collections
import sys

from sqlalchemy import select

from titan.db.models import ContactChannel, Workspace
from titan.db.session import get_sessionmaker
from titan.intelligence.mx import system_mx_resolver, check_mx
from titan.intelligence.smtp_probe import UNINFORMATIVE_MX_SUFFIXES


def operator(hosts: tuple[str, ...]) -> str:
    for h in hosts:
        low = h.lower().rstrip(".")
        for suffix in UNINFORMATIVE_MX_SUFFIXES:
            if low.endswith(suffix):
                return suffix
    return "probeable"


async def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    async with get_sessionmaker()() as session:
        ws = (
            await session.execute(select(Workspace).where(Workspace.slug == "titan"))
        ).scalar_one()
        domains = [
            d
            for (d,) in (
                await session.execute(
                    select(ContactChannel.value_domain)
                    .where(
                        ContactChannel.workspace_id == ws.id,
                        ContactChannel.channel_type == "email",
                        ContactChannel.is_active.is_(True),
                        ContactChannel.value_domain.is_not(None),
                    )
                    .distinct()
                )
            ).all()
        ]

    print(f"{len(domains)} distinct recipient domains; sampling {min(limit, len(domains))}\n")
    counts: collections.Counter[str] = collections.Counter()
    for d in domains[:limit]:
        try:
            check = check_mx(d, resolver=system_mx_resolver)
        except Exception:
            counts["lookup_failed"] += 1
            continue
        if check.is_conclusively_undeliverable:
            counts["NO MX - every address bounces"] += 1
            continue
        counts[operator(tuple(check.hosts))] += 1

    total = sum(counts.values())
    for name, n in counts.most_common():
        print(f"  {n:5d}  {100*n/total:5.1f}%  {name}")
    probeable = counts.get("probeable", 0)
    print(f"\nprobe can give a real answer for {probeable}/{total} = {100*probeable/total:.0f}%")
    return 0


raise SystemExit(asyncio.run(main()))
