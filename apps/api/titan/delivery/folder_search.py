"""Where did this probe land? Asked of a mailbox we own, folder by folder.

``placement.py`` records that a probe was sent and leaves ``folder`` null until
somebody looks. Until now somebody meant a person, opening three webmail tabs
and typing the answer back. This is the looking, done by machine.

**Gmail's Promotions tab is not a folder.** It is a label on a message that is
still in INBOX, so anything that walks the folder list finds a promoted message
sitting in the inbox and reports ``inbox``. That is the single most misleading
answer this module could give: Promotions is where cold outreach goes to be
technically delivered and never read, and calling it inbox would turn the whole
measurement into reassurance. Gmail exposes the category through its own search
extension, ``X-GM-RAW "category:promotions"``, and this asks that question
first, before believing the folder.

**Everything is read-only.** ``select(readonly=True)`` throughout, and nothing
is marked seen, moved or deleted. A probe left unread in the spam folder is
evidence that survives to the next run; a probe this code tidied away is a
reading nobody can check.

**Not found is an answer.** ``missing`` means the server accepted the message
and then filed it somewhere the recipient will never look, or dropped it. It is
in ``FOLDERS`` for that reason, and until something actually searched the
mailbox it was a value nothing could ever produce.
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
from dataclasses import dataclass

from titan.delivery.mailbox import ImapConfig, _close_quietly
from titan.delivery.microsoft_oauth import imap_login

logger = logging.getLogger(__name__)

#: Where to look, per provider, in the order that decides ties.
#:
#: Order matters when a provider has the same message in two places -- Gmail
#: shows a spam message in "All Mail" as well as "[Gmail]/Spam" -- and the
#: first hit wins. Spam is checked before inbox everywhere: a false "inbox" is
#: the reading that stops you acting, and a false "spam" only makes you look
#: again.
FOLDER_ORDER: dict[str, tuple[tuple[str, str], ...]] = {
    # (folder to select, what a hit there means)
    "gmail": (
        ("[Gmail]/Spam", "spam"),
        ("INBOX", "inbox"),
    ),
    "outlook": (
        ("Junk Email", "spam"),
        ("Junk", "spam"),
        ("INBOX", "inbox"),
    ),
    "other": (
        ("Junk", "spam"),
        ("Spam", "spam"),
        ("INBOX", "inbox"),
    ),
}

#: Gmail's own search extension. The categories are labels rather than folders,
#: so this is the only way to tell the Promotions tab from the Primary one.
GMAIL_PROMOTIONS = '(X-GM-RAW "category:promotions")'


@dataclass(frozen=True, slots=True)
class FolderVerdict:
    """Where one probe was found, and what was actually searched to find out."""

    probe_token: str
    folder: str
    #: The IMAP mailbox the hit came from, for the operator reading a surprise.
    #: Empty when the verdict is ``missing``.
    found_in: str = ""
    #: Folders that were successfully searched. A verdict of ``missing`` means
    #: nothing here matched -- which is only trustworthy if the list is long,
    #: so it travels with the verdict rather than being assumed.
    searched: tuple[str, ...] = ()


def _search(client: imaplib.IMAP4, folder: str, criteria: str) -> list[bytes]:
    """Search one folder, returning message ids, or nothing if it is absent.

    An absent folder is normal rather than exceptional: "Junk Email" exists at
    Outlook and not at Gmail, and the per-provider lists above are a best guess
    at names that vary by locale. A missing folder must not abort the round --
    it would turn one renamed mailbox into a day with no measurement at all.
    """
    status, _ = client.select(folder, readonly=True)
    if status != "OK":
        return []
    status, data = client.search(None, criteria)  # type: ignore[arg-type]
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def find_probe_blocking(
    config: ImapConfig, provider: str, probe_token: str
) -> FolderVerdict:
    """Look for one probe in one seed mailbox. Blocking; call via the async wrapper.

    The token is matched in the subject, which is where ``placement_probe``
    puts it. Matching the subject rather than a header keeps the search inside
    ordinary IMAP: ``HEADER X-Titan-Probe`` is supported unevenly and silently
    returns nothing where it is not, which reads exactly like a message that
    never arrived.
    """
    criteria = f'(SUBJECT "{probe_token}")'
    searched: list[str] = []
    client = _ImapConnection(config).open()
    try:
        # Gmail first, and only Gmail: promotions is a label on an INBOX
        # message, so asking after checking INBOX would already have answered
        # "inbox" and never got here.
        if provider == "gmail":
            status, _ = client.select("INBOX", readonly=True)
            if status == "OK":
                searched.append("INBOX/promotions")
                promoted = _search(client, "INBOX", f"{criteria} {GMAIL_PROMOTIONS}")
                if promoted:
                    return FolderVerdict(
                        probe_token=probe_token,
                        folder="promotions",
                        found_in="INBOX (category:promotions)",
                        searched=tuple(searched),
                    )

        for folder, verdict in FOLDER_ORDER.get(provider, FOLDER_ORDER["other"]):
            hits = _search(client, folder, criteria)
            searched.append(folder)
            if hits:
                return FolderVerdict(
                    probe_token=probe_token,
                    folder=verdict,
                    found_in=folder,
                    searched=tuple(searched),
                )

        return FolderVerdict(
            probe_token=probe_token, folder="missing", searched=tuple(searched)
        )
    finally:
        _close_quietly(client)


class _ImapConnection:
    """The same connect the reply collector makes, without its folder.

    ``ImapMailbox`` binds one folder at construction because a reply collector
    only ever reads INBOX. This needs to move between folders on one
    connection, so the connect is reused and the selecting is not.
    """

    def __init__(self, config: ImapConfig) -> None:
        self._config = config

    def open(self) -> imaplib.IMAP4:
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
        imap_login(
            client,
            username=config.username,
            password=config.password,
            auth=config.auth,
            client_id=config.client_id,
        )
        return client


async def find_probe(
    config: ImapConfig, *, provider: str, probe_token: str
) -> FolderVerdict:
    """Where this probe landed, or ``missing``."""
    return await asyncio.to_thread(find_probe_blocking, config, provider, probe_token)


__all__ = [
    "FOLDER_ORDER",
    "GMAIL_PROMOTIONS",
    "FolderVerdict",
    "find_probe",
    "find_probe_blocking",
]
