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
    email: str
    workspace_slug: str


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


__all__ = [
    "ApprovalDecisionRequest",
    "ApprovalOut",
    "CampaignCreate",
    "CampaignOut",
    "CampaignPolicyOut",
    "CampaignPolicyUpdate",
    "DraftOut",
    "ErrorBody",
    "EvidenceOut",
    "FindingOut",
    "LeadOut",
    "LoginRequest",
    "MessageOut",
    "Page",
    "ResearchStartRequest",
    "ScoreOut",
    "SendingAuthorizationRequest",
    "SuppressionCreate",
    "SuppressionOut",
    "TokenResponse",
    "UsageOut",
    "WorkflowRunOut",
    "WorkspaceOut",
]
