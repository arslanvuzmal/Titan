"""Reading a Google Business Profile for what it does not contain.

The point of this detector is that it works for a business with **no website**,
which every other detector in the system cannot say anything about at all.

The rule it must not break is the one the whole codebase turns on: a claim is
what was read, never what was inferred, and never a fact about our own request
dressed up as a fact about them. `None` in a snapshot means *the field was not
returned*. A mask that did not ask for photos must never produce "this business
has no photos" -- so the unmeasured cases below are the load-bearing tests, not
the populated ones.
"""

from __future__ import annotations

import pytest
from titan.intelligence.profile_defects import (
    MIN_PHOTOS,
    ProfileSnapshot,
    findings_from_profile,
    snapshot_from_places,
)

LISTING = "https://maps.google.com/?cid=123"


def _snap(**kw) -> ProfileSnapshot:
    return ProfileSnapshot(place_id="p1", listing_url=LISTING, **kw)


def _types(snapshot: ProfileSnapshot, *, pitchable: bool = True) -> set[str]:
    return {f.issue_type for f in findings_from_profile(snapshot, pitchable=pitchable)}


# ------------------------------------- nothing measured means nothing claimed


def test_an_empty_snapshot_produces_no_findings() -> None:
    """A profile nobody could read is not a profile with nothing in it."""
    assert findings_from_profile(_snap()) == []


@pytest.mark.parametrize(
    "field",
    ["has_opening_hours", "photo_count"],
)
def test_an_unreturned_field_is_never_an_absence(field: str) -> None:
    """None is "not asked for", not "not there".

    This is the difference between a fact about the business and a fact about
    our field mask -- the exact confusion this codebase elsewhere calls a claim
    about our crawler rather than about their site.
    """
    assert _types(_snap(**{field: None})) == set()


def test_a_missing_website_field_is_not_the_same_as_an_empty_one() -> None:
    """Not asking for websiteUri must not assert they have no website."""
    assert "no_website_listed" not in _types(_snap(website_uri=None))
    assert "no_website_listed" in _types(_snap(website_uri=""))


# ------------------------------------------------------------ what it detects


def test_a_siteless_listing_yields_the_headline_finding() -> None:
    findings = findings_from_profile(_snap(website_uri=""), pitchable=True)
    assert [f.issue_type for f in findings] == ["no_website_listed"]
    assert findings[0].severity.value == "high"
    assert findings[0].page_url == LISTING


def test_a_business_with_a_website_gets_no_website_finding() -> None:
    assert "no_website_listed" not in _types(_snap(website_uri="https://x.co.uk"))


def test_missing_hours_are_detected() -> None:
    assert "no_opening_hours_listed" in _types(_snap(has_opening_hours=False))
    assert "no_opening_hours_listed" not in _types(_snap(has_opening_hours=True))


@pytest.mark.parametrize("count", [0, 1, MIN_PHOTOS - 1])
def test_a_listing_with_almost_no_photos_is_detected(count: int) -> None:
    assert "listing_has_almost_no_photos" in _types(_snap(photo_count=count))


def test_a_listing_with_enough_photos_is_not(
) -> None:
    assert "listing_has_almost_no_photos" not in _types(_snap(photo_count=MIN_PHOTOS))








def test_a_neglected_listing_yields_every_finding_at_once() -> None:
    """The population this is for: findable on Google, invisible everywhere else."""
    assert _types(
        _snap(
            website_uri="",
            has_opening_hours=False,
            photo_count=1,
        )
    ) == {
        "no_website_listed",
        "no_opening_hours_listed",
        "listing_has_almost_no_photos",
    }


# ----------------------------------------------------------------- staging


def test_findings_are_unpitchable_until_switched_on() -> None:
    """Counted before they are said, exactly as absence.py stages its claims."""
    unpitchable = findings_from_profile(_snap(website_uri=""), pitchable=False)
    pitchable = findings_from_profile(_snap(website_uri=""), pitchable=True)

    assert not unpitchable[0].is_pitchable()
    assert pitchable[0].is_pitchable()


def test_every_finding_cites_the_listing_it_was_read_from() -> None:
    """No claim without a citation the recipient can open."""
    findings = findings_from_profile(
        _snap(website_uri="", has_opening_hours=False, photo_count=0), pitchable=True
    )
    assert findings
    for f in findings:
        assert f.evidence
        assert all(url == LISTING for _observed, url in f.evidence)


# ------------------------------------------- mapping the Places payload in


def _payload(**kw) -> dict:
    base = {"id": "p1", "googleMapsUri": LISTING}
    base.update(kw)
    return base


def test_a_thin_payload_claims_nothing() -> None:
    """A mask that asked for nothing extra must produce no findings.

    This is the mapping's whole job. Places omits a key both when we did not
    ask and when the business has nothing there, and collapsing those two is
    how "our request" becomes "their listing".
    """
    snapshot = snapshot_from_places(_payload(rating=4.5))
    assert findings_from_profile(snapshot, pitchable=True) == []


def test_a_blank_website_field_is_a_claim_but_a_missing_one_is_not() -> None:
    assert snapshot_from_places(_payload(websiteUri="")).website_uri == ""
    assert snapshot_from_places(_payload()).website_uri is None


def test_opening_hours_are_only_read_when_places_answered() -> None:
    assert snapshot_from_places(_payload(regularOpeningHours=None)).has_opening_hours is False
    assert snapshot_from_places(_payload(regularOpeningHours={"x": 1})).has_opening_hours is True
    assert snapshot_from_places(_payload()).has_opening_hours is None


def test_an_empty_photo_list_is_a_real_zero() -> None:
    """A list that came back empty is an answer; an absent key is not."""
    assert snapshot_from_places(_payload(photos=[])).photo_count == 0
    assert snapshot_from_places(_payload()).photo_count is None



def test_the_listing_url_is_carried_through_as_the_citation() -> None:
    assert snapshot_from_places(_payload()).listing_url == LISTING


# ------------------------- an absence Places expressed by saying nothing at all

# The bug these cover, found by running the activity against live listings
# rather than by reading the code:
#
# Places omits a field entirely when the business has nothing in it. It does
# not return it empty. `editorialSummary` was absent from every profile read,
# on a mask that asked for it by name.
#
# Reading that omission as "not returned" -- which the tests above assert for
# the case where we genuinely did not ask -- made three of the five detectors
# unable to fire at all, including `no_website_listed`, the one finding this
# whole module exists to produce. The activity dutifully reported `no_evidence`
# and looked exactly like a healthy pass.
#
# The distinguishing fact is our own field mask, so it is passed in.

ASKED = (
    "id",
    "googleMapsUri",
    "websiteUri",
    "regularOpeningHours",
    "photos",
    "userRatingCount",
)


def _asked(**kw) -> ProfileSnapshot:
    return snapshot_from_places(_payload(**kw), requested=ASKED)


def test_a_website_we_asked_for_and_did_not_get_is_a_business_with_no_website() -> None:
    """The headline finding, which could never fire before.

    A siteless business's payload simply has no `websiteUri` key. If that reads
    as "unmeasured", the module cannot say the one thing it was built to say.
    """
    snapshot = _asked()
    assert snapshot.website_uri == ""
    assert "no_website_listed" in {
        f.issue_type for f in findings_from_profile(snapshot, pitchable=True)
    }


def test_hours_we_asked_for_and_did_not_get_are_hours_they_have_not_published() -> None:
    assert _asked().has_opening_hours is False
    assert _asked(regularOpeningHours={"periods": []}).has_opening_hours is True


def test_googles_own_summary_is_never_turned_into_a_claim_about_their_copy() -> None:
    """The reason `editorialSummary` is not asked for at all.

    It is Google's copy about the place, not the description the owner wrote --
    that lives in the Business Profile, which this API does not return. Absent
    from every listing measured, so claiming it would have told nearly every
    recipient their listing has no description, which they disprove by opening
    their own profile.
    """
    from titan.providers.places import PROFILE_FIELDS

    assert "editorialSummary" not in PROFILE_FIELDS
    assert "listing_has_no_description" not in _types(
        _asked(editorialSummary={"text": "A dental practice"})
    )


def test_photos_we_asked_for_and_did_not_get_are_a_gallery_with_nothing_in_it() -> None:
    assert _asked().photo_count == 0
    assert _asked(photos=[{}, {}, {}]).photo_count == 3



def test_not_asking_still_claims_nothing_even_when_something_else_was_asked() -> None:
    """The rule the fix had to keep: only a field we asked about can be a claim."""
    snapshot = snapshot_from_places(_payload(), requested=("id", "googleMapsUri"))
    assert snapshot.website_uri is None
    assert snapshot.has_opening_hours is None
    assert snapshot.photo_count is None
    assert findings_from_profile(snapshot, pitchable=True) == []


def test_a_payload_that_cannot_identify_itself_is_a_failed_read_not_an_empty_listing(
) -> None:
    """The guard against publishing our own degradation as their listing.

    If Places ever returns a stripped response, every field we asked for would
    otherwise become an absence -- and the system would tell every business on
    the list, in the same hour, that they have no website.
    """
    snapshot = snapshot_from_places({}, place_id="p1", requested=ASKED)
    assert snapshot.website_uri is None
    assert snapshot.has_opening_hours is None
    assert snapshot.photo_count is None
    assert findings_from_profile(snapshot, pitchable=True) == []


def test_the_mask_and_the_claimable_fields_cannot_drift() -> None:
    """One list, used for the request and for what may be claimed from it."""
    from titan.providers.places import PROFILE_FIELD_MASK, PROFILE_FIELDS

    assert PROFILE_FIELD_MASK.split(",") == list(PROFILE_FIELDS)
    for field in ("websiteUri", "regularOpeningHours", "photos"):
        assert field in PROFILE_FIELDS


def test_a_neglected_listing_read_the_real_way_yields_every_finding() -> None:
    """End to end, in the shape Places actually answers in: by omission."""
    assert _types(_asked(userRatingCount=40)) == {
        "no_website_listed",
        "no_opening_hours_listed",
        "listing_has_almost_no_photos",
    }


def test_owner_replies_are_never_claimed_because_places_does_not_return_them() -> None:
    """Measured live: no review Places returned carried a reply field.

    The Review object has no reply on it -- Google does not expose owner
    responses through this API. A detector reading their absence said "nobody
    replies" about twelve consecutive businesses, when what it described was
    the response shape. `reviews` is not asked for, and nothing claims it.
    """
    from titan.providers.places import PROFILE_FIELDS

    assert "reviews" not in PROFILE_FIELDS
    payload = _payload(
        userRatingCount=40,
        reviews=[{"authorAttribution": {}, "originalText": {}} for _ in range(5)],
    )
    types = {
        f.issue_type
        for f in findings_from_profile(
            snapshot_from_places(payload, requested=ASKED), pitchable=True
        )
    }
    assert "reviews_go_unanswered" not in types
