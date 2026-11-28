"""Alembic environment configuration for Dalton (independent migration chain).

Builds the database URL from DB_* environment variables (same as upstream)
and tracks revisions in `alembic_version_dalton` so Dalton migrations never
collide with upstream linear revisions.
"""

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from src.dalton.pg_config import resolve_pg_dsn

# Load .env from project root
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

config = context.config

_PLACEHOLDER = "driver://user:pass@localhost/dbname"
current_url = config.get_main_option("sqlalchemy.url")
if not current_url or current_url == _PLACEHOLDER:
    raw_dsn = resolve_pg_dsn(default_host="localhost", default_sslmode=os.getenv("DB_SSLMODE", "prefer"))
    if raw_dsn.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + raw_dsn[len("postgresql://") :]
    elif raw_dsn.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + raw_dsn[len("postgres://") :]
    else:
        database_url = raw_dsn
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None
VERSION_TABLE = "alembic_version_dalton"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generates SQL script)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (applies directly to database)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table=VERSION_TABLE,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
