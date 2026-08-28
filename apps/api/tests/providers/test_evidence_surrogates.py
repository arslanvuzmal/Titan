"""Scraped text must never be able to fail the crawl save.

The browser worker executes a stranger's JavaScript and hands back whatever
their page contained. Some pages contain unpaired UTF-16 surrogates -- a
truncated emoji, a mangled byte sequence, a CMS that wrote UTF-16 into a UTF-8
document. Python holds such a string quite happily; Postgres does not, and
rejects the entire document with::

    invalid input syntax for type json
    DETAIL: Unicode low surrogate must follow a high surrogate

That fails the *save*, not the crawl. The page was fetched, the evidence
gathered and the crawl paid for, and then ``_persist_crawl`` dies -- identically
on all eight retries, because the bytes never change -- and the lead is thrown
away. Measured on the live workspace before this fix: 100 crashes in 24 hours,
and 6,945 of 14,950 research runs lost to ``Activity task failed``. It was the
single largest source of loss in the system.

The guard lives on ``StrictModel`` rather than at the call site that crashed,
so every model in the contract inherits it. These tests hold it there: one for
the mechanism, and one for each field family a lone surrogate can arrive in,
because a fix applied only to ``text_excerpt`` would simply be re-found later
in ``title``.
"""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest
from titan.contracts.evidence import PageEvidence, scrub_surrogates

#: A lone high surrogate, exactly as a truncated four-byte emoji arrives.
LONE_HIGH = "\ud83d"
#: A lone low surrogate -- the half Postgres names in the error message.
LONE_LOW = "\ude00"


def _page(**overrides: object) -> PageEvidence:
    payload: dict[str, object] = {
        "url": "https://example.test/",
        "final_url": "https://example.test/",
        "captured_at": dt.datetime(2026, 8, 26, tzinfo=dt.UTC),
    }
    payload.update(overrides)
    return PageEvidence.model_validate(payload)


class TestScrubFunction:
    def test_removes_a_lone_high_surrogate(self) -> None:
        assert scrub_surrogates(f"Book{LONE_HIGH} now") == "Book now"

    def test_removes_a_lone_low_surrogate(self) -> None:
        assert scrub_surrogates(f"{LONE_LOW}Clinic") == "Clinic"

    def test_leaves_clean_text_untouched(self) -> None:
        """The fast path must return the original object, not a copy."""
        value = "Zahnärzte München — buchen Sie einen Termin 🦷"
        assert scrub_surrogates(value) is value

    def test_preserves_real_emoji_and_non_ascii(self) -> None:
        """A *paired* surrogate is already one character and must survive."""
        value = "Dentist 😀 — Kraków, Ελλάδα, 日本"
        assert scrub_surrogates(value) == value


class TestContractIngest:
    @pytest.mark.parametrize(
        "field, dirty, clean",
        [
            ("title", f"Smile Clinic{LONE_HIGH}", "Smile Clinic"),
            ("meta_description", f"{LONE_LOW}Book online", "Book online"),
            ("text_excerpt", f"Call us{LONE_HIGH} today", "Call us today"),
            ("lang", f"en{LONE_LOW}", "en"),
        ],
    )
    def test_scalar_fields_are_scrubbed(self, field: str, dirty: str, clean: str) -> None:
        assert getattr(_page(**{field: dirty}), field) == clean

    def test_list_fields_are_scrubbed(self) -> None:
        page = _page(
            headings=[f"Our services{LONE_HIGH}", "Contact"],
            console_errors=[f"Uncaught{LONE_LOW} TypeError"],
        )
        assert page.headings == ["Our services", "Contact"]
        assert page.console_errors == ["Uncaught TypeError"]

    def test_nested_models_are_scrubbed(self) -> None:
        """The recursion has to reach inside nested observations too."""
        page = _page(
            nav_links=[
                {
                    "text": f"Book{LONE_HIGH} an appointment",
                    "href": "https://example.test/book",
                }
            ]
        )
        assert page.nav_links[0].text == "Book an appointment"


#: A ``\uXXXX`` escape naming a code point in the surrogate range. This is the
#: shape Postgres refuses, and writing the check this way rather than as an
#: encode attempt is deliberate -- see :class:`TestSurvivesTheWriteThatUsedToFail`.
SURROGATE_ESCAPE = re.compile(r"\\ud[89ab][0-9a-f]{2}", re.I)


class TestSurvivesTheWriteThatUsedToFail:
    """The actual regression: the dumped payload must reach Postgres intact.

    ``_persist_crawl`` stores ``evidence.model_dump(mode="json")`` in a
    ``jsonb`` column, and the exact failure mode is worth being precise about
    because the obvious test does not reproduce it.

    ``json.dumps`` defaults to ``ensure_ascii=True``, so a lone surrogate is
    emitted as the *seven ASCII characters* ``\\ud83d`` rather than as a
    character that cannot be encoded. The resulting document therefore encodes
    to UTF-8 perfectly well on this side, and is refused on the other by
    Postgres's own JSON parser -- which is why the error we saw in the worker
    log is a Postgres error (``invalid input syntax for type json``) and never
    a Python ``UnicodeEncodeError``.

    So the assertion is about the escape sequence, not about encodability.
    """

    def test_dumped_evidence_carries_no_surrogate_escape(self) -> None:
        page = _page(
            title=f"Praxis{LONE_HIGH}",
            text_excerpt=f"Termin{LONE_LOW} buchen",
            headings=[f"{LONE_HIGH}Leistungen"],
        )
        assert not SURROGATE_ESCAPE.search(json.dumps(page.model_dump(mode="json")))

    def test_an_unscrubbed_payload_would_have_been_refused(self) -> None:
        """Proves the test above is actually testing something.

        This is the document that reached Postgres 100 times in 24 hours.
        """
        assert SURROGATE_ESCAPE.search(json.dumps({"title": f"Praxis{LONE_HIGH}"}))

    def test_real_emoji_still_survive_the_round_trip(self) -> None:
        """The scrub must not be a blunt instrument on legitimate content."""
        page = _page(title="Smile Clinic 🦷 — Zahnärzte")
        restored = json.loads(json.dumps(page.model_dump(mode="json")))
        assert restored["title"] == "Smile Clinic 🦷 — Zahnärzte"
