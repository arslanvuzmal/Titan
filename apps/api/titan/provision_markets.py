"""Give the six markets campaigns of their own.

    python -m titan.provision_markets --apply

Every campaign in this workspace was ``region = uk`` and every geography was a
northern English city -- five of eleven were Manchester. The regional machinery
has existed since Phase 02 (working hours per market, timezone bands, holiday
calendars per country) with nothing feeding it, because nothing ever created a
campaign outside one market.

This creates them: USA, UK, Canada, Europe including the east, the Gulf, and
Australia. Each starts at the densest metro in its market and rotates onward as
the ground is worked out.

**The send window is derived, never typed.** ``default_window_for`` takes the
market's real working hours and opens an hour ahead of them, floored at 07:00.
Writing ``9`` and ``17`` into a Gulf campaign would be two errors at once: the
Gulf works to 18:00, and it works Sunday to Thursday. The days come from the
same table for the same reason.

**Nothing here authorises a send.** Every campaign is created in
``research_only`` with ``sending_authorized = False``, exactly as the eleven that
exist already. Discovery, research and drafting run; the gate in front of
delivery is untouched and stays where the operator put it.

Idempotent on ``slug``. An existing campaign is left alone rather than
overwritten -- a campaign with leads and history attached is not something a
provisioning script should feel free to redefine.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from titan.config import OperatingMode
from titan.db.enums import CampaignStatus, Industry, SubRegion
from titan.db.models import Campaign, CampaignPolicy, SenderIdentity, Workspace
from titan.db.session import dispose_engine, get_sessionmaker
from titan.intelligence import territories
from titan.policy.schedule import default_window_for
from titan.runtime import configure_event_loop

#: What a new campaign inherits from the eleven already running, so a workspace
#: provisioned here and one built by hand behave identically.
DEFAULT_MIN_LEAD_SCORE = 70
DEFAULT_RESEARCH_BUDGET_USD = 10.0
DEFAULT_DAILY_SEND_LIMIT = 25


@dataclass(frozen=True, slots=True)
class MarketCampaign:
    """One campaign to exist, and where it starts looking."""

    slug: str
    name: str
    industry: Industry
    business_type: str
    #: Must be a catalogued territory: the starting metro decides the campaign's
    #: sub-region and the timezone stamped on every lead it discovers.
    territory: str


#: Two per market, in the trades that already work in the UK. Deliberately not
#: one campaign per industry per market -- that is forty-two campaigns, and the
#: approval queue is human-gated and already three hundred deep.
PLAN: tuple[MarketCampaign, ...] = (
    # ---- United States --------------------------------------------------
    MarketCampaign(
        "us-dentists-new-york",
        "Dentists, New York",
        Industry.DENTIST,
        "dentists",
        "New York NY USA",
    ),
    MarketCampaign(
        "us-med-spas-miami",
        "Med spas, Miami",
        Industry.MED_SPA,
        "med spas",
        "Miami FL USA",
    ),
    # ---- Canada ---------------------------------------------------------
    MarketCampaign(
        "ca-dentists-toronto",
        "Dentists, Toronto",
        Industry.DENTIST,
        "dentists",
        "Toronto Canada",
    ),
    MarketCampaign(
        "ca-med-spas-vancouver",
        "Med spas, Vancouver",
        Industry.MED_SPA,
        "med spas",
        "Vancouver Canada",
    ),
    # ---- Europe, west ---------------------------------------------------
    MarketCampaign(
        "eu-dentists-amsterdam",
        "Dentists, Amsterdam",
        Industry.DENTIST,
        "dentists",
        "Amsterdam Netherlands",
    ),
    MarketCampaign(
        "eu-law-firms-dublin",
        "Law firms, Dublin",
        Industry.LAW_FIRM,
        "law firms",
        "Dublin Ireland",
    ),
    # ---- Europe, east ---------------------------------------------------
    MarketCampaign(
        "eu-dentists-warsaw",
        "Dentists, Warsaw",
        Industry.DENTIST,
        "dentists",
        "Warsaw Poland",
    ),
    MarketCampaign(
        "eu-med-spas-bucharest",
        "Med spas, Bucharest",
        Industry.MED_SPA,
        "med spas",
        "Bucharest Romania",
    ),
    # ---- Middle East ----------------------------------------------------
    MarketCampaign(
        "me-med-spas-dubai",
        "Med spas, Dubai",
        Industry.MED_SPA,
        "med spas",
        "Dubai UAE",
    ),
    MarketCampaign(
        "me-dentists-abu-dhabi",
        "Dentists, Abu Dhabi",
        Industry.DENTIST,
        "dentists",
        "Abu Dhabi UAE",
    ),
    # ---- Australia ------------------------------------------------------
    MarketCampaign(
        "au-dentists-sydney",
        "Dentists, Sydney",
        Industry.DENTIST,
        "dentists",
        "Sydney Australia",
    ),
    MarketCampaign(
        "au-med-spas-melbourne",
        "Med spas, Melbourne",
        Industry.MED_SPA,
        "med spas",
        "Melbourne Australia",
    ),
)


def plan_rows() -> list[tuple[MarketCampaign, territories.Territory]]:
    """The plan with each entry's territory resolved.

    Raises rather than skipping: a plan naming a metro the catalogue does not
    hold would create a campaign with no clock and no rotation, which fails
    silently at send time rather than here.
    """
    resolved: list[tuple[MarketCampaign, territories.Territory]] = []
    for entry in PLAN:
        territory = territories.find(entry.territory)
        if territory is None:
            raise ValueError(f"{entry.slug}: {entry.territory!r} is not a territory")
        resolved.append((entry, territory))
    return resolved


async def _workspace_id(slug: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        found = (
            await session.execute(select(Workspace).where(Workspace.slug == slug))
        ).scalar_one_or_none()
        if found is None:
            raise SystemExit(f"workspace {slug!r} does not exist")
        return found.id


async def provision(workspace_id: uuid.UUID, *, apply: bool) -> list[str]:
    """Create the missing campaigns. Returns a line per entry for the operator."""
    lines: list[str] = []
    async with get_sessionmaker()() as session, session.begin():
        sender = (
            (
                await session.execute(
                    select(SenderIdentity)
                    .where(SenderIdentity.workspace_id == workspace_id)
                    .order_by(
                        SenderIdentity.domain_verified.desc(), SenderIdentity.created_at
                    )
                )
            )
            .scalars()
            .first()
        )

        for entry, territory in plan_rows():
            existing = (
                await session.execute(
                    select(Campaign).where(
                        Campaign.workspace_id == workspace_id,
                        Campaign.slug == entry.slug,
                    )
                )
            ).scalar_one_or_none()
            window = default_window_for(territory.region)

            if existing is not None:
                lines.append(f"  = {entry.slug:<24} exists, left alone")
                continue

            lines.append(
                f"  + {entry.slug:<24} {territory.query_name} "
                f"[{territory.region.value}"
                + (
                    f"/{territory.sub_region.value}"
                    if territory.sub_region is not SubRegion.UNSPECIFIED
                    else ""
                )
                + f"] {territory.timezone} "
                f"{window.start_hour:02d}-{window.end_hour:02d} "
                f"days={list(window.days)}"
            )
            if not apply:
                continue

            campaign = Campaign(
                workspace_id=workspace_id,
                name=entry.name,
                slug=entry.slug,
                status=CampaignStatus.ACTIVE,
                industry=entry.industry,
                target_business_type=entry.business_type,
                target_geography=territory.query_name,
                target_country_code=territory.country_code,
                region=territory.region,
                sub_region=territory.sub_region,
                sender_identity_id=sender.id if sender is not None else None,
            )
            session.add(campaign)
            await session.flush()
            session.add(
                CampaignPolicy(
                    workspace_id=workspace_id,
                    campaign_id=campaign.id,
                    # Unchanged from every campaign already here. Provisioning
                    # a market is not authorising a send to it.
                    operating_mode=OperatingMode.RESEARCH_ONLY,
                    sending_authorized=False,
                    min_lead_score=DEFAULT_MIN_LEAD_SCORE,
                    research_budget_usd=DEFAULT_RESEARCH_BUDGET_USD,
                    daily_send_limit=DEFAULT_DAILY_SEND_LIMIT,
                    send_window_start_hour=window.start_hour,
                    send_window_end_hour=window.end_hour,
                    send_days=list(window.days),
                )
            )
    return lines


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="titan")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the campaigns; without it, print what would be created",
    )
    args = parser.parse_args()

    workspace_id = await _workspace_id(args.workspace)
    lines = await provision(workspace_id, apply=args.apply)

    print(f"workspace: {args.workspace} ({workspace_id})")
    print("dry run -- nothing written" if not args.apply else "applied")
    for line in lines:
        print(line)
    print()
    print("All campaigns are research_only with sending_authorized = False.")
    await dispose_engine()
    return 0


if __name__ == "__main__":
    configure_event_loop()
    raise SystemExit(asyncio.run(main()))
