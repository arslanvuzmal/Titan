"""The six pipeline activities the research workflow drives.

Each is idempotent on a key supplied by the workflow: a retry must find its own
prior work rather than repeat it. Where an activity writes evidence, the write
is keyed on a content fingerprint so a re-crawl of an unchanged page produces no
duplicate rows.

None of these sends anything. ``queue_message`` writes an outbox row; the outbox
worker re-evaluates the entire authorization chain before any provider call.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from temporalio import activity

from titan.config import get_settings
from titan.contracts.evidence import CrawlResult, fingerprint
from titan.db.enums import (
    ContactSource,
    DraftStatus,
    LeadStatus,
    MessageState,
    OutboxStatus,
    Severity,
    VerificationStatus,
)
from titan.db.models import (
    AuditFinding,
    BrowserArtifact,
    Campaign,
    CampaignPolicy,
    Contact,
    ContactChannel,
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
    Workspace,
)
from titan.db.session import workspace_session, workspace_unit_of_work
from titan.delivery.suppression import is_suppressed
from titan.intelligence.contacts import (
    check_contact_eligibility,
    extract_contacts_from_pages,
)
from titan.intelligence.findings import detect_findings
from titan.intelligence.message_validator import MessageContext, validate_message
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
        org = await session.get(Organization, lead.organization_id) if lead else None
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
                pg_insert(Page.__table__)
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
                pg_insert(BrowserArtifact.__table__)
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
                pg_insert(AuditFinding.__table__)
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
                    pg_insert(FindingEvidence.__table__)
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
            ResearchRun.__table__.update()
            .where(ResearchRun.id == uuid.UUID(request.research_run_id))
            .values(findings_count=created, pages_crawled=len(evidence))
        )

    top = detected[0].issue_type if detected else None
    return AnalyseActivityResult(
        findings_created=created, pitchable_findings=pitchable, top_issue_type=top
    )


# ==========================================================================
# 3. Score
# ==========================================================================
@activity.defn(name="score_lead")
async def score_lead(request: ScoreActivityInput) -> ScoreActivityResult:
    """Deterministic, explainable score. Persisted immutably."""
    workspace_id = uuid.UUID(request.workspace_id)

    async with workspace_session(workspace_id) as session:
        lead = await session.get(Lead, uuid.UUID(request.lead_id))
        org = await session.get(Organization, lead.organization_id)
        campaign = await session.get(Campaign, uuid.UUID(request.campaign_id))
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
        org_snapshot = {
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
            Lead.__table__.update()
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


def _to_detected(row: AuditFinding):
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
        org = await session.get(Organization, lead.organization_id)
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

    for candidate in discovered:
        if not candidate.is_usable:
            rejected.append(f"{candidate.normalized}: {candidate.rejection_reason}")
            continue

        verdict = check_contact_eligibility(
            source=candidate.source,
            # First-party published is the provenance; no external verification
            # provider is configured, and MX presence would not upgrade this.
            verification=VerificationStatus.PUBLISHED_FIRST_PARTY,
            is_active=True,
            allowed_sources=allowed,
            require_verified=require_verified,
            email=candidate.normalized,
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
                pg_insert(ContactChannel.__table__)
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
            if channel_id is None:
                channel_id = (
                    await session.execute(
                        select(ContactChannel.id).where(
                            ContactChannel.workspace_id == workspace_id,
                            ContactChannel.normalized_value == candidate.normalized,
                        )
                    )
                ).scalar_one()

            await session.execute(
                Lead.__table__.update()
                .where(Lead.id == uuid.UUID(request.lead_id))
                .values(primary_contact_channel_id=channel_id)
            )
            return ContactActivityResult(eligible_channel_id=str(channel_id))

    if not discovered:
        rejected.append("no email address published on the crawled pages")
    return ContactActivityResult(
        eligible_channel_id=None, rejected_reasons=tuple(rejected[:8])
    )


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
        org = await session.get(Organization, lead.organization_id)
        org_domain = org.canonical_domain or org.display_name
        org_industry = org.industry
        channel = await session.get(ContactChannel, uuid.UUID(request.contact_channel_id))
        channel_id = channel.id
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

    subject, body, claim_map = _compose(
        org_domain=org_domain,
        finding=headline,
        offer=offer,
        settings=settings,
        mailing_address=mailing_address,
        evidence_ids=evidenced[str(headline.id)],
    )

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
            Lead.__table__.update()
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


def _compose(
    *, org_domain, finding, offer, settings, mailing_address, evidence_ids: list[str]
):
    """Build the message from the evidence.

    Deliberately template-driven rather than model-generated in this build: the
    validator is the gate, and a deterministic composer means the claim map and
    the sentence cannot drift apart. The model gateway is wired and available to
    replace this once its output is held to the same claim-map contract.
    """
    portfolio = str(settings.owner_portfolio_url).rstrip("/")
    address = mailing_address or ""
    domain = org_domain

    claim = (
        f"On {domain} the {_describe(finding)}, so anyone who gets that far "
        f"cannot complete the step."
    )
    # Must be a NOUN PHRASE. An imperative here reads as "I build point the
    # button at a tested flow", and naming a page element turns the sentence
    # into an unsupported claim about the recipient's site.
    solution = offer.delivers if offer else "enquiry capture and follow-up automation"

    # The impact line is Titan's own description of the SAME evidenced finding,
    # so it is a claim about the recipient's site and belongs in the claim map.
    # Omitting it made the validator reject the draft -- correctly.
    impact = (
        finding.business_impact
        or "That is the step most likely to be used by someone ready to act"
    ).rstrip(".") + "."
    offer_line = (
        f"I build {solution.lower()} for firms of this size, and could outline "
        "what it would take in about ten minutes."
    )

    body = (
        "Hi there,\n\n"
        f"{claim}\n\n"
        f"{impact} {offer_line}\n\n"
        "Would a short call next week be useful?\n\n"
        f"{settings.owner_name}\n"
        f"{portfolio}\n"
        f"{address}\n"
        f"Unsubscribe: {portfolio}/unsubscribe\n"
    )
    subject = f"A broken step on {domain}"[:120]
    claim_map = [
        {
            "sentence": claim,
            "claim": finding.issue_type,
            "finding_id": str(finding.id),
            "evidence_ids": evidence_ids,
            "source_url": finding.page_url,
        },
        {
            "sentence": impact,
            "claim": f"{finding.issue_type}:business_impact",
            "finding_id": str(finding.id),
            "evidence_ids": evidence_ids,
            "source_url": finding.page_url,
        },
    ]
    return subject, body, claim_map


def _describe(finding) -> str:
    observed = (finding.observed_value or "").strip()
    mapping = {
        "broken_primary_cta": f"main call-to-action button returns {observed or 'nothing'}",
        "no_booking_or_enquiry_path": "site has no booking link or enquiry form",
        "high_friction_contact_form": f"enquiry form asks for {observed} fields",
        "missing_mobile_viewport": "homepage has no mobile viewport tag",
        "broken_internal_link": f"navigation links to a page that returns {observed}",
        "javascript_console_errors": "page raises JavaScript errors on load",
        "no_visible_phone_number": "pages carry no phone number",
    }
    return mapping.get(finding.issue_type, finding.title.lower())


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


ALL_PIPELINE_ACTIVITIES = [
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
