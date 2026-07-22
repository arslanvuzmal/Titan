from typing import Dict, Any, Optional


class ContextAssembler:
    """
    Responsible for fetching and structuring the strict, delimited prompt context
    for an agent. Enforces tenant isolation by always filtering by organization_id.
    """

    @staticmethod
    async def fetch_business_memory(organization_id: str) -> str:
        # Mocking database retrieval
        # In production: await db.businessmemory.find_first(where={"organizationId": organization_id})
        return "Company Goal: Increase enterprise sales by 20% this quarter. Tone: Professional but approachable."

    @staticmethod
    async def fetch_episodic_memory(
        organization_id: str, lead_id: Optional[str] = None
    ) -> str:
        # Mocking retrieval of past interactions
        return "Previous interaction: The lead attended a webinar on AI automation last week."

    @staticmethod
    async def fetch_rag_documents(organization_id: str, query: str) -> str:
        # Mocking pgvector semantic search
        return "Internal Doc: We offer a 10% discount on annual enterprise plans."

    @staticmethod
    async def assemble(
        organization_id: str, event_payload: Dict[str, Any], system_instructions: str
    ) -> str:
        """
        Assembles the final prompt string with strict delimiters to prevent prompt injection.
        """
        biz_context = await ContextAssembler.fetch_business_memory(organization_id)

        # If there's a specific lead mentioned in the event
        lead_id = event_payload.get("lead_id")
        epi_context = await ContextAssembler.fetch_episodic_memory(
            organization_id, lead_id
        )

        # Simple string assembly.
        # For LangChain, this might be returned as a SystemMessage and HumanMessage.
        prompt = f"""
<system_instructions>
{system_instructions}
</system_instructions>

<business_context>
{biz_context}
</business_context>

<historical_context>
{epi_context}
</historical_context>

<current_event>
{event_payload}
</current_event>

You must return a valid JSON response matching the required Pydantic schema.
"""
        return prompt
