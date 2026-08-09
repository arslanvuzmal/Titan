"""Acting on an inbound email.

Joins :mod:`titan.intelligence.replies`, which decides what an arriving message
means, to ``record_reply`` and ``suppress``, which already existed and already
did the right thing. Nothing here re-implements either: this module only
decides which to call.

The residual risk the verification report recorded -- "replies must be recorded
by hand, so invariant 15 protects only leads somebody entered" -- is what this
closes.

Collection is deliberately left to the caller. A webhook route, an IMAP poller
or an operator pasting a message all produce an :class:`InboundMessage`, and
all get identical treatment, so the rules cannot drift between intake paths.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import SuppressionReason
from titan.delivery.suppression import suppress
from titan.delivery.webhooks import record_reply
from titan.intelligence.replies import (
    InboundMessage,
    ReplyClassification,
    ReplyKind,
    classify_reply,
    is_hard_bounce,
)

logger = logging.getLogger(__name__)

#: What each classification suppresses for. A bounce only appears here when it
#: is permanent; a soft bounce suppresses nothing.
_SUPPRESSION_REASONS: dict[ReplyKind, SuppressionReason] = {
    ReplyKind.UNSUBSCRIBE: SuppressionReason.UNSUBSCRIBE,
    ReplyKind.COMPLAINT: SuppressionReason.COMPLAINT,
    ReplyKind.BOUNCE: SuppressionReason.HARD_BOUNCE,
}


@dataclass(frozen=True, slots=True)
class IngestResult:
    classification: ReplyClassification
    sequence_stopped: bool
    suppressed: bool

    @property
    def kind(self) -> ReplyKind:
        return self.classification.kind


async def ingest_inbound(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    message: InboundMessage,
    lead_id: uuid.UUID | None,
    now: dt.datetime | None = None,
) -> IngestResult:
    """Classify one inbound message and apply what it implies.

    ``lead_id`` may be None when the message could not be matched to a lead --
    a bounce for an address in two campaigns, say. Suppression still happens,
    because it is keyed on the address rather than on the lead; only the
    sequence stop needs to know which lead.
    """
    moment = now or dt.datetime.now(dt.UTC)
    classification = classify_reply(message)

    suppressed = False
    reason = _SUPPRESSION_REASONS.get(classification.kind)
    should_suppress = classification.requires_suppression or is_hard_bounce(
        classification
    )
    if should_suppress and reason is not None and message.from_email:
        await suppress(
            session,
            workspace_id=workspace_id,
            email_or_domain=message.from_email,
            reason=reason,
            source="inbound_email",
            detail={
                "signals": list(classification.signals),
                "subject": message.subject[:200],
                "classification": classification.kind.value,
            },
            now=moment,
        )
        suppressed = True

    stopped = False
    if classification.stops_the_sequence and lead_id is not None:
        await record_reply(
            session, workspace_id=workspace_id, lead_id=lead_id, replied_at=moment
        )
        stopped = True

    logger.info(
        "inbound email classified",
        extra={
            "classification": classification.kind.value,
            "signals": list(classification.signals),
            "sequence_stopped": stopped,
            "suppressed": suppressed,
            "lead_id": str(lead_id) if lead_id else None,
        },
    )
    return IngestResult(
        classification=classification, sequence_stopped=stopped, suppressed=suppressed
    )


__all__ = ["IngestResult", "ingest_inbound"]
