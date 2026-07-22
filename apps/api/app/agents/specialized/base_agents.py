from pydantic import BaseModel
from app.agents.schemas import TitanAgentState

class SupportAgentOutput(BaseModel):
    ticket_category: str
    suggested_reply: str
    urgency_level: str

class SupportAgent:
    @staticmethod
    async def invoke(state: TitanAgentState) -> TitanAgentState:
        state["agent_history"].append("SupportAgent skeleton executed.")
        return state

class BIAgentOutput(BaseModel):
    sql_query: str
    data_summary: str

class BIAgent:
    @staticmethod
    async def invoke(state: TitanAgentState) -> TitanAgentState:
        state["agent_history"].append("BIAgent skeleton executed.")
        return state

class DocumentAgentOutput(BaseModel):
    extracted_entities: dict
    summary: str

class DocumentAgent:
    @staticmethod
    async def invoke(state: TitanAgentState) -> TitanAgentState:
        state["agent_history"].append("DocumentAgent skeleton executed.")
        return state

class ExecutiveAgentOutput(BaseModel):
    strategic_decision: str
    delegated_tasks: list

class ExecutiveAgent:
    @staticmethod
    async def invoke(state: TitanAgentState) -> TitanAgentState:
        state["agent_history"].append("ExecutiveAgent skeleton executed.")
        return state
