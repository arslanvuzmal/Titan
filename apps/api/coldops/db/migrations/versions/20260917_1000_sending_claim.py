"""One host may send. Written down, in a place a database copy carries with it.

On 16 September the estate moved to a server and the laptop's containers were
never stopped. Both hosts held the same restored drafts and rotated mailboxes
independently, so 29 businesses received the same pitch twice, from two
different addresses on the same domain, minutes apart. Neither database could
see the other, so from inside each one the day looked entirely normal.

The outbox already leases rows, but that arbitrates between workers sharing one
database. It cannot help here: the laptop's database was a `pg_restore` of the
server's, so the two estates agreed about every row and shared no state at all.

What they did share is the dump. A row naming the host permitted to send
travels inside it, so a restored copy can be told what it is -- it reads a
holder that is not itself and stops. That is the whole idea, and it is why this
is a *claim* rather than a lease: a lease with an expiry would let the copy
take over the moment the original's heartbeat aged out, which is precisely the
wrong outcome. Moving hosts is meant to be a decision, not a timeout.

Revision ID: a1f4c7e20b95
Revises: c4a7e1b93d26
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1f4c7e20b95"
down_revision = "c4a7e1b93d26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sending_claims",
        # One row per thing that may be claimed. Only 'outbox' today; named
        # rather than implied so a second claimable thing does not need a
        # second table.
        sa.Column("scope", sa.String(length=40), primary_key=True),
        # Deliberately not the container's hostname, which changes every time
        # the container is recreated -- an identity that moves on restart would
        # make the real sender refuse its own claim and stop all sending.
        sa.Column("host_id", sa.String(length=200), nullable=False),
        sa.Column("host_label", sa.String(length=200), nullable=False, server_default=""),
        sa.Column(
            "claimed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Advisory only. Nothing reads it to decide anything -- it exists so an
        # operator can see how long the other host has been quiet before
        # deciding to take the claim.
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_table("sending_claims")
