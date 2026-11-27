"""Real PostgreSQL integration, migration round-trip, & 11-interleaving concurrency tests
for Channel Phase 2 Batch 3.

Uses ``tests/integration/conftest.py`` (``test_db_uri`` + ``test_db_pool`` ``_test`` DB guard)
and ``tests/integration/dalton/conftest.py`` (``dalton_schema``) so every test executes
against the isolated ``*_test`` PostgreSQL database. Each test opens a function-scoped
``AsyncConnectionPool`` on the test's own live event loop (``max_size=12``) with strict
transaction cleanup on return to prevent cross-loop ``PoolTimeout``.

Run outside sandbox (where PostgreSQL is reachable):
    uv run pytest -m integration tests/integration/dalton/test_channel_action_tokens_db.py -v
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import HTTPException
import psycopg
import psycopg.pq
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio

from src.dalton import channel_action_tokens as cat
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import weixin_multi_account as wx_db
from src.server.database import pool as db_pool
from src.server.database.encryption import get_encryption_key

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_ENCRYPTION_KEY = "test-channel-b3-encryption-key-32bytes!"
_SYNC_TIMEOUT = 15.0


def _build_dalton_alembic_cfg(db_uri: str) -> AlembicConfig:
    cfg = AlembicConfig(str(REPO_ROOT / "src" / "dalton" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "src" / "dalton" / "migrations"))
    sa_url = db_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    cfg.set_main_option("sqlalchemy.url", sa_url.replace("%", "%%"))
    return cfg


def _assert_not_lock_or_constraint_error(exc: BaseException) -> None:
    assert not isinstance(
        exc,
        (
            psycopg.errors.DeadlockDetected,
            psycopg.errors.UniqueViolation,
            psycopg.errors.CheckViolation,
            psycopg.errors.LockNotAvailable,
            psycopg.errors.QueryCanceled,
        ),
    ), f"Unexpected PostgreSQL error: {type(exc).__name__}: {exc}"


async def _wait_for_pg_lock_wait(
    pool: AsyncConnectionPool,
    query_pattern: str,
    *,
    timeout: float = _SYNC_TIMEOUT,
) -> dict[str, Any]:
    """Open a separate connection and poll ``pg_stat_activity`` + ``pg_locks`` until a
    backend in this test database is genuinely blocked with ``wait_event_type = 'Lock'``
    on a query matching ``query_pattern``."""
    deadline = asyncio.get_running_loop().time() + timeout
    async with pool.connection() as obs_conn:
        async with obs_conn.cursor() as obs_cur:
            while True:
                await obs_cur.execute(
                    """
                    SELECT a.pid,
                           a.wait_event_type,
                           a.wait_event,
                           a.query,
                           pg_blocking_pids(a.pid) AS blockers
                    FROM pg_stat_activity a
                    WHERE a.datname = current_database()
                      AND a.pid <> pg_backend_pid()
                      AND a.state = 'active'
                      AND a.wait_event_type = 'Lock'
                      AND a.query ILIKE %s
                      AND cardinality(pg_blocking_pids(a.pid)) > 0
                      AND EXISTS (
                          SELECT 1
                          FROM pg_locks l
                          WHERE l.pid = a.pid
                            AND NOT l.granted
                      )
                    LIMIT 1
                    """,
                    (query_pattern,),
                )
                row = await obs_cur.fetchone()
                if row is not None:
                    return dict(row)
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting for PostgreSQL Lock wait matching {query_pattern!r}"
                    )
                await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# Fixtures: live-loop AsyncConnectionPool + real pgcrypto Weixin + lease lifecycle
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_b3_env(
    test_db_uri: str,
    dalton_schema,
    monkeypatch: pytest.MonkeyPatch,
):
    """Open a function-scoped AsyncConnectionPool on the test's own event loop,
    verify ``current_database()`` ends with ``_test``, acquire a real gateway lease
    (asserting ``granted`` and releasing in ``finally``), and clean up rows on teardown."""
    prefix = f"b3pg_{uuid4().hex[:10]}"
    uid = f"{prefix}_user"
    tid = str(uuid4())
    ws_id = str(uuid4())
    instance_id = f"gw-{prefix}"

    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_GATEWAY", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_FEISHU", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_DISCORD", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_WEIXIN", "true")
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)
    for var in (
        "DALTON_CHANNEL_INTERACTIVE_ACTIONS_ENABLED",
        "DALTON_CHANNEL_INTERRUPT_LEDGER_ENABLED",
        "DALTON_CHANNEL_BT2_LINK_RECOVERY_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)

    local_pool = AsyncConnectionPool(
        conninfo=test_db_uri,
        min_size=2,
        max_size=12,
        check=AsyncConnectionPool.check_connection,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        open=False,
    )
    await local_pool.open()
    await local_pool.wait()

    @asynccontextmanager
    async def _live_get_db_connection(existing_conn: Any = None):
        if existing_conn is not None:
            yield existing_conn
            return
        async with local_pool.connection(timeout=15) as owned:
            try:
                yield owned
            finally:
                if owned.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                    try:
                        await owned.rollback()
                    except Exception:
                        pass

    gdc_mock = create_autospec(db_pool.get_db_connection, side_effect=_live_get_db_connection)
    monkeypatch.setattr(db_pool, "get_db_connection", gdc_mock)
    monkeypatch.setattr(cat, "get_db_connection", gdc_mock)
    monkeypatch.setattr(cg_db, "get_db_connection", gdc_mock)
    monkeypatch.setattr(wx_db, "get_db_connection", gdc_mock)

    tier_mock = create_autospec(cat.get_user_tier, return_value="internal")
    monkeypatch.setattr(cat, "get_user_tier", tier_mock)
    monkeypatch.setattr(cg_db, "get_user_tier", tier_mock)
    monkeypatch.setattr(wx_db, "get_user_tier", tier_mock)
    roster_mock = create_autospec(cg_db.check_team_member_status_live, return_value="active")
    monkeypatch.setattr(cg_db, "check_team_member_status_live", roster_mock)
    monkeypatch.setattr(cat, "check_team_member_status_live", roster_mock)
    monkeypatch.setattr(wx_db, "check_team_member_status_live", roster_mock)

    epoch: int | None = None
    try:
        async with local_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT current_database() AS db")
                db_row = await cur.fetchone()
                assert db_row and str(db_row["db"]).endswith("_test"), (
                    f"Refusing to run against non-test DB: {db_row!r}"
                )
                # Ensure pgcrypto is available for real pgp_sym_encrypt / pgp_sym_decrypt
                await cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
                # Reset runtime barrier & weixin config, and expire any stale prior lease
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_interrupt_runtime_config (singleton_id)
                    VALUES (1)
                    ON CONFLICT (singleton_id) DO UPDATE SET
                        interactive_actions_enabled = TRUE,
                        interrupt_ledger_enabled = TRUE,
                        bt2_link_recovery_enabled = TRUE,
                        updated_at = NOW()
                    """
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_weixin_config (singleton_id, enabled, allow_external)
                    VALUES (1, TRUE, FALSE)
                    ON CONFLICT (singleton_id) DO UPDATE SET
                        enabled = TRUE,
                        updated_at = NOW()
                    """
                )
                await cur.execute(
                    """
                    UPDATE dalton_channel_gateway_lease
                    SET expires_at = NOW() - INTERVAL '5 seconds'
                    WHERE singleton_id = 1
                    """
                )
                await cur.execute(
                    """
                    INSERT INTO users (user_id, email, name, created_at, updated_at)
                    VALUES (%s, %s, %s, NOW(), NOW())
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (uid, f"{uid}@test.local", "B3 Test User"),
                )
                await cur.execute(
                    """
                    INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                    VALUES (%s::uuid, %s, %s, 'running', NOW(), NOW())
                    ON CONFLICT (workspace_id) DO NOTHING
                    """,
                    (ws_id, uid, f"{prefix}_ws"),
                )
                await cur.execute(
                    """
                    INSERT INTO conversation_threads (
                        conversation_thread_id, workspace_id, user_id, current_status, thread_index, created_at, updated_at
                    )
                    VALUES (%s::uuid, %s::uuid, %s, 'in_progress', 1, NOW(), NOW())
                    ON CONFLICT (conversation_thread_id) DO NOTHING
                    """,
                    (tid, ws_id, uid),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id)
                    VALUES (%s, 'feishu', %s), (%s, 'discord', %s)
                    ON CONFLICT (user_id, platform) DO UPDATE SET platform_user_id = EXCLUDED.platform_user_id
                    """,
                    (uid, f"ou_{prefix}", uid, f"dc_{prefix}"),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_user_settings (user_id, version, apps)
                    VALUES (%s, 'v1', %s)
                    ON CONFLICT (user_id) DO UPDATE SET apps = EXCLUDED.apps
                    """,
                    (
                        uid,
                        Jsonb(
                            {
                                "feishu": {
                                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                                    "chats": {},
                                },
                                "discord": {
                                    "agent_messages": {"enabled": True, "allowed": ["discord:@me"]},
                                    "chats": {},
                                },
                            }
                        ),
                    ),
                )

        lease = await wx_db.acquire_gateway_lease(instance_id=instance_id, ttl_seconds=120)
        assert lease["granted"] is True, f"Expected gateway lease granted=True, got {lease!r}"
        epoch = int(lease["epoch"])

        yield {
            "pool": local_pool,
            "db_uri": test_db_uri,
            "prefix": prefix,
            "uid": uid,
            "tid": tid,
            "ws_id": ws_id,
            "epoch": epoch,
            "instance_id": instance_id,
            "feishu_puid": f"ou_{prefix}",
            "discord_puid": f"dc_{prefix}",
        }
    finally:
        # Always reset concurrency hooks, release gateway lease, clean up rows, and close pool
        cat._after_preview_hook = None
        cat._before_7b0_lock_hook = None
        cat._after_7b0_lock_hook = None
        cat._after_7a_inbound_lock_hook = None
        cat._before_7b2_lock_hook = None
        cat._after_7b2_lock_hook = None
        cg_db._after_recover_3a_lock_hook = None
        wx_db._after_unbind_3a_lock_hook = None
        try:
            if epoch is not None:
                await wx_db.release_gateway_lease(instance_id=instance_id, epoch=epoch)
        except Exception:
            pass
        try:
            async with local_pool.connection(timeout=10) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM dalton_channel_action_tokens WHERE thread_id = %s::uuid OR dalton_user_id = %s",
                        (tid, uid),
                    )
                    await cur.execute(
                        "DELETE FROM dalton_channel_interrupt_resumes WHERE thread_id = %s::uuid OR settled_by_user_id = %s",
                        (tid, uid),
                    )
                    await cur.execute(
                        "DELETE FROM dalton_channel_deliveries WHERE user_id = %s OR delivery_key LIKE %s",
                        (uid, f"%{prefix}%"),
                    )
                    await cur.execute(
                        "DELETE FROM dalton_channel_background_runs WHERE thread_id = %s::uuid OR dalton_user_id = %s",
                        (tid, uid),
                    )
                    await cur.execute(
                        "DELETE FROM dalton_channel_thread_watches WHERE thread_id = %s::uuid OR dalton_user_id = %s",
                        (tid, uid),
                    )
                    await cur.execute(
                        "DELETE FROM dalton_channel_inbound_events WHERE dalton_user_id = %s OR message_id LIKE %s",
                        (uid, f"%{prefix}%"),
                    )
                    await cur.execute(
                        "DELETE FROM dalton_channel_session_generations WHERE dalton_user_id = %s",
                        (uid,),
                    )
                    await cur.execute(
                        "DELETE FROM dalton_channel_weixin_audit_logs WHERE target_user_id = %s OR operator_user_id = %s",
                        (uid, uid),
                    )
                    await cur.execute(
                        "DELETE FROM dalton_channel_weixin_accounts WHERE user_id = %s",
                        (uid,),
                    )
                    await cur.execute(
                        "DELETE FROM dalton_channel_identities WHERE user_id = %s",
                        (uid,),
                    )
                    await cur.execute(
                        "DELETE FROM dalton_channel_user_settings WHERE user_id = %s",
                        (uid,),
                    )
                    await cur.execute(
                        "DELETE FROM conversation_responses WHERE conversation_thread_id = %s::uuid",
                        (tid,),
                    )
                    await cur.execute(
                        "DELETE FROM conversation_queries WHERE conversation_thread_id = %s::uuid",
                        (tid,),
                    )
                    await cur.execute(
                        "DELETE FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
                        (tid,),
                    )
                    await cur.execute(
                        "DELETE FROM workspaces WHERE workspace_id = %s::uuid",
                        (ws_id,),
                    )
                    await cur.execute("DELETE FROM users WHERE user_id = %s", (uid,))
                    await cur.execute(
                        """
                        UPDATE dalton_channel_interrupt_runtime_config
                        SET interactive_actions_enabled = TRUE,
                            interrupt_ledger_enabled = TRUE,
                            bt2_link_recovery_enabled = TRUE,
                            updated_at = NOW()
                        WHERE singleton_id = 1
                        """
                    )
        finally:
            await local_pool.close()


async def _seed_session_and_inbound(
    pool: AsyncConnectionPool,
    *,
    platform: str,
    message_id: str,
    uid: str,
    scope_key: str,
    chat_address: str,
    tid: str,
    run_id: str,
    interrupt_id: str,
    weixin_account_id: str | None = None,
    weixin_bot_gen: int | None = None,
    status: str = "pending",
) -> None:
    pi = {
        "interrupt_id": interrupt_id,
        "thread_id": tid,
        "run_id": run_id,
        "kind": "ask_user_question",
        "action_requests": [{"type": "ask_user_question", "question": "Proceed?"}],
    }
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_channel_session_generations (
                    platform, scope_key, dalton_user_id, generation, pending_interrupt
                )
                VALUES (%s, %s, %s, 1, %s)
                ON CONFLICT (platform, scope_key, dalton_user_id) DO UPDATE SET
                    generation = 1,
                    pending_interrupt = EXCLUDED.pending_interrupt
                """,
                (platform, scope_key, uid, Jsonb(pi)),
            )
            await cur.execute(
                """
                INSERT INTO dalton_channel_inbound_events (
                    platform, message_id, request_key, dalton_user_id,
                    chat_type, chat_address, thread_scope_key, thread_id, run_id,
                    status, raw_payload, weixin_account_id, weixin_context_bot_generation
                )
                VALUES (%s, %s, %s::uuid, %s, 'dm', %s, %s, %s::uuid, %s, %s, %s, %s::uuid, %s)
                ON CONFLICT (platform, message_id) DO NOTHING
                """,
                (
                    platform,
                    message_id,
                    str(uuid4()),
                    uid,
                    chat_address,
                    scope_key,
                    tid,
                    run_id,
                    status,
                    Jsonb({"platform_user_id": f"p_{uid}"}),
                    weixin_account_id,
                    weixin_bot_gen,
                ),
            )


async def _seed_weixin_account(
    pool: AsyncConnectionPool, *, uid: str, prefix: str
) -> str:
    """Seed an active Weixin account with REAL pgp_sym_encrypt ciphertext so
    ``unbind_weixin_account`` genuinely decrypts and deletes the account."""
    aid = str(uuid4())
    enc_key = get_encryption_key()
    wx_uid = f"wx_{prefix}@im.wechat"
    bot_id = f"bot_{prefix}@im.wechat"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_channel_weixin_accounts (
                    account_id, user_id, ilink_bot_id, ilink_user_id,
                    encrypted_bot_token, status, bot_generation
                )
                VALUES (%s::uuid, %s, %s, %s, pgp_sym_encrypt(%s, %s), 'active', 1)
                """,
                (aid, uid, bot_id, wx_uid, f"wx-bot-token-{prefix}", enc_key),
            )
            await cur.execute(
                """
                INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id)
                VALUES (%s, 'weixin', %s)
                ON CONFLICT (user_id, platform) DO UPDATE SET platform_user_id = EXCLUDED.platform_user_id
                """,
                (uid, wx_uid),
            )
    return aid


def _patch_live_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    *,
    interrupt_id: str,
    action_req: dict[str, Any] | None = None,
    kind: str = "ask_user_question",
) -> None:
    req = action_req or {"type": "ask_user_question", "question": "Proceed?"}
    live_mock = create_autospec(
        cat.load_live_pending_interrupts,
        return_value=(
            {interrupt_id: {"id": interrupt_id, "value": {"kind": kind, "action_requests": [req]}}},
            {},
        ),
    )
    monkeypatch.setattr(cat, "load_live_pending_interrupts", live_mock)


# ===========================================================================
# Real-DB Migration Round-Trip (d058 -> d055 -> d058) & Constraint Verification
# ===========================================================================


@pytest.mark.asyncio
async def test_d058_real_db_upgrade_downgrade_roundtrip_and_constraints(pg_b3_env) -> None:
    """Execute real Alembic ``d058 -> d055 -> d058`` round-trip against the isolated ``_test`` DB,
    verifying 3 tables, 7 additive delivery columns, 10 indexes, CHECK constraints (including
    late-card constraint), and Weixin FK ``ON DELETE SET NULL``."""
    pool = pg_b3_env["pool"]
    db_uri = pg_b3_env["db_uri"]
    tid = pg_b3_env["tid"]
    uid = pg_b3_env["uid"]
    alembic_cfg = _build_dalton_alembic_cfg(db_uri)

    expected_tables = {
        "dalton_channel_interrupt_runtime_config",
        "dalton_channel_action_tokens",
        "dalton_channel_interrupt_resumes",
    }
    expected_cols = {
        "bt2_enrolled",
        "source_delivery_key",
        "deliverable_manifest",
        "authorized_target_meta",
        "claim_attempt_id",
        "claim_gateway_epoch",
        "claim_lease_expires_at",
    }
    expected_indexes = [
        "idx_dalton_channel_action_tokens_interrupt",
        "idx_dalton_channel_action_tokens_expires",
        "idx_dalton_channel_action_tokens_pending_bind",
        "idx_dalton_channel_action_tokens_late_patch",
        "idx_dalton_channel_interrupt_resumes_unadmitted",
        "idx_dalton_channel_interrupt_resumes_unconsumed",
        "idx_dalton_channel_interrupt_resumes_unacked",
        "idx_dalton_channel_interrupt_resumes_admitted_run",
        "idx_dalton_channel_interrupt_resumes_inbound",
        "idx_dalton_channel_deliveries_links_recovery",
    ]

    try:
        # 1) Downgrade d058 -> d055 and verify full rollback of 3 tables, 7 columns, 10 indexes
        await asyncio.to_thread(alembic_command.downgrade, alembic_cfg, "d055")
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = ANY(%s::text[])
                    """,
                    (list(expected_tables),),
                )
                assert await cur.fetchall() == []

                await cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'dalton_channel_deliveries'
                      AND column_name = ANY(%s::text[])
                    """,
                    (list(expected_cols),),
                )
                assert await cur.fetchall() == []

                await cur.execute(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE schemaname = 'public' AND indexname = ANY(%s::text[])
                    """,
                    (expected_indexes,),
                )
                assert await cur.fetchall() == []
    finally:
        # 2) Re-upgrade d055 -> d058 (always restore head even on assertion failure)
        await asyncio.to_thread(alembic_command.upgrade, alembic_cfg, "d058")

    # 3) Verify schema, 7 columns, 10 indexes, CHECK constraints & Weixin FK ON DELETE SET NULL
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'dalton_channel_deliveries'
                  AND column_name = ANY(%s::text[])
                """,
                (list(expected_cols),),
            )
            assert {r["column_name"] for r in await cur.fetchall()} == expected_cols

            await cur.execute(
                """
                SELECT indexname FROM pg_indexes
                WHERE schemaname = 'public' AND indexname = ANY(%s::text[])
                """,
                (expected_indexes,),
            )
            assert {r["indexname"] for r in await cur.fetchall()} == set(expected_indexes)

            # CHECK 1: runtime_config forbids ledger=FALSE & interactive=TRUE
            with pytest.raises(psycopg.errors.CheckViolation):
                await cur.execute(
                    """
                    UPDATE dalton_channel_interrupt_runtime_config
                    SET interrupt_ledger_enabled = FALSE, interactive_actions_enabled = TRUE
                    WHERE singleton_id = 1
                    """
                )

            # CHECK 2: send_in_flight requires send_attempt_started_at NOT NULL
            with pytest.raises(psycopg.errors.CheckViolation):
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_action_tokens (
                        token_hash, token_role, platform, dalton_user_id, initiator_platform_user_id,
                        origin_chat_type, origin_chat_address, origin_scope_key, session_generation,
                        card_chat_type, card_delivery_address, bind_status, send_attempt_started_at,
                        thread_id, run_id, interrupt_id, risk_tier, action_type, args_sha256, expires_at
                    )
                    VALUES (
                        %s, 'direct', 'feishu', %s, 'ou_1',
                        'dm', 'feishu:@me', 'dm:ou_1', 1,
                        'dm', 'feishu:user:ou_1', 'send_in_flight', NULL,
                        %s::uuid, 'r1', 'i1', 'L0', 'ask_user_question', 'abc', NOW() + INTERVAL '1 hour'
                    )
                    """,
                    ("h" * 64, uid, tid),
                )

            # CHECK 3: late_patch_status = 'pending' requires bind_status = 'bound' & card_message_id NOT NULL
            with pytest.raises(psycopg.errors.CheckViolation):
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_action_tokens (
                        token_hash, token_role, platform, dalton_user_id, initiator_platform_user_id,
                        origin_chat_type, origin_chat_address, origin_scope_key, session_generation,
                        card_chat_type, card_delivery_address, bind_status, late_patch_status,
                        thread_id, run_id, interrupt_id, risk_tier, action_type, args_sha256, expires_at
                    )
                    VALUES (
                        %s, 'direct', 'feishu', %s, 'ou_1',
                        'dm', 'feishu:@me', 'dm:ou_1', 1,
                        'dm', 'feishu:user:ou_1', 'pending_bind', 'pending',
                        %s::uuid, 'r1', 'i1', 'L0', 'ask_user_question', 'abc', NOW() + INTERVAL '1 hour'
                    )
                    """,
                    ("g" * 64, uid, tid),
                )

            # CHECK 4: channel_ack_at NOT NULL with requires_gateway_ack=TRUE and pending card fails
            with pytest.raises(psycopg.errors.CheckViolation):
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_interrupt_resumes (
                        thread_id, run_id, interrupt_id, settled_via, settled_by_user_id,
                        request_key, resume_payload, card_targets,
                        resume_status, requires_gateway_ack, delivery_settled,
                        run_consumed_at, channel_ack_at
                    )
                    VALUES (
                        %s::uuid, 'r1', 'i1', 'web', %s,
                        %s::uuid, '{}'::jsonb, '[{"token_hash":"t1","patch_status":"pending"}]'::jsonb,
                        'admitted', TRUE, TRUE, NOW(), NOW()
                    )
                    """,
                    (tid, uid, str(uuid4())),
                )

    # FK behavior: fk_dalton_interrupt_resumes_wx_account ON DELETE SET NULL + CHECK verification
    wx_aid = await _seed_weixin_account(pool, uid=uid, prefix=pg_b3_env["prefix"])
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # CHECK 5 (d058:229-233): formal channel_text row (legacy_direct=FALSE) without
            # inbound_message_id MUST be rejected by dalton_channel_interrupt_resumes_check.
            with pytest.raises(psycopg.errors.CheckViolation):
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_interrupt_resumes (
                        thread_id, run_id, interrupt_id, settled_via, settled_by_user_id,
                        platform, weixin_account_id, request_key, resume_payload,
                        resume_status, requires_gateway_ack, delivery_settled, lease_expires_at
                    )
                    VALUES (
                        %s::uuid, 'r-fk-bad', 'i-fk-bad', 'channel_text', %s,
                        'weixin', %s::uuid, %s::uuid, '{}'::jsonb,
                        'pending_dispatch', TRUE, FALSE, NOW() + INTERVAL '90 seconds'
                    )
                    """,
                    (tid, uid, wx_aid, str(uuid4())),
                )

            # Valid formal channel_text resume row (platform='weixin', inbound_message_id set)
            await cur.execute(
                """
                INSERT INTO dalton_channel_interrupt_resumes (
                    thread_id, run_id, interrupt_id, settled_via, settled_by_user_id,
                    platform, inbound_message_id, weixin_account_id, request_key, resume_payload,
                    resume_status, requires_gateway_ack, delivery_settled, lease_expires_at
                )
                VALUES (
                    %s::uuid, 'r-fk', 'i-fk', 'channel_text', %s,
                    'weixin', %s, %s::uuid, %s::uuid, '{}'::jsonb,
                    'pending_dispatch', TRUE, FALSE, NOW() + INTERVAL '90 seconds'
                )
                """,
                (tid, uid, f"{pg_b3_env['prefix']}_fk_mid", wx_aid, str(uuid4())),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_weixin_accounts WHERE account_id = %s::uuid",
                (wx_aid,),
            )
            await cur.execute(
                """
                SELECT weixin_account_id FROM dalton_channel_interrupt_resumes
                WHERE thread_id = %s::uuid AND run_id = 'r-fk' AND interrupt_id = 'i-fk'
                """,
                (tid,),
            )
            fk_row = await cur.fetchone()
            assert fk_row is not None
            assert fk_row["weixin_account_id"] is None


# ===========================================================================
# All 11 Concurrency Interleavings (§3.2 #4) against Real PostgreSQL
# ===========================================================================


@pytest.mark.asyncio
async def test_pg_interleaving_01_claim_text_inbound_progress_lease_heartbeat(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ①: claim-text + update_inbound_progress + heartbeat_gateway_lease (FOR UPDATE)
    overlapped at preview barrier."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    mid = f"{pg_b3_env['prefix']}_m01"
    scope = f"dm:{pg_b3_env['feishu_puid']}"
    _patch_live_checkpoint(monkeypatch, interrupt_id="int-01")
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=mid, uid=uid,
        scope_key=scope, chat_address="feishu:@me",
        tid=tid, run_id="run-01", interrupt_id="int-01",
    )

    entered_preview = asyncio.Event()
    release_preview = asyncio.Event()

    async def _hook(op: str) -> None:
        if op == "claim_text":
            entered_preview.set()
            await asyncio.wait_for(release_preview.wait(), timeout=_SYNC_TIMEOUT)

    cat._after_preview_hook = _hook

    async def _run_peers():
        try:
            await asyncio.wait_for(entered_preview.wait(), timeout=_SYNC_TIMEOUT)
            release_preview.set()
            return await asyncio.gather(
                cg_db.update_inbound_progress(
                    platform="feishu", message_id=mid, fallback_pending=False,
                    gateway_epoch=epoch, require_gateway_epoch=True,
                ),
                wx_db.heartbeat_gateway_lease(
                    instance_id=pg_b3_env["instance_id"], epoch=epoch, ttl_seconds=120
                ),
            )
        finally:
            release_preview.set()

    claim_res, (prog_res, hb_res) = await asyncio.gather(
        cat.claim_channel_text_interrupt_resume(
            platform="feishu", message_id=mid, dalton_user_id=uid,
            scope_key=scope, generation=1,
            thread_id=tid, run_id="run-01", interrupt_id="int-01",
            resume_payload={"hitl_response": {"int-01": {"answer": "ok"}}},
            gateway_epoch=epoch,
        ),
        _run_peers(),
    )
    assert claim_res["ok"] is True
    assert prog_res["ok"] is True
    assert hb_res["ok"] is True
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status, fallback_pending FROM dalton_channel_inbound_events WHERE platform = 'feishu' AND message_id = %s",
                (mid,),
            )
            row = await cur.fetchone()
            assert row["status"] == "hitl_claimed"
            assert row["fallback_pending"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["new_lt_old", "new_gt_old"])
async def test_pg_interleaving_02_new_claim_text_takeover_both_pk_orders_and_old_ack(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch, order: str
) -> None:
    """Interleaving ②: new claim-text taking over expired channel_text in BOTH PK orders + old ack,
    overlapping after preview with strict outcome and DB state verification."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    pfx = pg_b3_env["prefix"]
    old_mid = f"{pfx}_b_mid" if order == "new_lt_old" else f"{pfx}_a_mid"
    new_mid = f"{pfx}_a_mid" if order == "new_lt_old" else f"{pfx}_b_mid"
    scope = f"dm:{pg_b3_env['feishu_puid']}"
    _patch_live_checkpoint(monkeypatch, interrupt_id="int-02")
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=old_mid, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id="run-02", interrupt_id="int-02",
    )
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=new_mid, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id="run-02", interrupt_id="int-02",
    )
    c_old = await cat.claim_channel_text_interrupt_resume(
        platform="feishu", message_id=old_mid, dalton_user_id=uid, scope_key=scope, generation=1,
        thread_id=tid, run_id="run-02", interrupt_id="int-02",
        resume_payload={"hitl_response": {"int-02": {"answer": "old"}}},
        gateway_epoch=epoch,
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_interrupt_resumes
                SET lease_expires_at = NOW() - INTERVAL '10 seconds'
                WHERE thread_id = %s::uuid AND run_id = 'run-02' AND interrupt_id = 'int-02'
                """,
                (tid,),
            )

    claim_previewed = asyncio.Event()
    ack_previewed = asyncio.Event()

    async def _hook(op: str) -> None:
        if op == "claim_text":
            claim_previewed.set()
            await asyncio.wait_for(ack_previewed.wait(), timeout=_SYNC_TIMEOUT)
        elif op == "ack_origin":
            ack_previewed.set()
            await asyncio.wait_for(claim_previewed.wait(), timeout=_SYNC_TIMEOUT)

    cat._after_preview_hook = _hook

    async def _run_new():
        try:
            return await cat.claim_channel_text_interrupt_resume(
                platform="feishu", message_id=new_mid, dalton_user_id=uid, scope_key=scope, generation=1,
                thread_id=tid, run_id="run-02", interrupt_id="int-02",
                resume_payload={"hitl_response": {"int-02": {"answer": "new"}}},
                gateway_epoch=epoch,
            )
        finally:
            claim_previewed.set()

    async def _run_old_ack():
        try:
            return await cat.ack_channel_interrupt_resume(
                thread_id=tid, run_id="run-02", interrupt_id="int-02",
                claim_id=c_old["claim_id"], outcome="retry_scheduled", error="timeout",
                gateway_epoch=epoch,
            )
        finally:
            ack_previewed.set()

    new_res, ack_res = await asyncio.gather(_run_new(), _run_old_ack(), return_exceptions=True)

    for item in (new_res, ack_res):
        if isinstance(item, Exception):
            _assert_not_lock_or_constraint_error(item)
            assert isinstance(item, HTTPException) and item.status_code == 409, f"Unexpected error: {item!r}"

    # Exactly two legal serializations:
    # (A) new_claim took over first -> new_res ok, old ack got 409 claim_id_mismatch, old inbound completed, new inbound hitl_claimed
    # (B) old ack ran first (retry_scheduled, lease still expired) -> new_claim retried and took over (both ok), OR if renewed -> new_claim 409 and new_mid pending
    assert isinstance(new_res, dict) or isinstance(ack_res, dict)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT inbound_message_id, claim_id::text AS claim_id FROM dalton_channel_interrupt_resumes WHERE thread_id = %s::uuid AND run_id = 'run-02' AND interrupt_id = 'int-02'",
                (tid,),
            )
            r_row = await cur.fetchone()
            await cur.execute(
                "SELECT message_id, status FROM dalton_channel_inbound_events WHERE platform = 'feishu' AND message_id = ANY(%s::text[])",
                ([old_mid, new_mid],),
            )
            inb_by_mid = {r["message_id"]: r["status"] for r in await cur.fetchall()}

    if isinstance(new_res, dict):
        assert new_res["ok"] is True
        assert r_row["inbound_message_id"] == new_mid
        assert r_row["claim_id"] == new_res["claim_id"]
        assert inb_by_mid[new_mid] == "hitl_claimed"
        assert inb_by_mid[old_mid] == "completed"
    else:
        assert isinstance(ack_res, dict) and ack_res["ok"] is True
        assert inb_by_mid[new_mid] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("contender", ["web", "consume"])
@pytest.mark.parametrize("order", ["contender_first", "ack_first"])
async def test_pg_interleaving_03_web_and_consume_takeover_old_channel_text_and_old_ack(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch, contender: str, order: str
) -> None:
    """Interleaving ③: BOTH web AND button ``consume`` takeover of expired ``channel_text``
    concurrent with old ``ack``, fixing BOTH commit orders (contender_first / ack_first)
    after synchronized preview with strict assertions."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    scope = f"dm:{pg_b3_env['feishu_puid']}"
    old_mid = f"{pg_b3_env['prefix']}_m03_{contender}_{order}"
    run_id = f"run-03-{contender}-{order}"
    int_id = f"int-03-{contender}-{order}"
    action_req = {"type": "ask_user_question", "question": "Proceed?"}
    _patch_live_checkpoint(monkeypatch, interrupt_id=int_id, action_req=action_req)
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=old_mid, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id=run_id, interrupt_id=int_id,
    )

    nonce: str | None = None
    token_hash: str | None = None
    if contender == "consume":
        minted = await cat.mint_channel_action_token(
            platform="feishu", dalton_user_id=uid, initiator_platform_user_id=pg_b3_env["feishu_puid"],
            origin_chat_type="dm", origin_chat_address="feishu:@me", origin_scope_key=scope,
            session_generation=1, thread_id=tid, run_id=run_id, interrupt_id=int_id,
            interrupt_value={"kind": "ask_user_question", "action_requests": [action_req]},
            gateway_epoch=epoch,
        )
        nonce = minted["approval_nonce"]
        token_hash = minted["token_hash"]
        await cat.bind_channel_action_card(
            approval_nonce=nonce, phase="prepare_send", gateway_epoch=epoch
        )
        await cat.bind_channel_action_card(
            approval_nonce=nonce, phase="confirm_bound",
            card_channel_id="oc_dm_03", card_message_id=f"om_card_03_{order}", gateway_epoch=epoch,
        )

    c_old = await cat.claim_channel_text_interrupt_resume(
        platform="feishu", message_id=old_mid, dalton_user_id=uid, scope_key=scope, generation=1,
        thread_id=tid, run_id=run_id, interrupt_id=int_id,
        resume_payload={"hitl_response": {int_id: {"answer": "old"}}},
        gateway_epoch=epoch,
    )
    # Expire old lease; for consume re-arm token as unconsumed so consume can take over the expired claim
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_interrupt_resumes
                SET lease_expires_at = NOW() - INTERVAL '10 seconds'
                WHERE thread_id = %s::uuid AND run_id = %s AND interrupt_id = %s
                """,
                (tid, run_id, int_id),
            )
            if contender == "consume":
                await cur.execute(
                    """
                    UPDATE dalton_channel_action_tokens
                    SET consumed_at = NULL, consumed_via = NULL
                    WHERE thread_id = %s::uuid AND run_id = %s AND interrupt_id = %s
                    """,
                    (tid, run_id, int_id),
                )

    contender_previewed = asyncio.Event()
    ack_previewed = asyncio.Event()
    first_tx_done = asyncio.Event()
    contender_preview_count = 0
    ack_preview_count = 0
    target_op = "claim_web" if contender == "web" else "consume"

    async def _hook(op: str) -> None:
        nonlocal contender_preview_count, ack_preview_count
        if op == target_op:
            contender_preview_count += 1
            if contender_preview_count == 1:
                contender_previewed.set()
                await asyncio.wait_for(ack_previewed.wait(), timeout=_SYNC_TIMEOUT)
                if order == "ack_first":
                    await asyncio.wait_for(first_tx_done.wait(), timeout=_SYNC_TIMEOUT)
        elif op == "ack_origin":
            ack_preview_count += 1
            if ack_preview_count == 1:
                ack_previewed.set()
                await asyncio.wait_for(contender_previewed.wait(), timeout=_SYNC_TIMEOUT)
                if order == "contender_first":
                    await asyncio.wait_for(first_tx_done.wait(), timeout=_SYNC_TIMEOUT)

    cat._after_preview_hook = _hook

    async def _run_contender():
        try:
            if contender == "web":
                return await cat.claim_web_interrupt_resume(
                    thread_id=tid, run_id=run_id, interrupt_id=int_id, user_id=uid,
                    resume_payload={"hitl_response": {int_id: {"answer": "web"}}},
                )
            assert nonce is not None
            return await cat.consume_channel_action_token(
                approval_nonce=nonce, decision="approve",
                clicker_platform_user_id=pg_b3_env["feishu_puid"],
                channel_id="oc_dm_03", message_id=f"om_card_03_{order}", answer_text="button",
                gateway_epoch=epoch,
            )
        finally:
            contender_previewed.set()
            if order == "contender_first":
                first_tx_done.set()

    async def _run_old_ack():
        try:
            return await cat.ack_channel_interrupt_resume(
                thread_id=tid, run_id=run_id, interrupt_id=int_id,
                claim_id=c_old["claim_id"], outcome="retry_scheduled", error="net",
                gateway_epoch=epoch,
            )
        finally:
            ack_previewed.set()
            if order == "ack_first":
                first_tx_done.set()

    c_res, ack_res = await asyncio.gather(_run_contender(), _run_old_ack(), return_exceptions=True)

    for item in (c_res, ack_res):
        if isinstance(item, Exception):
            _assert_not_lock_or_constraint_error(item)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT claim_id::text AS claim_id, settled_via, inbound_message_id
                FROM dalton_channel_interrupt_resumes
                WHERE thread_id = %s::uuid AND run_id = %s AND interrupt_id = %s
                """,
                (tid, run_id, int_id),
            )
            r_row = await cur.fetchone()
            await cur.execute(
                "SELECT status FROM dalton_channel_inbound_events WHERE platform = 'feishu' AND message_id = %s",
                (old_mid,),
            )
            inb_row = await cur.fetchone()
            tok_row = None
            if token_hash is not None:
                await cur.execute(
                    "SELECT consumed_at, consumed_via FROM dalton_channel_action_tokens WHERE token_hash = %s",
                    (token_hash,),
                )
                tok_row = await cur.fetchone()

    if order == "contender_first":
        assert isinstance(c_res, dict) and c_res["ok"] is True
        assert r_row is not None
        assert r_row["settled_via"] == ("web" if contender == "web" else "channel_button")
        assert r_row["inbound_message_id"] is None
        assert r_row["claim_id"] == c_res["claim_id"]
        assert inb_row is not None and inb_row["status"] == "completed"
        assert isinstance(ack_res, HTTPException) and ack_res.status_code == 409
        assert ack_res.detail["code"] == "interrupt_claim_mismatch"
    else:
        assert isinstance(ack_res, dict) and ack_res["ok"] is True
        assert isinstance(c_res, HTTPException) and c_res.status_code == 409
        if contender == "web":
            assert c_res.detail["code"] == "already_settled_in_channel"
        else:
            # ack renewed lease -> consume takeover denied at channel_action_tokens.py:3184-3193
            # before token consumption; resume is still pending_dispatch (delivery_settled=False)
            assert c_res.detail["code"] == "already_settled"
            assert c_res.detail["can_patch_terminal"] is False
            assert c_res.detail["patch_ack_mode"] is None
            assert tok_row is not None and tok_row["consumed_at"] is None
        assert r_row is not None
        assert r_row["claim_id"] == c_old["claim_id"]
        assert r_row["settled_via"] == "channel_text"
        assert r_row["inbound_message_id"] == old_mid
        assert inb_row is not None and inb_row["status"] == "hitl_claimed"


@pytest.mark.asyncio
async def test_pg_interleaving_04_claim_text_and_recover_startup_and_interrupt_resumes(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ④: claim-text pauses AFTER first INSERT into ``dalton_channel_interrupt_resumes``
    (before commit) AND again while holding ``FOR UPDATE`` on the committed 7b-2 row; concurrent
    ``/internal/recover`` (``startup`` & ``interrupt_resumes``) MUST skip the locked row via
    ``SKIP LOCKED``, exclude it from responses, and leave it unmodified."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    scope = f"dm:{pg_b3_env['feishu_puid']}"
    mid = f"{pg_b3_env['prefix']}_m04"
    _patch_live_checkpoint(monkeypatch, interrupt_id="int-04")
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=mid, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id="run-04", interrupt_id="int-04",
    )

    def _has_row(rec: dict[str, Any]) -> bool:
        return any(
            str(r.get("thread_id")) == tid
            and str(r.get("run_id")) == "run-04"
            and str(r.get("interrupt_id")) == "int-04"
            for r in (rec.get("interrupt_resumes") or [])
        )

    # Phase A: pause AFTER first INSERT into dalton_channel_interrupt_resumes, BEFORE commit
    inserted_uncommitted = asyncio.Event()
    phase_a_recovers_done = asyncio.Event()

    async def _hook_after_insert(op: str) -> None:
        if op == "claim_text_after_insert":
            inserted_uncommitted.set()
            await asyncio.wait_for(phase_a_recovers_done.wait(), timeout=_SYNC_TIMEOUT)

    cat._after_7b2_lock_hook = _hook_after_insert

    async def _run_first_claim():
        try:
            return await cat.claim_channel_text_interrupt_resume(
                platform="feishu", message_id=mid, dalton_user_id=uid, scope_key=scope, generation=1,
                thread_id=tid, run_id="run-04", interrupt_id="int-04",
                resume_payload={"hitl_response": {"int-04": {"answer": "yes"}}},
                gateway_epoch=epoch,
            )
        finally:
            inserted_uncommitted.set()

    async def _run_phase_a_recovers():
        try:
            await asyncio.wait_for(inserted_uncommitted.wait(), timeout=_SYNC_TIMEOUT)
            rec_start = await cg_db.recover_channel_state(
                mode="startup", gateway_epoch=epoch, require_gateway_epoch=True
            )
            rec_ir = await cg_db.recover_channel_state(
                mode="interrupt_resumes", gateway_epoch=epoch, require_gateway_epoch=True
            )
            return rec_start, rec_ir
        finally:
            phase_a_recovers_done.set()

    claim_res, (rec_start_a, rec_ir_a) = await asyncio.gather(_run_first_claim(), _run_phase_a_recovers())
    assert claim_res["ok"] is True
    assert rec_start_a["mode"] == "startup"
    assert rec_ir_a["mode"] == "interrupt_resumes"
    assert not _has_row(rec_start_a), "startup recover must not return uncommitted inserted resume row"
    assert not _has_row(rec_ir_a), "interrupt_resumes recover must not return uncommitted inserted resume row"

    # Phase B: now the 7b-2 row is committed; hold FOR UPDATE on it in claim-text and verify SKIP LOCKED skips it
    holding_committed_7b2 = asyncio.Event()
    phase_b_recovers_done = asyncio.Event()

    async def _hook_committed_7b2(op: str) -> None:
        if op == "claim_text":
            holding_committed_7b2.set()
            await asyncio.wait_for(phase_b_recovers_done.wait(), timeout=_SYNC_TIMEOUT)

    cat._after_7b2_lock_hook = _hook_committed_7b2

    async def _run_replay_claim():
        try:
            return await cat.claim_channel_text_interrupt_resume(
                platform="feishu", message_id=mid, dalton_user_id=uid, scope_key=scope, generation=1,
                thread_id=tid, run_id="run-04", interrupt_id="int-04",
                resume_payload={"hitl_response": {"int-04": {"answer": "yes"}}},
                gateway_epoch=epoch,
            )
        finally:
            holding_committed_7b2.set()

    async def _run_phase_b_recovers():
        try:
            await asyncio.wait_for(holding_committed_7b2.wait(), timeout=_SYNC_TIMEOUT)
            rec_start = await cg_db.recover_channel_state(
                mode="startup", gateway_epoch=epoch, require_gateway_epoch=True
            )
            rec_ir = await cg_db.recover_channel_state(
                mode="interrupt_resumes", gateway_epoch=epoch, require_gateway_epoch=True
            )
            return rec_start, rec_ir
        finally:
            phase_b_recovers_done.set()

    replay_res, (rec_start_b, rec_ir_b) = await asyncio.gather(_run_replay_claim(), _run_phase_b_recovers())
    cat._after_7b2_lock_hook = None
    assert replay_res["ok"] is True and replay_res.get("idempotent_replay") is True
    assert not _has_row(rec_start_b), "startup SKIP LOCKED must skip row locked FOR UPDATE by claim-text"
    assert not _has_row(rec_ir_b), "interrupt_resumes SKIP LOCKED must skip row locked FOR UPDATE by claim-text"

    # Verify DB row committed cleanly and unmodified, and is returned once lock is released
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT claim_id::text AS claim_id, resume_status, inbound_message_id
                FROM dalton_channel_interrupt_resumes
                WHERE thread_id = %s::uuid AND run_id = 'run-04' AND interrupt_id = 'int-04'
                """,
                (tid,),
            )
            r_row = await cur.fetchone()
            assert r_row is not None
            assert r_row["claim_id"] == claim_res["claim_id"]
            assert r_row["resume_status"] == "pending_dispatch"
            assert r_row["inbound_message_id"] == mid

    rec_after = await cg_db.recover_channel_state(
        mode="interrupt_resumes", gateway_epoch=epoch, require_gateway_epoch=True
    )
    assert _has_row(rec_after), "interrupt_resumes should return committed row once lock is released"


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["recover_first", "unbind_first"])
async def test_pg_interleaving_05_recover_and_unbind_weixin_account(
    pg_b3_env, order: str
) -> None:
    """Interleaving ⑤: deterministically fix BOTH lock orders between ``/internal/recover``
    (holding 3a ``FOR KEY SHARE`` -> backfilling wakeup row into ``dalton_channel_background_runs``)
    and ``unbind_weixin_account`` (holding 3a ``FOR UPDATE`` -> deleting account).
    Both orders MUST actually backfill the wakeup row (verified via row count), succeed on unbind,
    and leave ``weixin_account_id IS NULL``."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    aid = await _seed_weixin_account(pool, uid=uid, prefix=pg_b3_env["prefix"])
    wx_uid = f"wx_{pg_b3_env['prefix']}@im.wechat"
    bg_rid = str(uuid4())

    # Seed an active Weixin thread_watch AND a wakeup query+response > last_seen_turn_index
    # so watch_reconnect actually exercises INSERT INTO dalton_channel_background_runs (FK -> weixin_accounts)!
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_channel_thread_watches (
                    thread_id, dalton_user_id, platform, target_address,
                    scope_key, generation, watch_active, last_seen_turn_index,
                    weixin_account_id, weixin_recipient_id, weixin_bot_generation
                )
                VALUES (%s::uuid, %s, 'weixin', 'weixin:@me', %s, 1, TRUE, 0, %s::uuid, %s, 1)
                """,
                (tid, uid, f"dm:{wx_uid}", aid, wx_uid),
            )
            await cur.execute(
                """
                INSERT INTO conversation_queries (
                    conversation_query_id, conversation_thread_id, turn_index, content, type, created_at
                )
                VALUES (%s::uuid, %s::uuid, 1, 'wakeup check', 'wakeup', NOW())
                """,
                (str(uuid4()), tid),
            )
            await cur.execute(
                """
                INSERT INTO conversation_responses (
                    conversation_response_id, conversation_thread_id, turn_index, status, created_at
                )
                VALUES (%s::uuid, %s::uuid, 1, 'in_progress', NOW())
                """,
                (bg_rid, tid),
            )

    if order == "recover_first":
        recover_holds_3a = asyncio.Event()
        release_recover = asyncio.Event()

        async def _rec_3a_hook(mode_name: str) -> None:
            if mode_name == "watch_reconnect":
                recover_holds_3a.set()
                await asyncio.wait_for(release_recover.wait(), timeout=_SYNC_TIMEOUT)

        cg_db._after_recover_3a_lock_hook = _rec_3a_hook
        rec_watch_task = asyncio.create_task(
            cg_db.recover_channel_state(
                mode="watch_reconnect", thread_id=tid, gateway_epoch=epoch, require_gateway_epoch=True
            )
        )
        try:
            await asyncio.wait_for(recover_holds_3a.wait(), timeout=_SYNC_TIMEOUT)
            unbind_task = asyncio.create_task(
                wx_db.unbind_weixin_account(
                    account_id=aid, operator_user_id=uid, action="member_unbind"
                )
            )
            wait_row = await _wait_for_pg_lock_wait(pool, "%dalton_channel_weixin_accounts%")
            assert wait_row["wait_event_type"] == "Lock"
            assert not unbind_task.done()
        finally:
            release_recover.set()
        rec_watch, unbind_res = await asyncio.wait_for(
            asyncio.gather(rec_watch_task, unbind_task), timeout=_SYNC_TIMEOUT
        )
        cg_db._after_recover_3a_lock_hook = None
    else:
        unbind_holds_3a = asyncio.Event()
        release_unbind = asyncio.Event()

        async def _unbind_3a_hook(_op: str) -> None:
            unbind_holds_3a.set()
            await asyncio.wait_for(release_unbind.wait(), timeout=_SYNC_TIMEOUT)

        wx_db._after_unbind_3a_lock_hook = _unbind_3a_hook
        unbind_task = asyncio.create_task(
            wx_db.unbind_weixin_account(
                account_id=aid, operator_user_id=uid, action="member_unbind"
            )
        )
        try:
            await asyncio.wait_for(unbind_holds_3a.wait(), timeout=_SYNC_TIMEOUT)
            rec_watch_task = asyncio.create_task(
                cg_db.recover_channel_state(
                    mode="watch_reconnect", thread_id=tid, gateway_epoch=epoch, require_gateway_epoch=True
                )
            )
            wait_row = await _wait_for_pg_lock_wait(pool, "%dalton_channel_weixin_accounts%")
            assert wait_row["wait_event_type"] == "Lock"
            assert not rec_watch_task.done()
        finally:
            release_unbind.set()
        unbind_res, rec_watch = await asyncio.wait_for(
            asyncio.gather(unbind_task, rec_watch_task), timeout=_SYNC_TIMEOUT
        )
        wx_db._after_unbind_3a_lock_hook = None

    rec_start, rec_ir = await asyncio.gather(
        cg_db.recover_channel_state(mode="startup", gateway_epoch=epoch, require_gateway_epoch=True),
        cg_db.recover_channel_state(mode="interrupt_resumes", gateway_epoch=epoch, require_gateway_epoch=True),
    )
    assert rec_start["mode"] == "startup"
    assert rec_ir["mode"] == "interrupt_resumes"
    assert rec_watch["mode"] == "watch_reconnect"
    assert unbind_res["ok"] is True
    assert unbind_res["deleted_account"] is True

    # Verify backfill genuinely inserted the wakeup row in dalton_channel_background_runs,
    # Weixin account was deleted, and both watch & background_run FKs are NULL!
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*) AS cnt, MAX(weixin_account_id::text) AS wx_aid
                FROM dalton_channel_background_runs
                WHERE thread_id = %s::uuid AND run_id = %s
                """,
                (tid, bg_rid),
            )
            bg_row = await cur.fetchone()
            assert bg_row is not None and int(bg_row["cnt"]) == 1, (
                f"Expected wakeup background_run row to be backfilled for order={order}, got {bg_row!r}"
            )
            assert bg_row["wx_aid"] is None
            await cur.execute(
                "SELECT 1 FROM dalton_channel_weixin_accounts WHERE account_id = %s::uuid",
                (aid,),
            )
            assert await cur.fetchone() is None
            await cur.execute(
                "SELECT weixin_account_id FROM dalton_channel_thread_watches WHERE thread_id = %s::uuid",
                (tid,),
            )
            w_row = await cur.fetchone()
            assert w_row is not None and w_row["weixin_account_id"] is None


@pytest.mark.asyncio
async def test_pg_interleaving_06_weixin_claim_text_and_unbind_weixin_account(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ⑥: Weixin claim-text + real unbind_weixin_account (3a FOR KEY SHARE vs FOR UPDATE),
    overlapped after preview. Unbind MUST succeed; claim-text either commits cleanly before unbind
    (with FK SET NULL afterward) or fails cleanly with 403/404/409."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    aid = await _seed_weixin_account(pool, uid=uid, prefix=pg_b3_env["prefix"])
    mid = f"{pg_b3_env['prefix']}_wx06"
    scope = f"dm:wx_{pg_b3_env['prefix']}@im.wechat"
    _patch_live_checkpoint(monkeypatch, interrupt_id="int-06")
    await _seed_session_and_inbound(
        pool, platform="weixin", message_id=mid, uid=uid, scope_key=scope,
        chat_address="weixin:@me", tid=tid, run_id="run-06", interrupt_id="int-06",
        weixin_account_id=aid, weixin_bot_gen=1,
    )

    preview_done = asyncio.Event()

    async def _hook(op: str) -> None:
        if op == "claim_text":
            preview_done.set()

    cat._after_preview_hook = _hook

    async def _run_claim():
        try:
            return await cat.claim_channel_text_interrupt_resume(
                platform="weixin", message_id=mid, dalton_user_id=uid, scope_key=scope, generation=1,
                thread_id=tid, run_id="run-06", interrupt_id="int-06",
                resume_payload={"hitl_response": {"int-06": {"answer": "1"}}},
                gateway_epoch=epoch,
            )
        finally:
            preview_done.set()

    async def _run_unbind():
        await asyncio.wait_for(preview_done.wait(), timeout=_SYNC_TIMEOUT)
        return await wx_db.unbind_weixin_account(
            account_id=aid, operator_user_id=uid, action="member_unbind"
        )

    claim_res, unbind_res = await asyncio.gather(_run_claim(), _run_unbind(), return_exceptions=True)
    # Unbind MUST succeed (never swallow unbind failure!)
    assert isinstance(unbind_res, dict) and unbind_res["ok"] is True
    assert unbind_res["deleted_account"] is True

    if isinstance(claim_res, Exception):
        _assert_not_lock_or_constraint_error(claim_res)
        assert isinstance(claim_res, HTTPException) and claim_res.status_code in (403, 404, 409)
    else:
        assert claim_res["ok"] is True
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT weixin_account_id FROM dalton_channel_interrupt_resumes WHERE thread_id = %s::uuid AND run_id = 'run-06' AND interrupt_id = 'int-06'",
                    (tid,),
                )
                r_row = await cur.fetchone()
                assert r_row is not None and r_row["weixin_account_id"] is None


@pytest.mark.asyncio
async def test_pg_interleaving_07_three_terminal_race_consume_claim_text_web_and_on_start(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ⑦: real 4-party concurrent race — consume + claim-text + web claim
    synchronized at preview barrier + concurrent START ``_on_start`` holding Level 0c
    ``conversation_threads FOR UPDATE``."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    scope = f"dm:{pg_b3_env['feishu_puid']}"
    mid = f"{pg_b3_env['prefix']}_m07"
    mid2 = f"{pg_b3_env['prefix']}_m07_p2"
    action_req = {"type": "ask_user_question", "question": "Proceed?"}
    _patch_live_checkpoint(monkeypatch, interrupt_id="int-07", action_req=action_req)
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=mid, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id="run-07", interrupt_id="int-07",
    )
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=mid2, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id="run-07", interrupt_id="int-07",
    )
    minted = await cat.mint_channel_action_token(
        platform="feishu", dalton_user_id=uid, initiator_platform_user_id=pg_b3_env["feishu_puid"],
        origin_chat_type="dm", origin_chat_address="feishu:@me", origin_scope_key=scope,
        session_generation=1, thread_id=tid, run_id="run-07", interrupt_id="int-07",
        interrupt_value={"kind": "ask_user_question", "action_requests": [action_req]},
        gateway_epoch=epoch,
    )
    await cat.bind_channel_action_card(
        approval_nonce=minted["approval_nonce"], phase="prepare_send", gateway_epoch=epoch
    )
    await cat.bind_channel_action_card(
        approval_nonce=minted["approval_nonce"], phase="confirm_bound",
        card_channel_id="oc_dm_07", card_message_id="om_card_07", gateway_epoch=epoch,
    )
    # Second bound card token used as losing button contender in Phase 2 regardless of Phase 1 winner
    minted2 = await cat.mint_channel_action_token(
        platform="feishu", dalton_user_id=uid, initiator_platform_user_id=pg_b3_env["feishu_puid"],
        origin_chat_type="dm", origin_chat_address="feishu:@me", origin_scope_key=scope,
        session_generation=1, thread_id=tid, run_id="run-07", interrupt_id="int-07",
        interrupt_value={"kind": "ask_user_question", "action_requests": [action_req]},
        gateway_epoch=epoch,
    )
    await cat.bind_channel_action_card(
        approval_nonce=minted2["approval_nonce"], phase="prepare_send", gateway_epoch=epoch
    )
    await cat.bind_channel_action_card(
        approval_nonce=minted2["approval_nonce"], phase="confirm_bound",
        card_channel_id="oc_dm_07", card_message_id="om_card_07_2", gateway_epoch=epoch,
    )

    arrived = 0
    all_arrived = asyncio.Event()

    async def _barrier_hook(op: str) -> None:
        nonlocal arrived
        if op in ("consume", "claim_text", "claim_web") and not all_arrived.is_set():
            arrived += 1
            if arrived >= 3:
                all_arrived.set()
            await asyncio.wait_for(all_arrived.wait(), timeout=_SYNC_TIMEOUT)

    cat._after_preview_hook = _barrier_hook

    async def _start_contender_stale():
        # Concurrently run START transaction locking Level 0c (conversation_threads FOR UPDATE)
        # while the 3 claimants race on Level 7b-2; bogus claim_id MUST get 409 interrupt_claim_lost!
        await asyncio.wait_for(all_arrived.wait(), timeout=_SYNC_TIMEOUT)
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT conversation_thread_id FROM conversation_threads WHERE conversation_thread_id = %s::uuid FOR UPDATE",
                        (tid,),
                    )
                    await cat.admit_interrupt_resume_in_start_tx(
                        cur, thread_id=tid, run_id="run-07", interrupt_id="int-07",
                        claim_id=str(uuid4()), request_key=str(uuid4()),
                        admitted_run_id=str(uuid4()),
                    )

    async def _wrap(coro):
        try:
            return await coro
        finally:
            all_arrived.set()

    c_btn, c_txt, c_web, start_bogus = await asyncio.gather(
        _wrap(
            cat.consume_channel_action_token(
                approval_nonce=minted["approval_nonce"], decision="approve",
                clicker_platform_user_id=pg_b3_env["feishu_puid"],
                channel_id="oc_dm_07", message_id="om_card_07", answer_text="yes",
                gateway_epoch=epoch,
            )
        ),
        _wrap(
            cat.claim_channel_text_interrupt_resume(
                platform="feishu", message_id=mid, dalton_user_id=uid, scope_key=scope, generation=1,
                thread_id=tid, run_id="run-07", interrupt_id="int-07",
                resume_payload={"hitl_response": {"int-07": {"answer": "text"}}},
                gateway_epoch=epoch,
            )
        ),
        _wrap(
            cat.claim_web_interrupt_resume(
                thread_id=tid, run_id="run-07", interrupt_id="int-07", user_id=uid,
                resume_payload={"hitl_response": {"int-07": {"answer": "web"}}},
            )
        ),
        _start_contender_stale(),
        return_exceptions=True,
    )

    claim_results = [c_btn, c_txt, c_web]
    winners = [r for r in claim_results if isinstance(r, dict) and r.get("ok")]
    assert len(winners) == 1, f"Expected exactly 1 winner, got {claim_results!r}"
    for item in claim_results:
        if isinstance(item, Exception):
            _assert_not_lock_or_constraint_error(item)
            assert isinstance(item, HTTPException) and item.status_code == 409
    assert isinstance(start_bogus, HTTPException) and start_bogus.status_code == 409
    assert start_bogus.detail["code"] == "interrupt_claim_lost"

    # Phase 2: true concurrent race — winner's START holds 0c (conversation_threads FOR UPDATE)
    # and pauses BEFORE 7b-2 while ALL THREE losing contenders (button, text, web) request concurrently!
    win = winners[0]
    admitted_rid = str(uuid4())
    start_before_7b2 = asyncio.Event()
    losers_entered = asyncio.Event()
    losers_previewed = 0

    async def _phase2_before_7b2(op: str) -> None:
        if op == "admit_on_start":
            start_before_7b2.set()
            await asyncio.wait_for(losers_entered.wait(), timeout=_SYNC_TIMEOUT)

    async def _phase2_loser_preview(op: str) -> None:
        nonlocal losers_previewed
        if op in ("consume", "claim_text", "claim_web") and not losers_entered.is_set():
            losers_previewed += 1
            if losers_previewed >= 3:
                losers_entered.set()

    cat._before_7b2_lock_hook = _phase2_before_7b2
    cat._after_preview_hook = _phase2_loser_preview

    async def _real_start():
        try:
            async with pool.connection() as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT conversation_thread_id FROM conversation_threads WHERE conversation_thread_id = %s::uuid FOR UPDATE",
                            (tid,),
                        )
                        return await cat.admit_interrupt_resume_in_start_tx(
                            cur, thread_id=tid, run_id="run-07", interrupt_id="int-07",
                            claim_id=win["claim_id"], request_key=win["request_key"],
                            admitted_run_id=admitted_rid,
                        )
        finally:
            start_before_7b2.set()

    async def _phase2_loser(coro):
        await asyncio.wait_for(start_before_7b2.wait(), timeout=_SYNC_TIMEOUT)
        try:
            return await coro
        finally:
            if losers_previewed >= 3:
                losers_entered.set()

    try:
        start_res, late_btn, late_txt, late_web = await asyncio.gather(
            _real_start(),
            _phase2_loser(
                cat.consume_channel_action_token(
                    approval_nonce=minted2["approval_nonce"], decision="approve",
                    clicker_platform_user_id=pg_b3_env["feishu_puid"],
                    channel_id="oc_dm_07", message_id="om_card_07_2", answer_text="late-btn",
                    gateway_epoch=epoch,
                )
            ),
            _phase2_loser(
                cat.claim_channel_text_interrupt_resume(
                    platform="feishu", message_id=mid2, dalton_user_id=uid, scope_key=scope, generation=1,
                    thread_id=tid, run_id="run-07", interrupt_id="int-07",
                    resume_payload={"hitl_response": {"int-07": {"answer": "late-txt"}}},
                    gateway_epoch=epoch,
                )
            ),
            _phase2_loser(
                cat.claim_web_interrupt_resume(
                    thread_id=tid, run_id="run-07", interrupt_id="int-07", user_id=uid,
                    resume_payload={"hitl_response": {"int-07": {"answer": "late-web"}}},
                )
            ),
            return_exceptions=True,
        )
    finally:
        losers_entered.set()
        cat._before_7b2_lock_hook = None
        cat._after_preview_hook = None

    assert start_res is None, f"admit_interrupt_resume_in_start_tx returns None on success, got {start_res!r}"
    for loser in (late_btn, late_txt, late_web):
        assert isinstance(loser, HTTPException), f"Expected 409 HTTPException, got {loser!r}"
        _assert_not_lock_or_constraint_error(loser)
        assert loser.status_code == 409
    assert late_btn.detail["code"] == "already_settled"
    assert late_txt.detail["code"] == "already_settled"
    assert late_web.detail["code"] == "already_settled_in_channel"

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT resume_status, admitted_run_id FROM dalton_channel_interrupt_resumes WHERE thread_id = %s::uuid AND run_id = 'run-07' AND interrupt_id = 'int-07'",
                (tid,),
            )
            row = await cur.fetchone()
            assert row["resume_status"] == "admitted"
            assert row["admitted_run_id"] == admitted_rid


@pytest.mark.asyncio
async def test_pg_interleaving_08_rekey_cancelled_before_consume_and_new_claim_text(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ⑧: disconnect rekey (claim_id rotates, inbound_message_id unchanged)
    + new claim-text -> claim_id mismatch retries, returns 409, new inbound stays pending."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    scope = f"dm:{pg_b3_env['feishu_puid']}"
    old_mid = f"{pg_b3_env['prefix']}_m08_old"
    new_mid = f"{pg_b3_env['prefix']}_m08_new"
    _patch_live_checkpoint(monkeypatch, interrupt_id="int-08")
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=old_mid, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id="run-08", interrupt_id="int-08",
    )
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=new_mid, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id="run-08", interrupt_id="int-08",
    )
    c_old = await cat.claim_channel_text_interrupt_resume(
        platform="feishu", message_id=old_mid, dalton_user_id=uid, scope_key=scope, generation=1,
        thread_id=tid, run_id="run-08", interrupt_id="int-08",
        resume_payload={"hitl_response": {"int-08": {"answer": "old"}}},
        gateway_epoch=epoch,
    )
    admitted_rid = str(uuid4())
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO conversation_responses (
                        conversation_response_id, conversation_thread_id, turn_index, status, request_key, created_at
                    )
                    VALUES (%s::uuid, %s::uuid, 1, 'in_progress', %s::uuid, NOW())
                    """,
                    (admitted_rid, tid, c_old["request_key"]),
                )
                await cur.execute(
                    """
                    UPDATE conversation_responses
                    SET status = 'cancelled', metadata = %s
                    WHERE conversation_response_id = %s::uuid
                    """,
                    (Jsonb({"dalton_cancel_source": "graceful_restart"}), admitted_rid),
                )
                await cat.admit_interrupt_resume_in_start_tx(
                    cur, thread_id=tid, run_id="run-08", interrupt_id="int-08",
                    claim_id=c_old["claim_id"], request_key=c_old["request_key"],
                    admitted_run_id=admitted_rid,
                )

    preview_done = asyncio.Event()
    rekey_done = asyncio.Event()
    preview_calls = 0

    async def _hook(op: str) -> None:
        nonlocal preview_calls
        if op == "claim_text":
            preview_calls += 1
            if preview_calls == 1:
                preview_done.set()
                await asyncio.wait_for(rekey_done.wait(), timeout=_SYNC_TIMEOUT)

    cat._after_preview_hook = _hook

    async def _run_new_claim():
        try:
            return await cat.claim_channel_text_interrupt_resume(
                platform="feishu", message_id=new_mid, dalton_user_id=uid, scope_key=scope, generation=1,
                thread_id=tid, run_id="run-08", interrupt_id="int-08",
                resume_payload={"hitl_response": {"int-08": {"answer": "new"}}},
                gateway_epoch=epoch,
            )
        finally:
            preview_done.set()

    async def _run_rekey():
        try:
            await asyncio.wait_for(preview_done.wait(), timeout=_SYNC_TIMEOUT)
            return await cat.ack_channel_interrupt_resume(
                thread_id=tid, run_id="run-08", interrupt_id="int-08",
                claim_id=c_old["claim_id"], outcome="rekey_cancelled_before_consume",
                gateway_epoch=epoch,
            )
        finally:
            rekey_done.set()

    new_res, rekey_res = await asyncio.gather(_run_new_claim(), _run_rekey(), return_exceptions=True)
    assert isinstance(rekey_res, dict) and rekey_res["rekeyed"] is True
    assert preview_calls >= 2, "Expected claim-text to roll back and retry on claim_id drift"
    assert isinstance(new_res, HTTPException) and new_res.status_code == 409
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM dalton_channel_inbound_events WHERE platform = 'feishu' AND message_id = %s",
                (new_mid,),
            )
            assert (await cur.fetchone())["status"] == "pending"


@pytest.mark.asyncio
async def test_pg_interleaving_09_web_takeover_and_new_claim_text(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ⑨: web takeover (claim_id rotates & inbound_message_id -> NULL)
    + new claim-text -> claim_id mismatch retries, returns 409, new inbound stays pending."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    scope = f"dm:{pg_b3_env['feishu_puid']}"
    old_mid = f"{pg_b3_env['prefix']}_m09_old"
    new_mid = f"{pg_b3_env['prefix']}_m09_new"
    _patch_live_checkpoint(monkeypatch, interrupt_id="int-09")
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=old_mid, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id="run-09", interrupt_id="int-09",
    )
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=new_mid, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id="run-09", interrupt_id="int-09",
    )
    await cat.claim_channel_text_interrupt_resume(
        platform="feishu", message_id=old_mid, dalton_user_id=uid, scope_key=scope, generation=1,
        thread_id=tid, run_id="run-09", interrupt_id="int-09",
        resume_payload={"hitl_response": {"int-09": {"answer": "old"}}},
        gateway_epoch=epoch,
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_interrupt_resumes
                SET lease_expires_at = NOW() - INTERVAL '10 seconds'
                WHERE thread_id = %s::uuid AND run_id = 'run-09' AND interrupt_id = 'int-09'
                """,
                (tid,),
            )

    preview_done = asyncio.Event()
    web_done = asyncio.Event()
    preview_calls = 0

    async def _hook(op: str) -> None:
        nonlocal preview_calls
        if op == "claim_text":
            preview_calls += 1
            if preview_calls == 1:
                preview_done.set()
                await asyncio.wait_for(web_done.wait(), timeout=_SYNC_TIMEOUT)

    cat._after_preview_hook = _hook

    async def _run_new_claim():
        try:
            return await cat.claim_channel_text_interrupt_resume(
                platform="feishu", message_id=new_mid, dalton_user_id=uid, scope_key=scope, generation=1,
                thread_id=tid, run_id="run-09", interrupt_id="int-09",
                resume_payload={"hitl_response": {"int-09": {"answer": "new"}}},
                gateway_epoch=epoch,
            )
        finally:
            preview_done.set()

    async def _run_web():
        try:
            await asyncio.wait_for(preview_done.wait(), timeout=_SYNC_TIMEOUT)
            return await cat.claim_web_interrupt_resume(
                thread_id=tid, run_id="run-09", interrupt_id="int-09", user_id=uid,
                resume_payload={"hitl_response": {"int-09": {"answer": "web"}}},
            )
        finally:
            web_done.set()

    new_res, web_res = await asyncio.gather(_run_new_claim(), _run_web(), return_exceptions=True)
    assert isinstance(web_res, dict) and web_res["ok"] is True
    assert preview_calls >= 2, "Expected claim-text to roll back and retry on web takeover"
    assert isinstance(new_res, HTTPException) and new_res.status_code == 409
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM dalton_channel_inbound_events WHERE platform = 'feishu' AND message_id = %s",
                (new_mid,),
            )
            assert (await cur.fetchone())["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["account_preexists", "account_seeded_after_preview"])
async def test_pg_interleaving_10_weixin_ack_claim_text_recovery_with_backfill_weixin_account_and_unbind(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """Interleaving ⑩: two variants for Weixin inbound previewed with ``weixin_account_id = NULL``
    and backfilled before 7a lock:
      (a) ``account_preexists``: user's Weixin account already exists at preview -> preview includes it
          in 3a lock set -> succeeds on first attempt (``preview_attempts == 1``), zero deadlocks.
      (b) ``account_seeded_after_preview``: user has NO account at preview -> ``_after_preview_hook``
          seeds the account and backfills inbound -> 7a inbound check detects unlocked account and
          rolls back BEFORE ``_after_7a_inbound_lock_hook`` / ``thread_watches`` -> retries
          (``preview_attempts >= 2``, ``after_7a_inbound_calls == 1``) and succeeds.
    Both variants then run concurrent Weixin ``ack`` + ``RecoveryScanner`` + real ``unbind_weixin_account``."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    aid: str | None = (
        await _seed_weixin_account(pool, uid=uid, prefix=pg_b3_env["prefix"])
        if variant == "account_preexists"
        else None
    )
    mid = f"{pg_b3_env['prefix']}_wx10_{variant}"
    scope = f"dm:wx_{pg_b3_env['prefix']}@im.wechat"
    run_id = f"run-10-{variant}"
    int_id = f"int-10-{variant}"
    _patch_live_checkpoint(monkeypatch, interrupt_id=int_id)
    await _seed_session_and_inbound(
        pool, platform="weixin", message_id=mid, uid=uid, scope_key=scope,
        chat_address="weixin:@me", tid=tid, run_id=run_id, interrupt_id=int_id,
        weixin_account_id=None, weixin_bot_gen=1,
    )

    preview_attempts = 0
    after_7a_inbound_calls = 0

    async def _backfill_hook(op: str) -> None:
        nonlocal preview_attempts, aid
        if op == "claim_text":
            preview_attempts += 1
            if preview_attempts == 1:
                if aid is None:
                    aid = await _seed_weixin_account(pool, uid=uid, prefix=pg_b3_env["prefix"])
                async with pool.connection() as c2:
                    async with c2.cursor() as cur2:
                        await cur2.execute(
                            """
                            UPDATE dalton_channel_inbound_events
                            SET weixin_account_id = %s::uuid
                            WHERE platform = 'weixin' AND message_id = %s
                            """,
                            (aid, mid),
                        )

    async def _after_7a_hook(op: str) -> None:
        nonlocal after_7a_inbound_calls
        if op == "claim_text":
            after_7a_inbound_calls += 1

    cat._after_preview_hook = _backfill_hook
    cat._after_7a_inbound_lock_hook = _after_7a_hook
    claimed = await cat.claim_channel_text_interrupt_resume(
        platform="weixin", message_id=mid, dalton_user_id=uid, scope_key=scope, generation=1,
        thread_id=tid, run_id=run_id, interrupt_id=int_id,
        resume_payload={"hitl_response": {int_id: {"answer": "1"}}},
        gateway_epoch=epoch,
    )
    cat._after_preview_hook = None
    cat._after_7a_inbound_lock_hook = None
    assert claimed["ok"] is True
    assert aid is not None
    if variant == "account_preexists":
        assert preview_attempts == 1
        assert after_7a_inbound_calls == 1
    else:
        assert preview_attempts >= 2, (
            "Expected rollback & retry when account was absent at preview and backfilled before 7a lock"
        )
        # Attempt 1 rolled back inside _lock_inbound_events_and_verify_weixin_set_in_tx BEFORE
        # _after_7a_inbound_lock_hook (and before locking thread_watches); only attempt 2 passed 7a-1!
        assert after_7a_inbound_calls == 1

    # Phase B: Concurrent Weixin ack + RecoveryScanner + real unbind_weixin_account
    ack_previewed = asyncio.Event()

    async def _ack_hook(op: str) -> None:
        if op == "ack_origin":
            ack_previewed.set()

    cat._after_preview_hook = _ack_hook

    async def _run_ack():
        try:
            return await cat.ack_channel_interrupt_resume(
                thread_id=tid, run_id=run_id, interrupt_id=int_id,
                claim_id=claimed["claim_id"], outcome="retry_scheduled", error="net",
                gateway_epoch=epoch,
            )
        finally:
            ack_previewed.set()

    async def _run_unbind():
        await asyncio.wait_for(ack_previewed.wait(), timeout=_SYNC_TIMEOUT)
        return await wx_db.unbind_weixin_account(
            account_id=aid, operator_user_id=uid, action="member_unbind"
        )

    ack_res, unbind_res, rec_res = await asyncio.gather(
        _run_ack(),
        _run_unbind(),
        cat.reconcile_stale_interrupt_resumes(),
        return_exceptions=True,
    )
    assert isinstance(unbind_res, dict) and unbind_res["ok"] is True
    assert unbind_res["deleted_account"] is True
    assert isinstance(rec_res, dict) and "resumes_reconciled" in rec_res
    if isinstance(ack_res, Exception):
        _assert_not_lock_or_constraint_error(ack_res)
        assert isinstance(ack_res, HTTPException) and ack_res.status_code in (403, 404, 409)
    else:
        assert ack_res["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("i6_format", ["bt2_files", "legacy_links"])
async def test_pg_interleaving_11_cross_worker_7b0_for_share_with_i6_files_and_drain_barrier_repeatable_read(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch, i6_format: str
) -> None:
    """Interleaving ⑪ (both BT2 file-carrying I-6 AND legacy-format ``deliverables-links:<run_id>``):
    1. Contrast Path A button-only toggle (does NOT expire tokens or flip BT2).
    2. Prove via ``pg_stat_activity`` + ``pg_locks`` (``wait_event_type = 'Lock'``) that
       ``enter_interrupt_drain_mode`` (``7b-0 FOR UPDATE``) genuinely WAITS for the in-flight
       I-6 holding ``7b-0 FOR SHARE``.
    3. Verify post-drain I-6 is blocked (``bt2_drain_disabled`` with ZERO rows inserted for
       parent / legacy links) and §5.4 ``REPEATABLE READ`` counts drop to ``(0, 0)`` after drain."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    pfx = pg_b3_env["prefix"]
    scope = f"dm:{pg_b3_env['feishu_puid']}"
    mid = f"{pfx}_m11_{i6_format}"
    run_id = f"run-11-{i6_format}"
    int_id = f"int-11-{i6_format}"
    action_req = {"type": "ask_user_question", "question": "Proceed?"}
    _patch_live_checkpoint(monkeypatch, interrupt_id=int_id, action_req=action_req)
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=mid, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id=run_id, interrupt_id=int_id,
    )

    # 1) Contrast Path A: toggle interactive_actions_enabled=False -> preserves bound token & BT2=True
    minted = await cat.mint_channel_action_token(
        platform="feishu", dalton_user_id=uid, initiator_platform_user_id=pg_b3_env["feishu_puid"],
        origin_chat_type="dm", origin_chat_address="feishu:@me", origin_scope_key=scope,
        session_generation=1, thread_id=tid, run_id=run_id, interrupt_id=int_id,
        interrupt_value={"kind": "ask_user_question", "action_requests": [action_req]},
        gateway_epoch=epoch,
    )
    await cat.bind_channel_action_card(
        approval_nonce=minted["approval_nonce"], phase="prepare_send", gateway_epoch=epoch
    )
    await cat.bind_channel_action_card(
        approval_nonce=minted["approval_nonce"], phase="confirm_bound",
        card_channel_id="oc_dm_11", card_message_id=f"om_card_11_{i6_format}", gateway_epoch=epoch,
    )
    btn_off = await cat.set_interrupt_runtime_config(interactive_actions_enabled=False)
    assert btn_off["interactive_actions_enabled"] is False
    assert btn_off["interrupt_ledger_enabled"] is True
    assert btn_off["bt2_link_recovery_enabled"] is True
    assert btn_off["expired_bound_tokens"] == 0
    await cat.set_interrupt_runtime_config(interactive_actions_enabled=True)

    # 2) Pre-drain I-6 #1 acquires 7b-0 FOR SHARE and pauses WHILE HOLDING IT!
    holding_share = asyncio.Event()
    release_share = asyncio.Event()
    first_i6 = True
    expected_hook_op = "i6_files" if i6_format == "bt2_files" else "i6_links_legacy"

    async def _after_7b0_hook(op: str) -> None:
        nonlocal first_i6
        if op == expected_hook_op and first_i6:
            first_i6 = False
            holding_share.set()
            await asyncio.wait_for(release_share.wait(), timeout=_SYNC_TIMEOUT)

    cat._after_7b0_lock_hook = _after_7b0_hook

    if i6_format == "bt2_files":
        pre_key = f"tool:{run_id}:{pfx}_pre"
        i6_pre_task = asyncio.create_task(
            cg_db.authorize_and_claim_delivery(
                delivery_key=pre_key,
                kind="agent_send",
                user_id=uid,
                thread_id=tid,
                run_id=run_id,
                target="feishu:@me",
                requested_files=[{"workspace_id": pg_b3_env["ws_id"], "path": "pre.pdf"}],
                gateway_epoch=epoch,
                require_gateway_epoch=True,
            )
        )
    else:
        # Legacy-format deliverables-links:<run_id> without any new BT2 fields
        pre_key = f"deliverables-links:{run_id}-{pfx}-pre"
        i6_pre_task = asyncio.create_task(
            cg_db.authorize_and_claim_delivery(
                delivery_key=pre_key,
                kind="fallback",
                user_id=uid,
                thread_id=tid,
                run_id=run_id,
                gateway_epoch=epoch,
                require_gateway_epoch=True,
            )
        )

    try:
        await asyncio.wait_for(holding_share.wait(), timeout=_SYNC_TIMEOUT)
        # Start enter_interrupt_drain_mode (7b-0 FOR UPDATE) while I-6 #1 holds 7b-0 FOR SHARE!
        drain_task = asyncio.create_task(cat.enter_interrupt_drain_mode())
        # Poll pg_stat_activity + pg_locks on a separate connection to PROVE drain is waiting on 7b-0 Lock!
        lock_wait_row = await _wait_for_pg_lock_wait(
            pool, "%dalton_channel_interrupt_runtime_config%"
        )
        assert lock_wait_row["wait_event_type"] == "Lock"
        assert not drain_task.done(), (
            "enter_interrupt_drain_mode MUST block while I-6 holds 7b-0 FOR SHARE"
        )
    finally:
        release_share.set()

    i6_pre, drain_res = await asyncio.wait_for(
        asyncio.gather(i6_pre_task, drain_task), timeout=_SYNC_TIMEOUT
    )
    cat._after_7b0_lock_hook = None

    assert i6_pre["claim_status"] == "claimed"
    assert drain_res["interactive_actions_enabled"] is False
    assert drain_res["interrupt_ledger_enabled"] is False
    assert drain_res["bt2_link_recovery_enabled"] is False
    assert drain_res["expired_bound_tokens"] >= 1

    # 3) Post-drain I-6 #2 blocked by bt2_link_recovery_enabled = FALSE with ZERO new parent rows!
    if i6_format == "bt2_files":
        assert i6_pre["bt2_enrolled"] is True
        post_key = f"tool:{run_id}:{pfx}_post"
        i6_post = await cg_db.authorize_and_claim_delivery(
            delivery_key=post_key,
            kind="agent_send",
            user_id=uid,
            thread_id=tid,
            run_id=run_id,
            target="feishu:@me",
            requested_files=[{"workspace_id": pg_b3_env["ws_id"], "path": "post.pdf"}],
            gateway_epoch=epoch,
            require_gateway_epoch=True,
        )
        assert i6_post["claim_status"] == "claimed"
        assert i6_post["bt2_enrolled"] is False

        denied_parent_key = f"deliverables-links:{post_key}"
        denied_p = await cg_db.authorize_and_claim_delivery(
            delivery_key=denied_parent_key,
            kind="fallback",
            source_delivery_key=post_key,
            gateway_epoch=epoch,
            require_gateway_epoch=True,
        )
        assert denied_p["claim_status"] == "denied"
        assert denied_p["code"] == "bt2_drain_disabled"
    else:
        # Verify pre-drain legacy row stayed legacy (bt2_enrolled=FALSE, claim_attempt_id=NULL, lease=NULL)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT bt2_enrolled, claim_attempt_id, claim_lease_expires_at
                    FROM dalton_channel_deliveries
                    WHERE delivery_key = %s
                    """,
                    (pre_key,),
                )
                pre_row = await cur.fetchone()
                assert pre_row is not None
                assert pre_row["bt2_enrolled"] is False
                assert pre_row["claim_attempt_id"] is None
                assert pre_row["claim_lease_expires_at"] is None

        denied_parent_key = f"deliverables-links:{run_id}-{pfx}-post"
        denied_p = await cg_db.authorize_and_claim_delivery(
            delivery_key=denied_parent_key,
            kind="fallback",
            user_id=uid,
            thread_id=tid,
            run_id=run_id,
            gateway_epoch=epoch,
            require_gateway_epoch=True,
        )
        assert denied_p["claim_status"] == "denied"
        assert denied_p["status"] == "failed"
        assert denied_p["code"] == "bt2_drain_disabled"

    # Verify ZERO rows were inserted for the post-drain denied parent key
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM dalton_channel_deliveries WHERE delivery_key = %s",
                (denied_parent_key,),
            )
            assert await cur.fetchone() is None

    async def _run_repeatable_read_counts() -> tuple[int, int]:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    await cur.execute(
                        """
                        SELECT interactive_actions_enabled,
                               interrupt_ledger_enabled,
                               bt2_link_recovery_enabled
                        FROM dalton_channel_interrupt_runtime_config
                        WHERE singleton_id = 1
                        FOR SHARE
                        """
                    )
                    cfg = await cur.fetchone()
                    assert cfg["interactive_actions_enabled"] is False
                    assert cfg["interrupt_ledger_enabled"] is False
                    assert cfg["bt2_link_recovery_enabled"] is False
                    await cur.execute(
                        """
                        SELECT
                            (
                                SELECT count(*)
                                FROM dalton_channel_interrupt_resumes
                                WHERE thread_id = %s::uuid
                                  AND requires_gateway_ack = TRUE
                                  AND (
                                      resume_status = 'pending_dispatch'
                                      OR (resume_status = 'admitted' AND run_consumed_at IS NULL)
                                      OR channel_ack_at IS NULL
                                  )
                            )
                            +
                            (
                                SELECT count(*)
                                FROM dalton_channel_action_tokens
                                WHERE thread_id = %s::uuid
                                  AND token_role IN ('direct', 'gate')
                                  AND (
                                      bind_status IN ('pending_bind', 'send_in_flight', 'send_unknown')
                                      OR late_patch_status = 'pending'
                                  )
                            ) AS remaining_drain_items,
                            (
                                SELECT count(*)
                                FROM dalton_channel_deliveries src
                                WHERE src.user_id = %s
                                  AND (
                                      (
                                          src.delivery_key LIKE 'deliverables-links:%%'
                                          AND src.delivery_key NOT LIKE 'deliverables-links:%%:item:%%'
                                          AND (
                                              src.status IN ('in_flight', 'partial', 'unknown')
                                              OR (
                                                  src.status = 'failed'
                                                  AND COALESCE(src.error_code, '') NOT IN (
                                                      'not_allowed',
                                                      'max_attempts_exceeded',
                                                      'skipped_platform_disabled'
                                                  )
                                              )
                                          )
                                      )
                                      OR (
                                          src.bt2_enrolled = TRUE
                                          AND src.delivery_key NOT LIKE 'deliverables-links:%%'
                                          AND src.deliverable_manifest IS NOT NULL
                                          AND jsonb_typeof(src.deliverable_manifest) = 'array'
                                          AND jsonb_array_length(src.deliverable_manifest) > 0
                                          AND NOT EXISTS (
                                              SELECT 1
                                              FROM dalton_channel_deliveries p
                                              WHERE p.delivery_key = CASE
                                                  WHEN src.delivery_key LIKE 'deliverables:%%'
                                                  THEN 'deliverables-links:' || substr(src.delivery_key, 14)
                                                  ELSE 'deliverables-links:' || src.delivery_key
                                              END
                                          )
                                          AND (
                                              src.status = 'in_flight'
                                              OR (
                                                  (
                                                      src.status IN ('sent', 'partial', 'unknown')
                                                      OR (
                                                          src.status = 'failed'
                                                          AND COALESCE(src.error_code, '') NOT IN (
                                                              'not_allowed',
                                                              'max_attempts_exceeded',
                                                              'skipped_platform_disabled'
                                                          )
                                                      )
                                                  )
                                                  AND EXISTS (
                                                      SELECT 1
                                                      FROM jsonb_array_elements(src.deliverable_manifest) m
                                                      WHERE NOT EXISTS (
                                                          SELECT 1
                                                          FROM jsonb_array_elements(COALESCE(src.files_status, '[]'::jsonb)) fs
                                                          WHERE COALESCE(fs->>'workspace_id', '') = COALESCE(m->>'workspace_id', '')
                                                            AND fs->>'path' = m->>'path'
                                                            AND (
                                                                fs->>'status' IN ('sent', 'linked')
                                                                OR (
                                                                    fs->>'status' = 'failed'
                                                                    AND COALESCE(fs->>'reason', '') IN (
                                                                        'not_allowed',
                                                                        'max_attempts_exceeded',
                                                                        'skipped_platform_disabled'
                                                                    )
                                                                )
                                                            )
                                                      )
                                                  )
                                              )
                                          )
                                      )
                                  )
                            ) AS pending_link_recoveries
                        """,
                        (tid, tid, uid),
                    )
                    counts = await cur.fetchone()
                    return int(counts["remaining_drain_items"]), int(counts["pending_link_recoveries"])

    # 4) Verify counts are > 0 BEFORE draining
    rem_before, bt2_before = await _run_repeatable_read_counts()
    assert rem_before >= 1, f"Expected remaining_drain_items >= 1 for late_patch token, got {rem_before}"
    assert bt2_before >= 1, f"Expected pending_link_recoveries >= 1 for pre-drain row, got {bt2_before}"

    # 5) Settle the expired late-patch token AND the pre-drain delivery -> counts MUST drop to 0 / 0!
    await cat.ack_channel_interrupt_resume(
        late_card_results=[{"token_hash": minted["token_hash"], "patch_status": "patched"}],
        gateway_epoch=epoch,
    )
    if i6_format == "bt2_files":
        await cg_db.update_delivery_progress(
            delivery_key=pre_key,
            status="sent",
            delivery_attempt=i6_pre["delivery_attempt"],
            claim_attempt_id=i6_pre["claim_attempt_id"],
            files_status=[{"workspace_id": pg_b3_env["ws_id"], "path": "pre.pdf", "status": "sent"}],
            gateway_epoch=epoch,
            require_gateway_epoch=True,
        )
    else:
        await cg_db.update_delivery_progress(
            delivery_key=pre_key,
            status="sent",
            delivery_attempt=i6_pre["delivery_attempt"],
            gateway_epoch=epoch,
            require_gateway_epoch=True,
        )

    rem_after, bt2_after = await _run_repeatable_read_counts()
    assert rem_after == 0, f"Expected remaining_drain_items == 0 after drain, got {rem_after}"
    assert bt2_after == 0, f"Expected pending_link_recoveries == 0 after drain, got {bt2_after}"
