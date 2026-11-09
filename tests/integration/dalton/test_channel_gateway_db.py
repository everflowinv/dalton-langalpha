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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
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
    assert replay_before["existing_status"] == "received"
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
    await cg_db.update_user_channel_settings(
        user_id=user_a,
        settings={
            "feishu": {
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

    async def _handle_thread_turn(
        request: Request, existing_thread_id: str | None = None
    ) -> StreamingResponse:
        user_id = (request.headers.get("X-User-Id") or "").strip()
        body = await request.json()
        ws_id = str(body.get("workspace_id") or cg_db.get_home_workspace_id(user_id))
        platform = str(body.get("platform") or "feishu")
        ext_id = body.get("external_thread_id")
        msgs = body.get("messages") or []
        user_text = ""
        if msgs and isinstance(msgs[-1], dict):
            c = msgs[-1].get("content")
            user_text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)

        thread_id = existing_thread_id
        run_id = str(uuid4())
        turn_index = 1

        async with pool.connection() as conn:
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

                if not thread_id and ext_id:
                    await cur.execute(
                        """
                        SELECT conversation_thread_id, thread_index
                        FROM conversation_threads
                        WHERE platform = %s AND external_id = %s AND user_id = %s
                        LIMIT 1
                        """,
                        (platform, ext_id, user_id),
                    )
                    t_row = await cur.fetchone()
                    if t_row:
                        thread_id = str(t_row["conversation_thread_id"])
                        turn_index = int(t_row.get("thread_index") or 0) + 1

                if not thread_id:
                    thread_id = str(uuid4())
                    await cur.execute(
                        """
                        INSERT INTO conversation_threads (
                            conversation_thread_id, workspace_id, user_id, current_status,
                            thread_index, external_id, platform, created_at, updated_at
                        )
                        VALUES (%s, %s, %s, 'completed', 1, %s, %s, NOW(), NOW())
                        """,
                        (thread_id, ws_id, user_id, ext_id, platform),
                    )
                else:
                    await cur.execute(
                        """
                        UPDATE conversation_threads
                        SET thread_index = thread_index + 1,
                            current_status = 'completed',
                            updated_at = NOW()
                        WHERE conversation_thread_id = %s
                        RETURNING thread_index
                        """,
                        (thread_id,),
                    )
                    upd = await cur.fetchone()
                    if upd:
                        turn_index = int(upd["thread_index"])

                if thread_id not in cleanup_thread_ids:
                    cleanup_thread_ids.append(thread_id)

                await cur.execute(
                    """
                    INSERT INTO conversation_responses (
                        conversation_response_id, conversation_thread_id,
                        turn_index, status, user_id, created_at
                    )
                    VALUES (%s, %s, %s, 'completed', %s, NOW())
                    """,
                    (run_id, thread_id, turn_index, user_id),
                )

        reply_text = f"[stub-reply ws={ws_id} thread={thread_id} turn={turn_index}] {user_text}".strip()

        async def _sse_gen():
            meta = {
                "thread_id": thread_id,
                "run_id": run_id,
                "turn_index": turn_index,
                "workspace_id": ws_id,
            }
            yield f"id: 1\nevent: metadata\ndata: {json.dumps(meta)}\n\n"
            chunk = {
                "thread_id": thread_id,
                "agent": "main",
                "id": f"msg_{run_id}",
                "role": "assistant",
                "content": reply_text,
                "content_type": "text",
                "finish_reason": "stop",
            }
            yield f"id: 2\nevent: message_chunk\ndata: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            end = {
                "thread_id": thread_id,
                "run_id": run_id,
                "outcome": "completed",
            }
            yield f"id: 3\nevent: run_end\ndata: {json.dumps(end)}\n\n"

        return StreamingResponse(_sse_gen(), media_type="text/event-stream")

    @app.post("/api/v1/threads/messages")
    async def create_thread_messages_stub(request: Request) -> StreamingResponse:
        return await _handle_thread_turn(request, existing_thread_id=None)

    @app.post("/api/v1/threads/{thread_id}/messages")
    async def append_thread_messages_stub(
        thread_id: str, request: Request
    ) -> StreamingResponse:
        return await _handle_thread_turn(request, existing_thread_id=thread_id)

    @app.post("/api/v1/threads/{thread_id}/cancel")
    async def cancel_thread_stub(thread_id: str) -> dict[str, Any]:
        return {"status": "cancelled", "thread_id": thread_id}

    @app.get("/api/v1/threads/{thread_id}/status")
    async def status_thread_stub(thread_id: str) -> dict[str, Any]:
        return {
            "thread_id": thread_id,
            "run_id": None,
            "status": "idle",
            "can_reconnect": False,
            "latest_turn_index": 1,
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
                              EXTRACT(EPOCH FROM last_conversation_activity_at) AS activity_epoch
                    """,
                    (seconds_ago, platform, scope_key, dalton_user_id),
                )
                row = await cur.fetchone()
        return dict(row) if row else {}

    @app.get("/test-harness/inspect")
    async def test_harness_inspect(
        platform: str = "feishu",
        scope_key: str | None = None,
        dalton_user_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                session_row = None
                if scope_key and dalton_user_id:
                    await cur.execute(
                        """
                        SELECT generation,
                               override_workspace_id::text AS override_workspace_id,
                               EXTRACT(EPOCH FROM last_conversation_activity_at) AS activity_epoch,
                               EXTRACT(EPOCH FROM selection_initialized_at) AS initialized_epoch
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
                               command_reply_pending
                        FROM dalton_channel_inbound_events
                        WHERE platform = %s AND message_id = %s
                        """,
                        (platform, message_id),
                    )
                    ir = await cur.fetchone()
                    if ir:
                        inbound_row = dict(ir)

        return {
            "session": session_row,
            "inbound": inbound_row,
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
    await cg_db.update_user_channel_settings(
        user_id=user_a,
        settings={
            "default": {"workspace_id": ws_a1},
            "feishu": {
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
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)
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
