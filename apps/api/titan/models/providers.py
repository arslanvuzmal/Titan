"""Model provider adapters.

NVIDIA, OpenRouter and Cloudflare AI Gateway all expose an OpenAI-compatible
``/chat/completions`` surface, so one adapter serves all three with different
base URLs. Gemini's API differs enough to warrant its own.

None of these has been called with a real credential in this build. They are
**implemented, not live-verified** -- see the verification report. In particular
the model identifiers in ``config.py`` are unverified placeholders;
``titan validate-models`` is what checks them against a live catalogue.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from titan.models.gateway import ModelError, ModelResponse


class OpenAICompatibleProvider:
    """Adapter for any OpenAI-compatible chat-completions endpoint.

    Used for NVIDIA NIM, OpenRouter, and Cloudflare AI Gateway. The differences
    between them are the base URL, the auth header, and whether structured
    output is supported -- all constructor arguments rather than subclasses.
    """

    def __init__(
        self,
        name: str,
        api_key: str,
        base_url: str,
        *,
        supports_json_schema: bool = True,
        extra_headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._supports_json_schema = supports_json_schema
        self._extra_headers = extra_headers or {}
        self._client = client

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    **self._extra_headers,
                },
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def complete(
        self,
        *,
        model_id: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        json_schema: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_schema is not None and self._supports_json_schema:
            # Ask for structured output where the provider supports it. The
            # gateway still validates, because "supports" is not "guarantees".
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "Response"),
                    "schema": json_schema,
                    "strict": False,
                },
            }

        started = time.perf_counter()
        try:
            client = await self._http()
            response = await client.post(
                "/chat/completions", json=payload, timeout=timeout_seconds
            )
        except httpx.HTTPError as exc:
            raise ModelError(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code != 200:
            detail = response.text[:400]
            raise ModelError(f"{self.name}: HTTP {response.status_code}: {detail}")

        body = response.json()
        try:
            text = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"{self.name}: unexpected response shape") from exc

        usage = body.get("usage") or {}
        return ModelResponse(
            text=text,
            provider=self.name,
            model_id=model_id,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            latency_ms=latency_ms,
            # Most OpenAI-compatible endpoints do not report cost; the gateway's
            # estimate stands in, and cost_estimated stays true in the ledger.
            cost_usd=float(usage.get("cost") or 0.0),
            raw=body,
        )

    async def list_models(self) -> list[str]:
        client = await self._http()
        response = await client.get("/models")
        if response.status_code != 200:
            raise ModelError(f"{self.name}: catalogue HTTP {response.status_code}")
        data = response.json().get("data") or []
        return [str(entry.get("id")) for entry in data if entry.get("id")]

    async def health_check(self) -> tuple[bool, str]:
        try:
            models = await self.list_models()
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, f"ok ({len(models)} models)"


class GeminiProvider:
    """Google Generative Language API adapter."""

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=httpx.Timeout(60.0, connect=10.0)
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def complete(
        self,
        *,
        model_id: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        json_schema: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> ModelResponse:
        generation: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if json_schema is not None:
            generation["responseMimeType"] = "application/json"

        payload = {
            # Gemini has a dedicated system channel, which keeps Titan's
            # instructions structurally separate from untrusted page content.
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": generation,
        }

        started = time.perf_counter()
        try:
            client = await self._http()
            response = await client.post(
                f"/models/{model_id}:generateContent",
                params={"key": self._api_key},
                json=payload,
                timeout=timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ModelError(f"gemini: {type(exc).__name__}: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code != 200:
            raise ModelError(
                f"gemini: HTTP {response.status_code}: {response.text[:400]}"
            )

        body = response.json()
        try:
            parts = body["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError, TypeError) as exc:
            # A blocked response has candidates but no parts; surface the reason
            # rather than an opaque parse error.
            reason = (body.get("promptFeedback") or {}).get("blockReason")
            raise ModelError(
                "gemini: no content in response"
                + (f" (blocked: {reason})" if reason else "")
            ) from exc

        usage = body.get("usageMetadata") or {}
        return ModelResponse(
            text=text,
            provider=self.name,
            model_id=model_id,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
            latency_ms=latency_ms,
            cost_usd=0.0,
            raw=body,
        )

    async def list_models(self) -> list[str]:
        client = await self._http()
        response = await client.get("/models", params={"key": self._api_key})
        if response.status_code != 200:
            raise ModelError(f"gemini: catalogue HTTP {response.status_code}")
        out: list[str] = []
        for entry in response.json().get("models") or []:
            name = str(entry.get("name") or "")
            # The API returns "models/gemini-2.0-flash"; routes use the bare id.
            out.append(name.removeprefix("models/"))
        return [name for name in out if name]

    async def health_check(self) -> tuple[bool, str]:
        try:
            models = await self.list_models()
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, f"ok ({len(models)} models)"


class MockChatProvider:
    """Deterministic provider for tests.

    Returns queued responses in order. Because the gateway validates every
    response against a schema, a test can queue malformed output and assert the
    repair path behaves, which a stub that always succeeds cannot exercise.
    """

    name = "mock"

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        catalogue: list[str] | None = None,
        fail_times: int = 0,
    ) -> None:
        self.responses = list(responses or [])
        self.catalogue = catalogue or ["mock/model-a", "mock/model-b"]
        self.fail_times = fail_times
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        model_id: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        json_schema: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> ModelResponse:
        self.calls.append(
            {
                "model_id": model_id,
                "system": system,
                "user": user,
                "temperature": temperature,
                "has_schema": json_schema is not None,
            }
        )
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ModelError("mock: injected provider failure")
        text = self.responses.pop(0) if self.responses else "{}"
        return ModelResponse(
            text=text,
            provider=self.name,
            model_id=model_id,
            input_tokens=len(system) // 4 + len(user) // 4,
            output_tokens=len(text) // 4,
            latency_ms=1,
            cost_usd=0.0,
        )

    async def list_models(self) -> list[str]:
        return list(self.catalogue)

    async def health_check(self) -> tuple[bool, str]:
        return True, "mock provider"


def build_providers(settings: Any) -> dict[str, Any]:
    """Instantiate every provider that has a credential configured.

    A provider without a key is simply absent, so a route pointing at it fails
    with ProviderUnavailableError rather than a confusing auth error at call
    time.
    """
    providers: dict[str, Any] = {}

    if settings.nvidia_api_key is not None:
        providers["nvidia"] = OpenAICompatibleProvider(
            "nvidia",
            settings.nvidia_api_key.get_secret_value(),
            str(settings.nvidia_base_url),
        )
    if settings.openrouter_api_key is not None:
        providers["openrouter"] = OpenAICompatibleProvider(
            "openrouter",
            settings.openrouter_api_key.get_secret_value(),
            str(settings.openrouter_base_url),
            extra_headers={
                # OpenRouter asks callers to identify themselves.
                "HTTP-Referer": str(settings.owner_portfolio_url),
                "X-Title": "Titan-OS",
            },
        )
    if settings.gemini_api_key is not None:
        providers["gemini"] = GeminiProvider(
            settings.gemini_api_key.get_secret_value(),
            str(settings.gemini_base_url),
        )
    if (
        settings.cloudflare_api_token is not None
        and settings.cloudflare_account_id
        and settings.cloudflare_gateway_id
    ):
        providers["cloudflare"] = OpenAICompatibleProvider(
            "cloudflare",
            settings.cloudflare_api_token.get_secret_value(),
            f"https://gateway.ai.cloudflare.com/v1/"
            f"{settings.cloudflare_account_id}/{settings.cloudflare_gateway_id}/compat",
        )

    return providers


__all__ = [
    "GeminiProvider",
    "MockChatProvider",
    "OpenAICompatibleProvider",
    "build_providers",
]
