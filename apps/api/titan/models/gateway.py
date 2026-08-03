"""Provider-independent model gateway.

Mission section 9. Titan's domain code never imports a model SDK; it asks the
gateway for a *task* (extraction, research, verification, message, premium) and
the gateway resolves that to a configured provider and model ID.

Four properties this design exists to guarantee:

1. **Typed output or failure.** Every call declares a Pydantic schema. An
   unparseable response is retried with a bounded repair prompt, then fails.
   Malformed data never silently enters business logic (gap analysis H-14).
2. **The exact model ID is recorded.** Not the route name, not the alias --
   the string actually sent to the provider (section 9.1), so a past decision
   stays interpretable after the routing table changes.
3. **Cost is bounded before the call, not after.** The budget check happens
   first; a call that would exceed the remaining budget is refused rather than
   made and then regretted.
4. **A failing provider is stopped, not hammered.** A circuit breaker opens
   after consecutive failures and closes on a probe.

Model IDs in ``config.py`` are configuration, never hardcoded assumptions, and
they are **unverified placeholders** until checked against a live catalogue --
which is what ``validate_models()`` is for.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from titan.config import Settings, get_settings
from titan.db.enums import ModelTask
from titan.models.channels import PromptBundle

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

#: Consecutive failures before a provider is taken out of rotation.
CIRCUIT_FAILURE_THRESHOLD = 5
#: How long the breaker stays open before a probe is allowed through.
CIRCUIT_OPEN_SECONDS = 60.0
#: Bounded repair attempts for a schema-invalid response.
MAX_REPAIR_ATTEMPTS = 2


class ModelError(RuntimeError):
    """Base class for gateway failures."""


class BudgetExceededError(ModelError):
    """The call was refused before it was made."""


class CircuitOpenError(ModelError):
    """The provider is failing and has been taken out of rotation."""


class SchemaValidationError(ModelError):
    """The provider returned data that does not match the declared schema."""


class ProviderUnavailableError(ModelError):
    """No provider is configured for the requested route."""


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """One completed model call."""

    text: str
    provider: str
    #: Verbatim identifier sent to the provider.
    model_id: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    cost_usd: float
    used_fallback: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class ChatProvider(Protocol):
    """What every model provider must offer. Deliberately minimal."""

    name: str

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
    ) -> ModelResponse: ...

    async def list_models(self) -> list[str]: ...

    async def health_check(self) -> tuple[bool, str]: ...


# --------------------------------------------------------------------------
# Cost accounting
# --------------------------------------------------------------------------
@dataclass
class BudgetLedger:
    """In-process spend tracking, checked before each call.

    Authoritative spend lives in the ``usage_ledger`` table; this is the fast
    path that prevents a runaway loop from making a thousand calls before the
    database notices. Both are enforced -- this one bounds the blast radius
    within a single process.
    """

    workspace_limit_usd: float
    campaign_limit_usd: float
    lead_limit_usd: float
    hard_stop: bool = True

    workspace_spent: float = 0.0
    campaign_spent: dict[str, float] = field(default_factory=dict)
    lead_spent: dict[str, float] = field(default_factory=dict)
    premium_calls: int = 0
    total_calls: int = 0

    def check(
        self, *, estimated_usd: float, campaign_id: str | None, lead_id: str | None
    ) -> None:
        if not self.hard_stop:
            return
        if self.workspace_spent + estimated_usd > self.workspace_limit_usd:
            raise BudgetExceededError(
                f"workspace budget ${self.workspace_limit_usd:.2f} would be exceeded "
                f"(spent ${self.workspace_spent:.4f}, this call ~${estimated_usd:.4f})"
            )
        if campaign_id:
            spent = self.campaign_spent.get(campaign_id, 0.0)
            if spent + estimated_usd > self.campaign_limit_usd:
                raise BudgetExceededError(
                    f"campaign budget ${self.campaign_limit_usd:.2f} would be exceeded"
                )
        if lead_id:
            spent = self.lead_spent.get(lead_id, 0.0)
            if spent + estimated_usd > self.lead_limit_usd:
                raise BudgetExceededError(
                    f"per-lead budget ${self.lead_limit_usd:.2f} would be exceeded"
                )

    def record(
        self,
        *,
        actual_usd: float,
        campaign_id: str | None,
        lead_id: str | None,
        premium: bool,
    ) -> None:
        """Record real spend.

        Note the asymmetry with :meth:`check`: an over-budget call that already
        succeeded is recorded in full and NOT retried elsewhere (mission 9.5,
        "no fallback after a successful but over-budget call"). Paying twice for
        the same answer is worse than being slightly over.
        """
        self.workspace_spent += actual_usd
        if campaign_id:
            self.campaign_spent[campaign_id] = (
                self.campaign_spent.get(campaign_id, 0.0) + actual_usd
            )
        if lead_id:
            self.lead_spent[lead_id] = self.lead_spent.get(lead_id, 0.0) + actual_usd
        self.total_calls += 1
        if premium:
            self.premium_calls += 1

    def premium_share(self) -> float:
        return self.premium_calls / self.total_calls if self.total_calls else 0.0


@dataclass
class CircuitBreaker:
    """Per-provider failure isolation."""

    failures: int = 0
    opened_at: float | None = None

    def allow(self, now: float) -> bool:
        if self.opened_at is None:
            return True
        if now - self.opened_at >= CIRCUIT_OPEN_SECONDS:
            # Half-open: let one probe through.
            self.opened_at = None
            self.failures = CIRCUIT_FAILURE_THRESHOLD - 1
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self, now: float) -> None:
        self.failures += 1
        if self.failures >= CIRCUIT_FAILURE_THRESHOLD:
            self.opened_at = now


# --------------------------------------------------------------------------
# The gateway
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Route:
    provider: str
    model_id: str

    @classmethod
    def parse(cls, spec: str) -> Route:
        """Parse ``provider:model/id``.

        The model half may contain colons and slashes, so only the first colon
        separates provider from model.
        """
        provider, sep, model_id = spec.partition(":")
        if not sep or not provider or not model_id:
            raise ValueError(
                f"model route {spec!r} must be 'provider:model_id', "
                "e.g. 'nvidia:meta/llama-3.1-8b-instruct'"
            )
        return cls(provider=provider.strip(), model_id=model_id.strip())


class ModelGateway:
    """Routes typed model requests to configured providers."""

    def __init__(
        self,
        providers: dict[str, ChatProvider],
        settings: Settings | None = None,
        *,
        budget: BudgetLedger | None = None,
        now_fn: Any = None,
    ) -> None:
        self._providers = providers
        self._settings = settings or get_settings()
        self._breakers: dict[str, CircuitBreaker] = {
            name: CircuitBreaker() for name in providers
        }
        self._now = now_fn or time.monotonic
        self.budget = budget or BudgetLedger(
            workspace_limit_usd=self._settings.budget_workspace_daily_usd,
            campaign_limit_usd=self._settings.budget_campaign_daily_usd,
            lead_limit_usd=self._settings.budget_lead_usd,
            hard_stop=self._settings.budget_hard_stop,
        )
        #: Every call, for the usage ledger writer to persist.
        self.calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------- routing
    def route_for(self, task: ModelTask) -> Route:
        spec = {
            ModelTask.EXTRACTION: self._settings.model_route_extraction,
            ModelTask.RESEARCH: self._settings.model_route_research,
            ModelTask.VERIFICATION: self._settings.model_route_verification,
            ModelTask.MESSAGE: self._settings.model_route_message,
            ModelTask.PREMIUM: self._settings.model_route_premium,
        }[task]
        return Route.parse(spec)

    def _provider(self, name: str) -> ChatProvider:
        provider = self._providers.get(name)
        if provider is None:
            raise ProviderUnavailableError(
                f"no provider named {name!r} is configured; "
                f"available: {sorted(self._providers) or 'none'}"
            )
        return provider

    # -------------------------------------------------------------- calling
    async def complete_typed(
        self,
        task: ModelTask,
        schema: type[T],
        bundle: PromptBundle,
        *,
        campaign_id: str | None = None,
        lead_id: str | None = None,
        max_tokens: int = 1500,
        temperature: float = 0.2,
        allow_fallback: bool = True,
    ) -> tuple[T, ModelResponse]:
        """Run a model call and return a validated instance of ``schema``.

        Raises rather than returning partial data: a caller that gets a value
        back can rely on it having passed validation.
        """
        route = self.route_for(task)
        is_premium = task is ModelTask.PREMIUM

        if is_premium and self.budget.total_calls:
            # Prospective share, including the call about to be made. Checking
            # the share *before* the call meant a cap of 0 never bound, because
            # 0 premium of 1 total is 0%.
            prospective = (self.budget.premium_calls + 1) / (self.budget.total_calls + 1)
            if prospective > self._settings.budget_premium_share_max:
                raise BudgetExceededError(
                    f"premium model share would reach {prospective:.0%}, above the "
                    f"configured maximum {self._settings.budget_premium_share_max:.0%}"
                )

        estimated = self._estimate_cost(route, max_tokens)
        self.budget.check(
            estimated_usd=estimated, campaign_id=campaign_id, lead_id=lead_id
        )

        system, user = bundle.build()
        json_schema = schema.model_json_schema()

        attempts: list[Route] = [route]
        if allow_fallback:
            attempts.extend(self._fallbacks(route))

        last_error: Exception | None = None
        for index, candidate in enumerate(attempts):
            breaker = self._breakers.setdefault(candidate.provider, CircuitBreaker())
            if not breaker.allow(self._now()):
                last_error = CircuitOpenError(
                    f"circuit open for provider {candidate.provider!r}"
                )
                continue

            try:
                parsed, response = await self._call_with_repair(
                    candidate,
                    schema,
                    system,
                    user,
                    json_schema,
                    max_tokens,
                    temperature,
                )
            except SchemaValidationError as exc:
                # The provider answered; the model produced non-conforming
                # output. That is not a provider fault, so the breaker is left
                # alone -- otherwise one bad prompt removes a healthy provider.
                last_error = exc
                logger.warning(
                    "model returned schema-invalid output",
                    extra={"provider": candidate.provider, "model": candidate.model_id},
                )
                continue
            except (ModelError, TimeoutError, OSError) as exc:
                breaker.record_failure(self._now())
                last_error = exc
                logger.warning(
                    "model call failed",
                    extra={
                        "provider": candidate.provider,
                        "model": candidate.model_id,
                        "error_code": type(exc).__name__,
                    },
                )
                continue

            breaker.record_success()
            used_fallback = index > 0
            self.budget.record(
                actual_usd=response.cost_usd,
                campaign_id=campaign_id,
                lead_id=lead_id,
                premium=is_premium,
            )
            self.calls.append(
                {
                    "task": task.value,
                    "provider": response.provider,
                    "model_id": response.model_id,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "latency_ms": response.latency_ms,
                    "cost_usd": response.cost_usd,
                    "used_fallback": used_fallback,
                    "campaign_id": campaign_id,
                    "lead_id": lead_id,
                    "request_hash": _hash(system + user),
                    "occurred_at": dt.datetime.now(dt.UTC),
                }
            )
            return parsed, ModelResponse(
                text=response.text,
                provider=response.provider,
                model_id=response.model_id,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_ms=response.latency_ms,
                cost_usd=response.cost_usd,
                used_fallback=used_fallback,
                raw=response.raw,
            )

        if isinstance(last_error, SchemaValidationError | CircuitOpenError):
            # Re-raise the specific error: "the model cannot produce this
            # schema" and "the provider is down" call for different responses.
            raise last_error
        raise ModelError(
            f"all providers failed for task {task.value}: {last_error}"
        ) from last_error

    async def _call_with_repair(
        self,
        route: Route,
        schema: type[T],
        system: str,
        user: str,
        json_schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> tuple[T, ModelResponse]:
        """Call, validate, and repair up to a bounded number of times."""
        provider = self._provider(route.provider)
        message = user
        last_validation: ValidationError | None = None

        for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
            response = await asyncio.wait_for(
                provider.complete(
                    model_id=route.model_id,
                    system=system,
                    user=message,
                    max_tokens=max_tokens,
                    temperature=temperature if attempt == 0 else 0.0,
                    json_schema=json_schema,
                    timeout_seconds=self._settings.model_gateway_timeout_seconds,
                ),
                timeout=self._settings.model_gateway_timeout_seconds + 5,
            )
            try:
                return schema.model_validate_json(_extract_json(response.text)), response
            except (ValidationError, ValueError) as exc:
                last_validation = exc if isinstance(exc, ValidationError) else None
                if attempt == MAX_REPAIR_ATTEMPTS:
                    break
                # Repair prompt: show the model its own output and the errors,
                # at temperature 0. Bounded, so a model that cannot produce the
                # schema fails loudly instead of looping.
                message = (
                    f"{user}\n\n"
                    "Your previous response did not match the required JSON "
                    f"schema.\n\nYour response was:\n{response.text[:2000]}\n\n"
                    f"The validation errors were:\n{str(exc)[:1500]}\n\n"
                    "Return ONLY valid JSON matching the schema. No prose, no "
                    "code fences."
                )

        raise SchemaValidationError(
            f"{route.provider}:{route.model_id} did not produce valid "
            f"{schema.__name__} after {MAX_REPAIR_ATTEMPTS} repair attempts: "
            f"{last_validation}"
        )

    def _fallbacks(self, primary: Route) -> list[Route]:
        """Other configured providers that could serve the same request.

        Deliberately conservative: a fallback uses that provider's *own*
        configured model for the task, never the primary's model ID, because a
        model identifier is not portable across providers.
        """
        out: list[Route] = []
        for name in self._providers:
            if name == primary.provider:
                continue
            for spec in (
                self._settings.model_route_extraction,
                self._settings.model_route_research,
                self._settings.model_route_message,
            ):
                try:
                    candidate = Route.parse(spec)
                except ValueError:
                    continue
                if candidate.provider == name:
                    out.append(candidate)
                    break
        return out

    def _estimate_cost(self, route: Route, max_tokens: int) -> float:
        """Pre-call cost estimate.

        Estimates are intentionally pessimistic (assume max_tokens output) so
        the budget check errs toward refusing rather than overspending.
        """
        per_million = _PRICE_HINTS.get(route.provider, 1.0)
        return (max_tokens / 1_000_000) * per_million

    # ------------------------------------------------------------ validation
    async def validate_models(self) -> dict[str, Any]:
        """Check every configured route against the provider's live catalogue.

        This is the answer to "do not hardcode assumed model IDs": the defaults
        in config.py are placeholders, and this command is how an operator finds
        out before a campaign does.
        """
        report: dict[str, Any] = {"routes": [], "ok": True}
        for task in ModelTask:
            entry: dict[str, Any] = {"task": task.value}
            try:
                route = self.route_for(task)
                entry["provider"] = route.provider
                entry["model_id"] = route.model_id
            except ValueError as exc:
                entry["status"] = "malformed_route"
                entry["detail"] = str(exc)
                report["ok"] = False
                report["routes"].append(entry)
                continue

            provider = self._providers.get(route.provider)
            if provider is None:
                entry["status"] = "provider_not_configured"
                report["ok"] = False
                report["routes"].append(entry)
                continue

            try:
                catalogue = await provider.list_models()
            except Exception as exc:
                entry["status"] = "catalogue_unavailable"
                entry["detail"] = f"{type(exc).__name__}: {exc}"
                report["ok"] = False
                report["routes"].append(entry)
                continue

            if route.model_id in catalogue:
                entry["status"] = "ok"
            else:
                entry["status"] = "model_not_found"
                entry["detail"] = (
                    f"{route.model_id!r} is not in {route.provider}'s catalogue "
                    f"({len(catalogue)} models available)"
                )
                report["ok"] = False
            report["routes"].append(entry)
        return report


_PRICE_HINTS: dict[str, float] = {
    # USD per million output tokens. Rough, used only for the pre-call estimate;
    # actual cost comes from the provider response where it reports one.
    "nvidia": 0.20,
    "gemini": 0.30,
    "openrouter": 3.00,
    "cloudflare": 0.50,
    "mock": 0.0,
}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _extract_json(text: str) -> str:
    """Pull a JSON object out of a response that may be fenced or prefixed.

    Models wrap JSON in ```json fences or preface it with a sentence often
    enough that failing on it would waste a repair round trip.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```", 2)[1] if stripped.count("```") >= 2 else stripped
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    if stripped.startswith(("{", "[")):
        return stripped
    start = min(
        (i for i in (stripped.find("{"), stripped.find("[")) if i != -1), default=-1
    )
    if start == -1:
        raise ValueError("no JSON object found in the response")
    end = max(stripped.rfind("}"), stripped.rfind("]"))
    if end <= start:
        raise ValueError("no complete JSON object found in the response")
    return stripped[start : end + 1]


def parse_json_or_raise(text: str) -> Any:
    return json.loads(_extract_json(text))


__all__ = [
    "BudgetExceededError",
    "BudgetLedger",
    "ChatProvider",
    "CircuitBreaker",
    "CircuitOpenError",
    "ModelError",
    "ModelGateway",
    "ModelResponse",
    "ProviderUnavailableError",
    "Route",
    "SchemaValidationError",
    "parse_json_or_raise",
]
