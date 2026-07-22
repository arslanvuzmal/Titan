from app.agents.schemas import TitanAgentState, SalesAgentOutput
from app.agents.context_assembler import ContextAssembler

# For a real implementation, we would use:
# from langchain_openai import ChatOpenAI
# from langchain_core.prompts import PromptTemplate


class SalesAgent:
    """
    Analyzes inbound leads, scores them, and drafts outreach.
    """

    SYSTEM_PROMPT = """
    You are an expert Sales Development Representative. 
    Analyze the current event (a new lead), calculate a lead score based on the business context, 
    and draft a personalized outreach email.
    """

    @staticmethod
    async def invoke(state: TitanAgentState) -> TitanAgentState:
        """
        Executes the agent logic, returning the updated state.
        """
        org_id = state["organization_id"]
        event_payload = state["event"].get("payload", {})

        # 1. Assemble Context
        await ContextAssembler.assemble(
            organization_id=org_id,
            event_payload=event_payload,
            system_instructions=SalesAgent.SYSTEM_PROMPT,
        )

        # 2. Execute LLM Call (Mocked for now, but structured as a real call)
        # llm = ChatOpenAI(temperature=0, model="gpt-4-turbo").with_structured_output(SalesAgentOutput)
        # result = await llm.ainvoke(prompt)

        # MOCK LLM Execution returning a dictionary that will be parsed by Pydantic
        mock_llm_response = {
            "lead_score": 85,
            "score_factors": ["Enterprise tier interest", "Attended recent webinar"],
            "confidence": 0.9,
            "recommended_action": "Fast-track to AE",
            "drafted_email": "Hi there,\n\nI saw you attended our AI webinar...",
            "action_requests": [
                {
                    "tool_name": "update_crm",
                    "arguments": {
                        "lead_id": event_payload.get("lead_id", "unknown"),
                        "score": 85,
                    },
                    "requires_approval": False,
                },
                {
                    "tool_name": "send_email",
                    "arguments": {
                        "to": event_payload.get("email", "unknown@example.com"),
                        "body": "Hi there...",
                    },
                    "requires_approval": True,  # Enforced by Pydantic validator
                },
            ],
        }

        try:
            # 3. Pydantic V2 Validation
            # In LangChain with `.with_structured_output`, this happens automatically.
            validated_output = SalesAgentOutput(**mock_llm_response)

            # 4. Update State
            state["agent_history"].append("SalesAgent completed analysis.")
            state["pending_actions"].extend(
                [req.model_dump() for req in validated_output.action_requests]
            )
            state["error_state"] = None

        except Exception as e:
            # Catch Pydantic validation errors (e.g., missing required fields, or the custom email approval validator)
            state["error_state"] = f"SalesAgent Validation Error: {str(e)}"
            state["agent_history"].append("SalesAgent failed validation.")

        return state
