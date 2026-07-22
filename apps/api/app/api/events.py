from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from app.core.auth import UserContext, get_current_user
from app.core.dependencies import get_tenant_context
from app.core.database import get_db
from app.core.events import TitanEvent, EventDispatcher
from typing import Dict, Any

router = APIRouter(prefix="/events", tags=["Events"])

@router.post("/ingest")
async def ingest_event(
    event: TitanEvent,
    background_tasks: BackgroundTasks,
    # In a real system, webhooks use HMAC verification instead of user JWT,
    # but for internal/dashboard submission, we use the user token.
    user: UserContext = Depends(get_tenant_context),
    db = Depends(get_db)
):
    """
    Ingests a new event into the system.
    """
    # Enforce tenant isolation
    if event.organization_id != user.organization_id:
        raise HTTPException(status_code=403, detail="Cross-tenant event ingestion forbidden")
        
    # Log to database
    await db.event.create(
        data={
            "id": event.event_id,
            "organizationId": event.organization_id,
            "type": event.event_type,
            "payload": event.payload
        }
    )
    
    # Dispatch to Temporal in the background to ensure fast API response
    background_tasks.add_task(EventDispatcher.dispatch, event)
    
    return {"status": "accepted", "event_id": event.event_id}

@router.get("")
async def list_events(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, le=100),
    event_type: str | None = None,
    user: UserContext = Depends(get_tenant_context),
    db = Depends(get_db)
):
    """
    List events for the dashboard timeline.
    """
    where_clause = {"organizationId": user.organization_id}
    if event_type:
        where_clause["type"] = event_type
        
    events = await db.event.find_many(
        where=where_clause,
        skip=skip,
        take=limit,
        order={"createdAt": "desc"}
    )
    return {"data": events, "skip": skip, "limit": limit}

@router.get("/stats")
async def get_event_stats(
    user: UserContext = Depends(get_tenant_context),
    db = Depends(get_db)
):
    """
    Event statistics for dashboard charts.
    """
    grouped = await db.event.group_by(
        by=["type"],
        count={"id": True},
        where={"organizationId": user.organization_id}
    )
    stats = {item["type"]: item["_count"]["id"] for item in grouped}
    return {"data": stats}
