"""The lead-source standings query, against a real PostgreSQL.

Same reason as ``test_domain_history_query``: ``_lead_source_windows`` fails soft
and returns an empty list, so a query with a mistake in it degrades to "no
sources to report" -- silently, every week, with every other test still green.
The assertion that matters is that rows come back at all.

The aggregate is also the kind that is easy to get quietly wrong. Joining leads
to messages multiplies rows, so a lead with four messages counts as four leads
unless the count is distinct, and a batch would report four times the leads it
found. That one is asserted directly.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import update
from titan.activities.reporting import _lead_source_windows
from titan.db.models import Lead, LeadSource, Message
from titan.db.session import get_sessionmaker
from titan.intelligence.lead_sources import SourceGrade, classify

from tests.delivery.conftest import build_sendable

NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)


async def _source(session, workspace_id, *, kind: str, label: str) -> uuid.UUID:
    source = LeadSource(
        workspace_id=workspace_id,
        kind=kind,
        label=label,
        idempotency_key=f"key-{uuid.uuid4().hex[:12]}",
        records_returned=0,
        estimated_cost_usd=6.0,
    )
    session.add(source)
    await session.commit()
    return source.id


async def _lead_from(
    session,
    workspace_id,
    source_id,
    *,
    suffix: str,
    messages: int = 1,
    sent: bool = True,
    bounced: bool = False,
    replied: bool = False,
):
    """A lead attributed to a source, with some messages behind it."""
    fixture = await build_sendable(session, workspace_id, suffix=suffix)
    await session.execute(
        update(Lead)
        .where(Lead.id == fixture.lead_id)
        .values(
            lead_source_id=source_id,
            replied_at=NOW if replied else None,
        )
    )
    await session.execute(
        update(Message)
        .where(Message.id == fixture.message_id)
        .values(
            sent_at=NOW if sent else None,
            bounced_at=NOW if bounced else None,
        )
    )
    # Extra messages to the same lead, so the distinct-count behaviour is
    # exercised rather than assumed.
    for extra in range(messages - 1):
        clone = await build_sendable(session, workspace_id, suffix=f"{suffix}x{extra}")
        await session.execute(
            update(Message)
            .where(Message.id == clone.message_id)
            .values(lead_id=fixture.lead_id, sent_at=NOW if sent else None)
        )
    await session.commit()
    return fixture


@pytest.mark.asyncio
async def test_the_query_returns_a_row_for_a_source_with_leads(
    db_session, workspace
) -> None:
    """The regression guard. Empty here means the SQL is broken, not that there
    are no sources -- and the fail-soft path makes those identical."""
    source_id = await _source(
        db_session, workspace, kind="google_places", label="dentists"
    )
    await _lead_from(db_session, workspace, source_id, suffix="ls1")
    await _lead_from(db_session, workspace, source_id, suffix="ls2", bounced=True)

    windows = await _lead_source_windows(db_session, workspace, NOW)

    assert windows, (
        "the query returned nothing for a source with two leads; "
        "_lead_source_windows fails soft, so a broken query looks like this"
    )
    found = next(w for w in windows if w.source_id == str(source_id))
    assert found.leads == 2
    assert found.kind == "google_places"
    assert found.label == "dentists"
    assert found.cost_usd == 6.0
    assert found.bounced == 1


@pytest.mark.asyncio
async def test_a_lead_with_several_messages_is_still_one_lead(
    db_session, workspace
) -> None:
    """The join multiplies rows. Without DISTINCT a lead written to four times
    reports as four leads, and every rate computed from it is wrong."""
    source_id = await _source(db_session, workspace, kind="csv_import", label="import")
    await _lead_from(db_session, workspace, source_id, suffix="lsm", messages=4)

    windows = await _lead_source_windows(db_session, workspace, NOW)
    found = next(w for w in windows if w.source_id == str(source_id))

    assert found.leads == 1
    assert found.contacted == 1
    assert found.sent == 4, "messages should still be counted per message"


@pytest.mark.asyncio
async def test_replies_are_counted_per_lead(db_session, workspace) -> None:
    source_id = await _source(db_session, workspace, kind="referral", label="referrals")
    await _lead_from(
        db_session, workspace, source_id, suffix="lsr", messages=3, replied=True
    )

    windows = await _lead_source_windows(db_session, workspace, NOW)
    found = next(w for w in windows if w.source_id == str(source_id))

    assert found.replied == 1
    assert found.reply_rate == 1.0


@pytest.mark.asyncio
async def test_a_source_older_than_the_lookback_is_not_reported(
    db_session, workspace
) -> None:
    source_id = await _source(db_session, workspace, kind="google_places", label="old")
    await _lead_from(db_session, workspace, source_id, suffix="lsold")
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(
            update(LeadSource)
            .where(LeadSource.id == source_id)
            .values(created_at=NOW - dt.timedelta(days=200))
        )

    windows = await _lead_source_windows(db_session, workspace, NOW)

    assert all(w.source_id != str(source_id) for w in windows)


@pytest.mark.asyncio
async def test_a_source_with_no_leads_is_absent(db_session, workspace) -> None:
    """An inner join, deliberately. A search that returned nothing has no
    downstream outcomes to grade, and a zero row would claim it had been tried."""
    source_id = await _source(db_session, workspace, kind="google_places", label="empty")

    windows = await _lead_source_windows(db_session, workspace, NOW)

    assert all(w.source_id != str(source_id) for w in windows)


@pytest.mark.asyncio
async def test_another_workspace_is_not_included(db_session, workspace) -> None:
    """Raw SQL carries its own workspace predicate; nothing about the session
    supplies one."""
    from titan.db.models import Workspace

    other = Workspace(name="Other", slug=f"o-{uuid.uuid4().hex[:12]}")
    db_session.add(other)
    await db_session.commit()
    # Read the id now. After the rollback below the instance is expired, and
    # touching an attribute would lazy-load outside the async context.
    other_id = other.id
    try:
        theirs = await _source(db_session, other_id, kind="google_places", label="theirs")
        await _lead_from(db_session, other_id, theirs, suffix="lsiso")

        mine = await _lead_source_windows(db_session, workspace, NOW)
        assert all(w.source_id != str(theirs) for w in mine)

        yours = await _lead_source_windows(db_session, other_id, NOW)
        assert any(w.source_id == str(theirs) for w in yours)
    finally:
        from sqlalchemy import delete

        await db_session.rollback()
        await db_session.execute(delete(Workspace).where(Workspace.id == other_id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_the_grade_survives_the_round_trip(db_session, workspace) -> None:
    """End to end: a batch whose leads mostly bounced comes back POOR."""
    source_id = await _source(db_session, workspace, kind="csv_import", label="bad list")
    for i in range(10):
        await _lead_from(
            db_session, workspace, source_id, suffix=f"lsbad{i}", bounced=i < 6
        )

    windows = await _lead_source_windows(db_session, workspace, NOW)
    found = next(w for w in windows if w.source_id == str(source_id))

    assert found.leads == 10
    assert found.bounced == 6
    assert classify(found) is SourceGrade.POOR
