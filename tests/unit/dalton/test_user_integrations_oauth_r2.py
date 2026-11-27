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
    async def _default_has_other_user_claim(
        google_project: str, sub: str, *, exclude_user_id: str, conn: Any = None
    ) -> bool:
        return False

    async def _default_has_any_active_in_project(
        google_project: str, *, conn: Any = None
    ) -> bool:
        return False

    async def _default_peek_oauth_state_voided(
        state_hash: str, provider: str, *, conn: Any = None
    ) -> bool:
        return False

    async def _default_void_oauth_states(
        user_id: str, provider: str, *, service: str | None = None, conn: Any = None
    ) -> int:
        return 0

    async def _default_consume_active_intent(
        state_hash: str,
        provider: str,
        *,
        require_valid_exchange_lease: bool = False,
        conn: Any = None,
    ) -> tuple[str, OAuthStateRow | None]:
        return "ok", None

    async def _default_has_inflight_sub(
        google_project: str,
        sub: str,
        *,
        exclude_purge_id: Any = None,
        conn: Any = None,
    ) -> bool:
        return False

    async def _default_renew_sweep_lease(
        *, expected_sweep_gen: int, worker_id: str, lease_seconds: int, conn: Any = None
    ) -> bool:
        return True

    async def _default_peek_oauth_state(
        state_hash: str, provider: str, *, conn: Any = None
    ) -> tuple[str, str] | None:
        return None

    async def _default_delete_oauth_state(
        state_hash: str, provider: str, *, conn: Any = None
    ) -> None:
        return None

    monkeypatch.setattr(db, "has_other_user_google_account_claim", _default_has_other_user_claim)
    monkeypatch.setattr(db, "has_any_active_google_grant_in_project", _default_has_any_active_in_project)
    monkeypatch.setattr(db, "peek_oauth_state_voided", _default_peek_oauth_state_voided)
    monkeypatch.setattr(db, "void_oauth_states_for_user_provider", _default_void_oauth_states)
    monkeypatch.setattr(db, "consume_active_oauth_intent_tx", _default_consume_active_intent)
    monkeypatch.setattr(db, "has_inflight_google_revoke_for_project_sub", _default_has_inflight_sub)
    monkeypatch.setattr(db, "renew_health_sweep_lease", _default_renew_sweep_lease)
    monkeypatch.setattr(db, "peek_oauth_state", _default_peek_oauth_state)
    monkeypatch.setattr(db, "delete_oauth_state", _default_delete_oauth_state)
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
            if (
                "pg_try_advisory_lock" in self._last_sql
                or "pg_try_advisory_xact_lock" in self._last_sql
            ):
                val = lock_queue.pop(0) if lock_queue else True
                return (val,)
            if fetch_queue:
                return fetch_queue.pop(0)
            return None

        async def fetchall(self) -> list[Any]:
            if "RETURNING service, auth_epoch, token_generation, refresh_attempt_gen" in self._last_sql and fetch_queue:
                nxt = fetch_queue.pop(0)
                return nxt if isinstance(nxt, list) else [nxt]
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

        async def _atomic_consume(
            sh: str,
            prov: str,
            *,
            exchange_ttl_seconds: int = 120,
            conn: Any = None,
        ) -> OAuthStateRow | None:
            _ = (exchange_ttl_seconds, conn)
            async with consume_lock:
                row = states_store.pop((sh, prov), None)
                if row is not None and row.expires_at <= datetime.now(UTC):
                    return None
                return row

        monkeypatch.setattr(
            db, "consume_oauth_state", create_autospec(db.consume_oauth_state, side_effect=_atomic_consume)
        )
        monkeypatch.setattr(
            db, "peek_oauth_state", create_autospec(db.peek_oauth_state, return_value=(LOCAL_DEV_USER_ID, "111222333-abc.apps.googleusercontent.com"))
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
        monkeypatch.setattr(db, "peek_oauth_state", create_autospec(db.peek_oauth_state, return_value=None))
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

        async def _peek_pending(
            ch: str,
            uid: str,
            *,
            decrypt_tokens: bool = False,
            conn: Any = None,
        ) -> PendingConfirmationRow | None:
            _ = (decrypt_tokens, conn)
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
        monkeypatch.setattr(db, "peek_oauth_state", create_autospec(db.peek_oauth_state, return_value=None))
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
        monkeypatch.setattr(db, "peek_oauth_state", create_autospec(db.peek_oauth_state, return_value=None))
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

        async def _fake_commit(**kw: Any) -> Any:
            state_box["row"] = refreshed_row
            tok = oauth_mod._to_fresh_token(refreshed_row)
            return (tok, None) if kw.get("return_outcome") else tok

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

        # 2) Ambiguous timeout AFTER access token expiry (connected row) -> converges directly to needs_reauth
        expired_connected_row = _make_integration_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_1",
            expires_at=datetime.now(UTC) - timedelta(seconds=5),
            status=IntegrationStatus.CONNECTED,
        )
        get_conn_mock.return_value = expired_connected_row
        with pytest.raises(IntegrationNeedsReauthError) as exc_info:
            await ensure_fresh_access_token(
                LOCAL_DEV_USER_ID, "feishu", "calendar", feishu_client=f_client
            )
        assert exc_info.value.code == "refresh_timeout_after_expiry"
        assert status_updates[-1]["status"] == IntegrationStatus.NEEDS_REAUTH

        # 2b) Already REFRESH_AMBIGUOUS row whose access token has now expired ->
        #     converges directly to needs_reauth WITHOUT calling upstream refresh!
        f_client.refresh_token.reset_mock()
        expired_amb_row = _make_integration_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_test_app_1",
            provider_account_id="ou_1",
            expires_at=datetime.now(UTC) - timedelta(seconds=5),
            status=IntegrationStatus.REFRESH_AMBIGUOUS,
        )
        get_conn_mock.return_value = expired_amb_row
        with pytest.raises(IntegrationNeedsReauthError) as exc_amb_exp:
            await ensure_fresh_access_token(
                LOCAL_DEV_USER_ID, "feishu", "calendar", feishu_client=f_client
            )
        assert exc_amb_exp.value.code == "refresh_ambiguous_expired"
        assert status_updates[-1]["status"] == IntegrationStatus.NEEDS_REAUTH
        f_client.refresh_token.assert_not_awaited()
        expired_row = expired_connected_row
        get_conn_mock.return_value = expired_row

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
        monkeypatch.setattr(db, "update_health_sweep_state", create_autospec(db.update_health_sweep_state, return_value=1))
        monkeypatch.setattr(
            db, "list_orphaned_revoked_purging_connections",
            create_autospec(db.list_orphaned_revoked_purging_connections, return_value=[]),
        )
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
        monkeypatch.setattr(db, "update_health_sweep_state", create_autospec(db.update_health_sweep_state, return_value=1))
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
        monkeypatch.setattr(db, "peek_oauth_state", create_autospec(db.peek_oauth_state, return_value=None))
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
        monkeypatch.setattr(db, "update_health_sweep_state", create_autospec(db.update_health_sweep_state, return_value=1))
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
        """Item 12 + R5 Codex Med #3: Feishu open_id check compares strictly against the binding's OWN feishu_app_id; fallback reads ONLY get_credential_group; unknown binding app_id warns & skips."""
        from src.dalton.deployment_credentials.snapshot import (
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
        binding_mock = create_autospec(
            db.get_channel_feishu_binding,
            return_value=("ou_gateway_different", "cli_saved_app"),
        )
        monkeypatch.setattr(db, "get_channel_feishu_binding", binding_mock)
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=None))
        monkeypatch.setattr(db, "insert_pending_confirmation", create_autospec(db.insert_pending_confirmation, return_value=None))
        saved_row = _make_integration_row(
            provider="feishu", oauth_client_id="cli_saved_app", provider_account_id="ou_oauth_app"
        )
        monkeypatch.setattr(
            oauth_mod,
            "_finalize_connection",
            create_autospec(oauth_mod._finalize_connection, return_value=(saved_row, None)),
        )

        # 1) Saved credentials + binding owned by the SAME App ID -> different open_id MUST raise!
        set_active_snapshot(
            build_snapshot(
                revision=1,
                rows={
                    FEISHU_APP_ID_KEY: "cli_saved_app",
                    FEISHU_APP_SECRET_KEY: "sec-saved",
                },
            )
        )
        monkeypatch.setattr(
            db, "get_active_channel_gateway_feishu_app_id",
            create_autospec(db.get_active_channel_gateway_feishu_app_id, return_value=(False, None)),
        )
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

        # 2) Gateway rotated & restarted on cli_new_app while user's historical binding belongs to cli_old_app:
        #    binding's OWN app_id (cli_old_app) != OAuth client_id (cli_new_app) -> skips check & succeeds!
        set_active_snapshot(
            build_snapshot(
                revision=2,
                rows={
                    FEISHU_APP_ID_KEY: "cli_new_app",
                    FEISHU_APP_SECRET_KEY: "sec-new",
                },
            )
        )
        binding_mock.return_value = ("ou_old_app_open_id", "cli_old_app")
        consume_state_mock.return_value = _make_state_row(
            provider="feishu",
            service="calendar",
            oauth_client_id="cli_new_app",
            requested_scopes=" ".join(oauth_mod.FEISHU_CALENDAR_SCOPES),
        )
        res_rotated = await handle_oauth_callback(
            provider="feishu", code="c2", state="s2", feishu_client=f_client
        )
        assert res_rotated.status == "connected"

        # 3) Legacy binding with NO recorded feishu_app_id (None):
        #    cannot confirm app ownership -> logs warning & skips open_id check safely!
        binding_mock.return_value = ("ou_legacy_unknown_app", None)
        with caplog.at_level(logging.WARNING, logger=oauth_mod.logger.name):
            res_legacy = await handle_oauth_callback(
                provider="feishu", code="c3", state="s3", feishu_client=f_client
            )
        assert res_legacy.status == "connected"
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
                if "pg_advisory_xact_lock" in sql or "pg_advisory_lock" in sql:
                    timeline.append(f"lock:{params[0]}")
                elif "pg_advisory_unlock" in sql:
                    timeline.append(f"unlock:{params[0]}")

            @asynccontextmanager
            async def transaction(self) -> Any:
                timeline.append("tx_begin")
                try:
                    yield self
                finally:
                    timeline.append("tx_end")

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
        monkeypatch.setattr(db, "peek_oauth_state", create_autospec(db.peek_oauth_state, return_value=(LOCAL_DEV_USER_ID, "111222333-abc.apps.googleusercontent.com")))
        monkeypatch.setattr(db, "has_inflight_google_revoke_in_project", create_autospec(db.has_inflight_google_revoke_in_project, return_value=False))
        monkeypatch.setattr(db, "has_inflight_google_revoke_for_project_sub", create_autospec(db.has_inflight_google_revoke_for_project_sub, return_value=False))
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
        # No PG advisory lock held during exchange_code; Step 1 tx commits before exchange_code,
        # and Step 3 finalize consumes intent before finally cleanup!
        life_key = db.lifecycle_lock_key(LOCAL_DEV_USER_ID, "google")
        coord_key = db.google_project_coord_lock_key("111222333")
        assert timeline == [
            "tx_begin",
            f"lock:{life_key}",
            f"lock:{coord_key}",
            "consume_state",
            "tx_end",
            "exchange_code",
            "finalize",
            "delete_state",
        ]
        _ = target_key

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
                if "pg_advisory_xact_lock" in sql or "pg_advisory_lock" in sql:
                    events.append(f"lock:{params[0]}")

            @asynccontextmanager
            async def transaction(self) -> Any:
                events.append("tx_begin")
                try:
                    yield self
                finally:
                    events.append("tx_end")

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
        monkeypatch.setattr(db, "has_inflight_google_revoke_for_project_sub", create_autospec(db.has_inflight_google_revoke_for_project_sub, return_value=False))
        monkeypatch.setattr(db, "get_active_google_oauth_state_expiry", create_autospec(db.get_active_google_oauth_state_expiry, return_value=None))
        monkeypatch.setattr(db, "claim_revoke_attempt_tx", create_autospec(db.claim_revoke_attempt_tx, side_effect=_claim))
        monkeypatch.setattr(db, "finalize_revoke_attempt_tx", create_autospec(db.finalize_revoke_attempt_tx, side_effect=_finalize))

        st = await revoke_mod.process_revoke(pid, revoker)
        assert st == RemoteRevokeStatus.SUCCEEDED
        coord_key = db.google_project_coord_lock_key("111222333")
        assert events == [
            "tx_begin",
            f"lock:{target_key}",
            f"lock:{coord_key}",
            "claim_gen:7",
            "tx_end",
            "http_revoke",
            "tx_begin",
            f"lock:{target_key}",
            "finalize_gen:7:succeeded",
            "tx_end",
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
        monkeypatch.setattr(db, "peek_oauth_state", create_autospec(db.peek_oauth_state, return_value=(LOCAL_DEV_USER_ID, "111222333-abc.apps.googleusercontent.com")))
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
    async def test_r4_same_member_revoke_lock_and_grant_check_protects_sibling_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Decision 1: revoke lock & grant check scope to same member's (google_project, sub) so disconnecting calendar never revokes an active Gmail sibling."""
        from src.dalton.services import integration_revoke as revoke_mod

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
        assert grant_spy.await_args.args == ("111222333", "sub-shared")
        assert grant_spy.await_args.kwargs["user_id"] == "member-a"
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
        binding_spy = create_autospec(db.get_channel_feishu_binding, return_value=("ou_old_gw_binding", "cli_old_gw"))
        monkeypatch.setattr(db, "get_channel_feishu_binding", binding_spy)
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=None))
        monkeypatch.setattr(db, "insert_pending_confirmation", create_autospec(db.insert_pending_confirmation, return_value=None))
        saved_row = _make_integration_row(
            provider="feishu", oauth_client_id="cli_new_oauth", provider_account_id="ou_new_app_id"
        )
        monkeypatch.setattr(oauth_mod, "_finalize_connection", create_autospec(oauth_mod._finalize_connection, return_value=(saved_row, None)))

        cb = await handle_oauth_callback(provider="feishu", code="c1", state="s1", feishu_client=f_client)
        assert cb.status == "connected"
        binding_spy.assert_awaited_once()

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
                if ("pg_advisory_xact_lock" in sql or "pg_advisory_lock" in sql) and params:
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


# ---------------------------------------------------------------------------
# Round 5 Review Fixes (Unified 3-step no-lock-across-HTTP + lock guard + Codex & Grok items)
# ---------------------------------------------------------------------------


class TestRound5ReviewFixes:
    @pytest.mark.asyncio
    async def test_r5_advisory_lock_http_guard_raises_when_external_client_called_under_lock(
        self,
    ) -> None:
        """Lead §1: self-check raises AdvisoryLockHeldDuringExternalCallError if any external client is called while holding a PG advisory lock."""
        from src.dalton.integrations.lock_guard import (
            AdvisoryLockHeldDuringExternalCallError,
            advisory_lock_http_guard,
            assert_no_advisory_lock_held,
            track_advisory_lock,
        )

        # Outside any lock -> passes silently
        with advisory_lock_http_guard(True):
            assert_no_advisory_lock_held("google.refresh_token")

            # Inside a tracked advisory lock -> raises immediately!
            with track_advisory_lock("refresh", 12345):
                with pytest.raises(AdvisoryLockHeldDuringExternalCallError) as exc_info:
                    assert_no_advisory_lock_held("google.refresh_token")
                assert "refresh(12345)" in str(exc_info.value)

                g_client = GoogleOAuthClient()
                with pytest.raises(AdvisoryLockHeldDuringExternalCallError):
                    await g_client.refresh_token(
                        client_id="cid", client_secret="sec", refresh_token="rt"
                    )
                with pytest.raises(AdvisoryLockHeldDuringExternalCallError):
                    await g_client.revoke(
                        provider="google", oauth_client_id="cid", token="rt"
                    )

                f_client = FeishuOAuthClient()
                with pytest.raises(AdvisoryLockHeldDuringExternalCallError):
                    await f_client.refresh_token(
                        app_id="cli_1", app_secret="sec", refresh_token="rt"
                    )

            # After exiting lock scope -> passes again
            assert_no_advisory_lock_held("google.refresh_token")

    @pytest.mark.asyncio
    async def test_r5_refresh_3step_releases_lock_and_conn_before_http_and_loser_releases_conn_before_polling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lead §1 + Codex High #1 + Grok High #1: refresh winner unlocks & closes DB conn before HTTP; loser releases DB conn before polling."""
        from src.dalton.integrations.lock_guard import (
            advisory_lock_http_guard,
            held_advisory_locks,
        )

        _install_saved_credentials()
        events: list[str] = []
        open_conns = {"count": 0}

        class _StepCur:
            def __init__(self) -> None:
                self._last_sql = ""
                self.rowcount = 1

            async def __aenter__(self) -> _StepCur:
                return self

            async def __aexit__(self, *args: Any) -> bool:
                return False

            async def execute(self, sql: str, params: Any = None) -> None:
                self._last_sql = sql
                if "pg_try_advisory_xact_lock" in sql or "pg_try_advisory_lock" in sql:
                    events.append("try_lock")
                elif "RETURNING service, auth_epoch, token_generation, refresh_attempt_gen" in sql:
                    events.append("claim_attempt_gen")

            async def fetchone(self) -> Any:
                if "pg_try_advisory_xact_lock" in self._last_sql or "pg_try_advisory_lock" in self._last_sql:
                    return (True,)
                return None

            async def fetchall(self) -> list[Any]:
                if "RETURNING service, auth_epoch, token_generation, refresh_attempt_gen" in self._last_sql:
                    return [("calendar", 1, 1, 1)]
                return []

        class _StepConn:
            def cursor(self, **_k: Any) -> _StepCur:
                return _StepCur()

            async def execute(self, sql: str, params: Any = None) -> None:
                if "pg_advisory_xact_lock" in sql or "pg_advisory_lock" in sql:
                    events.append("lock")

            @asynccontextmanager
            async def transaction(self) -> Any:
                yield
                events.append("tx_commit")

        @asynccontextmanager
        async def _tracked_pool(_conn: Any = None) -> Any:
            open_conns["count"] += 1
            try:
                yield _StepConn()
            finally:
                open_conns["count"] -= 1

        exp_row = _make_integration_row(
            expires_at=datetime.now(UTC) - timedelta(seconds=60),
            token_generation=1,
        )
        fresh_tok = oauth_mod.FreshAccessToken(
            access_token="at-refreshed",
            provider="google",
            service="calendar",
            oauth_client_id=exp_row.oauth_client_id,
            provider_account_id=exp_row.provider_account_id,
            auth_epoch=exp_row.auth_epoch,
            token_generation=2,
            granted_scopes=exp_row.granted_scopes,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        get_conn_mock = create_autospec(db.get_connection, return_value=exp_row)
        list_acc_mock = create_autospec(db.list_connections_by_account, return_value=[exp_row])
        monkeypatch.setattr(oauth_mod, "get_db_connection", _tracked_pool)
        monkeypatch.setattr(db, "get_connection", get_conn_mock)
        monkeypatch.setattr(db, "list_connections_by_account", list_acc_mock)

        async def _fake_commit(**kw: Any) -> Any:
            assert kw["expected_attempt_gen"] == 1
            events.append("commit_step3")
            return (fresh_tok, None) if kw.get("return_outcome") else fresh_tok

        monkeypatch.setattr(
            oauth_mod,
            "_commit_refresh_success_tx",
            create_autospec(oauth_mod._commit_refresh_success_tx, side_effect=_fake_commit),
        )

        g_client = create_autospec(GoogleOAuthClient, instance=True)

        async def _http_refresh(**_k: Any) -> OAuthTokenResponse:
            # Self-check: ZERO advisory locks held AND ZERO DB connections held during HTTP!
            assert held_advisory_locks() == ()
            assert open_conns["count"] == 0
            events.append("http_refresh")
            return OAuthTokenResponse(
                access_token="at-refreshed",
                refresh_token="rt-refreshed",
                expires_in=3600,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scope=exp_row.granted_scopes,
            )

        g_client.refresh_token.side_effect = _http_refresh

        with advisory_lock_http_guard(True):
            got = await ensure_fresh_access_token(
                LOCAL_DEV_USER_ID, "google", "calendar", google_client=g_client
            )
        assert got.access_token == "at-refreshed"
        assert events == [
            "try_lock",
            "claim_attempt_gen",
            "tx_commit",
            "http_refresh",
            "lock",
            "commit_step3",
            "tx_commit",
        ]

        # Loser path: when active refresh_inflight_until lease is seen, exits lock & DB conn BEFORE polling!
        leased_row = _make_integration_row(
            expires_at=datetime.now(UTC) - timedelta(seconds=60),
            token_generation=1,
            refresh_inflight_gen=1,
        )
        import dataclasses

        leased_row = dataclasses.replace(
            leased_row,
            refresh_inflight_until=datetime.now(UTC) + timedelta(seconds=20),
            db_now=datetime.now(UTC),
        )
        get_conn_mock.return_value = leased_row
        list_acc_mock.return_value = [leased_row]

        async def _fake_waiter(**_k: Any) -> oauth_mod.FreshAccessToken:
            assert held_advisory_locks() == ()
            assert open_conns["count"] == 0
            return fresh_tok

        monkeypatch.setattr(
            oauth_mod,
            "_wait_for_refresh_winner",
            create_autospec(oauth_mod._wait_for_refresh_winner, side_effect=_fake_waiter),
        )
        g_client.refresh_token.reset_mock()
        with advisory_lock_http_guard(True):
            got_loser = await ensure_fresh_access_token(
                LOCAL_DEV_USER_ID, "google", "calendar", google_client=g_client
            )
        assert got_loser.access_token == "at-refreshed"
        g_client.refresh_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_r5_health_sweep_3step_releases_global_lock_and_conn_before_upstream_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lead §1 + Codex High #1 + Grok High #1: health sweep releases global advisory lock & DB pool conn before calling ensure_fresh_access_token."""
        from src.dalton.integrations.lock_guard import (
            advisory_lock_http_guard,
            held_advisory_locks,
        )

        _install_saved_credentials()
        open_conns = {"count": 0}
        timeline: list[str] = []

        class _SwCur:
            def __init__(self) -> None:
                self._last_sql = ""

            async def __aenter__(self) -> _SwCur:
                return self

            async def __aexit__(self, *args: Any) -> bool:
                return False

            async def execute(self, sql: str, params: Any = None) -> None:
                self._last_sql = sql
                if "pg_try_advisory_xact_lock" in sql or "pg_try_advisory_lock" in sql:
                    timeline.append("global_try_lock")

            async def fetchone(self) -> Any:
                if "pg_try_advisory_xact_lock" in self._last_sql or "pg_try_advisory_lock" in self._last_sql:
                    return (True,)
                return None

        class _SwConn:
            def cursor(self, **_k: Any) -> _SwCur:
                return _SwCur()

            async def execute(self, sql: str, params: Any = None) -> None:
                if "pg_advisory_xact_lock" in sql or "pg_advisory_lock" in sql:
                    timeline.append("global_lock_step")

            @asynccontextmanager
            async def transaction(self) -> Any:
                yield
                timeline.append("tx_end")

        @asynccontextmanager
        async def _sw_pool(_conn: Any = None) -> Any:
            open_conns["count"] += 1
            try:
                yield _SwConn()
            finally:
                open_conns["count"] -= 1

        read_sweep_mock = create_autospec(db.read_health_sweep_state, return_value=None)
        monkeypatch.setattr(sweep_mod, "get_db_connection", _sw_pool)
        monkeypatch.setattr(db, "read_health_sweep_state", read_sweep_mock)
        monkeypatch.setattr(db, "update_health_sweep_state", create_autospec(db.update_health_sweep_state, return_value=5))
        monkeypatch.setattr(db, "delete_expired_oauth_states_and_confirmations", create_autospec(db.delete_expired_oauth_states_and_confirmations, return_value=(0, 0)))
        monkeypatch.setattr(db, "list_orphaned_revoked_purging_connections", create_autospec(db.list_orphaned_revoked_purging_connections, return_value=[]))
        row_g = _make_integration_row(user_id="u-sw", provider="google", service="calendar")
        monkeypatch.setattr(db, "list_connections_for_health_sweep", create_autospec(db.list_connections_for_health_sweep, return_value=[row_g]))

        async def _check_refresh(*_a: Any, **_k: Any) -> Any:
            assert held_advisory_locks() == ()
            assert open_conns["count"] == 0
            timeline.append("refresh_group")
            return oauth_mod._to_fresh_token(row_g)

        monkeypatch.setattr(
            sweep_mod, "ensure_fresh_access_token", create_autospec(sweep_mod.ensure_fresh_access_token, side_effect=_check_refresh)
        )

        with advisory_lock_http_guard(True):
            rep = await run_integration_health_sweep_once(force=True)
        assert rep.acquired is True
        assert rep.healthy == 1
        assert timeline == [
            "global_try_lock",
            "tx_end",
            "global_lock_step",
            "tx_end",
            "refresh_group",
            "global_lock_step",
            "tx_end",
        ]

        # Active inflight_until lease blocks a second worker immediately with lock_busy
        read_sweep_mock.return_value = {
            "sweep_gen": 5,
            "inflight_until": datetime.now(UTC) + timedelta(minutes=5),
            "last_completed_at": None,
            "db_now": datetime.now(UTC),
        }
        rep_busy = await run_integration_health_sweep_once(force=True)
        assert rep_busy.acquired is False
        assert rep_busy.skipped_reason == "lock_busy"

    @pytest.mark.asyncio
    async def test_r5_finish_disconnect_releases_purge_lock_before_remote_revoke_http(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lead §1 + Codex High #1: finish_disconnect_locked exits try_purge_ledger_lock BEFORE invoking process_revoke / HTTP."""
        from src.dalton.integrations.lock_guard import (
            advisory_lock_http_guard,
            held_advisory_locks,
            track_advisory_lock,
        )
        from src.dalton.services import integration_data_purge as purge_mod
        from src.dalton.services import user_integrations_lifecycle as lc_mod

        pid = str(uuid.uuid4())
        done_ledger = PurgeLedgerRow(
            purge_id=pid,
            user_id=LOCAL_DEV_USER_ID,
            provider="google",
            service="calendar",
            oauth_client_id="111222333-abc.apps.googleusercontent.com",
            provider_account_id="sub-disc",
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
            revoke_token="rt-disc",
        )
        events: list[str] = []

        @asynccontextmanager
        async def _fake_purge_lock(_pid: str) -> Any:
            events.append("enter_purge_lock")
            with track_advisory_lock("purge_ledger", 999):
                yield True
            events.append("exit_purge_lock")

        monkeypatch.setattr(purge_mod, "try_purge_ledger_lock", _fake_purge_lock)
        monkeypatch.setattr(db, "get_purge_ledger", create_autospec(db.get_purge_ledger, return_value=done_ledger))

        async def _fake_process_revoke(arg_pid: str, revoker: Any) -> RemoteRevokeStatus:
            assert arg_pid == pid
            assert held_advisory_locks() == ()
            events.append("process_revoke")
            return RemoteRevokeStatus.SUCCEEDED

        monkeypatch.setattr(lc_mod, "process_revoke", create_autospec(lc_mod.process_revoke, side_effect=_fake_process_revoke))
        rev = create_autospec(RemoteRevoker, instance=True)

        with advisory_lock_http_guard(True):
            res = await lc_mod.finish_disconnect_locked(pid, revoker=rev)
        assert res is not None
        assert res.remote_revoke_status == RemoteRevokeStatus.SUCCEEDED
        assert events == ["enter_purge_lock", "exit_purge_lock", "process_revoke"]

    @pytest.mark.asyncio
    async def test_r5_feishu_public_event_requires_both_encrypt_key_and_verification_token(
        self,
    ) -> None:
        """Codex Med #1: Feishu public revocation endpoint rejects single-key configs (only verification_token OR only encrypt_key) with 503."""
        from src.dalton.capabilities import get_connector_providers_summary
        from src.dalton.services.feishu_integration_events import FeishuEventNotConfiguredError

        now_ts = str(int(time.time()))
        raw_event = json.dumps(
            {
                "header": {
                    "event_type": FEISHU_REVOKED_EVENT_TYPE,
                    "event_id": "evt-single-key",
                    "token": "ver-token-only",
                },
                "event": {"open_id": "ou_1", "revoke_reason": "Revoked by user action."},
            }
        ).encode("utf-8")

        # 1) Only Verification Token configured -> event_credentials_ready=False & verify rejects with 503!
        _install_saved_credentials(
            feishu_encrypt_key=None, feishu_verification_token="ver-token-only"
        )
        assert get_connector_providers_summary()["feishu"]["event_credentials_ready"] is False
        with pytest.raises(FeishuEventNotConfiguredError):
            verify_and_decode_feishu_event(raw_body=raw_event, headers={})

        # 2) Only Encrypt Key configured -> event_credentials_ready=False & verify rejects with 503!
        _install_saved_credentials(
            feishu_encrypt_key="enc-key-only", feishu_verification_token=None
        )
        assert get_connector_providers_summary()["feishu"]["event_credentials_ready"] is False
        with pytest.raises(FeishuEventNotConfiguredError):
            verify_and_decode_feishu_event(raw_body=raw_event, headers={})

        # 3) Both keys configured -> url_verification handshake passes without signature,
        #    while real event strictly requires signature + timestamp + nonce + token!
        _install_saved_credentials(
            feishu_encrypt_key="enc-key-both", feishu_verification_token="ver-token-both"
        )
        assert get_connector_providers_summary()["feishu"]["event_credentials_ready"] is True

        challenge_body = json.dumps(
            {"type": "url_verification", "challenge": "ch-ok", "token": "ver-token-both"}
        ).encode("utf-8")
        decoded_ch = verify_and_decode_feishu_event(raw_body=challenge_body, headers={})
        assert decoded_ch["challenge"] == "ch-ok"

        real_body = json.dumps(
            {
                "header": {
                    "event_type": FEISHU_REVOKED_EVENT_TYPE,
                    "event_id": "evt-both-keys",
                    "token": "ver-token-both",
                },
                "event": {"open_id": "ou_1", "revoke_reason": "Revoked by user action."},
            }
        ).encode("utf-8")
        with pytest.raises(FeishuEventAuthError) as missing_sig_exc:
            verify_and_decode_feishu_event(raw_body=real_body, headers={})
        assert missing_sig_exc.value.code == "missing_signature_headers"

        sig = compute_feishu_event_signature(
            timestamp=now_ts,
            nonce="n1",
            encrypt_key="enc-key-both",
            raw_body=real_body,
        )
        decoded_ev = verify_and_decode_feishu_event(
            raw_body=real_body,
            headers={
                "X-Lark-Request-Timestamp": now_ts,
                "X-Lark-Request-Nonce": "n1",
                "X-Lark-Signature": sig,
            },
        )
        assert decoded_ev["header"]["event_id"] == "evt-both-keys"

    @pytest.mark.asyncio
    async def test_r5_feishu_crash_takeover_wipes_refresh_token_and_never_replays_after_expiry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex Med #2: same Feishu row goes crash takeover (wipes refresh_token, keeps access_token) -> serves until expiry -> expires -> needs_reauth with 0 upstream calls."""
        import dataclasses

        _install_saved_credentials()
        fake_ctx, _ = _make_fake_lock_pool(try_lock_results=[True, True, True])
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        f_client = create_autospec(FeishuOAuthClient, instance=True)

        row_state = [
            _make_integration_row(
                provider="feishu",
                service="calendar",
                oauth_client_id="cli_test_app_1",
                provider_account_id="ou_full_lifecycle",
                token_generation=2,
                refresh_inflight_gen=2,
                access_token="at-live-before-exp",
                refresh_token="rt-possibly-consumed",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        ]

        async def _stateful_get(*_a: Any, **_k: Any) -> IntegrationRow:
            return row_state[0]

        async def _stateful_update(
            user_id: str,
            provider: str,
            service: str,
            *,
            status: IntegrationStatus | str,
            last_error_code: str | None = None,
            clear_tokens: bool = False,
            clear_refresh_token_only: bool = False,
            expected_auth_epoch: int | None = None,
            conn: Any = None,
        ) -> bool:
            _ = (user_id, provider, service, expected_auth_epoch, conn)
            r = row_state[0]
            new_at = None if clear_tokens else r.access_token
            new_rt = None if (clear_tokens or clear_refresh_token_only) else r.refresh_token
            row_state[0] = dataclasses.replace(
                r,
                status=IntegrationStatus(status),
                last_error_code=last_error_code or r.last_error_code,
                refresh_inflight_gen=None,
                refresh_inflight_until=None,
                access_token=new_at,
                refresh_token=new_rt,
                has_access_token=new_at is not None,
                has_refresh_token=new_rt is not None,
            )
            return True

        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, side_effect=_stateful_get))
        monkeypatch.setattr(db, "update_connection_status", create_autospec(db.update_connection_status, side_effect=_stateful_update))

        # 1) Takeover while unexpired: marks REFRESH_AMBIGUOUS, wipes refresh_token, preserves access_token, 0 upstream calls
        tok = await ensure_fresh_access_token(
            LOCAL_DEV_USER_ID, "feishu", "calendar", min_remaining_seconds=3600, feishu_client=f_client
        )
        assert tok.access_token == "at-live-before-exp"
        assert row_state[0].status == IntegrationStatus.REFRESH_AMBIGUOUS
        assert row_state[0].refresh_token is None
        assert row_state[0].access_token == "at-live-before-exp"
        f_client.refresh_token.assert_not_awaited()

        # 2) Time advances past access_token expiry on the SAME row:
        #    MUST converge directly to NEEDS_REAUTH with 0 upstream calls (never replays old refresh token!)
        row_state[0] = dataclasses.replace(
            row_state[0], expires_at=datetime.now(UTC) - timedelta(seconds=5)
        )
        with pytest.raises(IntegrationNeedsReauthError):
            await ensure_fresh_access_token(
                LOCAL_DEV_USER_ID, "feishu", "calendar", feishu_client=f_client
            )
        assert row_state[0].status == IntegrationStatus.NEEDS_REAUTH
        assert row_state[0].access_token is None
        assert row_state[0].refresh_token is None
        f_client.refresh_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_r5_everflow_confirm_keeps_pending_until_activation_tx_and_revokes_on_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex High #2 + Grok Med #2: confirm_pending_integration consumes pending row INSIDE _finalize_connection tx; on failure, pending row is still there and gets revoked."""
        _install_saved_credentials()
        raw_tok = "confirm-secret-r5"
        chash = hashlib.sha256(raw_tok.encode()).hexdigest()
        pending = PendingConfirmationRow(
            confirmation_hash=chash,
            user_id="sb-u1",
            provider="google",
            service="calendar",
            oauth_client_id="111222333-abc.apps.googleusercontent.com",
            provider_account_id="sub-r5",
            provider_union_id=None,
            provider_email="r5@example.com",
            granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            intent="connect",
            preferences={},
            confirm_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            created_at=datetime.now(UTC),
            access_token="at-r5",
            refresh_token="rt-r5-staged",
        )
        store: dict[tuple[str, str], PendingConfirmationRow] = {(chash, "sb-u1"): pending}

        monkeypatch.setattr(
            db, "peek_pending_confirmation",
            create_autospec(db.peek_pending_confirmation, side_effect=lambda h, u, **k: store.get((h, u))),
        )
        consume_spy = create_autospec(
            db.consume_pending_confirmation, side_effect=lambda h, u, **k: store.pop((h, u), None)
        )
        monkeypatch.setattr(db, "consume_pending_confirmation", consume_spy)
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=None))
        monkeypatch.setattr(db, "has_inflight_google_revoke_in_project", create_autospec(db.has_inflight_google_revoke_in_project, return_value=False))

        revoke_abandoned_spy = create_autospec(
            oauth_mod._revoke_abandoned_google_pending_confirmation, return_value=None
        )
        monkeypatch.setattr(
            oauth_mod, "_revoke_abandoned_google_pending_confirmation", revoke_abandoned_spy
        )

        async def _failing_finalize(**kw: Any) -> Any:
            # Pending row MUST STILL exist in store when _finalize_connection begins!
            assert (chash, "sb-u1") in store
            assert kw["consume_confirmation_hash"] == chash
            raise PendingConfirmationInvalidError(
                "Changed before activation", code="account_switch_state_changed", status_code=409
            )

        monkeypatch.setattr(
            oauth_mod, "_finalize_connection", create_autospec(oauth_mod._finalize_connection, side_effect=_failing_finalize)
        )
        rev = create_autospec(RemoteRevoker, instance=True)

        with pytest.raises(PendingConfirmationInvalidError):
            await confirm_pending_integration(
                user_id="sb-u1", confirmation_token=raw_tok, revoker=rev
            )
        # On failure, pending row was consumed in error handler and handed to revoke!
        assert (chash, "sb-u1") not in store
        revoke_abandoned_spy.assert_awaited_once()
        assert revoke_abandoned_spy.await_args.args[0].refresh_token == "rt-r5-staged"

    @pytest.mark.asyncio
    async def test_r5_core_direct_callback_aborts_and_revokes_if_commit_guard_voided_by_disconnect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Decision 4 + Codex High #1/#4: if concurrent disconnect voids the connect intent before _finalize_connection commits, callback aborts without resurrecting connection and revokes new Google token."""
        _install_saved_credentials()
        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "peek_oauth_state", create_autospec(db.peek_oauth_state, return_value=(LOCAL_DEV_USER_ID, "111222333-abc.apps.googleusercontent.com")))
        monkeypatch.setattr(db, "has_inflight_google_revoke_in_project", create_autospec(db.has_inflight_google_revoke_in_project, return_value=False))
        monkeypatch.setattr(db, "consume_oauth_state", create_autospec(db.consume_oauth_state, return_value=_make_state_row()))
        monkeypatch.setattr(db, "delete_oauth_state", create_autospec(db.delete_oauth_state, return_value=None))
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=None))
        monkeypatch.setattr(db, "has_running_local_purge_for_service", create_autospec(db.has_running_local_purge_for_service, return_value=[]))
        monkeypatch.setattr(db, "supersede_active_revokes_for_google_project_sub_tx", create_autospec(db.supersede_active_revokes_for_google_project_sub_tx, return_value=0))
        # Simulate concurrent DELETE having voided the connect intent in oauth_states -> consume returns ("voided", None)!
        monkeypatch.setattr(db, "consume_active_oauth_intent_tx", create_autospec(db.consume_active_oauth_intent_tx, return_value=("voided", None)))
        upsert_spy = create_autospec(db.upsert_connection)
        monkeypatch.setattr(db, "upsert_connection", upsert_spy)
        revoke_tok_spy = create_autospec(oauth_mod._revoke_abandoned_google_token, return_value=None)
        monkeypatch.setattr(oauth_mod, "_revoke_abandoned_google_token", revoke_tok_spy)

        g_client = create_autospec(GoogleOAuthClient, instance=True)
        g_client.exchange_code.return_value = OAuthTokenResponse(
            access_token="at-discard",
            refresh_token="rt-discard",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token="id.discard",
        )
        g_client.verify_id_token.return_value = GoogleIdTokenClaims(
            sub="sub-disc-race",
            email="disc@example.com",
            email_verified=True,
            aud="111222333-abc.apps.googleusercontent.com",
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )
        rev = create_autospec(RemoteRevoker, instance=True)

        cb = await handle_oauth_callback(
            provider="google", code="c-disc", state="s-disc", google_client=g_client, revoker=rev
        )
        assert cb.status == "error"
        assert cb.error_code == "connection_disconnected_during_callback"
        upsert_spy.assert_not_awaited()
        revoke_tok_spy.assert_awaited_once()
        assert revoke_tok_spy.await_args.kwargs["token"] == "rt-discard"

    @pytest.mark.asyncio
    async def test_r5_unconsumed_state_passes_sub_to_deferral_and_single_inflight_per_project_sub(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Grok Med #1 + Codex High #3: process_revoke passes sub to get_active_google_oauth_state_expiry and skips when another ledger for (project, sub) is already in-flight."""
        from src.dalton.services import integration_revoke as revoke_mod

        pid = str(uuid.uuid4())
        ledger = PurgeLedgerRow(
            purge_id=pid,
            user_id="member-a",
            provider="google",
            service="calendar",
            oauth_client_id="111222333-app.apps.googleusercontent.com",
            provider_account_id="sub-target-b",
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
            revoke_token="rt-b",
        )
        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(revoke_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "get_purge_ledger", create_autospec(db.get_purge_ledger, return_value=ledger))
        monkeypatch.setattr(db, "has_running_local_purge_for_user_provider", create_autospec(db.has_running_local_purge_for_user_provider, return_value=False))
        monkeypatch.setattr(revoke_mod, "_has_newer_active_google_connection", create_autospec(revoke_mod._has_newer_active_google_connection, return_value=False))

        # 1) Another ledger for the same (project, sub) is already in-flight -> skip without calling HTTP!
        inflight_sub_mock = create_autospec(db.has_inflight_google_revoke_for_project_sub, return_value=True)
        monkeypatch.setattr(db, "has_inflight_google_revoke_for_project_sub", inflight_sub_mock)
        revoker = create_autospec(RemoteRevoker, instance=True)
        st_busy = await revoke_mod.process_revoke(pid, revoker)
        assert st_busy == RemoteRevokeStatus.PENDING
        revoker.revoke.assert_not_awaited()

        # 2) No sibling in-flight -> passes exact sub to get_active_google_oauth_state_expiry!
        inflight_sub_mock.return_value = False
        exp_spy = create_autospec(db.get_active_google_oauth_state_expiry, return_value=None)
        monkeypatch.setattr(db, "get_active_google_oauth_state_expiry", exp_spy)
        monkeypatch.setattr(db, "claim_revoke_attempt_tx", create_autospec(db.claim_revoke_attempt_tx, return_value=1))
        import dataclasses

        monkeypatch.setattr(
            db, "finalize_revoke_attempt_tx",
            create_autospec(
                db.finalize_revoke_attempt_tx,
                return_value=dataclasses.replace(ledger, remote_revoke_status=RemoteRevokeStatus.SUCCEEDED),
            ),
        )
        revoker.revoke.return_value = RevokeOutcome.SUCCEEDED
        st_ok = await revoke_mod.process_revoke(pid, revoker)
        assert st_ok == RemoteRevokeStatus.SUCCEEDED
        exp_spy.assert_awaited_once()
        assert exp_spy.await_args.args == ("111222333",)
        assert exp_spy.await_args.kwargs["sub"] == "sub-target-b"


# ---------------------------------------------------------------------------
# Round 6 Narrowed Scope Fixes (Decisions 1–5 + Grok Medium/Low)
# ---------------------------------------------------------------------------


class TestRound6NarrowedFixes:
    @pytest.mark.asyncio
    async def test_r6_decision1_second_member_same_google_account_rejected_without_revoking_owner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Decision 1: connecting a Google (project, sub) already owned by another Dalton member rejects with exact Chinese prompt and NEVER revokes the owner's grant."""
        _install_saved_credentials()
        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "peek_oauth_state", create_autospec(db.peek_oauth_state, return_value=(LOCAL_DEV_USER_ID, "111222333-abc.apps.googleusercontent.com")))
        monkeypatch.setattr(db, "has_inflight_google_revoke_in_project", create_autospec(db.has_inflight_google_revoke_in_project, return_value=False))
        monkeypatch.setattr(db, "consume_oauth_state", create_autospec(db.consume_oauth_state, return_value=_make_state_row()))
        monkeypatch.setattr(db, "delete_oauth_state", create_autospec(db.delete_oauth_state, return_value=None))
        monkeypatch.setattr(db, "has_other_user_google_account_claim", create_autospec(db.has_other_user_google_account_claim, return_value=True))
        revoke_spy = create_autospec(oauth_mod._revoke_abandoned_google_token, return_value=None)
        monkeypatch.setattr(oauth_mod, "_revoke_abandoned_google_token", revoke_spy)
        upsert_spy = create_autospec(db.upsert_connection)
        monkeypatch.setattr(db, "upsert_connection", upsert_spy)

        g_client = create_autospec(GoogleOAuthClient, instance=True)
        g_client.exchange_code.return_value = OAuthTokenResponse(
            access_token="at-b",
            refresh_token="rt-b",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token="id.b",
        )
        g_client.verify_id_token.return_value = GoogleIdTokenClaims(
            sub="sub-owned-by-member-a",
            email="shared@example.com",
            email_verified=True,
            aud="111222333-abc.apps.googleusercontent.com",
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )

        cb = await handle_oauth_callback(
            provider="google", code="c-b", state="s-b", google_client=g_client
        )
        assert cb.status == "error"
        assert cb.error_code == "google_account_already_connected"
        assert cb.error_message == "这个 Google 账号已被团队里另一位成员连接，请换一个账号或请对方先断开"
        upsert_spy.assert_not_awaited()
        revoke_spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_r6_decision2_short_lock_tx_never_borrows_second_pooled_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Decision 2: disconnect, refresh, revoke, and health sweep each hold at most ONE pooled connection at any time (pg_advisory_xact_lock on same conn)."""
        from src.dalton.services import integration_revoke as revoke_mod
        from src.dalton.services import user_integrations_lifecycle as lc_mod

        _install_saved_credentials()
        conn_stats = {"cur": 0, "max": 0}
        base_ctx, _ = _make_fake_lock_pool()

        @asynccontextmanager
        async def _strict_single_conn(_existing: Any = None) -> Any:
            if _existing is not None:
                yield _existing
                return
            conn_stats["cur"] += 1
            conn_stats["max"] = max(conn_stats["max"], conn_stats["cur"])
            assert conn_stats["cur"] <= 1, f"Nested pool borrow detected: cur={conn_stats['cur']}"
            try:
                async with base_ctx() as c:
                    yield c
            finally:
                conn_stats["cur"] -= 1

        monkeypatch.setattr(oauth_mod, "get_db_connection", _strict_single_conn)
        monkeypatch.setattr(revoke_mod, "get_db_connection", _strict_single_conn)
        monkeypatch.setattr(sweep_mod, "get_db_connection", _strict_single_conn)
        monkeypatch.setattr(lc_mod, "get_db_connection", _strict_single_conn)

        # 1) Health sweep
        monkeypatch.setattr(db, "read_health_sweep_state", create_autospec(db.read_health_sweep_state, return_value=None))
        monkeypatch.setattr(db, "update_health_sweep_state", create_autospec(db.update_health_sweep_state, return_value=1))
        monkeypatch.setattr(db, "delete_expired_oauth_states_and_confirmations", create_autospec(db.delete_expired_oauth_states_and_confirmations, return_value=(0, 0)))
        monkeypatch.setattr(db, "list_connections_for_health_sweep", create_autospec(db.list_connections_for_health_sweep, return_value=[]))
        monkeypatch.setattr(db, "list_orphaned_revoked_purging_connections", create_autospec(db.list_orphaned_revoked_purging_connections, return_value=[]))
        rep = await run_integration_health_sweep_once(force=True)
        assert rep.acquired is True

        # 2) Refresh winner
        exp_row = _make_integration_row(expires_at=datetime.now(UTC) - timedelta(seconds=60))
        fresh_tok = oauth_mod._to_fresh_token(_make_integration_row(token_generation=2))
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=exp_row))
        monkeypatch.setattr(db, "list_connections_by_account", create_autospec(db.list_connections_by_account, return_value=[exp_row]))
        monkeypatch.setattr(oauth_mod, "_commit_refresh_success_tx", create_autospec(oauth_mod._commit_refresh_success_tx, return_value=(fresh_tok, None)))
        g_client = create_autospec(GoogleOAuthClient, instance=True)
        g_client.refresh_token.return_value = OAuthTokenResponse(
            access_token="at-new",
            refresh_token="rt-new",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=exp_row.granted_scopes,
        )
        got = await ensure_fresh_access_token(
            LOCAL_DEV_USER_ID, "google", "calendar", google_client=g_client
        )
        assert got.token_generation == 2
        assert conn_stats["max"] == 1

    @pytest.mark.asyncio
    async def test_r6_decision3_health_sweep_claim_fail_exits_and_lease_loss_stops_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Decision 3 + Grok Low #1: sweep exits immediately on claim/list failure (rolling back lease), renews before each group, and stops immediately when lease is lost."""
        _install_saved_credentials()
        fake_ctx, executed = _make_fake_lock_pool()
        monkeypatch.setattr(sweep_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "read_health_sweep_state", create_autospec(db.read_health_sweep_state, return_value={"sweep_gen": 1, "inflight_until": None, "last_completed_at": None, "db_now": datetime.now(UTC)}))
        monkeypatch.setattr(db, "delete_expired_oauth_states_and_confirmations", create_autospec(db.delete_expired_oauth_states_and_confirmations, return_value=(0, 0)))
        monkeypatch.setattr(db, "list_orphaned_revoked_purging_connections", create_autospec(db.list_orphaned_revoked_purging_connections, return_value=[]))
        refresh_spy = create_autospec(sweep_mod.ensure_fresh_access_token)
        monkeypatch.setattr(sweep_mod, "ensure_fresh_access_token", refresh_spy)

        # (a) Claim returns None -> exit immediately with claim_failed, 0 refreshes
        upd_spy = create_autospec(db.update_health_sweep_state, return_value=None)
        monkeypatch.setattr(db, "update_health_sweep_state", upd_spy)
        rep_fail = await run_integration_health_sweep_once(force=True)
        assert rep_fail.acquired is False
        assert rep_fail.skipped_reason == "claim_failed"
        refresh_spy.assert_not_awaited()

        # (b) Listing candidates raises -> transaction rolls back (no leaked lease), exits with claim_failed
        executed.clear()
        upd_spy.return_value = 2
        list_spy = create_autospec(db.list_connections_for_health_sweep, side_effect=RuntimeError("db_boom"))
        monkeypatch.setattr(db, "list_connections_for_health_sweep", list_spy)
        rep_boom = await run_integration_health_sweep_once(force=True)
        assert rep_boom.acquired is False
        assert rep_boom.skipped_reason == "claim_failed"
        assert ("TX_ROLLBACK", None) in executed
        refresh_spy.assert_not_awaited()

        # (c) Two groups: renew succeeds for group 1, fails (lease taken over) before group 2 -> stops after 1 group, does NOT stamp completed!
        r1 = _make_integration_row(user_id="u-1", provider="google", service="calendar", provider_account_id="sub-1")
        r2 = _make_integration_row(user_id="u-2", provider="google", service="calendar", provider_account_id="sub-2")
        upd_spy.reset_mock()
        upd_spy.return_value = 3
        list_spy.side_effect = None
        list_spy.return_value = [r1, r2]
        renew_spy = create_autospec(db.renew_health_sweep_lease, side_effect=[True, False])
        monkeypatch.setattr(db, "renew_health_sweep_lease", renew_spy)
        refresh_spy.return_value = oauth_mod._to_fresh_token(r1)

        rep_lost = await run_integration_health_sweep_once(force=True)
        assert rep_lost.acquired is True
        assert rep_lost.skipped_reason == "lease_lost"
        assert rep_lost.checked == 1
        assert rep_lost.healthy == 1
        assert refresh_spy.await_count == 1
        assert renew_spy.await_count == 2
        # Never called update_health_sweep_state(completed=True) after lease loss!
        assert not any(call.kwargs.get("completed") for call in upd_spy.await_args_list)

    @pytest.mark.asyncio
    async def test_r6_decision4_exchange_timeout_lease_expiry_and_missing_scope_revoke(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Decision 4 + Grok Medium #1: exchange total timeout aborts cleanly; lease expiry in Step 3 discards & revokes; missing required scope on first connect revokes minted refresh_token; failed exchange_code does NOT revoke."""
        _install_saved_credentials()
        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "peek_oauth_state", create_autospec(db.peek_oauth_state, return_value=(LOCAL_DEV_USER_ID, "111222333-abc.apps.googleusercontent.com")))
        monkeypatch.setattr(db, "has_inflight_google_revoke_in_project", create_autospec(db.has_inflight_google_revoke_in_project, return_value=False))
        monkeypatch.setattr(db, "consume_oauth_state", create_autospec(db.consume_oauth_state, return_value=_make_state_row()))
        monkeypatch.setattr(db, "delete_oauth_state", create_autospec(db.delete_oauth_state, return_value=None))
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=None))
        monkeypatch.setattr(db, "has_running_local_purge_for_service", create_autospec(db.has_running_local_purge_for_service, return_value=[]))
        revoke_spy = create_autospec(oauth_mod._revoke_abandoned_google_token, return_value=None)
        monkeypatch.setattr(oauth_mod, "_revoke_abandoned_google_token", revoke_spy)

        # (a) Slow exchange_code exceeds exchange_timeout_seconds -> exchange_timeout, 0 revokes (no token minted)
        g_slow = create_autospec(GoogleOAuthClient, instance=True)

        async def _hang(**_k: Any) -> OAuthTokenResponse:
            await asyncio.sleep(2.0)
            raise AssertionError("unreachable")

        g_slow.exchange_code.side_effect = _hang
        cb_to = await handle_oauth_callback(
            provider="google", code="c-to", state="s-to", google_client=g_slow, exchange_timeout_seconds=0.05
        )
        assert cb_to.status == "error"
        assert cb_to.error_code == "exchange_timeout"
        revoke_spy.assert_not_awaited()

        # (b) Grok Med #1: first connect missing required scope -> missing_required_scopes AND revokes minted refresh_token!
        g_scope = create_autospec(GoogleOAuthClient, instance=True)
        g_scope.exchange_code.return_value = OAuthTokenResponse(
            access_token="at-part",
            refresh_token="rt-abandoned-scope",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope="openid email",
            id_token="id.part",
        )
        g_scope.verify_id_token.return_value = GoogleIdTokenClaims(
            sub="sub-scope-fail",
            email="sf@example.com",
            email_verified=True,
            aud="111222333-abc.apps.googleusercontent.com",
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )
        cb_sc = await handle_oauth_callback(
            provider="google", code="c-sc", state="s-sc", google_client=g_scope
        )
        assert cb_sc.status == "error"
        assert cb_sc.error_code == "missing_required_scopes"
        revoke_spy.assert_awaited_once()
        assert revoke_spy.await_args.kwargs["token"] == "rt-abandoned-scope"
        assert revoke_spy.await_args.kwargs["provider_account_id"] == "sub-scope-fail"

        # (c) Exchange lease expired before Step 3 activation -> exchange_lease_expired AND revokes minted token!
        revoke_spy.reset_mock()
        monkeypatch.setattr(db, "consume_active_oauth_intent_tx", create_autospec(db.consume_active_oauth_intent_tx, return_value=("lease_expired", None)))
        g_exp = create_autospec(GoogleOAuthClient, instance=True)
        g_exp.exchange_code.return_value = OAuthTokenResponse(
            access_token="at-late",
            refresh_token="rt-late-expired-lease",
            expires_in=3600,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            id_token="id.late",
        )
        g_exp.verify_id_token.return_value = GoogleIdTokenClaims(
            sub="sub-late",
            email="late@example.com",
            email_verified=True,
            aud="111222333-abc.apps.googleusercontent.com",
            iss="https://accounts.google.com",
            exp=int(time.time()) + 3600,
        )
        cb_exp = await handle_oauth_callback(
            provider="google", code="c-late", state="s-late", google_client=g_exp
        )
        assert cb_exp.status == "error"
        assert cb_exp.error_code == "exchange_lease_expired"
        revoke_spy.assert_awaited_once()
        assert revoke_spy.await_args.kwargs["token"] == "rt-late-expired-lease"

    @pytest.mark.asyncio
    async def test_r6_decision5_disconnect_branch_cas_discards_stale_sibling_update_after_reauth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Decision 5 + Codex High #4: when calendar disconnects and Gmail re-authorizes during calendar's in-flight refresh, Step 3 checks claimed_targets and discards stale refresh without overwriting Gmail."""
        import dataclasses

        _install_saved_credentials()
        fake_ctx, executed = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)

        cal_disc = dataclasses.replace(
            _make_integration_row(service="calendar", auth_epoch=1, token_generation=1, status=IntegrationStatus.DISCONNECT_PENDING),
            refresh_attempt_gen=2,
        )
        mail_reauthed = dataclasses.replace(
            _make_integration_row(service="mail", auth_epoch=1, token_generation=2, status=IntegrationStatus.CONNECTED, access_token="at-fresh-reauth", refresh_token="rt-fresh-reauth"),
            refresh_attempt_gen=2,
        )
        monkeypatch.setattr(
            db, "list_connections_by_account",
            create_autospec(db.list_connections_by_account, return_value=[cal_disc, mail_reauthed]),
        )

        with pytest.raises(IntegrationEpochRevokedError):
            await oauth_mod._commit_refresh_success_tx(
                user_id=LOCAL_DEV_USER_ID,
                provider="google",
                service="calendar",
                start_client_id=cal_disc.oauth_client_id,
                start_sub=cal_disc.provider_account_id,
                start_auth_epoch=1,
                start_token_gen=1,
                token_resp=OAuthTokenResponse(
                    access_token="at-stale-refresh",
                    refresh_token="rt-stale-refresh",
                    expires_in=3600,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                    scope=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
                ),
                required_scopes=None,
                expected_attempt_gen=1,
                claimed_targets={
                    "calendar": (1, 1, 1),
                    "mail": (1, 1, 1),  # mail was claimed at tok_gen=1, attempt_gen=1; now at (1, 2, 2)!
                },
            )
        # Zero UPDATEs executed against mail (stale result discarded by CAS)!
        mail_updates = [
            sql for sql, params in executed
            if "UPDATE dalton_user_integrations" in sql and isinstance(params, dict) and params.get("service") == "mail"
        ]
        assert mail_updates == []


class TestRound7ReviewFixes:
    """Round 7 unit tests for Codex High #1/#2/#3 and Grok Medium #1/#2."""

    @pytest.mark.asyncio
    async def test_r7_item1_refresh_narrowed_scope_and_disconnect_sibling_commit_before_raising_and_clean_lease(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex High #1: ensure_fresh_access_token commits narrowed-scope needs_reauth AND disconnect-branch eligible sibling updates BEFORE raising business exceptions, and runs lease cleanup after commit."""
        import dataclasses

        from src.dalton.integrations.constants import GOOGLE_GMAIL_MIN_SCOPES

        _install_saved_credentials()
        now = datetime.now(UTC)

        # (a) Narrowed scope on refresh -> commits needs_reauth + new tokens, then releases lease & raises
        start_row = dataclasses.replace(
            _make_integration_row(
                service="calendar",
                auth_epoch=1,
                token_generation=1,
                expires_at=now - timedelta(seconds=30),
            ),
            refresh_attempt_gen=0,
        )
        claimed_row = dataclasses.replace(start_row, refresh_attempt_gen=1)
        returned_narrow = {
            "user_id": start_row.user_id,
            "provider": "google",
            "service": "calendar",
            "oauth_client_id": start_row.oauth_client_id,
            "provider_account_id": start_row.provider_account_id,
            "provider_union_id": None,
            "provider_email": start_row.provider_email,
            "granted_scopes": GOOGLE_SCOPE_CALENDAR_LIST_RO,
            "auth_epoch": 1,
            "authorized_at": start_row.authorized_at,
            "token_generation": 2,
            "status": "needs_reauth",
            "refresh_inflight_gen": None,
            "refresh_attempt_gen": 1,
            "refresh_started_at": None,
            "refresh_inflight_until": None,
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
        fake_ctx, executed = _make_fake_lock_pool(
            try_lock_results=[True],
            fetchone_results=[[("calendar", 1, 1, 1)], returned_narrow],
        )
        get_conn_spy = create_autospec(db.get_connection, return_value=start_row)
        list_acc_spy = create_autospec(
            db.list_connections_by_account, side_effect=[[start_row], [claimed_row]]
        )
        lease_spy = create_autospec(db.release_refresh_inflight_lease, return_value=None)
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "get_connection", get_conn_spy)
        monkeypatch.setattr(db, "list_connections_by_account", list_acc_spy)
        monkeypatch.setattr(db, "release_refresh_inflight_lease", lease_spy)

        g_narrow = create_autospec(GoogleOAuthClient, instance=True)
        g_narrow.refresh_token.return_value = OAuthTokenResponse(
            access_token="new-at-kept",
            refresh_token="new-rt-kept",
            expires_in=3600,
            expires_at=now + timedelta(hours=1),
            scope=GOOGLE_SCOPE_CALENDAR_LIST_RO,
        )
        with pytest.raises(IntegrationNeedsReauthError) as narrow_exc:
            await ensure_fresh_access_token(
                start_row.user_id, "google", "calendar", google_client=g_narrow
            )
        assert narrow_exc.value.code == "missing_required_scopes"
        assert executed.count(("TX_COMMIT", None)) == 2  # Step 1 claim + Step 3 commit!
        assert ("TX_ROLLBACK", None) not in executed
        lease_spy.assert_awaited_once()
        assert lease_spy.await_args.kwargs["preserve_crash_marker"] is False

        # (b) Calendar disconnects during HTTP while Mail still matches CAS -> commits Mail update + Calendar lease clear BEFORE raising
        cal_start = dataclasses.replace(
            _make_integration_row(
                service="calendar",
                auth_epoch=1,
                token_generation=1,
                expires_at=now - timedelta(seconds=30),
            ),
            refresh_attempt_gen=0,
        )
        mail_start = dataclasses.replace(
            _make_integration_row(
                service="mail",
                auth_epoch=1,
                token_generation=1,
                granted_scopes=" ".join(GOOGLE_GMAIL_MIN_SCOPES),
                expires_at=now - timedelta(seconds=30),
            ),
            refresh_attempt_gen=0,
        )
        cal_disc = dataclasses.replace(
            cal_start, status=IntegrationStatus.DISCONNECT_PENDING, refresh_attempt_gen=1
        )
        mail_eligible = dataclasses.replace(mail_start, refresh_attempt_gen=1)

        fake_ctx2, executed2 = _make_fake_lock_pool(
            try_lock_results=[True],
            fetchone_results=[[("calendar", 1, 1, 1), ("mail", 1, 1, 1)]],
        )
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx2)
        get_conn_spy.return_value = cal_start
        list_acc_spy.side_effect = [[cal_start, mail_start], [cal_disc, mail_eligible]]
        lease_spy.reset_mock()

        g_sib = create_autospec(GoogleOAuthClient, instance=True)
        g_sib.refresh_token.return_value = OAuthTokenResponse(
            access_token="at-for-mail",
            refresh_token="rt-for-mail",
            expires_in=3600,
            expires_at=now + timedelta(hours=1),
            scope=" ".join((*GOOGLE_CALENDAR_MIN_SCOPES, *GOOGLE_GMAIL_MIN_SCOPES)),
        )
        with pytest.raises(IntegrationEpochRevokedError):
            await ensure_fresh_access_token(
                cal_start.user_id, "google", "calendar", google_client=g_sib
            )
        assert executed2.count(("TX_COMMIT", None)) == 2  # Step 1 claim + Step 3 sibling commit!
        assert ("TX_ROLLBACK", None) not in executed2
        mail_updates = [
            params for sql, params in executed2
            if "UPDATE dalton_user_integrations" in sql and isinstance(params, dict) and params.get("service") == "mail"
        ]
        assert len(mail_updates) == 1
        assert mail_updates[0]["access"] == "at-for-mail"
        lease_spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_r7_item2_release_blob_reuses_caller_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex High #2: workspace_file_blobs.release_blob reuses caller's conn and never borrows a second pooled connection."""
        from src.server.database import pool as pool_mod
        from src.server.database import workspace_file_blobs as blobs_mod

        seen_sql: list[tuple[str, Any]] = []

        class _Cur:
            rowcount = 1

            async def __aenter__(self) -> "_Cur":
                return self

            async def __aexit__(self, *_a: Any) -> bool:
                return False

            async def execute(self, sql: str, params: Any = None) -> None:
                seen_sql.append((sql, params))

        class _Conn:
            def cursor(self, **_kw: Any) -> _Cur:
                return _Cur()

        pool_boom = create_autospec(
            pool_mod.get_or_create_pool,
            side_effect=AssertionError("Must NOT borrow a second pooled connection!"),
        )
        monkeypatch.setattr(pool_mod, "get_or_create_pool", pool_boom)
        now = datetime.now(UTC)
        ok = await blobs_mod.release_blob("u-blob", "ab" * 32, now, conn=_Conn())
        assert ok is True
        assert len(seen_sql) == 1
        assert "UPDATE workspace_file_blobs" in seen_sql[0][0]
        pool_boom.assert_not_called()

    @pytest.mark.asyncio
    async def test_r7_item3_expired_confirmations_uses_single_cutoff_and_locked_hashes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex High #3: delete_expired_oauth_states_and_confirmations uses ONE cutoff timestamp for pre-scan & DELETE and deletes ONLY pre-scanned confirmation_hash values under per-(project, sub) revoke_lock_key."""
        cutoff_ts = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
        executed: list[tuple[str, Any]] = []

        class _ExpCursor:
            rowcount = 1

            async def __aenter__(self) -> "_ExpCursor":
                return self

            async def __aexit__(self, *_a: Any) -> bool:
                return False

            async def execute(self, sql: str, params: Any = None) -> None:
                executed.append((sql, params))

            async def fetchone(self) -> Any:
                return {"cutoff": cutoff_ts}

            async def fetchall(self) -> list[Any]:
                last_sql = executed[-1][0]
                if "SELECT confirmation_hash" in last_sql:
                    return [
                        {
                            "confirmation_hash": "hash-mail-exp",
                            "provider": "google",
                            "oauth_client_id": "111222333-alpha.apps.googleusercontent.com",
                            "provider_account_id": "sub-member1",
                        }
                    ]
                if "DELETE FROM dalton_user_integration_pending_confirmations" in last_sql:
                    return [
                        {
                            "user_id": "u-m1",
                            "provider": "google",
                            "service": "mail",
                            "oauth_client_id": "111222333-alpha.apps.googleusercontent.com",
                            "provider_account_id": "sub-member1",
                            "access_token": b"enc-at",
                            "refresh_token": b"enc-rt",
                            "preferences": {},
                        }
                    ]
                return []

        class _ExpConn:
            def cursor(self, **_kw: Any) -> _ExpCursor:
                return _ExpCursor()

            @asynccontextmanager
            async def transaction(self) -> Any:
                yield

        @asynccontextmanager
        async def _get_conn(_c: Any = None) -> Any:
            yield _ExpConn()

        monkeypatch.setattr(db, "get_db_connection", _get_conn)
        monkeypatch.setattr(
            db,
            "has_active_or_pending_google_grant",
            create_autospec(db.has_active_or_pending_google_grant, return_value=False),
        )
        insert_spy = create_autospec(db.insert_purge_ledger, return_value=None)
        monkeypatch.setattr(db, "insert_purge_ledger", insert_spy)

        states_del, confirms_del = await db.delete_expired_oauth_states_and_confirmations()
        assert states_del == 1
        assert confirms_del == 1

        prescan_calls = [
            (sql, params) for sql, params in executed if "SELECT confirmation_hash" in sql
        ]
        lock_calls = [
            (sql, params) for sql, params in executed if "pg_advisory_xact_lock" in sql
        ]
        del_calls = [
            (sql, params)
            for sql, params in executed
            if "DELETE FROM dalton_user_integration_pending_confirmations" in sql
        ]
        assert len(prescan_calls) == 1
        assert prescan_calls[0][1] == (cutoff_ts,)
        assert len(lock_calls) == 1
        assert lock_calls[0][1] == (db.revoke_lock_key("111222333", "sub-member1"),)
        assert len(del_calls) == 1
        assert "confirmation_hash = ANY(%s::text[])" in del_calls[0][0]
        assert del_calls[0][1] == (["hash-mail-exp"], cutoff_ts)
        insert_spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_r7_item4_core_direct_callback_consumes_intent_before_draining_running_purge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Grok Med #1: core direct commit verifies & consumes OAuth intent + exchange lease FIRST before entering 2A purge drain."""
        _install_saved_credentials()
        fake_ctx, _ = _make_fake_lock_pool()
        monkeypatch.setattr(oauth_mod, "get_db_connection", fake_ctx)
        monkeypatch.setattr(db, "peek_oauth_state_voided", create_autospec(db.peek_oauth_state_voided, return_value=False))
        monkeypatch.setattr(db, "get_connection", create_autospec(db.get_connection, return_value=None))
        monkeypatch.setattr(db, "has_inflight_google_revoke_for_project_sub", create_autospec(db.has_inflight_google_revoke_for_project_sub, return_value=False))
        monkeypatch.setattr(db, "has_other_user_google_account_claim", create_autospec(db.has_other_user_google_account_claim, return_value=False))
        monkeypatch.setattr(db, "supersede_active_revokes_for_google_project_sub_tx", create_autospec(db.supersede_active_revokes_for_google_project_sub_tx, return_value=0))
        monkeypatch.setattr(db, "list_connections_by_account", create_autospec(db.list_connections_by_account, return_value=[]))

        # (a) Lease already expired -> rejects BEFORE touching running purge!
        wait_spy = create_autospec(oauth_mod._wait_for_running_service_purge, return_value=None)
        consume_intent_spy = create_autospec(
            db.consume_active_oauth_intent_tx, return_value=("lease_expired", None)
        )
        monkeypatch.setattr(oauth_mod, "_wait_for_running_service_purge", wait_spy)
        monkeypatch.setattr(db, "consume_active_oauth_intent_tx", consume_intent_spy)
        with pytest.raises(oauth_mod.OAuthExchangeLeaseExpiredError):
            await oauth_mod._finalize_connection(
                user_id=LOCAL_DEV_USER_ID,
                provider="google",
                service="calendar",
                oauth_client_id="111222333-abc.apps.googleusercontent.com",
                provider_account_id="sub-direct",
                provider_union_id=None,
                provider_email="d@example.com",
                granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
                access_token="at-1",
                refresh_token="rt-1",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                preferences={},
                allow_account_switch=False,
                consume_state_hash="state-hash-expired",
            )
        wait_spy.assert_not_awaited()

        # (b) Valid intent + running purge -> consumes intent FIRST, stages internal guard, drains purge, then commits via guard!
        events: list[str] = []

        async def _consume_intent(*_a: Any, **_kw: Any) -> tuple[str, None]:
            events.append("consume_intent")
            return ("ok", None)

        async def _insert_guard(**kw: Any) -> None:
            assert kw["preferences"] == {"_internal_commit_guard": True}
            events.append("stage_internal_guard")

        async def _wait_purge(*_a: Any, **_kw: Any) -> None:
            events.append("drain_running_purge")

        async def _consume_guard(*_a: Any, **_kw: Any) -> Any:
            events.append("consume_internal_guard")
            return object()

        saved_row = _make_integration_row(provider_account_id="sub-direct")

        async def _upsert(**_kw: Any) -> IntegrationRow:
            events.append("upsert_connection")
            return saved_row

        consume_intent_spy.side_effect = _consume_intent
        wait_spy.side_effect = _wait_purge
        monkeypatch.setattr(db, "has_running_local_purge_for_service", create_autospec(db.has_running_local_purge_for_service, return_value=[str(uuid.uuid4())]))
        monkeypatch.setattr(db, "insert_pending_confirmation", create_autospec(db.insert_pending_confirmation, side_effect=_insert_guard))
        monkeypatch.setattr(db, "consume_pending_confirmation", create_autospec(db.consume_pending_confirmation, side_effect=_consume_guard))
        monkeypatch.setattr(db, "upsert_connection", create_autospec(db.upsert_connection, side_effect=_upsert))

        row, pid = await oauth_mod._finalize_connection(
            user_id=LOCAL_DEV_USER_ID,
            provider="google",
            service="calendar",
            oauth_client_id="111222333-abc.apps.googleusercontent.com",
            provider_account_id="sub-direct",
            provider_union_id=None,
            provider_email="d@example.com",
            granted_scopes=" ".join(GOOGLE_CALENDAR_MIN_SCOPES),
            access_token="at-1",
            refresh_token="rt-1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            preferences={},
            allow_account_switch=False,
            consume_state_hash="state-hash-valid",
        )
        assert row is saved_row
        assert pid is None
        assert events == [
            "consume_intent",
            "stage_internal_guard",
            "drain_running_purge",
            "consume_internal_guard",
            "upsert_connection",
        ]

    @pytest.mark.asyncio
    async def test_r7_item5_purge_redis_calls_run_strictly_outside_ledger_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Grok Med #2 / Codex Med #1: finish_disconnect_locked releases try_purge_ledger_lock BEFORE 2A Redis cancel nudges & stream key deletion, and re-verifies local_purge_attempt_gen before final commit."""
        from types import SimpleNamespace

        from src.dalton.integrations.lock_guard import (
            advisory_lock_http_guard,
            held_advisory_locks,
            track_advisory_lock,
        )
        from src.dalton.services import integration_data_purge as purge_mod
        from src.dalton.services import user_integrations_lifecycle as lc_mod

        pid = str(uuid.uuid4())
        tid = str(uuid.uuid4())
        rid = str(uuid.uuid4())
        running_ledger = PurgeLedgerRow(
            purge_id=pid,
            user_id=LOCAL_DEV_USER_ID,
            provider="google",
            service="calendar",
            oauth_client_id="111222333-abc.apps.googleusercontent.com",
            provider_account_id="sub-r7-redis",
            auth_epoch_upto=1,
            trigger_reason=TriggerReason.USER_DISCONNECT,
            local_purge_status=LocalPurgeStatus.RUNNING,
            purged_max_turn_by_thread={tid: {"min_turn": 0, "max_turn": 0}},
            tracker_reconcile_needed=False,
            local_completed_at=None,
            remote_revoke_status=RemoteRevokeStatus.NOT_APPLICABLE,
            has_revoke_token=False,
            retry_count=0,
            next_retry_at=None,
            last_remote_error=None,
            remote_completed_at=datetime.now(UTC),
            disconnected_at=datetime.now(UTC),
            completed_at=None,
        )
        done_ledger = PurgeLedgerRow(
            purge_id=pid,
            user_id=LOCAL_DEV_USER_ID,
            provider="google",
            service="calendar",
            oauth_client_id="111222333-abc.apps.googleusercontent.com",
            provider_account_id="sub-r7-redis",
            auth_epoch_upto=1,
            trigger_reason=TriggerReason.USER_DISCONNECT,
            local_purge_status=LocalPurgeStatus.COMPLETED,
            purged_max_turn_by_thread={tid: {"min_turn": 0, "max_turn": 0}},
            tracker_reconcile_needed=False,
            local_completed_at=datetime.now(UTC),
            remote_revoke_status=RemoteRevokeStatus.NOT_APPLICABLE,
            has_revoke_token=False,
            retry_count=0,
            next_retry_at=None,
            last_remote_error=None,
            remote_completed_at=datetime.now(UTC),
            disconnected_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        events: list[str] = []

        @asynccontextmanager
        async def _fake_purge_lock(_pid: str) -> Any:
            events.append("enter_purge_lock")
            with track_advisory_lock("purge_ledger", 777):
                yield True
            events.append("exit_purge_lock")

        @asynccontextmanager
        async def _fake_thread_lock(_tid: str, **_kw: Any) -> Any:
            events.append("enter_thread_lock")
            yield
            events.append("exit_thread_lock")

        async def _fake_cancel_drain(*_a: Any, **_kw: Any) -> None:
            assert held_advisory_locks() == ()
            events.append("cancel_and_drain_unlocked")

        async def _fake_thread_layers(**kw: Any) -> None:
            kw["redis_keys_to_delete"].append(f"stream:{tid}:{rid}")
            events.append("thread_layers")

        async def _fake_del_redis(keys: list[str]) -> int:
            assert held_advisory_locks() == ()
            assert keys == [f"stream:{tid}:{rid}"]
            events.append("delete_redis_unlocked")
            return len(keys)

        class _CommitCursor:
            async def __aenter__(self) -> "_CommitCursor":
                return self

            async def __aexit__(self, *_a: Any) -> bool:
                return False

            async def execute(self, sql: str, params: Any = None) -> None:
                self._last = sql

            async def fetchone(self) -> Any:
                return {"local_purge_status": "running", "local_purge_attempt_gen": 1}

            async def fetchall(self) -> list[Any]:
                return []

        class _CommitConn:
            def cursor(self, **_kw: Any) -> _CommitCursor:
                return _CommitCursor()

            @asynccontextmanager
            async def transaction(self) -> Any:
                yield

        @asynccontextmanager
        async def _fake_db_conn(_c: Any = None) -> Any:
            yield _CommitConn()

        monkeypatch.setattr(purge_mod, "try_purge_ledger_lock", _fake_purge_lock)
        monkeypatch.setattr(purge_mod, "acquire_purge_thread_lock", _fake_thread_lock)
        monkeypatch.setattr(purge_mod, "get_db_connection", _fake_db_conn)
        monkeypatch.setattr(purge_mod, "_cancel_and_drain_thread", create_autospec(purge_mod._cancel_and_drain_thread, side_effect=_fake_cancel_drain))
        monkeypatch.setattr(purge_mod, "_purge_single_thread_layers", create_autospec(purge_mod._purge_single_thread_layers, side_effect=_fake_thread_layers))
        monkeypatch.setattr(purge_mod, "_delete_redis_keys", create_autospec(purge_mod._delete_redis_keys, side_effect=_fake_del_redis))
        monkeypatch.setattr(purge_mod, "get_checkpointer", create_autospec(purge_mod.get_checkpointer, return_value=SimpleNamespace(conn=None, serde=object())))
        monkeypatch.setattr(lc_mod, "_drain_refresh_locks", create_autospec(lc_mod._drain_refresh_locks, return_value=None))
        monkeypatch.setattr(db, "delete_connection_upto_epoch", create_autospec(db.delete_connection_upto_epoch, return_value=1))
        monkeypatch.setattr(db, "claim_local_purge_attempt", create_autospec(db.claim_local_purge_attempt, return_value=1))
        monkeypatch.setattr(db, "renew_local_purge_attempt", create_autospec(db.renew_local_purge_attempt, return_value=True))
        monkeypatch.setattr(db, "release_local_purge_attempt", create_autospec(db.release_local_purge_attempt, return_value=None))
        monkeypatch.setattr(db, "delete_approvals_for_disconnect", create_autospec(db.delete_approvals_for_disconnect, return_value=0))
        monkeypatch.setattr(
            db,
            "get_purge_ledger",
            create_autospec(db.get_purge_ledger, side_effect=[running_ledger, running_ledger, done_ledger]),
        )

        with advisory_lock_http_guard(True):
            res = await lc_mod.finish_disconnect_locked(pid, revoker=None)
        assert res is not None
        assert res.local_purge_status == LocalPurgeStatus.COMPLETED
        assert events == [
            "enter_purge_lock",
            "exit_purge_lock",
            "cancel_and_drain_unlocked",
            "enter_thread_lock",
            "thread_layers",
            "exit_thread_lock",
            "delete_redis_unlocked",
        ]
