"""Authentication and authorization.

Two defects from the pre-0.2 code are fixed here:

* **The role was trusted from a JWT claim** (gap analysis H-12). A token
  outlives revocation, so a demoted user kept their old privileges until it
  expired. The role is now read from ``workspace_members`` on every request.
* **RBAC was defined and applied to zero routes** (H-11). ``require`` is a
  dependency factory, and an invariant test asserts every mutating route uses
  one.

Failures return 404 rather than 403 for cross-workspace access: a 403 confirms
the resource exists, which is an existence oracle an attacker can enumerate.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select

from titan.config import Settings, get_settings
from titan.db.enums import ROLE_CAPABILITIES, SENSITIVE_CAPABILITIES, WorkspaceRole
from titan.db.models import User, WorkspaceMember
from titan.db.session import get_sessionmaker


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, with a role read from the database."""

    user_id: uuid.UUID
    email: str
    workspace_id: uuid.UUID
    role: WorkspaceRole

    @property
    def capabilities(self) -> frozenset[str]:
        return ROLE_CAPABILITIES[self.role]

    def can(self, capability: str) -> bool:
        return capability in self.capabilities


class AuthError(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


def issue_token(
    *, user_id: uuid.UUID, workspace_id: uuid.UUID, settings: Settings | None = None
) -> str:
    """Mint a session token.

    Deliberately carries no role: the role is authoritative in the database, and
    embedding it would recreate the staleness problem this design removes.
    """
    settings = settings or get_settings()
    if settings.local_jwt_secret is None:
        raise RuntimeError("TITAN_LOCAL_JWT_SECRET is not configured")
    now = dt.datetime.now(dt.UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "ws": str(workspace_id),
            "iat": int(now.timestamp()),
            "exp": int(
                (now + dt.timedelta(seconds=settings.session_ttl_seconds)).timestamp()
            ),
            "iss": "titan-os",
        },
        settings.local_jwt_secret.get_secret_value(),
        algorithm="HS256",
    )


async def current_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    """Resolve the caller, or reject.

    The membership lookup is the point: a token proves *who*, the database
    decides *what they may do*.
    """
    settings = get_settings()

    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("missing bearer token")
    token = authorization.split(" ", 1)[1].strip()

    if settings.auth_mode != "local":
        # Clerk verification would slot in here. Refusing is the correct
        # behaviour until it is implemented -- silently accepting would be worse.
        raise AuthError(f"auth_mode {settings.auth_mode!r} is not implemented")
    if settings.local_jwt_secret is None:
        raise AuthError("server authentication is not configured")

    try:
        claims = jwt.decode(
            token,
            settings.local_jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer="titan-os",
            options={"require": ["exp", "sub", "ws", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"invalid token: {type(exc).__name__}") from exc

    try:
        user_id = uuid.UUID(claims["sub"])
        workspace_id = uuid.UUID(claims["ws"])
    except (KeyError, ValueError) as exc:
        raise AuthError("malformed token claims") from exc

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(WorkspaceMember, User)
                .join(User, User.id == WorkspaceMember.user_id)
                .where(
                    WorkspaceMember.user_id == user_id,
                    WorkspaceMember.workspace_id == workspace_id,
                    User.is_active.is_(True),
                )
            )
        ).first()

    if row is None:
        # Covers a deleted user, a revoked membership, and a deactivated
        # account -- all of which a still-valid token would otherwise survive.
        raise AuthError("no active membership for this workspace")

    membership, user = row
    request.state.principal_id = str(user_id)
    return Principal(
        user_id=user_id,
        email=user.email,
        workspace_id=workspace_id,
        role=membership.role,
    )


def require(*capabilities: str):
    """Dependency factory enforcing capabilities server-side."""

    async def _check(
        principal: Principal = Depends(current_principal),
    ) -> Principal:
        missing = [c for c in capabilities if not principal.can(c)]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"role {principal.role.value} lacks: {', '.join(sorted(missing))}"
                ),
            )
        return principal

    return _check


def is_sensitive(capability: str) -> bool:
    """Whether exercising this capability must produce an audit_log row."""
    return capability in SENSITIVE_CAPABILITIES


__all__ = [
    "AuthError",
    "Principal",
    "current_principal",
    "is_sensitive",
    "issue_token",
    "require",
]
