"""Finding businesses to research.

The stage that was missing. :mod:`titan.providers.places` has been able to
search Google since the first release and was called only by ``titan/seed.py``,
a one-shot developer script -- so nothing in the running system could create a
lead, and every campaign worked a pool somebody had loaded by hand. The
planner's ``NO_WORK_AVAILABLE`` verdict says as much in its own notification
text: *"usually this means discovery has run dry"*.

Three properties this activity is built around.

**It costs real money.** Places bills per request, per field mask. So the spend
is bounded by the campaign's own ``research_budget_usd``, counted from midnight
UTC -- the same day boundary the send quota uses, because two components
disagreeing about where "today" starts is how a limit gets silently exceeded.
Every run writes what it spent to ``lead_sources`` whether it admitted anything
or not.

**It must not re-add somebody who opted out.** The suppression model says so
explicitly: entries are not foreign-keyed to contacts precisely so that erasing
a contact cannot resurrect them here. Domains are checked before the crawl is
paid for, not only at the send gate.

**Re-running it changes nothing.** Idempotent on the key the workflow supplies,
recorded in ``lead_sources.query_parameters``. A retry after a partial failure
finds its own ledger row and returns what the first attempt did, rather than
running a second billable search.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from titan.config import get_settings
from titan.db.enums import Industry, LeadStatus
from titan.db.models import (
    Campaign,
    CampaignPolicy,
    Lead,
    LeadSource,
    Organization,
    OrganizationDomain,
    OrganizationLocation,
)
from titan.db.models.compliance import SuppressionEntry
from titan.db.session import workspace_session, workspace_unit_of_work
from titan.intelligence.discovery import admit_all, build_query, targeting_blockers
from titan.notify.operator import NotificationKind, record_notification
from titan.providers.places import (
    DiscoveredBusiness,
    DiscoveryResult,
    GooglePlacesProvider,
    PlacesError,
)
from titan.workflows.types import DiscoverActivityInput, DiscoverActivityResult

logger = logging.getLogger(__name__)

#: Ledger kind for rows this activity writes. Matches what ``seed.py`` uses, so
#: a workspace seeded by hand and one discovered automatically read the same.
SOURCE_KIND = "google_places"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@activity.defn(name="discover_leads")
async def discover_leads(request: DiscoverActivityInput) -> DiscoverActivityResult:
    """Search for businesses matching a campaign's targeting and record them."""
    workspace_id = uuid.UUID(request.workspace_id)
    campaign_id = uuid.UUID(request.campaign_id)
    now = _now()

    async with workspace_session(workspace_id) as session:
        existing = await _previous_run(session, campaign_id, request.idempotency_key)
        if existing is not None:
            logger.info(
                "discovery already ran for this key; returning the recorded result",
                extra={"campaign_id": str(campaign_id), "key": request.idempotency_key},
            )
            return existing

        campaign = await session.get(Campaign, campaign_id)
        if campaign is None:
            return DiscoverActivityResult(
                refused_reason=f"campaign {request.campaign_id} not found"
            )

        blockers = targeting_blockers(
            business_type=campaign.target_business_type,
            geography=campaign.target_geography,
        )
        if blockers:
            return DiscoverActivityResult(refused_reason="; ".join(blockers))

        policy = (
            await session.execute(
                select(CampaignPolicy).where(CampaignPolicy.campaign_id == campaign_id)
            )
        ).scalar_one_or_none()
        if policy is None:
            return DiscoverActivityResult(
                refused_reason="campaign has no policy row; nothing defines its budget"
            )

        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spent_today = float(
            (
                await session.execute(
                    select(
                        func.coalesce(func.sum(LeadSource.estimated_cost_usd), 0.0)
                    ).where(
                        LeadSource.campaign_id == campaign_id,
                        LeadSource.created_at >= day_start,
                    )
                )
            ).scalar_one()
            or 0.0
        )
        if spent_today >= policy.research_budget_usd:
            return DiscoverActivityResult(
                spent_usd=spent_today,
                refused_reason=(
                    f"discovery budget spent: ${spent_today:.2f} of "
                    f"${policy.research_budget_usd:.2f} used today"
                ),
            )

        # Targeting is snapshotted before the session closes: the search below
        # runs outside any transaction, and holding one open across a network
        # call to Google is the pattern mission section 25 forbids.
        business_type = (campaign.target_business_type or "").strip()
        geography = (campaign.target_geography or "").strip()
        country_code = campaign.target_country_code
        industry = campaign.industry or Industry.GENERAL

    settings = get_settings()
    if settings.google_places_api_key is None:
        return DiscoverActivityResult(
            refused_reason="TITAN_GOOGLE_PLACES_API_KEY is not configured"
        )

    query = build_query(
        business_type=business_type,
        geography=geography,
        country_code=country_code,
        max_results=request.max_results,
    )

    provider = GooglePlacesProvider.from_settings(settings)
    try:
        activity.heartbeat("searching places")
        result = await provider.search(query)
    except PlacesError as exc:
        # Retryable errors propagate so Temporal's policy decides; a permanent
        # one is a configuration problem an operator must see, and burning the
        # retry budget on it would only delay them finding out.
        if exc.retryable:
            raise
        logger.warning("places search refused", extra={"error": str(exc)[:200]})
        return DiscoverActivityResult(refused_reason=f"places: {exc}")
    finally:
        await provider.aclose()

    return await _record(
        workspace_id=workspace_id,
        campaign_id=campaign_id,
        industry=industry,
        query_text=query.text_query,
        country_code=country_code,
        result=result,
        idempotency_key=request.idempotency_key,
        max_new_leads=request.max_results,
        now=now,
    )


async def _previous_run(
    session: AsyncSession, campaign_id: uuid.UUID, key: str
) -> DiscoverActivityResult | None:
    """What a prior attempt on this key already did, if there was one."""
    row = (
        await session.execute(
            select(LeadSource).where(
                LeadSource.campaign_id == campaign_id,
                LeadSource.query_parameters["idempotency_key"].astext == key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return DiscoverActivityResult(
        leads_created=int(row.records_returned) - int(row.records_deduplicated),
        returned=int(row.records_returned),
        spent_usd=float(row.estimated_cost_usd),
        lead_source_id=str(row.id),
        duplicate=True,
    )


async def _record(
    *,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    industry: Industry,
    query_text: str,
    country_code: str | None,
    result: DiscoveryResult,
    idempotency_key: str,
    max_new_leads: int,
    now: dt.datetime,
) -> DiscoverActivityResult:
    """Admit what came back and write the survivors."""
    async with workspace_unit_of_work(workspace_id) as session:
        candidates = result.businesses
        domains = {b.canonical_domain for b in candidates if b.canonical_domain}
        place_ids = {b.place_id for b in candidates}

        # Bounded by what this search returned rather than by the workspace's
        # whole history: a workspace with a hundred thousand organizations must
        # not load all of them to check sixty.
        known_domains, known_place_ids = await _known(
            session, domains=domains, place_ids=place_ids
        )
        suppressed = await _suppressed_domains(session, domains=domains)

        admissions, refused = admit_all(
            candidates,
            known_domains=known_domains,
            known_place_ids=known_place_ids,
            suppressed_domains=suppressed,
            limit=max_new_leads,
        )
        admitted = [a.business for a in admissions if a.admitted]

        source = LeadSource(
            workspace_id=workspace_id,
            kind=SOURCE_KIND,
            label=query_text[:200],
            campaign_id=campaign_id,
            query_parameters={
                "text_query": query_text,
                "region": country_code,
                # The idempotency key lives here rather than in a column because
                # lead_sources has none for it, and adding one would mean a
                # migration against a schema already ahead of the repository.
                "idempotency_key": idempotency_key,
                "refused": refused,
            },
            records_returned=result.returned_before_filtering,
            records_deduplicated=result.returned_before_filtering - len(admitted),
            estimated_cost_usd=result.estimated_cost_usd,
            usage_policy=result.usage_policy,
        )
        session.add(source)
        await session.flush()
        # Read inside the transaction, not from the object afterwards. The
        # session is configured with expire_on_commit=False so it would work
        # either way today, and would start raising the day somebody changes
        # that -- from a line that looks nothing like the cause.
        source_id = source.id

        for business in admitted:
            await _create_lead(
                session,
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                source_id=source_id,
                industry=industry,
                business=business,
                country_code=country_code,
                now=now,
            )

        notification = None
        if not admitted and result.returned_before_filtering:
            # Worth waking somebody for: the search worked, Google returned
            # businesses, and every one was refused. That is a targeting problem
            # -- usually a query that finds chains with no independent website --
            # and it will repeat every cycle until a person changes something.
            notification = await record_notification(
                session,
                workspace_id=workspace_id,
                kind=NotificationKind.CAMPAIGN_STALLED,
                title=f"Discovery found {result.returned_before_filtering} businesses, admitted none",
                description=(
                    f"Search: {query_text}\n"
                    f"Refused: {_describe(refused)}\n\n"
                    "The search is working and every result was rejected, so the "
                    "targeting is finding the wrong kind of business. This repeats "
                    "each cycle and costs a Places request every time."
                ),
                lead_id=None,
                dedupe_key=f"discovery-empty:{campaign_id}:{now.date().isoformat()}",
                now=now,
            )

    logger.info(
        "discovery complete",
        extra={
            "campaign_id": str(campaign_id),
            "returned": result.returned_before_filtering,
            "admitted": len(admitted),
            "refused": refused,
            "cost_usd": result.estimated_cost_usd,
        },
    )
    return DiscoverActivityResult(
        leads_created=len(admitted),
        returned=result.returned_before_filtering,
        refused_counts=tuple(sorted(refused.items())),
        spent_usd=result.estimated_cost_usd,
        lead_source_id=str(source_id),
        notified=notification is not None,
    )


async def _known(
    session: AsyncSession, *, domains: set[str], place_ids: set[str]
) -> tuple[frozenset[str], frozenset[str]]:
    """Which of these candidates the workspace already has an organization for."""
    if not domains and not place_ids:
        return frozenset(), frozenset()

    rows = (
        await session.execute(
            select(Organization.canonical_domain, Organization.google_place_id).where(
                Organization.canonical_domain.in_(domains or {""})
                | Organization.google_place_id.in_(place_ids or {""})
            )
        )
    ).all()
    return (
        frozenset(domain for domain, _ in rows if domain),
        frozenset(place_id for _, place_id in rows if place_id),
    )


async def _suppressed_domains(
    session: AsyncSession, *, domains: set[str]
) -> frozenset[str]:
    """Domains under a domain-scoped suppression.

    Only ``scope='domain'`` entries are consulted. A single suppressed address
    at a company does not make the company unreachable -- somebody's colleague
    opting out is not the business opting out -- and treating it that way would
    quietly destroy a workspace's reachable pool one unsubscribe at a time.
    """
    if not domains:
        return frozenset()
    rows = (
        await session.execute(
            select(SuppressionEntry.normalized_value).where(
                SuppressionEntry.scope == "domain",
                SuppressionEntry.normalized_value.in_(domains),
            )
        )
    ).scalars()
    return frozenset(rows)


async def _create_lead(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    source_id: uuid.UUID,
    industry: Industry,
    business: DiscoveredBusiness,
    country_code: str | None,
    now: dt.datetime,
) -> None:
    """One organization, its location and domain, and the lead pointing at it.

    ``industry`` comes from the campaign rather than from sniffing the search
    text. ``seed.py`` infers it with ``"dent" in query.lower()``, which is fine
    for a script and wrong here: the industry selects the playbook, the playbook
    constrains which offers may ever be proposed, and a substring match would
    let a search for "dental supplies wholesaler" pitch patient-recall
    automation to a distributor.
    """
    org = Organization(
        workspace_id=workspace_id,
        display_name=business.display_name[:300],
        normalized_name=business.display_name.lower().strip()[:300],
        industry=industry,
        canonical_domain=business.canonical_domain,
        google_place_id=business.place_id,
        website_url=business.website_uri,
        phone_e164=business.phone,
        rating=business.rating,
        review_count=business.review_count,
        business_status=business.business_status,
        # Verbatim, and only the fields Places permits storing. The evidence
        # Titan derives itself lives in separate tables so the two are never
        # confused (Places ToS, section 6.1).
        provenance=[
            {
                "source": SOURCE_KIND,
                "source_id": business.place_id,
                "retrieved_at": now.isoformat(),
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
            country_code=(business.country_code or country_code or None),
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
                domain=business.canonical_domain[:253],
                is_primary=True,
                # Null: Places said this is their website, and nothing has yet
                # confirmed it serves their site. The crawl sets it.
                verified_at=None,
            )
        )
    session.add(
        Lead(
            workspace_id=workspace_id,
            campaign_id=campaign_id,
            organization_id=org.id,
            lead_source_id=source_id,
            status=LeadStatus.DISCOVERED,
        )
    )


def _describe(refused: dict[str, int]) -> str:
    if not refused:
        return "nothing"
    return ", ".join(f"{count} {reason}" for reason, count in sorted(refused.items()))


ALL_DISCOVERY_ACTIVITIES = [discover_leads]

__all__ = ["ALL_DISCOVERY_ACTIVITIES", "SOURCE_KIND", "discover_leads"]
