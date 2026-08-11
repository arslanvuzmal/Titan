"""Finding businesses and recording them, against a real PostgreSQL.

Places is stubbed -- what is under test is everything around it: the budget
gate, the deduplication against organizations that already exist, the
suppression check that stops discovery re-adding somebody who opted out, and
the idempotency that keeps a retry from paying for a second search.
"""

from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from titan.activities.discovery import discover_leads
from titan.db.enums import CampaignStatus, Industry, LeadStatus, SuppressionReason
from titan.db.models import Campaign, CampaignPolicy, Lead, LeadSource, Organization
from titan.db.models.lead import OrganizationDomain, OrganizationLocation
from titan.db.models.ops import Task
from titan.db.session import get_sessionmaker, workspace_unit_of_work
from titan.delivery.suppression import suppress
from titan.providers.places import DiscoveredBusiness, DiscoveryResult
from titan.workflows.types import DiscoverActivityInput

pytestmark = pytest.mark.asyncio

NOW = dt.datetime(2026, 8, 11, 9, 0, tzinfo=dt.UTC)


#: Distinguishes "not specified" from "explicitly has no website". Without it
#: ``website or default`` reads an explicit None as absence-of-argument and
#: hands back the default URL -- which made the no-website case silently test
#: the opposite of its name.
_DEFAULT = object()


def found(
    n: int = 1, *, website: object = _DEFAULT, place_id: str | None = None
) -> DiscoveredBusiness:
    """One Places record. Pass ``website=None`` for a business without one."""
    return DiscoveredBusiness(
        place_id=place_id or f"places/found-{n}",
        display_name=f"Harborline Dental {n}",
        formatted_address=f"{n} Fictional Row, Manchester",
        website_uri=(
            f"https://harborline-{n}.test/"
            if website is _DEFAULT
            else (website if isinstance(website, str) else None)
        ),
        phone="+15550100",
        rating=4.7,
        review_count=90,
        business_status="OPERATIONAL",
        primary_type="dentist",
        latitude=53.4,
        longitude=-2.2,
        country_code="GB",
    )


def places_result(
    *businesses: DiscoveredBusiness, cost: float = 0.032
) -> DiscoveryResult:
    return DiscoveryResult(
        businesses=list(businesses),
        pages_fetched=1,
        returned_before_filtering=len(businesses),
        estimated_cost_usd=cost,
        usage_policy={"attribution": "Powered by Google"},
    )


async def seed_campaign(
    workspace_id: uuid.UUID,
    *,
    suffix: str,
    business_type: str | None = "dentists",
    geography: str | None = "Manchester UK",
    research_budget_usd: float = 10.0,
) -> uuid.UUID:
    async with get_sessionmaker()() as session, session.begin():
        campaign = Campaign(
            workspace_id=workspace_id,
            name=f"Discovery {suffix}",
            slug=f"discovery-{suffix}",
            status=CampaignStatus.ACTIVE,
            industry=Industry.DENTIST,
            target_business_type=business_type,
            target_geography=geography,
            target_country_code="GB",
        )
        session.add(campaign)
        await session.flush()
        session.add(
            CampaignPolicy(
                workspace_id=workspace_id,
                campaign_id=campaign.id,
                research_budget_usd=research_budget_usd,
            )
        )
        return campaign.id


def run_for(
    workspace: uuid.UUID, campaign: uuid.UUID, *, key: str = "k1", max_results: int = 20
) -> DiscoverActivityInput:
    return DiscoverActivityInput(
        workspace_id=str(workspace),
        campaign_id=str(campaign),
        idempotency_key=key,
        max_results=max_results,
    )


async def run_discovery(request: DiscoverActivityInput, result: DiscoveryResult):
    """Run the activity with Places and the API key both stubbed.

    Both are required: the activity refuses before searching when no key is
    configured, which is correct behaviour and would otherwise make every test
    below pass for the wrong reason.
    """
    with (
        patch("titan.activities.discovery.GooglePlacesProvider") as ProviderCls,
        patch("titan.activities.discovery.get_settings") as get_settings,
        patch("titan.activities.discovery.activity") as fake_activity,
    ):
        fake_activity.heartbeat = lambda *a, **k: None
        get_settings.return_value.google_places_api_key = "key"
        instance = ProviderCls.from_settings.return_value
        instance.search = AsyncMock(return_value=result)
        instance.aclose = AsyncMock()
        return await discover_leads(request)


# ==========================================================================
# The happy path
# ==========================================================================
async def test_a_search_creates_leads_with_full_provenance(db_session, workspace):
    """``lead_sources`` records what was asked, what it cost, and what it licensed."""
    campaign_id = await seed_campaign(workspace, suffix="happy")

    result = await run_discovery(
        run_for(workspace, campaign_id), places_result(found(1), found(2))
    )

    assert result.leads_created == 2
    assert result.returned == 2
    assert result.spent_usd == pytest.approx(0.032)

    async with get_sessionmaker()() as s:
        leads = (
            (await s.execute(select(Lead).where(Lead.campaign_id == campaign_id)))
            .scalars()
            .all()
        )
        assert len(leads) == 2
        assert all(lead.status is LeadStatus.DISCOVERED for lead in leads)
        assert all(lead.lead_source_id is not None for lead in leads)

        source = (
            (
                await s.execute(
                    select(LeadSource).where(LeadSource.campaign_id == campaign_id)
                )
            )
            .scalars()
            .one()
        )
        assert source.kind == "google_places"
        assert source.query_parameters["text_query"] == "dentists in Manchester UK"
        assert source.usage_policy


async def test_the_organization_takes_its_industry_from_the_campaign(
    db_session, workspace
):
    """Not from sniffing the search text.

    The industry selects the playbook, and the playbook constrains which offers
    may ever be proposed.
    """
    campaign_id = await seed_campaign(workspace, suffix="industry")

    await run_discovery(run_for(workspace, campaign_id), places_result(found(1)))

    async with get_sessionmaker()() as s:
        org = (await s.execute(select(Organization))).scalars().one()
        assert org.industry is Industry.DENTIST
        assert org.google_place_id == "places/found-1"
        assert org.provenance[0]["source"] == "google_places"


async def test_a_lead_gets_a_location_and_a_domain(db_session, workspace):
    """The crawl needs a domain; quiet hours need a country."""
    campaign_id = await seed_campaign(workspace, suffix="detail")

    await run_discovery(run_for(workspace, campaign_id), places_result(found(1)))

    async with get_sessionmaker()() as s:
        location = (await s.execute(select(OrganizationLocation))).scalars().one()
        assert location.country_code == "GB"
        domain = (await s.execute(select(OrganizationDomain))).scalars().one()
        assert domain.domain == "harborline-1.test"
        # Places said this is their website; nothing has confirmed it serves one.
        assert domain.verified_at is None


# ==========================================================================
# Refusals that cost nothing
# ==========================================================================
async def test_a_campaign_with_no_targeting_is_refused_before_searching(
    db_session, workspace
):
    campaign_id = await seed_campaign(workspace, suffix="untargeted", geography=None)

    result = await run_discovery(run_for(workspace, campaign_id), places_result(found(1)))

    assert result.leads_created == 0
    assert result.refused_reason is not None
    assert "target_geography" in result.refused_reason
    assert result.spent_usd == 0.0


async def test_a_spent_research_budget_refuses_before_searching(db_session, workspace):
    """Places bills per request, so the gate has to be in front of the call."""
    campaign_id = await seed_campaign(workspace, suffix="broke", research_budget_usd=0.01)

    async with workspace_unit_of_work(workspace) as session:
        session.add(
            LeadSource(
                workspace_id=workspace,
                kind="google_places",
                label="earlier today",
                campaign_id=campaign_id,
                estimated_cost_usd=0.05,
            )
        )

    result = await run_discovery(run_for(workspace, campaign_id), places_result(found(1)))

    assert result.leads_created == 0
    assert result.refused_reason is not None
    assert "budget spent" in result.refused_reason


# ==========================================================================
# Deduplication and suppression
# ==========================================================================
async def test_a_business_already_known_is_not_added_twice(db_session, workspace):
    campaign_id = await seed_campaign(workspace, suffix="dupe")

    first = await run_discovery(
        run_for(workspace, campaign_id, key="a"), places_result(found(1))
    )
    second = await run_discovery(
        run_for(workspace, campaign_id, key="b"), places_result(found(1), found(2))
    )

    assert first.leads_created == 1
    assert second.leads_created == 1  # only the new one

    async with get_sessionmaker()() as s:
        orgs = (await s.execute(select(Organization))).scalars().all()
        assert len(orgs) == 2


async def test_discovery_does_not_re_add_a_suppressed_domain(db_session, workspace):
    """Mission section 24. Suppression outlives the contact it came from."""
    campaign_id = await seed_campaign(workspace, suffix="suppressed")

    async with workspace_unit_of_work(workspace) as session:
        await suppress(
            session,
            workspace_id=workspace,
            email_or_domain="harborline-1.test",
            reason=SuppressionReason.COMPLAINT,
            source="test",
            # Explicit: suppress() defaults to scope="email", and discovery
            # consults only domain-scoped entries -- one colleague opting out
            # is not the business opting out.
            scope="domain",
            now=NOW,
        )

    result = await run_discovery(
        run_for(workspace, campaign_id), places_result(found(1), found(2))
    )

    assert result.leads_created == 1

    async with get_sessionmaker()() as s:
        domains = (await s.execute(select(Organization.canonical_domain))).scalars().all()
        assert "harborline-1.test" not in domains


async def test_one_persons_unsubscribe_does_not_blacklist_their_employer(
    db_session, workspace
):
    """An email-scoped suppression must not remove the whole company.

    Treating it as one would quietly destroy a workspace's reachable pool a
    single unsubscribe at a time. The address itself is still refused at the
    send gate, which is where per-address suppression belongs.
    """
    campaign_id = await seed_campaign(workspace, suffix="oneperson")

    async with workspace_unit_of_work(workspace) as session:
        await suppress(
            session,
            workspace_id=workspace,
            email_or_domain="sam@harborline-1.test",
            reason=SuppressionReason.UNSUBSCRIBE,
            source="test",
            now=NOW,
        )

    result = await run_discovery(run_for(workspace, campaign_id), places_result(found(1)))

    assert result.leads_created == 1


async def test_a_platform_page_is_never_admitted(db_session, workspace):
    """Crawling it would produce findings about Facebook's markup."""
    campaign_id = await seed_campaign(workspace, suffix="social")

    result = await run_discovery(
        run_for(workspace, campaign_id),
        places_result(found(1, website="https://www.facebook.com/harborline")),
    )

    assert result.leads_created == 0
    assert dict(result.refused_counts)["non_auditable_host"] == 1


# ==========================================================================
# Idempotency
# ==========================================================================
async def test_a_retry_on_the_same_key_does_not_search_again(db_session, workspace):
    """A second billable search is the expensive way to create zero leads."""
    campaign_id = await seed_campaign(workspace, suffix="idem")

    first = await run_discovery(
        run_for(workspace, campaign_id, key="same"), places_result(found(1), found(2))
    )
    second = await run_discovery(
        run_for(workspace, campaign_id, key="same"), places_result(found(3))
    )

    assert first.leads_created == 2
    assert second.duplicate is True
    assert second.leads_created == 2

    async with get_sessionmaker()() as s:
        sources = (await s.execute(select(LeadSource))).scalars().all()
        assert len(sources) == 1
        orgs = (await s.execute(select(Organization))).scalars().all()
        assert len(orgs) == 2


# ==========================================================================
# The stall worth waking somebody for
# ==========================================================================
async def test_a_search_that_admits_nothing_notifies_the_operator(db_session, workspace):
    """The search works, every result is refused: that is a targeting problem,
    and it repeats every cycle at the cost of a Places request each time."""
    campaign_id = await seed_campaign(workspace, suffix="empty")

    result = await run_discovery(
        run_for(workspace, campaign_id),
        places_result(
            found(1, website=None),
            found(2, website="https://instagram.com/x"),
        ),
    )

    assert result.leads_created == 0
    assert result.notified is True

    async with get_sessionmaker()() as s:
        task = (
            (await s.execute(select(Task).where(Task.kind == "campaign_stalled")))
            .scalars()
            .one()
        )
        assert "admitted none" in task.title


async def test_a_search_that_returns_nothing_at_all_does_not_notify(
    db_session, workspace
):
    """Nothing came back, so there is nothing to conclude about the targeting."""
    campaign_id = await seed_campaign(workspace, suffix="nothing")

    result = await run_discovery(run_for(workspace, campaign_id), places_result())

    assert result.leads_created == 0
    assert result.notified is False
