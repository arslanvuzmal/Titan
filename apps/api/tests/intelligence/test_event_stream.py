"""The event stream: one table, projected, idempotent, and free of personal data."""

from __future__ import annotations

import datetime as dt
import re
import uuid

import pytest
from coldops.db.enums import MessageState
from coldops.db.models import Message
from coldops.intelligence import event_stream as es
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from tests.delivery.conftest import build_sendable

NOW = dt.datetime.now(dt.UTC)


async def test_a_lead_history_is_one_query_in_order(db_session, workspace):
    fx = await build_sendable(db_session, workspace)
    await db_session.execute(
        update(Message)
        .where(Message.id == fx.message_id)
        .values(state=MessageState.SENT, sent_at=NOW, state_rank=1)
    )
    await db_session.commit()

    await es.project(db_session, workspace_id=workspace, since=es.EPOCH)
    await db_session.commit()

    rows = await es.lead_history(db_session, workspace_id=workspace, lead_id=fx.lead_id)
    kinds = {r["kind"] for r in rows}
    assert {
        "lead.discovered",
        "draft.created",
        "draft.decided",
        "message.sent",
    } <= kinds
    times = [r["occurred_at"] for r in rows]
    assert times == sorted(times)


async def test_projecting_twice_inserts_nothing_the_second_time(db_session, workspace):
    await build_sendable(db_session, workspace)
    first = await es.project(db_session, workspace_id=workspace, since=es.EPOCH)
    await db_session.commit()
    second = await es.project(db_session, workspace_id=workspace, since=es.EPOCH)
    await db_session.commit()
    assert first.total > 0
    assert second.total == 0


async def test_a_late_bounce_is_caught_by_record_time_not_event_time(
    db_session, workspace
):
    """The bounce happened a week ago but was only written now; it must still project."""
    fx = await build_sendable(db_session, workspace)
    a_week_ago = NOW - dt.timedelta(days=7)
    await db_session.execute(
        update(Message)
        .where(Message.id == fx.message_id)
        .values(
            state=MessageState.BOUNCED,
            sent_at=a_week_ago,
            bounced_at=a_week_ago,
            bounce_kind="hard",
            state_rank=5,
        )
    )
    await db_session.commit()

    await es.project(db_session, workspace_id=workspace, since=NOW - es.DEFAULT_LOOKBACK)
    await db_session.commit()

    rows = await es.lead_history(db_session, workspace_id=workspace, lead_id=fx.lead_id)
    bounced = [r for r in rows if r["kind"] == "message.bounced"]
    assert len(bounced) == 1
    assert bounced[0]["payload"]["bounce_kind"] == "hard"


async def test_no_address_or_body_reaches_the_stream(db_session, workspace):
    fx = await build_sendable(db_session, workspace)
    await es.project(db_session, workspace_id=workspace, since=es.EPOCH)
    await db_session.commit()
    payloads = (
        await db_session.execute(
            text("SELECT payload::text FROM events WHERE workspace_id = :ws"),
            {"ws": workspace},
        )
    ).scalars()
    for payload in payloads:
        assert fx.to_email not in payload
        assert "@" not in payload


async def test_another_workspace_is_never_projected(db_session, workspace):
    await build_sendable(db_session, workspace)
    report = await es.project(db_session, workspace_id=uuid.uuid4(), since=es.EPOCH)
    await db_session.commit()
    assert report.total == 0


async def test_the_stream_is_append_only(db_session, workspace):
    await build_sendable(db_session, workspace)
    await es.project(db_session, workspace_id=workspace, since=es.EPOCH)
    await db_session.commit()
    with pytest.raises(DBAPIError):
        await db_session.execute(
            text("UPDATE events SET kind = 'tampered' WHERE workspace_id = :ws"),
            {"ws": workspace},
        )
    await db_session.rollback()


# ---------------------------------------------------------------- static


_FORBIDDEN_COLUMNS = re.compile(
    r"\b(body_text|body_html|to_email|to_email_normalized|from_email|from_email_normalized|"
    r"client_ip|user_agent|contact_name|contact_email|notes|normalized_value|seed_address|"
    r"subject|raw_payload)\b"
)


@pytest.mark.parametrize("projection", es.PROJECTIONS, ids=lambda p: p.source)
def test_no_projection_selects_personal_data(projection):
    """Checked on the SQL itself, so a new projection cannot copy one in unnoticed."""
    found = _FORBIDDEN_COLUMNS.findall(projection.select)
    assert not found, f"{projection.source} copies {found} into events"


@pytest.mark.parametrize("projection", es.PROJECTIONS, ids=lambda p: p.source)
def test_every_projection_is_scoped_to_its_workspace(projection):
    assert "workspace_id = :ws" in projection.select


def test_every_source_appears_once():
    sources = [p.source for p in es.PROJECTIONS]
    assert len(sources) == len(set(sources))
