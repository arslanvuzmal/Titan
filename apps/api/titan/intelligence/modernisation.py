"""How far behind a business actually is, and therefore whether it needs us.

The operator's brief, in his words: *many leads have their own AI system... you
should verify that they don't have any AI setup and they are moving in a
traditional way, so here comes we.*

He is right that this is the thing that decides a lead's worth, and he is right
that it was missing. The crawler has been collecting ``technologies`` and
``has_chat_widget`` since the browser worker was written, and **nothing in this
repository read either of them.** A dental practice already running an AI
receptionist and one taking bookings by telephone scored identically, because
the only thing being scored was whether their website was broken.

A broken booking page is a *defect*. Having no booking page at all is a
*position*, and the second is what an AI services pitch is actually about.

**Presence is conclusive; absence is not.** Finding Intercom's launcher proves
a chat widget. Not finding one proves that we did not see one -- it may be
lazy-loaded after an interaction, injected below a cookie wall, or on a page we
did not crawl. So every capability has three states and the third is real:

    PRESENT       observed, and it settles the question
    ABSENT        looked for, on a page we could actually read
    NOT_MEASURED  the page was obstructed, or we never had the signal

``NOT_MEASURED`` is excluded from the score rather than counted as absence.
Counting "we could not see" as "they do not have it" would turn every
cookie-walled site into a perfect prospect, and cookie walls are commonest
exactly where budgets are largest.

**A floor before the score means anything.** Below
:data:`MIN_MEASURED_CAPABILITIES`, the profile reports ``None`` rather than a
number. Two capabilities out of six is not a measurement of how modern a
business is; it is a measurement of how much of the site we managed to read.

**This ranks prospects. It never justifies a claim.** Nothing here may appear in
a message. "You have no online booking" is an assertion about a business's
operations drawn from the absence of a script tag, and the absence of a script
tag is not evidence of anything a person would recognise. Findings carry claims;
this carries priority.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Signal(StrEnum):
    """What was learned about one capability."""

    PRESENT = "present"
    ABSENT = "absent"
    NOT_MEASURED = "not_measured"


class Capability(StrEnum):
    """The modern operating capabilities Titan sells, in the order it sells them."""

    #: Anything that answers a visitor without a person: AI agents, chatbots,
    #: live chat. The headline offering, and the one the operator names.
    CONVERSATIONAL = "conversational"
    #: Self-service appointment booking. The difference between a prospect
    #: booking at 22:00 and a prospect ringing back tomorrow, or not.
    SELF_SERVICE_BOOKING = "self_service_booking"
    #: Anything that follows up without somebody remembering to.
    MARKETING_AUTOMATION = "marketing_automation"
    #: Review collection and response handled by software.
    REPUTATION_AUTOMATION = "reputation_automation"
    #: Any measurement at all. A weak signal on its own; a business with no
    #: analytics is usually not running anything else either.
    ANALYTICS = "analytics"
    #: A site built this decade. Not a capability the operator sells, but a
    #: reliable indicator of when anybody last invested in the business online.
    MODERN_SITE = "modern_site"


#: Vendor tokens, by the capability their presence proves.
#:
#: Matched as substrings against lowercased technology tokens and script URLs,
#: because a detector reports "intercom" and a script URL says
#: "widget.intercom.io". Deliberately vendor names rather than generic words:
#: "chat" appears in a thousand CSS class names and "book" is half the verbs on
#: a dental website.
VENDORS: dict[Capability, frozenset[str]] = {
    Capability.CONVERSATIONAL: frozenset(
        {
            "intercom",
            "drift",
            "tidio",
            "crisp",
            "tawk",
            "livechat",
            "zendesk",
            "freshchat",
            "olark",
            "smartsupp",
            "chatra",
            "manychat",
            "chatbase",
            "voiceflow",
            "botpress",
            "ada-support",
            "landbot",
            "podium",
            "birdeye",
            "hubspot-conversations",
        }
    ),
    Capability.SELF_SERVICE_BOOKING: frozenset(
        {
            "calendly",
            "acuityscheduling",
            "acuity",
            "simplybook",
            "setmore",
            "squareup/appointments",
            "fresha",
            "booksy",
            "treatwell",
            "phorest",
            "timely",
            "dentally",
            "zocdoc",
            "cliniko",
            "jane-app",
            "janeapp",
            "mindbody",
            "vagaro",
            "youcanbook",
            "10to8",
            "appointedd",
            "opendental",
        }
    ),
    Capability.MARKETING_AUTOMATION: frozenset(
        {
            "hubspot",
            "mailchimp",
            "klaviyo",
            "activecampaign",
            "marketo",
            "pardot",
            "sendinblue",
            "brevo",
            "drip",
            "omnisend",
            "convertkit",
        }
    ),
    Capability.REPUTATION_AUTOMATION: frozenset(
        {
            "trustpilot",
            "birdeye",
            "podium",
            "reviews-io",
            "reviewsio",
            "yotpo",
            "feefo",
            "nicejob",
            "grade-us",
        }
    ),
    Capability.ANALYTICS: frozenset(
        {
            "google-analytics",
            "gtag",
            "googletagmanager",
            "meta-pixel",
            "facebook-pixel",
            "fbq",
            "hotjar",
            "clarity",
            "plausible",
            "fathom",
            "matomo",
        }
    ),
    Capability.MODERN_SITE: frozenset(
        {
            "nextjs",
            "react",
            "vue",
            "nuxt",
            "svelte",
            "webflow",
            "shopify",
            "squarespace",
        }
    ),
}

#: How many capabilities must have been measured before a gap score is honest.
#:
#: Four of six. Below that the number describes how much of the site we managed
#: to read rather than how the business runs, and a confident-looking score
#: computed from two observations is worse than no score.
MIN_MEASURED_CAPABILITIES = 4

#: Weights for the gap score. They sum to 1.0 across all six.
#:
#: Conversational and booking carry most of it because they are what the
#: operator sells and what a visitor actually hits. Analytics and site age are
#: indicators rather than offerings, and are weighted accordingly.
GAP_WEIGHTS: dict[Capability, float] = {
    Capability.CONVERSATIONAL: 0.30,
    Capability.SELF_SERVICE_BOOKING: 0.30,
    Capability.MARKETING_AUTOMATION: 0.15,
    Capability.REPUTATION_AUTOMATION: 0.10,
    Capability.ANALYTICS: 0.08,
    Capability.MODERN_SITE: 0.07,
}


@dataclass(frozen=True, slots=True)
class PageSignals:
    """What one crawled page can say about how a business operates.

    A narrow view of the evidence contract, taken deliberately: this module
    should be testable without constructing a full PageEvidence, and adding a
    field here is a decision rather than a side effect of the crawler changing.
    """

    technologies: tuple[str, ...] = ()
    script_urls: tuple[str, ...] = ()
    has_chat_widget: bool = False
    #: Hrefs the crawler judged to be about booking. Treated as weak evidence
    #: on purpose -- see :func:`booking_link_is_a_system`.
    booking_links: tuple[str, ...] = ()
    #: The host of the page these links were found on, so an off-site booking
    #: link can be told from an on-site page that merely says "Book".
    site_host: str | None = None
    #: True when a cookie wall or overlay stood between the crawler and the
    #: page. Everything absent becomes NOT_MEASURED, because it was.
    obstructed: bool = False
    #: False when the page could not be read at all -- a failed fetch, an empty
    #: body. Distinct from obstructed: nothing was observed, not even the wall.
    readable: bool = True


@dataclass(frozen=True, slots=True)
class ModernisationProfile:
    """What a business already runs, and what it does not."""

    signals: dict[Capability, Signal]

    @property
    def measured(self) -> tuple[Capability, ...]:
        return tuple(c for c, s in self.signals.items() if s is not Signal.NOT_MEASURED)

    @property
    def present(self) -> tuple[Capability, ...]:
        return tuple(c for c, s in self.signals.items() if s is Signal.PRESENT)

    @property
    def absent(self) -> tuple[Capability, ...]:
        return tuple(c for c, s in self.signals.items() if s is Signal.ABSENT)

    @property
    def gap(self) -> float | None:
        """How traditional this business is: 1.0 fully, 0.0 fully modernised.

        None when too little was measured. Not zero, and not 0.5 -- both would
        be a number where there is no measurement, and the caller must be able
        to tell "we did not learn enough" from "they are already modern".

        Computed over the measured capabilities only, renormalised so that a
        business whose analytics could not be read is not penalised for it.
        """
        measured = self.measured
        if len(measured) < MIN_MEASURED_CAPABILITIES:
            return None
        total = sum(GAP_WEIGHTS[c] for c in measured)
        if total <= 0:
            return None
        missing = sum(
            GAP_WEIGHTS[c] for c in measured if self.signals[c] is Signal.ABSENT
        )
        return round(missing / total, 3)

    def describe(self) -> str:
        """One line an operator can read, without a score in it."""
        if not self.measured:
            return "nothing measurable was read from this site"
        has = ", ".join(c.value for c in self.present) or "nothing detected"
        lacks = ", ".join(c.value for c in self.absent) or "nothing"
        return f"runs: {has} | lacks: {lacks}"


#: Hosts that are never a booking system, however far off-site they are.
#:
#: The "it leaves the site" rule is a good heuristic and these are where it
#: breaks: a practice's Facebook page is off-host and is not a booking system.
#: Kept here as well as in the crawler's own filter, because a rule that only
#: works when its input is already clean is a rule that fails the first time
#: somebody loosens the input.
NEVER_A_BOOKING_HOST: frozenset[str] = frozenset(
    {
        "facebook.com",
        "instagram.com",
        "linkedin.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "tiktok.com",
        "yelp.com",
        "pinterest.com",
        "wa.me",
        "whatsapp.com",
        "maps.google.com",
        "goo.gl",
        "g.page",
        "trustpilot.com",
    }
)


def _host_of(url: str) -> str:
    """The host part of a URL, lowercased, without credentials or port."""
    remainder = url.split("://", 1)[-1]
    host = remainder.split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    return host[4:] if host.startswith("www.") else host


def booking_link_is_a_system(link: str, *, site_host: str | None) -> bool:
    """Whether this href is evidence of a booking system rather than a page.

    A link that says "Book" is not a booking system. Half of them go to a
    contact form and a good number went nowhere at all -- the crawler's booking
    pattern matched the word "book" inside "facebook.com", so **2,056 of 2,494
    crawled businesses were recorded as taking online bookings**. The pattern is
    fixed; this is the second line, because the same over-counting would be
    re-introduced by any future loosening of it.

    Two things count, and both are observations rather than guesses:

    * the URL names a booking vendor, or
    * it leaves the site for a host that is not social or a directory. A
      practice that sends visitors elsewhere to book is using something; a
      ``/book-online`` page on its own domain is as likely to be a form and a
      phone number, and a Facebook page is neither.

    Neither is perfect. An on-site booking system built into the CMS reads as
    absent here, which is the direction to be wrong in: it under-counts what a
    business already has, so the lead is ranked slightly *lower* than it should
    be rather than being written to about something it already runs.
    """
    lowered = link.strip().lower()
    if not lowered:
        return False
    if any(vendor in lowered for vendor in VENDORS[Capability.SELF_SERVICE_BOOKING]):
        return True
    host = _host_of(lowered)
    if not host or any(
        host == social or host.endswith("." + social) for social in NEVER_A_BOOKING_HOST
    ):
        return False
    if site_host is None:
        return False
    return host != site_host.strip().lower().removeprefix("www.")


def _matches(tokens: Iterable[str], vendors: frozenset[str]) -> bool:
    for token in tokens:
        lowered = token.strip().lower()
        if not lowered:
            continue
        if any(vendor in lowered for vendor in vendors):
            return True
    return False


def profile(signals: PageSignals) -> ModernisationProfile:
    """Read one page's signals into a capability profile.

    Order matters in one place only: a positive detection wins over everything,
    including obstruction. A cookie wall does not un-run the Intercom script
    that was found underneath it.
    """
    tokens = tuple(signals.technologies) + tuple(signals.script_urls)
    out: dict[Capability, Signal] = {}

    for capability, vendors in VENDORS.items():
        present = _matches(tokens, vendors)

        if capability is Capability.CONVERSATIONAL and signals.has_chat_widget:
            # The DOM check catches widgets whose vendor we do not name -- and
            # a widget we cannot attribute is still a widget the business runs.
            present = True
        if capability is Capability.SELF_SERVICE_BOOKING and any(
            booking_link_is_a_system(link, site_host=signals.site_host)
            for link in signals.booking_links
        ):
            # An off-site or vendor-named booking link. A bare "/book" page on
            # the practice's own domain deliberately does not count.
            present = True

        if present:
            out[capability] = Signal.PRESENT
        elif not signals.readable or signals.obstructed:
            out[capability] = Signal.NOT_MEASURED
        else:
            out[capability] = Signal.ABSENT

    return ModernisationProfile(signals=out)


def merge(profiles: Iterable[ModernisationProfile]) -> ModernisationProfile:
    """One profile for a business crawled across several pages.

    A capability found on any page is PRESENT for the business -- booking lives
    on the booking page and chat is often only on the home page, so requiring
    every page to show it would find nothing.

    ABSENT requires at least one page where it was actually looked for. Merging
    six NOT_MEASURED pages yields NOT_MEASURED, which is the honest answer for a
    site that was obstructed throughout.
    """
    # Materialised, and that is the whole of this line's job. The loop below
    # walks `profiles` once per capability, so a generator -- which is exactly
    # what activities/pipeline.py passes -- is exhausted by the first capability
    # and every later one reads an empty list as NOT_MEASURED.
    #
    # It fails in the quietest possible way: CONVERSATIONAL, first in VENDORS,
    # was measured correctly, so the profile looked populated. The other five
    # came back NOT_MEASURED, `gap` needs MIN_MEASURED_CAPABILITIES of them and
    # returned None, and `findings_from_gap` therefore produced nothing. Zero
    # absence findings exist in the estate's entire history -- 22,000 pages
    # crawled with the signals sitting in them, read once each.
    #
    # Measured on one real crawl: as a generator, 1 of 6 capabilities and
    # gap=None. As a list, 6 of 6 and gap=0.92.
    profiles = list(profiles)
    merged: dict[Capability, Signal] = {}
    for capability in VENDORS:
        seen = [p.signals.get(capability, Signal.NOT_MEASURED) for p in profiles]
        if Signal.PRESENT in seen:
            merged[capability] = Signal.PRESENT
        elif Signal.ABSENT in seen:
            merged[capability] = Signal.ABSENT
        else:
            merged[capability] = Signal.NOT_MEASURED
    return ModernisationProfile(signals=merged)


__all__ = [
    "GAP_WEIGHTS",
    "MIN_MEASURED_CAPABILITIES",
    "NEVER_A_BOOKING_HOST",
    "VENDORS",
    "Capability",
    "ModernisationProfile",
    "PageSignals",
    "Signal",
    "booking_link_is_a_system",
    "merge",
    "profile",
]
