"""Campaigns, their persisted policy, and industry playbooks."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from titan.config import OperatingMode
from titan.db.base import (
    Base,
    TimestampMixin,
    VersionedMixin,
    WorkspaceScoped,
    pg_enum,
    uuid_pk,
)
from titan.db.enums import CampaignStatus, Industry


class IndustryPlaybook(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """Research priors for an industry.

    A playbook tells the research engine what to *look at*. It never tells it
    what to conclude -- findings come from evidence only. `research_priorities`
    seeds the crawl checklist; `offer_catalogue` constrains which solutions may
    be proposed, so the message generator cannot invent a service.
    """

    __tablename__ = "industry_playbooks"
    __extra_table_args__ = (UniqueConstraint("workspace_id", "industry", "name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    industry: Mapped[Industry] = mapped_column(
        pg_enum(Industry, "industry"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_builtin: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: list[{key, label, category, checks: [...]}] -- what to inspect.
    research_priorities: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    #: list[{key, label, delivers, requires_finding_types: [...]}] -- an offer is
    #: only selectable when at least one required finding type was evidenced.
    offer_catalogue: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    #: Per-category scoring weight overrides for this industry.
    scoring_weights: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    #: Claims that must never be made for this industry (e.g. medical outcomes).
    prohibited_claims: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )


class Campaign(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    __tablename__ = "campaigns"
    __extra_table_args__ = (UniqueConstraint("workspace_id", "slug"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        pg_enum(CampaignStatus, "campaign_status"),
        default=CampaignStatus.DRAFT,
        nullable=False,
        index=True,
    )
    industry: Mapped[Industry] = mapped_column(
        pg_enum(Industry, "industry"),
        default=Industry.GENERAL,
        nullable=False,
    )
    playbook_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("industry_playbooks.id", ondelete="SET NULL")
    )
    sender_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sender_identities.id", ondelete="SET NULL")
    )

    #: Targeting
    target_business_type: Mapped[str | None] = mapped_column(String(200))
    target_geography: Mapped[str | None] = mapped_column(String(200))
    target_country_code: Mapped[str | None] = mapped_column(String(2))
    offer_summary: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    policy: Mapped[CampaignPolicy] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )


class CampaignPolicy(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """The persisted, authoritative policy for a campaign.

    Invariant 18: a workflow-start request may *reference* a campaign but may
    never supply or override these values. The workflow reads this row at
    execution time, and the outbox worker re-reads it immediately before
    delivery -- so pausing a campaign stops mail already queued.
    """

    __tablename__ = "campaign_policies"
    __extra_table_args__ = (
        UniqueConstraint("campaign_id"),
        CheckConstraint(
            "min_lead_score >= 0 AND min_lead_score <= 100",
            name="min_lead_score_range",
        ),
        CheckConstraint("max_followups >= 0", name="max_followups_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Campaign-level ceiling; effective mode is the *minimum* of process,
    #: workspace, and campaign modes.
    operating_mode: Mapped[OperatingMode] = mapped_column(
        pg_enum(OperatingMode, "operating_mode"),
        default=OperatingMode.RESEARCH_ONLY,
        nullable=False,
    )
    #: Fourth delivery gate. Independent of workspace.sending_authorized.
    sending_authorized: Mapped[bool] = mapped_column(default=False, nullable=False)

    min_lead_score: Mapped[int] = mapped_column(default=70, nullable=False)
    require_verified_email: Mapped[bool] = mapped_column(default=True, nullable=False)
    require_evidence_backed_claims: Mapped[bool] = mapped_column(
        default=True, nullable=False
    )
    min_evidence_per_message: Mapped[int] = mapped_column(default=1, nullable=False)
    approval_ttl_hours: Mapped[int] = mapped_column(default=168, nullable=False)

    daily_send_limit: Mapped[int] = mapped_column(default=25, nullable=False)
    recipient_domain_daily_limit: Mapped[int] = mapped_column(default=2, nullable=False)
    min_spacing_seconds: Mapped[int] = mapped_column(default=90, nullable=False)
    max_followups: Mapped[int] = mapped_column(default=3, nullable=False)
    #: Day offsets for the sequence, e.g. [0, 3, 7, 14].
    followup_schedule_days: Mapped[list[str]] = mapped_column(
        JSONB, default=lambda: [0, 3, 7, 14], nullable=False
    )
    respect_quiet_hours: Mapped[bool] = mapped_column(default=True, nullable=False)

    research_budget_usd: Mapped[float] = mapped_column(default=10.0, nullable=False)
    per_lead_budget_usd: Mapped[float] = mapped_column(default=0.50, nullable=False)
    allow_premium_model: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: Contact sources accepted for this campaign, as a list of ContactSource
    #: values. Defaults exclude pattern guesses; the API rejects adding them.
    allowed_contact_sources: Mapped[list[str]] = mapped_column(
        JSONB,
        default=lambda: [
            "first_party_website",
            "public_directory",
            "google_places",
            "public_role_address",
            "manual_entry",
        ],
        nullable=False,
    )

    campaign: Mapped[Campaign] = relationship(back_populates="policy")

    def blocking_errors(self) -> list[str]:
        """Campaign-level reasons delivery is not permitted."""
        errors: list[str] = []
        if not self.sending_authorized:
            errors.append("campaign sending is not authorized")
        if (
            self.campaign is not None
            and self.campaign.status is not CampaignStatus.ACTIVE
        ):
            errors.append(f"campaign status is {self.campaign.status.value}, not active")
        if "pattern_guess" in (self.allowed_contact_sources or []):
            errors.append(
                "campaign policy lists pattern_guess as an allowed contact source"
            )
        return errors
