"""give campaigns a market, and backfill it from the country they already name

Campaigns carried ``target_country_code`` and their own policy, and each could be
read on its own. What did not exist was the layer above -- a way to ask which
markets Titan is actually working and how much of the week's sending each took.
That question was answered by holding six campaign pages open at once.

**Region is stored, not derived.** Deriving it from ``target_country_code`` was
the tempting version and it does not work: that column holds one country, so a
campaign aimed at Europe cannot be expressed by it at all, and most rows leave it
empty. What the two share is a consistency check --
``portfolio.disagrees_with_country`` -- which surfaces a campaign declaring USA
while naming GB, and never rewrites either. A campaign legitimately declaring
EUROPE while naming Germany first must not have one of them silently corrected.

**The backfill is one-directional and conservative.** Every existing row gets
UNSPECIFIED from the server default; rows whose country code maps cleanly to one
of the six markets are then upgraded. A code the map does not recognise is left
UNSPECIFIED rather than set to OTHER, because OTHER means somebody chose it and
nobody has chosen anything here yet. Getting a region wrong is worse than leaving
it unset: it schedules a business day in the wrong hemisphere.

The country list below is a copy of the one in ``titan.intelligence.portfolio``
rather than an import. A migration has to keep working when the module it was
written against has moved on -- importing application code into a migration is
how a schema change starts failing months later because a constant was renamed.

Revision ID: 3f8e2c19d740
Revises: a9d37f389e23
Create Date: 2026-08-15 11:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3f8e2c19d740"
down_revision: str | None = "a9d37f389e23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REGION_VALUES = (
    "usa",
    "canada",
    "uk",
    "europe",
    "australia",
    "middle_east",
    "other",
    "unspecified",
)

#: Frozen at the time of this migration. See the note above on why it is not
#: imported.
BACKFILL: dict[str, tuple[str, ...]] = {
    "usa": ("US",),
    "canada": ("CA",),
    "uk": ("GB", "UK"),
    "europe": (
        "IE",
        "DE",
        "FR",
        "ES",
        "PT",
        "IT",
        "NL",
        "BE",
        "LU",
        "AT",
        "CH",
        "DK",
        "SE",
        "NO",
        "FI",
        "PL",
        "CZ",
        "GR",
        "RO",
        "HU",
    ),
    "australia": ("AU", "NZ"),
    "middle_east": ("AE", "SA", "QA", "KW", "BH", "OM", "JO", "IL"),
}


def upgrade() -> None:
    # The type is created explicitly. Alembic emits ADD COLUMN before it would
    # create the enum, so letting sa.Enum do it produces "type region does not
    # exist" against a fresh database -- which is every database except the one
    # the migration was written on.
    region_type = postgresql.ENUM(*REGION_VALUES, name="region", create_type=False)
    region_type.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "campaigns",
        sa.Column(
            "region",
            region_type,
            server_default="unspecified",
            nullable=False,
        ),
    )
    op.create_index(op.f("ix_campaigns_region"), "campaigns", ["region"])

    for name, codes in BACKFILL.items():
        op.execute(
            # CAST, not a bare parameter: psycopg sends a bound string as
            # VARCHAR and PostgreSQL will not compare that to an enum column.
            sa.text(
                "UPDATE campaigns SET region = CAST(:region AS region) "
                " WHERE region = 'unspecified' "
                "   AND upper(trim(target_country_code)) = ANY(:codes)"
            ).bindparams(region=name, codes=list(codes))
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_campaigns_region"), table_name="campaigns")
    op.drop_column("campaigns", "region")
    op.execute("DROP TYPE IF EXISTS region")
