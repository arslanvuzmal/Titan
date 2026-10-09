"""Who to ring, in what order, and the one sentence to open with.

Email is free, so its list can be long and its ordering sloppy. A call costs
minutes that cannot be spent twice, which changes what the system owes the
operator: not a list of everyone reachable, but the shortest list that is worth
his morning, ordered so the best call is the first one.

**Ordering is the whole product.** Twenty calls a day against 2,109 practices
means the list is never exhausted -- so nothing is lost by being picky, and
everything is lost by calling in the order the database happens to return.

**What the score is built from, and why each part earns its place.**

*The defect tier* dominates, because it decides whether there is anything worth
saying. "Your Book Online page returns an error" is a different call from "some
images lack alt text", and no amount of practice size redeems the second.

*Review count* is the only revenue proxy available, and it is a good one for
dentistry: a practice with 800 reviews sees vastly more patients than one with
40, so the same broken booking page costs it more, and it has the budget to
care. Scaled logarithmically -- the difference between 40 and 400 reviews is
real; between 2,000 and 2,400 it is noise.

*Rating* is a proxy for whether they care. A practice at 4.8 has been working
at its reputation; one at 3.4 has other problems and a website is not the first
of them.

*Evidence age* subtracts, because the call opens by asserting a fact about
their site and a three-week-old reading may have been fixed. Being wrong in the
first sentence of a cold call is unrecoverable in a way that being wrong in an
email is not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Defect tiers, worth calling about in this order. These mirror the message
#: composer's own tiers so the phone and the inbox never disagree about what
#: matters -- a practice told one thing by email and another by phone hears an
#: organisation that does not know its own mind.
CONVERSION_DEFECTS = frozenset(
    {
        "broken_primary_cta",
        "high_friction_contact_form",
        "no_visible_phone_number",
    }
)
AUTOMATION_DEFECTS = frozenset(
    {
        "no_booking_or_enquiry_path",
        "no_self_service_booking",
        "no_conversational_capability",
        "no_follow_up_automation",
        "no_review_automation",
    }
)

#: Points by tier. The gap between a dead booking path and a cosmetic finding
#: is deliberately larger than anything practice size can make up.
TIER_POINTS = {"booking_dead": 60, "conversion": 45, "automation": 30, "cosmetic": 0}

#: Above this, a finding is too old to assert down a telephone. Shorter than
#: the 30 days the email gate allows: an email can be hedged and re-read, a
#: spoken claim cannot be withdrawn once the practice has checked and found it
#: fixed.
MAX_EVIDENCE_AGE_DAYS = 14


def tier_of(issue_type: str, page_url: str | None) -> str:
    """Which band of call this defect justifies."""
    url = (page_url or "").lower()
    if issue_type == "broken_internal_link" and any(
        seg in url for seg in ("/book", "/booking", "/appointment")
    ):
        # The strongest call in the set: their Book Online page is a 404.
        return "booking_dead"
    if issue_type in CONVERSION_DEFECTS:
        return "conversion"
    if issue_type in AUTOMATION_DEFECTS:
        return "automation"
    return "cosmetic"


def size_points(review_count: int | None) -> float:
    """Revenue proxy, compressed.

    Logarithmic because the interesting distinction is 40 reviews versus 400,
    not 2,000 versus 2,400. Capped so a single enormous practice cannot
    outrank a genuinely broken booking page at a normal one.
    """
    if not review_count or review_count < 1:
        return 0.0
    return min(20.0, math.log10(review_count) * 7.0)


def rating_points(rating: float | None) -> float:
    """Do they already care about how they look?

    Below 4.0 this is zero rather than negative: a struggling practice is not a
    worse prospect on principle, it is simply one whose website is not its
    most pressing problem, and the call is unlikely to land.
    """
    if rating is None or rating < 4.0:
        return 0.0
    return min(10.0, (rating - 4.0) * 10.0)


def freshness_penalty(evidence_age_days: int | None) -> float:
    """Age of the fact the call opens by asserting."""
    if evidence_age_days is None:
        return 25.0
    if evidence_age_days <= 3:
        return 0.0
    if evidence_age_days > MAX_EVIDENCE_AGE_DAYS:
        return 40.0
    return float(evidence_age_days - 3) * 1.5


@dataclass(frozen=True, slots=True)
class CallTarget:
    lead_id: str
    practice: str
    phone: str
    website: str | None
    issue_type: str
    page_url: str | None
    observed: str
    review_count: int | None
    rating: float | None
    evidence_age_days: int | None

    @property
    def tier(self) -> str:
        return tier_of(self.issue_type, self.page_url)

    @property
    def score(self) -> float:
        return round(
            TIER_POINTS[self.tier]
            + size_points(self.review_count)
            + rating_points(self.rating)
            - freshness_penalty(self.evidence_age_days),
            1,
        )

    @property
    def worth_calling(self) -> bool:
        """A cosmetic finding is never worth a phone call.

        It is worth an email -- barely -- because an email costs nothing. A
        call spends a minute of the only resource that does not scale, and
        opening one with "some of your images lack alt text" spends it on a
        conversation that cannot go anywhere.
        """
        return self.tier != "cosmetic" and (self.evidence_age_days or 99) <= (
            MAX_EVIDENCE_AGE_DAYS
        )

    def opener(self) -> str:
        """The first sentence, in the operator's mouth.

        Short, factual, and checkable while the phone is still in their hand.
        It names the page rather than describing it, because a receptionist who
        can look at it becomes an ally instead of a filter.
        """
        if self.tier == "booking_dead":
            return (
                f"Your Book Online page is returning an error -- "
                f"{_short(self.page_url)} -- so anyone clicking it from Google "
                f"gets an error page instead of an appointment."
            )
        if self.issue_type == "high_friction_contact_form":
            return (
                f"Your enquiry form asks for {_count(self.observed)} separate "
                f"fields before anyone can send it, and most people give up "
                f"partway."
            )
        if self.issue_type in {"no_booking_or_enquiry_path", "no_self_service_booking"}:
            return (
                "There's no way to book an appointment from your website -- "
                "every enquiry has to become a phone call during opening hours."
            )
        if self.issue_type == "no_visible_phone_number":
            return "There's no phone number visible on your website's main pages."
        return f"I noticed something on your website: {self.observed[:90]}"

    def ask(self) -> str:
        """Stage one closes here, and only here.

        Not a pitch and not a meeting. A receptionist can hand over a name and
        an email without deciding anything, which is why they will -- and a
        named human who agreed to hear from us is worth more than any address
        a crawler has ever produced.
        """
        return (
            "I can send a 30-second screen recording showing what a patient "
            "sees -- who's the best person to send that to?"
        )


def _short(url: str | None) -> str:
    if not url:
        return "your booking page"
    return url.replace("https://", "").replace("http://", "").rstrip("/")


def _count(observed: str) -> str:
    for token in observed.split():
        if token.isdigit():
            return token
    return "several"


__all__ = [
    "AUTOMATION_DEFECTS",
    "CONVERSION_DEFECTS",
    "MAX_EVIDENCE_AGE_DAYS",
    "TIER_POINTS",
    "CallTarget",
    "freshness_penalty",
    "rating_points",
    "size_points",
    "tier_of",
]
