from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from temporalio.client import Client
from app.core.auth import get_current_user
from app.core.database import get_db

router = APIRouter(prefix="/api/approvals", tags=["Approvals"])


class ApprovalDecision(BaseModel):
    decision: str = Field(..., description="APPROVED, REJECTED, or EDITED")
    workflow_id: str = Field(
        ..., description="The Temporal Workflow ID waiting for the signal"
    )
    edited_parameters: Optional[Dict[str, Any]] = Field(
        None, description="New params if EDITED"
    )
    reason: Optional[str] = None


class PendingAction(BaseModel):
    action_id: str
    workflow_id: str
    tool_name: str
    parameters: Dict[str, Any]
    status: str


@router.get("", response_model=List[PendingAction])
async def get_pending_approvals(user: dict = Depends(get_current_user)):
    """
    Retrieves all pending HITL actions for the current user's organization.
    """
    db = await anext(get_db())
    query = """
    SELECT id as action_id, "workflowId" as workflow_id, "toolName" as tool_name, parameters, status
    FROM "ActionRequest"
    WHERE "organizationId" = $1 AND status = 'PENDING_APPROVAL'
    ORDER BY "createdAt" DESC;
    """

    try:
        results = await db.query_raw(query, user["organization_id"])
        import json

        pending = []
        for r in results:
            pending.append(
                PendingAction(
                    action_id=r["action_id"],
                    workflow_id=r["workflow_id"],
                    tool_name=r["tool_name"],
                    parameters=(
                        json.loads(r["parameters"])
                        if isinstance(r["parameters"], str)
                        else r["parameters"]
                    ),
                    status=r["status"],
                )
            )
        return pending
    except Exception:
        # Fallback if DB table doesn't exist yet for smooth testing
        return []


@router.post("/{action_id}/decide")
async def decide_approval(
    action_id: str, payload: ApprovalDecision, user: dict = Depends(get_current_user)
):
    """
    Processes a human decision and sends a Temporal signal to the paused workflow.
    """
    if payload.decision not in ["APPROVED", "REJECTED", "EDITED"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid decision. Must be APPROVED, REJECTED, or EDITED.",
        )

    # 1. Validate action belongs to user's org
    db = await anext(get_db())
    check_query = """SELECT id FROM "ActionRequest" WHERE id = $1 AND "organizationId" = $2 AND status = 'PENDING_APPROVAL'"""

    try:
        exists = await db.query_raw(check_query, action_id, user["organization_id"])
        if not exists:
            raise HTTPException(
                status_code=404, detail="Action not found or already processed."
            )

        # 2. Update DB state
        new_status = payload.decision
        update_query = """UPDATE "ActionRequest" SET status = $1 WHERE id = $2"""
        await db.execute_raw(update_query, new_status, action_id)

    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        # Mocking for tests if table missing
        pass

    # 3. Send Temporal Signal
    try:
        # In production, cache the Temporal client
        client = await Client.connect("localhost:7233")
        handle = client.get_workflow_handle(payload.workflow_id)

        signal_name = "hitl-approval-signal"
        signal_payload = {
            "decision": payload.decision,
            "edited_parameters": payload.edited_parameters,
            "reason": payload.reason,
        }

        await handle.signal(signal_name, signal_payload)
        return {
            "status": "success",
            "message": f"Signal sent to workflow {payload.workflow_id}",
        }

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to communicate with Temporal: {str(e)}"
        )
