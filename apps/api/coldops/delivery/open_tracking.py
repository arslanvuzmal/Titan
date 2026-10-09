"""Whether a particular message was opened, and why it is switched off.

``messages.first_opened_at`` has existed since the first migration and has
never once been written. Nothing populated it, because populating it means
putting a one-pixel image in every message and watching for the fetch -- and
:mod:`coldops.delivery.placement` says plainly why that was refused:

    the alternatives for measuring engagement -- open pixels and redirect
    links -- are themselves spam signals, and adding them to a domain already
    in trouble would deepen exactly the hole being measured.

That argument has not gone away. On 27 September this domain got one message
into one inbox after six straight readings of spam, and a tracking pixel is
among the cheapest ways to lose that again: filters score remote images in
first-contact mail, and several strip or warn on them outright.

So this is built and **off**. ``open_tracking_enabled`` defaults to False, no
message carries a pixel until somebody sets it, and the setting's docstring
names the condition -- placement holding for a week -- rather than leaving the
decision to whoever is next in the file.

**What an open actually measures, and does not.** Apple Mail Privacy Protection
pre-fetches images for every message it receives, so an "open" from an Apple
client means the mail arrived, not that a person read it. Gmail proxies images
through its own cache, which fires once and hides whether it was opened again.
An open is therefore evidence of delivery and weak evidence of attention. It is
recorded as ``first_opened_at`` -- first, singular -- because a count of opens
from a proxied cache would be a number with no meaning at all.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import uuid

from pydantic import SecretStr
from sqlalchemy import text

logger = logging.getLogger(__name__)

#: A 1x1 transparent GIF, 43 bytes. Inline rather than a file on disk: it never
#: changes, and a missing asset would turn every open into a 404 in the logs.
PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
PIXEL_CONTENT_TYPE = "image/gif"


def _raw(secret: SecretStr | str) -> str:
    """The one place a tracking secret is unwrapped.

    Every caller passes the ``SecretStr`` itself, so the value exists in
    plain form only inside this module -- the same concentration the
    provider clients and the unsubscribe signer rely on. A second unwrap
    somewhere else is a second place it can reach a log line.
    """
    return secret.get_secret_value() if isinstance(secret, SecretStr) else secret


def open_token(message_id: uuid.UUID, secret: SecretStr | str) -> str:
    """A token identifying the message, that cannot be forged or enumerated.

    Signed rather than the raw id. A bare message id in an image URL is an
    invitation to walk the range and mark every message in the estate as
    opened, which would not merely add noise -- it would make the one number
    anybody trusts permanently wrong, with no way to tell the real opens from
    the walked ones.

    Truncated to 16 hex characters. Full-length would be 64 characters of URL
    in every message, and a forgery still needs the secret; the shortening
    costs collision resistance that nothing here depends on.
    """
    digest = hmac.new(
        _raw(secret).encode(), str(message_id).encode(), hashlib.sha256
    ).hexdigest()
    return f"{message_id}.{digest[:16]}"


def verify_open_token(token: str, secret: SecretStr | str) -> uuid.UUID | None:
    """The message this token refers to, or None if it was not signed by us."""
    raw, _, signature = token.partition(".")
    if not signature:
        return None
    try:
        message_id = uuid.UUID(raw)
    except ValueError:
        return None
    expected = open_token(message_id, secret).partition(".")[2]
    # Constant-time, because the comparison is against a secret-derived value
    # and an early return would leak it a character at a time.
    if not hmac.compare_digest(signature, expected):
        return None
    return message_id


def pixel_html(base_url: str, message_id: uuid.UUID, secret: SecretStr | str) -> str:
    """The tag to append to the HTML body.

    ``width``/``height`` as attributes as well as in the style, because several
    clients drop inline CSS; ``alt=""`` so a screen reader passes over it
    rather than announcing an untitled image; ``aria-hidden`` for the same
    reason. A pixel a blind reader has to listen to is worse than no pixel.
    """
    url = f"{base_url.rstrip('/')}/o/{open_token(message_id, secret)}.gif"
    return (
        f'<img src="{url}" width="1" height="1" alt="" aria-hidden="true" '
        f'style="width:1px;height:1px;border:0;display:block" />'
    )


async def record_open(session, *, message_id: uuid.UUID) -> bool:
    """Stamp the first open. True when this was the first one.

    **Unscoped by a workspace, deliberately, and allowlisted as such.** The
    caller is a mail client fetching an image; there is no session, no
    principal and no workspace to scope to. What stands in for it is the
    predicate: a primary key, reachable only from a token this estate
    signed. ``coalesce`` makes the write idempotent, so the worst a
    cross-workspace id could do is set a timestamp that was already set.

    Set once and never moved: a second fetch is Gmail's cache refreshing
    or Apple pre-fetching again, not a second reading, so overwriting
    would turn a timestamp that means something into one that means "most
    recent proxy hit".

    Returns rather than raises on an unknown id: the caller serves a pixel
    either way, because an image request that 404s tells whoever sent it that
    the id was wrong, and the id space is exactly what the signature exists to
    protect.
    """
    result = await session.execute(
        text(
            """
            UPDATE messages
               SET first_opened_at = coalesce(first_opened_at, now())
             WHERE id = :id AND first_opened_at IS NULL
            """
        ),
        {"id": message_id},
    )
    return bool(result.rowcount)


__all__ = [
    "PIXEL",
    "PIXEL_CONTENT_TYPE",
    "open_token",
    "pixel_html",
    "record_open",
    "verify_open_token",
]
