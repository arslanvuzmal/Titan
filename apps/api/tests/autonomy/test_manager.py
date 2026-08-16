"""The campaign manager: what it judges, what it proposes, and what it cannot do.

The tests that matter most here are the refusals. An autonomous system is only
as good as the things it is incapable of, and every one of those is asserted
below against the configuration a human set -- not against the manager's own
previous answer, which is exactly the mistake the separate columns exist to
prevent.
"""

from __future__ import annotations

import pytest
from titan.autonomy.actuator import (
    MAX_MANAGED_LEAD_SCORE,
    MAX_SCORE_STEP,
    Actuation,
    Bounds,
    Proposal,
    effective_daily_limit,
    effective_min_lead_score,
    evaluate,
)
from titan.autonomy.health import (
    SCALING_REPLY_RATE,
    CampaignHealth,
    CampaignWindow,
    classify,
    explain,
)
from titan.autonomy.manager import ManagedState, confidence_for, plan
from titan.db.enums import CampaignStatus
from titan.delivery.deliverability import MIN_SAMPLE_FOR_RATES, ReputationWindow

BOUNDS = Bounds(configured_daily_limit=40, configured_min_lead_score=70)


def proposal(actuation: Actuation, *, current: int, proposed: int) -> Proposal:
    """A proposal with a reason, because one without is not constructible.

    Deliberately required rather than defaulted: an audit row whose reason is
    empty explains nothing, and the whole value of the table is that a number
    nobody set can be traced back to why.
    """
    return Proposal(
        actuation=actuation,
        campaign_id="c-1",
        current=current,
        proposed=proposed,
        reason="test",
    )


def window(**overrides) -> CampaignWindow:
    """A campaign sending steadily with nothing wrong."""
    base: dict = {
        "campaign_id": "c-1",
        "status": CampaignStatus.ACTIVE,
        "window": ReputationWindow(sent=400, delivered=396, hard_bounced=2, complained=0),
        "contacted": 300,
        "replied": 6,
        "positive_replies": 3,
        "configured_limit": 40,
        "effective_limit": 40,
        "leads_available": 50,
    }
    base.update(overrides)
    return CampaignWindow(**base)


def state(**overrides) -> ManagedState:
    base: dict = {"campaign_id": "c-1", "bounds": BOUNDS}
    base.update(overrides)
    return ManagedState(**base)


# ==========================================================================
# The boundary. These are the tests the feature exists to pass.
# ==========================================================================
@pytest.mark.parametrize("wanted", [41, 100, 10_000])
def test_the_manager_can_never_send_more_than_a_human_approved(wanted: int) -> None:
    verdict = evaluate(
        proposal(Actuation.SET_DAILY_LIMIT, current=40, proposed=wanted),
        BOUNDS,
    )

    assert verdict.applied_value == BOUNDS.configured_daily_limit
    assert verdict.clamped is True


@pytest.mark.parametrize("wanted", [69, 50, 0])
def test_the_manager_can_never_mail_worse_leads_than_a_human_allowed(
    wanted: int,
) -> None:
    """Lowering the bar is the direction that mails people who should not have
    been mailed. It is not available from here at all."""
    verdict = evaluate(
        proposal(Actuation.SET_MIN_LEAD_SCORE, current=70, proposed=wanted),
        BOUNDS,
    )

    assert verdict.applied_value == BOUNDS.configured_min_lead_score


def test_the_bound_holds_even_against_a_value_written_directly() -> None:
    """min and max, not "trust the manager". A managed value above the ceiling
    cannot take effect even if something bypassed the actuator to store it."""
    assert effective_daily_limit(40, 999) == 40
    assert effective_min_lead_score(70, 10) == 70


def test_no_managed_value_means_the_configured_one_stands() -> None:
    assert effective_daily_limit(40, None) == 40
    assert effective_min_lead_score(70, None) == 70


def test_the_actuation_surface_is_three_things() -> None:
    """Adding a member is a deliberate widening of autonomy, and this is the
    test that makes it deliberate.

    The third was added for Phase 05's automatic promotion, and it is the first
    that touches *what a recipient reads* rather than how much or to whom. It is
    the narrowest form of that available: a choice between phrasing registers
    that are already written, reviewed and validated, never a licence to author
    anything. Widening it further -- to compose, to edit, to choose a claim --
    would be a different kind of decision and should fail here first.
    """
    assert {a.value for a in Actuation} == {
        "set_daily_limit",
        "set_min_lead_score",
        "set_promoted_variant",
    }


def test_a_reduction_never_reaches_zero_by_arithmetic() -> None:
    """A campaign cut to nothing looks identical to a paused one and recovers
    from neither. It keeps a trickle, so the outcomes its next decision needs
    keep arriving."""
    assert BOUNDS.floor_daily_limit >= 1

    verdict = evaluate(
        proposal(Actuation.SET_DAILY_LIMIT, current=40, proposed=0), BOUNDS
    )
    assert verdict.applied_value == BOUNDS.floor_daily_limit


def test_the_lead_bar_has_a_ceiling_of_its_own() -> None:
    """Above this almost nothing qualifies and the campaign stops by the back
    door -- a pause, dressed as a threshold."""
    verdict = evaluate(
        proposal(Actuation.SET_MIN_LEAD_SCORE, current=94, proposed=99),
        BOUNDS,
    )
    assert verdict.applied_value <= MAX_MANAGED_LEAD_SCORE


def test_the_bar_cannot_jump_in_one_cycle() -> None:
    """70 to 95 overnight is not tuning, it is switching the campaign off."""
    verdict = evaluate(
        proposal(Actuation.SET_MIN_LEAD_SCORE, current=70, proposed=95),
        BOUNDS,
    )
    assert verdict.applied_value == 70 + MAX_SCORE_STEP


# ==========================================================================
# Health
# ==========================================================================
def test_a_steady_campaign_is_healthy() -> None:
    assert classify(window()) is CampaignHealth.HEALTHY


def test_a_campaign_below_the_sample_floor_is_learning() -> None:
    """Not healthy. Treating it as healthy is how a bad campaign gets scaled on
    four data points."""
    thin = window(
        window=ReputationWindow(
            sent=MIN_SAMPLE_FOR_RATES - 1, delivered=5, hard_bounced=4, complained=0
        ),
        contacted=5,
        replied=0,
    )
    assert classify(thin) is CampaignHealth.LEARNING
    assert "sample floor" in explain(thin, classify(thin))


def test_a_paused_campaign_is_not_judged() -> None:
    assert classify(window(status=CampaignStatus.PAUSED)) is CampaignHealth.PAUSED
    assert classify(window(status=CampaignStatus.COMPLETED)) is CampaignHealth.PAUSED


def test_bouncing_is_degraded() -> None:
    bad = window(
        window=ReputationWindow(sent=400, delivered=300, hard_bounced=40, complained=0)
    )
    assert classify(bad) is CampaignHealth.DEGRADED


def test_a_cut_campaign_reads_as_throttled() -> None:
    """Invisible on a page showing the configured limit, which is the whole
    reason it is a state of its own."""
    assert classify(window(effective_limit=10, replied=0)) is CampaignHealth.THROTTLED


def test_a_performing_campaign_climbing_back_is_scaling_not_throttled() -> None:
    """Both states describe a campaign below its ceiling. What separates them is
    why it is still there -- held down, or on its way back up.

    They overlap exactly, so checking throttled first without distinguishing
    them left SCALING unreachable: a state in the enum that no input could
    produce.
    """
    climbing = window(effective_limit=20, replied=30, positive_replies=30, contacted=300)

    assert climbing.positive_reply_rate >= SCALING_REPLY_RATE
    assert climbing.is_throttled is True
    assert classify(climbing) is CampaignHealth.SCALING


def test_every_health_state_is_reachable() -> None:
    """A state no input can produce is a state nobody can act on."""
    reachable = {
        classify(window(status=CampaignStatus.PAUSED)),
        classify(window(window=ReputationWindow(4, 2, 2, 0), contacted=4, replied=0)),
        classify(window(window=ReputationWindow(400, 300, 40, 0))),
        classify(window(effective_limit=10, replied=0, positive_replies=0)),
        classify(
            window(effective_limit=20, replied=30, positive_replies=30, contacted=300)
        ),
        classify(window()),
    }
    assert reachable == set(CampaignHealth)


def test_no_leads_means_no_scaling() -> None:
    """More volume with nothing to send is a decision with no effect and a row
    in the audit trail claiming otherwise."""
    assert window(leads_available=0, effective_limit=20).has_headroom is False


# ==========================================================================
# What the manager proposes
# ==========================================================================
def test_a_learning_campaign_is_left_alone() -> None:
    thin = window(
        window=ReputationWindow(sent=4, delivered=2, hard_bounced=2, complained=0),
        contacted=4,
        replied=0,
    )
    assert plan(state(), thin) == []


def test_a_paused_campaign_is_left_alone() -> None:
    assert plan(state(), window(status=CampaignStatus.PAUSED)) == []


def test_a_degrading_campaign_is_made_choosier() -> None:
    bad = window(
        window=ReputationWindow(sent=400, delivered=300, hard_bounced=40, complained=0)
    )
    proposals = plan(state(), bad)

    assert [p.actuation for p in proposals] == [Actuation.SET_MIN_LEAD_SCORE]
    assert proposals[0].proposed > 70


def test_volume_is_not_decided_here() -> None:
    """One authority per knob.

    How much a campaign sends became a question about every campaign at once as
    soon as they shared a workspace limit, and titan.autonomy.allocation answers
    it. Two authorities writing the same column would fight over it every cycle.
    """
    cases = [
        window(),
        window(effective_limit=10),
        window(window=ReputationWindow(400, 300, 40, 0)),
        window(effective_limit=20, replied=30, contacted=300),
    ]
    for case in cases:
        for st in (state(), state(managed_daily_limit=10)):
            assert all(
                p.actuation is not Actuation.SET_DAILY_LIMIT for p in plan(st, case)
            )


def test_a_healthy_campaign_at_its_configured_bar_proposes_nothing() -> None:
    assert plan(state(), window()) == []


def test_the_bar_is_returned_in_one_step() -> None:
    """A bar left too high costs qualified leads and protects nothing, so there
    is no reason to walk it down slowly."""
    proposals = plan(state(managed_min_lead_score=85), window())
    score = next(p for p in proposals if p.actuation is Actuation.SET_MIN_LEAD_SCORE)

    assert score.proposed == BOUNDS.configured_min_lead_score


def test_every_proposal_carries_its_evidence_and_a_reason() -> None:
    """The audit trail is only worth having if the row explains itself."""
    bad = window(
        window=ReputationWindow(sent=400, delivered=300, hard_bounced=40, complained=0)
    )
    for proposal in plan(state(), bad):
        assert proposal.reason
        assert proposal.evidence["sent"] == 400
        assert proposal.evidence["health"] == CampaignHealth.DEGRADED.value
        assert 0.0 <= proposal.confidence <= 1.0


def test_confidence_grows_with_the_sample_and_is_never_acted_on() -> None:
    """A threshold on confidence would be a second policy, unstated and
    interacting with the first in ways nobody had reasoned about."""
    small = confidence_for(window(window=ReputationWindow(10, 10, 0, 0)))
    large = confidence_for(window(window=ReputationWindow(1000, 1000, 0, 0)))

    assert small < large
    assert large == 1.0
    assert confidence_for(window(window=ReputationWindow(0, 0, 0, 0))) == 0.0


# ==========================================================================
# What earns a campaign more volume
# ==========================================================================
def test_a_campaign_drawing_rejections_does_not_earn_more_volume() -> None:
    """The defect this guards. Scaling used to read ``replied``, so a campaign
    whose recipients all wrote back to say no looked like the best performer in
    the workspace and was handed a larger share of the daily budget."""
    rejected = window(effective_limit=20, contacted=300, replied=60, positive_replies=1)

    assert rejected.reply_rate >= SCALING_REPLY_RATE, "the setup is wrong"
    assert rejected.positive_reply_rate < SCALING_REPLY_RATE
    assert classify(rejected) is CampaignHealth.THROTTLED


def test_the_two_reply_rates_are_equal_only_when_nobody_said_no() -> None:
    """They diverge exactly on the campaigns that most need to stop growing."""
    clean = window(contacted=300, replied=30, positive_replies=30)
    mixed = window(contacted=300, replied=30, positive_replies=4)

    assert clean.reply_rate == clean.positive_reply_rate
    assert mixed.reply_rate > mixed.positive_reply_rate


def test_an_operator_can_see_both_numbers() -> None:
    """ "12 replies from 200 contacted" reads as healthy; the same campaign with
    one positive is a different decision, and the first line cannot show it."""
    mixed = window(contacted=300, replied=30, positive_replies=2, meetings_booked=1)

    line = explain(mixed, classify(mixed))

    assert "30 repl(ies)" in line
    assert "2 positive" in line
    assert "1 booked" in line
