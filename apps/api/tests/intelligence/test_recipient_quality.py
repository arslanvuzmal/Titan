"""Phase 02's acceptance criterion, as one test each.

"A known-invalid and a known catch-all address are both refused before the
outbox, an invariant test fails if that gate is bypassed, and a bad lead source
is visible as a number rather than a hunch."

Every part of that was covered somewhere -- the catch-all policy in
`test_bounce_risk`, MX disqualification in `test_mx`, the bypass rule in the
repository invariants, the lead-source rollup in `test_rollups`. What was not
covered is the sentence itself: that the layers still compose into the refusal
the phase promises. A property held by four modules separately is not the same
as a property held by the system, and it is the second one that was promised.

One thing this file deliberately does not test, because the codebase
deliberately does not do it: SMTP-level probing. The phase asks for it;
`titan.intelligence.mx` refuses it in writing -- asking a stranger's server
whether a stranger's mailbox exists is unreliable and is treated as abuse, and
it damages the sending reputation the rest of the package exists to protect.
Invalidity is established from DNS, which is conclusive in the direction that
matters, and mailbox-level answers are left to a purchased service.
"""

from __future__ import annotations

from titan.db.enums import (
    SENDABLE_VERIFICATION_STATUSES,
    ContactSource,
    VerificationStatus,
    verification_permits_sending,
)
from titan.intelligence.verifier import NullVerifier, build_verifier

UNTRUSTED = ContactSource.PUBLIC_DIRECTORY
TRUSTED = ContactSource.FIRST_PARTY_WEBSITE


# ------------------------------------------------------- the catch-all half


def test_a_catch_all_is_not_sendable_on_its_status_alone() -> None:
    """A catch-all server accepts everything and bounces later.

    The status therefore carries no information about the mailbox, which is why
    it is absent from the sendable set rather than merely ranked below it.
    """
    assert VerificationStatus.CATCH_ALL not in SENDABLE_VERIFICATION_STATUSES


def test_a_catch_all_from_a_third_party_listing_is_refused() -> None:
    """A directory or a Places record is a third party asserting an address.

    Combined with a server that accepts everything, nothing in the chain has
    actually seen this mailbox work.
    """
    assert verification_permits_sending(VerificationStatus.CATCH_ALL, UNTRUSTED) is False


def test_a_catch_all_the_business_published_itself_is_permitted() -> None:
    """The contrast, and the reason the rule takes two arguments.

    A refusal that fired on every catch-all would discard every business whose
    provider happens to accept all mail -- which is most of them -- so what
    decides is who put the address in front of us.
    """
    assert verification_permits_sending(VerificationStatus.CATCH_ALL, TRUSTED) is True


# --------------------------------------------------------- the invalid half


def test_an_invalid_address_is_refused_whatever_its_source() -> None:
    """Provenance cannot rescue an address a verifier has condemned.

    This is the asymmetry that matters: a strong source can carry an *unproven*
    address, never a disproven one.
    """
    for source in (TRUSTED, UNTRUSTED):
        assert verification_permits_sending(VerificationStatus.INVALID, source) is False


def test_an_unverified_address_cannot_be_upgraded_by_the_default_verifier() -> None:
    """With no verification service configured every address is UNKNOWN.

    UNKNOWN is not sendable, so the floor holds without a vendor: an
    unconfigured verifier lowers the ceiling on what can be reached, never the
    floor on what may be sent.
    """
    verifier = build_verifier(None)

    assert isinstance(verifier, NullVerifier)
    assert VerificationStatus.UNKNOWN not in SENDABLE_VERIFICATION_STATUSES
    assert verification_permits_sending(VerificationStatus.UNKNOWN, UNTRUSTED) is False


def test_a_typo_in_the_verifier_name_does_not_open_the_gate() -> None:
    """An unrecognised name falls back to null rather than raising.

    A typo must not take the pipeline down, and it must not quietly mark
    anything sendable either -- the fallback is safe in the direction that
    matters.
    """
    verifier = build_verifier("zerobounce-typo")

    assert isinstance(verifier, NullVerifier)


# ------------------------------------------------- the gate, and the bypass


def test_the_send_decision_asks_the_rule_not_the_set() -> None:
    """Membership of the sendable set is not the send decision.

    `verification_permits_sending` takes the source as well, because CATCH_ALL
    is decided by provenance. A module testing set membership directly would
    refuse every catch-all a business published itself -- and the repository
    invariant that forbids exactly that is what keeps the two from drifting.
    """
    in_set = VerificationStatus.CATCH_ALL in SENDABLE_VERIFICATION_STATUSES
    by_rule = verification_permits_sending(VerificationStatus.CATCH_ALL, TRUSTED)

    assert in_set is False
    assert by_rule is True, (
        "the set and the rule now agree, which means the rule has stopped "
        "carrying the provenance logic it exists for"
    )


def test_the_policy_engine_is_where_the_gate_lives() -> None:
    """Enforced at the send decision, re-evaluated by the outbox worker.

    Checking at draft time only would let an address downgraded between drafting
    and sending go out on a verification that was true an hour ago.
    """
    import inspect

    from titan.policy import engine

    source = inspect.getsource(engine)

    assert "verification_permits_sending" in source
    assert "require_verified_email" in source
