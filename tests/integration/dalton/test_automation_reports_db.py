"""PostgreSQL storage tests for automation report registration.

The DSN must name a database ending in ``_test``. Nothing here is sent to a
gateway, and the default localhost database is ``langalpha_test``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
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


def _profile(
    chat_a: str,
    chat_b: str,
    discord_target: str,
    *,
    report_targets: list[str] | None = None,
    failure_targets: list[str] | None = None,
    profile: str = "market-digest-am",
) -> dict[str, Any]:
    return {
        "ticker": "MSFT",
        "report_profile": {
            "profile": profile,
            "report_targets": [chat_a, chat_b] if report_targets is None else report_targets,
            "failure_targets": [discord_target] if failure_targets is None else failure_targets,
        },
    }


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
    assert cards_on_failure_only["code"] == "no_authorized_target"
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
