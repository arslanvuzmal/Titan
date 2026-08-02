"""Atomic send quotas.

The reservation is a single SQL statement. This matters: any design that reads
a counter, decides in Python, then writes it back has a race window in which N
concurrent outbox workers all observe ``used = limit - 1`` and all proceed
(gap analysis C-06, invariant 14).

    INSERT INTO quota_counters (...) VALUES (...)
    ON CONFLICT (workspace_id, scope_type, scope_key, window_date)
    DO UPDATE SET used = quota_counters.used + 1
    WHERE quota_counters.used < quota_counters.limit_value
    RETURNING used

PostgreSQL takes a row lock for the ON CONFLICT DO UPDATE path, so the read,
the bound check, and the increment are one indivisible step. An empty result
set means "limit reached" -- there is no third outcome.

Exhaustion produces a *deferral*, never a permanent failure (mission 15.4). The
next attempt is scheduled at the start of the next UTC window plus deterministic
per-message jitter, so a thousand deferred messages do not all fire at 00:00:00.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class QuotaScope(StrEnum):
    WORKSPACE = "workspace"
    CAMPAIGN = "campaign"
    SENDER = "sender"
    RECIPIENT_DOMAIN = "recipient_domain"


@dataclass(frozen=True, slots=True)
class QuotaRequest:
    scope_type: QuotaScope
    scope_key: str
    limit: int


@dataclass(frozen=True, slots=True)
class QuotaOutcome:
    granted: bool
    #: The scope that refused, when granted is False.
    exhausted_scope: QuotaScope | None = None
    exhausted_key: str | None = None
    limit: int | None = None

    @property
    def reason(self) -> str | None:
        if self.granted:
            return None
        return (
            f"{self.exhausted_scope.value if self.exhausted_scope else 'unknown'} "
            f"daily quota of {self.limit} reached for '{self.exhausted_key}'"
        )


_RESERVE_SQL = text(
    """
    INSERT INTO quota_counters
        (id, workspace_id, scope_type, scope_key, window_date,
         used, limit_value, created_at, updated_at)
    VALUES
        (gen_random_uuid(), :workspace_id, :scope_type, :scope_key, :window_date,
         1, :limit_value, now(), now())
    ON CONFLICT (workspace_id, scope_type, scope_key, window_date)
    DO UPDATE SET used = quota_counters.used + 1, updated_at = now()
    WHERE quota_counters.used < quota_counters.limit_value
    RETURNING used
    """
)

_RELEASE_SQL = text(
    """
    UPDATE quota_counters
       SET used = GREATEST(used - 1, 0), updated_at = now()
     WHERE workspace_id = :workspace_id
       AND scope_type = :scope_type
       AND scope_key = :scope_key
       AND window_date = :window_date
    """
)

_PEEK_SQL = text(
    """
    SELECT scope_type, scope_key, used, limit_value
      FROM quota_counters
     WHERE workspace_id = :workspace_id AND window_date = :window_date
    """
)


async def reserve_all(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    requests: list[QuotaRequest],
    window_date: dt.date,
) -> QuotaOutcome:
    """Reserve one unit in every scope, or none.

    Scopes are reserved in a fixed order (the order given) and rolled back
    individually if a later scope refuses. The caller is expected to run this
    inside the same transaction as the send-state change, so a crash between
    reservation and send rolls the counter back automatically; the explicit
    release path below covers the in-transaction partial-failure case only.
    """
    acquired: list[QuotaRequest] = []
    for req in requests:
        # A limit of zero means "this scope is closed"; skip the round trip.
        if req.limit <= 0:
            await _release(session, workspace_id, acquired, window_date)
            return QuotaOutcome(
                granted=False,
                exhausted_scope=req.scope_type,
                exhausted_key=req.scope_key,
                limit=req.limit,
            )
        result = await session.execute(
            _RESERVE_SQL,
            {
                "workspace_id": workspace_id,
                "scope_type": req.scope_type.value,
                "scope_key": req.scope_key,
                "window_date": window_date,
                "limit_value": req.limit,
            },
        )
        if result.scalar_one_or_none() is None:
            await _release(session, workspace_id, acquired, window_date)
            return QuotaOutcome(
                granted=False,
                exhausted_scope=req.scope_type,
                exhausted_key=req.scope_key,
                limit=req.limit,
            )
        acquired.append(req)
    return QuotaOutcome(granted=True)


async def _release(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    acquired: list[QuotaRequest],
    window_date: dt.date,
) -> None:
    for req in acquired:
        await session.execute(
            _RELEASE_SQL,
            {
                "workspace_id": workspace_id,
                "scope_type": req.scope_type.value,
                "scope_key": req.scope_key,
                "window_date": window_date,
            },
        )


async def snapshot(
    session: AsyncSession, *, workspace_id: uuid.UUID, window_date: dt.date
) -> list[dict[str, object]]:
    """Current usage for the dashboard. Read-only."""
    rows = await session.execute(
        _PEEK_SQL, {"workspace_id": workspace_id, "window_date": window_date}
    )
    return [
        {
            "scope_type": r.scope_type,
            "scope_key": r.scope_key,
            "used": r.used,
            "limit": r.limit_value,
        }
        for r in rows
    ]


def next_window_start(
    now: dt.datetime, dedupe_key: str, spread_seconds: int = 3600
) -> dt.datetime:
    """When a quota-deferred message should next be attempted.

    Deterministic per message so a retry computes the same instant, and spread
    across the first hour of the new window so the queue drains smoothly rather
    than stampeding at midnight (mission 15.4).
    """
    tomorrow = (now.astimezone(dt.UTC) + dt.timedelta(days=1)).date()
    midnight = dt.datetime.combine(tomorrow, dt.time.min, tzinfo=dt.UTC)
    digest = hashlib.sha256(dedupe_key.encode("utf-8")).digest()
    offset = int.from_bytes(digest[:4], "big") % max(spread_seconds, 1)
    return midnight + dt.timedelta(seconds=offset)


__all__ = [
    "QuotaOutcome",
    "QuotaRequest",
    "QuotaScope",
    "next_window_start",
    "reserve_all",
    "snapshot",
]
