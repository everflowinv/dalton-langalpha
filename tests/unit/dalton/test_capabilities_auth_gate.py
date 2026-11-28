"""Unit tests locking authenticated access to ``GET /api/v1/dalton/capabilities``.

Covers:
1. ``GET /api/v1/dalton/capabilities`` keeps ``CurrentUserId`` authentication in
   both Standalone Core (``DALTON_AUTH_MODE=local``) and Everflow
   (``DALTON_AUTH_MODE=supabase``), as well as proxied unkeyed ``oss`` mode.
2. ``tests/standalone/checks/b2_backend_capabilities.py`` acquires a session via
   the normal Core onboarding/login flow before calling authenticated endpoints.
3. ``.github/workflows/standalone-core-e2e.yml`` customer-smoke step parses
   cleanly (YAML + bash -n + Python AST) and authenticates via bootstrap before
   reading ``/api/v1/dalton/capabilities``.

All test doubles use ``unittest.mock.create_autospec``.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any
from unittest.mock import create_autospec

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import pytest
import yaml

from src.config import settings as app_settings
from src.config.core import clear_config_cache
from src.dalton import auth as dalton_auth
from src.dalton.app import dalton_router
from src.dalton.auth import dispatch as dispatch_mod
from src.dalton.auth.tokens import issue_local_access_token
from src.dalton.settings import get_dalton_config
from src.server.auth import jwt_bearer as jwt_bearer_mod
from src.server.utils import api as api_utils
from tests.standalone.checks import _lib as standalone_lib
from tests.standalone.checks import b2_backend_capabilities as b2_check

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "standalone-core-e2e.yml"

TEST_JWT_SECRET = "test-capabilities-jwt-signing-secret-at-least-32-bytes!"
TEST_USER_ID = "11111111-1111-4111-8111-111111111111"
TEST_SESSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _make_capabilities_app() -> FastAPI:
    """Mount the real ``dalton_router`` with ZERO auth dependency overrides."""
    app = FastAPI()
    app.include_router(dalton_router)
    return app


def _reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "APP_ENV",
        "DALTON_PROFILE",
        "DALTON_ENABLED",
        "DALTON_AUTH_MODE",
        "DALTON_ALLOW_DEV_OSS_AUTH",
        "JWT_SIGNING_SECRET",
        "LANGALPHA_OWNER_SECRET",
        "BRAND_NAME",
        "SUPABASE_URL",
        "DALTON_ALLOWED_EMAILS",
        "INTERNAL_SERVICE_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setattr(api_utils, "HOST_MODE", "oss")
    monkeypatch.setattr(jwt_bearer_mod, "HOST_MODE", "oss")
    clear_config_cache()
    get_dalton_config.cache_clear()


class TestCapabilitiesEndpointAuthGate:
    """Verify ``GET /api/v1/dalton/capabilities`` requires a valid authenticated session."""

    def test_route_depends_on_get_current_user_id_and_is_not_anonymous(self) -> None:
        from tests.unit.server.test_account_suspended_gate import NO_USER_CREDENTIAL

        assert ("GET", "/api/v1/dalton/capabilities") not in NO_USER_CREDENTIAL

        cap_routes = [
            route
            for route in dalton_router.routes
            if isinstance(route, APIRoute)
            and route.path == "/api/v1/dalton/capabilities"
            and "GET" in (route.methods or set())
        ]
        assert len(cap_routes) == 1
        dep_calls: set[Any] = set()
        stack = [cap_routes[0].dependant]
        while stack:
            dep = stack.pop()
            if dep.call is not None:
                dep_calls.add(dep.call)
            stack.extend(dep.dependencies)
        assert api_utils.get_current_user_id in dep_calls

    def test_core_local_auth_rejects_anonymous_and_accepts_active_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _reset_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "core")
        monkeypatch.setenv("DALTON_PROFILE", "core")
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        monkeypatch.setenv("JWT_SIGNING_SECRET", TEST_JWT_SECRET)
        clear_config_cache()
        get_dalton_config.cache_clear()

        verify_active = create_autospec(
            dispatch_mod.verify_local_session_active,
            return_value=None,
        )
        monkeypatch.setattr(dispatch_mod, "verify_local_session_active", verify_active)

        client = TestClient(_make_capabilities_app())

        # 1. Anonymous caller -> 401
        resp_anon = client.get("/api/v1/dalton/capabilities")
        assert resp_anon.status_code == 401
        verify_active.assert_not_awaited()

        # 2. Malformed Bearer token -> 401
        resp_bad = client.get(
            "/api/v1/dalton/capabilities",
            headers={"Authorization": "Bearer not.a.valid.jwt"},
        )
        assert resp_bad.status_code == 401

        # 3. Valid local Bearer JWT with active session -> 200
        token, _expires_in = issue_local_access_token(
            user_id=TEST_USER_ID,
            session_id=TEST_SESSION_ID,
        )
        resp_ok = client.get(
            "/api/v1/dalton/capabilities",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp_ok.status_code == 200
        body = resp_ok.json()
        assert body["profile"] == "core"
        assert body["brand_name"] == "Dalton"
        verify_active.assert_awaited_once_with(
            user_id=TEST_USER_ID,
            session_id=TEST_SESSION_ID,
        )

        # 4. Revoked session -> 401
        verify_active.side_effect = HTTPException(status_code=401, detail="Session revoked")
        resp_revoked = client.get(
            "/api/v1/dalton/capabilities",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp_revoked.status_code == 401

    def test_everflow_supabase_auth_rejects_anonymous(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _reset_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "dalton")
        monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        clear_config_cache()
        get_dalton_config.cache_clear()

        ready_stub = create_autospec(dalton_auth._ensure_dalton_auth_ready, return_value=None)
        monkeypatch.setattr(dalton_auth, "_ensure_dalton_auth_ready", ready_stub)

        client = TestClient(_make_capabilities_app())
        resp = client.get("/api/v1/dalton/capabilities")
        assert resp.status_code == 401
        ready_stub.assert_awaited_once()

    def test_unkeyed_oss_behind_edge_proxy_rejects_anonymous(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _reset_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "core")
        monkeypatch.setenv("DALTON_PROFILE", "core")
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
        monkeypatch.setenv("DALTON_ALLOW_DEV_OSS_AUTH", "1")
        clear_config_cache()
        get_dalton_config.cache_clear()

        client = TestClient(_make_capabilities_app())
        resp = client.get(
            "/api/v1/dalton/capabilities",
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert resp.status_code == 401


def _json_response(status: int, payload: Any) -> standalone_lib.HttpResponse:
    return standalone_lib.HttpResponse(
        status=status,
        headers={"content-type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )


class TestB2CapabilitiesSessionFlow:
    """Verify ``b2_backend_capabilities.py`` acquires a session and forwards auth headers."""

    def test_acquire_session_local_bootstrap_verifies_401_and_returns_bearer(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for var in (
            "STANDALONE_ACCESS_TOKEN",
            "STANDALONE_ADMIN_IDENTIFIER",
            "STANDALONE_ADMIN_EMAIL",
            "STANDALONE_ADMIN_PASSWORD",
            "BOOTSTRAP_SETUP_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)

        calls: list[tuple[str, str, dict[str, str] | None, Any]] = []

        def _fake_http(
            method: str,
            url: str,
            *,
            headers: Any = None,
            json_body: Any = None,
            data: bytes | None = None,
            timeout: float = 30.0,
        ) -> standalone_lib.HttpResponse:
            _ = data, timeout
            calls.append((method, url, dict(headers) if headers else None, json_body))
            if url.endswith("/api/v1/dalton/bootstrap/status"):
                return _json_response(200, {"needs_bootstrap": True})
            if url.endswith("/api/v1/dalton/capabilities") and not headers:
                return _json_response(401, {"detail": "Missing authentication"})
            if url.endswith("/api/v1/dalton/bootstrap/admin"):
                assert json_body["bootstrap_token"] == "boot-secret-token"
                return _json_response(
                    201,
                    {
                        "access_token": "jwt-from-bootstrap",
                        "user": {"user_id": "admin-uid-1", "tier": "admin"},
                    },
                )
            raise AssertionError(f"Unexpected HTTP call: {method} {url}")

        spy_http = create_autospec(b2_check._lib.http_request, side_effect=_fake_http)
        monkeypatch.setattr(b2_check._lib, "http_request", spy_http)

        ctx = create_autospec(standalone_lib.ComposeContext, instance=True)
        ctx.env = {"BOOTSTRAP_SETUP_TOKEN": "boot-secret-token"}

        auth_headers, uid = b2_check._acquire_auth_session(ctx, "http://127.0.0.1:8088")
        assert auth_headers == {"Authorization": "Bearer jwt-from-bootstrap"}
        assert uid == "admin-uid-1"
        assert [c[1] for c in calls] == [
            "http://127.0.0.1:8088/api/v1/dalton/bootstrap/status",
            "http://127.0.0.1:8088/api/v1/dalton/capabilities",
            "http://127.0.0.1:8088/api/v1/dalton/bootstrap/admin",
        ]

    def test_acquire_session_local_login_when_already_bootstrapped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("STANDALONE_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("STANDALONE_ADMIN_EMAIL", "admin@core.local")
        monkeypatch.setenv("STANDALONE_ADMIN_PASSWORD", "StrongPass!1234")

        def _fake_http(
            method: str,
            url: str,
            *,
            headers: Any = None,
            json_body: Any = None,
            data: bytes | None = None,
            timeout: float = 30.0,
        ) -> standalone_lib.HttpResponse:
            _ = data, timeout
            if url.endswith("/api/v1/dalton/bootstrap/status"):
                return _json_response(200, {"needs_bootstrap": False})
            if url.endswith("/api/v1/dalton/capabilities") and not headers:
                return _json_response(401, {"detail": "Missing authentication"})
            if url.endswith("/api/v1/dalton/auth/local/login"):
                assert json_body == {
                    "identifier": "admin@core.local",
                    "password": "StrongPass!1234",
                }
                return _json_response(
                    200,
                    {
                        "access_token": "jwt-from-login",
                        "user": {"user_id": "admin-uid-2", "tier": "admin"},
                    },
                )
            raise AssertionError(f"Unexpected HTTP call: {method} {url}")

        spy_http = create_autospec(b2_check._lib.http_request, side_effect=_fake_http)
        monkeypatch.setattr(b2_check._lib, "http_request", spy_http)

        ctx = create_autospec(standalone_lib.ComposeContext, instance=True)
        ctx.env = {}

        auth_headers, uid = b2_check._acquire_auth_session(ctx, "http://127.0.0.1:8088")
        assert auth_headers == {"Authorization": "Bearer jwt-from-login"}
        assert uid == "admin-uid-2"

    def test_acquire_session_oss_uses_owner_secret_header_when_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("LANGALPHA_OWNER_SECRET", raising=False)
        monkeypatch.delenv("TEST_USER_ID", raising=False)

        spy_http = create_autospec(
            b2_check._lib.http_request,
            return_value=_json_response(404, {"detail": "Not Found"}),
        )
        monkeypatch.setattr(b2_check._lib, "http_request", spy_http)

        ctx = create_autospec(standalone_lib.ComposeContext, instance=True)
        ctx.env = {"LANGALPHA_OWNER_SECRET": "owner-secret-123"}

        auth_headers, uid = b2_check._acquire_auth_session(ctx, "http://127.0.0.1:8088")
        assert auth_headers == {"X-LangAlpha-Owner": "owner-secret-123"}
        assert uid == b2_check.DEFAULT_USER_ID

    def test_check_capabilities_forwards_auth_headers_to_all_calls(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        observed_headers: list[dict[str, str] | None] = []

        def _fake_http(
            method: str,
            url: str,
            *,
            headers: Any = None,
            json_body: Any = None,
            data: bytes | None = None,
            timeout: float = 30.0,
        ) -> standalone_lib.HttpResponse:
            _ = method, json_body, data, timeout
            observed_headers.append(dict(headers) if headers else None)
            if url.endswith("/api/v1/dalton/capabilities"):
                return _json_response(200, {"profile": "core", "capabilities": []})
            if url.endswith("/api/v1/dalton/system-jobs"):
                return _json_response(200, {"jobs": []})
            return _json_response(404, {"detail": "disabled"})

        spy_http = create_autospec(b2_check._lib.http_request, side_effect=_fake_http)
        monkeypatch.setattr(b2_check._lib, "http_request", spy_http)

        bearer = {"Authorization": "Bearer test-jwt"}
        b2_check._check_capabilities_and_disabled_routes(
            "http://127.0.0.1:8088",
            headers=bearer,
        )
        assert len(observed_headers) == 12
        assert all(hdr == bearer for hdr in observed_headers)


class TestCustomerSmokeWorkflowContract:
    """Validate customer-smoke YAML, shell syntax, inline Python, and authenticated flow."""

    def test_customer_smoke_bootstraps_and_reads_capabilities_with_bearer(self) -> None:
        wf = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        steps = wf["jobs"]["standalone-core-e2e"]["steps"]
        smoke_step = next(
            s
            for s in steps
            if "start-check customer release tarball" in str(s.get("name", ""))
        )
        run_script: str = smoke_step["run"]

        # 1. Shell syntax check (`bash -n`)
        proc = subprocess.run(
            ["bash", "-n"],
            input=run_script,
            text=True,
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0, f"customer-smoke bash -n failed: {proc.stderr}"

        # 2. Inline Python syntax check
        match = re.search(r"python3 - <<'PY'\n(.*?)\nPY\b", run_script, flags=re.S)
        assert match is not None, "Expected inline python3 - <<'PY' block in customer-smoke"
        py_src = match.group(1)
        compile(py_src, "customer-smoke-inline.py", "exec")

        # 3. Flow assertions
        assert "/api/v1/dalton/bootstrap/status" in run_script
        assert "/api/v1/dalton/bootstrap/admin" in py_src
        assert "assert unauth_status == 401" in py_src
        assert '"Authorization": f"Bearer {access_token}"' in py_src
        assert "/api/v1/dalton/capabilities" in py_src
