"""The terminal success of the whole system finally has a writer.

``LeadStatus.MEETING_BOOKED`` sat in the enum with no code anywhere able to set
it. So the outcome the machine exists to produce was the one thing it could not
observe, and every self-tuning part of it optimised on ``replied_at`` instead --
a measure under which "not interested" is a win.

The other half of the fix is that a human sets this. A classifier reading
"sounds good, send me a time" and concluding a meeting exists would be
manufacturing a business outcome from a sentence, and that outcome then feeds
the campaign manager, the A/B decision and the weekly report.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from titan.db.enums import LeadStatus, WorkspaceRole
from titan.db.models import AuditLog, Lead
from titan.db.session import get_sessionmaker

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


async def _lead(session, workspace_id, *, suffix: str) -> uuid.UUID:
    fixture = await build_sendable(session, workspace_id, suffix=suffix)
    await session.commit()
    return fixture.lead_id


async def _status(lead_id: uuid.UUID) -> LeadStatus:
    async with get_sessionmaker()() as session:
        lead = await session.get(Lead, lead_id)
        assert lead is not None
        return lead.status


@pytest.mark.asyncio
async def test_a_meeting_can_finally_be_recorded(client, db_session, workspace) -> None:
    """The first writer this state has ever had."""
    lead_id = await _lead(db_session, workspace, suffix="mb1")
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="mb1")
    token = await token_for(client, email, await slug_of(workspace))

    response = await client.post(
        f"/api/v1/leads/{lead_id}/meeting",
        headers=auth(token),
        json={"note": "Tuesday 3pm, discovery call"},
    )

    assert response.status_code == 200
    assert await _status(lead_id) is LeadStatus.MEETING_BOOKED


@pytest.mark.asyncio
async def test_recording_a_meeting_twice_is_safe(client, db_session, workspace) -> None:
    """A double click should not produce two audit entries for one meeting."""
    lead_id = await _lead(db_session, workspace, suffix="mb2")
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="mb2")
    token = await token_for(client, email, await slug_of(workspace))

    first = await client.post(
        f"/api/v1/leads/{lead_id}/meeting", headers=auth(token), json={}
    )
    second = await client.post(
        f"/api/v1/leads/{lead_id}/meeting", headers=auth(token), json={}
    )

    assert first.status_code == second.status_code == 200
    async with get_sessionmaker()() as session:
        entries = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.resource_id == str(lead_id))
                    .where(AuditLog.action == "lead.meeting_booked")
                )
            )
            .scalars()
            .all()
        )
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_the_meeting_is_audited_with_what_it_replaced(
    client, db_session, workspace
) -> None:
    """A lead that jumps to booked from ``discovered`` was probably marked by
    somebody working from a calendar rather than from Titan, and that is worth
    being able to see later."""
    lead_id = await _lead(db_session, workspace, suffix="mb3")
    before = await _status(lead_id)
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="mb3")
    token = await token_for(client, email, await slug_of(workspace))

    await client.post(
        f"/api/v1/leads/{lead_id}/meeting", headers=auth(token), json={"note": "n"}
    )

    async with get_sessionmaker()() as session:
        entry = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.resource_id == str(lead_id))
                .where(AuditLog.action == "lead.meeting_booked")
            )
        ).scalar_one()

    assert entry.detail["previous_status"] == before.value
    assert entry.detail["note"] == "n"
    assert entry.actor_user_id is not None, "a machine must not appear as the actor"


@pytest.mark.asyncio
async def test_a_viewer_cannot_declare_a_meeting_booked(
    client, db_session, workspace
) -> None:
    """This number feeds the campaign manager and the weekly report. Writing it
    is a business claim, not a note."""
    lead_id = await _lead(db_session, workspace, suffix="mb4")
    _, email = await make_member(workspace, WorkspaceRole.VIEWER, tag="mb4")
    token = await token_for(client, email, await slug_of(workspace))

    response = await client.post(
        f"/api/v1/leads/{lead_id}/meeting", headers=auth(token), json={}
    )

    assert response.status_code == 403
    assert await _status(lead_id) is not LeadStatus.MEETING_BOOKED


@pytest.mark.asyncio
async def test_a_foreign_lead_is_not_found_rather_than_forbidden(
    client, db_session, workspace
) -> None:
    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="mb5")
    token = await token_for(client, email, await slug_of(workspace))

    response = await client.post(
        f"/api/v1/leads/{uuid.uuid4()}/meeting", headers=auth(token), json={}
    )

    assert response.status_code == 404
