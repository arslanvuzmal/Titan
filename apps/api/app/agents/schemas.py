from pydantic import BaseModel, Field, model_validator
from typing import List, Dict, Any, Optional, TypedDict


class ActionRequest(BaseModel):
    """
    Represents an action an agent wants to perform.
    Agents do not execute actions themselves; they return these requests.
    """

    tool_name: str = Field(..., description="The exact name of the tool to execute.")
    arguments: Dict[str, Any] = Field(
        default_factory=dict, description="JSON arguments for the tool."
    )
    requires_approval: bool = Field(
        default=False,
        description="Whether this action requires a human to approve it first.",
    )


class SalesAgentOutput(BaseModel):
    """
    Output schema for the Sales Intelligence Agent.
    Must adhere strictly to these constraints.
    """

    lead_score: int = Field(
        ..., ge=0, le=100, description="Lead score between 0 and 100."
    )
    score_factors: List[str] = Field(
        ...,
        description="List of positive or negative factors contributing to the score.",
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence in the lead score and draft."
    )
    recommended_action: str = Field(
        ...,
        description="High-level recommendation (e.g., 'Fast-track to AE', 'Nurture').",
    )
    drafted_email: Optional[str] = Field(
        None, description="A drafted outreach email, if applicable."
    )
    action_requests: List[ActionRequest] = Field(
        default_factory=list,
        description="Requested actions, e.g., updating CRM or sending the email.",
    )

    @model_validator(mode="after")
    def validate_approval_for_emails(self):
        # Enforce that if a tool sends an email, it MUST require approval
        for action in self.action_requests:
            if action.tool_name == "send_email" and not action.requires_approval:
                raise ValueError("Sending an email always requires human approval.")
        return self


class ResearchAgentOutput(BaseModel):
    """
    Output schema for the Research Agent.
    """

    executive_summary: str = Field(
        ..., description="A short summary of the target company."
    )
    key_findings: List[str] = Field(
        ..., description="List of important news, tech stack, or funding events."
    )
    business_implications: str = Field(
        ..., description="How this research affects our go-to-market motion."
    )
    action_requests: List[ActionRequest] = Field(default_factory=list)


class TitanAgentState(TypedDict):
    """
    LangGraph State representing the active workflow execution.
    """

    task_id: str
    organization_id: str
    event: Dict[str, Any]
    current_step: str
    agent_history: List[str]
    pending_actions: List[Dict[str, Any]]
    error_state: Optional[str]
    is_complete: bool
