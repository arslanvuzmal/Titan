"""give the manager somewhere to record a promoted phrasing

Phase 05 asks for a variant with a genuine lift to be promoted automatically and
one with a coin-flip difference explicitly not, with both decisions readable
months later. The comparison existed and the decision trail existed; what was
missing was anywhere for the answer to live.

An integer, not the variant string. The composer picks a register by index, so
an index is what it can act on directly; a name would have to be parsed and
validated at the point of use, and the point of use is exactly where an
unrecognised value would fall back to the old behaviour and look as though the
promotion had never happened.

Null means the manager has no opinion, which is the state every campaign starts
in and returns to if a promotion is ever withdrawn. It is deliberately not
zero -- zero is a real register, and a column where "no opinion" and "the first
phrasing" are the same value cannot express the difference.

``managed_`` like its two siblings. The human's configuration is never written
by the manager, so next cycle's bound is always what a person approved rather
than what the manager last decided.

Revision ID: c9e14a7b3502
Revises: b4a6f2c81e97
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e14a7b3502"
down_revision: str | None = "b4a6f2c81e97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaign_policies",
        sa.Column("managed_promoted_variant", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("campaign_policies", "managed_promoted_variant")
