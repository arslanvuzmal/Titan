"""Research, evidence, findings, and opportunity tables.

This is the product's differentiator: nothing may be claimed in an email unless
it traces back through a finding to an immutable evidence row captured by the
browser worker.
"""

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
from titan.db.enums import FindingCategory, Severity, VerificationMethod


class ResearchRun(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """One end-to-end research pass over a lead."""

    __tablename__ = "research_runs"
    __extra_table_args__ = (
        # Idempotency: a Temporal activity retry reuses the same key and finds
        # the existing run instead of starting a second crawl (invariant 11).
        UniqueConstraint("workspace_id", "idempotency_key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String(255), index=True)

    status: Mapped[str] = mapped_column(String(30), default="running", nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    pages_crawled: Mapped[int] = mapped_column(default=0, nullable=False)
    findings_count: Mapped[int] = mapped_column(default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(default=0.0, nullable=False)
    #: Playbook that guided this run, recorded so results stay interpretable
    #: after the playbook is edited.
    playbook_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )

    crawl_runs: Mapped[list[CrawlRun]] = relationship(
        back_populates="research_run", cascade="all, delete-orphan"
    )


class CrawlRun(Base, WorkspaceScoped, TimestampMixin):
    """One bounded crawl executed by the isolated browser worker."""

    __tablename__ = "crawl_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    research_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("research_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seed_url: Mapped[str] = mapped_column(Text, nullable=False)
    #: The URL actually reached after redirects, revalidated at each hop.
    final_url: Mapped[str | None] = mapped_column(Text)
    redirect_chain: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="running", nullable=False)
    #: Populated when the URL guard refused the target; the refusal itself is
    #: useful evidence (e.g. a site that redirects to a private address).
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    robots_allowed: Mapped[bool | None] = mapped_column()
    pages_fetched: Mapped[int] = mapped_column(default=0, nullable=False)
    bytes_fetched: Mapped[int] = mapped_column(default=0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    worker_version: Mapped[str | None] = mapped_column(String(40))

    research_run: Mapped[ResearchRun] = relationship(back_populates="crawl_runs")
    pages: Mapped[list[Page]] = relationship(
        back_populates="crawl_run", cascade="all, delete-orphan"
    )


class Page(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """A single observed page. Immutable: it is the substrate for claims."""

    __tablename__ = "pages"
    __extra_table_args__ = (
        UniqueConstraint("crawl_run_id", "url_fingerprint"),
        Index("ix_pages_ws_domain", "workspace_id", "domain"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    crawl_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("crawl_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    #: sha256 of the normalized URL; stable across retries.
    url_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    depth: Mapped[int] = mapped_column(default=0, nullable=False)

    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str | None] = mapped_column(Text)
    meta_description: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    robots_meta: Mapped[str | None] = mapped_column(String(200))
    lang: Mapped[str | None] = mapped_column(String(20))

    #: Structured observations. Kept as JSONB rather than 30 columns because the
    #: shape is versioned by the browser worker and validated on ingest by a
    #: Pydantic contract (titan.contracts.evidence).
    observations: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    #: sha256 of the *normalized content*, excluding volatile fields such as
    #: capture time, worker id, and session id (mission section 7.4).
    content_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    #: Sanitised, truncated visible text. Always treated as untrusted input and
    #: never interpolated into a model's instruction channel.
    text_excerpt: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    crawl_run: Mapped[CrawlRun] = relationship(back_populates="pages")


class BrowserArtifact(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """A stored capture: screenshot, Lighthouse report, axe report, HAR subset."""

    __tablename__ = "browser_artifacts"
    __extra_table_args__ = (
        # Same measured content on a retry must not create a second artifact.
        UniqueConstraint("workspace_id", "content_fingerprint", "kind"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("pages.id", ondelete="CASCADE"), index=True
    )
    crawl_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("crawl_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: "screenshot_mobile" | "screenshot_desktop" | "lighthouse" | "axe"
    #: | "console" | "network_failures" | "headers"
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    media_type: Mapped[str] = mapped_column(String(80), nullable=False)
    #: Storage key. Validated against a path-traversal guard on read.
    storage_key: Mapped[str | None] = mapped_column(Text)
    #: Small structured artifacts (axe violations, metrics) live inline.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    byte_size: Mapped[int | None] = mapped_column(Integer)
    #: Excludes timestamps/paths/session ids by construction.
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: Retention: rows past this instant are eligible for the purge job.
    expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )


class AuditFinding(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """A specific, evidenced problem or opportunity on a lead's web presence."""

    __tablename__ = "audit_findings"
    __extra_table_args__ = (
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="confidence_unit_interval"
        ),
        # Deduplicates the same issue across research retries.
        UniqueConstraint("research_run_id", "finding_fingerprint"),
        Index("ix_audit_findings_ws_lead", "workspace_id", "lead_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    research_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("research_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("pages.id", ondelete="SET NULL")
    )

    category: Mapped[FindingCategory] = mapped_column(
        pg_enum(FindingCategory, "finding_category"), nullable=False
    )
    #: Stable machine identifier, e.g. "broken_primary_cta". Drives offer
    #: selection and the fixture-corpus assertions.
    issue_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    page_url: Mapped[str | None] = mapped_column(Text)
    selector: Mapped[str | None] = mapped_column(Text)
    observed_value: Mapped[str | None] = mapped_column(Text)
    expected_behavior: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[Severity] = mapped_column(
        pg_enum(Severity, "severity"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    business_impact: Mapped[str | None] = mapped_column(Text)
    recommended_solution: Mapped[str | None] = mapped_column(Text)
    #: "small" | "medium" | "large"
    estimated_effort: Mapped[str | None] = mapped_column(String(20))
    verification_method: Mapped[VerificationMethod] = mapped_column(
        pg_enum(VerificationMethod, "verification_method"),
        nullable=False,
    )
    #: sha256 over (issue_type, normalized page url, selector, observed value).
    finding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Set by the verification model pass; a contradicted finding is not
    #: pitchable regardless of confidence.
    contradicted: Mapped[bool] = mapped_column(default=False, nullable=False)
    contradiction_reason: Mapped[str | None] = mapped_column(Text)

    evidence_links: Mapped[list[FindingEvidence]] = relationship(
        back_populates="finding", cascade="all, delete-orphan", lazy="selectin"
    )

    def is_pitchable(self, min_confidence: float = 0.7) -> bool:
        """Whether this finding may be referenced in a recipient-facing message.

        Requires: measured (not model-inferred) verification, no contradiction,
        sufficient confidence, and at least one evidence link.
        """
        from titan.db.enums import PITCHABLE_METHODS

        return (
            not self.contradicted
            and self.verification_method in PITCHABLE_METHODS
            and self.confidence >= min_confidence
            and len(self.evidence_links) > 0
        )


class FindingEvidence(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """Immutable link from a finding to the artifact/page that proves it."""

    __tablename__ = "finding_evidence"
    __extra_table_args__ = (
        UniqueConstraint("finding_id", "artifact_id", "page_id", "excerpt_fingerprint"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("audit_findings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("browser_artifacts.id", ondelete="RESTRICT")
    )
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("pages.id", ondelete="RESTRICT")
    )
    #: The exact observed fragment, e.g. the CTA href or the axe violation node.
    excerpt: Mapped[str | None] = mapped_column(Text)
    excerpt_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    finding: Mapped[AuditFinding] = relationship(back_populates="evidence_links")


class BusinessOpportunity(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """A commercial opportunity derived from one or more findings."""

    __tablename__ = "business_opportunities"

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    research_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("research_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Key from the playbook's offer_catalogue. Constrains what may be offered.
    offer_key: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    #: Finding UUIDs (as strings) supporting this opportunity.
    supporting_finding_ids: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    estimated_value_usd: Mapped[float | None] = mapped_column(Float)
    priority: Mapped[int] = mapped_column(default=0, nullable=False)
    #: False when the owner cannot credibly deliver this offer; such
    #: opportunities are recorded but never pitched.
    deliverable: Mapped[bool] = mapped_column(default=True, nullable=False)


class SolutionRecommendation(Base, WorkspaceScoped, TimestampMixin):
    """Internal, operator-facing recommendation. Not recipient-facing copy."""

    __tablename__ = "solution_recommendations"

    id: Mapped[uuid.UUID] = uuid_pk()
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("business_opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    implementation_outline: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    estimated_effort: Mapped[str | None] = mapped_column(String(20))
    prerequisites: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("model_runs.id", ondelete="SET NULL")
    )
