"""Identity, tenancy, and provider-connection tables."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
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
from titan.db.enums import WorkspaceRole


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    #: Sign-in handle for auth_mode="local". Stored lowercased so that the
    #: unique constraint and the lookup agree -- a case-sensitive column would
    #: let "Arslan" and "arslan" both exist and only one of them ever log in.
    username: Mapped[str | None] = mapped_column(String(64), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    #: External IdP subject (Clerk `sub`). Null for local-auth users.
    external_subject: Mapped[str | None] = mapped_column(String(255), unique=True)
    #: Only populated in auth_mode="local"; argon2id. Null means this account
    #: cannot sign in locally at all -- never "no passcode required".
    password_hash: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    #: Online-guessing defence. Kept on the row rather than in process memory
    #: so the count survives a restart and holds across API replicas; an
    #: in-memory counter is reset by the very deploy an attacker can trigger by
    #: making the service fall over.
    failed_login_count: Mapped[int] = mapped_column(default=0, nullable=False)
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Workspace(Base, TimestampMixin, VersionedMixin):
    """The tenancy boundary. Every scoped table's workspace_id points here."""

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)

    #: Workspace-level ceiling. Never more permissive than the process-level
    #: TITAN_OPERATING_MODE; the effective mode is min(process, ws, campaign).
    operating_mode: Mapped[OperatingMode] = mapped_column(
        pg_enum(OperatingMode, "operating_mode"),
        default=OperatingMode.RESEARCH_ONLY,
        nullable=False,
    )

    #: Second of four independent gates on delivery. An API request can set this
    #: only with the `sending:enable` capability, and it is always audit-logged.
    sending_authorized: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    sending_authorized_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    sending_authorized_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    daily_send_limit: Mapped[int] = mapped_column(default=50, nullable=False)
    daily_budget_usd: Mapped[float] = mapped_column(default=25.0, nullable=False)
    #: Data-retention overrides in days; null falls back to global policy.
    retention_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )

    members: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class WorkspaceMember(Base, TimestampMixin):
    """Server-side source of truth for a user's role (gap analysis H-12).

    The role is deliberately *not* trusted from a JWT claim: tokens outlive
    revocation, so authorization reads this table on every request.
    """

    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[WorkspaceRole] = mapped_column(
        pg_enum(WorkspaceRole, "workspace_role"), nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class SenderIdentity(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """A verified From: address plus its compliance metadata.

    Third of four delivery gates (invariant 10). All of `domain_verified`,
    `spf_ok`, `dkim_ok`, `dmarc_ok`, and a non-empty `mailing_address` are
    required before this identity may appear on an outbound message.
    """

    __tablename__ = "sender_identities"
    __extra_table_args__ = (UniqueConstraint("workspace_id", "from_email"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    from_email: Mapped[str] = mapped_column(String(320), nullable=False)
    from_name: Mapped[str] = mapped_column(String(200), nullable=False)
    reply_to_email: Mapped[str] = mapped_column(String(320), nullable=False)
    sending_domain: Mapped[str] = mapped_column(String(253), nullable=False, index=True)

    domain_verified: Mapped[bool] = mapped_column(default=False, nullable=False)
    spf_ok: Mapped[bool] = mapped_column(default=False, nullable=False)
    dkim_ok: Mapped[bool] = mapped_column(default=False, nullable=False)
    dmarc_ok: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: CAN-SPAM 5(a)(5) physical postal address. Required in every footer.
    mailing_address: Mapped[str | None] = mapped_column(Text)
    unsubscribe_mailto: Mapped[str | None] = mapped_column(String(320))
    unsubscribe_url_template: Mapped[str | None] = mapped_column(Text)
    supports_one_click_unsubscribe: Mapped[bool] = mapped_column(
        default=False, nullable=False
    )

    daily_send_limit: Mapped[int] = mapped_column(default=50, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    #: When this mailbox began building reputation, if that predates Titan.
    #:
    #: Warm-up position is otherwise derived from ``min(messages.sent_at)``,
    #: which is Titan's record of having sent through the mailbox -- a lower
    #: bound on its age, not its age. ``sales@`` was connected in Smartlead on
    #: 7 August with its warm-up pool running from that day, had no row here
    #: until the 17th, and was therefore placed on day zero and allowed five
    #: messages a day. The mailbox was ten days warm; only Titan's view of it
    #: was new.
    #:
    #: Set from the provider's own account record, never by hand, and consulted
    #: alongside the send history rather than instead of it -- the earlier of
    #: the two wins, so this can move a mailbox earlier in the ramp and never
    #: later. Null means "no evidence beyond what Titan sent", which is the
    #: previous behaviour exactly.
    warmup_started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    def authorization_errors(self) -> list[str]:
        """Reasons this identity may not be used for delivery.

        The four authentication flags are checked *together with*
        ``last_verified_at``, because a boolean on its own is an assertion
        rather than evidence. Twenty identities were found in production
        claiming SPF, DKIM and DMARC on a domain with no DNS at all; every one
        of them passed this gate, because nothing had ever asked when the claim
        was last true.

        Expiring the claim is what closes that. A flag can only be renewed by
        ``titan.intelligence.sender_auth.check_domain_auth`` actually resolving
        the domain, so an identity that nobody can verify stops sending on its
        own rather than sending forever on a typo.
        """
        from titan.intelligence.sender_auth import MAX_VERIFICATION_AGE, is_stale

        errors: list[str] = []
        if not self.is_active:
            errors.append("sender identity is inactive")
        if not self.domain_verified:
            errors.append(f"sending domain {self.sending_domain} is not verified")
        elif is_stale(self.last_verified_at):
            days = MAX_VERIFICATION_AGE.days
            errors.append(
                f"sending domain {self.sending_domain} has not been verified "
                f"in the last {days} days"
                + ("" if self.last_verified_at else "; it has never been verified")
            )
        for flag, name in (
            (self.spf_ok, "SPF"),
            (self.dkim_ok, "DKIM"),
            (self.dmarc_ok, "DMARC"),
        ):
            if not flag:
                errors.append(f"{name} is not confirmed for {self.sending_domain}")
        if not (self.mailing_address or "").strip():
            errors.append("sender mailing address is not configured")
        if not (self.unsubscribe_mailto or self.unsubscribe_url_template):
            errors.append("no unsubscribe mechanism is configured")
        return errors


class ProviderConnection(Base, WorkspaceScoped, TimestampMixin, VersionedMixin):
    """Configuration for an external provider.

    Credentials are stored encrypted (Fernet, key from TITAN_ENCRYPTION_KEY) and
    are never returned by any API serializer -- only `has_credential` is exposed
    (invariant 19).
    """

    __tablename__ = "provider_connections"
    __extra_table_args__ = (UniqueConstraint("workspace_id", "kind", "name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    #: e.g. "email", "discovery", "model", "enrichment"
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    #: e.g. "resend", "google_places", "nvidia", "agent_reach"
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    encrypted_credential: Mapped[bytes | None] = mapped_column()
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    last_health_check_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_health_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_health_detail: Mapped[str | None] = mapped_column(Text)

    #: Circuit breaker state, updated by the provider call sites.
    consecutive_failures: Mapped[int] = mapped_column(default=0, nullable=False)
    circuit_open_until: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    @property
    def has_credential(self) -> bool:
        return self.encrypted_credential is not None


class SenderHealthSnapshot(Base, WorkspaceScoped, TimestampMixin):
    """One day's measured health for one sending identity.

    Mutable and keyed by date, following ``quota_counters`` rather than the
    append-only tables. A snapshot is a rolling aggregate, not a record of an
    event: the outbox worker upserts it as a by-product of the deliverability
    check it already runs, so the last send of the day leaves the day's final
    numbers. Writing one row per message instead would be append-only, faithful,
    and produce tens of thousands of rows nobody would ever read.

    Everything here was already being computed and thrown away. Persisting it is
    what turns "this mailbox is at 0.04%" into "this mailbox was at 0.01% last
    Tuesday", which is the only form of the number an operator can act on.
    """

    __tablename__ = "sender_health_snapshots"
    __extra_table_args__ = (
        # The unique constraint's own btree is also the read path -- "the last
        # N days for this sender" is a prefix scan on exactly these columns in
        # this order. A second index over the same tuple would be paid for on
        # every write and never chosen by the planner.
        UniqueConstraint(
            "workspace_id",
            "sender_identity_id",
            "captured_on",
            name="uq_sender_health_day",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    sender_identity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("sender_identities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Denormalised so a snapshot still reads sensibly after the identity is
    #: deleted or its domain changed. History that silently rewrites itself is
    #: not history.
    sending_domain: Mapped[str] = mapped_column(String(253), nullable=False)
    #: UTC date, matching quota_counters.window_date.
    captured_on: Mapped[dt.date] = mapped_column(Date, nullable=False)

    #: "unknown" | "warming" | "healthy" | "watch" | "degraded" | "blocked".
    #: Deliberately a string rather than a native enum: this is a derived
    #: judgement whose vocabulary will move as the classifier learns, and a
    #: migration per adjustment would discourage adjusting it.
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    domain_verified: Mapped[bool] = mapped_column(default=False, nullable=False)
    spf_ok: Mapped[bool] = mapped_column(default=False, nullable=False)
    dkim_ok: Mapped[bool] = mapped_column(default=False, nullable=False)
    dmarc_ok: Mapped[bool] = mapped_column(default=False, nullable=False)
    auth_stale: Mapped[bool] = mapped_column(default=False, nullable=False)

    window_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    window_delivered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    window_bounced: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    window_complained: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deferred: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    sent_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warmup_day: Mapped[int | None] = mapped_column(Integer)
    warmup_limit: Mapped[int | None] = mapped_column(Integer)

    #: Human-readable, in the classifier's words, for the operator view.
    reasons: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
