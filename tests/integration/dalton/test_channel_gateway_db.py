"""Real Postgres integration tests for CH-1 (`d030_channel_gateway` + `d032_weixin_state`).

Reads the test database connection string from ``DALTON_CHANNEL_GATEWAY_TEST_DSN``
(or ``DALTON_TEST_DB_DSN`` / ``TEST_DATABASE_URL``); skips cleanly when unset.
A session-scoped fixture upgrades the test database to upstream head and Dalton ``d032``
(skipping if already at head, never dropping ``public`` schema), and each test uses
a unique prefix and cleans up its own rows on teardown.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from urllib.parse import urlsplit
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio

from src.dalton import sharing as dalton_sharing
from src.dalton.database import channel_gateway as cg_db
from src.server.database import pool as db_pool
from src.server.database import user as user_db
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
    """Upgrade upstream and Dalton Alembic chains to head/d030 only if not already there, without dropping schema."""
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
    dalton_target = "d032"

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
    """Ensure upstream + Dalton (`d030`) migrations are applied and yield a connection pool."""
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
    session_keys: list[str] | None = None,
) -> None:
    like_pat = f"%{prefix}%"
    ws_uuids = [w for w in workspace_ids if w]
    th_uuids = [t for t in thread_ids if t]
    sess_keys = [s for s in (session_keys or []) if s]
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM dalton_channel_weixin_context_tokens
                WHERE ilink_bot_id LIKE %s OR platform_user_id LIKE %s
                """,
                (like_pat, like_pat),
            )
            await cur.execute(
                """
                DELETE FROM dalton_channel_weixin_state
                WHERE owner_user_id = ANY(%s)
                   OR ilink_bot_id LIKE %s
                   OR ilink_user_id LIKE %s
                   OR login_owner_user_id = ANY(%s)
                   OR login_session_key LIKE %s
                   OR login_session_key = ANY(%s)
                   OR login_qrcode LIKE %s
                """,
                (
                    user_ids,
                    like_pat,
                    like_pat,
                    user_ids,
                    like_pat,
                    sess_keys,
                    like_pat,
                ),
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
            if th_uuids:
                await cur.execute(
                    "DELETE FROM hook_outbox WHERE conversation_thread_id = ANY(%s::uuid[])",
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
                    "DELETE FROM dalton_workspace_sharing WHERE workspace_id = ANY(%s)",
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
    monkeypatch.setattr(user_db, "get_db_connection", mock_gdc)

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

    # 3. Member user_a first-binds the group to ws_a1 -> succeeds AND advances all generations in the group (1 -> 2)
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
    for gr in gen_rows:
        assert gr["generation"] == 2

    # 4. Concurrent I-1 read and I-3 group workspace switch under the group advisory lock never observe a torn pair
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

    # 5. Decision 2: Global binding is now ws_a2 (from I-3 `/workspace`), while user_a's saved settings still have ws_a1.
    # (a) Saving automation_output_update (without changing `chats`) does NOT touch global binding ws_a2 or generations (still 3).
    upd_auto = await cg_db.update_user_channel_settings(
        user_id=user_a,
        automation_output_update={
            "workspace_id": ws_a1,
            "platform": "feishu",
            "address": "feishu:@me",
        },
    )
    # (a2) Round 4 Decision 2: Read-only `name` disappearing (while user_a's saved workspace_id is still ws_a1)
    # is NOT treated as a routing modification -> does NOT overwrite global binding ws_a2 or advance generations (still 3)!
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
                WHERE platform = 'feishu' AND (scope_key = %s OR scope_key = %s)
                """,
                (main_scope, thread_scope),
            )
            for gr in await cur.fetchall():
                assert gr["generation"] == 3

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
                WHERE platform = 'feishu' AND (scope_key = %s OR scope_key = %s)
                """,
                (main_scope, thread_scope),
            )
            for gr in await cur.fetchall():
                assert gr["generation"] == 3

    # (d) Deleting chat_addr from user_a's settings when global binding IS ws_a2 (== user_a's saved setting ws_a2)
    # deletes the global binding row AND advances generations (3 -> 4)!
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
                WHERE platform = 'feishu' AND (scope_key = %s OR scope_key = %s)
                """,
                (main_scope, thread_scope),
            )
            gen_rows_after_del = await cur.fetchall()
    for gr in gen_rows_after_del:
        assert gr["generation"] == 4

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
async def test_d032_weixin_login_sessions_context_tokens_and_group_rejection_real_pg(
    db_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Postgres verification of `d032_weixin_state` schema and runtime behavior:
    - Admin QR login session concurrency (`start` supersedes old pending session, stale confirm rejected, admin role revocation mid-flow rejected via real `get_user_tier`, confirm atomically advances `bot_generation`, resets cursor, invalidates old context tokens, duplicate confirm is idempotent, stale `-14` expiry rejected);
    - Non-empty `group_id` on Weixin is durably rejected (`weixin_group_unsupported`) and does not persist `context_token`;
    - Encrypted `context_token` persistence (`pgp_sym_encrypt`/`pgp_sym_decrypt`) with monotonic `seq` ordering and `I-6` selection for `notice` vs `agent_send`;
    - Idempotent `/bind` via `consumed_bind_result` on `dalton_channel_inbound_events`. The same inbound keeps its original seq. A newer message that arrives first stays in place when an older bind message runs later.
    """
    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    user_b = db_env["user_b"]
    pool: AsyncConnectionPool = db_env["pool"]

    # Configure real access tiers via DALTON_PM_USER_IDS + DALTON_ALLOWED_EMAILS (no monkeypatch on get_user_tier)
    monkeypatch.setenv("DALTON_PM_USER_IDS", f"{user_a},{user_b}")
    monkeypatch.setenv(
        "DALTON_ALLOWED_EMAILS", f"{user_a}@example.com,{user_b}@example.com"
    )

    bot_1 = f"{prefix}_wx_bot_1@im.wechat"
    bot_2 = f"{prefix}_wx_bot_2@im.wechat"
    wx_alice = f"{prefix}_wx_alice@im.wechat"

    # Pre-save unowned/unlinked control rows under a separate prefix to verify scoped cleanup preserves them
    ctrl_prefix = f"ctrl_other_{uuid4().hex[:8]}"
    ctrl_bot = f"{ctrl_prefix}_bot@im.wechat"
    ctrl_user = f"{ctrl_prefix}_user@im.wechat"
    ctrl_msg_id = f"{ctrl_bot}:ctrl_msg_1"
    ctrl_sess_key = f"{ctrl_prefix}_sess"
    ctrl_req_key = str(uuid4())

    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_weixin_context_tokens (
                        ilink_bot_id, bot_generation, platform_user_id,
                        encrypted_context_token, seq, updated_at
                    )
                    VALUES (%s, 1, %s, pgp_sym_encrypt('ctrl-token', %s), 7, NOW())
                    """,
                    (
                        ctrl_bot,
                        ctrl_user,
                        os.environ["BYOK_ENCRYPTION_KEY"],
                    ),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_inbound_events (
                        platform, message_id, request_key, dalton_user_id,
                        chat_type, chat_address, thread_scope_key, status,
                        fallback_pending, raw_payload
                    )
                    VALUES (
                        'weixin', %s, %s, NULL,
                        'dm', 'weixin:@me', %s, 'ignored',
                        FALSE, '{"deny_reason": "not_linked"}'::jsonb
                    )
                    """,
                    (ctrl_msg_id, ctrl_req_key, f"dm:{ctrl_user}"),
                )

        # 1. Seed initial active bot via real `start` -> `confirm` flow
        seed_start = await cg_db.upsert_weixin_state(
            login_session_action="start",
            login_session_key=f"{prefix}-sess-init",
            login_owner_user_id=user_a,
            login_status="wait",
            login_qrcode=f"{prefix}-qr-init",
            login_qrcode_img_content="data:image/png;base64,AAAA",
            login_expires_in_seconds=300,
        )
        assert seed_start["ok"] is True
        assert seed_start["updated"] is True
        v_init = int(seed_start["login_version"])
        exp_gen_init = int(seed_start["expected_bot_generation"])

        init_res = await cg_db.upsert_weixin_state(
            login_session_action="confirm",
            login_session_key=f"{prefix}-sess-init",
            login_owner_user_id=user_a,
            expected_login_version=v_init,
            expected_bot_generation=exp_gen_init,
            ilink_bot_id=bot_1,
            owner_user_id=user_a,
            bot_token="secret-pg-token-gen1",
            base_url="https://ilinkai.weixin.qq.com",
            status="active",
            ilink_user_id=f"{prefix}_wx_admin_a@im.wechat",
            ilink_username="Admin A",
        )
        assert init_res["ok"] is True
        assert init_res["updated"] is True
        gen_1 = int(init_res["bot_generation"])

        # 2. Non-empty group_id on Weixin is durably rejected (`weixin_group_unsupported`)
        grp_msg_id = f"{bot_1}:msg_grp_1"
        grp_res = await cg_db.register_and_resolve_inbound(
            platform="weixin",
            platform_user_id=wx_alice,
            chat_type="dm",
            chat_address="weixin:@me",
            thread_scope_key=f"dm:{wx_alice}",
            message_id=grp_msg_id,
            weixin_context_token="ctx-should-not-persist",
            raw_payload={
                "group_id": f"{prefix}_chatroom@chatroom",
                "ilink_bot_id": bot_1,
                "seq": 10,
            },
        )
        assert grp_res["allowed"] is False
        assert grp_res["deny_reason"] == "weixin_group_unsupported"

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT 1 FROM dalton_channel_weixin_context_tokens
                    WHERE ilink_bot_id = %s AND platform_user_id = %s
                    """,
                    (bot_1, wx_alice),
                )
                assert await cur.fetchone() is None

        # 3. Unlinked DM persists encrypted `context_token` with monotonic `seq` guard
        unlinked_mid_20 = f"{bot_1}:msg_u_20"
        u20 = await cg_db.register_and_resolve_inbound(
            platform="weixin",
            platform_user_id=wx_alice,
            chat_type="dm",
            chat_address="weixin:@me",
            thread_scope_key=f"dm:{wx_alice}",
            message_id=unlinked_mid_20,
            weixin_context_token="ctx-pg-seq-20",
            raw_payload={"ilink_bot_id": bot_1, "seq": 20},
        )
        assert u20["linked"] is False
        assert u20["deny_reason"] == "not_linked"

        # Out-of-order lower seq (seq=12 < 20) does not overwrite `ctx-pg-seq-20`
        await cg_db.register_and_resolve_inbound(
            platform="weixin",
            platform_user_id=wx_alice,
            chat_type="dm",
            chat_address="weixin:@me",
            thread_scope_key=f"dm:{wx_alice}",
            message_id=f"{bot_1}:msg_u_12",
            weixin_context_token="ctx-pg-stale-12",
            raw_payload={"ilink_bot_id": bot_1, "seq": 12},
        )

        # I-6 notice delivery selects `ctx-pg-seq-20` for the original sender and denies a mismatched recipient
        notice_claim = await cg_db.authorize_and_claim_delivery(
            delivery_key=f"{prefix}:notice:u20",
            kind="notice",
            platform="weixin",
            target=f"weixin:{wx_alice}",
            inbound_message_id=unlinked_mid_20,
            has_non_empty_text=True,
            total_chunks=1,
        )
        assert notice_claim["claim_status"] == "claimed"
        assert notice_claim["weixin_context_token"] == "ctx-pg-seq-20"

        notice_mismatch = await cg_db.authorize_and_claim_delivery(
            delivery_key=f"{prefix}:notice:u20:mismatch",
            kind="notice",
            platform="weixin",
            target=f"weixin:{prefix}_other@im.wechat",
            inbound_message_id=unlinked_mid_20,
            has_non_empty_text=True,
            total_chunks=1,
        )
        assert notice_mismatch["claim_status"] == "denied"
        assert notice_mismatch["code"] == "not_allowed"
        assert (
            notice_mismatch["message"]
            == "Notice target does not match original inbound conversation."
        )
        assert not notice_mismatch.get("weixin_context_token")

        # 4. Higher seq (seq=30 > 20) updates context_token, and `/bind` with `inbound_message_id` is idempotent
        bind_mid_30 = f"{bot_1}:msg_bind_30"
        await cg_db.register_and_resolve_inbound(
            platform="weixin",
            platform_user_id=wx_alice,
            chat_type="dm",
            chat_address="weixin:@me",
            thread_scope_key=f"dm:{wx_alice}",
            message_id=bind_mid_30,
            weixin_context_token="ctx-pg-seq-30",
            raw_payload={"ilink_bot_id": bot_1, "seq": 30},
        )
        code_wx = await cg_db.create_bind_code(user_id=user_b, platform="weixin")
        bind_1 = await cg_db.consume_bind_code_or_unbind(
            action="bind",
            platform="weixin",
            platform_user_id=wx_alice,
            platform_username="Alice PG",
            code=code_wx["code"],
            is_group=False,
            inbound_message_id=bind_mid_30,
            weixin_context_token="ctx-pg-seq-30",
        )
        assert bind_1["ok"] is True
        assert bind_1["user_id"] == user_b

        # Replay `/bind` with same `inbound_message_id` -> returns cached `consumed_bind_result`
        bind_replay = await cg_db.consume_bind_code_or_unbind(
            action="bind",
            platform="weixin",
            platform_user_id=wx_alice,
            platform_username="Alice PG",
            code=code_wx["code"],
            is_group=False,
            inbound_message_id=bind_mid_30,
            weixin_context_token="ctx-pg-seq-30",
        )
        assert bind_1.get("idempotent") is not True
        assert bind_replay["idempotent"] is True
        for field in ("ok", "user_id", "platform", "platform_user_id", "generation"):
            assert bind_replay[field] == bind_1[field]
        assert bind_replay["ok"] is True
        assert bind_replay["user_id"] == user_b
        assert bind_replay["platform"] == "weixin"
        assert bind_replay["platform_user_id"] == wx_alice
        enc_key = os.environ["BYOK_ENCRYPTION_KEY"]
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM dalton_channel_bind_codes
                    WHERE user_id = %s AND platform = 'weixin' AND used_at IS NOT NULL
                    """,
                    (user_b,),
                )
                assert (await cur.fetchone())["cnt"] == 1
                await cur.execute(
                    """
                    SELECT seq,
                           pgp_sym_decrypt(encrypted_context_token, %s) AS context_token
                    FROM dalton_channel_weixin_context_tokens
                    WHERE ilink_bot_id = %s
                      AND bot_generation = %s
                      AND platform_user_id = %s
                    """,
                    (enc_key, bot_1, gen_1, wx_alice),
                )
                token_row = await cur.fetchone()
                assert token_row["seq"] == 30
                assert token_row["context_token"] == "ctx-pg-seq-30"
                await cur.execute(
                    """
                    SELECT weixin_context_seq,
                           pgp_sym_decrypt(encrypted_weixin_context_token, %s) AS context_token
                    FROM dalton_channel_identities
                    WHERE user_id = %s AND platform = 'weixin'
                    """,
                    (enc_key, user_b),
                )
                identity_row = await cur.fetchone()
                assert identity_row["weixin_context_seq"] == 30
                assert identity_row["context_token"] == "ctx-pg-seq-30"

        # A newer message arrives first. The older inbound can then store that
        # newer seq beside its own ciphertext. Binding the older message keeps
        # the newer token and its original seq, and leaves the seq-30 row alone.
        wx_carol = f"{prefix}_wx_carol@im.wechat"
        newer_mid = f"{bot_1}:msg_u_45"
        older_mid = f"{bot_1}:msg_bind_16"
        await cg_db.register_and_resolve_inbound(
            platform="weixin",
            platform_user_id=wx_carol,
            chat_type="dm",
            chat_address="weixin:@me",
            thread_scope_key=f"dm:{wx_carol}",
            message_id=newer_mid,
            weixin_context_token="ctx-pg-seq-45",
            raw_payload={"ilink_bot_id": bot_1, "seq": 45},
        )
        await cg_db.register_and_resolve_inbound(
            platform="weixin",
            platform_user_id=wx_carol,
            chat_type="dm",
            chat_address="weixin:@me",
            thread_scope_key=f"dm:{wx_carol}",
            message_id=older_mid,
            weixin_context_token="ctx-pg-old-16",
            raw_payload={"ilink_bot_id": bot_1, "seq": 16},
        )
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT weixin_context_seq,
                           raw_payload->>'seq' AS payload_seq,
                           pgp_sym_decrypt(encrypted_weixin_context_token, %s) AS context_token
                    FROM dalton_channel_inbound_events
                    WHERE platform = 'weixin' AND message_id = %s
                    """,
                    (enc_key, older_mid),
                )
                older_inbound = await cur.fetchone()
                assert older_inbound["payload_seq"] == "16"
                assert older_inbound["weixin_context_seq"] == 45
                assert older_inbound["context_token"] == "ctx-pg-old-16"
        code_carol = await cg_db.create_bind_code(user_id=user_a, platform="weixin")
        bind_older = await cg_db.consume_bind_code_or_unbind(
            action="bind",
            platform="weixin",
            platform_user_id=wx_carol,
            platform_username="Carol PG",
            code=code_carol["code"],
            is_group=False,
            inbound_message_id=older_mid,
            weixin_context_token="ctx-pg-old-16",
        )
        assert bind_older["ok"] is True
        assert bind_older["user_id"] == user_a
        assert bind_older.get("idempotent") is not True
        bind_older_replay = await cg_db.consume_bind_code_or_unbind(
            action="bind",
            platform="weixin",
            platform_user_id=wx_carol,
            platform_username="Carol PG",
            code=code_carol["code"],
            is_group=False,
            inbound_message_id=older_mid,
            weixin_context_token="ctx-pg-old-16",
        )
        assert bind_older_replay["idempotent"] is True
        for field in ("ok", "user_id", "platform", "platform_user_id", "generation"):
            assert bind_older_replay[field] == bind_older[field]
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT seq,
                           pgp_sym_decrypt(encrypted_context_token, %s) AS context_token
                    FROM dalton_channel_weixin_context_tokens
                    WHERE ilink_bot_id = %s
                      AND bot_generation = %s
                      AND platform_user_id = %s
                    """,
                    (enc_key, bot_1, gen_1, wx_carol),
                )
                carol_token = await cur.fetchone()
                assert carol_token["seq"] == 45
                assert carol_token["context_token"] == "ctx-pg-seq-45"
                await cur.execute(
                    """
                    SELECT weixin_context_seq,
                           pgp_sym_decrypt(encrypted_weixin_context_token, %s) AS context_token
                    FROM dalton_channel_identities
                    WHERE user_id = %s AND platform = 'weixin'
                    """,
                    (enc_key, user_a),
                )
                carol_identity = await cur.fetchone()
                assert carol_identity["weixin_context_seq"] == 45
                assert carol_identity["context_token"] == "ctx-pg-seq-45"
                await cur.execute(
                    """
                    SELECT seq,
                           pgp_sym_decrypt(encrypted_context_token, %s) AS context_token
                    FROM dalton_channel_weixin_context_tokens
                    WHERE ilink_bot_id = %s
                      AND bot_generation = %s
                      AND platform_user_id = %s
                    """,
                    (enc_key, bot_1, gen_1, wx_alice),
                )
                alice_token = await cur.fetchone()
                assert alice_token["seq"] == 30
                assert alice_token["context_token"] == "ctx-pg-seq-30"
                await cur.execute(
                    """
                    SELECT weixin_context_seq,
                           pgp_sym_decrypt(encrypted_weixin_context_token, %s) AS context_token
                    FROM dalton_channel_identities
                    WHERE user_id = %s AND platform = 'weixin'
                    """,
                    (enc_key, user_b),
                )
                alice_identity = await cur.fetchone()
                assert alice_identity["weixin_context_seq"] == 30
                assert alice_identity["context_token"] == "ctx-pg-seq-30"

        # 5. Concurrent Admin QR login sessions: S1 started by user_a, superseded by S2 started by user_b
        s1 = await cg_db.upsert_weixin_state(
            login_session_action="start",
            login_session_key=f"{prefix}-sess-1",
            login_owner_user_id=user_a,
            login_status="wait",
        )
        assert s1["updated"] is True
        v1 = int(s1["login_version"])

        s2 = await cg_db.upsert_weixin_state(
            login_session_action="start",
            login_session_key=f"{prefix}-sess-2",
            login_owner_user_id=user_b,
            login_status="wait",
        )
        assert s2["updated"] is True
        v2 = int(s2["login_version"])
        assert v2 > v1

        # Stale confirmation from S1 is rejected
        stale_c1 = await cg_db.upsert_weixin_state(
            login_session_action="confirm",
            login_session_key=f"{prefix}-sess-1",
            expected_login_version=v1,
            expected_bot_generation=gen_1,
            login_owner_user_id=user_a,
            ilink_bot_id=f"{prefix}_wx_stale@im.wechat",
            owner_user_id=user_a,
            bot_token="stale-token",
            status="active",
        )
        assert stale_c1["updated"] is False
        assert stale_c1["stale_session"] is True
        assert stale_c1["reason"] == "session_replaced"

        # Revoking user_b's admin permission before confirm causes real `get_user_tier` to reject S2 confirm
        monkeypatch.setenv("DALTON_PM_USER_IDS", user_a)
        revoked_c2 = await cg_db.upsert_weixin_state(
            login_session_action="confirm",
            login_session_key=f"{prefix}-sess-2",
            expected_login_version=v2,
            expected_bot_generation=gen_1,
            login_owner_user_id=user_b,
            ilink_bot_id=bot_2,
            owner_user_id=user_b,
            bot_token="secret-pg-token-gen2",
            status="active",
        )
        assert revoked_c2["ok"] is False
        assert revoked_c2["updated"] is False
        assert revoked_c2["reason"] == "admin_required"

        # Restore user_b admin tier and confirm S2 -> advances bot_generation to gen_1 + 1, resets cursor, invalidates old context tokens
        monkeypatch.setenv("DALTON_PM_USER_IDS", f"{user_a},{user_b}")
        confirm_s2 = await cg_db.upsert_weixin_state(
            login_session_action="confirm",
            login_session_key=f"{prefix}-sess-2",
            expected_login_version=v2,
            expected_bot_generation=gen_1,
            login_owner_user_id=user_b,
            ilink_bot_id=bot_2,
            owner_user_id=user_b,
            bot_token="secret-pg-token-gen2",
            base_url="https://ilinkai.weixin.qq.com",
            status="active",
            ilink_user_id=f"{prefix}_wx_admin_b@im.wechat",
            ilink_username="Admin B",
        )
        assert confirm_s2["updated"] is True
        gen_2 = int(confirm_s2["bot_generation"])
        assert gen_2 == gen_1 + 1
        assert confirm_s2["get_updates_buf"] == ""

        # Duplicate confirmation of S2 is idempotent
        confirm_s2_dup = await cg_db.upsert_weixin_state(
            login_session_action="confirm",
            login_session_key=f"{prefix}-sess-2",
            expected_login_version=v2,
            expected_bot_generation=gen_1,
            login_owner_user_id=user_b,
            ilink_bot_id=bot_2,
            owner_user_id=user_b,
            bot_token="secret-pg-token-gen2",
            base_url="https://ilinkai.weixin.qq.com",
            status="active",
            ilink_user_id=f"{prefix}_wx_admin_b@im.wechat",
            ilink_username="Admin B",
        )
        assert confirm_s2_dup["ok"] is True
        assert confirm_s2_dup["updated"] is False
        assert confirm_s2_dup["already_connected"] is True
        assert confirm_s2_dup["idempotent"] is True
        assert int(confirm_s2_dup["bot_generation"]) == gen_2

        # Stale `-14` expiry from gen_1 poller is rejected (`stale_generation`) and keeps `active`
        stale_exp = await cg_db.upsert_weixin_state(
            ilink_bot_id=bot_1,
            expected_bot_generation=gen_1,
            status="expired",
        )
        assert stale_exp["updated"] is False
        assert stale_exp["stale_generation"] is True
        assert stale_exp["status"] == "active"

        # 6. Verify scoped cleanup removes this test's own rows while preserving another test's unowned/unlinked control rows
        await _cleanup_test_rows(
            pool,
            prefix=prefix,
            user_ids=db_env["cleanup_user_ids"],
            workspace_ids=[],
            thread_ids=[],
        )

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) AS cnt FROM dalton_channel_weixin_context_tokens WHERE ilink_bot_id = %s",
                    (ctrl_bot,),
                )
                assert (await cur.fetchone())["cnt"] == 1

                await cur.execute(
                    "SELECT COUNT(*) AS cnt FROM dalton_channel_inbound_events WHERE message_id = %s",
                    (ctrl_msg_id,),
                )
                assert (await cur.fetchone())["cnt"] == 1

                # Seed an unowned control row in dalton_channel_weixin_state (owner_user_id IS NULL, login_owner_user_id IS NULL)
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_weixin_state (
                        singleton_id, ilink_bot_id, owner_user_id, encrypted_bot_token,
                        base_url, get_updates_buf, status, bot_generation,
                        login_session_key, login_owner_user_id, login_version, updated_at
                    )
                    VALUES (
                        1, NULL, NULL, NULL,
                        'https://ilinkai.weixin.qq.com', '', 'unconfigured', 1,
                        %s, NULL, 1, NOW()
                    )
                    """,
                    (ctrl_sess_key,),
                )

        # Running this test's cleanup must NOT delete the unowned control row in dalton_channel_weixin_state
        await _cleanup_test_rows(
            pool,
            prefix=prefix,
            user_ids=db_env["cleanup_user_ids"],
            workspace_ids=[],
            thread_ids=[],
        )
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) AS cnt FROM dalton_channel_weixin_state WHERE login_session_key = %s AND owner_user_id IS NULL",
                    (ctrl_sess_key,),
                )
                assert (await cur.fetchone())["cnt"] == 1
    finally:
        # Clean up the control rows using the control test's own prefix/session key
        await _cleanup_test_rows(
            pool,
            prefix=ctrl_prefix,
            user_ids=[],
            workspace_ids=[],
            thread_ids=[],
            session_keys=[ctrl_sess_key],
        )
