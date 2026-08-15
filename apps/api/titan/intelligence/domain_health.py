"""What Titan's own sending history says about a recipient domain.

The bounce reduction engine's other layers reason about an address in isolation:
its shape, its domain's reputation in the abstract, what DNS and a verification
service say. This layer reasons about something none of them can see -- what
happened the last time Titan actually wrote to this business.

That is the cheapest evidence available and the only kind nobody can be wrong
about. A verification service can be mistaken about a mailbox. A bounce is a
receiver telling us, in its own words, that we were wrong.

**Computed, never materialised.** The window is a query over ``messages``, not a
counter table kept in step with one. A derived table drifts -- a webhook
processed twice, a backfill, a migration that forgets it -- and a drifted
reputation number is worse than none, because it is wrong in a way nobody
notices. ``titan.delivery.deliverability`` already computes sender reputation
this way over the same table, and this follows it deliberately rather than
inventing a second pattern for the same problem.

**On sample size.** Per-sender windows see hundreds of messages; per-domain
windows see two or three, because ``recipient_domain_daily_limit`` defaults to
2. A rate over three messages is not a rate, so this classifies on absolute
counts near the bottom and only uses rates once there is enough to divide by.
Getting that backwards would condemn a domain on one bounce that was really one
wrong address.

**On hard versus soft.** Titan does not yet distinguish them on the message row:
``bounced_at`` is set for both, and only the suppression path consults
``is_hard_bounce``. So the counter here is named ``bounced`` rather than
``hard_bounced``, which is what it actually holds. That imprecision is the
reason the thresholds below are set where they are, and closing it is the
soft-bounce work that ``REPEATED_SOFT_BOUNCE`` is still waiting for.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Trailing window. Matches the sender reputation window in
#: titan.delivery.deliverability: short enough that a domain recovers after a
#: bad month, long enough that a bad week is not forgotten by Friday.
WINDOW_DAYS = 30

#: Bounces at one domain, with nothing ever delivered there, after which the
#: domain itself is the problem rather than the addresses tried.
#:
#: Three, not two: two bad addresses at one business is an ordinary scrape
#: producing two guesses off the same page. Three with no delivery in between is
#: a domain that does not accept our mail.
BOUNCES_TO_BLOCK = 3

#: Below this, a bounce rate is arithmetic rather than evidence.
MIN_SENDS_FOR_RATE = 4

#: Bounce rate at or above which a domain is degraded, once there are enough
#: sends to compute one.
DEGRADED_BOUNCE_RATE = 0.5


class DomainHealth(StrEnum):
    #: Never sent to. Not a verdict.
    UNKNOWN = "unknown"
    #: Delivered at least once, nothing went wrong.
    HEALTHY = "healthy"
    #: Something went wrong, not enough to act on. Visible, no effect.
    WATCH = "watch"
    #: A real pattern. Sending is still possible but the address is downgraded.
    DEGRADED = "degraded"
    #: Conclusive. Titan stops writing to this domain.
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class DomainWindow:
    """Delivery outcomes for one recipient domain over the trailing window."""

    domain: str
    sent: int = 0
    delivered: int = 0
    #: Hard and soft together -- see the module docstring on why.
    bounced: int = 0
    complained: int = 0

    @property
    def bounce_rate(self) -> float:
        return self.bounced / self.sent if self.sent else 0.0

    @property
    def has_history(self) -> bool:
        return self.sent > 0


def classify(window: DomainWindow) -> DomainHealth:
    """Reduce a domain's history to one verdict.

    Ordered worst-first, so a domain that meets several conditions is reported
    at the most serious one it reaches.
    """
    if not window.has_history:
        return DomainHealth.UNKNOWN

    # A complaint is categorically different from a bounce and is not subject to
    # a sample-size threshold. One is enough, and the arithmetic of "one out of
    # three" is beside the point: somebody at this business marked Titan as
    # spam. Writing to their colleague next is how a complaint becomes a
    # pattern, and how a sending domain gets filtered everywhere.
    if window.complained > 0:
        return DomainHealth.BLOCKED

    if window.bounced >= BOUNCES_TO_BLOCK and window.delivered == 0:
        return DomainHealth.BLOCKED

    if window.sent >= MIN_SENDS_FOR_RATE and window.bounce_rate >= DEGRADED_BOUNCE_RATE:
        return DomainHealth.DEGRADED

    if window.bounced > 0:
        return DomainHealth.WATCH

    if window.delivered > 0:
        return DomainHealth.HEALTHY

    # Sent, but nothing has come back yet -- no delivery confirmation and no
    # failure. Common while a send is in flight, and not evidence of anything.
    return DomainHealth.UNKNOWN


def explain(window: DomainWindow, health: DomainHealth) -> str:
    """One sentence a human can act on, for the signal detail and the UI."""
    if health is DomainHealth.UNKNOWN and not window.has_history:
        return f"no sending history for {window.domain}"
    counts = (
        f"{window.sent} sent, {window.delivered} delivered, "
        f"{window.bounced} bounced, {window.complained} complained "
        f"in the last {WINDOW_DAYS} days"
    )
    if health is DomainHealth.BLOCKED and window.complained > 0:
        return (
            f"somebody at {window.domain} marked Titan as spam ({counts}); "
            "writing to a colleague at the same business is how one complaint "
            "becomes a pattern"
        )
    if health is DomainHealth.BLOCKED:
        return (
            f"{window.domain} has bounced {window.bounced} messages and "
            f"accepted none ({counts}); the domain is the problem, not the "
            "addresses tried"
        )
    if health is DomainHealth.DEGRADED:
        return f"{window.domain} is bouncing {window.bounce_rate:.0%} of mail ({counts})"
    if health is DomainHealth.WATCH:
        return f"{window.domain} has bounced before ({counts})"
    return f"{window.domain}: {counts}"


__all__ = [
    "BOUNCES_TO_BLOCK",
    "DEGRADED_BOUNCE_RATE",
    "MIN_SENDS_FOR_RATE",
    "WINDOW_DAYS",
    "DomainHealth",
    "DomainWindow",
    "classify",
    "explain",
]
