"""remember each mailbox's ramp ceiling instead of re-reading it

The ramp writes Smartlead's ``max_email_per_day`` and, until this table existed,
also read its ceiling from that field. Those are the same number, so the ceiling
became the ramp's own last output: a step down to 35% of 50 wrote 18, the next
run read 18 as the ceiling and wrote 35% of *that*, and a mailbox reached the
floor of one message a day in three runs. Recovery was impossible by
construction -- every share is a share of a ceiling that no longer existed.

So the ceiling is stored, and ``last_written_limit`` is what lets a human's edit
be told apart from the ramp's own write. That is the only signal available: the
provider does not record who changed a setting, and the ramp is not the only
thing that changes it.

``last_written_limit`` is nullable and starts null on purpose. A row created
before the ramp has ever written means "observed, never touched", and the first
run against it adopts whatever the operator has configured as the ceiling rather
than inventing one.

Revision ID: b4a6f2c81e97
Revises: 9d2a7f4be613
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4a6f2c81e97"
down_revision: str | None = "9d2a7f4be613"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "mailbox_ramp_state"

_ISOLATION = (
    "  current_setting('titan.workspace_id', true) IS NULL"
    "  OR current_setting('titan.workspace_id', true) = ''"
    "  OR workspace_id = current_setting('titan.workspace_id', true)::uuid"
)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("from_email", sa.String(320), nullable=False),
        sa.Column("ceiling", sa.Integer(), nullable=False),
        sa.Column("last_written_limit", sa.Integer()),
        sa.Column("last_written_at", sa.DateTime(timezone=True)),
        # No server default: ``VersionedMixin`` sets it in Python and
        # ``alembic check`` treats a default the model does not declare as
        # drift.
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "external_id",
            name="uq_mailbox_ramp_state_mailbox",
        ),
        sa.CheckConstraint("ceiling >= 0", name="ceiling_non_negative"),
        sa.CheckConstraint(
            "last_written_limit IS NULL OR last_written_limit >= 0",
            name="last_written_non_negative",
        ),
    )
    # Both indexes ``WorkspaceScoped`` declares: the single column from
    # ``workspace_id``'s own ``index=True``, and the composite behind "this
    # workspace, newest first". Creating only the composite is exactly the drift
    # that ``campaign_senders`` shipped with.
    op.create_index(f"ix_{TABLE}_workspace_id", TABLE, ["workspace_id"], unique=False)
    op.create_index(
        f"ix_{TABLE}_ws_created", TABLE, ["workspace_id", "created_at"], unique=False
    )
    op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY {TABLE}_workspace_isolation ON "{TABLE}" '
        f"USING ({_ISOLATION}) WITH CHECK ({_ISOLATION})"
    )


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS {TABLE}_workspace_isolation ON "{TABLE}"')
    op.drop_index(f"ix_{TABLE}_ws_created", table_name=TABLE)
    op.drop_index(f"ix_{TABLE}_workspace_id", table_name=TABLE)
    op.drop_table(TABLE)
