from fastapi import WebSocket, WebSocketDisconnect
from typing import Dict, List
import json
import logging

logger = logging.getLogger(__name__)

class ConnectionManager:
    """
    Manages active WebSocket connections for real-time dashboard updates.
    Connections are partitioned by organization_id to ensure tenant isolation.
    """
    def __init__(self):
        # organization_id -> list of active connections
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, organization_id: str):
        await websocket.accept()
        if organization_id not in self.active_connections:
            self.active_connections[organization_id] = []
        self.active_connections[organization_id].append(websocket)
        logger.info(f"WebSocket connected for org {organization_id}")

    def disconnect(self, websocket: WebSocket, organization_id: str):
        if organization_id in self.active_connections:
            try:
                self.active_connections[organization_id].remove(websocket)
                if not self.active_connections[organization_id]:
                    del self.active_connections[organization_id]
                logger.info(f"WebSocket disconnected for org {organization_id}")
            except ValueError:
                pass

    async def broadcast_to_org(self, organization_id: str, message: dict):
        """
        Sends a JSON message to all connected clients for a specific organization.
        """
        if organization_id in self.active_connections:
            disconnected = []
            for connection in self.active_connections[organization_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    disconnected.append(connection)
            
            # Clean up dropped connections
            for conn in disconnected:
                self.disconnect(conn, organization_id)

manager = ConnectionManager()
