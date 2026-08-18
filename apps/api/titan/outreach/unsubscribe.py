"""Opt-out links Titan issues, signed so only Titan can issue them.

The recipient's address travels in the URL. Without a signature anybody could
unsubscribe anybody by editing it -- a competitor could walk a list through the
endpoint and end every conversation silently, and nothing would distinguish
that from real requests.

The signature is an HMAC-SHA256 over the normalised address, base64url with the
padding stripped, using a secret shared with the endpoint that receives the
click. **The algorithm has to match that endpoint byte for byte**: a mismatch
does not fail loudly here, it produces links that every recipient finds broken,
at the moment they have already decided to leave. There is a test that pins the
exact output against a value computed independently.

Normalisation is lower-cased and trimmed on both sides, so a header written
with different casing than the body still verifies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import quote

from pydantic import SecretStr

#: Callers pass the setting itself rather than its value. Concentrating the
#: unwrapping here is what keeps the repository invariant meaningful: one module
#: handles the raw secret, in the same way a provider client does, and nothing
#: else in the codebase can put it into a log line or an f-string by accident.
Secret = SecretStr | str


def _raw(secret: Secret) -> str:
    return secret.get_secret_value() if isinstance(secret, SecretStr) else secret


def normalize(email: str) -> str:
    """Case and surrounding whitespace are not part of a mailbox's identity."""
    return (email or "").strip().lower()


def sign(email: str, secret: Secret) -> str:
    """The token proving this address was given an opt-out link by us."""
    mac = hmac.new(
        _raw(secret).encode("utf-8"), normalize(email).encode("utf-8"), hashlib.sha256
    ).digest()
    # base64url without padding, matching the endpoint. The `=` would be
    # percent-encoded in a query string and some clients drop it, which is why
    # neither side keeps it.
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def link(email: str, *, base_url: str, secret: Secret, path: str = "/unsubscribe") -> str:
    """The address a person clicks, or a mail client POSTs to.

    ``path`` is the only difference between the two: the body link goes to the
    page, the ``List-Unsubscribe`` header to the API route that acts without
    rendering anything.
    """
    root = (base_url or "").rstrip("/")
    return (
        f"{root}{path}?e={quote(normalize(email), safe='@')}"
        f"&t={quote(sign(email, secret), safe='')}"
    )


def one_click_url(email: str, *, base_url: str, secret: Secret) -> str:
    """The RFC 8058 target. Must accept POST and act without confirmation."""
    return link(email, base_url=base_url, secret=secret, path="/api/unsubscribe")


def bearer(secret: Secret) -> str:
    """The Authorization header value for the opt-out list endpoint.

    Built here for the same reason the tokens are: so the caller never holds the
    raw string.
    """
    return f"Bearer {_raw(secret)}"


__all__ = ["Secret", "bearer", "link", "normalize", "one_click_url", "sign"]
