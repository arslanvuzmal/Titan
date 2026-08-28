"""The pool adapter: one credential per sending address.

Hermetic. The blocking send inside each :class:`SmtpProvider` is replaced, so
nothing opens a socket; what is recorded is which mailbox's connection recipe
a message was routed to.
"""

from __future__ import annotations

import pytest
from titan.delivery.mailboxes import parse_mailboxes
from titan.delivery.providers.base import OutboundEmail, SendErrorKind
from titan.delivery.providers.smtp_pool import SmtpPoolProvider

OUTREACH = "outreach@arslanvuzmallone.com"
SALES = "sales@arslanvuzmallone.com"


def entry(address: str, *, host: str) -> dict:
    return {
        "from_email": address,
        "smtp": {
            "host": host,
            "port": 465,
            "security": "ssl",
            "username": address,
            "password": f"not-a-real-password-for-{address}",
        },
    }


def pool(*addresses: tuple[str, str]) -> SmtpPoolProvider:
    return SmtpPoolProvider(
        parse_mailboxes(
            {"mailboxes": [entry(address, host=host) for address, host in addresses]}
        )
    )


def email(**overrides) -> OutboundEmail:
    base = {
        "to_email": "sam@fixture-business.test",
        "from_email": OUTREACH,
        "from_name": "Arslan Vuzmal Lone",
        "reply_to": OUTREACH,
        "subject": "A broken button on your booking page",
        "text_body": "Hello.",
        "idempotency_key": "idem-lead-1-step-0",
    }
    base.update(overrides)
    return OutboundEmail(**base)


@pytest.fixture
def recorder(monkeypatch):
    """Capture which SMTP recipe each send used, without opening a socket."""
    calls: list[tuple[str, str, str]] = []

    def fake_send(self, message):
        calls.append((self._host, self._username, self._password))
        return True, None, None

    monkeypatch.setattr(
        "titan.delivery.providers.smtp.SmtpProvider._send_blocking", fake_send
    )
    return calls


# ------------------------------------------------------------------ routing
async def test_a_message_authenticates_as_the_address_it_is_sent_from(
    recorder,
) -> None:
    """The whole point. The pool picks the identity; the credential follows it."""
    provider = pool((OUTREACH, "smtp-a.test"), (SALES, "smtp-b.test"))

    await provider.send(email(from_email=SALES))

    assert recorder == [("smtp-b.test", SALES, f"not-a-real-password-for-{SALES}")]


async def test_each_mailbox_keeps_its_own_credential(recorder) -> None:
    provider = pool((OUTREACH, "smtp-a.test"), (SALES, "smtp-b.test"))

    await provider.send(email(from_email=OUTREACH))
    await provider.send(email(from_email=SALES))

    assert [username for _, username, _ in recorder] == [OUTREACH, SALES]


async def test_routing_ignores_the_case_the_address_was_stored_in(recorder) -> None:
    provider = pool((OUTREACH, "smtp-a.test"))

    result = await provider.send(email(from_email="Outreach@ArslanVuzmalLone.com"))

    assert result.accepted
    assert len(recorder) == 1


# ------------------------------------------------------------------ refusal
async def test_an_unknown_sender_is_refused_rather_than_sent_as_somebody_else(
    recorder,
) -> None:
    """Falling back to a configured mailbox would forge the From address."""
    provider = pool((OUTREACH, "smtp-a.test"))

    result = await provider.send(email(from_email="projects@arslanvuzmallone.com"))

    assert not result.accepted
    assert recorder == []


async def test_an_unknown_sender_does_not_suppress_the_recipient() -> None:
    """Our configuration is wrong, not their address.

    INVALID_SENDER is in CONFIGURATION_ERROR_KINDS, so the outbox stops
    retrying without adding the recipient to the suppression list.
    """
    provider = pool((OUTREACH, "smtp-a.test"))

    result = await provider.send(email(from_email="projects@arslanvuzmallone.com"))

    assert result.error_kind is SendErrorKind.INVALID_SENDER
    assert result.is_configuration_failure
    assert not result.is_permanent_failure


async def test_the_refusal_names_what_it_could_have_sent_as() -> None:
    """So the fix is the next thing an operator reads, not a search."""
    provider = pool((OUTREACH, "smtp-a.test"), (SALES, "smtp-b.test"))

    result = await provider.send(email(from_email="projects@arslanvuzmallone.com"))

    assert "projects@arslanvuzmallone.com" in (result.error_detail or "")
    assert OUTREACH in (result.error_detail or "")
    assert SALES in (result.error_detail or "")


def test_an_empty_pool_is_refused_at_construction() -> None:
    """It would accept the worker's startup and then refuse every message."""
    with pytest.raises(ValueError, match="at least one mailbox"):
        SmtpPoolProvider(parse_mailboxes({"mailboxes": []}))


# ------------------------------------------------------------------ headers
async def test_the_message_id_carries_the_sending_domain_not_the_relay(
    recorder,
) -> None:
    """A <...@smtp.provider> Message-ID on mail from your own domain reads as
    bulk-sender machinery to a receiving server. The From domain is free."""
    provider = pool((OUTREACH, "smtp.spacemail.test"))

    result = await provider.send(email(from_email=OUTREACH))

    assert result.provider_message_id is not None
    assert result.provider_message_id.endswith("@arslanvuzmallone.com")


# ------------------------------------------------------------------- health
async def test_health_names_each_mailbox_separately(monkeypatch) -> None:
    """'the pool is unhealthy' sends an operator to check three mailboxes."""

    async def fake_health(self):
        ok = self._host == "smtp-a.test"
        return ok, "authenticated" if ok else "535 bad credentials"

    monkeypatch.setattr(
        "titan.delivery.providers.smtp.SmtpProvider.health_check", fake_health
    )
    provider = pool((OUTREACH, "smtp-a.test"), (SALES, "smtp-b.test"))

    ok, detail = await provider.health_check()

    # One working mailbox means the pool can still send: that is what a pool is
    # for. The detail is where the broken one is named.
    assert ok
    assert "1/2 mailboxes usable" in detail
    assert f"{SALES}: FAILED" in detail
    assert f"{OUTREACH}: ok" in detail


async def test_a_pool_with_no_working_mailbox_reports_unhealthy(monkeypatch) -> None:
    async def fake_health(self):
        return False, "535 bad credentials"

    monkeypatch.setattr(
        "titan.delivery.providers.smtp.SmtpProvider.health_check", fake_health
    )
    provider = pool((OUTREACH, "smtp-a.test"), (SALES, "smtp-b.test"))

    ok, detail = await provider.health_check()

    assert not ok
    assert "0/2 mailboxes usable" in detail
