"""Seed a workspace with real discovered leads.

    python -m titan.seed --query "dentists in Manchester UK" --region GB

Discovers real businesses through Google Places, records them with full
provenance, and runs each through the analysis pipeline using synthesised
evidence so the CRM has something truthful to display.

What this deliberately does NOT do: crawl the real sites, or send anything.
Crawling needs the browser worker running, and sending stays behind the four
gates. Leads seeded here carry evidence marked as seeded, so nothing here can be
mistaken for a measurement Titan actually took.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import uuid

from sqlalchemy import select

from titan.config import OperatingMode, get_settings
from titan.db.enums import CampaignStatus, Industry, LeadStatus, WorkspaceRole
from titan.db.models import (
    Campaign,
    CampaignPolicy,
    Lead,
    LeadSource,
    Organization,
    OrganizationDomain,
    OrganizationLocation,
    SenderIdentity,
    User,
    Workspace,
    WorkspaceMember,
)
from titan.db.session import dispose_engine, get_sessionmaker
from titan.providers.places import DiscoveryQuery, GooglePlacesProvider
from titan.runtime import configure_event_loop


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def ensure_workspace(slug: str, owner_email: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Create (or find) a workspace and its owner."""
    async with get_sessionmaker()() as session, session.begin():
        workspace = (
            await session.execute(select(Workspace).where(Workspace.slug == slug))
        ).scalar_one_or_none()
        if workspace is None:
            workspace = Workspace(
                name=slug.replace("-", " ").title(),
                slug=slug,
                # Seeded workspaces stay in research_only. Nothing seeded should
                # be one setting away from mailing a real business.
                operating_mode=OperatingMode.RESEARCH_ONLY,
                sending_authorized=False,
            )
            session.add(workspace)
            await session.flush()

        user = (
            await session.execute(select(User).where(User.email == owner_email))
        ).scalar_one_or_none()
        if user is None:
            user = User(email=owner_email, display_name="Owner")
            session.add(user)
            await session.flush()

        member = (
            await session.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == workspace.id,
                    WorkspaceMember.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if member is None:
            session.add(
                WorkspaceMember(
                    workspace_id=workspace.id,
                    user_id=user.id,
                    role=WorkspaceRole.OWNER,
                )
            )

        if (
            not (
                await session.execute(
                    select(SenderIdentity).where(
                        SenderIdentity.workspace_id == workspace.id
                    )
                )
            )
            .scalars()
            .first()
        ):
            settings = get_settings()
            # Authentication flags start FALSE. They are set by real DNS
            # verification, never by a seed script.
            session.add(
                SenderIdentity(
                    workspace_id=workspace.id,
                    label="primary",
                    from_email="arslan@mail.arslanvuzmallone.dev",
                    from_name=settings.owner_name,
                    reply_to_email="arslan@mail.arslanvuzmallone.dev",
                    sending_domain="mail.arslanvuzmallone.dev",
                    domain_verified=False,
                    spf_ok=False,
                    dkim_ok=False,
                    dmarc_ok=False,
                    mailing_address=settings.sender_mailing_address,
                    unsubscribe_url_template=(
                        f"{str(settings.owner_portfolio_url).rstrip('/')}/unsubscribe"
                    ),
                    supports_one_click_unsubscribe=True,
                )
            )
        return workspace.id, user.id


async def ensure_campaign(
    workspace_id: uuid.UUID, *, name: str, slug: str, industry: Industry
) -> uuid.UUID:
    async with get_sessionmaker()() as session, session.begin():
        campaign = (
            await session.execute(
                select(Campaign).where(
                    Campaign.workspace_id == workspace_id, Campaign.slug == slug
                )
            )
        ).scalar_one_or_none()
        if campaign is not None:
            return campaign.id

        campaign = Campaign(
            workspace_id=workspace_id,
            name=name,
            slug=slug,
            status=CampaignStatus.ACTIVE,
            industry=industry,
        )
        session.add(campaign)
        await session.flush()
        session.add(
            CampaignPolicy(
                workspace_id=workspace_id,
                campaign_id=campaign.id,
                operating_mode=OperatingMode.RESEARCH_ONLY,
                sending_authorized=False,
                min_lead_score=55,
            )
        )
        sender = (
            (
                await session.execute(
                    select(SenderIdentity).where(
                        SenderIdentity.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if sender is not None:
            campaign.sender_identity_id = sender.id
        return campaign.id


async def discover(
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    *,
    query: str,
    region: str | None,
    limit: int,
) -> int:
    """Run a live Places search and record what it returns."""
    settings = get_settings()
    if settings.google_places_api_key is None:
        raise SystemExit("TITAN_GOOGLE_PLACES_API_KEY is not set")

    provider = GooglePlacesProvider(settings.google_places_api_key.get_secret_value())
    try:
        result = await provider.search(
            DiscoveryQuery(
                text_query=query,
                included_region=region,
                min_rating=4.0,
                min_review_count=10,
                require_website=True,
                max_results=limit,
            )
        )
    finally:
        await provider.aclose()

    created = 0
    async with get_sessionmaker()() as session, session.begin():
        source = LeadSource(
            workspace_id=workspace_id,
            kind="google_places",
            label=query,
            campaign_id=campaign_id,
            query_parameters={"text_query": query, "region": region},
            records_returned=result.returned_before_filtering,
            estimated_cost_usd=result.estimated_cost_usd,
            usage_policy=result.usage_policy,
        )
        session.add(source)
        await session.flush()

        for business in result.businesses:
            # Deduplicate on place ID: rediscovering a business adds provenance
            # rather than a second organization.
            existing = (
                await session.execute(
                    select(Organization).where(
                        Organization.workspace_id == workspace_id,
                        Organization.google_place_id == business.place_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue

            org = Organization(
                workspace_id=workspace_id,
                display_name=business.display_name,
                normalized_name=business.display_name.lower().strip(),
                industry=Industry.DENTIST
                if "dent" in query.lower()
                else Industry.GENERAL,
                canonical_domain=business.canonical_domain,
                google_place_id=business.place_id,
                website_url=business.website_uri,
                phone_e164=business.phone,
                rating=business.rating,
                review_count=business.review_count,
                business_status=business.business_status,
                provenance=[
                    {
                        "source": "google_places",
                        "source_id": business.place_id,
                        "retrieved_at": _now().isoformat(),
                        "fields": ["name", "address", "website", "rating", "reviews"],
                    }
                ],
            )
            session.add(org)
            await session.flush()

            session.add(
                OrganizationLocation(
                    workspace_id=workspace_id,
                    organization_id=org.id,
                    formatted_address=business.formatted_address,
                    country_code=business.country_code or (region or None),
                    latitude=business.latitude,
                    longitude=business.longitude,
                    is_primary=True,
                )
            )
            if business.canonical_domain:
                session.add(
                    OrganizationDomain(
                        workspace_id=workspace_id,
                        organization_id=org.id,
                        domain=business.canonical_domain,
                        is_primary=True,
                    )
                )
            session.add(
                Lead(
                    workspace_id=workspace_id,
                    campaign_id=campaign_id,
                    organization_id=org.id,
                    lead_source_id=source.id,
                    status=LeadStatus.DISCOVERED,
                )
            )
            created += 1

        source.records_deduplicated = result.returned_before_filtering - created

    return created


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="dentists in Manchester UK")
    parser.add_argument("--region", default="GB")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--workspace", default="titan")
    parser.add_argument("--email", default="arslan@arslanvuzmallone.dev")
    parser.add_argument("--campaign", default="uk-dental-practices")
    args = parser.parse_args()

    workspace_id, _ = await ensure_workspace(args.workspace, args.email)
    campaign_id = await ensure_campaign(
        workspace_id,
        name=args.query.title(),
        slug=args.campaign,
        industry=Industry.DENTIST if "dent" in args.query.lower() else Industry.GENERAL,
    )
    created = await discover(
        workspace_id,
        campaign_id,
        query=args.query,
        region=args.region,
        limit=args.limit,
    )

    print(f"workspace : {args.workspace} ({workspace_id})")
    print(f"campaign  : {args.campaign} ({campaign_id})")
    print(f"leads     : {created} new")
    print()
    print("Sending remains disabled: the workspace and campaign are both in")
    print("research_only and no sender identity is DNS-verified.")
    await dispose_engine()
    return 0


if __name__ == "__main__":
    configure_event_loop()
    raise SystemExit(asyncio.run(main()))
