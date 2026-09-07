"""Run today's eligibility rules over the whole live list, and change nothing.

Addresses were admitted under whatever rules existed on the day they were
found. Several rules have been added since -- the digit-run prefix that
catches a phone number glued to an address, the lookalike check, the
never-contact list -- and nothing has ever re-applied them to what is already
stored. So the question "how many queued addresses would today's rules refuse"
has never been asked.

Local layers only: syntax, shape, disposable, lookalike, role, and MX. No SMTP
probe, so this costs nothing and touches nobody's mail server.
"""

import asyncio
import collections
import sys

from sqlalchemy import select

from titan.db.enums import VerificationStatus, verification_permits_sending
from titan.db.models import ContactChannel, Workspace
from titan.db.session import get_sessionmaker
from titan.intelligence.bounce_risk import assess
from titan.intelligence.mx import check_mx, system_mx_resolver

try:
    from titan.intelligence.contacts import is_never_contact
except ImportError:
    is_never_contact = None


async def main() -> int:
    check_dns = "--dns" in sys.argv
    async with get_sessionmaker()() as session:
        ws = (
            await session.execute(select(Workspace).where(Workspace.slug == "titan"))
        ).scalar_one()
        rows = (
            await session.execute(
                select(
                    ContactChannel.normalized_value,
                    ContactChannel.source,
                    ContactChannel.verification_status,
                    ContactChannel.value_domain,
                ).where(
                    ContactChannel.workspace_id == ws.id,
                    ContactChannel.channel_type == "email",
                    ContactChannel.is_active.is_(True),
                )
            )
        ).all()

    print(f"{len(rows)} active email channels\n")

    mx_cache: dict[str, object] = {}
    signals: collections.Counter[str] = collections.Counter()
    verdicts: collections.Counter[str] = collections.Counter()
    would_lose: list[tuple[str, str, str]] = []
    never_contact: list[str] = []

    for email, source, status, domain in rows:
        if is_never_contact is not None and is_never_contact(email):
            never_contact.append(email)

        mx = None
        if check_dns and domain:
            if domain not in mx_cache:
                try:
                    mx_cache[domain] = check_mx(domain, resolver=system_mx_resolver)
                except Exception:
                    mx_cache[domain] = None
            mx = mx_cache[domain]

        risk = assess(email=email, source=source, mx=mx)
        for s in risk.signals:
            signals[f"{s.verdict.name:9s} {s.code}"] += 1

        sendable_now = verification_permits_sending(status, source)
        sendable_after = verification_permits_sending(risk.status, source)
        verdicts[f"{status.value} -> {risk.status.value}"] += 1
        if sendable_now and not sendable_after:
            would_lose.append((email, status.value, risk.status.value))

    print("SIGNALS RAISED")
    for name, n in signals.most_common(20):
        print(f"  {n:5d}  {name}")

    print("\nSTATUS TRANSITIONS (current -> today's rules)")
    for name, n in verdicts.most_common(12):
        print(f"  {n:5d}  {name}")

    print(f"\nWOULD STOP BEING SENDABLE: {len(would_lose)}")
    for email, before, after in would_lose[:30]:
        print(f"  {email:52s} {before} -> {after}")

    if is_never_contact is not None:
        print(f"\nNEVER-CONTACT addresses still active: {len(never_contact)}")
        for e in never_contact[:25]:
            print(f"  {e}")
    return 0


raise SystemExit(asyncio.run(main()))
