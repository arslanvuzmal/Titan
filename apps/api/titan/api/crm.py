"""CRM read surface.

The rest of ``/api/v1`` is the machine-facing contract: narrow models, one
resource per route. This module is the operator-facing one. A person working a
lead needs the business, its contacts and their provenance, the findings that
justify a claim, the drafts, and what was actually delivered -- assembled, not
scattered across eight requests.

Three rules shape everything here:

* **Nothing is invented.** Every field is a stored column or a live ``COUNT``.
  Where a value is unknown it is ``null``, never a plausible substitute.
* **Eligibility is shown, not implied.** A pattern-guessed address is returned
  so the operator can see Titan found it, flagged with the reason it can never
  be contacted. Hiding it would make the CRM look emptier than reality;
  showing it unflagged would invite someone to paste it into their mail client.
* **List endpoints do not N+1.** Counts arrive as grouped subqueries, so the
  lead list is a fixed number of statements regardless of page size.

This module is read-only by design. Mutations stay in ``routes.py`` where the
capability checks and audit writes live.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, func, or_, select
from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from titan.api.schemas import (
    ContactChannelOut,
    ContactOut,
    CrmStatsOut,
    DraftOut,
    LeadOut,
    MeetingOut,
    MessageOut,
    OpportunityOut,
    OrganizationLocationOut,
    OrganizationOut,
    OrganizationSummary,
    OutcomeRollupOut,
    OutcomeSliceOut,
    PortfolioOut,
    RecipientDomainOut,
    RecipientDomainsOut,
    RegionSliceOut,
    TimelineEventOut,
    TimingReportOut,
    TimingSlotOut,
    VariantArmOut,
    VariantComparisonOut,
)
from titan.api.security import Principal, require
from titan.autonomy import experiments
from titan.config import get_settings
from titan.db.enums import ContactSource, LeadStatus
from titan.db.models import (
    AuditFinding,
    BusinessOpportunity,
    Campaign,
    CampaignPolicy,
    Contact,
    ContactChannel,
    FindingEvidence,
    Lead,
    LeadScore,
    Meeting,
    Message,
    MessageApproval,
    MessageDraft,
    Organization,
    OrganizationDomain,
    OrganizationLocation,
    ResearchRun,
    SuppressionEntry,
    Workspace,
)
from titan.db.session import workspace_session
from titan.delivery.deliverability import MIN_SAMPLE_FOR_RATES
from titan.delivery.suppression import is_suppressed
from titan.intelligence import insights, timing
from titan.intelligence import domain_health, insights, timing
from titan.intelligence import portfolio as portfolio_mod
from titan.intelligence.contacts import check_contact_eligibility
from titan.intelligence.rollups import (
    DEFAULT_WINDOW_DAYS,
    Dimension,
    outcomes_by,
)
from titan.intelligence.scoring import band_for

router = APIRouter(prefix="/api/v1", tags=["crm"])


async def _not_found(kind: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{kind} not found")


# ==========================================================================
# Shared enrichment
# ==========================================================================
async def _primary_locations(
    session: AsyncSession, org_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, OrganizationLocation]:
    """One location per organization, preferring the primary one."""
    if not org_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(OrganizationLocation)
                .where(OrganizationLocation.organization_id.in_(org_ids))
                .order_by(
                    OrganizationLocation.organization_id,
                    OrganizationLocation.is_primary.desc(),
                    OrganizationLocation.created_at.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    out: dict[uuid.UUID, OrganizationLocation] = {}
    for row in rows:
        out.setdefault(row.organization_id, row)
    return out


def _org_summary(
    org: Organization, location: OrganizationLocation | None
) -> OrganizationSummary:
    return OrganizationSummary(
        id=org.id,
        display_name=org.display_name,
        canonical_domain=org.canonical_domain,
        website_url=org.website_url,
        industry=org.industry.value,
        phone_e164=org.phone_e164,
        rating=org.rating,
        review_count=org.review_count,
        business_status=org.business_status,
        locality=location.locality if location else None,
        region=location.region if location else None,
        country_code=location.country_code if location else None,
    )


async def _counts_by_lead(
    session: AsyncSession, column: Any, model: Any, lead_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """One grouped COUNT for a whole page of leads."""
    if not lead_ids:
        return {}
    rows = await session.execute(
        select(column, func.count())
        .select_from(model)
        .where(column.in_(lead_ids))
        .group_by(column)
    )
    return {row[0]: int(row[1]) for row in rows}


async def enrich_leads(session: AsyncSession, leads: Sequence[Lead]) -> list[LeadOut]:
    """Attach the business, the campaign name, and activity counts.

    Six statements total, regardless of how many leads are on the page.
    """
    if not leads:
        return []

    lead_ids = [lead.id for lead in leads]
    org_ids = sorted({lead.organization_id for lead in leads})
    campaign_ids = sorted({lead.campaign_id for lead in leads})

    orgs = {
        org.id: org
        for org in (
            await session.execute(
                select(Organization).where(Organization.id.in_(org_ids))
            )
        )
        .scalars()
        .all()
    }
    locations = await _primary_locations(session, org_ids)
    campaign_names = {
        row[0]: row[1]
        for row in await session.execute(
            select(Campaign.id, Campaign.name).where(Campaign.id.in_(campaign_ids))
        )
    }

    findings = await _counts_by_lead(
        session, AuditFinding.lead_id, AuditFinding, lead_ids
    )
    drafts = await _counts_by_lead(session, MessageDraft.lead_id, MessageDraft, lead_ids)
    messages = await _counts_by_lead(session, Message.lead_id, Message, lead_ids)

    # Evidence is one join away from the lead, so it needs its own statement
    # rather than the grouped helper.
    evidence: dict[uuid.UUID, int] = {}
    if lead_ids:
        evidence = {
            row[0]: int(row[1])
            for row in await session.execute(
                select(AuditFinding.lead_id, func.count(FindingEvidence.id))
                .select_from(AuditFinding)
                .join(FindingEvidence, FindingEvidence.finding_id == AuditFinding.id)
                .where(AuditFinding.lead_id.in_(lead_ids))
                .group_by(AuditFinding.lead_id)
            )
        }

    # "Has a contactable address" is the single most decision-relevant fact in
    # the list, so it is computed here rather than left to a per-row fetch.
    eligible_orgs: set[uuid.UUID] = set()
    if org_ids:
        rows = await session.execute(
            select(Contact.organization_id, ContactChannel.source)
            .select_from(ContactChannel)
            .join(Contact, Contact.id == ContactChannel.contact_id)
            .where(
                Contact.organization_id.in_(org_ids),
                ContactChannel.channel_type == "email",
                ContactChannel.is_active.is_(True),
            )
        )
        for org_id, source in rows:
            if source is not ContactSource.PATTERN_GUESS:
                eligible_orgs.add(org_id)

    out: list[LeadOut] = []
    for lead in leads:
        org = orgs.get(lead.organization_id)
        out.append(
            LeadOut(
                id=lead.id,
                campaign_id=lead.campaign_id,
                organization_id=lead.organization_id,
                status=lead.status.value,
                latest_score=lead.latest_score,
                replied_at=lead.replied_at,
                last_contacted_at=lead.last_contacted_at,
                followups_sent=lead.followups_sent,
                status_reason=lead.status_reason,
                next_action_at=lead.next_action_at,
                created_at=lead.created_at,
                organization=(
                    _org_summary(org, locations.get(org.id)) if org is not None else None
                ),
                campaign_name=campaign_names.get(lead.campaign_id),
                finding_count=findings.get(lead.id, 0),
                draft_count=drafts.get(lead.id, 0),
                message_count=messages.get(lead.id, 0),
                evidence_count=evidence.get(lead.id, 0),
                has_eligible_contact=lead.organization_id in eligible_orgs,
            )
        )
    return out


def apply_lead_filters(
    stmt: Select[Any],
    *,
    campaign_id: uuid.UUID | None,
    lead_status: str | None,
    min_score: int | None,
    max_score: int | None,
    search: str | None,
    has_reply: bool | None,
    contacted: bool | None,
) -> Select[Any]:
    """Filters shared by the list route and its total count.

    Applied to both statements from one place so a page can never report a
    total that disagrees with the rows it returned.
    """
    if campaign_id:
        stmt = stmt.where(Lead.campaign_id == campaign_id)
    if lead_status:
        stmt = stmt.where(Lead.status == LeadStatus(lead_status))
    if min_score is not None:
        stmt = stmt.where(Lead.latest_score >= min_score)
    if max_score is not None:
        stmt = stmt.where(Lead.latest_score <= max_score)
    if has_reply is not None:
        stmt = stmt.where(
            Lead.replied_at.is_not(None) if has_reply else Lead.replied_at.is_(None)
        )
    if contacted is not None:
        stmt = stmt.where(
            Lead.last_contacted_at.is_not(None)
            if contacted
            else Lead.last_contacted_at.is_(None)
        )
    if search:
        term = f"%{search.strip().lower()}%"
        stmt = stmt.join(Organization, Organization.id == Lead.organization_id).where(
            or_(
                func.lower(Organization.display_name).like(term),
                func.lower(Organization.canonical_domain).like(term),
                func.lower(Organization.normalized_name).like(term),
            )
        )
    return stmt


# ==========================================================================
# Organizations
# ==========================================================================
@router.get("/organizations/{organization_id}", response_model=OrganizationOut)
async def get_organization(
    organization_id: uuid.UUID,
    principal: Principal = Depends(require("research:read")),
) -> OrganizationOut:
    async with workspace_session(principal.workspace_id) as session:
        org = await session.get(Organization, organization_id)
        if org is None:
            raise await _not_found("organization")

        locations = (
            (
                await session.execute(
                    select(OrganizationLocation)
                    .where(OrganizationLocation.organization_id == org.id)
                    .order_by(OrganizationLocation.is_primary.desc())
                )
            )
            .scalars()
            .all()
        )
        domains = [
            row[0]
            for row in await session.execute(
                select(OrganizationDomain.domain)
                .where(OrganizationDomain.organization_id == org.id)
                .order_by(OrganizationDomain.is_primary.desc())
            )
        ]
        primary = locations[0] if locations else None
        summary = _org_summary(org, primary)
        return OrganizationOut(
            **summary.model_dump(),
            legal_name=org.legal_name,
            normalized_name=org.normalized_name,
            google_place_id=org.google_place_id,
            employee_estimate=org.employee_estimate,
            provenance=org.provenance,
            locations=[OrganizationLocationOut.model_validate(x) for x in locations],
            domains=domains,
            created_at=org.created_at,
        )


# ==========================================================================
# Contacts
# ==========================================================================
@router.get("/leads/{lead_id}/contacts", response_model=list[ContactOut])
async def lead_contacts(
    lead_id: uuid.UUID,
    principal: Principal = Depends(require("research:read")),
) -> list[ContactOut]:
    """Contacts for a lead's organization, each channel labelled with why it
    is or is not contactable under this campaign's policy."""
    async with workspace_session(principal.workspace_id) as session:
        lead = await session.get(Lead, lead_id)
        if lead is None:
            raise await _not_found("lead")

        policy = (
            await session.execute(
                select(CampaignPolicy).where(
                    CampaignPolicy.campaign_id == lead.campaign_id
                )
            )
        ).scalar_one_or_none()
        allowed = (
            frozenset(ContactSource(s) for s in policy.allowed_contact_sources)
            if policy is not None
            else frozenset()
        )
        require_verified = policy.require_verified_email if policy else True

        contacts = (
            (
                await session.execute(
                    select(Contact)
                    .where(Contact.organization_id == lead.organization_id)
                    .order_by(Contact.is_decision_maker.desc(), Contact.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

        out: list[ContactOut] = []
        for contact in contacts:
            channels: list[ContactChannelOut] = []
            for channel in contact.channels:
                model = ContactChannelOut(
                    id=channel.id,
                    channel_type=channel.channel_type,
                    value=channel.value,
                    normalized_value=channel.normalized_value,
                    value_domain=channel.value_domain,
                    source=channel.source.value,
                    source_url=channel.source_url,
                    discovered_at=channel.discovered_at,
                    verification_status=channel.verification_status.value,
                    confidence=channel.confidence,
                    consent_basis=channel.consent_basis,
                    is_active=channel.is_active,
                )
                if channel.channel_type == "email":
                    result = check_contact_eligibility(
                        source=channel.source,
                        verification=channel.verification_status,
                        is_active=channel.is_active,
                        allowed_sources=allowed,
                        require_verified=require_verified,
                        email=channel.normalized_value,
                    )
                    entry = await is_suppressed(
                        session,
                        workspace_id=principal.workspace_id,
                        email=channel.normalized_value,
                    )
                    model.suppressed = entry is not None
                    model.eligible_for_outreach = result.eligible and entry is None
                    reasons = list(result.reasons)
                    if entry is not None:
                        reasons.append(f"suppressed: {entry.reason.value}")
                    model.ineligibility_reason = "; ".join(reasons) or None
                else:
                    model.ineligibility_reason = (
                        f"{channel.channel_type} is not an outreach channel"
                    )
                channels.append(model)

            out.append(
                ContactOut(
                    id=contact.id,
                    organization_id=contact.organization_id,
                    full_name=contact.full_name,
                    role_title=contact.role_title,
                    is_decision_maker=contact.is_decision_maker,
                    is_generic_role=contact.is_generic_role,
                    notes=contact.notes,
                    channels=channels,
                )
            )
        return out


# ==========================================================================
# Per-lead drafts and messages
# ==========================================================================
@router.get("/leads/{lead_id}/drafts", response_model=list[DraftOut])
async def lead_drafts(
    lead_id: uuid.UUID,
    principal: Principal = Depends(require("draft:read")),
) -> list[DraftOut]:
    async with workspace_session(principal.workspace_id) as session:
        if await session.get(Lead, lead_id) is None:
            raise await _not_found("lead")
        rows = (
            (
                await session.execute(
                    select(MessageDraft)
                    .where(MessageDraft.lead_id == lead_id)
                    .order_by(MessageDraft.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [DraftOut.model_validate(r) for r in rows]


@router.get("/leads/{lead_id}/messages", response_model=list[MessageOut])
async def lead_messages(
    lead_id: uuid.UUID,
    principal: Principal = Depends(require("research:read")),
) -> list[MessageOut]:
    async with workspace_session(principal.workspace_id) as session:
        if await session.get(Lead, lead_id) is None:
            raise await _not_found("lead")
        rows = (
            (
                await session.execute(
                    select(Message)
                    .where(Message.lead_id == lead_id)
                    .order_by(Message.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [MessageOut.model_validate(r) for r in rows]


# ==========================================================================
# Timeline
# ==========================================================================
@router.get("/leads/{lead_id}/timeline", response_model=list[TimelineEventOut])
async def lead_timeline(
    lead_id: uuid.UUID,
    principal: Principal = Depends(require("research:read")),
) -> list[TimelineEventOut]:
    """Everything that happened to this lead, newest first.

    Derived from the record tables at read time. There is no separate event
    log to fall out of step with them, and nothing appears here that is not
    also a row somewhere.
    """
    async with workspace_session(principal.workspace_id) as session:
        lead = await session.get(Lead, lead_id)
        if lead is None:
            raise await _not_found("lead")

        events: list[TimelineEventOut] = [
            TimelineEventOut(
                at=lead.created_at,
                kind="lead.discovered",
                title="Lead discovered",
                detail=f"status {lead.status.value}",
                reference_id=lead.id,
            )
        ]

        for run in (
            (
                await session.execute(
                    select(ResearchRun).where(ResearchRun.lead_id == lead_id)
                )
            )
            .scalars()
            .all()
        ):
            events.append(
                TimelineEventOut(
                    at=run.created_at,
                    kind="research.run",
                    title=f"Research run {run.status}",
                    detail=run.failure_reason,
                    reference_id=run.id,
                )
            )

        for finding in (
            (
                await session.execute(
                    select(AuditFinding).where(AuditFinding.lead_id == lead_id)
                )
            )
            .scalars()
            .all()
        ):
            events.append(
                TimelineEventOut(
                    at=finding.created_at,
                    kind="finding.detected",
                    title=finding.title,
                    detail=finding.page_url,
                    reference_id=finding.id,
                    severity=finding.severity.value,
                )
            )

        for score in (
            (await session.execute(select(LeadScore).where(LeadScore.lead_id == lead_id)))
            .scalars()
            .all()
        ):
            events.append(
                TimelineEventOut(
                    at=score.created_at,
                    kind="lead.scored",
                    title=f"Scored {score.total} ({score.band})",
                    detail=f"threshold {score.threshold_applied}",
                    reference_id=score.id,
                )
            )

        drafts = (
            (
                await session.execute(
                    select(MessageDraft).where(MessageDraft.lead_id == lead_id)
                )
            )
            .scalars()
            .all()
        )
        draft_ids = [d.id for d in drafts]
        for draft in drafts:
            events.append(
                TimelineEventOut(
                    at=draft.created_at,
                    kind="draft.generated",
                    title=f"Draft v{draft.version}: {draft.subject}",
                    detail=(
                        "validation passed"
                        if draft.validation_passed
                        else "validation FAILED"
                    ),
                    reference_id=draft.id,
                    severity=None if draft.validation_passed else "high",
                )
            )

        if draft_ids:
            for approval in (
                (
                    await session.execute(
                        select(MessageApproval).where(
                            MessageApproval.draft_id.in_(draft_ids)
                        )
                    )
                )
                .scalars()
                .all()
            ):
                events.append(
                    TimelineEventOut(
                        at=approval.decided_at,
                        kind="draft.reviewed",
                        title=f"Review: {approval.decision}",
                        detail=approval.reason,
                        reference_id=approval.draft_id,
                    )
                )

        for message in (
            (await session.execute(select(Message).where(Message.lead_id == lead_id)))
            .scalars()
            .all()
        ):
            for when, kind, title in (
                (message.sent_at, "message.sent", "Message sent"),
                (message.delivered_at, "message.delivered", "Message delivered"),
                (message.bounced_at, "message.bounced", "Message bounced"),
                (message.complained_at, "message.complained", "Spam complaint"),
            ):
                if when is not None:
                    events.append(
                        TimelineEventOut(
                            at=when,
                            kind=kind,
                            title=title,
                            detail=message.to_email_normalized,
                            reference_id=message.id,
                            severity=(
                                "high"
                                if kind in {"message.bounced", "message.complained"}
                                else None
                            ),
                        )
                    )

        if lead.replied_at is not None:
            events.append(
                TimelineEventOut(
                    at=lead.replied_at,
                    kind="lead.replied",
                    title="Reply received",
                    detail="all further outreach is stopped for this lead",
                    reference_id=lead.id,
                )
            )

        events.sort(key=lambda e: e.at, reverse=True)
        return events


# ==========================================================================
# Overview counters
# ==========================================================================
async def _group_count(session: AsyncSession, column: Any, model: Any) -> dict[str, int]:
    rows = await session.execute(
        select(column, func.count()).select_from(model).group_by(column)
    )
    out: dict[str, int] = {}
    for value, count in rows:
        key = value.value if hasattr(value, "value") else str(value)
        out[key] = int(count)
    return out


@router.get("/stats", response_model=CrmStatsOut)
async def crm_stats(
    principal: Principal = Depends(require("research:read")),
) -> CrmStatsOut:
    settings = get_settings()
    async with workspace_session(principal.workspace_id) as session:
        workspace = await session.get(Workspace, principal.workspace_id)
        if workspace is None:
            raise await _not_found("workspace")

        async def count(model: Any) -> int:
            return int(await session.scalar(select(func.count()).select_from(model)) or 0)

        bands: dict[str, int] = {}
        for (score,) in await session.execute(
            select(Lead.latest_score).where(Lead.latest_score.is_not(None))
        ):
            bands[band_for(int(score)).value] = (
                bands.get(band_for(int(score)).value, 0) + 1
            )

        eligible = int(
            await session.scalar(
                select(func.count())
                .select_from(ContactChannel)
                .where(
                    ContactChannel.channel_type == "email",
                    ContactChannel.is_active.is_(True),
                    ContactChannel.source != ContactSource.PATTERN_GUESS,
                )
            )
            or 0
        )
        replied = int(
            await session.scalar(
                select(func.count()).select_from(Lead).where(Lead.replied_at.is_not(None))
            )
            or 0
        )

        async def opportunities(deliverable: bool) -> int:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(BusinessOpportunity)
                    .where(BusinessOpportunity.deliverable.is_(deliverable))
                )
                or 0
            )

        unscheduled = int(
            await session.scalar(
                select(func.count())
                .select_from(Meeting)
                .where(Meeting.status == "proposed", Meeting.scheduled_at.is_(None))
            )
            or 0
        )

        return CrmStatsOut(
            leads_total=await count(Lead),
            leads_by_status=await _group_count(session, Lead.status, Lead),
            leads_by_band=bands,
            campaigns_total=await count(Campaign),
            organizations_total=await count(Organization),
            contacts_total=await count(Contact),
            eligible_contacts=eligible,
            findings_total=await count(AuditFinding),
            evidence_total=await count(FindingEvidence),
            drafts_by_status=await _group_count(
                session, MessageDraft.status, MessageDraft
            ),
            messages_by_state=await _group_count(session, Message.state, Message),
            suppressions_total=await count(SuppressionEntry),
            replied_total=replied,
            opportunities_deliverable=await opportunities(True),
            opportunities_unserved=await opportunities(False),
            meetings_total=await count(Meeting),
            meetings_unscheduled=unscheduled,
            # Both must be true for anything to leave the building; the CRM
            # reports the conjunction rather than the workspace flag alone.
            sending_authorized=(
                workspace.sending_authorized and settings.production_sending_enabled
            ),
            operating_mode=workspace.operating_mode.value,
        )


# ==========================================================================
# Outcomes
#
# The two tables that record what the pipeline produced rather than what it
# did. Both were filled by the research and reply paths and had no reader:
# opportunities since the analyse stage began deriving them, meetings since a
# reply asking for a call began opening one.
# ==========================================================================
@router.get("/opportunities", response_model=list[OpportunityOut])
async def list_opportunities(
    deliverable: bool | None = None,
    limit: int = 100,
    principal: Principal = Depends(require("research:read")),
) -> list[OpportunityOut]:
    """Commercial opportunities, highest priority first.

    ``deliverable=false`` returns the gaps: problems evidenced on a site that
    no offer in the playbook covers. They are listed because hiding them would
    make the audit look like the site was sound in that respect, and they carry
    no price because there is nothing to sell against them.
    """
    async with workspace_session(principal.workspace_id) as session:
        query = (
            select(BusinessOpportunity, Organization.display_name)
            .join(Lead, Lead.id == BusinessOpportunity.lead_id)
            .join(Organization, Organization.id == Lead.organization_id)
            .order_by(
                BusinessOpportunity.priority.desc(),
                BusinessOpportunity.created_at.desc(),
            )
            .limit(max(1, min(limit, 500)))
        )
        if deliverable is not None:
            query = query.where(BusinessOpportunity.deliverable.is_(deliverable))

        return [
            OpportunityOut(
                id=row.id,
                lead_id=row.lead_id,
                organization_name=name,
                offer_key=row.offer_key,
                title=row.title,
                rationale=row.rationale,
                estimated_value_usd=row.estimated_value_usd,
                priority=row.priority,
                deliverable=row.deliverable,
                supporting_finding_count=len(row.supporting_finding_ids or []),
                created_at=row.created_at,
            )
            for row, name in await session.execute(query)
        ]


@router.get("/meetings", response_model=list[MeetingOut])
async def list_meetings(
    unscheduled_only: bool = False,
    limit: int = 100,
    principal: Principal = Depends(require("research:read")),
) -> list[MeetingOut]:
    """Calls somebody asked for.

    Unscheduled first, because that is the queue: every meeting Titan opens has
    no time on it, and one that has sat there a fortnight is the most
    embarrassing row in the system.
    """
    async with workspace_session(principal.workspace_id) as session:
        query = (
            select(Meeting, Organization.display_name)
            .join(Lead, Lead.id == Meeting.lead_id)
            .join(Organization, Organization.id == Lead.organization_id)
            .order_by(
                Meeting.scheduled_at.is_(None).desc(),
                Meeting.created_at.desc(),
            )
            .limit(max(1, min(limit, 500)))
        )
        if unscheduled_only:
            query = query.where(Meeting.scheduled_at.is_(None))

        return [
            MeetingOut(
                id=row.id,
                lead_id=row.lead_id,
                organization_name=name,
                status=row.status,
                scheduled_at=row.scheduled_at,
                duration_minutes=row.duration_minutes,
                location_or_link=row.location_or_link,
                notes=row.notes,
                created_at=row.created_at,
            )
            for row, name in await session.execute(query)
        ]


@router.get("/analytics/outcomes", response_model=list[OutcomeRollupOut], tags=["crm"])
async def outcome_rollups(
    principal: Principal = Depends(require("research:read")),
    dimension: str | None = Query(
        None,
        description=(
            "campaign, sender, recipient_domain, lead_source, local_slot or "
            "variant. Omitted returns every grouping."
        ),
    ),
    window_days: int = Query(DEFAULT_WINDOW_DAYS, ge=1, le=365),
    campaign_id: uuid.UUID | None = Query(None),
) -> list[OutcomeRollupOut]:
    """Delivery outcomes, grouped the ways decisions are actually made.

    The counters are the same across every grouping on purpose. A bounce rate
    computed one way for senders and another for domains would make the two
    incomparable, and comparing them is what this is for.

    Rates come back null below the sample floor rather than as a number nobody
    should act on. A client rendering these must show "not enough data yet"
    rather than 0% -- the difference between "measured clean" and "not measured"
    is the whole point.
    """
    if dimension is not None:
        try:
            wanted = [Dimension(dimension)]
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"unknown dimension {dimension!r}; expected one of "
                + ", ".join(d.value for d in Dimension),
            ) from None
    else:
        wanted = list(Dimension)

    now = dt.datetime.now(dt.UTC)
    rollups: list[OutcomeRollupOut] = []
    async with workspace_session(principal.workspace_id) as session:
        for each in wanted:
            slices = await outcomes_by(
                session,
                each,
                now=now,
                window_days=window_days,
                campaign_id=campaign_id,
            )
            rollups.append(
                OutcomeRollupOut(
                    dimension=each.value,
                    window_days=window_days,
                    sample_floor=MIN_SAMPLE_FOR_RATES,
                    slices=[
                        OutcomeSliceOut(
                            key=s.key,
                            label=s.label,
                            sent=s.sent,
                            delivered=s.delivered,
                            bounced=s.bounced,
                            complained=s.complained,
                            replied=s.replied,
                            positive_replies=s.positive_replies,
                            meetings=s.meetings,
                            has_signal=s.has_signal,
                            bounce_rate=s.bounce_rate,
                            reply_rate=s.reply_rate,
                            positive_reply_rate=s.positive_reply_rate,
                        )
                        for s in slices
                    ],
                )
            )
    return rollups


@router.get("/analytics/timing", response_model=TimingReportOut, tags=["crm"])
async def timing_report(
    principal: Principal = Depends(require("research:read")),
    window_days: int = Query(DEFAULT_WINDOW_DAYS, ge=1, le=365),
    campaign_id: uuid.UUID | None = Query(None),
) -> TimingReportOut:
    """Which hours of the recipient's week are worth writing in.

    A working week is forty-five slots and cold reply rates are single-digit
    percentages, so most slots hold too little to judge. `has_enough_to_rank`
    is the honest headline: below it this is an inventory of what was sent, not
    a recommendation about when to send.
    """
    now = dt.datetime.now(dt.UTC)
    async with workspace_session(principal.workspace_id) as session:
        report = await insights.timing_report(
            session, now=now, window_days=window_days, campaign_id=campaign_id
        )
    return TimingReportOut(
        total_sent=report.total_sent,
        slots=[
            TimingSlotOut(
                weekday=o.slot.weekday,
                hour=o.slot.hour,
                label=str(o.slot),
                sent=o.sent,
                replied=o.replied,
                reply_rate=o.reply_rate if o.has_signal else None,
                verdict=report.verdict_for(o).value,
            )
            for o in report.outcomes
        ],
        baseline_reply_rate=report.baseline,
        judged=report.judged,
        min_sends_per_slot=timing.MIN_SENDS_PER_SLOT,
        slots_needed_to_rank=timing.MIN_SLOTS_TO_RANK,
        has_enough_to_rank=report.has_enough_to_rank,
        summary=timing.describe(report),
    )


@router.get(
    "/analytics/variants", response_model=VariantComparisonOut | None, tags=["crm"]
)
async def variant_comparison(
    principal: Principal = Depends(require("research:read")),
    window_days: int = Query(DEFAULT_WINDOW_DAYS, ge=1, le=365),
    campaign_id: uuid.UUID | None = Query(None),
) -> VariantComparisonOut | None:
    """Whether one phrasing actually beat another, or merely differed.

    Returns null when there is nothing to compare -- one variant, or every arm
    below its floor. Null is the honest answer to "which won"; naming a winner
    from two arms of nine sends would be noise with a p-value attached.
    """
    now = dt.datetime.now(dt.UTC)
    async with workspace_session(principal.workspace_id) as session:
        result = await insights.variant_comparison(
            session, now=now, window_days=window_days, campaign_id=campaign_id
        )
    if result is None:
        return None

    def arm(a: experiments.Arm) -> VariantArmOut:
        return VariantArmOut(
            key=a.key,
            sent=a.sent,
            replied=a.replied,
            positive_replies=a.positive_replies,
        )

    return VariantComparisonOut(
        control=arm(result.control),
        challenger=arm(result.challenger),
        verdict=result.verdict.value,
        lift=result.lift,
        p_value=result.p_value,
        winner=result.winner.key if result.winner else None,
        summary=experiments.describe(result),
    )


@router.get("/analytics/portfolio", response_model=PortfolioOut, tags=["crm"])
async def portfolio_view(
    principal: Principal = Depends(require("research:read")),
    window_days: int = Query(DEFAULT_WINDOW_DAYS, ge=1, le=365),
) -> PortfolioOut:
    """The six markets as one object, busiest first.

    Campaign and lead counts are lifetime; delivery counters are the window. A
    market with three hundred leads and nothing sent this month is the shape
    worth seeing, and one window over both would hide it.
    """
    now = dt.datetime.now(dt.UTC)
    async with workspace_session(principal.workspace_id) as session:
        book = await insights.portfolio_view(session, now=now, window_days=window_days)
    return PortfolioOut(
        window_days=window_days,
        total_sent=book.sent,
        idle_markets=[s.region.value for s in book.idle],
        unconfigured_markets=[r.value for r in insights.unconfigured_markets(book)],
        slices=[
            RegionSliceOut(
                region=s.region.value,
                campaigns=s.campaigns,
                active_campaigns=s.active_campaigns,
                leads=s.leads,
                contacted=s.contacted,
                sent=s.sent,
                bounced=s.bounced,
                replied=s.replied,
                share_of_sending=book.share_of_sending(s.region),
                summary=portfolio_mod.describe(s, book),
            )
            for s in book.slices
        ],
    )


@router.get("/recipient-domains", response_model=RecipientDomainsOut, tags=["crm"])
async def recipient_domains(
    principal: Principal = Depends(require("research:read")),
    limit: int = Query(200, ge=1, le=1000),
) -> RecipientDomainsOut:
    """What Titan's own sending says about each recipient domain, worst first.

    Phase 02 promised that a bad source is visible as a number rather than a
    hunch. The lead-source half of that is the rollup; this is the domain half,
    and it was the one nothing could reach: `domain_health` has decided
    admission since it was written and had no reader outside the pipeline and
    the outbox worker.

    Computed from `messages`, never materialised -- the same query the gate runs,
    so what an operator reads is what the gate acted on rather than a counter
    kept alongside it. See `titan.intelligence.domain_health` on why a table
    here would drift.
    """
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=domain_health.WINDOW_DAYS)
    async with workspace_session(principal.workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT m.to_domain                                    AS domain,
                           count(*) FILTER (WHERE m.sent_at IS NOT NULL)   AS sent,
                           count(*) FILTER (
                               WHERE m.delivered_at IS NOT NULL
                           )                                              AS delivered,
                           count(*) FILTER (
                               WHERE m.bounced_at IS NOT NULL
                           )                                              AS bounced,
                           count(*) FILTER (
                               WHERE m.complained_at IS NOT NULL
                           )                                              AS complained,
                           count(DISTINCT m.lead_id)                      AS leads
                      FROM messages m
                     WHERE m.workspace_id = :workspace
                       AND m.to_domain IS NOT NULL
                       AND m.created_at >= :since
                     GROUP BY m.to_domain
                     LIMIT :limit
                    """
                ),
                {
                    "workspace": principal.workspace_id,
                    "since": since,
                    "limit": limit,
                },
            )
        ).all()

    out: list[RecipientDomainOut] = []
    for row in rows:
        window = domain_health.DomainWindow(
            domain=row.domain,
            sent=int(row.sent),
            delivered=int(row.delivered),
            bounced=int(row.bounced),
            complained=int(row.complained),
        )
        health = domain_health.classify(window)
        out.append(
            RecipientDomainOut(
                domain=row.domain,
                health=health.value,
                sent=window.sent,
                delivered=window.delivered,
                bounced=window.bounced,
                complained=window.complained,
                # Gated on the rate floor, not on `has_history` -- those are
                # different questions. `has_history` is "have we sent here at
                # all"; a *rate* needs MIN_SENDS_FOR_RATE behind it. One send
                # and one bounce has history and no meaningful rate, and
                # publishing 100% for it would rank it worst on the page.
                bounce_rate=(
                    window.bounce_rate
                    if window.sent >= domain_health.MIN_SENDS_FOR_RATE
                    else None
                ),
                has_history=window.has_history,
                leads=int(row.leads),
                explanation=domain_health.explain(window, health),
            )
        )

    # Worst first, and unmeasured domains last rather than first: an unknown
    # domain is not a problem, it is an absence of evidence about one.
    severity = {"degraded": 0, "watch": 1, "healthy": 2, "unknown": 3}
    out.sort(key=lambda d: (severity.get(d.health, 9), -(d.bounce_rate or 0), -d.sent))

    return RecipientDomainsOut(
        window_days=domain_health.WINDOW_DAYS,
        sample_floor=domain_health.MIN_SENDS_FOR_RATE,
        domains=out,
    )


__all__ = ["apply_lead_filters", "enrich_leads", "router"]
