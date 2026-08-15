"""Which hours and days of somebody's week are worth writing in.

The question is ordinary and the data to answer it did not exist. Every send
timestamp Titan holds is UTC, which says what time it was here; the recipient's
local hour is now stamped on the message at send time, and this is what reads it
back.

**A slot is a weekday and an hour in the recipient's week**, not in ours. Tuesday
at nine means nine o'clock wherever they are, so a London and a Los Angeles
business that both read their mail first thing land in the same slot rather than
eight hours apart.

**Almost every slot has too little in it.** A working week is forty-five slots
and cold outreach reply rates are single-digit percentages, so a campaign
sending forty a day fills each slot with about four messages a month and sees a
reply in one slot in five. Ranking those would be ranking noise, confidently.
Every judgement here is therefore gated on a sample floor, and a slot below it
is reported as unknown rather than as average -- those are different claims and
only one of them is true.

**Nothing acts on this yet, deliberately.** The column it reads was added in the
same change, so there is no history at all and will not be for weeks. Wiring a
window-narrowing actuator now would be building a mechanism whose input is
guaranteed empty, and whose first non-empty input would arrive unobserved. It
reports; acting is a decision to make once there is something to act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Messages in one slot before its reply rate is worth reading. Forty is roughly
#: a month of one campaign's sending at a single hour, and it is the point at
#: which one reply either way stops moving the rate by more than it means.
MIN_SENDS_PER_SLOT = 40

#: Slots that must clear the floor before the *set* is ranked at all. Comparing
#: two slots is only meaningful against a spread, and a spread of two points is
#: not one.
MIN_SLOTS_TO_RANK = 4

#: How far from the overall reply rate a slot must sit to be called anything.
#: A fifth better or worse: smaller differences are inside the noise a
#: forty-message sample carries.
MATERIAL_DIFFERENCE = 0.2

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class SlotVerdict(StrEnum):
    #: Not enough messages in this slot to say anything.
    UNKNOWN = "unknown"
    STRONG = "strong"
    TYPICAL = "typical"
    WEAK = "weak"


@dataclass(frozen=True, slots=True)
class Slot:
    """One hour of one weekday, in the recipient's local time."""

    weekday: int
    hour: int

    def __str__(self) -> str:
        return f"{WEEKDAY_NAMES[self.weekday]} {self.hour:02d}:00"


@dataclass(frozen=True, slots=True)
class SlotOutcome:
    slot: Slot
    sent: int = 0
    replied: int = 0

    @property
    def reply_rate(self) -> float:
        return self.replied / self.sent if self.sent else 0.0

    @property
    def has_signal(self) -> bool:
        return self.sent >= MIN_SENDS_PER_SLOT


@dataclass(frozen=True, slots=True)
class TimingReport:
    """What the week looks like, and how much of it is actually known."""

    outcomes: tuple[SlotOutcome, ...] = ()
    baseline: float = 0.0
    #: Slots that cleared the sample floor. The denominator of every claim here.
    judged: int = 0

    @property
    def has_enough_to_rank(self) -> bool:
        return self.judged >= MIN_SLOTS_TO_RANK

    @property
    def total_sent(self) -> int:
        return sum(o.sent for o in self.outcomes)

    def verdict_for(self, outcome: SlotOutcome) -> SlotVerdict:
        if not outcome.has_signal or not self.has_enough_to_rank:
            return SlotVerdict.UNKNOWN
        if not self.baseline:
            return SlotVerdict.TYPICAL
        delta = (outcome.reply_rate - self.baseline) / self.baseline
        if delta >= MATERIAL_DIFFERENCE:
            return SlotVerdict.STRONG
        if delta <= -MATERIAL_DIFFERENCE:
            return SlotVerdict.WEAK
        return SlotVerdict.TYPICAL

    def ranked(self) -> list[tuple[SlotOutcome, SlotVerdict]]:
        """Judged slots, best reply rate first. Unknown slots are excluded.

        Excluded rather than sorted to the bottom: a slot with four messages in
        it has a reply rate, and putting it in a ranked list invites somebody to
        read the number.
        """
        if not self.has_enough_to_rank:
            return []
        judged = [o for o in self.outcomes if o.has_signal]
        return sorted(
            ((o, self.verdict_for(o)) for o in judged),
            key=lambda pair: (
                -pair[0].reply_rate,
                pair[0].slot.weekday,
                pair[0].slot.hour,
            ),
        )

    def best(self, limit: int = 3) -> list[SlotOutcome]:
        return [o for o, v in self.ranked() if v is SlotVerdict.STRONG][:limit]

    def worst(self, limit: int = 3) -> list[SlotOutcome]:
        weak = [o for o, v in self.ranked() if v is SlotVerdict.WEAK]
        return list(reversed(weak))[:limit]


def learn(outcomes: list[SlotOutcome]) -> TimingReport:
    """Read a week of slots and say what, if anything, is known about it.

    The baseline is the reply rate across *judged* slots only. Computing it over
    everything would let a hundred barely-used slots drag the average toward
    zero and make every well-used slot look strong by comparison.
    """
    judged = [o for o in outcomes if o.has_signal]
    sent = sum(o.sent for o in judged)
    replied = sum(o.replied for o in judged)
    return TimingReport(
        outcomes=tuple(outcomes),
        baseline=replied / sent if sent else 0.0,
        judged=len(judged),
    )


def describe(report: TimingReport) -> str:
    """One line for the weekly report."""
    if not report.total_sent:
        return "no sends recorded in a local timezone yet"
    if not report.has_enough_to_rank:
        return (
            f"{report.total_sent} send(s) across {len(report.outcomes)} slot(s); "
            f"{report.judged} slot(s) have the {MIN_SENDS_PER_SLOT} messages needed "
            f"to judge, {MIN_SLOTS_TO_RANK} are needed to compare them"
        )

    best = report.best()
    worst = report.worst()
    if not best and not worst:
        return (
            f"{report.judged} slot(s) judged at a {report.baseline:.1%} reply rate; "
            "none differs from the rest by enough to matter"
        )

    parts = []
    if best:
        parts.append("best " + ", ".join(f"{o.slot} ({o.reply_rate:.1%})" for o in best))
    if worst:
        parts.append(
            "weakest " + ", ".join(f"{o.slot} ({o.reply_rate:.1%})" for o in worst)
        )
    return f"against a {report.baseline:.1%} baseline: " + "; ".join(parts)


__all__ = [
    "MATERIAL_DIFFERENCE",
    "MIN_SENDS_PER_SLOT",
    "MIN_SLOTS_TO_RANK",
    "WEEKDAY_NAMES",
    "Slot",
    "SlotOutcome",
    "SlotVerdict",
    "TimingReport",
    "describe",
    "learn",
]
