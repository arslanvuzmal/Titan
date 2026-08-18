"""Opt-out links, and the two ways they go wrong quietly.

The address travels in the URL, so an unsigned link lets anybody unsubscribe
anybody by editing it. And a signature that does not match the endpoint's does
not fail here -- it produces links every recipient finds broken, at the moment
they have already decided to leave.
"""

from __future__ import annotations

from urllib.parse import parse_qs

from titan.outreach.unsubscribe import link, normalize, one_click_url, sign

SECRET = "tVPCsDyTLWgZ0JAUbmoaLsIa4VqQ9iX88bR7BtsIh5s"
BASE = "https://arslanvuzmallone.com"


def test_the_signature_matches_the_endpoint() -> None:
    """Pinned against a value computed independently of this code.

    HMAC-SHA256 over the lower-cased address, base64url, padding stripped. If
    this ever changes, every link Titan issues starts returning 403 and the
    only symptom is recipients who cannot unsubscribe.
    """
    assert sign("titan-endpoint-check@example.com", SECRET) == (
        "Z7wCzBro6OOK3dIoxH-dTqrNMsDYR3AVzCl44wpCpq4"
    )


def test_padding_is_stripped() -> None:
    """`=` is percent-encoded in a query string and some clients drop it, so
    neither side keeps it."""
    assert "=" not in sign("a@b.com", SECRET)


def test_casing_does_not_change_the_token() -> None:
    """The header and the body are built from different sources; one of them
    having different casing must not produce a link that fails."""
    assert sign("A@B.com", SECRET) == sign("a@b.com", SECRET)
    assert normalize("  A@B.COM ") == "a@b.com"


def test_a_different_address_gets_a_different_token() -> None:
    """Otherwise one leaked link unsubscribes everybody."""
    assert sign("a@b.com", SECRET) != sign("c@d.com", SECRET)


def test_a_different_secret_gets_a_different_token() -> None:
    """Which is what rotating the secret is for."""
    assert sign("a@b.com", SECRET) != sign("a@b.com", "other")


def test_the_body_link_points_at_the_page() -> None:
    url = link("a@b.com", base_url=BASE, secret=SECRET)

    assert url.startswith(f"{BASE}/unsubscribe?")
    assert "e=a@b.com" in url
    assert "t=" in url


def test_the_header_link_points_at_the_api() -> None:
    """RFC 8058 requires a target that accepts POST and acts without rendering
    a page."""
    url = one_click_url("a@b.com", base_url=BASE, secret=SECRET)

    assert url.startswith(f"{BASE}/api/unsubscribe?")


def test_a_trailing_slash_on_the_base_does_not_double_up() -> None:
    assert "//unsubscribe" not in link("a@b.com", base_url=f"{BASE}/", secret=SECRET)


def test_the_token_survives_a_query_string() -> None:
    """base64url still contains characters worth encoding; a token mangled in
    transit is a 403 the recipient cannot diagnose."""
    url = link("a@b.com", base_url=BASE, secret=SECRET)
    token = parse_qs(url.split("?", 1)[1])["t"][0]

    assert token == sign("a@b.com", SECRET)
