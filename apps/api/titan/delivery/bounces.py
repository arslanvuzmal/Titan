"""What happens after a bounce, for both of the ways one can arrive.

A bounce reaches Titan two ways: as a provider webhook, and as an actual email
from a mail server that Titan's IMAP poller reads. Both already classified hard
from soft correctly -- ``resend.normalize_webhook`` reads the provider's flag,
``replies.is_hard_bounce`` reads the DSN status code -- and both then did the
same two things with the answer: suppress on hard, nothing on soft.

Nothing on soft is right for the first one. A 4.x.x is a full mailbox, a
greylisting server, a temporary outage; the address is fine and will accept mail
later, and suppressing it would throw away a real lead over a bad afternoon.
Nothing on the fifth is not right, and that is what this module adds. Until now
``SuppressionReason.REPEATED_SOFT_BOUNCE`` was a value in an enum that no code
had ever written -- the policy existed only as a name.

**Both paths call in here.** They were already two implementations of "decide
what a bounce means", differing in structure and drifting freely, and adding a
counter to each would have made two counters. The escalation rule now exists
once.

**Counting is per address, not per lead.** A full mailbox is a property of the
mailbox. Two campaigns writing to the same person should reach the threshold
together rather than each spending three attempts on it.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import LeadStatus, SuppressionReason
from titan.db.models import Lead, Message
from titan.delivery.suppression import suppress

logger = logging.getLogger(__name__)

#: How far back soft bounces are counted. Matches the reputation and domain
#: health windows: long enough that a mailbox full every fortnight is caught,
#: short enough that a bad month two quarters ago is forgotten.
SOFT_BOUNCE_WINDOW = dt.timedelta(days=30)

#: Soft bounces within that window after which the address is suppressed.
#:
#: Three, and the third one suppresses. Two is too eager -- a mailbox can
#: plausibly be full twice in a month and belong to somebody who wants to hear
#: from us. By the third the pattern is the mailbox, not the moment.
SOFT_BOUNCES_TO_SUPPRESS = 3

#: Undiagnosed bounces after which the address is suppressed.
#:
#: Two, because the reasoning behind three does not survive the loss of the
#: diagnosis. Three is right for a bounce a DSN *told us* was temporary: a
#: mailbox can plausibly be full twice in a month and belong to somebody who
#: wants to hear from us. When the provider says only "this bounced", the second
#: identical failure is the strongest evidence available, and the cost of the
#: third attempt is not symmetric -- being wrong here means one temporarily full
#: mailbox given up on, while being wrong the other way means a third bounce
#: against a denominator small enough that it moves the whole account's rate.
#:
#: Observed: one malformed address, mailed twice, produced two of the five
#: bounces behind a 6.2% rate -- which halved every mailbox's daily volume.
UNDIAGNOSED_BOUNCES_TO_SUPPRESS = 2

#: How long to leave the lead alone after the nth soft bounce, indexed from the
#: first. Retrying into a full mailbox the next morning produces a second soft
#: bounce and no information; the point of backing off is to let the condition
#: that caused it clear.
SOFT_BOUNCE_BACKOFF: tuple[dt.timedelta, ...] = (
    dt.timedelta(days=2),
    dt.timedelta(days=5),
)

#: How far back to look for the message an unattributed inbound bounce is about.
_ATTRIBUTION_WINDOW = dt.timedelta(days=14)


class BounceKind(StrEnum):
    HARD = "hard"
    SOFT = "soft"
    #: The provider said a message bounced and nothing about why.
    #:
    #: Distinct from SOFT, which is a *finding* -- a DSN carrying a 4.x.x code,
    #: meaning a real mailbox that was temporarily unable to accept. This is the
    #: absence of a finding. Smartlead reports ``is_bounced`` and no diagnostic,
    #: and calling that soft borrows the confidence of a diagnosis nobody made.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BounceOutcome:
    kind: BounceKind
    #: Soft bounces for this address in the window, including this one. Zero for
    #: a hard bounce, and zero when the bounce could not be tied to a message.
    soft_bounce_count: int = 0
    suppressed: bool = False
    reason: SuppressionReason | None = None
    #: When the lead may be approached again, when a soft bounce backed it off.
    retry_after: dt.datetime | None = None
    #: False when no sent message could be attributed, so nothing was counted.
    attributed: bool = True


async def record_bounce(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    to_email: str,
    kind: BounceKind,
    source: str,
    now: dt.datetime,
    message_id: uuid.UUID | None = None,
    lead_id: uuid.UUID | None = None,
    source_reference: str | None = None,
    detail: dict | None = None,
) -> BounceOutcome:
    """Apply the consequences of one bounce.

    ``message_id`` may be None: an inbound bounce is an ordinary email and does
    not always thread back to what it is about. In that case the most recent
    message sent to the address is used, which is very nearly always the right
    one -- a mail server bounces what it was just handed. If there is no such
    message either, the bounce is still acted on when it is hard, and is not
    counted when it is soft, because a soft bounce that cannot be tied to a send
    is not evidence about a send.
    """
    target = (to_email or "").strip().lower()
    attributed_id = message_id or await _most_recent_message_to(
        session, workspace_id=workspace_id, to_email=target, now=now
    )

    if attributed_id is not None:
        # bounced_at is set here too, with COALESCE so an existing timestamp is
        # never moved. The webhook path sets it a few lines earlier in
        # _apply_state, but the IMAP path has no equivalent -- and the soft
        # bounce count filters on bounced_at, so a bounce recorded without one
        # would stamp its kind and then never be counted.
        await session.execute(
            update(Message)
            .where(Message.id == attributed_id)
            .values(
                bounce_kind=kind.value,
                bounced_at=func.coalesce(Message.bounced_at, now),
            )
        )

    if kind is BounceKind.HARD:
        await _suppress_and_stop(
            session,
            workspace_id=workspace_id,
            target=target,
            lead_id=lead_id,
            reason=SuppressionReason.HARD_BOUNCE,
            source=source,
            source_reference=source_reference,
            detail=detail,
            now=now,
        )
        return BounceOutcome(
            kind=kind,
            suppressed=True,
            reason=SuppressionReason.HARD_BOUNCE,
            attributed=attributed_id is not None,
        )

    if attributed_id is None:
        logger.info(
            "soft bounce could not be attributed to a sent message; not counted",
            extra={"workspace_id": str(workspace_id), "source": source},
        )
        return BounceOutcome(kind=kind, attributed=False)

    count = await _soft_bounce_count(
        session, workspace_id=workspace_id, to_email=target, now=now
    )

    # Both kinds count toward the same total -- they are all evidence about the
    # same address -- but how many it takes depends on what the provider was
    # able to tell us about *this* one. A diagnosis earns the benefit of the
    # doubt; its absence does not.
    limit = (
        UNDIAGNOSED_BOUNCES_TO_SUPPRESS
        if kind is BounceKind.UNKNOWN
        else SOFT_BOUNCES_TO_SUPPRESS
    )

    if count >= limit:
        await _suppress_and_stop(
            session,
            workspace_id=workspace_id,
            target=target,
            lead_id=lead_id,
            reason=SuppressionReason.REPEATED_SOFT_BOUNCE,
            source=source,
            source_reference=source_reference,
            detail={**(detail or {}), "soft_bounce_count": count},
            now=now,
        )
        logger.info(
            "address suppressed after repeated soft bounces",
            extra={"count": count, "source": source},
        )
        return BounceOutcome(
            kind=kind,
            soft_bounce_count=count,
            suppressed=True,
            reason=SuppressionReason.REPEATED_SOFT_BOUNCE,
        )

    # Below the threshold: hold the lead back rather than write it off. The
    # index is count-1 because the first soft bounce is count 1, and it is
    # clamped so a threshold raised later cannot walk off the end of the tuple.
    delay = SOFT_BOUNCE_BACKOFF[min(count - 1, len(SOFT_BOUNCE_BACKOFF) - 1)]
    retry_after = now + delay
    if lead_id is not None:
        await session.execute(
            update(Lead)
            .where(Lead.id == lead_id)
            .values(
                next_action_at=retry_after,
                status_reason=(
                    f"{'undiagnosed' if kind is BounceKind.UNKNOWN else 'soft'} "
                    f"bounce {count} of {limit}; waiting {delay.days} days"
                ),
            )
        )
    return BounceOutcome(kind=kind, soft_bounce_count=count, retry_after=retry_after)


async def _most_recent_message_to(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    to_email: str,
    now: dt.datetime,
) -> uuid.UUID | None:
    if not to_email:
        return None
    return (
        await session.execute(
            text(
                """
                SELECT id FROM messages
                 WHERE workspace_id = :workspace
                   AND to_email_normalized = :email
                   AND sent_at IS NOT NULL
                   AND sent_at >= :since
                 ORDER BY sent_at DESC
                 LIMIT 1
                """
            ),
            {
                "workspace": workspace_id,
                "email": to_email,
                "since": now - _ATTRIBUTION_WINDOW,
            },
        )
    ).scalar_one_or_none()


async def _soft_bounce_count(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    to_email: str,
    now: dt.datetime,
) -> int:
    """Soft bounces recorded for this address inside the window.

    Reads the stamps rather than a counter, so the number is always whatever the
    messages actually say. A counter column would drift the first time a webhook
    was delivered twice, and would drift upward -- toward suppressing an address
    that never earned it.
    """
    return int(
        (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM messages
                     WHERE workspace_id = :workspace
                       AND to_email_normalized = :email
                       AND bounce_kind IN ('soft', 'unknown')
                       AND bounced_at >= :since
                    """
                ),
                {
                    "workspace": workspace_id,
                    "email": to_email,
                    "since": now - SOFT_BOUNCE_WINDOW,
                },
            )
        ).scalar_one()
        or 0
    )


async def _suppress_and_stop(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    target: str,
    lead_id: uuid.UUID | None,
    reason: SuppressionReason,
    source: str,
    source_reference: str | None,
    detail: dict | None,
    now: dt.datetime,
) -> None:
    if not target:
        return
    await suppress(
        session,
        workspace_id=workspace_id,
        email_or_domain=target,
        reason=reason,
        source=source,
        source_reference=source_reference,
        detail=detail or {},
        now=now,
    )
    if lead_id is not None:
        await session.execute(
            update(Lead)
            .where(Lead.id == lead_id)
            .values(
                status=LeadStatus.SUPPRESSED,
                status_reason=f"{reason.value} via {source}",
            )
        )


__all__ = [
    "SOFT_BOUNCES_TO_SUPPRESS",
    "SOFT_BOUNCE_BACKOFF",
    "SOFT_BOUNCE_WINDOW",
    "UNDIAGNOSED_BOUNCES_TO_SUPPRESS",
    "BounceKind",
    "BounceOutcome",
    "record_bounce",
]
