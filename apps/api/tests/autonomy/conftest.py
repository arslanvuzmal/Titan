"""Fixtures for the autonomy tests.

``sendable`` is defined in ``tests/delivery/conftest.py`` and a conftest only
reaches its own package, so it is rebuilt here from the shared builder rather
than imported -- a fixture cannot be imported into scope, only redeclared.
"""

from __future__ import annotations

import pytest_asyncio

from tests.delivery.conftest import SendableFixture, build_sendable


@pytest_asyncio.fixture
async def sendable(db_session, workspace) -> SendableFixture:
    """A campaign with a policy, a lead and one message behind it."""
    fixture = await build_sendable(db_session, workspace)
    await db_session.commit()
    return fixture
