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
    DuplicateWebhookEventError,
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
    )


def _make_fake_lock_pool(
    *,
    try_lock_results: list[bool] | None = None,
    fetchone_results: list[Any] | None = None,
) -> tuple[Any, list[tuple[str, Any]]]:
    """Create a fake ``get_db_connection`` context manager using ``create_autospec``."""
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
            yield

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
        supersede_spy = create_autospec(oauth_mod.supersede_pending_revokes, return_value=1)
        monkeypatch.setattr(db, "upsert_connection", upsert_spy)
        monkeypatch.setattr(db, "insert_purge_ledger", ledger_spy)
        monkeypatch.setattr(oauth_mod, "supersede_pending_revokes", supersede_spy)

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
        supersede_spy.assert_awaited_once_with(LOCAL_DEV_USER_ID, "111222333", "sub-alice")

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
        current_conn = [old_row]
        monkeypatch.setattr(
            db, "get_connection", create_autospec(db.get_connection, side_effect=lambda *a, **k: current_conn[0])
        )
        monkeypatch.setattr(
            db, "has_running_local_purge_for_service", create_autospec(db.has_running_local_purge_for_service, return_value=[])
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
        supersede_spy = create_autospec(oauth_mod.supersede_pending_revokes, return_value=0)
        revoker = create_autospec(RemoteRevoker, instance=True)

        monkeypatch.setattr(oauth_mod, "replace_connection_for_account_switch", replace_spy)
        monkeypatch.setattr(oauth_mod, "finish_disconnect", finish_spy)
        monkeypatch.setattr(oauth_mod, "supersede_pending_revokes", supersede_spy)

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
        finish_spy.assert_awaited_once_with(purge_uuid, revoker=revoker)
        supersede_spy.assert_awaited_once_with(LOCAL_DEV_USER_ID, "111222333", "sub-bob")


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

        # 4) Feishu confirmed user revocation code (20071) -> triggers begin_disconnect(ERROR_CODE_REVOKED) + finish_disconnect
        f_client.refresh_token.side_effect = OAuthUpstreamError(
            provider="feishu", kind="revoked", error_code="feishu_20071", feishu_code=20071
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
            LOCAL_DEV_USER_ID, "111222333", "sub-alice", revoker
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
        inner = {"type": "url_verification", "challenge": "chal-xyz", "token": ver_tok}
        body = json.dumps({"encrypt": encrypt_feishu_event_payload(json.dumps(inner), enc_key)}).encode()
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
        assert decoded["challenge"] == "chal-xyz"

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
        monkeypatch.setattr(
            db, "list_user_connections", create_autospec(db.list_user_connections, return_value=[conn_row])
        )
        begin_spy = create_autospec(feishu_events_mod.begin_disconnect, return_value="purge-fs-1")
        finish_spy = create_autospec(feishu_events_mod.finish_disconnect, return_value=None)
        monkeypatch.setattr(feishu_events_mod, "begin_disconnect", begin_spy)
        monkeypatch.setattr(feishu_events_mod, "finish_disconnect", finish_spy)

        # 1) Unrelated event type -> ignored
        res_other = await process_feishu_integration_event(
            {"header": {"event_type": "im.message.receive_v1", "event_id": "evt-other"}, "event": {}}
        )
        assert res_other.status == "ignored"
        assert res_other.reason == "unsupported_event_type"

        # 2) Non-user revocation reason -> ignored (no 2A!)
        res_admin = await process_feishu_integration_event(
            {
                "header": {
                    "event_type": FEISHU_REVOKED_EVENT_TYPE,
                    "event_id": "evt-admin",
                    "app_id": "cli_test_app_1",
                    "create_time": str(int(now.timestamp() * 1000)),
                },
                "event": {"open_id": "ou_user_1", "revoke_reason": "admin_disabled"},
            }
        )
        assert res_admin.status == "ignored"
        assert res_admin.reason == "non_user_revocation"

        # 3) Stale revocation predating authorized_at -> ignored
        stale_ms = str(int((now - timedelta(hours=2)).timestamp() * 1000))
        res_stale = await process_feishu_integration_event(
            {
                "header": {
                    "event_type": FEISHU_REVOKED_EVENT_TYPE,
                    "event_id": "evt-stale",
                    "app_id": "cli_test_app_1",
                    "create_time": stale_ms,
                },
                "event": {"open_id": "ou_user_1"},
            }
        )
        assert res_stale.status == "ignored"
        assert res_stale.reason == "stale_before_authorized_at"
        begin_spy.assert_not_awaited()

        # 4) Valid user-initiated revocation newer than authorized_at -> triggers 2A!
        res_ok = await process_feishu_integration_event(
            {
                "header": {
                    "event_type": FEISHU_REVOKED_EVENT_TYPE,
                    "event_id": "evt-valid-1",
                    "app_id": "cli_test_app_1",
                    "create_time": str(int(now.timestamp() * 1000)),
                },
                "event": {"open_id": "ou_user_1", "revoke_reason": "user_revoked"},
            }
        )
        assert res_ok.status == "purged"
        assert res_ok.purge_ids == ("purge-fs-1",)
        begin_spy.assert_awaited_once_with(
            "u-feishu-1",
            "feishu",
            "calendar",
            trigger_reason=TriggerReason.WEBHOOK_REVOKED,
            remote_mode="local_only",
            event_id="evt-valid-1",
        )
        finish_spy.assert_awaited_once_with("purge-fs-1", revoker=None)

        # 5) Duplicate event_id -> idempotent duplicate response
        begin_spy.side_effect = DuplicateWebhookEventError("duplicate")
        res_dup = await process_feishu_integration_event(
            {
                "header": {
                    "event_type": FEISHU_REVOKED_EVENT_TYPE,
                    "event_id": "evt-valid-1",
                    "app_id": "cli_test_app_1",
                    "create_time": str(int(now.timestamp() * 1000)),
                },
                "event": {"open_id": "ou_user_1"},
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

        # 4) Core with CHANNEL_GATEWAY OFF & personal caps OFF, feishu_event configured -> /feishu/events url_verification still 200!
        enc_key = "enc-key-secret-123456"
        ver_tok = "ver-token-secret-789"
        ev_body = json.dumps({"type": "url_verification", "challenge": "core-ok", "token": ver_tok}).encode()
        now_ts = str(int(time.time()))
        sig = compute_feishu_event_signature(
            timestamp=now_ts, nonce="n1", encrypt_key=enc_key, raw_body=ev_body
        )
        r_ev = client.post(
            "/api/v1/dalton/integrations/feishu/events",
            content=ev_body,
            headers={
                "X-Lark-Request-Timestamp": now_ts,
                "X-Lark-Request-Nonce": "n1",
                "X-Lark-Signature": sig,
            },
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
