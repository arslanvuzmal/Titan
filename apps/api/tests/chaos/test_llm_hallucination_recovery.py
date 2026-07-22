import pytest
from app.agents.orchestrator import TitanOrchestrator
from app.agents.schemas import AgentState

@pytest.mark.asyncio
async def test_llm_hallucination_recovery(mock_llm):
    """
    Simulates the LLM returning malformed JSON (missing required Pydantic fields).
    Asserts that the orchestrator's LangGraph catches the error, increments the retry counter,
    and doesn't crash the system.
    """
    # 1. Initialize orchestrator with a mocked LLM that returns invalid schema
    invalid_llm = mock_llm(response_type="invalid_schema")
    orchestrator = TitanOrchestrator()
    orchestrator.llm = invalid_llm  # Inject chaos
    
    # 2. Build the graph
    app = orchestrator.build_graph()
    
    # 3. Create initial state
    initial_state = AgentState(
        messages=[("user", "Send an email to elon@x.com")],
        context={},
        current_agent="Orchestrator",
        pending_actions=[],
        errors=[],
        retry_count=0
    )
    
    # 4. Run the first node manually to observe the error handling
    # In LangGraph, we can invoke the specific node.
    new_state = await orchestrator.orchestrate(initial_state)
    
    # 5. Assertions
    # Since the LLM returned invalid tool args (missing 'subject' and 'to_email' in mock),
    # the Pydantic parser inside LangChain/LangGraph will throw an error.
    # Our orchestrator's error handling should catch it, append to state.errors, and increment retry.
    assert len(new_state["errors"]) > 0
    assert "validation" in new_state["errors"][0].lower() or "missing" in new_state["errors"][0].lower()
    assert new_state["retry_count"] == 1
