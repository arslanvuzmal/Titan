import os
import httpx
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from cachetools import TTLCache, cached
from pydantic import BaseModel

security = HTTPBearer()

# Default to empty string to prevent startup errors, but should be set in .env
CLERK_ISSUER = os.getenv("CLERK_ISSUER_URL", "")
CLERK_JWKS_URL = f"{CLERK_ISSUER}/.well-known/jwks.json" if CLERK_ISSUER else ""


class UserContext(BaseModel):
    user_id: str
    email: str | None = None
    organization_id: str
    role: str | None = None


# Cache the JWKS for 1 hour (3600 seconds)
@cached(cache=TTLCache(maxsize=1, ttl=3600))
def get_jwks() -> dict:
    if not CLERK_JWKS_URL:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CLERK_ISSUER_URL environment variable is not configured.",
        )
    try:
        response = httpx.get(CLERK_JWKS_URL, timeout=10.0)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not fetch JWKS keys: {str(e)}",
        )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserContext:
    """
    Extracts the Bearer token, fetches the JWKS, and verifies the JWT.
    Returns a structured UserContext.
    """
    token = credentials.credentials
    try:
        unverified_header = jwt.get_unverified_header(token)
        jwks = get_jwks()

        rsa_key = {}
        for key in jwks.get("keys", []):
            if key["kid"] == unverified_header.get("kid"):
                rsa_key = {
                    "kty": key["kty"],
                    "kid": key["kid"],
                    "use": key["use"],
                    "n": key["n"],
                    "e": key["e"],
                }
                break

        if not rsa_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token kid"
            )

        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=["RS256"],
            issuer=CLERK_ISSUER,
            options={"verify_aud": False},  # We check org_id explicitly instead
        )

        # In a multi-tenant setup, Clerk allows passing the active org ID inside the JWT claims.
        # We ensure it exists so that backend queries are strictly scoped.
        org_id = payload.get("org_id")
        if not org_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Organization ID missing from token. User must have an active workspace.",
            )

        return UserContext(
            user_id=payload.get("sub"),
            email=payload.get("email"),
            organization_id=org_id,
            role=payload.get("org_role"),
        )

    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not validate credentials: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )
