"""Unit tests for Standalone Core Batch 4b:

- Migration ``d048_invite_admin_tier`` offline SQL upgrade/downgrade.
- Tiered invites in ``local`` mode (admin/internal/external) + atomic single-use redemption.
- Unified admin/member callers via ``src.dalton.access_tiers`` in ``local`` vs Everflow mode.
- Core multi-user LLM model URL egress enforcement, DNS-rebinding multi-IP refusal,
  operator allowlist (with path-segment boundary), and admin team-model exemption.
- Static verification of ``tests/standalone/checks/b4_local_auth.py`` (stdlib + ``_lib`` only).
"""

from __future__ import annotations

import ast
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException

from src.dalton import access_tiers, invites
from src.dalton.database import members as members_db
from src.dalton.llm import egress_policy
from src.server.services.llm import egress

REPO_ROOT = Path(__file__).resolve().parents[3]


class _FakeAsyncCursor:
    """Minimal async cursor recording queries and serving canned rows."""

    def __init__(self, fetchone_results: list[object] | None = None):
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []
        self._fetchone_results = list(fetchone_results or [])

    async def execute(self, sql: str, params=None) -> None:
        self.executed.append((sql, tuple(params) if params is not None else None))

    async def fetchone(self):
        if self._fetchone_results:
            return self._fetchone_results.pop(0)
        return None


class _FakeAsyncConn:
    """Minimal async psycopg connection stub with transaction context manager."""

    def __init__(self, cursor: _FakeAsyncCursor):
        self._cursor = cursor

    @asynccontextmanager
    async def cursor(self, row_factory=None):
        yield self._cursor

    @asynccontextmanager
    async def transaction(self):
        yield self


# ---------------------------------------------------------------------------
# 1. Migration d048 offline SQL verification
# ---------------------------------------------------------------------------


def test_d048_invite_admin_tier_offline_sql_upgrade_and_downgrade():
    up = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "src/dalton/alembic.ini",
            "upgrade",
            "d047:d048",
            "--sql",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert "ck_dalton_invites_tier" in up.stdout
    assert "'admin'" in up.stdout
    assert "'internal'" in up.stdout
    assert "'external'" in up.stdout

    down = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "src/dalton/alembic.ini",
            "downgrade",
            "d048:d047",
            "--sql",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert "ck_dalton_invites_tier" in down.stdout
    assert "'internal', 'external'" in down.stdout


# ---------------------------------------------------------------------------
# 2. Tiered invites in local mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_mode_admin_creates_tiered_invites_without_supabase_config(monkeypatch):
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    monkeypatch.setattr(
        access_tiers,
        "get_user_tier",
        create_autospec(access_tiers.get_user_tier, return_value="admin"),
    )

    inserted: list[dict[str, object]] = []

    async def _fake_insert_invite(
        *,
        invite_id: str | None = None,
        token_sha256: str,
        created_by_user_id: str,
        expires_at: datetime,
        tier: str | None = None,
        conn=None,
    ):
        row = {
            "invite_id": invite_id or "inv-001",
            "token_sha256": token_sha256,
            "created_by_user_id": created_by_user_id,
            "tier": tier or "external",
            "created_at": datetime.now(timezone.utc),
            "expires_at": expires_at,
            "used_at": None,
            "used_by_email": None,
            "supabase_user_id": None,
            "revoked_at": None,
            "redeeming_username": None,
            "redeeming_at": None,
        }
        inserted.append(row)
        return row

    monkeypatch.setattr(
        members_db,
        "insert_invite",
        create_autospec(members_db.insert_invite, side_effect=_fake_insert_invite),
    )

    for requested_tier in ("admin", "internal", "external"):
        created = await invites.create_invite_for_pm("admin-1", tier=requested_tier)
        assert created["tier"] == requested_tier
        assert created["token"]
        assert created["url"].startswith("/invite#") or "/invite#" in created["url"]

    assert [row["tier"] for row in inserted] == ["admin", "internal", "external"]


@pytest.mark.asyncio
async def test_local_mode_internal_cannot_issue_admin_or_internal_invites(monkeypatch):
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setattr(
        access_tiers,
        "get_user_tier",
        create_autospec(access_tiers.get_user_tier, return_value="internal"),
    )

    with pytest.raises(HTTPException) as exc_admin:
        await invites.create_invite_for_pm("int-1", tier="admin")
    assert exc_admin.value.status_code == 403

    with pytest.raises(HTTPException) as exc_internal:
        await invites.create_invite_for_pm("int-1", tier="internal")
    assert exc_internal.value.status_code == 403

    with pytest.raises(HTTPException) as exc_bad:
        await invites.create_invite_for_pm("int-1", tier="superadmin")
    assert exc_bad.value.status_code == 400


@pytest.mark.asyncio
async def test_local_mode_redeem_invite_atomic_role_assignment_and_replay_rejection(monkeypatch):
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    now = datetime.now(timezone.utc)
    active_invite = {
        "invite_id": "inv-local-1",
        "token_sha256": invites.hash_invite_token("raw-valid-token"),
        "tier": "internal",
        "created_by_user_id": "admin-1",
        "expires_at": now + timedelta(days=7),
        "used_at": None,
        "revoked_at": None,
        "redeeming_at": None,
    }

    get_invite_mock = create_autospec(
        members_db.get_invite_by_token_hash, return_value=active_invite
    )
    monkeypatch.setattr(members_db, "get_invite_by_token_hash", get_invite_mock)
    monkeypatch.setattr(
        members_db,
        "lock_invite_by_token_hash",
        create_autospec(members_db.lock_invite_by_token_hash, return_value=active_invite),
    )
    mark_used_mock = create_autospec(members_db.mark_invite_used, return_value=True)
    monkeypatch.setattr(members_db, "mark_invite_used", mark_used_mock)

    from src.dalton.auth import passwords as local_passwords

    monkeypatch.setattr(
        local_passwords,
        "hash_password_async",
        create_autospec(local_passwords.hash_password_async, return_value="$argon2id$stub"),
    )

    # fetchone sequence inside _redeem_invite_token_local transaction:
    # 1. SELECT clock_timestamp() AS now
    # 2. SELECT 1 FROM dalton_local_credentials WHERE ... (None = available)
    # 3. SELECT 1 FROM users WHERE ... (None = available)
    cursor = _FakeAsyncCursor(fetchone_results=[{"now": now}, None, None])
    conn = _FakeAsyncConn(cursor)

    @asynccontextmanager
    async def _fake_conn():
        yield conn

    monkeypatch.setattr(invites, "get_db_connection", _fake_conn)

    res = await invites.redeem_invite_token(
        token="raw-valid-token",
        username="alice_dev",
        password="StrongPassword123",
    )
    assert res["ok"] is True
    assert res["username"] == "alice_dev"
    assert res["tier"] == "internal"
    mark_used_mock.assert_awaited_once()

    role_inserts = [
        (sql, params)
        for sql, params in cursor.executed
        if "INSERT INTO dalton_user_roles" in sql
    ]
    assert len(role_inserts) == 1
    assert role_inserts[0][1] is not None
    assert "internal" in role_inserts[0][1]

    # Replay: invite already has used_at set -> 400
    get_invite_mock.return_value = {**active_invite, "used_at": now}
    with pytest.raises(HTTPException) as exc_replay:
        await invites.redeem_invite_token(
            token="raw-valid-token",
            username="bob_dev",
            password="StrongPassword123",
        )
    assert exc_replay.value.status_code == 400


# ---------------------------------------------------------------------------
# 3. Admin callers unified via access_tiers (local vs Everflow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_tiers_admin_helpers_local_vs_everflow(monkeypatch):
    # Everflow (supabase): reads DALTON_PM_USER_IDS
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-1,pm-2")
    assert await access_tiers.is_admin_user("pm-1") is True
    assert await access_tiers.is_admin_user("other-user") is False
    assert access_tiers.is_admin_user_sync("pm-2") is True
    assert await access_tiers.list_admin_user_ids() == ["pm-1", "pm-2"]
    assert await access_tiers.get_primary_admin_user_id() == "pm-1"

    # Local mode: reads dalton_user_roles / dalton_credential_holder and ignores DALTON_PM_USER_IDS
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    from src.dalton.auth import roles as local_roles

    monkeypatch.setattr(
        local_roles,
        "get_user_role",
        create_autospec(
            local_roles.get_user_role,
            side_effect=lambda uid, **_kw: "admin" if uid == "local-admin-1" else "external",
        ),
    )
    monkeypatch.setattr(
        local_roles,
        "list_admin_user_ids",
        create_autospec(local_roles.list_admin_user_ids, return_value=["local-admin-1"]),
    )
    monkeypatch.setattr(
        local_roles,
        "get_credential_holder_id",
        create_autospec(local_roles.get_credential_holder_id, return_value="local-admin-1"),
    )

    assert await access_tiers.is_admin_user("local-admin-1") is True
    assert await access_tiers.is_admin_user("pm-1") is False
    assert await access_tiers.list_admin_user_ids() == ["local-admin-1"]
    assert await access_tiers.get_primary_admin_user_id() == "local-admin-1"
    assert await access_tiers.is_team_member_user("local-admin-1") is True


@pytest.mark.asyncio
async def test_item4_admin_callers_follow_access_tiers_in_local_mode(monkeypatch):
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv("DALTON_PM_USER_IDS", "legacy-pm-ignored")

    monkeypatch.setattr(
        access_tiers,
        "is_admin_user",
        create_autospec(
            access_tiers.is_admin_user,
            side_effect=lambda uid, **_kw: uid == "local-admin",
        ),
    )
    monkeypatch.setattr(
        access_tiers,
        "is_admin_user_sync",
        create_autospec(
            access_tiers.is_admin_user_sync,
            side_effect=lambda uid, **_kw: uid == "local-admin",
        ),
    )
    monkeypatch.setattr(
        access_tiers,
        "list_admin_user_ids",
        create_autospec(access_tiers.list_admin_user_ids, return_value=["local-admin"]),
    )
    monkeypatch.setattr(
        access_tiers,
        "get_primary_admin_user_id",
        create_autospec(access_tiers.get_primary_admin_user_id, return_value="local-admin"),
    )
    monkeypatch.setattr(
        access_tiers,
        "get_primary_admin_user_id_sync",
        create_autospec(access_tiers.get_primary_admin_user_id_sync, return_value="local-admin"),
    )

    # system_jobs
    from src.dalton.autonomy import system_jobs

    assert await system_jobs.is_pm_user_async("local-admin") is True
    assert await system_jobs.is_pm_user_async("legacy-pm-ignored") is False
    with pytest.raises(PermissionError):
        await system_jobs.require_pm_user_async("legacy-pm-ignored")

    # router._require_feishu_admin & _require_pm_user_async
    from src.dalton.app import router as dalton_router

    await dalton_router._require_pm_user_async("local-admin")
    await dalton_router._require_feishu_admin("local-admin")
    with pytest.raises(HTTPException) as feishu_exc:
        await dalton_router._require_feishu_admin("legacy-pm-ignored")
    assert feishu_exc.value.status_code == 403

    # internal_llm_router
    from src.dalton.app import internal_llm_router

    captured_uid: list[str] = []

    class _FakeResult:
        ok = True
        text = "ok"
        data = None
        model = "test-model"
        status = "done"
        error = None
        error_kind = None

    async def _fake_complete(**kwargs):
        captured_uid.append(str(kwargs.get("user_id") or ""))
        return _FakeResult()

    monkeypatch.setattr(
        internal_llm_router,
        "get_allowed_host_oauth_models",
        lambda: frozenset({"test-model"}),
    )
    monkeypatch.setattr(internal_llm_router, "dalton_complete", _fake_complete)
    llm_resp = await internal_llm_router.internal_llm_complete(
        internal_llm_router.InternalLLMCompleteRequest(
            model="test-model",
            user_prompt="hi",
        )
    )
    assert llm_resp.ok is True
    assert captured_uid == ["local-admin"]

    # model_availability_watch
    from src.dalton import credentials as dalton_credentials
    from src.dalton.services import model_availability_watch

    monkeypatch.setattr(
        dalton_credentials,
        "get_team_credential_holder_id",
        create_autospec(dalton_credentials.get_team_credential_holder_id, return_value="local-admin"),
    )
    assert model_availability_watch.resolve_credential_holder_user_id() == "local-admin"

    # openclaw_admin.probe_health
    from src.dalton.services import openclaw_admin

    monkeypatch.setattr(
        openclaw_admin,
        "get_startup_git_info",
        lambda: ("deadbeef", False),
    )
    health = await openclaw_admin.probe_health("local-admin")
    assert health["is_pm"] is True
    assert health["pm_user_ids_source"] == "dalton_user_roles"

    # channel_vault_credentials.load_dedicated_feishu_vault_credentials
    from src.dalton.services import channel_vault_credentials

    seen_vault_uids: list[str] = []

    async def _fake_secrets(uid: str, names=None):
        seen_vault_uids.append(uid)
        return {k: f"val-{k}" for k in (names or ())}

    creds = await channel_vault_credentials.load_dedicated_feishu_vault_credentials(
        get_secrets_fn=_fake_secrets,
    )
    assert isinstance(creds, dict) and creds
    assert seen_vault_uids == ["local-admin"]


# ---------------------------------------------------------------------------
# 4. Core multi-user model URL egress restriction
# ---------------------------------------------------------------------------


def test_egress_enforcement_gate_core_local_vs_everflow(monkeypatch):
    # Everflow (supabase + oss): egress remains disabled
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("DALTON_PROFILE", "everflow")
    assert egress_policy.is_core_llm_egress_enforced() is False
    assert egress.enforced() is False

    # Standalone Core single-user (oss + oss): egress disabled
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
    monkeypatch.setenv("DALTON_PROFILE", "core")
    assert egress_policy.is_core_llm_egress_enforced() is False
    assert egress.enforced() is False

    # Standalone Core multi-user (local + oss): egress ENFORCED
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    assert egress_policy.is_core_llm_egress_enforced() is True
    assert egress.enforced() is True


@pytest.mark.asyncio
async def test_egress_blocks_private_loopback_metadata_and_mixed_dns_rebinding(monkeypatch):
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.delenv("DALTON_OPERATOR_LLM_ENDPOINTS", raising=False)

    # Loopback, RFC1918 private, and cloud metadata IPs
    for bad_url in (
        "http://127.0.0.1:8000/v1",
        "http://10.0.0.5:8080/v1",
        "http://192.168.1.20/v1",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:8000/v1",
    ):
        reason = await egress.endpoint_refusal(bad_url)
        assert reason is not None, f"Expected refusal for {bad_url}"

    # DNS rebinding / multi-record guard: one public IP + one private IP -> REFUSED
    def _mixed_dns(host, port, *_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port)),
        ]

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        create_autospec(socket.getaddrinfo, side_effect=_mixed_dns),
    )
    assert await egress.endpoint_refusal("https://dual-record.example.com/v1") == egress.PRIVATE_ADDRESS


@pytest.mark.asyncio
async def test_egress_operator_allowlist_canonical_path_and_traversal_blocked(monkeypatch):
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv(
        "DALTON_OPERATOR_LLM_ENDPOINTS",
        "http://fake-llm:8099/v1, https://internal-gateway.local:8443/openai",
    )

    assert egress_policy.is_operator_allowlisted_endpoint("http://fake-llm:8099/v1") is True
    assert (
        egress_policy.is_operator_allowlisted_endpoint("http://fake-llm:8099/v1/chat/completions")
        is True
    )
    # Path-segment boundary prevents prefix confusion (`/v1evil` must NOT match `/v1`)
    assert egress_policy.is_operator_allowlisted_endpoint("http://fake-llm:8099/v1evil") is False
    # Canonical normalization blocks dot-segment traversal out of allowlisted path
    assert (
        egress_policy.is_operator_allowlisted_endpoint("http://fake-llm:8099/v1/../admin")
        is False
    )
    assert (
        egress_policy.is_operator_allowlisted_endpoint("http://fake-llm:8099/v1/%2e%2e/admin")
        is False
    )
    assert (
        egress_policy.is_operator_allowlisted_endpoint("http://fake-llm:8099/v1/./models")
        is True
    )
    # Duplicate slashes (`//`) are NOT collapsed — matches the exact path HTTPX sends on the wire
    assert (
        egress_policy.is_operator_allowlisted_endpoint("http://fake-llm:8099//v1/models")
        is False
    )
    assert (
        egress_policy.is_operator_allowlisted_endpoint("http://fake-llm:8099/v1//models")
        is False
    )
    assert (
        egress_policy.is_operator_allowlisted_endpoint("http://fake-llm:8099/v1//")
        is False
    )
    assert (
        egress_policy.is_operator_allowlisted_endpoint("https://internal-gateway.local:8443/openai//chat")
        is False
    )
    monkeypatch.setenv("DALTON_OPERATOR_LLM_ENDPOINTS", "http://fake-llm:8099/openai/v1")
    assert (
        egress_policy.is_operator_allowlisted_endpoint("http://fake-llm:8099/openai//v1/models")
        is False
    )
    assert (
        egress_policy.is_operator_allowlisted_endpoint("http://fake-llm:8099/openai/v1/models")
        is True
    )
    # Default port normalization works (https://...:443 == https://...)
    monkeypatch.setenv("DALTON_OPERATOR_LLM_ENDPOINTS", "https://gw.internal/v1")
    assert egress_policy.is_operator_allowlisted_endpoint("https://gw.internal:443/v1/models") is True
    # Wrong port / scheme / userinfo rejected
    monkeypatch.setenv("DALTON_OPERATOR_LLM_ENDPOINTS", "http://fake-llm:8099/v1")
    assert egress_policy.is_operator_allowlisted_endpoint("http://fake-llm:9000/v1") is False
    assert egress_policy.is_operator_allowlisted_endpoint("https://fake-llm:8099/v1") is False
    assert (
        egress_policy.is_operator_allowlisted_endpoint("http://attacker@fake-llm:8099/v1") is False
    )

    # endpoint_refusal returns None (allowed) for operator-allowlisted URL even on private IP
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        create_autospec(
            socket.getaddrinfo,
            side_effect=lambda host, port, *_args, **_kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.20.0.9", port))
            ],
        ),
    )
    assert await egress.endpoint_refusal("http://fake-llm:8099/v1/models") is None
    assert await egress.endpoint_refusal("http://fake-llm:8099//v1/models") == egress.PRIVATE_ADDRESS
    assert await egress.endpoint_refusal("http://fake-llm:8099/v1//models") == egress.PRIVATE_ADDRESS
    assert await egress.endpoint_refusal("http://fake-llm:8099/v1/../secret") == egress.PRIVATE_ADDRESS
    assert await egress.endpoint_refusal("http://fake-llm:8099/v1evil") == egress.PRIVATE_ADDRESS


@pytest.mark.asyncio
async def test_egress_exemption_is_address_based_not_admin_identity(monkeypatch):
    """Admins have NO identity bypass; only operator allowlist or saved team endpoints exempt private IPs."""
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.delenv("DALTON_OPERATOR_LLM_ENDPOINTS", raising=False)

    monkeypatch.setattr(
        egress_policy,
        "get_saved_team_endpoints",
        create_autospec(egress_policy.get_saved_team_endpoints, return_value={}),
    )

    from src.dalton import credentials as creds_mod
    from src.server.app import api_keys
    from src.server.services.llm import custom_catalog

    holder_mock = create_autospec(
        creds_mod._fetch_local_holder_id_sync,
        return_value="admin-1",
    )
    monkeypatch.setattr(creds_mod, "_fetch_local_holder_id_sync", holder_mock)

    # Both member AND admin are refused on non-allowlisted, non-team private/metadata URLs
    for uid in ("member-1", "admin-1"):
        with pytest.raises(HTTPException) as exc_key_test:
            await api_keys._user_endpoint_transport("http://127.0.0.1:8000/v1", user_id=uid)
        assert exc_key_test.value.status_code == 400

        custom_list = [
            {
                "name": "private-model",
                "provider": "openai",
                "parameters": {"base_url": "http://169.254.169.254/v1"},
            }
        ]
        with pytest.raises(HTTPException) as exc_catalog:
            await custom_catalog.refuse_custom_model_endpoints(custom_list, user_id=uid)
        assert exc_catalog.value.status_code == 400

    # Operator-allowlisted URL passes AND still returns a DNS-pinning transport (never None).
    # With team credential sharing active (#485), the holder can save an operator-allowlisted
    # custom model base_url, while a non-holder member is still refused 400 (stricter policy wins).
    monkeypatch.setenv("DALTON_OPERATOR_LLM_ENDPOINTS", "http://127.0.0.1:8099/v1")
    transport = await api_keys._user_endpoint_transport("http://127.0.0.1:8099/v1", user_id="admin-1")
    assert transport is not None
    await custom_catalog.refuse_custom_model_endpoints(
        [{"name": "ok-model", "provider": "openai", "parameters": {"base_url": "http://127.0.0.1:8099/v1"}}],
        user_id="admin-1",
    )
    with pytest.raises(HTTPException) as exc_member_shared:
        await custom_catalog.refuse_custom_model_endpoints(
            [{"name": "ok-model", "provider": "openai", "parameters": {"base_url": "http://127.0.0.1:8099/v1"}}],
            user_id="member-1",
        )
    assert exc_member_shared.value.status_code == 400
    assert "team credential sharing" in str(exc_member_shared.value.detail)

    # Without a shared team holder, any user can save an operator-allowlisted endpoint.
    holder_mock.return_value = None
    await custom_catalog.refuse_custom_model_endpoints(
        [{"name": "ok-model", "provider": "openai", "parameters": {"base_url": "http://127.0.0.1:8099/v1"}}],
        user_id="member-1",
    )


@pytest.mark.asyncio
async def test_clients_egress_pins_saved_team_address_class_and_disables_redirects(monkeypatch):
    """Saved team base_url locks its saved address class (public vs private) and always pins DNS."""
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.delenv("DALTON_OPERATOR_LLM_ENDPOINTS", raising=False)

    saved_private = {
        egress_policy.canonical_url_key("http://team-gw.internal:8000/v1"): "private",
        egress_policy.canonical_url_key("https://team-public.example.com/v1"): "public",
    }
    monkeypatch.setattr(
        egress_policy,
        "get_saved_team_endpoints",
        create_autospec(egress_policy.get_saved_team_endpoints, return_value=saved_private),
    )

    # DNS resolves team-gw.internal to private IP, team-public.example.com to public IP
    def _fake_dns(host, port, *_args, **_kwargs):
        if host == "team-gw.internal":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.10.0.5", port))]
        if host == "team-public.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    dns_mock = create_autospec(socket.getaddrinfo, side_effect=_fake_dns)
    monkeypatch.setattr(socket, "getaddrinfo", dns_mock)

    from src.server.services.llm import clients

    # 1. Saved private team endpoint passes validation AND still receives a DNS-pinning factory
    kwargs_priv = await clients._egress_kwargs(
        "team-model",
        "http://team-gw.internal:8000/v1",
        {},
        user_id="member-1",
        team_base_url=True,
    )
    assert "egress_transport" in kwargs_priv and callable(kwargs_priv["egress_transport"])

    # Verify policy routing locks each URL to its saved address class
    assert (
        egress_policy.ip_policy_for_request_url(
            "http://team-gw.internal:8000/v1/responses",
            saved_team_endpoints=saved_private,
        )
        == "private"
    )
    assert (
        egress_policy.ip_policy_for_request_url(
            "https://team-public.example.com/v1/responses",
            saved_team_endpoints=saved_private,
        )
        == "public"
    )
    # Path traversal out of saved team prefix falls back to strict "public"
    assert (
        egress_policy.ip_policy_for_request_url(
            "http://team-gw.internal:8000/v1/../admin",
            saved_team_endpoints=saved_private,
        )
        == "public"
    )

    # 2. If a team URL saved as "public" rebinds to a private IP, connect-time pinning refuses it!
    dns_mock.side_effect = lambda host, port, *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))
    ]
    reason_rebind, _ = await egress_policy.check_core_endpoint_refusal(
        "https://team-public.example.com/v1/responses",
        saved_team_endpoints=saved_private,
    )
    assert reason_rebind == egress.PRIVATE_ADDRESS

    # And at connect time, PolicyHeldTransport routes team-gw.internal to ip_policy="private"
    # which rejects a public IP (enforcing saved address-class lock).
    import httpx

    dns_mock.side_effect = lambda host, port, *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
    ]
    transport = egress.core_http_transport(saved_team_endpoints=saved_private)
    with pytest.raises(egress.EndpointRefused):
        await transport.handle_async_request(
            httpx.Request("POST", "http://team-gw.internal:8000/v1/responses")
        )
    await transport.aclose()

    # 3. Member-supplied parameters URL targeting metadata IP is refused (400)
    with pytest.raises(HTTPException) as exc_member_param:
        await clients._egress_kwargs(
            "team-model",
            "http://team-gw.internal:8000/v1",
            {"base_url": "http://169.254.169.254/latest/meta-data/"},
            user_id="member-1",
            team_base_url=True,
        )
    assert exc_member_param.value.status_code == 400

    # 4. Even platform/default model with no custom base_url gets egress_transport in local mode
    #    so LLM._hold_params_to_egress enforces follow_redirects=False on all model requests.
    kwargs_default = await clients._egress_kwargs("gpt-4o", None)
    assert "egress_transport" in kwargs_default and callable(kwargs_default["egress_transport"])


@pytest.mark.asyncio
async def test_egress_saved_team_class_overrides_operator_allowlist_and_reuses_policy_pools(
    monkeypatch,
):
    """Items 2, 3 & 10c:
    - Saved team address class (public or private) takes precedence over DALTON_OPERATOR_LLM_ENDPOINTS.
    - No in-process mirror exists for team URL classes; DB write failures raise loudly.
    - core_sdk_transport_factory reuses cached HeldTransport pools per (family, ip_policy).
    """
    import httpx

    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv(
        "DALTON_OPERATOR_LLM_ENDPOINTS",
        "https://team-public.example.com/v1,http://team-gw.internal:8000/v1,http://allow-only.internal:8099/v1",
    )

    # Item 3: no in-process mirror attribute
    assert not hasattr(egress_policy, "_LOCAL_TEAM_URL_CLASSES")
    assert not hasattr(egress_policy, "clear_local_team_url_classes")

    saved_team = {
        egress_policy.canonical_url_key("https://team-public.example.com/v1"): "public",
        egress_policy.canonical_url_key("http://team-gw.internal:8000/v1"): "private",
    }

    # Item 2: team URL saved as "public" stays "public" even when also in DALTON_OPERATOR_LLM_ENDPOINTS
    assert (
        egress_policy.ip_policy_for_request_url(
            "https://team-public.example.com/v1/responses",
            saved_team_endpoints=saved_team,
        )
        == "public"
    )
    # Team URL saved as "private" stays "private" even when also in DALTON_OPERATOR_LLM_ENDPOINTS
    assert (
        egress_policy.ip_policy_for_request_url(
            "http://team-gw.internal:8000/v1/responses",
            saved_team_endpoints=saved_team,
        )
        == "private"
    )
    # Allowlist relaxes ONLY URLs that have NO saved team record
    assert (
        egress_policy.ip_policy_for_request_url(
            "http://allow-only.internal:8099/v1/responses",
            saved_team_endpoints=saved_team,
        )
        == "any"
    )

    # DNS rebinding a "public" team URL (that is also allowlisted!) to a private IP is REFUSED
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        create_autospec(
            socket.getaddrinfo,
            side_effect=lambda host, port, *_a, **_kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", port))
            ],
        ),
    )
    reason_pub_rebind, _ = await egress_policy.check_core_endpoint_refusal(
        "https://team-public.example.com/v1/responses",
        saved_team_endpoints=saved_team,
    )
    assert reason_pub_rebind == egress.PRIVATE_ADDRESS

    # Connect-time transport also refuses the rebound "public" team URL despite allowlist overlap
    transport = egress.core_http_transport(saved_team_endpoints=saved_team)
    with pytest.raises(egress.EndpointRefused):
        await transport.handle_async_request(
            httpx.Request("POST", "https://team-public.example.com/v1/responses")
        )
    await transport.aclose()

    # Item 3: DB write failure in record_saved_team_base_url_classes raises instead of silently skipping
    from src.dalton import credentials as creds_mod

    monkeypatch.setattr(
        creds_mod,
        "get_team_credential_holder_id_async",
        create_autospec(creds_mod.get_team_credential_holder_id_async, return_value="holder-1"),
    )

    class _FailingCursor:
        async def execute(self, *_a, **_kw):
            raise RuntimeError("db write failed")

    class _FailingConn:
        @asynccontextmanager
        async def cursor(self, **_kw):
            yield _FailingCursor()

    with pytest.raises(RuntimeError, match="db write failed"):
        await egress_policy.record_saved_team_base_url_classes(
            {"https://team-public.example.com/v1": "public"},
            conn=_FailingConn(),
        )

    # Item 10c: core_sdk_transport_factory reuses cached per-(family, ip_policy) HeldTransport pools
    t1 = egress.core_sdk_transport_factory(saved_team)("openai")
    t2 = egress.core_sdk_transport_factory(saved_team)("openai")
    req_pub = httpx.Request("POST", "https://team-public.example.com/v1/responses")
    req_priv = httpx.Request("POST", "http://team-gw.internal:8000/v1/responses")
    req_any = httpx.Request("POST", "http://allow-only.internal:8099/v1/responses")
    assert t1._select(req_pub) is t2._select(req_pub)
    assert t1._select(req_priv) is t2._select(req_priv)
    assert t1._select(req_any) is t2._select(req_any)
    assert t1._select(req_pub) is not t1._select(req_priv)
    assert t1._select(req_priv) is not t1._select(req_any)


@pytest.mark.asyncio
async def test_supabase_invite_summary_omits_tier_while_local_includes_tier(monkeypatch):
    """Item 11: Everflow Supabase invite summaries preserve exact response shape without `tier`."""
    from src.dalton import invites as invites_mod

    row = {
        "invite_id": "inv-1",
        "tier": "external",
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
        "used_at": None,
        "revoked_at": None,
        "used_by_username": None,
        "used_by_email": None,
    }
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    supa_summary = invites_mod._format_invite_summary(row)
    assert "tier" not in supa_summary

    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    local_summary = invites_mod._format_invite_summary(row)
    assert local_summary["tier"] == "external"


@pytest.mark.asyncio
async def test_coverage_units_action_flags_match_write_permission_in_local_mode(monkeypatch):
    """Item 9: units_view action flags and coverage_units._require_ws_write share one owner-tier rule."""
    from src.dalton.autonomy import units_view
    from src.dalton.database import coverage_units

    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")

    assert coverage_units.is_coverage_multiuser_auth_enabled() is True
    assert units_view._is_multiuser_auth_enabled() is True

    # 1. PM viewing bound unit owned by an internal member:
    #    flags say can_unbind/can_archive == True AND write is allowed.
    ws_row = {"workspace_id": "ws-1", "user_id": "analyst-1", "status": "active", "is_home": False}
    classify_mock = create_autospec(access_tiers.classify_owner_tier, return_value="internal")
    monkeypatch.setattr(access_tiers, "classify_owner_tier", classify_mock)

    assert await coverage_units.can_write_bound_unit_workspace(ws_row, "admin-1", is_pm=True) is True
    flags_internal = units_view._compute_unit_action_flags(
        user_id="admin-1",
        lead_uid="analyst-1",
        created_by_uid="analyst-1",
        ws_id="ws-1",
        ws_active=True,
        ws_owner="analyst-1",
        readable=True,
        web_level="ro",
        is_pm=True,
        workspace_owner_tier="internal",
    )
    assert flags_internal == {"can_bind": False, "can_unbind": True, "can_archive": True}
    await coverage_units._require_ws_write(ws_row, "admin-1", is_pm=True)

    # 2. Member is demoted to external: BOTH action flags become False AND write is refused!
    classify_mock.return_value = "external"
    assert await coverage_units.can_write_bound_unit_workspace(ws_row, "admin-1", is_pm=True) is False
    flags_demoted = units_view._compute_unit_action_flags(
        user_id="admin-1",
        lead_uid="analyst-1",
        created_by_uid="analyst-1",
        ws_id="ws-1",
        ws_active=True,
        ws_owner="analyst-1",
        readable=True,
        web_level="rw",
        is_pm=True,
        workspace_owner_tier="external",
    )
    assert flags_demoted == {"can_bind": False, "can_unbind": False, "can_archive": False}
    with pytest.raises(coverage_units._WorkspaceAccessError) as exc_ext:
        await coverage_units._require_ws_write(ws_row, "admin-1", is_pm=True)
    assert "belongs to another user" in str(exc_ext.value)

    # 3. End-to-end build_units_view with conn forwarding (Items 7 & 9):
    #    admin viewing a bound unit whose owner was demoted to external sees can_unbind/can_archive=False
    #    and reuses the caller's conn without opening extra connections.
    from src.dalton.auth import roles as local_roles
    from src.dalton.database import sector_members

    class _BoardCursor:
        def __init__(self):
            self.calls = 0

        async def execute(self, sql, params=None):
            self.calls += 1

        async def fetchall(self):
            if self.calls == 1:
                return [
                    {
                        "unit_id": "unit-1",
                        "kind": "company",
                        "owner_user_id": "analyst-1",
                        "created_by_user_id": "analyst-1",
                        "lead_analyst_user_id": "analyst-1",
                        "slug": "aapl",
                        "display_name": "Apple",
                        "workspace_id": "ws-1",
                        "ticker": "AAPL",
                        "workspace_owner_id": "analyst-1",
                        "workspace_status": "active",
                        "workspace_config": {},
                    }
                ]
            return []

    sentinel_conn = _FakeAsyncConn(_BoardCursor())
    seen_conns: list[object] = []

    async def _fake_get_role(uid, *, conn=None):
        seen_conns.append(conn)
        return "admin" if uid == "admin-1" else "external"

    async def _fake_roles_batch(uids, *, conn=None):
        seen_conns.append(conn)
        return {u: ("admin" if u == "admin-1" else "external") for u in uids}

    monkeypatch.setattr(local_roles, "get_user_role", create_autospec(local_roles.get_user_role, side_effect=_fake_get_role))
    monkeypatch.setattr(local_roles, "get_user_roles_batch", create_autospec(local_roles.get_user_roles_batch, side_effect=_fake_roles_batch))
    monkeypatch.setattr(
        units_view,
        "_load_colleague_workspace_state",
        create_autospec(
            units_view._load_colleague_workspace_state,
            return_value=({"ws-1": (True, False)}, {"ws-1": "rw"}, []),
        ),
    )
    monkeypatch.setattr(
        sector_members,
        "fetch_members_for_sectors",
        create_autospec(sector_members.fetch_members_for_sectors, return_value=[]),
    )

    view_rows = await units_view.build_units_view("admin-1", {}, conn=sentinel_conn)
    assert len(view_rows) == 1
    assert view_rows[0]["can_unbind"] is False
    assert view_rows[0]["can_archive"] is False
    assert seen_conns and all(c is sentinel_conn for c in seen_conns)


@pytest.mark.asyncio
async def test_sharing_list_team_workspaces_forwards_conn_and_batches_local_roles_async(monkeypatch):
    """Items 7 & 8: list_team_workspaces forwards caller's conn and batch-loads roles asynchronously (no sync DB calls)."""
    from src.dalton import sharing as sharing_mod
    from src.dalton.auth import roles as local_roles

    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")

    # Guard: sync role fetcher must NEVER be called on the async list path!
    sync_forbidden = create_autospec(
        access_tiers._fetch_local_role_sync,
        side_effect=AssertionError("sync _fetch_local_role_sync must not run on async list path"),
    )
    monkeypatch.setattr(access_tiers, "_fetch_local_role_sync", sync_forbidden)

    ws_rows = [
        {
            "workspace_id": "ws-int",
            "user_id": "u-internal",
            "name": "Internal WS",
            "description": "",
            "status": "active",
            "config": {},
            "created_at": None,
            "updated_at": None,
            "last_activity_at": None,
            "owner_email": "int@team.local",
            "owner_name": "Internal",
            "owner_avatar_url": None,
            "web_access": "ro",
            "cos_access": "rw",
        },
        {
            "workspace_id": "ws-ext",
            "user_id": "u-external",
            "name": "External WS",
            "description": "",
            "status": "active",
            "config": {},
            "created_at": None,
            "updated_at": None,
            "last_activity_at": None,
            "owner_email": "ext@team.local",
            "owner_name": "External",
            "owner_avatar_url": None,
            "web_access": "ro",
            "cos_access": "rw",
        },
    ]

    class _ListCursor:
        async def execute(self, sql, params=None):
            return None

        async def fetchall(self):
            return list(ws_rows)

    sentinel_conn = _FakeAsyncConn(_ListCursor())
    forwarded_conns: dict[str, object] = {}

    async def _fake_req_reader(uid, *, conn=None):
        forwarded_conns["require_team_reader"] = conn
        return uid

    async def _fake_get_tier(uid, *, conn=None):
        forwarded_conns["get_user_tier"] = conn
        return "admin"

    async def _fake_emails(*, bypass_cache=False, conn=None):
        forwarded_conns["get_team_member_emails"] = conn
        return {"int@team.local", "ext@team.local"}

    async def _fake_is_member(email, *, bypass_cache=False, cache=None, conn=None):
        forwarded_conns["is_team_member"] = conn
        return True

    batch_calls: list[tuple[tuple[str, ...], object]] = []

    async def _fake_batch_roles(uids, *, conn=None):
        batch_calls.append((tuple(sorted(uids)), conn))
        return {"u-internal": "internal", "u-external": "external"}

    monkeypatch.setattr(sharing_mod, "require_team_reader", create_autospec(sharing_mod.require_team_reader, side_effect=_fake_req_reader))
    monkeypatch.setattr(sharing_mod, "get_user_tier", create_autospec(sharing_mod.get_user_tier, side_effect=_fake_get_tier))
    monkeypatch.setattr(sharing_mod, "get_team_member_emails", create_autospec(sharing_mod.get_team_member_emails, side_effect=_fake_emails))
    monkeypatch.setattr(sharing_mod, "is_team_member", create_autospec(sharing_mod.is_team_member, side_effect=_fake_is_member))
    monkeypatch.setattr(local_roles, "get_user_roles_batch", create_autospec(local_roles.get_user_roles_batch, side_effect=_fake_batch_roles))

    # Guard: get_db_connection must reuse sentinel_conn (not open a new pool connection)
    @asynccontextmanager
    async def _fake_get_db(conn=None):
        assert conn is sentinel_conn
        yield conn

    monkeypatch.setattr(sharing_mod, "get_db_connection", _fake_get_db)

    members = await sharing_mod.list_team_workspaces("admin-1", conn=sentinel_conn)
    assert len(members) == 2
    by_uid = {m["user_id"]: m for m in members}
    assert by_uid["u-internal"]["workspaces"][0]["cos_access"] == "rw"
    assert by_uid["u-external"]["workspaces"][0]["cos_access"] == "hidden"

    # Exactly ONE batch async role query on sentinel_conn, ZERO sync queries
    assert batch_calls == [(("u-external", "u-internal"), sentinel_conn)]
    sync_forbidden.assert_not_called()
    assert forwarded_conns == {
        "require_team_reader": sentinel_conn,
        "get_user_tier": sentinel_conn,
        "get_team_member_emails": sentinel_conn,
        "is_team_member": sentinel_conn,
    }


def test_d048_downgrade_revokes_pending_admin_and_preserves_historical_audit():
    """Item 9: d048 downgrade SQL revokes unused admin invites and keeps historical admin rows."""
    mig_path = REPO_ROOT / "src" / "dalton" / "migrations" / "versions" / "d048_invite_admin_tier.py"
    src = mig_path.read_text(encoding="utf-8")
    assert "SET revoked_at = NOW()" in src
    assert "tier = 'admin'" in src
    assert "used_at IS NULL" in src
    assert "revoked_at IS NULL" in src
    assert "used_at IS NOT NULL OR revoked_at IS NOT NULL" in src
    assert "SET tier = 'internal'" not in src


# ---------------------------------------------------------------------------
# 5. CI check script static contract
# ---------------------------------------------------------------------------


def test_b4_local_auth_check_script_header_and_stdlib_only():
    script_path = REPO_ROOT / "tests" / "standalone" / "checks" / "b4_local_auth.py"
    source = script_path.read_text(encoding="utf-8")
    first_lines = source.splitlines()[:25]
    assert any(line.strip() == "# auth: local" for line in first_lines)
    assert "hasattr(" not in source, "Migration head check must not skip via hasattr"
    assert "/health" not in source, "Edge Caddy blocks /health with 404; poll bootstrap/status instead"

    tree = ast.parse(source, filename=str(script_path))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    forbidden = {"httpx", "psycopg", "psycopg2", "asyncpg", "aiohttp", "requests", "websockets"}
    assert imported_roots.isdisjoint(forbidden), f"Forbidden imports found: {imported_roots & forbidden}"
    assert "_lib" in imported_roots
