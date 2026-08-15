"""record whether a bounce was hard or soft

Both bounce paths already knew. ``resend.normalize_webhook`` reads the
provider's flag into ``NormalizedEvent.is_hard_bounce``; the IMAP path reads the
DSN status code and ``replies.is_hard_bounce`` distinguishes a permanent 5.x.x
from a temporary 4.x.x. Each then used the answer once, to decide whether to
suppress, and discarded it.

Two things could not exist as a result. ``SuppressionReason.REPEATED_SOFT_BOUNCE``
had no writer anywhere in the codebase -- the policy of giving up on a mailbox
that keeps refusing existed only as a name in an enum -- because counting soft
bounces requires knowing which bounces were soft. And
``titan.intelligence.domain_health`` had to treat every bounce as hard, which it
documents as the reason its thresholds sit where they do.

A plain string with a check constraint rather than a native enum. Two values
that will not grow, against the cost of a PostgreSQL type plus a migration to
change a vocabulary nobody expects to change.

The index is partial. Almost every row in this table never bounced, and the read
path is "every soft bounce for one address inside a window" -- indexing the
nulls would be paying storage and write time to record their absence.

Existing rows keep NULL, which reads as "no bounce, or a bounce from before this
column existed". Backfilling from the raw provider payloads was considered and
rejected: the classification would be a guess made now about an event nobody
recorded then, and it would be indistinguishable afterwards from one the
ingester actually made.

Revision ID: a9d37f389e23
Revises: 7595a767a63b
Create Date: 2026-08-15 09:16:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9d37f389e23"
down_revision: str | None = "7595a767a63b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages", sa.Column("bounce_kind", sa.String(length=4), nullable=True)
    )
    op.create_index(
        "ix_messages_soft_bounces",
        "messages",
        ["workspace_id", "to_email_normalized", "bounced_at"],
        unique=False,
        postgresql_where=sa.text("bounce_kind = 'soft'"),
    )
    op.create_check_constraint(
        op.f("ck_messages_bounce_kind_allowed"),
        "messages",
        "bounce_kind IS NULL OR bounce_kind IN ('hard', 'soft')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_messages_bounce_kind_allowed"), "messages", type_="check")
    op.drop_index(
        "ix_messages_soft_bounces",
        table_name="messages",
        postgresql_where=sa.text("bounce_kind = 'soft'"),
    )
    op.drop_column("messages", "bounce_kind")
