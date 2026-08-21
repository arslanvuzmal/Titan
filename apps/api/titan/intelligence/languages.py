"""Whether a cold email in English is a reasonable thing to send to a country.

The operator's brief said it in four words: *Europe has language problems.* It
does, and the shape of the problem is not the one intuition suggests.

Titan writes English. It writes to **local small businesses** -- dental
practices, med spas, gyms, estate agents -- not to technology companies with
international staff. A message the owner cannot comfortably read is not a weak
message; it is a spam complaint, and complaints are charged to a mailbox that
took three weeks to warm.

**Grounded in a published index, not in a guess.** The EF English Proficiency
Index scores adult English by country and bands it: Very High from 600, High
from 550, Moderate from 500. Checking it corrected two assumptions this module
would otherwise have shipped -- **Romania (605) and Poland (600) band Very
High**, while **Spain (540), France (539) and Italy (513) band Moderate**. The
intuitive ordering is close to backwards.

**What the index is not.** EF scores people who choose to take an online English
test: younger, more urban and more motivated than the population, and much more
so than a fifty-year-old dentist in a market town. So the score is a *ceiling*
on what a cold recipient is likely to manage, not a measurement of it. That is
why the threshold here is the top band rather than a passing grade.

**Absent is not fine.** A country with no published score is excluded rather
than admitted. Slovenia is probably comfortable; nothing here establishes that,
and "we did not check" must not read the same as "we checked and it was fine".

**English-speaking countries are not scored, and neither is the Gulf.** The UK,
USA, Canada, Ireland and Australia need no index. The Gulf states are listed
separately: English is the working language of private healthcare and services
across the UAE, Qatar, Bahrain and Oman, and their clinics advertise in it --
which is a fact about the businesses Titan writes to rather than about the
population, and is stated as its own judgement rather than smuggled in as a
score.
"""

from __future__ import annotations

#: Score at or above which English cold outreach to a local SMB is reasonable.
#:
#: EF's own "Very High" boundary. Deliberately not the passing grade: the index
#: over-represents exactly the people who are *not* the recipient here, so the
#: conservative band is the honest one to spend a mailbox's reputation on.
VERY_HIGH = 600

#: EF EPI 2025 scores, for the countries the territory catalogue actually
#: contains. Not a world list -- adding a territory means adding its country
#: here, and the absence is what stops an unchecked market being written to.
EF_EPI_2025: dict[str, int] = {
    "NL": 624,
    "HR": 617,
    "AT": 616,
    "DE": 615,
    "NO": 613,
    "PT": 612,
    "DK": 611,
    "SE": 609,
    "BE": 608,
    "SK": 606,
    "RO": 605,
    "FI": 603,
    "PL": 600,
    "LV": 599,
    "BG": 594,
    "GR": 592,
    "HU": 590,
    "CZ": 582,
    "CH": 564,
    "EE": 561,
    "ES": 540,
    "FR": 539,
    "LT": 543,
    "IT": 513,
}

#: Countries where English is the language of business, so no index applies.
NATIVE_ENGLISH: frozenset[str] = frozenset({"GB", "UK", "US", "CA", "IE", "AU", "NZ"})

#: Gulf states where English is the working language of the private clinics and
#: services Titan sells to. A judgement about an industry, held apart from the
#: population scores above so the two cannot be mistaken for each other.
ENGLISH_BUSINESS_LANGUAGE: frozenset[str] = frozenset(
    {"AE", "QA", "BH", "OM", "SA", "KW"}
)


def english_score(country_code: str | None) -> int | None:
    """The country's EF EPI score, or None where none applies or is held.

    None is two different facts wearing one value, and the caller must not
    treat either as a pass: a country Titan has not looked up, and a country
    where the index is beside the point because English is the local business
    language. :func:`english_outreach_ok` distinguishes them.
    """
    if not country_code:
        return None
    return EF_EPI_2025.get(country_code.strip().upper())


def english_outreach_ok(country_code: str | None) -> bool:
    """Whether to send an English cold email to a local business here.

    False for an unknown country, on purpose. The failure mode of a wrong
    ``True`` is a complaint against a warmed mailbox; the failure mode of a
    wrong ``False`` is one market unworked while a hundred others are open.
    """
    if not country_code:
        return False
    code = country_code.strip().upper()
    if code in NATIVE_ENGLISH or code in ENGLISH_BUSINESS_LANGUAGE:
        return True
    score = EF_EPI_2025.get(code)
    return score is not None and score >= VERY_HIGH


def why_excluded(country_code: str | None) -> str:
    """A sentence an operator can act on, for a country that did not pass."""
    if not country_code:
        return "no country recorded"
    code = country_code.strip().upper()
    if english_outreach_ok(code):
        return ""
    score = EF_EPI_2025.get(code)
    if score is None:
        return (
            f"{code} has no English proficiency score recorded; not written to "
            "until one is, because unchecked must not read as checked"
        )
    return (
        f"{code} scores {score} on the EF index, below the {VERY_HIGH} band "
        "this uses for cold outreach to local businesses"
    )


__all__ = [
    "EF_EPI_2025",
    "ENGLISH_BUSINESS_LANGUAGE",
    "NATIVE_ENGLISH",
    "VERY_HIGH",
    "english_outreach_ok",
    "english_score",
    "why_excluded",
]
