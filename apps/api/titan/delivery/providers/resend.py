"""Resend email provider.

Implements the :class:`~titan.delivery.providers.base.EmailProvider` protocol.
Nothing in this module is imported outside the outbox worker and the webhook
route; an invariant test enforces that.

Webhook verification follows the Svix scheme Resend uses: the signed content is
``{id}.{timestamp}.{body}``, the header carries one or more space-separated
``v1,<base64>`` signatures, and the secret is base64 after a ``whsec_`` prefix.
Comparison is constant-time and the timestamp is bounded to defeat replay.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
from typing import Any

import httpx

from titan.config import Settings
from titan.db.enums import MessageState
from titan.delivery.providers.base import (
    NormalizedEvent,
    OutboundEmail,
    SendErrorKind,
    SendResult,
    WebhookVerificationError,
)

#: Reject webhooks whose timestamp is further than this from now, in either
#: direction. Bounds the window in which a captured request can be replayed.
WEBHOOK_TOLERANCE_SECONDS = 300

#: Resend event type -> Titan delivery state.
_EVENT_STATE: dict[str, MessageState] = {
    "email.sent": MessageState.SENT,
    "email.delivered": MessageState.DELIVERED,
    "email.delivery_delayed": MessageState.DEFERRED,
    "email.bounced": MessageState.BOUNCED,
    "email.complained": MessageState.COMPLAINED,
    "email.opened": MessageState.OPENED,
    "email.clicked": MessageState.CLICKED,
    "email.failed": MessageState.FAILED,
}


class ResendProvider:
    name = "resend"

    def __init__(
        self,
        api_key: str,
        webhook_secret: str | None = None,
        *,
        base_url: str = "https://api.resend.com",
        timeout_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._webhook_secret = webhook_secret
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client = client

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @classmethod
    def for_webhook_verification(cls, settings: Settings) -> ResendProvider | None:
        """An adapter that can verify webhooks, or None when it cannot.

        The unwrapping happens here rather than at the call site so that raw
        credentials stay inside the provider layer -- the boundary a repository
        invariant test enforces, and the reason this exists instead of the HTTP
        route reading the secret itself.

        Returns None rather than raising when no secret is configured, because
        the caller has to tell that apart from a bad signature: one is a
        deployment that is not finished, the other is somebody knocking.
        """
        secret = settings.resend_webhook_secret
        if secret is None:
            return None
        api_key = settings.resend_api_key
        # Nothing on the verification path calls out to Resend, so an absent
        # API key is not a reason to refuse an inbound event.
        return cls(
            api_key.get_secret_value() if api_key else "",
            secret.get_secret_value(),
        )

    # ------------------------------------------------------------- sending
    async def send(self, email: OutboundEmail) -> SendResult:
        payload: dict[str, Any] = {
            "from": f"{email.from_name} <{email.from_email}>",
            "to": [email.to_email],
            "reply_to": email.reply_to,
            "subject": email.subject,
            "text": email.text_body,
        }
        if email.html_body:
            payload["html"] = email.html_body

        headers: dict[str, str] = dict(email.headers)
        if email.list_unsubscribe:
            headers["List-Unsubscribe"] = email.list_unsubscribe
        if email.list_unsubscribe_post:
            headers["List-Unsubscribe-Post"] = email.list_unsubscribe_post
        if headers:
            payload["headers"] = headers
        if email.tags:
            payload["tags"] = [{"name": k, "value": v} for k, v in email.tags.items()]

        request_headers = {}
        if email.idempotency_key:
            # Provider-side collapse of duplicate requests. Titan's own
            # dedupe_key is the primary guarantee; this is the second layer.
            request_headers["Idempotency-Key"] = email.idempotency_key

        try:
            client = await self._http()
            response = await client.post("/emails", json=payload, headers=request_headers)
        except httpx.TimeoutException as exc:
            # Ambiguous: the message may or may not have been accepted. Retrying
            # is safe only because the idempotency key collapses a duplicate.
            return SendResult(
                accepted=False,
                error_kind=SendErrorKind.TRANSIENT,
                error_detail=f"timeout: {exc}",
            )
        except httpx.HTTPError as exc:
            return SendResult(
                accepted=False,
                error_kind=SendErrorKind.TRANSIENT,
                error_detail=f"{type(exc).__name__}: {exc}",
            )

        return self._interpret(response)

    def _interpret(self, response: httpx.Response) -> SendResult:
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text[:500]}

        if response.status_code in (200, 201, 202):
            return SendResult(
                accepted=True,
                provider_message_id=str(body.get("id") or ""),
                raw=body,
            )

        detail = str(body.get("message") or body.get("error") or response.text)[:500]
        retry_after = response.headers.get("retry-after")

        if response.status_code == 429:
            kind = SendErrorKind.RATE_LIMITED
        elif response.status_code in (401, 403):
            kind = SendErrorKind.AUTH
        elif response.status_code == 422:
            lowered = detail.lower()
            if "from" in lowered or "domain" in lowered:
                kind = SendErrorKind.INVALID_SENDER
            elif "to" in lowered or "recipient" in lowered or "email" in lowered:
                kind = SendErrorKind.INVALID_RECIPIENT
            else:
                kind = SendErrorKind.PAYLOAD_REJECTED
        elif response.status_code == 400:
            kind = SendErrorKind.PAYLOAD_REJECTED
        elif 500 <= response.status_code < 600:
            kind = SendErrorKind.TRANSIENT
        else:
            kind = SendErrorKind.PROVIDER_ERROR

        return SendResult(
            accepted=False,
            error_kind=kind,
            error_detail=f"HTTP {response.status_code}: {detail}",
            retry_after_seconds=int(retry_after)
            if (retry_after or "").isdigit()
            else None,
            raw=body,
        )

    async def get_status(self, provider_message_id: str) -> MessageState | None:
        try:
            client = await self._http()
            response = await client.get(f"/emails/{provider_message_id}")
            if response.status_code != 200:
                return None
            status = str(response.json().get("last_event") or "")
        except httpx.HTTPError:
            return None
        return _EVENT_STATE.get(f"email.{status}") or _EVENT_STATE.get(status)

    # ------------------------------------------------------------ webhooks
    def verify_webhook(self, *, payload: bytes, headers: dict[str, str]) -> None:
        """Verify a Svix-style signature. Raises on any failure.

        Fails closed on a missing secret: an unverifiable webhook must never be
        treated as authentic, because a forged 'delivered' event would mask a
        bounce and a forged 'complained' event could suppress an address.
        """
        if not self._webhook_secret:
            raise WebhookVerificationError(
                "no webhook secret configured; refusing to trust the payload"
            )

        lowered = {k.lower(): v for k, v in headers.items()}
        msg_id = lowered.get("svix-id") or lowered.get("webhook-id")
        timestamp = lowered.get("svix-timestamp") or lowered.get("webhook-timestamp")
        signature_header = lowered.get("svix-signature") or lowered.get(
            "webhook-signature"
        )

        if not (msg_id and timestamp and signature_header):
            raise WebhookVerificationError("missing signature headers")

        try:
            sent_at = dt.datetime.fromtimestamp(int(timestamp), tz=dt.UTC)
        except (ValueError, OverflowError, OSError) as exc:
            raise WebhookVerificationError("malformed timestamp") from exc

        drift = abs((dt.datetime.now(dt.UTC) - sent_at).total_seconds())
        if drift > WEBHOOK_TOLERANCE_SECONDS:
            raise WebhookVerificationError(
                f"timestamp is {drift:.0f}s from now; outside the replay window"
            )

        secret = self._webhook_secret
        if secret.startswith("whsec_"):
            secret = secret[len("whsec_") :]
        try:
            key = base64.b64decode(secret)
        except Exception as exc:
            raise WebhookVerificationError("secret is not valid base64") from exc

        signed = b".".join([msg_id.encode(), timestamp.encode(), payload])
        expected = base64.b64encode(
            hmac.new(key, signed, hashlib.sha256).digest()
        ).decode()

        # The header may carry several versioned signatures during rotation.
        for candidate in signature_header.split():
            version, _, value = candidate.partition(",")
            if version != "v1":
                continue
            if hmac.compare_digest(value, expected):
                return
        raise WebhookVerificationError("no signature matched")

    def normalize_webhook(self, payload: dict[str, Any]) -> NormalizedEvent | None:
        event_type = str(payload.get("type") or "")
        state = _EVENT_STATE.get(event_type)
        if state is None:
            return None

        data = payload.get("data") or {}
        occurred_raw = payload.get("created_at") or data.get("created_at")
        occurred_at = _parse_time(occurred_raw)

        recipients = data.get("to")
        recipient = None
        if isinstance(recipients, list) and recipients:
            recipient = str(recipients[0])
        elif isinstance(recipients, str):
            recipient = recipients

        bounce = data.get("bounce") or {}
        # Only a hard bounce suppresses. A soft bounce (mailbox full, temporary
        # rejection) must not permanently remove a legitimate recipient.
        is_hard = str(bounce.get("type") or "").lower() in {"hard", "permanent"}

        # A provider event id is required for deduplication (invariant 12).
        # Falling back to a content hash keeps duplicate collapse working even
        # if the provider omits one, rather than silently allowing duplicates.
        provider_event_id = str(
            payload.get("id")
            or (
                data.get("email_id", "")
                and f"{event_type}:{data.get('email_id')}:{occurred_at.isoformat()}"
            )
            or hashlib.sha256(
                json.dumps(payload, sort_keys=True, default=str).encode()
            ).hexdigest()
        )

        return NormalizedEvent(
            provider=self.name,
            provider_event_id=provider_event_id,
            event_type=event_type,
            provider_message_id=str(data.get("email_id") or "") or None,
            state=state,
            occurred_at=occurred_at,
            recipient=recipient,
            is_hard_bounce=is_hard,
            raw=payload,
        )

    async def health_check(self) -> tuple[bool, str]:
        try:
            client = await self._http()
            response = await client.get("/domains")
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if response.status_code == 200:
            return True, "ok"
        if response.status_code in (401, 403):
            return False, "authentication failed: check TITAN_RESEND_API_KEY"
        return False, f"HTTP {response.status_code}"


def _parse_time(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
        except ValueError:
            pass
    return dt.datetime.now(dt.UTC)


__all__ = ["WEBHOOK_TOLERANCE_SECONDS", "ResendProvider"]
