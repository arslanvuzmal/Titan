"""The mailboxes Titan reads to find out where its own mail landed.

A seed is an address we own at a provider we want measured. Titan sends itself
a message, waits for the filter to make up its mind, then logs in and looks --
because no provider will tell you. A "delivered" webhook is equally true of a
message dropped into junk, and on 26 September six probes proved the point:
accepted at both Gmail and Outlook, filed as spam at both.

**A separate file from the sending mailboxes, deliberately.** Both hold an
address and an IMAP credential and the temptation is to add a ``seed: true``
flag to ``mailboxes.json``. That flag would be one edit away from a seed
appearing in the sender pool, and a seed in the sender pool means cold outreach
going out from a personal Gmail. Two files cannot make that mistake: nothing
reads this one looking for somewhere to send from.

**No SMTP half.** A seed is never sent *from*. Anything here that carried SMTP
credentials would be a sending mailbox nobody had decided to have.

**Outlook.com cannot be seeded this way, and that is Microsoft's decision.**
Personal Outlook, Hotmail, Live and MSN accounts stopped accepting basic
authentication on 16 September 2024; an app password against
``outlook.office365.com`` now returns NO LOGIN immediately. Reading a Microsoft
seed needs OAuth 2.0 -- an Azure app registration and a refresh token -- which
is a different shape from a password in a file and is not built here.

That matters more than it sounds: Microsoft is the largest single bucket in the
send list, 704 of 1,845 untouched addresses. Until OAuth exists, Microsoft
placement is measured by a person looking, and a registry holding only Gmail is
covering the *second* largest bucket while the largest goes unmeasured. The
report calls an unmeasured pairing "not measured" and sorts it above even total
failure for exactly this reason.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass
from typing import Any

from titan.delivery.mailboxes import Endpoint, MailboxConfigError, _endpoint
from titan.delivery.placement import provider_of

logger = logging.getLogger(__name__)


class SeedConfigError(MailboxConfigError):
    """The seed file cannot be trusted. Never partially applied.

    Inherits from ``MailboxConfigError`` so a caller that already handles a
    broken credential file handles this one too, and because the failure is
    the same kind: a file with one entry silently dropped is a measurement
    that has quietly stopped covering a provider, which looks exactly like a
    provider that is behaving.
    """


@dataclass(frozen=True, slots=True)
class Seed:
    """One address we own, and how to read it."""

    address: str
    imap: Endpoint
    #: "gmail", "outlook" or "other" -- which filter this seed measures, not
    #: which brand hosts it. Derived rather than declared: a file that can
    #: claim a gmail.com address is measuring Outlook is a file that can
    #: report the wrong provider's verdict forever.
    provider: str
    label: str = ""

    def redacted(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "provider": self.provider,
            "label": self.label,
            "imap": self.imap.redacted(),
        }


class SeedRegistry:
    """The seeds, grouped by the filter each one measures."""

    def __init__(self, seeds: list[Seed]) -> None:
        self._seeds = list(seeds)

    def __len__(self) -> int:
        return len(self._seeds)

    def all(self) -> list[Seed]:
        return list(self._seeds)

    def providers(self) -> list[str]:
        """Which filters are covered, in a stable order.

        Stable because the probe rotation indexes into it, and a rotation over
        a set that reorders itself between runs would re-probe one provider
        and skip another without anything looking wrong.
        """
        return sorted({seed.provider for seed in self._seeds})

    def for_provider(self, provider: str) -> list[Seed]:
        return [seed for seed in self._seeds if seed.provider == provider]


def parse_seeds(document: dict[str, Any]) -> SeedRegistry:
    """Turn the parsed file into a registry, or refuse the whole thing."""
    entries = document.get("seeds")
    if not isinstance(entries, list):
        found = type(entries).__name__ if entries is not None else "nothing"
        raise SeedConfigError(f"the file must hold a 'seeds' list; got {found}")

    seeds: list[Seed] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SeedConfigError(f"seed {index}: expected an object")
        if entry.get("enabled") is False:
            continue

        address = str(entry.get("address") or "").strip()
        if "@" not in address:
            raise SeedConfigError(f"seed {index}: {address!r} is not an email address")
        where = address

        if "smtp" in entry:
            raise SeedConfigError(
                f"{where}: a seed is only ever read, never sent from. Remove "
                f"'smtp' -- an address with sending credentials in this file "
                f"is a sending mailbox nobody decided to have."
            )

        folded = address.lower()
        if folded in seen:
            raise SeedConfigError(f"{where}: listed twice")
        seen.add(folded)

        imap_raw = entry.get("imap")
        if not isinstance(imap_raw, dict) or not imap_raw:
            raise SeedConfigError(
                f"{where}: 'imap' is required; a seed nobody can read measures nothing"
            )
        imap = _endpoint(
            imap_raw, where=f"{where} imap", default_port=993, fallback_username=address
        )
        if imap.security == "none":
            raise SeedConfigError(
                f"{where} imap: cleartext IMAP is never accepted; it reads mail "
                f"as well as authenticating"
            )

        seeds.append(
            Seed(
                address=address,
                imap=imap,
                provider=provider_of(address),
                label=str(entry.get("label") or ""),
            )
        )

    return SeedRegistry(seeds)


def load_seeds(path: str | pathlib.Path | None) -> SeedRegistry:
    """Read the seed file. An unset path yields an empty registry.

    Empty is not an error: placement measurement is an addition to the estate,
    and a deployment without it sends exactly as it did before. What it must
    not do is look like it is measuring when it is not, which is why the probe
    workflow refuses to run against an empty registry rather than reporting a
    clean round of nothing.
    """
    if not path:
        return SeedRegistry([])

    file = pathlib.Path(path)
    if not file.exists():
        raise SeedConfigError(f"seed file {file} does not exist")
    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SeedConfigError(f"seed file {file} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SeedConfigError(f"seed file {file} must hold a JSON object")

    registry = parse_seeds(document)
    logger.info(
        "seed mailboxes loaded",
        extra={
            "path": str(file),
            "seeds": len(registry),
            "providers": registry.providers(),
        },
    )
    return registry


__all__ = ["Seed", "SeedConfigError", "SeedRegistry", "load_seeds", "parse_seeds"]
