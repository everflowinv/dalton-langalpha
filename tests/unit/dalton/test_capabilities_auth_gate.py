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

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import ssl
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
from src.dalton.capabilities import effective_capabilities
from src.dalton.profile import DeploymentProfile
from src.dalton.settings import get_dalton_config
from src.server.auth import jwt_bearer as jwt_bearer_mod
from src.server.utils import api as api_utils
from tests.standalone.checks import _lib as standalone_lib
from tests.standalone.checks import b1_dev_mode as b1_check
from tests.standalone.checks import b2_backend_capabilities as b2_check
from tests.standalone.checks import b4_local_auth as b4_check
from tests.standalone.checks import b6_tls_backup_upgrade as b6_check

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
                return _json_response(
                    200,
                    {
                        "profile": "core",
                        "capabilities": list(b2_check.EXPECTED_CORE_DEFAULT_CAPABILITIES),
                    },
                )
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

    def test_b2_expected_core_capabilities_matches_product_defaults(self) -> None:
        actual_core_defaults = sorted(
            cap.value
            for cap in effective_capabilities(
                profile=DeploymentProfile.CORE,
                environ={"APP_ENV": "core", "DALTON_PROFILE": "core"},
            )
        )
        assert b2_check.EXPECTED_CORE_DEFAULT_CAPABILITIES == actual_core_defaults
        assert actual_core_defaults == ["company_wiki"]

    def test_b2_acquire_session_oss_delegates_to_shared_owner_header_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spy_http = create_autospec(
            b2_check._lib.http_request,
            return_value=_json_response(404, {"detail": "Not Found"}),
        )
        spy_owner_headers = create_autospec(
            b2_check._lib.oss_owner_headers,
            return_value={"X-LangAlpha-Owner": "delegated-owner-secret"},
        )
        monkeypatch.setattr(b2_check._lib, "http_request", spy_http)
        monkeypatch.setattr(b2_check._lib, "oss_owner_headers", spy_owner_headers)

        ctx = create_autospec(standalone_lib.ComposeContext, instance=True)
        ctx.env = {}
        headers, uid = b2_check._acquire_auth_session(ctx, "http://127.0.0.1:8088")
        assert headers == {"X-LangAlpha-Owner": "delegated-owner-secret"}
        assert uid == b2_check.DEFAULT_USER_ID
        spy_owner_headers.assert_called_once_with(ctx)


class TestStandaloneOssOwnerSecretHelpers:
    """Verify shared owner-secret helpers in ``_lib.py``, ``run_checks.sh``, and Caddyfile."""

    def test_ensure_oss_owner_secret_writes_0600_and_replaces_duplicates(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(
            "# header\n"
            "APP_ENV=core\n"
            "LANGALPHA_OWNER_SECRET=old-secret-1\n"
            "export LANGALPHA_OWNER_SECRET=old-secret-2\n"
            "DALTON_AUTH_MODE=local\n",
            encoding="utf-8",
        )
        env_path.chmod(0o644)

        result = standalone_lib.ensure_oss_owner_secret(env_path)
        assert result is None
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""
        assert (env_path.stat().st_mode & 0o777) == 0o600

        raw_lines = env_path.read_text(encoding="utf-8").splitlines()
        owner_lines = [ln for ln in raw_lines if "LANGALPHA_OWNER_SECRET=" in ln]
        assert len(owner_lines) == 1
        parsed = standalone_lib.load_env_file(env_path)
        secret_val = parsed["LANGALPHA_OWNER_SECRET"]
        assert len(secret_val) == 64
        assert secret_val not in ("old-secret-1", "old-secret-2")
        assert parsed["APP_ENV"] == "core"
        assert parsed["DALTON_AUTH_MODE"] == "local"

    def test_oss_owner_headers_precedence_merge_and_required_error_redaction(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LANGALPHA_OWNER_SECRET", "from-os-env")
        ctx = create_autospec(standalone_lib.ComposeContext, instance=True)
        ctx.env = {"LANGALPHA_OWNER_SECRET": "from-ctx-env"}

        extra = {"X-Probe": "1"}
        # Precedence: owner_secret > env_map > ctx.env > os.environ
        assert standalone_lib.oss_owner_headers(
            ctx,
            env_map={"LANGALPHA_OWNER_SECRET": "from-env-map"},
            owner_secret="from-explicit",
            extra=extra,
        ) == {"X-LangAlpha-Owner": "from-explicit", "X-Probe": "1"}
        assert extra == {"X-Probe": "1"}  # caller mapping untouched

        assert standalone_lib.oss_owner_headers(
            ctx, env_map={"LANGALPHA_OWNER_SECRET": "from-env-map"}
        ) == {"X-LangAlpha-Owner": "from-env-map"}
        assert standalone_lib.oss_owner_headers(ctx) == {"X-LangAlpha-Owner": "from-ctx-env"}

        ctx.env = {}
        assert standalone_lib.oss_owner_headers(ctx) == {"X-LangAlpha-Owner": "from-os-env"}

        monkeypatch.delenv("LANGALPHA_OWNER_SECRET", raising=False)
        assert standalone_lib.oss_owner_headers(ctx) == {}
        with pytest.raises(RuntimeError, match="LANGALPHA_OWNER_SECRET"):
            standalone_lib.oss_owner_headers(ctx, required=True)

    def test_configure_fake_llm_model_forwards_owner_header_to_all_three_calls(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen_headers: list[dict[str, str] | None] = []

        def _fake_http(
            method: str,
            url: str,
            *,
            headers: Any = None,
            json_body: Any = None,
            data: bytes | None = None,
            timeout: float = 30.0,
        ) -> standalone_lib.HttpResponse:
            _ = method, url, json_body, data, timeout
            seen_headers.append(dict(headers) if headers else None)
            return _json_response(200, {"ok": True})

        spy_http = create_autospec(standalone_lib.http_request, side_effect=_fake_http)
        monkeypatch.setattr(standalone_lib, "http_request", spy_http)

        ctx = create_autospec(standalone_lib.ComposeContext, instance=True)
        ctx.edge_url = "http://127.0.0.1:8088"
        ctx.env = {"LANGALPHA_OWNER_SECRET": "fake-llm-owner-secret"}

        model = standalone_lib.configure_fake_llm_model(ctx)
        assert model == standalone_lib.FAKE_MODEL_NAME
        assert len(seen_headers) == 3
        assert all(hdr == {"X-LangAlpha-Owner": "fake-llm-owner-secret"} for hdr in seen_headers)

    def test_owner_secret_in_secret_scan_and_caddy_access_log_redaction(
        self,
        tmp_path: Path,
    ) -> None:
        assert "LANGALPHA_OWNER_SECRET" in standalone_lib.SECRET_ENV_KEYS

        owner_val = "0123456789abcdef0123456789abcdef"
        env_file = tmp_path / ".env"
        env_file.write_text(f"LANGALPHA_OWNER_SECRET={owner_val}\n", encoding="utf-8")
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "compose-logs-oss.txt").write_text(
            f'{{"X-Langalpha-Owner":["{owner_val}"]}}\n', encoding="utf-8"
        )
        leaks = standalone_lib.scan_artifacts_for_secrets(artifacts, env_file)
        assert leaks == ["compose-logs-oss.txt: leaked LANGALPHA_OWNER_SECRET"]
        assert all(owner_val not in hit for hit in leaks)

        caddy_src = (REPO_ROOT / "deploy" / "standalone" / "Caddyfile").read_text(
            encoding="utf-8"
        )
        # Both global default/error logger and access-log snippet redact sensitive headers.
        assert caddy_src.count("request>headers>X-Langalpha-Owner delete") >= 2
        assert caddy_src.count("request>headers>X-LangAlpha-Owner delete") >= 2
        assert caddy_src.count("request>headers>Authorization delete") >= 2
        assert caddy_src.count("request>headers>Cookie delete") >= 2
        assert "log default {" in caddy_src

    def test_secret_scan_covers_research_host_and_runtime_credentials(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert "DALTON_RESEARCH_HOST_TOKEN" in standalone_lib.SECRET_ENV_KEYS
        assert "DALTON_RESEARCH_ADMIN_TOKEN" in standalone_lib.SECRET_ENV_KEYS

        rh_tok = "rh-host-token-secret-0123456789abcdef"
        rh_adm = "rh-admin-token-secret-0123456789abcdef"
        rt_jwt = "eyJhbGciOiJIUzI1NiJ9.runtime-access-token-secret"
        rt_ws = "ws-ticket-runtime-secret-9876543210"
        rt_inv = "invite-token-runtime-secret-abcdef1234"
        rt_pwd = "AdminPass!runtime987654"

        env_file = tmp_path / ".env"
        env_file.write_text(
            f"DALTON_RESEARCH_HOST_TOKEN={rh_tok}\nDALTON_RESEARCH_ADMIN_TOKEN={rh_adm}\n",
            encoding="utf-8",
        )
        rt_file = tmp_path / "runtime-secrets.jsonl"
        monkeypatch.setenv(standalone_lib.RUNTIME_SECRETS_ENV_VAR, str(rt_file))
        standalone_lib.record_runtime_secret("LOCAL_ACCESS_TOKEN", rt_jwt)
        standalone_lib.record_runtime_secret("LOCAL_WS_TICKET", rt_ws)
        standalone_lib.record_runtime_secret("LOCAL_INVITE_TOKEN", rt_inv)
        standalone_lib.record_runtime_secret("LOCAL_ADMIN_PASSWORD", rt_pwd)
        assert (rt_file.stat().st_mode & 0o777) == 0o600

        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "check-output-local.txt").write_text(
            f"a={rh_tok}\nb={rh_adm}\nc={rt_jwt}\nd={rt_ws}\ne={rt_inv}\nf={rt_pwd}\n",
            encoding="utf-8",
        )
        leaks = standalone_lib.scan_artifacts_for_secrets(artifacts, env_file)
        assert leaks == [
            "check-output-local.txt: leaked DALTON_RESEARCH_HOST_TOKEN",
            "check-output-local.txt: leaked DALTON_RESEARCH_ADMIN_TOKEN",
            "check-output-local.txt: leaked LOCAL_ACCESS_TOKEN",
            "check-output-local.txt: leaked LOCAL_WS_TICKET",
            "check-output-local.txt: leaked LOCAL_INVITE_TOKEN",
            "check-output-local.txt: leaked LOCAL_ADMIN_PASSWORD",
        ]
        for secret_val in (rh_tok, rh_adm, rt_jwt, rt_ws, rt_inv, rt_pwd):
            assert all(secret_val not in hit for hit in leaks)

    def test_auth_assertions_report_status_and_field_names_without_leaking_tokens(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 1. b2 docstring documents manual-only local bootstrap warning
        doc = b2_check._acquire_auth_session.__doc__ or ""
        assert "local 分支会消耗一次性自举，仅供手动排查" in doc

        # 2. b2 bootstrap & login assertions omit access_token value when user_id is missing
        for var in (
            "STANDALONE_ACCESS_TOKEN",
            "STANDALONE_ADMIN_IDENTIFIER",
            "STANDALONE_ADMIN_EMAIL",
            "STANDALONE_ADMIN_PASSWORD",
            "BOOTSTRAP_SETUP_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)

        boot_secret_jwt = "jwt-bootstrap-must-not-appear-in-assertion"

        def _fake_boot_missing_uid(
            method: str,
            url: str,
            *,
            headers: Any = None,
            json_body: Any = None,
            data: bytes | None = None,
            timeout: float = 30.0,
        ) -> standalone_lib.HttpResponse:
            _ = method, headers, json_body, data, timeout
            if url.endswith("/api/v1/dalton/bootstrap/status"):
                return _json_response(200, {"needs_bootstrap": True})
            if url.endswith("/api/v1/dalton/capabilities"):
                return _json_response(401, {"detail": "Missing authentication"})
            if url.endswith("/api/v1/dalton/bootstrap/admin"):
                return _json_response(201, {"access_token": boot_secret_jwt, "user": {}})
            raise AssertionError(f"Unexpected URL: {url}")

        monkeypatch.setattr(
            b2_check._lib,
            "http_request",
            create_autospec(standalone_lib.http_request, side_effect=_fake_boot_missing_uid),
        )
        ctx = create_autospec(standalone_lib.ComposeContext, instance=True)
        ctx.env = {"BOOTSTRAP_SETUP_TOKEN": "boot-secret-token"}
        with pytest.raises(AssertionError) as boot_exc:
            b2_check._acquire_auth_session(ctx, "http://127.0.0.1:8088")
        assert boot_secret_jwt not in str(boot_exc.value)
        assert "status=201" in str(boot_exc.value)
        assert "access_token" in str(boot_exc.value)

        # 2b. b2 login assertion omits access_token value when user_id is missing
        login_secret_jwt = "jwt-login-must-not-appear-in-assertion"
        monkeypatch.setenv("STANDALONE_ADMIN_EMAIL", "admin@core.local")
        monkeypatch.setenv("STANDALONE_ADMIN_PASSWORD", "StrongPass!123456")

        def _fake_login_missing_uid(
            method: str,
            url: str,
            *,
            headers: Any = None,
            json_body: Any = None,
            data: bytes | None = None,
            timeout: float = 30.0,
        ) -> standalone_lib.HttpResponse:
            _ = method, headers, json_body, data, timeout
            if url.endswith("/api/v1/dalton/bootstrap/status"):
                return _json_response(200, {"needs_bootstrap": False})
            if url.endswith("/api/v1/dalton/capabilities"):
                return _json_response(401, {"detail": "Missing authentication"})
            if url.endswith("/api/v1/dalton/auth/local/login"):
                return _json_response(200, {"access_token": login_secret_jwt, "user": {}})
            raise AssertionError(f"Unexpected URL: {url}")

        monkeypatch.setattr(
            b2_check._lib,
            "http_request",
            create_autospec(standalone_lib.http_request, side_effect=_fake_login_missing_uid),
        )
        with pytest.raises(AssertionError) as login_exc:
            b2_check._acquire_auth_session(ctx, "http://127.0.0.1:8088")
        assert login_secret_jwt not in str(login_exc.value)
        assert "status=200" in str(login_exc.value)
        assert "access_token" in str(login_exc.value)

        # 3. b4 WS ticket assertion omits ticket value when expires_in is unexpected
        ws_secret_ticket = "hmac-ws-ticket-must-not-appear-in-assertion"
        refresh_count = {"n": 0}

        def _fake_b4_http(
            method: str,
            url: str,
            *,
            headers: Any = None,
            json_body: Any = None,
            data: bytes | None = None,
            timeout: float = 30.0,
        ) -> standalone_lib.HttpResponse:
            _ = method, headers, data, timeout
            if url.endswith("/bootstrap/status"):
                return _json_response(200, {"needs_bootstrap": refresh_count["n"] == 0})
            if url.endswith("/bootstrap/admin"):
                if (json_body or {}).get("bootstrap_token") == "wrong-bootstrap-token":
                    return _json_response(401, {"detail": "invalid"})
                if refresh_count["n"] == 0:
                    refresh_count["n"] = 1
                    return standalone_lib.HttpResponse(
                        status=201,
                        headers={"set-cookie": "dalton_refresh_token=rcookie1-secret; HttpOnly"},
                        body=json.dumps(
                            {
                                "access_token": "adm-tok-secret-1",
                                "user": {"user_id": "uid-1", "tier": "admin"},
                            }
                        ).encode("utf-8"),
                    )
                return _json_response(409, {"detail": "already bootstrapped"})
            if url.endswith("/users/me"):
                return _json_response(200, {"user": {"user_id": "uid-1"}})
            if url.endswith("/auth/local/refresh"):
                refresh_count["n"] += 1
                idx = refresh_count["n"]
                return standalone_lib.HttpResponse(
                    status=200,
                    headers={"set-cookie": f"dalton_refresh_token=rcookie{idx}-secret; HttpOnly"},
                    body=json.dumps({"access_token": f"refreshed-tok-secret-{idx}"}).encode("utf-8"),
                )
            if url.endswith("/auth/local/ws-ticket"):
                return _json_response(200, {"ticket": ws_secret_ticket, "expires_in": 999})
            raise AssertionError(f"Unexpected URL: {url}")

        monkeypatch.setattr(
            b4_check._lib,
            "http_request",
            create_autospec(standalone_lib.http_request, side_effect=_fake_b4_http),
        )
        b4_ctx = create_autospec(standalone_lib.ComposeContext, instance=True)
        b4_ctx.edge_url = "http://127.0.0.1:8088"
        b4_ctx.env = {"BOOTSTRAP_SETUP_TOKEN": "boot-secret-token"}
        with pytest.raises(AssertionError) as ws_exc:
            b4_check._check_bootstrap_and_sessions(b4_ctx)
        assert ws_secret_ticket not in str(ws_exc.value)
        assert "status=200" in str(ws_exc.value)
        assert "ticket" in str(ws_exc.value)

    def test_run_checks_oss_writes_owner_secret_without_printing_it(
        self,
        tmp_path: Path,
    ) -> None:
        work_dir = tmp_path / "work"
        checks_dir = tmp_path / "checks"
        artifacts_dir = tmp_path / "artifacts"
        fake_bin = tmp_path / "bin"
        for d in (work_dir, checks_dir, artifacts_dir, fake_bin):
            d.mkdir()

        for name in ("docker-compose.yml", "docker-compose.core.yml", "docker-compose.ci.yml"):
            (work_dir / name).write_text("services: {}\n", encoding="utf-8")
        env_file = work_dir / ".env"
        env_file.write_text("APP_ENV=core\nDALTON_AUTH_MODE=local\n", encoding="utf-8")
        env_file.chmod(0o600)

        (checks_dir / "_lib.py").write_text(
            (REPO_ROOT / "tests/standalone/checks/_lib.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (checks_dir / "b1_stub.py").write_text(
            "# auth: oss\n"
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
            "import _lib\n"
            "ctx = _lib.ComposeContext.from_environment()\n"
            "hdrs = _lib.oss_owner_headers(ctx, required=True)\n"
            "assert len(hdrs['X-LangAlpha-Owner']) == 64\n"
            "print('b1_stub_ok')\n",
            encoding="utf-8",
        )

        fake_docker = fake_bin / "docker"
        fake_docker.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "ps" || "$1" == "volume" ]]; then\n'
            "  exit 0\n"
            "fi\n"
            'if [[ "$1" == "compose" ]]; then\n'
            '  if [[ "$*" == *"get_dalton_auth_mode"* ]]; then\n'
            '    echo "oss"\n'
            "    exit 0\n"
            "  fi\n"
            '  if [[ "$*" == *"LANGALPHA_OWNER_SECRET"* ]]; then\n'
            '    echo "1"\n'
            "    exit 0\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)

        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
        env["LANGALPHA_OWNER_SECRET"] = "stale-shell-secret-must-be-unset"

        proc = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "tests/standalone/run_checks.sh"),
                "--auth",
                "oss",
                "--work-dir",
                str(work_dir),
                "--checks-dir",
                str(checks_dir),
                "--ci-compose",
                str(work_dir / "docker-compose.ci.yml"),
                "--artifacts-dir",
                str(artifacts_dir),
                "--project-name",
                "unit-oss-owner-test",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert proc.returncode == 0, f"run_checks.sh failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        assert "b1_stub_ok" in proc.stdout

        parsed = standalone_lib.load_env_file(env_file)
        owner_secret = parsed.get("LANGALPHA_OWNER_SECRET", "")
        assert len(owner_secret) == 64
        assert owner_secret != "stale-shell-secret-must-be-unset"
        assert parsed.get("DALTON_AUTH_MODE") == "local"
        assert (env_file.stat().st_mode & 0o777) == 0o600
        assert owner_secret not in proc.stdout
        assert owner_secret not in proc.stderr


class TestB1AndB6OwnerHeaderAndEnvContracts:
    """Verify b1 & b6 attach ``X-LangAlpha-Owner`` on edge calls and respect ``init.sh`` defaults."""

    def test_b1_edge_requests_attach_owner_header_and_probe_omits_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        owner_secret = "b1-test-owner-secret-value-0001"
        ctx = create_autospec(b1_check._lib.ComposeContext, instance=True)
        ctx.edge_url = "http://127.0.0.1:8088"
        ctx.internal_service_token = "svc-tok-012345678901234567890123456789"
        ctx.env = {"LANGALPHA_OWNER_SECRET": owner_secret}
        ctx.psql_query.return_value = "probe-tid-1"

        seen_http: list[tuple[str, str, dict[str, str] | None]] = []

        def _fake_http(
            method: str,
            url: str,
            *,
            headers: Any = None,
            json_body: Any = None,
            data: bytes | None = None,
            timeout: float = 30.0,
        ) -> Any:
            _ = json_body, data, timeout
            hdr_copy = dict(headers) if headers else None
            seen_http.append((method, url, hdr_copy))
            if url.endswith("/__probe/headers"):
                return _json_response(200, {"headers": {"X-Probe-Sentinel": "keep-me"}})
            if url.endswith("/api/v1/users/me"):
                return _json_response(200, {"user": {"user_id": b1_check.LOCAL_DEV_USER}})
            if url.endswith("/messages/replay"):
                return _json_response(403, {"detail": "Forbidden"})
            if url.endswith("/sandbox-object"):
                return _json_response(404, {"detail": "Not Found"})
            if url.endswith("/api/v1/workspaces"):
                return _json_response(201, {"workspace_id": "ws-b1-1"})
            if url.endswith("/sandbox/preview-url"):
                return _json_response(200, {"url": "http://127.0.0.1:8765/"})
            return _json_response(200, {"ok": True})

        def _fake_direct(
            _ctx: Any,
            *,
            method: str,
            path: str,
            user_id: str,
            json_body: Any = None,
        ) -> tuple[int, str]:
            _ = method, json_body
            if path == "/api/v1/users/me":
                return 200, json.dumps({"user": {"user_id": user_id}})
            if path == "/api/v1/workspaces":
                return 201, json.dumps({"workspace_id": "probe-ws-1"})
            return 200, ""

        monkeypatch.setattr(
            b1_check._lib,
            "http_request",
            create_autospec(b1_check._lib.http_request, side_effect=_fake_http),
        )
        monkeypatch.setattr(
            b1_check,
            "_direct_backend_http",
            create_autospec(b1_check._direct_backend_http, side_effect=_fake_direct),
        )
        monkeypatch.setattr(
            b1_check._lib,
            "wait_for_http_ready",
            create_autospec(
                b1_check._lib.wait_for_http_ready,
                return_value=_json_response(200, {"ok": True}),
            ),
        )

        b1_check.check_users_and_edge_header_stripping(ctx)
        ws_id = b1_check.configure_fake_model_and_workspace(ctx)
        assert ws_id == "ws-b1-1"
        b1_check.check_loopback_preview(ctx, ws_id)

        by_suffix = {url.split("8088", 1)[1]: hdrs for _m, url, hdrs in seen_http}
        # Public /__probe/headers must NOT receive X-LangAlpha-Owner (avoid echoing secret)
        assert "X-LangAlpha-Owner" not in (by_suffix["/__probe/headers"] or {})
        # Authenticated edge endpoints MUST carry X-LangAlpha-Owner
        for path in (
            "/api/v1/users/me",
            "/api/v1/threads/probe-tid-1/messages/replay",
            "/api/v1/users/me/preferences",
            "/api/v1/users/me/api-keys",
            "/api/v1/keys/test",
            "/api/v1/workspaces",
            "/api/v1/workspaces/ws-b1-1/sandbox/preview-url",
        ):
            assert (by_suffix[path] or {}).get("X-LangAlpha-Owner") == owner_secret, (
                f"b1 call to {path} missing X-LangAlpha-Owner header"
            )

    def test_b6_seed_and_verify_restored_data_forward_owner_headers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        owner_secret = "b6-test-owner-secret-value-0002"
        ctx = create_autospec(b6_check._lib.ComposeContext, instance=True)
        ctx.edge_url = "http://127.0.0.1:8088"
        ctx.env = {
            "LANGALPHA_OWNER_SECRET": owner_secret,
            "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME": "chat-uploads",
        }
        ctx.psql_query.side_effect = ["uploads/obj-1.txt", "uploading"]

        http_headers_seen: list[tuple[str, dict[str, str] | None]] = []
        sse_headers_seen: list[tuple[str, dict[str, str] | None]] = []

        def _fake_http(
            method: str,
            url: str,
            *,
            headers: Any = None,
            json_body: Any = None,
            data: bytes | None = None,
            timeout: float = 30.0,
        ) -> Any:
            _ = method, json_body, data, timeout
            http_headers_seen.append((url, dict(headers) if headers else None))
            if url.endswith("/api/v1/workspaces"):
                return _json_response(201, {"workspace_id": "ws-b6-1"})
            if url.endswith("/api/v1/dalton/chat-uploads"):
                return _json_response(200, {"upload_id": "up-1"})
            return _json_response(200, {"ok": True})

        @contextmanager
        def _fake_sse(
            method: str,
            url: str,
            *,
            headers: Any = None,
            json_body: Any = None,
            timeout: float = 120.0,
        ) -> Any:
            _ = method, json_body, timeout
            sse_headers_seen.append((url, dict(headers) if headers else None))
            events = [
                b6_check._lib.SSEEvent(
                    event_id="1",
                    event="message_chunk",
                    raw_data='{"thread_id":"tid-b6-1","content":"hello restored"}',
                    data={"thread_id": "tid-b6-1", "content": "hello restored"},
                ),
                b6_check._lib.SSEEvent(
                    event_id="2",
                    event="replay_done",
                    raw_data="{}",
                    data={},
                ),
            ]
            yield 200, {}, iter(events)

        monkeypatch.setattr(
            b6_check._lib,
            "http_request",
            create_autospec(b6_check._lib.http_request, side_effect=_fake_http),
        )
        monkeypatch.setattr(b6_check._lib, "open_sse_stream", _fake_sse)

        sentinel = b6_check.seed_sentinel_data(ctx)
        assert sentinel["workspace_id"] == "ws-b6-1"
        assert sentinel["thread_id"] == "tid-b6-1"
        assert len(http_headers_seen) == 8  # 3 fake-llm + 1 workspace + 4 chat-upload calls
        assert all(
            (hdrs or {}).get("X-LangAlpha-Owner") == owner_secret
            for _url, hdrs in http_headers_seen
        )
        assert len(sse_headers_seen) == 2  # chat POST + replay GET
        assert all(
            (hdrs or {}).get("X-LangAlpha-Owner") == owner_secret
            for _url, hdrs in sse_headers_seen
        )

        # Verify _verify_restored_data forwards auth_headers to /workspaces and /messages/replay
        https_seen: list[tuple[str, dict[str, str] | None]] = []

        def _fake_https(
            method: str,
            server_hostname: str,
            port: int,
            path: str,
            *,
            context: ssl.SSLContext,
            headers: dict[str, str] | None = None,
            json_body: Any = None,
            data: bytes | None = None,
            timeout: float = 30.0,
        ) -> Any:
            _ = method, server_hostname, port, context, json_body, data, timeout
            https_seen.append((path, dict(headers) if headers else None))
            if path == "/api/v1/workspaces":
                return _json_response(200, {"workspaces": [{"workspace_id": "ws-b6-1"}]})
            sse_raw = (
                b'event: message_chunk\n'
                b'data: {"thread_id":"tid-b6-1","content":"hello restored"}\n\n'
                b'event: replay_done\ndata: {}\n\n'
            )
            return b6_check._lib.HttpResponse(status=200, headers={}, body=sse_raw)

        monkeypatch.setattr(
            b6_check,
            "_https_request",
            create_autospec(b6_check._https_request, side_effect=_fake_https),
        )
        monkeypatch.setattr(
            b6_check,
            "_snapshot_table_counts",
            create_autospec(b6_check._snapshot_table_counts, return_value={"users": 1}),
        )
        monkeypatch.setattr(
            b6_check,
            "_query_thread_row",
            create_autospec(b6_check._query_thread_row, return_value="row-1"),
        )
        monkeypatch.setattr(
            b6_check,
            "_count_minio_objects",
            create_autospec(b6_check._count_minio_objects, return_value=1),
        )
        monkeypatch.setattr(
            b6_check,
            "_fetch_minio_object_sha_and_ct",
            create_autospec(
                b6_check._fetch_minio_object_sha_and_ct,
                return_value=(sentinel["payload_sha"], "text/plain"),
            ),
        )

        auth_hdrs = {"X-LangAlpha-Owner": owner_secret}
        b6_check._verify_restored_data(
            ["docker", "compose"],
            Path("/tmp"),
            {},
            18543,
            create_autospec(ssl.SSLContext, instance=True),
            "postgres",
            "postgres",
            {"users": 1},
            "row-1",
            1,
            sentinel,
            auth_headers=auth_hdrs,
        )
        assert https_seen == [
            ("/api/v1/workspaces", auth_hdrs),
            ("/api/v1/threads/tid-b6-1/messages/replay", auth_hdrs),
        ]

    def test_b6_does_not_assert_stale_oss_default_on_init_env(self) -> None:
        b6_src = (
            REPO_ROOT / "tests" / "standalone" / "checks" / "b6_tls_backup_upgrade.py"
        ).read_text(encoding="utf-8")
        assert 'assert cur_env_map.get("DALTON_AUTH_MODE") == "local"' in b6_src
        assert 'assert cur_env_map.get("DALTON_AUTH_MODE") == "oss"' not in b6_src
        assert '_lib.OWNER_SECRET_ENV_KEY: restore_owner_secret' in b6_src
        assert '_lib.OWNER_SECRET_ENV_KEY: upgrade_owner_secret' in b6_src

    def test_b6_stage_init_dir_copies_all_init_sh_sourced_helpers(self, tmp_path: Path) -> None:
        init_src = (REPO_ROOT / "scripts" / "standalone" / "init.sh").read_text(encoding="utf-8")
        sourced_scripts: set[str] = set()
        for raw_line in init_src.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = re.match(r"^(?:source|\.)\s+(.+)$", stripped)
            if not m:
                continue
            target = m.group(1).strip().strip('"').strip("'")
            m_rel = re.match(r"^\$\{?SCRIPT_DIR\}?/(.+)$", target)
            assert m_rel is not None, (
                f"init.sh sources unexpected non-SCRIPT_DIR target: {stripped!r}"
            )
            sourced_scripts.add(m_rel.group(1))

        assert sourced_scripts == {"env_tokens.sh", "_compose_env.sh"}
        assert sourced_scripts.issubset(set(b6_check._INIT_STAGE_SCRIPTS))

        staged_init = b6_check._stage_init_dir(REPO_ROOT, tmp_path)
        assert staged_init.is_file()
        assert (tmp_path / ".env.core.example").is_file()
        for helper in sourced_scripts:
            assert (staged_init.parent / helper).is_file(), (
                f"_stage_init_dir did not copy sourced helper {helper}"
            )
        help_proc = subprocess.run(
            ["bash", str(staged_init), "--help"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert help_proc.returncode == 0, (
            f"Staged init.sh --help failed (missing sourced helper?): {help_proc.stderr}"
        )


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
