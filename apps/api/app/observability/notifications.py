"""TITAN Alert Notification System."""

import logging
import httpx
from typing import Dict, Any, Optional

logger = logging.getLogger("titan.notifications")


class NotificationSystem:
    def __init__(self, slack_webhook_url: Optional[str] = None):
        self.slack_webhook_url = slack_webhook_url

    async def send_slack_alert(
        self, title: str, message: str, severity: str = "warning"
    ) -> None:
        """Send an alert to Slack via webhook."""
        if not self.slack_webhook_url:
            logger.warning("Slack webhook URL not configured, skipping alert.")
            return

        color = "danger" if severity == "critical" else "warning"
        payload = {
            "attachments": [
                {
                    "fallback": f"{severity.upper()}: {title}",
                    "color": color,
                    "title": title,
                    "text": message,
                }
            ]
        }

        async with httpx.AsyncClient() as client:
            try:
                await client.post(self.slack_webhook_url, json=payload)
                logger.info(f"Sent {severity} alert to Slack: {title}")
            except Exception as e:
                logger.error(f"Failed to send Slack alert: {e}")

    async def send_email_alert(self, subject: str, body: str) -> None:
        """Mock email notification for critical failures."""
        logger.info(f"Mock sending email alert: {subject}")

    async def trigger_pagerduty(
        self, incident_title: str, details: Dict[str, Any]
    ) -> None:
        """Mock PagerDuty integration for dev."""
        logger.info(f"Mock triggering PagerDuty incident: {incident_title}")


notifier = NotificationSystem()
