"""Async engine, session factory, and the workspace-isolation guard.

Two defects from the baseline are addressed here (gap analysis C-07 and C-09):

* Read paths no longer open a transaction per request. Writes use an explicit
  unit of work with a short, visible boundary.
* Workspace isolation no longer depends on each handler remembering a WHERE
  clause. :func:`workspace_scoped_session` installs a loader criterion that
  applies to every ORM query for every :class:`WorkspaceScoped` entity, so a
  forgotten filter yields no rows instead of another tenant's rows.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any, Final

from sqlalchemy import event, orm
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import with_loader_criteria

from titan.config import get_settings
from titan.db.base import Base, ImmutableMixin, WorkspaceScoped

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

#: Session info key holding the active workspace UUID.
WORKSPACE_KEY: Final = "titan_workspace_id"


class WorkspaceIsolationError(RuntimeError):
    """Raised when scoped data is accessed without an active workspace."""


class ImmutableRowError(RuntimeError):
    """Raised on an attempt to UPDATE or DELETE an append-only row."""


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_pre_ping=True,
            # Statement timeout is a cheap backstop against a runaway query
            # holding a connection for the life of the process.
            connect_args={
                "options": f"-c statement_timeout={settings.database_statement_timeout_ms}"
            },
            echo=False,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def dispose_engine() -> None:
    """Release pooled connections. Called on graceful shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


# --------------------------------------------------------------------------
# Immutability enforcement
# --------------------------------------------------------------------------
@event.listens_for(orm.Session, "before_flush")
def _block_immutable_mutations(
    session: orm.Session, flush_context: Any, instances: Any
) -> None:
    """Refuse to UPDATE or DELETE append-only rows.

    A database trigger enforces the same rule for raw-SQL paths; this listener
    catches the far more common ORM path with a clearer error.
    """
    for obj in session.dirty:
        if isinstance(obj, ImmutableMixin) and session.is_modified(obj):
            raise ImmutableRowError(
                f"{type(obj).__name__} is append-only and cannot be updated. "
                "Insert a new row instead."
            )
    for obj in session.deleted:
        # Cascades from a parent delete are legitimate (e.g. purging a whole
        # research run); a direct delete of an evidence row is not.
        if isinstance(obj, ImmutableMixin) and obj not in session.info.get(
            "titan_cascade_deletes", ()
        ):
            raise ImmutableRowError(
                f"{type(obj).__name__} is append-only and cannot be deleted "
                "directly. Use the retention purge job."
            )


# --------------------------------------------------------------------------
# Workspace isolation
# --------------------------------------------------------------------------
def _install_workspace_guard(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Attach an automatic ``workspace_id = :ws`` criterion to every ORM query."""
    session.sync_session.info[WORKSPACE_KEY] = workspace_id

    @event.listens_for(session.sync_session, "do_orm_execute")
    def _add_criteria(execute_state: Any) -> None:
        if not execute_state.is_select:
            return
        if execute_state.is_column_load or execute_state.is_relationship_load:
            # Relationship loads are already constrained by their parent, which
            # was itself filtered. Re-applying here breaks eager loaders.
            return
        active = execute_state.session.info.get(WORKSPACE_KEY)
        if active is None:
            return
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                WorkspaceScoped,
                lambda cls: cls.workspace_id == active,
                include_aliases=True,
            )
        )


@contextlib.asynccontextmanager
async def workspace_session(
    workspace_id: uuid.UUID,
) -> AsyncIterator[AsyncSession]:
    """A read-oriented session hard-scoped to one workspace.

    No transaction is opened eagerly; PostgreSQL starts one implicitly on first
    statement and it is closed on exit. Use :func:`workspace_unit_of_work` when
    writing.
    """
    async with get_sessionmaker()() as session:
        _install_workspace_guard(session, workspace_id)
        try:
            yield session
        finally:
            await session.rollback()


@contextlib.asynccontextmanager
async def workspace_unit_of_work(
    workspace_id: uuid.UUID,
) -> AsyncIterator[AsyncSession]:
    """A write session with an explicit, short transaction boundary.

    Commits on clean exit, rolls back on any exception. Callers must not perform
    model, browser, or provider I/O inside this block (mission section 25) --
    an invariant test scans for such calls.
    """
    async with get_sessionmaker()() as session:
        _install_workspace_guard(session, workspace_id)
        async with session.begin():
            yield session


@contextlib.asynccontextmanager
async def system_unit_of_work() -> AsyncIterator[AsyncSession]:
    """Unscoped write session for genuinely cross-workspace machinery.

    Legitimate uses are narrow: the outbox worker's claim query (which spans
    workspaces before it knows which one it holds), webhook ingestion keyed by
    provider event id, and migrations/maintenance. Every caller is expected to
    re-scope as soon as the workspace is known; an invariant test enumerates the
    permitted call sites so a new one cannot appear unnoticed.
    """
    async with get_sessionmaker()() as session:
        async with session.begin():
            yield session


def assert_scoped(session: AsyncSession) -> uuid.UUID:
    """Return the session's workspace, or fail loudly.

    Used at the top of repository functions that must never run unscoped.
    """
    try:
        active = session.sync_session.info.get(WORKSPACE_KEY)
    except InvalidRequestError as exc:  # pragma: no cover - defensive
        raise WorkspaceIsolationError("session is not usable") from exc
    if active is None:
        raise WorkspaceIsolationError(
            "This operation requires a workspace-scoped session. "
            "Use workspace_session() or workspace_unit_of_work()."
        )
    return active


__all__ = [
    "AsyncSession",
    "Base",
    "ImmutableRowError",
    "WorkspaceIsolationError",
    "assert_scoped",
    "dispose_engine",
    "get_engine",
    "get_sessionmaker",
    "system_unit_of_work",
    "workspace_session",
    "workspace_unit_of_work",
]
