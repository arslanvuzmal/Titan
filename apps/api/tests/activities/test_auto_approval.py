"""Whether a draft needs a human, read from the database at execution time.

Autopilot is permission to approve without a human; it is not an instruction to.
The effective mode is the minimum of process, workspace and campaign, and the
process ceiling comes from the global sending kill switch -- so before the
per-campaign opt-in existed, turning production sending on would have dropped
the human gate from every campaign at once, which is a decision nobody made per
campaign.

Against a real PostgreSQL: the whole point of the activity is that it reads the
workspace and campaign rows rather than trusting the request.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select
from titan.activities.research import requires_human_approval
from titan.config import OperatingMode, Settings
from titan.db.enums import CampaignStatus, Industry
from titan.db.models import Campaign, CampaignPolicy, Workspace
from titan.db.session import get_sessionmaker
from titan.workflows.types import ResearchLeadInput

pytestmark = pytest.mark.asyncio


def autopilot_settings() -> Settings:
    """A process ceiling of controlled_autopilot -- the kill switch is off."""
    return Settings(environment="test", production_sending_enabled=True)


async def seed(
    workspace_id: uuid.UUID,
    *,
    suffix: str,
    workspace_mode: OperatingMode,
    campaign_mode: OperatingMode,
    auto_approve: bool,
) -> uuid.UUID:
    async with get_sessionmaker()() as session, session.begin():
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.operating_mode = workspace_mode

        campaign = Campaign(
            workspace_id=workspace_id,
            name=f"Approval {suffix}",
            slug=f"approval-{suffix}",
            status=CampaignStatus.ACTIVE,
            industry=Industry.DENTIST,
        )
        session.add(campaign)
        await session.flush()
        session.add(
            CampaignPolicy(
                workspace_id=workspace_id,
                campaign_id=campaign.id,
                operating_mode=campaign_mode,
                auto_approve=auto_approve,
            )
        )
        return campaign.id


def request_for(workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> ResearchLeadInput:
    return ResearchLeadInput(
        workspace_id=str(workspace_id),
        campaign_id=str(campaign_id),
        lead_id=str(uuid.uuid4()),
        run_key="run-1",
    )


async def test_autopilot_and_opted_in_needs_no_human(workspace) -> None:
    campaign_id = await seed(
        workspace,
        suffix="opted-in",
        workspace_mode=OperatingMode.CONTROLLED_AUTOPILOT,
        campaign_mode=OperatingMode.CONTROLLED_AUTOPILOT,
        auto_approve=True,
    )

    with patch("titan.activities.research.get_settings", autopilot_settings):
        assert await requires_human_approval(request_for(workspace, campaign_id)) is False


async def test_autopilot_without_the_opt_in_still_needs_a_human(workspace) -> None:
    """The default every campaign that already exists was migrated with."""
    campaign_id = await seed(
        workspace,
        suffix="not-opted-in",
        workspace_mode=OperatingMode.CONTROLLED_AUTOPILOT,
        campaign_mode=OperatingMode.CONTROLLED_AUTOPILOT,
        auto_approve=False,
    )

    with patch("titan.activities.research.get_settings", autopilot_settings):
        assert await requires_human_approval(request_for(workspace, campaign_id)) is True


async def test_opting_in_does_not_climb_the_mode_ladder(workspace) -> None:
    """The flag is the second half of an AND. It cannot buy autopilot."""
    campaign_id = await seed(
        workspace,
        suffix="approval-required",
        workspace_mode=OperatingMode.CONTROLLED_AUTOPILOT,
        campaign_mode=OperatingMode.APPROVAL_REQUIRED,
        auto_approve=True,
    )

    with patch("titan.activities.research.get_settings", autopilot_settings):
        assert await requires_human_approval(request_for(workspace, campaign_id)) is True


async def test_a_policy_written_without_an_opinion_keeps_the_human_gate(
    workspace,
) -> None:
    """The closed position is the default, so the migration changed no behaviour.

    Written without naming ``auto_approve`` at all -- which is how every policy
    row that already existed was migrated, and how any code path that predates
    the column still writes one.
    """
    async with get_sessionmaker()() as session, session.begin():
        campaign = Campaign(
            workspace_id=workspace,
            name="Approval default",
            slug="approval-default",
            status=CampaignStatus.ACTIVE,
            industry=Industry.DENTIST,
        )
        session.add(campaign)
        await session.flush()
        policy = CampaignPolicy(
            workspace_id=workspace,
            campaign_id=campaign.id,
            operating_mode=OperatingMode.CONTROLLED_AUTOPILOT,
        )
        session.add(policy)
        campaign_id = campaign.id

    async with get_sessionmaker()() as session:
        stored = (
            await session.execute(
                select(CampaignPolicy).where(CampaignPolicy.campaign_id == campaign_id)
            )
        ).scalar_one()
        assert stored.auto_approve is False

    with patch("titan.activities.research.get_settings", autopilot_settings):
        assert await requires_human_approval(request_for(workspace, campaign_id)) is True
