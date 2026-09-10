"""Durable operator notifications, with an optional push on top.

Backed by the ``tasks`` table, which was modelled for exactly this ("Operator
work item (e.g. 'high-intent reply needs a response')") and had no writer. No
new table, and the CRM's task list becomes the notification inbox for free.

**Durable first, pushed second, and never the other way round.** A notification
that exists only as a webhook delivery is lost when the webhook is down, the
token has rotated, or the channel was archived -- and it is lost silently, which
is the failure that matters: the operator does not know they missed anything.
So the row is written inside the caller's transaction, and the push is a
best-effort extra that runs *after* the commit.

That ordering also keeps HTTP out of the unit of work (mission section 25). A
webhook to a host having a bad minute would otherwise hold a database
transaction open for its full timeout, on the exact path that fires when
something important happened.

Deduplicated on ``dedupe_key``. A retried activity, a re-read mailbox folder, or
a workflow replay must not produce a second identical alert -- an operator who
is paged twice for one reply stops reading the pages.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.models.ops import Task

logger = logging.getLogger(__name__)


class NotificationKind(StrEnum):
    """Why the operator is being told.

    Stored in ``tasks.kind``. Kept coarse on purpose: a taxonomy fine enough to
    describe every event is one nobody filters on.
    """

    #: A person replied and wants something. The one that matters most.
    CLIENT_AGREED = "client_agreed"
    #: A person replied; intent unclear or mixed. Needs a human read.
    REPLY_NEEDS_READING = "reply_needs_reading"
    #: A person replied saying no. Filed, not urgent, still visible.
    REPLY_DECLINED = "reply_declined"
    #: A conversation is in progress and Titan is drafting into it.
    CONVERSATION_ACTIVE = "conversation_active"
    #: A draft is waiting on a human decision and will expire.
    APPROVAL_NEEDED = "approval_needed"
    #: Deliverability is degrading: bounces or complaints above threshold.
    DELIVERABILITY_ALERT = "deliverability_alert"
    #: A campaign stopped making progress for a reason worth knowing.
    CAMPAIGN_STALLED = "campaign_stalled"
    #: The pipeline itself is failing, running dry, or has quietly stopped.
    #: Every other kind here describes a lead, a campaign or a mailbox; none of
    #: them could say "the machine stopped", which is why every stoppage so far
    #: was found by a person going looking rather than by being told.
    PIPELINE_ALERT = "pipeline_alert"
    #: The weekly summary.
    WEEKLY_REPORT = "weekly_report"


#: Higher sorts first in the CRM. A reply from an interested prospect outranks
#: everything, because it is the only item here with a decaying value: an answer
#: two days late reads as indifference in a way a late report never does.
PRIORITY: dict[NotificationKind, int] = {
    NotificationKind.CLIENT_AGREED: 100,
    NotificationKind.REPLY_NEEDS_READING: 80,
    NotificationKind.CONVERSATION_ACTIVE: 70,
    NotificationKind.DELIVERABILITY_ALERT: 60,
    # Below deliverability, above approvals. A reputation problem is damage
    # already being done; a stalled pipeline is revenue not being made, which
    # is worse over a month and less urgent this hour.
    NotificationKind.PIPELINE_ALERT: 55,
    NotificationKind.APPROVAL_NEEDED: 50,
    NotificationKind.CAMPAIGN_STALLED: 30,
    NotificationKind.WEEKLY_REPORT: 20,
    NotificationKind.REPLY_DECLINED: 10,
}

#: How long before an unanswered item is overdue. Only set where lateness has a
#: real cost; a weekly report is not late.
DUE_WITHIN: dict[NotificationKind, dt.timedelta] = {
    NotificationKind.CLIENT_AGREED: dt.timedelta(hours=4),
    NotificationKind.REPLY_NEEDS_READING: dt.timedelta(hours=12),
    NotificationKind.CONVERSATION_ACTIVE: dt.timedelta(hours=12),
    NotificationKind.DELIVERABILITY_ALERT: dt.timedelta(hours=2),
    NotificationKind.PIPELINE_ALERT: dt.timedelta(hours=8),
    NotificationKind.APPROVAL_NEEDED: dt.timedelta(days=2),
}


@dataclass(frozen=True, slots=True)
class OperatorNotification:
    """A recorded notification, ready to be pushed."""

    task_id: uuid.UUID
    workspace_id: uuid.UUID
    kind: NotificationKind
    title: str
    description: str | None
    lead_id: uuid.UUID | None
    priority: int
    created_at: dt.datetime

    def as_push_text(self) -> str:
        """Plain text for a chat webhook. Deliberately not the full body.

        The description can contain the whole of somebody's reply. Pushing that
        into a chat channel copies a prospect's words into a third-party system
        nobody told them about, so the push carries the headline and points at
        the CRM for the rest.
        """
        return f"[{self.kind.value}] {self.title}"


async def record_notification(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    kind: NotificationKind,
    title: str,
    dedupe_key: str,
    description: str | None = None,
    lead_id: uuid.UUID | None = None,
    priority: int | None = None,
    now: dt.datetime | None = None,
) -> OperatorNotification | None:
    """Write the notification. Returns None when it already existed.

    Call inside the transaction that produced the thing being notified about, so
    the alert and the fact it describes commit together. A notification that
    survives a rolled-back ingest points at a reply that is not there.
    """
    moment = now or dt.datetime.now(dt.UTC)
    rank = priority if priority is not None else PRIORITY.get(kind, 0)
    window = DUE_WITHIN.get(kind)

    inserted = await session.execute(
        pg_insert(Task.__table__)  # type: ignore[arg-type]
        .values(
            workspace_id=workspace_id,
            dedupe_key=dedupe_key[:200],
            lead_id=lead_id,
            title=title[:300],
            description=description,
            kind=kind.value,
            priority=rank,
            status="open",
            due_at=moment + window if window else None,
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "dedupe_key"])
        .returning(Task.__table__.c.id)
    )
    task_id = inserted.scalar_one_or_none()
    if task_id is None:
        logger.debug(
            "notification already recorded", extra={"dedupe_key": dedupe_key[:120]}
        )
        return None

    logger.info(
        "operator notification recorded",
        extra={
            "kind": kind.value,
            "priority": rank,
            "lead_id": str(lead_id) if lead_id else None,
            "workspace_id": str(workspace_id),
        },
    )
    return OperatorNotification(
        task_id=task_id,
        workspace_id=workspace_id,
        kind=kind,
        title=title[:300],
        description=description,
        lead_id=lead_id,
        priority=rank,
        created_at=moment,
    )


#: The notifications that reach the operator by mail the moment they happen.
#:
#: A person replying is the only thing this system produces that is worth
#: interrupting somebody for, and until now nothing did: a reply wrote a row to
#: ``tasks`` and waited to be noticed. The webhook push is the other channel and
#: it has never been configured, so in practice a genuine reply surfaced in the
#: next daily report -- up to a day after the person wrote.
#:
#: All three human classes are here, including the refusal. "A reply from the
#: lead" means a person answered, and being told promptly that somebody said no
#: is worth as much as being told they said yes: it is the difference between
#: closing a thread and wondering about it. Bounces, auto-replies, unsubscribes
#: and every operational alarm stay out -- none of them is a person, and a
#: channel that fires for machines is a channel that gets filtered.
MAILED_INSTANTLY = frozenset(
    {
        NotificationKind.CLIENT_AGREED,
        NotificationKind.REPLY_NEEDS_READING,
        NotificationKind.REPLY_DECLINED,
    }
)


async def mail_notification(notification: OperatorNotification | None) -> bool:
    """Mail the operator about a reply, immediately. Never raises.

    Call *after* the transaction commits, for the reason
    :func:`record_notification` gives: a mail about a reply that was rolled
    back is worse than a late one.

    **Sends at most once per reply, and that is the insert's doing rather than
    this function's.** ``record_notification`` returns ``None`` when the dedupe
    key already existed, so a folder re-read or a provider retry produces no
    notification here and therefore no second mail.

    Unlike :meth:`OperatorNotification.as_push_text`, this carries the body. The
    push withholds it because a chat webhook copies a prospect's words into a
    third-party system nobody told them about; this goes to the operator's own
    mailbox, which is where the reply itself already arrived, so quoting it adds
    no exposure and is the whole point -- an alert that only says "you have a
    reply" is a prompt to go and look, not an answer.
    """
    if notification is None or notification.kind not in MAILED_INSTANTLY:
        return False

    from titan.notify.operator_mail import mail_the_operator

    body = notification.description or "(no body was captured)"
    lead = f"\nLead: {notification.lead_id}" if notification.lead_id else ""
    rule = "-" * min(len(notification.title), 60)
    try:
        await mail_the_operator(
            subject=f"Reply: {notification.title}"[:200],
            body=(
                f"{notification.title}\n{rule}\n\n"
                f"{body}\n{lead}\n"
                f"Received: {notification.created_at:%Y-%m-%d %H:%M UTC}\n"
                f"Task: {notification.task_id}\n"
            ),
        )
    except Exception as exc:
        # Dropped, not retried, and never allowed to reach the caller. The task
        # row is the durable record; this is the fast path on top of it, and an
        # unreachable mail server must not fail an ingest that already
        # succeeded.
        logger.warning(
            "could not mail the operator about a reply; it is still in the CRM",
            extra={
                "error_code": type(exc).__name__,
                "kind": notification.kind.value,
                "task_id": str(notification.task_id),
            },
        )
        return False
    logger.info(
        "operator mailed about a reply",
        extra={"kind": notification.kind.value, "task_id": str(notification.task_id)},
    )
    return True


async def push_notification(notification: OperatorNotification | None) -> bool:
    """Best-effort push to the configured chat webhook. Never raises.

    Call *after* the transaction commits. Returns whether the push landed, which
    callers are free to ignore -- the durable record is the guarantee, and this
    is convenience on top of it.

    A failure here is logged and dropped rather than retried. Retrying a chat
    notification that already arrived is worse than missing one: the row is in
    the CRM either way, and a duplicated 3am alert erodes trust in the channel
    faster than a missing one does.
    """
    if notification is None:
        return False

    from titan.config import get_settings

    settings = get_settings()
    url = settings.operator_webhook_url
    if url is None:
        return False

    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                str(url),
                json={"text": notification.as_push_text()},
            )
        if response.status_code >= 400:
            logger.warning(
                "operator webhook rejected the notification",
                extra={
                    "status_code": response.status_code,
                    "kind": notification.kind.value,
                },
            )
            return False
        return True
    except Exception as exc:
        logger.warning(
            "operator webhook unreachable; the notification is still in the CRM",
            extra={"error_code": type(exc).__name__, "kind": notification.kind.value},
        )
        return False


__all__ = [
    "DUE_WITHIN",
    "MAILED_INSTANTLY",
    "mail_notification",
    "PRIORITY",
    "NotificationKind",
    "OperatorNotification",
    "push_notification",
    "record_notification",
]
