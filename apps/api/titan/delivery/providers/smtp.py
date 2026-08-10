"""SMTP delivery adapter.

Two uses, one code path:

* **Mailpit**, a local capture server that accepts everything and delivers
  nothing. Pointing Titan at it renders real messages through the real send
  path -- headers, footers, unsubscribe, the lot -- with no possibility of mail
  reaching a stranger. This is how a message format is reviewed before any
  human sees it.
* **A real mailbox** (spacemail, Google Workspace, anything speaking SMTP).

What SMTP cannot give, stated plainly rather than faked:

* **No idempotency.** Resend and Smartlead both collapse a duplicated request;
  SMTP has no such concept. The ``Message-ID`` is derived deterministically
  from Titan's idempotency key so a duplicate is at least *identifiable* after
  the fact, and receiving servers commonly suppress a repeated Message-ID --
  but that is a convention, not a guarantee. The real protection remains the
  outbox lease and the ``SENT`` status transition.
* **No delivery status and no webhooks.** ``get_status`` returns None and
  ``verify_webhook`` refuses. Guessing a state would put fiction into the
  message state machine. A bounce instead arrives as a separate email to the
  envelope sender, and is picked up out of band by
  :mod:`titan.delivery.reply_collector` -- which reads the mailbox, matches the
  report to the message that provoked it via the deterministic ``Message-ID``
  built below, and suppresses the address that actually failed.

Only the outbox worker may import this module; an invariant test enforces it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate
from typing import Any

from titan.db.enums import MessageState
from titan.delivery.providers.base import (
    NormalizedEvent,
    OutboundEmail,
    SendErrorKind,
    SendResult,
    WebhookVerificationError,
)

logger = logging.getLogger(__name__)

#: SMTP reply codes that mean this recipient will never accept mail. Retrying
#: them damages sender reputation, so they suppress instead.
PERMANENT_REPLY_CODES = frozenset({550, 551, 553, 554})
#: Codes that mean "not now" -- greylisting, mailbox full, rate limited.
TRANSIENT_REPLY_CODES = frozenset({421, 450, 451, 452, 471})


class SmtpProvider:
    """Sends one message over SMTP."""

    name = "smtp"

    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str | None = None,
        password: str | None = None,
        security: str = "ssl",
        timeout_seconds: float = 30.0,
        message_id_domain: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._security = security
        self._timeout = timeout_seconds
        self._message_id_domain = message_id_domain or host

    # ------------------------------------------------------------- rendering
    def _build(self, email: OutboundEmail) -> EmailMessage:
        message = EmailMessage()
        message["From"] = f"{email.from_name} <{email.from_email}>"
        message["To"] = email.to_email
        message["Subject"] = email.subject
        if email.reply_to:
            message["Reply-To"] = email.reply_to
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = self._message_id(email)

        # RFC 8058: both headers, or Gmail shows no one-click unsubscribe.
        if email.list_unsubscribe:
            message["List-Unsubscribe"] = email.list_unsubscribe
        if email.list_unsubscribe_post:
            message["List-Unsubscribe-Post"] = email.list_unsubscribe_post
        for key, value in (email.headers or {}).items():
            if key.lower() not in {k.lower() for k in message.keys()}:
                message[key] = value

        message.set_content(email.text_body)
        if email.html_body:
            message.add_alternative(email.html_body, subtype="html")
        return message

    def _message_id(self, email: OutboundEmail) -> str:
        """Deterministic from the idempotency key.

        A retry produces byte-identical headers, so a duplicate is traceable
        and most receivers will drop the second copy.
        """
        digest = hashlib.sha256(
            (email.idempotency_key or f"{email.to_email}:{email.subject}").encode()
        ).hexdigest()[:32]
        return f"<{digest}@{self._message_id_domain}>"

    # ----------------------------------------------------------------- send
    def _send_blocking(self, email: OutboundEmail) -> tuple[bool, str | None, int | None]:
        message = self._build(email)
        try:
            if self._security == "ssl":
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    self._host,
                    self._port,
                    timeout=self._timeout,
                    context=ssl.create_default_context(),
                )
            else:
                client = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
                if self._security == "starttls":
                    client.starttls(context=ssl.create_default_context())

            with client:
                if self._username and self._password:
                    client.login(self._username, self._password)
                client.send_message(message)
            return True, None, None
        except smtplib.SMTPRecipientsRefused as exc:
            code = next(iter(exc.recipients.values()))[0] if exc.recipients else 550
            return False, f"recipient refused: {exc}", int(code)
        except smtplib.SMTPResponseException as exc:
            return False, f"{exc.smtp_code} {exc.smtp_error!r}", int(exc.smtp_code)
        except smtplib.SMTPException as exc:
            return False, f"{type(exc).__name__}: {exc}", None
        except OSError as exc:
            # Connection refused, DNS failure, TLS handshake -- all retryable.
            return False, f"{type(exc).__name__}: {exc}", None

    async def send(self, email: OutboundEmail) -> SendResult:
        # smtplib is synchronous; run it off the event loop so one slow server
        # cannot stall the whole outbox worker.
        ok, detail, code = await asyncio.to_thread(self._send_blocking, email)

        if ok:
            return SendResult(
                accepted=True, provider_message_id=self._message_id(email).strip("<>")
            )

        if code in PERMANENT_REPLY_CODES:
            return SendResult(
                accepted=False,
                error_kind=SendErrorKind.INVALID_RECIPIENT,
                error_detail=detail,
            )
        if code in TRANSIENT_REPLY_CODES:
            return SendResult(
                accepted=False, error_kind=SendErrorKind.TRANSIENT, error_detail=detail
            )
        if code in (530, 535, 538):
            # Authentication problem: ours, not the recipient's. Must not suppress.
            return SendResult(
                accepted=False, error_kind=SendErrorKind.AUTH, error_detail=detail
            )
        return SendResult(
            accepted=False, error_kind=SendErrorKind.TRANSIENT, error_detail=detail
        )

    # --------------------------------------------------------------- status
    async def get_status(self, provider_message_id: str) -> MessageState | None:
        """SMTP reports nothing after handoff. None, rather than a guess."""
        return None

    # -------------------------------------------------------------- webhooks
    def verify_webhook(self, *, payload: bytes, headers: dict[str, str]) -> None:
        raise WebhookVerificationError(
            "SMTP has no webhooks; bounces arrive as mail to the envelope sender "
            "and are ingested by the reply collector, not through this path"
        )

    def normalize_webhook(self, payload: dict[str, Any]) -> NormalizedEvent | None:
        return None

    # ---------------------------------------------------------------- health
    async def health_check(self) -> tuple[bool, str]:
        def probe() -> tuple[bool, str]:
            try:
                if self._security == "ssl":
                    client: smtplib.SMTP = smtplib.SMTP_SSL(
                        self._host,
                        self._port,
                        timeout=self._timeout,
                        context=ssl.create_default_context(),
                    )
                else:
                    client = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
                    if self._security == "starttls":
                        client.starttls(context=ssl.create_default_context())
                with client:
                    client.ehlo()
                    if self._username and self._password:
                        client.login(self._username, self._password)
                        return True, f"smtp {self._host}:{self._port} authenticated"
                    return True, f"smtp {self._host}:{self._port} reachable (no auth)"
            except Exception as exc:
                return False, f"{type(exc).__name__}: {exc}"

        return await asyncio.to_thread(probe)


__all__ = ["PERMANENT_REPLY_CODES", "TRANSIENT_REPLY_CODES", "SmtpProvider"]
