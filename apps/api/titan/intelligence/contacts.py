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
        # --- English front desks the original list missed -----------------
        # Read off the live workspace rather than imagined: each of these is
        # an address a real business published as its way in.
        "ask",
        "care",
        "concierge",
        "customercare",
        "customerservice",
        "emergency",
        "enquire",
        "feedback",
        "front",
        "frontoffice",
        "membership",
        "newpatients",
        "patients",
        "reservations",
        "reception2",
        "service",
        "smile",
        "smiles",
        "welcome",
        # --- German -------------------------------------------------------
        # kontakt@ and praxis@ were the two most common non-role local parts
        # on the live list, seven each, both scored as named individuals.
        "kontakt",
        "praxis",
        "buero",
        "zentrale",
        "empfang",
        "anfrage",
        "termine",
        "rezeption",
        "sekretariat",
        # --- Spanish / Portuguese ----------------------------------------
        "contacto",
        "informacion",
        "administracion",
        "oficina",
        "recepcion",
        "citas",
        "consulta",
        "consultas",
        "atendimento",
        "geral",
        "escritorio",
        # --- French -------------------------------------------------------
        "cabinet",
        "bureau",
        "accueil",
        "secretariat",
        "rendezvous",
        # --- Dutch --------------------------------------------------------
        "praktijk",
        "kantoor",
        "receptie",
        "afspraak",
        "informatie",
        "balie",
        # --- Polish -------------------------------------------------------
        "biuro",
        "gabinet",
        "recepcja",
        # --- Romanian -----------------------------------------------------
        # "receptie" is Dutch *and* Romanian; it is listed once, above.
        "birou",
        "programari",
        # --- Italian ------------------------------------------------------
        "segreteria",
        "ufficio",
        "informazioni",
        "accoglienza",
        "prenotazioni",
    }
)


#: How much Titan wants a given address, when a site published several.
#:
#: Lower sorts first. This exists because the resolver used to take the first
#: eligible candidate it saw, and iteration order is *page-crawl order* -- so a
#: practice publishing ``katie@`` on its team page and ``info@`` on its contact
#: page was written to at ``katie@``, decided by which page the crawler reached
#: first. Nothing was wrong with the address; it was simply the wrong one of
#: the two to choose.
#:
#: The ordering is measured, not assumed. On this workspace's own sending
#: history, role addresses hard-bounced at **1.30%** (2 of 154) and everything
#: else at **8.11%** (6 of 74). A front desk outlives whoever is standing at
#: it; a named mailbox leaves when the person does -- ``katie@reading-smiles``
#: bounced for exactly that reason.
PREFERRED = 0
ACCEPTABLE = 1
LAST_RESORT = 2


def contact_preference(contact: DiscoveredContact) -> tuple[int, str]:
    """Rank one candidate. Lower is better; ties break on the address itself.

    Deterministic on purpose: the same site must resolve to the same address on
    a re-run, or a retry silently writes to somebody different.
    """
    if contact.is_generic_role:
        band = PREFERRED
    elif looks_like_a_guess(contact.normalized):
        band = LAST_RESORT
    else:
        band = ACCEPTABLE
    return (band, contact.normalized)


def rank_contacts(contacts: list[DiscoveredContact]) -> list[DiscoveredContact]:
    """Best address first. Pure, so the caller can log what it chose and why."""
    return sorted(contacts, key=contact_preference)

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
        # Functions that exist to receive complaints, not enquiries.
        #
        # Spelled out in every market Titan actually sends to, because the
        # English-only version let four through on the live workspace --
        # including dataprotection@ at a law firm. A complaint from one of
        # these is not a bounce: domain_health returns BLOCKED on the first
        # one, with no sample-size threshold and no recovery window.
        "privacy",
        "dpo",
        "legal",
        "compliance",
        "gdpr",
        "dataprotection",
        "data-protection",
        "dataprivacy",
        "data-privacy",
        # German
        "datenschutz",
        "datenschutzbeauftragter",
        "recht",
        # Spanish / Portuguese
        "privacidad",
        "protecciondatos",
        "proteccion-datos",
        "privacidade",
        "juridico",
        # French
        "confidentialite",
        "rgpd",
        "juridique",
        # Dutch
        "gegevensbescherming",
        "privacyofficer",
        # Polish / Romanian
        "rodo",
        "protectiadatelor",
        "juridic",
        # Italian
        "privacyufficio",
        "legale",
        # Hiring. Not a deliverability judgement -- a "who is this" judgement.
        # These addresses exist for job applicants; they are read by whoever
        # handles hiring, are frequently pointed at an applicant-tracking
        # system that accepts nothing else, and are as frequently abandoned
        # between vacancies. A pitch about a broken booking page is the wrong
        # message to the wrong person however well it is written.
        #
        # Found by looking: recruitment@zenlaw.co.uk was one of five bounces on
        # the live workspace, published on the firm's own contact page and
        # syntactically perfect.
        "recruitment",
        "recruiting",
        "careers",
        "career",
        "jobs",
        "vacancies",
        "hr",
        "cv",
        "applications",
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


#: How a local part divides into words. Dots, hyphens, underscores and plus
#: signs are separators everywhere they appear in an address; nothing else is.
_LOCAL_PART_SEGMENTS = re.compile(r"[._+-]+")


def is_never_contact(email: str) -> bool:
    """Whether this address must not receive outreach, whoever published it.

    Three kinds, refused for the same reason -- writing to one is either
    pointless or actively harmful: mail infrastructure, unattended mailboxes,
    and addresses that are themselves a published request not to be contacted.
    Hiring addresses joined them because they are the wrong person by
    definition, not because they bounce.

    One definition, because it is checked in three places that are days apart:
    when an address is discovered, when a draft's contact is judged eligible,
    and once more at the moment of sending -- by which time the list may have
    been edited, and on this workspace it had been.

    **Segments, not the whole string.** This used to compare the entire local
    part, so ``careers.acmedental@`` was not ``careers`` and passed. Splitting
    on punctuation catches the qualified forms businesses actually publish --
    ``careers.acme``, ``hr-recruitment``, ``privacy_team`` -- while whole-segment
    matching keeps ``stopford`` from matching ``stop`` and ``legalise`` from
    matching ``legal``.
    """
    local = normalize_email(email).partition("@")[0]
    if local in NEVER_CONTACT_LOCAL_PARTS:
        return True
    return any(
        segment in NEVER_CONTACT_LOCAL_PARTS
        for segment in _LOCAL_PART_SEGMENTS.split(local)
        if segment
    )


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

            if is_never_contact(normalized):
                rejection = f"{local}@ is never an outreach target"
            elif domain in THIRD_PARTY_DOMAINS:
                rejection = f"{domain} is a third-party platform domain, not the business"
            elif (
                org_domain
                and domain in FREE_MAILBOX_DOMAINS
                and name_is_in_local_part(local, org_domain)
            ):
                # A small business running its site on one host and its mail on
                # Gmail is ordinary, and the local part is the evidence that
                # the address is theirs: snowymedispa@gmail.com on
                # snowymedispa.com.au. Narrow on purpose -- a webmail address
                # that does *not* carry the name stays refused below, and an
                # address at another business's domain is refused whatever its
                # local part says.
                rejection = None
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
            # Ambiguous rather than wrong: stored so the crawler does not
            # rediscover it every run, held below the sending bar because it
            # may be a phone number and a mailbox stuck together. The operator
            # can release it; nothing automatic will send to it.
            spliced = (
                rejection is None
                and DIGIT_RUN_PREFIX.match(normalized.split("@", 1)[0]) is not None
            )
            out[normalized] = DiscoveredContact(
                email=raw,
                normalized=normalized,
                domain=domain,
                source=source,
                source_url=page.final_url,
                is_generic_role=generic,
                # First-party published, but a role mailbox is slightly less
                # certain to reach a decision maker.
                confidence=(
                    0.0
                    if rejection
                    else (0.35 if spliced else (0.75 if generic else 0.9))
                ),
                rejection_reason=rejection,
            )
    return sorted(out.values(), key=lambda c: (-c.confidence, c.normalized))


#: A local part that is a run of digits immediately followed by letters.
#:
#: The shape a phone number makes when it runs into an address in one text
#: node: "Tel: 0161 234 0606info@207dentalcare.com" tokenises as
#: ``0606info@207dentalcare.com``, which is syntactically perfect and wrong.
#: No regex can split it -- the boundary between the number and the mailbox is
#: exactly where the whitespace is missing -- so it is not a parsing problem
#: and cannot be fixed by a better pattern.
#:
#: It is a *confidence* problem. Real addresses of this shape exist --
#: ``07handyman@`` is an ordinary trades mailbox -- so this cannot be a
#: rejection. It is a reason not to claim the address is published and
#: first-party when it might be two things stuck together.
#:
#: Three digits, not two: "01info@" is more likely a concatenation than a
#: mailbox, but "3dprint@" is a business. Requiring three keeps the common
#: legitimate shapes and catches the phone-number case, whose runs are longer.
DIGIT_RUN_PREFIX = re.compile(r"^[0-9]{3,}[a-z]", re.I)


#: Free mailbox providers a small business plausibly runs its own mail on.
#:
#: The distinction that matters: an address at one of these is *nobody's* by
#: virtue of its domain, so the domain says nothing about ownership either way
#: and the local part has to decide. An address at another *business's* domain
#: is somebody's -- theirs -- and no local part can override that.
FREE_MAILBOX_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "outlook.co.uk",
        "hotmail.com",
        "hotmail.co.uk",
        "hotmail.fr",
        "hotmail.de",
        "live.com",
        "live.co.uk",
        "msn.com",
        "yahoo.com",
        "yahoo.co.uk",
        "yahoo.fr",
        "yahoo.de",
        "ymail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "aol.co.uk",
        "gmx.de",
        "gmx.net",
        "gmx.com",
        "web.de",
        "t-online.de",
        "freenet.de",
        "orange.fr",
        "wanadoo.fr",
        "free.fr",
        "laposte.net",
        "libero.it",
        "virgilio.it",
        "alice.it",
        "wp.pl",
        "onet.pl",
        "interia.pl",
        "o2.pl",
        "seznam.cz",
        "btinternet.com",
        "sky.com",
        "virginmedia.com",
        "bigpond.com",
        "optusnet.com.au",
        "protonmail.com",
        "proton.me",
        "zoho.com",
        "yandex.ru",
        "mail.ru",
    }
)

#: How much of the business name has to appear before a webmail address counts
#: as theirs.
#:
#: Four characters. Below that the containment test stops being evidence: a
#: two-letter stem matches a large share of English local parts by accident,
#: and "the address contains the letters 'as'" is not a reason to write to a
#: stranger.
MIN_NAME_STEM = 4


def _alphanumeric(value: str) -> str:
    """Letters and digits only, lowercased. Punctuation carries no identity:
    ``snowy-medispa``, ``snowy.medispa`` and ``snowymedispa`` are one name."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def name_is_in_local_part(local_part: str, organization_domain: str) -> bool:
    """Whether a local part carries the organisation's own name.

    The test that lets ``snowymedispa@gmail.com`` through for
    snowymedispa.com.au while keeping ``fhdental.info@gmail.com`` out for
    fhfd.ca. Containment in either direction, because a business may shorten
    its name in an address (``elementdental@`` for elementdentalclinics.co.uk)
    or lengthen it (``enquiry.beightondentalcare@`` for beightondentalcare).
    """
    stem = _alphanumeric(organization_domain.split(".", 1)[0])
    if len(stem) < MIN_NAME_STEM:
        return False
    local = _alphanumeric(local_part)
    if len(local) < MIN_NAME_STEM:
        return False
    return stem in local or local in stem


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
    if is_never_contact(normalized):
        reasons.append(f"{normalized.partition('@')[0]}@ is never an outreach target")
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
    "ACCEPTABLE",
    "COMMON_GUESS_PATTERNS",
    "FREE_MAILBOX_DOMAINS",
    "LAST_RESORT",
    "MIN_NAME_STEM",
    "NEVER_CONTACT_LOCAL_PARTS",
    "PREFERRED",
    "ROLE_LOCAL_PARTS",
    "DiscoveredContact",
    "EligibilityResult",
    "check_contact_eligibility",
    "contact_preference",
    "email_domain",
    "extract_contacts_from_pages",
    "is_never_contact",
    "is_role_address",
    "looks_like_a_guess",
    "name_is_in_local_part",
    "normalize_email",
    "rank_contacts",
    "suppression_keys",
]
