"""The reply desk over HTTP: list, edit, send.

Reading needs ``draft:read``; changing or sending needs ``approval:decide`` --
the same permission that approves a first message, because sending a reply is
an approval. Every edit and send names the draft version the operator was
looking at, so two people (or two tabs) cannot overwrite each other unseen.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from coldops.api.security import Principal, require
from coldops.db.session import workspace_session, workspace_unit_of_work
from coldops.outreach import reply_desk

router = APIRouter(prefix="/api/v1/reply-desk", tags=["reply desk"])


class DeskItemOut(BaseModel):
    draft_id: uuid.UUID
    draft_version: int
    lead_id: uuid.UUID
    business_name: str | None
    from_email: str
    received_at: dt.datetime
    their_subject: str | None
    their_words: str
    reply_class: str
    confidence: float
    subject: str
    body: str
    ready_to_send: bool
    status: str


class ReplyEdit(BaseModel):
    draft_version: int
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)


class ReplySend(BaseModel):
    draft_version: int


class ReplySentOut(BaseModel):
    outbox_id: uuid.UUID
    status: str


@router.get("", response_model=list[DeskItemOut])
async def list_replies(
    principal: Principal = Depends(require("draft:read")),
) -> list[DeskItemOut]:
    """Replies waiting for an answer, newest first."""
    async with workspace_session(principal.workspace_id) as session:
        items = await reply_desk.waiting(session, workspace_id=principal.workspace_id)
    return [DeskItemOut.model_validate(item, from_attributes=True) for item in items]


@router.post("/{draft_id}", response_model=DeskItemOut)
async def edit_reply(
    draft_id: uuid.UUID,
    payload: ReplyEdit,
    principal: Principal = Depends(require("approval:decide")),
) -> DeskItemOut:
    """Put the operator's own words in place of the suggestion."""
    async with workspace_unit_of_work(principal.workspace_id) as session:
        try:
            await reply_desk.edit(
                session,
                workspace_id=principal.workspace_id,
                draft_id=draft_id,
                seen_version=payload.draft_version,
                subject=payload.subject,
                body=payload.body,
            )
        except reply_desk.DeskError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        items = await reply_desk.waiting(session, workspace_id=principal.workspace_id)
    match = next((i for i in items if i.draft_id == draft_id), None)
    if match is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "reply not found")
    return DeskItemOut.model_validate(match, from_attributes=True)


@router.post("/{draft_id}/send", response_model=ReplySentOut, status_code=201)
async def send_reply(
    draft_id: uuid.UUID,
    payload: ReplySend,
    request: Request,
    principal: Principal = Depends(require("approval:decide")),
) -> ReplySentOut:
    """Approve and queue the reply, threaded under their message."""
    async with workspace_unit_of_work(principal.workspace_id) as session:
        try:
            outbox = await reply_desk.send(
                session,
                workspace_id=principal.workspace_id,
                draft_id=draft_id,
                seen_version=payload.draft_version,
                decided_by=principal.user_id,
                actor_ip=request.client.host if request.client else None,
            )
        except reply_desk.DeskError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        return ReplySentOut(outbox_id=outbox.id, status=outbox.status.value)


__all__ = ["router"]
