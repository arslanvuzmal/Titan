import asyncio
import json
from typing import Dict, Any
from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        # Maps task_id to a list of active websocket connections
        self.active_connections: Dict[str, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, task_id: str):
        await websocket.accept()
        if task_id not in self.active_connections:
            self.active_connections[task_id] = []
        self.active_connections[task_id].append(websocket)

    def disconnect(self, websocket: WebSocket, task_id: str):
        if task_id in self.active_connections:
            if websocket in self.active_connections[task_id]:
                self.active_connections[task_id].remove(websocket)
            if not self.active_connections[task_id]:
                del self.active_connections[task_id]

    async def broadcast_task_update(self, task_id: str, update_payload: Dict[str, Any]):
        """
        Sends an execution trace update to all clients listening to a specific task_id.
        Expected payload format:
        {
            "step_number": int,
            "step_name": str,
            "status": "running" | "completed" | "paused" | "failed",
            "payload": dict,
            "duration_ms": int (optional)
        }
        """
        if task_id in self.active_connections:
            message = json.dumps(update_payload)
            for connection in self.active_connections[task_id]:
                try:
                    await connection.send_text(message)
                except Exception:
                    # If sending fails, client disconnected abruptly
                    pass

# Global singleton
manager = ConnectionManager()
