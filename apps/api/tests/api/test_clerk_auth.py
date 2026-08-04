"""Clerk token verification tests.

The verifier is the front door for a deployed Titan, so these concentrate on
what it must *refuse*. A verifier that accepts the right token is easy; one
that rejects every wrong token is the security property.

Keys are generated per-test and served from a local JWKS fixture, so nothing
here touches Clerk.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select
from titan.api.clerk import JWKS_PATH, ClerkAuthError, ClerkVerifier

ISSUER = "https://clerk.titan-fixture.test"


def make_keypair() -> tuple[rsa.RSAPrivateKey, dict]:
    """An RSA key plus its JWKS entry."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    numbers = public.public_numbers()

    def b64(value: int) -> str:
        import base64

        length = (value.bit_length() + 7) // 8
        return (
            base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode()
        )

    kid = uuid.uuid4().hex
    return private, {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": b64(numbers.n),
        "e": b64(numbers.e),
    }


def sign(
    private: rsa.RSAPrivateKey,
    jwk: dict,
    *,
    issuer: str = ISSUER,
    subject: str = "user_clerk_123",
    email: str | None = "operator@titan-fixture.test",
    email_verified: bool = True,
    expires_in: int = 600,
    algorithm: str = "RS256",
    key: object | None = None,
) -> str:
    now = dt.datetime.now(dt.UTC)
    claims: dict = {
        "sub": subject,
        "iss": issuer,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=expires_in)).timestamp()),
        "sid": "sess_1",
    }
    if email is not None:
        claims["email"] = email
        claims["email_verified"] = email_verified
    return jwt.encode(
        claims,
        key if key is not None else private,  # type: ignore[arg-type]
        algorithm=algorithm,
        headers={"kid": jwk["kid"]},
    )


@pytest.fixture
def jwks_server():
    """A verifier wired to an in-process JWKS, plus the fetch counter.

    `state["keys"]` can be swapped mid-test to simulate a key rotation, and
    `state["fetches"]` records how often the issuer was actually contacted --
    which is the property the rate-limiting test asserts on.
    """
    private, jwk = make_keypair()
    state: dict = {"fetches": 0, "keys": [jwk]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{ISSUER}{JWKS_PATH}"
        state["fetches"] += 1
        return httpx.Response(200, json={"keys": state["keys"]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    verifier = ClerkVerifier(ISSUER, client=client)
    yield verifier, private, jwk, state
    client.close()


# ==========================================================================
# The happy path, so the refusals below mean something
# ==========================================================================
def test_a_valid_token_yields_identity_only(jwks_server) -> None:
    verifier, private, jwk, _ = jwks_server
    identity = verifier.verify(sign(private, jwk))

    assert identity.subject == "user_clerk_123"
    assert identity.email == "operator@titan-fixture.test"
    assert identity.email_verified is True
    # The point of the type: there is no role and no workspace on it, so a
    # forged claim has nowhere to land.
    assert not hasattr(identity, "role")
    assert not hasattr(identity, "workspace_id")


# ==========================================================================
# Algorithm confusion
# ==========================================================================
def test_the_none_algorithm_is_refused(jwks_server) -> None:
    """`alg: none` skips signature verification entirely."""
    verifier, _, jwk = jwks_server[0], jwks_server[1], jwks_server[2]
    now = dt.datetime.now(dt.UTC)
    unsigned = jwt.encode(
        {
            "sub": "attacker",
            "iss": ISSUER,
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(seconds=600)).timestamp()),
        },
        key="",
        algorithm="none",
        headers={"kid": jwk["kid"]},
    )
    with pytest.raises(ClerkAuthError):
        verifier.verify(unsigned)


def test_an_hmac_token_signed_with_the_public_key_is_refused(jwks_server) -> None:
    """The classic RS256/HS256 confusion.

    The public key is public. If the verifier honoured the token's own `alg`,
    an attacker could sign with that public key as an HMAC secret and be
    believed.
    """
    verifier, private, jwk, _ = jwks_server
    import base64
    import hashlib
    import hmac

    from cryptography.hazmat.primitives import serialization

    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Assembled by hand: PyJWT refuses to *sign* with an asymmetric key as an
    # HMAC secret, so the forgery has to be built without it. The verifier is
    # what is under test, not the signer.
    def segment(payload: dict) -> bytes:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    now = dt.datetime.now(dt.UTC)
    header = segment({"alg": "HS256", "typ": "JWT", "kid": jwk["kid"]})
    body = segment(
        {
            "sub": "attacker",
            "iss": ISSUER,
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(seconds=600)).timestamp()),
        }
    )
    signing_input = header + b"." + body
    signature = base64.urlsafe_b64encode(
        hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    forged = (signing_input + b"." + signature).decode()

    with pytest.raises(ClerkAuthError):
        verifier.verify(forged)


# ==========================================================================
# Issuer, expiry, signature
# ==========================================================================
def test_a_token_from_another_issuer_is_refused(jwks_server) -> None:
    """Otherwise any Clerk tenant mints valid tokens for this deployment."""
    verifier, private, jwk, _ = jwks_server
    other = sign(private, jwk, issuer="https://clerk.someone-else.test")
    with pytest.raises(ClerkAuthError):
        verifier.verify(other)


def test_an_expired_token_is_refused(jwks_server) -> None:
    verifier, private, jwk, _ = jwks_server
    with pytest.raises(ClerkAuthError, match="expired"):
        verifier.verify(sign(private, jwk, expires_in=-3600))


def test_a_token_signed_by_an_unpublished_key_is_refused(jwks_server) -> None:
    """A valid-looking token whose key the issuer never published."""
    verifier, _, jwk, _ = jwks_server
    attacker_key, attacker_jwk = make_keypair()
    # Claim the *published* kid while signing with a different key.
    forged = sign(attacker_key, jwk)
    with pytest.raises(ClerkAuthError):
        verifier.verify(forged)
    del attacker_jwk


def test_a_token_with_no_subject_is_refused(jwks_server) -> None:
    verifier, private, jwk, _ = jwks_server
    now = dt.datetime.now(dt.UTC)
    no_sub = jwt.encode(
        {
            "iss": ISSUER,
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(seconds=600)).timestamp()),
        },
        private,  # type: ignore[arg-type]
        algorithm="RS256",
        headers={"kid": jwk["kid"]},
    )
    with pytest.raises(ClerkAuthError):
        verifier.verify(no_sub)


def test_garbage_is_refused(jwks_server) -> None:
    verifier = jwks_server[0]
    for token in ("", "not-a-jwt", "a.b.c", json.dumps({"sub": "x"})):
        with pytest.raises(ClerkAuthError):
            verifier.verify(token)


# ==========================================================================
# Key rotation and refetch behaviour
# ==========================================================================
def test_key_rotation_is_picked_up_by_one_refetch(jwks_server) -> None:
    """Clerk rotates keys; a single unknown kid must not lock everyone out."""
    verifier, private, jwk, state = jwks_server
    verifier.verify(sign(private, jwk))
    fetches_before = state["fetches"]

    rotated_key, rotated_jwk = make_keypair()
    state["keys"] = [rotated_jwk]

    identity = verifier.verify(sign(rotated_key, rotated_jwk))
    assert identity.subject == "user_clerk_123"
    assert state["fetches"] > fetches_before, "the new key set was never fetched"


def test_repeated_unknown_keys_do_not_refetch_every_time(jwks_server) -> None:
    """A stream of forged tokens must not become a request amplifier."""
    verifier, _, _, state = jwks_server
    attacker_key, attacker_jwk = make_keypair()
    token = sign(attacker_key, attacker_jwk)

    with pytest.raises(ClerkAuthError):
        verifier.verify(token)
    after_first = state["fetches"]

    for _ in range(10):
        with pytest.raises(ClerkAuthError):
            verifier.verify(token)

    assert state["fetches"] == after_first, (
        f"{state['fetches'] - after_first} extra JWKS fetches from forged tokens"
    )


# ==========================================================================
# Email binding
# ==========================================================================
def test_an_unverified_email_is_reported_as_unverified(jwks_server) -> None:
    """The account-binding path keys off this flag, so it must not be lenient."""
    verifier, private, jwk, _ = jwks_server
    identity = verifier.verify(sign(private, jwk, email_verified=False))
    assert identity.email_verified is False


def test_a_missing_email_verified_claim_is_not_treated_as_verified(
    jwks_server,
) -> None:
    verifier, private, jwk, _ = jwks_server
    now = dt.datetime.now(dt.UTC)
    token = jwt.encode(
        {
            "sub": "user_x",
            "iss": ISSUER,
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(seconds=600)).timestamp()),
            "email": "someone@titan-fixture.test",
        },
        private,  # type: ignore[arg-type]
        algorithm="RS256",
        headers={"kid": jwk["kid"]},
    )
    identity = verifier.verify(token)
    assert identity.email_verified is False


def test_jwks_url_is_derived_from_the_issuer() -> None:
    assert (
        ClerkVerifier("https://clerk.example.test/").jwks_url
        == "https://clerk.example.test/.well-known/jwks.json"
    )


# ==========================================================================
# The request path: identity in, database-authoritative role out
# ==========================================================================
@pytest_asyncio.fixture
async def clerk_client(jwks_server, monkeypatch):
    """The real ASGI app, in clerk auth mode, wired to the fixture issuer."""
    import os

    os.environ.setdefault("TITAN_LOCAL_JWT_SECRET", "test-secret-not-for-production")
    # Settings is frozen by design, so the mode is switched through the
    # environment it actually reads rather than by mutating the instance.
    monkeypatch.setenv("TITAN_AUTH_MODE", "clerk")
    monkeypatch.setenv("TITAN_CLERK_ISSUER_URL", ISSUER)

    from titan.config import get_settings

    get_settings.cache_clear()

    from titan.api import security

    monkeypatch.setattr(security, "get_clerk_verifier", lambda *_a, **_k: jwks_server[0])

    from titan.api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    get_settings.cache_clear()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unknown_subject_gets_no_account(clerk_client, jwks_server) -> None:
    """Nothing is provisioned implicitly by presenting a valid Clerk token."""
    _, private, jwk, _ = jwks_server
    token = sign(private, jwk, subject="user_nobody", email="nobody@nowhere.test")
    response = await clerk_client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_verified_email_binds_to_an_existing_account_once(
    clerk_client, jwks_server, workspace
) -> None:
    """The intended first sign-in: an operator-created account, claimed."""
    from titan.db.enums import WorkspaceRole
    from titan.db.models import User
    from titan.db.session import get_sessionmaker

    from tests.api.test_api_security import make_member

    _, email = await make_member(workspace, WorkspaceRole.ADMIN, tag="clerkbind")
    _, private, jwk, _ = jwks_server
    # Unique per run: `users` is not workspace-scoped, so a row bound to a
    # fixed subject outlives the workspace fixture and would be found by the
    # next run with no membership attached.
    subject = f"user_bind_{uuid.uuid4().hex[:10]}"
    token = sign(private, jwk, subject=subject, email=email)

    response = await clerk_client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == email
    # The role came from workspace_members, not from any token claim.
    assert body["role"] == "admin"

    async with get_sessionmaker()() as session:
        user = (
            await session.execute(
                sa_select(User).where(sa_func.lower(User.email) == email.lower())
            )
        ).scalar_one()
        assert user.external_subject == subject, "the binding was not recorded"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unverified_email_cannot_claim_an_account(
    clerk_client, jwks_server, workspace
) -> None:
    """Otherwise anyone who can assert an address takes over that account."""
    from titan.db.enums import WorkspaceRole

    from tests.api.test_api_security import make_member

    _, email = await make_member(workspace, WorkspaceRole.OWNER, tag="unverified")
    _, private, jwk, _ = jwks_server
    token = sign(private, jwk, subject="user_attacker", email=email, email_verified=False)

    response = await clerk_client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_forged_token_never_reaches_the_database(
    clerk_client, jwks_server
) -> None:
    attacker_key, attacker_jwk = make_keypair()
    forged = sign(attacker_key, attacker_jwk, subject=f"user_{uuid.uuid4().hex[:10]}")
    response = await clerk_client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {forged}"}
    )
    assert response.status_code == 401
