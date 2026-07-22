import pytest
import httpx
import respx
import uuid
from app.actions.engine import ActionEngine
from app.agents.schemas import ActionRequest
from app.tools.security import ToolContext

@pytest.mark.asyncio
async def test_external_api_timeout():
    """
    Simulates a total network outage or 3rd party API failure (e.g., SendGrid goes down).
    Asserts the ActionEngine handles the timeout cleanly without crashing the worker.
    """
    # 1. Setup Action Request
    req = ActionRequest(
        tool_name="send_email",
        arguments={"to_email": "test@x.com", "subject": "Test", "body": "Body"},
        requires_approval=True
    )
    
    context = ToolContext(
        organization_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4())
    )
    
    # 2. Inject Chaos: Mock the httpx call to raise a TimeoutException
    with respx.mock:
        respx.post("https://api.sendgrid.com/v3/mail/send").mock(
            side_effect=httpx.TimeoutException("Connection timed out")
        )
        
        # 3. Execute
        result = await ActionEngine.execute_action(req, context)
        
        # 4. Assertions
        assert result.status == "FAILED"
        assert "Network error" in result.message
        assert "timed out" in result.message.lower()
