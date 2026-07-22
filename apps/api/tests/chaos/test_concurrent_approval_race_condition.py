import pytest
import asyncio
from fastapi import HTTPException
from app.api.approvals import decide_approval, ApprovalDecision


@pytest.mark.asyncio
async def test_concurrent_approval_race_condition(monkeypatch):
    """
    Simulates a race condition where two different managers try to approve the
    exact same action at the exact same millisecond.
    Asserts that the database state locks or correctly handles the duplicate via
    the API's checks, preventing double execution of the workflow.
    """
    action_id = "test-action-123"
    workflow_id = "test-wf-123"

    # We mock the DB to simulate that the FIRST call succeeds,
    # but the SECOND call finds the status is already 'APPROVED'

    call_count = 0

    class MockDB:
        async def query_raw(self, query: str, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [{"id": action_id}]  # PENDING_APPROVAL exists
            else:
                return []  # Already processed

        async def execute_raw(self, query: str, *args):
            pass

    async def mock_get_db():
        yield MockDB()

    monkeypatch.setattr("app.api.approvals.get_db", mock_get_db)

    # Mock the Temporal Client to avoid actual network calls here
    class MockHandle:
        async def signal(self, *args, **kwargs):
            pass

    class MockClient:
        def get_workflow_handle(self, *args, **kwargs):
            return MockHandle()

    async def mock_connect(*args, **kwargs):
        return MockClient()

    monkeypatch.setattr("app.api.approvals.Client.connect", mock_connect)

    user = {"organization_id": "org-123"}
    decision = ApprovalDecision(decision="APPROVED", workflow_id=workflow_id)

    # 1. Execute the two requests concurrently
    task1 = asyncio.create_task(decide_approval(action_id, decision, user))
    task2 = asyncio.create_task(decide_approval(action_id, decision, user))

    results = await asyncio.gather(task1, task2, return_exceptions=True)

    # 2. Assertions
    # One should succeed, one should fail with a 404 HTTPException
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, HTTPException)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].status_code == 404
    assert "Action not found or already processed" in failures[0].detail
