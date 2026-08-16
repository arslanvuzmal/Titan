"""Planning one cycle of a campaign.

The orchestrator workflow decides *what to do with* a plan; this decides what
the plan is. Everything that requires reading the database lives here, because a
workflow body that queried Postgres directly would be non-deterministic on
replay -- the defect that got the pre-0.2 workflows deleted.

The planner is deliberately conservative in one direction: it never plans more
research than the campaign can actually send today. Researching a lead costs a
crawl, a model call and an operator's attention, and produces a draft with an
expiry. Planning two hundred when forty can be sent manufactures a hundred and
sixty drafts that quietly expire unapproved -- spend with nothing at the end of
it, and an approval queue nobody can face.

This is also where :class:`titan.delivery.followup_scheduler.FollowUpScheduler`
finally gets a caller. It was written, tested and never invoked, so
``next_action_at`` was null for every lead in the system and no follow-up was
ever owed.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from titan.autonomy import markets, promotion
from titan.autonomy.actuator import (
    Actuation,
    Bounds,
    Proposal,
    effective_daily_limit,
    effective_min_lead_score,
)
from titan.autonomy.allocation import CampaignDemand, allocate
from titan.autonomy.allocation import explain as explain_share
from titan.autonomy.apply import apply_all
from titan.autonomy.health import CampaignHealth, CampaignWindow
from titan.autonomy.health import classify as classify_campaign
from titan.autonomy.manager import ManagedState, plan
from titan.db.enums import (
    POSITIVE_REPLY_CLASSES,
    TERMINAL_LEAD_STATUSES,
    CampaignStatus,
    LeadStatus,
    MessageState,
)
from titan.db.models import (
    Campaign,
    CampaignPolicy,
    Lead,
    Message,
    Organization,
    Workspace,
)
from titan.db.session import WORKSPACE_KEY, workspace_session, workspace_unit_of_work
from titan.delivery import sender_pool
from titan.delivery.deliverability import ReputationWindow
from titan.delivery.followup_scheduler import FollowUpScheduler
from titan.intelligence.composer import VARIANT_REGISTERS
from titan.intelligence.insights import portfolio_view, variant_comparison
from titan.notify.operator import NotificationKind, record_notification
from titan.workflows.types import (
    CampaignCycleInput,
    CampaignCyclePlan,
    CycleVerdict,
    PlannedLead,
)

logger = logging.getLogger(__name__)

#: The window the manager judges a campaign over. The same trailing thirty
#: days the sender and recipient-domain judgements use.
MANAGER_WINDOW_DAYS = 30

#: Lead statuses that still need the research pipeline run over them. A lead
#: already DRAFTED or QUEUED has a message waiting on a human or on the outbox;
#: re-researching it would produce a second draft for the same person.
RESEARCHABLE_STATUSES = (
    LeadStatus.DISCOVERED,
    LeadStatus.RESEARCHED,
    LeadStatus.QUALIFIED,
)

#: Sends already made today count against the budget even if they failed, since
#: an attempt consumed the quota that the outbox worker reserved.
_SPENT_STATES = (
    MessageState.SENT,
    MessageState.DELIVERED,
    MessageState.OPENED,
    MessageState.BOUNCED,
    MessageState.COMPLAINED,
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@activity.defn(name="plan_campaign_cycle")
async def plan_campaign_cycle(request: CampaignCycleInput) -> CampaignCyclePlan:
    """Decide what this campaign should work on right now.

    Reads authorization from the database rather than trusting anything in the
    request (invariant 18): an orchestrator started weeks ago must not keep
    working a campaign that was paused yesterday.
    """
    workspace_id = uuid.UUID(request.workspace_id)
    campaign_id = uuid.UUID(request.campaign_id)
    now = _now()

    async with workspace_session(workspace_id) as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None:
            return CampaignCyclePlan(
                verdict=CycleVerdict.NOT_AUTHORIZED.value,
                detail=f"campaign {request.campaign_id} not found",
            )

        policy = (
            await session.execute(
                select(CampaignPolicy).where(CampaignPolicy.campaign_id == campaign_id)
            )
        ).scalar_one_or_none()

        blockers = _authorization_blockers(campaign, policy)
        if blockers:
            return CampaignCyclePlan(
                verdict=CycleVerdict.NOT_AUTHORIZED.value,
                detail="; ".join(blockers),
            )
        assert policy is not None  # narrowed by _authorization_blockers

        configured_limit = policy.daily_send_limit
        configured_score = policy.min_lead_score
        managed_limit = policy.managed_daily_send_limit
        managed_score = policy.managed_min_lead_score

        # Midnight UTC, not a rolling 24 hours. The quota engine counts the same
        # way, and two components disagreeing about where "today" starts is how
        # a limit gets silently exceeded around the boundary.
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spent = (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.campaign_id == campaign_id,
                    Message.state.in_(_SPENT_STATES),
                    Message.sent_at >= day_start,
                )
            )
        ).scalar_one()
        outcomes = await _campaign_outcomes(session, campaign_id, now)
        leads_available = await _pool_size(session, campaign_id=campaign_id)

    # The manager runs before the budget is read, so a decision made now governs
    # this cycle rather than the next one. It is deliberately *after* the
    # authorization check above: a campaign that may not send is not a campaign
    # worth tuning, and running the manager on one would fill the audit trail
    # with decisions about mail that was never going out.
    # Capacity is a portfolio question, so it is answered for the whole
    # workspace whenever any part of it cycles. Writing sibling campaigns'
    # limits from this campaign's cycle looks surprising and is the honest
    # shape of the problem: one workspace limit, many campaigns, and no
    # division of it possible from inside any single one.
    await _reallocate_capacity(workspace_id, now)

    effective_limit, effective_score = await _run_manager(
        workspace_id=workspace_id,
        campaign_id=campaign_id,
        status=campaign.status,
        bounds=Bounds(
            configured_daily_limit=configured_limit,
            configured_min_lead_score=configured_score,
        ),
        managed_limit=managed_limit,
        managed_score=managed_score,
        outcomes=outcomes,
        leads_available=leads_available,
        now=now,
    )

    async with workspace_session(workspace_id) as session:
        remaining = max(0, effective_limit - int(spent))
        min_score = effective_score

    if remaining == 0:
        return CampaignCyclePlan(
            verdict=CycleVerdict.BUDGET_SPENT.value,
            remaining_budget=0,
            detail=(
                f"{spent} of {policy.daily_send_limit} sends used today; "
                "the next cycle after midnight UTC will resume"
            ),
        )

    # The follow-up scan writes next_action_at, so it needs a write session and
    # must run before the selection below reads that column.
    async with workspace_unit_of_work(workspace_id) as session:
        scan = await FollowUpScheduler().scan_workspace(session, workspace_id)
    followups_due = sum(1 for result in scan if result.due)

    budget = min(remaining, request.max_new_research)
    async with workspace_session(workspace_id) as session:
        planned = await _select_leads(
            session,
            campaign_id=campaign_id,
            min_score=min_score,
            limit=budget,
            now=now,
        )
        pool = await _pool_size(session, campaign_id=campaign_id)

    if not planned:
        detail = (
            f"no lead qualifies: {followups_due} follow-up(s) due, "
            f"{remaining} send(s) of budget left, minimum score {min_score}"
        )
        async with workspace_unit_of_work(workspace_id) as session:
            await record_notification(
                session,
                workspace_id=workspace_id,
                kind=NotificationKind.CAMPAIGN_STALLED,
                title="Campaign has budget but no eligible leads",
                description=(
                    "The campaign is authorized and has sends left today, and "
                    "found nothing to work on. Usually this means discovery has "
                    "run dry or the score threshold is above what the current "
                    "lead pool reaches.\n\n" + detail
                ),
                lead_id=None,
                # One per campaign per day. A stall persists across every cycle,
                # and an alert per cycle would be an alert an hour until somebody
                # muted the channel.
                dedupe_key=f"stalled:{campaign_id}:{now.date().isoformat()}",
                now=now,
            )
        return CampaignCyclePlan(
            verdict=CycleVerdict.NO_WORK_AVAILABLE.value,
            remaining_budget=remaining,
            followups_due=followups_due,
            detail=detail,
            pool_remaining=pool,
        )

    logger.info(
        "campaign cycle planned",
        extra={
            "campaign_id": str(campaign_id),
            "planned": len(planned),
            "remaining_budget": remaining,
            "followups_due": followups_due,
        },
    )
    return CampaignCyclePlan(
        verdict=CycleVerdict.READY.value,
        leads=tuple(planned),
        remaining_budget=remaining,
        followups_due=followups_due,
        # Net of what this plan is about to consume: the orchestrator is asking
        # "will there be work next cycle", and counting the leads being
        # dispatched right now would answer a different question.
        pool_remaining=max(0, pool - len(planned)),
    )


def _authorization_blockers(
    campaign: Campaign, policy: CampaignPolicy | None
) -> list[str]:
    """Why this campaign may not be worked right now.

    Note what is *not* checked here: the workspace and process send gates. Those
    are re-evaluated by the outbox worker immediately before each provider call,
    and duplicating them would mean two places to keep in step. This gate decides
    whether to spend research effort, which is a lower bar than sending: a
    campaign whose sending is paused may still legitimately want its pipeline
    warm for when it resumes.
    """
    blockers: list[str] = []
    if campaign.status is not CampaignStatus.ACTIVE:
        blockers.append(f"campaign status is {campaign.status.value}, not active")
    if policy is None:
        blockers.append("campaign has no policy row; nothing defines its limits")
    elif policy.daily_send_limit <= 0:
        blockers.append("campaign daily_send_limit is zero")
    return blockers


async def _pool_size(session: AsyncSession, *, campaign_id: uuid.UUID) -> int:
    """How many leads are still available to research for this campaign.

    Deliberately ignores the score threshold. An unscored lead has never been
    measured, and scoring happens *inside* the research pipeline -- so counting
    only leads above the minimum would report a pool of zero for a campaign full
    of freshly discovered work, and trigger discovery that was not needed.
    """
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Lead)
                .where(
                    Lead.campaign_id == campaign_id,
                    Lead.status.in_(RESEARCHABLE_STATUSES),
                    Lead.replied_at.is_(None),
                    Lead.last_contacted_at.is_(None),
                )
            )
        ).scalar_one()
    )


async def _select_leads(
    session: AsyncSession,
    *,
    campaign_id: uuid.UUID,
    min_score: int,
    limit: int,
    now: dt.datetime,
) -> list[PlannedLead]:
    """Choose which leads to work, follow-ups before new ones.

    Ordering is the substance of this function. A lead already contacted and not
    yet replied is warmer than one nobody has written to, and both draw on the
    same daily budget, so spending it on cold outreach while a follow-up is due
    trades a likely reply for an unlikely one.
    """
    if limit <= 0:
        return []

    due_followups = (
        await session.execute(
            select(Lead, Organization.canonical_domain)
            .join(Organization, Organization.id == Lead.organization_id)
            .where(
                Lead.campaign_id == campaign_id,
                Lead.status.notin_(TERMINAL_LEAD_STATUSES),
                Lead.replied_at.is_(None),
                Lead.next_action_at.is_not(None),
                Lead.next_action_at <= now,
            )
            .order_by(Lead.next_action_at)
            .limit(limit)
        )
    ).all()

    planned = [
        PlannedLead(lead_id=str(lead.id), seed_url=_seed_url(domain), kind="followup")
        for lead, domain in due_followups
    ]

    remaining = limit - len(planned)
    if remaining <= 0:
        return planned

    already = {p.lead_id for p in planned}
    fresh = (
        await session.execute(
            select(Lead, Organization.canonical_domain)
            .join(Organization, Organization.id == Lead.organization_id)
            .where(
                Lead.campaign_id == campaign_id,
                Lead.status.in_(RESEARCHABLE_STATUSES),
                Lead.replied_at.is_(None),
                Lead.last_contacted_at.is_(None),
            )
            # Highest score first, then oldest: a lead discovered three weeks
            # ago and never worked is staler evidence than one found today,
            # and stale evidence is what produces a message about a bug the
            # business already fixed.
            .order_by(Lead.latest_score.desc().nullslast(), Lead.created_at)
            .limit(remaining * 2)
        )
    ).all()

    for lead, domain in fresh:
        if len(planned) >= limit:
            break
        if str(lead.id) in already:
            continue
        # An unscored lead is not below threshold -- it has never been measured.
        # Filtering it out here would mean a freshly discovered lead could never
        # be researched, because scoring happens *inside* the research pipeline.
        if lead.latest_score is not None and lead.latest_score < min_score:
            continue
        planned.append(
            PlannedLead(lead_id=str(lead.id), seed_url=_seed_url(domain), kind="new")
        )

    return planned


def _seed_url(domain: str | None) -> str | None:
    if not domain:
        return None
    cleaned = domain.strip().lower()
    if cleaned.startswith(("http://", "https://")):
        return cleaned
    return f"https://{cleaned}"


ALL_ORCHESTRATION_ACTIVITIES = [plan_campaign_cycle]

__all__ = [
    "ALL_ORCHESTRATION_ACTIVITIES",
    "RESEARCHABLE_STATUSES",
    "plan_campaign_cycle",
]


async def _campaign_outcomes(
    session: AsyncSession, campaign_id: uuid.UUID, now: dt.datetime
) -> dict[str, int]:
    """This campaign's recent delivery record, over the reputation window.

    The same trailing thirty days the sender and domain judgements use. A
    campaign judged on a different window from the gate that stops its mail
    would be answering a different question and calling it the same one.
    """
    since = now - dt.timedelta(days=MANAGER_WINDOW_DAYS)
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) FILTER (WHERE sent_at IS NOT NULL)       AS sent,
                       count(*) FILTER (WHERE delivered_at IS NOT NULL)  AS delivered,
                       count(*) FILTER (WHERE bounced_at IS NOT NULL)    AS bounced,
                       count(*) FILTER (WHERE complained_at IS NOT NULL) AS complained,
                       count(DISTINCT lead_id) FILTER (
                           WHERE sent_at IS NOT NULL
                       )                                                 AS contacted
                  FROM messages
                 WHERE workspace_id = :workspace
                   AND campaign_id = :campaign
                   AND created_at >= :since
                """
            ),
            {
                "workspace": session.info.get(WORKSPACE_KEY),
                "campaign": campaign_id,
                "since": since,
            },
        )
    ).one()
    replied = (
        await session.execute(
            select(func.count())
            .select_from(Lead)
            .where(Lead.campaign_id == campaign_id, Lead.replied_at >= since)
        )
    ).scalar_one()

    # Which replies were any good, and which ended in the outcome the whole
    # system exists to produce. Counted separately from ``replied`` rather than
    # replacing it: a campaign that provokes many responses and converts none of
    # them is a specific, diagnosable failure, and collapsing the two numbers
    # into one hides exactly that case.
    quality = (
        await session.execute(
            text(
                """
                SELECT count(DISTINCT l.id) FILTER (
                           WHERE c.reply_class = ANY(CAST(:positive AS reply_class[]))
                       ) AS positive,
                       count(DISTINCT l.id) FILTER (
                           WHERE l.status = 'meeting_booked'
                       ) AS meetings
                  FROM leads l
                  LEFT JOIN inbound_messages i
                         ON i.lead_id = l.id
                        AND i.workspace_id = l.workspace_id
                  LEFT JOIN reply_classifications c
                         ON c.inbound_message_id = i.id
                        AND c.workspace_id = l.workspace_id
                 WHERE l.workspace_id = :workspace
                   AND l.campaign_id = :campaign
                   AND l.replied_at >= :since
                """
            ),
            {
                "workspace": session.info.get(WORKSPACE_KEY),
                "campaign": campaign_id,
                "since": since,
                "positive": [c.value for c in POSITIVE_REPLY_CLASSES],
            },
        )
    ).one()

    return {
        "sent": int(row.sent or 0),
        "delivered": int(row.delivered or 0),
        "bounced": int(row.bounced or 0),
        "complained": int(row.complained or 0),
        "contacted": int(row.contacted or 0),
        "replied": int(replied or 0),
        "positive_replies": int(quality.positive or 0),
        "meetings_booked": int(quality.meetings or 0),
    }


async def _run_manager(
    *,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    status: CampaignStatus,
    bounds: Bounds,
    managed_limit: int | None,
    managed_score: int | None,
    outcomes: dict[str, int],
    leads_available: int,
    now: dt.datetime,
) -> tuple[int, int]:
    """Let the manager adjust this campaign, and return what it may now use.

    Fails soft to the *configured* values, not to the managed ones. If the
    manager cannot run, the campaign falls back to what a human approved rather
    than to whatever the manager last decided -- an autonomous adjustment should
    not outlive the ability to review it.
    """
    effective_limit = effective_daily_limit(bounds.configured_daily_limit, managed_limit)
    effective_score = effective_min_lead_score(
        bounds.configured_min_lead_score, managed_score
    )

    window = CampaignWindow(
        campaign_id=str(campaign_id),
        status=status,
        window=ReputationWindow(
            sent=outcomes["sent"],
            delivered=outcomes["delivered"],
            hard_bounced=outcomes["bounced"],
            complained=outcomes["complained"],
        ),
        contacted=outcomes["contacted"],
        replied=outcomes["replied"],
        positive_replies=outcomes["positive_replies"],
        meetings_booked=outcomes["meetings_booked"],
        configured_limit=bounds.configured_daily_limit,
        effective_limit=effective_limit,
        leads_available=leads_available,
    )
    health = classify_campaign(window)
    state = ManagedState(
        campaign_id=str(campaign_id),
        bounds=bounds,
        managed_daily_limit=managed_limit,
        managed_min_lead_score=managed_score,
    )

    try:
        proposals = plan(state, window)
        if not proposals:
            return effective_limit, effective_score
        async with workspace_unit_of_work(workspace_id) as session:
            verdicts = await apply_all(
                session,
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                health=health,
                proposals=proposals,
                bounds=bounds,
                now=now,
            )
    except Exception as exc:
        logger.warning(
            "campaign manager could not run; the configured limits stand",
            extra={"campaign_id": str(campaign_id), "error_code": type(exc).__name__},
        )
        return bounds.configured_daily_limit, bounds.configured_min_lead_score

    for verdict in verdicts:
        if not verdict.changes_anything:
            continue
        if verdict.proposal.actuation is Actuation.SET_DAILY_LIMIT:
            effective_limit = effective_daily_limit(
                bounds.configured_daily_limit, verdict.applied_value
            )
        elif verdict.proposal.actuation is Actuation.SET_MIN_LEAD_SCORE:
            effective_score = effective_min_lead_score(
                bounds.configured_min_lead_score, verdict.applied_value
            )
    return effective_limit, effective_score


async def _deliverable_budget(
    session: AsyncSession, workspace: Workspace, now: dt.datetime
) -> int:
    """How much the workspace may allocate today: the lower of what a human
    approved and what the mailboxes can actually send.

    The configured limit is a statement of intent. Three mailboxes at fifty a
    day is a hundred and fifty, and it stays a hundred and fifty on the mailboxes'
    first morning, when warm-up will let each of them send five. Dividing the
    intent rather than the reality does not produce extra sends -- the per-mailbox
    warm-up ceiling still refuses them at the gate -- it produces a hundred and
    thirty-five deferrals a day and a set of campaign limits describing volume
    that was never available. Every number downstream is then a plan against
    capacity that does not exist.

    Bounding it here fixes that at the only place it can be fixed: the allocator
    is where a single figure becomes each campaign's share.

    Fails soft *upward*, to the configured limit. That is the behaviour before
    this existed, and it is safe for the same reason the whole function is: the
    warm-up ceiling is enforced independently at send time, so a budget that is
    too generous costs deferrals, never sends.
    """
    configured = workspace.daily_send_limit
    try:
        slots = await sender_pool.load_slots(session, workspace.id, None, now=now)
    except Exception as exc:
        logger.warning(
            "could not read mailbox capacity; the configured limit stands",
            extra={
                "workspace_id": str(workspace.id),
                "error_code": type(exc).__name__,
            },
        )
        return configured

    ceiling = sender_pool.daily_ceiling(slots)
    if ceiling >= configured:
        return configured

    logger.info(
        "daily sending is bounded by mailbox warm-up, not by configuration",
        extra={
            "workspace_id": str(workspace.id),
            "configured_limit": configured,
            "deliverable_today": ceiling,
            "mailboxes": len(slots),
        },
    )
    return ceiling


async def _reallocate_capacity(workspace_id: uuid.UUID, now: dt.datetime) -> None:
    """Divide the workspace's daily sending between the campaigns competing for it.

    Campaign limits were never a division of anything: each has its own, and in
    the live database twenty active campaigns hold a hundred sends a day between
    them against a workspace cap of five. The cap is real and enforced at send
    time, so what happened was that whichever campaign the outbox worker claimed
    from first consumed the whole allowance -- by claim order, not by merit.

    Fails soft and silently. An allocation that cannot be computed leaves every
    campaign on the limit it already had, which is the state the system ran in
    before this existed and is safe by construction: those limits are already
    bounded by the human's configuration.
    """
    try:
        async with workspace_session(workspace_id) as session:
            workspace = await session.get(Workspace, workspace_id)
            if workspace is None:
                return
            rows = (
                await session.execute(
                    select(Campaign, CampaignPolicy)
                    .join(CampaignPolicy, CampaignPolicy.campaign_id == Campaign.id)
                    .where(Campaign.status == CampaignStatus.ACTIVE)
                )
            ).all()
            if not rows:
                return

            # How each market performed, once per cycle rather than per
            # campaign: it is a property of the portfolio, and recomputing it
            # inside the loop would ask the same question eleven times and
            # invite eleven slightly different answers.
            #
            # Returns no-opinion multipliers until at least two markets clear
            # the sample floor, so this is inert on a workspace that has not
            # sent enough to compare anything.
            market_weights = markets.weigh(await portfolio_view(session, now=now))

            demands: list[CampaignDemand] = []
            states: dict[str, tuple[uuid.UUID, CampaignPolicy, CampaignHealth]] = {}
            for campaign, policy in rows:
                outcomes = await _campaign_outcomes(session, campaign.id, now)
                current = effective_daily_limit(
                    policy.daily_send_limit, policy.managed_daily_send_limit
                )
                leads = await _pool_size(session, campaign_id=campaign.id)
                health = classify_campaign(
                    CampaignWindow(
                        campaign_id=str(campaign.id),
                        status=campaign.status,
                        window=ReputationWindow(
                            sent=outcomes["sent"],
                            delivered=outcomes["delivered"],
                            hard_bounced=outcomes["bounced"],
                            complained=outcomes["complained"],
                        ),
                        contacted=outcomes["contacted"],
                        replied=outcomes["replied"],
                        positive_replies=outcomes["positive_replies"],
                        meetings_booked=outcomes["meetings_booked"],
                        configured_limit=policy.daily_send_limit,
                        effective_limit=current,
                        leads_available=leads,
                    )
                )
                demands.append(
                    CampaignDemand(
                        campaign_id=str(campaign.id),
                        health=health,
                        configured_limit=policy.daily_send_limit,
                        leads_available=leads,
                        market_multiplier=markets.multiplier_for(
                            market_weights, campaign.region
                        ),
                    )
                )
                states[str(campaign.id)] = (campaign.id, policy, health)

            budget = await _deliverable_budget(session, workspace, now)
            allocation = allocate(demands, budget)
    except Exception as exc:
        logger.warning(
            "capacity could not be reallocated; existing limits stand",
            extra={"workspace_id": str(workspace_id), "error_code": type(exc).__name__},
        )
        return

    for demand in demands:
        campaign_id, policy, health = states[demand.campaign_id]
        share = allocation.per_campaign.get(demand.campaign_id, 0)
        current = effective_daily_limit(
            policy.daily_send_limit, policy.managed_daily_send_limit
        )
        if share == current:
            continue
        try:
            async with workspace_unit_of_work(workspace_id) as session:
                await apply_all(
                    session,
                    workspace_id=workspace_id,
                    campaign_id=campaign_id,
                    health=health,
                    proposals=[
                        Proposal(
                            actuation=Actuation.SET_DAILY_LIMIT,
                            campaign_id=demand.campaign_id,
                            current=current,
                            proposed=share,
                            reason=f"portfolio allocation: {explain_share(demand, allocation)}",
                            confidence=1.0,
                            evidence={
                                "health": health.value,
                                "workspace_limit": allocation.workspace_limit,
                                "allocated": share,
                                "configured_limit": demand.configured_limit,
                                "leads_available": demand.leads_available,
                                "share_of_allocated": round(
                                    allocation.share_of(demand.campaign_id), 4
                                ),
                                "unallocated": allocation.unallocated,
                            },
                        )
                    ],
                    bounds=Bounds(
                        configured_daily_limit=policy.daily_send_limit,
                        configured_min_lead_score=policy.min_lead_score,
                    ),
                    now=now,
                )
        except Exception as exc:
            logger.warning(
                "could not apply a capacity allocation",
                extra={
                    "campaign_id": str(campaign_id),
                    "error_code": type(exc).__name__,
                },
            )

    # ---- promotion ------------------------------------------------------
    # A separate pass, because the loop above skips any campaign whose capacity
    # did not move and a phrasing decision is not conditional on that.
    #
    # Refusals are applied too. `proposal_for` gives them `proposed == current`,
    # so the actuator writes nothing and `apply_all` still records the row --
    # which is what makes "every refusal to promote" readable months later. Only
    # the case where no comparison was possible produces nothing, because the
    # absence of a question is not a refusal to answer it.
    for demand in demands:
        campaign_id, policy, health = states[demand.campaign_id]
        try:
            async with workspace_unit_of_work(workspace_id) as session:
                comparison = await variant_comparison(
                    session, now=now, campaign_id=campaign_id
                )
                decision = promotion.decide(
                    comparison, currently_promoted=policy.managed_promoted_variant
                )
                proposal = promotion.proposal_for(
                    decision,
                    campaign_id=demand.campaign_id,
                    currently_promoted=policy.managed_promoted_variant,
                    comparison=comparison,
                )
                if proposal is None:
                    continue
                await apply_all(
                    session,
                    workspace_id=workspace_id,
                    campaign_id=campaign_id,
                    health=health,
                    proposals=[proposal],
                    bounds=Bounds(
                        configured_daily_limit=policy.daily_send_limit,
                        configured_min_lead_score=policy.min_lead_score,
                        variant_count=VARIANT_REGISTERS,
                    ),
                    now=now,
                )
        except Exception as exc:
            logger.warning(
                "could not decide a variant promotion",
                extra={
                    "campaign_id": str(campaign_id),
                    "error_code": type(exc).__name__,
                },
            )
