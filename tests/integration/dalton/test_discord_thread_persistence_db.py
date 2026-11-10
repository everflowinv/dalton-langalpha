"""Real PostgreSQL tests for a confirmed Discord thread on its delivery (d036).

The column is applied by the Dalton Alembic chain. Claim, progress, and startup
recovery call the real channel-gateway functions. This file does not ALTER a
stamped schema by hand.

``--serve-http-harness`` upgrades to at least d036 and serves the Discord HTTP
app used by the gateway vitest. Startup recovery rewrites every ``in_flight``
delivery on the database, so do not run this file while another suite still
has in-flight rows there.
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


@pytest_asyncio.fixture(scope="session")
async def thread_persistence_pool():
    """Upgrade the isolated test database to at least d036 and yield a pool."""
    dsn = _get_test_dsn()
    os.environ.setdefault(
        "BYOK_ENCRYPTION_KEY", "test-channel-gateway-encryption-key-32b"
    )
    await _ensure_alembic_upgraded_to_d031(dsn, minimum="d036")
    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=4,
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


@pytest_asyncio.fixture
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


async def _delivery_row(pool: AsyncConnectionPool, delivery_key: str) -> dict[str, Any]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT status,
                       platform,
                       target_address,
                       confirmed_thread_address,
                       error_code,
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


@pytest.mark.asyncio
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
    assert claimed["target_address"] == parent
    assert claimed["confirmed_thread_address"] is None

    stored = await cg_db.update_delivery_progress(
        delivery_key=same_key,
        confirmed_thread_address=confirmed,
    )
    assert stored["ok"] is True
    assert stored["confirmed_thread_address"] == confirmed
    repeated = await cg_db.update_delivery_progress(
        delivery_key=same_key,
        confirmed_thread_address=confirmed,
    )
    assert repeated["ok"] is True
    assert repeated["confirmed_thread_address"] == confirmed
    assert (await _delivery_row(pool, same_key))["confirmed_thread_address"] == confirmed

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
    assert (await _delivery_row(pool, dm_key))["confirmed_thread_address"] is None

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
    assert blocked["confirmed_thread_address"] is None
    assert (await _delivery_row(pool, unknown_key))["status"] == "unknown"
    assert (await _delivery_row(pool, unknown_key))["confirmed_thread_address"] is None

    rejected_key = f"{prefix}_rejected"
    assert (await _claim(user_a, rejected_key, parent))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=rejected_key,
        confirmed_thread_address=confirmed,
    )
    replaced = await cg_db.update_delivery_progress(
        delivery_key=rejected_key,
        status="failed",
        error_code="not_allowed",
        error_message="Discord message send failed",
        first_response_json={"status": "failed", "address": confirmed},
    )
    assert replaced["confirmed_thread_address"] == confirmed
    rejected_row = await _delivery_row(pool, rejected_key)
    assert rejected_row["status"] == "failed"
    assert rejected_row["target_address"] == parent
    assert rejected_row["confirmed_thread_address"] == confirmed
    assert rejected_row["first_response_json"]["status"] == "failed"

    restart_key = f"{prefix}_restart"
    assert (await _claim(user_a, restart_key, parent))["claim_status"] == "claimed"
    await cg_db.update_delivery_progress(
        delivery_key=restart_key,
        confirmed_thread_address=confirmed,
    )
    still_flying = await _claim(user_a, restart_key, parent)
    assert still_flying["claim_status"] == "in_flight_or_unknown"
    assert still_flying["confirmed_thread_address"] == confirmed
    assert (await _delivery_row(pool, restart_key))["status"] == "in_flight"

    await cg_db.recover_channel_state(mode="startup")

    restarted = await _delivery_row(pool, restart_key)
    assert restarted["status"] == "unknown"
    assert restarted["error_code"] == "gateway_restarted"
    assert restarted["confirmed_thread_address"] == confirmed
    assert restarted["target_address"] == parent
    survived = await _delivery_row(pool, same_key)
    assert survived["status"] == "unknown"
    assert survived["confirmed_thread_address"] == confirmed
    assert (await _delivery_row(pool, unknown_key))["error_code"] == "unavailable"
    assert (await _delivery_row(pool, rejected_key))["status"] == "failed"

    reclaimed = await _claim(user_a, restart_key, parent)
    assert reclaimed["claim_status"] == "claimed"
    assert reclaimed["target_address"] == parent
    assert reclaimed["confirmed_thread_address"] == confirmed
    assert (await _delivery_row(pool, restart_key))["status"] == "in_flight"
    assert (await _delivery_row(pool, restart_key))["confirmed_thread_address"] == confirmed

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


if __name__ == "__main__" and "--serve-http-harness" in sys.argv:
    asyncio.run(serve_discord_http_harness(dalton_revision="d036"))
