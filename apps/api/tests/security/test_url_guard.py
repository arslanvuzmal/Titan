"""SSRF guard tests.

Hermetic: DNS is injected, so these run with no network and give the same answer
on every machine. The pre-0.2 suite made real DNS queries to google.com and then
asserted that a validator with five separate bypasses was correct (gap analysis
C-03) -- that is the failure mode this file exists to prevent.

Each "bypass" test below fails against the old implementation and passes against
the new one.
"""

from __future__ import annotations

import ipaddress

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from titan.security.url_guard import (
    BlockReason,
    is_public_address,
    validate_redirect_chain,
    validate_url,
)


def fixed_resolver(*addresses: str):
    """A resolver that always returns the given addresses."""

    def _resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return _resolve


def failing_resolver(host: str, port: int) -> list[str]:
    raise OSError("NXDOMAIN")


PUBLIC = fixed_resolver("93.184.216.34")


# --------------------------------------------------------------------------
# Baseline: legitimate targets are allowed
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://example.com/services",
        "http://example.com:80/path?q=1#frag",
        "https://sub.domain.example.com:443/a/b",
    ],
)
def test_allows_ordinary_public_urls(url: str) -> None:
    verdict = validate_url(url, resolver=PUBLIC)
    assert verdict.allowed, verdict.detail
    assert verdict.resolved_ips == ("93.184.216.34",)


def test_verdict_returns_pinned_addresses() -> None:
    """The caller must be able to connect to the exact address that was vetted.

    Returning the address is what closes the TOCTOU window: without it the HTTP
    client re-resolves the name and a rebinding attack wins.
    """
    verdict = validate_url(
        "https://example.com", resolver=fixed_resolver("93.184.216.34", "2606:2800::1")
    )
    assert verdict.allowed
    assert set(verdict.resolved_ips) == {"93.184.216.34", "2606:2800::1"}


# --------------------------------------------------------------------------
# Scheme / port / shape
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url,reason",
    [
        ("file:///etc/passwd", BlockReason.SCHEME_NOT_ALLOWED),
        ("gopher://example.com/", BlockReason.SCHEME_NOT_ALLOWED),
        ("ftp://example.com/x", BlockReason.SCHEME_NOT_ALLOWED),
        ("javascript:alert(1)", BlockReason.SCHEME_NOT_ALLOWED),
        ("data:text/html,<script>", BlockReason.SCHEME_NOT_ALLOWED),
        ("dict://example.com:11211/", BlockReason.SCHEME_NOT_ALLOWED),
        ("https://example.com:22/", BlockReason.PORT_NOT_ALLOWED),
        ("https://example.com:6379/", BlockReason.PORT_NOT_ALLOWED),
        ("https://example.com:5432/", BlockReason.PORT_NOT_ALLOWED),
        ("https://user:pw@example.com/", BlockReason.CREDENTIALS_IN_URL),
        ("https://", BlockReason.MISSING_HOST),
        ("", BlockReason.TOO_LONG),
    ],
)
def test_rejects_bad_shapes(url: str, reason: BlockReason) -> None:
    verdict = validate_url(url, resolver=PUBLIC)
    assert not verdict.allowed
    assert verdict.reason is reason


def test_rejects_overlong_url() -> None:
    verdict = validate_url("https://example.com/" + "a" * 4000, resolver=PUBLIC)
    assert not verdict.allowed
    assert verdict.reason is BlockReason.TOO_LONG


# --------------------------------------------------------------------------
# Direct private / reserved literals
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://127.0.0.1:80/",
        "https://10.0.0.5/metrics",
        "http://192.168.1.1/config",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://224.0.0.1/",
        "http://255.255.255.255/",
    ],
)
def test_rejects_private_literals(url: str) -> None:
    verdict = validate_url(url, resolver=PUBLIC)
    assert not verdict.allowed, f"{url} was allowed"
    # 169.254.169.254 is caught by the metadata-host list first, which is an
    # equally correct refusal.
    assert verdict.reason in {BlockReason.PRIVATE_ADDRESS, BlockReason.METADATA_HOST}


# --------------------------------------------------------------------------
# BYPASS #1: IPv6. The old validator used gethostbyname (IPv4-only).
# --------------------------------------------------------------------------
def test_ipv6_only_host_resolving_to_loopback_is_blocked() -> None:
    verdict = validate_url("https://evil-host.net", resolver=fixed_resolver("::1"))
    assert not verdict.allowed
    assert verdict.reason is BlockReason.PRIVATE_ADDRESS


def test_ipv4_mapped_ipv6_metadata_address_is_blocked() -> None:
    """``::ffff:169.254.169.254`` is the cloud metadata service in v6 clothing."""
    verdict = validate_url(
        "https://evil-host.net", resolver=fixed_resolver("::ffff:169.254.169.254")
    )
    assert not verdict.allowed
    assert verdict.reason is BlockReason.PRIVATE_ADDRESS


def test_ipv4_mapped_loopback_literal_is_blocked() -> None:
    verdict = validate_url("http://[::ffff:127.0.0.1]/", resolver=PUBLIC)
    assert not verdict.allowed


def test_sixtofour_embedded_private_address_is_blocked() -> None:
    """2002:xxxx:xxxx::/16 embeds an IPv4 address in the next 32 bits."""
    embedded = ipaddress.IPv6Address("2002:c0a8:0101::")  # 192.168.1.1
    assert embedded.sixtofour == ipaddress.IPv4Address("192.168.1.1")
    verdict = validate_url(
        "https://evil-host.net", resolver=fixed_resolver(str(embedded))
    )
    assert not verdict.allowed


# --------------------------------------------------------------------------
# BYPASS #2: only the first record was checked.
# --------------------------------------------------------------------------
def test_mixed_public_and_private_resolution_is_blocked() -> None:
    """A host answering with one public and one private record is an attack."""
    verdict = validate_url(
        "https://evil-host.net", resolver=fixed_resolver("93.184.216.34", "127.0.0.1")
    )
    assert not verdict.allowed
    assert verdict.reason is BlockReason.MIXED_RESOLUTION


def test_private_record_ordered_last_is_still_blocked() -> None:
    verdict = validate_url(
        "https://evil-host.net",
        resolver=fixed_resolver("93.184.216.34", "8.8.8.8", "10.1.2.3"),
    )
    assert not verdict.allowed


# --------------------------------------------------------------------------
# BYPASS #3: numeric-literal obfuscation
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "host",
    [
        "2130706433",  # decimal 127.0.0.1
        "0x7f000001",  # hex 127.0.0.1
        "0177.0.0.1",  # dotted octal
        "0x7f.0.0.1",  # dotted hex
        "3232235777",  # decimal 192.168.1.1
        "2852039166",  # decimal 169.254.169.254
    ],
)
def test_numeric_obfuscated_loopback_is_blocked(host: str) -> None:
    verdict = validate_url(f"http://{host}/", resolver=PUBLIC)
    assert not verdict.allowed, f"{host} was allowed"


# --------------------------------------------------------------------------
# BYPASS #4: internal names
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://localhost:80/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://instance-data/latest/",
        "http://printer.local/",
        "http://db.internal/",
        "http://wiki.corp/",
        "http://something.onion/",
    ],
)
def test_rejects_internal_hostnames(url: str) -> None:
    # Resolver deliberately returns a *public* address: the name alone must be
    # enough to refuse, because split-horizon DNS can make these look external.
    verdict = validate_url(url, resolver=PUBLIC)
    assert not verdict.allowed, f"{url} was allowed"
    assert verdict.reason in {
        BlockReason.BLOCKED_HOSTNAME,
        BlockReason.BLOCKED_SUFFIX,
        BlockReason.METADATA_HOST,
    }


def test_dns_failure_is_closed() -> None:
    verdict = validate_url("https://nonexistent-host.net", resolver=failing_resolver)
    assert not verdict.allowed
    assert verdict.reason is BlockReason.DNS_FAILURE


def test_empty_resolution_is_closed() -> None:
    verdict = validate_url("https://void-host.net", resolver=fixed_resolver())
    assert not verdict.allowed
    assert verdict.reason is BlockReason.NO_ADDRESSES


# --------------------------------------------------------------------------
# BYPASS #5: redirects
# --------------------------------------------------------------------------
def test_redirect_to_private_address_fails_the_chain() -> None:
    def resolver(host: str, port: int) -> list[str]:
        return ["127.0.0.1"] if host == "inner-host.net" else ["93.184.216.34"]

    verdict = validate_redirect_chain(
        ["https://outer-host.net/", "https://inner-host.net/admin"], resolver=resolver
    )
    assert not verdict.allowed
    assert verdict.reason is BlockReason.PRIVATE_ADDRESS


def test_redirect_to_metadata_service_fails_the_chain() -> None:
    verdict = validate_redirect_chain(
        ["https://outer-host.net/", "http://169.254.169.254/latest/meta-data/"],
        resolver=PUBLIC,
    )
    assert not verdict.allowed


def test_redirect_limit_enforced() -> None:
    chain = [f"https://hop{i}-host.net/" for i in range(10)]
    verdict = validate_redirect_chain(chain, resolver=PUBLIC, max_redirects=5)
    assert not verdict.allowed
    assert verdict.reason is BlockReason.TOO_MANY_REDIRECTS


def test_clean_redirect_chain_is_allowed() -> None:
    verdict = validate_redirect_chain(
        ["http://example.com/", "https://example.com/", "https://www.example.com/"],
        resolver=PUBLIC,
    )
    assert verdict.allowed


# --------------------------------------------------------------------------
# Property tests (mission section 21.4)
# --------------------------------------------------------------------------
@given(st.integers(min_value=0, max_value=2**32 - 1))
@settings(max_examples=300, deadline=None)
def test_no_private_ipv4_is_ever_public(value: int) -> None:
    """is_public_address must agree with the stdlib on every IPv4 address."""
    addr = ipaddress.IPv4Address(value)
    expected_private = (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or addr in ipaddress.ip_network("0.0.0.0/8")
        or addr in ipaddress.ip_network("100.64.0.0/10")
        or addr in ipaddress.ip_network("192.0.0.0/24")
    )
    assert is_public_address(str(addr)) is (not expected_private)


@given(st.text(max_size=40))
@settings(max_examples=300, deadline=None)
def test_never_raises_on_arbitrary_input(garbage: str) -> None:
    """Hostile input must produce a refusal, never an exception."""
    verdict = validate_url(garbage, resolver=PUBLIC)
    assert isinstance(verdict.allowed, bool)
    if not verdict.allowed:
        assert verdict.reason is not None


@given(st.text(alphabet="0123456789.", min_size=1, max_size=20))
@settings(max_examples=300, deadline=None)
def test_numeric_hosts_never_yield_a_private_target(host: str) -> None:
    """An all-numeric host is an IP literal, so DNS is bypassed entirely.

    The guarantee is therefore not "always refused" -- 20000000 is a valid
    public address -- but "if allowed, every pinned address is public".
    """
    verdict = validate_url(f"http://{host}/", resolver=fixed_resolver("127.0.0.1"))
    if verdict.allowed:
        assert verdict.resolved_ips
        assert all(is_public_address(ip) for ip in verdict.resolved_ips)


def test_is_public_address_rejects_garbage() -> None:
    for value in ("", "not-an-ip", "999.999.999.999", "::gg"):
        assert is_public_address(value) is False
