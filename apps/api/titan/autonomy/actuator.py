"""The only way the campaign manager can change anything.

Bounded autonomy is easy to write down and hard to mean. Written down, it is a
paragraph saying the manager may optimise but must not bypass suppression,
compliance, evidence, approval or the delivery gates. Meant, it is a surface so
narrow that none of those is reachable -- not refused, *absent*.

So the manager holds exactly two numbers, and it writes them nowhere else.

**One rule covers the whole boundary: the manager can only ever be more
conservative than the human's configuration.** Every knob it holds is clamped
against the human's own value on the permissive side. It can send less than the
configured limit, never more. It can demand a higher lead score, never a lower
one. "Increase" means returning toward a number a human already approved, and
there is no path past it -- which is what makes the boundary a property of the
code rather than a promise about it.

**Manager values live in their own columns.** Writing to ``daily_send_limit``
directly would destroy the anchor: next cycle the ceiling would be the manager's
own previous number, and a small persistent error could ratchet in either
direction with nothing to measure against. The human's value stays untouched and
remains the bound.

**Nothing here changes campaign status.** Driving the daily limit to zero stops
a campaign sending, which is every practical effect of pausing it and none of
the record-keeping: status is a human's statement of intent, and a manager that
edited it would be writing in somebody else's voice. It also makes every action
here reversible by construction, which is the property that makes autonomy
survivable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Actuation(StrEnum):
    """The complete set. Adding a member is a deliberate widening of autonomy."""

    SET_DAILY_LIMIT = "set_daily_limit"
    SET_MIN_LEAD_SCORE = "set_min_lead_score"


#: A lead score the manager may never demand more than, however badly a campaign
#: is performing. Above this almost nothing qualifies and the campaign stops by
#: the back door -- which would be a pause, dressed as a threshold.
MAX_MANAGED_LEAD_SCORE = 95

#: The most the manager may raise the bar in one cycle. A campaign that jumps
#: from 70 to 95 overnight has not been tuned, it has been switched off.
MAX_SCORE_STEP = 10

#: Share of the configured limit below which a reduction is a pause in disguise.
#: Zero is reachable, but only by the explicit floor below rather than by
#: repeated halving.
MIN_MANAGED_LIMIT_RATIO = 0.1


@dataclass(frozen=True, slots=True)
class Bounds:
    """What a human configured. Every clamp is measured against this."""

    configured_daily_limit: int
    configured_min_lead_score: int

    @property
    def floor_daily_limit(self) -> int:
        """The least the manager may leave a campaign sending.

        Not zero. A campaign reduced to nothing looks identical to a paused one
        and recovers from neither, so the manager keeps a trickle: enough that
        the campaign continues to produce the outcomes its next decision needs.
        """
        return max(1, int(self.configured_daily_limit * MIN_MANAGED_LIMIT_RATIO))


@dataclass(frozen=True, slots=True)
class Proposal:
    """One change the manager wants to make, before anything checks it."""

    actuation: Actuation
    campaign_id: str
    current: int
    proposed: int
    reason: str
    #: How much the evidence supports this, 0 to 1. Recorded, never acted on --
    #: a threshold on confidence would be a second, unstated policy.
    confidence: float = 0.0
    #: The numbers the decision was made on, stored so it can be re-read later.
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_change(self) -> bool:
        return self.current != self.proposed


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the bounds made of a proposal."""

    proposal: Proposal
    applied_value: int
    refused: bool = False
    refusal: str | None = None
    clamped: bool = False

    @property
    def changes_anything(self) -> bool:
        return not self.refused and self.applied_value != self.proposal.current


def evaluate(proposal: Proposal, bounds: Bounds) -> Verdict:
    """Clamp a proposal to what the human's configuration permits.

    Clamping rather than refusing, wherever a bound can be satisfied by moving
    the number. A manager that wanted 200 when 50 is the ceiling wanted *more*,
    and 50 is the honest execution of that intent; refusing outright would leave
    a campaign at its old value for a reason nobody reading the row would guess.

    Refusal is kept for the case a bound cannot repair: an actuation this
    surface does not implement.
    """
    if proposal.actuation is Actuation.SET_DAILY_LIMIT:
        return _clamp(
            proposal,
            low=bounds.floor_daily_limit,
            high=bounds.configured_daily_limit,
        )

    if proposal.actuation is Actuation.SET_MIN_LEAD_SCORE:
        stepped = min(proposal.proposed, proposal.current + MAX_SCORE_STEP)
        return _clamp(
            proposal,
            low=bounds.configured_min_lead_score,
            high=MAX_MANAGED_LEAD_SCORE,
            value=stepped,
            note="one step" if stepped != proposal.proposed else None,
        )

    return Verdict(
        proposal=proposal,
        applied_value=proposal.current,
        refused=True,
        refusal=f"{proposal.actuation.value} is not an actuation this surface implements",
    )


def _clamp(
    proposal: Proposal,
    *,
    low: int,
    high: int,
    value: int | None = None,
    note: str | None = None,
) -> Verdict:
    wanted = proposal.proposed if value is None else value
    applied = max(low, min(high, wanted))
    return Verdict(
        proposal=proposal,
        applied_value=applied,
        clamped=applied != proposal.proposed,
        refusal=(
            None
            if applied == proposal.proposed
            else f"clamped from {proposal.proposed} to {applied}"
            + (f" ({note})" if note else "")
        ),
    )


def effective_daily_limit(configured: int, managed: int | None) -> int:
    """What the campaign may actually send today.

    ``min``, not the managed value: a managed number above the configured one
    cannot take effect even if something wrote it there directly, so the bound
    holds against a bug in the manager as well as against the manager.
    """
    if managed is None:
        return configured
    return min(configured, max(0, managed))


def effective_min_lead_score(configured: int, managed: int | None) -> int:
    """The bar a lead must clear.

    ``max``: the manager may demand better leads than a human asked for and
    never worse ones. Lowering the bar is the direction that mails people who
    should not have been mailed, and it is not available from here at all.
    """
    if managed is None:
        return configured
    return max(configured, min(MAX_MANAGED_LEAD_SCORE, managed))


__all__ = [
    "MAX_MANAGED_LEAD_SCORE",
    "MAX_SCORE_STEP",
    "MIN_MANAGED_LIMIT_RATIO",
    "Actuation",
    "Bounds",
    "Proposal",
    "Verdict",
    "effective_daily_limit",
    "effective_min_lead_score",
    "evaluate",
]
