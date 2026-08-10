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

from sqlalchemy import Select, func, select
from temporalio import activity

from titan.db.enums import DraftStatus, MessageState
from titan.db.models import Lead, Message, MessageDraft, Organization, Workspace
from titan.db.models.compliance import SuppressionEntry
from titan.db.models.messaging import InboundMessage as InboundMessageRow
from titan.db.models.messaging import ReplyClassification as ReplyClassificationRow
from titan.db.models.ops import Task
from titan.db.session import workspace_session, workspace_unit_of_work
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

    health = assess_deliverability(sent=sent, bounced=bounced, complained=complained)
    report = WeeklyReport(
        workspace_name=workspace_name,
        period_start=since.date().isoformat(),
        period_end=now.date().isoformat(),
        awaiting_reply=awaiting,
        replies_needing_reading=needs_reading,
        drafts_awaiting_approval=awaiting_approval,
        stalled_campaigns=stalled,
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
        health=health,
        hot_leads=tuple(
            f"{name} -- waiting since {created.date().isoformat()}"
            for name, created in hot
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
