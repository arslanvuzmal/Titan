"""Contact discovery rules and eligibility.

Invariant 6 lives here: **Titan never generates an email address.** It only
records addresses that were published, provided, or independently verified, and
it records *how* each one was obtained so the policy engine can refuse anything
weaker.

The distinction that matters:

* ``info@acme.example`` **found in a mailto: link on acme.example** is a
  first-party published address -- legitimate.
* ``info@acme.example`` **constructed because acme.example has MX records** is
  a guess -- never eligible, even though the two strings are identical.

Which is why provenance is stored per address rather than inferred from shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from titan.contracts.evidence import PageEvidence
from titan.db.enums import (
    ELIGIBLE_CONTACT_SOURCES,
    ContactSource,
    VerificationStatus,
    verification_permits_sending,
)
from titan.intelligence.mx import MxCheck

#: Local parts that indicate a shared/role mailbox rather than a person.
ROLE_LOCAL_PARTS: frozenset[str] = frozenset(
    {
        "info",
        "contact",
        "hello",
        "hi",
        "enquiries",
        "enquiry",
        "inquiries",
        "inquiry",
        "admin",
        "office",
        "reception",
        "team",
        "support",
        "help",
        "sales",
        "bookings",
        "booking",
        "appointments",
        "mail",
        "email",
        "general",
        "frontdesk",
        "desk",
        "clinic",
        "practice",
        "studio",
    }
)

#: Addresses that are never appropriate outreach targets regardless of source.
#:
#: Three kinds, all refused for the same reason -- writing to one is either
#: pointless or actively harmful:
#:
#: * **Infrastructure** (postmaster, abuse, mailer-daemon). RFC 2142 reserves
#:   these for operating the mail system, and several are monitored by
#:   blocklist operators. A cold pitch to ``abuse@`` is how a domain gets
#:   reported by the person whose job is reporting things.
#: * **Unattended** (noreply, bounce). Nobody reads them.
#: * **Opt-out** (unsubscribe, remove, optout, stop). This is the group that
#:   matters most and the one that was incomplete: an address named ``remove@``
#:   is a standing request not to be contacted, published in advance. Writing
#:   to it is not a cold email that happens to miss -- it is contacting somebody
#:   who already said no, in the one way that guarantees a complaint. Found in
#:   a live queue: ``remove@expressestateagency.co.uk`` was scheduled to receive
#:   two messages.
NEVER_CONTACT_LOCAL_PARTS: frozenset[str] = frozenset(
    {
        # Infrastructure
        "abuse",
        "postmaster",
        "hostmaster",
        "webmaster",
        "security",
        "root",
        # Unattended
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "bounce",
        "bounces",
        "mailer-daemon",
        "mailerdaemon",
        # Opt-out
        "unsubscribe",
        "unsub",
        "remove",
        "removeme",
        "optout",
        "opt-out",
        "stop",
        "delist",
        "donotcontact",
        "do-not-contact",
        # Functions that exist to receive complaints, not enquiries
        "privacy",
        "dpo",
        "legal",
        "compliance",
        "gdpr",
    }
)

#: Local parts a naive tool would construct from a person's name. Recording
#: them here does not make them usable -- it makes the *pattern* recognisable
#: so an imported address of this shape without provenance is flagged.
COMMON_GUESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(ceo|owner|founder|director|manager|principal|partner)$"),
    re.compile(r"^(firstname|lastname|fname|lname|first|last)$"),
    re.compile(r"^[a-z]$"),  # single initial
)

# Each label may contain hyphens ("mail.bellrose-dental.test"); the previous
# pattern only allowed them in the first label, which silently rejected
# legitimate subdomain addresses.
_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"

# The local part is a dot-atom (RFC 5322): atext runs joined by single dots,
# with no dot leading, trailing, or doubled. The previous pattern was
# ``[^@\s]{1,64}``, which accepted anything at all that was not whitespace or
# an at-sign, and that is how "%20csteam@ruhdental.com" reached a real send.
_ATEXT = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]"
_LOCAL = rf"{_ATEXT}+(?:\.{_ATEXT}+)*"
EMAIL_RE = re.compile(rf"^{_LOCAL}@{_LABEL}(?:\.{_LABEL})*\.[a-z]{{2,}}$", re.I)

#: A percent-encoding triplet surviving into an address means somebody handed us
#: a URI they never decoded -- ``mailto:%20info@x.com`` is a leading space, not a
#: mailbox named "%20info". Percent is legal atext, so the dot-atom rule above
#: cannot catch this; matching the encoding shape specifically is narrow enough
#: to leave a genuine (if eccentric) "a%b@x.com" alone.
PERCENT_ENCODED = re.compile(r"%[0-9a-f]{2}", re.I)

#: RFC 5321 section 4.5.3.1.1.
MAX_LOCAL_PART = 64


def is_valid_email(email: str) -> bool:
    """Whether an address is well-formed enough to be worth sending to.

    Deliberately stricter than "the string contains an @". Every address Titan
    holds arrives from a crawler reading somebody else's markup, so the shapes
    that matter here are the malformed ones a scrape produces rather than the
    exotic ones a standard permits.
    """
    normalized = normalize_email(email)
    if not EMAIL_RE.match(normalized):
        return False
    local = normalized.partition("@")[0]
    if len(local) > MAX_LOCAL_PART:
        return False
    return not PERCENT_ENCODED.search(local)


#: Addresses appearing on a site that clearly belong to someone else.
THIRD_PARTY_DOMAINS: frozenset[str] = frozenset(
    {
        "sentry.io",
        "wixpress.com",
        "squarespace.com",
        "shopify.com",
        "godaddy.com",
        "wordpress.com",
        "example.com",
        "domain.com",
        "yourcompany.com",
        "email.com",
    }
)


@dataclass(frozen=True, slots=True)
class DiscoveredContact:
    email: str
    normalized: str
    domain: str
    source: ContactSource
    source_url: str | None
    is_generic_role: bool
    confidence: float
    #: Set when the address was found but must not be used.
    rejection_reason: str | None = None

    @property
    def is_usable(self) -> bool:
        return self.rejection_reason is None


def normalize_email(raw: str) -> str:
    """Lowercase and strip. Deliberately does NOT strip plus-tags or dots.

    Gmail treats ``a.b+x@gmail.com`` as ``ab@gmail.com``, but most providers do
    not. Normalising aggressively would let one suppression entry silently fail
    to match the address actually being sent to, so Titan matches conservatively
    and suppresses the exact address plus its plus-tag base separately.
    """
    return raw.strip().lower()


def suppression_keys(email: str) -> tuple[str, ...]:
    """Every key an address should be checked against in the suppression list.

    Includes the plus-tag base so that unsubscribing as ``a+news@x.com`` also
    stops mail to ``a@x.com`` -- the same human either way.
    """
    normalized = normalize_email(email)
    keys = {normalized}
    local, _, domain = normalized.partition("@")
    if "+" in local:
        keys.add(f"{local.split('+', 1)[0]}@{domain}")
    return tuple(sorted(keys))


def email_domain(email: str) -> str:
    return normalize_email(email).partition("@")[2]


def is_role_address(email: str) -> bool:
    return normalize_email(email).partition("@")[0] in ROLE_LOCAL_PARTS


def looks_like_a_guess(email: str) -> bool:
    """Whether the local part matches a pattern a guesser would produce.

    Advisory only: a real ``ceo@`` address published on a site is legitimate,
    and provenance decides. This exists so an *imported* address with no
    provenance can be flagged rather than trusted.
    """
    local = normalize_email(email).partition("@")[0]
    return any(p.match(local) for p in COMMON_GUESS_PATTERNS)


def extract_contacts_from_pages(
    pages: list[PageEvidence], organization_domain: str | None
) -> list[DiscoveredContact]:
    """Collect addresses that the business published on its own website.

    Provenance is FIRST_PARTY_WEBSITE only when the address's domain matches
    the organization's; an address on a different domain found on their site is
    someone else's and is recorded as ineligible rather than silently used.
    """
    out: dict[str, DiscoveredContact] = {}
    # removeprefix, not lstrip: lstrip("www.") strips any leading w/./ characters,
    # so "wombat.test" would become "ombat.test" and never match itself.
    org_domain = (organization_domain or "").lower().removeprefix("www.")

    for page in pages:
        for raw in page.visible_emails:
            normalized = normalize_email(raw)
            if normalized in out:
                continue
            if not is_valid_email(normalized):
                continue

            domain = email_domain(normalized)
            local = normalized.partition("@")[0]
            rejection: str | None = None

            if local in NEVER_CONTACT_LOCAL_PARTS:
                rejection = (
                    f"{local}@ is a system mailbox and is never an outreach target"
                )
            elif domain in THIRD_PARTY_DOMAINS:
                rejection = f"{domain} is a third-party platform domain, not the business"
            elif org_domain and not _domains_related(domain, org_domain):
                rejection = (
                    f"{domain} does not belong to the organization ({org_domain}); "
                    "it is someone else's address that happens to appear on their site"
                )

            source = (
                ContactSource.FIRST_PARTY_WEBSITE
                if rejection is None
                else ContactSource.PATTERN_GUESS
            )
            generic = is_role_address(normalized)
            out[normalized] = DiscoveredContact(
                email=raw,
                normalized=normalized,
                domain=domain,
                source=source,
                source_url=page.final_url,
                is_generic_role=generic,
                # First-party published, but a role mailbox is slightly less
                # certain to reach a decision maker.
                confidence=0.0 if rejection else (0.75 if generic else 0.9),
                rejection_reason=rejection,
            )
    return sorted(out.values(), key=lambda c: (-c.confidence, c.normalized))


def _domains_related(candidate: str, organization: str) -> bool:
    """Whether an email domain plausibly belongs to the organization.

    Accepts exact matches and subdomains in either direction (mail.acme.com vs
    acme.com), which covers the common legitimate cases without accepting
    unrelated domains.
    """
    c = candidate.lower().removeprefix("www.")
    o = organization.lower().removeprefix("www.")
    return c == o or c.endswith(f".{o}") or o.endswith(f".{c}")


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    eligible: bool
    reasons: tuple[str, ...]


def check_contact_eligibility(
    *,
    source: ContactSource,
    verification: VerificationStatus,
    is_active: bool,
    allowed_sources: frozenset[ContactSource],
    require_verified: bool,
    email: str,
    mx: MxCheck | None = None,
) -> EligibilityResult:
    """Whether an address may receive outreach.

    Mirrors the contact checks in the policy engine so the UI can explain a
    blocked lead without running a full send evaluation. The policy engine
    remains authoritative at the send boundary.

    ``mx`` is optional and acts in one direction only. A domain that publishes
    no route for inbound mail disqualifies every address at it, because sending
    there produces a hard bounce that costs sender reputation. A *positive* MX
    result changes nothing -- see :func:`mx_presence_is_not_verification`.
    """
    reasons: list[str] = []
    normalized = normalize_email(email)

    if not is_valid_email(normalized):
        reasons.append("address is not a syntactically valid email")
    if normalized.partition("@")[0] in NEVER_CONTACT_LOCAL_PARTS:
        reasons.append("system mailbox; never an outreach target")
    if source is ContactSource.PATTERN_GUESS:
        reasons.append("address was pattern-guessed and is never eligible")
    elif source not in ELIGIBLE_CONTACT_SOURCES:
        reasons.append(f"source {source.value} is not an eligible provenance")
    elif source not in allowed_sources:
        reasons.append(f"source {source.value} is not permitted by this campaign")
    if not is_active:
        reasons.append("contact channel is inactive")
    if require_verified and not verification_permits_sending(verification, source):
        reasons.append(
            f"verification status {verification.value} does not meet the "
            "campaign's requirement"
            + (
                f" for an address sourced from {source.value}"
                if verification is VerificationStatus.CATCH_ALL
                else ""
            )
        )
    # One direction only. A failed lookup is deliberately not a disqualifier:
    # a resolver having a bad minute must not silently discard good leads.
    if mx is not None and mx.is_conclusively_undeliverable:
        reasons.append(
            f"domain {mx.domain} cannot receive mail ({mx.status.value}); "
            "sending would hard-bounce"
        )
    return EligibilityResult(eligible=not reasons, reasons=tuple(reasons))


def mx_presence_is_not_verification() -> str:
    """Documentation anchor for a rule that is easy to get wrong.

    A domain publishing MX records proves the *domain* accepts mail. It says
    nothing about whether a particular mailbox exists. Titan therefore records
    ``mx_present`` on ContactVerification for diagnostics but never lets it
    upgrade ``verification_status`` (mission section 11).
    """
    return (
        "MX records prove a domain accepts mail, not that a mailbox exists; "
        "mx_present never upgrades verification_status"
    )


__all__ = [
    "COMMON_GUESS_PATTERNS",
    "NEVER_CONTACT_LOCAL_PARTS",
    "ROLE_LOCAL_PARTS",
    "DiscoveredContact",
    "EligibilityResult",
    "check_contact_eligibility",
    "email_domain",
    "extract_contacts_from_pages",
    "is_role_address",
    "looks_like_a_guess",
    "normalize_email",
    "suppression_keys",
]
