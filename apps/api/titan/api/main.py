"""Titan-OS control-plane HTTP service.

Ships operational endpoints (health, readiness, send preflight) plus the
``/api/v1`` resource surface in titan.api.routes.

No route returns plausible-looking empty data on error, which was the defect the
audit found in the pre-0.2 approvals router: it queried a nonexistent table,
swallowed the failure, and returned [].

Readiness is deliberately separate from liveness: a service that cannot reach
its database is alive (do not restart it) but not ready (do not route to it).
"""

from __future__ import annotations

import contextlib
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from titan import __version__
from titan.api.crm import router as crm_router
from titan.api.routes import router as v1_router
from titan.config import get_settings
from titan.db.session import dispose_engine, get_engine
from titan.observability.logging import configure_logging
from titan.runtime import configure_event_loop

configure_event_loop()


class SecurityHeadersMiddleware:
    """Strict headers on every response.

    Ported from the pre-0.2 middleware (gap analysis K-02) with the CSP
    tightened: the old policy allowed 'unsafe-inline' scripts everywhere, which
    defeats most of the point of having one.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                for key, value in (
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                    (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
                    (
                        b"strict-transport-security",
                        b"max-age=31536000; includeSubDomains",
                    ),
                    (
                        b"content-security-policy",
                        b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
                    ),
                ):
                    headers.append((key, value))
            await send(message)

        await self.app(scope, receive, send_wrapper)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service=settings.service_name,
        environment=settings.environment.value,
    )
    yield
    await dispose_engine()


settings = get_settings()

app = FastAPI(
    title="Titan-OS",
    version=__version__,
    # Docs are disabled in production: the schema describes the safety controls
    # and there is no reason to publish it to unauthenticated callers.
    docs_url=None if settings.is_production else "/docs",
    openapi_url=None if settings.is_production else "/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(SecurityHeadersMiddleware)
#: Exact origins. The wildcard the pre-0.2 build used, combined with
#: allow_credentials, is the classic over-permissive CORS configuration.
CORS_ORIGINS = [
    str(settings.frontend_url).rstrip("/"),
    *(o.strip().rstrip("/") for o in settings.extra_cors_origins if o.strip()),
]

#: Vercel preview deployments get a unique hostname each time, so they cannot
#: be enumerated. The pattern is anchored at both ends and pinned to the
#: project's own Vercel scope -- `.*\.vercel\.app` would hand a credentialled
#: origin to anyone who can deploy to Vercel, which is everyone.
CORS_ORIGIN_REGEX = (
    rf"^https://[a-z0-9-]+-{re.escape(settings.vercel_preview_scope)}\.vercel\.app$"
    if settings.vercel_preview_scope
    else None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-Titan-Workspace",
    ],
    max_age=600,
)


app.include_router(v1_router)
# The operator-facing read surface. Same prefix, same auth; kept in its
# own module because it is assembled views rather than resources.
app.include_router(crm_router)


@app.middleware("http")
async def request_context(request: Request, call_next: Any) -> Response:
    """Attach a request id and duration to every response."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    response.headers["x-response-time-ms"] = (
        f"{(time.perf_counter() - started) * 1000:.1f}"
    )
    return response


@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    """Liveness. Answers without touching any dependency."""
    return {"status": "ok", "service": settings.service_name, "version": __version__}


@app.get("/ready", tags=["ops"])
async def ready() -> Response:
    """Readiness. Fails when a hard dependency is unreachable."""
    checks: dict[str, str] = {}
    ok = True
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
            head = await conn.execute(text("SELECT version_num FROM alembic_version"))
            checks["database"] = f"ok (schema {head.scalar_one_or_none() or 'unknown'})"
    except Exception as exc:
        ok = False
        checks["database"] = f"unavailable: {type(exc).__name__}"

    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", "checks": checks},
    )


@app.get("/ops/sending-preflight", tags=["ops"])
async def sending_preflight() -> dict[str, Any]:
    """Why Titan will or will not deliver mail from this process.

    Read-only and unauthenticated by design: it reports *whether* gates are
    open, never any credential. An operator debugging a campaign that is not
    sending should be able to see the reason without a token.
    """
    blockers = settings.sending_preflight_errors()
    return {
        "process_sending_enabled": settings.production_sending_enabled,
        "email_provider": settings.email_provider,
        "blockers": blockers,
        "would_send": not blockers,
        "note": (
            "Process-level gate only. A message must additionally pass the "
            "workspace, campaign, sender-identity, contact, evidence, "
            "suppression and quota gates -- see titan.policy.engine."
        ),
    }


__all__ = ["app"]
