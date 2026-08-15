"""Deciding whether one variant actually beat another.

Assignment was never the hard part and Titan already did it: the composer picks
a phrasing register from a stable hash of the lead id, so the same lead always
gets the same variant and a retry cannot contaminate the arm. What was missing
was the choice being written down, and then the only question that matters --
did it make any difference?

**"A has a higher reply rate than B" is not an answer.** Two arms of a hundred
messages at 5% and 7% differ by two replies. Promoting on that is promoting
noise, and the promotion then looks like evidence for the next decision, which
is how a system talks itself into a phrasing nobody tested. So this runs a
two-proportion z-test with a stated significance level, and reports
INCONCLUSIVE far more often than it reports a winner. That is the correct
output, not a limitation.

**The normal approximation has preconditions and they are checked.** It needs
enough of both outcomes in both arms -- the usual rule is at least five
successes and five failures per arm. Cold outreach replies at single-digit
percentages, so five replies means roughly a hundred messages per arm before
the test means anything at all. An arm below that returns INSUFFICIENT rather
than a p-value, because a p-value computed outside the approximation's domain
is a number with the shape of an answer and none of the content.

**Nothing here promotes anything.** It compares. Acting on a comparison is the
manager's, through the actuator, and the actuator does not currently reach
message content -- deliberately, since that is the boundary the composer and
validator sit behind. A recommendation is a recommendation.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from enum import StrEnum

#: Significance level. Two-tailed: the question is whether the arms differ, not
#: whether the challenger is better, and testing one tail because you hoped for
#: a direction is how a 5% false-positive rate becomes 10%.
ALPHA = 0.05

#: The normal approximation to the binomial needs roughly this many of *each*
#: outcome in *each* arm. Below it the test is not conservative, it is
#: undefined, and a p-value from it is decoration.
MIN_OUTCOMES_PER_ARM = 5

#: A floor on arm size independent of outcomes. An arm of thirty that happened
#: to collect five replies has a 17% reply rate and a very small denominator.
MIN_SENDS_PER_ARM = 100


class Verdict(StrEnum):
    #: One or both arms are too small for the test to mean anything.
    INSUFFICIENT = "insufficient"
    #: Big enough to test, and the difference is inside the noise.
    INCONCLUSIVE = "inconclusive"
    CHALLENGER_WINS = "challenger_wins"
    CONTROL_WINS = "control_wins"


@dataclass(frozen=True, slots=True)
class Arm:
    """One variant's record."""

    key: str
    sent: int = 0
    replied: int = 0

    @property
    def reply_rate(self) -> float:
        return self.replied / self.sent if self.sent else 0.0

    @property
    def is_testable(self) -> bool:
        """Whether the normal approximation holds for this arm.

        Both outcomes, not just replies: an arm of five messages that all
        replied fails this as surely as one that none did.
        """
        if self.sent < MIN_SENDS_PER_ARM:
            return False
        failures = self.sent - self.replied
        return self.replied >= MIN_OUTCOMES_PER_ARM and failures >= MIN_OUTCOMES_PER_ARM


@dataclass(frozen=True, slots=True)
class Comparison:
    control: Arm
    challenger: Arm
    verdict: Verdict
    #: Relative change in reply rate, challenger against control. None when the
    #: control never replied, because the increase is then undefined rather
    #: than infinite.
    lift: float | None = None
    #: None when the arms were too small to test. Not 1.0 -- that would read as
    #: "tested and found identical", which is a different claim.
    p_value: float | None = None

    @property
    def winner(self) -> Arm | None:
        if self.verdict is Verdict.CHALLENGER_WINS:
            return self.challenger
        if self.verdict is Verdict.CONTROL_WINS:
            return self.control
        return None


def assign(experiment: str, key: str, arms: int) -> int:
    """Which arm this subject belongs to. Deterministic, and stable forever.

    Hashed from the subject rather than drawn at random, for the same reason
    the composer does it: an assignment that is re-drawn on a retry puts one
    lead in two arms and quietly corrupts both.

    The experiment name is mixed in so that two experiments running at once do
    not split the population along the same line -- otherwise every lead in arm
    0 of the first is in arm 0 of the second, and the two results are entangled
    in a way nothing downstream can detect.
    """
    if arms <= 0:
        raise ValueError("an experiment needs at least one arm")
    digest = hashlib.sha256(f"{experiment}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % arms


def _phi(x: float) -> float:
    """Standard normal CDF. erf is in the standard library; scipy is not here."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def compare(control: Arm, challenger: Arm, alpha: float = ALPHA) -> Comparison:
    """Whether these two arms differ by more than chance.

    Pooled two-proportion z-test. The pooled estimate is the right one under
    the null hypothesis that both arms share a rate -- using each arm's own rate
    in the standard error tests a different, weaker question.
    """
    if not (control.is_testable and challenger.is_testable):
        return Comparison(
            control=control,
            challenger=challenger,
            verdict=Verdict.INSUFFICIENT,
            lift=_lift(control, challenger),
        )

    n1, n2 = control.sent, challenger.sent
    pooled = (control.replied + challenger.replied) / (n1 + n2)
    # No zero-variance guard is needed here and one would be dead code:
    # is_testable already requires at least five replies and five non-replies
    # in each arm, so the pooled rate is strictly between 0 and 1 and the
    # standard error is strictly positive. A degenerate pair never reaches this
    # line -- it is refused as INSUFFICIENT above.
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    z = (challenger.reply_rate - control.reply_rate) / se
    p_value = 2 * (1 - _phi(abs(z)))

    if p_value > alpha:
        verdict = Verdict.INCONCLUSIVE
    elif challenger.reply_rate > control.reply_rate:
        verdict = Verdict.CHALLENGER_WINS
    else:
        verdict = Verdict.CONTROL_WINS

    return Comparison(
        control=control,
        challenger=challenger,
        verdict=verdict,
        lift=_lift(control, challenger),
        p_value=p_value,
    )


def _lift(control: Arm, challenger: Arm) -> float | None:
    if not control.reply_rate:
        return None
    return (challenger.reply_rate - control.reply_rate) / control.reply_rate


def best_against_control(arms: list[Arm], alpha: float = ALPHA) -> Comparison | None:
    """Compare every arm against the largest one, and return the best result.

    The largest arm is the control because it is the one with most evidence
    behind it, not because it is first alphabetically.

    **No correction for multiple comparisons is applied, and the reason it does
    not need one is the sample floor.** With four arms tested at 5%, the chance
    of at least one false positive is about 14% rather than 5% -- but an arm only
    reaches the test after a hundred messages and five replies, which at
    Titan's volumes is months. A framework that ran forty comparisons a day
    would need Bonferroni; one that runs three a quarter is bounded by patience.
    If that ever stops being true, this is the paragraph to revisit.
    """
    testable = [a for a in arms if a.sent > 0]
    if len(testable) < 2:
        return None
    control = max(testable, key=lambda a: (a.sent, a.key))
    results = [compare(control, arm, alpha) for arm in testable if arm.key != control.key]
    if not results:
        return None
    decisive = [r for r in results if r.winner is not None]
    if decisive:
        return max(decisive, key=lambda r: r.challenger.reply_rate)
    return max(results, key=lambda r: r.challenger.sent)


def describe(comparison: Comparison | None) -> str:
    """One line for the weekly report."""
    if comparison is None:
        return "no variant has enough sends to compare yet"

    control, challenger = comparison.control, comparison.challenger
    counts = (
        f"{control.key} {control.reply_rate:.1%} of {control.sent} "
        f"vs {challenger.key} {challenger.reply_rate:.1%} of {challenger.sent}"
    )

    if comparison.verdict is Verdict.INSUFFICIENT:
        return (
            f"{counts} -- below the {MIN_SENDS_PER_ARM} sends and "
            f"{MIN_OUTCOMES_PER_ARM} replies per arm the test needs"
        )
    if comparison.verdict is Verdict.INCONCLUSIVE:
        return f"{counts} -- inside the noise (p={comparison.p_value:.2f})"

    winner = comparison.winner
    assert winner is not None
    lift = f", {comparison.lift:+.0%}" if comparison.lift is not None else ""
    return f"{counts} -- {winner.key} wins (p={comparison.p_value:.3f}{lift})"


__all__ = [
    "ALPHA",
    "MIN_OUTCOMES_PER_ARM",
    "MIN_SENDS_PER_ARM",
    "Arm",
    "Comparison",
    "Verdict",
    "assign",
    "best_against_control",
    "compare",
    "describe",
]
