"""Instantly delivery adapter.

Read the module docstring in :mod:`titan.delivery.providers.smartlead` first,
because the shape of the problem is identical and so is the answer.

**Instantly has no transactional send endpoint.** Nothing in its v2 API says
"send this message to this person now". Sending is a side effect of campaign
execution: a lead is created against a campaign, and Instantly's scheduler
emits the sequence step through its own mailboxes on its own timetable.

So ``send()`` means: *hand this one already-authorized message to a dedicated
single-step campaign.* The validated subject and body travel as custom
variables which the campaign's single step renders. What that preserves is what
it preserves for Smartlead:

* **Every Titan gate has already run.** This is called by the outbox worker,
  after the second policy evaluation, the deliverability check and the quota
  reservation. Nothing reaches Instantly that Titan did not authorize.
* **The text sent is the text Titan validated**, provided the campaign is
  shaped as :meth:`InstantlyProvider.verify_campaign_shape` insists.
* **A campaign with more than one step would send mail Titan never wrote.**
  Refused, per campaign, and cached only after it has passed.

**What has not been proven.** There is no Instantly account to test against
yet, so no call in this module has been exercised against a live key. The
request field names live in :data:`titan.providers.instantly.FIELD` and the
step-count reading is isolated in :func:`_sequence_step_count`; both are single
edit points. Failure is loud -- a wrong field name produces a 4xx carrying
Instantly's own message, not a message that quietly goes nowhere.

Only the outbox worker may import this module; an invariant test enforces it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from titan.db.enums import MessageState
from titan.delivery.providers.base import (
    NormalizedEvent,
    OutboundEmail,
    SendErrorKind,
    SendResult,
    WebhookVerificationError,
)
from titan.providers.instantly import (
    InstantlyAuthError,
    InstantlyClient,
    InstantlyError,
)

logger = logging.getLogger(__name__)

#: The custom variables the campaign's single sequence step must render, as
#: ``{{titan_subject}}`` and ``{{titan_body}}``. Same names as the Smartlead
#: carrier uses, so a campaign built for one reads the same as the other.
SUBJECT_FIELD = "titan_subject"
BODY_FIELD = "titan_body"
IDEMPOTENCY_FIELD = "titan_idempotency_key"

#: Instantly event names mapped onto Titan's message states.
#:
#: Only states Titan acts on are mapped. Anything unrecognised stays ``None``
#: rather than being guessed into a state machine that drives suppression --
#: a mis-mapped bounce permanently suppresses somebody who never bounced.
_EVENT_TO_STATE: dict[str, MessageState] = {
    "email_sent": MessageState.SENT,
    "email_bounced": MessageState.BOUNCED,
    "email_opened": MessageState.SENT,
    "reply_received": MessageState.SENT,
}

#: Events that mean the address must never be written to again.
_HARD_BOUNCE_EVENTS = frozenset({"email_bounced"})


def _sequence_step_count(campaign: dict[str, Any]) -> int | None:
    """How many steps the campaign will send, or None if it cannot be read.

    ``None`` is not zero and must not be treated as a pass. A campaign whose
    shape could not be determined is a campaign that might send three messages
    Titan never wrote, and the honest response is to refuse it rather than to
    assume the convenient answer.

    Instantly nests the steps under ``sequences[].steps[]``; both spellings of
    the outer key are accepted because the published schema and the help centre
    disagree, and being wrong here means refusing every campaign for ever.
    """
    sequences = campaign.get("sequences")
    if sequences is None:
        sequences = campaign.get("sequence")
    if not isinstance(sequences, list):
        return None
    total = 0
    for sequence in sequences:
        if not isinstance(sequence, dict):
            return None
        steps = sequence.get("steps")
        if not isinstance(steps, list):
            return None
        total += len(steps)
    return total


class InstantlyProvider:
    """Hands one authorized message to a single-step Instantly campaign."""

    name = "instantly"

    def __init__(
        self,
        client: InstantlyClient,
        *,
        campaign_id: str,
        webhook_secret: str | None = None,
    ) -> None:
        self._client = client
        self._campaign_id = campaign_id
        self._webhook_secret = webhook_secret
        self._shape_verified: set[str] = set()

    # -------------------------------------------------------------- shape
    async def verify_campaign_shape(
        self, campaign_id: str | None = None
    ) -> tuple[bool, str]:
        """Refuse a carrier campaign that is a real sequence.

        The check that makes the integration honest. A campaign with three
        steps would send two messages Titan never drafted, never validated
        against evidence and never authorized -- silently, and outside every
        gate in this repository.

        Per campaign: each market has its own carrier and each can be edited in
        the Instantly UI independently of the others.
        """
        target = campaign_id or self._campaign_id
        try:
            campaign = await self._client.get_campaign(target)
        except InstantlyError as exc:
            return False, f"cannot read campaign {target}: {exc}"

        count = _sequence_step_count(campaign)
        if count is None:
            return False, (
                f"campaign {target}: could not read its sequence steps, so it "
                "cannot be shown to send exactly one message. Refusing rather "
                "than assuming."
            )
        if count == 0:
            return False, (
                f"campaign {target} has no sequence steps; there is nothing to "
                "render the message into"
            )
        if count != 1:
            return False, (
                f"campaign {target} has {count} sequence steps. Titan authorizes "
                "exactly one message at a time, so the carrier campaign must have "
                "exactly one step; any other step would send unauthorized mail."
            )

        self._shape_verified.add(target)
        return True, f"campaign {target} is a single-step carrier"

    # --------------------------------------------------------------- send
    async def send(self, email: OutboundEmail) -> SendResult:
        """Hand the message over. Never called before Titan's gates have passed."""
        campaign_id = (
            str(email.carrier_campaign_id)
            if email.carrier_campaign_id is not None
            else self._campaign_id
        )

        if campaign_id not in self._shape_verified:
            ok, detail = await self.verify_campaign_shape(campaign_id)
            if not ok:
                # A configuration failure, not a transient one: retrying sends
                # the same message into the same wrongly-shaped campaign.
                # INVALID_SENDER, matching the Smartlead adapter: it is in
                # CONFIGURATION_ERROR_KINDS, so the row stops without the
                # recipient being suppressed. Our campaign is misconfigured;
                # nothing is wrong with the person we were writing to.
                return SendResult(
                    accepted=False,
                    error_kind=SendErrorKind.INVALID_SENDER,
                    error_detail=detail,
                )

        custom = {
            SUBJECT_FIELD: email.subject,
            BODY_FIELD: email.text_body,
            IDEMPOTENCY_FIELD: email.idempotency_key,
        }

        try:
            created = await self._client.create_lead(
                email=email.to_email,
                campaign_id=campaign_id,
                custom_variables=custom,
            )
        except InstantlyAuthError as exc:
            return SendResult(
                accepted=False,
                error_kind=SendErrorKind.AUTH,
                error_detail=str(exc),
            )
        except InstantlyError as exc:
            return SendResult(
                accepted=False,
                error_kind=SendErrorKind.TRANSIENT,
                error_detail=str(exc),
            )

        provider_id = str(created.get("id") or "") or None
        if provider_id is None:
            # Accepted without an identifier is not acceptance Titan can track:
            # reconciliation, status and bounce attribution all key on it.
            return SendResult(
                accepted=False,
                error_kind=SendErrorKind.TRANSIENT,
                error_detail="Instantly accepted the lead but returned no id",
            )

        logger.info(
            "handed message to Instantly",
            extra={"campaign_id": campaign_id, "provider_message_id": provider_id},
        )
        return SendResult(accepted=True, provider_message_id=provider_id)

    # ------------------------------------------------------------- status
    async def get_status(self, provider_message_id: str) -> MessageState | None:
        """Not readable per message on this API.

        Instantly reports outcomes through webhooks and campaign analytics
        rather than a per-lead status endpoint. ``None`` says "unknown", which
        is what the reconciler expects and is honest; inventing SENT here would
        mark as delivered a message that may never have left.
        """
        return None

    # ------------------------------------------------------------ webhook
    def verify_webhook(self, *, payload: bytes, headers: dict[str, str]) -> None:
        """Reject anything not signed with the configured shared secret.

        Fails closed. With no secret configured this refuses every delivery
        rather than accepting every delivery -- an unauthenticated webhook can
        mark a message bounced and suppress a real prospect for ever, so
        "unverifiable" has to mean "refused".
        """
        if not self._webhook_secret:
            raise WebhookVerificationError(
                "no Instantly webhook secret configured; refusing an "
                "unverifiable delivery event"
            )
        presented = (
            headers.get("x-instantly-signature")
            or headers.get("X-Instantly-Signature")
            or ""
        )
        expected = hmac.new(
            self._webhook_secret.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(presented, expected):
            raise WebhookVerificationError("Instantly webhook signature did not verify")

    def normalize_webhook(self, payload: dict[str, Any]) -> NormalizedEvent | None:
        """Reduce an event to what the state machine needs, or None."""
        import datetime as dt

        event_type = str(payload.get("event_type") or payload.get("event") or "")
        if not event_type:
            return None

        occurred_raw = payload.get("timestamp") or payload.get("occurred_at")
        try:
            occurred = (
                dt.datetime.fromisoformat(str(occurred_raw).replace("Z", "+00:00"))
                if occurred_raw
                else dt.datetime.now(dt.UTC)
            )
        except ValueError:
            occurred = dt.datetime.now(dt.UTC)

        return NormalizedEvent(
            provider=self.name,
            provider_event_id=str(payload.get("id") or payload.get("event_id") or ""),
            event_type=event_type,
            provider_message_id=str(payload.get("lead_id") or "") or None,
            state=_EVENT_TO_STATE.get(event_type),
            occurred_at=occurred,
            recipient=str(payload.get("email") or "") or None,
            is_hard_bounce=event_type in _HARD_BOUNCE_EVENTS,
            raw=payload,
        )

    async def health_check(self) -> tuple[bool, str]:
        return await self._client.health_check()


__all__ = [
    "BODY_FIELD",
    "IDEMPOTENCY_FIELD",
    "SUBJECT_FIELD",
    "InstantlyProvider",
]
