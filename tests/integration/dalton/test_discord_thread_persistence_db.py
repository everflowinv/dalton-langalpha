"""Real PostgreSQL tests for a confirmed Discord thread, its body phase, and claim generation (d036).

The columns are applied by the Dalton Alembic chain. Claim, progress, and startup
recovery call the real channel-gateway functions. This file does not ALTER a
stamped schema by hand.

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

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio

from src.dalton import invites as dalton_invites
from src.dalton import sharing as dalton_sharing
from src.dalton.database import channel_gateway as cg_db
from src.server.database import pool as db_pool
from src.server.database import user as user_db
from src.server.database import workspace as workspace_db
from tests.integration.dalton.test_discord_gateway_db import (
    _cleanup_test_rows,
    _ensure_alembic_upgraded_to_d031,
    _get_test_dsn,
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
    await _ensure_alembic_upgraded_to_d031(dsn, minimum="d036")
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


if __name__ == "__main__" and "--serve-http-harness" in sys.argv:
    asyncio.run(serve_discord_http_harness(dalton_revision="d036"))
