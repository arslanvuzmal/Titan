"""What to search for, once the map runs out.

Five active campaigns had spent every territory their region has and were
re-asking the same five questions every hour -- 286 runs in seven days, $18.30,
one lead admitted. Territory rotation was working correctly; there was simply
nowhere left for it to go.

But a dentist campaign that has worked twenty UK cities looking for "dentists"
has never asked any of them about "orthodontists" or "emergency dentists".
Different businesses, the same playbook, the same offers. The ground was not
worked out; one question about it was.

These hold the catalogue's shape and the rotation order. The shape matters more
than it looks: a term listed under the wrong industry gets a playbook whose
offers do not fit the business, and the message that produces is worse than no
message.
"""

from __future__ import annotations

import pytest
from titan.db.enums import Industry
from titan.intelligence.playbooks import PLAYBOOKS
from titan.intelligence.verticals import (
    VERTICALS,
    catalogue_size,
    next_vertical,
    verticals_for,
)


class TestTheCatalogue:
    def test_every_industry_listed_has_a_playbook(self) -> None:
        """A vertical without a playbook produces no offer, so no draft.

        ``select_offers`` returns nothing for an industry it does not know, and
        the drafting activity refuses rather than falling back to a generic
        pitch. Searching for such a business is paying to discover leads that
        can never be written to.
        """
        for industry in VERTICALS:
            assert industry in PLAYBOOKS, industry

    def test_general_is_not_a_search_target(self) -> None:
        """GENERAL is the fallback playbook, not a kind of business."""
        assert Industry.GENERAL not in VERTICALS
        assert verticals_for(Industry.GENERAL) == ()

    def test_the_industries_with_measured_yield_are_all_covered(self) -> None:
        """The six that have produced leads on this workspace."""
        for industry in (
            Industry.LAW_FIRM,
            Industry.GYM_FITNESS,
            Industry.MED_SPA,
            Industry.DENTIST,
            Industry.REAL_ESTATE,
            Industry.HVAC_HOME_SERVICES,
        ):
            assert len(verticals_for(industry)) >= 5, industry

    def test_no_term_appears_under_two_industries(self) -> None:
        """A term in two lists is a term whose playbook depends on which
        campaign happened to search it."""
        seen: dict[str, Industry] = {}
        for industry, terms in VERTICALS.items():
            for term in terms:
                key = term.casefold()
                assert key not in seen, f"{term}: {seen.get(key)} and {industry}"
                seen[key] = industry

    def test_no_duplicates_within_an_industry(self) -> None:
        for industry, terms in VERTICALS.items():
            lowered = [t.casefold() for t in terms]
            assert len(lowered) == len(set(lowered)), industry

    def test_terms_are_lower_case_and_trimmed(self) -> None:
        """They are interpolated into a Places query and compared casefolded
        against stored labels; a stray capital or space is a term that never
        matches its own exhaustion record."""
        for terms in VERTICALS.values():
            for term in terms:
                assert term == term.strip()
                assert term == term.lower()

    def test_the_head_of_each_list_is_the_broadest_term(self) -> None:
        """Rotation takes the first unspent term, so order is priority."""
        assert VERTICALS[Industry.DENTIST][0] == "dentists"
        assert VERTICALS[Industry.LAW_FIRM][0] == "law firms"
        assert VERTICALS[Industry.GYM_FITNESS][0] == "gyms"

    def test_the_catalogue_is_materially_bigger_than_one_term_each(self) -> None:
        """The whole point. Twelve terms was 1,284 possible searches against
        107 territories; this is what makes 250 contacts a day sustainable."""
        assert catalogue_size() >= 80
        assert catalogue_size() > len(VERTICALS) * 5


class TestRotation:
    def test_it_starts_at_the_top(self) -> None:
        assert next_vertical(Industry.LAW_FIRM, exhausted=set()) == "law firms"

    def test_it_skips_what_is_spent(self) -> None:
        assert (
            next_vertical(
                Industry.DENTIST, exhausted={"dentists"}, current="dentists"
            )
            == "dental clinics"
        )

    def test_it_skips_the_current_term_even_if_not_marked_spent(self) -> None:
        """The caller only reaches here because the current term ran out of
        territories; returning it again would loop."""
        assert next_vertical(Industry.DENTIST, exhausted=set(), current="dentists") != (
            "dentists"
        )

    def test_matching_ignores_case(self) -> None:
        """The spent set is built from stored query labels; one capital letter
        must not resurrect a finished term."""
        assert (
            next_vertical(Industry.DENTIST, exhausted={"DENTISTS"}, current=None)
            != "dentists"
        )

    def test_everything_spent_returns_nothing(self) -> None:
        """A real answer -- "widen the catalogue" rather than "search again"."""
        spent = {t.casefold() for t in VERTICALS[Industry.OPTICIAN]}
        assert next_vertical(Industry.OPTICIAN, exhausted=spent) is None

    def test_an_unknown_industry_returns_nothing(self) -> None:
        assert next_vertical(Industry.GENERAL, exhausted=set()) is None
        assert next_vertical(None, exhausted=set()) is None

    def test_it_walks_the_whole_list_and_then_stops(self) -> None:
        spent: set[str] = set()
        seen: list[str] = []
        while (term := next_vertical(Industry.VETERINARY, exhausted=spent)) is not None:
            seen.append(term)
            spent.add(term.casefold())
        assert seen == list(VERTICALS[Industry.VETERINARY])

    @pytest.mark.parametrize("industry", sorted(VERTICALS, key=str))
    def test_every_industry_can_be_walked_to_exhaustion(
        self, industry: Industry
    ) -> None:
        """No industry loops forever, and none stops early."""
        spent: set[str] = set()
        for _ in range(len(VERTICALS[industry])):
            term = next_vertical(industry, exhausted=spent)
            assert term is not None
            spent.add(term.casefold())
        assert next_vertical(industry, exhausted=spent) is None
