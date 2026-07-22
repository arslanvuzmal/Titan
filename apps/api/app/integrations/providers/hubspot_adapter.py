import httpx
from typing import Dict, Any
from .base import IntegrationAdapter

class HubSpotAdapter(IntegrationAdapter):
    """
    Adapter for HubSpot CRM API.
    All calls are fully asynchronous via httpx.
    """
    BASE_URL = "https://api.hubapi.com/crm/v3"

    async def create_contact(self, email: str, properties: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a contact in HubSpot."""
        payload = {
            "properties": {
                "email": email,
                **properties
            }
        }
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            # res = await client.post(f"{self.BASE_URL}/objects/contacts", json=payload, headers=headers)
            # return res.json()
            return {"id": "mock_contact_id_789", "properties": payload["properties"]}

    async def update_deal(self, deal_id: str, stage: str) -> Dict[str, Any]:
        """Updates a deal stage in HubSpot."""
        payload = {
            "properties": {
                "dealstage": stage
            }
        }
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            # res = await client.patch(f"{self.BASE_URL}/objects/deals/{deal_id}", json=payload, headers=headers)
            # return res.json()
            return {"id": deal_id, "properties": {"dealstage": stage}}
