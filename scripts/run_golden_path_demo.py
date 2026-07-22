import asyncio
import httpx
import websockets
import json
import uuid
import sys

# Color codes for pretty terminal output
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

async def receive_updates(websocket, task_id: str):
    """Listens to the websocket and prints formatted updates."""
    print(f"\n{Colors.OKCYAN}📡 Connected to live execution stream for task: {task_id}{Colors.ENDC}\n")
    try:
        while True:
            message = await websocket.recv()
            data = json.loads(message)
            
            step = data.get("step_number")
            name = data.get("step_name")
            status = data.get("status")
            payload = data.get("payload", {})
            
            if status == "running":
                print(f"{Colors.OKBLUE}⏳ [{step}/16] {name}...{Colors.ENDC}")
            elif status == "completed":
                print(f"{Colors.OKGREEN}✅ [{step}/16] {name} - SUCCESS{Colors.ENDC}")
                if payload:
                    print(f"      ↳ {Colors.HEADER}{payload}{Colors.ENDC}")
            elif status == "failed":
                print(f"{Colors.FAIL}❌ [{step}/16] {name} - FAILED{Colors.ENDC}")
                print(f"      ↳ {payload.get('error', 'Unknown error')}")
                break
            elif status == "paused":
                print(f"\n{Colors.WARNING}⚠️  [{step}/16] {name} - ACTION REQUIRED{Colors.ENDC}")
                print(f"      ↳ Review drafted email:\n{Colors.BOLD}{payload.get('draft', '')}{Colors.ENDC}\n")
                
                # We prompt the user natively in the terminal
                decision = input(f"{Colors.WARNING}Press ENTER to approve, or type 'reject' to cancel: {Colors.ENDC}").strip().upper()
                
                # Send the decision to the API
                async with httpx.AsyncClient() as client:
                    action = "REJECTED" if decision == "REJECT" else "APPROVED"
                    print(f"\n{Colors.OKCYAN}Sending {action} signal to Temporal...{Colors.ENDC}")
                    # Normally we'd use the actual action_id, but for demo we just signal the workflow
                    res = await client.post(f"http://localhost:8000/api/approvals/{task_id}/decision", json={"decision": action})
                    if res.status_code != 200:
                        print(f"{Colors.FAIL}Failed to send approval: {res.text}{Colors.ENDC}")
            
            if step == 16 and status == "completed":
                print(f"\n{Colors.OKGREEN}🎉 Golden Path execution completed successfully!{Colors.ENDC}")
                break
                
    except websockets.exceptions.ConnectionClosed:
        print(f"\n{Colors.FAIL}Connection closed.{Colors.ENDC}")

async def run_demo():
    print(f"{Colors.BOLD}{Colors.HEADER}--- TITAN AI Sales Intelligence: The Golden Path ---{Colors.ENDC}")
    
    event_id = str(uuid.uuid4())
    payload = {
        "event_id": event_id,
        "organization_id": "demo-org",
        "source": "webhook",
        "event_type": "lead.created",
        "payload": {
            "name": "Jane Doe",
            "company": "Acme Corp",
            "email": "jane@acme.com",
            "context": "Downloaded 'AI for Operations' whitepaper."
        }
    }

    # 1. Trigger the event via REST API
    print(f"{Colors.OKCYAN}📦 Injecting `lead.created` event into TITAN...{Colors.ENDC}")
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post("http://localhost:8000/api/events/ingest", json=payload)
            if res.status_code != 200:
                print(f"{Colors.FAIL}Failed to ingest event: {res.text}{Colors.ENDC}")
                return
    except httpx.ConnectError:
        print(f"{Colors.FAIL}Cannot connect to API. Is the FastAPI server running on port 8000?{Colors.ENDC}")
        return

    # 2. Connect to the WebSocket to watch the execution trace
    # Using the event_id as the task_id because events.py uses `orchestrator-{event.event_id}`
    # but the websocket manager connects via organization_id in our simplified version.
    # In `main.py` we used: `await manager.connect(websocket, user.organization_id)`
    # So we connect to the `demo-org` stream.
    
    ws_url = "ws://localhost:8000/api/ws?token=demo-token-bypass"
    
    try:
        async with websockets.connect(ws_url) as ws:
            await receive_updates(ws, "demo-org")
    except Exception as e:
        print(f"{Colors.FAIL}WebSocket connection failed: {e}{Colors.ENDC}")

if __name__ == "__main__":
    try:
        asyncio.run(run_demo())
    except KeyboardInterrupt:
        print("\nDemo terminated by user.")
        sys.exit(0)
