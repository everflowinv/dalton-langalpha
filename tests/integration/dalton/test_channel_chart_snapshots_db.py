"""Real Postgres integration tests for Channel Phase 2 Batch 2 (DM chart snapshots).

Reads the test database connection string from ``DALTON_CHANNEL_GATEWAY_TEST_DSN``
(or ``DALTON_TEST_DB_DSN`` / ``TEST_DATABASE_URL``); skips cleanly when unset/unreachable.
Upgrades the isolated test database to current upstream and Dalton Alembic heads without
dropping ``public``, and cleans up its own prefixed rows on teardown.

Covers:
1. Both snapshot registration entry points on real Postgres:
   - Synchronous ``src.tools.chart_annotation.layers._publish`` (DM registers ``pending`` in
     the same transaction as ``chart_annotations``; group chat skips snapshot registration).
   - Background ``command_runs.finalize`` (commits ``chart_annotations`` + ``pending`` snapshot
     on the launching run, resolves via ``command_report`` on the report-back run, and pins
     ``dm_recipient_platform_user_id`` for Feishu & Discord background DM deliveries).
2. Download verification on real Postgres:
   - ``<platform>:@me`` requires a matching ``in_flight`` DM delivery with verified pinned
     ``dm_recipient_platform_user_id`` matching current binding (denied when missing/completed/rebound).
   - Weixin download uses ``dalton_channel_weixin_accounts.user_id``, enforces valid
     ``X-Gateway-Epoch`` lease, and never falls back to legacy ``dalton_channel_identities``
     when the Weixin account is not ``active``.
3. State transitions & stale cleanup on real Postgres:
   - Re-publishing the same ``(chart_id, layer_id)`` assigns a fresh ``registration_id`` and
     prevents an earlier render task from overwriting the newer registration.
   - ``expire_stale_pending_chart_snapshots`` marks pending entries older than TTL as ``failed``
     and prevents late render tasks from reviving them to ``ready``.
4. Automation DM finish flow on real Postgres:
   - ``dalton_channel_automation_runs`` with DM target enables snapshot registration,
     ``finish_automation_run`` returns ``run_id`` + ``chart_snapshots``, and automation DM claim
     pins ``dm_recipient_platform_user_id`` for verified ``@me`` snapshot download.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ptc_agent.core.sandbox.jobs.protocol import Outbox
from src.dalton import channel_chart_snapshots as ccs
from src.dalton import channel_snapshot_storage as css
from src.dalton import colleague_outbound_guard as cog
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import weixin_multi_account as wx_db
from src.server.database import chart_annotation as ca_db
from src.server.database import pool as db_pool
from src.server.database.encryption import get_encryption_key
from src.server.database.runs import command_runs as cr_db
from src.server.models.chart_layer import ChartRef, CommandOutbox
from src.tools.chart_annotation import layers as chart_layers_mod

pytestmark = pytest.mark.integration

_DSN_ENV_VARS = (
    "DALTON_CHANNEL_GATEWAY_TEST_DSN",
    "DALTON_TEST_DB_DSN",
    "TEST_DATABASE_URL",
)

VALID_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
VALID_SHA256 = hashlib.sha256(VALID_PNG).hexdigest()


@pytest.fixture(scope="session", autouse=True)
def dalton_schema():
    """Override directory conftest autouse fixture so unset DSN skips without opening test_db_pool."""
    yield


@pytest.fixture(autouse=True)
def cleanup_dalton_tables():
    """Override directory conftest autouse fixture so unset DSN skips without opening test_db_pool."""
    yield


from tests.integration.dalton._db_guard import extract_effective_test_db_name


def _get_test_dsn() -> str:
    for var in _DSN_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            try:
                extract_effective_test_db_name(val, source=var)
            except ValueError as exc:
                pytest.fail(str(exc))
            return val

    host = (os.getenv("TEST_DB_HOST") or "").strip()
    port = (os.getenv("TEST_DB_PORT") or "").strip()
    name = (os.getenv("TEST_DB_NAME") or "").strip().lstrip("/")
    missing = [
        k
        for k, v in (("TEST_DB_HOST", host), ("TEST_DB_PORT", port), ("TEST_DB_NAME", name))
        if not v
    ]
    if missing:
        pytest.fail(
            "Refusing to run snapshot integration tests without explicit test DB configuration "
            f"(missing {', '.join(missing)}; or set one of {', '.join(_DSN_ENV_VARS)}). "
            "Falling back to localhost:5432 is disabled."
        )
    if not name.endswith("_test"):
        pytest.fail(
            f"Refusing to run snapshot integration tests against non-test DB {name!r} (from TEST_DB_NAME)"
        )
    user = os.getenv("TEST_DB_USER", "postgres")
    password = os.getenv("TEST_DB_PASSWORD", "postgres")
    sslmode = os.getenv("TEST_DB_SSLMODE", "prefer")
    fallback_dsn = f"postgresql://{user}:{password}@{host}:{port}/{name}?sslmode={sslmode}"
    try:
        with psycopg.connect(fallback_dsn, connect_timeout=2):
            return fallback_dsn
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"None of {_DSN_ENV_VARS} is set and default test DB {host}:{port}/{name} is unreachable ({exc}); skipping real Postgres chart snapshot integration tests"
        )


def _run_alembic_upgrade_sync(cfg: Config, revision: str) -> None:
    command.upgrade(cfg, revision)


def _revision_is_target_or_descendant(
    script: ScriptDirectory, current: str, target: str
) -> bool:
    seen: set[str] = set()
    revision: str | None = current
    while isinstance(revision, str) and revision not in seen:
        if revision == target:
            return True
        seen.add(revision)
        item = script.get_revision(revision)
        if item is None:
            return False
        down = item.down_revision
        revision = down if isinstance(down, str) else None
    return False


async def _ensure_alembic_upgraded_to_head(dsn: str) -> None:
    project_root = Path(__file__).resolve().parents[3]
    sa_url = dsn.replace("postgresql://", "postgresql+psycopg://", 1).replace("%", "%%")

    upstream_cfg = Config(str(project_root / "alembic.ini"))
    upstream_cfg.set_main_option("script_location", str(project_root / "migrations"))
    upstream_cfg.set_main_option("sqlalchemy.url", sa_url)
    upstream_head = ScriptDirectory.from_config(upstream_cfg).get_current_head()

    dalton_cfg = Config(str(project_root / "src" / "dalton" / "alembic.ini"))
    dalton_cfg.set_main_option(
        "script_location", str(project_root / "src" / "dalton" / "migrations")
    )
    dalton_cfg.set_main_option("sqlalchemy.url", sa_url)
    dalton_script = ScriptDirectory.from_config(dalton_cfg)
    dalton_target = dalton_script.get_current_head()
    assert dalton_target is not None

    current_upstream: str | None = None
    dalton_versions: list[str] = []
    async with (
        await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute("SELECT to_regclass('public.alembic_version')")
        row = await cur.fetchone()
        if row and row[0] is not None:
            await cur.execute("SELECT version_num FROM alembic_version LIMIT 1")
            vrow = await cur.fetchone()
            if vrow:
                current_upstream = str(vrow[0])

        await cur.execute("SELECT to_regclass('public.alembic_version_dalton')")
        drow = await cur.fetchone()
        if drow and drow[0] is not None:
            await cur.execute("SELECT version_num FROM alembic_version_dalton")
            dalton_versions = [
                str(item[0])
                for item in await cur.fetchall()
                if item and item[0]
            ]

    if current_upstream != upstream_head:
        await asyncio.to_thread(_run_alembic_upgrade_sync, upstream_cfg, "head")

    if not any(
        _revision_is_target_or_descendant(dalton_script, version, dalton_target)
        for version in dalton_versions
    ):
        await asyncio.to_thread(_run_alembic_upgrade_sync, dalton_cfg, dalton_target)


@pytest_asyncio.fixture
async def snapshot_db_pool():
    dsn = _get_test_dsn()
    os.environ.setdefault("BYOK_ENCRYPTION_KEY", "test-channel-snapshot-encryption-key-32b")
    await _ensure_alembic_upgraded_to_head(dsn)

    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=2,
        max_size=10,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        open=False,
    )
    await pool.open()
    await pool.wait()
    try:
        yield pool
    finally:
        await pool.close()


async def _cleanup_rows(
    pool: AsyncConnectionPool,
    *,
    prefix: str,
    user_ids: list[str],
    workspace_ids: list[str],
    thread_ids: list[str],
    command_ids: list[str],
) -> None:
    like_pat = f"%{prefix}%"
    ws_uuids = [w for w in workspace_ids if w]
    th_uuids = [t for t in thread_ids if t]
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM dalton_channel_weixin_audit_logs WHERE target_user_id = ANY(%s) OR ilink_bot_id LIKE %s",
            (user_ids, like_pat),
        )
        await cur.execute(
            "DELETE FROM dalton_channel_weixin_accounts WHERE user_id = ANY(%s) OR ilink_bot_id LIKE %s",
            (user_ids, like_pat),
        )
        await cur.execute(
            "DELETE FROM dalton_channel_thread_watches WHERE dalton_user_id = ANY(%s)",
            (user_ids,),
        )
        await cur.execute(
            "DELETE FROM dalton_channel_background_runs WHERE dalton_user_id = ANY(%s)",
            (user_ids,),
        )
        await cur.execute(
            "DELETE FROM dalton_channel_automation_runs WHERE user_id = ANY(%s)",
            (user_ids,),
        )
        await cur.execute(
            "DELETE FROM dalton_channel_deliveries WHERE user_id = ANY(%s) OR delivery_key LIKE %s",
            (user_ids, like_pat),
        )
        await cur.execute(
            "DELETE FROM dalton_channel_inbound_events WHERE dalton_user_id = ANY(%s) OR message_id LIKE %s",
            (user_ids, like_pat),
        )
        await cur.execute(
            "DELETE FROM dalton_channel_session_generations WHERE dalton_user_id = ANY(%s)",
            (user_ids,),
        )
        await cur.execute(
            "DELETE FROM dalton_channel_user_settings WHERE user_id = ANY(%s)",
            (user_ids,),
        )
        await cur.execute(
            "DELETE FROM dalton_channel_bind_codes WHERE user_id = ANY(%s)",
            (user_ids,),
        )
        await cur.execute(
            "DELETE FROM dalton_channel_identities WHERE user_id = ANY(%s) OR platform_user_id LIKE %s",
            (user_ids, like_pat),
        )
        if command_ids:
            await cur.execute(
                "DELETE FROM command_runs WHERE command_id = ANY(%s)",
                (command_ids,),
            )
        if th_uuids:
            await cur.execute(
                "DELETE FROM hook_outbox WHERE conversation_thread_id = ANY(%s::uuid[])",
                (th_uuids,),
            )
            await cur.execute(
                "DELETE FROM conversation_queries WHERE conversation_thread_id = ANY(%s::uuid[])",
                (th_uuids,),
            )
            await cur.execute(
                "DELETE FROM conversation_responses WHERE conversation_thread_id = ANY(%s::uuid[])",
                (th_uuids,),
            )
            await cur.execute(
                "DELETE FROM conversation_threads WHERE conversation_thread_id = ANY(%s::uuid[])",
                (th_uuids,),
            )
        if ws_uuids:
            await cur.execute(
                "DELETE FROM chart_annotations WHERE workspace_id = ANY(%s::uuid[])",
                (ws_uuids,),
            )
            await cur.execute(
                "DELETE FROM workspaces WHERE workspace_id = ANY(%s::uuid[])",
                (ws_uuids,),
            )
        await cur.execute(
            "DELETE FROM users WHERE user_id = ANY(%s)",
            (user_ids,),
        )


@pytest_asyncio.fixture
async def snap_db_env(
    snapshot_db_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
):
    """Route DB connections to real Postgres pool, enable channel capabilities, and clean up on teardown."""
    monkeypatch.setenv("DALTON_ENABLED", "true")
    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.delenv("DALTON_PROFILE", raising=False)
    monkeypatch.setenv("DALTON_CAP_CHANNEL_GATEWAY", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_FEISHU", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_DISCORD", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_WEIXIN", "true")

    @asynccontextmanager
    async def _pooled_get_db_connection(existing_conn=None):
        if existing_conn is not None:
            yield existing_conn
            return
        async with snapshot_db_pool.connection() as conn:
            yield conn

    mock_gdc = create_autospec(
        db_pool.get_db_connection, side_effect=_pooled_get_db_connection
    )
    monkeypatch.setattr(
        db_pool,
        "get_or_create_pool",
        create_autospec(db_pool.get_or_create_pool, return_value=snapshot_db_pool),
    )
    monkeypatch.setattr(db_pool, "get_db_connection", mock_gdc)
    monkeypatch.setattr(ccs, "get_db_connection", mock_gdc)
    monkeypatch.setattr(css, "get_db_connection", mock_gdc)
    monkeypatch.setattr(cog, "get_db_connection", mock_gdc)
    monkeypatch.setattr(cg_db, "get_db_connection", mock_gdc)
    monkeypatch.setattr(wx_db, "get_db_connection", mock_gdc)
    monkeypatch.setattr(ca_db, "get_db_connection", mock_gdc)
    monkeypatch.setattr(
        cg_db,
        "check_team_member_status_live",
        create_autospec(cg_db.check_team_member_status_live, return_value="active"),
    )
    monkeypatch.setattr(
        cg_db,
        "get_user_tier",
        create_autospec(cg_db.get_user_tier, return_value="internal"),
    )

    prefix = f"snapb2-{uuid4().hex[:8]}"
    user_id = f"{prefix}_user"
    ws_id = str(uuid4())
    cleanup_user_ids = [user_id]
    cleanup_workspace_ids = [ws_id]
    cleanup_thread_ids: list[str] = []
    cleanup_command_ids: list[str] = []

    async with snapshot_db_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO users (user_id, email, name, created_at, updated_at)
            VALUES (%s, %s, 'Snapshot User', NOW(), NOW())
            """,
            (user_id, f"{user_id}@example.com"),
        )
        await cur.execute(
            """
            INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
            VALUES (%s::uuid, %s, %s, 'running', NOW(), NOW())
            """,
            (ws_id, user_id, f"{prefix}_WS"),
        )

    env: dict[str, Any] = {
        "pool": snapshot_db_pool,
        "prefix": prefix,
        "user_id": user_id,
        "ws_id": ws_id,
        "cleanup_thread_ids": cleanup_thread_ids,
        "cleanup_command_ids": cleanup_command_ids,
    }
    try:
        yield env
    finally:
        await _cleanup_rows(
            snapshot_db_pool,
            prefix=prefix,
            user_ids=cleanup_user_ids,
            workspace_ids=cleanup_workspace_ids,
            thread_ids=cleanup_thread_ids,
            command_ids=cleanup_command_ids,
        )


async def _seed_thread_and_response(
    pool: AsyncConnectionPool,
    *,
    ws_id: str,
    user_id: str,
    thread_id: str | None = None,
    run_id: str | None = None,
    turn_index: int = 1,
    query_type: str = "initial",
    query_metadata: dict[str, Any] | None = None,
    response_metadata: dict[str, Any] | None = None,
    response_status: str = "completed",
) -> tuple[str, str]:
    tid = thread_id or str(uuid4())
    rid = run_id or str(uuid4())
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
                INSERT INTO conversation_threads (
                    conversation_thread_id, workspace_id, user_id,
                    current_status, thread_index, created_at, updated_at
                )
                VALUES (
                    %s::uuid,
                    %s::uuid,
                    %s,
                    'completed',
                    COALESCE(
                        (
                            SELECT MAX(thread_index) + 1
                            FROM conversation_threads
                            WHERE workspace_id = %s::uuid
                        ),
                        1
                    ),
                    NOW(),
                    NOW()
                )
                ON CONFLICT (conversation_thread_id) DO NOTHING
                """,
            (tid, ws_id, user_id, ws_id),
        )
        await cur.execute(
            """
                INSERT INTO conversation_queries (
                    conversation_query_id, conversation_thread_id, turn_index,
                    content, type, metadata, created_at
                )
                VALUES (%s::uuid, %s::uuid, %s, 'chart test', %s, %s::jsonb, NOW())
                """,
            (
                str(uuid4()),
                tid,
                turn_index,
                query_type,
                json.dumps(query_metadata or {}),
            ),
        )
        # Lifecycle trigger (migration 018) requires rows to be born 'in_progress'
        # before transitioning to a terminal status.
        await cur.execute(
            """
                INSERT INTO conversation_responses (
                    conversation_response_id, conversation_thread_id, turn_index,
                    status, user_id, metadata, created_at
                )
                VALUES (%s::uuid, %s::uuid, %s, 'in_progress', %s, %s::jsonb, NOW())
                """,
            (rid, tid, turn_index, user_id, json.dumps(response_metadata or {})),
        )
        if response_status != "in_progress":
            await cur.execute(
                """
                    UPDATE conversation_responses
                    SET status = %s
                    WHERE conversation_response_id = %s::uuid
                      AND status = 'in_progress'
                    """,
                (response_status, rid),
            )
    return tid, rid


@pytest.mark.asyncio
async def test_sync_publish_registers_pending_for_dm_and_skips_group_real_pg(
    snap_db_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entry point 1 (layers._publish): DM inbound event (chat_address) commits pending snapshot in same tx; group chat skips."""
    pool: AsyncConnectionPool = snap_db_env["pool"]
    prefix: str = snap_db_env["prefix"]
    user_id: str = snap_db_env["user_id"]
    ws_id: str = snap_db_env["ws_id"]

    mock_schedule_render = create_autospec(
        ccs.schedule_chart_snapshots_render, return_value=None
    )
    monkeypatch.setattr(
        ccs,
        "schedule_chart_snapshots_render",
        mock_schedule_render,
    )

    # Bind Feishu user
    ou_id = f"ou_{prefix}_dm"
    code = await cg_db.create_bind_code(user_id=user_id, platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=ou_id,
        code=code["code"],
        is_group=False,
    )

    # 1. DM thread
    dm_tid, dm_rid = await _seed_thread_and_response(pool, ws_id=ws_id, user_id=user_id)
    snap_db_env["cleanup_thread_ids"].append(dm_tid)
    dm_msg_id = f"{prefix}_om_dm_1"
    await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=ou_id,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=f"dm:{ou_id}",
        message_id=dm_msg_id,
    )
    await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=dm_msg_id,
        status="streaming",
        thread_id=dm_tid,
        run_id=dm_rid,
        turn_index=1,
    )

    layer_line = json.dumps(
        {
            "version": 1,
            "id": f"layer-dm-{prefix}",
            "symbol": "NVDA",
            "timeframe": "1day",
            "title": "NVDA DM Layer",
            "levels": [{"price": 120.0, "title": "Support"}],
        }
    )
    published_dm = await chart_layers_mod._publish(
        Outbox(lines=(layer_line,)),
        ws_id,
        thread_id=dm_tid,
        run_id=dm_rid,
        user_id=user_id,
    )
    assert len(published_dm.layers) == 1
    assert mock_schedule_render.call_count == 1
    assert mock_schedule_render.call_args.kwargs.get("registered_entries")

    st_dm = await ccs.get_run_chart_snapshots_status(
        run_id=dm_rid, thread_id=dm_tid, platform="feishu"
    )
    assert st_dm["has_pending"] is True
    assert len(st_dm["snapshots"]) == 1
    assert st_dm["snapshots"][0]["status"] == "pending"
    assert st_dm["snapshots"][0]["layer_id"] == f"layer-dm-{prefix}"
    assert st_dm["snapshots"][0]["registration_id"]

    # 2. Group chat thread -> layer commits to chart_annotations, but NO snapshot registered
    grp_tid, grp_rid = await _seed_thread_and_response(pool, ws_id=ws_id, user_id=user_id)
    snap_db_env["cleanup_thread_ids"].append(grp_tid)
    grp_msg_id = f"{prefix}_om_grp_1"
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
                INSERT INTO dalton_channel_inbound_events (
                    platform, message_id, request_key, dalton_user_id,
                    chat_type, chat_address, thread_scope_key,
                    thread_id, run_id, turn_index, status, raw_payload
                )
                VALUES (
                    'feishu', %s, %s::uuid, %s,
                    'group', %s, %s,
                    %s::uuid, %s, 1, 'streaming', %s::jsonb
                )
                """,
            (
                grp_msg_id,
                str(uuid4()),
                user_id,
                f"feishu:oc_{prefix}_grp",
                f"chat:oc_{prefix}_grp",
                grp_tid,
                grp_rid,
                json.dumps({"platform_user_id": ou_id}),
            ),
        )

    grp_layer_line = json.dumps(
        {
            "version": 1,
            "id": f"layer-grp-{prefix}",
            "symbol": "AAPL",
            "timeframe": "1day",
            "title": "AAPL Group Layer",
            "levels": [{"price": 210.0, "title": "R1"}],
        }
    )
    published_grp = await chart_layers_mod._publish(
        Outbox(lines=(grp_layer_line,)),
        ws_id,
        thread_id=grp_tid,
        run_id=grp_rid,
        user_id=user_id,
    )
    assert len(published_grp.layers) == 1
    assert mock_schedule_render.call_count == 1  # No new render scheduled for group chat
    st_grp = await ccs.get_run_chart_snapshots_status(
        run_id=grp_rid, thread_id=grp_tid, platform="feishu"
    )
    assert st_grp["has_pending"] is False
    assert st_grp["snapshots"] == []


@pytest.mark.asyncio
async def test_command_finalize_report_back_pinning_and_at_me_download_real_pg(
    snap_db_env: dict[str, Any],
) -> None:
    """Entry point 2 (command_runs.finalize) + command_report linkage + DM recipient pinning + @me auth."""
    pool: AsyncConnectionPool = snap_db_env["pool"]
    prefix: str = snap_db_env["prefix"]
    user_id: str = snap_db_env["user_id"]
    ws_id: str = snap_db_env["ws_id"]

    # Bind Feishu and Discord for this user
    ou_id = f"ou_{prefix}_cmd"
    disc_id = f"100{uuid4().int % 10_000_000}"
    for plat, puid in (("feishu", ou_id), ("discord", disc_id)):
        code = await cg_db.create_bind_code(user_id=user_id, platform=plat)
        await cg_db.consume_bind_code_or_unbind(
            action="bind",
            platform=plat,
            platform_user_id=puid,
            code=code["code"],
            is_group=False,
        )

    tid, launch_rid = await _seed_thread_and_response(
        pool, ws_id=ws_id, user_id=user_id, turn_index=1, query_type="initial"
    )
    snap_db_env["cleanup_thread_ids"].append(tid)

    # Seed original DM inbound event on turn 1
    inb_msg_id = f"{prefix}_om_cmd_1"
    await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=ou_id,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=f"dm:{ou_id}",
        message_id=inb_msg_id,
    )
    await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=inb_msg_id,
        status="completed",
        thread_id=tid,
        run_id=launch_rid,
        turn_index=1,
    )

    # Record background command handoff and finalize with chart outbox in real Postgres
    cmd_id = f"bg-{uuid4().hex[:12]}"
    snap_db_env["cleanup_command_ids"].append(cmd_id)
    cmd_row = await cr_db.record_handoff(
        command_id=cmd_id,
        thread_id=tid,
        user_id=user_id,
        workspace_id=ws_id,
        computer_id=str(uuid4()),
        run_id=launch_rid,
        tool_call_id="call-bg-1",
        tool="ExecuteCode",
        description="Draw NVDA chart",
        command_preview="chart.publish()",
        log_path=f".agents/threads/{tid[:8]}/commands/{cmd_id}.log",
        job_ref={"command_id": cmd_id},
        started_at=datetime.now(UTC) - timedelta(seconds=5),
    )

    ref = ChartRef.of(ws_id, "NVDA", "1day").layer(f"layer-cmd-{prefix}")
    body = {
        "id": ref.layer_id,
        "symbol": "NVDA",
        "timeframe": "1day",
        "title": "NVDA BG Layer",
        "levels": [{"price": 130.0, "title": "Target"}],
    }
    outbox = CommandOutbox(
        rows=((ref, body),),
        report="Published NVDA BG Layer",
        layers=(ref.artifact("NVDA BG Layer", None, None),),
        unsaved_report="Unsaved",
    )
    ended = await cr_db.finalize(
        cmd_row,
        cr_db.Outcome.exited(0, up=True),
        tail="ok",
        outbox=outbox,
    )
    assert ended is not None
    pending_snaps = ended.get("_pending_chart_snapshots")
    assert isinstance(pending_snaps, list) and len(pending_snaps) == 1
    reg_id = pending_snaps[0]["registration_id"]

    # Seed command_report turn 2 + dalton_channel_background_runs row
    _, report_rid = await _seed_thread_and_response(
        pool,
        ws_id=ws_id,
        user_id=user_id,
        thread_id=tid,
        turn_index=2,
        query_type="system",
        query_metadata={
            "report_source": {
                "kind": "command_report",
                "command_id": cmd_id,
                "chart_layers": list(outbox.layers or ()),
            }
        },
    )
    await cg_db.update_inbound_progress(
        background_run={
            "run_id": report_rid,
            "thread_id": tid,
            "dalton_user_id": user_id,
            "platform": "feishu",
            "target_address": "feishu:@me",
            "turn_index": 2,
            "status": "completed",
            "fallback_pending": True,
        },
        thread_watch={
            "thread_id": tid,
            "dalton_user_id": user_id,
            "platform": "feishu",
            "target_address": "feishu:@me",
            "scope_key": f"dm:{ou_id}",
            "generation": 1,
            "last_seen_turn_index": 2,
        },
    )

    # Mark snapshot ready on launching run
    snap_key = css.build_snapshot_key(tid, VALID_SHA256, ref.layer_id)
    ok_upd = await ccs.update_chart_snapshot_status(
        run_id=launch_rid,
        chart_id=ref.chart_id,
        layer_id=ref.layer_id,
        status="ready",
        snapshot_key=snap_key,
        sha256=VALID_SHA256,
        size_bytes=len(VALID_PNG),
        width=1200,
        height=720,
        expected_registration_id=reg_id,
    )
    assert ok_upd is True

    # Status endpoint + replay helper resolve command_report -> launching run's ready snapshot
    st_report = await ccs.get_run_chart_snapshots_status(
        run_id=report_rid, thread_id=tid, platform="feishu"
    )
    assert st_report["has_pending"] is False
    assert len(st_report["snapshots"]) == 1
    assert st_report["snapshots"][0]["snapshot_key"] == snap_key

    replay_map = await ccs.resolve_replay_chart_snapshots_by_run(
        thread_id=tid,
        queries=[
            {
                "turn_index": 2,
                "metadata": {
                    "report_source": {"kind": "command_report", "command_id": cmd_id}
                },
            }
        ],
        responses_by_turn={
            2: {"conversation_response_id": report_rid, "metadata": {}},
        },
    )
    assert report_rid in replay_map
    assert replay_map[report_rid][0]["snapshot_key"] == snap_key

    # run_end helper exposes report_source chart_layers alongside linked snapshots
    run_end_payload = await ccs.get_run_end_chart_payload(
        run_id=report_rid, thread_id=tid
    )
    assert run_end_payload["outbound_blocked"] is False
    assert len(run_end_payload["chart_snapshots"]) == 1
    assert any(
        c["layer_id"] == ref.layer_id and c["chart_id"] == ref.chart_id
        for c in run_end_payload["chart_layers"]
    )

    # Without an in_flight DM delivery (or without delivery_key/attempt), `feishu:@me` MUST be denied
    with pytest.raises(HTTPException) as exc_no_dlv:
        await ccs.download_verified_chart_snapshot(
            snapshot_key=snap_key,
            run_id=report_rid,
            target_address="feishu:@me",
            get_bytes_fn=lambda _k: VALID_PNG,
        )
    assert exc_no_dlv.value.status_code == 403
    assert exc_no_dlv.value.detail["code"] == "snapshot_recipient_denied"

    # Claim `charts:<report_rid>` (background DM delivery) -> MUST pin dm_recipient_platform_user_id!
    charts_key = f"{prefix}:charts:{report_rid}"
    claimed = await cg_db.authorize_and_claim_delivery(
        delivery_key=charts_key,
        kind="fallback",
        user_id=user_id,
        thread_id=tid,
        run_id=report_rid,
        platform="feishu",
        target="feishu:@me",
        has_non_empty_text=False,
        total_chunks=1,
        source_scope_key=f"dm:{ou_id}",
        source_generation=1,
        activity_origin="notification",
    )
    assert claimed["claim_status"] == "claimed"
    assert claimed["dm_recipient_platform_user_id"] == ou_id

    # Pre-send verification succeeds for pinned background DM delivery
    verified = await cg_db.verify_dm_delivery_recipient(
        delivery_key=charts_key,
        delivery_attempt=claimed["delivery_attempt"],
        require_gateway_epoch=False,
    )
    assert verified["authorized"] is True
    assert verified["recipient_id"] == ou_id

    # Missing delivery_key or wrong delivery_attempt MUST still be denied even while claimed delivery is in_flight
    with pytest.raises(HTTPException) as exc_wrong_attempt:
        await ccs.download_verified_chart_snapshot(
            snapshot_key=snap_key,
            run_id=report_rid,
            target_address="feishu:@me",
            delivery_key=charts_key,
            delivery_attempt=int(claimed["delivery_attempt"]) + 1,
            get_bytes_fn=lambda _k: VALID_PNG,
        )
    assert exc_wrong_attempt.value.status_code == 403
    assert exc_wrong_attempt.value.detail["code"] == "snapshot_recipient_denied"

    # Now `feishu:@me` download succeeds with exact delivery_key + delivery_attempt!
    png, mime, fname = await ccs.download_verified_chart_snapshot(
        snapshot_key=snap_key,
        run_id=report_rid,
        target_address="feishu:@me",
        delivery_key=charts_key,
        delivery_attempt=claimed["delivery_attempt"],
        get_bytes_fn=lambda _k: VALID_PNG,
    )
    assert png == VALID_PNG
    assert mime == "image/png"
    assert fname.endswith(".png")

    # Cross-platform `@me` (discord:@me when only feishu delivery is in_flight) MUST be denied
    with pytest.raises(HTTPException) as exc_cross:
        await ccs.download_verified_chart_snapshot(
            snapshot_key=snap_key,
            run_id=report_rid,
            target_address="discord:@me",
            delivery_key=charts_key,
            delivery_attempt=claimed["delivery_attempt"],
            get_bytes_fn=lambda _k: VALID_PNG,
        )
    assert exc_cross.value.status_code == 403
    assert exc_cross.value.detail["code"] == "snapshot_recipient_denied"

    # Once delivery completes (not in_flight), `feishu:@me` is denied again
    await cg_db.update_delivery_progress(
        delivery_key=charts_key,
        sent_chunks=1,
        total_chunks=1,
        status="sent",
        outcome="sent",
        delivery_attempt=claimed["delivery_attempt"],
    )
    with pytest.raises(HTTPException) as exc_done:
        await ccs.download_verified_chart_snapshot(
            snapshot_key=snap_key,
            run_id=report_rid,
            target_address="feishu:@me",
            delivery_key=charts_key,
            delivery_attempt=claimed["delivery_attempt"],
            get_bytes_fn=lambda _k: VALID_PNG,
        )
    assert exc_done.value.status_code == 403
    assert exc_done.value.detail["code"] == "snapshot_recipient_denied"


@pytest.mark.asyncio
async def test_weixin_download_active_account_lease_and_no_legacy_fallback_real_pg(
    snap_db_env: dict[str, Any],
) -> None:
    """Weixin download queries dalton_channel_weixin_accounts.user_id, enforces X-Gateway-Epoch, and never falls back to legacy identities."""
    pool: AsyncConnectionPool = snap_db_env["pool"]
    prefix: str = snap_db_env["prefix"]
    user_id: str = snap_db_env["user_id"]
    ws_id: str = snap_db_env["ws_id"]

    # Ensure Weixin config is enabled and acquire a fresh active gateway lease
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO dalton_channel_weixin_config (singleton_id, enabled, accounts_revision)
            VALUES (1, TRUE, 1)
            ON CONFLICT (singleton_id) DO UPDATE SET enabled = TRUE
            """
        )
    lease = await wx_db.acquire_gateway_lease(
        instance_id=f"{prefix}-gw-1", ttl_seconds=60
    )
    if not lease.get("granted"):
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                    UPDATE dalton_channel_gateway_lease
                    SET expires_at = NOW() - INTERVAL '1 second'
                    WHERE singleton_id = 1
                    """
            )
        lease = await wx_db.acquire_gateway_lease(
            instance_id=f"{prefix}-gw-1", ttl_seconds=60
        )
    assert lease.get("granted") is True
    active_epoch = int(lease["epoch"])

    # Seed real Weixin active account + legacy dalton_channel_identities row
    wx_uid = f"wx_{prefix}_active"
    wx_bot_id = f"bot_{prefix}_1"
    wx_acct_id = str(uuid4())
    enc_key = get_encryption_key()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO dalton_channel_weixin_accounts (
                account_id, user_id, ilink_bot_id, ilink_user_id,
                encrypted_bot_token, status, bot_generation
            )
            VALUES (
                %s::uuid, %s, %s, %s,
                pgp_sym_encrypt('bot-secret', %s), 'active', 2
            )
            """,
            (wx_acct_id, user_id, wx_bot_id, wx_uid, enc_key),
        )
        await cur.execute(
            """
            INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id)
            VALUES (%s, 'weixin', %s)
            """,
            (user_id, wx_uid),
        )

    tid, rid = await _seed_thread_and_response(pool, ws_id=ws_id, user_id=user_id)
    snap_db_env["cleanup_thread_ids"].append(tid)

    ref = ChartRef.of(ws_id, "NVDA", "1day").layer(f"layer-wx-{prefix}")
    snap_key = css.build_snapshot_key(tid, VALID_SHA256, ref.layer_id)
    async with pool.connection() as conn:
        pending = await ccs.register_pending_chart_snapshots_in_tx(
            conn,
            thread_id=tid,
            run_id=rid,
            user_id=user_id,
            bodies=[(ref, {"id": ref.layer_id, "title": "WX Chart"})],
            skip_eligibility_check=True,
        )
    assert len(pending) == 1
    assert await ccs.update_chart_snapshot_status(
        run_id=rid,
        chart_id=ref.chart_id,
        layer_id=ref.layer_id,
        status="ready",
        snapshot_key=snap_key,
        sha256=VALID_SHA256,
        size_bytes=len(VALID_PNG),
        expected_registration_id=pending[0]["registration_id"],
    )

    # Seed matching in_flight Weixin DM delivery (plus one missing account_id/bot_generation)
    dlv_key = f"{prefix}:charts:{rid}"
    dlv_missing_wx_key = f"{prefix}:charts-missing:{rid}"
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
                INSERT INTO dalton_channel_deliveries (
                    delivery_key, kind, user_id, thread_id, run_id,
                    is_current, platform, target_address, status, delivery_attempt,
                    weixin_account_id, weixin_bot_id, weixin_bot_generation,
                    weixin_recipient_id, dm_recipient_platform_user_id
                )
                VALUES (
                    %s, 'fallback', %s, %s::uuid, %s,
                    TRUE, 'weixin', 'weixin:@me', 'in_flight', 1,
                    %s::uuid, %s, 2, %s, %s
                )
                """,
            (dlv_key, user_id, tid, rid, wx_acct_id, wx_bot_id, wx_uid, wx_uid),
        )
        await cur.execute(
            """
                INSERT INTO dalton_channel_deliveries (
                    delivery_key, kind, user_id, thread_id, run_id,
                    is_current, platform, target_address, status, delivery_attempt,
                    weixin_account_id, weixin_bot_id, weixin_bot_generation,
                    weixin_recipient_id, dm_recipient_platform_user_id
                )
                VALUES (
                    %s, 'fallback', %s, %s::uuid, %s,
                    TRUE, 'weixin', 'weixin:@me', 'in_flight', 1,
                    NULL, NULL, NULL, %s, %s
                )
                """,
            (dlv_missing_wx_key, user_id, tid, rid, wx_uid, wx_uid),
        )

    # 1. Missing X-Gateway-Epoch -> 409 stale_gateway_epoch
    with pytest.raises(wx_db.StaleGatewayEpochError):
        await ccs.download_verified_chart_snapshot(
            snapshot_key=snap_key,
            run_id=rid,
            target_address="weixin:@me",
            delivery_key=dlv_key,
            delivery_attempt=1,
            gateway_epoch=None,
            get_bytes_fn=lambda _k: VALID_PNG,
        )

    # 2. Stale X-Gateway-Epoch -> 409 stale_gateway_epoch
    with pytest.raises(wx_db.StaleGatewayEpochError):
        await ccs.download_verified_chart_snapshot(
            snapshot_key=snap_key,
            run_id=rid,
            target_address="weixin:@me",
            delivery_key=dlv_key,
            delivery_attempt=1,
            gateway_epoch=active_epoch + 999,
            get_bytes_fn=lambda _k: VALID_PNG,
        )

    # 2b. Weixin delivery missing account_id / bot_generation -> MUST deny 403
    with pytest.raises(HTTPException) as exc_wx_meta:
        await ccs.download_verified_chart_snapshot(
            snapshot_key=snap_key,
            run_id=rid,
            target_address="weixin:@me",
            delivery_key=dlv_missing_wx_key,
            delivery_attempt=1,
            gateway_epoch=active_epoch,
            get_bytes_fn=lambda _k: VALID_PNG,
        )
    assert exc_wx_meta.value.status_code == 403
    assert exc_wx_meta.value.detail["code"] == "snapshot_recipient_denied"

    # 3. Valid X-Gateway-Epoch + active account + exact in_flight DM delivery -> 200 PNG
    png, mime, _fname = await ccs.download_verified_chart_snapshot(
        snapshot_key=snap_key,
        run_id=rid,
        target_address="weixin:@me",
        delivery_key=dlv_key,
        delivery_attempt=1,
        gateway_epoch=active_epoch,
        get_bytes_fn=lambda _k: VALID_PNG,
    )
    assert png == VALID_PNG
    assert mime == "image/png"

    # 4. Mark Weixin account expired (legacy dalton_channel_identities still exists!) -> MUST deny 403
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
                UPDATE dalton_channel_weixin_accounts
                SET status = 'expired'
                WHERE account_id = %s::uuid
                """,
            (wx_acct_id,),
        )
    with pytest.raises(HTTPException) as exc_exp:
        await ccs.download_verified_chart_snapshot(
            snapshot_key=snap_key,
            run_id=rid,
            target_address="weixin:@me",
            delivery_key=dlv_key,
            delivery_attempt=1,
            gateway_epoch=active_epoch,
            get_bytes_fn=lambda _k: VALID_PNG,
        )
    assert exc_exp.value.status_code == 403
    assert exc_exp.value.detail["code"] == "snapshot_recipient_denied"


@pytest.mark.asyncio
async def test_state_transitions_republish_guard_and_stale_reaper_real_pg(
    snap_db_env: dict[str, Any],
) -> None:
    """Re-published layer ignores stale worker updates; stale reaper marks failed and blocks late ready."""
    pool: AsyncConnectionPool = snap_db_env["pool"]
    prefix: str = snap_db_env["prefix"]
    user_id: str = snap_db_env["user_id"]
    ws_id: str = snap_db_env["ws_id"]

    tid, rid = await _seed_thread_and_response(pool, ws_id=ws_id, user_id=user_id)
    snap_db_env["cleanup_thread_ids"].append(tid)

    ref1 = ChartRef.of(ws_id, "NVDA", "1day").layer(f"layer-repub-{prefix}")
    ref2 = ChartRef.of(ws_id, "MSFT", "1day").layer(f"layer-stale-{prefix}")
    snap_key1 = css.build_snapshot_key(tid, VALID_SHA256, ref1.layer_id)
    snap_key2 = css.build_snapshot_key(tid, VALID_SHA256, ref2.layer_id)

    async with pool.connection() as conn:
        reg_v1 = await ccs.register_pending_chart_snapshots_in_tx(
            conn,
            thread_id=tid,
            run_id=rid,
            user_id=user_id,
            bodies=[
                (ref1, {"id": ref1.layer_id, "title": "v1"}),
                (ref2, {"id": ref2.layer_id, "title": "stale"}),
            ],
            skip_eligibility_check=True,
        )
    reg1_v1 = reg_v1[0]["registration_id"]
    reg2_id = reg_v1[1]["registration_id"]

    # Re-publish ref1 with new registration_id
    async with pool.connection() as conn:
        reg_v2 = await ccs.register_pending_chart_snapshots_in_tx(
            conn,
            thread_id=tid,
            run_id=rid,
            user_id=user_id,
            bodies=[(ref1, {"id": ref1.layer_id, "title": "v2"})],
            skip_eligibility_check=True,
        )
    reg1_v2 = reg_v2[0]["registration_id"]
    assert reg1_v2 != reg1_v1

    # Stale worker from v1 MUST be rejected
    stale_ok = await ccs.update_chart_snapshot_status(
        run_id=rid,
        chart_id=ref1.chart_id,
        layer_id=ref1.layer_id,
        status="ready",
        snapshot_key=snap_key1,
        sha256=VALID_SHA256,
        size_bytes=len(VALID_PNG),
        expected_registration_id=reg1_v1,
    )
    assert stale_ok is False

    # Current worker v2 succeeds
    v2_ok = await ccs.update_chart_snapshot_status(
        run_id=rid,
        chart_id=ref1.chart_id,
        layer_id=ref1.layer_id,
        status="ready",
        snapshot_key=snap_key1,
        sha256=VALID_SHA256,
        size_bytes=len(VALID_PNG),
        expected_registration_id=reg1_v2,
    )
    assert v2_ok is True

    # Expire ref2 (still pending) by passing now = +15 minutes
    expired = await ccs.expire_stale_pending_chart_snapshots(
        stale_after_seconds=600,
        now=datetime.now(UTC) + timedelta(minutes=15),
    )
    assert expired >= 1

    st_after_reap = await ccs.get_run_chart_snapshots_status(run_id=rid, thread_id=tid)
    by_lid = {s["layer_id"]: s for s in st_after_reap["snapshots"]}
    assert by_lid[ref1.layer_id]["status"] == "ready"
    assert by_lid[ref2.layer_id]["status"] == "failed"
    assert by_lid[ref2.layer_id]["error"] == "stale_pending_timeout"

    # Late worker trying to mark reaped ref2 as ready MUST be rejected (no failed -> ready)
    late_ok = await ccs.update_chart_snapshot_status(
        run_id=rid,
        chart_id=ref2.chart_id,
        layer_id=ref2.layer_id,
        status="ready",
        snapshot_key=snap_key2,
        sha256=VALID_SHA256,
        size_bytes=len(VALID_PNG),
        expected_registration_id=reg2_id,
    )
    assert late_ok is False


@pytest.mark.asyncio
async def test_automation_dm_registration_finish_and_download_real_pg(
    snap_db_env: dict[str, Any],
) -> None:
    """Automation DM target enables pending snapshot registration, finish returns run_id/snapshots, and DM claim allows @me download."""
    pool: AsyncConnectionPool = snap_db_env["pool"]
    prefix: str = snap_db_env["prefix"]
    user_id: str = snap_db_env["user_id"]
    ws_id: str = snap_db_env["ws_id"]

    ou_id = f"ou_{prefix}_auto"
    code = await cg_db.create_bind_code(user_id=user_id, platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=ou_id,
        code=code["code"],
        is_group=False,
    )

    exec_id = str(uuid4())
    tid, rid = await _seed_thread_and_response(
        pool,
        ws_id=ws_id,
        user_id=user_id,
        response_metadata={"automation_execution_id": exec_id},
    )
    snap_db_env["cleanup_thread_ids"].append(tid)

    reg_auto = await cg_db.register_automation_run(
        execution_id=exec_id,
        user_id=user_id,
        workspace_id=ws_id,
        automation_name="Daily NVDA Brief",
        entries=["feishu:@me"],
        thread_id=tid,
    )
    assert reg_auto["targets"][0]["ok"] is True

    # Eligibility check discovers DM target from dalton_channel_automation_runs
    ref = ChartRef.of(ws_id, "NVDA", "1day").layer(f"layer-auto-{prefix}")
    async with pool.connection() as conn:
        pending = await ccs.register_pending_chart_snapshots_in_tx(
            conn,
            thread_id=tid,
            run_id=rid,
            user_id=user_id,
            bodies=[(ref, {"id": ref.layer_id, "title": "Auto NVDA"})],
        )
    assert len(pending) == 1

    snap_key = css.build_snapshot_key(tid, VALID_SHA256, ref.layer_id)
    assert await ccs.update_chart_snapshot_status(
        run_id=rid,
        chart_id=ref.chart_id,
        layer_id=ref.layer_id,
        status="ready",
        snapshot_key=snap_key,
        sha256=VALID_SHA256,
        size_bytes=len(VALID_PNG),
        expected_registration_id=pending[0]["registration_id"],
    )

    # Simulate a user follow-up turn on the SAME thread before delayed automation settlement arrives
    _, followup_rid = await _seed_thread_and_response(
        pool,
        ws_id=ws_id,
        user_id=user_id,
        thread_id=tid,
        turn_index=2,
    )
    followup_ref = ChartRef.of(ws_id, "AAPL", "1day").layer(f"layer-followup-{prefix}")
    async with pool.connection() as conn:
        f_pending = await ccs.register_pending_chart_snapshots_in_tx(
            conn,
            thread_id=tid,
            run_id=followup_rid,
            user_id=user_id,
            bodies=[(followup_ref, {"id": followup_ref.layer_id, "title": "Follow-up AAPL"})],
            skip_eligibility_check=True,
        )
    assert len(f_pending) == 1
    followup_snap_key = css.build_snapshot_key(tid, VALID_SHA256, followup_ref.layer_id)
    assert await ccs.update_chart_snapshot_status(
        run_id=followup_rid,
        chart_id=followup_ref.chart_id,
        layer_id=followup_ref.layer_id,
        status="ready",
        snapshot_key=followup_snap_key,
        sha256=VALID_SHA256,
        size_bytes=len(VALID_PNG),
        expected_registration_id=f_pending[0]["registration_id"],
    )

    # finish_automation_run uses settlement's own run_id (NEVER latest turn on thread),
    # persists run_id, and returns ONLY rid's snapshot (excluding follow-up user turn)
    fin = await cg_db.finish_automation_run(
        execution_id=exec_id,
        status="completed",
        thread_id=tid,
        run_id=rid,
        final_text="Daily brief ready.",
        mark_finished=False,
    )
    assert fin["run_id"] == rid
    assert len(fin["chart_snapshots"]) == 1
    assert fin["chart_snapshots"][0]["snapshot_key"] == snap_key

    # Startup recovery of `finishing` automation run returns persisted run_id + chart_snapshots
    rec = await cg_db.recover_channel_state(mode="startup")
    matched_auto = [
        r for r in rec.get("automation_runs", []) if r.get("execution_id") == exec_id
    ]
    assert len(matched_auto) == 1
    assert matched_auto[0]["run_id"] == rid
    assert len(matched_auto[0]["chart_snapshots"]) == 1
    assert matched_auto[0]["chart_snapshots"][0]["snapshot_key"] == snap_key
    assert matched_auto[0]["outbound_blocked"] is False

    # Without run_id OR with an unlinked same-thread candidate run_id (no automation_executions row
    # and no metadata.automation_execution_id), a separate execution MUST reject it and never leak charts!
    other_exec_id = str(uuid4())
    await cg_db.register_automation_run(
        execution_id=other_exec_id,
        user_id=user_id,
        workspace_id=ws_id,
        automation_name="No RunId Check",
        entries=["feishu:@me"],
        thread_id=tid,
    )
    fin_unlinked_rid = await cg_db.finish_automation_run(
        execution_id=other_exec_id,
        status="completed",
        thread_id=tid,
        run_id=followup_rid,
        final_text="Summary only.",
        mark_finished=False,
    )
    assert fin_unlinked_rid["run_id"] is None
    assert fin_unlinked_rid["chart_snapshots"] == []

    third_exec_id = str(uuid4())
    await cg_db.register_automation_run(
        execution_id=third_exec_id,
        user_id=user_id,
        workspace_id=ws_id,
        automation_name="Nil RunId Check",
        entries=["feishu:@me"],
        thread_id=tid,
    )
    fin_no_rid = await cg_db.finish_automation_run(
        execution_id=third_exec_id,
        status="completed",
        thread_id=tid,
        run_id=None,
        final_text="Summary only.",
        mark_finished=False,
    )
    assert fin_no_rid["run_id"] is None
    assert fin_no_rid["chart_snapshots"] == []

    # Claim automation DM charts delivery -> pins dm_recipient_platform_user_id and authorizes @me download
    auto_charts_key = f"{prefix}:automation:{exec_id}:feishu:@me:charts"
    claimed = await cg_db.authorize_and_claim_delivery(
        delivery_key=auto_charts_key,
        kind="automation",
        user_id=user_id,
        thread_id=tid,
        run_id=rid,
        execution_id=exec_id,
        platform="feishu",
        target="feishu:@me",
        has_non_empty_text=False,
        total_chunks=1,
        activity_origin="notification",
    )
    assert claimed["claim_status"] == "claimed"
    assert claimed["dm_recipient_platform_user_id"] == ou_id

    verified = await cg_db.verify_dm_delivery_recipient(
        delivery_key=auto_charts_key,
        delivery_attempt=claimed["delivery_attempt"],
        require_gateway_epoch=False,
    )
    assert verified["authorized"] is True

    png, mime, _fname = await ccs.download_verified_chart_snapshot(
        snapshot_key=snap_key,
        run_id=rid,
        target_address="feishu:@me",
        delivery_key=auto_charts_key,
        delivery_attempt=claimed["delivery_attempt"],
        get_bytes_fn=lambda _k: VALID_PNG,
    )
    assert png == VALID_PNG
    assert mime == "image/png"
