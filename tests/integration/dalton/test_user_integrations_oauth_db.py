"""Real PostgreSQL integration tests for Personal Calendar & Mail Batch 1 — Step 2.

Tests real PostgreSQL advisory locks, atomic single-use OAuth state consumption across
concurrent connections, Google 3-case flows (first connect, same-sub client rotation,
confirmed account switch with Scheme 2A purge), serialized refresh + CAS 3-element check,
Google revoke coordination, Feishu webhook atomic dedup + authorized_at filtering, and
multi-worker 12-hour health patrol coordination.

Run outside the sandbox against PostgreSQL 18 + pgvector:
    TEST_DB_HOST=127.0.0.1 TEST_DB_PORT=55432 TEST_DB_NAME=lane82_test \
    TEST_DB_USER=postgres TEST_DB_PASSWORD=pgtest \
    DALTON_TEST_PG_DSN=postgresql://postgres:pgtest@127.0.0.1:55432/lane82_test \
    .venv/bin/python -m pytest -q -o addopts="" -p no:cacheprovider \
      tests/integration/dalton/test_user_integrations_oauth_db.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import time
from typing import Any
from unittest.mock import create_autospec
import uuid

import pytest

from src.config.settings import LOCAL_DEV_USER_ID
from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import (
    IntegrationStatus,
    LocalPurgeStatus,
    RemoteRevokeStatus,
    TriggerReason,
)
from src.dalton.deployment_credentials.catalog import (
    FEISHU_APP_ID_KEY,
    FEISHU_APP_SECRET_KEY,
    FEISHU_ENCRYPT_KEY,
    FEISHU_VERIFICATION_TOKEN_KEY,
    GOOGLE_OAUTH_CLIENT_ID_KEY,
    GOOGLE_OAUTH_CLIENT_SECRET_KEY,
)
from src.dalton.deployment_credentials.snapshot import (
    build_snapshot,
    reset_active_snapshot_for_test,
    set_active_snapshot,
)
from src.dalton.integrations.constants import (
    FEISHU_CALENDAR_SCOPES,
    FEISHU_REVOKED_EVENT_TYPE,
    GOOGLE_CALENDAR_MIN_SCOPES,
    GOOGLE_GMAIL_MIN_SCOPES,
    GOOGLE_SCOPE_CALENDAR_LIST_RO,
)
from src.dalton.integrations.oauth_clients import (
    FeishuOAuthClient,
    GoogleIdTokenClaims,
    GoogleOAuthClient,
    OAuthTokenResponse,
    OAuthUpstreamError,
)
from src.dalton.services import user_integrations_oauth as oauth_mod
from src.dalton.services.feishu_integration_events import process_feishu_integration_event
from src.dalton.services.integration_health_sweep import run_integration_health_sweep_once
from src.dalton.services.integration_revoke import RemoteRevoker, RevokeOutcome
from src.dalton.services.user_integrations_oauth import (
    IntegrationNeedsReauthError,
    IntegrationRevokedError,
    IntegrationTransientError,
    PendingConfirmationInvalidError,
    confirm_pending_integration,
    ensure_fresh_access_token,
    handle_oauth_callback,
    start_oauth_authorization,
)
from src.server.services.setup_settings import environment as setup_env_mod

from tests.integration.dalton.test_user_integrations_purge_db import _seed_turn

pytest_plugins = ["tests.integration.dalton.test_user_integrations_purge_db"]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _install_creds(
    *,
    google_client_id: str | None = "111222333-alpha.apps.googleusercontent.com",
    google_client_secret: str | None = "g-secret-alpha",
    feishu_app_id: str | None = "cli_db_test_1",
    feishu_app_secret: str | None = "fs-secret-1",
    feishu_encrypt_key: str | None = "fs-enc-key-1",
    feishu_verification_token: str | None = "fs-ver-tok-1",
) -> None:
    rows: dict[str, str] = {}
    if google_client_id is not None:
        rows[GOOGLE_OAUTH_CLIENT_ID_KEY] = google_client_id
    if google_client_secret is not None:
        rows[GOOGLE_OAUTH_CLIENT_SECRET_KEY] = google_client_secret
    if feishu_app_id is not None:
        rows[FEISHU_APP_ID_KEY] = feishu_app_id
    if feishu_app_secret is not None:
        rows[FEISHU_APP_SECRET_KEY] = feishu_app_secret
    if feishu_encrypt_key is not None:
        rows[FEISHU_ENCRYPT_KEY] = feishu_encrypt_key
    if feishu_verification_token is not None:
        rows[FEISHU_VERIFICATION_TOKEN_KEY] = feishu_verification_token
    set_active_snapshot(build_snapshot(revision=1, rows=rows))


@pytest.fixture(autouse=True)
def _step2_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    reset_active_snapshot_for_test()
    monkeypatch.setattr(setup_env_mod, "set_outside_app", create_autospec(setup_env_mod.set_outside_app, return_value=False))
    monkeypatch.setattr(setup_env_mod, "_snapshot", create_autospec(setup_env_mod._snapshot, return_value={}))
    monkeypatch.setenv("DALTON_ENABLED", "1")
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "test-pg-enc-key-0123456789abcdef")
    monkeypatch.setenv("PUBLIC_APP_URL", "https://dalton.example.com")
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.delenv("DALTON_PROFILE", raising=False)
    yield
    reset_active_snapshot_for_test()


async def test_oauth_state_atomic_single_use_concurrent_two_connections_and_expiry_db(
    gws_live_pool: Any,
) -> None:
    """Two concurrent PostgreSQL connections racing to consume the same OAuth state: exactly one wins."""
    _ = gws_live_pool
    _install_creds()
    started = await start_oauth_authorization(
        user_id=LOCAL_DEV_USER_ID,
        provider="google",
        service="calendar",
        auth_mode="oss",
        revoker=create_autospec(RemoteRevoker, instance=True),
    )
    sh = hashlib.sha256(started.state.encode()).hexdigest()

    r1, r2 = await asyncio.wait_for(
        asyncio.gather(
            db.consume_oauth_state(sh, "google"),
            db.consume_oauth_state(sh, "google"),
        ),
        timeout=15.0,
    )
    winners = [r for r in (r1, r2) if r is not None]
    assert len(winners) == 1
    assert winners[0].user_id == LOCAL_DEV_USER_ID
    await db.delete_oauth_state(sh, "google")

    # Expired state is never consumed
    exp_state = "expired-pg-state-" + uuid.uuid4().hex
    exp_hash = hashlib.sha256(exp_state.encode()).hexdigest()
    await db.insert_oauth_state(
        state_hash=exp_hash,
        user_id=LOCAL_DEV_USER_ID,
        auth_mode="oss",
        session_id=None,
        user_email=None,
        provider="google",
        service="calendar",
        oauth_client_id="111222333-alpha.apps.googleusercontent.com",
        intent="connect",
        code_verifier="verifier-exp",
        requested_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        redirect_uri="https://dalton.example.com/api/v1/dalton/integrations/google/callback",
        return_to="/settings?tab=integrations",
        expires_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    assert await db.consume_oauth_state(exp_hash, "google") is None


async def test_google_first_connect_client_rotation_rebind_and_account_switch_confirm_2a_db(
    gws_live_pool: Any,
) -> None:
    """Real PG: first connect -> same-sub client rotation keeps history -> different-sub switch confirms & purges 2A."""
    pool = gws_live_pool
    uid = LOCAL_DEV_USER_ID
    client_a = "111222333-alpha.apps.googleusercontent.com"
    client_b = "111222333-beta.apps.googleusercontent.com"
    _install_creds(google_client_id=client_a)

    g_client = create_autospec(GoogleOAuthClient, instance=True)
    g_client.revoke.return_value = RevokeOutcome.SUCCEEDED

    def _mock_google(sub: str, email: str, aud: str, rt: str) -> None:
        g_client.exchange_code.return_value = OAuthTokenResponse(
            access_token=f"at-{sub}-{aud}",
            refresh_token=rt,
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token=f"id-{sub}",
        )
        g_client.verify_id_token.return_value = GoogleIdTokenClaims(
            sub=sub,
            email=email,
            email_verified=True,
            aud=aud,
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )

    # 1) First connect as sub-alice on client_a
    _mock_google("sub-alice", "alice@example.com", client_a, "rt-alice-a")
    s1 = await start_oauth_authorization(
        user_id=uid, provider="google", service="calendar", auth_mode="oss", revoker=g_client
    )
    cb1 = await handle_oauth_callback(
        provider="google", code="c1", state=s1.state, google_client=g_client, revoker=g_client
    )
    assert cb1.status == "connected"
    assert cb1.connection is not None
    epoch_1 = cb1.connection.auth_epoch

    # Seed a private thread touched by sub-alice
    ws_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, email, name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (uid, "alice@example.com", "Alice"),
        )
        await conn.execute(
            "INSERT INTO workspaces (workspace_id, user_id, name, status) VALUES (%s::uuid, %s, 'Home', 'running')",
            (ws_id, uid),
        )
        await conn.execute(
            "INSERT INTO conversation_threads (conversation_thread_id, workspace_id, user_id, current_status, msg_type, thread_index, title) "
            "VALUES (%s::uuid, %s::uuid, %s, 'completed', 'ptc', 1, 'Private Cal')",
            (thread_id, ws_id, uid),
        )
    resp_id = await _seed_turn(
        pool,
        thread_id=thread_id,
        turn_index=0,
        user_text="Alice secret calendar note",
    )
    async with pool.connection() as conn:
        await db.record_touch(
            thread_id=thread_id,
            response_id=resp_id,
            provider="google",
            service="calendar",
            auth_epoch=epoch_1,
            turn_index=0,
            user_id=uid,
            provider_account_id="sub-alice",
            conn=conn,
        )

    # 2) Admin rotates client_a -> client_b; same sub-alice re-authorizes -> in-place rebind, history preserved!
    _install_creds(google_client_id=client_b)
    _mock_google("sub-alice", "alice@example.com", client_b, "rt-alice-b")
    s2 = await start_oauth_authorization(
        user_id=uid, provider="google", service="calendar", auth_mode="oss", revoker=g_client
    )
    cb2 = await handle_oauth_callback(
        provider="google", code="c2", state=s2.state, google_client=g_client, revoker=g_client
    )
    assert cb2.status == "connected"
    assert cb2.connection is not None
    assert cb2.connection.oauth_client_id == client_b
    assert cb2.connection.auth_epoch > epoch_1
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT content FROM conversation_queries WHERE conversation_thread_id = %s::uuid AND turn_index = 0",
                (thread_id,),
            )
            q_row = await cur.fetchone()
            assert q_row is not None and q_row[0] == "Alice secret calendar note"

    # 3) Account switch to sub-bob -> callback returns account_switch_confirmation_required
    _mock_google("sub-bob", "bob@example.com", client_b, "rt-bob-b")
    s3 = await start_oauth_authorization(
        user_id=uid,
        provider="google",
        service="calendar",
        auth_mode="oss",
        switch_account=True,
        revoker=g_client,
    )
    cb3 = await handle_oauth_callback(
        provider="google", code="c3", state=s3.state, google_client=g_client, revoker=g_client
    )
    assert cb3.status == "account_switch_confirmation_required"
    assert cb3.confirmation_token

    # Unconfirmed call does not switch
    unconfirmed = await confirm_pending_integration(
        user_id=uid, confirmation_token=cb3.confirmation_token, confirm_account_switch=False
    )
    assert unconfirmed.status == "account_switch_confirmation_required"

    # Confirmed switch runs Step 1 Phase 1 + 2A purge and keeps sub-bob connected
    confirmed = await confirm_pending_integration(
        user_id=uid,
        confirmation_token=cb3.confirmation_token,
        confirm_account_switch=True,
        revoker=g_client,
    )
    assert confirmed.status == "connected"
    assert confirmed.purge_id is not None
    assert confirmed.connection is not None
    assert confirmed.connection.provider_account_id == "sub-bob"
    assert confirmed.connection.status == IntegrationStatus.CONNECTED

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT content FROM conversation_queries WHERE conversation_thread_id = %s::uuid AND turn_index = 0",
                (thread_id,),
            )
            q_after = await cur.fetchone()
            assert q_after is not None and "Alice secret calendar note" not in q_after[0]


async def test_serialized_refresh_concurrent_two_workers_single_upstream_call_and_cas_db(
    gws_live_pool: Any,
) -> None:
    """Real PG advisory lock ensures 2 concurrent Feishu refreshes issue exactly 1 upstream call."""
    _ = gws_live_pool
    _install_creds()
    uid = "user-refresh-pg"
    await db.upsert_connection(
        user_id=uid,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou_refresh_1",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="expired-at",
        refresh_token="rt-once-1",
        expires_at=datetime.now(UTC) - timedelta(seconds=30),
    )

    f_client = create_autospec(FeishuOAuthClient, instance=True)

    async def _slow_refresh(**_kw: Any) -> OAuthTokenResponse:
        await asyncio.sleep(0.15)
        return OAuthTokenResponse(
            access_token="fresh-at-pg",
            refresh_token="rt-once-2",
            expires_in=7200,
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )

    f_client.refresh_token.side_effect = _slow_refresh

    t1, t2 = await asyncio.wait_for(
        asyncio.gather(
            ensure_fresh_access_token(
                uid,
                "feishu",
                "calendar",
                feishu_client=f_client,
                waiter_budget_seconds=5.0,
                waiter_poll_interval_seconds=0.05,
            ),
            ensure_fresh_access_token(
                uid,
                "feishu",
                "calendar",
                feishu_client=f_client,
                waiter_budget_seconds=5.0,
                waiter_poll_interval_seconds=0.05,
            ),
        ),
        timeout=15.0,
    )
    assert f_client.refresh_token.await_count == 1
    assert t1.access_token == "fresh-at-pg"
    assert t2.access_token == "fresh-at-pg"
    assert t1.token_generation == 2


async def test_feishu_webhook_revocation_atomic_dedup_and_authorized_at_db(
    gws_live_pool: Any,
) -> None:
    """Real PG: stale event ignored; fresh event atomically deduplicates event_id and purges; replay returns duplicate."""
    _ = gws_live_pool
    _install_creds()
    uid = "user-feishu-wh"
    row = await db.upsert_connection(
        user_id=uid,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou_wh_1",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-wh",
        refresh_token="rt-wh",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )

    stale_ts_ms = str(int((row.authorized_at - timedelta(minutes=30)).timestamp() * 1000))
    stale_res = await process_feishu_integration_event(
        {
            "header": {
                "event_type": FEISHU_REVOKED_EVENT_TYPE,
                "event_id": "evt-db-stale",
                "app_id": "cli_db_test_1",
                "create_time": stale_ts_ms,
            },
            "event": {"open_id": "ou_wh_1", "revoke_reason": "Revoked by user action."},
        }
    )
    assert stale_res.status == "ignored"
    assert stale_res.reason == "stale_before_authorized_at"
    assert await db.get_connection(uid, "feishu", "calendar") is not None

    fresh_ts_ms = str(int((row.authorized_at + timedelta(seconds=5)).timestamp() * 1000))
    ev_payload = {
        "header": {
            "event_type": FEISHU_REVOKED_EVENT_TYPE,
            "event_id": "evt-db-fresh-1",
            "app_id": "cli_db_test_1",
            "create_time": fresh_ts_ms,
        },
        "event": {"open_id": "ou_wh_1", "revoke_reason": "Revoked by user action."},
    }
    ok_res = await process_feishu_integration_event(ev_payload)
    assert ok_res.status == "purged"
    assert len(ok_res.purge_ids) == 1
    assert await db.get_connection(uid, "feishu", "calendar") is None

    # Replay of the exact same event_id returns duplicate
    dup_res = await process_feishu_integration_event(ev_payload)
    assert dup_res.status == "duplicate"
    assert dup_res.reason == "duplicate_event_id"


async def test_health_sweep_multiworker_mutex_and_unready_creds_skip_db(
    gws_live_pool: Any,
) -> None:
    """Real PG: 12h health sweep respects PG advisory lock, interval state, invalid_grant -> needs_reauth, and skips unready creds."""
    async with gws_live_pool.connection() as conn:
        await conn.execute(
            "UPDATE dalton_integration_health_sweep_state SET last_started_at = NULL, last_completed_at = NULL, last_worker_id = NULL"
        )
    # Configure ONLY google_oauth (leave feishu_app unset!)
    _install_creds(feishu_app_id=None, feishu_app_secret=None)
    await db.upsert_connection(
        user_id="u-sw-1",
        provider="google",
        service="calendar",
        oauth_client_id="111222333-alpha.apps.googleusercontent.com",
        provider_account_id="sub-sw-1",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-sw-1",
        refresh_token="rt-sw-1",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    await db.upsert_connection(
        user_id="u-sw-2",
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou-sw-2",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-sw-2",
        refresh_token="rt-sw-2",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )

    g_client = create_autospec(GoogleOAuthClient, instance=True)
    g_client.refresh_token.side_effect = OAuthUpstreamError(
        provider="google", kind="invalid_grant", error_code="invalid_grant", status_code=400
    )

    rep = await run_integration_health_sweep_once(force=True, google_client=g_client)
    assert rep.acquired is True
    assert rep.marked_needs_reauth == 1
    assert rep.skipped_credentials_not_ready == 1

    # Google row marked needs_reauth (NOT revoked_purging), Feishu row untouched (connected)
    g_after = await db.get_connection("u-sw-1", "google", "calendar")
    f_after = await db.get_connection("u-sw-2", "feishu", "calendar")
    assert g_after is not None and g_after.status == IntegrationStatus.NEEDS_REAUTH
    assert f_after is not None and f_after.status == IntegrationStatus.CONNECTED

    # Second sweep within 12 hours without force -> skipped by interval_not_elapsed
    rep2 = await run_integration_health_sweep_once(force=False, google_client=g_client)
    assert rep2.acquired is True
    assert rep2.skipped_reason == "interval_not_elapsed"


async def test_refresh_error_and_narrowed_scope_commit_final_row_and_generation_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Item 1): narrowed scope commits new tokens + generation + needs_reauth; ambiguous & transient commit cleanly."""
    _ = gws_live_pool
    _install_creds()
    uid = "u-refresh-tx-commit"

    # 1) Narrowed scope on refresh: commits newly issued tokens & token_generation=2 with status=needs_reauth
    await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id="111222333-alpha.apps.googleusercontent.com",
        provider_account_id="sub-narrow",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="old-at",
        refresh_token="old-rt",
        expires_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    g_client = create_autospec(GoogleOAuthClient, instance=True)
    g_client.refresh_token.return_value = OAuthTokenResponse(
        access_token="new-at-kept",
        refresh_token="new-rt-kept",
        expires_in=3600,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope=GOOGLE_SCOPE_CALENDAR_LIST_RO,  # missing calendar.events!
    )
    with pytest.raises(IntegrationNeedsReauthError) as narrow_exc:
        await ensure_fresh_access_token(uid, "google", "calendar", google_client=g_client)
    assert narrow_exc.value.code == "missing_required_scopes"
    narrow_row = await db.get_connection(uid, "google", "calendar", decrypt_tokens=True)
    assert narrow_row is not None
    assert narrow_row.status == IntegrationStatus.NEEDS_REAUTH
    assert narrow_row.token_generation == 2
    assert narrow_row.access_token == "new-at-kept"
    assert narrow_row.refresh_token == "new-rt-kept"
    assert narrow_row.refresh_inflight_gen is None

    # 2) Feishu ambiguous refresh (force_refresh=True, token not yet expired) commits refresh_ambiguous
    await db.upsert_connection(
        user_id=uid,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou-amb",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="fs-valid-at",
        refresh_token="fs-rt-1",
        expires_at=datetime.now(UTC) + timedelta(minutes=20),
    )
    f_client = create_autospec(FeishuOAuthClient, instance=True)
    f_client.refresh_token.side_effect = OAuthUpstreamError(
        provider="feishu", kind="ambiguous", error_code="readtimeout"
    )
    with pytest.raises(IntegrationTransientError) as amb_exc:
        await ensure_fresh_access_token(
            uid, "feishu", "calendar", force_refresh=True, feishu_client=f_client
        )
    assert amb_exc.value.code == "refresh_ambiguous"
    amb_row = await db.get_connection(uid, "feishu", "calendar")
    assert amb_row is not None
    assert amb_row.status == IntegrationStatus.REFRESH_AMBIGUOUS
    assert amb_row.refresh_inflight_gen is None

    # 3) Transient error clears refresh_inflight_gen and commits last_error_code while keeping connected
    await db.upsert_connection(
        user_id="u-transient-commit",
        provider="google",
        service="calendar",
        oauth_client_id="111222333-alpha.apps.googleusercontent.com",
        provider_account_id="sub-tr",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-tr",
        refresh_token="rt-tr",
        expires_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    g_client.refresh_token.side_effect = OAuthUpstreamError(
        provider="google", kind="transient", error_code="http_503", status_code=503
    )
    with pytest.raises(IntegrationTransientError):
        await ensure_fresh_access_token(
            "u-transient-commit", "google", "calendar", google_client=g_client
        )
    tr_row = await db.get_connection("u-transient-commit", "google", "calendar")
    assert tr_row is not None
    assert tr_row.status == IntegrationStatus.CONNECTED
    assert tr_row.refresh_inflight_gen is None
    assert tr_row.last_error_code == "http_503"


async def test_feishu_revoked_code_atomic_2a_registration_and_orphan_recovery_db(
    gws_live_pool: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG (Item 2): confirmed revocation sets revoked_purging + ledger atomically; failure rolls back; orphans repaired."""
    async with gws_live_pool.connection() as conn:
        await conn.execute(
            "UPDATE dalton_integration_health_sweep_state SET last_started_at = NULL, last_completed_at = NULL, last_worker_id = NULL"
        )
    _install_creds()
    uid = "u-fs-atomic-rev"
    await db.upsert_connection(
        user_id=uid,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou-rev-atomic",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-rev",
        refresh_token="rt-rev",
        expires_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    f_client = create_autospec(FeishuOAuthClient, instance=True)
    f_client.refresh_token.side_effect = OAuthUpstreamError(
        provider="feishu", kind="revoked", error_code="feishu_confirmed_rev", feishu_code=99999
    )

    # 1) When Phase 1 registration fails, exception is NOT swallowed and row stays connected (not orphaned)
    with monkeypatch.context() as mp:
        mp.setattr(
            oauth_mod,
            "begin_disconnect",
            create_autospec(oauth_mod.begin_disconnect, side_effect=RuntimeError("phase1 db fail")),
        )
        with pytest.raises(RuntimeError, match="phase1 db fail"):
            await ensure_fresh_access_token(uid, "feishu", "calendar", feishu_client=f_client)
    still_conn = await db.get_connection(uid, "feishu", "calendar", decrypt_tokens=True)
    assert still_conn is not None
    assert still_conn.status == IntegrationStatus.CONNECTED
    assert still_conn.refresh_token == "rt-rev"

    # 2) Succeeding Phase 1 atomically commits revoked_purging + purge ledger and runs 2A purge
    with pytest.raises(IntegrationRevokedError):
        await ensure_fresh_access_token(uid, "feishu", "calendar", feishu_client=f_client)
    assert await db.get_connection(uid, "feishu", "calendar") is None
    ledgers = await db.list_user_purge_ledgers(uid, active_only=False)
    assert any(
        l.trigger_reason == TriggerReason.ERROR_CODE_REVOKED
        and l.local_purge_status == LocalPurgeStatus.COMPLETED
        for l in ledgers
    )

    # 3) Orphaned revoked_purging row with NO running ledger is repaired by health sweep AND by ensure_fresh_access_token
    uid_orphan_sw = "u-orphan-sweep"
    await db.upsert_connection(
        user_id=uid_orphan_sw,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou-orphan-1",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token=None,
        refresh_token=None,
        status=IntegrationStatus.REVOKED_PURGING,
    )
    rep = await run_integration_health_sweep_once(force=True)
    assert rep.triggered_revocation_purge >= 1
    assert await db.get_connection(uid_orphan_sw, "feishu", "calendar") is None

    uid_orphan_ref = "u-orphan-refresh"
    await db.upsert_connection(
        user_id=uid_orphan_ref,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou-orphan-2",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token=None,
        refresh_token=None,
        status=IntegrationStatus.REVOKED_PURGING,
    )
    with pytest.raises(IntegrationRevokedError):
        await ensure_fresh_access_token(uid_orphan_ref, "feishu", "calendar", feishu_client=f_client)
    assert await db.get_connection(uid_orphan_ref, "feishu", "calendar") is None


async def test_feishu_event_race_with_reauth_and_multi_user_atomic_replay_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Items 4 & 5): same-client reauth updates authorized_at/epoch; multi-user single-tx & partial replay."""
    _ = gws_live_pool
    _install_creds()

    # 1) Multi-user atomic batch: 2 Dalton users bound to same Feishu open_id -> both purged in ONE tx
    u1, u2 = "u-multi-a", "u-multi-b"
    r1 = await db.upsert_connection(
        user_id=u1,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou_shared_multi",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-1",
        refresh_token="rt-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await db.upsert_connection(
        user_id=u2,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou_shared_multi",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-2",
        refresh_token="rt-2",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    ev_ts_ms = str(int((r1.authorized_at + timedelta(seconds=10)).timestamp() * 1000))
    res_both = await process_feishu_integration_event(
        {
            "header": {
                "event_type": FEISHU_REVOKED_EVENT_TYPE,
                "event_id": "evt-multi-both-1",
                "app_id": "cli_db_test_1",
                "create_time": ev_ts_ms,
            },
            "event": {"open_id": "ou_shared_multi", "revoke_reason": "Revoked by user action."},
        }
    )
    assert res_both.status == "purged"
    assert len(res_both.purge_ids) == 2
    assert await db.get_connection(u1, "feishu", "calendar") is None
    assert await db.get_connection(u2, "feishu", "calendar") is None

    # 2) Replay when event_id was already recorded and user A has a running ledger, user B lacks one -> skips A, registers B
    u3, u4 = "u-replay-a", "u-replay-b"
    r3 = await db.upsert_connection(
        user_id=u3,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou_replay_multi",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-3",
        refresh_token="rt-3",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    r4 = await db.upsert_connection(
        user_id=u4,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou_replay_multi",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-4",
        refresh_token="rt-4",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await db.record_webhook_event("feishu", "evt-partial-replay-1")
    await db.insert_purge_ledger(
        purge_id=str(uuid.uuid4()),
        user_id=u3,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou_replay_multi",
        auth_epoch_upto=r3.auth_epoch,
        trigger_reason=TriggerReason.WEBHOOK_REVOKED,
        local_purge_status=LocalPurgeStatus.RUNNING,
        purged_max_turn_by_thread={},
        remote_revoke_status=RemoteRevokeStatus.NOT_APPLICABLE,
    )
    ev_replay_ts = str(int((r4.authorized_at + timedelta(seconds=10)).timestamp() * 1000))
    res_replay = await process_feishu_integration_event(
        {
            "header": {
                "event_type": FEISHU_REVOKED_EVENT_TYPE,
                "event_id": "evt-partial-replay-1",
                "app_id": "cli_db_test_1",
                "create_time": ev_replay_ts,
            },
            "event": {"open_id": "ou_replay_multi", "revoke_reason": "Revoked by user action."},
        },
        execute_purge=False,
    )
    assert res_replay.status == "purged"
    assert len(res_replay.purge_ids) == 1
    u3_ledgers = await db.list_user_purge_ledgers(u3, active_only=True)
    u4_ledgers = await db.list_user_purge_ledgers(u4, active_only=True)
    assert len(u3_ledgers) == 1  # untouched
    assert len(u4_ledgers) == 1  # registered on replay!

    # 3) Same-client Feishu re-authorization updates authorized_at and bumps auth_epoch so older event is skipped
    u5 = "u-feishu-reauth"
    initial_row = await db.upsert_connection(
        user_id=u5,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou_reauth_race",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-old",
        refresh_token="rt-old",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    old_event_ts_ms = str(int((initial_row.authorized_at + timedelta(milliseconds=10)).timestamp() * 1000))
    await asyncio.sleep(0.05)
    reauthed, _ = await oauth_mod._finalize_connection(
        user_id=u5,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou_reauth_race",
        provider_union_id=None,
        provider_email="u5@example.com",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-fresh",
        refresh_token="rt-fresh",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        preferences={},
        allow_account_switch=False,
    )
    assert reauthed.auth_epoch > initial_row.auth_epoch
    assert reauthed.authorized_at > initial_row.authorized_at
    stale_after_reauth = await process_feishu_integration_event(
        {
            "header": {
                "event_type": FEISHU_REVOKED_EVENT_TYPE,
                "event_id": "evt-race-stale-1",
                "app_id": "cli_db_test_1",
                "create_time": old_event_ts_ms,
            },
            "event": {"open_id": "ou_reauth_race", "revoke_reason": "Revoked by user action."},
        }
    )
    assert stale_after_reauth.status == "ignored"
    assert stale_after_reauth.reason == "stale_before_authorized_at"
    assert await db.get_connection(u5, "feishu", "calendar") is not None


async def test_same_project_dual_service_rotation_no_revoke_retryable_queue_and_commit_supersede_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Item 7): same-project rotation with active new client skips revoke; RETRYABLE queues then supersedes on commit."""
    _ = gws_live_pool
    uid = LOCAL_DEV_USER_ID
    client_old = "111222333-old.apps.googleusercontent.com"
    client_new = "111222333-new.apps.googleusercontent.com"
    _install_creds(google_client_id=client_new)

    # Case A: Calendar already rebound to client_new; Gmail still on client_old -> starting Gmail OAuth MUST NOT revoke!
    await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_new,
        provider_account_id="sub-dual",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-cal-new",
        refresh_token="rt-cal-new",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        status=IntegrationStatus.CONNECTED,
    )
    await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="mail",
        oauth_client_id=client_old,
        provider_account_id="sub-dual",
        granted_scopes=" ".join(GOOGLE_GMAIL_MIN_SCOPES),
        access_token="at-mail-old",
        refresh_token="rt-mail-old",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        status=IntegrationStatus.NEEDS_REAUTH,
    )
    revoker = create_autospec(RemoteRevoker, instance=True)
    revoker.revoke.return_value = RevokeOutcome.SUCCEEDED

    await start_oauth_authorization(
        user_id=uid,
        provider="google",
        service="mail",
        auth_mode="oss",
        revoker=revoker,
    )
    assert revoker.revoke.await_count == 0
    ledgers_a = await db.list_user_purge_ledgers(uid, active_only=False)
    assert any(
        l.oauth_client_id == client_old
        and l.remote_revoke_status == RemoteRevokeStatus.SUPERSEDED
        for l in ledgers_a
    )

    # Case B: Sole service on old client (distinct project so Case A's unconsumed state does not defer),
    # revoker returns RETRYABLE -> queued in retry queue -> commit supersedes under (project, sub) lock
    client_old_b = "222333444-alpha.apps.googleusercontent.com"
    client_new_b = "222333444-beta.apps.googleusercontent.com"
    _install_creds(google_client_id=client_new_b)
    uid_b = "u-rot-retryable"
    await db.upsert_connection(
        user_id=uid_b,
        provider="google",
        service="calendar",
        oauth_client_id=client_old_b,
        provider_account_id="sub-solo",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-solo-old",
        refresh_token="rt-solo-old",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        status=IntegrationStatus.NEEDS_REAUTH,
    )
    revoker_retry = create_autospec(RemoteRevoker, instance=True)
    revoker_retry.revoke.return_value = RevokeOutcome.RETRYABLE

    await start_oauth_authorization(
        user_id=uid_b,
        provider="google",
        service="calendar",
        auth_mode="oss",
        revoker=revoker_retry,
    )
    assert revoker_retry.revoke.await_count == 1
    active_b = await db.list_active_revokes_for_google_project_sub(
        "222333444", "sub-solo", user_id=uid_b, decrypt_revoke_token=True
    )
    assert len(active_b) == 1
    assert active_b[0].remote_revoke_status == RemoteRevokeStatus.FAILED_RETRYABLE
    assert active_b[0].revoke_token == "rt-solo-old"

    # Committing the new grant supersedes that retryable ledger atomically under revoke_lock
    await oauth_mod._finalize_connection(
        user_id=uid_b,
        provider="google",
        service="calendar",
        oauth_client_id=client_new_b,
        provider_account_id="sub-solo",
        provider_union_id=None,
        provider_email="solo@example.com",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-solo-new",
        refresh_token="rt-solo-new",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        preferences={},
        allow_account_switch=False,
        revoker=revoker_retry,
    )
    after_b = await db.get_purge_ledger(active_b[0].purge_id, decrypt_revoke_token=True)
    assert after_b is not None
    assert after_b.remote_revoke_status == RemoteRevokeStatus.SUPERSEDED
    assert after_b.revoke_token is None


async def test_interleaved_account_switch_confirmations_bound_to_old_epoch_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Item 8): interleaved switch confirmations cannot overwrite a newer account."""
    _ = gws_live_pool
    uid = LOCAL_DEV_USER_ID
    client_a = "111222333-alpha.apps.googleusercontent.com"
    _install_creds(google_client_id=client_a)

    g_client = create_autospec(GoogleOAuthClient, instance=True)
    g_client.revoke.return_value = RevokeOutcome.SUCCEEDED

    def _mock_google(sub: str, email: str, rt: str) -> None:
        g_client.exchange_code.return_value = OAuthTokenResponse(
            access_token=f"at-{sub}",
            refresh_token=rt,
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token=f"id-{sub}",
        )
        g_client.verify_id_token.return_value = GoogleIdTokenClaims(
            sub=sub,
            email=email,
            email_verified=True,
            aud=client_a,
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )

    await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_a,
        provider_account_id="sub-a",
        provider_email="a@example.com",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-a",
        refresh_token="rt-a",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    # Stage ticket B and ticket C both against Account A
    _mock_google("sub-b", "b@example.com", "rt-b")
    s_b = await start_oauth_authorization(
        user_id=uid, provider="google", service="calendar", auth_mode="oss", switch_account=True, revoker=g_client
    )
    cb_b = await handle_oauth_callback(
        provider="google", code="cb", state=s_b.state, google_client=g_client, revoker=g_client
    )
    assert cb_b.status == "account_switch_confirmation_required" and cb_b.confirmation_token

    _mock_google("sub-c", "c@example.com", "rt-c")
    s_c = await start_oauth_authorization(
        user_id=uid, provider="google", service="calendar", auth_mode="oss", switch_account=True, revoker=g_client
    )
    cb_c = await handle_oauth_callback(
        provider="google", code="cc", state=s_c.state, google_client=g_client, revoker=g_client
    )
    assert cb_c.status == "account_switch_confirmation_required" and cb_c.confirmation_token

    # Confirm C first -> switches A -> C
    conf_c = await confirm_pending_integration(
        user_id=uid, confirmation_token=cb_c.confirmation_token, confirm_account_switch=True, revoker=g_client
    )
    assert conf_c.status == "connected"
    assert conf_c.connection is not None and conf_c.connection.provider_account_id == "sub-c"

    # Now confirming stale ticket B MUST fail (409) and void ticket B while keeping Account C intact!
    with pytest.raises(PendingConfirmationInvalidError) as stale_exc:
        await confirm_pending_integration(
            user_id=uid, confirmation_token=cb_b.confirmation_token, confirm_account_switch=True, revoker=g_client
        )
    assert stale_exc.value.code == "account_switch_state_changed"
    cur_conn = await db.get_connection(uid, "google", "calendar")
    assert cur_conn is not None and cur_conn.provider_account_id == "sub-c"


async def test_local_mode_browser_binding_and_sweep_shared_credential_grouping_db(
    gws_live_pool: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG (Items 9 & 11): local browser cookie binding; sweep groups Google siblings into 1 refresh; force_refresh reuses newer gen."""
    async with gws_live_pool.connection() as conn:
        await conn.execute(
            "UPDATE dalton_integration_health_sweep_state SET last_started_at = NULL, last_completed_at = NULL, last_worker_id = NULL"
        )
    _install_creds()
    uid = "u-local-browser"
    client_a = "111222333-alpha.apps.googleusercontent.com"

    # 1) Local mode browser binding cookie (Item 9)
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setattr(
        oauth_mod,
        "verify_local_session_active",
        create_autospec(oauth_mod.verify_local_session_active, return_value=None),
    )
    g_client = create_autospec(GoogleOAuthClient, instance=True)
    g_client.exchange_code.return_value = OAuthTokenResponse(
        access_token="at-loc-1",
        refresh_token="rt-loc-1",
        expires_in=3600,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        id_token="id-loc",
    )
    g_client.verify_id_token.return_value = GoogleIdTokenClaims(
        sub="sub-loc",
        email="loc@example.com",
        email_verified=True,
        aud=client_a,
        iss="https://accounts.google.com",
        exp=int(time.time()) + 3600,
    )

    started = await start_oauth_authorization(
        user_id=uid,
        provider="google",
        service="calendar",
        auth_mode="local",
        session_id="sid-1",
        revoker=g_client,
    )
    assert started.browser_binding_cookie

    # Different browser (no cookie / wrong cookie) -> confirmation_required, NOT immediately connected
    cb_other = await handle_oauth_callback(
        provider="google",
        code="c-other",
        state=started.state,
        browser_binding_token="wrong-browser-cookie",
        google_client=g_client,
        revoker=g_client,
    )
    assert cb_other.status == "confirmation_required"
    assert cb_other.confirmation_token
    assert await db.get_connection(uid, "google", "calendar") is None

    # Authenticated confirmation activates the connection
    conf = await confirm_pending_integration(
        user_id=uid, confirmation_token=cb_other.confirmation_token, revoker=g_client
    )
    assert conf.status == "connected"
    assert await db.get_connection(uid, "google", "calendar") is not None

    # 2) Sweep groups Google calendar + mail sharing (client, sub) into ONE upstream refresh (Item 11)
    await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="mail",
        oauth_client_id=client_a,
        provider_account_id="sub-loc",
        granted_scopes=" ".join(GOOGLE_GMAIL_MIN_SCOPES),
        access_token="at-loc-1",
        refresh_token="rt-loc-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    g_client.refresh_token.return_value = OAuthTokenResponse(
        access_token="at-swept-once",
        refresh_token="rt-swept-once",
        expires_in=3600,
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        scope=" ".join((*GOOGLE_CALENDAR_MIN_SCOPES, *GOOGLE_GMAIL_MIN_SCOPES)),
    )
    rep = await run_integration_health_sweep_once(force=True, google_client=g_client)
    assert rep.acquired is True
    assert g_client.refresh_token.await_count == 1
    cal_after = await db.get_connection(uid, "google", "calendar", decrypt_tokens=True)
    mail_after = await db.get_connection(uid, "google", "mail", decrypt_tokens=True)
    assert cal_after is not None and cal_after.access_token == "at-swept-once"
    assert mail_after is not None and mail_after.access_token == "at-swept-once"



# ---------------------------------------------------------------------------
# Round 3 Real PostgreSQL Integration Tests (Codex High + Medium items)
# ---------------------------------------------------------------------------


async def test_r3_google_callback_exchange_and_revoke_interleaved_two_connections_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Task 7 + Lead §1 + Codex High #1): Google revoke defers during active state AND in-flight exchange_code without holding PG locks across HTTP; concurrent disconnect creating a new ledger mid-exchange also defers then gets superseded."""
    from src.dalton.services.integration_revoke import process_revoke
    from src.dalton.services.user_integrations_lifecycle import (
        begin_disconnect,
        finish_disconnect,
    )

    _ = gws_live_pool
    uid = LOCAL_DEV_USER_ID
    client_old = "444555666-old.apps.googleusercontent.com"
    client_new = "444555666-new.apps.googleusercontent.com"
    _install_creds(google_client_id=client_new)

    # Seed old connection on client_old; pre-auth rotation revoke returns RETRYABLE -> queues FAILED_RETRYABLE
    await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_old,
        provider_account_id="sub-race",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-old",
        refresh_token="rt-old-race",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        status=IntegrationStatus.NEEDS_REAUTH,
    )
    rev_retry = create_autospec(RemoteRevoker, instance=True)
    rev_retry.revoke.return_value = RevokeOutcome.RETRYABLE

    started = await start_oauth_authorization(
        user_id=uid,
        provider="google",
        service="calendar",
        auth_mode="oss",
        revoker=rev_retry,
    )
    active_ledgers = await db.list_active_revokes_for_google_project_sub(
        "444555666", "sub-race", user_id=uid, decrypt_revoke_token=True
    )
    assert len(active_ledgers) == 1
    retry_pid = active_ledgers[0].purge_id

    # Case A: Background process_revoke runs while oauth_states is active -> MUST defer without calling revoker!
    rev_bg = create_autospec(RemoteRevoker, instance=True)
    rev_bg.revoke.return_value = RevokeOutcome.SUCCEEDED
    deferred_st = await asyncio.wait_for(process_revoke(retry_pid, rev_bg), timeout=10.0)
    assert deferred_st == RemoteRevokeStatus.FAILED_RETRYABLE
    assert rev_bg.revoke.await_count == 0

    # Also seed a mail connection on client_old to test concurrent disconnect creating a BRAND-NEW ledger mid-exchange!
    await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="mail",
        oauth_client_id=client_old,
        provider_account_id="sub-race",
        granted_scopes=" ".join(GOOGLE_GMAIL_MIN_SCOPES),
        access_token="at-old-mail",
        refresh_token="rt-old-mail",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        status=IntegrationStatus.CONNECTED,
    )

    # Case B: handle_oauth_callback marks in-flight exchange in DB (WITHOUT holding PG advisory lock across exchange_code).
    # While exchange_code is paused:
    #   1. process_revoke(retry_pid) returns FAILED_RETRYABLE immediately (defers, does NOT hang or call revoker!)
    #   2. Concurrent disconnect of mail creates a brand-new PENDING purge ledger + runs finish_disconnect ->
    #      its process_revoke ALSO sees the in-flight oauth_state record and defers without calling revoker!
    #   3. After exchange_code completes, BOTH ledgers are atomically SUPERSEDED with ciphertext wiped!
    exchange_entered = asyncio.Event()
    exchange_release = asyncio.Event()
    g_client = create_autospec(GoogleOAuthClient, instance=True)

    async def _slow_exchange(**_kw: Any) -> OAuthTokenResponse:
        exchange_entered.set()
        await asyncio.wait_for(exchange_release.wait(), timeout=10.0)
        return OAuthTokenResponse(
            access_token="at-new-safe",
            refresh_token="rt-new-safe",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token="id-new-safe",
        )

    g_client.exchange_code.side_effect = _slow_exchange
    g_client.verify_id_token.return_value = GoogleIdTokenClaims(
        sub="sub-race",
        email="race@example.com",
        email_verified=True,
        aud=client_new,
        iss="https://accounts.google.com",
        exp=int(time.time()) + 3600,
    )

    cb_task = asyncio.create_task(
        handle_oauth_callback(
            provider="google",
            code="code-race",
            state=started.state,
            google_client=g_client,
            revoker=rev_bg,
        )
    )
    try:
        entered_wait = asyncio.create_task(exchange_entered.wait())
        done, _pending = await asyncio.wait(
            {cb_task, entered_wait},
            timeout=10.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cb_task in done:
            entered_wait.cancel()
            # Fail fast with the real callback exception/result instead of hanging!
            cb_early = cb_task.result()
            pytest.fail(f"Callback exited before exchange_code: {cb_early!r}")
        assert exchange_entered.is_set(), "Timed out waiting for exchange_code to start"

        # 1. Concurrent disconnect registers a brand-new ledger for mail during exchange_code ->
        #    deletes the active mail row, sees in-flight exchange -> defers without calling revoker!
        disc_pid = await asyncio.wait_for(
            begin_disconnect(uid, "google", "mail"),
            timeout=10.0,
        )
        disc_fin = await asyncio.wait_for(
            finish_disconnect(disc_pid, revoker=rev_bg),
            timeout=10.0,
        )
        assert disc_fin is not None
        assert disc_fin.local_purge_status == LocalPurgeStatus.COMPLETED
        assert disc_fin.remote_revoke_status == RemoteRevokeStatus.FAILED_RETRYABLE
        assert rev_bg.revoke.await_count == 0

        # 2. Existing retryable ledger (now with no active mail row) also sees in-flight exchange ->
        #    defers immediately (FAILED_RETRYABLE, no deadlock, 0 upstream calls)!
        rev_mid = await asyncio.wait_for(process_revoke(retry_pid, rev_bg), timeout=10.0)
        assert rev_mid == RemoteRevokeStatus.FAILED_RETRYABLE
        assert rev_bg.revoke.await_count == 0

        # 3. Release exchange_code -> callback finishes and supersedes BOTH ledgers!
        exchange_release.set()
        cb_res = await asyncio.wait_for(cb_task, timeout=10.0)
    finally:
        exchange_release.set()
        if not cb_task.done():
            cb_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cb_task

    assert cb_res.status == "connected"
    assert rev_bg.revoke.await_count == 0  # Old token was NEVER revoked during or after new token issued!

    for check_pid in (retry_pid, disc_pid):
        final_ledger = await db.get_purge_ledger(check_pid, decrypt_revoke_token=True)
        assert final_ledger is not None
        assert final_ledger.remote_revoke_status == RemoteRevokeStatus.SUPERSEDED
        assert final_ledger.revoke_token is None

    # Follow-up process_revoke on either ledger sees SUPERSEDED and never calls revoker
    assert await asyncio.wait_for(process_revoke(retry_pid, rev_bg), timeout=10.0) == RemoteRevokeStatus.SUPERSEDED
    assert await asyncio.wait_for(process_revoke(disc_pid, rev_bg), timeout=10.0) == RemoteRevokeStatus.SUPERSEDED
    assert rev_bg.revoke.await_count == 0


async def test_r3_account_switch_confirm_race_deleted_or_same_target_overwritten_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Task 8): account-switch confirmation fails 409 in normal commit branch if old row was deleted or another ticket already switched to same target."""
    _ = gws_live_pool
    uid = "u-r3-switch-race"
    client_a = "111222333-alpha.apps.googleusercontent.com"
    _install_creds(google_client_id=client_a)

    # Race A: old row sub-a (epoch E1) was deleted after pre-check -> normal commit branch MUST reject!
    row_a = await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_a,
        provider_account_id="sub-a",
        provider_email="a@example.com",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-a",
        refresh_token="rt-a",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await db.delete_connection_upto_epoch(uid, "google", "calendar", row_a.auth_epoch)
    assert await db.get_connection(uid, "google", "calendar") is None

    with pytest.raises(PendingConfirmationInvalidError) as exc_del:
        await oauth_mod._finalize_connection(
            user_id=uid,
            provider="google",
            service="calendar",
            oauth_client_id=client_a,
            provider_account_id="sub-b",
            provider_union_id=None,
            provider_email="b@example.com",
            granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            access_token="at-b",
            refresh_token="rt-b",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            preferences={},
            allow_account_switch=True,
            expected_old_oauth_client_id=client_a,
            expected_old_provider_account_id="sub-a",
            expected_old_auth_epoch=row_a.auth_epoch,
        )
    assert exc_del.value.code == "account_switch_state_changed"
    assert await db.get_connection(uid, "google", "calendar") is None

    # Race B: another ticket already switched sub-a (epoch E2) -> sub-b (epoch E3, at-b-winner);
    # stale ticket targeting sub-b MUST NOT overwrite via normal reauth branch!
    row_a2 = await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_a,
        provider_account_id="sub-a",
        provider_email="a@example.com",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-a2",
        refresh_token="rt-a2",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    winner_b = await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_a,
        provider_account_id="sub-b",
        provider_email="b@example.com",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-b-winner",
        refresh_token="rt-b-winner",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        bump_auth_epoch=True,
    )
    with pytest.raises(PendingConfirmationInvalidError) as exc_ov:
        await oauth_mod._finalize_connection(
            user_id=uid,
            provider="google",
            service="calendar",
            oauth_client_id=client_a,
            provider_account_id="sub-b",
            provider_union_id=None,
            provider_email="b@example.com",
            granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            access_token="at-b-stale-loser",
            refresh_token="rt-b-stale-loser",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            preferences={},
            allow_account_switch=True,
            expected_old_oauth_client_id=client_a,
            expected_old_provider_account_id="sub-a",
            expected_old_auth_epoch=row_a2.auth_epoch,
        )
    assert exc_ov.value.code == "account_switch_state_changed"
    kept = await db.get_connection(uid, "google", "calendar", decrypt_tokens=True)
    assert kept is not None
    assert kept.auth_epoch == winner_b.auth_epoch
    assert kept.access_token == "at-b-winner"


async def test_r3_feishu_new_account_revocation_during_old_account_running_purge_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Task 5): Feishu new account self-revocation is NOT blocked by old account's running account_switch purge ledger."""
    from src.dalton.services.user_integrations_lifecycle import replace_connection_for_account_switch

    _ = gws_live_pool
    _install_creds()
    uid = "u-r3-feishu-switch-rev"
    await db.upsert_connection(
        user_id=uid,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou_old_acct",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-old",
        refresh_token="rt-old",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )

    # Account switch activates ou_new_acct and leaves ou_old_acct's purge ledger in local_purge_status='running'
    sw = await replace_connection_for_account_switch(
        uid,
        "feishu",
        "calendar",
        new_oauth_client_id="cli_db_test_1",
        new_provider_account_id="ou_new_acct",
        new_granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        new_access_token="at-new",
        new_refresh_token="rt-new",
        new_expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    old_ledger = await db.get_purge_ledger(sw.purge_id)
    assert old_ledger is not None
    assert old_ledger.local_purge_status == LocalPurgeStatus.RUNNING
    assert old_ledger.provider_account_id == "ou_old_acct"

    new_row = await db.get_connection(uid, "feishu", "calendar")
    assert new_row is not None and new_row.provider_account_id == "ou_new_acct"

    # While ou_old_acct's purge is STILL running, ou_new_acct revokes authorization!
    rev_ts_ms = str(int((new_row.authorized_at + timedelta(seconds=5)).timestamp() * 1000))
    ev_payload = {
        "header": {
            "event_type": FEISHU_REVOKED_EVENT_TYPE,
            "event_id": "evt-r3-new-acct-rev-1",
            "app_id": "cli_db_test_1",
            "create_time": rev_ts_ms,
        },
        "event": {"open_id": "ou_new_acct", "revoke_reason": "Revoked by user action."},
    }
    res = await process_feishu_integration_event(ev_payload, execute_purge=False)
    assert res.status == "purged"
    assert len(res.purge_ids) == 1
    assert res.purge_ids[0] != sw.purge_id

    revoked_row = await db.get_connection(uid, "feishu", "calendar")
    assert revoked_row is not None and revoked_row.status == IntegrationStatus.REVOKED_PURGING

    # Replay of the exact same ou_new_acct event is deduplicated (matched by ou_new_acct + epoch)
    replay_res = await process_feishu_integration_event(ev_payload, execute_purge=False)
    assert replay_res.status == "duplicate"


async def test_r3_interrupted_full_disconnect_resume_local_first_and_due_revokes_wait_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Task 10): list_due_remote_revokes waits for all local purges in the batch; resume finishes all local purges before remote revoke."""
    from src.dalton.services.integration_data_purge import run_personal_purge_resume_once
    from src.dalton.services.integration_revoke import run_due_remote_revokes_once
    from src.dalton.services.user_integrations_lifecycle import begin_disconnect_all_services

    pool = gws_live_pool
    uid = "u-r3-full-disc-resume"
    client_id = "777888999-disc.apps.googleusercontent.com"
    _install_creds(google_client_id=client_id)

    r_cal = await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-full",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-full",
        refresh_token="rt-full",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    r_mail = await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="mail",
        oauth_client_id=client_id,
        provider_account_id="sub-full",
        granted_scopes=" ".join(GOOGLE_GMAIL_MIN_SCOPES),
        access_token="at-full",
        refresh_token="rt-full",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    ws_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, email, name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (uid, "full@example.com", "Full"),
        )
        await conn.execute(
            "INSERT INTO workspaces (workspace_id, user_id, name, status) VALUES (%s::uuid, %s, 'Home', 'running')",
            (ws_id, uid),
        )
        await conn.execute(
            "INSERT INTO conversation_threads (conversation_thread_id, workspace_id, user_id, current_status, msg_type, thread_index, title) "
            "VALUES (%s::uuid, %s::uuid, %s, 'completed', 'ptc', 1, 'Shared Thread')",
            (thread_id, ws_id, uid),
        )
    resp_id = await _seed_turn(pool, thread_id=thread_id, turn_index=0, user_text="Shared cal+mail turn")
    async with pool.connection() as conn:
        await db.record_touch(
            thread_id=thread_id, response_id=resp_id, provider="google", service="calendar",
            auth_epoch=r_cal.auth_epoch, turn_index=0, user_id=uid, provider_account_id="sub-full", conn=conn,
        )
        await db.record_touch(
            thread_id=thread_id, response_id=resp_id, provider="google", service="mail",
            auth_epoch=r_mail.auth_epoch, turn_index=0, user_id=uid, provider_account_id="sub-full", conn=conn,
        )

    # Simulate interrupted DELETE /google right after Phase 1
    pids = await begin_disconnect_all_services(uid, "google")
    assert len(pids) == 2

    # While local purges are still running, list_due_remote_revokes MUST NOT return this user's pending revoke
    due_before = [r for r in await db.list_due_remote_revokes() if r.user_id == uid]
    assert due_before == []

    # Production resume (revoker=None) completes ALL local purges and unblocks the shared thread without calling remote revoke
    resumed = await run_personal_purge_resume_once(revoker=None)
    assert set(pids).issubset(set(resumed))

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT personal_purge_in_progress FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
                (thread_id,),
            )
            t_flag = await cur.fetchone()
            assert t_flag is not None and t_flag[0] is False

    # Now that ALL local purges finished, list_due_remote_revokes surfaces the pending remote revoke!
    due_after = [r for r in await db.list_due_remote_revokes() if r.user_id == uid]
    assert len(due_after) == 1

    # Remote retry loop executes it cleanly
    rev_ok = create_autospec(RemoteRevoker, instance=True)
    rev_ok.revoke.return_value = RevokeOutcome.SUCCEEDED
    outcomes = await run_due_remote_revokes_once(rev_ok)
    assert outcomes[due_after[0].purge_id] == RemoteRevokeStatus.SUCCEEDED
    assert rev_ok.revoke.await_count == 1


async def test_r3_feishu_crash_takeover_and_google_sibling_actual_scopes_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Feishu crash recovery & Google actual scopes): crash takeover never replays refresh token; Google sibling recomputes scopes/status."""
    pool = gws_live_pool
    _install_creds()

    # Part A: Feishu crash takeover with unexpired access token
    uid_fs = "u-r3-feishu-crash"
    await db.upsert_connection(
        user_id=uid_fs,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou_crash_db",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-valid-fs",
        refresh_token="rt-maybe-used",
        expires_at=datetime.now(UTC) + timedelta(minutes=20),
    )
    async with pool.connection() as conn:
        await conn.execute(
            """
            UPDATE dalton_user_integrations
            SET refresh_inflight_gen = token_generation, refresh_started_at = NOW()
            WHERE user_id = %s AND provider = 'feishu' AND service = 'calendar'
            """,
            (uid_fs,),
        )
    f_client = create_autospec(FeishuOAuthClient, instance=True)
    with pytest.raises(IntegrationTransientError) as exc_amb:
        await ensure_fresh_access_token(
            uid_fs, "feishu", "calendar", force_refresh=True, feishu_client=f_client
        )
    assert exc_amb.value.code == "refresh_ambiguous"
    assert f_client.refresh_token.await_count == 0
    amb_row = await db.get_connection(uid_fs, "feishu", "calendar", decrypt_tokens=True)
    assert amb_row is not None
    assert amb_row.status == IntegrationStatus.REFRESH_AMBIGUOUS
    assert amb_row.refresh_inflight_gen is None
    assert amb_row.access_token == "at-valid-fs"
    assert amb_row.refresh_token is None  # Codex Med #2: possibly-consumed refresh token wiped!

    # Non-force call before expiry returns the still-valid access token with 0 upstream calls
    tok = await ensure_fresh_access_token(
        uid_fs, "feishu", "calendar", min_remaining_seconds=3600, feishu_client=f_client
    )
    assert tok.access_token == "at-valid-fs"
    assert f_client.refresh_token.await_count == 0

    # Once access token expires on the SAME row, converges directly to NEEDS_REAUTH with 0 upstream calls!
    async with pool.connection() as conn:
        await conn.execute(
            """
            UPDATE dalton_user_integrations
            SET expires_at = NOW() - INTERVAL '10 seconds'
            WHERE user_id = %s AND provider = 'feishu' AND service = 'calendar'
            """,
            (uid_fs,),
        )
    with pytest.raises(IntegrationNeedsReauthError):
        await ensure_fresh_access_token(
            uid_fs, "feishu", "calendar", feishu_client=f_client
        )
    assert f_client.refresh_token.await_count == 0
    expired_row = await db.get_connection(uid_fs, "feishu", "calendar", decrypt_tokens=True)
    assert expired_row is not None
    assert expired_row.status == IntegrationStatus.NEEDS_REAUTH
    assert expired_row.access_token is None
    assert expired_row.refresh_token is None

    # Part B: Google sibling row gets actual granted scopes (no union) and recomputes status & preferences
    uid_g = "u-r3-google-scopes"
    client_g = "111222333-alpha.apps.googleusercontent.com"
    await db.upsert_connection(
        user_id=uid_g,
        provider="google",
        service="calendar",
        oauth_client_id=client_g,
        provider_account_id="sub-same",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-old-g",
        refresh_token="rt-old-g",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await db.upsert_connection(
        user_id=uid_g,
        provider="google",
        service="mail",
        oauth_client_id=client_g,
        provider_account_id="sub-same",
        granted_scopes=f"{GOOGLE_GMAIL_MIN_SCOPES[0]} https://www.googleapis.com/auth/gmail.drafts.create",
        access_token="at-old-g",
        refresh_token="rt-old-g",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        preferences={"allow_create_draft": True},
    )
    await oauth_mod._finalize_connection(
        user_id=uid_g,
        provider="google",
        service="calendar",
        oauth_client_id=client_g,
        provider_account_id="sub-same",
        provider_union_id=None,
        provider_email="same@example.com",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-new-cal-only",
        refresh_token="rt-new-cal-only",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        preferences={},
        allow_account_switch=False,
    )
    mail_after = await db.get_connection(uid_g, "google", "mail")
    assert mail_after is not None
    assert mail_after.status == IntegrationStatus.NEEDS_REAUTH
    assert mail_after.last_error_code == "missing_required_scopes"
    assert GOOGLE_GMAIL_MIN_SCOPES[0] not in mail_after.granted_scopes
    assert mail_after.preferences.get("allow_create_draft") is False


# ---------------------------------------------------------------------------
# Round 4 Real PostgreSQL Integration Tests
# ---------------------------------------------------------------------------


async def test_r4_google_disconnect_and_expiry_of_pending_confirmation_revokes_staged_token_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Grok Medium #2): disconnecting while a Supabase pending confirmation exists voids it & revokes its token; expiring pending confirmation enqueues revoke ledger."""
    from src.dalton.services.integration_revoke import run_due_remote_revokes_once
    from src.dalton.services.user_integrations_lifecycle import (
        begin_disconnect,
        begin_disconnect_all_services,
        finish_disconnect,
    )

    _ = gws_live_pool
    client_id = "666777888-pend.apps.googleusercontent.com"
    _install_creds(google_client_id=client_id)

    # Part A: Single-service disconnect while a pending confirmation exists for the SAME (client, sub)
    uid1 = "u-r4-pend-single"
    await db.upsert_connection(
        user_id=uid1,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-p1",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-old-p1",
        refresh_token="rt-old-p1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await db.insert_pending_confirmation(
        confirmation_hash=hashlib.sha256(b"conf-p1").hexdigest(),
        user_id=uid1,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-p1",
        provider_union_id=None,
        provider_email="p1@example.com",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-staged-p1",
        refresh_token="rt-staged-p1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        intent="reauth",
        preferences={},
        confirm_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    rev1 = create_autospec(RemoteRevoker, instance=True)
    rev1.revoke.return_value = RevokeOutcome.SUCCEEDED
    pid1 = await begin_disconnect(uid1, "google", "calendar")
    fin1 = await finish_disconnect(pid1, revoker=rev1)
    assert fin1 is not None
    assert fin1.remote_revoke_status == RemoteRevokeStatus.SUCCEEDED
    rev1.revoke.assert_awaited_once_with(
        provider="google", oauth_client_id=client_id, token="rt-staged-p1"
    )
    assert await db.peek_pending_confirmation(hashlib.sha256(b"conf-p1").hexdigest(), uid1) is None

    # Part B: Full disconnect while pending confirmation exists -> voids pending confirmation & revokes!
    uid2 = "u-r4-pend-full"
    await db.upsert_connection(
        user_id=uid2,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-p2",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-old-p2",
        refresh_token="rt-old-p2",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await db.insert_pending_confirmation(
        confirmation_hash=hashlib.sha256(b"conf-p2").hexdigest(),
        user_id=uid2,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-p2",
        provider_union_id=None,
        provider_email="p2@example.com",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-staged-p2",
        refresh_token="rt-staged-p2",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        intent="reauth",
        preferences={},
        confirm_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    rev2 = create_autospec(RemoteRevoker, instance=True)
    rev2.revoke.return_value = RevokeOutcome.SUCCEEDED
    pids2 = await begin_disconnect_all_services(uid2, "google")
    assert len(pids2) == 1
    fin2 = await finish_disconnect(pids2[0], revoker=rev2)
    assert fin2 is not None
    assert fin2.remote_revoke_status == RemoteRevokeStatus.SUCCEEDED
    rev2.revoke.assert_awaited_once_with(
        provider="google", oauth_client_id=client_id, token="rt-staged-p2"
    )

    # Part C: Expired pending confirmation cleaned up -> enqueues PENDING revoke ledger that due runner executes!
    uid3 = "u-r4-pend-expired"
    await db.insert_pending_confirmation(
        confirmation_hash=hashlib.sha256(b"conf-exp").hexdigest(),
        user_id=uid3,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-expired-only",
        provider_union_id=None,
        provider_email="exp@example.com",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-exp",
        refresh_token="rt-exp-staged",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        intent="connect",
        preferences={},
        confirm_expires_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    _, confirms_del = await db.delete_expired_oauth_states_and_confirmations()
    assert confirms_del >= 1
    active_exp = await db.list_active_revokes_for_google_project_sub(
        "666777888", "sub-expired-only", decrypt_revoke_token=True
    )
    assert len(active_exp) == 1
    assert active_exp[0].revoke_token == "rt-exp-staged"
    rev3 = create_autospec(RemoteRevoker, instance=True)
    rev3.revoke.return_value = RevokeOutcome.SUCCEEDED
    outcomes = await run_due_remote_revokes_once(rev3)
    assert outcomes[active_exp[0].purge_id] == RemoteRevokeStatus.SUCCEEDED
    rev3.revoke.assert_awaited_with(
        provider="google", oauth_client_id=client_id, token="rt-exp-staged"
    )


async def test_r4_google_account_switch_syncs_target_sibling_actual_scopes_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Codex Medium #2): switching Google Calendar to an account that already has Gmail connected updates the Gmail sibling in the same tx with the new token and actual granted scopes."""
    from src.dalton.services.user_integrations_lifecycle import (
        replace_connection_for_account_switch,
    )

    _ = gws_live_pool
    uid = "u-r4-switch-sibling"
    client_id = "888999000-sw.apps.googleusercontent.com"
    _install_creds(google_client_id=client_id)

    await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-old-cal",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-old-cal",
        refresh_token="rt-old-cal",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    mail_before = await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="mail",
        oauth_client_id=client_id,
        provider_account_id="sub-target-acct",
        granted_scopes=f"{GOOGLE_GMAIL_MIN_SCOPES[0]} https://www.googleapis.com/auth/gmail.drafts.create",
        access_token="at-old-target-mail",
        refresh_token="rt-old-target-mail",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        preferences={"allow_create_draft": True},
    )

    # Switch calendar from sub-old-cal -> sub-target-acct with calendar-only granted scopes
    await replace_connection_for_account_switch(
        uid,
        "google",
        "calendar",
        new_oauth_client_id=client_id,
        new_provider_account_id="sub-target-acct",
        new_granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        new_access_token="at-new-shared",
        new_refresh_token="rt-new-shared",
        new_expires_at=datetime.now(UTC) + timedelta(hours=2),
    )

    cal_after = await db.get_connection(uid, "google", "calendar", decrypt_tokens=True)
    mail_after = await db.get_connection(uid, "google", "mail", decrypt_tokens=True)
    assert cal_after is not None and mail_after is not None
    assert cal_after.provider_account_id == "sub-target-acct"
    assert cal_after.refresh_token == "rt-new-shared"
    # Gmail sibling on target account received the NEW shared token & actual scopes, preserved auth_epoch,
    # and converged to needs_reauth with allow_create_draft=False since Gmail scope wasn't in the new grant!
    assert mail_after.refresh_token == "rt-new-shared"
    assert mail_after.access_token == "at-new-shared"
    assert mail_after.auth_epoch == mail_before.auth_epoch
    assert mail_after.token_generation == mail_before.token_generation + 1
    assert mail_after.status == IntegrationStatus.NEEDS_REAUTH
    assert mail_after.last_error_code == "missing_required_scopes"
    assert mail_after.preferences.get("allow_create_draft") is False


async def test_r4_local_first_process_revoke_and_live_gateway_feishu_app_id_and_historical_match_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Codex High #3 + Medium #1 + Medium #3): process_revoke enforces local-first; live gateway runtime Feishu App ID overrides rotated config; has_matching_feishu_event_connection matches historical rows."""
    from src.dalton.services.feishu_integration_events import (
        has_matching_feishu_event_connection,
    )
    from src.dalton.services.integration_revoke import process_revoke

    _ = gws_live_pool

    # 1. Local-first in process_revoke: running local purge blocks remote revoke even on direct call
    uid_lf = "u-r4-local-first"
    cid_g = "999000111-lf.apps.googleusercontent.com"
    running_ledger = await db.insert_purge_ledger(
        user_id=uid_lf,
        provider="google",
        service="calendar",
        oauth_client_id=cid_g,
        provider_account_id="sub-lf",
        auth_epoch_upto=1,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        local_purge_status=LocalPurgeStatus.RUNNING,
        purged_max_turn_by_thread={},
        remote_revoke_status=RemoteRevokeStatus.PENDING,
        revoke_token_plaintext="rt-lf",
    )
    rev_lf = create_autospec(RemoteRevoker, instance=True)
    rev_lf.revoke.return_value = RevokeOutcome.SUCCEEDED
    st_running = await process_revoke(running_ledger.purge_id, rev_lf)
    assert st_running == RemoteRevokeStatus.PENDING
    assert rev_lf.revoke.await_count == 0

    # 2. Live gateway runtime Feishu App ID takes precedence over rotated deployment credentials
    _install_creds(feishu_app_id="cli_rotated_new", feishu_app_secret="sec_new")
    await db.upsert_channel_gateway_runtime(
        instance_id="gw-live-1",
        lease_epoch=10,
        feishu_app_id="cli_running_old",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert await oauth_mod._resolve_channel_gateway_feishu_app_id() == "cli_running_old"
    await db.clear_channel_gateway_runtime(instance_id="gw-live-1", lease_epoch=10)
    assert await oauth_mod._resolve_channel_gateway_feishu_app_id() == "cli_rotated_new"

    # 3. Historical Feishu connection match helper works for gateway revocation forwarding
    uid_fs = "u-r4-hist-fs"
    await db.upsert_connection(
        user_id=uid_fs,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_hist_app",
        provider_account_id="ou_hist_member",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-fs-hist",
        refresh_token="rt-fs-hist",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    matched = await has_matching_feishu_event_connection(
        {
            "header": {
                "event_type": FEISHU_REVOKED_EVENT_TYPE,
                "event_id": "evt-r4-hist-1",
                "app_id": "cli_hist_app",
            },
            "event": {"open_id": "ou_hist_member", "revoke_reason": "Revoked by user action."},
        }
    )
    assert matched is True
    unmatched = await has_matching_feishu_event_connection(
        {
            "header": {
                "event_type": FEISHU_REVOKED_EVENT_TYPE,
                "event_id": "evt-r4-hist-2",
                "app_id": "cli_hist_app",
            },
            "event": {"open_id": "ou_nobody", "revoke_reason": "Revoked by user action."},
        }
    )
    assert unmatched is False


# ---------------------------------------------------------------------------
# Round 5 Real PostgreSQL Integration Tests
# ---------------------------------------------------------------------------


async def test_r6_second_member_same_google_account_rejected_and_single_inflight_revoke_db(
    gws_live_pool: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG (Decision 1): one Google (project, sub) connects to at most ONE Dalton member (rejecting a second member with exact Chinese prompt without revoking owner, including under concurrent race or unexpired pending confirmation), while same-member multi-service sharing and single-inflight revoke remain protected."""
    from src.dalton.services import user_integrations_oauth as _oauth_mod
    from src.dalton.services.integration_revoke import process_revoke

    _ = gws_live_pool
    client_id = "666777888-singleowner.apps.googleusercontent.com"
    _install_creds(google_client_id=client_id)
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setattr(
        _oauth_mod, "is_team_member", create_autospec(_oauth_mod.is_team_member, return_value=True)
    )

    uid_a = "u-r6-owner-a"
    uid_b = "u-r6-other-b"
    shared_sub = "sub-r6-single-owner"

    # (a) Member A is connected -> Member B callback for same (project, sub) is REJECTED without revoking Member A!
    await db.upsert_connection(
        user_id=uid_a,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id=shared_sub,
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-owner-a",
        refresh_token="rt-owner-a",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    started_b = await start_oauth_authorization(
        user_id=uid_b,
        provider="google",
        service="calendar",
        auth_mode="supabase",
        user_email="b@example.com",
    )
    g_client = create_autospec(GoogleOAuthClient, instance=True)
    g_client.exchange_code.return_value = OAuthTokenResponse(
        access_token="at-b-rej",
        refresh_token="rt-b-rej",
        expires_in=3600,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        id_token="id.b",
    )
    g_client.verify_id_token.return_value = GoogleIdTokenClaims(
        sub=shared_sub,
        email="shared@example.com",
        email_verified=True,
        aud=client_id,
        iss="https://accounts.google.com",
        exp=int(time.time()) + 3600,
    )
    rev_check = create_autospec(RemoteRevoker, instance=True)
    rev_check.revoke.return_value = RevokeOutcome.SUCCEEDED

    cb_b = await handle_oauth_callback(
        provider="google",
        code="code-b",
        state=started_b.state,
        google_client=g_client,
        revoker=rev_check,
    )
    assert cb_b.status == "error"
    assert cb_b.error_code == "google_account_already_connected"
    assert cb_b.error_message == "这个 Google 账号已被团队里另一位成员连接，请换一个账号或请对方先断开"
    assert rev_check.revoke.await_count == 0
    assert await db.get_connection(uid_b, "google", "calendar") is None
    assert await db.get_connection(uid_a, "google", "calendar") is not None

    # (b) Member A has an unexpired pending confirmation -> Member B callback for same (project, sub) is ALSO rejected!
    sub_pend = "sub-r6-pending-owner"
    await db.insert_pending_confirmation(
        confirmation_hash=hashlib.sha256(b"conf-r6-owner").hexdigest(),
        user_id=uid_a,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id=sub_pend,
        provider_union_id=None,
        provider_email="pend@example.com",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-p",
        refresh_token="rt-p",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        intent="connect",
        preferences={},
        confirm_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    started_b2 = await start_oauth_authorization(
        user_id=uid_b,
        provider="google",
        service="calendar",
        auth_mode="supabase",
        user_email="b@example.com",
    )
    g_client.verify_id_token.return_value = GoogleIdTokenClaims(
        sub=sub_pend,
        email="pend@example.com",
        email_verified=True,
        aud=client_id,
        iss="https://accounts.google.com",
        exp=int(time.time()) + 3600,
    )
    cb_b2 = await handle_oauth_callback(
        provider="google",
        code="code-b2",
        state=started_b2.state,
        google_client=g_client,
        revoker=rev_check,
    )
    assert cb_b2.status == "error"
    assert cb_b2.error_code == "google_account_already_connected"
    assert rev_check.revoke.await_count == 0

    # (c) Concurrent race between Member A and Member B for a brand-new (project, sub) -> strictly ONE wins, ONE rejected!
    sub_race = "sub-r6-race-two-members"
    st_ra = await start_oauth_authorization(
        user_id=uid_a,
        provider="google",
        service="mail",
        auth_mode="supabase",
        user_email="a@example.com",
    )
    st_rb = await start_oauth_authorization(
        user_id=uid_b,
        provider="google",
        service="mail",
        auth_mode="supabase",
        user_email="b@example.com",
    )
    g_race = create_autospec(GoogleOAuthClient, instance=True)
    g_race.exchange_code.return_value = OAuthTokenResponse(
        access_token="at-race",
        refresh_token="rt-race",
        expires_in=3600,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope=" ".join(GOOGLE_GMAIL_MIN_SCOPES),
        id_token="id.race",
    )
    g_race.verify_id_token.return_value = GoogleIdTokenClaims(
        sub=sub_race,
        email="race@example.com",
        email_verified=True,
        aud=client_id,
        iss="https://accounts.google.com",
        exp=int(time.time()) + 3600,
    )
    res_a, res_b = await asyncio.wait_for(
        asyncio.gather(
            handle_oauth_callback(
                provider="google", code="ca", state=st_ra.state, google_client=g_race, revoker=rev_check
            ),
            handle_oauth_callback(
                provider="google", code="cb", state=st_rb.state, google_client=g_race, revoker=rev_check
            ),
        ),
        timeout=10.0,
    )
    outcomes = sorted([(res_a.status, res_a.error_code), (res_b.status, res_b.error_code)])
    assert outcomes == [
        ("confirmation_required", None),
        ("error", "google_account_already_connected"),
    ]
    assert rev_check.revoke.await_count == 0

    # (d) Same member with two distinct PENDING ledgers for same (project, sub) -> at most ONE in-flight revoke HTTP!
    l_dup1 = await db.insert_purge_ledger(
        user_id=uid_a,
        provider="google",
        service="mail",
        oauth_client_id=client_id,
        provider_account_id="sub-single-inflight",
        auth_epoch_upto=1,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        local_purge_status=LocalPurgeStatus.COMPLETED,
        purged_max_turn_by_thread={},
        remote_revoke_status=RemoteRevokeStatus.PENDING,
        revoke_token_plaintext="rt-dup-1",
    )
    l_dup2 = await db.insert_purge_ledger(
        user_id=uid_a,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-single-inflight",
        auth_epoch_upto=2,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        local_purge_status=LocalPurgeStatus.COMPLETED,
        purged_max_turn_by_thread={},
        remote_revoke_status=RemoteRevokeStatus.PENDING,
        revoke_token_plaintext="rt-dup-2",
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    active_calls = {"cur": 0, "max": 0, "total": 0}

    async def _slow_rev(**_k: Any) -> RevokeOutcome:
        active_calls["cur"] += 1
        active_calls["total"] += 1
        active_calls["max"] = max(active_calls["max"], active_calls["cur"])
        entered.set()
        try:
            await asyncio.wait_for(release.wait(), timeout=10.0)
            return RevokeOutcome.SUCCEEDED
        finally:
            active_calls["cur"] -= 1

    rev = create_autospec(RemoteRevoker, instance=True)
    rev.revoke.side_effect = _slow_rev

    t1 = asyncio.create_task(process_revoke(l_dup1.purge_id, rev))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10.0)
        s2 = await asyncio.wait_for(process_revoke(l_dup2.purge_id, rev), timeout=10.0)
        assert s2 == RemoteRevokeStatus.PENDING
        assert active_calls["max"] == 1
        assert active_calls["total"] == 1
        release.set()
        s1 = await asyncio.wait_for(t1, timeout=10.0)
    finally:
        release.set()
        if not t1.done():
            t1.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t1
    assert s1 == RemoteRevokeStatus.SUCCEEDED


async def test_r5_unconsumed_state_defers_only_matching_project_sub_not_other_accounts_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Grok Med #1): unconsumed Google OAuth state matches strictly by (project, sub) and NEVER blocks revokes for other Google accounts or when expected sub is unknown."""
    from src.dalton.services.integration_revoke import process_revoke

    _ = gws_live_pool
    client_id = "777888999-subscope.apps.googleusercontent.com"
    _install_creds(google_client_id=client_id)

    # Seed unconsumed state expecting sub-A (e.g. reauth of sub-A)
    await db.insert_oauth_state(
        state_hash=hashlib.sha256(b"st-sub-a").hexdigest(),
        user_id="u-r5-a",
        auth_mode="oss",
        session_id=None,
        user_email=None,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        intent="reauth",
        code_verifier="v-a",
        requested_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        redirect_uri="https://dalton.example.com/api/v1/dalton/integrations/google/callback",
        return_to="/settings",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        expected_provider_account_id="sub-A",
    )
    # Seed unconsumed first-connect state with NULL expected_provider_account_id
    await db.insert_oauth_state(
        state_hash=hashlib.sha256(b"st-first").hexdigest(),
        user_id="u-r5-first",
        auth_mode="oss",
        session_id=None,
        user_email=None,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        intent="connect",
        code_verifier="v-first",
        requested_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        redirect_uri="https://dalton.example.com/api/v1/dalton/integrations/google/callback",
        return_to="/settings",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        expected_provider_account_id=None,
    )

    l_sub_a = await db.insert_purge_ledger(
        user_id="u-r5-a",
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-A",
        auth_epoch_upto=1,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        local_purge_status=LocalPurgeStatus.COMPLETED,
        purged_max_turn_by_thread={},
        remote_revoke_status=RemoteRevokeStatus.PENDING,
        revoke_token_plaintext="rt-sub-a",
    )
    l_sub_b = await db.insert_purge_ledger(
        user_id="u-r5-b",
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-B",
        auth_epoch_upto=1,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        local_purge_status=LocalPurgeStatus.COMPLETED,
        purged_max_turn_by_thread={},
        remote_revoke_status=RemoteRevokeStatus.PENDING,
        revoke_token_plaintext="rt-sub-b",
    )

    rev = create_autospec(RemoteRevoker, instance=True)
    rev.revoke.return_value = RevokeOutcome.SUCCEEDED

    # sub-A IS deferred by sub-A's own unconsumed state
    st_a = await process_revoke(l_sub_a.purge_id, rev)
    assert st_a == RemoteRevokeStatus.FAILED_RETRYABLE
    assert rev.revoke.await_count == 0

    # sub-B in the SAME Google project is NOT blocked by sub-A's or unknown first-connect unconsumed states!
    st_b = await process_revoke(l_sub_b.purge_id, rev)
    assert st_b == RemoteRevokeStatus.SUCCEEDED
    assert rev.revoke.await_count == 1


async def test_r5_everflow_confirm_atomic_consume_and_core_direct_commit_guard_no_resurrection_db(
    gws_live_pool: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG (Codex High #2 & #4 + Grok Med #2): Everflow confirm consumes pending row inside activation tx & revokes on failed activation; Core direct callback aborts & revokes if concurrent DELETE voids commit-guard."""
    from src.dalton.services.user_integrations_lifecycle import begin_disconnect

    _ = gws_live_pool
    client_id = "888999000-guard.apps.googleusercontent.com"
    _install_creds(google_client_id=client_id)

    # Part A: Everflow confirm failure keeps pending row until rollback then consumes & revokes Google token
    uid_ef = "u-r5-ef-confirm"
    old_row = await db.upsert_connection(
        user_id=uid_ef,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-ef-old",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-old",
        refresh_token="rt-old",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    raw_conf = "tok-r5-ef-confirm"
    chash = hashlib.sha256(raw_conf.encode()).hexdigest()
    await db.insert_pending_confirmation(
        confirmation_hash=chash,
        user_id=uid_ef,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-ef-new",
        provider_union_id=None,
        provider_email="new@example.com",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-ef-new",
        refresh_token="rt-ef-new-staged",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        intent="switch_account",
        preferences={},
        confirm_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        expected_old_oauth_client_id=client_id,
        expected_old_provider_account_id="sub-ef-old",
        expected_old_auth_epoch=old_row.auth_epoch,
    )
    # Delete old row so confirmation invalidates inside activation
    await db.delete_connection_upto_epoch(uid_ef, "google", "calendar", old_row.auth_epoch)
    rev_ef = create_autospec(RemoteRevoker, instance=True)
    rev_ef.revoke.return_value = RevokeOutcome.SUCCEEDED

    with pytest.raises(PendingConfirmationInvalidError):
        await confirm_pending_integration(
            user_id=uid_ef,
            confirmation_token=raw_conf,
            confirm_account_switch=True,
            revoker=rev_ef,
        )
    assert await db.peek_pending_confirmation(chash, uid_ef) is None
    assert rev_ef.revoke.await_count == 1
    assert rev_ef.revoke.await_args.kwargs["token"] == "rt-ef-new-staged"

    # Part B: Core/OSS direct callback where disconnect + full finish_disconnect (DELETING the row!)
    # completes DURING exchange_code HTTP -> Step 1 connect intent was voided -> NEVER resurrects
    # and newly minted Google token is discarded & revoked!
    from src.dalton.services.user_integrations_lifecycle import finish_disconnect

    uid_core = LOCAL_DEV_USER_ID
    await db.upsert_connection(
        user_id=uid_core,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-core-race",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-core-old",
        refresh_token="rt-core-old",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    started = await start_oauth_authorization(
        user_id=uid_core,
        provider="google",
        service="calendar",
        auth_mode="oss",
        revoker=rev_ef,
    )
    g_client = create_autospec(GoogleOAuthClient, instance=True)

    async def _exchange_with_full_disconnect_during_http(**_kw: Any) -> OAuthTokenResponse:
        # Full disconnect + row deletion completes while HTTP exchange is in flight!
        disc_pid = await begin_disconnect(
            uid_core, "google", "calendar", remote_mode="local_only"
        )
        await finish_disconnect(disc_pid, revoker=None)
        assert await db.get_connection(uid_core, "google", "calendar") is None
        return OAuthTokenResponse(
            access_token="at-core-new",
            refresh_token="rt-core-new-discarded",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token="id.core",
        )

    g_client.exchange_code.side_effect = _exchange_with_full_disconnect_during_http
    g_client.verify_id_token.return_value = GoogleIdTokenClaims(
        sub="sub-core-race",
        email="core@example.com",
        email_verified=True,
        aud=client_id,
        iss="https://accounts.google.com",
        exp=int(time.time()) + 3600,
    )
    rev_core = create_autospec(RemoteRevoker, instance=True)
    rev_core.revoke.return_value = RevokeOutcome.SUCCEEDED

    cb_res = await handle_oauth_callback(
        provider="google",
        code="code-core-race",
        state=started.state,
        google_client=g_client,
        revoker=rev_core,
    )
    assert cb_res.status == "error"
    assert cb_res.error_code == "connection_disconnected_during_callback"
    # Connection row MUST remain deleted (NEVER resurrected as a fresh first connect!)
    assert await db.get_connection(uid_core, "google", "calendar") is None
    # Discarded new Google token was enqueued & revoked!
    rev_core.revoke.assert_awaited_once_with(
        provider="google",
        oauth_client_id=client_id,
        token="rt-core-new-discarded",
    )
    ledgers_core = await db.list_user_purge_ledgers(uid_core, active_only=False)
    assert any(
        l.trigger_reason == "callback_disconnected_before_commit"
        and l.remote_revoke_status == RemoteRevokeStatus.SUCCEEDED
        for l in ledgers_core
    )


async def test_r5_lock_guard_enabled_across_refresh_sweep_disconnect_revoke_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Lead §1): with advisory_lock_http_guard(True), refresh (2 concurrent workers), 12h sweep (2 concurrent workers), disconnect, and revoke all execute external HTTP with ZERO PG advisory locks held."""
    from src.dalton.integrations.lock_guard import (
        advisory_lock_http_guard,
        held_advisory_locks,
    )
    from src.dalton.services.user_integrations_lifecycle import (
        begin_disconnect,
        finish_disconnect,
    )

    pool = gws_live_pool
    client_id = "999000111-guard.apps.googleusercontent.com"
    _install_creds(google_client_id=client_id)

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE dalton_integration_health_sweep_state SET sweep_gen = 0, inflight_until = NULL, last_started_at = NULL, last_completed_at = NULL"
        )

    uid = "u-r5-guard-all"
    await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-guard-all",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-old",
        refresh_token="rt-old",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    # 1) Refresh (concurrent 2 workers): single upstream call, zero advisory locks held during HTTP
    ref_entered = asyncio.Event()
    ref_release = asyncio.Event()
    g_client = create_autospec(GoogleOAuthClient, instance=True)

    async def _slow_ref(**_k: Any) -> OAuthTokenResponse:
        assert held_advisory_locks() == ()
        ref_entered.set()
        await asyncio.wait_for(ref_release.wait(), timeout=10.0)
        return OAuthTokenResponse(
            access_token="at-r5-refreshed",
            refresh_token="rt-r5-refreshed",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        )

    g_client.refresh_token.side_effect = _slow_ref

    with advisory_lock_http_guard(True):
        w1 = asyncio.create_task(
            ensure_fresh_access_token(uid, "google", "calendar", google_client=g_client)
        )
        try:
            await asyncio.wait_for(ref_entered.wait(), timeout=10.0)
            w2 = asyncio.create_task(
                ensure_fresh_access_token(
                    uid,
                    "google",
                    "calendar",
                    google_client=g_client,
                    waiter_poll_interval_seconds=0.05,
                )
            )
            await asyncio.sleep(0.05)
            ref_release.set()
            r1, r2 = await asyncio.wait_for(asyncio.gather(w1, w2), timeout=10.0)
        finally:
            ref_release.set()
            if not w1.done():
                w1.cancel()
        assert r1.access_token == "at-r5-refreshed"
        assert r2.access_token == "at-r5-refreshed"
        assert g_client.refresh_token.await_count == 1

        # 2) Health sweep (concurrent 2 workers): 1st runs HTTP outside global lock; 2nd gets lock_busy via inflight_until!
        sw_entered = asyncio.Event()
        sw_release = asyncio.Event()

        async def _slow_sw_ref(**_k: Any) -> OAuthTokenResponse:
            assert held_advisory_locks() == ()
            sw_entered.set()
            await asyncio.wait_for(sw_release.wait(), timeout=10.0)
            return OAuthTokenResponse(
                access_token="at-r5-swept",
                refresh_token="rt-r5-swept",
                expires_in=3600,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            )

        g_client.refresh_token.side_effect = _slow_sw_ref
        sw1_task = asyncio.create_task(
            run_integration_health_sweep_once(force=True, google_client=g_client)
        )
        try:
            await asyncio.wait_for(sw_entered.wait(), timeout=10.0)
            rep_busy = await asyncio.wait_for(
                run_integration_health_sweep_once(force=True, google_client=g_client),
                timeout=10.0,
            )
            assert rep_busy.acquired is False
            assert rep_busy.skipped_reason == "lock_busy"
            sw_release.set()
            rep_ok = await asyncio.wait_for(sw1_task, timeout=10.0)
        finally:
            sw_release.set()
            if not sw1_task.done():
                sw1_task.cancel()
        assert rep_ok.acquired is True
        assert rep_ok.healthy == 1

        # 3 & 4) Disconnect + Remote Revoke: purge lock released BEFORE revoke HTTP!
        rev = create_autospec(RemoteRevoker, instance=True)

        async def _guarded_revoke(**_k: Any) -> RevokeOutcome:
            assert held_advisory_locks() == ()
            return RevokeOutcome.SUCCEEDED

        rev.revoke.side_effect = _guarded_revoke
        pid = await begin_disconnect(uid, "google", "calendar")
        fin = await finish_disconnect(pid, revoker=rev)
        assert fin is not None
        assert fin.local_purge_status == LocalPurgeStatus.COMPLETED
        assert fin.remote_revoke_status == RemoteRevokeStatus.SUCCEEDED
        assert rev.revoke.await_count == 1


async def test_r5_feishu_binding_own_app_id_after_gateway_rotation_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Codex Med #3): dalton_channel_identities.feishu_app_id scopes Feishu open_id verification to the binding's OWN app_id so gateway app rotation + restart never falsely rejects valid connections."""
    pool = gws_live_pool
    uid = "u-r5-fs-binding"
    async with pool.connection() as conn:
        has_tbl = await (
            await conn.execute("SELECT to_regclass('public.dalton_channel_identities')")
        ).fetchone()
        if not has_tbl or has_tbl[0] is None:
            pytest.skip("dalton_channel_identities table not present in stripped schema")
        await conn.execute(
            """
            INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id, feishu_app_id)
            VALUES (%s, 'feishu', 'ou_from_old_app', 'cli_old_app')
            ON CONFLICT (user_id, platform) DO UPDATE
            SET platform_user_id = EXCLUDED.platform_user_id,
                feishu_app_id = EXCLUDED.feishu_app_id
            """,
            (uid,),
        )
    assert await db.get_channel_feishu_binding(uid) == ("ou_from_old_app", "cli_old_app")

    # Legacy row with NULL feishu_app_id returns (open_id, None)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE dalton_channel_identities SET feishu_app_id = NULL WHERE user_id = %s AND platform = 'feishu'",
            (uid,),
        )
    assert await db.get_channel_feishu_binding(uid) == ("ou_from_old_app", None)


# ---------------------------------------------------------------------------
# Round 6 Narrowed Scope Real PostgreSQL Integration Tests (Decisions 2–5 + Grok)
# ---------------------------------------------------------------------------


async def test_r6_small_pool_size_2_saturated_concurrency_no_deadlock_db(
    gws_small_pool_2: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG (Decision 2 + Codex High #2): with a genuinely wired AsyncConnectionPool(min_size=1, max_size=2, timeout=5.0), concurrent refresh, disconnect, revoke, health sweep, AND two concurrent 2A purges with real transcript blobs NEVER deadlock or starve the pool."""
    from src.dalton.services.purge_fence import save_stored_transcript_fenced
    from src.dalton.services.user_integrations_lifecycle import (
        begin_disconnect,
        finish_disconnect,
    )
    from src.server.database import thread_transcripts as tt_db
    from src.utils import storage as storage_mod
    from tests.integration.dalton.test_user_integrations_purge_db import (
        _seed_user_ws_thread,
    )

    small_pool = gws_small_pool_2
    assert small_pool.max_size == 2

    client_id = "222333444-pool2.apps.googleusercontent.com"
    _install_creds(google_client_id=client_id)

    async with small_pool.connection() as conn:
        await conn.execute(
            "UPDATE dalton_integration_health_sweep_state SET sweep_gen = 0, inflight_until = NULL, last_started_at = NULL, last_completed_at = NULL"
        )

    uid_ref = "u-r6-pool2-ref"
    uid_disc = "u-r6-pool2-disc"
    await db.upsert_connection(
        user_id=uid_ref,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-pool2-ref",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-old-ref",
        refresh_token="rt-old-ref",
        expires_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    await db.upsert_connection(
        user_id=uid_disc,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-pool2-disc",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-old-disc",
        refresh_token="rt-old-disc",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    g_client = create_autospec(GoogleOAuthClient, instance=True)

    async def _fast_ref(**_k: Any) -> OAuthTokenResponse:
        await asyncio.sleep(0.05)
        return OAuthTokenResponse(
            access_token="at-pool2-new",
            refresh_token="rt-pool2-new",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        )

    g_client.refresh_token.side_effect = _fast_ref
    rev = create_autospec(RemoteRevoker, instance=True)
    rev.revoke.return_value = RevokeOutcome.SUCCEEDED

    async def _disconnect_and_revoke() -> Any:
        pid = await begin_disconnect(uid_disc, "google", "calendar")
        return await finish_disconnect(pid, revoker=rev)

    results = await asyncio.wait_for(
        asyncio.gather(
            ensure_fresh_access_token(
                uid_ref, "google", "calendar", google_client=g_client, waiter_poll_interval_seconds=0.05
            ),
            ensure_fresh_access_token(
                uid_ref, "google", "calendar", google_client=g_client, waiter_poll_interval_seconds=0.05
            ),
            _disconnect_and_revoke(),
            run_integration_health_sweep_once(force=True, google_client=g_client),
        ),
        timeout=10.0,
    )
    assert results[0].access_token == "at-pool2-new"
    assert results[1].access_token == "at-pool2-new"
    assert results[2] is not None
    assert results[2].remote_revoke_status == RemoteRevokeStatus.SUCCEEDED
    assert results[3].acquired is True

    # Part 2 (Codex High #2): TWO concurrent 2A purges with real transcript blobs on max_size=2 pool!
    # Both reach Layer 3b release_blob concurrently; reusing blob_conn prevents pool starvation/timeout.
    monkeypatch.setattr(storage_mod, "upload_bytes", create_autospec(storage_mod.upload_bytes, return_value=True))
    blob_users: list[tuple[str, str, str]] = []
    for idx in (1, 2):
        b_uid, _, b_tid = await _seed_user_ws_thread(small_pool, user_id=f"u-r7-pool2-blob-{idx}")
        run_id = await _seed_turn(small_pool, thread_id=b_tid, turn_index=0, user_text=f"blob turn {idx}")
        conn_row = await db.upsert_connection(
            user_id=b_uid,
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_db_test_1",
            provider_account_id=f"ou-blob-{idx}",
            granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
            access_token=f"at-b-{idx}",
            refresh_token=f"rt-b-{idx}",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await db.record_touch(
            thread_id=b_tid,
            response_id=run_id,
            provider="feishu",
            service="calendar",
            auth_epoch=conn_row.auth_epoch,
            turn_index=0,
            user_id=b_uid,
            provider_account_id=f"ou-blob-{idx}",
        )
        payload = f"transcript-blob-content-{idx}".encode() * 200
        sha = hashlib.sha256(payload).hexdigest()
        tr = tt_db.StoredTranscript(
            f"fp-blob-{idx}",
            f"cp-blob-{idx}",
            '{"segments": []}',
            files={f"turn-000{idx}.jsonl": (sha, len(payload))},
        )
        saved = await save_stored_transcript_fenced(
            b_tid, "", b_uid, tr, {sha: payload}, expected_purge_gen=0
        )
        assert saved is True
        pid_b = await begin_disconnect(b_uid, "feishu", "calendar", remote_mode="local_only")
        blob_users.append((b_uid, sha, pid_b))

    purge_res = await asyncio.wait_for(
        asyncio.gather(
            finish_disconnect(blob_users[0][2], revoker=None),
            finish_disconnect(blob_users[1][2], revoker=None),
        ),
        timeout=10.0,
    )
    assert all(r is not None and r.local_purge_status == LocalPurgeStatus.COMPLETED for r in purge_res)
    async with small_pool.connection() as conn:
        for b_uid, sha, _ in blob_users:
            brow = await (
                await conn.execute(
                    "SELECT condemned_at FROM workspace_file_blobs WHERE user_id = %s AND sha256 = %s",
                    (b_uid, sha),
                )
            ).fetchone()
            assert brow is not None and brow[0] is not None

    # Part 3 (Round 8 Decision 3 / Codex High #4): TWO concurrent 2A purges hitting
    # historical archived conversation cards on max_size=2 pool! Sandbox session lookup,
    # workspace lookup (get_workspace), rm -f, and delete_file_rows run strictly OUTSIDE
    # thread advisory locks and App DB transactions so two slow concurrent purges NEVER timeout.
    from types import SimpleNamespace

    from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
    from src.dalton.integrations.lock_guard import (
        advisory_lock_http_guard,
        held_advisory_locks,
    )
    from src.server.services.workspace_manager import WorkspaceManager
    from tests.integration.dalton.test_user_integrations_purge_db import (
        _seed_conversation_card,
    )

    card_pids: list[str] = []
    card_ids: list[str] = []
    for idx in (1, 2):
        c_uid, c_wid, c_tid = await _seed_user_ws_thread(
            small_pool, user_id=f"u-r8-pool2-card-{idx}"
        )
        c_run_id = await _seed_turn(
            small_pool, thread_id=c_tid, turn_index=0, user_text=f"card turn {idx}"
        )
        c_conn = await db.upsert_connection(
            user_id=c_uid,
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_db_test_1",
            provider_account_id=f"ou-card-{idx}",
            granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
            access_token=f"at-c-{idx}",
            refresh_token=f"rt-c-{idx}",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await db.record_touch(
            thread_id=c_tid,
            response_id=c_run_id,
            provider="feishu",
            service="calendar",
            auth_epoch=c_conn.auth_epoch,
            turn_index=0,
            user_id=c_uid,
            provider_account_id=f"ou-card-{idx}",
        )
        cid_val = f"card-r8-pool2-{idx}"
        await _seed_conversation_card(
            small_pool,
            card_id=cid_val,
            thread_id=c_tid,
            workspace_id=c_wid,
            claim=f"archived card secret {idx}",
        )
        card_ids.append(cid_val)
        card_pids.append(
            await begin_disconnect(c_uid, "feishu", "calendar", remote_mode="local_only")
        )

    rm_commands: list[str] = []
    fake_sandbox = create_autospec(PTCSandbox, instance=True)
    fake_sandbox.working_dir = "/workspace"
    # autospec does not create attributes assigned only in __init__; SandboxBackend reads it.
    fake_sandbox._work_dir = "/workspace"

    async def _slow_bash(**kwargs: Any) -> dict[str, Any]:
        assert held_advisory_locks() == ()
        await asyncio.sleep(0.15)
        rm_commands.append(str(kwargs.get("command") or ""))
        return {"stdout": "", "stderr": "", "exit_code": 0}

    fake_sandbox.execute_bash_command.side_effect = _slow_bash

    fake_wm = create_autospec(WorkspaceManager, instance=True)

    async def _slow_session(workspace_id: str, user_id: str | None = None) -> Any:
        _ = (workspace_id, user_id)
        assert held_advisory_locks() == ()
        await asyncio.sleep(0.15)
        return SimpleNamespace(sandbox=fake_sandbox)

    fake_wm.get_session_for_workspace.side_effect = _slow_session
    monkeypatch.setattr(
        WorkspaceManager,
        "get_instance",
        classmethod(lambda cls, **_kw: fake_wm),
    )

    with advisory_lock_http_guard(True):
        card_purge_res = await asyncio.wait_for(
            asyncio.gather(
                finish_disconnect(card_pids[0], revoker=None),
                finish_disconnect(card_pids[1], revoker=None),
            ),
            timeout=10.0,
        )
    assert all(
        r is not None and r.local_purge_status == LocalPurgeStatus.COMPLETED
        for r in card_purge_res
    )
    assert len(rm_commands) == 2
    async with small_pool.connection() as conn:
        rem_cards = await (
            await conn.execute(
                "SELECT COUNT(*) FROM dalton_conversation_cards WHERE card_id = ANY(%s::text[])",
                (card_ids,),
            )
        ).fetchone()
    assert rem_cards == (0,)


async def test_r6_health_sweep_lease_expiry_takeover_stops_old_worker_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Decision 3 + Item 6): when worker 1's health sweep lease expires during group 1 and worker 2 takes over, worker 1's stale refresh is discarded by CAS, and worker 1 detects sweep_gen loss before group 2 and stops immediately without processing group 2 or overwriting last_completed_at."""
    pool = gws_live_pool
    client_id = "333444555-sweep-takeover.apps.googleusercontent.com"
    _install_creds(google_client_id=client_id)

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE dalton_integration_health_sweep_state SET sweep_gen = 0, inflight_until = NULL, last_started_at = NULL, last_completed_at = NULL"
        )

    await db.upsert_connection(
        user_id="u-r6-sw-1",
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-r6-sw-1",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-1",
        refresh_token="rt-1",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    await db.upsert_connection(
        user_id="u-r6-sw-2",
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-r6-sw-2",
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-2",
        refresh_token="rt-2",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    w1_entered = asyncio.Event()
    w1_release = asyncio.Event()
    w1_calls: list[str] = []
    g_w1 = create_autospec(GoogleOAuthClient, instance=True)

    async def _w1_slow_refresh(*, refresh_token: str, **_k: Any) -> OAuthTokenResponse:
        w1_calls.append(refresh_token)
        w1_entered.set()
        await asyncio.wait_for(w1_release.wait(), timeout=10.0)
        return OAuthTokenResponse(
            access_token="at-w1",
            refresh_token=refresh_token,
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        )

    g_w1.refresh_token.side_effect = _w1_slow_refresh

    g_w2 = create_autospec(GoogleOAuthClient, instance=True)
    g_w2.refresh_token.return_value = OAuthTokenResponse(
        access_token="at-w2",
        refresh_token="rt-w2",
        expires_in=3600,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
    )

    t1 = asyncio.create_task(
        run_integration_health_sweep_once(
            force=True, lease_seconds=1, google_client=g_w1
        )
    )
    try:
        await asyncio.wait_for(w1_entered.wait(), timeout=10.0)
        # Wait for Worker 1's 1-second sweep lease to expire in PostgreSQL AND expire the
        # 25s refresh inflight lease on group 1 (in production 600s sweep lease >> 25s refresh
        # lease, so by takeover time the stalled worker's refresh lease has already elapsed).
        await asyncio.sleep(1.3)
        async with pool.connection() as conn:
            await conn.execute(
                """
                UPDATE dalton_user_integrations
                SET refresh_inflight_until = clock_timestamp() - INTERVAL '1 second'
                WHERE user_id IN ('u-r6-sw-1', 'u-r6-sw-2')
                  AND refresh_inflight_until IS NOT NULL
                """
            )
        # Worker 2 takes over cleanly with a fresh sweep_gen and refreshes both groups!
        rep_w2 = await asyncio.wait_for(
            run_integration_health_sweep_once(
                force=True, lease_seconds=30, google_client=g_w2
            ),
            timeout=10.0,
        )
        assert rep_w2.acquired is True
        assert rep_w2.skipped_reason is None
        assert rep_w2.checked == 2
        assert rep_w2.healthy == 2

        state_after_w2 = await db.read_health_sweep_state()
        assert state_after_w2 is not None
        gen_after_w2 = state_after_w2["sweep_gen"]
        done_after_w2 = state_after_w2["last_completed_at"]
        assert done_after_w2 is not None

        # Now release Worker 1 -> its stale group-1 refresh is discarded by CAS, and before
        # starting group 2 Worker 1 detects sweep_gen loss and STOPS!
        w1_release.set()
        rep_w1 = await asyncio.wait_for(t1, timeout=10.0)
    finally:
        w1_release.set()
        if not t1.done():
            t1.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t1

    assert rep_w1.acquired is True
    assert rep_w1.skipped_reason == "lease_lost"
    assert rep_w1.checked == 1
    assert len(w1_calls) == 1
    # Both connections retain Worker 2's fresh token (never overwritten by stale Worker 1's at-w1!)
    for uid_sw in ("u-r6-sw-1", "u-r6-sw-2"):
        sw_row = await db.get_connection(uid_sw, "google", "calendar", decrypt_tokens=True)
        assert sw_row is not None and sw_row.access_token == "at-w2"
    state_final = await db.read_health_sweep_state()
    assert state_final is not None
    assert state_final["sweep_gen"] == gen_after_w2
    assert state_final["last_completed_at"] == done_after_w2


async def test_r6_google_exchange_timeout_and_lease_expiry_vs_revoke_and_missing_scope_revoke_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Decision 4 + Grok Medium #1): exchange timeout aborts cleanly without revoking; expired exchange lease / claimed revoke aborts staging & revokes discarded token; missing required scopes on first connect revokes minted refresh token."""
    from src.dalton.services.integration_revoke import process_revoke

    pool = gws_live_pool
    uid = LOCAL_DEV_USER_ID
    client_id = "444555666-r6ex.apps.googleusercontent.com"
    _install_creds(google_client_id=client_id)

    # (a) First-connect missing required scopes -> error + newly minted refresh_token is revoked!
    st_scope = await start_oauth_authorization(
        user_id=uid, provider="google", service="calendar", auth_mode="oss"
    )
    g_scope = create_autospec(GoogleOAuthClient, instance=True)
    g_scope.exchange_code.return_value = OAuthTokenResponse(
        access_token="at-min-scope",
        refresh_token="rt-min-scope-abandoned",
        expires_in=3600,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope="openid email",
        id_token="id.min",
    )
    g_scope.verify_id_token.return_value = GoogleIdTokenClaims(
        sub="sub-r6-missing-scope",
        email="ms@example.com",
        email_verified=True,
        aud=client_id,
        iss="https://accounts.google.com",
        exp=int(time.time()) + 3600,
    )
    rev_scope = create_autospec(RemoteRevoker, instance=True)
    rev_scope.revoke.return_value = RevokeOutcome.SUCCEEDED
    cb_scope = await handle_oauth_callback(
        provider="google",
        code="c-ms",
        state=st_scope.state,
        google_client=g_scope,
        revoker=rev_scope,
    )
    assert cb_scope.status == "error"
    assert cb_scope.error_code == "missing_required_scopes"
    rev_scope.revoke.assert_awaited_once_with(
        provider="google",
        oauth_client_id=client_id,
        token="rt-min-scope-abandoned",
    )

    # (b) Exchange lease expires during slow exchange_code AND pending revoke claims execution ->
    #     late callback sees expired lease / claimed revoke in Step 3, aborts with exchange_lease_expired,
    #     NEVER commits connection, NEVER supersedes the in-flight revoke, and enqueues cleanup!
    st_late = await start_oauth_authorization(
        user_id=uid, provider="google", service="calendar", auth_mode="oss"
    )
    old_ledger = await db.insert_purge_ledger(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id="sub-r6-lease-race",
        auth_epoch_upto=1,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        local_purge_status=LocalPurgeStatus.COMPLETED,
        purged_max_turn_by_thread={},
        remote_revoke_status=RemoteRevokeStatus.PENDING,
        revoke_token_plaintext="rt-old-to-revoke",
    )
    ex_entered = asyncio.Event()
    ex_release = asyncio.Event()
    g_late = create_autospec(GoogleOAuthClient, instance=True)

    async def _late_exchange(**_k: Any) -> OAuthTokenResponse:
        ex_entered.set()
        await asyncio.wait_for(ex_release.wait(), timeout=10.0)
        return OAuthTokenResponse(
            access_token="at-late",
            refresh_token="rt-late-discarded",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token="id.late",
        )

    g_late.exchange_code.side_effect = _late_exchange
    g_late.verify_id_token.return_value = GoogleIdTokenClaims(
        sub="sub-r6-lease-race",
        email="late@example.com",
        email_verified=True,
        aud=client_id,
        iss="https://accounts.google.com",
        exp=int(time.time()) + 3600,
    )
    rev_late = create_autospec(RemoteRevoker, instance=True)
    rev_late.revoke.return_value = RevokeOutcome.SUCCEEDED

    cb_task = asyncio.create_task(
        handle_oauth_callback(
            provider="google",
            code="c-late",
            state=st_late.state,
            google_client=g_late,
            revoker=rev_late,
            exchange_lease_seconds=1,
            exchange_timeout_seconds=10.0,
        )
    )
    try:
        await asyncio.wait_for(ex_entered.wait(), timeout=10.0)
        # Expire exchange_expires_at in DB so old_ledger's process_revoke can run!
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE dalton_user_integration_oauth_states SET exchange_expires_at = clock_timestamp() - INTERVAL '5 seconds' WHERE state_hash = %s",
                (hashlib.sha256(st_late.state.encode()).hexdigest(),),
            )
        old_st = await asyncio.wait_for(process_revoke(old_ledger.purge_id, rev_late), timeout=10.0)
        assert old_st == RemoteRevokeStatus.SUCCEEDED
        ex_release.set()
        cb_late = await asyncio.wait_for(cb_task, timeout=10.0)
    finally:
        ex_release.set()
        if not cb_task.done():
            cb_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cb_task

    assert cb_late.status == "error"
    assert cb_late.error_code == "exchange_lease_expired"
    assert await db.get_connection(uid, "google", "calendar") is None
    # Both old token and discarded late token were revoked
    revoked_tokens = {c.kwargs["token"] for c in rev_late.revoke.await_args_list}
    assert "rt-old-to-revoke" in revoked_tokens
    assert "rt-late-discarded" in revoked_tokens


async def test_r6_shared_google_refresh_disconnect_and_sibling_reauth_cas_discards_stale_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Decision 5 + Codex High #4): calendar refresh claims ONE shared attempt across calendar+mail; if calendar disconnects and mail re-authorizes during HTTP, stale calendar refresh fails CAS on mail and NEVER overwrites mail's fresh token."""
    from src.dalton.services.user_integrations_lifecycle import begin_disconnect
    from src.dalton.services.user_integrations_oauth import IntegrationEpochRevokedError

    _ = gws_live_pool
    uid = "u-r6-shared-cas"
    client_id = "555666777-cas.apps.googleusercontent.com"
    _install_creds(google_client_id=client_id)

    for svc, scopes in (
        ("calendar", " ".join(GOOGLE_CALENDAR_MIN_SCOPES)),
        ("mail", " ".join(GOOGLE_GMAIL_MIN_SCOPES)),
    ):
        await db.upsert_connection(
            user_id=uid,
            provider="google",
            service=svc,
            oauth_client_id=client_id,
            provider_account_id="sub-r6-cas",
            granted_scopes=scopes,
            access_token="at-init",
            refresh_token="rt-init",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )

    entered = asyncio.Event()
    release = asyncio.Event()
    g_client = create_autospec(GoogleOAuthClient, instance=True)

    async def _slow_refresh(**_k: Any) -> OAuthTokenResponse:
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=10.0)
        return OAuthTokenResponse(
            access_token="at-stale-refresh-MUST-NOT-OVERWRITE",
            refresh_token="rt-stale-refresh-MUST-NOT-OVERWRITE",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join((*GOOGLE_CALENDAR_MIN_SCOPES, *GOOGLE_GMAIL_MIN_SCOPES)),
        )

    g_client.refresh_token.side_effect = _slow_refresh

    ref_task = asyncio.create_task(
        ensure_fresh_access_token(uid, "google", "calendar", google_client=g_client)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10.0)
        # 1. Calendar starts disconnecting while refresh HTTP is in flight
        await begin_disconnect(uid, "google", "calendar", remote_mode="local_only")
        # 2. Mail re-authorizes with fresh tokens (bumps token_generation + refresh_attempt_gen)
        await db.upsert_connection(
            user_id=uid,
            provider="google",
            service="mail",
            oauth_client_id=client_id,
            provider_account_id="sub-r6-cas",
            granted_scopes=" ".join(GOOGLE_GMAIL_MIN_SCOPES),
            access_token="at-mail-fresh-reauth",
            refresh_token="rt-mail-fresh-reauth",
            expires_at=datetime.now(UTC) + timedelta(hours=2),
            bump_auth_epoch=False,
        )
        # 3. Stale calendar refresh finishes HTTP and enters Step 3 disconnect branch
        release.set()
        with pytest.raises(IntegrationEpochRevokedError):
            await asyncio.wait_for(ref_task, timeout=10.0)
    finally:
        release.set()
        if not ref_task.done():
            ref_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ref_task

    # Mail MUST keep its brand new re-authorized tokens (stale refresh discarded by CAS!)
    mail_after = await db.get_connection(uid, "google", "mail", decrypt_tokens=True)
    assert mail_after is not None
    assert mail_after.access_token == "at-mail-fresh-reauth"
    assert mail_after.refresh_token == "rt-mail-fresh-reauth"


# ---------------------------------------------------------------------------
# Round 7 Review Fixes Real PostgreSQL Integration Tests
# ---------------------------------------------------------------------------


async def test_r7_shared_google_refresh_disconnect_commits_eligible_sibling_and_clears_lease_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Codex High #1): when calendar disconnects during shared Google refresh HTTP and mail STILL matches Step 1 CAS, Step 3 commits mail's refreshed tokens & generation AND clears calendar's inflight lease BEFORE raising IntegrationEpochRevokedError."""
    from src.dalton.services.user_integrations_lifecycle import begin_disconnect
    from src.dalton.services.user_integrations_oauth import IntegrationEpochRevokedError

    _ = gws_live_pool
    uid = "u-r7-sib-commit"
    client_id = "666777888-r7cas.apps.googleusercontent.com"
    _install_creds(google_client_id=client_id)

    for svc, scopes in (
        ("calendar", " ".join(GOOGLE_CALENDAR_MIN_SCOPES)),
        ("mail", " ".join(GOOGLE_GMAIL_MIN_SCOPES)),
    ):
        await db.upsert_connection(
            user_id=uid,
            provider="google",
            service=svc,
            oauth_client_id=client_id,
            provider_account_id="sub-r7-sib-ok",
            granted_scopes=scopes,
            access_token="at-init-r7",
            refresh_token="rt-init-r7",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )

    entered = asyncio.Event()
    release = asyncio.Event()
    g_client = create_autospec(GoogleOAuthClient, instance=True)

    async def _slow_ref(**_k: Any) -> OAuthTokenResponse:
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=10.0)
        return OAuthTokenResponse(
            access_token="at-r7-committed-for-mail",
            refresh_token="rt-r7-committed-for-mail",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join((*GOOGLE_CALENDAR_MIN_SCOPES, *GOOGLE_GMAIL_MIN_SCOPES)),
        )

    g_client.refresh_token.side_effect = _slow_ref

    ref_task = asyncio.create_task(
        ensure_fresh_access_token(uid, "google", "calendar", google_client=g_client)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10.0)
        # Calendar disconnects during HTTP, while Mail stays untouched (still matches CAS!)
        await begin_disconnect(uid, "google", "calendar", remote_mode="local_only")
        release.set()
        with pytest.raises(IntegrationEpochRevokedError):
            await asyncio.wait_for(ref_task, timeout=10.0)
    finally:
        release.set()
        if not ref_task.done():
            ref_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ref_task

    mail_after = await db.get_connection(uid, "google", "mail", decrypt_tokens=True)
    cal_after = await db.get_connection(uid, "google", "calendar", decrypt_tokens=False)
    assert mail_after is not None
    assert mail_after.status == IntegrationStatus.CONNECTED
    assert mail_after.token_generation == 2
    assert mail_after.access_token == "at-r7-committed-for-mail"
    assert mail_after.refresh_token == "rt-r7-committed-for-mail"
    assert mail_after.refresh_inflight_gen is None
    assert mail_after.refresh_inflight_until is None
    assert cal_after is not None
    assert cal_after.status == IntegrationStatus.DISCONNECT_PENDING
    assert cal_after.refresh_inflight_gen is None
    assert cal_after.refresh_inflight_until is None


async def test_r7_calendar_disconnect_and_mail_confirmation_expiry_dual_conn_no_missed_revoke_db(
    gws_live_pool: Any,
) -> None:
    """Real PG (Codex High #3): dual-connection race between same-member calendar disconnect and mail confirmation expiry NEVER drops remote Google revocation."""
    from src.dalton.services.user_integrations_lifecycle import begin_disconnect

    pool = gws_live_pool
    uid = "u-r7-dual-exp"
    client_id = "777888999-r7exp.apps.googleusercontent.com"
    proj = db.extract_google_project(client_id)
    sub = "sub-r7-dual-exp"
    _install_creds(google_client_id=client_id)

    # Subcase A: mail confirmation expires while Conn 1 holds revoke_lock_key during calendar disconnect
    # (having already seen the unexpired mail ticket -> skipped revoke) and Conn 2 runs expiry cleanup.
    await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id=sub,
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-cal-a",
        refresh_token="rt-cal-a",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    mail_confirm_hash = hashlib.sha256(b"r7-mail-confirm-a").hexdigest()
    await db.insert_pending_confirmation(
        confirmation_hash=mail_confirm_hash,
        user_id=uid,
        provider="google",
        service="mail",
        oauth_client_id=client_id,
        provider_account_id=sub,
        provider_union_id=None,
        provider_email="dual@example.com",
        granted_scopes=" ".join(GOOGLE_GMAIL_MIN_SCOPES),
        access_token="at-mail-a",
        refresh_token="rt-mail-a-staged",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        intent="connect",
        preferences={},
        confirm_expires_at=datetime.now(UTC) + timedelta(milliseconds=350),
    )

    rkey = db.revoke_lock_key(proj, sub)
    lock_acquired = asyncio.Event()
    release_conn1 = asyncio.Event()

    async def _conn1_disconnect_tx() -> None:
        async with pool.connection() as c1, c1.transaction():
            await c1.execute("SELECT pg_advisory_xact_lock(%s)", (rkey,))
            # Calendar sees unexpired mail confirmation -> skips remote revoke
            assert await db.has_active_or_pending_google_grant(
                proj, sub, user_id=uid, exclude_service="calendar", conn=c1
            )
            await db.update_connection_status(
                uid,
                "google",
                "calendar",
                status=IntegrationStatus.DISCONNECT_PENDING,
                clear_tokens=True,
                conn=c1,
            )
            lock_acquired.set()
            await asyncio.wait_for(release_conn1.wait(), timeout=10.0)

    c1_task = asyncio.create_task(_conn1_disconnect_tx())
    try:
        await asyncio.wait_for(lock_acquired.wait(), timeout=10.0)
        # Wait until mail confirmation is past confirm_expires_at while Conn 1 STILL holds revoke_lock_key
        await asyncio.sleep(0.5)
        # Conn 2 runs expiry cleanup -> MUST block on revoke_lock_key until Conn 1 commits!
        c2_task = asyncio.create_task(db.delete_expired_oauth_states_and_confirmations())
        await asyncio.sleep(0.1)
        assert not c2_task.done()  # serialized by revoke_lock_key!
        release_conn1.set()
        await asyncio.wait_for(c1_task, timeout=10.0)
        _, del_cnt = await asyncio.wait_for(c2_task, timeout=10.0)
        assert del_cnt == 1
    finally:
        release_conn1.set()
        if not c1_task.done():
            c1_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await c1_task

    # Conn 2 saw committed disconnect_pending calendar -> MUST have queued PENDING remote revoke!
    async with pool.connection() as conn:
        pending_cnt = await (
            await conn.execute(
                """
                SELECT COUNT(*)
                FROM dalton_integration_purge_ledger
                WHERE user_id = %s
                  AND provider = 'google'
                  AND provider_account_id = %s
                  AND remote_revoke_status = 'pending'
                """,
                (uid, sub),
            )
        ).fetchone()
    assert pending_cnt is not None and pending_cnt[0] == 1

    # Subcase B: concurrent begin_disconnect(calendar) + delete_expired_oauth_states_and_confirmations()
    # across two connections for a second account -> AT LEAST ONE side MUST enqueue PENDING revoke!
    uid_b = "u-r7-dual-exp-b"
    sub_b = "sub-r7-dual-exp-b"
    await db.upsert_connection(
        user_id=uid_b,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id=sub_b,
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-cal-b",
        refresh_token="rt-cal-b",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await db.insert_pending_confirmation(
        confirmation_hash=hashlib.sha256(b"r7-mail-confirm-b").hexdigest(),
        user_id=uid_b,
        provider="google",
        service="mail",
        oauth_client_id=client_id,
        provider_account_id=sub_b,
        provider_union_id=None,
        provider_email="dualb@example.com",
        granted_scopes=" ".join(GOOGLE_GMAIL_MIN_SCOPES),
        access_token="at-mail-b",
        refresh_token="rt-mail-b-staged",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        intent="connect",
        preferences={},
        confirm_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await asyncio.wait_for(
        asyncio.gather(
            begin_disconnect(uid_b, "google", "calendar"),
            db.delete_expired_oauth_states_and_confirmations(),
        ),
        timeout=10.0,
    )
    async with pool.connection() as conn:
        pending_b = await (
            await conn.execute(
                """
                SELECT COUNT(*)
                FROM dalton_integration_purge_ledger
                WHERE user_id = %s
                  AND provider = 'google'
                  AND provider_account_id = %s
                  AND remote_revoke_status = 'pending'
                """,
                (uid_b, sub_b),
            )
        ).fetchone()
    assert pending_b is not None and pending_b[0] >= 1


async def test_r8_core_direct_callback_rejects_409_during_cleanup_and_reconnects_after_db(
    gws_live_pool: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG (Round 8 Decision 1): core direct callback rejects with retryable 409 ('上一次断开还在清理，请稍后重新连接') without draining 2A or erasing pending revoke tokens when (a) sole Google auth has pending revoke + unstarted 2A, (b) in-flight Google revoke claims execution before final tx (rolling back and revoking the in-memory new token), or (c) a background worker holds the 2A lease; once both clear, reconnect succeeds and atomically supersedes any pending revoke."""
    from src.dalton.services.user_integrations_lifecycle import (
        begin_disconnect,
        finish_disconnect_locked,
    )

    pool = gws_live_pool
    uid = LOCAL_DEV_USER_ID
    client_id = "888999000-r8core.apps.googleusercontent.com"
    sub = "sub-r8-sole-google"
    _install_creds(google_client_id=client_id)

    # Seed sole Google calendar authorization and begin disconnect (creates running 2A + pending revoke with old token)
    await db.upsert_connection(
        user_id=uid,
        provider="google",
        service="calendar",
        oauth_client_id=client_id,
        provider_account_id=sub,
        granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        access_token="at-r8-old-sole",
        refresh_token="rt-r8-old-sole",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    old_purge_id = await begin_disconnect(uid, "google", "calendar")
    old_before = await db.get_purge_ledger(old_purge_id, decrypt_revoke_token=True)
    assert old_before is not None
    assert old_before.local_purge_status == LocalPurgeStatus.RUNNING
    assert old_before.remote_revoke_status == RemoteRevokeStatus.PENDING
    assert old_before.revoke_token == "rt-r8-old-sole"

    rev = create_autospec(RemoteRevoker, instance=True)
    rev.revoke.return_value = RevokeOutcome.SUCCEEDED

    def _make_g_client(new_rt: str) -> Any:
        gc = create_autospec(GoogleOAuthClient, instance=True)
        gc.exchange_code.return_value = OAuthTokenResponse(
            access_token="at-r8-new",
            refresh_token=new_rt,
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token="id.r8core",
        )
        gc.verify_id_token.return_value = GoogleIdTokenClaims(
            sub=sub,
            email="sole@example.com",
            email_verified=True,
            aud=client_id,
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )
        return gc

    # (a) Sole Google auth + pending revoke ledger + running/pending 2A -> 409 AND old token still in ledger!
    st_a = await start_oauth_authorization(
        user_id=uid, provider="google", service="calendar", auth_mode="oss"
    )
    g_a = _make_g_client("rt-r8-never-exchanged")
    with pytest.raises(oauth_mod.DisconnectCleanupInProgressError) as exc_a:
        await handle_oauth_callback(
            provider="google",
            code="c-r8-a",
            state=st_a.state,
            google_client=g_a,
            revoker=rev,
        )
    assert exc_a.value.status_code == 409
    assert str(exc_a.value) == "上一次断开还在清理，请稍后重新连接"
    g_a.exchange_code.assert_not_awaited()
    old_after_a = await db.get_purge_ledger(old_purge_id, decrypt_revoke_token=True)
    assert old_after_a is not None
    assert old_after_a.local_purge_status == LocalPurgeStatus.RUNNING
    assert old_after_a.remote_revoke_status == RemoteRevokeStatus.PENDING
    assert old_after_a.revoke_token == "rt-r8-old-sole"

    # (b) Background worker holds the 2A durable lease -> 409 AND old token still in ledger!
    worker_gen = await db.claim_local_purge_attempt(old_purge_id, lease_seconds=60)
    assert worker_gen is not None
    st_b = await start_oauth_authorization(
        user_id=uid, provider="google", service="calendar", auth_mode="oss"
    )
    g_b = _make_g_client("rt-r8-worker-lease")
    with pytest.raises(oauth_mod.DisconnectCleanupInProgressError) as exc_b:
        await handle_oauth_callback(
            provider="google",
            code="c-r8-b",
            state=st_b.state,
            google_client=g_b,
            revoker=rev,
        )
    assert exc_b.value.status_code == 409
    assert str(exc_b.value) == "上一次断开还在清理，请稍后重新连接"
    old_after_b = await db.get_purge_ledger(old_purge_id, decrypt_revoke_token=True)
    assert old_after_b is not None
    assert old_after_b.local_purge_status == LocalPurgeStatus.RUNNING
    assert old_after_b.remote_revoke_status == RemoteRevokeStatus.PENDING
    assert old_after_b.revoke_token == "rt-r8-old-sole"

    # Release worker lease & complete 2A local purge (keeping remote_revoke_status = PENDING)
    await db.release_local_purge_attempt(old_purge_id, expected_attempt_gen=worker_gen)
    fin = await finish_disconnect_locked(old_purge_id, revoker=None, run_remote_revoke=False)
    assert fin is not None and fin.local_purge_status == LocalPurgeStatus.COMPLETED

    # (c) Final-tx race: 2A is complete, but an in-flight revoke claims execution during exchange_code
    # -> final tx rolls back with 409, old pending token stays intact, and in-memory new token is queued/revoked!
    # Scenario (c) isolates the callback's final-tx race: keep the old revoke PENDING by
    # skipping the pre-auth revoke coordination (it would otherwise process the old token).
    monkeypatch.setattr(
        oauth_mod,
        "_coordinate_pre_auth_google_revokes",
        create_autospec(oauth_mod._coordinate_pre_auth_google_revokes, return_value=None),
    )
    st_c = await start_oauth_authorization(
        user_id=uid, provider="google", service="calendar", auth_mode="oss"
    )
    g_c = _make_g_client("rt-r8-abandoned-new")

    async def _exchange_while_revoke_starts(**_k: Any) -> OAuthTokenResponse:
        async with pool.connection() as conn, conn.transaction():
            gen_r = await db.claim_revoke_attempt_tx(
                old_purge_id, lease_seconds=60, conn=conn
            )
            assert gen_r is not None
        return OAuthTokenResponse(
            access_token="at-r8-abandoned",
            refresh_token="rt-r8-abandoned-new",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token="id.r8c",
        )

    g_c.exchange_code.side_effect = _exchange_while_revoke_starts
    with pytest.raises(oauth_mod.DisconnectCleanupInProgressError) as exc_c:
        await handle_oauth_callback(
            provider="google",
            code="c-r8-c",
            state=st_c.state,
            google_client=g_c,
            revoker=rev,
        )
    assert exc_c.value.status_code == 409
    assert str(exc_c.value) == "上一次断开还在清理，请稍后重新连接"
    old_after_c = await db.get_purge_ledger(old_purge_id, decrypt_revoke_token=True)
    assert old_after_c is not None
    assert old_after_c.remote_revoke_status == RemoteRevokeStatus.PENDING
    assert old_after_c.revoke_token == "rt-r8-old-sole"
    assert await db.get_connection(uid, "google", "calendar") is None
    # Verify the abandoned in-memory new token was recorded for remote revocation
    async with pool.connection() as conn:
        abandoned_rows = await (
            await conn.execute(
                """
                SELECT purge_id::text
                FROM dalton_integration_purge_ledger
                WHERE user_id = %s
                  AND provider = 'google'
                  AND trigger_reason = 'callback_disconnect_cleanup_in_progress'
                """,
                (uid,),
            )
        ).fetchall()
    assert len(abandoned_rows) == 1

    # Clear in-flight revoke lease (both 2A completed & no in-flight revoke) -> reconnect succeeds!
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE dalton_integration_purge_ledger SET revoke_inflight_until = NULL WHERE provider = 'google' AND user_id = %s",
            (uid,),
        )
    st_d = await start_oauth_authorization(
        user_id=uid, provider="google", service="calendar", auth_mode="oss"
    )
    g_d = _make_g_client("rt-r8-final-ok")
    cb_ok = await handle_oauth_callback(
        provider="google",
        code="c-r8-d",
        state=st_d.state,
        google_client=g_d,
        revoker=rev,
    )
    assert cb_ok.status == "connected"
    assert cb_ok.connection is not None
    assert cb_ok.connection.provider_account_id == sub
    assert cb_ok.connection.status == IntegrationStatus.CONNECTED
    old_after_d = await db.get_purge_ledger(old_purge_id, decrypt_revoke_token=True)
    assert old_after_d is not None
    assert old_after_d.remote_revoke_status == RemoteRevokeStatus.SUPERSEDED
    assert old_after_d.revoke_token is None


async def test_r8_purge_stale_worker_after_takeover_and_new_turn_preserves_new_data_db(
    gws_live_pool: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG (Round 8 Decision 2 / Codex High #3): when Worker 1 pauses before acquiring the thread lock until its durable lease expires, Worker 2 takes over, completes 2A purge, unblocks the thread, and a new turn writes a transcript + pending steer; resuming Worker 1 re-verifies attempt_gen & thread block inside destructive phases and leaves ALL new-turn data untouched."""
    from contextlib import asynccontextmanager
    from psycopg.types.json import Jsonb

    from src.dalton.services import integration_data_purge as purge_mod
    from src.dalton.services.purge_fence import save_stored_transcript_fenced
    from src.dalton.services.user_integrations_lifecycle import (
        begin_disconnect,
        finish_disconnect_locked,
    )
    from src.server.database import thread_transcripts as tt_db
    from tests.integration.dalton.test_user_integrations_purge_db import (
        _seed_user_ws_thread,
    )

    pool = gws_live_pool
    uid, _, tid = await _seed_user_ws_thread(pool, user_id="u-r8-stale-worker")
    run0_id = await _seed_turn(pool, thread_id=tid, turn_index=0, user_text="turn 0 secret")

    conn_row = await db.upsert_connection(
        user_id=uid,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou-r8-stale",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-r8-s",
        refresh_token="rt-r8-s",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await db.record_touch(
        thread_id=tid,
        response_id=run0_id,
        provider="feishu",
        service="calendar",
        auth_epoch=conn_row.auth_epoch,
        turn_index=0,
        user_id=uid,
        provider_account_id="ou-r8-stale",
    )
    purge_id = await begin_disconnect(uid, "feishu", "calendar", remote_mode="local_only")

    real_acq_thread = purge_mod.acquire_purge_thread_lock
    w1_paused = asyncio.Event()
    w1_release = asyncio.Event()
    acq_calls = 0

    @asynccontextmanager
    async def _pausing_thread_lock(thread_id: str, **kw: Any) -> Any:
        nonlocal acq_calls
        acq_calls += 1
        if acq_calls == 1:
            # Pause Worker 1 RIGHT BEFORE acquiring the thread lock (after lease claim)
            w1_paused.set()
            await asyncio.wait_for(w1_release.wait(), timeout=10.0)
        async with real_acq_thread(thread_id, **kw):
            yield

    monkeypatch.setattr(purge_mod, "acquire_purge_thread_lock", _pausing_thread_lock)

    w1_task = asyncio.create_task(finish_disconnect_locked(purge_id, revoker=None))
    try:
        await asyncio.wait_for(w1_paused.wait(), timeout=10.0)

        # Expire Worker 1's durable lease -> Worker 2 takes over, purges Turn 0, and unblocks thread
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE dalton_integration_purge_ledger SET local_purge_inflight_until = clock_timestamp() - INTERVAL '1 second' WHERE purge_id = %s::uuid",
                (purge_id,),
            )
        w2_res = await asyncio.wait_for(
            finish_disconnect_locked(purge_id, revoker=None), timeout=10.0
        )
        assert w2_res is not None
        assert w2_res.local_purge_status == LocalPurgeStatus.COMPLETED

        # Thread is now unblocked (generation == 2); write brand-new Turn 1 data (query, transcript, pending steer)
        run1_id = await _seed_turn(
            pool, thread_id=tid, turn_index=1, user_text="new turn 1 after unblock"
        )
        new_tr = tt_db.StoredTranscript("fp-new-1", "cp-new-1", '{"segments": ["turn1"]}', files={})
        saved_tr = await save_stored_transcript_fenced(
            tid, "", uid, new_tr, {}, expected_purge_gen=2
        )
        assert saved_tr is True
        new_steer_id = str(uuid.uuid4())
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO hook_outbox (
                    hook_outbox_id, run_id, conversation_thread_id, hook_type,
                    payload, idempotency_key, status
                )
                VALUES (%s::uuid, %s::uuid, %s::uuid, 'steer', %s, %s, 'pending')
                """,
                (
                    new_steer_id,
                    run1_id,
                    tid,
                    Jsonb({"content": "new turn 1 pending steer"}),
                    f"ik-r8-stale-{new_steer_id}",
                ),
            )

        # Resume stale Worker 1 -> MUST detect superseded attempt_gen / unblocked thread and abort!
        w1_release.set()
        await asyncio.wait_for(w1_task, timeout=10.0)
    finally:
        w1_release.set()
        if not w1_task.done():
            w1_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await w1_task

    # Verify ALL new Turn 1 data is completely preserved!
    async with pool.connection() as conn:
        tr_cnt = await (
            await conn.execute(
                "SELECT COUNT(*) FROM thread_transcripts WHERE conversation_thread_id = %s::uuid",
                (tid,),
            )
        ).fetchone()
        steer_row = await (
            await conn.execute(
                "SELECT status, payload->>'content' FROM hook_outbox WHERE hook_outbox_id = %s::uuid",
                (new_steer_id,),
            )
        ).fetchone()
        q1_row = await (
            await conn.execute(
                "SELECT content FROM conversation_queries WHERE conversation_thread_id = %s::uuid AND turn_index = 1",
                (tid,),
            )
        ).fetchone()
        t_row = await (
            await conn.execute(
                "SELECT personal_purge_in_progress, personal_purge_generation FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
                (tid,),
            )
        ).fetchone()
    assert tr_cnt == (1,)
    assert steer_row == ("pending", "new turn 1 pending steer")
    assert q1_row == ("new turn 1 after unblock",)
    assert t_row == (False, 2)


async def test_r7_purge_redis_outside_ledger_lock_and_attempt_gen_takeover_db(
    gws_live_pool: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG (Grok Med #2 / Codex Med #1): 2A purge runs Redis cancel nudges & stream deletions strictly OUTSIDE try_purge_ledger_lock (zero PG advisory lock held), and durable local_purge_attempt_gen allows safe takeover after lease expiry without double-bumping thread generation."""
    from src.dalton.integrations.lock_guard import (
        advisory_lock_http_guard,
        held_advisory_locks,
    )
    from src.dalton.services import integration_data_purge as purge_mod
    from src.dalton.services.user_integrations_lifecycle import (
        begin_disconnect,
        finish_disconnect_locked,
    )
    from tests.integration.dalton.test_user_integrations_purge_db import (
        _seed_user_ws_thread,
    )

    pool = gws_live_pool
    uid, _, tid = await _seed_user_ws_thread(pool, user_id="u-r7-redis-purge")
    run_id = await _seed_turn(
        pool,
        thread_id=tid,
        turn_index=0,
        user_text="redis purge turn",
        status="in_progress",
    )

    conn_row = await db.upsert_connection(
        user_id=uid,
        provider="feishu",
        service="calendar",
        oauth_client_id="cli_db_test_1",
        provider_account_id="ou-r7-redis",
        granted_scopes=" ".join(FEISHU_CALENDAR_SCOPES),
        access_token="at-r7-r",
        refresh_token="rt-r7-r",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await db.record_touch(
        thread_id=tid,
        response_id=run_id,
        provider="feishu",
        service="calendar",
        auth_epoch=conn_row.auth_epoch,
        turn_index=0,
        user_id=uid,
        provider_account_id="ou-r7-redis",
    )
    purge_id = await begin_disconnect(uid, "feishu", "calendar", remote_mode="local_only")
    purge_lock_num = db.purge_lock_key(purge_id)

    nudge_seen: list[bool] = []
    redis_entered = asyncio.Event()
    redis_release = asyncio.Event()
    redis_calls: list[int] = []

    async def _check_no_pg_ledger_lock() -> None:
        assert held_advisory_locks() == ()
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM pg_locks
                    WHERE locktype = 'advisory'
                      AND (
                          (classid::bigint << 32) | (objid::bigint & x'ffffffff'::bigint)
                      ) = %s
                    """,
                    (purge_lock_num,),
                )
            ).fetchone()
        assert row is not None and row[0] == 0

    async def _stub_nudge(t_id: str, r_id: str | None, **_kw: Any) -> None:
        await _check_no_pg_ledger_lock()
        nudge_seen.append(True)

    async def _stub_del_redis(keys: list[str]) -> int:
        await _check_no_pg_ledger_lock()
        call_no = len(redis_calls) + 1
        redis_calls.append(call_no)
        if call_no == 1:
            redis_entered.set()
            await asyncio.wait_for(redis_release.wait(), timeout=10.0)
        return len(keys)

    monkeypatch.setattr(purge_mod, "publish_cancel_nudge", _stub_nudge)
    monkeypatch.setattr(purge_mod, "_delete_redis_keys", _stub_del_redis)

    with advisory_lock_http_guard(True):
        w1_task = asyncio.create_task(finish_disconnect_locked(purge_id, revoker=None))
        try:
            await asyncio.wait_for(redis_entered.wait(), timeout=10.0)
            assert len(nudge_seen) >= 1

            # While Worker 1's durable lease is active, Worker 2 skips immediately
            w2_skip = await asyncio.wait_for(finish_disconnect_locked(purge_id, revoker=None), timeout=10.0)
            assert w2_skip is None

            # Expire Worker 1's durable lease -> Worker 2 takes over cleanly (no advisory lock blocks it!)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE dalton_integration_purge_ledger SET local_purge_inflight_until = clock_timestamp() - INTERVAL '1 second' WHERE purge_id = %s::uuid",
                    (purge_id,),
                )
            w2_res = await asyncio.wait_for(finish_disconnect_locked(purge_id, revoker=None), timeout=10.0)
            assert w2_res is not None
            assert w2_res.local_purge_status == LocalPurgeStatus.COMPLETED

            # Thread was unblocked with generation == 2 (1 from Phase 1 + 1 from Worker 2 commit)
            async with pool.connection() as conn:
                trow = await (
                    await conn.execute(
                        "SELECT personal_purge_in_progress, personal_purge_generation FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
                        (tid,),
                    )
                ).fetchone()
            assert trow == (False, 2)

            # Release stale Worker 1 -> detects superseded local_purge_attempt_gen and does NOT double-bump!
            redis_release.set()
            await asyncio.wait_for(w1_task, timeout=10.0)
        finally:
            redis_release.set()
            if not w1_task.done():
                w1_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await w1_task

    async with pool.connection() as conn:
        trow_final = await (
            await conn.execute(
                "SELECT personal_purge_in_progress, personal_purge_generation FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
                (tid,),
            )
        ).fetchone()
    assert trow_final == (False, 2)
