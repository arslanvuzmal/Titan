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
    SENDABLE_VERIFICATION_STATUSES,
    ContactSource,
    VerificationStatus,
)

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
NEVER_CONTACT_LOCAL_PARTS: frozenset[str] = frozenset(
    {
        "abuse",
        "postmaster",
        "hostmaster",
        "webmaster",
        "security",
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "bounce",
        "bounces",
        "mailer-daemon",
        "unsubscribe",
        "privacy",
        "dpo",
        "legal",
        "compliance",
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
EMAIL_RE = re.compile(rf"^[^@\s]{{1,64}}@{_LABEL}(?:\.{_LABEL})*\.[a-z]{{2,}}$", re.I)

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
            if not EMAIL_RE.match(normalized):
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
) -> EligibilityResult:
    """Whether an address may receive outreach.

    Mirrors the contact checks in the policy engine so the UI can explain a
    blocked lead without running a full send evaluation. The policy engine
    remains authoritative at the send boundary.
    """
    reasons: list[str] = []
    normalized = normalize_email(email)

    if not EMAIL_RE.match(normalized):
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
    if require_verified and verification not in SENDABLE_VERIFICATION_STATUSES:
        reasons.append(
            f"verification status {verification.value} does not meet the "
            "campaign's requirement"
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
