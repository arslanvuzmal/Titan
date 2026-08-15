"""The recipient domain history query, against a real PostgreSQL.

Separated from ``test_domain_health.py``, which is pure and hermetic, because
this covers the one part that cannot be: the SQL itself.

It exists because of a near-miss. ``_domain_history`` swallows exceptions and
returns an empty mapping, on the reasoning that history is purely additive and a
slow database must not abandon contact discovery. That reasoning is right, and
it also means a query with a typo in it degrades to "this domain has no history"
-- forever, silently, for every domain. Nothing else would have noticed: the
engine would keep working, every other test would keep passing, and the layer
would simply never fire.

So the assertion that matters here is not any particular count. It is that rows
come back at all.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import update
from titan.activities.pipeline import _domain_history
from titan.db.models import Message
from titan.intelligence.domain_health import DomainHealth, classify

from tests.delivery.conftest import build_sendable

FIXTURE_DOMAIN = "fixture-business.test"


async def _outcome(
    session,
    workspace_id,
    *,
    suffix: str,
    sent: bool = True,
    delivered: bool = False,
    bounced: bool = False,
    complained: bool = False,
) -> None:
    """One message to the fixture domain, with a delivery outcome on it."""
    fixture = await build_sendable(session, workspace_id, suffix=suffix)
    now = dt.datetime.now(dt.UTC)
    await session.execute(
        update(Message)
        .where(Message.id == fixture.message_id)
        .values(
            sent_at=now if sent else None,
            delivered_at=now if delivered else None,
            bounced_at=now if bounced else None,
            complained_at=now if complained else None,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_the_query_returns_rows_for_a_domain_with_history(
    db_session, workspace
) -> None:
    """The regression guard. An empty result here means the SQL is broken, not
    that the domain is clean -- and the fail-soft path makes those identical."""
    await _outcome(db_session, workspace, suffix="hist1", delivered=True)
    await _outcome(db_session, workspace, suffix="hist2", delivered=True)
    await _outcome(db_session, workspace, suffix="hist3", bounced=True)

    history = await _domain_history(workspace, [FIXTURE_DOMAIN])

    assert FIXTURE_DOMAIN in history, (
        "the query returned nothing for a domain with three messages; "
        "_domain_history fails soft, so a broken query looks exactly like this"
    )
    window = history[FIXTURE_DOMAIN]
    assert window.sent == 3
    assert window.delivered == 2
    assert window.bounced == 1
    assert window.complained == 0
    assert classify(window) is DomainHealth.WATCH


@pytest.mark.asyncio
async def test_a_complaint_is_counted_and_blocks_the_domain(
    db_session, workspace
) -> None:
    await _outcome(db_session, workspace, suffix="comp1", delivered=True)
    await _outcome(db_session, workspace, suffix="comp2", delivered=True, complained=True)

    history = await _domain_history(workspace, [FIXTURE_DOMAIN])

    window = history[FIXTURE_DOMAIN]
    assert window.complained == 1
    assert classify(window) is DomainHealth.BLOCKED


@pytest.mark.asyncio
async def test_a_domain_with_no_history_is_absent_rather_than_zero(
    db_session, workspace
) -> None:
    """Absent and all-zero are different facts. UNKNOWN means never written to;
    a zero row would claim we had tried and learned nothing."""
    await _outcome(db_session, workspace, suffix="abs1", delivered=True)

    history = await _domain_history(workspace, [FIXTURE_DOMAIN, "never-contacted.test"])

    assert FIXTURE_DOMAIN in history
    assert "never-contacted.test" not in history


@pytest.mark.asyncio
async def test_duplicate_domains_are_queried_once(db_session, workspace) -> None:
    await _outcome(db_session, workspace, suffix="dup1", delivered=True)

    history = await _domain_history(
        workspace, [FIXTURE_DOMAIN, FIXTURE_DOMAIN, FIXTURE_DOMAIN]
    )

    assert list(history) == [FIXTURE_DOMAIN]
    assert history[FIXTURE_DOMAIN].sent == 1


@pytest.mark.asyncio
async def test_no_domains_issues_no_query(db_session, workspace) -> None:
    assert await _domain_history(workspace, []) == {}


@pytest.mark.asyncio
async def test_another_workspace_history_is_not_visible(db_session, workspace) -> None:
    """One workspace's bounce record must not downgrade another workspace's lead.

    Nothing ambient provides this. ``workspace_session``'s guard rewrites ORM
    queries only, and the RLS policy is permissive while ``titan.workspace_id``
    is unset, so isolation here rests entirely on the ``workspace_id`` predicate
    written into the SQL. Delete that line and this test fails -- it did, which
    is why the line is there.
    """
    import uuid

    from titan.db.models import Workspace

    other = Workspace(name="Other WS", slug=f"other-{uuid.uuid4().hex[:12]}")
    db_session.add(other)
    await db_session.commit()
    try:
        await _outcome(db_session, other.id, suffix="iso1", bounced=True)

        mine = await _domain_history(workspace, [FIXTURE_DOMAIN])
        theirs = await _domain_history(other.id, [FIXTURE_DOMAIN])

        assert FIXTURE_DOMAIN not in mine
        assert theirs[FIXTURE_DOMAIN].bounced == 1
    finally:
        from sqlalchemy import delete

        await db_session.rollback()
        await db_session.execute(delete(Workspace).where(Workspace.id == other.id))
        await db_session.commit()
