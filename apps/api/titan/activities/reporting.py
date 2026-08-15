"""Gathering the week's numbers.

The counting half of :mod:`titan.intelligence.reporting`, which does the
judging. Split so the thresholds -- the part that has to be right -- are tested
against a table of numbers instead of a database.

The report is delivered as an operator notification like everything else, so it
lands in the same task list as the replies and the alerts rather than in a
second place nobody remembers to check.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from titan.db.enums import DraftStatus, MessageState, Region
from titan.db.models import (
    BusinessOpportunity,
    Lead,
    Message,
    MessageDraft,
    Organization,
    Workspace,
)
from titan.db.models.compliance import SuppressionEntry
from titan.db.models.messaging import InboundMessage as InboundMessageRow
from titan.db.models.messaging import ReplyClassification as ReplyClassificationRow
from titan.db.models.ops import Meeting, Task
from titan.db.session import workspace_session, workspace_unit_of_work
from titan.intelligence import lead_sources, portfolio
from titan.intelligence.intent import NEGATIVE_CLASSES, POSITIVE_CLASSES
from titan.intelligence.reporting import (
    WeeklyReport,
    assess_deliverability,
    headline,
    render,
)
from titan.notify.operator import NotificationKind, record_notification
from titan.workflows.types import WeeklyReportInput, WeeklyReportResult

logger = logging.getLogger(__name__)

REPORT_WINDOW = dt.timedelta(days=7)

#: Named on the report so an operator can act without opening the CRM first.
MAX_HOT_LEADS = 8

#: How far back a discovery batch is still worth grading. Much wider than the
#: report window: a batch discovered on Monday has produced almost no outcomes
#: by Friday, and grading only this week's searches would report UNKNOWN for
#: every one of them forever.
SOURCE_LOOKBACK = dt.timedelta(days=90)

#: Sources shown in the report. Ranked worst first, so the truncated tail is the
#: part nobody needed to read.
MAX_SOURCE_LINES = 5


@activity.defn(name="generate_weekly_report")
async def generate_weekly_report(request: WeeklyReportInput) -> WeeklyReportResult:
    """Build the week's report and record it as a notification."""
    workspace_id = uuid.UUID(request.workspace_id)
    now = dt.datetime.now(dt.UTC)
    since = now - REPORT_WINDOW

    async with workspace_session(workspace_id) as session:
        workspace = await session.get(Workspace, workspace_id)
        workspace_name = workspace.name if workspace else str(workspace_id)

        async def count(stmt: Select[tuple[int]]) -> int:
            return int((await session.execute(stmt)).scalar_one())

        sent = await count(
            select(func.count())
            .select_from(Message)
            .where(Message.sent_at >= since, Message.sent_at.is_not(None))
        )
        delivered = await count(
            select(func.count())
            .select_from(Message)
            .where(
                Message.sent_at >= since,
                Message.state.in_(
                    (
                        MessageState.DELIVERED,
                        MessageState.OPENED,
                        MessageState.CLICKED,
                    )
                ),
            )
        )
        bounced = await count(
            select(func.count())
            .select_from(Message)
            .where(Message.sent_at >= since, Message.state == MessageState.BOUNCED)
        )
        complained = await count(
            select(func.count())
            .select_from(Message)
            .where(Message.sent_at >= since, Message.state == MessageState.COMPLAINED)
        )

        leads_discovered = await count(
            select(func.count()).select_from(Lead).where(Lead.created_at >= since)
        )
        leads_researched = await count(
            select(func.count())
            .select_from(Lead)
            .where(Lead.latest_score.is_not(None), Lead.updated_at >= since)
        )

        replies = await count(
            select(func.count())
            .select_from(InboundMessageRow)
            .where(InboundMessageRow.received_at >= since)
        )
        positive = await count(
            select(func.count())
            .select_from(ReplyClassificationRow)
            .where(
                ReplyClassificationRow.created_at >= since,
                ReplyClassificationRow.reply_class.in_(tuple(POSITIVE_CLASSES)),
            )
        )
        declined = await count(
            select(func.count())
            .select_from(ReplyClassificationRow)
            .where(
                ReplyClassificationRow.created_at >= since,
                ReplyClassificationRow.reply_class.in_(tuple(NEGATIVE_CLASSES)),
            )
        )
        suppressions = await count(
            select(func.count())
            .select_from(SuppressionEntry)
            .where(SuppressionEntry.created_at >= since)
        )

        # Open work, not week-bounded: a prospect who said yes three weeks ago
        # and is still waiting is more urgent than one who said yes on Friday,
        # and a window would hide exactly the item that has gone stale.
        awaiting = await count(
            select(func.count())
            .select_from(Task)
            .where(
                Task.status == "open",
                Task.kind == NotificationKind.CLIENT_AGREED.value,
            )
        )
        needs_reading = await count(
            select(func.count())
            .select_from(Task)
            .where(
                Task.status == "open",
                Task.kind == NotificationKind.REPLY_NEEDS_READING.value,
            )
        )
        stalled = await count(
            select(func.count())
            .select_from(Task)
            .where(
                Task.status == "open",
                Task.kind == NotificationKind.CAMPAIGN_STALLED.value,
            )
        )
        awaiting_approval = await count(
            select(func.count())
            .select_from(MessageDraft)
            .where(MessageDraft.status == DraftStatus.AWAITING_APPROVAL)
        )

        meetings_proposed = await count(
            select(func.count()).select_from(Meeting).where(Meeting.created_at >= since)
        )
        # Also unbounded by the window, and for the same reason as the tasks
        # above: a call somebody asked for a fortnight ago and never got a time
        # for is the most embarrassing item this report can contain, and a
        # seven-day window is exactly what would hide it.
        meetings_unscheduled = await count(
            select(func.count())
            .select_from(Meeting)
            .where(Meeting.status == "proposed", Meeting.scheduled_at.is_(None))
        )

        opportunities = await count(
            select(func.count())
            .select_from(BusinessOpportunity)
            .where(
                BusinessOpportunity.created_at >= since,
                BusinessOpportunity.deliverable.is_(True),
            )
        )
        # SUM over no rows is NULL, not zero. COALESCE covers it in the database
        # and ``or 0.0`` covers it in the type checker, which cannot see that.
        summed = (
            await session.execute(
                select(
                    func.coalesce(func.sum(BusinessOpportunity.estimated_value_usd), 0.0)
                ).where(
                    BusinessOpportunity.created_at >= since,
                    BusinessOpportunity.deliverable.is_(True),
                )
            )
        ).scalar_one()
        pipeline_value = float(summed or 0.0)

        hot = (
            await session.execute(
                select(Organization.display_name, Task.created_at)
                .join(Lead, Lead.organization_id == Organization.id)
                .join(Task, Task.lead_id == Lead.id)
                .where(
                    Task.status == "open",
                    Task.kind == NotificationKind.CLIENT_AGREED.value,
                )
                .order_by(Task.created_at.desc())
                .limit(MAX_HOT_LEADS)
            )
        ).all()

        source_windows = await _lead_source_windows(session, workspace_id, now)
        region_slices = await _portfolio_slices(session, workspace_id, since)

    standing = portfolio.summarise(region_slices)
    health = assess_deliverability(sent=sent, bounced=bounced, complained=complained)
    report = WeeklyReport(
        workspace_name=workspace_name,
        period_start=since.date().isoformat(),
        period_end=now.date().isoformat(),
        awaiting_reply=awaiting,
        replies_needing_reading=needs_reading,
        drafts_awaiting_approval=awaiting_approval,
        stalled_campaigns=stalled,
        meetings_unscheduled=meetings_unscheduled,
        leads_discovered=leads_discovered,
        leads_researched=leads_researched,
        messages_sent=sent,
        delivered=delivered,
        bounced=bounced,
        complained=complained,
        replies_received=replies,
        positive_replies=positive,
        declined=declined,
        suppressions_added=suppressions,
        meetings_proposed=meetings_proposed,
        opportunities_identified=opportunities,
        pipeline_value_usd=pipeline_value,
        health=health,
        hot_leads=tuple(
            f"{name} -- waiting since {created.date().isoformat()}"
            for name, created in hot
        ),
        portfolio=tuple(
            (window.region.value, portfolio.describe(window, standing))
            for window in standing.slices
        ),
        lead_sources=tuple(
            (
                window.label or window.kind,
                grade.value,
                lead_sources.explain(window, grade),
            )
            for window, grade in lead_sources.rank(source_windows)[:MAX_SOURCE_LINES]
        ),
    )

    body = render(report)
    async with workspace_unit_of_work(workspace_id) as session:
        await record_notification(
            session,
            workspace_id=workspace_id,
            kind=NotificationKind.WEEKLY_REPORT,
            title=headline(report),
            description=body,
            lead_id=None,
            # One per workspace per ISO week. A retried activity, a redeployed
            # worker or a second cron firing all collapse onto the same row
            # rather than producing a second copy of the same week.
            dedupe_key=f"weekly:{workspace_id}:{now.isocalendar().year}-W{now.isocalendar().week:02d}",
            now=now,
        )

    logger.info(
        "weekly report generated",
        extra={
            "workspace_id": str(workspace_id),
            "sent": sent,
            "replies": replies,
            "health": health.status.value,
        },
    )
    return WeeklyReportResult(
        workspace_id=request.workspace_id,
        headline=headline(report),
        body=body,
        messages_sent=sent,
        replies_received=replies,
        health=health.status.value,
        needs_attention=report.needs_attention,
    )


ALL_REPORTING_ACTIVITIES = [generate_weekly_report]

__all__ = ["ALL_REPORTING_ACTIVITIES", "REPORT_WINDOW", "generate_weekly_report"]


async def _lead_source_windows(
    session: AsyncSession, workspace_id: uuid.UUID, now: dt.datetime
) -> list[lead_sources.LeadSourceWindow]:
    """What each recent discovery batch produced downstream.

    One query rather than one per source: a workspace accumulates a discovery
    batch per campaign cycle, and a report that issued a query each would grow
    slower every week it ran.

    The outcome counts are deliberately *not* windowed to the report period.
    A batch discovered six weeks ago is graded on everything it has produced
    since, because that is what it produced -- clipping the outcomes to the last
    seven days would report a batch as having achieved nothing whenever its
    replies happened to arrive in a different week from the report.

    ``workspace_id`` is written into the SQL rather than left to the session.
    ``workspace_session`` scopes ORM entity queries only and has no effect on
    ``text()``; see the invariant test that enforces this.

    Fails soft and returns nothing. The source standings are one section of a
    report whose other twenty numbers are already gathered; losing them must not
    cost the operator the whole week's summary.
    """
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT ls.id::text                                   AS source_id,
                           ls.kind                                       AS kind,
                           ls.label                                      AS label,
                           ls.estimated_cost_usd                         AS cost_usd,
                           count(DISTINCT l.id)                          AS leads,
                           count(DISTINCT l.id) FILTER (
                               WHERE l.primary_contact_channel_id IS NOT NULL
                           )                                             AS contactable,
                           count(DISTINCT m.lead_id) FILTER (
                               WHERE m.sent_at IS NOT NULL
                           )                                             AS contacted,
                           count(m.id) FILTER (WHERE m.sent_at IS NOT NULL)
                                                                         AS sent,
                           count(m.id) FILTER (WHERE m.delivered_at IS NOT NULL)
                                                                         AS delivered,
                           count(m.id) FILTER (WHERE m.bounced_at IS NOT NULL)
                                                                         AS bounced,
                           count(m.id) FILTER (WHERE m.complained_at IS NOT NULL)
                                                                         AS complained,
                           count(DISTINCT l.id) FILTER (
                               WHERE l.replied_at IS NOT NULL
                           )                                             AS replied
                      FROM lead_sources ls
                      JOIN leads l
                        ON l.lead_source_id = ls.id
                       AND l.workspace_id = ls.workspace_id
                      LEFT JOIN messages m
                        ON m.lead_id = l.id
                       AND m.workspace_id = ls.workspace_id
                     WHERE ls.workspace_id = :workspace
                       AND ls.created_at >= :since
                     GROUP BY ls.id, ls.kind, ls.label, ls.estimated_cost_usd
                    """
                ),
                {"workspace": workspace_id, "since": now - SOURCE_LOOKBACK},
            )
        ).all()
    except Exception as exc:
        logger.warning(
            "lead source standings unavailable; the rest of the report stands",
            extra={"error_code": type(exc).__name__},
        )
        return []

    return [
        lead_sources.LeadSourceWindow(
            source_id=row.source_id,
            kind=row.kind,
            label=row.label or "",
            cost_usd=float(row.cost_usd or 0.0),
            leads=int(row.leads or 0),
            contactable=int(row.contactable or 0),
            contacted=int(row.contacted or 0),
            sent=int(row.sent or 0),
            delivered=int(row.delivered or 0),
            bounced=int(row.bounced or 0),
            complained=int(row.complained or 0),
            replied=int(row.replied or 0),
        )
        for row in rows
    ]


async def _portfolio_slices(
    session: AsyncSession, workspace_id: uuid.UUID, since: dt.datetime
) -> list[portfolio.RegionSlice]:
    """This week's activity, grouped by market.

    Campaign counts are current state and deliberately not windowed: a market
    with three active campaigns that sent nothing this week is the single most
    useful row this view produces, and windowing the campaigns would erase it by
    reporting the region as absent rather than as idle.

    Everything else is windowed to the report period, because share of sending
    is the question -- and a share computed over all time would be a history
    lesson rather than a description of the week.

    Fails soft: the portfolio is one section of a report whose other numbers are
    already gathered.
    """
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.region                                      AS region,
                           count(DISTINCT c.id)                          AS campaigns,
                           count(DISTINCT c.id) FILTER (
                               WHERE c.status = 'active'
                           )                                             AS active_campaigns,
                           count(DISTINCT l.id) FILTER (
                               WHERE l.created_at >= :since
                           )                                             AS leads,
                           count(DISTINCT m.lead_id) FILTER (
                               WHERE m.sent_at >= :since
                           )                                             AS contacted,
                           count(m.id) FILTER (WHERE m.sent_at >= :since) AS sent,
                           count(m.id) FILTER (
                               WHERE m.bounced_at >= :since
                           )                                             AS bounced,
                           count(DISTINCT l.id) FILTER (
                               WHERE l.replied_at >= :since
                           )                                             AS replied
                      FROM campaigns c
                      LEFT JOIN leads l
                        ON l.campaign_id = c.id
                       AND l.workspace_id = c.workspace_id
                      LEFT JOIN messages m
                        ON m.lead_id = l.id
                       AND m.workspace_id = c.workspace_id
                     WHERE c.workspace_id = :workspace
                     GROUP BY c.region
                    """
                ),
                {"workspace": workspace_id, "since": since},
            )
        ).all()
    except Exception as exc:
        logger.warning(
            "portfolio view unavailable; the rest of the report stands",
            extra={"error_code": type(exc).__name__},
        )
        return []

    return [
        portfolio.RegionSlice(
            region=Region(row.region),
            campaigns=int(row.campaigns or 0),
            active_campaigns=int(row.active_campaigns or 0),
            leads=int(row.leads or 0),
            contacted=int(row.contacted or 0),
            sent=int(row.sent or 0),
            bounced=int(row.bounced or 0),
            replied=int(row.replied or 0),
        )
        for row in rows
    ]
