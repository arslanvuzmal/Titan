from pydantic import BaseModel
from typing import Any, Dict, Literal

class ActionRequest(BaseModel):
    """An action an agent wants to perform."""
    tool_name: str
    parameters: Dict[str, Any]
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    reasoning: str  # Why the agent wants to do this
    expected_outcome: str

class ApprovalDecision(BaseModel):
    """Human decision on an action."""
    action_id: str
    decision: Literal["APPROVED", "REJECTED", "EDITED"]
    modified_parameters: Dict[str, Any] | None = None
    comments: str | None = None
