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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from titan.db.enums import (
    TERMINAL_LEAD_STATUSES,
    CampaignStatus,
    LeadStatus,
    MessageState,
)
from titan.db.models import Campaign, CampaignPolicy, Lead, Message, Organization
from titan.db.session import workspace_session, workspace_unit_of_work
from titan.delivery.followup_scheduler import FollowUpScheduler
from titan.notify.operator import NotificationKind, record_notification
from titan.workflows.types import (
    CampaignCycleInput,
    CampaignCyclePlan,
    CycleVerdict,
    PlannedLead,
)

logger = logging.getLogger(__name__)

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
        remaining = max(0, policy.daily_send_limit - int(spent))
        min_score = policy.min_lead_score

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
