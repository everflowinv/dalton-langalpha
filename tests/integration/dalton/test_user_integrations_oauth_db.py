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
from src.dalton.database.user_integrations import IntegrationStatus
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
    FEISHU_REVOKED_EVENT_TYPE,
    GOOGLE_CALENDAR_MIN_SCOPES,
)
from src.dalton.integrations.oauth_clients import (
    FeishuOAuthClient,
    GoogleIdTokenClaims,
    GoogleOAuthClient,
    OAuthTokenResponse,
    OAuthUpstreamError,
)
from src.dalton.services.feishu_integration_events import process_feishu_integration_event
from src.dalton.services.integration_health_sweep import run_integration_health_sweep_once
from src.dalton.services.integration_revoke import RemoteRevoker, RevokeOutcome
from src.dalton.services.user_integrations_oauth import (
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
        _, resp_id = await _seed_turn(conn, thread_id, 0, "Alice secret calendar note")
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
        granted_scopes="offline_access calendar:calendar:readonly",
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
        granted_scopes="offline_access calendar:calendar:readonly",
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
            "event": {"open_id": "ou_wh_1"},
        }
    )
    assert stale_res.status == "ignored"
    assert await db.get_connection(uid, "feishu", "calendar") is not None

    fresh_ts_ms = str(int((row.authorized_at + timedelta(seconds=5)).timestamp() * 1000))
    ev_payload = {
        "header": {
            "event_type": FEISHU_REVOKED_EVENT_TYPE,
            "event_id": "evt-db-fresh-1",
            "app_id": "cli_db_test_1",
            "create_time": fresh_ts_ms,
        },
        "event": {"open_id": "ou_wh_1", "revoke_reason": "user_revoked"},
    }
    ok_res = await process_feishu_integration_event(ev_payload)
    assert ok_res.status == "purged"
    assert len(ok_res.purge_ids) == 1
    assert await db.get_connection(uid, "feishu", "calendar") is None


async def test_health_sweep_multiworker_mutex_and_unready_creds_skip_db(
    gws_live_pool: Any,
) -> None:
    """Real PG: 12h health sweep respects PG advisory lock, interval state, invalid_grant -> needs_reauth, and skips unready creds."""
    _ = gws_live_pool
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
        granted_scopes="offline_access calendar:calendar:readonly",
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
