"""The HTTP surface for provider delivery events.

``titan/delivery/webhooks.py`` has known what to do with a bounce since the
first release -- record it, advance the message state without letting a late
event regress a final one, suppress the address on a complaint -- and nothing
ever called it. ``ingest_event`` appears exactly twice in the repository: its
own definition, and its own test.

The consequence is quieter than a crash and worse. ``MessageState.COMPLAINED``
is written by nothing in production, so the weekly report's complaint rate is
structurally 0.00%: not "no complaints", but "no way to hear about one". The
thresholds in :mod:`titan.intelligence.reporting` are measured against Gmail's
0.30% ceiling, and a report that says *good* by construction is worse than a
report that says nothing, because somebody trusts it.

**The signature is the authentication.** A provider cannot hold a session
token, so this router deliberately carries no ``Depends(require(...))``. That
makes verification the only thing standing between the open internet and a
handler that suppresses email addresses -- so the order here is: verify first,
parse second, and never call the handler on a payload that failed.

Three refusals, three different meanings:

* **503** -- no secret configured. Our problem. Fails closed, because an
  unverifiable webhook must never be treated as authentic: a forged
  ``delivered`` masks a bounce, and a forged ``complained`` suppresses whatever
  address it names.
* **401** -- the signature did not verify. Their problem, or an attacker's.
  Nothing is stored; storing attacker-controlled payloads is a liability, not a
  diagnostic.
* **200** -- accepted, including for events that changed nothing. A provider
  retries on any non-2xx and disables an endpoint that keeps failing, so
  "understood, and there was nothing to do" must not look like an error.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from titan.config import get_settings
from titan.db.session import system_unit_of_work
from titan.delivery.providers.base import WebhookVerificationError
from titan.delivery.webhooks import ingest_event, is_known_provider, verifier_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/delivery/webhooks", tags=["delivery"])

#: Larger than any provider event and small enough that an unauthenticated
#: caller cannot make us buffer something expensive. The signature is computed
#: over the whole body, so this is checked before any work is done on it.
MAX_BODY_BYTES = 256 * 1024


@router.post("/{provider_name}", status_code=status.HTTP_200_OK)
async def receive_delivery_event(provider_name: str, request: Request) -> dict[str, Any]:
    """Verify, record, and apply one provider delivery event.

    No concrete provider is named or imported here. Invariant 1 confines those
    to the outbox worker and the delivery layer, and a route holding a provider
    instance would have ``send()`` within reach -- the exact shape of the
    pre-0.2 defect that invariant exists to prevent. What comes back from
    ``verifier_for`` can only verify.
    """
    if not is_known_provider(provider_name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown provider"
        )

    provider = verifier_for(provider_name, get_settings())
    if provider is None:
        logger.error(
            "delivery webhook received but no secret is configured; refusing",
            extra={"provider": provider_name},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="webhook verification is not configured",
        )

    # The raw bytes, before any parsing. The signature covers exactly what was
    # sent: re-serialising a parsed body changes key order and whitespace, and
    # the HMAC would never match again.
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="payload too large",
        )

    try:
        provider.verify_webhook(payload=body, headers=dict(request.headers))
    except WebhookVerificationError as exc:
        # Logged without the body. An unverified payload is attacker-controlled
        # input, and copying it into the log moves the problem rather than
        # recording it.
        logger.warning(
            "rejected an unverified delivery webhook",
            extra={"provider": provider_name, "reason": str(exc)[:200]},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature"
        ) from exc

    try:
        payload = json.loads(body)
    except ValueError as exc:
        # Signed but unparseable. Worth a 400 rather than a 200: the signature
        # proves it came from the provider, so this is a real integration fault
        # and silently accepting it would hide it.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="malformed JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="expected a JSON object",
        )

    # Cross-workspace by necessity: the event names a provider message id, and
    # which workspace it belongs to is the answer rather than the question. The
    # handler re-scopes as soon as it has resolved the message.
    async with system_unit_of_work() as session:
        outcome = await ingest_event(session, provider, payload, signature_verified=True)

    logger.info(
        "delivery webhook processed",
        extra={
            "provider": provider_name,
            "duplicate": outcome.duplicate,
            "state_changed": outcome.state_changed,
            "ignored_reason": outcome.ignored_reason,
        },
    )
    return {
        "status": "accepted",
        "duplicate": outcome.duplicate,
        "state_changed": outcome.state_changed,
        "ignored_reason": outcome.ignored_reason,
    }


__all__ = ["MAX_BODY_BYTES", "receive_delivery_event", "router"]
