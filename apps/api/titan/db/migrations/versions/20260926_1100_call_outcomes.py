"""Calling costs an hour of the operator's day; record what it bought.

Email is free and its failure mode is a silent spam folder. Calling is the
opposite: each attempt spends real minutes that cannot be spent twice, so the
expensive thing is not a bad call, it is calling the wrong practice or calling
the right one twice.

Two stages, because the first voice on a dental practice line is a receptionist
whose job includes filtering sales calls. Stage one is forty seconds and asks
for a name and an email, which a receptionist can give without deciding
anything. Stage two is the qualifying conversation with whoever they named.

BANT rather than MEDDIC, deliberately. MEDDIC needs discovery discipline and
half an hour; attempted in a three-minute SMB call it produces plausible
answers to every field and knowledge of none. A dental practice has one or two
people who decide, so Authority is a single question with a real answer, which
is exactly the shape BANT is for.

The columns are nullable on purpose. A call that ends at "she's with a patient"
has an outcome and no BANT, and forcing a score onto it would put invented
qualification into the record -- the same failure as a research run that
reported success because nothing wrote the real outcome.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c9e3a15d8f42"
down_revision = "b8d2f9a14c73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_outcomes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phone_e164", sa.String(20), nullable=False),
        #: 1 = reach the practice and get a name; 2 = qualify the named person.
        sa.Column("stage", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        #: no_answer | gatekeeper | reached_dm | callback | not_interested |
        #: do_not_call | wrong_number | interested
        sa.Column("outcome", sa.String(24), nullable=False),
        #: What stage one exists to capture. An email obtained by consent on a
        #: call is worth more than any address a crawler ever found: it is a
        #: named human who agreed to hear from us, which is the one thing 944
        #: cold messages never produced.
        sa.Column("contact_name", sa.String(200), nullable=True),
        sa.Column("contact_role", sa.String(120), nullable=True),
        sa.Column("contact_email", sa.String(320), nullable=True),
        sa.Column("consent_to_email", sa.Boolean(), nullable=False, server_default=sa.false()),
        #: BANT, each nullable because a call can end before reaching it.
        sa.Column("bant_budget", sa.SmallInteger(), nullable=True),
        sa.Column("bant_authority", sa.SmallInteger(), nullable=True),
        sa.Column("bant_need", sa.SmallInteger(), nullable=True),
        sa.Column("bant_timing", sa.SmallInteger(), nullable=True),
        #: Whether the defect we rang about was news to them. The single most
        #: useful thing a call can establish: a practice that already knows and
        #: has not fixed it is a different prospect from one hearing it first.
        sa.Column("knew_about_defect", sa.Boolean(), nullable=True),
        sa.Column("callback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_call_outcomes_ws_called", "call_outcomes", ["workspace_id", "called_at"]
    )
    op.create_index("ix_call_outcomes_lead", "call_outcomes", ["lead_id"])

    # A hard stop, separate from the outcome log.
    #
    # "Do not call again" has to be answerable before dialling, in one cheap
    # lookup, by a process that may know nothing else about the lead. Deriving
    # it from the outcome history would make the check a scan and -- worse --
    # would let a new outcome row quietly reopen a number somebody asked us to
    # stop ringing.
    op.create_table(
        "call_suppressions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phone_e164", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_unique_constraint(
        "uq_call_suppressions_ws_phone",
        "call_suppressions",
        ["workspace_id", "phone_e164"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_call_suppressions_ws_phone", "call_suppressions", type_="unique"
    )
    op.drop_table("call_suppressions")
    op.drop_index("ix_call_outcomes_lead", table_name="call_outcomes")
    op.drop_index("ix_call_outcomes_ws_called", table_name="call_outcomes")
    op.drop_table("call_outcomes")
