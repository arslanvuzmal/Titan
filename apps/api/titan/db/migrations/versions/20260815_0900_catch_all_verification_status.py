"""add catch_all to the verification_status enum

The bounce reduction engine classifies a recipient as deliverable, risky,
catch-all, unknown or invalid. Four of those five already existed; catch-all did
not, so a domain that accepts mail for every local part collapsed into UNKNOWN
and was indistinguishable from "the lookup failed".

Those two are not the same fact and must not share a value. UNKNOWN means Titan
learned nothing and should try again later. CATCH_ALL means Titan learned
something conclusive -- that no mailbox-level answer is obtainable from this
domain at any price, from any verification service -- and retrying is waste. A
send decision that cannot tell them apart either re-verifies forever or treats a
permanent condition as a transient one.

``verification_permits_sending`` in ``titan.db.enums`` is where the new value
earns its keep: catch-all sends only behind first-party provenance.

**On ordering.** The value is inserted AFTER 'risky' rather than appended, so
the type's label order continues to run from strongest to weakest evidence. PG
enum order is only cosmetic here -- nothing in Titan sorts on it -- but a type
whose labels read in a meaningless order invites somebody to assume they do not
matter and add the next one wherever it lands.

PostgreSQL 12+ permits ADD VALUE inside a transaction block provided the new
label is not *used* in the same transaction. This migration only declares it;
the first write happens in a later session. ``IF NOT EXISTS`` makes a re-run
against a partially migrated database a no-op rather than an error.

**Downgrade is deliberately partial.** PostgreSQL cannot drop an enum label.
Removing it properly means creating a replacement type, rewriting every column
that uses it, and repointing the default -- which would destroy rows already
carrying the value. The downgrade instead moves those rows to the safest
neighbouring value and leaves the label in place. UNKNOWN is that value: it is
not in SENDABLE_VERIFICATION_STATUSES, so a downgrade can only ever stop mail,
never start it.

Revision ID: d3b8072a15c4
Revises: c7d15e93f8a2
Create Date: 2026-08-15 09:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d3b8072a15c4"
down_revision: str | None = "c7d15e93f8a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every column typed verification_status, as (table, column). Both live on the
#: contact tables; contact_verifications is append-only, so its rows are
#: historical records of what a check concluded rather than current state.
_COLUMNS = (
    ("contact_channels", "verification_status"),
    ("contact_verifications", "result"),
)


def upgrade() -> None:
    op.execute(
        "ALTER TYPE verification_status ADD VALUE IF NOT EXISTS 'catch_all' AFTER 'risky'"
    )


def downgrade() -> None:
    for table, column in _COLUMNS:
        op.execute(
            f"UPDATE {table} SET {column} = 'unknown' WHERE {column} = 'catch_all'"  # noqa: S608
        )
