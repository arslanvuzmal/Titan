"""Reading the outreach mailbox: IMAP transport and RFC 5322 parsing.

Split from :mod:`titan.delivery.reply_collector` on purpose. Everything here is
either pure (parsing) or pure I/O (IMAP), and neither half touches the database,
so the parser -- which is where the subtle mistakes live -- is testable against
a corpus of real messages with no network and no Postgres.

**Why imaplib and a thread.** There is no async IMAP in the standard library.
The alternatives are adding a third-party async client or running the blocking
stdlib one in a worker thread. A mailbox is polled once a minute and holds at
most a few dozen messages, so the thread costs nothing measurable, and it keeps
the dependency surface of a process that handles untrusted input as small as it
can be. If poll volume ever makes this the bottleneck, that is the moment to
revisit it -- not before.

**Why a connection per poll.** Reconnecting costs one TLS handshake a minute.
Holding a connection open across idle periods means discovering it went away
only when a fetch fails halfway through a batch, which is a far more annoying
failure to reason about than a handshake.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email
import email.policy
import email.utils
import html
import imaplib
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)

#: Messages larger than this are parsed for headers and truncated body. A mail
#: loop or a 40MB attachment must not be able to exhaust the poller's memory.
MAX_MESSAGE_BYTES = 2_000_000

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]*\n[ \t]*")


@dataclass(frozen=True, slots=True)
class RawMessage:
    """One message as the server handed it over."""

    uid: str
    raw: bytes


@dataclass(frozen=True, slots=True)
class ParsedEmail:
    """The fields Titan acts on, pulled out of an RFC 5322 message."""

    message_id: str | None
    from_email: str
    from_display: str
    subject: str
    body_text: str
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "text/plain"
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    date: dt.datetime | None = None
    #: The address that actually failed, read out of a delivery status report.
    #: Only ever set on a DSN, and the reason bounces can suppress the right
    #: mailbox instead of the reporting daemon.
    failed_recipient: str | None = None

    def thread_ids(self) -> tuple[str, ...]:
        """Every upstream Message-ID this message claims to answer.

        ``In-Reply-To`` first because it names the direct parent; ``References``
        after it, reversed, because the list runs oldest-first and the nearest
        ancestor is the most likely match. Order decides which of several
        messages in one thread a reply is attributed to.
        """
        ids: list[str] = []
        if self.in_reply_to:
            ids.append(self.in_reply_to)
        for ref in reversed(self.references):
            if ref not in ids:
                ids.append(ref)
        return tuple(ids)


class Mailbox(Protocol):
    """Injectable mail source, so tests need no IMAP server."""

    async def fetch_unread(self, limit: int) -> list[RawMessage]: ...

    async def mark_read(self, uids: list[str]) -> None: ...


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_email(raw: bytes) -> ParsedEmail:
    """Turn a raw RFC 5322 message into the fields Titan acts on.

    Never raises on malformed input. Mail arriving from strangers is the least
    trustworthy data in the system, and a poller that dies on one broken
    encoding stops processing every message behind it -- including the
    unsubscribe requests, which is the failure with legal consequences.
    """
    try:
        message = email.message_from_bytes(
            raw[:MAX_MESSAGE_BYTES], policy=email.policy.default
        )
    except Exception as exc:
        logger.warning("unparseable inbound message", extra={"error": str(exc)[:200]})
        return ParsedEmail(
            message_id=None,
            from_email="",
            from_display="",
            subject="",
            body_text="",
            headers={},
        )

    headers: dict[str, str] = {}
    for key, value in message.items():
        # First occurrence wins. Trace headers repeat, and the topmost is the
        # most recent hop; for the headers classification cares about
        # (Auto-Submitted, Precedence) a repeat would be the sender's own.
        if key not in headers:
            headers[key] = _header_str(value)

    display, address = email.utils.parseaddr(headers.get("From", ""))

    return ParsedEmail(
        message_id=_clean_message_id(headers.get("Message-ID")),
        from_email=address.strip().lower(),
        from_display=display.strip(),
        subject=_decode(message.get("Subject")),
        body_text=_extract_body(message),
        headers=headers,
        # The full header value, not get_content_type(): the DSN marker lives in
        # the report-type *parameter*, which get_content_type() discards.
        content_type=headers.get("Content-Type", "text/plain"),
        in_reply_to=_clean_message_id(headers.get("In-Reply-To")),
        references=_parse_references(headers.get("References")),
        date=_parse_date(headers.get("Date")),
        failed_recipient=_failed_recipient(message, headers),
    )


def _header_str(value: object) -> str:
    try:
        return str(value)
    except Exception:
        return ""


def _decode(value: object) -> str:
    """RFC 2047 decoding, tolerant of the encodings that do not parse."""
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return repr(value)


def _clean_message_id(value: str | None) -> str | None:
    """Normalise a Message-ID to its bare addr-spec.

    Servers vary on whether they include the angle brackets and on surrounding
    whitespace, and this value is a *join key* -- against the Message-ID Titan
    recorded when it sent. An unnormalised comparison threads nothing and every
    reply silently loses its lead.
    """
    if not value:
        return None
    candidate = value.strip().split()[0] if value.strip() else ""
    candidate = candidate.strip().lstrip("<").rstrip(">").strip()
    return candidate or None


def _parse_references(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(
        cleaned
        for token in value.replace(",", " ").split()
        if (cleaned := token.strip().lstrip("<").rstrip(">").strip())
    )


def _parse_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    # A Date header with no zone is legal and common. Assume UTC rather than
    # local: the poller's own timezone is an accident of where it is deployed
    # and must not leak into a stored timestamp.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _extract_body(message: email.message.Message) -> str:
    """Best available plain text.

    Prefers a text/plain part; falls back to stripping a text/html one. An
    HTML-only auto-responder is common enough that ignoring it would let
    out-of-office replies through as human replies and stop live sequences.
    """
    try:
        part = message.get_body(preferencelist=("plain",))  # type: ignore[attr-defined]
        if part is not None:
            return _content(part)
        part = message.get_body(preferencelist=("html",))  # type: ignore[attr-defined]
        if part is not None:
            return strip_html(_content(part))
    except Exception as exc:
        logger.debug("get_body failed, walking parts (%s)", type(exc).__name__)

    collected: list[str] = []
    for sub in message.walk():
        if sub.get_content_maintype() != "text":
            continue
        text = _content(sub)
        collected.append(
            strip_html(text) if sub.get_content_subtype() == "html" else text
        )
    return "\n".join(t for t in collected if t).strip()


def _content(part: email.message.Message) -> str:
    try:
        content = part.get_content()  # type: ignore[attr-defined]
    except Exception:
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, "replace")
        return str(part.get_payload())
    return content if isinstance(content, str) else str(content)


def strip_html(value: str) -> str:
    """Crude tag removal, adequate for phrase matching.

    Not sanitisation and not rendering: the output is only ever read by the
    regex classifiers and shown to an operator, never re-emitted as markup.
    """
    without_blocks = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", value)
    with_breaks = re.sub(r"(?i)<(br|/p|/div|/tr|/li)\s*/?>", "\n", without_blocks)
    text = html.unescape(_TAG_RE.sub(" ", with_breaks))
    return _WHITESPACE_RE.sub("\n", text).strip()


def _failed_recipient(
    message: email.message.Message, headers: dict[str, str]
) -> str | None:
    """The mailbox a delivery status report is complaining about.

    RFC 3464 puts it in a ``message/delivery-status`` part as ``Final-Recipient:
    rfc822; someone@example.com``. Exim and others also set
    ``X-Failed-Recipients`` on the outer message, which is checked as a fallback.

    Without this a bounce suppresses ``MAILER-DAEMON@`` at the *receiving* host:
    the address that actually rejected the mail stays in rotation and keeps
    bouncing, while a postmaster mailbox nobody writes to is blocked forever.
    """
    for part in _walk(message):
        if part.get_content_type() != "message/delivery-status":
            continue
        for field_name in ("Final-Recipient", "Original-Recipient"):
            for sub in _walk(part):
                value = sub.get(field_name)
                if value:
                    address = str(value).partition(";")[2].strip() or str(value).strip()
                    _, parsed = email.utils.parseaddr(address)
                    if parsed:
                        return parsed.strip().lower()

    fallback = headers.get("X-Failed-Recipients")
    if fallback:
        _, parsed = email.utils.parseaddr(fallback.split(",")[0])
        if parsed:
            return parsed.strip().lower()
    return None


def _walk(message: email.message.Message) -> list[email.message.Message]:
    try:
        return list(message.walk())
    except Exception:
        return [message]


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImapConfig:
    host: str
    port: int = 993
    username: str = ""
    password: str = ""
    #: "ssl" (implicit TLS, 993) or "starttls" (143). Plaintext is not offered:
    #: this connection carries a mailbox password and the full text of every
    #: reply, and there is no local-capture case for reading mail the way there
    #: is for sending it.
    security: str = "ssl"
    folder: str = "INBOX"
    timeout_seconds: float = 30.0


class ImapMailbox:
    """A real IMAP mailbox, polled over a fresh connection each time."""

    def __init__(self, config: ImapConfig) -> None:
        self._config = config

    async def fetch_unread(self, limit: int) -> list[RawMessage]:
        return await asyncio.to_thread(self._fetch_unread_blocking, limit)

    async def mark_read(self, uids: list[str]) -> None:
        if uids:
            await asyncio.to_thread(self._mark_read_blocking, uids)

    async def health_check(self) -> tuple[bool, str]:
        """Authenticate and select the folder, touching nothing.

        Run at worker startup so a wrong password is a loud failure at boot
        rather than a poller that appears healthy and silently reads nothing.
        """
        try:
            return await asyncio.to_thread(self._health_check_blocking)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    # -- blocking internals -------------------------------------------------

    def _connect(self) -> imaplib.IMAP4:
        config = self._config
        if config.security == "ssl":
            client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                config.host, config.port, timeout=config.timeout_seconds
            )
        else:
            client = imaplib.IMAP4(
                config.host, config.port, timeout=config.timeout_seconds
            )
            client.starttls()
        client.login(config.username, config.password)
        return client

    def _health_check_blocking(self) -> tuple[bool, str]:
        client = self._connect()
        try:
            status, detail = client.select(self._config.folder, readonly=True)
            if status != "OK":
                return False, f"cannot select {self._config.folder}: {detail!r}"
            return True, f"authenticated; {self._config.folder} selected"
        finally:
            _close_quietly(client)

    def _fetch_unread_blocking(self, limit: int) -> list[RawMessage]:
        client = self._connect()
        try:
            status, _ = client.select(self._config.folder)
            if status != "OK":
                raise RuntimeError(f"cannot select folder {self._config.folder!r}")

            status, data = client.uid("SEARCH", None, "UNSEEN")  # type: ignore[arg-type]
            if status != "OK":
                raise RuntimeError(f"IMAP SEARCH failed: {status}")

            uids = [token.decode("ascii", "ignore") for token in (data[0] or b"").split()]
            messages: list[RawMessage] = []
            for uid in uids[:limit]:
                # BODY.PEEK[], never BODY[]: a plain fetch sets \Seen as a side
                # effect, so a crash between fetch and ingest would leave the
                # message marked read and never processed. PEEK keeps the flag
                # ours to set, only once the reply is safely recorded.
                status, payload = client.uid("FETCH", uid, "(BODY.PEEK[])")
                if status != "OK" or not payload:
                    logger.warning("IMAP fetch failed", extra={"uid": uid})
                    continue
                raw = next((item[1] for item in payload if isinstance(item, tuple)), None)
                if isinstance(raw, bytes):
                    messages.append(RawMessage(uid=uid, raw=raw))
            return messages
        finally:
            _close_quietly(client)

    def _mark_read_blocking(self, uids: list[str]) -> None:
        client = self._connect()
        try:
            status, _ = client.select(self._config.folder)
            if status != "OK":
                raise RuntimeError(f"cannot select folder {self._config.folder!r}")
            client.uid("STORE", ",".join(uids), "+FLAGS", "(\\Seen)")
        finally:
            _close_quietly(client)


def _close_quietly(client: imaplib.IMAP4) -> None:
    for step in (client.close, client.logout):
        try:
            step()
        except Exception as exc:
            logger.debug("IMAP teardown: %s", type(exc).__name__, exc_info=exc)


__all__ = [
    "MAX_MESSAGE_BYTES",
    "ImapConfig",
    "ImapMailbox",
    "Mailbox",
    "ParsedEmail",
    "RawMessage",
    "parse_email",
    "strip_html",
]
