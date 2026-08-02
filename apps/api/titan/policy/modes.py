"""Operating modes and the capabilities each one grants.

Mission section 3. Four modes, strictly ordered by permissiveness:

    research_only  <  draft_only  <  approval_required  <  controlled_autopilot

The **effective** mode for any action is the minimum of three independently
configured values -- process (env), workspace (DB), and campaign (DB). A
workspace cannot exceed the process ceiling, and a campaign cannot exceed the
workspace ceiling. This is what makes "one casual boolean cannot activate live
outreach" true structurally rather than by convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from titan.config import MODE_RANK, OperatingMode


class Capability(StrEnum):
    """A discrete thing Titan might do, in increasing order of consequence."""

    DISCOVER_LEADS = "discover_leads"
    CRAWL_WEBSITE = "crawl_website"
    STORE_EVIDENCE = "store_evidence"
    SCORE_LEAD = "score_lead"
    RECOMMEND_INTERNALLY = "recommend_internally"
    GENERATE_DRAFT = "generate_draft"
    REQUEST_APPROVAL = "request_approval"
    AUTO_APPROVE = "auto_approve"
    QUEUE_MESSAGE = "queue_message"
    DELIVER_MESSAGE = "deliver_message"


#: Capabilities each mode grants. Note that AUTO_APPROVE exists only in
#: controlled_autopilot -- in approval_required a human decision is the only
#: path from draft to outbox.
MODE_CAPABILITIES: dict[OperatingMode, frozenset[Capability]] = {
    OperatingMode.RESEARCH_ONLY: frozenset(
        {
            Capability.DISCOVER_LEADS,
            Capability.CRAWL_WEBSITE,
            Capability.STORE_EVIDENCE,
            Capability.SCORE_LEAD,
            Capability.RECOMMEND_INTERNALLY,
        }
    ),
    OperatingMode.DRAFT_ONLY: frozenset(
        {
            Capability.DISCOVER_LEADS,
            Capability.CRAWL_WEBSITE,
            Capability.STORE_EVIDENCE,
            Capability.SCORE_LEAD,
            Capability.RECOMMEND_INTERNALLY,
            Capability.GENERATE_DRAFT,
        }
    ),
    OperatingMode.APPROVAL_REQUIRED: frozenset(
        {
            Capability.DISCOVER_LEADS,
            Capability.CRAWL_WEBSITE,
            Capability.STORE_EVIDENCE,
            Capability.SCORE_LEAD,
            Capability.RECOMMEND_INTERNALLY,
            Capability.GENERATE_DRAFT,
            Capability.REQUEST_APPROVAL,
            Capability.QUEUE_MESSAGE,
            Capability.DELIVER_MESSAGE,
        }
    ),
    OperatingMode.CONTROLLED_AUTOPILOT: frozenset(
        {
            Capability.DISCOVER_LEADS,
            Capability.CRAWL_WEBSITE,
            Capability.STORE_EVIDENCE,
            Capability.SCORE_LEAD,
            Capability.RECOMMEND_INTERNALLY,
            Capability.GENERATE_DRAFT,
            Capability.REQUEST_APPROVAL,
            Capability.AUTO_APPROVE,
            Capability.QUEUE_MESSAGE,
            Capability.DELIVER_MESSAGE,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class EffectiveMode:
    mode: OperatingMode
    #: Which layer imposed the ceiling -- shown in the UI so an operator can
    #: see *why* Titan will not send, rather than guessing.
    limited_by: str
    process_mode: OperatingMode
    workspace_mode: OperatingMode
    campaign_mode: OperatingMode

    def allows(self, capability: Capability) -> bool:
        return capability in MODE_CAPABILITIES[self.mode]

    @property
    def can_send(self) -> bool:
        return self.allows(Capability.DELIVER_MESSAGE)


def resolve_mode(
    process_mode: OperatingMode,
    workspace_mode: OperatingMode,
    campaign_mode: OperatingMode,
) -> EffectiveMode:
    """The most restrictive of the three configured modes wins."""
    candidates = (
        ("process", process_mode),
        ("workspace", workspace_mode),
        ("campaign", campaign_mode),
    )
    limited_by, mode = min(candidates, key=lambda pair: MODE_RANK[pair[1]])
    return EffectiveMode(
        mode=mode,
        limited_by=limited_by,
        process_mode=process_mode,
        workspace_mode=workspace_mode,
        campaign_mode=campaign_mode,
    )


def requires_human_approval(mode: OperatingMode) -> bool:
    """Whether a draft needs an explicit human decision before queueing."""
    return mode is OperatingMode.APPROVAL_REQUIRED


__all__ = [
    "MODE_CAPABILITIES",
    "Capability",
    "EffectiveMode",
    "requires_human_approval",
    "resolve_mode",
]
