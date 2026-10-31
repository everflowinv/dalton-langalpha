"""Sector layer P4: read-only coverage-units API and flag delivery.

锁定的行为：
- 开关关闭：列表 200 + ``{"enabled": false}``（唯一键，前端据此隐藏一切
  行业入口），不触任何注册表查询；详情一律 404；
- 开关开启：列表 = units（与看板 P2 同一构造：三态、成员、同事可见性）+
  unclassified（本人的、未绑 active 单元、非 Home 的工作区）；
- 详情：sector 显示文档摘要 / 议程待办 / 成员 / 证据缺口 / 跨层台账 gap；
  company 显示关联行业 + 待复核待办（origin=sector 的开放问题）；
- 可见性：同事隐藏工作区上的单元 404（不泄漏存在）；台账只对 owner 输出；
- SQL 规则：无 LIKE / 字面量 ``%``，IS NULL 上下文显式 ``::text``，uuid
  比较显式 ``::uuid``。

真实路由函数 + 真实 units_view/board 代码全程在测；DB 层用
``create_autospec`` / SQL-求值替身（不连库），真实 Postgres 由 lead 在
沙箱外验证。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.dalton.autonomy.units_view as units_view_mod
import src.dalton.database.coverage_units as cu_db
import src.dalton.database.cross_unit_requests as cur_db
import src.dalton.database.sector_members as sm_db
import src.dalton.settings as settings_mod
from src.dalton.app import dalton_router
from src.server.database.home_workspace import get_home_workspace_id
from src.server.utils.api import get_current_user_id

USER = "user-1"
COLLEAGUE = "user-2"
WS_SECTOR = "11111111-1111-1111-1111-111111111111"
WS_COMPANY = "33333333-3333-3333-3333-333333333333"
WS_EXTRA = "66666666-6666-6666-6666-666666666666"
HOME_WS = get_home_workspace_id(USER)
COLLEAGUE_VISIBLE_WS = "77777777-7777-7777-7777-777777777777"
COLLEAGUE_HIDDEN_WS = "88888888-8888-8888-8888-888888888888"
UNIT_SECTOR = "22222222-2222-2222-2222-222222222222"
UNIT_COMPANY = "44444444-4444-4444-4444-444444444444"
UNIT_HIDDEN_SECTOR = "99999999-9999-9999-9999-999999999999"


def _sector_unit_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "unit_id": UNIT_SECTOR,
        "kind": "sector",
        "owner_user_id": USER,
        "slug": "ai-infrastructure",
        "display_name": "AI Infrastructure",
        "workspace_id": WS_SECTOR,
        "ticker": None,
        "status": "active",
    }
    row.update(overrides)
    return row


def _company_unit_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "unit_id": UNIT_COMPANY,
        "kind": "company",
        "owner_user_id": USER,
        "slug": "example",
        "display_name": "EXAMPLE",
        "workspace_id": WS_COMPANY,
        "ticker": "EXAMPLE",
        "status": "active",
    }
    row.update(overrides)
    return row


def _sector_doc(version_no: int = 2) -> dict[str, Any]:
    return {
        "doc_key": "sector",
        "version_no": version_no,
        "content": {
            "schema_version": 1,
            "name": "AI Infrastructure",
            "definition": "算力、网络与智算中心的供给链条",
            "one_liner": "算力供给仍紧张",
            "coverage_stage": "monitoring",
            "scope": {"regions": ["全球"], "markets": ["US equities"]},
        },
    }


def _thesis_doc(version_no: int = 3) -> dict[str, Any]:
    return {
        "doc_key": "thesis",
        "version_no": version_no,
        "content": {
            "schema_version": 1,
            "one_liner": "算力缺口延续到 2027",
            "aggregate_status": "intact",
            "house_view": {"stance": "constructive", "conviction": "high"},
            "pillars": [
                {"id": "P1", "claim": "训练需求仍在上行", "priority": "core", "status": "supported"}
            ],
        },
    }


def _drivers_doc(version_no: int = 2) -> dict[str, Any]:
    """Schema-valid drivers 文档（codex p4 r1 #4）：driver 与 indicator 的
    必填字段齐全，latest 读数满足 reading 的全部必填项。测试内用
    validate_schema 复核过合法。"""
    return {
        "doc_key": "drivers",
        "version_no": version_no,
        "content": {
            "schema_version": 1,
            "as_of": "2026-10-20",
            "drivers": [
                {
                    "id": "K1",
                    "name": "全球 AI 服务器出货量",
                    "kind": "volume",
                    "description": "AI 服务器季度出货量（含机柜）",
                    "updated_at": "2026-10-20T00:00:00Z",
                    "leading_indicators": [
                        {
                            "id": "K1.1",
                            "name": "CoWoS 月产能",
                            "source": "TrendForce",
                            "frequency": "monthly",
                            "good_direction": "up",
                            "latest": {
                                "value": 35.0,
                                "unit": "count_mn",
                                "period": "2026-09",
                                "as_of": "2026-10-05T00:00:00Z",
                                "ref": {"kind": "url", "path": "https://example.com/cowos"},
                                "label": "estimate",
                            },
                        },
                        {
                            "id": "K1.2",
                            "name": "尚无读数的 HBM 出货指标",
                            "source": "内部测算",
                            "frequency": "quarterly",
                            "good_direction": "up",
                        },
                    ],
                }
            ],
        },
    }


def _sector_agenda_doc(version_no: int = 4) -> dict[str, Any]:
    return {
        "doc_key": "agenda",
        "version_no": version_no,
        "content": {
            "schema_version": 1,
            "open_questions": [
                {
                    "id": "Q-001",
                    "question": "HBM 供给何时宽松？",
                    "why": "",
                    "value": "high",
                    "urgency": "this_week",
                    "origin": "analyst",
                    "owner": "analyst",
                    "status": "open",
                    "attempts": [],
                    "created_at": "2026-10-01T00:00:00Z",
                }
            ],
            "next_validations": [
                {"id": "V1", "what": "季度 capex 口径核对", "due": "2026-11-05", "status": "open"}
            ],
        },
    }


def _company_agenda_doc(version_no: int = 6) -> dict[str, Any]:
    return {
        "doc_key": "agenda",
        "version_no": version_no,
        "content": {
            "schema_version": 1,
            "open_questions": [
                {
                    "id": "Q-002",
                    "question": "待复核：行业支柱 P1 已更新到 v3，本公司论点仍采用旧版本。",
                    "why": "行业联动",
                    "value": "high",
                    "urgency": "this_week",
                    "origin": "sector",
                    "owner": "analyst",
                    "status": "open",
                    "attempts": [],
                    "created_at": "2026-10-12T00:00:00Z",
                    "source_ref": {
                        "workspace_id": WS_SECTOR,
                        "doc_key": "thesis",
                        "version_no": 3,
                        "entry_id": "P1",
                        "relation": "assumes",
                    },
                },
                {
                    "id": "Q-003",
                    "question": "已解决的老问题",
                    "why": "",
                    "value": "low",
                    "urgency": "someday",
                    "origin": "sector",
                    "owner": "analyst",
                    "status": "resolved",
                    "attempts": [],
                    "created_at": "2026-09-01T00:00:00Z",
                },
            ],
        },
    }


def _member_rows() -> list[dict[str, Any]]:
    return [
        {
            "sector_unit_id": UNIT_SECTOR,
            "member_id": "M1",
            "member_name": "某综合科技公司",
            "ticker": "EXAMPLE",
            "company_unit_id": UNIT_COMPANY,
            "workspace_created": True,
            "scope": "只计 AI 加速卡收入",
            "reason": "",
            "valid_until": None,
            "position": 0,
            "synced_version_no": 2,
        },
        {
            "sector_unit_id": UNIT_SECTOR,
            "member_id": "M2",
            "member_name": "未覆盖的封装厂商",
            "ticker": None,
            "company_unit_id": None,
            "workspace_created": False,
            "scope": "",
            "reason": "",
            "valid_until": None,
            "position": 1,
            "synced_version_no": 2,
        },
    ]


def _make_cursor(
    *,
    unit_rows: list[dict[str, Any]] | None = None,
    ws_rows: list[dict[str, Any]] | None = None,
    auto_rows: list[dict[str, Any]] | None = None,
    kv_rows: list[dict[str, Any]] | None = None,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> tuple[Any, list[str]]:
    """SQL 求值替身：按语句特征返回对应行，并记录全部执行过的 SQL。"""
    unit_rows = unit_rows if unit_rows is not None else []
    ws_rows = ws_rows if ws_rows is not None else []
    auto_rows = auto_rows if auto_rows is not None else []
    kv_rows = kv_rows if kv_rows is not None else []
    ledger_rows = ledger_rows if ledger_rows is not None else []
    executed: list[str] = []
    last: dict[str, Any] = {"sql": "", "params": ()}

    class _Cur:
        description: list[Any] = []

        async def __aenter__(self) -> "_Cur":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: str, params: Any = ()) -> None:
            executed.append(" ".join(str(sql).split()))
            last["sql"] = executed[-1]
            last["params"] = tuple(params or ())

        def _query(self) -> list[dict[str, Any]]:
            sql = last["sql"]
            p = list(last["params"] or [])
            if "FROM workspaces" in sql:
                return ws_rows
            if "FROM automations" in sql:
                if p and "user_id = %s" in sql:
                    return [
                        r for r in auto_rows if str(r.get("user_id")) == str(p[0])
                    ]
                return list(auto_rows)
            if "dalton_knowledge_versions" in sql:
                # _latest_valid_docs: (workspace_id, [doc_keys])
                rows = [
                    r for r in kv_rows if str(r.get("ws", p[0] if p else "")) == str(p[0])
                ] if p else list(kv_rows)
                if len(p) >= 2:
                    keys = {str(k) for k in (p[1] or [])}
                    rows = [r for r in rows if str(r.get("doc_key")) in keys]
                return rows
            if "dalton_cross_unit_requests" in sql:
                # list_pending_for_sector 的 SQL 语义（codex p4 r1 #2）：
                # 先按 owner + 行业（unit 或 workspace）+ 待处理状态过滤，
                # 再排序、LIMIT；COUNT 走同一过滤。参数顺序见 db 函数。
                rows = [
                    r
                    for r in ledger_rows
                    if str(r.get("owner_user_id") or "") == str(p[0])
                ]
                unit_id, ws_id = str(p[1]), str(p[2])
                rows = [
                    r
                    for r in rows
                    if str(r.get("sector_unit_id") or "") == unit_id
                    or str(r.get("sector_workspace_id") or "") == ws_id
                ]
                rows = [
                    r
                    for r in rows
                    if str(r.get("status") or "") in ("gap", "open", "dispatched")
                ]
                rows.sort(
                    key=lambda r: (
                        str(r.get("created_at") or ""),
                        str(r.get("request_id") or ""),
                    ),
                    reverse=True,
                )
                if "COUNT(*)" in sql.upper():
                    return [{"cnt": len(rows)}]
                limit = int(p[3]) if len(p) >= 4 else 50
                return rows[:limit]
            if "FROM dalton_coverage_units" in sql:
                if "unit_id = ANY" in sql:
                    ids = {str(u) for u in (p[0] if p else [])}
                    return [r for r in unit_rows if str(r.get("unit_id")) in ids]
                if "workspace_id = ANY" in sql:
                    ws_ids = {str(u) for u in (p[0] if p else [])}
                    rows = [
                        r for r in unit_rows if str(r.get("workspace_id")) in ws_ids
                    ]
                    if len(p) >= 3 and p[2] is not None:
                        rows = [
                            r
                            for r in rows
                            if str(r.get("owner_user_id")) != str(p[2])
                        ]
                    return rows
                if len(p) >= 2 and p[1] is not None:
                    owner = str(p[1])
                    return [
                        r
                        for r in unit_rows
                        if str(r.get("owner_user_id")) == owner
                        and str(r.get("status")) == "active"
                    ]
                return list(unit_rows)
            return []

        async def fetchall(self) -> list[dict[str, Any]]:
            return self._query()

        async def fetchone(self) -> dict[str, Any] | None:
            rows = self._query()
            return rows[0] if rows else None

    return _Cur(), executed


class _Lease:
    """一次连接租借：退出上下文即归还，之后再用 cursor 就报错。"""

    def __init__(self, cur: Any) -> None:
        self._cur = cur
        self._returned = False

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        if self._returned:
            raise RuntimeError("connection already returned to the pool")
        return self._cur

    async def __aenter__(self) -> "_Lease":
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._returned = True


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: USER
    app.include_router(dalton_router)
    return TestClient(app, raise_server_exceptions=False)


def _install_flag(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(
        settings_mod, "is_coverage_units_enabled", lambda f=enabled: f
    )


def _install_db(
    monkeypatch: pytest.MonkeyPatch,
    cur: Any,
    *,
    units_by_ws: dict[str, dict[str, Any]] | None = None,
    unit_by_id: dict[str, dict[str, Any]] | None = None,
    member_rows: list[dict[str, Any]] | None = None,
    company_members: list[dict[str, Any]] | None = None,
    ledger_rows: list[dict[str, Any]] | None = None,
    team_workspaces: list[dict[str, Any]] | None = None,
) -> None:
    monkeypatch.setattr(units_view_mod, "get_db_connection", lambda: _Lease(cur))
    monkeypatch.setattr(
        units_view_mod,
        "get_coverage_unit",
        create_autospec(
            cu_db.get_coverage_unit,
            side_effect=lambda uid, **kw: (unit_by_id or {}).get(str(uid)),
        ),
    )
    monkeypatch.setattr(
        units_view_mod,
        "load_active_units_by_workspace",
        create_autospec(
            cu_db.load_active_units_by_workspace,
            side_effect=lambda ws_ids, **kw: {
                wid: (units_by_ws or {}).get(str(wid))
                for wid in ws_ids
                if str(wid) in (units_by_ws or {})
            },
        ),
    )
    monkeypatch.setattr(
        sm_db,
        "fetch_members_for_sectors",
        create_autospec(
            sm_db.fetch_members_for_sectors,
            side_effect=lambda c, ids: [
                m for m in (member_rows or []) if str(m.get("sector_unit_id")) in {
                    str(i) for i in ids
                }
            ],
        ),
    )
    monkeypatch.setattr(
        sm_db,
        "list_members_for_company",
        create_autospec(
            sm_db.list_members_for_company,
            side_effect=lambda uid, **kw: [
                m
                for m in (company_members or [])
                if str(m.get("company_unit_id")) == str(uid)
            ],
        ),
    )
    monkeypatch.setattr(
        cur_db,
        "get_db_connection",
        lambda *a, **kw: _Lease(cur),
    )

    async def _fake_team(uid: str) -> list[dict[str, Any]]:
        return team_workspaces or []

    import src.dalton.sharing as sharing_mod

    monkeypatch.setattr(sharing_mod, "list_team_workspaces", _fake_team)


def _default_units_by_ws() -> dict[str, dict[str, Any]]:
    return {WS_SECTOR: _sector_unit_row(), WS_COMPANY: _company_unit_row()}


# ---------------------------------------------------------------------------
# 开关关闭：列表是唯一的开关通道，详情不存在
# ---------------------------------------------------------------------------


def test_flag_off_list_returns_enabled_false_and_queries_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _forbidden_pool() -> Any:
        raise AssertionError("coverage_units disabled must not touch the registry")

    _install_flag(monkeypatch, False)
    monkeypatch.setattr(units_view_mod, "get_db_connection", _forbidden_pool)

    resp = client.get("/api/v1/dalton/coverage/units")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"enabled": False}

    resp_detail = client.get(f"/api/v1/dalton/coverage/units/{UNIT_SECTOR}")
    assert resp_detail.status_code == 404
    assert resp_detail.json()["detail"] == "coverage units disabled"


# ---------------------------------------------------------------------------
# 列表：units（P2 同构）+ unclassified
# ---------------------------------------------------------------------------


def test_list_units_and_unclassified(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_flag(monkeypatch, True)
    unit_rows = [_sector_unit_row(), _company_unit_row()]
    kv_rows: list[dict[str, Any]] = []
    # 未分类：本人的普通工作区进来；Home 与已绑单元的工作区被排除
    ws_rows = [
        {
            "workspace_id": WS_EXTRA,
            "user_id": USER,
            "name": "随便的主题工作区",
            "description": "",
            "updated_at": None,
        },
        {
            "workspace_id": HOME_WS,
            "user_id": USER,
            "name": "Home",
            "description": "",
            "updated_at": None,
        },
    ]
    auto_rows = [
        {"user_id": USER, "workspace_id": WS_COMPANY, "name": "dalton-example-watch", "metadata": {}}
    ]
    cur, executed = _make_cursor(
        unit_rows=unit_rows, ws_rows=ws_rows, auto_rows=auto_rows, kv_rows=kv_rows
    )
    _install_db(
        monkeypatch,
        cur,
        units_by_ws=_default_units_by_ws(),
        unit_by_id={UNIT_SECTOR: _sector_unit_row(), UNIT_COMPANY: _company_unit_row()},
        member_rows=_member_rows(),
    )

    resp = client.get("/api/v1/dalton/coverage/units")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True

    units = {u["unit_id"]: u for u in body["units"]}
    assert set(units) == {UNIT_SECTOR, UNIT_COMPANY}
    sector = units[UNIT_SECTOR]
    assert sector["readable"] is True and sector["delegatable"] is True
    assert sector["auto_tracked"] is False  # 行业工作区没有本人的 dalton-* 自动化
    m1, m2 = sector["members"]
    assert m1["workspace_created"] is True and m1["workspace_id"] == WS_COMPANY
    assert m2["workspace_created"] is False and m2["workspace_id"] is None
    company = units[UNIT_COMPANY]
    assert company["ticker"] == "EXAMPLE"
    assert company["auto_tracked"] is True

    assert [w["workspace_id"] for w in body["unclassified"]] == [WS_EXTRA]

    # SQL 规则：IS NULL 上下文显式 ::text；无 LIKE / 字面量 %
    for s in executed:
        assert "%s IS NULL" not in s, f"untyped NULL param: {s}"
        assert " LIKE " not in s, f"LIKE literal pattern: {s}"
    assert any("%s::text IS NULL" in s for s in executed)


def test_list_colleague_units_only_via_visible_workspaces(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    unit_rows = [
        _sector_unit_row(),
        _sector_unit_row(
            unit_id=UNIT_HIDDEN_SECTOR,
            owner_user_id=COLLEAGUE,
            slug="hidden",
            display_name="Hidden Sector",
            workspace_id=COLLEAGUE_HIDDEN_WS,
        ),
        _sector_unit_row(
            unit_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            owner_user_id=COLLEAGUE,
            slug="digital-ads",
            display_name="Digital Ads",
            workspace_id=COLLEAGUE_VISIBLE_WS,
        ),
    ]
    cur, _ = _make_cursor(unit_rows=unit_rows, ws_rows=[], auto_rows=[], kv_rows=[])
    _install_db(
        monkeypatch,
        cur,
        units_by_ws={},
        unit_by_id={u["unit_id"]: u for u in unit_rows},
        member_rows=[],
        team_workspaces=[
            {
                "user_id": COLLEAGUE,
                "workspaces": [
                    {
                        "workspace_id": COLLEAGUE_VISIBLE_WS,
                        "web_access": "ro",
                        "cos_access": "ro",
                    }
                ],
            }
        ],
    )

    body = client.get("/api/v1/dalton/coverage/units").json()
    ids = [u["unit_id"] for u in body["units"]]
    # 隐藏工作区上的同事单元不出现（不泄漏存在）；可见的照常输出且不可委派
    assert UNIT_HIDDEN_SECTOR not in ids
    colleague_unit = next(
        u for u in body["units"] if u["unit_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )
    assert colleague_unit["readable"] is True
    assert colleague_unit["delegatable"] is False
    assert colleague_unit["auto_tracked"] is False


# ---------------------------------------------------------------------------
# 详情：sector（文档摘要 / 议程待办 / 成员 / 证据缺口 / 台账 gap）
# ---------------------------------------------------------------------------


def test_sector_detail_docs_agenda_members_ledger(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    unit_rows = [_sector_unit_row(), _company_unit_row()]
    kv_rows = [
        {**_sector_doc(), "ws": WS_SECTOR},
        {**_thesis_doc(), "ws": WS_SECTOR},
        {**_drivers_doc(), "ws": WS_SECTOR},
        {**_sector_agenda_doc(), "ws": WS_SECTOR},
    ]
    ledger = [
        {
            "request_id": "77777777-7777-7777-7777-777777777777",
            "owner_user_id": USER,
            "kind": "research_request",
            "sector_unit_id": UNIT_SECTOR,
            "question": "需要 CoWoS 月度产能数据",
            "evidence_needed": [],
            "completion_condition": "拿到 10 月数据",
            "target_workspace_id": None,
            "status": "open",
            "created_at": "2026-10-10T00:00:00+00:00",
            "updated_at": "2026-10-10T00:00:00+00:00",
        },
        {
            "request_id": "88888888-8888-8888-8888-888888888888",
            "owner_user_id": USER,
            "kind": "linkage_retry",
            "sector_unit_id": UNIT_SECTOR,
            "question": "行业支柱 P1 改版后的联动补发",
            "target_workspace_id": WS_COMPANY,
            "status": "gap",
            "created_at": "2026-10-12T00:00:00+00:00",
            "updated_at": "2026-10-12T00:00:00+00:00",
        },
        {
            "request_id": "99999999-9999-9999-9999-999999999988",
            "owner_user_id": USER,
            "kind": "research_request",
            "sector_unit_id": UNIT_HIDDEN_SECTOR,
            "question": "别的行业的缺口不应出现",
            "target_workspace_id": None,
            "status": "open",
            "created_at": "2026-10-12T00:00:00+00:00",
            "updated_at": "2026-10-12T00:00:00+00:00",
        },
    ]
    cur, executed = _make_cursor(
        unit_rows=unit_rows, ws_rows=[], auto_rows=[], kv_rows=kv_rows, ledger_rows=ledger
    )
    _install_db(
        monkeypatch,
        cur,
        units_by_ws=_default_units_by_ws(),
        unit_by_id={UNIT_SECTOR: _sector_unit_row(), UNIT_COMPANY: _company_unit_row()},
        member_rows=_member_rows(),
        ledger_rows=ledger,
    )

    resp = client.get(f"/api/v1/dalton/coverage/units/{UNIT_SECTOR}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "sector"
    assert body["workspace_id"] == WS_SECTOR
    sector = body["sector"]
    assert sector["docs"]["sector"]["one_liner"] == "算力供给仍紧张"
    assert sector["docs"]["thesis"]["one_liner"] == "算力缺口延续到 2027"
    assert sector["docs"]["thesis"]["pillars"][0]["id"] == "P1"
    assert sector["docs"]["drivers"]["drivers"][0]["name"] == "全球 AI 服务器出货量"
    assert sector["agenda_todos"]["questions"][0]["question"] == "HBM 供给何时宽松？"
    assert sector["agenda_todos"]["validations"][0]["due"] == "2026-11-05"
    m1, m2 = sector["members"]
    assert m1["workspace_id"] == WS_COMPANY
    assert m2["workspace_created"] is False
    # 证据缺口 = open 的 research_request；台账 gap = status gap；都只限本行业
    assert [g["question"] for g in sector["evidence_gaps"]] == ["需要 CoWoS 月度产能数据"]
    assert [g["question"] for g in sector["ledger_gaps"]] == [
        "行业支柱 P1 改版后的联动补发"
    ]
    assert sector["pending_total"] == 2
    # 台账读取在 SQL 里先按 owner + 行业 + 待处理状态过滤、再 LIMIT
    # （codex p4 r1 #2）：WHERE 在前、LIMIT 在后，owner 参数首位。
    ledger_sqls = [s for s in executed if "dalton_cross_unit_requests" in s]
    assert len(ledger_sqls) == 2  # COUNT + 行查询
    rows_sql = next(s for s in ledger_sqls if "COUNT" not in s.upper())
    where_pos = rows_sql.index("status IN")
    limit_pos = rows_sql.index("LIMIT")
    assert where_pos < limit_pos
    assert "sector_unit_id = %s::uuid OR sector_workspace_id = %s::uuid" in rows_sql
    assert "'%'" not in rows_sql


def test_sector_detail_colleague_ledger_not_exposed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同事可读的行业：详情可见，但 d026 台账按 owner 隔离，不输出。"""
    _install_flag(monkeypatch, True)
    colleague_sector = _sector_unit_row(
        unit_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        owner_user_id=COLLEAGUE,
        slug="digital-ads",
        display_name="Digital Ads",
        workspace_id=COLLEAGUE_VISIBLE_WS,
    )
    cur, _ = _make_cursor(unit_rows=[colleague_sector], ws_rows=[], auto_rows=[], kv_rows=[])
    _install_db(
        monkeypatch,
        cur,
        units_by_ws={},
        unit_by_id={colleague_sector["unit_id"]: colleague_sector},
        member_rows=[],
        team_workspaces=[
            {
                "user_id": COLLEAGUE,
                "workspaces": [
                    {"workspace_id": COLLEAGUE_VISIBLE_WS, "web_access": "ro", "cos_access": "ro"}
                ],
            }
        ],
    )
    # 台账查询按 owner 隔离：非本人行业整个分支不发（替身按调用计）
    ledger_read = create_autospec(
        cur_db.list_pending_for_sector, return_value=([], 0)
    )
    monkeypatch.setattr(cur_db, "list_pending_for_sector", ledger_read)

    resp = client.get(f"/api/v1/dalton/coverage/units/{colleague_sector['unit_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["readable"] is True and body["delegatable"] is False
    ledger_read.assert_not_called()
    assert body["sector"]["evidence_gaps"] == []
    assert body["sector"]["ledger_gaps"] == []
    assert body["sector"]["pending_total"] == 0


def test_ledger_filters_by_sector_before_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex p4 r1 #2：台账在 SQL 里先按行业过滤再 LIMIT。

    owner 名下有远超窗口的别行业记录（且都比本行业的旧 gap 新）时，
    旧 gap 仍要返回；本行业待处理超过上限时截断并给出 total。替身按
    SQL 语义求值（owner + 行业 + 待处理状态过滤 → 排序 → LIMIT）。
    """
    _install_flag(monkeypatch, True)
    unit_rows = [_sector_unit_row()]
    # 别的行业/已完成的记录 220 条（全部比旧 gap 新）
    ledger: list[dict[str, Any]] = [
        {
            "request_id": f"aaaaaaa1-0000-0000-0000-{i:012d}",
            "owner_user_id": USER,
            "kind": "research_request",
            "sector_unit_id": UNIT_HIDDEN_SECTOR,
            "question": f"别行业记录 {i}",
            "target_workspace_id": None,
            "status": "open",
            "created_at": f"2026-11-{(i % 28) + 1:02d}T00:00:00+00:00",
            "updated_at": f"2026-11-{(i % 28) + 1:02d}T00:00:00+00:00",
        }
        for i in range(220)
    ]
    # 本行业的旧 gap（最早）——先截断后过滤的旧实现永远拿不到它
    old_gap = {
        "request_id": "bbbbbbbb-0000-0000-0000-000000000001",
        "owner_user_id": USER,
        "kind": "linkage_retry",
        "sector_unit_id": UNIT_SECTOR,
        "sector_workspace_id": WS_SECTOR,
        "question": "本行业最旧的 gap",
        "target_workspace_id": WS_COMPANY,
        "status": "gap",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    # 本行业再加 60 条较新的 gap，凑足超过 LIMIT=50 的待处理量
    ledger.append(old_gap)
    ledger.extend(
        {
            "request_id": f"bbbbbbbb-0000-0000-0000-{i:012d}",
            "owner_user_id": USER,
            "kind": "linkage_retry",
            "sector_unit_id": UNIT_SECTOR,
            "question": f"本行业较新 gap {i}",
            "target_workspace_id": WS_COMPANY,
            "status": "gap",
            "created_at": f"2026-10-{(i % 27) + 1:02d}T00:00:00+00:00",
            "updated_at": f"2026-10-{(i % 27) + 1:02d}T00:00:00+00:00",
        }
        for i in range(60)
    )
    cur, _ = _make_cursor(
        unit_rows=unit_rows, ws_rows=[], auto_rows=[], kv_rows=[], ledger_rows=ledger
    )
    _install_db(
        monkeypatch,
        cur,
        units_by_ws={WS_SECTOR: _sector_unit_row()},
        unit_by_id={UNIT_SECTOR: _sector_unit_row()},
        member_rows=[],
        ledger_rows=ledger,
    )

    body = client.get(f"/api/v1/dalton/coverage/units/{UNIT_SECTOR}").json()
    sector = body["sector"]
    # 本行业待处理共 61 条，返回前 50 条；total 如实
    assert sector["pending_total"] == 61
    shown = sector["evidence_gaps"] + sector["ledger_gaps"]
    assert len(shown) == 50
    # 旧实现（全局最新 200 再过滤）在这个数据下只能看到较新的 50 条，
    # 最旧那条永远丢失；新实现按 created_at DESC 截断，最旧的 gap 恰好
    # 被截掉——为证明"先过滤"，去掉较新记录重跑，旧 gap 必须出现。
    # （同测试内不能重复 _install_db：create_autospec 的 spec 必须是真
    # 函数；第二场景只需要把两个连接池重指到新 cursor。）
    ledger_without_recent = [
        r for r in ledger if not str(r.get("question", "")).startswith("本行业较新")
    ]
    cur2, _ = _make_cursor(
        unit_rows=unit_rows, ws_rows=[], auto_rows=[], kv_rows=[],
        ledger_rows=ledger_without_recent,
    )
    monkeypatch.setattr(
        units_view_mod, "get_db_connection", lambda *a, **kw: _Lease(cur2)
    )
    monkeypatch.setattr(cur_db, "get_db_connection", lambda *a, **kw: _Lease(cur2))
    body2 = client.get(f"/api/v1/dalton/coverage/units/{UNIT_SECTOR}").json()
    sector2 = body2["sector"]
    assert sector2["pending_total"] == 1
    assert [g["question"] for g in sector2["ledger_gaps"]] == ["本行业最旧的 gap"]


def test_agenda_todos_keep_waiting_pm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex p4 r1 #3：waiting_pm 的议程待办保留并带标识，只有
    resolved/dropped（真正关闭）被排除。schema 状态枚举见 agenda.schema。"""
    _install_flag(monkeypatch, True)
    agenda = {
        "doc_key": "agenda",
        "version_no": 5,
        "content": {
            "schema_version": 1,
            "as_of": "2026-10-20T00:00:00Z",
            "open_questions": [
                {
                    "id": "Q-010",
                    "question": "已转交 PM 的行业问题",
                    "why": "",
                    "value": "high",
                    "urgency": "this_week",
                    "origin": "analyst",
                    "owner": "pm",
                    "status": "waiting_pm",
                    "attempts": [],
                    "created_at": "2026-10-02T00:00:00Z",
                },
                {
                    "id": "Q-011",
                    "question": "开放中的问题",
                    "why": "",
                    "value": "medium",
                    "urgency": "this_week",
                    "origin": "analyst",
                    "owner": "analyst",
                    "status": "open",
                    "attempts": [],
                    "created_at": "2026-10-03T00:00:00Z",
                },
                {
                    "id": "Q-012",
                    "question": "已解决的问题",
                    "why": "",
                    "value": "low",
                    "urgency": "someday",
                    "origin": "analyst",
                    "owner": "analyst",
                    "status": "resolved",
                    "attempts": [],
                    "created_at": "2026-09-03T00:00:00Z",
                },
                {
                    "id": "Q-013",
                    "question": "已放弃的问题",
                    "why": "",
                    "value": "low",
                    "urgency": "someday",
                    "origin": "analyst",
                    "owner": "analyst",
                    "status": "dropped",
                    "attempts": [],
                    "created_at": "2026-09-04T00:00:00Z",
                },
            ],
            "next_validations": [
                {
                    "id": "V-001",
                    "what": "季度 capex 口径核对",
                    "due": "2026-11-05",
                    "expected": "与上季口径一致",
                    "status": "pending",
                }
            ],
            "watch": {"events": []},
            "wake_conditions": [],
        },
    }
    from src.dalton.knowledge.validate import validate_schema

    assert validate_schema("agenda", agenda["content"]) == []

    cur, _ = _make_cursor(
        unit_rows=[_sector_unit_row()],
        ws_rows=[],
        auto_rows=[],
        kv_rows=[{**agenda, "ws": WS_SECTOR}],
    )
    _install_db(
        monkeypatch,
        cur,
        units_by_ws={WS_SECTOR: _sector_unit_row()},
        unit_by_id={UNIT_SECTOR: _sector_unit_row()},
        member_rows=[],
    )

    body = client.get(f"/api/v1/dalton/coverage/units/{UNIT_SECTOR}").json()
    questions = body["sector"]["agenda_todos"]["questions"]
    by_id = {q["id"]: q for q in questions}
    # waiting_pm 保留且带标识；open 照常；resolved/dropped 排除
    assert set(by_id) == {"Q-010", "Q-011"}
    assert by_id["Q-010"]["waiting_pm"] is True
    assert by_id["Q-011"]["waiting_pm"] is False


def test_drivers_summary_reads_leading_indicators(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex p4 r1 #4：指标摘要读取 leading_indicators 的真实 schema 字段；
    测试 drivers 文档先经 validate_schema 验证合法。"""
    _install_flag(monkeypatch, True)
    from src.dalton.knowledge.validate import validate_schema

    drivers = _drivers_doc()
    assert validate_schema("drivers", drivers["content"]) == []

    cur, _ = _make_cursor(
        unit_rows=[_sector_unit_row()],
        ws_rows=[],
        auto_rows=[],
        kv_rows=[{**drivers, "ws": WS_SECTOR}],
    )
    _install_db(
        monkeypatch,
        cur,
        units_by_ws={WS_SECTOR: _sector_unit_row()},
        unit_by_id={UNIT_SECTOR: _sector_unit_row()},
        member_rows=[],
    )

    body = client.get(f"/api/v1/dalton/coverage/units/{UNIT_SECTOR}").json()
    driver = body["sector"]["docs"]["drivers"]["drivers"][0]
    assert driver["leading_indicators"][0]["name"] == "CoWoS 月产能"
    assert driver["leading_indicators"][0]["latest"] == {
        "value": 35.0,
        "unit": "count_mn",
        "period": "2026-09",
        "as_of": "2026-10-05T00:00:00Z",
    }
    # 无读数的指标照常输出名字，latest 为 null
    assert driver["leading_indicators"][1]["latest"] is None
    assert driver["leading_indicators"][1]["name"] == "尚无读数的 HBM 出货指标"


def test_detail_hidden_colleague_unit_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    hidden = _sector_unit_row(
        unit_id=UNIT_HIDDEN_SECTOR,
        owner_user_id=COLLEAGUE,
        slug="hidden",
        display_name="Hidden Sector",
        workspace_id=COLLEAGUE_HIDDEN_WS,
    )
    cur, _ = _make_cursor(unit_rows=[hidden], ws_rows=[], auto_rows=[], kv_rows=[])
    _install_db(
        monkeypatch,
        cur,
        units_by_ws={},
        unit_by_id={UNIT_HIDDEN_SECTOR: hidden},
        member_rows=[],
        team_workspaces=[],
    )

    resp = client.get(f"/api/v1/dalton/coverage/units/{UNIT_HIDDEN_SECTOR}")
    assert resp.status_code == 404


def test_detail_unknown_or_archived_unit_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    archived = _company_unit_row(status="archived")
    cur, _ = _make_cursor(unit_rows=[archived], ws_rows=[], auto_rows=[], kv_rows=[])
    _install_db(
        monkeypatch, cur, units_by_ws={}, unit_by_id={UNIT_COMPANY: archived}, member_rows=[]
    )

    assert (
        client.get(f"/api/v1/dalton/coverage/units/{UNIT_COMPANY}").status_code == 404
    )
    assert (
        client.get("/api/v1/dalton/coverage/units/00000000-0000-0000-0000-0000000000ff").status_code
        == 404
    )


# ---------------------------------------------------------------------------
# 详情：company（关联行业 + 待复核待办）
# ---------------------------------------------------------------------------


def test_company_detail_related_sectors_and_re_review(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    kv_rows = [
        {**_thesis_doc(version_no=5), "ws": WS_COMPANY},
        {**_company_agenda_doc(), "ws": WS_COMPANY},
    ]
    cur, _ = _make_cursor(
        unit_rows=[_sector_unit_row(), _company_unit_row()],
        ws_rows=[],
        auto_rows=[],
        kv_rows=kv_rows,
    )
    _install_db(
        monkeypatch,
        cur,
        units_by_ws=_default_units_by_ws(),
        unit_by_id={UNIT_SECTOR: _sector_unit_row(), UNIT_COMPANY: _company_unit_row()},
        member_rows=[],
        company_members=_member_rows(),
    )

    resp = client.get(f"/api/v1/dalton/coverage/units/{UNIT_COMPANY}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "company"
    assert body["ticker"] == "EXAMPLE"
    company = body["company"]
    assert company["docs"]["thesis"]["version_no"] == 5
    # 反查关联行业：本人的行业单元 + 成员范围
    assert len(company["sectors"]) == 1
    related = company["sectors"][0]
    assert related["unit_id"] == UNIT_SECTOR
    assert related["readable"] is True and related["delegatable"] is True
    assert related["workspace_id"] == WS_SECTOR
    assert related["scope"] == "只计 AI 加速卡收入"
    # 待复核 = origin=sector 且未关闭的问题；resolved 的不出现
    assert [r["id"] for r in company["re_review"]] == ["Q-002"]
    assert company["re_review"][0]["source_ref"]["entry_id"] == "P1"


def test_company_detail_skips_hidden_colleague_sector(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_flag(monkeypatch, True)
    hidden_sector = _sector_unit_row(
        unit_id=UNIT_HIDDEN_SECTOR,
        owner_user_id=COLLEAGUE,
        slug="hidden",
        display_name="Hidden Sector",
        workspace_id=COLLEAGUE_HIDDEN_WS,
    )
    cur, _ = _make_cursor(
        unit_rows=[_company_unit_row(), hidden_sector], ws_rows=[], auto_rows=[], kv_rows=[]
    )
    _install_db(
        monkeypatch,
        cur,
        units_by_ws={WS_COMPANY: _company_unit_row()},
        unit_by_id={UNIT_COMPANY: _company_unit_row(), UNIT_HIDDEN_SECTOR: hidden_sector},
        member_rows=[
            {
                "sector_unit_id": UNIT_HIDDEN_SECTOR,
                "member_id": "M9",
                "member_name": "EXAMPLE",
                "ticker": "EXAMPLE",
                "company_unit_id": UNIT_COMPANY,
                "workspace_created": True,
                "scope": "",
                "reason": "",
                "valid_until": None,
                "position": 0,
                "synced_version_no": 1,
            }
        ],
        company_members=[
            {
                "sector_unit_id": UNIT_HIDDEN_SECTOR,
                "member_id": "M9",
                "member_name": "EXAMPLE",
                "ticker": "EXAMPLE",
                "company_unit_id": UNIT_COMPANY,
                "workspace_created": True,
                "scope": "",
                "reason": "",
                "valid_until": None,
                "position": 0,
                "synced_version_no": 1,
            }
        ],
        team_workspaces=[],
    )

    body = client.get(f"/api/v1/dalton/coverage/units/{UNIT_COMPANY}").json()
    # 同事隐藏工作区上的行业不进入关联列表
    assert body["company"]["sectors"] == []


# ---------------------------------------------------------------------------
# SQL 与模块规则
# ---------------------------------------------------------------------------


def test_units_view_sql_rules_no_like_no_untyped_null() -> None:
    import pathlib

    text = pathlib.Path(units_view_mod.__file__).read_text(encoding="utf-8")
    # 无 LIKE 关键字（也就没有字面量 % 通配；文档字符串里的小写 like 不是 SQL）；
    # 占位符只有 psycopg 的 %s[::cast]
    assert " LIKE " not in text
    assert "'%" not in text
    assert "%%" not in text
    # IS NULL 上下文与 uuid 比较都显式转型（生产 psycopg 参数类型的教训）
    assert "%s::text IS NULL" in text
    assert "%s::uuid" in text
