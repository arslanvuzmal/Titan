"""Every sign that a message was seen, kept raw and graded.

``messages.first_opened_at`` is one timestamp with no provenance. It cannot
say whether the fetch that set it was a person, Apple's proxy loading every
image on arrival, or a corporate gateway scanning the link before delivery --
and those three mean "read", "delivered" and "nothing" respectively. Follow-ups
that branch on "seen" need to know which, and a grading rule that turns out to
be wrong needs the raw events to be re-graded from.

So one row per event: what happened (a pixel fetch, an evidence-page visit,
the page's own beacon confirming a person stayed), who asked (address, user
agent), how long after the send, and the grade the classifier gave it at the
time. The grade is a column rather than a computation so a later report shows
what the system believed when it acted.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d4b8e2c71a90"
down_revision = "c9e3a15d8f42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "engagement_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        #: The message whose pixel was fetched. NULL for an evidence-page visit,
        #: which belongs to the lead rather than to any one message.
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        #: open | visit | visit_confirmed
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        #: confirmed | likely | delivered | machine
        sa.Column("grade", sa.String(16), nullable=False),
        #: Why the classifier chose that grade, in a sentence.
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_engagement_events_ws_lead",
        "engagement_events",
        ["workspace_id", "lead_id", "occurred_at"],
    )
    op.create_index(
        "ix_engagement_events_message",
        "engagement_events",
        ["message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_engagement_events_message", table_name="engagement_events")
    op.drop_index("ix_engagement_events_ws_lead", table_name="engagement_events")
    op.drop_table("engagement_events")
