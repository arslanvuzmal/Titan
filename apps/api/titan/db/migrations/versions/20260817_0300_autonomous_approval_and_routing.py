"""autonomous approval, and a carrier campaign per market

Two columns, for two halves of the same instruction: let the system approve its
own work, and send each lead from the campaign that belongs to its market.

``campaign_policies.auto_approve`` -- off by default. Every campaign that exists
today keeps the human gate it was created with; turning it on is a deliberate
act per campaign, not a global switch that changes behaviour on deploy.

``campaigns.smartlead_campaign_id`` -- which carrier campaign this Titan
campaign hands leads to. Until now one id lived in ``TITAN_SMARTLEAD_CAMPAIGN_ID``
and every lead in every market went to it, which is why a Dubai recipient was
scheduled on London time. Nullable, because a campaign with no carrier should
fall back to the configured default rather than fail to send.

Revision ID: d2f81a4c6b39
Revises: c9e14a7b3502
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d2f81a4c6b39"
down_revision = "c9e14a7b3502"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaign_policies",
        sa.Column(
            "auto_approve",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column("smartlead_campaign_id", sa.Integer(), nullable=True),
    )
    # Looked up by the delivery path for every send, and by the poller when it
    # reconciles a carrier campaign back to the Titan campaign that owns it.
    op.create_index(
        "ix_campaigns_smartlead_campaign_id",
        "campaigns",
        ["smartlead_campaign_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_campaigns_smartlead_campaign_id", table_name="campaigns")
    op.drop_column("campaigns", "smartlead_campaign_id")
    op.drop_column("campaign_policies", "auto_approve")
