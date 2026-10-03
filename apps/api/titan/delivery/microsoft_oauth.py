"""Reading a personal Outlook/Hotmail inbox, which no longer takes a password.

Microsoft stopped accepting basic authentication over IMAP for personal
Outlook, Hotmail, Live and MSN accounts on 16 September 2024; an app password
returns NO LOGIN. Outlook is the largest single bucket on the send list, so a
placement round that cannot read an Outlook inbox measures everyone except the
biggest audience. This is the way in that Microsoft still allows: OAuth 2.0.

**Three pieces, and only one needs a person.**

1. Once: an app registration in Azure (free, under the operator's own
   Microsoft account), "Personal Microsoft accounts only", public client flows
   allowed. It yields a client id, which is not a secret.
2. Once per inbox: the device-code sign-in. ``titan oauth-microsoft`` prints a
   short code and a Microsoft URL; the operator opens it, signs in to *that*
   inbox, and agrees. Titan never sees the account's password. What comes back
   is a refresh token.
3. Every read after that: the refresh token is exchanged for a one-hour access
   token, and IMAP logs in with XOAUTH2. No person involved.

**Refresh tokens rotate.** Each exchange returns a new one, and a token left
unused expires after about 90 days. When a writable token directory is
configured (``TITAN_OAUTH_TOKEN_DIR``) the newest token is kept there and used
next time, so an inbox read daily never expires. Without one, the token from
the file is used every time and the sign-in has to be repeated every ~90 days.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: "consumers": personal Microsoft accounts. Work accounts are not seeds.
AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
DEVICE_URL = f"{AUTHORITY}/devicecode"
TOKEN_URL = f"{AUTHORITY}/token"
#: IMAP access, and offline_access so a refresh token comes back at all.
SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

#: The value of an endpoint's ``auth`` field that selects this module.
AUTH_KIND = "microsoft_oauth"

#: Access tokens last an hour; refreshed this long before they lapse.
_EARLY = 300


class OAuthError(RuntimeError):
    """Microsoft refused, with its own explanation attached."""


@dataclass(frozen=True, slots=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int
    message: str


def start_device_flow(client_id: str, *, http: httpx.Client | None = None) -> DeviceCode:
    """Ask Microsoft for a code the operator types in at its sign-in page."""
    with _client(http) as c:
        r = c.post(DEVICE_URL, data={"client_id": client_id, "scope": SCOPE})
    body = _json(r)
    if r.status_code != 200:
        raise OAuthError(_explain(body))
    return DeviceCode(
        device_code=body["device_code"],
        user_code=body["user_code"],
        verification_uri=body.get(
            "verification_uri", "https://microsoft.com/devicelogin"
        ),
        interval=int(body.get("interval", 5)),
        expires_in=int(body.get("expires_in", 900)),
        message=body.get("message", ""),
    )


def wait_for_sign_in(
    client_id: str,
    code: DeviceCode,
    *,
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll until the operator has signed in. Returns Microsoft's token reply."""
    deadline = time.monotonic() + code.expires_in
    interval = code.interval
    with _client(http) as c:
        while time.monotonic() < deadline:
            sleep(interval)
            r = c.post(
                TOKEN_URL,
                data={
                    "grant_type": DEVICE_GRANT,
                    "client_id": client_id,
                    "device_code": code.device_code,
                },
            )
            body = _json(r)
            if r.status_code == 200:
                return body
            error = body.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            raise OAuthError(_explain(body))
    raise OAuthError(
        "the sign-in code expired before anyone used it; run the command again"
    )


# ------------------------------------------------------------------ storage
_SAFE = re.compile(r"[^a-z0-9@._-]")


class TokenStore:
    """The newest refresh token per address, in a directory only Titan writes."""

    def __init__(self, directory: str | None) -> None:
        self._dir = pathlib.Path(directory) if directory else None

    def _path(self, address: str) -> pathlib.Path | None:
        if self._dir is None:
            return None
        return self._dir / f"{_SAFE.sub('_', address.strip().lower())}.json"

    def load(self, address: str) -> str | None:
        path = self._path(address)
        if path is None or not path.exists():
            return None
        try:
            return str(json.loads(path.read_text(encoding="utf-8"))["refresh_token"])
        except (OSError, ValueError, KeyError):
            return None

    def save(self, address: str, refresh_token: str) -> bool:
        path = self._path(address)
        if path is None:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps({"refresh_token": refresh_token}), encoding="utf-8"
            )
            temp.chmod(0o600)
            temp.replace(path)
            return True
        except OSError:
            logger.warning("could not keep a rotated Microsoft refresh token")
            return False


# ------------------------------------------------------------------ access
_cache: dict[str, tuple[str, float]] = {}


def access_token(
    *,
    address: str,
    client_id: str,
    refresh_token: str,
    store: TokenStore,
    http: httpx.Client | None = None,
    now: Callable[[], float] = time.time,
) -> str:
    """A current access token for this inbox, refreshing when needed.

    The stored token wins over the one in the credentials file: it is newer.
    """
    key = address.strip().lower()
    cached = _cache.get(key)
    if cached and cached[1] - _EARLY > now():
        return cached[0]
    current = store.load(address) or refresh_token
    with _client(http) as c:
        r = c.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": current,
                "scope": SCOPE,
            },
        )
    body = _json(r)
    if r.status_code != 200:
        raise OAuthError(_explain(body))
    token = str(body["access_token"])
    _cache[key] = (token, now() + int(body.get("expires_in", 3600)))
    rotated = body.get("refresh_token")
    if rotated and rotated != current:
        store.save(address, str(rotated))
    return token


def imap_login(
    client: Any,
    *,
    username: str,
    password: str,
    auth: str = "password",
    client_id: str | None = None,
) -> None:
    """Log an IMAP connection in, by password or by Microsoft token.

    The one place every reader logs in, so a mailbox's ``auth`` setting means
    the same thing to the reply collector, the placement round and warm-up.
    For ``microsoft_oauth`` the ``password`` field holds the first refresh
    token, from the sign-in.
    """
    if auth != AUTH_KIND:
        client.login(username, password)
        return
    if not client_id:
        raise OAuthError(f"{username}: microsoft_oauth needs a client_id")
    from titan.config import get_settings

    token = access_token(
        address=username,
        client_id=client_id,
        refresh_token=password,
        store=TokenStore(get_settings().oauth_token_dir),
    )
    client.authenticate("XOAUTH2", lambda _challenge: xoauth2(username, token))


def xoauth2(address: str, token: str) -> bytes:
    """The SASL XOAUTH2 initial response IMAP expects."""
    return f"user={address}\x01auth=Bearer {token}\x01\x01".encode()


# ------------------------------------------------------------------ helpers
def _client(http: httpx.Client | None):  # type: ignore[no-untyped-def]
    if http is not None:
        return _Borrowed(http)
    return httpx.Client(timeout=30.0)


class _Borrowed:
    """A caller's client, used without closing it."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __enter__(self) -> httpx.Client:
        return self._client

    def __exit__(self, *exc: object) -> None:
        return None


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {"error": f"http_{response.status_code}"}
    return body if isinstance(body, dict) else {}


def _explain(body: dict[str, Any]) -> str:
    error = body.get("error", "unknown_error")
    detail = str(body.get("error_description", "")).split("\r\n")[0]
    return f"Microsoft refused: {error}" + (f" -- {detail}" if detail else "")


__all__ = [
    "AUTH_KIND",
    "SCOPE",
    "DeviceCode",
    "OAuthError",
    "TokenStore",
    "access_token",
    "imap_login",
    "start_device_flow",
    "wait_for_sign_in",
    "xoauth2",
]
