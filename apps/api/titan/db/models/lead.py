"""Lead-side domain: sources, organizations, contacts, leads, and scores."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from titan.db.base import (
    Base,
    ImmutableMixin,
    TimestampMixin,
    VersionedMixin,
    WorkspaceScoped,
    pg_enum,
    uuid_pk,
)
from titan.db.enums import (
    ContactSource,
    Industry,
    LeadStatus,
    VerificationStatus,
)


class LeadSource(Base, WorkspaceScoped, TimestampMixin):
    """Provenance of a batch of discovered leads.

    Kept distinct from the organization record so that re-discovering the same
    business through a second source adds provenance rather than overwriting the
    canonical identity (mission section 5.2).
    """

    __tablename__ = "lead_sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    #: "google_places" | "csv_import" | "manual" | "crm_import" | "referral"
    kind: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    query_parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    records_returned: Mapped[int] = mapped_column(default=0, nullable=False)
    records_deduplicated: Mapped[int] = mapped_column(default=0, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(default=0.0, nullable=False)
    #: Licence/usage constraints attached by the provider adapter, so downstream
    #: code knows what it may store and for how long (Places ToS, section 6.1).
    usage_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )


class Organization(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """A real-world business. The canonical identity for deduplication."""

    __tablename__ = "organizations"
    __extra_table_args__ = (
        # Two independent partial-unique identities. Either alone is sufficient
        # to recognise a repeat discovery; neither may be silently overwritten.
        # Partial (WHERE ... IS NOT NULL) so that many organizations may coexist
        # without a place ID or without a known domain.
        Index(
            "uq_organizations_ws_place_id",
            "workspace_id",
            "google_place_id",
            unique=True,
            postgresql_where=text("google_place_id IS NOT NULL"),
        ),
        Index(
            "uq_organizations_ws_canonical_domain",
            "workspace_id",
            "canonical_domain",
            unique=True,
            postgresql_where=text("canonical_domain IS NOT NULL"),
        ),
        Index("ix_organizations_ws_name_norm", "workspace_id", "normalized_name"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    legal_name: Mapped[str | None] = mapped_column(String(300))
    display_name: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Casefolded, punctuation-stripped, legal-suffix-stripped name used as a
    #: weak dedupe signal (never sufficient alone).
    normalized_name: Mapped[str] = mapped_column(String(300), nullable=False)
    industry: Mapped[Industry] = mapped_column(
        pg_enum(Industry, "industry"),
        default=Industry.GENERAL,
        nullable=False,
    )
    canonical_domain: Mapped[str | None] = mapped_column(String(253), index=True)
    google_place_id: Mapped[str | None] = mapped_column(String(255), index=True)
    website_url: Mapped[str | None] = mapped_column(Text)
    phone_e164: Mapped[str | None] = mapped_column(String(20))
    rating: Mapped[float | None] = mapped_column(Float)
    review_count: Mapped[int | None] = mapped_column(Integer)
    business_status: Mapped[str | None] = mapped_column(String(40))
    employee_estimate: Mapped[int | None] = mapped_column(Integer)

    #: list[{source, source_id, retrieved_at, fields: [...]}] -- append-only.
    provenance: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )

    locations: Mapped[list[OrganizationLocation]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    domains: Mapped[list[OrganizationDomain]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    contacts: Mapped[list[Contact]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class OrganizationLocation(Base, WorkspaceScoped, TimestampMixin):
    __tablename__ = "organization_locations"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    formatted_address: Mapped[str | None] = mapped_column(Text)
    locality: Mapped[str | None] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(200))
    postal_code: Mapped[str | None] = mapped_column(String(30))
    country_code: Mapped[str | None] = mapped_column(String(2), index=True)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    #: IANA zone, used for quiet-hours enforcement at send time.
    timezone: Mapped[str | None] = mapped_column(String(64))
    is_primary: Mapped[bool] = mapped_column(default=True, nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="locations")


class OrganizationDomain(Base, WorkspaceScoped, TimestampMixin):
    __tablename__ = "organization_domains"
    __extra_table_args__ = (UniqueConstraint("workspace_id", "domain"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    is_primary: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: Whether the domain was observed to actually serve the business site.
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    organization: Mapped[Organization] = relationship(back_populates="domains")


class Contact(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    full_name: Mapped[str | None] = mapped_column(String(200))
    role_title: Mapped[str | None] = mapped_column(String(200))
    #: Heuristic seniority for decision-maker scoring; never used for guessing.
    is_decision_maker: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: True for info@/contact@/hello@ style addresses.
    is_generic_role: Mapped[bool] = mapped_column(default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    organization: Mapped[Organization] = relationship(back_populates="contacts")
    channels: Mapped[list[ContactChannel]] = relationship(
        back_populates="contact", cascade="all, delete-orphan", lazy="selectin"
    )


class ContactChannel(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """A single reachable address with full provenance.

    Invariant 6 lives here: `source` records *how* the address was obtained, and
    a PATTERN_GUESS address is stored (so the system remembers not to guess it
    again) but can never satisfy the eligibility check.
    """

    __tablename__ = "contact_channels"
    __extra_table_args__ = (
        UniqueConstraint(
            "workspace_id", "contact_id", "channel_type", "normalized_value"
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="confidence_unit_interval"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: "email" | "phone" | "form" | "linkedin"
    channel_type: Mapped[str] = mapped_column(String(20), nullable=False)
    value: Mapped[str] = mapped_column(String(400), nullable=False)
    #: Lowercased/trimmed form used for suppression matching and uniqueness.
    normalized_value: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    #: Registrable domain of an email address; drives per-domain quotas.
    value_domain: Mapped[str | None] = mapped_column(String(253), index=True)

    source: Mapped[ContactSource] = mapped_column(
        pg_enum(ContactSource, "contact_source"), nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    verification_status: Mapped[VerificationStatus] = mapped_column(
        pg_enum(VerificationStatus, "verification_status"),
        default=VerificationStatus.UNVERIFIED,
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Recorded consent or prior relationship, where one exists.
    consent_basis: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    contact: Mapped[Contact] = relationship(back_populates="channels")
    verifications: Mapped[list[ContactVerification]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )


class ContactVerification(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """Immutable record of one verification attempt.

    Deliberately append-only: a later "risky" result must not erase the earlier
    evidence that an address was once confirmed, and vice versa.
    """

    __tablename__ = "contact_verifications"

    id: Mapped[uuid.UUID] = uuid_pk()
    channel_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("contact_channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    result: Mapped[VerificationStatus] = mapped_column(
        pg_enum(VerificationStatus, "verification_status"),
        nullable=False,
    )
    #: Note: MX presence proves a domain accepts mail, NOT that a mailbox exists.
    #: Stored for diagnostics only; it never upgrades verification_status.
    mx_present: Mapped[bool | None] = mapped_column()
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    verified_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    channel: Mapped[ContactChannel] = relationship(back_populates="verifications")


class Lead(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """An organization considered within the context of one campaign."""

    __tablename__ = "leads"
    __extra_table_args__ = (
        UniqueConstraint("workspace_id", "campaign_id", "organization_id"),
        Index("ix_leads_ws_status", "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lead_source_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("lead_sources.id", ondelete="SET NULL")
    )
    primary_contact_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("contact_channels.id", ondelete="SET NULL")
    )

    status: Mapped[LeadStatus] = mapped_column(
        pg_enum(LeadStatus, "lead_status"),
        default=LeadStatus.DISCOVERED,
        nullable=False,
    )
    status_reason: Mapped[str | None] = mapped_column(Text)
    latest_score: Mapped[int | None] = mapped_column(Integer, index=True)
    #: Set the moment any human reply is observed. Checked by the follow-up
    #: engine and the outbox worker (invariant 15).
    replied_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_contacted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    followups_sent: Mapped[int] = mapped_column(default=0, nullable=False)
    next_action_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )

    def outreach_blocking_errors(self) -> list[str]:
        from titan.db.enums import TERMINAL_LEAD_STATUSES

        errors: list[str] = []
        if self.status in TERMINAL_LEAD_STATUSES:
            errors.append(f"lead status {self.status.value} is terminal for outreach")
        if self.replied_at is not None:
            errors.append("lead has already replied")
        return errors


class LeadScore(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """One immutable scoring result.

    Scores are append-only so that the score which justified a send can always
    be reconstructed, even after the scoring policy changes.
    """

    __tablename__ = "lead_scores"
    __extra_table_args__ = (
        CheckConstraint("total >= 0 AND total <= 100", name="total_range"),
        Index("ix_lead_scores_lead_created", "lead_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    band: Mapped[str] = mapped_column(String(20), nullable=False)
    #: {dimension: {raw, weight, weighted, reason, evidence_ids: [...]}}
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    #: Version of the deterministic scoring policy that produced this row.
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    threshold_applied: Mapped[int] = mapped_column(Integer, nullable=False)
    passed_threshold: Mapped[bool] = mapped_column(nullable=False)
    #: Model contribution, if any, recorded separately so a human can see
    #: exactly how much of the score was model-influenced.
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("model_runs.id", ondelete="SET NULL")
    )
