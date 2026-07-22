from dataclasses import dataclass, field
from typing import Dict
import os

@dataclass
class ToolContext:
    """
    Context passed to a tool during execution.
    Holds tenant identifiers and dynamically injected secrets.
    """
    organization_id: str
    user_id: str
    task_id: str
    secrets: Dict[str, str] = field(default_factory=dict)

def inject_secrets(tool_name: str) -> Dict[str, str]:
    """
    Securely fetches required API keys based on the tool being executed.
    These secrets are never exposed to the LLM.
    In a real system, this might fetch from AWS Secrets Manager or HashiCorp Vault.
    Here we fetch from the environment securely.
    """
    secrets = {}
    
    if tool_name == "send_email":
        # Required for SendGrid/SMTP
        secrets["SENDGRID_API_KEY"] = os.getenv("SENDGRID_API_KEY", "")
    elif tool_name == "update_crm":
        # Required for HubSpot/Salesforce
        secrets["HUBSPOT_API_KEY"] = os.getenv("HUBSPOT_API_KEY", "")
    elif tool_name == "search_web":
        # Required for Serper/Tavily
        secrets["SERPER_API_KEY"] = os.getenv("SERPER_API_KEY", "")
        
    return secrets
