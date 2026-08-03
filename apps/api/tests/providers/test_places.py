"""Google Places adapter tests.

Hermetic: every HTTP call is intercepted, so these run with no API key and no
network. They verify the request shape Titan sends (field masks are the cost
control), the filtering, the deduplication, and the error taxonomy.

They do NOT prove the adapter works against the live service -- see
docs/audits/FINAL-PRODUCTION-VERIFICATION.md.
"""

from __future__ import annotations

import httpx
import pytest
from titan.providers.places import (
    DETAIL_FIELD_MASK,
    SEARCH_FIELD_MASK,
    DiscoveryQuery,
    GooglePlacesProvider,
    PlacesError,
)


def place(
    place_id: str,
    name: str = "Fixture Business",
    *,
    website: str | None = "https://fixture.test",
    rating: float | None = 4.6,
    reviews: int | None = 40,
    status: str = "OPERATIONAL",
) -> dict:
    return {
        "id": place_id,
        "displayName": {"text": name, "languageCode": "en"},
        "formattedAddress": "1 Fictional Road, Testville",
        "websiteUri": website,
        "nationalPhoneNumber": "+15550100",
        "rating": rating,
        "userRatingCount": reviews,
        "businessStatus": status,
        "primaryType": "dentist",
        "location": {"latitude": 51.5, "longitude": -0.1},
    }


def provider_with(handler) -> GooglePlacesProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport, base_url="https://places.googleapis.com/v1"
    )
    return GooglePlacesProvider("test-key", client=client)


def single_page(places: list[dict]):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode() if request.content else ""
        return httpx.Response(200, json={"places": places})

    return handler, captured


# ==========================================================================
# Request shape
# ==========================================================================
@pytest.mark.asyncio
async def test_field_mask_and_key_are_sent_as_headers() -> None:
    """v1 requires an explicit field mask; it is also the billing control."""
    handler, captured = single_page([place("p1")])
    provider = provider_with(handler)

    await provider.search(DiscoveryQuery(text_query="dentists in Testville"))

    assert captured["headers"]["x-goog-api-key"] == "test-key"
    assert captured["headers"]["x-goog-fieldmask"] == SEARCH_FIELD_MASK
    # The mask must stay minimal; every added field can change the SKU.
    assert "places.reviews" not in SEARCH_FIELD_MASK
    assert "places.photos" not in SEARCH_FIELD_MASK


@pytest.mark.asyncio
async def test_region_and_radius_become_a_restriction_not_a_bias() -> None:
    """A bias returns far-away results and wastes budget silently."""
    handler, captured = single_page([place("p1")])
    provider = provider_with(handler)

    await provider.search(
        DiscoveryQuery(
            text_query="gyms",
            included_region="gb",
            latitude=51.5,
            longitude=-0.1,
            radius_meters=5000,
        )
    )
    body = captured["body"]
    assert '"regionCode": "GB"' in body or '"regionCode":"GB"' in body
    assert "locationRestriction" in body
    assert "locationBias" not in body


@pytest.mark.asyncio
async def test_radius_is_capped_at_the_api_maximum() -> None:
    handler, captured = single_page([place("p1")])
    provider = provider_with(handler)
    await provider.search(
        DiscoveryQuery(text_query="x", latitude=1.0, longitude=1.0, radius_meters=999_999)
    )
    assert "50000" in captured["body"]


@pytest.mark.asyncio
async def test_details_use_the_richer_mask() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-fieldmask"] == DETAIL_FIELD_MASK
        assert request.url.path.endswith("/places/p9")
        return httpx.Response(200, json=place("p9"))

    provider = provider_with(handler)
    business = await provider.get_details("p9")
    assert business is not None
    assert business.place_id == "p9"


@pytest.mark.asyncio
async def test_empty_place_id_is_rejected_before_a_request() -> None:
    provider = provider_with(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="place_id"):
        await provider.get_details("   ")


# ==========================================================================
# Query validation
# ==========================================================================
def test_empty_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="text_query"):
        DiscoveryQuery(text_query="   ")


@pytest.mark.parametrize("value", [0, -1, 61, 1000])
def test_out_of_range_max_results_is_rejected(value: int) -> None:
    with pytest.raises(ValueError, match="max_results"):
        DiscoveryQuery(text_query="x", max_results=value)


# ==========================================================================
# Filtering
# ==========================================================================
@pytest.mark.asyncio
async def test_closed_businesses_are_filtered_out() -> None:
    handler, _ = single_page([place("p1"), place("p2", status="CLOSED_PERMANENTLY")])
    result = await provider_with(handler).search(DiscoveryQuery(text_query="x"))

    assert [b.place_id for b in result.businesses] == ["p1"]
    assert result.filtered_out["not_operational"] == 1
    assert result.returned_before_filtering == 2


@pytest.mark.asyncio
async def test_websiteless_businesses_are_filtered_when_required() -> None:
    handler, _ = single_page([place("p1"), place("p2", website=None)])
    result = await provider_with(handler).search(
        DiscoveryQuery(text_query="x", require_website=True)
    )
    assert [b.place_id for b in result.businesses] == ["p1"]
    assert result.filtered_out["no_website"] == 1


@pytest.mark.asyncio
async def test_websiteless_businesses_are_kept_when_not_required() -> None:
    handler, _ = single_page([place("p1"), place("p2", website=None)])
    result = await provider_with(handler).search(
        DiscoveryQuery(text_query="x", require_website=False)
    )
    assert len(result.businesses) == 2


@pytest.mark.asyncio
async def test_review_and_rating_thresholds_are_applied() -> None:
    handler, _ = single_page(
        [
            place("keep", rating=4.8, reviews=100),
            place("few_reviews", rating=4.9, reviews=2),
            place("low_rating", rating=2.1, reviews=200),
            place("no_rating", rating=None, reviews=None),
        ]
    )
    result = await provider_with(handler).search(
        DiscoveryQuery(text_query="x", min_rating=4.0, min_review_count=10)
    )
    assert [b.place_id for b in result.businesses] == ["keep"]
    assert result.filtered_out["below_min_review_count"] >= 1


@pytest.mark.asyncio
async def test_duplicate_place_ids_are_collapsed() -> None:
    handler, _ = single_page([place("dup"), place("dup"), place("other")])
    result = await provider_with(handler).search(DiscoveryQuery(text_query="x"))
    assert sorted(b.place_id for b in result.businesses) == ["dup", "other"]
    assert result.filtered_out["duplicate_place_id"] == 1


@pytest.mark.asyncio
async def test_max_results_is_respected() -> None:
    handler, _ = single_page([place(f"p{i}") for i in range(20)])
    result = await provider_with(handler).search(
        DiscoveryQuery(text_query="x", max_results=5)
    )
    assert len(result.businesses) == 5


@pytest.mark.asyncio
async def test_unparseable_entries_are_counted_not_crashed_on() -> None:
    handler, _ = single_page([{"displayName": {"text": "no id"}}, place("good")])
    result = await provider_with(handler).search(DiscoveryQuery(text_query="x"))
    assert [b.place_id for b in result.businesses] == ["good"]
    assert result.filtered_out["unparseable"] == 1


# ==========================================================================
# Parsing and provenance
# ==========================================================================
@pytest.mark.asyncio
async def test_canonical_domain_is_derived_for_deduplication() -> None:
    handler, _ = single_page(
        [place("p1", website="https://www.Acme-Dental.test/contact?utm=x")]
    )
    result = await provider_with(handler).search(DiscoveryQuery(text_query="x"))
    assert result.businesses[0].canonical_domain == "acme-dental.test"


@pytest.mark.asyncio
async def test_canonical_domain_is_none_rather_than_guessed() -> None:
    """A wrong domain would merge two unrelated businesses."""
    handler, _ = single_page([place("p1", website="not a url")])
    result = await provider_with(handler).search(
        DiscoveryQuery(text_query="x", require_website=False)
    )
    assert result.businesses[0].canonical_domain is None


@pytest.mark.asyncio
async def test_raw_payload_is_preserved_for_provenance() -> None:
    handler, _ = single_page([place("p1")])
    result = await provider_with(handler).search(DiscoveryQuery(text_query="x"))
    assert result.businesses[0].raw["id"] == "p1"


@pytest.mark.asyncio
async def test_usage_policy_records_licensing_constraints() -> None:
    """Places data is licensed, not owned; the constraint travels with it."""
    handler, _ = single_page([place("p1")])
    result = await provider_with(handler).search(DiscoveryQuery(text_query="x"))
    policy = result.usage_policy
    assert policy["attribution_required"] is True
    assert policy["place_id_cacheable"] is True
    assert "cache" in policy["note"].lower()


@pytest.mark.asyncio
async def test_cost_is_estimated_per_page() -> None:
    handler, _ = single_page([place("p1")])
    result = await provider_with(handler).search(DiscoveryQuery(text_query="x"))
    assert result.estimated_cost_usd > 0
    assert result.pages_fetched == 1


# ==========================================================================
# Pagination
# ==========================================================================
@pytest.mark.asyncio
async def test_pagination_follows_the_next_page_token(monkeypatch) -> None:
    # The adapter sleeps 2s between pages because Places rejects a token used
    # too quickly; that delay is irrelevant to correctness here.
    import titan.providers.places as places_module

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(places_module.asyncio, "sleep", no_sleep)

    pages = [
        {"places": [place("p1")], "nextPageToken": "token-2"},
        {"places": [place("p2")]},
    ]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        calls.append(body)
        return httpx.Response(200, json=pages[len(calls) - 1])

    result = await provider_with(handler).search(
        DiscoveryQuery(text_query="x", max_results=40)
    )
    assert [b.place_id for b in result.businesses] == ["p1", "p2"]
    assert result.pages_fetched == 2
    assert "token-2" in calls[1]


# ==========================================================================
# Error taxonomy
# ==========================================================================
@pytest.mark.asyncio
async def test_auth_failure_is_not_retried() -> None:
    """Retrying an invalid key burns quota against a guaranteed failure."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(
            403, json={"error": {"message": "Places API has not been used"}}
        )

    with pytest.raises(PlacesError) as excinfo:
        await provider_with(handler).search(DiscoveryQuery(text_query="x"))

    assert excinfo.value.retryable is False
    assert attempts["n"] == 1
    assert "TITAN_GOOGLE_PLACES_API_KEY" in str(excinfo.value)


@pytest.mark.asyncio
async def test_bad_request_is_not_retried() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, json={"error": {"message": "invalid field mask"}})

    with pytest.raises(PlacesError) as excinfo:
        await provider_with(handler).search(DiscoveryQuery(text_query="x"))
    assert excinfo.value.retryable is False
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_rate_limit_is_retried_then_succeeds(monkeypatch) -> None:
    import titan.providers.places as places_module

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(places_module.asyncio, "sleep", no_sleep)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(200, json={"places": [place("p1")]})

    result = await provider_with(handler).search(DiscoveryQuery(text_query="x"))
    assert len(result.businesses) == 1
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_server_error_is_retried_then_gives_up(monkeypatch) -> None:
    import titan.providers.places as places_module

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(places_module.asyncio, "sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    with pytest.raises(PlacesError) as excinfo:
        await provider_with(handler).search(DiscoveryQuery(text_query="x"))
    assert excinfo.value.retryable is True


@pytest.mark.asyncio
async def test_health_check_reports_failure_honestly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "denied"}})

    ok, detail = await provider_with(handler).health_check()
    assert ok is False
    assert "denied" in detail or "authentication" in detail
