from temporalio import workflow
from typing import Any, Dict

with workflow.unsafe.imports_passed_through():
    from app.core.events import TitanEvent


@workflow.defn
class TitanOrchestratorWorkflow:
    """
    Main orchestrator workflow that receives a normalized TitanEvent
    and decides which specific sub-workflows or activities to execute.
    """

    @workflow.run
    async def run(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        # Validate the input dict back into our Pydantic model for type safety
        event = TitanEvent(**event_data)

        workflow.logger.info(
            f"Orchestrating event: {event.event_id} of type {event.event_type}"
        )

        # Routing Logic
        if event.event_type.startswith("agent."):
            # Delegate to the Agent Execution sub-workflow
            return await workflow.execute_child_workflow(
                "AgentExecutionWorkflow",
                event_data,
                id=f"agent-execution-{event.event_id}",
                task_queue="titan-task-queue",
            )

        elif event.event_type.startswith("approval."):
            # Delegate to the Human-in-the-loop Approval Workflow
            return await workflow.execute_child_workflow(
                "ApprovalWorkflow",
                event_data,
                id=f"approval-{event.event_id}",
                task_queue="titan-task-queue",
            )

        else:
            # Generic event processing (perhaps an activity that simply logs it to DB)
            return {"status": "unhandled_event_type", "event_id": event.event_id}
