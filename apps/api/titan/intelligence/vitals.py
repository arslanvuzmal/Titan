"""The six numbers that say whether the machine is alive, and the alarms on them.

Every fault found in this system so far was found by somebody going looking.
None of them announced itself, because each individual component was behaving
exactly as written while the whole had stopped:

* research failed 1,819 times against 2,005 starts in a day -- the crawler
  correctly reported saturation, the workflow correctly caught the error, and
  the sweeper correctly cleaned up after it;
* sends fell 36, 18, 5 over three days;
* the only mailbox with volume was paused for a month;
* Gemini was switched on in ``.env`` and off in every container, and the
  ``model_runs`` table held zero rows for the system's entire life.

The notification layer needed for all of this already existed and was good --
durable row first, push second, deduplicated. What it had no vocabulary for was
the pipeline itself. Every kind in :class:`~titan.notify.operator.
NotificationKind` describes a *lead*, a *campaign* or a *mailbox*; none of them
can say "the machine stopped".

**Vitals are computed, never stored.** A materialised health table is a second
account of the same facts, free to drift from the first, and the moment it
drifts it is worse than nothing because it looks authoritative. Everything here
is a query against the rows that already exist.

**Rates need a floor before they are evidence.** Two failed research runs out of
three is not a 67% failure rate, and paging on it teaches an operator to ignore
the pager. Below the sample floor the rate is ``None`` -- "not measured", which
is a different fact from "measured and healthy" and must not be rendered as
zero.

**An alarm names what to do.** "Research failure rate 91%" is a reading.
"Research is failing; the crawler is saturated or unreachable" is a diagnosis,
and the second one is what gets somebody to the right place at 2am.
"""

from __future__ import annotations

import datetime as dt
import itertools
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import HUMAN_REPLY_CLASSES
from titan.db.models import (
    ContactChannel,
    CrawlRun,
    InboundMessage,
    Message,
    ReplyClassification,
    ResearchRun,
    SenderIdentity,
)
from titan.intelligence.fuel import _reachable_untouched_query

# ==========================================================================
# Thresholds
# ==========================================================================

#: Research runs needed in the window before a failure rate means anything.
#: Matches the sample discipline used for reply and bounce rates.
MIN_RESEARCH_SAMPLE = 50

#: Failure share that indicates the crawl path is broken rather than merely
#: encountering difficult sites. Some failure is normal -- sites time out, block
#: robots, or resolve to nothing. A third is not normal.
RESEARCH_FAILURE_ALARM = 0.30

#: How far back to count research outcomes. Long enough to clear the sample
#: floor at a modest rate, short enough that yesterday's outage does not page
#: anyone today.
RESEARCH_WINDOW = dt.timedelta(hours=6)

#: Days of remaining reachable leads below which the pipeline is running dry.
#: Three rather than one: an alarm that fires on the last day is a report, not a
#: warning, and refilling takes more than a day.
RUNWAY_ALARM_DAYS = 3.0

#: Consecutive strictly-falling days of sending that count as a decline.
#:
#: Three, and each day must have sent something. Requiring a non-zero start is
#: what keeps weekends out of it: campaigns run Monday to Friday, so a Saturday
#: of zero is the schedule working rather than a fault, and a rule that could
#: not tell the difference would page every Monday morning.
DECLINE_DAYS = 3

#: Hour (UTC) after which a day with capacity, fuel and no sends is a fault
#: rather than a morning. Every market's send window has opened by now, and the
#: earliest -- the Gulf -- is more than half over.
IDLE_ALARM_HOUR_UTC = 14


# ==========================================================================
# The reading
# ==========================================================================


@dataclass(frozen=True, slots=True)
class Vitals:
    """One workspace's pulse. Every field is counted, none is remembered."""

    #: Leads with a usable address that have never been written to.
    leads_in_hand: int
    #: What every active mailbox is between them allowed to send today.
    daily_send_capacity: int
    #: Mailboxes that could send right now, out of those configured active.
    mailboxes_sending: int
    mailboxes_active: int

    crawls_today: int
    addresses_found_today: int
    sends_today: int
    human_replies_this_week: int

    #: Research outcomes over :data:`RESEARCH_WINDOW`, for the failure rate.
    research_finished: int
    research_failed: int

    #: Sends per day, oldest first, for the decline check. Excludes today, which
    #: is still being written and would read as a fall every morning.
    sends_by_day: tuple[int, ...] = field(default=())

    #: Whether the sending provider still accepts Titan's credentials.
    #:
    #: ``None`` means nobody asked, which must not alarm -- a check that could
    #: not run is not evidence of failure. ``False`` means it was asked and
    #: refused, and that is the most serious thing this module can report:
    #: every other alarm here describes the pipeline working badly, and this one
    #: means nothing will leave at all.
    sending_provider_ok: bool | None = None

    #: Whether the configured model routes still answer a real call.
    #:
    #: ``None`` means nobody asked. ``False`` means a route was called and did
    #: not answer, and unlike every other field here that is a fault which
    #: *hides itself*: the rewriter, the reply classifier and the contact
    #: finder all catch their own failure and carry on with a degraded result,
    #: by design, because a bad minute at a third party must not fail a draft.
    #: The cost of that design is that a permanent failure looks exactly like a
    #: bad minute. Between 26 August and 10 September every model route was
    #: dead -- two retired on the same morning, one account at zero credits,
    #: one free tier gone paid, one overloaded -- and the pipeline reported
    #: itself healthy for fifteen days while every inbound reply was filed
    #: ``UNKNOWN``. This is the field that makes that visible.
    model_routes_ok: bool | None = None
    #: Which routes failed and why, for the alarm to quote. Empty when nothing
    #: was asked or everything answered.
    model_routes_detail: str = ""

    now: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    # ---------------------------------------------------------- derived
    @property
    def days_of_fuel(self) -> float | None:
        """How long the reachable leads last at today's send rate.

        ``None`` when nothing can send: the reserve is not being consumed, so
        the question does not apply. Reporting zero days would read as a fire
        at the exact moment there is no fire.
        """
        if self.daily_send_capacity <= 0:
            return None
        return self.leads_in_hand / self.daily_send_capacity

    @property
    def research_failure_rate(self) -> float | None:
        """``None`` below the sample floor -- not measured, not healthy."""
        if self.research_finished < MIN_RESEARCH_SAMPLE:
            return None
        return self.research_failed / self.research_finished

    @property
    def sends_are_declining(self) -> bool:
        """Strictly falling for :data:`DECLINE_DAYS`, from a non-zero start."""
        if len(self.sends_by_day) < DECLINE_DAYS:
            return False
        run = self.sends_by_day[-DECLINE_DAYS:]
        if any(day == 0 for day in run):
            # A day that sent nothing is either the schedule or a stoppage, and
            # this alarm is about neither. Campaigns run Monday to Friday, so a
            # weekend inside the run makes "36, 25, 0" look like collapse when
            # it is Friday, Saturday; and a genuine stop to zero is reported by
            # ``idle_with_capacity``, which can say why. What is left here is
            # erosion: still sending, sending less every day, nothing broken
            # enough for anything else to notice.
            return False
        return all(later < earlier for earlier, later in itertools.pairwise(run))


# ==========================================================================
# The alarms
# ==========================================================================


@dataclass(frozen=True, slots=True)
class Alarm:
    """Something worth waking somebody for, and what to do about it."""

    #: Stable identifier, and the dedupe key's discriminator. An operator
    #: filtering on one of these should get every instance of that fault.
    code: str
    title: str
    #: The diagnosis and the next step, not just the reading.
    detail: str


def check(vitals: Vitals) -> list[Alarm]:
    """Every alarm the vitals justify, most urgent first.

    Ordered rather than scored. A scored list needs weights nobody can defend
    and hides the ordering rule inside arithmetic; an explicit sequence is
    arguable, which is the point.
    """
    alarms: list[Alarm] = []

    # ---- nothing can leave the building at all ---------------------------
    # First, because it outranks every other fault here. The others describe a
    # pipeline working badly; this one means no mail goes out however well
    # everything upstream is running.
    if vitals.sending_provider_ok is False:
        alarms.append(
            Alarm(
                code="sending_provider_rejected",
                title="The sending provider is refusing Titan's credentials",
                detail=(
                    "Every send will fail until this is resolved, whatever the "
                    "rest of the pipeline is doing.\n\n"
                    "Usually the account rather than the key: an expired plan, "
                    "a lapsed trial, or a failed payment. A rotated or revoked "
                    "key looks identical from here. Check the provider's "
                    "billing page first, then the key in the environment."
                ),
            )
        )

    # ---- the thinking has stopped, quietly -------------------------------
    # Second, and above every pipeline alarm below it, because this is the only
    # fault here that does not show up in any number on the dashboard. Sends,
    # crawls and addresses all keep moving with the model layer dead; what
    # stops is the judgement -- replies stop being classified, contacts stop
    # being found, and messages go out in the deterministic words. Nothing on
    # this page falls, so nothing here would ever have said so.
    if vitals.model_routes_ok is False:
        alarms.append(
            Alarm(
                code="model_routes_dead",
                title="No model route is answering",
                detail=(
                    "Drafting still works and mail still goes out -- every "
                    "caller degrades on purpose rather than failing a draft -- "
                    "but nothing is being judged: inbound replies are filed "
                    "UNKNOWN, no new contacts are found, and messages send in "
                    "the deterministic wording.\n\n"
                    "Model routes expire without warning and several usually "
                    "expire together: a provider retires a model, an account "
                    "runs out of credit, a free tier goes paid. Run `titan "
                    "validate-models` for the per-route reason, then change "
                    "TITAN_MODEL_ROUTE_* to something that answers.\n\n"
                    f"{vitals.model_routes_detail}".rstrip()
                ),
            )
        )

    # ---- the machine has stopped producing -------------------------------
    rate = vitals.research_failure_rate
    if rate is not None and rate >= RESEARCH_FAILURE_ALARM:
        alarms.append(
            Alarm(
                code="research_failing",
                title=f"Research is failing {rate:.0%} of the time",
                detail=(
                    f"{vitals.research_failed} of {vitals.research_finished} runs "
                    f"failed in the last {int(RESEARCH_WINDOW.total_seconds() // 3600)} "
                    "hours. Some failure is normal -- sites time out or refuse "
                    "robots -- but a third is not.\n\n"
                    "Usually the browser worker: saturated, unreachable, or "
                    "being ordered more work than it can clear. Check its "
                    "in-flight count and the worker log for 'saturated'."
                ),
            )
        )

    # ---- nothing can send at all -----------------------------------------
    if vitals.mailboxes_active > 0 and vitals.mailboxes_sending == 0:
        alarms.append(
            Alarm(
                code="no_sending_capacity",
                title="Every mailbox is blocked",
                detail=(
                    f"All {vitals.mailboxes_active} active mailboxes are refusing "
                    "to send. Nothing will leave until one recovers.\n\n"
                    "Usually a reputation pause after bounces, or lapsed "
                    "authentication. A reputation pause clears on its own as the "
                    "window ages; failing DNS does not."
                ),
            )
        )

    # ---- capacity and fuel, and still nothing went out -------------------
    elif (
        vitals.now.hour >= IDLE_ALARM_HOUR_UTC
        and vitals.daily_send_capacity > 0
        and vitals.leads_in_hand > 0
        and vitals.sends_today == 0
    ):
        alarms.append(
            Alarm(
                code="idle_with_capacity",
                title="Nothing sent today, with capacity and leads available",
                detail=(
                    f"{vitals.daily_send_capacity} sends were allowed and "
                    f"{vitals.leads_in_hand} leads are reachable, and none left.\n\n"
                    "The gap is between an approved draft and the outbox. Check "
                    "for approved drafts with no outbox row, and for campaigns "
                    "outside their send window."
                ),
            )
        )

    # ---- running dry ------------------------------------------------------
    runway = vitals.days_of_fuel
    if runway is not None and runway < RUNWAY_ALARM_DAYS:
        alarms.append(
            Alarm(
                code="runway_short",
                title=f"{runway:.1f} days of leads left",
                detail=(
                    f"{vitals.leads_in_hand} reachable leads against "
                    f"{vitals.daily_send_capacity} sends a day. Below "
                    f"{RUNWAY_ALARM_DAYS:.0f} days there is no margin for a bad "
                    "discovery run.\n\n"
                    "Either discovery has stopped finding businesses, or research "
                    "is not turning them into addresses. Today: "
                    f"{vitals.crawls_today} crawls produced "
                    f"{vitals.addresses_found_today} addresses."
                ),
            )
        )

    # ---- the slow fade ----------------------------------------------------
    if vitals.sends_are_declining:
        run = ", ".join(str(n) for n in vitals.sends_by_day[-DECLINE_DAYS:])
        alarms.append(
            Alarm(
                code="sends_declining",
                title=f"Sending has fallen {DECLINE_DAYS} days running",
                detail=(
                    f"Daily sends: {run}. Each day lower than the last.\n\n"
                    "This is the shape every stoppage in this system has taken, "
                    "and it is visible days before anything reports itself "
                    "broken. Compare capacity against what actually left."
                ),
            )
        )

    return alarms


# ==========================================================================
# Reading it from the database
# ==========================================================================


def _midnight(now: dt.datetime) -> dt.datetime:
    return dt.datetime.combine(now.date(), dt.time.min, tzinfo=dt.UTC)


async def read_vitals(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    daily_send_capacity: int,
    mailboxes_sending: int,
    sending_provider_ok: bool | None = None,
    model_routes_ok: bool | None = None,
    model_routes_detail: str = "",
    now: dt.datetime | None = None,
    history_days: int = 7,
) -> Vitals:
    """Count everything. Capacity is passed in because only the outbox worker's
    adaptive limits know what a mailbox may actually send today.
    """
    moment = now or dt.datetime.now(dt.UTC)
    today = _midnight(moment)

    # The same definition the research budget spends money against, imported
    # rather than restated. Two counts of "a usable lead" that were free to
    # disagree would eventually disagree, and the dashboard would then be
    # arguing with the planner about whether to buy more.
    leads_in_hand = (
        await session.execute(_reachable_untouched_query(workspace_id))
    ).scalar_one()

    crawls_today = (
        await session.execute(
            select(func.count())
            .select_from(CrawlRun)
            .where(CrawlRun.workspace_id == workspace_id, CrawlRun.created_at >= today)
        )
    ).scalar_one()

    addresses_today = (
        await session.execute(
            select(func.count())
            .select_from(ContactChannel)
            .where(
                ContactChannel.workspace_id == workspace_id,
                ContactChannel.channel_type == "email",
                ContactChannel.created_at >= today,
            )
        )
    ).scalar_one()

    sends_today = (
        await session.execute(
            select(func.count())
            .select_from(Message)
            .where(
                Message.workspace_id == workspace_id,
                Message.sent_at >= today,
            )
        )
    ).scalar_one()

    replies_this_week = (
        await session.execute(
            select(func.count())
            .select_from(ReplyClassification)
            .join(
                InboundMessage,
                InboundMessage.id == ReplyClassification.inbound_message_id,
            )
            .where(
                ReplyClassification.workspace_id == workspace_id,
                ReplyClassification.reply_class.in_(list(HUMAN_REPLY_CLASSES)),
                InboundMessage.received_at >= moment - dt.timedelta(days=7),
            )
        )
    ).scalar_one()

    # Research outcomes. 'failed' and 'abandoned' are both the run not arriving;
    # 'abandoned' is the sweeper's verdict on one that stopped without saying so,
    # and counting only the first would understate the failure by exactly the
    # runs nobody was told about.
    finished_since = moment - RESEARCH_WINDOW
    outcomes = (
        await session.execute(
            select(ResearchRun.status, func.count())
            .where(
                ResearchRun.workspace_id == workspace_id,
                ResearchRun.finished_at >= finished_since,
            )
            .group_by(ResearchRun.status)
        )
    ).all()
    counts = {status: int(n) for status, n in outcomes}
    research_finished = sum(counts.values())
    research_failed = counts.get("failed", 0) + counts.get("abandoned", 0)

    # Sends per day, today excluded: a day still being written reads as a fall
    # every morning, and an alarm that fires every morning is furniture.
    rows = (
        await session.execute(
            select(
                func.date(func.timezone("UTC", Message.sent_at)).label("d"),
                func.count(),
            )
            .where(
                Message.workspace_id == workspace_id,
                Message.sent_at >= today - dt.timedelta(days=history_days),
                Message.sent_at < today,
            )
            .group_by("d")
            .order_by("d")
        )
    ).all()
    sends_by_day = tuple(int(n) for _, n in rows)

    mailboxes_active = (
        await session.execute(
            select(func.count())
            .select_from(SenderIdentity)
            .where(
                SenderIdentity.workspace_id == workspace_id,
                SenderIdentity.is_active.is_(True),
            )
        )
    ).scalar_one()

    return Vitals(
        leads_in_hand=int(leads_in_hand),
        daily_send_capacity=daily_send_capacity,
        mailboxes_sending=mailboxes_sending,
        mailboxes_active=int(mailboxes_active),
        crawls_today=int(crawls_today),
        addresses_found_today=int(addresses_today),
        sends_today=int(sends_today),
        human_replies_this_week=int(replies_this_week),
        research_finished=research_finished,
        research_failed=research_failed,
        sends_by_day=sends_by_day,
        sending_provider_ok=sending_provider_ok,
        model_routes_ok=model_routes_ok,
        model_routes_detail=model_routes_detail,
        now=moment,
    )


def render(vitals: Vitals) -> str:
    """The six numbers, for a terminal or a log line.

    Fixed width and fixed order so two readings a day apart can be compared by
    eye without either being parsed.
    """
    runway = vitals.days_of_fuel
    rate = vitals.research_failure_rate
    return "\n".join(
        [
            f"  leads in hand       {vitals.leads_in_hand:>6}",
            "  days of fuel        "
            + (f"{runway:>6.1f}" if runway is not None else "     -  (nothing sending)"),
            f"  crawls today        {vitals.crawls_today:>6}",
            f"  addresses today     {vitals.addresses_found_today:>6}",
            f"  sends today         {vitals.sends_today:>6}"
            f"  of {vitals.daily_send_capacity} allowed",
            f"  replies this week   {vitals.human_replies_this_week:>6}  (human)",
            "",
            "  research failures   "
            + (
                f"{rate:>5.0%}  of {vitals.research_finished} runs"
                if rate is not None
                else f"     -  only {vitals.research_finished} runs, too few to say"
            ),
            f"  mailboxes sending   {vitals.mailboxes_sending:>6}"
            f"  of {vitals.mailboxes_active} active",
            "  sending provider    "
            + (
                "     -  not checked"
                if vitals.sending_provider_ok is None
                else ("    ok" if vitals.sending_provider_ok else " REFUSING CREDENTIALS")
            ),
            "  model routes        "
            + (
                "     -  not checked"
                if vitals.model_routes_ok is None
                else ("    ok" if vitals.model_routes_ok else " NONE ANSWERING")
            ),
        ]
    )


__all__ = [
    "DECLINE_DAYS",
    "IDLE_ALARM_HOUR_UTC",
    "MIN_RESEARCH_SAMPLE",
    "RESEARCH_FAILURE_ALARM",
    "RESEARCH_WINDOW",
    "RUNWAY_ALARM_DAYS",
    "Alarm",
    "Vitals",
    "check",
    "read_vitals",
    "render",
]
