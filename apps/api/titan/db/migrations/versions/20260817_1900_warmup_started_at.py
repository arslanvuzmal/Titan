"""when a mailbox began warming, as opposed to when Titan first used it

Warm-up position is derived from ``min(messages.sent_at)`` for a sender --
Titan's own record of having sent through it. That is a *lower bound* on how
long a mailbox has been building reputation, not the thing itself.

``sales@`` made the gap concrete. Connected in Smartlead on 7 August with its
warm-up pool running ever since, it had no ``sender_identity`` row in Titan
until 17 August, so Titan placed it on day zero and allowed it five messages a
day. The mailbox was ten days warm; only Titan's view of it was new.

Nullable, and only ever consulted alongside the send history -- the earlier of
the two wins, so this can move a mailbox earlier in the ramp and never later.

Revision ID: e3c92b5d7a41
Revises: d2f81a4c6b39
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e3c92b5d7a41"
down_revision = "d2f81a4c6b39"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sender_identities",
        sa.Column("warmup_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sender_identities", "warmup_started_at")
