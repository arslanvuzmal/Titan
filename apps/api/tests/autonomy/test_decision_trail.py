"""The audit trail, against a real database.

The assertion that carries the most weight is that a *refused* proposal still
produces a row. A table containing only the changes that succeeded is a record
of the manager agreeing with itself; the rows worth having are the ones where it
reached for something and the boundary held.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select, update
from titan.autonomy.actuator import Actuation, Bounds, Proposal
from titan.autonomy.apply import apply_all
from titan.autonomy.health import CampaignHealth
from titan.db.models import AutonomyDecision, CampaignPolicy
from titan.db.session import get_sessionmaker, workspace_unit_of_work

from tests.delivery.conftest import build_sendable

NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)
BOUNDS = Bounds(configured_daily_limit=40, configured_min_lead_score=70)


def proposal(actuation: Actuation, campaign_id, *, current: int, proposed: int):
    return Proposal(
        actuation=actuation,
        campaign_id=str(campaign_id),
        current=current,
        proposed=proposed,
        reason="test decision",
        confidence=0.5,
        evidence={"sent": 400},
    )


async def _apply(workspace_id, campaign_id, proposals):
    async with workspace_unit_of_work(workspace_id) as session:
        return await apply_all(
            session,
            workspace_id=workspace_id,
            campaign_id=campaign_id,
            health=CampaignHealth.DEGRADED,
            proposals=proposals,
            bounds=BOUNDS,
            now=NOW,
        )


async def _decisions(workspace_id) -> list[AutonomyDecision]:
    async with get_sessionmaker()() as s:
        return list(
            (
                await s.execute(
                    select(AutonomyDecision)
                    .where(AutonomyDecision.workspace_id == workspace_id)
                    .order_by(AutonomyDecision.actuation)
                )
            )
            .scalars()
            .all()
        )


async def _policy(campaign_id) -> CampaignPolicy:
    async with get_sessionmaker()() as s:
        return (
            await s.execute(
                select(CampaignPolicy).where(CampaignPolicy.campaign_id == campaign_id)
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_an_applied_decision_writes_the_managed_column(
    db_session, sendable
) -> None:
    await _apply(
        sendable.workspace_id,
        sendable.campaign_id,
        [
            proposal(
                Actuation.SET_DAILY_LIMIT, sendable.campaign_id, current=40, proposed=10
            )
        ],
    )

    policy = await _policy(sendable.campaign_id)
    assert policy.managed_daily_send_limit == 10


@pytest.mark.asyncio
async def test_the_humans_own_columns_are_never_touched(db_session, sendable) -> None:
    """The anchor. If the manager wrote here, next cycle's ceiling would be its
    own previous answer and there would be nothing left to measure against."""
    before = await _policy(sendable.campaign_id)
    configured_limit = before.daily_send_limit
    configured_score = before.min_lead_score

    await _apply(
        sendable.workspace_id,
        sendable.campaign_id,
        [
            proposal(
                Actuation.SET_DAILY_LIMIT, sendable.campaign_id, current=40, proposed=5
            ),
            proposal(
                Actuation.SET_MIN_LEAD_SCORE,
                sendable.campaign_id,
                current=70,
                proposed=78,
            ),
        ],
    )

    after = await _policy(sendable.campaign_id)
    assert after.daily_send_limit == configured_limit
    assert after.min_lead_score == configured_score


@pytest.mark.asyncio
async def test_a_clamped_proposal_is_recorded_and_not_applied_as_asked(
    db_session, sendable
) -> None:
    """The row worth having: the manager reaching past the ceiling and the
    boundary holding. Storing only successes would leave this unrecorded."""
    verdicts = await _apply(
        sendable.workspace_id,
        sendable.campaign_id,
        [
            proposal(
                Actuation.SET_DAILY_LIMIT, sendable.campaign_id, current=40, proposed=9999
            )
        ],
    )

    assert verdicts[0].clamped is True
    rows = await _decisions(sendable.workspace_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.proposed_value == 9999
    assert row.applied_value == BOUNDS.configured_daily_limit
    assert row.refusal and "clamped" in row.refusal


@pytest.mark.asyncio
async def test_a_decision_records_why_and_on_what(db_session, sendable) -> None:
    """Every question an operator asks when they find a number they did not
    set: what changed, from what to what, why, on what evidence, how sure."""
    await _apply(
        sendable.workspace_id,
        sendable.campaign_id,
        [
            proposal(
                Actuation.SET_DAILY_LIMIT, sendable.campaign_id, current=40, proposed=10
            )
        ],
    )

    row = (await _decisions(sendable.workspace_id))[0]
    assert row.actuation == Actuation.SET_DAILY_LIMIT.value
    assert row.health == CampaignHealth.DEGRADED.value
    assert row.previous_value == 40
    assert row.applied_value == 10
    assert row.applied is True
    assert row.reason == "test decision"
    assert row.evidence["sent"] == 400
    assert row.confidence == 0.5
    assert row.decided_at == NOW


@pytest.mark.asyncio
async def test_the_trail_cannot_be_edited(db_session, sendable) -> None:
    """An audit trail that can be rewritten is not one. The ORM guard is not
    the only defence -- the database refuses raw UPDATE too."""
    from sqlalchemy.exc import IntegrityError

    await _apply(
        sendable.workspace_id,
        sendable.campaign_id,
        [
            proposal(
                Actuation.SET_DAILY_LIMIT, sendable.campaign_id, current=40, proposed=10
            )
        ],
    )
    row = (await _decisions(sendable.workspace_id))[0]

    # RestrictViolation from titan_forbid_mutation, which SQLAlchemy surfaces as
    # IntegrityError -- the database refusing, not the ORM declining.
    with pytest.raises(IntegrityError, match="append-only"):
        async with get_sessionmaker()() as s, s.begin():
            await s.execute(
                update(AutonomyDecision)
                .where(AutonomyDecision.id == row.id)
                .values(reason="something else")
            )


@pytest.mark.asyncio
async def test_a_decision_that_changes_nothing_is_still_recorded(
    db_session, sendable
) -> None:
    """A cycle where the manager considered a campaign and left it alone is a
    fact about the manager, not an absence of one."""
    await _apply(
        sendable.workspace_id,
        sendable.campaign_id,
        [
            proposal(
                Actuation.SET_DAILY_LIMIT, sendable.campaign_id, current=40, proposed=40
            )
        ],
    )

    rows = await _decisions(sendable.workspace_id)
    assert len(rows) == 1
    assert rows[0].applied is False


@pytest.mark.asyncio
async def test_another_workspace_cannot_see_the_trail(db_session, sendable) -> None:
    from titan.db.models import Workspace

    other = Workspace(name="Other", slug=f"o-{uuid.uuid4().hex[:12]}")
    db_session.add(other)
    await db_session.commit()
    other_id = other.id
    try:
        theirs = await build_sendable(db_session, other_id, suffix="autiso")
        await db_session.commit()
        await _apply(
            other_id,
            theirs.campaign_id,
            [
                proposal(
                    Actuation.SET_DAILY_LIMIT, theirs.campaign_id, current=40, proposed=10
                )
            ],
        )

        assert await _decisions(sendable.workspace_id) == []
        assert len(await _decisions(other_id)) == 1
    finally:
        from sqlalchemy import delete

        await db_session.rollback()
        await db_session.execute(delete(Workspace).where(Workspace.id == other_id))
        await db_session.commit()
