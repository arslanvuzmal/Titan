"""Template variables: what a finding earns, and what it must not.

The rule under test throughout is that a variable is either traceable to a
verified finding or absent. Most of these tests are refusals, because the
failure that matters here is not a wrong phrase -- it is a confident phrase
about a stranger's business that nothing measured.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy.orm.attributes import set_committed_value
from titan.db.enums import VerificationMethod
from titan.db.models.research import AuditFinding, FindingEvidence
from titan.intelligence import findings as findings_mod
from titan.outreach.variables import (
    _CONSEQUENCE,
    _FRICTION,
    _INSIGHT,
    _SHORT,
    derive_variables,
)

MAPPED = sorted(_CONSEQUENCE)


def finding(
    *,
    issue_type: str = "broken_internal_link",
    confidence: float = 0.95,
    contradicted: bool = False,
    method: VerificationMethod = VerificationMethod.HTTP_RESPONSE,
    with_evidence: bool = True,
) -> AuditFinding:
    """A finding that is pitchable unless the caller breaks exactly one thing."""
    row = AuditFinding(
        issue_type=issue_type,
        confidence=confidence,
        contradicted=contradicted,
        verification_method=method,
    )
    # Assigning to the relationship would make SQLAlchemy try to resolve it.
    set_committed_value(
        row, "evidence_links", [FindingEvidence()] if with_evidence else []
    )
    return row


@pytest.mark.parametrize("issue_type", MAPPED)
def test_every_mapped_issue_type_yields_all_three_required_phrases(
    issue_type: str,
) -> None:
    variables = derive_variables(finding(issue_type=issue_type))
    assert variables.supported
    assert variables.consequence and variables.short and variables.insight


def test_an_unrecognised_issue_type_yields_nothing_rather_than_a_guess() -> None:
    variables = derive_variables(finding(issue_type="a_detector_added_next_week"))
    assert not variables.supported
    assert variables.consequence == ""
    assert variables.short == ""
    assert variables.insight == ""


def test_every_issue_type_the_detectors_produce_can_be_written_about() -> None:
    """A detector with no phrase mapping finds a fault nothing can say out loud.

    That is not a crash, which is why it needs a test: the finding is stored, it
    scores the lead, it shows in the CRM, and then the message step silently
    falls back or refuses. Two of these -- accessibility and load time -- also
    happen to be the only faults that mean the same thing on every kind of site,
    so leaving them unmapped narrows what Titan can talk about the most.
    """
    detectors = Path(findings_mod.__file__).read_text(encoding="utf-8")
    produced = set(re.findall(r'issue_type="([a-z_]+)"', detectors))
    assert produced, "no issue types found; the detector module moved"
    unmapped = sorted(produced - set(_CONSEQUENCE))
    assert unmapped == [], f"detected but unpitchable: {unmapped}"


def test_a_finding_that_is_not_pitchable_yields_nothing() -> None:
    """Whatever the issue type, unverified evidence earns no sentence."""
    assert not derive_variables(finding(contradicted=True)).supported
    assert not derive_variables(finding(confidence=0.5)).supported
    assert not derive_variables(finding(with_evidence=False)).supported
    assert not derive_variables(
        finding(method=VerificationMethod.MODEL_INFERENCE)
    ).supported


def test_the_phrase_maps_cover_exactly_the_same_issue_types() -> None:
    """A type in one map and not another is how a half-filled step ships.

    ``derive_variables`` already refuses that case at runtime, but a mismatch
    here means an issue type silently stopped being pitchable when somebody
    edited one dictionary, which is worth failing the build over.
    """
    assert set(_SHORT) == set(_CONSEQUENCE)
    assert set(_INSIGHT) == set(_CONSEQUENCE)
    assert set(_FRICTION) == set(_CONSEQUENCE)


@pytest.mark.parametrize("issue_type", MAPPED)
def test_the_consequence_reads_as_a_gerund_inside_its_sentence(
    issue_type: str,
) -> None:
    """It fills "could be making {X} harder than it needs to be"."""
    phrase = _CONSEQUENCE[issue_type]
    assert phrase == phrase.lower(), "a mid-sentence phrase must not be capitalised"
    assert not phrase.endswith("."), "a fragment, not a sentence"
    first = phrase.split()[0]
    assert first.endswith("ing"), f"{first!r} does not read as a gerund"


@pytest.mark.parametrize("issue_type", MAPPED)
def test_the_short_phrase_reads_as_a_noun_phrase(issue_type: str) -> None:
    """It fills "One additional thought on {X}:"."""
    phrase = _SHORT[issue_type]
    assert phrase.startswith("the "), f"{phrase!r} does not read as a noun phrase"
    assert not phrase.endswith(".")


@pytest.mark.parametrize("issue_type", MAPPED)
def test_the_insight_carries_no_invented_number(issue_type: str) -> None:
    """Rule 3. No metric, no percentage, no currency about somebody's business."""
    insight = _INSIGHT[issue_type]
    assert not any(ch.isdigit() for ch in insight)
    assert "%" not in insight
    assert not any(sym in insight for sym in ("$", "£", "€"))


def test_the_context_names_the_slots_the_templates_read() -> None:
    context = derive_variables(finding()).as_context()
    assert set(context) == {
        "likely_consequence",
        "verified_finding_short",
        "additional_insight",
        "specific_business_friction",
    }
