"""Google Places (New) discovery adapter.

Mission section 6.1. Uses the Places API **v1** surface
(``places.googleapis.com/v1``), which requires an explicit ``X-Goog-FieldMask``
on every request -- there is no "return everything" mode, and a missing mask is
an error rather than a default.

Two things this adapter is careful about beyond the mechanics:

* **Field masks are the cost control.** Places bills by SKU according to which
  fields you request. The mask here is the minimum Titan needs, split into a
  cheap search pass and an optional detail pass, so a large discovery run does
  not silently bill at the Enterprise SKU.
* **Places data is licensed, not owned.** Google's terms restrict caching and
  redistribution of most fields. Titan stores the Place ID (explicitly
  cacheable), a small set of fields needed to operate, and attribution -- and
  keeps its own independently-derived evidence in separate tables so the two are
  never confused.

Live-provider-tested (2026-08-03): a real text search against
``places.googleapis.com/v1`` returned and parsed 8 UK businesses. Pagination
past page 1 and the details pass are implemented but were not exercised live.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from titan.config import Settings

logger = logging.getLogger(__name__)

#: Minimum fields for the search pass. Every added field can change the billing
#: SKU, so this list is deliberately short and reviewed when changed.
SEARCH_FIELD_MASK = ",".join(
    (
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.websiteUri",
        "places.nationalPhoneNumber",
        "places.rating",
        "places.userRatingCount",
        "places.businessStatus",
        "places.primaryType",
        "places.location",
        "nextPageToken",
    )
)

#: Additional fields fetched only for leads that survive the first filter.
DETAIL_FIELD_MASK = ",".join(
    (
        "id",
        "displayName",
        "formattedAddress",
        "websiteUri",
        "nationalPhoneNumber",
        "internationalPhoneNumber",
        "rating",
        "userRatingCount",
        "businessStatus",
        "primaryType",
        "types",
        "location",
        "addressComponents",
        "utcOffsetMinutes",
    )
)

#: Places caps text search at 3 pages of 20.
MAX_PAGES = 3
PAGE_SIZE = 20

#: Rough per-request cost used for the usage ledger. Real billing comes from
#: the Google console; this is an estimate and is recorded as such.
ESTIMATED_COST_PER_SEARCH_USD = 0.032
ESTIMATED_COST_PER_DETAIL_USD = 0.017


class PlacesError(RuntimeError):
    """Normalized Places API failure."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DiscoveredBusiness:
    """One business as Places reported it. Provenance is preserved verbatim."""

    place_id: str
    display_name: str
    formatted_address: str | None
    website_uri: str | None
    phone: str | None
    rating: float | None
    review_count: int | None
    business_status: str | None
    primary_type: str | None
    latitude: float | None
    longitude: float | None
    country_code: str | None = None
    utc_offset_minutes: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def canonical_domain(self) -> str | None:
        """Registrable-ish domain from the website URI.

        Used for deduplication. Returns None rather than guessing when the URI
        is missing or unparseable -- a wrong domain would merge two unrelated
        businesses.
        """
        if not self.website_uri:
            return None
        match = re.match(r"^https?://([^/:?#]+)", self.website_uri.strip(), re.I)
        if not match:
            return None
        host = match.group(1).lower().removeprefix("www.")
        return host or None

    @property
    def is_operational(self) -> bool:
        return (self.business_status or "OPERATIONAL").upper() == "OPERATIONAL"


@dataclass(frozen=True, slots=True)
class DiscoveryQuery:
    """A bounded search. Every limit is explicit; none defaults to unbounded."""

    text_query: str
    #: ISO-3166-1 alpha-2, e.g. "GB". Restricts, not biases.
    included_region: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    radius_meters: int | None = None
    min_rating: float | None = None
    min_review_count: int | None = None
    require_website: bool = True
    open_now: bool | None = None
    max_results: int = 60

    def __post_init__(self) -> None:
        if not self.text_query.strip():
            raise ValueError("text_query must not be empty")
        if self.max_results < 1 or self.max_results > MAX_PAGES * PAGE_SIZE:
            raise ValueError(f"max_results must be between 1 and {MAX_PAGES * PAGE_SIZE}")


@dataclass
class DiscoveryResult:
    businesses: list[DiscoveredBusiness] = field(default_factory=list)
    pages_fetched: int = 0
    returned_before_filtering: int = 0
    filtered_out: dict[str, int] = field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    #: Attribution and usage constraints, stored on the lead_sources row.
    usage_policy: dict[str, Any] = field(default_factory=dict)


class GooglePlacesProvider:
    """Places API (New) text search + optional place details."""

    name = "google_places"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://places.googleapis.com/v1",
        *,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._max_retries = max_retries

    @classmethod
    def from_settings(cls, settings: Settings) -> GooglePlacesProvider:
        """Build from configuration.

        The unwrapping of the SecretStr happens here, inside the provider
        module, so that callers never hold a raw key -- the repository
        invariant test enforces that boundary.
        """
        if settings.google_places_api_key is None:
            raise PlacesError("TITAN_GOOGLE_PLACES_API_KEY is not configured")
        return cls(settings.google_places_api_key.get_secret_value())

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(30.0, connect=10.0),
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------- searching
    async def search(self, query: DiscoveryQuery) -> DiscoveryResult:
        """Run a paginated text search and apply Titan's filters."""
        result = DiscoveryResult(
            usage_policy={
                "source": "google_places_api_v1",
                "attribution_required": True,
                "place_id_cacheable": True,
                "other_fields_cache_days": 30,
                "note": (
                    "Place IDs may be cached indefinitely; other Places content is "
                    "subject to Google's caching and display terms. Titan stores "
                    "its own measurements separately."
                ),
            }
        )
        seen: set[str] = set()
        page_token: str | None = None

        for _page in range(MAX_PAGES):
            if len(result.businesses) >= query.max_results:
                break

            body = self._build_request(query, page_token)
            payload = await self._post("/places:searchText", body, SEARCH_FIELD_MASK)
            result.pages_fetched += 1
            result.estimated_cost_usd += ESTIMATED_COST_PER_SEARCH_USD

            places = payload.get("places") or []
            result.returned_before_filtering += len(places)

            for entry in places:
                business = self._parse(entry)
                if business is None:
                    self._count(result, "unparseable")
                    continue
                if business.place_id in seen:
                    self._count(result, "duplicate_place_id")
                    continue
                reason = self._rejection_reason(business, query)
                if reason:
                    self._count(result, reason)
                    continue
                seen.add(business.place_id)
                result.businesses.append(business)
                if len(result.businesses) >= query.max_results:
                    break

            page_token = payload.get("nextPageToken")
            if not page_token:
                break
            # Places rejects a nextPageToken used too quickly.
            await asyncio.sleep(2.0)

        return result

    def _build_request(
        self, query: DiscoveryQuery, page_token: str | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "textQuery": query.text_query,
            "pageSize": min(PAGE_SIZE, query.max_results),
        }
        if page_token:
            body["pageToken"] = page_token
        if query.included_region:
            body["regionCode"] = query.included_region.upper()
        if query.min_rating is not None:
            body["minRating"] = query.min_rating
        if query.open_now is not None:
            body["openNow"] = query.open_now
        if (
            query.latitude is not None
            and query.longitude is not None
            and query.radius_meters
        ):
            # locationRestriction, not locationBias: a bias would return results
            # far outside the target geography and quietly waste budget.
            body["locationRestriction"] = {
                "circle": {
                    "center": {"latitude": query.latitude, "longitude": query.longitude},
                    "radius": float(min(query.radius_meters, 50_000)),
                }
            }
        return body

    def _rejection_reason(
        self, business: DiscoveredBusiness, query: DiscoveryQuery
    ) -> str | None:
        if not business.is_operational:
            return "not_operational"
        if query.require_website and not business.website_uri:
            return "no_website"
        if query.min_review_count is not None and (
            business.review_count is None
            or business.review_count < query.min_review_count
        ):
            return "below_min_review_count"
        if query.min_rating is not None and (
            business.rating is None or business.rating < query.min_rating
        ):
            return "below_min_rating"
        return None

    @staticmethod
    def _count(result: DiscoveryResult, reason: str) -> None:
        result.filtered_out[reason] = result.filtered_out.get(reason, 0) + 1

    # -------------------------------------------------------------- details
    async def get_details(self, place_id: str) -> DiscoveredBusiness | None:
        """Fetch the richer field set for one place."""
        if not place_id.strip():
            raise ValueError("place_id must not be empty")
        payload = await self._get(f"/places/{place_id}", DETAIL_FIELD_MASK)
        return self._parse(payload)

    # ------------------------------------------------------------ transport
    async def _post(
        self, path: str, body: dict[str, Any], field_mask: str
    ) -> dict[str, Any]:
        return await self._request("POST", path, field_mask, json=body)

    async def _get(self, path: str, field_mask: str) -> dict[str, Any]:
        return await self._request("GET", path, field_mask)

    async def _request(
        self, method: str, path: str, field_mask: str, **kwargs: Any
    ) -> dict[str, Any]:
        headers = {
            "X-Goog-Api-Key": self._api_key,
            # v1 requires an explicit mask; omitting it is an error, not a
            # default, which is what keeps billing predictable.
            "X-Goog-FieldMask": field_mask,
        }
        client = await self._http()
        delay = 1.0

        for attempt in range(self._max_retries):
            try:
                response = await client.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                if attempt == self._max_retries - 1:
                    raise PlacesError(
                        f"{type(exc).__name__}: {exc}", retryable=True
                    ) from exc
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if response.status_code == 200:
                return response.json()

            error = self._normalize_error(response)
            if not error.retryable or attempt == self._max_retries - 1:
                raise error
            retry_after = response.headers.get("retry-after")
            await asyncio.sleep(
                float(retry_after) if (retry_after or "").isdigit() else delay
            )
            delay *= 2

        raise PlacesError("exhausted retries", retryable=True)

    @staticmethod
    def _normalize_error(response: httpx.Response) -> PlacesError:
        try:
            detail = (response.json().get("error") or {}).get("message", "")
        except ValueError:
            detail = response.text[:300]

        status = response.status_code
        if status == 429:
            return PlacesError(f"rate limited: {detail}", retryable=True)
        if status in (500, 502, 503, 504):
            return PlacesError(f"upstream error {status}: {detail}", retryable=True)
        if status in (401, 403):
            # Not retryable: an invalid key or a disabled API will not fix
            # itself, and retrying burns quota against a guaranteed failure.
            return PlacesError(
                f"authentication or authorization failed ({status}): {detail}. "
                "Check TITAN_GOOGLE_PLACES_API_KEY and that the Places API (New) "
                "is enabled for the project.",
                retryable=False,
            )
        if status == 400:
            return PlacesError(f"invalid request: {detail}", retryable=False)
        return PlacesError(f"HTTP {status}: {detail}", retryable=False)

    # -------------------------------------------------------------- parsing
    @staticmethod
    def _parse(entry: dict[str, Any]) -> DiscoveredBusiness | None:
        place_id = entry.get("id")
        display = entry.get("displayName") or {}
        name = display.get("text") if isinstance(display, dict) else display
        if not place_id or not name:
            return None

        location = entry.get("location") or {}
        country = None
        for component in entry.get("addressComponents") or []:
            if "country" in (component.get("types") or []):
                country = component.get("shortText")
                break

        return DiscoveredBusiness(
            place_id=str(place_id),
            display_name=str(name),
            formatted_address=entry.get("formattedAddress"),
            website_uri=entry.get("websiteUri"),
            phone=entry.get("nationalPhoneNumber")
            or entry.get("internationalPhoneNumber"),
            rating=entry.get("rating"),
            review_count=entry.get("userRatingCount"),
            business_status=entry.get("businessStatus"),
            primary_type=entry.get("primaryType"),
            latitude=location.get("latitude"),
            longitude=location.get("longitude"),
            country_code=country,
            utc_offset_minutes=entry.get("utcOffsetMinutes"),
            raw=entry,
        )

    async def health_check(self) -> tuple[bool, str]:
        """Cheapest possible real call, to confirm the key works."""
        try:
            await self._post(
                "/places:searchText",
                {"textQuery": "coffee", "pageSize": 1},
                "places.id",
            )
        except PlacesError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, "ok"


__all__ = [
    "DETAIL_FIELD_MASK",
    "SEARCH_FIELD_MASK",
    "DiscoveredBusiness",
    "DiscoveryQuery",
    "DiscoveryResult",
    "GooglePlacesProvider",
    "PlacesError",
]
