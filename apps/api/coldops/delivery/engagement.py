"""Was it seen? Every signal graded by how much it actually proves.

Three things fetch the content of a message, and they mean different things:

* **a person** opening it or clicking through -- the only one that matters;
* **Apple's privacy proxy**, which loads every image of every message on
  arrival, whether anybody ever reads it -- proof of delivery, nothing more;
* **a security scanner** at the recipient's gateway, which fetches links and
  images seconds after delivery to check them -- proof of nothing about the
  reader at all.

A follow-up that branches on "seen" and cannot tell these apart branches on
noise. So every event is graded here, and the raw event is kept beside its
grade (``engagement_events``) so a rule that turns out wrong can be re-run.

Grades, strongest first:

``confirmed``  a person, beyond reasonable doubt: the evidence page's own
               script reported that someone stayed on it.
``likely``     probably a person: Gmail's image proxy (which fetches when the
               message is opened), or a real browser fetching after the
               scanner window has passed.
``delivered``  arrived, nothing more: Apple's proxy.
``machine``    a scanner, a script, or a fetch too fast to be a human.

Pure functions above :func:`record_event`; the rules are argued with in tests.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

CONFIRMED = "confirmed"
LIKELY = "likely"
DELIVERED = "delivered"
MACHINE = "machine"
GRADES = (CONFIRMED, LIKELY, DELIVERED, MACHINE)

#: Which grades count as a person having seen it.
SEEN = frozenset({CONFIRMED, LIKELY})

OPEN = "open"
VISIT = "visit"
VISIT_CONFIRMED = "visit_confirmed"
KINDS = (OPEN, VISIT, VISIT_CONFIRMED)

#: Apple Mail Privacy Protection fetches from Apple's own 17.0.0.0/8.
APPLE_NETWORKS = (ipaddress.ip_network("17.0.0.0/8"),)

#: Gmail fetches images through its own proxy, once, when the message is
#: opened -- and caches it afterwards, so only the first fetch means anything.
GOOGLE_PROXY = re.compile(r"GoogleImageProxy|ggpht\.com", re.I)

#: User agents that are never a person reading mail.
SCANNER = re.compile(
    r"python|curl|wget|go-http-client|java/|okhttp|libwww|httpclient|"
    r"headlesschrome|phantomjs|bot\b|crawler|spider|preview|"
    r"barracuda|mimecast|proofpoint|symantec|messagelabs|trend ?micro|"
    r"fireeye|zscaler|forcepoint|sophos|cisco|ironport|safelinks",
    re.I,
)

#: A fetch this soon after the send is a gateway scanning the message, not a
#: person: nobody reads mail within seconds of it arriving often enough to
#: trust the exception.
SCANNER_WINDOW = dt.timedelta(seconds=60)

#: How long somebody must have stayed on the evidence page for its beacon to
#: count. The page's script waits this long before it reports at all.
MIN_DWELL_SECONDS = 4


@dataclass(frozen=True, slots=True)
class Graded:
    grade: str
    reason: str

    @property
    def seen(self) -> bool:
        return self.grade in SEEN


def _is_apple(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        address = ipaddress.ip_address(ip.split(",")[0].strip())
    except ValueError:
        return False
    return any(address in network for network in APPLE_NETWORKS)


def grade(
    kind: str,
    *,
    user_agent: str | None,
    client_ip: str | None,
    since_send: dt.timedelta | None,
    dwell_seconds: float | None = None,
) -> Graded:
    """Grade one event. ``since_send`` is None when the send time is unknown."""
    ua = (user_agent or "").strip()

    if kind == VISIT_CONFIRMED:
        if dwell_seconds is not None and dwell_seconds >= MIN_DWELL_SECONDS:
            return Graded(CONFIRMED, f"stayed {dwell_seconds:.0f}s on the evidence page")
        return Graded(MACHINE, "beacon without the minimum stay")

    if not ua:
        return Graded(MACHINE, "no user agent")
    if kind == OPEN and _is_apple(client_ip):
        return Graded(
            DELIVERED, "Apple Mail Privacy Protection loads every image on arrival"
        )
    if kind == OPEN and GOOGLE_PROXY.search(ua):
        return Graded(LIKELY, "Gmail's image proxy fetches when the message is opened")
    if SCANNER.search(ua):
        return Graded(MACHINE, "user agent is a scanner or a script")
    if since_send is not None and since_send < SCANNER_WINDOW:
        return Graded(
            MACHINE,
            f"{since_send.total_seconds():.0f}s after sending, inside the scanner window",
        )
    if kind == VISIT:
        return Graded(LIKELY, "a browser opened the evidence page")
    return Graded(LIKELY, "a mail client loaded the image after the scanner window")


def client_ip_of(headers: dict[str, str], fallback: str | None) -> str | None:
    """The caller's address as nginx saw it. X-Real-IP is set by our own proxy."""
    for name in ("x-real-ip", "x-forwarded-for"):
        value = headers.get(name)
        if value:
            return value.split(",")[0].strip()[:64]
    return fallback


async def last_send_to_lead(
    session: AsyncSession, *, workspace_id: uuid.UUID, lead_id: uuid.UUID
) -> dt.datetime | None:
    return await session.scalar(
        text(
            "SELECT max(sent_at) FROM messages "
            "WHERE workspace_id = :ws AND lead_id = :lead AND sent_at IS NOT NULL"
        ),
        {"ws": workspace_id, "lead": lead_id},
    )


async def record_event(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    lead_id: uuid.UUID | None,
    message_id: uuid.UUID | None,
    kind: str,
    graded: Graded,
    client_ip: str | None,
    user_agent: str | None,
    occurred_at: dt.datetime,
    detail: dict[str, Any] | None = None,
) -> None:
    """Keep the raw event beside the grade it was given."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    await session.execute(
        text(
            """
            INSERT INTO engagement_events
                (workspace_id, lead_id, message_id, kind, occurred_at,
                 client_ip, user_agent, grade, reason, detail)
            VALUES (:ws, :lead, :message, :kind, :at, :ip, :ua, :grade, :reason,
                    CAST(:detail AS jsonb))
            """
        ),
        {
            "ws": workspace_id,
            "lead": lead_id,
            "message": message_id,
            "kind": kind,
            "at": occurred_at,
            "ip": client_ip,
            "ua": (user_agent or "")[:1000] or None,
            "grade": graded.grade,
            "reason": graded.reason,
            "detail": json.dumps(detail) if detail is not None else None,
        },
    )


async def best_grade(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    lead_id: uuid.UUID,
    since: dt.datetime,
) -> str | None:
    """The strongest grade recorded for a lead since a moment, or None.

    What a follow-up asks: since the last message went out, was it seen, and
    how sure are we?
    """
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT grade FROM engagement_events "
                "WHERE workspace_id = :ws AND lead_id = :lead AND occurred_at >= :since"
            ),
            {"ws": workspace_id, "lead": lead_id, "since": since},
        )
    ).scalars()
    found = set(rows)
    for candidate in GRADES:
        if candidate in found:
            return candidate
    return None


__all__ = [
    "CONFIRMED",
    "DELIVERED",
    "GRADES",
    "KINDS",
    "LIKELY",
    "MACHINE",
    "MIN_DWELL_SECONDS",
    "OPEN",
    "SCANNER_WINDOW",
    "SEEN",
    "VISIT",
    "VISIT_CONFIRMED",
    "Graded",
    "best_grade",
    "client_ip_of",
    "grade",
    "last_send_to_lead",
    "record_event",
]
