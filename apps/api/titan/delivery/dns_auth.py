"""Real DNS verification of SPF, DKIM and DMARC.

The pre-0.2 sender identity had ``spf_ok`` / ``dkim_ok`` / ``dmarc_ok`` booleans
that a human ticked. A checkbox is not verification: the operator who ticks it
is usually the one who misconfigured the record.

This module resolves the actual DNS records and checks them, including the part
most guides omit -- **alignment**. A domain can publish a perfectly valid SPF
record and still fail DMARC, because SPF authenticates the Return-Path
(envelope sender), not the visible From:. If those two domains do not align, and
DKIM does not cover the From: domain either, DMARC fails and mail lands in spam
regardless of how green the individual checks look.

What this cannot do: predict inbox placement. Authentication is necessary, not
sufficient. It removes a *reason* to be filtered; reputation decides the rest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class AuthResult(StrEnum):
    PASS = "pass"  # noqa: S105 - an authentication outcome, not a credential
    FAIL = "fail"
    MISSING = "missing"
    MISCONFIGURED = "misconfigured"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RecordCheck:
    name: str
    result: AuthResult
    detail: str
    raw: str | None = None
    #: Problems that will not block delivery today but will degrade it.
    warnings: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return self.result is AuthResult.PASS


@dataclass(frozen=True, slots=True)
class DomainAuthReport:
    domain: str
    from_domain: str
    spf: RecordCheck
    dkim: RecordCheck
    dmarc: RecordCheck
    alignment: RecordCheck

    @property
    def ok(self) -> bool:
        return all((self.spf.ok, self.dkim.ok, self.dmarc.ok, self.alignment.ok))

    @property
    def blocking_errors(self) -> list[str]:
        return [
            f"{check.name}: {check.detail}"
            for check in (self.spf, self.dkim, self.dmarc, self.alignment)
            if not check.ok
        ]

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        for check in (self.spf, self.dkim, self.dmarc, self.alignment):
            out.extend(f"{check.name}: {w}" for w in check.warnings)
        return out


class TxtResolver(Protocol):
    """Injectable TXT lookup so tests need no network."""

    def __call__(self, name: str) -> list[str]: ...


def system_txt_resolver(name: str) -> list[str]:
    """Resolve TXT records. Returns [] when the name does not exist."""
    try:
        import dns.resolver  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "dnspython is required for DNS verification: pip install dnspython"
        ) from None

    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=8.0)
    except Exception:
        return []
    out: list[str] = []
    for record in answers:
        # A TXT record longer than 255 bytes arrives as multiple strings that
        # must be concatenated with no separator; splitting them is a classic
        # source of "my DKIM key looks right but fails".
        parts = getattr(record, "strings", None)
        if parts:
            out.append(b"".join(parts).decode("utf-8", "replace"))
        else:
            out.append(str(record).strip('"'))
    return out


# --------------------------------------------------------------------------
# SPF
# --------------------------------------------------------------------------
def check_spf(domain: str, *, resolver: TxtResolver = system_txt_resolver) -> RecordCheck:
    records = [r for r in resolver(domain) if r.lower().startswith("v=spf1")]

    if not records:
        return RecordCheck(
            "SPF",
            AuthResult.MISSING,
            f"no SPF record on {domain}. Publish a TXT record starting 'v=spf1'.",
        )
    if len(records) > 1:
        # Two SPF records is a permerror, and permerror is treated as fail by
        # most receivers -- worse than having none.
        return RecordCheck(
            "SPF",
            AuthResult.MISCONFIGURED,
            f"{len(records)} SPF records on {domain}; exactly one is permitted. "
            "Multiple records are a permanent error and fail authentication.",
            raw="; ".join(records),
        )

    record = records[0]
    warnings: list[str] = []

    if re.search(r"\s\+all\b", record):
        return RecordCheck(
            "SPF",
            AuthResult.MISCONFIGURED,
            "'+all' authorises the entire internet to send as this domain. "
            "Use '~all' or '-all'.",
            raw=record,
        )
    if not re.search(r"\s[-~?]all\b", record):
        warnings.append("no 'all' mechanism; receivers apply a neutral default")
    if re.search(r"\s\?all\b", record):
        warnings.append("'?all' is neutral and provides no protection; prefer '~all'")

    # Each of these costs a DNS lookup; the RFC 7208 limit is 10 and exceeding
    # it is a permerror.
    lookups = len(re.findall(r"\b(?:include|a|mx|ptr|exists|redirect)[:=]", record))
    if lookups > 10:
        return RecordCheck(
            "SPF",
            AuthResult.MISCONFIGURED,
            f"{lookups} DNS-lookup mechanisms exceeds the RFC 7208 limit of 10, "
            "which is a permanent error. Flatten some includes.",
            raw=record,
        )
    if lookups > 8:
        warnings.append(f"{lookups} of the 10 permitted DNS lookups used")

    return RecordCheck(
        "SPF", AuthResult.PASS, "valid", raw=record, warnings=tuple(warnings)
    )


# --------------------------------------------------------------------------
# DKIM
# --------------------------------------------------------------------------
def check_dkim(
    domain: str,
    selectors: tuple[str, ...] = ("resend", "titan", "default", "google", "k1", "s1"),
    *,
    resolver: TxtResolver = system_txt_resolver,
) -> RecordCheck:
    """Look for a DKIM public key under any of the usual selectors."""
    found: list[tuple[str, str]] = []
    for selector in selectors:
        for record in resolver(f"{selector}._domainkey.{domain}"):
            if "p=" in record:
                found.append((selector, record))

    if not found:
        return RecordCheck(
            "DKIM",
            AuthResult.MISSING,
            f"no DKIM key found on {domain} under selectors "
            f"{', '.join(selectors)}. Publish the CNAME/TXT records your provider "
            "issued.",
        )

    selector, record = found[0]
    warnings: list[str] = []

    key = re.search(r"\bp=([A-Za-z0-9+/=]*)", record)
    if key is None or not key.group(1):
        # p= present but empty is the documented way to REVOKE a key.
        return RecordCheck(
            "DKIM",
            AuthResult.MISCONFIGURED,
            f"selector {selector} publishes an empty p= value, which revokes the "
            "key. Signatures will fail.",
            raw=record,
        )

    # A 1024-bit RSA key is roughly 216 base64 characters; 2048-bit is ~392.
    if len(key.group(1)) < 300:
        warnings.append(
            "key appears to be 1024-bit; 2048-bit is the current recommendation "
            "and some receivers now discount shorter keys"
        )
    if "t=y" in record:
        warnings.append(
            "'t=y' marks this domain as in DKIM test mode, so receivers ignore "
            "signature failures -- and give the domain no credit either"
        )

    return RecordCheck(
        "DKIM",
        AuthResult.PASS,
        f"key present under selector '{selector}'",
        raw=record,
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------
# DMARC
# --------------------------------------------------------------------------
def check_dmarc(
    domain: str, *, resolver: TxtResolver = system_txt_resolver
) -> RecordCheck:
    records = [
        r for r in resolver(f"_dmarc.{domain}") if r.lower().startswith("v=dmarc1")
    ]

    if not records:
        # Gmail and Yahoo require a DMARC record for bulk senders. Without one,
        # bulk mail is rejected or filtered regardless of SPF and DKIM.
        return RecordCheck(
            "DMARC",
            AuthResult.MISSING,
            f"no DMARC record on _dmarc.{domain}. Gmail and Yahoo require one "
            "from bulk senders. Start with 'v=DMARC1; p=none; rua=mailto:...'.",
        )
    if len(records) > 1:
        return RecordCheck(
            "DMARC",
            AuthResult.MISCONFIGURED,
            f"{len(records)} DMARC records; exactly one is permitted.",
            raw="; ".join(records),
        )

    record = records[0]
    warnings: list[str] = []

    policy = re.search(r"\bp=(none|quarantine|reject)\b", record, re.I)
    if policy is None:
        return RecordCheck(
            "DMARC",
            AuthResult.MISCONFIGURED,
            "record has no p= policy tag, which makes it invalid.",
            raw=record,
        )
    if policy.group(1).lower() == "none":
        warnings.append(
            "p=none monitors but does not protect. Move to quarantine, then "
            "reject, once reports show your legitimate mail passing."
        )
    if "rua=" not in record.lower():
        warnings.append(
            "no rua= aggregate-report address, so you will not see "
            "authentication failures"
        )

    pct = re.search(r"\bpct=(\d+)", record)
    if pct and int(pct.group(1)) < 100:
        warnings.append(
            f"pct={pct.group(1)} applies the policy to only part of your mail"
        )

    return RecordCheck(
        "DMARC",
        AuthResult.PASS,
        f"policy p={policy.group(1).lower()}",
        raw=record,
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------
# Alignment -- the check most setups miss
# --------------------------------------------------------------------------
def check_alignment(
    *,
    from_domain: str,
    sending_domain: str,
    dmarc_record: str | None,
) -> RecordCheck:
    """Whether the visible From: domain aligns with the authenticated domain.

    DMARC only passes if SPF or DKIM authenticates a domain that *aligns* with
    the From: header. Sending as ``arslan@arslanvuzmallone.dev`` through a
    subdomain that signs as ``mail.arslanvuzmallone.dev`` aligns in relaxed mode
    (shared organizational domain) but fails in strict mode. Getting this wrong
    is the single most common cause of "everything looks configured but mail
    still goes to spam".
    """
    from_domain = from_domain.lower().strip()
    sending_domain = sending_domain.lower().strip()

    strict = "s"
    if dmarc_record:
        adkim = re.search(r"\badkim=([rs])", dmarc_record, re.I)
        aspf = re.search(r"\baspf=([rs])", dmarc_record, re.I)
        # Relaxed is the DMARC default when the tag is absent.
        strict = "s" if (adkim and adkim.group(1).lower() == "s") else "r"
        if aspf and aspf.group(1).lower() == "s":
            strict = "s"

    if from_domain == sending_domain:
        return RecordCheck("Alignment", AuthResult.PASS, "From: and signing domain match")

    if strict == "s":
        return RecordCheck(
            "Alignment",
            AuthResult.FAIL,
            f"DMARC is in strict alignment mode but From: is {from_domain} while "
            f"the signing domain is {sending_domain}. Either send From: "
            f"@{sending_domain}, or relax alignment (adkim=r; aspf=r).",
        )

    if _organizational_domain(from_domain) == _organizational_domain(sending_domain):
        return RecordCheck(
            "Alignment",
            AuthResult.PASS,
            f"relaxed alignment: {from_domain} and {sending_domain} share the "
            f"organizational domain {_organizational_domain(from_domain)}",
        )

    return RecordCheck(
        "Alignment",
        AuthResult.FAIL,
        f"From: {from_domain} does not align with signing domain "
        f"{sending_domain}. DMARC will fail and mail will be filtered.",
    )


def _organizational_domain(domain: str) -> str:
    """Best-effort registrable domain.

    Uses tldextract when available so multi-part suffixes (.co.uk, .com.au) are
    handled correctly; a naive last-two-labels rule gets those wrong.
    """
    try:
        import tldextract

        extracted = tldextract.extract(domain)
        if extracted.domain and extracted.suffix:
            return f"{extracted.domain}.{extracted.suffix}"
    except Exception:  # noqa: S110 - tldextract is optional; fall back below
        pass
    parts = domain.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def verify_sender_domain(
    *,
    from_email: str,
    sending_domain: str,
    dkim_selectors: tuple[str, ...] = ("resend", "titan", "default"),
    resolver: TxtResolver = system_txt_resolver,
) -> DomainAuthReport:
    """Run every authentication check for one sender identity."""
    from_domain = from_email.rpartition("@")[2].lower()
    sending_domain = sending_domain.lower()

    spf = check_spf(sending_domain, resolver=resolver)
    dkim = check_dkim(sending_domain, dkim_selectors, resolver=resolver)
    dmarc = check_dmarc(_organizational_domain(from_domain), resolver=resolver)
    alignment = check_alignment(
        from_domain=from_domain,
        sending_domain=sending_domain,
        dmarc_record=dmarc.raw,
    )

    return DomainAuthReport(
        domain=sending_domain,
        from_domain=from_domain,
        spf=spf,
        dkim=dkim,
        dmarc=dmarc,
        alignment=alignment,
    )


__all__ = [
    "AuthResult",
    "DomainAuthReport",
    "RecordCheck",
    "TxtResolver",
    "check_alignment",
    "check_dkim",
    "check_dmarc",
    "check_spf",
    "system_txt_resolver",
    "verify_sender_domain",
]
