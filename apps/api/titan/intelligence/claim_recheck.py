"""Asking whether a claim is still true, just before Titan asserts it.

The send gate asks how *old* a measurement is and never whether it still
holds. So a business that fixed its booking page a fortnight ago was still
being told it was broken, and ``audit_findings.contradicted`` -- the column
that exists for precisely this -- was read in two places and written in none.

**Only claims that can be checked cheaply are checked at all.** A finding that
a page returns 404, or that a call to action leads nowhere, is one HTTP
request to settle. A finding about alt text, console errors or a Lighthouse
score is not, and pretending otherwise would put a full crawl on the send path.
So this covers the URL-status family -- which is also, by a wide margin, what
Titan actually leads with: 305 of the first 413 messages opened with a broken
link on a booking or contact path.

**The asymmetry is the whole design.** A probe that comes back clean is
believed and the claim is withdrawn; a probe that comes back inconclusive
changes nothing and the message goes as written. That is deliberate and it is
the same rule :mod:`titan.intelligence.smtp_probe` applies to mailboxes: a
wrong "still broken" costs one false claim to one business, while a wrong "now
fixed" silently discards a real lead and nothing downstream would ever show it
happened. Only a positive, conclusive contradiction stops a send.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from titan.providers.browser_client import RecheckResult

logger = logging.getLogger(__name__)

#: Issue types whose truth is a URL's HTTP status, and nothing else.
#:
#: Each of these asserts "this address does not work". That is settled by
#: asking the address. Everything absent from this set -- alt text, console
#: errors, headers, structured data, paint timings -- needs the page parsed or
#: the browser instrumented, which is a crawl, and a crawl does not belong on
#: the send path.
URL_STATUS_CLAIMS: frozenset[str] = frozenset(
    {
        "broken_internal_link",
        "broken_primary_cta",
    }
)

#: Below this, the address works. A claim that it is broken is withdrawn.
#:
#: 400 rather than 300: a redirect that lands somewhere real is a working link,
#: and the probe follows redirects before reporting a status.
OK_BELOW = 400


class Verdict(StrEnum):
    #: The defect is still there. Send as written.
    CONFIRMED = "confirmed"
    #: The business has fixed it. Do not assert it.
    CONTRADICTED = "contradicted"
    #: Nothing was learned. Send as written -- see the module docstring.
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class ClaimCheck:
    verdict: Verdict
    detail: str

    @property
    def blocks_send(self) -> bool:
        return self.verdict is Verdict.CONTRADICTED


def is_recheckable(issue_type: str, page_url: str | None) -> bool:
    """Whether this claim can be settled by asking one URL."""
    return bool(page_url) and issue_type in URL_STATUS_CLAIMS


def judge(
    *, issue_type: str, observed: RecheckResult, claimed_status: int | None = None
) -> ClaimCheck:
    """Compare what the page does now against what the message says about it.

    ``claimed_status`` is what the crawl recorded, when it recorded one. It is
    not required: the claim these findings make is "this address does not
    work", so a working address contradicts it whatever the original number
    was.
    """
    if issue_type not in URL_STATUS_CLAIMS:
        return ClaimCheck(
            Verdict.INCONCLUSIVE,
            f"{issue_type} is not settled by an HTTP status",
        )

    if not observed.allowed:
        # The URL guard refusing to fetch it says something about our own
        # safety rules, not about the recipient's site.
        return ClaimCheck(
            Verdict.INCONCLUSIVE,
            f"not re-checked: {observed.blocked_reason or 'url guard refused'}",
        )

    if observed.error:
        return ClaimCheck(Verdict.INCONCLUSIVE, f"probe failed: {observed.error}")

    if observed.status is None:
        # The probe ran and could not get an answer. Two attempts, a real
        # browser, and still nothing -- which is what a site behind a hostile
        # bot filter looks like, and is not evidence that anything was fixed.
        return ClaimCheck(
            Verdict.INCONCLUSIVE, "the page did not answer; nothing was learned"
        )

    if observed.status < OK_BELOW:
        was = f" (was {claimed_status})" if claimed_status else ""
        return ClaimCheck(
            Verdict.CONTRADICTED,
            f"{observed.url} now returns {observed.status}{was}; "
            "the defect this message describes has been fixed",
        )

    return ClaimCheck(
        Verdict.CONFIRMED,
        f"{observed.url} still returns {observed.status}",
    )


__all__ = [
    "OK_BELOW",
    "URL_STATUS_CLAIMS",
    "ClaimCheck",
    "Verdict",
    "is_recheckable",
    "judge",
]
