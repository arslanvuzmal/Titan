"""The rules that decide what a message says, and to whom.

Four samples of what this system actually sent, before any of this existed:

    Something on wellbeingclinic.ae looks unintentional: a navigation link
    points at a page that returns HTTP 404. [...] Fixing this sort of thing is
    what I do -- follow-up for enquiries that do not book immediately, mostly
    for firms your size.

    On kaytons.co.uk the 11 of 16 images lack alt text, so anyone who gets that
    far cannot complete the step.

    the enquiry form asks for 8 visible fields fields

    My work is trial booking plus an automatic sequence up to the first
    session, usually for teams around your size.

Every defect in those four has a test below. The offer that has nothing to do
with the finding above it; the consequence sentence written for broken links
and glued onto an accessibility finding; the claim about a client base that was
never named; the doubled word; the meeting asked for by a stranger.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from titan.db.enums import Industry
from titan.intelligence.composer import (
    PITCH_MAX_WORDS,
    PITCH_MIN_WORDS,
    ComposerContext,
    compose,
)
from titan.intelligence.message_validator import (
    MessageContext,
    ViolationCode,
    validate_message,
)
from titan.intelligence.playbooks import select_offers
from titan.intelligence.vernacular import (
    VERNACULARS,
    Engine,
    engine_for,
    on_a_money_path,
    vernacular_for,
)

OWNER = "Arslan Vuzmal Lone"
PORTFOLIO = "https://arslanvuzmallone.com"
ADDRESS = "House No. 440, Street 23, Block C, Sector B-17, Islamabad, 44000, Pakistan"

#: Every issue type the detectors actually produce, with a real observed value
#: taken from the database rather than invented. A description written against a
#: made-up value passes and then mangles the real one.
LIVE_FINDINGS: tuple[tuple[str, str, str, str], ...] = (
    ("broken_internal_link", "4 internal page(s) return an error", "/book", "HTTP 404"),
    (
        "broken_internal_link",
        "4 internal page(s) return an error",
        "/news/2019/press",
        "HTTP 404",
    ),
    (
        "failed_network_requests",
        "50 resource(s) failed to load",
        "/",
        "GET http://x.test/a.woff2 :: mixed-content",
    ),
    (
        "high_friction_contact_form",
        "Enquiry form asks for 12 fields",
        "/contact",
        "12 visible fields",
    ),
    ("images_missing_alt_text", "7 of 18 images lack alt text", "/team", "7/18"),
    (
        "javascript_console_errors",
        "1 JavaScript error(s) on load",
        "/",
        "Failed to load resource",
    ),
    ("missing_meta_description", "Homepage has no meta description", "/", "absent"),
    ("missing_mobile_viewport", "Homepage has no mobile viewport tag", "/", "absent"),
    (
        "missing_security_headers",
        "2 standard security headers are absent",
        "/",
        "Strict-Transport-Security, Content-Security-Policy",
    ),
    (
        "no_booking_or_enquiry_path",
        "No booking link or enquiry form found on the site",
        "/",
        "0 booking links and 0 forms across 7 pages",
    ),
    (
        "no_structured_data",
        "No structured data (schema.org) found",
        "/",
        "0 application/ld+json blocks",
    ),
    (
        "no_visible_phone_number",
        "No phone number found on the crawled pages",
        "/",
        "no tel: link or phone-shaped text across 1 pages",
    ),
    (
        "serious_accessibility_violations",
        "1 serious accessibility issue(s) detected",
        "/contact",
        "color-contrast (54 nodes)",
    ),
    (
        "slow_largest_contentful_paint",
        "Main content takes 24.0s to appear",
        "/",
        "LCP 23994ms",
    ),
)


@dataclass
class Finding:
    issue_type: str
    title: str
    page_url: str | None
    observed_value: str | None
    id: str = "finding-1"


def message(
    issue: str,
    title: str,
    path: str,
    observed: str,
    *,
    industry: Industry = Industry.DENTIST,
    seed: str = "lead-1",
):
    finding = Finding(issue, title, f"https://example.test{path}", observed)
    offers = select_offers(industry, {issue})
    assert offers, f"no offer covers {issue} for {industry.value}"
    return compose(
        ComposerContext(
            org_domain="example.test",
            finding=finding,
            evidence_ids=["ev-1"],
            owner_name=OWNER,
            portfolio_url=PORTFOLIO,
            mailing_address=ADDRESS,
            unsubscribe_url=f"{PORTFOLIO}/unsubscribe?e=a&t=b",
            offer_key=offers[0].key,
            industry=industry,
            business_name="Bellrose Dental Practice",
            variant_seed=seed,
        )
    )


def report(composed):
    return validate_message(
        MessageContext(
            subject=composed.subject,
            body=composed.body,
            claim_map=composed.claim_map,
            evidenced_finding_ids=frozenset({"finding-1"}),
            sender_name=OWNER,
            portfolio_url=PORTFOLIO,
            mailing_address=ADDRESS,
            unsubscribe_present=True,
        )
    )


# ==========================================================================
# The engines
# ==========================================================================


def test_a_broken_booking_link_and_a_broken_footer_link_are_different_sales() -> None:
    """``broken_internal_link`` is 2,355 findings and covers both.

    Every one of them used to open "A broken step on {domain}". One of those
    two readers has a person who tried to book and could not; the other has a
    dead link to a 2019 press release.
    """
    assert engine_for("broken_internal_link", "https://x.test/book") is Engine.CONVERSION
    assert (
        engine_for("broken_internal_link", "https://x.test/news/2019/press")
        is Engine.QUALITY
    )


def test_no_booking_path_at_all_is_not_a_defect() -> None:
    """Nothing is broken. There is no system, which is a different pitch --
    and the only operational gap a crawler can evidence."""
    assert engine_for("no_booking_or_enquiry_path") is Engine.AUTOMATION


def test_a_money_path_is_matched_on_segments_not_substrings() -> None:
    """Planted violation: use ``in`` and "/about-our-book-club" becomes a
    booking emergency, and "/no-contact-order" becomes a broken contact page at
    a law firm -- wrong on the facts, and unrecoverable."""
    assert on_a_money_path("https://x.test/book")
    assert on_a_money_path("https://x.test/en/new-patients/")
    assert on_a_money_path("https://x.test/contact.html")
    assert not on_a_money_path("https://x.test/about-our-book-club")
    assert not on_a_money_path("https://x.test/no-contact-order")
    assert not on_a_money_path("https://x.test/")
    assert not on_a_money_path(None)


# ==========================================================================
# The vocabulary
# ==========================================================================


def test_each_industry_is_written_to_in_its_own_words() -> None:
    """The same finding, five industries, five different second lines.

    Not decoration: a solicitor reading about "treatments" and a gym owner
    reading about "patients" both learn the same thing about who wrote it.
    """
    said = {
        industry: message(
            "broken_internal_link",
            "4 internal page(s) return an error",
            "/book",
            "HTTP 404",
            industry=industry,
        ).body
        for industry in (
            Industry.LAW_FIRM,
            Industry.DENTIST,
            Industry.GYM_FITNESS,
            Industry.MED_SPA,
            Industry.REAL_ESTATE,
        )
    }

    assert "making an enquiry" in said[Industry.LAW_FIRM]
    assert "browsing treatments" in said[Industry.DENTIST]
    assert "try the place or join it" in said[Industry.GYM_FITNESS]
    assert "choosing a treatment" in said[Industry.MED_SPA]
    assert "interested in a property" in said[Industry.REAL_ESTATE]
    assert len({body for body in said.values()}) == len(said)


def test_every_industry_has_a_voice_and_none_falls_through() -> None:
    """Planted violation: add an industry to the enum, forget the vernacular,
    and its whole campaign quietly writes in the general voice."""
    for industry in Industry:
        assert industry in VERNACULARS, industry
        assert vernacular_for(industry).industry is industry


def test_an_unknown_industry_gets_the_general_voice_not_a_crash() -> None:
    general = vernacular_for("something_nobody_defined")

    assert general.industry is Industry.GENERAL
    assert vernacular_for(None).industry is Industry.GENERAL


def test_a_slow_page_is_not_called_a_small_issue() -> None:
    """The industry quality line opens "It is a small technical issue", which is
    true of a missing alt attribute and false of a homepage that takes
    twenty-four seconds. Sending it anyway is how a reader learns the sentence
    was not written about them."""
    body = message(
        "slow_largest_contentful_paint",
        "Main content takes 24.0s to appear",
        "/",
        "LCP 23994ms",
    ).body

    assert "24.0 seconds" in body
    assert "small" not in body


# ==========================================================================
# The four sentences
# ==========================================================================


@pytest.mark.parametrize("issue,title,path,observed", LIVE_FINDINGS)
@pytest.mark.parametrize("industry", list(Industry))
def test_every_real_finding_composes_a_sendable_message(
    issue: str, title: str, path: str, observed: str, industry: Industry
) -> None:
    """Every issue type the detectors produce, in every industry.

    The registers are picked by hashing the lead, so a phrasing that trips a
    rule or misses the word band shows up only for the leads that hash to it --
    an intermittent failure in production and a passing test suite.
    """
    composed = message(issue, title, path, observed, industry=industry)
    result = report(composed)

    assert result.passed, [f"{v.code}: {v.detail}" for v in result.violations]
    assert PITCH_MIN_WORDS <= composed.pitch_words <= PITCH_MAX_WORDS


@pytest.mark.parametrize("seed", ["a", "b", "c", "d", "e", "f", "g", "h"])
def test_no_register_asks_a_stranger_for_a_meeting(seed: str) -> None:
    """ "Worth a short call next week?" was the most common closing line this
    system had. A stranger asking for ten minutes is asking for something
    before giving anything; the meeting request belongs after a reply."""
    body = message(
        "broken_internal_link",
        "4 internal page(s) return an error",
        "/book",
        "HTTP 404",
        seed=seed,
    ).body.lower()

    for ask in ("call next week", "ten minutes", "a quick call", "15 minutes"):
        assert ask not in body


def test_a_word_is_never_doubled_by_a_template() -> None:
    """ "asks for 8 visible fields fields" reached real recipients: the template
    supplied the noun and the observed value already carried it."""
    body = message(
        "high_friction_contact_form",
        "Enquiry form asks for 12 fields",
        "/contact",
        "12 visible fields",
    ).body

    assert "fields fields" not in body
    assert "12 visible fields" in body


def test_the_message_never_says_the_same_thing_twice() -> None:
    """The meta-description observation used to end "so search engines write
    their own summary", and the consequence line then said exactly that
    again."""
    composed = message(
        "missing_meta_description", "Homepage has no meta description", "/", "absent"
    )
    sentences = [line.strip() for line in composed.body.split("\n\n") if line.strip()]

    assert len(sentences) == len(set(sentences))
    assert composed.body.lower().count("write their own summary") == 1


def test_a_name_ending_in_s_takes_a_bare_apostrophe() -> None:
    """ "Hartley & Co Solicitors's consultation page" is not English, and the
    subject line is the one string every recipient reads."""
    composed = compose(
        ComposerContext(
            org_domain="hartley-law.co.uk",
            finding=Finding(
                "broken_internal_link",
                "1 internal page(s) return an error",
                "https://hartley-law.co.uk/consultation",
                "HTTP 404",
            ),
            evidence_ids=["ev-1"],
            owner_name=OWNER,
            portfolio_url=PORTFOLIO,
            mailing_address=ADDRESS,
            unsubscribe_url=f"{PORTFOLIO}/unsubscribe",
            offer_key="intake_automation",
            industry=Industry.LAW_FIRM,
            business_name="Hartley & Co Solicitors",
            # Seeded onto the possessive register specifically.
            variant_seed="lead-1",
        )
    )

    assert "Solicitors's" not in composed.subject
    assert composed.subject[0].isupper()


# ==========================================================================
# What the validator now refuses
# ==========================================================================


def test_a_client_base_that_cannot_be_named_is_refused() -> None:
    """ "mostly for firms your size", "usually for teams around your size", "for
    firms of this size" -- three phrasings of the same move, all of them sent.

    Planted violation: put any of them back in a register and this fails.
    """
    for claim in (
        "Fixing this is what I do, mostly for firms your size.",
        "My work is trial booking, usually for teams around your size.",
        "I build enquiry routing for firms of this size.",
        "I have helped dozens of clinics with exactly this.",
        "I work with businesses like yours on this.",
    ):
        result = validate_message(
            MessageContext(
                subject="A note",
                body=f"Hi there,\n\n{claim}\n\n{OWNER}\n{PORTFOLIO}\n{ADDRESS}\n",
                claim_map=[],
                evidenced_finding_ids=frozenset(),
                sender_name=OWNER,
                portfolio_url=PORTFOLIO,
                mailing_address=ADDRESS,
                unsubscribe_present=True,
            )
        )
        codes = {v.code for v in result.violations}
        assert ViolationCode.UNVERIFIABLE_CLIENTELE in codes, claim


def test_findings_nobody_counted_are_refused() -> None:
    """Three of the four offer registers ended "and I noticed a few other things
    while I was there". That asserts a number of findings, in the one sentence
    the claim map does not check, and the number was never read from
    anything."""
    for claim in (
        "My work is booking fixes, and I noticed a few other things while I was there.",
        "I do intake work, and there were a couple of others worth a look.",
        "There are several other problems on the site.",
    ):
        result = validate_message(
            MessageContext(
                subject="A note",
                body=f"Hi there,\n\n{claim}\n\n{OWNER}\n{PORTFOLIO}\n{ADDRESS}\n",
                claim_map=[],
                evidenced_finding_ids=frozenset(),
                sender_name=OWNER,
                portfolio_url=PORTFOLIO,
                mailing_address=ADDRESS,
                unsubscribe_present=True,
            )
        )
        codes = {v.code for v in result.violations}
        assert ViolationCode.UNCOUNTED_FINDINGS in codes, claim


def test_the_word_band_is_counted_before_the_signature() -> None:
    """The whole-body count is nearly useless as a quality bound: the footer is
    forty words on its own, so a body could pass the floor while saying almost
    nothing, and a 140-word pitch with a paragraph of portfolio history passed
    the ceiling comfortably. Both happened."""
    footer = f"{OWNER}\n{PORTFOLIO}\n{ADDRESS}\nUnsubscribe: {PORTFOLIO}/u\n"
    padded = " ".join(["word"] * 140)

    result = validate_message(
        MessageContext(
            subject="A note",
            body=f"Hi there,\n\n{padded}\n\n{footer}",
            claim_map=[],
            evidenced_finding_ids=frozenset(),
            sender_name=OWNER,
            portfolio_url=PORTFOLIO,
            mailing_address=ADDRESS,
            unsubscribe_present=True,
        )
    )

    codes = {v.code for v in result.violations}
    assert ViolationCode.PITCH_TOO_LONG in codes
    # And the old whole-body ceiling would have let it through.
    assert ViolationCode.TOO_LONG not in codes
