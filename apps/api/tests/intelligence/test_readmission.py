"""Leads parked under a bar that has since moved.

Runs against a real PostgreSQL: the whole behaviour is one conditional UPDATE
joined to campaign policy, and a mock would only prove the SQL string had not
changed.

The expensive failure here is not leaving a lead parked. It is promoting one
that a person decided against -- rejected, suppressed, or already in a
conversation -- because that writes to somebody who asked us not to. Most of
the tests below guard that direction.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from titan.intelligence.readmission import readmit

from tests.delivery.conftest import build_sendable

pytestmark = pytest.mark.asyncio


async def _set(session, lead_id, *, status: str, score: int | None) -> None:
    await session.execute(
        text("UPDATE leads SET status = :s, latest_score = :sc WHERE id = :id"),
        {"s": status, "sc": score, "id": lead_id},
    )
    await session.commit()


async def _gate(session, campaign_id, value: int) -> None:
    await session.execute(
        text("UPDATE campaign_policies SET min_lead_score = :v WHERE campaign_id = :c"),
        {"v": value, "c": campaign_id},
    )
    await session.commit()


async def _status(session, lead_id) -> str:
    return await session.scalar(
        text("SELECT status FROM leads WHERE id = :id"), {"id": lead_id}
    )


async def test_a_lead_above_the_current_gate_is_readmitted(db_session, workspace):
    """Planted violation: never re-ask, and the bucket only fills.

    3,412 leads sat in manual_review at or above their own campaign's gate
    while those same campaigns filed 457 notices saying they had budget and no
    eligible leads.
    """
    lead = await build_sendable(db_session, workspace, suffix="parked")
    await _gate(db_session, lead.campaign_id, 55)
    await _set(db_session, lead.lead_id, status="manual_review", score=65)

    report = await readmit(db_session, workspace_id=workspace)
    await db_session.commit()

    assert report.promoted >= 1
    assert await _status(db_session, lead.lead_id) == "qualified"


async def test_a_lead_still_below_the_gate_stays_parked(db_session, workspace):
    """The threshold is still a threshold. This promotes leads the bar moved
    past, not leads the bar still excludes."""
    lead = await build_sendable(db_session, workspace, suffix="below")
    await _gate(db_session, lead.campaign_id, 70)
    await _set(db_session, lead.lead_id, status="manual_review", score=60)

    await readmit(db_session, workspace_id=workspace)
    await db_session.commit()

    assert await _status(db_session, lead.lead_id) == "manual_review"


@pytest.mark.parametrize("decided", ["rejected", "suppressed", "replied", "archived"])
async def test_a_decision_about_the_business_is_never_undone(
    db_session, workspace, decided
):
    """Planted violation: promote on score alone, ignoring status.

    These are decisions about the business, not a comparison against a number
    that has since moved. Re-admitting a suppressed lead writes to somebody who
    asked us not to, which is the one error here that cannot be taken back.
    """
    lead = await build_sendable(db_session, workspace, suffix=f"decided-{decided}")
    await _gate(db_session, lead.campaign_id, 55)
    await _set(db_session, lead.lead_id, status=decided, score=90)

    await readmit(db_session, workspace_id=workspace)
    await db_session.commit()

    assert await _status(db_session, lead.lead_id) == decided


async def test_an_unscored_lead_is_left_alone(db_session, workspace):
    """A lead with no score has never been measured. Promoting it would put an
    unjudged business in front of the composer."""
    lead = await build_sendable(db_session, workspace, suffix="unscored")
    await _gate(db_session, lead.campaign_id, 55)
    await _set(db_session, lead.lead_id, status="manual_review", score=None)

    await readmit(db_session, workspace_id=workspace)
    await db_session.commit()

    assert await _status(db_session, lead.lead_id) == "manual_review"


async def test_the_backlog_is_reported_so_it_can_be_watched(db_session, workspace):
    """It runs hourly against a backlog of thousands. A pass that reported only
    what it did would leave no way to see the queue draining."""
    lead = await build_sendable(db_session, workspace, suffix="counted")
    await _gate(db_session, lead.campaign_id, 55)
    await _set(db_session, lead.lead_id, status="manual_review", score=65)

    report = await readmit(db_session, workspace_id=workspace, limit=0)
    await db_session.commit()

    assert report.promoted == 0, "a zero batch must promote nothing"
    assert report.remaining >= 1, "and must still report what is waiting"
