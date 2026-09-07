"""Clinics, private hospitals and insurance.

Three verticals added because the gap Titan sells against is widest there: a
clinic that takes every booking by telephone during office hours is the exact
shape of the problem, and until now there was no playbook to work one under.

Insurance differs in kind from the other two and the tests say so. Its
conversion event is a quote request, not an appointment, so a playbook that
led with booking would be selling the wrong thing to the whole vertical.
"""

from __future__ import annotations

from titan.db.enums import Industry
from titan.intelligence.playbooks import PLAYBOOKS, get_playbook, select_offers


def test_the_three_new_verticals_have_playbooks() -> None:
    for industry in (Industry.CLINIC, Industry.PRIVATE_HOSPITAL, Industry.INSURANCE):
        assert industry in PLAYBOOKS, f"{industry} has no playbook"


def test_a_clinic_is_crawled_where_patients_actually_book() -> None:
    """Planted violation: give the clinic the generic path list.

    The crawl budget is eighteen pages and discovered links compete for it.
    A playbook that does not name /appointments spends the budget on an about
    page and reports that the site is fine.
    """
    paths = get_playbook(Industry.CLINIC).crawl_paths

    assert "/appointments" in paths
    assert "/new-patients" in paths


def test_insurance_sells_against_the_quote_not_the_appointment() -> None:
    """Planted violation: copy the dental playbook and rename it.

    A broker has no appointments. The conversion event is a quote request and
    the retention event is a renewal, so an offer set led by booking would be
    pitching a mechanism the business does not have.
    """
    playbook = get_playbook(Industry.INSURANCE)
    priorities = {p.key for p in playbook.priorities}

    assert "quote_request" in priorities
    assert "renewal_follow_up" in priorities
    assert not any("appointment" in key for key in priorities)


def test_a_hospital_pitch_turns_on_referrals_and_the_switchboard() -> None:
    """What is distinctive about a private hospital: enquiries arrive from GPs
    and consultants as well as patients, and they land on a switchboard."""
    priorities = {p.key for p in get_playbook(Industry.PRIVATE_HOSPITAL).priorities}

    assert "referral_routing" in priorities


def test_no_new_vertical_offers_to_handle_records_or_claims() -> None:
    """Planted violation: add a records or claims-handling offer.

    Deliberately out of scope. Patient records are special-category data under
    UK GDPR and claims handling is FCA territory; neither is a thing to
    promise in a cold opening email, and an offer is a promise. The design
    keeps these three verticals to the front desk.
    """
    banned = ("record", "claim", "diagnos", "triage", "prescri")
    for industry in (Industry.CLINIC, Industry.PRIVATE_HOSPITAL, Industry.INSURANCE):
        for offer in get_playbook(industry).offers:
            haystack = f"{offer.key} {offer.label} {offer.delivers}".lower()
            assert not any(word in haystack for word in banned), (
                f"{industry} offers {offer.key!r}, which promises regulated "
                "handling this design deliberately excludes"
            )


def test_every_new_offer_is_gated_on_evidence_like_every_other() -> None:
    """An offer with no required finding type could be pitched to anybody,
    which is the failure the whole evidence rule exists to prevent."""
    for industry in (Industry.CLINIC, Industry.PRIVATE_HOSPITAL, Industry.INSURANCE):
        for offer in get_playbook(industry).offers:
            assert offer.requires_finding_types, f"{industry}/{offer.key} is ungated"


def test_a_clinic_with_no_evidence_is_offered_nothing() -> None:
    """The gate holds for the new verticals exactly as for the old ones."""
    assert select_offers(Industry.CLINIC, set()) == []
