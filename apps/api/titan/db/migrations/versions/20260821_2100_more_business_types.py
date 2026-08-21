"""five more trades the catalogue could not name

Titan discovered 2,756 businesses across six industries and 1,181 of them were
dentists. The narrowness was not a discovery bug: a campaign searches for
whatever ``target_business_type`` says, and the industry it stamps on every
organisation it finds has to be a value this enum already holds. Six values, six
kinds of business.

Everything else keys off that value -- the playbook that decides which offers
may be proposed, and now the vernacular that decides what the message actually
says. A veterinary practice created as ``general`` gets the general voice, which
is honest but is not the practice's own words, and the whole point of the
vernacular is that it is.

So the enum is the catalogue. These five are all appointment trades that book by
telephone during office hours, which is precisely the gap Titan sells into.

Postgres cannot remove an enum value, so this does not go backwards. That is a
property of the database and not an oversight: the downgrade says so rather than
pretending, because a downgrade that silently does nothing is worse than one
that refuses.
"""

from __future__ import annotations

from alembic import op

revision = "d5c1e8a02f47"
down_revision = "c62d80f1ba37"
branch_labels = None
depends_on = None

NEW_VALUES = (
    "veterinary",
    "accountant",
    "optician",
    "physiotherapy",
    "salon_barber",
)


def upgrade() -> None:
    # IF NOT EXISTS so a database that has been ahead of the migration -- which
    # has happened on this project -- converges rather than failing.
    for value in NEW_VALUES:
        op.execute(f"ALTER TYPE industry ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    raise NotImplementedError(
        "Postgres cannot drop an enum value. Reversing this means recreating "
        "the type and rewriting every column that uses it, which is not "
        "something a downgrade should do unattended while rows may hold the "
        "values being removed."
    )
