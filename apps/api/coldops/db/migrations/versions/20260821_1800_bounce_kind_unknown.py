"""the bounce kind the code already writes, which the column cannot hold

``BounceKind.UNKNOWN`` was added because Smartlead reports ``is_bounced`` and no
diagnostic, and recording the absence of a diagnosis as SOFT borrows confidence
nobody earned. The enum gained the value and the reader was updated to
``bounce_kind IN ('soft', 'unknown')``. The column was not.

It is ``varchar(4)`` with ``CHECK (bounce_kind IN ('hard', 'soft'))``. Writing
``'unknown'`` fails twice over -- proven directly against a database:

    ERROR:  value too long for type character varying(4)

Nothing has noticed because nothing has bounced since: sending stopped when the
carrier plan expired. **The first bounce after sending resumes would raise
inside the delivery-events activity**, and the whole poll -- every reply and
every other bounce in that batch -- would roll back with it.

**The five existing rows are relabelled.** Every bounce in this workspace came
from the Smartlead statistics poller, which supplies no diagnostic, and was
written as SOFT by the code that predates UNKNOWN. SOFT is a finding: a DSN
carrying a 4.x.x code, a real mailbox temporarily unable to accept. These carry
no such finding, and leaving them labelled as one would let a mailbox-health fix
that correctly excludes soft bounces silently clear a mailbox on the strength of
a label that was never true.

Identified by ``dedupe_key LIKE 'smartlead:%'`` rather than by date: provenance
is the thing that makes them unknown, and it is recorded on the row.

Revision ID: c62d80f1ba37
Revises: b7d419e0c3a5
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c62d80f1ba37"
down_revision = "b7d419e0c3a5"
branch_labels = None
depends_on = None

_ALLOWED = "bounce_kind IS NULL OR bounce_kind IN ('hard', 'soft', 'unknown')"
_OLD_ALLOWED = "bounce_kind IS NULL OR bounce_kind IN ('hard', 'soft')"


def upgrade() -> None:
    # Drop first: the constraint is checked against the old column type, and
    # widening under it is refused on some versions.
    op.drop_constraint(
        # op.f marks the name as already final: without it the naming
        # convention prefixes it again, into ck_messages_ck_messages_...
        op.f("ck_messages_bounce_kind_allowed"),
        "messages",
        type_="check",
    )
    op.alter_column(
        "messages",
        "bounce_kind",
        existing_type=sa.String(length=4),
        type_=sa.String(length=8),
        existing_nullable=True,
    )
    op.create_check_constraint("bounce_kind_allowed", "messages", _ALLOWED)

    # A bounce the provider never explained is not a soft bounce.
    op.execute(
        """
        UPDATE messages
           SET bounce_kind = 'unknown'
         WHERE bounce_kind = 'soft'
           AND dedupe_key LIKE 'smartlead:%'
        """
    )


def downgrade() -> None:
    # Anything genuinely unknown becomes soft again, which is what it was
    # called before this migration. Lossy in one direction only, and the
    # direction that does not lose a hard bounce.
    op.execute("UPDATE messages SET bounce_kind = 'soft' WHERE bounce_kind = 'unknown'")
    op.drop_constraint(
        # op.f marks the name as already final: without it the naming
        # convention prefixes it again, into ck_messages_ck_messages_...
        op.f("ck_messages_bounce_kind_allowed"),
        "messages",
        type_="check",
    )
    op.alter_column(
        "messages",
        "bounce_kind",
        existing_type=sa.String(length=8),
        type_=sa.String(length=4),
        existing_nullable=True,
    )
    op.create_check_constraint("bounce_kind_allowed", "messages", _OLD_ALLOWED)
