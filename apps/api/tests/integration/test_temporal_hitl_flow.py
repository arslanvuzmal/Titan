import pytest
import uuid
import asyncio
from temporalio.client import Client
from temporalio.worker import Worker
from app.workflows.approval_workflow import ApprovalWorkflow
from app.agents.schemas import ActionRequest


@pytest.mark.asyncio
async def test_temporal_hitl_approval_flow(temporal_client: Client):
    """
    Spins up a real Temporal Worker in the test environment, executes the
    ApprovalWorkflow, sends an APPROVE signal, and asserts it completes.
    """
    task_queue = f"test-queue-{uuid.uuid4()}"

    # Setup the worker with our workflow
    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[ApprovalWorkflow],
    ):
        workflow_id = f"test-wf-{uuid.uuid4()}"

        # 1. Start Workflow (this will pause waiting for the signal)
        action_req = ActionRequest(
            tool_name="send_email",
            arguments={"to_email": "test@x.com"},
            requires_approval=True,
        )

        handle = await temporal_client.start_workflow(
            ApprovalWorkflow.run, action_req, id=workflow_id, task_queue=task_queue
        )

        # Allow workflow to start and reach the paused await state
        await asyncio.sleep(0.5)

        # 2. Simulate User Approval via Signal
        await handle.signal(
            "hitl-approval-signal",
            {"decision": "APPROVED", "edited_parameters": None, "reason": None},
        )

        # 3. Await workflow completion
        result = await handle.result()

        # Assert the workflow returned the correct decision
        assert result["decision"] == "APPROVED"
        assert result["original_action"]["tool_name"] == "send_email"


@pytest.mark.asyncio
async def test_temporal_hitl_rejection_flow(temporal_client: Client):
    """
    Tests the REJECTED path, ensuring the workflow exits cleanly with a rejection status.
    """
    task_queue = f"test-queue-{uuid.uuid4()}"

    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[ApprovalWorkflow],
    ):
        workflow_id = f"test-wf-{uuid.uuid4()}"
        action_req = ActionRequest(
            tool_name="delete_database", arguments={}, requires_approval=True
        )

        handle = await temporal_client.start_workflow(
            ApprovalWorkflow.run, action_req, id=workflow_id, task_queue=task_queue
        )

        await asyncio.sleep(0.5)

        await handle.signal(
            "hitl-approval-signal",
            {
                "decision": "REJECTED",
                "edited_parameters": None,
                "reason": "Too dangerous",
            },
        )

        result = await handle.result()
        assert result["decision"] == "REJECTED"
