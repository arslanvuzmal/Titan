"""Growing a mailbox's daily volume on its own, a week at a time.

Titan already warms its *own* mailboxes: ``deliverability.warmup_limit`` ramps a
sender identity from a tenth of its target to its target over twenty days, and
``adaptive_limits`` throttles it back when health drops. None of that reaches
the mailboxes that actually send, because those live in Smartlead and their
``max_email_per_day`` is a number a human typed. The intelligence and the
sending were in different systems.

This is the join. It computes what a mailbox should be allowed to send today and
reconciles the external setting to it, so the ramp is a property of the system
rather than a task somebody remembers to do on Mondays.

**Weekly steps, daily evaluation.** Volume moves once a week because a receiver
judges a sender on a trend, and a limit that changes daily produces a trend made
of noise. Evidence is re-read every day, so a mailbox that starts bouncing on a
Tuesday is held or cut on Tuesday rather than at the end of the week.

**The ceiling is the human's number and is never exceeded.** Same bound as every
other autonomous decision here: the manager may only ever be *more* conservative
than what a person configured. Wanting more volume than the ceiling is a request
to change the ceiling, and that is not a decision this makes.

**And the ceiling is remembered, not re-read from the field being written.** The
provider has exactly one number per mailbox, ``max_email_per_day``, and the ramp
writes it. Taking the ceiling from that same field would make the ceiling the
ramp's own last output: a step down lowers the ceiling, the next step takes a
share of the lowered number, and a mailbox at 50 reaches the floor in three days
with nothing able to bring it back -- recovery multiplies a share by a ceiling
that no longer exists. :func:`observe_ceiling` is the fix, and it is the reason
this module needs a stored state at all.

**Absence of evidence is not evidence of safety.** Below the sample floor no
rate means anything, so the ramp *holds* rather than stepping up. That is the
asymmetry that matters: a mailbox with four sends and no bounces has not earned
more volume, it has simply not been measured yet. Stepping up on an unmeasured
mailbox is how a cold domain reaches full volume without anything having
checked it.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from titan.delivery.deliverability import (
    ReputationWindow,
    Severity,
    check_reputation,
)

#: Share of the ceiling permitted in each week of the ramp, indexed from the
#: first week. Six weeks from a fifth of target to target.
#:
#: Geometric-ish rather than linear, for the same reason the per-send ramp is:
#: reputation responds to *relative* growth, so the early steps are small in
#: absolute terms and the later ones are not. The last entry is 1.00 because a
#: ramp that never reaches the configured number is a permanent reduction
#: wearing warm-up's name.
WEEKLY_STEPS: tuple[float, ...] = (0.20, 0.35, 0.50, 0.70, 0.85, 1.00)

#: A mailbox in the ramp always keeps at least this much, so a small ceiling
#: multiplied by an early share does not floor to zero and silently become a
#: pause. 20% of 5 is 1, but 20% of 2 is 0.
MIN_DAILY = 1

#: How far volume is cut when evidence says the mailbox is in trouble. Not to
#: zero: a mailbox sending nothing produces no evidence, so it can never
#: demonstrate recovery and would stay cut forever.
SETBACK_SHARE = 0.5


@dataclass(frozen=True, slots=True)
class RampDecision:
    """What one mailbox should be allowed to send today, and why."""

    mailbox: str
    ceiling: int
    current: int
    target: int
    week: int
    reason: str

    @property
    def changed(self) -> bool:
        return self.target != self.current

    @property
    def direction(self) -> str:
        if self.target > self.current:
            return "up"
        if self.target < self.current:
            return "down"
        return "hold"

    def describe(self) -> str:
        arrow = {"up": "->", "down": "<-", "hold": "=="}[self.direction]
        return (
            f"{self.mailbox}: {self.current} {arrow} {self.target} "
            f"of {self.ceiling} (week {self.week + 1}) -- {self.reason}"
        )


def week_index(first_send_at: dt.datetime | None, now: dt.datetime) -> int:
    """Which week of the ramp this mailbox is in, counted from its first send.

    A mailbox that has never sent is in week 0. Counted from the first *send*
    rather than from when the account was created, because an account
    configured in March and first used in July is new to receivers in July.
    """
    if first_send_at is None:
        return 0
    days = (now.date() - first_send_at.date()).days
    return max(0, days // 7)


def scheduled_share(week: int) -> float:
    """The share of the ceiling this week of the ramp allows."""
    if week >= len(WEEKLY_STEPS):
        return WEEKLY_STEPS[-1]
    return WEEKLY_STEPS[max(0, week)]


def observe_ceiling(
    *,
    observed: int,
    stored_ceiling: int | None,
    last_written: int | None,
) -> int:
    """The ceiling after seeing what the provider currently has set.

    Only a person moves the ceiling. The ramp writes the same field, so the
    question this answers is "did a human set this number, or did I?" --
    answered by remembering the last value written rather than by guessing from
    its size.

    A human's edit is adopted in *both* directions. Lowering the limit is a
    person asking for less, and the whole bound of this module is that it may
    only ever be more conservative than what a person configured; treating a
    reduction as anything other than a new ceiling would let the ramp climb back
    over an instruction it was given.

    The one case that cannot be distinguished is a human setting, by hand, the
    exact number the ramp last wrote. The ceiling then stands, which is the
    reading that loses nothing: it is what the ramp already believed.
    """
    if stored_ceiling is None:
        # First sight. Whatever a person has configured is the ceiling, and
        # there is no prior belief to defend.
        return max(0, observed)
    if last_written is not None and observed == last_written:
        return stored_ceiling
    return max(0, observed)


def _bounded(value: int, ceiling: int) -> int:
    if ceiling <= 0:
        return 0
    return max(MIN_DAILY, min(value, ceiling))


def decide(
    *,
    mailbox: str,
    ceiling: int,
    current: int,
    first_send_at: dt.datetime | None,
    now: dt.datetime,
    evidence: ReputationWindow,
) -> RampDecision:
    """Today's allowance for one mailbox.

    Ordered, not scored. Each layer can only ever reduce what the one above it
    allowed, so a mailbox in trouble cannot be talked back up by a good number
    somewhere else.
    """
    week = week_index(first_send_at, now)

    def decision(target: int, reason: str) -> RampDecision:
        return RampDecision(
            mailbox=mailbox,
            ceiling=ceiling,
            current=current,
            target=_bounded(target, ceiling),
            week=week,
            reason=reason,
        )

    if ceiling <= 0:
        return decision(0, "no ceiling configured; the mailbox is closed")

    scheduled = math.ceil(scheduled_share(week) * ceiling)
    signals = check_reputation(evidence)

    # Negative evidence first, and it outranks everything below it.
    blocking = [s for s in signals if s.severity is Severity.BLOCK]
    if blocking:
        # Half of what this week would otherwise allow -- not half of what the
        # mailbox currently has.
        #
        # Halving the current value compounds: the same unresolved problem seen
        # on four consecutive days walks a mailbox 25, 12, 6, 3, 1, and the
        # severity of the cut then depends on how many times it has been
        # observed rather than on how bad it is. A mailbox with one persistent
        # signal ends up closed as surely as one that is genuinely burnt.
        #
        # Anchoring to the week's schedule makes the cut proportional to what
        # the mailbox has earned, and it holds there while the problem lasts.
        # min() keeps it a cut: a mailbox already below half its schedule is
        # never raised by a blocking signal.
        return decision(
            min(current, math.floor(scheduled * SETBACK_SHARE)),
            f"cut on delivery evidence: {'; '.join(s.detail for s in blocking)}",
        )

    warning = [s for s in signals if s.severity is Severity.WARN]
    if warning:
        return decision(
            min(current, scheduled),
            f"held on delivery evidence: {'; '.join(s.detail for s in warning)}",
        )

    if not evidence.has_signal:
        # The asymmetry this module exists for. Not measured is not the same as
        # measured clean, so the mailbox keeps what it has and waits.
        return decision(
            min(current, scheduled) if current > 0 else scheduled,
            (
                f"held below the sample floor: {evidence.sent} sent, "
                "no rate means anything yet"
            ),
        )

    if scheduled <= current:
        return decision(current, f"week {week + 1} allows {scheduled}; already there")

    return decision(
        scheduled,
        f"clean over {evidence.sent} sends; week {week + 1} allows {scheduled}",
    )


def summarise(decisions: list[RampDecision]) -> str:
    """One line per mailbox, then what actually moved."""
    if not decisions:
        return "no mailboxes to ramp"
    moved = [d for d in decisions if d.changed]
    lines = [f"  {d.describe()}" for d in decisions]
    tail = f"{len(moved)} of {len(decisions)} changed"
    return "\n".join(lines) + f"\n{tail}"


__all__ = [
    "MIN_DAILY",
    "SETBACK_SHARE",
    "WEEKLY_STEPS",
    "RampDecision",
    "decide",
    "observe_ceiling",
    "scheduled_share",
    "summarise",
    "week_index",
]
