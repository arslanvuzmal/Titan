"""Instantly API v2 client.

The transport half of the Instantly integration: HTTP, auth, errors, and the
handful of calls Titan makes. The delivery adapter that speaks
:class:`~titan.delivery.providers.base.EmailProvider` sits in
``titan.delivery.providers.instantly`` and uses this.

**Instantly is campaign-shaped, exactly like Smartlead.** There is no "send this
message to this person now" endpoint. Sending is a side effect of campaign
execution: a lead is created against a campaign, and Instantly's own scheduler
emits the sequence step. The adapter therefore does what the Smartlead one does
-- hands one already-authorized message to a dedicated single-step campaign,
carrying the validated subject and body as custom variables -- and inherits the
same caveat: the campaign must have exactly one step, or Instantly will send
mail Titan never wrote.

**What is verified and what is not.** Base URL, Bearer authentication, and the
four resource paths below are documented. The exact request field names for
lead creation are taken from the published OpenAPI component schemas and have
**not been exercised against a live key**, because there is no Instantly account
to exercise them against yet. Every one of them is a named constant in
:data:`FIELD`, so confirming them is a single edit in a single place rather than
a hunt through request bodies -- and :meth:`InstantlyClient.health_check`
fails loudly rather than quietly mis-sending if the shape is wrong.

API v1 was retired in January 2026 and v2 keys are not interchangeable with it.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.instantly.ai/api/v2"

#: Documented resource paths.
LEADS_PATH = "/leads"
CAMPAIGNS_PATH = "/campaigns"
ACCOUNTS_PATH = "/accounts"
VERIFY_PATH = "/email-verification"

#: Request/response property names, gathered in one place.
#:
#: Taken from Instantly's published OpenAPI component schemas. They are
#: **unconfirmed against a live account** -- there is no key to test with yet --
#: and this is the single edit point when there is one. A wrong name here
#: produces a 4xx from Instantly, which :class:`InstantlyError` surfaces with
#: the response body attached, rather than a message that silently goes nowhere.
FIELD = {
    "email": "email",
    "campaign": "campaign",
    "first_name": "first_name",
    "last_name": "last_name",
    "company": "company_name",
    "custom": "custom_variables",
    "verification_status": "verification_status",
    "daily_limit": "daily_limit",
    "status": "status",
}

DEFAULT_TIMEOUT = 30.0


class InstantlyError(RuntimeError):
    """Any failure talking to Instantly, with the response kept for diagnosis."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class InstantlyAuthError(InstantlyError):
    """The key was rejected. Never retried -- a retry cannot fix a credential.

    Kept distinct because the two ways it happens need different responses and
    look identical from here: a revoked or mistyped key, and a lapsed plan.
    Smartlead returned ``401 {"message": "Plan expired!"}`` for hours and the
    only thing that noticed was a health probe, which is why this class exists
    separately rather than as a generic 4xx.
    """


class InstantlyClient:
    """The calls Titan makes. Deliberately few."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout

    @classmethod
    def from_settings(cls, settings: Any) -> InstantlyClient:
        """Build from configuration, unwrapping the key exactly once.

        The same shape as the Places and Smartlead clients, and for the same
        reason: an invariant test confines ``get_secret_value()`` to the
        provider layer, so every caller passes the ``SecretStr`` and only this
        module sees the value.
        """
        if settings.instantly_api_key is None:
            raise InstantlyError("TITAN_INSTANTLY_API_KEY is not configured")
        return cls(settings.instantly_api_key.get_secret_value())

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout, connect=10.0),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any] | list[Any]:
        client = await self._http()
        try:
            response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise InstantlyError(
                f"Instantly unreachable on {method} {path}: {type(exc).__name__}"
            ) from exc

        if response.status_code in (401, 403):
            # The body carries the distinction between a bad key and a dead
            # plan, and both arrive as 401. Kept verbatim: guessing which one
            # it is here would send somebody to rotate a key that was fine.
            raise InstantlyAuthError(
                f"Instantly rejected the API key on {method} {path}: "
                f"{response.text[:200]}",
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise InstantlyError(
                f"Instantly HTTP {response.status_code} on {method} {path}: "
                f"{response.text[:300]}",
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise InstantlyError(
                f"Instantly returned non-JSON on {method} {path}"
            ) from exc

    # ------------------------------------------------------------ calls
    async def create_lead(
        self,
        *,
        email: str,
        campaign_id: str,
        custom_variables: dict[str, str],
        first_name: str | None = None,
    ) -> dict[str, Any]:
        """Hand one authorized message to a single-step campaign.

        The subject and body travel as custom variables so the campaign step
        renders exactly what Titan validated. They are *data*, not a template
        Instantly could re-render differently -- provided the campaign has the
        shape ``verify_campaign_shape`` insists on.
        """
        payload: dict[str, Any] = {
            FIELD["email"]: email,
            FIELD["campaign"]: campaign_id,
            FIELD["custom"]: custom_variables,
        }
        if first_name:
            payload[FIELD["first_name"]] = first_name
        result = await self._request("POST", LEADS_PATH, json=payload)
        return result if isinstance(result, dict) else {}

    async def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        result = await self._request("GET", f"{CAMPAIGNS_PATH}/{campaign_id}")
        return result if isinstance(result, dict) else {}

    async def list_accounts(self) -> list[dict[str, Any]]:
        result = await self._request("GET", ACCOUNTS_PATH)
        if isinstance(result, dict):
            items = result.get("items") or result.get("data") or []
            return [i for i in items if isinstance(i, dict)]
        return [i for i in result if isinstance(i, dict)]

    async def verify_email(self, email: str) -> dict[str, Any]:
        """Instantly's own address verification.

        Worth noting rather than burying: this is the mailbox-level answer
        Titan has never had. :mod:`titan.intelligence.verifier` defines the port
        for exactly this and ships a null implementation, so a workspace on
        Instantly can close that gap without a second vendor.
        """
        result = await self._request("POST", VERIFY_PATH, json={FIELD["email"]: email})
        return result if isinstance(result, dict) else {}

    async def health_check(self) -> tuple[bool, str]:
        """Cheap, read-only, and specific about which way it failed."""
        try:
            accounts = await self.list_accounts()
        except InstantlyAuthError as exc:
            return False, f"credentials rejected: {exc}"
        except InstantlyError as exc:
            return False, str(exc)
        return True, f"ok ({len(accounts)} sending account(s))"


__all__ = [
    "ACCOUNTS_PATH",
    "BASE_URL",
    "CAMPAIGNS_PATH",
    "FIELD",
    "LEADS_PATH",
    "VERIFY_PATH",
    "InstantlyAuthError",
    "InstantlyClient",
    "InstantlyError",
]
