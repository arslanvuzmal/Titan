"""The weekly report: what happened, and what needs a person.

Pure. Takes counts, returns a judgement and some prose. Kept separate from the
queries that gather them so the thresholds below -- which are the part that has
to be *right* -- can be tested against a table of numbers rather than a
database.

**Attention first, activity second.** A report that opens with "42 messages
sent" trains the reader to skim, because the number is the same most weeks and
carries no decision. Opening with the three prospects who said yes and are still
waiting, or with a bounce rate that will cost the sending domain, gives them
something to *do* -- and the volume numbers are still underneath for whoever
wants them.

The deliverability thresholds are not invented. Gmail's bulk-sender rules put
the complaint ceiling at 0.30% and ask senders to stay under 0.10%; a bounce
rate above 2% is the point at which most providers start throttling, and 5% is
where reputation damage becomes hard to undo. Those are the numbers below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

#: Complaint rate at which Gmail's bulk-sender policy is breached outright.
COMPLAINT_RATE_CRITICAL = 0.003
#: The rate Gmail asks senders to stay beneath. Crossing it is a warning, not a
#: breach -- but it is the last quiet moment before one.
COMPLAINT_RATE_WARNING = 0.001

#: Above this, providers begin throttling and reputation starts to erode.
BOUNCE_RATE_WARNING = 0.02
#: Above this, the damage takes months of careful sending to undo.
BOUNCE_RATE_CRITICAL = 0.05

#: Below this many sends, a rate is noise. One bounce in six sends is 17% and
#: means nothing; reporting it as critical would train the reader to ignore the
#: line that matters later.
MIN_VOLUME_FOR_RATES = 20


class Health(StrEnum):
    GOOD = "good"
    WARNING = "warning"
    CRITICAL = "critical"
    #: Too little volume to say anything honest.
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True, slots=True)
class DeliverabilityHealth:
    status: Health
    bounce_rate: float
    complaint_rate: float
    detail: str

    @property
    def needs_action(self) -> bool:
        return self.status in (Health.WARNING, Health.CRITICAL)


@dataclass(frozen=True, slots=True)
class WeeklyReport:
    workspace_name: str
    period_start: str
    period_end: str

    # --- what needs a person -------------------------------------------
    #: Prospects who said yes and have an open task. The only decaying item.
    awaiting_reply: int = 0
    replies_needing_reading: int = 0
    drafts_awaiting_approval: int = 0
    stalled_campaigns: int = 0
    #: Somebody asked for a call and no time has been set. Every meeting starts
    #: this way -- Titan does not parse a time out of a reply -- so this is a
    #: standing item rather than an exception, and it is the one on the list that
    #: has a person waiting on the other end of it.
    meetings_unscheduled: int = 0

    # --- what happened --------------------------------------------------
    leads_discovered: int = 0
    leads_researched: int = 0
    messages_sent: int = 0
    delivered: int = 0
    bounced: int = 0
    complained: int = 0
    replies_received: int = 0
    positive_replies: int = 0
    declined: int = 0
    suppressions_added: int = 0
    #: Conversations that reached a request for a call. The closest thing in
    #: this report to an outcome.
    meetings_proposed: int = 0
    #: Deliverable opportunities identified this week, and what they would be
    #: worth if every one of them closed. Reported as a ceiling, never as a
    #: forecast -- see :func:`render`.
    opportunities_identified: int = 0
    pipeline_value_usd: float = 0.0

    health: DeliverabilityHealth | None = None
    #: Named leads worth chasing, most recent first.
    hot_leads: tuple[str, ...] = field(default_factory=tuple)
    #: (region, detail) per market, busiest first. The layer above campaigns:
    #: forty campaign rows are not a portfolio, they are the same problem in a
    #: longer form.
    portfolio: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    #: What the recipient's own week says about when to write. One line:
    #: almost every slot is below the sample floor and saying so at length
    #: would fill the report with the word unknown.
    timing: str = ""
    #: (label, grade, detail) per discovery batch, worst first. Graded rather
    #: than merely counted: lead_sources already records what a search cost and
    #: how many rows it returned, and neither says whether the leads were good.
    lead_sources: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)

    @property
    def reply_rate(self) -> float:
        return self.replies_received / self.messages_sent if self.messages_sent else 0.0

    @property
    def positive_rate(self) -> float:
        return self.positive_replies / self.messages_sent if self.messages_sent else 0.0

    @property
    def needs_attention(self) -> int:
        return (
            self.awaiting_reply
            + self.replies_needing_reading
            + self.drafts_awaiting_approval
            + self.stalled_campaigns
            + self.meetings_unscheduled
            + (1 if self.health and self.health.needs_action else 0)
        )


def assess_deliverability(
    *, sent: int, bounced: int, complained: int
) -> DeliverabilityHealth:
    """Judge sending health against the thresholds providers actually enforce.

    Returns INSUFFICIENT_DATA below :data:`MIN_VOLUME_FOR_RATES` rather than
    computing a rate from a handful of sends. A single bounce out of six is not
    a 17% bounce rate in any useful sense, and reporting it as critical is how a
    reader learns to ignore this line before the week it is true.
    """
    if sent < MIN_VOLUME_FOR_RATES:
        return DeliverabilityHealth(
            status=Health.INSUFFICIENT_DATA,
            bounce_rate=0.0,
            complaint_rate=0.0,
            detail=(
                f"{sent} message(s) sent -- too few to read a rate from. "
                f"Rates are reported from {MIN_VOLUME_FOR_RATES} sends."
            ),
        )

    bounce_rate = bounced / sent
    complaint_rate = complained / sent

    if complaint_rate >= COMPLAINT_RATE_CRITICAL:
        return DeliverabilityHealth(
            Health.CRITICAL,
            bounce_rate,
            complaint_rate,
            f"Complaint rate {complaint_rate:.2%} is at or above Gmail's 0.30% "
            "ceiling. Pause sending on this domain and find out what is being "
            "sent to whom before anything else.",
        )
    if bounce_rate >= BOUNCE_RATE_CRITICAL:
        return DeliverabilityHealth(
            Health.CRITICAL,
            bounce_rate,
            complaint_rate,
            f"Bounce rate {bounce_rate:.2%} is above 5%. The address list needs "
            "verifying before the next send; damage at this level takes months "
            "of careful sending to undo.",
        )
    if complaint_rate >= COMPLAINT_RATE_WARNING:
        return DeliverabilityHealth(
            Health.WARNING,
            bounce_rate,
            complaint_rate,
            f"Complaint rate {complaint_rate:.2%} is above the 0.10% Gmail asks "
            "senders to stay under. Not a breach yet -- this is the quiet moment "
            "before one.",
        )
    if bounce_rate >= BOUNCE_RATE_WARNING:
        return DeliverabilityHealth(
            Health.WARNING,
            bounce_rate,
            complaint_rate,
            f"Bounce rate {bounce_rate:.2%} is above 2%, where providers start "
            "throttling. Worth checking how the newest addresses were sourced.",
        )
    return DeliverabilityHealth(
        Health.GOOD,
        bounce_rate,
        complaint_rate,
        f"Bounce {bounce_rate:.2%}, complaints {complaint_rate:.2%}. Both well "
        "inside provider limits.",
    )


def render(report: WeeklyReport) -> str:
    """Plain text, because it is read in a task list and in a chat window.

    No table alignment and no colour: both survive exactly one of those places.
    """
    lines: list[str] = [
        f"Titan weekly report -- {report.workspace_name}",
        f"{report.period_start} to {report.period_end}",
        "",
    ]

    # ---- needs you -----------------------------------------------------
    if report.needs_attention:
        lines.append("NEEDS YOU")
        if report.awaiting_reply:
            lines.append(
                f"  {report.awaiting_reply} prospect(s) said yes and are waiting "
                "on a reply. These go cold fastest."
            )
        if report.meetings_unscheduled:
            lines.append(
                f"  {report.meetings_unscheduled} call(s) requested with no time "
                "set. Titan does not guess a time out of a reply, so somebody has "
                "to confirm the slot."
            )
        if report.replies_needing_reading:
            lines.append(
                f"  {report.replies_needing_reading} repl(ies) the rules could not "
                "read confidently."
            )
        if report.drafts_awaiting_approval:
            lines.append(
                f"  {report.drafts_awaiting_approval} draft(s) awaiting approval. "
                "They expire."
            )
        if report.stalled_campaigns:
            lines.append(
                f"  {report.stalled_campaigns} campaign(s) had budget and nothing "
                "to work on -- usually discovery has run dry."
            )
        if report.health and report.health.needs_action:
            lines.append(f"  Deliverability: {report.health.detail}")
        lines.append("")
    else:
        lines.append("Nothing needs you this week.")
        lines.append("")

    # ---- hot leads -----------------------------------------------------
    if report.hot_leads:
        lines.append("WORTH CHASING")
        lines.extend(f"  {name}" for name in report.hot_leads)
        lines.append("")

    # ---- the markets -----------------------------------------------------
    if report.portfolio:
        lines.append("PORTFOLIO")
        for region, detail in report.portfolio:
            lines.append(f"  {region}: {detail}")
        lines.append("")

    # ---- when to write ---------------------------------------------------
    if report.timing:
        lines.append("TIMING")
        lines.append(f"  {report.timing}")
        lines.append("")

    # ---- where the leads came from --------------------------------------
    if report.lead_sources:
        lines.append("LEAD SOURCES")
        for label, grade, detail in report.lead_sources:
            lines.append(f"  [{grade}] {label}: {detail}")
        lines.append("")

    # ---- the numbers ---------------------------------------------------
    lines.append("THIS WEEK")
    lines.append(
        f"  Leads: {report.leads_discovered} discovered, "
        f"{report.leads_researched} researched"
    )
    lines.append(
        f"  Sent: {report.messages_sent}   delivered: {report.delivered}   "
        f"bounced: {report.bounced}   complaints: {report.complained}"
    )
    lines.append(
        f"  Replies: {report.replies_received} "
        f"({report.reply_rate:.1%} of sent) -- {report.positive_replies} positive, "
        f"{report.declined} declined"
    )
    if report.meetings_proposed:
        lines.append(f"  Calls requested: {report.meetings_proposed}")
    if report.opportunities_identified:
        # "if every one closed", not "pipeline worth". The number is a sum of
        # catalogue prices for work nobody has agreed to buy, and stating it as
        # a forecast would be the one dishonest line in an evidence-first report.
        lines.append(
            f"  Opportunities: {report.opportunities_identified} identified, "
            f"${report.pipeline_value_usd:,.0f} if every one closed"
        )
    if report.suppressions_added:
        lines.append(
            f"  Suppressed: {report.suppressions_added} address(es) added to the "
            "do-not-contact list"
        )

    if report.health and report.health.status is not Health.INSUFFICIENT_DATA:
        lines.append("")
        lines.append(
            f"Deliverability: {report.health.status.value} -- {report.health.detail}"
        )
    elif report.health:
        lines.append("")
        lines.append(report.health.detail)

    # The honest closing line. A reply rate is the only number here that says
    # whether the messages are any good; volume says only that the machine ran.
    if report.messages_sent >= MIN_VOLUME_FOR_RATES:
        lines.append("")
        lines.append(
            f"Reply rate {report.reply_rate:.1%}. That is the number worth moving "
            "-- volume only says the machine ran."
        )

    return "\n".join(lines)


def headline(report: WeeklyReport) -> str:
    """One line, for a notification title and a chat push."""
    if report.health and report.health.status is Health.CRITICAL:
        return f"Weekly report: deliverability needs attention ({report.workspace_name})"
    if report.awaiting_reply:
        return (
            f"Weekly report: {report.awaiting_reply} prospect(s) waiting on you "
            f"({report.workspace_name})"
        )
    if report.needs_attention:
        return f"Weekly report: {report.needs_attention} item(s) need you"
    return (
        f"Weekly report: {report.messages_sent} sent, {report.replies_received} repl(ies)"
    )


__all__ = [
    "BOUNCE_RATE_CRITICAL",
    "BOUNCE_RATE_WARNING",
    "COMPLAINT_RATE_CRITICAL",
    "COMPLAINT_RATE_WARNING",
    "MIN_VOLUME_FOR_RATES",
    "DeliverabilityHealth",
    "Health",
    "WeeklyReport",
    "assess_deliverability",
    "headline",
    "render",
]
