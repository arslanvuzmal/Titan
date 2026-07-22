import httpx
import logging
from pydantic import BaseModel, EmailStr, Field
from app.tools.registry import BaseTool, ToolResult
from app.tools.security import ToolContext

logger = logging.getLogger(__name__)

class EmailParams(BaseModel):
    to_email: EmailStr = Field(..., description="The recipient email address.")
    subject: str = Field(..., description="The subject line of the email.")
    body: str = Field(..., description="The body content of the email.")

class EmailTool(BaseTool):
    @property
    def name(self) -> str:
        return "send_email"
        
    @property
    def description(self) -> str:
        return "Sends an email to a specified recipient. Use this for outreach or notifications."
        
    @property
    def parameters_schema(self) -> type[BaseModel]:
        return EmailParams

    async def execute(self, params: EmailParams, context: ToolContext) -> ToolResult:
        api_key = context.secrets.get("SENDGRID_API_KEY")
        if not api_key:
            return ToolResult(status="FAILED", message="Missing SENDGRID_API_KEY secret in context.")
            
        logger.info(f"[{context.task_id}] Executing send_email to {params.to_email}")

        # SendGrid v3 API Payload
        payload = {
            "personalizations": [{"to": [{"email": params.to_email}]}],
            "from": {"email": "titan@your-organization.com", "name": "TITAN AI"},
            "subject": params.subject,
            "content": [{"type": "text/plain", "value": params.body}]
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        # Real httpx execution
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    json=payload,
                    headers=headers,
                    timeout=10.0
                )
                
                if response.status_code in (200, 202):
                    return ToolResult(
                        status="SUCCESS", 
                        message=f"Email sent successfully to {params.to_email}"
                    )
                else:
                    return ToolResult(
                        status="FAILED", 
                        message=f"SendGrid API Error: {response.status_code} - {response.text}"
                    )
            except Exception as e:
                return ToolResult(status="FAILED", message=f"Network error executing send_email: {str(e)}")

# Initialize for registry
email_tool = EmailTool()
