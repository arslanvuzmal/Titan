"""Repricing the model calls the old adapters recorded as free.

The bug this repairs: NVIDIA and Gemini report token counts and no price, and
both adapters hardcoded ``cost_usd=0.0``. Every run they served landed in the
ledger at $0.00, so the budget caps had no spend to check against and could
never fire.

The ledger is append-only, so the repair appends adjustment entries rather than
rewriting anything -- these tests exist to hold that line, and the three other
ways a repair like this goes wrong: overwriting a real invoice with a guess,
inventing a figure for a row there is nothing to derive one from, and
double-counting on a rerun.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select
from titan.models.backfill import UNIT, reprice, survey

pytestmark = pytest.mark.asyncio


async def _call(
    session,
    workspace_id: uuid.UUID,
    *,
    provider: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float,
    category: str = "model",
) -> str:
    """One recorded model call, in both tables, exactly as the gateway writes it."""
    from titan.db.models import ModelRun, UsageLedger

    key = f"k-{uuid.uuid4().hex[:16]}"
    session.add(
        UsageLedger(
            workspace_id=workspace_id,
            idempotency_key=key,
            category=category,
            provider=provider,
            resource="a-model",
            quantity=1.0,
            unit="call",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            cost_estimated=True,
            occurred_at=dt.datetime.now(dt.UTC),
        )
    )
    session.add(
        ModelRun(
            workspace_id=workspace_id,
            idempotency_key=key,
            task="extraction",
            provider=provider,
            model_id="a-model",
            attempt=1,
            status="completed",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=10,
            cost_usd=cost_usd,
            request_hash="h",
            schema_valid=True,
            repair_attempts=0,
            used_fallback=False,
        )
    )
    await session.commit()
    return key


async def _entries(session, workspace_id: uuid.UUID) -> dict:
    from titan.db.models import UsageLedger

    session.expire_all()
    rows = (
        await session.execute(
            select(UsageLedger).where(UsageLedger.workspace_id == workspace_id)
        )
    ).scalars()
    return {row.idempotency_key: row for row in rows}


async def _spend(session, workspace_id: uuid.UUID) -> float:
    """What the /usage route reports: the sum over the whole ledger."""
    from titan.db.models import UsageLedger

    session.expire_all()
    total = await session.scalar(
        select(func.coalesce(func.sum(UsageLedger.cost_usd), 0.0)).where(
            UsageLedger.workspace_id == workspace_id
        )
    )
    return float(total or 0.0)


async def test_a_survey_reports_without_writing_anything(db_session, workspace) -> None:
    await _call(
        db_session,
        workspace,
        provider="nvidia",
        input_tokens=400_000,
        output_tokens=600_000,
        cost_usd=0.0,
    )

    report = await survey(workspace)

    assert report.applied is False
    assert report.rows == 1
    assert report.cost_usd == pytest.approx(0.20)
    assert len(await _entries(db_session, workspace)) == 1
    assert await _spend(db_session, workspace) == 0.0


async def test_an_unpriced_call_gains_an_adjustment_for_its_own_tokens(
    db_session, workspace
) -> None:
    key = await _call(
        db_session,
        workspace,
        provider="nvidia",
        input_tokens=400_000,
        output_tokens=600_000,
        cost_usd=0.0,
    )

    report = await reprice(workspace)
    assert report.applied is True
    assert report.written == 1

    entries = await _entries(db_session, workspace)
    adjustment = entries[f"{key}{'#repriced'}"]
    # 1M tokens at nvidia's $0.20/M hint.
    assert adjustment.cost_usd == pytest.approx(0.20)
    # Derived from a rate card, not an invoice, and the ledger says so.
    assert adjustment.cost_estimated is True
    assert adjustment.detail["adjusts"] == key
    assert await _spend(db_session, workspace) == pytest.approx(0.20)


async def test_the_original_row_is_left_exactly_as_it_was(db_session, workspace) -> None:
    """The ledger is append-only for a reason: the original still has to say
    what was recorded at the time, or there is no record of the defect."""
    key = await _call(
        db_session,
        workspace,
        provider="nvidia",
        input_tokens=400_000,
        output_tokens=600_000,
        cost_usd=0.0,
    )

    await reprice(workspace)

    original = (await _entries(db_session, workspace))[key]
    assert original.cost_usd == 0.0
    assert original.unit == "call"


async def test_an_adjustment_carries_no_tokens_and_no_call_count(
    db_session, workspace
) -> None:
    """The call it adjusts was already counted. Counting it twice would trade a
    wrong cost for a wrong call volume, which is not an improvement."""
    key = await _call(
        db_session,
        workspace,
        provider="gemini",
        input_tokens=1_000,
        output_tokens=1_570,
        cost_usd=0.0,
    )

    await reprice(workspace)

    adjustment = (await _entries(db_session, workspace))[f"{key}#repriced"]
    assert adjustment.cost_usd == pytest.approx(2_570 / 1_000_000 * 0.30)
    assert adjustment.quantity == 0.0
    assert adjustment.unit == UNIT
    assert adjustment.input_tokens is None
    assert adjustment.output_tokens is None


async def test_an_adjustment_is_dated_to_the_call_not_the_repair(
    db_session, workspace
) -> None:
    """Otherwise a spend query over last month stops reporting last month."""
    from titan.db.models import UsageLedger

    key = await _call(
        db_session,
        workspace,
        provider="nvidia",
        input_tokens=100_000,
        output_tokens=0,
        cost_usd=0.0,
    )
    original = (
        await db_session.execute(
            select(UsageLedger).where(UsageLedger.idempotency_key == key)
        )
    ).scalar_one()
    when = original.occurred_at

    await reprice(workspace)

    adjustment = (await _entries(db_session, workspace))[f"{key}#repriced"]
    assert adjustment.occurred_at == when


async def test_a_provider_reported_price_is_never_adjusted(db_session, workspace) -> None:
    """OpenRouter sends an actual price. A rate-card guess must not top it up."""
    key = await _call(
        db_session,
        workspace,
        provider="openrouter",
        input_tokens=5_000,
        output_tokens=2_383,
        cost_usd=0.034197,
    )

    report = await reprice(workspace)

    assert report.rows == 0
    assert report.reported_rows == 1
    assert report.written == 0

    entries = await _entries(db_session, workspace)
    assert f"{key}#repriced" not in entries
    assert await _spend(db_session, workspace) == pytest.approx(0.034197)


async def test_a_call_without_token_counts_is_left_at_zero(db_session, workspace) -> None:
    """No price and no tokens: there is nothing to derive a figure from, and a
    ledger number nobody can reproduce is worse than an absent one."""
    key = await _call(
        db_session,
        workspace,
        provider="nvidia",
        input_tokens=None,
        output_tokens=None,
        cost_usd=0.0,
    )

    report = await reprice(workspace)

    assert report.unpriceable == 1
    assert report.cost_usd == 0.0
    assert report.written == 0
    assert f"{key}#repriced" not in await _entries(db_session, workspace)
    assert await _spend(db_session, workspace) == 0.0


async def test_another_workspace_is_not_repriced(
    db_session, workspace, second_workspace
) -> None:
    """These statements run against __table__ and get no ORM workspace guard,
    so the filter has to be in the statement -- there is no RLS underneath."""
    await _call(
        db_session,
        workspace,
        provider="nvidia",
        input_tokens=500_000,
        output_tokens=500_000,
        cost_usd=0.0,
    )
    await _call(
        db_session,
        second_workspace,
        provider="nvidia",
        input_tokens=500_000,
        output_tokens=500_000,
        cost_usd=0.0,
    )

    await reprice(workspace)

    assert await _spend(db_session, workspace) == pytest.approx(0.20)
    assert await _spend(db_session, second_workspace) == 0.0


async def test_non_model_usage_is_out_of_scope(db_session, workspace) -> None:
    """The rate card prices model tokens. Places lookups and email sends are
    counted in the same table and priced by something else entirely."""
    key = await _call(
        db_session,
        workspace,
        provider="places",
        input_tokens=None,
        output_tokens=None,
        cost_usd=0.0,
        category="places",
    )

    report = await reprice(workspace)

    assert report.rows == 0
    assert f"{key}#repriced" not in await _entries(db_session, workspace)


async def test_a_second_run_adjusts_nothing_and_doubles_nothing(
    db_session, workspace
) -> None:
    """The failure this guards: an adjustment is itself a category='model' row,
    so a pass that did not exclude them would keep topping up its own work."""
    await _call(
        db_session,
        workspace,
        provider="nvidia",
        input_tokens=400_000,
        output_tokens=600_000,
        cost_usd=0.0,
    )

    await reprice(workspace)
    second = await reprice(workspace)

    assert second.rows == 0
    assert second.written == 0
    assert await _spend(db_session, workspace) == pytest.approx(0.20)
    assert len(await _entries(db_session, workspace)) == 2


async def test_model_runs_is_not_touched(db_session, workspace) -> None:
    """It is append-only too, and it is the record of invocations rather than
    the authoritative cost record. Leaving it is the honest option; the docs
    and the CLI both say where spend is read from."""
    from titan.db.models import ModelRun

    key = await _call(
        db_session,
        workspace,
        provider="nvidia",
        input_tokens=400_000,
        output_tokens=600_000,
        cost_usd=0.0,
    )

    await reprice(workspace)

    db_session.expire_all()
    runs = (
        (
            await db_session.execute(
                select(ModelRun).where(ModelRun.workspace_id == workspace)
            )
        )
        .scalars()
        .all()
    )
    assert [r.idempotency_key for r in runs] == [key]
    assert runs[0].cost_usd == 0.0


async def test_the_backfill_and_the_gateway_share_one_rate_card() -> None:
    """Two rate cards would drift, and the ledger would stop agreeing with
    itself across the date the fix shipped."""
    from titan.models import backfill, gateway

    assert backfill._PRICE_HINTS is gateway._PRICE_HINTS
