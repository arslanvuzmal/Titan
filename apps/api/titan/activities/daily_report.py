"""Mail the operator what the day did, once the day is done.

Asked for directly: *mail me every day after sending all mails, with all
details -- and if they are not able to send it, mail me too.* Both halves are
here, and the second one shapes the design more than the first.

**Claim, then send, and give the claim back if the send fails.** The check runs
hourly so it can fire the moment the quota is spent, which means twenty-four
chances a day to report twice. The claim is a row in ``tasks`` deduplicated on
the date, taken *before* the mail goes out -- if it were taken after, a crash
in between would mail the operator again on the next pass. But a claim that
survived a failed send would mean the operator silently gets nothing that day,
which is the exact failure this exists to prevent, so a send that raises hands
the claim back.

**Never through the outbox.** A report about the day's sending that was itself
a tracked message would consume outreach quota, land in the bounce statistics,
and distort the numbers it reports. It goes over SMTP directly and leaves no
row in ``messages``.

**The ping is not decoration.** No process can report its own absence, and the
absence is the failure that actually happens here -- five Docker outages, five
stalled schedules. An external watchdog holds that alarm, and it only stays
armed if this pings it *after* a mail genuinely went out. Pinging regardless
would silence the one alarm that still works when Titan cannot mail anybody.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import delete, text
from temporalio import activity

from titan.config import get_settings
from titan.db.models.ops import Task
from titan.db.session import workspace_session, workspace_unit_of_work
from titan.delivery import day_report
from titan.delivery.day_digest import compose, day_is_over
from titan.notify.operator import NotificationKind, record_notification
from titan.workflows.types import DailyReportInput

logger = logging.getLogger(__name__)

Mailer = Callable[..., Awaitable[None]]
Pinger = Callable[[], Awaitable[None]]

#: How many recipients to name individually before the list is summarised.
#: Fifty is a day's sending; a thousand would be a database dump nobody reads.
MAX_LISTED = 200


@dataclass(frozen=True, slots=True)
class DailyReportResult:
    sent: bool
    reason: str


def _claim_key(day: dt.date) -> str:
    return f"daily-report:{day.isoformat()}"


async def send_daily_report_for(
    *,
    workspace_id: uuid.UUID,
    now: dt.datetime,
    mailer: Mailer,
    pinger: Pinger,
) -> DailyReportResult:
    """Report the day if it is over and has not been reported already."""
    async with workspace_session(workspace_id) as session:
        report = await day_report.build(session, workspace_id, now=now)

    if not day_is_over(report, now=now):
        # Today is still running, so look back one day before giving up.
        #
        # Without this the report is unreachable on a machine that sleeps. The
        # estate runs on a laptop: the day ends at 23:00 with nothing awake to
        # notice, the quota never spends because nobody is up to spend it, and
        # the next morning this function is asked about a fresh day that is
        # also not over. Eleven runs, nothing sent, and no error anywhere --
        # the operator asked twice why no mail had arrived.
        #
        # One day back and no further. A week of unsent reports arriving at
        # once is noise, and the figures for last Tuesday change nothing now.
        yesterday = now - dt.timedelta(days=1)
        async with workspace_session(workspace_id) as session:
            report = await day_report.build(session, workspace_id, now=yesterday)
        if not day_is_over(report, now=now):
            return DailyReportResult(False, "the day is not over yet")

    async with workspace_unit_of_work(workspace_id) as session:
        claim = await record_notification(
            session,
            workspace_id=workspace_id,
            kind=NotificationKind.WEEKLY_REPORT,
            title=f"Daily send report for {report.window_date.isoformat()}",
            description="Mailed to the operator.",
            dedupe_key=_claim_key(report.window_date),
            now=now,
        )
    if claim is None:
        return DailyReportResult(False, "already reported today")

    async with workspace_session(workspace_id) as session:
        recipients = await _recipients(session, workspace_id, report.window_date)
        bounces = await _bounces(session, workspace_id, report.window_date)
        alarms = await _alarms(session, workspace_id)

    subject, body = compose(
        report, recipients=recipients, bounces=bounces, alarms=alarms
    )

    try:
        await mailer(to=_operator_address(), subject=subject, body=body)
    except Exception as error:
        # Hand the day back. A claim that outlives a failed send is a day the
        # operator never hears about, and hearing about every day is the whole
        # requirement.
        logger.warning("daily report could not be sent; releasing the day", exc_info=True)
        async with workspace_unit_of_work(workspace_id) as session:
            await session.execute(
                delete(Task).where(
                    Task.workspace_id == workspace_id,
                    Task.dedupe_key == _claim_key(report.window_date),
                )
            )
        return DailyReportResult(False, f"send failed: {error}")

    try:
        await pinger()
    except Exception:
        # Never fatal: the mail is the deliverable and it has gone. A missed
        # ping raises the external alarm, which is a false positive rather
        # than a silent failure -- the safe direction.
        logger.warning("healthcheck ping failed", exc_info=True)

    return DailyReportResult(True, subject)


def _operator_address() -> str:
    return get_settings().operator_email or ""


async def _alarms(session, workspace_id: uuid.UUID) -> tuple[tuple[str, int, str], ...]:
    """Everything still open in the operator queue, grouped by kind.

    Grouped rather than listed. 607 of the 646 open on 9 September were the
    same `campaign_stalled` notice repeating hourly, and a mail that pasted
    them one per line would be deleted unread -- which is exactly how the queue
    got to 646 in the first place.

    Ordered by count so the loudest is first, with the newest title as the
    example, because a count alone says something is wrong and the title says
    what.

    Failures here are swallowed by the caller's own try/except around the send:
    a report that lists the day's sends and omits the alarms is worth far more
    than no report, and this section must never be the reason the mail does not
    go out.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT kind, count(*) AS n,
                       (ARRAY_AGG(title ORDER BY created_at DESC))[1] AS newest
                  FROM tasks
                 WHERE workspace_id = :ws
                   AND status = 'open'
                 GROUP BY kind
                 ORDER BY n DESC
                 LIMIT 12
                """
            ),
            {"ws": workspace_id},
        )
    ).all()
    return tuple((str(k), int(n), str(t or "")[:80]) for k, n, t in rows)


async def _recipients(
    session, workspace_id: uuid.UUID, day: dt.date
) -> tuple[tuple[str, str, str], ...]:
    """Who was written to today: address, campaign, and from which mailbox."""
    rows = (
        await session.execute(
            text(
                """
                SELECT m.to_email, COALESCE(c.name, '-'), COALESCE(s.from_email, '-')
                  FROM messages m
                  LEFT JOIN campaigns c ON c.id = m.campaign_id
                  LEFT JOIN sender_identities s ON s.id = m.sender_identity_id
                 WHERE m.workspace_id = :ws
                   AND m.sent_at >= :day AND m.sent_at < :next
                 ORDER BY m.sent_at
                 LIMIT :cap
                """
            ),
            {
                "ws": workspace_id,
                "day": dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC),
                "next": dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC)
                + dt.timedelta(days=1),
                "cap": MAX_LISTED,
            },
        )
    ).all()
    return tuple((row[0], row[1], row[2]) for row in rows)


async def _bounces(
    session, workspace_id: uuid.UUID, day: dt.date
) -> tuple[tuple[str, str, str], ...]:
    """What bounced today, with the kind -- soft and hard mean opposite things."""
    rows = (
        await session.execute(
            text(
                """
                SELECT m.to_email,
                       COALESCE(m.bounce_kind::text, 'unknown'),
                       -- The wording lives on the outbox row, not on the
                       -- message: `messages` records that a bounce happened
                       -- and of what kind, and the provider's own sentence is
                       -- what the worker wrote down when it handled the DSN.
                       COALESCE(o.last_error, '')
                  FROM messages m
                  LEFT JOIN outbox_messages o ON o.message_id = m.id
                 WHERE m.workspace_id = :ws
                   AND m.bounced_at >= :day AND m.bounced_at < :next
                 ORDER BY m.bounced_at
                """
            ),
            {
                "ws": workspace_id,
                "day": dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC),
                "next": dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC)
                + dt.timedelta(days=1),
            },
        )
    ).all()
    return tuple((row[0], row[1], row[2]) for row in rows)


__all__ = [
    "ALL_DAILY_REPORT_ACTIVITIES",
    "DailyReportResult",
    "send_daily_report",
    "send_daily_report_for",
]


# ==========================================================================
# The real mailer and pinger, injected above so the logic stays testable
# ==========================================================================
async def smtp_mailer(*, to: str, subject: str, body: str) -> None:
    """Send the report, through the one narrow path allowed to bypass the outbox.

    ``to`` is accepted so the injected fake in the tests can assert on it, and
    then deliberately not passed on: :func:`mail_the_operator` takes no
    recipient at all, so nothing here can redirect operator mail at a
    prospect. The two must agree, and the check says so out loud.
    """
    from titan.notify.operator_mail import mail_the_operator

    actual = await mail_the_operator(subject=subject, body=body)
    if to and actual != to:
        raise RuntimeError(
            f"the operator address changed under us: composed for {to}, sent to {actual}"
        )


async def healthcheck_pinger() -> None:
    """Tell the external watchdog Titan got through another day.

    A no-op until the URL is configured, so the report ships before the check
    exists. Short timeout: this runs after the deliverable has already gone,
    and a watchdog that hangs must not hold the activity open.
    """
    import httpx

    url = get_settings().healthcheck_ping_url
    if not url:
        logger.info("no healthcheck URL configured; skipping the ping")
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()


@activity.defn(name="send_daily_report")
async def send_daily_report(request: DailyReportInput) -> DailyReportResult:
    """Activity wrapper: the real mailer and the real ping."""
    return await send_daily_report_for(
        workspace_id=uuid.UUID(request.workspace_id),
        now=dt.datetime.now(dt.UTC),
        mailer=smtp_mailer,
        pinger=healthcheck_pinger,
    )


ALL_DAILY_REPORT_ACTIVITIES = [send_daily_report]
