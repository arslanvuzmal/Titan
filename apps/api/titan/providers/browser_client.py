"""Client for the isolated browser evidence worker.

The control plane never fetches a lead's website itself. It asks this client,
which asks the browser worker, which is the only component permitted to touch
attacker-controlled URLs (invariant 3).

Two checks bracket the call:

* **Before**: the seed URL passes the control plane's own SSRF guard, and the
  vetted addresses are sent along so the worker can pin to them.
* **After**: the redirect chain the worker *reports* is re-validated here. A
  compromised worker must not be able to smuggle a private-origin capture into
  the evidence store by simply claiming the hop was fine.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from titan.config import Settings, get_settings
from titan.contracts.evidence import CrawlResult, ResearchRequest
from titan.security.url_guard import validate_redirect_chain, validate_url

logger = logging.getLogger(__name__)


#: How many times to wait for a free browser lane before giving up.
#:
#: The Temporal retry policy above this handles a worker that is down. These
#: attempts handle one that is merely busy, which is a different condition and
#: a much commoner one.
SATURATION_ATTEMPTS = 6

#: Base gap between those attempts, multiplied by the attempt number. Six
#: attempts therefore span about two minutes, which is longer than any single
#: crawl takes and so longer than a lane can stay occupied.
SATURATION_WAIT_SECONDS = 6.0


#: One permit per browser lane, shared by every crawl in this process.
#:
#: The waiting above treats a 503 gracefully; this stops most of them being
#: provoked. The Temporal worker runs ``max_concurrent_activities=8`` against a
#: browser worker serving four crawls at a time, so under load half of every
#: batch was guaranteed an instant 503 -- the worker's own comment says "an
#: unbounded worker will happily start more crawls than the browser worker can
#: serve", which is exactly what 8-against-4 does.
#:
#: Lowering the activity limit to four would have fixed it by starving
#: everything else: most activities here never touch a browser, and throttling
#: reporting and verification to the crawl budget is the wrong trade. The limit
#: belongs at the resource, not at the worker, so a crawl waits for a lane and
#: every other activity keeps its slot.
#:
#: Built lazily because the permit count comes from settings, and asyncio
#: primitives must not be created before there is a loop to bind them to.
_lane_semaphore: asyncio.Semaphore | None = None
_lane_permits: int | None = None


def _lanes(settings: Settings) -> asyncio.Semaphore:
    global _lane_semaphore, _lane_permits
    permits = settings.browser_worker_concurrency
    if _lane_semaphore is None or _lane_permits != permits:
        _lane_semaphore = asyncio.Semaphore(permits)
        _lane_permits = permits
    return _lane_semaphore


class BrowserWorkerError(RuntimeError):
    """The worker could not be reached or returned something unusable."""


class UrlBlockedError(RuntimeError):
    """The URL guard refused the target.

    Named in the workflow's ``non_retryable_error_types``: a refused URL is
    refused identically on every retry, so retrying only wastes time.
    """


@dataclass(frozen=True, slots=True)
class RecheckResult:
    """What one URL did when asked again.

    Deliberately not a verdict. ``status is None`` means the probe could not
    reach a conclusion -- blocked by the URL guard, the worker unreachable, the
    page refusing to answer -- and a caller must treat that as "unchanged",
    never as "fixed". The asymmetry matters: reading an inconclusive probe as
    "the defect is gone" would silently discard real leads, and nothing
    downstream would ever show it happened.
    """

    url: str
    allowed: bool = True
    blocked_reason: str | None = None
    status: int | None = None
    is_empty: bool | None = None
    error: str | None = None

    @property
    def is_conclusive(self) -> bool:
        return self.status is not None


class BrowserWorkerClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {}
            token = self._settings.browser_worker_token
            if token is not None:
                headers["Authorization"] = f"Bearer {token.get_secret_value()}"
            self._client = httpx.AsyncClient(
                base_url=str(self._settings.browser_worker_url).rstrip("/"),
                headers=headers,
                timeout=httpx.Timeout(
                    self._settings.crawl_timeout_seconds + 60, connect=10.0
                ),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def research(
        self,
        *,
        request_id: str,
        seed_url: str,
        priority_paths: tuple[str, ...] = (),
    ) -> CrawlResult:
        settings = self._settings

        verdict = validate_url(seed_url)
        if not verdict.allowed:
            raise UrlBlockedError(
                f"{seed_url}: {verdict.reason.value if verdict.reason else 'refused'}"
                + (f" ({verdict.detail})" if verdict.detail else "")
            )

        payload = ResearchRequest(
            request_id=request_id,
            seed_url=seed_url,
            max_pages=settings.crawl_max_pages,
            max_depth=settings.crawl_max_depth,
            timeout_seconds=settings.crawl_timeout_seconds,
            max_response_bytes=settings.crawl_max_response_bytes,
            max_redirects=settings.crawl_max_redirects,
            user_agent=settings.crawl_user_agent,
            respect_robots=settings.crawl_respect_robots,
            priority_paths=list(priority_paths),
            # The worker pins to the addresses we already vetted rather than
            # re-resolving, which is what closes the DNS-rebinding window.
            pinned_ips=list(verdict.resolved_ips),
        )

        # A 503 here means "every lane is busy", which is a wait, not a
        # failure. Raising immediately made it one: the Temporal worker runs
        # eight activity slots against four browser lanes, so under any real
        # backlog half of every batch got an instant 503, spent its retries on
        # backoff, and the lead was thrown away. 358 of 412 research runs in a
        # day, against a browser worker that was healthy throughout and simply
        # busy.
        #
        # Waiting in-process for a lane costs one idle coroutine and converts
        # most of those into ordinary successes. The Temporal retry above it is
        # unchanged and still catches a worker that is genuinely down --
        # this only stops a *busy* worker being reported as a broken one.
        # Held across the whole exchange, not just the request: the lane is
        # occupied until the worker answers, so releasing early would let the
        # next crawl start against a worker that is still busy -- which is the
        # oversubscription this exists to prevent.
        response = None
        async with _lanes(self._settings):
            for attempt in range(SATURATION_ATTEMPTS):
                try:
                    http = await self._http()
                    response = await http.post(
                        "/research", json=payload.model_dump(mode="json")
                    )
                except httpx.HTTPError as exc:
                    raise BrowserWorkerError(
                        f"browser worker unreachable: {type(exc).__name__}: {exc}"
                    ) from exc

                if response.status_code != 503:
                    break

                if attempt + 1 < SATURATION_ATTEMPTS:
                    # Linear, not exponential. The wait is for a lane to free
                    # up, and lanes free up at a roughly constant rate --
                    # backing off exponentially would idle longest exactly when
                    # the queue is draining fastest.
                    await asyncio.sleep(SATURATION_WAIT_SECONDS * (attempt + 1))

        assert response is not None  # the loop runs at least once
        if response.status_code == 503:
            raise BrowserWorkerError(
                f"browser worker saturated after waiting "
                f"{SATURATION_ATTEMPTS} times for a free lane"
            )
        if response.status_code != 200:
            raise BrowserWorkerError(
                f"browser worker HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            result = CrawlResult.model_validate(response.json())
        except Exception as exc:
            # A worker returning something outside the contract is a version
            # mismatch or a compromise; either way it must not be ingested.
            raise BrowserWorkerError(f"contract violation: {exc}") from exc

        self._reverify(result)
        return result

    async def recheck(self, url: str, *, timeout_seconds: int = 25) -> RecheckResult:
        """Ask what one URL does *now*. One request, no crawl.

        Used to test a claim Titan is about to make before it makes it. The
        send gate only asks how old a measurement is, never whether it is still
        true, so a business that fixed its booking page a fortnight ago was
        still being told it was broken.

        Goes through the browser worker rather than fetching here, and that is
        a security boundary rather than a convenience: the worker is
        deliberately the only component that opens attacker-controlled URLs and
        deliberately the only one holding no database, mail or model
        credentials. Fetching a lead's site from this process would hand a
        hostile page all three.

        Returns what was seen. It never decides whether the observation
        contradicts a claim -- that belongs with the claim.
        """
        verdict = validate_url(url)
        if not verdict.allowed:
            return RecheckResult(
                url=url,
                allowed=False,
                blocked_reason=(
                    verdict.reason.value if verdict.reason else "url_guard_refused"
                ),
            )

        async with _lanes(self._settings):
            try:
                http = await self._http()
                response = await http.post(
                    "/recheck",
                    json={
                        "url": url,
                        "user_agent": self._settings.crawl_user_agent,
                        "timeout_seconds": timeout_seconds,
                    },
                )
            except httpx.HTTPError as exc:
                # Inconclusive, not failed. A probe that could not run is not
                # evidence that anything changed, and the caller must not read
                # it as one.
                return RecheckResult(
                    url=url, error=f"{type(exc).__name__}: {str(exc)[:160]}"
                )

        if response.status_code != 200:
            return RecheckResult(
                url=url, error=f"HTTP {response.status_code}: {response.text[:160]}"
            )

        try:
            body = response.json()
        except Exception as exc:
            return RecheckResult(url=url, error=f"unparseable response: {exc}")

        return RecheckResult(
            url=url,
            allowed=bool(body.get("allowed", True)),
            blocked_reason=body.get("blocked_reason"),
            status=body.get("status"),
            is_empty=body.get("is_empty"),
        )

    def _reverify(self, result: CrawlResult) -> None:
        """Re-check what the worker claims it visited.

        The worker validates its own redirects, but the control plane does not
        take its word for it.
        """
        if result.redirect_chain:
            verdict = validate_redirect_chain(
                list(result.redirect_chain),
                max_redirects=self._settings.crawl_max_redirects,
            )
            if not verdict.allowed:
                raise UrlBlockedError(
                    "worker reported a redirect chain that fails revalidation: "
                    f"{verdict.reason.value if verdict.reason else 'refused'}"
                )

        for page in result.pages:
            page_verdict = validate_url(page.final_url)
            if not page_verdict.allowed:
                raise UrlBlockedError(
                    f"worker returned a page from a disallowed origin: {page.final_url}"
                )

    async def health_check(self) -> tuple[bool, str]:
        try:
            http = await self._http()
            response = await http.get("/health", timeout=10.0)
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if response.status_code == 200:
            data: dict[str, Any] = response.json()
            return True, f"ok (worker {data.get('worker_version', '?')})"
        return False, f"HTTP {response.status_code}"


__all__ = ["BrowserWorkerClient", "BrowserWorkerError", "UrlBlockedError"]
