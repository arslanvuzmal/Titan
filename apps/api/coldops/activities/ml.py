"""The shadow round: let every model in shadow read what has arrived, and keep what it said.

Runs on a schedule. Each round:

1. copies the rules' verdicts on replies into the shadow log (free, always on);
2. if ``COLDOPS_ML_REPLY_READER_ENABLED``, has the LLM reader read up to
   ``BATCH`` replies it has not read yet, and keeps each verdict.

Nothing here acts on a prediction. The model calls happen outside any
database transaction (mission section 25): read the batch, close the session,
call the model, open a new session to write.
"""

from __future__ import annotations

import logging
import uuid

from temporalio import activity

from coldops.config import get_settings
from coldops.db.session import workspace_unit_of_work
from coldops.ml import registry, reply_reader
from coldops.workflows.types import MlShadowInput, MlShadowResult

logger = logging.getLogger(__name__)

#: Replies read per round. Replies arrive in ones and twos; ten is headroom.
BATCH = 10


async def run_ml_shadow_now(request: MlShadowInput) -> MlShadowResult:
    settings = get_settings()
    workspace_id = uuid.UUID(request.workspace_id)

    async with workspace_unit_of_work(workspace_id) as session:
        rules = await reply_reader.record_rules_verdicts(
            session, workspace_id=workspace_id
        )

    if not settings.ml_reply_reader_enabled:
        return MlShadowResult(rules_recorded=rules, unavailable="LLM reply reader is off")

    from coldops.models.gateway import ModelGateway
    from coldops.models.providers import build_providers

    providers = build_providers(settings)
    if not providers:
        return MlShadowResult(
            rules_recorded=rules, unavailable="no model provider configured"
        )

    route = settings.model_route_extraction
    async with workspace_unit_of_work(workspace_id) as session:
        model_id = await registry.ensure_model(
            session,
            workspace_id=workspace_id,
            name=reply_reader.NAME,
            version=reply_reader.llm_version(route),
            kind="llm",
            config={"route": route, "prompt": reply_reader.PROMPT_VERSION},
        )
        batch = await reply_reader.unread_replies(
            session, workspace_id=workspace_id, model_id=model_id, limit=BATCH
        )

    gateway = ModelGateway(providers, settings)
    verdicts: list[tuple[uuid.UUID, reply_reader.ReaderVerdict]] = []
    failures: list[str] = []
    try:
        for inbound_id, subject, body in batch:
            try:
                verdicts.append(
                    (
                        inbound_id,
                        await reply_reader.read_one(gateway, subject=subject, body=body),
                    )
                )
            except Exception as exc:  # one unreadable reply is not a failed round
                failures.append(f"{inbound_id}: {type(exc).__name__}: {str(exc)[:120]}")
    finally:
        for provider in providers.values():
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()

    async with workspace_unit_of_work(workspace_id) as session:
        for inbound_id, verdict in verdicts:
            await registry.record_prediction(
                session,
                workspace_id=workspace_id,
                model_id=model_id,
                subject_kind=reply_reader.SUBJECT,
                subject_id=inbound_id,
                label=verdict.reply_class,
                score=verdict.confidence,
                # No excerpt: it is the sender's own words, and the shadow log
                # holds no personal data. The reply itself keeps them.
            )
    return MlShadowResult(
        rules_recorded=rules,
        llm_read=len(verdicts),
        failures=tuple(failures[:10]),
    )


@activity.defn(name="run_ml_shadow")
async def run_ml_shadow(request: MlShadowInput) -> MlShadowResult:
    return await run_ml_shadow_now(request)


ALL_ML_ACTIVITIES = [run_ml_shadow]

__all__ = ["ALL_ML_ACTIVITIES", "run_ml_shadow", "run_ml_shadow_now"]
