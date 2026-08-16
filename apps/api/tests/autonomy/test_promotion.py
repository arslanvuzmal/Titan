"""Whether a winning phrasing has earned the right to become everyone's default.

The statistics and the policy are separate questions and are tested separately.
`experiments.py` answers "did this beat that". This answers "given that, do we
change what every future lead receives" -- and the cost of a wrong answer there
is not symmetric, so almost every test here is about a refusal.

The phase is explicit that both outcomes must be readable months later, so a
refusal carries a reason for the same reason a promotion does.
"""

from __future__ import annotations

from titan.autonomy.actuator import Actuation, Bounds, evaluate
from titan.autonomy.experiments import Arm, Comparison, Verdict
from titan.autonomy.promotion import (
    PROMOTION_ALPHA,
    decide,
    proposal_for,
    register_of,
)

REGISTERS = 4


def comparison(
    verdict: Verdict,
    *,
    p_value: float | None = 0.01,
    challenger: str = "v2",
    lift: float | None = 0.4,
    sent: int = 500,
) -> Comparison:
    return Comparison(
        control=Arm(key="v0", sent=sent, replied=40, positive_replies=20),
        challenger=Arm(key=challenger, sent=sent, replied=56, positive_replies=28),
        verdict=verdict,
        lift=lift,
        p_value=p_value,
    )


# ------------------------------------------------------------------ promotion


def test_a_genuine_win_is_promoted() -> None:
    """The capability the phase asks for."""
    result = decide(comparison(Verdict.CHALLENGER_WINS), currently_promoted=None)

    assert result.promoted is True
    assert result.register == 2
    assert "p=0.010" in result.reason
    assert "lift" in result.reason


def test_the_promotion_becomes_an_actuator_proposal() -> None:
    """It goes through the actuator like every other change the manager makes."""
    result = decide(comparison(Verdict.CHALLENGER_WINS), currently_promoted=None)

    proposal = proposal_for(
        result,
        campaign_id="c",
        currently_promoted=None,
        comparison=comparison(Verdict.CHALLENGER_WINS),
    )

    assert proposal is not None
    assert proposal.actuation is Actuation.SET_PROMOTED_VARIANT
    assert proposal.proposed == 2
    # -1 rather than None, so the trail records a transition rather than a null
    # the reader has to interpret.
    assert proposal.current == -1
    assert proposal.evidence["challenger"] == "v2"
    assert proposal.evidence["p_value"] == 0.01


# ------------------------------------------------------------------- refusals


def test_a_coin_flip_is_explicitly_not_promoted() -> None:
    """Named in the phase's own acceptance criteria."""
    result = decide(
        comparison(Verdict.INCONCLUSIVE, p_value=0.42), currently_promoted=None
    )

    assert result.promoted is False
    assert "inside the noise" in result.reason
    assert "0.42" in result.reason


def test_arms_too_small_to_test_are_refused_with_their_sizes() -> None:
    result = decide(
        comparison(Verdict.INSUFFICIENT, p_value=None, sent=9), currently_promoted=None
    )

    assert result.promoted is False
    assert "too small" in result.reason
    assert "9 against 9" in result.reason


def test_a_win_that_misses_the_threshold_is_refused() -> None:
    """Verdict and threshold are checked separately on purpose.

    A verdict computed under one alpha must not promote under another.
    """
    result = decide(
        comparison(Verdict.CHALLENGER_WINS, p_value=PROMOTION_ALPHA + 0.01),
        currently_promoted=None,
    )

    assert result.promoted is False
    assert f"p<{PROMOTION_ALPHA}" in result.reason


def test_the_control_winning_changes_nothing() -> None:
    result = decide(comparison(Verdict.CONTROL_WINS), currently_promoted=None)

    assert result.promoted is False
    assert "already what most leads receive" in result.reason


def test_no_comparison_is_a_refusal_with_a_reason() -> None:
    """The state this workspace is actually in today: one variant, nothing to
    compare. It must produce a readable decision, not silence."""
    result = decide(None, currently_promoted=None)

    assert result.promoted is False
    assert "fewer than two variants" in result.reason
    # The absence of a question is not a refusal to answer it. Recording this
    # would write a row per campaign per cycle saying nothing happened.
    assert (
        proposal_for(result, campaign_id="c", currently_promoted=None, comparison=None)
        is None
    )


def test_the_same_register_is_not_promoted_twice() -> None:
    """Otherwise a comparison that keeps winning writes a decision row every
    hour for the rest of the month."""
    result = decide(comparison(Verdict.CHALLENGER_WINS), currently_promoted=2)

    assert result.promoted is False
    assert "already promoted" in result.reason
    # Still recorded: a refusal is a decision, and the phase asks for every one
    # to be readable later. proposed == current, so nothing is written.
    proposal = proposal_for(
        result,
        campaign_id="c",
        currently_promoted=2,
        comparison=comparison(Verdict.CHALLENGER_WINS),
    )
    assert proposal is not None
    assert proposal.proposed == proposal.current
    assert proposal.is_change is False


def test_a_variant_naming_no_register_cannot_be_promoted() -> None:
    """`none` is a real key -- it is what the rollup reports for drafts composed
    before variants were recorded -- and it is not a register."""
    result = decide(
        comparison(Verdict.CHALLENGER_WINS, challenger="none"), currently_promoted=None
    )

    assert result.promoted is False
    assert "names no register" in result.reason


# --------------------------------------------------------------- the boundary


def test_a_register_that_does_not_exist_is_refused_not_clamped() -> None:
    """The one place clamping would be wrong.

    Clamping a limit still executes the intent -- the manager wanted more and
    gets what it may have. Clamping a register index would promote a *different
    phrasing* from the one the evidence was about: not a smaller version of the
    decision but a different decision, made silently.
    """
    result = decide(
        comparison(Verdict.CHALLENGER_WINS, challenger="v99"), currently_promoted=None
    )
    proposal = proposal_for(
        result,
        campaign_id="c",
        currently_promoted=None,
        comparison=comparison(Verdict.CHALLENGER_WINS),
    )
    assert proposal is not None

    verdict = evaluate(
        proposal,
        Bounds(
            configured_daily_limit=50,
            configured_min_lead_score=70,
            variant_count=REGISTERS,
        ),
    )

    assert verdict.refused is True
    assert verdict.clamped is False
    assert "does not exist" in (verdict.refusal or "")
    assert verdict.applied_value == proposal.current


def test_a_real_register_passes_the_bound() -> None:
    """The contrast. A refusal that fired on everything would be a switch."""
    result = decide(comparison(Verdict.CHALLENGER_WINS), currently_promoted=None)
    proposal = proposal_for(
        result,
        campaign_id="c",
        currently_promoted=None,
        comparison=comparison(Verdict.CHALLENGER_WINS),
    )
    assert proposal is not None

    verdict = evaluate(
        proposal,
        Bounds(
            configured_daily_limit=50,
            configured_min_lead_score=70,
            variant_count=REGISTERS,
        ),
    )

    assert verdict.refused is False
    assert verdict.applied_value == 2


def test_register_parsing_handles_the_step_suffix() -> None:
    """Variant keys carry the sequence step they were composed for. The register
    is what gets promoted; the step is which message it appeared in."""
    assert register_of("v2") == 2
    assert register_of("v2:step1") == 2
    assert register_of("none") is None
    assert register_of("") is None
