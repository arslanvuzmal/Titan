"""add the missing workspace/created index on campaign_senders

``WorkspaceScoped`` declares ``ix_<table>_ws_created`` for every table that
inherits it -- the composite index behind the near-universal "this workspace,
newest first" access pattern. The ``campaign_senders`` migration created the
three single-column indexes it needed and never created that one, so the model
and the schema disagreed from the moment the table was added.

Nothing failed at runtime, which is why it survived local testing: the index is
an access-path optimisation, not a constraint, and every query against a table
this small is a sequential scan either way. ``alembic check`` is what noticed,
and it is the reason that check exists -- drift is silent until the table is big
enough for the missing index to matter, by which point it is a production
problem rather than a migration one.

Added forward rather than folded into ``3f1c9a2b74de``. Editing an applied
migration leaves every database that already ran it permanently disagreeing with
its own history, and the fix is worth less than that costs.

Revision ID: 9d2a7f4be613
Revises: 7c4e19b8d05a
"""

from __future__ import annotations

from alembic import op

revision = "9d2a7f4be613"
down_revision = "7c4e19b8d05a"
branch_labels = None
depends_on = None

INDEX = "ix_campaign_senders_ws_created"
TABLE = "campaign_senders"


def upgrade() -> None:
    op.create_index(INDEX, TABLE, ["workspace_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
