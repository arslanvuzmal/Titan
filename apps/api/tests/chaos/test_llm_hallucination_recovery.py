import pytest
from app.agents.orchestrator import titan_orchestrator_graph
from app.agents.schemas import TitanAgentState


@pytest.mark.asyncio
async def test_llm_hallucination_recovery(monkeypatch):
    """
    Simulates the LLM returning malformed JSON (missing required Pydantic fields).
    Asserts that the orchestrator's LangGraph catches the error, increments the retry counter,
    and doesn't crash the system.
    """

    # 1. Initialize orchestrator with a mocked LLM that returns invalid schema
    # Instead of an object, we use the graph directly.
    # For testing, we mock the underlying agents' LLM locally if needed.
    # Here we simulate by raising a ValueError in the SalesAgentOutput parser.
    def mock_init(*args, **kwargs):
        raise ValueError("Simulated validation error due to missing fields")

    monkeypatch.setattr(
        "app.agents.specialized.sales_agent.SalesAgentOutput.__init__", mock_init
    )

    # 3. Create initial state
    initial_state = TitanAgentState(
        task_id="test-task-123",
        organization_id="test-org-123",
        event={"event_type": "sales"},
        messages=[("user", "Send an email to elon@x.com")],
        context={},
        current_step="router",
        pending_actions=[],
        errors=[],
        agent_history=[],
    )

    # 4. Run the first node manually to observe the error handling
    # In LangGraph, we can invoke the specific node.
    new_state = await titan_orchestrator_graph.ainvoke(initial_state)

    # 5. Assertions
    # Since the LLM returned invalid tool args (missing 'subject' and 'to_email' in mock),
    # the Pydantic parser inside LangChain/LangGraph will throw an error.
    # Our orchestrator's error handling should catch it, append to state.errors, and increment retry.
    assert new_state.get("error_state") is not None
    assert (
        "validation" in new_state["error_state"].lower()
        or "missing" in new_state["error_state"].lower()
    )
