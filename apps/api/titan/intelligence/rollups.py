"""Delivery outcomes, sliced the six ways decisions are actually made.

``_campaign_outcomes`` answers "how is this campaign doing", which is the
question the orchestrator asks before dispatching. It is not the question
anything *learning* asks. Those are: which mailbox is degrading, which recipient
domain refuses us, which lead source produces bounces, what hour of the
recipient's day gets answered, which phrasing wins. Every one of them is the
same counters grouped differently, and none of them existed.

**One shape, six groupings.** The counters are identical across dimensions on
purpose -- a bounce rate computed one way for senders and another way for
domains would make the two incomparable, and comparing them is the entire point.
The dimensions differ only in what they group by and what they join to reach it.

**Rates are withheld below the sample floor**, the same floor the delivery gate
and the mailbox ramp use. A slice with four sends and one bounce is not a 25%
bounce rate, it is four sends; publishing the number invites acting on it, and
the ranking that results would be sorted mostly by who has the smallest sample.

**A reply is attributed to every slice that contacted the lead.** A lead mailed
on Tuesday and again on Thursday who then replies counts for both slots, because
there is no way to know which message moved them and inventing an attribution
rule would be worse than admitting the ambiguity. It means slice replies sum to
more than distinct replies, which is why the totals are reported per slice and
never added up.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from titan.db.enums import POSITIVE_REPLY_CLASSES
from titan.db.session import WORKSPACE_KEY
from titan.delivery.deliverability import MIN_SAMPLE_FOR_RATES

#: The trailing window every judgement here uses. The same one the campaign
#: manager and the reputation gate use, so a mailbox called unhealthy by one is
#: not called healthy by the other on a different month's data.
DEFAULT_WINDOW_DAYS = 30


class Dimension(enum.StrEnum):
    """What to group by. Each maps to one grouping expression below."""

    CAMPAIGN = "campaign"
    SENDER = "sender"
    RECIPIENT_DOMAIN = "recipient_domain"
    LEAD_SOURCE = "lead_source"
    #: Weekday and hour in the recipient's own clock, not the sender's.
    LOCAL_SLOT = "local_slot"
    VARIANT = "variant"


@dataclass(frozen=True, slots=True)
class Slice:
    """One group's delivery record over the window."""

    dimension: Dimension
    key: str
    label: str
    sent: int
    delivered: int
    bounced: int
    complained: int
    replied: int
    positive_replies: int
    meetings: int

    @property
    def has_signal(self) -> bool:
        """Whether this slice has been measured enough to have a rate at all."""
        return self.sent >= MIN_SAMPLE_FOR_RATES

    def _rate(self, count: int) -> float | None:
        if not self.has_signal or self.sent <= 0:
            return None
        return count / self.sent

    @property
    def bounce_rate(self) -> float | None:
        return self._rate(self.bounced)

    @property
    def reply_rate(self) -> float | None:
        return self._rate(self.replied)

    @property
    def positive_reply_rate(self) -> float | None:
        """The metric the optimiser steers on. A reply that went somewhere."""
        return self._rate(self.positive_replies)

    def describe(self) -> str:
        if not self.has_signal:
            return f"{self.label}: {self.sent} sent -- below the sample floor"
        return (
            f"{self.label}: {self.sent} sent, "
            f"{self.bounce_rate:.1%} bounced, "
            f"{self.positive_reply_rate:.1%} positive"
        )


#: How each dimension reaches its grouping value. ``join`` is spliced into the
#: FROM clause and ``key``/``label`` into the SELECT -- all three are constants
#: in this module, never caller input, which is what keeps this parameterised
#: query parameterised.
_GROUPINGS: dict[Dimension, tuple[str, str, str]] = {
    Dimension.CAMPAIGN: (
        "JOIN campaigns g ON g.id = m.campaign_id",
        "g.id::text",
        "g.name",
    ),
    Dimension.SENDER: (
        "JOIN sender_identities g ON g.id = m.sender_identity_id",
        "g.id::text",
        "g.from_email",
    ),
    Dimension.RECIPIENT_DOMAIN: ("", "m.to_domain", "m.to_domain"),
    Dimension.LEAD_SOURCE: (
        "JOIN leads dl ON dl.id = m.lead_id "
        "JOIN lead_sources g ON g.id = dl.lead_source_id",
        "g.id::text",
        "g.kind || ' / ' || g.label",
    ),
    # Key and label are the same expression here, and the readable form is built
    # in Python by `_relabel`. A literal like ':00' inside `text()` is read as a
    # bind parameter, and escaping it in SQL to produce a string that Python
    # could format more clearly is the wrong trade.
    Dimension.LOCAL_SLOT: (
        "",
        "m.local_sent_weekday::text || '-' || m.local_sent_hour::text",
        "m.local_sent_weekday::text || '-' || m.local_sent_hour::text",
    ),
    Dimension.VARIANT: (
        "JOIN message_drafts g ON g.id = m.draft_id",
        "coalesce(g.variant, 'none')",
        "coalesce(g.variant, 'none')",
    ),
}

#: Slices with nothing to group on are dropped rather than bucketed together.
#: A "null" row mixing every message whose clock could not be resolved is not a
#: time of day, and ranking it against real slots would be comparing a fact to
#: an absence.
_REQUIRED_NOT_NULL: dict[Dimension, str] = {
    Dimension.RECIPIENT_DOMAIN: "m.to_domain IS NOT NULL",
    Dimension.LEAD_SOURCE: "dl.lead_source_id IS NOT NULL",
    Dimension.LOCAL_SLOT: "m.local_sent_hour IS NOT NULL",
}


#: Monday is 0, matching `datetime.weekday()` and the column the outbox writes.
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _relabel(dimension: Dimension, key: str, label: str) -> str:
    """The human-readable form, where SQL is the wrong place to build it."""
    if dimension is not Dimension.LOCAL_SLOT:
        return label
    weekday, _, hour = key.partition("-")
    try:
        return f"{_WEEKDAYS[int(weekday)]} {int(hour):02d}:00"
    except (ValueError, IndexError):
        return label


async def outcomes_by(
    session: AsyncSession,
    dimension: Dimension,
    *,
    now: dt.datetime,
    window_days: int = DEFAULT_WINDOW_DAYS,
    campaign_id: uuid.UUID | None = None,
    limit: int = 200,
) -> list[Slice]:
    """Every group's delivery record over the window, most sent first.

    ``campaign_id`` narrows to one campaign; omitted, this is the workspace.
    """
    join, key_expr, label_expr = _GROUPINGS[dimension]
    since = now - dt.timedelta(days=window_days)

    conditions = [
        # Written out rather than left to row-level security. RLS is permissive
        # while `titan.workspace_id` is unset and the database role owns these
        # tables anyway, so raw SQL that does not name the workspace is not
        # scoped at all -- it only looks as though it is.
        "m.workspace_id = :workspace",
        "m.sent_at IS NOT NULL",
        "m.created_at >= :since",
    ]
    if extra := _REQUIRED_NOT_NULL.get(dimension):
        conditions.append(extra)
    if campaign_id is not None:
        conditions.append("m.campaign_id = :campaign")

    sql = f"""
        SELECT {key_expr} AS key,
               {label_expr} AS label,
               count(*)                                            AS sent,
               count(*) FILTER (WHERE m.delivered_at IS NOT NULL)   AS delivered,
               count(*) FILTER (WHERE m.bounced_at IS NOT NULL)     AS bounced,
               count(*) FILTER (WHERE m.complained_at IS NOT NULL)  AS complained,
               count(DISTINCT l.id) FILTER (
                   WHERE l.replied_at IS NOT NULL
               )                                                    AS replied,
               count(DISTINCT l.id) FILTER (
                   WHERE rc.reply_class = ANY(CAST(:positive AS reply_class[]))
               )                                                    AS positive,
               count(DISTINCT l.id) FILTER (
                   WHERE l.status = 'meeting_booked'
               )                                                    AS meetings
          FROM messages m
          JOIN leads l ON l.id = m.lead_id
          LEFT JOIN inbound_messages im ON im.lead_id = l.id
          LEFT JOIN reply_classifications rc
                 ON rc.inbound_message_id = im.id
          {join}
         WHERE {" AND ".join(conditions)}
         GROUP BY 1, 2
         ORDER BY sent DESC
         LIMIT :limit
    """  # noqa: S608 -- every interpolated fragment is a constant in this module

    params: dict[str, object] = {
        "workspace": session.info.get(WORKSPACE_KEY),
        "since": since,
        "positive": [c.value for c in POSITIVE_REPLY_CLASSES],
        "limit": limit,
    }
    if campaign_id is not None:
        params["campaign"] = campaign_id

    rows = (await session.execute(text(sql), params)).all()
    return [
        Slice(
            dimension=dimension,
            key=str(row.key),
            label=_relabel(dimension, str(row.key), str(row.label)),
            sent=int(row.sent),
            delivered=int(row.delivered),
            bounced=int(row.bounced),
            complained=int(row.complained),
            replied=int(row.replied),
            positive_replies=int(row.positive),
            meetings=int(row.meetings),
        )
        for row in rows
    ]


async def all_dimensions(
    session: AsyncSession,
    *,
    now: dt.datetime,
    window_days: int = DEFAULT_WINDOW_DAYS,
    campaign_id: uuid.UUID | None = None,
) -> dict[Dimension, list[Slice]]:
    """Every slicing, in one call. What the weekly report and the CRM want."""
    return {
        dimension: await outcomes_by(
            session,
            dimension,
            now=now,
            window_days=window_days,
            campaign_id=campaign_id,
        )
        for dimension in Dimension
    }


def worst_by_bounce(slices: list[Slice]) -> Slice | None:
    """The measured slice bouncing most, or None if nothing is measured yet.

    Only slices above the sample floor are eligible, which is the whole point:
    without it this returns whichever group happens to have one send and one
    bounce.
    """
    measured = [s for s in slices if s.has_signal]
    if not measured:
        return None
    return max(measured, key=lambda s: s.bounce_rate or 0.0)


def best_by_positive_reply(slices: list[Slice]) -> Slice | None:
    """The measured slice provoking the most replies that went somewhere."""
    measured = [s for s in slices if s.has_signal]
    if not measured:
        return None
    return max(measured, key=lambda s: s.positive_reply_rate or 0.0)


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "Dimension",
    "Slice",
    "all_dimensions",
    "best_by_positive_reply",
    "outcomes_by",
    "worst_by_bounce",
]
