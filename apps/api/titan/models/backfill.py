"""Repricing historical model calls that were recorded as free.

NVIDIA and Gemini return token counts and no price. Both adapters used to
hardcode ``cost_usd=0.0``, so every run they served landed in the ledger at
$0.00 and ``BudgetLedger.workspace_spent`` never moved off zero -- a spend
guard that never saw spend. The gateway now prices an unpriced call from its
own token counts (:func:`titan.models.gateway.ModelGateway._settle_cost`); this
module applies the same arithmetic to the rows written before it did.

**It does not rewrite them.** ``usage_ledger`` is append-only, enforced by an
ORM listener, a database trigger, and an invariant test. That is the correct
design for a cost record and the repair has to respect it: each underpriced
call gets a second row carrying the difference, the way a ledger has always
been corrected. The original still says what was recorded at the time, the
adjustment says what it should have been, and ``sum(cost_usd)`` comes out right
without either being falsified.

An adjustment carries no tokens and ``quantity=0.0``: the call it adjusts was
already counted, and counting it twice would trade a wrong cost for a wrong
call volume.

Two things this deliberately does not do:

* It never adjusts a row that already carries a provider-reported price.
  OpenRouter sends one, and a rate-card guess must not overwrite an invoice.
* It never prices a row with no token counts. There is nothing to price it
  from, and inventing a figure for the ledger is the failure mode the
  ``cost_estimated`` column exists to prevent.

``model_runs`` is likewise append-only and keeps its $0.00. It is the record of
invocations, not the authoritative cost record -- ``usage_ledger`` is, and it
is where a reader is pointed for spend.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from titan.db.models import UsageLedger
from titan.db.session import get_sessionmaker
from titan.models.gateway import _PRICE_HINTS
from titan.models.recording import CATEGORY

#: Rows priced below this are indistinguishable from unpriced ones in float
#: terms. Nothing legitimate lands here: the cheapest real call in the ledger
#: is several orders of magnitude above it.
_ZERO = 1e-12

#: Suffix that turns an original row's key into its adjustment's key. The
#: unique constraint on (workspace_id, idempotency_key) is then what makes a
#: second run a no-op rather than a doubling.
_SUFFIX = "#repriced"

#: Marks an adjustment row. ``quantity=0.0`` already keeps it out of call
#: counts; this makes it legible to a human reading the table.
UNIT = "adjustment"


@dataclass(frozen=True, slots=True)
class ProviderRepricing:
    """What one provider's unpriced history comes to."""

    provider: str
    rows: int
    tokens: int
    #: Rows with no token counts at all -- left alone, and reported so that the
    #: total is never mistaken for "everything is now priced".
    unpriceable: int
    rate_per_million: float

    @property
    def cost_usd(self) -> float:
        return (self.tokens / 1_000_000) * self.rate_per_million

    def line(self) -> str:
        note = f"  ({self.unpriceable} without token counts, left at $0.00)"
        return (
            f"{self.provider:<12} {self.rows:>6} rows  "
            f"{self.tokens:>10,} tokens  @ ${self.rate_per_million:.2f}/M  "
            f"= ${self.cost_usd:.6f}" + (note if self.unpriceable else "")
        )


@dataclass(frozen=True, slots=True)
class BackfillReport:
    providers: list[ProviderRepricing] = field(default_factory=list)
    #: Rows whose price came from the provider. Left exactly as they are.
    reported_rows: int = 0
    #: Adjustment rows actually written. Lower than ``rows`` on a rerun, where
    #: the adjustments already exist.
    written: int = 0
    applied: bool = False

    @property
    def rows(self) -> int:
        return sum(p.rows for p in self.providers)

    @property
    def unpriceable(self) -> int:
        return sum(p.unpriceable for p in self.providers)

    @property
    def cost_usd(self) -> float:
        return sum(p.cost_usd for p in self.providers)


async def survey(workspace_id: uuid.UUID) -> BackfillReport:
    """Report what repricing would add, writing nothing."""
    return await _run(workspace_id, apply=False)


async def reprice(workspace_id: uuid.UUID) -> BackfillReport:
    """Append the adjustments, and report what was written."""
    return await _run(workspace_id, apply=True)


def _tokens_of(row: UsageLedger) -> int:
    return (row.input_tokens or 0) + (row.output_tokens or 0)


async def _run(workspace_id: uuid.UUID, *, apply: bool) -> BackfillReport:
    ledger = UsageLedger.__table__

    # Written against the table rather than through the ORM guard, so every
    # statement carries its own workspace filter -- there is no RLS underneath
    # to catch a missing one.
    scope = (ledger.c.workspace_id == workspace_id) & (ledger.c.category == CATEGORY)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # A row that already has an adjustment is not outstanding, and saying
        # otherwise would leave a dry run reporting money that was accounted
        # for on the last pass -- a figure nobody could reconcile. The unique
        # constraint already stops a double write; this stops a double claim.
        adjusted = ledger.alias("adjustment")
        already = (
            select(adjusted.c.id)
            .where(
                adjusted.c.workspace_id == workspace_id,
                adjusted.c.idempotency_key == ledger.c.idempotency_key + _SUFFIX,
            )
            .exists()
        )

        originals = (
            (
                await session.execute(
                    select(UsageLedger).where(
                        scope,
                        func.abs(ledger.c.cost_usd) < _ZERO,
                        # An adjustment is itself a category='model' row, so
                        # excluding them keeps a rerun off its own work.
                        ledger.c.unit != UNIT,
                        ~already,
                    )
                )
            )
            .scalars()
            .all()
        )

        reported = (
            await session.execute(
                select(func.count())
                .select_from(ledger)
                .where(
                    scope,
                    func.abs(ledger.c.cost_usd) >= _ZERO,
                    ledger.c.unit != UNIT,
                )
            )
        ).scalar_one()

        totals: dict[str, list[int]] = {}
        for row in originals:
            tokens = _tokens_of(row)
            bucket = totals.setdefault(row.provider, [0, 0, 0])
            bucket[0] += 1
            bucket[1] += tokens
            if tokens == 0:
                bucket[2] += 1

        providers = [
            ProviderRepricing(
                provider=name,
                rows=count,
                tokens=tokens,
                unpriceable=unpriceable,
                # An unknown provider gets the same $1.00/M the pre-call
                # estimate assumes. Guessing high is the safe direction for a
                # number a budget cap is checked against.
                rate_per_million=_PRICE_HINTS.get(name, 1.0),
            )
            for name, (count, tokens, unpriceable) in sorted(totals.items())
        ]

        if not apply:
            return BackfillReport(
                providers=providers, reported_rows=int(reported), applied=False
            )

        written = 0
        for row in originals:
            tokens = _tokens_of(row)
            if tokens == 0:
                continue
            rate = _PRICE_HINTS.get(row.provider, 1.0)
            inserted = await session.execute(
                pg_insert(ledger)  # type: ignore[arg-type]
                .values(
                    workspace_id=workspace_id,
                    idempotency_key=f"{row.idempotency_key}{_SUFFIX}"[:255],
                    # Carried so per-campaign and per-lead spend stays
                    # attributed to whatever the call was actually for.
                    campaign_id=row.campaign_id,
                    lead_id=row.lead_id,
                    category=CATEGORY,
                    provider=row.provider,
                    resource=row.resource,
                    quantity=0.0,
                    unit=UNIT,
                    # The tokens belong to the original row. Repeating them
                    # here would trade a wrong cost for a wrong token total.
                    input_tokens=None,
                    output_tokens=None,
                    cost_usd=(tokens / 1_000_000) * rate,
                    # Derived from a rate card, not an invoice, and the column
                    # exists to say exactly that.
                    cost_estimated=True,
                    # Dated to the call, not to the repair, so a spend query
                    # over last month still reports last month's spend.
                    occurred_at=row.occurred_at,
                    detail={
                        "adjusts": row.idempotency_key,
                        "reason": "provider reported no cost",
                        "rate_per_million_usd": rate,
                        "tokens": tokens,
                    },
                )
                .on_conflict_do_nothing(
                    index_elements=["workspace_id", "idempotency_key"]
                )
                .returning(ledger.c.id)
            )
            if inserted.scalar_one_or_none() is not None:
                written += 1

        await session.commit()

    return BackfillReport(
        providers=providers,
        reported_rows=int(reported),
        written=written,
        applied=True,
    )


__all__ = ["UNIT", "BackfillReport", "ProviderRepricing", "reprice", "survey"]
