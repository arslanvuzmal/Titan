"""Identity, tenancy, and provider-connection tables."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
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
    display_name: Mapped[str | None] = mapped_column(String(200))
    #: External IdP subject (Clerk `sub`). Null for local-auth users.
    external_subject: Mapped[str | None] = mapped_column(String(255), unique=True)
    #: Only populated in auth_mode="local"; argon2id.
    password_hash: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

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

    def authorization_errors(self) -> list[str]:
        """Reasons this identity may not be used for delivery."""
        errors: list[str] = []
        if not self.is_active:
            errors.append("sender identity is inactive")
        if not self.domain_verified:
            errors.append(f"sending domain {self.sending_domain} is not verified")
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
