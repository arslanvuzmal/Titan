"""Email provider interface.

Titan's domain never imports a concrete provider. The outbox worker resolves an
implementation of this protocol at runtime, which is what keeps Resend
replaceable and what lets the whole delivery path be tested against a mock that
records exactly what it was asked to send.

**Only the outbox worker may hold one of these.** An invariant test AST-scans
the repository and fails if any other module imports a provider implementation
(gap analysis C-01, invariant 1 and 4).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from titan.db.enums import MessageState


class SendErrorKind(StrEnum):
    """Normalized provider failure taxonomy.

    The distinction that matters operationally is TRANSIENT vs PERMANENT:
    transient failures are retried with backoff, permanent ones suppress the
    recipient and stop. Guessing wrong in either direction is costly -- retrying
    a hard bounce damages reputation; suppressing on a timeout loses a lead.
    """

    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    AUTH = "authentication"
    INVALID_RECIPIENT = "invalid_recipient"
    INVALID_SENDER = "invalid_sender"
    SUPPRESSED_BY_PROVIDER = "suppressed_by_provider"
    PAYLOAD_REJECTED = "payload_rejected"
    PROVIDER_ERROR = "provider_error"
    UNKNOWN = "unknown"


#: Failures after which retrying is pointless and the address must be suppressed.
PERMANENT_ERROR_KINDS = frozenset(
    {
        SendErrorKind.INVALID_RECIPIENT,
        SendErrorKind.SUPPRESSED_BY_PROVIDER,
    }
)

#: Failures that indicate a configuration problem, not a recipient problem.
#: Retrying is pointless but the recipient must NOT be suppressed.
CONFIGURATION_ERROR_KINDS = frozenset(
    {
        SendErrorKind.AUTH,
        SendErrorKind.INVALID_SENDER,
        SendErrorKind.PAYLOAD_REJECTED,
    }
)


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    """Everything the provider needs. Rendered before the worker leases the row."""

    to_email: str
    from_email: str
    from_name: str
    reply_to: str
    subject: str
    text_body: str
    html_body: str | None = None
    #: Sent to the provider so a network-level retry collapses provider-side.
    idempotency_key: str = ""
    #: RFC 8058 one-click unsubscribe, when the sender identity supports it.
    list_unsubscribe: str | None = None
    list_unsubscribe_post: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SendResult:
    accepted: bool
    provider_message_id: str | None = None
    error_kind: SendErrorKind | None = None
    error_detail: str | None = None
    #: Provider hint for when to retry, honoured over Titan's own backoff.
    retry_after_seconds: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_permanent_failure(self) -> bool:
        return self.error_kind in PERMANENT_ERROR_KINDS

    @property
    def is_configuration_failure(self) -> bool:
        return self.error_kind in CONFIGURATION_ERROR_KINDS


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    """A provider webhook, reduced to what Titan's state machine needs."""

    provider: str
    provider_event_id: str
    event_type: str
    provider_message_id: str | None
    state: MessageState | None
    occurred_at: dt.datetime
    recipient: str | None = None
    #: Set for bounces/complaints so the worker knows whether to suppress.
    is_hard_bounce: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class WebhookVerificationError(RuntimeError):
    """Raised when a webhook signature does not verify. Never retried."""


@runtime_checkable
class EmailProvider(Protocol):
    """The complete surface Titan depends on (mission section 15.1)."""

    name: str

    async def send(self, email: OutboundEmail) -> SendResult: ...

    async def get_status(self, provider_message_id: str) -> MessageState | None: ...

    def verify_webhook(self, *, payload: bytes, headers: dict[str, str]) -> None: ...

    def normalize_webhook(self, payload: dict[str, Any]) -> NormalizedEvent | None: ...

    async def health_check(self) -> tuple[bool, str]: ...


__all__ = [
    "CONFIGURATION_ERROR_KINDS",
    "PERMANENT_ERROR_KINDS",
    "EmailProvider",
    "NormalizedEvent",
    "OutboundEmail",
    "SendErrorKind",
    "SendResult",
    "WebhookVerificationError",
]
