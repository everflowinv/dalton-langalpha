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

    r1, r2 = await asyncio.gather(
        db.consume_oauth_state(sh, "google"),
        db.consume_oauth_state(sh, "google"),
    )
    winners = [r for r in (r1, r2) if r is not None]
    assert len(winners) == 1
    assert winners[0].user_id == LOCAL_DEV_USER_ID

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

    t1, t2 = await asyncio.gather(
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

    # Case B: Sole service on old client, revoker returns RETRYABLE -> queued in retry queue -> commit supersedes under revoke_lock
    uid_b = "u-rot-retryable"
    await db.upsert_connection(
        user_id=uid_b,
        provider="google",
        service="calendar",
        oauth_client_id=client_old,
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
        uid_b, "111222333", "sub-solo", decrypt_revoke_token=True
    )
    assert len(active_b) == 1
    assert active_b[0].remote_revoke_status == RemoteRevokeStatus.FAILED_RETRYABLE
    assert active_b[0].revoke_token == "rt-solo-old"

    # Committing the new grant supersedes that retryable ledger atomically under revoke_lock
    await oauth_mod._finalize_connection(
        user_id=uid_b,
        provider="google",
        service="calendar",
        oauth_client_id=client_new,
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
