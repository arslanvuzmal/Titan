"""Recover the clock for leads discovered before discovery stamped one.

Discovery now writes each business's real IANA zone, taken from the metro it
searched. Everything found before that has ``organization_locations.timezone``
null -- 362 rows on the live workspace -- and a null timezone is not a small
gap: ``resolve_timezone`` returns nothing, the send window cannot decide whether
it is inside working hours, and the outbox refuses with

    recipient_quiet_hours: local time for None is inside quiet hours

so those leads are permanently unsendable rather than merely unscheduled.

**The evidence is already in the database.** Every lead points at the
``lead_sources`` row for the search that found it, and that row's label is the
query -- "med spas in Manchester UK". The geography is the part after the
business type, and if it names a catalogued territory then the metro, and
therefore the zone, is known exactly. This is not inference; it is reading back
the question that produced the row.

**Only where it is certain.** A label whose geography is not a catalogued
territory is left null. A guessed zone is worse than a missing one: null is
visibly unscheduled and gets fixed, while a wrong zone sends somebody mail at
four in the morning and looks correct in every report.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections import Counter

from sqlalchemy import select, update

from titan.db.models import Lead, LeadSource, OrganizationLocation, Workspace
from titan.db.session import dispose_engine, get_sessionmaker
from titan.intelligence import territories
from titan.runtime import configure_event_loop


def geography_of(label: str) -> str | None:
    """The place a discovery query asked about.

    Labels are ``"<business type> in <geography>"``. Split on the last ``" in "``
    rather than the first, because a business type can contain the word -- "walk
    in clinics in Leeds UK" -- and splitting on the first would ask about
    "clinics in Leeds UK" and match nothing.
    """
    marker = " in "
    if not label or marker not in label:
        return None
    return label.rsplit(marker, 1)[1].strip() or None


def timezone_for_label(label: str) -> str | None:
    """The zone that search's results are in, when the catalogue knows it."""
    geography = geography_of(label)
    return territories.timezone_of(geography) if geography else None


async def backfill(workspace_slug: str, *, apply: bool) -> int:
    async with get_sessionmaker()() as session, session.begin():
        workspace_id: uuid.UUID | None = (
            await session.execute(
                select(Workspace.id).where(Workspace.slug == workspace_slug)
            )
        ).scalar_one_or_none()
        if workspace_id is None:
            raise SystemExit(f"workspace {workspace_slug!r} does not exist")

        rows = (
            await session.execute(
                select(OrganizationLocation.id, LeadSource.label)
                .join(Lead, Lead.organization_id == OrganizationLocation.organization_id)
                .join(LeadSource, LeadSource.id == Lead.lead_source_id)
                .where(
                    OrganizationLocation.workspace_id == workspace_id,
                    OrganizationLocation.timezone.is_(None),
                )
            )
        ).all()

        resolved: dict[uuid.UUID, str] = {}
        unknown: Counter[str] = Counter()
        for location_id, label in rows:
            zone = timezone_for_label(label or "")
            if zone is None:
                unknown[geography_of(label or "") or "(no geography in label)"] += 1
                continue
            # First writer wins. A location reached through two searches is one
            # business in one place; the zone cannot differ, and taking the
            # first keeps the pass deterministic.
            resolved.setdefault(location_id, zone)

        by_zone = Counter(resolved.values())
        print(f"workspace: {workspace_slug} ({workspace_id})")
        print("dry run -- nothing written" if not apply else "applied")
        print(f"  {len(rows)} rows with no timezone")
        for zone, count in by_zone.most_common():
            print(f"  + {count:>4}  {zone}")
        for place, count in unknown.most_common(5):
            print(f"  ? {count:>4}  left null: {place}")

        if not apply:
            await session.rollback()
            return 0

        for location_id, zone in resolved.items():
            await session.execute(
                update(OrganizationLocation)
                .where(OrganizationLocation.id == location_id)
                .values(timezone=zone)
            )
        print(f"  wrote {len(resolved)} timezones")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="titan")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    code = await backfill(args.workspace, apply=args.apply)
    await dispose_engine()
    return code


if __name__ == "__main__":
    configure_event_loop()
    raise SystemExit(asyncio.run(main()))
