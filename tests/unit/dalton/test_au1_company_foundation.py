"""Unit tests for AU1 Batch 1: company identity, external auth refusal, scope classification, and read-only endpoints."""

from __future__ import annotations

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
    build_research_tracking_sql_predicate,
    classify_automation_scope,
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

        # 3e. Session Bearer JWT path (capture original callable before patching!)
        orig_decode_token = jwt_bearer._decode_token
        monkeypatch.setattr(api_utils, "HOST_MODE", "saas")
        monkeypatch.setattr(jwt_bearer, "HOST_MODE", "saas")

        async def _fake_decode(tok: str) -> jwt_bearer.AuthInfo:
            uid = COMPANY_SYSTEM_USER_ID if "company" in tok else INTERNAL_UID
            reject_company_system_auth(uid)
            return jwt_bearer.AuthInfo(user_id=uid, auth_provider="email")

        monkeypatch.setattr(
            api_utils,
            "_decode_token",
            create_autospec(orig_decode_token, side_effect=_fake_decode),
        )
        monkeypatch.setattr(
            jwt_bearer,
            "_decode_token",
            create_autospec(orig_decode_token, side_effect=_fake_decode),
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

    # 3f. WebSocket auth path: company token closes with 1008 + WebSocketException; normal user succeeds.
    monkeypatch.setattr(ws_auth, "HOST_MODE", "saas")
    monkeypatch.setattr(
        ws_auth,
        "_decode_token",
        create_autospec(orig_decode_token, side_effect=_fake_decode),
    )

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
