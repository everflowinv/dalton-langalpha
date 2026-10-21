"""Dalton integration test fixtures (applies Dalton Alembic migrations on top of upstream test DB)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

_DALTON_TABLES = [
    "dalton_budget_reservations",
    "dalton_budget_task_caps",
    "dalton_budget_config",
    "dalton_llm_journal",
]


@pytest.fixture(scope="session", autouse=True)
def dalton_schema(test_db_pool, test_db_uri: str):
    """Upgrade Dalton Alembic chain (`src/dalton/alembic.ini`) to head on the test database."""
    db_name = urlsplit(test_db_uri).path.lstrip("/")
    assert db_name.endswith("_test"), (
        f"Refusing to run Dalton integration migrations against non-test database: {db_name!r}"
    )

    project_root = Path(__file__).resolve().parents[3]
    alembic_cfg = Config(str(project_root / "src" / "dalton" / "alembic.ini"))
    alembic_cfg.set_main_option(
        "script_location", str(project_root / "src" / "dalton" / "migrations")
    )
    sa_url = test_db_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    alembic_cfg.set_main_option("sqlalchemy.url", sa_url.replace("%", "%%"))

    command.upgrade(alembic_cfg, "head")
    yield


@pytest_asyncio.fixture(autouse=True)
async def cleanup_dalton_tables(dalton_schema, test_db_uri: str):
    """Truncate Dalton budget tables before and after each test, on a connection of its own."""
    import psycopg

    async def _truncate() -> None:
        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
            for table in _DALTON_TABLES:
                await conn.execute(f"TRUNCATE TABLE {table} CASCADE")

    await _truncate()
    yield
    await _truncate()
