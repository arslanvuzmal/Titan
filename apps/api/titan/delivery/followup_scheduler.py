"""Scans leads and schedules the follow-ups they are owed.

The database half of mission section 13. :mod:`titan.intelligence.sequencing`
decides; this reads the state that decision needs, and writes the result back.

It deliberately stops at ``next_action_at``. Composing the follow-up is the
research pipeline's job, and it must go through the same model gateway,
validator and approval path as a first message -- a scheduler that wrote its own
drafts would be a second, unaudited way to produce outbound text.

So this marks *what is owed and when*. Nothing here can send.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import TERMINAL_LEAD_STATUSES, DraftStatus
from titan.db.models import (
    Campaign,
    CampaignPolicy,
    ContactChannel,
    EmailSequence,
    Lead,
    MessageDraft,
)
from titan.delivery.suppression import is_suppressed
from titan.intelligence.sequencing import (
    FollowUpContext,
    FollowUpPlan,
    Step,
    plan_followup,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScanResult:
    lead_id: uuid.UUID
    due: bool
    step_number: int | None
    reason: str | None


class FollowUpScheduler:
    def __init__(self, *, now_fn: Callable[[], dt.datetime] | None = None) -> None:
        self._now = now_fn or (lambda: dt.datetime.now(dt.UTC))

    async def scan_workspace(
        self, session: AsyncSession, workspace_id: uuid.UUID, *, limit: int = 200
    ) -> list[ScanResult]:
        """Evaluate every contacted lead in a workspace.

        Only leads that have actually been contacted are considered: a
        follow-up follows something, and scanning the whole table would spend
        most of its time re-deciding "never contacted" for discovered leads.
        """
        now = self._now()
        leads = (
            (
                await session.execute(
                    select(Lead)
                    .where(
                        Lead.workspace_id == workspace_id,
                        Lead.last_contacted_at.is_not(None),
                        Lead.replied_at.is_(None),
                    )
                    .order_by(Lead.last_contacted_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

        results: list[ScanResult] = []
        for lead in leads:
            plan = await self._plan_for(session, lead, now)
            # Persisted so the CRM can show why a lead is dormant instead of
            # leaving an operator to infer it from an empty timeline.
            lead.next_action_at = plan.next_action_at
            if plan.skip_reason is not None:
                lead.status_reason = f"{plan.skip_reason.value}: {plan.detail or ''}"[
                    :500
                ]
            results.append(
                ScanResult(
                    lead_id=lead.id,
                    due=plan.due,
                    step_number=plan.step.step_number if plan.step else None,
                    reason=plan.skip_reason.value if plan.skip_reason else None,
                )
            )

        due = sum(1 for r in results if r.due)
        logger.info(
            "follow-up scan complete",
            extra={
                "workspace_id": str(workspace_id),
                "scanned": len(results),
                "due": due,
            },
        )
        return results

    async def _plan_for(
        self, session: AsyncSession, lead: Lead, now: dt.datetime
    ) -> FollowUpPlan:
        campaign = await session.get(Campaign, lead.campaign_id)
        policy = (
            await session.execute(
                select(CampaignPolicy).where(
                    CampaignPolicy.campaign_id == lead.campaign_id
                )
            )
        ).scalar_one_or_none()

        sequence = (
            await session.execute(
                select(EmailSequence)
                .where(
                    EmailSequence.campaign_id == lead.campaign_id,
                    EmailSequence.is_active.is_(True),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

        steps: tuple[Step, ...] = ()
        if sequence is not None:
            steps = tuple(
                Step(
                    id=str(s.id),
                    step_number=s.step_number,
                    delay_days=s.delay_days,
                    template_key=s.template_key,
                    requires_new_evidence=s.requires_new_evidence,
                )
                for s in sorted(sequence.steps, key=lambda s: s.step_number)
            )

        # Steps already represented by a draft, whatever its state: a rejected
        # draft still means that step was attempted, and re-offering it would
        # loop on the reviewer's decision.
        drafts = (
            (
                await session.execute(
                    select(MessageDraft).where(MessageDraft.lead_id == lead.id)
                )
            )
            .scalars()
            .all()
        )
        completed: set[int] = set()
        pending_numbers: set[int] = set()
        by_step_id = {s.id: s for s in steps}
        for draft in drafts:
            if draft.sequence_step_id is None:
                continue
            step = by_step_id.get(str(draft.sequence_step_id))
            if step is None:
                continue
            completed.add(step.step_number)
            if draft.status in (DraftStatus.GENERATED, DraftStatus.APPROVED):
                pending_numbers.add(step.step_number)

        channel = None
        if lead.primary_contact_channel_id is not None:
            channel = await session.get(ContactChannel, lead.primary_contact_channel_id)

        suppressed = False
        if channel is not None and channel.normalized_value:
            suppressed = (
                await is_suppressed(
                    session,
                    workspace_id=lead.workspace_id,
                    email=channel.normalized_value,
                    now=now,
                )
            ) is not None

        remaining = sorted(n for n in {s.step_number for s in steps} - completed)
        next_number = remaining[0] if remaining else None

        return plan_followup(
            FollowUpContext(
                now=now,
                lead_status_is_terminal=lead.status in TERMINAL_LEAD_STATUSES,
                replied_at=lead.replied_at,
                last_contacted_at=lead.last_contacted_at,
                followups_sent=lead.followups_sent,
                max_followups=policy.max_followups if policy else 0,
                sequence_is_active=sequence is not None
                and campaign is not None
                and bool(steps),
                steps=steps,
                completed_step_numbers=frozenset(completed),
                has_eligible_contact=channel is not None and channel.is_active,
                is_suppressed=suppressed,
                draft_pending_for_next_step=next_number in pending_numbers
                if next_number is not None
                else False,
            )
        )


__all__ = ["FollowUpScheduler", "ScanResult"]
