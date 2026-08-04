"""Clerk token verification.

Verifies a Clerk-issued session JWT against the issuer's published JWKS, and
returns only the *identity* it proves. It deliberately returns no role and no
workspace: those are read from ``workspace_members`` on every request, because
a token outlives a revocation and embedding either would recreate the staleness
problem the local path already avoids (gap analysis H-12).

What this refuses, and why each matters:

* **Any algorithm other than RS256.** Accepting the token's own ``alg`` is the
  classic confusion attack -- ``none`` skips verification entirely, and ``HS256``
  lets an attacker sign with the *public* key as an HMAC secret.
* **An issuer that is not the configured one.** Otherwise any Clerk tenant in
  the world mints valid tokens for this deployment.
* **An unknown key id**, after one bounded refetch. Clerk rotates keys, so a
  single miss is normal and must be tolerated; repeated misses are not.
* **A token whose subject is not already a Titan user.** Nothing is created
  implicitly. Who may reach this system stays a deliberate act.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWK

logger = logging.getLogger(__name__)

#: Clerk publishes its keys here, relative to the issuer.
JWKS_PATH = "/.well-known/jwks.json"

#: How long a fetched key set is trusted before it is fetched again. Clerk
#: rotates on the order of months; this only bounds how long a *revoked* key
#: stays usable after rotation.
JWKS_TTL_SECONDS = 600

#: Refetching on every unknown `kid` would turn a stream of forged tokens into
#: a request amplifier against Clerk. One refetch per interval is enough to
#: pick up a rotation.
JWKS_REFETCH_COOLDOWN_SECONDS = 30

#: Clerk's own clock skew allowance.
LEEWAY_SECONDS = 10


class ClerkAuthError(Exception):
    """Verification failed. The message is safe to log, not to return verbatim."""


@dataclass(frozen=True, slots=True)
class ClerkIdentity:
    """What a verified Clerk token proves. Identity only -- never authority."""

    subject: str
    email: str | None
    email_verified: bool
    session_id: str | None


class ClerkVerifier:
    """Caches the issuer's JWKS and verifies tokens against it.

    One instance per process. The key set is fetched and cached here rather
    than by ``PyJWKClient``, which refetches on *every* cache miss: a stream of
    tokens bearing unknown key ids would then turn this service into a request
    amplifier pointed at the issuer. Refetches are on a TTL, plus at most one
    per cooldown when a genuinely unknown key id appears.
    """

    def __init__(
        self,
        issuer: str,
        *,
        audience: str | None = None,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._timeout = timeout_seconds
        self._client = client
        self._lock = threading.Lock()
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0
        #: Zero means "never", so the first unknown key id always refetches.
        self._last_miss_refetch = 0.0

    @property
    def jwks_url(self) -> str:
        return f"{self._issuer}{JWKS_PATH}"

    def _fetch(self) -> dict[str, PyJWK]:
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            response = client.get(self.jwks_url)
            response.raise_for_status()
            document = response.json()
        except Exception as exc:
            raise ClerkAuthError(f"cannot reach the issuer's key set: {exc}") from exc
        finally:
            if self._client is None:
                client.close()

        keys: dict[str, PyJWK] = {}
        for entry in document.get("keys", []):
            kid = entry.get("kid")
            # Signature keys only. An encryption key in the set is not a
            # licence to verify a signature with it.
            if not kid or entry.get("use") not in (None, "sig"):
                continue
            try:
                keys[kid] = PyJWK.from_dict(entry)
            except Exception:
                # One malformed entry must not invalidate the whole set, and
                # the key material is not safe to log.
                logger.warning("skipping unusable JWKS entry kid=%s", kid)
                continue
        if not keys:
            raise ClerkAuthError("the issuer's key set contains no usable keys")
        return keys

    def _signing_key(self, kid: str | None) -> Any:
        if not kid:
            raise ClerkAuthError("token header carries no key id")

        with self._lock:
            now = time.monotonic()
            if not self._keys or now - self._fetched_at > JWKS_TTL_SECONDS:
                self._keys = self._fetch()
                self._fetched_at = now

            key = self._keys.get(kid)
            if key is not None:
                return key.key

            # An unknown kid is what a rotation looks like, and also what a
            # forged token looks like, and they cannot be told apart without
            # asking the issuer. So: always ask the first time, then throttle.
            #
            # Only this path arms the cooldown. Arming it on the scheduled
            # fetch above would mean a rotation landing just after one was
            # locked out for the whole interval, which is a real outage for a
            # defence against a hypothetical one.
            if now - self._last_miss_refetch >= JWKS_REFETCH_COOLDOWN_SECONDS:
                self._last_miss_refetch = now
                self._keys = self._fetch()
                self._fetched_at = now
                key = self._keys.get(kid)
                if key is not None:
                    return key.key

        raise ClerkAuthError(f"no published key matches key id {kid!r}")

    def verify(self, token: str) -> ClerkIdentity:
        """Verify and return the identity, or raise :class:`ClerkAuthError`."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise ClerkAuthError(f"malformed token: {type(exc).__name__}") from exc

        key = self._signing_key(header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key,
                # Fixed, never read from the token's own header.
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                leeway=LEEWAY_SECONDS,
                options={
                    "require": ["exp", "iat", "sub", "iss"],
                    "verify_aud": self._audience is not None,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise ClerkAuthError("token has expired") from exc
        except jwt.InvalidTokenError as exc:
            raise ClerkAuthError(f"invalid token: {type(exc).__name__}") from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise ClerkAuthError("token has no usable subject")

        email = claims.get("email")
        return ClerkIdentity(
            subject=subject,
            email=email if isinstance(email, str) else None,
            # Absent means unverified. An unverified address must never be
            # enough to bind a token to an existing account by email.
            email_verified=claims.get("email_verified") is True,
            session_id=claims.get("sid") if isinstance(claims.get("sid"), str) else None,
        )

    def health(self) -> bool:
        """Whether the issuer's key set is reachable and usable. Used by /ready."""
        try:
            self._fetch()
            return True
        except ClerkAuthError:
            return False


__all__ = [
    "JWKS_PATH",
    "JWKS_TTL_SECONDS",
    "ClerkAuthError",
    "ClerkIdentity",
    "ClerkVerifier",
]
