"""Unit tests for deployment credentials admin API, auth matrix, store validation, and redaction.

Uses synthetic fixtures and ``unittest.mock.create_autospec`` exclusively.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.dalton import auth as dalton_auth
from src.dalton import credentials as dalton_credentials
from src.dalton import settings as dalton_settings
from src.dalton.app import dalton_router
from src.dalton.auth import dispatch as auth_dispatch
from src.dalton.deployment_credentials import (
    auth as dc_auth,
    catalog as dc_catalog,
    hooks as dc_hooks,
    router as dc_router,
    snapshot as dc_snapshot,
    store as dc_store,
)
from src.dalton.deployment_credentials.catalog import (
    BATCH1_FIELD_SPECS,
    DaltonDeploymentFieldSpec,
)
from src.dalton.deployment_credentials.redirect_uri import google_redirect_uri_eligible
from src.dalton.integrations.constants import (
    FEISHU_INTEGRATION_EVENTS_PATH,
    FEISHU_OAUTH_CALLBACK_PATH,
    GOOGLE_OAUTH_CALLBACK_PATH,
    build_feishu_integration_events_url,
    build_integration_redirect_uri,
)
from src.server.services import vault_invalidation
from src.server.services.setup_settings import apply as setup_apply
from src.server.services.setup_settings import environment as setup_env
from src.server.services.setup_settings import follow as setup_follow
from src.server.services.setup_settings.catalog import key_specs
from src.server.utils.secret_redactor import SecretRedactor

ENDPOINT = "/api/v1/dalton/deployment-settings"

FAKE_GOOGLE_SECRET = "test-secret-not-real-google-0001"
FAKE_FEISHU_SECRET = "test-secret-not-real-feishu-0002"
FAKE_ENCRYPT_KEY = "test-secret-not-real-encrypt-0003"
FAKE_VERIFY_TOKEN = "test-secret-not-real-verify-0004"
ALL_FAKE_SECRETS = (
    FAKE_GOOGLE_SECRET,
    FAKE_FEISHU_SECRET,
    FAKE_ENCRYPT_KEY,
    FAKE_VERIFY_TOKEN,
)


class _FakeEncryptedStore:
    """In-memory stand-in for Postgres deployment_settings + meta + audit tables."""

    def __init__(self, *, revision: int = 0, rows: dict[str, str] | None = None) -> None:
        self.revision = revision
        self.rows: dict[str, str] = dict(rows or {})
        self.updated_at: dict[str, datetime] = {
            k: datetime(2026, 11, 26, 12, 0, tzinfo=UTC) for k in self.rows
        }
        self.audit_rows: list[tuple[Any, ...]] = []
        self.executed_sql: list[str] = []

    def make_get_db_connection(self):
        store = self

        class _FakeCursor:
            def __init__(self) -> None:
                self._next_one: Any = None
                self._next_all: list[Any] = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def execute(self, sql: str, params: Any = None) -> None:
                store.executed_sql.append(sql)
                norm = " ".join(sql.split()).lower()
                assert "user_vault_secrets" not in norm, "Batch 1 must never touch user_vault_secrets"
                if "pg_advisory_xact_lock" in norm:
                    self._next_one = (None,)
                elif "select revision from dalton_deployment_settings_meta" in norm:
                    self._next_one = (store.revision,)
                elif "select name, updated_at from deployment_settings" in norm:
                    self._next_all = [
                        (k, store.updated_at[k]) for k in sorted(store.rows)
                    ]
                elif "select name from deployment_settings" in norm:
                    self._next_all = [(k,) for k in sorted(store.rows)]
                elif "select pgp_sym_decrypt" in norm:
                    name = params[1]
                    self._next_one = (store.rows[name],) if name in store.rows else None
                elif "insert into deployment_settings" in norm:
                    name, plaintext, _enc_key = params
                    store.rows[name] = plaintext
                    store.updated_at[name] = datetime.now(UTC)
                elif "delete from deployment_settings" in norm:
                    name = params[0]
                    store.rows.pop(name, None)
                    store.updated_at.pop(name, None)
                elif "update dalton_deployment_settings_meta" in norm:
                    store.revision = int(params[0])
                elif "insert into dalton_deployment_settings_audit" in norm:
                    store.audit_rows.append(tuple(params))

            async def fetchone(self) -> Any:
                return self._next_one

            async def fetchall(self) -> list[Any]:
                return list(self._next_all)

        class _FakeConn:
            def cursor(self, **_kwargs):
                return _FakeCursor()

            @asynccontextmanager
            async def transaction(self):
                yield

        @asynccontextmanager
        async def _get_db_connection(conn=None):
            yield conn if conn is not None else _FakeConn()

        return _get_db_connection


@pytest.fixture(autouse=True)
def _clean_deployment_env(monkeypatch: pytest.MonkeyPatch):
    for key in (
        "DALTON_GOOGLE_OAUTH_CLIENT_ID",
        "DALTON_GOOGLE_OAUTH_CLIENT_SECRET",
        "DALTON_FEISHU_APP_ID",
        "DALTON_FEISHU_APP_SECRET",
        "DALTON_FEISHU_ENCRYPT_KEY",
        "DALTON_FEISHU_VERIFICATION_TOKEN",
        "PUBLIC_APP_URL",
        "DALTON_FRONTEND_URL",
        "FRONTEND_URL",
        "DALTON_PM_USER_IDS",
        "DALTON_PROFILE",
        "APP_ENV",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DALTON_ENABLED", "1")
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "test-byok-key-not-real-0123456789abcdef")
    monkeypatch.setattr(setup_env, "_at_start", None)
    key_specs.cache_clear()
    dc_snapshot.reset_active_snapshot_for_test()
    yield
    dc_snapshot.reset_active_snapshot_for_test()
    key_specs.cache_clear()


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(dalton_router)
    return TestClient(app)


def _patch_local_admin(monkeypatch: pytest.MonkeyPatch, *, user_id: str = "admin-1", is_admin: bool = True) -> None:
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setattr(
        dc_auth,
        "dispatch_oss_user_id",
        create_autospec(auth_dispatch.dispatch_oss_user_id, return_value=user_id),
    )
    if is_admin:
        monkeypatch.setattr(
            dc_auth,
            "require_catalog_admin_async",
            create_autospec(dalton_credentials.require_catalog_admin_async, return_value=None),
        )
    else:
        monkeypatch.setattr(
            dc_auth,
            "require_catalog_admin_async",
            create_autospec(
                dalton_credentials.require_catalog_admin_async,
                side_effect=HTTPException(status_code=403, detail="Admin required"),
            ),
        )


def _assert_no_secrets_leaked(text: str) -> None:
    for secret in ALL_FAKE_SECRETS:
        assert secret not in text, "Test secret leaked into response or logs"


class TestAuthMatrix:
    def test_oss_mode_service_token_and_cli_token_rejected_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client()

        # 1. DALTON_AUTH_MODE=oss -> 403 login_mode_required
        monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
        res_oss = client.get(ENDPOINT)
        assert res_oss.status_code == 403
        assert res_oss.json()["detail"]["code"] == "login_mode_required"

        # 2. X-Service-Token -> 403 session_auth_required even in local mode
        monkeypatch.setenv("DALTON_AUTH_MODE", "local")
        res_svc = client.get(ENDPOINT, headers={"X-Service-Token": "s" * 40})
        assert res_svc.status_code == 403
        assert res_svc.json()["detail"]["code"] == "session_auth_required"

        # 2b. Admin JWT + empty X-Service-Token header -> 403 session_auth_required
        _patch_local_admin(monkeypatch, user_id="local-admin-empty-svc", is_admin=True)
        res_empty_svc = client.get(
            ENDPOINT,
            headers={"Authorization": "Bearer local-jwt", "X-Service-Token": ""},
        )
        assert res_empty_svc.status_code == 403
        assert res_empty_svc.json()["detail"]["code"] == "session_auth_required"

        # 3. Computer CLI token (lac1_* / lac1.) -> 403 session_auth_required
        for cli_tok in ("lac1_abc1234567890", "lac1.abc1234567890"):
            res_cli = client.get(ENDPOINT, headers={"Authorization": f"Bearer {cli_tok}"})
            assert res_cli.status_code == 403
            assert res_cli.json()["detail"]["code"] == "session_auth_required"

    def test_local_and_supabase_admin_vs_member_and_db_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_db = _FakeEncryptedStore()
        monkeypatch.setattr(dc_store, "get_db_connection", fake_db.make_get_db_connection())
        monkeypatch.setattr(
            dc_router,
            "preload_feishu_vault_pair",
            create_autospec(dc_snapshot.preload_feishu_vault_pair, return_value=(None, False)),
        )
        client = _make_client()

        # Local admin -> 200
        _patch_local_admin(monkeypatch, user_id="local-admin-1", is_admin=True)
        assert client.get(ENDPOINT, headers={"Authorization": "Bearer local-jwt"}).status_code == 200

        # Local member -> 403
        _patch_local_admin(monkeypatch, user_id="local-member-1", is_admin=False)
        assert client.get(ENDPOINT, headers={"Authorization": "Bearer local-jwt"}).status_code == 403

        # Local role DB error -> 503 fail-closed
        monkeypatch.setattr(
            dc_auth,
            "require_catalog_admin_async",
            create_autospec(
                dalton_credentials.require_catalog_admin_async,
                side_effect=HTTPException(status_code=503, detail="DB unavailable"),
            ),
        )
        assert client.get(ENDPOINT, headers={"Authorization": "Bearer local-jwt"}).status_code == 503

        # Supabase admin vs regular member
        monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
        monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-admin-1")

        class _SupaUser:
            def __init__(self, uid: str) -> None:
                self.user_id = uid

        monkeypatch.setattr(
            dc_auth,
            "verify_dalton_bearer",
            create_autospec(
                dalton_auth.verify_dalton_bearer,
                return_value=_SupaUser("pm-admin-1"),
            ),
        )
        assert client.get(ENDPOINT, headers={"Authorization": "Bearer supa-jwt"}).status_code == 200

        monkeypatch.setattr(
            dc_auth,
            "verify_dalton_bearer",
            create_autospec(
                dalton_auth.verify_dalton_bearer,
                return_value=_SupaUser("regular-member-2"),
            ),
        )
        assert client.get(ENDPOINT, headers={"Authorization": "Bearer supa-jwt"}).status_code == 403


class TestReadWriteValidationAndConflicts:
    def test_six_b1_fields_roundtrip_callbacks_zero_secret_echo_and_zero_vault_writes(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake_db = _FakeEncryptedStore()
        monkeypatch.setattr(dc_store, "get_db_connection", fake_db.make_get_db_connection())
        monkeypatch.setattr(
            dc_router,
            "preload_feishu_vault_pair",
            create_autospec(dc_snapshot.preload_feishu_vault_pair, return_value=(None, False)),
        )
        monkeypatch.setattr(
            dc_store,
            "query_feishu_vault_pair_strict",
            create_autospec(
                dc_snapshot.query_feishu_vault_pair_strict,
                return_value=(None, False, None),
            ),
        )
        monkeypatch.setattr(
            setup_apply,
            "refresh",
            create_autospec(setup_apply.refresh, return_value={}),
        )
        monkeypatch.setattr(
            setup_follow,
            "publish_changed",
            create_autospec(setup_follow.publish_changed, return_value=None),
        )
        monkeypatch.setenv("PUBLIC_APP_URL", "https://research.example.com")
        _patch_local_admin(monkeypatch)

        client = _make_client()

        with caplog.at_level(logging.DEBUG):
            get0 = client.get(ENDPOINT, headers={"Authorization": "Bearer local-jwt"})
            assert get0.status_code == 200
            data0 = get0.json()
            assert data0["revision"] == 0
            assert data0["redirect_uris"]["google"] == build_integration_redirect_uri("google")
            assert data0["redirect_uris"]["feishu"] == build_integration_redirect_uri("feishu")
            assert data0["redirect_uris"]["google"] == f"https://research.example.com{GOOGLE_OAUTH_CALLBACK_PATH}"
            assert data0["redirect_uris"]["feishu"] == f"https://research.example.com{FEISHU_OAUTH_CALLBACK_PATH}"
            assert data0["feishu_events_url"] == build_feishu_integration_events_url()
            assert data0["feishu_events_url"] == f"https://research.example.com{FEISHU_INTEGRATION_EVENTS_PATH}"
            assert data0["google_redirect_uri_eligible"] is True
            assert data0["google_redirect_uri_reason"] is None
            assert len(data0["fields"]) == 6

            # Write all 6 B1 credentials in one atomic request
            put1 = client.put(
                ENDPOINT,
                headers={"Authorization": "Bearer local-jwt"},
                json={
                    "expected_revision": 0,
                    "values": {
                        "DALTON_GOOGLE_OAUTH_CLIENT_ID": "g-cid-1",
                        "DALTON_GOOGLE_OAUTH_CLIENT_SECRET": FAKE_GOOGLE_SECRET,
                        "DALTON_FEISHU_APP_ID": "cli_feishu_1",
                        "DALTON_FEISHU_APP_SECRET": FAKE_FEISHU_SECRET,
                        "DALTON_FEISHU_ENCRYPT_KEY": FAKE_ENCRYPT_KEY,
                        "DALTON_FEISHU_VERIFICATION_TOKEN": FAKE_VERIFY_TOKEN,
                    },
                },
            )
            assert put1.status_code == 200
            assert put1.json()["revision"] == 1
            assert put1.json()["pending_apply"] is False
            assert put1.json()["warnings"] == []
            _assert_no_secrets_leaked(put1.text)

            # Read back: non-secrets echo value, secrets omit value & show configured=True
            get1 = client.get(ENDPOINT, headers={"Authorization": "Bearer local-jwt"})
            assert get1.status_code == 200
            _assert_no_secrets_leaked(get1.text)
            by_name = {f["name"]: f for f in get1.json()["fields"]}
            assert by_name["DALTON_GOOGLE_OAUTH_CLIENT_ID"]["value"] == "g-cid-1"
            assert by_name["DALTON_FEISHU_APP_ID"]["value"] == "cli_feishu_1"
            for secret_name in (
                "DALTON_GOOGLE_OAUTH_CLIENT_SECRET",
                "DALTON_FEISHU_APP_SECRET",
                "DALTON_FEISHU_ENCRYPT_KEY",
                "DALTON_FEISHU_VERIFICATION_TOKEN",
            ):
                assert by_name[secret_name]["configured"] is True
                assert by_name[secret_name]["source"] == "saved"
                assert "value" not in by_name[secret_name]

        _assert_no_secrets_leaked(caplog.text)
        # Confirm zero SQL touched user_vault_secrets
        assert all("user_vault_secrets" not in sql.lower() for sql in fake_db.executed_sql)

    def test_unknown_setting_half_group_and_sanitized_422_errors(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake_db = _FakeEncryptedStore()
        monkeypatch.setattr(dc_store, "get_db_connection", fake_db.make_get_db_connection())
        monkeypatch.setattr(
            dc_store,
            "query_feishu_vault_pair_strict",
            create_autospec(
                dc_snapshot.query_feishu_vault_pair_strict,
                return_value=(None, False, None),
            ),
        )
        _patch_local_admin(monkeypatch)
        client = _make_client()

        with caplog.at_level(logging.DEBUG):
            # 1. Unknown setting -> 422 unknown_setting masked as _unknown without echoing key or value
            r_unknown = client.put(
                ENDPOINT,
                headers={"Authorization": "Bearer local-jwt"},
                json={"expected_revision": 0, "values": {"OPENAI_API_KEY": FAKE_GOOGLE_SECRET}},
            )
            assert r_unknown.status_code == 422
            assert r_unknown.json() == {
                "code": "validation_error",
                "errors": [{"setting_name": "_unknown", "reason": "unknown_setting"}],
            }
            assert "OPENAI_API_KEY" not in r_unknown.text
            _assert_no_secrets_leaked(r_unknown.text)

            # 1b. Secret-like unknown key in values and at top level -> masked as _unknown
            secret_like_key = "test-secret-not-real-0009"
            r_unknown_val_key = client.put(
                ENDPOINT,
                headers={"Authorization": "Bearer local-jwt"},
                json={"expected_revision": 0, "values": {secret_like_key: "placeholder"}},
            )
            assert r_unknown_val_key.status_code == 422
            assert secret_like_key not in r_unknown_val_key.text
            assert r_unknown_val_key.json() == {
                "code": "validation_error",
                "errors": [{"setting_name": "_unknown", "reason": "unknown_setting"}],
            }

            r_unknown_top_key = client.put(
                ENDPOINT,
                headers={"Authorization": "Bearer local-jwt"},
                json={"expected_revision": 0, "values": {}, secret_like_key: "placeholder"},
            )
            assert r_unknown_top_key.status_code == 422
            assert secret_like_key not in r_unknown_top_key.text
            assert r_unknown_top_key.json() == {
                "code": "validation_error",
                "errors": [{"setting_name": "_unknown", "reason": "unknown_setting"}],
            }

            # 1c. Malformed JSON -> 422 with _body
            r_malformed = client.put(
                ENDPOINT,
                headers={
                    "Authorization": "Bearer local-jwt",
                    "Content-Type": "application/json",
                },
                content="{not-json",
            )
            assert r_malformed.status_code == 422
            assert r_malformed.json()["code"] == "validation_error"
            assert all(e["setting_name"] == "_body" for e in r_malformed.json()["errors"])

            # 2. Half pair for pair_required=True google_oauth -> 422 incomplete_credential_group
            r_half = client.put(
                ENDPOINT,
                headers={"Authorization": "Bearer local-jwt"},
                json={
                    "expected_revision": 0,
                    "values": {"DALTON_GOOGLE_OAUTH_CLIENT_SECRET": FAKE_GOOGLE_SECRET},
                },
            )
            assert r_half.status_code == 422
            assert r_half.json() == {
                "code": "validation_error",
                "errors": [
                    {
                        "setting_name": "DALTON_GOOGLE_OAUTH_CLIENT_ID",
                        "reason": "incomplete_credential_group",
                    }
                ],
            }
            _assert_no_secrets_leaked(r_half.text)

            # 3. Overlong & non-printable values -> 422 without raw input echo
            r_bad = client.put(
                ENDPOINT,
                headers={"Authorization": "Bearer local-jwt"},
                json={
                    "expected_revision": 0,
                    "values": {
                        "DALTON_FEISHU_ENCRYPT_KEY": FAKE_ENCRYPT_KEY + "x" * 5000,
                        "DALTON_FEISHU_VERIFICATION_TOKEN": f"{FAKE_VERIFY_TOKEN}\nnewline",
                    },
                },
            )
            assert r_bad.status_code == 422
            reasons = {e["setting_name"]: e["reason"] for e in r_bad.json()["errors"]}
            assert reasons == {
                "DALTON_FEISHU_ENCRYPT_KEY": "value_too_long",
                "DALTON_FEISHU_VERIFICATION_TOKEN": "non_printable",
            }
            _assert_no_secrets_leaked(r_bad.text)

            # 4. Extra top-level Pydantic field -> sanitized 422 with _unknown and zero raw input
            r_extra = client.put(
                ENDPOINT,
                headers={"Authorization": "Bearer local-jwt"},
                json={
                    "expected_revision": 0,
                    "values": {},
                    "unexpected_secret_field": FAKE_FEISHU_SECRET,
                },
            )
            assert r_extra.status_code == 422
            assert r_extra.json() == {
                "code": "validation_error",
                "errors": [{"setting_name": "_unknown", "reason": "unknown_setting"}],
            }
            assert "unexpected_secret_field" not in r_extra.text
            assert "input" not in r_extra.text
            _assert_no_secrets_leaked(r_extra.text)

            # 5. Internal whitespace in credential-group value -> 422 non_printable, revision unchanged
            r_ws = client.put(
                ENDPOINT,
                headers={"Authorization": "Bearer local-jwt"},
                json={
                    "expected_revision": 0,
                    "values": {
                        "DALTON_FEISHU_APP_ID": "cli_feishu_ws_1",
                        "DALTON_FEISHU_APP_SECRET": "abc def",
                    },
                },
            )
            assert r_ws.status_code == 422
            assert r_ws.json() == {
                "code": "validation_error",
                "errors": [{"setting_name": "DALTON_FEISHU_APP_SECRET", "reason": "non_printable"}],
            }
            assert "abc def" not in r_ws.text
            assert fake_db.revision == 0

        _assert_no_secrets_leaked(caplog.text)

    def test_tristate_boolean_write_true_false_and_delete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        temp_bool = DaltonDeploymentFieldSpec(
            name="DALTON_CAP_TEMP_TEST_FLAG",
            group="capabilities",
            provider="dalton",
            section="general",
            label_zh="临时布尔测试",
            label_en="Temp Bool Test",
            secret=False,
            credential_group=None,
            input_kind="boolean",
            batch=1,
        )
        monkeypatch.setattr(dc_catalog, "_ACTIVE_FIELD_SPECS", (*BATCH1_FIELD_SPECS, temp_bool))
        fake_db = _FakeEncryptedStore()
        monkeypatch.setattr(dc_store, "get_db_connection", fake_db.make_get_db_connection())
        monkeypatch.setattr(
            setup_apply,
            "refresh",
            create_autospec(setup_apply.refresh, return_value={}),
        )
        monkeypatch.setattr(
            setup_follow,
            "publish_changed",
            create_autospec(setup_follow.publish_changed, return_value=None),
        )
        _patch_local_admin(monkeypatch)
        client = _make_client()

        # Enable -> stores "true"
        r1 = client.put(
            ENDPOINT,
            headers={"Authorization": "Bearer local-jwt"},
            json={"expected_revision": 0, "values": {"DALTON_CAP_TEMP_TEST_FLAG": "true"}},
        )
        assert r1.status_code == 200
        assert fake_db.rows["DALTON_CAP_TEMP_TEST_FLAG"] == "true"

        # Disable -> stores explicit "false" (does NOT delete row)
        r2 = client.put(
            ENDPOINT,
            headers={"Authorization": "Bearer local-jwt"},
            json={"expected_revision": 1, "values": {"DALTON_CAP_TEMP_TEST_FLAG": "false"}},
        )
        assert r2.status_code == 200
        assert fake_db.rows["DALTON_CAP_TEMP_TEST_FLAG"] == "false"

        # Restore default -> null executes DELETE
        r3 = client.put(
            ENDPOINT,
            headers={"Authorization": "Bearer local-jwt"},
            json={"expected_revision": 2, "values": {"DALTON_CAP_TEMP_TEST_FLAG": None}},
        )
        assert r3.status_code == 200
        assert "DALTON_CAP_TEMP_TEST_FLAG" not in fake_db.rows
        assert [row[3] for row in fake_db.audit_rows] == ["set", "rotate", "clear"]

    def test_revision_conflict_env_locked_and_vault_readonly_or_error_vs_feishu_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_db = _FakeEncryptedStore(revision=4)
        monkeypatch.setattr(dc_store, "get_db_connection", fake_db.make_get_db_connection())
        monkeypatch.setattr(
            setup_apply,
            "refresh",
            create_autospec(setup_apply.refresh, return_value={}),
        )
        monkeypatch.setattr(
            setup_follow,
            "publish_changed",
            create_autospec(setup_follow.publish_changed, return_value=None),
        )
        _patch_local_admin(monkeypatch)
        client = _make_client()

        # Missing expected_revision -> 409 revision_conflict
        r_missing_rev = client.put(
            ENDPOINT,
            headers={"Authorization": "Bearer local-jwt"},
            json={"values": {"DALTON_FEISHU_VERIFICATION_TOKEN": FAKE_VERIFY_TOKEN}},
        )
        assert r_missing_rev.status_code == 409
        assert r_missing_rev.json()["code"] == "revision_conflict"
        assert r_missing_rev.json()["current_revision"] == 4

        # Stale expected_revision -> 409 revision_conflict
        r_stale_rev = client.put(
            ENDPOINT,
            headers={"Authorization": "Bearer local-jwt"},
            json={
                "expected_revision": 2,
                "values": {"DALTON_FEISHU_VERIFICATION_TOKEN": FAKE_VERIFY_TOKEN},
            },
        )
        assert r_stale_rev.status_code == 409
        assert r_stale_rev.json()["code"] == "revision_conflict"

        # env_locked: setting one key in google_oauth locks the whole group (409 env_locked)
        monkeypatch.setenv("DALTON_GOOGLE_OAUTH_CLIENT_ID", "env-g-id")
        monkeypatch.setattr(setup_env, "_at_start", None)
        r_env = client.put(
            ENDPOINT,
            headers={"Authorization": "Bearer local-jwt"},
            json={
                "expected_revision": 4,
                "values": {
                    "DALTON_GOOGLE_OAUTH_CLIENT_ID": "new-id",
                    "DALTON_GOOGLE_OAUTH_CLIENT_SECRET": FAKE_GOOGLE_SECRET,
                },
            },
        )
        assert r_env.status_code == 409
        assert r_env.json()["code"] == "env_locked"

        # vault_readonly: GET echoes App ID, hides secret; PUT feishu_app -> 409, PUT feishu_event -> 200
        vault_pair = {
            "DALTON_FEISHU_APP_ID": "cli_vault_readonly_id",
            "DALTON_FEISHU_APP_SECRET": FAKE_FEISHU_SECRET,
        }
        monkeypatch.setattr(
            dc_router,
            "preload_feishu_vault_pair",
            create_autospec(dc_snapshot.preload_feishu_vault_pair, return_value=(vault_pair, False)),
        )
        monkeypatch.setattr(
            dc_store,
            "query_feishu_vault_pair_strict",
            create_autospec(
                dc_snapshot.query_feishu_vault_pair_strict,
                return_value=(vault_pair, False, None),
            ),
        )
        g_ro = client.get(ENDPOINT, headers={"Authorization": "Bearer local-jwt"})
        assert g_ro.status_code == 200
        _assert_no_secrets_leaked(g_ro.text)
        ro_fields = {f["name"]: f for f in g_ro.json()["fields"]}
        assert ro_fields["DALTON_FEISHU_APP_ID"]["source"] == "vault_readonly"
        assert ro_fields["DALTON_FEISHU_APP_ID"]["readonly"] is True
        assert ro_fields["DALTON_FEISHU_APP_ID"]["value"] == "cli_vault_readonly_id"
        assert ro_fields["DALTON_FEISHU_APP_SECRET"]["source"] == "vault_readonly"
        assert "value" not in ro_fields["DALTON_FEISHU_APP_SECRET"]

        r_ro_put = client.put(
            ENDPOINT,
            headers={"Authorization": "Bearer local-jwt"},
            json={
                "expected_revision": 4,
                "values": {
                    "DALTON_FEISHU_APP_ID": "cli_overwrite",
                    "DALTON_FEISHU_APP_SECRET": FAKE_FEISHU_SECRET,
                },
            },
        )
        assert r_ro_put.status_code == 409
        assert r_ro_put.json()["code"] == "feishu_bot_readonly_in_batch1"

        # feishu_event remains writable under vault_readonly
        r_evt = client.put(
            ENDPOINT,
            headers={"Authorization": "Bearer local-jwt"},
            json={
                "expected_revision": 4,
                "values": {"DALTON_FEISHU_VERIFICATION_TOKEN": FAKE_VERIFY_TOKEN},
            },
        )
        assert r_evt.status_code == 200
        assert r_evt.json()["revision"] == 5

        # vault_error: GET hides App ID (None), PUT feishu_app -> 409, PUT feishu_event -> 200
        monkeypatch.setattr(
            dc_router,
            "preload_feishu_vault_pair",
            create_autospec(dc_snapshot.preload_feishu_vault_pair, return_value=(None, True)),
        )
        monkeypatch.setattr(
            dc_store,
            "query_feishu_vault_pair_strict",
            create_autospec(
                dc_snapshot.query_feishu_vault_pair_strict,
                return_value=(None, True, None),
            ),
        )
        g_err = client.get(ENDPOINT, headers={"Authorization": "Bearer local-jwt"})
        assert g_err.status_code == 200
        err_fields = {f["name"]: f for f in g_err.json()["fields"]}
        assert err_fields["DALTON_FEISHU_APP_ID"]["source"] == "vault_error"
        assert err_fields["DALTON_FEISHU_APP_ID"]["readonly"] is True
        assert err_fields["DALTON_FEISHU_APP_ID"]["value"] is None

        r_err_put = client.put(
            ENDPOINT,
            headers={"Authorization": "Bearer local-jwt"},
            json={
                "expected_revision": 5,
                "values": {
                    "DALTON_FEISHU_APP_ID": "cli_overwrite",
                    "DALTON_FEISHU_APP_SECRET": FAKE_FEISHU_SECRET,
                },
            },
        )
        assert r_err_put.status_code == 409
        assert r_err_put.json()["code"] == "feishu_bot_readonly_in_batch1"

        r_evt2 = client.put(
            ENDPOINT,
            headers={"Authorization": "Bearer local-jwt"},
            json={
                "expected_revision": 5,
                "values": {"DALTON_FEISHU_ENCRYPT_KEY": FAKE_ENCRYPT_KEY},
            },
        )
        assert r_evt2.status_code == 200
        assert r_evt2.json()["revision"] == 6

    def test_post_commit_refresh_failure_returns_200_pending_apply_and_still_publishes(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake_db = _FakeEncryptedStore(revision=0)
        monkeypatch.setattr(dc_store, "get_db_connection", fake_db.make_get_db_connection())
        refresh_mock = create_autospec(
            setup_apply.refresh,
            side_effect=RuntimeError(f"boom {FAKE_VERIFY_TOKEN}"),
        )
        publish_mock = create_autospec(setup_follow.publish_changed, return_value=None)
        monkeypatch.setattr(setup_apply, "refresh", refresh_mock)
        monkeypatch.setattr(setup_follow, "publish_changed", publish_mock)
        _patch_local_admin(monkeypatch)
        client = _make_client()

        with caplog.at_level(logging.DEBUG):
            res = client.put(
                ENDPOINT,
                headers={"Authorization": "Bearer local-jwt"},
                json={
                    "expected_revision": 0,
                    "values": {"DALTON_FEISHU_VERIFICATION_TOKEN": FAKE_VERIFY_TOKEN},
                },
            )
        assert res.status_code == 200
        assert res.json() == {
            "revision": 1,
            "changed": ["DALTON_FEISHU_VERIFICATION_TOKEN"],
            "pending_apply": True,
            "warnings": ["local_refresh_deferred"],
        }
        refresh_mock.assert_awaited_once()
        publish_mock.assert_awaited_once()
        _assert_no_secrets_leaked(res.text)
        _assert_no_secrets_leaked(caplog.text)


class TestRedirectUriRedactionAndVaultHooks:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://research.example.com", (True, None)),
            ("https://dalton.corp.example.co.uk/api", (True, None)),
            ("http://localhost:8088", (True, None)),
            ("http://localhost", (True, None)),
            ("http://127.0.0.1:8088", (True, None)),
            ("https://192.168.1.10", (False, "raw_ip_not_allowed")),
            ("http://10.0.0.5:8088", (False, "raw_ip_not_allowed")),
            ("https://[::1]:8088", (False, "raw_ip_not_allowed")),
            ("https://dalton.internal", (False, "private_tld_not_allowed")),
            ("https://dalton.local", (False, "private_tld_not_allowed")),
            ("http://research.example.com", (False, "https_required")),
            ("", (False, "https_required")),
        ],
    )
    def test_google_redirect_uri_eligible_reason_codes(
        self, url: str, expected: tuple[bool, str | None]
    ) -> None:
        assert google_redirect_uri_eligible(url) == expected

    def test_four_new_secrets_scrubbed_by_secret_redactor_and_redact_managed_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dc_snapshot.set_active_snapshot(
            dc_snapshot.build_snapshot(
                1,
                {
                    "DALTON_GOOGLE_OAUTH_CLIENT_SECRET": FAKE_GOOGLE_SECRET,
                    "DALTON_FEISHU_APP_SECRET": FAKE_FEISHU_SECRET,
                    "DALTON_FEISHU_ENCRYPT_KEY": FAKE_ENCRYPT_KEY,
                    "DALTON_FEISHU_VERIFICATION_TOKEN": FAKE_VERIFY_TOKEN,
                },
            )
        )
        payload = (
            f"g={FAKE_GOOGLE_SECRET} f={FAKE_FEISHU_SECRET} "
            f"e={FAKE_ENCRYPT_KEY} v={FAKE_VERIFY_TOKEN}"
        )

        # redact_managed_tokens scrubs all 4
        redacted_tokens = dalton_settings.redact_managed_tokens(payload)
        _assert_no_secrets_leaked(redacted_tokens)

        # SecretRedactor scrubs all 4 dynamically even if instantiated before snapshot update
        redactor = SecretRedactor()
        redacted_text = redactor.redact(payload)
        _assert_no_secrets_leaked(redacted_text)
        assert "[REDACTED:DALTON_GOOGLE_OAUTH_CLIENT_SECRET]" in redacted_text
        assert "[REDACTED:DALTON_FEISHU_APP_SECRET]" in redacted_text
        assert "[REDACTED:DALTON_FEISHU_ENCRYPT_KEY]" in redacted_text
        assert "[REDACTED:DALTON_FEISHU_VERIFICATION_TOKEN]" in redacted_text

        # Vault-only Feishu App Secret (Everflow prod fallback) is scrubbed by both redactors
        vault_only_secret = "test-secret-not-real-vault-only-0010"
        dc_snapshot.set_active_snapshot(
            dc_snapshot.build_snapshot(
                2,
                {},
                feishu_vault_pair={
                    "DALTON_FEISHU_APP_ID": "cli_vault_only_id",
                    "DALTON_FEISHU_APP_SECRET": vault_only_secret,
                },
            )
        )
        vault_payload = f"app=cli_vault_only_id sec={vault_only_secret}"
        redacted_vault_tokens = dalton_settings.redact_managed_tokens(vault_payload)
        assert vault_only_secret not in redacted_vault_tokens
        assert "cli_vault_only_id" in redacted_vault_tokens

        redacted_vault_text = redactor.redact(vault_payload)
        assert vault_only_secret not in redacted_vault_text
        assert "[REDACTED:DALTON_FEISHU_APP_SECRET]" in redacted_vault_text
        assert "cli_vault_only_id" in redacted_vault_text

    @pytest.mark.asyncio
    async def test_vault_invalidation_hook_refreshes_on_feishu_keys_and_ignores_errors(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(
            vault_invalidation,
            "_invalidate_mcp",
            create_autospec(vault_invalidation._invalidate_mcp, return_value=None),
        )
        monkeypatch.setattr(
            vault_invalidation,
            "_push_secrets",
            create_autospec(vault_invalidation._push_secrets, return_value=None),
        )
        orig_refresh = setup_apply.refresh
        orig_publish = setup_follow.publish_changed
        refresh_mock = create_autospec(orig_refresh, return_value={})
        publish_mock = create_autospec(orig_publish, return_value=None)
        monkeypatch.setattr(setup_apply, "refresh", refresh_mock)
        monkeypatch.setattr(setup_follow, "publish_changed", publish_mock)

        # Unrelated secret does not trigger setup_settings refresh
        await vault_invalidation.after_secret_change("u1", "SOME_OTHER_SECRET")
        refresh_mock.assert_not_awaited()
        publish_mock.assert_not_awaited()

        # Dedicated Feishu secret triggers refresh + publish_changed
        await vault_invalidation.after_secret_change("u1", "DALTON_FEISHU_APP_SECRET")
        refresh_mock.assert_awaited_once()
        publish_mock.assert_awaited_once()

        # Batch mutation triggers refresh + publish_changed; errors are logged without failing caller
        refresh_fail = create_autospec(
            orig_refresh,
            side_effect=RuntimeError(f"refresh failed near {FAKE_FEISHU_SECRET}"),
        )
        publish_mock2 = create_autospec(orig_publish, return_value=None)
        monkeypatch.setattr(setup_apply, "refresh", refresh_fail)
        monkeypatch.setattr(setup_follow, "publish_changed", publish_mock2)
        with caplog.at_level(logging.DEBUG):
            await vault_invalidation.after_secrets_changed(
                "u1", ["OTHER", "DALTON_FEISHU_APP_ID"]
            )
        refresh_fail.assert_awaited_once()
        publish_mock2.assert_awaited_once()
        _assert_no_secrets_leaked(caplog.text)
        assert dc_hooks.on_vault_secrets_changed is not None
