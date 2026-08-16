"""The row that joins four written steps to the scheduler that walks them.

``titan.outreach.sequence`` has held the four steps since it was ported and
``titan.delivery.followup_scheduler`` has known how to walk them, and no code
path had ever created the ``email_sequences`` row between them. The scheduler's
lookup returned None for every campaign, so every lead was contacted once and
three quarters of the outreach existed only as tested functions.

These tests are about the join, not the wording: that a campaign gets steps, and
that provisioning a campaign twice does not give it two sequences.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from titan.db.enums import CampaignStatus, Industry
from titan.db.models import Campaign, EmailSequence, SequenceStep
from titan.outreach.provisioning import (
    REQUIRES_NEW_EVIDENCE,
    ensure_sequence,
)
from titan.outreach.sequence import STEP_DELAYS_IN_DAYS, TEMPLATE_KEYS

pytestmark = pytest.mark.asyncio


async def _campaign(session, workspace_id, *, slug: str) -> uuid.UUID:
    campaign = Campaign(
        workspace_id=workspace_id,
        name=slug,
        slug=slug,
        industry=Industry.DENTIST,
        status=CampaignStatus.DRAFT,
    )
    session.add(campaign)
    await session.flush()
    return campaign.id


async def _steps(session, sequence_id) -> list[SequenceStep]:
    rows = (
        (
            await session.execute(
                select(SequenceStep).where(SequenceStep.sequence_id == sequence_id)
            )
        )
        .scalars()
        .all()
    )
    return sorted(rows, key=lambda s: s.step_number)


async def test_a_campaign_gets_the_four_steps(db_session, workspace) -> None:
    campaign_id = await _campaign(db_session, workspace, slug="provision-one")

    sequence = await ensure_sequence(
        db_session, workspace_id=workspace, campaign_id=campaign_id
    )

    assert sequence is not None
    steps = await _steps(db_session, sequence.id)
    assert [s.step_number for s in steps] == [1, 2, 3, 4]


async def test_the_cadence_comes_from_the_module_that_declares_it(
    db_session, workspace
) -> None:
    """Not typed a second time.

    A copy of the delays in SQL or in this function would be free to drift from
    the wording it names, and the drift would show up as a follow-up arriving on
    the wrong day rather than as a failure.
    """
    campaign_id = await _campaign(db_session, workspace, slug="provision-cadence")

    sequence = await ensure_sequence(
        db_session, workspace_id=workspace, campaign_id=campaign_id
    )
    steps = await _steps(db_session, sequence.id)

    assert [s.delay_days for s in steps] == list(STEP_DELAYS_IN_DAYS)
    assert [s.template_key for s in steps] == list(TEMPLATE_KEYS)


async def test_the_nudge_is_the_one_step_not_required_to_carry_new_evidence(
    db_session, workspace
) -> None:
    """``compose_follow_up_1`` takes no variables, so it cannot cite anything.

    Requiring new evidence of it would make it permanently unsendable. The other
    two follow-ups do take findings, and requiring it of them is what stops a
    follow-up being the first message again in different words.
    """
    campaign_id = await _campaign(db_session, workspace, slug="provision-evidence")

    sequence = await ensure_sequence(
        db_session, workspace_id=workspace, campaign_id=campaign_id
    )
    steps = await _steps(db_session, sequence.id)

    assert [s.requires_new_evidence for s in steps] == list(REQUIRES_NEW_EVIDENCE)
    assert steps[1].requires_new_evidence is False
    assert steps[2].requires_new_evidence is True
    assert steps[3].requires_new_evidence is True


async def test_provisioning_twice_leaves_one_sequence(db_session, workspace) -> None:
    """The backfill runs over campaigns the create path already provisioned.

    A second sequence would not merely be untidy: the scheduler takes the first
    active one it finds, so which steps a campaign follows would depend on row
    order.
    """
    campaign_id = await _campaign(db_session, workspace, slug="provision-twice")

    first = await ensure_sequence(
        db_session, workspace_id=workspace, campaign_id=campaign_id
    )
    second = await ensure_sequence(
        db_session, workspace_id=workspace, campaign_id=campaign_id
    )

    assert first is not None
    assert second is None, "an existing sequence must be kept, not replaced"

    count = len(
        (
            await db_session.execute(
                select(EmailSequence).where(EmailSequence.campaign_id == campaign_id)
            )
        )
        .scalars()
        .all()
    )
    assert count == 1


async def test_two_campaigns_get_their_own_sequences(db_session, workspace) -> None:
    one = await _campaign(db_session, workspace, slug="provision-a")
    two = await _campaign(db_session, workspace, slug="provision-b")

    first = await ensure_sequence(db_session, workspace_id=workspace, campaign_id=one)
    second = await ensure_sequence(db_session, workspace_id=workspace, campaign_id=two)

    assert first is not None and second is not None
    assert first.id != second.id
