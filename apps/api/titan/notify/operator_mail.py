"""The one path that may send mail without going through the outbox.

Invariant 1 says every send routes through ``titan.delivery.outbox_worker``,
and it exists because the pre-0.2 code had a model-invocable tool that POSTed
straight to SendGrid. The daily operator report genuinely cannot go that way:

* it would consume outreach quota, so a report about a spent quota would be
  competing with the sending it describes;
* it would enter the bounce and delivery statistics, distorting the very
  numbers it exists to report;
* and it would be subject to mailbox health, so a report explaining that every
  mailbox is blocked would be held back by every mailbox being blocked.

So this module is an allowlisted exception -- and the exception is made as
narrow as it can be made rather than granted to whatever wants it.

**It cannot send to anybody but the operator.** There is no recipient
parameter. The address is read from ``settings.operator_email``, which is the
human who runs this system and is deliberately not a sender identity. A caller
that wanted to mail a prospect through here has no way to say so, which is a
stronger guarantee than an allowlist entry on a module that takes an address.

**It writes nothing.** No outbox row, no message, no quota counter. Nothing
downstream can mistake this for outreach because there is no trace of it in
any table that outreach is measured from.
"""

from __future__ import annotations

import logging

from titan.config import get_settings
from titan.delivery.providers.base import OutboundEmail
from titan.delivery.providers.smtp_pool import SmtpPoolProvider

logger = logging.getLogger(__name__)


class NoOperatorAddress(RuntimeError):
    """Raised when there is nobody configured to report to."""


async def mail_the_operator(*, subject: str, body: str) -> str:
    """Send one plain-text message to the configured operator. Returns the address.

    Deliberately no recipient argument. See the module docstring: the absence
    is the guarantee.
    """
    settings = get_settings()
    to = settings.operator_email
    if not to:
        raise NoOperatorAddress(
            "TITAN_OPERATOR_EMAIL is not set; there is nobody to report to"
        )
    if not settings.mailbox_file:
        raise RuntimeError("TITAN_MAILBOX_FILE is not set; cannot send the report")

    from titan.delivery.mailboxes import load_mailboxes

    provider = SmtpPoolProvider(
        load_mailboxes(settings.mailbox_file),
        timeout_seconds=float(settings.smtp_timeout_seconds),
    )
    # A named mailbox, never the pool's choice. The pool answers "which mailbox
    # should carry outreach today", weighing health, warm-up and quota -- every
    # one of which is a reason the report might not go out, and the report has
    # to survive all of them.
    sender = settings.report_from_email or next(iter(provider.routable_addresses))

    result = await provider.send(
        OutboundEmail(
            to_email=to,
            from_email=sender,
            from_name=settings.owner_name,
            reply_to=sender,
            subject=subject,
            text_body=body,
            # No List-Unsubscribe. That header is a legal and reputational
            # requirement for bulk outreach; this is 1:1 operational mail to
            # the person who runs the system, and offering to unsubscribe him
            # from his own alarms would be worse than absurd -- it would let
            # one misclick end the only reporting channel there is.
            idempotency_key=f"operator:{subject}",
        )
    )
    if not result.accepted:
        # `error_kind` and `error_detail`, never `error`. SendResult has never
        # had that attribute, so this line raised AttributeError instead of the
        # reason -- which is how a Hetzner block on port 465 surfaced during the
        # 16 September migration as "'SendResult' object has no attribute
        # 'error'" rather than "TimeoutError: timed out". The failure path is
        # the one place a wrong attribute name costs most: it only runs when
        # something is already wrong, so it is never exercised until it matters.
        kind = result.error_kind.value if result.error_kind else "refused"
        detail = result.error_detail or "no detail from the provider"
        raise RuntimeError(f"operator mail {kind}: {detail}")
    return to


__all__ = ["NoOperatorAddress", "mail_the_operator"]
