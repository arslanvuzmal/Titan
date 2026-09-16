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
from titan.workflows.types import (
    CheckVitalsInput,
    CheckVitalsResult,
    ExpandMarketsInput,
    ExpandMarketsResult,
    ExpireAlarmsInput,
    ExpireAlarmsResult,
    PingWatchdogInput,
    PingWatchdogResult,
    SweepStaleEvidenceInput,
    SweepStaleEvidenceResult,
)

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


@activity.defn(name="sweep_stale_evidence")
async def sweep_stale_evidence_activity(
    request: SweepStaleEvidenceInput,
) -> SweepStaleEvidenceResult:
    """Return leads with ageing claims to the research pipeline.

    The send gate refuses evidence older than thirty days and nothing acted
    before it, so a draft written on day one sat in the queue until day thirty
    and then became permanently unsendable. Measured the day the estate moved
    to its own server: 167 queued drafts already past that limit, and 255 more
    resting on evidence between two and four weeks old -- each one a claim
    about a defect the business may have fixed a fortnight ago.

    This re-measures rather than relaxing. The lead goes back to QUALIFIED, the
    orchestrator plans it like any other, and the research pipeline writes a new
    draft from what is true today. The old draft is superseded rather than
    deleted: its claim map is the record of what was asserted and why.
    """
    from titan.intelligence.staleness import sweep_stale_evidence

    workspace_id = uuid.UUID(request.workspace_id)
    async with workspace_unit_of_work(workspace_id) as session:
        report = await sweep_stale_evidence(
            session, workspace_id=workspace_id, now=dt.datetime.now(dt.UTC)
        )

    return SweepStaleEvidenceResult(
        stale=report.stale,
        reopened=report.reopened,
        already_unsendable=report.already_unsendable,
        oldest_days=report.oldest_days or 0,
    )


@activity.defn(name="expand_markets")
async def expand_markets(request: ExpandMarketsInput) -> ExpandMarketsResult:
    """Open the next market when the current one is worked out.

    The last decision in lead supply that a person still had to make. Discovery
    runs on a schedule, research runs on a schedule, sending runs on a
    schedule -- and *where to look next* was somebody editing
    ``provision_markets.py``. So when the 29 configured combinations were
    worked out, discovery stopped for eight days and the only complaint was a
    campaign filing "budget but no eligible leads" into a CRM read through a
    daily report that was itself broken.

    Discovery was never exhausted; the query list was. The catalogue holds 107
    territories against 13 business types in use, and 29 of those 1,391
    combinations had been tried.

    Safe to run unattended because of what it creates: a RESEARCH_ONLY campaign
    that is not authorised to send. The worst case of a bug here is crawling a
    city nobody asked for.
    """
    from titan.intelligence.expansion import expand

    workspace_id = uuid.UUID(request.workspace_id)
    async with workspace_unit_of_work(workspace_id) as session:
        report = await expand(session, workspace_id=workspace_id, apply=True)

    return ExpandMarketsResult(
        exhausted=len(report.exhausted),
        opened=tuple(report.opened),
        reason=report.reason,
    )


@activity.defn(name="expire_stale_alarms")
async def expire_stale_alarms_activity(
    request: ExpireAlarmsInput,
) -> ExpireAlarmsResult:
    """Close the machine alarms nobody is going to read. Never a reply.

    ``tasks`` was write-only: 648 rows, every one ``open``, the oldest from
    16 August, 607 of them the same ``campaign_stalled`` alarm. Nothing in the
    codebase had ever written a status other than "open", so the queue that
    exists to be worked could only ever grow -- and a queue nobody can read is
    a queue where the sixteen genuine replies sitting in it are invisible.

    The split between what expires and what does not is in
    ``titan.notify.task_expiry``; the short version is that an alarm re-fires
    while it is still true and a person does not.
    """
    from titan.notify.task_expiry import expire_stale_alarms

    workspace_id = uuid.UUID(request.workspace_id)
    async with workspace_unit_of_work(workspace_id) as session:
        report = await expire_stale_alarms(
            session, workspace_id=workspace_id, now=dt.datetime.now(dt.UTC)
        )
    return ExpireAlarmsResult(expired=report.expired, still_open=report.still_open)


@activity.defn(name="ping_watchdog")
async def ping_watchdog(_: PingWatchdogInput) -> PingWatchdogResult:
    """Tell an external watchdog the stack is still up. Hourly.

    **Titan cannot report that Titan is down.** Every alarm in this module runs
    inside the process it is watching, so the one failure mode none of them can
    reach is the machine being off -- which is the failure mode that has
    actually happened. Nothing went out on 4, 5 or 6 September and nobody knew
    until somebody looked.

    A ping already existed, attached to the daily report. That is the right
    signal for "the report went out" and the wrong one for "the stack is up":
    it fires once a day, so the detection window is about twenty-six hours, and
    a three-day outage is still two days old before anyone hears. This runs on
    the hourly housekeeping pass instead, which takes the window to roughly two
    hours with no new schedule and no new machinery.

    Both pings may point at the same check. Extra pings never trip a dead man's
    switch -- only their absence does -- so the daily one becomes a harmless
    second heartbeat rather than something to remove.

    A no-op until ``TITAN_HEALTHCHECK_PING_URL`` is set, which is the shipped
    state: the wiring lands before the URL exists, so turning it on later is a
    configuration change and not a deployment.
    """
    from titan.activities.daily_report import healthcheck_pinger

    if not get_settings().healthcheck_ping_url:
        return PingWatchdogResult(pinged=False, reason="no url configured")
    try:
        await healthcheck_pinger()
    except Exception as exc:
        # Swallowed here as well as by the workflow. A watchdog that cannot be
        # reached is a watchdog problem; failing the housekeeping pass over it
        # would let a monitoring outage stop the repairs being monitored.
        logger.warning("watchdog ping failed: %s", str(exc)[:200])
        return PingWatchdogResult(pinged=False, reason=f"{type(exc).__name__}")
    return PingWatchdogResult(pinged=True)


ALL_VITALS_ACTIVITIES = [
    check_pipeline_vitals,
    expand_markets,
    sweep_stale_evidence_activity,
    expire_stale_alarms_activity,
    ping_watchdog,
]

__all__ = ["ALL_VITALS_ACTIVITIES", "check_pipeline_vitals"]
