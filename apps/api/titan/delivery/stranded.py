"""Approved drafts that nothing ever queued.

Queueing happens inside ``LeadResearchWorkflow``, one step after the approval it
waits for. That is the right place for it and it has one failure mode: if the
workflow is not there when the approval arrives, nothing else is watching. The
workflow can be gone for entirely ordinary reasons -- the approval window
elapsed, the worker was restarted mid-run, the run was cancelled, the campaign
was paused and resumed.

Found on the live workspace: **225 drafts approved, with no outbox row and no
message.** Mail that was researched, composed, validated and authorised, and
that would never have left. Nothing reported it, because every individual
component had done its own job correctly.

**This does not decide anything.** It finds the drafts and hands each one to the
same ``queue_message`` activity the workflow would have called, which re-applies
every gate -- suppression, sender pool, the duplicate-recipient rule, validation.
A draft that should not go out is refused there, exactly as it would have been
on the original path. The sweeper's only opinion is about *existence*: an
approved draft with nowhere to go is a bug, not a state.

**Idempotent by construction.** ``queue_message`` dedupes on ``draft-{id}``, so
a draft swept twice queues once. That is what makes it safe to run on a
schedule rather than by hand.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import DraftStatus
from titan.db.models import Message, MessageDraft, OutboxMessage

#: How many to take in one pass.
#:
#: Bounded because the sweeper competes with live sending for the same mailbox
#: quota, and because a backlog that appeared over two weeks does not need to
#: clear in one minute. The daily send limits bound what actually leaves
#: regardless; this bounds how much work is created at once.
DEFAULT_BATCH = 100


@dataclass(frozen=True, slots=True)
class Stranded:
    draft_id: uuid.UUID
    lead_id: uuid.UUID
    campaign_id: uuid.UUID


async def find_stranded(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    limit: int = DEFAULT_BATCH,
) -> list[Stranded]:
    """Approved drafts with neither an outbox row nor a message.

    Both absences are required. An outbox row means it is queued and the worker
    owns it. A message with no outbox row means it was already sent through
    another path -- Smartlead's own sequence, reconciled back in -- and queueing
    it now would send the same person the same message twice.

    Ordered oldest first: the drafts that have waited longest were composed
    against the oldest evidence, so they are the ones whose claims are closest
    to going stale.
    """
    outbox_exists = (
        select(OutboxMessage.id).where(OutboxMessage.draft_id == MessageDraft.id).exists()
    )
    message_exists = (
        select(Message.id).where(Message.draft_id == MessageDraft.id).exists()
    )
    rows = (
        await session.execute(
            select(MessageDraft.id, MessageDraft.lead_id, MessageDraft.campaign_id)
            .where(
                MessageDraft.workspace_id == workspace_id,
                MessageDraft.status == DraftStatus.APPROVED,
                MessageDraft.validation_passed.is_(True),
                ~outbox_exists,
                ~message_exists,
            )
            .order_by(MessageDraft.created_at)
            .limit(limit)
        )
    ).all()
    return [Stranded(draft_id=row[0], lead_id=row[1], campaign_id=row[2]) for row in rows]


__all__ = ["DEFAULT_BATCH", "Stranded", "find_stranded"]
