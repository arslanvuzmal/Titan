import logging
from typing import Any, Dict, Optional, List
from uuid import UUID

# Mock import for LangChain base callback, you might use from langchain_core.callbacks import BaseCallbackHandler
try:
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError:
    class BaseCallbackHandler:
        pass

logger = logging.getLogger(__name__)

class TitanLangGraphCallbackHandler(BaseCallbackHandler):
    """
    Observability handler for LangGraph nodes.
    Logs token usage, durations, and validation errors to the database.
    """
    def __init__(self, task_id: str, organization_id: str):
        self.task_id = task_id
        self.organization_id = organization_id

    def on_llm_start(
        self, serialized: Dict[str, Any], prompts: List[str], **kwargs: Any
    ) -> Any:
        logger.info(f"[TRACING] LLM Start | Task: {self.task_id} | Org: {self.organization_id}")

    def on_llm_end(self, response: Any, **kwargs: Any) -> Any:
        # In a real setup, we would extract response.llm_output['token_usage']
        # and write it to the `TaskStep` or `AgentExecution` table using Prisma.
        logger.info(f"[TRACING] LLM End | Task: {self.task_id}")

    def on_llm_error(
        self, error: Exception, **kwargs: Any
    ) -> Any:
        logger.error(f"[TRACING] LLM Error | Task: {self.task_id} | Error: {str(error)}")

    def on_chain_start(
        self, serialized: Dict[str, Any], inputs: Dict[str, Any], **kwargs: Any
    ) -> Any:
        node_name = serialized.get("name", "Unknown Node")
        logger.info(f"[TRACING] Node Start: {node_name} | Task: {self.task_id}")

    def on_chain_end(self, outputs: Dict[str, Any], **kwargs: Any) -> Any:
        logger.info(f"[TRACING] Node End | Task: {self.task_id}")

    def on_chain_error(self, error: Exception, **kwargs: Any) -> Any:
        # Crucial for catching Pydantic validation errors and feeding them back to the orchestrator
        logger.error(f"[TRACING] Node Error | Task: {self.task_id} | Error: {str(error)}")
