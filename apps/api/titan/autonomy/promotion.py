"""Deciding whether a winning phrasing has earned the right to be the default.

:mod:`titan.autonomy.experiments` already answers "did this variant beat that
one" with a pooled two-proportion test and returns one of four verdicts. This
answers the separate question the phase actually asks: *given that verdict, do we
change what everyone gets from now on?*

They are separate because the statistics do not know what a wrong answer costs.
A comparison that clears p < 0.05 has cleared p < 0.05; whether that is enough to
change the wording every future lead receives is a policy, and writing it here
means it is one line to find and one line to argue with.

**Promotion happens on one verdict and no others.** ``CHALLENGER_WINS`` with a
p-value under the threshold. Everything else -- too small to test, inside the
noise, or the control already winning -- is a refusal, and each is *recorded*
rather than passed over in silence. A trail containing only promotions would
show a manager that always acts and never declines, which is precisely the
behaviour that would be worth catching.

**Nothing is promoted twice.** A register already promoted is refused rather
than re-applied, so a comparison that keeps winning stops changing anything
after the first time it wins.

**A promotion is reversible and is not a ratchet.** The manager may promote a
different register later on new evidence, and a human clearing the column
returns the campaign to per-lead selection. What it may never do is invent
wording: the registers are written, reviewed and validated in advance, and this
chooses between them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from titan.autonomy.actuator import Actuation, Proposal
from titan.autonomy.experiments import ALPHA, Comparison, Verdict

#: The p-value a comparison must clear to change what every future lead sees.
#: The same ALPHA the test itself uses, restated here on purpose: a promotion
#: threshold that silently tracked the test's alpha would change meaning the day
#: somebody tuned the test.
PROMOTION_ALPHA = ALPHA

#: Variant keys look like ``v2`` or ``v2:step1`` -- register, then optionally the
#: sequence step it was composed for. The register is what gets promoted; the
#: step is which message it appeared in and is not part of the choice.
_VARIANT = re.compile(r"^v(\d+)")


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """What was decided about a comparison, and why. Recorded either way."""

    promoted: bool
    register: int | None
    reason: str
    confidence: float

    def describe(self) -> str:
        if self.promoted:
            return f"promoted register v{self.register}: {self.reason}"
        return f"no promotion: {self.reason}"


def register_of(variant_key: str) -> int | None:
    """The phrasing register a variant key names, or None if it names none.

    ``none`` is a real key -- it is what the rollup reports for drafts composed
    before variants were recorded -- and it is not a register. Returning None
    rather than raising keeps an old draft from taking down a manager cycle.
    """
    match = _VARIANT.match((variant_key or "").strip())
    return int(match.group(1)) if match else None


def decide(
    comparison: Comparison | None,
    *,
    currently_promoted: int | None,
    alpha: float = PROMOTION_ALPHA,
) -> PromotionDecision:
    """Whether this comparison should change the default phrasing."""
    if comparison is None:
        return PromotionDecision(
            promoted=False,
            register=None,
            reason="no comparison was possible; fewer than two variants have run",
            confidence=0.0,
        )

    confidence = 1.0 - comparison.p_value if comparison.p_value is not None else 0.0

    if comparison.verdict is Verdict.INSUFFICIENT:
        return PromotionDecision(
            promoted=False,
            register=None,
            reason=(
                "one or both variants are too small to test; "
                f"{comparison.control.sent} against {comparison.challenger.sent} sends"
            ),
            confidence=0.0,
        )

    if comparison.verdict is Verdict.INCONCLUSIVE:
        return PromotionDecision(
            promoted=False,
            register=None,
            reason=(
                "the difference is inside the noise"
                + (f" (p={comparison.p_value:.3f})" if comparison.p_value else "")
            ),
            confidence=confidence,
        )

    if comparison.verdict is Verdict.CONTROL_WINS:
        return PromotionDecision(
            promoted=False,
            register=None,
            reason=(
                "the control won; it is already what most leads receive, "
                "so there is nothing to change"
            ),
            confidence=confidence,
        )

    if comparison.p_value is None or comparison.p_value >= alpha:
        return PromotionDecision(
            promoted=False,
            register=None,
            reason=(
                f"the challenger led but did not clear p<{alpha}"
                + (f" (p={comparison.p_value:.3f})" if comparison.p_value else "")
            ),
            confidence=confidence,
        )

    register = register_of(comparison.challenger.key)
    if register is None:
        return PromotionDecision(
            promoted=False,
            register=None,
            reason=(
                f"the winning variant {comparison.challenger.key!r} names no "
                "register, so there is nothing to promote"
            ),
            confidence=confidence,
        )

    if register == currently_promoted:
        return PromotionDecision(
            promoted=False,
            register=register,
            reason=f"register v{register} is already promoted",
            confidence=confidence,
        )

    lift = f", {comparison.lift:+.0%} lift" if comparison.lift is not None else ""
    return PromotionDecision(
        promoted=True,
        register=register,
        reason=(
            f"beat the control at p={comparison.p_value:.3f}{lift} over "
            f"{comparison.challenger.sent} sends"
        ),
        confidence=confidence,
    )


def proposal_for(
    decision: PromotionDecision,
    *,
    campaign_id: str,
    currently_promoted: int | None,
    comparison: Comparison | None,
) -> Proposal | None:
    """The proposal this decision becomes, including when it refuses.

    A refusal is still a proposal, with ``proposed == current``: ``apply_all``
    records every proposal and writes only the ones that change something, so
    routing refusals through it is what produces the written record of *every*
    refusal to promote that this phase asks for. A trail of promotions alone
    would show a manager that always acts and never declines.

    The one case that returns None is no comparison at all. That is not a
    refusal to promote -- it is the absence of a question, and recording it
    would write a row per campaign per cycle saying nothing happened, which is
    how a decision trail becomes unreadable.
    """
    if comparison is None:
        return None
    if decision.register is None:
        # Refused, and there is no register to name as either current or
        # proposed. Recorded against whatever is promoted now, unchanged.
        register = currently_promoted if currently_promoted is not None else -1
    else:
        register = decision.register

    evidence: dict[str, object] = {"reason": decision.reason}
    if comparison is not None:
        evidence.update(
            {
                "control": comparison.control.key,
                "control_sent": comparison.control.sent,
                "control_positive": comparison.control.positive_replies,
                "challenger": comparison.challenger.key,
                "challenger_sent": comparison.challenger.sent,
                "challenger_positive": comparison.challenger.positive_replies,
                "p_value": comparison.p_value,
                "lift": comparison.lift,
            }
        )

    current = currently_promoted if currently_promoted is not None else -1
    return Proposal(
        actuation=Actuation.SET_PROMOTED_VARIANT,
        campaign_id=campaign_id,
        # -1 stands for "no opinion" so the trail records a real transition
        # rather than a null-to-value the reader has to interpret.
        current=current,
        # Equal to current when refusing, so the actuator writes nothing and
        # apply_all still records the row and its reason.
        proposed=register if decision.promoted else current,
        reason=decision.reason,
        confidence=decision.confidence,
        evidence=evidence,
    )


__all__ = [
    "PROMOTION_ALPHA",
    "PromotionDecision",
    "decide",
    "proposal_for",
    "register_of",
]
