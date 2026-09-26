"""Is our mail reaching inboxes, or is nobody seeing it?

The question this answers was unanswerable for two months. 944 messages went
out and none produced a reply, and there are exactly two explanations -- the
offer is wrong, or the mail is not being read. They demand opposite responses,
and nothing in the estate could tell them apart. On 26 September a hand-run
seed test settled it in four minutes: spam at Gmail, junk at Outlook, three of
three.

**Why this cannot be done per message.** No provider reports the folder it
filed a stranger's mail into -- not Gmail, not Outlook, not any ESP. A
"delivered" webhook means accepted by the receiving server, which is equally
true of a message dropped into junk. The only way to observe placement is to
send to a mailbox you control and look inside it. That is what a probe is.

**Why one reading is nearly worthless.** Reputation moves over weeks, so the
value is the series. A warm-up is working when the same probe, from the same
address, stops being junked -- and that is only visible if the readings are
kept and comparable. Hence a table rather than a log line.

**What it costs.** Nothing that hurts: a probe is one message to an address we
own. It adds no pixel, rewrites no link and asks nothing of a recipient, which
matters because the alternatives for measuring engagement -- open pixels and
redirect links -- are themselves spam signals, and adding them to a domain
already in trouble would deepen exactly the hole being measured.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: Folders a probe can be found in. `missing` is a real answer and a bad one:
#: the message was accepted and then filed nowhere the recipient will look, or
#: silently dropped. `unknown` means nobody has looked yet.
FOLDERS = ("inbox", "promotions", "spam", "missing", "unknown")

#: Which of those count as the recipient plausibly seeing it.
REACHED = frozenset({"inbox"})


def provider_of(address: str) -> str:
    """Which filter is being measured.

    Grouped by the filter rather than the brand: anything on Microsoft's
    consumer or business stack is judged by the same engine, and the useful
    comparison is Google versus Microsoft versus everyone else.
    """
    domain = address.rpartition("@")[2].lower()
    if domain in {"gmail.com", "googlemail.com"}:
        return "gmail"
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        return "outlook"
    if domain in {"yahoo.com", "yahoo.co.uk", "ymail.com"}:
        return "yahoo"
    return "other"


def new_probe_token() -> str:
    """A token the probe carries and the checker searches for.

    Random rather than derived from the date: two rounds in one day must not
    collide, and a predictable token in a subject line is the kind of thing a
    filter learns to recognise.
    """
    return f"tp-{secrets.token_hex(6)}"


@dataclass(frozen=True, slots=True)
class ProbeRow:
    probe_token: str
    seed_address: str
    provider: str
    from_email: str
    subject: str
    had_attachment: bool
    sent_at: dt.datetime


async def record_sent(
    session: AsyncSession, *, workspace_id: uuid.UUID, probe: ProbeRow
) -> None:
    """Remember that a probe went out, before anyone knows where it landed.

    Written at send time rather than at check time so an unchecked probe is
    visible as unchecked. A table that only holds results cannot distinguish
    "we never looked" from "it arrived", and those are very different.
    """
    await session.execute(
        text(
            """
            INSERT INTO placement_checks
                (workspace_id, probe_token, seed_address, provider, from_email,
                 subject, had_attachment, sent_at, folder, checked_by)
            VALUES
                (:ws, :token, :seed, :provider, :from_email,
                 :subject, :attach, :sent_at, NULL, NULL)
            ON CONFLICT (probe_token, seed_address) DO NOTHING
            """
        ),
        {
            "ws": workspace_id,
            "token": probe.probe_token,
            "seed": probe.seed_address,
            "provider": probe.provider,
            "from_email": probe.from_email,
            "subject": probe.subject,
            "attach": probe.had_attachment,
            "sent_at": probe.sent_at,
        },
    )


async def record_result(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    probe_token: str,
    seed_address: str,
    folder: str,
    checked_by: str,
    note: str | None = None,
) -> bool:
    """Write where a probe was found. Returns False if that probe is unknown."""
    if folder not in FOLDERS:
        raise ValueError(f"folder must be one of {FOLDERS}, got {folder!r}")
    result = await session.execute(
        text(
            """
            UPDATE placement_checks
               SET folder = :folder,
                   checked_at = now(),
                   checked_by = :by,
                   note = COALESCE(:note, note)
             WHERE workspace_id = :ws
               AND probe_token = :token
               AND seed_address = :seed
            """
        ),
        {
            "ws": workspace_id,
            "token": probe_token,
            "seed": seed_address,
            "folder": folder,
            "by": checked_by,
            "note": note,
        },
    )
    return bool(result.rowcount)


async def latest_round(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> list[dict[str, object]]:
    """The most recent probe round, one row per seed."""
    rows = (
        await session.execute(
            text(
                """
                SELECT seed_address, provider, folder, checked_by,
                       sent_at, checked_at, from_email, had_attachment
                  FROM placement_checks
                 WHERE workspace_id = :ws
                   AND probe_token = (
                        SELECT probe_token FROM placement_checks
                         WHERE workspace_id = :ws
                         ORDER BY sent_at DESC LIMIT 1
                   )
                 ORDER BY provider, seed_address
                """
            ),
            {"ws": workspace_id},
        )
    ).mappings()
    return [dict(r) for r in rows]


async def history(
    session: AsyncSession, *, workspace_id: uuid.UUID, days: int = 30
) -> list[dict[str, object]]:
    """Placement by provider over time -- the series that actually matters.

    Reported as a share reaching the inbox rather than a count, because the
    number of seeds changes as addresses are added and a raw count would read
    as improvement when it is only more probes.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT sent_at::date            AS day,
                       provider,
                       count(*)                 AS probes,
                       count(*) FILTER (WHERE folder = 'inbox')  AS inbox,
                       count(*) FILTER (WHERE folder = 'spam')   AS spam,
                       count(*) FILTER (WHERE folder IS NULL)    AS unchecked
                  FROM placement_checks
                 WHERE workspace_id = :ws
                   AND sent_at > now() - make_interval(days => :days)
                 GROUP BY 1, 2
                 ORDER BY 1 DESC, 2
                """
            ),
            {"ws": workspace_id, "days": days},
        )
    ).mappings()
    return [dict(r) for r in rows]


__all__ = [
    "FOLDERS",
    "REACHED",
    "ProbeRow",
    "history",
    "latest_round",
    "new_probe_token",
    "provider_of",
    "record_result",
    "record_sent",
]
