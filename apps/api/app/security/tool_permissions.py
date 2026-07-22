from typing import List, Dict
import logging

logger = logging.getLogger(__name__)


class PermissionDeniedError(Exception):
    pass


class ToolPermissionMatrix:
    """
    Role-Based Access Control (RBAC) mapping for AI Agents.
    Enforces the principle of least privilege at the Tool Execution layer.
    """

    # Mapping of agent names (or roles) to the list of tools they are allowed to execute.
    _permissions: Dict[str, List[str]] = {
        "SalesAgent": [
            "search_knowledge_base",
            "search_web",
            "send_email",
            "update_crm",
            "create_deal",
        ],
        "SupportAgent": [
            "search_knowledge_base",
            "create_ticket",
            "refund_order",  # Requires HITL approval natively
        ],
        "ResearchAgent": ["search_web", "search_knowledge_base", "read_document"],
    }

    @classmethod
    def check_permission(cls, agent_name: str, tool_name: str) -> None:
        """
        Validates if an agent is authorized to use a specific tool.
        Raises PermissionDeniedError if unauthorized.
        """
        allowed_tools = cls._permissions.get(agent_name, [])

        if tool_name not in allowed_tools:
            logger.error(
                f"RBAC Violation: {agent_name} attempted to use unauthorized tool '{tool_name}'"
            )
            raise PermissionDeniedError(
                f"Security Violation: Agent '{agent_name}' is not authorized to execute '{tool_name}'."
            )
