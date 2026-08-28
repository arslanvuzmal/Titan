"""Warming a mailbox by using it, rather than by waiting.

Two different things are called warm-up and only one of them was built.

:mod:`titan.delivery.mailbox_ramp` and ``deliverability.warmup_limit`` are the
*ramp*: how much a mailbox is allowed to send today, grown from a fifth of its
target to its target as evidence accumulates. That has been running and it is
the load-bearing half -- it is what stops a cold mailbox sending fifty cold
emails on its first morning.

This module is the other half: **traffic**. A mailbox with a good ramp and no
history is still a mailbox nobody has ever received mail from. Warm-up traffic
gives it one -- messages that are delivered, opened, replied to, and rescued
from the spam folder when they land there -- so that by the time it writes to a
stranger, the receiving networks have seen it behave like a person.

**What this can and cannot do, said plainly.** Mail between two mailboxes on
the same domain usually never leaves the server, and generates no signal at
Gmail or Microsoft, which is where the reputation that matters is kept. Three
mailboxes on ``arslanvuzmallone.com`` warming each other is therefore worth
very little on its own. The value appears when the participant list spans
providers: a message from ``outreach@`` that a Gmail account receives, does not
mark as spam, and replies to is a real signal at Google. So participants are
whatever mailboxes the operator lists, on any provider, and the module is
honest about the pool it was given.

**It cannot reach a stranger.** Every recipient is a participant, read from the
same credential file the sender authenticates with -- an address Titan holds
the password for. There is no path here that takes a recipient from a lead, a
draft, or the outbox, and this module imports none of them. That is the
property the tests check first.

**It never touches the outbox.** Warm-up mail is not outreach: it is not
composed, not gated, not recorded as a message to a lead, and it does not spend
the mailbox's daily sending quota on anything a customer would receive. It goes
out on its own SMTP connection with its own headers, and the only trace it
leaves is the mail itself, sitting in the participants' mailboxes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import hashlib
import imaplib
import logging
import random
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage

from titan.delivery.mailboxes import Endpoint, MailboxRegistry

logger = logging.getLogger(__name__)

#: Marks a message as warm-up, in the mail itself. Present on every message
#: this module sends, so a human reading a mailbox -- or this module on a later
#: pass -- can tell warm-up traffic from anything a person wrote.
WARMUP_HEADER = "X-Titan-Warmup"

#: Folders a receiving server might file warm-up mail in. Finding a message in
#: one of these and moving it back to the inbox is the single most valuable
#: thing warm-up does: it is the signal that says "this sender is wanted".
JUNK_FOLDERS: tuple[str, ...] = (
    "Junk",
    "Junk E-mail",
    "Spam",
    "INBOX.Junk",
    "INBOX.Spam",
    "[Gmail]/Spam",
)

#: How many warm-up messages one mailbox sends on each day of its ramp.
#:
#: Slower than the sending ramp on purpose. Warm-up traffic is meant to look
#: like correspondence, and correspondence does not arrive in a burst that
#: doubles weekly. Past the end of the list a mailbox stays at the last value:
#: a warm mailbox still benefits from continuing to have conversations.
DAILY_VOLUME: tuple[int, ...] = (
    2,
    2,
    3,
    3,
    4,
    4,
    5,
    5,
    6,
    6,
    7,
    7,
    8,
    8,
    9,
    9,
    10,
    10,
    11,
    12,
)

#: Never more than this to the same partner in one day, whatever the ramp says.
#: Four messages a day to one colleague is correspondence; twelve is a machine.
MAX_PER_PARTNER = 3

#: Share of received warm-up messages that get a reply. A thread with a reply
#: is worth several without one, and a pool where everything is answered
#: immediately is its own pattern.
REPLY_SHARE = 0.6

#: Plain, internal-sounding notes. Nothing here is marketing, nothing asks for
#: anything, and nothing would embarrass anybody who opened the mailbox and
#: read it. They are short because real internal mail is short.
OPENERS: tuple[tuple[str, str], ...] = (
    (
        "Notes from this morning",
        "Went through the list from last week and tidied up the ones that were "
        "duplicated. Nothing surprising in there.\n\nWill pick up the rest "
        "tomorrow.",
    ),
    (
        "Re: scheduling",
        "Thursday works better than Wednesday for me if that is still open. "
        "Either way I can move things around.\n\nLet me know which suits.",
    ),
    (
        "Quick check",
        "Did the invoice for last month go out? I have it marked as sent but I "
        "cannot find the confirmation.\n\nNo rush.",
    ),
    (
        "Draft for review",
        "Put a first pass together this afternoon. It is rough in the middle "
        "section and I want to rewrite the ending, but the shape is there.\n\n"
        "Have a look when you get a minute.",
    ),
    (
        "Following up on the call",
        "Wrote up what we agreed so it is not sitting only in my head. Two "
        "things still open, both on my side.\n\nI will chase them this week.",
    ),
    (
        "Small thing",
        "The link in the footer goes to the old page. Not urgent, but worth "
        "fixing before anyone else notices it.",
    ),
    (
        "This week",
        "Fairly quiet on my end. Two things landing Thursday and then nothing "
        "until the following week.\n\nShout if you need anything moved.",
    ),
    (
        "Re: the numbers",
        "Checked them against last quarter and they line up. The dip in the "
        "middle month is the holiday, nothing else.",
    ),
)

#: Replies. Deliberately short -- a real reply usually is.
RESPONSES: tuple[str, ...] = (
    "Thanks, that all makes sense. Nothing from me.",
    "Got it. I will take a look this afternoon and come back to you.",
    "Perfect, that works. Let us go with that.",
    "Understood. I will pick it up from here.",
    "Yes, agreed. No changes from my side.",
    "Noted, thank you. I will sort the other half.",
)


@dataclass(frozen=True, slots=True)
class Participant:
    """A mailbox that both sends and receives warm-up mail.

    Both directions are required. A mailbox that can send but not be read
    cannot have its mail rescued from a spam folder, cannot be marked as read,
    and cannot reply -- and those three are the whole of what warm-up is for.
    A send-only mailbox in the pool would generate traffic and no signal.
    """

    address: str
    smtp: Endpoint
    imap: Endpoint
    #: Day of this mailbox's ramp, used to decide today's volume.
    day: int = 0

    @property
    def domain(self) -> str:
        return self.address.partition("@")[2]

    def volume_today(self) -> int:
        index = min(max(self.day, 0), len(DAILY_VOLUME) - 1)
        return DAILY_VOLUME[index]


@dataclass(frozen=True, slots=True)
class PlannedSend:
    """One warm-up message, decided before anything is opened."""

    sender: Participant
    recipient: Participant
    subject: str
    body: str
    message_id: str

    def describe(self) -> str:
        return f"{self.sender.address} -> {self.recipient.address}  {self.subject}"


@dataclass
class WarmupReport:
    planned: int = 0
    sent: int = 0
    skipped_already_sent: int = 0
    failed: int = 0
    rescued_from_spam: int = 0
    marked_read: int = 0
    replied: int = 0
    errors: list[str] = field(default_factory=list)


class NotAParticipant(ValueError):
    """Refused: warm-up may only ever write to a mailbox in the pool."""


def participants_from(
    registry: MailboxRegistry, *, days: dict[str, int] | None = None
) -> list[Participant]:
    """The mailboxes in the credential file that can take part.

    Silently dropping a send-only mailbox would shrink the pool invisibly, so
    the caller is told which were excluded through the log.
    """
    days = days or {}
    out: list[Participant] = []
    excluded: list[str] = []
    for account in registry.accounts():
        if account.imap is None:
            excluded.append(account.from_email)
            continue
        out.append(
            Participant(
                address=account.from_email,
                smtp=account.smtp,
                imap=account.imap,
                day=int(days.get(account.from_email.lower(), 0)),
            )
        )
    if excluded:
        logger.warning(
            "mailboxes excluded from warm-up: they can send but cannot be read, "
            "so their mail could not be rescued from spam or replied to",
            extra={"mailboxes": excluded},
        )
    return out


def _message_id(
    *, on_date: dt.date, sender: str, recipient: str, index: int, domain: str
) -> str:
    """Deterministic, so running the plan twice in a day is not two messages.

    The same id is what lets :func:`already_delivered` recognise a message that
    has already arrived, and what makes most receiving servers drop a duplicate
    if one is sent anyway.
    """
    digest = hashlib.sha256(
        f"titan-warmup:{on_date.isoformat()}:{sender}:{recipient}:{index}".encode()
    ).hexdigest()[:32]
    return f"<{digest}@{domain}>"


def plan(
    participants: list[Participant], *, on_date: dt.date | None = None
) -> list[PlannedSend]:
    """Who writes to whom today.

    Deterministic for a given day and pool, so the plan can be printed, read,
    and then carried out -- rather than a different plan being carried out from
    the one that was shown.
    """
    on_date = on_date or dt.datetime.now(dt.UTC).date()
    if len(participants) < 2:
        return []

    # Seeded on the date so the pairings vary day to day without varying
    # between the dry run and the run.
    rng = random.Random(f"titan-warmup:{on_date.isoformat()}")
    sends: list[PlannedSend] = []

    for sender in participants:
        partners = [p for p in participants if p.address != sender.address]
        wanted = min(sender.volume_today(), len(partners) * MAX_PER_PARTNER)
        per_partner: dict[str, int] = {p.address: 0 for p in partners}

        # No pair repeats a note within a day. Choosing with replacement put
        # "This week" in front of the same colleague three times in one
        # morning, which is a machine signature -- precisely what warm-up
        # exists to avoid teaching a receiving server.
        used: dict[str, set[int]] = {p.address: set() for p in partners}

        for index in range(wanted):
            available = [p for p in partners if per_partner[p.address] < MAX_PER_PARTNER]
            if not available:
                break
            recipient = rng.choice(available)
            per_partner[recipient.address] += 1
            unused = [i for i in range(len(OPENERS)) if i not in used[recipient.address]]
            # Running the corpus dry for one pair needs more than
            # MAX_PER_PARTNER messages, so this cannot happen today; falling
            # back beats raising if the cap ever moves.
            choice = rng.choice(unused) if unused else rng.randrange(len(OPENERS))
            used[recipient.address].add(choice)
            subject, body = OPENERS[choice]
            sends.append(
                PlannedSend(
                    sender=sender,
                    recipient=recipient,
                    subject=subject,
                    body=body,
                    message_id=_message_id(
                        on_date=on_date,
                        sender=sender.address,
                        recipient=recipient.address,
                        index=index,
                        domain=sender.domain,
                    ),
                )
            )
    return sends


def _build(send: PlannedSend) -> EmailMessage:
    message = EmailMessage()
    message["From"] = send.sender.address
    message["To"] = send.recipient.address
    message["Subject"] = send.subject
    message["Date"] = email.utils.formatdate(localtime=True)
    message["Message-ID"] = send.message_id
    message[WARMUP_HEADER] = "1"
    # Not a bulk message, and saying so is true: it is one message to one
    # person. Marking it bulk would teach the receiver the opposite of what
    # warm-up exists to teach it.
    message.set_content(send.body + "\n")
    return message


def _smtp_send(send: PlannedSend, *, timeout: float) -> str | None:
    """Deliver one warm-up message. Returns an error string, or None."""
    endpoint = send.sender.smtp
    try:
        if endpoint.security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                endpoint.host,
                endpoint.port,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(endpoint.host, endpoint.port, timeout=timeout)
            if endpoint.security == "starttls":
                client.starttls(context=ssl.create_default_context())
        with client:
            if endpoint.username and endpoint.password:
                client.login(endpoint.username, endpoint.password)
            client.send_message(_build(send))
        return None
    except (smtplib.SMTPException, OSError) as exc:
        return f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------ receiving
def _connect(endpoint: Endpoint, *, timeout: float) -> imaplib.IMAP4:
    if endpoint.security == "ssl":
        client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
            endpoint.host,
            endpoint.port,
            timeout=int(timeout),
            ssl_context=ssl.create_default_context(),
        )
    else:
        client = imaplib.IMAP4(endpoint.host, endpoint.port, timeout=int(timeout))
        client.starttls(ssl.create_default_context())
    client.login(endpoint.username, endpoint.password)
    return client


def _search_warmup(client: imaplib.IMAP4, folder: str) -> list[str]:
    """UIDs of warm-up messages in one folder, or [] if it does not exist.

    Returned as text. ``imaplib`` hands back bytes from SEARCH and expects text
    on the way back into UID commands, and mixing the two is how a UID reaches
    a server as ``b'123'``.
    """
    status, _ = client.select(f'"{folder}"')
    if status != "OK":
        return []
    # The charset argument is positional and untyped in imaplib; None means
    # "no charset", which is what every server here wants.
    status, data = client.uid("SEARCH", None, "HEADER", WARMUP_HEADER, "1")  # type: ignore[arg-type]
    if status != "OK" or not data or not data[0]:
        return []
    return [uid.decode() for uid in data[0].split()]


def _delivered_ids_blocking(endpoint: Endpoint, *, timeout: float) -> set[str]:
    """Message-IDs of warm-up mail already in this mailbox, anywhere."""
    found: set[str] = set()
    client: imaplib.IMAP4 | None = None
    try:
        client = _connect(endpoint, timeout=timeout)
        for folder in ("INBOX", *JUNK_FOLDERS):
            for uid in _search_warmup(client, folder):
                status, data = client.uid(
                    "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
                )
                if status != "OK" or not data or not isinstance(data[0], tuple):
                    continue
                header = data[0][1].decode(errors="replace")
                _, _, value = header.partition(":")
                cleaned = value.strip()
                if cleaned:
                    found.add(cleaned)
    except (imaplib.IMAP4.error, OSError) as exc:
        logger.warning(
            "could not read a warm-up mailbox; today's plan may re-send",
            extra={"mailbox": endpoint.username, "error": f"{type(exc).__name__}: {exc}"},
        )
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception as exc:  # pragma: no cover - best effort close
                logger.debug("imap logout failed: %s", exc)
    return found


async def already_delivered(
    participant: Participant, *, timeout_seconds: float = 30.0
) -> set[str]:
    """Which of today's messages this mailbox already holds."""
    return await asyncio.to_thread(
        _delivered_ids_blocking, participant.imap, timeout=timeout_seconds
    )


async def send_round(
    sends: list[PlannedSend],
    *,
    timeout_seconds: float = 30.0,
    skip_delivered: bool = True,
) -> WarmupReport:
    """Carry out a plan.

    ``skip_delivered`` reads each recipient's mailbox first and drops anything
    already there, so running the command twice in a day is not two messages.
    """
    report = WarmupReport(planned=len(sends))
    if not sends:
        return report

    delivered: dict[str, set[str]] = {}
    if skip_delivered:
        recipients = {s.recipient.address: s.recipient for s in sends}
        for address, participant in recipients.items():
            delivered[address] = await already_delivered(
                participant, timeout_seconds=timeout_seconds
            )

    for send in sends:
        if send.message_id in delivered.get(send.recipient.address, set()):
            report.skipped_already_sent += 1
            continue
        error = await asyncio.to_thread(_smtp_send, send, timeout=timeout_seconds)
        if error is None:
            report.sent += 1
        else:
            report.failed += 1
            report.errors.append(f"{send.describe()}: {error}")
    return report


def _tend_blocking(
    participant: Participant, *, timeout: float, reply_share: float, seed: str
) -> tuple[int, int, int, list[str]]:
    """Rescue from spam, mark read, and answer a share. One connection."""
    rescued = marked = replied = 0
    errors: list[str] = []
    rng = random.Random(f"{seed}:{participant.address}")
    client: imaplib.IMAP4 | None = None
    try:
        client = _connect(participant.imap, timeout=timeout)

        # 1. Anything filed as junk is moved back. This is the signal that
        #    matters most: a receiver learns from what its user rescues.
        for folder in JUNK_FOLDERS:
            uids = _search_warmup(client, folder)
            for uid in uids:
                status, _ = client.uid("COPY", uid, "INBOX")
                if status != "OK":
                    continue
                client.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                rescued += 1
            if uids:
                client.expunge()

        # 2. Read them, and answer some.
        client.select("INBOX")
        for uid in _search_warmup(client, "INBOX"):
            status, data = client.uid(
                "FETCH",
                uid,
                "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT X-TITAN-WARMUP-REPLY)])",
            )
            if status != "OK" or not data or not isinstance(data[0], tuple):
                continue
            headers = data[0][1].decode(errors="replace")
            fields = {}
            for line in headers.splitlines():
                key, _, value = line.partition(":")
                if value:
                    fields[key.strip().lower()] = value.strip()

            client.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            marked += 1

            # Already answered, or is itself a reply: leave it.
            if fields.get("x-titan-warmup-reply") or fields.get(
                "subject", ""
            ).lower().startswith("re: re:"):
                continue
            if rng.random() > reply_share:
                continue

            sender = email.utils.parseaddr(fields.get("from", ""))[1]
            original_id = fields.get("message-id", "")
            if not sender or not original_id:
                continue

            reply = EmailMessage()
            reply["From"] = participant.address
            reply["To"] = sender
            subject = fields.get("subject", "")
            reply["Subject"] = (
                subject if subject.lower().startswith("re:") else f"Re: {subject}"
            )
            reply["Date"] = email.utils.formatdate(localtime=True)
            reply["In-Reply-To"] = original_id
            reply["References"] = original_id
            reply["Message-ID"] = email.utils.make_msgid(domain=participant.domain)
            reply[WARMUP_HEADER] = "1"
            # So a later pass does not answer the answer.
            reply["X-Titan-Warmup-Reply"] = "1"
            reply.set_content(RESPONSES[rng.randrange(len(RESPONSES))] + "\n")

            endpoint = participant.smtp
            try:
                if endpoint.security == "ssl":
                    smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                        endpoint.host,
                        endpoint.port,
                        timeout=timeout,
                        context=ssl.create_default_context(),
                    )
                else:
                    smtp = smtplib.SMTP(endpoint.host, endpoint.port, timeout=timeout)
                    if endpoint.security == "starttls":
                        smtp.starttls(context=ssl.create_default_context())
                with smtp:
                    if endpoint.username and endpoint.password:
                        smtp.login(endpoint.username, endpoint.password)
                    smtp.send_message(reply)
                replied += 1
            except (smtplib.SMTPException, OSError) as exc:
                errors.append(f"{participant.address} reply: {type(exc).__name__}: {exc}")
    except (imaplib.IMAP4.error, OSError) as exc:
        errors.append(f"{participant.address}: {type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception as exc:  # pragma: no cover - best effort close
                logger.debug("imap logout failed: %s", exc)
    return rescued, marked, replied, errors


async def tend(
    participants: list[Participant],
    *,
    timeout_seconds: float = 30.0,
    reply_share: float = REPLY_SHARE,
    on_date: dt.date | None = None,
) -> WarmupReport:
    """Do the receiving half: rescue from spam, read, and answer some."""
    report = WarmupReport()
    seed = (on_date or dt.datetime.now(dt.UTC).date()).isoformat()
    for participant in participants:
        rescued, marked, replied, errors = await asyncio.to_thread(
            _tend_blocking,
            participant,
            timeout=timeout_seconds,
            reply_share=reply_share,
            seed=seed,
        )
        report.rescued_from_spam += rescued
        report.marked_read += marked
        report.replied += replied
        report.errors.extend(errors)
    return report


def check_recipients_are_participants(
    sends: list[PlannedSend], participants: list[Participant]
) -> None:
    """Refuse a plan that would write to anybody outside the pool.

    Belt and braces over :func:`plan`, which builds sends only from the pool.
    It is here because the cost of being wrong is a stranger receiving a
    fabricated internal note from a business they have never heard of, and
    because a future caller may build a plan some other way.
    """
    allowed = {p.address.lower() for p in participants}
    for send in sends:
        if send.recipient.address.lower() not in allowed:
            raise NotAParticipant(
                f"warm-up refused: {send.recipient.address} is not one of the "
                f"mailboxes Titan holds credentials for"
            )


def describe_pool(participants: list[Participant]) -> str:
    """An honest sentence about what this pool can and cannot achieve."""
    if len(participants) < 2:
        return (
            "fewer than two mailboxes can take part; warm-up needs at least a "
            "sender and a receiver"
        )
    domains = {p.domain for p in participants}
    if len(domains) == 1:
        return (
            f"{len(participants)} mailboxes, all on {next(iter(domains))}. Mail "
            f"between them usually never leaves the server, so this builds very "
            f"little reputation at Gmail or Microsoft. Adding a mailbox on "
            f"another provider is what makes warm-up worth running."
        )
    return (
        f"{len(participants)} mailboxes across {len(domains)} domains "
        f"({', '.join(sorted(domains))}); mail between them crosses provider "
        f"boundaries, which is where the signal comes from"
    )


__all__ = [
    "DAILY_VOLUME",
    "JUNK_FOLDERS",
    "MAX_PER_PARTNER",
    "OPENERS",
    "REPLY_SHARE",
    "RESPONSES",
    "WARMUP_HEADER",
    "NotAParticipant",
    "Participant",
    "PlannedSend",
    "WarmupReport",
    "already_delivered",
    "check_recipients_are_participants",
    "describe_pool",
    "participants_from",
    "plan",
    "send_round",
    "tend",
]
