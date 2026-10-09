"""Reading an Outlook inbox by Microsoft token, against a fake Microsoft."""

from __future__ import annotations

import pathlib
import tempfile

import httpx
import pytest
from coldops.delivery import microsoft_oauth as mo
from coldops.delivery.mailboxes import MailboxConfigError, _endpoint

ADDRESS = "coldops.seed@outlook.com"


def _fake(responses: list[tuple[int, dict]]) -> tuple[httpx.Client, list[dict]]:
    """A client whose server answers from a list, recording what was asked."""
    asked: list[dict] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(dict(httpx.QueryParams(request.content.decode())))
        status, body = queue.pop(0)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), asked


def _store() -> mo.TokenStore:
    # Not pytest's tmp_path: its base directory is refused on the dev laptop.
    return mo.TokenStore(tempfile.mkdtemp(prefix="titan-oauth-"))


def setup_function() -> None:
    mo._cache.clear()


def test_the_device_flow_waits_through_pending_then_returns_the_tokens() -> None:
    client, asked = _fake(
        [
            (
                200,
                {
                    "device_code": "dc",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "interval": 1,
                    "expires_in": 60,
                },
            ),
            (400, {"error": "authorization_pending"}),
            (200, {"access_token": "at", "refresh_token": "rt-1", "expires_in": 3600}),
        ]
    )
    code = mo.start_device_flow("client-1", http=client)
    assert code.user_code == "ABCD-EFGH"
    assert "IMAP.AccessAsUser.All" in asked[0]["scope"]
    reply = mo.wait_for_sign_in("client-1", code, http=client, sleep=lambda _s: None)
    assert reply["refresh_token"] == "rt-1"
    assert asked[-1]["grant_type"] == mo.DEVICE_GRANT


def test_a_refusal_carries_microsofts_reason() -> None:
    client, _ = _fake(
        [
            (
                400,
                {
                    "error": "invalid_client",
                    "error_description": "AADSTS700016: app not found\r\nTrace",
                },
            )
        ]
    )
    with pytest.raises(
        mo.OAuthError, match="invalid_client -- AADSTS700016: app not found"
    ):
        mo.start_device_flow("wrong", http=client)


def test_a_rotated_refresh_token_is_kept_and_used_next_time() -> None:
    store = _store()
    client, asked = _fake(
        [
            (200, {"access_token": "at-1", "refresh_token": "rt-2", "expires_in": 3600}),
            (200, {"access_token": "at-2", "refresh_token": "rt-3", "expires_in": 3600}),
        ]
    )
    first = mo.access_token(
        address=ADDRESS, client_id="c", refresh_token="rt-1", store=store, http=client
    )
    assert first == "at-1"
    assert asked[0]["refresh_token"] == "rt-1"
    assert store.load(ADDRESS) == "rt-2"

    mo._cache.clear()
    mo.access_token(
        address=ADDRESS, client_id="c", refresh_token="rt-1", store=store, http=client
    )
    assert asked[1]["refresh_token"] == "rt-2", (
        "the stored, newer token wins over the file's"
    )


def test_a_live_access_token_is_reused_without_asking_again() -> None:
    client, asked = _fake([(200, {"access_token": "at", "expires_in": 3600})])
    store = _store()
    for _ in range(3):
        assert (
            mo.access_token(
                address=ADDRESS,
                client_id="c",
                refresh_token="rt",
                store=store,
                http=client,
            )
            == "at"
        )
    assert len(asked) == 1


def test_the_store_keeps_its_files_to_its_own_directory() -> None:
    store = _store()
    assert store.save("../../etc/passwd", "rt")
    path = store._path("../../etc/passwd")
    # No separator survives, so the file cannot leave the directory.
    assert path is not None and "/" not in path.name and "\\" not in path.name
    assert pathlib.Path(path).parent == store._dir


def test_imap_login_uses_xoauth2_for_microsoft_and_a_password_otherwise(
    monkeypatch,
) -> None:
    calls: list[tuple] = []

    class FakeImap:
        def login(self, user, password):
            calls.append(("login", user, password))

        def authenticate(self, mechanism, responder):
            calls.append(("auth", mechanism, responder(b"")))

    mo.imap_login(FakeImap(), username="a@spacemail.com", password="pw")
    monkeypatch.setattr(mo, "access_token", lambda **_k: "token-x")
    mo.imap_login(
        FakeImap(),
        username=ADDRESS,
        password="rt",
        auth=mo.AUTH_KIND,
        client_id="c",
    )
    assert calls[0] == ("login", "a@spacemail.com", "pw")
    assert calls[1][0:2] == ("auth", "XOAUTH2")
    assert calls[1][2] == f"user={ADDRESS}\x01auth=Bearer token-x\x01\x01".encode()


def test_the_file_must_name_the_app_for_microsoft_oauth() -> None:
    raw = {"host": "outlook.office365.com", "password": "rt", "auth": "microsoft_oauth"}
    with pytest.raises(MailboxConfigError, match="client_id"):
        _endpoint(raw, where="seed 0", default_port=993, fallback_username=ADDRESS)
    raw["client_id"] = "c"
    endpoint = _endpoint(raw, where="seed 0", default_port=993, fallback_username=ADDRESS)
    assert endpoint.auth == "microsoft_oauth" and endpoint.client_id == "c"
    assert endpoint.redacted()["auth"] == "microsoft_oauth"


def test_an_unknown_auth_is_refused() -> None:
    raw = {"host": "imap.example", "password": "x", "auth": "magic"}
    with pytest.raises(MailboxConfigError, match="auth must be"):
        _endpoint(raw, where="seed 0", default_port=993, fallback_username="a@b.c")
