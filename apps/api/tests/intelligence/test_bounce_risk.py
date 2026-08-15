"""Bounce reduction engine tests.

Hermetic: no DNS, no network, no verification vendor. Every input the engine
reasons about is constructed here, which is the point of keeping it pure.

Most of these are refusals. The engine's job is to keep a message out of the
outbox, so the interesting assertions are the ones that prove something did
*not* become sendable -- and, just as important, the false-positive controls
that prove a legitimate address still is.
"""

from __future__ import annotations

import pytest
from titan.db.enums import (
    SENDABLE_VERIFICATION_STATUSES,
    ContactSource,
    VerificationStatus,
    verification_permits_sending,
)
from titan.intelligence.bounce_risk import Verdict, assess
from titan.intelligence.mx import MxCheck, MxStatus
from titan.intelligence.recipient_domains import (
    FREE_MAILBOX_PROVIDERS,
    TYPO_REFERENCE_DOMAINS,
    is_disposable,
    is_free_mailbox_provider,
    is_one_edit_apart,
    typo_of,
)
from titan.intelligence.verifier import (
    DeterministicVerifier,
    MailboxVerifier,
    NullVerifier,
    VerificationResult,
    build_verifier,
)

MX_OK = MxCheck(MxStatus.PRESENT, "harborline-legal.test", hosts=("mx1.harborline.test",))
MX_DEAD = MxCheck(MxStatus.ABSENT, "harborline-legal.test", detail="no route")
MX_NXDOMAIN = MxCheck(MxStatus.NXDOMAIN, "gone.test", detail="NXDOMAIN")
MX_ERROR = MxCheck(MxStatus.ERROR, "harborline-legal.test", detail="timeout")


def codes(risk) -> set[str]:
    return {s.code for s in risk.signals}


# ==========================================================================
# Layer 2: disposable domains
# ==========================================================================
def test_a_disposable_domain_is_recognised() -> None:
    assert is_disposable("mailinator.com") is True
    assert is_disposable("Guerrillamail.COM") is True
    assert is_disposable("harborline-legal.test") is False


def test_a_disposable_address_is_refused_outright() -> None:
    """Deliverable in the narrow sense, and worthless. Usually a defect in our
    own crawl rather than a contact anybody meant to publish."""
    risk = assess(
        email="info@mailinator.com",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
    )

    assert risk.status is VerificationStatus.INVALID
    assert risk.permits_sending is False
    assert "disposable_domain" in codes(risk)


# ==========================================================================
# Layer 3: lookalike domains
# ==========================================================================
@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("gmial.com", "gmail.com"),  # transposition
        ("gmai.com", "gmail.com"),  # deletion
        ("gmaill.com", "gmail.com"),  # insertion
        ("gmzil.com", "gmail.com"),  # substitution
        ("hotmial.com", "hotmail.com"),
        ("yahooo.com", "yahoo.com"),
        ("outlok.com", "outlook.com"),
    ],
)
def test_common_webmail_misspellings_are_caught(candidate: str, expected: str) -> None:
    assert typo_of(candidate) == expected


@pytest.mark.parametrize(
    "domain",
    [
        "gmail.com",  # the real thing
        "mail.com",  # a real provider one insertion from gmail.com
        "harborline-legal.test",  # an ordinary business domain
        "bellrose-dental.co.uk",
        "",
    ],
)
def test_legitimate_domains_are_not_called_misspellings(domain: str) -> None:
    assert typo_of(domain) is None


@pytest.mark.parametrize("domain", ["gm.com", "al.com", "we.de", "cox.net"])
def test_short_corporate_domains_are_not_flagged(domain: str) -> None:
    """The regression MIN_TYPO_REFERENCE_LENGTH exists for.

    ``gm.com`` is General Motors and is one deletion from ``gmx.com``;
    ``al.com`` is one deletion from ``aol.com``. Using short provider labels as
    typo references would refuse real corporate mail.
    """
    assert typo_of(domain) is None


def test_no_typo_reference_is_short_enough_to_collide() -> None:
    for reference in TYPO_REFERENCE_DOMAINS:
        assert len(reference.split(".", 1)[0]) >= 5, reference


def test_a_lookalike_downgrades_but_does_not_refuse() -> None:
    """Edit distance is a heuristic, not evidence, so it leaves room for a
    verification service to overrule it."""
    risk = assess(
        email="info@gmial.com", source=ContactSource.FIRST_PARTY_WEBSITE, mx=MX_OK
    )

    assert risk.status is VerificationStatus.RISKY
    assert risk.permits_sending is False
    assert risk.refusals == ()
    assert "lookalike_domain" in codes(risk)


# ==========================================================================
# is_one_edit_apart
# ==========================================================================
@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("abc", "abc", False),  # identical is zero edits, not one
        ("abc", "abd", True),  # substitution
        ("abc", "acb", True),  # adjacent transposition
        ("abc", "abcd", True),  # insertion at the end
        ("abc", "zabc", True),  # insertion at the front
        ("abcd", "abc", True),  # deletion
        ("abc", "acbd", False),  # two edits
        ("abc", "xyz", False),
        ("abc", "abcde", False),  # length gap of two
        ("", "a", True),
        ("abc", "bac", True),  # transposition at the front
        ("abcd", "abdc", True),  # transposition at the end
        ("ab", "ba", True),
        ("abcd", "badc", False),  # two transpositions
    ],
)
def test_one_edit_apart(a: str, b: str, expected: bool) -> None:
    assert is_one_edit_apart(a, b) is expected
    assert is_one_edit_apart(b, a) is expected, "the relation must be symmetric"


# ==========================================================================
# Layer 4: MX
# ==========================================================================
@pytest.mark.parametrize("mx", [MX_DEAD, MX_NXDOMAIN])
def test_a_domain_that_cannot_receive_mail_is_refused(mx: MxCheck) -> None:
    risk = assess(
        email="info@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=mx,
    )

    assert risk.status is VerificationStatus.INVALID
    assert "domain_cannot_receive_mail" in codes(risk)


def test_a_failed_mx_lookup_is_never_a_disqualifier() -> None:
    """A resolver having a bad minute must not silently discard good leads."""
    risk = assess(
        email="info@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_ERROR,
    )

    assert risk.status is VerificationStatus.PUBLISHED_FIRST_PARTY
    assert risk.permits_sending is True


def test_mx_presence_alone_does_not_confirm_a_mailbox() -> None:
    """The rule mx.py documents: a positive MX result never reaches
    PROVIDER_VERIFIED, because DNS cannot see a mailbox."""
    risk = assess(
        email="info@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
    )

    assert risk.status is not VerificationStatus.PROVIDER_VERIFIED
    assert risk.status is VerificationStatus.PUBLISHED_FIRST_PARTY


# ==========================================================================
# Layer 1: syntax, and short-circuiting
# ==========================================================================
@pytest.mark.parametrize(
    "email",
    [
        "%20csteam@ruhdental.com",  # an undecoded mailto: URI, seen in a real send
        "not-an-address",
        "two@@ats.com",
        "trailing.dot.@acme.test",
        "",
    ],
)
def test_a_malformed_address_is_invalid(email: str) -> None:
    risk = assess(email=email, source=ContactSource.FIRST_PARTY_WEBSITE, mx=MX_OK)

    assert risk.status is VerificationStatus.INVALID
    assert "malformed_address" in codes(risk)


def test_syntax_failure_stops_before_the_expensive_layers() -> None:
    """Nothing below layer 1 can mean anything about a string that is not an
    address, and a verification service bills per lookup."""
    risk = assess(
        email="%20info@mailinator.com",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_DEAD,
        verification=VerificationResult(
            status=VerificationStatus.PROVIDER_VERIFIED, provider="test"
        ),
    )

    assert codes(risk) == {"malformed_address"}


# ==========================================================================
# Layer 5: mailbox verification
# ==========================================================================
def test_a_confirmed_mailbox_reaches_provider_verified() -> None:
    risk = assess(
        email="info@harborline-legal.test",
        source=ContactSource.GOOGLE_PLACES,
        mx=MX_OK,
        verification=VerificationResult(
            status=VerificationStatus.PROVIDER_VERIFIED, provider="test"
        ),
    )

    assert risk.status is VerificationStatus.PROVIDER_VERIFIED
    assert risk.permits_sending is True
    assert "mailbox_confirmed" in codes(risk)


def test_a_nonexistent_mailbox_is_invalid() -> None:
    risk = assess(
        email="nobody@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
        verification=VerificationResult(
            status=VerificationStatus.INVALID, provider="test", detail="550 no such user"
        ),
    )

    assert risk.status is VerificationStatus.INVALID
    assert "mailbox_does_not_exist" in codes(risk)


def test_a_catch_all_domain_is_reported_as_catch_all() -> None:
    risk = assess(
        email="info@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
        verification=VerificationResult(
            status=VerificationStatus.CATCH_ALL, provider="test", is_catch_all=True
        ),
    )

    assert risk.status is VerificationStatus.CATCH_ALL
    assert "catch_all_domain" in codes(risk)


def test_catch_all_reported_only_as_a_flag_is_still_catch_all() -> None:
    """A service may answer "accepted" and separately note that the domain
    accepts everything. The second fact is what makes the first meaningless."""
    risk = assess(
        email="info@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
        verification=VerificationResult(
            status=VerificationStatus.PROVIDER_VERIFIED,
            provider="test",
            is_catch_all=True,
        ),
    )

    assert risk.status is VerificationStatus.CATCH_ALL


def test_an_unknown_verification_result_changes_nothing() -> None:
    """UNKNOWN is what a service returns when it was not asked, could not
    answer, or timed out. Treating it as settled turns an outage into a verdict."""
    risk = assess(
        email="info@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
        verification=VerificationResult(
            status=VerificationStatus.UNKNOWN, provider="null"
        ),
    )

    assert risk.status is VerificationStatus.PUBLISHED_FIRST_PARTY
    assert codes(risk) == set()


# ==========================================================================
# Precedence: negative evidence outranks positive
# ==========================================================================
def test_a_confirmed_mailbox_does_not_rescue_a_lookalike_domain() -> None:
    """The precedence rule that stops the engine being a scoring function.

    A verification service confirming that info@gmial.com accepts mail does not
    make gmial.com less of a misspelling -- it confirms that the squatter's
    catch-all is working.
    """
    risk = assess(
        email="info@gmial.com",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
        verification=VerificationResult(
            status=VerificationStatus.PROVIDER_VERIFIED, provider="test"
        ),
    )

    assert risk.status is VerificationStatus.RISKY
    assert risk.permits_sending is False
    assert {"lookalike_domain", "mailbox_confirmed"} <= codes(risk)


def test_a_conclusive_refusal_outranks_a_catch_all_report() -> None:
    risk = assess(
        email="info@mailinator.com",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
        verification=VerificationResult(
            status=VerificationStatus.CATCH_ALL, provider="test", is_catch_all=True
        ),
    )

    assert risk.status is VerificationStatus.INVALID


def test_a_dead_domain_outranks_a_confirmed_mailbox() -> None:
    risk = assess(
        email="info@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_DEAD,
        verification=VerificationResult(
            status=VerificationStatus.PROVIDER_VERIFIED, provider="test"
        ),
    )

    assert risk.status is VerificationStatus.INVALID


# ==========================================================================
# Free providers are context, not a defect
# ==========================================================================
def test_a_free_provider_address_is_noted_and_still_sendable() -> None:
    """A great many small businesses run on Gmail. Refusing them would discard
    the population Titan targets."""
    risk = assess(
        email="bellrosedental@gmail.com",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
    )

    assert risk.status is VerificationStatus.PUBLISHED_FIRST_PARTY
    assert risk.permits_sending is True
    assert "free_mailbox_provider" in codes(risk)
    assert [s.verdict for s in risk.signals] == [Verdict.NOTE]


def test_every_free_provider_classifies_as_one() -> None:
    for domain in FREE_MAILBOX_PROVIDERS:
        assert is_free_mailbox_provider(domain) is True


# ==========================================================================
# Provenance without evidence
# ==========================================================================
def test_provenance_is_the_floor_for_a_first_party_address() -> None:
    risk = assess(
        email="enquiries@harborline-legal.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
    )

    assert risk.status is VerificationStatus.PUBLISHED_FIRST_PARTY
    assert risk.permits_sending is True


def test_a_weaker_provenance_with_no_evidence_stays_unknown() -> None:
    """A directory listing is a third party asserting an address. With nothing
    corroborating it, UNKNOWN is the honest answer, and UNKNOWN does not send."""
    risk = assess(
        email="enquiries@harborline-legal.test",
        source=ContactSource.PUBLIC_DIRECTORY,
    )

    assert risk.status is VerificationStatus.UNKNOWN
    assert risk.permits_sending is False


# ==========================================================================
# The sendability rule
# ==========================================================================
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (ContactSource.FIRST_PARTY_WEBSITE, True),
        (ContactSource.MANUAL_ENTRY, True),
        (ContactSource.EXISTING_CRM_RELATIONSHIP, True),
        (ContactSource.GOOGLE_PLACES, False),
        (ContactSource.PUBLIC_DIRECTORY, False),
        (ContactSource.PUBLIC_ROLE_ADDRESS, False),
        (ContactSource.VERIFIED_ENRICHMENT, False),
        (ContactSource.PATTERN_GUESS, False),
    ],
)
def test_catch_all_sends_only_behind_direct_provenance(
    source: ContactSource, expected: bool
) -> None:
    """Catch-all is the default on most small-business hosting, so the mail
    server tells us nothing. Who put the address in front of us does."""
    assert verification_permits_sending(VerificationStatus.CATCH_ALL, source) is expected


@pytest.mark.parametrize(
    "status",
    [
        VerificationStatus.UNVERIFIED,
        VerificationStatus.RISKY,
        VerificationStatus.INVALID,
        VerificationStatus.UNKNOWN,
    ],
)
def test_no_provenance_rescues_a_non_sendable_status(
    status: VerificationStatus,
) -> None:
    for source in ContactSource:
        assert verification_permits_sending(status, source) is False


@pytest.mark.parametrize("status", sorted(SENDABLE_VERIFICATION_STATUSES))
def test_a_sendable_status_sends_from_any_provenance(
    status: VerificationStatus,
) -> None:
    """Provenance is enforced separately, by ELIGIBLE_CONTACT_SOURCES and the
    policy engine. This rule is only about what verification established, and
    conflating the two would put the guessed-address refusal in two places."""
    for source in ContactSource:
        assert verification_permits_sending(status, source) is True


def test_every_status_is_decided_one_way_or_the_other() -> None:
    """Exhaustiveness. A status added without a rule here would fall through to
    the default, and the default is 'does not send' -- safe, but silent."""
    for status in VerificationStatus:
        for source in ContactSource:
            assert isinstance(verification_permits_sending(status, source), bool)


# ==========================================================================
# The verifier port
# ==========================================================================
async def test_the_null_verifier_asserts_nothing() -> None:
    verifier = NullVerifier()
    result = await verifier.verify("info@harborline-legal.test")

    assert result.status is VerificationStatus.UNKNOWN
    assert result.is_conclusive is False
    assert (
        verification_permits_sending(result.status, ContactSource.MANUAL_ENTRY) is False
    )


async def test_the_deterministic_verifier_is_stable_for_one_address() -> None:
    verifier = DeterministicVerifier()
    first = await verifier.verify("info@harborline-legal.test")
    second = await verifier.verify("INFO@Harborline-Legal.test  ")

    assert first.status is second.status


async def test_the_deterministic_verifier_reaches_every_branch() -> None:
    """A fake that only ever returned one verdict would exercise one branch of
    the engine and quietly make the rest untested."""
    verifier = DeterministicVerifier()
    seen = {
        (await verifier.verify(f"contact{i}@harborline-legal.test")).status
        for i in range(200)
    }

    assert seen == {
        VerificationStatus.PROVIDER_VERIFIED,
        VerificationStatus.CATCH_ALL,
        VerificationStatus.RISKY,
        VerificationStatus.INVALID,
    }


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("null", NullVerifier),
        ("deterministic", DeterministicVerifier),
        ("DETERMINISTIC", DeterministicVerifier),
        ("a-vendor-nobody-installed", NullVerifier),
        (None, NullVerifier),
        ("", NullVerifier),
    ],
)
def test_build_verifier_falls_back_to_null(name: str | None, expected: type) -> None:
    """A typo in configuration must not take discovery down, and the fallback is
    safe in the direction that matters: null can never mark an address sendable."""
    assert isinstance(build_verifier(name), expected)


def test_both_adapters_satisfy_the_protocol() -> None:
    assert isinstance(NullVerifier(), MailboxVerifier)
    assert isinstance(DeterministicVerifier(), MailboxVerifier)


# ==========================================================================
# The audit record
# ==========================================================================
def test_the_assessment_serialises_every_signal() -> None:
    """ContactVerification.detail is how a past refusal gets explained months
    later, so nothing the engine reasoned about may be dropped on the way in."""
    risk = assess(
        email="info@gmial.com",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        mx=MX_OK,
        verification=VerificationResult(
            status=VerificationStatus.PROVIDER_VERIFIED, provider="test"
        ),
    )
    detail = risk.as_verification_detail()

    assert detail["check"] == "bounce_risk"
    assert detail["status"] == VerificationStatus.RISKY.value
    assert detail["source"] == ContactSource.FIRST_PARTY_WEBSITE.value
    assert detail["permits_sending"] is False
    recorded = {s["code"] for s in detail["signals"]}  # type: ignore[index,union-attr]
    assert recorded == codes(risk)
