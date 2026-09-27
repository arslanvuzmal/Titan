"""The page an operator dials from.

Thin on purpose. The page composes no claims -- every sentence it shows comes
from ``/briefing``, which is where the staleness and suppression gates live.
What is worth holding in a test is that it stays that way, and that it keeps
asking for a token rather than carrying one.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient
from titan.api.call_sheet import _PAGE
from titan.api.main import app

client = TestClient(app)


def test_the_sheet_is_served_as_a_page():
    r = client.get("/api/v1/calls/sheet")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>Call sheet</title>" in r.text


def test_it_is_left_out_of_the_agent_contract():
    """The OpenAPI document describes what a voice agent may call. A dialling
    screen is not part of that contract and would only be noise in it."""
    assert "/api/v1/calls/sheet" not in app.openapi()["paths"]


def test_no_token_is_baked_into_the_file():
    """It lives in the repository; the token does not.

    Anything that looks like a long opaque secret in here is a mistake, so this
    fails on the shape rather than on any particular value.
    """
    assert "Bearer " in _PAGE  # it sends one
    assert "localStorage" in _PAGE  # and gets it from the operator's browser

    # Token-shaped: a long unbroken run mixing letters and digits. Comment
    # rules and CSS are long runs of one character, which is why the mix
    # matters rather than the length alone.
    for run in re.findall(r"[A-Za-z0-9_\-]{32,}", _PAGE):
        mixed = any(c.isdigit() for c in run) and any(c.isalpha() for c in run)
        assert not mixed, f"something secret-shaped is in the page: {run[:12]}..."


def test_the_page_never_invents_a_sentence_to_say():
    """Opener and ask are read from the briefing, not composed here.

    A screen that wrote its own opening line would route around the one gate
    that keeps a cold call truthful -- the API refuses stale, suppressed and
    already-called leads, and a claim assembled in the browser answers to
    nothing.
    """
    assert "b.opener" in _PAGE and "b.ask" in _PAGE
    for invented in ("Hi, I'm", "I noticed your", "We help", "Quick question about"):
        assert invented not in _PAGE


def test_the_dangerous_outcome_is_called_what_it_is():
    """`do_not_call` writes a permanent suppression before the next dial."""
    assert "Do not call again" in _PAGE
    assert "cannot be undone" in _PAGE


def test_the_page_does_not_claim_to_dial():
    """Nothing here places a call, and saying so stops a wrong assumption on
    the first morning."""
    assert "nothing here places the call" in _PAGE.lower()
