"""give each campaign its own working hours, in the recipient's time

One process-wide quiet-hours window -- 20:00 to 08:00 -- governed every campaign
in every market. It models the wrong thing twice.

It models night rather than work. A cold approach landing at 14:00 on a Sunday
clears a quiet-hours check comfortably and is still arriving in somebody's
weekend. And one window cannot be right for two markets at once: a single
setting is either correct for London or correct for Los Angeles, and it has
never been capable of being both, which is the whole difficulty of running six
markets out of one process.

So the window moves onto the campaign, bounded on both sides, with the working
week as part of it. The process-wide setting stays where it is and becomes a
floor: a campaign window is configuration, configuration can be wrong, and
22:00 to 23:00 is a legal pair of integers.

**``send_days`` needs a server default even though the model has one.** The
model default is a Python callable, which applies to rows this application
inserts and to nothing else. Adding a NOT NULL column without a database default
fails immediately on any table that already has rows -- which is every
deployment.

**The backfill reads the region added one migration ago.** Saudi Arabia and most
of the Levant work Sunday to Thursday, so a Middle East campaign left on Monday
to Friday would send on the two days its recipients are not working and skip the
two they are. Every other market keeps the Monday-to-Friday default.

Sunday to Thursday is the safer of the two possible defaults there rather than
the universally correct one: the UAE moved its public sector to Monday-Friday in
2022 and its private sector did not follow uniformly. Sending on a Sunday to
somebody who does not work Sundays is a message read on Monday; sending on a
Friday to somebody observing Jumu'ah is a message read as ignorant. A UAE-only
campaign should override it, which is why this is a column and not a constant.

Revision ID: 6b21a4f0c8d3
Revises: 3f8e2c19d740
Create Date: 2026-08-15 11:12:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "6b21a4f0c8d3"
down_revision: str | None = "3f8e2c19d740"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONDAY_TO_FRIDAY = "[0, 1, 2, 3, 4]"
SUNDAY_TO_THURSDAY = "[6, 0, 1, 2, 3]"


def upgrade() -> None:
    op.add_column(
        "campaign_policies",
        sa.Column(
            "send_window_start_hour",
            sa.Integer(),
            server_default="8",
            nullable=False,
        ),
    )
    op.add_column(
        "campaign_policies",
        sa.Column(
            "send_window_end_hour",
            sa.Integer(),
            server_default="17",
            nullable=False,
        ),
    )
    op.add_column(
        "campaign_policies",
        sa.Column(
            "send_days",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(f"'{MONDAY_TO_FRIDAY}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_campaign_policies_send_window_ordered"),
        "campaign_policies",
        "send_window_start_hour >= 0 AND send_window_end_hour <= 24 "
        "AND send_window_start_hour < send_window_end_hour",
    )

    op.execute(
        sa.text(
            "UPDATE campaign_policies p "
            "   SET send_days = CAST(:days AS jsonb) "
            "  FROM campaigns c "
            " WHERE c.id = p.campaign_id "
            "   AND c.workspace_id = p.workspace_id "
            "   AND c.region = 'middle_east'"
        ).bindparams(days=SUNDAY_TO_THURSDAY)
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_campaign_policies_send_window_ordered"),
        "campaign_policies",
        type_="check",
    )
    op.drop_column("campaign_policies", "send_days")
    op.drop_column("campaign_policies", "send_window_end_hour")
    op.drop_column("campaign_policies", "send_window_start_hour")
