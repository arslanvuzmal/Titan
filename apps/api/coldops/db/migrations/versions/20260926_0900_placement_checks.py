"""Where the mail actually landed, recorded rather than assumed.

On 26 September a seed test put the same message in front of three mailboxes
the operator owns: two Gmail, one Outlook. All three went to spam. 944 messages
had been sent before that test and not one had produced a reply, and until the
test nobody could tell whether that meant a bad offer or an unread inbox. They
are opposite problems with opposite fixes, and two months were spent guessing.

No mail provider will tell you which folder an individual message reached --
not Gmail, not Outlook, not any ESP. That is not a gap in this system, it is
how the medium works. Placement is only observable by sending to a mailbox you
control and looking.

So this table is the memory of doing exactly that: one row per probe per seed
address, carrying the folder it was found in and when. A single reading is
weather; the value is the series, because reputation moves slowly and the only
way to know a warm-up is working is to watch the same probe stop being junked.

Deliberately not a per-message column on `messages`. Attaching a folder to a
real send would invent a fact about a stranger's mailbox that nobody measured.
Only probes to our own seeds can carry this, and keeping them in their own
table is what stops the two ever being confused.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b8d2f9a14c73"
down_revision = "a1f4c7e20b95"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "placement_checks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        #: Ties the probes of one round together, so a round can be read as a
        #: row across providers rather than three unrelated readings.
        sa.Column("probe_token", sa.String(64), nullable=False),
        sa.Column("seed_address", sa.String(320), nullable=False),
        #: gmail / outlook / other. Kept as free text because the interesting
        #: comparison is between providers and a new one must not need a
        #: migration to start being measured.
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("from_email", sa.String(320), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("had_attachment", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        #: NULL until somebody or something looks. An unchecked probe is not a
        #: delivered one, and the report has to be able to say so.
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        #: inbox | spam | promotions | missing | unknown
        sa.Column("folder", sa.String(24), nullable=True),
        #: "imap" when a machine looked, "manual" when a person did. Worth
        #: keeping: a human glance is the less reliable instrument and a series
        #: that mixes them should say which is which.
        sa.Column("checked_by", sa.String(16), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_placement_checks_ws_sent",
        "placement_checks",
        ["workspace_id", "sent_at"],
    )
    # One row per seed per probe round: re-checking a probe updates the folder
    # rather than appending a second opinion about the same message.
    op.create_unique_constraint(
        "uq_placement_checks_probe_seed",
        "placement_checks",
        ["probe_token", "seed_address"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_placement_checks_probe_seed", "placement_checks", type_="unique"
    )
    op.drop_index("ix_placement_checks_ws_sent", table_name="placement_checks")
    op.drop_table("placement_checks")
