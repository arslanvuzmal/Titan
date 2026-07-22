import pytest
import respx
from httpx import Response
from temporalio.testing import WorkflowEnvironment
from temporalio.client import Client

# Pytest Asyncio Configuration
pytest_plugins = ('pytest_asyncio',)

@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"

@pytest.fixture(scope="session")
async def temporal_client():
    """
    Spins up an ephemeral, in-memory Temporal test server.
    This guarantees 100% deterministic workflow execution without needing Docker.
    """
    async with WorkflowEnvironment.start_local() as env:
        yield env.client

@pytest.fixture
def mock_external_apis():
    """
    Intercepts all outbound HTTP traffic via httpx and returns mocked responses.
    Prevents tests from making real network calls.
    """
    with respx.mock(assert_all_called=False) as respx_mock:
        # Mock SendGrid
        respx_mock.post("https://api.sendgrid.com/v3/mail/send").mock(
            return_value=Response(202, json={"status": "accepted"})
        )
        
        # Mock HubSpot Search
        respx_mock.post("https://api.hubapi.com/crm/v3/objects/contacts/search").mock(
            return_value=Response(200, json={"results": [{"id": "12345"}]})
        )
        
        # Mock HubSpot Update
        respx_mock.patch("https://api.hubapi.com/crm/v3/objects/contacts/12345").mock(
            return_value=Response(200, json={"id": "12345", "updated": True})
        )
        
        # Mock Serper Web Search
        respx_mock.post("https://google.serper.dev/search").mock(
            return_value=Response(200, json={
                "organic": [
                    {"title": "Mock Title", "snippet": "Mock Snippet"}
                ]
            })
        )
        
        yield respx_mock

@pytest.fixture
def mock_llm():
    """
    A fixture that intercepts LangChain LLM calls.
    In a real implementation, you would patch `ChatOpenAI.ainvoke` here
    to return a structured AIMessage with function calls.
    """
    class MockAIMessage:
        def __init__(self, tool_calls=None, content=""):
            self.tool_calls = tool_calls or []
            self.content = content

    class MockModel:
        def __init__(self, response):
            self._response = response
            
        async def ainvoke(self, messages, *args, **kwargs):
            return self._response
            
    def _create_mock(response_type="valid"):
        if response_type == "valid":
            return MockModel(MockAIMessage(tool_calls=[{
                "name": "send_email",
                "args": {"to_email": "test@test.com", "subject": "Hi", "body": "Hello"}
            }]))
        elif response_type == "invalid_schema":
            return MockModel(MockAIMessage(tool_calls=[{
                "name": "send_email",
                # Missing required fields
                "args": {"body": "Hello"}
            }]))
            
    return _create_mock
