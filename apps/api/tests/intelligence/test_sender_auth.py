"""Sender domain authentication, checked against DNS rather than asserted.

The defect these exist for was found in production: twenty sender identities
with domain_verified, spf_ok, dkim_ok and dmarc_ok all true, on a domain with
no DNS at all. Every one passed the delivery gate.
"""

from __future__ import annotations

import datetime as dt

import pytest
from titan.intelligence.mx import DomainDoesNotExist
from titan.intelligence.sender_auth import (
    MAX_VERIFICATION_AGE,
    check_domain_auth,
    is_stale,
)

GOOD_SPF = "v=spf1 include:spf.spacemail.com ~all"
GOOD_DMARC = "v=DMARC1; p=quarantine; rua=mailto:dmarc@example.test"
DKIM_KEY = "v=DKIM1; k=rsa; p=" + "A" * 40


def resolvers(
    *,
    exists: bool = True,
    spf: list[str] | None = None,
    dmarc: list[str] | None = None,
    dkim_at: str | None = "default",
):
    """A fake DNS pair. Nothing here touches the network."""

    def mx(domain: str) -> tuple[list[str], bool]:
        if not exists:
            raise DomainDoesNotExist(domain)
        return ["mx1.example.test"], True

    def txt(name: str) -> list[str]:
        if name.startswith("_dmarc."):
            return dmarc if dmarc is not None else [GOOD_DMARC]
        if "._domainkey." in name:
            selector = name.split("._domainkey.")[0]
            return [DKIM_KEY] if dkim_at and selector == dkim_at else []
        return spf if spf is not None else [GOOD_SPF]

    return txt, mx


def check(**kwargs):
    txt, mx = resolvers(**kwargs)
    return check_domain_auth("example.test", txt_resolver=txt, mx_resolver=mx)


# ==========================================================================
# The case that was live in production
# ==========================================================================
def test_a_domain_that_does_not_exist_is_never_authenticated() -> None:
    """mail.arslanvuzmallone.dev, with all four flags true in the database."""
    result = check(exists=False)

    assert result.resolved is False
    assert result.spf_ok is False
    assert result.dmarc_ok is False
    assert result.dkim_ok is False
    assert not result.sendable
    assert "does not exist" in " ".join(result.notes)


def test_a_nonexistent_domain_reports_that_rather_than_missing_spf() -> None:
    """Reporting 'no SPF' for a domain that is not registered buries the point."""
    result = check(exists=False)

    joined = " ".join(result.notes)
    assert "SPF" not in joined
    assert result.dkim_conclusive is True


# ==========================================================================
# SPF
# ==========================================================================
def test_a_good_domain_is_sendable() -> None:
    result = check()

    assert result.sendable
    assert result.spf_record == GOOD_SPF
    assert result.dmarc_policy == "quarantine"


def test_a_domain_with_no_spf_is_not_sendable() -> None:
    result = check(spf=[])

    assert result.spf_ok is False
    assert not result.sendable
    assert "no SPF record" in " ".join(result.notes)


def test_two_spf_records_are_treated_as_none() -> None:
    """RFC 7208: more than one is a permanent error, and receivers ignore both."""
    result = check(spf=[GOOD_SPF, "v=spf1 include:other.test ~all"])

    assert result.spf_ok is False


def test_an_spf_ending_in_all_is_refused() -> None:
    """+all authorises the entire internet to send as this domain.

    Worse than publishing nothing: it looks like authentication and defeats it.
    """
    result = check(spf=["v=spf1 include:spf.example.test +all"])

    assert result.spf_ok is False
    assert "+all" in " ".join(result.notes)


def test_unrelated_txt_records_do_not_count_as_spf() -> None:
    result = check(spf=["google-site-verification=abc123", "MS=ms12345"])

    assert result.spf_ok is False


# ==========================================================================
# DMARC
# ==========================================================================
def test_a_domain_with_no_dmarc_is_not_sendable() -> None:
    result = check(dmarc=[])

    assert result.dmarc_ok is False
    assert not result.sendable


def test_dmarc_without_a_policy_does_not_count() -> None:
    result = check(dmarc=["v=DMARC1; rua=mailto:x@example.test"])

    assert result.dmarc_ok is False


def test_p_none_satisfies_dmarc_but_is_reported() -> None:
    """Gmail's bulk-sender rule asks for a record, not an enforcing one."""
    result = check(dmarc=["v=DMARC1; p=none"])

    assert result.dmarc_ok is True
    assert result.dmarc_policy == "none"
    assert "does not enforce" in " ".join(result.notes)


# ==========================================================================
# DKIM -- the honest edge case
# ==========================================================================
def test_a_found_dkim_key_is_conclusive() -> None:
    result = check(dkim_at="default")

    assert result.dkim_ok is True
    assert result.dkim_conclusive is True
    assert result.dkim_selector == "default"


def test_a_missing_dkim_key_is_not_proof_that_dkim_is_absent() -> None:
    """DNS cannot enumerate selectors, so a miss means 'unknown', not 'no'."""
    result = check(dkim_at=None)

    assert result.dkim_ok is False
    assert result.dkim_conclusive is False
    assert "not proof" in " ".join(result.notes)


def test_an_unknown_dkim_selector_fails_closed() -> None:
    """The alternative is sending unauthenticated mail believing it landed.

    A false refusal costs one line added to COMMON_DKIM_SELECTORS. Sending
    without DKIM costs a spam folder and a domain reputation, silently, because
    Gmail's bulk-sender rules require it.
    """
    result = check(dkim_at="some-vendor-specific-selector")

    assert result.dkim_ok is False
    assert result.dkim_conclusive is False
    assert result.sendable is False


def test_sendable_agrees_with_the_identity_gate() -> None:
    """Both require all four. Two gates disagreeing is how a flag passes one."""
    assert check().sendable is True
    assert check(dkim_at=None).sendable is False
    assert check(spf=[]).sendable is False
    assert check(dmarc=[]).sendable is False


def test_a_non_dkim_txt_at_the_selector_is_not_a_key() -> None:
    txt, mx = resolvers()

    def wrong(name: str) -> list[str]:
        if "._domainkey." in name:
            return ["this is not a dkim key"]
        return txt(name)

    result = check_domain_auth("example.test", txt_resolver=wrong, mx_resolver=mx)

    assert result.dkim_ok is False


# ==========================================================================
# Inconclusive results never upgrade a flag
# ==========================================================================
def test_a_resolver_outage_never_reports_a_domain_as_authenticated() -> None:
    def broken(domain: str) -> tuple[list[str], bool]:
        raise TimeoutError("resolver unreachable")

    txt, _ = resolvers()
    result = check_domain_auth("example.test", txt_resolver=txt, mx_resolver=broken)

    assert result.resolved is False
    assert not result.sendable
    assert result.dkim_conclusive is False
    assert "resolver error" in " ".join(result.notes)


@pytest.mark.parametrize("domain", ["", "   ", "localhost", "not-a-domain"])
def test_a_malformed_domain_is_never_sendable(domain: str) -> None:
    txt, mx = resolvers()

    result = check_domain_auth(domain, txt_resolver=txt, mx_resolver=mx)

    assert not result.sendable


def test_the_evidence_is_recorded_for_every_claim() -> None:
    detail = check().as_detail()

    assert detail["spf_record"] == GOOD_SPF
    assert detail["dmarc_record"] == GOOD_DMARC
    assert detail["dkim_selector"] == "default"
    assert detail["checked_at"]


# ==========================================================================
# Staleness -- what turns an assertion back into a claim that expires
# ==========================================================================
def test_a_verification_that_never_happened_is_stale() -> None:
    """The important case: it is what a hand-set flag looks like."""
    assert is_stale(None) is True


def test_a_recent_verification_is_not_stale() -> None:
    assert is_stale(dt.datetime.now(dt.UTC)) is False


def test_a_verification_older_than_the_window_is_stale() -> None:
    old = dt.datetime.now(dt.UTC) - MAX_VERIFICATION_AGE - dt.timedelta(hours=1)

    assert is_stale(old) is True


def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing() -> None:
    """Postgres can hand back a naive datetime; comparing it must not raise."""
    naive = dt.datetime.now(dt.UTC).replace(tzinfo=None)

    assert is_stale(naive) is False
