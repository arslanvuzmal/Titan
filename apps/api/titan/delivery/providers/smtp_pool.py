"""Sending as whichever mailbox the pool chose, authenticated as that mailbox.

:mod:`titan.delivery.providers.smtp` speaks to one server as one user. That is
the right shape for one mailbox and the wrong shape for a pool: the sender pool
picks a different identity per message, and a single-credential adapter puts
that identity in ``From`` while authenticating as somebody else. See
:mod:`titan.delivery.mailboxes` for why that fails DMARC rather than merely
looking untidy.

This adapter holds one :class:`~titan.delivery.providers.smtp.SmtpProvider` per
mailbox and routes on ``from_email``. Nothing else about the send path changes:
the same rendering, the same headers, the same deterministic ``Message-ID``,
the same error taxonomy.

**Routing is by exact address, and a miss is refused.** If the pool picks a
mailbox this file has no credential for, the message comes back as
``INVALID_SENDER`` -- a configuration failure, so the recipient is not
suppressed and the row is not retried into an infinite loop. The alternative,
falling back to whichever mailbox happens to be configured, sends a stranger a
message from an address that did not authenticate it. That is worse than not
sending.

**The Message-ID is stamped with the sending domain, not the relay's.** A
message from ``outreach@arslanvuzmallone.com`` carrying a
``<...@smtp.spacemail.com>`` Message-ID is a small, free deliverability tax;
receivers read the mismatch as bulk-sender machinery. The domain of the From
address is the honest answer and costs nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from titan.db.enums import MessageState
from titan.delivery.mailboxes import MailboxAccount, MailboxRegistry
from titan.delivery.providers.base import (
    NormalizedEvent,
    OutboundEmail,
    SendErrorKind,
    SendResult,
    WebhookVerificationError,
)
from titan.delivery.providers.smtp import SmtpProvider

logger = logging.getLogger(__name__)


def _domain_of(address: str) -> str | None:
    _, _, domain = (address or "").partition("@")
    return domain.strip().lower() or None


class SmtpPoolProvider:
    """One SMTP connection recipe per sending address."""

    name = "smtp_pool"

    def __init__(
        self, registry: MailboxRegistry, *, timeout_seconds: float = 30.0
    ) -> None:
        if not registry:
            raise ValueError(
                "the SMTP pool needs at least one mailbox; an empty pool would "
                "refuse every message it was handed"
            )
        self._registry = registry
        self._timeout = timeout_seconds
        self._providers: dict[str, SmtpProvider] = {
            account.from_email.lower(): self._build(account)
            for account in registry.accounts()
        }

    def _build(self, account: MailboxAccount) -> SmtpProvider:
        return SmtpProvider(
            account.smtp.host,
            account.smtp.port,
            username=account.smtp.username,
            password=account.smtp.password,
            security=account.smtp.security,
            timeout_seconds=self._timeout,
            message_id_domain=_domain_of(account.from_email) or account.smtp.host,
        )

    # ----------------------------------------------------------------- send
    def _route(self, from_email: str) -> SmtpProvider | None:
        return self._providers.get((from_email or "").strip().lower())

    async def send(self, email: OutboundEmail) -> SendResult:
        provider = self._route(email.from_email)
        if provider is None:
            # Not a mystery worth debugging later: say which address, and which
            # ones this process could have sent as.
            detail = (
                f"no SMTP credential is configured for {email.from_email!r}; "
                f"this worker can authenticate as {', '.join(self._registry.addresses())}"
            )
            logger.error(
                "outbound refused: unknown sending mailbox",
                extra={
                    "from_email": email.from_email,
                    "configured": self._registry.addresses(),
                },
            )
            return SendResult(
                accepted=False,
                error_kind=SendErrorKind.INVALID_SENDER,
                error_detail=detail,
            )
        return await provider.send(email)

    # --------------------------------------------------------------- status
    async def get_status(self, provider_message_id: str) -> MessageState | None:
        """SMTP reports nothing after handoff, however many mailboxes there are."""
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
        """Probe every mailbox, and report each one.

        Aggregated to a single boolean the worker can act on, but the detail
        names the mailboxes individually: a pool reported as simply "unhealthy"
        tells an operator to check three mailboxes when one is broken.
        """
        addresses = self._registry.addresses()
        results = await asyncio.gather(
            *(self._providers[address.lower()].health_check() for address in addresses)
        )
        lines = [
            f"{address}: {'ok' if ok else 'FAILED'} -- {detail}"
            for address, (ok, detail) in zip(addresses, results, strict=True)
        ]
        healthy = [
            address for address, (ok, _) in zip(addresses, results, strict=True) if ok
        ]
        # Any working mailbox means the pool can send something. The pool exists
        # so that one broken mailbox costs its share of the volume rather than
        # all of it, and reporting the whole pool down would undo that.
        return bool(healthy), (
            f"{len(healthy)}/{len(addresses)} mailboxes usable; " + "; ".join(lines)
        )


__all__ = ["SmtpPoolProvider"]
