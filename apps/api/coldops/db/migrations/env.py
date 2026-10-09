"""Alembic environment.

The DSN comes from coldops.config (COLDOPS_DATABASE_URL) so that migrations,
the API, and the workers can never disagree about which database they target.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from coldops.config import get_settings
from coldops.db.models import Base  # registers every table on Base.metadata
from coldops.runtime import configure_event_loop

configure_event_loop()

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", get_settings().database_url)


#: Tables created by migrations and used through SQL, with no ORM model.
#:
#: Without this list ``alembic check`` proposes dropping all of them, so it
#: has failed on every run since the first one was added -- and a check that
#: always fails is a check nobody reads, which is how it stopped guarding
#: anything. Named here, one by one, so a table that is meant to have a model
#: and lacks one still shows up as drift.
SQL_ONLY_TABLES = frozenset(
    {
        "call_outcomes",
        "call_suppressions",
        "engagement_events",
        "events",
        "ml_labels",
        "ml_models",
        "ml_predictions",
        "placement_checks",
        "sending_claims",
    }
)

#: Indexes created by a migration on a modelled table, for a query the ORM
#: does not express.
SQL_ONLY_INDEXES = frozenset({"ix_messages_one_pager"})


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Keep Alembic's attention on ColdOps's own modelled tables.

    Prevents autogenerate from proposing to drop tables owned by extensions,
    left over from the pre-0.2 Prisma schema, or deliberately SQL-only.
    """
    if type_ == "table" and name in {"alembic_version"} | SQL_ONLY_TABLES:
        return False
    if type_ == "index":
        table = getattr(getattr(obj, "table", None), "name", None)
        if table in SQL_ONLY_TABLES or name in SQL_ONLY_INDEXES:
            return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        # One transaction for the whole upgrade: a partially-applied migration
        # is worse than a failed one.
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
