"""Admitting a business that has no website of its own.

Discovery has always refused these, and the reasoning in the module is sound:
every message is built from evidence a crawler gathered on the recipient's own
site, so a business with no site produces no evidence, no finding and no claim.
Admitting them would produce a lead the pipeline must refuse later, after
paying for the crawl.

What that reasoning missed is that *having no website* is itself a measured
fact about the business -- read from their own Google listing, checkable by
them in one click, and a great deal more legible than a missing alt attribute.
The evidence rule is not relaxed here. The evidence moves.

Two separate questions were also being answered by one list. "Can I audit this
page?" and "might the business's own email be on it?" are not the same, and a
practice whose only web presence is a Facebook page was refused outright even
though that page routinely carries an address the owner typed in themselves.

Nothing changes unless `allow_siteless=True` is passed. These tests pin both
directions, because the default must stay exactly as it was.
"""

from __future__ import annotations

import pytest
from titan.intelligence.discovery import (
    Admission,
    LeadKind,
    Refusal,
    admit,
    is_auditable_host,
    is_contact_source,
)
from titan.providers.places import DiscoveredBusiness


def _business(
    *,
    domain: str | None,
    place_id: str = "place-1",
    reviews: int = 40,
    rating: float = 4.6,
    operational: bool = True,
) -> DiscoveredBusiness:
    return DiscoveredBusiness(
        place_id=place_id,
        display_name="A Dental Practice",
        formatted_address="1 High Street",
        website_uri=(f"https://{domain}/" if domain else None),
        phone="+44 20 7000 0000",
        rating=rating,
        review_count=reviews,
        business_status="OPERATIONAL" if operational else "CLOSED_PERMANENTLY",
        primary_type="dentist",
        latitude=None,
        longitude=None,
    )


# ------------------------------------------------- the default must not move


def test_no_website_is_still_refused_by_default() -> None:
    assert admit(_business(domain=None)).refusal is Refusal.NO_WEBSITE


def test_a_social_profile_is_still_refused_by_default() -> None:
    assert (
        admit(_business(domain="facebook.com/apractice")).refusal
        is Refusal.NON_AUDITABLE_HOST
    )


def test_an_ordinary_site_is_admitted_as_auditable() -> None:
    admission = admit(_business(domain="apractice.co.uk"))
    assert admission.admitted
    assert admission.kind is LeadKind.AUDITABLE


# --------------------------------------------------- the two questions, split


@pytest.mark.parametrize(
    ("host", "auditable", "contactable"),
    [
        ("apractice.co.uk", True, False),
        ("facebook.com", False, True),
        ("www.instagram.com", False, True),
        ("yell.com", False, True),
        # Nothing behind a shortener but a redirect: nothing to audit and
        # nothing of theirs to read either.
        ("linktr.ee", False, False),
        ("bit.ly", False, False),
        # A retired builder. The address survives in Places records long after
        # the page stopped resolving, so there is no profile to read.
        ("business.site", False, False),
    ],
)
def test_auditable_and_contactable_are_different_questions(
    host: str, auditable: bool, contactable: bool
) -> None:
    assert is_auditable_host(host) is auditable
    assert is_contact_source(host) is contactable


def test_a_host_is_never_both(
) -> None:
    """An auditable host is the ordinary path, not this exception."""
    assert is_contact_source("apractice.co.uk") is False


# ------------------------------------------------------- with the flag raised


def test_no_website_is_admitted_as_siteless_when_asked_for() -> None:
    admission = admit(_business(domain=None), allow_siteless=True)
    assert admission.admitted
    assert admission.kind is LeadKind.SITELESS


def test_a_social_profile_is_admitted_as_siteless() -> None:
    admission = admit(_business(domain="facebook.com"), allow_siteless=True)
    assert admission.admitted
    assert admission.kind is LeadKind.SITELESS


def test_a_shortener_is_refused_even_when_siteless_is_allowed() -> None:
    """The flag admits businesses, not redirects.

    There is no profile behind a shortener, so it yields neither a finding nor
    a contact -- which is the lead the original rule existed to avoid paying
    for.
    """
    admission = admit(_business(domain="linktr.ee"), allow_siteless=True)
    assert admission.refusal is Refusal.NON_AUDITABLE_HOST


# ------------------------------------------- the rules that must still apply


def test_a_closed_business_is_refused_before_anything_else() -> None:
    """Ordered most-informative-first: closed, not siteless."""
    admission = admit(
        _business(domain=None, operational=False), allow_siteless=True
    )
    assert admission.refusal is Refusal.NOT_OPERATIONAL


def test_quality_floors_still_apply_to_a_siteless_business() -> None:
    assert (
        admit(_business(domain=None, reviews=1), allow_siteless=True).refusal
        is Refusal.TOO_FEW_REVIEWS
    )
    assert (
        admit(_business(domain=None, rating=2.0), allow_siteless=True).refusal
        is Refusal.RATING_BELOW_FLOOR
    )


def test_a_siteless_business_still_dedupes_on_its_place_id() -> None:
    """The domain is the usual dedupe key and there isn't one.

    Without this a siteless business is re-admitted on every search that
    returns it, and each admission is another lead for the same company.
    """
    seen = frozenset({"place-1"})
    admission = admit(
        _business(domain=None, place_id="place-1"),
        known_place_ids=seen,
        allow_siteless=True,
    )
    assert admission.refusal is Refusal.ALREADY_KNOWN


def test_suppression_does_not_crash_on_a_business_with_no_domain() -> None:
    """The suppression check reads a domain that may not exist."""
    admission = admit(
        _business(domain=None),
        suppressed_domains=frozenset({"someone.co.uk"}),
        allow_siteless=True,
    )
    assert admission.admitted


def test_a_suppressed_domain_is_still_refused_with_the_flag_on() -> None:
    admission = admit(
        _business(domain="apractice.co.uk"),
        suppressed_domains=frozenset({"apractice.co.uk"}),
        allow_siteless=True,
    )
    assert admission.refusal is Refusal.SUPPRESSED_DOMAIN


def test_the_admission_default_kind_is_auditable() -> None:
    """Anything constructing an Admission without saying must mean the old path."""
    assert Admission(_business(domain="x.co.uk")).kind is LeadKind.AUDITABLE
