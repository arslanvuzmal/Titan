"""Suppression and quota tables — the two hard stops on delivery."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db.base import (
    Base,
    ImmutableMixin,
    TimestampMixin,
    WorkspaceScoped,
    pg_enum,
    uuid_pk,
)
from titan.db.enums import SuppressionReason


class SuppressionEntry(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """A recipient that must never be contacted again.

    Deliberately **not** a foreign key to contacts. Deleting a contact — for a
    GDPR erasure request, say — must not delete the record that the person
    asked not to be emailed, or the next discovery run would re-add them
    (mission section 24). The entry stores only the minimum needed to recognise
    the address: its normalized form and a hash for constant-time comparison.
    """

    __tablename__ = "suppression_entries"
    __extra_table_args__ = (
        UniqueConstraint("workspace_id", "scope", "normalized_value"),
        Index("ix_suppression_lookup", "workspace_id", "normalized_value"),
        CheckConstraint("scope IN ('email','domain')", name="scope_allowed"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    #: 'email' suppresses one address; 'domain' suppresses an entire domain
    #: (used after a complaint from a shared mailbox, or on legal request).
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[SuppressionReason] = mapped_column(
        pg_enum(SuppressionReason, "suppression_reason"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(60), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(255))
    suppressed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: Null means permanent. Only non-permanent reasons may set this.
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Legal hold prevents purge even when retention would otherwise apply.
    legal_hold: Mapped[bool] = mapped_column(default=False, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    def is_active_at(self, when: dt.datetime) -> bool:
        return self.expires_at is None or self.expires_at > when


class QuotaCounter(Base, WorkspaceScoped, TimestampMixin):
    """Atomic daily counter.

    Reservation is a single statement:

        INSERT INTO quota_counters (...) VALUES (...)
        ON CONFLICT (workspace_id, scope_type, scope_key, window_date)
        DO UPDATE SET used = quota_counters.used + 1
        WHERE quota_counters.used < quota_counters.limit_value
        RETURNING used;

    An empty result means the limit is reached. Because the increment and the
    bound check happen inside one statement under the row lock PostgreSQL takes
    for the ON CONFLICT path, N concurrent workers can never overshoot
    (invariant 14). No application-level locking is involved.
    """

    __tablename__ = "quota_counters"
    __extra_table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "scope_type",
            "scope_key",
            "window_date",
            name="uq_quota_scope_window",
        ),
        CheckConstraint("used >= 0", name="used_non_negative"),
        CheckConstraint("limit_value >= 0", name="limit_non_negative"),
        CheckConstraint(
            "scope_type IN ('workspace','campaign','sender','recipient_domain')",
            name="scope_type_allowed",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    #: UUID string for workspace/campaign/sender scopes; the domain for
    #: recipient_domain scope.
    scope_key: Mapped[str] = mapped_column(String(253), nullable=False)
    #: UTC date. Quiet-hours and spacing are handled separately by the worker.
    window_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    limit_value: Mapped[int] = mapped_column(Integer, nullable=False)


class UsageLedger(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """Append-only cost record for every billable external call."""

    __tablename__ = "usage_ledger"
    __extra_table_args__ = (
        # A retried activity reports the same key and is recorded once.
        UniqueConstraint("workspace_id", "idempotency_key"),
        Index("ix_usage_ledger_ws_occurred", "workspace_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), index=True
    )
    #: "model" | "places" | "agent_reach" | "email" | "browser"
    category: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    #: Exact model/endpoint identifier actually used (mission section 9.1).
    resource: Mapped[str | None] = mapped_column(String(200))
    quantity: Mapped[float] = mapped_column(default=1.0, nullable=False)
    unit: Mapped[str] = mapped_column(String(30), default="call", nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(default=0.0, nullable=False)
    #: True when the cost was estimated rather than reported by the provider.
    cost_estimated: Mapped[bool] = mapped_column(default=True, nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class AuditLog(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """Append-only trail for sensitive mutations (mission section 18).

    Includes a hash chain so that tampering with history is detectable: each row
    commits to the previous row's hash for the same workspace.
    """

    __tablename__ = "audit_log"
    __extra_table_args__ = (
        Index("ix_audit_log_ws_occurred", "workspace_id", "occurred_at"),
        Index("ix_audit_log_ws_action", "workspace_id", "action"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: "user" | "system" | "workflow" | "worker"
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_ip: Mapped[str | None] = mapped_column(String(45))
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: Redacted before write: no keys, tokens, cookies, or full message bodies.
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), default="success", nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
