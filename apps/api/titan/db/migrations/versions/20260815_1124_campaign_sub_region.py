"""let a campaign target one timezone band inside a market

A market is one working week and one set of holidays. It is not one clock, and
the USA is where that stops being a quibble: a business in California and one in
Georgia share a country, a market and a business day, and are three hours apart.
Until now every part of Titan scheduled both against a single Eastern clock.

The column lets a campaign say which band it works. It matters for exactly one
thing -- the fallback clock for a lead whose own timezone Places never resolved
-- and that is enough, because a US Pacific campaign scheduling those leads on
Eastern opens their window three hours before anybody has arrived.

**No backfill.** Every existing campaign is UNSPECIFIED and stays there. Unlike
``region``, which could be read off the country code the campaign already named,
nothing in the existing data says which band a campaign is aimed at: a UK
campaign has no band, and a US campaign that never declared one is genuinely
undeclared rather than implicitly Eastern. Inventing a band here would be
choosing a clock on somebody's behalf, which is the mistake being fixed.

Recipients are unaffected by this column either way. Their band is derived per
address from state and coordinates at send time, which is more specific than
anything a campaign could declare and does not need storing.

Revision ID: 8c4d7e2b91f5
Revises: 6b21a4f0c8d3
Create Date: 2026-08-15 11:24:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8c4d7e2b91f5"
down_revision: str | None = "6b21a4f0c8d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SUB_REGION_VALUES = (
    "us_eastern",
    "us_central",
    "us_mountain",
    "us_arizona",
    "us_pacific",
    "us_alaska",
    "us_hawaii",
    "ca_atlantic",
    "ca_eastern",
    "ca_central",
    "ca_mountain",
    "ca_pacific",
    "au_eastern",
    "au_central",
    "au_western",
    "unspecified",
)


def upgrade() -> None:
    sub_region = postgresql.ENUM(*SUB_REGION_VALUES, name="sub_region", create_type=False)
    sub_region.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "campaigns",
        sa.Column(
            "sub_region",
            sub_region,
            server_default="unspecified",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "sub_region")
    op.execute("DROP TYPE IF EXISTS sub_region")
