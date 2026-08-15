"""The mailbox verification port.

Only one thing can establish that a specific mailbox exists, and Titan does not
do it itself. :mod:`titan.intelligence.mx` explains why at length; the short
version is that asking a stranger's mail server whether a stranger's mailbox
exists gets a truthful answer from almost nobody and gets the asker
rate-limited or blocklisted by the rest. Building that prober would damage the
sending reputation the rest of this package exists to protect.

So the mailbox-level answer is bought, not made, and this module is the socket
it plugs into. Adding a verification service becomes an adapter plus a settings
value -- never a change to the discovery pipeline, the eligibility rules or the
send gate, all of which depend on this protocol rather than on any vendor.

**The default asserts nothing.** :class:`NullVerifier` returns UNKNOWN for every
address, which is the honest answer when nobody has been asked. UNKNOWN is not
in ``SENDABLE_VERIFICATION_STATUSES``, so it cannot upgrade an address -- but it
cannot condemn one either, and that is what keeps the engine's other layers
working normally on a deployment with no verification service configured. An
unconfigured verifier degrades the engine's ceiling, not its floor.

**Verification runs at discovery, never at send time.** A verification call is a
network round trip to a third party, and the outbox worker holds a database lease
while it processes a row. Putting a vendor's latency inside that lease means one
slow provider stalls the send queue, and a provider outage stops mail that was
already authorized. The result is stored on the contact channel; the send gate
reads the stored status.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from titan.db.enums import VerificationStatus


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """One verification service's answer about one address."""

    status: VerificationStatus
    provider: str
    #: True when the service reported that the domain accepts every local part.
    #: Carried separately from ``status`` because a service can report both
    #: "this mailbox accepts" and "so does every other one", and the second
    #: fact is what makes the first meaningless.
    is_catch_all: bool = False
    detail: str | None = None
    #: The provider's own payload, stored on ContactVerification for audit.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_conclusive(self) -> bool:
        """Whether this answer settles the question.

        UNKNOWN never does -- it is the answer given when the service was not
        asked, could not answer, or timed out, and treating it as settled would
        turn an outage into a permanent verdict on a good address.
        """
        return self.status is not VerificationStatus.UNKNOWN

    def as_verification_detail(self) -> dict[str, object]:
        """Shaped for ContactVerification.detail (append-only, diagnostic)."""
        return {
            "check": "mailbox_verification",
            "provider": self.provider,
            "status": self.status.value,
            "is_catch_all": self.is_catch_all,
            "detail": self.detail,
            "raw": self.raw,
        }


@runtime_checkable
class MailboxVerifier(Protocol):
    """The complete surface Titan depends on for mailbox-level verification."""

    name: str

    async def verify(self, email: str) -> VerificationResult: ...

    async def health_check(self) -> tuple[bool, str]: ...


class NullVerifier:
    """The default: answers UNKNOWN, asserts nothing, costs nothing.

    Deliberately not a failure mode. A deployment with no verification service
    is the normal state of this system today, and the engine is designed to
    reach a sound decision without one -- syntax, provenance, disposable and
    lookalike domains, and MX between them refuse most of what would bounce.
    What is lost without a real verifier is the ability to say *deliverable*, so
    the ceiling drops from PROVIDER_VERIFIED to PUBLISHED_FIRST_PARTY.
    """

    name = "null"

    async def verify(self, email: str) -> VerificationResult:
        return VerificationResult(
            status=VerificationStatus.UNKNOWN,
            provider=self.name,
            detail="no mailbox verification service is configured",
        )

    async def health_check(self) -> tuple[bool, str]:
        return True, "null verifier: always available, never informative"


class DeterministicVerifier:
    """A fake for tests and local development. Never for production.

    Derives a stable verdict from a hash of the address, so a given address
    always gets the same answer and a test can assert on real engine behaviour
    without a network or a vendor account. The distribution is roughly the shape
    a real list produces -- mostly deliverable, a minority catch-all, a few
    invalid -- which is what makes it useful for exercising the branches rather
    than only the happy one.
    """

    name = "deterministic"

    #: Buckets over 100, in order. Chosen to resemble a scraped list rather
    #: than a clean one: catch-all is common in small-business hosting.
    _DELIVERABLE_BELOW = 60
    _CATCH_ALL_BELOW = 82
    _RISKY_BELOW = 92

    def __init__(self, *, salt: str = "titan") -> None:
        self._salt = salt

    def _bucket(self, email: str) -> int:
        digest = hashlib.sha256(f"{self._salt}:{email.strip().lower()}".encode()).digest()
        return digest[0] % 100

    async def verify(self, email: str) -> VerificationResult:
        bucket = self._bucket(email)
        if bucket < self._DELIVERABLE_BELOW:
            return VerificationResult(
                status=VerificationStatus.PROVIDER_VERIFIED,
                provider=self.name,
                detail="mailbox accepted",
            )
        if bucket < self._CATCH_ALL_BELOW:
            return VerificationResult(
                status=VerificationStatus.CATCH_ALL,
                provider=self.name,
                is_catch_all=True,
                detail="domain accepts every local part",
            )
        if bucket < self._RISKY_BELOW:
            return VerificationResult(
                status=VerificationStatus.RISKY,
                provider=self.name,
                detail="mailbox full or temporarily rejecting",
            )
        return VerificationResult(
            status=VerificationStatus.INVALID,
            provider=self.name,
            detail="mailbox does not exist",
        )

    async def health_check(self) -> tuple[bool, str]:
        return True, "deterministic verifier: for tests only"


#: Resolvable names, so a settings value selects an adapter without the caller
#: importing one. A real vendor adapter is added here and nowhere else.
_REGISTRY: dict[str, type[NullVerifier] | type[DeterministicVerifier]] = {
    NullVerifier.name: NullVerifier,
    DeterministicVerifier.name: DeterministicVerifier,
}


def build_verifier(name: str | None) -> MailboxVerifier:
    """The configured verifier, or the null one.

    An unrecognised name falls back to null rather than raising. A typo in
    configuration must not take the discovery pipeline down, and the null
    verifier's answers are safe in the direction that matters: it can never
    mark an address sendable.
    """
    factory = _REGISTRY.get((name or "").strip().lower(), NullVerifier)
    return factory()


__all__ = [
    "DeterministicVerifier",
    "MailboxVerifier",
    "NullVerifier",
    "VerificationResult",
    "build_verifier",
]
