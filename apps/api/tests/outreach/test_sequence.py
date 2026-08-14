"""The sequence copy: what each step says, and what it refuses to say.

The spec these enforce is the personalization ruleset: no invented metrics, no
generic compliments, no spam markers in the subject, a low-friction ask rather
than a meeting demand, and follow-ups that never restate the pitch.
"""

from __future__ import annotations

import pytest
from titan.outreach.sequence import (
    MAX_WORDS,
    MIN_WORDS,
    STEP_DELAYS_IN_DAYS,
    SUBJECT_ROTATION,
    TEMPLATE_KEYS,
    compliance_footer,
    compose_first_email,
    compose_follow_up_1,
    compose_follow_up_2,
    compose_follow_up_3,
    count_words,
    rotate_subject,
    salutation,
    with_footer,
)
from titan.outreach.variables import _CONSEQUENCE, _INSIGHT, _SHORT, FindingVariables

MAPPED = sorted(_CONSEQUENCE)

FOOTER = compliance_footer(
    sender_name="Arslan Vuzmal Lone",
    portfolio_url="https://arslanvuzmallone.com",
    mailing_address="House No. 440, Islamabad, 44000, Pakistan",
    unsubscribe_line="To stop receiving these, reply STOP.",
)


def variables_for(issue_type: str) -> FindingVariables:
    return FindingVariables(
        consequence=_CONSEQUENCE[issue_type],
        short=_SHORT[issue_type],
        insight=_INSIGHT[issue_type],
        friction="",
    )


# ------------------------------------------------------------------ greeting
def test_a_missing_name_never_renders_as_a_bare_comma() -> None:
    """Most leads are role addresses. "Hi ," is the tell that gives that away."""
    assert salutation(None) == "Hello,"
    assert salutation("") == "Hello,"
    assert salutation("   ") == "Hello,"


def test_a_known_name_is_used() -> None:
    assert salutation("Sarah") == "Hi Sarah,"


# ------------------------------------------------------------------- subject
def test_the_subject_is_stable_for_a_lead() -> None:
    """A retry must not compose a different subject than the first attempt."""
    args = {"lead_id": "1f0c9d2e", "company": "Oakwood Dental", "short": "the link"}
    assert rotate_subject(**args) == rotate_subject(**args)


def test_different_leads_do_not_all_get_the_same_subject() -> None:
    subjects = {
        rotate_subject(lead_id=str(n), company="Acme", short="the link")
        for n in range(60)
    }
    assert len(subjects) > 1


def test_the_subject_starts_with_a_capital() -> None:
    """A bare "{short}" would otherwise start lower-case in an inbox list."""
    for n in range(30):
        subject = rotate_subject(
            lead_id=str(n), company="acme dental", short="the broken link"
        )
        assert subject[:1].isupper()


def test_the_subject_is_bounded() -> None:
    subject = rotate_subject(lead_id="x", company="C" * 400, short="s")
    assert len(subject) <= 120


def test_no_subject_line_uses_a_spam_marker() -> None:
    """Rule: no fake Re:/Fwd:, no urgency, no exaggeration, no emoji."""
    banned = ("re:", "fwd:", "urgent", "!", "free", "guarantee", "act now")
    for template in SUBJECT_ROTATION:
        lowered = template.lower()
        for marker in banned:
            assert marker not in lowered, f"{template!r} contains {marker!r}"
        assert template.isascii(), f"{template!r} is not plain ascii"


# --------------------------------------------------------------- first email
@pytest.mark.parametrize("issue_type", MAPPED)
def test_the_first_email_stays_within_the_word_bounds(issue_type: str) -> None:
    body = compose_first_email(
        first_name=None,
        company_name="Oakwood Dental",
        verified_finding="a navigation link that returns HTTP 404",
        likely_consequence=_CONSEQUENCE[issue_type],
    )
    assert MIN_WORDS <= count_words(body) <= MAX_WORDS


def test_the_first_email_asks_permission_rather_than_a_meeting() -> None:
    """Rule 7. The CTA is low friction, not a calendar demand."""
    body = compose_first_email(
        first_name=None,
        company_name="Acme",
        verified_finding="a broken link",
        likely_consequence="finding the page they were after",
    )
    assert "Would you like me to send the short breakdown?" in body
    for demand in ("call", "meeting", "calendar", "book a time", "15 minutes"):
        assert demand not in body.lower()


def test_the_first_email_makes_no_claim_about_results_elsewhere() -> None:
    body = compose_first_email(
        first_name=None,
        company_name="Acme",
        verified_finding="a broken link",
        likely_consequence="finding the page they were after",
    )
    assert "%" not in body
    assert not any(sym in body for sym in ("$", "£", "€"))


def test_an_empty_company_name_does_not_leave_a_hole() -> None:
    body = compose_first_email(
        first_name=None,
        company_name="   ",
        verified_finding="a broken link",
        likely_consequence="finding the page",
    )
    assert "your site" in body


# ---------------------------------------------------------------- follow-ups
def test_follow_up_1_needs_no_finding_at_all() -> None:
    """It asserts nothing about the recipient, so any contacted lead may get it."""
    body = compose_follow_up_1(first_name=None)
    assert body.startswith("Hello,")
    assert "breakdown" in body


@pytest.mark.parametrize("issue_type", MAPPED)
def test_the_later_follow_ups_name_the_same_fault_as_the_first_message(
    issue_type: str,
) -> None:
    variables = variables_for(issue_type)
    assert _SHORT[issue_type] in compose_follow_up_2(first_name=None, variables=variables)
    assert _SHORT[issue_type] in compose_follow_up_3(first_name=None, variables=variables)


@pytest.mark.parametrize(
    "compose", [compose_follow_up_2, compose_follow_up_3], ids=["day8", "day13"]
)
def test_a_follow_up_refuses_an_unmapped_finding_rather_than_rendering_a_blank(
    compose,
) -> None:
    """ "One additional thought on :" is the failure this guard exists for."""
    empty = FindingVariables(consequence="", short="", insight="", friction="")
    with pytest.raises(ValueError, match="mapped finding"):
        compose(first_name=None, variables=empty)


@pytest.mark.parametrize("issue_type", MAPPED)
def test_a_follow_up_never_restates_the_pitch(issue_type: str) -> None:
    """Rule 9. The offer is made once."""
    pitch = "I mapped a simple way to fix it"
    variables = variables_for(issue_type)
    assert pitch not in compose_follow_up_1(first_name=None)
    assert pitch not in compose_follow_up_2(first_name=None, variables=variables)
    assert pitch not in compose_follow_up_3(first_name=None, variables=variables)


@pytest.mark.parametrize("issue_type", MAPPED)
def test_no_follow_up_runs_longer_than_a_first_message_may(issue_type: str) -> None:
    """The word ceiling binds every step, not just the pitch.

    Not "shorter than the first message": the day-8 note carries the one extra
    observation, and for the longest of them it lands at the same length as the
    pitch. That is the note doing its job, so the ceiling is what is enforced.
    """
    variables = variables_for(issue_type)
    for body in (
        compose_follow_up_1(first_name=None),
        compose_follow_up_2(first_name=None, variables=variables),
        compose_follow_up_3(first_name=None, variables=variables),
    ):
        assert count_words(body) <= MAX_WORDS


@pytest.mark.parametrize("issue_type", MAPPED)
def test_the_bookend_follow_ups_stay_brief(issue_type: str) -> None:
    """Neither the nudge nor the sign-off carries new material."""
    variables = variables_for(issue_type)
    assert count_words(compose_follow_up_1(first_name=None)) < MIN_WORDS
    assert (
        count_words(compose_follow_up_3(first_name=None, variables=variables)) < MIN_WORDS
    )


# -------------------------------------------------------------------- footer
def test_the_footer_carries_identity_address_and_opt_out_together() -> None:
    assert "Arslan Vuzmal Lone" in FOOTER
    assert "https://arslanvuzmallone.com" in FOOTER
    assert "Islamabad" in FOOTER
    assert "reply STOP" in FOOTER


def test_the_footer_omits_parts_it_was_not_given() -> None:
    assert (
        compliance_footer(
            sender_name="A", portfolio_url="", mailing_address="", unsubscribe_line=""
        )
        == "A"
    )


def test_the_footer_is_separated_from_the_message_a_human_reads() -> None:
    """Rule 12. The legal block is appended, not woven into the copy."""
    body = compose_follow_up_1(first_name=None)
    full = with_footer(body, FOOTER)
    conversational, separator, footer = full.partition("\n--\n")
    assert separator, "the footer must be delimited"
    assert conversational.strip() == body
    assert footer.strip() == FOOTER


def test_a_body_with_no_footer_is_left_alone() -> None:
    assert with_footer("hello", "") == "hello\n"


# -------------------------------------------------------------------- cadence
def test_the_steps_land_on_days_1_4_8_13() -> None:
    day, days = 1, []
    for gap in STEP_DELAYS_IN_DAYS:
        day += gap
        days.append(day)
    assert days == [1, 4, 8, 13]


def test_every_step_has_a_template_key() -> None:
    assert len(TEMPLATE_KEYS) == len(STEP_DELAYS_IN_DAYS)
    assert len(set(TEMPLATE_KEYS)) == len(TEMPLATE_KEYS)
