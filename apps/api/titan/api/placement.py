"""Whether our mail is being seen, as an endpoint the dashboard can read.

The same question :mod:`titan.delivery.placement_report` answers in a terminal,
shaped for a screen. It exists because the operator asked to see, in one place,
which messages were filed as spam and which were seen -- and because the terminal
answer is only read by whoever is already in the terminal.

**It reports per mailbox and provider, and refuses to report an estate-wide
number.** There is no honest single figure: "62% inbox" across two providers
and five sending addresses describes nothing anybody can act on, and its only
real use is feeling informed. What the caller gets is the same breakdown the
decision is actually made on -- which address is landing, where, and which one
to stop using.

**Unchecked probes are their own count, never folded into a percentage.** A
probe nobody looked at is silence. The two months this subsystem was built to
end were two months of silence being read as an acceptable number.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from titan.api.security import Principal, require
from titan.config import get_settings
from titan.db.session import workspace_session
from titan.delivery.open_tracking import (
    PIXEL,
    PIXEL_CONTENT_TYPE,
    record_open,
    verify_open_token,
)
from titan.delivery.placement import by_mailbox, history, latest_round
from titan.delivery.placement_report import placements

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/placement", tags=["placement"])


class MailboxPlacement(BaseModel):
    """How one sending address is doing at one provider."""

    from_email: str
    provider: str
    probes: int
    inbox: int
    promotions: int
    spam: int
    missing: int
    #: Sent, and nobody has looked yet. Deliberately not part of any ratio.
    unchecked: int
    #: Probes actually looked at -- the denominator behind ``reach``.
    measured: int
    #: Share reaching the inbox, 0.0 to 1.0. Null when nothing was measured,
    #: which is not the same as zero and must not render as it.
    reach: float | None
    #: "landing", "patchy", "filtered", "not landing" or "not measured".
    verdict: str


class DayPlacement(BaseModel):
    """One day's probes at one provider. The series, not the snapshot."""

    day: dt.date
    provider: str
    probes: int
    inbox: int
    spam: int
    unchecked: int


class ProbeOut(BaseModel):
    seed_address: str
    provider: str
    from_email: str
    folder: str | None
    checked_by: str | None
    sent_at: dt.datetime
    checked_at: dt.datetime | None


class PlacementOut(BaseModel):
    """Everything the screen needs, in one round trip."""

    days: int
    by_mailbox: list[MailboxPlacement]
    trend: list[DayPlacement]
    latest: list[ProbeOut]
    #: True when no probe has ever been recorded. The caller should say so
    #: rather than draw an empty chart: nothing measured and nothing wrong look
    #: identical on an axis, and only one of them is good news.
    never_measured: bool = False
    #: Providers with addresses on the send list that no seed covers. Named
    #: because an unmeasured provider is the failure this whole subsystem
    #: exists to make visible, and a dashboard that omits it is the old problem
    #: with better typography.
    unmeasured_note: str | None = None


@router.get("", response_model=PlacementOut)
async def read_placement(
    days: int = Query(14, ge=1, le=90),
    principal: Principal = Depends(require("research:read")),
) -> PlacementOut:
    async with workspace_session(principal.workspace_id) as session:
        mailbox_rows = await by_mailbox(
            session, workspace_id=principal.workspace_id, days=days
        )
        trend_rows = await history(
            session, workspace_id=principal.workspace_id, days=days
        )
        latest_rows = await latest_round(session, workspace_id=principal.workspace_id)

    typed = placements(mailbox_rows)
    measured_providers = {p.provider for p in typed}

    return PlacementOut(
        days=days,
        by_mailbox=[
            MailboxPlacement(
                from_email=p.from_email,
                provider=p.provider,
                probes=p.probes,
                inbox=p.inbox,
                promotions=p.promotions,
                spam=p.spam,
                missing=p.missing,
                unchecked=p.unchecked,
                measured=p.measured,
                reach=p.reach,
                verdict=p.verdict,
            )
            for p in typed
        ],
        trend=[
            DayPlacement(
                day=row["day"],
                provider=str(row["provider"]),
                probes=int(row["probes"] or 0),
                inbox=int(row["inbox"] or 0),
                spam=int(row["spam"] or 0),
                unchecked=int(row["unchecked"] or 0),
            )
            for row in trend_rows
        ],
        latest=[
            ProbeOut(
                seed_address=str(row["seed_address"]),
                provider=str(row["provider"]),
                from_email=str(row["from_email"]),
                folder=row["folder"],  # type: ignore[arg-type]
                checked_by=row["checked_by"],  # type: ignore[arg-type]
                sent_at=row["sent_at"],  # type: ignore[arg-type]
                checked_at=row["checked_at"],  # type: ignore[arg-type]
            )
            for row in latest_rows
        ],
        never_measured=not mailbox_rows,
        unmeasured_note=_unmeasured(measured_providers),
    )


#: Providers we send to and what it takes to measure each one.
#:
#: Outlook is here rather than quietly absent because it is the largest single
#: bucket on the send list -- 704 of 1,845 untouched addresses -- and it cannot
#: be probed the way the others are: Microsoft stopped accepting basic auth for
#: personal accounts on 16 September 2024, so an app password returns NO LOGIN
#: and reading one needs OAuth against Graph. A dashboard that simply left
#: Microsoft off its chart would show a clean sweep while the biggest audience
#: went unwatched.
MEASURABLE = {
    "gmail": "app password",
    "outlook": "OAuth via Microsoft Graph -- not built",
    "other": "app password",
}


def _unmeasured(covered: set[str]) -> str | None:
    missing = sorted(set(MEASURABLE) - covered)
    if not missing:
        return None
    return "no seed covers: " + ", ".join(
        f"{name} ({MEASURABLE[name]})" for name in missing
    )


# ==========================================================================
# The open pixel
# ==========================================================================
#: Its own router, with no authentication, because the caller is a mail client
#: fetching an image. There is no session to present and nobody to prompt.
#:
#: The signature on the token is what stands in for auth: without it the URL
#: would carry a bare message id, and anybody could walk the range and mark
#: every message in the estate as opened. That would not merely add noise -- it
#: would make the one figure people quote permanently wrong, with no way to
#: separate the real opens from the walked ones.
open_pixel_router = APIRouter(tags=["placement"])


@open_pixel_router.get("/o/{token}.gif", include_in_schema=False)
async def open_pixel(token: str) -> Response:
    """Serve the pixel, and record the open if the token is ours.

    **Always 200, always the same 43 bytes.** A 404 on an unknown token tells
    whoever sent it that the token was wrong, which turns the endpoint into an
    oracle for guessing them. A crawler, a preview fetcher and a forged request
    all get exactly what a real client gets and learn nothing.
    """
    settings = get_settings()
    body = Response(
        content=PIXEL,
        media_type=PIXEL_CONTENT_TYPE,
        headers={
            # Never cached. A cached pixel is an open that happened once and is
            # then invisible forever, and the point of recording the first one is
            # that it is the first.
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Pragma": "no-cache",
        },
    )

    secret = settings.open_tracking_secret
    if secret is None:
        return body
    message_id = verify_open_token(token, secret)
    if message_id is None:
        return body

    from titan.db.session import get_sessionmaker

    try:
        async with get_sessionmaker()() as session, session.begin():
            await record_open(session, message_id=message_id)
    except Exception:
        # The pixel is served regardless. A database blip must not turn every
        # message in somebody's inbox into a broken image.
        logger.exception("could not record open", extra={"message_id": str(message_id)})
    return body


__all__ = ["MEASURABLE", "PlacementOut", "open_pixel_router", "router"]
