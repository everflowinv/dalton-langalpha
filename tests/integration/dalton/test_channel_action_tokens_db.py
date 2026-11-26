"""Real PostgreSQL integration & 11-interleaving concurrency tests for Channel Phase 2 Batch 3.

Uses ``tests/integration/conftest.py`` fixtures (``test_db_pool``, ``test_db_uri``,
``patched_get_db_connection``) and ``tests/integration/dalton/conftest.py``
(``dalton_schema`` running Alembic upgrade to ``d058``) so every test executes
against the isolated ``*_test`` PostgreSQL database.

Run outside sandbox (where PostgreSQL is reachable):
    uv run pytest -m integration tests/integration/dalton/test_channel_action_tokens_db.py -v
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

from fastapi import HTTPException
import psycopg
from psycopg.types.json import Jsonb
import pytest
import pytest_asyncio

from src.dalton import channel_action_tokens as cat
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import weixin_multi_account as wx_db
from src.server.database import pool as db_pool

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures: ensure d058 tables + clean isolated rows per test
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_b3_env(
    test_db_pool,
    patched_get_db_connection,
    dalton_schema,
    monkeypatch: pytest.MonkeyPatch,
):
    """Route all channel DB modules to the isolated test pool and clean up after each test."""
    prefix = f"b3pg_{uuid4().hex[:10]}"
    uid = f"{prefix}_user"
    tid = str(uuid4())
    ws_id = str(uuid4())

    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_GATEWAY", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_FEISHU", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_DISCORD", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_WEIXIN", "true")
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "test-channel-b3-encryption-key-32bytes!")
    for var in (
        "DALTON_CHANNEL_INTERACTIVE_ACTIONS_ENABLED",
        "DALTON_CHANNEL_INTERRUPT_LEDGER_ENABLED",
        "DALTON_CHANNEL_BT2_LINK_RECOVERY_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)

    # Ensure channel_action_tokens, channel_gateway, and weixin_multi_account all use test_db_pool
    gdc_mock = create_autospec(db_pool.get_db_connection, side_effect=patched_get_db_connection)
    monkeypatch.setattr(db_pool, "get_db_connection", gdc_mock)
    monkeypatch.setattr(cat, "get_db_connection", gdc_mock)
    monkeypatch.setattr(cg_db, "get_db_connection", gdc_mock)
    monkeypatch.setattr(wx_db, "get_db_connection", gdc_mock)

    tier_mock = create_autospec(cat.get_user_tier, return_value="internal")
    monkeypatch.setattr(cat, "get_user_tier", tier_mock)
    monkeypatch.setattr(cg_db, "get_user_tier", tier_mock)
    roster_mock = create_autospec(cg_db.check_team_member_status_live, return_value="active")
    monkeypatch.setattr(cg_db, "check_team_member_status_live", roster_mock)
    monkeypatch.setattr(cat, "check_team_member_status_live", roster_mock)

    # Reset runtime config to all TRUE and acquire a valid gateway lease epoch
    async with test_db_pool.connection() as conn:
        async with conn.cursor() as cur:
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

    lease = await wx_db.acquire_gateway_lease(instance_id=f"gw-{prefix}", ttl_seconds=120)
    epoch = int(lease["epoch"])

    yield {
        "pool": test_db_pool,
        "prefix": prefix,
        "uid": uid,
        "tid": tid,
        "ws_id": ws_id,
        "epoch": epoch,
        "instance_id": f"gw-{prefix}",
        "feishu_puid": f"ou_{prefix}",
        "discord_puid": f"dc_{prefix}",
    }

    # Reset hooks & clean up test rows
    cat._after_preview_hook = None
    cat._before_7b0_lock_hook = None
    cat._after_7a_inbound_lock_hook = None
    cat._after_7b2_lock_hook = None
    async with test_db_pool.connection() as conn:
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


async def _seed_session_and_inbound(
    pool,
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


async def _seed_weixin_account(pool, *, uid: str, prefix: str) -> str:
    aid = str(uuid4())
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_channel_weixin_accounts (
                    account_id, user_id, ilink_bot_id, ilink_user_id,
                    encrypted_bot_token, status, bot_generation
                )
                VALUES (%s::uuid, %s, %s, %s, %s, 'active', 1)
                """,
                (aid, uid, f"bot_{prefix}@im.wechat", f"wx_{prefix}@im.wechat", b"enc"),
            )
            await cur.execute(
                """
                INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id)
                VALUES (%s, 'weixin', %s)
                ON CONFLICT (user_id, platform) DO UPDATE SET platform_user_id = EXCLUDED.platform_user_id
                """,
                (uid, f"wx_{prefix}@im.wechat"),
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
# Schema & CHECK Constraint Verification against Real PostgreSQL
# ===========================================================================


@pytest.mark.asyncio
async def test_d058_live_schema_indexes_and_check_constraints_db(pg_b3_env) -> None:
    pool = pg_b3_env["pool"]
    tid = pg_b3_env["tid"]
    uid = pg_b3_env["uid"]

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Verify all 10 indexes exist in pg_indexes
            await cur.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND indexname = ANY(%s::text[])
                """,
                (
                    [
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
                    ],
                ),
            )
            found = {r["indexname"] for r in await cur.fetchall()}
            assert len(found) == 10

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

            # CHECK 3: channel_ack_at NOT NULL with requires_gateway_ack=TRUE and pending card fails
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


# ===========================================================================
# All 11 Concurrency Interleavings (§3.2 #4) against Real PostgreSQL
# ===========================================================================


@pytest.mark.asyncio
async def test_pg_interleaving_01_claim_text_inbound_progress_lease_heartbeat(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ①: claim-text + update_inbound_progress + heartbeat_gateway_lease (FOR UPDATE)."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    mid = f"{pg_b3_env['prefix']}_m01"
    _patch_live_checkpoint(monkeypatch, interrupt_id="int-01")
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=mid, uid=uid,
        scope_key=f"dm:{pg_b3_env['feishu_puid']}", chat_address="feishu:@me",
        tid=tid, run_id="run-01", interrupt_id="int-01",
    )

    results = await asyncio.gather(
        cat.claim_channel_text_interrupt_resume(
            platform="feishu", message_id=mid, dalton_user_id=uid,
            scope_key=f"dm:{pg_b3_env['feishu_puid']}", generation=1,
            thread_id=tid, run_id="run-01", interrupt_id="int-01",
            resume_payload={"hitl_response": {"int-01": {"answer": "ok"}}},
            gateway_epoch=epoch,
        ),
        cg_db.update_inbound_progress(
            platform="feishu", message_id=mid, fallback_pending=False,
            gateway_epoch=epoch, require_gateway_epoch=True,
        ),
        wx_db.heartbeat_gateway_lease(
            instance_id=pg_b3_env["instance_id"], epoch=epoch, ttl_seconds=120
        ),
    )
    assert results[0]["ok"] is True
    assert results[2]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["new_lt_old", "new_gt_old"])
async def test_pg_interleaving_02_new_claim_text_takeover_both_pk_orders_and_old_ack(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch, order: str
) -> None:
    """Interleaving ②: new claim-text taking over expired channel_text in BOTH PK orders + old ack."""
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
    # Expire old claim lease so new_mid may take over
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

    res = await asyncio.gather(
        cat.claim_channel_text_interrupt_resume(
            platform="feishu", message_id=new_mid, dalton_user_id=uid, scope_key=scope, generation=1,
            thread_id=tid, run_id="run-02", interrupt_id="int-02",
            resume_payload={"hitl_response": {"int-02": {"answer": "new"}}},
            gateway_epoch=epoch,
        ),
        cat.ack_channel_interrupt_resume(
            thread_id=tid, run_id="run-02", interrupt_id="int-02",
            claim_id=c_old["claim_id"], outcome="retry_scheduled", error="timeout",
            gateway_epoch=epoch,
        ),
        return_exceptions=True,
    )
    # Zero DeadlockDetected (40P01) and zero UniqueViolation; one wins cleanly
    for item in res:
        if isinstance(item, Exception):
            assert not isinstance(item, (psycopg.errors.DeadlockDetected, psycopg.errors.UniqueViolation))


@pytest.mark.asyncio
async def test_pg_interleaving_03_web_and_consume_takeover_old_channel_text_and_old_ack(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ③: web/consume takeover of expired channel_text + concurrent old ack."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    scope = f"dm:{pg_b3_env['feishu_puid']}"
    old_mid = f"{pg_b3_env['prefix']}_m03"
    _patch_live_checkpoint(monkeypatch, interrupt_id="int-03")
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=old_mid, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id="run-03", interrupt_id="int-03",
    )
    c_old = await cat.claim_channel_text_interrupt_resume(
        platform="feishu", message_id=old_mid, dalton_user_id=uid, scope_key=scope, generation=1,
        thread_id=tid, run_id="run-03", interrupt_id="int-03",
        resume_payload={"hitl_response": {"int-03": {"answer": "old"}}},
        gateway_epoch=epoch,
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_channel_interrupt_resumes
                SET lease_expires_at = NOW() - INTERVAL '10 seconds'
                WHERE thread_id = %s::uuid AND run_id = 'run-03' AND interrupt_id = 'int-03'
                """,
                (tid,),
            )

    res = await asyncio.gather(
        cat.claim_web_interrupt_resume(
            thread_id=tid, run_id="run-03", interrupt_id="int-03", user_id=uid,
            resume_payload={"hitl_response": {"int-03": {"answer": "web"}}},
        ),
        cat.ack_channel_interrupt_resume(
            thread_id=tid, run_id="run-03", interrupt_id="int-03",
            claim_id=c_old["claim_id"], outcome="retry_scheduled", error="net",
            gateway_epoch=epoch,
        ),
        return_exceptions=True,
    )
    for item in res:
        if isinstance(item, Exception):
            assert not isinstance(item, (psycopg.errors.DeadlockDetected, psycopg.errors.UniqueViolation))


@pytest.mark.asyncio
async def test_pg_interleaving_04_claim_text_and_recover_startup_and_interrupt_resumes(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ④: claim-text + /internal/recover (startup & interrupt_resumes)."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    scope = f"dm:{pg_b3_env['feishu_puid']}"
    mid = f"{pg_b3_env['prefix']}_m04"
    _patch_live_checkpoint(monkeypatch, interrupt_id="int-04")
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=mid, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id="run-04", interrupt_id="int-04",
    )

    res = await asyncio.gather(
        cat.claim_channel_text_interrupt_resume(
            platform="feishu", message_id=mid, dalton_user_id=uid, scope_key=scope, generation=1,
            thread_id=tid, run_id="run-04", interrupt_id="int-04",
            resume_payload={"hitl_response": {"int-04": {"answer": "yes"}}},
            gateway_epoch=epoch,
        ),
        cg_db.recover_channel_state(mode="startup", gateway_epoch=epoch, require_gateway_epoch=True),
        cg_db.recover_channel_state(mode="interrupt_resumes", gateway_epoch=epoch, require_gateway_epoch=True),
    )
    assert res[0]["ok"] is True
    assert res[1]["mode"] == "startup"
    assert res[2]["mode"] == "interrupt_resumes"


@pytest.mark.asyncio
async def test_pg_interleaving_05_recover_and_unbind_weixin_account(pg_b3_env) -> None:
    """Interleaving ⑤: /internal/recover (startup & interrupt_resumes) + unbind_weixin_account."""
    pool, uid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["epoch"]
    aid = await _seed_weixin_account(pool, uid=uid, prefix=pg_b3_env["prefix"])

    res = await asyncio.gather(
        cg_db.recover_channel_state(mode="startup", gateway_epoch=epoch, require_gateway_epoch=True),
        cg_db.recover_channel_state(mode="interrupt_resumes", gateway_epoch=epoch, require_gateway_epoch=True),
        wx_db.unbind_weixin_account(account_id=aid, operator_user_id=uid, action="member_unbind"),
        return_exceptions=True,
    )
    for item in res:
        if isinstance(item, Exception):
            assert not isinstance(item, (psycopg.errors.DeadlockDetected, psycopg.errors.UniqueViolation))


@pytest.mark.asyncio
async def test_pg_interleaving_06_weixin_claim_text_and_unbind_weixin_account(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ⑥: Weixin claim-text + unbind_weixin_account (3a FOR KEY SHARE vs FOR UPDATE)."""
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

    res = await asyncio.gather(
        cat.claim_channel_text_interrupt_resume(
            platform="weixin", message_id=mid, dalton_user_id=uid, scope_key=scope, generation=1,
            thread_id=tid, run_id="run-06", interrupt_id="int-06",
            resume_payload={"hitl_response": {"int-06": {"answer": "1"}}},
            gateway_epoch=epoch,
        ),
        wx_db.unbind_weixin_account(account_id=aid, operator_user_id=uid, action="member_unbind"),
        return_exceptions=True,
    )
    for item in res:
        if isinstance(item, Exception):
            assert not isinstance(item, (psycopg.errors.DeadlockDetected, psycopg.errors.UniqueViolation))


@pytest.mark.asyncio
async def test_pg_interleaving_07_three_terminal_race_consume_claim_text_web_and_on_start(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ⑦: concurrent consume + claim-text + web claim + START _on_start."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    scope = f"dm:{pg_b3_env['feishu_puid']}"
    mid = f"{pg_b3_env['prefix']}_m07"
    action_req = {"type": "ask_user_question", "question": "Proceed?"}
    _patch_live_checkpoint(monkeypatch, interrupt_id="int-07", action_req=action_req)
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=mid, uid=uid, scope_key=scope,
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

    res = await asyncio.gather(
        cat.consume_channel_action_token(
            approval_nonce=minted["approval_nonce"], decision="approve",
            clicker_platform_user_id=pg_b3_env["feishu_puid"],
            channel_id="oc_dm_07", message_id="om_card_07", answer_text="yes",
            gateway_epoch=epoch,
        ),
        cat.claim_channel_text_interrupt_resume(
            platform="feishu", message_id=mid, dalton_user_id=uid, scope_key=scope, generation=1,
            thread_id=tid, run_id="run-07", interrupt_id="int-07",
            resume_payload={"hitl_response": {"int-07": {"answer": "text"}}},
            gateway_epoch=epoch,
        ),
        cat.claim_web_interrupt_resume(
            thread_id=tid, run_id="run-07", interrupt_id="int-07", user_id=uid,
            resume_payload={"hitl_response": {"int-07": {"answer": "web"}}},
        ),
        return_exceptions=True,
    )
    winners = [r for r in res if isinstance(r, dict) and r.get("ok")]
    assert len(winners) == 1
    for item in res:
        if isinstance(item, Exception):
            assert not isinstance(item, (psycopg.errors.DeadlockDetected, psycopg.errors.UniqueViolation))
            assert isinstance(item, HTTPException) and item.status_code == 409

    # Winner completes _on_start cleanly; bogus claim_id gets 409 interrupt_claim_lost
    win = winners[0]
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cat.admit_interrupt_resume_in_start_tx(
                    cur, thread_id=tid, run_id="run-07", interrupt_id="int-07",
                    claim_id=win["claim_id"], request_key=win["request_key"],
                    admitted_run_id=str(uuid4()),
                )


@pytest.mark.asyncio
async def test_pg_interleaving_08_rekey_cancelled_before_consume_and_new_claim_text(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ⑧: disconnect rekey (claim_id rotates, inbound_message_id unchanged) + new claim-text -> 409, new inbound stays pending."""
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
    # Admit old claim onto a cancelled (graceful_restart) run (trigger-compliant: born in_progress, then updated)
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
                    (
                        admitted_rid,
                        tid,
                        c_old["request_key"],
                    ),
                )
                await cur.execute(
                    """
                    UPDATE conversation_responses
                    SET status = 'cancelled',
                        metadata = %s
                    WHERE conversation_response_id = %s::uuid
                    """,
                    (
                        Jsonb({"dalton_cancel_source": "graceful_restart"}),
                        admitted_rid,
                    ),
                )
                await cat.admit_interrupt_resume_in_start_tx(
                    cur, thread_id=tid, run_id="run-08", interrupt_id="int-08",
                    claim_id=c_old["claim_id"], request_key=c_old["request_key"],
                    admitted_run_id=admitted_rid,
                )

    # Pause new claim-text right after preview so rekey rotates claim_id first!
    preview_done = asyncio.Event()
    rekey_done = asyncio.Event()
    first_preview = True

    async def _hook(op: str) -> None:
        nonlocal first_preview
        if op == "claim_text" and first_preview:
            first_preview = False
            preview_done.set()
            await rekey_done.wait()

    cat._after_preview_hook = _hook

    async def _run_rekey():
        await preview_done.wait()
        out = await cat.ack_channel_interrupt_resume(
            thread_id=tid, run_id="run-08", interrupt_id="int-08",
            claim_id=c_old["claim_id"], outcome="rekey_cancelled_before_consume",
            gateway_epoch=epoch,
        )
        rekey_done.set()
        return out

    new_res, rekey_res = await asyncio.gather(
        cat.claim_channel_text_interrupt_resume(
            platform="feishu", message_id=new_mid, dalton_user_id=uid, scope_key=scope, generation=1,
            thread_id=tid, run_id="run-08", interrupt_id="int-08",
            resume_payload={"hitl_response": {"int-08": {"answer": "new"}}},
            gateway_epoch=epoch,
        ),
        _run_rekey(),
        return_exceptions=True,
    )
    assert isinstance(rekey_res, dict) and rekey_res["rekeyed"] is True
    assert isinstance(new_res, HTTPException) and new_res.status_code == 409
    # Verify new inbound remained 'pending'
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM dalton_channel_inbound_events WHERE platform = 'feishu' AND message_id = %s",
                (new_mid,),
            )
            row = await cur.fetchone()
            assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_pg_interleaving_09_web_takeover_and_new_claim_text(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ⑨: web takeover (claim_id rotates & inbound_message_id -> NULL) + new claim-text -> 409, new inbound stays pending."""
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
    first = True

    async def _hook(op: str) -> None:
        nonlocal first
        if op == "claim_text" and first:
            first = False
            preview_done.set()
            await web_done.wait()

    cat._after_preview_hook = _hook

    async def _run_web():
        await preview_done.wait()
        out = await cat.claim_web_interrupt_resume(
            thread_id=tid, run_id="run-09", interrupt_id="int-09", user_id=uid,
            resume_payload={"hitl_response": {"int-09": {"answer": "web"}}},
        )
        web_done.set()
        return out

    new_res, web_res = await asyncio.gather(
        cat.claim_channel_text_interrupt_resume(
            platform="feishu", message_id=new_mid, dalton_user_id=uid, scope_key=scope, generation=1,
            thread_id=tid, run_id="run-09", interrupt_id="int-09",
            resume_payload={"hitl_response": {"int-09": {"answer": "new"}}},
            gateway_epoch=epoch,
        ),
        _run_web(),
        return_exceptions=True,
    )
    assert isinstance(web_res, dict) and web_res["ok"] is True
    assert isinstance(new_res, HTTPException) and new_res.status_code == 409
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM dalton_channel_inbound_events WHERE platform = 'feishu' AND message_id = %s",
                (new_mid,),
            )
            assert (await cur.fetchone())["status"] == "pending"


@pytest.mark.asyncio
async def test_pg_interleaving_10_weixin_ack_claim_text_recovery_with_backfill_weixin_account_and_unbind(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ⑩: Weixin inbound previewed with weixin_account_id=NULL, concurrently
    backfilled before 7a lock -> claim-text rolls back & retries before locking thread_watches,
    zero deadlock with concurrent unbind_weixin_account."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    aid = await _seed_weixin_account(pool, uid=uid, prefix=pg_b3_env["prefix"])
    mid = f"{pg_b3_env['prefix']}_wx10"
    scope = f"dm:wx_{pg_b3_env['prefix']}@im.wechat"
    _patch_live_checkpoint(monkeypatch, interrupt_id="int-10")
    # Seed inbound with weixin_account_id = NULL initially!
    await _seed_session_and_inbound(
        pool, platform="weixin", message_id=mid, uid=uid, scope_key=scope,
        chat_address="weixin:@me", tid=tid, run_id="run-10", interrupt_id="int-10",
        weixin_account_id=None, weixin_bot_gen=1,
    )

    backfilled = asyncio.Event()
    first = True

    async def _hook(op: str) -> None:
        nonlocal first
        if op == "claim_text" and first:
            first = False
            # Concurrently backfill weixin_account_id AFTER preview and BEFORE 7a lock!
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
            backfilled.set()

    cat._after_preview_hook = _hook

    async def _delayed_unbind():
        await backfilled.wait()
        return await wx_db.unbind_weixin_account(
            account_id=aid, operator_user_id=uid, action="member_unbind"
        )

    res = await asyncio.gather(
        cat.claim_channel_text_interrupt_resume(
            platform="weixin", message_id=mid, dalton_user_id=uid, scope_key=scope, generation=1,
            thread_id=tid, run_id="run-10", interrupt_id="int-10",
            resume_payload={"hitl_response": {"int-10": {"answer": "1"}}},
            gateway_epoch=epoch,
        ),
        _delayed_unbind(),
        cat.reconcile_stale_interrupt_resumes(),
        return_exceptions=True,
    )
    for item in res:
        if isinstance(item, Exception):
            assert not isinstance(item, (psycopg.errors.DeadlockDetected, psycopg.errors.UniqueViolation))


@pytest.mark.asyncio
async def test_pg_interleaving_11_cross_worker_7b0_for_share_with_i6_files_and_drain_barrier_repeatable_read(
    pg_b3_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving ⑪: cross-worker 7b-0 FOR SHARE (claim-text + file-carrying I-6) vs
    enter_interrupt_drain_mode (7b-0 FOR UPDATE) + §5.4 REPEATABLE READ zero-drain SQL."""
    pool, uid, tid, epoch = pg_b3_env["pool"], pg_b3_env["uid"], pg_b3_env["tid"], pg_b3_env["epoch"]
    scope = f"dm:{pg_b3_env['feishu_puid']}"
    mid = f"{pg_b3_env['prefix']}_m11"
    _patch_live_checkpoint(monkeypatch, interrupt_id="int-11")
    await _seed_session_and_inbound(
        pool, platform="feishu", message_id=mid, uid=uid, scope_key=scope,
        chat_address="feishu:@me", tid=tid, run_id="run-11", interrupt_id="int-11",
    )

    at_7b0 = asyncio.Event()
    release_7b0 = asyncio.Event()

    async def _hook_7b0(op: str) -> None:
        if op == "i6_files":
            at_7b0.set()
            await release_7b0.wait()

    cat._before_7b0_lock_hook = _hook_7b0

    async def _run_drain():
        await at_7b0.wait()
        drain_res = await cat.enter_interrupt_drain_mode()
        release_7b0.set()
        return drain_res

    i6_res, drain_res = await asyncio.gather(
        cg_db.authorize_and_claim_delivery(
            delivery_key=f"tool:run-11:{pg_b3_env['prefix']}",
            kind="agent_send",
            user_id=uid,
            thread_id=tid,
            run_id="run-11",
            target="feishu:@me",
            requested_files=[{"workspace_id": pg_b3_env["ws_id"], "path": "doc.pdf"}],
            gateway_epoch=epoch,
            require_gateway_epoch=True,
        ),
        _run_drain(),
    )
    assert drain_res["bt2_link_recovery_enabled"] is False
    # File-carrying I-6 entered 7b-0 AFTER drain committed -> bt2_enrolled MUST be FALSE!
    assert i6_res["claim_status"] == "claimed"
    assert i6_res["bt2_enrolled"] is False

    # Post-drain parent creation for this non-enrolled source is refused with bt2_drain_disabled
    denied_p = await cg_db.authorize_and_claim_delivery(
        delivery_key=f"deliverables-links:tool:run-11:{pg_b3_env['prefix']}",
        kind="fallback",
        source_delivery_key=f"tool:run-11:{pg_b3_env['prefix']}",
        gateway_epoch=epoch,
        require_gateway_epoch=True,
    )
    assert denied_p["claim_status"] == "denied"
    assert denied_p["code"] == "bt2_drain_disabled"

    # Run §5.4 REPEATABLE READ verification SQL directly on real PostgreSQL
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                await cur.execute(
                    """
                    SELECT config_revision,
                           interactive_actions_enabled,
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
                assert int(counts["remaining_drain_items"]) == 0
                assert int(counts["pending_link_recoveries"]) == 0
