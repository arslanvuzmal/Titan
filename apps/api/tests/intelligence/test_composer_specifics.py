"""The two facts that turn a generic warning into something only this business
could have received.

The first message this system composed at scale read:

    A quick note about liquidrom-berlin.de -- a navigation link points at a
    page that returns HTTP 404. That matters because visitors following a
    navigation link reach an error page.

Every word of that is true and none of it is persuasive. It was written to the
owner of a spa whose ``/book`` page was down, and the finding had carried that
path all along -- the single most checkable fact available, discarded in favour
of "a navigation link".
"""

from __future__ import annotations

from dataclasses import dataclass

from titan.intelligence.composer import (
    ComposerContext,
    compose,
)


@dataclass
class Finding:
    issue_type: str = "broken_internal_link"
    title: str = "A page returns an error"
    page_url: str | None = "https://example.test/book"
    observed_value: str | None = "HTTP 404"
    business_impact: str | None = "Visitors reach an error page"
    recommended_solution: str | None = "Fix the link"
    id: str = "f1"


def message(**overrides):
    base = {
        "org_domain": "example.test",
        "finding": Finding(),
        "evidence_ids": ["e1"],
        "owner_name": "Owner",
        "portfolio_url": "https://portfolio.test",
        "mailing_address": "An address",
        "unsubscribe_url": "https://portfolio.test/u",
        "solution": "booking fixes",
        "variant_seed": "lead-1",
    }
    base.update(overrides)
    return compose(ComposerContext(**base))


# ------------------------------------------------------- naming the page


def test_the_message_names_the_page() -> None:
    """Planted violation: put "a navigation link" back and this fails."""
    assert "/book" in message().body


def test_the_subject_names_the_page() -> None:
    """ "A broken link" could have been about any site on the internet."""
    assert "/book" in message().subject


def test_a_root_page_is_named_as_the_home_page() -> None:
    """ "your / page" is not English."""
    body = message(finding=Finding(page_url="https://example.test/")).body

    assert "your home page" in body
    assert "your / page" not in body


def test_a_missing_url_does_not_produce_a_dangling_sentence() -> None:
    body = message(finding=Finding(page_url=None)).body

    assert "your  page" not in body
    assert "None" not in body


def test_query_strings_are_not_read_out() -> None:
    body = message(
        finding=Finding(page_url="https://example.test/book?utm_source=x#form")
    ).body

    assert "/book" in body
    assert "utm_source" not in body
