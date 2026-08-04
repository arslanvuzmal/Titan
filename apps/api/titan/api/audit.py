"""Append-only audit trail for sensitive actions (mission section 18).

Entries form a hash chain per workspace: each commits to the previous entry's
hash, so removing or altering a record breaks every hash after it. That does not
make tampering impossible -- it makes it *detectable*, which is the property an
audit trail actually needs.

Detail is redacted before write, so a payload containing a token cannot smuggle
it into the log.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.models import AuditLog
from titan.security.redaction import redact


async def record(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_kind: str = "user",
    actor_ip: str | None = None,
    request_id: str | None = None,
    detail: dict[str, Any] | None = None,
    outcome: str = "success",
    failure_reason: str | None = None,
) -> AuditLog:
    """Append one entry. Must be called inside the caller's transaction.

    Sharing the transaction is deliberate: if the action rolls back, so does its
    audit entry, and the log never claims something happened that did not.
    """
    now = dt.datetime.now(dt.UTC)

    previous_hash = (
        await session.execute(
            select(AuditLog.entry_hash)
            .where(AuditLog.workspace_id == workspace_id)
            .order_by(desc(AuditLog.occurred_at), desc(AuditLog.id))
            .limit(1)
        )
    ).scalar_one_or_none()

    safe_detail = redact(detail or {})
    entry_hash = hashlib.sha256(
        json.dumps(
            {
                "previous": previous_hash or "",
                "workspace": str(workspace_id),
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id or "",
                "actor": str(actor_user_id) if actor_user_id else "",
                "occurred_at": now.isoformat(),
                "detail": safe_detail,
                "outcome": outcome,
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()

    entry = AuditLog(
        workspace_id=workspace_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
        actor_ip=actor_ip,
        request_id=request_id,
        occurred_at=now,
        detail=safe_detail,
        previous_hash=previous_hash,
        entry_hash=entry_hash,
        outcome=outcome,
        failure_reason=failure_reason,
    )
    session.add(entry)
    return entry


async def verify_chain(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> tuple[bool, str | None]:
    """Walk the chain and report the first entry whose link is broken."""
    entries = (
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.workspace_id == workspace_id)
                .order_by(AuditLog.occurred_at, AuditLog.id)
            )
        )
        .scalars()
        .all()
    )

    expected_previous: str | None = None
    for entry in entries:
        if entry.previous_hash != expected_previous:
            return False, str(entry.id)
        expected_previous = entry.entry_hash
    return True, None


__all__ = ["record", "verify_chain"]
