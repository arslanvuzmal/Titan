"""Choosing the address that will still be there next week.

Eight hard bounces put two of three mailboxes into timeout and capped the whole
system at 8 sends a day. Reading them one by one, five were addresses that
should never have been chosen:

    0606info@207dentalcare.com      a phone number and a mailbox, spliced
    recruitment@zenlaw.co.uk        a hiring inbox
    lettings@nppresidential.co.uk   a department
    dubai@shamswilliams.com         a branch
    katie@reading-smiles.co.uk      somebody who had left

None of them were malformed and none were undeliverable in principle. Every one
was published on the business's own site. They were simply the wrong address of
the several each site offered, and the resolver picked them because it took the
first candidate that passed -- in *page-crawl order*.

The numbers behind the ordering, measured on this workspace's own sending:

    role addresses      2 bounces / 154 sends   1.30%
    everything else     6 bounces /  74 sends   8.11%

So these tests hold three things that only work together: that a front desk is
recognised in the languages Titan actually sends in, that the best address wins
rather than the earliest, and that the scorer stops rewarding the shape that
bounces six times more often.
"""

from __future__ import annotations

import pytest
from titan.db.enums import ContactSource, VerificationStatus
from titan.intelligence.contacts import (
    DiscoveredContact,
    extract_contacts_from_pages,
    is_never_contact,
    is_role_address,
    name_is_in_local_part,
    rank_contacts,
)
from titan.intelligence.scoring import ScoringInput, score_lead


def _contact(email: str, *, role: bool | None = None) -> DiscoveredContact:
    return DiscoveredContact(
        email=email,
        normalized=email,
        domain=email.split("@", 1)[1],
        source=ContactSource.FIRST_PARTY_WEBSITE,
        source_url="https://example.com/contact",
        is_generic_role=is_role_address(email) if role is None else role,
        confidence=0.8,
    )


class TestAFrontDeskIsAFrontDeskInAnyLanguage:
    """The list was English-only while the campaigns were not."""

    @pytest.mark.parametrize(
        "email",
        [
            # The two most common non-role local parts on the live list,
            # seven each, both previously scored as named individuals.
            "kontakt@zahnarztpraxis.de",
            "praxis@sp64.de",
            # Poland, Spain, Romania, the Netherlands, France, Italy.
            "biuro@dentalavenue.pl",
            "gabinet@stomatologia.pl",
            "administracion@clinica.es",
            "contacto@clinica.es",
            "programari@cabinet.ro",
            "praktijk@tandarts.nl",
            "accueil@cabinet.fr",
            "segreteria@studio.it",
        ],
    )
    def test_foreign_front_desks_are_recognised(self, email: str) -> None:
        assert is_role_address(email)

    @pytest.mark.parametrize(
        "email",
        ["ask@clinic.co.uk", "care@clinic.co.uk", "reservations@spa.co.uk"],
    )
    def test_english_front_desks_the_first_list_missed(self, email: str) -> None:
        assert is_role_address(email)

    @pytest.mark.parametrize(
        "email",
        [
            "katie@reading-smiles.co.uk",  # the one that actually bounced
            "lauren@clinic.co.uk",
            "j.smith@solicitors.co.uk",
        ],
    )
    def test_a_person_is_still_a_person(self, email: str) -> None:
        """Widening the vocabulary must not swallow actual names."""
        assert not is_role_address(email)


class TestTheBestAddressWinsNotTheEarliest:
    def test_the_front_desk_beats_the_named_mailbox(self) -> None:
        """The exact shape of the katie@ mistake, in one assertion."""
        ranked = rank_contacts(
            [
                _contact("katie@reading-smiles.co.uk"),
                _contact("info@reading-smiles.co.uk"),
            ]
        )
        assert ranked[0].normalized == "info@reading-smiles.co.uk"

    def test_page_order_does_not_decide(self) -> None:
        """Same two candidates, opposite discovery order, same winner."""
        a = _contact("info@practice.co.uk")
        b = _contact("sarah@practice.co.uk")
        assert rank_contacts([a, b])[0] == rank_contacts([b, a])[0]

    def test_a_foreign_front_desk_also_beats_a_name(self) -> None:
        ranked = rank_contacts(
            [_contact("stefan@zahnarzt.de"), _contact("praxis@zahnarzt.de")]
        )
        assert ranked[0].normalized == "praxis@zahnarzt.de"

    def test_a_template_placeholder_sorts_last(self) -> None:
        """``looks_like_a_guess`` catches whole local parts -- ``owner``,
        ``ceo``, ``firstname``, a bare initial. Those are titles and template
        leftovers rather than published mailboxes, so they go behind a real
        name, which in turn goes behind the front desk."""
        ranked = rank_contacts(
            [
                _contact("owner@practice.co.uk"),
                _contact("sarah@practice.co.uk"),
                _contact("info@practice.co.uk"),
            ]
        )
        assert [c.normalized for c in ranked] == [
            "info@practice.co.uk",
            "sarah@practice.co.uk",
            "owner@practice.co.uk",
        ]

    def test_ranking_is_stable_across_runs(self) -> None:
        """A retry that picks a different address writes to a different person."""
        candidates = [
            _contact("info@practice.co.uk"),
            _contact("hello@practice.co.uk"),
            _contact("sarah@practice.co.uk"),
        ]
        first = [c.normalized for c in rank_contacts(candidates)]
        for _ in range(5):
            assert [c.normalized for c in rank_contacts(candidates)] == first

    def test_nothing_is_discarded_by_ranking(self) -> None:
        """It reorders. Refusing is the eligibility check's job, not this one."""
        candidates = [
            _contact("sarah@practice.co.uk"),
            _contact("info@practice.co.uk"),
        ]
        assert len(rank_contacts(candidates)) == 2


class TestNeverPitchTheComplaintsDesk:
    """A complaint is not a bounce.

    ``domain_health.classify`` returns BLOCKED on the first complaint, with no
    sample-size threshold and no seven-day recovery. These four were live and
    eligible on the workspace, one of them at a law firm.
    """

    @pytest.mark.parametrize(
        "email",
        [
            "dataprotection@doyleclayton.co.uk",
            "dataprotection@therme.ro",
            "datenschutz@palace-dayspa.de",
            "datenschutzbeauftragter@vpmk.de",
            "rgpd@cabinet.fr",
            "rodo@kancelaria.pl",
            "privacidad@clinica.es",
            "gegevensbescherming@praktijk.nl",
        ],
    )
    def test_privacy_functions_are_refused_in_every_market(self, email: str) -> None:
        assert is_never_contact(email)

    def test_the_english_ones_still_are(self) -> None:
        for email in ("privacy@x.co.uk", "dpo@x.co.uk", "gdpr@x.co.uk"):
            assert is_never_contact(email)

    def test_an_ordinary_address_is_not_caught(self) -> None:
        assert not is_never_contact("info@x.co.uk")
        assert not is_never_contact("praxis@x.de")


class TestTheScorerNoLongerPrefersWhatBounces:
    """It used to rank a nameless mailbox above a published front desk."""

    @staticmethod
    def _decision_maker(*, generic: bool, known: bool = False):
        result = score_lead(_scoring_input(generic=generic, known=known))
        return next(c for c in result.components if c.key == "decision_maker")

    def test_a_published_role_address_outranks_an_unidentified_name(self) -> None:
        assert self._decision_maker(generic=True).raw > (
            self._decision_maker(generic=False).raw
        )

    def test_a_named_decision_maker_still_wins_outright(self) -> None:
        """The change reorders the two unqualified cases. It does not level them."""
        best = self._decision_maker(generic=False, known=True)
        assert best.raw == 1.0
        assert best.raw > self._decision_maker(generic=True).raw

    def test_the_reason_says_what_it_now_believes(self) -> None:
        assert self._decision_maker(generic=True).reason == "published role address"
        assert (
            self._decision_maker(generic=False).reason == "unidentified named mailbox"
        )

    def test_a_role_address_lead_now_scores_at_least_as_high(self) -> None:
        """The whole point: the safer contact must not be the lower-scoring one."""
        role = score_lead(_scoring_input(generic=True)).total
        named = score_lead(_scoring_input(generic=False)).total
        assert role >= named


def _scoring_input(*, generic: bool, known: bool = False) -> ScoringInput:
    """A lead identical in every respect except the shape of its address."""
    return ScoringInput(
        findings=[],
        industry_matches_campaign=True,
        geography_matches_campaign=True,
        services_deliverable=True,
        review_count=40,
        rating=4.5,
        has_website=True,
        website_reachable=True,
        business_status="OPERATIONAL",
        contact_source=ContactSource.FIRST_PARTY_WEBSITE,
        contact_verification=VerificationStatus.PUBLISHED_FIRST_PARTY,
        contact_is_decision_maker=known,
        contact_is_generic_role=generic,
        estimated_project_value_usd=2000.0,
    )


class TestAWebmailAddressThatCarriesTheBusinessName:
    """``snowymedispa@gmail.com`` on snowymedispa.com.au was refused.

    The rule that refused it -- an address on a different domain belongs to
    somebody else -- is right in general and wrong for a small business that
    runs its website on one host and its mail on Gmail. 111 organisations were
    unreachable for that reason while having published a working address.

    The exception is narrow: a *known free-mailbox provider*, and a local part
    that carries the organisation's own name. Both, or it stays refused.
    """

    @pytest.mark.parametrize(
        ("local", "domain"),
        [
            ("snowymedispa", "snowymedispa.com.au"),
            ("elementdentalclinics", "elementdental.co.uk"),
            ("enquiry.beightondentalcare", "beightondentalcare.co.uk"),
            ("brightondentalcare-pm", "brightondentalcare.co.uk"),
            ("crossaircon", "crossaircon.co.uk"),
        ],
    )
    def test_the_name_is_recognised(self, local: str, domain: str) -> None:
        assert name_is_in_local_part(local, domain)

    @pytest.mark.parametrize(
        ("local", "domain"),
        [
            ("fhdental.info", "fhfd.ca"),  # a real refusal, and correct
            ("info", "somedentist.co.uk"),
            ("john", "somedentist.co.uk"),
            ("reception", "brightondentalcare.co.uk"),
        ],
    )
    def test_a_local_part_that_is_not_the_name_is_refused(
        self, local: str, domain: str
    ) -> None:
        assert not name_is_in_local_part(local, domain)

    def test_a_stem_too_short_to_be_evidence_is_refused(self) -> None:
        """Two letters match a large share of local parts by accident."""
        assert not name_is_in_local_part("askme", "as.co.uk")

    def test_punctuation_carries_no_identity(self) -> None:
        assert name_is_in_local_part("snowy-medispa", "snowymedispa.com.au")
        assert name_is_in_local_part("snowy.medispa", "snowymedispa.com.au")


class TestTheExceptionIsNarrow:
    """What must keep being refused, checked through the real extractor."""

    @staticmethod
    def _extract(email: str, org_domain: str):
        import datetime as dt

        from titan.contracts.evidence import PageEvidence

        page = PageEvidence(
            url=f"https://{org_domain}/contact",
            final_url=f"https://{org_domain}/contact",
            http_status=200,
            visible_emails=[email],
            captured_at=dt.datetime(2026, 8, 28, tzinfo=dt.UTC),
        )
        return extract_contacts_from_pages([page], org_domain)

    def test_the_business_own_webmail_becomes_usable(self) -> None:
        found = self._extract("snowymedispa@gmail.com", "snowymedispa.com.au")
        assert [c.is_usable for c in found] == [True]

    def test_another_business_domain_is_still_refused(self) -> None:
        """No local part can make somebody else's domain ours.

        ``info@adcottawa.com`` appeared on billingsbridgedental.ca. It is a
        different business, and remains refused however the address is spelled.
        """
        found = self._extract("info@adcottawa.com", "billingsbridgedental.ca")
        assert [c.is_usable for c in found] == [False]

    def test_a_webmail_without_the_name_is_still_refused(self) -> None:
        found = self._extract("fhdental.info@gmail.com", "fhfd.ca")
        assert [c.is_usable for c in found] == [False]

    def test_a_never_contact_address_is_refused_even_with_the_name(self) -> None:
        """The exception widens *whose* address it is, not what may be written to."""
        found = self._extract("careers.acmedental@gmail.com", "acmedental.co.uk")
        assert [c.is_usable for c in found] == [False]

    def test_the_business_own_domain_still_works_as_before(self) -> None:
        found = self._extract("info@acmedental.co.uk", "acmedental.co.uk")
        assert [c.is_usable for c in found] == [True]
