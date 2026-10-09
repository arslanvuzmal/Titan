"""One table every outcome lands in, so a lead's history is one query.

The facts already exist, spread over a dozen tables that each answer one
question: ``messages`` knows what was sent, ``engagement_events`` what was
seen, ``inbound_messages`` what came back, ``call_outcomes`` what a phone call
bought. Asking "what happened to this business, in order" means joining all of
them by hand, and asking "what tends to happen after X" -- the question every
model ColdOps will train has to ask -- means doing it for every lead at once.

So ``events`` is a projection, not a new source of truth. Each row points back
at the row it was derived from (``source``, ``source_id``) and the unique key on
``(source, source_id, kind)`` is what makes projecting idempotent: running the
projector twice, or over a window that overlaps the last run, inserts nothing
new. The backfill and the every-fifteen-minutes run are the same code.

**No personal data in ``payload``.** No bodies, no email addresses, no names,
no IP addresses, no user agents -- the source rows keep those, under their own
retention rules. That is what lets this table be append-only without fighting
the retention job: there is nothing in it the retention job would need to
null. A business-level ``to_domain`` is kept because it is the business, not a
person.

Append-only by trigger, like ``autonomy_decisions``: UPDATE is refused, DELETE
stays possible so a lead's or workspace's rows can still cascade away.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e5c9a3d17b20"
down_revision = "d4b8e2c71a90"
branch_labels = None
depends_on = None

_ISOLATION = (
    "current_setting('titan.workspace_id', true) IS NULL"
    " OR current_setting('titan.workspace_id', true) = ''"
    " OR workspace_id = current_setting('titan.workspace_id', true)::uuid"
)


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        #: NULL for events about the system rather than a business: a
        #: placement probe, a manager decision, a suppression keyed by address.
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=True,
        ),
        #: When it happened in the world, which is not when it was recorded: a
        #: delivery event can arrive hours after the delivery.
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        #: Dotted, noun first: ``message.sent``, ``reply.classified``.
        sa.Column("kind", sa.String(48), nullable=False),
        #: The table the fact was projected from, and that row's id.
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("source", "source_id", "kind", name="uq_events_source"),
    )
    op.create_index(
        "ix_events_ws_occurred", "events", ["workspace_id", sa.text("occurred_at DESC")]
    )
    op.create_index("ix_events_lead_occurred", "events", ["lead_id", "occurred_at"])
    op.create_index(
        "ix_events_ws_kind_occurred", "events", ["workspace_id", "kind", "occurred_at"]
    )

    op.execute('ALTER TABLE "events" ENABLE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY events_workspace_isolation ON "events" '
        f"USING ({_ISOLATION}) WITH CHECK ({_ISOLATION})"
    )
    op.execute(
        "CREATE TRIGGER events_no_update "
        'BEFORE UPDATE ON "events" FOR EACH ROW '
        "EXECUTE FUNCTION titan_forbid_mutation()"
    )


def downgrade() -> None:
    op.execute('DROP TRIGGER IF EXISTS events_no_update ON "events"')
    op.execute('DROP POLICY IF EXISTS events_workspace_isolation ON "events"')
    op.drop_index("ix_events_ws_kind_occurred", table_name="events")
    op.drop_index("ix_events_lead_occurred", table_name="events")
    op.drop_index("ix_events_ws_occurred", table_name="events")
    op.drop_table("events")
