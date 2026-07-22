from datetime import timedelta
from temporalio import workflow, activity
from temporalio.common import RetryPolicy

# Import activity definitions. Since we define the activities inside the worker/app,
# we use temporalio's proxy imports or declare signatures.
with workflow.unsafe.imports_passed_through():
    from app.core.websocket import manager

from app.core.websocket import manager
from typing import Optional, Dict, Any


# Dummy activities for the Golden Path (These would normally be in a separate activities.py)
@activity.defn
async def ingest_and_validate_lead(payload: dict) -> dict:
    # 1-2. A new lead enters TITAN, normalizes event
    # In a real app, we would insert into the database here.
    return {"lead_id": "lead_123", "status": "validated", "data": payload}


@activity.defn
async def execute_sales_agent_graph(lead_data: dict) -> dict:
    # 4-9. The LangGraph agent analyzes, researches (RAG), scores, and generates email
    # Mocking the langgraph execution for the demo
    return {
        "score": 85,
        "reasoning": "High match with ICP. Recent funding round detected.",
        "proposed_email": "Hi Jane, noticed Acme Corp recently raised Series B...",
    }


@activity.defn
async def execute_approved_actions(email_draft: str) -> dict:
    # 12-14. Send email, update CRM, schedule follow-up
    return {"status": "success", "crm_id": "crm_456"}


@activity.defn
async def finalize_and_audit(results: dict) -> dict:
    # 15. Log every action immutably
    return {"status": "audited"}


@activity.defn
async def emit_workflow_update(org_id: str, step_data: dict) -> None:
    # Helper activity to push websocket updates
    await manager.broadcast_to_org(org_id, step_data)


@workflow.defn
class SalesPipelineWorkflow:
    @workflow.run
    async def run(self, event_data: dict) -> dict:
        org_id = event_data.get("organization_id", "demo-org")

        # Helper to emit steps
        async def emit(
            step: int, name: str, status: str, payload: Optional[Dict[str, Any]] = None
        ):
            await workflow.execute_activity(
                emit_workflow_update,
                args=[
                    org_id,
                    {
                        "step_number": step,
                        "step_name": name,
                        "status": status,
                        "payload": payload or {},
                    },
                ],
                start_to_close_timeout=timedelta(seconds=5),
            )

        # Steps 1-2
        await emit(1, "Receive Lead", "running")
        lead_result = await workflow.execute_activity(
            ingest_and_validate_lead,
            args=[event_data.get("payload", {})],
            start_to_close_timeout=timedelta(seconds=10),
        )
        await emit(1, "Receive Lead", "completed", lead_result)
        await emit(2, "Normalize Event", "completed", lead_result)

        # Step 3
        await emit(3, "Create Task & Orchestrate", "completed")

        # Steps 4-9
        await emit(4, "Analyze Lead", "running")
        # Simulating sub-steps via LangGraph activity
        agent_result = await workflow.execute_activity(
            execute_sales_agent_graph,
            args=[lead_result],
            start_to_close_timeout=timedelta(minutes=2),
        )
        await emit(4, "Analyze Lead", "completed", agent_result)
        await emit(5, "Research Company (RAG)", "completed", {"sources": 3})
        await emit(6, "Retrieve Business Context", "completed")
        await emit(
            7, "Generate Lead Score", "completed", {"score": agent_result["score"]}
        )
        await emit(
            8, "Explain Score", "completed", {"reasoning": agent_result["reasoning"]}
        )
        await emit(
            9,
            "Generate Outreach Email",
            "completed",
            {"draft": agent_result["proposed_email"]},
        )

        # Step 10: HITL
        await emit(
            10,
            "Awaiting Human Approval",
            "paused",
            {"draft": agent_result["proposed_email"]},
        )

        # Wait for signal (Human-in-the-Loop)
        await workflow.wait_condition(
            lambda: getattr(self, "hitl_decision", None) is not None
        )

        if self.hitl_decision == "REJECTED":
            await emit(
                10, "Awaiting Human Approval", "failed", {"reason": "User rejected."}
            )
            return {"status": "rejected"}

        await emit(11, "User Approved Email", "completed", {"decision": "APPROVED"})

        # Steps 12-14
        await emit(12, "Send Email via Tool", "running")
        try:
            action_result = await workflow.execute_activity(
                execute_approved_actions,
                args=[agent_result["proposed_email"]],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            await emit(12, "Send Email via Tool", "completed")
            await emit(13, "Update CRM Record", "completed")
            await emit(14, "Schedule Follow-up Event", "completed")
        except Exception as e:
            await emit(12, "Send Email via Tool", "failed", {"error": str(e)})
            # Partial failure handling: continue to audit
            action_result = {"status": "partial_failure"}

        # Step 15
        await emit(15, "Log Actions Immutably", "running")
        await workflow.execute_activity(
            finalize_and_audit,
            args=[action_result],
            start_to_close_timeout=timedelta(seconds=10),
        )
        await emit(15, "Log Actions Immutably", "completed")

        # Step 16
        await emit(16, "Execution Complete", "completed")

        return {"status": "success", "final_state": action_result}

    @workflow.signal(name="hitl-approval-signal")
    async def process_approval(self, decision: str) -> None:
        self.hitl_decision = decision
