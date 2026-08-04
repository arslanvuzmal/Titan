"""Suppression list operations.

Invariants 5 and 16. Suppression is checked twice -- once when a draft is
queued, and again by the outbox worker inside the same transaction that
reserves quota, because a recipient can unsubscribe in the seconds between
those two moments.

Matching is deliberately broader than exact string equality:

* the exact normalized address;
* its plus-tag base, so unsubscribing as ``a+news@x.com`` also stops ``a@x.com``;
* the registrable domain, when a domain-scope entry exists.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import PERMANENT_SUPPRESSION_REASONS, SuppressionReason
from titan.db.models import SuppressionEntry
from titan.intelligence.contacts import email_domain, suppression_keys


class SuppressedError(RuntimeError):
    """Raised when an operation would contact a suppressed recipient."""


async def is_suppressed(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    email: str,
    now: dt.datetime | None = None,
) -> SuppressionEntry | None:
    """Return the matching active suppression entry, or None.

    Returns the entry rather than a bool so the caller can record *why* a send
    was refused, which is what makes a blocked message explainable in the UI.
    """
    moment = now or dt.datetime.now(dt.UTC)
    candidates = list(suppression_keys(email))
    domain = email_domain(email)

    stmt = select(SuppressionEntry).where(
        SuppressionEntry.workspace_id == workspace_id,
        (
            (SuppressionEntry.scope == "email")
            & SuppressionEntry.normalized_value.in_(candidates)
        )
        | (
            (SuppressionEntry.scope == "domain")
            & (SuppressionEntry.normalized_value == domain)
        ),
    )
    for entry in (await session.execute(stmt)).scalars():
        if entry.is_active_at(moment):
            return entry
    return None


async def suppress(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    email_or_domain: str,
    reason: SuppressionReason,
    source: str,
    source_reference: str | None = None,
    created_by: uuid.UUID | None = None,
    scope: str = "email",
    expires_at: dt.datetime | None = None,
    detail: dict | None = None,
    now: dt.datetime | None = None,
) -> SuppressionEntry:
    """Add a suppression entry. Idempotent.

    A repeat suppression of the same address is a no-op rather than an error:
    duplicate webhook deliveries are normal, and failing here would turn a
    routine event into an alert.
    """
    moment = now or dt.datetime.now(dt.UTC)
    value = email_or_domain.strip().lower()

    if reason in PERMANENT_SUPPRESSION_REASONS and expires_at is not None:
        raise ValueError(
            f"{reason.value} is a permanent suppression reason and cannot expire"
        )

    # Suppressing a plus-tagged address also suppresses its base, because it is
    # the same human. Recorded as a second row at write time rather than
    # expanded at read time: the read path cannot know which tags exist, so
    # unsubscribing as `sam+news@x.test` would otherwise leave `sam@x.test`
    # reachable -- exactly the failure that produces a spam complaint.
    if scope == "email" and "+" in value.partition("@")[0]:
        local, _, domain_part = value.partition("@")
        base = f"{local.split('+', 1)[0]}@{domain_part}"
        if base != value:
            await suppress(
                session,
                workspace_id=workspace_id,
                email_or_domain=base,
                reason=reason,
                source=source,
                source_reference=source_reference,
                created_by=created_by,
                scope="email",
                expires_at=expires_at,
                detail={**(detail or {}), "derived_from": value},
                now=moment,
            )

    stmt = (
        pg_insert(SuppressionEntry.__table__)  # type: ignore[arg-type]
        .values(
            workspace_id=workspace_id,
            scope=scope,
            normalized_value=value,
            reason=reason.value,
            source=source,
            source_reference=source_reference,
            suppressed_at=moment,
            expires_at=expires_at,
            created_by=created_by,
            detail=detail or {},
        )
        # The row is immutable, so a conflict must do nothing rather than update.
        .on_conflict_do_nothing(
            index_elements=["workspace_id", "scope", "normalized_value"]
        )
        .returning(SuppressionEntry.__table__.c.id)
    )
    result = await session.execute(stmt)
    inserted_id = result.scalar_one_or_none()

    if inserted_id is None:
        existing = (
            await session.execute(
                select(SuppressionEntry).where(
                    SuppressionEntry.workspace_id == workspace_id,
                    SuppressionEntry.scope == scope,
                    SuppressionEntry.normalized_value == value,
                )
            )
        ).scalar_one()
        return existing

    return (
        await session.execute(
            select(SuppressionEntry).where(SuppressionEntry.id == inserted_id)
        )
    ).scalar_one()


async def suppress_many(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    emails: list[str],
    reason: SuppressionReason,
    source: str,
    created_by: uuid.UUID | None = None,
) -> int:
    """Bulk suppression, e.g. importing an existing do-not-contact list."""
    count = 0
    for email in emails:
        value = email.strip().lower()
        if not value:
            continue
        await suppress(
            session,
            workspace_id=workspace_id,
            email_or_domain=value,
            reason=reason,
            source=source,
            created_by=created_by,
        )
        count += 1
    return count


__all__ = ["SuppressedError", "is_suppressed", "suppress", "suppress_many"]
