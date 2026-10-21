"""Unit tests for Dalton Supabase authentication + email allowlist in OSS mode."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from unittest.mock import create_autospec

import httpx
import jwt
import pytest
from fastapi import HTTPException, Request, WebSocket, WebSocketException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.datastructures import Headers, QueryParams

from src.config import settings as app_settings
from src.dalton import auth as dalton_auth
from src.dalton.auth import (
    _fetch_supabase_auth_settings,
    is_dalton_service_request,
    log_dalton_auth_startup_if_enabled,
    verify_dalton_optional_bearer,
    verify_dalton_token,
)
from src.dalton.settings import (
    get_dalton_allowed_emails,
    get_dalton_auth_mode,
    get_supabase_publishable_key,
    is_dalton_supabase_auth_enabled,
)
from src.server.auth import jwt_bearer
from src.server.auth.jwt_bearer import (
    LOCAL_DEV_USER_ID,
    AuthInfo,
    _decode_token,
    get_current_auth_info,
    verify_jwt_token,
    warm_jwks,
)
from src.server.auth.ws_auth import authenticate_websocket
from src.server.utils import api as api_utils
from src.server.utils.api import (
    get_current_user_id,
    get_optional_user_id,
    get_stamp_auth,
    get_viewer,
)


def _make_jwt(
    sub: str,
    email: str | None = None,
    *,
    is_anonymous: object | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, object] = {"sub": sub, "aud": "authenticated"}
    if email is not None:
        payload["email"] = email
    if is_anonymous is not None:
        payload["is_anonymous"] = is_anonymous
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(
        payload, "unit-test-secret-key-at-least-32-bytes!", algorithm="HS256"
    )


def _make_request(headers: dict[str, str] | None = None) -> Request:
    req = create_autospec(Request, instance=True)
    req.__len__.return_value = 1
    req.headers = Headers(headers or {})
    return req


def _make_websocket(
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> WebSocket:
    ws = create_autospec(WebSocket, instance=True)
    ws.headers = Headers(headers or {})
    ws.query_params = QueryParams(query or {})
    return ws


class TestDaltonAuthSettings:
    """Settings parsing for DALTON_AUTH_MODE, DALTON_ALLOWED_EMAILS, and switch state."""

    def test_default_is_disabled_in_oss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
        monkeypatch.delenv("DALTON_AUTH_MODE", raising=False)
        monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")
        assert get_dalton_auth_mode() == "oss"
        assert is_dalton_supabase_auth_enabled() is False

    def test_enabled_requires_oss_and_supabase_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
        monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")
        assert is_dalton_supabase_auth_enabled() is True

        # Missing SUPABASE_URL in supabase mode stays enabled so auth dependencies fail-closed (503)
        monkeypatch.setattr(app_settings, "SUPABASE_URL", "")
        assert is_dalton_supabase_auth_enabled() is True

        # HOST_MODE != oss -> disabled
        monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setattr(app_settings, "HOST_MODE", "platform")
        assert is_dalton_supabase_auth_enabled() is False

    def test_allowed_emails_normalized_to_lowercase_frozenset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "DALTON_ALLOWED_EMAILS", " Alice@Example.com , BOB@example.com ,, "
        )
        assert get_dalton_allowed_emails() == frozenset(
            {"alice@example.com", "bob@example.com"}
        )

    def test_supabase_publishable_key_read_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "  sb_publishable_test123  ")
        assert get_supabase_publishable_key() == "sb_publishable_test123"


class TestSwitchOffPreservesOssBehavior:
    """When Dalton auth is off, all entrypoints and /auth/sync preserve OSS behavior."""

    @pytest.mark.asyncio
    async def test_entrypoints_return_local_dev_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
        monkeypatch.setattr(jwt_bearer, "HOST_MODE", "oss")
        monkeypatch.setattr(api_utils, "HOST_MODE", "oss")

        req = _make_request()
        ws = _make_websocket()

        assert await verify_jwt_token(credentials=None) == LOCAL_DEV_USER_ID
        auth_info = await get_current_auth_info(credentials=None)
        assert auth_info.user_id == LOCAL_DEV_USER_ID
        assert await get_current_user_id(request=req, credentials=None) == LOCAL_DEV_USER_ID
        assert await get_optional_user_id(request=req, credentials=None) == LOCAL_DEV_USER_ID
        assert await authenticate_websocket(ws) == LOCAL_DEV_USER_ID

    @pytest.mark.asyncio
    async def test_auth_sync_preserves_legacy_email_migration_when_switch_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime, timezone
        from src.server.app import users as users_app

        monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")

        now = datetime.now(timezone.utc)
        user_row = {
            "user_id": "new-uuid-1",
            "email": "legacy@example.com",
            "name": "Legacy User",
            "avatar_url": None,
            "timezone": "UTC",
            "locale": None,
            "onboarding_completed": True,
            "personalization_completed": False,
            "has_api_key": False,
            "has_oauth_token": False,
            "created_at": now,
            "updated_at": now,
        }

        mock_get_user = create_autospec(users_app.db_get_user, return_value=None)
        mock_find_by_email = create_autospec(
            users_app.find_user_by_email,
            return_value={"user_id": "legacy-user-id", "email": "legacy@example.com"},
        )
        mock_migrate = create_autospec(
            users_app.migrate_user_id,
            return_value=user_row,
        )
        mock_get_with_prefs = create_autospec(
            users_app.get_user_with_preferences,
            return_value={"user": user_row, "preferences": None},
        )
        monkeypatch.setattr(users_app, "db_get_user", mock_get_user)
        monkeypatch.setattr(users_app, "find_user_by_email", mock_find_by_email)
        monkeypatch.setattr(users_app, "migrate_user_id", mock_migrate)
        monkeypatch.setattr(users_app, "get_user_with_preferences", mock_get_with_prefs)

        resp = await users_app.sync_user(
            body=users_app.AuthSyncRequest(email="legacy@example.com", name="Legacy User"),
            auth_info=AuthInfo(user_id="new-uuid-1"),
        )
        assert resp.user.user_id == "new-uuid-1"
        mock_find_by_email.assert_awaited_once_with("legacy@example.com")
        mock_migrate.assert_awaited_once_with("legacy-user-id", "new-uuid-1")


class TestSwitchOnDaltonSupabaseAuth:
    """When Dalton auth is enabled, verify JWT + allowlist + security invariants across all entrypoints."""

    @pytest.fixture(autouse=True)
    def _enable_dalton_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
        monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_unit_test_key")
        monkeypatch.setenv(
            "INTERNAL_SERVICE_TOKEN", "svc-unit-test-token-at-least-32-chars!!"
        )
        monkeypatch.setenv(
            "DALTON_ALLOWED_EMAILS", "analyst1@dalton.org,owner@dalton.org"
        )
        monkeypatch.setenv(
            "DALTON_PM_USER_IDS", "11111111-1111-4111-8111-111111111111"
        )
        monkeypatch.setattr(jwt_bearer, "HOST_MODE", "oss")
        monkeypatch.setattr(api_utils, "HOST_MODE", "oss")
        monkeypatch.setattr(dalton_auth, "_auth_settings_cache", None)

        fetch_mock = create_autospec(
            _fetch_supabase_auth_settings,
            return_value={
                "disable_signup": True,
                "mailer_autoconfirm": False,
                "external": {"email": True, "anonymous_users": False},
            },
        )
        monkeypatch.setattr(dalton_auth, "_fetch_supabase_auth_settings", fetch_mock)

    @pytest.mark.asyncio
    async def test_missing_token_returns_401_and_optional_skips_remote_check(
        self,
    ) -> None:
        req = _make_request()
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user_id(request=req, credentials=None)
        assert exc_info.value.status_code == 401

        with pytest.raises(HTTPException) as exc_info2:
            await verify_jwt_token(credentials=None)
        assert exc_info2.value.status_code == 401

        with pytest.raises(HTTPException) as exc_info3:
            await get_current_auth_info(credentials=None)
        assert exc_info3.value.status_code == 401

        # Optional user id returns None when credentials are omitted without invoking remote readiness check
        dalton_auth._fetch_supabase_auth_settings.reset_mock()  # type: ignore[attr-defined]
        dalton_auth._auth_settings_cache = None
        assert await get_optional_user_id(request=req, credentials=None) is None
        assert await verify_dalton_optional_bearer(None) is None
        dalton_auth._fetch_supabase_auth_settings.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        decode_mock = create_autospec(_decode_token)
        decode_mock.side_effect = HTTPException(
            status_code=401, detail="Invalid or expired token"
        )
        monkeypatch.setattr(jwt_bearer, "_decode_token", decode_mock)

        req = _make_request()
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad-jwt")

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user_id(request=req, credentials=creds)
        assert exc_info.value.status_code == 401

        with pytest.raises(HTTPException) as exc_info2:
            await get_optional_user_id(request=req, credentials=creds)
        assert exc_info2.value.status_code == 401

    @pytest.mark.asyncio
    async def test_email_not_in_allowlist_returns_403_and_logs_only_email_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        raw_token = _make_jwt(sub="user-secret-sub-999", email="Intruder@Evil.com")
        decode_mock = create_autospec(_decode_token)
        decode_mock.return_value = AuthInfo(user_id="user-secret-sub-999")
        monkeypatch.setattr(jwt_bearer, "_decode_token", decode_mock)

        req = _make_request()
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw_token)

        with caplog.at_level(logging.WARNING, logger="src.dalton.auth"):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user_id(request=req, credentials=creds)

        assert exc_info.value.status_code == 403
        warnings = [r for r in caplog.records if r.name == "src.dalton.auth"]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "intruder@evil.com" in msg
        assert raw_token not in msg
        assert "user-secret-sub-999" not in msg

    @pytest.mark.asyncio
    async def test_email_in_allowlist_returns_sub_and_attaches_verified_email(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw_token = _make_jwt(sub="user-uuid-42", email="Analyst1@Dalton.ORG")
        decode_mock = create_autospec(_decode_token)
        decode_mock.return_value = AuthInfo(user_id="user-uuid-42")
        monkeypatch.setattr(jwt_bearer, "_decode_token", decode_mock)

        req = _make_request()
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw_token)

        assert await get_current_user_id(request=req, credentials=creds) == "user-uuid-42"
        assert await get_optional_user_id(request=req, credentials=creds) == "user-uuid-42"
        assert await verify_jwt_token(credentials=creds) == "user-uuid-42"
        info = await get_current_auth_info(credentials=creds)
        assert info.user_id == "user-uuid-42"
        assert getattr(info, "email", None) == "analyst1@dalton.org"

        ws = _make_websocket(headers={"authorization": f"Bearer {raw_token}"})
        assert await authenticate_websocket(ws) == "user-uuid-42"

    @pytest.mark.asyncio
    async def test_rejects_anonymous_token_and_sub_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 1. is_anonymous == True rejected with 403 even if email is on allowlist
        anon_token = _make_jwt(
            sub="user-anon-1",
            email="analyst1@dalton.org",
            is_anonymous=True,
            extra_claims={"user_metadata": {"email_verified": True}},
        )
        decode_mock = create_autospec(_decode_token)
        decode_mock.return_value = AuthInfo(user_id="user-anon-1")
        monkeypatch.setattr(jwt_bearer, "_decode_token", decode_mock)

        with pytest.raises(HTTPException) as exc_anon:
            await verify_dalton_token(anon_token)
        assert exc_anon.value.status_code == 403

        # 2. Unverified claims sub != _decode_token user_id rejected with 401
        mismatch_token = _make_jwt(sub="sub-in-claims", email="analyst1@dalton.org")
        decode_mock.return_value = AuthInfo(user_id="different-sub-from-decoder")
        with pytest.raises(HTTPException) as exc_sub:
            await verify_dalton_token(mismatch_token)
        assert exc_sub.value.status_code == 401

    @pytest.mark.asyncio
    async def test_startup_settings_check_disable_signup_autoconfirm_anonymous_and_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw_token = _make_jwt(sub="user-uuid-42", email="analyst1@dalton.org")
        decode_mock = create_autospec(_decode_token)
        decode_mock.return_value = AuthInfo(user_id="user-uuid-42")
        monkeypatch.setattr(jwt_bearer, "_decode_token", decode_mock)

        mock_resp = create_autospec(httpx.Response, instance=True)
        mock_client = create_autospec(httpx.AsyncClient, instance=True)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False
        mock_client.get.return_value = mock_resp
        mock_client_cls = create_autospec(httpx.AsyncClient, return_value=mock_client)
        monkeypatch.setattr(httpx, "AsyncClient", mock_client_cls)
        monkeypatch.setattr(
            dalton_auth,
            "_fetch_supabase_auth_settings",
            dalton_auth._fetch_supabase_auth_settings.__wrapped__
            if hasattr(dalton_auth._fetch_supabase_auth_settings, "__wrapped__")
            else _fetch_supabase_auth_settings,
        )

        req = _make_request()
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw_token)

        # Case 1: disable_signup is False -> startup logs ERROR and auth path rejects (503)
        mock_resp.json.return_value = {
            "disable_signup": False,
            "mailer_autoconfirm": False,
            "external": {"email": True, "anonymous_users": False},
        }
        mock_logger = create_autospec(logging.Logger, instance=True)
        assert await log_dalton_auth_startup_if_enabled(mock_logger) is True
        mock_logger.error.assert_called_once()
        mock_logger.info.assert_not_called()
        with pytest.raises(HTTPException) as exc_open:
            await get_current_user_id(request=req, credentials=creds)
        assert exc_open.value.status_code == 503

        # Case 2: mailer_autoconfirm is True (not False) -> fail-closed (503) + startup logs ERROR
        mock_resp.json.return_value = {
            "disable_signup": True,
            "mailer_autoconfirm": True,
            "external": {"email": True, "anonymous_users": False},
        }
        mock_logger_ac = create_autospec(logging.Logger, instance=True)
        assert await log_dalton_auth_startup_if_enabled(mock_logger_ac) is True
        mock_logger_ac.error.assert_called_once()
        with pytest.raises(HTTPException) as exc_ac:
            await get_current_user_id(request=req, credentials=creds)
        assert exc_ac.value.status_code == 503

        # Case 3: external.anonymous_users is True -> fail-closed (503) + startup logs ERROR
        mock_resp.json.return_value = {
            "disable_signup": True,
            "mailer_autoconfirm": False,
            "external": {"email": True, "anonymous_users": True},
        }
        mock_logger_anon = create_autospec(logging.Logger, instance=True)
        assert await log_dalton_auth_startup_if_enabled(mock_logger_anon) is True
        mock_logger_anon.error.assert_called_once()
        with pytest.raises(HTTPException) as exc_anon_cfg:
            await get_current_user_id(request=req, credentials=creds)
        assert exc_anon_cfg.value.status_code == 503

        # Case 3b: external field missing in settings -> fail-closed (503) + startup logs ERROR
        mock_resp.json.return_value = {
            "disable_signup": True,
            "mailer_autoconfirm": False,
        }
        mock_logger_no_ext = create_autospec(logging.Logger, instance=True)
        assert await log_dalton_auth_startup_if_enabled(mock_logger_no_ext) is True
        mock_logger_no_ext.error.assert_called_once()
        with pytest.raises(HTTPException) as exc_no_ext:
            await get_current_user_id(request=req, credentials=creds)
        assert exc_no_ext.value.status_code == 503

        # Case 4: /auth/v1/settings request raises network error -> fail-closed (503) + logs ERROR
        mock_client.get.side_effect = httpx.ConnectError("connection refused")
        mock_logger2 = create_autospec(logging.Logger, instance=True)
        assert await log_dalton_auth_startup_if_enabled(mock_logger2) is True
        mock_logger2.error.assert_called_once()
        with pytest.raises(HTTPException) as exc_err:
            await get_current_user_id(request=req, credentials=creds)
        assert exc_err.value.status_code == 503

        # Case 5: All three settings valid -> startup logs INFO and auth succeeds
        mock_resp.json.return_value = {
            "disable_signup": True,
            "mailer_autoconfirm": False,
            "external": {"email": True, "anonymous_users": False},
        }
        mock_client.get.side_effect = None
        mock_client.get.return_value = mock_resp

        mock_logger3 = create_autospec(logging.Logger, instance=True)
        assert await log_dalton_auth_startup_if_enabled(mock_logger3) is True
        mock_logger3.error.assert_not_called()
        mock_logger3.info.assert_called_once()
        assert await get_current_user_id(request=req, credentials=creds) == "user-uuid-42"

    @pytest.mark.asyncio
    async def test_auth_settings_ttl_cache_and_concurrency_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw_token = _make_jwt(sub="user-uuid-42", email="analyst1@dalton.org")
        decode_mock = create_autospec(_decode_token)
        decode_mock.return_value = AuthInfo(user_id="user-uuid-42")
        monkeypatch.setattr(jwt_bearer, "_decode_token", decode_mock)

        current_time = 1000.0
        monotonic_mock = create_autospec(time.monotonic, side_effect=lambda: current_time)
        monkeypatch.setattr(dalton_auth.time, "monotonic", monotonic_mock)

        fetch_mock = dalton_auth._fetch_supabase_auth_settings

        # 1. Success TTL (300s): first call fetches, second at +299s hits cache, third at +301s re-fetches
        assert (await verify_dalton_token(raw_token)).user_id == "user-uuid-42"
        assert fetch_mock.await_count == 1  # type: ignore[attr-defined]

        current_time = 1299.0
        assert (await verify_dalton_token(raw_token)).user_id == "user-uuid-42"
        assert fetch_mock.await_count == 1  # type: ignore[attr-defined]

        current_time = 1301.0
        assert (await verify_dalton_token(raw_token)).user_id == "user-uuid-42"
        assert fetch_mock.await_count == 2  # type: ignore[attr-defined]

        # 2. Failure TTL (30s): failure cached for 30s without re-fetching, then re-fetched after 30s
        monkeypatch.setattr(dalton_auth, "_auth_settings_cache", None)
        fetch_mock.reset_mock()  # type: ignore[attr-defined]
        fetch_mock.side_effect = httpx.ConnectError("upstream down")  # type: ignore[attr-defined]

        current_time = 2000.0
        with pytest.raises(HTTPException) as exc1:
            await verify_dalton_token(raw_token)
        assert exc1.value.status_code == 503
        assert fetch_mock.await_count == 1  # type: ignore[attr-defined]

        current_time = 2029.0
        with pytest.raises(HTTPException) as exc2:
            await verify_dalton_token(raw_token)
        assert exc2.value.status_code == 503
        assert fetch_mock.await_count == 1  # type: ignore[attr-defined]

        # After 30s failure TTL expires, next request re-fetches
        fetch_mock.side_effect = None  # type: ignore[attr-defined]
        current_time = 2031.0
        assert (await verify_dalton_token(raw_token)).user_id == "user-uuid-42"
        assert fetch_mock.await_count == 2  # type: ignore[attr-defined]

        # 3. Concurrent callers share a single fetch via _auth_settings_lock
        monkeypatch.setattr(dalton_auth, "_auth_settings_cache", None)
        fetch_mock.reset_mock()  # type: ignore[attr-defined]
        results = await asyncio.gather(
            verify_dalton_token(raw_token),
            verify_dalton_token(raw_token),
            verify_dalton_token(raw_token),
        )
        assert [r.user_id for r in results] == [
            "user-uuid-42",
            "user-uuid-42",
            "user-uuid-42",
        ]
        assert fetch_mock.await_count == 1  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_misconfig_missing_supabase_url_fails_closed_503_and_logs_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(app_settings, "SUPABASE_URL", "")
        assert is_dalton_supabase_auth_enabled() is True

        mock_logger = create_autospec(logging.Logger, instance=True)
        assert await log_dalton_auth_startup_if_enabled(mock_logger) is True
        mock_logger.error.assert_called()

        req = _make_request()
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="some-jwt")
        # Optional auth without token returns None directly without readiness check
        assert await get_optional_user_id(request=req, credentials=None) is None

        for call in (
            lambda: get_current_user_id(request=req, credentials=None),
            lambda: get_optional_user_id(request=req, credentials=creds),
            lambda: verify_jwt_token(credentials=None),
            lambda: get_current_auth_info(credentials=None),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await call()
            assert exc_info.value.status_code == 503
            assert exc_info.value.detail == "认证配置错误"

    @pytest.mark.asyncio
    async def test_empty_allowlist_fails_closed_403_and_logs_startup_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DALTON_ALLOWED_EMAILS", "   ")
        raw_token = _make_jwt(sub="user-uuid-42", email="owner@dalton.org")
        decode_mock = create_autospec(_decode_token)
        decode_mock.return_value = AuthInfo(user_id="user-uuid-42")
        monkeypatch.setattr(jwt_bearer, "_decode_token", decode_mock)

        with pytest.raises(HTTPException) as exc_info:
            await verify_dalton_token(raw_token)
        assert exc_info.value.status_code == 403

        mock_logger = create_autospec(logging.Logger, instance=True)
        assert await log_dalton_auth_startup_if_enabled(mock_logger) is True
        mock_logger.error.assert_called_once()
        mock_logger.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_service_token_takes_priority_and_rejects_short_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        valid_svc = "svc-secret-token-at-least-32-chars-long!!"
        monkeypatch.setattr(api_utils, "_SERVICE_TOKEN", valid_svc)
        decode_mock = create_autospec(_decode_token)
        monkeypatch.setattr(jwt_bearer, "_decode_token", decode_mock)

        req = _make_request(
            headers={
                "X-Service-Token": valid_svc,
                "X-User-Id": "automation-target-user",
            }
        )
        assert (
            await get_current_user_id(request=req, credentials=None)
            == "automation-target-user"
        )
        assert (
            await get_optional_user_id(request=req, credentials=None)
            == "automation-target-user"
        )
        assert (
            await get_stamp_auth(request=req, credentials=None)
            == "automation-target-user"
        )
        decode_mock.assert_not_called()

        # Short service token (< 32 chars) is rejected across all service token paths
        short_svc = "short-token-under-32"
        monkeypatch.setattr(api_utils, "_SERVICE_TOKEN", short_svc)
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", short_svc)
        req_short = _make_request(
            headers={
                "X-Service-Token": short_svc,
                "X-User-Id": "automation-target-user",
            }
        )
        assert is_dalton_service_request(short_svc, short_svc) is False
        with pytest.raises(HTTPException) as exc_cur:
            await get_current_user_id(request=req_short, credentials=None)
        assert exc_cur.value.status_code == 401
        assert await get_optional_user_id(request=req_short, credentials=None) is None
        with pytest.raises(HTTPException) as exc_stamp:
            await get_stamp_auth(request=req_short, credentials=None)
        assert exc_stamp.value.status_code == 401

        mock_logger = create_autospec(logging.Logger, instance=True)
        assert await log_dalton_auth_startup_if_enabled(mock_logger) is True
        mock_logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_warm_jwks_warms_when_dalton_auth_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signing_keys_mock = create_autospec(jwt_bearer._SigningKeys, instance=True)
        monkeypatch.setattr(jwt_bearer, "_signing_keys", signing_keys_mock)

        await warm_jwks()
        signing_keys_mock.refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_websocket_rejects_missing_or_unallowed_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws_missing = _make_websocket()
        with pytest.raises(WebSocketException):
            await authenticate_websocket(ws_missing)
        ws_missing.close.assert_awaited_once()

        raw_token = _make_jwt(sub="user-uuid-99", email="stranger@example.com")
        decode_mock = create_autospec(_decode_token)
        decode_mock.return_value = AuthInfo(user_id="user-uuid-99")
        monkeypatch.setattr(jwt_bearer, "_decode_token", decode_mock)

        ws_unallowed = _make_websocket(query={"token": raw_token})
        with pytest.raises(WebSocketException):
            await authenticate_websocket(ws_unallowed)
        ws_unallowed.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_viewer_treats_dalton_403_as_anonymous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw_token = _make_jwt(sub="user-visitor-1", email="visitor@outside.com")
        decode_mock = create_autospec(_decode_token)
        decode_mock.return_value = AuthInfo(user_id="user-visitor-1")
        monkeypatch.setattr(jwt_bearer, "_decode_token", decode_mock)

        req = _make_request()
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw_token)
        viewer = await get_viewer(request=req, credentials=creds)
        assert viewer.user_id is None
        assert viewer.unconfirmed is None

    @pytest.mark.asyncio
    async def test_auth_sync_blocks_other_email_takeover_and_disables_legacy_migration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime, timezone
        from src.server.app import users as users_app

        raw_token = _make_jwt(sub="attacker-uuid", email="analyst1@dalton.org")
        decode_mock = create_autospec(_decode_token)
        decode_mock.return_value = AuthInfo(user_id="attacker-uuid", auth_provider="email")
        monkeypatch.setattr(jwt_bearer, "_decode_token", decode_mock)

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw_token)
        auth_info = await get_current_auth_info(credentials=creds)

        mock_get_user = create_autospec(users_app.db_get_user, return_value=None)
        mock_find_by_email = create_autospec(users_app.find_user_by_email)
        mock_migrate = create_autospec(users_app.migrate_user_id)
        now = datetime.now(timezone.utc)
        created_row = {
            "user_id": "attacker-uuid",
            "email": "analyst1@dalton.org",
            "name": "Analyst 1",
            "avatar_url": None,
            "timezone": "UTC",
            "locale": None,
            "onboarding_completed": False,
            "personalization_completed": False,
            "has_api_key": False,
            "has_oauth_token": False,
            "created_at": now,
            "updated_at": now,
        }
        mock_create_user = create_autospec(
            users_app.create_user_from_auth,
            return_value=created_row,
        )
        monkeypatch.setattr(users_app, "db_get_user", mock_get_user)
        monkeypatch.setattr(users_app, "find_user_by_email", mock_find_by_email)
        monkeypatch.setattr(users_app, "migrate_user_id", mock_migrate)
        monkeypatch.setattr(users_app, "create_user_from_auth", mock_create_user)

        # 1. Body submits another user's email -> 400, no migration, no DB modification
        with pytest.raises(HTTPException) as exc_info:
            await users_app.sync_user(
                body=users_app.AuthSyncRequest(email="owner@dalton.org", name="Spoof"),
                auth_info=auth_info,
            )
        assert exc_info.value.status_code == 400
        mock_find_by_email.assert_not_called()
        mock_migrate.assert_not_called()
        mock_create_user.assert_not_called()

        # 2. Body omits email (or matches token email) -> legacy migration still skipped, uses token email
        resp = await users_app.sync_user(
            body=users_app.AuthSyncRequest(email=None, name="Analyst 1"),
            auth_info=auth_info,
        )
        assert resp.user.user_id == "attacker-uuid"
        assert resp.user.email == "analyst1@dalton.org"
        mock_find_by_email.assert_not_called()
        mock_migrate.assert_not_called()
        mock_create_user.assert_awaited_once()
        assert mock_create_user.call_args.kwargs["email"] == "analyst1@dalton.org"

    @pytest.mark.asyncio
    async def test_put_users_me_ignores_body_email_when_dalton_auth_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime, timezone
        from src.server.app import users as users_app
        from src.server.models.user import UserBase, UserUpdate

        now = datetime.now(timezone.utc)
        existing_row = {
            "user_id": "user-uuid-42",
            "email": "analyst1@dalton.org",
            "name": "Old Name",
            "avatar_url": None,
            "timezone": "UTC",
            "locale": None,
            "onboarding_completed": True,
            "personalization_completed": False,
            "has_api_key": False,
            "has_oauth_token": False,
            "created_at": now,
            "updated_at": now,
        }
        updated_row = {**existing_row, "name": "New Name"}
        mock_get_user = create_autospec(users_app.db_get_user, return_value=existing_row)
        mock_update_user = create_autospec(
            users_app.db_update_user, return_value=updated_row
        )
        mock_db_create_user = create_autospec(
            users_app.db_create_user, return_value=existing_row
        )
        mock_invalidate = create_autospec(users_app.invalidate_user_profile_cache)
        mock_get_prefs = create_autospec(
            users_app.db_get_user_preferences, return_value=None
        )
        monkeypatch.setattr(users_app, "db_get_user", mock_get_user)
        monkeypatch.setattr(users_app, "db_update_user", mock_update_user)
        monkeypatch.setattr(users_app, "db_create_user", mock_db_create_user)
        monkeypatch.setattr(users_app, "invalidate_user_profile_cache", mock_invalidate)
        monkeypatch.setattr(users_app, "db_get_user_preferences", mock_get_prefs)

        # 1. Switch ON: PUT /users/me ignores email in request body (passed as None to db_update_user)
        await users_app.update_current_user(
            request=UserUpdate(email="spoof@evil.com", name="New Name"),
            user_id="user-uuid-42",
        )
        mock_update_user.assert_awaited_once()
        assert mock_update_user.call_args.kwargs["email"] is None
        assert mock_update_user.call_args.kwargs["name"] == "New Name"

        # 1b. Switch ON: POST /users ignores request body email and uses verified token email
        raw_token = _make_jwt(sub="user-uuid-42", email="Analyst1@Dalton.ORG")
        decode_mock = create_autospec(
            _decode_token, return_value=AuthInfo(user_id="user-uuid-42")
        )
        monkeypatch.setattr(jwt_bearer, "_decode_token", decode_mock)
        verified_uid = await get_current_user_id(
            request=_make_request(),
            credentials=HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=raw_token
            ),
        )
        assert verified_uid == "user-uuid-42"
        await users_app.create_user(
            request=UserBase(email="spoof@evil.com", name="Analyst 1"),
            user_id=verified_uid,
        )
        mock_db_create_user.assert_awaited_once()
        assert mock_db_create_user.call_args.kwargs["email"] == "analyst1@dalton.org"
        assert mock_db_create_user.call_args.kwargs["name"] == "Analyst 1"

        # 2. Switch OFF: email in request body is forwarded as before for both PUT /users/me and POST /users
        mock_update_user.reset_mock()
        mock_db_create_user.reset_mock()
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
        await users_app.update_current_user(
            request=UserUpdate(email="local@example.com", name="New Name"),
            user_id="user-uuid-42",
        )
        mock_update_user.assert_awaited_once()
        assert mock_update_user.call_args.kwargs["email"] == "local@example.com"

        await users_app.create_user(
            request=UserBase(email="local@example.com", name="Local User"),
            user_id="user-uuid-42",
        )
        mock_db_create_user.assert_awaited_once()
        assert mock_db_create_user.call_args.kwargs["email"] == "local@example.com"

    @pytest.mark.asyncio
    async def test_self_dispatch_and_background_dispatch_requires_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.server.app import setup as setup_mod
        from src.server.app.threads import messaging as msg_mod
        from src.server.dependencies.usage_limits import ChatAuthResult
        from src.server.models.chat import ChatRequest
        from src.server.services import turn_runtime as tr_mod
        from src.server.services.llm import thread_model as tm_mod
        from src.server.services.runs import admission as adm_mod

        mock_get_svc_token = create_autospec(
            msg_mod._get_service_token, return_value=""
        )
        monkeypatch.setattr(msg_mod, "_get_service_token", mock_get_svc_token)

        # 1. INTERNAL_SERVICE_TOKEN is empty -> never trusted in Dalton mode, background_dispatch_requires_token() is True, logs ERROR on startup
        monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)

        assert app_settings.background_dispatch_requires_token() is True

        req_no_hdr = _make_request()
        assert msg_mod._is_service_request(req_no_hdr) is False

        chat_req = ChatRequest(query_type="system", steer_job_id="job-1")
        cleaned = msg_mod._client_body(chat_req, req_no_hdr)
        assert cleaned.query_type is None
        assert cleaned.steer_job_id is None

        mock_logger = create_autospec(logging.Logger, instance=True)
        assert await log_dalton_auth_startup_if_enabled(mock_logger) is True
        mock_logger.error.assert_called_once()

        # 2. INTERNAL_SERVICE_TOKEN < 32 chars -> still requires token & rejected (including from_service in _handle_send_message)
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "short-token")
        mock_get_svc_token.return_value = "short-token"
        assert app_settings.background_dispatch_requires_token() is True
        req_short_hdr = _make_request(headers={"X-Service-Token": "short-token"})
        assert msg_mod._is_service_request(req_short_hdr) is False

        # Verify _handle_send_message reuses _is_service_request for from_service (fall_back) when Dalton auth is on
        mock_scope = create_autospec(adm_mod.RunScope, instance=True)
        monkeypatch.setattr(
            adm_mod,
            "RunScope",
            create_autospec(adm_mod.RunScope, return_value=mock_scope),
        )
        monkeypatch.setattr(
            msg_mod,
            "get_thread_auth_meta",
            create_autospec(msg_mod.get_thread_auth_meta, return_value=None),
        )
        monkeypatch.setattr(
            msg_mod,
            "_settle_against_branch",
            create_autospec(
                msg_mod._settle_against_branch,
                side_effect=lambda _tid, r, _uid: r,
            ),
        )
        monkeypatch.setattr(setup_mod, "agent_config", object(), raising=False)
        monkeypatch.setattr(
            tr_mod,
            "requested_workspace",
            create_autospec(tr_mod.requested_workspace, return_value=None),
        )
        mock_route = create_autospec(tr_mod.TurnRoute, instance=True)
        mock_route.workspace_id = "ws-home"
        mock_route.role = "chief_of_staff"
        monkeypatch.setattr(
            tr_mod,
            "turn_route",
            create_autospec(tr_mod.turn_route, return_value=mock_route),
        )
        mock_resolve_turn = create_autospec(
            tm_mod.resolve_turn_config,
            side_effect=HTTPException(status_code=422, detail="stop_after_resolve"),
        )
        monkeypatch.setattr(tm_mod, "resolve_turn_config", mock_resolve_turn)

        auth_adm = ChatAuthResult(
            user_id="user-uuid-42",
            is_byok=False,
            has_oauth=True,
            access_tier=0,
            burst_slot_id="slot-1",
        )
        with pytest.raises(HTTPException) as exc_short_send:
            await msg_mod._handle_send_message(
                ChatRequest(messages=[]),
                auth_adm,
                "thread-1",
                raw_request=req_short_hdr,
            )
        assert exc_short_send.value.status_code == 422
        assert mock_resolve_turn.call_args.kwargs["fall_back"] is False

        # 3. INTERNAL_SERVICE_TOKEN >= 32 chars -> background_dispatch_requires_token() is False, matching X-Service-Token trusted
        valid_token = "expected-svc-token-at-least-32-chars-long!!"
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", valid_token)
        mock_get_svc_token.return_value = valid_token
        assert app_settings.background_dispatch_requires_token() is False

        assert msg_mod._is_service_request(_make_request()) is False
        assert (
            msg_mod._is_service_request(
                _make_request(headers={"X-Service-Token": "wrong-token"})
            )
            is False
        )
        req_valid = _make_request(headers={"X-Service-Token": valid_token})
        assert msg_mod._is_service_request(req_valid) is True
        kept = msg_mod._client_body(chat_req, req_valid)
        assert kept.query_type == "system"

        mock_resolve_turn.reset_mock()
        with pytest.raises(HTTPException):
            await msg_mod._handle_send_message(
                ChatRequest(messages=[]),
                auth_adm,
                "thread-1",
                raw_request=req_valid,
            )
        assert mock_resolve_turn.call_args.kwargs["fall_back"] is True

        # 4. Switch OFF -> background_dispatch_requires_token() is False and short matching service token still sets fall_back=True
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
        mock_get_svc_token.return_value = "short-token"
        mock_resolve_turn.reset_mock()
        with pytest.raises(HTTPException):
            await msg_mod._handle_send_message(
                ChatRequest(messages=[]),
                auth_adm,
                "thread-1",
                raw_request=req_short_hdr,
            )
        assert mock_resolve_turn.call_args.kwargs["fall_back"] is True

        monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
        assert app_settings.background_dispatch_requires_token() is False

    @pytest.mark.asyncio
    async def test_startup_logs_error_when_pm_user_ids_invalid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for bad_pm in (
            "",
            "local-dev-user",
            "11111111-1111-4111-8111-111111111111,not-a-uuid",
        ):
            monkeypatch.setenv("DALTON_PM_USER_IDS", bad_pm)
            mock_logger = create_autospec(logging.Logger, instance=True)
            assert await log_dalton_auth_startup_if_enabled(mock_logger) is True
            mock_logger.error.assert_called_once()
            mock_logger.info.assert_not_called()


class TestNoOwnerFallbackWhenUserMissing:
    """Missing or empty user_id raises ValueError('user_id is required') (or explicit skip in decide)."""

    @pytest.mark.asyncio
    async def test_request_and_agent_paths_reject_missing_user_id(self) -> None:
        from src.dalton.autonomy.preflight import decide, preflight_check
        from src.dalton.autonomy.report import generate_weekly_autonomy_report
        from src.dalton.autonomy.schedule import schedule_dalton_automation_helper
        from src.dalton.budget.service import get_budget_today
        from src.dalton.llm.assist import assist_json
        from src.dalton.llm.complete import dalton_complete
        from src.dalton.llm.role_policy import (
            available_model_keys,
            explain_model_availability,
        )
        from src.dalton.publish.audit import aaudit_draft, judge_figures_batch_with_llm
        from src.dalton.publish.numbers import label_figures_with_status
        from src.dalton.publish.review import ReviewBundle, run_review
        from src.dalton.publish.service import run_check_draft
        from src.dalton.services.provider_status import (
            get_available_models_report,
            get_providers_status,
            probe_provider,
        )
        from src.dalton.tools.director_tools import build_director_tools
        from src.dalton.tools.publish_tools import create_publish_tools

        with pytest.raises(ValueError, match="user_id is required"):
            await assist_json("task", "inst", {}, {}, user_id="")

        with pytest.raises(ValueError, match="user_id is required"):
            await dalton_complete(user_id="")

        with pytest.raises(ValueError, match="user_id is required"):
            await explain_model_availability(user_id="")

        with pytest.raises(ValueError, match="user_id is required"):
            await available_model_keys(user_id="")

        with pytest.raises(ValueError, match="user_id is required"):
            await run_check_draft(
                workspace_id="ws-1",
                draft_path="drafts/a.md",
                backend=None,
                user_id="",
            )

        with pytest.raises(ValueError, match="user_id is required"):
            await judge_figures_batch_with_llm([], user_id="")

        with pytest.raises(ValueError, match="user_id is required"):
            await aaudit_draft("# draft", use_llm_judge=False, user_id="")

        with pytest.raises(ValueError, match="user_id is required"):
            await label_figures_with_status("# draft", [], user_id="")

        bundle = ReviewBundle(
            title="t", kind="topic", draft_markdown="# d", audit_summary={}
        )
        with pytest.raises(ValueError, match="user_id is required"):
            await run_review(user_id=None, author_model="claude-opus-5-5-oauth", bundle=bundle)

        with pytest.raises(ValueError, match="user_id is required"):
            await schedule_dalton_automation_helper("dalton-meta-watch", {}, user_id="")

        with pytest.raises(ValueError, match="user_id is required"):
            await generate_weekly_autonomy_report(user_id=None)

        with pytest.raises(ValueError, match="user_id is required"):
            await preflight_check(
                {
                    "automation_id": "auto-1",
                    "name": "dalton-meta-watch",
                    "trigger_type": "cron",
                    "workspace_id": "ws-1",
                },
                "exec-1",
                persist=False,
            )

        # preflight.decide() returns explicit skip (with reason) when Dalton automation lacks user_id
        decide_res = await decide(
            {
                "automation_id": "auto-1",
                "name": "dalton-meta-watch",
                "trigger_type": "cron",
                "workspace_id": "ws-1",
            },
            "exec-1",
        )
        assert decide_res is not None
        assert decide_res.skip is True
        assert decide_res.decision == "skip"
        assert decide_res.reason == "missing_user_id"

        with pytest.raises(ValueError, match="user_id is required"):
            await get_budget_today(user_id="")

        with pytest.raises(ValueError, match="user_id is required"):
            await get_providers_status(user_id="")

        with pytest.raises(ValueError, match="user_id is required"):
            await probe_provider("deepseek", user_id="")

        with pytest.raises(ValueError, match="user_id is required"):
            await get_available_models_report(user_id="")

        director_tools = {t.name: t for t in build_director_tools(role="chief_of_staff")}
        empty_cfg: dict[str, object] = {"configurable": {"workspace_id": "ws-1"}}
        with pytest.raises(ValueError, match="user_id is required"):
            await director_tools["dalton_coverage_board"].ainvoke({}, config=empty_cfg)
        with pytest.raises(ValueError, match="user_id is required"):
            await director_tools["dalton_record_plan"].ainvoke(
                {"dispatches": []}, config=empty_cfg
            )
        with pytest.raises(ValueError, match="user_id is required"):
            await director_tools["dalton_agenda_note"].ainvoke(
                {"workspace_id": "ws-1", "question": "q"}, config=empty_cfg
            )
        with pytest.raises(ValueError, match="user_id is required"):
            await director_tools["dalton_schedule_automation"].ainvoke(
                {"action": "list"}, config=empty_cfg
            )

        pub_tools = {t.name: t for t in create_publish_tools(None)}
        err_msg = await pub_tools["check_draft"].coroutine(
            draft_path="drafts/a.md", config=empty_cfg
        )
        assert "ERROR:" in err_msg and "user_id" in err_msg

    @pytest.mark.asyncio
    async def test_scripts_pass_keyword_user_id_for_bootstrap_and_week_report(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.dalton import (
            autonomy_week_report,
            bootstrap_watch,
        )

        # 1. bootstrap_watch._upsert_automation_spec passes user_id= keyword arg
        mock_sched = create_autospec(
            bootstrap_watch.schedule_dalton_automation_helper,
            return_value={"status": "created"},
        )
        monkeypatch.setattr(
            bootstrap_watch, "schedule_dalton_automation_helper", mock_sched
        )
        spec = {"name": "dalton-meta-watch", "workspace_id": "ws-meta"}
        res = await bootstrap_watch._upsert_automation_spec("user-uuid-1", spec)
        assert res == {"status": "created"}
        mock_sched.assert_awaited_once_with(
            "dalton-meta-watch",
            spec,
            user_id="user-uuid-1",
            workspace_id="ws-meta",
        )

        # 2. autonomy_week_report requires --user-id and _run passes user_id= keyword arg
        with pytest.raises(SystemExit):
            autonomy_week_report.main([])

        mock_report = create_autospec(
            autonomy_week_report.generate_weekly_autonomy_report,
            return_value={"markdown": "# Week Report"},
        )
        monkeypatch.setattr(
            autonomy_week_report, "generate_weekly_autonomy_report", mock_report
        )
        await autonomy_week_report._run(7, "user-uuid-3", None, False)
        mock_report.assert_awaited_once_with(days=7, user_id="user-uuid-3")

    def test_apply_role_models_passes_keyword_user_id_and_does_not_swallow_type_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.dalton import apply_role_models

        mock_explain = create_autospec(
            apply_role_models.explain_model_availability,
            return_value={},
        )
        monkeypatch.setattr(
            apply_role_models, "explain_model_availability", mock_explain
        )
        rc = apply_role_models.main(["--user-id", "user-uuid-2", "--dry-run"])
        assert rc in (0, 1)
        mock_explain.assert_awaited_once_with(user_id="user-uuid-2")

        mock_explain.side_effect = RuntimeError("transient availability probe error")
        rc_fallback = apply_role_models.main(["--user-id", "user-uuid-2", "--dry-run"])
        assert rc_fallback in (0, 1)

        mock_explain.side_effect = TypeError("unexpected positional argument")
        with pytest.raises(TypeError, match="unexpected positional argument"):
            apply_role_models.main(["--user-id", "user-uuid-2", "--dry-run"])


class TestProviderStatusScopedByUserId:
    """Provider status card queries filter conversation_usages, dalton_llm_journal, and dalton_provider_events by user_id while retaining system events (user_id IS NULL)."""

    @pytest.mark.asyncio
    async def test_fetch_usage_stats_and_provider_events_filter_by_user_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from contextlib import asynccontextmanager
        from psycopg import AsyncConnection, AsyncCursor
        from src.dalton.database import provider_events as pe_mod
        from src.dalton.services import provider_status as ps_mod
        from src.server.database import pool as pool_mod

        executed_queries: list[tuple[str, tuple[object, ...]]] = []

        mock_cur = create_autospec(AsyncCursor, instance=True)

        async def _exec_side_effect(query: str, params: object = None) -> None:
            executed_queries.append((query, tuple(params) if isinstance(params, (list, tuple)) else ()))

        mock_cur.execute.side_effect = _exec_side_effect
        mock_cur.fetchall.return_value = []

        @asynccontextmanager
        async def _cursor_ctx(*args: object, **kwargs: object):
            yield mock_cur

        mock_conn = create_autospec(AsyncConnection, instance=True)
        mock_conn.cursor.side_effect = _cursor_ctx

        @asynccontextmanager
        async def _conn_ctx():
            yield mock_conn

        mock_get_db_conn = create_autospec(
            pool_mod.get_db_connection, side_effect=_conn_ctx
        )
        monkeypatch.setattr(pool_mod, "get_db_connection", mock_get_db_conn)
        monkeypatch.setattr(pe_mod, "get_db_connection", mock_get_db_conn)

        await ps_mod._fetch_usage_stats_24h(
            {"deepseek-flash": "deepseek"}, user_id="user-alice"
        )
        assert len(executed_queries) == 2
        usage_sql, usage_params = executed_queries[0]
        journal_sql, journal_params = executed_queries[1]
        assert "FROM conversation_usages" in usage_sql
        assert "WHERE user_id = %s AND created_at >= %s" in usage_sql
        assert usage_params[0] == "user-alice"
        assert "FROM dalton_llm_journal" in journal_sql
        assert "WHERE user_id = %s AND state = 'done' AND created_at >= %s" in journal_sql
        assert journal_params[0] == "user-alice"

        executed_queries.clear()
        await pe_mod.list_recent_provider_events("deepseek", user_id="user-alice", limit=5)
        await pe_mod.count_provider_errors_24h("deepseek", user_id="user-alice")
        assert len(executed_queries) == 2
        ev_sql, ev_params = executed_queries[0]
        cnt_sql, cnt_params = executed_queries[1]
        assert "(user_id = %s OR user_id IS NULL)" in ev_sql
        assert "user-alice" in ev_params
        assert "(user_id = %s OR user_id IS NULL)" in cnt_sql
        assert "user-alice" in cnt_params

        # Verify get_providers_status passes user_id to _fetch_usage_stats_24h, list_recent_provider_events, and count_provider_errors_24h
        mock_fetch_usage = create_autospec(ps_mod._fetch_usage_stats_24h, return_value={})
        mock_list_ev = create_autospec(ps_mod.list_recent_provider_events, return_value=[])
        mock_count_err = create_autospec(ps_mod.count_provider_errors_24h, return_value={})
        mock_explain = create_autospec(ps_mod.explain_model_availability, return_value={})
        monkeypatch.setattr(ps_mod, "_fetch_usage_stats_24h", mock_fetch_usage)
        monkeypatch.setattr(ps_mod, "list_recent_provider_events", mock_list_ev)
        monkeypatch.setattr(ps_mod, "count_provider_errors_24h", mock_count_err)
        monkeypatch.setattr(ps_mod, "explain_model_availability", mock_explain)

        await ps_mod.get_providers_status(user_id="user-bob")
        assert mock_fetch_usage.call_args.kwargs["user_id"] == "user-bob"
        assert all(
            c.kwargs.get("user_id") == "user-bob" for c in mock_list_ev.call_args_list
        )
        assert all(
            c.kwargs.get("user_id") == "user-bob" for c in mock_count_err.call_args_list
        )
        mock_explain.assert_awaited_once_with(user_id="user-bob")


class TestAutomationWorkspaceOwnershipAndWeeklyReportScoping:
    """Tests for Round 5 Item 1 (automation nested workspace ownership + preflight ownership check) and Item 2 (weekly report per-user SQL filtering)."""

    @pytest.mark.asyncio
    async def test_schedule_helper_and_put_automation_enforce_resolved_workspace_ownership(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.app import router as dalton_router
        from src.dalton.autonomy.schedule import schedule_dalton_automation_helper
        from src.server.database import automation as auto_db
        from src.server.database import workspace as ws_db

        mock_get_workspace = create_autospec(
            ws_db.get_workspace,
            return_value={"workspace_id": "ws-bob", "user_id": "user-bob"},
        )
        mock_create_auto = create_autospec(auto_db.create_automation)
        mock_update_auto = create_autospec(auto_db.update_automation)
        mock_list_auto = create_autospec(auto_db.list_automations, return_value=([], 0))
        monkeypatch.setattr(ws_db, "get_workspace", mock_get_workspace)
        monkeypatch.setattr(auto_db, "create_automation", mock_create_auto)
        monkeypatch.setattr(auto_db, "update_automation", mock_update_auto)
        monkeypatch.setattr(auto_db, "list_automations", mock_list_auto)

        # 1. Omitting top-level workspace_id while passing another user's workspace_id in spec/entry raises 403
        with pytest.raises(HTTPException) as exc_spec:
            await schedule_dalton_automation_helper(
                "dalton-meta-watch",
                {"workspace_id": "ws-bob", "trigger_type": "cron", "instruction": "run"},
                user_id="user-alice",
                workspace_id=None,
            )
        assert exc_spec.value.status_code == 403
        mock_create_auto.assert_not_called()
        mock_update_auto.assert_not_called()

        # 2. PUT /api/v1/dalton/automations/{name} with top-level workspace_id=None and nested entry.workspace_id="ws-bob" raises 403
        with pytest.raises(HTTPException) as exc_put:
            await dalton_router.put_dalton_automation_endpoint(
                name="dalton-meta-watch",
                body=dalton_router.DaltonAutomationWriteRequest(
                    workspace_id=None,
                    entry={"workspace_id": "ws-bob", "trigger_type": "cron", "instruction": "run"},
                ),
                user_id="user-alice",
            )
        assert exc_put.value.status_code == 403
        mock_create_auto.assert_not_called()

        # 3. Missing workspace raises 404
        mock_get_workspace.return_value = None
        with pytest.raises(HTTPException) as exc_404:
            await schedule_dalton_automation_helper(
                "dalton-meta-watch",
                {"workspace_id": "ws-nonexistent", "trigger_type": "cron", "instruction": "run"},
                user_id="user-alice",
                workspace_id=None,
            )
        assert exc_404.value.status_code == 404
        mock_create_auto.assert_not_called()

    @pytest.mark.asyncio
    async def test_preflight_blocks_foreign_workspace_before_any_side_effects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.autonomy import preflight as preflight_mod
        from src.dalton.database import watch as watch_db
        from src.server.database import workspace as ws_db

        mock_get_workspace = create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "22222222-2222-2222-2222-222222222222",
                "user_id": "user-bob",
            },
        )
        mock_expire = create_autospec(watch_db.expire_old_signals)
        mock_get_ws = create_autospec(watch_db.get_watch_state)
        mock_upsert_ws = create_autospec(watch_db.upsert_watch_state)
        mock_upsert_sig = create_autospec(watch_db.upsert_signal)
        mock_insert_log = create_autospec(watch_db.insert_preflight_log)
        monkeypatch.setattr(ws_db, "get_workspace", mock_get_workspace)
        monkeypatch.setattr(watch_db, "expire_old_signals", mock_expire)
        monkeypatch.setattr(watch_db, "get_watch_state", mock_get_ws)
        monkeypatch.setattr(watch_db, "upsert_watch_state", mock_upsert_ws)
        monkeypatch.setattr(watch_db, "upsert_signal", mock_upsert_sig)
        monkeypatch.setattr(watch_db, "insert_preflight_log", mock_insert_log)

        foreign_auto = {
            "automation_id": "11111111-1111-1111-1111-111111111111",
            "name": "dalton-meta-watch",
            "trigger_type": "cron",
            "user_id": "user-alice",
            "workspace_id": "22222222-2222-2222-2222-222222222222",
        }

        res = await preflight_mod.decide(foreign_auto, "exec-foreign-1")
        assert res is not None
        assert res.should_wake is False
        assert res.decision == "skip"
        assert res.reason == "workspace_access_denied"

        mock_expire.assert_not_called()
        mock_get_ws.assert_not_called()
        mock_upsert_ws.assert_not_called()
        mock_upsert_sig.assert_not_called()
        mock_insert_log.assert_not_called()

    @pytest.mark.asyncio
    async def test_weekly_autonomy_report_filters_all_five_queries_by_user_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from contextlib import asynccontextmanager
        from psycopg import AsyncConnection, AsyncCursor
        from src.dalton.autonomy import report as report_mod

        executed_queries: list[tuple[str, tuple[object, ...]]] = []
        mock_cur = create_autospec(AsyncCursor, instance=True)

        async def _exec_side_effect(query: str, params: object = None) -> None:
            executed_queries.append(
                (query, tuple(params) if isinstance(params, (list, tuple)) else ())
            )

        mock_cur.execute.side_effect = _exec_side_effect
        mock_cur.fetchall.return_value = []
        mock_cur.fetchone.return_value = {"cnt": 0}

        @asynccontextmanager
        async def _cursor_ctx(*args: object, **kwargs: object):
            yield mock_cur

        mock_conn = create_autospec(AsyncConnection, instance=True)
        mock_conn.cursor.side_effect = _cursor_ctx

        @asynccontextmanager
        async def _conn_ctx():
            yield mock_conn

        mock_get_db_conn = create_autospec(
            report_mod.get_db_connection, side_effect=_conn_ctx
        )
        mock_budget = create_autospec(
            report_mod.get_budget_today,
            return_value={"date": "2026-10-21", "spent_usd": 1.0},
        )
        monkeypatch.setattr(report_mod, "get_db_connection", mock_get_db_conn)
        monkeypatch.setattr(report_mod, "get_budget_today", mock_budget)

        await report_mod.generate_weekly_autonomy_report(days=7, user_id="user-alice")
        assert len(executed_queries) == 5

        q_preflight, p_preflight = executed_queries[0]
        assert "FROM dalton_preflight_log p" in q_preflight
        assert "LEFT JOIN workspaces w" in q_preflight
        assert "LEFT JOIN automations a" in q_preflight
        assert p_preflight[1:] == ("user-alice", "user-alice", "user-alice")

        q_signals, p_signals = executed_queries[1]
        assert "FROM dalton_signals s" in q_signals
        assert "LEFT JOIN workspaces w" in q_signals
        assert "LEFT JOIN automation_executions e" in q_signals
        assert "LEFT JOIN automations a" in q_signals
        assert p_signals[1:] == ("user-alice", "user-alice", "user-alice")

        q_verdicts, p_verdicts = executed_queries[2]
        assert "FROM dalton_verdicts" in q_verdicts
        assert "WHERE user_id = %s AND verdict_at >= %s" in q_verdicts
        assert p_verdicts[0] == "user-alice"

        q_pubs, p_pubs = executed_queries[3]
        assert "FROM dalton_publications" in q_pubs
        assert "WHERE user_id = %s AND created_at >= %s" in q_pubs
        assert p_pubs[0] == "user-alice"

        q_reqs, p_reqs = executed_queries[4]
        assert "FROM dalton_publish_requests" in q_reqs
        assert "WHERE user_id = %s AND status = 'awaiting_pm'" in q_reqs
        assert p_reqs == ("user-alice",)
        mock_budget.assert_awaited_once_with(user_id="user-alice")


class TestDaltonAuthRound6WriteIsolation:
    """Round 6 regression tests for systematic write-path ownership enforcement."""

    @pytest.mark.asyncio
    async def test_resolve_signals_rejects_foreign_signal_uuid_and_leaves_db_unmodified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from contextlib import asynccontextmanager
        from psycopg import AsyncConnection, AsyncCursor
        from src.dalton.database import watch as watch_db

        ws_alice = "11111111-1111-1111-1111-111111111111"
        ws_bob = "22222222-2222-2222-2222-222222222222"
        sig_bob = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

        executed_sql: list[str] = []
        mock_cur = create_autospec(AsyncCursor, instance=True)

        async def _exec_side_effect(query: str, params: object = None) -> None:
            executed_sql.append(query)

        mock_cur.execute.side_effect = _exec_side_effect
        mock_cur.fetchall.return_value = [
            {"signal_id": sig_bob, "workspace_id": ws_bob}
        ]

        @asynccontextmanager
        async def _cursor_ctx(*args: object, **kwargs: object):
            yield mock_cur

        mock_conn = create_autospec(AsyncConnection, instance=True)
        mock_conn.cursor.side_effect = _cursor_ctx

        @asynccontextmanager
        async def _conn_ctx():
            yield mock_conn

        monkeypatch.setattr(
            watch_db,
            "get_db_connection",
            create_autospec(watch_db.get_db_connection, side_effect=_conn_ctx),
        )

        # Missing/Home workspace_id rejected before any DB query
        with pytest.raises(HTTPException) as exc_no_ws:
            await watch_db.resolve_signals([sig_bob], workspace_id=None)
        assert exc_no_ws.value.status_code == 403
        assert executed_sql == []

        # Foreign signal UUID in caller's own workspace rejected with 403 and zero UPDATEs
        with pytest.raises(HTTPException) as exc_foreign:
            await watch_db.resolve_signals([sig_bob], workspace_id=ws_alice)
        assert exc_foreign.value.status_code == 403
        assert len(executed_sql) == 1
        assert "SELECT signal_id, workspace_id" in executed_sql[0]
        assert not any("UPDATE" in q for q in executed_sql)

    @pytest.mark.asyncio
    async def test_knowledge_append_verdict_rejects_foreign_signal_uuid_before_writing_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from contextlib import asynccontextmanager
        from psycopg import AsyncConnection, AsyncCursor
        from src.dalton.database import knowledge as db_knowledge
        from src.dalton.database import verdicts as db_verdicts
        from src.dalton.database import watch as watch_db
        from src.dalton.tools.knowledge_tools import knowledge_append_verdict
        from src.server.database import workspace as ws_db

        ws_alice = "11111111-1111-1111-1111-111111111111"
        ws_bob = "22222222-2222-2222-2222-222222222222"
        sig_bob = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(
                ws_db.get_workspace,
                return_value={"workspace_id": ws_alice, "user_id": "user-alice"},
            ),
        )
        mock_insert_verdict = create_autospec(db_verdicts.insert_verdict)
        mock_insert_version = create_autospec(db_knowledge.insert_version)
        monkeypatch.setattr(db_verdicts, "insert_verdict", mock_insert_verdict)
        monkeypatch.setattr(db_knowledge, "insert_version", mock_insert_version)

        mock_cur = create_autospec(AsyncCursor, instance=True)
        mock_cur.fetchall.return_value = [
            {"signal_id": sig_bob, "workspace_id": ws_bob}
        ]

        @asynccontextmanager
        async def _cursor_ctx(*args: object, **kwargs: object):
            yield mock_cur

        mock_conn = create_autospec(AsyncConnection, instance=True)
        mock_conn.cursor.side_effect = _cursor_ctx

        @asynccontextmanager
        async def _conn_ctx():
            yield mock_conn

        monkeypatch.setattr(
            watch_db,
            "get_db_connection",
            create_autospec(watch_db.get_db_connection, side_effect=_conn_ctx),
        )

        with pytest.raises(HTTPException) as exc_info:
            await knowledge_append_verdict.coroutine(
                verdict={
                    "verdict": "NO_CHANGE",
                    "event": "Check",
                    "rationale": "Test",
                    "signal_ids": [sig_bob],
                },
                config={
                    "configurable": {
                        "user_id": "user-alice",
                        "workspace_id": ws_alice,
                        "thread_id": "thread-1",
                    }
                },
                tool_call_id="tc-1",
            )
        assert exc_info.value.status_code == 403
        mock_insert_verdict.assert_not_called()
        mock_insert_version.assert_not_called()

    @pytest.mark.asyncio
    async def test_dalton_agenda_note_and_record_plan_reject_foreign_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.database import knowledge as db_knowledge
        from src.dalton.database import plans as plans_db
        from src.dalton.tools.director_tools import build_director_tools
        from src.server.database import workspace as ws_db

        ws_alice = "11111111-1111-1111-1111-111111111111"
        ws_bob = "22222222-2222-2222-2222-222222222222"

        async def _get_ws(ws_id: str) -> dict[str, str] | None:
            if ws_id == ws_alice:
                return {"workspace_id": ws_alice, "user_id": "user-alice"}
            if ws_id == ws_bob:
                return {"workspace_id": ws_bob, "user_id": "user-bob"}
            return None

        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, side_effect=_get_ws),
        )
        mock_latest = create_autospec(db_knowledge.latest_version)
        mock_insert_ver = create_autospec(db_knowledge.insert_version)
        mock_upsert_plan = create_autospec(plans_db.upsert_daily_plan)
        monkeypatch.setattr(db_knowledge, "latest_version", mock_latest)
        monkeypatch.setattr(db_knowledge, "insert_version", mock_insert_ver)
        monkeypatch.setattr(plans_db, "upsert_daily_plan", mock_upsert_plan)

        tools = {t.name: t for t in build_director_tools(role="chief_of_staff")}
        cfg = {"configurable": {"user_id": "user-alice", "workspace_id": ws_alice}}

        with pytest.raises(HTTPException) as exc_agenda:
            await tools["dalton_agenda_note"].ainvoke(
                {"workspace_id": ws_bob, "question": "Foreign agenda write"},
                config=cfg,
            )
        assert exc_agenda.value.status_code == 403
        mock_latest.assert_not_called()
        mock_insert_ver.assert_not_called()

        with pytest.raises(HTTPException) as exc_plan:
            await tools["dalton_record_plan"].ainvoke(
                {
                    "dispatches": [
                        {
                            "ticker": "META",
                            "workspace_id": ws_bob,
                            "task": "check",
                            "kind": "watch",
                            "est_usd": 1.0,
                            "why": "foreign",
                        }
                    ]
                },
                config=cfg,
            )
        assert exc_plan.value.status_code == 403
        mock_upsert_plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_knowledge_service_reconcile_and_publish_service_reject_foreign_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.database import knowledge as db_knowledge
        from src.dalton.database import publications as db_publications
        from src.dalton.database import verdicts as db_verdicts
        from src.dalton.knowledge import reconcile as reconcile_mod
        from src.dalton.knowledge import service as knowledge_svc
        from src.dalton.publish import service as publish_svc
        from src.server.database import workspace as ws_db

        ws_alice = "11111111-1111-1111-1111-111111111111"
        ws_bob = "22222222-2222-2222-2222-222222222222"

        async def _get_ws(ws_id: str) -> dict[str, str] | None:
            if ws_id == ws_alice:
                return {"workspace_id": ws_alice, "user_id": "user-alice"}
            if ws_id == ws_bob:
                return {"workspace_id": ws_bob, "user_id": "user-bob"}
            return None

        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, side_effect=_get_ws),
        )
        mock_insert_version = create_autospec(db_knowledge.insert_version)
        mock_insert_verdict = create_autospec(db_verdicts.insert_verdict)
        mock_get_or_create_req = create_autospec(db_publications.get_or_create_request)
        monkeypatch.setattr(db_knowledge, "insert_version", mock_insert_version)
        monkeypatch.setattr(db_verdicts, "insert_verdict", mock_insert_verdict)
        monkeypatch.setattr(
            db_publications, "get_or_create_request", mock_get_or_create_req
        )

        with pytest.raises(HTTPException) as exc_upd:
            await knowledge_svc.apply_update(
                user_id="user-alice",
                workspace_id=ws_bob,
                doc_key="agenda",
                ops=[],
                reason="test",
                base_version=None,
            )
        assert exc_upd.value.status_code == 403

        with pytest.raises(HTTPException) as exc_ver:
            await knowledge_svc.append_verdict(
                user_id="user-alice",
                workspace_id=ws_bob,
                verdict={"verdict": "NO_CHANGE"},
            )
        assert exc_ver.value.status_code == 403

        with pytest.raises(HTTPException) as exc_imp:
            await knowledge_svc.import_knowledge_doc(
                user_id="user-alice",
                workspace_id=ws_bob,
                doc_key="company",
                content={},
                reason="import",
            )
        assert exc_imp.value.status_code == 403

        with pytest.raises(HTTPException) as exc_det:
            await reconcile_mod.detect_direct_edits(ws_bob, "user-alice")
        assert exc_det.value.status_code == 403

        with pytest.raises(HTTPException) as exc_pend:
            await reconcile_mod.apply_pending(ws_bob, object(), user_id="user-alice")
        assert exc_pend.value.status_code == 403

        with pytest.raises(HTTPException) as exc_chk:
            await publish_svc.run_check_draft(
                workspace_id=ws_bob,
                draft_path="drafts/d.md",
                backend=object(),
                user_id="user-alice",
            )
        assert exc_chk.value.status_code == 403

        with pytest.raises(HTTPException) as exc_pub:
            await publish_svc.run_publish(
                user_id="user-alice",
                workspace_id=ws_bob,
                thread_id="t-1",
                tool_call_id="tc-1",
                draft_path="drafts/d.md",
                title="Title",
                kind="flash",
                slug="meta-q3",
                backend=object(),
            )
        assert exc_pub.value.status_code == 403

        mock_insert_version.assert_not_called()
        mock_insert_verdict.assert_not_called()
        mock_get_or_create_req.assert_not_called()

    @pytest.mark.asyncio
    async def test_put_automation_and_schedule_helper_resolve_empty_workspace_to_caller_home_and_block_global_expire(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.app import router as dalton_router
        from src.dalton.autonomy.probes import director as director_probe
        from src.dalton.database import watch as watch_db
        from src.server.database import automation as auto_db
        from src.server.database import workspace as ws_db

        ws_home_alice = "99999999-9999-9999-9999-999999999999"
        ws_bob = "22222222-2222-2222-2222-222222222222"

        mock_home = create_autospec(
            ws_db.get_or_create_home_workspace,
            return_value={"workspace_id": ws_home_alice, "user_id": "user-alice"},
        )

        async def _get_ws(ws_id: str, conn: object = None) -> dict[str, str] | None:
            if ws_id == ws_home_alice:
                return {"workspace_id": ws_home_alice, "user_id": "user-alice"}
            if ws_id == ws_bob:
                return {"workspace_id": ws_bob, "user_id": "user-bob"}
            return None

        mock_get_ws = create_autospec(ws_db.get_workspace, side_effect=_get_ws)
        mock_list_auto = create_autospec(
            auto_db.list_automations, return_value=([], 0)
        )
        mock_create_auto = create_autospec(
            auto_db.create_automation,
            return_value={"automation_id": "auto-1", "name": "dalton-director-daily"},
        )
        monkeypatch.setattr(ws_db, "get_or_create_home_workspace", mock_home)
        monkeypatch.setattr(ws_db, "get_workspace", mock_get_ws)
        monkeypatch.setattr(auto_db, "list_automations", mock_list_auto)
        monkeypatch.setattr(auto_db, "create_automation", mock_create_auto)

        # 1. PUT /dalton/automations/{name} with workspace_id=None resolves to caller's Home workspace
        res = await dalton_router.put_dalton_automation_endpoint(
            name="dalton-director-daily",
            body=dalton_router.DaltonAutomationWriteRequest(
                workspace_id=None,
                spec={"instruction": "Morning director check", "trigger_type": "cron", "cron_expression": "0 8 * * 1-5"},
            ),
            user_id="user-alice",
        )
        assert res["ok"] is True
        mock_home.assert_awaited_with("user-alice")
        assert mock_create_auto.call_args.kwargs["workspace_id"] == ws_home_alice

        # 2. PUT /dalton/automations/{name} with foreign workspace inside spec is rejected with 403
        mock_create_auto.reset_mock()
        with pytest.raises(HTTPException) as exc_foreign_spec:
            await dalton_router.put_dalton_automation_endpoint(
                name="dalton-director-daily",
                body=dalton_router.DaltonAutomationWriteRequest(
                    workspace_id=None,
                    spec={"workspace_id": ws_bob, "instruction": "Run on Bob"},
                ),
                user_id="user-alice",
            )
        assert exc_foreign_spec.value.status_code == 403
        mock_create_auto.assert_not_called()

        # 3. expire_old_signals(workspace_id=None) without system_maintenance=True is rejected
        with pytest.raises(ValueError, match="workspace_id is required"):
            await watch_db.expire_old_signals(workspace_id=None)

        # 4. User director_daily probe never triggers global system maintenance
        mock_sys_maint = create_autospec(director_probe._run_daily_system_maintenance)
        mock_check_work = create_autospec(
            director_probe._check_db_actionable_work,
            return_value={
                "due_validations": 0,
                "actionable_questions": 0,
                "stale_signals_24h": 0,
                "publish_near_or_past_ttl": 0,
                "unconfirmed_earnings_14d": 0,
            },
        )
        monkeypatch.setattr(
            director_probe, "_run_daily_system_maintenance", mock_sys_maint
        )
        monkeypatch.setattr(
            director_probe, "_check_db_actionable_work", mock_check_work
        )
        await director_probe.probe_director(
            kind="director_daily",
            watch_state={},
            persist=True,
            user_id="user-alice",
        )
        mock_sys_maint.assert_not_called()

    @pytest.mark.asyncio
    async def test_preflight_checks_ownership_before_autonomy_switch_and_timeout_and_executor_verifies_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        from src.dalton.autonomy import preflight as preflight_mod
        from src.dalton.settings import DaltonAutonomyConfig, DaltonConfig
        from src.server.database import conversation as conv_db
        from src.server.database import workspace as ws_db
        from src.server.services import automation_executor as exec_mod

        ws_bob = "22222222-2222-2222-2222-222222222222"
        foreign_auto = {
            "automation_id": "11111111-1111-1111-1111-111111111111",
            "name": "dalton-meta-watch",
            "trigger_type": "cron",
            "user_id": "user-alice",
            "workspace_id": ws_bob,
        }

        mock_get_ws = create_autospec(
            ws_db.get_workspace,
            return_value={"workspace_id": ws_bob, "user_id": "user-bob"},
        )
        monkeypatch.setattr(ws_db, "get_workspace", mock_get_ws)

        # 1. Even when autonomy.enabled is False, ownership check runs FIRST and rejects foreign workspace
        disabled_settings = DaltonConfig(
            enabled=True,
            autonomy=DaltonAutonomyConfig(enabled=False),
        )
        monkeypatch.setattr(
            preflight_mod,
            "get_dalton_settings",
            create_autospec(preflight_mod.get_dalton_settings, return_value=disabled_settings),
        )
        with pytest.raises(HTTPException) as exc_disabled:
            await preflight_mod.preflight_check(foreign_auto, "exec-disabled")
        assert exc_disabled.value.status_code == 403

        dec_disabled = await preflight_mod.decide(foreign_auto, "exec-disabled")
        assert dec_disabled is not None
        assert dec_disabled.should_wake is False
        assert dec_disabled.reason == "workspace_access_denied"

        # 2. Missing/None workspace_id is rejected before any probe or global maintenance
        no_ws_auto = {**foreign_auto, "workspace_id": None}
        with pytest.raises(HTTPException) as exc_no_ws:
            await preflight_mod.preflight_check(no_ws_auto, "exec-no-ws")
        assert exc_no_ws.value.status_code == 403

        # 3. Ownership verification timeout refuses execution (should_wake=False), never conservatively wakes
        async def _slow_get_ws(workspace_id: str, conn: object = None):
            await asyncio.sleep(0.2)
            return {"workspace_id": workspace_id, "user_id": "user-alice"}

        mock_get_ws.side_effect = _slow_get_ws
        res_timeout = await preflight_mod.preflight_check(
            foreign_auto, "exec-own-timeout", timeout_s=0.02
        )
        assert res_timeout.should_wake is False
        assert res_timeout.decision == "skip"
        assert res_timeout.reason == "workspace_access_timeout"

        # 4. AutomationExecutor._stored_workspace and _resolve_thread reject foreign workspace before thread creation
        monkeypatch.setenv("HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
        mock_get_ws.side_effect = None
        mock_get_ws.return_value = {"workspace_id": ws_bob, "user_id": "user-bob"}
        mock_create_thread = create_autospec(conv_db.create_thread)
        monkeypatch.setattr(conv_db, "create_thread", mock_create_thread)

        with pytest.raises(ValueError, match="does not belong to the automation owner"):
            await exec_mod._stored_workspace(foreign_auto)

        executor = exec_mod.AutomationExecutor()
        foreign_place = exec_mod._Place(
            workspace={"workspace_id": ws_bob, "user_id": "user-bob"},
            thread_id=None,
        )
        with pytest.raises(ValueError, match="does not belong to the automation owner"):
            await executor._resolve_thread(foreign_auto, foreign_place, ws_bob)
        mock_create_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_round6_success_paths_for_signals_agenda_executor_and_publish_request_owner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round 7 item 5: verify positive/success paths for signal UUID resolution + verdict write,
        agenda tool write, AutomationExecutor thread creation, and publish_request owner check (accept + reject)."""
        import json
        from contextlib import asynccontextmanager
        from psycopg import AsyncConnection, AsyncCursor
        from src.dalton.database import knowledge as db_knowledge
        from src.dalton.database import publications as db_publications
        from src.dalton.database import verdicts as db_verdicts
        from src.dalton.database import watch as watch_db
        from src.dalton.publish import service as publish_svc
        from src.dalton.tools.director_tools import build_director_tools
        from src.dalton.tools.knowledge_tools import knowledge_append_verdict
        from src.server.database import conversation as conv_db
        from src.server.database import workspace as ws_db
        from src.server.services import automation_executor as exec_mod

        ws_alice = "11111111-1111-1111-1111-111111111111"
        sig_alice = "cccccccc-cccc-cccc-cccc-cccccccccccc"

        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(
                ws_db.get_workspace,
                return_value={"workspace_id": ws_alice, "user_id": "user-alice"},
            ),
        )

        # 1. Valid signal UUID resolution & verdict write
        executed_sql: list[tuple[str, object]] = []
        mock_cur = create_autospec(AsyncCursor, instance=True)

        async def _exec_side_effect(query: str, params: object = None) -> None:
            executed_sql.append((" ".join(str(query).split()), params))

        mock_cur.execute.side_effect = _exec_side_effect
        mock_cur.fetchall.return_value = [
            {"signal_id": sig_alice, "workspace_id": ws_alice}
        ]
        mock_cur.rowcount = 1

        @asynccontextmanager
        async def _cursor_ctx(*args: object, **kwargs: object):
            yield mock_cur

        mock_conn = create_autospec(AsyncConnection, instance=True)
        mock_conn.cursor.side_effect = _cursor_ctx

        @asynccontextmanager
        async def _conn_ctx():
            yield mock_conn

        monkeypatch.setattr(
            watch_db,
            "get_db_connection",
            create_autospec(watch_db.get_db_connection, side_effect=_conn_ctx),
        )

        resolved_cnt = await watch_db.resolve_signals([sig_alice], workspace_id=ws_alice)
        assert resolved_cnt == 1
        assert len(executed_sql) == 2
        assert "SELECT signal_id, workspace_id FROM dalton_signals" in executed_sql[0][0]
        assert "UPDATE dalton_signals SET resolved_at = NOW()" in executed_sql[1][0]
        assert "AND workspace_id = %s" in executed_sql[1][0]

        # Valid knowledge_append_verdict with owned signal UUID
        mock_insert_verdict = create_autospec(
            db_verdicts.insert_verdict,
            return_value={"verdict_id": "VD-20261021-01", "word": "NO_CHANGE"},
        )
        mock_latest_valid = create_autospec(
            db_knowledge.latest_valid_version, return_value=None
        )
        mock_latest_ver = create_autospec(
            db_knowledge.latest_version, return_value=None
        )
        monkeypatch.setattr(db_verdicts, "insert_verdict", mock_insert_verdict)
        monkeypatch.setattr(db_knowledge, "latest_valid_version", mock_latest_valid)
        monkeypatch.setattr(db_knowledge, "latest_version", mock_latest_ver)

        verdict_res_str = await knowledge_append_verdict.coroutine(
            verdict={
                "at": "2026-10-21T12:00:00Z",
                "trigger": {"kind": "filing"},
                "verdict": "NO_CHANGE",
                "event": "Q3 10-Q check",
                "rationale": "Inline with expectations",
                "next_validation": {"what": "Next 10-Q", "due": "2027-01-31"},
                "progress": "some",
                "signal_ids": [sig_alice],
            },
            config={
                "configurable": {
                    "user_id": "user-alice",
                    "workspace_id": ws_alice,
                    "thread_id": "thread-1",
                }
            },
            tool_call_id="tc-valid-1",
        )
        assert "VD-20261021-01" in verdict_res_str
        mock_insert_verdict.assert_awaited_once()

        # 2. Valid dalton_agenda_note write on caller's workspace
        base_agenda = {
            "schema_version": 1,
            "as_of": "2026-10-21T00:00:00Z",
            "open_questions": [],
            "next_validations": [],
            "watch": {"filings": {"sources": ["sec"], "forms": ["10-Q"]}, "events": []},
            "wake_conditions": [],
        }
        mock_latest_ver.return_value = {"version_no": 1, "content": base_agenda}
        mock_latest_valid.return_value = {"version_no": 1, "content": base_agenda}
        mock_insert_version = create_autospec(
            db_knowledge.insert_version,
            return_value={
                "knowledge_version_id": "kv-2",
                "version_no": 2,
                "content": base_agenda,
                "content_sha256": "0" * 64,
                "schema_valid": True,
            },
        )
        monkeypatch.setattr(db_knowledge, "insert_version", mock_insert_version)

        tools = {t.name: t for t in build_director_tools(role="chief_of_staff")}
        agenda_out_raw = await tools["dalton_agenda_note"].ainvoke(
            {"workspace_id": ws_alice, "question": "Check AWS margin trajectory"},
            config={"configurable": {"user_id": "user-alice", "workspace_id": ws_alice}},
        )
        agenda_out = json.loads(agenda_out_raw)
        assert agenda_out["ok"] is True
        mock_insert_version.assert_awaited_once()
        assert mock_insert_version.call_args.kwargs["user_id"] == "user-alice"
        assert mock_insert_version.call_args.kwargs["workspace_id"] == ws_alice

        # 3. Valid AutomationExecutor._stored_workspace and _resolve_thread for owner
        monkeypatch.setenv("HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
        owned_auto = {
            "automation_id": "11111111-1111-1111-1111-111111111111",
            "name": "dalton-meta-watch",
            "trigger_type": "cron",
            "user_id": "user-alice",
            "workspace_id": ws_alice,
            "thread_strategy": "new",
        }
        stored_ws = await exec_mod._stored_workspace(owned_auto)
        assert stored_ws == {"workspace_id": ws_alice, "user_id": "user-alice"}

        created_tid = "33333333-3333-3333-3333-333333333333"
        mock_create_thread = create_autospec(
            conv_db.create_thread,
            return_value={"conversation_thread_id": created_tid},
        )
        monkeypatch.setattr(conv_db, "create_thread", mock_create_thread)
        executor = exec_mod.AutomationExecutor()
        owned_place = exec_mod._Place(
            workspace={"workspace_id": ws_alice, "user_id": "user-alice"},
            thread_id=None,
        )
        resolved_tid = await executor._resolve_thread(owned_auto, owned_place, ws_alice)
        mock_create_thread.assert_awaited_once()
        assert resolved_tid == mock_create_thread.call_args.kwargs["conversation_thread_id"]

        # 4. Publish request owner verification: reject foreign/ownerless request_id, accept owned request_id
        class _BackendProto:
            async def aread_text(self, path: str) -> str | None: ...

        fake_backend = create_autospec(_BackendProto, instance=True)
        fake_backend.aread_text.return_value = "# Draft\n\nBody text."

        monkeypatch.setattr(
            db_publications,
            "resolve_thread_author_model",
            create_autospec(
                db_publications.resolve_thread_author_model,
                return_value="claude-opus-5-5-oauth",
            ),
        )
        monkeypatch.setattr(
            db_knowledge,
            "list_all_latest_for_workspace",
            create_autospec(db_knowledge.list_all_latest_for_workspace, return_value={}),
        )
        mock_get_req = create_autospec(db_publications.get_request)
        mock_get_pub = create_autospec(
            db_publications.get_publication,
            return_value={"publication_id": "pub-1", "files": [{"path": "published/2026-10-21-meta.md"}]},
        )
        monkeypatch.setattr(db_publications, "get_request", mock_get_req)
        monkeypatch.setattr(db_publications, "get_publication", mock_get_pub)

        # 4a. Foreign user's publish_request_id -> 403
        mock_get_req.return_value = {
            "publish_request_id": "req-bob",
            "workspace_id": ws_alice,
            "user_id": "user-bob",
            "status": "published",
        }
        with pytest.raises(HTTPException) as exc_pub_foreign:
            await publish_svc.run_publish(
                user_id="user-alice",
                workspace_id=ws_alice,
                thread_id="t-1",
                tool_call_id="tc-1",
                draft_path="drafts/d.md",
                title="Meta Note",
                kind="event_note",
                slug="meta-q3",
                request_id="req-bob",
                backend=fake_backend,
            )
        assert exc_pub_foreign.value.status_code == 403
        mock_get_pub.assert_not_called()

        # 4b. Ownerless publish_request_id -> 403
        mock_get_req.return_value = {
            "publish_request_id": "req-no-owner",
            "workspace_id": ws_alice,
            "user_id": "",
            "status": "published",
        }
        with pytest.raises(HTTPException) as exc_pub_no_owner:
            await publish_svc.run_publish(
                user_id="user-alice",
                workspace_id=ws_alice,
                thread_id="t-1",
                tool_call_id="tc-1",
                draft_path="drafts/d.md",
                title="Meta Note",
                kind="event_note",
                slug="meta-q3",
                request_id="req-no-owner",
                backend=fake_backend,
            )
        assert exc_pub_no_owner.value.status_code == 403

        # 4c. Caller's own publish_request_id -> succeeds
        mock_get_req.return_value = {
            "publish_request_id": "req-alice",
            "workspace_id": ws_alice,
            "user_id": "user-alice",
            "status": "published",
            "publication_id": "pub-1",
        }
        pub_ok = await publish_svc.run_publish(
            user_id="user-alice",
            workspace_id=ws_alice,
            thread_id="t-1",
            tool_call_id="tc-1",
            draft_path="drafts/d.md",
            title="Meta Note",
            kind="event_note",
            slug="meta-q3",
            request_id="req-alice",
            backend=fake_backend,
        )
        assert pub_ok["ok"] is True
        assert pub_ok["status"] == "published"
        assert pub_ok["publication_id"] == "pub-1"

    @pytest.mark.asyncio
    async def test_daily_plans_per_user_isolation_concurrent_first_write_and_d008_migration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round 7 item 1: daily plans are isolated by (user_id, plan_date) in a single transaction,
        reject missing snapshot owner, support two users on the same date without overwriting, and ship d008 migration."""
        import json
        from contextlib import asynccontextmanager
        from datetime import date
        from psycopg import AsyncConnection, AsyncCursor, AsyncTransaction
        from src.dalton.database import plans as plans_db
        from src.dalton.migrations.versions import d008_daily_plans_user_id
        from src.dalton.tools import director_tools as dt_mod

        # 1. Verify d008 migration revision chain & SQL statements
        assert d008_daily_plans_user_id.revision == "d008"
        assert d008_daily_plans_user_id.down_revision == "d007"
        upgrade_sqls: list[str] = []
        downgrade_sqls: list[str] = []
        monkeypatch.setattr(
            d008_daily_plans_user_id.op,
            "execute",
            lambda sql: upgrade_sqls.append(" ".join(str(sql).split())),
        )
        d008_daily_plans_user_id.upgrade()
        assert any("ADD COLUMN IF NOT EXISTS user_id TEXT" in s for s in upgrade_sqls)
        assert any("local-dev-user" in s and "board_snapshot->>'user_id'" in s for s in upgrade_sqls)
        assert any("ALTER COLUMN user_id SET NOT NULL" in s for s in upgrade_sqls)
        assert any("PRIMARY KEY (user_id, plan_date)" in s for s in upgrade_sqls)

        monkeypatch.setattr(
            d008_daily_plans_user_id.op,
            "execute",
            lambda sql: downgrade_sqls.append(" ".join(str(sql).split())),
        )
        d008_daily_plans_user_id.downgrade()
        assert any("PRIMARY KEY (plan_date)" in s for s in downgrade_sqls)
        assert any("DROP COLUMN IF EXISTS user_id" in s for s in downgrade_sqls)

        # 2. Transactional fake DB keyed by (user_id, plan_date)
        store: dict[tuple[str, date], dict[str, Any]] = {}
        executed_queries: list[tuple[str, Any]] = []
        tx_entries: list[str] = []

        def _make_tx_conn() -> AsyncConnection:
            cur = create_autospec(AsyncCursor, instance=True)
            last_result: dict[str, Any] = {"row": None, "rows": []}

            async def _execute(query: str, params: Any = None) -> None:
                norm_q = " ".join(str(query).split())
                executed_queries.append((norm_q, params))
                if "SELECT user_id, plan_date" in norm_q and "FOR UPDATE" in norm_q:
                    uid, p_date = params[0], params[1]
                    last_result["row"] = store.get((uid, p_date))
                elif "INSERT INTO dalton_daily_plans" in norm_q:
                    assert "ON CONFLICT (user_id, plan_date) DO UPDATE" in norm_q
                    assert "WHERE dalton_daily_plans.user_id = EXCLUDED.user_id" in norm_q
                    uid, p_date = params[0], params[1]
                    budget_snap = params[2].obj
                    board_snap = params[3].obj
                    dispatches = params[4].obj
                    skipped = params[5].obj
                    pm_att = params[6].obj
                    t_uuid = params[7]
                    existing = store.get((uid, p_date))
                    if existing is not None:
                        ex_owner = str((existing.get("board_snapshot") or {}).get("user_id") or "").strip()
                        if existing.get("user_id") != uid or ex_owner != uid:
                            last_result["row"] = None
                            return
                    row = {
                        "user_id": uid,
                        "plan_date": p_date,
                        "budget_snapshot": budget_snap,
                        "board_snapshot": board_snap,
                        "dispatches": dispatches,
                        "skipped": skipped,
                        "pm_attention": pm_att,
                        "director_thread_id": t_uuid,
                        "created_at": "2026-10-21T08:00:00+00:00",
                    }
                    store[(uid, p_date)] = row
                    last_result["row"] = row
                elif "FROM dalton_daily_plans WHERE user_id = %s AND plan_date = %s" in norm_q:
                    uid, p_date = params[0], params[1]
                    last_result["row"] = store.get((uid, p_date))
                elif "FROM dalton_daily_plans WHERE user_id = %s ORDER BY plan_date DESC" in norm_q:
                    uid = params[0]
                    last_result["rows"] = [
                        r for (u, _), r in store.items() if u == uid
                    ]

            async def _fetchone() -> dict[str, Any] | None:
                return last_result["row"]

            async def _fetchall() -> list[dict[str, Any]]:
                return list(last_result["rows"])

            cur.execute.side_effect = _execute
            cur.fetchone.side_effect = _fetchone
            cur.fetchall.side_effect = _fetchall

            @asynccontextmanager
            async def _cursor_cm(*args: object, **kwargs: object):
                yield cur

            @asynccontextmanager
            async def _tx_cm():
                tx = create_autospec(AsyncTransaction, instance=True)
                tx_entries.append("enter")
                yield tx
                tx_entries.append("exit")

            conn = create_autospec(AsyncConnection, instance=True)
            conn.cursor.side_effect = _cursor_cm
            conn.transaction.side_effect = _tx_cm
            return conn

        @asynccontextmanager
        async def _get_conn():
            yield _make_tx_conn()

        monkeypatch.setattr(
            plans_db,
            "get_db_connection",
            create_autospec(plans_db.get_db_connection, side_effect=_get_conn),
        )
        monkeypatch.setattr(
            dt_mod,
            "build_coverage_board",
            create_autospec(
                dt_mod.build_coverage_board,
                side_effect=lambda user_id=None, **kw: {
                    "date": "2026-10-21",
                    "budget": {
                        "cap_usd": 20.0,
                        "spent_usd": 1.0,
                        "remaining_usd": 19.0,
                        "reserve_for_events_usd": 3.0,
                        "allocatable_usd": 16.0,
                    },
                    "companies": [{"ticker": "AAPL" if user_id == "user-alice" else "MSFT"}],
                    "candidates": [],
                    "pm_handoffs": [],
                },
            ),
        )

        tools = {t.name: t for t in dt_mod.build_director_tools(role="chief_of_staff")}

        # 3. Concurrent first-write on the same date by user-alice and user-bob preserves both plans
        res_alice_raw, res_bob_raw = await asyncio.gather(
            tools["dalton_record_plan"].ainvoke(
                {
                    "plan_date": "2026-10-21",
                    "dispatches": [{"ticker": "AAPL", "task": "Alice task", "est_usd": 2.0}],
                },
                config={"configurable": {"user_id": "user-alice"}},
            ),
            tools["dalton_record_plan"].ainvoke(
                {
                    "plan_date": "2026-10-21",
                    "dispatches": [{"ticker": "MSFT", "task": "Bob task", "est_usd": 3.0}],
                },
                config={"configurable": {"user_id": "user-bob"}},
            ),
        )
        assert json.loads(res_alice_raw)["ok"] is True
        assert json.loads(res_bob_raw)["ok"] is True
        assert tx_entries.count("enter") == 2 and tx_entries.count("exit") == 2

        alice_plan = await plans_db.get_daily_plan("2026-10-21", user_id="user-alice")
        bob_plan = await plans_db.get_daily_plan("2026-10-21", user_id="user-bob")
        assert alice_plan is not None and alice_plan["dispatches"][0]["ticker"] == "AAPL"
        assert bob_plan is not None and bob_plan["dispatches"][0]["ticker"] == "MSFT"

        alice_list = await plans_db.list_daily_plans(user_id="user-alice")
        assert len(alice_list) == 1 and alice_list[0]["user_id"] == "user-alice"

        # 4. Snapshot without owner (or existing DB row without snapshot owner) is rejected with 403
        with pytest.raises(HTTPException) as exc_no_snap_owner:
            await plans_db.upsert_daily_plan(
                user_id="user-alice",
                plan_date="2026-10-21",
                budget_snapshot={},
                board_snapshot={"companies_count": 1},  # missing user_id in snapshot
                dispatches=[],
                skipped=[],
                pm_attention=[],
            )
        assert exc_no_snap_owner.value.status_code == 403

        # Simulate legacy row in DB whose board_snapshot has no user_id -> rejected with 403, not overwritten
        store[("user-alice", date(2026, 10, 22))] = {
            "user_id": "user-alice",
            "plan_date": date(2026, 10, 22),
            "budget_snapshot": {},
            "board_snapshot": {},  # no owner in snapshot
            "dispatches": [{"ticker": "LEGACY"}],
            "skipped": [],
            "pm_attention": [],
            "director_thread_id": None,
            "created_at": "2026-10-20T00:00:00+00:00",
        }
        with pytest.raises(HTTPException) as exc_legacy_unowned:
            await plans_db.upsert_daily_plan(
                user_id="user-alice",
                plan_date="2026-10-22",
                budget_snapshot={},
                board_snapshot={"user_id": "user-alice"},
                dispatches=[{"ticker": "OVERWRITE"}],
                skipped=[],
                pm_attention=[],
            )
        assert exc_legacy_unowned.value.status_code == 403
        assert store[("user-alice", date(2026, 10, 22))]["dispatches"] == [{"ticker": "LEGACY"}]

    @pytest.mark.asyncio
    async def test_system_maintenance_periodic_entrypoint_and_tightened_watch_and_decide(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round 7 items 2, 3, 4: ProvenanceGCService runs Dalton daily system maintenance under system context;
        claim_signals_woken rejects workspace_id=None; preflight.decide skips on unexpected error; no _ownership_verified flag."""
        import inspect
        from contextlib import asynccontextmanager
        from psycopg import AsyncConnection, AsyncCursor
        from src.dalton.autonomy import preflight as preflight_mod
        from src.dalton.database import llm_journal as journal_mod
        from src.dalton.database import publications as pub_db
        from src.dalton.database import watch as watch_db
        from src.server.services.provenance_gc import ProvenanceGCService

        # 1. _preflight_check_inner has no _ownership_verified boolean skip parameter and requires _VerifiedAutomationOwnership
        sig = inspect.signature(preflight_mod._preflight_check_inner)
        assert "_ownership_verified" not in sig.parameters
        assert "ownership" in sig.parameters
        with pytest.raises(TypeError, match="_VerifiedAutomationOwnership"):
            await preflight_mod._preflight_check_inner(
                {"name": "dalton-meta-watch", "user_id": "u", "workspace_id": "w"},
                "exec-1",
                ownership=None,  # type: ignore[arg-type]
            )

        # 2. claim_signals_woken rejects workspace_id=None / "home" with 403 and succeeds with valid workspace_id
        with pytest.raises(HTTPException) as exc_claim_none:
            await watch_db.claim_signals_woken(
                ["cccccccc-cccc-cccc-cccc-cccccccccccc"],
                "exec-1",
                workspace_id=None,
            )
        assert exc_claim_none.value.status_code == 403

        ws_alice = "11111111-1111-1111-1111-111111111111"
        sig_alice = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        mock_cur = create_autospec(AsyncCursor, instance=True)
        mock_cur.fetchall.return_value = [
            {
                "signal_id": sig_alice,
                "workspace_id": ws_alice,
                "woken_execution_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            }
        ]

        @asynccontextmanager
        async def _cursor_ctx(*args: object, **kwargs: object):
            yield mock_cur

        mock_conn = create_autospec(AsyncConnection, instance=True)
        mock_conn.cursor.side_effect = _cursor_ctx

        @asynccontextmanager
        async def _conn_ctx():
            yield mock_conn

        monkeypatch.setattr(
            watch_db,
            "get_db_connection",
            create_autospec(watch_db.get_db_connection, side_effect=_conn_ctx),
        )
        claimed = await watch_db.claim_signals_woken(
            [sig_alice], "exec-1", workspace_id=ws_alice
        )
        assert len(claimed) == 1
        assert "AND workspace_id = %s" in mock_cur.execute.call_args[0][0]

        # 3. preflight.decide returns skip (should_wake=False) on unexpected RuntimeError, never None
        monkeypatch.setattr(
            preflight_mod,
            "preflight_check",
            create_autospec(
                preflight_mod.preflight_check,
                side_effect=RuntimeError("unexpected db pool crash"),
            ),
        )
        dec_err = await preflight_mod.decide(
            {
                "name": "dalton-meta-watch",
                "user_id": "user-alice",
                "workspace_id": ws_alice,
            },
            "exec-unexpected",
        )
        assert dec_err is not None
        assert dec_err.should_wake is False
        assert dec_err.skip is True
        assert dec_err.reason == "preflight_error"

        # 4. System maintenance entrypoint via ProvenanceGCService._loop -> run_daily_system_maintenance
        mock_exp_pubs = create_autospec(
            pub_db.expire_stale_publications,
            return_value=2,  # the real function returns a row count
        )
        mock_prune_journal = create_autospec(
            journal_mod.prune_expired_journal_responses, return_value=3
        )
        mock_exp_sigs = create_autospec(
            watch_db.expire_old_signals,
            return_value=[{"signal_id": sig_alice, "status": "expired"}],
        )
        monkeypatch.setattr(pub_db, "expire_stale_publications", mock_exp_pubs)
        monkeypatch.setattr(
            journal_mod, "prune_expired_journal_responses", mock_prune_journal
        )
        monkeypatch.setattr(watch_db, "expire_old_signals", mock_exp_sigs)

        swept_evt = asyncio.Event()

        async def _fake_sweep(grace_days: int = 7) -> int:
            swept_evt.set()
            return 0

        monkeypatch.setattr(
            "src.server.services.provenance_gc.sweep_orphan_bodies", _fake_sweep
        )
        svc = ProvenanceGCService(interval_seconds=3600)
        try:
            await svc.start()
            await asyncio.wait_for(swept_evt.wait(), timeout=2.0)
        finally:
            await svc.stop()

        mock_exp_pubs.assert_awaited_once()
        mock_prune_journal.assert_awaited_once_with(retention_days=30)
        mock_exp_sigs.assert_awaited_once_with(
            max_age_days=7,
            workspace_id=None,
            system_maintenance=True,
        )





@pytest.mark.asyncio
async def test_daily_system_maintenance_counts_expired_publications_from_row_count(monkeypatch) -> None:
    from src.dalton.autonomy.probes import director as director_mod
    from src.dalton.database import llm_journal as journal_mod
    from src.dalton.database import publications as pub_db
    from src.dalton.database import watch as watch_db

    monkeypatch.setattr(
        pub_db, "expire_stale_publications",
        create_autospec(pub_db.expire_stale_publications, return_value=3),
    )
    monkeypatch.setattr(
        journal_mod, "prune_expired_journal_responses",
        create_autospec(journal_mod.prune_expired_journal_responses, return_value=0),
    )
    monkeypatch.setattr(
        watch_db, "expire_old_signals",
        create_autospec(watch_db.expire_old_signals, return_value=[{"signal_id": "s1"}]),
    )
    result = await director_mod.run_daily_system_maintenance()
    assert result["expired_publications"] == 3
    assert result["expired_signals"] == 1
