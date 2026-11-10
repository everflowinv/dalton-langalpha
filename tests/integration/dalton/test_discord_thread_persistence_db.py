"""Real PostgreSQL tests for a confirmed Discord thread, its body phase, and claim generation (d036).

The columns are applied by the Dalton Alembic chain. Claim, progress, and startup
recovery call the real channel-gateway functions. This file does not ALTER a
stamped schema by hand. A Discord ``data_task`` row is inserted with the real
columns and updated through I-7, including the HTTP route. This branch has no
data_task claim handler. Startup recovery between the dispatch acknowledgment
and the final result is also covered here. That test calls the real recovery
function, which rewrites every ``in_flight`` delivery on the database.

``--serve-http-harness`` upgrades to at least d036 and serves the Discord HTTP
app used by the gateway vitest. Startup recovery rewrites every ``in_flight``
delivery on the database, so do not run this file while another suite still
has in-flight rows there. The pool is function-scoped so its workers share the
test's running event loop.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import sys
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio

from src.dalton import invites as dalton_invites
from src.dalton import sharing as dalton_sharing
from src.dalton.app.channels_router import channels_router
from src.dalton.database import channel_gateway as cg_db
from src.server.database import pool as db_pool
from src.server.database import user as user_db
from src.server.database import workspace as workspace_db
from tests.integration.dalton.test_discord_gateway_db import (
    _cleanup_test_rows,
    _ensure_alembic_upgraded_to_d036,
    _get_test_dsn,
    _run_ephemeral_fastapi_server,
    _seed_real_http_discord_fixture,
    serve_discord_http_harness,
)

pytestmark = pytest.mark.integration

THREAD_ID = "700000555"
OTHER_THREAD_ID = "700000556"


@pytest.fixture(scope="session", autouse=True)
def dalton_schema():
    """Override the directory autouse fixture so an unset DSN skips without opening test_db_pool."""
    yield


@pytest.fixture(autouse=True)
def cleanup_dalton_tables():
    """Override the directory autouse fixture so an unset DSN skips without opening test_db_pool."""
    yield


@pytest_asyncio.fixture(scope="function", loop_scope="function")
async def thread_persistence_pool():
    """Upgrade the isolated test database to at least d036 and yield a pool.

    The pool is opened on this test's loop. A session-scoped pool leaves its
    workers on the session loop, and a later checkout from the function loop
    waits until that frozen loop grows the pool.
    """
    dsn = _get_test_dsn()
    os.environ.setdefault(
        "BYOK_ENCRYPTION_KEY", "test-channel-gateway-encryption-key-32b"
    )
    await _ensure_alembic_upgraded_to_d036(dsn, minimum="d036")
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


@pytest_asyncio.fixture(loop_scope="function")
async def seeded_env(
    thread_persistence_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
):
    """Point channel-gateway calls at the test pool and seed one Discord group."""

    @asynccontextmanager
    async def _pooled_get_db_connection(existing_conn=None):
        if existing_conn is not None:
            yield existing_conn
            return
        async with thread_persistence_pool.connection() as conn:
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

    prefix = f"thr036_{uuid4().hex[:8]}"
    user_a = f"{prefix}_user_a"
    user_b = f"{prefix}_user_b"
    ws_a1 = str(uuid4())
    ws_b_shared_rw = str(uuid4())
    ws_b_private = str(uuid4())
    cleanup_user_ids = [user_a, user_b]
    cleanup_workspace_ids = [ws_a1, ws_b_shared_rw, ws_b_private]

    async with thread_persistence_pool.connection() as conn:
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
        thread_persistence_pool,
        prefix=prefix,
        user_a=user_a,
        user_b=user_b,
        ws_a1=ws_a1,
        ws_b_shared_rw=ws_b_shared_rw,
        ws_b_private=ws_b_private,
    )
    try:
        yield {
            "pool": thread_persistence_pool,
            "prefix": prefix,
            "user_a": user_a,
            "bootstrap": bootstrap,
        }
    finally:
        await _cleanup_test_rows(
            thread_persistence_pool,
            prefix=prefix,
            user_ids=cleanup_user_ids,
            workspace_ids=cleanup_workspace_ids,
            thread_ids=[],
        )


async def _assert_d036_column(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT version_num FROM alembic_version_dalton")
            versions = [str(row["version_num"]) for row in await cur.fetchall()]
            assert versions == ["d036"]
            await cur.execute(
                """
                SELECT data_type, character_maximum_length, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'dalton_channel_deliveries'
                  AND column_name = 'confirmed_thread_address'
                """
            )
            column = await cur.fetchone()
            assert column == {
                "data_type": "character varying",
                "character_maximum_length": 255,
                "is_nullable": "YES",
            }
            await cur.execute(
                """
                SELECT data_type, character_maximum_length, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'dalton_channel_deliveries'
                  AND column_name = 'discord_body_phase'
                """
            )
            phase_column = await cur.fetchone()
            assert phase_column == {
                "data_type": "character varying",
                "character_maximum_length": 32,
                "is_nullable": "YES",
            }
            await cur.execute(
                """
                SELECT pg_get_constraintdef(oid) AS definition
                FROM pg_constraint
                WHERE conname = 'ck_dalton_channel_deliveries_discord_body_phase'
                """
            )
            constraint = await cur.fetchone()
            assert constraint is not None
            definition = str(constraint["definition"])
            assert "before_body" in definition
            assert "dispatching" in definition
            await cur.execute(
                """
                SELECT data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'dalton_channel_deliveries'
                  AND column_name = 'delivery_attempt'
                """
            )
            attempt_column = await cur.fetchone()
            assert attempt_column is not None
            assert attempt_column["data_type"] == "integer"
            assert attempt_column["is_nullable"] == "NO"


async def _delivery_row(pool: AsyncConnectionPool, delivery_key: str) -> dict[str, Any]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT status,
                       kind,
                       platform,
                       target_address,
                       confirmed_thread_address,
                       discord_body_phase,
                       delivery_attempt,
                       error_code,
                       sent_chunks,
                       platform_message_ids,
                       first_response_json
                FROM dalton_channel_deliveries
                WHERE delivery_key = %s
                """,
                (delivery_key,),
            )
            row = await cur.fetchone()
            assert row is not None, delivery_key
            return dict(row)


async def _insert_unknown_discord_delivery(
    pool: AsyncConnectionPool,
    *,
    delivery_key: str,
    user_id: str,
    kind: str,
    target_address: str,
    delivery_attempt: int = 1,
) -> None:
    """Store one Discord delivery directly. This branch has no data_task claim path."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_channel_deliveries (
                    delivery_key, kind, user_id, platform, target_address,
                    status, delivery_attempt, error_code, error_message,
                    is_current, has_non_empty_text, total_chunks, sent_chunks
                )
                VALUES (
                    %s, %s, %s, 'discord', %s,
                    'unknown', %s, 'gateway_restarted',
                    'startup left this delivery unknown',
                    FALSE, TRUE, 1, 0
                )
                """,
                (delivery_key, kind, user_id, target_address, delivery_attempt),
            )


async def _arm_discord_dispatch(
    user_id: str, delivery_key: str, parent: str, confirmed: str
) -> int:
    """Claim one Discord send, store its thread, and record dispatching.

    This does not call Discord. The body phase is dispatching before any final
    status, which is the window startup recovery can mark unknown.
    """
    claimed = await _claim(user_id, delivery_key, parent)
    assert claimed["claim_status"] == "claimed"
    attempt = int(claimed["delivery_attempt"])
    stored = await cg_db.update_delivery_progress(
        delivery_key=delivery_key,
        delivery_attempt=attempt,
        confirmed_thread_address=confirmed,
    )
    assert stored["discord_body_phase"] == "before_body"
    marked = await cg_db.update_delivery_progress(
        delivery_key=delivery_key,
        delivery_attempt=attempt,
        discord_body_phase="dispatching",
    )
    assert marked["discord_body_phase"] == "dispatching"
    assert marked["delivery_attempt"] == attempt
    return attempt


async def _claim(user_id: str, delivery_key: str, target: str) -> dict[str, Any]:
    platform = target.split(":", 1)[0]
    return await cg_db.authorize_and_claim_delivery(
        delivery_key=delivery_key,
        kind="agent_send",
        user_id=user_id,
        platform=platform,
        target=target,
        has_non_empty_text=True,
        total_chunks=1,
    )


async def _reject_progress(
    delivery_key: str, address: str, status_code: int, code: str
) -> None:
    with pytest.raises(HTTPException) as caught:
        await cg_db.update_delivery_progress(
            delivery_key=delivery_key,
            confirmed_thread_address=address,
        )
    assert caught.value.status_code == status_code
    assert caught.value.detail["code"] == code


@pytest.mark.asyncio(loop_scope="function")
async def test_confirmed_discord_thread_survives_progress_restart_and_revocation(
    seeded_env: dict[str, Any],
) -> None:
    """Store one thread, reject changes, and reclaim it after startup recovery."""
    pool: AsyncConnectionPool = seeded_env["pool"]
    user_a = seeded_env["user_a"]
    prefix = seeded_env["prefix"]
    parent = seeded_env["bootstrap"]["parent_addr"]
    guild_id = seeded_env["bootstrap"]["guild_id"]
    parent_id = seeded_env["bootstrap"]["parent_cid"]
    other_parent = seeded_env["bootstrap"]["other_cid"]
    confirmed = f"discord:{guild_id}/{parent_id}:thread:{THREAD_ID}"
    conflict = f"discord:{guild_id}/{parent_id}:thread:{OTHER_THREAD_ID}"
    wrong_parent = f"discord:{guild_id}/{other_parent}:thread:{THREAD_ID}"
    wrong_guild = f"discord:700000009/{parent_id}:thread:{THREAD_ID}"

    await _assert_d036_column(pool)

    same_key = f"{prefix}_same"
    claimed = await _claim(user_a, same_key, parent)
    assert claimed["claim_status"] == "claimed"
    assert claimed["delivery_attempt"] == 1
    assert claimed["target_address"] == parent
    assert claimed["confirmed_thread_address"] is None

    stored = await cg_db.update_delivery_progress(
        delivery_key=same_key,
        confirmed_thread_address=confirmed,
    )
    assert stored["ok"] is True
    assert stored["confirmed_thread_address"] == confirmed
    assert stored["discord_body_phase"] == "before_body"
    repeated = await cg_db.update_delivery_progress(
        delivery_key=same_key,
        confirmed_thread_address=confirmed,
    )
    assert repeated["ok"] is True
    assert repeated["confirmed_thread_address"] == confirmed
    assert repeated["discord_body_phase"] == "before_body"
    same_row = await _delivery_row(pool, same_key)
    assert same_row["confirmed_thread_address"] == confirmed
    assert same_row["discord_body_phase"] == "before_body"

    await _reject_progress(same_key, conflict, 409, "conflict")
    await _reject_progress(same_key, wrong_parent, 400, "invalid")
    await _reject_progress(same_key, wrong_guild, 400, "invalid")
    await _reject_progress(same_key, f" {confirmed}", 400, "invalid")
    await _reject_progress(same_key, "", 400, "invalid")
    assert (await _delivery_row(pool, same_key))["confirmed_thread_address"] == confirmed
    assert (await _delivery_row(pool, same_key))["target_address"] == parent

    dm_key = f"{prefix}_dm"
    dm_claim = await _claim(user_a, dm_key, "discord:@me")
    assert dm_claim["claim_status"] == "claimed"
    await _reject_progress(dm_key, confirmed, 400, "invalid")
    dm_row = await _delivery_row(pool, dm_key)
    assert dm_row["confirmed_thread_address"] is None
    assert dm_row["discord_body_phase"] is None

    unknown_key = f"{prefix}_unknown"
    assert (await _claim(user_a, unknown_key, parent))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=unknown_key,
        status="unknown",
        error_code="unavailable",
        error_message="Discord thread creation was not confirmed",
    )
    blocked = await _claim(user_a, unknown_key, parent)
    assert blocked["claim_status"] == "in_flight_or_unknown"
    assert blocked["delivery_attempt"] == 1
    assert blocked["confirmed_thread_address"] is None
    assert blocked["discord_body_phase"] is None
    unknown_row = await _delivery_row(pool, unknown_key)
    assert unknown_row["status"] == "unknown"
    assert unknown_row["confirmed_thread_address"] is None
    assert unknown_row["discord_body_phase"] is None

    rejected_key = f"{prefix}_rejected"
    assert (await _claim(user_a, rejected_key, parent))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=rejected_key,
        confirmed_thread_address=confirmed,
    )
    await cg_db.update_delivery_progress(
        delivery_key=rejected_key,
        discord_body_phase="dispatching",
    )
    replaced = await cg_db.update_delivery_progress(
        delivery_key=rejected_key,
        status="failed",
        error_code="not_allowed",
        error_message="Discord message send failed",
        first_response_json={"status": "failed", "address": confirmed},
    )
    assert replaced["confirmed_thread_address"] == confirmed
    assert replaced["discord_body_phase"] == "dispatching"
    rejected_row = await _delivery_row(pool, rejected_key)
    assert rejected_row["status"] == "failed"
    assert rejected_row["target_address"] == parent
    assert rejected_row["confirmed_thread_address"] == confirmed
    assert rejected_row["discord_body_phase"] == "dispatching"
    assert rejected_row["first_response_json"]["status"] == "failed"

    restart_key = f"{prefix}_restart"
    assert (await _claim(user_a, restart_key, parent))["claim_status"] == "claimed"
    restart_stored = await cg_db.update_delivery_progress(
        delivery_key=restart_key,
        confirmed_thread_address=confirmed,
    )
    assert restart_stored["discord_body_phase"] == "before_body"
    still_flying = await _claim(user_a, restart_key, parent)
    assert still_flying["claim_status"] == "in_flight_or_unknown"
    assert still_flying["confirmed_thread_address"] == confirmed
    assert still_flying["discord_body_phase"] == "before_body"
    assert (await _delivery_row(pool, restart_key))["status"] == "in_flight"

    timeout_key = f"{prefix}_body_timeout"
    assert (await _claim(user_a, timeout_key, parent))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=timeout_key,
        confirmed_thread_address=confirmed,
    )
    await cg_db.update_delivery_progress(
        delivery_key=timeout_key,
        discord_body_phase="dispatching",
    )
    await cg_db.update_delivery_progress(
        delivery_key=timeout_key,
        status="unknown",
        error_code="timeout",
        error_message="Discord message response was lost",
    )
    timeout_blocked = await _claim(user_a, timeout_key, parent)
    assert timeout_blocked["claim_status"] == "in_flight_or_unknown"
    assert timeout_blocked["discord_body_phase"] == "dispatching"
    timeout_before = await _delivery_row(pool, timeout_key)
    assert timeout_before["status"] == "unknown"
    assert timeout_before["error_code"] == "timeout"
    assert timeout_before["confirmed_thread_address"] == confirmed
    assert timeout_before["discord_body_phase"] == "dispatching"
    assert timeout_before["sent_chunks"] == 0

    crash_key = f"{prefix}_startup_after_post"
    assert (await _claim(user_a, crash_key, parent))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=crash_key,
        confirmed_thread_address=confirmed,
    )
    dispatching = await cg_db.update_delivery_progress(
        delivery_key=crash_key,
        discord_body_phase="dispatching",
    )
    assert dispatching["discord_body_phase"] == "dispatching"
    repeated_dispatch = await cg_db.update_delivery_progress(
        delivery_key=crash_key,
        discord_body_phase="dispatching",
    )
    assert repeated_dispatch["discord_body_phase"] == "dispatching"
    crash_flying = await _claim(user_a, crash_key, parent)
    assert crash_flying["claim_status"] == "in_flight_or_unknown"
    assert (await _delivery_row(pool, crash_key))["status"] == "in_flight"
    assert (await _delivery_row(pool, crash_key))["discord_body_phase"] == "dispatching"

    legacy_key = f"{prefix}_legacy_null_phase"
    assert (await _claim(user_a, legacy_key, parent))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=legacy_key,
        confirmed_thread_address=confirmed,
    )
    # Progress cannot clear discord_body_phase. This statement only builds the
    # migrated row: a stored thread whose phase is still null.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_deliveries
                SET discord_body_phase = NULL,
                    status = 'unknown',
                    error_code = 'timeout',
                    error_message = 'body outcome unknown without a body phase'
                WHERE delivery_key = %s
                """,
                (legacy_key,),
            )
    with pytest.raises(HTTPException) as legacy_phase:
        await cg_db.update_delivery_progress(
            delivery_key=legacy_key,
            discord_body_phase="before_body",
        )
    assert legacy_phase.value.status_code == 409
    assert legacy_phase.value.detail["code"] == "conflict"
    legacy_blocked = await _claim(user_a, legacy_key, parent)
    assert legacy_blocked["claim_status"] == "in_flight_or_unknown"
    legacy_row = await _delivery_row(pool, legacy_key)
    assert legacy_row["status"] == "unknown"
    assert legacy_row["confirmed_thread_address"] == confirmed
    assert legacy_row["discord_body_phase"] is None
    assert legacy_row["target_address"] == parent

    await cg_db.recover_channel_state(mode="startup")

    restarted = await _delivery_row(pool, restart_key)
    assert restarted["status"] == "unknown"
    assert restarted["error_code"] == "gateway_restarted"
    assert restarted["delivery_attempt"] == 1
    assert restarted["confirmed_thread_address"] == confirmed
    assert restarted["discord_body_phase"] == "before_body"
    assert restarted["target_address"] == parent
    survived = await _delivery_row(pool, same_key)
    assert survived["status"] == "unknown"
    assert survived["confirmed_thread_address"] == confirmed
    assert survived["discord_body_phase"] == "before_body"
    assert (await _delivery_row(pool, unknown_key))["error_code"] == "unavailable"
    assert (await _delivery_row(pool, rejected_key))["status"] == "failed"
    assert (await _delivery_row(pool, rejected_key))["discord_body_phase"] == "dispatching"
    timeout_after = await _delivery_row(pool, timeout_key)
    assert timeout_after["status"] == "unknown"
    assert timeout_after["error_code"] == "timeout"
    assert timeout_after["discord_body_phase"] == "dispatching"
    crashed = await _delivery_row(pool, crash_key)
    assert crashed["status"] == "unknown"
    assert crashed["error_code"] == "gateway_restarted"
    assert crashed["confirmed_thread_address"] == confirmed
    assert crashed["discord_body_phase"] == "dispatching"
    assert crashed["target_address"] == parent
    legacy_after = await _delivery_row(pool, legacy_key)
    assert legacy_after["discord_body_phase"] is None
    assert legacy_after["error_code"] == "timeout"

    reclaimed = await _claim(user_a, restart_key, parent)
    assert reclaimed["claim_status"] == "claimed"
    assert reclaimed["delivery_attempt"] == 2
    assert reclaimed["target_address"] == parent
    assert reclaimed["confirmed_thread_address"] == confirmed
    assert reclaimed["discord_body_phase"] == "before_body"
    reclaimed_row = await _delivery_row(pool, restart_key)
    assert reclaimed_row["status"] == "in_flight"
    assert reclaimed_row["delivery_attempt"] == 2
    assert reclaimed_row["confirmed_thread_address"] == confirmed
    assert reclaimed_row["discord_body_phase"] == "before_body"

    timeout_retry = await _claim(user_a, timeout_key, parent)
    assert timeout_retry["claim_status"] == "in_flight_or_unknown"
    assert timeout_retry["delivery_attempt"] == 1
    assert (await _delivery_row(pool, timeout_key))["status"] == "unknown"
    crash_retry = await _claim(user_a, crash_key, parent)
    assert crash_retry["claim_status"] == "in_flight_or_unknown"
    assert crash_retry["discord_body_phase"] == "dispatching"
    assert (await _delivery_row(pool, crash_key))["status"] == "unknown"
    legacy_retry = await _claim(user_a, legacy_key, parent)
    assert legacy_retry["claim_status"] == "in_flight_or_unknown"

    with pytest.raises(HTTPException) as downgraded_status:
        await cg_db.update_delivery_progress(
            delivery_key=crash_key,
            status="failed",
            error_code="not_allowed",
            error_message="stale worker",
        )
    assert downgraded_status.value.status_code == 409
    assert downgraded_status.value.detail["code"] == "conflict"
    with pytest.raises(HTTPException) as downgraded_phase:
        await cg_db.update_delivery_progress(
            delivery_key=crash_key,
            discord_body_phase="before_body",
        )
    assert downgraded_phase.value.status_code == 409
    assert downgraded_phase.value.detail["code"] == "conflict"
    kept = await cg_db.update_delivery_progress(
        delivery_key=crash_key,
        discord_body_phase="dispatching",
        first_response_json={"status": "unknown", "address": confirmed},
    )
    assert kept["ok"] is True
    assert kept["confirmed_thread_address"] == confirmed
    assert kept["discord_body_phase"] == "dispatching"
    kept_row = await _delivery_row(pool, crash_key)
    assert kept_row["status"] == "unknown"
    assert kept_row["error_code"] == "gateway_restarted"
    assert kept_row["confirmed_thread_address"] == confirmed
    assert kept_row["discord_body_phase"] == "dispatching"
    assert kept_row["target_address"] == parent
    assert kept_row["first_response_json"]["address"] == confirmed

    state = await cg_db.get_channel_internal_state(user_id=user_a)
    apps = json.loads(json.dumps(state["settings"]["apps"]))
    discord_app = apps["discord"]
    discord_app["agent_messages"]["allowed"] = [
        address
        for address in discord_app["agent_messages"]["allowed"]
        if address != parent
    ]
    discord_app["chats"].pop(parent, None)
    await cg_db.update_user_channel_settings(
        user_id=user_a,
        version=state["settings"]["version"],
        settings=apps,
    )
    denied = await _claim(user_a, rejected_key, parent)
    assert denied["claim_status"] == "denied"
    assert denied["code"] == "not_allowed"
    denied_row = await _delivery_row(pool, rejected_key)
    assert denied_row["status"] == "failed"
    assert denied_row["target_address"] == parent
    assert denied_row["confirmed_thread_address"] == confirmed
    assert denied_row["discord_body_phase"] == "dispatching"


@pytest.mark.asyncio(loop_scope="function")
async def test_stale_delivery_attempt_cannot_replace_a_newer_attempt(
    seeded_env: dict[str, Any],
) -> None:
    """An older claim generation cannot overwrite a newer in-flight or sent row."""
    pool: AsyncConnectionPool = seeded_env["pool"]
    user_a = seeded_env["user_a"]
    prefix = seeded_env["prefix"]
    parent = seeded_env["bootstrap"]["parent_addr"]
    guild_id = seeded_env["bootstrap"]["guild_id"]
    parent_id = seeded_env["bootstrap"]["parent_cid"]
    confirmed = f"discord:{guild_id}/{parent_id}:thread:{THREAD_ID}"
    await _assert_d036_column(pool)

    key = f"{prefix}_stale_attempt"
    first = await _claim(user_a, key, parent)
    assert first["claim_status"] == "claimed"
    assert first["delivery_attempt"] == 1
    stored = await cg_db.update_delivery_progress(
        delivery_key=key,
        delivery_attempt=1,
        confirmed_thread_address=confirmed,
    )
    assert stored["discord_body_phase"] == "before_body"
    assert stored["delivery_attempt"] == 1
    await cg_db.update_delivery_progress(
        delivery_key=key,
        delivery_attempt=1,
        status="unknown",
        error_code="timeout",
        error_message="older attempt lost the body response",
    )
    second = await _claim(user_a, key, parent)
    assert second["claim_status"] == "claimed"
    assert second["delivery_attempt"] == 2
    assert second["discord_body_phase"] == "before_body"
    marked = await cg_db.update_delivery_progress(
        delivery_key=key,
        delivery_attempt=2,
        discord_body_phase="dispatching",
    )
    assert marked["discord_body_phase"] == "dispatching"
    with pytest.raises(HTTPException) as bad_token:
        await cg_db.update_delivery_progress(
            delivery_key=key,
            delivery_attempt=True,  # type: ignore[arg-type]
            status="sent",
            platform_message_ids=["old"],
        )
    assert bad_token.value.status_code == 400
    assert bad_token.value.detail["code"] == "invalid"
    with pytest.raises(HTTPException) as stale_inflight:
        await cg_db.update_delivery_progress(
            delivery_key=key,
            delivery_attempt=1,
            status="failed",
            error_code="not_allowed",
            error_message="stale older attempt",
            platform_message_ids=["old"],
            first_response_json={"status": "failed"},
            discord_body_phase="before_body",
        )
    assert stale_inflight.value.status_code == 409
    assert stale_inflight.value.detail["code"] == "conflict"
    flying = await _delivery_row(pool, key)
    assert flying["status"] == "in_flight"
    assert flying["delivery_attempt"] == 2
    assert flying["platform_message_ids"] == []
    assert flying["confirmed_thread_address"] == confirmed
    assert flying["discord_body_phase"] == "dispatching"
    assert flying["first_response_json"] is None

    sent = await cg_db.update_delivery_progress(
        delivery_key=key,
        delivery_attempt=2,
        status="sent",
        platform_message_ids=["m-new"],
        first_response_json={"status": "sent", "address": confirmed},
    )
    assert sent["ok"] is True
    assert sent["status"] == "sent"
    with pytest.raises(HTTPException) as stale_sent:
        await cg_db.update_delivery_progress(
            delivery_key=key,
            delivery_attempt=1,
            status="unknown",
            error_code="unavailable",
            error_message="stale completion",
            platform_message_ids=[],
            first_response_json={"status": "unknown"},
        )
    assert stale_sent.value.status_code == 409
    assert stale_sent.value.detail["code"] == "conflict"
    done = await _delivery_row(pool, key)
    assert done["status"] == "sent"
    assert done["delivery_attempt"] == 2
    assert done["platform_message_ids"] == ["m-new"]
    assert done["confirmed_thread_address"] == confirmed
    assert done["discord_body_phase"] == "dispatching"
    assert done["first_response_json"]["status"] == "sent"
    repeated = await cg_db.update_delivery_progress(
        delivery_key=key,
        delivery_attempt=2,
        status="sent",
        platform_message_ids=["m-new"],
        first_response_json={"status": "sent", "address": confirmed},
    )
    assert repeated["ok"] is True
    assert (await _delivery_row(pool, key))["platform_message_ids"] == ["m-new"]

    sticky_key = f"{prefix}_sticky_current"
    sticky_claim = await _claim(user_a, sticky_key, parent)
    assert sticky_claim["delivery_attempt"] == 1
    await cg_db.update_delivery_progress(
        delivery_key=sticky_key,
        delivery_attempt=1,
        confirmed_thread_address=confirmed,
    )
    await cg_db.update_delivery_progress(
        delivery_key=sticky_key,
        delivery_attempt=1,
        discord_body_phase="dispatching",
    )
    await cg_db.update_delivery_progress(
        delivery_key=sticky_key,
        delivery_attempt=1,
        status="unknown",
        error_code="timeout",
        error_message="current attempt lost the body",
    )
    with pytest.raises(HTTPException) as sticky_sent:
        await cg_db.update_delivery_progress(
            delivery_key=sticky_key,
            delivery_attempt=1,
            status="sent",
            platform_message_ids=["should-not-stick"],
            first_response_json={"status": "sent"},
        )
    assert sticky_sent.value.status_code == 409
    assert sticky_sent.value.detail["code"] == "conflict"
    sticky_row = await _delivery_row(pool, sticky_key)
    assert sticky_row["status"] == "unknown"
    assert sticky_row["delivery_attempt"] == 1
    assert sticky_row["discord_body_phase"] == "dispatching"
    assert sticky_row["platform_message_ids"] == []
    assert sticky_row["confirmed_thread_address"] == confirmed


@pytest.mark.asyncio(loop_scope="function")
async def test_feishu_unknown_can_still_be_completed(
    seeded_env: dict[str, Any],
) -> None:
    """A Feishu delivery can still record a late sent result without an attempt token."""
    pool: AsyncConnectionPool = seeded_env["pool"]
    user_a = seeded_env["user_a"]
    prefix = seeded_env["prefix"]
    await _assert_d036_column(pool)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_channel_identities (
                    user_id, platform, platform_user_id, platform_username
                )
                VALUES (%s, 'feishu', %s, 'feishu-user')
                """,
                (user_a, f"{prefix}_feishu"),
            )
    state = await cg_db.get_channel_internal_state(user_id=user_a)
    apps = json.loads(json.dumps(state["settings"]["apps"]))
    apps["feishu"] = {
        "preferred": "feishu:@me",
        "chats": {},
        "automation_output": {},
        "agent_messages": {
            "enabled": True,
            "allowed": ["feishu:@me"],
        },
    }
    await cg_db.update_user_channel_settings(
        user_id=user_a,
        version=state["settings"]["version"],
        settings=apps,
    )

    key = f"{prefix}_feishu_late"
    claimed = await _claim(user_a, key, "feishu:@me")
    assert claimed["claim_status"] == "claimed"
    assert claimed["delivery_attempt"] == 1
    assert claimed["target_address"] == "feishu:@me"
    await cg_db.update_delivery_progress(
        delivery_key=key,
        status="unknown",
        error_code="timeout",
        error_message="feishu response was not confirmed",
    )
    with pytest.raises(HTTPException) as phase_rejected:
        await cg_db.update_delivery_progress(
            delivery_key=key,
            discord_body_phase="dispatching",
        )
    assert phase_rejected.value.status_code == 400
    assert phase_rejected.value.detail["code"] == "invalid"
    unchanged = await _delivery_row(pool, key)
    assert unchanged["status"] == "unknown"
    assert unchanged["discord_body_phase"] is None
    assert unchanged["confirmed_thread_address"] is None

    completed = await cg_db.update_delivery_progress(
        delivery_key=key,
        status="sent",
        platform_message_ids=["fs-1"],
        first_response_json={"status": "sent"},
    )
    assert completed["ok"] is True
    assert completed["status"] == "sent"
    repeated = await cg_db.update_delivery_progress(
        delivery_key=key,
        status="sent",
        platform_message_ids=["fs-1", "fs-2"],
        first_response_json={"status": "sent"},
    )
    assert repeated["ok"] is True
    with pytest.raises(HTTPException) as stale:
        await cg_db.update_delivery_progress(
            delivery_key=key,
            delivery_attempt=0,
            status="unknown",
            platform_message_ids=[],
            first_response_json={"status": "unknown"},
        )
    assert stale.value.status_code == 409
    assert stale.value.detail["code"] == "conflict"
    row = await _delivery_row(pool, key)
    assert row["status"] == "sent"
    assert row["platform"] == "feishu"
    assert row["delivery_attempt"] == 1
    assert row["platform_message_ids"] == ["fs-1", "fs-2"]
    assert row["first_response_json"]["status"] == "sent"
    assert row["confirmed_thread_address"] is None
    assert row["discord_body_phase"] is None


_UNKNOWN_FROZEN = "Discord delivery status unknown cannot be changed"


@pytest.mark.asyncio(loop_scope="function")
async def test_discord_data_task_unknown_can_be_completed(
    seeded_env: dict[str, Any],
) -> None:
    """A stored Discord data_task can leave unknown. An agent_send row cannot."""
    pool: AsyncConnectionPool = seeded_env["pool"]
    user_a = seeded_env["user_a"]
    prefix = seeded_env["prefix"]
    target = seeded_env["bootstrap"]["parent_addr"]
    await _assert_d036_column(pool)

    omitted_key = f"{prefix}_data_task_omit"
    token_key = f"{prefix}_data_task_token"
    sent_key = f"{prefix}_agent_unknown_sent"
    failed_key = f"{prefix}_agent_unknown_failed"
    await _insert_unknown_discord_delivery(
        pool,
        delivery_key=omitted_key,
        user_id=user_a,
        kind="data_task",
        target_address=target,
    )
    await _insert_unknown_discord_delivery(
        pool,
        delivery_key=token_key,
        user_id=user_a,
        kind="data_task",
        target_address=target,
    )
    await _insert_unknown_discord_delivery(
        pool,
        delivery_key=sent_key,
        user_id=user_a,
        kind="agent_send",
        target_address=target,
    )
    await _insert_unknown_discord_delivery(
        pool,
        delivery_key=failed_key,
        user_id=user_a,
        kind="agent_send",
        target_address=target,
    )

    omitted = await cg_db.update_delivery_progress(
        delivery_key=omitted_key,
        status="sent",
        platform_message_ids=["dt-omit"],
        first_response_json={"status": "sent"},
    )
    assert omitted["ok"] is True
    assert omitted["status"] == "sent"
    assert omitted["delivery_attempt"] == 1
    omitted_row = await _delivery_row(pool, omitted_key)
    assert omitted_row["kind"] == "data_task"
    assert omitted_row["platform"] == "discord"
    assert omitted_row["status"] == "sent"
    assert omitted_row["delivery_attempt"] == 1
    assert omitted_row["platform_message_ids"] == ["dt-omit"]
    assert omitted_row["first_response_json"]["status"] == "sent"

    matched = await cg_db.update_delivery_progress(
        delivery_key=token_key,
        delivery_attempt=1,
        status="sent",
        platform_message_ids=["dt-token"],
        first_response_json={"status": "sent"},
    )
    assert matched["ok"] is True
    assert matched["status"] == "sent"
    assert matched["delivery_attempt"] == 1
    token_row = await _delivery_row(pool, token_key)
    assert token_row["kind"] == "data_task"
    assert token_row["status"] == "sent"
    assert token_row["delivery_attempt"] == 1
    assert token_row["platform_message_ids"] == ["dt-token"]

    with pytest.raises(HTTPException) as stale_data_task:
        await cg_db.update_delivery_progress(
            delivery_key=token_key,
            delivery_attempt=0,
            status="unknown",
            platform_message_ids=[],
            first_response_json={"status": "unknown"},
        )
    assert stale_data_task.value.status_code == 409
    assert stale_data_task.value.detail["code"] == "conflict"
    assert (
        stale_data_task.value.detail["message"]
        == "delivery_attempt is no longer the current attempt"
    )
    assert (await _delivery_row(pool, token_key))["status"] == "sent"
    assert (await _delivery_row(pool, token_key))["platform_message_ids"] == ["dt-token"]

    with pytest.raises(HTTPException) as sent_rejected:
        await cg_db.update_delivery_progress(
            delivery_key=sent_key,
            delivery_attempt=1,
            status="sent",
            platform_message_ids=["should-not-send"],
            first_response_json={"status": "sent"},
        )
    assert sent_rejected.value.status_code == 409
    assert sent_rejected.value.detail["code"] == "conflict"
    assert sent_rejected.value.detail["message"] == _UNKNOWN_FROZEN
    sent_row = await _delivery_row(pool, sent_key)
    assert sent_row["kind"] == "agent_send"
    assert sent_row["status"] == "unknown"
    assert sent_row["delivery_attempt"] == 1
    assert sent_row["platform_message_ids"] == []
    assert sent_row["first_response_json"] is None
    assert sent_row["error_code"] == "gateway_restarted"
    assert sent_row["discord_body_phase"] is None

    with pytest.raises(HTTPException) as failed_rejected:
        await cg_db.update_delivery_progress(
            delivery_key=failed_key,
            delivery_attempt=1,
            status="failed",
            error_code="not_allowed",
            error_message="stale worker",
            platform_message_ids=["should-not-fail"],
            first_response_json={"status": "failed"},
        )
    assert failed_rejected.value.status_code == 409
    assert failed_rejected.value.detail["code"] == "conflict"
    assert failed_rejected.value.detail["message"] == _UNKNOWN_FROZEN
    failed_row = await _delivery_row(pool, failed_key)
    assert failed_row["kind"] == "agent_send"
    assert failed_row["status"] == "unknown"
    assert failed_row["delivery_attempt"] == 1
    assert failed_row["platform_message_ids"] == []
    assert failed_row["first_response_json"] is None
    assert failed_row["error_code"] == "gateway_restarted"


@pytest.mark.asyncio(loop_scope="function")
async def test_discord_data_task_unknown_progress_over_http(
    seeded_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same I-7 rules over the real progress route. No Discord API call."""
    pool: AsyncConnectionPool = seeded_env["pool"]
    user_a = seeded_env["user_a"]
    prefix = seeded_env["prefix"]
    target = seeded_env["bootstrap"]["parent_addr"]
    token = "0123456789abcdef0123456789abcdef_data_task_http"
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", token)
    await _assert_d036_column(pool)

    omitted_key = f"{prefix}_http_data_task_omit"
    token_key = f"{prefix}_http_data_task_token"
    sent_key = f"{prefix}_http_agent_unknown_sent"
    failed_key = f"{prefix}_http_agent_unknown_failed"
    for key, kind in (
        (omitted_key, "data_task"),
        (token_key, "data_task"),
        (sent_key, "agent_send"),
        (failed_key, "agent_send"),
    ):
        await _insert_unknown_discord_delivery(
            pool,
            delivery_key=key,
            user_id=user_a,
            kind=kind,
            target_address=target,
        )

    app = FastAPI()
    app.include_router(channels_router, prefix="/api/v1/dalton")

    async with _run_ephemeral_fastapi_server(app) as base_url:
        async with httpx.AsyncClient(timeout=10.0) as client:

            async def post(body: dict[str, Any]) -> httpx.Response:
                return await client.post(
                    f"{base_url}/api/v1/dalton/channels/internal/deliveries/progress",
                    headers={"X-Service-Token": token},
                    json=body,
                )

            omitted = await post(
                {
                    "delivery_key": omitted_key,
                    "status": "sent",
                    "platform_message_ids": ["dt-http-omit"],
                    "first_response_json": {"status": "sent"},
                }
            )
            assert omitted.status_code == 200
            omitted_body = omitted.json()
            assert omitted_body["ok"] is True
            assert omitted_body["status"] == "sent"
            assert omitted_body["delivery_attempt"] == 1

            matched = await post(
                {
                    "delivery_key": token_key,
                    "delivery_attempt": 1,
                    "status": "sent",
                    "platform_message_ids": ["dt-http-token"],
                    "first_response_json": {"status": "sent"},
                }
            )
            assert matched.status_code == 200
            assert matched.json()["status"] == "sent"
            assert matched.json()["delivery_attempt"] == 1

            sent_rejected = await post(
                {
                    "delivery_key": sent_key,
                    "delivery_attempt": 1,
                    "status": "sent",
                    "platform_message_ids": ["should-not-send"],
                    "first_response_json": {"status": "sent"},
                }
            )
            assert sent_rejected.status_code == 409
            assert sent_rejected.json()["detail"]["code"] == "conflict"
            assert sent_rejected.json()["detail"]["message"] == _UNKNOWN_FROZEN

            failed_rejected = await post(
                {
                    "delivery_key": failed_key,
                    "delivery_attempt": 1,
                    "status": "failed",
                    "error_code": "not_allowed",
                    "error_message": "stale worker",
                    "platform_message_ids": ["should-not-fail"],
                    "first_response_json": {"status": "failed"},
                }
            )
            assert failed_rejected.status_code == 409
            assert failed_rejected.json()["detail"]["code"] == "conflict"
            assert failed_rejected.json()["detail"]["message"] == _UNKNOWN_FROZEN

    omitted_row = await _delivery_row(pool, omitted_key)
    assert omitted_row["kind"] == "data_task"
    assert omitted_row["platform"] == "discord"
    assert omitted_row["status"] == "sent"
    assert omitted_row["delivery_attempt"] == 1
    assert omitted_row["platform_message_ids"] == ["dt-http-omit"]
    token_row = await _delivery_row(pool, token_key)
    assert token_row["kind"] == "data_task"
    assert token_row["status"] == "sent"
    assert token_row["delivery_attempt"] == 1
    assert token_row["platform_message_ids"] == ["dt-http-token"]
    sent_row = await _delivery_row(pool, sent_key)
    assert sent_row["kind"] == "agent_send"
    assert sent_row["status"] == "unknown"
    assert sent_row["delivery_attempt"] == 1
    assert sent_row["platform_message_ids"] == []
    assert sent_row["first_response_json"] is None
    assert sent_row["error_code"] == "gateway_restarted"
    failed_row = await _delivery_row(pool, failed_key)
    assert failed_row["kind"] == "agent_send"
    assert failed_row["status"] == "unknown"
    assert failed_row["delivery_attempt"] == 1
    assert failed_row["platform_message_ids"] == []
    assert failed_row["first_response_json"] is None
    assert failed_row["error_code"] == "gateway_restarted"


def _assert_recovered_dispatch(row: dict[str, Any], confirmed: str, attempt: int) -> None:
    assert row["status"] == "unknown"
    assert row["error_code"] == "gateway_restarted"
    assert row["delivery_attempt"] == attempt
    assert row["discord_body_phase"] == "dispatching"
    assert row["confirmed_thread_address"] == confirmed
    assert row["platform_message_ids"] == []
    assert row["sent_chunks"] == 0
    assert row["first_response_json"] is None


@pytest.mark.asyncio(loop_scope="function")
async def test_current_attempt_finishes_after_startup_recovery(
    seeded_env: dict[str, Any],
) -> None:
    """Startup may mark a dispatching attempt unknown. That attempt can still finish.

    A second claim while the row is unknown and dispatching does not reclaim it,
    so a retry would not send another body. A lost-response unknown, an omitted
    token, a stale attempt, and a return to in_flight stay rejected.
    """
    pool: AsyncConnectionPool = seeded_env["pool"]
    user_a = seeded_env["user_a"]
    prefix = seeded_env["prefix"]
    parent = seeded_env["bootstrap"]["parent_addr"]
    confirmed = (
        f"discord:{seeded_env['bootstrap']['guild_id']}/"
        f"{seeded_env['bootstrap']['parent_cid']}:thread:{THREAD_ID}"
    )
    await _assert_d036_column(pool)
    sent_key = f"{prefix}_recover_sent"
    failed_key = f"{prefix}_recover_failed"
    partial_key = f"{prefix}_recover_partial"
    sent_attempt = await _arm_discord_dispatch(user_a, sent_key, parent, confirmed)
    failed_attempt = await _arm_discord_dispatch(user_a, failed_key, parent, confirmed)
    partial_attempt = await _arm_discord_dispatch(
        user_a, partial_key, parent, confirmed
    )
    assert sent_attempt == failed_attempt == partial_attempt == 1
    for key in (sent_key, failed_key, partial_key):
        flying = await _delivery_row(pool, key)
        assert flying["status"] == "in_flight"
        assert flying["error_code"] is None
        assert flying["discord_body_phase"] == "dispatching"

    await cg_db.recover_channel_state(mode="startup")

    for key in (sent_key, failed_key, partial_key):
        _assert_recovered_dispatch(await _delivery_row(pool, key), confirmed, 1)
        blocked = await _claim(user_a, key, parent)
        assert blocked["claim_status"] == "in_flight_or_unknown"
        assert blocked["code"] == "in_flight"
        assert blocked["delivery_attempt"] == 1
        assert blocked["discord_body_phase"] == "dispatching"
        assert blocked["confirmed_thread_address"] == confirmed
        _assert_recovered_dispatch(await _delivery_row(pool, key), confirmed, 1)

    with pytest.raises(HTTPException) as omitted:
        await cg_db.update_delivery_progress(
            delivery_key=sent_key,
            status="sent",
            platform_message_ids=["omitted-token"],
            first_response_json={"status": "sent"},
        )
    assert omitted.value.status_code == 409
    assert omitted.value.detail["message"] == _UNKNOWN_FROZEN
    with pytest.raises(HTTPException) as stale:
        await cg_db.update_delivery_progress(
            delivery_key=sent_key,
            delivery_attempt=0,
            status="sent",
            platform_message_ids=["stale-attempt"],
            first_response_json={"status": "sent"},
        )
    assert stale.value.status_code == 409
    assert (
        stale.value.detail["message"]
        == "delivery_attempt is no longer the current attempt"
    )
    with pytest.raises(HTTPException) as reopened:
        await cg_db.update_delivery_progress(
            delivery_key=sent_key,
            delivery_attempt=1,
            status="in_flight",
        )
    assert reopened.value.status_code == 409
    assert reopened.value.detail["message"] == _UNKNOWN_FROZEN
    _assert_recovered_dispatch(await _delivery_row(pool, sent_key), confirmed, 1)

    finished = await cg_db.update_delivery_progress(
        delivery_key=sent_key,
        delivery_attempt=1,
        status="sent",
        platform_message_ids=["m-sent"],
        first_response_json={"status": "sent", "address": confirmed},
    )
    assert finished["ok"] is True
    assert finished["status"] == "sent"
    assert finished["delivery_attempt"] == 1
    assert finished["confirmed_thread_address"] == confirmed
    assert finished["discord_body_phase"] == "dispatching"
    sent_row = await _delivery_row(pool, sent_key)
    assert sent_row["status"] == "sent"
    assert sent_row["delivery_attempt"] == 1
    assert sent_row["platform_message_ids"] == ["m-sent"]
    assert sent_row["confirmed_thread_address"] == confirmed
    assert sent_row["discord_body_phase"] == "dispatching"
    assert sent_row["sent_chunks"] == 0
    completed = await _claim(user_a, sent_key, parent)
    assert completed["claim_status"] == "already_completed"
    assert completed["delivery_attempt"] == 1
    with pytest.raises(HTTPException) as stale_sent:
        await cg_db.update_delivery_progress(
            delivery_key=sent_key,
            delivery_attempt=0,
            status="unknown",
            platform_message_ids=[],
            first_response_json={"status": "unknown"},
        )
    assert stale_sent.value.status_code == 409
    assert (
        stale_sent.value.detail["message"]
        == "delivery_attempt is no longer the current attempt"
    )
    assert (await _delivery_row(pool, sent_key))["platform_message_ids"] == ["m-sent"]
    assert (await _delivery_row(pool, sent_key))["confirmed_thread_address"] == confirmed

    failed = await cg_db.update_delivery_progress(
        delivery_key=failed_key,
        delivery_attempt=1,
        status="failed",
        error_code="not_allowed",
        error_message="Discord message send failed",
        platform_message_ids=["m-fail"],
        first_response_json={"status": "failed", "address": confirmed},
    )
    assert failed["ok"] is True
    assert failed["status"] == "failed"
    assert failed["delivery_attempt"] == 1
    failed_row = await _delivery_row(pool, failed_key)
    assert failed_row["status"] == "failed"
    assert failed_row["platform_message_ids"] == ["m-fail"]
    assert failed_row["confirmed_thread_address"] == confirmed
    assert failed_row["discord_body_phase"] == "dispatching"
    assert failed_row["delivery_attempt"] == 1
    reclaimed = await _claim(user_a, failed_key, parent)
    assert reclaimed["claim_status"] == "claimed"
    assert reclaimed["delivery_attempt"] == 2
    assert reclaimed["confirmed_thread_address"] == confirmed
    assert reclaimed["discord_body_phase"] == "dispatching"
    with pytest.raises(HTTPException) as old_attempt:
        await cg_db.update_delivery_progress(
            delivery_key=failed_key,
            delivery_attempt=1,
            status="sent",
            platform_message_ids=["replaced"],
            first_response_json={"status": "sent"},
        )
    assert old_attempt.value.status_code == 409
    assert (
        old_attempt.value.detail["message"]
        == "delivery_attempt is no longer the current attempt"
    )
    after_old = await _delivery_row(pool, failed_key)
    assert after_old["status"] == "in_flight"
    assert after_old["delivery_attempt"] == 2
    assert after_old["platform_message_ids"] == ["m-fail"]
    assert after_old["confirmed_thread_address"] == confirmed
    assert after_old["discord_body_phase"] == "dispatching"

    partial = await cg_db.update_delivery_progress(
        delivery_key=partial_key,
        delivery_attempt=1,
        status="partial",
        platform_message_ids=["m-partial"],
        first_response_json={"status": "partial", "address": confirmed},
    )
    assert partial["ok"] is True
    assert partial["status"] == "partial"
    assert partial["delivery_attempt"] == 1
    partial_row = await _delivery_row(pool, partial_key)
    assert partial_row["status"] == "partial"
    assert partial_row["delivery_attempt"] == 1
    assert partial_row["platform_message_ids"] == ["m-partial"]
    assert partial_row["confirmed_thread_address"] == confirmed
    assert partial_row["discord_body_phase"] == "dispatching"
    partial_again = await _claim(user_a, partial_key, parent)
    assert partial_again["claim_status"] == "already_completed"
    assert partial_again["delivery_attempt"] == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_current_attempt_finishes_after_startup_recovery_over_http(
    seeded_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same recovery window over the real claim, recover, and progress routes."""
    pool: AsyncConnectionPool = seeded_env["pool"]
    user_a = seeded_env["user_a"]
    prefix = seeded_env["prefix"]
    parent = seeded_env["bootstrap"]["parent_addr"]
    confirmed = (
        f"discord:{seeded_env['bootstrap']['guild_id']}/"
        f"{seeded_env['bootstrap']['parent_cid']}:thread:{THREAD_ID}"
    )
    token = "0123456789abcdef0123456789abcdef_recovery_http"
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", token)
    await _assert_d036_column(pool)
    sent_key = f"{prefix}_http_recover_sent"
    failed_key = f"{prefix}_http_recover_failed"
    partial_key = f"{prefix}_http_recover_partial"
    app = FastAPI()
    app.include_router(channels_router, prefix="/api/v1/dalton")

    async with _run_ephemeral_fastapi_server(app) as base_url:
        async with httpx.AsyncClient(timeout=10.0) as client:
            claim_url = (
                f"{base_url}/api/v1/dalton/channels/internal/deliveries/authorize-and-claim"
            )
            progress_url = (
                f"{base_url}/api/v1/dalton/channels/internal/deliveries/progress"
            )
            recover_url = f"{base_url}/api/v1/dalton/channels/internal/recover"
            headers = {"X-Service-Token": token}

            async def claim(key: str) -> dict[str, Any]:
                response = await client.post(
                    claim_url,
                    headers=headers,
                    json={
                        "delivery_key": key,
                        "kind": "agent_send",
                        "user_id": user_a,
                        "platform": "discord",
                        "target": parent,
                        "has_non_empty_text": True,
                        "total_chunks": 1,
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()

            async def progress(body: dict[str, Any]) -> httpx.Response:
                return await client.post(progress_url, headers=headers, json=body)

            async def arm(key: str) -> int:
                claimed = await claim(key)
                assert claimed["claim_status"] == "claimed"
                attempt = int(claimed["delivery_attempt"])
                stored = await progress(
                    {
                        "delivery_key": key,
                        "delivery_attempt": attempt,
                        "confirmed_thread_address": confirmed,
                    }
                )
                assert stored.status_code == 200, stored.text
                assert stored.json()["discord_body_phase"] == "before_body"
                marked = await progress(
                    {
                        "delivery_key": key,
                        "delivery_attempt": attempt,
                        "discord_body_phase": "dispatching",
                    }
                )
                assert marked.status_code == 200, marked.text
                assert marked.json()["discord_body_phase"] == "dispatching"
                assert marked.json()["delivery_attempt"] == attempt
                return attempt

            sent_attempt = await arm(sent_key)
            failed_attempt = await arm(failed_key)
            partial_attempt = await arm(partial_key)
            assert sent_attempt == failed_attempt == partial_attempt == 1
            recovered = await client.get(
                recover_url, headers=headers, params={"mode": "startup"}
            )
            assert recovered.status_code == 200, recovered.text

            for key in (sent_key, failed_key, partial_key):
                _assert_recovered_dispatch(await _delivery_row(pool, key), confirmed, 1)
                blocked = await claim(key)
                assert blocked["claim_status"] == "in_flight_or_unknown"
                assert blocked["code"] == "in_flight"
                assert blocked["delivery_attempt"] == 1
                assert blocked["confirmed_thread_address"] == confirmed
                assert blocked["discord_body_phase"] == "dispatching"
                _assert_recovered_dispatch(await _delivery_row(pool, key), confirmed, 1)

            omitted = await progress(
                {
                    "delivery_key": sent_key,
                    "status": "sent",
                    "platform_message_ids": ["omitted-token"],
                    "first_response_json": {"status": "sent"},
                }
            )
            assert omitted.status_code == 409
            assert omitted.json()["detail"]["message"] == _UNKNOWN_FROZEN
            stale = await progress(
                {
                    "delivery_key": sent_key,
                    "delivery_attempt": 0,
                    "status": "sent",
                    "platform_message_ids": ["stale-attempt"],
                    "first_response_json": {"status": "sent"},
                }
            )
            assert stale.status_code == 409
            assert (
                stale.json()["detail"]["message"]
                == "delivery_attempt is no longer the current attempt"
            )
            reopened = await progress(
                {
                    "delivery_key": sent_key,
                    "delivery_attempt": 1,
                    "status": "in_flight",
                }
            )
            assert reopened.status_code == 409
            assert reopened.json()["detail"]["message"] == _UNKNOWN_FROZEN
            _assert_recovered_dispatch(await _delivery_row(pool, sent_key), confirmed, 1)

            finished = await progress(
                {
                    "delivery_key": sent_key,
                    "delivery_attempt": 1,
                    "status": "sent",
                    "platform_message_ids": ["m-sent"],
                    "first_response_json": {"status": "sent", "address": confirmed},
                }
            )
            assert finished.status_code == 200, finished.text
            assert finished.json()["status"] == "sent"
            assert finished.json()["delivery_attempt"] == 1
            assert finished.json()["confirmed_thread_address"] == confirmed
            assert finished.json()["discord_body_phase"] == "dispatching"
            sent_done = await claim(sent_key)
            assert sent_done["claim_status"] == "already_completed"
            assert sent_done["delivery_attempt"] == 1

            failed = await progress(
                {
                    "delivery_key": failed_key,
                    "delivery_attempt": 1,
                    "status": "failed",
                    "error_code": "not_allowed",
                    "error_message": "Discord message send failed",
                    "platform_message_ids": ["m-fail"],
                    "first_response_json": {"status": "failed", "address": confirmed},
                }
            )
            assert failed.status_code == 200, failed.text
            assert failed.json()["status"] == "failed"
            assert failed.json()["delivery_attempt"] == 1
            reclaimed = await claim(failed_key)
            assert reclaimed["claim_status"] == "claimed"
            assert reclaimed["delivery_attempt"] == 2
            assert reclaimed["confirmed_thread_address"] == confirmed
            assert reclaimed["discord_body_phase"] == "dispatching"
            old_attempt = await progress(
                {
                    "delivery_key": failed_key,
                    "delivery_attempt": 1,
                    "status": "sent",
                    "platform_message_ids": ["replaced"],
                    "first_response_json": {"status": "sent"},
                }
            )
            assert old_attempt.status_code == 409
            assert (
                old_attempt.json()["detail"]["message"]
                == "delivery_attempt is no longer the current attempt"
            )

            partial = await progress(
                {
                    "delivery_key": partial_key,
                    "delivery_attempt": 1,
                    "status": "partial",
                    "platform_message_ids": ["m-partial"],
                    "first_response_json": {"status": "partial", "address": confirmed},
                }
            )
            assert partial.status_code == 200, partial.text
            assert partial.json()["status"] == "partial"
            assert partial.json()["delivery_attempt"] == 1
            assert partial.json()["discord_body_phase"] == "dispatching"

    sent_row = await _delivery_row(pool, sent_key)
    assert sent_row["status"] == "sent"
    assert sent_row["delivery_attempt"] == 1
    assert sent_row["platform_message_ids"] == ["m-sent"]
    assert sent_row["confirmed_thread_address"] == confirmed
    assert sent_row["discord_body_phase"] == "dispatching"
    assert sent_row["sent_chunks"] == 0
    failed_row = await _delivery_row(pool, failed_key)
    assert failed_row["status"] == "in_flight"
    assert failed_row["delivery_attempt"] == 2
    assert failed_row["platform_message_ids"] == ["m-fail"]
    assert failed_row["confirmed_thread_address"] == confirmed
    assert failed_row["discord_body_phase"] == "dispatching"
    partial_row = await _delivery_row(pool, partial_key)
    assert partial_row["status"] == "partial"
    assert partial_row["delivery_attempt"] == 1
    assert partial_row["platform_message_ids"] == ["m-partial"]
    assert partial_row["confirmed_thread_address"] == confirmed
    assert partial_row["discord_body_phase"] == "dispatching"


if __name__ == "__main__" and "--serve-http-harness" in sys.argv:
    asyncio.run(serve_discord_http_harness(dalton_revision="d036"))
