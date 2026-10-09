"""One answer to the morning question: is cold mail moving, and if not, why?

The overview counts everything ever. The Today section counts sends. Neither
says *why* nothing is going out, and since 3 October the most likely answer is
the placement gate doing its job: no mailbox sends cold mail without a recent
inbox reading, and no reading exists until test inboxes are set up. A paused
estate that does not say why it is paused looks exactly like a broken one.

So this returns, in one round trip:

* every active mailbox with the gate's own verdict -- sending, or paused and
  the sentence why -- computed by the same function the sender uses, never a
  second opinion;
* what the gate needs and whether it has it: test inboxes, warm-up partners;
* what came back this week: replies waiting, and every "seen" signal by grade;
* how the leads grade, A to D.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select, text

from coldops.api.security import Principal, require
from coldops.config import get_settings
from coldops.db.models import Lead, SenderIdentity
from coldops.db.session import workspace_session
from coldops.delivery import placement_gate
from coldops.intelligence.grading import letter_for
from coldops.outreach import reply_desk

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/health", tags=["health"])


class MailboxStatus(BaseModel):
    from_email: str
    sending: bool
    #: Null when sending. Otherwise the gate's code: placement_unmeasured,
    #: placement_below_floor or placement_domain_resting.
    code: str | None
    detail: str
    reach: float | None
    measured: int
    rest_until: dt.datetime | None


class SeenSignal(BaseModel):
    occurred_at: dt.datetime
    kind: str
    grade: str
    reason: str
    business_name: str | None
    lead_id: str | None


class HealthOut(BaseModel):
    gate_enabled: bool
    #: One sentence for the banner: what is happening to cold mail right now.
    cold_mail: str
    cold_mail_moving: bool
    mailboxes: list[MailboxStatus]
    seeds_configured: int
    warmup_enabled: bool
    warmup_partners: int
    message_form: str
    replies_waiting: int
    seen_7d: dict[str, int]
    recent_seen: list[SeenSignal]
    grades: dict[str, int]


def _count_file(loader, path: str | None) -> int:  # type: ignore[no-untyped-def]
    """How many entries a credentials file holds; 0 when absent or unreadable.

    Never raises: a broken file is reported as none, and the screen says what
    that means, rather than the whole dashboard failing over one file.
    """
    if not path:
        return 0
    try:
        return len(list(loader(path)))
    except Exception:
        logger.warning("could not read a credentials file for the health view")
        return 0


def _cold_mail_sentence(
    gate_enabled: bool, seeds: int, statuses: list[MailboxStatus]
) -> tuple[str, bool]:
    sending = [m for m in statuses if m.sending]
    if not statuses:
        return "No active sending mailbox.", False
    if not gate_enabled:
        return (
            "The inbox gate is off: cold mail goes out without checking where it lands.",
            True,
        )
    if sending:
        names = ", ".join(m.from_email for m in sending)
        return f"Cold mail is moving from {names}.", True
    if seeds == 0:
        return (
            "Cold mail is paused: no test inboxes are set up, so no mailbox has an "
            "inbox reading. Replies to people who wrote in still go out.",
            False,
        )
    if all(m.code == placement_gate.UNMEASURED for m in statuses):
        return (
            "Cold mail is paused until the next morning's test emails are read.",
            False,
        )
    return (
        "Cold mail is paused: every mailbox's test emails are landing outside the "
        "inbox. It resumes on its own when they recover.",
        False,
    )


@router.get("", response_model=HealthOut)
async def read_health(
    principal: Principal = Depends(require("research:read")),
) -> HealthOut:
    settings = get_settings()
    now = dt.datetime.now(dt.UTC)
    ws = principal.workspace_id

    from coldops.delivery.mailboxes import load_mailboxes
    from coldops.delivery.seeds import load_seeds

    seeds = _count_file(lambda p: load_seeds(p).all(), settings.seed_file)
    partners = _count_file(
        lambda p: load_mailboxes(p).addresses(), settings.warmup_partner_file
    )

    async with workspace_session(ws) as session:
        active = [
            str(a)
            for a in (
                await session.execute(
                    select(SenderIdentity.from_email).where(SenderIdentity.is_active)
                )
            ).scalars()
        ]
        verdicts = await placement_gate.verdicts_for(
            session, workspace_id=ws, mailboxes=active, now=now
        )
        statuses = [
            MailboxStatus(
                from_email=v.from_email,
                sending=v.may_send or not settings.placement_gate_enabled,
                code=None if v.may_send else v.code,
                detail=v.detail,
                reach=v.reach,
                measured=v.measured,
                rest_until=v.rest_until,
            )
            for v in sorted(verdicts.values(), key=lambda v: v.from_email)
        ]

        replies = len(await reply_desk.waiting(session, workspace_id=ws, limit=200))

        seen_rows = (
            await session.execute(
                text(
                    "SELECT grade, count(*) AS n FROM engagement_events "
                    "WHERE workspace_id = :ws AND occurred_at >= :since GROUP BY grade"
                ),
                {"ws": ws, "since": now - dt.timedelta(days=7)},
            )
        ).all()
        recent_rows = (
            await session.execute(
                text(
                    """
                    SELECT e.occurred_at, e.kind, e.grade, e.reason,
                           o.display_name, e.lead_id
                      FROM engagement_events e
                      LEFT JOIN leads l ON l.id = e.lead_id AND l.workspace_id = :ws
                      LEFT JOIN organizations o
                             ON o.id = l.organization_id AND o.workspace_id = :ws
                     WHERE e.workspace_id = :ws
                       AND e.grade IN ('confirmed', 'likely')
                     ORDER BY e.occurred_at DESC
                     LIMIT 10
                    """
                ),
                {"ws": ws},
            )
        ).all()

        grades: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0, "unscored": 0}
        for score, n in (
            await session.execute(
                select(Lead.latest_score, func.count()).group_by(Lead.latest_score)
            )
        ).all():
            grades[letter_for(score) or "unscored"] += int(n)

    sentence, moving = _cold_mail_sentence(
        settings.placement_gate_enabled, seeds, statuses
    )
    return HealthOut(
        gate_enabled=settings.placement_gate_enabled,
        cold_mail=sentence,
        cold_mail_moving=moving,
        mailboxes=statuses,
        seeds_configured=seeds,
        warmup_enabled=settings.warmup_enabled,
        warmup_partners=partners,
        message_form=settings.message_form,
        replies_waiting=replies,
        seen_7d={str(r.grade): int(r.n) for r in seen_rows},
        recent_seen=[
            SeenSignal(
                occurred_at=r.occurred_at,
                kind=r.kind,
                grade=r.grade,
                reason=r.reason,
                business_name=r.display_name,
                lead_id=str(r.lead_id) if r.lead_id else None,
            )
            for r in recent_rows
        ],
        grades=grades,
    )


__all__ = ["router"]
