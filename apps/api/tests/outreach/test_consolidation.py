"""Merging city campaigns into business-type ones, without losing a lead.

The plan is read-only and the move is not. What these fix is the boundary
between them, and the one property that makes the move safe to run on a live
workspace: everything a campaign owns travels together, or nothing does.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text
from titan.db.enums import CampaignStatus, Industry
from titan.db.models import Campaign, CampaignPolicy, Workspace
from titan.outreach.consolidation import (
    KEPT_TABLES,
    MOVED_TABLES,
    Move,
    apply_move,
    build_plan,
    vertical_name,
)

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------- pure, no db


def test_a_vertical_is_named_as_a_population_not_a_place() -> None:
    assert vertical_name(Industry.DENTIST) == "Dentists"
    assert vertical_name(Industry.HVAC_HOME_SERVICES) == "HVAC and home services"


def test_an_unnamed_industry_still_gets_a_serviceable_name() -> None:
    """An industry added later should not stop a consolidation."""
    assert vertical_name(Industry.GENERAL)


def test_the_campaigns_own_tables_are_not_moved() -> None:
    """Planted violation: move campaign_policies and the survivor's own budget,
    approval rule and score threshold are overwritten by whichever absorbed
    campaign was updated last."""
    assert not set(MOVED_TABLES) & set(KEPT_TABLES)
    for table in ("campaign_policies", "campaign_senders", "email_sequences"):
        assert table not in MOVED_TABLES


def test_the_lead_and_everything_that_points_at_it_move_together() -> None:
    """A draft left behind belongs to a campaign that is no longer its lead's,
    and every gate reading policy from the campaign then reads the wrong one."""
    for table in ("leads", "message_drafts", "outbox_messages", "messages"):
        assert table in MOVED_TABLES


async def test_a_refused_move_cannot_be_applied(db_session) -> None:
    """The blockers are not advisory.

    ``apply_move`` raises before touching the session, so a caller that reads
    the plan and ignores the refusal cannot move anything by accident.
    """
    move = Move(
        industry=Industry.DENTIST,
        survivor_id=uuid.uuid4(),
        survivor_name="Dentists",
        absorbed=((uuid.uuid4(), "Dentists Leeds UK"),),
        leads=10,
        drafts=2,
        blockers=("3 message(s) are leased by a sending worker right now",),
    )

    assert not move.ok
    with pytest.raises(ValueError, match="refused"):
        await apply_move(db_session, move, workspace_id=uuid.uuid4())


# ------------------------------------------------------------------ with db


async def _campaign(session, workspace_id, *, name, industry, created) -> Campaign:
    campaign = Campaign(
        workspace_id=workspace_id,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
        status=CampaignStatus.ACTIVE,
        industry=industry,
        created_at=created,
    )
    session.add(campaign)
    await session.flush()
    session.add(CampaignPolicy(workspace_id=workspace_id, campaign_id=campaign.id))
    await session.flush()
    return campaign


async def test_the_campaign_with_the_most_leads_survives(db_session, workspace) -> None:
    """So the fewest rows move. Measured, not chosen by name or by age."""
    now = dt.datetime.now(dt.UTC)
    big = await _campaign(
        db_session,
        workspace,
        name="Dentists Leeds UK",
        industry=Industry.DENTIST,
        created=now,
    )
    small = await _campaign(
        db_session,
        workspace,
        name="Dentists, Warsaw",
        industry=Industry.DENTIST,
        created=now - dt.timedelta(days=30),
    )
    await _leads(db_session, workspace, big.id, 5)
    await _leads(db_session, workspace, small.id, 1)
    await db_session.commit()

    plan = await build_plan(db_session, workspace_id=workspace)
    move = next(m for m in plan.moves if m.industry is Industry.DENTIST)

    assert move.survivor_id == big.id
    assert [name for _, name in move.absorbed] == ["Dentists, Warsaw"]
    assert move.leads == 6


async def test_a_lone_campaign_is_left_alone(db_session, workspace) -> None:
    """Nothing to merge is not the same as nothing to do, and the plan says so
    rather than marking a single campaign as spanning markets behind the
    operator's back."""
    await _campaign(
        db_session,
        workspace,
        name="Gyms Manchester UK",
        industry=Industry.GYM_FITNESS,
        created=dt.datetime.now(dt.UTC),
    )
    await db_session.commit()

    plan = await build_plan(db_session, workspace_id=workspace)

    assert plan.actionable == []


async def test_the_move_carries_the_leads_and_renames_the_survivor(
    db_session, workspace
) -> None:
    now = dt.datetime.now(dt.UTC)
    survivor = await _campaign(
        db_session,
        workspace,
        name="Dentists Leeds UK",
        industry=Industry.DENTIST,
        created=now,
    )
    absorbed = await _campaign(
        db_session,
        workspace,
        name="Dentists, Sydney",
        industry=Industry.DENTIST,
        created=now,
    )
    # The survivor is whichever holds the most, so the naming here has to match
    # that rule rather than the other way round.
    await _leads(db_session, workspace, survivor.id, 5)
    await _leads(db_session, workspace, absorbed.id, 3)
    await db_session.commit()

    plan = await build_plan(db_session, workspace_id=workspace)
    move = next(m for m in plan.actionable if m.industry is Industry.DENTIST)
    assert move.survivor_id == survivor.id
    moved = await apply_move(db_session, move, workspace_id=workspace)
    await db_session.commit()

    assert moved["leads"] == 3
    after = await db_session.get(Campaign, survivor.id)
    await db_session.refresh(after)
    assert after.name == "Dentists"
    assert after.spans_all_markets is True

    gone = await db_session.get(Campaign, absorbed.id)
    await db_session.refresh(gone)
    assert gone.status is CampaignStatus.PAUSED, "absorbed campaigns are paused"

    remaining = (
        await db_session.execute(
            text("SELECT count(*) FROM leads WHERE campaign_id = :cid"),
            {"cid": absorbed.id},
        )
    ).scalar_one()
    assert remaining == 0


async def test_another_workspaces_rows_are_never_touched(db_session, workspace) -> None:
    """Planted violation: drop the workspace_id predicate from the UPDATE and a
    consolidation in one tenant reassigns another tenant's leads.

    Worth its own test because these are raw statements: workspace isolation
    here is an ORM guard, and raw SQL does not get it.
    """
    other = Workspace(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.flush()

    now = dt.datetime.now(dt.UTC)
    survivor = await _campaign(
        db_session, workspace, name="A", industry=Industry.DENTIST, created=now
    )
    absorbed = await _campaign(
        db_session, workspace, name="B", industry=Industry.DENTIST, created=now
    )
    await _leads(db_session, workspace, survivor.id, 2)
    await _leads(db_session, workspace, absorbed.id, 1)

    # Same campaign id is impossible across tenants, so the isolation is proved
    # the way it can actually fail: a foreign workspace's rows must be untouched
    # by an UPDATE that names only campaign ids.
    foreign = await _campaign(
        db_session, other.id, name="C", industry=Industry.DENTIST, created=now
    )
    await _leads(db_session, other.id, foreign.id, 4)
    await db_session.commit()

    plan = await build_plan(db_session, workspace_id=workspace)
    move = next(m for m in plan.actionable if m.industry is Industry.DENTIST)
    await apply_move(db_session, move, workspace_id=workspace)
    await db_session.commit()

    still_there = (
        await db_session.execute(
            text("SELECT count(*) FROM leads WHERE campaign_id = :cid"),
            {"cid": foreign.id},
        )
    ).scalar_one()
    assert still_there == 4

    await db_session.execute(
        text("DELETE FROM workspaces WHERE id = :wid"), {"wid": other.id}
    )
    await db_session.commit()


async def _leads(session, workspace_id, campaign_id, count: int) -> None:
    from titan.db.models import Lead, Organization

    for index in range(count):
        label = f"Org {uuid.uuid4().hex[:8]}-{index}"
        org = Organization(
            workspace_id=workspace_id,
            display_name=label,
            normalized_name=label.lower(),
        )
        session.add(org)
        await session.flush()
        session.add(
            Lead(
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                organization_id=org.id,
            )
        )
    await session.flush()
