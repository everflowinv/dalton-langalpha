"""Real PostgreSQL integration tests for Dalton Channel Gateway (`d030_channel_gateway`).

Reads the isolated test database connection string from ``DALTON_CHANNEL_GATEWAY_TEST_DSN``
(or ``DALTON_TEST_DB_DSN`` / ``TEST_DATABASE_URL``); skips cleanly when unset and unreachable.
Enforces that the target database name ends with ``_test``, upgrades upstream Alembic to ``head``
and Dalton Alembic to ``d031`` or leaves a later Dalton revision in place, and exercises real permission checks, real FastAPI routes, and
real HTTP-to-FastAPI-to-PostgreSQL flows with ``DiscordChannelAdapter`` and ``GatewayOrchestrator``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
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
from fastapi.responses import Response, StreamingResponse
import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio
import uvicorn

from src.config import settings as app_settings
from src.dalton import invites as dalton_invites
from src.dalton import sharing as dalton_sharing
from src.dalton.app.channels_router import channels_router
from src.dalton.app.router import router as dalton_router
from src.dalton.database import channel_gateway as cg_db
from src.server.database import pool as db_pool
from src.server.database import user as user_db
from src.server.database import workspace as workspace_db
from src.server.utils.api import get_current_user_id

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


def _abort_non_test_db(message: str) -> None:
    if "pytest" in sys.modules and "--serve-http-harness" not in sys.argv:
        pytest.fail(message)
    raise RuntimeError(message)


def _get_test_dsn() -> str:
    for var in _DSN_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            db_name = urlsplit(val).path.lstrip("/")
            if not db_name.endswith("_test"):
                _abort_non_test_db(
                    f"Refusing to run channel gateway integration tests against non-test DB {db_name!r} (from {var})"
                )
            return val

    host = os.getenv("TEST_DB_HOST", "localhost")
    port = os.getenv("TEST_DB_PORT", "5432")
    name = (os.getenv("TEST_DB_NAME", "langalpha_test") or "").strip().lstrip("/")
    if not name.endswith("_test"):
        _abort_non_test_db(
            f"Refusing to run channel gateway integration tests against non-test DB {name!r} (from TEST_DB_NAME)"
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
        if "--serve-http-harness" in sys.argv:
            raise RuntimeError(
                f"Test DB {host}:{port}/{name} is unreachable: {exc}"
            ) from exc
        pytest.skip(
            f"None of {_DSN_ENV_VARS} is set and default test DB {host}:{port}/{name} is unreachable ({exc}); skipping real Postgres channel gateway integration tests"
        )


def _run_alembic_upgrade_sync(cfg: Config, revision: str) -> None:
    command.upgrade(cfg, revision)


def _revision_is_target_or_descendant(
    script: ScriptDirectory, current: str, target: str
) -> bool:
    """True when ``current`` is ``target`` or a later revision on the same chain."""
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


async def _ensure_alembic_upgraded_to_d031(
    dsn: str, *, minimum: str = "d031"
) -> None:
    """Upgrade upstream to head and Dalton to ``minimum`` when the database is behind it.

    A database already at ``minimum`` or a descendant (d035, d036, …) is left
    alone. Alembic cannot upgrade to an older revision, so this must not call
    ``upgrade("d031")`` after a later revision has been applied. The Dalton
    version table is ``alembic_version_dalton``.
    """
    project_root = Path(__file__).resolve().parents[3]
    sa_url = dsn.replace("postgresql://", "postgresql+psycopg://", 1).replace(
        "%", "%%"
    )

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

    current_upstream: str | None = None
    dalton_versions: list[str] = []
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT to_regclass('public.alembic_version')")
            row = await cur.fetchone()
            if row and row[0] is not None:
                await cur.execute("SELECT version_num FROM alembic_version LIMIT 1")
                vrow = await cur.fetchone()
                if vrow:
                    current_upstream = str(vrow[0])

            await cur.execute(
                "SELECT to_regclass('public.alembic_version_dalton')"
            )
            drow = await cur.fetchone()
            if drow and drow[0] is not None:
                await cur.execute(
                    "SELECT version_num FROM alembic_version_dalton"
                )
                dalton_versions = [
                    str(item[0])
                    for item in await cur.fetchall()
                    if item and item[0]
                ]

    if current_upstream != upstream_head:
        await asyncio.to_thread(_run_alembic_upgrade_sync, upstream_cfg, "head")

    if not any(
        _revision_is_target_or_descendant(dalton_script, version, minimum)
        for version in dalton_versions
    ):
        await asyncio.to_thread(_run_alembic_upgrade_sync, dalton_cfg, minimum)


@pytest_asyncio.fixture(scope="session")
async def channel_gateway_db_pool():
    """Ensure upstream (`head`) + Dalton (`d031`) migrations are applied and yield a connection pool."""
    dsn = _get_test_dsn()
    os.environ.setdefault(
        "BYOK_ENCRYPTION_KEY", "test-channel-gateway-encryption-key-32b"
    )

    await _ensure_alembic_upgraded_to_d031(dsn)

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
                    "DELETE FROM dalton_workspace_sharing WHERE workspace_id = ANY(%s::uuid[])",
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

    prefix = f"ch1_dc_{uuid4().hex[:8]}"
    user_a = f"{prefix}_user_a"
    user_b = f"{prefix}_user_b"
    user_ext = f"{prefix}_user_ext"
    ws_a1 = str(uuid4())
    ws_a2 = str(uuid4())
    ws_b_shared_rw = str(uuid4())
    ws_b_private = str(uuid4())
    cleanup_user_ids: list[str] = [user_a, user_b, user_ext]
    cleanup_workspace_ids: list[str] = [
        ws_a1,
        ws_a2,
        ws_b_shared_rw,
        ws_b_private,
    ]
    cleanup_thread_ids: list[str] = []

    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("DALTON_COLLEAGUE_DISPATCH_ENABLED", "true")
    monkeypatch.setenv(
        "DALTON_ALLOWED_EMAILS",
        f"{user_a}@example.com,{user_b}@example.com",
    )

    async with channel_gateway_db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO users (user_id, email, name, created_at, updated_at)
                VALUES (%s, %s, 'User A', NOW(), NOW()),
                       (%s, %s, 'User B', NOW(), NOW()),
                       (%s, %s, 'External User', NOW(), NOW())
                """,
                (
                    user_a,
                    f"{user_a}@example.com",
                    user_b,
                    f"{user_b}@example.com",
                    user_ext,
                    f"{user_ext}@external.test",
                ),
            )
            await cur.execute(
                """
                INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'running', NOW(), NOW()),
                       (%s, %s, %s, 'running', NOW(), NOW()),
                       (%s, %s, %s, 'running', NOW(), NOW()),
                       (%s, %s, %s, 'running', NOW(), NOW())
                """,
                (
                    ws_a1,
                    user_a,
                    f"{prefix}_WS_A1",
                    ws_a2,
                    user_a,
                    f"{prefix}_WS_A2",
                    ws_b_shared_rw,
                    user_b,
                    f"{prefix}_WS_B_Shared_RW",
                    ws_b_private,
                    user_b,
                    f"{prefix}_WS_B_Private",
                ),
            )
            await cur.execute(
                """
                INSERT INTO dalton_workspace_sharing (
                    workspace_id, cos_access, web_access, updated_by, updated_at
                )
                VALUES (%s, 'rw', 'rw', %s, NOW()),
                       (%s, 'hidden', 'hidden', %s, NOW())
                """,
                (ws_b_shared_rw, user_b, ws_b_private, user_b),
            )

    env: dict[str, Any] = {
        "pool": channel_gateway_db_pool,
        "prefix": prefix,
        "user_a": user_a,
        "user_b": user_b,
        "user_ext": user_ext,
        "ws_a1": ws_a1,
        "ws_a2": ws_a2,
        "ws_b_shared_rw": ws_b_shared_rw,
        "ws_b_private": ws_b_private,
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
async def test_discord_thread_delivery_claim_recovery_and_real_permissions(
    db_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise I-1 -> I-4 -> I-5 -> I-6 -> I-7 against real migrated PostgreSQL (d030) and real permissions:
    - Real user tiers (`get_user_tier`), real workspace write checks (`_get_writable_workspace_names`,
      `require_workspace_access`), and real group chat membership checks (`_user_can_manage_or_view_group_chat`).
    - Complete Discord group configuration (`preferred`, `automation_output`, `agent_messages`, `chats`) after
      inbound group member registration; real rejection of private colleague workspaces and external-tier group chats.
    - Discord guild thread inbound events persist `chat_address` (parent channel) and `thread_scope_key` (thread).
    - `I-6` (`authorize-and-claim`) recovers and returns `thread_scope_key` for current-conversation sends
      across `inbound_events`, `background_runs`, and `thread_watches`.
    - Retry claims after `failed` and duplicate claims (`in_flight_or_unknown`, `already_completed`) preserve `thread_scope_key`.
    - Explicit `target` to another allowed channel keeps `is_current = False` and `thread_scope_key = None`;
      explicit `target` matching current conversation sets `is_current = True` and `thread_scope_key`;
      unauthorized targets and revoked workspace write permissions are denied (`not_allowed`).
    - `I-7` (`recover`) returns `scope_key` and `thread_scope_key` on recovered background runs and thread watches.
    """
    service_token = "0123456789abcdef0123456789abcdef_pg_test"
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", service_token)

    prefix = db_env["prefix"]
    user_a = db_env["user_a"]
    user_b = db_env["user_b"]
    user_ext = db_env["user_ext"]
    ws_a1 = db_env["ws_a1"]
    ws_b_shared_rw = db_env["ws_b_shared_rw"]
    ws_b_private = db_env["ws_b_private"]

    puid_a = f"{prefix}_dc_u_a"
    puid_b = f"{prefix}_dc_u_b"
    puid_ext = f"{prefix}_dc_u_ext"

    guild_id = "700000001"
    parent_cid = f"700{uuid4().int % 1000000:06d}"
    other_cid = f"800{uuid4().int % 1000000:06d}"
    private_b_cid = f"850{uuid4().int % 1000000:06d}"
    unauth_cid = f"900{uuid4().int % 1000000:06d}"
    thread_cid = f"300{uuid4().int % 1000000:06d}"
    watch_thread_cid = f"400{uuid4().int % 1000000:06d}"

    parent_addr = f"discord:{guild_id}/{parent_cid}"
    other_allowed_addr = f"discord:{guild_id}/{other_cid}"
    private_b_addr = f"discord:{guild_id}/{private_b_cid}"
    unauthorized_addr = f"discord:{guild_id}/{unauth_cid}"
    thread_scope_key = f"chat:{guild_id}/{parent_cid}:thread:{thread_cid}"
    watch_scope_key = f"chat:{guild_id}/{parent_cid}:thread:{watch_thread_cid}"

    turn_tid = str(uuid4())
    watch_tid = str(uuid4())
    db_env["cleanup_thread_ids"].extend([turn_tid, watch_tid])
    turn_rid = f"run-pg-in-{uuid4().hex[:8]}"
    bg_rid = f"run-pg-bg-{uuid4().hex[:8]}"
    watch_rid = f"run-pg-watch-{uuid4().hex[:8]}"
    inbound_mid = f"{prefix}_msg_in_thread"

    app = FastAPI()
    app.include_router(dalton_router)
    app.dependency_overrides[get_current_user_id] = lambda: user_a

    headers = {
        "X-Service-Token": service_token,
        "X-User-Id": user_a,
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # 1. Bind Discord identities for user_a, user_b, and user_ext via real I-2
        for uid, puid in (
            (user_a, puid_a),
            (user_b, puid_b),
            (user_ext, puid_ext),
        ):
            code = await cg_db.create_bind_code(user_id=uid, platform="discord")
            bind_res = await client.post(
                "/api/v1/dalton/channels/internal/bind-code/consume",
                headers={"X-Service-Token": service_token, "X-User-Id": uid},
                json={
                    "action": "bind",
                    "platform": "discord",
                    "platform_user_id": puid,
                    "platform_username": f"name_{uid}",
                    "code": code["code"],
                    "is_group": False,
                },
            )
            assert bind_res.status_code == 200
            assert bind_res.json()["ok"] is True

        # 2. External user group chat veto in I-1 & I-5 (real get_user_tier)
        ext_in = await client.post(
            "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
            headers={"X-Service-Token": service_token},
            json={
                "platform": "discord",
                "platform_user_id": puid_ext,
                "chat_type": "group",
                "chat_address": parent_addr,
                "thread_scope_key": f"chat:{guild_id}/{parent_cid}",
                "message_id": f"{prefix}_ext_group_msg",
                "raw_payload": {"platform_user_id": puid_ext},
            },
        )
        assert ext_in.status_code == 200
        assert ext_in.json()["allowed"] is False
        assert ext_in.json()["deny_reason"] == "external_member_group_forbidden"

        ext_put = await client.put(
            "/api/v1/dalton/channels/internal/settings",
            headers={"X-Service-Token": service_token, "X-User-Id": user_ext},
            json={
                "user_id": user_ext,
                "settings": {
                    "discord": {
                        "preferred": "discord:@me",
                        "automation_output": {},
                        "agent_messages": {
                            "enabled": True,
                            "allowed": ["discord:@me", parent_addr],
                        },
                        "chats": {
                            parent_addr: {"name": "Ext Group", "workspace_id": None}
                        },
                    }
                },
            },
        )
        assert ext_put.status_code == 400
        assert "External members cannot configure group chats" in json.dumps(
            ext_put.json()
        )

        # 3. Register inbound group membership for user_a in parent_addr, other_allowed_addr, and private_b_addr,
        #    and for user_b in private_b_addr, then configure settings via I-5.
        for mid, puid, addr in (
            (f"{prefix}_mem_parent", puid_a, parent_addr),
            (f"{prefix}_mem_other", puid_a, other_allowed_addr),
            (f"{prefix}_mem_priv_a", puid_a, private_b_addr),
            (f"{prefix}_mem_priv_b", puid_b, private_b_addr),
        ):
            r_mem = await client.post(
                "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
                headers=headers,
                json={
                    "platform": "discord",
                    "platform_user_id": puid,
                    "chat_type": "group",
                    "chat_address": addr,
                    "thread_scope_key": f"chat:{addr.split(':', 1)[1]}",
                    "message_id": mid,
                    "raw_payload": {"platform_user_id": puid},
                },
            )
            assert r_mem.status_code == 200
            assert r_mem.json()["allowed"] is True
            await cg_db.update_inbound_progress(
                platform="discord", message_id=mid, status="completed"
            )

        # Real permission rejection: user_a cannot bind user_b's private workspace `ws_b_private`
        unauth_ws_put = await client.put(
            "/api/v1/dalton/channels/internal/settings",
            headers=headers,
            json={
                "user_id": user_a,
                "settings": {
                    "discord": {
                        "preferred": "discord:@me",
                        "automation_output": {},
                        "agent_messages": {
                            "enabled": True,
                            "allowed": ["discord:@me", private_b_addr],
                        },
                        "chats": {
                            private_b_addr: {
                                "name": "Private B Channel",
                                "workspace_id": ws_b_private,
                            }
                        },
                    }
                },
            },
        )
        assert unauth_ws_put.status_code == 400
        assert "not a writable workspace" in json.dumps(unauth_ws_put.json())

        # Real permission acceptance: user_a binds own `ws_a1` and colleague-shared `ws_b_shared_rw`
        valid_put = await client.put(
            "/api/v1/dalton/channels/internal/settings",
            headers=headers,
            json={
                "user_id": user_a,
                "settings": {
                    "default": {"workspace_id": ws_a1},
                    "discord": {
                        "preferred": "discord:@me",
                        "automation_output": {ws_a1: parent_addr},
                        "agent_messages": {
                            "enabled": True,
                            "allowed": [
                                "discord:@me",
                                parent_addr,
                                other_allowed_addr,
                                private_b_addr,
                            ],
                        },
                        "chats": {
                            parent_addr: {
                                "name": "Equities",
                                "workspace_id": ws_a1,
                            },
                            other_allowed_addr: {
                                "name": "Shared Colleague Alerts",
                                "workspace_id": ws_b_shared_rw,
                            },
                        },
                    },
                },
            },
        )
        assert valid_put.status_code == 200, valid_put.text

        # User B binds `private_b_addr` to `ws_b_private`; User A also lists `private_b_addr` in `chats` (workspace_id: null)
        valid_put_b = await client.put(
            "/api/v1/dalton/channels/internal/settings",
            headers={"X-Service-Token": service_token, "X-User-Id": user_b},
            json={
                "user_id": user_b,
                "settings": {
                    "discord": {
                        "preferred": "discord:@me",
                        "automation_output": {},
                        "agent_messages": {
                            "enabled": True,
                            "allowed": ["discord:@me", private_b_addr],
                        },
                        "chats": {
                            private_b_addr: {
                                "name": "Private B Channel",
                                "workspace_id": ws_b_private,
                            },
                        },
                    },
                },
            },
        )
        assert valid_put_b.status_code == 200, valid_put_b.text
        async with db_env["pool"].connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_channel_user_settings
                    SET apps = jsonb_set(
                        apps,
                        ARRAY['discord', 'chats', %s],
                        '{"name": "Private B Channel", "workspace_id": null}'::jsonb
                    )
                    WHERE user_id = %s
                    """,
                    (private_b_addr, user_a),
                )

        # 4. A new thread scope starts in Chief of Staff (Home). Select ws_a1 on that
        #    same scope through the real session command before workspace delivery.
        home_ws_a = cg_db.get_home_workspace_id(user_a)
        home_probe_mid = f"{prefix}_msg_home_probe"
        r_home = await client.post(
            "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
            headers=headers,
            json={
                "platform": "discord",
                "platform_user_id": puid_a,
                "chat_type": "group",
                "chat_address": parent_addr,
                "thread_scope_key": thread_scope_key,
                "message_id": home_probe_mid,
                "raw_payload": {"platform_user_id": puid_a},
            },
        )
        assert r_home.status_code == 200, r_home.text
        home_data = r_home.json()
        assert home_data["allowed"] is True
        assert home_data["dalton_user_id"] == user_a
        assert home_data["workspace_id"] == home_ws_a
        assert home_data["workspace_id"] != ws_a1
        assert home_data["generation"] == 1
        assert home_data["external_thread_id"] == (
            f"discord:{thread_scope_key}:user:{user_a}:gen:1"
        )
        r_home_done = await client.post(
            "/api/v1/dalton/channels/internal/inbound/progress",
            headers=headers,
            json={
                "platform": "discord",
                "message_id": home_probe_mid,
                "status": "completed",
                "fallback_pending": False,
            },
        )
        assert r_home_done.status_code == 200, r_home_done.text

        switch_mid = f"{prefix}_cmd_sw_ws_a1"
        switch_json = {
            "cmd": "workspace_switch",
            "platform": "discord",
            "platform_user_id": puid_a,
            "chat_type": "group",
            "chat_address": parent_addr,
            "scope_key": thread_scope_key,
            "message_id": switch_mid,
            "workspace_id": ws_a1,
            "target_kind": "workspace_id",
        }
        r_sw = await client.post(
            "/api/v1/dalton/channels/internal/session/command",
            headers=headers,
            json=switch_json,
        )
        assert r_sw.status_code == 200, r_sw.text
        sw_data = r_sw.json()
        assert sw_data["ok"] is True
        assert sw_data["duplicate"] is False
        assert sw_data["switched"] is True
        assert sw_data["workspace_id"] == ws_a1
        assert sw_data["override_workspace_id"] == ws_a1
        assert sw_data["is_home"] is False
        assert sw_data["generation"] == 2
        assert sw_data["command_effect_status"] == "done"
        assert sw_data["command_reply_pending"] is True
        assert sw_data["external_thread_id"] == (
            f"discord:{thread_scope_key}:user:{user_a}:gen:2"
        )

        r_sw_replay = await client.post(
            "/api/v1/dalton/channels/internal/session/command",
            headers=headers,
            json=switch_json,
        )
        assert r_sw_replay.status_code == 200, r_sw_replay.text
        sw_replay = r_sw_replay.json()
        assert sw_replay["ok"] is True
        assert sw_replay["duplicate"] is True
        assert sw_replay["generation"] == 2
        assert sw_replay["workspace_id"] == ws_a1
        assert sw_replay["override_workspace_id"] == ws_a1
        assert sw_replay["command_effect_status"] == "done"
        assert sw_replay["command_reply_pending"] is True
        assert sw_replay["external_thread_id"] == sw_data["external_thread_id"]

        async with db_env["pool"].connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT generation, override_workspace_id::text AS override_workspace_id
                    FROM dalton_channel_session_generations
                    WHERE platform = 'discord' AND scope_key = %s AND dalton_user_id = %s
                    """,
                    (thread_scope_key, user_a),
                )
                sess_row = await cur.fetchone()
                await cur.execute(
                    """
                    SELECT event_kind,
                           status,
                           workspace_id::text AS workspace_id,
                           thread_scope_key,
                           session_generation,
                           command_effect_status,
                           command_reply_pending
                    FROM dalton_channel_inbound_events
                    WHERE platform = 'discord' AND message_id = %s
                    """,
                    (switch_mid,),
                )
                cmd_row = await cur.fetchone()
        assert sess_row is not None
        assert int(sess_row["generation"]) == 2
        assert sess_row["override_workspace_id"] == ws_a1
        assert cmd_row is not None
        assert cmd_row["event_kind"] == "command"
        assert cmd_row["status"] == "accepted"
        assert cmd_row["workspace_id"] == ws_a1
        assert cmd_row["thread_scope_key"] == thread_scope_key
        assert int(cmd_row["session_generation"]) == 2
        assert cmd_row["command_effect_status"] == "done"
        assert cmd_row["command_reply_pending"] is True

        r_sw_ack = await client.post(
            "/api/v1/dalton/channels/internal/inbound/progress",
            headers=headers,
            json={
                "platform": "discord",
                "message_id": switch_mid,
                "status": "completed",
                "command_reply_pending": False,
                "fallback_pending": False,
            },
        )
        assert r_sw_ack.status_code == 200, r_sw_ack.text
        async with db_env["pool"].connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT generation
                    FROM dalton_channel_session_generations
                    WHERE platform = 'discord' AND scope_key = %s AND dalton_user_id = %s
                    """,
                    (thread_scope_key, user_a),
                )
                sess_after_ack = await cur.fetchone()
                await cur.execute(
                    """
                    SELECT status, session_generation, command_effect_status, command_reply_pending
                    FROM dalton_channel_inbound_events
                    WHERE platform = 'discord' AND message_id = %s
                    """,
                    (switch_mid,),
                )
                cmd_after_ack = await cur.fetchone()
        assert sess_after_ack is not None
        assert int(sess_after_ack["generation"]) == 2
        assert cmd_after_ack is not None
        assert cmd_after_ack["status"] == "completed"
        assert int(cmd_after_ack["session_generation"]) == 2
        assert cmd_after_ack["command_effect_status"] == "done"
        assert cmd_after_ack["command_reply_pending"] is False

        # Register Discord guild thread inbound event via I-1 (`chat_address` = parent, `thread_scope_key` = thread)
        r_in = await client.post(
            "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
            headers=headers,
            json={
                "platform": "discord",
                "platform_user_id": puid_a,
                "chat_type": "group",
                "chat_address": parent_addr,
                "thread_scope_key": thread_scope_key,
                "message_id": inbound_mid,
                "raw_payload": {
                    "chat_id": f"{guild_id}/{parent_cid}",
                    "discord_message": {
                        "id": inbound_mid,
                        "channel_id": thread_cid,
                        "guild_id": guild_id,
                    },
                },
            },
        )
        assert r_in.status_code == 200
        in_data = r_in.json()
        assert in_data["allowed"] is True
        assert in_data["dalton_user_id"] == user_a
        assert in_data["workspace_id"] == ws_a1
        assert in_data["generation"] == 2
        assert in_data["external_thread_id"] == (
            f"discord:{thread_scope_key}:user:{user_a}:gen:2"
        )

        # Advance inbound event to streaming with turn_tid / turn_rid via I-7
        r_prog = await client.post(
            "/api/v1/dalton/channels/internal/inbound/progress",
            headers=headers,
            json={
                "platform": "discord",
                "message_id": inbound_mid,
                "status": "streaming",
                "thread_id": turn_tid,
                "run_id": turn_rid,
                "turn_index": 0,
                "fallback_pending": True,
            },
        )
        assert r_prog.status_code == 200

        # 5. I-4 (`GET /internal/state`) must include `thread_scope_key` in `current_conversation`
        r_state = await client.get(
            "/api/v1/dalton/channels/internal/state",
            headers=headers,
            params={
                "user_id": user_a,
                "platform": "discord",
                "thread_id": turn_tid,
                "run_id": turn_rid,
            },
        )
        assert r_state.status_code == 200
        cur_conv = r_state.json()["current_conversation"]
        assert cur_conv is not None
        assert cur_conv["address"] == parent_addr
        assert cur_conv["thread_scope_key"] == thread_scope_key

        # 6. I-6 (`POST /internal/deliveries/authorize-and-claim`) for current conversation (omitted `target`)
        dlv_key_cur = f"send:{turn_rid}:tc-pg-cur-1"
        r_claim = await client.post(
            "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
            headers=headers,
            json={
                "delivery_key": dlv_key_cur,
                "kind": "agent_send",
                "user_id": user_a,
                "thread_id": turn_tid,
                "run_id": turn_rid,
                "platform": "discord",
                "target": None,
                "has_non_empty_text": True,
                "total_chunks": 1,
            },
        )
        assert r_claim.status_code == 200
        claim_data = r_claim.json()
        assert claim_data["claim_status"] == "claimed"
        assert claim_data["is_current"] is True
        assert claim_data["target_address"] == parent_addr
        assert claim_data["thread_scope_key"] == thread_scope_key

        # Duplicate claim while in_flight preserves `thread_scope_key`
        r_dup_inflight = await client.post(
            "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
            headers=headers,
            json={
                "delivery_key": dlv_key_cur,
                "kind": "agent_send",
                "user_id": user_a,
                "thread_id": turn_tid,
                "run_id": turn_rid,
                "platform": "discord",
                "target": None,
                "has_non_empty_text": True,
                "total_chunks": 1,
            },
        )
        assert r_dup_inflight.status_code == 200
        assert r_dup_inflight.json()["claim_status"] == "in_flight_or_unknown"
        assert r_dup_inflight.json()["thread_scope_key"] == thread_scope_key

        # Complete delivery via I-7 and verify `sent_to_current_count` increments to 1
        r_dlv_done = await client.post(
            "/api/v1/dalton/channels/internal/deliveries/progress",
            headers=headers,
            json={
                "delivery_key": dlv_key_cur,
                "sent_chunks": 1,
                "total_chunks": 1,
                "platform_message_ids": ["dc-out-1"],
                "status": "sent",
                "first_response_json": {
                    "status": "sent",
                    "code": None,
                    "address": f"{parent_addr}:thread:{thread_cid}",
                    "current": True,
                    "duplicate": False,
                    "message": "Sent.",
                },
            },
        )
        assert r_dlv_done.status_code == 200
        assert r_dlv_done.json()["sent_to_current_count"] == 1

        # 7. Retry claim after `failed` delivery recovers `thread_scope_key`
        dlv_key_retry = f"send:{turn_rid}:tc-pg-retry-1"
        await client.post(
            "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
            headers=headers,
            json={
                "delivery_key": dlv_key_retry,
                "kind": "agent_send",
                "user_id": user_a,
                "thread_id": turn_tid,
                "run_id": turn_rid,
                "platform": "discord",
                "target": None,
                "has_non_empty_text": True,
                "total_chunks": 1,
            },
        )
        await client.post(
            "/api/v1/dalton/channels/internal/deliveries/progress",
            headers=headers,
            json={
                "delivery_key": dlv_key_retry,
                "sent_chunks": 0,
                "total_chunks": 1,
                "status": "failed",
                "error_code": "platform_error",
                "error_message": "HTTP 400",
            },
        )
        r_reclaim = await client.post(
            "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
            headers=headers,
            json={
                "delivery_key": dlv_key_retry,
                "kind": "agent_send",
                "user_id": user_a,
                "thread_id": turn_tid,
                "run_id": turn_rid,
                "platform": "discord",
                "target": None,
                "has_non_empty_text": True,
                "total_chunks": 1,
            },
        )
        assert r_reclaim.status_code == 200
        assert r_reclaim.json()["claim_status"] == "claimed"
        assert r_reclaim.json()["is_current"] is True
        assert r_reclaim.json()["thread_scope_key"] == thread_scope_key

        # 8. Explicit `target` to `other_allowed_addr` (bound to `ws_b_shared_rw` with `rw` sharing) -> allowed (`is_current = False`)
        r_explicit_other = await client.post(
            "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
            headers=headers,
            json={
                "delivery_key": f"send:{turn_rid}:tc-pg-other-1",
                "kind": "agent_send",
                "user_id": user_a,
                "thread_id": turn_tid,
                "run_id": turn_rid,
                "platform": "discord",
                "target": other_allowed_addr,
                "has_non_empty_text": True,
                "total_chunks": 1,
            },
        )
        assert r_explicit_other.status_code == 200
        assert r_explicit_other.json()["claim_status"] == "claimed"
        assert r_explicit_other.json()["is_current"] is False
        assert r_explicit_other.json()["target_address"] == other_allowed_addr
        assert r_explicit_other.json()["thread_scope_key"] is None

        # Explicit `target` to `private_b_addr` (bound to `user_b`'s private workspace `ws_b_private`) -> denied (`not_allowed`)
        r_priv_b = await client.post(
            "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
            headers=headers,
            json={
                "delivery_key": f"send:{turn_rid}:tc-pg-priv-b-1",
                "kind": "agent_send",
                "user_id": user_a,
                "thread_id": turn_tid,
                "run_id": turn_rid,
                "platform": "discord",
                "target": private_b_addr,
                "has_non_empty_text": True,
                "total_chunks": 1,
            },
        )
        assert r_priv_b.status_code == 200
        assert r_priv_b.json()["claim_status"] == "denied"
        assert r_priv_b.json()["code"] == "not_allowed"

        # Explicit `target` to `unauthorized_addr` -> denied (`not_allowed`)
        r_unauth = await client.post(
            "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
            headers=headers,
            json={
                "delivery_key": f"send:{turn_rid}:tc-pg-unauth-1",
                "kind": "agent_send",
                "user_id": user_a,
                "thread_id": turn_tid,
                "run_id": turn_rid,
                "platform": "discord",
                "target": unauthorized_addr,
                "has_non_empty_text": True,
                "total_chunks": 1,
            },
        )
        assert r_unauth.status_code == 200
        assert r_unauth.json()["claim_status"] == "denied"
        assert r_unauth.json()["code"] == "not_allowed"

        # Real workspace write revocation: downgrade `ws_b_shared_rw` to `ro` in `dalton_workspace_sharing`
        # and verify `I-6` immediately denies delivery to `other_allowed_addr` via real `require_workspace_access`!
        await dalton_sharing.upsert_workspace_sharing(
            ws_b_shared_rw, cos_access="ro", web_access="ro", updated_by=user_b
        )
        r_revoked = await client.post(
            "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
            headers=headers,
            json={
                "delivery_key": f"send:{turn_rid}:tc-pg-revoked-1",
                "kind": "agent_send",
                "user_id": user_a,
                "thread_id": turn_tid,
                "run_id": turn_rid,
                "platform": "discord",
                "target": other_allowed_addr,
                "has_non_empty_text": True,
                "total_chunks": 1,
            },
        )
        assert r_revoked.status_code == 200
        assert r_revoked.json()["claim_status"] == "denied"
        assert r_revoked.json()["code"] == "not_allowed"

        # Restore `rw` sharing on `ws_b_shared_rw`
        await dalton_sharing.upsert_workspace_sharing(
            ws_b_shared_rw, cos_access="rw", web_access="rw", updated_by=user_b
        )

        # 9. Background run & thread watch current-conversation resolution + I-7 recovery
        r_bg_prog = await client.post(
            "/api/v1/dalton/channels/internal/inbound/progress",
            headers=headers,
            json={
                "thread_watch": {
                    "thread_id": turn_tid,
                    "dalton_user_id": user_a,
                    "platform": "discord",
                    "target_address": parent_addr,
                    "scope_key": thread_scope_key,
                    "generation": 2,
                    "last_seen_turn_index": 0,
                    "watch_active": True,
                },
                "background_run": {
                    "run_id": bg_rid,
                    "thread_id": turn_tid,
                    "dalton_user_id": user_a,
                    "platform": "discord",
                    "target_address": parent_addr,
                    "turn_index": 1,
                    "status": "running",
                    "fallback_pending": True,
                },
            },
        )
        assert r_bg_prog.status_code == 200

        r_bg_claim = await client.post(
            "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
            headers=headers,
            json={
                "delivery_key": f"send:{bg_rid}:tc-pg-bg-1",
                "kind": "agent_send",
                "user_id": user_a,
                "thread_id": turn_tid,
                "run_id": bg_rid,
                "platform": "discord",
                "target": None,
                "has_non_empty_text": True,
                "total_chunks": 1,
            },
        )
        assert r_bg_claim.status_code == 200
        assert r_bg_claim.json()["claim_status"] == "claimed"
        assert r_bg_claim.json()["is_current"] is True
        assert r_bg_claim.json()["thread_scope_key"] == thread_scope_key

        # Watch-only thread resolution (`watch_tid`)
        await client.post(
            "/api/v1/dalton/channels/internal/inbound/progress",
            headers=headers,
            json={
                "thread_watch": {
                    "thread_id": watch_tid,
                    "dalton_user_id": user_a,
                    "platform": "discord",
                    "target_address": parent_addr,
                    "scope_key": watch_scope_key,
                    # This watch scope was not switched, so its session generation stays 1.
                    "generation": 1,
                    "last_seen_turn_index": 0,
                    "watch_active": True,
                },
            },
        )
        r_watch_claim = await client.post(
            "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
            headers=headers,
            json={
                "delivery_key": f"send:{watch_rid}:tc-pg-watch-1",
                "kind": "agent_send",
                "user_id": user_a,
                "thread_id": watch_tid,
                "run_id": watch_rid,
                "platform": "discord",
                "target": None,
                "has_non_empty_text": True,
                "total_chunks": 1,
            },
        )
        assert r_watch_claim.status_code == 200
        assert r_watch_claim.json()["claim_status"] == "claimed"
        assert r_watch_claim.json()["is_current"] is True
        assert r_watch_claim.json()["thread_scope_key"] == watch_scope_key

        # I-7 `recover` (`mode=startup` and `mode=watch_reconnect`) returns `scope_key` and `thread_scope_key`
        r_rec_startup = await client.get(
            "/api/v1/dalton/channels/internal/recover",
            headers=headers,
            params={"mode": "startup"},
        )
        assert r_rec_startup.status_code == 200
        rec_startup = r_rec_startup.json()
        bg_item = next(
            r for r in rec_startup["background_runs"] if r["run_id"] == bg_rid
        )
        assert bg_item["scope_key"] == thread_scope_key
        assert bg_item["thread_scope_key"] == thread_scope_key

        r_rec_watch = await client.get(
            "/api/v1/dalton/channels/internal/recover",
            headers=headers,
            params={"mode": "watch_reconnect", "thread_id": turn_tid},
        )
        assert r_rec_watch.status_code == 200
        rec_watch = r_rec_watch.json()
        assert rec_watch["background_runs"][0]["thread_scope_key"] == thread_scope_key
        assert rec_watch["thread_watches"][0]["thread_scope_key"] == thread_scope_key


def _create_real_http_test_app(
    pool: AsyncConnectionPool,
    *,
    bootstrap_state: dict[str, Any],
    cleanup_workspace_ids: list[str],
    cleanup_thread_ids: list[str],
) -> FastAPI:
    """Create a FastAPI app mounting real `channels_router` backed by isolated Postgres (`d030`)
    plus stub LLM thread/replay/file endpoints so only external Discord APIs and LLM SSE are stubbed.
    """
    app = FastAPI()
    app.include_router(channels_router, prefix="/api/v1/dalton")

    harness_control: dict[str, Any] = {
        "hold_turn_sse": False,
        "turn_metadata_emitted": asyncio.Event(),
        "release_turn_sse": asyncio.Event(),
        "latest_thread_id": None,
        "latest_run_id": None,
        "replay_runs": {},
    }

    async def _handle_thread_turn(
        request: Request, existing_thread_id: str | None = None
    ) -> StreamingResponse:
        user_id = (request.headers.get("X-User-Id") or "").strip()
        body = await request.json()
        ws_id = str(body.get("workspace_id") or cg_db.get_home_workspace_id(user_id))
        platform = str(body.get("platform") or "discord")
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
                        VALUES (%s, %s, %s, 'in_progress', 1, %s, %s, NOW(), NOW())
                        """,
                        (thread_id, ws_id, user_id, ext_id, platform),
                    )
                else:
                    await cur.execute(
                        """
                        UPDATE conversation_threads
                        SET thread_index = thread_index + 1,
                            current_status = 'in_progress',
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
                    VALUES (%s, %s, %s, 'in_progress', %s, NOW())
                    """,
                    (run_id, thread_id, turn_index, user_id),
                )

        harness_control["latest_thread_id"] = thread_id
        harness_control["latest_run_id"] = run_id
        should_hold = bool(harness_control["hold_turn_sse"])
        reply_text = (
            f"[stub-reply ws={ws_id} thread={thread_id} turn={turn_index}] {user_text}".strip()
        )

        async def _sse_gen():
            meta = {
                "thread_id": thread_id,
                "run_id": run_id,
                "turn_index": turn_index,
                "workspace_id": ws_id,
            }
            yield f"id: 1\nevent: metadata\ndata: {json.dumps(meta)}\n\n"
            harness_control["turn_metadata_emitted"].set()
            if should_hold:
                await harness_control["release_turn_sse"].wait()
            async with pool.connection() as finish_conn:
                async with finish_conn.cursor() as finish_cur:
                    await finish_cur.execute(
                        """
                        UPDATE conversation_responses
                        SET status = 'completed'
                        WHERE conversation_response_id = %s
                        """,
                        (run_id,),
                    )
                    await finish_cur.execute(
                        """
                        UPDATE conversation_threads
                        SET current_status = 'completed', updated_at = NOW()
                        WHERE conversation_thread_id = %s
                        """,
                        (thread_id,),
                    )
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

    @app.get("/api/v1/threads/{thread_id}/messages/replay")
    async def replay_thread_messages_stub(thread_id: str) -> StreamingResponse:
        replay_cfg = harness_control["replay_runs"].get(thread_id) or {}
        run_id = str(replay_cfg.get("run_id") or harness_control["latest_run_id"] or uuid4())
        turn_index = int(replay_cfg.get("turn_index") or 1)
        text = str(
            replay_cfg.get("text")
            or f"Recovered background thread reply for {thread_id}"
        )

        async def _replay_gen():
            u_msg = {
                "turn_index": turn_index,
                "run_id": run_id,
                "run_completed_at": "2026-01-01T00:00:00Z",
                "content": "background trigger",
            }
            yield f"event: user_message\ndata: {json.dumps(u_msg)}\n\n"
            chunk = {
                "turn_index": turn_index,
                "response_id": run_id,
                "run_id": run_id,
                "agent": "main",
                "role": "assistant",
                "content_type": "text",
                "content": text,
                "finish_reason": "stop",
            }
            yield f"event: message_chunk\ndata: {json.dumps(chunk)}\n\n"
            done = {
                "has_more": False,
                "oldest_turn_index": turn_index,
                "latest_turn_index": turn_index,
                "latest_run_id": run_id,
            }
            yield f"event: replay_done\ndata: {json.dumps(done)}\n\n"

        return StreamingResponse(_replay_gen(), media_type="text/event-stream")

    @app.post("/api/v1/threads/{thread_id}/cancel")
    async def cancel_thread_stub(thread_id: str) -> dict[str, Any]:
        return {"status": "cancelled", "thread_id": thread_id}

    @app.get("/api/v1/threads/{thread_id}/status")
    async def status_thread_stub(thread_id: str) -> dict[str, Any]:
        return {
            "thread_id": thread_id,
            "run_id": None,
            "status": "completed",
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

    @app.get("/api/v1/workspaces/{workspace_id}/files/download")
    async def download_workspace_file_stub(
        workspace_id: str, path: str = ""
    ) -> Response:
        return Response(
            content=f"file-bytes:{workspace_id}:{path}".encode("utf-8"),
            media_type="image/png",
        )

    @app.get("/test-harness/bootstrap")
    async def test_harness_bootstrap() -> dict[str, Any]:
        return bootstrap_state

    @app.post("/test-harness/turn-gate")
    async def test_harness_turn_gate(request: Request) -> dict[str, Any]:
        body = await request.json()
        action = str(body.get("action") or "")
        if action == "arm":
            harness_control["hold_turn_sse"] = True
            harness_control["turn_metadata_emitted"] = asyncio.Event()
            harness_control["release_turn_sse"] = asyncio.Event()
            return {"ok": True}
        if action == "wait_streaming":
            await asyncio.wait_for(
                harness_control["turn_metadata_emitted"].wait(), timeout=10.0
            )
            # Wait until GatewayOrchestrator writes thread_id & run_id to dalton_channel_inbound_events via I-7
            message_id = body.get("message_id")
            if message_id:
                for _ in range(100):
                    async with pool.connection() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                """
                                SELECT thread_id::text AS thread_id, run_id, status
                                FROM dalton_channel_inbound_events
                                WHERE platform = 'discord' AND message_id = %s
                                """,
                                (str(message_id),),
                            )
                            row = await cur.fetchone()
                            if row and row.get("thread_id") and row.get("run_id"):
                                return {
                                    "ok": True,
                                    "thread_id": row["thread_id"],
                                    "run_id": row["run_id"],
                                    "status": row["status"],
                                }
                    await asyncio.sleep(0.02)
            return {
                "ok": True,
                "thread_id": harness_control["latest_thread_id"],
                "run_id": harness_control["latest_run_id"],
            }
        if action == "release":
            harness_control["hold_turn_sse"] = False
            harness_control["release_turn_sse"].set()
            return {"ok": True}
        if action == "configure_replay":
            tid = str(body["thread_id"])
            harness_control["replay_runs"][tid] = {
                "run_id": body["run_id"],
                "turn_index": int(body.get("turn_index", 1)),
                "text": str(body["text"]),
            }
            return {"ok": True}
        raise HTTPException(status_code=400, detail=f"Unknown action {action!r}")

    @app.get("/test-harness/inspect")
    async def test_harness_inspect(
        platform: str = "discord",
        message_id: str | None = None,
        delivery_key: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                inbound_row = None
                if message_id:
                    await cur.execute(
                        """
                        SELECT message_id,
                               event_kind,
                               status,
                               dalton_user_id,
                               chat_address,
                               thread_scope_key,
                               workspace_id::text AS workspace_id,
                               external_thread_id,
                               session_generation,
                               thread_id::text AS thread_id,
                               run_id,
                               command_request,
                               command_result,
                               command_effect_status,
                               command_reply_pending,
                               fallback_pending,
                               sent_to_current_count
                        FROM dalton_channel_inbound_events
                        WHERE platform = %s AND message_id = %s
                        """,
                        (platform, message_id),
                    )
                    ir = await cur.fetchone()
                    if ir:
                        inbound_row = dict(ir)

                delivery_row = None
                if delivery_key:
                    await cur.execute(
                        """
                        SELECT
                            EXISTS (
                                SELECT 1
                                FROM information_schema.columns
                                WHERE table_schema = 'public'
                                  AND table_name = 'dalton_channel_deliveries'
                                  AND column_name = 'confirmed_thread_address'
                            ) AS has_confirmed_thread_address,
                            EXISTS (
                                SELECT 1
                                FROM information_schema.columns
                                WHERE table_schema = 'public'
                                  AND table_name = 'dalton_channel_deliveries'
                                  AND column_name = 'discord_body_phase'
                            ) AS has_discord_body_phase,
                            EXISTS (
                                SELECT 1
                                FROM information_schema.columns
                                WHERE table_schema = 'public'
                                  AND table_name = 'dalton_channel_deliveries'
                                  AND column_name = 'delivery_attempt'
                            ) AS has_delivery_attempt
                        """
                    )
                    exists_row = await cur.fetchone()
                    has_confirmed_thread = bool(
                        exists_row
                        and exists_row.get("has_confirmed_thread_address")
                    )
                    has_body_phase = bool(
                        exists_row and exists_row.get("has_discord_body_phase")
                    )
                    has_attempt = bool(
                        exists_row and exists_row.get("has_delivery_attempt")
                    )
                    # Constants chosen from the catalog check. Not user input.
                    confirmed_column_sql = (
                        "confirmed_thread_address"
                        if has_confirmed_thread
                        else "NULL::varchar AS confirmed_thread_address"
                    )
                    body_phase_column_sql = (
                        "discord_body_phase"
                        if has_body_phase
                        else "NULL::varchar AS discord_body_phase"
                    )
                    attempt_column_sql = (
                        "delivery_attempt"
                        if has_attempt
                        else "0::integer AS delivery_attempt"
                    )
                    await cur.execute(
                        f"""
                        SELECT delivery_key,
                               kind,
                               user_id,
                               thread_id::text AS thread_id,
                               run_id,
                               is_current,
                               platform,
                               target_address,
                               status,
                               sent_chunks,
                               total_chunks,
                               platform_message_ids,
                               files_status,
                               error_code,
                               first_response_json,
                               {confirmed_column_sql},
                               {body_phase_column_sql},
                               {attempt_column_sql}
                        FROM dalton_channel_deliveries
                        WHERE delivery_key = %s
                        """,
                        (delivery_key,),
                    )
                    dr = await cur.fetchone()
                    if dr:
                        delivery_row = dict(dr)

                bg_row = None
                if run_id:
                    await cur.execute(
                        """
                        SELECT run_id,
                               thread_id::text AS thread_id,
                               dalton_user_id,
                               platform,
                               target_address,
                               turn_index,
                               status,
                               fallback_pending,
                               sent_to_current_count
                        FROM dalton_channel_background_runs
                        WHERE run_id = %s
                        """,
                        (run_id,),
                    )
                    br = await cur.fetchone()
                    if br:
                        bg_row = dict(br)

        return {
            "inbound": inbound_row,
            "delivery": delivery_row,
            "background_run": bg_row,
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


async def _seed_real_http_discord_fixture(
    pool: AsyncConnectionPool,
    *,
    prefix: str,
    user_a: str,
    user_b: str,
    ws_a1: str,
    ws_b_shared_rw: str,
    ws_b_private: str,
) -> dict[str, Any]:
    """Seed Discord identities, group inbound membership, and full channel settings in Postgres (`d030`)."""
    service_token = "0123456789abcdef0123456789abcdef_real_http_token"
    os.environ["INTERNAL_SERVICE_TOKEN"] = service_token
    os.environ["DALTON_AUTH_MODE"] = "supabase"
    os.environ["DALTON_COLLEAGUE_DISPATCH_ENABLED"] = "true"
    os.environ["DALTON_ALLOWED_EMAILS"] = (
        f"{user_a}@example.com,{user_b}@example.com"
    )
    app_settings.HOST_MODE = "oss"

    puid_a = f"{prefix}_dc_http_a"
    puid_b = f"{prefix}_dc_http_b"
    guild_id = "700000001"
    parent_cid = "700000002"
    thread_cid = "700000003"
    other_cid = "700000099"
    private_b_cid = "700000088"
    unauth_cid = "700000077"

    parent_addr = f"discord:{guild_id}/{parent_cid}"
    other_allowed_addr = f"discord:{guild_id}/{other_cid}"
    private_b_addr = f"discord:{guild_id}/{private_b_cid}"
    unauthorized_addr = f"discord:{guild_id}/{unauth_cid}"
    thread_scope_key = f"chat:{guild_id}/{parent_cid}:thread:{thread_cid}"

    code_a = await cg_db.create_bind_code(user_id=user_a, platform="discord")
    code_b = await cg_db.create_bind_code(user_id=user_b, platform="discord")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="discord",
        platform_user_id=puid_a,
        code=code_a["code"],
        is_group=False,
    )
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="discord",
        platform_user_id=puid_b,
        code=code_b["code"],
        is_group=False,
    )

    # Register group membership inbound events before binding group chats in user settings
    for mid, puid, addr in (
        (f"{prefix}_mem_parent_a", puid_a, parent_addr),
        (f"{prefix}_mem_other_a", puid_a, other_allowed_addr),
        (f"{prefix}_mem_priv_b", puid_b, private_b_addr),
    ):
        await cg_db.register_and_resolve_inbound(
            platform="discord",
            platform_user_id=puid,
            chat_type="group",
            chat_address=addr,
            thread_scope_key=f"chat:{addr.split(':', 1)[1]}",
            message_id=mid,
            raw_payload={"platform_user_id": puid},
        )
        await cg_db.update_inbound_progress(
            platform="discord", message_id=mid, status="completed"
        )

    await cg_db.update_user_channel_settings(
        user_id=user_a,
        settings={
            "default": {"workspace_id": ws_a1},
            "discord": {
                "preferred": "discord:@me",
                "automation_output": {ws_a1: parent_addr},
                "agent_messages": {
                    "enabled": True,
                    "allowed": [
                        "discord:@me",
                        parent_addr,
                        other_allowed_addr,
                        private_b_addr,
                    ],
                },
                "chats": {
                    parent_addr: {
                        "name": "Equities Channel",
                        "workspace_id": ws_a1,
                    },
                    other_allowed_addr: {
                        "name": "Alerts Channel",
                        "workspace_id": ws_b_shared_rw,
                    },
                },
            },
        },
    )

    # User B binds `private_b_addr` to `ws_b_private` (which User A does NOT have write access to)
    await cg_db.update_user_channel_settings(
        user_id=user_b,
        settings={
            "discord": {
                "preferred": "discord:@me",
                "automation_output": {},
                "agent_messages": {
                    "enabled": True,
                    "allowed": ["discord:@me", private_b_addr],
                },
                "chats": {
                    private_b_addr: {
                        "name": "Private B Channel",
                        "workspace_id": ws_b_private,
                    },
                },
            },
        },
    )

    # Also add `private_b_addr` into User A's `chats` JSON with `workspace_id: None` so `I-6` reaches
    # the real `dalton_channel_chat_bindings.workspace_id` (`ws_b_private`) write-permission check!
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_user_settings
                SET apps = jsonb_set(
                    apps,
                    ARRAY['discord', 'chats', %s],
                    '{"name": "Private B Channel", "workspace_id": null}'::jsonb
                )
                WHERE user_id = %s
                """,
                (private_b_addr, user_a),
            )

    return {
        "prefix": prefix,
        "service_token": service_token,
        "user_a": user_a,
        "user_b": user_b,
        "home_ws_a": cg_db.get_home_workspace_id(user_a),
        "ws_a1": ws_a1,
        "ws_b_shared_rw": ws_b_shared_rw,
        "ws_b_private": ws_b_private,
        "puid_a": puid_a,
        "puid_b": puid_b,
        "guild_id": guild_id,
        "parent_cid": parent_cid,
        "thread_cid": thread_cid,
        "other_cid": other_cid,
        "private_b_cid": private_b_cid,
        "unauth_cid": unauth_cid,
        "parent_addr": parent_addr,
        "other_allowed_addr": other_allowed_addr,
        "private_b_addr": private_b_addr,
        "unauthorized_addr": unauthorized_addr,
        "thread_scope_key": thread_scope_key,
    }


def _assert_one_selected_vitest_suite(output: str, *, suite_token: str) -> None:
    """Fail unless Vitest passed exactly one test. A filter that matches nothing is not a pass."""
    normalized = output.replace("\u00a0", " ").replace("\u202f", " ")
    summaries = re.findall(r"(?m)^\s*Tests\s+(.+?)\s*$", normalized)
    assert summaries, f"Vitest did not print a Tests summary:\n{output}"
    body = summaries[-1]
    passed_match = re.search(r"(\d+)\s+passed", body)
    failed_match = re.search(r"(\d+)\s+failed", body)
    passed_n = int(passed_match.group(1)) if passed_match else 0
    failed_n = int(failed_match.group(1)) if failed_match else 0
    assert failed_n == 0, f"Vitest reported {failed_n} failed test(s):\n{output}"
    assert passed_n == 1, (
        "Expected exactly 1 selected real HTTP suite to pass "
        f"(got {passed_n}). A selector that matches nothing is not a pass:\n{output}"
    )
    assert suite_token in normalized, (
        f"Vitest output did not include the selected suite {suite_token!r}:\n{output}"
    )


@pytest.mark.asyncio
async def test_real_http_discord_gateway_orchestrator_to_fastapi_and_postgres(
    db_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end real HTTP test: DiscordChannelAdapter -> GatewayOrchestrator -> POST /agent/send ->
    HttpBackendClient -> live FastAPI channels_router -> isolated PostgreSQL (`d030`).
    """
    pool: AsyncConnectionPool = db_env["pool"]
    bootstrap = await _seed_real_http_discord_fixture(
        pool,
        prefix=db_env["prefix"],
        user_a=db_env["user_a"],
        user_b=db_env["user_b"],
        ws_a1=db_env["ws_a1"],
        ws_b_shared_rw=db_env["ws_b_shared_rw"],
        ws_b_private=db_env["ws_b_private"],
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
            "DALTON_REAL_HTTP_SUITE": "discord",
        }
        proc = await asyncio.create_subprocess_exec(
            str(vitest_bin),
            "run",
            "test/real-http-postgres.test.ts",
            "-t",
            "DiscordChannelAdapter",
            "--reporter=verbose",
            "--pool=forks",
            "--poolOptions.forks.singleFork=true",
            cwd=str(gateway_dir),
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=60.0
            )
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

        output = stdout_bytes.decode("utf-8", errors="replace")
        assert proc.returncode == 0, f"Vitest real HTTP test failed:\n{output}"
        _assert_one_selected_vitest_suite(
            output, suite_token="DiscordChannelAdapter"
        )


async def serve_discord_http_harness(*, dalton_revision: str = "d031") -> None:
    """Serve the Discord HTTP harness until SIGINT or SIGTERM.

    ``dalton_revision`` is the minimum Dalton revision. The default stays d031
    for the existing real HTTP suite. A descendant already applied is not
    downgraded. The confirmed-thread suite passes ``d036``.
    """
    dsn = _get_test_dsn()
    os.environ.setdefault(
        "BYOK_ENCRYPTION_KEY", "test-channel-gateway-encryption-key-32b"
    )
    await _ensure_alembic_upgraded_to_d031(dsn, minimum=dalton_revision)

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

    for mod in (
        db_pool,
        cg_db,
        dalton_sharing,
        dalton_invites,
        user_db,
        workspace_db,
    ):
        setattr(mod, "get_db_connection", _pooled_get_db_connection)

    prefix = f"ch1_http_{uuid4().hex[:8]}"
    user_a = f"{prefix}_user_a"
    user_b = f"{prefix}_user_b"
    ws_a1 = str(uuid4())
    ws_b_shared_rw = str(uuid4())
    ws_b_private = str(uuid4())
    cleanup_user_ids = [user_a, user_b]
    cleanup_workspace_ids = [ws_a1, ws_b_shared_rw, ws_b_private]
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
                    (
                        user_a,
                        f"{user_a}@example.com",
                        user_b,
                        f"{user_b}@example.com",
                    ),
                )
                await cur.execute(
                    """
                    INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                    VALUES (%s, %s, %s, 'running', NOW(), NOW()),
                           (%s, %s, %s, 'running', NOW(), NOW()),
                           (%s, %s, %s, 'running', NOW(), NOW())
                    """,
                    (
                        ws_a1,
                        user_a,
                        f"{prefix}_WS_A1",
                        ws_b_shared_rw,
                        user_b,
                        f"{prefix}_WS_B_Shared_RW",
                        ws_b_private,
                        user_b,
                        f"{prefix}_WS_B_Private",
                    ),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_workspace_sharing (
                        workspace_id, cos_access, web_access, updated_by, updated_at
                    )
                    VALUES (%s, 'rw', 'rw', %s, NOW()),
                           (%s, 'hidden', 'hidden', %s, NOW())
                    """,
                    (ws_b_shared_rw, user_b, ws_b_private, user_b),
                )

        bootstrap = await _seed_real_http_discord_fixture(
            pool,
            prefix=prefix,
            user_a=user_a,
            user_b=user_b,
            ws_a1=ws_a1,
            ws_b_shared_rw=ws_b_shared_rw,
            ws_b_private=ws_b_private,
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
    asyncio.run(serve_discord_http_harness())
