"""Real Postgres integration tests for CH-1 (`d030_channel_gateway` + `d031_channel_session_selection`).

Reads the test database connection string from ``DALTON_CHANNEL_GATEWAY_TEST_DSN``
(or ``DALTON_TEST_DB_DSN`` / ``TEST_DATABASE_URL``); skips cleanly when unset.
A session-scoped fixture upgrades the test database to upstream head and Dalton ``d031``
(skipping if already at head, never dropping ``public`` schema), and each test uses
a unique prefix and cleans up its own rows on teardown.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import sys
from typing import Any
from unittest.mock import create_autospec
from urllib.parse import urlsplit
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from httpx import ASGITransport, AsyncClient
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio
import uvicorn

from src.dalton import invites as dalton_invites
from src.dalton import sharing as dalton_sharing
from src.dalton.app.channels_router import channels_router
from src.dalton.database import channel_gateway as cg_db
from src.server.database import pool as db_pool
from src.server.database import user as user_db
from src.server.database import workspace as workspace_db
from src.server.database.runs import handoff_liveness

pytestmark = pytest.mark.integration

_DSN_ENV_VARS = (
    "DALTON_CHANNEL_GATEWAY_TEST_DSN",
    "DALTON_TEST_DB_DSN",
    "TEST_DATABASE_URL",
)


@pytest.fixture(scope="session", autouse=True)
def dalton_schema():
    """Override directory conftest autouse fixture so unset DSN skips without opening test_db_pool."""
    yield


@pytest.fixture(autouse=True)
def cleanup_dalton_tables():
    """Override directory conftest autouse fixture so unset DSN skips without opening test_db_pool."""
    yield


def _get_test_dsn() -> str:
    for var in _DSN_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            db_name = urlsplit(val).path.lstrip("/")
            if not db_name.endswith("_test"):
                pytest.fail(
                    f"Refusing to run channel gateway integration tests against non-test DB {db_name!r} (from {var})"
                )
            return val

    host = os.getenv("TEST_DB_HOST", "localhost")
    port = os.getenv("TEST_DB_PORT", "5432")
    name = (os.getenv("TEST_DB_NAME", "langalpha_test") or "").strip().lstrip("/")
    if not name.endswith("_test"):
        pytest.fail(
            f"Refusing to run channel gateway integration tests against non-test DB {name!r} (from TEST_DB_NAME)"
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
            f"None of {_DSN_ENV_VARS} is set and default test DB {host}:{port}/{name} is unreachable ({exc}); skipping real Postgres channel gateway integration tests"
        )


def _run_alembic_upgrade_sync(cfg: Config, revision: str) -> None:
    command.upgrade(cfg, revision)


async def _ensure_alembic_upgraded_to_head(dsn: str) -> None:
    """Upgrade upstream and Dalton Alembic chains to head/d031 only if not already there, without dropping schema."""
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
    dalton_target = "d031"

    current_upstream: str | None = None
    current_dalton: str | None = None
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT to_regclass('public.alembic_version')")
            row = await cur.fetchone()
            if row and row[0] is not None:
                await cur.execute("SELECT version_num FROM alembic_version LIMIT 1")
                vrow = await cur.fetchone()
                if vrow:
                    current_upstream = str(vrow[0])

            await cur.execute("SELECT to_regclass('public.dalton_alembic_version')")
            drow = await cur.fetchone()
            if drow and drow[0] is not None:
                await cur.execute(
                    "SELECT version_num FROM dalton_alembic_version LIMIT 1"
                )
                dvrow = await cur.fetchone()
                if dvrow:
                    current_dalton = str(dvrow[0])

    if current_upstream != upstream_head:
        await asyncio.to_thread(_run_alembic_upgrade_sync, upstream_cfg, "head")

    if current_dalton != dalton_target:
        await asyncio.to_thread(_run_alembic_upgrade_sync, dalton_cfg, dalton_target)


@pytest_asyncio.fixture(scope="session")
async def channel_gateway_db_pool():
    """Ensure upstream + Dalton (`d031`) migrations are applied and yield a connection pool."""
    dsn = _get_test_dsn()
    os.environ.setdefault("BYOK_ENCRYPTION_KEY", "test-channel-gateway-encryption-key-32b")

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


async def _cleanup_test_rows(
    pool: AsyncConnectionPool,
    *,
    prefix: str,
    user_ids: list[str],
    workspace_ids: list[str],
    thread_ids: list[str],
) -> None:
    like_pat = f"%{prefix}%"
    ws_uuids = [w for w in workspace_ids if w]
    th_uuids = [t for t in thread_ids if t]
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM dalton_channel_weixin_state
                WHERE owner_user_id = ANY(%s) OR ilink_bot_id LIKE %s
                """,
                (user_ids, like_pat),
            )
            await cur.execute(
                """
                DELETE FROM dalton_channel_thread_watches
                WHERE dalton_user_id = ANY(%s) OR target_address LIKE %s
                """,
                (user_ids, like_pat),
            )
            await cur.execute(
                """
                DELETE FROM dalton_channel_background_runs
                WHERE dalton_user_id = ANY(%s)
                """,
                (user_ids,),
            )
            await cur.execute(
                """
                DELETE FROM dalton_channel_automation_runs
                WHERE user_id = ANY(%s)
                """,
                (user_ids,),
            )
            await cur.execute(
                """
                DELETE FROM dalton_channel_deliveries
                WHERE user_id = ANY(%s) OR delivery_key LIKE %s OR target_address LIKE %s
                """,
                (user_ids, like_pat, like_pat),
            )
            await cur.execute(
                """
                DELETE FROM dalton_channel_inbound_events
                WHERE dalton_user_id = ANY(%s)
                   OR raw_payload->>'platform_user_id' LIKE %s
                   OR message_id LIKE %s
                   OR chat_address LIKE %s
                """,
                (user_ids, like_pat, like_pat, like_pat),
            )
            await cur.execute(
                """
                DELETE FROM dalton_channel_session_generations
                WHERE dalton_user_id = ANY(%s) OR scope_key LIKE %s
                """,
                (user_ids, like_pat),
            )
            await cur.execute(
                """
                DELETE FROM dalton_channel_chat_bindings
                WHERE bound_by_user_id = ANY(%s)
                   OR workspace_id = ANY(%s::uuid[])
                   OR chat_address LIKE %s
                """,
                (user_ids, ws_uuids, like_pat),
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
                """
                DELETE FROM dalton_channel_identities
                WHERE user_id = ANY(%s) OR platform_user_id LIKE %s
                """,
                (user_ids, like_pat),
            )
            await cur.execute(
                """
                DELETE FROM hook_outbox
                WHERE conversation_thread_id IN (
                    SELECT conversation_thread_id FROM conversation_threads
                    WHERE user_id = ANY(%s) OR conversation_thread_id = ANY(%s::uuid[])
                )
                """,
                (user_ids, th_uuids),
            )
            await cur.execute(
                """
                DELETE FROM conversation_responses
                WHERE user_id = ANY(%s) OR conversation_thread_id = ANY(%s::uuid[])
                """,
                (user_ids, th_uuids),
            )
            await cur.execute(
                """
                DELETE FROM conversation_threads
                WHERE user_id = ANY(%s) OR conversation_thread_id = ANY(%s::uuid[])
                """,
                (user_ids, th_uuids),
            )
            if ws_uuids:
                await cur.execute(
                    "DELETE FROM dalton_workspace_sharing WHERE workspace_id = ANY(%s)",
                    (ws_uuids,),
                )
            await cur.execute(
                """
                DELETE FROM workspaces
                WHERE user_id = ANY(%s) OR workspace_id = ANY(%s::uuid[])
                """,
                (user_ids, ws_uuids),
            )
            await cur.execute(
                "DELETE FROM users WHERE user_id = ANY(%s)",
                (user_ids,),
            )


@pytest_asyncio.fixture
async def db_env(
    channel_gateway_db_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
):
    """Route `get_db_connection` to the session pool, seed isolated prefix users/workspaces, and clean up own rows."""

    @asynccontextmanager
    async def _pooled_get_db_connection(existing_conn=None):
        if existing_conn is not None:
            yield existing_conn
            return
        async with channel_gateway_db_pool.connection() as conn:
            yield conn

    mock_gdc = create_autospec(
        db_pool.get_db_connection, side_effect=_pooled_get_db_connection
    )
    monkeypatch.setattr(db_pool, "get_db_connection", mock_gdc)
    monkeypatch.setattr(cg_db, "get_db_connection", mock_gdc)
    monkeypatch.setattr(dalton_sharing, "get_db_connection", mock_gdc)
    monkeypatch.setattr(dalton_invites, "get_db_connection", mock_gdc)
    monkeypatch.setattr(user_db, "get_db_connection", mock_gdc)
    monkeypatch.setattr(workspace_db, "get_db_connection", mock_gdc)

    prefix = f"ch1_{uuid4().hex[:8]}"
    user_a = f"{prefix}_user_a"
    user_b = f"{prefix}_user_b"
    ws_a1 = uuid4()
    ws_a2 = uuid4()
    cleanup_user_ids: list[str] = [user_a, user_b]
    cleanup_workspace_ids: list[str] = [str(ws_a1), str(ws_a2)]
    cleanup_thread_ids: list[str] = []

    async with channel_gateway_db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO users (user_id, email, name, created_at, updated_at)
                VALUES (%s, %s, 'User A', NOW(), NOW()),
                       (%s, %s, 'User B', NOW(), NOW())
                """,
                (user_a, f"{user_a}@example.com", user_b, f"{user_b}@example.com"),
            )
            await cur.execute(
                """
                INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'running', NOW(), NOW()),
                       (%s, %s, %s, 'running', NOW(), NOW())
                """,
                (ws_a1, user_a, f"{prefix}_WS_A1", ws_a2, user_a, f"{prefix}_WS_A2"),
            )

    env: dict[str, Any] = {
        "pool": channel_gateway_db_pool,
        "prefix": prefix,
        "user_a": user_a,
        "user_b": user_b,
        "ws_a1": str(ws_a1),
        "ws_a2": str(ws_a2),
        "cleanup_user_ids": cleanup_user_ids,
        "cleanup_workspace_ids": cleanup_workspace_ids,
        "cleanup_thread_ids": cleanup_thread_ids,
    }
    try:
        yield env
    finally:
        await _cleanup_test_rows(
            channel_gateway_db_pool,
            prefix=prefix,
            user_ids=cleanup_user_ids,
            workspace_ids=cleanup_workspace_ids,
            thread_ids=cleanup_thread_ids,
        )


@pytest.mark.asyncio
async def test_concurrent_bind_code_consumption(db_env: dict[str, Any]) -> None:
    """Concurrent bind-code redemption allows only one caller to succeed; group use invalidates; cross-platform DM keeps code valid."""
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]

    # 1. Group message containing a bind code invalidates the code immediately
    code_group = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    res_group = await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="discord",
        platform_user_id=f"{prefix}_ext_group_user",
        code=code_group["code"],
        is_group=True,
    )
    assert res_group["ok"] is False
    assert res_group["reason"] == "revoked_in_group"

    res_after_group = await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=f"{prefix}_ext_group_user",
        code=code_group["code"],
        is_group=False,
    )
    assert res_after_group["ok"] is False
    assert res_after_group["reason"] == "invalid_or_expired"

    # 2. Cross-platform DM rejects with platform_mismatch without invalidating the code
    code_dm = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    res_mismatch = await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="discord",
        platform_user_id=f"{prefix}_ext_user_1",
        code=code_dm["code"],
        is_group=False,
    )
    assert res_mismatch["ok"] is False
    assert res_mismatch["reason"] == "platform_mismatch"

    # 3. Concurrent DM redemptions on the matching platform: exactly one wins
    results = await asyncio.gather(
        cg_db.consume_bind_code_or_unbind(
            action="bind",
            platform="feishu",
            platform_user_id=f"{prefix}_ext_user_1",
            platform_username="Alice1",
            code=code_dm["code"],
            is_group=False,
        ),
        cg_db.consume_bind_code_or_unbind(
            action="bind",
            platform="feishu",
            platform_user_id=f"{prefix}_ext_user_2",
            platform_username="Alice2",
            code=code_dm["code"],
            is_group=False,
        ),
    )
    oks = sorted(r["ok"] for r in results)
    assert oks == [False, True]
    winner = next(r for r in results if r["ok"] is True)
    loser = next(r for r in results if r["ok"] is False)
    assert winner["user_id"] == user_a
    assert winner["generation"] == 1
    assert loser["reason"] == "invalid_or_expired"


@pytest.mark.asyncio
async def test_concurrent_bind_code_creation_leaves_single_active_code(
    db_env: dict[str, Any],
) -> None:
    """Concurrent `create_bind_code` for the same `(user_id, platform)` serializes under advisory lock and leaves one active code."""
    user_a = db_env["user_a"]
    pool: AsyncConnectionPool = db_env["pool"]

    created = await asyncio.gather(
        cg_db.create_bind_code(user_id=user_a, platform="feishu"),
        cg_db.create_bind_code(user_id=user_a, platform="feishu"),
        cg_db.create_bind_code(user_id=user_a, platform="feishu"),
    )
    assert len({c["code"] for c in created}) == 3

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT code_hash, revoked_reason
                FROM dalton_channel_bind_codes
                WHERE user_id = %s AND platform = 'feishu'
                """,
                (user_a,),
            )
            rows = await cur.fetchall()

    active_rows = [r for r in rows if r["revoked_reason"] is None]
    superseded_rows = [r for r in rows if r["revoked_reason"] == "superseded"]
    assert len(active_rows) == 1
    assert len(superseded_rows) == 2


@pytest.mark.asyncio
async def test_inbound_event_idempotency(db_env: dict[str, Any]) -> None:
    """Concurrent and sequential I-1 calls with the same (platform, message_id) register once and replay idempotently."""
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    ws_a1 = db_env["ws_a1"]
    ou_id = f"{prefix}_ou_1"
    scope_key = f"dm:{ou_id}"
    msg_id = f"{prefix}_om_concurrent_1"

    code_info = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=ou_id,
        code=code_info["code"],
        is_group=False,
    )
    await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        dalton_user_id=user_a,
        chat_type="dm",
        scope_key=scope_key,
        workspace_id=ws_a1,
    )

    first, second = await asyncio.gather(
        cg_db.register_and_resolve_inbound(
            platform="feishu",
            platform_user_id=ou_id,
            chat_type="dm",
            chat_address="feishu:@me",
            thread_scope_key=scope_key,
            message_id=msg_id,
        ),
        cg_db.register_and_resolve_inbound(
            platform="feishu",
            platform_user_id=ou_id,
            chat_type="dm",
            chat_address="feishu:@me",
            thread_scope_key=scope_key,
            message_id=msg_id,
        ),
    )
    existing_statuses = sorted(
        [first["existing_status"] is not None, second["existing_status"] is not None]
    )
    assert existing_statuses == [False, True]

    await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=msg_id,
        status="completed",
    )

    replay = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=ou_id,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope_key,
        message_id=msg_id,
    )
    assert replay["existing_status"] == "completed"
    assert replay["allowed"] is True


@pytest.mark.asyncio
async def test_outbound_delivery_claim_uniqueness(db_env: dict[str, Any]) -> None:
    """Concurrent I-6 claims for the same delivery_key allow only one worker to claim; completed deliveries report already_completed."""
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    thread_id = str(uuid4())
    run_id = str(uuid4())
    ou_id = f"{prefix}_ou_claim"
    scope_key = f"dm:{ou_id}"
    msg_id = f"{prefix}_om_claim_in"
    dlv_key = f"{prefix}_dlv_unique_1"

    code_info = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=ou_id,
        code=code_info["code"],
        is_group=False,
    )
    await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=ou_id,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope_key,
        message_id=msg_id,
    )
    await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=msg_id,
        status="streaming",
        thread_id=thread_id,
        run_id=run_id,
        turn_index=1,
    )

    c1, c2 = await asyncio.gather(
        cg_db.authorize_and_claim_delivery(
            delivery_key=dlv_key,
            kind="tool_send_message",
            user_id=user_a,
            thread_id=thread_id,
            run_id=run_id,
            platform="feishu",
            target="feishu:@me",
            has_non_empty_text=True,
            total_chunks=1,
        ),
        cg_db.authorize_and_claim_delivery(
            delivery_key=dlv_key,
            kind="tool_send_message",
            user_id=user_a,
            thread_id=thread_id,
            run_id=run_id,
            platform="feishu",
            target="feishu:@me",
            has_non_empty_text=True,
            total_chunks=1,
        ),
    )
    assert sorted([c1["claim_status"], c2["claim_status"]]) == [
        "claimed",
        "in_flight_or_unknown",
    ]

    done = await cg_db.update_delivery_progress(
        delivery_key=dlv_key,
        sent_chunks=1,
        total_chunks=1,
        platform_message_ids=["om_sent_1"],
        first_response_json={"message_id": "om_sent_1"},
        status="sent",
    )
    assert done["ok"] is True

    c3 = await cg_db.authorize_and_claim_delivery(
        delivery_key=dlv_key,
        kind="tool_send_message",
        user_id=user_a,
        thread_id=thread_id,
        run_id=run_id,
        platform="feishu",
        target="feishu:@me",
        has_non_empty_text=True,
        total_chunks=1,
    )
    assert c3["claim_status"] == "already_completed"
    assert c3["first_response_json"] == {"message_id": "om_sent_1"}


@pytest.mark.asyncio
async def test_session_generation_advancement(
    db_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind, workspace_switch, /new, and unbind strictly advance session generations and Weixin pgp_sym_encrypt round-trips."""
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    ws_a1 = db_env["ws_a1"]
    ws_a2 = db_env["ws_a2"]
    ou_id = f"{prefix}_ou_gen"
    dm_scope = f"dm:{ou_id}"
    chat_id = f"{prefix}_oc_group_1"
    chat_addr = f"feishu:{chat_id}"
    group_scope = f"chat:{chat_id}"

    monkeypatch.setattr(
        cg_db,
        "get_user_tier",
        create_autospec(cg_db.get_user_tier, return_value="internal"),
    )

    # 1. Bind -> generation = 1
    code_info = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    bound = await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=ou_id,
        code=code_info["code"],
        is_group=False,
    )
    assert bound["ok"] is True
    assert bound["generation"] == 1

    # 2. DM workspace_switch -> generation = 2
    sw1 = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        dalton_user_id=user_a,
        chat_type="dm",
        scope_key=dm_scope,
        workspace_id=ws_a1,
    )
    assert sw1["ok"] is True
    assert sw1["generation"] == 2
    assert sw1["workspace_id"] == ws_a1

    # 3. /new -> generation = 3
    nb1 = await cg_db.execute_session_command(
        cmd="new",
        platform="feishu",
        dalton_user_id=user_a,
        chat_type="dm",
        scope_key=dm_scope,
    )
    assert nb1["ok"] is True
    assert nb1["generation"] == 3

    # 4. Second DM workspace_switch -> generation = 4
    sw2 = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        dalton_user_id=user_a,
        chat_type="dm",
        scope_key=dm_scope,
        workspace_id=ws_a2,
    )
    assert sw2["ok"] is True
    assert sw2["generation"] == 4

    # 5. Unbind -> generation = 5 in `generations` map
    unbound = await cg_db.consume_bind_code_or_unbind(
        action="unbind",
        platform="feishu",
        platform_user_id=ou_id,
    )
    assert unbound["ok"] is True
    assert unbound["generations"][dm_scope] == 5

    # 6. Group workspace_switch advances all scopes in that group chat
    g_sw1 = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        dalton_user_id=user_a,
        chat_type="group",
        chat_address=chat_addr,
        chat_id=chat_id,
        scope_key=group_scope,
        workspace_id=ws_a1,
    )
    assert g_sw1["ok"] is True
    assert g_sw1["generation"] == 2

    g_sw2 = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        dalton_user_id=user_a,
        chat_type="group",
        chat_address=chat_addr,
        chat_id=chat_id,
        scope_key=group_scope,
        workspace_id=ws_a2,
    )
    assert g_sw2["ok"] is True
    assert g_sw2["generation"] == 3

    # 7. Weixin credential pgp_sym_encrypt / pgp_sym_decrypt round-trip
    await cg_db.upsert_weixin_state(
        ilink_bot_id=f"{prefix}_wx_bot_1",
        owner_user_id=user_a,
        bot_token="secret-ilink-token-xyz",
        base_url="https://ilinkai.weixin.qq.com",
        get_updates_buf="buf-001",
        status="active",
        ilink_user_id=f"{prefix}_ilink_u1",
        ilink_username="WxUser",
    )
    wx_state = await cg_db.get_weixin_state(include_token=True)
    assert wx_state["bot_token"] == "secret-ilink-token-xyz"
    assert wx_state["get_updates_buf"] == "buf-001"


@pytest.mark.asyncio
async def test_group_binding_permissions_generations_and_i1_lock(
    db_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decisions 1, 2, 3, 4: current bound identity membership check, settings diff vs global binding, read-only group list, I-1 replay generation, and group lock."""
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    user_b = db_env["user_b"]
    ws_a1 = db_env["ws_a1"]
    ws_a2 = db_env["ws_a2"]
    pool: AsyncConnectionPool = db_env["pool"]

    monkeypatch.setattr(
        cg_db,
        "get_user_tier",
        create_autospec(cg_db.get_user_tier, return_value="internal"),
    )

    async def _allow_ws_write(workspace: Any, user_id: str, **kwargs: Any) -> dict[str, Any]:
        ws_id = workspace.get("workspace_id") if isinstance(workspace, dict) else workspace
        return {"workspace_id": ws_id, "user_id": user_a}

    monkeypatch.setattr(
        cg_db.dalton_sharing,
        "require_workspace_access",
        create_autospec(
            cg_db.dalton_sharing.require_workspace_access,
            side_effect=_allow_ws_write,
        ),
    )

    async def _writable_ws_names(
        user_id: str, extra_workspace_ids: Any = None
    ) -> dict[str, str]:
        return {ws_a1: f"{prefix}_WS_A1", ws_a2: f"{prefix}_WS_A2"}

    monkeypatch.setattr(
        cg_db,
        "_get_writable_workspace_names",
        create_autospec(
            cg_db._get_writable_workspace_names,
            side_effect=_writable_ws_names,
        ),
    )

    ou_a = f"{prefix}_ou_grp_a"
    ou_b = f"{prefix}_ou_grp_b"
    chat_id = f"{prefix}_oc_team_grp"
    chat_addr = f"feishu:{chat_id}"
    main_scope = f"chat:{chat_id}"
    thread_scope = f"chat:{chat_id}:thread:t99"

    # Bind both user_a and user_b on feishu
    code_a = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=ou_a,
        code=code_a["code"],
        is_group=False,
    )
    code_b = await cg_db.create_bind_code(user_id=user_b, platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=ou_b,
        code=code_b["code"],
        is_group=False,
    )

    # 1. Only user_a speaks in the group (plus user_a in a sub-thread); user_b has NOT spoken in the group
    r_a_main = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=ou_a,
        chat_type="group",
        chat_address=chat_addr,
        thread_scope_key=main_scope,
        message_id=f"{prefix}_om_grp_a1",
    )
    assert r_a_main["allowed"] is True
    assert r_a_main["workspace_id"] == cg_db.get_home_workspace_id(user_a)
    assert r_a_main["generation"] == 1

    r_a_sub = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=ou_a,
        chat_type="group",
        chat_address=chat_addr,
        thread_scope_key=thread_scope,
        message_id=f"{prefix}_om_grp_a2",
    )
    assert r_a_sub["generation"] == 1

    # Seed user_b's generation row in that group as if user_b had a prior generation row, but no inbound event
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_channel_session_generations
                    (platform, scope_key, dalton_user_id, generation, updated_at)
                VALUES ('feishu', %s, %s, 1, NOW())
                """,
                (main_scope, user_b),
            )

    def _feishu_settings(chats: dict[str, Any]) -> dict[str, Any]:
        return {
            "feishu": {
                "preferred": "feishu:@me",
                "chats": chats,
                "automation_output": {},
                "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
            }
        }

    # 2. Non-member user_b tries to bind the group -> 403
    state_b = await cg_db.get_channel_internal_state(user_id=user_b)
    assert all(cb["chat_address"] != chat_addr for cb in state_b["chat_bindings"])
    with pytest.raises(HTTPException) as exc_403:
        await cg_db.update_user_channel_settings(
            user_id=user_b,
            version=state_b["settings"]["version"],
            settings=_feishu_settings(
                {
                    chat_addr: {
                        "name": "Team Group",
                        "workspace_id": ws_a1,
                    }
                }
            ),
        )
    assert exc_403.value.status_code == 403

    # 3. Member user_a first-binds the group to ws_a1 -> succeeds, updates dalton_channel_chat_bindings,
    # and keeps personal session generations independent (still 1)
    state_a = await cg_db.get_channel_internal_state(user_id=user_a)
    upd1 = await cg_db.update_user_channel_settings(
        user_id=user_a,
        version=state_a["settings"]["version"],
        settings=_feishu_settings(
            {
                chat_addr: {
                    "name": "Team Group",
                    "workspace_id": ws_a1,
                }
            }
        ),
    )
    assert upd1["version"] == "v2"

    # user_a switches their personal session in main_scope to ws_a1 via I-3 -> advances ONLY user_a's main_scope generation (1 -> 2)
    sw_a_init = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        dalton_user_id=user_a,
        chat_type="group",
        chat_address=chat_addr,
        chat_id=chat_id,
        scope_key=main_scope,
        workspace_id=ws_a1,
    )
    assert sw_a_init["ok"] is True
    assert sw_a_init["generation"] == 2

    # Decision 4: Replaying I-1 for the earlier inbound message after generation advanced to 2
    # returns the creation-time generation == 1 recorded on the row, not the live generation 2
    r_a_main_replay = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=ou_a,
        chat_type="group",
        chat_address=chat_addr,
        thread_scope_key=main_scope,
        message_id=f"{prefix}_om_grp_a1",
    )
    assert r_a_main_replay["existing_status"] == "pending"
    assert r_a_main_replay["generation"] == 1
    assert r_a_main_replay["workspace_id"] == cg_db.get_home_workspace_id(user_a)
    assert r_a_main_replay["external_thread_id"].endswith(":gen:1")

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT scope_key, dalton_user_id, generation
                FROM dalton_channel_session_generations
                WHERE platform = 'feishu' AND (scope_key = %s OR scope_key = %s)
                """,
                (main_scope, thread_scope),
            )
            gen_rows = await cur.fetchall()
    gen_map = {(r["scope_key"], r["dalton_user_id"]): r["generation"] for r in gen_rows}
    assert gen_map[(main_scope, user_a)] == 2
    assert gen_map[(thread_scope, user_a)] == 1
    assert gen_map[(main_scope, user_b)] == 1

    # 4. Concurrent I-1 read and I-3 personal workspace switch for user_a never observe a torn pair
    i1_res, sw_res = await asyncio.gather(
        cg_db.register_and_resolve_inbound(
            platform="feishu",
            platform_user_id=ou_a,
            chat_type="group",
            chat_address=chat_addr,
            thread_scope_key=main_scope,
            message_id=f"{prefix}_om_grp_race",
        ),
        cg_db.execute_session_command(
            cmd="workspace_switch",
            platform="feishu",
            dalton_user_id=user_a,
            chat_type="group",
            chat_address=chat_addr,
            chat_id=chat_id,
            scope_key=main_scope,
            workspace_id=ws_a2,
        ),
    )
    assert sw_res["ok"] is True
    assert (i1_res["generation"], i1_res["workspace_id"]) in {
        (2, ws_a1),
        (3, ws_a2),
    }

    # 5. Decision 2: Suppose global notification binding is updated to ws_a2 while user_a's saved settings still have ws_a1.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_chat_bindings
                SET workspace_id = %s, updated_at = NOW()
                WHERE platform = 'feishu' AND chat_address = %s
                """,
                (ws_a2, chat_addr),
            )

    # (a) Saving automation_output_update (without changing `chats`) does NOT touch global binding ws_a2 or generations.
    upd_auto = await cg_db.update_user_channel_settings(
        user_id=user_a,
        automation_output_update={
            "workspace_id": ws_a1,
            "platform": "feishu",
            "address": "feishu:@me",
        },
    )
    # (a2) Round 4 Decision 2: Read-only `name` disappearing (while user_a's saved workspace_id is still ws_a1)
    # is NOT treated as a routing modification -> does NOT overwrite global binding ws_a2 or advance generations!
    upd_no_name = await cg_db.update_user_channel_settings(
        user_id=user_a,
        version=upd_auto["version"],
        settings=_feishu_settings(
            {
                chat_addr: {
                    "workspace_id": ws_a1,
                }
            }
        ),
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT workspace_id FROM dalton_channel_chat_bindings
                WHERE platform = 'feishu' AND chat_address = %s
                """,
                (chat_addr,),
            )
            cb_after_noname = await cur.fetchone()
            assert cb_after_noname is not None
            assert str(cb_after_noname["workspace_id"]) == ws_a2
            await cur.execute(
                """
                SELECT generation
                FROM dalton_channel_session_generations
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (main_scope, user_a),
            )
            gr_a = await cur.fetchone()
            assert gr_a["generation"] == 3

    # (b) Deleting chat_addr from user_a's settings when global binding is ws_a2 (!= user_a's old setting ws_a1)
    # removes it from user_a's settings ONLY, leaving global binding ws_a2 and generation 3 untouched!
    upd_del_mismatch = await cg_db.update_user_channel_settings(
        user_id=user_a,
        version=upd_no_name["version"],
        settings=_feishu_settings({}),
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT workspace_id FROM dalton_channel_chat_bindings
                WHERE platform = 'feishu' AND chat_address = %s
                """,
                (chat_addr,),
            )
            cb_still = await cur.fetchone()
            assert cb_still is not None
            assert str(cb_still["workspace_id"]) == ws_a2

    # (c) Adding chat_addr -> ws_a2 (matching current global binding ws_a2) in user_a's settings does NOT advance generations (still 3)!
    upd_same_global = await cg_db.update_user_channel_settings(
        user_id=user_a,
        version=upd_del_mismatch["version"],
        settings=_feishu_settings(
            {
                chat_addr: {
                    "name": "Team Group",
                    "workspace_id": ws_a2,
                }
            }
        ),
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT generation
                FROM dalton_channel_session_generations
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (main_scope, user_a),
            )
            gr_a2 = await cur.fetchone()
            assert gr_a2["generation"] == 3

    # (d) Deleting chat_addr from user_a's settings when global binding IS ws_a2 (== user_a's saved setting ws_a2)
    # deletes the global binding row without mutating personal session generations!
    upd2 = await cg_db.update_user_channel_settings(
        user_id=user_a,
        version=upd_same_global["version"],
        settings=_feishu_settings({}),
    )
    assert upd2["version"] == "v7"

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1 FROM dalton_channel_chat_bindings
                WHERE platform = 'feishu' AND chat_address = %s
                """,
                (chat_addr,),
            )
            assert await cur.fetchone() is None
            await cur.execute(
                """
                SELECT generation
                FROM dalton_channel_session_generations
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (main_scope, user_a),
            )
            gr_after_del = await cur.fetchone()
            assert gr_after_del["generation"] == 3

    # (e) Round 4 Decision 1 & 4: Two members concurrently cross-deleting and modifying two groups (chat_addr and chat_addr_2)
    # across two connections acquire group locks in the same sorted order and never deadlock.
    chat_id_2 = f"{prefix}_oc_team_grp_2"
    chat_addr_2 = f"feishu:{chat_id_2}"
    main_scope_2 = f"chat:{chat_id_2}"
    for u_ou, m_sfx in [
        (ou_a, "a_g2"),
        (ou_b, "b_g1"),
        (ou_b, "b_g2"),
    ]:
        target_addr = chat_addr if m_sfx == "b_g1" else chat_addr_2
        target_scope = main_scope if m_sfx == "b_g1" else main_scope_2
        await cg_db.register_and_resolve_inbound(
            platform="feishu",
            platform_user_id=u_ou,
            chat_type="group",
            chat_address=target_addr,
            thread_scope_key=target_scope,
            message_id=f"{prefix}_om_{m_sfx}",
        )
    seed_a = await cg_db.update_user_channel_settings(
        user_id=user_a,
        version=upd2["version"],
        settings=_feishu_settings(
            {
                chat_addr: {"workspace_id": ws_a1},
                chat_addr_2: {"workspace_id": ws_a1},
            }
        ),
    )
    state_b_cur = await cg_db.get_channel_internal_state(user_id=user_b)
    seed_b = await cg_db.update_user_channel_settings(
        user_id=user_b,
        version=state_b_cur["settings"]["version"],
        settings=_feishu_settings(
            {
                chat_addr: {"workspace_id": ws_a1},
                chat_addr_2: {"workspace_id": ws_a1},
            }
        ),
    )
    # user_a deletes chat_addr_2 and modifies chat_addr -> ws_a2;
    # user_b deletes chat_addr and modifies chat_addr_2 -> ws_a2 concurrently across two DB connections.
    cross_a, cross_b = await asyncio.gather(
        cg_db.update_user_channel_settings(
            user_id=user_a,
            version=seed_a["version"],
            settings=_feishu_settings(
                {
                    chat_addr: {"workspace_id": ws_a2},
                }
            ),
        ),
        cg_db.update_user_channel_settings(
            user_id=user_b,
            version=seed_b["version"],
            settings=_feishu_settings(
                {
                    chat_addr_2: {"workspace_id": ws_a2},
                }
            ),
        ),
    )
    assert cross_a["version"] == "v9"
    assert cross_b["version"] == "v3"

    # 6. Decision 1: Rebind user_a to a new platform_user_id that has NOT spoken in chat_addr.
    # Even though dalton_channel_inbound_events has old rows with dalton_user_id == user_a,
    # user_a's new platform identity has not spoken in chat_addr -> 403!
    await cg_db.consume_bind_code_or_unbind(
        action="unbind",
        platform="feishu",
        user_id=user_a,
    )
    code_a_new = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=f"{prefix}_ou_grp_a_rebound",
        code=code_a_new["code"],
        is_group=False,
    )
    # Unbind and rebind each bump the settings version, so re-read it.
    state_a_rebound = await cg_db.get_channel_internal_state(user_id=user_a)
    with pytest.raises(HTTPException) as exc_rebind_403:
        await cg_db.update_user_channel_settings(
            user_id=user_a,
            version=state_a_rebound["settings"]["version"],
            settings=_feishu_settings(
                {
                    chat_addr: {
                        "name": "Team Group",
                        "workspace_id": ws_a1,
                    }
                }
            ),
        )
    assert exc_rebind_403.value.status_code == 403


@pytest.mark.asyncio
async def test_unknown_delivery_blocks_fallback_and_credit_steer_reconcile(
    db_env: dict[str, Any],
) -> None:
    """Decisions 5 & 7: `unknown` delivery sets `has_in_flight_or_unknown_current_text` and blocks `fallback:<run_id>`; credit steer waits for terminal run."""
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    ws_a1 = db_env["ws_a1"]
    pool: AsyncConnectionPool = db_env["pool"]
    ou_id = f"{prefix}_ou_unk"
    dm_scope = f"dm:{ou_id}"

    # Seed real thread and in_progress run in Postgres
    thread_id = str(uuid4())
    run_id = str(uuid4())
    db_env["cleanup_thread_ids"].append(thread_id)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO conversation_threads
                    (conversation_thread_id, workspace_id, user_id, current_status, thread_index, created_at, updated_at)
                VALUES (%s, %s, %s, 'in_progress', 1, NOW(), NOW())
                """,
                (thread_id, ws_a1, user_a),
            )
            await cur.execute(
                """
                INSERT INTO conversation_responses
                    (conversation_response_id, conversation_thread_id, turn_index, status, user_id, created_at)
                VALUES (%s, %s, 1, 'in_progress', %s, NOW())
                """,
                (run_id, thread_id, user_a),
            )

    code_info = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=ou_id,
        code=code_info["code"],
        is_group=False,
    )

    msg_id = f"{prefix}_om_unk_1"
    await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=ou_id,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=dm_scope,
        message_id=msg_id,
    )
    await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=msg_id,
        status="streaming",
        thread_id=thread_id,
        run_id=run_id,
        turn_index=1,
    )

    # Claim a current-turn text delivery and transition it to `unknown` via startup recovery
    dlv_key = f"{prefix}_dlv_unk_1"
    claimed = await cg_db.authorize_and_claim_delivery(
        delivery_key=dlv_key,
        kind="agent_send",
        user_id=user_a,
        thread_id=thread_id,
        run_id=run_id,
        platform="feishu",
        target=None,
        has_non_empty_text=True,
        total_chunks=1,
    )
    assert claimed["claim_status"] == "claimed"

    await cg_db.recover_channel_state(mode="startup")

    prog = await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=msg_id,
        status="streaming",
    )
    assert prog["has_unknown_current_text"] is True
    assert prog["has_in_flight_or_unknown_current_text"] is True

    fb_claim = await cg_db.authorize_and_claim_delivery(
        delivery_key=f"fallback:{run_id}",
        kind="fallback",
        user_id=user_a,
        thread_id=thread_id,
        run_id=run_id,
        platform="feishu",
        target="feishu:@me",
        has_non_empty_text=True,
        total_chunks=1,
    )
    assert fb_claim["claim_status"] == "denied"
    assert fb_claim["code"] == "unknown_delivery_blocks_fallback"

    # Decision 7: credit supplement steer without outbox row stays pending while run is `in_progress`, settles once `completed`
    credit_msg_id = f"{prefix}_om_credit_1"
    credit_req_key = str(uuid4())
    await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=ou_id,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=dm_scope,
        message_id=credit_msg_id,
    )
    await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=credit_msg_id,
        status="streaming",
        thread_id=thread_id,
        run_id=run_id,
        steered_into_run_id=run_id,
        fallback_pending=True,
        raw_payload_patch={
            "credit_steer_request_key": credit_req_key,
            "credit_steer_state": "pending",
        },
    )

    rec_in_prog = await cg_db.reconcile_steer(
        platform="feishu",
        message_id=credit_msg_id,
        run_id=run_id,
    )
    assert rec_in_prog["reconcile_status"] == "still_pending"

    # Transition the run to `completed` (respecting trg_responses_lifecycle_guard)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE conversation_responses
                SET status = 'completed'
                WHERE conversation_response_id = %s
                """,
                (run_id,),
            )

    rec_done = await cg_db.reconcile_steer(
        platform="feishu",
        message_id=credit_msg_id,
        run_id=run_id,
    )
    assert rec_done["reconcile_status"] == "pending_resubmit"
    assert rec_done["outcome"] == "not_running"
    assert rec_done["inbound_status"] == "pending_resubmit"
    assert rec_done["resubmitted_request_key"] is not None


@pytest.mark.asyncio
async def test_handoff_liveness_real_sql_owner_colleague_stranger(
    db_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 1: directly execute `get_owned_runs` and `get_owned_steers` against real Postgres for owner, colleague, and stranger."""
    prefix = db_env["prefix"]
    ws_owner = db_env["user_a"]
    colleague = db_env["user_b"]
    stranger = f"{prefix}_user_stranger"
    ws_id = db_env["ws_a1"]
    pool: AsyncConnectionPool = db_env["pool"]
    db_env["cleanup_user_ids"].append(stranger)

    thread_id = str(uuid4())
    run_id = str(uuid4())
    steer_id = str(uuid4())
    db_env["cleanup_thread_ids"].append(thread_id)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO users (user_id, email, name, created_at, updated_at)
                VALUES (%s, %s, 'Stranger', NOW(), NOW())
                """,
                (stranger, f"{stranger}@example.com"),
            )
            await cur.execute(
                """
                INSERT INTO dalton_workspace_sharing (workspace_id, cos_access, web_access, updated_by, updated_at)
                VALUES (%s, 'rw', 'rw', %s, NOW())
                """,
                (ws_id, ws_owner),
            )
            # Thread created by colleague inside ws_owner's workspace
            await cur.execute(
                """
                INSERT INTO conversation_threads
                    (conversation_thread_id, workspace_id, user_id, current_status, thread_index, created_at, updated_at)
                VALUES (%s, %s, %s, 'in_progress', 1, NOW(), NOW())
                """,
                (thread_id, ws_id, colleague),
            )
            await cur.execute(
                """
                INSERT INTO conversation_responses
                    (conversation_response_id, conversation_thread_id, turn_index, status, user_id, created_at)
                VALUES (%s, %s, 1, 'in_progress', %s, NOW())
                """,
                (run_id, thread_id, colleague),
            )
            await cur.execute(
                """
                INSERT INTO hook_outbox
                    (hook_outbox_id, run_id, conversation_thread_id, hook_type, payload, ordering_key, idempotency_key, status, next_retry_at)
                VALUES (%s, %s, %s, 'steer', '{"target": "thread", "sender": "agent"}'::jsonb, %s, %s, 'pending', 'infinity'::timestamptz)
                """,
                (
                    steer_id,
                    run_id,
                    thread_id,
                    f"{thread_id}:steer",
                    f"{prefix}:steer_idem_1",
                ),
            )

    monkeypatch.setattr(
        handoff_liveness,
        "is_colleague_dispatch_enabled",
        create_autospec(handoff_liveness.is_colleague_dispatch_enabled, return_value=True),
    )

    async def _tier_lookup(uid: str) -> str:
        return "internal" if uid in {ws_owner, colleague} else "external"

    monkeypatch.setattr(
        dalton_sharing,
        "get_user_tier",
        create_autospec(dalton_sharing.get_user_tier, side_effect=_tier_lookup),
    )

    async def _thread_access(
        thread_id_arg: str,
        user_id_arg: str,
        *,
        channel: str = "web",
        need: str = "read",
        owner_id: str | None = None,
        workspace_owner_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        if user_id_arg in {ws_owner, colleague}:
            return {"conversation_thread_id": thread_id_arg}
        raise HTTPException(status_code=403, detail="Forbidden")

    monkeypatch.setattr(
        dalton_sharing,
        "require_thread_access",
        create_autospec(dalton_sharing.require_thread_access, side_effect=_thread_access),
    )

    # 1. Workspace owner can read both the colleague's run and steer in their workspace
    owner_runs = await handoff_liveness.get_owned_runs([run_id], ws_owner)
    assert run_id in owner_runs
    owner_steers = await handoff_liveness.get_owned_steers([steer_id], ws_owner)
    assert steer_id in owner_steers

    # 2. Colleague (the thread owner) can read their own run and steer
    colleague_runs = await handoff_liveness.get_owned_runs([run_id], colleague)
    assert run_id in colleague_runs
    colleague_steers = await handoff_liveness.get_owned_steers([steer_id], colleague)
    assert steer_id in colleague_steers

    # 3. Stranger / external user gets empty results (filtered out in SQL and Python)
    stranger_runs = await handoff_liveness.get_owned_runs([run_id], stranger)
    assert stranger_runs == {}
    stranger_steers = await handoff_liveness.get_owned_steers([steer_id], stranger)
    assert stranger_steers == {}


@pytest.mark.asyncio
async def test_concurrent_unbind_rebind_and_first_bind_vs_first_settings_save(
    db_env: dict[str, Any],
) -> None:
    """Round 6 Decision 1, 2, 3:
    - 10 rounds of concurrent unbind + rebind on the same user across two connections never deadlock or raise, and leave consistent state;
    - concurrent first bind + first settings save when `dalton_channel_user_settings` row does not exist both succeed (via retry on PK collision if needed).
    """
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    user_b = db_env["user_b"]
    pool: AsyncConnectionPool = db_env["pool"]

    # 1. Seed initial binding and settings row for user_a, then run 10 rounds of concurrent unbind + rebind
    init_code = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    init_bind = await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=f"{prefix}_ou_conc_init",
        code=init_code["code"],
        is_group=False,
    )
    assert init_bind["ok"] is True

    for round_idx in range(10):
        code_info = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
        puid_round = f"{prefix}_ou_conc_{round_idx}"
        unbind_res, bind_res = await asyncio.gather(
            cg_db.consume_bind_code_or_unbind(
                action="unbind",
                platform="feishu",
                user_id=user_a,
            ),
            cg_db.consume_bind_code_or_unbind(
                action="bind",
                platform="feishu",
                platform_user_id=puid_round,
                code=code_info["code"],
                is_group=False,
            ),
        )
        assert bind_res["ok"] is True
        assert unbind_res["ok"] in (True, False)

        state_a = await cg_db.get_channel_internal_state(user_id=user_a)
        has_identity = "feishu" in state_a["identities"]
        has_app_settings = "feishu" in state_a["settings"]["apps"]
        assert has_identity == has_app_settings
        if has_identity:
            assert state_a["identities"]["feishu"]["platform_user_id"] == puid_round

    # 2. Ensure user_b has no personal settings row, then concurrently run first bind and first settings save
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM dalton_channel_user_settings WHERE user_id = %s",
                (user_b,),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_identities WHERE user_id = %s",
                (user_b,),
            )

    code_b = await cg_db.create_bind_code(user_id=user_b, platform="feishu")
    first_bind_res, first_save_res = await asyncio.gather(
        cg_db.consume_bind_code_or_unbind(
            action="bind",
            platform="feishu",
            platform_user_id=f"{prefix}_ou_first_b",
            code=code_b["code"],
            is_group=False,
        ),
        cg_db.update_user_channel_settings(
            user_id=user_b,
            settings={},
        ),
    )
    assert first_bind_res["ok"] is True
    assert str(first_save_res["version"]).startswith("v")

    state_b = await cg_db.get_channel_internal_state(user_id=user_b)
    assert state_b["identities"]["feishu"]["platform_user_id"] == f"{prefix}_ou_first_b"


@pytest.mark.asyncio
async def test_ch1_dm_default_home_switch_new_main_ambiguous_and_real_permission(
    db_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Postgres CH-1 DM flow: default Home -> `/workspace_name` (Chinese + spaces) -> `/new` -> `/main`, duplicate names, and real permission checks."""
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    user_b = db_env["user_b"]
    ws_a1 = db_env["ws_a1"]
    pool: AsyncConnectionPool = db_env["pool"]

    # Configure real internal team emails so real `get_user_tier`, `dalton_sharing`, and `_get_writable_workspace_names` run without mocks
    monkeypatch.setenv(
        "DALTON_ALLOWED_EMAILS",
        f"{user_a}@example.com,{user_b}@example.com",
    )
    monkeypatch.setenv("DALTON_SUPABASE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DALTON_COLLEAGUE_DISPATCH_ENABLED", "true")

    ws_cn = uuid4()
    ws_dup_1 = uuid4()
    ws_dup_2 = uuid4()
    ws_b_private = uuid4()
    db_env["cleanup_workspace_ids"].extend(
        [str(ws_cn), str(ws_dup_1), str(ws_dup_2), str(ws_b_private)]
    )

    cn_name = f"{prefix} 量化研究 A组"
    dup_name = f"{prefix} Alpha Research"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'running', NOW(), NOW()),
                       (%s, %s, %s, 'running', NOW(), NOW()),
                       (%s, %s, %s, 'running', NOW(), NOW()),
                       (%s, %s, %s, 'running', NOW(), NOW())
                """,
                (
                    ws_cn,
                    user_a,
                    cn_name,
                    ws_dup_1,
                    user_a,
                    dup_name,
                    ws_dup_2,
                    user_a,
                    dup_name,
                    ws_b_private,
                    user_b,
                    f"{prefix}_Private_B",
                ),
            )

    code_a = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    puid_a = f"{prefix}_ou_dm_a"
    scope_a = f"dm:{puid_a}"
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=puid_a,
        code=code_a["code"],
        is_group=False,
    )

    # Set default notification workspace to ws_a1; DM conversation still starts in Chief of Staff (Home)
    await cg_db.update_user_channel_settings(
        user_id=user_a,
        settings={"default": {"workspace_id": ws_a1}},
    )
    home_ws_a = cg_db.get_home_workspace_id(user_a)

    # 1. First DM message starts in Home (I-1 returns get_home_workspace_id(user_a), override_workspace_id is NULL, generation=1)
    in1 = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope_a,
        message_id=f"{prefix}_m_dm_1",
    )
    assert in1["allowed"] is True
    assert in1["workspace_id"] == home_ws_a
    assert in1["workspace_id"] != ws_a1
    assert in1["generation"] == 1
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT override_workspace_id
                FROM dalton_channel_session_generations
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (scope_a, user_a),
            )
            gen_row_1 = await cur.fetchone()
    assert gen_row_1 is not None
    assert gen_row_1["override_workspace_id"] is None

    # 2. Switch to Chinese + space workspace name via `shortcut_name`
    sw_cn = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_cmd_sw_cn",
        target_kind="shortcut_name",
        workspace_query=f"  {cn_name}  ",
    )
    assert sw_cn["ok"] is True
    assert sw_cn["workspace_id"] == str(ws_cn)
    assert sw_cn["override_workspace_id"] == str(ws_cn)
    assert sw_cn["is_home"] is False
    assert sw_cn["workspace_name"] == cn_name
    assert sw_cn["generation"] == 2

    # 3. Duplicate workspace name returns `workspace_ambiguous` + candidates and keeps session in `ws_cn` (gen 2)
    sw_dup = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_cmd_sw_dup",
        target_kind="shortcut_name",
        workspace_query=dup_name,
    )
    assert sw_dup["ok"] is False
    assert sw_dup["deny_reason"] == "workspace_ambiguous"
    cand_ids = {c["workspace_id"] for c in sw_dup["candidates"]}
    assert cand_ids == {str(ws_dup_1), str(ws_dup_2)}

    # 4. Unauthorized workspace (`ws_b_private`, not shared with `user_a`) returns `workspace_write_denied` and keeps session in `ws_cn` (gen 2)
    sw_unauth = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_cmd_sw_unauth",
        target_kind="workspace_id",
        workspace_id=str(ws_b_private),
    )
    assert sw_unauth["ok"] is False
    assert sw_unauth["deny_reason"] == "workspace_write_denied"

    in2 = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope_a,
        message_id=f"{prefix}_m_dm_2",
    )
    assert in2["workspace_id"] == str(ws_cn)
    assert in2["generation"] == 2

    # 5. `/new` advances generation once inside `ws_cn` (gen 3)
    new_res = await cg_db.execute_session_command(
        cmd="new",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_cmd_new_1",
    )
    assert new_res["ok"] is True
    assert new_res["workspace_id"] == str(ws_cn)
    assert new_res["override_workspace_id"] == str(ws_cn)
    assert new_res["is_home"] is False
    assert new_res["workspace_name"] == cn_name
    assert new_res["generation"] == 3

    # 6. `/main` switches back to Chief of Staff (Home: workspace_id == home_ws_a, override_workspace_id is None, is_home is True, gen 4)
    main_res = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_cmd_main_1",
        target_kind="home",
        workspace_query="home",
    )
    assert main_res["ok"] is True
    assert main_res["workspace_id"] == home_ws_a
    assert main_res["home_workspace_id"] == home_ws_a
    assert main_res["override_workspace_id"] is None
    assert main_res["is_home"] is True
    assert main_res["workspace_name"] == "Chief of Staff"
    assert main_res["generation"] == 4


@pytest.mark.asyncio
async def test_ch1_six_hour_inactivity_before_boundary_after_and_concurrent_workers(
    db_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Postgres 6-hour inactivity: `< 6h` stays in workspace, `== 6h` and `> 6h` reset to Home, historical I-1 replay preserves original routing, and two concurrent workers crossing 6h advance generation only once."""
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    ws_a1 = db_env["ws_a1"]
    ws_a2 = db_env["ws_a2"]
    pool: AsyncConnectionPool = db_env["pool"]
    home_ws_a = cg_db.get_home_workspace_id(user_a)

    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", f"{user_a}@example.com")
    code_a = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    puid_a = f"{prefix}_ou_6h_a"
    scope_a = f"dm:{puid_a}"
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=puid_a,
        code=code_a["code"],
        is_group=False,
    )

    # Switch to ws_a1 (gen 2)
    sw1 = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_6h_sw1",
        target_kind="workspace_id",
        workspace_id=ws_a1,
    )
    assert sw1["ok"] is True
    assert sw1["generation"] == 2

    # 1. Before 6 hours (5h 59m ago): remains in ws_a1 (gen 2)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_session_generations
                SET last_conversation_activity_at = NOW() - INTERVAL '5 hours 59 minutes'
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (scope_a, user_a),
            )

    msg_before = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope_a,
        message_id=f"{prefix}_6h_msg_before",
    )
    assert msg_before["workspace_id"] == ws_a1
    assert msg_before["generation"] == 2

    # 2. Exact 6-hour boundary (`NOW() - INTERVAL '6 hours'`): two concurrent workers cross expiry simultaneously
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_session_generations
                SET last_conversation_activity_at = NOW() - INTERVAL '6 hours'
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (scope_a, user_a),
            )

    w1, w2 = await asyncio.gather(
        cg_db.register_and_resolve_inbound(
            platform="feishu",
            platform_user_id=puid_a,
            chat_type="dm",
            chat_address="feishu:@me",
            thread_scope_key=scope_a,
            message_id=f"{prefix}_6h_conc_1",
        ),
        cg_db.register_and_resolve_inbound(
            platform="feishu",
            platform_user_id=puid_a,
            chat_type="dm",
            chat_address="feishu:@me",
            thread_scope_key=scope_a,
            message_id=f"{prefix}_6h_conc_2",
        ),
    )
    assert w1["workspace_id"] == home_ws_a
    assert w2["workspace_id"] == home_ws_a
    assert w1["workspace_id"] != ws_a1
    # Generation advanced only ONCE (from 2 to 3) across both concurrent workers!
    assert w1["generation"] == 3
    assert w2["generation"] == 3

    # 3. Historical I-1 replay of `msg_before` after expiry still returns original `ws_a1` and `generation == 2`
    replay_before = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope_a,
        message_id=f"{prefix}_6h_msg_before",
    )
    assert replay_before["existing_status"] == "pending"
    assert replay_before["workspace_id"] == ws_a1
    assert replay_before["generation"] == 2

    # 4. Already in Home after > 6h does NOT advance generation
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_session_generations
                SET last_conversation_activity_at = NOW() - INTERVAL '6 hours 5 minutes'
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (scope_a, user_a),
            )

    msg_home_after = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope_a,
        message_id=f"{prefix}_6h_home_after",
    )
    assert msg_home_after["workspace_id"] == home_ws_a
    assert msg_home_after["generation"] == 3

    # 5. Switch to ws_a2 (gen 4), expire > 6h, then `/new` resets to Home and advances generation only ONCE (to gen 5)
    await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_6h_sw2",
        target_kind="workspace_id",
        workspace_id=ws_a2,
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_session_generations
                SET last_conversation_activity_at = NOW() - INTERVAL '7 hours'
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (scope_a, user_a),
            )

    new_after_6h = await cg_db.execute_session_command(
        cmd="new",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_6h_new_after",
    )
    assert new_after_6h["ok"] is True
    assert new_after_6h["workspace_id"] == home_ws_a
    assert new_after_6h["home_workspace_id"] == home_ws_a
    assert new_after_6h["override_workspace_id"] is None
    assert new_after_6h["is_home"] is True
    assert new_after_6h["workspace_name"] == "Chief of Staff"
    assert new_after_6h["generation"] == 5


@pytest.mark.asyncio
async def test_ch1_command_idempotency_late_delivery_group_isolation_and_recovery(
    db_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Postgres: duplicate command idempotency, late/expired/replay delivery activity guards, group per-member session isolation, and command startup recovery."""
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    user_b = db_env["user_b"]
    ws_a1 = db_env["ws_a1"]
    ws_a2 = db_env["ws_a2"]
    pool: AsyncConnectionPool = db_env["pool"]
    home_ws_a = cg_db.get_home_workspace_id(user_a)
    home_ws_b = cg_db.get_home_workspace_id(user_b)

    monkeypatch.setenv(
        "DALTON_ALLOWED_EMAILS",
        f"{user_a}@example.com,{user_b}@example.com",
    )
    puid_a = f"{prefix}_ou_iso_a"
    puid_b = f"{prefix}_ou_iso_b"
    scope_dm_a = f"dm:{puid_a}"
    grp_chat_id = f"{prefix}_oc_grp_iso"
    grp_addr = f"feishu:{grp_chat_id}"
    grp_scope = f"chat:{grp_chat_id}"

    code_a = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    code_b = await cg_db.create_bind_code(user_id=user_b, platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=puid_a,
        code=code_a["code"],
        is_group=False,
    )
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=puid_b,
        code=code_b["code"],
        is_group=False,
    )

    # 1. Concurrent & sequential duplicate `/new` commands with the same `message_id` advance generation only once
    await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope_dm_a,
        message_id=f"{prefix}_iso_init_dm",
    )
    dup_cmd_1, dup_cmd_2 = await asyncio.gather(
        cg_db.execute_session_command(
            cmd="new",
            platform="feishu",
            platform_user_id=puid_a,
            chat_type="dm",
            chat_address="feishu:@me",
            scope_key=scope_dm_a,
            message_id=f"{prefix}_cmd_dup_new",
        ),
        cg_db.execute_session_command(
            cmd="new",
            platform="feishu",
            platform_user_id=puid_a,
            chat_type="dm",
            chat_address="feishu:@me",
            scope_key=scope_dm_a,
            message_id=f"{prefix}_cmd_dup_new",
        ),
    )
    assert dup_cmd_1["generation"] == 2
    assert dup_cmd_2["generation"] == 2
    assert sorted([dup_cmd_1.get("duplicate", False), dup_cmd_2.get("duplicate", False)]) == [
        False,
        True,
    ]

    # Startup recovery includes the unacknowledged command row (`command_reply_pending = TRUE`)
    rec_before = await cg_db.recover_channel_state(mode="startup")
    cmd_rec_rows = [
        r
        for r in rec_before["inbound_events"]
        if r["message_id"] == f"{prefix}_cmd_dup_new"
    ]
    assert len(cmd_rec_rows) == 1
    assert cmd_rec_rows[0]["event_kind"] == "command"
    assert cmd_rec_rows[0]["command_reply_pending"] is True

    await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=f"{prefix}_cmd_dup_new",
        status="completed",
        command_reply_pending=False,
    )
    rec_after = await cg_db.recover_channel_state(mode="startup")
    assert not any(
        r["message_id"] == f"{prefix}_cmd_dup_new" for r in rec_after["inbound_events"]
    )

    # 2. Delivery activity updates: current conversation delivery extends `last_conversation_activity_at`,
    # whereas stale generation, replay/notification, or expired (>= 6h) delivery does NOT extend or revert workspace.
    await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_dm_a,
        message_id=f"{prefix}_cmd_sw_deliv",
        target_kind="workspace_id",
        workspace_id=ws_a1,
    )
    # Current DM session for user_a is now generation 3 in ws_a1.
    in_turn = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope_dm_a,
        message_id=f"{prefix}_msg_turn_g3",
    )
    assert in_turn["generation"] == 3
    tid_g3 = str(uuid4())
    run_g3 = str(uuid4())
    db_env["cleanup_thread_ids"].append(tid_g3)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO conversation_threads (
                    conversation_thread_id, workspace_id, user_id, current_status, thread_index, external_id, platform, created_at, updated_at
                )
                VALUES (%s, %s, %s, 'completed', 0, %s, 'feishu', NOW(), NOW())
                """,
                (tid_g3, ws_a1, user_a, in_turn["external_thread_id"]),
            )
            await cur.execute(
                """
                UPDATE dalton_channel_session_generations
                SET last_conversation_activity_at = NOW() - INTERVAL '2 hours'
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                RETURNING last_conversation_activity_at
                """,
                (scope_dm_a, user_a),
            )
            t_two_hours_ago = (await cur.fetchone())["last_conversation_activity_at"]

    await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=f"{prefix}_msg_turn_g3",
        thread_id=tid_g3,
        run_id=run_g3,
        turn_index=0,
        status="dispatched",
    )
    claim_conv = await cg_db.authorize_and_claim_delivery(
        delivery_key=f"fallback:{run_g3}",
        kind="fallback",
        user_id=user_a,
        thread_id=tid_g3,
        run_id=run_g3,
        platform="feishu",
        target="feishu:@me",
        inbound_message_id=f"{prefix}_msg_turn_g3",
        has_non_empty_text=True,
        total_chunks=1,
        source_scope_key=scope_dm_a,
        source_generation=3,
        activity_origin="conversation",
    )
    assert claim_conv["claim_status"] == "claimed"

    await cg_db.update_delivery_progress(
        delivery_key=f"fallback:{run_g3}",
        status="sent",
        sent_chunks=1,
        total_chunks=1,
        platform_message_ids=["om_out_g3_1"],
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT last_conversation_activity_at
                FROM dalton_channel_session_generations
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (scope_dm_a, user_a),
            )
            t_after_conv = (await cur.fetchone())["last_conversation_activity_at"]
    assert t_after_conv > t_two_hours_ago

    # Late delivery from generation 3 after user switched to generation 4 (`/main`) must NOT update activity or revert workspace
    await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_dm_a,
        message_id=f"{prefix}_cmd_main_g4",
        target_kind="home",
        workspace_query="home",
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_session_generations
                SET last_conversation_activity_at = NOW() - INTERVAL '1 hour'
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                RETURNING last_conversation_activity_at
                """,
                (scope_dm_a, user_a),
            )
            t_g4_fixed = (await cur.fetchone())["last_conversation_activity_at"]

    await cg_db.authorize_and_claim_delivery(
        delivery_key=f"send:{run_g3}:late",
        kind="agent_send",
        user_id=user_a,
        thread_id=tid_g3,
        run_id=run_g3,
        platform="feishu",
        target="feishu:@me",
        inbound_message_id=f"{prefix}_msg_turn_g3",
        has_non_empty_text=True,
        total_chunks=1,
        source_scope_key=scope_dm_a,
        source_generation=3,
        activity_origin="conversation",
    )
    await cg_db.update_delivery_progress(
        delivery_key=f"send:{run_g3}:late",
        status="sent",
        sent_chunks=1,
        total_chunks=1,
        platform_message_ids=["om_out_g3_late"],
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT generation, override_workspace_id, last_conversation_activity_at
                FROM dalton_channel_session_generations
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (scope_dm_a, user_a),
            )
            row_g4 = await cur.fetchone()
    assert row_g4["generation"] == 4
    assert row_g4["override_workspace_id"] is None
    assert row_g4["last_conversation_activity_at"] == t_g4_fixed

    # 3. Group chat two members (`user_a` and `user_b`) have completely isolated workspace selections
    # Even when the group chat notification binding (`dalton_channel_chat_bindings`) points to `ws_a1`,
    # both members start in their own Chief of Staff (Home) workspace.
    member_message_id = f"{prefix}_group_membership"
    await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="group",
        chat_address=grp_addr,
        thread_scope_key=grp_scope,
        message_id=member_message_id,
    )
    await cg_db.update_inbound_progress(
        platform="feishu", message_id=member_message_id, status="completed"
    )
    await cg_db.update_user_channel_settings(
        user_id=user_a,
        settings={
            "feishu": {
                "preferred": "feishu:@me",
                "automation_output": {},
                "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                "chats": {
                    grp_addr: {"name": "Research Group", "workspace_id": ws_a1}
                }
            }
        },
    )
    g_a1 = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="group",
        chat_address=grp_addr,
        thread_scope_key=grp_scope,
        message_id=f"{prefix}_grp_m_a1",
    )
    g_b1 = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_b,
        chat_type="group",
        chat_address=grp_addr,
        thread_scope_key=grp_scope,
        message_id=f"{prefix}_grp_m_b1",
    )
    assert g_a1["workspace_id"] == home_ws_a
    assert g_a1["workspace_id"] != ws_a1
    assert g_b1["workspace_id"] == home_ws_b
    assert g_b1["workspace_id"] != ws_a1

    # `user_a` switches to `ws_a2` in the group; `user_b` stays in Home (generation 1)
    g_sw_a = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="group",
        chat_address=grp_addr,
        chat_id=grp_chat_id,
        scope_key=grp_scope,
        message_id=f"{prefix}_grp_sw_a",
        target_kind="workspace_id",
        workspace_id=ws_a2,
    )
    assert g_sw_a["ok"] is True
    assert g_sw_a["workspace_id"] == ws_a2
    assert g_sw_a["generation"] == 2

    g_b2 = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_b,
        chat_type="group",
        chat_address=grp_addr,
        thread_scope_key=grp_scope,
        message_id=f"{prefix}_grp_m_b2",
    )
    assert g_b2["workspace_id"] == home_ws_b
    assert g_b2["generation"] == 1


@pytest.mark.asyncio
async def test_ch1_stop_pinning_reauth_hitl_source_delivery_immutability_and_concurrent_locks(
    db_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Postgres verification of stop pinning, re-authorization, and delivery locks:
    - `/stop` active `run_id` pinning, immediate `done` when idle, and startup recovery healing of `run_id IS NULL` rows;
    - same-workspace switch (`switched=False`) preserving `generation` & `pending_interrupt`, plus exact case-sensitive name priority;
    - command redelivery/recovery re-authorization after workspace permission revocation, unbind, or rebind to another user;
    - strict `session_interrupt_update` source & generation validation (`generation=None` and `pending_interrupt=None` clear guards);
    - `I-6` spoofed `source_scope_key`/`source_generation` rejection and immutable delivery source fields;
    - `I-7` idempotent `command_effect_status='done'` activity timestamp update;
    - controllable real Postgres concurrency tests for:
      (a) `I-2` unbind + `I-7` `update_inbound_progress` (`session_interrupt_update` + `thread_watch`),
      (b) `I-3` command redelivery + `I-7` `update_inbound_progress` (`command_effect_status='done'`),
      (c) `I-6` `authorize_and_claim_delivery` + `I-7` `update_delivery_progress`.
    """
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    user_b = db_env["user_b"]
    pool: AsyncConnectionPool = db_env["pool"]

    monkeypatch.setenv(
        "DALTON_ALLOWED_EMAILS",
        f"{user_a}@example.com,{user_b}@example.com",
    )
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("DALTON_COLLEAGUE_DISPATCH_ENABLED", "true")

    ws_upper = str(uuid4())
    ws_lower = str(uuid4())
    ws_shared = str(uuid4())
    db_env["cleanup_workspace_ids"].extend([ws_upper, ws_lower, ws_shared])

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'running', NOW(), NOW()),
                       (%s, %s, %s, 'running', NOW(), NOW()),
                       (%s, %s, %s, 'running', NOW(), NOW())
                """,
                (
                    ws_upper,
                    user_a,
                    f"{prefix}_Alpha",
                    ws_lower,
                    user_a,
                    f"{prefix}_alpha",
                    ws_shared,
                    user_b,
                    f"{prefix}_Shared",
                ),
            )
            await cur.execute(
                """
                INSERT INTO dalton_workspace_sharing (
                    workspace_id, cos_access, web_access, updated_by, updated_at
                )
                VALUES (%s, 'rw', 'rw', %s, NOW())
                """,
                (ws_shared, user_b),
            )

    puid_a = f"{prefix}_ou_sec_a"
    scope_a = f"dm:{puid_a}"
    code_a = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=puid_a,
        code=code_a["code"],
        is_group=False,
    )

    # 1. Exact case-sensitive match (`{prefix}_Alpha` vs `{prefix}_alpha`) & same-workspace switch (`switched=False`)
    sw_upper = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_sec_sw_upper",
        target_kind="shortcut_name",
        workspace_query=f"{prefix}_Alpha",
    )
    assert sw_upper["ok"] is True
    assert sw_upper["workspace_id"] == ws_upper
    assert sw_upper["switched"] is True
    assert sw_upper["generation"] == 2

    # Set a pending_interrupt on generation 2, then switch to `{prefix}_Alpha` again -> preserves gen 2 & interrupt
    tid_g2 = str(uuid4())
    rid_g2_1 = str(uuid4())
    db_env["cleanup_thread_ids"].append(tid_g2)
    in_g2 = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope_a,
        message_id=f"{prefix}_sec_in_g2",
    )
    assert in_g2["generation"] == 2

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO conversation_threads (
                    conversation_thread_id, workspace_id, user_id, current_status,
                    thread_index, external_id, platform, created_at, updated_at
                )
                VALUES (%s, %s, %s, 'completed', 1, %s, 'feishu', NOW(), NOW())
                """,
                (tid_g2, ws_upper, user_a, in_g2["external_thread_id"]),
            )
            # Born in_progress, then completed, so `/stop` sees no active run.
            await cur.execute(
                """
                INSERT INTO conversation_responses (
                    conversation_response_id, conversation_thread_id,
                    turn_index, status, user_id, created_at
                )
                VALUES (%s, %s, 1, 'in_progress', %s, NOW())
                """,
                (rid_g2_1, tid_g2, user_a),
            )
            await cur.execute(
                """
                UPDATE conversation_responses
                SET status = 'completed'
                WHERE conversation_response_id = %s
                  AND status = 'in_progress'
                """,
                (rid_g2_1,),
            )

    await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=f"{prefix}_sec_in_g2",
        thread_id=tid_g2,
        run_id=rid_g2_1,
        turn_index=1,
        status="completed",
        session_interrupt_update={
            "platform": "feishu",
            "scope_key": scope_a,
            "dalton_user_id": user_a,
            "generation": 2,
            "thread_id": tid_g2,
            "run_id": rid_g2_1,
            "interrupt_id": "int_g2_keep",
            "pending_interrupt": {
                "interrupt_id": "int_g2_keep",
                "thread_id": tid_g2,
                "run_id": rid_g2_1,
                "generation": 2,
            },
        },
        thread_watch={
            "thread_id": tid_g2,
            "dalton_user_id": user_a,
            "platform": "feishu",
            "target_address": "feishu:@me",
            "scope_key": scope_a,
            "generation": 2,
            "last_seen_turn_index": 1,
            "watch_active": True,
        },
    )

    sw_same = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_sec_sw_same",
        target_kind="shortcut_name",
        workspace_query=f"{prefix}_Alpha",
    )
    assert sw_same["ok"] is True
    assert sw_same["switched"] is False
    assert sw_same["generation"] == 2
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT pending_interrupt
                FROM dalton_channel_session_generations
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (scope_a, user_a),
            )
            s_row = await cur.fetchone()
    assert s_row["pending_interrupt"]["interrupt_id"] == "int_g2_keep"

    # 2. `/stop` when thread has NO active run (`status='completed'`): `run_id=None`, `command_effect_status='done'`
    stop_idle = await cg_db.execute_session_command(
        cmd="stop",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_sec_stop_idle",
    )
    assert stop_idle["ok"] is True
    assert stop_idle["thread_id"] == tid_g2
    assert stop_idle["run_id"] is None
    assert stop_idle["command_effect_status"] == "done"

    # When an active run IS in progress, `/stop` pins `run_id = rid_g2_active` and `command_effect_status = 'pending_cancel'`
    rid_g2_active = str(uuid4())
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO conversation_responses (
                    conversation_response_id, conversation_thread_id,
                    turn_index, status, user_id, created_at
                )
                VALUES (%s, %s, 2, 'in_progress', %s, NOW())
                """,
                (rid_g2_active, tid_g2, user_a),
            )

    stop_active = await cg_db.execute_session_command(
        cmd="stop",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_sec_stop_active",
    )
    assert stop_active["ok"] is True
    assert stop_active["thread_id"] == tid_g2
    assert stop_active["run_id"] == rid_g2_active
    assert stop_active["command_effect_status"] == "pending_cancel"

    # 3. Idempotent `command_effect_status='done'` activity timestamp update + controllable concurrent I-3 replay & I-7 done
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_session_generations
                SET last_conversation_activity_at = NOW() - INTERVAL '30 minutes'
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                RETURNING last_conversation_activity_at
                """,
                (scope_a, user_a),
            )
            t_before_done = (await cur.fetchone())["last_conversation_activity_at"]

    # Concurrent command redelivery and `command_effect_status='done'`.
    await asyncio.gather(
        cg_db.execute_session_command(
            cmd="stop",
            platform="feishu",
            platform_user_id=puid_a,
            chat_type="dm",
            chat_address="feishu:@me",
            scope_key=scope_a,
            message_id=f"{prefix}_sec_stop_active",
        ),
        cg_db.update_inbound_progress(
            platform="feishu",
            message_id=f"{prefix}_sec_stop_active",
            status="completed",
            command_effect_status="done",
            command_reply_pending=False,
        ),
    )

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT last_conversation_activity_at
                FROM dalton_channel_session_generations
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (scope_a, user_a),
            )
            t_after_first_done = (await cur.fetchone())["last_conversation_activity_at"]
            assert t_after_first_done > t_before_done

            # Age activity timestamp and call `command_effect_status='done'` a second time -> must NOT re-extend!
            await cur.execute(
                """
                UPDATE dalton_channel_session_generations
                SET last_conversation_activity_at = NOW() - INTERVAL '20 minutes'
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                RETURNING last_conversation_activity_at
                """,
                (scope_a, user_a),
            )
            t_aged_after_done = (await cur.fetchone())["last_conversation_activity_at"]

    await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=f"{prefix}_sec_stop_active",
        status="completed",
        command_effect_status="done",
        command_reply_pending=False,
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT last_conversation_activity_at
                FROM dalton_channel_session_generations
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (scope_a, user_a),
            )
            t_after_second_done = (await cur.fetchone())["last_conversation_activity_at"]
    assert t_after_second_done == t_aged_after_done

    # 4. Command redelivery & recovery authorization after permission revocation or rebind
    list_1 = await cg_db.execute_session_command(
        cmd="workspace_list",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_sec_list_1",
    )
    assert list_1["ok"] is True
    assert ws_shared in {w["workspace_id"] for w in list_1["workspaces"]}

    sw_shared = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_sec_sw_shared",
        target_kind="workspace_id",
        workspace_id=ws_shared,
    )
    assert sw_shared["ok"] is True
    assert sw_shared["generation"] == 3

    # Revoke `user_a`'s write access to `ws_shared`
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_workspace_sharing
                SET web_access = 'hidden', updated_at = NOW()
                WHERE workspace_id = %s
                """,
                (ws_shared,),
            )

    # Replaying `list_1` filters out `ws_shared`!
    list_replay = await cg_db.execute_session_command(
        cmd="workspace_list",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_sec_list_1",
    )
    assert list_replay["ok"] is True
    assert list_replay["duplicate"] is True
    assert ws_shared not in {w["workspace_id"] for w in list_replay["workspaces"]}

    # Replaying `sw_shared` after write permission was revoked denies with `workspace_write_denied` and marks row ignored
    sw_shared_replay = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_sec_sw_shared",
        target_kind="workspace_id",
        workspace_id=ws_shared,
    )
    assert sw_shared_replay["ok"] is False
    assert sw_shared_replay["deny_reason"] == "workspace_write_denied"
    assert sw_shared_replay["command_reply_pending"] is False

    # 5. Strict `session_interrupt_update` source & generation validation + I-6 spoofing rejection
    sw_back = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_sec_sw_back_g4",
        target_kind="workspace_id",
        workspace_id=ws_upper,
    )
    assert sw_back["generation"] == 4
    tid_g4 = str(uuid4())
    rid_g4 = str(uuid4())
    db_env["cleanup_thread_ids"].append(tid_g4)
    in_g4 = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope_a,
        message_id=f"{prefix}_sec_in_g4",
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO conversation_threads (
                    conversation_thread_id, workspace_id, user_id, current_status,
                    thread_index, external_id, platform, created_at, updated_at
                )
                SELECT %s, %s, %s, 'completed',
                       COALESCE(
                           (
                               SELECT MAX(thread_index)
                               FROM conversation_threads
                               WHERE workspace_id = %s
                           ),
                           -1
                       ) + 1,
                       %s, 'feishu', NOW(), NOW()
                """,
                (tid_g4, ws_upper, user_a, ws_upper, in_g4["external_thread_id"]),
            )
    await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=f"{prefix}_sec_in_g4",
        thread_id=tid_g4,
        run_id=rid_g4,
        turn_index=1,
        status="streaming",
        session_interrupt_update={
            "platform": "feishu",
            "scope_key": scope_a,
            "dalton_user_id": user_a,
            "generation": 4,
            "thread_id": tid_g4,
            "run_id": rid_g4,
            "interrupt_id": "int_g4_cur",
            "pending_interrupt": {
                "interrupt_id": "int_g4_cur",
                "thread_id": tid_g4,
                "run_id": rid_g4,
                "generation": 4,
            },
        },
    )

    # Stale thread `tid_g2` (generation 2) sending `generation=None` MUST NOT clear or overwrite generation 4's interrupt!
    stale_clear = await cg_db.update_inbound_progress(
        session_interrupt_update={
            "platform": "feishu",
            "scope_key": scope_a,
            "dalton_user_id": user_a,
            "generation": None,
            "thread_id": tid_g2,
            "run_id": rid_g2_1,
            "pending_interrupt": None,
        }
    )
    assert stale_clear["session_is_current_for_interrupt"] is False
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT pending_interrupt
                FROM dalton_channel_session_generations
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                """,
                (scope_a, user_a),
            )
            assert (await cur.fetchone())["pending_interrupt"]["interrupt_id"] == "int_g4_cur"

    # Spoofed `source_generation=4` on a delivery for `tid_g2` / `rid_g2_1` (which belongs to generation 2) is denied by I-6!
    spoof_claim = await cg_db.authorize_and_claim_delivery(
        delivery_key=f"fallback:{rid_g2_1}:spoof_g4",
        kind="fallback",
        user_id=user_a,
        thread_id=tid_g2,
        run_id=rid_g2_1,
        platform="feishu",
        target="feishu:@me",
        inbound_message_id=f"{prefix}_sec_in_g2",
        has_non_empty_text=True,
        total_chunks=1,
        source_scope_key=scope_a,
        source_generation=4,
        activity_origin="conversation",
    )
    assert spoof_claim["claim_status"] == "denied"
    assert spoof_claim["code"] == "not_allowed"

    # 6. Concurrent delivery claim and delivery progress on `tid_g4`.
    dkey_conc = f"fallback:{rid_g4}:conc"
    await cg_db.authorize_and_claim_delivery(
        delivery_key=dkey_conc,
        kind="fallback",
        user_id=user_a,
        thread_id=tid_g4,
        run_id=rid_g4,
        platform="feishu",
        target="feishu:@me",
        inbound_message_id=f"{prefix}_sec_in_g4",
        has_non_empty_text=True,
        total_chunks=2,
        source_scope_key=scope_a,
        source_generation=4,
        activity_origin="conversation",
    )
    await asyncio.gather(
        cg_db.authorize_and_claim_delivery(
            delivery_key=dkey_conc,
            kind="fallback",
            user_id=user_a,
            thread_id=tid_g4,
            run_id=rid_g4,
            platform="feishu",
            target="feishu:@me",
            inbound_message_id=f"{prefix}_sec_in_g4",
            has_non_empty_text=True,
            total_chunks=2,
            source_scope_key=scope_a,
            source_generation=4,
            activity_origin="conversation",
        ),
        cg_db.update_delivery_progress(
            delivery_key=dkey_conc,
            status="in_flight",
            sent_chunks=1,
            total_chunks=2,
            platform_message_ids=["om_conc_1"],
        ),
    )

    # 7. Concurrent unbind and inbound progress (`session_interrupt_update` + `thread_watch`).
    # Lock order is `session_generations` before `thread_watches`, with no deadlock.
    await asyncio.gather(
        cg_db.consume_bind_code_or_unbind(
            action="unbind",
            platform="feishu",
            platform_user_id=puid_a,
        ),
        cg_db.update_inbound_progress(
            platform="feishu",
            message_id=f"{prefix}_sec_in_g4",
            status="completed",
            session_interrupt_update={
                "platform": "feishu",
                "scope_key": scope_a,
                "dalton_user_id": user_a,
                "generation": 4,
                "thread_id": tid_g4,
                "run_id": rid_g4,
                "interrupt_id": "int_g4_cur",
                "pending_interrupt": None,
            },
            thread_watch={
                "thread_id": tid_g4,
                "dalton_user_id": user_a,
                "platform": "feishu",
                "target_address": "feishu:@me",
                "scope_key": scope_a,
                "generation": 4,
                "last_seen_turn_index": 2,
                "watch_active": True,
            },
        ),
    )

    # After unbind, replaying `list_1` for `puid_a` fails with `not_linked` and does not leak `user_a`'s workspaces
    list_after_unbind = await cg_db.execute_session_command(
        cmd="workspace_list",
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope_a,
        message_id=f"{prefix}_sec_list_1",
    )
    assert list_after_unbind["ok"] is False
    assert list_after_unbind["linked"] is False
    assert list_after_unbind["deny_reason"] == "not_linked"


@pytest.mark.asyncio
async def test_ch1_failed_conversation_delivery_replays_without_extending_activity(
    db_env: dict[str, Any],
) -> None:
    """A failed conversation delivery can be reclaimed as replay on the same key.

    The same scope and generation are required. The stored origin becomes replay,
    so the recovered send does not move last_conversation_activity_at. A sent or
    partial row with the same user, platform, target, scope, and generation
    returns already_completed and keeps its original origin. A different scope,
    generation, user, platform, or target stays denied. Another origin, or an
    in-flight or unknown row, is not resent.
    """
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    pool: AsyncConnectionPool = db_env["pool"]
    ws_a1 = db_env["ws_a1"]
    scope = f"dm:{prefix}_replay_a"
    puid = f"{prefix}_ou_replay_a"

    code = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=puid,
        code=code["code"],
        is_group=False,
    )
    await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope,
        message_id=f"{prefix}_replay_init",
    )
    switched = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope,
        message_id=f"{prefix}_replay_switch",
        target_kind="workspace_id",
        workspace_id=ws_a1,
    )
    assert switched["ok"] is True
    assert switched["generation"] == 2
    turn = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope,
        message_id=f"{prefix}_replay_turn",
    )
    assert turn["generation"] == 2
    assert turn["workspace_id"] == ws_a1
    tid = str(uuid4())
    run_id = str(uuid4())
    db_env["cleanup_thread_ids"].append(tid)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO conversation_threads (
                    conversation_thread_id, workspace_id, user_id, current_status,
                    thread_index, external_id, platform, created_at, updated_at
                )
                VALUES (%s, %s, %s, 'completed', 0, %s, 'feishu', NOW(), NOW())
                """,
                (tid, ws_a1, user_a, turn["external_thread_id"]),
            )
            await cur.execute(
                """
                UPDATE dalton_channel_session_generations
                SET last_conversation_activity_at = NOW() - INTERVAL '2 hours'
                WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                RETURNING last_conversation_activity_at
                """,
                (scope, user_a),
            )
            aged = (await cur.fetchone())["last_conversation_activity_at"]
    await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=f"{prefix}_replay_turn",
        thread_id=tid,
        run_id=run_id,
        turn_index=0,
        status="dispatched",
    )

    async def _claim(
        key: str,
        *,
        origin: str,
        source_scope: str | None = scope,
        source_generation: int | None = 2,
    ) -> dict[str, Any]:
        return await cg_db.authorize_and_claim_delivery(
            delivery_key=key,
            kind="fallback",
            user_id=user_a,
            thread_id=tid,
            run_id=run_id,
            platform="feishu",
            target="feishu:@me",
            inbound_message_id=f"{prefix}_replay_turn",
            has_non_empty_text=True,
            total_chunks=1,
            source_scope_key=source_scope,
            source_generation=source_generation,
            activity_origin=origin,
        )

    async def _row(key: str) -> dict[str, Any]:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT status, activity_origin, user_id, platform, target_address,
                           source_scope_key, source_generation
                    FROM dalton_channel_deliveries
                    WHERE delivery_key = %s
                    """,
                    (key,),
                )
                return dict(await cur.fetchone())

    async def _activity() -> Any:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT generation,
                           override_workspace_id::text AS override_workspace_id,
                           last_conversation_activity_at
                    FROM dalton_channel_session_generations
                    WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                    """,
                    (scope, user_a),
                )
                return dict(await cur.fetchone())

    recover_key = f"fallback:{run_id}"
    first = await _claim(recover_key, origin="conversation")
    assert first["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=recover_key,
        status="failed",
        sent_chunks=0,
        total_chunks=1,
        error_code="unavailable",
        error_message="send failed",
    )
    assert (await _row(recover_key))["status"] == "failed"
    assert (await _activity())["last_conversation_activity_at"] == aged

    recovered = await _claim(recover_key, origin="replay")
    assert recovered["claim_status"] == "claimed"
    assert recovered["target_address"] == "feishu:@me"
    stored = await _row(recover_key)
    assert stored["status"] == "in_flight"
    assert stored["activity_origin"] == "replay"
    assert stored["user_id"] == user_a
    assert stored["target_address"] == "feishu:@me"
    await cg_db.update_delivery_progress(
        delivery_key=recover_key,
        status="sent",
        sent_chunks=1,
        total_chunks=1,
        platform_message_ids=["om_replay_recovered"],
    )
    after = await _activity()
    assert after["last_conversation_activity_at"] == aged
    assert after["generation"] == 2
    assert after["override_workspace_id"] == ws_a1
    assert (await _row(recover_key))["activity_origin"] == "replay"

    async def _deny_stays(key: str, **claim_kwargs: Any) -> None:
        denied = await _claim(key, **claim_kwargs)
        assert denied["claim_status"] == "denied"
        assert denied["code"] == "not_allowed"
        row = await _row(key)
        assert row["status"] == "failed"
        assert row["activity_origin"] == "conversation"
        assert row["target_address"] == "feishu:@me"

    scope_key = f"fallback:{run_id}:scope"
    assert (await _claim(scope_key, origin="conversation"))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=scope_key, status="failed", sent_chunks=0, total_chunks=1
    )
    await _deny_stays(scope_key, origin="replay", source_scope=f"{scope}:other")

    gen_key = f"fallback:{run_id}:gen"
    assert (await _claim(gen_key, origin="conversation"))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=gen_key, status="failed", sent_chunks=0, total_chunks=1
    )
    await _deny_stays(gen_key, origin="replay", source_generation=9)

    notice_key = f"fallback:{run_id}:notice"
    assert (await _claim(notice_key, origin="conversation"))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=notice_key, status="failed", sent_chunks=0, total_chunks=1
    )
    await _deny_stays(notice_key, origin="notification")

    # Activity was checked above. This sent row is a separate current conversation
    # delivery, so its own progress may move the activity clock. Reclaiming it as
    # replay reports the body already delivered and does not change its origin.
    sent_key = f"fallback:{run_id}:sent"
    assert (await _claim(sent_key, origin="conversation"))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=sent_key,
        status="sent",
        sent_chunks=1,
        total_chunks=1,
        platform_message_ids=["om_replay_sent"],
    )
    replay_sent = await _claim(sent_key, origin="replay")
    assert replay_sent["claim_status"] == "already_completed"
    assert replay_sent["status"] == "sent"
    same_sent = await _claim(sent_key, origin="conversation")
    assert same_sent["claim_status"] == "already_completed"
    sent_row = await _row(sent_key)
    assert sent_row["status"] == "sent"
    assert sent_row["activity_origin"] == "conversation"
    assert sent_row["target_address"] == "feishu:@me"

    partial_key = f"fallback:{run_id}:partial"
    assert (await _claim(partial_key, origin="conversation"))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=partial_key,
        status="partial",
        sent_chunks=1,
        total_chunks=2,
    )
    partial_activity = (await _activity())["last_conversation_activity_at"]
    replay_partial = await _claim(partial_key, origin="replay")
    assert replay_partial["claim_status"] == "already_completed"
    assert replay_partial["status"] == "partial"
    partial_row = await _row(partial_key)
    assert partial_row["status"] == "partial"
    assert partial_row["activity_origin"] == "conversation"
    assert partial_row["target_address"] == "feishu:@me"
    assert partial_row["source_scope_key"] == scope
    assert int(partial_row["source_generation"]) == 2
    assert (await _activity())["last_conversation_activity_at"] == partial_activity

    async def _deny_partial_stays(**claim_kwargs: Any) -> None:
        denied = await cg_db.authorize_and_claim_delivery(
            delivery_key=partial_key,
            kind="fallback",
            user_id=claim_kwargs.pop("user_id", user_a),
            thread_id=tid,
            run_id=run_id,
            platform=claim_kwargs.pop("platform", "feishu"),
            target=claim_kwargs.pop("target", "feishu:@me"),
            inbound_message_id=f"{prefix}_replay_turn",
            has_non_empty_text=True,
            total_chunks=1,
            source_scope_key=claim_kwargs.pop("source_scope", scope),
            source_generation=claim_kwargs.pop("source_generation", 2),
            activity_origin=claim_kwargs.pop("origin", "replay"),
        )
        assert denied["claim_status"] == "denied"
        assert denied["code"] == "not_allowed"
        stayed = await _row(partial_key)
        assert stayed["status"] == "partial"
        assert stayed["activity_origin"] == "conversation"
        assert stayed["target_address"] == "feishu:@me"
        assert stayed["user_id"] == user_a
        assert (await _activity())["last_conversation_activity_at"] == partial_activity

    await _deny_partial_stays(user_id=str(uuid4()))
    await _deny_partial_stays(platform="weixin")
    await _deny_partial_stays(target="feishu:other")
    await _deny_partial_stays(source_scope=f"{scope}:other")
    await _deny_partial_stays(source_generation=9)

    ident_key = f"fallback:{run_id}:ident"
    assert (await _claim(ident_key, origin="conversation"))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=ident_key, status="failed", sent_chunks=0, total_chunks=1
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_deliveries
                SET target_address = 'feishu:tampered'
                WHERE delivery_key = %s
                """,
                (ident_key,),
            )
    ident_denied = await _claim(ident_key, origin="replay")
    assert ident_denied["claim_status"] == "denied"
    assert ident_denied["code"] == "not_allowed"
    ident_row = await _row(ident_key)
    assert ident_row["status"] == "failed"
    assert ident_row["activity_origin"] == "conversation"
    assert ident_row["target_address"] == "feishu:tampered"
    assert ident_row["user_id"] == user_a

    flight_key = f"fallback:{run_id}:flight"
    assert (await _claim(flight_key, origin="conversation"))["claim_status"] == "claimed"
    flight_again = await _claim(flight_key, origin="conversation")
    assert flight_again["claim_status"] == "in_flight_or_unknown"
    assert flight_again["code"] == "in_flight"
    flight_replay = await _claim(flight_key, origin="replay")
    assert flight_replay["claim_status"] == "denied"
    assert flight_replay["code"] == "not_allowed"
    flight_row = await _row(flight_key)
    assert flight_row["status"] == "in_flight"
    assert flight_row["activity_origin"] == "conversation"

    unknown_key = f"fallback:{run_id}:unknown"
    assert (await _claim(unknown_key, origin="conversation"))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=unknown_key, status="unknown", sent_chunks=0, total_chunks=1
    )
    unknown_same = await _claim(unknown_key, origin="conversation")
    assert unknown_same["claim_status"] == "in_flight_or_unknown"
    assert unknown_same["code"] == "in_flight"
    unknown_replay = await _claim(unknown_key, origin="replay")
    assert unknown_replay["claim_status"] == "denied"
    assert unknown_replay["code"] == "not_allowed"
    unknown_row = await _row(unknown_key)
    assert unknown_row["status"] == "unknown"
    assert unknown_row["activity_origin"] == "conversation"
    final = await _activity()
    assert final["generation"] == 2
    assert final["override_workspace_id"] == ws_a1


@pytest.mark.asyncio
async def test_ch1_denied_command_resend_keeps_selection(
    db_env: dict[str, Any],
) -> None:
    """A denied command stays denied when the same message_id is delivered again.

    After the user switches to another workspace and one duplicate name disappears,
    the old event does not change generation, the current workspace, or activity.
    """
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    pool: AsyncConnectionPool = db_env["pool"]
    ws_a1 = db_env["ws_a1"]
    dup_name = f"{prefix} Research"
    ws_dup_1 = str(uuid4())
    ws_dup_2 = str(uuid4())
    db_env["cleanup_workspace_ids"].extend([ws_dup_1, ws_dup_2])
    scope = f"dm:{prefix}_deny_cmd"
    puid = f"{prefix}_ou_deny_cmd"
    ambiguous_id = f"{prefix}_cmd_ambiguous"

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'running', NOW(), NOW()),
                       (%s, %s, %s, 'running', NOW(), NOW())
                """,
                (ws_dup_1, user_a, dup_name, ws_dup_2, user_a, dup_name),
            )
    code = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=puid,
        code=code["code"],
        is_group=False,
    )
    await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid,
        chat_type="dm",
        chat_address="feishu:@me",
        thread_scope_key=scope,
        message_id=f"{prefix}_deny_init",
    )
    denied = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope,
        message_id=ambiguous_id,
        target_kind="shortcut_name",
        workspace_query=dup_name,
    )
    assert denied["ok"] is False
    assert denied["deny_reason"] == "workspace_ambiguous"
    assert denied["duplicate"] is False
    assert {c["workspace_id"] for c in denied["candidates"]} == {ws_dup_1, ws_dup_2}

    chosen = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope,
        message_id=f"{prefix}_cmd_choose_a1",
        target_kind="workspace_id",
        workspace_id=ws_a1,
    )
    assert chosen["ok"] is True
    assert chosen["generation"] == 2
    assert chosen["workspace_id"] == ws_a1

    async def _session() -> dict[str, Any]:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT generation,
                           override_workspace_id::text AS override_workspace_id,
                           last_conversation_activity_at
                    FROM dalton_channel_session_generations
                    WHERE platform = 'feishu' AND scope_key = %s AND dalton_user_id = %s
                    """,
                    (scope, user_a),
                )
                return dict(await cur.fetchone())

    async def _inbound() -> dict[str, Any]:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT status, command_result, command_reply_pending, command_effect_status
                    FROM dalton_channel_inbound_events
                    WHERE platform = 'feishu' AND message_id = %s
                    """,
                    (ambiguous_id,),
                )
                return dict(await cur.fetchone())

    pinned = await _session()
    assert pinned["generation"] == 2
    assert pinned["override_workspace_id"] == ws_a1
    assert (await _inbound())["command_reply_pending"] is True

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE workspaces
                SET name = %s, updated_at = NOW()
                WHERE workspace_id = %s
                """,
                (f"{dup_name} renamed", ws_dup_2),
            )

    resent = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope,
        message_id=ambiguous_id,
        target_kind="shortcut_name",
        workspace_query=dup_name,
    )
    assert resent["ok"] is False
    assert resent["duplicate"] is True
    assert resent["deny_reason"] == "workspace_ambiguous"
    assert resent["command_reply_pending"] is True
    assert {c["workspace_id"] for c in resent["candidates"]} == {ws_dup_1, ws_dup_2}
    assert (await _session()) == pinned
    stored = await _inbound()
    assert stored["status"] == "accepted"
    assert stored["command_reply_pending"] is True
    assert stored["command_effect_status"] == "done"
    assert stored["command_result"]["ok"] is False
    assert stored["command_result"]["deny_reason"] == "workspace_ambiguous"

    await cg_db.update_inbound_progress(
        platform="feishu",
        message_id=ambiguous_id,
        command_reply_pending=False,
    )
    resent_done = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="feishu",
        platform_user_id=puid,
        chat_type="dm",
        chat_address="feishu:@me",
        scope_key=scope,
        message_id=ambiguous_id,
        target_kind="shortcut_name",
        workspace_query=dup_name,
    )
    assert resent_done["ok"] is False
    assert resent_done["duplicate"] is True
    assert resent_done["deny_reason"] == "workspace_ambiguous"
    assert resent_done["command_reply_pending"] is False
    assert (await _session()) == pinned
    assert (await _inbound())["command_reply_pending"] is False


def _create_real_http_test_app(
    pool: AsyncConnectionPool,
    *,
    bootstrap_state: dict[str, Any],
    cleanup_workspace_ids: list[str],
    cleanup_thread_ids: list[str],
) -> FastAPI:
    """Create a FastAPI app mounting real `channels_router` backed by isolated Postgres plus lightweight stub thread turn routes to avoid real LLM calls."""
    app = FastAPI()
    app.include_router(channels_router, prefix="/api/v1/dalton")
    workspace_files: dict[tuple[str, str], tuple[bytes, str]] = {}
    # One-shot turn script. Unscripted threads are not recorded, so their replay stays 404.
    turn_scripts: dict[str, Any] = {"next": None}
    scripted_replays: dict[str, str] = {}

    def _format_sse_page(events: list[tuple[str, dict[str, Any]]]) -> str:
        parts: list[str] = []
        for event_id, (name, data) in enumerate(events, start=1):
            parts.append(
                f"id: {event_id}\nevent: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            )
        return "".join(parts)

    async def _authorize_existing_thread_turn(
        thread_id: str, user_id: str, requested_ws_id: str | None
    ) -> str:
        """Owner, workspace, and live-run checks used by the message route.

        A non-owner follows the message route: when Supabase auth is on, colleague
        write access is checked and then the route still refuses the send. A
        workspace id on the request must match the thread. An in-progress response
        is the running admission conflict.
        """
        from src.dalton.settings import (
            is_colleague_dispatch_enabled,
            is_dalton_supabase_auth_enabled,
        )
        from src.server.database.conversation.threads_read import get_thread_auth_meta
        from src.server.database.runs.lifecycle import get_active_run
        from src.server.handlers.chat.admission_gate import admission_conflict_detail

        meta = await get_thread_auth_meta(thread_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        owner_id = meta.get("user_id")
        if owner_id is not None and str(owner_id) != user_id:
            if is_dalton_supabase_auth_enabled():
                from src.dalton.sharing import require_thread_access

                if not is_colleague_dispatch_enabled():
                    await require_thread_access(
                        thread_id,
                        user_id,
                        channel="web",
                        need="read",
                        owner_id=str(owner_id),
                        workspace_owner_id=meta.get("workspace_owner_id"),
                        workspace_id=meta.get("workspace_id"),
                    )
                    raise HTTPException(status_code=403, detail="该功能尚未开放")
                await require_thread_access(
                    thread_id,
                    user_id,
                    channel="web",
                    need="write",
                    owner_id=str(owner_id),
                    workspace_owner_id=meta.get("workspace_owner_id"),
                    workspace_id=meta.get("workspace_id"),
                )
            raise HTTPException(status_code=403, detail="Forbidden")

        thread_ws = meta.get("workspace_id")
        ws_id = requested_ws_id or ""
        if thread_ws:
            thread_ws_id = str(thread_ws)
            if requested_ws_id and requested_ws_id != thread_ws_id:
                raise HTTPException(
                    status_code=400,
                    detail="workspace_id does not match thread's workspace",
                )
            ws_id = thread_ws_id
        if not ws_id:
            ws_id = cg_db.get_home_workspace_id(user_id)
        if await get_active_run(thread_id) is not None:
            raise HTTPException(
                status_code=409, detail=admission_conflict_detail("running")
            )
        return ws_id

    async def _authorize_thread_status(thread_id: str, user_id: str) -> dict[str, Any]:
        """Same caller check as ``GET /threads/{id}/status`` before the ledger read."""
        from src.dalton.settings import is_dalton_supabase_auth_enabled
        from src.server.database.conversation.threads_read import get_thread_auth_meta

        meta = await get_thread_auth_meta(thread_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        if is_dalton_supabase_auth_enabled():
            from src.dalton.sharing import require_thread_reader

            await require_thread_reader(
                thread_id,
                user_id,
                owner_id=str(meta.get("user_id") or ""),
                workspace_owner_id=meta.get("workspace_owner_id"),
                workspace_id=meta.get("workspace_id"),
            )
        elif str(meta.get("user_id") or "") != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        return meta

    async def _handle_thread_turn(
        request: Request, existing_thread_id: str | None = None
    ) -> StreamingResponse:
        user_id = (request.headers.get("X-User-Id") or "").strip()
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        body = await request.json()
        raw_ws = body.get("workspace_id")
        requested_ws_id = str(raw_ws).strip() if raw_ws else None
        platform = str(body.get("platform") or "feishu")
        ext_id = body.get("external_thread_id")
        msgs = body.get("messages") or []
        user_text = ""
        if msgs and isinstance(msgs[-1], dict):
            c = msgs[-1].get("content")
            user_text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)

        thread_id = existing_thread_id
        if not thread_id and ext_id:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT conversation_thread_id
                        FROM conversation_threads
                        WHERE platform = %s AND external_id = %s AND user_id = %s
                        LIMIT 1
                        """,
                        (platform, ext_id, user_id),
                    )
                    t_row = await cur.fetchone()
                    if t_row:
                        thread_id = str(t_row["conversation_thread_id"])

        # Reject before inserting a workspace, so a forbidden request creates no rows.
        if thread_id:
            ws_id = await _authorize_existing_thread_turn(
                thread_id, user_id, requested_ws_id
            )
        else:
            ws_id = requested_ws_id or cg_db.get_home_workspace_id(user_id)

        # Read after authorization. A rejected turn leaves the one-shot script armed.
        armed_script = turn_scripts.get("next")
        script_kind = (
            str(armed_script.get("kind") or "")
            if isinstance(armed_script, dict)
            else ""
        )

        run_id = str(uuid4())
        turn_index = 1

        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                        VALUES (%s, %s, 'Home', 'running', NOW(), NOW())
                        ON CONFLICT (workspace_id) DO NOTHING
                        """,
                        (ws_id, user_id),
                    )
                    if ws_id not in cleanup_workspace_ids:
                        cleanup_workspace_ids.append(ws_id)

                    if not thread_id:
                        await cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtext(%s))",
                            (f"conversation_thread_index:{ws_id}",),
                        )
                        await cur.execute(
                            """
                            SELECT COALESCE(MAX(thread_index), -1) + 1 AS next_index
                            FROM conversation_threads
                            WHERE workspace_id = %s
                            """,
                            (ws_id,),
                        )
                        next_index = int((await cur.fetchone())["next_index"])
                        thread_id = str(uuid4())
                        await cur.execute(
                            """
                            INSERT INTO conversation_threads (
                                conversation_thread_id, workspace_id, user_id, current_status,
                                thread_index, external_id, platform, created_at, updated_at
                            )
                            VALUES (%s, %s, %s, 'completed', %s, %s, %s, NOW(), NOW())
                            """,
                            (thread_id, ws_id, user_id, next_index, ext_id, platform),
                        )
                    else:
                        await cur.execute(
                            """
                            SELECT thread_index
                            FROM conversation_threads
                            WHERE conversation_thread_id = %s
                            FOR UPDATE
                            """,
                            (thread_id,),
                        )
                        locked = await cur.fetchone()
                        if not locked:
                            raise HTTPException(
                                status_code=404, detail="Thread not found"
                            )
                        await cur.execute(
                            """
                            UPDATE conversation_threads
                            SET current_status = 'completed',
                                updated_at = NOW()
                            WHERE conversation_thread_id = %s
                            """,
                            (thread_id,),
                        )

                    await cur.execute(
                        """
                        SELECT COALESCE(MAX(turn_index), 0) + 1 AS next_turn
                        FROM conversation_responses
                        WHERE conversation_thread_id = %s
                        """,
                        (thread_id,),
                    )
                    turn_index = int((await cur.fetchone())["next_turn"])

                    if thread_id not in cleanup_thread_ids:
                        cleanup_thread_ids.append(thread_id)

                    await cur.execute(
                        """
                        INSERT INTO conversation_responses (
                            conversation_response_id, conversation_thread_id,
                            turn_index, status, user_id, created_at
                        )
                        VALUES (%s, %s, %s, 'in_progress', %s, NOW())
                        """,
                        (run_id, thread_id, turn_index, user_id),
                    )
                    # interrupted is a legal one-step transition from in_progress.
                    final_status = (
                        "interrupted" if script_kind == "interrupt" else "completed"
                    )
                    await cur.execute(
                        """
                        UPDATE conversation_responses
                        SET status = %s
                        WHERE conversation_response_id = %s
                        """,
                        (final_status, run_id),
                    )

        reply_text = f"[stub-reply ws={ws_id} thread={thread_id} turn={turn_index}] {user_text}".strip()
        meta = {
            "thread_id": thread_id,
            "run_id": run_id,
            "turn_index": turn_index,
            "workspace_id": ws_id,
        }
        user_message = {
            "thread_id": thread_id,
            "turn_index": turn_index,
            "run_id": run_id,
            "role": "user",
            "content": user_text,
            "run_completed_at": "2026-01-01T00:00:00Z",
        }
        replay_done = {
            "has_more": False,
            "latest_run_id": run_id,
            "latest_turn_index": turn_index,
        }
        if script_kind == "interrupt" and isinstance(armed_script, dict):
            interrupt_payload = {
                "thread_id": thread_id,
                "run_id": run_id,
                "interrupt_id": str(
                    armed_script.get("interrupt_id") or "int_script_1"
                ),
                "action_requests": [
                    {
                        "type": "ask_user_question",
                        "question": str(
                            armed_script.get("question")
                            or "Which benchmark index should we use?"
                        ),
                        "options": ["CSI 300", "S&P 500"],
                    }
                ],
            }
            live_events = [
                ("metadata", meta),
                ("interrupt", interrupt_payload),
            ]
            scripted_replays[str(thread_id)] = _format_sse_page(
                [
                    ("user_message", user_message),
                    ("interrupt", interrupt_payload),
                    ("replay_done", replay_done),
                ]
            )
            turn_scripts["next"] = None
        elif script_kind == "record_replay":
            chunk = {
                "thread_id": thread_id,
                "turn_index": turn_index,
                "run_id": run_id,
                "agent": "main",
                "id": f"msg_{run_id}",
                "role": "assistant",
                "content": reply_text,
                "content_type": "text",
                "finish_reason": "stop",
            }
            live_events = [
                ("metadata", meta),
                ("message_chunk", chunk),
                (
                    "run_end",
                    {
                        "thread_id": thread_id,
                        "run_id": run_id,
                        "outcome": "completed",
                    },
                ),
            ]
            scripted_replays[str(thread_id)] = _format_sse_page(
                [
                    ("user_message", user_message),
                    ("message_chunk", chunk),
                    ("replay_done", replay_done),
                ]
            )
            turn_scripts["next"] = None
        else:
            live_events = [
                ("metadata", meta),
                (
                    "message_chunk",
                    {
                        "thread_id": thread_id,
                        "agent": "main",
                        "id": f"msg_{run_id}",
                        "role": "assistant",
                        "content": reply_text,
                        "content_type": "text",
                        "finish_reason": "stop",
                    },
                ),
                (
                    "run_end",
                    {
                        "thread_id": thread_id,
                        "run_id": run_id,
                        "outcome": "completed",
                    },
                ),
            ]

        async def _sse_gen():
            # enumerate yields (index, (name, data)); all three branches store that pair.
            for event_id, (name, data) in enumerate(live_events, start=1):
                yield (
                    f"id: {event_id}\nevent: {name}\ndata: "
                    f"{json.dumps(data, ensure_ascii=False)}\n\n"
                )

        return StreamingResponse(_sse_gen(), media_type="text/event-stream")

    @app.post("/api/v1/threads/messages")
    async def create_thread_messages_stub(request: Request) -> StreamingResponse:
        return await _handle_thread_turn(request, existing_thread_id=None)

    @app.post("/api/v1/threads/{thread_id}/messages")
    async def append_thread_messages_stub(
        thread_id: str, request: Request
    ) -> StreamingResponse:
        return await _handle_thread_turn(request, existing_thread_id=thread_id)

    @app.get("/api/v1/threads/{thread_id}/messages/replay")
    async def replay_thread_stub(thread_id: str, request: Request) -> StreamingResponse:
        """Scripted first page only. Unscripted threads and before_turn stay 404."""
        user_id = (request.headers.get("X-User-Id") or "").strip()
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        if request.query_params.get("before_turn") is not None:
            raise HTTPException(status_code=404, detail="Replay page not scripted")
        page = scripted_replays.get(thread_id)
        if not page:
            raise HTTPException(status_code=404, detail="Replay page not scripted")
        await _authorize_thread_status(thread_id, user_id)

        async def _replay_gen():
            yield page

        return StreamingResponse(_replay_gen(), media_type="text/event-stream")

    @app.post("/test-harness/next-turn-script")
    async def test_harness_next_turn_script(request: Request) -> dict[str, Any]:
        body = await request.json()
        kind = str(body.get("kind") or "")
        if kind not in ("interrupt", "record_replay"):
            raise HTTPException(
                status_code=400,
                detail="kind must be interrupt or record_replay",
            )
        turn_scripts["next"] = {
            "kind": kind,
            "interrupt_id": str(body.get("interrupt_id") or "int_script_1"),
            "question": str(
                body.get("question") or "Which benchmark index should we use?"
            ),
        }
        return {"armed": True, "kind": kind}

    cancel_state: dict[str, Any] = {"next_status": 200, "calls": []}

    @app.post("/api/v1/threads/{thread_id}/cancel")
    async def cancel_thread_stub(thread_id: str, request: Request) -> dict[str, Any]:
        run_id = request.query_params.get("run_id")
        status_code = int(cancel_state.get("next_status") or 200)
        cancel_state["calls"].append(
            {
                "thread_id": thread_id,
                "run_id": run_id,
                "status": status_code,
            }
        )
        if status_code >= 400:
            raise HTTPException(
                status_code=status_code,
                detail=f"simulated cancel status {status_code}",
            )
        if run_id:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE conversation_responses SET status = 'cancelled' WHERE conversation_response_id = %s",
                        (run_id,),
                    )
        return {"status": "cancelled", "thread_id": thread_id, "run_id": run_id}

    @app.post("/test-harness/cancel-config")
    async def test_harness_cancel_config(request: Request) -> dict[str, Any]:
        body = await request.json()
        if "next_status" in body:
            cancel_state["next_status"] = int(body["next_status"])
        if body.get("clear_calls"):
            cancel_state["calls"] = []
        return dict(cancel_state)

    @app.post("/test-harness/set-active-run")
    async def test_harness_set_active_run(request: Request) -> dict[str, Any]:
        body = await request.json()
        thread_id = str(body["thread_id"])
        user_id = str(body["user_id"])
        run_id = str(body.get("run_id") or uuid4())
        status = str(body.get("status") or "in_progress")
        async with pool.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT conversation_thread_id FROM conversation_threads "
                    "WHERE conversation_thread_id = %s FOR UPDATE",
                    (thread_id,),
                )
                await cur.execute(
                    """
                    INSERT INTO conversation_responses (
                        conversation_response_id, conversation_thread_id,
                        turn_index, status, user_id, created_at
                    )
                    SELECT %s, %s, COALESCE(MAX(turn_index), 0) + 1, 'in_progress', %s, NOW()
                    FROM conversation_responses WHERE conversation_thread_id = %s
                    ON CONFLICT (conversation_response_id) DO NOTHING
                    """,
                    (run_id, thread_id, user_id, thread_id),
                )
                await cur.execute(
                    "UPDATE conversation_responses SET status = %s "
                    "WHERE conversation_response_id = %s "
                    "AND conversation_thread_id = %s AND user_id = %s",
                    (status, run_id, thread_id, user_id),
                )
        return {"thread_id": thread_id, "run_id": run_id, "status": status}

    @app.post("/test-harness/drop-run")
    async def test_harness_drop_run(request: Request) -> dict[str, Any]:
        """Delete one ledger run so a later liveness read omits that id."""
        body = await request.json()
        thread_id = str(body["thread_id"])
        run_id = str(body["run_id"])
        async with pool.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    DELETE FROM conversation_responses
                    WHERE conversation_response_id = %s
                      AND conversation_thread_id = %s
                    """,
                    (run_id, thread_id),
                )
                deleted = cur.rowcount
        if deleted != 1:
            raise HTTPException(status_code=404, detail="run was not deleted")
        return {"deleted": deleted, "thread_id": thread_id, "run_id": run_id}

    @app.get("/api/v1/threads/dispatches/liveness")
    async def dispatches_liveness(request: Request) -> dict[str, Any]:
        """Owned runs from the ledger. An unknown run id is omitted."""
        from src.server.contracts.status import to_public

        user_id = (request.headers.get("X-User-Id") or "").strip()
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        run_ids = [
            part.strip()
            for part in (request.query_params.get("runs") or "").split(",")
            if part.strip()
        ]
        owned = await handoff_liveness.get_owned_runs(run_ids, user_id)
        runs = [
            {
                "run_id": rid,
                "thread_id": str(row["conversation_thread_id"]),
                "status": to_public(
                    row.get("status"),
                    cancel_requested_at=row.get("cancel_requested_at"),
                ),
            }
            for rid, row in owned.items()
        ]
        return {"liveness": [], "runs": runs, "steers": []}

    @app.get("/api/v1/threads/{thread_id}/status")
    async def status_thread_stub(thread_id: str, request: Request) -> dict[str, Any]:
        """Ledger status for the caller. One latest attempt, without Redis or checkpoints."""
        from src.server.contracts.status import to_public
        from src.server.database.runs.lifecycle import get_latest_attempt

        user_id = (request.headers.get("X-User-Id") or "").strip()
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        meta = await _authorize_thread_status(thread_id, user_id)
        is_non_owner = str(meta.get("user_id") or "") != user_id
        latest = await get_latest_attempt(thread_id)
        run_id = None
        can_reconnect = False
        if latest is not None and latest.get("status") == "in_progress":
            status = to_public(
                latest["status"],
                cancel_requested_at=latest.get("cancel_requested_at"),
            )
            # A non-owner reader sees the public status and not the live run id.
            if not is_non_owner:
                run_id = str(latest["conversation_response_id"])
                can_reconnect = status in ("running", "stopping")
        else:
            status = to_public(latest["status"]) if latest is not None else "idle"
        # conversation_queries is empty in this harness. The response turn is the
        # index the gateway uses when it recovers a turn it has not seen.
        latest_turn_index = None
        if latest is not None and latest.get("turn_index") is not None:
            latest_turn_index = int(latest["turn_index"])
        return {
            "thread_id": thread_id,
            "run_id": run_id,
            "status": status,
            "can_reconnect": can_reconnect,
            "latest_turn_index": latest_turn_index,
            "pending_report_back": False,
            "report_back_run_id": None,
            "recent_report_back_run_ids": [],
            "owed_threads": [],
            "owed_report_kinds": [],
            "active_tasks": [],
            "active_commands": [],
            "pending_steers": [],
        }

    @app.get("/api/v1/threads/{thread_id}/wakeups")
    async def wakeups_thread_stub(thread_id: str) -> dict[str, Any]:
        return {"thread_id": thread_id, "user_timezone": "UTC", "wakeups": []}

    @app.get("/api/v1/threads/{thread_id}/watch")
    async def watch_thread_stub(thread_id: str) -> StreamingResponse:
        async def _watch_gen():
            snap = {
                "thread_id": thread_id,
                "pending_report_back": False,
                "report_back_run_id": None,
                "recent_report_back_run_ids": [],
                "owed_threads": [],
                "active_tasks": [],
                "active_commands": [],
            }
            yield f"event: watch_snapshot\ndata: {json.dumps(snap)}\n\n"

        return StreamingResponse(_watch_gen(), media_type="text/event-stream")

    @app.post("/api/v1/workspaces/home")
    async def ensure_home_stub(request: Request) -> dict[str, Any]:
        user_id = (request.headers.get("X-User-Id") or "").strip()
        home_id = cg_db.get_home_workspace_id(user_id)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                    VALUES (%s, %s, 'Home', 'running', NOW(), NOW())
                    ON CONFLICT (workspace_id) DO NOTHING
                    """,
                    (home_id, user_id),
                )
        if home_id not in cleanup_workspace_ids:
            cleanup_workspace_ids.append(home_id)
        return {"workspace_id": home_id, "status": "running"}

    @app.post("/api/v1/workspaces/{workspace_id}/start")
    async def start_workspace_stub(workspace_id: str) -> dict[str, Any]:
        return {"workspace_id": workspace_id, "status": "running"}

    @app.get("/test-harness/bootstrap")
    async def test_harness_bootstrap() -> dict[str, Any]:
        return bootstrap_state

    @app.post("/test-harness/age-session")
    async def test_harness_age_session(request: Request) -> dict[str, Any]:
        body = await request.json()
        platform = str(body["platform"])
        scope_key = str(body["scope_key"])
        dalton_user_id = str(body["dalton_user_id"])
        seconds_ago = int(body["seconds_ago"])
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_channel_session_generations
                    SET last_conversation_activity_at = clock_timestamp() - (%s * INTERVAL '1 second')
                    WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
                    RETURNING generation,
                              override_workspace_id::text AS override_workspace_id,
                              EXTRACT(EPOCH FROM last_conversation_activity_at)::double precision AS activity_epoch
                    """,
                    (seconds_ago, platform, scope_key, dalton_user_id),
                )
                row = await cur.fetchone()
        return dict(row) if row else {}

    @app.post("/api/v1/workspaces/{workspace_id}/files/upload")
    async def upload_workspace_file(
        workspace_id: str,
        request: Request,
        file: UploadFile = File(...),
    ) -> dict[str, str]:
        dest = (request.query_params.get("path") or file.filename or "").strip()
        if not dest:
            raise HTTPException(status_code=400, detail="Destination path is required")
        content = await file.read()
        mime = file.content_type or "application/octet-stream"
        workspace_files[(workspace_id, dest)] = (content, mime)
        return {"workspace_id": workspace_id, "path": dest}

    @app.get("/api/v1/workspaces/{workspace_id}/files/download")
    async def download_workspace_file(workspace_id: str, path: str) -> Response:
        stored = workspace_files.get((workspace_id, path))
        if stored is None:
            raise HTTPException(status_code=404, detail="File not found")
        content, mime = stored
        return Response(content=content, media_type=mime)

    @app.get("/test-harness/inspect")
    async def test_harness_inspect(
        platform: str = "feishu",
        scope_key: str | None = None,
        dalton_user_id: str | None = None,
        message_id: str | None = None,
        workspace_id: str | None = None,
        delivery_key: str | None = None,
    ) -> dict[str, Any]:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                session_row = None
                if scope_key and dalton_user_id:
                    await cur.execute(
                        """
                        SELECT generation,
                               override_workspace_id::text AS override_workspace_id,
                               EXTRACT(EPOCH FROM last_conversation_activity_at)::double precision AS activity_epoch,
                               EXTRACT(EPOCH FROM selection_initialized_at)::double precision AS initialized_epoch
                        FROM dalton_channel_session_generations
                        WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
                        """,
                        (platform, scope_key, dalton_user_id),
                    )
                    r = await cur.fetchone()
                    if r:
                        session_row = dict(r)

                inbound_row = None
                if message_id:
                    await cur.execute(
                        """
                        SELECT message_id,
                               event_kind,
                               status,
                               dalton_user_id,
                               workspace_id::text AS workspace_id,
                               external_thread_id,
                               session_generation,
                               thread_id::text AS thread_id,
                               run_id,
                               command_request,
                               command_result,
                               command_effect_status,
                               command_reply_pending,
                               fallback_pending
                        FROM dalton_channel_inbound_events
                        WHERE platform = %s AND message_id = %s
                        """,
                        (platform, message_id),
                    )
                    ir = await cur.fetchone()
                    if ir:
                        inbound_row = dict(ir)

                threads: list[dict[str, Any]] = []
                if workspace_id:
                    await cur.execute(
                        """
                        SELECT conversation_thread_id::text AS thread_id,
                               thread_index,
                               external_id
                        FROM conversation_threads
                        WHERE workspace_id = %s
                        ORDER BY thread_index, conversation_thread_id
                        """,
                        (workspace_id,),
                    )
                    for trow in await cur.fetchall():
                        await cur.execute(
                            """
                            SELECT turn_index
                            FROM conversation_responses
                            WHERE conversation_thread_id = %s
                            ORDER BY turn_index
                            """,
                            (trow["thread_id"],),
                        )
                        threads.append(
                            {
                                "thread_id": trow["thread_id"],
                                "thread_index": int(trow["thread_index"]),
                                "external_id": trow["external_id"],
                                "turn_indexes": [
                                    int(turn["turn_index"])
                                    for turn in await cur.fetchall()
                                ],
                            }
                        )

                delivery_row = None
                if delivery_key:
                    await cur.execute(
                        """
                        SELECT delivery_key,
                               kind,
                               status,
                               activity_origin,
                               source_scope_key,
                               source_generation
                        FROM dalton_channel_deliveries
                        WHERE delivery_key = %s
                        """,
                        (delivery_key,),
                    )
                    drow = await cur.fetchone()
                    if drow:
                        delivery_row = dict(drow)
                        if delivery_row.get("source_generation") is not None:
                            delivery_row["source_generation"] = int(
                                delivery_row["source_generation"]
                            )

        return {
            "session": session_row,
            "inbound": inbound_row,
            "threads": threads,
            "delivery": delivery_row,
        }

    return app


@asynccontextmanager
async def _run_ephemeral_fastapi_server(app: FastAPI):
    """Start `uvicorn.Server` on an ephemeral local port `127.0.0.1:0` and guarantee shutdown in `finally`."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = int(sock.getsockname()[1])

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    serve_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started and not serve_task.done():
            await asyncio.sleep(0.01)
        if serve_task.done():
            serve_task.result()
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except Exception:
            serve_task.cancel()
        finally:
            sock.close()


async def _seed_real_http_e2e_fixture(
    pool: AsyncConnectionPool,
    *,
    prefix: str,
    user_a: str,
    user_b: str,
    ws_a1: str,
    ws_a2: str,
    cleanup_workspace_ids: list[str],
) -> dict[str, Any]:
    """Seed users, workspaces (Chinese + space, duplicate names, literal 'main', private B), bindings, and default notification settings."""
    service_token = "0123456789abcdef0123456789abcdef_real_http_token"
    os.environ["INTERNAL_SERVICE_TOKEN"] = service_token
    os.environ["DALTON_COLLEAGUE_DISPATCH_ENABLED"] = "true"
    os.environ["DALTON_ALLOWED_EMAILS"] = f"{user_a}@example.com,{user_b}@example.com"

    ws_cn = str(uuid4())
    ws_dup_1 = str(uuid4())
    ws_dup_2 = str(uuid4())
    ws_named_main = str(uuid4())
    ws_b_private = str(uuid4())
    cleanup_workspace_ids.extend(
        [ws_cn, ws_dup_1, ws_dup_2, ws_named_main, ws_b_private]
    )

    cn_name = f"{prefix} 量化研究 A组"
    dup_name = f"{prefix} 重复工作区"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'running', NOW(), NOW()),
                       (%s, %s, %s, 'running', NOW(), NOW()),
                       (%s, %s, %s, 'running', NOW(), NOW()),
                       (%s, %s, 'main', 'running', NOW(), NOW()),
                       (%s, %s, %s, 'running', NOW(), NOW())
                """,
                (
                    ws_cn,
                    user_a,
                    cn_name,
                    ws_dup_1,
                    user_a,
                    dup_name,
                    ws_dup_2,
                    user_a,
                    dup_name,
                    ws_named_main,
                    user_a,
                    ws_b_private,
                    user_b,
                    f"{prefix}_Private_B",
                ),
            )

    puid_a = f"{prefix}_ou_http_a"
    puid_b = f"{prefix}_ou_http_b"
    grp_chat_id = f"{prefix}_oc_grp_http"
    grp_addr = f"feishu:{grp_chat_id}"
    grp_scope = f"chat:{grp_chat_id}"

    code_a = await cg_db.create_bind_code(user_id=user_a, platform="feishu")
    code_b = await cg_db.create_bind_code(user_id=user_b, platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=puid_a,
        code=code_a["code"],
        is_group=False,
    )
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id=puid_b,
        code=code_b["code"],
        is_group=False,
    )

    # Configure both default notification workspace and group notification binding to `ws_a1`;
    # conversations must still default to Chief of Staff (Home)!
    member_message_id = f"{prefix}_group_membership"
    await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id=puid_a,
        chat_type="group",
        chat_address=grp_addr,
        thread_scope_key=grp_scope,
        message_id=member_message_id,
    )
    await cg_db.update_inbound_progress(
        platform="feishu", message_id=member_message_id, status="completed"
    )
    await cg_db.update_user_channel_settings(
        user_id=user_a,
        settings={
            "default": {"workspace_id": ws_a1},
            "feishu": {
                "preferred": "feishu:@me",
                "automation_output": {},
                "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                "chats": {
                    grp_addr: {"name": "HTTP E2E Group", "workspace_id": ws_a1}
                }
            },
        },
    )

    return {
        "prefix": prefix,
        "service_token": service_token,
        "user_a": user_a,
        "user_b": user_b,
        "home_ws_a": cg_db.get_home_workspace_id(user_a),
        "home_ws_b": cg_db.get_home_workspace_id(user_b),
        "ws_a1": ws_a1,
        "ws_a2": ws_a2,
        "ws_cn": ws_cn,
        "cn_name": cn_name,
        "ws_dup_1": ws_dup_1,
        "ws_dup_2": ws_dup_2,
        "dup_name": dup_name,
        "ws_named_main": ws_named_main,
        "ws_b_private": ws_b_private,
        "puid_a": puid_a,
        "puid_b": puid_b,
        "grp_chat_id": grp_chat_id,
        "grp_addr": grp_addr,
        "grp_scope": grp_scope,
    }


@pytest.mark.asyncio
async def test_ch1_harness_thread_routes_keep_access_workspace_and_run_checks(
    db_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thread message and status routes keep the caller's access, workspace, and run state.

    Another user's thread, a workspace that does not match the thread, and a thread
    with an in-progress response are refused. The owner's idle thread in its own
    workspace still accepts a turn. Status comes from the latest response, not a
    fixed idle value.
    """
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    user_b = db_env["user_b"]
    pool: AsyncConnectionPool = db_env["pool"]
    ws_a1 = db_env["ws_a1"]
    ws_a2 = db_env["ws_a2"]
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("DALTON_COLLEAGUE_DISPATCH_ENABLED", "true")
    monkeypatch.setenv(
        "DALTON_ALLOWED_EMAILS",
        f"{user_a}@example.com,{user_b}@example.com",
    )

    tid_idle = str(uuid4())
    tid_running = str(uuid4())
    rid_idle = str(uuid4())
    rid_running = str(uuid4())
    ext_running = f"{prefix}_ext_running"
    db_env["cleanup_thread_ids"].extend([tid_idle, tid_running])

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COALESCE(MAX(thread_index), -1) + 1 AS next_index
                FROM conversation_threads
                WHERE workspace_id = %s
                """,
                (ws_a1,),
            )
            next_index = int((await cur.fetchone())["next_index"])
            await cur.execute(
                """
                INSERT INTO conversation_threads (
                    conversation_thread_id, workspace_id, user_id, current_status,
                    thread_index, external_id, platform, created_at, updated_at
                )
                VALUES (%s, %s, %s, 'completed', %s, %s, 'feishu', NOW(), NOW()),
                       (%s, %s, %s, 'completed', %s, %s, 'feishu', NOW(), NOW())
                """,
                (
                    tid_idle,
                    ws_a1,
                    user_a,
                    next_index,
                    f"{prefix}_ext_idle",
                    tid_running,
                    ws_a1,
                    user_a,
                    next_index + 1,
                    ext_running,
                ),
            )
            await cur.execute(
                """
                INSERT INTO conversation_responses (
                    conversation_response_id, conversation_thread_id,
                    turn_index, status, user_id, created_at
                )
                VALUES (%s, %s, 1, 'in_progress', %s, NOW()),
                       (%s, %s, 1, 'in_progress', %s, NOW())
                """,
                (rid_idle, tid_idle, user_a, rid_running, tid_running, user_a),
            )
            await cur.execute(
                """
                UPDATE conversation_responses
                SET status = 'completed'
                WHERE conversation_response_id = %s AND status = 'in_progress'
                """,
                (rid_idle,),
            )

    async def _response_count(thread_id: str) -> int:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM conversation_responses
                    WHERE conversation_thread_id = %s
                    """,
                    (thread_id,),
                )
                return int((await cur.fetchone())["n"])

    async def _thread_index(thread_id: str) -> int:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT thread_index
                    FROM conversation_threads
                    WHERE conversation_thread_id = %s
                    """,
                    (thread_id,),
                )
                return int((await cur.fetchone())["thread_index"])

    idle_index = await _thread_index(tid_idle)
    app = _create_real_http_test_app(
        pool,
        bootstrap_state={"prefix": prefix},
        cleanup_workspace_ids=db_env["cleanup_workspace_ids"],
        cleanup_thread_ids=db_env["cleanup_thread_ids"],
    )

    def _messages(workspace_id: str, external_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "workspace_id": workspace_id,
            "platform": "feishu",
            "messages": [{"role": "user", "content": "hello"}],
        }
        if external_id:
            body["external_thread_id"] = external_id
        return body

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing_user = await client.post(
            f"/api/v1/threads/{tid_idle}/messages",
            json=_messages(ws_a1),
        )
        assert missing_user.status_code == 401
        assert await _response_count(tid_idle) == 1

        other_user = await client.post(
            f"/api/v1/threads/{tid_idle}/messages",
            headers={"X-User-Id": user_b},
            json=_messages(ws_a1),
        )
        assert other_user.status_code in (403, 404)
        assert await _response_count(tid_idle) == 1

        wrong_ws = await client.post(
            f"/api/v1/threads/{tid_idle}/messages",
            headers={"X-User-Id": user_a},
            json=_messages(ws_a2),
        )
        assert wrong_ws.status_code == 400
        assert wrong_ws.json()["detail"] == (
            "workspace_id does not match thread's workspace"
        )
        assert await _response_count(tid_idle) == 1
        assert await _thread_index(tid_idle) == idle_index

        running = await client.post(
            f"/api/v1/threads/{tid_running}/messages",
            headers={"X-User-Id": user_a},
            json=_messages(ws_a1),
        )
        assert running.status_code == 409
        assert running.json()["detail"]["code"] == "running"
        assert await _response_count(tid_running) == 1

        reused = await client.post(
            "/api/v1/threads/messages",
            headers={"X-User-Id": user_a},
            json=_messages(ws_a1, ext_running),
        )
        assert reused.status_code == 409
        assert reused.json()["detail"]["code"] == "running"
        assert await _response_count(tid_running) == 1

        accepted = await client.post(
            f"/api/v1/threads/{tid_idle}/messages",
            headers={"X-User-Id": user_a},
            json=_messages(ws_a1),
        )
        assert accepted.status_code == 200
        assert await _response_count(tid_idle) == 2
        assert await _thread_index(tid_idle) == idle_index

        no_status_user = await client.get(f"/api/v1/threads/{tid_running}/status")
        assert no_status_user.status_code == 401

        other_status = await client.get(
            f"/api/v1/threads/{tid_running}/status",
            headers={"X-User-Id": user_b},
        )
        assert other_status.status_code in (403, 404)

        live = await client.get(
            f"/api/v1/threads/{tid_running}/status",
            headers={"X-User-Id": user_a},
        )
        assert live.status_code == 200
        live_body = live.json()
        assert live_body["status"] == "running"
        assert live_body["run_id"] == rid_running
        assert live_body["can_reconnect"] is True
        assert live_body["latest_turn_index"] == 1

        settled = await client.get(
            f"/api/v1/threads/{tid_idle}/status",
            headers={"X-User-Id": user_a},
        )
        assert settled.status_code == 200
        settled_body = settled.json()
        assert settled_body["status"] == "completed"
        assert settled_body["run_id"] is None
        assert settled_body["latest_turn_index"] == 2


@pytest.mark.asyncio
async def test_ch1_real_http_gateway_orchestrator_to_fastapi_and_postgres(
    db_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end real HTTP test: FeishuChannelAdapter -> ChannelOrchestrator -> real HttpBackendClient -> live FastAPI channels_router -> isolated Postgres."""
    pool: AsyncConnectionPool = db_env["pool"]
    bootstrap = await _seed_real_http_e2e_fixture(
        pool,
        prefix=db_env["prefix"],
        user_a=db_env["user_a"],
        user_b=db_env["user_b"],
        ws_a1=db_env["ws_a1"],
        ws_a2=db_env["ws_a2"],
        cleanup_workspace_ids=db_env["cleanup_workspace_ids"],
    )
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", bootstrap["service_token"])
    monkeypatch.setenv(
        "DALTON_ALLOWED_EMAILS",
        f"{bootstrap['user_a']}@example.com,{bootstrap['user_b']}@example.com",
    )

    app = _create_real_http_test_app(
        pool,
        bootstrap_state=bootstrap,
        cleanup_workspace_ids=db_env["cleanup_workspace_ids"],
        cleanup_thread_ids=db_env["cleanup_thread_ids"],
    )

    gateway_dir = (
        Path(__file__).resolve().parents[3]
        / "integrations"
        / "dalton-channel-gateway"
    )
    vitest_bin = gateway_dir / "node_modules" / ".bin" / "vitest"

    async with _run_ephemeral_fastapi_server(app) as base_url:
        child_env = {
            **os.environ,
            "DALTON_REAL_BACKEND_URL": base_url,
            "DALTON_REAL_SERVICE_TOKEN": bootstrap["service_token"],
        }
        proc = await asyncio.create_subprocess_exec(
            str(vitest_bin),
            "run",
            "test/real-http-postgres.test.ts",
            "--pool=forks",
            "--poolOptions.forks.singleFork=true",
            cwd=str(gateway_dir),
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=150.0)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

        output = stdout_bytes.decode("utf-8", errors="replace")
        assert proc.returncode == 0, f"Vitest real HTTP test failed:\n{output}"


async def _serve_cli_harness_main() -> None:
    """Standalone CLI entrypoint (`--serve-http-harness`) used when running Vitest directly with `DALTON_CHANNEL_GATEWAY_TEST_DSN`."""
    dsn = _get_test_dsn()
    os.environ.setdefault(
        "BYOK_ENCRYPTION_KEY", "test-channel-gateway-encryption-key-32b"
    )
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

    @asynccontextmanager
    async def _pooled_get_db_connection(existing_conn=None):
        if existing_conn is not None:
            yield existing_conn
            return
        async with pool.connection() as conn:
            yield conn

    for mod in (db_pool, cg_db, dalton_sharing, dalton_invites, user_db, workspace_db):
        setattr(mod, "get_db_connection", _pooled_get_db_connection)

    prefix = f"ch1_http_{uuid4().hex[:8]}"
    user_a = f"{prefix}_user_a"
    user_b = f"{prefix}_user_b"
    ws_a1 = str(uuid4())
    ws_a2 = str(uuid4())
    cleanup_user_ids = [user_a, user_b]
    cleanup_workspace_ids = [ws_a1, ws_a2]
    cleanup_thread_ids: list[str] = []

    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO users (user_id, email, name, created_at, updated_at)
                    VALUES (%s, %s, 'User A', NOW(), NOW()),
                           (%s, %s, 'User B', NOW(), NOW())
                    """,
                    (user_a, f"{user_a}@example.com", user_b, f"{user_b}@example.com"),
                )
                await cur.execute(
                    """
                    INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                    VALUES (%s, %s, %s, 'running', NOW(), NOW()),
                           (%s, %s, %s, 'running', NOW(), NOW())
                    """,
                    (ws_a1, user_a, f"{prefix}_WS_A1", ws_a2, user_a, f"{prefix}_WS_A2"),
                )

        bootstrap = await _seed_real_http_e2e_fixture(
            pool,
            prefix=prefix,
            user_a=user_a,
            user_b=user_b,
            ws_a1=ws_a1,
            ws_a2=ws_a2,
            cleanup_workspace_ids=cleanup_workspace_ids,
        )
        app = _create_real_http_test_app(
            pool,
            bootstrap_state=bootstrap,
            cleanup_workspace_ids=cleanup_workspace_ids,
            cleanup_thread_ids=cleanup_thread_ids,
        )
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass

        async with _run_ephemeral_fastapi_server(app) as base_url:
            print(f"HARNESS_READY {base_url}", flush=True)
            await stop_event.wait()
    finally:
        try:
            await _cleanup_test_rows(
                pool,
                prefix=prefix,
                user_ids=cleanup_user_ids,
                workspace_ids=cleanup_workspace_ids,
                thread_ids=cleanup_thread_ids,
            )
        finally:
            await pool.close()


if __name__ == "__main__" and "--serve-http-harness" in sys.argv:
    asyncio.run(_serve_cli_harness_main())
