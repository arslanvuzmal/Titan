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

#: Below this share of reviews answered, nobody is replying as a matter of
#: habit. Set low on purpose: the claim is "almost none", not "not enough".
MIN_REVIEW_REPLY_RATE = 0.1

#: Only businesses with at least this many reviews are judged on replies. Two
#: unanswered reviews is not a policy.
MIN_REVIEWS_TO_JUDGE_REPLIES = 5


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
    review_count: int | None = None
    replied_review_count: int | None = None
    has_description: bool | None = None


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

    if (
        snapshot.review_count is not None
        and snapshot.replied_review_count is not None
        and snapshot.review_count >= MIN_REVIEWS_TO_JUDGE_REPLIES
    ):
        rate = snapshot.replied_review_count / snapshot.review_count
        if rate < MIN_REVIEW_REPLY_RATE:
            out.append(
                _finding(
                    issue_type="reviews_go_unanswered",
                    category=FindingCategory.RETENTION,
                    title="Reviews on the Google listing are not replied to",
                    observed=(
                        f"{snapshot.replied_review_count} of "
                        f"{snapshot.review_count} reviews have a reply from the business"
                    ),
                    expected="a reply to each review, particularly the unhappy ones",
                    impact=(
                        "An unanswered complaint is the last thing a prospective "
                        "customer reads about you, and replying is the only part "
                        "of it you control"
                    ),
                    solution="Automatic review requests, and drafted replies to approve",
                    listing_url=snapshot.listing_url,
                    pitchable=pitchable,
                )
            )

    if snapshot.has_description is False:
        out.append(
            _finding(
                issue_type="listing_has_no_description",
                category=FindingCategory.CONTENT,
                title="The Google listing has no description",
                observed="the Google listing has no business description",
                expected="a description saying what the business does",
                impact=(
                    "Google shows whatever it can infer instead, which is the "
                    "one piece of copy about you that you did not write"
                ),
                solution="A written profile, and a site it can point at",
                listing_url=snapshot.listing_url,
                pitchable=pitchable,
            )
        )

    return out


def snapshot_from_places(payload: dict, *, place_id: str | None = None) -> ProfileSnapshot:
    """Map a Places profile response onto a snapshot.

    The whole of this function is the rule that `None` means *not returned*.
    Places omits a key entirely when the field was not asked for **and** when
    the business genuinely has nothing there, and those two cases must not
    collapse -- one is a fact about them, the other a fact about our mask.

    `regularOpeningHours` and `editorialSummary` are omitted in both cases, so
    they are read as measured only when the mask asked for them; the caller
    passes a payload fetched with PROFILE_FIELD_MASK, which did. `photos` and
    `reviews` come back as lists, and an empty list is a genuine zero rather
    than a silence.
    """
    def _asked(key: str) -> bool:
        # A key present at all -- even empty -- means Places answered on it.
        return key in payload

    website = payload.get("websiteUri")
    reviews = payload.get("reviews")
    replied = None
    if isinstance(reviews, list):
        # Google nests the owner's response under the review it answers.
        replied = sum(1 for r in reviews if isinstance(r, dict) and r.get("authorAttribution") and r.get("originalText") and r.get("reply"))
        if not any(isinstance(r, dict) and "reply" in r for r in reviews):
            # The field is not in this response shape at all, so "none replied"
            # would be our omission wearing their name.
            replied = None

    photos = payload.get("photos")

    return ProfileSnapshot(
        place_id=str(place_id or payload.get("id") or ""),
        listing_url=str(payload.get("googleMapsUri") or ""),
        # "" means Places answered and the field was blank; None means it did
        # not answer. Only the first is a claim.
        website_uri=("" if _asked("websiteUri") and not website else website),
        has_opening_hours=(
            bool(payload.get("regularOpeningHours")) if _asked("regularOpeningHours") else None
        ),
        photo_count=(len(photos) if isinstance(photos, list) else None),
        review_count=payload.get("userRatingCount"),
        replied_review_count=replied,
        has_description=(
            bool(payload.get("editorialSummary")) if _asked("editorialSummary") else None
        ),
    )


__all__ = [
    "MIN_PHOTOS",
    "MIN_REVIEWS_TO_JUDGE_REPLIES",
    "MIN_REVIEW_REPLY_RATE",
    "PROFILE_CONFIDENCE",
    "ProfileSnapshot",
    "findings_from_profile",
    "snapshot_from_places",
]
