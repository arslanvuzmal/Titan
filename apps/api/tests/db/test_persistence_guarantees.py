"""Proofs that the persistence layer actually enforces what the design claims.

Every test here targets a specific invariant from mission section 28 or a
specific finding from the gap analysis. They run against a real PostgreSQL --
see tests/conftest.py for why.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from titan.db.base import ImmutableMixin, WorkspaceScoped
from titan.db.models import (
    Base,
    Campaign,
    Lead,
    Organization,
    SuppressionEntry,
)
from titan.db.session import (
    ImmutableRowError,
    get_sessionmaker,
    workspace_session,
    workspace_unit_of_work,
)
from titan.delivery import quotas

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# Schema shape (gap analysis H-03, H-07, C-07)
# --------------------------------------------------------------------------
def test_every_scoped_table_has_workspace_id() -> None:
    """A table that forgets WorkspaceScoped would silently escape isolation."""
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if not issubclass(cls, WorkspaceScoped):
            continue
        table = cls.__table__
        assert "workspace_id" in table.c, f"{table.name} lacks workspace_id"
        assert not table.c.workspace_id.nullable, (
            f"{table.name}.workspace_id must be NOT NULL"
        )


def test_all_primary_keys_are_uuid() -> None:
    """Mission section 5 requires UUID PKs; the pre-0.2 schema used cuid."""
    for mapper in Base.registry.mappers:
        table = mapper.class_.__table__
        for col in table.primary_key.columns:
            assert col.type.python_type is uuid.UUID, (
                f"{table.name}.{col.name} is {col.type}, expected UUID"
            )


@pytest.mark.asyncio
async def test_immutable_tables_have_update_triggers(db_session) -> None:
    """The ORM guard is not the only defence; raw SQL must be blocked too."""
    immutable = {
        m.class_.__tablename__
        for m in Base.registry.mappers
        if issubclass(m.class_, ImmutableMixin)
    }
    rows = await db_session.execute(
        text(
            "SELECT c.relname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname LIKE '%_no_update' AND NOT t.tgisinternal"
        )
    )
    protected = {r[0] for r in rows}
    assert immutable <= protected, f"missing UPDATE triggers on {immutable - protected}"


@pytest.mark.asyncio
async def test_row_level_security_is_enabled_on_scoped_tables(db_session) -> None:
    scoped = {
        m.class_.__tablename__
        for m in Base.registry.mappers
        if issubclass(m.class_, WorkspaceScoped)
    }
    rows = await db_session.execute(
        text("SELECT relname FROM pg_class WHERE relrowsecurity")
    )
    secured = {r[0] for r in rows}
    assert scoped <= secured, f"RLS not enabled on {scoped - secured}"


# --------------------------------------------------------------------------
# Immutability (evidence and compliance records cannot be rewritten)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_orm_refuses_to_update_an_immutable_row(db_session, workspace) -> None:
    entry = SuppressionEntry(
        workspace_id=workspace,
        scope="email",
        normalized_value="someone@example.invalid",
        reason="unsubscribe",
        source="test",
        suppressed_at=dt.datetime.now(dt.UTC),
    )
    db_session.add(entry)
    await db_session.commit()

    entry.reason = "manual"
    with pytest.raises(ImmutableRowError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_database_trigger_refuses_raw_sql_update(db_session, workspace) -> None:
    """Defence in depth: bypassing the ORM must not bypass immutability."""
    entry = SuppressionEntry(
        workspace_id=workspace,
        scope="email",
        normalized_value="raw@example.invalid",
        reason="complaint",
        source="test",
        suppressed_at=dt.datetime.now(dt.UTC),
    )
    db_session.add(entry)
    await db_session.commit()

    with pytest.raises(DBAPIError) as excinfo:
        await db_session.execute(
            text("UPDATE suppression_entries SET source = 'tampered' WHERE id = :i"),
            {"i": entry.id},
        )
    assert "append-only" in str(excinfo.value)
    await db_session.rollback()


# --------------------------------------------------------------------------
# Workspace isolation (invariant 17, gap analysis C-07)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scoped_session_hides_other_workspaces(
    db_session, workspace, second_workspace
) -> None:
    """A query with NO explicit workspace filter must still be isolated.

    This is the specific defect the pre-0.2 code had: isolation depended on
    each handler remembering its WHERE clause.
    """
    for ws, name in ((workspace, "Mine Ltd"), (second_workspace, "Theirs Ltd")):
        db_session.add(
            Organization(
                workspace_id=ws,
                display_name=name,
                normalized_name=name.lower(),
            )
        )
    await db_session.commit()

    async with workspace_session(workspace) as scoped:
        # Deliberately unfiltered -- the guard must supply the predicate.
        # Names are read inside the block; the rollback on exit detaches them.
        names = {
            o.display_name
            for o in (await scoped.execute(select(Organization))).scalars().all()
        }

    assert "Mine Ltd" in names
    assert "Theirs Ltd" not in names, "cross-workspace leak through an unfiltered query"


@pytest.mark.asyncio
async def test_scoped_session_cannot_fetch_foreign_row_by_id(
    db_session, workspace, second_workspace
) -> None:
    """Knowing the UUID of another tenant's row must not be enough."""
    other = Organization(
        workspace_id=second_workspace,
        display_name="Secret Co",
        normalized_name="secret co",
    )
    db_session.add(other)
    await db_session.commit()
    other_id = other.id

    async with workspace_session(workspace) as scoped:
        row = (
            await scoped.execute(select(Organization).where(Organization.id == other_id))
        ).scalar_one_or_none()
    assert row is None


# --------------------------------------------------------------------------
# Lead deduplication (mission section 5.2)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_place_id_is_unique_per_workspace(db_session, workspace) -> None:
    place_id = f"ChIJ{uuid.uuid4().hex[:16]}"
    for name in ("First Co", "Impostor Co"):
        db_session.add(
            Organization(
                workspace_id=workspace,
                display_name=name,
                normalized_name=name.lower(),
                google_place_id=place_id,
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_null_place_ids_do_not_collide(db_session, workspace) -> None:
    """The uniqueness index is partial; many orgs may lack a place ID."""
    for i in range(3):
        db_session.add(
            Organization(
                workspace_id=workspace,
                display_name=f"No Place {i}",
                normalized_name=f"no place {i}",
                google_place_id=None,
                canonical_domain=None,
            )
        )
    await db_session.commit()  # must not raise
    await db_session.rollback()


@pytest.mark.asyncio
async def test_same_place_id_allowed_in_different_workspaces(
    db_session, workspace, second_workspace
) -> None:
    place_id = f"ChIJ{uuid.uuid4().hex[:16]}"
    for ws in (workspace, second_workspace):
        db_session.add(
            Organization(
                workspace_id=ws,
                display_name="Shared Business",
                normalized_name="shared business",
                google_place_id=place_id,
            )
        )
    await db_session.commit()  # tenants are independent
    await db_session.rollback()


# --------------------------------------------------------------------------
# Quota atomicity (invariant 14, gap analysis C-06)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_quota_never_overshoots_under_concurrency(workspace) -> None:
    """32 concurrent reservations against a limit of 10 must grant exactly 10.

    Each reservation runs on its own connection and its own transaction, which
    is what a fleet of outbox workers looks like.
    """
    limit = 10
    attempts = 32
    scope_key = f"campaign-{uuid.uuid4()}"
    window = dt.date(2026, 8, 2)
    maker = get_sessionmaker()

    async def try_reserve() -> bool:
        async with maker() as session, session.begin():
            outcome = await quotas.reserve_all(
                session,
                workspace_id=workspace,
                requests=[
                    quotas.QuotaRequest(quotas.QuotaScope.CAMPAIGN, scope_key, limit)
                ],
                window_date=window,
            )
            return outcome.granted

    results = await asyncio.gather(*(try_reserve() for _ in range(attempts)))
    granted = sum(results)
    assert granted == limit, f"expected exactly {limit} grants, got {granted}"

    async with maker() as session:
        used = await session.scalar(
            text(
                "SELECT used FROM quota_counters WHERE workspace_id=:w "
                "AND scope_type='campaign' AND scope_key=:k AND window_date=:d"
            ),
            {"w": workspace, "k": scope_key, "d": window},
        )
    assert used == limit


@pytest.mark.asyncio
async def test_multi_scope_reservation_rolls_back_on_later_refusal(
    workspace,
) -> None:
    """If the domain quota refuses, the workspace quota must not stay consumed."""
    window = dt.date(2026, 8, 3)
    ws_key = str(workspace)
    domain_key = f"blocked-{uuid.uuid4().hex[:8]}.example"
    maker = get_sessionmaker()

    async with maker() as session, session.begin():
        outcome = await quotas.reserve_all(
            session,
            workspace_id=workspace,
            requests=[
                quotas.QuotaRequest(quotas.QuotaScope.WORKSPACE, ws_key, 100),
                # limit 0 == closed; must refuse and unwind the first reservation
                quotas.QuotaRequest(quotas.QuotaScope.RECIPIENT_DOMAIN, domain_key, 0),
            ],
            window_date=window,
        )
    assert not outcome.granted
    assert outcome.exhausted_scope is quotas.QuotaScope.RECIPIENT_DOMAIN

    async with maker() as session:
        used = await session.scalar(
            text(
                "SELECT used FROM quota_counters WHERE workspace_id=:w "
                "AND scope_type='workspace' AND scope_key=:k AND window_date=:d"
            ),
            {"w": workspace, "k": ws_key, "d": window},
        )
    assert used == 0, "workspace quota leaked after a downstream refusal"


def test_deferral_time_is_deterministic_and_spread() -> None:
    now = dt.datetime(2026, 8, 2, 18, 30, tzinfo=dt.UTC)
    a1 = quotas.next_window_start(now, "msg-a")
    a2 = quotas.next_window_start(now, "msg-a")
    b = quotas.next_window_start(now, "msg-b")

    assert a1 == a2, "retry must compute the same next-attempt instant"
    assert a1 != b, "different messages must not stampede at the same instant"
    for value in (a1, b):
        assert value.date() == dt.date(2026, 8, 3)
        assert (
            0 <= (value - dt.datetime(2026, 8, 3, tzinfo=dt.UTC)).total_seconds() < 3600
        )


# --------------------------------------------------------------------------
# Transaction hygiene (gap analysis C-09)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unit_of_work_rolls_back_on_exception(db_session, workspace) -> None:
    marker = f"rollback-{uuid.uuid4().hex[:8]}"
    with pytest.raises(RuntimeError):
        async with workspace_unit_of_work(workspace) as session:
            session.add(
                Organization(
                    workspace_id=workspace,
                    display_name=marker,
                    normalized_name=marker,
                )
            )
            await session.flush()
            raise RuntimeError("boom")

    remaining = (
        await db_session.execute(
            select(Organization).where(Organization.display_name == marker)
        )
    ).scalar_one_or_none()
    assert remaining is None


@pytest.mark.asyncio
async def test_optimistic_locking_detects_lost_update(db_session, workspace) -> None:
    """Two writers editing the same campaign must not silently overwrite."""
    from sqlalchemy.orm.exc import StaleDataError

    campaign = Campaign(
        workspace_id=workspace,
        name="Race",
        slug=f"race-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(campaign)
    await db_session.commit()

    maker = get_sessionmaker()
    async with maker() as s1, maker() as s2:
        c1 = await s1.get(Campaign, campaign.id)
        c2 = await s2.get(Campaign, campaign.id)
        c1.name = "Writer one"
        await s1.commit()

        c2.name = "Writer two"
        with pytest.raises(StaleDataError):
            await s2.commit()


# --------------------------------------------------------------------------
# Suppression survives contact deletion (mission section 24)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_suppression_has_no_foreign_key_to_contacts() -> None:
    """Deleting a contact must never delete the record of their opt-out."""
    fks = SuppressionEntry.__table__.foreign_keys
    targets = {fk.column.table.name for fk in fks}
    assert "contacts" not in targets
    assert "contact_channels" not in targets
    assert "leads" not in targets
    # Only workspace and the acting user may be referenced.
    assert targets <= {"workspaces", "users"}, f"unexpected FK targets: {targets}"


@pytest.mark.asyncio
async def test_deleting_a_lead_leaves_suppression_intact(db_session, workspace) -> None:
    campaign = Campaign(
        workspace_id=workspace, name="C", slug=f"c-{uuid.uuid4().hex[:8]}"
    )
    org = Organization(workspace_id=workspace, display_name="Org", normalized_name="org")
    db_session.add_all([campaign, org])
    await db_session.flush()
    lead = Lead(workspace_id=workspace, campaign_id=campaign.id, organization_id=org.id)
    address = f"gone-{uuid.uuid4().hex[:8]}@example.invalid"
    db_session.add_all(
        [
            lead,
            SuppressionEntry(
                workspace_id=workspace,
                scope="email",
                normalized_value=address,
                reason="unsubscribe",
                source="webhook",
                suppressed_at=dt.datetime.now(dt.UTC),
            ),
        ]
    )
    await db_session.commit()

    await db_session.delete(lead)
    await db_session.commit()

    still_there = (
        await db_session.execute(
            select(SuppressionEntry).where(SuppressionEntry.normalized_value == address)
        )
    ).scalar_one_or_none()
    assert still_there is not None, "suppression must outlive the lead"


# --------------------------------------------------------------------------
# The migration chain describes the models it claims to
# --------------------------------------------------------------------------
def test_the_migration_chain_has_exactly_one_head() -> None:
    """Two heads stop every migration, including unrelated ones.

    This is what stranded production for two days: a branch applied to the live
    database was never pushed, so the stamped revision named a file nobody had
    and Alembic refused to move at all. A second head is easy to create by
    accident on a branch and invisible until someone deploys.
    """
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "titan" / "db" / "migrations"))

    heads = ScriptDirectory.from_config(config).get_heads()

    assert len(heads) == 1, (
        f"the migration chain has {len(heads)} heads: {heads}. "
        "Merge them with `alembic merge`, or no database can be upgraded."
    )


async def test_every_model_table_exists_in_the_migrated_database(db_session) -> None:
    """The models and the migrations must describe the same schema.

    CI runs `alembic check`, which catches drift in the other direction. This
    catches the case that check cannot: a table declared in the models whose
    migration was never written, which looks fine until the first query.
    """
    from sqlalchemy import inspect

    def table_names(connection) -> set[str]:
        return set(inspect(connection).get_table_names())

    live = await db_session.run_sync(lambda s: table_names(s.connection()))
    expected = set(Base.metadata.tables)

    missing = sorted(expected - live)
    assert not missing, (
        f"these tables are declared in the models but absent from the migrated "
        f"database: {missing}"
    )


# --------------------------------------------------------------------------
# Model spend is recorded
# --------------------------------------------------------------------------
async def test_model_calls_are_written_to_both_ledgers(db_session, workspace) -> None:
    """``model_runs`` and ``usage_ledger`` had no writer since the first release.

    The gateway has always collected calls under the comment "for the usage
    ledger writer to persist"; nothing persisted them, so model spend was
    invisible per lead and per campaign.
    """
    from titan.db.models import ModelRun, UsageLedger
    from titan.models.recording import record_calls

    calls = [
        {
            "task": "message",
            "provider": "gemini",
            "model_id": "gemini-2.0-flash",
            "input_tokens": 800,
            "output_tokens": 60,
            "latency_ms": 940,
            "cost_usd": 0.00021,
            "used_fallback": False,
            "request_hash": "abc123",
            "occurred_at": dt.datetime.now(dt.UTC),
        }
    ]

    async with workspace_unit_of_work(workspace) as session:
        written = await record_calls(session, workspace_id=workspace, calls=calls)

    assert written == 1
    # Drained, so a gateway reused across drafts cannot re-record the first
    # draft's calls against the second.
    assert calls == []

    async with get_sessionmaker()() as s:
        run = (await s.execute(select(ModelRun))).scalars().one()
        assert run.provider == "gemini"
        assert run.cost_usd == pytest.approx(0.00021)
        # The prompt carries the prospect's page text and the response carries a
        # sentence about their business; neither is needed to answer what this
        # table exists to answer.
        assert run.request_snapshot is None
        assert run.response_snapshot is None

        entry = (await s.execute(select(UsageLedger))).scalars().one()
        assert entry.category == "model"
        assert entry.cost_estimated is True


async def test_a_retried_model_call_is_not_billed_twice(db_session, workspace) -> None:
    """A ledger that double-counts a retry reports spend nobody was charged."""
    from titan.db.models import UsageLedger
    from titan.models.recording import record_calls

    def call() -> dict:
        return {
            "task": "message",
            "provider": "gemini",
            "model_id": "gemini-2.0-flash",
            "cost_usd": 0.0005,
            "request_hash": "same-request",
            "lead_id": None,
            "campaign_id": None,
            "occurred_at": dt.datetime.now(dt.UTC),
        }

    async with workspace_unit_of_work(workspace) as session:
        first = await record_calls(session, workspace_id=workspace, calls=[call()])
    async with workspace_unit_of_work(workspace) as session:
        second = await record_calls(session, workspace_id=workspace, calls=[call()])

    assert first == 1
    assert second == 0

    async with get_sessionmaker()() as s:
        entries = (await s.execute(select(UsageLedger))).scalars().all()
        total = sum(e.cost_usd for e in entries)
    assert len(entries) == 1
    assert total == pytest.approx(0.0005)
