"""Re-checking addresses stored before there was a verifier.

The unit tests are hermetic. The two that touch the database skip when it is
not reachable -- the same rule the rest of the suite follows.

What is being pinned here is the direction of travel. A catch-up pass that can
*raise* a status on a vendor's say-so is a pass that can talk a bad address
into being sendable; one that writes the verifier's answer straight onto the
channel can throw away a first-party address a human read off a contact page.
Neither is possible if resolution stays inside ``assess``, and that is what
these assert.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from titan.db.enums import (
    SENDABLE_VERIFICATION_STATUSES,
    ContactSource,
    VerificationStatus,
)
from titan.intelligence.reverification import (
    RECHECKABLE,
    Candidate,
    ReverifyReport,
    reverify,
)
from titan.intelligence.verifier import VerificationResult


class ScriptedVerifier:
    """Answers from a table, or raises. No network, no vendor."""

    name = "scripted"

    def __init__(self, answers: dict[str, VerificationResult], *, raises: bool = False):
        self._answers = answers
        self._raises = raises
        self.asked: list[str] = []

    async def verify(self, email: str) -> VerificationResult:
        self.asked.append(email)
        if self._raises:
            raise RuntimeError("verification service unreachable")
        return self._answers.get(
            email,
            VerificationResult(status=VerificationStatus.UNKNOWN, provider=self.name),
        )

    async def health_check(self) -> tuple[bool, str]:
        return True, "scripted"


def catch_all() -> VerificationResult:
    return VerificationResult(
        status=VerificationStatus.CATCH_ALL,
        provider="scripted",
        is_catch_all=True,
        detail="domain accepts every local part",
    )


def invalid() -> VerificationResult:
    return VerificationResult(status=VerificationStatus.INVALID, provider="scripted")


def deliverable_domain(domain: str) -> tuple[list[str], bool]:
    """A resolver that says the domain can receive mail.

    Without it every `.test` address is NXDOMAIN, the MX layer settles the
    question on its own, and the verifier's answer is never reached -- which
    is correct behaviour and useless for testing what this module does.
    """
    return [f"mx.{domain}"], True


# ------------------------------------------------------------------ selection
def test_a_conclusive_answer_is_not_re_asked() -> None:
    """Re-probing a domain that already gave a definite answer costs somebody
    else a connection and teaches us nothing."""
    assert VerificationStatus.PROVIDER_VERIFIED not in RECHECKABLE
    assert VerificationStatus.CATCH_ALL not in RECHECKABLE
    assert VerificationStatus.INVALID not in RECHECKABLE


def test_a_full_mailbox_is_left_alone() -> None:
    """RISKY usually means a real person whose mailbox is full. Re-checking it
    every run probes the same tired server daily for an answer that changes on
    its own."""
    assert VerificationStatus.RISKY not in RECHECKABLE


def test_an_address_sendable_only_on_provenance_is_re_asked() -> None:
    """The 611. PUBLISHED_FIRST_PARTY means a human published it, not that
    anybody checked it -- which is exactly the population worth checking."""
    assert VerificationStatus.PUBLISHED_FIRST_PARTY in RECHECKABLE
    assert VerificationStatus.UNVERIFIED in RECHECKABLE
    assert VerificationStatus.UNKNOWN in RECHECKABLE


# ------------------------------------------------------------------ reporting
def test_the_report_counts_what_it_learned() -> None:
    report = ReverifyReport()
    report.record(VerificationStatus.INVALID)
    report.record(VerificationStatus.INVALID)
    report.record(VerificationStatus.CATCH_ALL)

    assert report.outcomes == {"invalid": 2, "catch_all": 1}


# ------------------------------------------------------------------- database
@pytest.fixture
async def channel(db_session, workspace):
    """One first-party address, sendable on provenance and never checked."""
    from titan.db.models import Contact, ContactChannel, Organization

    tag = uuid.uuid4().hex[:8]
    org = Organization(
        workspace_id=workspace,
        display_name=f"{tag} Dental",
        normalized_name=f"{tag} dental",
        canonical_domain=f"{tag}.test",
    )
    db_session.add(org)
    await db_session.flush()
    contact = Contact(
        workspace_id=workspace, organization_id=org.id, full_name="Practice Manager"
    )
    db_session.add(contact)
    await db_session.flush()

    address = f"hello@{tag}.test"
    row = ContactChannel(
        workspace_id=workspace,
        contact_id=contact.id,
        channel_type="email",
        value=address,
        normalized_value=address,
        value_domain=f"{tag}.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        source_url=f"https://{tag}.test/contact",
        discovered_at=dt.datetime.now(dt.UTC),
        verification_status=VerificationStatus.PUBLISHED_FIRST_PARTY,
        confidence=0.9,
        is_active=True,
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def test_a_dry_run_records_nothing(db_session, workspace, channel) -> None:
    """The whole point of a dry run on 611 addresses somebody is about to mail."""
    from sqlalchemy import func, select
    from titan.db.models import ContactChannel, ContactVerification

    verifier = ScriptedVerifier({channel.normalized_value: invalid()})

    report = await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=False,
        resolver=deliverable_domain,
    )

    assert report.checked == 1
    assert report.changed == 1
    await db_session.refresh(channel)
    assert channel.verification_status is VerificationStatus.PUBLISHED_FIRST_PARTY
    written = await db_session.scalar(
        select(func.count())
        .select_from(ContactVerification)
        .where(ContactVerification.channel_id == channel.id)
    )
    assert written == 0
    _ = ContactChannel


async def test_applying_downgrades_the_channel_and_appends_the_check(
    db_session, workspace, channel
) -> None:
    from sqlalchemy import select
    from titan.db.models import ContactVerification

    verifier = ScriptedVerifier({channel.normalized_value: invalid()})

    report = await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=True,
        resolver=deliverable_domain,
    )
    await db_session.commit()

    assert report.changed == 1
    await db_session.refresh(channel)
    assert channel.verification_status is VerificationStatus.INVALID
    assert channel.verification_status not in SENDABLE_VERIFICATION_STATUSES

    rows = (
        (
            await db_session.execute(
                select(ContactVerification).where(
                    ContactVerification.channel_id == channel.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    # The record of having asked, not a rewrite of the summary.
    assert rows[0].result is VerificationStatus.INVALID
    assert rows[0].detail["reverification"] is True
    assert rows[0].detail["mailbox_verification"]["provider"] == "scripted"


async def test_every_check_is_recorded_even_when_nothing_moved(
    db_session, workspace, channel
) -> None:
    """Caught on the first full pass: 584 checked, 53 rows written.

    531 addresses had been asked about and nothing recorded it, so a second run
    would have opened 531 more connections to servers that had already
    answered. The row is the record of having asked, not a diff.
    """
    from sqlalchemy import select
    from titan.db.models import ContactVerification

    # UNKNOWN is not conclusive, so the status will not move.
    verifier = ScriptedVerifier({})

    report = await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=True,
        resolver=deliverable_domain,
    )
    await db_session.commit()

    assert report.checked == 1
    assert report.changed == 0
    rows = (
        (
            await db_session.execute(
                select(ContactVerification).where(
                    ContactVerification.channel_id == channel.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_an_address_asked_about_recently_is_not_asked_again(
    db_session, workspace, channel
) -> None:
    """What makes the command safe to re-run against 604 strangers' servers."""
    verifier = ScriptedVerifier({})

    first = await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=True,
        resolver=deliverable_domain,
    )
    await db_session.commit()
    second = await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=True,
        resolver=deliverable_domain,
    )

    assert first.examined == 1
    assert second.examined == 0
    assert verifier.asked == [channel.normalized_value]


async def test_a_discovery_row_does_not_count_as_having_asked(
    db_session, workspace, channel
) -> None:
    """Caught on the second live pass, which stopped at 95 of 551.

    The discovery path appends a row for every address it stores -- provider
    ``bounce_risk``, recording syntax, domain lists and MX. Nothing in it asked
    a mail server anything. Excluding on recency alone matched 489 of those and
    cut the catch-up pass off over exactly the population it exists for.
    """
    import datetime as dt

    from titan.db.models import ContactVerification

    db_session.add(
        ContactVerification(
            workspace_id=workspace,
            channel_id=channel.id,
            provider="bounce_risk",
            result=VerificationStatus.PUBLISHED_FIRST_PARTY,
            mx_present=True,
            detail={"check": "bounce_risk", "mx": {"status": "mx_present"}},
            verified_at=dt.datetime.now(dt.UTC),
        )
    )
    await db_session.commit()

    verifier = ScriptedVerifier({})
    report = await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=True,
        resolver=deliverable_domain,
    )

    assert report.examined == 1
    assert verifier.asked == [channel.normalized_value]


async def test_a_dry_run_leaves_the_address_askable(
    db_session, workspace, channel
) -> None:
    """It records nothing, so it must not consume the address's turn either."""
    verifier = ScriptedVerifier({})

    await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=False,
        resolver=deliverable_domain,
    )
    await db_session.commit()
    again = await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=False,
        resolver=deliverable_domain,
    )

    assert again.examined == 1


async def test_a_verifier_outage_leaves_the_stored_status_alone(
    db_session, workspace, channel
) -> None:
    """An outage must not rewrite 611 stored answers into UNKNOWN."""
    verifier = ScriptedVerifier({}, raises=True)

    report = await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=True,
        resolver=deliverable_domain,
    )
    await db_session.commit()

    assert report.examined == 1
    assert report.checked == 0
    assert report.changed == 0
    await db_session.refresh(channel)
    assert channel.verification_status is VerificationStatus.PUBLISHED_FIRST_PARTY


async def test_a_catch_all_answer_stops_the_address_being_sendable(
    db_session, workspace, channel
) -> None:
    """A domain that accepts every local part tells us nothing about this
    mailbox, and provenance alone should not carry it past the gate."""
    verifier = ScriptedVerifier({channel.normalized_value: catch_all()})

    await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=True,
        resolver=deliverable_domain,
    )
    await db_session.commit()

    await db_session.refresh(channel)
    assert channel.verification_status is VerificationStatus.CATCH_ALL


async def test_a_first_party_address_going_catch_all_is_not_a_loss(
    db_session, workspace, channel
) -> None:
    """Caught by the repository invariant while this was still wrong.

    Sendability is provenance plus status, never status alone. Catch-all is the
    default on most small-business hosting, and a human publishing the address
    on their own contact page is the evidence the server declines to give. The
    status changes; what may be sent does not, so the report must not count it
    as an address lost.
    """
    verifier = ScriptedVerifier({channel.normalized_value: catch_all()})

    report = await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=True,
        resolver=deliverable_domain,
    )
    await db_session.commit()

    assert report.changed == 1
    assert report.downgraded == []


async def test_an_unknown_answer_changes_nothing(db_session, workspace, channel) -> None:
    """UNKNOWN is what the null verifier says about everything. It must read as
    'nobody asked', not as a verdict."""
    verifier = ScriptedVerifier({})  # defaults to UNKNOWN

    report = await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=True,
        resolver=deliverable_domain,
    )
    await db_session.commit()

    assert report.changed == 0
    await db_session.refresh(channel)
    assert channel.verification_status is VerificationStatus.PUBLISHED_FIRST_PARTY


async def test_another_tenants_addresses_are_not_touched(
    db_session, workspace, second_workspace, channel
) -> None:
    verifier = ScriptedVerifier({channel.normalized_value: invalid()})

    report = await reverify(
        db_session,
        workspace_id=second_workspace,
        verifier=verifier,
        apply=True,
        resolver=deliverable_domain,
    )

    assert report.examined == 0
    assert verifier.asked == []


async def test_a_pager_is_told_how_many_rows_stayed_in_the_set(
    db_session, workspace, channel
) -> None:
    """A caller paging through 604 addresses in committed batches advances by
    this, not by how many it examined.

    Most re-checks leave the status where it was, so those rows stay in the
    result set; advancing by the full batch would step over the rows that *had*
    left it, and those addresses would never be checked at all.
    """
    verifier = ScriptedVerifier({channel.normalized_value: invalid()})

    report = await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=True,
        resolver=deliverable_domain,
    )
    await db_session.commit()

    # It became INVALID, which is not recheckable, so the window shrank by one.
    assert report.examined == 1
    assert report.remained == 0


async def test_an_address_nobody_could_answer_for_stays_in_the_set(
    db_session, workspace, channel
) -> None:
    """An outage must not quietly retire an address from the queue of things
    still worth checking."""
    verifier = ScriptedVerifier({}, raises=True)

    report = await reverify(
        db_session,
        workspace_id=workspace,
        verifier=verifier,
        apply=True,
        resolver=deliverable_domain,
    )

    assert report.remained == 1


def test_the_candidate_carries_the_status_it_started_from() -> None:
    """Needed to tell a downgrade from a sideways move, which is the only
    number in the report worth reading."""
    candidate = Candidate(
        channel_id=uuid.uuid4(),
        email="hello@example.test",
        domain="example.test",
        source=ContactSource.FIRST_PARTY_WEBSITE,
        status_before=VerificationStatus.PUBLISHED_FIRST_PARTY,
    )

    assert candidate.status_before in SENDABLE_VERIFICATION_STATUSES
