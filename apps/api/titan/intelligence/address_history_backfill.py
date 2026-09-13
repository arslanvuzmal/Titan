"""Mark the addresses that already bounced, before anyone looked.

The write-back in :func:`titan.delivery.bounces._suppress_and_stop` fixes every
bounce from now on. It does nothing for the bounces that already happened, and
those are the ones that motivated the work: ten addresses on the live estate
had hard-bounced and not one was marked invalid, six of them still carrying
``published_first_party``.

**Evidence-only.** An address is marked here because a mail server refused it,
recorded either as a ``hard_bounce`` suppression or a ``hard`` bounce on a
message Titan sent. Nothing is inferred, nothing is guessed from a pattern, and
an address with only soft bounces is left alone -- it is doubtful, not dead,
and downgrading it is a judgement the live path makes with more context than a
backfill has.

Dry by default. :func:`survey` reports; :func:`apply` writes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text

from titan.db.session import get_sessionmaker

#: Addresses a mail server has permanently refused, by either record.
#:
#: The union matters. A suppression outlives the message rows and survives the
#: lead being rediscovered; the message rows carry bounces that predate the
#: suppression logic or arrived without threading back to a suppression. Either
#: alone would miss real cases.
#:
#: Read into Python and passed back as a bound array rather than composed into
#: the statements below. The alternative -- interpolating this fragment into
#: both -- is the idiom three other queries in this codebase already use, but it
#: is interpolation into SQL, and the only thing it buys over a bound parameter
#: is one fewer round trip against a list of tens of addresses.
_DEAD = text(
    """
    SELECT s.normalized_value AS addr
      FROM suppression_entries s
     WHERE s.workspace_id = :ws
       AND s.reason::text = 'hard_bounce'
    UNION
    SELECT m.to_email_normalized AS addr
      FROM messages m
     WHERE m.workspace_id = :ws
       AND m.bounce_kind = 'hard'
       AND m.to_email_normalized IS NOT NULL
    """
)

_SURVEY = text(
    """
    SELECT cc.verification_status::text AS current_status, count(*) AS addresses
      FROM contact_channels cc
     WHERE cc.workspace_id = :ws
       AND cc.verification_status::text <> 'invalid'
       AND cc.normalized_value = ANY(:addrs)
     GROUP BY 1
     ORDER BY 2 DESC
    """
)

_APPLY = text(
    """
    UPDATE contact_channels cc
       SET verification_status = 'invalid'
     WHERE cc.workspace_id = :ws
       AND cc.verification_status::text <> 'invalid'
       AND cc.normalized_value = ANY(:addrs)
    """
)


@dataclass
class BackfillReport:
    #: How many channels would move, keyed by the status they hold now. A run
    #: that finds everything already at published_first_party says something
    #: different from one spread across statuses.
    by_current_status: dict[str, int] = field(default_factory=dict)
    applied: bool = False

    @property
    def total(self) -> int:
        return sum(self.by_current_status.values())

    @property
    def is_noop(self) -> bool:
        return self.total == 0


async def survey(workspace_id: uuid.UUID) -> BackfillReport:
    """What would be marked invalid. Writes nothing."""
    return await _run(workspace_id, apply_changes=False)


async def apply(workspace_id: uuid.UUID) -> BackfillReport:
    """Mark them. Idempotent: only ever moves a channel *to* invalid."""
    return await _run(workspace_id, apply_changes=True)


async def _run(workspace_id: uuid.UUID, *, apply_changes: bool) -> BackfillReport:
    report = BackfillReport(applied=apply_changes)
    params = {"ws": workspace_id}

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        dead = [r.addr for r in (await session.execute(_DEAD, params)).all() if r.addr]
        if not dead:
            return report

        scoped = {**params, "addrs": dead}
        for row in (await session.execute(_SURVEY, scoped)).all():
            report.by_current_status[row.current_status] = int(row.addresses)

        if apply_changes and report.total:
            await session.execute(_APPLY, scoped)
            await session.commit()

    return report


__all__ = ["BackfillReport", "apply", "survey"]
