"""Which host is allowed to send.

The guard for the 16 September double-send, in which the laptop and the server
worked the same restored queue for eleven hours and 29 businesses received one
pitch twice, from two addresses on the same domain.

The reason nothing caught it is worth stating, because it rules out the obvious
fixes. The outbox already leases rows, and the two hosts still collided -- row
leases arbitrate between workers sharing a database, and these had a database
each, identical because one was a `pg_restore` of the other. Nothing either
host could read said anything about the other. Message-IDs and campaign headers
are issued independently, so those agreed too.

The one thing the two estates genuinely shared was the dump. So the permission
lives in the database, where a copy carries it: a restored estate reads a
holder that is not itself and declines. That is also why this is a claim and
not a lease. A lease expires, and an expiring lease hands the copy exactly what
we are trying to deny it -- the right to take over once the original stops
heartbeating. Moving hosts is a decision, taken once, by a person.

Failure is closed on purpose. A host that cannot establish it holds the claim
does not send. The expensive error here is two senders, not a paused one: a
pause is visible within the hour in the daily report, whereas a duplicate is
invisible from inside either host and lands in a stranger's inbox.
"""

from __future__ import annotations

import dataclasses
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The only scope today. Named rather than implied so that adding, say, a
#: research claim later does not mean reinterpreting existing rows.
OUTBOX = "outbox"


@dataclasses.dataclass(frozen=True)
class ClaimVerdict:
    """Whether this host may send, and what to say if not."""

    may_send: bool
    reason: str
    holder: str | None = None


#: Take the claim if nobody holds it, or if we already do. Never otherwise --
#: there is deliberately no expiry and no takeover.
#:
#: `ON CONFLICT ... WHERE` is what makes this safe under concurrency: two hosts
#: inserting at the same instant are serialised by the primary key, and the
#: loser's UPDATE is filtered out by the WHERE rather than overwriting. It
#: returns no row, which reads as "someone else holds it" -- the correct answer.
_CLAIM = text(
    """
    INSERT INTO sending_claims (scope, host_id, host_label, note)
    VALUES (:scope, :host_id, :host_label, :note)
    ON CONFLICT (scope) DO UPDATE
       SET heartbeat_at = now(),
           host_label = EXCLUDED.host_label
     WHERE sending_claims.host_id = EXCLUDED.host_id
    RETURNING host_id
    """
)

_HOLDER = text("SELECT host_id, heartbeat_at FROM sending_claims WHERE scope = :scope")

_FORCE = text(
    """
    INSERT INTO sending_claims (scope, host_id, host_label, claimed_at, heartbeat_at, note)
    VALUES (:scope, :host_id, :host_label, now(), now(), :note)
    ON CONFLICT (scope) DO UPDATE
       SET host_id = EXCLUDED.host_id,
           host_label = EXCLUDED.host_label,
           claimed_at = now(),
           heartbeat_at = now(),
           note = EXCLUDED.note
    RETURNING host_id
    """
)


async def hold(
    session: AsyncSession,
    *,
    host_id: str,
    host_label: str = "",
    scope: str = OUTBOX,
    note: str = "",
) -> ClaimVerdict:
    """Establish that this host holds the claim, taking it if it is free.

    Called on every poll cycle rather than once at startup. A worker that
    checked only at boot would keep sending for as long as it stayed up after
    somebody moved the claim elsewhere, which is the same eleven-hour overlap
    in a different costume.
    """
    if not host_id:
        # Not a guess-worthy condition. An empty identity would make every host
        # look like every other, which is worse than not having the guard.
        return ClaimVerdict(
            False,
            "TITAN_SENDER_HOST_ID is not set, so this host cannot prove which one it is",
        )

    won = await session.scalar(
        _CLAIM,
        {"scope": scope, "host_id": host_id, "host_label": host_label, "note": note},
    )
    if won is not None:
        return ClaimVerdict(True, "holds the sending claim", holder=host_id)

    row = (await session.execute(_HOLDER, {"scope": scope})).first()
    holder = row[0] if row else None
    return ClaimVerdict(
        False,
        (
            f"another host holds the sending claim ({holder!r}); this host is "
            f"{host_id!r}. If this estate is the one that should send, run "
            f"`titan sending claim --force`."
        ),
        holder=holder,
    )


async def current_holder(session: AsyncSession, *, scope: str = OUTBOX) -> str | None:
    row = (await session.execute(_HOLDER, {"scope": scope})).first()
    return row[0] if row else None


async def take(
    session: AsyncSession,
    *,
    host_id: str,
    host_label: str = "",
    scope: str = OUTBOX,
    note: str = "",
) -> str:
    """Move the claim to this host, whoever held it.

    The deliberate act. Separated from `hold` so that taking a claim away from
    a live sender can never happen as a side effect of a poll cycle.
    """
    if not host_id:
        raise ValueError("refusing to claim sending with an empty host id")
    holder = await session.scalar(
        _FORCE,
        {"scope": scope, "host_id": host_id, "host_label": host_label, "note": note},
    )
    return str(holder)


__all__ = ["OUTBOX", "ClaimVerdict", "current_holder", "hold", "take"]
