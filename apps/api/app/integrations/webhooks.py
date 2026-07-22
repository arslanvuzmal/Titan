import hmac
import hashlib
import os
import json
import uuid
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from app.core.events import TitanEvent, EventDispatcher

router = APIRouter()

# In production, these must be loaded from secrets
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "mock_slack_secret")
HUBSPOT_WEBHOOK_SECRET = os.getenv("HUBSPOT_WEBHOOK_SECRET", "mock_hubspot_secret")


def verify_slack_signature(request: Request, body: bytes) -> bool:
    """Verifies the X-Slack-Signature header."""
    slack_signature = request.headers.get("X-Slack-Signature", "")
    slack_timestamp = request.headers.get("X-Slack-Request-Timestamp", "")

    if not slack_signature or not slack_timestamp:
        return False

    sig_basestring = f"v0:{slack_timestamp}:{body.decode('utf-8')}"
    my_signature = (
        "v0="
        + hmac.new(
            SLACK_SIGNING_SECRET.encode(), sig_basestring.encode(), hashlib.sha256
        ).hexdigest()
    )

    return hmac.compare_digest(my_signature, slack_signature)


@router.post("/slack")
async def slack_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receives webhooks from Slack.
    Verifies signature, then pushes to internal event router.
    """
    body = await request.body()

    # In a real scenario, you enforce this. We bypass for testing if secret is mock.
    if SLACK_SIGNING_SECRET != "mock_slack_secret" and not verify_slack_signature(
        request, body
    ):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    payload = json.loads(body)

    # Handle Slack URL verification challenge
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    # Map to internal TITAN event
    event_data = {
        "event_id": str(uuid.uuid4()),
        "organization_id": payload.get("team_id", "demo-org"),
        "source": "slack",
        "event_type": "slack.message_received",
        "payload": payload,
    }

    event = TitanEvent(**event_data)
    # Push to our main event router
    background_tasks.add_task(EventDispatcher.dispatch, event)

    return {"status": "ok"}


@router.post("/hubspot")
async def hubspot_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receives webhooks from HubSpot.
    Verifies signature, then pushes to internal event router.
    """
    body = await request.body()
    signature = request.headers.get("X-HubSpot-Signature")

    if not signature:
        raise HTTPException(status_code=401, detail="Missing HubSpot signature")

    # Simple v1 signature check (SHA256 of client secret + body)
    if HUBSPOT_WEBHOOK_SECRET != "mock_hubspot_secret":
        source_string = HUBSPOT_WEBHOOK_SECRET + body.decode("utf-8")
        hash_res = hashlib.sha256(source_string.encode()).hexdigest()
        if not hmac.compare_digest(hash_res, signature):
            raise HTTPException(status_code=401, detail="Invalid HubSpot signature")

    payload = json.loads(body)

    event_data = {
        "event_id": str(uuid.uuid4()),
        "organization_id": "demo-org",  # HubSpot usually requires a lookup to map portalId -> org_id
        "source": "hubspot",
        "event_type": "hubspot.webhook",
        "payload": payload,
    }

    event = TitanEvent(**event_data)
    background_tasks.add_task(EventDispatcher.dispatch, event)

    return {"status": "ok"}
