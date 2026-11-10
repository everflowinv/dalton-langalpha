"""Unit tests for AU1 Batch 1: company identity, external auth refusal, scope classification, and read-only endpoints."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
import uuid

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketException
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection, AsyncCursor
import pytest

from src.dalton import access_tiers, auth as dalton_auth, sharing as dalton_sharing
from src.dalton.app import router as dalton_router_mod
from src.dalton.autonomy.classification import (
    AUTOMATION_SCOPES,
    RESEARCH_TRACKING_KINDS,
    SCOPE_TRIM_WHITESPACE,
    build_research_tracking_sql_predicate,
    build_scope_case_sql,
    classify_automation_scope,
    needs_workspace_tracking_lookup,
    normalize_dalton_kind,
    normalize_scope_text,
)
from src.dalton.autonomy import schedule as schedule_mod
from src.dalton.budget.service import resolve_user_budget_config
from src.dalton.company_identity import (
    COMPANY_SYSTEM_EMAIL,
    COMPANY_SYSTEM_USER_ID,
    ensure_company_system_identity,
    ensure_company_system_identity_sync,
    is_company_system_user,
    reject_company_system_auth,
    resolve_company_budget_seed,
)
from src.dalton.database import budget as budget_db
from src.dalton.database import company_automations as company_auto_db
from src.dalton.notify import dispatch as notify_dispatch
from src.dalton.services import company_automations as company_auto_svc
from src.dalton.settings import DaltonBudgetDefaults
from src.server.app import automations as automations_app
from src.server.auth import jwt_bearer, oauth_resource, pats, ws_auth
from src.server.database import automation as auto_db
from src.server.database import principal_reach
from src.server.database import user as user_db
from src.server.database.home_workspace import get_home_workspace_id
from src.server.dependencies import principal as principal_mod
from src.server.dependencies.principal import Principal, get_principal
from src.server.models.automation import (
    AutomationCreate,
    AutomationUpdate,
)
from src.server.services import cli_tokens
from src.server.services.automations import lifecycle as lifecycle_mod
from src.server.utils import api as api_utils

D035_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "dalton"
    / "migrations"
    / "versions"
    / "d035_company_automations.py"
)

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
INTERNAL_UID = "user-internal-001"
ADMIN_UID = "user-admin-001"
EXTERNAL_UID = "user-external-001"
OTHER_UID = "user-other-002"
COMPUTER_A = "11111111-1111-4111-8111-111111111111"
COMPUTER_B = "22222222-2222-4222-8222-222222222222"
WS_ON_COMP_A = "33333333-3333-4333-8333-333333333333"
WS_ON_COMP_B = "44444444-4444-4444-8444-444444444444"
COMPANY_WS = "55555555-5555-4555-8555-555555555555"
AUTO_PERSONAL_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
AUTO_TRACKING_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
AUTO_COMPANY_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _sample_automation_row(
    *,
    automation_id: str = AUTO_PERSONAL_ID,
    user_id: str = INTERNAL_UID,
    name: str = "Daily Notes",
    scope: str = "personal",
    workspace_id: str | None = WS_ON_COMP_A,
    notify_storage_mode: str = "legacy",
    notify_enabled: bool = True,
    notify_routes: list[str] | None = None,
    config_version: int = 1,
    active_version: int = 1,
    created_by: str | None = INTERNAL_UID,
    updated_by: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "automation_id": automation_id,
        "user_id": user_id,
        "name": name,
        "file_name": "daily-notes.json",
        "description": "Test automation",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "trigger_config": {},
        "next_run_at": NOW,
        "last_run_at": None,
        "instruction": "Summarize updates",
        "workspace_id": workspace_id,
        "llm_model": None,
        "additional_context": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "status": "active",
        "max_failures": 3,
        "failure_count": 0,
        "disable_reason": None,
        "delivery_config": {},
        "metadata": metadata if metadata is not None else {},
        "scope": scope,
        "notify_enabled": notify_enabled,
        "notify_routes": notify_routes,
        "notify_storage_mode": notify_storage_mode,
        "config_version": config_version,
        "active_version": active_version,
        "created_by": created_by,
        "updated_by": updated_by,
        "deleted_at": None,
        "created_at": NOW,
        "updated_at": NOW,
        "last_execution": None,
    }


def _make_spec_async_conn(
    *,
    rowcount: int = 1,
    fetchone_rows: list[Any] | None = None,
) -> tuple[Any, list[tuple[str, Any]]]:
    executed: list[tuple[str, Any]] = []
    queue = list(fetchone_rows or [])
    conn = create_autospec(AsyncConnection, instance=True)
    cur = create_autospec(AsyncCursor, instance=True)
    cur.rowcount = rowcount

    async def _execute(sql: str, params: Any = None, *args: Any, **kwargs: Any) -> Any:
        executed.append((sql, params))
        return None

    async def _fetchone() -> Any:
        return queue.pop(0) if queue else None

    cur.execute.side_effect = _execute
    cur.fetchone.side_effect = _fetchone

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cur

    @asynccontextmanager
    async def _tx_cm(*args: Any, **kwargs: Any):
        yield conn

    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm
    return conn, executed


# ---------------------------------------------------------------------------
# 1. Migration contract & helper signature checks
# ---------------------------------------------------------------------------


def test_d035_migration_revision_and_safety_contracts() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    import src.dalton.migrations.versions.d035_company_automations as d035

    assert d035.revision == "d035"
    assert d035.down_revision == "d031"
    assert d035.branch_labels is None
    assert d035.depends_on is None

    dalton_root = D035_PATH.parents[2]
    alembic_cfg = Config(str(dalton_root / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(dalton_root / "migrations"))
    script_dir = ScriptDirectory.from_config(alembic_cfg)
    assert script_dir.get_heads() == ["d035"]
    assert script_dir.get_revision("d035").down_revision == "d031"
    assert script_dir.get_revision("d031").down_revision == "d030"
    assert [p.name for p in D035_PATH.parent.glob("d031*.py")] == [
        "d031_channel_session_selection.py"
    ]

    source = D035_PATH.read_text(encoding="utf-8")
    assert "Revises: d031" in source
    assert "d001..d031" in source
    for forbidden_rev in ("d032", "d033", "d034"):
        assert forbidden_rev not in source

    for upstream_tbl in ("public.users", "public.workspaces", "public.automations"):
        assert upstream_tbl in source

    for created_tbl in (
        "dalton_internal_feishu_groups",
        "dalton_notify_storage_state",
        "dalton_company_migration_batches",
    ):
        assert created_tbl in source

    assert "automations_company_owner_check" in source
    assert "automations_company_notify_mode_check" in source
    assert "scope <> 'company' OR notify_storage_mode = 'task_row'" in source
    assert "WHERE deleted_at IS NULL" in source
    assert "build_research_tracking_sql_predicate" in source
    assert "_assert_safe_downgrade" in source
    assert "public.computers" in source
    assert "pg_constraint" in source


ALLOWED_SCOPES_EXPECTED = ("personal", "research_tracking", "company")


def test_modified_helper_signatures() -> None:
    assert AUTOMATION_SCOPES == ALLOWED_SCOPES_EXPECTED

    create_sig = inspect.signature(auto_db.create_automation)
    assert "scope" in create_sig.parameters
    assert create_sig.parameters["scope"].default is None
    assert "created_by" in create_sig.parameters
    assert create_sig.parameters["created_by"].default is None

    list_route_sig = inspect.signature(automations_app.list_automations)
    assert "scope" in list_route_sig.parameters

    history_route_sig = inspect.signature(automations_app.list_automation_history)
    assert list(history_route_sig.parameters) == [
        "automation_id",
        "principal",
        "limit",
        "offset",
    ]

    classify_sig = inspect.signature(classify_automation_scope)
    assert "row" in classify_sig.parameters
    assert "workspace" in classify_sig.parameters


# ---------------------------------------------------------------------------
# 2. Company identity, budget defaults, and tier/sharing isolation
# ---------------------------------------------------------------------------


def test_company_budget_seed_reads_config_without_hardcoding() -> None:
    custom_cfg = DaltonBudgetDefaults(
        default_daily_cap_usd=37.5,
        default_task_cap_usd=4.25,
        timezone="America/New_York",
    )
    seed, valid = resolve_company_budget_seed(custom_cfg)
    assert valid is True
    assert seed is not None
    assert seed.daily_cap_usd == Decimal("37.5")
    assert seed.default_task_cap_usd == Decimal("4.25")
    assert seed.timezone == "America/New_York"

    # Unset default_daily_cap_usd leaves DB unseeded so resolve_user_budget_config fails closed.
    none_cfg = DaltonBudgetDefaults(
        default_daily_cap_usd=None,
        default_task_cap_usd=5.0,
        timezone="Asia/Shanghai",
    )
    seed_none, valid_none = resolve_company_budget_seed(none_cfg)
    assert valid_none is True
    assert seed_none is None

    # Invalid negative value fails closed to $0 rather than granting open access.
    class _CorruptBudget:
        default_daily_cap_usd = -10
        default_task_cap_usd = None
        timezone = "Asia/Shanghai"

    seed_bad, valid_bad = resolve_company_budget_seed(_CorruptBudget())
    assert valid_bad is False
    assert seed_bad is not None
    assert seed_bad.daily_cap_usd == Decimal("0")


@pytest.mark.asyncio
async def test_ensure_company_identity_idempotent_and_unconfigured_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, executed = _make_spec_async_conn(rowcount=1)
    cfg = DaltonBudgetDefaults(
        default_daily_cap_usd=42.0,
        default_task_cap_usd=6.0,
        timezone="Asia/Shanghai",
    )
    res1 = await ensure_company_system_identity(conn=conn, budget_settings=cfg)
    assert res1["user_id"] == COMPANY_SYSTEM_USER_ID
    assert len(executed) == 2
    assert "ON CONFLICT (user_id) DO NOTHING" in executed[0][0]
    assert executed[0][1][0] == COMPANY_SYSTEM_USER_ID
    assert executed[0][1][1] == COMPANY_SYSTEM_EMAIL
    assert "ON CONFLICT (user_id) DO NOTHING" in executed[1][0]
    assert executed[1][1] == (
        COMPANY_SYSTEM_USER_ID,
        Decimal("42.0"),
        Decimal("6.0"),
        "Asia/Shanghai",
    )

    # Second call with rowcount=0 (already present) reports no overwrite.
    conn2, executed2 = _make_spec_async_conn(rowcount=0)
    res2 = await ensure_company_system_identity(
        conn=conn2,
        budget_settings=DaltonBudgetDefaults(
            default_daily_cap_usd=999.0,
            default_task_cap_usd=50.0,
            timezone="UTC",
        ),
    )
    assert res2["user_inserted"] is False
    assert res2["budget_inserted"] is False
    assert "DO UPDATE" not in executed2[1][0]

    # Sync path shares the same ON CONFLICT DO NOTHING behavior.
    class _SyncBind:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        def exec_driver_sql(self, sql: str, params: Any = None) -> Any:
            self.calls.append((sql, params))

            class _Res:
                rowcount = 0

            return _Res()

    sync_bind = _SyncBind()
    sync_res = ensure_company_system_identity_sync(sync_bind, budget_settings=cfg)
    assert sync_res["budget_inserted"] is False
    assert len(sync_bind.calls) == 2

    # When default_daily_cap_usd is None and no DB row exists, resolve_user_budget_config fails closed ($0).
    mock_fetch_budget = create_autospec(budget_db.fetch_budget_config, return_value=None)
    monkeypatch.setattr("src.dalton.budget.service.fetch_budget_config", mock_fetch_budget)

    class _FakeDaltonSettings:
        budget = DaltonBudgetDefaults(
            default_daily_cap_usd=None,
            default_task_cap_usd=5.0,
            timezone="Asia/Shanghai",
        )

    monkeypatch.setattr(
        "src.dalton.budget.service.get_dalton_settings",
        lambda: _FakeDaltonSettings(),
    )
    resolved = await resolve_user_budget_config(conn, COMPANY_SYSTEM_USER_ID)
    assert resolved["daily_cap_usd"] == Decimal("0")
    assert resolved["configured"] is False


@pytest.mark.asyncio
async def test_company_tier_and_no_workspace_sharing_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert is_company_system_user(COMPANY_SYSTEM_USER_ID) is True
    assert await access_tiers.get_user_tier(COMPANY_SYSTEM_USER_ID) == "company"
    assert await access_tiers.classify_owner_tier(COMPANY_SYSTEM_USER_ID) == "company"
    assert (
        dalton_sharing._classify_owner_row_tier(
            COMPANY_SYSTEM_USER_ID, COMPANY_SYSTEM_EMAIL
        )
        == "company"
    )
    # is_internal_or_admin remains strictly human internal/admin only.
    assert await access_tiers.is_internal_or_admin(COMPANY_SYSTEM_USER_ID) is False

    monkeypatch.setattr(
        access_tiers,
        "is_invite_pm",
        create_autospec(access_tiers.is_invite_pm, side_effect=lambda uid: uid == ADMIN_UID),
    )
    monkeypatch.setattr(
        access_tiers,
        "get_dalton_allowed_emails",
        create_autospec(
            access_tiers.get_dalton_allowed_emails,
            return_value=frozenset({"internal@dalton.test", "admin@dalton.test"}),
        ),
    )

    async def _fake_get_user(uid: str) -> dict[str, Any] | None:
        roster = {
            INTERNAL_UID: {"user_id": INTERNAL_UID, "email": "internal@dalton.test"},
            ADMIN_UID: {"user_id": ADMIN_UID, "email": "admin@dalton.test"},
            EXTERNAL_UID: {"user_id": EXTERNAL_UID, "email": "guest@client.test"},
            COMPANY_SYSTEM_USER_ID: {
                "user_id": COMPANY_SYSTEM_USER_ID,
                "email": COMPANY_SYSTEM_EMAIL,
            },
        }
        return roster.get(uid)

    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(user_db.get_user, side_effect=_fake_get_user),
    )
    assert await access_tiers.is_internal_or_admin(ADMIN_UID) is True
    assert await access_tiers.is_internal_or_admin(INTERNAL_UID) is True
    assert await access_tiers.is_internal_or_admin(EXTERNAL_UID) is False
    assert await access_tiers.is_internal_or_admin(COMPANY_SYSTEM_USER_ID) is False

    company_ws = {
        "workspace_id": COMPANY_WS,
        "user_id": COMPANY_SYSTEM_USER_ID,
        "status": "running",
    }
    assert dalton_sharing.can_edit_workspace_sharing(company_ws, ADMIN_UID) is False
    assert (
        dalton_sharing.can_edit_workspace_sharing(
            {"workspace_id": WS_ON_COMP_A, "user_id": INTERNAL_UID, "status": "running"},
            COMPANY_SYSTEM_USER_ID,
        )
        is False
    )

    monkeypatch.setattr(
        dalton_sharing,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_sharing.is_dalton_supabase_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "is_team_member",
        create_autospec(
            dalton_sharing.is_team_member,
            side_effect=lambda email: email in {"internal@dalton.test", "admin@dalton.test"},
        ),
    )
    with pytest.raises(HTTPException) as exc_internal:
        await dalton_sharing.require_workspace_access(company_ws, INTERNAL_UID)
    assert exc_internal.value.status_code == 403

    with pytest.raises(HTTPException) as exc_company_caller:
        await dalton_sharing.require_workspace_access(
            {"workspace_id": WS_ON_COMP_A, "user_id": INTERNAL_UID, "status": "running"},
            COMPANY_SYSTEM_USER_ID,
        )
    assert exc_company_caller.value.status_code == 403


# ---------------------------------------------------------------------------
# 3. Shared external authentication refusal across HTTP & WebSocket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_company_identity_rejected_across_all_http_and_ws_auth_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(HTTPException) as direct_exc:
        reject_company_system_auth(COMPANY_SYSTEM_USER_ID)
    assert direct_exc.value.status_code == 401
    reject_company_system_auth(INTERNAL_UID)  # Normal user passes cleanly.

    app = FastAPI()

    @app.get("/probe-principal")
    async def _probe_principal(p: principal_mod.ReadPrincipal) -> dict[str, Any]:
        return {"user_id": p.user_id, "auth": p.auth}

    @app.get("/probe-current-user")
    async def _probe_current_user(uid: api_utils.CurrentUserId) -> dict[str, Any]:
        return {"user_id": uid}

    @app.get("/probe-optional-user")
    async def _probe_optional_user(uid: api_utils.OptionalUserId) -> dict[str, Any]:
        return {"user_id": uid}

    @app.get("/probe-stamp")
    async def _probe_stamp(uid: api_utils.StampThreadAuth) -> dict[str, Any]:
        return {"user_id": uid}

    # 3a. Service token path: company rejected, normal user allowed.
    svc_secret = "s" * 40
    monkeypatch.setattr(api_utils, "_SERVICE_TOKEN", svc_secret)
    monkeypatch.setattr(
        dalton_auth,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_auth.is_dalton_supabase_auth_enabled, return_value=False),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in (
            "/probe-principal",
            "/probe-current-user",
            "/probe-optional-user",
            "/probe-stamp",
        ):
            bad = await client.get(
                path,
                headers={
                    "X-Service-Token": svc_secret,
                    "X-User-Id": COMPANY_SYSTEM_USER_ID,
                },
            )
            assert bad.status_code == 401, f"Expected 401 on {path}, got {bad.status_code}"

            ok = await client.get(
                path,
                headers={"X-Service-Token": svc_secret, "X-User-Id": INTERNAL_UID},
            )
            assert ok.status_code == 200
            assert ok.json()["user_id"] == INTERNAL_UID

        # 3b. PAT path
        class _PatKey:
            def __init__(self, user_id: str) -> None:
                self.user_id = user_id
                self.scopes = ("agent:read",)

        monkeypatch.setattr(
            pats,
            "is_pat",
            create_autospec(pats.is_pat, side_effect=lambda tok: tok.startswith("lapat_")),
        )
        monkeypatch.setattr(
            pats,
            "verify_pat",
            create_autospec(
                pats.verify_pat,
                side_effect=lambda tok: _PatKey(
                    COMPANY_SYSTEM_USER_ID if "company" in tok else INTERNAL_UID
                ),
            ),
        )
        pat_bad = await client.get(
            "/probe-principal", headers={"Authorization": "Bearer lapat_company"}
        )
        assert pat_bad.status_code == 401
        pat_ok = await client.get(
            "/probe-principal", headers={"Authorization": "Bearer lapat_human"}
        )
        assert pat_ok.status_code == 200
        assert pat_ok.json() == {"user_id": INTERNAL_UID, "auth": "pat"}

        # 3c. OAuth access token path
        class _OAuthApp:
            def __init__(self, user_id: str) -> None:
                self.user_id = user_id
                self.client_id = "oauth-client-1"
                self.scopes = ("agent:read",)

        monkeypatch.setattr(
            oauth_resource,
            "from_resource_issuer",
            create_autospec(
                oauth_resource.from_resource_issuer,
                side_effect=lambda tok: tok.startswith("oauth_"),
            ),
        )
        monkeypatch.setattr(
            oauth_resource,
            "verify_access_token",
            create_autospec(
                oauth_resource.verify_access_token,
                side_effect=lambda tok: _OAuthApp(
                    COMPANY_SYSTEM_USER_ID if "company" in tok else INTERNAL_UID
                ),
            ),
        )
        oauth_bad = await client.get(
            "/probe-principal", headers={"Authorization": "Bearer oauth_company"}
        )
        assert oauth_bad.status_code == 401
        oauth_ok = await client.get(
            "/probe-principal", headers={"Authorization": "Bearer oauth_human"}
        )
        assert oauth_ok.status_code == 200
        assert oauth_ok.json() == {"user_id": INTERNAL_UID, "auth": "oauth"}

        # 3d. Computer CLI token path (with CLI tokens enabled)
        class _CliIdentity:
            def __init__(self, user_id: str) -> None:
                self.user_id = user_id
                self.scopes = ("agent:read",)
                self.computer_id = COMPUTER_A

        monkeypatch.setattr(
            "src.dalton.settings.is_computer_cli_tokens_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            cli_tokens,
            "is_cli_token",
            create_autospec(
                cli_tokens.is_cli_token,
                side_effect=lambda tok: tok.startswith("lac1."),
            ),
        )
        monkeypatch.setattr(
            cli_tokens,
            "authenticate",
            create_autospec(
                cli_tokens.authenticate,
                side_effect=lambda tok: _CliIdentity(
                    COMPANY_SYSTEM_USER_ID if "company" in tok else INTERNAL_UID
                ),
            ),
        )
        cli_bad = await client.get(
            "/probe-principal", headers={"Authorization": "Bearer lac1.company"}
        )
        assert cli_bad.status_code == 401
        cli_ok = await client.get(
            "/probe-principal", headers={"Authorization": "Bearer lac1.human"}
        )
        assert cli_ok.status_code == 200
        assert cli_ok.json() == {"user_id": INTERNAL_UID, "auth": "computer"}

        # 3e. Session Bearer JWT path (exercises real jwt_bearer._decode_token rejection!)
        monkeypatch.setattr(api_utils, "HOST_MODE", "saas")
        monkeypatch.setattr(jwt_bearer, "HOST_MODE", "saas")
        monkeypatch.setattr(
            jwt_bearer.jwt,
            "get_unverified_header",
            create_autospec(
                jwt_bearer.jwt.get_unverified_header,
                return_value={"kid": "test-kid"},
            ),
        )
        monkeypatch.setattr(
            jwt_bearer._signing_keys,
            "get",
            create_autospec(jwt_bearer._signing_keys.get, return_value="fake-jwk-key"),
        )
        monkeypatch.setattr(
            jwt_bearer.jwt,
            "decode",
            create_autospec(
                jwt_bearer.jwt.decode,
                side_effect=lambda tok, *a, **kw: {
                    "sub": COMPANY_SYSTEM_USER_ID if "company" in tok else INTERNAL_UID,
                    "app_metadata": {"provider": "email"},
                },
            ),
        )
        sess_bad = await client.get(
            "/probe-principal", headers={"Authorization": "Bearer jwt_company"}
        )
        assert sess_bad.status_code == 401
        sess_ok = await client.get(
            "/probe-principal", headers={"Authorization": "Bearer jwt_human"}
        )
        assert sess_ok.status_code == 200
        assert sess_ok.json() == {"user_id": INTERNAL_UID, "auth": "session"}

    # 3f. WebSocket auth path: real ws_auth.authenticate_websocket -> real _decode_token
    monkeypatch.setattr(ws_auth, "HOST_MODE", "saas")

    ws_bad = create_autospec(WebSocket, instance=True)
    ws_bad.headers = {"authorization": "Bearer jwt_company"}
    ws_bad.query_params = {}
    with pytest.raises(WebSocketException) as ws_exc:
        await ws_auth.authenticate_websocket(ws_bad)
    assert ws_exc.value.code == 1008
    ws_bad.close.assert_awaited_once()

    ws_ok = create_autospec(WebSocket, instance=True)
    ws_ok.headers = {"authorization": "Bearer jwt_human"}
    ws_ok.query_params = {}
    assert await ws_auth.authenticate_websocket(ws_ok) == INTERNAL_UID


# ---------------------------------------------------------------------------
# 4. Unified classify_automation_scope & creation entrypoint guards
# ---------------------------------------------------------------------------


def test_classify_automation_scope_positive_and_negative_matrix() -> None:
    active_ws = {
        "workspace_id": WS_ON_COMP_A,
        "user_id": INTERNAL_UID,
        "status": "running",
    }
    active_company_unit = {
        "workspace_id": WS_ON_COMP_A,
        "owner_user_id": INTERNAL_UID,
        "kind": "company",
        "status": "active",
    }
    active_sector_unit = {
        "workspace_id": WS_ON_COMP_A,
        "owner_user_id": INTERNAL_UID,
        "kind": "sector",
        "status": "active",
    }

    # 1. Company system user -> always company
    assert (
        classify_automation_scope(
            {"user_id": COMPANY_SYSTEM_USER_ID, "name": "dalton-data-health"}
        )
        == "company"
    )

    # 2. Other user's dalton-earnings-calendar -> research_tracking (even with empty metadata)
    assert (
        classify_automation_scope(
            {"user_id": INTERNAL_UID, "name": "dalton-earnings-calendar", "metadata": {}}
        )
        == "research_tracking"
    )

    # 3. Every standard research dalton_kind -> research_tracking
    for kind in RESEARCH_TRACKING_KINDS:
        assert (
            classify_automation_scope(
                {
                    "user_id": INTERNAL_UID,
                    "name": f"custom-{kind}",
                    "metadata": {"dalton_kind": kind},
                }
            )
            == "research_tracking"
        )

    # 4. Custom dalton-<slug>-* in active owned coverage unit (company or sector) -> research_tracking
    assert (
        classify_automation_scope(
            {
                "user_id": INTERNAL_UID,
                "name": "dalton-nvda-supply-chain",
                "workspace_id": WS_ON_COMP_A,
                "metadata": {"dalton_managed": True},
            },
            coverage_unit=active_company_unit,
        )
        == "research_tracking"
    )
    assert (
        classify_automation_scope(
            {
                "user_id": INTERNAL_UID,
                "name": "dalton-sector-semi-custom",
                "workspace_id": WS_ON_COMP_A,
                "metadata": {},
            },
            coverage_unit=active_sector_unit,
        )
        == "research_tracking"
    )

    # 5. Custom dalton-<ticker>-* in owned active workspace + covered ticker -> research_tracking
    assert (
        classify_automation_scope(
            {
                "user_id": INTERNAL_UID,
                "name": "dalton-brk.b-channel-check",
                "workspace_id": WS_ON_COMP_A,
                "metadata": {},
            },
            active_ws,
            covered_tickers=["AAPL", "BRK.B"],
        )
        == "research_tracking"
    )

    # 6. Negative cases that MUST stay personal:
    # 6a. Director / data-health / personal AlphaEngine tasks remain personal in Batch 1.
    for kept_personal in (
        {
            "user_id": ADMIN_UID,
            "name": "dalton-director-daily",
            "workspace_id": WS_ON_COMP_A,
            "metadata": {"dalton_kind": "director_daily"},
        },
        {
            "user_id": ADMIN_UID,
            "name": "dalton-director-weekly",
            "workspace_id": WS_ON_COMP_A,
            "metadata": {"dalton_kind": "director_weekly"},
        },
        {
            "user_id": ADMIN_UID,
            "name": "dalton-data-health",
            "workspace_id": WS_ON_COMP_A,
            "metadata": {"dalton_kind": "data_health"},
        },
        {
            "user_id": ADMIN_UID,
            "name": "AlphaEngine 机构会议增量整理入库（每日 09:00 / 18:00）",
            "workspace_id": WS_ON_COMP_A,
            "metadata": {},
        },
    ):
        assert (
            classify_automation_scope(
                kept_personal,
                {**active_ws, "user_id": ADMIN_UID},
                coverage_unit={**active_company_unit, "owner_user_id": ADMIN_UID},
                covered_tickers=["DIRECTOR", "DATA", "ALPHAENGINE"],
            )
            == "personal"
        )

    # 6b. Near-miss names
    for near_name in ("my-dalton-nvda-watch", "dalton-nvda", "nvda-watch", "DALTON-NVDA-check"):
        assert (
            classify_automation_scope(
                {"user_id": INTERNAL_UID, "name": near_name, "workspace_id": WS_ON_COMP_A},
                active_ws,
                coverage_unit=active_company_unit,
                covered_tickers=["NVDA"],
            )
            == "personal"
        )

    # 6c. Workspace owned by another user
    assert (
        classify_automation_scope(
            {"user_id": INTERNAL_UID, "name": "dalton-nvda-custom", "workspace_id": WS_ON_COMP_A},
            {**active_ws, "user_id": OTHER_UID},
            coverage_unit={**active_company_unit, "owner_user_id": OTHER_UID},
            covered_tickers=["NVDA"],
        )
        == "personal"
    )

    # 6d. Archived coverage unit without covered ticker
    assert (
        classify_automation_scope(
            {"user_id": INTERNAL_UID, "name": "dalton-nvda-custom", "workspace_id": WS_ON_COMP_A},
            active_ws,
            coverage_unit={**active_company_unit, "status": "archived"},
            covered_tickers=["MSFT"],
        )
        == "personal"
    )

    # 6e. Deleted or unbound workspace without active coverage unit
    for bad_status in ("deleted", "unbound"):
        assert (
            classify_automation_scope(
                {"user_id": INTERNAL_UID, "name": "dalton-nvda-custom", "workspace_id": WS_ON_COMP_A},
                {**active_ws, "status": bad_status},
                covered_tickers=["NVDA"],
            )
            == "personal"
        )

    sql_pred = build_research_tracking_sql_predicate("a")
    assert "dalton-earnings-calendar" in sql_pred
    assert "dalton_coverage_units" in sql_pred
    assert "dalton_coverage" in sql_pred


@pytest.mark.asyncio
async def test_creation_entrypoints_refuse_company_smuggling_and_compute_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Model-level refusal of client-supplied scope="company" or system user_id
    with pytest.raises( ValueError, match="company scope" ):
        AutomationCreate.model_validate(
            {
                "name": "Attempt",
                "trigger_type": "cron",
                "cron_expression": "0 8 * * *",
                "instruction": "hi",
                "scope": "company",
            }
        )
    with pytest.raises( ValueError, match="company system user_id" ):
        AutomationCreate.model_validate(
            {
                "name": "Attempt",
                "trigger_type": "cron",
                "cron_expression": "0 8 * * *",
                "instruction": "hi",
                "user_id": COMPANY_SYSTEM_USER_ID,
            }
        )
    with pytest.raises( ValueError, match="company scope" ):
        AutomationUpdate.model_validate({"name": "Updated", "scope": "company"})

    # DB & lifecycle entrypoints reject system:dalton-company
    with pytest.raises(ValueError, match="Company automations"):
        await auto_db.create_automation(
            COMPANY_SYSTEM_USER_ID,
            "Bad",
            "cron",
            "run",
            cron_expression="0 8 * * *",
        )
    with pytest.raises(ValueError, match="Company automations"):
        await auto_db.create_automation(
            INTERNAL_UID,
            "Bad",
            "cron",
            "run",
            cron_expression="0 8 * * *",
            scope="company",
        )

    valid_create = AutomationCreate.model_validate(
        {
            "name": "dalton-earnings-calendar",
            "trigger_type": "cron",
            "cron_expression": "0 8 * * 1",
            "instruction": "scan earnings",
            # Attempted spoof to personal is stripped & recomputed as research_tracking!
            "scope": "personal",
        }
    )
    with pytest.raises(ValueError, match="Company automations"):
        await lifecycle_mod.create_automation(COMPANY_SYSTEM_USER_ID, valid_create)

    monkeypatch.setattr(
        lifecycle_mod,
        "_placement",
        create_autospec(lifecycle_mod._placement, return_value=WS_ON_COMP_A),
    )
    monkeypatch.setattr(
        lifecycle_mod,
        "require_target_ownership",
        create_autospec(lifecycle_mod.require_target_ownership, return_value=None),
    )
    monkeypatch.setattr(
        lifecycle_mod,
        "_check_claude_code_hosted",
        create_autospec(lifecycle_mod._check_claude_code_hosted, return_value=None),
    )
    mock_db_create = create_autospec(
        auto_db.create_automation,
        return_value=_sample_automation_row(
            name="dalton-earnings-calendar",
            scope="research_tracking",
        ),
    )
    monkeypatch.setattr(lifecycle_mod.auto_db, "create_automation", mock_db_create)

    await lifecycle_mod.create_automation(INTERNAL_UID, valid_create)
    assert mock_db_create.await_args.kwargs["scope"] == "research_tracking"
    assert mock_db_create.await_args.kwargs["created_by"] == INTERNAL_UID

    # Scheduler helper refuses company user_id or smuggled spec fields
    with pytest.raises(ValueError, match="Company system identity"):
        await schedule_mod.schedule_dalton_automation_helper(
            name="dalton-nvda-watch",
            spec={"instruction": "hi", "cron": "0 9 * * *"},
            user_id=COMPANY_SYSTEM_USER_ID,
        )

    smuggled = await schedule_mod.schedule_dalton_automation_helper(
        name="dalton-nvda-watch",
        spec={"instruction": "hi", "cron": "0 9 * * *", "scope": "company"},
        user_id=INTERNAL_UID,
    )
    assert smuggled["ok"] is False
    assert smuggled["error"] == "forbidden_company_scope"

    # PUT /api/v1/dalton/automations/{name} rejects scope="company" and user_id override with 403
    dalton_app = FastAPI()
    dalton_app.include_router(dalton_router_mod.router)
    dalton_app.dependency_overrides[api_utils.get_current_user_id] = lambda: INTERNAL_UID
    monkeypatch.setattr(dalton_router_mod, "_require_dalton_enabled", lambda: None)

    async with AsyncClient(
        transport=ASGITransport(app=dalton_app), base_url="http://test"
    ) as d_client:
        put_scope = await d_client.put(
            "/api/v1/dalton/automations/dalton-nvda-watch",
            json={"instruction": "hi", "cron": "0 9 * * *", "scope": "company"},
        )
        assert put_scope.status_code == 403

        put_uid = await d_client.put(
            "/api/v1/dalton/automations/dalton-nvda-watch",
            json={
                "instruction": "hi",
                "cron": "0 9 * * *",
                "user_id": COMPANY_SYSTEM_USER_ID,
            },
        )
        assert put_uid.status_code == 403


# ---------------------------------------------------------------------------
# 5. Read-only endpoints: GET /api/v1/automations & GET /history
# ---------------------------------------------------------------------------


def _build_automations_test_app(caller_principal: Principal) -> FastAPI:
    app = FastAPI()
    app.include_router(automations_app.router)
    app.dependency_overrides[get_principal] = lambda: caller_principal
    app.dependency_overrides[api_utils.get_current_user_id] = lambda: caller_principal.user_id
    return app


@pytest.mark.asyncio
async def test_list_automations_scopes_permissions_and_company_notify_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        company_auto_svc,
        "is_internal_or_admin",
        create_autospec(
            access_tiers.is_internal_or_admin,
            side_effect=lambda uid: uid in {INTERNAL_UID, ADMIN_UID},
        ),
    )

    # Caller has personal notify preferences configured; company list MUST NOT consult or apply them.
    override_spy = create_autospec(
        notify_dispatch.resolve_dalton_notify_overrides,
        return_value={"director_weekly": True, f"automation:{AUTO_COMPANY_ID}": True},
    )
    routes_spy = create_autospec(
        notify_dispatch.resolve_dalton_notify_routes,
        return_value={f"automation:{AUTO_COMPANY_ID}": ["feishu:chat:oc_personal_leak"]},
    )
    monkeypatch.setattr(notify_dispatch, "resolve_dalton_notify_overrides", override_spy)
    monkeypatch.setattr(notify_dispatch, "resolve_dalton_notify_routes", routes_spy)

    personal_row = _sample_automation_row(
        automation_id=AUTO_PERSONAL_ID,
        name="Personal Brief",
        scope="personal",
    )
    tracking_row = _sample_automation_row(
        automation_id=AUTO_TRACKING_ID,
        name="dalton-nvda-watch",
        scope="research_tracking",
        metadata={"dalton_managed": True, "dalton_kind": "company_watch"},
    )
    company_task_row = _sample_automation_row(
        automation_id=AUTO_COMPANY_ID,
        user_id=COMPANY_SYSTEM_USER_ID,
        name="AlphaEngine 机构会议增量整理入库",
        scope="company",
        workspace_id=COMPANY_WS,
        notify_storage_mode="task_row",
        notify_enabled=False,
        notify_routes=["feishu:chat:oc_company_team"],
        config_version=2,
        active_version=2,
        created_by=ADMIN_UID,
        updated_by=INTERNAL_UID,
    )

    # Unsupported company+legacy state is explicitly rejected rather than represented as valid.
    class _ProbeResp:
        name = "AlphaEngine"
        metadata: dict[str, Any] = {}
        notify_storage_mode = "legacy"

    with pytest.raises(ValueError, match="notify_storage_mode='task_row'"):
        company_auto_svc.apply_company_notify_representation(_ProbeResp())

    mock_legacy_list = create_autospec(
        auto_db.list_automations,
        return_value=([personal_row, tracking_row], 2),
    )
    monkeypatch.setattr(automations_app.auto_db, "list_automations", mock_legacy_list)

    async def _fake_scoped_db(
        *,
        scope: str,
        caller_user_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        computer_id: str | None = None,
        conn: Any = None,
    ) -> tuple[list[dict[str, Any]], int]:
        if scope == "personal":
            return [personal_row], 1
        if scope == "research_tracking":
            return [tracking_row], 1
        if scope == "company":
            return [company_task_row], 1
        return [], 0

    mock_scoped_db = create_autospec(
        company_auto_db.list_automations_for_scope_db,
        side_effect=_fake_scoped_db,
    )
    monkeypatch.setattr(
        company_auto_svc.company_auto_db,
        "list_automations_for_scope_db",
        mock_scoped_db,
    )

    internal_app = _build_automations_test_app(
        Principal(user_id=INTERNAL_UID, auth="session")
    )
    async with AsyncClient(
        transport=ASGITransport(app=internal_app), base_url="http://test"
    ) as client:
        # 1. Omitted scope returns all visible user tasks via legacy call signature.
        r_all = await client.get("/api/v1/automations")
        assert r_all.status_code == 200
        assert r_all.json()["total"] == 2
        assert {a["scope"] for a in r_all.json()["automations"]} == {
            "personal",
            "research_tracking",
        }
        mock_legacy_list.assert_awaited_once_with(
            INTERNAL_UID,
            status=None,
            limit=50,
            offset=0,
            computer_id=None,
        )

        # 2. Invalid scope -> 422
        r_bad = await client.get("/api/v1/automations?scope=invalid_scope")
        assert r_bad.status_code == 422

        # 3. Company scope -> 200 for internal member; caller notify prefs NEVER consulted or applied!
        override_spy.reset_mock()
        routes_spy.reset_mock()
        r_comp = await client.get("/api/v1/automations?scope=company")
        assert r_comp.status_code == 200
        comp_body = r_comp.json()
        assert comp_body["total"] == 1
        [comp_item] = comp_body["automations"]
        assert comp_item["scope"] == "company"
        assert comp_item["engine_kind"] == "agent"
        assert comp_item["config_version"] == 2
        assert comp_item["active_version"] == 2
        assert comp_item["created_by"] == ADMIN_UID
        assert comp_item["updated_by"] == INTERNAL_UID
        assert comp_item["notify_storage_mode"] == "task_row"
        assert comp_item["dalton_notify_effective"] is False
        assert comp_item["dalton_notify_source"] == "task_row"
        assert comp_item["dalton_notify_route_targets"] == ["feishu:chat:oc_company_team"]
        override_spy.assert_not_awaited()
        routes_spy.assert_not_awaited()

    # 4. External member gets 403 on scope=company BEFORE any DB query.
    mock_scoped_db.reset_mock()
    ext_app = _build_automations_test_app(
        Principal(user_id=EXTERNAL_UID, auth="session")
    )
    async with AsyncClient(
        transport=ASGITransport(app=ext_app), base_url="http://test"
    ) as ext_client:
        r_ext = await ext_client.get("/api/v1/automations?scope=company")
        assert r_ext.status_code == 403
        mock_scoped_db.assert_not_awaited()

    # 5. Computer token (even owned by ADMIN_UID) gets 403 on scope=company BEFORE any DB query.
    comp_token_app = _build_automations_test_app(
        Principal(
            user_id=ADMIN_UID,
            auth="computer",
            scopes=("agent:read",),
            computer_id=COMPUTER_A,
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=comp_token_app), base_url="http://test"
    ) as comp_client:
        r_comp_tok = await comp_client.get("/api/v1/automations?scope=company")
        assert r_comp_tok.status_code == 403
        mock_scoped_db.assert_not_awaited()

        # Personal scope passes computer_id through to DB filter.
        r_pers_tok = await comp_client.get("/api/v1/automations?scope=personal")
        assert r_pers_tok.status_code == 200
        assert mock_scoped_db.await_args.kwargs["computer_id"] == COMPUTER_A


@pytest.mark.asyncio
async def test_automation_history_endpoint_sanitization_pagination_and_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        company_auto_svc,
        "is_internal_or_admin",
        create_autospec(
            access_tiers.is_internal_or_admin,
            side_effect=lambda uid: uid in {INTERNAL_UID, ADMIN_UID},
        ),
    )

    stored_entries = [
        {
            "version": 1,
            "prev_version": None,
            "actor_user_id": ADMIN_UID,
            "changed_at": "2026-11-08T08:00:00+00:00",
            "action": "seed",
            "extra_secret_blob": "MUST_NOT_APPEAR",
            "changes": {},
            "snapshot": {
                "name": "AlphaEngine v1",
                "desired_status": "active",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "instruction": "Run v1",
                "workspace_id": COMPANY_WS,
                "notify_enabled": True,
                "notify_routes": ["feishu:chat:oc_internal"],
                "delivery_config": {
                    "methods": ["feishu:chat:oc_internal"],
                    "webhook_secret": "SECRET_DO_NOT_LEAK",
                    "bot_token": "BOT_TOKEN_DO_NOT_LEAK",
                },
                "metadata": {"pre_company_migration": {"user_id": ADMIN_UID}},
                "created_secrets": ["TENCENT_COS_SECRET_KEY"],
                "api_key": "sk-secret",
            },
        },
        {
            "version": 2,
            "prev_version": 1,
            "actor_user_id": INTERNAL_UID,
            "changed_at": "2026-11-09T09:00:00+00:00",
            "action": "update",
            "changes": {
                "cron_expression": {"old": "0 9 * * *", "new": "0 9,18 * * *"},
                "metadata": {"old": {"a": 1}, "new": {"b": 2}},
                "vault_source_user_id": {"old": None, "new": ADMIN_UID},
            },
            "snapshot": {
                "name": "AlphaEngine v2",
                "desired_status": "active",
                "trigger_type": "cron",
                "cron_expression": "0 9,18 * * *",
                "once_run_at": None,
                "trigger_config": {"symbol": "SPY", "api_token": "DROP_ME"},
                "timezone": "Asia/Shanghai",
                "instruction": "Run v2",
                "workspace_id": COMPANY_WS,
                "llm_model": "gpt-5",
                "additional_context": [{"type": "skill", "name": "s1", "secret_value": "DROP"}],
                "thread_strategy": "new",
                "conversation_thread_id": None,
                "max_failures": 3,
                "notify_enabled": True,
                "notify_routes": ["feishu:chat:oc_internal"],
                "delivery_config": {},
                "pre_company_migration": {"vault_source_user_id": ADMIN_UID},
            },
        },
    ]

    history_rows = {
        AUTO_COMPANY_ID: {
            "automation_id": AUTO_COMPANY_ID,
            "user_id": COMPANY_SYSTEM_USER_ID,
            "scope": "company",
            "workspace_id": COMPANY_WS,
            "config_version": 2,
            "active_version": 2,
            "audit_history": stored_entries,
            "deleted_at": None,
        },
        AUTO_PERSONAL_ID: {
            "automation_id": AUTO_PERSONAL_ID,
            "user_id": INTERNAL_UID,
            "scope": "personal",
            "workspace_id": WS_ON_COMP_A,
            "config_version": 1,
            "active_version": 1,
            "audit_history": [],
            "deleted_at": None,
        },
        AUTO_TRACKING_ID: {
            "automation_id": AUTO_TRACKING_ID,
            "user_id": INTERNAL_UID,
            "scope": "research_tracking",
            "workspace_id": WS_ON_COMP_B,
            "config_version": 1,
            "active_version": 1,
            "audit_history": [],
            "deleted_at": None,
        },
    }

    monkeypatch.setattr(
        company_auto_svc.company_auto_db,
        "get_automation_history_row",
        create_autospec(
            company_auto_db.get_automation_history_row,
            side_effect=lambda aid, conn=None: history_rows.get(str(aid)),
        ),
    )

    # Guard that GET /history never triggers update_automation or create_automation.
    update_spy = create_autospec(auto_db.update_automation)
    monkeypatch.setattr(auto_db, "update_automation", update_spy)

    internal_app = _build_automations_test_app(
        Principal(user_id=INTERNAL_UID, auth="session")
    )
    async with AsyncClient(
        transport=ASGITransport(app=internal_app), base_url="http://test"
    ) as client:
        # 1. Company row history: descending order, paginated, secrets/metadata stripped!
        resp = await client.get(f"/api/v1/automations/{AUTO_COMPANY_ID}/history?limit=1&offset=0")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["config_version"] == 2
        assert payload["active_version"] == 2
        assert payload["total"] == 2
        assert [e["version"] for e in payload["entries"]] == [2]
        v2_entry = payload["entries"][0]
        assert set(v2_entry["changes"].keys()) == {"cron_expression"}
        assert set(v2_entry["snapshot"].keys()) <= company_auto_svc.ALLOWED_AUTOMATION_SNAPSHOT_FIELD_SET
        assert "pre_company_migration" not in v2_entry["snapshot"]
        assert "api_token" not in v2_entry["snapshot"]["trigger_config"]
        assert "secret_value" not in v2_entry["snapshot"]["additional_context"][0]

        resp_p2 = await client.get(
            f"/api/v1/automations/{AUTO_COMPANY_ID}/history?limit=1&offset=1"
        )
        assert resp_p2.status_code == 200
        v1_entry = resp_p2.json()["entries"][0]
        assert v1_entry["version"] == 1
        assert "extra_secret_blob" not in v1_entry
        assert "metadata" not in v1_entry["snapshot"]
        assert "created_secrets" not in v1_entry["snapshot"]
        assert "api_key" not in v1_entry["snapshot"]
        assert "webhook_secret" not in v1_entry["snapshot"]["delivery_config"]
        assert "bot_token" not in v1_entry["snapshot"]["delivery_config"]

        # 2. Empty history returns [] without seeding/mutating DB!
        empty_resp = await client.get(f"/api/v1/automations/{AUTO_PERSONAL_ID}/history")
        assert empty_resp.status_code == 200
        assert empty_resp.json()["entries"] == []
        assert empty_resp.json()["total"] == 0
        update_spy.assert_not_awaited()

        # 3. Non-existent UUID -> 404; malformed UUID -> 422
        missing_resp = await client.get(f"/api/v1/automations/{uuid.uuid4()}/history")
        assert missing_resp.status_code == 404
        malformed_resp = await client.get("/api/v1/automations/not-a-uuid/history")
        assert malformed_resp.status_code == 422

    # 4. Cross-user access to personal/research_tracking -> 403
    other_app = _build_automations_test_app(
        Principal(user_id=OTHER_UID, auth="session")
    )
    async with AsyncClient(
        transport=ASGITransport(app=other_app), base_url="http://test"
    ) as other_client:
        cross_resp = await other_client.get(
            f"/api/v1/automations/{AUTO_PERSONAL_ID}/history"
        )
        assert cross_resp.status_code == 403

    # 5. External user access to company history -> 403
    ext_app = _build_automations_test_app(
        Principal(user_id=EXTERNAL_UID, auth="session")
    )
    async with AsyncClient(
        transport=ASGITransport(app=ext_app), base_url="http://test"
    ) as ext_client:
        ext_comp_resp = await ext_client.get(
            f"/api/v1/automations/{AUTO_COMPANY_ID}/history"
        )
        assert ext_comp_resp.status_code == 403

    # 6. Computer token reach checks: allowed on own computer, 403 on company / Home / other computer
    async def _fake_computer_of(kind: str, user_id: str, resource_id: str) -> Any:
        mapping = {
            ("automation", AUTO_PERSONAL_ID): COMPUTER_A,
            ("workspace", WS_ON_COMP_A): COMPUTER_A,
            ("automation", AUTO_TRACKING_ID): COMPUTER_B,
            ("workspace", WS_ON_COMP_B): COMPUTER_B,
            ("workspace", get_home_workspace_id(INTERNAL_UID)): principal_reach.HOME,
        }
        return mapping.get((kind, resource_id), principal_reach.MISSING)

    monkeypatch.setattr(
        principal_reach,
        "computer_of",
        create_autospec(principal_reach.computer_of, side_effect=_fake_computer_of),
    )

    comp_app = _build_automations_test_app(
        Principal(
            user_id=INTERNAL_UID,
            auth="computer",
            scopes=("agent:read",),
            computer_id=COMPUTER_A,
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=comp_app), base_url="http://test"
    ) as comp_client:
        ok_on_comp = await comp_client.get(
            f"/api/v1/automations/{AUTO_PERSONAL_ID}/history"
        )
        assert ok_on_comp.status_code == 200

        refused_other_comp = await comp_client.get(
            f"/api/v1/automations/{AUTO_TRACKING_ID}/history"
        )
        assert refused_other_comp.status_code == 403

        refused_company = await comp_client.get(
            f"/api/v1/automations/{AUTO_COMPANY_ID}/history"
        )
        assert refused_company.status_code == 403


# ---------------------------------------------------------------------------
# 6. R4 Hardening: whitespace/case normalization parity, saved-row scope on update,
#    and channel gateway company identity rejection across all entrypoints
# ---------------------------------------------------------------------------


def test_scope_normalization_whitespace_case_and_sql_contract() -> None:
    for ws_ch in (" ", "\t", "\n", "\r", "\x0b", "\x0c", "\u00a0", "\u2003", "\u3000"):
        assert ws_ch in SCOPE_TRIM_WHITESPACE
        assert normalize_scope_text(f"{ws_ch}dalton-earnings-calendar{ws_ch}") == (
            "dalton-earnings-calendar"
        )

    assert normalize_scope_text(None) == ""
    assert normalize_scope_text(True) == ""
    assert normalize_scope_text({"a": 1}) == ""
    assert normalize_dalton_kind(None) == ""
    assert normalize_dalton_kind({"dalton_kind": None}) == ""
    assert normalize_dalton_kind({"dalton_kind": 123}) == ""
    assert normalize_dalton_kind({"dalton_kind": ["company_watch"]}) == ""
    assert (
        normalize_dalton_kind({"dalton_kind": " \t\n\u00a0\u3000Company_Watch \r"})
        == "company_watch"
    )

    active_ws = {
        "workspace_id": f" \t{WS_ON_COMP_A}\u3000",
        "user_id": f" {INTERNAL_UID}\n",
        "status": " \tRUNNING\u00a0",
    }
    active_unit = {
        "workspace_id": WS_ON_COMP_A,
        "owner_user_id": f"\u3000{INTERNAL_UID} ",
        "kind": " COMPANY\t",
        "status": "\nACTIVE ",
    }

    cases: list[tuple[dict[str, Any], bool, str]] = [
        (
            {"user_id": " \tSyStEm:DaLtOn-CoMpAnY\n\u3000", "name": "any"},
            False,
            "company",
        ),
        (
            {
                "user_id": INTERNAL_UID,
                "name": " \t\u00a0dalton-earnings-calendar\u3000\n",
                "metadata": {},
            },
            False,
            "research_tracking",
        ),
        (
            {
                "user_id": INTERNAL_UID,
                "name": "Custom Watch",
                "metadata": {"dalton_kind": " \t\u3000PRICE_MOVE\n"},
            },
            False,
            "research_tracking",
        ),
        (
            {
                "user_id": INTERNAL_UID,
                "name": "Custom Unknown",
                "workspace_id": WS_ON_COMP_A,
                "metadata": {"dalton_kind": " \tunknown_kind\u3000"},
            },
            False,
            "personal",
        ),
        (
            {
                "user_id": INTERNAL_UID,
                "name": " \t\u3000dalton-nvda-ChannelCheck\u00a0\n",
                "workspace_id": WS_ON_COMP_A,
                "metadata": {"dalton_kind": " \t\u3000"},
            },
            True,
            "research_tracking",
        ),
        (
            {
                "user_id": INTERNAL_UID,
                "name": " \tdalton-director-daily\u3000\n",
                "workspace_id": WS_ON_COMP_A,
                "metadata": {"dalton_kind": " \tdirector_daily\n"},
            },
            False,
            "personal",
        ),
        (
            {
                "user_id": INTERNAL_UID,
                "name": " \t\n\u00a0\u3000",
                "workspace_id": WS_ON_COMP_A,
                "metadata": None,
            },
            False,
            "personal",
        ),
    ]

    for row, exp_lookup, exp_scope in cases:
        assert (
            needs_workspace_tracking_lookup(
                row.get("user_id"),
                name=row.get("name"),
                workspace_id=row.get("workspace_id"),
                metadata=row.get("metadata"),
            )
            is exp_lookup
        ), row
        assert (
            classify_automation_scope(
                row,
                active_ws,
                coverage_unit=active_unit,
                covered_tickers=[" \tnVdA\u3000"],
            )
            == exp_scope
        ), row

    case_sql = build_scope_case_sql("a")
    assert "BTRIM(COALESCE(a.name, ''), E'" in case_sql
    assert "\\u3000" in case_sql
    assert "\\u00a0" in case_sql
    assert "jsonb_typeof(a.metadata->'dalton_kind') = 'string'" in case_sql


@pytest.mark.asyncio
async def test_schedule_upsert_uses_saved_workspace_for_legacy_null_ws_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_id = get_home_workspace_id(ADMIN_UID)
    legacy_null_ws_row = _sample_automation_row(
        automation_id=AUTO_PERSONAL_ID,
        user_id=ADMIN_UID,
        name="dalton-nvda-legacy-custom",
        scope="personal",
        workspace_id=None,
        metadata={},
    )
    covered_ws_row = _sample_automation_row(
        automation_id=AUTO_TRACKING_ID,
        user_id=ADMIN_UID,
        name="dalton-nvda-covered-custom",
        scope="research_tracking",
        workspace_id=WS_ON_COMP_A,
        metadata={},
    )

    @asynccontextmanager
    async def _noop_lock(uid: str, *, conn: Any = None):
        yield conn

    monkeypatch.setattr(schedule_mod, "user_automations_lock", _noop_lock)
    monkeypatch.setattr(
        schedule_mod,
        "resolve_caller_home_workspace_id",
        create_autospec(schedule_mod.resolve_caller_home_workspace_id, return_value=home_id),
    )
    monkeypatch.setattr(
        schedule_mod,
        "verify_workspace_access",
        create_autospec(
            schedule_mod.verify_workspace_access,
            return_value={"workspace_id": WS_ON_COMP_A, "user_id": ADMIN_UID, "status": "running"},
        ),
    )

    stored_rows = [legacy_null_ws_row]
    monkeypatch.setattr(
        schedule_mod,
        "list_all_user_automations",
        create_autospec(
            schedule_mod.list_all_user_automations,
            side_effect=lambda uid, name_prefix=None, conn=None: list(stored_rows),
        ),
    )

    captured_scope_calls: list[dict[str, Any]] = []

    async def _fake_resolve_scope(
        uid: str,
        *,
        name: str,
        workspace_id: str | None,
        metadata: Any,
        conn: Any = None,
    ) -> str:
        captured_scope_calls.append(
            {"user_id": uid, "name": name, "workspace_id": workspace_id, "metadata": metadata}
        )
        return classify_automation_scope(
            {"user_id": uid, "name": name, "workspace_id": workspace_id, "metadata": metadata},
            {"workspace_id": WS_ON_COMP_A, "user_id": ADMIN_UID, "status": "running"}
            if workspace_id == WS_ON_COMP_A
            else None,
            covered_tickers=["NVDA"],
        )

    monkeypatch.setattr(
        company_auto_db,
        "resolve_automation_scope_for_write",
        create_autospec(
            company_auto_db.resolve_automation_scope_for_write,
            side_effect=_fake_resolve_scope,
        ),
    )

    captured_updates: list[dict[str, Any]] = []

    async def _fake_db_update(automation_id: str, uid: str, *, conn: Any = None, **kwargs: Any) -> dict[str, Any]:
        captured_updates.append({"automation_id": automation_id, **kwargs})
        return {**stored_rows[0], **kwargs}

    monkeypatch.setattr(
        auto_db,
        "update_automation",
        create_autospec(auto_db.update_automation, side_effect=_fake_db_update),
    )

    # Analyst creation stays in the caller's workspace and leaves the Home task intact.
    created_id = str(uuid.uuid4())
    create_spy = create_autospec(
        auto_db.create_automation,
        return_value={"automation_id": created_id},
    )
    monkeypatch.setattr(auto_db, "create_automation", create_spy)
    monkeypatch.setattr(
        "src.dalton.autonomy.tracking._resolve_workspace_ticker",
        lambda _ws: _async_val("NVDA"),
    )
    analyst_created = await schedule_mod.schedule_dalton_automation_helper(
        name="dalton-nvda-legacy-custom",
        workspace_id=WS_ON_COMP_A,
        caller_workspace_id=WS_ON_COMP_A,
        spec={"instruction": "updated", "cron": "0 9 * * *"},
        user_id=INTERNAL_UID,
        role="analyst",
    )
    assert analyst_created["ok"] is True
    assert analyst_created["action"] == "created"
    assert analyst_created["automation_id"] == created_id
    create_spy.assert_awaited_once()
    assert create_spy.await_args.kwargs["workspace_id"] == WS_ON_COMP_A
    assert create_spy.await_args.kwargs["user_id"] == INTERNAL_UID
    assert not captured_updates

    # 2. Non-PM chief_of_staff cannot touch legacy null-workspace row -> forbidden_pm
    monkeypatch.setattr("src.dalton.autonomy.system_jobs.is_pm_user", lambda _u: False)
    non_pm_refused = await schedule_mod.schedule_dalton_automation_helper(
        name="dalton-nvda-legacy-custom",
        workspace_id=WS_ON_COMP_A,
        spec={"instruction": "updated", "cron": "0 9 * * *"},
        user_id=INTERNAL_UID,
        role="chief_of_staff",
    )
    assert non_pm_refused["ok"] is False
    assert non_pm_refused["error"] == "forbidden_pm"
    assert not captured_updates

    # 3. PM chief_of_staff upserts legacy null-workspace row (even passing covered WS_ON_COMP_A):
    #    patch does NOT modify workspace_id, so scope uses existing None workspace -> personal!
    monkeypatch.setattr("src.dalton.autonomy.system_jobs.is_pm_user", lambda u: u == ADMIN_UID)
    pm_ok = await schedule_mod.schedule_dalton_automation_helper(
        name="dalton-nvda-legacy-custom",
        workspace_id=WS_ON_COMP_A,
        spec={"instruction": "updated by pm", "cron": "0 10 * * *"},
        user_id=ADMIN_UID,
        role="chief_of_staff",
    )
    assert pm_ok["ok"] is True
    assert pm_ok["action"] == "updated"
    assert captured_scope_calls[-1]["workspace_id"] is None
    assert "workspace_id" not in captured_updates[-1]
    assert captured_updates[-1]["scope"] == "personal"

    # 4. PM chief_of_staff upserts legacy null-workspace row WITH research dalton_kind -> research_tracking
    pm_rt = await schedule_mod.schedule_dalton_automation_helper(
        name="dalton-nvda-legacy-custom",
        spec={
            "instruction": "watch",
            "cron": "0 10 * * *",
            "metadata": {"dalton_kind": "company_watch"},
        },
        user_id=ADMIN_UID,
        role="chief_of_staff",
    )
    assert pm_rt["ok"] is True
    assert captured_scope_calls[-1]["workspace_id"] is None
    assert captured_updates[-1]["scope"] == "research_tracking"

    # 5. Existing row with non-null covered workspace keeps workspace_id=WS_ON_COMP_A -> research_tracking
    stored_rows[:] = [covered_ws_row]
    pm_cov = await schedule_mod.schedule_dalton_automation_helper(
        name="dalton-nvda-covered-custom",
        workspace_id=WS_ON_COMP_A,
        spec={"instruction": "keep covered ws", "cron": "0 11 * * *"},
        user_id=ADMIN_UID,
        role="chief_of_staff",
    )
    assert pm_cov["ok"] is True
    assert pm_cov["action"] == "updated"
    assert captured_scope_calls[-1]["workspace_id"] == WS_ON_COMP_A
    assert captured_updates[-1]["scope"] == "research_tracking"


async def _async_val(val: Any) -> Any:
    return val


@pytest.mark.asyncio
async def test_channels_router_and_gateway_reject_company_identity_before_db_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.app import channels_router as ch_router_mod
    from src.dalton.database import channel_gateway as cg_db

    svc_secret = "c" * 40
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", svc_secret)

    # Guard that router-level rejections never even open a DB connection!
    conn_opened = {"count": 0}

    @asynccontextmanager
    async def _fail_if_opened(_existing: Any = None):
        conn_opened["count"] += 1
        raise AssertionError("DB connection should not be opened on direct company identity!")
        yield  # pragma: no cover

    monkeypatch.setattr(cg_db.db_pool, "get_db_connection", _fail_if_opened)

    app = FastAPI()
    app.include_router(ch_router_mod.channels_router, prefix="/api/v1/dalton")
    headers = {"X-Service-Token": svc_secret}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for variant in (
            COMPANY_SYSTEM_USER_ID,
            " \tSyStEm:DaLtOn-CoMpAnY\n\u3000",
        ):
            # Exact prompt scenario: cmd="new", platform="feishu", dalton_user_id=company, scope_key="dm:test"
            r_cmd = await client.post(
                "/api/v1/dalton/channels/internal/session/command",
                headers=headers,
                json={
                    "cmd": "new",
                    "platform": "feishu",
                    "dalton_user_id": variant,
                    "scope_key": "dm:test",
                },
            )
            assert r_cmd.status_code == 401

            r_state_q = await client.get(
                "/api/v1/dalton/channels/internal/state",
                headers=headers,
                params={"user_id": variant},
            )
            assert r_state_q.status_code == 401

            r_bind = await client.post(
                "/api/v1/dalton/channels/internal/bind-code/consume",
                headers=headers,
                json={"action": "unbind", "platform": "feishu", "user_id": variant},
            )
            assert r_bind.status_code == 401

            r_claim = await client.post(
                "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
                headers=headers,
                json={
                    "delivery_key": "tool:r1:c1",
                    "kind": "agent_send",
                    "user_id": variant,
                    "target": "feishu:@me",
                },
            )
            assert r_claim.status_code == 401

            r_in_prog = await client.post(
                "/api/v1/dalton/channels/internal/inbound/progress",
                headers=headers,
                json={
                    "thread_watch": {
                        "thread_id": str(uuid.uuid4()),
                        "dalton_user_id": variant,
                        "platform": "feishu",
                        "target_address": "feishu:@me",
                    }
                },
            )
            assert r_in_prog.status_code == 401

            r_wx = await client.put(
                "/api/v1/dalton/channels/internal/weixin-state",
                headers=headers,
                json={
                    "ilink_bot_id": "bot1",
                    "owner_user_id": variant,
                    "bot_token": "tok",
                },
            )
            assert r_wx.status_code == 401

        for hdr_variant in (COMPANY_SYSTEM_USER_ID, "SyStEm:DaLtOn-CoMpAnY"):
            r_cmd_hdr = await client.post(
                "/api/v1/dalton/channels/internal/session/command",
                headers={**headers, "X-User-Id": hdr_variant},
                json={"cmd": "new", "platform": "feishu", "scope_key": "dm:test"},
            )
            assert r_cmd_hdr.status_code == 401

    # Also test raw ASGI X-User-Id header carrying leading/trailing whitespace + mixed case
    raw_body = b'{"cmd":"new","platform":"feishu","scope_key":"dm:test"}'
    sent_events: list[dict[str, Any]] = []
    body_delivered = False

    async def _receive() -> dict[str, Any]:
        nonlocal body_delivered
        if not body_delivered:
            body_delivered = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, Any]) -> None:
        sent_events.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/dalton/channels/internal/session/command",
            "raw_path": b"/api/v1/dalton/channels/internal/session/command",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-service-token", svc_secret.encode("ascii")),
                (b"x-user-id", b" \tSyStEm:DaLtOn-CoMpAnY\t "),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        },
        _receive,
        _send,
    )
    assert sent_events[0]["status"] == 401
    assert conn_opened["count"] == 0


# ---------------------------------------------------------------------------
# 8. AU1 Batch A: Config History, Snapshot, Once Schedule & Expected Version
# ---------------------------------------------------------------------------


def test_au1_expected_config_version_validation_and_protected_fields() -> None:
    # Omitted -> None
    assert company_auto_svc.validate_expected_config_version(None, provided=False) is None
    assert company_auto_svc.extract_expected_config_version(None) is None
    assert company_auto_svc.extract_expected_config_version({}) is None

    # Valid positive integers
    assert company_auto_svc.validate_expected_config_version(1, provided=True) == 1
    assert company_auto_svc.extract_expected_config_version({"expected_config_version": 7}) == 7

    # Invalid values -> 400 HTTPException
    for bad_val in (0, -1, -99, True, False, "1", "0", 1.5, None, [], {}):
        with pytest.raises(HTTPException) as exc_info:
            company_auto_svc.validate_expected_config_version(bad_val, provided=True)
        assert exc_info.value.status_code == 400

        with pytest.raises(HTTPException) as exc_info2:
            company_auto_svc.extract_expected_config_version({"expected_config_version": bad_val})
        assert exc_info2.value.status_code == 400

    # AutomationUpdate excludes expected_config_version from model_dump so fields stays clean
    upd = AutomationUpdate.model_validate(
        {"description": "new desc", "expected_config_version": 3}
    )
    assert upd.expected_config_version == 3
    dumped = upd.model_dump(exclude_unset=True)
    assert dumped == {"description": "new desc"}

    # Protected audit/version fields are rejected on AutomationCreate and AutomationUpdate
    for protected_field, val in (
        ("created_by", "other-user"),
        ("updated_by", "other-user"),
        ("audit_history", []),
        ("config_version", 99),
        ("active_version", 99),
        ("notify_storage_mode", "task_row"),
    ):
        with pytest.raises(ValueError, match=protected_field):
            AutomationCreate.model_validate(
                {
                    "name": "Test",
                    "trigger_type": "cron",
                    "cron_expression": "0 8 * * *",
                    "instruction": "run",
                    protected_field: val,
                }
            )
        with pytest.raises(ValueError, match=protected_field):
            AutomationUpdate.model_validate({protected_field: val})


def test_au1_snapshot_builder_desired_status_and_corrupt_history_validation() -> None:
    base_row = {
        **_sample_automation_row(
            metadata={"dalton_kind": "company_watch", "api_key": "k-secret"},
        ),
        "status": "executing",
        "trigger_type": "price",
        "cron_expression": None,
        "delivery_config": {"methods": ["feishu"], "webhook_secret": "sec-123"},
    }
    active_hist = [
        {
            "version": 1,
            "snapshot": {"desired_status": "active"},
        }
    ]
    paused_hist = [
        {
            "version": 1,
            "snapshot": {"desired_status": "paused"},
        }
    ]
    # Price/executing row (claimed from active) and executing + active history -> active
    assert company_auto_svc.resolve_desired_status_for_snapshot(base_row) == "active"
    assert (
        company_auto_svc.resolve_desired_status_for_snapshot(
            {**base_row, "active_version": 1, "audit_history": active_hist}
        )
        == "active"
    )
    # Explicitly paused configuration -> paused
    assert (
        company_auto_svc.resolve_desired_status_for_snapshot(
            {**base_row, "status": "paused"}
        )
        == "paused"
    )
    # Once/price runtime completion with active history or no history -> active
    assert (
        company_auto_svc.resolve_desired_status_for_snapshot(
            {**base_row, "trigger_type": "once", "status": "completed", "audit_history": []}
        )
        == "active"
    )
    assert (
        company_auto_svc.resolve_desired_status_for_snapshot(
            {
                **base_row,
                "trigger_type": "once",
                "status": "completed",
                "active_version": 1,
                "audit_history": active_hist,
            }
        )
        == "active"
    )
    # Runtime status (completed/disabled) whose active_version history is paused -> paused
    assert (
        company_auto_svc.resolve_desired_status_for_snapshot(
            {**base_row, "status": "completed", "active_version": 1, "audit_history": paused_hist}
        )
        == "paused"
    )

    snap = company_auto_svc.build_automation_config_snapshot(
        base_row,
        notify_enabled=True,
        notify_routes=["feishu:dm"],
    )
    assert set(snap.keys()) == set(company_auto_svc.ALLOWED_AUTOMATION_SNAPSHOT_FIELDS)
    assert "metadata" not in snap
    assert snap["desired_status"] == "active"
    assert snap["notify_enabled"] is True
    assert snap["notify_routes"] == ["feishu:dm"]
    assert "webhook_secret" not in snap["delivery_config"]

    # Corrupt history state validation
    assert (
        company_auto_svc.validate_existing_history_state(
            {"config_version": 1, "active_version": 1, "audit_history": []}
        )
        is None
    )
    with pytest.raises(company_auto_svc.AutomationHistoryCorruptError):
        company_auto_svc.validate_existing_history_state(
            {"config_version": 3, "active_version": 3, "audit_history": []}
        )
    with pytest.raises(company_auto_svc.AutomationHistoryCorruptError):
        company_auto_svc.validate_existing_history_state(
            {
                "config_version": 2,
                "active_version": 2,
                "audit_history": [{"version": 1, "snapshot": snap}],
            }
        )
    with pytest.raises(company_auto_svc.AutomationHistoryCorruptError):
        company_auto_svc.validate_existing_history_state(
            {
                "config_version": 2,
                "active_version": 2,
                "audit_history": [{"version": 2, "snapshot": None}],
            }
        )


@pytest.mark.asyncio
async def test_au1_automation_history_context_invariants_and_concurrency_isolation() -> None:
    assert company_auto_svc.get_current_history_context() is None

    # 1. Implicit actor / action vs explicit human actor vs explicit None system actor
    with company_auto_svc.automation_history_context() as ctx_implicit:
        assert ctx_implicit.has_explicit_actor is False
        assert ctx_implicit.has_explicit_action is False
        assert ctx_implicit.action is None

    with company_auto_svc.automation_history_context(
        actor_user_id="pm-actor-1",
        action="resume",
        expected_config_version=4,
    ) as ctx_outer:
        assert ctx_outer.has_explicit_actor is True
        assert ctx_outer.actor_user_id == "pm-actor-1"
        assert ctx_outer.has_explicit_action is True
        assert ctx_outer.action == "resume"
        assert ctx_outer.expected_config_version == 4

        # Nested context without actor/action/expected_config_version inherits parent values
        with company_auto_svc.automation_history_context(
            skip_if_unchanged=True,
        ) as ctx_inner_inherit:
            assert ctx_inner_inherit.has_explicit_actor is True
            assert ctx_inner_inherit.actor_user_id == "pm-actor-1"
            assert ctx_inner_inherit.has_explicit_action is True
            assert ctx_inner_inherit.action == "resume"
            assert ctx_inner_inherit.expected_config_version == 4
            assert ctx_inner_inherit.skip_if_unchanged is True

        # Nested context with explicit None actor (maintenance/system origin) overrides parent
        with pytest.raises(RuntimeError, match="inner boom"):
            with company_auto_svc.automation_history_context(
                actor_user_id=None,
                action="maintenance_pause_stale_binding",
            ) as ctx_sys:
                assert ctx_sys.has_explicit_actor is True
                assert ctx_sys.actor_user_id is None
                assert ctx_sys.has_explicit_action is True
                assert ctx_sys.action == "maintenance_pause_stale_binding"
                raise RuntimeError("inner boom")

        # Outer context is restored after inner exit / exception
        restored = company_auto_svc.get_current_history_context()
        assert restored is ctx_outer
        assert restored.actor_user_id == "pm-actor-1"
        assert restored.action == "resume"

    assert company_auto_svc.get_current_history_context() is None

    # 2. Concurrent coroutine isolation (no ContextVar leakage across asyncio.gather tasks)
    barrier = asyncio.Event()

    async def _worker_a() -> tuple[str | None, str | None]:
        with company_auto_svc.automation_history_context(
            actor_user_id="actor-a",
            action="pause",
        ):
            await barrier.wait()
            cur = company_auto_svc.get_current_history_context()
            assert cur is not None
            return str(cur.actor_user_id), cur.action

    async def _worker_b() -> tuple[bool, str | None, str | None]:
        with company_auto_svc.automation_history_context(
            actor_user_id=None,
            action="maintenance_pause_stale_binding",
        ):
            barrier.set()
            await asyncio.sleep(0)
            cur = company_auto_svc.get_current_history_context()
            assert cur is not None
            return cur.has_explicit_actor, cur.actor_user_id, cur.action

    res_a, res_b = await asyncio.gather(_worker_a(), _worker_b())
    assert res_a == ("actor-a", "pause")
    assert res_b == (True, None, "maintenance_pause_stale_binding")
    assert company_auto_svc.get_current_history_context() is None


@pytest.mark.asyncio
async def test_au1_resolve_effective_once_run_at_precedence_and_legacy_fallback() -> None:
    t1 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    t2 = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    t3 = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)

    # 1. target_snapshot wins
    row = {
        "automation_id": AUTO_PERSONAL_ID,
        "trigger_type": "once",
        "next_run_at": t2,
        "audit_history": [],
        "metadata": {"once_schedule_at": t3.isoformat()},
    }
    assert (
        await company_auto_db._resolve_effective_once_run_at(
            row, target_snapshot={"once_run_at": t1.isoformat()}
        )
        == t1.isoformat()
    )

    # 2. row.next_run_at wins when target_snapshot is None
    assert (
        await company_auto_db._resolve_effective_once_run_at(row)
        == t2.isoformat()
    )

    # 3. When next_run_at is None and audit_history is non-empty, active_version snapshot wins
    #    and never falls back to automation_executions even if once_run_at is None in history.
    row_with_hist = {
        "automation_id": AUTO_PERSONAL_ID,
        "trigger_type": "once",
        "next_run_at": None,
        "active_version": 1,
        "audit_history": [
            {"version": 1, "snapshot": {"once_run_at": t1.isoformat()}},
        ],
        "metadata": {"once_schedule_at": t3.isoformat()},
    }
    assert (
        await company_auto_db._resolve_effective_once_run_at(row_with_hist)
        == t1.isoformat()
    )

    # 4a. Empty audit_history + metadata.once_schedule_at wins without DB query
    row_empty_hist_meta = {
        "automation_id": AUTO_PERSONAL_ID,
        "trigger_type": "once",
        "next_run_at": None,
        "audit_history": [],
        "metadata": {"once_schedule_at": t3.isoformat()},
    }
    assert (
        await company_auto_db._resolve_effective_once_run_at(row_empty_hist_meta)
        == t3.isoformat()
    )

    # 4b. Empty audit_history + no once_schedule_at -> queries automation_executions
    exec_rows_state: list[dict[str, Any]] = [{"scheduled_at": t1}]

    class _FakeExecCursor:
        async def __aenter__(self) -> "_FakeExecCursor":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: str, params: Any = None) -> None:
            self.sql = sql
            self.params = params

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(exec_rows_state)

    class _FakeExecConn:
        def cursor(self, **kwargs: Any) -> _FakeExecCursor:
            return _FakeExecCursor()

    row_legacy_claimed = {
        "automation_id": AUTO_PERSONAL_ID,
        "trigger_type": "once",
        "next_run_at": None,
        "last_run_at": t1,
        "audit_history": [],
        "metadata": {},
    }
    # Exactly 1 matching execution -> returns scheduled_at ISO
    assert (
        await company_auto_db._resolve_effective_once_run_at(
            row_legacy_claimed, conn=_FakeExecConn()
        )
        == t1.isoformat()
    )
    # 0 matching executions -> None
    exec_rows_state[:] = []
    assert (
        await company_auto_db._resolve_effective_once_run_at(
            row_legacy_claimed, conn=_FakeExecConn()
        )
        is None
    )
    # >1 matching executions -> refuses to guess -> None
    exec_rows_state[:] = [{"scheduled_at": t1}, {"scheduled_at": t2}]
    assert (
        await company_auto_db._resolve_effective_once_run_at(
            row_legacy_claimed, conn=_FakeExecConn()
        )
        is None
    )
