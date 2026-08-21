"""Mailbox verification through Instantly's own endpoint.

The first real implementation of the port in :mod:`titan.intelligence.verifier`.
Everything before it was the null verifier, which says UNKNOWN about everything,
and the deterministic fake, which is for tests.

**Why this one.** Titan holds 601 addresses and every one of them is
``published_first_party`` -- the business printed it on its own website. That is
real provenance and it is not proof that the mailbox accepts mail. Two of five
bounces on the live workspace were an address the crawler invented from a phone
number; the rest were published, valid-looking, and dead or unmonitored. Only a
mailbox-level check separates those from the rest, and Titan deliberately does
not perform one itself -- see :mod:`titan.intelligence.mx` for why probing a
stranger's mail server is a reputation expense rather than a feature.

Instantly publishes ``/email-verification`` and the workspace may already be
paying for it as a sending carrier, which is the whole argument: one account
closes the carrier gap and the verification gap together.

**The catch-all answer is the valuable one.** A domain that accepts every local
part cannot tell you anything about a specific mailbox, and about a third of
small-business hosting is configured that way. Reported separately from the
status so that the engine can apply its own rule -- an address on a catch-all
domain is sendable only on the strength of who published it -- rather than
treating "the server said yes" as "the mailbox exists".

**Unmapped verdicts stay UNKNOWN.** Instantly's status vocabulary is not
contractually stable, and guessing that an unrecognised word means "deliverable"
is how an invalid address gets sent to. UNKNOWN cannot upgrade an address and
cannot condemn one, so an unfamiliar answer costs a verification and changes
nothing else.
"""

from __future__ import annotations

import logging
from typing import Any

from titan.db.enums import VerificationStatus
from titan.intelligence.verifier import VerificationResult
from titan.providers.instantly import InstantlyClient, InstantlyError

logger = logging.getLogger(__name__)

#: Instantly's verification verdicts, mapped onto Titan's vocabulary.
#:
#: Both spellings of each appear in their documentation and in community
#: reports, so both are accepted. Anything absent is UNKNOWN by construction --
#: see the module docstring.
_STATUS_MAP: dict[str, VerificationStatus] = {
    "valid": VerificationStatus.PROVIDER_VERIFIED,
    "verified": VerificationStatus.PROVIDER_VERIFIED,
    "deliverable": VerificationStatus.PROVIDER_VERIFIED,
    "invalid": VerificationStatus.INVALID,
    "undeliverable": VerificationStatus.INVALID,
    "risky": VerificationStatus.RISKY,
    "accept_all": VerificationStatus.CATCH_ALL,
    "accept-all": VerificationStatus.CATCH_ALL,
    "catch_all": VerificationStatus.CATCH_ALL,
    "catch-all": VerificationStatus.CATCH_ALL,
    "unknown": VerificationStatus.UNKNOWN,
    "pending": VerificationStatus.UNKNOWN,
}

#: Response keys the verdict may arrive under.
_STATUS_KEYS = ("verification_status", "status", "result", "state")


def read_status(payload: dict[str, Any]) -> tuple[VerificationStatus, str | None]:
    """Instantly's verdict, and the raw word it used.

    Returns UNKNOWN and the raw word when the word is not one we map, so the
    caller can log what was actually said. A verdict nobody recognised is a
    reason to look, not a reason to guess.
    """
    for key in _STATUS_KEYS:
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            word = raw.strip().lower()
            return _STATUS_MAP.get(word, VerificationStatus.UNKNOWN), word
    return VerificationStatus.UNKNOWN, None


class InstantlyVerifier:
    """Buys the one answer Titan will not make for itself."""

    name = "instantly"

    def __init__(self, client: InstantlyClient) -> None:
        self._client = client

    async def verify(self, email: str) -> VerificationResult:
        try:
            payload = await self._client.verify_email(email)
        except InstantlyError as exc:
            # An outage must not condemn an address. UNKNOWN leaves the local
            # layers' answer standing, which is what the pipeline already does
            # when no verifier is configured at all.
            logger.warning(
                "mailbox verification unavailable",
                extra={"error": str(exc)[:200], "verifier": self.name},
            )
            return VerificationResult(
                status=VerificationStatus.UNKNOWN,
                provider=self.name,
                detail=f"verification unavailable: {exc}",
            )

        status, word = read_status(payload)
        if (
            word is not None
            and status is VerificationStatus.UNKNOWN
            and word
            not in (
                "unknown",
                "pending",
            )
        ):
            logger.warning(
                "unmapped verification verdict; treated as unknown",
                extra={"verdict": word[:40], "verifier": self.name},
            )

        catch_all = status is VerificationStatus.CATCH_ALL or bool(
            payload.get("catch_all") or payload.get("accept_all")
        )
        return VerificationResult(
            status=status,
            provider=self.name,
            is_catch_all=catch_all,
            detail=word,
            raw=payload,
        )

    async def health_check(self) -> tuple[bool, str]:
        return await self._client.health_check()


__all__ = ["InstantlyVerifier", "read_status"]
