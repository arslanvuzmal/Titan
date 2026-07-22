from temporalio import workflow
from datetime import timedelta
from typing import Any, Dict

with workflow.unsafe.imports_passed_through():
    from app.agents.orchestrator import titan_orchestrator_graph
    from app.agents.schemas import TitanAgentState


@workflow.defn
class AgentExecutionWorkflow:
    """
    Executes the LangGraph Agent Orchestrator.
    Handles the transition between synchronous LLM execution and durable Temporal sleep for HITL.
    """

    def __init__(self):
        self.is_approved = False
        self.is_rejected = False

    @workflow.signal
    def approve(self) -> None:
        self.is_approved = True

    @workflow.signal
    def reject(self) -> None:
        self.is_rejected = True

    @workflow.run
    async def run(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        workflow.logger.info(
            f"Starting LangGraph agent execution for event: {event_data.get('event_id')}"
        )

        # 1. Initialize LangGraph State
        initial_state: TitanAgentState = {
            "task_id": event_data.get("event_id", "unknown_task"),
            "organization_id": event_data.get("organization_id", "unknown_org"),
            "event": event_data,
            "current_step": "start",
            "agent_history": [],
            "pending_actions": [],
            "error_state": None,
            "is_complete": False,
        }

        # 2. Execute the Graph using an Activity (LangChain/LangGraph calls must be in activities
        #    since they make external HTTP calls and are non-deterministic, but for this skeleton
        #    where we are mocking LLMs without network calls, we can run it directly.
        #    In production, wrap `titan_orchestrator_graph.ainvoke` in an @activity.defn).

        # MOCKING HTTP ACTIVITY WRAPPER FOR NOW
        final_state = await titan_orchestrator_graph.ainvoke(initial_state)

        # 3. Check for Human-in-the-Loop requirement
        if final_state.get("current_step") == "PENDING_APPROVAL":
            workflow.logger.info(
                "Graph yielded PENDING_APPROVAL. Pausing workflow for HITL signal."
            )

            # Wait until a human sends the `approve` or `reject` signal from the dashboard
            await workflow.wait_condition(
                lambda: self.is_approved or self.is_rejected, timeout=timedelta(days=7)
            )

            if self.is_approved:
                workflow.logger.info("Action Approved. Resuming execution.")
                final_state["agent_history"].append("Human approved actions.")
                final_state["is_complete"] = True
            elif self.is_rejected:
                workflow.logger.info("Action Rejected. Aborting execution.")
                final_state["agent_history"].append("Human rejected actions.")
                final_state["error_state"] = "Action rejected by human."
                final_state["is_complete"] = True

        return {
            "status": "completed" if not final_state.get("error_state") else "failed",
            "task_id": final_state["task_id"],
            "agent_history": final_state["agent_history"],
            "pending_actions": final_state["pending_actions"],
            "error_state": final_state["error_state"],
        }
