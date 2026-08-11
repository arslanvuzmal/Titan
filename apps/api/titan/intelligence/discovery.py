"""Deciding who is worth discovering, and who is not worth the crawl.

:mod:`titan.providers.places` knows how to ask Google for businesses. This
decides *what to ask* and *which answers to keep* -- the two judgements that
determine whether the rest of the pipeline has anything truthful to work with.

**The admission rules are not quality filters, they are honesty filters.** Every
message Titan sends is built from evidence a crawler gathered on the recipient's
own website. A business with no website produces no evidence, therefore no
finding, therefore no claim -- and the only message that could be written to
them is a generic one, which is the thing the whole system exists not to send.
Admitting them would not produce a worse lead; it would produce a lead the
pipeline must later refuse, after paying for the crawl.

The same reasoning drives :data:`NON_AUDITABLE_HOSTS`. A Places record whose
"website" is a Facebook page is not a business with a website. Crawling it would
produce findings about Facebook's markup -- perfectly real observations about
somebody else's site -- and an email confidently describing problems the
recipient did not cause and cannot fix.

Pure, no I/O. The activity in :mod:`titan.activities.discovery` does the asking
and the writing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from titan.providers.places import DiscoveredBusiness, DiscoveryQuery

#: Hosts where a page belongs to a platform rather than to the business.
#:
#: Matched on the registrable host and any subdomain of it. Two kinds are
#: listed, for two different reasons:
#:
#: * Social and directory profiles -- crawling one audits the platform, not the
#:   business, and every finding would be about somebody else's markup.
#: * Dead site builders -- ``business.site`` was Google's own, retired in 2024,
#:   and the addresses survive in Places records long after the pages stopped
#:   resolving.
#:
#: Deliberately *not* listed: wixsite.com, squarespace.com, wordpress.com and
#: their kin. Those are real sites on shared infrastructure, they are the
#: recipient's to change, and their defects are genuinely the recipient's
#: problem. Excluding them would drop a large share of exactly the small
#: businesses this system is for.
NON_AUDITABLE_HOSTS: frozenset[str] = frozenset(
    {
        # Social profiles
        "facebook.com",
        "fb.com",
        "fb.me",
        "instagram.com",
        "twitter.com",
        "x.com",
        "linkedin.com",
        "tiktok.com",
        "youtube.com",
        "pinterest.com",
        "nextdoor.com",
        # Directories and review platforms
        "yelp.com",
        "yell.com",
        "thomsonlocal.com",
        "trustpilot.com",
        "tripadvisor.com",
        "tripadvisor.co.uk",
        "checkatrade.com",
        "ratedpeople.com",
        "bark.com",
        "thumbtack.com",
        "angi.com",
        "houzz.com",
        "opentable.com",
        "booksy.com",
        "treatwell.co.uk",
        "fresha.com",
        # Link aggregators and shorteners: no site to audit at all
        "linktr.ee",
        "bit.ly",
        "linkin.bio",
        # Retired site builders
        "business.site",
        "negocio.site",
    }
)

#: Below this, a rating is not evidence of an active business so much as
#: evidence of a quiet one. Not a quality judgement about the business -- it is
#: a proxy for whether the website matters enough to them that a message about
#: it will land as useful rather than as noise.
DEFAULT_MIN_REVIEWS = 8
DEFAULT_MIN_RATING = 3.5

#: Places caps a text search at 60 results across 3 pages. Asking for more is an
#: error from the adapter, not a larger result set.
MAX_RESULTS_PER_SEARCH = 60


class Refusal(StrEnum):
    """Why a discovered business was not admitted.

    Counted per reason on the ``lead_sources`` row, so a discovery run that
    returns forty businesses and admits three can say which rule ate the other
    thirty-seven. Without that the honest answer to "why is the pipeline empty"
    is a shrug.
    """

    NOT_OPERATIONAL = "not_operational"
    NO_WEBSITE = "no_website"
    NON_AUDITABLE_HOST = "non_auditable_host"
    ALREADY_KNOWN = "already_known"
    SUPPRESSED_DOMAIN = "suppressed_domain"
    TOO_FEW_REVIEWS = "too_few_reviews"
    RATING_BELOW_FLOOR = "rating_below_floor"


@dataclass(frozen=True, slots=True)
class Admission:
    """Whether one business may become a lead, and why not when it may not."""

    business: DiscoveredBusiness
    refusal: Refusal | None = None

    @property
    def admitted(self) -> bool:
        return self.refusal is None


def targeting_blockers(*, business_type: str | None, geography: str | None) -> list[str]:
    """Why this campaign cannot be discovered for.

    Mirrors ``_authorization_blockers`` in the planner: a list of sentences an
    operator can act on, rather than a bare False. A campaign with no targeting
    is not broken -- it has simply never been told who to look for -- and the
    difference matters in the notification.
    """
    blockers: list[str] = []
    if not (business_type or "").strip():
        blockers.append(
            "campaign has no target_business_type; there is nothing to search for"
        )
    if not (geography or "").strip():
        blockers.append(
            "campaign has no target_geography; an unbounded search would return "
            "businesses on other continents"
        )
    return blockers


def build_query(
    *,
    business_type: str,
    geography: str,
    country_code: str | None = None,
    max_results: int = 20,
    min_rating: float | None = DEFAULT_MIN_RATING,
    min_review_count: int | None = DEFAULT_MIN_REVIEWS,
) -> DiscoveryQuery:
    """Turn a campaign's targeting into one bounded search.

    ``require_website=True`` is set at the provider so businesses without one
    are dropped before they are billed for, rather than admitted and refused
    here. The check still exists in :func:`admit` because the provider's filter
    is advisory -- Places returns records with empty ``websiteUri`` regardless --
    and a rule the pipeline depends on should not live only in a remote API's
    query parameters.
    """
    return DiscoveryQuery(
        text_query=f"{business_type.strip()} in {geography.strip()}",
        included_region=(country_code or "").strip().upper() or None,
        min_rating=min_rating,
        min_review_count=min_review_count,
        require_website=True,
        max_results=max(1, min(max_results, MAX_RESULTS_PER_SEARCH)),
    )


def is_auditable_host(domain: str | None) -> bool:
    """Whether a crawl of this domain would describe the business itself."""
    if not domain:
        return False
    host = domain.strip().lower().removeprefix("www.")
    if not host:
        return False
    return not any(
        host == blocked or host.endswith(f".{blocked}") for blocked in NON_AUDITABLE_HOSTS
    )


def admit(
    business: DiscoveredBusiness,
    *,
    known_domains: frozenset[str] = frozenset(),
    known_place_ids: frozenset[str] = frozenset(),
    suppressed_domains: frozenset[str] = frozenset(),
    min_reviews: int = DEFAULT_MIN_REVIEWS,
    min_rating: float = DEFAULT_MIN_RATING,
) -> Admission:
    """Decide whether one discovered business becomes a lead.

    Ordered cheapest-and-most-certain first, so the refusal reason recorded is
    the most informative one available: a permanently closed business with no
    website is reported as closed, not as siteless.
    """
    if not business.is_operational:
        return Admission(business, Refusal.NOT_OPERATIONAL)

    domain = business.canonical_domain
    if not domain:
        return Admission(business, Refusal.NO_WEBSITE)
    if not is_auditable_host(domain):
        return Admission(business, Refusal.NON_AUDITABLE_HOST)

    if business.place_id in known_place_ids or domain in known_domains:
        return Admission(business, Refusal.ALREADY_KNOWN)

    # Checked here as well as at send time. Suppression is per address, and this
    # is a domain, so it cannot replace the send-time check -- but discovering a
    # business somebody at that domain has already opted out of, then paying to
    # crawl it before refusing at the last gate, is spend with a guaranteed
    # refusal at the end of it.
    if domain in suppressed_domains:
        return Admission(business, Refusal.SUPPRESSED_DOMAIN)

    if business.review_count is not None and business.review_count < min_reviews:
        return Admission(business, Refusal.TOO_FEW_REVIEWS)
    if business.rating is not None and business.rating < min_rating:
        return Admission(business, Refusal.RATING_BELOW_FLOOR)

    return Admission(business)


def admit_all(
    businesses: list[DiscoveredBusiness],
    *,
    known_domains: frozenset[str] = frozenset(),
    known_place_ids: frozenset[str] = frozenset(),
    suppressed_domains: frozenset[str] = frozenset(),
    min_reviews: int = DEFAULT_MIN_REVIEWS,
    min_rating: float = DEFAULT_MIN_RATING,
    limit: int | None = None,
) -> tuple[list[Admission], dict[str, int]]:
    """Admit a batch, deduplicating *within* it as well as against what is known.

    One Places search regularly returns the same business twice -- a chain with
    two entries pointing at one website, or a record duplicated across pages.
    Carrying the accumulated domains forward through the loop is what stops two
    leads being created for one company, which would produce two emails into the
    same inbox from the same campaign.

    Returns the admissions and a count per refusal reason, for the ledger.
    """
    seen_domains = set(known_domains)
    seen_place_ids = set(known_place_ids)
    admissions: list[Admission] = []
    refused: dict[str, int] = {}

    for business in businesses:
        if limit is not None and sum(a.admitted for a in admissions) >= limit:
            break

        decision = admit(
            business,
            known_domains=frozenset(seen_domains),
            known_place_ids=frozenset(seen_place_ids),
            suppressed_domains=suppressed_domains,
            min_reviews=min_reviews,
            min_rating=min_rating,
        )
        admissions.append(decision)

        if decision.refusal is not None:
            refused[decision.refusal.value] = refused.get(decision.refusal.value, 0) + 1
            continue

        seen_place_ids.add(business.place_id)
        if business.canonical_domain:
            seen_domains.add(business.canonical_domain)

    return admissions, refused


__all__ = [
    "DEFAULT_MIN_RATING",
    "DEFAULT_MIN_REVIEWS",
    "MAX_RESULTS_PER_SEARCH",
    "NON_AUDITABLE_HOSTS",
    "Admission",
    "Refusal",
    "admit",
    "admit_all",
    "build_query",
    "is_auditable_host",
    "targeting_blockers",
]
