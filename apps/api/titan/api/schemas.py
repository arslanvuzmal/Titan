"""API request and response models.

Every response is an explicit model rather than a serialised ORM row. That is
what keeps invariant 19 true by construction: a field has to be *named here* to
reach a client, so a credential column cannot leak by being added to a table.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorBody(BaseModel):
    """Consistent error shape across every route."""

    error: str
    detail: str | None = None
    request_id: str | None = None


class LoginRequest(BaseModel):
    username: str
    passcode: str
    #: Only consulted when the account belongs to more than one workspace. The
    #: sign-in form does not show it, so the common case stays two fields; it
    #: exists so a multi-workspace operator has a way through that is not
    #: "the server picks one for you".
    workspace: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - the OAuth scheme name, not a secret
    expires_in: int
    workspace_id: uuid.UUID
    role: str


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    operating_mode: str
    sending_authorized: bool
    daily_send_limit: int


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9-]+$")
    industry: str = "general"
    target_business_type: str | None = None
    target_geography: str | None = None
    target_country_code: str | None = Field(default=None, max_length=2)
    offer_summary: str | None = None
    min_lead_score: int = Field(default=70, ge=0, le=100)

    #: The market this campaign is aimed at. Decides the working week, the
    #: sending window, and -- for every lead whose own timezone Places never
    #: resolved -- which clock the window is measured against. Left unspecified,
    #: the campaign has no clock and defers those leads rather than guessing at
    #: their local hour, so this is the single most consequential field here.
    region: str | None = None
    #: The timezone band inside that market, where the market spans several.
    #: A US campaign selling to California should say so; without it the market
    #: default is Eastern and three hours early.
    sub_region: str | None = None


class MeetingBookedRequest(BaseModel):
    """An operator recording that a lead booked a meeting.

    Deliberately thin. The only thing worth capturing beyond the fact itself is
    a note, because the fact is what the optimiser reads and everything else
    would be a second place for the truth to live.
    """

    note: str | None = Field(default=None, max_length=500)


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: str
    industry: str
    created_at: dt.datetime


class CampaignPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    operating_mode: str
    sending_authorized: bool
    min_lead_score: int
    require_verified_email: bool
    require_evidence_backed_claims: bool
    daily_send_limit: int
    recipient_domain_daily_limit: int
    max_followups: int
    allowed_contact_sources: list[str]
    #: The sending window, in the recipient's local time. Surfaced because it is
    #: derived from the campaign's market at creation rather than typed in, and
    #: a derived value nobody can see is one nobody can check.
    send_window_start_hour: int
    send_window_end_hour: int
    send_days: list[int]


class CampaignPolicyUpdate(BaseModel):
    """Only the fields an operator may change.

    ``operating_mode`` is deliberately absent from the widening direction: the
    route refuses any value more permissive than the workspace ceiling.
    """

    operating_mode: str | None = None
    min_lead_score: int | None = Field(default=None, ge=0, le=100)
    daily_send_limit: int | None = Field(default=None, ge=0, le=10_000)
    recipient_domain_daily_limit: int | None = Field(default=None, ge=0, le=1000)
    max_followups: int | None = Field(default=None, ge=0, le=10)


class SendingAuthorizationRequest(BaseModel):
    """Enabling delivery is a deliberate, audited act, not a PATCH field."""

    authorized: bool
    acknowledgement: str = Field(
        description=(
            "Must be exactly 'I authorize production sending for this campaign'. "
            "A typed acknowledgement makes an accidental enablement impossible."
        )
    )


class OrganizationSummary(BaseModel):
    """Just enough of a business to recognise it in a list.

    The CRM lists leads, but a human recognises *businesses*. A row showing
    only ``organization_id`` is unusable, so the list endpoint joins this in.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    canonical_domain: str | None = None
    website_url: str | None = None
    industry: str = "general"
    phone_e164: str | None = None
    rating: float | None = None
    review_count: int | None = None
    business_status: str | None = None
    locality: str | None = None
    region: str | None = None
    country_code: str | None = None


class OrganizationLocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    formatted_address: str | None
    locality: str | None
    region: str | None
    postal_code: str | None
    country_code: str | None
    timezone: str | None
    is_primary: bool


class OrganizationOut(OrganizationSummary):
    """Full business record, including where each field came from."""

    legal_name: str | None = None
    normalized_name: str = ""
    google_place_id: str | None = None
    employee_estimate: int | None = None
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    locations: list[OrganizationLocationOut] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    created_at: dt.datetime | None = None


class ContactChannelOut(BaseModel):
    """One reachable address, with the provenance that decides eligibility.

    ``eligible_for_outreach`` is computed rather than stored: a pattern-guessed
    address is displayed (so an operator can see Titan found it) but is never
    contactable, and the UI must show that distinction rather than imply the
    address is usable.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_type: str
    value: str
    normalized_value: str
    value_domain: str | None
    source: str
    source_url: str | None
    discovered_at: dt.datetime
    verification_status: str
    confidence: float
    consent_basis: str | None
    is_active: bool
    eligible_for_outreach: bool = False
    ineligibility_reason: str | None = None
    suppressed: bool = False


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    full_name: str | None
    role_title: str | None
    is_decision_maker: bool
    is_generic_role: bool
    notes: str | None
    channels: list[ContactChannelOut] = Field(default_factory=list)


class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    campaign_id: uuid.UUID
    organization_id: uuid.UUID
    status: str
    latest_score: int | None
    replied_at: dt.datetime | None
    last_contacted_at: dt.datetime | None
    followups_sent: int

    # --- CRM enrichment. Optional so that a plain `model_validate(lead)`
    # --- still produces a valid (if sparse) record.
    status_reason: str | None = None
    next_action_at: dt.datetime | None = None
    created_at: dt.datetime | None = None
    organization: OrganizationSummary | None = None
    campaign_name: str | None = None
    finding_count: int = 0
    draft_count: int = 0
    message_count: int = 0
    evidence_count: int = 0
    #: True when at least one contact channel could lawfully be contacted.
    #: Distinct from "an address exists" -- see ContactChannelOut.
    has_eligible_contact: bool = False


class TimelineEventOut(BaseModel):
    """One dated thing that happened to a lead.

    Assembled from the record tables rather than a separate event log, so the
    timeline cannot drift from what actually happened.
    """

    at: dt.datetime
    kind: str
    title: str
    detail: str | None = None
    reference_id: uuid.UUID | None = None
    severity: str | None = None


class CrmStatsOut(BaseModel):
    """Counters for the CRM overview. Every number is a live COUNT."""

    leads_total: int
    leads_by_status: dict[str, int]
    leads_by_band: dict[str, int]
    campaigns_total: int
    organizations_total: int
    contacts_total: int
    eligible_contacts: int
    findings_total: int
    evidence_total: int
    drafts_by_status: dict[str, int]
    messages_by_state: dict[str, int]
    suppressions_total: int
    replied_total: int
    #: Opportunities the owner sells a fix for. Gaps -- problems evidenced but
    #: not covered by any offer -- are counted separately rather than folded in,
    #: because a total that mixes them reads as a pipeline and is not one.
    opportunities_deliverable: int
    opportunities_unserved: int
    #: Calls somebody asked for, and how many still have no time on them. Every
    #: meeting starts unscheduled: Titan does not parse a time out of a reply.
    meetings_total: int
    meetings_unscheduled: int
    #: Reflects the process kill switch and the workspace ceiling, so the CRM
    #: can state plainly whether anything could actually be sent right now.
    sending_authorized: bool
    operating_mode: str


class FindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category: str
    issue_type: str
    title: str
    page_url: str | None
    selector: str | None
    observed_value: str | None
    expected_behavior: str | None
    severity: str
    confidence: float
    business_impact: str | None
    recommended_solution: str | None
    verification_method: str
    contradicted: bool


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    finding_id: uuid.UUID
    excerpt: str | None
    source_url: str | None
    captured_at: dt.datetime


class ScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total: int
    band: str
    components: dict[str, Any]
    reasons: list[str]
    policy_version: str
    threshold_applied: int
    passed_threshold: bool
    created_at: dt.datetime


class DraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID
    status: str
    subject: str
    body_text: str
    claim_map: list[dict[str, Any]]
    validation_passed: bool
    validation_report: dict[str, Any]
    version: int
    created_at: dt.datetime


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(pattern=r"^(approved|rejected|changes_requested)$")
    #: The version the reviewer actually looked at. A mismatch is rejected, so
    #: "approve, then edit, then send" cannot bypass review.
    draft_version: int
    reason: str | None = None


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    draft_id: uuid.UUID
    draft_version: int
    decision: str
    decided_at: dt.datetime
    reason: str | None


class SuppressionCreate(BaseModel):
    value: str = Field(min_length=3, max_length=320)
    scope: str = Field(default="email", pattern=r"^(email|domain)$")
    reason: str = "manual"
    note: str | None = None


class SuppressionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scope: str
    normalized_value: str
    reason: str
    source: str
    suppressed_at: dt.datetime
    expires_at: dt.datetime | None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID
    to_email_normalized: str
    subject: str
    state: str
    sent_at: dt.datetime | None
    delivered_at: dt.datetime | None
    bounced_at: dt.datetime | None
    complained_at: dt.datetime | None


class ResearchStartRequest(BaseModel):
    """Note what is absent: no policy, no limits, no mode.

    Invariant 18. The workflow reads those from the database. Accepting them
    here would let a request widen what Titan is allowed to do.
    """

    lead_id: uuid.UUID
    seed_url: str | None = None


class WorkflowRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: str
    workflow_type: str
    status: str
    started_at: dt.datetime
    closed_at: dt.datetime | None
    failure_reason: str | None


class UsageOut(BaseModel):
    window_date: dt.date
    quotas: list[dict[str, Any]]
    spend_usd: float
    model_calls: int


class OutcomeSliceOut(BaseModel):
    """One group's delivery record. Rates are null below the sample floor.

    Null is deliberate and is not the same as zero. A slice with four sends and
    one bounce has no bounce rate -- publishing 25% invites acting on it, and
    any ranking built from such numbers sorts mostly by who has the smallest
    sample. The UI should render "not enough data yet", never "0%".
    """

    key: str
    label: str
    sent: int
    delivered: int
    bounced: int
    complained: int
    replied: int
    positive_replies: int
    meetings: int
    has_signal: bool
    bounce_rate: float | None = None
    reply_rate: float | None = None
    positive_reply_rate: float | None = None


class OutcomeRollupOut(BaseModel):
    """Delivery outcomes grouped one way, plus what the grouping means."""

    dimension: str
    window_days: int
    #: Below this many sends a slice carries no rate at all.
    sample_floor: int
    slices: list[OutcomeSliceOut]


class TimingSlotOut(BaseModel):
    """One hour of one weekday, in the recipient's local time."""

    weekday: int
    hour: int
    label: str
    sent: int
    replied: int
    reply_rate: float | None = None
    verdict: str


class TimingReportOut(BaseModel):
    """What the week looks like, and how much of it is actually known.

    `judged` is the denominator of every claim here. When it is below
    `slots_needed_to_rank` the report is an inventory, not a ranking, and
    `has_enough_to_rank` says so rather than leaving a caller to infer it.
    """

    total_sent: int
    slots: list[TimingSlotOut]
    baseline_reply_rate: float
    judged: int
    min_sends_per_slot: int
    slots_needed_to_rank: int
    has_enough_to_rank: bool
    summary: str


class VariantArmOut(BaseModel):
    key: str
    sent: int
    replied: int
    positive_replies: int


class VariantComparisonOut(BaseModel):
    """Whether one phrasing beat another, or merely differed.

    `p_value` is null when the arms were too small to test -- not 1.0, which
    would read as "tested and found identical" and is a different claim.
    """

    control: VariantArmOut
    challenger: VariantArmOut
    verdict: str
    lift: float | None = None
    p_value: float | None = None
    winner: str | None = None
    summary: str


class RegionSliceOut(BaseModel):
    region: str
    campaigns: int
    active_campaigns: int
    leads: int
    contacted: int
    sent: int
    bounced: int
    replied: int
    share_of_sending: float
    summary: str


class PortfolioOut(BaseModel):
    """The six markets as one object, busiest first."""

    window_days: int
    total_sent: int
    slices: list[RegionSliceOut]
    #: Markets with an active campaign that sent nothing this window. The point
    #: of the whole view.
    idle_markets: list[str] = []
    #: Markets with no campaign at all. Listed rather than given a row of zeros:
    #: "0% bounced" for a market that has never sent would read as the healthiest
    #: line in the table.
    unconfigured_markets: list[str] = []


__all__ = [
    "ApprovalDecisionRequest",
    "ApprovalOut",
    "CampaignCreate",
    "CampaignOut",
    "CampaignPolicyOut",
    "CampaignPolicyUpdate",
    "ContactChannelOut",
    "ContactOut",
    "CrmStatsOut",
    "DraftOut",
    "ErrorBody",
    "EvidenceOut",
    "FindingOut",
    "LeadOut",
    "LoginRequest",
    "MessageOut",
    "OrganizationLocationOut",
    "OrganizationOut",
    "OrganizationSummary",
    "OutcomeRollupOut",
    "OutcomeSliceOut",
    "Page",
    "PortfolioOut",
    "RegionSliceOut",
    "ResearchStartRequest",
    "ScoreOut",
    "SendingAuthorizationRequest",
    "SuppressionCreate",
    "SuppressionOut",
    "TimelineEventOut",
    "TimingReportOut",
    "TimingSlotOut",
    "TokenResponse",
    "UsageOut",
    "VariantArmOut",
    "VariantComparisonOut",
    "WorkflowRunOut",
    "WorkspaceOut",
]


class OpportunityOut(BaseModel):
    """A commercial opportunity derived from evidenced findings.

    ``estimated_value_usd`` is the offer's catalogue price, not a forecast and
    not a probability-weighted figure. It is null for an unserved gap, because
    attaching a number to work the owner does not do would put revenue in a
    total nobody can deliver.
    """

    id: uuid.UUID
    lead_id: uuid.UUID
    organization_name: str | None = None
    offer_key: str
    title: str
    rationale: str | None = None
    estimated_value_usd: float | None = None
    priority: int
    #: False means no offer in the playbook covers this. Recorded so the gap is
    #: visible; it must never be pitched.
    deliverable: bool
    supporting_finding_count: int = 0
    created_at: dt.datetime

    model_config = ConfigDict(from_attributes=True)


class MeetingOut(BaseModel):
    """A conversation somebody asked for.

    ``scheduled_at`` is null on every meeting Titan opens: a reply naming a time
    is not parsed, because a wrong time does not read as a parsing failure, it
    reads as a confirmed appointment. A person sets it.
    """

    id: uuid.UUID
    lead_id: uuid.UUID
    organization_name: str | None = None
    status: str
    scheduled_at: dt.datetime | None = None
    duration_minutes: int | None = None
    location_or_link: str | None = None
    notes: str | None = None
    created_at: dt.datetime

    model_config = ConfigDict(from_attributes=True)
