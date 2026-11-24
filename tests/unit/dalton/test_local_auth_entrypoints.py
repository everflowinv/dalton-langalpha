"""Unit tests for Batch 4a auth mode validation, real app mounting, and unified backend dispatch.

Covers:
- Item 1: Real FastAPI app mounting (local mode reachable, supabase mode 404)
- Item 2: Auth mode validation (core default local, core+oss dev flag, platform+local rejected, Everflow unchanged)
- Item 3: Immediate session revocation on HTTP / JWT / SSE / WebSocket (handshake + active WS close)
- Item 6: Local transcription ticket secret enforcement & no predictable fallback
- Item 14: Unified service-token flags (api.py, messaging.py, background_dispatch_requires_token)
- Item 15: Optional auth distinguishes absent Authorization header (None) vs malformed header (401)
- Grok #13 / #14: WS query token toggle, no local-dev-user provisioning in local mode

All test doubles use ``unittest.mock.create_autospec``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import jwt
import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, QueryParams

from src.config import settings as app_settings
from src.dalton import auth as dalton_auth
from src.dalton.auth import bootstrap as bootstrap_mod
from src.dalton.auth import dispatch as dispatch_mod
from src.dalton.auth.local_router import register_local_auth_routers
from src.dalton.auth.tokens import LOCAL_JWT_ALGORITHM, issue_local_access_token
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.profile import DaltonStartupConfigError
from src.dalton.settings import (
    get_dalton_auth_mode,
    get_dalton_transcription_ticket_hmac_key,
    is_dalton_local_auth_enabled,
    validate_dalton_auth_config,
)
from src.server.app import user_events as user_events_mod
from src.server.app import users as users_mod
from src.server.app.threads import messaging as msg_mod
from src.server.app.threads import tasks as tasks_mod
from src.server.auth import jwt_bearer
from src.server.auth.jwt_bearer import (
    LOCAL_DEV_USER_ID,
    AuthInfo,
    get_current_auth_info,
    verify_jwt_token,
)
from src.server.auth.ws_auth import authenticate_websocket
from src.server.database import conversation as conv_db
from src.server.utils import api as api_utils
from src.server.utils.api import (
    get_current_user_id,
    get_optional_user_id,
    get_user_id_any_state,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_JWT_SECRET = "test-local-jwt-signing-secret-at-least-32-bytes-long!"
TEST_TICKET_SECRET = "test-transcription-ticket-secret-at-least-32-bytes!"
TEST_USER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_USER_ID = "22222222-2222-4222-8222-222222222222"
TEST_SESSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _make_request(headers: dict[str, str] | None = None) -> Request:
    req = create_autospec(Request, instance=True)
    req.__len__.return_value = 1
    req.headers = Headers(headers or {})
    req.scope = {"state": {}}
    return req


def _make_websocket(
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> WebSocket:
    ws = create_autospec(WebSocket, instance=True)
    ws.headers = Headers(headers or {})
    ws.query_params = QueryParams(query or {})
    ws.scope = {
        "type": "websocket",
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
    }
    ws.state = SimpleNamespace()
    return ws


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setattr(api_utils, "HOST_MODE", "oss")
    monkeypatch.setattr(jwt_bearer, "HOST_MODE", "oss")
    monkeypatch.delenv("DALTON_PROFILE", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DALTON_AUTH_MODE", raising=False)
    monkeypatch.delenv("DALTON_ALLOW_DEV_OSS_AUTH", raising=False)
    monkeypatch.delenv("DALTON_EDGE_TLS_BIND", raising=False)
    monkeypatch.delenv("JWT_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("DALTON_TRANSCRIPTION_ENABLED", raising=False)
    monkeypatch.delenv("DALTON_TRANSCRIPTION_TICKET_SECRET", raising=False)
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("DALTON_BRIDGE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("DALTON_WS_ALLOW_QUERY_TOKEN", raising=False)
    monkeypatch.setattr(api_utils, "_SERVICE_TOKEN", "")
    dalton_auth._auth_settings_cache = None


class TestRealFastApiAppMounting:
    """Item 1: real FastAPI ``src.server.app.setup:app`` mounts local auth ONLY in local mode."""

    def _probe_real_app_in_subprocess(self, auth_mode: str) -> dict[str, Any]:
        env = {
            **os.environ,
            "HOST_MODE": "oss",
            "DALTON_AUTH_MODE": auth_mode,
            "JWT_SIGNING_SECRET": TEST_JWT_SECRET,
            "PYTHONPATH": str(REPO_ROOT),
        }
        code = (
            "import json\n"
            "from unittest.mock import create_autospec\n"
            "from fastapi.testclient import TestClient\n"
            "import src.dalton.auth.local_router as lr\n"
            "from src.server.app.setup import app\n"
            "lr.get_bootstrap_status = create_autospec(lr.get_bootstrap_status, return_value={'needs_bootstrap': True})\n"
            "paths = sorted({getattr(r, 'path', '') for r in app.routes if '/dalton/' in getattr(r, 'path', '')})\n"
            "client = TestClient(app, raise_server_exceptions=False)\n"
            "r_status = client.get('/api/v1/dalton/bootstrap/status')\n"
            "r_login = client.post('/api/v1/dalton/auth/local/login', json={})\n"
            "print(json.dumps({'status_code': r_status.status_code, 'status_body': r_status.json(), "
            "'login_code': r_login.status_code, 'has_bootstrap': '/api/v1/dalton/bootstrap/admin' in paths, "
            "'has_login': '/api/v1/dalton/auth/local/login' in paths}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, f"Subprocess failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        # Take the last JSON line (skip any startup log lines on stdout)
        import json

        for line in reversed(proc.stdout.splitlines()):
            if line.strip().startswith("{"):
                return json.loads(line)
        raise AssertionError(f"No JSON payload in stdout:\n{proc.stdout}")

    def test_real_app_mounts_local_routes_in_local_mode_and_404s_in_supabase(self) -> None:
        local_res = self._probe_real_app_in_subprocess("local")
        assert local_res["has_bootstrap"] is True
        assert local_res["has_login"] is True
        assert local_res["status_code"] == 200
        assert local_res["status_body"] == {"needs_bootstrap": True}
        # Missing body fails Pydantic validation (422), NOT 404
        assert local_res["login_code"] == 422

        sb_res = self._probe_real_app_in_subprocess("supabase")
        assert sb_res["has_bootstrap"] is False
        assert sb_res["has_login"] is False
        assert sb_res["status_code"] == 404
        assert sb_res["login_code"] == 404


class TestAuthModeStartupValidationAndTranscriptionSecret:
    """Items 2 & 6: fail-closed startup validation across auth modes and transcription secret."""

    def test_everflow_defaults_to_oss_and_passes(self) -> None:
        assert get_dalton_auth_mode() == "oss"
        assert is_dalton_local_auth_enabled() is False
        assert validate_dalton_auth_config() == "oss"

    def test_core_profile_defaults_to_local_and_requires_jwt_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DALTON_PROFILE", "core")
        assert get_dalton_auth_mode() == "local"
        assert is_dalton_local_auth_enabled() is True

        with pytest.raises(DaltonStartupConfigError, match="JWT_SIGNING_SECRET"):
            validate_dalton_auth_config()

        monkeypatch.setenv("JWT_SIGNING_SECRET", "too-short")
        with pytest.raises(DaltonStartupConfigError, match="32 bytes"):
            validate_dalton_auth_config()

        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)
        assert validate_dalton_auth_config() == "local"

    def test_platform_host_mode_rejects_local_auth_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(app_settings, "HOST_MODE", "platform")
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)

        with pytest.raises(DaltonStartupConfigError, match="HOST_MODE=platform"):
            validate_dalton_auth_config()

    def test_unknown_auth_mode_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DALTON_AUTH_MODE", "kerberos")
        with pytest.raises(DaltonStartupConfigError, match="Invalid DALTON_AUTH_MODE"):
            validate_dalton_auth_config()

    def test_core_profile_refuses_oss_without_explicit_dev_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DALTON_PROFILE", "core")
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")

        with pytest.raises(
            DaltonStartupConfigError, match="DALTON_ALLOW_DEV_OSS_AUTH"
        ):
            validate_dalton_auth_config()

        monkeypatch.setenv("DALTON_ALLOW_DEV_OSS_AUTH", "1")
        assert validate_dalton_auth_config() == "oss"

    @pytest.mark.parametrize(
        "bind_addr",
        [
            "0.0.0.0",
            "::",
            "[::]",
            "192.168.1.50",
            "10.0.0.5",
            "fd00::1",
            "[fd00::1]",
            "dalton.example.com",
            "[localhost]",
        ],
    )
    def test_core_profile_non_loopback_tls_bind_requires_local_auth(
        self, monkeypatch: pytest.MonkeyPatch, bind_addr: str
    ) -> None:
        monkeypatch.setenv("DALTON_PROFILE", "core")
        monkeypatch.setenv("DALTON_EDGE_TLS_BIND", bind_addr)

        # oss + dev flag is still refused when binding outside loopback
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
        monkeypatch.setenv("DALTON_ALLOW_DEV_OSS_AUTH", "1")
        with pytest.raises(
            DaltonStartupConfigError, match="DALTON_EDGE_TLS_BIND.*DALTON_AUTH_MODE=local"
        ):
            validate_dalton_auth_config()

        # supabase under core profile is also refused when binding outside loopback
        monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
        with pytest.raises(
            DaltonStartupConfigError, match="DALTON_EDGE_TLS_BIND.*DALTON_AUTH_MODE=local"
        ):
            validate_dalton_auth_config()

        # local auth with valid JWT secret is allowed
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)
        assert validate_dalton_auth_config() == "local"

    @pytest.mark.parametrize(
        "loopback_addr",
        [
            "",
            "127.0.0.1",
            "127.0.0.2",
            "127.255.255.254",
            "::1",
            "[::1]",
            "0:0:0:0:0:0:0:1",
            "[0:0:0:0:0:0:0:1]",
            "::0001",
            "localhost",
            "LOCALHOST",
        ],
    )
    def test_core_profile_loopback_tls_bind_allows_dev_oss_auth(
        self, monkeypatch: pytest.MonkeyPatch, loopback_addr: str
    ) -> None:
        monkeypatch.setenv("DALTON_PROFILE", "core")
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
        monkeypatch.setenv("DALTON_ALLOW_DEV_OSS_AUTH", "1")
        if loopback_addr:
            monkeypatch.setenv("DALTON_EDGE_TLS_BIND", loopback_addr)
        else:
            monkeypatch.delenv("DALTON_EDGE_TLS_BIND", raising=False)
        assert validate_dalton_auth_config() == "oss"

    @pytest.mark.parametrize("everflow_auth_mode", ["supabase", "oss"])
    def test_everflow_profile_unaffected_by_edge_tls_bind(
        self, monkeypatch: pytest.MonkeyPatch, everflow_auth_mode: str
    ) -> None:
        monkeypatch.setenv("DALTON_PROFILE", "everflow")
        monkeypatch.setenv("DALTON_AUTH_MODE", everflow_auth_mode)
        monkeypatch.setenv("DALTON_EDGE_TLS_BIND", "0.0.0.0")
        assert validate_dalton_auth_config() == everflow_auth_mode

    def test_transcription_ticket_secret_hardening_in_local_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 1. Explicit oss mode keeps predictable dev fallback
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
        assert get_dalton_transcription_ticket_hmac_key() is not None

        # 2. Local mode NEVER falls back to predictable dev seed or service/bridge tokens
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "s" * 40)
        monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "b" * 40)
        assert get_dalton_transcription_ticket_hmac_key() is None

        # 3. Local mode with transcription enabled fails startup without dedicated secret
        monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "true")
        with pytest.raises(
            DaltonStartupConfigError, match="DALTON_TRANSCRIPTION_TICKET_SECRET"
        ):
            validate_dalton_auth_config()

        # 4. With dedicated >=32-byte secret, startup passes and HMAC key is derived
        monkeypatch.setenv("DALTON_TRANSCRIPTION_TICKET_SECRET", TEST_TICKET_SECRET)
        assert validate_dalton_auth_config() == "local"
        assert get_dalton_transcription_ticket_hmac_key() is not None

    @pytest.mark.asyncio
    async def test_startup_db_hook_seeds_bootstrap_and_skips_local_dev_user_in_local_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)
        mock_seed = create_autospec(
            bootstrap_mod.seed_bootstrap_token_from_env_if_needed, return_value=True
        )
        monkeypatch.setattr(
            bootstrap_mod, "seed_bootstrap_token_from_env_if_needed", mock_seed
        )
        mock_logger = create_autospec(logging.Logger, instance=True)

        # In local mode: validates, seeds bootstrap token, returns False (do NOT create local-dev-user)
        should_provision = await bootstrap_mod.run_auth_db_startup_hook("oss", mock_logger)
        assert should_provision is False
        mock_seed.assert_awaited_once()

        # In local mode: seeding failure MUST fail startup loudly WITHOUT leaking raw DB error text
        secret_dsn_msg = "FATAL: password authentication failed for postgres://admin:s3cr3t@10.0.0.5/prod"
        mock_seed.side_effect = RuntimeError(secret_dsn_msg)
        with pytest.raises(DaltonStartupConfigError) as exc_boot:
            await bootstrap_mod.run_auth_db_startup_hook("oss", mock_logger)
        assert "RuntimeError" in str(exc_boot.value)
        assert "s3cr3t" not in str(exc_boot.value)
        assert "10.0.0.5" not in str(exc_boot.value)
        assert exc_boot.value.__cause__ is None
        logged_text = " ".join(str(call) for call in mock_logger.error.call_args_list)
        assert "RuntimeError" in logged_text
        assert "s3cr3t" not in logged_text
        assert "10.0.0.5" not in logged_text
        mock_seed.side_effect = None
        mock_seed.return_value = True

        # In platform + local mode: raises DaltonStartupConfigError even though HOST_MODE != oss
        monkeypatch.setattr(app_settings, "HOST_MODE", "platform")
        with pytest.raises(DaltonStartupConfigError, match="HOST_MODE=platform"):
            await bootstrap_mod.run_auth_db_startup_hook("platform", mock_logger)

        # In oss mode: returns True (keeps local-dev-user auto-provisioning)
        monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
        assert await bootstrap_mod.run_auth_db_startup_hook("oss", mock_logger) is True


class TestUnifiedBackendEntrypoints:
    """Every backend auth entrypoint across oss, local, and supabase modes."""

    @pytest.mark.asyncio
    async def test_oss_mode_allows_passwordless_local_dev_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
        req = _make_request()
        ws = _make_websocket()

        assert await get_current_user_id(request=req, credentials=None) == LOCAL_DEV_USER_ID
        assert await get_user_id_any_state(request=req, credentials=None) == LOCAL_DEV_USER_ID
        assert await get_optional_user_id(request=req, credentials=None) == LOCAL_DEV_USER_ID
        assert await verify_jwt_token(request=req, credentials=None) == LOCAL_DEV_USER_ID
        assert (await get_current_auth_info(request=req, credentials=None)).user_id == LOCAL_DEV_USER_ID
        assert await authenticate_websocket(ws) == LOCAL_DEV_USER_ID

    @pytest.mark.asyncio
    async def test_local_mode_enforces_jwt_session_revocation_and_optional_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)

        session_check = create_autospec(
            dispatch_mod.verify_local_session_active, return_value=True
        )
        monkeypatch.setattr(dispatch_mod, "verify_local_session_active", session_check)

        valid_token, expires_in = issue_local_access_token(
            user_id=TEST_USER_ID, session_id=TEST_SESSION_ID
        )
        assert expires_in == 900
        valid_creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=valid_token)
        bad_creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not-a-jwt")
        expired_token = jwt.encode(
            {
                "sub": TEST_USER_ID,
                "iat": int(time.time()) - 2000,
                "exp": int(time.time()) - 10,
                "sid": TEST_SESSION_ID,
                "typ": "access",
            },
            TEST_JWT_SECRET,
            algorithm=LOCAL_JWT_ALGORITHM,
        )
        expired_creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=expired_token)

        req = _make_request()

        # 1. Missing Authorization header -> 401 on required, None on optional
        for fn in (get_current_user_id, get_user_id_any_state):
            with pytest.raises(HTTPException) as exc:
                await fn(request=req, credentials=None)
            assert exc.value.status_code == 401
        assert await get_optional_user_id(request=req, credentials=None) is None

        # 1b. Item 15: Optional auth with malformed/empty Authorization header -> 401 (NOT anonymous!)
        for bad_hdr in ("Bearer ", "Basic dXNlcjpwYXNz", "", "Token abc"):
            req_malformed = _make_request({"Authorization": bad_hdr})
            with pytest.raises(HTTPException) as exc_opt_bad:
                await get_optional_user_id(request=req_malformed, credentials=None)
            assert exc_opt_bad.value.status_code == 401

        # 2. Bad / expired token -> 401 everywhere
        for creds in (bad_creds, expired_creds):
            for fn in (get_current_user_id, get_user_id_any_state, get_optional_user_id):
                with pytest.raises(HTTPException) as exc:
                    await fn(request=req, credentials=creds)
                assert exc.value.status_code == 401
            for fn in (verify_jwt_token, get_current_auth_info):
                with pytest.raises(HTTPException) as exc:
                    await fn(request=req, credentials=creds)
                assert exc.value.status_code == 401

        # 3. Valid token with active DB session -> passes everywhere
        assert await get_current_user_id(request=req, credentials=valid_creds) == TEST_USER_ID
        assert await get_user_id_any_state(request=req, credentials=valid_creds) == TEST_USER_ID
        assert await get_optional_user_id(request=req, credentials=valid_creds) == TEST_USER_ID
        assert await verify_jwt_token(request=req, credentials=valid_creds) == TEST_USER_ID
        assert await get_current_auth_info(request=req, credentials=valid_creds) == AuthInfo(
            user_id=TEST_USER_ID, auth_provider="local"
        )
        assert session_check.await_count == 5

        # 4. Item 3: Revoked DB session -> STILL-UNEXPIRED valid JWT immediately rejected (401 / WS 1008)
        session_check.side_effect = HTTPException(status_code=401, detail="Session has been revoked")
        for fn in (get_current_user_id, get_user_id_any_state, get_optional_user_id):
            with pytest.raises(HTTPException) as exc_rev:
                await fn(request=req, credentials=valid_creds)
            assert exc_rev.value.status_code == 401
        for fn in (verify_jwt_token, get_current_auth_info):
            with pytest.raises(HTTPException) as exc_rev2:
                await fn(request=req, credentials=valid_creds)
            assert exc_rev2.value.status_code == 401
        ws_revoked = _make_websocket(headers={"authorization": f"Bearer {valid_token}"})
        with pytest.raises(WebSocketException) as exc_ws_rev:
            await authenticate_websocket(ws_revoked)
        assert exc_ws_rev.value.code == 1008

        # 5. Active WebSocket closes with 1008 when session is revoked mid-connection
        session_check.side_effect = None
        session_check.return_value = True
        ws_live = _make_websocket(headers={"authorization": f"Bearer {valid_token}"})
        assert await authenticate_websocket(ws_live) == TEST_USER_ID
        assert ws_live.state.local_session_id == TEST_SESSION_ID
        assert await dispatch_mod.check_ws_local_session_or_close(ws_live) is True

        # Revoke session -> periodic watcher closes socket with 1008 and sets disconnected
        session_check.side_effect = HTTPException(status_code=401, detail="Session has been revoked")
        disconnected = asyncio.Event()
        watch_task = dispatch_mod.start_ws_session_revocation_watch(
            ws_live, disconnected, interval_seconds=0.05
        )
        assert watch_task is not None
        await asyncio.wait_for(disconnected.wait(), timeout=2.0)
        await watch_task
        ws_live.close.assert_awaited_once()
        assert ws_live.close.await_args.kwargs["code"] == 1008

        # 6. Round 3 Item 9: local mode REJECTS ?token= query param by default;
        #    only explicit DALTON_WS_ALLOW_QUERY_TOKEN=true opts in.
        session_check.side_effect = None
        session_check.return_value = True
        monkeypatch.delenv("DALTON_WS_ALLOW_QUERY_TOKEN", raising=False)
        ws_q_default_blocked = _make_websocket(query={"token": valid_token})
        with pytest.raises(WebSocketException) as exc_q:
            await authenticate_websocket(ws_q_default_blocked)
        assert exc_q.value.code == 1008

        monkeypatch.setenv("DALTON_WS_ALLOW_QUERY_TOKEN", "true")
        ws_q_opt_in = _make_websocket(query={"token": valid_token})
        assert await authenticate_websocket(ws_q_opt_in) == TEST_USER_ID
        monkeypatch.delenv("DALTON_WS_ALLOW_QUERY_TOKEN", raising=False)

        # Company system identity token -> rejected 401
        sys_token = jwt.encode(
            {
                "sub": COMPANY_SYSTEM_USER_ID,
                "iat": int(time.time()),
                "exp": int(time.time()) + 900,
                "sid": TEST_SESSION_ID,
                "typ": "access",
            },
            TEST_JWT_SECRET,
            algorithm=LOCAL_JWT_ALGORITHM,
        )
        with pytest.raises(HTTPException) as exc_sys:
            await get_current_user_id(
                request=req,
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=sys_token),
            )
        assert exc_sys.value.status_code == 401

    @pytest.mark.asyncio
    async def test_supabase_mode_preserves_existing_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
        monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")

        sb_info = AuthInfo(user_id=TEST_USER_ID, auth_provider="email")
        mock_verify_bearer = create_autospec(
            dalton_auth.verify_dalton_bearer, return_value=sb_info
        )
        mock_verify_opt = create_autospec(
            dalton_auth.verify_dalton_optional_bearer, return_value=TEST_USER_ID
        )
        mock_ws = create_autospec(
            dalton_auth.authenticate_dalton_websocket, return_value=TEST_USER_ID
        )
        monkeypatch.setattr(dalton_auth, "verify_dalton_bearer", mock_verify_bearer)
        monkeypatch.setattr(dalton_auth, "verify_dalton_optional_bearer", mock_verify_opt)
        monkeypatch.setattr(dalton_auth, "authenticate_dalton_websocket", mock_ws)

        req = _make_request()
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="sb-jwt")
        ws = _make_websocket(headers={"authorization": "Bearer sb-jwt"})

        assert await get_current_user_id(request=req, credentials=creds) == TEST_USER_ID
        assert await get_optional_user_id(request=req, credentials=creds) == TEST_USER_ID
        assert await verify_jwt_token(request=req, credentials=creds) == TEST_USER_ID
        assert await get_current_auth_info(request=req, credentials=creds) == sb_info
        assert await authenticate_websocket(ws) == TEST_USER_ID
        assert mock_verify_bearer.await_count == 3
        mock_verify_opt.assert_awaited_once()
        mock_ws.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["local", "supabase"])
    async def test_owner_header_alone_never_signs_in_under_dalton_auth(
        self, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        """Upstream's owner fallback must stay behind Dalton auth: a request
        carrying only the desktop owner secret, with no JWT, is refused."""
        from src.server.auth import owner_requests

        secret = "owner-secret-for-test"
        monkeypatch.setenv(owner_requests.OWNER_SECRET_ENV, secret)
        monkeypatch.setenv("DALTON_AUTH_MODE", mode)
        monkeypatch.setattr(api_utils, "HOST_MODE", "oss")
        monkeypatch.setattr(jwt_bearer, "HOST_MODE", "oss")
        if mode == "local":
            monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)
        else:
            monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")
            refuse = HTTPException(status_code=401, detail="missing bearer")
            monkeypatch.setattr(
                dalton_auth,
                "verify_dalton_bearer",
                create_autospec(dalton_auth.verify_dalton_bearer, side_effect=refuse),
            )
            monkeypatch.setattr(
                dalton_auth,
                "authenticate_dalton_websocket",
                create_autospec(
                    dalton_auth.authenticate_dalton_websocket,
                    side_effect=WebSocketException(code=1008),
                ),
            )

        owner_headers = {owner_requests.OWNER_HEADER: secret}
        req = _make_request(owner_headers)
        req.scope = {
            "state": {},
            "headers": [(owner_requests.OWNER_HEADER.encode(), secret.encode())],
        }
        assert owner_requests.is_owner(req.scope) is True

        for entry in (get_current_user_id, get_user_id_any_state, verify_jwt_token):
            with pytest.raises(HTTPException) as refused:
                await entry(request=req, credentials=None)
            assert refused.value.status_code == 401
        with pytest.raises(HTTPException) as refused_info:
            await get_current_auth_info(request=req, credentials=None)
        assert refused_info.value.status_code == 401
        with pytest.raises(WebSocketException):
            await authenticate_websocket(_make_websocket(headers=owner_headers))

    def test_sse_service_flags_background_dispatch_and_routes_enforce_local_auth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 1. Unified service-token checks across oss vs local
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
        svc_tok_mock = create_autospec(msg_mod._get_service_token, return_value="")
        monkeypatch.setattr(msg_mod, "_get_service_token", svc_tok_mock)
        assert msg_mod._is_service_request(_make_request()) is True
        assert app_settings.background_dispatch_requires_token() is False

        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)
        # Empty / short service token is NEVER trusted in local mode (including background_dispatch + messaging flags)
        assert msg_mod._is_service_request(_make_request()) is False
        assert app_settings.background_dispatch_requires_token() is True

        svc_tok_mock.return_value = "short-token"
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "short-token")
        assert msg_mod._is_service_request(_make_request({"X-Service-Token": "short-token"})) is False
        assert app_settings.background_dispatch_requires_token() is True

        # Item 14: resolve_messaging_service_flags in local mode does NOT trust a short matching token!
        from_svc, is_retry = dispatch_mod.resolve_messaging_service_flags(
            is_internal=False,
            is_dispatch=False,
            sent_token="short-token",
            configured_token="short-token",
        )
        assert from_svc is False
        assert is_retry is False

        long_token = "s" * 40
        svc_tok_mock.return_value = long_token
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", long_token)
        assert msg_mod._is_service_request(_make_request({"X-Service-Token": long_token})) is True
        assert app_settings.background_dispatch_requires_token() is False
        from_svc_ok, is_retry_ok = dispatch_mod.resolve_messaging_service_flags(
            is_internal=True,
            is_dispatch=False,
            sent_token=long_token,
            configured_token=long_token,
        )
        assert from_svc_ok is True
        assert is_retry_ok is True

        # 2. SSE send / reconnect / replay routes enforce Bearer JWT + active session in local mode
        session_check = create_autospec(
            dispatch_mod.verify_local_session_active, return_value=True
        )
        monkeypatch.setattr(dispatch_mod, "verify_local_session_active", session_check)

        app = FastAPI()
        app.include_router(msg_mod.router)
        client = TestClient(app, raise_server_exceptions=False)

        assert client.post("/api/v1/threads/messages", json={"messages": []}).status_code == 401
        assert client.get("/api/v1/threads/t1/messages/stream").status_code == 401
        assert client.get("/api/v1/threads/t1/messages/replay").status_code == 401

        bad_hdr = {"Authorization": "Bearer invalid-token"}
        assert client.post("/api/v1/threads/messages", json={"messages": []}, headers=bad_hdr).status_code == 401
        assert client.get("/api/v1/threads/t1/messages/stream", headers=bad_hdr).status_code == 401
        assert client.get("/api/v1/threads/t1/messages/replay", headers=bad_hdr).status_code == 401

        other_token, _ = issue_local_access_token(
            user_id=OTHER_USER_ID, session_id=TEST_SESSION_ID
        )
        other_hdr = {"Authorization": f"Bearer {other_token}"}

        mock_meta = create_autospec(
            conv_db.get_thread_auth_meta,
            return_value={"user_id": TEST_USER_ID, "workspace_owner_id": TEST_USER_ID, "workspace_id": "ws1"},
        )
        monkeypatch.setattr(conv_db, "get_thread_auth_meta", mock_meta)
        assert client.get("/api/v1/threads/t1/messages/stream", headers=other_hdr).status_code == 403

        # Revoked session on SSE reconnect -> immediate 401
        session_check.side_effect = HTTPException(status_code=401, detail="Session has been revoked")
        assert client.get("/api/v1/threads/t1/messages/stream", headers=other_hdr).status_code == 401
        session_check.side_effect = None
        session_check.return_value = True

        fake_rows: Any = type(
            "ReplayRows",
            (),
            {
                "owner_id": TEST_USER_ID,
                "workspace_owner_id": TEST_USER_ID,
                "thread": {"workspace_id": "ws1", "latest_run_id": None},
            },
        )()
        mock_replay = create_autospec(
            msg_mod.get_replay_thread_data, return_value=fake_rows
        )
        monkeypatch.setattr(msg_mod, "get_replay_thread_data", mock_replay)
        assert client.get("/api/v1/threads/t1/messages/replay", headers=other_hdr).status_code == 403


class TestConditionalLocalRouterRegistration:
    """Local auth & bootstrap routes are mounted ONLY when DALTON_AUTH_MODE=local."""

    def test_only_registers_in_local_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for mode in ("oss", "supabase"):
            monkeypatch.setenv("DALTON_AUTH_MODE", mode)
            parent = APIRouter()
            assert register_local_auth_routers(parent) is False
            assert parent.routes == []

        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        parent = APIRouter()
        assert register_local_auth_routers(parent) is True
        paths = {getattr(r, "path", "") for r in parent.routes}
        assert "/api/v1/dalton/auth/local/login" in paths
        assert "/api/v1/dalton/auth/local/refresh" in paths
        assert "/api/v1/dalton/auth/local/logout" in paths
        assert "/api/v1/dalton/auth/local/change-password" in paths
        assert "/api/v1/dalton/bootstrap/status" in paths
        assert "/api/v1/dalton/bootstrap/admin" in paths

    @pytest.mark.asyncio
    async def test_cli_auth_endpoint_reports_auth_required_in_local_mode_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round 3 Item 10: GET /api/v1/auth/cli returns auth_required=True in local mode."""
        monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
        res_oss = await users_mod.get_cli_auth()
        assert res_oss.auth_required is False

        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)
        res_local = await users_mod.get_cli_auth()
        assert res_local.auth_required is True

    @pytest.mark.asyncio
    async def test_established_user_events_sse_stops_promptly_when_session_revoked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round 3 Item 6: established user_events SSE terminates once local session is revoked."""
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)

        session_active = True

        async def _verify_session(*, user_id: str, session_id: str, now=None) -> bool:
            if not session_active:
                raise HTTPException(status_code=401, detail="Session has been revoked")
            return True

        monkeypatch.setattr(
            dispatch_mod,
            "verify_local_session_active",
            create_autospec(dispatch_mod.verify_local_session_active, side_effect=_verify_session),
        )
        monkeypatch.setattr(
            user_events_mod.SharedFeed,
            "open",
            create_autospec(user_events_mod.SharedFeed.open, return_value=None),
        )
        monkeypatch.setattr(
            user_events_mod,
            "_read_snapshot",
            create_autospec(
                user_events_mod._read_snapshot,
                return_value={"v": 1, "as_of_seq": 1, "live": [], "unseen": []},
            ),
        )

        # Prepare a controllable pub/sub wait generator that emits two events;
        # revoke the local session right before the second event arrives.
        events = iter([("message", {"type": "run_started", "seq": 1}), ("message", {"type": "run_finished", "seq": 2})])

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_subscribe(_uid: str):
            async def _wait(_timeout: float):
                nonlocal session_active
                kind, payload = next(events)
                if payload.get("seq") == 2:
                    session_active = False
                return kind, payload

            yield _wait

        monkeypatch.setattr(
            user_events_mod,
            "subscribe_to_user_events",
            create_autospec(user_events_mod.subscribe_to_user_events, side_effect=_fake_subscribe),
        )

        token, _ = issue_local_access_token(user_id=TEST_USER_ID, session_id=TEST_SESSION_ID)
        req = _make_request()
        uid = await get_current_user_id(
            request=req,
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        )
        assert uid == TEST_USER_ID
        assert req.scope["state"]["dalton_local_session_id"] == TEST_SESSION_ID

        resp = await user_events_mod.user_thread_events(x_user_id=uid, request=req)
        emitted: list[str] = []
        async for chunk in resp.body_iterator:
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            emitted.append(text)

        joined = "".join(emitted)
        assert "event: snapshot" in joined
        assert "run_started" in joined
        # Revoked before second event -> stream terminated immediately without delivering run_finished!
        assert "run_finished" not in joined

    @pytest.mark.asyncio
    async def test_long_lived_thread_mux_subagent_and_idle_chat_streams_close_on_revocation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round 4 Item 5: thread mux stream, subagent task stream, and silent live chat stream close <=30s on revocation."""
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)
        monkeypatch.setenv("DALTON_SSE_SESSION_RECHECK_SECONDS", "0.05")

        session_active = True

        async def _verify_session(*, user_id: str, session_id: str, now=None) -> bool:
            if not session_active:
                raise HTTPException(status_code=401, detail="Session has been revoked")
            return True

        monkeypatch.setattr(
            dispatch_mod,
            "verify_local_session_active",
            create_autospec(dispatch_mod.verify_local_session_active, side_effect=_verify_session),
        )

        token, _ = issue_local_access_token(user_id=TEST_USER_ID, session_id=TEST_SESSION_ID)
        req = _make_request()
        uid = await get_current_user_id(
            request=req,
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        )
        assert uid == TEST_USER_ID

        # 1. Silent live chat stream wrapped by wrap_local_sse_stream closes on periodic timer even with 0 incoming frames!
        async def _silent_chat_gen():
            yield "event: token\ndata: hello\n\n"
            await asyncio.sleep(5.0)
            yield "event: token\ndata: should_never_arrive\n\n"

        async def _revoke_soon():
            nonlocal session_active
            await asyncio.sleep(0.08)
            session_active = False

        revoker = asyncio.create_task(_revoke_soon())
        wrapped = dispatch_mod.wrap_local_sse_stream(_silent_chat_gen(), req, uid, interval_seconds=0.05)
        chunks = [c async for c in wrapped]
        await revoker
        assert chunks == ["event: token\ndata: hello\n\n"]

        # 2. Thread multiplexed content stream GET /threads/{id}/stream closes with timeout frame on revocation
        session_active = True
        import src.server.handlers.chat.thread_stream_mux_v2 as mux_mod

        async def _fake_mux(*args: Any, **kwargs: Any):
            nonlocal session_active
            yield "event: attached\ndata: {}\n\n"
            await asyncio.sleep(0.06)
            session_active = False
            await asyncio.sleep(5.0)
            yield "event: message_chunk\ndata: leaked\n\n"

        monkeypatch.setattr(
            msg_mod,
            "thread_access",
            create_autospec(msg_mod.thread_access, return_value=SimpleNamespace(kind="owner")),
        )
        monkeypatch.setattr(
            msg_mod.ThreadWatch,
            "of",
            create_autospec(msg_mod.ThreadWatch.of, return_value=None),
        )
        monkeypatch.setattr(
            mux_mod,
            "stream_thread_mux_v2",
            create_autospec(mux_mod.stream_thread_mux_v2, side_effect=_fake_mux),
        )
        mux_resp = await msg_mod.thread_stream_mux_endpoint(
            thread_id="t-1",
            x_user_id=uid,
            cursors=None,
            contract="v2",
            since_age_s=0.0,
            open_turn=False,
            request=req,
        )
        mux_out = "".join(
            [
                c.decode("utf-8") if isinstance(c, bytes) else str(c)
                async for c in mux_resp.body_iterator
            ]
        )
        assert "event: attached" in mux_out
        assert "event: timeout" in mux_out
        assert "leaked" not in mux_out

        # 3. Subagent task stream GET /threads/{id}/tasks/{task_id} closes promptly on revocation
        session_active = True
        import src.server.handlers.chat.task_run_sse_reader as task_sse_mod

        async def _fake_task_sse(*args: Any, **kwargs: Any):
            nonlocal session_active
            yield "event: tool_call\ndata: first\n\n"
            await asyncio.sleep(0.06)
            session_active = False
            await asyncio.sleep(5.0)
            yield "event: tool_call\ndata: leaked_task\n\n"

        monkeypatch.setattr(
            tasks_mod,
            "thread_access",
            create_autospec(tasks_mod.thread_access, return_value=SimpleNamespace(kind="owner")),
        )
        monkeypatch.setattr(
            task_sse_mod,
            "stream_task_run_sse",
            create_autospec(task_sse_mod.stream_task_run_sse, side_effect=_fake_task_sse),
        )
        task_resp = await tasks_mod.stream_subagent_task(
            thread_id="t-1",
            task_id="task1",
            x_user_id=uid,
            last_event_id=None,
            last_event_id_header=None,
            request=req,
        )
        task_out = "".join(
            [
                c.decode("utf-8") if isinstance(c, bytes) else str(c)
                async for c in task_resp.body_iterator
            ]
        )
        assert "first" in task_out
        assert "leaked_task" not in task_out

        # 4. Supabase mode: wrap_local_sse_stream returns the exact original stream untouched
        monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
        raw_gen = _silent_chat_gen()
        assert dispatch_mod.wrap_local_sse_stream(raw_gen, req, uid) is raw_gen
        await raw_gen.aclose()

    @pytest.mark.asyncio
    async def test_recheck_interval_cap_otel_context_and_initial_reject_upload_cleanup(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Round 5 Items 2 & 4: <=30s cap, OTel span preserved across frames & clean detach, upload claims released on initial reject."""
        # Item 2: Configs > 30s are clamped to 30.0s
        monkeypatch.setenv("DALTON_SSE_SESSION_RECHECK_SECONDS", "60")
        monkeypatch.setenv("DALTON_WS_SESSION_RECHECK_SECONDS", "120")
        assert dispatch_mod.get_sse_session_recheck_seconds() == 30.0
        assert dispatch_mod.get_ws_session_recheck_seconds() == 30.0
        watch_capped = dispatch_mod.LocalSSESessionWatch(
            user_id=TEST_USER_ID, session_id=TEST_SESSION_ID, interval_seconds=90.0
        )
        assert watch_capped._interval == 30.0
        assert watch_capped.seconds_until_next_check() <= 30.0

        # Item 4a: OpenTelemetry traced_body keeps chat.turn span across EVERY frame and detaches cleanly
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)
        monkeypatch.setenv("DALTON_SSE_SESSION_RECHECK_SECONDS", "0.05")
        session_active = True

        async def _verify_session(*, user_id: str, session_id: str, now=None) -> bool:
            if not session_active:
                raise HTTPException(status_code=401, detail="Session has been revoked")
            return True

        monkeypatch.setattr(
            dispatch_mod,
            "verify_local_session_active",
            create_autospec(dispatch_mod.verify_local_session_active, side_effect=_verify_session),
        )
        token, _ = issue_local_access_token(user_id=TEST_USER_ID, session_id=TEST_SESSION_ID)
        req = _make_request()
        uid = await get_current_user_id(
            request=req,
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        )

        from opentelemetry import trace as otel_trace
        from src.observability.tracing import chat_turn_observed, traced_body

        seen_spans: list[Any] = []

        async def _raw_frames():
            for idx in range(3):
                seen_spans.append(otel_trace.get_current_span())
                await asyncio.sleep(0.01)
                yield f"event: token\ndata: chunk-{idx}\n\n"

        with caplog.at_level(logging.ERROR):
            async with chat_turn_observed(
                model="test-model", user_id=uid, workspace_id="ws-1", thread_id="t-1"
            ) as (ctx, span, status):
                stream = dispatch_mod.wrap_local_sse_stream(
                    traced_body(ctx, span, _raw_frames()), req, uid, interval_seconds=1.0
                )
                out_chunks = [c async for c in stream]
                status["value"] = "completed"

        assert len(out_chunks) == 3
        assert len(seen_spans) == 3
        assert all(s is span for s in seen_spans), f"OTel span lost across frames: {seen_spans}"
        assert "Failed to detach context" not in caplog.text

        # Item 4b: When initial session recheck rejects BEFORE stream starts, provisional
        # upload claims and burst slot are released cleanly and underlying gen never starts.
        session_active = False
        import src.dalton.services.chat_uploads as chat_uploads_mod

        release_uploads_mock = create_autospec(
            chat_uploads_mod.release_request_chat_uploads, return_value=None
        )
        monkeypatch.setattr(
            chat_uploads_mod, "release_request_chat_uploads", release_uploads_mock
        )
        slot_released = False

        async def _release_slot() -> None:
            nonlocal slot_released
            slot_released = True

        gen_entered = False

        async def _never_started_gen():
            nonlocal gen_entered
            gen_entered = True
            yield "event: token\ndata: nope\n\n"

        cleanup = dispatch_mod.make_prestart_chat_cleanup(
            user_id=uid,
            upload_claim_key="claim-key-1",
            request_key="req-key-1",
            additional_context=[{"type": "chat_upload", "upload_id": "up-1"}],
            release_slot=_release_slot,
        )
        rejected_stream = dispatch_mod.wrap_local_sse_stream(
            _never_started_gen(), req, uid, on_initial_reject=cleanup
        )
        assert [c async for c in rejected_stream] == []
        assert gen_entered is False
        assert slot_released is True
        release_uploads_mock.assert_awaited_once_with(
            user_id=uid,
            claim_key="claim-key-1",
            request_key="req-key-1",
            additional_context=[{"type": "chat_upload", "upload_id": "up-1"}],
        )


class TestLocalWsTicketAndSameOriginStartup:
    """Item 6 (short-lived HMAC WS tickets) and Item 7 (same-origin startup validation)."""

    def test_ws_ticket_roundtrip_expiry_and_type_isolation(self, monkeypatch):
        from src.dalton.auth import tokens as tokens_mod

        monkeypatch.setenv("JWT_SIGNING_SECRET", "w" * 48)
        now = 1_750_000_000
        ticket, ttl = tokens_mod.issue_local_ws_ticket(
            user_id="user-ws-1",
            session_id="sid-ws-1",
            now=now,
        )
        ticket2, _ = tokens_mod.issue_local_ws_ticket(
            user_id="user-ws-1",
            session_id="sid-ws-1",
            now=now,
        )
        assert ttl == 60
        assert len(ticket.split(".")) == 2

        claims = tokens_mod.verify_local_ws_ticket(ticket, now=now + 59)
        claims2 = tokens_mod.verify_local_ws_ticket(ticket2, now=now + 59)
        assert claims.user_id == "user-ws-1"
        assert claims.session_id == "sid-ws-1"
        assert claims.jti and claims2.jti and claims.jti != claims2.jti

        # Expired at +60s
        with pytest.raises(HTTPException) as exc_exp:
            tokens_mod.verify_local_ws_ticket(ticket, now=now + 60)
        assert exc_exp.value.status_code == 401

        # Access JWT cannot pass as WS ticket, and WS ticket cannot pass as access JWT
        jwt_tok, _ = tokens_mod.issue_local_access_token(
            user_id="user-ws-1", session_id="sid-ws-1", now=now
        )
        with pytest.raises(HTTPException) as exc_jwt_as_ticket:
            tokens_mod.verify_local_ws_ticket(jwt_tok, now=now)
        assert exc_jwt_as_ticket.value.status_code == 401

        with pytest.raises(HTTPException) as exc_ticket_as_jwt:
            tokens_mod.decode_local_access_token(ticket)
        assert exc_ticket_as_jwt.value.status_code == 401

    @pytest.mark.asyncio
    async def test_websocket_dispatch_accepts_ws_ticket_once_and_rejects_replay_and_jwt(
        self, monkeypatch
    ):
        from types import SimpleNamespace
        from fastapi import WebSocketException
        from src.dalton.auth import dispatch as dispatch_mod, tokens as tokens_mod

        monkeypatch.setenv("HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", "s" * 48)
        monkeypatch.delenv("DALTON_WS_ALLOW_QUERY_TOKEN", raising=False)

        monkeypatch.setattr(
            dispatch_mod,
            "verify_local_session_active",
            create_autospec(dispatch_mod.verify_local_session_active, return_value=None),
        )
        monkeypatch.setattr(dispatch_mod, "_refuse_holder", lambda _u: None)

        seen_jti_keys: dict[str, int] = {}

        class _FakeRedisClient:
            async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
                assert nx is True
                assert ex is not None and 1 <= ex <= 60
                if key in seen_jti_keys:
                    return False
                seen_jti_keys[key] = ex
                return True

        redis_holder: list[Any] = [_FakeRedisClient()]

        async def _fake_ws_redis() -> Any:
            return redis_holder[0]

        monkeypatch.setattr(dispatch_mod, "_get_ws_ticket_redis_client", _fake_ws_redis)

        ticket, _ = tokens_mod.issue_local_ws_ticket(user_id="u-ws", session_id="sid-ws")
        jwt_tok, _ = tokens_mod.issue_local_access_token(user_id="u-ws", session_id="sid-ws")

        class _FakeWS:
            def __init__(self, query_params: dict[str, str], headers: dict[str, str] | None = None):
                self.query_params = query_params
                self.headers = headers or {}
                self.state = SimpleNamespace()
                self.closed_code: int | None = None

            async def close(self, code: int = 1000, reason: str = "") -> None:
                self.closed_code = code

        # 1. Valid ?ticket= succeeds once and records its jti in Redis with <=60s TTL
        ws_ok = _FakeWS({"ticket": ticket})
        uid = await dispatch_mod.dispatch_oss_websocket(ws_ok, local_dev_user_id="local-dev-user")
        assert uid == "u-ws"
        assert ws_ok.state.local_user_id == "u-ws"
        assert ws_ok.state.local_session_id == "sid-ws"
        assert len(seen_jti_keys) == 1

        # 1b. Replaying the same one-time ticket is rejected with 1008
        ws_replay = _FakeWS({"ticket": ticket})
        with pytest.raises(WebSocketException) as exc_replay:
            await dispatch_mod.dispatch_oss_websocket(ws_replay, local_dev_user_id="local-dev-user")
        assert exc_replay.value.code == 1008
        assert ws_replay.closed_code == 1008

        # 1c. Unavailable Redis fails closed with 1008
        fresh_ticket, _ = tokens_mod.issue_local_ws_ticket(user_id="u-ws", session_id="sid-ws")
        redis_holder[0] = None
        ws_no_redis = _FakeWS({"ticket": fresh_ticket})
        with pytest.raises(WebSocketException) as exc_no_redis:
            await dispatch_mod.dispatch_oss_websocket(
                ws_no_redis, local_dev_user_id="local-dev-user"
            )
        assert exc_no_redis.value.code == 1008
        redis_holder[0] = _FakeRedisClient()

        # 2. Access JWT passed via ?ticket= is rejected with 1008
        ws_bad = _FakeWS({"ticket": jwt_tok})
        with pytest.raises(WebSocketException) as exc_bad:
            await dispatch_mod.dispatch_oss_websocket(ws_bad, local_dev_user_id="local-dev-user")
        assert exc_bad.value.code == 1008
        assert ws_bad.closed_code == 1008

        # 3. WS ticket passed via Authorization: Bearer is rejected with 1008
        ws_bearer_ticket = _FakeWS({}, headers={"authorization": f"Bearer {ticket}"})
        with pytest.raises(WebSocketException) as exc_bt:
            await dispatch_mod.dispatch_oss_websocket(
                ws_bearer_ticket, local_dev_user_id="local-dev-user"
            )
        assert exc_bt.value.code == 1008

    @pytest.mark.asyncio
    async def test_ws_ticket_redis_independent_of_cache_toggle_and_rechecks_expiry_before_jti(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.auth import dispatch as dispatch_mod, tokens as tokens_mod
        from src.utils.cache.redis_cache import RedisCacheClient

        monkeypatch.setenv("HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", "s" * 48)
        monkeypatch.setenv("REDIS_CACHE_ENABLED", "false")
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/2")

        # 1. Even with REDIS_CACHE_ENABLED=false, the dedicated WS-ticket Redis client initializes.
        cache_client = RedisCacheClient()
        assert cache_client.enabled is False

        ws_redis = await dispatch_mod._get_ws_ticket_redis_client()
        try:
            assert ws_redis is not None
            conn_kwargs = ws_redis.connection_pool.connection_kwargs
            assert conn_kwargs.get("host") == "127.0.0.1"
            assert conn_kwargs.get("port") == 6399
            assert conn_kwargs.get("db") == 2
        finally:
            await dispatch_mod.close_ws_ticket_redis_client()

        # 2. If the ticket expires while waiting on DB session validation, reject with 401
        #    BEFORE calling Redis SET NX EX (never clamp remaining_ttl up to 1s).
        issued_now = int(time.time())
        ticket, _ = tokens_mod.issue_local_ws_ticket(
            user_id="u-exp",
            session_id="sid-exp",
            ttl_seconds=2,
            now=issued_now,
        )
        redis_set_called = False

        class _GuardRedis:
            async def set(self, *_a: Any, **_kw: Any) -> bool:
                nonlocal redis_set_called
                redis_set_called = True
                return True

        async def _guard_redis_client() -> Any:
            return _GuardRedis()

        clock_box = [float(issued_now + 1)]

        async def _slow_session_check(*, user_id: str, session_id: str) -> None:
            # Advance clock past ticket.expires_at (issued_now + 2) while in DB check
            clock_box[0] = float(issued_now + 3)

        monkeypatch.setattr(dispatch_mod, "_get_ws_ticket_redis_client", _guard_redis_client)
        monkeypatch.setattr(dispatch_mod, "_refuse_holder", lambda _u: None)
        monkeypatch.setattr(
            dispatch_mod,
            "verify_local_session_active",
            create_autospec(dispatch_mod.verify_local_session_active, side_effect=_slow_session_check),
        )
        monkeypatch.setattr(tokens_mod.time, "time", lambda: clock_box[0])
        monkeypatch.setattr(dispatch_mod.time, "time", lambda: clock_box[0])

        with pytest.raises(HTTPException) as exc_exp:
            await dispatch_mod._verify_local_ws_ticket_and_session(ticket)
        assert exc_exp.value.status_code == 401
        assert "expired" in str(exc_exp.value.detail).lower()
        assert redis_set_called is False

    def test_uvicorn_handshake_log_filter_redacts_ws_ticket_and_token_query(self) -> None:
        from src.config.logging_config import _SENSITIVE_QUERY_LOG_FILTER

        raw_ticket = "eyJ2IjoxLCJzdWIiOiJ1LTEifQ.supersecretHMACsig"
        rec = logging.LogRecord(
            name="uvicorn.error",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "WebSocket %s" [accepted]',
            args=("127.0.0.1:54321", f"/ws/v1/market-data/aggregates/stock?ticket={raw_ticket}&foo=bar"),
            exc_info=None,
        )
        assert _SENSITIVE_QUERY_LOG_FILTER.filter(rec) is True
        formatted = rec.getMessage()
        assert raw_ticket not in formatted
        assert "ticket=REDACTED" in formatted
        assert "foo=bar" in formatted

        rec_msg = logging.LogRecord(
            name="uvicorn.error",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=f'GET /ws/v1/stream?token={raw_ticket}',
            args=(),
            exc_info=None,
        )
        assert _SENSITIVE_QUERY_LOG_FILTER.filter(rec_msg) is True
        assert raw_ticket not in rec_msg.getMessage()
        assert "token=REDACTED" in rec_msg.getMessage()

    def test_logout_refuses_cross_account_stale_tab_with_409_and_keeps_cookie(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.auth import local_router as lr_mod

        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)

        revoke_mock = create_autospec(
            lr_mod.revoke_session_for_logout,
            return_value="mismatch",
        )
        monkeypatch.setattr(lr_mod, "revoke_session_for_logout", revoke_mock)

        app = FastAPI()
        app.include_router(lr_mod.local_auth_router)
        client = TestClient(app, raise_server_exceptions=False)

        resp_mismatch = client.post(
            "/api/v1/dalton/auth/local/logout",
            json={"sub": TEST_USER_ID},
            cookies={"dalton_refresh_token": "cookie-for-user-b"},
            headers={"Origin": "http://testserver"},
        )
        assert resp_mismatch.status_code == 409
        # Must NOT clear user B's refresh cookie on 409!
        assert "set-cookie" not in {k.lower() for k in resp_mismatch.headers.keys()}
        revoke_mock.assert_awaited_once_with(
            "cookie-for-user-b",
            expected_user_id=TEST_USER_ID,
            expected_session_id=None,
            reason="logout",
        )

        # Matching sub revokes and clears cookie
        revoke_mock.reset_mock()
        revoke_mock.return_value = "revoked"
        resp_ok = client.post(
            "/api/v1/dalton/auth/local/logout",
            json={"sub": TEST_USER_ID},
            cookies={"dalton_refresh_token": "cookie-for-user-a"},
            headers={"Origin": "http://testserver"},
        )
        assert resp_ok.status_code == 200
        assert "set-cookie" in {k.lower() for k in resp_ok.headers.keys()}

    def test_validate_dalton_auth_config_requires_same_origin_in_local_mode(self, monkeypatch):
        from src.dalton.profile import DaltonStartupConfigError
        from src.dalton.settings import validate_dalton_auth_config

        monkeypatch.setenv("HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_PROFILE", "core")
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", "k" * 48)
        monkeypatch.delenv("DALTON_FRONTEND_URL", raising=False)
        monkeypatch.delenv("FRONTEND_URL", raising=False)
        monkeypatch.delenv("VITE_API_BASE_URL", raising=False)

        # Same-origin succeeds (including implicit default port 443 == :443)
        monkeypatch.setenv("PUBLIC_APP_URL", "https://app.example.com")
        monkeypatch.setenv("SERVER_BASE_URL", "https://app.example.com:443")
        assert validate_dalton_auth_config() == "local"

        # Cross-origin rejected at startup
        monkeypatch.setenv("PUBLIC_APP_URL", "https://app.example.com")
        monkeypatch.setenv("SERVER_BASE_URL", "https://api.example.com")
        with pytest.raises(DaltonStartupConfigError, match="same-origin"):
            validate_dalton_auth_config()
