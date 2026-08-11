"""The six pipeline activities the research workflow drives.

Each is idempotent on a key supplied by the workflow: a retry must find its own
prior work rather than repeat it. Where an activity writes evidence, the write
is keyed on a content fingerprint so a re-crawl of an unchanged page produces no
duplicate rows.

None of these sends anything. ``queue_message`` writes an outbox row; the outbox
worker re-evaluates the entire authorization chain before any provider call.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from titan.config import get_settings
from titan.contracts.evidence import CrawlResult, fingerprint
from titan.db.enums import (
    ContactSource,
    DraftStatus,
    Industry,
    LeadStatus,
    MessageState,
    OutboxStatus,
    Severity,
    VerificationStatus,
)
from titan.db.models import (
    AuditFinding,
    BrowserArtifact,
    BusinessOpportunity,
    Campaign,
    CampaignPolicy,
    Contact,
    ContactChannel,
    ContactVerification,
    CrawlRun,
    FindingEvidence,
    Lead,
    LeadScore,
    Message,
    MessageDraft,
    Organization,
    OutboxMessage,
    Page,
    ResearchRun,
    SenderIdentity,
    SolutionRecommendation,
    Workspace,
)
from titan.db.session import workspace_session, workspace_unit_of_work
from titan.delivery.suppression import is_suppressed
from titan.intelligence.composer import ComposerContext, compose
from titan.intelligence.contacts import (
    check_contact_eligibility,
    extract_contacts_from_pages,
)
from titan.intelligence.findings import DetectedFinding, detect_findings
from titan.intelligence.message_validator import MessageContext, validate_message
from titan.intelligence.mx import MxCheck, check_many
from titan.intelligence.opportunities import DerivedOpportunity, derive_opportunities
from titan.intelligence.playbooks import get_playbook, select_offers
from titan.intelligence.scoring import ScoringInput
from titan.intelligence.scoring import score_lead as compute_score
from titan.providers.browser_client import BrowserWorkerClient
from titan.workflows.types import (
    AnalyseActivityInput,
    AnalyseActivityResult,
    ContactActivityInput,
    ContactActivityResult,
    CrawlActivityInput,
    CrawlActivityResult,
    DraftActivityInput,
    DraftActivityResult,
    QueueActivityInput,
    QueueActivityResult,
    ScoreActivityInput,
    ScoreActivityResult,
)

logger = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


#: Outbox states that still represent a message on its way to somebody. SENT,
#: CANCELLED and FAILED_PERMANENT are deliberately absent: once the first
#: message has left (or provably will not), a second one is a follow-up rather
#: than a duplicate, and blocking it would break the sequence.
_UNSENT_OUTBOX_STATUSES = (
    OutboxStatus.PENDING,
    OutboxStatus.LEASED,
    OutboxStatus.DEFERRED,
)


# ==========================================================================
# 1. Crawl
# ==========================================================================
@activity.defn(name="crawl_lead_website")
async def crawl_lead_website(request: CrawlActivityInput) -> CrawlActivityResult:
    """Crawl the lead's site via the isolated worker and store the evidence."""
    workspace_id = uuid.UUID(request.workspace_id)

    async with workspace_session(workspace_id) as session:
        existing = (
            (
                await session.execute(
                    select(CrawlRun).where(
                        CrawlRun.research_run_id == uuid.UUID(request.research_run_id)
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            pages = await session.scalar(
                select(func.count())
                .select_from(Page)
                .where(Page.crawl_run_id == existing.id)
            )
            return CrawlActivityResult(
                crawl_run_id=str(existing.id),
                status=existing.status,
                pages_captured=int(pages or 0),
                blocked_reason=existing.blocked_reason,
            )

        lead = await session.get(Lead, uuid.UUID(request.lead_id))
        org = (
            await session.get(Organization, lead.organization_id)
            if lead is not None
            else None
        )
        seed = request.seed_url or (org.website_url if org else "") or ""
        industry = org.industry if org else None

    if not seed:
        return CrawlActivityResult(
            crawl_run_id="",
            status="failed",
            pages_captured=0,
            failure_reason="lead has no website URL",
        )

    playbook = (
        get_playbook(industry)
        if industry
        else get_playbook(  # type: ignore[arg-type]
            __import__("titan.db.enums", fromlist=["Industry"]).Industry.GENERAL
        )
    )

    client = BrowserWorkerClient()
    try:
        # Heartbeat so a hung crawl is detected long before start_to_close.
        activity.heartbeat("dispatching to browser worker")
        result: CrawlResult = await client.research(
            request_id=request.idempotency_key,
            seed_url=seed,
            priority_paths=playbook.priority_paths,
        )
        activity.heartbeat(f"captured {len(result.pages)} pages")
    finally:
        await client.aclose()

    return await _persist_crawl(workspace_id, request, result)


async def _persist_crawl(
    workspace_id: uuid.UUID, request: CrawlActivityInput, result: CrawlResult
) -> CrawlActivityResult:
    async with workspace_unit_of_work(workspace_id) as session:
        crawl = CrawlRun(
            workspace_id=workspace_id,
            research_run_id=uuid.UUID(request.research_run_id),
            seed_url=result.seed_url,
            final_url=result.final_url,
            redirect_chain=list(result.redirect_chain),
            status=result.status,
            blocked_reason=result.blocked_reason,
            robots_allowed=result.robots_allowed,
            pages_fetched=result.pages_fetched,
            bytes_fetched=result.bytes_fetched,
            duration_ms=result.duration_ms,
            worker_version=result.worker_version,
        )
        session.add(crawl)
        await session.flush()

        stored = 0
        for evidence in result.pages:
            url_fp = fingerprint({"url": evidence.final_url.rstrip("/").lower()})
            # Immutable, and unique per (crawl_run, url): a retried ingest of
            # the same page collapses rather than duplicating evidence.
            inserted = await session.execute(
                pg_insert(Page.__table__)  # type: ignore[arg-type]
                .values(
                    workspace_id=workspace_id,
                    crawl_run_id=crawl.id,
                    url=evidence.url,
                    url_fingerprint=url_fp,
                    domain=_domain_of(evidence.final_url),
                    depth=evidence.depth,
                    http_status=evidence.http_status,
                    content_type=evidence.content_type,
                    title=evidence.title,
                    meta_description=evidence.meta_description,
                    canonical_url=evidence.canonical_url,
                    robots_meta=evidence.robots_meta,
                    lang=evidence.lang,
                    observations=evidence.model_dump(mode="json"),
                    content_fingerprint=evidence.content_fingerprint(),
                    text_excerpt=evidence.text_excerpt,
                    captured_at=evidence.captured_at,
                )
                .on_conflict_do_nothing(
                    index_elements=["crawl_run_id", "url_fingerprint"]
                )
                .returning(Page.__table__.c.id)
            )
            if inserted.scalar_one_or_none() is not None:
                stored += 1

        for artifact in result.artifacts:
            await session.execute(
                pg_insert(BrowserArtifact.__table__)  # type: ignore[arg-type]
                .values(
                    workspace_id=workspace_id,
                    crawl_run_id=crawl.id,
                    kind=artifact.kind,
                    media_type=artifact.media_type,
                    storage_key=artifact.storage_key,
                    payload=artifact.payload,
                    byte_size=artifact.byte_size,
                    content_fingerprint=artifact.content_fingerprint,
                    captured_at=_now(),
                )
                .on_conflict_do_nothing(
                    index_elements=["workspace_id", "content_fingerprint", "kind"]
                )
            )

        return CrawlActivityResult(
            crawl_run_id=str(crawl.id),
            status=result.status,
            pages_captured=stored,
            blocked_reason=result.blocked_reason,
            failure_reason=result.failure_reason,
        )


def _domain_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


# ==========================================================================
# 2. Analyse
# ==========================================================================
@activity.defn(name="analyse_evidence")
async def analyse_evidence(request: AnalyseActivityInput) -> AnalyseActivityResult:
    """Run the deterministic detectors over stored evidence.

    Findings come from measurement, never from a model. A model may later add
    narrative, but it cannot create a finding -- a hallucinated one would be a
    false statement about a real business.
    """
    from titan.contracts.evidence import PageEvidence

    workspace_id = uuid.UUID(request.workspace_id)

    async with workspace_session(workspace_id) as session:
        pages = (
            (
                await session.execute(
                    select(Page)
                    .where(Page.crawl_run_id == uuid.UUID(request.crawl_run_id))
                    .order_by(Page.depth)
                )
            )
            .scalars()
            .all()
        )
        page_ids = {p.url_fingerprint: p.id for p in pages}
        evidence = [PageEvidence.model_validate(p.observations) for p in pages]

        # Read here rather than in the write transaction below: the playbook
        # only constrains which offers may be proposed, and holding the unit of
        # work open for a second lookup buys nothing.
        lead = await session.get(Lead, uuid.UUID(request.lead_id))
        org = (
            await session.get(Organization, lead.organization_id)
            if lead is not None
            else None
        )
        industry = (org.industry if org else None) or Industry.GENERAL

    if not evidence:
        return AnalyseActivityResult(findings_created=0, pitchable_findings=0)

    crawl_result = CrawlResult(
        request_id=request.idempotency_key,
        status="completed",
        seed_url=evidence[0].url,
        final_url=evidence[0].final_url,
        pages=evidence,
        pages_fetched=len(evidence),
        worker_version="stored",
    )
    detected = detect_findings(crawl_result)

    created = 0
    pitchable = 0
    async with workspace_unit_of_work(workspace_id) as session:
        for finding in detected:
            page_id = page_ids.get(
                fingerprint({"url": (finding.page_url or "").rstrip("/").lower()})
            )
            inserted = await session.execute(
                pg_insert(AuditFinding.__table__)  # type: ignore[arg-type]
                .values(
                    workspace_id=workspace_id,
                    research_run_id=uuid.UUID(request.research_run_id),
                    lead_id=uuid.UUID(request.lead_id),
                    page_id=page_id,
                    category=finding.category.value,
                    issue_type=finding.issue_type,
                    title=finding.title,
                    page_url=finding.page_url,
                    selector=finding.selector,
                    observed_value=finding.observed_value,
                    expected_behavior=finding.expected_behavior,
                    severity=finding.severity.value,
                    confidence=finding.confidence,
                    business_impact=finding.business_impact,
                    recommended_solution=finding.recommended_solution,
                    estimated_effort=finding.estimated_effort,
                    verification_method=finding.verification_method.value,
                    finding_fingerprint=finding.fingerprint,
                )
                .on_conflict_do_nothing(
                    index_elements=["research_run_id", "finding_fingerprint"]
                )
                .returning(AuditFinding.__table__.c.id)
            )
            finding_id = inserted.scalar_one_or_none()
            if finding_id is None:
                continue
            created += 1

            for excerpt, source_url in finding.evidence:
                await session.execute(
                    pg_insert(FindingEvidence.__table__)  # type: ignore[arg-type]
                    .values(
                        workspace_id=workspace_id,
                        finding_id=finding_id,
                        page_id=page_id,
                        excerpt=excerpt,
                        excerpt_fingerprint=fingerprint({"e": excerpt, "u": source_url}),
                        source_url=source_url,
                        captured_at=_now(),
                    )
                    .on_conflict_do_nothing()
                )
            if finding.is_pitchable():
                pitchable += 1

        await session.execute(
            ResearchRun.__table__.update()  # type: ignore[attr-defined]
            .where(ResearchRun.id == uuid.UUID(request.research_run_id))
            .values(findings_count=created, pages_crawled=len(evidence))
        )

        opportunities = await _persist_opportunities(
            session,
            workspace_id=workspace_id,
            lead_id=uuid.UUID(request.lead_id),
            research_run_id=uuid.UUID(request.research_run_id),
            industry=industry,
            detected=detected,
        )

    top = detected[0].issue_type if detected else None
    sellable = [o for o in opportunities if o.deliverable]
    return AnalyseActivityResult(
        findings_created=created,
        pitchable_findings=pitchable,
        top_issue_type=top,
        opportunities_created=len(opportunities),
        deliverable_opportunities=len(sellable),
        top_offer_key=sellable[0].offer_key if sellable else None,
    )


async def _persist_opportunities(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    lead_id: uuid.UUID,
    research_run_id: uuid.UUID,
    industry: Industry,
    detected: list[DetectedFinding],
) -> list[DerivedOpportunity]:
    """Replace this run's opportunities with what the findings now support.

    **Replace, not merge.** Opportunities are a pure function of the run's
    findings and the playbook, so re-deriving them is cheap and the previous set
    carries no information the new one lacks. Merging would instead accumulate
    offers from every retry, including ones a corrected finding no longer
    justifies -- and an opportunity that outlives its evidence is precisely the
    unfounded claim the whole pipeline exists to prevent.

    That also removes the need for a unique constraint the table does not have,
    so this ships without a migration against a schema that is already ahead of
    the repository.

    ``solution_recommendations`` has ``ON DELETE CASCADE`` on its opportunity, so
    the delete below takes the outlines with it. Nothing is orphaned.
    """
    await session.execute(
        BusinessOpportunity.__table__.delete().where(  # type: ignore[attr-defined]
            BusinessOpportunity.__table__.c.research_run_id == research_run_id
        )
    )

    opportunities = derive_opportunities(industry, detected)
    if not opportunities:
        return []

    # Findings are addressed by fingerprint up to this point, because that is
    # the only identity a detector can produce. Resolve to ids over the whole
    # run rather than only what this call inserted: on a retry every finding
    # already exists, ``created`` is zero, and keying off the insert would link
    # the opportunities to nothing.
    rows = await session.execute(
        select(AuditFinding.finding_fingerprint, AuditFinding.id).where(
            AuditFinding.research_run_id == research_run_id
        )
    )
    finding_ids = {fp: str(fid) for fp, fid in rows.all()}

    for opportunity in opportunities:
        row = BusinessOpportunity(
            workspace_id=workspace_id,
            lead_id=lead_id,
            research_run_id=research_run_id,
            offer_key=opportunity.offer_key[:80],
            title=opportunity.title[:300],
            rationale=opportunity.rationale,
            supporting_finding_ids=[
                finding_ids[fp]
                for fp in opportunity.supporting_fingerprints
                if fp in finding_ids
            ],
            estimated_value_usd=opportunity.estimated_value_usd,
            priority=opportunity.priority,
            deliverable=opportunity.deliverable,
        )
        session.add(row)
        await session.flush()

        if opportunity.solution is None:
            continue
        session.add(
            SolutionRecommendation(
                workspace_id=workspace_id,
                opportunity_id=row.id,
                summary=opportunity.solution.summary,
                implementation_outline=list(opportunity.solution.implementation_outline),
                estimated_effort=opportunity.solution.estimated_effort,
                prerequisites=list(opportunity.solution.prerequisites),
                # Null on purpose: nothing here came from a model, and pointing
                # at a model run would misattribute deterministic work.
                model_run_id=None,
            )
        )

    logger.info(
        "opportunities derived",
        extra={
            "lead_id": str(lead_id),
            "research_run_id": str(research_run_id),
            "industry": industry.value,
            "opportunities": len(opportunities),
            "deliverable": sum(1 for o in opportunities if o.deliverable),
        },
    )
    return opportunities


# ==========================================================================
# 3. Score
# ==========================================================================
@activity.defn(name="score_lead")
async def score_lead(request: ScoreActivityInput) -> ScoreActivityResult:
    """Deterministic, explainable score. Persisted immutably."""
    workspace_id = uuid.UUID(request.workspace_id)

    async with workspace_session(workspace_id) as session:
        lead = await session.get(Lead, uuid.UUID(request.lead_id))
        if lead is None:
            raise ValueError(f"lead {request.lead_id} not found")
        org = await session.get(Organization, lead.organization_id)
        campaign = await session.get(Campaign, uuid.UUID(request.campaign_id))
        if org is None or campaign is None:
            raise ValueError("lead references a missing organization or campaign")
        policy = (
            await session.execute(
                select(CampaignPolicy).where(
                    CampaignPolicy.campaign_id == uuid.UUID(request.campaign_id)
                )
            )
        ).scalar_one()
        findings = (
            (
                await session.execute(
                    select(AuditFinding).where(
                        AuditFinding.research_run_id == uuid.UUID(request.research_run_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        channel = (
            await session.get(ContactChannel, lead.primary_contact_channel_id)
            if lead.primary_contact_channel_id
            else None
        )
        contact = await session.get(Contact, channel.contact_id) if channel else None
        # Snapshot every attribute needed after the session closes.
        org_snapshot: dict[str, Any] = {
            "industry": org.industry,
            "review_count": org.review_count,
            "rating": org.rating,
            "website_url": org.website_url,
            "business_status": org.business_status,
        }
        campaign_industry = campaign.industry
        min_score = policy.min_lead_score
        channel_source = channel.source if channel else None
        channel_verification = (
            channel.verification_status if channel else VerificationStatus.UNVERIFIED
        )
        is_decision_maker = bool(contact and contact.is_decision_maker)
        is_generic_role = bool(contact and contact.is_generic_role)

    detected = [_to_detected(f) for f in findings]
    evidenced_types = {f.issue_type for f in detected if f.is_pitchable()}
    offers = select_offers(org_snapshot["industry"], evidenced_types)

    result = compute_score(
        ScoringInput(
            findings=detected,
            industry_matches_campaign=org_snapshot["industry"] == campaign_industry,
            geography_matches_campaign=True,
            services_deliverable=bool(offers),
            review_count=org_snapshot["review_count"],
            rating=org_snapshot["rating"],
            has_website=bool(org_snapshot["website_url"]),
            website_reachable=True,
            business_status=org_snapshot["business_status"],
            contact_source=channel_source,
            contact_verification=channel_verification,
            contact_is_decision_maker=is_decision_maker,
            contact_is_generic_role=is_generic_role,
            estimated_project_value_usd=(
                max((o.estimated_value_usd for o in offers), default=0.0)
            ),
        ),
        threshold=min_score,
    )

    async with workspace_unit_of_work(workspace_id) as session:
        session.add(
            LeadScore(
                workspace_id=workspace_id,
                lead_id=uuid.UUID(request.lead_id),
                total=result.total,
                band=result.band.value,
                components=result.to_json()["components"],
                reasons=list(result.reasons),
                policy_version=result.policy_version,
                threshold_applied=result.threshold_applied,
                passed_threshold=result.passed_threshold,
            )
        )
        await session.execute(
            Lead.__table__.update()  # type: ignore[attr-defined]
            .where(Lead.id == uuid.UUID(request.lead_id))
            .values(
                latest_score=result.total,
                status=(
                    LeadStatus.QUALIFIED
                    if result.passed_threshold
                    else LeadStatus.MANUAL_REVIEW
                ),
            )
        )

    return ScoreActivityResult(
        total=result.total,
        band=result.band.value,
        passed_threshold=result.passed_threshold,
        threshold=result.threshold_applied,
    )


def _to_detected(row: AuditFinding) -> DetectedFinding:
    from titan.intelligence.findings import DetectedFinding

    return DetectedFinding(
        category=row.category,
        issue_type=row.issue_type,
        title=row.title,
        severity=row.severity,
        confidence=row.confidence,
        verification_method=row.verification_method,
        page_url=row.page_url,
        selector=row.selector,
        observed_value=row.observed_value,
        business_impact=row.business_impact,
        recommended_solution=row.recommended_solution,
        estimated_effort=row.estimated_effort,
        # Evidence rows exist in the database; one marker is enough for the
        # is_pitchable() check, which only asks whether any evidence exists.
        evidence=(("stored", row.page_url or ""),) if not row.contradicted else (),
    )


# ==========================================================================
# 4. Resolve contact
# ==========================================================================
@activity.defn(name="resolve_contact")
async def resolve_contact(request: ContactActivityInput) -> ContactActivityResult:
    """Find an eligible address, or explain why there is none.

    Invariant 6 in practice: addresses come from what the business published on
    its own site. Nothing is constructed.
    """
    from titan.contracts.evidence import PageEvidence

    workspace_id = uuid.UUID(request.workspace_id)

    async with workspace_session(workspace_id) as session:
        lead = await session.get(Lead, uuid.UUID(request.lead_id))
        if lead is None:
            raise ValueError(f"lead {request.lead_id} not found")
        org = await session.get(Organization, lead.organization_id)
        if org is None:
            raise ValueError("lead references a missing organization")
        policy = (
            await session.execute(
                select(CampaignPolicy).where(
                    CampaignPolicy.campaign_id == uuid.UUID(request.campaign_id)
                )
            )
        ).scalar_one()
        crawls = (
            (
                await session.execute(
                    select(CrawlRun).where(
                        CrawlRun.research_run_id == uuid.UUID(request.research_run_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        pages: list[PageEvidence] = []
        for crawl in crawls:
            rows = (
                (await session.execute(select(Page).where(Page.crawl_run_id == crawl.id)))
                .scalars()
                .all()
            )
            pages.extend(PageEvidence.model_validate(r.observations) for r in rows)
        contact_row_id = (
            await session.execute(
                select(Contact.id).where(Contact.organization_id == org.id).limit(1)
            )
        ).scalar_one_or_none()
        # Snapshot scalars before the session closes: reading an ORM attribute
        # afterwards raises DetachedInstanceError.
        org_id = org.id
        org_domain = org.canonical_domain
        allowed_sources = list(policy.allowed_contact_sources or [])
        require_verified = policy.require_verified_email

    discovered = extract_contacts_from_pages(pages, org_domain)
    allowed = frozenset(ContactSource(s) for s in allowed_sources)
    rejected: list[str] = []

    # DNS before the write loop, never inside it: check_many blocks on the
    # resolver, and mission section 25 forbids I/O inside a unit of work. One
    # lookup per distinct domain, not per address, so a site publishing six
    # mailboxes costs one query.
    mx_checks = await _resolve_mx(
        [c.domain for c in discovered if c.is_usable and c.domain]
    )

    for candidate in discovered:
        if not candidate.is_usable:
            rejected.append(f"{candidate.normalized}: {candidate.rejection_reason}")
            continue

        mx = mx_checks.get(candidate.domain)
        verdict = check_contact_eligibility(
            source=candidate.source,
            # First-party published is the provenance. MX is passed as a
            # disqualifier only: a domain with no route for inbound mail hard-
            # bounces every address at it, but a domain that *does* publish MX
            # has proved nothing about this mailbox and never upgrades the
            # status. See contacts.mx_presence_is_not_verification.
            verification=VerificationStatus.PUBLISHED_FIRST_PARTY,
            is_active=True,
            allowed_sources=allowed,
            require_verified=require_verified,
            email=candidate.normalized,
            mx=mx,
        )
        if not verdict.eligible:
            rejected.append(f"{candidate.normalized}: {'; '.join(verdict.reasons)}")
            continue

        async with workspace_unit_of_work(workspace_id) as session:
            if contact_row_id is None:
                new_contact = Contact(
                    workspace_id=workspace_id,
                    organization_id=org_id,
                    is_generic_role=candidate.is_generic_role,
                )
                session.add(new_contact)
                await session.flush()
                contact_row_id = new_contact.id

            suppressed = await is_suppressed(
                session, workspace_id=workspace_id, email=candidate.normalized
            )
            if suppressed is not None:
                rejected.append(
                    f"{candidate.normalized}: suppressed ({suppressed.reason.value})"
                )
                continue

            inserted = await session.execute(
                pg_insert(ContactChannel.__table__)  # type: ignore[arg-type]
                .values(
                    workspace_id=workspace_id,
                    contact_id=contact_row_id,
                    channel_type="email",
                    value=candidate.email,
                    normalized_value=candidate.normalized,
                    value_domain=candidate.domain,
                    source=candidate.source.value,
                    source_url=candidate.source_url,
                    discovered_at=_now(),
                    verification_status=VerificationStatus.PUBLISHED_FIRST_PARTY.value,
                    confidence=candidate.confidence,
                    is_active=True,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        "workspace_id",
                        "contact_id",
                        "channel_type",
                        "normalized_value",
                    ]
                )
                .returning(ContactChannel.__table__.c.id)
            )
            channel_id = inserted.scalar_one_or_none()
            newly_discovered = channel_id is not None
            if channel_id is None:
                channel_id = (
                    await session.execute(
                        select(ContactChannel.id).where(
                            ContactChannel.workspace_id == workspace_id,
                            ContactChannel.normalized_value == candidate.normalized,
                        )
                    )
                ).scalar_one()

            # Only on first discovery. The table is append-only by design, so a
            # genuinely new check should add a row -- but an activity retry is
            # not a new check, and letting one append would turn a retry storm
            # into a verification history that never happened.
            if newly_discovered and mx is not None:
                session.add(
                    ContactVerification(
                        workspace_id=workspace_id,
                        channel_id=channel_id,
                        provider="dns_mx",
                        result=_mx_verification_status(mx),
                        mx_present=mx.can_receive_mail,
                        detail=mx.as_verification_detail(),
                        verified_at=_now(),
                    )
                )

            await session.execute(
                Lead.__table__.update()  # type: ignore[attr-defined]
                .where(Lead.id == uuid.UUID(request.lead_id))
                .values(primary_contact_channel_id=channel_id)
            )
            return ContactActivityResult(eligible_channel_id=str(channel_id))

    if not discovered:
        rejected.append("no email address published on the crawled pages")
    return ContactActivityResult(
        eligible_channel_id=None, rejected_reasons=tuple(rejected[:8])
    )


async def _resolve_mx(domains: list[str]) -> dict[str, MxCheck]:
    """MX for each distinct domain, off the event loop.

    ``check_many`` uses a blocking resolver. Calling it directly would stall the
    whole worker for as long as DNS took, which on an unresponsive nameserver is
    the full eight-second timeout per domain.

    A resolver failure returns an ERROR check rather than raising: it is not
    evidence about the domain, and letting it propagate would abandon contact
    discovery for a lead whose address is probably fine.
    """
    if not domains:
        return {}
    try:
        result = await asyncio.to_thread(check_many, domains)
    except Exception as exc:
        logger.warning(
            "mx resolution failed for the whole batch; proceeding without it",
            extra={"error_code": type(exc).__name__, "domains": len(set(domains))},
        )
        return {}
    return dict(result.checks)


def _mx_verification_status(mx: MxCheck) -> VerificationStatus:
    """What an MX check is allowed to conclude about an address.

    Never PUBLISHED_FIRST_PARTY or PROVIDER_VERIFIED -- neither is in
    SENDABLE_VERIFICATION_STATUSES by accident, and DNS cannot establish that a
    mailbox exists. A positive result is recorded as UNKNOWN precisely so it
    cannot be mistaken for verification later.
    """
    if mx.is_conclusively_undeliverable:
        return VerificationStatus.INVALID
    return VerificationStatus.UNKNOWN


# ==========================================================================
# 5. Generate draft
# ==========================================================================
@activity.defn(name="generate_draft")
async def generate_draft(request: DraftActivityInput) -> DraftActivityResult:
    """Compose a message from evidence and validate it before it can be approved."""
    workspace_id = uuid.UUID(request.workspace_id)
    settings = get_settings()

    async with workspace_session(workspace_id) as session:
        existing = (
            await session.execute(
                select(MessageDraft).where(
                    MessageDraft.idempotency_key == request.idempotency_key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return DraftActivityResult(
                draft_id=str(existing.id),
                validation_passed=existing.validation_passed,
                violation_codes=tuple(
                    v.get("code", "")
                    for v in (existing.validation_report or {}).get("violations", [])
                ),
            )

        lead = await session.get(Lead, uuid.UUID(request.lead_id))
        if lead is None:
            raise ValueError(f"lead {request.lead_id} not found")
        org = await session.get(Organization, lead.organization_id)
        channel_row = await session.get(
            ContactChannel, uuid.UUID(request.contact_channel_id)
        )
        if org is None or channel_row is None:
            raise ValueError("draft references a missing organization or channel")
        org_domain = org.canonical_domain or org.display_name
        org_industry = org.industry
        channel_id = channel_row.id
        campaign_row = await session.get(Campaign, uuid.UUID(request.campaign_id))
        sender_row = (
            await session.get(SenderIdentity, campaign_row.sender_identity_id)
            if campaign_row and campaign_row.sender_identity_id
            else None
        )
        # The footer address comes from the sender identity that will actually
        # send; the process setting is only a deployment-wide default.
        mailing_address = (
            sender_row.mailing_address if sender_row else None
        ) or settings.sender_mailing_address
        findings = (
            (
                await session.execute(
                    select(AuditFinding)
                    .where(
                        AuditFinding.research_run_id
                        == uuid.UUID(request.research_run_id),
                        AuditFinding.contradicted.is_(False),
                    )
                    .order_by(AuditFinding.confidence.desc())
                )
            )
            .scalars()
            .all()
        )
        evidenced: dict[str, list[str]] = {}
        for finding in findings:
            rows = (
                (
                    await session.execute(
                        select(FindingEvidence.id).where(
                            FindingEvidence.finding_id == finding.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            if rows:
                evidenced[str(finding.id)] = [str(r) for r in rows]

    pitchable = [
        f
        for f in findings
        if str(f.id) in evidenced
        and f.severity in {Severity.HIGH, Severity.CRITICAL, Severity.MEDIUM}
    ]
    if not pitchable:
        return DraftActivityResult(
            draft_id="",
            validation_passed=False,
            violation_codes=("no_evidence_backed_claims",),
        )

    headline = pitchable[0]
    offers = select_offers(org_industry, {f.issue_type for f in pitchable})
    offer = offers[0] if offers else None

    portfolio = str(settings.owner_portfolio_url).rstrip("/")
    composed = compose(
        ComposerContext(
            org_domain=org_domain,
            finding=headline,
            evidence_ids=evidenced[str(headline.id)],
            owner_name=settings.owner_name,
            portfolio_url=portfolio,
            mailing_address=mailing_address or "",
            unsubscribe_url=f"{portfolio}/unsubscribe",
            solution=(
                offer.delivers if offer else "enquiry capture and follow-up automation"
            ),
            # The lead, so the same lead always composes to the same message.
            # Seeding on anything that varies between runs would produce a
            # second, differently worded draft on an activity retry.
            variant_seed=request.lead_id,
        )
    )
    subject, body, claim_map = composed.subject, composed.body, composed.claim_map

    report = validate_message(
        MessageContext(
            subject=subject,
            body=body,
            claim_map=claim_map,
            evidenced_finding_ids=frozenset(evidenced),
            sender_name=settings.owner_name,
            portfolio_url=str(settings.owner_portfolio_url).rstrip("/"),
            mailing_address=mailing_address,
            unsubscribe_present=True,
        )
    )

    async with workspace_unit_of_work(workspace_id) as session:
        draft = MessageDraft(
            workspace_id=workspace_id,
            lead_id=uuid.UUID(request.lead_id),
            campaign_id=uuid.UUID(request.campaign_id),
            contact_channel_id=channel_id,
            idempotency_key=request.idempotency_key,
            status=(
                DraftStatus.AWAITING_APPROVAL
                if report.passed
                else DraftStatus.VALIDATION_FAILED
            ),
            subject=subject,
            body_text=body,
            claim_map=claim_map,
            validation_report=report.to_json(),
            validation_passed=report.passed,
            template_key=request.template_key,
        )
        session.add(draft)
        await session.flush()
        await session.execute(
            Lead.__table__.update()  # type: ignore[attr-defined]
            .where(Lead.id == uuid.UUID(request.lead_id))
            .values(
                status=(
                    LeadStatus.AWAITING_APPROVAL if report.passed else LeadStatus.DRAFTED
                )
            )
        )
        draft_id = str(draft.id)

    return DraftActivityResult(
        draft_id=draft_id,
        validation_passed=report.passed,
        violation_codes=tuple(v.code.value for v in report.violations),
    )


# ==========================================================================
# 6. Queue
# ==========================================================================
@activity.defn(name="queue_message")
async def queue_message(request: QueueActivityInput) -> QueueActivityResult:
    """Write the outbox row. Does NOT send.

    The outbox worker re-evaluates the whole authorization chain immediately
    before any provider call, so this is a queueing step, not a delivery one.
    """
    workspace_id = uuid.UUID(request.workspace_id)

    async with workspace_unit_of_work(workspace_id) as session:
        draft = await session.get(MessageDraft, uuid.UUID(request.draft_id))
        if draft is None:
            return QueueActivityResult(
                outbox_id=None, queued=False, refused_reasons=("draft not found",)
            )
        if not draft.validation_passed:
            return QueueActivityResult(
                outbox_id=None,
                queued=False,
                refused_reasons=("draft did not pass message validation",),
            )

        channel = await session.get(ContactChannel, draft.contact_channel_id)
        campaign = await session.get(Campaign, draft.campaign_id)
        workspace = await session.get(Workspace, workspace_id)
        if channel is None or campaign is None:
            return QueueActivityResult(
                outbox_id=None,
                queued=False,
                refused_reasons=("draft references a missing channel or campaign",),
            )
        sender = (
            await session.get(SenderIdentity, campaign.sender_identity_id)
            if campaign.sender_identity_id
            else None
        )
        if sender is None:
            return QueueActivityResult(
                outbox_id=None,
                queued=False,
                refused_reasons=("campaign has no sender identity configured",),
            )

        suppressed = await is_suppressed(
            session, workspace_id=workspace_id, email=channel.normalized_value
        )
        if suppressed is not None:
            return QueueActivityResult(
                outbox_id=None,
                queued=False,
                refused_reasons=(f"recipient suppressed ({suppressed.reason.value})",),
            )

        dedupe = f"draft-{draft.id}"
        already = (
            await session.execute(
                select(OutboxMessage).where(OutboxMessage.dedupe_key == dedupe)
            )
        ).scalar_one_or_none()
        if already is not None:
            return QueueActivityResult(outbox_id=str(already.id), queued=True)

        # The dedupe key above is per *draft*, so it collapses a retry of this
        # activity and nothing else. Two drafts for one lead -- which is what a
        # re-run of the research pipeline produces -- get two keys and both
        # queue. Found in a live outbox: seventeen recipients each holding two
        # pending messages from the same campaign, differing only in which
        # finding they led with.
        #
        # Refused rather than collapsed, because the second message is not a
        # follow-up. A real follow-up is scheduled by FollowUpScheduler after
        # the first has *sent* and a spacing interval has passed; two arriving
        # together is the thing that reads as careless and generates the
        # complaint.
        waiting = (
            await session.execute(
                select(OutboxMessage.id).where(
                    OutboxMessage.campaign_id == draft.campaign_id,
                    OutboxMessage.to_email_normalized == channel.normalized_value,
                    OutboxMessage.status.in_(_UNSENT_OUTBOX_STATUSES),
                )
            )
        ).scalar_one_or_none()
        if waiting is not None:
            logger.info(
                "refusing a second queued message to a recipient already waiting",
                extra={
                    "campaign_id": str(draft.campaign_id),
                    "outbox_id": str(waiting),
                },
            )
            return QueueActivityResult(
                outbox_id=None,
                queued=False,
                refused_reasons=(
                    "a message to this recipient is already queued for this "
                    "campaign and has not been sent",
                ),
            )

        message = Message(
            workspace_id=workspace_id,
            draft_id=draft.id,
            lead_id=draft.lead_id,
            campaign_id=draft.campaign_id,
            sender_identity_id=sender.id,
            dedupe_key=dedupe,
            to_email=channel.value,
            to_email_normalized=channel.normalized_value,
            to_domain=channel.value_domain or "",
            from_email=sender.from_email,
            subject=draft.subject,
            state=MessageState.QUEUED,
            state_rank=0,
            provider=get_settings().email_provider,
        )
        session.add(message)
        await session.flush()

        outbox = OutboxMessage(
            workspace_id=workspace_id,
            message_id=message.id,
            draft_id=draft.id,
            approval_id=(uuid.UUID(request.approval_id) if request.approval_id else None),
            campaign_id=draft.campaign_id,
            lead_id=draft.lead_id,
            sender_identity_id=sender.id,
            dedupe_key=dedupe,
            # Stored BEFORE the first attempt, so every retry presents the same
            # key and the provider collapses a duplicate.
            provider_idempotency_key=f"idem-{dedupe}",
            status=OutboxStatus.PENDING,
            to_email_normalized=channel.normalized_value,
            to_domain=channel.value_domain or "",
            next_attempt_at=_now(),
            payload={
                "to_email": channel.value,
                "from_email": sender.from_email,
                "from_name": sender.from_name,
                "reply_to": sender.reply_to_email,
                "subject": draft.subject,
                "text_body": draft.body_text,
                "list_unsubscribe": f"<{sender.unsubscribe_mailto}>"
                if sender.unsubscribe_mailto
                else None,
            },
        )
        session.add(outbox)
        await session.flush()
        draft.status = DraftStatus.QUEUED
        _ = workspace
        return QueueActivityResult(outbox_id=str(outbox.id), queued=True)


#: Registered with the Temporal worker. Temporal's decorator returns an
#: untyped callable, so the element type is widened deliberately.
ALL_PIPELINE_ACTIVITIES: list[Callable[..., Any]] = [
    crawl_lead_website,
    analyse_evidence,
    score_lead,
    resolve_contact,
    generate_draft,
    queue_message,
]

__all__ = [
    "ALL_PIPELINE_ACTIVITIES",
    "analyse_evidence",
    "crawl_lead_website",
    "generate_draft",
    "queue_message",
    "resolve_contact",
    "score_lead",
]
