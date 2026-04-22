"""Alembic migrations environment. Reads POSTGRES_DSN from settings.

Note: alembic itself uses sync SQLAlchemy, so we swap the asyncpg driver name
for psycopg2 when constructing the URL.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.config import settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

dsn = settings.POSTGRES_DSN.replace("+asyncpg", "+psycopg2")
config.set_main_option("sqlalchemy.url", dsn)

target_metadata = None  # raw-SQL migrations — no ORM metadata


def run_migrations_offline() -> None:
    context.configure(
        url=dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
