"""Unit tests for Personal Calendar & Mail Batch 1 — Step 2:
OAuth connect/callback/state, Google 3-case flows, serialized CAS refresh,
Google remote revocation HTTP client, Feishu revocation webhook, 12-hour health patrol,
credential readiness (get_credential_group), and Core vs Everflow route gating.

All upstream HTTP & DB doubles use ``create_autospec`` (no real network, no bare Mock).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import time
from typing import Any
from unittest.mock import create_autospec
from urllib.parse import parse_qs, urlparse
import uuid

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import httpx
import jwt
import pytest

from src.config.settings import LOCAL_DEV_USER_ID
from src.dalton.app import dalton_router
from src.dalton.app import integrations_router as router_mod
from src.dalton.auth.dispatch import SensitiveQueryLogFilter
from src.dalton.capabilities import (
    CapabilityId,
    capabilities_fingerprint,
    effective_capabilities,
    get_capabilities_payload,
    has_capability,
)
from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import (
    IntegrationRow,
    IntegrationStatus,
    LocalPurgeStatus,
    OAuthStateRow,
    PendingConfirmationRow,
    PurgeLedgerRow,
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
    FEISHU_INTEGRATION_EVENTS_PATH,
    FEISHU_OAUTH_CALLBACK_PATH,
    FEISHU_REVOKED_EVENT_TYPE,
    GOOGLE_CALENDAR_MIN_SCOPES,
    GOOGLE_OAUTH_CALLBACK_PATH,
    GOOGLE_SCOPE_CALENDAR_LIST_RO,
    GOOGLE_SCOPE_GMAIL_READONLY,
    PERSONAL_INTEGRATION_CONNECTOR_SPECS,
    build_feishu_integration_events_url,
    build_integration_redirect_uri,
)
from src.dalton.integrations.oauth_clients import (
    FeishuOAuthClient,
    GoogleIdTokenClaims,
    GoogleOAuthClient,
    OAuthTokenResponse,
    OAuthUpstreamError,
)
from src.dalton.services import feishu_integration_events as feishu_events_mod
from src.dalton.services import integration_health_sweep as sweep_mod
from src.dalton.services import user_integrations_oauth as oauth_mod
from src.dalton.services.feishu_integration_events import (
    FeishuEventAuthError,
    compute_feishu_event_signature,
    encrypt_feishu_event_payload,
    process_feishu_integration_event,
    verify_and_decode_feishu_event,
)
from src.dalton.services.integration_health_sweep import run_integration_health_sweep_once
from src.dalton.services.integration_revoke import RemoteRevoker, RevokeOutcome
from src.dalton.services.user_integrations_lifecycle import (
    AccountSwitchResult,
    FinishDisconnectResult,
)
from src.dalton.services.user_integrations_oauth import (
    IntegrationEpochRevokedError,
    IntegrationNeedsReauthError,
    IntegrationRevokedError,
    OAuthSessionInvalidError,
    OAuthStateInvalidError,
    PendingConfirmationInvalidError,
    confirm_pending_integration,
    ensure_fresh_access_token,
    handle_oauth_callback,
    start_oauth_authorization,
)
from src.server.services.setup_settings import environment as setup_env_mod
from src.server.utils.api import get_current_user_id


@pytest.fixture(autouse=True)
def _clean_env_and_snapshot(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Isolate deployment credential snapshot, auth mode, encryption key, and profile per test."""
    reset_active_snapshot_for_test()
    monkeypatch.setattr(setup_env_mod, "set_outside_app", create_autospec(setup_env_mod.set_outside_app, return_value=False))
    monkeypatch.setattr(setup_env_mod, "_snapshot", create_autospec(setup_env_mod._snapshot, return_value={}))
    monkeypatch.setenv("DALTON_ENABLED", "1")
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "test-byok-key-0123456789abcdef")
    monkeypatch.setenv("PUBLIC_APP_URL", "https://dalton.example.com")
    monkeypatch.delenv("DALTON_FRONTEND_URL", raising=False)
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.delenv("DALTON_PROFILE", raising=False)
    for cap in (
        "DALTON_CAP_GOOGLE_CALENDAR",
        "DALTON_CAP_FEISHU_CALENDAR",
        "DALTON_CAP_FEISHU_MAIL",
        "DALTON_CAP_GOOGLE_GMAIL",
        "DALTON_CAP_CHANNEL_GATEWAY",
    ):
        monkeypatch.delenv(cap, raising=False)
    yield
    reset_active_snapshot_for_test()


def _install_saved_credentials(
    *,
    google_client_id: str | None = "111222333-abc.apps.googleusercontent.com",
    google_client_secret: str | None = "g-secret-xyz",
    feishu_app_id: str | None = "cli_test_app_1",
    feishu_app_secret: str | None = "fs-secret-xyz",
    feishu_encrypt_key: str | None = "enc-key-secret-123456",
    feishu_verification_token: str | None = "ver-token-secret-789",
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


def _make_integration_row(
    *,
    user_id: str = LOCAL_DEV_USER_ID,
    provider: str = "google",
    service: str = "calendar",
    oauth_client_id: str = "111222333-abc.apps.googleusercontent.com",
    provider_account_id: str = "sub-alice",
    provider_union_id: str | None = None,
    provider_email: str | None = "alice@example.com",
    granted_scopes: str = " ".join(GOOGLE_CALENDAR_MIN_SCOPES),
    auth_epoch: int = 1,
    authorized_at: datetime | None = None,
    token_generation: int = 1,
    status: IntegrationStatus = IntegrationStatus.CONNECTED,
    refresh_inflight_gen: int | None = None,
    expires_at: datetime | None = None,
    access_token: str | None = "access-tok-1",
    refresh_token: str | None = "refresh-tok-1",
    preferences: dict[str, Any] | None = None,
    last_error_code: str | None = None,
) -> IntegrationRow:
    now = datetime.now(UTC)
    return IntegrationRow(
        user_id=user_id,
        provider=provider,
        service=service,
        oauth_client_id=oauth_client_id,
        provider_account_id=provider_account_id,
        provider_union_id=provider_union_id,
        provider_email=provider_email,
        granted_scopes=granted_scopes,
        auth_epoch=auth_epoch,
        authorized_at=authorized_at or (now - timedelta(hours=1)),
        token_generation=token_generation,
        status=status,
        refresh_inflight_gen=refresh_inflight_gen,
        refresh_started_at=None,
        expires_at=expires_at if expires_at is not None else (now + timedelta(hours=1)),
        last_refresh_at=None,
        needs_reauth_at=None,
        last_error_code=last_error_code,
        reauth_notified_at=None,
        preferences=dict(preferences or {}),
        has_access_token=access_token is not None,
        has_refresh_token=refresh_token is not None,
        created_at=now - timedelta(days=1),
        updated_at=now,
        access_token=access_token,
        refresh_token=refresh_token,
    )


def _make_state_row(
    *,
    raw_state: str = "state-secret-123",
    user_id: str = LOCAL_DEV_USER_ID,
    auth_mode: str = "oss",
    session_id: str | None = None,
    user_email: str | None = "alice@example.com",
    provider: str = "google",
    service: str = "calendar",
    oauth_client_id: str = "111222333-abc.apps.googleusercontent.com",
    intent: str = "connect",
    requested_scopes: str = " ".join(GOOGLE_CALENDAR_MIN_SCOPES),
    redirect_uri: str = "https://dalton.example.com" + GOOGLE_OAUTH_CALLBACK_PATH,
    return_to: str = "/settings?tab=integrations",
    expires_at: datetime | None = None,
    browser_binding_hash: str | None = None,
) -> OAuthStateRow:
    now = datetime.now(UTC)
    return OAuthStateRow(
        state_hash=hashlib.sha256(raw_state.encode()).hexdigest(),
        user_id=user_id,
        auth_mode=auth_mode,
        session_id=session_id,
        user_email=user_email,
        provider=provider,
        service=service,
        oauth_client_id=oauth_client_id,
        intent=intent,
        requested_scopes=requested_scopes,
        redirect_uri=redirect_uri,
        return_to=return_to,
        expires_at=expires_at or (now + timedelta(minutes=10)),
        created_at=now,
        code_verifier="pkce-verifier-secret-value",
        browser_binding_hash=browser_binding_hash,
    )


def _make_fake_lock_pool(
    *,
    try_lock_results: list[bool] | None = None,
    fetchone_results: list[Any] | None = None,
) -> tuple[Any, list[tuple[str, Any]]]:
    """Create a fake ``get_db_connection`` context manager tracking commits & rollbacks."""
    executed: list[tuple[str, Any]] = []
    lock_queue = list(try_lock_results) if try_lock_results is not None else [True]
    fetch_queue = list(fetchone_results or [])

    class _Cur:
        def __init__(self) -> None:
            self._last_sql = ""
            self.rowcount = 1

        async def __aenter__(self) -> _Cur:
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            self._last_sql = sql
            executed.append((sql, params))

        async def fetchone(self) -> Any:
            if "pg_try_advisory_lock" in self._last_sql:
                val = lock_queue.pop(0) if lock_queue else True
                return (val,)
            if fetch_queue:
                return fetch_queue.pop(0)
            return None

        async def fetchall(self) -> list[Any]:
            return []

    class _Conn:
        def cursor(self, **_kw: Any) -> _Cur:
            return _Cur()

        async def execute(self, sql: str, params: Any = None) -> None:
            executed.append((sql, params))

        @asynccontextmanager
        async def transaction(self) -> Any:
            try:
                yield
            except BaseException:
                executed.append(("TX_ROLLBACK", None))
                raise
            else:
                executed.append(("TX_COMMIT", None))

    @asynccontextmanager
    async def _get_conn(_existing: Any = None) -> Any:
        yield _Conn()

    return _get_conn, executed


def _make_app(user_id: str = LOCAL_DEV_USER_ID) -> FastAPI:
    app = FastAPI()
    app.include_router(dalton_router)
    app.dependency_overrides[get_current_user_id] = create_autospec(
        get_current_user_id, return_value=user_id
    )
    return app


# ---------------------------------------------------------------------------
# 1. State single-use consumption: replay, expiry, cross-user, concurrent workers, 3 auth modes
# ---------------------------------------------------------------------------


class TestOAuthStateAndSessionVerification:
    @pytest.mark.asyncio
    async def test_state_single_use_replay_expiry_and_concurrent_two_workers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_saved_credentials()
        raw_state = "one-time-state-secret-token"
        state_hash = hashlib.sha256(raw_state.encode()).hexdigest()

        # Simulate atomic DELETE ... RETURNING in PostgreSQL shared across 2 concurrent workers
        states_store: dict[tuple[str, str], OAuthStateRow] = {
            (state_hash, "google"): _make_state_row(raw_state=raw_state)
        }
        consume_lock = asyncio.Lock()

        async def _atomic_consume(sh: str, prov: str, *, conn: Any = None) -> OAuthStateRow | None:
            async with consume_lock:
                row = states_store.pop((sh, prov), None)
                if row is not None and row.expires_at <= datetime.now(UTC):
                    return None
                return row

        monkeypatch.setattr(
            db, "consume_oauth_state", create_autospec(db.consume_oauth_state, side_effect=_atomic_consume)
        )
        monkeypatch.setattr(
            db, "delete_oauth_state", create_autospec(db.delete_oauth_state, return_value=None)
        )
        monkeypatch.setattr(
            db,
            "has_inflight_google_revoke_in_project",
            create_autospec(db.has_inflight_google_revoke_in_project, return_value=False),
        )
        fake_conn_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_conn_ctx)
        monkeypatch.setattr(
            db, "supersede_active_revokes_for_google_project_sub_tx", create_autospec(db.supersede_active_revokes_for_google_project_sub_tx, return_value=0)
        )
        monkeypatch.setattr(
            db, "insert_pending_confirmation", create_autospec(db.insert_pending_confirmation, return_value=None)
        )
        monkeypatch.setattr(
            db, "delete_pending_confirmation_by_hash", create_autospec(db.delete_pending_confirmation_by_hash, return_value=None)
        )
        monkeypatch.setattr(
            db, "get_connection", create_autospec(db.get_connection, return_value=None)
        )

        saved_row = _make_integration_row()
        monkeypatch.setattr(
            oauth_mod,
            "_finalize_connection",
            create_autospec(oauth_mod._finalize_connection, return_value=(saved_row, None)),
        )

        g_client = create_autospec(GoogleOAuthClient, instance=True)
        g_client.exchange_code.return_value = OAuthTokenResponse(
            access_token="at-new",
            refresh_token="rt-new",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token="fake.id.token",
        )
        g_client.verify_id_token.return_value = GoogleIdTokenClaims(
            sub="sub-alice",
            email="alice@example.com",
            email_verified=True,
            aud="111222333-abc.apps.googleusercontent.com",
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )

        # Two workers concurrently receive callback with the same state
        results = await asyncio.gather(
            handle_oauth_callback(provider="google", code="code-1", state=raw_state, google_client=g_client),
            handle_oauth_callback(provider="google", code="code-1", state=raw_state, google_client=g_client),
            return_exceptions=True,
        )
        successes = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(successes) == 1
        assert successes[0].status == "connected"
        assert len(failures) == 1
        assert isinstance(failures[0], OAuthStateInvalidError)
        # Upstream code exchange called only ONCE
        assert g_client.exchange_code.await_count == 1

        # Replay after consumption is rejected
        with pytest.raises(OAuthStateInvalidError):
            await handle_oauth_callback(provider="google", code="code-1", state=raw_state, google_client=g_client)

        # Expired state is rejected
        expired_state = "expired-state-xyz"
        expired_hash = hashlib.sha256(expired_state.encode()).hexdigest()
        states_store[(expired_hash, "google")] = _make_state_row(
            raw_state=expired_state,
            expires_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        with pytest.raises(OAuthStateInvalidError):
            await handle_oauth_callback(provider="google", code="code-2", state=expired_state, google_client=g_client)

    @pytest.mark.asyncio
    async def test_three_auth_modes_local_oss_supabase_and_cross_user_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_saved_credentials()
        g_client = create_autospec(GoogleOAuthClient, instance=True)
        g_client.exchange_code.return_value = OAuthTokenResponse(
            access_token="at-1",
            refresh_token="rt-1",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token="fake.id.token",
        )
        g_client.verify_id_token.return_value = GoogleIdTokenClaims(
            sub="sub-alice",
            email="alice@example.com",
            email_verified=True,
            aud="111222333-abc.apps.googleusercontent.com",
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )

        # 1) local mode: revoked/expired/password-changed session rejected via verify_local_session_active
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        consume_mock = create_autospec(
            db.consume_oauth_state,
            return_value=_make_state_row(
                user_id="user-local-1", auth_mode="local", session_id="sid-revoked"
            ),
        )
        monkeypatch.setattr(db, "consume_oauth_state", consume_mock)
        monkeypatch.setattr(db, "delete_oauth_state", create_autospec(db.delete_oauth_state, return_value=None))
        monkeypatch.setattr(db, "has_inflight_google_revoke_in_project", create_autospec(db.has_inflight_google_revoke_in_project, return_value=False))
        verify_local_spy = create_autospec(
            oauth_mod.verify_local_session_active,
            side_effect=HTTPException(status_code=401, detail="Session has been revoked"),
        )
        monkeypatch.setattr(oauth_mod, "verify_local_session_active", verify_local_spy)
        with pytest.raises(OAuthSessionInvalidError):
            await handle_oauth_callback(
                provider="google", code="c1", state="s1", google_client=g_client
            )
        verify_local_spy.assert_awaited_once_with(user_id="user-local-1", session_id="sid-revoked")
        assert g_client.exchange_code.await_count == 0

        # 2) oss mode: cross-user state rejected
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
        consume_mock.return_value = _make_state_row(
            user_id="some-other-user", auth_mode="oss"
        )
        with pytest.raises(OAuthSessionInvalidError):
            await handle_oauth_callback(
                provider="google", code="c2", state="s2", google_client=g_client
            )

        # 3) supabase mode: callback stages 5-min pending confirmation; cross-user confirm fails; owner confirm succeeds
        monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
        consume_mock.return_value = _make_state_row(
            user_id="sb-user-1",
            auth_mode="supabase",
            user_email="alice@example.com",
        )
        monkeypatch.setattr(
            oauth_mod,
            "is_team_member",
            create_autospec(oauth_mod.is_team_member, return_value=True),
        )
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=None))

        fake_conn_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_conn_ctx)
        supersede_tx_spy = create_autospec(
            db.supersede_active_revokes_for_google_project_sub_tx, return_value=0
        )
        monkeypatch.setattr(
            db, "supersede_active_revokes_for_google_project_sub_tx", supersede_tx_spy
        )

        staged: dict[tuple[str, str], PendingConfirmationRow] = {}

        async def _insert_pending(**kw: Any) -> None:
            staged[(kw["confirmation_hash"], kw["user_id"])] = PendingConfirmationRow(
                confirmation_hash=kw["confirmation_hash"],
                user_id=kw["user_id"],
                provider=kw["provider"],
                service=kw["service"],
                oauth_client_id=kw["oauth_client_id"],
                provider_account_id=kw["provider_account_id"],
                provider_union_id=kw["provider_union_id"],
                provider_email=kw["provider_email"],
                granted_scopes=kw["granted_scopes"],
                expires_at=kw["expires_at"],
                intent=kw["intent"],
                preferences=kw["preferences"] or {},
                confirm_expires_at=kw["confirm_expires_at"],
                created_at=datetime.now(UTC),
                access_token=kw["access_token"],
                refresh_token=kw["refresh_token"],
                expected_old_oauth_client_id=kw.get("expected_old_oauth_client_id"),
                expected_old_provider_account_id=kw.get("expected_old_provider_account_id"),
                expected_old_auth_epoch=kw.get("expected_old_auth_epoch"),
            )

        async def _peek_pending(ch: str, uid: str, *, conn: Any = None) -> PendingConfirmationRow | None:
            return staged.get((ch, uid))

        async def _consume_pending(ch: str, uid: str, *, conn: Any = None) -> PendingConfirmationRow | None:
            return staged.pop((ch, uid), None)

        monkeypatch.setattr(db, "insert_pending_confirmation", create_autospec(db.insert_pending_confirmation, side_effect=_insert_pending))
        monkeypatch.setattr(db, "peek_pending_confirmation", create_autospec(db.peek_pending_confirmation, side_effect=_peek_pending))
        monkeypatch.setattr(db, "consume_pending_confirmation", create_autospec(db.consume_pending_confirmation, side_effect=_consume_pending))

        cb = await handle_oauth_callback(provider="google", code="c3", state="s3", google_client=g_client)
        assert cb.status == "confirmation_required"
        assert cb.confirmation_token

        # Cross-user confirmation MUST fail
        with pytest.raises(PendingConfirmationInvalidError):
            await confirm_pending_integration(
                user_id="sb-other-user", confirmation_token=cb.confirmation_token
            )

        # Owner confirmation succeeds
        saved = _make_integration_row(user_id="sb-user-1")
        monkeypatch.setattr(
            oauth_mod,
            "_finalize_connection",
            create_autospec(oauth_mod._finalize_connection, return_value=(saved, None)),
        )
        conf = await confirm_pending_integration(
            user_id="sb-user-1", confirmation_token=cb.confirmation_token
        )
        assert conf.status == "connected"
        assert conf.connection is saved

    def test_callback_query_redaction_and_safe_repr(self) -> None:
        tok_resp = OAuthTokenResponse(
            access_token="SECRET_ACCESS_XYZ",
            refresh_token="SECRET_REFRESH_XYZ",
            id_token="SECRET_ID_TOKEN_XYZ",
        )
        assert "SECRET_ACCESS_XYZ" not in repr(tok_resp)
        assert "SECRET_REFRESH_XYZ" not in repr(tok_resp)
        assert "SECRET_ID_TOKEN_XYZ" not in repr(tok_resp)

        # Log filter scrubs code= and state= on callback URLs
        filt = SensitiveQueryLogFilter()
        rec = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='GET /api/v1/dalton/integrations/google/callback?code=%s&state=%s 302',
            args=("secret_code_val", "secret_state_val"),
            exc_info=None,
        )
        rec.msg = "GET /api/v1/dalton/integrations/google/callback?code=secret_code_val&state=secret_state_val 302"
        rec.args = ()
        filt.filter(rec)
        assert "secret_code_val" not in rec.msg
        assert "secret_state_val" not in rec.msg
        assert "code=REDACTED" in rec.msg
        assert "state=REDACTED" in rec.msg


# ---------------------------------------------------------------------------
# 2. Google 3 cases: first connect, same-sub client rotation, account switch confirm -> 2A purge
# ---------------------------------------------------------------------------


class TestGoogleThreeCases:
    @pytest.mark.asyncio
    async def test_google_first_connect_min_scope_rejection_and_gmail_partial_grant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_saved_credentials()
        g_client = create_autospec(GoogleOAuthClient, instance=True)
        g_client.verify_id_token.return_value = GoogleIdTokenClaims(
            sub="sub-alice",
            email="alice@example.com",
            email_verified=True,
            aud="111222333-abc.apps.googleusercontent.com",
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )

        # Calendar missing calendar.events -> rejected, never saved
        consume_mock = create_autospec(
            db.consume_oauth_state, return_value=_make_state_row(service="calendar")
        )
        monkeypatch.setattr(db, "consume_oauth_state", consume_mock)
        monkeypatch.setattr(db, "delete_oauth_state", create_autospec(db.delete_oauth_state, return_value=None))
        monkeypatch.setattr(db, "has_inflight_google_revoke_in_project", create_autospec(db.has_inflight_google_revoke_in_project, return_value=False))
        fake_conn_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_conn_ctx)
        monkeypatch.setattr(
            db, "supersede_active_revokes_for_google_project_sub_tx", create_autospec(db.supersede_active_revokes_for_google_project_sub_tx, return_value=0)
        )
        monkeypatch.setattr(
            db, "insert_pending_confirmation", create_autospec(db.insert_pending_confirmation, return_value=None)
        )
        monkeypatch.setattr(
            db, "delete_pending_confirmation_by_hash", create_autospec(db.delete_pending_confirmation_by_hash, return_value=None)
        )
        g_client.exchange_code.return_value = OAuthTokenResponse(
            access_token="at-1",
            refresh_token="rt-1",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=GOOGLE_SCOPE_CALENDAR_LIST_RO,  # missing calendar.events!
            id_token="id.tok",
        )
        res_missing = await handle_oauth_callback(
            provider="google", code="c1", state="s1", google_client=g_client
        )
        assert res_missing.status == "error"
        assert res_missing.error_code == "missing_required_scopes"

        # Gmail partial grant (draft scope unchecked) -> succeeds with allow_create_draft=False & partial_grant=True
        consume_mock.return_value = _make_state_row(
            service="mail",
            requested_scopes=f"{GOOGLE_SCOPE_GMAIL_READONLY} https://www.googleapis.com/auth/gmail.drafts.create",
        )
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=None))
        g_client.exchange_code.return_value = OAuthTokenResponse(
            access_token="at-2",
            refresh_token="rt-2",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=GOOGLE_SCOPE_GMAIL_READONLY,
            id_token="id.tok",
        )
        captured_prefs: list[dict[str, Any]] = []

        async def _capture_finalize(**kw: Any) -> tuple[IntegrationRow, None]:
            captured_prefs.append(dict(kw["preferences"]))
            return (
                _make_integration_row(
                    service="mail",
                    granted_scopes=kw["granted_scopes"],
                    preferences=kw["preferences"],
                ),
                None,
            )

        monkeypatch.setattr(
            oauth_mod,
            "_finalize_connection",
            create_autospec(oauth_mod._finalize_connection, side_effect=_capture_finalize),
        )
        res_gmail = await handle_oauth_callback(
            provider="google", code="c2", state="s2", google_client=g_client
        )
        assert res_gmail.status == "connected"
        assert res_gmail.partial_grant is True
        assert captured_prefs == [{"allow_create_draft": False}]

    @pytest.mark.asyncio
    async def test_google_same_sub_client_rotation_rebinds_in_place_and_preserves_history(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old_client = "111222333-old.apps.googleusercontent.com"
        new_client = "111222333-new.apps.googleusercontent.com"
        _install_saved_credentials(google_client_id=new_client)

        old_row = _make_integration_row(
            oauth_client_id=old_client,
            provider_account_id="sub-alice",
            auth_epoch=7,
            status=IntegrationStatus.NEEDS_REAUTH,
        )
        fake_conn_ctx, _ = _make_fake_lock_pool(
            fetchone_results=[{"refresh_token": b"enc-old-rt", "access_token": b"enc-old-at"}]
        )
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_conn_ctx)
        monkeypatch.setattr(
            db, "has_running_local_purge_for_service", create_autospec(db.has_running_local_purge_for_service, return_value=[])
        )
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=old_row))
        monkeypatch.setattr(db, "list_connections_by_account", create_autospec(db.list_connections_by_account, return_value=[]))
        monkeypatch.setattr(db, "get_sibling_connections", create_autospec(db.get_sibling_connections, return_value=[]))

        rebound_row = _make_integration_row(
            oauth_client_id=new_client,
            provider_account_id="sub-alice",
            auth_epoch=8,
            token_generation=1,
            status=IntegrationStatus.CONNECTED,
        )
        upsert_spy = create_autospec(db.upsert_connection, return_value=rebound_row)
        ledger_spy = create_autospec(
            db.insert_purge_ledger,
            return_value=PurgeLedgerRow(
                purge_id=str(uuid.uuid4()),
                user_id=LOCAL_DEV_USER_ID,
                provider="google",
                service="calendar",
                oauth_client_id=old_client,
                provider_account_id="sub-alice",
                auth_epoch_upto=7,
                trigger_reason=TriggerReason.CLIENT_ROTATION,
                local_purge_status=LocalPurgeStatus.NOT_REQUIRED,
                purged_max_turn_by_thread={},
                tracker_reconcile_needed=False,
                local_completed_at=datetime.now(UTC),
                remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
                has_revoke_token=False,
                retry_count=0,
                next_retry_at=None,
                last_remote_error=None,
                remote_completed_at=datetime.now(UTC),
                disconnected_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            ),
        )
        supersede_tx_spy = create_autospec(
            db.supersede_active_revokes_for_google_project_sub_tx, return_value=1
        )
        monkeypatch.setattr(db, "upsert_connection", upsert_spy)
        monkeypatch.setattr(db, "insert_purge_ledger", ledger_spy)
        monkeypatch.setattr(
            db, "supersede_active_revokes_for_google_project_sub_tx", supersede_tx_spy
        )

        saved, purge_id = await oauth_mod._finalize_connection(
            user_id=LOCAL_DEV_USER_ID,
            provider="google",
            service="calendar",
            oauth_client_id=new_client,
            provider_account_id="sub-alice",
            provider_union_id=None,
            provider_email="alice@example.com",
            granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            access_token="at-new",
            refresh_token="rt-new",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            preferences={},
            allow_account_switch=False,
        )
        assert saved.oauth_client_id == new_client
        assert purge_id is None
        # Upsert rebinds IN PLACE with reset_token_generation=True & bump_auth_epoch=True
        upsert_kwargs = upsert_spy.await_args.kwargs
        assert upsert_kwargs["oauth_client_id"] == new_client
        assert upsert_kwargs["reset_token_generation"] is True
        assert upsert_kwargs["bump_auth_epoch"] is True
        # Ledger marks local_purge_status = NOT_REQUIRED (history preserved!) and same-project remote_revoke = SUPERSEDED
        ledger_kwargs = ledger_spy.await_args.kwargs
        assert ledger_kwargs["trigger_reason"] == TriggerReason.CLIENT_ROTATION
        assert ledger_kwargs["local_purge_status"] == LocalPurgeStatus.NOT_REQUIRED
        assert ledger_kwargs["remote_revoke_status"] == RemoteRevokeStatus.SUPERSEDED
        assert ledger_kwargs["encrypted_revoke_token"] is None
        # Pending revokes were superseded INSIDE the commit transaction under cross-user (project, sub) revoke_lock
        supersede_tx_spy.assert_awaited_once()
        assert supersede_tx_spy.await_args.args[:2] == ("111222333", "sub-alice")
        assert supersede_tx_spy.await_args.kwargs.get("conn") is not None

    @pytest.mark.asyncio
    async def test_google_account_switch_returns_needs_confirm_then_runs_2a_and_saves_new(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_saved_credentials()
        old_row = _make_integration_row(
            provider_account_id="sub-alice",
            provider_email="alice@example.com",
            auth_epoch=3,
        )
        new_row = _make_integration_row(
            provider_account_id="sub-bob",
            provider_email="bob@example.com",
            auth_epoch=4,
            token_generation=1,
        )

        g_client = create_autospec(GoogleOAuthClient, instance=True)
        g_client.exchange_code.return_value = OAuthTokenResponse(
            access_token="at-bob",
            refresh_token="rt-bob",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token="id.bob",
        )
        g_client.verify_id_token.return_value = GoogleIdTokenClaims(
            sub="sub-bob",
            email="bob@example.com",
            email_verified=True,
            aud="111222333-abc.apps.googleusercontent.com",
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )

        monkeypatch.setattr(
            db, "consume_oauth_state", create_autospec(db.consume_oauth_state, return_value=_make_state_row())
        )
        monkeypatch.setattr(
            db, "delete_oauth_state", create_autospec(db.delete_oauth_state, return_value=None)
        )
        monkeypatch.setattr(
            db, "has_inflight_google_revoke_in_project", create_autospec(db.has_inflight_google_revoke_in_project, return_value=False)
        )
        current_conn = [old_row]
        monkeypatch.setattr(
            db, "get_connection", create_autospec(db.get_connection, side_effect=lambda *a, **k: current_conn[0])
        )
        monkeypatch.setattr(
            db, "has_running_local_purge_for_service", create_autospec(db.has_running_local_purge_for_service, return_value=[])
        )

        fake_conn_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_conn_ctx)
        supersede_tx_spy = create_autospec(
            db.supersede_active_revokes_for_google_project_sub_tx, return_value=0
        )
        monkeypatch.setattr(
            db, "supersede_active_revokes_for_google_project_sub_tx", supersede_tx_spy
        )

        staged: dict[tuple[str, str], PendingConfirmationRow] = {}

        async def _insert_pending(**kw: Any) -> None:
            staged[(kw["confirmation_hash"], kw["user_id"])] = PendingConfirmationRow(
                confirmation_hash=kw["confirmation_hash"],
                user_id=kw["user_id"],
                provider=kw["provider"],
                service=kw["service"],
                oauth_client_id=kw["oauth_client_id"],
                provider_account_id=kw["provider_account_id"],
                provider_union_id=kw["provider_union_id"],
                provider_email=kw["provider_email"],
                granted_scopes=kw["granted_scopes"],
                expires_at=kw["expires_at"],
                intent=kw["intent"],
                preferences=kw["preferences"] or {},
                confirm_expires_at=kw["confirm_expires_at"],
                created_at=datetime.now(UTC),
                access_token=kw["access_token"],
                refresh_token=kw["refresh_token"],
                expected_old_oauth_client_id=kw.get("expected_old_oauth_client_id"),
                expected_old_provider_account_id=kw.get("expected_old_provider_account_id"),
                expected_old_auth_epoch=kw.get("expected_old_auth_epoch"),
            )

        monkeypatch.setattr(db, "insert_pending_confirmation", create_autospec(db.insert_pending_confirmation, side_effect=_insert_pending))
        monkeypatch.setattr(db, "peek_pending_confirmation", create_autospec(db.peek_pending_confirmation, side_effect=lambda h, u, **k: staged.get((h, u))))
        monkeypatch.setattr(db, "consume_pending_confirmation", create_autospec(db.consume_pending_confirmation, side_effect=lambda h, u, **k: staged.pop((h, u), None)))

        # Step A: callback discovers sub-bob != sub-alice -> returns account_switch_confirmation_required
        cb = await handle_oauth_callback(
            provider="google", code="c-bob", state="s-bob", google_client=g_client
        )
        assert cb.status == "account_switch_confirmation_required"
        assert cb.previous_account_email == "alice@example.com"
        assert cb.new_account_email == "bob@example.com"
        assert cb.confirmation_token

        # Step B: confirm without confirm_account_switch=True -> returns needs-confirm WITHOUT consuming token
        unconfirmed = await confirm_pending_integration(
            user_id=LOCAL_DEV_USER_ID,
            confirmation_token=cb.confirmation_token,
            confirm_account_switch=False,
        )
        assert unconfirmed.status == "account_switch_confirmation_required"
        assert len(staged) == 1

        # Step C: confirm with confirm_account_switch=True -> calls Step 1 replace_connection_for_account_switch + finish_disconnect
        purge_uuid = str(uuid.uuid4())

        async def _fake_replace(*args: Any, **kwargs: Any) -> AccountSwitchResult:
            current_conn[0] = new_row
            return AccountSwitchResult(
                purge_id=purge_uuid,
                old_auth_epoch=3,
                new_auth_epoch=4,
                remote_revoke_status=RemoteRevokeStatus.PENDING,
                affected_threads={"t-1": {"min_turn": 1, "max_turn": 3}},
            )

        replace_spy = create_autospec(
            oauth_mod.replace_connection_for_account_switch, side_effect=_fake_replace
        )
        finish_spy = create_autospec(
            oauth_mod.finish_disconnect,
            return_value=FinishDisconnectResult(
                purge_id=purge_uuid,
                deleted_connections=0,
                local_purge_status=LocalPurgeStatus.COMPLETED,
                remote_revoke_status=RemoteRevokeStatus.SUCCEEDED,
            ),
        )
        revoker = create_autospec(RemoteRevoker, instance=True)

        monkeypatch.setattr(oauth_mod, "replace_connection_for_account_switch", replace_spy)
        monkeypatch.setattr(oauth_mod, "finish_disconnect", finish_spy)

        confirmed = await confirm_pending_integration(
            user_id=LOCAL_DEV_USER_ID,
            confirmation_token=cb.confirmation_token,
            confirm_account_switch=True,
            revoker=revoker,
        )
        assert confirmed.status == "connected"
        assert confirmed.purge_id == purge_uuid
        assert confirmed.connection is not None
        assert confirmed.connection.provider_account_id == "sub-bob"
        replace_spy.assert_awaited_once()
        assert replace_spy.await_args.kwargs["expected_old_oauth_client_id"] == old_row.oauth_client_id
        assert replace_spy.await_args.kwargs["expected_old_provider_account_id"] == old_row.provider_account_id
        assert replace_spy.await_args.kwargs["expected_old_auth_epoch"] == old_row.auth_epoch
        finish_spy.assert_awaited_once_with(purge_uuid, revoker=revoker)
        # Staging the pending confirmation coordinated under revoke_lock and superseded old revokes
        supersede_tx_spy.assert_awaited_once()
        assert supersede_tx_spy.await_args.args[:2] == ("111222333", "sub-bob")


# ---------------------------------------------------------------------------
# 3. Serialized refresh, CAS 3-element check, Feishu refresh_ambiguous & revocation split
# ---------------------------------------------------------------------------


class TestSerializedRefreshAndCAS:
    @pytest.mark.asyncio
    async def test_concurrent_refresh_sends_only_one_upstream_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_saved_credentials()
        expired_row = _make_integration_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_alice",
            expires_at=datetime.now(UTC) - timedelta(seconds=10),
            token_generation=1,
            access_token="old-at",
            refresh_token="rt-single-use-1",
        )
        refreshed_row = _make_integration_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_alice",
            expires_at=datetime.now(UTC) + timedelta(hours=2),
            token_generation=2,
            access_token="new-at-2",
            refresh_token="rt-single-use-2",
        )
        state_box = {"row": expired_row}
        fake_conn_ctx, _ = _make_fake_lock_pool(try_lock_results=[True, False])
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_conn_ctx)
        monkeypatch.setattr(
            db, "get_connection", create_autospec(db.get_connection, side_effect=lambda *a, **k: state_box["row"])
        )

        f_client = create_autospec(FeishuOAuthClient, instance=True)

        async def _slow_refresh(**_kw: Any) -> OAuthTokenResponse:
            await asyncio.sleep(0.05)
            return OAuthTokenResponse(
                access_token="new-at-2",
                refresh_token="rt-single-use-2",
                expires_in=7200,
                expires_at=refreshed_row.expires_at,
            )

        f_client.refresh_token.side_effect = _slow_refresh

        async def _fake_commit(**_kw: Any) -> oauth_mod.FreshAccessToken:
            state_box["row"] = refreshed_row
            return oauth_mod._to_fresh_token(refreshed_row)

        monkeypatch.setattr(
            oauth_mod,
            "_commit_refresh_success_tx",
            create_autospec(oauth_mod._commit_refresh_success_tx, side_effect=_fake_commit),
        )

        t1, t2 = await asyncio.gather(
            ensure_fresh_access_token(
                LOCAL_DEV_USER_ID,
                "feishu",
                "calendar",
                feishu_client=f_client,
                waiter_budget_seconds=2.0,
                waiter_poll_interval_seconds=0.02,
            ),
            ensure_fresh_access_token(
                LOCAL_DEV_USER_ID,
                "feishu",
                "calendar",
                feishu_client=f_client,
                waiter_budget_seconds=2.0,
                waiter_poll_interval_seconds=0.02,
            ),
        )
        assert f_client.refresh_token.await_count == 1
        assert t1.access_token == "new-at-2"
        assert t2.access_token == "new-at-2"

    @pytest.mark.asyncio
    async def test_cas_conflict_three_element_check_allows_same_identity_and_blocks_epoch_change(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_saved_credentials()
        fake_conn_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_conn_ctx)

        # Case A: token_generation advanced (1 -> 2), but (client, sub, auth_epoch) identical -> returns DB token
        same_identity_newer_gen = _make_integration_row(
            oauth_client_id="111222333-abc.apps.googleusercontent.com",
            provider_account_id="sub-alice",
            auth_epoch=5,
            token_generation=2,
            access_token="latest-db-token",
        )
        list_by_acct_mock = create_autospec(
            db.list_connections_by_account, return_value=[same_identity_newer_gen]
        )
        monkeypatch.setattr(db, "list_connections_by_account", list_by_acct_mock)
        tok = await oauth_mod._commit_refresh_success_tx(
            user_id=LOCAL_DEV_USER_ID,
            provider="google",
            service="calendar",
            start_client_id="111222333-abc.apps.googleusercontent.com",
            start_sub="sub-alice",
            start_auth_epoch=5,
            start_token_gen=1,
            token_resp=OAuthTokenResponse(access_token="stale-token"),
            required_scopes=None,
        )
        assert tok.access_token == "latest-db-token"
        assert tok.token_generation == 2

        # Case B: concurrent account switch changed sub & auth_epoch -> MUST raise IntegrationEpochRevokedError
        switched_row = _make_integration_row(
            oauth_client_id="111222333-abc.apps.googleusercontent.com",
            provider_account_id="sub-bob",
            auth_epoch=6,
            token_generation=1,
            access_token="bobs-secret-token",
        )
        list_by_acct_mock.return_value = []
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=switched_row))
        with pytest.raises(IntegrationEpochRevokedError):
            await oauth_mod._commit_refresh_success_tx(
                user_id=LOCAL_DEV_USER_ID,
                provider="google",
                service="calendar",
                start_client_id="111222333-abc.apps.googleusercontent.com",
                start_sub="sub-alice",
                start_auth_epoch=5,
                start_token_gen=1,
                token_resp=OAuthTokenResponse(access_token="stale-alice-token"),
                required_scopes=None,
            )

    @pytest.mark.asyncio
    async def test_feishu_refresh_ambiguous_and_strict_revocation_branching(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_saved_credentials()
        fake_conn_ctx, _ = _make_fake_lock_pool(try_lock_results=[True, True, True, True])
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_conn_ctx)

        status_updates: list[dict[str, Any]] = []

        async def _capture_status(*args: Any, **kwargs: Any) -> bool:
            status_updates.append({"args": args, **kwargs})
            return True

        monkeypatch.setattr(
            db, "update_connection_status", create_autospec(db.update_connection_status, side_effect=_capture_status)
        )
        f_client = create_autospec(FeishuOAuthClient, instance=True)

        # 1) Ambiguous timeout while access token STILL valid -> marks refresh_ambiguous & returns existing access token
        valid_exp_row = _make_integration_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_1",
            expires_at=datetime.now(UTC) + timedelta(seconds=120),
            access_token="still-valid-at",
        )
        get_conn_mock = create_autospec(db.get_connection, return_value=valid_exp_row)
        monkeypatch.setattr(db, "get_connection", get_conn_mock)
        f_client.refresh_token.side_effect = OAuthUpstreamError(
            provider="feishu", kind="ambiguous", error_code="readtimeout"
        )
        tok = await ensure_fresh_access_token(
            LOCAL_DEV_USER_ID,
            "feishu",
            "calendar",
            min_remaining_seconds=600,
            feishu_client=f_client,
        )
        assert tok.access_token == "still-valid-at"
        assert status_updates[-1]["status"] == IntegrationStatus.REFRESH_AMBIGUOUS

        # 2) Ambiguous timeout AFTER access token expiry -> converges directly to needs_reauth
        expired_row = _make_integration_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_1",
            expires_at=datetime.now(UTC) - timedelta(seconds=5),
            status=IntegrationStatus.REFRESH_AMBIGUOUS,
        )
        get_conn_mock.return_value = expired_row
        with pytest.raises(IntegrationNeedsReauthError) as exc_info:
            await ensure_fresh_access_token(
                LOCAL_DEV_USER_ID, "feishu", "calendar", feishu_client=f_client
            )
        assert exc_info.value.code == "refresh_timeout_after_expiry"
        assert status_updates[-1]["status"] == IntegrationStatus.NEEDS_REAUTH

        # 3) Feishu 20064 / Google invalid_grant -> needs_reauth ONLY (never triggers 2A)
        begin_disc_spy = create_autospec(oauth_mod.begin_disconnect, return_value="purge-1")
        finish_disc_spy = create_autospec(oauth_mod.finish_disconnect, return_value=None)
        monkeypatch.setattr(oauth_mod, "begin_disconnect", begin_disc_spy)
        monkeypatch.setattr(oauth_mod, "finish_disconnect", finish_disc_spy)

        f_client.refresh_token.side_effect = OAuthUpstreamError(
            provider="feishu", kind="needs_reauth", error_code="feishu_20064", feishu_code=20064
        )
        with pytest.raises(IntegrationNeedsReauthError):
            await ensure_fresh_access_token(
                LOCAL_DEV_USER_ID, "feishu", "calendar", feishu_client=f_client
            )
        begin_disc_spy.assert_not_awaited()

        # 4) Feishu confirmed user revocation code -> atomic Phase 1 via begin_disconnect + finish_disconnect
        f_client.refresh_token.side_effect = OAuthUpstreamError(
            provider="feishu", kind="revoked", error_code="feishu_confirmed_rev", feishu_code=99999
        )
        with pytest.raises(IntegrationRevokedError):
            await ensure_fresh_access_token(
                LOCAL_DEV_USER_ID, "feishu", "calendar", feishu_client=f_client
            )
        begin_disc_spy.assert_awaited_once_with(
            LOCAL_DEV_USER_ID,
            "feishu",
            "calendar",
            trigger_reason=TriggerReason.ERROR_CODE_REVOKED,
            remote_mode="local_only",
            expected_oauth_client_id="cli_test_app_1",
            expected_provider_account_id="ou_1",
            expected_auth_epoch=expired_row.auth_epoch,
            expected_token_generation=expired_row.token_generation,
            last_error_code="feishu_confirmed_rev",
            clear_tokens=True,
        )
        finish_disc_spy.assert_awaited_once_with("purge-1", revoker=None)


# ---------------------------------------------------------------------------
# 4. Google HTTP revoke client, OIDC JWKS verification, & (Google project, sub) coordination
# ---------------------------------------------------------------------------


class TestGoogleHttpRevokerAndCoordination:
    @pytest.mark.asyncio
    async def test_google_revoke_http_status_classification(self) -> None:
        http_double = create_autospec(httpx.AsyncClient, instance=True)
        client = GoogleOAuthClient(http_client=http_double)

        http_double.post.return_value = httpx.Response(200)
        assert (
            await client.revoke(provider="google", oauth_client_id="123-a", token="tok-1")
            == RevokeOutcome.SUCCEEDED
        )

        http_double.post.return_value = httpx.Response(400, json={"error": "invalid_token"})
        assert (
            await client.revoke(provider="google", oauth_client_id="123-a", token="tok-2")
            == RevokeOutcome.SUCCEEDED
        )

        http_double.post.return_value = httpx.Response(503)
        assert (
            await client.revoke(provider="google", oauth_client_id="123-a", token="tok-3")
            == RevokeOutcome.RETRYABLE
        )

        http_double.post.side_effect = httpx.ReadTimeout("timeout")
        assert (
            await client.revoke(provider="google", oauth_client_id="123-a", token="tok-4")
            == RevokeOutcome.RETRYABLE
        )

    @pytest.mark.asyncio
    async def test_google_id_token_jwks_signature_verification(self) -> None:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
        public_jwk["kid"] = "k-test-1"
        public_jwk["use"] = "sig"
        public_jwk["alg"] = "RS256"

        http_double = create_autospec(httpx.AsyncClient, instance=True)
        http_double.get.return_value = httpx.Response(200, json={"keys": [public_jwk]})
        client = GoogleOAuthClient(http_client=http_double)

        id_tok = jwt.encode(
            {
                "iss": "https://accounts.google.com",
                "aud": "111222333-abc.apps.googleusercontent.com",
                "sub": "google-sub-999",
                "email": "Alice@Example.com",
                "email_verified": True,
                "exp": int(time.time()) + 600,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "k-test-1"},
        )
        claims = await client.verify_id_token(
            id_token=id_tok, expected_aud="111222333-abc.apps.googleusercontent.com"
        )
        assert claims.sub == "google-sub-999"
        assert claims.email == "alice@example.com"
        assert claims.email_verified is True

    @pytest.mark.asyncio
    async def test_start_oauth_authorization_coordinates_pre_auth_revoke_by_project_sub(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_saved_credentials(google_client_id="111222333-new.apps.googleusercontent.com")
        pending_same_project = PurgeLedgerRow(
            purge_id=str(uuid.uuid4()),
            user_id=LOCAL_DEV_USER_ID,
            provider="google",
            service="calendar",
            oauth_client_id="111222333-old.apps.googleusercontent.com",
            provider_account_id="sub-alice",
            auth_epoch_upto=2,
            trigger_reason=TriggerReason.USER_DISCONNECT,
            local_purge_status=LocalPurgeStatus.COMPLETED,
            purged_max_turn_by_thread={},
            tracker_reconcile_needed=False,
            local_completed_at=datetime.now(UTC),
            remote_revoke_status=RemoteRevokeStatus.FAILED_RETRYABLE,
            has_revoke_token=True,
            retry_count=1,
            next_retry_at=datetime.now(UTC) + timedelta(minutes=5),
            last_remote_error="retryable",
            remote_completed_at=None,
            disconnected_at=datetime.now(UTC),
            completed_at=None,
        )
        monkeypatch.setattr(db, "list_user_connections", create_autospec(db.list_user_connections, return_value=[]))
        monkeypatch.setattr(
            db, "list_user_purge_ledgers", create_autospec(db.list_user_purge_ledgers, return_value=[pending_same_project])
        )
        monkeypatch.setattr(db, "insert_oauth_state", create_autospec(db.insert_oauth_state, return_value=None))
        pre_auth_spy = create_autospec(
            oauth_mod.revoke_before_new_authorization,
            return_value={pending_same_project.purge_id: RemoteRevokeStatus.SUCCEEDED},
        )
        monkeypatch.setattr(oauth_mod, "revoke_before_new_authorization", pre_auth_spy)

        revoker = create_autospec(RemoteRevoker, instance=True)
        started = await start_oauth_authorization(
            user_id=LOCAL_DEV_USER_ID,
            provider="google",
            service="calendar",
            auth_mode="oss",
            revoker=revoker,
        )
        pre_auth_spy.assert_awaited_once_with(
            "111222333", "sub-alice", revoker, user_id=LOCAL_DEV_USER_ID
        )
        assert "access_type=offline" in started.authorize_url
        assert "code_challenge_method=S256" in started.authorize_url


# ---------------------------------------------------------------------------
# 5. Feishu revocation event: signature, replay, decrypt, authorized_at filter, other events ignored
# ---------------------------------------------------------------------------


class TestFeishuRevocationEvents:
    def test_verify_signature_decrypt_replay_window_and_token(self) -> None:
        enc_key = "feishu-encrypt-key-secret"
        ver_tok = "feishu-ver-token-secret"
        _install_saved_credentials(
            feishu_encrypt_key=enc_key, feishu_verification_token=ver_tok
        )
        # 1) Official Feishu url_verification handshake succeeds WITHOUT X-Lark-Signature headers
        challenge_inner = {"type": "url_verification", "challenge": "chal-xyz", "token": ver_tok}
        challenge_body = json.dumps(
            {"encrypt": encrypt_feishu_event_payload(json.dumps(challenge_inner), enc_key)}
        ).encode()
        decoded_challenge = verify_and_decode_feishu_event(
            raw_body=challenge_body,
            headers={},
        )
        assert decoded_challenge["type"] == "url_verification"
        assert decoded_challenge["challenge"] == "chal-xyz"

        # 2) Normal event callback requires valid X-Lark-Signature and 5-minute replay window
        event_inner = {
            "schema": "2.0",
            "header": {
                "event_id": "evt-sig-1",
                "event_type": FEISHU_REVOKED_EVENT_TYPE,
                "token": ver_tok,
                "app_id": "cli_test_app_1",
                "create_time": str(int(time.time() * 1000)),
            },
            "event": {"open_id": "ou_1", "revoke_reason": "Revoked by user action."},
        }
        body = json.dumps({"encrypt": encrypt_feishu_event_payload(json.dumps(event_inner), enc_key)}).encode()
        now_ts = int(time.time())
        ts = str(now_ts)
        nonce = "nonce-42"
        sig = compute_feishu_event_signature(
            timestamp=ts, nonce=nonce, encrypt_key=enc_key, raw_body=body
        )

        decoded = verify_and_decode_feishu_event(
            raw_body=body,
            headers={
                "X-Lark-Request-Timestamp": ts,
                "X-Lark-Request-Nonce": nonce,
                "X-Lark-Signature": sig,
            },
            now_epoch_s=float(now_ts),
        )
        assert decoded["header"]["event_id"] == "evt-sig-1"

        # Missing signature headers on normal event rejected
        with pytest.raises(FeishuEventAuthError) as missing_sig:
            verify_and_decode_feishu_event(
                raw_body=body,
                headers={},
                now_epoch_s=float(now_ts),
            )
        assert missing_sig.value.code == "missing_signature_headers"

        # Invalid signature rejected
        with pytest.raises(FeishuEventAuthError) as bad_sig:
            verify_and_decode_feishu_event(
                raw_body=body,
                headers={
                    "X-Lark-Request-Timestamp": ts,
                    "X-Lark-Request-Nonce": nonce,
                    "X-Lark-Signature": "0" * 64,
                },
                now_epoch_s=float(now_ts),
            )
        assert bad_sig.value.code == "invalid_signature"

        # Timestamp outside 5-minute replay window rejected
        stale_ts = str(now_ts - 600)
        stale_sig = compute_feishu_event_signature(
            timestamp=stale_ts, nonce=nonce, encrypt_key=enc_key, raw_body=body
        )
        with pytest.raises(FeishuEventAuthError) as replay_err:
            verify_and_decode_feishu_event(
                raw_body=body,
                headers={
                    "X-Lark-Request-Timestamp": stale_ts,
                    "X-Lark-Request-Nonce": nonce,
                    "X-Lark-Signature": stale_sig,
                },
                now_epoch_s=float(now_ts),
            )
        assert replay_err.value.code == "timestamp_out_of_window"

    @pytest.mark.asyncio
    async def test_feishu_user_revocation_triggers_2a_and_ignores_stale_non_user_and_other_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_saved_credentials()
        now = datetime.now(UTC)
        conn_row = _make_integration_row(
            user_id="u-feishu-1",
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_user_1",
            authorized_at=now - timedelta(minutes=10),
        )
        monkeypatch.setattr(
            db, "list_connections_by_provider_account", create_autospec(db.list_connections_by_provider_account, return_value=[conn_row])
        )
        batch_spy = create_autospec(
            feishu_events_mod.begin_feishu_webhook_revocation_batch,
            return_value=("purged", ["purge-fs-1"]),
        )
        finish_spy = create_autospec(feishu_events_mod.finish_disconnect, return_value=None)
        monkeypatch.setattr(
            feishu_events_mod, "begin_feishu_webhook_revocation_batch", batch_spy
        )
        monkeypatch.setattr(feishu_events_mod, "finish_disconnect", finish_spy)

        # 1) Unrelated event type -> ignored
        res_other = await process_feishu_integration_event(
            {"header": {"event_type": "im.message.receive_v1", "event_id": "evt-other"}, "event": {}}
        )
        assert res_other.status == "ignored"
        assert res_other.reason == "unsupported_event_type"

        # 2) Non-user revocation reason & missing reason -> ignored (no 2A!)
        for bad_event in (
            {"open_id": "ou_user_1", "revoke_reason": "Revoked by administrator action."},
            {"open_id": "ou_user_1", "revoke_reason": "Revoked by security and risk control action."},
            {"open_id": "ou_user_1"},  # missing revoke_reason NEVER triggers 2A
        ):
            res_admin = await process_feishu_integration_event(
                {
                    "header": {
                        "event_type": FEISHU_REVOKED_EVENT_TYPE,
                        "event_id": "evt-admin",
                        "app_id": "cli_test_app_1",
                        "create_time": str(int(now.timestamp() * 1000)),
                    },
                    "event": bad_event,
                }
            )
            assert res_admin.status == "ignored"
            assert res_admin.reason == "non_user_revocation"

        # 3) Missing event timestamp -> ignored (never bypasses authorized_at)
        res_no_ts = await process_feishu_integration_event(
            {
                "header": {
                    "event_type": FEISHU_REVOKED_EVENT_TYPE,
                    "event_id": "evt-no-ts",
                    "app_id": "cli_test_app_1",
                },
                "event": {"open_id": "ou_user_1", "revoke_reason": "Revoked by user action."},
            }
        )
        assert res_no_ts.status == "ignored"
        assert res_no_ts.reason == "missing_event_timestamp"
        batch_spy.assert_not_awaited()

        # 4) Stale revocation predating authorized_at (rejected inside tx) -> ignored
        batch_spy.return_value = ("stale_before_authorized_at", [])
        stale_ms = str(int((now - timedelta(hours=2)).timestamp() * 1000))
        res_stale = await process_feishu_integration_event(
            {
                "header": {
                    "event_type": FEISHU_REVOKED_EVENT_TYPE,
                    "event_id": "evt-stale",
                    "app_id": "cli_test_app_1",
                    "create_time": stale_ms,
                },
                "event": {"open_id": "ou_user_1", "revoke_reason": "Revoked by user action."},
            }
        )
        assert res_stale.status == "ignored"
        assert res_stale.reason == "stale_before_authorized_at"
        finish_spy.assert_not_awaited()

        # 5) Official user-initiated revocation newer than authorized_at -> triggers atomic batch 2A!
        batch_spy.reset_mock()
        batch_spy.return_value = ("purged", ["purge-fs-1"])
        res_ok = await process_feishu_integration_event(
            {
                "header": {
                    "event_type": FEISHU_REVOKED_EVENT_TYPE,
                    "event_id": "evt-valid-1",
                    "app_id": "cli_test_app_1",
                    "create_time": str(int(now.timestamp() * 1000)),
                },
                "event": {"open_id": "ou_user_1", "revoke_reason": "Revoked by user action."},
            }
        )
        assert res_ok.status == "purged"
        assert res_ok.purge_ids == ("purge-fs-1",)
        batch_spy.assert_awaited_once()
        assert batch_spy.await_args.kwargs["event_id"] == "evt-valid-1"
        assert batch_spy.await_args.kwargs["candidate_user_ids"] == ["u-feishu-1"]
        finish_spy.assert_awaited_once_with("purge-fs-1", revoker=None)

        # 6) Duplicate event_id -> idempotent duplicate response
        batch_spy.return_value = ("duplicate", [])
        res_dup = await process_feishu_integration_event(
            {
                "header": {
                    "event_type": FEISHU_REVOKED_EVENT_TYPE,
                    "event_id": "evt-valid-1",
                    "app_id": "cli_test_app_1",
                    "create_time": str(int(now.timestamp() * 1000)),
                },
                "event": {"open_id": "ou_user_1", "revoke_reason": "Revoked by user action."},
            }
        )
        assert res_dup.status == "duplicate"


# ---------------------------------------------------------------------------
# 6 & 7. 12-hour health patrol (multi-worker mutex, error split) & unready credentials
# ---------------------------------------------------------------------------


class TestHealthSweepAndUnreadyCredentials:
    @pytest.mark.asyncio
    async def test_health_sweep_multiworker_mutex_error_split_and_unready_creds_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Worker 2 loses pg_try_advisory_lock -> immediately skipped
        fake_busy_ctx, _ = _make_fake_lock_pool(try_lock_results=[False])
        monkeypatch.setattr(sweep_mod, "get_db_connection", fake_busy_ctx)
        rep_busy = await run_integration_health_sweep_once(force=True)
        assert rep_busy.acquired is False
        assert rep_busy.skipped_reason == "lock_busy"

        # Worker 1 wins lock with ONLY Google credentials configured (Feishu unready!)
        _install_saved_credentials(feishu_app_id=None, feishu_app_secret=None)
        fake_win_ctx, _ = _make_fake_lock_pool(try_lock_results=[True])
        monkeypatch.setattr(sweep_mod, "get_db_connection", fake_win_ctx)
        monkeypatch.setattr(db, "read_health_sweep_state", create_autospec(db.read_health_sweep_state, return_value=None))
        monkeypatch.setattr(db, "update_health_sweep_state", create_autospec(db.update_health_sweep_state, return_value=None))
        monkeypatch.setattr(
            db, "delete_expired_oauth_states_and_confirmations",
            create_autospec(db.delete_expired_oauth_states_and_confirmations, return_value=(2, 1)),
        )

        row_google_ok = _make_integration_row(user_id="u1", provider="google", service="calendar")
        row_google_invalid = _make_integration_row(user_id="u2", provider="google", service="calendar")
        row_google_transient = _make_integration_row(user_id="u3", provider="google", service="calendar")
        row_feishu_unready = _make_integration_row(
            user_id="u4", provider="feishu", service="calendar", oauth_client_id="cli_old"
        )
        monkeypatch.setattr(
            db,
            "list_connections_for_health_sweep",
            create_autospec(
                db.list_connections_for_health_sweep,
                return_value=[row_google_ok, row_google_invalid, row_google_transient, row_feishu_unready],
            ),
        )

        async def _fake_ensure(uid: str, prov: str, svc: str, **_kw: Any) -> Any:
            if uid == "u1":
                return oauth_mod._to_fresh_token(row_google_ok)
            if uid == "u2":
                raise IntegrationNeedsReauthError("invalid_grant", code="invalid_grant")
            if uid == "u3":
                raise oauth_mod.IntegrationTransientError("timeout", code="readtimeout")
            raise AssertionError("u4 feishu row should have been skipped because feishu_app is not ready!")

        ensure_spy = create_autospec(sweep_mod.ensure_fresh_access_token, side_effect=_fake_ensure)
        monkeypatch.setattr(sweep_mod, "ensure_fresh_access_token", ensure_spy)

        rep = await run_integration_health_sweep_once(force=True)
        assert rep.acquired is True
        assert rep.checked == 3
        assert rep.healthy == 1
        assert rep.marked_needs_reauth == 1
        assert rep.transient_errors == 1
        assert rep.triggered_revocation_purge == 0
        assert rep.skipped_credentials_not_ready == 1
        assert rep.expired_states_deleted == 2
        assert rep.expired_confirmations_deleted == 1

    @pytest.mark.asyncio
    async def test_unready_credentials_return_explicit_503_not_500(self) -> None:
        # Credentials NOT configured -> ready=False
        reset_active_snapshot_for_test()
        client = TestClient(_make_app())
        resp = client.post("/api/v1/dalton/integrations/google/calendar/authorize", json={})
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["code"] == "personal_integrations_not_ready"
        assert "管理员尚未" in detail["message"]


# ---------------------------------------------------------------------------
# 8. Core vs Everflow route gating & authoritative constants/capabilities exports
# ---------------------------------------------------------------------------


class TestCoreAndEverflowRoutingAndExports:
    def test_authoritative_constants_and_capabilities_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert GOOGLE_OAUTH_CALLBACK_PATH == "/api/v1/dalton/integrations/google/callback"
        assert FEISHU_OAUTH_CALLBACK_PATH == "/api/v1/dalton/integrations/feishu/callback"
        assert FEISHU_INTEGRATION_EVENTS_PATH == "/api/v1/dalton/integrations/feishu/events"
        assert build_integration_redirect_uri("google") == "https://dalton.example.com/api/v1/dalton/integrations/google/callback"
        assert build_integration_redirect_uri("feishu") == "https://dalton.example.com/api/v1/dalton/integrations/feishu/callback"
        assert build_feishu_integration_events_url() == "https://dalton.example.com/api/v1/dalton/integrations/feishu/events"
        assert {s.capability_id for s in PERSONAL_INTEGRATION_CONNECTOR_SPECS} == {
            "google_calendar",
            "feishu_calendar",
            "feishu_mail",
            "google_gmail",
        }

        fp_unready = capabilities_fingerprint()
        _install_saved_credentials()
        fp_ready = capabilities_fingerprint()
        assert fp_ready != fp_unready

        payload = get_capabilities_payload()
        assert payload["connector_providers"]["google"]["ready"] is True
        assert payload["connector_providers"]["feishu"]["ready"] is True
        assert payload["fingerprint"] == fp_ready

    def test_core_vs_everflow_single_capability_and_manage_mode_gates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_saved_credentials()
        list_conns_mock = create_autospec(db.list_user_connections, return_value=[])
        has_any_mock = create_autospec(db.has_user_any_integration_records, return_value=False)
        monkeypatch.setattr(db, "list_user_connections", list_conns_mock)
        monkeypatch.setattr(db, "list_user_purge_ledgers", create_autospec(db.list_user_purge_ledgers, return_value=[]))
        monkeypatch.setattr(db, "insert_oauth_state", create_autospec(db.insert_oauth_state, return_value=None))
        monkeypatch.setattr(db, "has_user_any_integration_records", has_any_mock)

        client = TestClient(_make_app())

        # 1) Core default: all 4 personal capabilities OFF -> authorize 404, list 404 (no existing records)
        monkeypatch.setenv("DALTON_PROFILE", "core")
        monkeypatch.setenv("APP_ENV", "core")
        assert has_capability(CapabilityId.GOOGLE_CALENDAR) is False
        assert has_capability(CapabilityId.FEISHU_CALENDAR) is False
        assert client.post("/api/v1/dalton/integrations/google/calendar/authorize", json={}).status_code == 404
        assert client.get("/api/v1/dalton/integrations").status_code == 404

        # 2) Core with ONLY GOOGLE_CALENDAR enabled (CHANNEL_GATEWAY still OFF!)
        monkeypatch.setenv("DALTON_CAP_GOOGLE_CALENDAR", "true")
        assert has_capability(CapabilityId.GOOGLE_CALENDAR) is True
        assert has_capability(CapabilityId.CHANNEL_GATEWAY) is False
        r_gc = client.post("/api/v1/dalton/integrations/google/calendar/authorize", json={})
        assert r_gc.status_code == 200
        assert "authorize_url" in r_gc.json()
        # Other personal capabilities still 404!
        assert client.post("/api/v1/dalton/integrations/feishu/calendar/authorize", json={}).status_code == 404
        assert client.post("/api/v1/dalton/integrations/google/mail/authorize", json={}).status_code == 404

        # 3) Core with all personal capabilities OFF again, but user HAS existing connection -> manage GET/DELETE still 200!
        monkeypatch.delenv("DALTON_CAP_GOOGLE_CALENDAR", raising=False)
        existing = _make_integration_row()
        list_conns_mock.return_value = [existing]
        has_any_mock.return_value = True
        r_list = client.get("/api/v1/dalton/integrations")
        assert r_list.status_code == 200
        assert r_list.json()["has_personal_integrations"] is True
        assert len(r_list.json()["connections"]) == 1

        # 4) Core with CHANNEL_GATEWAY OFF & personal caps OFF, feishu_event configured -> /feishu/events url_verification still 200 WITHOUT signature headers!
        enc_key = "enc-key-secret-123456"
        ver_tok = "ver-token-secret-789"
        ev_body = json.dumps(
            {
                "encrypt": encrypt_feishu_event_payload(
                    json.dumps({"type": "url_verification", "challenge": "core-ok", "token": ver_tok}),
                    enc_key,
                )
            }
        ).encode()
        r_ev = client.post(
            "/api/v1/dalton/integrations/feishu/events",
            content=ev_body,
        )
        assert r_ev.status_code == 200
        assert r_ev.json() == {"challenge": "core-ok"}

        # 5) Everflow default: all 4 personal capabilities ON -> authorize returns 200
        monkeypatch.delenv("DALTON_PROFILE", raising=False)
        monkeypatch.setenv("APP_ENV", "dalton")
        assert CapabilityId.GOOGLE_CALENDAR in effective_capabilities()
        assert CapabilityId.FEISHU_CALENDAR in effective_capabilities()
        assert client.post("/api/v1/dalton/integrations/feishu/calendar/authorize", json={}).status_code == 200

        # 6) Callback redirect puts confirmation_token in URL fragment (never query string) and 409 on unconfirmed switch
        cb_res = oauth_mod.OAuthCallbackResult(
            status="account_switch_confirmation_required",
            provider="google",
            service="calendar",
            return_to="/settings?tab=integrations",
            confirmation_token="confirm-secret-xyz",
            previous_account_email="alice@example.com",
            new_account_email="bob@example.com",
        )
        monkeypatch.setattr(
            router_mod,
            "handle_oauth_callback",
            create_autospec(router_mod.handle_oauth_callback, return_value=cb_res),
        )
        r_cb = client.get(
            "/api/v1/dalton/integrations/google/callback?code=c1&state=s1",
            follow_redirects=False,
        )
        assert r_cb.status_code == 302
        parsed_loc = urlparse(r_cb.headers["location"])
        q = parse_qs(parsed_loc.query)
        frag = parse_qs(parsed_loc.fragment)
        assert q["integration_status"] == ["account_switch_confirmation_required"]
        assert "confirmation_token" not in q
        assert frag["confirmation_token"] == ["confirm-secret-xyz"]

        monkeypatch.setattr(
            router_mod,
            "confirm_pending_integration",
            create_autospec(
                router_mod.confirm_pending_integration,
                return_value=oauth_mod.ConfirmIntegrationResult(
                    status="account_switch_confirmation_required",
                    provider="google",
                    service="calendar",
                    previous_account_email="alice@example.com",
                    new_account_email="bob@example.com",
                ),
            ),
        )
        r_conf = client.post(
            "/api/v1/dalton/integrations/confirm",
            json={"confirmation_token": "confirm-secret-xyz", "confirm_account_switch": False},
        )
        assert r_conf.status_code == 409
        assert r_conf.json()["code"] == "account_switch_confirmation_required"


# ---------------------------------------------------------------------------
# 9. Round 2 Review Fixes (Items 1–13)
# ---------------------------------------------------------------------------


class TestRound2ReviewFixes:
    @pytest.mark.asyncio
    async def test_r2_item1_refresh_transactions_commit_before_raising_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item 1: narrowed scope, invalid_grant, Feishu ambiguous, and transient errors COMMIT before raising."""
        _install_saved_credentials()
        now = datetime.now(UTC)

        # 1a) Narrowed scope on refresh commits the newly issued token + needs_reauth (zero TX_ROLLBACK)
        start_row = _make_integration_row(
            provider="google",
            service="calendar",
            expires_at=now - timedelta(seconds=30),
            token_generation=1,
        )
        returned_row_dict = {
            "user_id": start_row.user_id,
            "provider": "google",
            "service": "calendar",
            "oauth_client_id": start_row.oauth_client_id,
            "provider_account_id": start_row.provider_account_id,
            "provider_union_id": None,
            "provider_email": start_row.provider_email,
            "granted_scopes": GOOGLE_SCOPE_CALENDAR_LIST_RO,
            "auth_epoch": start_row.auth_epoch,
            "authorized_at": start_row.authorized_at,
            "token_generation": 2,
            "status": "needs_reauth",
            "refresh_inflight_gen": None,
            "refresh_started_at": None,
            "expires_at": now + timedelta(hours=1),
            "last_refresh_at": now,
            "needs_reauth_at": now,
            "last_error_code": "missing_required_scopes",
            "reauth_notified_at": None,
            "preferences": {},
            "has_access_token": True,
            "has_refresh_token": True,
            "created_at": start_row.created_at,
            "updated_at": now,
        }
        fake_conn_ctx, executed = _make_fake_lock_pool(
            try_lock_results=[True],
            fetchone_results=[returned_row_dict],
        )
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_conn_ctx)
        monkeypatch.setattr(
            db, "list_connections_by_account", create_autospec(db.list_connections_by_account, return_value=[start_row])
        )
        with pytest.raises(IntegrationNeedsReauthError) as narrow_exc:
            await oauth_mod._commit_refresh_success_tx(
                user_id=start_row.user_id,
                provider="google",
                service="calendar",
                start_client_id=start_row.oauth_client_id,
                start_sub=start_row.provider_account_id,
                start_auth_epoch=start_row.auth_epoch,
                start_token_gen=1,
                token_resp=OAuthTokenResponse(
                    access_token="new-at",
                    refresh_token="new-rt",
                    expires_in=3600,
                    expires_at=now + timedelta(hours=1),
                    scope=GOOGLE_SCOPE_CALENDAR_LIST_RO,
                ),
                required_scopes=None,
            )
        assert narrow_exc.value.code == "missing_required_scopes"
        assert ("TX_COMMIT", None) in executed
        assert ("TX_ROLLBACK", None) not in executed

        # 1b) Upstream invalid_grant, Feishu ambiguous, and transient error ALL commit without rollback
        fake_err_ctx, err_exec = _make_fake_lock_pool(try_lock_results=[True, True, True])
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_err_ctx)
        monkeypatch.setattr(
            db, "update_connection_status", create_autospec(db.update_connection_status, return_value=True)
        )
        g_client = create_autospec(GoogleOAuthClient, instance=True)
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=start_row))

        g_client.refresh_token.side_effect = OAuthUpstreamError(
            provider="google", kind="invalid_grant", error_code="invalid_grant", status_code=400
        )
        with pytest.raises(IntegrationNeedsReauthError):
            await ensure_fresh_access_token(LOCAL_DEV_USER_ID, "google", "calendar", google_client=g_client)

        g_client.refresh_token.side_effect = OAuthUpstreamError(
            provider="google", kind="transient", error_code="http_503", status_code=503
        )
        with pytest.raises(oauth_mod.IntegrationTransientError):
            await ensure_fresh_access_token(LOCAL_DEV_USER_ID, "google", "calendar", google_client=g_client)

        assert ("TX_ROLLBACK", None) not in err_exec
        assert err_exec.count(("TX_COMMIT", None)) >= 4

    @pytest.mark.asyncio
    async def test_r2_item2_feishu_revoked_code_atomic_tx_no_swallow_and_orphan_recovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item 2: revoked code does not mutate to revoked_purging before Phase 1; Phase 1 failure propagates; orphans repaired."""
        _install_saved_credentials()
        now = datetime.now(UTC)
        fs_row = _make_integration_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_atomic_1",
            expires_at=now - timedelta(seconds=30),
        )
        fake_ctx, _ = _make_fake_lock_pool(try_lock_results=[True])
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        get_conn_mock = create_autospec(db.get_connection, return_value=fs_row)
        monkeypatch.setattr(db, "get_connection", get_conn_mock)
        status_spy = create_autospec(db.update_connection_status, return_value=True)
        monkeypatch.setattr(db, "update_connection_status", status_spy)
        f_client = create_autospec(FeishuOAuthClient, instance=True)
        f_client.refresh_token.side_effect = OAuthUpstreamError(
            provider="feishu", kind="revoked", error_code="feishu_confirmed_rev", feishu_code=99999
        )
        begin_disc_mock = create_autospec(
            oauth_mod.begin_disconnect, side_effect=RuntimeError("phase1 failed")
        )
        monkeypatch.setattr(oauth_mod, "begin_disconnect", begin_disc_mock)
        with pytest.raises(RuntimeError, match="phase1 failed"):
            await ensure_fresh_access_token(LOCAL_DEV_USER_ID, "feishu", "calendar", feishu_client=f_client)
        # Never pre-mutated status to revoked_purging outside begin_disconnect!
        status_spy.assert_not_awaited()

        # Orphaned revoked_purging with no running ledger repaired in ensure_fresh_access_token
        orphan_row = _make_integration_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_orphan",
            status=IntegrationStatus.REVOKED_PURGING,
        )
        get_conn_mock.return_value = orphan_row
        monkeypatch.setattr(
            db, "has_running_local_purge_for_service", create_autospec(db.has_running_local_purge_for_service, return_value=[])
        )
        begin_disc_mock.reset_mock()
        begin_disc_mock.side_effect = None
        begin_disc_mock.return_value = "purge-orphan-1"
        repair_finish = create_autospec(oauth_mod.finish_disconnect, return_value=None)
        monkeypatch.setattr(oauth_mod, "finish_disconnect", repair_finish)
        with pytest.raises(IntegrationRevokedError):
            await ensure_fresh_access_token(LOCAL_DEV_USER_ID, "feishu", "calendar", feishu_client=f_client)
        begin_disc_mock.assert_awaited_once()
        assert begin_disc_mock.await_args.kwargs["skip_if_running_ledger"] is True
        repair_finish.assert_awaited_once_with("purge-orphan-1", revoker=None)

        # Orphaned revoked_purging also repaired by health sweep
        sweep_ctx, _ = _make_fake_lock_pool(try_lock_results=[True])
        monkeypatch.setattr(sweep_mod, "get_db_connection", sweep_ctx)
        monkeypatch.setattr(db, "read_health_sweep_state", create_autospec(db.read_health_sweep_state, return_value=None))
        monkeypatch.setattr(db, "update_health_sweep_state", create_autospec(db.update_health_sweep_state, return_value=None))
        monkeypatch.setattr(
            db, "delete_expired_oauth_states_and_confirmations",
            create_autospec(db.delete_expired_oauth_states_and_confirmations, return_value=(0, 0)),
        )
        monkeypatch.setattr(db, "list_connections_for_health_sweep", create_autospec(db.list_connections_for_health_sweep, return_value=[]))
        monkeypatch.setattr(
            db, "list_orphaned_revoked_purging_connections",
            create_autospec(db.list_orphaned_revoked_purging_connections, return_value=[orphan_row]),
        )
        sw_begin = create_autospec(sweep_mod.begin_disconnect, return_value="purge-sw-orphan")
        sw_finish = create_autospec(sweep_mod.finish_disconnect, return_value=None)
        monkeypatch.setattr(sweep_mod, "begin_disconnect", sw_begin)
        monkeypatch.setattr(sweep_mod, "finish_disconnect", sw_finish)
        rep = await run_integration_health_sweep_once(force=True)
        assert rep.triggered_revocation_purge == 1
        sw_begin.assert_awaited_once()
        sw_finish.assert_awaited_once_with("purge-sw-orphan", revoker=None)

    @pytest.mark.asyncio
    async def test_r2_item3_feishu_20071_needs_reauth_and_official_revoke_reason_only(self) -> None:
        """Item 3: Feishu 20071 classifies as needs_reauth (NOT revoked); official revoke_reason strictly enforced."""
        from src.dalton.integrations.constants import (
            FEISHU_CONFIRMED_REVOKED_CODES,
            FEISHU_NEEDS_REAUTH_CODES,
        )

        assert 20071 not in FEISHU_CONFIRMED_REVOKED_CODES
        assert 20071 in FEISHU_NEEDS_REAUTH_CODES

        http_double = create_autospec(httpx.AsyncClient, instance=True)
        http_double.post.return_value = httpx.Response(
            200, json={"code": 20071, "error": "invalid_request", "error_description": "redirect_uri mismatch"}
        )
        client = FeishuOAuthClient(http_client=http_double)
        with pytest.raises(OAuthUpstreamError) as err_20071:
            await client.refresh_token(app_id="cli_1", app_secret="sec", refresh_token="rt")
        assert err_20071.value.kind == "needs_reauth"
        assert err_20071.value.feishu_code == 20071

        assert feishu_events_mod._is_user_initiated_revocation({"revoke_reason": "Revoked by user action."}) is True
        assert feishu_events_mod._is_user_initiated_revocation({"revoke_reason": "Revoked by administrator action."}) is False
        assert feishu_events_mod._is_user_initiated_revocation({"revoke_reason": "unknown"}) is False
        assert feishu_events_mod._is_user_initiated_revocation({}) is False

    @pytest.mark.asyncio
    async def test_r2_item4_feishu_reauth_updates_authorized_at_and_epoch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item 4: same-client Feishu re-authorization on a connected row resets authorized_at and bumps auth_epoch."""
        _install_saved_credentials()
        existing = _make_integration_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_1",
            auth_epoch=5,
            status=IntegrationStatus.CONNECTED,
        )
        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "has_running_local_purge_for_service", create_autospec(db.has_running_local_purge_for_service, return_value=[]))
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=existing))
        monkeypatch.setattr(db, "list_connections_by_account", create_autospec(db.list_connections_by_account, return_value=[existing]))
        upsert_spy = create_autospec(db.upsert_connection, return_value=existing)
        monkeypatch.setattr(db, "upsert_connection", upsert_spy)

        await oauth_mod._finalize_connection(
            user_id=LOCAL_DEV_USER_ID,
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_1",
            provider_union_id=None,
            provider_email="u@example.com",
            granted_scopes="offline_access calendar:calendar:readonly",
            access_token="at-new",
            refresh_token="rt-new",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            preferences={},
            allow_account_switch=False,
        )
        assert upsert_spy.await_args.kwargs["reset_authorized_at"] is True
        assert upsert_spy.await_args.kwargs["bump_auth_epoch"] is True

    @pytest.mark.asyncio
    async def test_r2_item5_feishu_multi_user_event_single_batch_and_replay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item 5: multi-user Feishu event submits all matched users in ONE atomic batch call."""
        _install_saved_credentials()
        now = datetime.now(UTC)
        r1 = _make_integration_row(user_id="u-a", provider="feishu", service="calendar", oauth_client_id="cli_test_app_1", provider_account_id="ou_shared")
        r2 = _make_integration_row(user_id="u-b", provider="feishu", service="calendar", oauth_client_id="cli_test_app_1", provider_account_id="ou_shared")
        monkeypatch.setattr(
            db, "list_connections_by_provider_account",
            create_autospec(db.list_connections_by_provider_account, return_value=[r1, r2]),
        )
        batch_spy = create_autospec(
            feishu_events_mod.begin_feishu_webhook_revocation_batch,
            return_value=("purged", ["p-a", "p-b"]),
        )
        finish_spy = create_autospec(feishu_events_mod.finish_disconnect, return_value=None)
        monkeypatch.setattr(feishu_events_mod, "begin_feishu_webhook_revocation_batch", batch_spy)
        monkeypatch.setattr(feishu_events_mod, "finish_disconnect", finish_spy)

        res = await process_feishu_integration_event(
            {
                "header": {
                    "event_type": FEISHU_REVOKED_EVENT_TYPE,
                    "event_id": "evt-multi-1",
                    "app_id": "cli_test_app_1",
                    "create_time": str(int(now.timestamp() * 1000)),
                },
                "event": {"open_id": "ou_shared", "revoke_reason": "Revoked by user action."},
            }
        )
        assert res.status == "purged"
        assert res.purge_ids == ("p-a", "p-b")
        batch_spy.assert_awaited_once()
        assert batch_spy.await_args.kwargs["candidate_user_ids"] == ["u-a", "u-b"]
        assert finish_spy.await_count == 2

    def test_r2_item6_feishu_challenge_without_signature_and_internal_gateway_forward(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item 6: url_verification works without X-Lark-Signature; gateway internal forward endpoint invokes unified processor."""
        _install_saved_credentials()
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "s" * 40)
        client = TestClient(_make_app())

        # Plaintext url_verification with valid verification token & NO signature headers -> 200
        r_plain = client.post(
            "/api/v1/dalton/integrations/feishu/events",
            json={"type": "url_verification", "challenge": "plain-chal", "token": "ver-token-secret-789"},
        )
        assert r_plain.status_code == 200
        assert r_plain.json() == {"challenge": "plain-chal"}

        # Bad verification token on url_verification -> 401
        r_bad_tok = client.post(
            "/api/v1/dalton/integrations/feishu/events",
            json={"type": "url_verification", "challenge": "plain-chal", "token": "wrong-token"},
        )
        assert r_bad_tok.status_code == 401

        # Gateway internal forwarding endpoint requires X-Service-Token
        r_unauth = client.post(
            "/api/v1/dalton/integrations/internal/feishu/events",
            json={"event_id": "evt-gw-1", "open_id": "ou_1", "revoke_reason": "Revoked by user action."},
        )
        assert r_unauth.status_code == 401

        # Valid X-Service-Token normalizes flat Lark node-sdk payload and invokes process_feishu_integration_event
        proc_spy = create_autospec(
            router_mod.process_feishu_integration_event,
            return_value=feishu_events_mod.FeishuEventProcessResult(
                status="purged", event_id="evt-gw-1", purge_ids=("p-gw-1",)
            ),
        )
        monkeypatch.setattr(router_mod, "process_feishu_integration_event", proc_spy)
        r_gw = client.post(
            "/api/v1/dalton/integrations/internal/feishu/events",
            headers={"X-Service-Token": "s" * 40},
            json={
                "event_id": "evt-gw-1",
                "event_type": FEISHU_REVOKED_EVENT_TYPE,
                "app_id": "cli_test_app_1",
                "create_time": str(int(time.time() * 1000)),
                "open_id": "ou_1",
                "revoke_reason": "Revoked by user action.",
            },
        )
        assert r_gw.status_code == 200
        assert r_gw.json()["status"] == "purged"
        passed_payload = proc_spy.await_args.args[0]
        assert passed_payload["header"]["event_type"] == FEISHU_REVOKED_EVENT_TYPE
        assert passed_payload["header"]["event_id"] == "evt-gw-1"
        assert passed_payload["event"]["revoke_reason"] == "Revoked by user action."

    @pytest.mark.asyncio
    async def test_r2_item7_same_project_rotation_checks_new_client_and_handles_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item 7: same-project rotation skips revoke when new client is active; RETRYABLE queues retry ledger."""
        old_client = "111222333-old.apps.googleusercontent.com"
        new_client = "111222333-new.apps.googleusercontent.com"
        _install_saved_credentials(google_client_id=new_client)

        cal_on_new = _make_integration_row(service="calendar", oauth_client_id=new_client, provider_account_id="sub-a")
        mail_on_old = _make_integration_row(service="mail", oauth_client_id=old_client, provider_account_id="sub-a", status=IntegrationStatus.NEEDS_REAUTH)

        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "list_user_purge_ledgers", create_autospec(db.list_user_purge_ledgers, return_value=[]))
        list_conns_mock = create_autospec(db.list_user_connections, return_value=[cal_on_new, mail_on_old])
        has_grant_mock = create_autospec(db.has_active_or_pending_google_grant, return_value=True)
        monkeypatch.setattr(db, "list_user_connections", list_conns_mock)
        monkeypatch.setattr(db, "has_active_or_pending_google_grant", has_grant_mock)
        monkeypatch.setattr(db, "supersede_active_revokes_for_google_project_sub_tx", create_autospec(db.supersede_active_revokes_for_google_project_sub_tx, return_value=0))
        ledger_spy = create_autospec(db.insert_purge_ledger, return_value=None)
        monkeypatch.setattr(db, "insert_purge_ledger", ledger_spy)
        revoker = create_autospec(RemoteRevoker, instance=True)

        await oauth_mod._coordinate_pre_auth_google_revokes(
            user_id=LOCAL_DEV_USER_ID,
            service="mail",
            target_client_id=new_client,
            switch_account=False,
            existing_rows=[cal_on_new, mail_on_old],
            revoker=revoker,
        )
        revoker.revoke.assert_not_awaited()
        assert ledger_spy.await_args.kwargs["remote_revoke_status"] == RemoteRevokeStatus.SUPERSEDED

        # Sole service on old client with RETRYABLE -> registers PENDING ledger under short lock,
        # then invokes process_revoke OUTSIDE lock -> FAILED_RETRYABLE keeping ciphertext
        from src.dalton.services import integration_revoke as revoke_mod

        list_conns_mock.return_value = [mail_on_old]
        has_grant_mock.return_value = False
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=mail_on_old))
        monkeypatch.setattr(db, "update_connection_status", create_autospec(db.update_connection_status, return_value=True))
        stored_ledgers: dict[str, PurgeLedgerRow] = {}

        async def _store_ledger(**kw: Any) -> PurgeLedgerRow:
            row = PurgeLedgerRow(
                purge_id=kw["purge_id"],
                user_id=kw["user_id"],
                provider=kw["provider"],
                service=kw["service"],
                oauth_client_id=kw["oauth_client_id"],
                provider_account_id=kw["provider_account_id"],
                auth_epoch_upto=kw["auth_epoch_upto"],
                trigger_reason=kw["trigger_reason"],
                local_purge_status=kw["local_purge_status"],
                purged_max_turn_by_thread=kw["purged_max_turn_by_thread"],
                tracker_reconcile_needed=False,
                local_completed_at=datetime.now(UTC),
                remote_revoke_status=kw["remote_revoke_status"],
                has_revoke_token=bool(kw.get("revoke_token_plaintext") or kw.get("encrypted_revoke_token")),
                retry_count=0,
                next_retry_at=None,
                last_remote_error=None,
                remote_completed_at=None,
                disconnected_at=datetime.now(UTC),
                completed_at=None,
                revoke_token=kw.get("revoke_token_plaintext"),
            )
            stored_ledgers[row.purge_id] = row
            return row

        import dataclasses

        ledger_spy.side_effect = _store_ledger
        monkeypatch.setattr(revoke_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "get_purge_ledger", create_autospec(db.get_purge_ledger, side_effect=lambda pid, **_k: stored_ledgers.get(pid)))
        monkeypatch.setattr(db, "has_running_local_purge_for_user_provider", create_autospec(db.has_running_local_purge_for_user_provider, return_value=False))
        monkeypatch.setattr(db, "get_active_google_oauth_state_expiry", create_autospec(db.get_active_google_oauth_state_expiry, return_value=None))
        monkeypatch.setattr(db, "claim_revoke_attempt_tx", create_autospec(db.claim_revoke_attempt_tx, return_value=1))
        finalize_spy = create_autospec(
            db.finalize_revoke_attempt_tx,
            side_effect=lambda pid, **kw: dataclasses.replace(
                stored_ledgers[pid],
                remote_revoke_status=kw["remote_revoke_status"],
            ),
        )
        monkeypatch.setattr(db, "finalize_revoke_attempt_tx", finalize_spy)
        revoker.revoke.return_value = RevokeOutcome.RETRYABLE

        await oauth_mod._coordinate_pre_auth_google_revokes(
            user_id=LOCAL_DEV_USER_ID,
            service="mail",
            target_client_id=new_client,
            switch_account=False,
            existing_rows=[mail_on_old],
            revoker=revoker,
        )
        revoker.revoke.assert_awaited_once()
        finalize_spy.assert_awaited_once()
        assert finalize_spy.await_args.kwargs["remote_revoke_status"] == RemoteRevokeStatus.FAILED_RETRYABLE
        assert finalize_spy.await_args.kwargs["clear_revoke_token"] is False

    @pytest.mark.asyncio
    async def test_r2_item8_interleaved_account_switch_confirmation_voided_on_epoch_change(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item 8: confirmation ticket bound to old (client, sub, auth_epoch) is voided when current row changed."""
        _install_saved_credentials()
        now = datetime.now(UTC)
        stale_ticket = PendingConfirmationRow(
            confirmation_hash=hashlib.sha256(b"tok-b").hexdigest(),
            user_id=LOCAL_DEV_USER_ID,
            provider="google",
            service="calendar",
            oauth_client_id="111222333-abc.apps.googleusercontent.com",
            provider_account_id="sub-b",
            provider_union_id=None,
            provider_email="b@example.com",
            granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            expires_at=now + timedelta(hours=1),
            intent=oauth_mod.OAuthIntent.SWITCH_ACCOUNT.value,
            preferences={},
            confirm_expires_at=now + timedelta(minutes=5),
            created_at=now,
            access_token="at-b",
            refresh_token="rt-b",
            expected_old_oauth_client_id="111222333-abc.apps.googleusercontent.com",
            expected_old_provider_account_id="sub-a",
            expected_old_auth_epoch=3,
        )
        current_c = _make_integration_row(provider_account_id="sub-c", auth_epoch=4)
        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "peek_pending_confirmation", create_autospec(db.peek_pending_confirmation, return_value=stale_ticket))
        consume_spy = create_autospec(db.consume_pending_confirmation, return_value=stale_ticket)
        monkeypatch.setattr(db, "consume_pending_confirmation", consume_spy)
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=current_c))
        monkeypatch.setattr(db, "has_active_or_pending_google_grant", create_autospec(db.has_active_or_pending_google_grant, return_value=False))
        insert_ledger_spy = create_autospec(db.insert_purge_ledger, return_value=None)
        monkeypatch.setattr(db, "insert_purge_ledger", insert_ledger_spy)
        proc_rev_spy = create_autospec(oauth_mod.process_revoke, return_value=RemoteRevokeStatus.SUCCEEDED)
        monkeypatch.setattr(oauth_mod, "process_revoke", proc_rev_spy)

        with pytest.raises(PendingConfirmationInvalidError) as exc_info:
            await confirm_pending_integration(
                user_id=LOCAL_DEV_USER_ID,
                confirmation_token="tok-b",
                confirm_account_switch=True,
                revoker=create_autospec(RemoteRevoker, instance=True),
            )
        assert exc_info.value.code == "account_switch_state_changed"
        assert exc_info.value.status_code == 409
        consume_spy.assert_awaited_once()  # ticket voided!
        insert_ledger_spy.assert_awaited_once()  # unactivated staged token queued for revoke!
        proc_rev_spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_r2_item9_local_mode_browser_binding_cookie_and_callback_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item 9: local mode sets HttpOnly SameSite=Lax one-time cookie; mismatched browser requires confirmation."""
        _install_saved_credentials()
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setattr(
            router_mod,
            "_read_local_session_from_request",
            create_autospec(router_mod._read_local_session_from_request, return_value="sid-local-1"),
        )
        monkeypatch.setattr(db, "list_user_connections", create_autospec(db.list_user_connections, return_value=[]))
        monkeypatch.setattr(db, "list_user_purge_ledgers", create_autospec(db.list_user_purge_ledgers, return_value=[]))
        captured_state: dict[str, Any] = {}

        async def _capture_insert_state(**kw: Any) -> None:
            captured_state.update(kw)

        monkeypatch.setattr(db, "insert_oauth_state", create_autospec(db.insert_oauth_state, side_effect=_capture_insert_state))

        client = TestClient(_make_app())
        r_auth = client.post("/api/v1/dalton/integrations/google/calendar/authorize", json={})
        assert r_auth.status_code == 200
        set_cookie_hdr = r_auth.headers.get("set-cookie", "")
        assert oauth_mod.OAUTH_BROWSER_BINDING_COOKIE_NAME in set_cookie_hdr
        assert "httponly" in set_cookie_hdr.lower()
        assert "samesite=lax" in set_cookie_hdr.lower()
        assert captured_state.get("browser_binding_hash")

        # Callback from a DIFFERENT browser (no matching cookie) -> confirmation_required!
        cookie_val = r_auth.cookies.get(oauth_mod.OAUTH_BROWSER_BINDING_COOKIE_NAME)
        assert cookie_val
        state_row = _make_state_row(
            auth_mode="local",
            session_id="sid-local-1",
            browser_binding_hash=hashlib.sha256(cookie_val.encode()).hexdigest(),
        )
        monkeypatch.setattr(db, "consume_oauth_state", create_autospec(db.consume_oauth_state, return_value=state_row))
        monkeypatch.setattr(db, "delete_oauth_state", create_autospec(db.delete_oauth_state, return_value=None))
        monkeypatch.setattr(db, "has_inflight_google_revoke_in_project", create_autospec(db.has_inflight_google_revoke_in_project, return_value=False))
        monkeypatch.setattr(oauth_mod, "verify_local_session_active", create_autospec(oauth_mod.verify_local_session_active, return_value=None))
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=None))
        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "supersede_active_revokes_for_google_project_sub_tx", create_autospec(db.supersede_active_revokes_for_google_project_sub_tx, return_value=0))
        monkeypatch.setattr(db, "insert_pending_confirmation", create_autospec(db.insert_pending_confirmation, return_value=None))
        monkeypatch.setattr(db, "delete_pending_confirmation_by_hash", create_autospec(db.delete_pending_confirmation_by_hash, return_value=None))

        g_client = create_autospec(GoogleOAuthClient, instance=True)
        g_client.exchange_code.return_value = OAuthTokenResponse(
            access_token="at-loc",
            refresh_token="rt-loc",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token="id.tok",
        )
        g_client.verify_id_token.return_value = GoogleIdTokenClaims(
            sub="sub-loc",
            email="loc@example.com",
            email_verified=True,
            aud="111222333-abc.apps.googleusercontent.com",
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )

        cb_mismatch = await handle_oauth_callback(
            provider="google",
            code="c1",
            state="s1",
            browser_binding_token="other-browser-cookie",
            google_client=g_client,
        )
        assert cb_mismatch.status == "confirmation_required"
        assert cb_mismatch.confirmation_token

        # Callback from the SAME initiating browser (matching cookie) -> connected directly!
        saved_loc = _make_integration_row(provider_account_id="sub-loc")
        monkeypatch.setattr(
            oauth_mod, "_finalize_connection", create_autospec(oauth_mod._finalize_connection, return_value=(saved_loc, None))
        )
        cb_match = await handle_oauth_callback(
            provider="google",
            code="c2",
            state="s2",
            browser_binding_token=cookie_val,
            google_client=g_client,
        )
        assert cb_match.status == "connected"
        assert cb_match.connection is saved_loc

    def test_r2_item10_full_provider_disconnect_local_first_then_background_revoke(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item 10: DELETE /{provider} finishes all local purges first, then schedules remote revokes in background."""
        _install_saved_credentials()
        monkeypatch.setattr(db, "has_user_any_integration_records", create_autospec(db.has_user_any_integration_records, return_value=True))
        monkeypatch.setattr(
            router_mod,
            "begin_disconnect_all_services",
            create_autospec(router_mod.begin_disconnect_all_services, return_value=["p-cal", "p-mail"]),
        )
        call_order: list[str] = []

        async def _fake_finish(pid: str, *, revoker: Any = None, cancel_drain_timeout_s: float = 120.0, run_remote_revoke: bool = True) -> FinishDisconnectResult:
            assert revoker is None
            assert run_remote_revoke is False
            call_order.append(f"local:{pid}")
            return FinishDisconnectResult(
                purge_id=pid,
                deleted_connections=1,
                local_purge_status=LocalPurgeStatus.COMPLETED,
                remote_revoke_status=(
                    RemoteRevokeStatus.PENDING if pid == "p-cal" else RemoteRevokeStatus.NOT_APPLICABLE
                ),
            )

        monkeypatch.setattr(router_mod, "finish_disconnect", create_autospec(router_mod.finish_disconnect, side_effect=_fake_finish))
        sched_spy = create_autospec(
            router_mod.schedule_background_remote_revokes,
            side_effect=lambda pids, rev: (call_order.append(f"bg:{','.join(pids)}"), [])[1],
        )
        monkeypatch.setattr(router_mod, "schedule_background_remote_revokes", sched_spy)

        client = TestClient(_make_app())
        r = client.delete("/api/v1/dalton/integrations/google")
        assert r.status_code == 200
        assert call_order == ["local:p-cal", "local:p-mail", "bg:p-cal"]
        sched_spy.assert_called_once()

    @pytest.mark.asyncio
    async def test_r2_item11_sweep_groups_shared_google_cred_and_force_refresh_reuses_newer_gen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item 11: sweep sends 1 refresh for shared Google (client, sub); force_refresh reuses committed newer generation."""
        _install_saved_credentials()
        cal_row = _make_integration_row(user_id="u1", provider="google", service="calendar", provider_account_id="sub-1")
        mail_row = _make_integration_row(user_id="u1", provider="google", service="mail", provider_account_id="sub-1")

        sw_ctx, _ = _make_fake_lock_pool(try_lock_results=[True])
        monkeypatch.setattr(sweep_mod, "get_db_connection", sw_ctx)
        monkeypatch.setattr(db, "read_health_sweep_state", create_autospec(db.read_health_sweep_state, return_value=None))
        monkeypatch.setattr(db, "update_health_sweep_state", create_autospec(db.update_health_sweep_state, return_value=None))
        monkeypatch.setattr(db, "delete_expired_oauth_states_and_confirmations", create_autospec(db.delete_expired_oauth_states_and_confirmations, return_value=(0, 0)))
        monkeypatch.setattr(db, "list_orphaned_revoked_purging_connections", create_autospec(db.list_orphaned_revoked_purging_connections, return_value=[]))
        monkeypatch.setattr(db, "list_connections_for_health_sweep", create_autospec(db.list_connections_for_health_sweep, return_value=[cal_row, mail_row]))
        ensure_spy = create_autospec(sweep_mod.ensure_fresh_access_token, return_value=oauth_mod._to_fresh_token(cal_row))
        monkeypatch.setattr(sweep_mod, "ensure_fresh_access_token", ensure_spy)

        rep = await run_integration_health_sweep_once(force=True)
        assert rep.checked == 1
        assert ensure_spy.await_count == 1

        # Winner re-reading token_generation > start_token_gen on force_refresh=True reuses committed token with 0 upstream calls
        now = datetime.now(UTC)
        gen1 = _make_integration_row(token_generation=1, access_token="old-at", expires_at=now + timedelta(minutes=30))
        gen2 = _make_integration_row(token_generation=2, access_token="already-committed-at", expires_at=now + timedelta(hours=1))
        calls = [gen1, gen2]
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, side_effect=lambda *a, **k: calls.pop(0)))
        ref_ctx, _ = _make_fake_lock_pool(try_lock_results=[True])
        monkeypatch.setattr(oauth_mod, "get_db_connection", ref_ctx)
        g_client = create_autospec(GoogleOAuthClient, instance=True)

        tok = await ensure_fresh_access_token(
            LOCAL_DEV_USER_ID, "google", "calendar", force_refresh=True, google_client=g_client
        )
        assert tok.access_token == "already-committed-at"
        assert tok.token_generation == 2
        g_client.refresh_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_r2_item12_feishu_open_id_check_only_when_app_id_matches_gateway(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Item 12: gateway App ID uses the exact same production resolve_feishu_app_credentials source; unavailable logs warning & skips."""
        from types import MappingProxyType

        from src.dalton.deployment_credentials import snapshot as snap_mod
        from src.dalton.deployment_credentials.snapshot import (
            CredentialGroupResult,
            build_snapshot,
            set_active_snapshot,
        )
        from src.dalton.integrations.oauth_clients import FeishuUserInfo

        now = datetime.now(UTC)
        f_client = create_autospec(FeishuOAuthClient, instance=True)
        f_client.exchange_code.return_value = OAuthTokenResponse(
            access_token="at-fs",
            refresh_token="rt-fs",
            expires_in=7200,
            expires_at=now + timedelta(hours=2),
            scope=" ".join(oauth_mod.FEISHU_CALENDAR_SCOPES),
        )
        f_client.get_user_info.return_value = FeishuUserInfo(
            open_id="ou_oauth_app", union_id="on_union_1", email="u@example.com"
        )
        monkeypatch.setattr(
            db,
            "get_channel_feishu_open_id",
            create_autospec(db.get_channel_feishu_open_id, return_value="ou_gateway_different"),
        )
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=None))
        saved_row = _make_integration_row(
            provider="feishu", oauth_client_id="cli_saved_app", provider_account_id="ou_oauth_app"
        )
        monkeypatch.setattr(
            oauth_mod,
            "_finalize_connection",
            create_autospec(oauth_mod._finalize_connection, return_value=(saved_row, None)),
        )

        # 1) Saved credentials (production gateway fetches via /feishu-credentials -> resolve_feishu_app_credentials -> saved):
        #    both gateway endpoint and OAuth resolve the exact same App ID; different open_id MUST raise!
        set_active_snapshot(
            build_snapshot(
                revision=1,
                rows={
                    FEISHU_APP_ID_KEY: "cli_saved_app",
                    FEISHU_APP_SECRET_KEY: "sec-saved",
                },
            )
        )
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "t" * 40)
        gw_client = TestClient(_make_app())
        gw_resp = gw_client.post(
            "/api/v1/dalton/channels/internal/data-task-notifications/feishu-credentials",
            headers={"X-Service-Token": "t" * 40},
        )
        assert gw_resp.status_code == 200
        assert gw_resp.json()["credentials"][FEISHU_APP_ID_KEY] == "cli_saved_app"
        assert await oauth_mod._resolve_channel_gateway_feishu_app_id() == "cli_saved_app"

        consume_state_mock = create_autospec(
            db.consume_oauth_state,
            return_value=_make_state_row(
                provider="feishu",
                service="calendar",
                oauth_client_id="cli_saved_app",
                requested_scopes=" ".join(oauth_mod.FEISHU_CALENDAR_SCOPES),
            ),
        )
        monkeypatch.setattr(db, "consume_oauth_state", consume_state_mock)
        with pytest.raises(oauth_mod.FeishuIdentityMismatchError):
            await handle_oauth_callback(
                provider="feishu", code="c1", state="s1", feishu_client=f_client
            )

        # 1b) Env credentials (production gateway fetches via /feishu-credentials -> env):
        #     same App ID + different open_id MUST raise FeishuIdentityMismatchError!
        setup_env_mod.set_outside_app.side_effect = (
            lambda k: k in (FEISHU_APP_ID_KEY, FEISHU_APP_SECRET_KEY)
        )
        setup_env_mod._snapshot.return_value = {
            FEISHU_APP_ID_KEY: "cli_env_app",
            FEISHU_APP_SECRET_KEY: "sec-env",
        }
        try:
            gw_env_resp = gw_client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/feishu-credentials",
                headers={"X-Service-Token": "t" * 40},
            )
            assert gw_env_resp.status_code == 200
            assert gw_env_resp.json()["credentials"][FEISHU_APP_ID_KEY] == "cli_env_app"
            assert await oauth_mod._resolve_channel_gateway_feishu_app_id() == "cli_env_app"
            consume_state_mock.return_value = _make_state_row(
                provider="feishu",
                service="calendar",
                oauth_client_id="cli_env_app",
                requested_scopes=" ".join(oauth_mod.FEISHU_CALENDAR_SCOPES),
            )
            with pytest.raises(oauth_mod.FeishuIdentityMismatchError):
                await handle_oauth_callback(
                    provider="feishu", code="c1b", state="s1b", feishu_client=f_client
                )
        finally:
            setup_env_mod.set_outside_app.side_effect = None
            setup_env_mod.set_outside_app.return_value = False
            setup_env_mod._snapshot.return_value = {}

        # 2) Vault readonly credentials via live Vault resolution (production gateway startup path):
        #    same App ID + different open_id MUST raise FeishuIdentityMismatchError!
        set_active_snapshot(
            build_snapshot(
                revision=2,
                rows={},
                feishu_vault_pair={
                    FEISHU_APP_ID_KEY: "cli_vault_app",
                    FEISHU_APP_SECRET_KEY: "sec-vault",
                },
            )
        )
        monkeypatch.setattr(
            snap_mod,
            "query_feishu_vault_pair_strict",
            create_autospec(
                snap_mod.query_feishu_vault_pair_strict,
                return_value=(
                    MappingProxyType(
                        {
                            FEISHU_APP_ID_KEY: "cli_vault_app",
                            FEISHU_APP_SECRET_KEY: "sec-vault",
                        }
                    ),
                    False,
                    None,
                ),
            ),
        )
        consume_state_mock.return_value = _make_state_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_vault_app",
            requested_scopes=" ".join(oauth_mod.FEISHU_CALENDAR_SCOPES),
        )
        with pytest.raises(oauth_mod.FeishuIdentityMismatchError):
            await handle_oauth_callback(
                provider="feishu", code="c2", state="s2", feishu_client=f_client
            )

        # 3) Gateway App ID unavailable (e.g. resolve_feishu_app_credentials not ready / error):
        #    skips open_id comparison and logs a warning!
        set_active_snapshot(
            build_snapshot(
                revision=3,
                rows={
                    FEISHU_APP_ID_KEY: "cli_saved_app",
                    FEISHU_APP_SECRET_KEY: "sec-saved",
                },
            )
        )
        consume_state_mock.return_value = _make_state_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_saved_app",
            requested_scopes=" ".join(oauth_mod.FEISHU_CALENDAR_SCOPES),
        )
        with monkeypatch.context() as mp:
            mp.setattr(
                oauth_mod,
                "resolve_feishu_app_credentials",
                create_autospec(
                    oauth_mod.resolve_feishu_app_credentials,
                    return_value=CredentialGroupResult(
                        group="feishu_app",
                        source="vault_error",
                        revision=3,
                        ready=False,
                        values=MappingProxyType({}),
                    ),
                ),
            )
            with caplog.at_level(logging.WARNING, logger=oauth_mod.logger.name):
                res_unavail = await handle_oauth_callback(
                    provider="feishu", code="c3", state="s3", feishu_client=f_client
                )
            assert res_unavail.status == "connected"
            assert any("skipping open_id check" in rec.getMessage() for rec in caplog.records)

        # 4) Standalone Core profile (CHANNEL_FEISHU disabled) -> skips open_id check cleanly
        monkeypatch.setenv("APP_ENV", "core")
        monkeypatch.setenv("DALTON_PROFILE", "core")
        monkeypatch.setenv("DALTON_CAP_FEISHU_CALENDAR", "1")
        res_core = await handle_oauth_callback(
            provider="feishu", code="c4", state="s4", feishu_client=f_client
        )
        assert res_core.status == "connected"

    def test_r2_item13_return_to_known_routes_only_and_final_location_validation(self) -> None:
        """Item 13: control chars, protocol-relative, backslash, and unknown paths fall back to safe default; Location validated."""
        for evil in (
            "/\t/evil.com",
            "/\x0b/evil.com",
            "/\r\nLocation: https://evil.com",
            "//evil.com/settings",
            "/\\evil.com",
            "https://evil.com/settings",
            "/settings/../../evil",
            "/unknown-route?x=1",
        ):
            assert oauth_mod._sanitize_return_to(evil) == "/settings?tab=integrations"
            loc = router_mod._build_callback_redirect_url(
                evil,
                query_updates={"integration_status": "confirmation_required"},
                fragment_params={"confirmation_token": "tok"},
            )
            assert oauth_mod.validate_callback_location(loc) is True
            assert loc.startswith("/settings?")
            assert "evil.com" not in loc

        assert oauth_mod._sanitize_return_to("/settings?tab=integrations") == "/settings?tab=integrations"
        assert oauth_mod._sanitize_return_to("/chat/t/abc-123") == "/chat/t/abc-123"



# ---------------------------------------------------------------------------
# 10. Round 3 Review Fixes (Codex High + Medium items)
# ---------------------------------------------------------------------------


class TestRound3ReviewFixes:
    @pytest.mark.asyncio
    async def test_r3_item1_google_callback_holds_revoke_lock_across_exchange_and_process_revoke_defers_active_oauth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Task 7 + r4: Google callback never holds PG lock across exchange_code; keeps in-flight state visible until guard commits; process_revoke defers during active OAuth."""
        from src.dalton.services import integration_revoke as revoke_mod

        _install_saved_credentials()
        timeline: list[str] = []
        target_key = db.revoke_lock_key("111222333", "sub-alice")

        class _TrackingConn:
            async def execute(self, sql: str, params: Any = None) -> None:
                if "pg_advisory_lock" in sql:
                    timeline.append(f"lock:{params[0]}")
                elif "pg_advisory_unlock" in sql:
                    timeline.append(f"unlock:{params[0]}")

            @asynccontextmanager
            async def transaction(self) -> Any:
                yield self

        @asynccontextmanager
        async def _tracking_conn_ctx(_conn: Any = None) -> Any:
            yield _TrackingConn()

        monkeypatch.setattr(oauth_mod, "get_db_connection", _tracking_conn_ctx)

        async def _track_consume(*_a: Any, **_k: Any) -> OAuthStateRow:
            timeline.append("consume_state")
            return _make_state_row()

        async def _track_delete_state(*_a: Any, **_k: Any) -> None:
            timeline.append("delete_state")

        monkeypatch.setattr(db, "consume_oauth_state", create_autospec(db.consume_oauth_state, side_effect=_track_consume))
        monkeypatch.setattr(db, "delete_oauth_state", create_autospec(db.delete_oauth_state, side_effect=_track_delete_state))
        monkeypatch.setattr(db, "has_inflight_google_revoke_in_project", create_autospec(db.has_inflight_google_revoke_in_project, return_value=False))
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=None))

        async def _track_supersede(*_a: Any, **_k: Any) -> int:
            timeline.append("supersede")
            return 1

        async def _track_insert_guard(**_k: Any) -> None:
            timeline.append("insert_guard")

        async def _track_finalize(**_k: Any) -> tuple[IntegrationRow, None]:
            timeline.append("finalize")
            return _make_integration_row(), None

        monkeypatch.setattr(
            db, "supersede_active_revokes_for_google_project_sub_tx", create_autospec(db.supersede_active_revokes_for_google_project_sub_tx, side_effect=_track_supersede)
        )
        monkeypatch.setattr(db, "insert_pending_confirmation", create_autospec(db.insert_pending_confirmation, side_effect=_track_insert_guard))
        monkeypatch.setattr(oauth_mod, "_finalize_connection", create_autospec(oauth_mod._finalize_connection, side_effect=_track_finalize))

        g_client = create_autospec(GoogleOAuthClient, instance=True)

        async def _track_exchange(**_k: Any) -> OAuthTokenResponse:
            timeline.append("exchange_code")
            return OAuthTokenResponse(
                access_token="at-new",
                refresh_token="rt-new",
                expires_in=3600,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
                id_token="id.tok",
            )

        g_client.exchange_code.side_effect = _track_exchange
        g_client.verify_id_token.return_value = GoogleIdTokenClaims(
            sub="sub-alice",
            email="alice@example.com",
            email_verified=True,
            aud="111222333-abc.apps.googleusercontent.com",
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )

        cb = await handle_oauth_callback(
            provider="google", code="c1", state="s1", google_client=g_client
        )
        assert cb.status == "connected"
        # No PG advisory lock held during exchange_code; in-flight oauth_state deleted ONLY AFTER guard commits!
        assert timeline == [
            "consume_state",
            "exchange_code",
            f"lock:{target_key}",
            "supersede",
            "insert_guard",
            f"unlock:{target_key}",
            "delete_state",
            "finalize",
        ]

        # process_revoke defers when an active Google OAuth state/exchange exists in google_project
        pid = str(uuid.uuid4())
        ledger = PurgeLedgerRow(
            purge_id=pid,
            user_id=LOCAL_DEV_USER_ID,
            provider="google",
            service="calendar",
            oauth_client_id="111222333-old.apps.googleusercontent.com",
            provider_account_id="sub-alice",
            auth_epoch_upto=1,
            trigger_reason=TriggerReason.CLIENT_ROTATION,
            local_purge_status=LocalPurgeStatus.NOT_REQUIRED,
            purged_max_turn_by_thread={},
            tracker_reconcile_needed=False,
            local_completed_at=datetime.now(UTC),
            remote_revoke_status=RemoteRevokeStatus.FAILED_RETRYABLE,
            has_revoke_token=True,
            retry_count=1,
            next_retry_at=datetime.now(UTC) - timedelta(seconds=5),
            last_remote_error="pre_auth_rotation_revoke_retryable",
            remote_completed_at=None,
            disconnected_at=datetime.now(UTC),
            completed_at=None,
            revoke_token="rt-old-secret",
        )
        oauth_exp = datetime.now(UTC) + timedelta(minutes=8)
        fake_rev_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(revoke_mod, "get_db_connection", fake_rev_ctx)
        monkeypatch.setattr(db, "get_purge_ledger", create_autospec(db.get_purge_ledger, return_value=ledger))
        monkeypatch.setattr(db, "has_running_local_purge_for_user_provider", create_autospec(db.has_running_local_purge_for_user_provider, return_value=False))
        monkeypatch.setattr(revoke_mod, "_has_newer_active_google_connection", create_autospec(revoke_mod._has_newer_active_google_connection, return_value=False))
        monkeypatch.setattr(db, "get_active_google_oauth_state_expiry", create_autospec(db.get_active_google_oauth_state_expiry, return_value=oauth_exp))
        update_remote_spy = create_autospec(db.update_purge_ledger_remote_status, return_value=ledger)
        monkeypatch.setattr(db, "update_purge_ledger_remote_status", update_remote_spy)
        revoker = create_autospec(RemoteRevoker, instance=True)

        st = await revoke_mod.process_revoke(pid, revoker)
        assert st == RemoteRevokeStatus.FAILED_RETRYABLE
        revoker.revoke.assert_not_awaited()
        update_remote_spy.assert_awaited_once()
        assert update_remote_spy.await_args.kwargs["clear_revoke_token"] is False
        assert update_remote_spy.await_args.kwargs["increment_retry"] is False
        assert update_remote_spy.await_args.kwargs["next_retry_at"] >= oauth_exp

    @pytest.mark.asyncio
    async def test_r3_item2_account_switch_ticket_reverified_in_normal_commit_branch_on_delete_or_overwrite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Task 8: normal commit branch re-verifies expected_old_* inside FOR UPDATE tx; deleted row or concurrent same-target switch rejects with 409."""
        _install_saved_credentials()
        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(
            db, "has_running_local_purge_for_service", create_autospec(db.has_running_local_purge_for_service, return_value=[])
        )
        monkeypatch.setattr(
            db, "supersede_active_revokes_for_google_project_sub_tx", create_autospec(db.supersede_active_revokes_for_google_project_sub_tx, return_value=0)
        )
        upsert_spy = create_autospec(db.upsert_connection)
        monkeypatch.setattr(db, "upsert_connection", upsert_spy)

        # Case A: old row concurrently disconnected/deleted before _finalize_connection -> normal branch MUST reject!
        get_conn_mock = create_autospec(db.get_connection, return_value=None)
        monkeypatch.setattr(db, "get_connection", get_conn_mock)
        with pytest.raises(PendingConfirmationInvalidError) as exc_deleted:
            await oauth_mod._finalize_connection(
                user_id=LOCAL_DEV_USER_ID,
                provider="google",
                service="calendar",
                oauth_client_id="111222333-abc.apps.googleusercontent.com",
                provider_account_id="sub-b",
                provider_union_id=None,
                provider_email="b@example.com",
                granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
                access_token="at-b",
                refresh_token="rt-b",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                preferences={},
                allow_account_switch=True,
                expected_old_oauth_client_id="111222333-abc.apps.googleusercontent.com",
                expected_old_provider_account_id="sub-a",
                expected_old_auth_epoch=3,
            )
        assert exc_deleted.value.code == "account_switch_state_changed"
        assert exc_deleted.value.status_code == 409
        upsert_spy.assert_not_awaited()

        # Case B: another ticket already switched sub-a (epoch 3) -> sub-b (epoch 4) -> normal branch MUST reject!
        already_b = _make_integration_row(provider_account_id="sub-b", auth_epoch=4)
        get_conn_mock.return_value = already_b
        with pytest.raises(PendingConfirmationInvalidError) as exc_overwritten:
            await oauth_mod._finalize_connection(
                user_id=LOCAL_DEV_USER_ID,
                provider="google",
                service="calendar",
                oauth_client_id="111222333-abc.apps.googleusercontent.com",
                provider_account_id="sub-b",
                provider_union_id=None,
                provider_email="b@example.com",
                granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
                access_token="at-stale-b",
                refresh_token="rt-stale-b",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                preferences={},
                allow_account_switch=True,
                expected_old_oauth_client_id="111222333-abc.apps.googleusercontent.com",
                expected_old_provider_account_id="sub-a",
                expected_old_auth_epoch=3,
            )
        assert exc_overwritten.value.code == "account_switch_state_changed"
        assert exc_overwritten.value.status_code == 409
        upsert_spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_r3_item3_feishu_revocation_matches_running_ledger_by_account_and_epoch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Task 5: Feishu revocation & orphan repair pass (oauth_client_id, provider_account_id, min_auth_epoch_upto)."""
        from src.dalton.services import user_integrations_lifecycle as lifecycle_mod

        now = datetime.now(UTC)
        new_row_dict = {
            "user_id": "u-switch",
            "provider": "feishu",
            "service": "calendar",
            "oauth_client_id": "cli_test_app_1",
            "provider_account_id": "ou_new",
            "auth_epoch": 9,
            "authorized_at": now - timedelta(minutes=5),
            "status": "connected",
        }

        class _BatchCur:
            def __init__(self) -> None:
                self._step = 0

            async def __aenter__(self) -> _BatchCur:
                return self

            async def __aexit__(self, *_: Any) -> None:
                return None

            async def execute(self, sql: str, params: Any = None) -> None:
                self._last_sql = sql

            async def fetchall(self) -> list[dict[str, Any]]:
                if "FROM dalton_user_integrations" in self._last_sql:
                    return [new_row_dict]
                return []

        class _BatchConn:
            async def execute(self, *_a: Any, **_k: Any) -> None:
                return None

            def cursor(self, **_k: Any) -> _BatchCur:
                return _BatchCur()

            @asynccontextmanager
            async def transaction(self) -> Any:
                yield self

        @asynccontextmanager
        async def _batch_conn_ctx(_conn: Any = None) -> Any:
            yield _BatchConn()

        monkeypatch.setattr(lifecycle_mod, "get_db_connection", _batch_conn_ctx)
        monkeypatch.setattr(db, "has_webhook_event", create_autospec(db.has_webhook_event, return_value=False))
        monkeypatch.setattr(db, "record_webhook_event", create_autospec(db.record_webhook_event, return_value=True))
        has_running_spy = create_autospec(db.has_running_local_purge_for_service, return_value=[])
        monkeypatch.setattr(db, "has_running_local_purge_for_service", has_running_spy)
        insert_ledger_spy = create_autospec(db.insert_purge_ledger, return_value=None)
        monkeypatch.setattr(db, "insert_purge_ledger", insert_ledger_spy)

        outcome, pids = await lifecycle_mod.begin_feishu_webhook_revocation_batch(
            app_id="cli_test_app_1",
            open_id="ou_new",
            union_id=None,
            event_id="evt-new-acct-1",
            event_time=now,
            candidate_user_ids=["u-switch"],
        )
        assert outcome == "purged"
        assert len(pids) == 1
        has_running_spy.assert_awaited_once()
        kw = has_running_spy.await_args.kwargs
        assert kw["oauth_client_id"] == "cli_test_app_1"
        assert kw["provider_account_id"] == "ou_new"
        assert kw["min_auth_epoch_upto"] == 9
        insert_ledger_spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_r3_item5_production_resume_loop_local_only_and_batch_local_before_remote(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Task 10: production resume loop runs local-only; run_personal_purge_resume_once completes ALL local purges before any remote revoke."""
        from src.dalton.services import integration_background as bg_mod
        from src.dalton.services import integration_data_purge as purge_mod
        from src.dalton.services import integration_revoke as revoke_mod
        from src.dalton.services import user_integrations_lifecycle as lifecycle_mod

        inner_loop_spy = create_autospec(bg_mod._run_personal_purge_resume_loop, return_value=None)
        monkeypatch.setattr(bg_mod, "_run_personal_purge_resume_loop", inner_loop_spy)
        await bg_mod.run_personal_purge_resume_loop(poll_interval_s=15.0)
        inner_loop_spy.assert_awaited_once_with(revoker=None, poll_interval_s=15.0)

        now = datetime.now(UTC)
        l_cal = PurgeLedgerRow(
            purge_id=str(uuid.uuid4()),
            user_id="u-batch",
            provider="google",
            service="calendar",
            oauth_client_id="111-a",
            provider_account_id="sub-1",
            auth_epoch_upto=1,
            trigger_reason=TriggerReason.USER_DISCONNECT,
            local_purge_status=LocalPurgeStatus.RUNNING,
            purged_max_turn_by_thread={},
            tracker_reconcile_needed=False,
            local_completed_at=None,
            remote_revoke_status=RemoteRevokeStatus.PENDING,
            has_revoke_token=True,
            retry_count=0,
            next_retry_at=now,
            last_remote_error=None,
            remote_completed_at=None,
            disconnected_at=now,
            completed_at=None,
        )
        l_mail = PurgeLedgerRow(
            purge_id=str(uuid.uuid4()),
            user_id="u-batch",
            provider="google",
            service="mail",
            oauth_client_id="111-a",
            provider_account_id="sub-1",
            auth_epoch_upto=2,
            trigger_reason=TriggerReason.USER_DISCONNECT,
            local_purge_status=LocalPurgeStatus.RUNNING,
            purged_max_turn_by_thread={},
            tracker_reconcile_needed=False,
            local_completed_at=None,
            remote_revoke_status=RemoteRevokeStatus.NOT_APPLICABLE,
            has_revoke_token=False,
            retry_count=0,
            next_retry_at=None,
            last_remote_error=None,
            remote_completed_at=now,
            disconnected_at=now,
            completed_at=None,
        )
        call_order: list[str] = []
        list_running_calls = [[l_cal, l_mail], []]
        monkeypatch.setattr(
            db,
            "list_running_local_purges",
            create_autospec(db.list_running_local_purges, side_effect=lambda **k: list_running_calls.pop(0)),
        )

        async def _fake_finish(pid: str, *, revoker: Any = None, **_k: Any) -> FinishDisconnectResult:
            assert revoker is None
            call_order.append(f"local:{pid}")
            return FinishDisconnectResult(
                purge_id=pid,
                deleted_connections=1,
                local_purge_status=LocalPurgeStatus.COMPLETED,
                remote_revoke_status=(
                    RemoteRevokeStatus.PENDING if pid == l_cal.purge_id else RemoteRevokeStatus.NOT_APPLICABLE
                ),
            )

        async def _fake_revoke(pid: str, _rev: Any) -> RemoteRevokeStatus:
            call_order.append(f"remote:{pid}")
            return RemoteRevokeStatus.SUCCEEDED

        monkeypatch.setattr(lifecycle_mod, "finish_disconnect_locked", create_autospec(lifecycle_mod.finish_disconnect_locked, side_effect=_fake_finish))
        monkeypatch.setattr(revoke_mod, "process_revoke", create_autospec(revoke_mod.process_revoke, side_effect=_fake_revoke))
        rev = create_autospec(RemoteRevoker, instance=True)

        resumed = await purge_mod.run_personal_purge_resume_once(revoker=rev)
        assert resumed == [l_cal.purge_id, l_mail.purge_id]
        assert call_order == [
            f"local:{l_cal.purge_id}",
            f"local:{l_mail.purge_id}",
            f"remote:{l_cal.purge_id}",
        ]

    @pytest.mark.asyncio
    async def test_r3_item6_feishu_crash_takeover_never_replays_refresh_token_and_converges_by_expiry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Feishu refresh crash recovery: takeover worker NEVER replays possibly-consumed refresh token."""
        _install_saved_credentials()
        fake_ctx, _ = _make_fake_lock_pool(try_lock_results=[True, True, True, True])
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        status_updates: list[dict[str, Any]] = []

        async def _capture_status(*args: Any, **kwargs: Any) -> bool:
            status_updates.append({"args": args, **kwargs})
            return True

        monkeypatch.setattr(
            db, "update_connection_status", create_autospec(db.update_connection_status, side_effect=_capture_status)
        )
        f_client = create_autospec(FeishuOAuthClient, instance=True)

        # 1) Unexpired access token + leftover refresh_inflight_gen == token_generation (force_refresh=True / sweep):
        #    marks REFRESH_AMBIGUOUS, keeps tokens, makes 0 upstream calls!
        crashed_unexpired = _make_integration_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_crash_1",
            token_generation=3,
            refresh_inflight_gen=3,
            access_token="still-valid-at-crash",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        get_conn_mock = create_autospec(db.get_connection, return_value=crashed_unexpired)
        monkeypatch.setattr(db, "get_connection", get_conn_mock)
        with pytest.raises(oauth_mod.IntegrationTransientError) as sweep_exc:
            await ensure_fresh_access_token(
                LOCAL_DEV_USER_ID, "feishu", "calendar", force_refresh=True, feishu_client=f_client
            )
        assert sweep_exc.value.code == "refresh_ambiguous"
        assert status_updates[-1]["status"] == IntegrationStatus.REFRESH_AMBIGUOUS
        assert status_updates[-1]["clear_tokens"] is False
        f_client.refresh_token.assert_not_awaited()

        # 2) Same unexpired crashed row on normal tool call (min_remaining_seconds=3600 > 900s remaining):
        #    marks REFRESH_AMBIGUOUS & returns still-valid access token with 0 upstream calls!
        tok = await ensure_fresh_access_token(
            LOCAL_DEV_USER_ID,
            "feishu",
            "calendar",
            min_remaining_seconds=3600,
            force_refresh=False,
            feishu_client=f_client,
        )
        assert tok.access_token == "still-valid-at-crash"
        f_client.refresh_token.assert_not_awaited()

        # 3) Already REFRESH_AMBIGUOUS (no inflight marker) while access token is STILL unexpired:
        #    does NOT replay refresh token before expiry!
        already_amb_unexpired = _make_integration_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_crash_1",
            token_generation=3,
            refresh_inflight_gen=None,
            status=IntegrationStatus.REFRESH_AMBIGUOUS,
            access_token="still-valid-at-amb",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        get_conn_mock.return_value = already_amb_unexpired
        tok_amb = await ensure_fresh_access_token(
            LOCAL_DEV_USER_ID,
            "feishu",
            "calendar",
            min_remaining_seconds=600,
            force_refresh=False,
            feishu_client=f_client,
        )
        assert tok_amb.access_token == "still-valid-at-amb"
        f_client.refresh_token.assert_not_awaited()

        # 4) Expired access token + leftover refresh_inflight_gen == token_generation:
        #    converges directly to NEEDS_REAUTH (clear_tokens=True) with 0 upstream calls!
        crashed_expired = _make_integration_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_crash_2",
            token_generation=4,
            refresh_inflight_gen=4,
            access_token="expired-at",
            expires_at=datetime.now(UTC) - timedelta(seconds=30),
        )
        get_conn_mock.return_value = crashed_expired
        with pytest.raises(IntegrationNeedsReauthError) as exp_exc:
            await ensure_fresh_access_token(
                LOCAL_DEV_USER_ID, "feishu", "calendar", feishu_client=f_client
            )
        assert exp_exc.value.code == "previous_refresh_crashed"
        assert status_updates[-1]["status"] == IntegrationStatus.NEEDS_REAUTH
        assert status_updates[-1]["clear_tokens"] is True
        f_client.refresh_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_r3_item7_google_sibling_updates_use_actual_granted_scopes_and_recompute_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Google actual granted scopes: sibling row gets ONLY new token's scopes (no union) and recomputes status/preferences."""
        _install_saved_credentials()
        cal_row = _make_integration_row(
            service="calendar",
            provider_account_id="sub-alice",
            granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
        )
        mail_sib = _make_integration_row(
            service="mail",
            provider_account_id="sub-alice",
            granted_scopes=f"{GOOGLE_SCOPE_GMAIL_READONLY} https://www.googleapis.com/auth/gmail.drafts.create",
            preferences={"allow_create_draft": True},
        )
        captured_updates: list[dict[str, Any]] = []

        class _CapCur:
            async def __aenter__(self) -> _CapCur:
                return self

            async def __aexit__(self, *_: Any) -> None:
                return None

            async def execute(self, sql: str, params: Any = None) -> None:
                if "UPDATE dalton_user_integrations" in sql and isinstance(params, dict):
                    captured_updates.append(dict(params))

        class _CapConn:
            async def execute(self, *_a: Any, **_k: Any) -> None:
                return None

            def cursor(self, **_k: Any) -> _CapCur:
                return _CapCur()

            @asynccontextmanager
            async def transaction(self) -> Any:
                yield self

        @asynccontextmanager
        async def _cap_conn_ctx(_conn: Any = None) -> Any:
            yield _CapConn()

        monkeypatch.setattr(oauth_mod, "get_db_connection", _cap_conn_ctx)
        monkeypatch.setattr(db, "has_running_local_purge_for_service", create_autospec(db.has_running_local_purge_for_service, return_value=[]))
        monkeypatch.setattr(db, "supersede_active_revokes_for_google_project_sub_tx", create_autospec(db.supersede_active_revokes_for_google_project_sub_tx, return_value=0))
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=cal_row))
        monkeypatch.setattr(db, "list_connections_by_account", create_autospec(db.list_connections_by_account, return_value=[cal_row, mail_sib]))
        monkeypatch.setattr(db, "upsert_connection", create_autospec(db.upsert_connection, return_value=cal_row))

        # Switch-account flow picked back original sub-alice, so Google only granted Calendar scopes (no Gmail!)
        await oauth_mod._finalize_connection(
            user_id=LOCAL_DEV_USER_ID,
            provider="google",
            service="calendar",
            oauth_client_id=cal_row.oauth_client_id,
            provider_account_id="sub-alice",
            provider_union_id=None,
            provider_email="alice@example.com",
            granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            access_token="at-cal-only",
            refresh_token="rt-cal-only",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            preferences={},
            allow_account_switch=False,
        )
        assert len(captured_updates) == 1
        sib_upd = captured_updates[0]
        assert sib_upd["service"] == "mail"
        assert GOOGLE_SCOPE_GMAIL_READONLY not in sib_upd["granted_scopes"]
        assert sib_upd["status"] == IntegrationStatus.NEEDS_REAUTH.value
        assert sib_upd["err_code"] == "missing_required_scopes"
        assert sib_upd["preferences"].obj["allow_create_draft"] is False

    def test_r3_item8_return_to_browser_normalization_rejects_percent_encoded_dot_segments_and_checks_location(
        self,
    ) -> None:
        """Task 13: WHATWG browser path normalization resolves %2e/%2E dot segments before route allowlist check and validates final Location."""
        for traversal in (
            "/settings/%2e%2e/outside",
            "/settings/.%2e/outside",
            "/settings/%2e./outside",
            "/settings/%2E%2E/outside",
            "/chat/%2e%2e/login",
            "/monitors/%2e%2e/admin",
            "/settings/%2e%2e%2foutside",
            "/settings/%2e%2e%5coutside",
            "/%2e%2e//evil.com",
            "/settings/%00/evil",
            "/settings/%2e%2e/evil?x=1",
        ):
            assert oauth_mod.sanitize_return_to(traversal) == "/settings?tab=integrations"
            assert oauth_mod.validate_callback_location(traversal) is False
            loc = router_mod._build_callback_redirect_url(
                traversal,
                query_updates={"integration_status": "connected", "provider": "google", "service": "calendar"},
                fragment_params={"confirmation_token": "tok"},
            )
            assert oauth_mod.validate_callback_location(loc) is True
            # Simulate browser WHATWG URL resolution of Location against app origin
            resolved_path = oauth_mod._normalize_browser_path(urlparse(loc).path)
            assert resolved_path == "/settings"
            assert "outside" not in loc
            assert "evil.com" not in loc

        # In-app dot segment that stays inside allowed prefix is normalized to canonical path
        assert oauth_mod.sanitize_return_to("/settings/sub/%2e%2e/integrations") == "/settings/integrations"
        assert oauth_mod.validate_callback_location("/settings/sub/%2e%2e/integrations") is False
        loc_norm = router_mod._build_callback_redirect_url(
            "/settings/sub/%2e%2e/integrations?tab=1",
            query_updates={"integration_status": "connected"},
        )
        assert loc_norm.startswith("/settings/integrations?")
        assert "%2e" not in loc_norm.lower()
        assert oauth_mod.validate_callback_location(loc_norm) is True


# ---------------------------------------------------------------------------
# 10. Round 4 Review Fixes (No Lock Across HTTP, Cross-User Revoke, Gateway App ID, Local-First, Sibling Switch, Pending Revoke)
# ---------------------------------------------------------------------------


class TestRound4ReviewFixes:
    @pytest.mark.asyncio
    async def test_r4_process_revoke_releases_lock_before_http_and_commits_by_attempt_gen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lead §1: process_revoke checks & claims attempt_gen under short lock, calls revoker OUTSIDE any PG lock, then re-acquires lock to commit by gen."""
        from src.dalton.services import integration_revoke as revoke_mod

        pid = str(uuid.uuid4())
        target_key = db.revoke_lock_key("111222333", "sub-alice")
        ledger = PurgeLedgerRow(
            purge_id=pid,
            user_id="user-a",
            provider="google",
            service="calendar",
            oauth_client_id="111222333-old.apps.googleusercontent.com",
            provider_account_id="sub-alice",
            auth_epoch_upto=2,
            trigger_reason=TriggerReason.USER_DISCONNECT,
            local_purge_status=LocalPurgeStatus.COMPLETED,
            purged_max_turn_by_thread={},
            tracker_reconcile_needed=False,
            local_completed_at=datetime.now(UTC),
            remote_revoke_status=RemoteRevokeStatus.PENDING,
            has_revoke_token=True,
            retry_count=0,
            next_retry_at=None,
            last_remote_error=None,
            remote_completed_at=None,
            disconnected_at=datetime.now(UTC),
            completed_at=None,
            revoke_token="rt-secret-r4",
        )
        events: list[str] = []

        class _TrackConn:
            async def execute(self, sql: str, params: Any = None) -> None:
                if "pg_advisory_lock" in sql:
                    events.append(f"lock:{params[0]}")
                elif "pg_advisory_unlock" in sql:
                    events.append(f"unlock:{params[0]}")

        @asynccontextmanager
        async def _track_pool(_conn: Any = None) -> Any:
            yield _TrackConn()

        async def _claim(purge_id: str, **_k: Any) -> int:
            events.append("claim_gen:7")
            return 7

        import dataclasses

        async def _finalize(purge_id: str, **kw: Any) -> PurgeLedgerRow:
            events.append(f"finalize_gen:{kw['expected_attempt_gen']}:{kw['remote_revoke_status'].value}")
            return dataclasses.replace(
                ledger, remote_revoke_status=kw["remote_revoke_status"]
            )

        revoker = create_autospec(RemoteRevoker, instance=True)

        async def _http_revoke(**_k: Any) -> RevokeOutcome:
            events.append("http_revoke")
            return RevokeOutcome.SUCCEEDED

        revoker.revoke.side_effect = _http_revoke

        monkeypatch.setattr(revoke_mod, "get_db_connection", _track_pool)
        monkeypatch.setattr(db, "get_purge_ledger", create_autospec(db.get_purge_ledger, return_value=ledger))
        monkeypatch.setattr(db, "has_running_local_purge_for_user_provider", create_autospec(db.has_running_local_purge_for_user_provider, return_value=False))
        monkeypatch.setattr(revoke_mod, "_has_newer_active_google_connection", create_autospec(revoke_mod._has_newer_active_google_connection, return_value=False))
        monkeypatch.setattr(db, "get_active_google_oauth_state_expiry", create_autospec(db.get_active_google_oauth_state_expiry, return_value=None))
        monkeypatch.setattr(db, "claim_revoke_attempt_tx", create_autospec(db.claim_revoke_attempt_tx, side_effect=_claim))
        monkeypatch.setattr(db, "finalize_revoke_attempt_tx", create_autospec(db.finalize_revoke_attempt_tx, side_effect=_finalize))

        st = await revoke_mod.process_revoke(pid, revoker)
        assert st == RemoteRevokeStatus.SUCCEEDED
        assert events == [
            f"lock:{target_key}",
            "claim_gen:7",
            f"unlock:{target_key}",
            "http_revoke",
            f"lock:{target_key}",
            "finalize_gen:7:succeeded",
            f"unlock:{target_key}",
        ]

    @pytest.mark.asyncio
    async def test_r4_callback_waits_for_inflight_google_revoke_outside_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lead §1 + Codex High #1: callback waits without holding DB locks until any pre-existing in-flight revoke lease clears, then runs exchange_code."""
        _install_saved_credentials()
        polls = {"n": 0}
        order: list[str] = []

        async def _inflight_check(proj: str, *, conn: Any = None) -> bool:
            assert proj == "111222333"
            polls["n"] += 1
            order.append(f"poll:{polls['n']}")
            return polls["n"] < 2

        monkeypatch.setattr(oauth_mod, "_INFLIGHT_REVOKE_POLL_INTERVAL_S", 0.005)
        monkeypatch.setattr(db, "consume_oauth_state", create_autospec(db.consume_oauth_state, return_value=_make_state_row()))
        monkeypatch.setattr(db, "delete_oauth_state", create_autospec(db.delete_oauth_state, return_value=None))
        monkeypatch.setattr(db, "has_inflight_google_revoke_in_project", create_autospec(db.has_inflight_google_revoke_in_project, side_effect=_inflight_check))
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=None))
        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "supersede_active_revokes_for_google_project_sub_tx", create_autospec(db.supersede_active_revokes_for_google_project_sub_tx, return_value=0))
        monkeypatch.setattr(db, "insert_pending_confirmation", create_autospec(db.insert_pending_confirmation, return_value=None))
        monkeypatch.setattr(oauth_mod, "_finalize_connection", create_autospec(oauth_mod._finalize_connection, return_value=(_make_integration_row(), None)))

        g_client = create_autospec(GoogleOAuthClient, instance=True)

        async def _ex(**_k: Any) -> OAuthTokenResponse:
            order.append("exchange_code")
            return OAuthTokenResponse(
                access_token="at-ok",
                refresh_token="rt-ok",
                expires_in=3600,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
                id_token="id.ok",
            )

        g_client.exchange_code.side_effect = _ex
        g_client.verify_id_token.return_value = GoogleIdTokenClaims(
            sub="sub-alice",
            email="alice@example.com",
            email_verified=True,
            aud="111222333-abc.apps.googleusercontent.com",
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )

        cb = await asyncio.wait_for(
            handle_oauth_callback(provider="google", code="c1", state="s1", google_client=g_client),
            timeout=5.0,
        )
        assert cb.status == "connected"
        assert order == ["poll:1", "poll:2", "exchange_code"]

    @pytest.mark.asyncio
    async def test_r4_cross_user_revoke_lock_and_grant_check_prevents_destroying_peer_member(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lead §2.1 + Codex High #2 + Grok Medium #3: revoke lock & grant check scope to (google_project, sub) across all Dalton users."""
        from src.dalton.services import integration_revoke as revoke_mod

        # Lock key depends ONLY on (google_project, sub)
        assert db.revoke_lock_key("111222333", "sub-shared") == db.revoke_lock_key("111222333", "sub-shared")
        assert db.revoke_lock_key("111222333", "sub-shared") != db.revoke_lock_key("999888777", "sub-shared")

        pid = str(uuid.uuid4())
        user_a_ledger = PurgeLedgerRow(
            purge_id=pid,
            user_id="member-a",
            provider="google",
            service="calendar",
            oauth_client_id="111222333-app.apps.googleusercontent.com",
            provider_account_id="sub-shared",
            auth_epoch_upto=1,
            trigger_reason=TriggerReason.USER_DISCONNECT,
            local_purge_status=LocalPurgeStatus.COMPLETED,
            purged_max_turn_by_thread={},
            tracker_reconcile_needed=False,
            local_completed_at=datetime.now(UTC),
            remote_revoke_status=RemoteRevokeStatus.PENDING,
            has_revoke_token=True,
            retry_count=0,
            next_retry_at=None,
            last_remote_error=None,
            remote_completed_at=None,
            disconnected_at=datetime.now(UTC),
            completed_at=None,
            revoke_token="rt-member-a",
        )
        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(revoke_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "get_purge_ledger", create_autospec(db.get_purge_ledger, return_value=user_a_ledger))
        monkeypatch.setattr(db, "has_running_local_purge_for_user_provider", create_autospec(db.has_running_local_purge_for_user_provider, return_value=False))
        grant_spy = create_autospec(db.has_active_or_pending_google_grant, return_value=True)
        monkeypatch.setattr(db, "has_active_or_pending_google_grant", grant_spy)
        upd_spy = create_autospec(db.update_purge_ledger_remote_status, return_value=user_a_ledger)
        monkeypatch.setattr(db, "update_purge_ledger_remote_status", upd_spy)
        revoker = create_autospec(RemoteRevoker, instance=True)

        st = await revoke_mod.process_revoke(pid, revoker)
        assert st == RemoteRevokeStatus.SUPERSEDED
        revoker.revoke.assert_not_awaited()
        grant_spy.assert_awaited_once()
        # Verified called with (google_project, sub) — no user_id filter!
        assert grant_spy.await_args.args == ("111222333", "sub-shared")
        assert upd_spy.await_args.kwargs["remote_revoke_status"] == RemoteRevokeStatus.SUPERSEDED
        assert upd_spy.await_args.kwargs["clear_revoke_token"] is True

    @pytest.mark.asyncio
    async def test_r4_live_gateway_reported_feishu_app_id_beats_rotated_config(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Lead §2.3 + Codex High #3: live running gateway's reported Feishu App ID takes precedence over newly rotated config; fallback uses resolve_feishu_app_credentials and warns when unavailable."""
        from src.dalton.integrations.oauth_clients import FeishuUserInfo

        _install_saved_credentials(feishu_app_id="cli_new_oauth", feishu_app_secret="sec_new")
        gw_app_mock = create_autospec(
            db.get_active_channel_gateway_feishu_app_id,
            return_value=(True, "cli_old_gw"),
        )
        monkeypatch.setattr(db, "get_active_channel_gateway_feishu_app_id", gw_app_mock)
        assert await oauth_mod._resolve_channel_gateway_feishu_app_id() == "cli_old_gw"

        # OAuth callback using rotated cli_new_oauth does NOT compare against cli_old_gw's channel binding!
        f_client = create_autospec(FeishuOAuthClient, instance=True)
        f_client.exchange_code.return_value = OAuthTokenResponse(
            access_token="at-new",
            refresh_token="rt-new",
            expires_in=7200,
            expires_at=datetime.now(UTC) + timedelta(hours=2),
            scope=" ".join(oauth_mod.FEISHU_CALENDAR_SCOPES),
        )
        f_client.get_user_info.return_value = FeishuUserInfo(
            open_id="ou_new_app_id", union_id="on_u1", email="u@example.com"
        )
        monkeypatch.setattr(
            db, "consume_oauth_state",
            create_autospec(
                db.consume_oauth_state,
                return_value=_make_state_row(
                    provider="feishu",
                    service="calendar",
                    oauth_client_id="cli_new_oauth",
                    requested_scopes=" ".join(oauth_mod.FEISHU_CALENDAR_SCOPES),
                ),
            ),
        )
        channel_open_id_spy = create_autospec(db.get_channel_feishu_open_id, return_value="ou_old_gw_binding")
        monkeypatch.setattr(db, "get_channel_feishu_open_id", channel_open_id_spy)
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=None))
        saved_row = _make_integration_row(
            provider="feishu", oauth_client_id="cli_new_oauth", provider_account_id="ou_new_app_id"
        )
        monkeypatch.setattr(oauth_mod, "_finalize_connection", create_autospec(oauth_mod._finalize_connection, return_value=(saved_row, None)))

        cb = await handle_oauth_callback(provider="feishu", code="c1", state="s1", feishu_client=f_client)
        assert cb.status == "connected"
        channel_open_id_spy.assert_not_awaited()

        # When live gateway reports no active Feishu adapter -> warns and returns None (skips check)
        gw_app_mock.return_value = (True, None)
        with caplog.at_level(logging.WARNING, logger=oauth_mod.__name__):
            assert await oauth_mod._resolve_channel_gateway_feishu_app_id() is None
        assert any("no active Feishu App ID" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_r4_local_first_blocks_manual_retry_and_full_disconnect_when_local_purge_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex Medium #1: process_revoke defers while local purge is running; DELETE /google skips background revoke if another worker holds a running local purge."""
        from src.dalton.services import integration_revoke as revoke_mod

        pid = str(uuid.uuid4())
        running_ledger = PurgeLedgerRow(
            purge_id=pid,
            user_id=LOCAL_DEV_USER_ID,
            provider="google",
            service="calendar",
            oauth_client_id="111222333-app.apps.googleusercontent.com",
            provider_account_id="sub-1",
            auth_epoch_upto=1,
            trigger_reason=TriggerReason.USER_DISCONNECT,
            local_purge_status=LocalPurgeStatus.RUNNING,
            purged_max_turn_by_thread={},
            tracker_reconcile_needed=False,
            local_completed_at=None,
            remote_revoke_status=RemoteRevokeStatus.PENDING,
            has_revoke_token=True,
            retry_count=0,
            next_retry_at=None,
            last_remote_error=None,
            remote_completed_at=None,
            disconnected_at=datetime.now(UTC),
            completed_at=None,
            revoke_token="rt-1",
        )
        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(revoke_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "get_purge_ledger", create_autospec(db.get_purge_ledger, return_value=running_ledger))
        upd_spy = create_autospec(db.update_purge_ledger_remote_status, return_value=running_ledger)
        monkeypatch.setattr(db, "update_purge_ledger_remote_status", upd_spy)
        revoker = create_autospec(RemoteRevoker, instance=True)

        st = await revoke_mod.process_revoke(pid, revoker)
        assert st == RemoteRevokeStatus.PENDING
        revoker.revoke.assert_not_awaited()
        assert upd_spy.await_args.kwargs["last_remote_error"] == "deferred_local_purge_running"
        assert upd_spy.await_args.kwargs["clear_revoke_token"] is False

        # DELETE /google when another worker holds purge lock on one ledger (finish_disconnect returns None)
        _install_saved_credentials()
        monkeypatch.setattr(db, "has_user_any_integration_records", create_autospec(db.has_user_any_integration_records, return_value=True))
        monkeypatch.setattr(
            router_mod, "begin_disconnect_all_services",
            create_autospec(router_mod.begin_disconnect_all_services, return_value=["p-cal", "p-mail"]),
        )
        monkeypatch.setattr(
            router_mod,
            "finish_disconnect",
            create_autospec(
                router_mod.finish_disconnect,
                side_effect=[
                    FinishDisconnectResult(
                        purge_id="p-cal",
                        deleted_connections=1,
                        local_purge_status=LocalPurgeStatus.COMPLETED,
                        remote_revoke_status=RemoteRevokeStatus.PENDING,
                    ),
                    None,  # Another worker currently holds local purge lock for p-mail!
                ],
            ),
        )
        sched_spy = create_autospec(router_mod.schedule_background_remote_revokes, return_value=[])
        monkeypatch.setattr(router_mod, "schedule_background_remote_revokes", sched_spy)

        client = TestClient(_make_app())
        resp = client.delete("/api/v1/dalton/integrations/google")
        assert resp.status_code == 200
        sched_spy.assert_not_called()

    def test_r4_everflow_gateway_feishu_revocation_allowed_after_personal_caps_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex Medium #3: after personal integration capabilities are disabled & feishu_event unconfigured, internal gateway route allows events matching a historical Feishu connection and rejects non-matching with 404."""
        _install_saved_credentials(feishu_encrypt_key=None, feishu_verification_token=None)
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.setenv("DALTON_PROFILE", "core")  # all personal integration capabilities default OFF
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "s" * 40)

        proc_spy = create_autospec(
            router_mod.process_feishu_integration_event,
            return_value=feishu_events_mod.FeishuEventProcessResult(
                status="purged", event_id="evt-hist-1", purge_ids=("p-hist-1",)
            ),
        )
        monkeypatch.setattr(router_mod, "process_feishu_integration_event", proc_spy)
        match_mock = create_autospec(router_mod.has_matching_feishu_event_connection, return_value=True)
        monkeypatch.setattr(router_mod, "has_matching_feishu_event_connection", match_mock)

        client = TestClient(_make_app())
        payload = {
            "event_id": "evt-hist-1",
            "event_type": FEISHU_REVOKED_EVENT_TYPE,
            "app_id": "cli_test_app_1",
            "create_time": str(int(time.time() * 1000)),
            "open_id": "ou_hist_1",
            "revoke_reason": "Revoked by user action.",
        }
        # Matching historical connection -> allowed (200)
        r_ok = client.post(
            "/api/v1/dalton/integrations/internal/feishu/events",
            headers={"X-Service-Token": "s" * 40},
            json=payload,
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["status"] == "purged"
        proc_spy.assert_awaited_once()

        # Non-matching event when capabilities disabled & feishu_event unconfigured -> 404
        match_mock.return_value = False
        r_404 = client.post(
            "/api/v1/dalton/integrations/internal/feishu/events",
            headers={"X-Service-Token": "s" * 40},
            json=payload,
        )
        assert r_404.status_code == 404

        # Public endpoint STILL returns 404 when capabilities disabled & feishu_event unconfigured
        r_pub = client.post("/api/v1/dalton/integrations/feishu/events", json=payload)
        assert r_pub.status_code == 404

    @pytest.mark.asyncio
    async def test_r4_cancel_google_pending_confirmation_revokes_unactivated_token_only_when_no_other_grant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Grok Medium #2: cancelling a Google pending confirmation revokes the staged refresh token when no other active/pending grant exists."""
        _install_saved_credentials()
        now = datetime.now(UTC)
        staged = PendingConfirmationRow(
            confirmation_hash=hashlib.sha256(b"tok-cancel").hexdigest(),
            user_id=LOCAL_DEV_USER_ID,
            provider="google",
            service="calendar",
            oauth_client_id="111222333-abc.apps.googleusercontent.com",
            provider_account_id="sub-cancel",
            provider_union_id=None,
            provider_email="cancel@example.com",
            granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            expires_at=now + timedelta(hours=1),
            intent=oauth_mod.OAuthIntent.CONNECT.value,
            preferences={},
            confirm_expires_at=now + timedelta(minutes=5),
            created_at=now,
            access_token="at-cancel",
            refresh_token="rt-cancel",
        )
        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "consume_pending_confirmation", create_autospec(db.consume_pending_confirmation, return_value=staged))
        has_grant_mock = create_autospec(db.has_active_or_pending_google_grant, return_value=False)
        monkeypatch.setattr(db, "has_active_or_pending_google_grant", has_grant_mock)
        ledger_spy = create_autospec(db.insert_purge_ledger, return_value=None)
        monkeypatch.setattr(db, "insert_purge_ledger", ledger_spy)
        proc_spy = create_autospec(oauth_mod.process_revoke, return_value=RemoteRevokeStatus.SUCCEEDED)
        monkeypatch.setattr(oauth_mod, "process_revoke", proc_spy)
        revoker = create_autospec(RemoteRevoker, instance=True)

        res = await confirm_pending_integration(
            user_id=LOCAL_DEV_USER_ID,
            confirmation_token="tok-cancel",
            cancel=True,
            revoker=revoker,
        )
        assert res.status == "cancelled"
        ledger_spy.assert_awaited_once()
        assert ledger_spy.await_args.kwargs["revoke_token_plaintext"] == "rt-cancel"
        proc_spy.assert_awaited_once()

        # When another active/pending grant exists for (project, sub), cancel does NOT revoke!
        ledger_spy.reset_mock()
        proc_spy.reset_mock()
        has_grant_mock.return_value = True
        res2 = await confirm_pending_integration(
            user_id=LOCAL_DEV_USER_ID,
            confirmation_token="tok-cancel",
            cancel=True,
            revoker=revoker,
        )
        assert res2.status == "cancelled"
        ledger_spy.assert_not_awaited()
        proc_spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_r4_google_account_switch_acquires_target_refresh_lock_and_syncs_target_sibling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex Medium #2: replace_connection_for_account_switch acquires lifecycle -> target refresh -> target revoke locks and updates active target sibling rows."""
        from src.dalton.services import user_integrations_lifecycle as lc_mod

        _install_saved_credentials()
        uid = LOCAL_DEV_USER_ID
        client_id = "111222333-alpha.apps.googleusercontent.com"
        target_sibling = _make_integration_row(
            user_id=uid,
            provider="google",
            service="mail",
            oauth_client_id=client_id,
            provider_account_id="sub-target",
            granted_scopes=f"{oauth_mod.GOOGLE_GMAIL_MIN_SCOPES[0]} https://www.googleapis.com/auth/gmail.drafts.create",
            preferences={"allow_create_draft": True},
        )
        locks_acquired: list[int] = []

        class _SwitchCursor:
            def __init__(self) -> None:
                self._step = 0

            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            async def execute(self, sql: str, params: Any = None) -> None:
                self._last_sql = sql

            async def fetchone(self) -> Any:
                if "SELECT user_id, provider, service" in self._last_sql:
                    return {
                        "user_id": uid,
                        "provider": "google",
                        "service": "calendar",
                        "oauth_client_id": client_id,
                        "provider_account_id": "sub-old",
                        "auth_epoch": 5,
                        "access_token": b"enc-at-old",
                        "refresh_token": b"enc-rt-old",
                        "status": "connected",
                    }
                if "RETURNING auth_epoch" in self._last_sql:
                    return {"auth_epoch": 6}
                return None

        class _SwitchConn:
            async def execute(self, sql: str, params: Any = None) -> None:
                if "pg_advisory_lock" in sql and params:
                    locks_acquired.append(params[0])

            def cursor(self, **_k: Any) -> _SwitchCursor:
                return _SwitchCursor()

            @asynccontextmanager
            async def transaction(self) -> Any:
                yield self

        @asynccontextmanager
        async def _switch_pool(_conn: Any = None) -> Any:
            yield _SwitchConn()

        monkeypatch.setattr(lc_mod, "get_db_connection", _switch_pool)
        monkeypatch.setattr(db, "get_sibling_connections", create_autospec(db.get_sibling_connections, return_value=[]))
        monkeypatch.setattr(db, "has_active_or_pending_google_grant", create_autospec(db.has_active_or_pending_google_grant, return_value=False))
        monkeypatch.setattr(
            lc_mod, "_lock_and_register_phase1_tx",
            create_autospec(lc_mod._lock_and_register_phase1_tx, return_value=("pid-sw-1", {})),
        )
        monkeypatch.setattr(
            db, "list_connections_by_account",
            create_autospec(db.list_connections_by_account, return_value=[target_sibling]),
        )
        sync_spy = create_autospec(lc_mod.sync_google_sibling_rows_tx, return_value=None)
        monkeypatch.setattr(lc_mod, "sync_google_sibling_rows_tx", sync_spy)
        sup_spy = create_autospec(db.supersede_active_revokes_for_google_project_sub_tx, return_value=0)
        monkeypatch.setattr(db, "supersede_active_revokes_for_google_project_sub_tx", sup_spy)

        res = await lc_mod.replace_connection_for_account_switch(
            uid,
            "google",
            "calendar",
            new_oauth_client_id=client_id,
            new_provider_account_id="sub-target",
            new_granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            new_access_token="at-new-cal",
            new_refresh_token="rt-new-cal",
            new_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        assert res.purge_id == "pid-sw-1"
        assert locks_acquired == [
            db.lifecycle_lock_key(uid, "google"),
            db.refresh_lock_key(uid, "google", client_id, "sub-target"),
            db.revoke_lock_key("111222333", "sub-target"),
        ]
        sync_spy.assert_awaited_once()
        assert sync_spy.await_args.kwargs["active_siblings"] == [target_sibling]
        assert sync_spy.await_args.kwargs["granted_scopes"] == " ".join(GOOGLE_CALENDAR_MIN_SCOPES)
        sup_spy.assert_awaited_once()
        assert sup_spy.await_args.args[:2] == ("111222333", "sub-target")
