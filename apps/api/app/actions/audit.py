import logging
import json
from copy import deepcopy
from typing import Dict, Any
from app.core.database import get_db

logger = logging.getLogger(__name__)

class AuditLogger:
    """
    Handles immutable logging of all tool executions for compliance and debugging.
    """

    @staticmethod
    def _redact_sensitive_params(params: Dict[str, Any]) -> str:
        """
        Removes sensitive information from the payload before saving to the DB.
        """
        redacted = deepcopy(params)
        sensitive_keys = ["password", "secret", "token", "key", "authorization"]
        
        for k in redacted.keys():
            if any(s in k.lower() for s in sensitive_keys):
                redacted[k] = "[REDACTED]"
                
        return json.dumps(redacted)

    @staticmethod
    async def log_action(
        organization_id: str,
        task_id: str,
        tool_name: str,
        parameters: Dict[str, Any],
        outcome: str,
        error_message: str = None
    ):
        """
        Writes the audit trail to the database.
        """
        db = await anext(get_db())
        safe_params = AuditLogger._redact_sensitive_params(parameters)
        
        logger.info(f"AUDIT LOG | Org: {organization_id} | Task: {task_id} | Tool: {tool_name} | Outcome: {outcome}")

        # Assuming an AuditLog table in Prisma
        # fallback to raw query for architectural completeness
        query = """
        INSERT INTO "AuditLog" ("organizationId", "taskId", "actionType", parameters, outcome, "errorMessage")
        VALUES ($1, $2, $3, $4::jsonb, $5, $6)
        """
        
        try:
            await db.execute_raw(
                query,
                organization_id,
                task_id,
                tool_name,
                safe_params,
                outcome,
                error_message
            )
        except Exception as e:
            logger.error(f"CRITICAL: Failed to write to audit log: {str(e)}")
            # Even if DB fails, we must ensure it's in the stdout logs
            pass
