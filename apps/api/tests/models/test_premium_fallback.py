"""The frontier model that was configured, funded, and never reachable.

``_fallbacks`` scanned the extraction, research and message routes for other
providers that could serve a request. The premium route is on none of them, so
it was never a candidate for anything.

The consequence was invisible until Gemini started returning 429. From that
moment the only fallback for a message was
``nvidia:meta/llama-3.1-8b-instruct`` -- every email this system phrased was
phrased by an eight-billion-parameter model, while ``anthropic/claude-sonnet-4``
sat configured, in credit, and unconsulted.
"""

from __future__ import annotations

import pytest
from titan.config import Settings
from titan.models.gateway import ModelGateway, Route


class _Stub:
    def __init__(self, name: str) -> None:
        self.name = name


def gateway(**overrides) -> ModelGateway:
    base = {
        "model_route_extraction": "nvidia:cheap-extract",
        "model_route_research": "nvidia:cheap-research",
        "model_route_message": "gemini:gemini-flash",
        "model_route_premium": "openrouter:anthropic/claude-sonnet-4",
    }
    base.update(overrides)
    settings = Settings(**base)
    providers = {n: _Stub(n) for n in ("nvidia", "gemini", "openrouter")}
    return ModelGateway(providers, settings)


def chain(g: ModelGateway, spec: str) -> list[str]:
    return [f"{r.provider}:{r.model_id}" for r in g._fallbacks(Route.parse(spec))]


def test_the_premium_route_is_reachable_as_a_fallback() -> None:
    """Planted violation: drop the premium append and this fails."""
    assert "openrouter:anthropic/claude-sonnet-4" in chain(
        gateway(), "gemini:gemini-flash"
    )


def test_it_is_tried_last() -> None:
    """Appended rather than inserted. Cheaper providers are still tried first --
    this is what happens when they have all failed, not a quiet upgrade of the
    default."""
    assert chain(gateway(), "gemini:gemini-flash")[-1].startswith("openrouter:")


def test_the_primary_is_never_its_own_fallback() -> None:
    """Retrying the provider that just failed is not a fallback."""
    assert not any(
        c.startswith("openrouter:")
        for c in chain(gateway(), "openrouter:anthropic/claude-sonnet-4")
    )


def test_an_unconfigured_premium_provider_is_not_offered() -> None:
    """A route naming a provider with no credentials would fail every call and
    hide the real error behind a second one."""
    settings = Settings(
        model_route_extraction="nvidia:cheap",
        model_route_research="nvidia:cheap2",
        model_route_message="gemini:flash",
        model_route_premium="openrouter:anthropic/claude-sonnet-4",
    )
    g = ModelGateway({"nvidia": _Stub("nvidia"), "gemini": _Stub("gemini")}, settings)

    assert not any(c.startswith("openrouter:") for c in chain(g, "gemini:flash"))


def test_a_malformed_premium_route_does_not_break_the_chain() -> None:
    """The cheap fallbacks must survive a typo in an unrelated setting."""
    g = gateway(model_route_premium="not-a-route")

    assert chain(g, "gemini:gemini-flash"), "the whole chain was lost"


def test_the_premium_route_is_recognised_by_route_not_by_task() -> None:
    """Planted violation: key the share guard on ``ModelTask.PREMIUM`` again
    and this fails.

    Now that premium is reachable by falling through, guarding only the premium
    *task* leaves a back door that opens on exactly the day every cheap provider
    is rate-limited.
    """
    g = gateway()

    assert g._is_premium_route(Route.parse("openrouter:anthropic/claude-sonnet-4"))
    assert not g._is_premium_route(Route.parse("nvidia:cheap-extract"))


@pytest.mark.parametrize("share", [0.0, 0.15, 1.0])
def test_the_cap_is_still_the_operators_number(share: float) -> None:
    g = gateway()
    g._settings = Settings(  # type: ignore[misc]
        model_route_extraction="nvidia:cheap-extract",
        model_route_research="nvidia:cheap-research",
        model_route_message="gemini:gemini-flash",
        model_route_premium="openrouter:anthropic/claude-sonnet-4",
        budget_premium_share_max=share,
    )
    assert g._settings.budget_premium_share_max == share
