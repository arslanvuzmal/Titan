"""Writing down each sending identity's health once a day, so a trend exists.

``sender_health.py`` classifies a snapshot and can tell an improving sender from
a degrading one -- ``Trend`` is one of its types. ``sender_health_snapshots``
has held the shape for that since 15 August and has never held a row, so the
trend had nothing to compare and every judgement about a mailbox was made from
whatever the last few hours happened to look like.

That is the difference this closes. A number recomputed per send answers "how is
it right now"; a record answers "is it getting worse", and only the second can
justify backing off before something breaks.

**One row per sender per day, upserted.** The unique constraint on
``(workspace, sender, captured_on)`` is the idempotency: running twice in a day
refreshes the day rather than appending to it, so a retry after a partial run
cannot manufacture a trend out of duplicate points.

**It runs between verification and the ramp, and the order is the point.**
Verification at 05:40 refreshes the authentication flags this records; the
mailbox ramp at 06:10 reads health to decide volume. Capturing in between means
the ramp acts on a snapshot taken after today's DNS check rather than on
yesterday's.

**Measured from ``messages``, the same table every other outcome query reads.**
A separate definition of "sent" here would drift from the rollups, and two
screens disagreeing about one mailbox's bounce rate is worse than either being
slightly wrong.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from titan.db.models import SenderHealthSnapshot, SenderIdentity
from titan.db.session import WORKSPACE_KEY, workspace_unit_of_work
from titan.delivery import sender_health
from titan.delivery.deliverability import ReputationWindow
from titan.intelligence.sender_auth import MAX_VERIFICATION_AGE
from titan.workflows.types import CaptureSenderHealthInput, CaptureSenderHealthResult

logger = logging.getLogger(__name__)

ALL_SENDER_HEALTH_ACTIVITIES = ["capture_sender_health"]

#: The trailing window every reputation judgement here uses, matching the one
#: the delivery gate and the campaign manager apply.
WINDOW_DAYS = 30


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _measure(
    session: AsyncSession,
    *,
    sender_id: uuid.UUID,
    since: dt.datetime,
    today: dt.date,
) -> dict[str, int]:
    """This sender's delivery record over the window, and what it sent today."""
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) FILTER (WHERE sent_at IS NOT NULL)       AS sent,
                       count(*) FILTER (WHERE delivered_at IS NOT NULL)  AS delivered,
                       count(*) FILTER (WHERE bounced_at IS NOT NULL)    AS bounced,
                       count(*) FILTER (WHERE complained_at IS NOT NULL) AS complained,
                       count(*) FILTER (
                           WHERE sent_at IS NOT NULL
                             AND sent_at::date = :today
                       )                                                 AS sent_today
                  FROM messages
                 WHERE workspace_id = :workspace
                   AND sender_identity_id = :sender
                   AND created_at >= :since
                """
            ),
            {
                "workspace": session.info.get(WORKSPACE_KEY),
                "sender": sender_id,
                "since": since,
                "today": today,
            },
        )
    ).one()
    return {
        "sent": int(row.sent),
        "delivered": int(row.delivered),
        "bounced": int(row.bounced),
        "complained": int(row.complained),
        "sent_today": int(row.sent_today),
    }


@activity.defn(name="capture_sender_health")
async def capture_sender_health(
    request: CaptureSenderHealthInput,
) -> CaptureSenderHealthResult:
    """Record today's health for every sending identity in the workspace."""
    workspace_id = uuid.UUID(request.workspace_id)
    now = _now()
    today = now.date()
    since = now - dt.timedelta(days=WINDOW_DAYS)

    captured = 0
    by_status: dict[str, int] = {}
    try:
        async with workspace_unit_of_work(workspace_id) as session:
            identities = (
                (await session.execute(SenderIdentity.__table__.select()))
                .mappings()
                .all()
            )
            for identity in identities:
                counts = await _measure(
                    session,
                    sender_id=identity["id"],
                    since=since,
                    today=today,
                )
                last_verified = identity["last_verified_at"]
                snapshot = sender_health.SenderSnapshot(
                    sender_identity_id=str(identity["id"]),
                    sending_domain=identity["sending_domain"],
                    captured_on=today,
                    domain_verified=bool(identity["domain_verified"]),
                    spf_ok=bool(identity["spf_ok"]),
                    dkim_ok=bool(identity["dkim_ok"]),
                    dmarc_ok=bool(identity["dmarc_ok"]),
                    # Staleness is part of the verdict, not a footnote: a flag
                    # set two months ago is an assertion, not a check.
                    auth_stale=(
                        last_verified is None
                        or (now - last_verified) > MAX_VERIFICATION_AGE
                    ),
                    window=ReputationWindow(
                        sent=counts["sent"],
                        delivered=counts["delivered"],
                        hard_bounced=counts["bounced"],
                        complained=counts["complained"],
                    ),
                    sent_today=counts["sent_today"],
                )
                status = sender_health.classify(snapshot)

                await session.execute(
                    pg_insert(SenderHealthSnapshot.__table__)  # type: ignore[arg-type]
                    .values(
                        workspace_id=workspace_id,
                        sender_identity_id=identity["id"],
                        sending_domain=snapshot.sending_domain,
                        captured_on=today,
                        status=status.value,
                        domain_verified=snapshot.domain_verified,
                        spf_ok=snapshot.spf_ok,
                        dkim_ok=snapshot.dkim_ok,
                        dmarc_ok=snapshot.dmarc_ok,
                        auth_stale=snapshot.auth_stale,
                        window_sent=counts["sent"],
                        window_delivered=counts["delivered"],
                        window_bounced=counts["bounced"],
                        window_complained=counts["complained"],
                        sent_today=counts["sent_today"],
                        reasons=list(sender_health.reasons(snapshot)),
                    )
                    # Refreshes the day rather than appending to it. A retry
                    # after a partial run must not manufacture a trend out of
                    # duplicate points for the same date.
                    .on_conflict_do_update(
                        constraint="uq_sender_health_day",
                        set_={
                            "status": status.value,
                            "window_sent": counts["sent"],
                            "window_delivered": counts["delivered"],
                            "window_bounced": counts["bounced"],
                            "window_complained": counts["complained"],
                            "sent_today": counts["sent_today"],
                            "auth_stale": snapshot.auth_stale,
                            "reasons": list(sender_health.reasons(snapshot)),
                        },
                    )
                )
                captured += 1
                by_status[status.value] = by_status.get(status.value, 0) + 1
    except Exception as exc:
        logger.warning(
            "could not capture sender health; today's point is missing",
            extra={"error_code": type(exc).__name__},
        )
        return CaptureSenderHealthResult(unavailable=str(exc))

    return CaptureSenderHealthResult(
        captured=captured,
        detail=tuple(f"{name}={count}" for name, count in sorted(by_status.items())),
    )


__all__ = [
    "ALL_SENDER_HEALTH_ACTIVITIES",
    "WINDOW_DAYS",
    "capture_sender_health",
]
