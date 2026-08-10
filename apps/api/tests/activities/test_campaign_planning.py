"""Deciding what a campaign works on this cycle.

Runs against a real PostgreSQL: the budget arithmetic counts rows the outbox
worker wrote, and the follow-up scan writes ``next_action_at`` that the lead
selection then reads back in the same activity.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from titan.activities.orchestration import plan_campaign_cycle
from titan.db.enums import CampaignStatus, LeadStatus, MessageState
from titan.db.models import Campaign, CampaignPolicy, Lead, Message
from titan.db.models.ops import Task
from titan.db.session import workspace_unit_of_work
from titan.workflows.types import CampaignCycleInput, CycleVerdict

from tests.delivery.conftest import build_sendable

pytestmark = pytest.mark.asyncio


def request_for(workspace: uuid.UUID, campaign: uuid.UUID) -> CampaignCycleInput:
    return CampaignCycleInput(
        workspace_id=str(workspace),
        campaign_id=str(campaign),
        cycle_key=f"{campaign}:0",
        max_new_research=25,
    )


async def test_a_qualified_lead_is_planned(db_session, workspace):
    fixture = await build_sendable(db_session, workspace)

    plan = await plan_campaign_cycle(request_for(workspace, fixture.campaign_id))

    assert plan.verdict == CycleVerdict.READY.value
    assert [lead.lead_id for lead in plan.leads] == [str(fixture.lead_id)]
    assert plan.leads[0].kind == "new"
    # Derived from the organisation's canonical domain, so the research crawl
    # has somewhere to start without another query.
    assert plan.leads[0].seed_url is not None


async def test_a_paused_campaign_plans_nothing(db_session, workspace):
    """Authorization is read at execution time, never taken from the request.

    An orchestrator started weeks ago must not keep working a campaign that was
    paused yesterday (invariant 18).
    """
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        campaign = await session.get(Campaign, fixture.campaign_id)
        campaign.status = CampaignStatus.PAUSED

    plan = await plan_campaign_cycle(request_for(workspace, fixture.campaign_id))

    assert plan.verdict == CycleVerdict.NOT_AUTHORIZED.value
    assert plan.leads == ()
    assert "paused" in (plan.detail or "")


async def test_a_missing_campaign_is_refused_rather_than_crashing(db_session, workspace):
    """An orchestrator outlives the campaign it was started for.

    Raising here would fail the activity, exhaust its retries and leave the
    workflow logging an error every cycle forever.
    """
    plan = await plan_campaign_cycle(request_for(workspace, uuid.uuid4()))

    assert plan.verdict == CycleVerdict.NOT_AUTHORIZED.value
    assert "not found" in (plan.detail or "")


async def test_todays_sends_are_subtracted_from_the_budget(db_session, workspace):
    """Research is planned against what can still be sent, not the raw limit.

    Planning two hundred when forty can be sent manufactures a hundred and sixty
    drafts that expire unapproved: model spend with nothing at the end of it.
    """
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        policy = (
            await session.execute(
                select(CampaignPolicy).where(
                    CampaignPolicy.campaign_id == fixture.campaign_id
                )
            )
        ).scalar_one()
        policy.daily_send_limit = 1

        message = await session.get(Message, fixture.message_id)
        message.state = MessageState.SENT
        message.sent_at = dt.datetime.now(dt.UTC)

    plan = await plan_campaign_cycle(request_for(workspace, fixture.campaign_id))

    assert plan.verdict == CycleVerdict.BUDGET_SPENT.value
    assert plan.remaining_budget == 0
    assert plan.leads == ()


async def test_a_send_from_yesterday_does_not_count_against_today(db_session, workspace):
    """The budget window is midnight UTC, matching the quota engine.

    Two components disagreeing about where "today" starts is how a daily limit
    gets silently exceeded around the boundary.
    """
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        policy = (
            await session.execute(
                select(CampaignPolicy).where(
                    CampaignPolicy.campaign_id == fixture.campaign_id
                )
            )
        ).scalar_one()
        policy.daily_send_limit = 1

        message = await session.get(Message, fixture.message_id)
        message.state = MessageState.SENT
        message.sent_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=2)

    plan = await plan_campaign_cycle(request_for(workspace, fixture.campaign_id))

    assert plan.verdict == CycleVerdict.READY.value
    assert plan.remaining_budget == 1


async def test_a_bounced_send_still_counts_against_the_budget(db_session, workspace):
    """The attempt consumed the quota the outbox worker reserved.

    Refunding failed sends would let a campaign with a bad list send several
    times its daily limit -- while bouncing, which is the worst possible way to
    spend a sending reputation.
    """
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        policy = (
            await session.execute(
                select(CampaignPolicy).where(
                    CampaignPolicy.campaign_id == fixture.campaign_id
                )
            )
        ).scalar_one()
        policy.daily_send_limit = 1

        message = await session.get(Message, fixture.message_id)
        message.state = MessageState.BOUNCED
        message.sent_at = dt.datetime.now(dt.UTC)

    plan = await plan_campaign_cycle(request_for(workspace, fixture.campaign_id))

    assert plan.verdict == CycleVerdict.BUDGET_SPENT.value


async def test_a_replied_lead_is_never_planned(db_session, workspace):
    """Invariant 15 at the planning layer.

    The outbox worker would refuse the send anyway, but researching and drafting
    to somebody who already answered wastes a crawl and a model call to produce
    a message that must not go out.
    """
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        lead.replied_at = dt.datetime.now(dt.UTC)
        lead.status = LeadStatus.REPLIED

    plan = await plan_campaign_cycle(request_for(workspace, fixture.campaign_id))

    assert plan.verdict == CycleVerdict.NO_WORK_AVAILABLE.value
    assert plan.leads == ()


async def test_an_unscored_lead_is_not_treated_as_below_threshold(db_session, workspace):
    """Scoring happens *inside* the research pipeline.

    Filtering unscored leads out here would mean a freshly discovered lead could
    never be researched, so it could never be scored, so it would never qualify
    -- and discovery would silently produce nothing usable forever.
    """
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        lead.latest_score = None
        lead.status = LeadStatus.DISCOVERED

    plan = await plan_campaign_cycle(request_for(workspace, fixture.campaign_id))

    assert plan.verdict == CycleVerdict.READY.value
    assert [lead.lead_id for lead in plan.leads] == [str(fixture.lead_id)]


async def test_a_scored_lead_below_the_threshold_is_skipped(db_session, workspace):
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        lead.latest_score = 10
        lead.status = LeadStatus.QUALIFIED

    plan = await plan_campaign_cycle(request_for(workspace, fixture.campaign_id))

    assert plan.verdict == CycleVerdict.NO_WORK_AVAILABLE.value


async def test_a_stalled_campaign_notifies_once_per_day(db_session, workspace):
    """Budget available and nothing to do is worth knowing about.

    The campaign looks alive in every dashboard and is doing nothing -- usually
    discovery has run dry. But the stall persists across every cycle, so an
    alert per cycle would be an alert an hour until somebody muted the channel.
    """
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        lead.status = LeadStatus.ARCHIVED

    for _ in range(3):
        plan = await plan_campaign_cycle(request_for(workspace, fixture.campaign_id))
        assert plan.verdict == CycleVerdict.NO_WORK_AVAILABLE.value

    async with workspace_unit_of_work(workspace) as session:
        tasks = (await session.execute(select(Task))).scalars().all()
        stalls = [t for t in tasks if t.kind == "campaign_stalled"]
        assert len(stalls) == 1


async def test_planning_runs_the_follow_up_scan(db_session, workspace):
    """FollowUpScheduler was written, tested and never invoked.

    next_action_at was null for every lead in the system, so no follow-up was
    ever owed and the nurture half of the pipeline did nothing. Planning is the
    caller it was missing.
    """
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        lead.status = LeadStatus.CONTACTED
        lead.last_contacted_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=5)
        lead.next_action_at = None

    await plan_campaign_cycle(request_for(workspace, fixture.campaign_id))

    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        # Either a due date or a recorded reason for there not being one. Null
        # with no explanation is the state that meant nothing was ever owed.
        assert lead.next_action_at is not None or lead.status_reason is not None
