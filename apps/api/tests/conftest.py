"""Shared test fixtures.

Integration tests run against a **real** PostgreSQL, not a mock. The primitives
ColdOps depends on for safety -- ``ON CONFLICT DO UPDATE ... WHERE``,
``FOR UPDATE SKIP LOCKED``, partial unique indexes, row-level security, and
BEFORE UPDATE triggers -- have no faithful in-memory equivalent, so exercising
them against SQLite would prove nothing about production behaviour.

Point ``COLDOPS_TEST_DATABASE_URL`` at a disposable database. Tests that need one
skip cleanly when it is absent, so the pure-logic suite still runs anywhere.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from coldops.runtime import configure_event_loop

configure_event_loop()

TEST_DB_URL = os.getenv(
    "COLDOPS_TEST_DATABASE_URL",
    "postgresql+psycopg://titan:titan_dev_password@localhost:5439/titan",
)

# Point the settings singleton at the test database before anything imports it.
os.environ.setdefault("COLDOPS_DATABASE_URL", TEST_DB_URL)
os.environ.setdefault("COLDOPS_ENVIRONMENT", "test")


@pytest_asyncio.fixture(scope="session")
async def database_available() -> bool:
    """True when the integration database is reachable and migrated."""
    from coldops.db.session import get_engine
    from sqlalchemy import text

    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM workspaces LIMIT 1"))
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def db_session(database_available: bool) -> AsyncIterator:
    """An unscoped session on the test database."""
    if not database_available:
        pytest.skip("integration database unavailable (set COLDOPS_TEST_DATABASE_URL)")
    from coldops.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def workspace(db_session) -> AsyncIterator[uuid.UUID]:
    """A fresh workspace; all its rows cascade away on teardown."""
    from coldops.db.models import Workspace
    from sqlalchemy import delete

    ws = Workspace(
        name=f"Test WS {uuid.uuid4().hex[:8]}", slug=f"test-{uuid.uuid4().hex[:12]}"
    )
    db_session.add(ws)
    await db_session.commit()
    ws_id = ws.id
    try:
        yield ws_id
    finally:
        await db_session.rollback()
        await db_session.execute(delete(Workspace).where(Workspace.id == ws_id))
        await db_session.commit()


@pytest_asyncio.fixture
async def second_workspace(db_session) -> AsyncIterator[uuid.UUID]:
    """A second tenant, for cross-workspace isolation tests."""
    from coldops.db.models import Workspace
    from sqlalchemy import delete

    ws = Workspace(
        name=f"Other WS {uuid.uuid4().hex[:8]}", slug=f"other-{uuid.uuid4().hex[:12]}"
    )
    db_session.add(ws)
    await db_session.commit()
    ws_id = ws.id
    try:
        yield ws_id
    finally:
        await db_session.rollback()
        await db_session.execute(delete(Workspace).where(Workspace.id == ws_id))
        await db_session.commit()
