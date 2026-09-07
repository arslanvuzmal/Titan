"""How many messages a mailbox may send today, given how it has been behaving.

Until now every limit in Titan was a fixed number on a policy row. Health could
refuse a specific message -- a broken SPF record or a complaint rate past the
pause threshold stops the send outright -- but nothing could turn the volume
*down*. The choice was between sending the full configured amount and sending
none, which is the wrong shape for the problem: a mailbox that is drifting
toward trouble needs to send less for a few days, not to stop and not to carry
on as though nothing were happening.

**The configured limit is a ceiling, never a target.** Adaptation moves the
effective limit within ``[0, configured]`` and cannot exceed it. "Increase"
means recovering toward the number a human already approved, never past it --
a system that raised its own sending limit would be bypassing the control it
exists to respect, which is exactly the line the bounded-autonomy rule draws.
Growing a *new* mailbox's volume is warm-up's job, and warm-up applies as a
second ceiling here.

**This is a throttle, not a gate.** Every condition that must stop a send
already stops it elsewhere and independently: ``SenderIdentity.authorization_errors``
refuses a mailbox whose authentication has lapsed, and
``deliverability.evaluate`` refuses one past a published reputation threshold.
Nothing here is load-bearing for safety. What it does is pace the mailbox
between those extremes, which is the range the gates say nothing about.

**Recovery is graded, arrival is not.** Volume drops the moment health drops,
and climbs back over three days. That asymmetry is deliberate: the cost of
sending too little for two days is two days of leads, and the cost of jumping
straight back to full volume after a reputation dip is the dip repeating -- a
sudden return to previous volume is itself one of the patterns receivers watch
for.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from titan.delivery.sender_health import SenderHealth

#: Share of the configured ceiling permitted at each health level.
#:
#: WARMING and UNKNOWN sit at 1.0 and are not oversights. Warm-up already
#: imposes its own, much lower ceiling, and applying a second reduction on top
#: would slow a new mailbox to a crawl for a reason nobody wrote down. UNKNOWN
#: means no evidence either way, and the configured number is already a human's
#: conservative choice -- inventing a reduction from an absence of data would be
#: acting on nothing.
HEALTH_FACTORS: dict[SenderHealth, float] = {
    SenderHealth.BLOCKED: 0.0,
    SenderHealth.DEGRADED: 0.25,
    SenderHealth.WATCH: 0.6,
    SenderHealth.WARMING: 1.0,
    SenderHealth.HEALTHY: 1.0,
    SenderHealth.UNKNOWN: 1.0,
}

#: What a blocked mailbox may send while it has stopped bouncing.
#:
#: Zero was a deadlock, and it took a live mailbox to make it visible.
#: ``outreach@`` accumulated five hard bounces over 94 sends -- 5.32%, against
#: a 2% pause threshold -- every one of them from a fortnight earlier, before
#: address verification existed. The gate blocked it, correctly. But the rate
#: is *bounces over sends*, so the only way down is more clean sends, and a
#: blocked mailbox sends nothing. It could not earn its way out; it could only
#: wait for the thirty-day window to roll past the bad days.
#:
#: This is the same asymmetry the warm-up peak window already reasons about:
#: "a mailbox quarantined by the reputation gate must not also be throttled to
#: the floor by its own quarantine, or it could never send its way back out."
#: That argument was applied to the step-up bound and not to the block itself.
#:
#: Five a day, and only on the condition below. Small enough that a mailbox
#: which really is mailing dead addresses produces its next bounce almost
#: immediately and returns to zero; large enough that a mailbox whose list has
#: since been cleaned dilutes a stale rate in a few weeks rather than never.
PROBATION_VOLUME = 5

#: How long a blocked mailbox must go without a hard bounce before probation.
#:
#: The distinction this draws is the whole point: *still bouncing* and *bounced
#: a while ago and the window has not rolled* are different states that the
#: rate alone cannot tell apart. A mailbox that bounced yesterday keeps its
#: zero. One whose most recent bounce is older than this is not currently
#: harming anybody, and its rate is a fact about history.
PROBATION_QUIET_DAYS = 7

#: Statuses that count as a dip worth recovering from.
_DIPPED = frozenset({SenderHealth.BLOCKED, SenderHealth.DEGRADED, SenderHealth.WATCH})

#: How far back a dip is remembered. A week: long enough that recovery is not
#: gamed by one good day, short enough that a bad Monday does not still be
#: costing volume a fortnight later.
RECOVERY_LOOKBACK_DAYS = 7

#: Share of the ceiling on the first healthy day after a dip, and the step added
#: for each further consecutive healthy day. 0.5 then 0.75 then full: three days
#: back to normal.
RECOVERY_START = 0.5
RECOVERY_STEP = 0.25

#: A sender that is not paused may always send at least this many. Without it,
#: a small configured limit multiplied by a reduction factor floors to zero and
#: a throttle silently becomes a pause -- 4 a day at 0.25 is 1, but 3 a day is 0.
MIN_ACTIVE_LIMIT = 1


@dataclass(frozen=True, slots=True)
class LimitDecision:
    """The effective daily ceiling, and enough to explain it."""

    configured: int
    effective: int
    factor: float
    health: SenderHealth
    recovering: bool = False
    warmup_limit: int | None = None
    #: True when the only reason this mailbox may send anything is the
    #: probation floor below -- the health factor alone would have given it
    #: zero. Carried because ``explain`` is otherwise forced to describe a
    #: mailbox sending five a day as "reduced to 0%", which is the sentence a
    #: dashboard would print next to the five it is actually sending.
    probation: bool = False

    @property
    def paused(self) -> bool:
        return self.effective <= 0

    @property
    def reduced(self) -> bool:
        return self.effective < self.configured

    def explain(self) -> str:
        if self.paused:
            return f"sending paused: mailbox health is {self.health.value}"
        parts = [f"{self.effective} of {self.configured} a day"]
        if self.probation:
            # Said first and said plainly, because it is the whole reason the
            # number is not zero. "Reduced to 0%" beside an allowance of five
            # reads as a bug in the dashboard rather than a deliberate remedy.
            parts.append(
                f"on probation: health is {self.health.value}, but nothing has "
                f"hard-bounced recently, so a small allowance runs to let the "
                f"rate recover"
            )
        elif self.recovering:
            parts.append(
                f"recovering from a recent dip at {self.factor:.0%} of the ceiling"
            )
        elif self.factor < 1.0:
            parts.append(f"reduced to {self.factor:.0%}: health is {self.health.value}")
        if self.warmup_limit is not None and self.warmup_limit <= self.effective:
            parts.append(f"warm-up caps today at {self.warmup_limit}")
        return "; ".join(parts)


def recovery_factor(recent: tuple[SenderHealth, ...]) -> tuple[float, bool]:
    """The ceiling share allowed while climbing back, newest status first.

    Returns (factor, recovering). A mailbox with no dip in the lookback is not
    recovering and gets the full ceiling; one whose recent history contains a
    dip gets a share that grows with each consecutive healthy day since.
    """
    window = recent[:RECOVERY_LOOKBACK_DAYS]
    if not any(status in _DIPPED for status in window):
        return 1.0, False

    consecutive = 0
    for status in window:
        if status in _DIPPED:
            break
        consecutive += 1

    if consecutive == 0:
        # Today is itself a dip. The health factor governs; there is nothing to
        # recover from yet.
        return 1.0, False

    factor = RECOVERY_START + (consecutive - 1) * RECOVERY_STEP
    return (1.0, False) if factor >= 1.0 else (factor, True)


def daily_limit(
    configured: int,
    *,
    recent: tuple[SenderHealth, ...],
    warmup_limit: int | None = None,
    days_since_bounce: int | None = None,
) -> LimitDecision:
    """The effective ceiling for this mailbox today.

    ``recent`` is the mailbox's health newest-first, today's verdict included,
    as read from ``sender_health_snapshots``. An empty history means no snapshot
    exists yet, which is treated as UNKNOWN and therefore as no adjustment.

    ``days_since_bounce`` is how long since this mailbox last hard-bounced.
    ``None`` means it never has, or nobody looked, and grants **no** probation:
    a mailbox blocked without any bounce behind it was blocked for some other
    reason -- failed authentication, or a complaint rate -- and neither of those
    is repaired by sending five more. Probation is a remedy for one specific
    deadlock, not a general floor. It is consulted only when the mailbox is
    blocked; see :data:`PROBATION_VOLUME` for what that deadlock is.
    """
    health = recent[0] if recent else SenderHealth.UNKNOWN
    factor = HEALTH_FACTORS.get(health, 1.0)
    recovering = False

    if factor >= 1.0:
        factor, recovering = recovery_factor(recent)

    # floor, not round: rounding up would let a reduction hand back more than
    # the factor allows on small ceilings.
    effective = math.floor(max(configured, 0) * factor)
    if factor > 0 and configured > 0:
        effective = max(effective, MIN_ACTIVE_LIMIT)

    if warmup_limit is not None:
        effective = min(effective, max(warmup_limit, 0))

    # Probation, last: it raises a floor rather than lifting a cap, so it must
    # not be re-narrowed by the warm-up bound above. A mailbox blocked *and*
    # mid-warm-up is still allowed its five -- warm-up limits growth, and five
    # is not growth.
    probation = False
    if (
        health is SenderHealth.BLOCKED
        and configured > 0
        and days_since_bounce is not None
        and days_since_bounce >= PROBATION_QUIET_DAYS
    ):
        allowance = min(PROBATION_VOLUME, configured)
        probation = allowance > effective
        effective = max(effective, allowance)

    return LimitDecision(
        configured=configured,
        effective=effective,
        factor=factor,
        health=health,
        recovering=recovering,
        warmup_limit=warmup_limit,
        probation=probation,
    )


__all__ = [
    "HEALTH_FACTORS",
    "MIN_ACTIVE_LIMIT",
    "RECOVERY_LOOKBACK_DAYS",
    "RECOVERY_START",
    "RECOVERY_STEP",
    "LimitDecision",
    "daily_limit",
    "recovery_factor",
]
