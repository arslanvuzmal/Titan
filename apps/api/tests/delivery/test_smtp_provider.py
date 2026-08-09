"""SMTP adapter tests.

Hermetic: the blocking send is replaced, so nothing opens a socket. These
verify the message Titan hands to SMTP and the mapping from reply codes onto
Titan's error taxonomy -- the mapping matters because guessing wrong either
suppresses an innocent recipient or retries a hard bounce.
"""

from __future__ import annotations

import pytest
from titan.delivery.providers.base import OutboundEmail, SendErrorKind
from titan.delivery.providers.smtp import SmtpProvider


def email(**overrides) -> OutboundEmail:
    base = {
        "to_email": "sam@fixture-business.test",
        "from_email": "outreach@arslanvuzmallone.com",
        "from_name": "Arslan Vuzmal Lone",
        "reply_to": "outreach@arslanvuzmallone.com",
        "subject": "A broken button on your booking page",
        "text_body": "Hello.\nA second line.",
        "idempotency_key": "idem-lead-1-step-0",
        "list_unsubscribe": "<https://arslanvuzmallone.dev/unsubscribe>",
        "list_unsubscribe_post": "List-Unsubscribe=One-Click",
    }
    base.update(overrides)
    return OutboundEmail(**base)


def provider(**kw) -> SmtpProvider:
    return SmtpProvider("localhost", 1025, security="none", **kw)


def test_the_message_carries_the_rfc8058_unsubscribe_headers() -> None:
    """Without both headers Gmail renders no one-click unsubscribe button."""
    built = provider()._build(email())

    assert built["List-Unsubscribe"] == "<https://arslanvuzmallone.dev/unsubscribe>"
    assert built["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert built["To"] == "sam@fixture-business.test"
    assert built["Reply-To"] == "outreach@arslanvuzmallone.com"
    assert "Arslan Vuzmal Lone" in built["From"]


def test_the_message_id_is_derived_from_the_idempotency_key() -> None:
    """SMTP cannot dedupe, so a retry must at least be identifiable.

    Byte-identical headers give the receiving server the chance to collapse a
    duplicate, and give an operator a way to find both copies afterwards.
    """
    first = provider()._build(email())["Message-ID"]
    again = provider()._build(email())["Message-ID"]
    different = provider()._build(email(idempotency_key="idem-lead-2-step-0"))

    assert first == again
    assert different["Message-ID"] != first


def test_a_plain_text_body_survives_intact() -> None:
    """The validator approved this exact text; SMTP must not reshape it."""
    built = provider()._build(email())
    body = built.get_body(preferencelist=("plain",)).get_content()

    assert "Hello." in body
    assert "A second line." in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,expected_kind,permanent,configuration",
    [
        (550, SendErrorKind.INVALID_RECIPIENT, True, False),
        (554, SendErrorKind.INVALID_RECIPIENT, True, False),
        (421, SendErrorKind.TRANSIENT, False, False),
        (451, SendErrorKind.TRANSIENT, False, False),
        (535, SendErrorKind.AUTH, False, True),
        (None, SendErrorKind.TRANSIENT, False, False),
    ],
)
async def test_reply_codes_map_onto_the_error_taxonomy(
    monkeypatch, code, expected_kind, permanent, configuration
) -> None:
    """Retrying a hard bounce burns reputation; suppressing on a timeout loses
    a lead. The mapping is the whole point of this adapter."""
    p = provider()
    monkeypatch.setattr(p, "_send_blocking", lambda e: (False, f"simulated {code}", code))

    result = await p.send(email())

    assert result.accepted is False
    assert result.error_kind is expected_kind
    assert result.is_permanent_failure is permanent
    assert result.is_configuration_failure is configuration


@pytest.mark.asyncio
async def test_a_successful_send_reports_the_message_id(monkeypatch) -> None:
    p = provider()
    monkeypatch.setattr(p, "_send_blocking", lambda e: (True, None, None))

    result = await p.send(email())

    assert result.accepted is True
    assert result.provider_message_id
    assert "@" in result.provider_message_id


@pytest.mark.asyncio
async def test_status_and_webhooks_refuse_rather_than_guess() -> None:
    """SMTP reports nothing after handoff. Inventing a state would put fiction
    into the message state machine."""
    from titan.delivery.providers.base import WebhookVerificationError

    p = provider()
    assert await p.get_status("anything") is None
    assert p.normalize_webhook({"event": "delivered"}) is None
    with pytest.raises(WebhookVerificationError):
        p.verify_webhook(payload=b"{}", headers={})


def test_cleartext_smtp_is_refused_for_a_remote_host() -> None:
    """A mailbox password in clear on the wire is not a warning-level problem."""
    from pydantic import ValidationError
    from titan.config import Settings

    # Loopback capture server: allowed, nothing leaves the machine.
    Settings(environment="test", smtp_host="localhost", smtp_security="none")

    with pytest.raises(ValidationError, match="loopback"):
        Settings(environment="test", smtp_host="mail.spacemail.com", smtp_security="none")
