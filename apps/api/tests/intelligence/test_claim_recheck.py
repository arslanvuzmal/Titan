"""Asking whether a claim is still true before asserting it.

The send gate asks how old a measurement is and never whether it still holds,
so a business that fixed its booking page a fortnight ago was still being told
it was broken. `audit_findings.contradicted` existed for exactly this and was
written by nothing.

Almost every test here guards one direction. A wrong "still broken" costs one
false claim to one business. A wrong "now fixed" silently discards a real lead,
and nothing downstream would ever show it happened -- so an inconclusive probe
must never be read as a fix.
"""

from __future__ import annotations

import pytest
from titan.intelligence.claim_recheck import (
    OK_BELOW,
    URL_STATUS_CLAIMS,
    Verdict,
    is_recheckable,
    judge,
)
from titan.providers.browser_client import RecheckResult

URL = "https://example.test/book"
CLAIM = "broken_internal_link"


def seen(**kw) -> RecheckResult:
    return RecheckResult(url=URL, **kw)


# ------------------------------------------------ the fix is caught
@pytest.mark.parametrize("status", [200, 201, 301, 302, 399])
def test_a_working_address_withdraws_the_claim(status: int) -> None:
    """Planted violation: send anyway because the evidence is recent.

    The message says this address does not work. It works. Sending it is the
    one failure a recipient can disprove in a single click -- which is what 156
    messages did before the detectors were corrected.
    """
    check = judge(issue_type=CLAIM, observed=seen(status=status))

    assert check.verdict is Verdict.CONTRADICTED
    assert check.blocks_send


def test_the_withdrawal_says_what_changed() -> None:
    """It lands in `contradiction_reason`, which is the audit trail for why a
    message was not sent. "contradicted" is not a reason."""
    detail = judge(issue_type=CLAIM, observed=seen(status=200), claimed_status=404).detail

    assert "200" in detail and "404" in detail and URL in detail


# ------------------------------------------------ the claim stands
@pytest.mark.parametrize("status", [404, 410, 500, 503])
def test_a_broken_address_confirms_the_claim(status: int) -> None:
    check = judge(issue_type=CLAIM, observed=seen(status=status))

    assert check.verdict is Verdict.CONFIRMED
    assert not check.blocks_send


def test_the_boundary_is_where_http_puts_it() -> None:
    """4xx is an error, 3xx that lands somewhere real is a working link."""
    assert judge(issue_type=CLAIM, observed=seen(status=OK_BELOW - 1)).verdict is (
        Verdict.CONTRADICTED
    )
    assert judge(issue_type=CLAIM, observed=seen(status=OK_BELOW)).verdict is (
        Verdict.CONFIRMED
    )


# ------------------------------------------------ inconclusive is not a fix
@pytest.mark.parametrize(
    ("label", "observed"),
    [
        ("no answer at all", RecheckResult(url=URL, status=None)),
        ("probe errored", RecheckResult(url=URL, error="TimeoutError: timed out")),
        (
            "url guard refused",
            RecheckResult(url=URL, allowed=False, blocked_reason="private_ip"),
        ),
    ],
)
def test_an_inconclusive_probe_never_withdraws_a_message(label, observed) -> None:
    """Planted violation: treat "we could not check" as "it was fixed".

    A site behind a hostile bot filter answers nothing, twice, from a real
    browser -- which looks identical to a fix if you only check for the absence
    of an error. Reading it that way would discard exactly the leads whose
    sites are hardest to crawl, and the loss would be invisible.
    """
    check = judge(issue_type=CLAIM, observed=observed)

    assert check.verdict is Verdict.INCONCLUSIVE, label
    assert not check.blocks_send, label


# ------------------------------------------------ scope
@pytest.mark.parametrize("issue_type", sorted(URL_STATUS_CLAIMS))
def test_the_url_claims_are_recheckable(issue_type: str) -> None:
    assert is_recheckable(issue_type, URL)


@pytest.mark.parametrize(
    "issue_type",
    [
        "images_missing_alt_text",
        "javascript_console_errors",
        "slow_largest_contentful_paint",
        "missing_security_headers",
        "high_friction_contact_form",
    ],
)
def test_claims_a_status_cannot_settle_are_left_alone(issue_type: str) -> None:
    """Planted violation: judge every finding by one HTTP request.

    A 200 from the home page says nothing about whether images have alt text or
    the console throws. Treating it as confirmation would withdraw true claims,
    which is the same error as sending false ones, pointed the other way.
    """
    assert not is_recheckable(issue_type, URL)
    assert judge(issue_type=issue_type, observed=seen(status=200)).verdict is (
        Verdict.INCONCLUSIVE
    )


def test_a_finding_with_no_page_cannot_be_rechecked() -> None:
    """There is no address to ask."""
    assert not is_recheckable(CLAIM, None)
    assert not is_recheckable(CLAIM, "")
