"""PostgreSQL storage tests for automation report registration.

The DSN must name a database ending in ``_test``. Nothing here is sent to a
gateway, and the default localhost database is ``langalpha_test``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
from urllib.parse import urlsplit
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio

from src.dalton import access_tiers
from src.dalton.database import automation_reports as reports
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import data_task_notifications as dtn
from src.dalton.notify import dispatch as notify_dispatch
from src.dalton.notify.channel_transport import post_data_task_gateway_notify
from src.server.database import automation_executions as execution_db
from src.server.database import pool as db_pool
from src.server.database import user as user_db

pytestmark = pytest.mark.integration

_DSN_ENV_VARS = (
    "DALTON_CHANNEL_GATEWAY_TEST_DSN",
    "DALTON_TEST_DB_DSN",
    "TEST_DATABASE_URL",
)


def _get_test_dsn() -> str:
    for var in _DSN_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            db_name = urlsplit(val).path.lstrip("/")
            if not db_name.endswith("_test"):
                pytest.fail(
                    f"Refusing to run integration tests against non-test DB {db_name!r} (from {var})"
                )
            return val

    host = os.getenv("TEST_DB_HOST", "localhost")
    port = os.getenv("TEST_DB_PORT", "5432")
    name = (os.getenv("TEST_DB_NAME", "langalpha_test") or "").strip().lstrip("/")
    if not name.endswith("_test"):
        pytest.fail(
            f"Refusing to run integration tests against non-test DB {name!r}"
        )
    user = os.getenv("TEST_DB_USER", "postgres")
    password = os.getenv("TEST_DB_PASSWORD", "postgres")
    sslmode = os.getenv("TEST_DB_SSLMODE", "prefer")
    fallback_dsn = (
        f"postgresql://{user}:{password}@{host}:{port}/{name}?sslmode={sslmode}"
    )
    try:
        with psycopg.connect(fallback_dsn, connect_timeout=2):
            return fallback_dsn
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"None of {_DSN_ENV_VARS} is set and default test DB is unreachable ({exc})"
        )


def test_dsn_guard_rejects_a_database_that_is_not_suffixed_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in _DSN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(
        "DALTON_TEST_DB_DSN",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )
    try:
        _get_test_dsn()
    except BaseException as exc:
        assert "Refusing to run integration tests" in str(exc)
    else:
        raise AssertionError("DSN guard did not refuse a production database name")


def _run_alembic_upgrade_sync(cfg: Config, revision: str) -> None:
    command.upgrade(cfg, revision)


async def _ensure_alembic_upgraded(dsn: str) -> None:
    project_root = Path(__file__).resolve().parents[3]
    sa_url = dsn.replace("postgresql://", "postgresql+psycopg://", 1).replace("%", "%%")

    upstream_cfg = Config(str(project_root / "alembic.ini"))
    upstream_cfg.set_main_option("script_location", str(project_root / "migrations"))
    upstream_cfg.set_main_option("sqlalchemy.url", sa_url)
    upstream_heads = set(ScriptDirectory.from_config(upstream_cfg).get_heads())

    dalton_cfg = Config(str(project_root / "src" / "dalton" / "alembic.ini"))
    dalton_cfg.set_main_option(
        "script_location", str(project_root / "src" / "dalton" / "migrations")
    )
    dalton_cfg.set_main_option("sqlalchemy.url", sa_url)
    dalton_heads = set(ScriptDirectory.from_config(dalton_cfg).get_heads())

    current_up: set[str] = set()
    current_dalton: set[str] = set()
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT to_regclass('public.alembic_version')")
            upstream_table = await cur.fetchone()
            if upstream_table and upstream_table[0] is not None:
                await cur.execute("SELECT version_num FROM alembic_version")
                current_up = {str(row[0]) for row in await cur.fetchall()}
            await cur.execute("SELECT to_regclass('public.alembic_version_dalton')")
            dalton_table = await cur.fetchone()
            if dalton_table and dalton_table[0] is not None:
                await cur.execute("SELECT version_num FROM alembic_version_dalton")
                current_dalton = {str(row[0]) for row in await cur.fetchall()}

    if current_up != upstream_heads:
        await asyncio.to_thread(_run_alembic_upgrade_sync, upstream_cfg, "heads")
    if not dalton_heads.issubset(current_dalton):
        await asyncio.to_thread(_run_alembic_upgrade_sync, dalton_cfg, "heads")


@pytest_asyncio.fixture
async def report_db_pool() -> AsyncConnectionPool:
    dsn = _get_test_dsn()
    await _ensure_alembic_upgraded(dsn)
    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=2,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


@dataclass
class AuthState:
    enabled: bool = True
    admin: bool = True
    personal: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    overrides: dict[str, bool] = field(default_factory=dict)


@dataclass
class World:
    pool: AsyncConnectionPool
    prefix: str
    owner: str
    colleague: str
    workspace_id: str
    automation_id: str
    execution_id: str
    thread_id: str
    run_id: str
    chat_a: str
    chat_b: str
    address_a: str
    address_b: str
    discord_target: str
    discord_id: str
    auth: AuthState
    execution_ids: list[str]
    resolve_enabled: Any
    resolve_overrides: Any
    resolve_targets: Any
    resolve_routes: Any
    is_admin: Any


def _card(content: str = "See [10-K](https://example.com/msft/10k)") -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "Digest"}},
        "body": {
            "elements": [
                {"tag": "markdown", "content": content},
            ]
        },
    }


_UNSET_TEXT_TARGETS = object()


def _profile(
    chat_a: str,
    chat_b: str,
    discord_target: str,
    *,
    report_targets: list[str] | None = None,
    failure_targets: list[str] | None = None,
    text_targets: Any = _UNSET_TEXT_TARGETS,
    profile: str = "market-digest-am",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "profile": profile,
        "report_targets": [chat_a, chat_b] if report_targets is None else report_targets,
        "failure_targets": [discord_target] if failure_targets is None else failure_targets,
    }
    if text_targets is not _UNSET_TEXT_TARGETS:
        body["text_targets"] = text_targets
    return {"ticker": "MSFT", "report_profile": body}


def _bind_auth(state: AuthState, automation_id: str) -> dict[str, Any]:
    async def resolve_enabled(user_id: str | None = None) -> bool:
        return state.enabled

    async def resolve_overrides(user_id: str | None = None) -> dict[str, bool]:
        return dict(state.overrides)

    async def resolve_targets(user_id: str | None = None) -> list[str]:
        return list(state.personal)

    async def resolve_routes(user_id: str | None = None) -> dict[str, list[str]]:
        return {f"automation:{automation_id}": list(state.routes)}

    async def is_admin(user_id: str | None = None) -> bool:
        return state.admin

    return {
        "resolve_enabled_fn": resolve_enabled,
        "resolve_overrides_fn": resolve_overrides,
        "resolve_targets_fn": resolve_targets,
        "resolve_routes_fn": resolve_routes,
        "is_admin_fn": is_admin,
    }


async def _seed(pool: AsyncConnectionPool, world: World, metadata: dict[str, Any]) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO users (user_id, email, name, created_at, updated_at)
                VALUES (%s, %s, 'Owner', NOW(), NOW()),
                       (%s, %s, 'Colleague', NOW(), NOW())
                """,
                (
                    world.owner,
                    f"{world.prefix}_owner@example.com",
                    world.colleague,
                    f"{world.prefix}_colleague@example.com",
                ),
            )
            await cur.execute(
                """
                INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                VALUES (%s::uuid, %s, %s, 'stopped', NOW(), NOW())
                """,
                (world.workspace_id, world.owner, world.prefix),
            )
            await cur.execute(
                """
                INSERT INTO dalton_workspace_sharing
                    (workspace_id, cos_access, web_access, updated_by)
                VALUES (%s::uuid, 'rw', 'rw', %s)
                """,
                (world.workspace_id, world.owner),
            )
            await cur.execute(
                """
                INSERT INTO automations (
                    automation_id, user_id, name, trigger_type, instruction,
                    workspace_id, metadata, delivery_config, status
                )
                VALUES (
                    %s::uuid, %s, 'AM digest', 'cron', 'Deliver the digest',
                    %s::uuid, %s::jsonb, '{}'::jsonb, 'active'
                )
                """,
                (world.automation_id, world.owner, world.workspace_id, json.dumps(metadata)),
            )
            await cur.execute(
                """
                INSERT INTO automation_executions (
                    automation_execution_id, automation_id, status,
                    conversation_thread_id, conversation_response_id,
                    scheduled_at, started_at
                )
                VALUES (%s::uuid, %s::uuid, 'running', %s::uuid, %s::uuid, NOW(), NOW())
                """,
                (world.execution_id, world.automation_id, world.thread_id, world.run_id),
            )
            await cur.execute(
                """
                INSERT INTO dalton_channel_chat_bindings
                    (platform, chat_address, chat_name, workspace_id, bound_by_user_id)
                VALUES ('feishu', %s, 'AM', NULL, %s),
                       ('feishu', %s, 'PM', NULL, %s)
                """,
                (world.address_a, world.owner, world.address_b, world.owner),
            )
            apps = {
                "feishu": {
                    "chats": {
                        world.address_a: {"name": "AM"},
                        world.address_b: {"name": "PM"},
                    }
                }
            }
            await cur.execute(
                """
                INSERT INTO dalton_channel_user_settings (user_id, version, apps)
                VALUES (%s, 'v1', %s::jsonb)
                """,
                (world.owner, json.dumps(apps)),
            )
            await cur.execute(
                """
                INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id)
                VALUES (%s, 'discord', %s)
                """,
                (world.owner, world.discord_id),
            )


async def _cleanup(pool: AsyncConnectionPool, world: World) -> None:
    users = [world.owner, world.colleague]
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM dalton_notify_deliveries
                WHERE user_id = ANY(%s) OR execution_id = ANY(%s)
                """,
                (users, world.execution_ids),
            )
            await cur.execute(
                """
                DELETE FROM dalton_channel_deliveries
                WHERE user_id = ANY(%s)
                   OR execution_id = ANY(%s::uuid[])
                   OR run_id = ANY(%s)
                """,
                (users, world.execution_ids, world.execution_ids),
            )
            await cur.execute(
                """
                DELETE FROM dalton_data_task_runs
                WHERE run_id = ANY(%s::uuid[])
                   OR job_key LIKE %s
                """,
                (world.execution_ids, f"rptnative_{world.prefix}%"),
            )
            await cur.execute(
                "DELETE FROM dalton_data_task_defs WHERE job_key LIKE %s",
                (f"rptnative_{world.prefix}%",),
            )
            await cur.execute(
                """
                DELETE FROM automation_executions
                WHERE automation_id = %s::uuid
                   OR automation_execution_id = ANY(%s::uuid[])
                """,
                (world.automation_id, world.execution_ids),
            )
            await cur.execute(
                "DELETE FROM automations WHERE automation_id = %s::uuid OR user_id = ANY(%s)",
                (world.automation_id, users),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_identities WHERE user_id = ANY(%s)",
                (users,),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_user_settings WHERE user_id = ANY(%s)",
                (users,),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_chat_bindings WHERE bound_by_user_id = ANY(%s)",
                (users,),
            )
            await cur.execute(
                "DELETE FROM dalton_workspace_sharing WHERE workspace_id = %s::uuid",
                (world.workspace_id,),
            )
            await cur.execute(
                "DELETE FROM workspaces WHERE workspace_id = %s::uuid OR user_id = ANY(%s)",
                (world.workspace_id, users),
            )
            await cur.execute("DELETE FROM users WHERE user_id = ANY(%s)", (users,))


@pytest_asyncio.fixture
async def world(report_db_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch):
    prefix = f"rpt_{uuid4().hex[:8]}"
    automation_id = str(uuid4())
    execution_id = str(uuid4())
    chat_a_id = f"oc_{prefix}a"
    chat_b_id = f"oc_{prefix}b"
    discord_id = str(uuid4().int % 10**18).zfill(18)
    auth = AuthState(
        personal=[f"discord:user:{discord_id}"],
        routes=[
            f"feishu:chat:{chat_a_id}",
            f"feishu:chat:{chat_b_id}",
            f"discord:user:{discord_id}",
        ],
    )
    fns = _bind_auth(auth, automation_id)
    built = World(
        pool=report_db_pool,
        prefix=prefix,
        owner=f"{prefix}_owner",
        colleague=f"{prefix}_colleague",
        workspace_id=str(uuid4()),
        automation_id=automation_id,
        execution_id=execution_id,
        thread_id=str(uuid4()),
        run_id=str(uuid4()),
        chat_a=f"feishu:chat:{chat_a_id}",
        chat_b=f"feishu:chat:{chat_b_id}",
        address_a=f"feishu:{chat_a_id}",
        address_b=f"feishu:{chat_b_id}",
        discord_target=f"discord:user:{discord_id}",
        discord_id=discord_id,
        auth=auth,
        execution_ids=[execution_id],
        resolve_enabled=fns["resolve_enabled_fn"],
        resolve_overrides=fns["resolve_overrides_fn"],
        resolve_targets=fns["resolve_targets_fn"],
        resolve_routes=fns["resolve_routes_fn"],
        is_admin=fns["is_admin_fn"],
    )

    @asynccontextmanager
    async def get_db_connection(conn=None):
        if conn is not None:
            yield conn
            return
        async with report_db_pool.connection() as borrowed:
            yield borrowed

    monkeypatch.setattr(db_pool, "get_db_connection", get_db_connection)
    monkeypatch.setattr(reports, "get_db_connection", get_db_connection)
    monkeypatch.setattr(
        cg_db,
        "get_user_tier",
        create_autospec(access_tiers.get_user_tier, return_value="admin"),
    )
    try:
        await _seed(
            report_db_pool,
            built,
            _profile(built.chat_a, built.chat_b, built.discord_target),
        )
        yield built
    finally:
        await _cleanup(report_db_pool, built)


def _submit(world: World, **overrides: Any):
    args: dict[str, Any] = {
        "user_id": world.owner,
        "workspace_id": world.workspace_id,
        "thread_id": world.thread_id,
        "run_id": world.run_id,
        "automation_id": world.automation_id,
        "execution_id": world.execution_id,
        "report_run_id": "digest-2026-11-10",
        "material": "yes",
        "cards": [_card()],
        "failure_text": None,
        "resolve_enabled_fn": world.resolve_enabled,
        "resolve_overrides_fn": world.resolve_overrides,
        "resolve_targets_fn": world.resolve_targets,
        "resolve_routes_fn": world.resolve_routes,
        "is_admin_fn": world.is_admin,
    }
    args.update(overrides)
    return reports.submit_automation_report(**args)


def _status(world: World, **overrides: Any):
    args: dict[str, Any] = {
        "user_id": world.owner,
        "workspace_id": world.workspace_id,
        "thread_id": world.thread_id,
        "run_id": world.run_id,
        "automation_id": world.automation_id,
        "execution_id": world.execution_id,
        "report_run_id": "digest-2026-11-10",
    }
    args.update(overrides)
    return reports.read_automation_report_status(**args)


async def _rows(world: World) -> list[dict[str, Any]]:
    async with world.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT execution_id, automation_id, user_id, channel, target,
                       status, attempt_count, error_code, payload_json
                FROM dalton_notify_deliveries
                WHERE user_id = %s
                ORDER BY target ASC, execution_id ASC
                """,
                (world.owner,),
            )
            fetched = await cur.fetchall()
    return [dict(row) for row in fetched or []]


async def _set_metadata(world: World, metadata: dict[str, Any]) -> None:
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE automations SET metadata = %s::jsonb WHERE automation_id = %s::uuid",
                (json.dumps(metadata), world.automation_id),
            )


async def _set_delivery_config(world: World, config: dict[str, Any]) -> None:
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE automations
                SET delivery_config = %s::jsonb
                WHERE automation_id = %s::uuid
                """,
                (json.dumps(config), world.automation_id),
            )


async def _add_execution(
    world: World,
    *,
    status: str = "running",
    thread_id: str | None = None,
    run_id: str | None = None,
) -> tuple[str, str, str]:
    execution_id = str(uuid4())
    thread = thread_id or world.thread_id
    run = run_id or str(uuid4())
    world.execution_ids.append(execution_id)
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO automation_executions (
                    automation_execution_id, automation_id, status,
                    conversation_thread_id, conversation_response_id,
                    scheduled_at, started_at
                )
                VALUES (%s::uuid, %s::uuid, %s, %s::uuid, %s::uuid, NOW(), NOW())
                """,
                (execution_id, world.automation_id, status, thread, run),
            )
    return execution_id, thread, run


@pytest.mark.asyncio
async def test_register_stores_every_target_and_the_original_link(world: World) -> None:
    result = await _submit(world)
    assert result["ok"] is True
    assert result["created"] is True
    assert result["quiet"] is False
    assert result["delivery_confirmed"] is False
    assert result["status"] == "pending"
    assert result["execution_id"] == world.execution_id
    assert result["targets"] == [world.chat_a, world.chat_b]
    assert result["payload"]["source"] == "automation_report"
    assert result["payload"]["cards"][0]["body"]["elements"][0]["content"] == (
        "See [10-K](https://example.com/msft/10k)"
    )

    rows = await _rows(world)
    assert len(rows) == 2
    assert {row["target"] for row in rows} == {world.chat_a, world.chat_b}
    for row in rows:
        assert row["execution_id"] == world.execution_id
        assert row["automation_id"] == world.automation_id
        assert row["status"] == "pending"
        assert row["attempt_count"] == 0
        assert row["error_code"] is None
        assert row["channel"] == "feishu"
        payload = row["payload_json"]
        assert payload["source"] == "automation_report"
        assert payload["content_sha256"] == result["content_sha256"]
        assert payload["target_snapshot"] == [world.chat_a, world.chat_b]
        assert payload["first_execution_id"] == world.execution_id
        assert payload["expected_item_count"] == 1
        assert payload["cards"][0]["body"]["elements"][0]["content"].startswith("See [10-K]")
        assert "route_key" not in payload

    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM dalton_data_task_runs WHERE run_id = %s::uuid",
                (world.execution_id,),
            )
            count = await cur.fetchone()
    assert int(count[0] if not isinstance(count, dict) else count["count"]) == 0


@pytest.mark.asyncio
async def test_concurrent_same_report_registers_once(world: World) -> None:
    first, second = await asyncio.gather(_submit(world), _submit(world))
    assert first["ok"] and second["ok"]
    assert sorted([first["created"], second["created"]]) == [False, True]
    assert first["content_sha256"] == second["content_sha256"]
    assert first["execution_id"] == second["execution_id"] == world.execution_id
    assert len(await _rows(world)) == 2


@pytest.mark.asyncio
async def test_later_execution_returns_the_first_immutable_batch(world: World) -> None:
    saved = await _submit(world)
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE automation_executions
                SET status = 'completed', completed_at = NOW()
                WHERE automation_execution_id = %s::uuid
                """,
                (world.execution_id,),
            )
    later_id, later_thread, later_run = await _add_execution(world, thread_id=world.thread_id)
    replay = await _submit(
        world,
        execution_id=later_id,
        thread_id=later_thread,
        run_id=later_run,
    )
    assert replay["ok"] is True
    assert replay["created"] is False
    assert replay["execution_id"] == world.execution_id
    assert replay["targets"] == saved["targets"]
    assert replay["content_sha256"] == saved["content_sha256"]
    assert replay["payload"]["first_execution_id"] == world.execution_id
    rows = await _rows(world)
    assert len(rows) == 2
    assert {row["execution_id"] for row in rows} == {world.execution_id}

    changed = await _submit(
        world,
        execution_id=later_id,
        thread_id=later_thread,
        run_id=later_run,
        cards=[_card("A different digest")],
    )
    assert changed["ok"] is False
    assert changed["code"] == "content_conflict"
    assert changed["delivery_confirmed"] is False
    reread = await _rows(world)
    assert len(reread) == 2
    assert reread[0]["payload_json"]["cards"][0]["body"]["elements"][0]["content"].startswith(
        "See [10-K]"
    )

    await _set_metadata(
        world,
        _profile(
            world.chat_a,
            world.chat_b,
            world.discord_target,
            report_targets=[world.chat_a],
        ),
    )
    narrowed = await _submit(
        world,
        execution_id=later_id,
        thread_id=later_thread,
        run_id=later_run,
    )
    assert narrowed["created"] is False
    assert narrowed["targets"] == [world.chat_a, world.chat_b]
    assert len(await _rows(world)) == 2


@pytest.mark.asyncio
async def test_second_batch_on_the_same_execution_is_rejected(world: World) -> None:
    assert (await _submit(world))["created"] is True
    other = await _submit(world, report_run_id="digest-other", cards=[_card("Other")])
    assert other["ok"] is False
    assert other["code"] == "execution_batch_conflict"
    rows = await _rows(world)
    assert len(rows) == 2
    assert {row["payload_json"]["report_run_id"] for row in rows} == {"digest-2026-11-10"}


@pytest.mark.asyncio
async def test_identity_mismatches_store_nothing(world: World) -> None:
    cases = [
        {"user_id": world.colleague, "code": "owner_mismatch"},
        {"workspace_id": str(uuid4()), "code": "workspace_mismatch"},
        {"run_id": str(uuid4()), "code": "run_mismatch"},
        {"thread_id": str(uuid4()), "code": "thread_mismatch"},
        {"automation_id": str(uuid4()), "code": "not_automation_run"},
        {"automation_id": "", "code": "not_automation_run"},
        {"execution_id": "not-a-uuid", "code": "invalid_execution"},
        {"execution_id": str(uuid4()), "code": "invalid_execution"},
    ]
    for overrides in cases:
        code = overrides.pop("code")
        result = await _submit(world, **overrides)
        assert result["ok"] is False
        assert result["code"] == code
        assert result["delivery_confirmed"] is False
    assert await _rows(world) == []

    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE automation_executions
                SET status = 'completed', completed_at = NOW()
                WHERE automation_execution_id = %s::uuid
                """,
                (world.execution_id,),
            )
    settled = await _submit(world)
    assert settled["code"] == "execution_not_running"
    assert await _rows(world) == []


@pytest.mark.asyncio
async def test_missing_binding_or_partial_route_stores_no_rows(world: World) -> None:
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM dalton_channel_chat_bindings WHERE chat_address = %s",
                (world.address_b,),
            )
    revoked = await _submit(world)
    assert revoked["ok"] is False
    assert revoked["code"] == "target_revoked"
    assert await _rows(world) == []

    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_channel_chat_bindings
                    (platform, chat_address, chat_name, workspace_id, bound_by_user_id)
                VALUES ('feishu', %s, 'PM', NULL, %s)
                """,
                (world.address_b, world.owner),
            )
    world.auth.routes = [world.chat_a, world.discord_target]
    partial = await _submit(world)
    assert partial["code"] == "no_authorized_target"
    assert await _rows(world) == []

    world.auth.routes = [world.chat_a, world.chat_b, world.discord_target]
    world.auth.enabled = False
    assert (await _submit(world))["code"] == "toggle_off"
    assert await _rows(world) == []


@pytest.mark.asyncio
async def test_quiet_held_and_failure_text_follow_the_profile_route(world: World) -> None:
    quiet = await _submit(world, material="no", cards=None)
    assert quiet == {
        "ok": True,
        "action": "submit",
        "quiet": True,
        "report_run_id": "digest-2026-11-10",
        "delivery_confirmed": False,
        "created": False,
    }
    noisy = await _submit(world, material="no_change", cards=[_card()])
    assert noisy["code"] == "quiet_with_content"
    dashed = await _submit(world, material="no-change", cards=None, failure_text="  ")
    assert dashed["quiet"] is True
    assert await _rows(world) == []

    await _set_delivery_config(world, {"methods": ["feishu"]})
    held = await _submit(world)
    assert held["code"] == "held_delivery_configured"
    assert await _rows(world) == []
    await _set_delivery_config(world, {})

    await _set_metadata(
        world,
        _profile(
            world.chat_a,
            world.chat_b,
            world.discord_target,
            report_targets=[],
            profile="msft-tracker",
        ),
    )
    cards_on_failure_only = await _submit(world)
    assert cards_on_failure_only["code"] == "profile_invalid"
    failure_without_report_route = await _submit(
        world,
        cards=None,
        failure_text="Tracker failed. Source https://example.com/msft",
    )
    assert failure_without_report_route["code"] == "profile_invalid"
    assert await _rows(world) == []
    await _set_metadata(
        world,
        _profile(
            world.chat_a,
            world.chat_b,
            world.discord_target,
            profile="msft-tracker",
        ),
    )
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_identities
                SET platform_user_id = '99999'
                WHERE user_id = %s AND platform = 'discord'
                """,
                (world.owner,),
            )
    mismatched = await _submit(
        world,
        cards=None,
        failure_text="Tracker failed. Source https://example.com/msft",
    )
    assert mismatched["code"] == "target_revoked"
    assert await _rows(world) == []
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_identities
                SET platform_user_id = %s
                WHERE user_id = %s AND platform = 'discord'
                """,
                (world.discord_id, world.owner),
            )
    failure = await _submit(
        world,
        cards=None,
        failure_text="Tracker failed. Source https://example.com/msft",
    )
    assert failure["created"] is True
    assert failure["delivery_confirmed"] is False
    assert failure["content_kind"] == "failure_text"
    assert failure["targets"] == [world.discord_target]
    rows = await _rows(world)
    assert len(rows) == 1
    assert rows[0]["channel"] == "discord"
    assert rows[0]["attempt_count"] == 0
    assert rows[0]["status"] == "pending"
    assert rows[0]["payload_json"]["failure_text"] == (
        "Tracker failed. Source https://example.com/msft"
    )
    assert "cards" not in rows[0]["payload_json"]


@pytest.mark.asyncio
async def test_status_reads_an_earlier_batch_and_does_not_confirm_uncertainty(
    world: World,
) -> None:
    saved = await _submit(world)
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE automation_executions
                SET status = 'completed', completed_at = NOW()
                WHERE automation_execution_id = %s::uuid
                """,
                (world.execution_id,),
            )
    later_id, later_thread, later_run = await _add_execution(world)
    found = await _status(
        world,
        execution_id=later_id,
        thread_id=later_thread,
        run_id=later_run,
    )
    assert found["found"] is True
    assert "created" not in found
    assert found["execution_id"] == world.execution_id
    assert found["delivery_confirmed"] is False
    assert found["status"] == "pending"
    assert found["content_sha256"] == saved["content_sha256"]
    assert len(await _rows(world)) == 2

    guessed = await _status(
        world,
        execution_id=later_id,
        thread_id=later_thread,
        run_id=world.run_id,
    )
    assert guessed["code"] == "run_mismatch"

    missing = await _status(
        world,
        execution_id=later_id,
        thread_id=later_thread,
        run_id=later_run,
        report_run_id="missing-batch",
    )
    assert missing["found"] is False
    assert missing["delivery_confirmed"] is False

    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_notify_deliveries
                SET status = 'pending', error_code = 'unknown'
                WHERE user_id = %s AND target = %s
                """,
                (world.owner, world.chat_a),
            )
    unknown = await _status(
        world,
        execution_id=later_id,
        thread_id=later_thread,
        run_id=later_run,
    )
    assert unknown["status"] == "unknown"
    assert unknown["delivery_confirmed"] is False

    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_notify_deliveries
                SET status = 'failed', error_code = 'http_502'
                WHERE user_id = %s
                """,
                (world.owner,),
            )
    failed = await _status(
        world,
        execution_id=later_id,
        thread_id=later_thread,
        run_id=later_run,
    )
    assert failed["status"] == "failed"
    assert failed["delivery_confirmed"] is False

    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_notify_deliveries
                SET status = 'sent', error_code = NULL
                WHERE user_id = %s
                """,
                (world.owner,),
            )
    sent = await _status(
        world,
        execution_id=later_id,
        thread_id=later_thread,
        run_id=later_run,
    )
    assert sent["status"] == "sent"
    assert sent["delivery_confirmed"] is True
    assert len(await _rows(world)) == 2


async def _wait_for_automation_lock_wait(pool: AsyncConnectionPool) -> None:
    """Return once another backend is blocked on the automations row lock."""
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT a.pid
                    FROM pg_stat_activity a
                    WHERE a.datname = current_database()
                      AND a.pid <> pg_backend_pid()
                      AND a.wait_event_type = 'Lock'
                      AND (
                        (
                          a.query ILIKE '%%FROM automations%%'
                          AND (
                            a.query ILIKE '%%FOR UPDATE%%'
                            OR a.query ILIKE '%%FOR SHARE%%'
                          )
                        )
                        OR EXISTS (
                          SELECT 1
                          FROM pg_locks l
                          WHERE l.pid = a.pid
                            AND l.granted = FALSE
                            AND l.relation = 'automations'::regclass
                        )
                      )
                    """
                )
                found = await cur.fetchone()
        if found:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("report lock did not wait on the automation row")


async def _overlap_settle_locks(world: World, report_call: Any) -> Any:
    """Hold settle_execution's locks, then run a report call on another connection.

    The holder takes ``automations`` ``FOR NO KEY UPDATE`` and, only after the
    report backend is waiting, ``automation_executions`` ``FOR UPDATE``. That is
    the order ``settle_execution`` uses. A report lock that takes the execution
    first deadlocks here.
    """
    automation_held = asyncio.Event()
    report_blocked = asyncio.Event()
    execution_locked = asyncio.Event()
    release_settle = asyncio.Event()
    settle_task: asyncio.Task[None] | None = None
    report_task: asyncio.Task[Any] | None = None

    async def settle() -> None:
        async with world.pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT 1
                        FROM automations
                        WHERE automation_id = %s::uuid
                        FOR NO KEY UPDATE
                        """,
                        (world.automation_id,),
                    )
                    if await cur.fetchone() is None:
                        raise AssertionError("settle could not lock the automation")
                    automation_held.set()
                    await report_blocked.wait()
                    await cur.execute(
                        """
                        SELECT status
                        FROM automation_executions
                        WHERE automation_execution_id = %s::uuid
                          AND automation_id = %s::uuid
                        FOR UPDATE
                        """,
                        (world.execution_id, world.automation_id),
                    )
                    if await cur.fetchone() is None:
                        raise AssertionError("settle could not lock the execution")
                    execution_locked.set()
                    await release_settle.wait()

    async def body() -> Any:
        nonlocal settle_task, report_task
        settle_task = asyncio.create_task(settle())
        await automation_held.wait()
        report_task = asyncio.create_task(report_call())
        await _wait_for_automation_lock_wait(world.pool)
        report_blocked.set()
        await execution_locked.wait()
        release_settle.set()
        report_result = await report_task
        await settle_task
        return report_result

    try:
        return await asyncio.wait_for(body(), timeout=15)
    finally:
        release_settle.set()
        report_blocked.set()
        pending = [task for task in (settle_task, report_task) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_submit_and_status_do_not_deadlock_with_settle_execution(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = await _overlap_settle_locks(
        world,
        lambda: _submit(world, report_run_id="lock-order-submit"),
    )
    assert submitted["ok"] is True
    assert submitted["created"] is True
    assert submitted["targets"] == [world.chat_a, world.chat_b]

    found = await _overlap_settle_locks(
        world,
        lambda: _status(world, report_run_id="lock-order-submit"),
    )
    assert found["found"] is True
    assert found["execution_id"] == world.execution_id

    monkeypatch.setattr(execution_db, "get_db_connection", db_pool.get_db_connection)
    settled = await execution_db.settle_execution(
        world.execution_id,
        automation_id=world.automation_id,
        from_statuses=("running",),
        to="completed",
    )
    assert settled is not None
    assert settled["settled_from"] == "running"
    after = await _status(world, report_run_id="lock-order-submit")
    assert after["code"] == "execution_not_running"


def _single_connection_pool(dsn: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=1,
        timeout=2,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )


@pytest.mark.asyncio
async def test_default_authorization_reuses_the_held_connection(
    report_db_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold preference reads and the real tier lookup stay on the submit connection.

    The pool has one slot. A second checkout times out. Injected resolvers are
    not used on the first submit. The group helper must not call ``get_user_tier``
    again after the storage code has already resolved it.
    """
    assert report_db_pool.closed is False
    pool = _single_connection_pool(_get_test_dsn())
    await pool.open()
    prefix = f"rpt_{uuid4().hex[:8]}"
    automation_id = str(uuid4())
    execution_id = str(uuid4())
    chat_a_id = f"oc_{prefix}a"
    chat_b_id = f"oc_{prefix}b"
    discord_id = str(uuid4().int % 10**18).zfill(18)
    owner = f"{prefix}_owner"
    email = f"{owner}@example.com"
    auth = AuthState(
        personal=[f"discord:user:{discord_id}"],
        routes=[
            f"feishu:chat:{chat_a_id}",
            f"feishu:chat:{chat_b_id}",
            f"discord:user:{discord_id}",
        ],
    )
    fns = _bind_auth(auth, automation_id)
    built = World(
        pool=pool,
        prefix=prefix,
        owner=owner,
        colleague=f"{prefix}_colleague",
        workspace_id=str(uuid4()),
        automation_id=automation_id,
        execution_id=execution_id,
        thread_id=str(uuid4()),
        run_id=str(uuid4()),
        chat_a=f"feishu:chat:{chat_a_id}",
        chat_b=f"feishu:chat:{chat_b_id}",
        address_a=f"feishu:{chat_a_id}",
        address_b=f"feishu:{chat_b_id}",
        discord_target=f"discord:user:{discord_id}",
        discord_id=discord_id,
        auth=auth,
        execution_ids=[execution_id],
        resolve_enabled=fns["resolve_enabled_fn"],
        resolve_overrides=fns["resolve_overrides_fn"],
        resolve_targets=fns["resolve_targets_fn"],
        resolve_routes=fns["resolve_routes_fn"],
        is_admin=fns["is_admin_fn"],
    )
    allowed = os.environ.get("DALTON_ALLOWED_EMAILS", "")
    monkeypatch.setenv(
        "DALTON_ALLOWED_EMAILS",
        f"{allowed},{email}" if allowed else email,
    )
    tier_calls = {"n": 0}
    real_tier = cg_db.get_user_tier

    async def counting_tier(user_id: str | None = None) -> str:
        tier_calls["n"] += 1
        return await real_tier(user_id)

    monkeypatch.setattr(cg_db, "get_user_tier", counting_tier)

    @asynccontextmanager
    async def get_db_connection(conn=None):
        if conn is not None:
            yield conn
            return
        async with pool.connection() as borrowed:
            yield borrowed

    monkeypatch.setattr(reports, "get_db_connection", get_db_connection)
    monkeypatch.setattr(user_db, "get_db_connection", get_db_connection)
    try:
        await _seed(
            pool,
            built,
            _profile(built.chat_a, built.chat_b, built.discord_target),
        )
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE users SET email = %s WHERE user_id = %s
                    """,
                    (email, owner),
                )
                await cur.execute(
                    """
                    INSERT INTO user_preferences (user_id, model_preference)
                    VALUES (%s, %s::jsonb)
                    """,
                    (
                        owner,
                        json.dumps(
                            {
                                "dalton_notify_enabled": True,
                                "dalton_notify_targets": [built.discord_target],
                                "dalton_notify_routes": {
                                    f"automation:{automation_id}": list(auth.routes),
                                },
                            }
                        ),
                    ),
                )
        await user_db.invalidate_user_prefs_cache(owner)
        tier_calls["n"] = 0
        default_submit = await reports.submit_automation_report(
            user_id=owner,
            workspace_id=built.workspace_id,
            thread_id=built.thread_id,
            run_id=built.run_id,
            automation_id=automation_id,
            execution_id=execution_id,
            report_run_id="prefs-default",
            material="yes",
            cards=[_card()],
            failure_text=None,
        )
        assert default_submit["ok"] is True
        assert default_submit["created"] is True
        assert default_submit["targets"] == [built.chat_a, built.chat_b]
        assert tier_calls["n"] == 1
        rows = await _rows(built)
        assert len(rows) == 2
        assert all(row["attempt_count"] == 0 and row["status"] == "pending" for row in rows)

        later_id, later_thread, later_run = await _add_execution(built)
        tier_calls["n"] = 0
        injected = await reports.submit_automation_report(
            user_id=owner,
            workspace_id=built.workspace_id,
            thread_id=later_thread,
            run_id=later_run,
            automation_id=automation_id,
            execution_id=later_id,
            report_run_id="prefs-injected",
            material="yes",
            cards=[_card()],
            failure_text=None,
            resolve_enabled_fn=built.resolve_enabled,
            resolve_overrides_fn=built.resolve_overrides,
            resolve_targets_fn=built.resolve_targets,
            resolve_routes_fn=built.resolve_routes,
            is_admin_fn=built.is_admin,
        )
        assert injected["ok"] is True
        assert injected["created"] is True
        assert tier_calls["n"] == 1
        assert len(await _rows(built)) == 4
    finally:
        await _cleanup(pool, built)
        await pool.close()


def _plan_kwargs(world: World) -> dict[str, Any]:
    return {
        "resolve_enabled_fn": world.resolve_enabled,
        "resolve_overrides_fn": world.resolve_overrides,
        "resolve_targets_fn": world.resolve_targets,
        "resolve_routes_fn": world.resolve_routes,
        "is_admin_fn": world.is_admin,
    }


def _patch_native_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route plan, claim, and dispatch through the fixture pool. Notifications stay enabled."""
    monkeypatch.setattr(dtn, "get_db_connection", reports.get_db_connection)
    monkeypatch.setattr(notify_dispatch, "get_db_connection", reports.get_db_connection)
    monkeypatch.setattr(
        dtn,
        "get_dalton_settings",
        create_autospec(
            dtn.get_dalton_settings,
            return_value=SimpleNamespace(enabled=True),
        ),
    )


async def _insert_data_run(
    world: World,
    run_id: str,
    *,
    status: str,
    outbox: dict[str, Any] | None = None,
) -> str:
    job_key = f"rptnative_{world.prefix}_{run_id.replace('-', '')[:12]}"
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_data_task_defs (
                    job_key, task_code, title, batch_no, runner, cron_expr,
                    timezone, command_argv, workdir
                )
                VALUES (%s, 'rptnative', 'native plan stage', 1, 'container',
                        '0 0 * * *', 'UTC', %s::jsonb, '/tmp')
                """,
                (job_key, json.dumps(["true"])),
            )
            await cur.execute(
                """
                INSERT INTO dalton_data_task_runs (
                    run_id, job_key, trigger_source, scheduled_slot_utc,
                    deadline_utc, status, notify_status, outbox_payload
                )
                VALUES (
                    %s::uuid, %s, 'manual', NOW(), NOW() + INTERVAL '1 hour',
                    %s, 'pending_dispatch', %s::jsonb
                )
                """,
                (run_id, job_key, status, json.dumps(outbox or {})),
            )
    return job_key


async def _notify_status(world: World, run_id: str) -> str | None:
    async with world.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT notify_status FROM dalton_data_task_runs WHERE run_id = %s::uuid",
                (run_id,),
            )
            row = await cur.fetchone()
    if not isinstance(row, dict):
        return None
    return str(row.get("notify_status") or "")


async def _channel_rows(world: World, execution_id: str) -> list[dict[str, Any]]:
    async with world.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT delivery_key, kind, run_id, execution_id::text AS execution_id,
                       platform, target_address, status, user_id
                FROM dalton_channel_deliveries
                WHERE run_id = %s OR execution_id = %s::uuid
                ORDER BY delivery_key ASC
                """,
                (execution_id, execution_id),
            )
            fetched = await cur.fetchall()
    return [dict(row) for row in fetched or []]


@pytest.mark.asyncio
async def test_saved_report_plan_and_claim_keep_original_cards(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_db(monkeypatch)
    first = _card("First [10-K](https://example.com/msft/10k)")
    second = _card("Second [10-Q](https://example.com/msft/10q)")
    saved = await _submit(world, cards=[first, second])
    assert saved["ok"] is True
    digest = saved["payload"]["content_sha256"]
    chat_id = world.chat_a.rsplit(":", 1)[-1]

    plan = await dtn.authorize_data_task_notification_plan(
        run_id=world.execution_id,
        target=world.chat_a,
        **_plan_kwargs(world),
    )
    assert plan["authorized"] is True
    assert plan["status"] == "authorized"
    assert plan["run_id"] == world.execution_id
    assert plan["job_key"] == world.automation_id
    assert plan["recipient_id"] == chat_id
    assert plan["canonical_target_address"] == world.address_a
    assert plan["requested_target"] == world.chat_a
    assert plan["target_kind"] == "group"
    assert [item["kind"] for item in plan["items"]] == ["feishu_card", "feishu_card"]
    assert [item["item_index"] for item in plan["items"]] == [0, 1]
    assert [item["card"]["body"]["elements"][0]["content"] for item in plan["items"]] == [
        first["body"]["elements"][0]["content"],
        second["body"]["elements"][0]["content"],
    ]
    assert all(item["images"] == [] and "artifact" not in item for item in plan["items"])

    rows = await _rows(world)
    stamped = next(row for row in rows if row["target"] == world.chat_a)
    assert stamped["payload_json"]["source"] == "automation_report"
    assert stamped["payload_json"]["content_sha256"] == digest
    assert stamped["payload_json"]["cards"] == saved["payload"]["cards"]
    assert stamped["payload_json"]["target_snapshot"] == saved["targets"]
    assert stamped["payload_json"]["reconcile_recipient_id"] == chat_id
    assert stamped["automation_id"] == world.automation_id
    assert not str(stamped["automation_id"]).startswith("data:")

    delivery_key = plan["items"][0]["delivery_key"]
    claimed = await dtn.authorize_and_claim_data_task_delivery(
        delivery_key=delivery_key,
        run_id=world.execution_id,
        target=world.chat_a,
        has_non_empty_text=True,
        total_chunks=0,
        **_plan_kwargs(world),
    )
    assert claimed["claim_status"] == "claimed"
    assert claimed["recipient_id"] == chat_id
    assert claimed["target_address"] == world.address_a
    ledger = await _channel_rows(world, world.execution_id)
    assert len(ledger) == 1
    assert ledger[0]["kind"] == "data_task"
    assert ledger[0]["execution_id"] == world.execution_id
    assert ledger[0]["run_id"] == world.execution_id
    assert ledger[0]["status"] == "in_flight"

    again = await dtn.authorize_and_claim_data_task_delivery(
        delivery_key=delivery_key,
        run_id=world.execution_id,
        target=world.chat_a,
        **_plan_kwargs(world),
    )
    assert again["claim_status"] == "in_flight_or_unknown"
    assert len(await _channel_rows(world, world.execution_id)) == 1
    still = next(row for row in await _rows(world) if row["target"] == world.chat_a)
    assert still["payload_json"]["content_sha256"] == digest
    assert still["payload_json"]["cards"] == saved["payload"]["cards"]


@pytest.mark.asyncio
async def test_saved_report_plan_denies_wrong_body_identity_and_removed_target(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_db(monkeypatch)
    saved = await _submit(world)
    assert saved["ok"] is True
    missing = await dtn.authorize_data_task_notification_plan(
        run_id=world.execution_id,
        target="feishu:chat:oc_not_in_this_batch",
        **_plan_kwargs(world),
    )
    assert missing["authorized"] is False
    assert missing["code"] == "target_not_routed"
    assert missing["status"] == "skipped"

    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_notify_deliveries
                SET payload_json = jsonb_set(payload_json, '{expected_item_count}', '5'::jsonb)
                WHERE execution_id = %s AND target = %s
                """,
                (world.execution_id, world.chat_a),
            )
    invalid = await dtn.authorize_data_task_notification_plan(
        run_id=world.execution_id,
        target=world.chat_a,
        **_plan_kwargs(world),
    )
    assert invalid["authorized"] is False
    assert invalid["code"] == "payload_invalid"

    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_notify_deliveries
                SET payload_json = payload_json
                    || jsonb_build_object(
                        'expected_item_count', 1,
                        'reconcile_recipient_id', 'oc_wrong_recipient',
                        'reconcile_platform', 'feishu',
                        'reconcile_canonical_target', 'feishu:oc_wrong_recipient'
                    )
                WHERE execution_id = %s AND target = %s
                """,
                (world.execution_id, world.chat_b),
            )
    conflict = await dtn.authorize_data_task_notification_plan(
        run_id=world.execution_id,
        target=world.chat_b,
        **_plan_kwargs(world),
    )
    assert conflict["authorized"] is False
    assert conflict["code"] == "recipient_identity_conflict"

    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_notify_deliveries
                SET payload_json = jsonb_set(payload_json, '{expected_item_count}', '1'::jsonb)
                WHERE execution_id = %s AND target = %s
                """,
                (world.execution_id, world.chat_a),
            )
    await _set_metadata(
        world,
        _profile(
            world.chat_a,
            world.chat_b,
            world.discord_target,
            report_targets=[world.chat_a],
        ),
    )
    removed = await dtn.authorize_data_task_notification_plan(
        run_id=world.execution_id,
        target=world.chat_b,
        **_plan_kwargs(world),
    )
    assert removed["authorized"] is False
    assert removed["code"] == "target_removed"
    kept = await dtn.authorize_data_task_notification_plan(
        run_id=world.execution_id,
        target=world.chat_a,
        **_plan_kwargs(world),
    )
    assert kept["authorized"] is True
    assert kept["items"][0]["kind"] == "feishu_card"
    assert kept["recipient_id"] == world.chat_a.rsplit(":", 1)[-1]


@pytest.mark.asyncio
async def test_report_plan_does_not_use_a_colliding_data_run_and_data_path_remains(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_db(monkeypatch)
    saved = await _submit(world, cards=[_card("Original body")])
    assert saved["ok"] is True
    data_job = await _insert_data_run(
        world,
        world.execution_id,
        status="succeeded",
        outbox={"text": "decoy-not-a-card"},
    )
    plan = await dtn.authorize_data_task_notification_plan(
        run_id=world.execution_id,
        target=world.chat_a,
        **_plan_kwargs(world),
    )
    assert plan["authorized"] is True
    assert plan["job_key"] == world.automation_id
    assert plan["job_key"] != data_job
    assert plan["items"][0]["kind"] == "feishu_card"
    assert "decoy-not-a-card" not in json.dumps(plan["items"])
    assert await _notify_status(world, world.execution_id) == "pending_dispatch"

    other_run = str(uuid4())
    world.execution_ids.append(other_run)
    await _insert_data_run(world, other_run, status="queued", outbox={"text": "queued"})
    queued = await dtn.authorize_data_task_notification_plan(
        run_id=other_run,
        target="feishu:@me",
        **_plan_kwargs(world),
    )
    assert queued["authorized"] is False
    assert queued["code"] == "run_not_succeeded"

    absent = str(uuid4())
    world.execution_ids.append(absent)
    missing = await dtn.authorize_data_task_notification_plan(
        run_id=absent,
        target=world.chat_a,
        **_plan_kwargs(world),
    )
    assert missing["authorized"] is False
    assert missing["code"] == "run_not_found"
    assert await _channel_rows(world, absent) == []


@pytest.mark.asyncio
async def test_failure_text_plan_claims_the_saved_discord_recipient(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_db(monkeypatch)
    execution_id, thread_id, run_id = await _add_execution(world)
    text = "The digest failed closed."
    saved = await _submit(
        world,
        execution_id=execution_id,
        thread_id=thread_id,
        run_id=run_id,
        report_run_id="fail-2026-11-10",
        cards=None,
        failure_text=text,
    )
    assert saved["ok"] is True
    assert saved["targets"] == [world.discord_target]
    plan = await dtn.authorize_data_task_notification_plan(
        run_id=execution_id,
        target=world.discord_target,
        **_plan_kwargs(world),
    )
    assert plan["authorized"] is True
    assert plan["run_id"] == execution_id
    assert plan["job_key"] == world.automation_id
    assert plan["platform"] == "discord"
    assert plan["target_kind"] == "dm"
    assert plan["canonical_target_address"] == "discord:@me"
    assert plan["recipient_id"] == world.discord_id
    assert plan["owner_platform_user_id"] == world.discord_id
    assert len(plan["items"]) == 1
    assert plan["items"][0]["kind"] == "text"
    assert plan["items"][0]["text"] == text
    assert "artifact" not in plan["items"][0]
    claimed = await dtn.authorize_and_claim_data_task_delivery(
        delivery_key=plan["items"][0]["delivery_key"],
        run_id=execution_id,
        target=world.discord_target,
        has_non_empty_text=True,
        total_chunks=0,
        **_plan_kwargs(world),
    )
    assert claimed["claim_status"] == "claimed"
    assert claimed["recipient_id"] == world.discord_id
    ledger = await _channel_rows(world, execution_id)
    assert ledger[0]["kind"] == "data_task"
    assert ledger[0]["target_address"] == "discord:@me"

    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_identities
                SET platform_user_id = '999999999999999999'
                WHERE user_id = %s AND platform = 'discord'
                """,
                (world.owner,),
            )
    rebound = await dtn.authorize_data_task_notification_plan(
        run_id=execution_id,
        target=world.discord_target,
        **_plan_kwargs(world),
    )
    assert rebound["authorized"] is False
    assert rebound["code"] == "target_revoked"
    stored = next(row for row in await _rows(world) if row["execution_id"] == execution_id)
    assert stored["payload_json"]["failure_text"] == text
    assert stored["payload_json"]["reconcile_recipient_id"] == world.discord_id


def _dispatch(world: World, notify: Any, execution_id: str | None = None):
    return notify_dispatch.dispatch_automation_report(
        execution_id or world.execution_id,
        notify_fn=notify,
        **_plan_kwargs(world),
    )


async def _write_sent_ledger(
    world: World,
    execution_id: str,
    target: str,
    *,
    item_limit: int | None = None,
) -> list[str]:
    rows = await reports.load_execution_report_rows(execution_id)
    row = next(item for item in rows if item["target"] == target)
    payload = row["payload_json"]
    keys = [str(key) for key in payload["reconcile_delivery_keys"]]
    if item_limit is not None:
        keys = keys[:item_limit]
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            for key in keys:
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_deliveries (
                        delivery_key, kind, user_id, thread_id, run_id, execution_id,
                        is_current, has_non_empty_text, platform, target_address,
                        status, total_chunks, sent_chunks, platform_message_ids,
                        files_status, created_at, updated_at
                    )
                    VALUES (
                        %s, 'data_task', %s, NULL, %s, %s::uuid,
                        FALSE, TRUE, %s, %s,
                        'sent', 0, 0, '[]'::jsonb,
                        '[]'::jsonb, NOW(), NOW()
                    )
                    ON CONFLICT (delivery_key) DO UPDATE
                    SET status = 'sent', updated_at = NOW()
                    """,
                    (
                        key,
                        row["user_id"],
                        row["execution_id"],
                        row["execution_id"],
                        payload["reconcile_platform"],
                        payload["reconcile_canonical_target"],
                    ),
                )
    return keys


def _gateway(world: World, calls: list[dict[str, Any]], *, item_limit: int | None = None):
    async def _notify(
        targets: list[str],
        text: str = "",
        card: dict[str, Any] | None = None,
        *,
        images: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        gateway_url: str | None = None,
        service_token: str | None = None,
        timeout_s: float | None = None,
        http_client: Any = None,
    ) -> dict[str, Any]:
        assert text == ""
        assert card is None
        assert images is None
        assert gateway_url is None
        assert service_token is None
        assert http_client is None
        assert len(targets) == 1
        assert timeout_s is not None and timeout_s > 0
        calls.append({"target": targets[0], "run_id": run_id, "timeout_s": timeout_s})
        await asyncio.sleep(0.05)
        await _write_sent_ledger(
            world,
            str(run_id),
            targets[0],
            item_limit=item_limit,
        )
        return {
            "ok": True,
            "status": "sent",
            "results": [{"target": targets[0], "ok": True, "status": "sent"}],
        }

    return create_autospec(post_data_task_gateway_notify, side_effect=_notify)


@pytest.mark.asyncio
async def test_dispatch_confirms_every_saved_card_once(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_db(monkeypatch)
    first = _card("First [10-K](https://example.com/msft/10k)")
    second = _card("Second [10-Q](https://example.com/msft/10q)")
    saved = await _submit(world, cards=[first, second])
    assert saved["ok"] is True
    original_cards = saved["payload"]["cards"]
    digest = saved["payload"]["content_sha256"]
    await _insert_data_run(world, world.execution_id, status="succeeded")
    calls: list[dict[str, Any]] = []
    notify = _gateway(world, calls)
    sent = await _dispatch(world, notify)
    assert sent["ok"] is True
    assert sent["delivery_confirmed"] is True
    assert sent["status"] == "sent"
    assert sent["execution_id"] == world.execution_id
    assert sent["payload"]["cards"] == original_cards
    assert sent["payload"]["content_sha256"] == digest
    assert {call["target"] for call in calls} == {world.chat_a, world.chat_b}
    assert {call["run_id"] for call in calls} == {world.execution_id}
    assert len(calls) == 2
    rows = await _rows(world)
    assert len(rows) == 2
    assert all(row["status"] == "sent" and row["attempt_count"] == 1 for row in rows)
    assert all(row["payload_json"]["cards"] == original_cards for row in rows)
    assert all(row["payload_json"]["content_sha256"] == digest for row in rows)
    assert all(row["error_code"] in (None, "") for row in rows)
    ledger = await _channel_rows(world, world.execution_id)
    assert len(ledger) == 4
    assert all(row["kind"] == "data_task" and row["status"] == "sent" for row in ledger)
    assert await _notify_status(world, world.execution_id) == "pending_dispatch"

    again = await _dispatch(world, notify)
    assert again["delivery_confirmed"] is True
    assert len(calls) == 2
    assert all(row["attempt_count"] == 1 for row in await _rows(world))


@pytest.mark.asyncio
async def test_dispatch_keeps_an_incomplete_ledger_unconfirmed_and_does_not_resend(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_db(monkeypatch)
    saved = await _submit(world, cards=[_card("First"), _card("Second")])
    assert saved["ok"] is True
    calls: list[dict[str, Any]] = []
    notify = _gateway(world, calls, item_limit=1)
    incomplete = await _dispatch(world, notify)
    assert incomplete["delivery_confirmed"] is False
    assert incomplete["status"] == "unknown"
    assert len(calls) == 2
    rows = await _rows(world)
    assert all(row["status"] == "pending" for row in rows)
    assert all(row["error_code"] == "delivery_uncertain" for row in rows)
    assert all(row["attempt_count"] == 1 for row in rows)
    again = await _dispatch(world, notify)
    assert again["delivery_confirmed"] is False
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_quiet_and_missing_report_dispatch_does_not_send(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_db(monkeypatch)
    quiet = await _submit(world, material="no", cards=None)
    assert quiet["quiet"] is True
    calls: list[dict[str, Any]] = []
    notify = _gateway(world, calls)
    missing_id = str(uuid4())
    world.execution_ids.append(missing_id)
    missing = await _dispatch(world, notify, missing_id)
    assert missing["ok"] is False
    assert missing["code"] == "not_found"
    assert missing["delivery_confirmed"] is False
    quiet_view = await _dispatch(world, notify)
    assert quiet_view["ok"] is False
    assert quiet_view["delivery_confirmed"] is False
    assert calls == []
    assert await notify_dispatch.claim_saved_report_delivery(
        world.execution_id,
        "feishu",
        "feishu:chat:oc_not_saved",
    ) == (False, None)
    assert await _rows(world) == []


@pytest.mark.asyncio
async def test_concurrent_dispatch_claims_each_saved_target_once(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_db(monkeypatch)
    saved = await _submit(world, cards=[_card("First"), _card("Second")])
    assert saved["ok"] is True
    calls: list[dict[str, Any]] = []
    notify = _gateway(world, calls)
    await asyncio.gather(_dispatch(world, notify), _dispatch(world, notify))
    assert len(calls) == 2
    assert {call["target"] for call in calls} == {world.chat_a, world.chat_b}
    rows = await _rows(world)
    assert {row["attempt_count"] for row in rows} == {1}
    assert {row["status"] for row in rows} == {"sent"}
    assert await reports.read_execution_report_confirmed(world.execution_id) is True


@pytest.mark.asyncio
async def test_report_profile_settlement_does_not_send_generic_text(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_db(monkeypatch)
    saved = await _submit(world, cards=[_card("First"), _card("Second")])
    assert saved["delivery_confirmed"] is False
    post = create_autospec(
        notify_dispatch.post_bridge_notify,
        side_effect=AssertionError("generic notification"),
    )
    automation = {
        "automation_id": world.automation_id,
        "user_id": world.owner,
        "name": "AM digest",
        "metadata": _profile(world.chat_a, world.chat_b, world.discord_target),
    }
    assert await notify_dispatch.on_automation_settled(
        automation=automation,
        execution_id=world.execution_id,
        final_text="Generic completion text must not be sent.",
        post_notify_fn=post,
        **_plan_kwargs(world),
    ) is False
    calls: list[dict[str, Any]] = []
    sent = await _dispatch(world, _gateway(world, calls))
    assert sent["delivery_confirmed"] is True
    assert await notify_dispatch.on_automation_settled(
        automation=automation,
        execution_id=world.execution_id,
        final_text="Generic completion text must not be sent.",
        post_notify_fn=post,
        **_plan_kwargs(world),
    ) is True
    post.assert_not_called()
    assert len(calls) == 2


async def _set_delivery(
    world: World,
    execution_id: str,
    target: str,
    *,
    status: str,
    attempt: int,
    error_code: str | None,
) -> None:
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_notify_deliveries
                SET status = %s,
                    attempt_count = %s,
                    error_code = %s,
                    updated_at = NOW()
                WHERE execution_id = %s
                  AND target = %s
                """,
                (status, attempt, error_code, execution_id, target),
            )
            assert cur.rowcount == 1


def _sourced(rows: list[dict[str, Any]], execution_id: str, source: str) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row["execution_id"] == execution_id and row["payload_json"].get("source") == source
    ]


async def _mark_cards_failed(world: World, execution_id: str, *, attempt: int, error_code: str) -> None:
    for target in (world.chat_a, world.chat_b):
        await _set_delivery(
            world,
            execution_id,
            target,
            status="failed",
            attempt=attempt,
            error_code=error_code,
        )


@pytest.mark.asyncio
async def test_cards_save_a_failure_snapshot_without_changing_the_body(world: World) -> None:
    saved = await _submit(world)
    payload = saved["payload"]
    digest = reports.automation_report_content_sha256(
        profile=payload["profile"],
        content_kind="cards",
        cards=payload["cards"],
        failure_text=None,
    )
    assert payload["content_sha256"] == digest
    assert payload["failure_target_snapshot"] == [world.discord_target]
    assert payload["target_snapshot"] == [world.chat_a, world.chat_b]
    stored = _sourced(await _rows(world), world.execution_id, "automation_report")
    assert len(stored) == 2
    assert all(row["payload_json"]["cards"] == payload["cards"] for row in stored)
    assert all(row["payload_json"]["content_sha256"] == digest for row in stored)

    execution_id, thread_id, run_id = await _add_execution(world)
    text = await _submit(
        world,
        execution_id=execution_id,
        thread_id=thread_id,
        run_id=run_id,
        report_run_id="fail-no-snapshot",
        cards=None,
        failure_text="The digest failed closed.",
    )
    assert text["ok"] is True
    assert text["targets"] == [world.discord_target]
    assert "failure_target_snapshot" not in text["payload"]


@pytest.mark.asyncio
async def test_terminal_card_failure_creates_one_discord_failure_alert(world: World) -> None:
    saved = await _submit(world)
    digest = saved["payload"]["content_sha256"]
    cards = saved["payload"]["cards"]
    early = await reports.ensure_report_failure_alert(world.execution_id)
    assert early == {
        "ok": True,
        "code": "not_terminal",
        "created": False,
        "targets": [world.discord_target],
    }
    await _mark_cards_failed(world, world.execution_id, attempt=1, error_code="http_500")
    retryable = await reports.ensure_report_failure_alert(world.execution_id)
    assert retryable["code"] == "not_terminal"
    assert _sourced(await _rows(world), world.execution_id, reports.FAILURE_ALERT_SOURCE) == []

    for code in ("unknown", "in_flight", "partial"):
        for target in (world.chat_a, world.chat_b):
            await _set_delivery(
                world,
                world.execution_id,
                target,
                status="pending",
                attempt=3,
                error_code=code,
            )
        uncertain = await reports.ensure_report_failure_alert(world.execution_id)
        assert uncertain["code"] == "not_terminal"
    assert _sourced(await _rows(world), world.execution_id, reports.FAILURE_ALERT_SOURCE) == []

    await _mark_cards_failed(world, world.execution_id, attempt=3, error_code="http_500")
    created, repeated = await asyncio.gather(
        reports.ensure_report_failure_alert(world.execution_id),
        reports.ensure_report_failure_alert(world.execution_id),
    )
    assert sorted([created["code"], repeated["code"]]) == ["created", "exists"]
    assert sorted([created["created"], repeated["created"]]) == [False, True]
    alerts = _sourced(await _rows(world), world.execution_id, reports.FAILURE_ALERT_SOURCE)
    assert len(alerts) == 1
    assert alerts[0]["channel"] == "discord"
    assert alerts[0]["target"] == world.discord_target
    assert alerts[0]["status"] == "pending"
    assert alerts[0]["attempt_count"] == 0
    notice = alerts[0]["payload_json"]
    assert notice["original_execution_id"] == world.execution_id
    assert notice["original_content_sha256"] == digest
    assert notice["content_kind"] == "failure_text"
    assert notice["expected_item_count"] == 1
    assert "cards" not in notice
    assert world.chat_a in notice["failure_text"]
    assert world.chat_b in notice["failure_text"]
    original = _sourced(await _rows(world), world.execution_id, "automation_report")
    assert len(original) == 2
    assert all(row["payload_json"]["cards"] == cards for row in original)
    assert all(row["payload_json"]["content_sha256"] == digest for row in original)
    assert all(
        row["payload_json"]["failure_target_snapshot"] == [world.discord_target]
        for row in original
    )
    status = await _status(world)
    assert status["targets"] == [world.chat_a, world.chat_b]
    assert status["delivery_confirmed"] is False
    assert await reports.read_execution_report_confirmed(world.execution_id) is False
    loaded = await reports.load_execution_report_rows(world.execution_id)
    assert {row["target"] for row in loaded} == {world.chat_a, world.chat_b}

    again = await reports.ensure_report_failure_alert(world.execution_id)
    assert again["code"] == "exists"
    assert again["created"] is False
    assert len(_sourced(await _rows(world), world.execution_id, reports.FAILURE_ALERT_SOURCE)) == 1

    missing_id, missing_thread, missing_run = await _add_execution(world)
    missing_saved = await _submit(
        world,
        execution_id=missing_id,
        thread_id=missing_thread,
        run_id=missing_run,
        report_run_id="missing-snapshot",
        cards=[_card("Other [10-Q](https://example.com/msft/10q)")],
    )
    assert missing_saved["created"] is True
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_notify_deliveries
                SET payload_json = payload_json - 'failure_target_snapshot'
                WHERE execution_id = %s
                  AND COALESCE(payload_json->>'source', '') = 'automation_report'
                """,
                (missing_id,),
            )
            assert cur.rowcount == 2
    await _mark_cards_failed(world, missing_id, attempt=3, error_code="http_500")
    missing = await reports.ensure_report_failure_alert(missing_id)
    assert missing["code"] == "failure_snapshot_missing"
    assert missing["created"] is False
    assert _sourced(await _rows(world), missing_id, reports.FAILURE_ALERT_SOURCE) == []

    text_id, text_thread, text_run = await _add_execution(world)
    text_saved = await _submit(
        world,
        execution_id=text_id,
        thread_id=text_thread,
        run_id=text_run,
        report_run_id="original-failure-text",
        cards=None,
        failure_text="The digest failed closed.",
    )
    assert text_saved["targets"] == [world.discord_target]
    await _set_delivery(
        world,
        text_id,
        world.discord_target,
        status="failed",
        attempt=3,
        error_code="http_500",
    )
    derived = await reports.ensure_report_failure_alert(text_id)
    assert derived["code"] == "not_cards"
    assert _sourced(await _rows(world), text_id, reports.FAILURE_ALERT_SOURCE) == []
    assert len(_sourced(await _rows(world), text_id, "automation_report")) == 1


@pytest.mark.asyncio
async def test_terminal_text_failure_alerts_once_and_sent_text_does_not(world: World) -> None:
    body = "# 中国消费周报\n\n本周无结构性变化\n"
    saved = await _submit(
        world,
        cards=None,
        failure_text=None,
        text=body,
        report_run_id="china-consumer-weekly-2026-11-13",
    )
    assert saved["ok"] is True
    assert saved["content_kind"] == "text"
    assert saved["payload"]["text"] == body
    assert saved["payload"]["failure_target_snapshot"] == [world.discord_target]
    early = await reports.ensure_report_failure_alert(world.execution_id)
    assert early["code"] == "not_terminal"
    assert early["targets"] == [world.discord_target]
    await _mark_cards_failed(world, world.execution_id, attempt=1, error_code="http_500")
    assert (await reports.ensure_report_failure_alert(world.execution_id))["code"] == "not_terminal"
    for code in ("unknown", "in_flight", "partial"):
        await _mark_cards_failed(world, world.execution_id, attempt=3, error_code=code)
        uncertain = await reports.ensure_report_failure_alert(world.execution_id)
        assert uncertain["code"] == "not_terminal"
    assert _sourced(await _rows(world), world.execution_id, reports.FAILURE_ALERT_SOURCE) == []

    await _mark_cards_failed(world, world.execution_id, attempt=3, error_code="http_500")
    created = await reports.ensure_report_failure_alert(world.execution_id)
    again = await reports.ensure_report_failure_alert(world.execution_id)
    assert created["code"] == "created"
    assert created["created"] is True
    assert created["targets"] == [world.discord_target]
    assert again["code"] == "exists"
    assert again["created"] is False
    alerts = _sourced(await _rows(world), world.execution_id, reports.FAILURE_ALERT_SOURCE)
    assert len(alerts) == 1
    assert alerts[0]["channel"] == "discord"
    assert alerts[0]["target"] == world.discord_target
    assert alerts[0]["status"] == "pending"
    original = _sourced(await _rows(world), world.execution_id, "automation_report")
    assert len(original) == 2
    assert all(row["payload_json"]["content_kind"] == "text" for row in original)
    assert all(row["payload_json"]["text"] == body for row in original)
    assert all(
        row["payload_json"]["failure_target_snapshot"] == [world.discord_target]
        for row in original
    )

    sent_id, sent_thread, sent_run = await _add_execution(world)
    sent = await _submit(
        world,
        execution_id=sent_id,
        thread_id=sent_thread,
        run_id=sent_run,
        cards=None,
        failure_text=None,
        text=body + "已发送\n",
        report_run_id="china-consumer-weekly-sent",
    )
    assert sent["created"] is True
    for target in (world.chat_a, world.chat_b):
        await _set_delivery(
            world,
            sent_id,
            target,
            status="sent",
            attempt=1,
            error_code=None,
        )
    quiet = await reports.ensure_report_failure_alert(sent_id)
    assert quiet["code"] == "not_terminal"
    assert _sourced(await _rows(world), sent_id, reports.FAILURE_ALERT_SOURCE) == []


@pytest.mark.asyncio
async def test_failure_notice_skips_body_targets_and_sends_other_targets_once(
    world: World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure recipient that already holds the body is not inserted again."""
    caplog.set_level(logging.WARNING, logger=reports.logger.name)
    body = "# 中国消费周报\n\n本周无结构性变化\n"
    await _set_metadata(
        world,
        _profile(
            world.chat_a,
            world.chat_b,
            world.discord_target,
            text_targets=[world.discord_target],
            failure_targets=[world.discord_target],
        ),
    )
    saved = await _submit(
        world,
        cards=None,
        failure_text=None,
        text=body,
        report_run_id="overlap-text",
    )
    assert saved["ok"] is True
    assert saved["targets"] == [world.discord_target]
    early = await reports.ensure_report_failure_alert(world.execution_id)
    assert early["code"] == "not_terminal"
    assert "failure_target_is_delivery_target" not in caplog.text
    await _set_delivery(
        world,
        world.execution_id,
        world.discord_target,
        status="failed",
        attempt=3,
        error_code="http_500",
    )
    skipped = await reports.ensure_report_failure_alert(world.execution_id)
    assert skipped == {
        "ok": True,
        "code": "failure_target_is_delivery_target",
        "created": False,
        "targets": [world.discord_target],
    }
    repeated = await reports.ensure_report_failure_alert(world.execution_id)
    assert repeated["ok"] is True
    assert repeated["code"] == "failure_target_is_delivery_target"
    assert repeated["created"] is False
    assert _sourced(await _rows(world), world.execution_id, reports.FAILURE_ALERT_SOURCE) == []
    assert "failure_target_is_delivery_target" in caplog.text

    extra = "discord:user:123456789012345678"
    partial_id, partial_thread, partial_run = await _add_execution(world)
    await _set_metadata(
        world,
        _profile(
            world.chat_a,
            world.chat_b,
            world.discord_target,
            text_targets=[world.discord_target],
            failure_targets=[world.discord_target, extra],
        ),
    )
    partial_saved = await _submit(
        world,
        execution_id=partial_id,
        thread_id=partial_thread,
        run_id=partial_run,
        cards=None,
        failure_text=None,
        text=body + "部分重叠\n",
        report_run_id="partial-overlap-text",
    )
    assert partial_saved["ok"] is True
    assert partial_saved["targets"] == [world.discord_target]
    await _set_delivery(
        world,
        partial_id,
        world.discord_target,
        status="failed",
        attempt=3,
        error_code="http_500",
    )
    partial = await reports.ensure_report_failure_alert(partial_id)
    partial_again = await reports.ensure_report_failure_alert(partial_id)
    assert partial["ok"] is True
    assert partial["code"] == "created"
    assert partial["created"] is True
    assert partial["targets"] == [extra]
    assert partial_again["code"] == "exists"
    assert partial_again["created"] is False
    partial_alerts = _sourced(await _rows(world), partial_id, reports.FAILURE_ALERT_SOURCE)
    assert len(partial_alerts) == 1
    assert partial_alerts[0]["target"] == extra
    assert partial_alerts[0]["status"] == "pending"

    caplog.clear()
    await _set_metadata(
        world,
        _profile(world.chat_a, world.chat_b, world.discord_target),
    )
    distinct_id, distinct_thread, distinct_run = await _add_execution(world)
    distinct = await _submit(
        world,
        execution_id=distinct_id,
        thread_id=distinct_thread,
        run_id=distinct_run,
        cards=None,
        failure_text=None,
        text=body + "另一目标\n",
        report_run_id="distinct-text",
    )
    assert distinct["ok"] is True
    assert distinct["targets"] == [world.chat_a, world.chat_b]
    await _mark_cards_failed(world, distinct_id, attempt=3, error_code="http_500")
    created = await reports.ensure_report_failure_alert(distinct_id)
    again = await reports.ensure_report_failure_alert(distinct_id)
    assert created["ok"] is True
    assert created["code"] == "created"
    assert created["created"] is True
    assert created["targets"] == [world.discord_target]
    assert again["code"] == "exists"
    distinct_alerts = _sourced(await _rows(world), distinct_id, reports.FAILURE_ALERT_SOURCE)
    assert len(distinct_alerts) == 1
    assert distinct_alerts[0]["target"] == world.discord_target

    card_id, card_thread, card_run = await _add_execution(world)
    cards = await _submit(
        world,
        execution_id=card_id,
        thread_id=card_thread,
        run_id=card_run,
        report_run_id="distinct-cards",
    )
    assert cards["ok"] is True
    assert cards["content_kind"] == "cards"
    assert cards["targets"] == [world.chat_a, world.chat_b]
    await _mark_cards_failed(world, card_id, attempt=3, error_code="http_500")
    card_alert = await reports.ensure_report_failure_alert(card_id)
    assert card_alert["ok"] is True
    assert card_alert["code"] == "created"
    assert card_alert["targets"] == [world.discord_target]
    assert len(_sourced(await _rows(world), card_id, reports.FAILURE_ALERT_SOURCE)) == 1
    assert "failure_target_is_delivery_target" not in caplog.text


@pytest.mark.asyncio
async def test_failure_alert_skips_recovered_cards_and_catchup_sends_once(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_db(monkeypatch)
    await _submit(world)
    denied = await dtn.authorize_data_task_notification_plan(
        run_id=world.execution_id,
        target=world.discord_target,
        **_plan_kwargs(world),
    )
    assert denied["authorized"] is False
    assert denied["code"] == "target_not_routed"

    await _mark_cards_failed(world, world.execution_id, attempt=3, error_code="http_500")
    created = await reports.ensure_report_failure_alert(world.execution_id)
    assert created["created"] is True
    plan = await dtn.authorize_data_task_notification_plan(
        run_id=world.execution_id,
        target=world.discord_target,
        **_plan_kwargs(world),
    )
    assert plan["authorized"] is True
    assert plan["recipient_id"] == world.discord_id
    assert plan["platform"] == "discord"
    notice_text = _sourced(
        await _rows(world), world.execution_id, reports.FAILURE_ALERT_SOURCE
    )[0]["payload_json"]["failure_text"]
    assert len(plan["items"]) == 1
    alert_item = plan["items"][0]
    assert alert_item["item_index"] == 0
    assert alert_item["kind"] == "text"
    assert alert_item["text"] == notice_text
    assert "card" not in alert_item
    assert "artifact" not in alert_item
    assert alert_item["delivery_key"] == (
        f"data_task:{world.execution_id}:discord:{world.discord_id}:item:0"
    )
    card_plan = await dtn.authorize_data_task_notification_plan(
        run_id=world.execution_id,
        target=world.chat_a,
        **_plan_kwargs(world),
    )
    assert card_plan["authorized"] is True
    assert card_plan["items"][0]["delivery_key"] != plan["items"][0]["delivery_key"]

    for target in (world.chat_a, world.chat_b):
        await _set_delivery(
            world,
            world.execution_id,
            target,
            status="sent",
            attempt=3,
            error_code=None,
        )
    calls: list[dict[str, Any]] = []

    async def _notify(
        targets: list[str],
        text: str = "",
        card: dict[str, Any] | None = None,
        *,
        images: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        gateway_url: str | None = None,
        service_token: str | None = None,
        timeout_s: float | None = None,
        http_client: Any = None,
    ) -> dict[str, Any]:
        _ = (text, card, images, gateway_url, service_token, timeout_s, http_client)
        calls.append({"target": targets[0], "run_id": run_id})
        return {
            "ok": False,
            "status": "failed",
            "results": [{"target": targets[0], "ok": False, "status": "failed"}],
        }

    notify = create_autospec(post_data_task_gateway_notify, side_effect=_notify)
    skipped = await notify_dispatch.dispatch_report_failure_alert(
        world.execution_id,
        notify_fn=notify,
        **_plan_kwargs(world),
    )
    assert calls == []
    assert skipped["code"] == "report_recovered"
    recovered = _sourced(await _rows(world), world.execution_id, reports.FAILURE_ALERT_SOURCE)
    assert len(recovered) == 1
    assert recovered[0]["status"] == "skipped"
    assert recovered[0]["error_code"] == "report_recovered"

    recur_id, recur_thread, recur_run = await _add_execution(world)
    recur_saved = await _submit(
        world,
        execution_id=recur_id,
        thread_id=recur_thread,
        run_id=recur_run,
        report_run_id="recur-alert",
        cards=[_card("Recur [10-K](https://example.com/msft/10k)")],
    )
    assert recur_saved["created"] is True
    await _mark_cards_failed(world, recur_id, attempt=3, error_code="http_500")
    assert (await reports.ensure_report_failure_alert(recur_id))["created"] is True
    await _set_delivery(
        world,
        recur_id,
        world.discord_target,
        status="failed",
        attempt=3,
        error_code="http_500",
    )
    repeat = await reports.ensure_report_failure_alert(recur_id)
    assert repeat["code"] == "exists"
    exhausted = await notify_dispatch.dispatch_report_failure_alert(
        recur_id,
        notify_fn=notify,
        **_plan_kwargs(world),
    )
    assert exhausted["ok"] is True
    assert calls == []
    assert len(_sourced(await _rows(world), recur_id, reports.FAILURE_ALERT_SOURCE)) == 1

    catch_id, catch_thread, catch_run = await _add_execution(world)
    catch_saved = await _submit(
        world,
        execution_id=catch_id,
        thread_id=catch_thread,
        run_id=catch_run,
        report_run_id="catchup-alert",
        cards=[_card("Catch-up [10-K](https://example.com/msft/10k)")],
    )
    assert catch_saved["created"] is True
    await _mark_cards_failed(world, catch_id, attempt=3, error_code="http_500")

    async def _skip_reconcile(**_kwargs: Any) -> int:
        return 0

    async def _send(
        targets: list[str],
        text: str = "",
        card: dict[str, Any] | None = None,
        *,
        images: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        gateway_url: str | None = None,
        service_token: str | None = None,
        timeout_s: float | None = None,
        http_client: Any = None,
    ) -> dict[str, Any]:
        _ = (text, card, images, gateway_url, service_token, timeout_s, http_client)
        calls.append({"target": targets[0], "run_id": run_id})
        if run_id == catch_id and targets == [world.discord_target]:
            rows = await reports.load_execution_failure_alert_rows(catch_id)
            payload = rows[0]["payload_json"]
            async with world.pool.connection() as conn:
                async with conn.cursor() as cur:
                    for key in payload["reconcile_delivery_keys"]:
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_deliveries (
                                delivery_key, kind, user_id, thread_id, run_id,
                                execution_id, is_current, has_non_empty_text,
                                platform, target_address, status, total_chunks,
                                sent_chunks, platform_message_ids, files_status,
                                created_at, updated_at
                            )
                            VALUES (
                                %s, 'data_task', %s, NULL, %s,
                                %s::uuid, FALSE, TRUE,
                                %s, %s, 'sent', 0,
                                0, '[]'::jsonb, '[]'::jsonb,
                                NOW(), NOW()
                            )
                            ON CONFLICT (delivery_key) DO UPDATE
                            SET status = 'sent', updated_at = NOW()
                            """,
                            (
                                key,
                                rows[0]["user_id"],
                                catch_id,
                                catch_id,
                                payload["reconcile_platform"],
                                payload["reconcile_canonical_target"],
                            ),
                        )
            return {
                "ok": True,
                "status": "sent",
                "results": [{"target": targets[0], "ok": True, "status": "sent"}],
            }
        return {
            "ok": False,
            "status": "failed",
            "results": [{"target": targets[0], "ok": False, "status": "failed"}],
        }

    sender = create_autospec(post_data_task_gateway_notify, side_effect=_send)
    bridge = create_autospec(notify_dispatch.post_bridge_notify)
    await notify_dispatch.retry_failed_notify_deliveries(
        post_notify_fn=bridge,
        data_task_notify_fn=sender,
        reconcile_data_tasks_fn=_skip_reconcile,
        max_attempts=3,
        limit=20,
        **_plan_kwargs(world),
    )
    assert all(call["run_id"] != catch_id for call in calls)
    caught = _sourced(await _rows(world), catch_id, reports.FAILURE_ALERT_SOURCE)
    assert len(caught) == 1
    assert caught[0]["status"] == "pending"
    assert caught[0]["attempt_count"] == 0
    calls.clear()
    await notify_dispatch.retry_failed_notify_deliveries(
        post_notify_fn=bridge,
        data_task_notify_fn=sender,
        reconcile_data_tasks_fn=_skip_reconcile,
        max_attempts=3,
        limit=20,
        **_plan_kwargs(world),
    )
    assert {"target": world.discord_target, "run_id": catch_id} in calls
    sent_alert = _sourced(await _rows(world), catch_id, reports.FAILURE_ALERT_SOURCE)
    assert len(sent_alert) == 1
    assert sent_alert[0]["status"] == "sent"
    assert len(_sourced(await _rows(world), catch_id, "automation_report")) == 2


async def _submit_distinct_cards(
    world: World,
    report_run_id: str,
    content: str,
) -> tuple[str, dict[str, Any]]:
    execution_id, thread_id, run_id = await _add_execution(world)
    saved = await _submit(
        world,
        execution_id=execution_id,
        thread_id=thread_id,
        run_id=run_id,
        report_run_id=report_run_id,
        cards=[_card(content)],
    )
    assert saved["ok"] is True
    assert saved["created"] is True
    return execution_id, saved


async def _native_card_progress(
    world: World,
    execution_id: str,
    target: str,
    *,
    sent: bool,
) -> str:
    plan = await dtn.authorize_data_task_notification_plan(
        run_id=execution_id,
        target=target,
        **_plan_kwargs(world),
    )
    assert plan["authorized"] is True
    delivery_key = plan["items"][0]["delivery_key"]
    claim = await dtn.authorize_and_claim_data_task_delivery(
        delivery_key=delivery_key,
        run_id=execution_id,
        target=target,
        total_chunks=1,
        **_plan_kwargs(world),
    )
    assert claim["claim_status"] == "claimed"
    if sent:
        await cg_db.update_delivery_progress(
            delivery_key=delivery_key,
            status="sent",
            sent_chunks=1,
            total_chunks=1,
            platform_message_ids=["123456789012345678"],
            first_response_json={"message_id": "123456789012345678"},
        )
    return delivery_key


async def _quarantine_other_deliveries(
    world: World,
    execution_ids: list[str],
) -> list[dict[str, Any]]:
    """Move other recent rows outside the catch-up lookback, and return the old times."""
    async with world.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT delivery_id::text AS delivery_id, created_at, updated_at
                FROM dalton_notify_deliveries
                WHERE NOT (execution_id = ANY(%s))
                  AND created_at >= NOW() - INTERVAL '24 hours'
                """,
                (execution_ids,),
            )
            saved = [dict(row) for row in await cur.fetchall()]
            if not saved:
                return []
            await cur.execute(
                """
                UPDATE dalton_notify_deliveries
                SET created_at = NOW() - INTERVAL '30 days',
                    updated_at = NOW() - INTERVAL '30 days'
                WHERE delivery_id::text = ANY(%s)
                """,
                ([row["delivery_id"] for row in saved],),
            )
    return saved


async def _restore_delivery_times(world: World, saved: list[dict[str, Any]]) -> None:
    if not saved:
        return
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            for row in saved:
                await cur.execute(
                    """
                    UPDATE dalton_notify_deliveries
                    SET created_at = %s, updated_at = %s
                    WHERE delivery_id = %s::uuid
                    """,
                    (row["created_at"], row["updated_at"], row["delivery_id"]),
                )


@pytest.mark.asyncio
async def test_alert_plan_refuses_a_recovered_or_unresolved_card_ledger(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_db(monkeypatch)
    recovered_id, recovered_saved = await _submit_distinct_cards(
        world,
        "ledger-recovered",
        "Recovered [10-K](https://example.com/msft/recovered)",
    )
    digest = recovered_saved["payload"]["content_sha256"]
    cards = recovered_saved["payload"]["cards"]
    await _mark_cards_failed(world, recovered_id, attempt=3, error_code="http_500")
    assert (await reports.ensure_report_failure_alert(recovered_id))["created"] is True
    for target in (world.chat_a, world.chat_b):
        await _native_card_progress(world, recovered_id, target, sent=True)
    alert_plan = await dtn.authorize_data_task_notification_plan(
        run_id=recovered_id,
        target=world.discord_target,
        **_plan_kwargs(world),
    )
    assert alert_plan["authorized"] is False
    assert alert_plan["code"] == "report_recovered"
    alert_key = dtn.build_data_task_item_delivery_key(
        run_id=recovered_id,
        platform="discord",
        recipient_id=world.discord_id,
        item_index=0,
    )
    alert_claim = await dtn.authorize_and_claim_data_task_delivery(
        delivery_key=alert_key,
        run_id=recovered_id,
        target=world.discord_target,
        total_chunks=1,
        **_plan_kwargs(world),
    )
    assert alert_claim["claim_status"] == "denied"
    recovered_alert = _sourced(
        await _rows(world), recovered_id, reports.FAILURE_ALERT_SOURCE
    )
    assert len(recovered_alert) == 1
    assert recovered_alert[0]["status"] == "skipped"
    assert recovered_alert[0]["error_code"] == "report_recovered"
    original = _sourced(await _rows(world), recovered_id, "automation_report")
    assert len(original) == 2
    assert all(row["payload_json"]["cards"] == cards for row in original)
    assert all(row["payload_json"]["content_sha256"] == digest for row in original)
    assert await reports.read_execution_report_confirmed(recovered_id) is False
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM dalton_channel_deliveries WHERE delivery_key = %s",
                (alert_key,),
            )
            assert await cur.fetchone() is None

    active_id, _active_saved = await _submit_distinct_cards(
        world,
        "ledger-inflight",
        "In flight [10-Q](https://example.com/msft/inflight)",
    )
    await _mark_cards_failed(world, active_id, attempt=3, error_code="http_500")
    assert (await reports.ensure_report_failure_alert(active_id))["created"] is True
    await _native_card_progress(world, active_id, world.chat_a, sent=False)
    held = await dtn.authorize_data_task_notification_plan(
        run_id=active_id,
        target=world.discord_target,
        **_plan_kwargs(world),
    )
    assert held["authorized"] is False
    assert held["code"] == "parent_unresolved"
    active_key = dtn.build_data_task_item_delivery_key(
        run_id=active_id,
        platform="discord",
        recipient_id=world.discord_id,
        item_index=0,
    )
    active_claim = await dtn.authorize_and_claim_data_task_delivery(
        delivery_key=active_key,
        run_id=active_id,
        target=world.discord_target,
        total_chunks=1,
        **_plan_kwargs(world),
    )
    assert active_claim["claim_status"] == "denied"
    active_alert = _sourced(await _rows(world), active_id, reports.FAILURE_ALERT_SOURCE)
    assert len(active_alert) == 1
    assert active_alert[0]["status"] == "pending"
    assert active_alert[0]["attempt_count"] == 0
    assert active_alert[0]["error_code"] in (None, "")


@pytest.mark.asyncio
async def test_catchup_skips_unusable_snapshots_before_the_page_limit(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_db(monkeypatch)
    invalid_ids: list[str] = []
    mutations = (
        "payload_json - 'failure_target_snapshot'",
        "jsonb_set(payload_json, '{failure_target_snapshot}', '[]'::jsonb)",
        "jsonb_set(payload_json, '{failure_target_snapshot}', '\"not-an-array\"'::jsonb)",
    )
    for index, expression in enumerate(mutations):
        execution_id, _saved = await _submit_distinct_cards(
            world,
            f"unusable-snapshot-{index}",
            f"Old snapshot {index} [10-K](https://example.com/msft/old-{index})",
        )
        invalid_ids.append(execution_id)
        await _mark_cards_failed(world, execution_id, attempt=3, error_code="http_500")
        async with world.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    UPDATE dalton_notify_deliveries
                    SET payload_json = {expression},
                        created_at = NOW() - INTERVAL '3 hours',
                        updated_at = NOW() - INTERVAL '3 hours'
                    WHERE execution_id = %s
                      AND COALESCE(payload_json->>'source', '') = 'automation_report'
                    """,
                    (execution_id,),
                )
                assert cur.rowcount == 2
    valid_id, valid_saved = await _submit_distinct_cards(
        world,
        "usable-snapshot",
        "Fresh snapshot [10-K](https://example.com/msft/fresh)",
    )
    valid_digest = valid_saved["payload"]["content_sha256"]
    await _mark_cards_failed(world, valid_id, attempt=3, error_code="http_500")
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_notify_deliveries
                SET created_at = NOW() - INTERVAL '1 hour',
                    updated_at = NOW() - INTERVAL '1 hour'
                WHERE execution_id = %s
                """,
                (valid_id,),
            )
            assert cur.rowcount == 2
    quarantined = await _quarantine_other_deliveries(
        world, [*invalid_ids, valid_id]
    )
    try:
        from src.dalton.notify.channel_transport import UNCERTAIN_NOTIFY_ERROR_CODES

        args = {
            "max_attempts": 3,
            "lookback_hours": 24,
            "limit": 2,
            "uncertain_codes": sorted(UNCERTAIN_NOTIFY_ERROR_CODES),
        }
        await notify_dispatch._catch_up_terminal_report_failure_alerts(**args)
        created = _sourced(await _rows(world), valid_id, reports.FAILURE_ALERT_SOURCE)
        assert len(created) == 1
        assert created[0]["target"] == world.discord_target
        assert created[0]["status"] == "pending"
        assert created[0]["payload_json"]["original_content_sha256"] == valid_digest
        assert "cards" not in created[0]["payload_json"]
        for execution_id in invalid_ids:
            assert _sourced(
                await _rows(world), execution_id, reports.FAILURE_ALERT_SOURCE
            ) == []
        await notify_dispatch._catch_up_terminal_report_failure_alerts(**args)
        assert len(
            _sourced(await _rows(world), valid_id, reports.FAILURE_ALERT_SOURCE)
        ) == 1
    finally:
        await _restore_delivery_times(world, quarantined)


@pytest.mark.asyncio
async def test_ensure_reconcile_lock_order_does_not_deadlock(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_db(monkeypatch)
    execution_id, _saved = await _submit_distinct_cards(
        world,
        "lock-order",
        "Lock order [10-K](https://example.com/msft/locks)",
    )
    await _native_card_progress(world, execution_id, world.chat_a, sent=True)
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_notify_deliveries
                SET status = 'pending', attempt_count = 1, error_code = NULL
                WHERE execution_id = %s
                  AND COALESCE(payload_json->>'source', '') = 'automation_report'
                """,
                (execution_id,),
            )
            assert cur.rowcount == 2
            await cur.execute(
                """
                SELECT delivery_id::text AS delivery_id,
                       execution_id, automation_id, user_id, channel, target,
                       status, error_code, attempt_count, payload_json
                FROM dalton_notify_deliveries
                WHERE execution_id = %s AND target = %s
                """,
                (execution_id, world.chat_a),
            )
            outer = dict(await cur.fetchone())
    errors: list[str] = []

    class _ReconcileErrors(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            error_type = ""
            if isinstance(record.args, tuple) and record.args:
                error_type = str(record.args[-1])
            if error_type in {
                "DeadlockDetected",
                "InFailedSqlTransaction",
                "QueryCanceled",
            }:
                errors.append(error_type)

    handler = _ReconcileErrors()
    reconcile_logger = logging.getLogger(dtn.__name__)
    reconcile_logger.addHandler(handler)
    held = asyncio.Event()
    proceed = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []

    async def gated(row: dict[str, Any], **kwargs: Any) -> bool:
        held.set()
        await asyncio.wait_for(proceed.wait(), timeout=5)
        return await dtn.reconcile_single_data_task_delivery_row(row, **kwargs)

    async def run_ensure(conn: Any) -> Any:
        try:
            result = await reports.ensure_report_failure_alert(
                execution_id,
                conn=conn,
                reconcile_row_fn=gated,
            )
            return ("completed", result.get("code"), result.get("created"))
        except Exception as exc:
            return type(exc).__name__

    async def run_reconcile(conn: Any) -> Any:
        try:
            await dtn.reconcile_single_data_task_delivery_row(
                outer,
                conn=conn,
                allow_retry_candidate_statuses=True,
            )
            return "completed"
        except Exception as exc:
            return type(exc).__name__

    ensure_outcome: Any = None
    reconcile_outcome: Any = None
    try:
        async with world.pool.connection() as first, world.pool.connection() as second:
            try:
                await first.execute("SET deadlock_timeout = '100ms'")
                await second.execute("SET deadlock_timeout = '100ms'")
                await first.execute("SET statement_timeout = '5000ms'")
                await second.execute("SET statement_timeout = '5000ms'")
                first_pid = first.info.backend_pid
                second_pid = second.info.backend_pid
                ensure_task = asyncio.create_task(run_ensure(first))
                tasks.append(ensure_task)
                await asyncio.wait_for(held.wait(), timeout=5)
                reconcile_task = asyncio.create_task(run_reconcile(second))
                tasks.append(reconcile_task)
                deadline = asyncio.get_running_loop().time() + 3
                blocked = False
                while asyncio.get_running_loop().time() < deadline:
                    async with world.pool.connection() as observer:
                        async with observer.cursor(row_factory=dict_row) as cur:
                            await cur.execute(
                                """
                                SELECT pg_blocking_pids(pid) AS blockers
                                FROM pg_stat_activity
                                WHERE datname = current_database()
                                  AND pid = %s
                                """,
                                (second_pid,),
                            )
                            activity = await cur.fetchone()
                    blockers = list((activity or {}).get("blockers") or [])
                    if first_pid in blockers:
                        blocked = True
                        break
                    await asyncio.sleep(0.02)
                proceed.set()
                ensure_outcome, reconcile_outcome = await asyncio.wait_for(
                    asyncio.gather(ensure_task, reconcile_task),
                    timeout=8,
                )
                assert blocked
            finally:
                proceed.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                for held_conn in (first, second):
                    try:
                        await held_conn.rollback()
                    except Exception:
                        pass
                    await held_conn.execute("RESET deadlock_timeout")
                    await held_conn.execute("RESET statement_timeout")
    finally:
        reconcile_logger.removeHandler(handler)

    assert errors == []
    assert ensure_outcome == ("completed", "not_terminal", False)
    assert reconcile_outcome == "completed"
    refreshed = _sourced(await _rows(world), execution_id, "automation_report")
    chat_a = next(row for row in refreshed if row["target"] == world.chat_a)
    assert chat_a["status"] == "sent"
    assert chat_a["error_code"] in (None, "")
    assert _sourced(await _rows(world), execution_id, reports.FAILURE_ALERT_SOURCE) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_id", ["meta-tracker", "amzn-tracker", "googl-tracker"])
async def test_quiet_submit_accepts_company_tracker_profiles(world: World, profile_id: str) -> None:
    await _set_metadata(
        world,
        _profile(world.chat_a, world.chat_b, world.discord_target, profile=profile_id),
    )
    quiet = await _submit(
        world,
        material="no",
        cards=None,
        report_run_id=f"{profile_id}-quiet",
    )
    assert quiet["ok"] is True
    assert quiet["quiet"] is True
    assert quiet.get("code") != "profile_invalid"
    assert await _rows(world) == []


def _report_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if (row.get("payload_json") or {}).get("source") == "automation_report"
    ]


@pytest.mark.asyncio
async def test_text_submit_stores_text_targets_and_replays(world: World) -> None:
    await _set_metadata(
        world,
        _profile(
            world.chat_a,
            world.chat_b,
            world.discord_target,
            text_targets=[world.discord_target],
        ),
    )
    body = "# Discord\n\n| Ticker | P/E |\n| --- | --- |\n| AAPL | 28 |"
    saved = await _submit(
        world,
        cards=None,
        failure_text=None,
        text=body,
        report_run_id="text-discord",
    )
    assert saved["ok"] is True
    assert saved["created"] is True
    assert saved["content_kind"] == "text"
    assert saved["targets"] == [world.discord_target]
    assert saved["payload"]["text"] == body
    assert saved["payload"]["expected_item_count"] == 1
    assert "cards" not in saved["payload"]
    assert "failure_text" not in saved["payload"]
    assert saved["payload"]["failure_target_snapshot"] == [world.discord_target]
    rows = _report_rows(await _rows(world))
    assert len(rows) == 1
    assert rows[0]["channel"] == "discord"
    assert rows[0]["target"] == world.discord_target
    assert rows[0]["status"] == "pending"
    assert rows[0]["payload_json"]["content_sha256"] == saved["content_sha256"]
    assert rows[0]["payload_json"]["text"] == body

    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE automation_executions
                SET status = 'completed', completed_at = NOW()
                WHERE automation_execution_id = %s::uuid
                """,
                (world.execution_id,),
            )
    later_id, later_thread, later_run = await _add_execution(world)
    replay = await _submit(
        world,
        execution_id=later_id,
        thread_id=later_thread,
        run_id=later_run,
        cards=None,
        failure_text=None,
        text=body,
        report_run_id="text-discord",
    )
    assert replay["ok"] is True
    assert replay["created"] is False
    assert replay["content_sha256"] == saved["content_sha256"]
    assert len(_report_rows(await _rows(world))) == 1

    padded = await _submit(
        world,
        execution_id=later_id,
        thread_id=later_thread,
        run_id=later_run,
        cards=None,
        failure_text=None,
        text=f"  {body}  ",
        report_run_id="text-discord",
    )
    assert padded["ok"] is False
    assert padded["code"] == "content_conflict"
    assert len(_report_rows(await _rows(world))) == 1

    conflict = await _submit(
        world,
        execution_id=later_id,
        thread_id=later_thread,
        run_id=later_run,
        cards=None,
        failure_text=None,
        text="a different markdown body",
        report_run_id="text-discord",
    )
    assert conflict["ok"] is False
    assert conflict["code"] == "content_conflict"
    assert len(_report_rows(await _rows(world))) == 1

    card_id, card_thread, card_run = await _add_execution(world)
    cards = await _submit(
        world,
        execution_id=card_id,
        thread_id=card_thread,
        run_id=card_run,
        cards=[_card()],
        failure_text=None,
        text=None,
        report_run_id="cards-still-feishu",
    )
    assert cards["ok"] is True
    assert cards["created"] is True
    assert cards["targets"] == [world.chat_a, world.chat_b]
    card_rows = [
        row
        for row in _report_rows(await _rows(world))
        if row["payload_json"]["content_kind"] == "cards"
    ]
    assert {row["target"] for row in card_rows} == {world.chat_a, world.chat_b}
    assert all(row["channel"] == "feishu" for row in card_rows)


@pytest.mark.asyncio
async def test_text_target_outside_routes_stores_nothing(world: World) -> None:
    foreign = "discord:user:999999999999999999"
    await _set_metadata(
        world,
        _profile(
            world.chat_a,
            world.chat_b,
            world.discord_target,
            text_targets=[foreign],
        ),
    )
    refused = await _submit(
        world,
        cards=None,
        failure_text=None,
        text="not authorized",
        report_run_id="text-unauthorized",
    )
    assert refused["ok"] is False
    assert refused["code"] == "no_authorized_target"
    assert await _rows(world) == []


@pytest.mark.asyncio
async def test_empty_report_targets_can_send_text_only(world: World) -> None:
    await _set_metadata(
        world,
        _profile(
            world.chat_a,
            world.chat_b,
            world.discord_target,
            report_targets=[],
            failure_targets=[],
            text_targets=[world.chat_a],
        ),
    )
    saved = await _submit(
        world,
        cards=None,
        failure_text=None,
        text="feishu group text",
        report_run_id="text-only",
    )
    assert saved["ok"] is True
    assert saved["created"] is True
    assert saved["targets"] == [world.chat_a]
    rows = _report_rows(await _rows(world))
    assert len(rows) == 1
    assert rows[0]["channel"] == "feishu"
    assert rows[0]["target"] == world.chat_a

    later_id, later_thread, later_run = await _add_execution(world)
    cards = await _submit(
        world,
        execution_id=later_id,
        thread_id=later_thread,
        run_id=later_run,
        cards=[_card()],
        report_run_id="cards-without-report-targets",
    )
    assert cards["ok"] is False
    assert cards["code"] == "no_authorized_target"
    assert len(_report_rows(await _rows(world))) == 1


@pytest.mark.asyncio
async def test_unset_text_targets_use_report_targets(world: World) -> None:
    saved = await _submit(
        world,
        cards=None,
        failure_text=None,
        text="fallback markdown",
        report_run_id="text-fallback",
    )
    assert saved["ok"] is True
    assert saved["targets"] == [world.chat_a, world.chat_b]
    rows = _report_rows(await _rows(world))
    assert {row["target"] for row in rows} == {world.chat_a, world.chat_b}
    assert all(row["payload_json"]["content_kind"] == "text" for row in rows)
    assert all("cards" not in row["payload_json"] for row in rows)

    quiet = await _submit(
        world,
        material="no",
        cards=None,
        failure_text=None,
        text="should not store",
        report_run_id="text-quiet",
    )
    assert quiet["ok"] is False
    assert quiet["code"] == "quiet_with_content"
    assert len(_report_rows(await _rows(world))) == 2


@pytest.mark.asyncio
async def test_discord_channel_text_target_is_authorized(world: World) -> None:
    channel_id = str(uuid4().int % 10**18).zfill(18)
    channel_target = f"discord:channel:{channel_id}"
    address = f"discord:{channel_id}"
    async with world.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_channel_chat_bindings
                    (platform, chat_address, chat_name, workspace_id, bound_by_user_id)
                VALUES ('discord', %s, 'Channel', NULL, %s)
                """,
                (address, world.owner),
            )
            await cur.execute(
                """
                UPDATE dalton_channel_user_settings
                SET apps = apps || %s::jsonb
                WHERE user_id = %s
                """,
                (
                    json.dumps({"discord": {"chats": {address: {"name": "Channel"}}}}),
                    world.owner,
                ),
            )
    world.auth.routes.append(channel_target)
    await _set_metadata(
        world,
        _profile(
            world.chat_a,
            world.chat_b,
            world.discord_target,
            text_targets=[channel_target],
        ),
    )
    saved = await _submit(
        world,
        cards=None,
        failure_text=None,
        text="channel markdown",
        report_run_id="text-channel",
    )
    assert saved["ok"] is True
    assert saved["created"] is True
    assert saved["targets"] == [channel_target]
    rows = _report_rows(await _rows(world))
    assert len(rows) == 1
    assert rows[0]["channel"] == "discord"
    assert rows[0]["target"] == channel_target
    assert rows[0]["payload_json"]["expected_item_count"] == 1
