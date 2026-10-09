"""a campaign may be a business type rather than a place

Every campaign here is an industry crossed with a city -- "Dentists Leeds UK",
"Med spas, Dubai". Twenty-three of them against twenty-five sends a day, which
is one or two sends each, and the allocator classifies all twenty-three as
*learning* because none accumulates enough volume to be judged on. It is not
failing to concentrate; it has nothing to tell them apart with.

A campaign that is a business type instead accumulates its whole vertical's
volume, which is what makes "which businesses need us" answerable from evidence
rather than assumed.

**Why a column and not just leaving region unset.** ``UNSPECIFIED`` means
nobody has said. "Every market" is a different fact and needs to say so: twenty
paused campaigns in this workspace are UNSPECIFIED because they are leftover
test rows, and a rule that read them as global would put them to work.

**False everywhere, including for new rows.** Nothing changes behaviour until a
campaign is deliberately marked. The rotation, the language gate and the carrier
routing are all in place before any campaign uses them, so the switch is one
row's edit and is reversible by editing it back.

Revision ID: b7d419e0c3a5
Revises: a8b3f2c71d94
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7d419e0c3a5"
down_revision = "a8b3f2c71d94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column(
            "spans_all_markets",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "spans_all_markets")
