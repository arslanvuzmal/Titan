"""Smartlead delivery adapter.

Read this before trusting it, because Smartlead does not fit the provider
protocol the way Resend does, and the gap is real rather than cosmetic.

**Smartlead has no transactional send endpoint.** Nothing in its API says "send
this message to this person now". Sending is a side effect of campaign
execution: leads are imported into a campaign, and Smartlead's scheduler emits
the sequence steps on its own timetable, through its own mailboxes.

So ``send()`` here means: *hand this one already-authorized message to a
dedicated single-step campaign.* Concretely, the message becomes one lead import
carrying the validated subject and body as custom fields, which the campaign's
single sequence step renders through ``{{titan_subject}}`` / ``{{titan_body}}``.

What that preserves, and what it does not:

* **Every Titan gate still runs first.** This adapter is called by the outbox
  worker, after the second policy evaluation, the deliverability check and the
  quota reservation. Nothing reaches Smartlead that Titan did not authorize.
* **The text sent is the text Titan validated.** The body travels as data, not
  as a template Smartlead could re-render differently -- provided the campaign's
  step is configured as documented in ``verify_campaign_shape``.
* **Timing is Smartlead's, not Titan's.** Quiet hours, spacing and the send
  window are enforced by Titan at handover, but Smartlead applies its own
  schedule afterwards. The two must be configured to agree; Titan cannot make
  that true by itself.
* **Follow-ups must not exist on the Smartlead side.** A campaign with more than
  one sequence step would send mail Titan never authorized and never evaluated.
  ``verify_campaign_shape`` refuses such a campaign, and ``send`` refuses to run
  until the shape has been verified once.

Only the outbox worker may import this module; an invariant test enforces it.
"""

from __future__ import annotations

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
from titan.providers.smartlead import (
    SmartleadAuthError,
    SmartleadClient,
    SmartleadError,
)

logger = logging.getLogger(__name__)

#: The custom fields the campaign's single sequence step must render.
SUBJECT_FIELD = "titan_subject"
BODY_FIELD = "titan_body"
#: Carried for traceability so a message can be found from either side.
IDEMPOTENCY_FIELD = "titan_idempotency_key"

#: Lead statuses Smartlead reports, mapped onto Titan's message states. Only
#: states Titan can act on are mapped; anything else stays None rather than
#: being guessed into a state machine that drives suppression.
_LEAD_STATUS_TO_STATE: dict[str, MessageState] = {
    "COMPLETED": MessageState.SENT,
    "INPROGRESS": MessageState.SENT,
    "BLOCKED": MessageState.FAILED,
    "STOPPED": MessageState.FAILED,
}


class SmartleadProvider:
    """Hands one authorized message to a single-step Smartlead campaign."""

    name = "smartlead"

    def __init__(
        self,
        api_key: str,
        campaign_id: int,
        *,
        base_url: str = "https://server.smartlead.ai/api/v1",
        timeout_seconds: float = 30.0,
        client: SmartleadClient | None = None,
    ) -> None:
        #: The carrier used when a message does not name one of its own.
        self._campaign_id = campaign_id
        self._client = client or SmartleadClient(
            api_key, base_url=base_url, timeout_seconds=timeout_seconds
        )
        #: Which campaigns verify_campaign_shape() has cleared. A set rather than
        #: a boolean because the single-step guarantee is a property of one
        #: campaign, not of the provider: with a carrier per market, clearing
        #: London would otherwise have vouched for Dubai as well, and a carrier
        #: that had grown a second step would send mail Titan never authorized.
        self._shape_verified: set[int] = set()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- shape
    async def verify_campaign_shape(
        self, campaign_id: int | None = None
    ) -> tuple[bool, str]:
        """Check the campaign is a single-step carrier, not a real sequence.

        This is the safety check that makes the whole integration honest. A
        Smartlead campaign with three sequence steps would send two messages
        Titan never drafted, never validated against evidence, and never
        authorized -- silently, and outside every gate in this repository.

        Checked per campaign. Each market has its own carrier, and each one can
        have been edited in the Smartlead UI independently of the others.
        """
        target = self._campaign_id if campaign_id is None else campaign_id
        # Read the sequences endpoint, not the campaign payload: the campaign
        # object does not carry its steps (verified live, 2026-08-09), so
        # inspecting it would find nothing and refuse every campaign forever.
        try:
            steps = await self._client.get_sequences(target)
        except SmartleadError as exc:
            return False, f"cannot read sequences for {target}: {exc}"

        count = len(steps)
        if count == 0:
            return False, (
                f"campaign {target} has no sequence steps; there is "
                "nothing to render the message into"
            )

        if count != 1:
            return False, (
                f"campaign {target} has {count} sequence steps. Titan "
                "authorizes exactly one message at a time, so the carrier campaign "
                "must have exactly one step; any other step would send unauthorized "
                "mail."
            )

        self._shape_verified.add(target)
        return True, f"campaign {target} is a single-step carrier"

    # ----------------------------------------------------------------- send
    async def send(self, email: OutboundEmail) -> SendResult:
        """Hand the message over. Never called before Titan's gates have passed."""
        campaign_id = email.carrier_campaign_id or self._campaign_id
        if campaign_id not in self._shape_verified:
            ok, detail = await self.verify_campaign_shape(campaign_id)
            if not ok:
                # A configuration fault, not the recipient's: do not suppress.
                return SendResult(
                    accepted=False,
                    error_kind=SendErrorKind.INVALID_SENDER,
                    error_detail=detail,
                )

        lead: dict[str, Any] = {
            "email": email.to_email,
            "custom_fields": {
                SUBJECT_FIELD: email.subject,
                BODY_FIELD: email.html_body or _as_html(email.text_body),
                IDEMPOTENCY_FIELD: email.idempotency_key,
            },
        }

        try:
            result = await self._client.add_leads(campaign_id, [lead])
        except SmartleadAuthError as exc:
            return SendResult(
                accepted=False,
                error_kind=SendErrorKind.AUTH,
                error_detail=str(exc),
            )
        except SmartleadError as exc:
            return SendResult(
                accepted=False,
                error_kind=SendErrorKind.TRANSIENT,
                error_detail=str(exc),
            )

        if result.invalid:
            return SendResult(
                accepted=False,
                error_kind=SendErrorKind.INVALID_RECIPIENT,
                error_detail=f"Smartlead rejected {email.to_email} as invalid",
                raw=result.raw,
            )

        if result.already_added:
            # The retry path. Smartlead dedupes by address within a campaign, so
            # the message is already queued there and importing again did not
            # duplicate it. Reporting this as accepted is what makes a retry
            # after a lost response safe.
            logger.info(
                "smartlead reported the lead as already present; treating the "
                "handover as complete",
                extra={"campaign_id": campaign_id},
            )
            return SendResult(
                accepted=True,
                provider_message_id=_first_id(result.lead_ids, email.idempotency_key),
                raw=result.raw,
            )

        if not result.uploaded:
            return SendResult(
                accepted=False,
                error_kind=SendErrorKind.PROVIDER_ERROR,
                error_detail="Smartlead accepted the request but uploaded no lead",
                raw=result.raw,
            )

        return SendResult(
            accepted=True,
            provider_message_id=_first_id(result.lead_ids, email.idempotency_key),
            raw=result.raw,
        )

    # --------------------------------------------------------------- status
    async def get_status(self, provider_message_id: str) -> MessageState | None:
        """Best-effort status.

        Smartlead tracks *leads*, not messages, so this reports the lead's
        campaign status. It cannot distinguish delivered from opened, and
        returns None rather than guessing.
        """
        try:
            leads = await self._client.campaign_leads(self._campaign_id, limit=200)
        except SmartleadError:
            return None
        for row in leads:
            nested = row.get("lead")
            lead: dict[str, Any] = nested if isinstance(nested, dict) else row
            if str(lead.get("id")) != provider_message_id:
                continue
            status = str(row.get("status") or lead.get("status") or "").upper()
            return _LEAD_STATUS_TO_STATE.get(status)
        return None

    # -------------------------------------------------------------- webhooks
    def verify_webhook(self, *, payload: bytes, headers: dict[str, str]) -> None:
        """Always refuses.

        Titan verifies Resend webhooks with a documented Svix scheme. No
        equivalent signing scheme for Smartlead has been confirmed here, and
        inventing one would produce a check that looks like verification and
        proves nothing -- which is worse than having none, because state
        changes would be accepted from anyone who found the URL.

        Until the scheme is confirmed against Smartlead's documentation, this
        fails closed and Smartlead events are not ingested.
        """
        raise WebhookVerificationError(
            "Smartlead webhook verification is not implemented; refusing to "
            "accept an unauthenticated event that would change message state"
        )

    def normalize_webhook(self, payload: dict[str, Any]) -> NormalizedEvent | None:
        """Unreachable while verify_webhook refuses. Returns None deliberately."""
        return None

    # ---------------------------------------------------------------- health
    async def health_check(self) -> tuple[bool, str]:
        ok, detail = await self._client.health_check()
        if not ok:
            return ok, detail
        shape_ok, shape_detail = await self.verify_campaign_shape()
        return shape_ok, detail if shape_ok else f"{detail}; but {shape_detail}"


def _as_html(text_body: str) -> str:
    """Smartlead sequence bodies are HTML; preserve the validated line breaks."""
    escaped = text_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped.replace("\n", "<br />\n")


def _first_id(lead_ids: tuple[str, ...], fallback: str) -> str:
    """Smartlead does not always return ids; fall back to our own key.

    The outbox refuses to send without an idempotency key, so the fallback is
    always a real, unique value rather than an empty string.
    """
    return lead_ids[0] if lead_ids else fallback


__all__ = ["BODY_FIELD", "IDEMPOTENCY_FIELD", "SUBJECT_FIELD", "SmartleadProvider"]
