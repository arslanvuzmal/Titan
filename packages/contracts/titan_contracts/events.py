from pydantic import BaseModel, Field
from typing import Any, Dict, Literal
from datetime import datetime
from uuid import uuid4

class TitanEvent(BaseModel):
    """The universal envelope for all incoming business events."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    organization_id: str
    source: str  # e.g., "gmail", "hubspot"
    type: str    # e.g., "lead.created"
    payload: Dict[str, Any]
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
