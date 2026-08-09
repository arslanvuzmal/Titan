"""Smartlead client and delivery adapter tests.

Hermetic: every HTTP call is intercepted, so these run with no API key and no
network. They do NOT prove the integration works against the live service --
see docs/audits/FINAL-PRODUCTION-VERIFICATION.md for what has and has not been
verified against real credentials.

The tests that matter most here are the refusals. Smartlead is a campaign
platform, so the failure mode this integration has to prevent is Titan handing
a message to a campaign that then sends *more* messages on its own schedule --
mail no gate in this repository ever evaluated.
"""

from __future__ import annotations

import httpx
import pytest
from titan.delivery.providers.base import (
    OutboundEmail,
    SendErrorKind,
    WebhookVerificationError,
)
from titan.delivery.providers.smartlead import (
    BODY_FIELD,
    IDEMPOTENCY_FIELD,
    SUBJECT_FIELD,
    SmartleadProvider,
)
from titan.providers.smartlead import (
    MAX_LEADS_PER_IMPORT,
    SmartleadAuthError,
    SmartleadClient,
    SmartleadError,
)

SINGLE_STEP_CAMPAIGN = {
    "id": 42,
    "name": "Titan carrier",
    "status": "START",
    "sequences": [{"seq_number": 1, "subject": "{{titan_subject}}"}],
}


def client_with(handler) -> SmartleadClient:
    transport = httpx.MockTransport(handler)
    return SmartleadClient(
        "test-key",
        base_url="https://server.smartlead.ai/api/v1",
        client=httpx.AsyncClient(transport=transport),
    )


def email(**overrides) -> OutboundEmail:
    base = {
        "to_email": "sam@fixture-business.test",
        "from_email": "arslan@mail.arslanvuzmallone.dev",
        "from_name": "Arslan Vuzmal Lone",
        "reply_to": "arslan@mail.arslanvuzmallone.dev",
        "subject": "A broken button on your booking page",
        "text_body": "Line one.\nLine two.",
        "idempotency_key": "idem-lead-1-step-0",
    }
    base.update(overrides)
    return OutboundEmail(**base)


# ==========================================================================
# Client: transport and auth
# ==========================================================================
@pytest.mark.asyncio
async def test_the_api_key_travels_as_a_query_parameter() -> None:
    """Smartlead's design, not ours -- but it must actually be sent."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[SINGLE_STEP_CAMPAIGN])

    campaigns = await client_with(handler).list_campaigns()

    assert "api_key=test-key" in seen["url"]
    assert [c.id for c in campaigns] == [42]


@pytest.mark.asyncio
async def test_a_rejected_key_raises_auth_rather_than_a_generic_error() -> None:
    """The outbox maps auth failures to 'stop, do not suppress the recipient'."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    with pytest.raises(SmartleadAuthError):
        await client_with(handler).list_campaigns()


@pytest.mark.asyncio
async def test_an_oversized_lead_import_is_refused_before_the_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("the request should never have been made")

    leads = [{"email": f"p{i}@x.test"} for i in range(MAX_LEADS_PER_IMPORT + 1)]
    with pytest.raises(SmartleadError, match="exceeds Smartlead's limit"):
        await client_with(handler).add_leads(42, leads)


@pytest.mark.asyncio
async def test_titan_never_overrides_smartleads_block_or_unsubscribe_lists() -> None:
    """Those lists are a second opinion on Titan's own suppression table."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as jsonlib

        seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"upload_count": 1, "bulk_lead_ids": ["7"]})

    await client_with(handler).add_leads(42, [{"email": "sam@x.test"}])

    settings = seen["body"]["settings"]
    assert settings["ignore_global_block_list"] is False
    assert settings["ignore_unsubscribe_list"] is False


@pytest.mark.asyncio
async def test_sequence_steps_are_written_with_explicit_order() -> None:
    """Order is the caller's intent, not list position luck."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as jsonlib

        seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    await client_with(handler).set_sequences(
        42, [{"subject": "first"}, {"subject": "second"}]
    )

    assert [s["seq_number"] for s in seen["body"]["sequences"]] == [1, 2]


# ==========================================================================
# Adapter: the campaign-shape refusal
# ==========================================================================
def provider_with(handler, campaign_id: int = 42) -> SmartleadProvider:
    transport = httpx.MockTransport(handler)
    client = SmartleadClient(
        "test-key",
        base_url="https://server.smartlead.ai/api/v1",
        client=httpx.AsyncClient(transport=transport),
    )
    return SmartleadProvider("test-key", campaign_id, client=client)


@pytest.mark.asyncio
async def test_a_multi_step_campaign_is_refused() -> None:
    """The failure this integration exists to prevent.

    A carrier campaign with follow-up steps would send messages Titan never
    drafted, never checked against evidence and never authorized.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **SINGLE_STEP_CAMPAIGN,
                "sequences": [{"seq_number": 1}, {"seq_number": 2}, {"seq_number": 3}],
            },
        )

    provider = provider_with(handler)
    ok, detail = await provider.verify_campaign_shape()

    assert ok is False
    assert "3 sequence steps" in detail

    result = await provider.send(email())
    assert result.accepted is False
    # A configuration fault: stop, but do not punish the recipient.
    assert result.is_configuration_failure is True
    assert result.is_permanent_failure is False


@pytest.mark.asyncio
async def test_a_campaign_that_does_not_report_its_shape_is_refused() -> None:
    """Unknown shape is a refusal, not an assumption."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 42, "name": "opaque", "status": "START"})

    ok, detail = await provider_with(handler).verify_campaign_shape()

    assert ok is False
    assert "unknown" in detail


# ==========================================================================
# Adapter: the handover
# ==========================================================================
@pytest.mark.asyncio
async def test_the_validated_subject_and_body_travel_as_data() -> None:
    """Titan validated this exact text; Smartlead must not re-render it."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as jsonlib

        if request.url.path.endswith("/leads"):
            seen["body"] = jsonlib.loads(request.content)
            return httpx.Response(200, json={"upload_count": 1, "bulk_lead_ids": ["9"]})
        return httpx.Response(200, json=SINGLE_STEP_CAMPAIGN)

    result = await provider_with(handler).send(email())

    assert result.accepted is True
    assert result.provider_message_id == "9"

    fields = seen["body"]["lead_list"][0]["custom_fields"]
    assert fields[SUBJECT_FIELD] == "A broken button on your booking page"
    assert "Line one." in fields[BODY_FIELD]
    assert fields[IDEMPOTENCY_FIELD] == "idem-lead-1-step-0"


@pytest.mark.asyncio
async def test_a_duplicate_import_is_accepted_rather_than_resent() -> None:
    """Invariant 11 across the handover.

    Smartlead dedupes by address within a campaign. A retry after a lost
    response must therefore report success, not queue a second email.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/leads"):
            return httpx.Response(
                200, json={"upload_count": 0, "already_added_to_campaign": 1}
            )
        return httpx.Response(200, json=SINGLE_STEP_CAMPAIGN)

    result = await provider_with(handler).send(email())

    assert result.accepted is True
    # No lead id came back, so the idempotency key stands in -- never empty.
    assert result.provider_message_id == "idem-lead-1-step-0"


@pytest.mark.asyncio
async def test_an_invalid_address_is_a_permanent_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/leads"):
            return httpx.Response(200, json={"upload_count": 0, "invalid_email_count": 1})
        return httpx.Response(200, json=SINGLE_STEP_CAMPAIGN)

    result = await provider_with(handler).send(email())

    assert result.accepted is False
    assert result.error_kind is SendErrorKind.INVALID_RECIPIENT
    assert result.is_permanent_failure is True


@pytest.mark.asyncio
async def test_a_rejected_key_stops_without_suppressing_the_recipient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/leads"):
            return httpx.Response(401, json={"message": "unauthorized"})
        return httpx.Response(200, json=SINGLE_STEP_CAMPAIGN)

    result = await provider_with(handler).send(email())

    assert result.accepted is False
    assert result.error_kind is SendErrorKind.AUTH
    assert result.is_configuration_failure is True
    assert result.is_permanent_failure is False


@pytest.mark.asyncio
async def test_a_transport_failure_is_retryable_not_permanent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/leads"):
            return httpx.Response(503, text="upstream unavailable")
        return httpx.Response(200, json=SINGLE_STEP_CAMPAIGN)

    result = await provider_with(handler).send(email())

    assert result.accepted is False
    assert result.error_kind is SendErrorKind.TRANSIENT
    assert result.is_permanent_failure is False
    assert result.is_configuration_failure is False


# ==========================================================================
# Adapter: webhooks fail closed
# ==========================================================================
def test_webhook_verification_refuses_rather_than_pretending() -> None:
    """No confirmed signing scheme means no verification, and no ingestion.

    A check that looks like verification but proves nothing is worse than
    none: it would accept state changes from anyone who found the URL.
    """
    provider = SmartleadProvider("test-key", 42, client=client_with(lambda r: None))

    with pytest.raises(WebhookVerificationError):
        provider.verify_webhook(payload=b"{}", headers={})

    assert provider.normalize_webhook({"event": "EMAIL_SENT"}) is None
