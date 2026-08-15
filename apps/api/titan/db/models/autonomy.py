"""What the campaign manager did, and what it was looking at when it decided."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db.base import Base, ImmutableMixin, TimestampMixin, WorkspaceScoped, uuid_pk


class AutonomyDecision(Base, WorkspaceScoped, TimestampMixin, ImmutableMixin):
    """One decision the manager made, or was refused.

    Append-only, and it records the refusals too. A proposal the bounds clamped
    is the most interesting row in the table: it is the manager reaching for
    something and the boundary holding, which is the only direct evidence that
    the boundary works at all. Storing only what was applied would leave exactly
    the events worth auditing unrecorded.

    Every column here answers one of the questions an operator asks when they
    find a number they did not set: what changed, from what to what, why, on
    what evidence, how sure, and did it actually happen.
    """

    __tablename__ = "autonomy_decisions"
    __extra_table_args__ = (
        Index(
            "ix_autonomy_decisions_campaign", "workspace_id", "campaign_id", "decided_at"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    decided_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    #: An Actuation value. A plain string rather than a native enum: the set is
    #: the manager's surface and is expected to grow, and a migration per
    #: addition would put a schema change between somebody and a decision about
    #: how much autonomy to grant.
    actuation: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    #: The campaign health that prompted it.
    health: Mapped[str] = mapped_column(String(20), nullable=False)

    previous_value: Mapped[int | None] = mapped_column(Integer)
    proposed_value: Mapped[int | None] = mapped_column(Integer)
    #: What was actually written. Differs from proposed when a bound clamped it.
    applied_value: Mapped[int | None] = mapped_column(Integer)

    applied: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: Set when the bounds changed or rejected the proposal. The audit trail's
    #: most useful column.
    refusal: Mapped[str | None] = mapped_column(Text)

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: The metrics the decision was made on, so it can be re-read months later
    #: without reconstructing what the numbers were at the time.
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    #: Recorded, never acted on -- see titan.autonomy.manager.confidence_for.
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
