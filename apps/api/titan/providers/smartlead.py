"""Smartlead campaign-management client.

Smartlead is a cold-email *campaign* platform: mailboxes, warm-up, sequences and
a scheduler. It is not a transactional ESP, and that difference drives the whole
design of this integration.

**There is no endpoint that sends one email now.** The API surface is campaigns,
sequences, schedules and lead imports; Smartlead's own scheduler decides when
anything leaves. The only direct-send endpoint, ``reply-email-thread``, requires
a ``reply_message_id`` and so can only continue a thread that already exists.

So this module is the control-plane half of the integration: it manages
campaigns, their order and their schedules. The delivery half lives in
:mod:`titan.delivery.providers.smartlead`, which is the only module permitted to
hand a message over, and which does so only after every Titan gate has passed.

This client holds a credential and calls a *named* provider, which is why it
lives here beside the Places adapter rather than in the browser worker: it never
fetches an attacker-supplied URL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from titan.config import Settings

logger = logging.getLogger(__name__)

#: Smartlead rejects lead imports larger than this.
MAX_LEADS_PER_IMPORT = 400

#: Campaign status values Smartlead accepts on PATCH /campaigns/{id}/status.
CAMPAIGN_STATUSES = ("START", "PAUSED", "STOPPED")


class SmartleadError(RuntimeError):
    """A Smartlead call failed. Carries the status so callers can classify."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SmartleadAuthError(SmartleadError):
    """The API key was rejected. Never retried -- it will not fix itself."""


@dataclass(frozen=True, slots=True)
class SmartleadCampaign:
    id: int
    name: str
    status: str
    created_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LeadImportResult:
    """What Smartlead did with a lead import.

    ``already_added`` is the property the outbox depends on: Smartlead dedupes
    by address within a campaign, so re-importing the same lead after a crash
    does not queue a second email. That is what makes the handover idempotent.
    """

    uploaded: int
    already_added: int
    invalid: int
    lead_ids: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


class SmartleadClient:
    """Async Smartlead API client.

    Authentication is an ``api_key`` query parameter -- Smartlead's design, not
    a choice made here. The key is therefore kept out of logs by never logging a
    full request URL; only the path is ever recorded.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://server.smartlead.ai/api/v1",
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    @classmethod
    def from_settings(cls, settings: Settings) -> SmartleadClient:
        if settings.smartlead_api_key is None:
            raise SmartleadError("TITAN_SMARTLEAD_API_KEY is not configured")
        return cls(
            settings.smartlead_api_key.get_secret_value(),
            base_url=str(settings.smartlead_base_url),
            timeout_seconds=float(settings.smartlead_timeout_seconds),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------- transport
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        query = {"api_key": self._api_key, **(params or {})}
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                params=query,
                json=json,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # Log the path, never the URL: the key is in the query string.
            logger.warning(
                "smartlead request failed",
                extra={"path": path, "error_code": type(exc).__name__},
            )
            raise SmartleadError(f"{method} {path} failed: {type(exc).__name__}") from exc

        if response.status_code in (401, 403):
            raise SmartleadAuthError(
                f"Smartlead rejected the API key on {method} {path}",
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise SmartleadError(
                f"{method} {path} returned {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise SmartleadError(f"{method} {path} returned non-JSON") from exc

    # ------------------------------------------------------------- campaigns
    async def list_campaigns(self) -> list[SmartleadCampaign]:
        payload = await self._request("GET", "/campaigns/")
        rows = payload if isinstance(payload, list) else (payload or {}).get("data", [])
        return [_campaign(row) for row in rows if isinstance(row, dict)]

    async def get_campaign(self, campaign_id: int) -> SmartleadCampaign:
        payload = await self._request("GET", f"/campaigns/{campaign_id}")
        if not isinstance(payload, dict):
            raise SmartleadError(f"campaign {campaign_id} not found")
        return _campaign(payload)

    async def create_campaign(
        self, name: str, *, client_id: int | None = None
    ) -> SmartleadCampaign:
        body: dict[str, Any] = {"name": name}
        if client_id is not None:
            body["client_id"] = client_id
        payload = await self._request("POST", "/campaigns/create", json=body)
        return _campaign(payload if isinstance(payload, dict) else {})

    async def set_campaign_status(self, campaign_id: int, status: str) -> None:
        """START, PAUSED or STOPPED.

        Pausing here is not a Titan safety control -- Titan's own campaign gate
        already stops queued mail. This exists so an operator can hold the
        Smartlead side too, rather than having two systems disagree.
        """
        if status not in CAMPAIGN_STATUSES:
            raise SmartleadError(
                f"status must be one of {CAMPAIGN_STATUSES}, got {status!r}"
            )
        await self._request(
            "PATCH", f"/campaigns/{campaign_id}/status", json={"status": status}
        )

    async def update_settings(
        self, campaign_id: int, settings: dict[str, Any]
    ) -> dict[str, Any]:
        """General campaign settings: tracking, plain text, unsubscribe copy.

        POST, not PATCH. The published reference says PATCH and the live API
        answers 404 to it, while POST works -- verified against the account on
        2026-08-17. The same page is wrong about two other endpoints this client
        calls, so the method here follows the server rather than the document.
        """
        result = await self._request(
            "POST", f"/campaigns/{campaign_id}/settings", json=settings
        )
        return result if isinstance(result, dict) else {}

    async def set_schedule(
        self, campaign_id: int, schedule: dict[str, Any]
    ) -> dict[str, Any]:
        """Set sending days, hours and timezone for a campaign."""
        result = await self._request(
            "POST", f"/campaigns/{campaign_id}/schedule", json=schedule
        )
        return result if isinstance(result, dict) else {}

    async def set_sequences(
        self, campaign_id: int, sequences: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Replace a campaign's sequence steps, in order.

        Order is the caller's: Smartlead sends ``seq_number`` ascending, so the
        list is written with explicit numbers rather than relying on position.
        """
        ordered = [
            {**step, "seq_number": step.get("seq_number", index + 1)}
            for index, step in enumerate(sequences)
        ]
        result = await self._request(
            "POST", f"/campaigns/{campaign_id}/sequences", json={"sequences": ordered}
        )
        return result if isinstance(result, dict) else {}

    async def get_sequences(self, campaign_id: int) -> list[dict[str, Any]]:
        """The campaign's sequence steps, in Smartlead's own order.

        A separate call on purpose: ``GET /campaigns/{id}`` does **not** include
        sequences, verified against the live API on 2026-08-09. Reading the
        campaign payload and finding no steps therefore means "this endpoint
        does not report them", not "this campaign has none" -- a distinction the
        carrier-shape check depends on to avoid refusing every campaign.
        """
        payload = await self._request("GET", f"/campaigns/{campaign_id}/sequences")
        rows = payload if isinstance(payload, list) else (payload or {}).get("data", [])
        return [row for row in rows if isinstance(row, dict)]

    async def attach_email_accounts(
        self, campaign_id: int, email_account_ids: list[int]
    ) -> dict[str, Any]:
        result = await self._request(
            "POST",
            f"/campaigns/{campaign_id}/email-accounts",
            json={"email_account_ids": email_account_ids},
        )
        return result if isinstance(result, dict) else {}

    async def campaign_analytics(self, campaign_id: int) -> dict[str, Any]:
        result = await self._request("GET", f"/campaigns/{campaign_id}/analytics")
        return result if isinstance(result, dict) else {}

    async def campaign_email_accounts(self, campaign_id: int) -> list[dict[str, Any]]:
        """The mailboxes this campaign is allowed to send from.

        The account list endpoint reports ``is_connected_to_campaign`` as null
        and ``campaign_count`` as zero for every account regardless of truth --
        verified against the live API -- so attachment can only be established
        from the campaign's side. That matters: it is the difference between a
        mailbox this system may reconfigure and one a person deliberately kept
        out of outreach.
        """
        payload = await self._request("GET", f"/campaigns/{campaign_id}/email-accounts")
        rows = payload if isinstance(payload, list) else (payload or {}).get("data", [])
        return [row for row in rows if isinstance(row, dict)]

    async def campaign_statistics(
        self, campaign_id: int, *, offset: int = 0, limit: int = 100
    ) -> tuple[list[dict[str, Any]], int]:
        """Per-lead, per-step delivery outcomes. Returns the page and the total.

        This is the only place Smartlead exposes what happened to an individual
        send. ``/analytics`` gives campaign totals, which cannot answer "which
        addresses bounced" or "did step 2 ever go out"; the webhook callbacks
        would answer both but require a public endpoint to receive them, and
        there is not one. Polling this is how a system with no inbound HTTP
        surface still learns from its own sending.

        Each row carries a ``stats_id``, stable across pages and across calls,
        which is what makes repeated polling idempotent rather than duplicative.
        """
        payload = await self._request(
            "GET",
            f"/campaigns/{campaign_id}/statistics",
            params={"offset": offset, "limit": limit},
        )
        if not isinstance(payload, dict):
            return [], 0
        rows = [row for row in (payload.get("data") or []) if isinstance(row, dict)]
        try:
            total = int(payload.get("total_stats") or 0)
        except (TypeError, ValueError):
            total = 0
        return rows, total

    # ----------------------------------------------------------------- leads
    async def add_leads(
        self,
        campaign_id: int,
        leads: list[dict[str, Any]],
        *,
        ignore_global_block_list: bool = False,
        ignore_unsubscribe_list: bool = False,
    ) -> LeadImportResult:
        """Import leads into a campaign.

        The two ``ignore_*`` flags default to False and Titan never sets them
        true: Smartlead's block and unsubscribe lists are a second opinion on
        top of Titan's own suppression table, and overriding them would mean
        mailing someone who opted out of one system or the other.
        """
        if not leads:
            raise SmartleadError("no leads to import")
        if len(leads) > MAX_LEADS_PER_IMPORT:
            raise SmartleadError(
                f"{len(leads)} leads exceeds Smartlead's limit of "
                f"{MAX_LEADS_PER_IMPORT} per request"
            )
        payload = await self._request(
            "POST",
            f"/campaigns/{campaign_id}/leads",
            json={
                "lead_list": leads,
                "settings": {
                    "ignore_global_block_list": ignore_global_block_list,
                    "ignore_unsubscribe_list": ignore_unsubscribe_list,
                    "ignore_duplicate_leads_in_other_campaign": False,
                },
            },
        )
        body = payload if isinstance(payload, dict) else {}
        return LeadImportResult(
            uploaded=int(body.get("upload_count") or 0),
            already_added=int(body.get("already_added_to_campaign") or 0),
            invalid=int(body.get("invalid_email_count") or 0),
            lead_ids=tuple(str(i) for i in (body.get("bulk_lead_ids") or [])),
            raw=body,
        )

    async def lead_message_history(
        self, campaign_id: int, lead_id: int
    ) -> list[dict[str, Any]]:
        """Every message in one lead's thread, sends and replies together.

        The only place the *content* of a reply is available. ``/statistics``
        reports ``reply_time`` and nothing else, which is enough to know that
        somebody answered and not enough to know whether the answer was
        "sounds good" or "I am on annual leave" -- and those two are the same
        number to anything counting replies.

        Called only for leads whose statistics row shows a reply, so a campaign
        costs one request plus one per replying lead rather than one per lead.
        """
        payload = await self._request(
            "GET", f"/campaigns/{campaign_id}/leads/{lead_id}/message-history"
        )
        if isinstance(payload, dict):
            rows = payload.get("history") or payload.get("data") or []
        else:
            rows = payload or []
        return [row for row in rows if isinstance(row, dict)]

    async def lead_by_email(self, email: str) -> dict[str, Any] | None:
        # "/leads/" with an email parameter, not "/leads/by-email": the latter
        # routes to the by-id handler and answers
        # `"leadId" must be a number`. Verified against the live account on
        # 2026-08-17; this method had never been called by anything.
        payload = await self._request("GET", "/leads/", params={"email": email})
        return payload if isinstance(payload, dict) and payload else None

    async def campaign_leads(
        self, campaign_id: int, *, offset: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/campaigns/{campaign_id}/leads",
            params={"offset": offset, "limit": limit},
        )
        if isinstance(payload, dict):
            rows = payload.get("data") or []
        else:
            rows = payload or []
        return [row for row in rows if isinstance(row, dict)]

    # --------------------------------------------------------- email accounts
    async def list_email_accounts(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/email-accounts")
        rows = payload if isinstance(payload, list) else (payload or {}).get("data", [])
        return [row for row in rows if isinstance(row, dict)]

    async def set_daily_limit(self, email_account_id: int, limit: int) -> None:
        """Set one mailbox's ``max_email_per_day``, and nothing else.

        The payload carries a single field on purpose. This is the same endpoint
        that holds the mailbox's SMTP and IMAP configuration, and a caller that
        sent a fuller body reconstructed from a GET could put stale connection
        settings back over working ones. Verified against the live API: posting
        only this field leaves ``is_smtp_success`` and ``is_imap_success``
        untouched.

        Refuses a negative limit rather than passing it through. Zero is
        meaningful -- it closes the mailbox -- but a negative number is a
        calculation that went wrong upstream, and forwarding it would let a bug
        here become a mailbox setting nobody chose.
        """
        if limit < 0:
            raise ValueError(f"daily limit cannot be negative: {limit}")
        await self._request(
            "POST",
            f"/email-accounts/{email_account_id}",
            json={"max_email_per_day": limit},
        )

    async def warmup_stats(self, email_account_id: int) -> dict[str, Any]:
        result = await self._request(
            "GET", f"/email-accounts/{email_account_id}/warmup-stats"
        )
        return result if isinstance(result, dict) else {}

    # ---------------------------------------------------------------- health
    async def health_check(self) -> tuple[bool, str]:
        """Whether the configured key can reach Smartlead."""
        try:
            campaigns = await self.list_campaigns()
        except SmartleadAuthError as exc:
            return False, str(exc)
        except SmartleadError as exc:
            return False, f"smartlead unreachable: {exc}"
        return True, f"smartlead ok ({len(campaigns)} campaigns visible)"


def _campaign(row: dict[str, Any]) -> SmartleadCampaign:
    return SmartleadCampaign(
        id=int(row.get("id") or 0),
        name=str(row.get("name") or ""),
        status=str(row.get("status") or "UNKNOWN"),
        created_at=row.get("created_at"),
        raw=row,
    )


__all__ = [
    "CAMPAIGN_STATUSES",
    "MAX_LEADS_PER_IMPORT",
    "LeadImportResult",
    "SmartleadAuthError",
    "SmartleadCampaign",
    "SmartleadClient",
    "SmartleadError",
]
