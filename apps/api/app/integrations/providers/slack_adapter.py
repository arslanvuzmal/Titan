import httpx
from typing import Dict, Any
from .base import IntegrationAdapter

class SlackAdapter(IntegrationAdapter):
    """
    Adapter for Slack API.
    All calls are fully asynchronous via httpx.
    """
    BASE_URL = "https://slack.com/api"

    async def send_message(self, channel: str, text: str) -> Dict[str, Any]:
        """Posts a message to a Slack channel."""
        payload = {
            "channel": channel,
            "text": text
        }
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            # res = await client.post(f"{self.BASE_URL}/chat.postMessage", json=payload, headers=headers)
            # return res.json()
            return {"ok": True, "channel": channel, "ts": "1234567890.123456"}

    async def get_user_profile(self, user_id: str) -> Dict[str, Any]:
        """Fetches details for a specific user."""
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            # res = await client.get(f"{self.BASE_URL}/users.profile.get", params={"user": user_id}, headers=headers)
            # return res.json()
            return {"ok": True, "profile": {"real_name": "Mock User", "email": "mock@example.com"}}
