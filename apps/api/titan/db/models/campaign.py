"""Campaigns, their persisted policy, and industry playbooks."""

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
    false,
)
from sqlalchemy import text as sa_text
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

    #: Whether this campaign is a business type rather than a place.
    #:
    #: A campaign crossed with a city exhausts one market and stops. A campaign
    #: that is "dentists worth writing to" rotates through every territory the
    #: language gate admits, and each of its leads is routed to the carrier for
    #: the market that lead is actually in -- so the clock and the working week
    #: follow the recipient rather than the campaign.
    #:
    #: Distinct from ``region == UNSPECIFIED``, which means nobody has said.
    #: "Every market" and "not stated" must not be the same value: the twenty
    #: leftover test campaigns in this workspace are the second, and a rule that
    #: read them as the first would put them to work.
    spans_all_markets: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=false()
    )

    #: Which carrier campaign this one's leads are handed to.
    #:
    #: Null falls back to ``TITAN_SMARTLEAD_CAMPAIGN_ID``, which is how every
    #: campaign behaved before this column existed -- one carrier for every
    #: market, on one clock, which is why a Dubai recipient was scheduled to
    #: London hours. Set per market by ``titan.provision_smartlead``.
    smartlead_campaign_id: Mapped[int | None] = mapped_column(Integer, index=True)

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

    #: Whether this campaign may approve its own drafts.
    #:
    #: Fifth gate, and the only one that is about *review* rather than delivery.
    #: ``controlled_autopilot`` grants the AUTO_APPROVE capability, but the mode
    #: ladder is resolved from process, workspace and campaign together -- so
    #: turning on the process kill switch was enough to drop the human gate from
    #: every campaign at once, without anybody deciding that per campaign.
    #:
    #: This ANDs with the capability rather than replacing it: autopilot is still
    #: required, and on top of it a campaign has to have been opted in. Default
    #: false, so every campaign that exists today keeps the gate it was created
    #: with and a deploy changes no behaviour.
    #:
    #: The default is declared on the database as well as in Python. For a gate,
    #: the closed position should not depend on the row having been created
    #: through the ORM -- an insert from anywhere gets the human gate.
    auto_approve: Mapped[bool] = mapped_column(
        default=False, server_default=false(), nullable=False
    )

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
    #: The phrasing register the manager has promoted, or null for "no opinion",
    #: in which case the composer keeps picking per lead as it always has.
    #:
    #: An index rather than the variant string, because that is what the
    #: composer selects with and what the actuator can bound. A name would have
    #: to be parsed and validated at the point of use, which is the point where
    #: an unrecognised value would silently fall back to the old behaviour and
    #: look like the promotion never happened.
    managed_promoted_variant: Mapped[int | None] = mapped_column(Integer)
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


class CarrierCampaign(Base, WorkspaceScoped, TimestampMixin):
    """The carrier campaign that serves one market.

    Which Smartlead or Instantly campaign a message is handed to decides one
    thing above all others: *when it is actually sent*. Both carriers hold one
    clock per campaign, so the campaign is the timezone.

    That routing used to hang off ``Campaign.smartlead_campaign_id``, which is
    correct only while a Titan campaign is a single city. A campaign that is a
    business type spans six markets, and one column cannot name six carriers --
    so the market names its own carrier here, and a message is routed by the
    market its recipient is actually in.
    """

    __tablename__ = "carrier_campaigns"
    # Partial indexes, not a unique constraint over the nullable column:
    # Postgres treats NULLs as distinct, so a constraint including ``timezone``
    # would admit two market rows for one market -- and then which clock a lead
    # rides would depend on row order.
    __extra_table_args__ = (
        Index(
            "uq_carrier_campaign_market",
            "workspace_id",
            "provider",
            "region",
            unique=True,
            postgresql_where=sa_text("timezone IS NULL"),
        ),
        Index(
            "uq_carrier_campaign_clock",
            "workspace_id",
            "provider",
            "timezone",
            unique=True,
            postgresql_where=sa_text("timezone IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()

    #: ``smartlead`` or ``instantly``. Plain text rather than an enum: the set
    #: of carriers is settings-driven, and adding one must not need a migration
    #: before a single message can be routed through it.
    provider: Mapped[str] = mapped_column(String(20), nullable=False)

    # No index: one row per market per carrier is single digits, and the whole
    # table is read at once by the router rather than looked up per message.
    region: Mapped[Region] = mapped_column(pg_enum(Region, "region"), nullable=False)

    #: The clock this carrier campaign keeps, when it was provisioned for one
    #: rather than for a whole market. Null means it is the market's carrier,
    #: scheduled on the market's representative zone.
    #:
    #: An IANA name rather than a SubRegion band: bands are defined only for
    #: the USA, Canada and Australia, and two of the five markets getting this
    #: wrong are Europe (Dublin against Berlin) and the Gulf (Riyadh against
    #: Dubai), which no band can express.
    timezone: Mapped[str | None] = mapped_column(String(64))

    #: Text, because Smartlead's ids are numeric and Instantly's are not. A
    #: column typed for one carrier would need migrating to admit the other,
    #: and this is the value that decides where mail goes.
    carrier_campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
