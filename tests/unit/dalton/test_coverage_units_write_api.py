"""Sector layer P5: coverage-unit write API and the ``dalton_coverage_unit`` tool.

锁定的行为：
- 开关关闭：四个写端点一律 404 ``coverage units disabled``，不触任何服务
  函数（公司路径零新增动作）；工具不挂载（两个角色都不挂），已建的工具
  实例在开关翻转后返回 ``coverage_units_disabled``；
- 开关开启：登记 / 绑定 / 解绑 / 归档全部走 P0 的真实服务函数
  （``src/dalton/coverage/units.py``），服务层 ValueError 映射为明确 4xx，
  ``detail`` 带 ``{error: <稳定码>, message}``；
- 输入校验（sector 带 ticker、kind/slug/tz/UUID）由真实服务在触库之前
  拒绝；唯一冲突 / 工作区归属 / Home / 槽位占用按服务层契约映射。

真实路由函数 + 真实服务代码全程在测；DB 层用 ``create_autospec`` 替身
（不连库），真实 Postgres 由 lead 在沙箱外验证。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import AsyncConnection

import src.dalton.access_tiers as access_tiers_mod
import src.dalton.coverage.units as cu_svc
import src.dalton.database.coverage_units as cu_db
import src.dalton.settings as settings_mod
import src.dalton.tools.director_tools as dt_mod
from src.dalton.app import dalton_router
from src.server.utils.api import get_current_user_id

_ORIG_IS_INTERNAL_OR_ADMIN = access_tiers_mod.is_internal_or_admin
_ORIG_CU_GET_DB_CONN = cu_db.get_db_connection


class _ConnCm:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        return None


def _install_tx_conn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    unit_row: dict[str, Any] | None = None,
    readable_ws: set[str] | None = None,
) -> Any:
    import src.dalton.sharing as sharing_mod
    from fastapi import HTTPException

    tx_conn = create_autospec(AsyncConnection, instance=True)

    def _begin_tx(savepoint_name: str | None = None, force_rollback: bool = False) -> _ConnCm:
        return _ConnCm(tx_conn)

    tx_conn.transaction.side_effect = _begin_tx
    ctx = _ConnCm(tx_conn)
    monkeypatch.setattr(
        cu_db,
        "get_db_connection",
        create_autospec(_ORIG_CU_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )
    monkeypatch.setattr(
        "src.dalton.autonomy.schedule.get_db_connection",
        create_autospec(_ORIG_CU_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )
    monkeypatch.setattr(
        "src.dalton.notify.dispatch.get_db_connection",
        create_autospec(_ORIG_CU_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )
    monkeypatch.setattr(
        "src.server.database.automation.get_db_connection",
        create_autospec(_ORIG_CU_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )
    monkeypatch.setattr(
        "src.server.database.automation_executions.get_db_connection",
        create_autospec(_ORIG_CU_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )
    monkeypatch.setattr(
        cu_db,
        "get_coverage_unit",
        create_autospec(cu_db.get_coverage_unit, return_value=unit_row),
    )
    allowed_ws = {WS_ID} if readable_ws is None else set(readable_ws)

    async def _req_ws_access(
        ws_id: Any,
        uid: str,
        *,
        channel: str = "web",
        need: str = "read",
        workspace: dict[str, Any] | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        wid = str(ws_id or "").strip()
        if wid in allowed_ws:
            return {"workspace_id": wid, "user_id": uid}
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(sharing_mod.require_workspace_access, side_effect=_req_ws_access),
    )
    return tx_conn

USER = "user-1"
UNIT_ID = "22222222-2222-2222-2222-222222222222"
WS_ID = "11111111-1111-1111-1111-111111111111"
OTHER_UNIT_ID = "44444444-4444-4444-4444-444444444444"


def _unit_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "unit_id": UNIT_ID,
        "kind": "sector",
        "owner_user_id": USER,
        "slug": "ai-infrastructure",
        "display_name": "AI Infrastructure",
        "workspace_id": None,
        "ticker": None,
        "status": "active",
        "research_tz": None,
        "created_at": "2026-11-01T00:00:00+00:00",
        "updated_at": "2026-11-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


@pytest.fixture(autouse=True)
def _allow_internal_member(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(_ORIG_IS_INTERNAL_OR_ADMIN, return_value=True),
    )


@pytest.fixture()
def client() -> TestClient:
    # 与其余 Dalton 路由测试一致：默认 dalton.yaml enabled=true，
    # _require_dalton_enabled 不拦测试请求。
    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: USER
    app.include_router(dalton_router)
    return TestClient(app, raise_server_exceptions=False)


def _install_flag(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true" if enabled else "false")
    monkeypatch.setattr(
        settings_mod, "is_coverage_units_enabled", lambda f=enabled: f
    )


def _detail(resp: Any) -> dict[str, Any]:
    body = resp.json()
    assert isinstance(body.get("detail"), dict), f"expected structured detail: {body}"
    return body["detail"]


# ---------------------------------------------------------------------------
# 开关关闭：写路径不存在，服务层零调用
# ---------------------------------------------------------------------------


def test_flag_off_write_endpoints_404_and_never_touch_services(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, False)
    # 服务函数一旦被调用就炸：开关关闭必须连服务层入口都不碰。
    for name in (
        "register_coverage_unit",
        "bind_coverage_unit_workspace",
        "unbind_coverage_unit_workspace",
        "archive_coverage_unit",
    ):
        monkeypatch.setattr(
            cu_svc,
            name,
            create_autospec(getattr(cu_svc, name), side_effect=AssertionError(name)),
        )
    insert = create_autospec(
        cu_db.insert_coverage_unit, side_effect=AssertionError("insert")
    )
    monkeypatch.setattr(cu_db, "insert_coverage_unit", insert)

    posts = [
        ("/api/v1/dalton/coverage/units", {"kind": "sector", "slug": "ai"}),
        (f"/api/v1/dalton/coverage/units/{UNIT_ID}/bind", {"workspace_id": WS_ID}),
        (f"/api/v1/dalton/coverage/units/{UNIT_ID}/unbind", None),
        (f"/api/v1/dalton/coverage/units/{UNIT_ID}/archive", None),
    ]
    for path, body in posts:
        resp = client.post(path, json=body)
        assert resp.status_code == 404, f"{path}: {resp.text}"
        assert resp.json()["detail"] == "coverage units disabled"
    insert.assert_not_called()


def test_flag_off_tool_not_mounted_for_either_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for enabled in (False, True):
        monkeypatch.setattr(
            dt_mod, "is_coverage_units_enabled", lambda e=enabled: e
        )
        cos_names = {t.name for t in dt_mod.build_director_tools(role="chief_of_staff")}
        analyst_names = {t.name for t in dt_mod.build_director_tools(role="analyst")}
        if enabled:
            assert "dalton_coverage_unit" in cos_names
            assert "dalton_coverage_unit" not in analyst_names
        else:
            assert "dalton_coverage_unit" not in cos_names
            assert "dalton_coverage_unit" not in analyst_names


# ---------------------------------------------------------------------------
# 登记：真实服务 + DB 层替身
# ---------------------------------------------------------------------------


def test_register_sector_normalizes_and_returns_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    tx_conn = _install_tx_conn(monkeypatch)
    row = _unit_row()
    insert = create_autospec(cu_db.insert_coverage_unit)
    insert.return_value = row
    monkeypatch.setattr(cu_db, "insert_coverage_unit", insert)

    resp = client.post(
        "/api/v1/dalton/coverage/units",
        json={
            "kind": "Sector",
            "slug": "  AI-Infrastructure  ",
            "display_name": "AI Infra",
            "research_tz": "America/New_York",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == row
    insert.assert_awaited_once_with(
        kind="sector",
        owner_user_id=USER,
        lead_analyst_user_id=USER,
        slug="ai-infrastructure",
        display_name="AI Infra",
        ticker=None,
        ticker_key=None,
        research_tz="America/New_York",
        conn=tx_conn,
    )


def test_register_sector_with_ticker_refused_before_db(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    insert = create_autospec(cu_db.insert_coverage_unit)
    monkeypatch.setattr(cu_db, "insert_coverage_unit", insert)

    resp = client.post(
        "/api/v1/dalton/coverage/units",
        json={"kind": "sector", "slug": "ai-infrastructure", "ticker": "NVDA"},
    )
    assert resp.status_code == 400
    assert _detail(resp)["error"] == "sector_ticker_refused"
    insert.assert_not_called()


def test_register_invalid_kind_slug_and_tz_map_to_distinct_codes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    cases = [
        ({"kind": "theme", "slug": "ai"}, "invalid_kind"),
        ({"kind": "sector", "slug": "Bad Slug"}, "invalid_slug"),
        ({"kind": "sector", "slug": "ai", "research_tz": "Not/AZone"}, "invalid_research_tz"),
        ({"kind": "company", "slug": "ex", "ticker": "NOT A TICKER!"}, "invalid_ticker"),
    ]
    for body, code in cases:
        resp = client.post("/api/v1/dalton/coverage/units", json=body)
        assert resp.status_code == 400, f"{body}: {resp.text}"
        assert _detail(resp)["error"] == code


def test_register_slug_conflict_maps_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    _install_tx_conn(monkeypatch)
    insert = create_autospec(
        cu_db.insert_coverage_unit,
        side_effect=ValueError(
            "coverage unit slug already registered for this owner and kind"
        ),
    )
    monkeypatch.setattr(cu_db, "insert_coverage_unit", insert)

    resp = client.post(
        "/api/v1/dalton/coverage/units",
        json={"kind": "sector", "slug": "ai-infrastructure"},
    )
    assert resp.status_code == 409
    assert _detail(resp)["error"] == "slug_conflict"
    insert.assert_awaited_once()


# ---------------------------------------------------------------------------
# 绑定：映射服务层事务校验的全部拒因
# ---------------------------------------------------------------------------


def test_bind_happy_path_calls_service_with_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    row = _unit_row(workspace_id=WS_ID)
    bind = create_autospec(cu_svc.bind_coverage_unit_workspace, return_value=row)
    monkeypatch.setattr(cu_svc, "bind_coverage_unit_workspace", bind)

    resp = client.post(
        f"/api/v1/dalton/coverage/units/{UNIT_ID}/bind", json={"workspace_id": WS_ID}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == row
    bind.assert_awaited_once_with(UNIT_ID, WS_ID, USER)


@pytest.mark.parametrize(
    ("service_error", "status", "code"),
    [
        ("workspace belongs to another user", 403, "workspace_not_owned"),
        ("the Home workspace never binds a coverage unit", 400, "home_workspace_refused"),
        ("workspace already bound to another active coverage unit", 409, "workspace_conflict"),
        ("coverage unit already bound to another workspace; 请先解绑再绑定到新的工作区", 409, "rebind_not_supported"),
        (
            "coverage unit still has active automations or unstarted executions: 请先关闭该单元的跟踪（会暂停相关任务），再解绑（相关任务：dalton-nvda-watch）",
            409,
            "active_tracking_blocks_unbind",
        ),
        (f"workspace {WS_ID} does not exist or is deleted", 404, "workspace_not_found"),
        (f"active coverage unit {UNIT_ID} not found for this owner", 404, "unit_not_found"),
    ],
)
def test_bind_refusals_map_to_explicit_4xx(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    service_error: str,
    status: int,
    code: str,
) -> None:
    _install_flag(monkeypatch, True)
    monkeypatch.setattr(
        cu_svc,
        "bind_coverage_unit_workspace",
        create_autospec(
            cu_svc.bind_coverage_unit_workspace,
            side_effect=ValueError(service_error),
        ),
    )


    resp = client.post(
        f"/api/v1/dalton/coverage/units/{UNIT_ID}/bind", json={"workspace_id": WS_ID}
    )
    assert resp.status_code == status, resp.text
    assert _detail(resp) == {"error": code, "message": service_error}


def test_bind_non_uuid_ids_refused_by_real_service_before_db(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    bind = create_autospec(cu_db.bind_coverage_unit_workspace)
    monkeypatch.setattr(cu_db, "bind_coverage_unit_workspace", bind)

    resp = client.post(
        "/api/v1/dalton/coverage/units/not-a-uuid/bind",
        json={"workspace_id": WS_ID},
    )
    assert resp.status_code == 400
    assert _detail(resp)["error"] == "invalid_id"
    bind.assert_not_called()


# ---------------------------------------------------------------------------
# 解绑 / 归档
# ---------------------------------------------------------------------------


def test_unbind_happy_and_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    tx_conn = _install_tx_conn(monkeypatch)
    row = _unit_row(workspace_id=None)
    db_unbind = create_autospec(cu_db.unbind_coverage_unit_workspace, return_value=row)
    monkeypatch.setattr(cu_db, "unbind_coverage_unit_workspace", db_unbind)

    resp = client.post(f"/api/v1/dalton/coverage/units/{UNIT_ID}/unbind")
    assert resp.status_code == 200, resp.text
    assert resp.json() == row
    db_unbind.assert_awaited_once_with(
        UNIT_ID, USER, conn=tx_conn, previous_out={}
    )

    db_unbind.return_value = None
    resp_missing = client.post(
        f"/api/v1/dalton/coverage/units/{OTHER_UNIT_ID}/unbind"
    )
    assert resp_missing.status_code == 404
    assert _detail(resp_missing)["error"] == "unit_not_found"


def test_archive_happy_and_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    tx_conn = _install_tx_conn(monkeypatch)
    row = _unit_row(status="archived")
    db_archive = create_autospec(cu_db.set_coverage_unit_status, return_value=row)
    monkeypatch.setattr(cu_db, "set_coverage_unit_status", db_archive)

    resp = client.post(f"/api/v1/dalton/coverage/units/{UNIT_ID}/archive")
    assert resp.status_code == 200, resp.text
    assert resp.json() == row
    db_archive.assert_awaited_once_with(
        UNIT_ID, USER, "archived", conn=tx_conn, previous_out={}
    )

    db_archive.return_value = None
    resp_missing = client.post(
        f"/api/v1/dalton/coverage/units/{OTHER_UNIT_ID}/archive"
    )
    assert resp_missing.status_code == 404
    assert _detail(resp_missing)["error"] == "unit_not_found"


# ---------------------------------------------------------------------------
# 工具：真实入口（build_director_tools + ainvoke）
# ---------------------------------------------------------------------------


def _tool_map(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(dt_mod, "is_coverage_units_enabled", lambda: True)
    return {t.name: t for t in dt_mod.build_director_tools(role="chief_of_staff")}


@pytest.mark.asyncio
async def test_tool_register_calls_service_with_config_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _tool_map(monkeypatch)
    register = create_autospec(
        dt_mod.register_coverage_unit, return_value=_unit_row()
    )
    monkeypatch.setattr(dt_mod, "register_coverage_unit", register)

    out = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "register", "kind": "sector", "slug": "ai-infrastructure"},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert out["ok"] is True
    assert out["unit"]["unit_id"] == UNIT_ID
    register.assert_awaited_once_with(
        USER,
        kind="sector",
        slug="ai-infrastructure",
        display_name="",
        ticker=None,
        research_tz=None,
    )


@pytest.mark.asyncio
async def test_tool_bind_sector_unit_returns_init_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _tool_map(monkeypatch)
    monkeypatch.setattr(
        dt_mod,
        "bind_coverage_unit_workspace",
        create_autospec(
            dt_mod.bind_coverage_unit_workspace,
            return_value=_unit_row(workspace_id=WS_ID),
        ),
    )


    out = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {
                "action": "bind",
                "unit_id": UNIT_ID,
                "workspace_id": WS_ID,
            },
            config={"configurable": {"user_id": USER}},
        )
    )
    assert out["ok"] is True
    assert out["unit"]["workspace_id"] == WS_ID
    # 提示（不强制）：绑定行业单元后建议 dalton-init-sector
    assert "dalton-init-sector" in out["hint"]


@pytest.mark.asyncio
async def test_tool_bind_refusal_returns_error_code_not_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _tool_map(monkeypatch)
    monkeypatch.setattr(
        dt_mod,
        "bind_coverage_unit_workspace",
        create_autospec(
            dt_mod.bind_coverage_unit_workspace,
            side_effect=ValueError("workspace belongs to another user"),
        ),
    )

    out = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "bind", "unit_id": UNIT_ID, "workspace_id": WS_ID},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert out["ok"] is False
    assert out["error"] == "workspace_not_owned"
    assert out["message"] == "workspace belongs to another user"


@pytest.mark.asyncio
async def test_tool_register_sector_ticker_refused_through_real_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实工具 → 真实服务：sector 带 ticker 在触库前被拒。"""
    tools = _tool_map(monkeypatch)
    insert = create_autospec(cu_db.insert_coverage_unit)
    monkeypatch.setattr(cu_db, "insert_coverage_unit", insert)

    out = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "register", "kind": "sector", "slug": "ai", "ticker": "NVDA"},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert out["ok"] is False
    assert out["error"] == "sector_ticker_refused"
    insert.assert_not_called()


@pytest.mark.asyncio
async def test_tool_list_and_invalid_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _tool_map(monkeypatch)
    list_units = create_autospec(
        dt_mod.get_registered_units, return_value=[_unit_row()]
    )
    monkeypatch.setattr(dt_mod, "get_registered_units", list_units)

    out = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "list"},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert out["ok"] is True
    assert [u["unit_id"] for u in out["units"]] == [UNIT_ID]
    list_units.assert_awaited_once_with(USER, include_archived=False)

    bad = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "explode"},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert bad["ok"] is False
    assert bad["error"] == "invalid_action"


@pytest.mark.asyncio
async def test_tool_flag_flipped_off_after_mount_returns_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关在挂载后关闭：已建的工具实例不得再写注册表。"""
    tools = _tool_map(monkeypatch)
    monkeypatch.setattr(dt_mod, "is_coverage_units_enabled", lambda: False)

    out = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "list"},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert out["ok"] is False
    assert out["error"] == "coverage_units_disabled"


def test_external_user_forbidden_across_all_coverage_routes_and_tool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only internal/admin team members can list, read, register, bind, unbind, or archive coverage units."""
    import asyncio

    _install_flag(monkeypatch, True)
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(_ORIG_IS_INTERNAL_OR_ADMIN, return_value=False),
    )

    assert client.get("/api/v1/dalton/coverage/units").status_code == 403
    assert client.get("/api/v1/dalton/coverage/board").status_code == 403
    assert client.get(f"/api/v1/dalton/coverage/units/{UNIT_ID}").status_code == 403
    assert (
        client.post(
            "/api/v1/dalton/coverage/units",
            json={"kind": "sector", "slug": "ai"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/dalton/coverage/units/{UNIT_ID}/bind",
            json={"workspace_id": WS_ID},
        ).status_code
        == 403
    )
    assert (
        client.post(f"/api/v1/dalton/coverage/units/{UNIT_ID}/unbind").status_code == 403
    )
    assert (
        client.post(f"/api/v1/dalton/coverage/units/{UNIT_ID}/archive").status_code
        == 403
    )

    tools = _tool_map(monkeypatch)
    tool_out = json.loads(
        asyncio.run(
            tools["dalton_coverage_unit"].ainvoke(
                {"action": "list"},
                config={"configurable": {"user_id": USER}},
            )
        )
    )
    assert tool_out["ok"] is False
    assert tool_out["error"] == "forbidden"

    board_tool_out = json.loads(
        asyncio.run(
            tools["dalton_coverage_board"].ainvoke(
                {},
                config={"configurable": {"user_id": USER}},
            )
        )
    )
    assert board_tool_out["ok"] is False
    assert board_tool_out["error"] == "forbidden"


def test_conflict_detail_mapping(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    _install_tx_conn(monkeypatch)

    conflict_exc = cu_db.CoverageUnitConflictError(
        "coverage unit ticker already registered for an active company",
        conflict={"unit_id": OTHER_UNIT_ID, "lead_analyst_user_id": "user-2"},
    )
    monkeypatch.setattr(
        cu_db,
        "insert_coverage_unit",
        create_autospec(cu_db.insert_coverage_unit, side_effect=conflict_exc),
    )
    resp_409 = client.post(
        "/api/v1/dalton/coverage/units",
        json={"kind": "company", "slug": "nvda", "ticker": "NVDA"},
    )
    assert resp_409.status_code == 409
    detail_409 = _detail(resp_409)
    assert detail_409["error"] == "ticker_conflict"
    assert detail_409["unit_id"] == OTHER_UNIT_ID
    assert detail_409["lead_analyst_user_id"] == "user-2"
    assert "workspace_id" not in detail_409


@pytest.mark.asyncio
async def test_tool_list_redacts_unreadable_workspace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C8 + Round 8 Item 2: dalton_coverage_unit(action='list') reuses clear_unreadable_unit_identity,
    clearing workspace_id, owner_user_id, lead_analyst_user_id, and created_by_user_id."""
    from fastapi import HTTPException

    tools = _tool_map(monkeypatch)
    hidden_ws = "99999999-9999-9999-9999-999999999999"
    secret_owner = "u-secret-owner"
    secret_lead = "u-secret-lead"
    secret_creator = "u-secret-creator"
    unit_with_hidden_ws = _unit_row(
        unit_id=UNIT_ID,
        workspace_id=hidden_ws,
        owner_user_id=secret_owner,
        lead_analyst_user_id=secret_lead,
        created_by_user_id=secret_creator,
    )
    unit_with_visible_ws = _unit_row(unit_id=OTHER_UNIT_ID, slug="semi", workspace_id=WS_ID)
    list_units = create_autospec(
        dt_mod.get_registered_units,
        return_value=[unit_with_hidden_ws, unit_with_visible_ws],
    )
    monkeypatch.setattr(dt_mod, "get_registered_units", list_units)

    async def _verify(ws_id: str, uid: str, *, channel: str = "web", need: str = "read", **kw: Any) -> dict[str, Any]:
        if ws_id == WS_ID:
            return {"workspace_id": WS_ID}
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(
        dt_mod,
        "verify_workspace_access",
        create_autospec(dt_mod.verify_workspace_access, side_effect=_verify),
    )

    out = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "list"},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert out["ok"] is True
    by_id = {u["unit_id"]: u for u in out["units"]}
    hidden_entry = by_id[UNIT_ID]
    assert hidden_entry["readable"] is False
    for field in ("workspace_id", "owner_user_id", "lead_analyst_user_id", "created_by_user_id"):
        assert hidden_entry[field] is None
    dumped_hidden = json.dumps(hidden_entry, ensure_ascii=False)
    for secret in (hidden_ws, secret_owner, secret_lead, secret_creator):
        assert secret not in dumped_hidden
    assert by_id[OTHER_UNIT_ID]["readable"] is True
    assert by_id[OTHER_UNIT_ID]["workspace_id"] == WS_ID


@pytest.mark.asyncio
async def test_write_paths_never_leak_unreadable_ws_uuid_in_success_or_errors(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 1: refused bind and PM bind/archive of unreadable ws never return the workspace UUID."""
    _install_flag(monkeypatch, True)
    hidden_ws = "99999999-9999-9999-9999-999999999999"
    _install_tx_conn(monkeypatch, readable_ws=set())
    orig_bind = cu_db.bind_coverage_unit_workspace

    bind_mock = create_autospec(
        orig_bind,
        side_effect=ValueError("workspace does not exist or is hidden"),
    )
    monkeypatch.setattr(cu_db, "bind_coverage_unit_workspace", bind_mock)
    refused = client.post(
        f"/api/v1/dalton/coverage/units/{UNIT_ID}/bind",
        json={"workspace_id": hidden_ws},
    )
    assert refused.status_code == 404
    assert hidden_ws not in refused.text

    # Explicit PM archiving a bound unit whose workspace is unreadable on Web gets workspace_id=None
    monkeypatch.setattr(
        cu_db,
        "set_coverage_unit_status",
        create_autospec(
            cu_db.set_coverage_unit_status,
            return_value=_unit_row(workspace_id=hidden_ws, status="archived"),
        ),
    )
    arch = client.post(f"/api/v1/dalton/coverage/units/{UNIT_ID}/archive")
    assert arch.status_code == 200
    assert arch.json()["workspace_id"] is None
    assert hidden_ws not in arch.text

    # Tool bind response & sector hint also redact unreadable workspace_id
    monkeypatch.setattr(
        cu_db,
        "bind_coverage_unit_workspace",
        create_autospec(
            orig_bind,
            return_value=_unit_row(kind="sector", workspace_id=hidden_ws),
        ),
    )
    tools = _tool_map(monkeypatch)
    tool_out = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "bind", "unit_id": UNIT_ID, "workspace_id": hidden_ws},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert tool_out["ok"] is True
    assert tool_out["unit"]["workspace_id"] is None
    assert hidden_ws not in json.dumps(tool_out, ensure_ascii=False)


@pytest.mark.asyncio
async def test_pm_bind_unreadable_workspace_redacts_workspace_and_derived_owners(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6 Item 5: when PM binds or rebinds to a workspace the caller lacks Web read
    access on, workspace_id, owner_user_id, and lead_analyst_user_id are all redacted to None."""
    _install_flag(monkeypatch, True)
    _install_tx_conn(monkeypatch, readable_ws=set())

    hidden_ws = "88888888-8888-4888-8888-888888888888"
    secret_owner = "u-secret-owner"
    secret_lead = "u-secret-lead"
    returned_row = _unit_row(
        kind="sector",
        workspace_id=hidden_ws,
        owner_user_id=secret_owner,
        lead_analyst_user_id=secret_lead,
    )
    monkeypatch.setattr(
        cu_db,
        "bind_coverage_unit_workspace",
        create_autospec(cu_db.bind_coverage_unit_workspace, return_value=returned_row),
    )

    resp = client.post(
        f"/api/v1/dalton/coverage/units/{UNIT_ID}/bind",
        json={"workspace_id": hidden_ws},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace_id"] is None
    assert body["owner_user_id"] is None
    assert body["lead_analyst_user_id"] is None
    dumped = json.dumps(body, ensure_ascii=False)
    for leak in (hidden_ws, secret_owner, secret_lead):
        assert leak not in dumped

    tools = _tool_map(monkeypatch)
    tool_out = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "bind", "unit_id": UNIT_ID, "workspace_id": hidden_ws},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert tool_out["ok"] is True
    assert tool_out["unit"]["workspace_id"] is None
    assert tool_out["unit"]["owner_user_id"] is None
    assert tool_out["unit"]["lead_analyst_user_id"] is None
    tool_dumped = json.dumps(tool_out, ensure_ascii=False)
    for leak in (hidden_ws, secret_owner, secret_lead):
        assert leak not in tool_dumped


@pytest.mark.asyncio
async def test_external_user_flag_off_rest_and_board_tool_match_baseline(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 6: when coverage_units.enabled is off, external users get their own board,
    200 {enabled: false} on /coverage/units, 404 on detail/writes, and dalton_coverage_board works."""
    import src.dalton.autonomy.board as board_mod

    _install_flag(monkeypatch, False)
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(_ORIG_IS_INTERNAL_OR_ADMIN, return_value=False),
    )
    fake_board = {
        "date": "2026-11-20",
        "tz": "America/New_York",
        "budget": {"remaining_usd": 10.0},
        "companies": [{"ticker": "AAPL", "workspace_id": "ws-ext"}],
        "candidates": [],
        "est_cost_by_kind": {},
    }
    monkeypatch.setattr(
        dt_mod,
        "build_coverage_board",
        create_autospec(board_mod.build_coverage_board, return_value=fake_board),
    )
    monkeypatch.setattr(
        board_mod,
        "build_coverage_board",
        create_autospec(board_mod.build_coverage_board, return_value=fake_board),
    )

    units_resp = client.get("/api/v1/dalton/coverage/units")
    assert units_resp.status_code == 200
    assert units_resp.json() == {"enabled": False}

    detail_resp = client.get(f"/api/v1/dalton/coverage/units/{UNIT_ID}")
    assert detail_resp.status_code == 404

    board_resp = client.get("/api/v1/dalton/coverage/board")
    assert board_resp.status_code == 200
    assert board_resp.json()["companies"] == fake_board["companies"]

    tools = _tool_map(monkeypatch)
    tool_board = json.loads(
        await tools["dalton_coverage_board"].ainvoke(
            {}, config={"configurable": {"user_id": USER}}
        )
    )
    assert tool_board["companies"] == fake_board["companies"]


@pytest.mark.asyncio
async def test_unbind_response_clears_all_identity_fields_when_pre_unbind_workspace_unreadable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 7 Item 3: unbind checks readability against pre-unbind workspace_id
    and clears workspace_id, owner_user_id, lead_analyst_user_id, created_by_user_id."""
    _install_flag(monkeypatch, True)
    hidden_ws = "88888888-8888-4888-8888-888888888888"
    secret_owner = "u-secret-owner"
    secret_lead = "u-secret-lead"
    secret_creator = "u-secret-creator"
    pre_unbind_row = _unit_row(
        kind="sector",
        workspace_id=hidden_ws,
        owner_user_id=secret_owner,
        lead_analyst_user_id=secret_lead,
        created_by_user_id=secret_creator,
    )
    unbound_row = {**pre_unbind_row, "workspace_id": None}
    _install_tx_conn(monkeypatch, unit_row=pre_unbind_row, readable_ws=set())
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("DALTON_PM_USER_IDS", USER)

    async def _fake_unbind(
        uid_arg: str,
        actor_uid: str,
        *,
        conn: Any = None,
        previous_out: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if previous_out is not None:
            previous_out.clear()
            previous_out.update(pre_unbind_row)
        return dict(unbound_row)

    monkeypatch.setattr(
        cu_db,
        "unbind_coverage_unit_workspace",
        create_autospec(cu_db.unbind_coverage_unit_workspace, side_effect=_fake_unbind),
    )

    resp = client.post(f"/api/v1/dalton/coverage/units/{UNIT_ID}/unbind", json={})
    assert resp.status_code == 200
    body = resp.json()
    for field in ("workspace_id", "owner_user_id", "lead_analyst_user_id", "created_by_user_id"):
        assert body[field] is None
    dumped = json.dumps(body, ensure_ascii=False)
    for secret in (hidden_ws, secret_owner, secret_lead, secret_creator):
        assert secret not in dumped

    tools = _tool_map(monkeypatch)
    tool_out = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "unbind", "unit_id": UNIT_ID},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert tool_out["ok"] is True
    for field in ("workspace_id", "owner_user_id", "lead_analyst_user_id", "created_by_user_id"):
        assert tool_out["unit"][field] is None
    tool_dumped = json.dumps(tool_out, ensure_ascii=False)
    for secret in (hidden_ws, secret_owner, secret_lead, secret_creator):
        assert secret not in tool_dumped


@pytest.mark.asyncio
async def test_rebind_unreadable_bound_unit_returns_404_indistinguishable_from_missing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 7 Item 7b: non-PM internal user trying to bind an unreadable unit to
    their own writable workspace or a nonexistent workspace gets 404 unit_not_found."""
    import src.dalton.sharing as sharing_mod
    from fastapi import HTTPException
    from psycopg import AsyncConnection

    _install_flag(monkeypatch, True)
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.delenv("DALTON_PM_USER_IDS", raising=False)
    caller_writable_ws = "11111111-1111-4111-8111-111111111111"
    missing_ws = "99999999-9999-4999-8999-999999999999"
    hidden_prev_ws = "88888888-8888-4888-8888-888888888888"
    foreign_unit = _unit_row(
        unit_id=UNIT_ID,
        kind="sector",
        owner_user_id="other-owner",
        lead_analyst_user_id="other-owner",
        workspace_id=hidden_prev_ws,
    )
    ws_table = {
        caller_writable_ws: {"workspace_id": caller_writable_ws, "user_id": USER, "is_home": False, "status": "stopped", "config": {}},
        hidden_prev_ws: {"workspace_id": hidden_prev_ws, "user_id": "other-owner", "is_home": False, "status": "stopped", "config": {}},
    }

    class _FakeCur:
        def __init__(self) -> None:
            self._rows: list[Any] = []

        async def __aenter__(self) -> _FakeCur:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: str, params: Any = None) -> None:
            q = " ".join(str(sql).split())
            if "FROM dalton_coverage_units WHERE unit_id =" in q and "FOR UPDATE" in q:
                self._rows = [foreign_unit] if str(params[0]) == UNIT_ID else []
            elif "FROM workspaces w" in q and "ANY(%s::uuid[])" in q:
                wanted = {str(x).lower() for x in (params[0] if params else [])}
                self._rows = [dict(ws_table[k]) for k in ws_table if k.lower() in wanted]
            elif "FROM workspaces WHERE workspace_id =" in q:
                wid = str(params[0])
                self._rows = [dict(ws_table[wid])] if wid in ws_table else []
            else:
                self._rows = []

        async def fetchone(self) -> Any:
            return self._rows[0] if self._rows else None

        async def fetchall(self) -> list[Any]:
            return list(self._rows)

    fake_cur = _FakeCur()
    tx_conn = create_autospec(AsyncConnection, instance=True)
    tx_conn.cursor.side_effect = lambda *a, **kw: fake_cur
    tx_conn.transaction.side_effect = lambda *a, **kw: _ConnCm(tx_conn)
    ctx = _ConnCm(tx_conn)
    monkeypatch.setattr(
        cu_db,
        "get_db_connection",
        create_autospec(_ORIG_CU_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )
    monkeypatch.setattr(
        "src.dalton.autonomy.schedule.get_db_connection",
        create_autospec(_ORIG_CU_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )
    monkeypatch.setattr(
        "src.dalton.notify.dispatch.get_db_connection",
        create_autospec(_ORIG_CU_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )
    monkeypatch.setattr(
        "src.server.database.automation.get_db_connection",
        create_autospec(_ORIG_CU_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )
    monkeypatch.setattr(
        "src.server.database.automation_executions.get_db_connection",
        create_autospec(_ORIG_CU_GET_DB_CONN, side_effect=lambda conn=None: ctx),
    )

    orig_req = sharing_mod.require_workspace_access
    monkeypatch.setattr(
        access_tiers_mod,
        "classify_owner_tier",
        create_autospec(access_tiers_mod.classify_owner_tier, return_value="internal"),
    )

    async def _req_ws(ws_id: Any, uid: str, *, need: str = "read", **kwargs: Any) -> dict[str, Any]:
        wid = str(ws_id.get("workspace_id") if isinstance(ws_id, dict) else ws_id)
        if wid == caller_writable_ws and str(uid) == USER:
            return {"workspace_id": caller_writable_ws, "user_id": USER}
        raise HTTPException(status_code=404, detail="Workspace not found")

    monkeypatch.setattr(sharing_mod, "require_workspace_access", create_autospec(orig_req, side_effect=_req_ws))

    resp_missing_unit = client.post(
        "/api/v1/dalton/coverage/units/00000000-0000-4000-8000-000000000099/bind",
        json={"workspace_id": caller_writable_ws},
    )
    resp_writable_target = client.post(
        f"/api/v1/dalton/coverage/units/{UNIT_ID}/bind",
        json={"workspace_id": caller_writable_ws},
    )
    resp_missing_target = client.post(
        f"/api/v1/dalton/coverage/units/{UNIT_ID}/bind",
        json={"workspace_id": missing_ws},
    )

    assert resp_missing_unit.status_code == 404
    assert resp_writable_target.status_code == 404
    assert resp_missing_target.status_code == 404
    assert resp_writable_target.json()["detail"]["error"] == "unit_not_found"
    assert resp_missing_target.json()["detail"]["error"] == "unit_not_found"
    assert resp_writable_target.json()["detail"]["error"] == resp_missing_unit.json()["detail"]["error"]
    assert hidden_prev_ws not in resp_writable_target.text
    assert hidden_prev_ws not in resp_missing_target.text


@pytest.mark.asyncio
async def test_round9_rest_and_tool_refuse_rebind_and_active_tracking_unbind_archive(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Items 1 & 2 via REST and `dalton_coverage_unit` tool."""
    import src.dalton.coverage.service as cov_svc
    import src.dalton.database.coverage as cov_db

    _install_flag(monkeypatch, True)
    other_ws = "99999999-9999-4999-8999-999999999998"
    bound_row = _unit_row(
        kind="company",
        slug="nvda",
        display_name="NVIDIA",
        ticker="NVDA",
        ticker_key="NVDA",
        workspace_id=WS_ID,
        lead_analyst_user_id=USER,
    )
    blocking_names = [
        {"user_id": USER, "name": "dalton-nvda-watch", "metadata": {"dalton_kind": "company_watch", "ticker": "NVDA"}},
        {"user_id": USER, "name": "dalton-nvda-earn-2026q4-t0", "metadata": {"dalton_kind": "earnings_t0", "ticker": "NVDA"}},
    ]

    class _R9Cur:
        def __init__(self) -> None:
            self._rows: list[Any] = []

        async def __aenter__(self) -> _R9Cur:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: str, params: Any = None) -> None:
            q = " ".join(str(sql).split())
            if "FROM dalton_coverage_units" in q and "WHERE unit_id = %s::uuid" in q:
                self._rows = [dict(bound_row)]
            elif "FROM workspaces" in q:
                wanted = {WS_ID, other_ws}
                req = (
                    params[0]
                    if params and isinstance(params[0], list)
                    else ([params[0]] if params else [])
                )
                self._rows = [
                    {
                        "workspace_id": wid,
                        "user_id": USER,
                        "status": "active",
                        "config": {},
                    }
                    for wid in req
                    if str(wid) in wanted
                ]
            elif "FROM automations" in q:
                self._rows = list(blocking_names)
            elif q.startswith("UPDATE dalton_coverage_units"):
                if "SET workspace_id = NULL" in q:
                    bound_row["workspace_id"] = None
                elif "SET status" in q:
                    bound_row["status"] = str(params[0])
                self._rows = [dict(bound_row)]
            else:
                self._rows = []

        async def fetchone(self) -> Any:
            return self._rows[0] if self._rows else None

        async def fetchall(self) -> list[Any]:
            return list(self._rows)

    tx_conn = _install_tx_conn(monkeypatch, unit_row=bound_row, readable_ws={WS_ID, other_ws})
    tx_conn.cursor.side_effect = lambda *a, **kw: _R9Cur()
    monkeypatch.setattr(
        cov_db,
        "acquire_coverage_advisory_lock",
        create_autospec(cov_db.acquire_coverage_advisory_lock, return_value=None),
    )
    monkeypatch.setattr(
        cov_svc,
        "clear_unit_from_personal_coverage",
        create_autospec(cov_svc.clear_unit_from_personal_coverage, return_value=None),
    )
    monkeypatch.setattr(
        cu_db,
        "_require_ws_write",
        create_autospec(cu_db._require_ws_write, return_value=None),
    )

    tools = _tool_map(monkeypatch)

    # 1. Rebind to different workspace -> 409 rebind_not_supported (REST & tool); same workspace -> 200
    rebind_resp = client.post(
        f"/api/v1/dalton/coverage/units/{UNIT_ID}/bind",
        json={"workspace_id": other_ws},
    )
    assert rebind_resp.status_code == 409
    assert _detail(rebind_resp)["error"] == "rebind_not_supported"

    tool_rebind = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "bind", "unit_id": UNIT_ID, "workspace_id": other_ws},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert tool_rebind["ok"] is False and tool_rebind["error"] == "rebind_not_supported"

    same_resp = client.post(
        f"/api/v1/dalton/coverage/units/{UNIT_ID}/bind",
        json={"workspace_id": WS_ID},
    )
    assert same_resp.status_code == 200 and same_resp.json()["workspace_id"] == WS_ID

    same_unhyphenated = client.post(
        f"/api/v1/dalton/coverage/units/{UNIT_ID}/bind",
        json={"workspace_id": WS_ID.replace("-", "").upper()},
    )
    assert same_unhyphenated.status_code == 200 and same_unhyphenated.json()["workspace_id"] == WS_ID

    # 2. Unbind & archive while active automations / unstarted executions exist -> 409 active_tracking_blocks_unbind
    unbind_resp = client.post(f"/api/v1/dalton/coverage/units/{UNIT_ID}/unbind")
    assert unbind_resp.status_code == 409
    assert _detail(unbind_resp)["error"] == "active_tracking_blocks_unbind"
    assert "请先关闭该单元的跟踪（会暂停相关任务），再解绑" in _detail(unbind_resp)["message"]
    assert "dalton-nvda-earn-2026q4-t0" in _detail(unbind_resp)["message"]
    assert _detail(unbind_resp)["blocking_automations"] == [
        "dalton-nvda-watch",
        "dalton-nvda-earn-2026q4-t0",
    ]
    assert bound_row["workspace_id"] == WS_ID

    archive_resp = client.post(f"/api/v1/dalton/coverage/units/{UNIT_ID}/archive")
    assert archive_resp.status_code == 409
    assert _detail(archive_resp)["error"] == "active_tracking_blocks_unbind"
    assert bound_row["status"] == "active"

    tool_unbind = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "unbind", "unit_id": UNIT_ID},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert tool_unbind["ok"] is False and tool_unbind["error"] == "active_tracking_blocks_unbind"
    assert tool_unbind["blocking_automations"] == [
        "dalton-nvda-watch",
        "dalton-nvda-earn-2026q4-t0",
    ]

    tool_archive = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "archive", "unit_id": UNIT_ID},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert tool_archive["ok"] is False and tool_archive["error"] == "active_tracking_blocks_unbind"

    # 2b. Without Web read access on bound workspace, neither REST nor tool leaks blocking task names
    from fastapi import HTTPException
    import src.dalton.sharing as sharing_mod

    monkeypatch.setattr(
        sharing_mod.require_workspace_access,
        "side_effect",
        HTTPException(status_code=404, detail="Workspace not found"),
    )
    unbind_hidden = client.post(f"/api/v1/dalton/coverage/units/{UNIT_ID}/unbind")
    assert unbind_hidden.status_code == 409
    assert "该单元下仍有 2 个运行中的受管任务" in _detail(unbind_hidden)["message"]
    assert "dalton-nvda-watch" not in unbind_hidden.text
    assert "blocking_automations" not in _detail(unbind_hidden)
    assert _detail(unbind_hidden)["blocking_automations_count"] == 2

    tool_hidden = json.loads(
        await tools["dalton_coverage_unit"].ainvoke(
            {"action": "unbind", "unit_id": UNIT_ID},
            config={"configurable": {"user_id": USER}},
        )
    )
    assert tool_hidden["ok"] is False and tool_hidden["error"] == "active_tracking_blocks_unbind"
    assert "该单元下仍有 2 个运行中的受管任务" in tool_hidden["message"]
    assert "dalton-nvda-watch" not in json.dumps(tool_hidden, ensure_ascii=False)
    assert tool_hidden["blocking_automations_count"] == 2

    # 3. After tracking is disabled (0 active/pending rows), unbind succeeds
    blocking_names.clear()
    unbind_ok = client.post(f"/api/v1/dalton/coverage/units/{UNIT_ID}/unbind")
    assert unbind_ok.status_code == 200
    assert unbind_ok.json()["workspace_id"] is None
