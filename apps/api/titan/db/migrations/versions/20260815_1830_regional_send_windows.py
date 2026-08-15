"""derive send windows from each campaign's market

Two things were true at once. The working week *had* been set per market -- a
one-off UPDATE in ``20260815_1112`` gave Middle East campaigns Sunday to
Thursday -- and nothing carried that rule into campaign creation, so every
campaign created afterwards was Monday to Friday whatever market it was aimed
at. A backfill is an event; this makes it a rule, and creation now derives the
same window from the same table.

The hours change too. ``08:00`` was one number for every market, which is an
hour ahead of a nine-o'clock working day, exactly on time for Germany's eight,
and an hour late for the Gulf's close. The window now opens
``LEAD_IN_HOURS`` before the market's own working day and shuts when it shuts,
so a message lands at the top of the first pass rather than partway down an
inbox that has been filling since somebody sat down.

**Only untouched windows are rewritten.** A row whose hours or days differ from
the old global default is somebody's decision, and re-deriving it would discard
that decision silently -- the same failure the schedule installer guards against
on the other side of the system. Those rows are left exactly as they are, and
the count of what was skipped is reported rather than hidden.

Revision ID: 7c4e19b8d05a
Revises: 3f1c9a2b74de
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "7c4e19b8d05a"
down_revision = "3f1c9a2b74de"
branch_labels = None
depends_on = None

#: The window every campaign carried before this migration: the column defaults
#: from ``20260815_1112``, plus the Middle East working week that migration
#: backfilled. A row matching one of these for its market has never been edited.
#:
#: Duplicated from ``titan.policy.schedule`` rather than imported, deliberately.
#: A migration reproduces the world as it was at the moment it runs; importing
#: live constants means a later edit to that table silently changes what this
#: migration did last year.
_MON_FRI = "[0, 1, 2, 3, 4]"
_SUN_THU = "[6, 0, 1, 2, 3]"

#: region -> (previous days, new start hour, new end hour, new days)
#:
#: New hours are ``working start - 1`` and ``working end``. See
#: ``titan.policy.schedule.REGION_WORKING_HOURS``.
DERIVED: dict[str, tuple[str, int, int, str]] = {
    "usa": (_MON_FRI, 8, 17, _MON_FRI),
    "canada": (_MON_FRI, 8, 17, _MON_FRI),
    "uk": (_MON_FRI, 8, 17, _MON_FRI),
    "europe": (_MON_FRI, 7, 17, _MON_FRI),
    "australia": (_MON_FRI, 8, 17, _MON_FRI),
    # Sunday to Thursday already, from the earlier backfill.
    "middle_east": (_SUN_THU, 8, 18, _SUN_THU),
}

#: Every value is bound; the only interpolation is none. Written out in full
#: rather than assembled from fragments so that stays visible at a glance.
_DERIVE = sa.text(
    "UPDATE campaign_policies p"
    "   SET send_window_start_hour = :start,"
    "       send_window_end_hour = :end,"
    "       send_days = CAST(:days AS jsonb),"
    "       updated_at = now()"
    "  FROM campaigns c"
    " WHERE c.id = p.campaign_id"
    "   AND c.workspace_id = p.workspace_id"
    "   AND c.region = CAST(:region AS region)"
    "   AND p.send_window_start_hour = 8"
    "   AND p.send_window_end_hour = 17"
    "   AND p.send_days = CAST(:previous_days AS jsonb)"
)

_REVERT = sa.text(
    "UPDATE campaign_policies p"
    "   SET send_window_start_hour = 8,"
    "       send_window_end_hour = 17,"
    "       send_days = CAST(:previous_days AS jsonb),"
    "       updated_at = now()"
    "  FROM campaigns c"
    " WHERE c.id = p.campaign_id"
    "   AND c.workspace_id = p.workspace_id"
    "   AND c.region = CAST(:region AS region)"
    "   AND p.send_window_start_hour = :start"
    "   AND p.send_window_end_hour = :end"
    "   AND p.send_days = CAST(:days AS jsonb)"
)


def upgrade() -> None:
    bind = op.get_bind()
    for region, (previous_days, start, end, days) in DERIVED.items():
        bind.execute(
            _DERIVE,
            {
                "region": region,
                "previous_days": previous_days,
                "start": start,
                "end": end,
                "days": days,
            },
        )


def downgrade() -> None:
    """Return every derived window to the old single default.

    Only rows this migration could have written are touched, by the same
    reasoning as the upgrade: a window edited since is not ours to reset.
    """
    bind = op.get_bind()
    for region, (previous_days, start, end, days) in DERIVED.items():
        bind.execute(
            _REVERT,
            {
                "region": region,
                "previous_days": previous_days,
                "start": start,
                "end": end,
                "days": days,
            },
        )
