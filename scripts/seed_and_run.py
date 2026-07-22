import httpx
import asyncio
import time
import json
import uuid

API_BASE_URL = "http://localhost:8000/api"

async def main():
    print("🚀 Starting TITAN End-to-End Execution Seed Script")
    print("-" * 50)
    
    org_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    
    # 1. Simulate an incoming webhook (e.g., from a web form or email parser)
    payload = {
        "event_type": "new_lead_captured",
        "organization_id": org_id,
        "source": "website_contact_form",
        "payload": {
            "name": "Elon Musk",
            "email": "elon@x.com",
            "company": "xAI",
            "message": "I am interested in deploying TITAN across our data centers. Need enterprise pricing."
        }
    }
    
    print(f"📥 [1] Ingesting New Event for Org: {org_id}")
    async with httpx.AsyncClient() as client:
        try:
            # We mock the auth token or bypass depending on local config
            headers = {
                "Authorization": f"Bearer mock_token_{user_id}",
                "X-Organization-Id": org_id
            }
            
            res = await client.post(
                f"{API_BASE_URL}/events/ingest", 
                json=payload,
                headers=headers,
                timeout=10.0
            )
            
            if res.status_code != 200:
                print(f"❌ Failed to ingest event. Make sure the FastAPI server is running on port 8000.\nError: {res.text}")
                return
                
            data = res.json()
            task_id = data.get("task_id")
            print(f"✅ Event Ingested! Triggered Task ID: {task_id}")
            
        except Exception as e:
            print(f"❌ Network Error: Is the FastAPI server running? {e}")
            return

    # 2. Poll the API to watch execution
    print("\n⏳ [2] Polling Task Execution Status...")
    async with httpx.AsyncClient() as client:
        for _ in range(30):
            try:
                res = await client.get(f"{API_BASE_URL}/tasks/{task_id}", headers=headers)
                if res.status_code == 200:
                    status_data = res.json()
                    status = status_data.get("status")
                    print(f"   [{time.strftime('%X')}] Status: {status}")
                    
                    if status in ["COMPLETED", "FAILED", "PENDING_APPROVAL"]:
                        print(f"\n🎯 Execution reached terminal/pause state: {status}")
                        if status == "PENDING_APPROVAL":
                            print("\n🛑 HITL PAUSE: The agent requested an action that requires human approval.")
                            print("   Go to the TITAN Dashboard -> Approvals tab to review and approve it.")
                        break
                else:
                    print(f"   Error fetching status: {res.status_code}")
            except Exception:
                pass
                
            await asyncio.sleep(2)
            
    print("\n✅ Seed Script Complete. Check the Temporal UI (http://localhost:8233) for full workflow traces.")

if __name__ == "__main__":
    asyncio.run(main())
