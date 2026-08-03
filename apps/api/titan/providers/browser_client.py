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

import logging
from typing import Any

import httpx

from titan.config import Settings, get_settings
from titan.contracts.evidence import CrawlResult, ResearchRequest
from titan.security.url_guard import validate_redirect_chain, validate_url

logger = logging.getLogger(__name__)


class BrowserWorkerError(RuntimeError):
    """The worker could not be reached or returned something unusable."""


class UrlBlockedError(RuntimeError):
    """The URL guard refused the target.

    Named in the workflow's ``non_retryable_error_types``: a refused URL is
    refused identically on every retry, so retrying only wastes time.
    """


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

        try:
            http = await self._http()
            response = await http.post("/research", json=payload.model_dump(mode="json"))
        except httpx.HTTPError as exc:
            raise BrowserWorkerError(
                f"browser worker unreachable: {type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code == 503:
            raise BrowserWorkerError("browser worker saturated")
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
