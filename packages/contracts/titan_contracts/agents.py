from pydantic import BaseModel, Field
from typing import Any, Dict, Literal
from .events import TitanEvent

class AgentContext(BaseModel):
    """Context provided to an agent before execution."""
    task_id: str
    organization_id: str
    event: TitanEvent
    retrieved_memories: list[dict] = Field(default_factory=list)
    retrieved_documents: list[dict] = Field(default_factory=list)

class AgentOutput(BaseModel):
    """Standardized output from any agent."""
    agent_type: str
    status: Literal["SUCCESS", "FAILED", "REQUIRES_HUMAN"]
    summary: str
    data: Dict[str, Any] = Field(default_factory=dict)
    confidence_score: float = Field(ge=0.0, le=1.0)
