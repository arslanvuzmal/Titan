"""Drafting, approval, sequencing, and the transactional outbox.

The outbox is the single chokepoint through which every outbound message must
pass (invariant 4). No other module in the codebase is permitted to hold an
email-provider client -- enforced by tests/invariants/test_no_direct_send.py.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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
from titan.db.enums import DraftStatus, MessageState, OutboxStatus, ReplyClass


class MessageDraft(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """A generated, not-yet-authorized message.

    A model may create rows in this table and nothing else. Progression to an
    outbox row requires a human approval record (or, in controlled_autopilot, a
    policy-engine decision recorded with the same rigour).
    """

    __tablename__ = "message_drafts"
    __extra_table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key"),
        Index("ix_message_drafts_ws_status", "workspace_id", "status"),
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
    contact_channel_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("contact_channels.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence_step_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sequence_steps.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    status: Mapped[DraftStatus] = mapped_column(
        pg_enum(DraftStatus, "draft_status"),
        default=DraftStatus.GENERATED,
        nullable=False,
    )
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text)

    #: The machine-readable claim chain (mission section 12.2):
    #: [{sentence, claim, finding_id, evidence_ids: [...], source_url}]
    #: The validator rejects any sentence making a factual claim about the
    #: recipient's business that is absent from this structure.
    claim_map: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    #: Result of titan.intelligence.message_validator, stored so a reviewer can
    #: see exactly which rules passed.
    validation_report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    validation_passed: Mapped[bool] = mapped_column(default=False, nullable=False)

    model_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("model_runs.id", ondelete="SET NULL")
    )
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("prompt_versions.id", ondelete="SET NULL")
    )
    #: Which of the 12 template shapes this draft follows (section 12.3).
    template_key: Mapped[str] = mapped_column(String(60), nullable=False)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("message_drafts.id", ondelete="SET NULL")
    )

    approvals: Mapped[list[MessageApproval]] = relationship(
        back_populates="draft", cascade="all, delete-orphan", lazy="selectin"
    )


class MessageApproval(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """An immutable human decision on a specific draft *version*.

    Binding to `draft_version` is what prevents the edit-after-approval attack:
    changing the draft bumps its version, which invalidates the approval.
    """

    __tablename__ = "message_approvals"
    __extra_table_args__ = (
        UniqueConstraint("draft_id", "draft_version", "decision_seq"),
        CheckConstraint(
            "decision IN ('approved','rejected','expired','changes_requested')",
            name="decision_allowed",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    draft_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("message_drafts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The draft version this decision applies to. An outbox row may only be
    #: created when draft.version == approval.draft_version.
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_seq: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    #: Snapshot of the policy evaluation shown to the approver, so the record
    #: reflects what they actually saw.
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    #: Request context for accountability.
    actor_ip: Mapped[str | None] = mapped_column(String(45))
    actor_user_agent: Mapped[str | None] = mapped_column(String(400))

    draft: Mapped[MessageDraft] = relationship(back_populates="approvals")


class EmailSequence(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    __tablename__ = "email_sequences"
    __extra_table_args__ = (UniqueConstraint("workspace_id", "campaign_id", "name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    steps: Mapped[list[SequenceStep]] = relationship(
        back_populates="sequence", cascade="all, delete-orphan", lazy="selectin"
    )


class SequenceStep(Base, WorkspaceScoped, TimestampMixin):
    __tablename__ = "sequence_steps"
    __extra_table_args__ = (
        UniqueConstraint("sequence_id", "step_number"),
        CheckConstraint("delay_days >= 0", name="delay_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    sequence_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("email_sequences.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    delay_days: Mapped[int] = mapped_column(Integer, nullable=False)
    template_key: Mapped[str] = mapped_column(String(60), nullable=False)
    #: Each follow-up must contribute new evidence rather than re-sending the
    #: first message with different wording (section 13). Enforced by the
    #: validator: a step with this flag requires >=1 finding not cited before.
    requires_new_evidence: Mapped[bool] = mapped_column(default=True, nullable=False)

    sequence: Mapped[EmailSequence] = relationship(back_populates="steps")


class Message(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """The durable record of a message that entered the delivery pipeline."""

    __tablename__ = "messages"
    __extra_table_args__ = (
        UniqueConstraint("workspace_id", "dedupe_key"),
        Index("ix_messages_ws_state", "workspace_id", "state"),
        Index("ix_messages_provider_msg", "provider_message_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    draft_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("message_drafts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
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
    sender_identity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("sender_identities.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: Logical identity of "this message to this recipient for this step".
    #: Unique per workspace: the definition of exactly-once delivery.
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    to_email: Mapped[str] = mapped_column(String(320), nullable=False)
    to_email_normalized: Mapped[str] = mapped_column(
        String(320), nullable=False, index=True
    )
    to_domain: Mapped[str] = mapped_column(String(253), nullable=False, index=True)
    from_email: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)

    state: Mapped[MessageState] = mapped_column(
        pg_enum(MessageState, "message_state"),
        default=MessageState.QUEUED,
        nullable=False,
    )
    #: Monotonic guard: a webhook may only raise this value (invariant 13).
    state_rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Provider timestamp of the event that produced the current state, used to
    #: break ties deterministically when ranks are equal.
    state_event_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    first_opened_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    bounced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    complained_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Body retained only until the retention window expires.
    body_retained_until: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )


class OutboxMessage(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """The single chokepoint for delivery.

    Lease protocol (see titan.delivery.outbox_worker):
      1. ``SELECT ... WHERE status IN (pending, deferred) AND next_attempt_at <= now()
         ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT :batch``
      2. mark LEASED with ``lease_owner`` / ``leased_until``
      3. **re-evaluate the entire authorization chain** (nothing is trusted from
         when the row was created -- the campaign may have been paused, the
         recipient may have replied or unsubscribed in the interim)
      4. reserve quota atomically
      5. send with ``provider_idempotency_key``
      6. record provider id, mark SENT

    A crash at any point leaves the row leased; the lease expires and another
    worker retries. Because the provider receives the same idempotency key, a
    retry after an unacknowledged success does not duplicate the email.
    """

    __tablename__ = "outbox_messages"
    __extra_table_args__ = (
        UniqueConstraint("workspace_id", "dedupe_key"),
        UniqueConstraint("provider_idempotency_key"),
        Index(
            "ix_outbox_claimable",
            "status",
            "next_attempt_at",
            postgresql_where=text("status IN ('pending','deferred')"),
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    draft_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("message_drafts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("message_approvals.id", ondelete="RESTRICT")
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    sender_identity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("sender_identities.id", ondelete="RESTRICT"),
        nullable=False,
    )

    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Sent to the provider so a network-level retry is collapsed provider-side.
    provider_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    status: Mapped[OutboxStatus] = mapped_column(
        pg_enum(OutboxStatus, "outbox_status"),
        default=OutboxStatus.PENDING,
        nullable=False,
        index=True,
    )
    to_email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    to_domain: Mapped[str] = mapped_column(String(253), nullable=False)
    #: Rendered at queue time so the worker performs no model or template work.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    leased_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    #: Populated when the final authorization check refuses the send. Retained
    #: for audit: it is the record of the system stopping itself.
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderEvent(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """Raw, immutable webhook event.

    Stored before any interpretation so that a mis-parse can be replayed, and
    deduplicated by the provider's own event id (invariant 12).
    """

    __tablename__ = "provider_events"
    __extra_table_args__ = (
        UniqueConstraint("provider", "provider_event_id"),
        Index("ix_provider_events_msg", "message_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    #: Provider-reported occurrence time, used for ordering.
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    signature_verified: Mapped[bool] = mapped_column(nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: True when the event was accepted but did not change message state
    #: (duplicate, or lower rank than the current state).
    ignored: Mapped[bool] = mapped_column(default=False, nullable=False)
    ignored_reason: Mapped[str | None] = mapped_column(Text)


class InboundMessage(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """A reply received from a recipient."""

    __tablename__ = "inbound_messages"
    __extra_table_args__ = (UniqueConstraint("workspace_id", "provider_inbound_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_inbound_id: Mapped[str] = mapped_column(String(255), nullable=False)
    in_reply_to_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), index=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), index=True
    )
    from_email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(500))
    #: Untrusted content. Never placed in a model instruction channel.
    body_text: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )


class ReplyClassification(Base, WorkspaceScoped, TimestampMixin):
    __tablename__ = "reply_classifications"
    __extra_table_args__ = (UniqueConstraint("inbound_message_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    inbound_message_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("inbound_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    reply_class: Mapped[ReplyClass] = mapped_column(
        pg_enum(ReplyClass, "reply_class"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(nullable=False)
    #: Deterministic rules run first; the model only refines. Recorded so an
    #: operator can tell which decided.
    decided_by: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    #: Suggested reply is drafted, never auto-sent (mission section 14).
    suggested_reply_draft_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("message_drafts.id", ondelete="SET NULL")
    )
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("model_runs.id", ondelete="SET NULL")
    )
