"""May this mailbox send cold mail today, judged by where its own probes landed?

Every other gate in the estate asks about the *message* or the *mailbox's
history*: is the body well formed, is the bounce rate under two percent, is the
warm-up allowance spent. None of them asks the only question that decides
whether a send is worth making -- is mail from this address reaching an inbox?
On 27 September the honest answer was "never once measured": six probes read,
six in spam, and 1,039 messages had gone out regardless.

So the rule here is blunt. **A mailbox sends cold mail only while it holds a
recent inbox reading.** Three ways to fail it, in the order they are checked:

* **The domain is resting.** Any mailbox on the domain read under the floor on
  two consecutive days. Receivers judge the domain as well as the address, so
  one mailbox junked two days running is evidence about all of them, and the
  remedy is time without cold volume -- fourteen days of it.
* **Nothing was measured in the last 48 hours.** "We have not looked" is not
  "it is fine". A probe nobody has read counts for nothing either way.
* **The recent readings are under the floor.** 70% of measured probes must be
  in the inbox. Promotions does not count: delivered, unread.

The probes themselves never pass through this gate -- they go out from
``placement_probe`` directly -- so a mailbox that fails it keeps being measured
and earns its way back the moment its readings recover. Nothing has to be
re-enabled by hand.

Everything below :func:`assess` is pure, so the rule can be argued with in a
test rather than by reading SQL.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coldops.delivery.placement import REACHED

#: Share of measured probes that must reach the inbox.
FLOOR = 0.70

#: How recent a reading must be to count. Probes go out once a day, so this
#: tolerates one missed round and not two.
FRESH_WITHIN = dt.timedelta(hours=48)

#: How long a domain rests after two consecutive days under the floor.
DOMAIN_REST = dt.timedelta(days=14)

#: Folders that count as a measurement. ``unknown`` and NULL mean nobody looked.
MEASURED = frozenset({"inbox", "promotions", "spam", "missing"})

#: Signal codes, shared with ``deliverability.evaluate`` and the outbox worker,
#: which treats all three as temporary -- a fact about the mailbox today, not
#: about the message.
UNMEASURED = "placement_unmeasured"
BELOW_FLOOR = "placement_below_floor"
DOMAIN_RESTING = "placement_domain_resting"
CODES = frozenset({UNMEASURED, BELOW_FLOOR, DOMAIN_RESTING})


@dataclass(frozen=True, slots=True)
class Reading:
    """One probe somebody looked at."""

    from_email: str
    sent_at: dt.datetime
    folder: str

    @property
    def reached(self) -> bool:
        return self.folder in REACHED


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether one mailbox may send, and the sentence that says why not."""

    from_email: str
    code: str | None
    detail: str
    #: Inbox share across the fresh window. None when nothing was measured.
    reach: float | None = None
    measured: int = 0
    rest_until: dt.datetime | None = None

    @property
    def may_send(self) -> bool:
        return self.code is None


def domain_of(address: str) -> str:
    return address.rpartition("@")[2].strip().lower()


def _daily_reach(readings: Iterable[Reading]) -> dict[str, dict[dt.date, float]]:
    """Inbox share per mailbox per UTC day, from measured readings only."""
    counts: dict[tuple[str, dt.date], list[int]] = defaultdict(lambda: [0, 0])
    for r in readings:
        if r.folder not in MEASURED:
            continue
        day = r.sent_at.astimezone(dt.UTC).date()
        cell = counts[(r.from_email.lower(), day)]
        cell[0] += int(r.reached)
        cell[1] += 1
    out: dict[str, dict[dt.date, float]] = defaultdict(dict)
    for (mailbox, day), (reached, total) in counts.items():
        out[mailbox][day] = reached / total
    return out


def rest_until(readings: Iterable[Reading], *, domain: str) -> dt.datetime | None:
    """When the domain's rest ends, or None if it is not resting.

    Two *calendar-consecutive* days, both measured, both under the floor, on
    any one mailbox of the domain. A day with no reading breaks the run rather
    than bridging it: an unmeasured day is not evidence of a bad one. The rest
    runs from the end of the second day, and the latest such pair wins.
    """
    daily = _daily_reach(r for r in readings if domain_of(r.from_email) == domain)
    latest: dt.datetime | None = None
    for by_day in daily.values():
        for day, share in by_day.items():
            nxt = day + dt.timedelta(days=1)
            if share < FLOOR and nxt in by_day and by_day[nxt] < FLOOR:
                end_of_second = dt.datetime.combine(
                    nxt + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC
                )
                candidate = end_of_second + DOMAIN_REST
                if latest is None or candidate > latest:
                    latest = candidate
    return latest


def assess(from_email: str, readings: Iterable[Reading], *, now: dt.datetime) -> Verdict:
    """The verdict for one mailbox, given every reading on its domain.

    ``readings`` may hold other mailboxes on the same domain -- they are what
    the rest rule reads -- and need span at least ``DOMAIN_REST`` plus two days
    for that rule to see a pair that is still in force.
    """
    mailbox = from_email.strip().lower()
    domain = domain_of(mailbox)
    pool = [r for r in readings if domain_of(r.from_email) == domain]

    resting = rest_until(pool, domain=domain)
    if resting is not None and now < resting:
        return Verdict(
            from_email=mailbox,
            code=DOMAIN_RESTING,
            detail=(
                f"{domain} read under {FLOOR:.0%} inbox on two consecutive days; "
                f"resting until {resting:%Y-%m-%d}"
            ),
            rest_until=resting,
        )

    fresh = [
        r
        for r in pool
        if r.from_email.lower() == mailbox
        and r.folder in MEASURED
        and now - r.sent_at <= FRESH_WITHIN
    ]
    if not fresh:
        return Verdict(
            from_email=mailbox,
            code=UNMEASURED,
            detail=(
                f"no placement reading for {mailbox} in the last "
                f"{int(FRESH_WITHIN.total_seconds() // 3600)} hours"
            ),
        )

    reached = sum(r.reached for r in fresh)
    reach = reached / len(fresh)
    if reach < FLOOR:
        return Verdict(
            from_email=mailbox,
            code=BELOW_FLOOR,
            detail=(
                f"{mailbox} reached the inbox in {reached} of {len(fresh)} probes "
                f"({reach:.0%}) in the last 48 hours, under the {FLOOR:.0%} floor"
            ),
            reach=reach,
            measured=len(fresh),
        )

    return Verdict(
        from_email=mailbox,
        code=None,
        detail=f"{mailbox} reached the inbox in {reached} of {len(fresh)} probes",
        reach=reach,
        measured=len(fresh),
    )


async def load_readings(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    domains: Iterable[str],
    now: dt.datetime,
) -> list[Reading]:
    """Every measured probe on these domains, far enough back for the rest rule.

    ``workspace_id`` is written into the SQL: raw text bypasses the ORM's
    workspace scoping and RLS is not load-bearing for the application role.
    """
    wanted = sorted({d.strip().lower() for d in domains if d})
    if not wanted:
        return []
    since = now - DOMAIN_REST - dt.timedelta(days=2)
    rows = (
        await session.execute(
            text(
                """
                SELECT from_email, sent_at, folder
                  FROM placement_checks
                 WHERE workspace_id = :ws
                   AND sent_at >= :since
                   AND folder = ANY(CAST(:measured AS text[]))
                   AND lower(split_part(from_email, '@', 2)) = ANY(CAST(:domains AS text[]))
                """
            ),
            {
                "ws": workspace_id,
                "since": since,
                "measured": sorted(MEASURED),
                "domains": wanted,
            },
        )
    ).all()
    return [
        Reading(from_email=str(r.from_email), sent_at=r.sent_at, folder=str(r.folder))
        for r in rows
    ]


async def verdicts_for(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    mailboxes: Iterable[str],
    now: dt.datetime,
) -> dict[str, Verdict]:
    """One verdict per mailbox, keyed by lower-cased address. One query."""
    addresses = sorted({m.strip().lower() for m in mailboxes if m})
    readings = await load_readings(
        session,
        workspace_id=workspace_id,
        domains={domain_of(a) for a in addresses},
        now=now,
    )
    return {a: assess(a, readings, now=now) for a in addresses}


__all__ = [
    "BELOW_FLOOR",
    "CODES",
    "DOMAIN_REST",
    "DOMAIN_RESTING",
    "FLOOR",
    "FRESH_WITHIN",
    "MEASURED",
    "UNMEASURED",
    "Reading",
    "Verdict",
    "assess",
    "domain_of",
    "load_readings",
    "rest_until",
    "verdicts_for",
]
