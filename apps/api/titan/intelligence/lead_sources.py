"""How a batch of discovered leads actually turned out.

``lead_sources`` records what a search cost and how many rows it returned. Both
are inputs. Neither says whether the leads were any good, so a query that
reliably produces businesses with no published contact address, or addresses
that bounce, costs the same as one that produces meetings and looks identical in
the table.

Two things are being graded here and they are not the same question:

* **Safety** -- did writing to these leads damage the sending domain? Bounces
  and complaints, which are the expensive kind of bad.
* **Performance** -- did anyone reply? Cheap to measure, slow to accumulate, and
  the only one that speaks to whether the search was pointed at the right
  businesses.

A source can be perfectly safe and useless: every address deliverable, nobody
interested. It is graded down for that, but far less sharply than for bouncing,
because the costs are not comparable. A quiet week wastes research budget; a
bouncing batch spends sending reputation that took months to build and is shared
by every other campaign.

**Contactability is counted separately and early.** The share of leads that ever
yielded an eligible address is knowable within minutes of discovery, long before
any send, and it is the fastest signal that a search is pointed at the wrong
kind of business. It is also the one number here that costs nothing to be wrong
about -- no mail was sent either way.

**Nothing here acts.** Grading is reported; deciding what to do about a bad
source -- stop buying from it, shift budget to a better one -- is the campaign
manager's job, and doing it silently from inside a reporting module would be an
autonomous decision nobody asked for and nobody could see.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Leads below which no rate is reported as a grade. Discovery batches are
#: small -- a search returning twenty businesses is normal -- so this is much
#: lower than a sender's sample floor, and the grades stay advisory in
#: consequence.
MIN_LEADS_TO_GRADE = 10

#: Sends below which the safety half is not graded, whatever the lead count. A
#: batch can hold fifty leads and have had two messages sent from it.
MIN_SENDS_TO_GRADE_SAFETY = 5

#: Bounce rate at or above which a source is refused outright. Deliberately far
#: above the sender-level pause threshold: this is a judgement about a batch of
#: addresses, not about a mailbox's standing with receivers, and one bad batch
#: should be visible long before it is fatal.
BOUNCE_RATE_POOR = 0.15
BOUNCE_RATE_WATCH = 0.05

#: Share of leads that must yield an eligible contact address for a search to be
#: worth repeating. Below a third, most of what it returns cannot be written to
#: at all and the research spend is going nowhere.
CONTACTABILITY_POOR = 0.33
CONTACTABILITY_WATCH = 0.6


class SourceGrade(StrEnum):
    #: Too little has happened to say anything.
    UNKNOWN = "unknown"
    #: Safe and producing replies.
    STRONG = "strong"
    #: Safe, and nothing has come back yet.
    STEADY = "steady"
    #: Something is off: low contactability, or bounces starting to show.
    WATCH = "watch"
    #: Bouncing, complained about, or almost nothing reachable.
    POOR = "poor"


@dataclass(frozen=True, slots=True)
class LeadSourceWindow:
    """Everything downstream of one discovery batch."""

    source_id: str
    kind: str
    label: str = ""
    cost_usd: float = 0.0

    leads: int = 0
    #: Leads that ended up with an address eligible to receive outreach.
    contactable: int = 0
    #: Leads that actually received at least one message. Distinct from
    #: ``sent``, which counts messages: one lead can be written to four times.
    contacted: int = 0
    sent: int = 0
    delivered: int = 0
    bounced: int = 0
    complained: int = 0
    replied: int = 0

    @property
    def contactability(self) -> float:
        return self.contactable / self.leads if self.leads else 0.0

    @property
    def bounce_rate(self) -> float:
        """Per message. A bounce is a property of a send, not of a person."""
        return self.bounced / self.sent if self.sent else 0.0

    @property
    def reply_rate(self) -> float:
        """Per lead contacted, not per message sent.

        Dividing replies by messages would make a source look worse the more
        follow-ups it received, which is a property of the sequence rather than
        of the search. A person replies once however many times they were asked.
        """
        return self.replied / self.contacted if self.contacted else 0.0

    @property
    def cost_per_contactable(self) -> float | None:
        """What a usable lead cost. None when none were usable.

        Deliberately not zero: a batch that produced nothing reachable has an
        undefined cost per lead, and reporting 0.00 would read as free.
        """
        return self.cost_usd / self.contactable if self.contactable else None

    @property
    def cost_per_reply(self) -> float | None:
        return self.cost_usd / self.replied if self.replied else None

    @property
    def has_enough_leads(self) -> bool:
        return self.leads >= MIN_LEADS_TO_GRADE

    @property
    def has_enough_sends(self) -> bool:
        return self.sent >= MIN_SENDS_TO_GRADE_SAFETY


def classify(window: LeadSourceWindow) -> SourceGrade:
    """Reduce a batch's outcomes to one grade, worst condition first.

    Safety is checked before performance and on its own sample floor, so a
    source that has bounced badly across a handful of sends is not rescued by
    having also produced a reply.
    """
    if not window.has_enough_leads:
        return SourceGrade.UNKNOWN

    # A complaint is not subject to a rate. Somebody the search found marked
    # Titan as spam, which says the search is finding the wrong people.
    if window.complained > 0:
        return SourceGrade.POOR

    if window.has_enough_sends and window.bounce_rate >= BOUNCE_RATE_POOR:
        return SourceGrade.POOR
    if window.contactability < CONTACTABILITY_POOR:
        return SourceGrade.POOR

    if window.has_enough_sends and window.bounce_rate >= BOUNCE_RATE_WATCH:
        return SourceGrade.WATCH
    if window.contactability < CONTACTABILITY_WATCH:
        return SourceGrade.WATCH

    if window.replied > 0:
        return SourceGrade.STRONG
    if window.sent > 0:
        return SourceGrade.STEADY
    # Contactable leads exist but none have been written to yet. The safety
    # half is still unmeasured, and calling that steady would overstate it.
    return SourceGrade.UNKNOWN


def explain(window: LeadSourceWindow, grade: SourceGrade) -> str:
    """One line an operator can act on."""
    if grade is SourceGrade.UNKNOWN:
        if not window.has_enough_leads:
            return (
                f"{window.leads} lead(s) so far -- too few to judge "
                f"(needs {MIN_LEADS_TO_GRADE})"
            )
        return f"{window.contactable} contactable lead(s), none written to yet"

    parts = [
        f"{window.contactability:.0%} contactable",
        f"{window.sent} sent",
    ]
    if window.sent:
        parts.append(f"{window.bounce_rate:.0%} bounced")
        parts.append(f"{window.replied} repl(ies)")
    if window.complained:
        parts.append(f"{window.complained} complaint(s)")

    detail = ", ".join(parts)
    if grade is SourceGrade.POOR and window.complained:
        return f"{detail} -- somebody this search found reported Titan as spam"
    if grade is SourceGrade.POOR and window.contactability < CONTACTABILITY_POOR:
        return f"{detail} -- most of what it returns has no address to write to"
    return detail


def rank(windows: list[LeadSourceWindow]) -> list[tuple[LeadSourceWindow, SourceGrade]]:
    """Graded, worst first.

    Worst first because the list is read to find what to stop doing. A ranking
    that opened with the best performer would bury the batch that is costing
    reputation at the bottom of the page.
    """
    order = {
        SourceGrade.POOR: 0,
        SourceGrade.WATCH: 1,
        SourceGrade.UNKNOWN: 2,
        SourceGrade.STEADY: 3,
        SourceGrade.STRONG: 4,
    }
    graded = [(w, classify(w)) for w in windows]
    return sorted(
        graded,
        key=lambda pair: (order[pair[1]], -pair[0].bounce_rate, pair[0].label),
    )


def roll_up(windows: list[LeadSourceWindow], kind: str) -> LeadSourceWindow:
    """Every batch of one kind, added together.

    An individual search returns a handful of businesses and rarely clears the
    sample floor on its own. The kind -- Places, a CSV import, a referral -- is
    where enough accumulates to say something, and it is also the level at which
    an operator can actually change anything.
    """
    of_kind = [w for w in windows if w.kind == kind]
    return LeadSourceWindow(
        source_id=f"kind:{kind}",
        kind=kind,
        label=kind,
        cost_usd=sum(w.cost_usd for w in of_kind),
        leads=sum(w.leads for w in of_kind),
        contactable=sum(w.contactable for w in of_kind),
        contacted=sum(w.contacted for w in of_kind),
        sent=sum(w.sent for w in of_kind),
        delivered=sum(w.delivered for w in of_kind),
        bounced=sum(w.bounced for w in of_kind),
        complained=sum(w.complained for w in of_kind),
        replied=sum(w.replied for w in of_kind),
    )


__all__ = [
    "BOUNCE_RATE_POOR",
    "BOUNCE_RATE_WATCH",
    "CONTACTABILITY_POOR",
    "CONTACTABILITY_WATCH",
    "MIN_LEADS_TO_GRADE",
    "MIN_SENDS_TO_GRADE_SAFETY",
    "LeadSourceWindow",
    "SourceGrade",
    "classify",
    "explain",
    "rank",
    "roll_up",
]
