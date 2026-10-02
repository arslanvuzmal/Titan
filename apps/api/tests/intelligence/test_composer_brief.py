"""The brief form: what goes out from the cold domains.

60-120 words before the signature, exactly one link, no references block --
and still every factual sentence traced to an evidenced finding, held by the
real validator rather than a mock of it, for every issue type and register.
"""

from __future__ import annotations

import re

import pytest
from titan.intelligence import message_validator as mv
from titan.intelligence.composer import ComposerContext, compose

from tests.intelligence.test_composer_four_part import ISSUE_TYPES, STUDY, Finding

URL = re.compile(r"https?://")


def _brief(issue_type: str, seed: str, *, study=None, step_number: int = 0):
    return compose(
        ComposerContext(
            org_domain="example.com",
            finding=Finding(
                "f-1", issue_type, "Title", "https://example.com/book", "404"
            ),
            evidence_ids=["ev-1"],
            owner_name="Arslan Vuzmal",
            portfolio_url="https://arslanvuzmallone.com",
            mailing_address="1 Some Street, Manchester M1 1AA",
            unsubscribe_url="https://titan.example/u/abc123",
            offer_key="conversion",
            business_name="Example Dental",
            industry="dentist",
            contact_first_name="Sarah",
            variant_seed=seed,
            case_study=study,
            step_number=step_number,
            one_pager_url="https://arslanvuzmallone.com/brief",
            brief=True,
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
            form="brief",
        )
    )


@pytest.mark.parametrize("issue_type", ISSUE_TYPES)
@pytest.mark.parametrize("seed", ["a", "b", "c", "d"])
@pytest.mark.parametrize("study", [None, STUDY], ids=["no-study", "study"])
def test_every_issue_type_passes_the_real_validator(issue_type, seed, study) -> None:
    composed = _brief(issue_type, seed, study=study)
    report = _validate(composed)
    assert report.passed, (issue_type, seed, report.violations, composed.body)


@pytest.mark.parametrize("issue_type", ISSUE_TYPES)
def test_exactly_one_link_and_no_references(issue_type) -> None:
    composed = _brief(issue_type, "a")
    assert len(URL.findall(composed.body)) == 1, composed.body
    assert "References" not in composed.body
    assert composed.reference_urls == []
    assert composed.body_html.count("<a ") == 1


@pytest.mark.parametrize("issue_type", ISSUE_TYPES)
def test_the_pitch_sits_in_the_brief_band(issue_type) -> None:
    for seed in "abcd":
        words = len(mv.pitch_of(_brief(issue_type, seed).body, "Arslan Vuzmal").split())
        assert mv.BRIEF_PITCH_MIN_WORDS <= words <= mv.BRIEF_PITCH_MAX_WORDS, (
            issue_type,
            seed,
            words,
        )


def test_the_variant_records_the_form() -> None:
    assert _brief("broken_primary_cta", "a").variant.endswith(":brief")


def test_a_follow_up_is_brief_too() -> None:
    composed = _brief("broken_primary_cta", "a", step_number=1)
    assert _validate(composed).passed
    assert len(URL.findall(composed.body)) == 1


def test_the_full_form_is_refused_under_the_brief_band() -> None:
    full = compose(
        ComposerContext(
            org_domain="example.com",
            finding=Finding(
                "f-1", "no_booking_or_enquiry_path", "T", "https://example.com/book", "x"
            ),
            evidence_ids=["ev-1"],
            owner_name="Arslan Vuzmal",
            portfolio_url="https://arslanvuzmallone.com",
            mailing_address="1 Some Street, Manchester M1 1AA",
            unsubscribe_url="https://titan.example/u/abc123",
            offer_key="conversion",
            industry="dentist",
            contact_first_name="Sarah",
            variant_seed="a",
        )
    )
    codes = {v.code for v in _validate(full).violations}
    assert mv.ViolationCode.PITCH_TOO_LONG in codes


def test_the_send_gate_accepts_either_form() -> None:
    low, high = mv.ANY_FORM_BAND
    assert low == mv.BRIEF_PITCH_MIN_WORDS
    assert high == mv.PITCH_MAX_WORDS
