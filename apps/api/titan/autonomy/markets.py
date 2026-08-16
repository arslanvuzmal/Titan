"""Letting market performance modulate capacity, without letting it decide.

:mod:`titan.autonomy.allocation` divides a workspace's daily sending between
campaigns by health, which is right and is not the whole question. Two campaigns
can both be Healthy while one sits in a market that answers and the other in a
market that has never replied to anything. Phase 06 asks for volume to move
toward the markets that perform; this is the part that says by how much.

**A modifier, never a decision.** The result is a multiplier on an existing
weight, bounded on both sides, so a market can shade the split and can never
invert it. Health stays dominant: a Degraded campaign in the best market in the
portfolio is still weight zero, because being in a good market is not evidence
that *this* campaign is well.

**Silent until measured.** Every market below the sample floor returns exactly
1.0 -- not a low multiplier, not an average, but literal no-opinion. This is the
same asymmetry the mailbox ramp and the rollups apply, and it matters more here
than anywhere: an unmeasured market that got a low multiplier would be starved
of the very sending that would measure it, and would stay unmeasured for as long
as the system ran.

**Compared against the portfolio, not against a fixed target.** "Good" is
relative to how the other measured markets did in the same window. A 2% positive
reply rate is excellent in one industry and poor in another, and hard-coding a
threshold would encode this month's business into the allocator.
"""

from __future__ import annotations

from dataclasses import dataclass

from titan.db.enums import Region
from titan.delivery.deliverability import MIN_SAMPLE_FOR_RATES
from titan.intelligence.portfolio import Portfolio, RegionSlice

#: The widest a market may shade a campaign's weight. Deliberately narrow: at
#: 1.35 the best market gets roughly a third more than the worst, which moves
#: real volume over a month and cannot starve anything in a week. A wider band
#: would let one good fortnight in one market empty the others.
MAX_MULTIPLIER = 1.35
MIN_MULTIPLIER = 0.75

#: No opinion. Returned for every market that has not been measured, and for
#: every market when nothing has.
NEUTRAL = 1.0


@dataclass(frozen=True, slots=True)
class MarketWeight:
    """One market's multiplier, and why it has it."""

    region: Region
    multiplier: float
    measured: bool
    positive_reply_rate: float | None
    reason: str

    def describe(self) -> str:
        return f"{self.region.value}: x{self.multiplier:.2f} -- {self.reason}"


def _positive_rate(slice_: RegionSlice) -> float | None:
    """Replies that went somewhere, per send. None below the sample floor.

    ``replied`` is used rather than a positive-reply count because
    ``RegionSlice`` carries only the one number. That is a real limitation and
    it is recorded here rather than papered over: this modulates on answers of
    any kind, so a market that provokes many "not interested" replies looks
    better than it is. It is bounded to a third either way, which is the reason
    that is tolerable and not a reason it is correct.
    """
    if slice_.sent < MIN_SAMPLE_FOR_RATES:
        return None
    return slice_.replied / slice_.sent if slice_.sent else 0.0


def weigh(book: Portfolio) -> dict[Region, MarketWeight]:
    """How much each market should shade the campaigns inside it.

    Every market gets an entry, including unmeasured ones, so a caller cannot
    silently miss a region by looking one up and finding nothing.
    """
    rates: dict[Region, float | None] = {s.region: _positive_rate(s) for s in book.slices}
    measured = {r: v for r, v in rates.items() if v is not None}

    if len(measured) < 2:
        # One measured market cannot be compared with anything, and comparing it
        # against itself would give it 1.0 by a longer route. Two is the minimum
        # at which "performing better" means something.
        return {
            region: MarketWeight(
                region=region,
                multiplier=NEUTRAL,
                measured=rate is not None,
                positive_reply_rate=rate,
                reason=("fewer than two measured markets; nothing to compare against"),
            )
            for region, rate in rates.items()
        }

    best = max(measured.values())
    worst = min(measured.values())
    spread = best - worst

    weights: dict[Region, MarketWeight] = {}
    for region, rate in rates.items():
        if rate is None:
            weights[region] = MarketWeight(
                region=region,
                multiplier=NEUTRAL,
                measured=False,
                positive_reply_rate=None,
                reason=(
                    f"below the {MIN_SAMPLE_FOR_RATES}-send floor; "
                    "not starved for being unmeasured"
                ),
            )
            continue
        if spread <= 0:
            weights[region] = MarketWeight(
                region=region,
                multiplier=NEUTRAL,
                measured=True,
                positive_reply_rate=rate,
                reason="every measured market performed identically",
            )
            continue

        # Linear between the worst and best measured market. Position, not
        # absolute rate: the question is "better than the alternatives", and the
        # alternatives are what capacity would otherwise go to.
        position = (rate - worst) / spread
        multiplier = MIN_MULTIPLIER + position * (MAX_MULTIPLIER - MIN_MULTIPLIER)
        weights[region] = MarketWeight(
            region=region,
            multiplier=round(multiplier, 4),
            measured=True,
            positive_reply_rate=rate,
            reason=(
                f"{rate:.1%} replies against {worst:.1%}-{best:.1%} across "
                f"{len(measured)} measured markets"
            ),
        )
    return weights


def multiplier_for(weights: dict[Region, MarketWeight], region: Region) -> float:
    """The multiplier for one market, defaulting to no opinion.

    A region absent from the portfolio -- a campaign created since the window
    was computed, say -- must not be penalised for being new.
    """
    weight = weights.get(region)
    return weight.multiplier if weight else NEUTRAL


def describe(weights: dict[Region, MarketWeight]) -> str:
    """One line, for the decision record."""
    measured = [w for w in weights.values() if w.measured]
    if not measured:
        return "no market has enough sending to be judged; capacity split on health alone"
    moved = [w for w in measured if w.multiplier != NEUTRAL]
    if not moved:
        return f"{len(measured)} measured market(s), none separated from the others"
    ordered = sorted(moved, key=lambda w: -w.multiplier)
    return "; ".join(w.describe() for w in ordered)


__all__ = [
    "MAX_MULTIPLIER",
    "MIN_MULTIPLIER",
    "NEUTRAL",
    "MarketWeight",
    "describe",
    "multiplier_for",
    "weigh",
]
