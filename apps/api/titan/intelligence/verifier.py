"""The mailbox verification port.

Only one thing can establish that a specific mailbox exists: asking the server
that would receive its mail. This module is the socket that answer plugs into.
Adding or changing a verifier is an adapter plus a settings value -- never a
change to the discovery pipeline, the eligibility rules or the send gate, all
of which depend on this protocol rather than on any implementation.

Two implementations of the real thing exist. A vendor adapter
(:mod:`titan.intelligence.instantly_verifier`) buys the answer, and
:mod:`titan.intelligence.smtp_probe` asks for it directly, against the
minority of domains where asking is both truthful and safe -- it declines to
open a connection at all to the large providers and filtering front-ends that
accept every recipient. That module's docstring sets out why probing is
defensible there and nowhere else; this one only cares that both produce a
:class:`VerificationResult`.

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
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from titan.db.enums import VerificationStatus

logger = logging.getLogger(__name__)


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
#:
#: The two that need no configuration are constructed by name. A vendor adapter
#: needs a key, so it is built from settings in :func:`build_verifier` instead
#: of being called with no arguments here.
_REGISTRY: dict[str, type[NullVerifier] | type[DeterministicVerifier]] = {
    NullVerifier.name: NullVerifier,
    DeterministicVerifier.name: DeterministicVerifier,
}


def _instantly(settings: Any) -> MailboxVerifier:
    """Instantly's verifier, or the null one when it is not usable.

    Falling back rather than raising, for the same reason an unrecognised name
    does: a missing key must not take the discovery pipeline down, and the null
    verifier is safe in the direction that matters -- it can never mark an
    address sendable. Loud in the log, harmless in behaviour.
    """
    key = getattr(settings, "instantly_api_key", None)
    if key is None:
        logger.error(
            "TITAN_MAILBOX_VERIFIER=instantly but TITAN_INSTANTLY_API_KEY is not "
            "set; falling back to the null verifier, which verifies nothing"
        )
        return NullVerifier()

    from titan.intelligence.instantly_verifier import InstantlyVerifier
    from titan.providers.instantly import InstantlyClient

    # from_settings, not the raw value: unwrapping a SecretStr is confined to
    # the provider layer by an invariant test, and this module is not it.
    return InstantlyVerifier(InstantlyClient.from_settings(settings))


def _smtp_probe(settings: Any) -> MailboxVerifier:
    """Titan's own probe, or the null one when it cannot identify itself.

    Falling back rather than raising, for the same reason ``_instantly`` does.
    The difference is what the missing configuration means here: without a real
    HELO name and a real envelope sender, the probe would be indistinguishable
    from the abusive kind, so refusing to build it is protecting somebody else
    rather than protecting Titan.
    """
    from titan.intelligence.smtp_probe import ProbeConfigError, SmtpProbeVerifier

    try:
        return SmtpProbeVerifier(
            helo_hostname=getattr(settings, "smtp_probe_helo", None) or "",
            mail_from=getattr(settings, "smtp_probe_mail_from", None) or "",
            timeout_seconds=float(getattr(settings, "smtp_probe_timeout_seconds", 12)),
            concurrency=int(getattr(settings, "smtp_probe_concurrency", 4)),
        )
    except ProbeConfigError as exc:
        logger.error(
            "TITAN_MAILBOX_VERIFIER=smtp_probe but it cannot identify itself; "
            "falling back to the null verifier, which verifies nothing",
            extra={"detail": str(exc)},
        )
        return NullVerifier()


#: One verifier per name, per event loop.
#:
#: Rebuilding per address was harmless while every verifier was stateless, and
#: the eligibility activity does exactly that -- once per candidate address.
#: :class:`~titan.intelligence.smtp_probe.SmtpProbeVerifier` is not stateless:
#: its catch-all cache, its per-domain lock and the spacing between connections
#: are the whole of what stops it opening fifty connections to one small mail
#: server, and a fresh instance per address has none of them. A vendor adapter
#: benefits too, keeping one HTTP connection pool instead of one per lead.
#:
#: Keyed by event loop as well as by name. The locks inside bind to the loop
#: they are first awaited on, so an instance reused from a second loop raises
#: rather than quietly working a little bit wrong -- which is a shape of bug
#: that would only appear under a second worker.
_INSTANCES: dict[tuple[str, int], MailboxVerifier] = {}


def _loop_key() -> int:
    """Which event loop is asking, or 0 outside one."""
    import asyncio

    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def reset_verifier_cache() -> None:
    """Drop every cached verifier. For tests, and for a settings reload."""
    _INSTANCES.clear()


def build_verifier(name: str | None, settings: Any = None) -> MailboxVerifier:
    """The configured verifier, or the null one.

    An unrecognised name falls back to null rather than raising. A typo in
    configuration must not take the discovery pipeline down, and the null
    verifier's answers are safe in the direction that matters: it can never
    mark an address sendable.

    ``settings`` is optional so every existing caller keeps working. A vendor
    adapter without it falls back to null and says so, rather than being
    constructed with no credentials and failing on the first address.
    """
    key = (name or "").strip().lower()
    cache_key = (key, _loop_key())
    cached = _INSTANCES.get(cache_key)
    if cached is not None:
        return cached

    if key in ("instantly", "smtp_probe"):
        if settings is None:
            from titan.config import get_settings

            settings = get_settings()
        built: MailboxVerifier = (
            _instantly(settings) if key == "instantly" else _smtp_probe(settings)
        )
    else:
        built = _REGISTRY.get(key, NullVerifier)()

    _INSTANCES[cache_key] = built
    return built


__all__ = [
    "DeterministicVerifier",
    "MailboxVerifier",
    "NullVerifier",
    "VerificationResult",
    "build_verifier",
    "reset_verifier_cache",
]
