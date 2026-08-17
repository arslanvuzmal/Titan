"""Whether a browser complaint is the prospect's problem or ours.

The crawler reports everything the page did. Two detectors passed that straight
through at ``confidence=0.95`` -- above the pitchable bar -- so it reached the
composer and would have reached a real inbox. On the live workspace, of 527
``failed_network_requests``:

    121  third-party analytics and tracking beacons
     70  net::ERR_ABORTED

and among the console errors, complaints about ``X-Frame-Options`` on
``doctify.com`` -- a directory site embedded in an iframe, not the prospect's
site at all.

Telling a dentist their website is broken when the actual observation is "our
headless browser blocked Google Analytics" is the worst thing a cold email can
do. It is checkably false, it is the first thing their developer will check, and
it makes every other claim in the message worthless.

**First-party or it does not count.** A finding must be about something the
recipient can see and fix on their own site. Anything else is either our
environment or somebody else's.

**Registrable domain, not hostname.** ``cdn.example.com`` and ``www.example.com``
are the same business; ``example.wixstatic.com`` is not, but is a first-party
asset host in practice, which is why the platform CDNs are named explicitly
rather than inferred.

Nothing here decides whether a finding matters. It decides only whether it is
*theirs*, which is the question that has to be answered first.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import tldextract

#: Hosts whose failures say nothing about the prospect's site.
#:
#: Two separate reasons, deliberately in one list because the consequence is the
#: same. Analytics and tag managers are blocked by our own crawler and by a
#: large share of real visitors' ad-blockers, so a failure is expected rather
#: than diagnostic. Consent and chat widgets fail on their own schedule and are
#: not something the site owner wrote.
THIRD_PARTY_NOISE: frozenset[str] = frozenset(
    {
        "google-analytics.com",
        "googletagmanager.com",
        "analytics.google.com",
        "doubleclick.net",
        "googlesyndication.com",
        "googleadservices.com",
        "facebook.com",
        "facebook.net",
        "connect.facebook.net",
        "tiktok.com",
        "linkedin.com",
        "licdn.com",
        "twitter.com",
        "x.com",
        "hotjar.com",
        "hotjar.io",
        "clarity.ms",
        "cloudflareinsights.com",
        "segment.com",
        "segment.io",
        "mixpanel.com",
        "intercom.io",
        "intercomcdn.com",
        "hubspot.com",
        "hs-scripts.com",
        "hs-analytics.net",
        "zdassets.com",
        "zendesk.com",
        "crisp.chat",
        "tawk.to",
        "livechatinc.com",
        "trustpilot.com",
        "doctify.com",
        "recaptcha.net",
        "gstatic.com",
        "googleapis.com",
        "bing.com",
        "clarity.microsoft.com",
        "pinterest.com",
        "snapchat.com",
        "cookiebot.com",
        "cookieyes.com",
        "onetrust.com",
        "usercentrics.eu",
        "termly.io",
        "iubenda.com",
    }
)

#: Asset hosts belonging to the site builder a prospect uses. A failure here is
#: genuinely their page misbehaving -- a missing image on a Wix site is a
#: missing image -- so these are treated as first-party despite the domain.
FIRST_PARTY_PLATFORMS: frozenset[str] = frozenset(
    {
        "wixstatic.com",
        "wixsite.com",
        "parastorage.com",
        "squarespace.com",
        "squarespace-cdn.com",
        "shopify.com",
        "shopifycdn.com",
        "wordpress.com",
        "wp.com",
        "webflow.com",
        "website-files.com",
        "godaddysites.com",
        "weebly.com",
        "jimdo.com",
        "duda.co",
        "multiscreensite.com",
    }
)

#: Browser error codes that describe the navigation rather than the resource.
#:
#: ``ERR_ABORTED`` is overwhelmingly the page moving on before a request
#: finished -- a font or image cancelled because the crawler navigated away. It
#: is our timing, not their bug. The others are transport-level and real, so
#: they are deliberately not listed.
NAVIGATION_NOISE: tuple[str, ...] = (
    "ERR_ABORTED",
    "ERR_BLOCKED_BY_CLIENT",
    "ERR_BLOCKED_BY_RESPONSE",
    "ERR_CACHE_MISS",
)

#: Console messages that are somebody else's page, or a browser policy note
#: rather than a defect. Matched case-insensitively against the message.
CONSOLE_NOISE_MARKERS: tuple[str, ...] = (
    "x-frame-options",
    "content security policy",
    "refused to load",
    "third-party cookie",
    "cookie will be soon rejected",
    "samesite",
    "deprecated",
    "devtools",
    "preload",
    "was preloaded using link preload but not used",
    "favicon",
)


def registrable_domain(url_or_host: str) -> str:
    """The part of a host that identifies the business.

    ``www.example.co.uk`` and ``cdn.example.co.uk`` both give ``example.co.uk``.
    Public-suffix aware, so ``.co.uk`` is not mistaken for a registrable domain
    -- naive last-two-labels would make every UK business look like a different
    company from its own CDN.
    """
    candidate = (url_or_host or "").strip()
    if not candidate:
        return ""
    if "://" in candidate:
        candidate = urlsplit(candidate).netloc
    candidate = candidate.split("@")[-1].split(":")[0]
    extracted = tldextract.extract(candidate)
    if not extracted.domain or not extracted.suffix:
        return candidate.lower()
    return f"{extracted.domain}.{extracted.suffix}".lower()


def is_same_site(url: str, site_url: str) -> bool:
    """Whether a URL belongs to the site being audited."""
    theirs = registrable_domain(site_url)
    if not theirs:
        return False
    host = registrable_domain(url)
    return host == theirs or host in FIRST_PARTY_PLATFORMS


def is_third_party_noise(url: str) -> bool:
    return registrable_domain(url) in THIRD_PARTY_NOISE


def is_attributable_request(entry: str, *, site_url: str) -> bool:
    """Whether one failed-request line is a defect on the prospect's site.

    ``entry`` is the crawler's own line, roughly ``GET <url> :: <error>``. It is
    matched loosely because the shape is the browser's, not ours, and a format
    change should lose precision rather than start rejecting everything.
    """
    text = (entry or "").strip()
    if not text:
        return False
    if any(marker.casefold() in text.casefold() for marker in NAVIGATION_NOISE):
        return False

    url = _first_url(text)
    if url is None:
        # No URL to attribute. Kept rather than dropped: the alternative is
        # discarding a real failure because the line was formatted unusually,
        # and this function's job is to remove what is provably not theirs.
        return True
    if is_third_party_noise(url):
        return False
    return is_same_site(url, site_url)


def is_attributable_console_error(message: str, *, site_url: str) -> bool:
    """Whether one console line describes the prospect's own code failing.

    Browser policy notes -- CSP refusals, cookie deprecation warnings, preload
    hints -- are printed as errors and are not defects anybody asked for. A
    message naming a third-party host is that host's problem.
    """
    text = (message or "").strip()
    if not text:
        return False
    lowered = text.casefold()
    if any(marker in lowered for marker in CONSOLE_NOISE_MARKERS):
        return False

    url = _first_url(text)
    if url is None:
        return True
    if is_third_party_noise(url):
        return False
    return is_same_site(url, site_url)


def attributable_requests(entries: list[str], *, site_url: str) -> list[str]:
    return [e for e in entries if is_attributable_request(e, site_url=site_url)]


def attributable_console_errors(messages: list[str], *, site_url: str) -> list[str]:
    return [m for m in messages if is_attributable_console_error(m, site_url=site_url)]


def _first_url(text: str) -> str | None:
    """The first http(s) URL in a line, if there is one."""
    for token in text.replace('"', " ").replace("'", " ").split():
        if token.startswith(("http://", "https://")):
            return token.rstrip(".,;:)")
    return None


__all__ = [
    "CONSOLE_NOISE_MARKERS",
    "FIRST_PARTY_PLATFORMS",
    "NAVIGATION_NOISE",
    "THIRD_PARTY_NOISE",
    "attributable_console_errors",
    "attributable_requests",
    "is_attributable_console_error",
    "is_attributable_request",
    "is_same_site",
    "is_third_party_noise",
    "registrable_domain",
]
