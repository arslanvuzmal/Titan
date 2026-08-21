"""one carrier campaign per market, addressed by the market rather than by us

``campaigns.smartlead_campaign_id`` names the carrier campaign a Titan campaign
hands its leads to. That works only while a Titan campaign is one city: the
campaign knows its market, so it knows its clock.

The moment a campaign is a *business type* -- dentists, everywhere worth writing
to -- that stops being true. Its leads span six markets and one column can name
one carrier, so every lead would be scheduled on whichever market happened to be
written there. A Dubai dentist on London hours is the exact defect the six
market campaigns were created to end.

So the carrier for a market is recorded against the market. The routing
decision moves from the campaign to the lead, which is where the market
actually lives, and a vertical campaign becomes possible without any lead being
scheduled on somebody else's working day.

**Stored as text, not an integer.** Smartlead's campaign ids are numeric and
Instantly's are not. A column typed for one carrier would have to be migrated
to add the other, and this is the row that decides where mail goes.

**Backfilled from what is already true.** Every distinct (market, carrier) pair
in ``campaigns`` becomes a row, so routing by market produces exactly today's
answers on day one and the change is observable rather than a cutover.

Revision ID: f4a17c93b2e8
Revises: e3c92b5d7a41
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f4a17c93b2e8"
down_revision = "e3c92b5d7a41"
branch_labels = None
depends_on = None

TABLE = "carrier_campaigns"

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
        # Not an enum: the set of carriers is settings-driven and a new one must
        # not require a migration to route through.
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column(
            "region",
            postgresql.ENUM(name="region", create_type=False),
            nullable=False,
        ),
        sa.Column("carrier_campaign_id", sa.String(length=64), nullable=False),
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
        # One carrier per market per provider. Two rows would make the routing
        # decision depend on row order, and the thing being decided is which
        # working day somebody's mail arrives in.
        sa.UniqueConstraint(
            "workspace_id", "provider", "region", name="uq_carrier_campaign_market"
        ),
    )
    op.create_index(op.f("ix_carrier_campaigns_workspace_id"), TABLE, ["workspace_id"])
    # The "this workspace, newest first" index every workspace-scoped table
    # carries. Declared by the base class, so it has to be created here too
    # or the model and the schema disagree.
    op.create_index(
        op.f("ix_carrier_campaigns_ws_created"), TABLE, ["workspace_id", "created_at"]
    )

    op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY {TABLE}_workspace_isolation ON "{TABLE}" '
        f"USING ({_ISOLATION}) WITH CHECK ({_ISOLATION})"
    )

    # What the campaigns already say, said once per market instead of once per
    # campaign. DISTINCT ON keeps the most recently created campaign's answer
    # where two disagree -- they do not today, and if they ever do, the newer
    # one is the one somebody provisioned last.
    op.execute(
        """
        INSERT INTO carrier_campaigns
               (workspace_id, provider, region, carrier_campaign_id)
        SELECT DISTINCT ON (c.workspace_id, c.region)
               c.workspace_id, 'smartlead', c.region, c.smartlead_campaign_id::text
          FROM campaigns c
         WHERE c.smartlead_campaign_id IS NOT NULL
           AND c.region NOT IN ('unspecified', 'other')
         ORDER BY c.workspace_id, c.region, c.created_at DESC
        ON CONFLICT ON CONSTRAINT uq_carrier_campaign_market DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS {TABLE}_workspace_isolation ON "{TABLE}"')
    op.drop_index(op.f("ix_carrier_campaigns_ws_created"), table_name=TABLE)
    op.drop_index(op.f("ix_carrier_campaigns_workspace_id"), table_name=TABLE)
    op.drop_table(TABLE)
