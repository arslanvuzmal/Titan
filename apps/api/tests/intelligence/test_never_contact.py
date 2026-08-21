"""Addresses that are the wrong recipient, whoever published them.

Every one of these was found on a business's own website, is syntactically
perfect, and would pass every deliverability layer Titan has. What makes them
refusals is not that they bounce -- it is that nobody at the other end wanted
this message, and one of them is how a domain gets reported by the person whose
job is reporting things.
"""

from __future__ import annotations

import pytest
from titan.db.enums import ContactSource, VerificationStatus
from titan.intelligence.contacts import (
    NEVER_CONTACT_LOCAL_PARTS,
    check_contact_eligibility,
    normalize_email,
)


def refused(address: str) -> bool:
    return normalize_email(address).partition("@")[0] in NEVER_CONTACT_LOCAL_PARTS


# ------------------------------------------------------ hiring is not a lead


@pytest.mark.parametrize(
    "address",
    [
        "recruitment@zenlaw.co.uk",
        "recruiting@example.test",
        "careers@example.test",
        "jobs@example.test",
        "vacancies@example.test",
        "hr@example.test",
        "applications@example.test",
        "cv@example.test",
    ],
)
def test_hiring_addresses_are_never_contacted(address: str) -> None:
    """A "who is this" judgement, not a deliverability one.

    These exist for job applicants. They are read by whoever handles hiring,
    are often pointed at an applicant-tracking system that accepts nothing
    else, and are as often abandoned between vacancies. A pitch about a broken
    booking page is the wrong message to the wrong person however well written.

    ``recruitment@zenlaw.co.uk`` was one of five bounces on the live
    workspace -- published on the firm's own contact page, syntactically
    perfect, and refused by every layer that existed at the time, which was
    none of them.
    """
    assert refused(address)


def test_real_recipients_whose_names_contain_those_letters_survive() -> None:
    """Planted violation: match on substrings rather than the whole local part
    and Chris, Hrafn and McVeigh all stop receiving mail."""
    for address in (
        "chris@example.test",
        "hrafn@example.test",
        "mcveigh@example.test",
        "jobsworth.dental@example.test",
        "careerscoach@example.test",
    ):
        assert not refused(address), address


# --------------------------------------------- the categories that came before


@pytest.mark.parametrize(
    ("address", "why"),
    [
        ("abuse@example.test", "monitored by the people who report senders"),
        ("postmaster@example.test", "reserved by RFC 2142 for running mail"),
        ("noreply@example.test", "nobody reads it"),
        ("remove@example.test", "a standing request not to be contacted"),
        ("unsubscribe@example.test", "the same request, spelled differently"),
        ("dpo@example.test", "exists to receive complaints, not enquiries"),
    ],
)
def test_the_existing_categories_still_hold(address: str, why: str) -> None:
    assert refused(address), why


# ------------------------------------------------------ the gate says it plainly


def test_the_refusal_names_the_address_rather_than_calling_it_a_system_mailbox() -> None:
    """``recruitment@`` is not a system mailbox, and a reason that says so
    would send somebody looking for a mail-server problem that is not there."""
    result = check_contact_eligibility(
        email="recruitment@zenlaw.co.uk",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        allowed_sources=frozenset({ContactSource.FIRST_PARTY_WEBSITE}),
        is_active=True,
        verification=VerificationStatus.UNVERIFIED,
        require_verified=False,
    )

    assert not result.eligible
    assert any("never an outreach target" in reason for reason in result.reasons)
    assert not any("system mailbox" in reason for reason in result.reasons)
