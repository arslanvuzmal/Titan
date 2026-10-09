"""a carrier campaign can be provisioned for a clock, not only for a market

The previous migration keyed the carrier by market, which is as precise as a
market's *representative* timezone -- and measured on this workspace, that is
not precise enough:

    canada        258 leads   130 on a clock that is not theirs   50%
    usa           237         122                                 51%
    middle_east   246          99                                 40%
    australia     236          89                                 38%
    europe        488          99                                 20%
    uk          1,268           0                                  0%

**539 of 2,733 leads, one in five.** Toronto answers for Vancouver three hours
away, New York for Los Angeles, Dubai for Riyadh, Berlin for Dublin. The UK is
clean because it is one zone; every market that spans several is between a fifth
and half wrong.

Keyed by IANA timezone rather than by ``SubRegion``, because SubRegion is
defined only for the USA, Canada and Australia -- deliberately, since Europe's
zones follow national borders. But that is exactly where two of the five
failures are, so a band-shaped key would leave Dublin and Riyadh unfixable.
A timezone is what a carrier campaign actually holds, and every market has one.

**Nullable, and null keeps its old meaning.** A row with no timezone is the
market's carrier, which is what all six existing rows are. Routing tries the
exact clock first and falls back to the market, so this migration changes no
decision until a carrier campaign is provisioned for a specific zone. Nothing
is provisioned by this file: creating campaigns and attaching mailboxes is
somebody's deliberate act, and 539 leads' scheduling is not something to move
during a schema upgrade.

Revision ID: a8b3f2c71d94
Revises: f4a17c93b2e8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a8b3f2c71d94"
down_revision = "f4a17c93b2e8"
branch_labels = None
depends_on = None

TABLE = "carrier_campaigns"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("timezone", sa.String(length=64), nullable=True))

    # The old constraint allowed one row per market. It has to go, because a
    # market may now hold several -- one per clock inside it.
    op.drop_constraint("uq_carrier_campaign_market", TABLE, type_="unique")

    # Two partial indexes rather than one constraint over a nullable column:
    # Postgres treats NULLs as distinct in a UNIQUE constraint, so
    # (workspace, provider, region, timezone) would happily admit two market
    # rows for the same market -- and then which carrier a lead rides would
    # depend on row order.
    op.create_index(
        "uq_carrier_campaign_market",
        TABLE,
        ["workspace_id", "provider", "region"],
        unique=True,
        postgresql_where=sa.text("timezone IS NULL"),
    )
    op.create_index(
        "uq_carrier_campaign_clock",
        TABLE,
        ["workspace_id", "provider", "timezone"],
        unique=True,
        postgresql_where=sa.text("timezone IS NOT NULL"),
    )


def downgrade() -> None:
    # A zone-specific row cannot survive a column that no longer exists, and
    # collapsing it into its market would silently reroute mail. Refused.
    count = (
        op.get_bind()
        .execute(
            sa.text("SELECT count(*) FROM carrier_campaigns WHERE timezone IS NOT NULL")
        )
        .scalar()
    )
    if count:
        raise RuntimeError(
            f"{count} carrier campaign(s) are provisioned for a specific timezone. "
            "Downgrading would reroute their leads onto their market's clock. "
            "Delete or re-point those rows deliberately first."
        )
    op.drop_index("uq_carrier_campaign_clock", table_name=TABLE)
    op.drop_index("uq_carrier_campaign_market", table_name=TABLE)
    op.create_unique_constraint(
        "uq_carrier_campaign_market", TABLE, ["workspace_id", "provider", "region"]
    )
    op.drop_column(TABLE, "timezone")
