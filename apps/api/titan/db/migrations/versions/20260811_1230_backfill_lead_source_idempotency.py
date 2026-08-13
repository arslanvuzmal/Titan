"""backfill lead_source idempotency keys out of query_parameters

``lead_sources.idempotency_key`` existed in the deployed database and in no
model, because the migration that created it was the one this repository had
lost. The discovery activity, unable to see the column, wrote its key into the
``query_parameters`` JSON blob instead and looked it up with a JSON path
expression -- with a comment stating the column did not exist.

Now that the column is declared, discovery uses it, and the unique constraint
on ``(workspace_id, idempotency_key)`` enforces what the old lookup could only
hope for. This moves the keys already written so a retry of an *older*
discovery run still finds its own prior work rather than paying for a second
billable Places search.

Copies rather than moves: the value stays in ``query_parameters`` as well. The
blob is the run's recorded provenance, and editing history to tidy a
denormalisation is a poor trade against being able to read what a past run
actually recorded.

Guarded on the column being null, so this is safe to run against a database
where the keys are already in place.

Revision ID: 9a4f0c2d6e11
Revises: 5e2a94c17b03
Create Date: 2026-08-11 12:30:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9a4f0c2d6e11"
down_revision: str | None = "5e2a94c17b03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE lead_sources
               SET idempotency_key = query_parameters ->> 'idempotency_key'
             WHERE idempotency_key IS NULL
               AND query_parameters ->> 'idempotency_key' IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    """Deliberately not reversed.

    The value is still in ``query_parameters``, so nothing is lost by leaving
    the column populated -- and clearing it would strip keys from rows that
    always had them, which this migration never touched and has no way to tell
    apart.
    """
