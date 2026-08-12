"""Whether a sending domain is actually authenticated, checked against DNS.

``SenderIdentity`` carries four booleans -- ``domain_verified``, ``spf_ok``,
``dkim_ok``, ``dmarc_ok`` -- and its own docstring calls them the third of four
delivery gates. Nothing ever set them from evidence. They were assertions
somebody typed, and a gate that reads an assertion is not a gate.

Found in the live database on 2026-08-11: twenty identities on
``mail.arslanvuzmallone.dev`` with all four flags true and
``domain_verified=True``. That domain has no DNS at all -- no MX, no A record,
nothing. Every message from it would have failed SPF, failed DKIM, failed
DMARC, and arrived from a domain the receiver cannot even resolve. The gate
would have passed all twenty.

**One direction only**, the same rule :func:`titan.intelligence.mx.check_mx`
follows. A conclusive negative disqualifies. An inconclusive result never
upgrades a flag to true -- because the whole defect being fixed here is a flag
that said yes without having looked.

DKIM is the honest edge case. A DKIM key lives at
``<selector>._domainkey.<domain>`` and DNS offers no way to enumerate
selectors, so absence of a key at the selectors tried is *not* proof there is
no DKIM. That distinction is recorded rather than smoothed over: a
:class:`DomainAuth` reports ``dkim_ok=False`` alongside
``dkim_conclusive=False``, and a caller that treats those as the same thing is
making a claim this module refused to make.

Recording the uncertainty is not the same as ignoring it. ``sendable`` still
requires DKIM -- see the property for why an inconclusive check refuses rather
than waves through, and what to do when a real domain's selector is not on the
list below.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from titan.intelligence.mx import DomainDoesNotExist, MxResolver, system_mx_resolver

logger = logging.getLogger(__name__)

DNS_TIMEOUT_SECONDS = 5.0

#: Selectors worth trying before giving up on discovering DKIM. Ordered by how
#: often they appear in the wild for the providers this system sends through.
#: Not exhaustive, and deliberately not presented as such -- see the module
#: docstring on why a miss here is inconclusive rather than negative.
COMMON_DKIM_SELECTORS: tuple[str, ...] = (
    "default",
    "google",
    "selector1",
    "selector2",
    "k1",
    "k2",
    "s1",
    "s2",
    "mail",
    "dkim",
    "resend",
    "smartlead",
    "spacemail",
    "zoho",
    "mandrill",
    "sendgrid",
    "protonmail",
    "fm1",
)

#: How long a verification stays good. Chosen because DNS changes silently: a
#: registrar lapse, a nameserver migration or somebody tidying TXT records
#: costs nothing to do and breaks authentication invisibly. Two weeks bounds
#: how long Titan can keep sending on the strength of a check that has since
#: become false.
MAX_VERIFICATION_AGE = dt.timedelta(days=14)

_SPF_RE = re.compile(r"^\s*v=spf1\b", re.I)
_DMARC_RE = re.compile(r"^\s*v=DMARC1\b", re.I)
_DMARC_POLICY_RE = re.compile(r"\bp\s*=\s*(none|quarantine|reject)\b", re.I)
#: A bare "+all" (or "all" with no qualifier) authorises the entire internet to
#: send as this domain, which is worse than publishing nothing: it looks like
#: authentication and defeats it.
_SPF_PERMISSIVE_RE = re.compile(r"(?:^|\s)\+?all\s*$", re.I)
_DKIM_RE = re.compile(r"\bv=DKIM1\b|\bp=[A-Za-z0-9+/=]{16,}", re.I)


class TxtResolver(Protocol):
    """Injectable DNS TXT lookup, so tests need no network.

    Returns the TXT strings at ``name``, empty when there are none. Raises
    :class:`DomainDoesNotExist` only when the name's *domain* does not exist,
    never merely because a record is absent -- the two mean different things
    and conflating them is how "no DKIM found" becomes "domain is fake".
    """

    def __call__(self, name: str) -> list[str]: ...


def system_txt_resolver(name: str) -> list[str]:
    import dns.resolver  # type: ignore[import-untyped]

    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=DNS_TIMEOUT_SECONDS)
    except dns.resolver.NXDOMAIN:
        # For a _dmarc or _domainkey lookup this means the record is absent,
        # not that the parent domain is gone. The caller establishes existence
        # separately, via MX/A.
        return []
    except Exception as exc:
        logger.debug("TXT lookup failed for %s (%s)", name, type(exc).__name__)
        return []

    records: list[str] = []
    for answer in answers:
        # A TXT record is a sequence of strings that the receiver concatenates;
        # long DKIM keys are always split this way, so joining is required
        # rather than cosmetic.
        parts = getattr(answer, "strings", None)
        if parts:
            records.append(b"".join(parts).decode("utf-8", "replace"))
        else:
            records.append(str(answer).strip('"'))
    return records


@dataclass(frozen=True, slots=True)
class DomainAuth:
    """What DNS actually says about a sending domain."""

    domain: str
    #: False means the domain does not exist. Conclusive, and catastrophic:
    #: mail from it cannot authenticate by any mechanism.
    resolved: bool
    spf_ok: bool
    dmarc_ok: bool
    dkim_ok: bool
    #: Whether the DKIM answer means anything. False when no key was found,
    #: because absence at the selectors tried proves nothing.
    dkim_conclusive: bool
    spf_record: str | None = None
    dmarc_record: str | None = None
    dmarc_policy: str | None = None
    dkim_selector: str | None = None
    checked_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    notes: tuple[str, ...] = ()

    @property
    def sendable(self) -> bool:
        """Whether Titan may send from this domain at all.

        All four, DKIM included, and that is a deliberate reversal. The first
        draft of this property left DKIM out on the grounds that an
        undiscoverable selector would block a correctly-configured domain --
        true, but it weighs the wrong risk. Gmail's bulk-sender rules require
        DKIM, so the alternative to a false refusal is sending unauthenticated
        mail into a spam folder while believing it was delivered. Failing
        closed is also what every other gate in this system does.

        It also has to agree with ``SenderIdentity.authorization_errors()``,
        which has always required all four. Two gates disagreeing about what
        "authenticated" means is how a system ends up with a flag that passes
        one and not the other.

        When a domain really is configured and the selector is simply unknown
        here, the fix is to add it to :data:`COMMON_DKIM_SELECTORS` -- a
        one-line change with a name attached, rather than a silent exemption.
        """
        return self.resolved and self.spf_ok and self.dmarc_ok and self.dkim_ok

    def as_detail(self) -> dict[str, object]:
        """Evidence, for the verification record. Every claim traceable."""
        return {
            "domain": self.domain,
            "resolved": self.resolved,
            "spf_ok": self.spf_ok,
            "spf_record": self.spf_record,
            "dmarc_ok": self.dmarc_ok,
            "dmarc_record": self.dmarc_record,
            "dmarc_policy": self.dmarc_policy,
            "dkim_ok": self.dkim_ok,
            "dkim_conclusive": self.dkim_conclusive,
            "dkim_selector": self.dkim_selector,
            "checked_at": self.checked_at.isoformat(),
            "notes": list(self.notes),
        }


def check_domain_auth(
    domain: str,
    *,
    txt_resolver: TxtResolver = system_txt_resolver,
    mx_resolver: MxResolver = system_mx_resolver,
    dkim_selectors: tuple[str, ...] = COMMON_DKIM_SELECTORS,
) -> DomainAuth:
    """Resolve what a receiver would see when mail arrives from ``domain``."""
    normalized = (domain or "").strip().lower().rstrip(".")
    if not normalized or "." not in normalized:
        return DomainAuth(
            domain=normalized,
            resolved=False,
            spf_ok=False,
            dmarc_ok=False,
            dkim_ok=False,
            dkim_conclusive=False,
            notes=("not a domain",),
        )

    notes: list[str] = []

    # Existence first. Everything below is meaningless for a domain that is not
    # registered, and reporting "no SPF" for a nonexistent domain buries the
    # part that actually matters.
    try:
        _, has_address = mx_resolver(normalized)
        resolved = True
    except DomainDoesNotExist:
        return DomainAuth(
            domain=normalized,
            resolved=False,
            spf_ok=False,
            dmarc_ok=False,
            dkim_ok=False,
            dkim_conclusive=True,
            notes=(
                "domain does not exist in DNS; mail from it cannot authenticate "
                "by any mechanism",
            ),
        )
    except Exception as exc:
        # A resolver having a bad minute is not evidence about the domain.
        return DomainAuth(
            domain=normalized,
            resolved=False,
            spf_ok=False,
            dmarc_ok=False,
            dkim_ok=False,
            dkim_conclusive=False,
            notes=(f"resolver error: {type(exc).__name__}",),
        )

    if not has_address:
        notes.append("domain resolves but publishes no MX or address record")

    spf_record, spf_ok, spf_note = _check_spf(normalized, txt_resolver)
    if spf_note:
        notes.append(spf_note)

    dmarc_record, dmarc_policy, dmarc_ok, dmarc_note = _check_dmarc(
        normalized, txt_resolver
    )
    if dmarc_note:
        notes.append(dmarc_note)

    dkim_selector = _find_dkim(normalized, txt_resolver, dkim_selectors)
    if dkim_selector is None:
        notes.append(
            "no DKIM key found at the selectors tried; this is not proof that "
            "DKIM is absent, only that its selector is unknown here"
        )

    return DomainAuth(
        domain=normalized,
        resolved=resolved,
        spf_ok=spf_ok,
        dmarc_ok=dmarc_ok,
        dkim_ok=dkim_selector is not None,
        dkim_conclusive=dkim_selector is not None,
        spf_record=spf_record,
        dmarc_record=dmarc_record,
        dmarc_policy=dmarc_policy,
        dkim_selector=dkim_selector,
        notes=tuple(notes),
    )


def _check_spf(domain: str, resolver: TxtResolver) -> tuple[str | None, bool, str | None]:
    records = [r for r in resolver(domain) if _SPF_RE.match(r)]
    if not records:
        return None, False, "no SPF record published"
    if len(records) > 1:
        # RFC 7208: more than one SPF record is a permanent error, and
        # receivers treat it as no SPF at all rather than picking one.
        return records[0], False, f"{len(records)} SPF records published; RFC 7208 "
    record = records[0]
    if _SPF_PERMISSIVE_RE.search(record):
        return (
            record,
            False,
            "SPF ends in +all, which authorises any host to send as this domain",
        )
    return record, True, None


def _check_dmarc(
    domain: str, resolver: TxtResolver
) -> tuple[str | None, str | None, bool, str | None]:
    records = [r for r in resolver(f"_dmarc.{domain}") if _DMARC_RE.match(r)]
    if not records:
        return None, None, False, "no DMARC record published"
    record = records[0]
    found = _DMARC_POLICY_RE.search(record)
    if not found:
        return record, None, False, "DMARC record has no p= policy"
    policy = found.group(1).lower()
    note = (
        "DMARC policy is p=none, which reports but does not enforce"
        if policy == "none"
        else None
    )
    # p=none still satisfies Gmail's bulk-sender requirement, which asks for a
    # DMARC record rather than an enforcing one. Recorded, not refused.
    return record, policy, True, note


def _find_dkim(
    domain: str, resolver: TxtResolver, selectors: tuple[str, ...]
) -> str | None:
    for selector in selectors:
        records = resolver(f"{selector}._domainkey.{domain}")
        if any(_DKIM_RE.search(r) for r in records):
            return selector
    return None


def is_stale(
    last_verified_at: dt.datetime | None,
    *,
    now: dt.datetime | None = None,
    max_age: dt.timedelta = MAX_VERIFICATION_AGE,
) -> bool:
    """Whether a recorded verification is too old to keep sending on.

    ``None`` is stale, and that is the important case: it is what an identity
    whose flags were set by hand looks like. A boolean with no timestamp behind
    it is an assertion; this is what turns it back into a claim that expires.
    """
    if last_verified_at is None:
        return True
    moment = now or dt.datetime.now(dt.UTC)
    if last_verified_at.tzinfo is None:
        last_verified_at = last_verified_at.replace(tzinfo=dt.UTC)
    return (moment - last_verified_at) > max_age


__all__ = [
    "COMMON_DKIM_SELECTORS",
    "MAX_VERIFICATION_AGE",
    "DomainAuth",
    "TxtResolver",
    "check_domain_auth",
    "is_stale",
    "system_txt_resolver",
]
