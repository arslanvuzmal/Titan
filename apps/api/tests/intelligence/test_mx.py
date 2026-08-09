"""MX verification tests.

Hermetic: the resolver is injected, so nothing touches DNS.

The asymmetry is what these defend. A negative result disqualifies; a positive
result must change nothing, because a domain accepting mail says nothing about
whether a particular mailbox exists.
"""

from __future__ import annotations

import pytest
from titan.db.enums import (
    ELIGIBLE_CONTACT_SOURCES,
    ContactSource,
    VerificationStatus,
)
from titan.intelligence.contacts import check_contact_eligibility
from titan.intelligence.mx import (
    DomainDoesNotExist,
    MxStatus,
    check_many,
    check_mx,
)


def resolver_returning(hosts: list[str], has_address: bool = True):
    def resolve(domain: str) -> tuple[list[str], bool]:
        return hosts, has_address

    return resolve


def resolver_raising(exc: Exception):
    def resolve(domain: str) -> tuple[list[str], bool]:
        raise exc

    return resolve


# ==========================================================================
# The four outcomes
# ==========================================================================
def test_a_domain_with_mx_records_can_receive_mail() -> None:
    check = check_mx("acme.test", resolver=resolver_returning(["mx1.acme.test"]))

    assert check.status is MxStatus.PRESENT
    assert check.can_receive_mail is True
    assert check.is_conclusively_undeliverable is False
    assert check.hosts == ("mx1.acme.test",)


def test_an_address_record_is_a_valid_implicit_destination() -> None:
    """RFC 5321 5.1. Treating this as undeliverable would discard legitimate
    small-business domains -- exactly the population Titan targets."""
    check = check_mx("small.test", resolver=resolver_returning([], has_address=True))

    assert check.status is MxStatus.IMPLICIT_A
    assert check.can_receive_mail is True
    assert check.is_conclusively_undeliverable is False


def test_a_domain_with_no_mail_route_is_undeliverable() -> None:
    check = check_mx("parked.test", resolver=resolver_returning([], has_address=False))

    assert check.status is MxStatus.ABSENT
    assert check.can_receive_mail is False
    assert check.is_conclusively_undeliverable is True


def test_a_nonexistent_domain_is_undeliverable() -> None:
    check = check_mx(
        "nope.test", resolver=resolver_raising(DomainDoesNotExist("nope.test"))
    )

    assert check.status is MxStatus.NXDOMAIN
    assert check.is_conclusively_undeliverable is True


# ==========================================================================
# A failed lookup is not evidence of anything
# ==========================================================================
def test_a_resolver_failure_never_disqualifies() -> None:
    """A resolver having a bad minute must not silently discard good leads.

    The failure would look identical to a real negative answer, so it is
    deliberately kept out of the disqualifying set.
    """
    check = check_mx("acme.test", resolver=resolver_raising(TimeoutError("no reply")))

    assert check.status is MxStatus.ERROR
    assert check.is_conclusively_undeliverable is False
    assert check.can_receive_mail is False


@pytest.mark.parametrize("bad", ["", "   ", "localhost", "not-a-domain"])
def test_a_malformed_domain_is_rejected_without_a_lookup(bad: str) -> None:
    def explode(domain: str) -> tuple[list[str], bool]:  # pragma: no cover
        raise AssertionError("should not have resolved")

    assert check_mx(bad, resolver=explode).status is MxStatus.NXDOMAIN


# ==========================================================================
# The asymmetry, at the eligibility gate
# ==========================================================================
def eligibility(mx=None):
    return check_contact_eligibility(
        source=ContactSource.FIRST_PARTY_WEBSITE,
        verification=VerificationStatus.PUBLISHED_FIRST_PARTY,
        is_active=True,
        allowed_sources=frozenset(ELIGIBLE_CONTACT_SOURCES),
        require_verified=True,
        email="hello@acme.test",
        mx=mx,
    )


def test_an_undeliverable_domain_blocks_the_contact() -> None:
    check = check_mx("acme.test", resolver=resolver_returning([], has_address=False))
    result = eligibility(mx=check)

    assert result.eligible is False
    assert any("cannot receive mail" in r for r in result.reasons)


def test_mx_presence_does_not_make_an_otherwise_ineligible_contact_eligible() -> None:
    """The rule mx_presence_is_not_verification() documents.

    A guessed address at a domain with perfect MX records is still a guess.
    """
    check = check_mx("acme.test", resolver=resolver_returning(["mx1.acme.test"]))
    result = check_contact_eligibility(
        source=ContactSource.PATTERN_GUESS,
        verification=VerificationStatus.PUBLISHED_FIRST_PARTY,
        is_active=True,
        allowed_sources=frozenset(ELIGIBLE_CONTACT_SOURCES),
        require_verified=True,
        email="ceo@acme.test",
        mx=check,
    )

    assert result.eligible is False
    assert any("pattern-guessed" in r for r in result.reasons)


def test_a_lookup_failure_leaves_eligibility_unchanged() -> None:
    failed = check_mx("acme.test", resolver=resolver_raising(OSError("dns down")))

    assert eligibility(mx=failed).eligible is True
    assert eligibility(mx=None).eligible is True


def test_omitting_the_mx_check_changes_nothing() -> None:
    """The parameter is optional; existing callers keep their behaviour."""
    assert eligibility().eligible is True


# ==========================================================================
# Batching and reporting
# ==========================================================================
def test_each_distinct_domain_is_resolved_once() -> None:
    calls: list[str] = []

    def counting(domain: str) -> tuple[list[str], bool]:
        calls.append(domain)
        return ["mx1"], True

    check_many(["a.test", "a.test", "B.TEST", "b.test "], resolver=counting)

    assert sorted(calls) == ["a.test", "b.test"]


def test_the_batch_reports_only_the_conclusive_negatives() -> None:
    def mixed(domain: str) -> tuple[list[str], bool]:
        if domain == "dead.test":
            return [], False
        if domain == "broken.test":
            raise TimeoutError("resolver down")
        return ["mx1"], True

    result = check_many(["good.test", "dead.test", "broken.test"], resolver=mixed)

    assert result.undeliverable_domains() == ("dead.test",)


def test_the_detail_record_states_what_mx_does_not_prove() -> None:
    """Written into the append-only ContactVerification row, so the caveat
    travels with the evidence rather than living only in a docstring."""
    detail = check_mx(
        "acme.test", resolver=resolver_returning(["mx1.acme.test"])
    ).as_verification_detail()

    assert detail["status"] == MxStatus.PRESENT.value
    assert "never upgrades verification_status" in str(detail["note"])
