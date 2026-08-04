"""SSRF defence for every URL Titan is asked to fetch.

Replaces ``app.tools.implementations.web_search_tool._validate_url_for_ssrf``
(gap analysis C-02), which was unsound in five distinct ways:

1. It was only ever called on a hardcoded constant, never on attacker-influenced
   input, so it protected nothing.
2. ``socket.gethostbyname`` is IPv4-only -- an AAAA-only host bypassed it.
3. It checked only the *first* resolved address; a multi-record host bypassed it.
4. It had no scheme, port, redirect, size, or time bounds.
5. Classic TOCTOU: it resolved the name, then httpx resolved it again
   independently, so DNS rebinding won every time.

The design here closes all five:

* every address returned by ``getaddrinfo`` (A **and** AAAA) must pass;
* IPv4-mapped and 6to4/Teredo-embedded IPv6 addresses are unwrapped and the
  embedded IPv4 re-checked, so ``::ffff:169.254.169.254`` is refused;
* scheme and port are allowlisted;
* the vetted IP is returned to the caller so the connection can be **pinned**
  to the address that was checked, closing the rebinding window;
* every redirect hop is revalidated by the same function.

This module performs no I/O other than DNS resolution and holds no credentials.
Actual page fetching happens in the isolated browser worker.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
ALLOWED_PORTS: frozenset[int] = frozenset({80, 443})
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

#: Hostnames that resolve to link-local metadata services on the major clouds.
#: Blocked by name as well as by address, because some of them resolve to
#: addresses that look ordinary from outside the provider's network.
METADATA_HOSTNAMES: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
        "instance-data",
        "instance-data.ec2.internal",
        "169.254.169.254",
        "fd00:ec2::254",
        "100.100.100.200",  # Alibaba Cloud
        "metadata.packet.net",
        "metadata.platformequinix.com",
    }
)

#: Suffixes that only ever name internal hosts.
BLOCKED_SUFFIXES: tuple[str, ...] = (
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".corp",
    ".home",
    ".lan",
    ".onion",
    ".test",
    ".example",
    ".invalid",
    ".localhost",
)

BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)

MAX_URL_LENGTH = 2048
MAX_HOSTNAME_LENGTH = 253

_HOSTNAME_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


class BlockReason(StrEnum):
    MALFORMED = "malformed_url"
    TOO_LONG = "url_too_long"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    PORT_NOT_ALLOWED = "port_not_allowed"
    CREDENTIALS_IN_URL = "credentials_in_url"
    MISSING_HOST = "missing_host"
    BLOCKED_HOSTNAME = "blocked_hostname"
    BLOCKED_SUFFIX = "blocked_suffix"
    METADATA_HOST = "cloud_metadata_host"
    INVALID_HOSTNAME = "invalid_hostname"
    DNS_FAILURE = "dns_resolution_failed"
    NO_ADDRESSES = "no_addresses_resolved"
    PRIVATE_ADDRESS = "private_or_reserved_address"
    MIXED_RESOLUTION = "mixed_public_and_private_resolution"
    TOO_MANY_REDIRECTS = "too_many_redirects"


@dataclass(frozen=True, slots=True)
class UrlVerdict:
    allowed: bool
    url: str
    reason: BlockReason | None = None
    detail: str | None = None
    hostname: str | None = None
    port: int | None = None
    scheme: str | None = None
    #: Every address the hostname resolved to, all of which passed. Pin the
    #: connection to one of these to defeat DNS rebinding.
    resolved_ips: tuple[str, ...] = field(default=())

    def __bool__(self) -> bool:
        return self.allowed


class Resolver(Protocol):
    """Injectable name resolution, so tests need no network."""

    def __call__(self, host: str, port: int) -> list[str]: ...


def system_resolver(host: str, port: int) -> list[str]:
    """Resolve a hostname to every A and AAAA address."""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def _unwrap(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return the address plus any IPv4 address embedded inside it.

    ``::ffff:127.0.0.1`` (IPv4-mapped), ``2002:7f00:1::`` (6to4), and Teredo
    addresses all carry an IPv4 address that the outer form can hide from a
    naive ``is_private`` check.
    """
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        for embedded in (
            ip.ipv4_mapped,
            ip.sixtofour,
            ip.teredo[1] if ip.teredo else None,
        ):
            if embedded is not None:
                out.append(embedded)
    return out


def is_public_address(ip_text: str) -> bool:
    """Whether an address is safe to connect to from a research worker."""
    try:
        parsed = ipaddress.ip_address(ip_text)
    except ValueError:
        return False

    for candidate in _unwrap(parsed):
        if (
            candidate.is_private
            or candidate.is_loopback
            or candidate.is_link_local
            or candidate.is_multicast
            or candidate.is_reserved
            or candidate.is_unspecified
        ):
            return False
        # 0.0.0.0/8 ("this network") is not covered by is_reserved on all
        # Python versions and routes to localhost on Linux.
        if isinstance(candidate, ipaddress.IPv4Address):
            if candidate in ipaddress.ip_network("0.0.0.0/8"):
                return False
            # Carrier-grade NAT: not private per RFC1918 but never a customer
            # site, and commonly used for internal infrastructure.
            if candidate in ipaddress.ip_network("100.64.0.0/10"):
                return False
            if candidate in ipaddress.ip_network("192.0.0.0/24"):
                return False
        else:
            # Unique-local (fc00::/7) is not flagged private by every Python
            # version; check explicitly.
            if candidate in ipaddress.ip_network("fc00::/7"):
                return False
    return True


def _hostname_is_syntactically_valid(host: str) -> bool:
    if len(host) > MAX_HOSTNAME_LENGTH:
        return False
    labels = host.rstrip(".").split(".")
    return all(_HOSTNAME_LABEL.match(label) for label in labels)


def validate_url(
    raw_url: str,
    *,
    resolver: Resolver = system_resolver,
    require_https: bool = False,
) -> UrlVerdict:
    """Decide whether Titan may fetch ``raw_url``.

    On success the verdict carries every resolved address; the caller must pin
    its connection to one of them rather than re-resolving the name.
    """
    if not raw_url or len(raw_url) > MAX_URL_LENGTH:
        return UrlVerdict(False, raw_url, BlockReason.TOO_LONG, "url length out of range")

    try:
        parts = urlsplit(raw_url.strip())
    except ValueError as exc:
        return UrlVerdict(False, raw_url, BlockReason.MALFORMED, str(exc))

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return UrlVerdict(
            False, raw_url, BlockReason.SCHEME_NOT_ALLOWED, f"scheme {scheme!r}"
        )
    if require_https and scheme != "https":
        return UrlVerdict(
            False, raw_url, BlockReason.SCHEME_NOT_ALLOWED, "https required"
        )

    # user:pass@host is a common SSRF/phishing obfuscation and Titan never needs it.
    if parts.username or parts.password:
        return UrlVerdict(
            False, raw_url, BlockReason.CREDENTIALS_IN_URL, "userinfo present"
        )

    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:  # malformed port
        return UrlVerdict(False, raw_url, BlockReason.MALFORMED, str(exc))

    if not hostname:
        return UrlVerdict(False, raw_url, BlockReason.MISSING_HOST, "no hostname")

    host = hostname.lower().rstrip(".")
    port = port or DEFAULT_PORTS[scheme]
    if port not in ALLOWED_PORTS:
        return UrlVerdict(False, raw_url, BlockReason.PORT_NOT_ALLOWED, f"port {port}")

    if host in BLOCKED_HOSTNAMES:
        return UrlVerdict(False, raw_url, BlockReason.BLOCKED_HOSTNAME, host)
    if host in METADATA_HOSTNAMES:
        return UrlVerdict(False, raw_url, BlockReason.METADATA_HOST, host)
    if host.endswith(BLOCKED_SUFFIXES):
        return UrlVerdict(False, raw_url, BlockReason.BLOCKED_SUFFIX, host)

    # An IP literal skips DNS entirely; validate it directly. This also catches
    # decimal/octal/hex integer forms, which ip_address rejects outright.
    literal = _as_ip_literal(host)
    if literal is not None:
        if not is_public_address(literal):
            return UrlVerdict(
                False, raw_url, BlockReason.PRIVATE_ADDRESS, f"literal {literal}"
            )
        return UrlVerdict(
            True,
            raw_url,
            hostname=host,
            port=port,
            scheme=scheme,
            resolved_ips=(literal,),
        )

    if not _hostname_is_syntactically_valid(host):
        return UrlVerdict(False, raw_url, BlockReason.INVALID_HOSTNAME, host)

    try:
        addresses = resolver(host, port)
    except Exception as exc:
        return UrlVerdict(
            False, raw_url, BlockReason.DNS_FAILURE, f"{type(exc).__name__}: {exc}"
        )

    if not addresses:
        return UrlVerdict(False, raw_url, BlockReason.NO_ADDRESSES, host)

    # EVERY address must pass. A host with one public and one private record is
    # a rebinding attempt, not a misconfiguration -- refuse the whole host.
    bad = [a for a in addresses if not is_public_address(a)]
    if bad:
        reason = (
            BlockReason.MIXED_RESOLUTION
            if len(bad) < len(addresses)
            else BlockReason.PRIVATE_ADDRESS
        )
        return UrlVerdict(False, raw_url, reason, f"{host} -> {', '.join(sorted(bad))}")

    return UrlVerdict(
        True,
        raw_url,
        hostname=host,
        port=port,
        scheme=scheme,
        resolved_ips=tuple(dict.fromkeys(addresses)),
    )


def _as_ip_literal(host: str) -> str | None:
    """Return a normalized IP string when ``host`` is any IP literal form.

    Handles bare v4/v6 plus the integer and dotted-octal encodings that browsers
    accept and naive validators miss (``2130706433``, ``0177.0.0.1``,
    ``0x7f.0.0.1``).
    """
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass

    # Bracketed IPv6 has already been stripped by urlsplit.hostname, so only
    # numeric obfuscations remain.
    compact = host.replace("_", "")
    if compact.isdigit():
        try:
            return str(ipaddress.ip_address(int(compact)))
        except (ValueError, OverflowError):
            return None
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", compact):
        try:
            return str(ipaddress.ip_address(int(compact, 16)))
        except (ValueError, OverflowError):
            return None
    # Dotted forms with octal/hex octets, e.g. 0177.0.0.1 or 0x7f.0.0.1
    octets = compact.split(".")
    if 2 <= len(octets) <= 4 and all(
        re.fullmatch(r"0[xX][0-9a-fA-F]{1,2}|0[0-7]{1,3}|\d{1,3}", o) for o in octets
    ):
        try:
            values = [
                int(o, 16)
                if o.lower().startswith("0x")
                else int(o, 8)
                if o.startswith("0") and len(o) > 1
                else int(o)
                for o in octets
            ]
        except ValueError:
            return None
        if len(values) == 4 and all(0 <= v <= 255 for v in values):
            packed = (values[0] << 24) | (values[1] << 16) | (values[2] << 8) | values[3]
            try:
                return str(ipaddress.ip_address(packed))
            except ValueError:
                return None
    return None


def validate_redirect_chain(
    chain: list[str],
    *,
    resolver: Resolver = system_resolver,
    max_redirects: int = 5,
) -> UrlVerdict:
    """Validate every hop. A redirect to a private address fails the whole chain.

    Called by the browser worker on each response, and re-checked server-side on
    ingest so a compromised worker cannot smuggle a private-origin capture into
    the evidence store.
    """
    if len(chain) > max_redirects + 1:
        return UrlVerdict(
            False,
            chain[-1] if chain else "",
            BlockReason.TOO_MANY_REDIRECTS,
            f"{len(chain) - 1} redirects exceeds limit {max_redirects}",
        )
    last: UrlVerdict | None = None
    for hop in chain:
        last = validate_url(hop, resolver=resolver)
        if not last.allowed:
            return last
    return last or UrlVerdict(False, "", BlockReason.MISSING_HOST, "empty chain")


__all__ = [
    "ALLOWED_PORTS",
    "ALLOWED_SCHEMES",
    "BlockReason",
    "Resolver",
    "UrlVerdict",
    "is_public_address",
    "system_resolver",
    "validate_redirect_chain",
    "validate_url",
]
