import logging
from enum import Enum
from pydantic import ValidationError
from app.agents.schemas import ActionRequest
from app.tools.registry import ToolRegistry, ToolResult
from app.tools.security import ToolContext, inject_secrets
from app.actions.audit import AuditLogger

logger = logging.getLogger(__name__)

class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

class ActionEngine:
    """
    The secure execution environment. Receives ActionRequests, evaluates risk,
    injects isolated secrets, enforces Pydantic validation, and executes.
    """

    @staticmethod
    def evaluate_risk(action_request: ActionRequest) -> RiskLevel:
        """
        Classifies the risk of an action based on the tool name.
        """
        high_risk_tools = ["send_email", "execute_sql_query", "delete_record"]
        medium_risk_tools = ["update_crm", "create_deal"]
        low_risk_tools = ["search_web", "query_knowledge_base", "read_document"]

        if action_request.tool_name in high_risk_tools:
            return RiskLevel.HIGH
        elif action_request.tool_name in medium_risk_tools:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    @staticmethod
    async def execute_action(action_request: ActionRequest, context: ToolContext) -> ToolResult:
        """
        The main entrypoint for executing an agent's requested action safely.
        """
        tool = ToolRegistry.get_tool(action_request.tool_name)
        if not tool:
            error_msg = f"Security Exception: Tool '{action_request.tool_name}' not found in registry."
            logger.error(error_msg)
            return ToolResult(status="FAILED", message=error_msg)

        # 1. Strict Pydantic Validation of untrusted inputs
        try:
            validated_params = tool.parameters_schema(**action_request.arguments)
        except ValidationError as e:
            error_msg = f"Validation Error: Agent provided invalid arguments for {tool.name}. Details: {str(e)}"
            logger.error(error_msg)
            # We catch this before execution to prevent malicious payloads from reaching external APIs
            return ToolResult(status="FAILED", message=error_msg)

        # 2. Risk Evaluation & Approval Check
        risk = ActionEngine.evaluate_risk(action_request)
        if risk == RiskLevel.HIGH and not action_request.requires_approval:
            error_msg = f"Security Exception: {tool.name} is HIGH risk but missing approval flag."
            logger.error(error_msg)
            return ToolResult(status="FAILED", message=error_msg)

        # 3. Secret Injection (Sandboxed)
        context.secrets = inject_secrets(tool.name)

        # 4. Execution
        try:
            result = await tool.execute(validated_params, context)
            
            # 5. Audit Logging (Success or Failure)
            await AuditLogger.log_action(
                organization_id=context.organization_id,
                task_id=context.task_id,
                tool_name=tool.name,
                parameters=action_request.arguments,
                outcome=result.status,
                error_message=result.message if result.status == "FAILED" else None
            )
            
            return result
            
        except Exception as e:
            error_msg = f"Unhandled Exception during execution of {tool.name}: {str(e)}"
            logger.exception(error_msg)
            
            await AuditLogger.log_action(
                organization_id=context.organization_id,
                task_id=context.task_id,
                tool_name=tool.name,
                parameters=action_request.arguments,
                outcome="FAILED",
                error_message=error_msg
            )
            return ToolResult(status="FAILED", message=error_msg)
