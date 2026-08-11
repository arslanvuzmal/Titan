"""Who gets discovered, and who is refused before anyone pays to crawl them.

The admission rules are honesty filters, not quality filters: a business the
pipeline cannot gather evidence about is one it can only write a generic
message to, which is the thing the whole system exists not to send.
"""

from __future__ import annotations

import pytest
from titan.intelligence.discovery import (
    DEFAULT_MIN_RATING,
    DEFAULT_MIN_REVIEWS,
    MAX_RESULTS_PER_SEARCH,
    Refusal,
    admit,
    admit_all,
    build_query,
    is_auditable_host,
    targeting_blockers,
)
from titan.providers.places import DiscoveredBusiness


def business(
    place_id: str = "places/abc",
    *,
    website: str | None = "https://harborline-legal.test/",
    status: str | None = "OPERATIONAL",
    rating: float | None = 4.6,
    reviews: int | None = 120,
    name: str = "Harborline Legal",
) -> DiscoveredBusiness:
    return DiscoveredBusiness(
        place_id=place_id,
        display_name=name,
        formatted_address="12 Fictional Row",
        website_uri=website,
        phone="+15550100",
        rating=rating,
        review_count=reviews,
        business_status=status,
        primary_type="lawyer",
        latitude=53.4,
        longitude=-2.2,
    )


# ==========================================================================
# Targeting
# ==========================================================================
def test_a_campaign_with_no_business_type_cannot_be_discovered_for() -> None:
    blockers = targeting_blockers(business_type=None, geography="Manchester")

    assert len(blockers) == 1
    assert "target_business_type" in blockers[0]


def test_a_campaign_with_no_geography_cannot_be_discovered_for() -> None:
    """An unbounded search returns businesses on other continents."""
    blockers = targeting_blockers(business_type="dentists", geography="   ")

    assert len(blockers) == 1
    assert "target_geography" in blockers[0]


def test_complete_targeting_has_no_blockers() -> None:
    assert targeting_blockers(business_type="dentists", geography="Manchester") == []


def test_the_query_joins_type_and_geography() -> None:
    query = build_query(business_type="dentists", geography="Manchester UK")

    assert query.text_query == "dentists in Manchester UK"


def test_the_region_is_upper_cased_and_optional() -> None:
    assert (
        build_query(business_type="d", geography="g", country_code="gb").included_region
        == "GB"
    )
    assert build_query(business_type="d", geography="g").included_region is None


def test_max_results_is_clamped_to_what_places_allows() -> None:
    """Asking for more is an error from the adapter, not a bigger result set."""
    query = build_query(business_type="d", geography="g", max_results=500)

    assert query.max_results == MAX_RESULTS_PER_SEARCH


def test_the_query_always_requires_a_website() -> None:
    assert build_query(business_type="d", geography="g").require_website is True


# ==========================================================================
# Auditable hosts
# ==========================================================================
@pytest.mark.parametrize(
    "domain",
    [
        "facebook.com",
        "www.facebook.com",
        "business.facebook.com",
        "instagram.com",
        "yell.com",
        "linktr.ee",
        "business.site",
        "tripadvisor.co.uk",
    ],
)
def test_a_platform_page_is_not_an_auditable_website(domain: str) -> None:
    """Crawling one produces findings about the platform, not the business."""
    assert not is_auditable_host(domain)


@pytest.mark.parametrize(
    "domain",
    [
        "harborline-legal.test",
        "harborline.wixsite.com",
        "shop.squarespace.com",
        "myfirm.wordpress.com",
    ],
)
def test_a_real_site_on_shared_infrastructure_is_auditable(domain: str) -> None:
    """Excluding site builders would drop most of the businesses this is for.

    Their defects are genuinely theirs, and theirs to fix.
    """
    assert is_auditable_host(domain)


def test_no_domain_is_not_auditable() -> None:
    assert not is_auditable_host(None)
    assert not is_auditable_host("")


# ==========================================================================
# Admission
# ==========================================================================
def test_a_good_business_is_admitted() -> None:
    assert admit(business()).admitted


def test_a_closed_business_is_refused() -> None:
    result = admit(business(status="CLOSED_PERMANENTLY"))

    assert result.refusal is Refusal.NOT_OPERATIONAL


def test_a_business_with_no_website_is_refused() -> None:
    """No site means no evidence, no finding, and no honest message."""
    result = admit(business(website=None))

    assert result.refusal is Refusal.NO_WEBSITE


def test_a_facebook_page_is_refused_as_a_website() -> None:
    result = admit(business(website="https://www.facebook.com/harborline"))

    assert result.refusal is Refusal.NON_AUDITABLE_HOST


def test_a_known_domain_is_refused() -> None:
    result = admit(business(), known_domains=frozenset({"harborline-legal.test"}))

    assert result.refusal is Refusal.ALREADY_KNOWN


def test_a_known_place_id_is_refused_even_at_a_new_domain() -> None:
    """A business that changed website is still the same business."""
    result = admit(
        business(website="https://newsite.test/"),
        known_place_ids=frozenset({"places/abc"}),
    )

    assert result.refusal is Refusal.ALREADY_KNOWN


def test_a_suppressed_domain_is_refused_before_the_crawl_is_paid_for() -> None:
    """Mission section 24: discovery must not re-add somebody who opted out."""
    result = admit(business(), suppressed_domains=frozenset({"harborline-legal.test"}))

    assert result.refusal is Refusal.SUPPRESSED_DOMAIN


def test_a_business_with_too_few_reviews_is_refused() -> None:
    result = admit(business(reviews=DEFAULT_MIN_REVIEWS - 1))

    assert result.refusal is Refusal.TOO_FEW_REVIEWS


def test_a_poorly_rated_business_is_refused() -> None:
    result = admit(business(rating=DEFAULT_MIN_RATING - 0.1))

    assert result.refusal is Refusal.RATING_BELOW_FLOOR


def test_an_unrated_business_is_not_refused_for_being_unrated() -> None:
    """None is 'not reported', not 'zero'. A new business is still a business."""
    assert admit(business(rating=None, reviews=None)).admitted


def test_the_most_informative_refusal_wins() -> None:
    """A closed business with no website is reported as closed."""
    result = admit(business(status="CLOSED_PERMANENTLY", website=None))

    assert result.refusal is Refusal.NOT_OPERATIONAL


# ==========================================================================
# Batches
# ==========================================================================
def test_a_duplicate_inside_one_search_is_admitted_only_once() -> None:
    """Two Places entries for one website must not become two emails."""
    admissions, refused = admit_all(
        [
            business(place_id="places/1"),
            business(place_id="places/2"),  # same website
        ]
    )

    assert sum(a.admitted for a in admissions) == 1
    assert refused[Refusal.ALREADY_KNOWN.value] == 1


def test_refusals_are_counted_by_reason() -> None:
    """A run that admits three of forty has to be able to say why."""
    _, refused = admit_all(
        [
            business(place_id="places/1"),
            business(place_id="places/2", website=None),
            business(place_id="places/3", website="https://facebook.com/x"),
            business(place_id="places/4", website="https://b.test/", status="CLOSED"),
        ]
    )

    assert refused == {
        Refusal.NO_WEBSITE.value: 1,
        Refusal.NON_AUDITABLE_HOST.value: 1,
        Refusal.NOT_OPERATIONAL.value: 1,
    }


def test_the_limit_bounds_admissions_not_the_batch() -> None:
    admissions, _ = admit_all(
        [
            business(place_id=f"places/{i}", website=f"https://firm{i}.test/")
            for i in range(10)
        ],
        limit=3,
    )

    assert sum(a.admitted for a in admissions) == 3


def test_an_empty_batch_is_not_an_error() -> None:
    admissions, refused = admit_all([])

    assert admissions == []
    assert refused == {}
