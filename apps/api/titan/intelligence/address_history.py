"""What this exact address has already done when written to.

The layer the estate was missing, and the one a paid verification service is
actually selling. MillionVerifier does not know a mailbox is dead by reasoning
about it; it knows because it has watched that address bounce, across billions
of verifications accumulated over years. The algorithm is cheap. The history is
the product.

Titan generates that history on every send and, until this module, discarded
it. Measured on the live estate: **ten addresses had hard-bounced and not one
was marked invalid.** Six still carried ``published_first_party`` -- the
strongest status the system can assign -- after proving themselves dead. An
address that bounced in August, rediscovered in September on a second crawl of
the same site, came back through the pipeline with a clean record and was
eligible to be written to again.

**Why this is a separate layer rather than a fix to the verifier.** Everything
in :mod:`titan.intelligence.bounce_risk` before this point is inference: a
syntax rule, a domain reputation, an SMTP server's opinion. This is not an
inference. The mail was sent, and a mail server said the mailbox does not
exist. Nothing else in the stack carries that weight, which is why its signal
is ``REFUSE`` and why it is asked first.

**Domain history already existed and is not this.**
:class:`titan.intelligence.domain_health.DomainWindow` answers "does this
domain accept our mail", which its own code is careful to note says nothing
about whether a particular mailbox exists. This answers the mailbox question,
and only for mailboxes Titan has itself written to.

**It reads two sources because they decay differently.** ``suppression_entries``
is permanent and survives the lead being deleted and rediscovered, which is
exactly the case that motivated this. ``messages`` carries the per-send detail
and the soft bounces, which never reach suppression individually. Neither alone
is enough: suppression without messages loses the soft-bounce pattern, messages
without suppression loses everything older than the retention window.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from titan.intelligence.contacts import normalize_email

#: Soft bounces before the address is treated as doubtful rather than merely
#: unlucky. A single soft bounce is a full mailbox or a bad afternoon; three is
#: a pattern. Deliberately lower than the send path's own threshold, because
#: this runs at discovery where the cost of being wrong is one unsent lead
#: rather than a message that damages the domain.
SOFT_BOUNCES_BEFORE_DOUBT = 3


@dataclass(frozen=True, slots=True)
class AddressHistory:
    """What Titan has observed about one address, from its own sends.

    Absent (``None`` at the call site) means "nobody looked", never "looked and
    found nothing" -- the same convention every other optional layer in
    ``bounce_risk`` follows. ``AddressHistory(address)`` with all counts at zero
    is the second thing: asked, and this address has no history.
    """

    address: str
    hard_bounces: int = 0
    soft_bounces: int = 0
    complaints: int = 0
    #: Set when a permanent suppression exists, which outlives the messages.
    suppressed_reason: str | None = None
    last_bounce_at: dt.datetime | None = None

    @property
    def is_proven_dead(self) -> bool:
        """A mail server has said this mailbox does not exist."""
        return self.hard_bounces > 0 or self.suppressed_reason == "hard_bounce"

    @property
    def is_doubtful(self) -> bool:
        """Not proven dead, but behaving badly enough to hold back."""
        return self.soft_bounces >= SOFT_BOUNCES_BEFORE_DOUBT

    @property
    def has_complained(self) -> bool:
        return self.complaints > 0 or self.suppressed_reason == "complaint"

    def describe(self) -> str:
        if self.suppressed_reason == "hard_bounce":
            return "this address is suppressed for a previous hard bounce"
        if self.hard_bounces:
            when = (
                f" (last {self.last_bounce_at:%Y-%m-%d})" if self.last_bounce_at else ""
            )
            return (
                f"Titan has sent to this address before and it hard-bounced "
                f"{self.hard_bounces} time(s){when}"
            )
        if self.is_doubtful:
            return (
                f"{self.soft_bounces} soft bounces on previous sends to this "
                "address, which is a pattern rather than one bad afternoon"
            )
        return "no adverse history on previous sends to this address"


_HISTORY = text(
    """
    SELECT
      count(*) FILTER (WHERE m.bounce_kind = 'hard')            AS hard_bounces,
      count(*) FILTER (WHERE m.bounce_kind = 'soft')            AS soft_bounces,
      max(m.bounced_at)                                          AS last_bounce_at
      FROM messages m
     WHERE m.workspace_id = :ws
       AND m.to_email_normalized = :addr
       AND m.bounced_at IS NOT NULL
    """
)

#: Read separately and deliberately unscoped by time: a suppression is the
#: permanent record, and the whole point of this layer is that it outlives both
#: the message rows and the lead being rediscovered.
_SUPPRESSION = text(
    """
    SELECT reason::text AS reason
      FROM suppression_entries
     WHERE workspace_id = :ws
       AND normalized_value = :addr
     ORDER BY suppressed_at DESC
     LIMIT 1
    """
)


async def read_history(
    session: AsyncSession, *, workspace_id: uuid.UUID, email: str
) -> AddressHistory:
    """Everything Titan knows about this address from having written to it.

    Returns a zeroed history rather than None for an address with no record:
    the caller asked, and "nothing adverse" is an answer. ``None`` is reserved
    for call sites that did not ask at all.
    """
    address = normalize_email(email)
    if not address:
        return AddressHistory(address="")

    params = {"ws": workspace_id, "addr": address}
    row = (await session.execute(_HISTORY, params)).one_or_none()
    suppression = (await session.execute(_SUPPRESSION, params)).scalar_one_or_none()

    return AddressHistory(
        address=address,
        hard_bounces=int(row.hard_bounces or 0) if row else 0,
        soft_bounces=int(row.soft_bounces or 0) if row else 0,
        # Complaints are recorded as a suppression rather than on the message,
        # so there is no message-level count to read.
        complaints=1 if suppression == "complaint" else 0,
        suppressed_reason=suppression,
        last_bounce_at=row.last_bounce_at if row else None,
    )


_HISTORY_MANY = text(
    """
    SELECT m.to_email_normalized                                 AS addr,
           count(*) FILTER (WHERE m.bounce_kind = 'hard')        AS hard_bounces,
           count(*) FILTER (WHERE m.bounce_kind = 'soft')        AS soft_bounces,
           max(m.bounced_at)                                     AS last_bounce_at
      FROM messages m
     WHERE m.workspace_id = :ws
       AND m.to_email_normalized = ANY(:addrs)
       AND m.bounced_at IS NOT NULL
     GROUP BY 1
    """
)

_SUPPRESSION_MANY = text(
    """
    SELECT DISTINCT ON (normalized_value)
           normalized_value AS addr, reason::text AS reason
      FROM suppression_entries
     WHERE workspace_id = :ws
       AND normalized_value = ANY(:addrs)
     ORDER BY normalized_value, suppressed_at DESC
    """
)


async def read_many(
    session: AsyncSession, *, workspace_id: uuid.UUID, emails: list[str]
) -> dict[str, AddressHistory]:
    """History for a batch of addresses, in two queries rather than 2N.

    Discovery assesses every address on a site in one pass, so this follows the
    same shape as the bulk MX and domain-history reads beside it: resolve once
    before the loop, hand each candidate its own answer.

    Every requested address appears in the result, zeroed when nothing adverse
    is recorded -- the caller asked, and "nothing" is an answer. The workspace
    predicate is written out because raw SQL inherits no ORM scoping; see
    ``_domain_history`` in the pipeline for the full reasoning.
    """
    wanted = sorted({normalize_email(e) for e in emails if e})
    if not wanted:
        return {}

    params = {"ws": workspace_id, "addrs": wanted}
    found = {a: AddressHistory(address=a) for a in wanted}

    for row in (await session.execute(_HISTORY_MANY, params)).all():
        found[row.addr] = AddressHistory(
            address=row.addr,
            hard_bounces=int(row.hard_bounces or 0),
            soft_bounces=int(row.soft_bounces or 0),
            last_bounce_at=row.last_bounce_at,
        )

    for row in (await session.execute(_SUPPRESSION_MANY, params)).all():
        current = found.get(row.addr) or AddressHistory(address=row.addr)
        found[row.addr] = AddressHistory(
            address=current.address,
            hard_bounces=current.hard_bounces,
            soft_bounces=current.soft_bounces,
            complaints=1 if row.reason == "complaint" else current.complaints,
            suppressed_reason=row.reason,
            last_bounce_at=current.last_bounce_at,
        )

    return found


__all__ = [
    "SOFT_BOUNCES_BEFORE_DOUBT",
    "AddressHistory",
    "read_history",
    "read_many",
]
