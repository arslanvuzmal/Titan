from temporalio import workflow
from datetime import timedelta
from typing import Any, Dict

@workflow.defn
class ApprovalWorkflow:
    """
    Workflow that pauses and waits for a human-in-the-loop signal.
    """
    def __init__(self) -> None:
        self.is_approved: bool = False
        self.is_rejected: bool = False

    @workflow.signal
    def approve(self) -> None:
        self.is_approved = True

    @workflow.signal
    def reject(self) -> None:
        self.is_rejected = True

    @workflow.run
    async def run(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        workflow.logger.info(f"Waiting for approval for event: {event_data.get('event_id')}")
        
        # In a real app, we might trigger an email or Slack notification here via an Activity.
        
        # Pause execution until one of the signals sets the flag
        # Uses minimal compute resources while waiting indefinitely or up to a timeout
        await workflow.wait_condition(
            lambda: self.is_approved or self.is_rejected,
            timeout=timedelta(days=7) # Wait up to 7 days
        )
        
        if self.is_approved:
            # Proceed with the execution
            return {"status": "approved", "event_id": event_data.get('event_id')}
        elif self.is_rejected:
            # Abort
            return {"status": "rejected", "event_id": event_data.get('event_id')}
        else:
            # Timeout
            return {"status": "timeout", "event_id": event_data.get('event_id')}
