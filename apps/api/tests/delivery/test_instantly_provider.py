"""The Instantly carrier, and the one thing it must never do.

Instantly is campaign-shaped like Smartlead: there is no transactional send, so
``send()`` hands one already-authorized message to a dedicated single-step
campaign. That makes the step count the load-bearing safety property. A campaign
with three steps sends two messages Titan never drafted, never validated against
evidence and never authorized -- silently, outside every gate in this
repository.

Nothing here talks to Instantly. There is no account yet, and the request field
names are unconfirmed against a live key; these prove the behaviour Titan
controls, which is what it refuses and how it fails.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from titan.db.enums import MessageState
from titan.delivery.providers.base import (
    EmailProvider,
    OutboundEmail,
    SendErrorKind,
    WebhookVerificationError,
)
from titan.delivery.providers.instantly import (
    BODY_FIELD,
    SUBJECT_FIELD,
    InstantlyProvider,
    _sequence_step_count,
)
from titan.providers.instantly import InstantlyError

pytestmark = pytest.mark.asyncio


class FakeClient:
    """Records what the adapter asked for, answers what the test wants."""

    def __init__(self, *, campaign: dict | None = None, lead_id: str | None = "lead-1"):
        self.campaign = campaign if campaign is not None else _steps(1)
        self.lead_id = lead_id
        self.created: list[dict] = []
        self.raise_on_create: Exception | None = None

    async def get_campaign(self, campaign_id: str) -> dict:
        if isinstance(self.campaign, Exception):
            raise self.campaign
        return self.campaign

    async def create_lead(self, **kwargs) -> dict:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        self.created.append(kwargs)
        return {"id": self.lead_id} if self.lead_id else {}

    async def health_check(self) -> tuple[bool, str]:
        return True, "ok"


def _steps(n: int) -> dict:
    return {"sequences": [{"steps": [{"step": i} for i in range(n)]}]}


def provider(client: FakeClient, **kwargs) -> InstantlyProvider:
    return InstantlyProvider(client, campaign_id="camp-1", **kwargs)  # type: ignore[arg-type]


def email(**overrides) -> OutboundEmail:
    base = {
        "to_email": "hello@prospect.test",
        "from_email": "sales@sender.test",
        "from_name": "Sender",
        "reply_to": "sales@sender.test",
        "subject": "Your /booking page is down",
        "text_body": "Hi there,\n\nYour /booking page returns HTTP 404.\n",
        "idempotency_key": "idem-1",
    }
    base.update(overrides)
    return OutboundEmail(**base)


# ------------------------------------------------------- the port itself


def test_it_satisfies_the_provider_port() -> None:
    """The whole point of the swap being cheap. Five methods, structurally
    checked, so a carrier change is an adapter rather than a migration."""
    assert isinstance(provider(FakeClient()), EmailProvider)


# ------------------------------------------- the step count is load-bearing


async def test_a_single_step_campaign_is_accepted() -> None:
    p = provider(FakeClient(campaign=_steps(1)))

    ok, detail = await p.verify_campaign_shape()

    assert ok, detail


async def test_a_multi_step_campaign_is_refused() -> None:
    """Planted violation: accept any step count and Instantly sends two
    messages Titan never wrote, to a stranger, outside every gate here."""
    p = provider(FakeClient(campaign=_steps(3)))

    ok, detail = await p.verify_campaign_shape()

    assert not ok
    assert "3 sequence steps" in detail


async def test_a_campaign_with_no_steps_is_refused() -> None:
    """Nothing to render the message into. Accepting it would report a send
    that never happened."""
    ok, _ = await provider(FakeClient(campaign=_steps(0))).verify_campaign_shape()

    assert not ok


async def test_an_unreadable_shape_is_refused_not_assumed() -> None:
    """Planted violation: treat an unreadable campaign as one step and this
    fails.

    ``None`` is not zero and is not one. A campaign whose shape could not be
    determined might send three messages Titan never wrote, and the honest
    answer is to refuse rather than to assume the convenient one.
    """
    ok, detail = await provider(
        FakeClient(campaign={"unexpected": True})
    ).verify_campaign_shape()

    assert not ok
    assert "could not read" in detail


async def test_a_send_verifies_the_shape_before_handing_anything_over() -> None:
    """The check is worthless if the first message goes out before it runs."""
    client = FakeClient(campaign=_steps(4))

    result = await provider(client).send(email())

    assert not result.accepted
    assert result.error_kind is SendErrorKind.INVALID_SENDER
    assert result.is_configuration_failure, (
        "the recipient must not be suppressed for our misconfiguration"
    )
    assert client.created == [], "a message was handed over to a 4-step campaign"


async def test_the_shape_is_checked_once_per_campaign_not_once_ever() -> None:
    """Each market has its own carrier and each can be edited independently in
    the Instantly UI."""
    client = FakeClient(campaign=_steps(1))
    p = provider(client)

    await p.send(email())
    await p.send(email(carrier_campaign_id=99))

    assert {c["campaign_id"] for c in client.created} == {"camp-1", "99"}


# --------------------------------------------------------------- sending


async def test_the_validated_text_travels_as_data() -> None:
    """Subject and body go as custom variables the single step renders, not as
    a template Instantly could re-render into something else."""
    client = FakeClient()

    await provider(client).send(email())

    custom = client.created[0]["custom_variables"]
    assert custom[SUBJECT_FIELD] == "Your /booking page is down"
    assert "HTTP 404" in custom[BODY_FIELD]


async def test_acceptance_without_an_id_is_not_acceptance() -> None:
    """Planted violation: return accepted=True with no id and reconciliation,
    status and bounce attribution all lose the thread -- every one of them keys
    on the provider's identifier."""
    result = await provider(FakeClient(lead_id=None)).send(email())

    assert not result.accepted


async def test_a_rejected_key_is_a_configuration_failure_not_a_retry() -> None:
    """Retrying cannot fix a credential, and an expired plan looks identical
    from here. Retrying either just burns the queue."""
    from titan.providers.instantly import InstantlyAuthError

    client = FakeClient()
    client.raise_on_create = InstantlyAuthError("Plan expired!")

    result = await provider(client).send(email())

    assert result.error_kind is SendErrorKind.AUTH
    assert result.is_configuration_failure


async def test_a_network_failure_is_transient() -> None:
    client = FakeClient()
    client.raise_on_create = InstantlyError("unreachable")

    result = await provider(client).send(email())

    assert result.error_kind is SendErrorKind.TRANSIENT


# --------------------------------------------------------------- webhooks


def test_an_unsigned_webhook_is_refused_when_no_secret_is_set() -> None:
    """Planted violation: accept when unconfigured and anyone who guesses the
    URL can mark a message bounced and suppress a real prospect for ever."""
    with pytest.raises(WebhookVerificationError):
        provider(FakeClient()).verify_webhook(payload=b"{}", headers={})


def test_a_correctly_signed_webhook_verifies() -> None:
    secret = "s3cret"
    payload = b'{"event_type":"email_bounced"}'
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    provider(FakeClient(), webhook_secret=secret).verify_webhook(
        payload=payload, headers={"x-instantly-signature": signature}
    )


def test_a_forged_signature_is_refused() -> None:
    with pytest.raises(WebhookVerificationError):
        provider(FakeClient(), webhook_secret="s3cret").verify_webhook(
            payload=b"{}", headers={"x-instantly-signature": "deadbeef"}
        )


def test_a_bounce_is_normalized_as_one() -> None:
    event = provider(FakeClient()).normalize_webhook(
        {"event_type": "email_bounced", "id": "e1", "lead_id": "l1", "email": "a@b.test"}
    )

    assert event is not None
    assert event.state is MessageState.BOUNCED
    assert event.is_hard_bounce


def test_an_unknown_event_gets_no_state_rather_than_a_guessed_one() -> None:
    """Planted violation: default to SENT and an unrecognised event marks
    delivered a message that may never have left."""
    event = provider(FakeClient()).normalize_webhook(
        {"event_type": "something_new", "id": "e2"}
    )

    assert event is not None
    assert event.state is None
    assert not event.is_hard_bounce


def test_an_event_with_no_type_is_dropped() -> None:
    assert provider(FakeClient()).normalize_webhook({"id": "e3"}) is None


# ------------------------------------------------------------ step counting


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"sequences": [{"steps": [1]}]}, 1),
        ({"sequence": [{"steps": [1, 2]}]}, 2),
        ({"sequences": [{"steps": [1]}, {"steps": [1, 2]}]}, 3),
        ({"sequences": []}, 0),
        ({}, None),
        ({"sequences": "nope"}, None),
        ({"sequences": [{"no_steps": True}]}, None),
    ],
)
def test_step_counting_is_explicit_about_what_it_cannot_read(payload, expected) -> None:
    """Both spellings of the outer key are accepted because the published
    schema and the help centre disagree, and being wrong would refuse every
    campaign for ever. Anything else unreadable returns None."""
    assert _sequence_step_count(payload) == expected


async def test_status_is_unknown_rather_than_invented() -> None:
    """Instantly reports outcomes by webhook, not per lead. Returning SENT here
    would mark delivered a message that may never have left."""
    assert await provider(FakeClient()).get_status("lead-1") is None
