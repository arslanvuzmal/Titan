"""The dashboard read surface: pipeline counts, stage drill-down, mailboxes, activity."""

from __future__ import annotations

import datetime as dt

import pytest
from coldops.db.enums import MessageState, WorkspaceRole
from coldops.db.models import Message
from coldops.intelligence import event_stream
from sqlalchemy import update

from tests.api.test_api_security import auth, make_member, slug_of, token_for
from tests.api.test_crm import client  # noqa: F401 -- the shared ASGI client fixture
from tests.delivery.conftest import build_sendable


async def _token(client, workspace) -> str:  # noqa: F811
    _, email = await make_member(workspace, WorkspaceRole.RESEARCHER, tag="dash")
    return await token_for(client, email, await slug_of(workspace))


@pytest.mark.asyncio
async def test_the_pipeline_counts_businesses_stage_by_stage(
    client, db_session, workspace  # noqa: F811
):  # noqa: F811, RUF100
    fx = await build_sendable(db_session, workspace)
    await db_session.execute(
        update(Message)
        .where(Message.id == fx.message_id)
        .values(state=MessageState.SENT, sent_at=dt.datetime.now(dt.UTC), state_rank=1)
    )
    await db_session.commit()
    token = await _token(client, workspace)

    response = await client.get("/api/v1/dashboard/pipeline", headers=auth(token))

    assert response.status_code == 200, response.text
    stages = {s["key"]: s for s in response.json()["stages"]}
    assert next(iter(stages)) == "discovered" and list(stages)[-1] == "meeting"
    assert len(stages) == 13
    assert stages["discovered"]["count"] == 1
    assert stages["drafted"]["count"] == 1
    assert stages["approved"]["count"] == 1
    assert stages["sent"]["count"] == 1
    assert stages["delivered"]["count"] == 1
    assert stages["replied"]["count"] == 0
    assert stages["discovered"]["of_discovered"] is None
    assert stages["sent"]["of_discovered"] == 1.0


@pytest.mark.asyncio
async def test_a_stage_lists_its_businesses(client, db_session, workspace):  # noqa: F811
    fx = await build_sendable(db_session, workspace)
    token = await _token(client, workspace)

    response = await client.get("/api/v1/dashboard/pipeline/drafted", headers=auth(token))

    assert response.status_code == 200, response.text
    rows = response.json()
    assert [r["lead_id"] for r in rows] == [str(fx.lead_id)]
    assert rows[0]["business_name"]


@pytest.mark.asyncio
async def test_an_unknown_stage_is_a_404(client, workspace):  # noqa: F811
    token = await _token(client, workspace)
    response = await client.get(
        "/api/v1/dashboard/pipeline/nonsense", headers=auth(token)
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_activity_is_the_event_stream_newest_first(client, db_session, workspace):  # noqa: F811
    await build_sendable(db_session, workspace)
    await event_stream.project(
        db_session, workspace_id=workspace, since=event_stream.EPOCH
    )
    await db_session.commit()
    token = await _token(client, workspace)

    response = await client.get("/api/v1/dashboard/activity", headers=auth(token))
    assert response.status_code == 200, response.text
    rows = response.json()
    assert rows, "the seeded lead's events should be listed"
    times = [r["occurred_at"] for r in rows]
    assert times == sorted(times, reverse=True)
    assert any(r["business_name"] for r in rows)

    filtered = await client.get(
        "/api/v1/dashboard/activity", params={"kind": "draft."}, headers=auth(token)
    )
    assert filtered.json() and all(
        r["kind"].startswith("draft.") for r in filtered.json()
    )


@pytest.mark.asyncio
async def test_mailboxes_lists_every_sender(client, db_session, workspace):  # noqa: F811
    fx = await build_sendable(db_session, workspace)
    token = await _token(client, workspace)

    response = await client.get("/api/v1/dashboard/mailboxes", headers=auth(token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["warmup_hours_utc"] == list(range(9, 18))
    addresses = [m["address"] for m in body["mailboxes"]]
    assert len(addresses) == 1
    assert body["mailboxes"][0]["warmup_today"] >= 2
    assert body["mailboxes"][0]["sender_active"] is True
    del fx


@pytest.mark.asyncio
async def test_another_workspace_sees_nothing_of_this_one(
    client, db_session, workspace, second_workspace  # noqa: F811
):
    await build_sendable(db_session, workspace)
    await event_stream.project(
        db_session, workspace_id=workspace, since=event_stream.EPOCH
    )
    await db_session.commit()
    outsider = await _token(client, second_workspace)

    pipeline = await client.get("/api/v1/dashboard/pipeline", headers=auth(outsider))
    activity = await client.get("/api/v1/dashboard/activity", headers=auth(outsider))

    assert all(s["count"] == 0 for s in pipeline.json()["stages"])
    assert activity.json() == []
