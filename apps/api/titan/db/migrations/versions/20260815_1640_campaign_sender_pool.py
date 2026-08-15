"""campaign sender pool

A campaign could name exactly one mailbox, which capped it at that mailbox's
daily limit -- fifty messages -- and the only lever was to raise the limit. That
is the wrong lever: fifty a day is roughly where a cold-outreach mailbox stops
looking like a person, so raising it buys volume by spending reputation.

Every existing campaign is backfilled into a pool of one, and
``campaigns.sender_identity_id`` is kept rather than dropped -- it remains the
fallback for a campaign with no pool rows, so nothing that worked before this
migration stops working, and a single-mailbox campaign needs no migration of its
own.

Revision ID: 3f1c9a2b74de
Revises: be758247a3ba
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "3f1c9a2b74de"
down_revision = "be758247a3ba"
branch_labels = None
depends_on = None

TABLE = "campaign_senders"

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
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["sender_identity_id"], ["sender_identities.id"], ondelete="CASCADE"
        ),
        # A mailbox listed twice would be counted twice by anything summing the
        # pool's capacity, and the sum is what decides how much a campaign may
        # send in a day.
        sa.UniqueConstraint(
            "campaign_id", "sender_identity_id", name="uq_campaign_sender"
        ),
    )
    op.create_index(op.f("ix_campaign_senders_workspace_id"), TABLE, ["workspace_id"])
    op.create_index(op.f("ix_campaign_senders_campaign_id"), TABLE, ["campaign_id"])
    op.create_index(
        op.f("ix_campaign_senders_sender_identity_id"), TABLE, ["sender_identity_id"]
    )

    op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY {TABLE}_workspace_isolation ON "{TABLE}" '
        f"USING ({_ISOLATION}) WITH CHECK ({_ISOLATION})"
    )

    # Every campaign that already names a mailbox becomes a pool of one, so the
    # pool is the single source of truth from here rather than a second one that
    # only some campaigns use.
    op.execute(
        """
        INSERT INTO campaign_senders (workspace_id, campaign_id, sender_identity_id)
        SELECT c.workspace_id, c.id, c.sender_identity_id
          FROM campaigns c
         WHERE c.sender_identity_id IS NOT NULL
        ON CONFLICT (campaign_id, sender_identity_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS {TABLE}_workspace_isolation ON "{TABLE}"')
    op.drop_index(op.f("ix_campaign_senders_sender_identity_id"), table_name=TABLE)
    op.drop_index(op.f("ix_campaign_senders_campaign_id"), table_name=TABLE)
    op.drop_index(op.f("ix_campaign_senders_workspace_id"), table_name=TABLE)
    op.drop_table(TABLE)
