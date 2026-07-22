import httpx
import logging
import socket
import ipaddress
from urllib.parse import urlparse
from pydantic import BaseModel, Field
from app.tools.registry import BaseTool, ToolResult
from app.tools.security import ToolContext

logger = logging.getLogger(__name__)

class WebSearchParams(BaseModel):
    query: str = Field(..., description="The search query.")

class WebSearchTool(BaseTool):
    @property
    def name(self) -> str:
        return "search_web"
        
    @property
    def description(self) -> str:
        return "Performs a web search to gather recent information on a topic or company."
        
    @property
    def parameters_schema(self) -> type[BaseModel]:
        return WebSearchParams
        
    def _validate_url_for_ssrf(self, target_url: str) -> bool:
        """
        CRITICAL SECURITY: Resolves the domain and checks if the IP falls into a private,
        loopback, or reserved IP range to prevent SSRF attacks.
        """
        try:
            parsed_url = urlparse(target_url)
            hostname = parsed_url.hostname
            if not hostname:
                return False
                
            ip_address = socket.gethostbyname(hostname)
            ip_obj = ipaddress.ip_address(ip_address)
            
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved or ip_obj.is_link_local:
                logger.error(f"SSRF Attempt blocked: {hostname} resolved to internal IP {ip_address}")
                return False
                
            return True
        except Exception as e:
            logger.error(f"Failed to resolve URL {target_url}: {str(e)}")
            return False

    async def execute(self, params: WebSearchParams, context: ToolContext) -> ToolResult:
        api_key = context.secrets.get("SERPER_API_KEY")
        if not api_key:
            return ToolResult(status="FAILED", message="Missing SERPER_API_KEY secret in context.")
            
        logger.info(f"[{context.task_id}] Executing web search for: '{params.query}'")
        
        target_api_url = "https://google.serper.dev/search"
        
        # Security validation before execution
        if not self._validate_url_for_ssrf(target_api_url):
            return ToolResult(status="FAILED", message="Security Exception: Invalid target URL for search API.")

        headers = {
            "X-API-KEY": api_key,
            "Content-Type": "application/json"
        }
        
        payload = {"q": params.query}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    target_api_url,
                    json=payload,
                    headers=headers,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    # Return a simplified dictionary of organic results
                    simplified = [{"title": res.get("title"), "snippet": res.get("snippet")} for res in data.get("organic", [])[:5]]
                    return ToolResult(status="SUCCESS", message="Search completed.", data={"results": simplified})
                else:
                    return ToolResult(status="FAILED", message=f"Serper API Error: {response.text}")
                    
            except Exception as e:
                return ToolResult(status="FAILED", message=f"Network error executing search: {str(e)}")

# Initialize for registry
web_search_tool = WebSearchTool()
