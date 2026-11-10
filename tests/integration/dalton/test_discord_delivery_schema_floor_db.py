"""Initialize current I-6 on a database that has not already been stamped d036.

The shared test database may already be at d036. These tests create their own
databases, so that stamp cannot hide a helper that stops at d031 or d035.
An empty database and a database explicitly upgraded only to d035 must both
gain ``delivery_attempt``. A database already at d036 is left there. A later
revision is also left in place by the same descendant walk. Deliberate
downgrades to d031 stay in the AU1 tests.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import psycopg
from psycopg.rows import dict_row
import pytest

from tests.integration.dalton.test_discord_gateway_db import (
    _ensure_alembic_upgraded_to_d036,
    _get_test_dsn,
    _revision_is_target_or_descendant,
)

pytestmark = pytest.mark.integration

EMPTY_DB = f"langalpha_channel_d036_empty_{uuid4().hex[:12]}_test"
FROM_D035_DB = f"langalpha_channel_d036_from_d035_{uuid4().hex[:12]}_test"


@pytest.fixture(scope="session", autouse=True)
def dalton_schema():
    """Do not upgrade the shared test database for this file."""
    yield


@pytest.fixture(autouse=True)
def cleanup_dalton_tables():
    """Do not open the shared test pool for this file."""
    yield


def _with_database(dsn: str, name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{name}"))


def _dalton_script() -> ScriptDirectory:
    project_root = Path(__file__).resolve().parents[3]
    cfg = Config(str(project_root / "src" / "dalton" / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(project_root / "src" / "dalton" / "migrations")
    )
    return ScriptDirectory.from_config(cfg)


def _is_d036_or_descendant(version: str) -> bool:
    return _revision_is_target_or_descendant(_dalton_script(), version, "d036")


async def _upgrade(dsn: str, *, chain: str, revision: str) -> None:
    """Upgrade one chain to an explicit revision. This is not the d036 helper."""
    project_root = Path(__file__).resolve().parents[3]
    if chain == "upstream":
        cfg = Config(str(project_root / "alembic.ini"))
        cfg.set_main_option("script_location", str(project_root / "migrations"))
    else:
        cfg = Config(str(project_root / "src" / "dalton" / "alembic.ini"))
        cfg.set_main_option(
            "script_location", str(project_root / "src" / "dalton" / "migrations")
        )
    sa_url = dsn.replace("postgresql://", "postgresql+psycopg://", 1).replace("%", "%%")
    cfg.set_main_option("sqlalchemy.url", sa_url)
    await asyncio.to_thread(command.upgrade, cfg, revision)


async def _recreate(admin_dsn: str, name: str, *, create: bool) -> None:
    assert name.endswith("_test")
    async with await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True) as conn:
        terminated = await conn.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s AND pid <> pg_backend_pid()
            """,
            (name,),
        )
        await terminated.fetchall()
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        if create:
            await conn.execute(f'CREATE DATABASE "{name}"')


async def _regclass(dsn: str, name: str) -> str | None:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        cursor = await conn.execute("SELECT to_regclass(%s)", (name,))
        row = await cursor.fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


async def _dalton_state(dsn: str) -> dict[str, Any]:
    async with await psycopg.AsyncConnection.connect(
        dsn, autocommit=True, row_factory=dict_row
    ) as conn:
        versions = await conn.execute(
            "SELECT version_num FROM alembic_version_dalton ORDER BY version_num"
        )
        version_rows = await versions.fetchall()
        column = await conn.execute(
            """
            SELECT data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'dalton_channel_deliveries'
              AND column_name = 'delivery_attempt'
            """
        )
        attempt = await column.fetchone()
        return {
            "versions": [str(row["version_num"]) for row in version_rows],
            "attempt": dict(attempt) if attempt is not None else None,
        }


def _assert_attempt_column(state: dict[str, Any]) -> None:
    assert len(state["versions"]) == 1
    assert _is_d036_or_descendant(state["versions"][0])
    assert state["attempt"] is not None
    assert state["attempt"]["data_type"] == "integer"
    assert state["attempt"]["is_nullable"] == "NO"


def _prepare_migration_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = tmp_path / "agent_config.yaml"
    config.write_text("{}\n")
    monkeypatch.setenv("PTC_CONFIG_FILE", str(config))
    os.environ.setdefault(
        "BYOK_ENCRYPTION_KEY", "test-channel-gateway-encryption-key-32b"
    )


@pytest.mark.asyncio(loop_scope="function")
async def test_empty_database_initializes_delivery_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new database gets d036 or a descendant, including delivery_attempt."""
    source = _get_test_dsn()
    admin = _with_database(source, "postgres")
    dsn = _with_database(source, EMPTY_DB)
    _prepare_migration_env(monkeypatch, tmp_path)
    await _recreate(admin, EMPTY_DB, create=True)
    try:
        assert await _regclass(dsn, "public.alembic_version_dalton") is None
        assert await _regclass(dsn, "public.dalton_channel_deliveries") is None
        await _ensure_alembic_upgraded_to_d036(dsn)
        first = await _dalton_state(dsn)
        _assert_attempt_column(first)
        await _ensure_alembic_upgraded_to_d036(dsn)
        second = await _dalton_state(dsn)
        assert second["versions"] == first["versions"]
        _assert_attempt_column(second)
    finally:
        await _recreate(admin, EMPTY_DB, create=False)


@pytest.mark.asyncio(loop_scope="function")
async def test_d035_database_is_upgraded_to_delivery_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database explicitly left at d035 is not treated as current."""
    source = _get_test_dsn()
    admin = _with_database(source, "postgres")
    dsn = _with_database(source, FROM_D035_DB)
    _prepare_migration_env(monkeypatch, tmp_path)
    await _recreate(admin, FROM_D035_DB, create=True)
    try:
        await _upgrade(dsn, chain="upstream", revision="head")
        await _upgrade(dsn, chain="dalton", revision="d035")
        before = await _dalton_state(dsn)
        assert before["versions"] == ["d035"]
        assert before["attempt"] is None
        await _ensure_alembic_upgraded_to_d036(dsn)
        after = await _dalton_state(dsn)
        assert after["versions"] != ["d035"]
        _assert_attempt_column(after)
    finally:
        await _recreate(admin, FROM_D035_DB, create=False)
