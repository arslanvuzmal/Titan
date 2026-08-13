"""Persisting what the model gateway did.

``ModelGateway`` appends a record for every completed call to ``self.calls``,
under a comment that has been true and unacted-on since the first release:
*"Every call, for the usage ledger writer to persist."* This is that writer.

Two tables, because they answer different questions and one cannot be derived
from the other:

* ``model_runs`` is the audit trail -- which model, which route, how many
  tokens, whether a fallback served it. It answers "what happened on this
  lead", and it is immutable.
* ``usage_ledger`` is the cost ledger, shared with every other billable thing
  Titan does (Places searches, browser time). It answers "what did this
  campaign cost", across providers that have nothing else in common.

**Idempotent on a key derived from the request.** A retried activity re-runs
its model calls, and a ledger that double-counts a retry reports a cost the
account was never charged -- which is worse than reporting nothing, because
somebody will budget against it. ``ON CONFLICT DO NOTHING`` on the existing
unique constraints does the arbitrating rather than a read-then-write, which
two concurrent workers would both lose.

Draining is deliberate: :func:`record_calls` empties the list it consumed, so a
gateway reused across several drafts cannot write the first draft's calls again
with the second's.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.models import ModelRun, UsageLedger

logger = logging.getLogger(__name__)

#: ``usage_ledger.category`` for a model call. The column is shared with other
#: spend, so the value has to say which kind this is.
CATEGORY = "model"


def _as_uuid(value: Any) -> uuid.UUID | None:
    if not value:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def idempotency_key(call: dict[str, Any]) -> str:
    """A key that is the same for a retry and different for a real second call.

    Built from the request hash and the identifiers the call was made under.
    Two genuinely distinct calls with byte-identical prompts on the same lead
    collapse to one row -- accepted, because at that point they are also
    indistinguishable from a retry, and over-counting spend is the error that
    misleads.
    """
    parts = (
        str(call.get("request_hash") or ""),
        str(call.get("task") or ""),
        str(call.get("lead_id") or ""),
        str(call.get("campaign_id") or ""),
    )
    return ":".join(parts)[:200]


async def record_calls(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    calls: list[dict[str, Any]],
    drain: bool = True,
) -> int:
    """Write every call to both tables. Returns how many were newly recorded.

    Call inside the transaction that produced the work the calls paid for, so
    a rolled-back draft does not leave a charge behind for a message that was
    never written.
    """
    if not calls:
        return 0

    recorded = 0
    for call in calls:
        key = idempotency_key(call)
        occurred = call.get("occurred_at") or dt.datetime.now(dt.UTC)
        lead_id = _as_uuid(call.get("lead_id"))
        campaign_id = _as_uuid(call.get("campaign_id"))

        inserted = await session.execute(
            pg_insert(ModelRun.__table__)  # type: ignore[arg-type]
            .values(
                workspace_id=workspace_id,
                idempotency_key=key,
                task=str(call.get("task") or "message"),
                provider=str(call.get("provider") or "unknown")[:40],
                model_id=str(call.get("model_id") or "unknown")[:200],
                lead_id=lead_id,
                campaign_id=campaign_id,
                attempt=1,
                status="completed",
                input_tokens=call.get("input_tokens"),
                output_tokens=call.get("output_tokens"),
                latency_ms=call.get("latency_ms"),
                cost_usd=float(call.get("cost_usd") or 0.0),
                request_hash=str(call.get("request_hash") or "")[:64],
                # Snapshots are deliberately absent. The prompt carries the
                # prospect's page text, the response carries a sentence about
                # their business, and neither is needed to answer what this
                # table exists to answer. The hash gives reproducibility
                # without retaining the content.
                request_snapshot=None,
                response_snapshot=None,
                schema_valid=True,
                repair_attempts=0,
                used_fallback=bool(call.get("used_fallback")),
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "idempotency_key"])
            .returning(ModelRun.__table__.c.id)
        )
        if inserted.scalar_one_or_none() is None:
            continue
        recorded += 1

        await session.execute(
            pg_insert(UsageLedger.__table__)  # type: ignore[arg-type]
            .values(
                workspace_id=workspace_id,
                idempotency_key=key,
                campaign_id=campaign_id,
                lead_id=lead_id,
                category=CATEGORY,
                provider=str(call.get("provider") or "unknown")[:60],
                resource=str(call.get("model_id") or "")[:200] or None,
                quantity=1.0,
                unit="call",
                input_tokens=call.get("input_tokens"),
                output_tokens=call.get("output_tokens"),
                cost_usd=float(call.get("cost_usd") or 0.0),
                # The gateway prices from a table, not from a provider invoice.
                # Saying so is the difference between a figure somebody can
                # reconcile and one they will argue with.
                cost_estimated=True,
                occurred_at=occurred,
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "idempotency_key"])
        )

    if drain:
        # Emptied so a gateway reused across drafts cannot re-record the first
        # draft's calls against the second.
        calls.clear()

    if recorded:
        logger.info(
            "model calls recorded",
            extra={"workspace_id": str(workspace_id), "calls": recorded},
        )
    return recorded


__all__ = ["CATEGORY", "idempotency_key", "record_calls"]
