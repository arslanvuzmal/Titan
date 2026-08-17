"""Whether a browser complaint is the prospect's problem or ours.

Two detectors passed everything the crawler saw straight through at
``confidence=0.95``, which is above the pitchable bar, so it reached the
composer. On the live workspace, of 527 ``failed_network_requests``, 121 were
third-party analytics beacons and 70 were ``net::ERR_ABORTED``.

Telling a dentist their website is broken when the observation is "our headless
browser blocked Google Analytics" is checkably false, it is the first thing
their developer checks, and it makes every other claim in the message worthless.
"""

from __future__ import annotations

from titan.intelligence.attribution import (
    attributable_console_errors,
    attributable_requests,
    is_attributable_console_error,
    is_attributable_request,
    is_same_site,
    registrable_domain,
)

SITE = "https://www.smilecare-dental.co.uk/services/"


# ------------------------------------------------------------ registrable domain


def test_a_public_suffix_is_not_a_domain() -> None:
    """Naive last-two-labels makes every UK business look like a different
    company from its own CDN, and then every first-party asset reads as
    third-party."""
    assert registrable_domain("https://www.smilecare-dental.co.uk/x") == (
        "smilecare-dental.co.uk"
    )
    assert registrable_domain("cdn.smilecare-dental.co.uk") == "smilecare-dental.co.uk"


def test_subdomains_are_the_same_business() -> None:
    assert is_same_site("https://cdn.smilecare-dental.co.uk/a.png", SITE)
    assert is_same_site("https://smilecare-dental.co.uk/b.css", SITE)


def test_a_different_company_is_not() -> None:
    assert not is_same_site("https://www.doctify.com/", SITE)


# ------------------------------------------------------------- failed requests


def test_a_blocked_analytics_beacon_is_not_their_bug() -> None:
    """The single largest source of false findings.

    Our crawler blocks it, and so does a large share of real visitors'
    ad-blockers. A failure here is expected, not diagnostic.
    """
    entry = (
        "POST https://www.google-analytics.com/g/collect?v=2&tid=G-X :: net::ERR_FAILED"
    )

    assert not is_attributable_request(entry, site_url=SITE)


def test_err_aborted_is_our_timing_not_their_resource() -> None:
    """Overwhelmingly the crawler navigating away before a request finished."""
    entry = "GET https://www.smilecare-dental.co.uk/fonts/x.woff2 :: net::ERR_ABORTED"

    assert not is_attributable_request(entry, site_url=SITE)


def test_a_genuinely_broken_first_party_asset_still_counts() -> None:
    """The whole point is to keep these. A filter that dropped real defects
    would trade a false claim for no claim at all."""
    entry = "GET https://www.smilecare-dental.co.uk/img/hero.jpg :: net::ERR_HTTP_RESPONSE_CODE_FAILURE"

    assert is_attributable_request(entry, site_url=SITE)


def test_a_site_builders_cdn_is_treated_as_first_party() -> None:
    """A missing image on a Wix site is a missing image, whatever host serves
    it -- and the recipient can fix it."""
    entry = "GET https://static.wixstatic.com/media/480222_69b.jpg :: net::ERR_FAILED"

    assert is_attributable_request(entry, site_url="https://someclinic.com/")


def test_a_line_with_no_url_is_kept() -> None:
    """The shape is the browser's, not ours. A format change should cost
    precision, not start discarding real failures."""
    assert is_attributable_request("something failed", site_url=SITE)


def test_the_live_sample_is_mostly_removed() -> None:
    """Nine real lines from the workspace, filtered as a batch."""
    entries = [
        "POST https://www.google-analytics.com/g/collect?v=2 :: net::ERR_FAILED",
        "POST https://www.google-analytics.com/g/collect?v=3 :: net::ERR_FAILED",
        "GET https://www.smilecare-dental.co.uk/fonts/a.woff2 :: net::ERR_ABORTED",
        "GET https://www.smilecare-dental.co.uk/img/real.png :: net::ERR_CONNECTION_REFUSED",
    ]

    assert attributable_requests(entries, site_url=SITE) == [
        "GET https://www.smilecare-dental.co.uk/img/real.png :: net::ERR_CONNECTION_REFUSED"
    ]


# ------------------------------------------------------------- console errors


def test_a_third_party_iframe_policy_note_is_not_their_error() -> None:
    """Observed live: a complaint about a directory site embedded in a page."""
    message = (
        "Invalid 'X-Frame-Options' header encountered when loading "
        "'https://www.doctify.com/': 'ANY' is not a recognized directive."
    )

    assert not is_attributable_console_error(message, site_url=SITE)


def test_a_csp_refusal_is_a_browser_policy_note() -> None:
    message = (
        "Refused to load the script "
        "'https://static.cloudflareinsights.com/beacon.min.js' because it "
        "violates the following Content Security Policy directive"
    )

    assert not is_attributable_console_error(message, site_url=SITE)


def test_mixed_content_on_their_own_page_is_a_real_defect() -> None:
    """A stylesheet loaded over http on an https page is theirs, it is visible
    to visitors as a security warning, and they can fix it."""
    message = (
        "Mixed Content: The page at 'https://www.smilecare-dental.co.uk/' was "
        "loaded over HTTPS, but requested an insecure stylesheet "
        "'http://www.smilecare-dental.co.uk/a.css'"
    )

    assert is_attributable_console_error(message, site_url=SITE)


def test_a_bare_application_error_is_kept() -> None:
    """No host to attribute and no policy marker: this is their script."""
    assert is_attributable_console_error(
        "Uncaught TypeError: Cannot read properties of null", site_url=SITE
    )


def test_console_filtering_works_as_a_batch() -> None:
    messages = [
        "Uncaught TypeError: Cannot read properties of null",
        "Refused to load the script 'https://static.cloudflareinsights.com/b.js'",
        "Third-party cookie will be blocked in future Chrome versions",
    ]

    assert attributable_console_errors(messages, site_url=SITE) == [
        "Uncaught TypeError: Cannot read properties of null"
    ]


def test_nothing_left_means_no_finding() -> None:
    """The detectors return None on an empty list, so a page whose only errors
    were ours produces no claim at all -- rather than a claim about zero."""
    assert attributable_console_errors([], site_url=SITE) == []
    assert attributable_requests([""], site_url=SITE) == []
