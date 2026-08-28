"""The registry that will not make anything up.

The credibility paragraph is the one place in a cold message where the sender
talks about themselves, and the tempting content for it -- "I rebuilt the
booking system for a dental group in Leeds and enquiries tripled" -- is content
no machine can check. So the module ships empty and stays empty until a human
writes real projects into a file.

These tests hold the two halves of that. Nothing appears without a file, and
what appears from a file is exactly what the file said. They also hold the
constraint that surprised me: an entry has to pass the *message* validator's
claim rule at load time, because "rebuilt **the** booking flow" reads to that
rule as an assertion about the recipient's own booking flow. Catching it here
means an operator gets a warning naming the fix; catching it later means drafts
failing validation for a reason nobody can see.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from titan.intelligence.case_studies import CaseStudy, load, registry, select

GOOD = {
    "reference": "leeds-dental",
    "descriptor": "a two-site dental practice in Leeds",
    "work": "rebuilt a booking flow after a site migration broke it",
    "outcome": "enquiries started arriving again the same week",
    "industries": ["dentist"],
    "families": ["technical"],
    "issue_types": ["broken_primary_cta"],
}


def _file(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    path = tmp_path / "case_studies.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


class TestNothingAppearsFromNothing:
    def test_no_path_is_an_empty_registry(self) -> None:
        assert load(None) == ()
        assert registry(None) == ()

    def test_a_missing_file_is_an_empty_registry(self, tmp_path: Path) -> None:
        assert load(tmp_path / "absent.json") == ()

    def test_unparseable_json_is_an_empty_registry(self, tmp_path: Path) -> None:
        path = tmp_path / "case_studies.json"
        path.write_text("{not json", encoding="utf-8")
        assert load(path) == ()

    def test_a_json_object_instead_of_a_list_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "case_studies.json"
        path.write_text(json.dumps({"reference": "x"}), encoding="utf-8")
        assert load(path) == ()

    def test_one_broken_entry_does_not_discard_the_good_ones(
        self, tmp_path: Path
    ) -> None:
        """A composer that dies on a typo is worse than a weaker paragraph."""
        studies = load(_file(tmp_path, [{"reference": "no-descriptor"}, GOOD]))
        assert [study.reference for study in studies] == ["leeds-dental"]


class TestWhatTheFileSaidIsWhatIsCited:
    def test_the_fields_survive_the_round_trip(self, tmp_path: Path) -> None:
        study = load(_file(tmp_path, [GOOD]))[0]
        assert study.descriptor == "a two-site dental practice in Leeds"
        assert study.industries == frozenset({"dentist"})
        assert study.issue_types == frozenset({"broken_primary_cta"})

    def test_the_sentence_reads_as_written(self, tmp_path: Path) -> None:
        study = load(_file(tmp_path, [GOOD]))[0]
        assert study.sentence() == (
            "I recently rebuilt a booking flow after a site migration broke it "
            "for a two-site dental practice in Leeds; enquiries started "
            "arriving again the same week."
        )

    def test_a_missing_outcome_is_not_invented(self, tmp_path: Path) -> None:
        """"No clean number for that one" is a normal state of affairs."""
        entry = {key: value for key, value in GOOD.items() if key != "outcome"}
        study = load(_file(tmp_path, [entry]))[0]
        assert study.outcome is None
        assert study.sentence().endswith("in Leeds.")


class TestAnEntryMustPassTheSameClaimRuleTheMessageDoes:
    def test_the_definite_article_is_rejected(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """"rebuilt the booking flow" reads as a claim about *their* booking flow."""
        entry = dict(GOOD, work="rebuilt the booking flow after a migration broke it")
        with caplog.at_level(logging.WARNING):
            assert load(_file(tmp_path, [entry])) == ()
        assert "reads as a claim about the recipient" in caplog.text

    def test_the_warning_names_the_fix(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        entry = dict(GOOD, work="rebuilt the booking flow after a migration broke it")
        with caplog.at_level(logging.WARNING):
            load(_file(tmp_path, [entry]))
        assert "Prefer 'a booking flow'" in caplog.text

    def test_the_indefinite_article_passes(self, tmp_path: Path) -> None:
        assert len(load(_file(tmp_path, [GOOD]))) == 1

    def test_the_documented_example_is_itself_valid(self, tmp_path: Path) -> None:
        """The docstring shows an entry. It has to be one that actually loads."""
        documented = {
            "reference": "dental-booking-2025",
            "descriptor": "a two-site dental practice in Leeds",
            "industries": ["dentist"],
            "families": ["technical", "conversion"],
            "issue_types": ["broken_primary_cta"],
            "work": "rebuilt a booking flow after a site migration broke it",
            "outcome": "enquiries arrived again the same week",
            "url": None,
        }
        assert len(load(_file(tmp_path, [documented]))) == 1


class TestChoosingTheMostRelevantOne:
    INDUSTRY_AND_ISSUE = CaseStudy(
        reference="a-both",
        descriptor="a dental practice",
        work="did the work",
        industries=frozenset({"dentist"}),
        issue_types=frozenset({"broken_primary_cta"}),
    )
    INDUSTRY_ONLY = CaseStudy(
        reference="b-industry",
        descriptor="a dental practice",
        work="did the work",
        industries=frozenset({"dentist"}),
    )
    ISSUE_ONLY = CaseStudy(
        reference="c-issue",
        descriptor="a bakery",
        work="did the work",
        issue_types=frozenset({"broken_primary_cta"}),
    )
    FAMILY_ONLY = CaseStudy(
        reference="d-family",
        descriptor="a bakery",
        work="did the work",
        families=frozenset({"technical"}),
    )

    @property
    def all_studies(self) -> tuple[CaseStudy, ...]:
        return (
            self.FAMILY_ONLY,
            self.ISSUE_ONLY,
            self.INDUSTRY_ONLY,
            self.INDUSTRY_AND_ISSUE,
        )

    def test_industry_and_issue_wins(self) -> None:
        chosen = select(
            self.all_studies,
            industry="dentist",
            family="technical",
            issue_type="broken_primary_cta",
        )
        assert chosen is not None and chosen.reference == "a-both"

    def test_the_exact_issue_beats_the_industry_alone(self) -> None:
        chosen = select(
            (self.INDUSTRY_ONLY, self.ISSUE_ONLY),
            industry="dentist",
            family=None,
            issue_type="broken_primary_cta",
        )
        assert chosen is not None and chosen.reference == "c-issue"

    def test_nothing_relevant_returns_nothing(self) -> None:
        """A weaker true paragraph beats a stronger irrelevant one."""
        assert (
            select(
                self.all_studies,
                industry="florist",
                family="search",
                issue_type="missing_meta_description",
            )
            is None
        )

    def test_an_empty_registry_returns_nothing(self) -> None:
        assert select((), industry="dentist", family="technical", issue_type="x") is None

    def test_an_untargeted_entry_matches_nothing(self) -> None:
        """Blank targeting means "I did not say what this is evidence of"."""
        untargeted = CaseStudy(
            reference="anything", descriptor="a business", work="did the work"
        )
        assert (
            select(
                (untargeted,),
                industry="dentist",
                family="technical",
                issue_type="broken_primary_cta",
            )
            is None
        )

    def test_the_choice_is_stable_across_calls(self) -> None:
        """A follow-up citing different previous work reads as a second sender."""
        args = {
            "industry": "dentist",
            "family": "technical",
            "issue_type": "broken_primary_cta",
        }
        first = select(self.all_studies, **args)
        for _ in range(5):
            assert select(self.all_studies, **args) == first

    def test_matching_is_case_insensitive(self) -> None:
        chosen = select(
            self.all_studies, industry="DENTIST", family=None, issue_type=None
        )
        assert chosen is not None
