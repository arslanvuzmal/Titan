from fastapi import APIRouter, Depends, HTTPException, Query
from app.core.auth import UserContext
from app.core.dependencies import get_tenant_context
from app.core.database import get_db

router = APIRouter(prefix="/tasks", tags=["Tasks"])


@router.get("")
async def list_tasks(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, le=100),
    status: str | None = None,
    user: UserContext = Depends(get_tenant_context),
    db=Depends(get_db),
):
    """
    List tasks for the current organization.
    """
    where_clause = {"organizationId": user.organization_id}
    if status:
        where_clause["status"] = status

    tasks = await db.task.find_many(
        where=where_clause, skip=skip, take=limit, order={"createdAt": "desc"}
    )
    return {"data": tasks, "skip": skip, "limit": limit}


@router.get("/stats")
async def get_task_stats(
    user: UserContext = Depends(get_tenant_context), db=Depends(get_db)
):
    """
    Get aggregated task statistics for the dashboard.
    """
    # Prisma Python supports grouping
    grouped = await db.task.group_by(
        by=["status"],
        count={"id": True},
        where={"organizationId": user.organization_id},
    )

    stats = {item["status"]: item["_count"]["id"] for item in grouped}
    return {"data": stats}


@router.get("/{task_id}")
async def get_task(
    task_id: str, user: UserContext = Depends(get_tenant_context), db=Depends(get_db)
):
    """
    Get a specific task by ID. Ensures tenant isolation.
    """
    task = await db.task.find_first(
        where={"id": task_id, "organizationId": user.organization_id},
        include={"steps": True, "agent": True},
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"data": task}


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: str, user: UserContext = Depends(get_tenant_context), db=Depends(get_db)
):
    """
    Cancels a running task.
    In a real system, this would send a cancellation signal to the Temporal workflow.
    """
    task = await db.task.find_first(
        where={"id": task_id, "organizationId": user.organization_id}
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Update DB state
    updated = await db.task.update(where={"id": task_id}, data={"status": "CANCELLED"})

    # Trigger temporal cancellation here...

    return {"data": updated}


@router.get("/{task_id}/trace")
async def get_task_trace(
    task_id: str, user: UserContext = Depends(get_tenant_context), db=Depends(get_db)
):
    """
    Gets the detailed execution trace for a task from the AuditLog or AgentExecution table.
    """
    traces = await db.agentexecution.find_many(
        where={
            "organizationId": user.organization_id,
            # Assuming trace is linked somehow, we mock filter by status or generic link
            # "taskId": task_id # If we added relation to schema
        },
        take=10,
    )
    return {"data": traces}
