"""Record whether a message carried the one-page brief.

The attachment ships to a sample -- ``TITAN_ONE_PAGER_SAMPLE_PERCENT`` -- so
that its effect on reply rate can be read against a control group drawn from the
same campaigns on the same days. Nothing recorded which side of that line a
message fell on, which made the trial unmeasurable: the sample is a hash of the
outbox row id, and outbox rows are pruned, so the answer would have been gone
before the replies arrived.

Derived but not derivable later, exactly like ``local_sent_hour`` above it:
recomputing the hash next month would answer with *next month's* percentage
about last month's send, and would be wrong for every message sent either side
of a change. Stamping it at send time is the only way the question stays
answerable.

Null for every message sent before this column existed. Null is not False --
"nobody recorded" and "recorded as no attachment" are different answers, and a
default of False would silently enrol a fortnight of historical sends into the
control group and bias the comparison it exists to support.

Revision ID: e2f7a91c4b83
Revises: d5c1e8a02f47
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2f7a91c4b83"
down_revision = "d5c1e8a02f47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("one_pager_attached", sa.Boolean(), nullable=True),
    )
    # Partial: the reading query asks "reply rate with the brief against reply
    # rate without", so it only ever touches rows where the answer is known.
    # Indexing the nulls would be paying to store the messages the query
    # deliberately excludes -- and while the trial runs at 10%, those are the
    # overwhelming majority.
    op.create_index(
        "ix_messages_one_pager",
        "messages",
        ["workspace_id", "campaign_id", "one_pager_attached"],
        postgresql_where=sa.text("one_pager_attached IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_messages_one_pager", table_name="messages")
    op.drop_column("messages", "one_pager_attached")
