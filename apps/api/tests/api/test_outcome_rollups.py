"""The rollups, over the API.

Phase 01's whole complaint is that things get built and never wired: a poller
that was never scheduled, sequence steps nothing created, a local-frame column
with a partial index over an empty set. A query layer no route can reach is the
same defect, so the route is tested rather than assumed.

The other assertion here is about *nulls*. A slice below the sample floor has no
rate, and the API must return null rather than 0.0 -- a client that renders
"0% bounced" for a mailbox nobody has measured is worse than one that says
nothing, because it looks like good news.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
import pytest_asyncio
from titan.db.enums import WorkspaceRole
from titan.delivery.deliverability import MIN_SAMPLE_FOR_RATES

from tests.delivery.conftest import build_sendable

from .test_api_security import auth, make_member, slug_of, token_for

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client():
    import os

    os.environ.setdefault("TITAN_LOCAL_JWT_SECRET", "test-secret-not-for-production")
    from titan.config import get_settings

    get_settings.cache_clear()
    from titan.api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http_client:
        yield http_client


async def _headers(client, workspace, *, tag: str) -> dict[str, str]:
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag=tag)
    return auth(await token_for(client, email, await slug_of(workspace)))


async def test_every_grouping_is_reachable(client, workspace) -> None:
    """Six dimensions, one route. Omitting the parameter returns them all."""
    headers = await _headers(client, workspace, tag="roll1")

    response = await client.get("/api/v1/analytics/outcomes", headers=headers)

    assert response.status_code == 200
    dimensions = {row["dimension"] for row in response.json()}
    assert dimensions == {
        "campaign",
        "sender",
        "recipient_domain",
        "lead_source",
        "local_slot",
        "variant",
    }


async def test_one_grouping_can_be_asked_for(client, workspace) -> None:
    headers = await _headers(client, workspace, tag="roll2")

    response = await client.get(
        "/api/v1/analytics/outcomes",
        params={"dimension": "local_slot"},
        headers=headers,
    )

    body = response.json()
    assert response.status_code == 200
    assert len(body) == 1
    assert body[0]["dimension"] == "local_slot"
    assert body[0]["sample_floor"] == MIN_SAMPLE_FOR_RATES


async def test_an_unknown_grouping_is_refused_rather_than_ignored(
    client, workspace
) -> None:
    """Silently returning every dimension for a typo would be worse than a 422.

    The caller asked a specific question and would be shown an answer to a
    different one.
    """
    headers = await _headers(client, workspace, tag="roll3")

    response = await client.get(
        "/api/v1/analytics/outcomes",
        params={"dimension": "not_a_dimension"},
        headers=headers,
    )

    assert response.status_code == 422


async def test_a_thin_slice_reports_a_null_rate_not_zero(
    client, db_session, workspace
) -> None:
    """Null means "not measured". Zero means "measured, and clean".

    Collapsing the two is how an unmeasured mailbox reads as a healthy one.
    """
    from sqlalchemy import update
    from titan.db.models import Message

    built = await build_sendable(db_session, workspace, suffix="thinslice")
    await db_session.execute(
        update(Message)
        .where(Message.id == built.message_id)
        .values(sent_at=dt.datetime.now(dt.UTC))
    )
    await db_session.commit()

    headers = await _headers(client, workspace, tag="roll4")
    response = await client.get(
        "/api/v1/analytics/outcomes",
        params={"dimension": "campaign"},
        headers=headers,
    )

    slices = response.json()[0]["slices"]
    assert slices, "the send did not appear; the test proves nothing"
    thin = slices[0]
    assert thin["sent"] < MIN_SAMPLE_FOR_RATES
    assert thin["has_signal"] is False
    assert thin["bounce_rate"] is None
    assert thin["positive_reply_rate"] is None


async def test_the_rollups_require_authentication(client) -> None:
    response = await client.get("/api/v1/analytics/outcomes")

    assert response.status_code in (401, 403)
