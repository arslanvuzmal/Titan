"""Deciding what a campaign works on this cycle.

Runs against a real PostgreSQL: the budget arithmetic counts rows the outbox
worker wrote, and the follow-up scan writes ``next_action_at`` that the lead
selection then reads back in the same activity.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import replace
from unittest import mock

import pytest
from sqlalchemy import select
from titan.activities.orchestration import (
    _read_fuel,
    _select_leads,
    plan_campaign_cycle,
)
from titan.db.enums import (
    CampaignStatus,
    ContactSource,
    LeadStatus,
    MessageState,
    VerificationStatus,
)
from titan.db.models import (
    Campaign,
    CampaignPolicy,
    Contact,
    ContactChannel,
    Lead,
    Message,
    Organization,
)
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


async def test_todays_sends_are_subtracted_from_the_send_budget(db_session, workspace):
    """The *send* budget still counts today's sends against the daily limit.

    What changed is what that budget governs. It used to gate research too, so
    intake was capped at the send rate and a reserve could never form -- see
    ``titan.intelligence.fuel``. ``remaining_budget`` reaching zero is still
    correct and still reported; it simply no longer stops the pipeline from
    filling the tank for tomorrow.
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

    assert plan.remaining_budget == 0


async def test_a_spent_send_budget_does_not_stop_the_pipeline(db_session, workspace):
    """Planted violation: restore ``min(remaining, max_new_research)`` in the
    planner and this fails.

    Capping research at the sends left today means the intake rate can never
    exceed the send rate, so no reserve can build -- and because only about a
    third of crawled sites yield an address, it drains the pipeline rather than
    holding it level. It also made a bad bounce day cut discovery, since the
    ramp lowers the send limit and the research budget followed it down.
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

    assert plan.remaining_budget == 0, "the send budget is genuinely spent"
    assert plan.verdict != CycleVerdict.BUDGET_SPENT.value, (
        "research must continue while the reserve is short, so there is "
        "something to send tomorrow"
    )


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

    # The send budget, not the verdict: a spent budget no longer ends the cycle,
    # because research keeps running to fill the reserve.
    assert plan.remaining_budget == 0


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


async def test_a_due_follow_up_is_planned_as_the_step_it_is(db_session, workspace):
    """Planted violation: tag the lead "followup" and start it as an opener.

    ``_select_leads`` sorted follow-ups to the front of every cycle and set
    ``kind="followup"`` on them -- a field written in one place and read in
    none. The orchestrator then built ``ResearchLeadInput`` without a step, so
    ``generate_draft`` composed message one. The prioritisation was real and
    the thing it prioritised was a duplicate opener.

    The number carried here is zero-based because zero is the opener;
    ``sequence_steps.step_number`` is one-based because it is a position in a
    list a person wrote. This is the boundary where the two meet.
    """
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        lead.status = LeadStatus.CONTACTED
        lead.last_contacted_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=10)
        lead.next_action_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)

    async with workspace_unit_of_work(workspace) as session:
        planned = await _select_leads(
            session,
            campaign_id=fixture.campaign_id,
            min_score=0,
            limit=10,
            now=dt.datetime.now(dt.UTC),
            # The scan's own numbering: step 2 is the first follow-up.
            due_steps={str(fixture.lead_id): 2},
        )

    followups = [p for p in planned if p.lead_id == str(fixture.lead_id)]
    assert followups, "a lead past its next_action_at is owed a follow-up"
    assert followups[0].kind == "followup"
    assert followups[0].step_number == 1, (
        "sequence step 2 is draft step 1; sending step 0 would re-open with the "
        "message this business already received"
    )


async def test_a_follow_up_the_scan_did_not_reach_waits(db_session, workspace):
    """Fails closed, like every other branch of the sequencing decision.

    ``next_action_at`` says a follow-up is due; it does not say which one. The
    scan is the only caller of ``plan_followup`` and so the only thing that
    knows. Absent that answer the lead is left for the next cycle rather than
    guessed at -- an hour's delay against sending somebody the message they
    already had.
    """
    fixture = await build_sendable(db_session, workspace)

    async with workspace_unit_of_work(workspace) as session:
        lead = await session.get(Lead, fixture.lead_id)
        lead.status = LeadStatus.CONTACTED
        lead.last_contacted_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=10)
        lead.next_action_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)

    async with workspace_unit_of_work(workspace) as session:
        planned = await _select_leads(
            session,
            campaign_id=fixture.campaign_id,
            min_score=0,
            limit=10,
            now=dt.datetime.now(dt.UTC),
            due_steps={},
        )

    assert not [
        p for p in planned if p.lead_id == str(fixture.lead_id) and p.kind == "followup"
    ], "an unmapped lead must not be planned as a follow-up of unknown step"


async def add_reachable_leads(
    session, workspace: uuid.UUID, campaign_id: uuid.UUID, *, count: int
) -> None:
    """Leads holding a published address that nobody has written to yet.

    Exactly what ``titan.intelligence.fuel`` counts as the reserve: an address,
    no suppression, no message. Built here rather than in ``build_sendable``
    because that fixture's lead carries a message and so is, by definition, not
    in the reserve at all.
    """
    for index in range(count):
        tag = uuid.uuid4().hex[:8]
        org = Organization(
            workspace_id=workspace,
            display_name=f"Reserve Business {tag}",
            normalized_name=f"reserve business {tag}",
            canonical_domain=f"reserve-{tag}.test",
        )
        session.add(org)
        await session.flush()
        contact = Contact(
            workspace_id=workspace, organization_id=org.id, full_name="Sam Reserve"
        )
        session.add(contact)
        await session.flush()
        address = f"hello-{tag}@reserve-{tag}.test"
        channel = ContactChannel(
            workspace_id=workspace,
            contact_id=contact.id,
            channel_type="email",
            value=address,
            normalized_value=address,
            value_domain=address.split("@", 1)[1],
            source=ContactSource.FIRST_PARTY_WEBSITE,
            source_url=f"https://reserve-{tag}.test/contact",
            discovered_at=dt.datetime.now(dt.UTC),
            verification_status=VerificationStatus.PUBLISHED_FIRST_PARTY,
            confidence=0.9,
        )
        lead = Lead(
            workspace_id=workspace,
            campaign_id=campaign_id,
            organization_id=org.id,
            status=LeadStatus.QUALIFIED,
            latest_score=88,
        )
        session.add_all([channel, lead])
        await session.flush()
        lead.primary_contact_channel_id = channel.id
    # The planner reads through its own session, so uncommitted rows would be
    # invisible to it and the reserve would read as empty.
    await session.commit()


async def test_a_full_reserve_does_not_stop_the_sending(db_session, workspace):
    """Planted violation: pass ``fuel_budget.leads`` alone as the cycle's
    budget and this fails.

    The mirror of ``test_a_spent_send_budget_does_not_stop_the_pipeline``, and
    the deadlock that one's fix opened. A lead leaves the reserve only when a
    message exists for it, and the only thing that writes a message is a lead
    this planner returned. So a full tank switches off the engine that empties
    it, and nothing ever refills the argument for switching it back on.

    Observed on the live workspace: 2,031 reachable leads against a 1,250-lead
    target, 407 of them awaiting approval, twenty-nine campaigns all reporting
    ``no_work_available`` -- and four consecutive days with no mail sent.
    """
    fixture = await build_sendable(db_session, workspace, daily_send_limit=1)
    # One day's capacity is 1, so RESERVE_DAYS puts the target at 5. Five
    # untouched reachable leads is a reserve that is exactly full.
    await add_reachable_leads(db_session, workspace, fixture.campaign_id, count=5)

    plan = await plan_campaign_cycle(request_for(workspace, fixture.campaign_id))

    assert plan.remaining_budget > 0, "the day's sends are not spent"
    assert plan.verdict == CycleVerdict.READY.value, (
        f"a full reserve must not stop the cycle that drains it: {plan.detail}"
    )
    assert plan.leads, "there are leads with an address and nobody has written to them"


async def test_a_jammed_crawler_bounds_the_send_side_too(db_session, workspace):
    """Planted violation: drop the ``fuel.headroom`` bound and this fails.

    The send budget is a claim on the same crawler the research budget is
    bounded against. Letting it through unbounded is how 1,123 research runs
    failed against 1,242 started -- none of it a crawl going wrong, all of it
    work ordered past what the machine could clear.
    """
    fixture = await build_sendable(db_session, workspace, daily_send_limit=50)
    await add_reachable_leads(db_session, workspace, fixture.campaign_id, count=3)

    async def saturated(session, *, workspace_id):
        real = await _read_fuel(session, workspace_id=workspace_id)
        return replace(real, in_flight=real.queue_ceiling + 10)

    with mock.patch("titan.activities.orchestration._read_fuel", saturated):
        plan = await plan_campaign_cycle(request_for(workspace, fixture.campaign_id))

    assert plan.remaining_budget == 50, "the day's sends are not spent"
    assert plan.verdict == CycleVerdict.NO_WORK_AVAILABLE.value, (
        "a saturated crawler must not be handed more work, however much "
        f"send budget is left: {plan.detail}"
    )
