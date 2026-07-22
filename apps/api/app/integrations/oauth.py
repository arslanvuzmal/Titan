import os
import base64
import json
from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Request, Depends
from typing import Dict, Any

# Ensure we have a valid fernet key. In production, this MUST come from an env var.
# For local dev, we generate one if missing (though it won't persist across restarts).
_secret = os.getenv("TITAN_ENCRYPTION_KEY")
if not _secret:
    _secret = Fernet.generate_key().decode()
    os.environ["TITAN_ENCRYPTION_KEY"] = _secret

cipher_suite = Fernet(_secret.encode())

class OAuth2Vault:
    """
    Secure vault for encrypting and storing OAuth tokens.
    """
    @staticmethod
    def encrypt_token(token_data: dict) -> str:
        """Encrypts token JSON to a string."""
        json_str = json.dumps(token_data)
        return cipher_suite.encrypt(json_str.encode()).decode()

    @staticmethod
    def decrypt_token(encrypted_str: str) -> dict:
        """Decrypts a token string back to JSON."""
        try:
            json_str = cipher_suite.decrypt(encrypted_str.encode()).decode()
            return json.loads(json_str)
        except Exception as e:
            raise ValueError("Failed to decrypt token") from e

# --- FastAPI Endpoints ---
router = APIRouter()

# In-memory mock for the integrations table since we didn't push a Prisma schema update yet.
# Format: org_id -> { provider_name -> encrypted_token }
MOCK_INTEGRATIONS_DB: Dict[str, Dict[str, str]] = {}

@router.get("/{provider}/auth")
async def start_oauth(provider: str, org_id: str):
    """
    Starts the OAuth2 flow with PKCE.
    In a real implementation, we'd generate a code_verifier, store it in Redis,
    and redirect to the provider's authorize URL with the code_challenge.
    """
    supported_providers = ["gmail", "slack", "hubspot"]
    if provider not in supported_providers:
        raise HTTPException(status_code=400, detail="Unsupported provider")
        
    # Mocking the redirect URL for demonstration
    auth_url = f"https://{provider}.com/oauth/authorize?client_id=mock&response_type=code&state={org_id}"
    return {"auth_url": auth_url}

@router.get("/{provider}/callback")
async def oauth_callback(provider: str, code: str, state: str):
    """
    Handles the OAuth callback, exchanges code for token, and encrypts it at rest.
    """
    org_id = state  # the state param carries our org_id for correlation
    
    # Mocking the token exchange
    mock_token_data = {
        "access_token": f"mock_access_{provider}_{code}",
        "refresh_token": f"mock_refresh_{provider}",
        "expires_in": 3600
    }
    
    # 1. Encrypt the token at rest
    encrypted_token = OAuth2Vault.encrypt_token(mock_token_data)
    
    # 2. Store in "Database"
    if org_id not in MOCK_INTEGRATIONS_DB:
        MOCK_INTEGRATIONS_DB[org_id] = {}
    MOCK_INTEGRATIONS_DB[org_id][provider] = encrypted_token
    
    return {"status": "success", "message": f"{provider} integration secured."}
