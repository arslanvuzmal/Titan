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

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from titan.config import get_settings
from titan.db.models import (
    MessageDraft,
    ModelRun,
    SenderHealthSnapshot,
    SenderIdentity,
)
from titan.db.session import workspace_unit_of_work
from titan.delivery import adaptive_limits
from titan.delivery.bounces import COUNTS_AGAINST_REPUTATION
from titan.delivery.sender_health import SenderHealth
from titan.intelligence.vitals import check, read_vitals, render
from titan.models.gateway import ModelGateway
from titan.models.providers import build_providers
from titan.notify.operator import NotificationKind, record_notification
from titan.providers import smartlead
from titan.workflows.types import CheckVitalsInput, CheckVitalsResult

logger = logging.getLogger(__name__)


async def _provider_accepts_us() -> bool | None:
    """Whether the sending provider still accepts Titan's credentials.

    ``None`` when there is nothing to ask -- no provider configured, or the
    check itself could not run. A check that did not happen is not evidence of
    failure, and alarming on it would page somebody every time the network
    hiccuped.

    Worth the one network call an hour. The provider refusing us is the only
    fault in this module that stops mail entirely, and it is invisible until a
    send is attempted: Smartlead returned ``401 {"message": "Plan expired!"}``
    for hours while every other number on the dashboard looked healthy, because
    the day's allowance had already been spent before the plan lapsed.
    """
    settings = get_settings()
    if settings.email_provider != "smartlead" or settings.smartlead_api_key is None:
        return None
    client = smartlead.SmartleadClient.from_settings(settings)
    try:
        ok, _detail = await client.health_check()
        return bool(ok)
    except Exception as exc:
        logger.info(
            "sending provider probe did not complete",
            extra={"error_code": type(exc).__name__},
        )
        return None
    finally:
        await client.aclose()


#: How long the model ledger may stay silent while drafts are still being
#: written before the silence is treated as a fault rather than a quiet spell.
#:
#: A day rather than an hour because the ledger is a *usage* record, not a
#: heartbeat: a workspace can legitimately go hours without a call that needs a
#: model. Fifteen days is what it actually took to notice, so anything inside a
#: day is an enormous improvement and a day is long enough that no ordinary
#: lull reaches it.
MODEL_SILENCE_WINDOW = dt.timedelta(hours=24)

#: Line break for the alarm's per-route detail block.
NEWLINE = "\n"


async def _models_answer(
    session: AsyncSession, *, workspace_id: uuid.UUID, now: dt.datetime
) -> tuple[bool | None, str]:
    """Whether the model layer is alive, and what is wrong if it is not.

    Two steps, cheap one first.

    The ledger is the cheap one. ``model_runs`` is written only on success, so
    a row in it is proof a model answered. If drafts have been written in the
    last day and not one of them produced a row, something is wrong -- and that
    query costs nothing, cannot be rate-limited, and is exactly the signal that
    was sitting in the database unread for fifteen days while the last
    successful call (26 August, 08:05 UTC) sat fifty-five minutes on the near
    side of NVIDIA's published end-of-life for two of the routes.

    The live call is the confirmation. Free tiers answer 429 when busy, and an
    alarm that fires on one rate-limited minute is an alarm that gets muted, so
    nothing is raised on the ledger alone: the silence only decides whether it
    is worth spending one small completion to find out.

    Returns ``(None, "")`` when the question cannot be answered -- no provider
    configured, or nothing drafted to be silent about. A check that did not run
    is not evidence of failure.
    """
    settings = get_settings()
    providers = build_providers(settings)
    if not providers:
        return None, ""

    since = now - MODEL_SILENCE_WINDOW
    recent_calls = int(
        await session.scalar(
            select(func.count())
            .select_from(ModelRun)
            .where(ModelRun.workspace_id == workspace_id, ModelRun.created_at >= since)
        )
        or 0
    )
    if recent_calls:
        return True, ""

    drafts = int(
        await session.scalar(
            select(func.count())
            .select_from(MessageDraft)
            .where(
                MessageDraft.workspace_id == workspace_id,
                MessageDraft.created_at >= since,
            )
        )
        or 0
    )
    if not drafts:
        # Nothing asked for a model, so its silence means nothing.
        return None, ""

    gateway = ModelGateway(providers, settings)
    try:
        report = await gateway.validate_models()
    except Exception as exc:
        logger.info(
            "model route probe did not complete",
            extra={"error_code": type(exc).__name__},
        )
        return None, ""

    if report["ok"]:
        # The routes answer, so the silence is something else -- rewrites
        # switched off, or no draft that needed one. Not an alarm.
        return True, ""

    broken = [
        f"  {e['task']:<13} {e.get('provider', '?')}:{e.get('model_id', '?')}"
        + NEWLINE
        + f"      {e.get('detail') or e.get('status', 'failed')}"
        for e in report["routes"]
        if e.get("status") != "ok"
    ]
    return False, NEWLINE.join(broken)


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

        # When each mailbox last hard-bounced, by the same definition the
        # gate and the day report use, so all three agree about what a bounce
        # is. One query rather than one per sender.
        #
        # This is not decoration: ``daily_limit`` grants probation only to a
        # blocked mailbox that has been quiet for PROBATION_QUIET_DAYS, and it
        # reads that from ``days_since_bounce``. Omitting the argument makes it
        # None, which grants nothing -- so a mailbox the gate is letting send
        # its five was reported here as sending zero. Latent while anything has
        # bounced recently, and wrong precisely when somebody is watching a
        # blocked mailbox for signs of recovery.
        last_bounce = {
            row.sender_identity_id: row.bounced_at
            for row in (
                await session.execute(
                    text(
                        "SELECT m.sender_identity_id, max(m.bounced_at) AS bounced_at "
                        "FROM messages m "
                        "WHERE m.workspace_id = :workspace "
                        f"  AND {COUNTS_AGAINST_REPUTATION} "
                        "GROUP BY m.sender_identity_id"
                    ),
                    {"workspace": workspace_id},
                )
            ).all()
        }

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
            bounced_at = last_bounce.get(sender.id)
            decision = adaptive_limits.daily_limit(
                sender.daily_send_limit,
                recent=recent,
                warmup_limit=history[0].warmup_limit if history else None,
                # The argument whose absence made this a second opinion rather
                # than the same computation. None means "never bounced, or
                # nobody looked", and grants no probation -- which is right for
                # a mailbox that really has no bounce behind it and wrong for
                # one that simply was not asked.
                days_since_bounce=(
                    (now - bounced_at).days if bounced_at is not None else None
                ),
            )
            capacity += decision.effective
            if decision.effective > 0:
                sending += 1

        models_ok, models_detail = await _models_answer(
            session, workspace_id=workspace_id, now=now
        )
        vitals = await read_vitals(
            session,
            workspace_id=workspace_id,
            daily_send_capacity=capacity,
            mailboxes_sending=sending,
            sending_provider_ok=await _provider_accepts_us(),
            model_routes_ok=models_ok,
            model_routes_detail=models_detail,
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
