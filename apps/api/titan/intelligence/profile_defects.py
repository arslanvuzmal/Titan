"""What a business's Google listing shows, and what it does not.

Every other detector in this system reads the recipient's own website. That is
the right instrument for a business that has one, and it is no instrument at
all for a business that does not -- which is why
:mod:`titan.intelligence.discovery` refused those outright, and why the refusal
was correct as long as the website was the only place anyone looked.

This reads the other public record: the Google Business Profile. A profile is
maintained by the business, shown to everybody searching for them, and its gaps
are visible to the owner in one click. A missing set of opening hours is not a
subtle finding -- it is the reason somebody rang a competitor at seven in the
evening.

**Where the data comes from, and why it is not scraped.** Google's robots.txt
allows ``/maps/search/`` and disallows ``/maps/`` place pages. Titan attaches a
one-pager to every message stating "robots.txt obeyed", so reading a place page
with a browser would make a document this system sends to strangers untrue --
in a system whose entire discipline is never asserting what it cannot evidence.
Every field below comes from the Places API instead, under the licence the
workspace already pays for.

**The same rule as everywhere else.** A claim is what was *read*, never what
was inferred. "Your Google listing shows no opening hours" is checkable by the
recipient in one click and is wrong only if Google is wrong. "You are losing
customers in the evenings" would be neither.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection

from titan.db.enums import FindingCategory, Severity, VerificationMethod
from titan.intelligence.findings import DetectedFinding

#: Confidence for a profile reading.
#:
#: Higher than an absence read off a crawl (0.8): a website absence means "not
#: on the pages we managed to fetch", whereas this is a single authoritative
#: record with a field either filled in or empty. It is still not 1.0, because
#: the profile can be edited between the read and the send.
PROFILE_CONFIDENCE = 0.85

#: Used while a defect class is being counted rather than sent. Below
#: `DetectedFinding.is_pitchable`'s 0.7 floor, exactly as absence.py stages its
#: own claims: detected, stored and scored, but never composed into a sentence.
UNPITCHABLE_CONFIDENCE = 0.5

#: Below this, a listing has effectively no gallery. Not a judgement about
#: photography -- a profile with one photo is one somebody set up and never
#: returned to, which is the thing being observed.
MIN_PHOTOS = 3

# There is deliberately no reply-rate detector, and `reviews` is deliberately
# not in the field mask.
#
# The Places Review object has no reply field: `name`, `text`, `originalText`,
# `rating`, `authorAttribution`, `publishTime`, `flagContentUri`,
# `googleMapsUri`, `visitDate` -- and nothing for the owner's response. Google
# does not expose owner replies through this API at all.
#
# So a reply habit cannot be measured from here, at any sample size. Reading
# every returned review and finding no reply field said "nobody replies" about
# twelve consecutive businesses, when what it actually described was the
# response shape. The other route -- reading the place page -- is the one
# Google's robots.txt disallows, and the one-pager attached to every message
# says robots.txt is obeyed. So this stays unmeasured and unsaid.
#
# Dropping `reviews` from the mask is also what takes the profile read off the
# Enterprise SKU for everything except photos.

@dataclasses.dataclass(frozen=True, slots=True)
class ProfileSnapshot:
    """One Google Business Profile, as the Places API returned it.

    Every field is optional and ``None`` means *not returned*, which is not the
    same as absent. A mask that did not ask for photos must never produce "this
    business has no photos" -- that would be a fact about our request, not about
    them, which is the failure mode this codebase names as a claim about our
    crawler rather than about their site.
    """

    place_id: str
    listing_url: str
    website_uri: str | None = None
    has_opening_hours: bool | None = None
    photo_count: int | None = None
    #: Every review the business has, as Google counts them. Carried as
    #: context, not as a claim -- nothing in the listing makes a review count
    #: a defect.
    review_count: int | None = None


def _finding(
    *,
    issue_type: str,
    category: FindingCategory,
    title: str,
    observed: str,
    expected: str,
    impact: str,
    solution: str,
    listing_url: str,
    pitchable: bool,
    severity: Severity = Severity.MEDIUM,
) -> DetectedFinding:
    return DetectedFinding(
        category=category,
        issue_type=issue_type,
        title=title,
        severity=severity,
        confidence=PROFILE_CONFIDENCE if pitchable else UNPITCHABLE_CONFIDENCE,
        # The field was read from a structured API response, not inferred from
        # rendered markup -- which is a stronger warrant than a DOM assertion,
        # but DOM_ASSERTION is the closest existing value meaning "measured".
        verification_method=VerificationMethod.DOM_ASSERTION,
        page_url=listing_url,
        observed_value=observed,
        expected_behavior=expected,
        business_impact=impact,
        recommended_solution=solution,
        evidence=((observed, listing_url),),
    )


def findings_from_profile(
    snapshot: ProfileSnapshot, *, pitchable: bool = False
) -> list[DetectedFinding]:
    """Everything this listing shows to be missing.

    Returns an empty list when nothing was measured, rather than a list of
    absences -- a profile nobody could read is not a profile with nothing in
    it.
    """
    out: list[DetectedFinding] = []

    # The headline one, and the only finding in this system that can be made
    # about a business with no website at all.
    if snapshot.website_uri is not None and not snapshot.website_uri.strip():
        out.append(
            _finding(
                issue_type="no_website_listed",
                category=FindingCategory.CONVERSION,
                title="Google lists this business with no website",
                observed="the Google listing has no website field filled in",
                expected="a website somebody can be sent to",
                impact=(
                    "Everyone who finds you on Google and wants to know more has "
                    "nowhere to go, so the enquiry either becomes a phone call "
                    "during opening hours or goes to whoever does have a site"
                ),
                solution="A site that answers the questions the phone currently answers",
                listing_url=snapshot.listing_url,
                pitchable=pitchable,
                # Higher than the rest: this is not a gap in a profile, it is
                # the absence of the thing every other finding is about.
                severity=Severity.HIGH,
            )
        )

    if snapshot.has_opening_hours is False:
        out.append(
            _finding(
                issue_type="no_opening_hours_listed",
                category=FindingCategory.CONVERSION,
                title="Google lists no opening hours for this business",
                observed="the Google listing shows no opening hours",
                expected="hours, so somebody searching at 8pm knows whether to call",
                impact=(
                    "Google shows nothing where it would normally say open or "
                    "closed, and a search that cannot answer that question is "
                    "usually answered by the next result down"
                ),
                solution="Publish the hours, and a way to book outside them",
                listing_url=snapshot.listing_url,
                pitchable=pitchable,
            )
        )

    if snapshot.photo_count is not None and snapshot.photo_count < MIN_PHOTOS:
        out.append(
            _finding(
                issue_type="listing_has_almost_no_photos",
                category=FindingCategory.CONTENT,
                title="The Google listing has almost no photographs",
                observed=f"{snapshot.photo_count} photo(s) on the Google listing",
                expected="enough photographs to show what the place is like",
                impact=(
                    "A listing with no photographs reads as one nobody has "
                    "looked after, next to competitors showing their rooms"
                ),
                solution="A maintained profile, kept current without anybody remembering",
                listing_url=snapshot.listing_url,
                pitchable=pitchable,
            )
        )

    # There is deliberately no "listing has no description" finding here.
    #
    # The only description field Places exposes is `editorialSummary`, and that
    # is Google's own copy about the place -- the documentation requires it to
    # be shown exactly as provided. The description the *owner* writes lives in
    # their Business Profile, which this API does not return at all.
    #
    # So an absent editorialSummary means Google has not written a summary for
    # this business. Saying "your listing has no description" on that evidence
    # would be a claim the recipient disproves in one click by opening their
    # profile and reading the description they wrote -- in a system whose whole
    # standing rests on the opposite. Measured live, the field was absent from
    # every listing read, so this would have been said to nearly everybody.
    # The copy for the issue type is left in the composer against the day an
    # owner-authorised source can answer it honestly.

    return out


def snapshot_from_places(
    payload: dict,
    *,
    place_id: str | None = None,
    requested: Collection[str] | None = None,
) -> ProfileSnapshot:
    """Map a Places profile response onto a snapshot.

    The whole of this function is the rule that an unmeasured field must never
    become a claim. What changed, and why it had to:

    **Places omits a field entirely when the business has nothing in it.** It
    does not return it blank. Measured against live listings, ``websiteUri``,
    ``regularOpeningHours`` and ``editorialSummary`` were simply absent from
    the payload rather than present-and-empty -- ``editorialSummary`` was
    missing from every profile read, on a mask that explicitly asked for it.

    So key presence cannot distinguish the two cases. Reading absence as "not
    returned" made every absence unclaimable: the three detectors above could
    never fire, and this function's caller reported ``no_evidence`` on a
    business with no website at all. That is the failure this codebase keeps
    meeting -- a pass that runs, logs nothing, and is indistinguishable from a
    healthy one.

    **What can answer it is our own field mask.** We know what we asked for,
    because we wrote it. `requested` carries that list:

    * asked for, and present   -> they have it
    * asked for, and absent    -> they do not (a claim, checkable in one click)
    * not asked for            -> unmeasured, and nothing is said

    `requested` is not optional in spirit. Omitting it falls back to key
    presence, which claims nothing -- safe, and wrong in the direction that
    loses findings rather than the one that invents them.

    **Why an absent field is trustworthy here.** Places v1 rejects a bad field
    mask with an error rather than quietly dropping fields, so a response that
    arrived at all had its mask honoured. That inference is what makes omission
    a fact about the business; it is checked below by requiring the response to
    carry its own identity before any absence is read from it.
    """
    asked = frozenset(requested or ())

    # A response too thin to identify is not a listing with nothing in it. If
    # Places ever does start returning degraded payloads, this is the line that
    # keeps the degradation from being published as a claim about every
    # business at once.
    answered = bool(payload) and bool(payload.get("id") or payload.get("googleMapsUri"))

    def _measured(key: str) -> bool:
        """Did we get an answer about this field -- either way?"""
        if key in payload:
            return True
        return answered and key in asked

    def _flag(key: str) -> bool | None:
        """True/False when measured, None when the field was never asked."""
        return bool(payload.get(key)) if _measured(key) else None

    website = payload.get("websiteUri")

    photos = payload.get("photos")
    if isinstance(photos, list):
        photo_count = len(photos)
    elif _measured("photos"):
        # Asked for, nothing came back: a listing with no photographs at all.
        photo_count = 0
    else:
        photo_count = None

    return ProfileSnapshot(
        place_id=str(place_id or payload.get("id") or ""),
        listing_url=str(payload.get("googleMapsUri") or ""),
        # "" is the claim "they have no website"; None is "we never asked".
        website_uri=("" if _measured("websiteUri") and not website else website),
        has_opening_hours=_flag("regularOpeningHours"),
        photo_count=photo_count,
        review_count=payload.get("userRatingCount"),
    )


__all__ = [
    "MIN_PHOTOS",
    "PROFILE_CONFIDENCE",
    "ProfileSnapshot",
    "findings_from_profile",
    "snapshot_from_places",
]
