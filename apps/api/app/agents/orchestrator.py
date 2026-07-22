from typing import Dict, Any, Literal
import logging
from langgraph.graph import StateGraph, END
from app.agents.schemas import TitanAgentState
from app.agents.specialized.sales_agent import SalesAgent
from app.agents.specialized.research_agent import ResearchAgent

logger = logging.getLogger(__name__)

async def router_node(state: TitanAgentState) -> TitanAgentState:
    """
    Analyzes the event type to determine the appropriate specialized agent.
    """
    event_type = state["event"].get("event_type", "")
    logger.info(f"Router analyzing event: {event_type}")
    
    # Simple routing logic based on event type.
    # In a more advanced setup, this could be an LLM call itself (semantic routing).
    if "lead" in event_type or "sales" in event_type:
        state["current_step"] = "route_to_sales"
    elif "research" in event_type or "competitor" in event_type:
        state["current_step"] = "route_to_research"
    else:
        state["current_step"] = "end"
        
    state["agent_history"].append(f"Routed to {state['current_step']}")
    return state

async def planner_node(state: TitanAgentState) -> TitanAgentState:
    """
    Optional node to break complex tasks into subtasks.
    Currently a pass-through for simplicity.
    """
    logger.info("Planner passing through.")
    return state

async def verifier_node(state: TitanAgentState) -> TitanAgentState:
    """
    Checks if there was a Pydantic validation error in the previous node.
    If so, we might want to loop back to the agent (retry).
    """
    if state.get("error_state"):
        logger.warning(f"Verifier caught error: {state['error_state']}")
        # We could implement retry counting here
        # state["retry_count"] += 1
        # if state["retry_count"] >= 3:
        #    state["is_complete"] = True
    return state

async def action_gate_node(state: TitanAgentState) -> TitanAgentState:
    """
    Evaluates generated actions. If any require approval, pauses the graph.
    """
    for action in state.get("pending_actions", []):
        if action.get("requires_approval"):
            logger.info("Action requires approval. Yielding to HITL.")
            state["current_step"] = "PENDING_APPROVAL"
            return state
            
    # If no approval needed, mark complete (Phase 7 Action Engine will pick it up)
    state["is_complete"] = True
    return state

# --- Conditional Edges ---

def decide_next_agent(state: TitanAgentState) -> str:
    """Determines which node to execute next based on the router."""
    if state["current_step"] == "route_to_sales":
        return "sales_agent"
    elif state["current_step"] == "route_to_research":
        return "research_agent"
    return "end"

def check_verification(state: TitanAgentState) -> str:
    """Checks if we need to retry or move to the action gate."""
    if state.get("error_state"):
        # We could route back to the specific agent if we track it.
        # For this skeleton, we just end on error.
        return "end"
    return "action_gate"

# --- Build Graph ---

workflow = StateGraph(TitanAgentState)

# Add nodes
workflow.add_node("router", router_node)
workflow.add_node("planner", planner_node)
workflow.add_node("sales_agent", SalesAgent.invoke)
workflow.add_node("research_agent", ResearchAgent.invoke)
workflow.add_node("verifier", verifier_node)
workflow.add_node("action_gate", action_gate_node)

# Add edges
workflow.set_entry_point("router")

# Router decides which specialized agent to call
workflow.add_conditional_edges(
    "router",
    decide_next_agent,
    {
        "sales_agent": "sales_agent",
        "research_agent": "research_agent",
        "end": END
    }
)

# After specialized agents, go to verifier
workflow.add_edge("sales_agent", "verifier")
workflow.add_edge("research_agent", "verifier")

# Verifier decides if we proceed to Action Gate or End (on error)
workflow.add_conditional_edges(
    "verifier",
    check_verification,
    {
        "action_gate": "action_gate",
        "end": END
    }
)

workflow.add_edge("action_gate", END)

# Compile
titan_orchestrator_graph = workflow.compile()
