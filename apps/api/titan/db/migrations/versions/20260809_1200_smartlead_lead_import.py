"""smartlead lead import

**Recovered, not authored.** This migration ran against the production database
from a branch that was never pushed, and this repository lost the file. The
stamped revision therefore named something nobody had, and Alembic refused to
run against production at all -- every later migration was blocked behind it.

Reconstructed on 2026-08-11 by reflecting the live schema: column types,
nullability, defaults, indexes, foreign keys, check constraints and enum labels
were read out of ``information_schema`` and ``pg_enum`` rather than recalled.
A reconstruction that differs from the schema it describes is worse than none,
because it makes a fresh database silently unlike production and the difference
surfaces months later as a constraint nobody can explain.

The revision id is deliberately the one production already carries. Any other
value would leave the live database stamped at a revision that still exists
nowhere, which is the entire defect being fixed.

**What it describes.** A lead-import design: push leads into a Smartlead
campaign and let Smartlead send to them on a sequence. This repository does not
send that way -- see ``titan/db/models/smartlead.py`` for why the carrier
design replaced it -- but six batches ran, seventeen leads were genuinely
imported, and all 295 leads carry a status. Dropping the schema would destroy
the only record of what the deployed system did.

Revision ID: 4c1d9b7a2e50
Revises: ab42f11ce875
Create Date: 2026-08-09 12:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4c1d9b7a2e50"
down_revision: str | None = "ab42f11ce875"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMPORT_STATUS = postgresql.ENUM(
    "not_imported",
    "reserved",
    "imported",
    "skipped",
    "failed",
    name="smartlead_import_status",
    create_type=False,
)
BATCH_STATUS = postgresql.ENUM(
    "dry_run",
    "running",
    "completed",
    "partial",
    "failed",
    name="smartlead_batch_status",
    create_type=False,
)
EVENT_TYPE = postgresql.ENUM(
    "sent",
    "opened",
    "clicked",
    "replied",
    "bounced",
    "unsubscribed",
    "unknown",
    name="smartlead_event_type",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()

    # checkfirst so this is safe on the production database, which already has
    # all three. It is stamped at this revision and will never replay it, but a
    # recovered migration that cannot survive being replayed is a trap for
    # whoever next restores a backup.
    for enum_type in (IMPORT_STATUS, BATCH_STATUS, EVENT_TYPE):
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "smartlead_import_batches",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("smartlead_campaign_id", sa.String(length=40), nullable=False),
        sa.Column("is_sandbox", sa.Boolean(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("status", BATCH_STATUS, nullable=False),
        sa.Column("leads_requested", sa.Integer(), nullable=False),
        sa.Column("leads_eligible", sa.Integer(), nullable=False),
        sa.Column("leads_imported", sa.Integer(), nullable=False),
        sa.Column("leads_skipped", sa.Integer(), nullable=False),
        sa.Column("leads_failed", sa.Integer(), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcomes", postgresql.JSONB(), nullable=False),
        sa.Column("provider_response", postgresql.JSONB(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "leads_requested >= 0 AND leads_eligible >= 0 AND leads_imported >= 0 "
            "AND leads_skipped >= 0 AND leads_failed >= 0",
            name=op.f("ck_smartlead_import_batches_counts_non_negative"),
        ),
        sa.CheckConstraint(
            "leads_imported <= leads_eligible",
            name=op.f("ck_smartlead_import_batches_imported_within_eligible"),
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f("fk_smartlead_import_batches_campaign_id_campaigns"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_smartlead_import_batches_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_smartlead_import_batches_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_smartlead_import_batches")),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name=op.f("uq_smartlead_import_batches_workspace_id_idempotency_key"),
        ),
    )
    op.create_index(
        op.f("ix_smartlead_import_batches_workspace_id"),
        "smartlead_import_batches",
        ["workspace_id"],
    )
    op.create_index(
        "ix_smartlead_import_batches_ws_created",
        "smartlead_import_batches",
        ["workspace_id", "created_at"],
    )

    op.create_table(
        "smartlead_webhook_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.Column("event_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("raw_event_type", sa.String(length=60), nullable=False),
        sa.Column("event_type", EVENT_TYPE, nullable=False),
        sa.Column("smartlead_campaign_id", sa.String(length=40), nullable=False),
        sa.Column("smartlead_lead_id", sa.String(length=64), nullable=True),
        sa.Column("normalized_email", sa.String(length=320), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signature_verified", sa.Boolean(), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("ignored", sa.Boolean(), nullable=False),
        sa.Column("ignored_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_smartlead_webhook_events_lead_id_leads"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_smartlead_webhook_events_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_smartlead_webhook_events_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_smartlead_webhook_events")),
        sa.UniqueConstraint("event_fingerprint", name="uq_smartlead_events_fingerprint"),
    )
    for name, columns in (
        ("ix_smartlead_webhook_events_workspace_id", ["workspace_id"]),
        ("ix_smartlead_webhook_events_provider_request_id", ["provider_request_id"]),
        ("ix_smartlead_webhook_events_smartlead_lead_id", ["smartlead_lead_id"]),
        ("ix_smartlead_webhook_events_ws_created", ["workspace_id", "created_at"]),
        ("ix_smartlead_events_lead", ["lead_id"]),
        (
            "ix_smartlead_events_campaign_email",
            ["smartlead_campaign_id", "normalized_email"],
        ),
    ):
        op.create_index(name, "smartlead_webhook_events", columns)

    # ---------------------------------------------------------------- leads
    op.add_column(
        "leads",
        sa.Column(
            "smartlead_status",
            IMPORT_STATUS,
            server_default="not_imported",
            nullable=False,
        ),
    )
    for column in (
        sa.Column("smartlead_campaign_id", sa.String(length=40), nullable=True),
        sa.Column("smartlead_normalized_email", sa.String(length=320), nullable=True),
        sa.Column("smartlead_lead_id", sa.String(length=64), nullable=True),
        sa.Column(
            "smartlead_import_batch_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("smartlead_imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("smartlead_last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("smartlead_skipped_reason", sa.Text(), nullable=True),
        sa.Column("smartlead_last_error", sa.Text(), nullable=True),
        sa.Column("smartlead_last_event", sa.String(length=40), nullable=True),
        sa.Column("smartlead_last_event_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("leads", column)

    op.create_foreign_key(
        op.f("fk_leads_smartlead_import_batch_id_smartlead_import_batches"),
        "leads",
        "smartlead_import_batches",
        ["smartlead_import_batch_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_leads_smartlead_lead_id", "leads", ["smartlead_lead_id"])
    op.create_index(
        "ix_leads_smartlead_status", "leads", ["workspace_id", "smartlead_status"]
    )
    # Both columns are null for a lead never imported, and PostgreSQL does not
    # treat two null tuples as equal, so this constrains imported leads only.
    op.create_index(
        "uq_leads_smartlead_campaign_email",
        "leads",
        ["smartlead_campaign_id", "smartlead_normalized_email"],
        unique=True,
    )

    # --------------------------------------------------------- lead_sources
    op.add_column(
        "lead_sources",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        op.f("uq_lead_sources_workspace_id_idempotency_key"),
        "lead_sources",
        ["workspace_id", "idempotency_key"],
    )

    # ----------------------------------------------------------------- users
    # Also added on this branch. 7f3c81d0a4be adds it too, on the sibling
    # branch, and tolerates finding it already present -- see the guard there.
    op.add_column("users", sa.Column("username", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_users_username", "users", ["username"])


def downgrade() -> None:
    op.drop_constraint("uq_users_username", "users", type_="unique")
    op.drop_column("users", "username")

    op.drop_constraint(
        op.f("uq_lead_sources_workspace_id_idempotency_key"),
        "lead_sources",
        type_="unique",
    )
    op.drop_column("lead_sources", "idempotency_key")

    op.drop_index("uq_leads_smartlead_campaign_email", table_name="leads")
    op.drop_index("ix_leads_smartlead_status", table_name="leads")
    op.drop_index("ix_leads_smartlead_lead_id", table_name="leads")
    op.drop_constraint(
        op.f("fk_leads_smartlead_import_batch_id_smartlead_import_batches"),
        "leads",
        type_="foreignkey",
    )
    for name in (
        "smartlead_last_event_at",
        "smartlead_last_event",
        "smartlead_last_error",
        "smartlead_skipped_reason",
        "smartlead_last_synced_at",
        "smartlead_imported_at",
        "smartlead_import_batch_id",
        "smartlead_lead_id",
        "smartlead_normalized_email",
        "smartlead_campaign_id",
        "smartlead_status",
    ):
        op.drop_column("leads", name)

    op.drop_table("smartlead_webhook_events")
    op.drop_table("smartlead_import_batches")

    bind = op.get_bind()
    for enum_type in (EVENT_TYPE, BATCH_STATUS, IMPORT_STATUS):
        enum_type.drop(bind, checkfirst=True)
