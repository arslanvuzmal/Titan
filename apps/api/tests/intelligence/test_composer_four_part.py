"""The message has four parts now, and all four have to survive the validator.

The shape asked for, in order: a greeting that knows the time of day; the
defect and what it costs; what changes for them once it is fixed; and who the
sender is, in terms of work they have actually done. Then the citations, at the
end, under a heading.

Two of those are new prose in a message that is already checked line by line,
and the checks are not decorative -- every factual sentence about the
recipient has to trace to an evidenced finding, the pitch has a word band, and
the whole body has another. Adding two paragraphs and a link list to a message
sitting at 218 words was the kind of change that passes on one example and
fails on the eleventh issue type.

So the centrepiece here is ``test_every_issue_type_passes_the_real_validator``,
which composes all fourteen through the actual validator rather than a mock of
it. It has already earned its place twice: a reference *title* reading "the
rest of the page" tripped the claim detector, and so did a case study saying
"rebuilt **the** booking flow". Both were caught here, and both were fixed by
changing the content rather than by loosening the rule.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from titan.intelligence import composer as C
from titan.intelligence import message_validator as mv
from titan.intelligence.case_studies import CaseStudy
from titan.intelligence.composer import (
    OPT_OUT_LINE,
    ComposerContext,
    compose,
    family_for,
)
from titan.intelligence.references import REFERENCES, all_references, references_for

ISSUE_TYPES = sorted(REFERENCES)

STUDY = CaseStudy(
    reference="leeds-dental",
    descriptor="a two-site dental practice in Leeds",
    work="rebuilt a booking flow after a site migration broke it",
    outcome="enquiries started arriving again the same week",
    industries=frozenset({"dentist"}),
    families=frozenset({"technical"}),
    issue_types=frozenset({"broken_primary_cta"}),
)


@dataclass(frozen=True)
class Finding:
    id: str
    issue_type: str
    title: str
    page_url: str | None
    observed_value: str | None


def _compose(
    issue_type: str = "broken_primary_cta",
    *,
    seed: str = "seed",
    industry: str | None = "dentist",
    page_url: str | None = "https://example.com/book",
    case_study: CaseStudy | None = None,
    step_number: int = 0,
    one_pager_url: str | None = None,
):
    return compose(
        ComposerContext(
            org_domain="example.com",
            finding=Finding("f-1", issue_type, "Title", page_url, "404"),
            evidence_ids=["ev-1"],
            owner_name="Arslan Vuzmal",
            portfolio_url="https://arslanvuzmallone.com",
            mailing_address="1 Some Street, Manchester M1 1AA",
            unsubscribe_url="https://titan.example/u/abc123",
            offer_key="conversion",
            business_name="Example Dental",
            industry=industry,
            contact_first_name="Sarah",
            variant_seed=seed,
            step_number=step_number,
            case_study=case_study,
            one_pager_url=one_pager_url,
        )
    )


def _validate(composed) -> mv.ValidationReport:
    return mv.validate_message(
        mv.MessageContext(
            subject=composed.subject,
            body=composed.body,
            claim_map=composed.claim_map,
            evidenced_finding_ids=frozenset({"f-1"}),
            sender_name="Arslan Vuzmal",
            portfolio_url="https://arslanvuzmallone.com",
            mailing_address="1 Some Street, Manchester M1 1AA",
            unsubscribe_present=True,
            known_names=frozenset({"Sarah"}),
        )
    )


class TestTheWholeShapeStillValidates:
    @pytest.mark.parametrize("issue_type", ISSUE_TYPES)
    @pytest.mark.parametrize("seed", ["a", "b", "c", "d"])
    @pytest.mark.parametrize("case_study", [None, STUDY])
    def test_every_issue_type_passes_the_real_validator(
        self, issue_type: str, seed: str, case_study: CaseStudy | None
    ) -> None:
        report = _validate(
            _compose(issue_type, seed=seed, case_study=case_study)
        )
        assert report.passed, [str(v) for v in report.violations]

    @pytest.mark.parametrize("issue_type", ISSUE_TYPES)
    def test_the_pitch_stays_inside_its_band(self, issue_type: str) -> None:
        """The band the validator enforces, measured the way it measures it."""
        composed = _compose(issue_type, case_study=STUDY)
        words = len(mv.pitch_of(composed.body, "Arslan Vuzmal").split())
        assert mv.PITCH_MIN_WORDS <= words <= mv.PITCH_MAX_WORDS

    @pytest.mark.parametrize("issue_type", ISSUE_TYPES)
    def test_the_body_stays_inside_its_band(self, issue_type: str) -> None:
        composed = _compose(issue_type, case_study=STUDY)
        assert len(composed.body.split()) <= mv.MAX_BODY_WORDS

    def test_the_reported_pitch_length_agrees_with_the_validators(self) -> None:
        """The two counts disagreeing is how a message passes here and fails there."""
        composed = _compose(case_study=STUDY)
        assert composed.pitch_words == len(
            mv.pitch_of(composed.body, "Arslan Vuzmal").split()
        )


class TestWhatChangesIfTheyFixIt:
    """The half of the argument that was missing: the upside, not just the cost."""

    @pytest.mark.parametrize("issue_type", ISSUE_TYPES)
    def test_every_issue_type_says_what_gets_better(self, issue_type: str) -> None:
        composed = _compose(issue_type)
        assert any(
            entry["claim"].endswith(":upside") for entry in composed.claim_map
        ), f"{issue_type} has no upside paragraph"

    @pytest.mark.parametrize("issue_type", ISSUE_TYPES)
    def test_the_upside_is_traced_to_the_finding_that_entails_it(
        self, issue_type: str
    ) -> None:
        """A conditional about their site is still a claim about their site."""
        composed = _compose(issue_type)
        upside = [e for e in composed.claim_map if e["claim"].endswith(":upside")]
        assert upside
        for entry in upside:
            assert entry["finding_id"] == "f-1"
            assert entry["evidence_ids"] == ["ev-1"]
            assert entry["sentence"] in composed.body

    def test_it_reads_as_a_consequence_of_the_repair(self) -> None:
        composed = _compose("broken_primary_cta")
        assert "Once the button points somewhere that exists" in composed.body

    @pytest.mark.parametrize("issue_type", ISSUE_TYPES)
    def test_no_invented_numbers(self, issue_type: str) -> None:
        """The paragraph most likely to attract "30% more bookings"."""
        composed = _compose(issue_type)
        report = _validate(composed)
        assert not [
            v
            for v in report.violations
            if v.code is mv.ViolationCode.FABRICATED_METRIC
        ]


class TestTheReferencesSection:
    @pytest.mark.parametrize("issue_type", ISSUE_TYPES)
    def test_the_citations_are_listed_under_a_heading(self, issue_type: str) -> None:
        composed = _compose(issue_type)
        assert "\nReferences\n" in composed.body

    @pytest.mark.parametrize("issue_type", ISSUE_TYPES)
    def test_the_page_examined_is_cited_first(self, issue_type: str) -> None:
        composed = _compose(issue_type)
        assert composed.reference_urls[0] == "https://example.com/book"

    @pytest.mark.parametrize("issue_type", ISSUE_TYPES)
    def test_the_curated_sources_for_the_issue_are_cited(
        self, issue_type: str
    ) -> None:
        composed = _compose(issue_type)
        for reference in references_for(issue_type):
            assert reference.url in composed.reference_urls
            assert reference.url in composed.body

    def test_a_finding_with_no_page_still_cites_its_standards(self) -> None:
        composed = _compose("no_visible_phone_number", page_url=None)
        assert "\nReferences\n" in composed.body
        assert composed.reference_urls == [
            reference.url for reference in references_for("no_visible_phone_number")
        ]

    def test_the_citations_sit_below_the_signature(self) -> None:
        """Above it they would be charged against the pitch word budget."""
        composed = _compose()
        assert composed.body.index("Arslan Vuzmal") < composed.body.index("References")

    def test_the_opt_out_is_not_mistaken_for_a_citation(self) -> None:
        body = _compose().body
        assert "\n\n" + OPT_OUT_LINE in body

    def test_the_html_part_lists_them_as_links(self) -> None:
        composed = _compose()
        assert "<ul" in composed.body_html
        for url in composed.reference_urls:
            assert f'href="{url}"' in composed.body_html

    def test_every_curated_url_is_https(self) -> None:
        """A cited source served over http is a citation that warns the reader."""
        for reference in all_references():
            assert reference.url.startswith("https://"), reference.url

    def test_no_reference_title_reads_as_a_claim_about_the_recipient(self) -> None:
        """The failure that got here first: "stops the rest of the page"."""
        for reference in all_references():
            assert not mv.reads_as_recipient_claim(reference.title), reference.title


class TestTheCredibilityParagraph:
    def test_without_a_case_study_the_generic_credential_stays(self) -> None:
        composed = _compose()
        assert "I recently" not in composed.body
        assert composed.case_study_reference == ""

    def test_a_matching_case_study_is_cited_verbatim(self) -> None:
        composed = _compose(case_study=STUDY)
        assert STUDY.sentence() in composed.body
        assert composed.case_study_reference == "leeds-dental"

    def test_the_portfolio_link_survives_beside_it(self) -> None:
        """The validator refuses a message that lost its portfolio URL."""
        composed = _compose(case_study=STUDY)
        assert "https://arslanvuzmallone.com" in composed.body

    def test_a_case_study_does_not_become_an_unevidenced_claim(self) -> None:
        report = _validate(_compose(case_study=STUDY))
        assert not [
            v
            for v in report.violations
            if v.code
            in {
                mv.ViolationCode.UNSUPPORTED_CLAIM,
                mv.ViolationCode.UNVERIFIABLE_CLIENTELE,
            }
        ]


class TestNothingThatWorkedBeforeStopped:
    def test_the_page_is_still_named_in_the_observation(self) -> None:
        assert "your booking page" in _compose().body

    def test_a_compound_slug_still_resolves_to_a_trade_noun(self) -> None:
        """/book-appointment is not a key, though book and appointment are."""
        composed = _compose(page_url="https://example.com/book-appointment")
        assert "your appointment page" in composed.body

    def test_an_unknown_slug_does_not_name_the_page_twice(self) -> None:
        """"the main button on the page on example.com" -- the link text and the
        sentence saying the same thing three words apart."""
        composed = _compose(page_url="https://example.com/some-odd-slug")
        assert "on the page on example.com" not in composed.body
        assert "your /some-odd-slug page" in composed.body

    def test_a_follow_up_still_opens_by_acknowledging_the_first(self) -> None:
        composed = _compose(step_number=1)
        assert composed.variant.endswith(":step1")

    def test_the_same_lead_composes_the_same_message(self) -> None:
        first = _compose(seed="lead-42", case_study=STUDY)
        assert first.body == _compose(seed="lead-42", case_study=STUDY).body

    def test_family_for_agrees_with_the_composers_own_table(self) -> None:
        assert family_for("slow_largest_contentful_paint") == "speed"
        assert family_for("an_issue_type_that_does_not_exist") == "technical"


class TestTheOptOut:
    """The visible unsubscribe link is gone; the opt-out is not.

    Removing the *header* would be the expensive mistake -- it is what renders
    Gmail's own one-click control, it is a BLOCK-severity gate in
    ``deliverability.py``, and a recipient who cannot find it presses "report
    spam" instead. That header is built in ``activities/pipeline.py`` from the
    sender identity and never came from the body, so these hold the visible
    half only.
    """

    def test_no_bare_unsubscribe_url_in_the_body(self) -> None:
        body = _compose().body
        assert "https://titan.example/u/abc123" not in body
        assert "Unsubscribe (" not in body

    def test_the_opt_out_sentence_is_present(self) -> None:
        assert OPT_OUT_LINE in _compose().body

    def test_the_html_carries_it_and_links_nothing(self) -> None:
        html = _compose().body_html
        assert OPT_OUT_LINE in html
        assert "https://titan.example/u/abc123" not in html

    def test_the_offer_it_makes_is_one_the_system_keeps(self) -> None:
        """The footer promises suppression on request. Hold the classifier to it."""
        from titan.intelligence.replies import InboundMessage, classify_reply

        for wording in (
            "Please don't contact me again.",
            "Please take us off your list.",
            "Stop emailing me.",
            "No further emails please.",
        ):
            result = classify_reply(
                InboundMessage(
                    from_email="owner@practice.example",
                    subject="Re: your booking page",
                    body_text=wording,
                )
            )
            assert result.requires_suppression, wording

    def test_a_request_to_fix_something_is_not_an_opt_out(self) -> None:
        """The most expensive false positive available: suppressing a hot lead."""
        from titan.intelligence.replies import InboundMessage, classify_reply

        result = classify_reply(
            InboundMessage(
                from_email="owner@practice.example",
                subject="Re: your booking page",
                body_text="Can you remove the broken link and email me the fix?",
            )
        )
        assert not result.requires_suppression


PROJECT = CaseStudy(
    reference="voxcircuit",
    name="VoxCircuit",
    summary="a platform that answers enquiry calls and books the appointment",
    url="https://arslanvuzmallone.com/projects/voxdesk-ai",
    families=frozenset({"conversion"}),
    issue_types=frozenset({"no_booking_or_enquiry_path"}),
)


class TestTheProjectLink:
    def test_the_system_is_named_and_linked_inside_the_sentence(self) -> None:
        composed = _compose(case_study=PROJECT)
        assert (
            "I built VoxCircuit (https://arslanvuzmallone.com/projects/voxdesk-ai),"
            in composed.body
        )

    def test_the_html_anchors_the_name(self) -> None:
        html = _compose(case_study=PROJECT).body_html
        assert (
            '<a href="https://arslanvuzmallone.com/projects/voxdesk-ai">VoxCircuit</a>'
            in html
        )

    def test_the_generic_portfolio_sentence_is_dropped(self) -> None:
        """Two links to the same site three words apart is one too many."""
        assert "my portfolio" not in _compose(case_study=PROJECT).body

    def test_the_portfolio_check_still_passes(self) -> None:
        """It passes because the project URL sits beneath the portfolio URL."""
        composed = _compose(case_study=PROJECT)
        assert "https://arslanvuzmallone.com" in composed.body
        report = _validate(composed)
        assert not [
            v
            for v in report.violations
            if v.code is mv.ViolationCode.WRONG_PORTFOLIO_URL
        ]

    def test_a_project_hosted_elsewhere_keeps_the_portfolio_sentence(self) -> None:
        """So the check above passes for a reason rather than by luck."""
        offsite = CaseStudy(
            reference="offsite",
            name="Elsewhere",
            summary="a thing built somewhere else",
            url="https://github.com/arslanvuzmal/Orchestrion",
            families=frozenset({"conversion"}),
        )
        composed = _compose("no_booking_or_enquiry_path", case_study=offsite)
        assert "https://github.com/arslanvuzmal/Orchestrion" in composed.body
        assert "https://arslanvuzmallone.com" in composed.body
        assert _validate(composed).passed

    def test_a_client_engagement_still_renders_the_old_way(self) -> None:
        composed = _compose(case_study=STUDY)
        assert "I recently rebuilt a booking flow" in composed.body
        assert "my portfolio" in composed.body


class TestTheOnePager:
    """Linked, not attached.

    An unsolicited PDF from an unknown sender is a strong spam signal and
    corporate gateways strip them, so a share of recipients would be told to
    read something removed in transit. A hosted page also produces a click,
    which is the one engagement signal this system does not otherwise have.
    """

    URL = "https://arslanvuzmallone.com/one-pager.pdf"

    def test_it_is_cited_when_configured(self) -> None:
        composed = _compose(one_pager_url=self.URL)
        assert self.URL in composed.body
        assert self.URL in composed.reference_urls

    def test_it_comes_last_behind_the_primary_sources(self) -> None:
        """The sender's own material has not earned the top of a source list."""
        composed = _compose(one_pager_url=self.URL)
        assert composed.reference_urls[-1] == self.URL

    def test_unset_renders_nothing(self) -> None:
        composed = _compose()
        assert "One-page summary" not in composed.body

    def test_it_does_not_break_the_validator(self) -> None:
        assert _validate(_compose(one_pager_url=self.URL)).passed

    def test_it_does_not_push_the_body_over_its_band(self) -> None:
        for issue_type in ISSUE_TYPES:
            composed = _compose(
                issue_type, case_study=STUDY, one_pager_url=self.URL
            )
            assert len(composed.body.split()) <= mv.MAX_BODY_WORDS, issue_type

    def test_the_message_never_claims_an_attachment(self) -> None:
        """A message that says "attached" while attaching nothing is worse than
        one that says nothing at all -- and OutboundEmail carries no
        attachment field, so nothing could be attached even if it did."""
        body = _compose(one_pager_url=self.URL).body.lower()
        for word in ("attached", "attachment", "please find enclosed"):
            assert word not in body


class TestItStoppedClearingItsThroat:
    """The generic-context paragraph, dropped 10 September.

    It said the same thing to every recipient in every industry -- "in 2026
    this is doing the work a receptionist used to do" -- and it was the only
    paragraph in the message carrying no claim-map entry. That is the tell
    rather than a technicality: it asserted nothing about this business because
    there was nothing about this business in it.

    Measured over 413 delivered messages the old structure averaged 277 words.
    """

    @pytest.mark.parametrize("issue_type", ISSUE_TYPES)
    def test_the_generic_context_sentence_is_gone(self, issue_type: str) -> None:
        """Planted violation: keep a paragraph nobody can act on.

        Asserted against the registers themselves rather than a quoted string,
        so editing the copy cannot quietly reintroduce it.
        """
        body = _compose(issue_type).body
        for register in C._CONTEXT_REGISTERS:
            assert register not in body, f"{issue_type} still renders a context register"

    @pytest.mark.parametrize("issue_type", ISSUE_TYPES)
    def test_every_message_still_lands_in_the_band(self, issue_type: str) -> None:
        """The floor is a structural guarantee, not a style preference.

        Under it, one of observation, mechanism, consequence, repair, credential
        or ask has gone missing. Dropping a paragraph is only safe if the rest
        still clear the floor -- which is the whole reason the floor moved with
        the structure rather than after it.
        """
        composed = _compose(issue_type)
        assert mv.PITCH_MIN_WORDS <= composed.pitch_words <= mv.PITCH_MAX_WORDS

    @pytest.mark.parametrize("issue_type", ISSUE_TYPES)
    def test_it_still_validates(self, issue_type: str) -> None:
        """Removing a paragraph must not orphan a claim or break the rules."""
        composed = _compose(issue_type)
        assert _validate(composed).passed, f"{issue_type} no longer validates"

    def test_the_parts_that_earn_their_space_are_all_still_there(self) -> None:
        """What was kept, stated as a test so a later trim has to argue with it.

        The upside was considered for the same cut and kept: it restates the
        consequence with the sign flipped, which is the argument against it, but
        it is the only paragraph telling the reader what they get rather than
        what they have lost. There is no evidence in 417 sends and one reply
        that would justify overturning that on taste.
        """
        composed = _compose("broken_primary_cta")
        kinds = {entry["claim"].rsplit(":", 1)[-1] for entry in composed.claim_map}

        assert "mechanism" in kinds, "the paragraph that proves we looked"
        assert "business_impact" in kinds, "what it costs them"
        assert "remediation" in kinds, "what the repair actually is"
        assert "upside" in kinds, "what they get once it is done"

    def test_the_band_has_exactly_one_definition(self) -> None:
        """Two constants that must agree are one constant.

        Both modules used to declare it, each with a comment asking a human to
        keep them in step. When they drifted, the validator refused everything
        the composer wrote -- 628 drafts failed their own generator's rules.
        """
        assert C.PITCH_MIN_WORDS is mv.PITCH_MIN_WORDS
        assert C.PITCH_MAX_WORDS is mv.PITCH_MAX_WORDS
