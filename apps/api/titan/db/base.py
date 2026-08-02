"""SQLAlchemy declarative base, shared column types, and mixins.

Every persistent table in Titan-OS derives from :class:`Base`. Workspace-scoped
tables additionally derive from :class:`WorkspaceScoped`, which is what the
session-level isolation guard (titan.db.session) keys off -- a table that forgets
the mixin is caught by an invariant test rather than leaking data at runtime.
"""

from __future__ import annotations

import datetime as dt
import enum as _enum
import uuid
from typing import Any, ClassVar

from sqlalchemy import DateTime, Enum, ForeignKey, Index, MetaData, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

#: Explicit naming convention so Alembic autogenerate produces stable,
#: reviewable constraint names instead of database-assigned ones.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        list[str]: JSONB,
        list[dict[str, Any]]: JSONB,
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


def pg_enum(python_enum: type[_enum.Enum], name: str) -> Enum:
    """A PostgreSQL enum whose labels are the member *values*, not their names.

    SQLAlchemy defaults to storing ``Enum.name`` (``PENDING``). Titan stores
    ``Enum.value`` (``pending``) so that raw SQL, partial indexes, and the
    JSON API all speak the same vocabulary -- without this, a partial index
    such as ``WHERE status IN ('pending','deferred')`` fails to create.
    """
    return Enum(
        python_enum,
        name=name,
        native_enum=True,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    """Primary key column. UUIDv4 generated database-side."""
    return mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    )


def utcnow_col(onupdate: bool = False) -> Mapped[dt.datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now() if onupdate else None,
        nullable=False,
    )


class TimestampMixin:
    created_at: Mapped[dt.datetime] = utcnow_col()
    updated_at: Mapped[dt.datetime] = utcnow_col(onupdate=True)


class VersionedMixin:
    """Optimistic locking.

    SQLAlchemy increments ``version`` on flush and adds ``WHERE version = :old``
    to the UPDATE. A concurrent writer therefore raises StaleDataError instead of
    silently losing the earlier write (gap analysis H-09).
    """

    version: Mapped[int] = mapped_column(nullable=False, default=1)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:
        return {"version_id_col": cls.__table__.c.version}


class WorkspaceScoped:
    """Marks a table as tenant-owned.

    Two independent mechanisms rely on this marker:

    * :func:`titan.db.session.workspace_scoped_session` injects a
      ``workspace_id = :current`` criterion into every ORM query for these
      entities, so a handler that forgets its WHERE clause still cannot read
      another tenant's rows (gap analysis C-07).
    * The Alembic migration emits a PostgreSQL row-level-security policy per
      table as a defence-in-depth backstop for raw SQL paths.
    """

    @declared_attr
    def workspace_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            PGUUID(as_uuid=True),
            ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:
        # Composite index supporting the near-universal
        # "this workspace, newest first" access pattern.
        extra = getattr(cls, "__extra_table_args__", ())
        return (
            *extra,
            Index(f"ix_{cls.__tablename__}_ws_created", "workspace_id", "created_at"),
        )


class ImmutableMixin:
    """Rows that must never be updated after insert.

    Enforced three ways: an ORM event listener (titan.db.session), a database
    trigger installed by migration, and an invariant test. Used for evidence and
    provider events, where mutability would let a later run rewrite the record
    that justified an earlier claim.
    """

    __titan_immutable__ = True
