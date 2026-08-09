"""MX verification for a recipient domain.

Mission section 11. Answers one narrow question: *can this domain receive mail
at all?*

The asymmetry is the whole point, and it is easy to get backwards:

* **No MX (and no A/AAAA fallback) is conclusive.** A domain that publishes no
  way to receive mail cannot receive mail. Every address at it is undeliverable,
  and sending produces a hard bounce that costs sender reputation. This is a
  disqualifier.
* **Having MX proves nothing about a mailbox.** ``gmail.com`` has MX records;
  that says nothing about whether ``notarealperson@gmail.com`` exists. So a
  positive result never upgrades ``verification_status`` -- the rule
  :func:`titan.intelligence.contacts.mx_presence_is_not_verification` already
  documents, and which this module implements rather than contradicts.

What this is **not**: an SMTP ``RCPT TO`` probe. Asking a mail server whether a
stranger's mailbox exists is unreliable (most large providers accept everything
and bounce later) and is treated as abuse -- it gets the prober rate-limited or
blocklisted, damaging the very reputation Titan works to protect. Titan does not
do it. A mailbox-level answer requires a verification service, which is a
deliberate purchasing decision, not something to sneak in behind a DNS call.

RFC 5321 §5.1: a domain with no MX but with an address record is still a valid
mail destination -- the A record is the implicit fallback. Treating that as
undeliverable would discard legitimate small-business domains, which is exactly
the population Titan targets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

logger = logging.getLogger(__name__)

#: Bounded so one unresponsive nameserver cannot stall a discovery run.
DNS_TIMEOUT_SECONDS = 8.0


class MxStatus(StrEnum):
    #: MX records published. The domain accepts mail. Says nothing about a mailbox.
    PRESENT = "mx_present"
    #: No MX, but an address record exists: valid implicit destination (RFC 5321).
    IMPLICIT_A = "implicit_a_record"
    #: Domain resolves but publishes no way to receive mail. Undeliverable.
    ABSENT = "no_mail_exchanger"
    #: Domain does not exist. Undeliverable.
    NXDOMAIN = "domain_does_not_exist"
    #: Lookup failed. Says nothing either way -- never treated as a disqualifier.
    ERROR = "lookup_failed"


@dataclass(frozen=True, slots=True)
class MxCheck:
    status: MxStatus
    domain: str
    hosts: tuple[str, ...] = ()
    detail: str | None = None

    @property
    def can_receive_mail(self) -> bool:
        """True when the domain has a route for inbound mail."""
        return self.status in (MxStatus.PRESENT, MxStatus.IMPLICIT_A)

    @property
    def is_conclusively_undeliverable(self) -> bool:
        """True only when DNS gave a definite negative.

        A failed lookup is *not* undeliverable. Treating a timeout as a
        disqualifier would silently drop good leads whenever the resolver had a
        bad minute, and the failure would look identical to a real answer.
        """
        return self.status in (MxStatus.ABSENT, MxStatus.NXDOMAIN)

    def as_verification_detail(self) -> dict[str, object]:
        """Shaped for ContactVerification.detail (append-only, diagnostic)."""
        return {
            "check": "mx",
            "status": self.status.value,
            "hosts": list(self.hosts),
            "detail": self.detail,
            "note": (
                "MX presence proves the domain accepts mail, not that this "
                "mailbox exists; it never upgrades verification_status"
            ),
        }


class MxResolver(Protocol):
    """Injectable DNS, so tests need no network.

    Returns (mx_hosts, has_address_record). Raises for NXDOMAIN.
    """

    def __call__(self, domain: str) -> tuple[list[str], bool]: ...


class DomainDoesNotExist(Exception):
    """The domain is not registered. Distinct from 'no MX published'."""


def system_mx_resolver(domain: str) -> tuple[list[str], bool]:
    """Resolve MX, falling back to A/AAAA per RFC 5321."""
    import dns.resolver  # type: ignore[import-untyped]

    hosts: list[str] = []
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=DNS_TIMEOUT_SECONDS)
        hosts = sorted(
            (str(r.exchange).rstrip(".") for r in answers),
            key=lambda h: h,
        )
    except dns.resolver.NXDOMAIN as exc:
        raise DomainDoesNotExist(domain) from exc
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        hosts = []

    if hosts:
        return hosts, True

    for record_type in ("A", "AAAA"):
        try:
            dns.resolver.resolve(domain, record_type, lifetime=DNS_TIMEOUT_SECONDS)
            return [], True
        except dns.resolver.NXDOMAIN as exc:
            raise DomainDoesNotExist(domain) from exc
        except Exception as exc:
            # An IPv4-only host has no AAAA and vice versa, so one absent
            # record type is expected rather than exceptional. Logged at debug
            # so a genuinely broken resolver is still traceable.
            logger.debug(
                "no %s record for %s (%s)", record_type, domain, type(exc).__name__
            )
    return [], False


def check_mx(domain: str, *, resolver: MxResolver = system_mx_resolver) -> MxCheck:
    """Whether ``domain`` can receive mail."""
    normalized = (domain or "").strip().lower().rstrip(".")
    if not normalized or "." not in normalized:
        return MxCheck(
            MxStatus.NXDOMAIN, normalized, detail="not a resolvable domain name"
        )

    try:
        hosts, has_address = resolver(normalized)
    except DomainDoesNotExist:
        return MxCheck(MxStatus.NXDOMAIN, normalized, detail="NXDOMAIN")
    except Exception as exc:
        # Never a disqualifier: an unreachable resolver is our problem.
        logger.warning(
            "mx lookup failed",
            extra={"domain": normalized, "error_code": type(exc).__name__},
        )
        return MxCheck(MxStatus.ERROR, normalized, detail=f"{type(exc).__name__}: {exc}")

    if hosts:
        return MxCheck(MxStatus.PRESENT, normalized, hosts=tuple(hosts))
    if has_address:
        return MxCheck(
            MxStatus.IMPLICIT_A,
            normalized,
            detail="no MX; address record is the implicit destination (RFC 5321)",
        )
    return MxCheck(
        MxStatus.ABSENT, normalized, detail="domain publishes no route for inbound mail"
    )


@dataclass(frozen=True, slots=True)
class BulkMxResult:
    checks: dict[str, MxCheck] = field(default_factory=dict)

    def undeliverable_domains(self) -> tuple[str, ...]:
        return tuple(d for d, c in self.checks.items() if c.is_conclusively_undeliverable)


def check_many(
    domains: list[str], *, resolver: MxResolver = system_mx_resolver
) -> BulkMxResult:
    """Check a batch, resolving each distinct domain once."""
    checks: dict[str, MxCheck] = {}
    for domain in domains:
        key = (domain or "").strip().lower().rstrip(".")
        if key and key not in checks:
            checks[key] = check_mx(key, resolver=resolver)
    return BulkMxResult(checks=checks)


__all__ = [
    "BulkMxResult",
    "DomainDoesNotExist",
    "MxCheck",
    "MxResolver",
    "MxStatus",
    "check_many",
    "check_mx",
    "system_mx_resolver",
]
