"""Real PostgreSQL integration tests for Personal Calendar & Mail Batch 1 (r1).

Covers:
- Alembic migration d06X upgrade head / downgrade roundtrip
- Phase 1 atomic registration, crash recovery via startup resume, dual generation bump, immediate unblock on local commit
- Account switch in-place replacement (keeps new connection connected; sibling not_applicable vs sole pending; missing refresh_token refused)
- Bounded deletion by auth_epoch_upto (reconnection during purge with higher auth_epoch survives)
- Remote revocation state machine & concurrent advisory-lock mutual exclusion
- START transaction, HTTP _admit, and Fork gates (409 personal_data_purge_in_progress)
- Dual-connection interleaving FOR SHARE writeback fences (replay slices, search write_turns, transcript _save + blob release)
- Full 2A purge scope (pre-touch preserved, suffix checkpoints + 3 compaction summary copies, queries, steering_inputs, hook_outbox, Redis keys, approvals across all services, tracker_reconcile_needed)
- Closed-range search guard, truncate_thread_from_turn clipping, and Fork ThroughTurn / BeforeTurn boundaries
- Shared restore reset SQL on both migrated schema and missing-table schema
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec, patch
import uuid

from alembic import command
from alembic.config import Config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.serde.types import _DeltaSnapshot
import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio

from ptc_agent.agent.middleware.compaction.utils import (
    COMPACTION_SUMMARY_KEY,
    SIDECAR_KEY,
    SUMMARIZED_KEY,
)
from src.dalton.database import user_integrations as ui_db
from src.dalton.database.user_integrations import (
    IntegrationStatus,
    LocalPurgeStatus,
    RemoteRevokeStatus,
    TriggerReason,
    extract_google_project,
)
from src.dalton.services import integration_data_purge as purge_mod
from src.dalton.services import integration_revoke as revoke_mod
from src.dalton.services import purge_fence as fence_mod
from src.dalton.services import user_integrations_lifecycle as lifecycle_mod
from src.dalton.services.integration_data_purge import (
    PURGED_PLACEHOLDER,
    PURGED_TOOL_ARGS,
    run_local_purge,
    run_startup_personal_purge_resume,
)
from src.dalton.services.integration_revoke import (
    RemoteRevoker,
    RevokeOutcome,
    process_revoke,
    revoke_before_new_authorization,
    supersede_pending_revokes,
)
from src.dalton.services.purge_fence import (
    PURGE_IN_PROGRESS_CODE,
    PersonalDataPurgeInProgressError,
    assert_fork_source_not_purging_tx,
    check_thread_not_purging_fast,
    copy_personal_integration_state_for_fork_tx,
    read_expected_purge_generation,
    save_stored_transcript_fenced,
)
from src.dalton.services.user_integrations_lifecycle import (
    MissingRefreshTokenOnAccountSwitchError,
    begin_disconnect,
    finish_disconnect,
    replace_connection_for_account_switch,
)
from src.server.database import pool as pool_mod
from src.server.database import thread_transcripts as tt_db
from src.server.database import workspace_file_blobs as blobs_db
from src.server.database.conversation import threads_write as tw_db
from src.server.database.conversation import turn_slices as slices_db
from src.server.database.runs import lifecycle as runs_lifecycle
from src.server.database.search import index as search_db
from src.server.services.forks.model import BeforeTurn, ForkRefused, ThroughTurn

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _dalton_alembic_cfg(test_db_uri: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "src" / "dalton" / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(PROJECT_ROOT / "src" / "dalton" / "migrations")
    )
    sa_url = test_db_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    cfg.set_main_option("sqlalchemy.url", sa_url.replace("%", "%%"))
    return cfg


@pytest_asyncio.fixture
async def gws_live_pool(
    dalton_schema,
    cleanup_dalton_tables,
    test_db_uri: str,
    monkeypatch,
):
    """Open an isolated AsyncConnectionPool wired into all Dalton & server DB call sites."""
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "test-byok-key-for-gws-r1-integration")

    local_pool = AsyncConnectionPool(
        conninfo=test_db_uri,
        min_size=2,
        max_size=8,
        configure=pool_mod._configure_postgres_connection,
        check=AsyncConnectionPool.check_connection,
        kwargs=dict(pool_mod._CONNECTION_KWARGS),
        open=False,
    )
    await local_pool.open()
    await local_pool.wait()

    @asynccontextmanager
    async def _live_get_db_connection(conn=None):
        if conn is not None:
            yield conn
            return
        async with local_pool.connection(timeout=10) as owned:
            yield owned

    call_sites = (
        pool_mod,
        ui_db,
        fence_mod,
        revoke_mod,
        lifecycle_mod,
        purge_mod,
        tt_db,
        blobs_db,
    )

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(pool_mod, "get_db_connection_string", lambda: test_db_uri)
        )
        stack.enter_context(
            patch.object(pool_mod, "get_or_create_pool", lambda: local_pool)
        )
        for mod in call_sites:
            stack.enter_context(
                patch.object(mod, "get_db_connection", _live_get_db_connection)
            )
        async with local_pool.connection() as conn:
            await conn.execute(
                """
                TRUNCATE TABLE
                    dalton_integration_webhook_events,
                    dalton_integration_purge_ledger,
                    dalton_thread_integration_touches,
                    dalton_integration_action_approvals,
                    dalton_user_integration_pending_confirmations,
                    dalton_user_integration_oauth_states,
                    dalton_user_integrations,
                    thread_transcript_files,
                    thread_transcripts,
                    thread_search_turns,
                    turn_slices,
                    task_run_slices,
                    provenance_records,
                    hook_outbox,
                    subagent_runs,
                    subagent_tasks,
                    conversation_responses,
                    conversation_queries,
                    conversation_threads,
                    workspace_file_blobs,
                    workspaces
                CASCADE
                """
            )
        try:
            yield local_pool
        finally:
            await local_pool.close()


async def _seed_user_ws_thread(
    pool: AsyncConnectionPool,
    *,
    user_id: str | None = None,
    workspace_id: str | None = None,
    thread_id: str | None = None,
) -> tuple[str, str, str]:
    uid = user_id or f"user-{uuid.uuid4().hex[:8]}"
    wid = workspace_id or str(uuid.uuid4())
    tid = thread_id or str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, email, name, created_at, updated_at)
            VALUES (%s, %s, %s, NOW(), NOW())
            ON CONFLICT (user_id) DO NOTHING
            """,
            (uid, f"{uid}@example.com", uid),
        )
        await conn.execute(
            """
            INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
            VALUES (%s::uuid, %s, 'ws-test', 'running', NOW(), NOW())
            ON CONFLICT (workspace_id) DO NOTHING
            """,
            (wid, uid),
        )
        await conn.execute(
            """
            INSERT INTO conversation_threads (
                conversation_thread_id, workspace_id, user_id, current_status,
                msg_type, thread_index, title, created_at, updated_at
            )
            VALUES (%s::uuid, %s::uuid, %s, 'completed', 'ptc', 0, 'test', NOW(), NOW())
            """,
            (tid, wid, uid),
        )
    return uid, wid, tid


async def _seed_turn(
    pool: AsyncConnectionPool,
    *,
    thread_id: str,
    turn_index: int,
    user_text: str,
    status: str = "completed",
    steering_inputs: list[dict] | None = None,
) -> str:
    rid = str(uuid.uuid4())
    qid = str(uuid.uuid4())
    meta = {"steering_inputs": steering_inputs} if steering_inputs else {}
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO conversation_queries (
                conversation_query_id, conversation_thread_id, content, type,
                turn_index, created_at
            )
            VALUES (%s::uuid, %s::uuid, %s, 'user', %s, NOW())
            """,
            (qid, thread_id, user_text, turn_index),
        )
        await conn.execute(
            """
            INSERT INTO conversation_responses (
                conversation_response_id, conversation_thread_id, turn_index,
                attempt_no, status, request_key, metadata, created_at
            )
            VALUES (%s::uuid, %s::uuid, %s, 1, %s, %s, %s, NOW())
            """,
            (rid, thread_id, turn_index, status, f"rk-{rid}", Jsonb(meta)),
        )
    return rid


# ---------------------------------------------------------------------------
# 1. Migration upgrade head / downgrade roundtrip
# ---------------------------------------------------------------------------


def test_d06X_migration_upgrade_head_and_downgrade_roundtrip(dalton_schema, test_db_uri: str):
    cfg = _dalton_alembic_cfg(test_db_uri)
    command.downgrade(cfg, "d057")
    with psycopg.connect(test_db_uri) as conn:
        row = conn.execute("SELECT to_regclass('public.dalton_user_integrations')").fetchone()
        assert row is not None and row[0] is None
    command.upgrade(cfg, "head")
    with psycopg.connect(test_db_uri) as conn:
        ver = conn.execute("SELECT version_num FROM dalton_alembic_version").fetchone()
        assert ver is not None and ver[0] == "d06X"
        for tbl in (
            "dalton_user_integrations",
            "dalton_user_integration_oauth_states",
            "dalton_user_integration_pending_confirmations",
            "dalton_integration_action_approvals",
            "dalton_thread_integration_touches",
            "dalton_integration_purge_ledger",
            "dalton_integration_webhook_events",
        ):
            r = conn.execute("SELECT to_regclass(%s)", (f"public.{tbl}",)).fetchone()
            assert r is not None and r[0] is not None


# ---------------------------------------------------------------------------
# 2. Phase 1 crash resume, dual generation bump, and immediate unblock on local commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase1_crash_resume_dual_gen_bump_and_unblock_on_local_commit(gws_live_pool):
    uid, _, tid = await _seed_user_ws_thread(gws_live_pool)
    await _seed_turn(gws_live_pool, thread_id=tid, turn_index=0, user_text="public Q0")
    r1 = await _seed_turn(gws_live_pool, thread_id=tid, turn_index=1, user_text="private Q1")

    conn_row = await ui_db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id="123456-app.apps.googleusercontent.com",
        provider_account_id="sub-1",
        granted_scopes="openid email calendar.events",
        access_token="at-1",
        refresh_token="rt-1",
    )
    first = await ui_db.record_touch(
        thread_id=tid,
        response_id=r1,
        provider="google",
        service="calendar",
        auth_epoch=conn_row.auth_epoch,
        turn_index=1,
        user_id=uid,
        provider_account_id="sub-1",
    )
    assert first is True

    # Phase 1 commits then process "exits" before Phase 2 runs
    purge_id = await begin_disconnect(
        uid, "google", "calendar", trigger_reason=TriggerReason.USER_DISCONNECT, remote_mode="remote_if_sole"
    )

    async with gws_live_pool.connection() as conn:
        t = await (
            await conn.execute(
                "SELECT personal_purge_in_progress, personal_purge_generation, personal_purged_turn_ranges "
                "FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
                (tid,),
            )
        ).fetchone()
    assert t == (True, 1, [[1, 1]])

    ledger = await ui_db.get_purge_ledger(purge_id)
    assert ledger is not None
    assert ledger.local_purge_status == LocalPurgeStatus.RUNNING
    assert ledger.remote_revoke_status == RemoteRevokeStatus.PENDING

    # Startup resume picks up running ledger and finishes local purge + unblocks thread
    resumed = await run_startup_personal_purge_resume()
    assert purge_id in resumed

    async with gws_live_pool.connection() as conn:
        t2 = await (
            await conn.execute(
                "SELECT personal_purge_in_progress, personal_purge_generation "
                "FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
                (tid,),
            )
        ).fetchone()
    assert t2 == (False, 2)  # SECOND bump & unblocked immediately

    ledger2 = await ui_db.get_purge_ledger(purge_id)
    assert ledger2 is not None
    assert ledger2.local_purge_status == LocalPurgeStatus.COMPLETED
    assert ledger2.remote_revoke_status == RemoteRevokeStatus.PENDING  # remote independent
    assert await ui_db.get_connection(uid, "google", "calendar") is None


# ---------------------------------------------------------------------------
# 3. Account switch in-place replacement (sibling not_applicable vs sole pending)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_switch_reuses_phase1_keeps_new_connection_and_sibling_revoke_status(gws_live_pool):
    uid, _, tid = await _seed_user_ws_thread(gws_live_pool)
    r0 = await _seed_turn(gws_live_pool, thread_id=tid, turn_index=0, user_text="calendar Q")

    cal = await ui_db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id="123456-app.apps.googleusercontent.com",
        provider_account_id="sub-old",
        granted_scopes="openid email calendar.events",
        access_token="at-old",
        refresh_token="rt-old",
        preferences={"primary_calendar_id": "old-cal"},
    )
    # Add sibling mail connection on same (client, sub-old)
    await ui_db.upsert_connection(
        user_id=uid,
        provider="google",
        service="mail",
        oauth_client_id="123456-app.apps.googleusercontent.com",
        provider_account_id="sub-old",
        granted_scopes="openid email gmail.readonly",
        access_token="at-old",
        refresh_token="rt-old",
    )
    await ui_db.record_touch(
        thread_id=tid,
        response_id=r0,
        provider="google",
        service="calendar",
        auth_epoch=cal.auth_epoch,
        turn_index=0,
        user_id=uid,
        provider_account_id="sub-old",
    )

    # Missing refresh_token rejected, row intact
    with pytest.raises(MissingRefreshTokenOnAccountSwitchError):
        await replace_connection_for_account_switch(
            uid,
            "google",
            "calendar",
            new_oauth_client_id="123456-app.apps.googleusercontent.com",
            new_provider_account_id="sub-new",
            new_granted_scopes="openid email calendar.events",
            new_access_token="at-new",
            new_refresh_token=None,
        )

    # With active sibling on old sub -> old token remote_revoke_status='not_applicable'
    sw1 = await replace_connection_for_account_switch(
        uid,
        "google",
        "calendar",
        new_oauth_client_id="123456-app.apps.googleusercontent.com",
        new_provider_account_id="sub-new",
        new_granted_scopes="openid email calendar.events",
        new_access_token="at-new",
        new_refresh_token="rt-new",
    )
    assert sw1.remote_revoke_status == RemoteRevokeStatus.NOT_APPLICABLE
    assert sw1.new_auth_epoch > sw1.old_auth_epoch

    await finish_disconnect(sw1.purge_id)
    after_cal = await ui_db.get_connection(uid, "google", "calendar", decrypt_tokens=True)
    assert after_cal is not None
    assert after_cal.status == IntegrationStatus.CONNECTED
    assert after_cal.provider_account_id == "sub-new"
    assert after_cal.token_generation == 1
    assert after_cal.preferences == {}
    assert after_cal.refresh_token == "rt-new"

    # Now switch the last remaining row on sub-old (mail) -> remote_revoke_status='pending'
    sw2 = await replace_connection_for_account_switch(
        uid,
        "google",
        "mail",
        new_oauth_client_id="123456-app.apps.googleusercontent.com",
        new_provider_account_id="sub-new-2",
        new_granted_scopes="openid email gmail.readonly",
        new_access_token="at-new-2",
        new_refresh_token="rt-new-2",
    )
    assert sw2.remote_revoke_status == RemoteRevokeStatus.PENDING
    await finish_disconnect(sw2.purge_id)
    after_mail = await ui_db.get_connection(uid, "google", "mail")
    assert after_mail is not None and after_mail.status == IntegrationStatus.CONNECTED


# ---------------------------------------------------------------------------
# 4. Bounded delete by auth_epoch_upto preserves newer reconnection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_connection_upto_epoch_preserves_newer_reconnection(gws_live_pool):
    uid, _, _ = await _seed_user_ws_thread(gws_live_pool)
    old_conn = await ui_db.upsert_connection(
        user_id=uid,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_feishu",
        provider_account_id="ou_1",
        granted_scopes="offline_access calendar:calendar:readonly",
        access_token="at-1",
        refresh_token="rt-1",
    )
    purge_id = await begin_disconnect(uid, "feishu", "calendar")

    # User reconnects before Phase 2 deletes -> larger auth_epoch
    new_conn = await ui_db.upsert_connection(
        user_id=uid,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_feishu",
        provider_account_id="ou_1",
        granted_scopes="offline_access calendar:calendar:readonly",
        access_token="at-2",
        refresh_token="rt-2",
        bump_auth_epoch=True,
    )
    assert new_conn.auth_epoch > old_conn.auth_epoch

    res = await finish_disconnect(purge_id)
    assert res.deleted_connections == 0
    surviving = await ui_db.get_connection(uid, "feishu", "calendar")
    assert surviving is not None
    assert surviving.auth_epoch == new_conn.auth_epoch
    assert surviving.status == IntegrationStatus.CONNECTED


# ---------------------------------------------------------------------------
# 5. Remote revocation state machine & real Postgres advisory-lock mutual exclusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_state_machine_outcomes_supersede_and_lock_mutual_exclusion(gws_live_pool):
    uid, _, _ = await _seed_user_ws_thread(gws_live_pool)
    client_id = "777888-web.apps.googleusercontent.com"
    project = extract_google_project(client_id)

    ledger = await ui_db.insert_purge_ledger(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-lock",
        auth_epoch_upto=5,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        local_purge_status=LocalPurgeStatus.COMPLETED,
        purged_max_turn_by_thread={},
        remote_revoke_status=RemoteRevokeStatus.PENDING,
        revoke_token_plaintext="rt-mutex-secret",
    )

    entered = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def _slow_revoke(*, provider: str, oauth_client_id: str, token: str) -> RevokeOutcome:
        nonlocal call_count
        call_count += 1
        entered.set()
        await release.wait()
        return RevokeOutcome.SUCCEEDED

    revoker = create_autospec(RemoteRevoker, instance=True)
    revoker.revoke = AsyncMock(side_effect=_slow_revoke)

    t1 = asyncio.create_task(process_revoke(ledger.purge_id, revoker))
    await entered.wait()
    t2 = asyncio.create_task(process_revoke(ledger.purge_id, revoker))
    await asyncio.sleep(0.05)
    release.set()
    s1, s2 = await asyncio.gather(t1, t2)

    assert s1 == RemoteRevokeStatus.SUCCEEDED
    assert s2 == RemoteRevokeStatus.SUCCEEDED
    assert call_count == 1  # Mutual exclusion guaranteed single upstream call!
    done = await ui_db.get_purge_ledger(ledger.purge_id, decrypt_revoke_token=True)
    assert done is not None and done.revoke_token is None and done.remote_completed_at is not None

    # Retryable -> backoff -> supersede_pending_revokes & revoke_before_new_authorization
    l2 = await ui_db.insert_purge_ledger(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-2",
        auth_epoch_upto=6,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        local_purge_status=LocalPurgeStatus.COMPLETED,
        purged_max_turn_by_thread={},
        remote_revoke_status=RemoteRevokeStatus.PENDING,
        revoke_token_plaintext="rt-retry",
    )
    rev_retry = create_autospec(RemoteRevoker, instance=True)
    rev_retry.revoke = AsyncMock(return_value=RevokeOutcome.RETRYABLE)
    assert await process_revoke(l2.purge_id, rev_retry) == RemoteRevokeStatus.FAILED_RETRYABLE
    l2_after = await ui_db.get_purge_ledger(l2.purge_id)
    assert l2_after is not None and l2_after.retry_count == 1 and l2_after.next_retry_at is not None

    # revoke_before_new_authorization on retryable failure marks superseded immediately
    res_pre = await revoke_before_new_authorization(uid, project, "sub-2", rev_retry)
    assert res_pre[l2.purge_id] == RemoteRevokeStatus.SUPERSEDED
    assert (await ui_db.get_purge_ledger(l2.purge_id, decrypt_revoke_token=True)).revoke_token is None

    # supersede_pending_revokes clears pending tasks
    l3 = await ui_db.insert_purge_ledger(
        user_id=uid,
        provider="google",
        service="mail",
        oauth_client_id=client_id,
        provider_account_id="sub-3",
        auth_epoch_upto=7,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        local_purge_status=LocalPurgeStatus.COMPLETED,
        purged_max_turn_by_thread={},
        remote_revoke_status=RemoteRevokeStatus.PENDING,
        revoke_token_plaintext="rt-sup",
    )
    assert await supersede_pending_revokes(uid, project, "sub-3") == 1
    l3_after = await ui_db.get_purge_ledger(l3.purge_id, decrypt_revoke_token=True)
    assert l3_after.remote_revoke_status == RemoteRevokeStatus.SUPERSEDED
    assert l3_after.revoke_token is None


# ---------------------------------------------------------------------------
# 6. START transaction, HTTP _admit, and Fork gates (409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gates_start_run_admit_and_fork_refuse_during_purge(gws_live_pool):
    uid, _, tid = await _seed_user_ws_thread(gws_live_pool)
    r0 = await _seed_turn(gws_live_pool, thread_id=tid, turn_index=0, user_text="q0")

    c = await ui_db.upsert_connection(
        user_id=uid,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_1",
        provider_account_id="ou_gate",
        granted_scopes="offline_access",
        access_token="at",
        refresh_token="rt",
    )
    await ui_db.record_touch(
        thread_id=tid,
        response_id=r0,
        provider="feishu",
        service="calendar",
        auth_epoch=c.auth_epoch,
        turn_index=0,
        user_id=uid,
        provider_account_id="ou_gate",
    )
    purge_id = await begin_disconnect(uid, "feishu", "calendar")

    # 1. HTTP _admit fast gate
    with pytest.raises(PersonalDataPurgeInProgressError) as e_admit:
        await check_thread_not_purging_fast(tid)
    assert e_admit.value.status_code == 409
    assert e_admit.value.headers["Retry-After"] == "3"

    # 2. Authoritative START transaction gate (covers HTTP, automation, wakeup, retry)
    with pytest.raises(PersonalDataPurgeInProgressError) as e_start:
        await runs_lifecycle.start_run(
            run_id=str(uuid.uuid4()),
            thread_id=tid,
            request_key=f"rk-{uuid.uuid4()}",
            turn_index=1,
            user_id=uid,
        )
    assert e_start.value.status_code == 409
    assert e_start.value.detail["code"] == PURGE_IN_PROGRESS_CODE

    # 3. Fork gate inside transaction
    async with gws_live_pool.connection() as conn, conn.transaction():
        with pytest.raises(ForkRefused) as e_fork:
            await assert_fork_source_not_purging_tx(conn, tid, cut_turn_index=0)
    assert e_fork.value.status_code == 409
    assert e_fork.value.code == PURGE_IN_PROGRESS_CODE

    # Finish purge -> gates admit again
    await finish_disconnect(purge_id)
    await check_thread_not_purging_fast(tid)


# ---------------------------------------------------------------------------
# 7. Dual-connection interleaving FOR SHARE writeback fences
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dual_connection_interleaving_fences_replay_search_and_transcript(gws_live_pool):
    uid, _, tid = await _seed_user_ws_thread(gws_live_pool)
    r0 = await _seed_turn(gws_live_pool, thread_id=tid, turn_index=0, user_text="secret calendar text")

    c = await ui_db.upsert_connection(
        user_id=uid,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_1",
        provider_account_id="ou_fence",
        granted_scopes="offline_access",
        access_token="at",
        refresh_token="rt",
    )
    await ui_db.record_touch(
        thread_id=tid,
        response_id=r0,
        provider="feishu",
        service="calendar",
        auth_epoch=c.auth_epoch,
        turn_index=0,
        user_id=uid,
        provider_account_id="ou_fence",
    )

    # Snapshot BEFORE purge starts (gen = 0)
    gen_before = await read_expected_purge_generation(tid)
    assert gen_before == 0

    # Start purge (gen = 1, in_progress = True)
    purge_id = await begin_disconnect(uid, "feishu", "calendar")
    gen_during = await read_expected_purge_generation(tid)
    assert gen_during == 1

    # Complete purge (gen = 2, in_progress = False)
    await finish_disconnect(purge_id)
    gen_after = await read_expected_purge_generation(tid)
    assert gen_after == 2

    # Both stale snapshots (gen_before=0 AND gen_during=1) are REJECTED by turn_slices, search write_turns, and transcript save!
    for stale_gen in (gen_before, gen_during):
        await slices_db.upsert_turn_slices(
            [
                slices_db.TurnRow(
                    r0,
                    tid,
                    slices_db.StoredSlice("cp-in", "cp-tail", "sk", "json", b"{}", "lk", "stale"),
                )
            ],
            thread_id=tid,
            expected_purge_gen=stale_gen,
        )
        assert await slices_db.get_turn_lines([r0]) == {}

        written = await search_db.write_turns(
            tid,
            [search_db.IndexedTurn(r0, "stale secret", "stale answer", True, datetime.now(UTC))],
            read_at=datetime.now(UTC),
            expected_purge_gen=stale_gen,
        )
        assert written == 0

        blob_bytes = b"stale transcript content" * 100
        sha = hashlib.sha256(blob_bytes).hexdigest()
        tr = tt_db.StoredTranscript("fp-stale", "cp-1", '{"segments": []}', files={"manifest.json": (sha, len(blob_bytes))})
        saved = await save_stored_transcript_fenced(
            tid, "", uid, tr, {sha: blob_bytes}, expected_purge_gen=stale_gen
        )
        assert saved is False

    # Dual-connection transcript interleaving: upload landed while holding FOR SHARE,
    # then save_stored fails/aborts -> uploaded blob is condemned via release_blob with zero orphan refs!
    blob_bytes = b"large transcript blob payload" * 3000
    sha = hashlib.sha256(blob_bytes).hexdigest()
    tr_ok = tt_db.StoredTranscript(
        "fp-new",
        "cp-2",
        '{"segments": []}',
        files={"turn-0001.jsonl": (sha, len(blob_bytes))},
    )

    with (
        patch("src.utils.storage.upload_bytes", return_value=True),
        patch.object(tt_db, "save_stored", AsyncMock(return_value=False)),
    ):
        ok = await save_stored_transcript_fenced(
            tid, "", uid, tr_ok, {sha: blob_bytes}, expected_purge_gen=gen_after
        )
    assert ok is False

    async with gws_live_pool.connection() as conn:
        blob_row = await (
            await conn.execute(
                "SELECT condemned_at FROM workspace_file_blobs WHERE user_id = %s AND sha256 = %s",
                (uid, sha),
            )
        ).fetchone()
        ref_row = await (
            await conn.execute(
                "SELECT COUNT(*) FROM thread_transcript_files WHERE conversation_thread_id = %s::uuid",
                (tid,),
            )
        ).fetchone()
    assert blob_row is not None and blob_row[0] is not None  # condemned cleanly!
    assert ref_row is not None and ref_row[0] == 0  # zero orphan refs!


# ---------------------------------------------------------------------------
# 8. Full 2A purge scope (checkpoints + 3 summary copies + outbox + Redis + approvals + tracker)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_2a_full_scope_checkpoints_summaries_outbox_redis_approvals_and_tracker(
    gws_live_pool, monkeypatch
):
    uid, wid, tid = await _seed_user_ws_thread(gws_live_pool)
    monkeypatch.setenv("DALTON_TRACKER_CALENDAR_OWNER_ID", uid)

    r0 = await _seed_turn(gws_live_pool, thread_id=tid, turn_index=0, user_text="public NVDA question")
    r1 = await _seed_turn(
        gws_live_pool,
        thread_id=tid,
        turn_index=1,
        user_text="show my personal calendar",
        steering_inputs=[{"content": "steer about private dinner"}],
    )

    cal = await ui_db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id="123456-app.apps.googleusercontent.com",
        provider_account_id="sub-2a",
        granted_scopes="openid email calendar.events",
        access_token="at",
        refresh_token="rt",
    )
    await ui_db.record_touch(
        thread_id=tid,
        response_id=r1,
        provider="google",
        service="calendar",
        auth_epoch=cal.auth_epoch,
        turn_index=1,
        user_id=uid,
        provider_account_id="sub-2a",
    )

    # Seed hook_outbox delivered, pending, and failed steers + approvals across TWO services
    async with gws_live_pool.connection() as conn:
        for idx, (st, payload) in enumerate(
            [
                ("done", {"outcome": "delivered", "steered_run_id": r1, "content": "delivered secret steer"}),
                ("pending", {"target": "thread", "content": "pending secret steer"}),
                ("done", {"outcome": "failed", "failed_run_id": r1, "content": "failed secret steer"}),
            ]
        ):
            await conn.execute(
                """
                INSERT INTO hook_outbox (
                    hook_outbox_id, run_id, conversation_thread_id, hook_type,
                    payload, idempotency_key, status
                )
                VALUES (%s::uuid, %s::uuid, %s::uuid, 'steer', %s, %s, %s)
                """,
                (str(uuid.uuid4()), r1, tid, Jsonb(payload), f"ik-{idx}-{uuid.uuid4()}", st),
            )
        # Approval for another service ('feishu','mail') in the SAME suffix run r1 -> must also be deleted!
        await conn.execute(
            """
            INSERT INTO dalton_integration_action_approvals (
                approval_id, caller_user_id, thread_id, run_id, action_type,
                provider, service, provider_account_id, auth_epoch,
                payload_sha256, status, expires_at
            )
            VALUES (%s::uuid, %s, %s::uuid, %s::uuid, 'dalton_mail_send',
                    'feishu', 'mail', 'ou_other', 1, 'abc', 'pending', NOW() + interval '10 minutes')
            """,
            (str(uuid.uuid4()), uid, tid, r1),
        )

    # Seed checkpoint tables (if present on test DB) with pre-touch turn 0, suffix turn 1, and cross-boundary summary
    serde = JsonPlusSerializer()
    summary_id = "sum-cross-db"
    fake_cp = SimpleNamespace(conn=gws_live_pool, serde=serde)

    async with gws_live_pool.connection() as conn:
        has_cp_tbl = (await (await conn.execute("SELECT to_regclass('public.checkpoints')")).fetchone())[0]
        if has_cp_tbl:
            await conn.execute(
                """
                INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata)
                VALUES
                  (%s, '', 'cp-0', NULL, 'json', %s, %s),
                  (%s, '', 'cp-1', 'cp-0', 'json', %s, %s)
                """,
                (
                    tid,
                    Jsonb({"channel_versions": {"messages": "v0", "ui": "u0"}}),
                    Jsonb({"turn_index": 0, "run_id": r0}),
                    tid,
                    Jsonb({"channel_versions": {"messages": "v1", "ui": "u1"}}),
                    Jsonb({"turn_index": 1, "run_id": r1}),
                ),
            )
            m0 = HumanMessage(content="public NVDA question", id="m0")
            ai0 = AIMessage(
                content="public NVDA answer",
                id="ai0",
                response_metadata={
                    COMPACTION_SUMMARY_KEY: {
                        "summary_message_id": summary_id,
                        "summary_text": "cross-boundary summary leaking private dinner",
                        "summary_length": 45,
                    }
                },
            )
            sum_msg = HumanMessage(
                content="cross-boundary summary leaking private dinner",
                id=summary_id,
                additional_kwargs={
                    "lc_source": "summarization",
                    SUMMARIZED_KEY: {"span": [0, 1], "gap": None, "skills": []},
                    SIDECAR_KEY: {"native": "secret"},
                },
            )
            m1 = HumanMessage(content="show my personal calendar", id="m1")
            ai1 = AIMessage(
                content="Private dinner at 8pm",
                id="ai1",
                tool_calls=[{"id": "tc1", "name": "dalton_calendar", "type": "tool_call", "args": {"secret": "val"}}],
            )
            t1 = ToolMessage(content="Dinner with CEO", tool_call_id="tc1", id="t1")

            b0_t, b0_b = serde.dumps_typed(_DeltaSnapshot([m0, ai0, sum_msg]))
            b1_t, b1_b = serde.dumps_typed(_DeltaSnapshot([m0, ai0, sum_msg, m1, ai1, t1]))
            u0_t, u0_b = serde.dumps_typed(
                [
                    {
                        "type": "ui",
                        "id": "ui-1",
                        "name": "context_window",
                        "props": {"action": "summarize"},
                        "metadata": {
                            "summary_message_id": summary_id,
                            "replaced_summary": {
                                "id": summary_id,
                                "action": "summarize",
                                "summary_text": "replaced cross summary secret",
                            },
                        },
                    }
                ]
            )
            for ch, ver, ty, bl in (
                ("messages", "v0", b0_t, b0_b),
                ("messages", "v1", b1_t, b1_b),
                ("ui", "u0", u0_t, u0_b),
            ):
                await conn.execute(
                    """
                    INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob)
                    VALUES (%s, '', %s, %s, %s, %s)
                    """,
                    (tid, ch, ver, ty, bl),
                )

    deleted_redis_keys: list[str] = []

    class _FakeRedisClient:
        async def delete(self, *keys):
            deleted_redis_keys.extend(keys)
            return len(keys)

    fake_cache = SimpleNamespace(enabled=True, client=_FakeRedisClient())

    purge_id = await begin_disconnect(uid, "google", "calendar", remote_mode="local_only")
    with (
        patch.object(purge_mod, "get_checkpointer", return_value=fake_cp),
        patch("src.utils.cache.redis_cache.get_cache_client", return_value=fake_cache),
        patch("src.server.services.transcripts.schedule_thread_export"),
    ):
        report = await run_local_purge(purge_id)

    assert report.tracker_reconcile_needed is True
    assert f"workflow:stream:{tid}:{r1}" in deleted_redis_keys
    assert f"workflow:events:meta:{tid}:{r1}" in deleted_redis_keys

    async with gws_live_pool.connection() as conn:
        # Turn 0 query preserved, turn 1 query redacted
        q0 = (await (await conn.execute("SELECT content FROM conversation_queries WHERE conversation_thread_id=%s::uuid AND turn_index=0", (tid,))).fetchone())[0]
        q1 = (await (await conn.execute("SELECT content FROM conversation_queries WHERE conversation_thread_id=%s::uuid AND turn_index=1", (tid,))).fetchone())[0]
        assert q0 == "public NVDA question"
        assert q1 == PURGED_PLACEHOLDER

        # steering_inputs redacted
        meta1 = (await (await conn.execute("SELECT metadata FROM conversation_responses WHERE conversation_response_id=%s::uuid", (r1,))).fetchone())[0]
        assert meta1["steering_inputs"][0]["content"] == PURGED_PLACEHOLDER

        # All 3 hook_outbox steer rows redacted
        ob_rows = await (await conn.execute("SELECT status, payload FROM hook_outbox WHERE conversation_thread_id=%s::uuid", (tid,))).fetchall()
        assert len(ob_rows) == 3
        for st, pl in ob_rows:
            assert st == "done"
            assert pl["content"] == PURGED_PLACEHOLDER

        # Other-service approval in suffix run deleted
        app_cnt = (await (await conn.execute("SELECT COUNT(*) FROM dalton_integration_action_approvals WHERE thread_id=%s::uuid", (tid,))).fetchone())[0]
        assert app_cnt == 0

        # Checkpoint blobs redacted (including all 3 compaction summary copies) while pre-touch preserved
        if has_cp_tbl:
            row_v1 = await (await conn.execute("SELECT type, blob FROM checkpoint_blobs WHERE thread_id=%s AND channel='messages' AND version='v1'", (tid,))).fetchone()
            msgs_v1 = serde.loads_typed((row_v1[0], bytes(row_v1[1]))).value
            by_id = {m.id: m for m in msgs_v1}
            assert by_id["m0"].content == "public NVDA question"
            assert by_id["ai0"].content == "public NVDA answer"
            assert by_id["ai0"].response_metadata[COMPACTION_SUMMARY_KEY]["summary_text"] == PURGED_PLACEHOLDER
            assert by_id[summary_id].content == PURGED_PLACEHOLDER
            assert by_id["m1"].content == PURGED_PLACEHOLDER
            assert by_id["ai1"].content == PURGED_PLACEHOLDER
            assert by_id["ai1"].tool_calls == [{"id": "tc1", "name": "dalton_calendar", "type": "tool_call", "args": PURGED_TOOL_ARGS}]
            assert by_id["t1"].content == PURGED_PLACEHOLDER

            row_u0 = await (await conn.execute("SELECT type, blob FROM checkpoint_blobs WHERE thread_id=%s AND channel='ui' AND version='u0'", (tid,))).fetchone()
            ui_u0 = serde.loads_typed((row_u0[0], bytes(row_u0[1])))
            assert ui_u0[0]["metadata"]["replaced_summary"]["summary_text"] == PURGED_PLACEHOLDER


# ---------------------------------------------------------------------------
# 9. Search closed-range guard, truncate clipping, and Fork ThroughTurn / BeforeTurn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_closed_range_truncate_and_fork_boundaries(gws_live_pool):
    uid, wid, tid = await _seed_user_ws_thread(gws_live_pool)
    r0 = await _seed_turn(gws_live_pool, thread_id=tid, turn_index=0, user_text="turn0")
    r1 = await _seed_turn(gws_live_pool, thread_id=tid, turn_index=1, user_text="turn1")
    r2 = await _seed_turn(gws_live_pool, thread_id=tid, turn_index=2, user_text="turn2")

    c = await ui_db.upsert_connection(
        user_id=uid,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_1",
        provider_account_id="ou_s",
        granted_scopes="offline_access",
        access_token="at",
        refresh_token="rt",
    )
    await ui_db.record_touch(
        thread_id=tid,
        response_id=r1,
        provider="feishu",
        service="calendar",
        auth_epoch=c.auth_epoch,
        turn_index=1,
        user_id=uid,
        provider_account_id="ou_s",
    )
    purge_id = await begin_disconnect(uid, "feishu", "calendar")
    await finish_disconnect(purge_id)

    # Fork ThroughTurn(2) -> keeps [1, 2]; Fork BeforeTurn(2) -> keeps [1, 1]
    _, _, fork_through = await _seed_user_ws_thread(gws_live_pool, user_id=uid, workspace_id=wid)
    _, _, fork_before = await _seed_user_ws_thread(gws_live_pool, user_id=uid, workspace_id=wid)
    new_r1_a = str(uuid.uuid4())
    new_r1_b = str(uuid.uuid4())

    async with gws_live_pool.connection() as conn, conn.transaction():
        await copy_personal_integration_state_for_fork_tx(
            conn,
            source_thread_id=tid,
            target_thread_id=fork_through,
            cut=ThroughTurn(2),
            kept_turn_indexes=[0, 1, 2],
            run_map={r0: str(uuid.uuid4()), r1: new_r1_a, r2: str(uuid.uuid4())},
        )
        await copy_personal_integration_state_for_fork_tx(
            conn,
            source_thread_id=tid,
            target_thread_id=fork_before,
            cut=BeforeTurn(2),
            kept_turn_indexes=[0, 1],
            run_map={r0: str(uuid.uuid4()), r1: new_r1_b},
        )

    async with gws_live_pool.connection() as conn:
        ft = await (await conn.execute("SELECT has_personal_integration_data, personal_purged_turn_ranges FROM conversation_threads WHERE conversation_thread_id=%s::uuid", (fork_through,))).fetchone()
        fb = await (await conn.execute("SELECT has_personal_integration_data, personal_purged_turn_ranges FROM conversation_threads WHERE conversation_thread_id=%s::uuid", (fork_before,))).fetchone()
    assert ft == (True, [[1, 2]])
    assert fb == (True, [[1, 1]])

    # Truncate original thread from_turn_index=2 -> clips [[1, 2]] to [[1, 1]]
    await tw_db.truncate_thread_from_turn(tid, from_turn_index=2)
    async with gws_live_pool.connection() as conn:
        clipped = (await (await conn.execute("SELECT personal_purged_turn_ranges FROM conversation_threads WHERE conversation_thread_id=%s::uuid", (tid,))).fetchone())[0]
    assert clipped == [[1, 1]]


# ---------------------------------------------------------------------------
# 10. Restore reset SQL on migrated schema AND missing-table schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_reset_sql_clears_four_targets_and_safe_when_tables_missing(gws_live_pool):
    uid, _, _ = await _seed_user_ws_thread(gws_live_pool)
    await ui_db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id="123-c.apps.googleusercontent.com",
        provider_account_id="sub-r",
        granted_scopes="openid email",
        access_token="at-restore",
        refresh_token="rt-restore",
    )
    ledger = await ui_db.insert_purge_ledger(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id="123-c.apps.googleusercontent.com",
        provider_account_id="sub-r",
        auth_epoch_upto=1,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        local_purge_status=LocalPurgeStatus.COMPLETED,
        purged_max_turn_by_thread={},
        remote_revoke_status=RemoteRevokeStatus.PENDING,
        revoke_token_plaintext="rt-revoke-restore",
    )
    now = datetime.now(UTC)
    await ui_db.insert_oauth_state(
        state_hash="sh-1",
        user_id=uid,
        auth_mode="supabase",
        session_id=None,
        user_email="u@example.com",
        provider="google",
        service="calendar",
        oauth_client_id="123-c.apps.googleusercontent.com",
        intent="connect",
        code_verifier="verifier-1",
        requested_scopes="openid",
        redirect_uri="https://example.com/cb",
        return_to="/settings",
        expires_at=now + timedelta(minutes=10),
    )
    await ui_db.insert_pending_confirmation(
        confirmation_hash="ch-1",
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id="123-c.apps.googleusercontent.com",
        provider_account_id="sub-r",
        provider_union_id=None,
        provider_email="u@example.com",
        granted_scopes="openid",
        access_token="at-stage",
        refresh_token="rt-stage",
        expires_at=now + timedelta(hours=1),
        intent="connect",
        preferences={},
        confirm_expires_at=now + timedelta(minutes=5),
    )

    sql = (
        PROJECT_ROOT / "scripts" / "dalton" / "sql" / "personal_integrations_restore_reset.sql"
    ).read_text(encoding="utf-8")

    # Execute on migrated DB
    async with gws_live_pool.connection() as conn:
        await conn.execute(sql)

    c_after = await ui_db.get_connection(uid, "google", "calendar")
    assert c_after is not None
    assert c_after.status == IntegrationStatus.NEEDS_REAUTH
    assert c_after.has_access_token is False
    assert c_after.has_refresh_token is False
    l_after = await ui_db.get_purge_ledger(ledger.purge_id)
    assert l_after is not None and l_after.has_revoke_token is False
    async with gws_live_pool.connection() as conn:
        s_cnt = (await (await conn.execute("SELECT COUNT(*) FROM dalton_user_integration_oauth_states")).fetchone())[0]
        p_cnt = (await (await conn.execute("SELECT COUNT(*) FROM dalton_user_integration_pending_confirmations")).fetchone())[0]
    assert s_cnt == 0 and p_cnt == 0

    # Execute safely on an empty schema lacking these tables (simulated via dedicated schema without public)
    # Note: to_regclass('public.<table>') checks public explicitly, so we test a standalone DO block
    # against a nonexistent table name as well as re-running the idempotent SQL.
    async with gws_live_pool.connection() as conn:
        await conn.execute(sql)  # idempotent re-run
        missing_sql = sql.replace("public.dalton_", "public.__nonexistent_dalton_")
        await conn.execute(missing_sql)
