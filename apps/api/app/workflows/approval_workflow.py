from temporalio import workflow
from datetime import timedelta
from typing import Any, Dict, Optional


@workflow.defn
class ApprovalWorkflow:
    """
    Workflow that pauses and waits for a human-in-the-loop signal.
    """

    def __init__(self) -> None:
        self.decision: Optional[str] = None
        self.edited_parameters: Optional[Dict[str, Any]] = None
        self.reason: Optional[str] = None

    @workflow.signal(name="hitl-approval-signal")
    def hitl_approval_signal(self, payload: Dict[str, Any]) -> None:
        self.decision = payload.get("decision")
        self.edited_parameters = payload.get("edited_parameters")
        self.reason = payload.get("reason")

    @workflow.run
    async def run(self, action_req: Any) -> Dict[str, Any]:
        workflow.logger.info("Waiting for approval")

        # In a real app, we might trigger an email or Slack notification here via an Activity.

        # Pause execution until the signal sets the decision
        await workflow.wait_condition(
            lambda: self.decision is not None,
            timeout=timedelta(days=7),  # Wait up to 7 days
        )

        return {
            "decision": self.decision,
            "original_action": action_req,
            "edited_parameters": self.edited_parameters,
            "reason": self.reason,
        }
