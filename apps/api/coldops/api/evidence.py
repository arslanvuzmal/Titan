"""The evidence page, served to whoever holds its signed link.

Unauthenticated by design: the reader is a business owner who clicked a link
in an email, with no account and nothing to log in with. The HMAC on the token
is the credential. A forged token, a deleted lead and a switched-off feature
all get the same neutral page, so the route cannot be used to test guesses.

Recording never breaks the page. A database blip while writing an engagement
event is logged and swallowed; the owner still sees what was found.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import text

from coldops.config import get_settings
from coldops.db.session import get_sessionmaker
from coldops.delivery import engagement
from coldops.intelligence import evidence_page

logger = logging.getLogger(__name__)

evidence_router = APIRouter(tags=["evidence"])

#: Never cached by anything in between, and never indexed.
_HEADERS = {
    "Cache-Control": "no-store, private",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Referrer-Policy": "no-referrer",
}


def _not_found() -> HTMLResponse:
    return HTMLResponse(evidence_page.not_found_html(), status_code=404, headers=_HEADERS)


@evidence_router.get("/e/{token}", include_in_schema=False)
async def show_evidence(token: str, request: Request) -> HTMLResponse:
    settings = get_settings()
    secret = settings.evidence_secret
    if secret is None:
        return _not_found()
    lead_id = evidence_page.verify_evidence_token(token, secret)
    if lead_id is None:
        return _not_found()

    now = dt.datetime.now(dt.UTC)
    async with get_sessionmaker()() as session:
        page = await evidence_page.load_page(session, lead_id=lead_id)
        if page is None:
            return _not_found()
        try:
            async with session.begin_nested():
                last_send = await engagement.last_send_to_lead(
                    session, workspace_id=page.workspace_id, lead_id=lead_id
                )
                user_agent = request.headers.get("user-agent")
                client_ip = engagement.client_ip_of(
                    dict(request.headers), request.client.host if request.client else None
                )
                graded = engagement.grade(
                    engagement.VISIT,
                    user_agent=user_agent,
                    client_ip=client_ip,
                    since_send=(now - last_send) if last_send else None,
                )
                await engagement.record_event(
                    session,
                    workspace_id=page.workspace_id,
                    lead_id=lead_id,
                    message_id=None,
                    kind=engagement.VISIT,
                    graded=graded,
                    client_ip=client_ip,
                    user_agent=user_agent,
                    occurred_at=now,
                )
            await session.commit()
        except Exception:
            logger.exception("could not record an evidence-page visit")

    html = evidence_page.render(
        page,
        token=token,
        owner_name=settings.owner_name,
        portfolio_url=str(settings.owner_portfolio_url).rstrip("/"),
    )
    return HTMLResponse(html, headers=_HEADERS)


@evidence_router.post("/e/{token}/seen", include_in_schema=False)
async def evidence_seen(token: str, request: Request) -> Response:
    """The page's own beacon: somebody stayed long enough to read.

    Always 204, whatever happens, for the same reason the pixel is always 200.
    """
    done = Response(status_code=204, headers=_HEADERS)
    settings = get_settings()
    secret = settings.evidence_secret
    if secret is None:
        return done
    lead_id = evidence_page.verify_evidence_token(token, secret)
    if lead_id is None:
        return done

    try:
        payload = json.loads((await request.body())[:2000] or b"{}")
        dwell = float(payload.get("dwell", 0))
        scroll = int(payload.get("scroll", 0))
    except (ValueError, TypeError, AttributeError):
        return done

    now = dt.datetime.now(dt.UTC)
    try:
        async with get_sessionmaker()() as session, session.begin():
            # Primary key only, as on the page itself: the caller holds a
            # token we signed and no workspace.
            workspace_id = await session.scalar(
                text("SELECT workspace_id FROM leads WHERE id = :lead"),
                {"lead": lead_id},
            )
            if workspace_id is None:
                return done
            user_agent = request.headers.get("user-agent")
            await engagement.record_event(
                session,
                workspace_id=workspace_id,
                lead_id=lead_id,
                message_id=None,
                kind=engagement.VISIT_CONFIRMED,
                graded=engagement.grade(
                    engagement.VISIT_CONFIRMED,
                    user_agent=user_agent,
                    client_ip=None,
                    since_send=None,
                    dwell_seconds=dwell,
                ),
                client_ip=engagement.client_ip_of(
                    dict(request.headers), request.client.host if request.client else None
                ),
                user_agent=user_agent,
                occurred_at=now,
                detail={"dwell": dwell, "scroll": max(0, min(scroll, 100))},
            )
    except Exception:
        logger.exception("could not record an evidence-page beacon")
    return done


@evidence_router.get("/e/{token}/shot/{view}.jpg", include_in_schema=False)
async def evidence_shot(token: str, view: str) -> Response:
    """One of the lead's homepage screenshots, for the page that names it.

    The same token gates it, so a screenshot is no more public than the page.
    The storage key is read from the database and checked against the one
    shape a screenshot key has before any path is built from it.
    """
    missing = Response(status_code=404, headers=_HEADERS)
    settings = get_settings()
    if settings.evidence_secret is None or not settings.artifact_dir:
        return missing
    if view not in evidence_page.SHOT_KINDS:
        return missing
    lead_id = evidence_page.verify_evidence_token(token, settings.evidence_secret)
    if lead_id is None:
        return missing
    async with get_sessionmaker()() as session:
        workspace_id = await session.scalar(
            text("SELECT workspace_id FROM leads WHERE id = :lead"), {"lead": lead_id}
        )
        if workspace_id is None:
            return missing
        shots = await evidence_page.latest_shots(
            session, workspace_id=workspace_id, lead_id=lead_id
        )
    if view not in shots:
        return missing
    path = evidence_page.shot_path(settings.artifact_dir, shots[view][0])
    if path is None:
        return missing
    try:
        data = await asyncio.to_thread(path.read_bytes)
    except OSError:
        return missing
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={**_HEADERS, "Cache-Control": "private, max-age=3600"},
    )


__all__ = ["evidence_router"]
