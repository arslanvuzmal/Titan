from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.auth import get_current_user, require_role, UserContext
from app.core.websocket import manager
from app.api import tasks, events

app = FastAPI(
    title="TITAN API",
    version="0.1.0",
    docs_url="/api/docs" if settings.ENVIRONMENT != "production" else None
)

# CORS for Next.js Frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include core routers
app.include_router(tasks.router, prefix="/api")
app.include_router(events.router, prefix="/api")

@app.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket, token: str):
    """
    WebSocket endpoint for real-time dashboard updates.
    We authenticate the connection using the JWT token passed as a query param.
    """
    from fastapi.security import HTTPAuthorizationCredentials
    
    try:
        # Manually authenticate the token since Depends() inside WebSockets
        # behaves slightly differently and we want to control the failure.
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        user = await get_current_user(creds)
    except Exception as e:
        await websocket.close(code=1008, reason=f"Authentication failed: {str(e)}")
        return
        
    await manager.connect(websocket, user.organization_id)
    try:
        while True:
            # We don't expect the client to send much data, but we must keep the connection alive
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, user.organization_id)

@app.get("/api/health")
async def health_check():
    return {"status": "operational", "service": "titan-api"}
