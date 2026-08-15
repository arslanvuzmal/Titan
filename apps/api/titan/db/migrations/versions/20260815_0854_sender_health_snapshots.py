"""record sender health once a day instead of recomputing it per send

Every number in this table was already being calculated. The outbox worker
resolves SPF/DKIM/DMARC freshness through ``sender_auth`` and measures bounces
and complaints over a trailing thirty days through ``deliverability``, on every
single message -- and then throws the result away once the send decision is made.

That is enough to stop one message and not enough to answer the question anybody
actually asks, which is not "is this mailbox healthy" but "is it getting worse".
A 0.04% complaint rate is fine. A 0.04% complaint rate that was 0.01% last
Tuesday is a mailbox about to be cut off, and a single measurement cannot tell
those apart.

**One row per sender per day, upserted.** Following ``quota_counters`` rather
than the append-only tables: this is a rolling aggregate keyed by date, not a
record of an event. The worker upserts as a by-product of the check it already
runs, so the last send of the day leaves the day's final numbers and no
scheduled job is needed. The append-only alternative -- a row per message --
would be more faithful and would produce tens of thousands of rows nobody reads.

A sender that sends nothing on a day gets no row. The gap is the information:
absent means idle, and inventing a zero row would claim a measurement nobody
took.

**Row-level security is added here, not left to a later migration.** Autogenerate
does not emit it, and ``test_row_level_security_is_enabled_on_scoped_tables``
exists precisely because a scoped table once shipped without it -- see
``c7d15e93f8a2``, which had to repair two tables in a live database. The policy
text below is identical to the one the initial schema installs on the other
forty-odd scoped tables: an unset ``titan.workspace_id`` means an unscoped
session, which is how migrations and the outbox worker legitimately run.

Revision ID: 7595a767a63b
Revises: d3b8072a15c4
Create Date: 2026-08-15 08:54:56.211714+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7595a767a63b"
down_revision: str | None = "d3b8072a15c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "sender_health_snapshots"

_ISOLATION = (
    "  current_setting('titan.workspace_id', true) IS NULL"
    "  OR current_setting('titan.workspace_id', true) = ''"
    "  OR workspace_id = current_setting('titan.workspace_id', true)::uuid"
)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("sender_identity_id", sa.UUID(), nullable=False),
        sa.Column("sending_domain", sa.String(length=253), nullable=False),
        sa.Column("captured_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("domain_verified", sa.Boolean(), nullable=False),
        sa.Column("spf_ok", sa.Boolean(), nullable=False),
        sa.Column("dkim_ok", sa.Boolean(), nullable=False),
        sa.Column("dmarc_ok", sa.Boolean(), nullable=False),
        sa.Column("auth_stale", sa.Boolean(), nullable=False),
        sa.Column("window_sent", sa.Integer(), nullable=False),
        sa.Column("window_delivered", sa.Integer(), nullable=False),
        sa.Column("window_bounced", sa.Integer(), nullable=False),
        sa.Column("window_complained", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retries", sa.Integer(), nullable=False),
        sa.Column("deferred", sa.Integer(), nullable=False),
        sa.Column("sent_today", sa.Integer(), nullable=False),
        sa.Column("warmup_day", sa.Integer(), nullable=True),
        sa.Column("warmup_limit", sa.Integer(), nullable=True),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["sender_identity_id"],
            ["sender_identities.id"],
            name=op.f("fk_sender_health_snapshots_sender_identity_id_sender_identities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_sender_health_snapshots_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sender_health_snapshots")),
        # Also the read path: "the last N days for this sender" is a prefix scan
        # on exactly this tuple, so no second index over the same columns.
        sa.UniqueConstraint(
            "workspace_id",
            "sender_identity_id",
            "captured_on",
            name="uq_sender_health_day",
        ),
    )
    op.create_index(
        op.f("ix_sender_health_snapshots_sender_identity_id"),
        TABLE,
        ["sender_identity_id"],
    )
    op.create_index(op.f("ix_sender_health_snapshots_status"), TABLE, ["status"])
    op.create_index(
        op.f("ix_sender_health_snapshots_workspace_id"), TABLE, ["workspace_id"]
    )
    op.create_index(
        "ix_sender_health_snapshots_ws_created", TABLE, ["workspace_id", "created_at"]
    )

    op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY {TABLE}_workspace_isolation ON "{TABLE}" '
        f"USING ({_ISOLATION}) WITH CHECK ({_ISOLATION})"
    )


def downgrade() -> None:
    op.execute(f'DROP POLICY IF EXISTS {TABLE}_workspace_isolation ON "{TABLE}"')
    op.drop_index("ix_sender_health_snapshots_ws_created", table_name=TABLE)
    op.drop_index(op.f("ix_sender_health_snapshots_workspace_id"), table_name=TABLE)
    op.drop_index(op.f("ix_sender_health_snapshots_status"), table_name=TABLE)
    op.drop_index(op.f("ix_sender_health_snapshots_sender_identity_id"), table_name=TABLE)
    op.drop_table(TABLE)
