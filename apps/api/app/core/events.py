from pydantic import BaseModel, Field
from typing import Any, Dict, Optional
from datetime import datetime
import uuid

class TitanEvent(BaseModel):
    """
    Normalized core event schema. All incoming events (webhooks, API calls, internal triggers)
    must be normalized to this structure before being passed to the Temporal orchestrator.
    """
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    organization_id: str
    source: str
    event_type: str
    payload: Dict[str, Any]
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    correlation_id: Optional[str] = None

class EventDispatcher:
    """
    Handles validation, normalization, and dispatching of events.
    In a real production environment, this might push to a robust queue.
    Here we directly bridge to Temporal by starting the main orchestrator workflow.
    """
    
    @staticmethod
    async def dispatch(event: TitanEvent):
        """
        Dispatches the event to the Temporal orchestrator workflow.
        We lazily import Temporal client to avoid circular dependencies or initialization issues.
        """
        from temporalio.client import Client
        import os
        
        # Connect to temporal. In production, this client should be a persistent singleton.
        temporal_host = os.getenv("TEMPORAL_HOST", "localhost:7233")
        try:
            client = await Client.connect(temporal_host)
            
            # Start the main orchestrator workflow for this event.
            # We use the event_id as the workflow ID to ensure deduplication.
            await client.start_workflow(
                "TitanOrchestratorWorkflow",
                event.model_dump(mode="json"),
                id=f"orchestrator-{event.event_id}",
                task_queue="titan-task-queue",
            )
            return {"status": "dispatched", "event_id": event.event_id}
        except Exception as e:
            # Fallback/Dead-letter queue logic would go here
            raise Exception(f"Failed to dispatch event to Temporal: {str(e)}")
