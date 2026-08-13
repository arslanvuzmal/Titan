"""enable row level security on the smartlead tables

``smartlead_import_batches`` and ``smartlead_webhook_events`` are
workspace-scoped and were created without row-level security. Every other
scoped table has had it since the initial schema, and
``test_row_level_security_is_enabled_on_scoped_tables`` exists precisely to
stop one being added without it -- but that test could not run against
production, because production could not be migrated at all.

**This is a real gap in the live database, not a reconstruction artefact.**
Recovering ``4c1d9b7a2e50`` faithfully reproduced what production has, and what
production has is two tables where a scoped session sees every workspace's
rows. The recovered migration is left as it was, because it is history and
history is what makes the stamped revision honest. The fix goes forward, where
it applies to production and to every fresh database alike.

The practical exposure was small -- nothing in this repository reads either
table, and both hold data from a single workspace -- but "no reader today" is a
property of the current code rather than of the schema, and the isolation
guarantee is supposed to hold regardless.

Policy text is identical to the one the initial schema installs on the other
forty-odd scoped tables: an unset ``titan.workspace_id`` means an unscoped
session, which is how migrations, the outbox claim query and webhook ingestion
legitimately run.

Revision ID: c7d15e93f8a2
Revises: 9a4f0c2d6e11
Create Date: 2026-08-11 13:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c7d15e93f8a2"
down_revision: str | None = "9a4f0c2d6e11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("smartlead_import_batches", "smartlead_webhook_events")

_ISOLATION = (
    "  current_setting('titan.workspace_id', true) IS NULL"
    "  OR current_setting('titan.workspace_id', true) = ''"
    "  OR workspace_id = current_setting('titan.workspace_id', true)::uuid"
)


def upgrade() -> None:
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY {table}_workspace_isolation ON "{table}" '
            f"USING ({_ISOLATION}) WITH CHECK ({_ISOLATION})"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f'DROP POLICY IF EXISTS {table}_workspace_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
