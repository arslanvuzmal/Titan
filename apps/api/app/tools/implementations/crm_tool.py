import httpx
import logging
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
from app.tools.registry import BaseTool, ToolResult
from app.tools.security import ToolContext

logger = logging.getLogger(__name__)

class CRMParams(BaseModel):
    action_type: str = Field(..., description="'create_deal' or 'update_lead'")
    contact_email: str = Field(..., description="The email of the contact.")
    properties: Dict[str, Any] = Field(default_factory=dict, description="Properties to update (e.g., lead_score).")

class CRMTool(BaseTool):
    @property
    def name(self) -> str:
        return "update_crm"
        
    @property
    def description(self) -> str:
        return "Updates a CRM record (e.g., HubSpot) with new lead scores or deal properties."
        
    @property
    def parameters_schema(self) -> type[BaseModel]:
        return CRMParams

    async def execute(self, params: CRMParams, context: ToolContext) -> ToolResult:
        api_key = context.secrets.get("HUBSPOT_API_KEY")
        if not api_key:
            return ToolResult(status="FAILED", message="Missing HUBSPOT_API_KEY secret in context.")
            
        logger.info(f"[{context.task_id}] Executing CRM action '{params.action_type}' for {params.contact_email}")

        # Real httpx execution targeting HubSpot v3 API
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            try:
                # 1. Look up contact by email
                search_payload = {
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": "email",
                                    "operator": "EQ",
                                    "value": params.contact_email
                                }
                            ]
                        }
                    ]
                }
                
                search_res = await client.post(
                    "https://api.hubapi.com/crm/v3/objects/contacts/search",
                    json=search_payload,
                    headers=headers
                )
                search_data = search_res.json()
                
                if search_res.status_code != 200 or not search_data.get("results"):
                    return ToolResult(status="FAILED", message=f"Contact {params.contact_email} not found in CRM.")
                    
                contact_id = search_data["results"][0]["id"]
                
                # 2. Update the contact
                update_payload = {"properties": params.properties}
                update_res = await client.patch(
                    f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
                    json=update_payload,
                    headers=headers
                )
                
                if update_res.status_code in (200, 201):
                    return ToolResult(status="SUCCESS", message=f"Successfully updated CRM for {params.contact_email}")
                else:
                    return ToolResult(status="FAILED", message=f"HubSpot API Error: {update_res.text}")
                    
            except Exception as e:
                return ToolResult(status="FAILED", message=f"Network error executing CRM update: {str(e)}")

# Initialize for registry
crm_tool = CRMTool()
