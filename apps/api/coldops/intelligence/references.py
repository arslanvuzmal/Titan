"""Where a claim comes from, listed at the end of the message.

The observation paragraph says what is wrong with *this* site and is backed by
crawl evidence. The consequence paragraph says what that costs a business, and
until now it was backed by nothing a reader could check -- it was the sender's
assertion, in the sender's voice, about the reader's money.

A references section closes that gap the way any other serious document closes
it: the standard being cited is named, and the reader can go and read it. It
also does something a cold approach badly needs, which is to change the
register of the message from sales to assessment.

**These are curated, never generated.** Every URL here is a stable primary
source -- the body that publishes the standard, not somebody's summary of it.
An LLM asked for "a source about page speed" will produce a plausible URL that
404s, and a broken citation is worse than no citation: it converts the one
paragraph designed to establish credibility into evidence of carelessness.
``scripts/check_references.py`` walks this table against the live web so a link
that rots is found by us rather than by a recipient.

**A missing entry is a missing section, not an invented one.** An issue type
with no reference renders no references block at all.

Titles are also written to avoid pairing an article with a site noun ("the
rest of the page"). The citation block unwraps into a single sentence, and the
claim validator reads any such pairing as an assertion about the recipient that
must appear in the claim map -- so a title phrased that way would drag every
citation into the claim map with it. ``test_composer_four_part.py`` holds this
by composing every issue type through the real validator.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Reference:
    """One citation: who says it, and where they say it."""

    #: The publisher, as a reader would recognise them. "Google Search Central",
    #: not "developers.google.com" -- the authority is the point of the line.
    publisher: str
    #: What the page is about, in the reader's terms rather than the spec's.
    title: str
    url: str

    def render(self) -> str:
        return f"{self.title} -- {self.publisher}"


#: Primary sources per issue type.
#:
#: At most two each. A list of five reads as padding and nobody opens any of
#: them; two reads as a person who checked. Where both a standards body and a
#: search engine have something to say, the standards body goes first.
REFERENCES: dict[str, tuple[Reference, ...]] = {
    "broken_primary_cta": (
        Reference(
            "MDN Web Docs",
            "What a 404 response means",
            "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status/404",
        ),
        Reference(
            "Google Search Central",
            "Redirects and how search handles them",
            "https://developers.google.com/search/docs/crawling-indexing/301-redirects",
        ),
    ),
    "broken_internal_link": (
        Reference(
            "MDN Web Docs",
            "What a 404 response means",
            "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status/404",
        ),
        Reference(
            "Google Search Central",
            "Redirects and how search handles them",
            "https://developers.google.com/search/docs/crawling-indexing/301-redirects",
        ),
    ),
    "high_friction_contact_form": (
        Reference(
            "Nielsen Norman Group",
            "Why form length costs completions",
            "https://www.nngroup.com/articles/web-form-design/",
        ),
    ),
    "no_visible_phone_number": (
        Reference(
            "MDN Web Docs",
            "Making a phone number tappable on mobile",
            "https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/a",
        ),
    ),
    "no_booking_or_enquiry_path": (
        Reference(
            "Nielsen Norman Group",
            "Why form length costs completions",
            "https://www.nngroup.com/articles/web-form-design/",
        ),
    ),
    "slow_largest_contentful_paint": (
        Reference(
            "Google web.dev",
            "Largest Contentful Paint and the 2.5 second threshold",
            "https://web.dev/articles/lcp",
        ),
        Reference(
            "Google web.dev",
            "Core Web Vitals, and what search does with them",
            "https://web.dev/articles/vitals",
        ),
    ),
    "images_missing_alt_text": (
        Reference(
            "W3C Web Accessibility Initiative",
            "Text alternatives for images",
            "https://www.w3.org/WAI/tutorials/images/",
        ),
    ),
    "serious_accessibility_violations": (
        Reference(
            "W3C Web Accessibility Initiative",
            "WCAG 2.2 success criteria",
            "https://www.w3.org/WAI/WCAG22/quickref/",
        ),
        Reference(
            "W3C Web Accessibility Initiative",
            "Accessibility law by country",
            "https://www.w3.org/WAI/policies/",
        ),
    ),
    "javascript_console_errors": (
        # The label used to read "How one failing script halts everything after
        # it" over a link to MDN's `console` object -- an API reference whose
        # own summary is "provides access to the debugging console". It said
        # nothing about scripts halting. In a message whose entire premise is
        # that every claim is checkable, a citation that does not support its
        # own label is worse than no citation: the one reader who follows it is
        # the one who was taking us seriously.
        Reference(
            "MDN Web Docs",
            "JavaScript errors, and what each one means",
            "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Errors",
        ),
    ),
    "failed_network_requests": (
        Reference(
            "MDN Web Docs",
            "HTTP response codes and what each one means",
            "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status",
        ),
    ),
    "missing_meta_description": (
        Reference(
            "Google Search Central",
            "How search builds the snippet under your name",
            "https://developers.google.com/search/docs/appearance/snippet",
        ),
    ),
    "no_structured_data": (
        Reference(
            "Google Search Central",
            "Local business structured data",
            "https://developers.google.com/search/docs/appearance/structured-data/local-business",
        ),
        Reference(
            "Schema.org",
            "The LocalBusiness vocabulary",
            "https://schema.org/LocalBusiness",
        ),
    ),
    "missing_mobile_viewport": (
        Reference(
            "MDN Web Docs",
            "The viewport meta tag, and what a phone does without one",
            "https://developer.mozilla.org/en-US/docs/Web/HTML/Guides/Viewport_meta_element",
        ),
    ),
    "missing_security_headers": (
        Reference(
            "OWASP",
            "The response headers a browser expects",
            # The OWASP project page this used to point at now answers 404.
            # Checked 2026-09-10, along with every other reference here.
            "https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html",
        ),
    ),
}


def references_for(issue_type: str) -> tuple[Reference, ...]:
    """Citations for an issue type, or nothing.

    Nothing is a real answer: it renders no section rather than a section with
    a plausible-looking link nobody verified.
    """
    return REFERENCES.get(issue_type, ())


def all_references() -> tuple[Reference, ...]:
    """Every distinct citation, for the link checker."""
    seen: dict[str, Reference] = {}
    for group in REFERENCES.values():
        for reference in group:
            seen.setdefault(reference.url, reference)
    return tuple(seen.values())


__all__ = ["REFERENCES", "Reference", "all_references", "references_for"]
