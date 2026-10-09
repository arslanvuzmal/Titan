"""merge the smartlead and passcode branches

Two migrations descend from ``ab42f11ce875`` and neither descends from the
other:

    868132205b6f
      └─ ab42f11ce875
           ├─ 7f3c81d0a4be  local passcode authentication  (this repository)
           └─ 4c1d9b7a2e50  smartlead lead import          (production)

Alembic will not move a database with two heads, which is why production --
stamped at ``4c1d9b7a2e50`` -- could not take any migration at all, including
ones that had nothing to do with either branch.

This revision joins them and does no work of its own. Everything it needs was
already expressed by its parents; a merge that also carried DDL would be a
migration whose effects depend on which branch a given database happened to
take, and that is the failure mode this whole reconciliation exists to end.

Applying it to production runs ``7f3c81d0a4be`` on the way, which is the branch
production never saw: that adds ``users.failed_login_count`` and
``users.locked_until``, and skips ``users.username``, which production already
has. After it, the live schema and this repository's models agree exactly.

Revision ID: 5e2a94c17b03
Revises: 7f3c81d0a4be, 4c1d9b7a2e50
Create Date: 2026-08-11 12:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "5e2a94c17b03"
down_revision: tuple[str, str] = ("7f3c81d0a4be", "4c1d9b7a2e50")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing. The parents did the work; this only rejoins the history."""


def downgrade() -> None:
    """Nothing. Splitting the history back apart is the parents' business."""
