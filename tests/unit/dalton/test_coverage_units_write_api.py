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

import src.dalton.coverage.units as cu_svc
import src.dalton.database.coverage_units as cu_db
import src.dalton.settings as settings_mod
import src.dalton.tools.director_tools as dt_mod
from src.dalton.app import dalton_router
from src.server.utils.api import get_current_user_id

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


@pytest.fixture()
def client() -> TestClient:
    # 与其余 Dalton 路由测试一致：默认 dalton.yaml enabled=true，
    # _require_dalton_enabled 不拦测试请求。
    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: USER
    app.include_router(dalton_router)
    return TestClient(app, raise_server_exceptions=False)


def _install_flag(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
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
        slug="ai-infrastructure",
        display_name="AI Infra",
        ticker=None,
        research_tz="America/New_York",
        conn=None,
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
    row = _unit_row(workspace_id=None)
    db_unbind = create_autospec(cu_db.unbind_coverage_unit_workspace, return_value=row)
    monkeypatch.setattr(cu_db, "unbind_coverage_unit_workspace", db_unbind)

    resp = client.post(f"/api/v1/dalton/coverage/units/{UNIT_ID}/unbind")
    assert resp.status_code == 200, resp.text
    assert resp.json() == row
    db_unbind.assert_awaited_once_with(UNIT_ID, USER, conn=None)

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
    row = _unit_row(status="archived")
    db_archive = create_autospec(cu_db.set_coverage_unit_status, return_value=row)
    monkeypatch.setattr(cu_db, "set_coverage_unit_status", db_archive)

    resp = client.post(f"/api/v1/dalton/coverage/units/{UNIT_ID}/archive")
    assert resp.status_code == 200, resp.text
    assert resp.json() == row
    db_archive.assert_awaited_once_with(UNIT_ID, USER, "archived", conn=None)

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
