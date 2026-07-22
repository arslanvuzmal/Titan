from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from typing import List, Callable
from app.core.auth import UserContext, get_current_user


def require_role(allowed_roles: List[str]) -> Callable:
    """
    Dependency generator for Role-Based Access Control (RBAC).
    Verifies that the current user has one of the allowed roles.
    """

    def role_checker(user: UserContext = Depends(get_current_user)):
        if not user.role or user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this action",
            )
        return user

    return role_checker


async def get_tenant_context(
    user: UserContext = Depends(get_current_user),
) -> UserContext:
    """
    Middleware dependency that explicitly guarantees the presence of
    an organization_id in the user context for tenant isolation.
    """
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant context required. You must belong to an organization.",
        )
    return user


class TenantBaseModel(BaseModel):
    """
    Base Pydantic model for multi-tenant data structures.
    Any model that handles database input/output for a tenant
    should inherit from this to ensure organization_id is structurally enforced
    and never omitted by accident.
    """

    model_config = ConfigDict(from_attributes=True)

    organization_id: str
