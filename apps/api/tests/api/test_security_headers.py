"""`SecurityHeadersMiddleware`, exercised directly as ASGI.

No database and no HTTP client: the middleware is a plain ASGI wrapper, and
driving it directly is the only way to state the scheme cases, because a test
client that speaks plaintext to the app can never produce the `https` one.

The defect these exist for: HSTS was sent on *every* response, plain HTTP
included. On an IP literal browsers ignore it, which is why the 17 September
CRM deployment survived; on a hostname, one plain-HTTP page view pins that name
to HTTPS for a year, and a deployment that has no certificate yet becomes
unreachable with no visible cause.
"""

from __future__ import annotations

from typing import Any

import pytest
from titan.api.main import SecurityHeadersMiddleware, _is_https

HSTS = b"strict-transport-security"


async def _ok_app(scope: Any, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def _scope(*, scheme: str = "http", headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {"type": "http", "scheme": scheme, "headers": headers or []}


async def _headers_for(scope: dict) -> dict[bytes, bytes]:
    """Run the middleware over `scope` and return the response headers."""
    captured: dict[bytes, bytes] = {}

    async def send(message: Any) -> None:
        if message["type"] == "http.response.start":
            captured.update(dict(message["headers"]))

    await SecurityHeadersMiddleware(_ok_app)(scope, None, send)
    return captured


# ---------------------------------------------------------------- the defect


@pytest.mark.asyncio
async def test_plain_http_gets_no_hsts() -> None:
    """The whole point. A plaintext response must not pin anything."""
    assert HSTS not in await _headers_for(_scope())


@pytest.mark.asyncio
async def test_forwarded_https_gets_hsts() -> None:
    """nginx terminates TLS, so this is the only way production ever says yes."""
    headers = await _headers_for(
        _scope(headers=[(b"x-forwarded-proto", b"https")])
    )
    assert headers[HSTS] == b"max-age=31536000; includeSubDomains"


@pytest.mark.asyncio
async def test_direct_https_gets_hsts() -> None:
    """Held for the day the app is ever served TLS directly."""
    assert HSTS in await _headers_for(_scope(scheme="https"))


@pytest.mark.asyncio
async def test_forwarded_http_gets_no_hsts() -> None:
    """The header being present is not the question; its value is."""
    assert HSTS not in await _headers_for(
        _scope(headers=[(b"x-forwarded-proto", b"http")])
    )


# ------------------------------------------------------- reading the header


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"https", True),
        (b"HTTPS", True),  # the grammar is case-insensitive
        (b"  https  ", True),  # nginx does not pad it; something upstream might
        (b"https,http", True),  # a proxy chain appends, so the client is first
        (b"https, http", True),
        (b"http,https", False),  # ...and this one reached us over plaintext
        (b"http", False),
        (b"", False),
        (b"httpsx", False),  # prefix match would be wrong here
    ],
)
def test_forwarded_proto_is_read_exactly(value: bytes, expected: bool) -> None:
    assert _is_https(_scope(headers=[(b"x-forwarded-proto", value)])) is expected


def test_absent_header_is_not_https() -> None:
    """Fails closed: no evidence of TLS is not evidence of TLS."""
    assert _is_https(_scope()) is False


# ------------------------------------------- the headers that are NOT conditional


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_every_other_header_is_unconditional(scheme: str) -> None:
    """Only HSTS may depend on the scheme.

    Guards the obvious bad fix for the defect above -- moving the whole block
    behind the `if` -- which would silently drop the CSP and the framing and
    sniffing guards on exactly the plaintext responses that need them most.
    """
    headers = await _headers_for(_scope(scheme=scheme))
    for name in (
        b"x-content-type-options",
        b"x-frame-options",
        b"referrer-policy",
        b"cross-origin-opener-policy",
        b"permissions-policy",
        b"content-security-policy",
    ):
        assert name in headers, name


@pytest.mark.asyncio
async def test_non_http_scopes_pass_through_untouched() -> None:
    """A websocket scope has no response headers to decorate."""
    seen: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope)

    await SecurityHeadersMiddleware(app)({"type": "websocket"}, None, None)
    assert seen == [{"type": "websocket"}]
