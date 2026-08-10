"""The /api/v1 surface.

Every route is workspace-scoped through :func:`workspace_session`, so a
forgotten WHERE clause yields nothing rather than another tenant's rows. Every
mutating route declares a capability. Every sensitive action writes an audit
entry inside the same transaction as the change.

Cross-workspace access returns **404, not 403**: a 403 confirms the resource
exists, which is an existence oracle.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import func, select

from titan.api import audit
from titan.api.crm import apply_lead_filters, enrich_leads
from titan.api.passwords import hash_passcode, verify_passcode
from titan.api.schemas import (
    ApprovalDecisionRequest,
    ApprovalOut,
    CampaignCreate,
    CampaignOut,
    CampaignPolicyOut,
    CampaignPolicyUpdate,
    DraftOut,
    EvidenceOut,
    FindingOut,
    LeadOut,
    LoginRequest,
    MessageOut,
    Page,
    ResearchStartRequest,
    ScoreOut,
    SendingAuthorizationRequest,
    SuppressionCreate,
    SuppressionOut,
    TokenResponse,
    UsageOut,
    WorkflowRunOut,
    WorkspaceOut,
)
from titan.api.security import Principal, current_principal, issue_token, require
from titan.config import MODE_RANK, OperatingMode, get_settings
from titan.db.enums import (
    ELIGIBLE_CONTACT_SOURCES,
    CampaignStatus,
    ContactSource,
    DraftStatus,
    Industry,
    SuppressionReason,
)
from titan.db.models import (
    AuditFinding,
    Campaign,
    CampaignPolicy,
    FindingEvidence,
    Lead,
    LeadScore,
    Message,
    MessageApproval,
    MessageDraft,
    SuppressionEntry,
    User,
    WorkflowRun,
    Workspace,
    WorkspaceMember,
)
from titan.db.session import (
    get_sessionmaker,
    workspace_session,
    workspace_unit_of_work,
)
from titan.delivery import quotas
from titan.delivery.suppression import suppress

router = APIRouter(prefix="/api/v1")

REQUIRED_ACK = "I authorize production sending for this campaign"


def _request_id(request: Request) -> str | None:
    return request.headers.get("x-request-id")


async def _not_found(kind: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{kind} not found")


# ==========================================================================
# Auth
# ==========================================================================
#: One message for every way a sign-in can fail. Unknown username, wrong
#: passcode, deactivated account and never-set passcode are indistinguishable,
#: because telling them apart is how an attacker enumerates valid accounts.
_INVALID = "invalid credentials"


@router.post("/auth/token", response_model=TokenResponse, tags=["auth"])
async def login(payload: LoginRequest) -> TokenResponse:
    """Issue a session token for a username and passcode.

    This route used to take an email address and a workspace slug and no
    secret, so it had to be refused in every deployed environment -- the
    environment check was standing in for authentication. It now verifies an
    argon2id hash, and the gate is what it should have been all along: this is
    the local identity provider, so it answers only when Titan is *configured*
    to authenticate locally. Selecting Clerk and then posting here still gets a
    501, in local development as much as in production.

    An account with no stored hash can never sign in. That covers every row
    that predates passcodes, so enabling this could not hand anyone a session
    they did not already have.
    """
    settings = get_settings()
    if settings.auth_mode != "local":
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "this deployment authenticates through Clerk; local tokens are disabled",
        )
    if settings.local_jwt_secret is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "local authentication is selected but TITAN_LOCAL_JWT_SECRET is unset",
        )

    username = payload.username.strip().lower()
    now = dt.datetime.now(dt.UTC)

    # Not `session.begin()`: a failed attempt has to *commit* its increment and
    # then raise. Wrapping the whole handler in one transaction would roll the
    # counter back on the very path that needs to record it, leaving a lockout
    # that never locks -- the failure mode is invisible, because the happy path
    # keeps working.
    async with get_sessionmaker()() as session:
        user = (
            await session.execute(
                select(User)
                .where(
                    func.lower(User.username) == username,
                    User.is_active.is_(True),
                )
                # Serialize concurrent attempts on one account. Without it,
                # parallel guesses read the same count and each write back
                # "1", so the limit is per-connection rather than per-account.
                .with_for_update()
            )
        ).scalar_one_or_none()

        if user is not None and user.locked_until and user.locked_until > now:
            # Say how long, and only to someone who already got this far. The
            # wait is not a secret -- concealing it just produces support
            # tickets -- but it is reported identically whether or not the
            # passcode was right, so it cannot be used as an oracle.
            wait = int((user.locked_until - now).total_seconds())
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"too many failed attempts; try again in {wait}s",
                headers={"Retry-After": str(wait)},
            )

        # Runs even when the username is unknown: `verify_passcode(None, ...)`
        # spends the same time against a placeholder hash, so timing does not
        # separate "no such account" from "wrong passcode".
        result = verify_passcode(user.password_hash if user else None, payload.passcode)

        if user is None or not result.ok:
            if user is not None:
                user.failed_login_count += 1
                if user.failed_login_count >= settings.login_max_attempts:
                    user.locked_until = now + dt.timedelta(
                        seconds=settings.login_lockout_seconds
                    )
                    user.failed_login_count = 0
                await session.commit()
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, _INVALID)

        memberships = (
            await session.execute(
                select(WorkspaceMember, Workspace)
                .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
                .where(WorkspaceMember.user_id == user.id)
                .order_by(Workspace.slug)
            )
        ).all()
        if not memberships:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, _INVALID)

        if len(memberships) == 1 and payload.workspace is None:
            membership, workspace = memberships[0]
        else:
            wanted = (payload.workspace or "").strip().lower()
            match = next(
                (
                    row
                    for row in memberships
                    if row[1].slug == wanted or str(row[1].id) == wanted
                ),
                None,
            )
            if match is None:
                # The passcode already checked out, so naming this operator's
                # own workspaces reveals nothing they do not have.
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "this account belongs to several workspaces; send one of: "
                    + ", ".join(sorted(w.slug for _m, w in memberships)),
                )
            membership, workspace = match

        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        if result.needs_rehash:
            # Cost parameters have gone up since this hash was written, and
            # the plaintext is in hand exactly once -- here.
            user.password_hash = hash_passcode(payload.passcode)

        response = TokenResponse(
            access_token=issue_token(
                user_id=user.id, workspace_id=workspace.id, settings=settings
            ),
            expires_in=settings.session_ttl_seconds,
            workspace_id=workspace.id,
            role=membership.role.value,
        )
        # After the token is minted, so a failure to mint one does not clear
        # the operator's failure counter.
        await session.commit()

    return response


@router.get("/me", response_model=dict, tags=["auth"])
async def me(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {
        "user_id": str(principal.user_id),
        "email": principal.email,
        "workspace_id": str(principal.workspace_id),
        "role": principal.role.value,
        "capabilities": sorted(principal.capabilities),
    }


# ==========================================================================
# Workspace
# ==========================================================================
@router.get("/workspace", response_model=WorkspaceOut, tags=["workspace"])
async def get_workspace(
    principal: Principal = Depends(require("workspace:read")),
) -> WorkspaceOut:
    async with workspace_session(principal.workspace_id) as session:
        workspace = await session.get(Workspace, principal.workspace_id)
        if workspace is None:
            raise await _not_found("workspace")
        return WorkspaceOut.model_validate(workspace)


# ==========================================================================
# Campaigns
# ==========================================================================
@router.post(
    "/campaigns",
    response_model=CampaignOut,
    status_code=status.HTTP_201_CREATED,
    tags=["campaigns"],
)
async def create_campaign(
    payload: CampaignCreate,
    request: Request,
    principal: Principal = Depends(require("campaign:write")),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CampaignOut:
    async with workspace_unit_of_work(principal.workspace_id) as session:
        existing = (
            await session.execute(select(Campaign).where(Campaign.slug == payload.slug))
        ).scalar_one_or_none()
        if existing is not None:
            # An idempotent create returns the original rather than 409, so a
            # retried request is safe.
            if idempotency_key:
                return CampaignOut.model_validate(existing)
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"campaign slug {payload.slug!r} already exists"
            )

        campaign = Campaign(
            workspace_id=principal.workspace_id,
            name=payload.name,
            slug=payload.slug,
            industry=Industry(payload.industry),
            status=CampaignStatus.DRAFT,
            target_business_type=payload.target_business_type,
            target_geography=payload.target_geography,
            target_country_code=payload.target_country_code,
            offer_summary=payload.offer_summary,
        )
        session.add(campaign)
        await session.flush()

        # A campaign is created in the most restrictive mode with sending off.
        # It cannot be born permissive.
        session.add(
            CampaignPolicy(
                workspace_id=principal.workspace_id,
                campaign_id=campaign.id,
                operating_mode=OperatingMode.RESEARCH_ONLY,
                sending_authorized=False,
                min_lead_score=payload.min_lead_score,
            )
        )
        await audit.record(
            session,
            workspace_id=principal.workspace_id,
            action="campaign.create",
            resource_type="campaign",
            resource_id=str(campaign.id),
            actor_user_id=principal.user_id,
            request_id=_request_id(request),
            detail={"slug": payload.slug, "name": payload.name},
        )
        return CampaignOut.model_validate(campaign)


@router.get("/campaigns", response_model=Page[CampaignOut], tags=["campaigns"])
async def list_campaigns(
    principal: Principal = Depends(require("campaign:read")),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    campaign_status: str | None = Query(None, alias="status"),
) -> Page[CampaignOut]:
    async with workspace_session(principal.workspace_id) as session:
        stmt = select(Campaign)
        if campaign_status:
            stmt = stmt.where(Campaign.status == CampaignStatus(campaign_status))
        total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            (
                await session.execute(
                    stmt.order_by(Campaign.created_at.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return Page(
            items=[CampaignOut.model_validate(r) for r in rows],
            total=int(total or 0),
            limit=limit,
            offset=offset,
        )


@router.get(
    "/campaigns/{campaign_id}/policy",
    response_model=CampaignPolicyOut,
    tags=["campaigns"],
)
async def get_policy(
    campaign_id: uuid.UUID,
    principal: Principal = Depends(require("campaign:read")),
) -> CampaignPolicyOut:
    async with workspace_session(principal.workspace_id) as session:
        policy = (
            await session.execute(
                select(CampaignPolicy).where(CampaignPolicy.campaign_id == campaign_id)
            )
        ).scalar_one_or_none()
        if policy is None:
            raise await _not_found("campaign")
        return CampaignPolicyOut.model_validate(policy)


@router.patch(
    "/campaigns/{campaign_id}/policy",
    response_model=CampaignPolicyOut,
    tags=["campaigns"],
)
async def update_policy(
    campaign_id: uuid.UUID,
    payload: CampaignPolicyUpdate,
    request: Request,
    principal: Principal = Depends(require("campaign:write")),
) -> CampaignPolicyOut:
    """Update campaign policy.

    A campaign may never be set to a mode more permissive than its workspace.
    That check lives here *and* in the policy engine; this one gives a clear
    error instead of a silently ineffective setting.
    """
    async with workspace_unit_of_work(principal.workspace_id) as session:
        policy = (
            await session.execute(
                select(CampaignPolicy).where(CampaignPolicy.campaign_id == campaign_id)
            )
        ).scalar_one_or_none()
        if policy is None:
            raise await _not_found("campaign")

        workspace = await session.get(Workspace, principal.workspace_id)
        if workspace is None:
            raise await _not_found("workspace")
        changes: dict[str, Any] = {}

        if payload.operating_mode is not None:
            requested = OperatingMode(payload.operating_mode)
            if MODE_RANK[requested] > MODE_RANK[workspace.operating_mode]:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"campaign mode {requested.value} exceeds the workspace ceiling "
                    f"{workspace.operating_mode.value}",
                )
            changes["operating_mode"] = requested.value
            policy.operating_mode = requested

        for field in (
            "min_lead_score",
            "daily_send_limit",
            "recipient_domain_daily_limit",
            "max_followups",
        ):
            value = getattr(payload, field)
            if value is not None:
                changes[field] = value
                setattr(policy, field, value)

        if changes:
            await audit.record(
                session,
                workspace_id=principal.workspace_id,
                action="campaign.policy.update",
                resource_type="campaign_policy",
                resource_id=str(campaign_id),
                actor_user_id=principal.user_id,
                request_id=_request_id(request),
                detail=changes,
            )
        return CampaignPolicyOut.model_validate(policy)


@router.post(
    "/campaigns/{campaign_id}/sending-authorization",
    response_model=CampaignPolicyOut,
    tags=["campaigns"],
)
async def set_sending_authorization(
    campaign_id: uuid.UUID,
    payload: SendingAuthorizationRequest,
    request: Request,
    principal: Principal = Depends(require("sending:enable")),
) -> CampaignPolicyOut:
    """Enable or disable delivery for a campaign.

    Separate route, separate capability, typed acknowledgement, always audited.
    The mission's requirement that "no API request should be able to activate
    live outreach using one casual boolean field" is what this shape exists for.
    """
    if payload.authorized and payload.acknowledgement.strip() != REQUIRED_ACK:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"acknowledgement must be exactly: {REQUIRED_ACK!r}",
        )

    async with workspace_unit_of_work(principal.workspace_id) as session:
        policy = (
            await session.execute(
                select(CampaignPolicy).where(CampaignPolicy.campaign_id == campaign_id)
            )
        ).scalar_one_or_none()
        if policy is None:
            raise await _not_found("campaign")

        policy.sending_authorized = payload.authorized
        await audit.record(
            session,
            workspace_id=principal.workspace_id,
            action="campaign.sending_authorization",
            resource_type="campaign_policy",
            resource_id=str(campaign_id),
            actor_user_id=principal.user_id,
            actor_ip=request.client.host if request.client else None,
            request_id=_request_id(request),
            detail={"authorized": payload.authorized},
        )
        return CampaignPolicyOut.model_validate(policy)


# ==========================================================================
# Leads, findings, evidence, scores
# ==========================================================================
#: Sort keys the CRM may order by. An allowlist rather than a raw column name,
#: so a query parameter can never reach the SQL as an identifier.
LEAD_SORTS: dict[str, Any] = {
    "score": Lead.latest_score,
    "created": Lead.created_at,
    "contacted": Lead.last_contacted_at,
    "next_action": Lead.next_action_at,
}


@router.get("/leads", response_model=Page[LeadOut], tags=["leads"])
async def list_leads(
    principal: Principal = Depends(require("research:read")),
    campaign_id: uuid.UUID | None = None,
    lead_status: str | None = Query(None, alias="status"),
    min_score: int | None = Query(None, ge=0, le=100),
    max_score: int | None = Query(None, ge=0, le=100),
    search: str | None = Query(None, alias="q", max_length=200),
    has_reply: bool | None = None,
    contacted: bool | None = None,
    sort: str = Query("score", pattern=r"^(score|created|contacted|next_action)$"),
    direction: str = Query("desc", pattern=r"^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[LeadOut]:
    """The CRM lead list.

    Rows come back enriched with the business, campaign, and activity counts
    (see :func:`titan.api.crm.enrich_leads`) because a list of bare UUIDs is
    not something a human can work from.
    """
    filters = {
        "campaign_id": campaign_id,
        "lead_status": lead_status,
        "min_score": min_score,
        "max_score": max_score,
        "search": search,
        "has_reply": has_reply,
        "contacted": contacted,
    }
    async with workspace_session(principal.workspace_id) as session:
        try:
            stmt = apply_lead_filters(select(Lead), **filters)  # type: ignore[arg-type]
            count_stmt = apply_lead_filters(
                select(func.count()).select_from(Lead),
                **filters,  # type: ignore[arg-type]
            )
        except ValueError as exc:  # an unknown status string
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

        total = await session.scalar(count_stmt)
        column = LEAD_SORTS[sort]
        ordering = column.desc() if direction == "desc" else column.asc()
        rows = (
            (
                await session.execute(
                    stmt.order_by(ordering.nullslast(), Lead.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return Page(
            items=await enrich_leads(session, rows),
            total=int(total or 0),
            limit=limit,
            offset=offset,
        )


@router.get("/leads/{lead_id}", response_model=LeadOut, tags=["leads"])
async def get_lead(
    lead_id: uuid.UUID,
    principal: Principal = Depends(require("research:read")),
) -> LeadOut:
    async with workspace_session(principal.workspace_id) as session:
        lead = await session.get(Lead, lead_id)
        if lead is None:
            raise await _not_found("lead")
        return (await enrich_leads(session, [lead]))[0]


@router.get(
    "/leads/{lead_id}/findings", response_model=list[FindingOut], tags=["research"]
)
async def lead_findings(
    lead_id: uuid.UUID,
    principal: Principal = Depends(require("research:read")),
) -> list[FindingOut]:
    async with workspace_session(principal.workspace_id) as session:
        if await session.get(Lead, lead_id) is None:
            raise await _not_found("lead")
        rows = (
            (
                await session.execute(
                    select(AuditFinding)
                    .where(AuditFinding.lead_id == lead_id)
                    .order_by(AuditFinding.confidence.desc())
                )
            )
            .scalars()
            .all()
        )
        return [FindingOut.model_validate(r) for r in rows]


@router.get(
    "/findings/{finding_id}/evidence",
    response_model=list[EvidenceOut],
    tags=["research"],
)
async def finding_evidence(
    finding_id: uuid.UUID,
    principal: Principal = Depends(require("research:read")),
) -> list[EvidenceOut]:
    async with workspace_session(principal.workspace_id) as session:
        if await session.get(AuditFinding, finding_id) is None:
            raise await _not_found("finding")
        rows = (
            (
                await session.execute(
                    select(FindingEvidence).where(
                        FindingEvidence.finding_id == finding_id
                    )
                )
            )
            .scalars()
            .all()
        )
        return [EvidenceOut.model_validate(r) for r in rows]


@router.get("/leads/{lead_id}/scores", response_model=list[ScoreOut], tags=["research"])
async def lead_scores(
    lead_id: uuid.UUID,
    principal: Principal = Depends(require("research:read")),
) -> list[ScoreOut]:
    async with workspace_session(principal.workspace_id) as session:
        if await session.get(Lead, lead_id) is None:
            raise await _not_found("lead")
        rows = (
            (
                await session.execute(
                    select(LeadScore)
                    .where(LeadScore.lead_id == lead_id)
                    .order_by(LeadScore.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [ScoreOut.model_validate(r) for r in rows]


# ==========================================================================
# Drafts and approvals
# ==========================================================================
@router.get("/drafts", response_model=Page[DraftOut], tags=["drafts"])
async def list_drafts(
    principal: Principal = Depends(require("draft:read")),
    draft_status: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[DraftOut]:
    async with workspace_session(principal.workspace_id) as session:
        stmt = select(MessageDraft)
        if draft_status:
            stmt = stmt.where(MessageDraft.status == DraftStatus(draft_status))
        total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            (
                await session.execute(
                    stmt.order_by(MessageDraft.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return Page(
            items=[DraftOut.model_validate(r) for r in rows],
            total=int(total or 0),
            limit=limit,
            offset=offset,
        )


@router.get("/drafts/{draft_id}", response_model=DraftOut, tags=["drafts"])
async def get_draft(
    draft_id: uuid.UUID,
    principal: Principal = Depends(require("draft:read")),
) -> DraftOut:
    async with workspace_session(principal.workspace_id) as session:
        draft = await session.get(MessageDraft, draft_id)
        if draft is None:
            raise await _not_found("draft")
        return DraftOut.model_validate(draft)


@router.post(
    "/drafts/{draft_id}/decision",
    response_model=ApprovalOut,
    status_code=status.HTTP_201_CREATED,
    tags=["approvals"],
)
async def decide_draft(
    draft_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    request: Request,
    principal: Principal = Depends(require("approval:decide")),
) -> ApprovalOut:
    """Record a human decision on a specific draft version.

    The pre-0.2 route queried a table that did not exist, swallowed the
    ownership check in a bare except, and then signalled Temporal with a
    caller-supplied workflow id. This one: scopes to the workspace, verifies the
    version the reviewer actually saw, writes an immutable approval, and audits.
    """
    async with workspace_unit_of_work(principal.workspace_id) as session:
        draft = await session.get(MessageDraft, draft_id)
        if draft is None:
            raise await _not_found("draft")

        if draft.version != payload.draft_version:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"draft has changed since you reviewed it "
                f"(you saw v{payload.draft_version}, it is now v{draft.version})",
            )
        if draft.status not in (
            DraftStatus.AWAITING_APPROVAL,
            DraftStatus.GENERATED,
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"draft is {draft.status.value} and cannot be decided",
            )
        if payload.decision == "approved" and not draft.validation_passed:
            # Belt and braces: a draft that failed validation must not be
            # approvable even by a privileged human.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "draft failed message validation and cannot be approved",
            )

        seq = int(
            await session.scalar(
                select(func.count())
                .select_from(MessageApproval)
                .where(
                    MessageApproval.draft_id == draft_id,
                    MessageApproval.draft_version == payload.draft_version,
                )
            )
            or 0
        )
        approval = MessageApproval(
            workspace_id=principal.workspace_id,
            draft_id=draft.id,
            draft_version=draft.version,
            decision_seq=seq + 1,
            decision=payload.decision,
            decided_by=principal.user_id,
            decided_at=dt.datetime.now(dt.UTC),
            reason=payload.reason,
            actor_ip=request.client.host if request.client else None,
            actor_user_agent=request.headers.get("user-agent", "")[:400] or None,
        )
        session.add(approval)

        draft.status = {
            "approved": DraftStatus.APPROVED,
            "rejected": DraftStatus.REJECTED,
            "changes_requested": DraftStatus.GENERATED,
        }[payload.decision]

        await audit.record(
            session,
            workspace_id=principal.workspace_id,
            action="draft.decision",
            resource_type="message_draft",
            resource_id=str(draft_id),
            actor_user_id=principal.user_id,
            actor_ip=request.client.host if request.client else None,
            request_id=_request_id(request),
            detail={"decision": payload.decision, "draft_version": draft.version},
        )
        await session.flush()
        return ApprovalOut.model_validate(approval)


# ==========================================================================
# Suppression
# ==========================================================================
@router.get("/suppressions", response_model=Page[SuppressionOut], tags=["compliance"])
async def list_suppressions(
    principal: Principal = Depends(require("suppression:read")),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[SuppressionOut]:
    async with workspace_session(principal.workspace_id) as session:
        stmt = select(SuppressionEntry)
        total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            (
                await session.execute(
                    stmt.order_by(SuppressionEntry.suppressed_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return Page(
            items=[SuppressionOut.model_validate(r) for r in rows],
            total=int(total or 0),
            limit=limit,
            offset=offset,
        )


@router.post(
    "/suppressions",
    response_model=SuppressionOut,
    status_code=status.HTTP_201_CREATED,
    tags=["compliance"],
)
async def create_suppression(
    payload: SuppressionCreate,
    request: Request,
    principal: Principal = Depends(require("suppression:write")),
) -> SuppressionOut:
    async with workspace_unit_of_work(principal.workspace_id) as session:
        entry = await suppress(
            session,
            workspace_id=principal.workspace_id,
            email_or_domain=payload.value,
            reason=SuppressionReason(payload.reason),
            source="api",
            created_by=principal.user_id,
            scope=payload.scope,
            detail={"note": payload.note} if payload.note else None,
        )
        await audit.record(
            session,
            workspace_id=principal.workspace_id,
            action="suppression.create",
            resource_type="suppression_entry",
            resource_id=str(entry.id),
            actor_user_id=principal.user_id,
            request_id=_request_id(request),
            detail={"scope": payload.scope, "reason": payload.reason},
        )
        return SuppressionOut.model_validate(entry)


# There is deliberately NO delete route for suppressions. Removing a
# do-not-contact record is not an ordinary operation; it requires a documented
# legal process, not an HTTP DELETE.


# ==========================================================================
# Messages, workflows, usage
# ==========================================================================
@router.get("/messages", response_model=Page[MessageOut], tags=["delivery"])
async def list_messages(
    principal: Principal = Depends(require("research:read")),
    lead_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[MessageOut]:
    async with workspace_session(principal.workspace_id) as session:
        stmt = select(Message)
        if lead_id:
            stmt = stmt.where(Message.lead_id == lead_id)
        total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            (
                await session.execute(
                    stmt.order_by(Message.created_at.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return Page(
            items=[MessageOut.model_validate(r) for r in rows],
            total=int(total or 0),
            limit=limit,
            offset=offset,
        )


@router.post(
    "/research/runs",
    response_model=WorkflowRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["research"],
)
async def start_research(
    payload: ResearchStartRequest,
    request: Request,
    principal: Principal = Depends(require("research:run")),
) -> WorkflowRunOut:
    """Start research for a lead.

    Returns immediately with the workflow identity (mission section 25). The
    request carries no policy: the workflow reads it from the database, so this
    route cannot widen what Titan may do (invariant 18).
    """
    from titan.workflows.research import research_workflow_id

    async with workspace_session(principal.workspace_id) as session:
        lead = await session.get(Lead, payload.lead_id)
        if lead is None:
            raise await _not_found("lead")
        campaign_id = lead.campaign_id

    workflow_id = research_workflow_id(
        str(principal.workspace_id), str(campaign_id), str(payload.lead_id)
    )

    async with workspace_unit_of_work(principal.workspace_id) as session:
        existing = (
            await session.execute(
                select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Deterministic workflow id: starting the same research twice is a
            # no-op rather than a second crawl.
            return WorkflowRunOut.model_validate(existing)

        run = WorkflowRun(
            workspace_id=principal.workspace_id,
            workflow_id=workflow_id,
            workflow_type="LeadResearchWorkflow",
            task_queue="titan-research",
            campaign_id=campaign_id,
            lead_id=payload.lead_id,
            started_at=dt.datetime.now(dt.UTC),
        )
        session.add(run)
        await audit.record(
            session,
            workspace_id=principal.workspace_id,
            action="research.start",
            resource_type="workflow_run",
            resource_id=workflow_id,
            actor_user_id=principal.user_id,
            request_id=_request_id(request),
            detail={"lead_id": str(payload.lead_id)},
        )
        await session.flush()
        return WorkflowRunOut.model_validate(run)


@router.get("/workflows", response_model=Page[WorkflowRunOut], tags=["workflows"])
async def list_workflows(
    principal: Principal = Depends(require("research:read")),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[WorkflowRunOut]:
    async with workspace_session(principal.workspace_id) as session:
        stmt = select(WorkflowRun)
        total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            (
                await session.execute(
                    stmt.order_by(WorkflowRun.started_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return Page(
            items=[WorkflowRunOut.model_validate(r) for r in rows],
            total=int(total or 0),
            limit=limit,
            offset=offset,
        )


@router.get("/usage", response_model=UsageOut, tags=["ops"])
async def usage(
    principal: Principal = Depends(require("workspace:read")),
) -> UsageOut:
    from titan.db.models import ModelRun, UsageLedger

    today = dt.datetime.now(dt.UTC).date()
    async with workspace_session(principal.workspace_id) as session:
        counters = await quotas.snapshot(
            session, workspace_id=principal.workspace_id, window_date=today
        )
        spend = await session.scalar(
            select(func.coalesce(func.sum(UsageLedger.cost_usd), 0.0))
        )
        calls = await session.scalar(select(func.count()).select_from(ModelRun))
    return UsageOut(
        window_date=today,
        quotas=counters,
        spend_usd=float(spend or 0.0),
        model_calls=int(calls or 0),
    )


@router.get("/contact-sources", response_model=dict, tags=["reference"])
async def contact_sources(
    principal: Principal = Depends(require("campaign:read")),
) -> dict[str, Any]:
    """Which contact provenances may be used, and which never may."""
    return {
        "eligible": sorted(s.value for s in ELIGIBLE_CONTACT_SOURCES),
        "never_eligible": [ContactSource.PATTERN_GUESS.value],
        "note": (
            "A pattern-guessed address is stored so Titan remembers not to guess "
            "it again, but it can never be sent to -- the eligibility set is "
            "code, not configuration."
        ),
    }


__all__ = ["router"]
