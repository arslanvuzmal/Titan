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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import func, select

from titan.api.clerk import ClerkAuthError, ClerkVerifier
from titan.config import Settings, get_settings
from titan.db.enums import ROLE_CAPABILITIES, SENSITIVE_CAPABILITIES, WorkspaceRole
from titan.db.models import User, Workspace, WorkspaceMember
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


@lru_cache(maxsize=1)
def _verifier_for(issuer: str) -> ClerkVerifier:
    """One verifier per issuer, so the JWKS cache is shared across requests."""
    return ClerkVerifier(issuer)


def get_clerk_verifier(settings: Settings | None = None) -> ClerkVerifier:
    settings = settings or get_settings()
    if settings.clerk_issuer_url is None:
        raise AuthError("clerk auth is selected but TITAN_CLERK_ISSUER_URL is unset")
    return _verifier_for(str(settings.clerk_issuer_url))


def _decode_local(token: str, settings: Settings) -> tuple[uuid.UUID, uuid.UUID]:
    """Verify a locally-issued token. Returns (user_id, workspace_id)."""
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
        return uuid.UUID(claims["sub"]), uuid.UUID(claims["ws"])
    except (KeyError, ValueError) as exc:
        raise AuthError("malformed token claims") from exc


async def _resolve_clerk_user(
    token: str, settings: Settings, requested_workspace: str | None
) -> tuple[uuid.UUID, uuid.UUID]:
    """Verify a Clerk token and map it onto a Titan user and workspace.

    A Clerk token proves an identity at Clerk. It says nothing about which
    Titan workspace the caller is acting in, so the workspace is chosen here
    and the membership check downstream is what actually authorizes it.
    """
    verifier = get_clerk_verifier(settings)
    try:
        identity = verifier.verify(token)
    except ClerkAuthError as exc:
        raise AuthError(str(exc)) from exc

    async with get_sessionmaker()() as session:
        user = (
            await session.execute(
                select(User).where(User.external_subject == identity.subject)
            )
        ).scalar_one_or_none()

        if user is None and identity.email and identity.email_verified:
            # First sign-in for an account an operator already created. The
            # binding is done once, and only on an address Clerk states it has
            # verified -- an unverified claim would let anyone who can assert
            # someone's email take over their Titan account.
            user = (
                await session.execute(
                    select(User).where(
                        func.lower(User.email) == identity.email.strip().lower(),
                        User.external_subject.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if user is not None:
                user.external_subject = identity.subject
                await session.commit()

        if user is None or not user.is_active:
            # No implicit provisioning. An unknown subject is not an error to
            # explain in detail -- doing so would confirm which emails exist.
            raise AuthError("no Titan account is linked to this identity")

        memberships = (
            await session.execute(
                select(WorkspaceMember, Workspace)
                .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
                .where(WorkspaceMember.user_id == user.id)
            )
        ).all()

    if not memberships:
        raise AuthError("this identity has no workspace membership")

    if requested_workspace:
        wanted = requested_workspace.strip().lower()
        for _membership, workspace in memberships:
            if workspace.slug == wanted or str(workspace.id) == wanted:
                return user.id, workspace.id
        # Same message as "no membership": distinguishing them would reveal
        # which workspaces exist.
        raise AuthError("no active membership for this workspace")

    if len(memberships) > 1:
        raise AuthError(
            "this identity belongs to several workspaces; "
            "send the X-Titan-Workspace header"
        )
    return user.id, memberships[0][1].id


async def current_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_titan_workspace: str | None = Header(default=None),
) -> Principal:
    """Resolve the caller, or reject.

    The membership lookup is the point: a token proves *who*, the database
    decides *what they may do*. That holds for both auth modes -- the Clerk
    path returns an identity and nothing else, so a forged or stale role claim
    has nowhere to enter.
    """
    settings = get_settings()

    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("missing bearer token")
    token = authorization.split(" ", 1)[1].strip()

    if settings.auth_mode == "clerk":
        user_id, workspace_id = await _resolve_clerk_user(
            token, settings, x_titan_workspace
        )
    else:
        user_id, workspace_id = _decode_local(token, settings)

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


def require(*capabilities: str) -> Callable[..., Awaitable[Principal]]:
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
    "get_clerk_verifier",
    "is_sensitive",
    "issue_token",
    "require",
]
