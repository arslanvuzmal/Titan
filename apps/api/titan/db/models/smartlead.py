"""The Smartlead lead-import path, recovered from the deployed database.

These two tables existed in production and in no commit. They were created by
migration ``4c1d9b7a2e50``, which was applied to the live database from a
branch that was never pushed, and which is why Alembic refused to run against
production: the stamped revision named a file nobody had.

**They describe a different design from the one this repository sends with.**
The import path pushes leads into a Smartlead campaign and lets Smartlead send
to them on a sequence. :mod:`titan.delivery.providers.smartlead` does the
opposite -- it hands Smartlead exactly one already-authorized message at a
time, so every gate in this repository still runs before Smartlead is involved.
The second design exists because the first cannot be gated: once leads are in a
Smartlead sequence, Smartlead decides what to send and when, and Titan's
approval, suppression and quota checks are no longer in the path.

They are declared here rather than dropped because the data is real: six import
batches, seventeen leads genuinely pushed to Smartlead, and one webhook event.
That is the only record of what the deployed system did, and a schema the ORM
cannot see is one that silently accumulates rows nothing can read.

Nothing in this repository writes to them. If the import path is ever revived,
it must gate before importing, not after.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
    TimestampMixin,
    VersionedMixin,
    WorkspaceScoped,
    pg_enum,
    uuid_pk,
)
from titan.db.enums import SmartleadBatchStatus, SmartleadEventType


class SmartleadImportBatch(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """One run of the import, and what it did.

    The counters are constrained rather than trusted: a batch claiming to have
    imported more leads than it found eligible is arithmetically impossible, and
    a check constraint catches that at write time instead of leaving somebody to
    notice a wrong total weeks later.
    """

    __tablename__ = "smartlead_import_batches"
    __extra_table_args__ = (
        # A retried import must find its own prior run rather than pushing the
        # same leads to Smartlead twice.
        UniqueConstraint("workspace_id", "idempotency_key"),
        Index("ix_smartlead_import_batches_ws_created", "workspace_id", "created_at"),
        CheckConstraint(
            "leads_requested >= 0 AND leads_eligible >= 0 AND leads_imported >= 0 "
            "AND leads_skipped >= 0 AND leads_failed >= 0",
            name="counts_non_negative",
        ),
        CheckConstraint(
            "leads_imported <= leads_eligible",
            name="imported_within_eligible",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    smartlead_campaign_id: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Whether this went to the sandbox campaign rather than the production one.
    is_sandbox: Mapped[bool] = mapped_column(Boolean, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    #: A dry run counts and reports what it would do without writing to
    #: Smartlead. An import writes recipient data to a third party and is not
    #: undone by changing your mind afterwards.
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[SmartleadBatchStatus] = mapped_column(
        pg_enum(SmartleadBatchStatus, "smartlead_batch_status"), nullable=False
    )

    leads_requested: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Survived the eligibility rules. The gap between requested and eligible is
    #: where suppression, missing addresses and pattern guesses were refused.
    leads_eligible: Mapped[int] = mapped_column(Integer, nullable=False)
    leads_imported: Mapped[int] = mapped_column(Integer, nullable=False)
    leads_skipped: Mapped[int] = mapped_column(Integer, nullable=False)
    leads_failed: Mapped[int] = mapped_column(Integer, nullable=False)

    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Per-lead outcomes, so a partial batch can say which leads failed and why
    #: without a second query.
    outcomes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    provider_response: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text)


class SmartleadWebhookEvent(Base, WorkspaceScoped, TimestampMixin):
    """A delivery event Smartlead reported for an imported lead.

    Deduplicated on ``event_fingerprint`` rather than on a provider event id,
    because Smartlead's callbacks do not carry a stable one. The fingerprint is
    derived from the event's own content, so a redelivery collapses onto the
    first copy.
    """

    __tablename__ = "smartlead_webhook_events"
    __extra_table_args__ = (
        UniqueConstraint("event_fingerprint", name="uq_smartlead_events_fingerprint"),
        Index(
            "ix_smartlead_events_campaign_email",
            "smartlead_campaign_id",
            "normalized_email",
        ),
        Index("ix_smartlead_events_lead", "lead_id"),
        Index("ix_smartlead_webhook_events_ws_created", "workspace_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    event_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255), index=True)
    #: Smartlead's own wording, kept verbatim beside the normalized form so an
    #: event that mapped to UNKNOWN can be diagnosed without asking for a resend.
    raw_event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    event_type: Mapped[SmartleadEventType] = mapped_column(
        pg_enum(SmartleadEventType, "smartlead_event_type"), nullable=False
    )

    smartlead_campaign_id: Mapped[str] = mapped_column(String(40), nullable=False)
    smartlead_lead_id: Mapped[str | None] = mapped_column(String(64), index=True)
    normalized_email: Mapped[str] = mapped_column(String(320), nullable=False)
    #: Nullable, and SET NULL on delete: an event for a lead that was since
    #: erased is still evidence that something was sent, and losing it would
    #: quietly shrink the denominator of every deliverability figure.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )

    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: False means the callback's HMAC did not verify. Recorded rather than
    #: refused so a signing misconfiguration is visible as data instead of as
    #: an absence.
    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    ignored: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ignored_reason: Mapped[str | None] = mapped_column(Text)


__all__ = ["SmartleadImportBatch", "SmartleadWebhookEvent"]
