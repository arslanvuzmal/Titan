"""Model runs, prompt versions, workflow tracking, CRM objects."""

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
from sqlalchemy.orm import Mapped, mapped_column

from titan.db.base import (
    Base,
    ImmutableMixin,
    TimestampMixin,
    VersionedMixin,
    WorkspaceScoped,
    pg_enum,
    uuid_pk,
)
from titan.db.enums import ModelTask, WorkflowRunStatus


class PromptVersion(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """An immutable prompt revision.

    Immutable so that a stored model_run always resolves to the exact text that
    produced it, which is what makes a past message auditable.
    """

    __tablename__ = "prompt_versions"
    __extra_table_args__ = (UniqueConstraint("workspace_id", "key", "version"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    task: Mapped[ModelTask] = mapped_column(
        pg_enum(ModelTask, "model_task"), nullable=False
    )
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    developer_policy: Mapped[str | None] = mapped_column(Text)
    #: JSON Schema the response must satisfy.
    output_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


class ModelRun(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """One model invocation, recorded whether it succeeded or not."""

    __tablename__ = "model_runs"
    __extra_table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key"),
        Index("ix_model_runs_ws_created", "workspace_id", "created_at"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    task: Mapped[ModelTask] = mapped_column(
        pg_enum(ModelTask, "model_task"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The exact model identifier the provider was asked for, verbatim
    #: (mission section 9.1: "Record the exact model ID used for every run").
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("prompt_versions.id", ondelete="SET NULL")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL")
    )

    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: sha256 of the assembled request, for reproducibility without storing
    #: potentially sensitive prompt content beyond the retention window.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Retained subject to the workspace retention policy; purged thereafter.
    request_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    schema_valid: Mapped[bool | None] = mapped_column()
    repair_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(Text)
    #: True when a fallback provider served this run after the primary failed.
    used_fallback: Mapped[bool] = mapped_column(default=False, nullable=False)


class WorkflowRun(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """Mirror of a Temporal execution, so the API can answer without Temporal."""

    __tablename__ = "workflow_runs"
    __extra_table_args__ = (
        UniqueConstraint("workflow_id", "run_id"),
        Index("ix_workflow_runs_ws_status", "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(String(255))
    workflow_type: Mapped[str] = mapped_column(String(120), nullable=False)
    task_queue: Mapped[str] = mapped_column(String(80), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[WorkflowRunStatus] = mapped_column(
        pg_enum(WorkflowRunStatus, "workflow_run_status"),
        default=WorkflowRunStatus.RUNNING,
        nullable=False,
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    #: Snapshot of the policy in force when the run started, so a later policy
    #: edit does not rewrite history.
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )


class WorkflowEvent(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """Append-only workflow event.

    Identity is (workflow_run_id, event_key). `event_key` is derived from
    semantic content — never from a timestamp or a random id — so a retried
    activity that re-emits the same logical event is collapsed rather than
    duplicated (mission section 5.1).
    """

    __tablename__ = "workflow_events"
    __extra_table_args__ = (UniqueConstraint("workflow_run_id", "event_key"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_key: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    activity_id: Mapped[str | None] = mapped_column(String(120))
    attempt: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class Meeting(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    __tablename__ = "meetings"

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(30), default="proposed", nullable=False)
    scheduled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    location_or_link: Mapped[str | None] = mapped_column(Text)
    external_ref: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)


class Task(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """Operator work item (e.g. "high-intent reply needs a response")."""

    __tablename__ = "tasks"
    __extra_table_args__ = (
        UniqueConstraint("workspace_id", "dedupe_key"),
        Index("ix_tasks_ws_status_due", "workspace_id", "status", "due_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    #: Prevents a retried classification from creating a second identical task.
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    due_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
