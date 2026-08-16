"""Campaigns, their persisted policy, and industry playbooks."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
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
from titan.db.enums import CampaignStatus, Industry, Region, SubRegion


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

    #: The market this campaign works. Coarser than target_country_code and not
    #: derived from it: one country code cannot express a campaign aimed at
    #: Europe, and most campaigns leave it empty. See
    #: titan.intelligence.portfolio.disagrees_with_country for how the two are
    #: reconciled -- surfaced, never silently rewritten.
    region: Mapped[Region] = mapped_column(
        pg_enum(Region, "region"),
        default=Region.UNSPECIFIED,
        server_default=Region.UNSPECIFIED.value,
        nullable=False,
        index=True,
    )

    #: The timezone band inside the market, where the market spans several.
    #: Only meaningful for the USA, Canada and Australia -- Europe's zones
    #: follow its national borders, which target_country_code already names.
    sub_region: Mapped[SubRegion] = mapped_column(
        pg_enum(SubRegion, "sub_region"),
        default=SubRegion.UNSPECIFIED,
        server_default=SubRegion.UNSPECIFIED.value,
        nullable=False,
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


class CampaignSender(Base, WorkspaceScoped, TimestampMixin):
    """One mailbox a campaign is allowed to send from.

    Campaigns carry a single ``sender_identity_id`` as well, and it stays: it is
    the fallback for a campaign with no pool rows, so nothing that worked before
    this table existed stops working. Where pool rows exist they win.

    A pool rather than a bigger number on one mailbox, because the constraint is
    per-mailbox and not per-campaign. Providers rate-limit a mailbox, receivers
    build reputation against a mailbox, and a mailbox that loses its DKIM record
    takes only its own share of the volume down with it. Three mailboxes at 50 a
    day is a different system from one at 150, not a scaled one.
    """

    __tablename__ = "campaign_senders"
    __extra_table_args__ = (
        UniqueConstraint("campaign_id", "sender_identity_id", name="uq_campaign_sender"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sender_identity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("sender_identities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


class MailboxRampState(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """What the ramp must remember about one provider mailbox between runs.

    Only two things, and both exist because the provider has a single
    ``max_email_per_day`` field per mailbox and the ramp writes it.

    ``ceiling`` is the number a human configured. It cannot be re-read from the
    provider each run, because by then it is the ramp's own last output -- see
    :func:`titan.delivery.mailbox_ramp.observe_ceiling` for what that costs.

    ``last_written_limit`` is what makes a human's edit distinguishable from the
    ramp's own write, and so it is the only thing that lets the ceiling move.

    Keyed on the provider's id rather than on a Titan sender identity: these
    mailboxes exist in Smartlead and may have no row here at all. The mailbox is
    the thing receivers judge, so the mailbox is the thing tracked.
    """

    __tablename__ = "mailbox_ramp_state"
    __extra_table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "provider",
            "external_id",
            name="uq_mailbox_ramp_state_mailbox",
        ),
        CheckConstraint("ceiling >= 0", name="ceiling_non_negative"),
        CheckConstraint(
            "last_written_limit IS NULL OR last_written_limit >= 0",
            name="last_written_non_negative",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    #: Which system the mailbox lives in, so a second provider cannot collide
    #: with Smartlead on a numeric id that means something else there.
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Carried for diagnosis only. The provider's id is the key; an address can
    #: be reassigned to a different account without the ramp's history moving.
    from_email: Mapped[str] = mapped_column(String(320), nullable=False)

    ceiling: Mapped[int] = mapped_column(Integer, nullable=False)
    last_written_limit: Mapped[int | None] = mapped_column(Integer)
    #: When the ramp last wrote, so an operator can tell a mailbox this manages
    #: from one it has only ever observed.
    last_written_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


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
        CheckConstraint(
            "send_window_start_hour >= 0 AND send_window_end_hour <= 24 "
            "AND send_window_start_hour < send_window_end_hour",
            name="send_window_ordered",
        ),
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

    #: What the campaign manager has set, if anything. Kept apart from the two
    #: columns above rather than overwriting them, because those are the human's
    #: numbers and they are the bound every managed value is clamped against.
    #: Writing to them directly would make next cycle's ceiling the manager's own
    #: previous answer, with nothing left to measure drift against.
    #:
    #: Null means the manager has no opinion and the configured value stands.
    #: See titan.autonomy.actuator for how the pair resolve: the effective limit
    #: is the *lower* of the two and the effective score the *higher*, so a
    #: managed value can only ever be more conservative than what was approved.
    managed_daily_send_limit: Mapped[int | None] = mapped_column(Integer)
    managed_min_lead_score: Mapped[int | None] = mapped_column(Integer)
    recipient_domain_daily_limit: Mapped[int] = mapped_column(default=2, nullable=False)
    min_spacing_seconds: Mapped[int] = mapped_column(default=90, nullable=False)
    max_followups: Mapped[int] = mapped_column(default=3, nullable=False)
    #: Day offsets for the sequence, e.g. [0, 3, 7, 14].
    followup_schedule_days: Mapped[list[str]] = mapped_column(
        JSONB, default=lambda: [0, 3, 7, 14], nullable=False
    )
    #: Whether the recipient's local schedule is honoured at all. Named for
    #: quiet hours because that is all it used to govern; it now governs the
    #: working-hours window below, which subsumes them -- anything outside
    #: 08:00-17:00 is also outside 08:00-20:00.
    respect_quiet_hours: Mapped[bool] = mapped_column(default=True, nullable=False)

    #: The working window, in the *recipient's* local time. End hour exclusive:
    #: 17 means the last minute is 16:59. Defaults are a conventional business
    #: day; the market's own working week comes from
    #: titan.policy.schedule.REGION_SEND_DAYS when a campaign is created.
    send_window_start_hour: Mapped[int] = mapped_column(
        default=8, server_default="8", nullable=False
    )
    send_window_end_hour: Mapped[int] = mapped_column(
        default=17, server_default="17", nullable=False
    )
    #: Weekdays the campaign may send, Monday is 0 (datetime.weekday()).
    #: Mon-Fri by default; a Middle East campaign wants Sun-Thu, which is why
    #: this is per-campaign rather than a constant.
    send_days: Mapped[list[int]] = mapped_column(
        JSONB, default=lambda: [0, 1, 2, 3, 4], nullable=False
    )

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
