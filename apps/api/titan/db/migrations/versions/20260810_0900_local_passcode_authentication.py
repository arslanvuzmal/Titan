"""local passcode authentication

Adds the sign-in handle and the lockout counters that turn `/auth/token` from
"knowing an email address" into "knowing a secret".

`password_hash` already exists (initial schema) and is untouched here. Every
existing row keeps NULL in it, which means no account gains the ability to sign
in as a side effect of this migration -- an operator has to set a passcode
explicitly with `titan set-passcode`.

Note for the deployed database: it already carries a `users.username` column
from a schema this repository has never seen (live revision 4c1d9b7a2e50).
Alembic cannot run against it until that divergence is reconciled, so this
migration is written for a database at ab42f11ce875 and the reconciliation is a
separate, deliberate step -- see deploy/railway/README.md.

Revision ID: 7f3c81d0a4be
Revises: ab42f11ce875
Create Date: 2026-08-10 09:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7f3c81d0a4be"
down_revision: str | None = "ab42f11ce875"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_username() -> bool:
    """Whether ``users.username`` is present right now.

    Both this branch and 4c1d9b7a2e50 create the column, so neither may assume
    its own state: on a database that took the sibling path first, the column
    is already there on the way up and already gone on the way down.
    """
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'username'"
            )
        )
        .scalar()
    )


def upgrade() -> None:
    # `username` is added by 4c1d9b7a2e50 as well, on the sibling branch that
    # ran in production before this repository had the file. Both branches
    # converge at the merge revision, so on any database that took the other
    # path first the column is already present and a bare add_column would
    # abort the whole upgrade -- which is exactly what it did.
    #
    # Guarded rather than reordered: the two branches are siblings, so neither
    # can be said to run first, and whichever arrives second has to cope.
    if not _has_username():
        op.add_column("users", sa.Column("username", sa.String(length=64), nullable=True))
        op.create_unique_constraint("uq_users_username", "users", ["username"])
    op.add_column(
        "users",
        sa.Column(
            "failed_login_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "users",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    # The default exists to backfill existing rows; leaving it in place would
    # let an INSERT that forgets the column silently succeed against the ORM's
    # expectations.
    op.alter_column("users", "failed_login_count", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_count")
    # Guarded for the same reason as the upgrade: 4c1d9b7a2e50 drops this too,
    # and unwinding a merge runs both parents in an order the history does not
    # fix. Whichever goes second finds the column already gone.
    if _has_username():
        op.drop_constraint("uq_users_username", "users", type_="unique")
        op.drop_column("users", "username")
