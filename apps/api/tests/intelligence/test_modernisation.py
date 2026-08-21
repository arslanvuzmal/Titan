"""Telling a business that already runs AI from one that answers the phone.

The distinction the operator asked for, and the one that decides whether a lead
is worth writing to at all. A practice running an AI receptionist and one taking
bookings by telephone used to score identically, because the only thing being
scored was whether their website was broken.

Most of what follows is about the third state. ``NOT_MEASURED`` is what keeps
this from being a machine that ranks cookie walls as perfect prospects.
"""

from __future__ import annotations

import pytest
from titan.intelligence.modernisation import (
    GAP_WEIGHTS,
    MIN_MEASURED_CAPABILITIES,
    VENDORS,
    Capability,
    PageSignals,
    Signal,
    booking_link_is_a_system,
    merge,
    profile,
)


def page(**overrides) -> PageSignals:
    return PageSignals(**overrides)


# ------------------------------------------------------------- they have it


def test_a_practice_running_intercom_is_not_a_prospect_for_chat() -> None:
    p = profile(page(technologies=("wordpress", "intercom")))

    assert p.signals[Capability.CONVERSATIONAL] is Signal.PRESENT


def test_a_vendor_is_matched_inside_a_script_url() -> None:
    """Detectors report "intercom"; script tags say "widget.intercom.io"."""
    p = profile(page(script_urls=("https://widget.intercom.io/widget/abc123",)))

    assert p.signals[Capability.CONVERSATIONAL] is Signal.PRESENT


def test_a_chat_widget_we_cannot_attribute_still_counts() -> None:
    """Planted violation: require a known vendor and every business running
    something we have not heard of is scored as having nothing.

    The DOM check saw a launcher. A widget whose vendor is unknown is still a
    widget the business runs.
    """
    p = profile(page(has_chat_widget=True))

    assert p.signals[Capability.CONVERSATIONAL] is Signal.PRESENT


def test_a_booking_link_to_an_unknown_system_still_counts() -> None:
    """Off-site and not social: the vendor is unknown, the fact is not."""
    p = profile(
        page(
            booking_links=("https://bookings.example.test/practice",),
            site_host="practice.test",
        )
    )

    assert p.signals[Capability.SELF_SERVICE_BOOKING] is Signal.PRESENT


@pytest.mark.parametrize(
    ("token", "capability"),
    [
        ("calendly", Capability.SELF_SERVICE_BOOKING),
        ("dentally", Capability.SELF_SERVICE_BOOKING),
        ("fresha", Capability.SELF_SERVICE_BOOKING),
        ("hubspot", Capability.MARKETING_AUTOMATION),
        ("klaviyo", Capability.MARKETING_AUTOMATION),
        ("trustpilot", Capability.REPUTATION_AUTOMATION),
        ("google-analytics", Capability.ANALYTICS),
        ("nextjs", Capability.MODERN_SITE),
    ],
)
def test_each_vendor_proves_its_own_capability(token, capability) -> None:
    p = profile(page(technologies=(token,)))

    assert p.signals[capability] is Signal.PRESENT


# ------------------------------------------------- absence is not the same fact


def test_a_readable_page_with_nothing_on_it_is_the_prospect_we_want() -> None:
    p = profile(page(technologies=("wordpress",)))

    assert p.signals[Capability.CONVERSATIONAL] is Signal.ABSENT
    assert p.signals[Capability.SELF_SERVICE_BOOKING] is Signal.ABSENT
    assert p.gap == 1.0


def test_a_cookie_wall_means_not_measured_not_absent() -> None:
    """Planted violation: treat obstruction as absence and every cookie-walled
    site becomes a perfect prospect.

    Cookie walls are commonest exactly where budgets are largest, so the bug
    would systematically rank the worst prospects highest.
    """
    p = profile(page(obstructed=True))

    assert all(s is Signal.NOT_MEASURED for s in p.signals.values())
    assert p.gap is None


def test_an_unreadable_page_measures_nothing() -> None:
    p = profile(page(readable=False))

    assert p.gap is None


def test_a_widget_found_under_a_cookie_wall_still_counts() -> None:
    """Obstruction does not un-run the script that was found underneath it.
    Positive detection wins over everything."""
    p = profile(page(technologies=("intercom",), obstructed=True))

    assert p.signals[Capability.CONVERSATIONAL] is Signal.PRESENT
    assert p.signals[Capability.ANALYTICS] is Signal.NOT_MEASURED


# ------------------------------------------------------------- the gap score


def test_the_score_is_withheld_below_the_floor() -> None:
    """Planted violation: score on any evidence at all and two observations
    produce a confident-looking number about how a business runs."""
    signals = {c: Signal.NOT_MEASURED for c in Capability}
    signals[Capability.CONVERSATIONAL] = Signal.ABSENT
    signals[Capability.ANALYTICS] = Signal.ABSENT
    from titan.intelligence.modernisation import ModernisationProfile

    p = ModernisationProfile(signals=signals)

    assert len(p.measured) < MIN_MEASURED_CAPABILITIES
    assert p.gap is None


def test_none_is_not_zero() -> None:
    """A caller must be able to tell "we did not learn enough" from "they are
    already modern". Both are unattractive leads for opposite reasons."""
    unknown = profile(page(obstructed=True))
    modern = profile(
        page(
            technologies=(
                "intercom",
                "calendly",
                "hubspot",
                "trustpilot",
                "google-analytics",
                "nextjs",
            )
        )
    )

    assert unknown.gap is None
    assert modern.gap == 0.0


def test_missing_measurements_do_not_penalise_the_business() -> None:
    """The score is renormalised over what was measured. A business whose
    analytics we could not read must not be scored as though it had none."""
    fully_traditional = profile(page(technologies=("wordpress",)))

    signals = dict(fully_traditional.signals)
    signals[Capability.ANALYTICS] = Signal.NOT_MEASURED
    signals[Capability.MODERN_SITE] = Signal.NOT_MEASURED
    from titan.intelligence.modernisation import ModernisationProfile

    partial = ModernisationProfile(signals=signals)

    assert partial.gap == 1.0, "renormalised, not diluted toward zero"


def test_the_weights_sum_to_one() -> None:
    """Planted violation: change one weight without rebalancing and every gap
    score silently shifts."""
    assert round(sum(GAP_WEIGHTS.values()), 6) == 1.0
    assert set(GAP_WEIGHTS) == set(Capability)


def test_chat_and_booking_carry_most_of_the_score() -> None:
    """They are what the operator sells. Analytics is an indicator."""
    assert (
        GAP_WEIGHTS[Capability.CONVERSATIONAL]
        + GAP_WEIGHTS[Capability.SELF_SERVICE_BOOKING]
    ) >= 0.5
    assert GAP_WEIGHTS[Capability.ANALYTICS] < 0.1


def test_a_partly_modernised_business_scores_between() -> None:
    """Booking but no chat: half the headline capability, and a real prospect
    for the other half."""
    p = profile(page(technologies=("wordpress", "calendly", "google-analytics")))

    assert p.gap is not None
    assert 0.0 < p.gap < 1.0
    assert Capability.SELF_SERVICE_BOOKING in p.present
    assert Capability.CONVERSATIONAL in p.absent


# ------------------------------------------------------- across several pages


def test_a_capability_found_on_any_page_counts_for_the_business() -> None:
    """Booking lives on the booking page and chat is often only on the home
    page. Requiring every page to show it would find nothing."""
    home = profile(page(technologies=("wordpress", "intercom")))
    booking = profile(page(technologies=("wordpress", "calendly")))

    both = merge([home, booking])

    assert both.signals[Capability.CONVERSATIONAL] is Signal.PRESENT
    assert both.signals[Capability.SELF_SERVICE_BOOKING] is Signal.PRESENT


def test_absence_needs_one_page_where_it_was_looked_for() -> None:
    obstructed = profile(page(obstructed=True))
    readable = profile(page(technologies=("wordpress",)))

    assert merge([obstructed, readable]).signals[Capability.CONVERSATIONAL] is (
        Signal.ABSENT
    )


def test_a_site_obstructed_throughout_stays_unmeasured() -> None:
    """Planted violation: let merge default to ABSENT and a site nobody could
    read becomes the highest-priority lead in the database."""
    everywhere = [profile(page(obstructed=True)) for _ in range(6)]

    assert merge(everywhere).gap is None


def test_merging_nothing_measures_nothing() -> None:
    assert merge([]).gap is None


# ------------------------------------------------------------ what it is not


def test_the_description_carries_no_score() -> None:
    """This ranks prospects; it never justifies a claim. "You have no online
    booking" drawn from a missing script tag is not evidence a person would
    recognise, and nothing here may reach a message."""
    text = profile(page(technologies=("wordpress", "calendly"))).describe()

    assert "runs:" in text and "lacks:" in text
    assert not any(ch.isdigit() for ch in text)


def test_no_vendor_token_is_a_bare_english_word() -> None:
    """Planted violation: add "chat" or "book" and half the CSS classes on a
    dental website match. Vendor names only."""
    too_generic = {"chat", "book", "booking", "review", "reviews", "analytics", "ai"}

    for capability, vendors in VENDORS.items():
        assert not (vendors & too_generic), capability


# ------------------------------------------- a link that says "Book" is not one


def test_a_facebook_page_is_not_a_booking_system() -> None:
    """Planted violation: accept any booking link and this fails.

    The crawler's booking pattern matched "book" inside "facebook.com", so
    2,056 of 2,494 crawled businesses were recorded as taking online bookings.
    The pattern is fixed; this is the second line, because the same
    over-counting returns with any future loosening of it.
    """
    assert not booking_link_is_a_system(
        "https://www.facebook.com/citycentredentalclinic/",
        site_host="citycentredental.co.uk",
    )


def test_a_named_vendor_counts_wherever_it_is_hosted() -> None:
    assert booking_link_is_a_system(
        "https://calendly.com/practice/checkup", site_host="practice.test"
    )
    assert booking_link_is_a_system(
        "https://practice.dentally.co/book", site_host="practice.test"
    )


def test_leaving_the_site_to_book_counts() -> None:
    """A practice that sends visitors to another host to book is using
    something, even if we cannot name it."""
    assert booking_link_is_a_system(
        "https://nightandday.seamlessslot.co.uk/appointment?locationId=3",
        site_host="nightanddaydental.co.uk",
    )


def test_a_book_page_on_the_practices_own_domain_does_not_count() -> None:
    """As likely a form and a phone number as a booking system. Wrong in the
    safe direction: it under-counts what the business has, so the lead is
    ranked lower rather than being pitched something it already runs."""
    assert not booking_link_is_a_system(
        "https://practice.test/book-online", site_host="practice.test"
    )
    assert not booking_link_is_a_system(
        "https://www.practice.test/appointments", site_host="practice.test"
    )


def test_without_a_site_host_only_named_vendors_count() -> None:
    """Planted violation: default to counting every link and the whole
    over-count returns for any caller that does not supply the host."""
    assert not booking_link_is_a_system("https://anything.test/book", site_host=None)
    assert booking_link_is_a_system("https://calendly.com/x", site_host=None)


def test_the_profile_uses_the_tightened_rule() -> None:
    social = profile(
        page(
            technologies=("wordpress",),
            booking_links=("https://www.facebook.com/practice/",),
            site_host="practice.test",
        )
    )
    real = profile(
        page(
            technologies=("wordpress",),
            booking_links=("https://calendly.com/practice",),
            site_host="practice.test",
        )
    )

    assert social.signals[Capability.SELF_SERVICE_BOOKING] is Signal.ABSENT
    assert real.signals[Capability.SELF_SERVICE_BOOKING] is Signal.PRESENT
