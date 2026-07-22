from app.agents.schemas import TitanAgentState, ResearchAgentOutput
from app.agents.context_assembler import ContextAssembler


class ResearchAgent:
    """
    Executes deep web research on targets.
    """

    SYSTEM_PROMPT = """
    You are an expert Market Intelligence Analyst.
    Your job is to parse the current event, extract the target company or entity, 
    and output a structured research summary including executive findings and business implications.
    """

    @staticmethod
    async def invoke(state: TitanAgentState) -> TitanAgentState:
        org_id = state["organization_id"]
        event_payload = state["event"].get("payload", {})

        # 1. Assemble Context
        await ContextAssembler.assemble(
            organization_id=org_id,
            event_payload=event_payload,
            system_instructions=ResearchAgent.SYSTEM_PROMPT,
        )

        # MOCK LLM Execution
        mock_llm_response = {
            "executive_summary": "Acme Corp is a mid-sized B2B SaaS company specializing in logistics.",
            "key_findings": [
                "Recently raised $5M Series A",
                "Using outdated CRM system",
            ],
            "business_implications": "Strong candidate for our Enterprise tier upgrade due to recent funding.",
            "action_requests": [],
        }

        try:
            # 3. Pydantic V2 Validation
            validated_output = ResearchAgentOutput(**mock_llm_response)

            # 4. Update State
            state["agent_history"].append("ResearchAgent completed analysis.")
            state["pending_actions"].extend(
                [req.model_dump() for req in validated_output.action_requests]
            )
            state["error_state"] = None

        except Exception as e:
            state["error_state"] = f"ResearchAgent Validation Error: {str(e)}"
            state["agent_history"].append("ResearchAgent failed validation.")

        return state
