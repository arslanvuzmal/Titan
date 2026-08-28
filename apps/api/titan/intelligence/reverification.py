"""Re-checking addresses that were stored before there was anything to check with.

Verification runs at discovery, which is the right place for it: a network
round trip to a mail server does not belong inside the lease the outbox worker
holds while it sends. The consequence is that every address discovered while
``TITAN_MAILBOX_VERIFIER`` was ``null`` carries the answer that was available
then -- none. 611 of them on the live workspace, every one at
``published_first_party``: sendable on provenance, and never actually checked.

This is the catch-up pass. It is not a second verification pipeline. It reads
the same channels, calls the same verifier, and resolves the answer through the
same :func:`titan.intelligence.bounce_risk.assess` the discovery path uses, so
an address re-checked here lands in exactly the state it would have landed in
had the verifier been configured on the day it was found.

**It can only downgrade what provenance already granted, or leave it alone.**
That is not a rule imposed here; it falls out of ``assess`` resolving every
layer in one place. Worth stating because the temptation with a catch-up job is
to write the verifier's answer straight onto the channel, which would let a
``CATCH_ALL`` domain overwrite a first-party address that a human read off the
company's own contact page.

**The history is appended, never rewritten.** ``ContactVerification`` is
append-only, and a re-check is a genuinely new check -- unlike an activity
retry, which is why the discovery path guards against appending on one. The
channel's current status is the mutable summary; the row is the record of
having asked.

A row is written for **every** address checked, not only for the ones whose
status moved. The first full pass wrote 53 rows for 584 checks, which meant
531 addresses had been asked about and nothing recorded it -- so a second run
would have opened 531 more connections to servers that had already answered.
Nothing was wrong with the answers; there was simply no way to tell they had
happened. ``find_recheckable`` now skips anything asked about inside
:data:`RECHECK_AFTER`, which is what makes this command safe to re-run.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import VerificationStatus, verification_permits_sending
from titan.db.models import ContactChannel, ContactVerification
from titan.intelligence.bounce_risk import assess
from titan.intelligence.mx import MxCheck, MxResolver, check_many, system_mx_resolver
from titan.intelligence.verifier import MailboxVerifier

logger = logging.getLogger(__name__)

#: The statuses worth spending a check on.
#:
#: A channel already ``PROVIDER_VERIFIED``, ``CATCH_ALL`` or ``INVALID`` has a
#: conclusive answer recorded and re-asking buys nothing. ``RISKY`` is left out
#: for the opposite reason: it usually means a full mailbox, which is a real
#: person, and re-checking it every run would probe the same tired server daily
#: for an answer that changes on its own.
RECHECKABLE: frozenset[VerificationStatus] = frozenset(
    {
        VerificationStatus.UNVERIFIED,
        VerificationStatus.UNKNOWN,
        VerificationStatus.PUBLISHED_FIRST_PARTY,
    }
)

#: How many to take in one pass. Bounded because every one of these is a
#: connection to somebody else's mail server.
DEFAULT_BATCH = 200

#: How long an answer stands before the address is worth asking about again.
#:
#: Long, because the thing being measured moves slowly: a mailbox that exists
#: today usually exists next month, and each re-ask costs a stranger's server a
#: connection. Short enough that a mailbox retired after a staff change is
#: found before a whole sequence has been written to it.
RECHECK_AFTER = dt.timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One stored address, with what is needed to re-resolve its status."""

    channel_id: uuid.UUID
    email: str
    domain: str
    source: object
    status_before: VerificationStatus


@dataclass
class ReverifyReport:
    """What was found, what changed, and in which direction."""

    examined: int = 0
    checked: int = 0
    changed: int = 0
    #: How many of this batch a *dry run* would see again.
    #:
    #: A pager advances by this rather than by ``examined``. On a dry run
    #: nothing is recorded, so every row it looked at is still in the result
    #: set and the window has not moved. With ``apply`` every checked row is
    #: recorded and excluded, so the window moves on its own and the pager
    #: advances by nothing -- which is why it counts only the rows nobody could
    #: get an answer for.
    remained: int = 0
    #: New status to count, for the summary line.
    outcomes: dict[str, int] = field(default_factory=dict)
    #: Addresses that stopped being sendable, named. The number that matters:
    #: each one is a hard bounce that will not now happen.
    downgraded: list[str] = field(default_factory=list)

    def record(self, status: VerificationStatus) -> None:
        self.outcomes[status.value] = self.outcomes.get(status.value, 0) + 1


async def find_recheckable(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    limit: int = DEFAULT_BATCH,
    offset: int = 0,
) -> list[Candidate]:
    """Active email channels whose status was never established by a check.

    ``offset`` exists so a caller can page through the whole population in
    committed batches. Most re-checks leave the status where it was -- the
    address is behind an operator this module declines to ask -- so those rows
    stay in the result set, and a caller that only ever asked for the first N
    would check the same N forever.
    """
    rows = (
        await session.execute(
            select(
                ContactChannel.id,
                ContactChannel.normalized_value,
                ContactChannel.value_domain,
                ContactChannel.source,
                ContactChannel.verification_status,
            )
            .where(
                ContactChannel.workspace_id == workspace_id,
                ContactChannel.channel_type == "email",
                ContactChannel.is_active.is_(True),
                ContactChannel.verification_status.in_(tuple(RECHECKABLE)),
                # Recently asked about *by a mailbox verifier*: that answer
                # stands, and re-asking costs a stranger's mail server a
                # connection for information already in hand.
                #
                # The presence of the ``mailbox_verification`` key is what
                # separates the two kinds of row, and it matters. The discovery
                # path appends a row for every address it stores, provider
                # ``bounce_risk``, recording the local layers -- syntax, domain
                # lists, MX. Nothing in it asked a mail server anything. Keying
                # the exclusion on recency alone matched 489 of those and cut
                # the catch-up pass off at 95 addresses out of 551, which is
                # precisely the population it exists to check.
                ~select(ContactVerification.id)
                .where(
                    ContactVerification.channel_id == ContactChannel.id,
                    ContactVerification.workspace_id == workspace_id,
                    ContactVerification.verified_at
                    > dt.datetime.now(dt.UTC) - RECHECK_AFTER,
                    ContactVerification.detail.has_key("mailbox_verification"),
                )
                .exists(),
            )
            .order_by(ContactChannel.discovered_at.desc(), ContactChannel.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return [
        Candidate(
            channel_id=row[0],
            email=row[1],
            domain=(row[2] or row[1].partition("@")[2] or "").lower(),
            source=row[3],
            status_before=row[4],
        )
        for row in rows
    ]


async def reverify(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    verifier: MailboxVerifier,
    limit: int = DEFAULT_BATCH,
    offset: int = 0,
    apply: bool = False,
    resolver: MxResolver = system_mx_resolver,
) -> ReverifyReport:
    """Re-check a batch, and write the answers when ``apply`` is given.

    ``resolver`` is injectable for the same reason it is in
    :mod:`titan.intelligence.mx`: the MX layer is conclusive on its own, so a
    test that cannot control it is testing DNS rather than this function.
    """
    candidates = await find_recheckable(
        session, workspace_id=workspace_id, limit=limit, offset=offset
    )
    report = ReverifyReport(examined=len(candidates))
    if not candidates:
        return report

    # One resolve per distinct domain, not per address: a list of 200 addresses
    # is routinely 40 domains.
    bulk = check_many([candidate.domain for candidate in candidates], resolver=resolver)

    for candidate in candidates:
        mx: MxCheck | None = bulk.checks.get(candidate.domain)
        try:
            verification = await verifier.verify(candidate.email)
        except Exception as exc:
            # An outage must not rewrite a stored status. The address keeps the
            # answer it had, which is the same thing that happens on the
            # discovery path.
            logger.warning(
                "re-verification failed; leaving the stored status alone",
                extra={
                    "channel_id": str(candidate.channel_id),
                    "error_code": type(exc).__name__,
                    "verifier": verifier.name,
                },
            )
            # Nothing was recorded for it, so it will come back. That is the
            # right outcome: an outage is not an answer.
            report.remained += 1
            continue

        report.checked += 1
        risk = assess(
            email=candidate.email,
            source=candidate.source,  # type: ignore[arg-type]
            mx=mx,
            verification=verification if verification.is_conclusive else None,
        )
        report.record(risk.status)

        # Written whether or not anything moved. The row is the record of
        # having asked, and an unrecorded question gets asked again.
        if apply:
            detail = risk.as_verification_detail()
            detail["reverification"] = True
            if mx is not None:
                detail["mx"] = mx.as_verification_detail()
            detail["mailbox_verification"] = verification.as_verification_detail()
            session.add(
                ContactVerification(
                    workspace_id=workspace_id,
                    channel_id=candidate.channel_id,
                    provider=verification.provider,
                    result=risk.status,
                    mx_present=mx.can_receive_mail if mx is not None else None,
                    detail=detail,
                    verified_at=dt.datetime.now(dt.UTC),
                )
            )

        if risk.status is candidate.status_before:
            continue

        # Still in RECHECKABLE, but recorded now, so the exclusion above keeps
        # it out of the next run rather than the status doing it.
        report.changed += 1
        # Sendability is provenance plus status, never status alone. A
        # first-party address that turns out to be catch-all is still sendable
        # -- catch-all is the default on most small-business hosting -- so
        # counting it as a loss here would put a wrong number in front of
        # whoever is deciding whether to send tonight.
        was_sendable = verification_permits_sending(
            candidate.status_before,
            candidate.source,  # type: ignore[arg-type]
        )
        still_sendable = verification_permits_sending(
            risk.status,
            candidate.source,  # type: ignore[arg-type]
        )
        if was_sendable and not still_sendable:
            report.downgraded.append(f"{candidate.email} -> {risk.status.value}")

        if not apply:
            continue

        await session.execute(
            update(ContactChannel)
            .where(
                ContactChannel.id == candidate.channel_id,
                ContactChannel.workspace_id == workspace_id,
            )
            .values(verification_status=risk.status)
        )

    return report


__all__ = [
    "DEFAULT_BATCH",
    "RECHECKABLE",
    "Candidate",
    "ReverifyReport",
    "find_recheckable",
    "reverify",
]
