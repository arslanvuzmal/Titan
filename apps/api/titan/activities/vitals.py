"""Take the pipeline's pulse each hour and say so when it is bad.

:mod:`titan.intelligence.vitals` decides what counts as bad. This reads the
numbers, records what it finds, and stops.

**Deduplicated per day, per alarm.** ``campaign_stalled`` has fired 180 times on
the live workspace, which is 180 rows describing one situation, and an operator
who is told the same thing every hour learns to close the tab. One row per code
per day says the same thing once and keeps saying it tomorrow if it is still
true -- which is the behaviour that survives being read every morning.

**Capacity is asked for, not assumed.** How many messages a mailbox may send
today is the outbox worker's answer, arrived at from warm-up position, health
and the configured ceiling. Recomputing it here would be a second opinion free
to disagree with the one that actually governs sending, and the dashboard would
then be arguing with the gate.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import select
from temporalio import activity

from titan.db.models import SenderHealthSnapshot, SenderIdentity
from titan.db.session import workspace_unit_of_work
from titan.delivery import adaptive_limits
from titan.delivery.sender_health import SenderHealth
from titan.intelligence.vitals import check, read_vitals, render
from titan.notify.operator import NotificationKind, record_notification
from titan.workflows.types import CheckVitalsInput, CheckVitalsResult

logger = logging.getLogger(__name__)


@activity.defn(name="check_pipeline_vitals")
async def check_pipeline_vitals(request: CheckVitalsInput) -> CheckVitalsResult:
    """Read the six numbers, raise what they justify."""
    workspace_id = uuid.UUID(request.workspace_id)
    now = dt.datetime.now(dt.UTC)
    day = now.date().isoformat()

    async with workspace_unit_of_work(workspace_id) as session:
        senders = (
            (
                await session.execute(
                    select(SenderIdentity).where(
                        SenderIdentity.workspace_id == workspace_id,
                        SenderIdentity.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )

        capacity = 0
        sending = 0
        for sender in senders:
            # The snapshots the outbox worker itself wrote, run back through the
            # same function it uses. Not a second opinion: the same computation
            # over the same recorded facts. Recomputing warm-up position or
            # health here would be a number free to disagree with the one that
            # actually governs sending.
            history = (
                (
                    await session.execute(
                        select(SenderHealthSnapshot)
                        .where(
                            SenderHealthSnapshot.workspace_id == workspace_id,
                            SenderHealthSnapshot.sender_identity_id == sender.id,
                        )
                        .order_by(SenderHealthSnapshot.captured_on.desc())
                        .limit(adaptive_limits.RECOVERY_LOOKBACK_DAYS)
                    )
                )
                .scalars()
                .all()
            )
            recent = tuple(SenderHealth(row.status) for row in history)
            decision = adaptive_limits.daily_limit(
                sender.daily_send_limit,
                recent=recent,
                warmup_limit=history[0].warmup_limit if history else None,
            )
            capacity += decision.effective
            if decision.effective > 0:
                sending += 1

        vitals = await read_vitals(
            session,
            workspace_id=workspace_id,
            daily_send_capacity=capacity,
            mailboxes_sending=sending,
            now=now,
        )

        alarms = check(vitals)
        raised: list[str] = []
        suppressed: list[str] = []
        for alarm in alarms:
            written = await record_notification(
                session,
                workspace_id=workspace_id,
                kind=NotificationKind.PIPELINE_ALERT,
                title=alarm.title,
                description=alarm.detail,
                # One per code per day. Same fault tomorrow raises again;
                # same fault an hour later does not.
                dedupe_key=f"vitals:{alarm.code}:{day}",
                now=now,
            )
            (raised if written is not None else suppressed).append(alarm.code)

    # Logged whether or not anything fired. A health check that is silent when
    # healthy is indistinguishable from one that has stopped running, which is
    # the failure this whole module exists to make impossible.
    reading = render(vitals)
    logger.info(
        "pipeline vitals\n%s",
        reading,
        extra={
            "workspace_id": str(workspace_id),
            "alarms": raised,
            "suppressed": suppressed,
        },
    )

    return CheckVitalsResult(
        leads_in_hand=vitals.leads_in_hand,
        days_of_fuel=vitals.days_of_fuel,
        sends_today=vitals.sends_today,
        daily_send_capacity=vitals.daily_send_capacity,
        research_failure_rate=vitals.research_failure_rate,
        alarms=tuple(raised),
        suppressed=tuple(suppressed),
        reading=reading,
    )


ALL_VITALS_ACTIVITIES = [check_pipeline_vitals]

__all__ = ["ALL_VITALS_ACTIVITIES", "check_pipeline_vitals"]
