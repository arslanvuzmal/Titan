"""Remove the manager's "keep it as it is" rows from the event stream.

The first backfill projected every row of ``autonomy_decisions`` -- 294,356
events, 83% of the stream, almost all one refusal repeated each cycle ("one or
both variants are too small to test"). The projection now keeps only proposals
that reached for a change; this removes what it would no longer project.

Only ``events`` is touched. It is a projection: every row removed here is still
in ``autonomy_decisions``, so nothing is lost, and the projector could not put
these rows back because its filter now excludes them.
"""

from __future__ import annotations

from alembic import op

revision = "b9f3d6a20e57"
down_revision = "a7e2c5f91d04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM events e
         USING autonomy_decisions a
         WHERE e.source = 'autonomy_decisions'
           AND e.source_id = a.id::text
           AND NOT a.applied
           AND a.proposed_value IS NOT DISTINCT FROM a.previous_value
        """
    )


def downgrade() -> None:
    # Nothing to restore: the rows are derived, and the next projection with
    # the old filter would recreate them from autonomy_decisions.
    pass
