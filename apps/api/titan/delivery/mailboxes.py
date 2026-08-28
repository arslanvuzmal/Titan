"""Every mailbox Titan can send from, and the credential that opens each one.

Titan already decides *which* mailbox a message leaves from.
:mod:`titan.delivery.sender_pool` weighs remaining headroom, warm-up position
and health, and hands the outbox a sender identity per message. The SMTP
adapter then ignored that entirely: it was built once, at worker start, from a
single ``TITAN_SMTP_USERNAME`` and ``TITAN_SMTP_PASSWORD``.

So a pool of three mailboxes authenticated as one of them and put the other
two in the ``From`` header. That is not a cosmetic mismatch. The envelope
sender is the authenticated account, the header sender is somebody else, and
SPF and DKIM both align against the envelope -- which is precisely the shape
DMARC exists to reject. Best case the provider refuses the message outright
with a 550; worst case it accepts it and the recipient's server files it as a
forgery, quietly, and the mailbox's reputation pays for it.

This module is the missing half: a credential per mailbox, looked up by the
address the pool actually chose.

**Credentials live in a file, not in the environment and not in this
repository.** Passwords in ``.env`` end up in ``docker compose config`` output,
in shell history, and in every screenshot of a terminal. The file is read once
at worker start, is never logged, and is never echoed back by any command here
-- :meth:`MailboxAccount.redacted` is the only rendering that exists.

**A mailbox with no credential does not send.** It is tempting to fall back to
the global ``TITAN_SMTP_*`` account so that "something goes out". Something
going out under the wrong authentication is the failure this module was written
to stop, so the send is refused instead, as a configuration error -- which
leaves the recipient un-suppressed and the message retryable once the operator
fixes the file.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Transport security a mailbox may ask for. ``none`` exists for Mailpit and
#: nothing else; :func:`_check_security` holds it to that.
SECURITIES = frozenset({"ssl", "starttls", "none"})

#: Hosts where cleartext SMTP is a local capture server rather than a mistake.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "mailpit"})

#: What the shipped template puts where a password goes. A file still carrying
#: one of these was written and never filled in, and saying so beats an
#: authentication failure against a live provider an hour later.
PLACEHOLDERS = frozenset(
    {
        "",
        "paste_app_password_here",
        "paste-app-password-here",
        "changeme",
        "change_me",
        "your_password",
        "your-password",
        "xxxxxxxx",
        "todo",
    }
)


class MailboxConfigError(ValueError):
    """The mailbox file cannot be trusted to send mail. Never partially applied.

    Loading is all-or-nothing on purpose. A file with one bad entry silently
    dropped is a pool that has quietly shrunk, which looks exactly like a pool
    that is working until the volume does not arrive.
    """


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One server this mailbox talks to, in one direction."""

    host: str
    port: int
    username: str
    password: str
    security: str = "ssl"

    def redacted(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "security": self.security,
            "password": "set" if self.password else "missing",
        }


@dataclass(frozen=True, slots=True)
class MailboxAccount:
    """A single sending address and the credentials behind it.

    ``imap`` is optional and separately meaningful: a mailbox that can send but
    cannot be read is a mailbox whose bounces and unsubscribe requests nobody
    sees. It is allowed, because a first mailbox often has SMTP working before
    IMAP is enabled, but :meth:`MailboxRegistry.readable` reports which is
    which so the gap is visible rather than assumed away.
    """

    from_email: str
    smtp: Endpoint
    imap: Endpoint | None = None
    #: Free-text note from the file, shown in the CLI. Never sent anywhere.
    label: str = ""

    def redacted(self) -> dict[str, Any]:
        return {
            "from_email": self.from_email,
            "label": self.label,
            "smtp": self.smtp.redacted(),
            "imap": self.imap.redacted() if self.imap else None,
        }


class MailboxRegistry:
    """Lookup from a sending address to its credentials.

    Addresses are matched case-insensitively on the whole address. No
    normalisation beyond that: ``sales@`` and ``sales+tag@`` are different
    mailboxes as far as an SMTP server is concerned, and folding them together
    here would authenticate one as the other -- the exact failure above.
    """

    def __init__(self, accounts: list[MailboxAccount]) -> None:
        self._by_address: dict[str, MailboxAccount] = {}
        for account in accounts:
            key = account.from_email.lower()
            if key in self._by_address:
                raise MailboxConfigError(
                    f"{account.from_email} appears twice; one address, one credential"
                )
            self._by_address[key] = account

    def __len__(self) -> int:
        return len(self._by_address)

    def __bool__(self) -> bool:
        return bool(self._by_address)

    def get(self, from_email: str) -> MailboxAccount | None:
        return self._by_address.get((from_email or "").strip().lower())

    def addresses(self) -> list[str]:
        return sorted(account.from_email for account in self._by_address.values())

    def accounts(self) -> list[MailboxAccount]:
        return sorted(self._by_address.values(), key=lambda account: account.from_email)

    def readable(self) -> list[MailboxAccount]:
        """The mailboxes whose replies and bounces can actually be collected."""
        return [account for account in self.accounts() if account.imap is not None]

    def redacted(self) -> list[dict[str, Any]]:
        return [account.redacted() for account in self.accounts()]


# --------------------------------------------------------------------- parse
def _require(source: dict[str, Any], key: str, where: str) -> Any:
    if key not in source or source[key] in (None, ""):
        raise MailboxConfigError(f"{where}: {key!r} is required")
    return source[key]


def _check_security(security: str, host: str, where: str) -> str:
    value = str(security).strip().lower()
    if value not in SECURITIES:
        raise MailboxConfigError(
            f"{where}: security {security!r} is not one of {sorted(SECURITIES)}"
        )
    if value == "none" and host.strip().lower() not in LOOPBACK_HOSTS:
        raise MailboxConfigError(
            f"{where}: security 'none' sends the password in clear text and is "
            f"only allowed to a local capture server; host is {host!r}"
        )
    return value


def _check_password(password: Any, where: str) -> str:
    if str(password).strip().lower() in PLACEHOLDERS:
        raise MailboxConfigError(
            f"{where}: the password is still the template placeholder. Put the "
            f"mailbox's real app password there, or remove the mailbox."
        )
    return str(password)


def _endpoint(
    raw: dict[str, Any], *, where: str, default_port: int, fallback_username: str
) -> Endpoint:
    host = str(_require(raw, "host", where)).strip()
    port = int(raw.get("port") or default_port)
    if not 1 <= port <= 65535:
        raise MailboxConfigError(f"{where}: port {port} is out of range")
    username = str(raw.get("username") or fallback_username).strip()
    if not username:
        raise MailboxConfigError(f"{where}: username is required")
    password = _check_password(_require(raw, "password", where), where)
    return Endpoint(
        host=host,
        port=port,
        username=username,
        password=password,
        security=_check_security(raw.get("security", "ssl"), host, where),
    )


def parse_mailboxes(document: dict[str, Any]) -> MailboxRegistry:
    """Turn the parsed file into a registry, or refuse the whole thing."""
    entries = document.get("mailboxes")
    if not isinstance(entries, list):
        found = type(entries).__name__ if entries is not None else "nothing"
        raise MailboxConfigError(f"the file must hold a 'mailboxes' list; got {found}")

    accounts: list[MailboxAccount] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise MailboxConfigError(f"mailbox {index}: expected an object")
        if entry.get("enabled") is False:
            # Kept in the file with its credential, deliberately out of the
            # pool. Better than deleting the block and retyping the password.
            continue

        from_email = str(_require(entry, "from_email", f"mailbox {index}")).strip()
        if "@" not in from_email:
            raise MailboxConfigError(
                f"mailbox {index}: {from_email!r} is not an email address"
            )
        where = from_email

        smtp_raw = _require(entry, "smtp", where)
        if not isinstance(smtp_raw, dict):
            raise MailboxConfigError(f"{where}: 'smtp' must be an object")
        smtp = _endpoint(
            smtp_raw,
            where=f"{where} smtp",
            default_port=465,
            fallback_username=from_email,
        )

        imap: Endpoint | None = None
        imap_raw = entry.get("imap")
        if isinstance(imap_raw, dict) and imap_raw:
            imap = _endpoint(
                imap_raw,
                where=f"{where} imap",
                default_port=993,
                fallback_username=from_email,
            )
            if imap.security == "none":
                raise MailboxConfigError(
                    f"{where} imap: cleartext IMAP is never accepted; it reads "
                    f"mail as well as authenticating"
                )

        accounts.append(
            MailboxAccount(
                from_email=from_email,
                smtp=smtp,
                imap=imap,
                label=str(entry.get("label") or ""),
            )
        )

    return MailboxRegistry(accounts)


def load_mailboxes(path: str | pathlib.Path | None) -> MailboxRegistry:
    """Read the mailbox file. An unset path yields an empty registry.

    Empty is not an error here -- the worker decides whether it can run without
    one, because that answer depends on which provider is configured.
    """
    if not path:
        return MailboxRegistry([])

    file = pathlib.Path(path)
    if not file.exists():
        raise MailboxConfigError(f"mailbox file {file} does not exist")
    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MailboxConfigError(f"mailbox file {file} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise MailboxConfigError(f"mailbox file {file} must hold a JSON object")

    registry = parse_mailboxes(document)
    logger.info(
        "mailbox credentials loaded",
        extra={
            "path": str(file),
            "mailboxes": len(registry),
            "addresses": registry.addresses(),
            "readable": len(registry.readable()),
        },
    )
    return registry


__all__ = [
    "LOOPBACK_HOSTS",
    "PLACEHOLDERS",
    "SECURITIES",
    "Endpoint",
    "MailboxAccount",
    "MailboxConfigError",
    "MailboxRegistry",
    "load_mailboxes",
    "parse_mailboxes",
]
