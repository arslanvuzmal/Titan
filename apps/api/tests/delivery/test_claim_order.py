"""Which lead gets written to when the queue is longer than the day's ceiling.

A mailbox has a daily ceiling. On any day the queue exceeds it -- which is
every day worth having -- the claim order decides who is contacted and who
waits. Pure FIFO decides that by when the crawler happened to reach the
business, which is a fact about the crawler and not about the prospect.

These need the database: the ordering lives in the claim SQL, and a test that
did not execute that SQL would be asserting about a string.

Each test asserts the order of *its own* rows within the claim, not the whole
claim. ``claim_batch`` is deliberately workspace-agnostic -- one worker drains
every tenant -- so rows a sibling test left behind are legitimately claimable
here, and a test that assumed otherwise would be asserting that the worker is
scoped when it is not.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest_asyncio
from sqlalchemy import select
from titan.db.models import Lead, OutboxMessage
from titan.delivery.outbox_worker import OutboxWorker

from tests.delivery.conftest import build_sendable, sending_settings

#: Big enough that this test's rows are always inside the claim, whatever
#: siblings have left lying about.
WIDE = 200


@pytest_asyncio.fixture(autouse=True)
async def empty_queue(db_session):
    """Start each test with an empty outbox.

    These assert on the order of a queue, so they have to own the queue.
    ``claim_batch`` is workspace-agnostic by design -- one worker drains every
    tenant -- so rows a sibling test committed are genuinely claimable here,
    and in a full run there are hundreds of them. Filtering the result to this
    test's own rows is not enough: with enough leftovers, this test's rows fall
    outside the claim entirely and the assertion passes on an empty list.
    """
    await db_session.execute(OutboxMessage.__table__.delete())
    await db_session.commit()


@pytest_asyncio.fixture
async def worker(db_session):
    """A worker that can claim, with no provider attached: claiming is all
    these tests exercise, and a provider would invite them to send."""
    return OutboxWorker(
        None,  # type: ignore[arg-type]
        sending_settings(),
        owner=f"test-{uuid.uuid4().hex[:8]}",
    )


def order_of(claimed, *wanted: uuid.UUID) -> list[uuid.UUID]:
    """The claimed rows that belong to this test, in the order claimed."""
    interesting = set(wanted)
    return [row.id for row in claimed if row.id in interesting]


async def test_the_best_lead_is_claimed_first(db_session, workspace, worker) -> None:
    """The whole point. Three leads, three scores, one order."""
    worst = await build_sendable(db_session, workspace, suffix="low", lead_score=58)
    best = await build_sendable(db_session, workspace, suffix="high", lead_score=94)
    middle = await build_sendable(db_session, workspace, suffix="mid", lead_score=71)

    claimed = await worker.claim_batch(db_session, limit=WIDE)

    assert order_of(claimed, worst.outbox_id, best.outbox_id, middle.outbox_id) == [
        best.outbox_id,
        middle.outbox_id,
        worst.outbox_id,
    ]


async def test_arrival_order_breaks_a_tie(db_session, workspace, worker) -> None:
    """Equal scores are common -- most of a campaign scores identically -- and
    then the oldest row should go, which is what FIFO was right about."""
    first = await build_sendable(db_session, workspace, suffix="first", lead_score=70)
    second = await build_sendable(db_session, workspace, suffix="second", lead_score=70)
    await db_session.execute(
        OutboxMessage.__table__.update()
        .where(OutboxMessage.id == first.outbox_id)
        .values(next_attempt_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    )
    await db_session.execute(
        OutboxMessage.__table__.update()
        .where(OutboxMessage.id == second.outbox_id)
        .values(next_attempt_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC))
    )
    await db_session.commit()

    claimed = await worker.claim_batch(db_session, limit=WIDE)

    assert order_of(claimed, first.outbox_id, second.outbox_id) == [
        first.outbox_id,
        second.outbox_id,
    ]


async def test_a_lead_with_no_score_goes_last_not_first(
    db_session, workspace, worker
) -> None:
    """NULLS LAST, deliberately. An unscored lead has not been judged, and
    treating "not judged" as "best" would put the least-known business at the
    front of a queue whose whole purpose is to spend a scarce ceiling well."""
    unscored = await build_sendable(db_session, workspace, suffix="none", lead_score=70)
    scored = await build_sendable(db_session, workspace, suffix="known", lead_score=65)
    await db_session.execute(
        Lead.__table__.update()
        .where(Lead.id == unscored.lead_id)
        .values(latest_score=None)
    )
    await db_session.commit()

    claimed = await worker.claim_batch(db_session, limit=WIDE)

    assert order_of(claimed, unscored.outbox_id, scored.outbox_id) == [
        scored.outbox_id,
        unscored.outbox_id,
    ]


async def test_claiming_still_leases_what_it_took(
    db_session, workspace, worker
) -> None:
    """The ordering change touched the locking clause; this is the property
    that clause exists for."""
    row = await build_sendable(db_session, workspace, suffix="lease", lead_score=80)

    claimed = await worker.claim_batch(db_session, limit=WIDE)

    assert row.outbox_id in {c.id for c in claimed}
    stored = (
        await db_session.execute(
            select(OutboxMessage).where(OutboxMessage.id == row.outbox_id)
        )
    ).scalar_one()
    assert stored.status.value == "leased"
    assert stored.leased_until is not None
