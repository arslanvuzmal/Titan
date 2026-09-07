"""Release the higher-risk addresses a few a day, automatically.

157 leads have only a named or departmental mailbox to write to, because their
site published nothing else. Measured on this workspace those bounce at 8.11%
against 1.30% for a front desk, and a mailbox is blocked at 2%. Sent as a block
they would re-block every mailbox and restart the seven-day recovery clock;
never sent, they are 157 businesses written off for the shape of a string.

So they are held inactive and released in small numbers. Held with
``contact_channels.is_active`` because the send gate already refuses an
inactive channel -- nothing new has to be trusted at the moment of sending.

**The budget is on risk in flight, not on releases.** What is counted is risky
messages *sent today plus still queued* -- and the activity releases only up to
that headroom. Housekeeping runs hourly and this machine sleeps, so a
per-invocation count would be meaningless; and counting releases directly is
worse than meaningless, because a channel can become active for reasons that
have nothing to do with this activity. Measured while building it: 34 risky
channels went active in one day from the webmail recovery, none of them
released here.

Counting flight also closes a failure the release-count version could not see.
Releasing five a day while every mailbox is blocked accumulates -- ten quiet
days would leave fifty risky messages queued, and they would leave together the
moment the mailboxes recovered, which is the exact event this exists to
prevent. With the budget on flight, a queue that is not draining holds the
count at the ceiling and nothing further is released.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import select
from temporalio import activity

from titan.db.models import ContactChannel, Lead, Message, OutboxMessage
from titan.db.session import workspace_session, workspace_unit_of_work
from titan.intelligence.contacts import is_never_contact, is_role_address
from titan.workflows.types import ReleaseHeldInput, ReleaseHeldResult

logger = logging.getLogger(__name__)

#: How many higher-risk messages may be in flight -- sent today or queued.
#:
#: Five. The arithmetic: at a 1.30% base rate and an 8.11% risky rate, five
#: risky messages inside a day of roughly fifty keeps the blended rate near
#: 1.9%, under the 2% that blocks a mailbox, with the margin on the right side.
#:
#: Deliberately not derived at runtime from the live rates. A throttle that
#: widens as the measured rate improves is exactly how a recovering mailbox
#: gets pushed back over the line -- the rate improves *because* the throttle
#: is narrow, so reading it as permission to widen is circular.
DAILY_RELEASE_BUDGET = 5


@activity.defn(name="release_held_contacts")
async def release_held_contacts(request: ReleaseHeldInput) -> ReleaseHeldResult:
    """Release up to the day's remaining budget of held addresses."""
    workspace_id = uuid.UUID(request.workspace_id)
    budget = request.daily_budget or DAILY_RELEASE_BUDGET
    now = dt.datetime.now(dt.UTC)
    day_start = dt.datetime.combine(now.date(), dt.time.min, tzinfo=dt.UTC)

    async with workspace_session(workspace_id) as session:
        # Everything already committed to a risky address: sent today, or
        # sitting in the outbox waiting to go. Addresses rather than counts,
        # because the risky/safe judgement is Python's -- SQL would need the
        # role vocabulary restated, and two copies of that rule would diverge.
        sent_today = (
            (
                await session.execute(
                    select(Message.to_email).where(
                        Message.workspace_id == workspace_id,
                        Message.sent_at.is_not(None),
                        Message.sent_at >= day_start,
                    )
                )
            )
            .scalars()
            .all()
        )
        queued = (
            (
                await session.execute(
                    select(OutboxMessage.to_email_normalized).where(
                        OutboxMessage.workspace_id == workspace_id,
                        OutboxMessage.status.in_(("pending", "deferred", "leased")),
                    )
                )
            )
            .scalars()
            .all()
        )

        rows = (
            (
                await session.execute(
                    select(
                        ContactChannel.id,
                        ContactChannel.normalized_value,
                        ContactChannel.is_active,
                        ContactChannel.updated_at,
                    )
                    .join(Lead, Lead.primary_contact_channel_id == ContactChannel.id)
                    .where(
                        ContactChannel.workspace_id == workspace_id,
                        Lead.last_contacted_at.is_(None),
                    )
                    .order_by(ContactChannel.created_at)
                )
            )
            .tuples()
            .all()
        )

    risky = [
        row
        for row in rows
        if not is_role_address(row[1]) and not is_never_contact(row[1])
    ]
    held = [row for row in risky if not row[2]]

    def _is_risky(email: str) -> bool:
        return not is_role_address(email) and not is_never_contact(email)

    in_flight = sum(1 for email in sent_today if email and _is_risky(email)) + sum(
        1 for email in queued if email and _is_risky(email)
    )
    remaining = max(0, budget - in_flight)

    if not held or remaining == 0:
        return ReleaseHeldResult(
            held=len(held),
            released=0,
            reason=(
                "nothing held"
                if not held
                else f"{in_flight} risky messages already in flight, budget {budget}"
            ),
        )

    released = 0
    async with workspace_unit_of_work(workspace_id) as session:
        for channel_id, _email, _active, _updated in held[:remaining]:
            channel = await session.get(ContactChannel, channel_id)
            if channel is None or channel.is_active:
                continue
            channel.is_active = True
            released += 1

    logger.info(
        "released %s held contacts (%s risky already in flight, %s still held)",
        released,
        in_flight,
        len(held) - released,
    )
    return ReleaseHeldResult(
        held=len(held) - released,
        released=released,
        reason=f"{in_flight + released} of {budget} risky messages in flight",
    )


__all__ = ["DAILY_RELEASE_BUDGET", "release_held_contacts"]
