"""Mock email provider.

The default in every environment where ``TITAN_EMAIL_PROVIDER`` is not
explicitly set to a real provider -- which is what makes "production sending
disabled by default" true even if every other gate were misconfigured
(invariant 21).

It records every send so tests can assert exactly-once delivery, and it can be
told to fail in specific ways so retry, deferral, and suppression paths are
exercised against something that behaves like a provider rather than a stub
that always succeeds.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from titan.db.enums import MessageState
from titan.delivery.providers.base import (
    NormalizedEvent,
    OutboundEmail,
    SendErrorKind,
    SendResult,
    WebhookVerificationError,
)


@dataclass
class RecordedSend:
    email: OutboundEmail
    at: dt.datetime
    attempt: int


@dataclass
class MockEmailProvider:
    """An in-memory provider that behaves like a real one.

    ``fail_times`` makes the next N sends fail transiently, so a test can prove
    a retried message is delivered exactly once rather than twice.
    """

    name: str = "mock"
    sends: list[RecordedSend] = field(default_factory=list)
    #: idempotency_key -> provider_message_id, mirroring provider-side collapse.
    _by_idempotency_key: dict[str, str] = field(default_factory=dict)
    fail_times: int = 0
    fail_kind: SendErrorKind = SendErrorKind.TRANSIENT
    permanent_failure_recipients: set[str] = field(default_factory=set)
    #: Raise instead of returning, to simulate a crash mid-send.
    raise_on_send: bool = False
    _attempts: int = 0

    async def send(self, email: OutboundEmail) -> SendResult:
        self._attempts += 1

        if self.raise_on_send:
            raise RuntimeError("simulated worker crash during provider call")

        # Provider-side idempotency: a repeat of the same key returns the
        # original id and does NOT deliver a second message.
        if email.idempotency_key and email.idempotency_key in self._by_idempotency_key:
            return SendResult(
                accepted=True,
                provider_message_id=self._by_idempotency_key[email.idempotency_key],
                raw={"deduplicated": True},
            )

        if email.to_email.lower() in self.permanent_failure_recipients:
            return SendResult(
                accepted=False,
                error_kind=SendErrorKind.INVALID_RECIPIENT,
                error_detail="mock: recipient rejected",
            )

        if self.fail_times > 0:
            self.fail_times -= 1
            return SendResult(
                accepted=False,
                error_kind=self.fail_kind,
                error_detail=f"mock: injected {self.fail_kind.value} failure",
                retry_after_seconds=1
                if self.fail_kind is SendErrorKind.RATE_LIMITED
                else None,
            )

        message_id = (
            "mock-"
            + hashlib.sha256(
                f"{email.to_email}|{email.subject}|{email.idempotency_key}".encode()
            ).hexdigest()[:24]
        )
        if email.idempotency_key:
            self._by_idempotency_key[email.idempotency_key] = message_id
        self.sends.append(
            RecordedSend(email=email, at=dt.datetime.now(dt.UTC), attempt=self._attempts)
        )
        return SendResult(accepted=True, provider_message_id=message_id)

    async def get_status(self, provider_message_id: str) -> MessageState | None:
        return (
            MessageState.SENT
            if any(
                s.email.idempotency_key
                and self._by_idempotency_key.get(s.email.idempotency_key)
                == provider_message_id
                for s in self.sends
            )
            else None
        )

    def verify_webhook(self, *, payload: bytes, headers: dict[str, str]) -> None:
        """Verify a simple HMAC so forgery tests exercise a real comparison."""
        import hmac

        secret = headers.get("x-mock-secret", "")
        signature = headers.get("x-mock-signature", "")
        if not secret or not signature:
            raise WebhookVerificationError("missing mock signature headers")
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise WebhookVerificationError("mock signature mismatch")

    def normalize_webhook(self, payload: dict[str, Any]) -> NormalizedEvent | None:
        state_name = str(payload.get("state") or "")
        try:
            state = MessageState(state_name)
        except ValueError:
            return None
        occurred = payload.get("occurred_at")
        occurred_at = (
            dt.datetime.fromisoformat(str(occurred))
            if occurred
            else dt.datetime.now(dt.UTC)
        )
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=dt.UTC)
        return NormalizedEvent(
            provider=self.name,
            provider_event_id=str(
                payload.get("event_id")
                or hashlib.sha256(
                    json.dumps(payload, sort_keys=True, default=str).encode()
                ).hexdigest()
            ),
            event_type=state_name,
            provider_message_id=str(payload.get("provider_message_id") or "") or None,
            state=state,
            occurred_at=occurred_at,
            recipient=payload.get("recipient"),
            is_hard_bounce=bool(payload.get("hard_bounce")),
            raw=payload,
        )

    async def health_check(self) -> tuple[bool, str]:
        return True, "mock provider is always healthy and never delivers real mail"

    # ------------------------------------------------------- test helpers
    @property
    def delivered_count(self) -> int:
        return len(self.sends)

    def recipients(self) -> list[str]:
        return [s.email.to_email for s in self.sends]

    def dedupe_keys(self) -> list[str]:
        return [s.email.idempotency_key for s in self.sends]


__all__ = ["MockEmailProvider", "RecordedSend"]
