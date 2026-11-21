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
from src.dalton.access_tiers import is_internal_or_admin as _ORIG_IS_INTERNAL_OR_ADMIN
from src.server.database.home_workspace import get_home_workspace_id
from src.server.utils.api import get_current_user_id

_ORIG_UNITS_VIEW_GET_DB_CONN = units_view_mod.get_db_connection
_ORIG_CUR_GET_DB_CONN = cur_db.get_db_connection

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
    row.setdefault("lead_analyst_user_id", row["owner_user_id"])
    if row.get("workspace_id") and "workspace_owner_id" not in row:
        row["workspace_owner_id"] = row["owner_user_id"]
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
    row.setdefault("lead_analyst_user_id", row["owner_user_id"])
    if row.get("workspace_id") and "workspace_owner_id" not in row:
        row["workspace_owner_id"] = row["owner_user_id"]
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
                # 按 SELECT 列清单投影（codex r2 #3）：漏选的键不得出现在行里，
                # 否则 is_home_row 对 status 的依赖测不出来（撤修即失败）。
                cols = [
                    c.strip().rsplit(" AS ", 1)[-1].strip()
                    for c in sql.split("SELECT", 1)[1]
                    .split("FROM workspaces", 1)[0]
                    .split(",")
                ]
                rows_src = ws_rows
                if "WHERE workspace_id = %s::uuid" in sql and p:
                    rows_src = [r for r in ws_rows if str(r.get("workspace_id") or "") == str(p[0])]
                elif "status <> 'deleted' AND user_id = %s" in sql and p:
                    rows_src = [
                        r
                        for r in ws_rows
                        if str(r.get("user_id") or "") == str(p[0])
                        and str(r.get("status") or "active") != "deleted"
                    ]
                return [{k: r.get(k) for k in cols} for r in rows_src]
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
                ws_by_id = {
                    str(w.get("workspace_id") or ""): w
                    for w in ws_rows
                    if w.get("workspace_id") and w.get("status") != "deleted"
                }
                enriched_units = []
                for r in unit_rows:
                    item = dict(r)
                    ws_id_u = str(item.get("workspace_id") or "")
                    if ws_id_u and "workspace_owner_id" not in item:
                        ws_match = ws_by_id.get(ws_id_u)
                        item["workspace_owner_id"] = (
                            ws_match.get("user_id") if ws_match is not None else item.get("owner_user_id")
                        )
                    item.setdefault("lead_analyst_user_id", item.get("owner_user_id"))
                    enriched_units.append(item)
                if "unit_id = ANY" in sql:
                    ids = {str(u) for u in (p[0] if p else [])}
                    return [r for r in enriched_units if str(r.get("unit_id")) in ids]
                if "workspace_id = ANY" in sql:
                    ws_ids = {str(u) for u in (p[0] if p else [])}
                    return [
                        r for r in enriched_units if str(r.get("workspace_id")) in ws_ids
                    ]
                if len(p) >= 2 and p[1] is not None:
                    owner = str(p[1])
                    return [
                        r
                        for r in enriched_units
                        if (
                            str(r.get("lead_analyst_user_id") or r.get("owner_user_id")) == owner
                            or str(r.get("created_by_user_id") or r.get("owner_user_id")) == owner
                            or str(r.get("owner_user_id")) == owner
                        )
                        and str(r.get("status")) == "active"
                    ]
                return enriched_units
            return []

        async def fetchall(self) -> list[dict[str, Any]]:
            return self._query()

        async def fetchone(self) -> dict[str, Any] | None:
            rows = self._query()
            return rows[0] if rows else None

    return _Cur(), executed


class _NullTx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> None:
        return None


class _Lease:
    """一次连接租借：退出上下文即归还，之后再用 cursor 就报错。"""

    def __init__(self, cur: Any) -> None:
        self._cur = cur
        self._returned = False

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        if self._returned:
            raise RuntimeError("connection already returned to the pool")
        return self._cur

    def transaction(self) -> _NullTx:
        if self._returned:
            raise RuntimeError("connection already returned to the pool")
        return _NullTx()

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


def _install_flag(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, *, internal: bool = True
) -> None:
    import src.dalton.access_tiers as access_tiers_mod

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true" if enabled else "false")
    monkeypatch.setattr(
        settings_mod, "is_coverage_units_enabled", lambda f=enabled: f
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(_ORIG_IS_INTERNAL_OR_ADMIN, return_value=internal),
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
    def _lease_cur_conn(conn: Any = None) -> _Lease:
        return _Lease(cur)

    monkeypatch.setattr(
        cur_db,
        "get_db_connection",
        create_autospec(_ORIG_CUR_GET_DB_CONN, side_effect=_lease_cur_conn),
    )

    async def _fake_team(uid: str, *, conn: Any = None) -> list[dict[str, Any]]:
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


def test_list_unclassified_excludes_legacy_unbound_home(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex r2 #3：账号合并遗留的旧 Home（id 与新所有者不匹配、status='unbound'）
    不得进入未分类列表——查询必须投影 status，is_home_row 才认得出它。"""
    _install_flag(monkeypatch, True)
    unit_rows = [_sector_unit_row(), _company_unit_row()]
    legacy_home_ws = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"  # 非 uuid5 派生的 Home id
    ws_rows = [
        {
            "workspace_id": WS_EXTRA,
            "user_id": USER,
            "name": "随便的主题工作区",
            "description": "",
            "updated_at": None,
            "status": "active",
        },
        {
            "workspace_id": legacy_home_ws,
            "user_id": USER,
            "name": "旧 Home",
            "description": "",
            "updated_at": None,
            "status": "unbound",
        },
    ]
    cur, executed = _make_cursor(
        unit_rows=unit_rows, ws_rows=ws_rows, auto_rows=[], kv_rows=[]
    )
    _install_db(
        monkeypatch,
        cur,
        units_by_ws=_default_units_by_ws(),
        unit_by_id={UNIT_SECTOR: _sector_unit_row(), UNIT_COMPANY: _company_unit_row()},
        member_rows=_member_rows(),
    )

    body = client.get("/api/v1/dalton/coverage/units").json()
    # 遗留 Home 被排除；普通未分类工作区照常出现
    assert [w["workspace_id"] for w in body["unclassified"]] == [WS_EXTRA]
    ws_sql = next(s for s in executed if "FROM workspaces" in s)
    assert "status" in ws_sql.split("FROM workspaces", 1)[0]


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
        unit_rows=unit_rows,
        ws_rows=[{"workspace_id": WS_SECTOR, "user_id": USER, "status": "active"}],
        auto_rows=[],
        kv_rows=kv_rows,
        ledger_rows=ledger,
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
    cur, _ = _make_cursor(
        unit_rows=[colleague_sector],
        ws_rows=[
            {"workspace_id": COLLEAGUE_VISIBLE_WS, "user_id": COLLEAGUE, "status": "active"}
        ],
        auto_rows=[],
        kv_rows=[],
    )
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
        unit_rows=unit_rows,
        ws_rows=[{"workspace_id": WS_SECTOR, "user_id": USER, "status": "active"}],
        auto_rows=[],
        kv_rows=[],
        ledger_rows=ledger,
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
        unit_rows=unit_rows,
        ws_rows=[{"workspace_id": WS_SECTOR, "user_id": USER, "status": "active"}],
        auto_rows=[],
        kv_rows=[],
        ledger_rows=ledger_without_recent,
    )
    def _lease_cur2_conn(conn: Any = None) -> _Lease:
        return _Lease(cur2)

    monkeypatch.setattr(
        units_view_mod,
        "get_db_connection",
        create_autospec(_ORIG_UNITS_VIEW_GET_DB_CONN, side_effect=_lease_cur2_conn),
    )
    monkeypatch.setattr(
        cur_db,
        "get_db_connection",
        create_autospec(_ORIG_CUR_GET_DB_CONN, side_effect=_lease_cur2_conn),
    )
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
        ws_rows=[{"workspace_id": WS_SECTOR, "user_id": USER, "status": "active"}],
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
        ws_rows=[{"workspace_id": WS_SECTOR, "user_id": USER, "status": "active"}],
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
        ws_rows=[
            {"workspace_id": WS_SECTOR, "user_id": USER, "status": "active"},
            {"workspace_id": WS_COMPANY, "user_id": USER, "status": "active"},
        ],
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
        unit_rows=[_company_unit_row(), hidden_sector],
        ws_rows=[{"workspace_id": WS_COMPANY, "user_id": USER, "status": "active"}],
        auto_rows=[],
        kv_rows=[],
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


def test_read_isolation_for_unit_owner_without_workspace_access(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C8: unit.owner_user_id == viewer without workspace read access returns redacted workspace_id and 404 detail."""
    _install_flag(monkeypatch, True)
    revoked = _company_unit_row(
        unit_id=UNIT_COMPANY,
        owner_user_id=USER,
        lead_analyst_user_id=USER,
        workspace_id=COLLEAGUE_HIDDEN_WS,
        workspace_owner_id=COLLEAGUE,
    )
    kv_rows = [{**_thesis_doc(version_no=5), "ws": COLLEAGUE_HIDDEN_WS}]
    cur, _ = _make_cursor(
        unit_rows=[revoked],
        ws_rows=[{"workspace_id": COLLEAGUE_HIDDEN_WS, "user_id": COLLEAGUE, "status": "active"}],
        auto_rows=[],
        kv_rows=kv_rows,
    )
    _install_db(
        monkeypatch,
        cur,
        units_by_ws={COLLEAGUE_HIDDEN_WS: revoked},
        unit_by_id={UNIT_COMPANY: revoked},
        member_rows=[],
        team_workspaces=[],
    )

    resp_list = client.get("/api/v1/dalton/coverage/units")
    assert resp_list.status_code == 200
    items = resp_list.json()["units"]
    assert len(items) == 1
    assert items[0]["readable"] is False
    assert items[0]["delegatable"] is False
    assert items[0]["workspace_id"] is None
    assert "workspace_owner_id" not in items[0]

    resp_detail = client.get(f"/api/v1/dalton/coverage/units/{UNIT_COMPANY}")
    assert resp_detail.status_code == 404


def test_unclassified_and_bindable_workspaces_exclude_historical_duplicates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C7: historical duplicates are excluded; colleague rw workspace appears in bindable_workspaces."""
    _install_flag(monkeypatch, True)
    colleague_rw_ws = "abababab-abab-abab-abab-abababababab"
    colleague_ro_ws = "cdcdcdcd-cdcd-cdcd-cdcd-cdcdcdcdcdcd"
    colleague_dup_ws = "efefefef-efef-efef-efef-efefefefefef"
    ws_rows = [
        {
            "workspace_id": WS_EXTRA,
            "user_id": USER,
            "name": "Own Normal",
            "description": "",
            "updated_at": None,
            "status": "active",
            "config": {},
        },
        {
            "workspace_id": "12121212-1212-1212-1212-121212121212",
            "user_id": USER,
            "name": "Own Dup",
            "description": "",
            "updated_at": None,
            "status": "active",
            "config": {"dalton_historical_duplicate_of": WS_COMPANY},
        },
    ]
    cur, _ = _make_cursor(unit_rows=[], ws_rows=ws_rows, auto_rows=[], kv_rows=[])
    _install_db(
        monkeypatch,
        cur,
        units_by_ws={},
        unit_by_id={},
        member_rows=[],
        team_workspaces=[
            {
                "user_id": COLLEAGUE,
                "display_name": "Bob",
                "email": "bob@firm.com",
                "workspaces": [
                    {
                        "workspace_id": colleague_rw_ws,
                        "name": "Colleague RW",
                        "web_access": "rw",
                        "cos_access": "rw",
                        "status": "active",
                        "is_historical_duplicate": False,
                    },
                    {
                        "workspace_id": colleague_ro_ws,
                        "name": "Colleague RO",
                        "web_access": "ro",
                        "cos_access": "ro",
                        "status": "active",
                        "is_historical_duplicate": False,
                    },
                    {
                        "workspace_id": colleague_dup_ws,
                        "name": "Colleague RW Dup",
                        "web_access": "rw",
                        "cos_access": "rw",
                        "status": "active",
                        "is_historical_duplicate": True,
                    },
                ],
            }
        ],
    )

    body = client.get("/api/v1/dalton/coverage/units").json()
    assert [w["workspace_id"] for w in body["unclassified"]] == [WS_EXTRA]
    bindable_ids = {w["workspace_id"] for w in body["bindable_workspaces"]}
    assert bindable_ids == {WS_EXTRA, colleague_rw_ws}
    colleague_entry = next(
        w for w in body["bindable_workspaces"] if w["workspace_id"] == colleague_rw_ws
    )
    assert colleague_entry["is_own"] is False
    assert colleague_entry["owner_name"] == "Bob"


def test_p4_external_user_403_on_list_detail_and_board(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C8: non-autouse per-test gate restores 403 verification for list, detail, and /coverage/board."""
    _install_flag(monkeypatch, True, internal=False)
    assert client.get("/api/v1/dalton/coverage/units").status_code == 403
    assert client.get(f"/api/v1/dalton/coverage/units/{UNIT_SECTOR}").status_code == 403
    assert client.get("/api/v1/dalton/coverage/board").status_code == 403


def test_unreadable_sector_omits_members_and_detail_redacts_unreadable_gap_targets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 2: unreadable sector in list returns members=[]; readable sector detail redacts unreadable target_workspace_id."""
    _install_flag(monkeypatch, True, internal=True)
    hidden_sector = _sector_unit_row(
        unit_id="55555555-5555-5555-5555-555555555555",
        owner_user_id=USER,
        lead_analyst_user_id=USER,
        workspace_id=COLLEAGUE_HIDDEN_WS,
        workspace_owner_id=COLLEAGUE,
    )
    readable_sector = _sector_unit_row(unit_id=UNIT_SECTOR, workspace_id=WS_SECTOR)
    ledger = [
        {
            "request_id": "77777777-7777-7777-7777-777777777701",
            "owner_user_id": USER,
            "kind": "research_request",
            "sector_unit_id": UNIT_SECTOR,
            "question": "Own readable gap",
            "evidence_needed": [],
            "completion_condition": "done",
            "target_workspace_id": WS_COMPANY,
            "status": "open",
            "created_at": "2026-10-10T00:00:00+00:00",
            "updated_at": "2026-10-10T00:00:00+00:00",
        },
        {
            "request_id": "77777777-7777-7777-7777-777777777702",
            "owner_user_id": USER,
            "kind": "linkage_retry",
            "sector_unit_id": UNIT_SECTOR,
            "question": "Hidden target gap",
            "target_workspace_id": COLLEAGUE_HIDDEN_WS,
            "status": "gap",
            "created_at": "2026-10-11T00:00:00+00:00",
            "updated_at": "2026-10-11T00:00:00+00:00",
        },
    ]
    hidden_member = [
        {
            "sector_unit_id": "55555555-5555-5555-5555-555555555555",
            "member_id": "M-SECRET",
            "member_name": "Secret Company",
            "ticker": "SCRT",
            "company_unit_id": None,
            "workspace_created": False,
            "scope": "Sensitive thesis scope",
            "reason": "Sensitive reason",
            "valid_until": "2027-01-01",
            "position": 0,
            "synced_version_no": 1,
        }
    ]
    cur, _ = _make_cursor(
        unit_rows=[hidden_sector, readable_sector],
        ws_rows=[
            {"workspace_id": WS_SECTOR, "user_id": USER, "status": "active"},
            {"workspace_id": WS_COMPANY, "user_id": USER, "status": "active"},
        ],
        auto_rows=[],
        kv_rows=[],
        ledger_rows=ledger,
    )
    _install_db(
        monkeypatch,
        cur,
        units_by_ws={WS_SECTOR: readable_sector, COLLEAGUE_HIDDEN_WS: hidden_sector},
        unit_by_id={
            "55555555-5555-5555-5555-555555555555": hidden_sector,
            UNIT_SECTOR: readable_sector,
        },
        member_rows=hidden_member,
        ledger_rows=ledger,
    )

    resp_list = client.get("/api/v1/dalton/coverage/units")
    assert resp_list.status_code == 200
    by_id = {u["unit_id"]: u for u in resp_list.json()["units"]}
    assert by_id["55555555-5555-5555-5555-555555555555"]["readable"] is False
    assert by_id["55555555-5555-5555-5555-555555555555"]["workspace_id"] is None
    assert by_id["55555555-5555-5555-5555-555555555555"]["members"] == []

    resp_detail = client.get(f"/api/v1/dalton/coverage/units/{UNIT_SECTOR}")
    assert resp_detail.status_code == 200
    sector_detail = resp_detail.json()["sector"]
    assert sector_detail["evidence_gaps"][0]["target_workspace_id"] == WS_COMPANY
    assert sector_detail["ledger_gaps"][0]["target_workspace_id"] is None


def test_company_re_review_redacts_unreadable_source_ref_and_repeatable_read_and_flags(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Items 1, 2, 11: redact unreadable source_ref on company.re_review, run REPEATABLE READ snapshots, and match action flags to closed-auth/deleted-ws auth rules."""
    import asyncio
    import src.dalton.invites as invites_mod
    from src.dalton.invites import is_invite_pm as orig_is_pm
    from src.dalton.settings import is_dalton_supabase_auth_enabled as orig_sb_auth

    _install_flag(monkeypatch, True, internal=True)
    monkeypatch.setattr(invites_mod, "is_invite_pm", create_autospec(orig_is_pm, return_value=True))
    monkeypatch.setattr(settings_mod, "is_dalton_supabase_auth_enabled", create_autospec(orig_sb_auth, return_value=True))
    monkeypatch.setattr(units_view_mod, "is_dalton_supabase_auth_enabled", create_autospec(orig_sb_auth, return_value=True))
    monkeypatch.setattr(units_view_mod, "is_invite_pm", create_autospec(orig_is_pm, return_value=True))

    agenda_with_two_sources = {
        "doc_key": "agenda",
        "version_no": 4,
        "content": {
            "open_questions": [
                {
                    "id": "Q-VISIBLE",
                    "question": "Check readable sector assumption",
                    "value": "high",
                    "urgency": "now",
                    "origin": "sector",
                    "status": "open",
                    "source_ref": {
                        "workspace_id": WS_SECTOR,
                        "doc_key": "thesis",
                        "version_no": 3,
                        "entry_id": "P1",
                        "relation": "assumes",
                    },
                },
                {
                    "id": "Q-HIDDEN",
                    "question": "Check hidden sector assumption",
                    "value": "medium",
                    "urgency": "this_week",
                    "origin": "sector",
                    "status": "open",
                    "source_ref": {
                        "workspace_id": COLLEAGUE_HIDDEN_WS,
                        "doc_key": "thesis",
                        "version_no": 7,
                        "entry_id": "P2",
                        "relation": "assumes",
                    },
                },
            ]
        },
    }
    company_unit = _company_unit_row(unit_id=UNIT_COMPANY, workspace_id=WS_COMPANY)
    deleted_ws_id = "aaaaaaaa-0000-0000-0000-000000000001"
    orphan_unit = _company_unit_row(
        unit_id="bbbbbbbb-0000-0000-0000-000000000002",
        owner_user_id=COLLEAGUE,
        created_by_user_id=USER,
        lead_analyst_user_id=COLLEAGUE,
        slug="orphan-co",
        ticker="ORPH",
        workspace_id=deleted_ws_id,
        workspace_owner_id=None,
    )
    pm_colleague_rw = _company_unit_row(
        unit_id="cccccccc-0000-0000-0000-000000000003",
        owner_user_id=COLLEAGUE,
        created_by_user_id=COLLEAGUE,
        lead_analyst_user_id=USER,
        slug="colleague-co",
        ticker="CLG",
        workspace_id=COLLEAGUE_VISIBLE_WS,
        workspace_owner_id=COLLEAGUE,
    )
    cur, executed = _make_cursor(
        unit_rows=[company_unit, orphan_unit, pm_colleague_rw],
        ws_rows=[
            {"workspace_id": WS_COMPANY, "user_id": USER, "status": "active"},
            {"workspace_id": WS_SECTOR, "user_id": USER, "status": "active"},
            {"workspace_id": COLLEAGUE_VISIBLE_WS, "user_id": COLLEAGUE, "status": "active"},
        ],
        auto_rows=[],
        kv_rows=[{**agenda_with_two_sources, "ws": WS_COMPANY}],
    )
    _install_db(
        monkeypatch,
        cur,
        units_by_ws={WS_COMPANY: company_unit},
        unit_by_id={UNIT_COMPANY: company_unit},
        member_rows=[],
        company_members=[],
        team_workspaces=[],
    )

    # Item 1 & Item 2: company detail runs REPEATABLE READ and strips unreadable source_ref
    resp_detail = client.get(f"/api/v1/dalton/coverage/units/{UNIT_COMPANY}")
    assert resp_detail.status_code == 200
    re_reviews = {q["id"]: q for q in resp_detail.json()["company"]["re_review"]}
    assert re_reviews["Q-VISIBLE"]["source_ref"]["workspace_id"] == WS_SECTOR
    assert "source_ref" not in re_reviews["Q-HIDDEN"]
    assert re_reviews["Q-HIDDEN"]["question"] == "Check hidden sector assumption"
    iso_idx = executed.index("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    cu_idx = next(i for i, s in enumerate(executed) if "dalton_knowledge_versions" in s)
    assert iso_idx < cu_idx

    # Phase 1: bound orphan unit refuses rebind (can_bind=False), while creator still recovers can_unbind/can_archive
    executed.clear()
    monkeypatch.setattr(units_view_mod, "is_invite_pm", create_autospec(orig_is_pm, return_value=False))
    resp_list = client.get("/api/v1/dalton/coverage/units")
    assert resp_list.status_code == 200
    list_iso_idx = executed.index("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    list_cu_idx = next(i for i, s in enumerate(executed) if "FROM dalton_coverage_units" in s)
    assert list_iso_idx < list_cu_idx
    units_by_id = {u["unit_id"]: u for u in resp_list.json()["units"]}
    orphan_entry = units_by_id[orphan_unit["unit_id"]]
    assert orphan_entry["can_bind"] is False
    assert orphan_entry["can_unbind"] is True
    assert orphan_entry["can_archive"] is True

    # Item 11 (closed Supabase auth): non-owner PM must get ALL False action flags on colleague unit
    monkeypatch.setattr(units_view_mod, "is_invite_pm", create_autospec(orig_is_pm, return_value=True))
    monkeypatch.setattr(
        units_view_mod,
        "is_dalton_supabase_auth_enabled",
        create_autospec(orig_sb_auth, return_value=False),
    )
    items_closed = asyncio.run(
        units_view_mod.build_units_view(USER, {})
    )
    closed_by_id = {u["unit_id"]: u for u in items_closed}
    colleague_entry = closed_by_id[pm_colleague_rw["unit_id"]]
    assert colleague_entry["can_bind"] is False
    assert colleague_entry["can_unbind"] is False
    assert colleague_entry["can_archive"] is False


def test_list_unclassified_and_bindable_exclude_home_stand_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 6: list_unclassified_workspaces and list_bindable_workspaces omit home_stand_in rows."""
    import asyncio
    from src.dalton.autonomy import units_view as units_view_mod
    import src.dalton.sharing as sharing_mod

    _install_flag(monkeypatch, True)

    ws_normal = "10000000-0000-0000-0000-000000000001"
    ws_stand_in = "10000000-0000-0000-0000-000000000002"
    ws_coll_rw = "10000000-0000-0000-0000-000000000003"

    cur, _ = _make_cursor(
        ws_rows=[
            {"workspace_id": ws_normal, "user_id": USER, "name": "Research A", "description": None, "updated_at": None, "status": "stopped", "config": {}},
            {"workspace_id": ws_stand_in, "user_id": USER, "name": "Home Stand-In", "description": None, "updated_at": None, "status": "stopped", "config": {"home_stand_in": True}},
        ],
    )

    def _lease_conn(conn: Any = None) -> _Lease:
        return _Lease(cur)

    monkeypatch.setattr(
        units_view_mod,
        "get_db_connection",
        create_autospec(units_view_mod.get_db_connection, side_effect=_lease_conn),
    )
    monkeypatch.setattr(
        units_view_mod,
        "load_active_units_by_workspace",
        create_autospec(units_view_mod.load_active_units_by_workspace, return_value={}),
    )

    async def _team_ws(uid: str, *, conn: Any = None) -> list[dict[str, Any]]:
        # Upstream sharing.py (`list_team_workspaces`) omits reserved home_stand_in
        # rows before constructing the stripped colleague workspace dicts.
        return [
            {
                "user_id": "u-colleague",
                "display_name": "Alice",
                "email": "alice@firm.com",
                "workspaces": [
                    {
                        "workspace_id": ws_coll_rw,
                        "name": "Colleague Research",
                        "web_access": "rw",
                        "status": "stopped",
                        "is_home": False,
                        "is_historical_duplicate": False,
                    },
                ],
            }
        ]

    monkeypatch.setattr(sharing_mod, "list_team_workspaces", create_autospec(sharing_mod.list_team_workspaces, side_effect=_team_ws))
    monkeypatch.setattr(units_view_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(units_view_mod, "is_invite_pm", lambda uid: False)

    unclassified = asyncio.run(units_view_mod.list_unclassified_workspaces(USER))
    assert [w["workspace_id"] for w in unclassified] == [ws_normal]

    bindable = asyncio.run(units_view_mod.list_bindable_workspaces(USER))
    assert [w["workspace_id"] for w in bindable] == [ws_normal, ws_coll_rw]


@pytest.mark.asyncio
async def test_unreadable_unit_sets_bound_flag_and_hides_workspace_id(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 8: even for PM, unreadable unit sets bound=True while workspace_id is redacted to None."""
    from src.dalton import invites as invites_mod

    _install_flag(monkeypatch, True, internal=True)
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("DALTON_PM_USER_IDS", USER)
    monkeypatch.setattr(settings_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(units_view_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(units_view_mod, "is_invite_pm", lambda uid: str(uid) == USER)
    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: str(uid) == USER)
    hidden_ws = "88888888-8888-4888-8888-888888888888"
    hidden_unit = {
        "unit_id": "u-pm-hidden",
        "kind": "sector",
        "slug": "hidden-sector",
        "display_name": "Hidden Sector",
        "owner_user_id": USER,
        "created_by_user_id": USER,
        "lead_analyst_user_id": "colleague-a",
        "workspace_id": hidden_ws,
        "workspace_owner_id": "colleague-a",
        "workspace_status": "stopped",
        "status": "active",
    }
    cur, _ = _make_cursor(unit_rows=[hidden_unit])
    _install_db(monkeypatch, cur, unit_by_id={}, team_workspaces=[])

    resp = client.get("/api/v1/dalton/coverage/units")
    assert resp.status_code == 200
    u = next(item for item in resp.json()["units"] if item["unit_id"] == "u-pm-hidden")
    assert u["bound"] is True
    assert u["workspace_id"] is None
    assert u["readable"] is False
    assert hidden_ws not in resp.text


@pytest.mark.asyncio
async def test_board_savepoint_isolates_coverage_unit_failure_inside_repeatable_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4: load_active_units_by_workspace failure inside REPEATABLE READ runs inside a savepoint
    so outer knowledge/company queries still succeed."""
    import src.dalton.autonomy.board as board_mod
    from tests.unit.dalton.test_sector_p2 import _board_cursor, _install_board as _p2_install_board

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    ws_comp = "12121212-1212-4212-8212-121212121212"
    ws_rows = [
        {
            "workspace_id": ws_comp,
            "user_id": USER,
            "name": "NVDA",
            "description": "",
            "status": "running",
            "updated_at": None,
            "config": {},
        }
    ]
    kv_rows = [
        {
            "workspace_id": ws_comp,
            "doc_key": "company",
            "version_no": 1,
            "content": {"ticker": "NVDA"},
            "created_at": None,
        }
    ]
    cur, _ = _board_cursor(ws_rows, kv_rows)
    _p2_install_board(monkeypatch, cur)

    savepoint_entries = 0

    class _TxCheckingLease:
        def __init__(self, c: Any) -> None:
            self._cur = c
            self._returned = False
            self._failed_outer = False

        def cursor(self, *args: Any, **kwargs: Any) -> Any:
            if self._returned:
                raise RuntimeError("returned")
            if self._failed_outer:
                raise RuntimeError("InFailedSqlTransaction")
            return self._cur

        def transaction(self):
            class _Sp:
                async def __aenter__(self_sp):
                    nonlocal savepoint_entries
                    savepoint_entries += 1
                    return self_sp

                async def __aexit__(self_sp, exc_type, exc, tb):
                    # Savepoint catches error without poisoning outer tx
                    return False

            return _Sp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> None:
            self._returned = True

    monkeypatch.setattr(board_mod, "get_db_connection", lambda: _TxCheckingLease(cur))
    monkeypatch.setattr(
        board_mod,
        "load_active_units_by_workspace",
        create_autospec(
            board_mod.load_active_units_by_workspace,
            side_effect=RuntimeError("boom inside coverage query"),
        ),
    )

    board = await board_mod.build_coverage_board(user_id=USER)
    # Savepoint entered for outer REPEATABLE READ + degradable inner reads; company still surfaced!
    assert savepoint_entries >= 2
    assert [c["ticker"] for c in board["companies"]] == ["NVDA"]


@pytest.mark.asyncio
async def test_unit_detail_and_list_clear_all_unreadable_identity_fields_consistently(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 7 Item 3: both list and detail clear workspace_id, owner_user_id,
    lead_analyst_user_id, and created_by_user_id for an unreadable bound unit."""
    from src.dalton import invites as invites_mod

    _install_flag(monkeypatch, True, internal=True)
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("DALTON_PM_USER_IDS", USER)
    monkeypatch.setattr(settings_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(units_view_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(units_view_mod, "is_invite_pm", lambda uid: str(uid) == USER)
    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: str(uid) == USER)
    hidden_ws = "88888888-8888-4888-8888-888888888888"
    secret_lead = "u-secret-lead"
    secret_creator = "u-secret-creator"
    hidden_unit = {
        "unit_id": "u-hidden-all-ids",
        "kind": "sector",
        "slug": "hidden-sector-ids",
        "display_name": "Hidden Sector",
        "owner_user_id": USER,
        "created_by_user_id": secret_creator,
        "lead_analyst_user_id": secret_lead,
        "workspace_id": hidden_ws,
        "workspace_owner_id": secret_lead,
        "workspace_status": "stopped",
        "status": "active",
    }
    cur, _ = _make_cursor(unit_rows=[hidden_unit])
    _install_db(monkeypatch, cur, unit_by_id={"u-hidden-all-ids": hidden_unit}, team_workspaces=[])

    list_resp = client.get("/api/v1/dalton/coverage/units")
    assert list_resp.status_code == 200
    u_list = next(item for item in list_resp.json()["units"] if item["unit_id"] == "u-hidden-all-ids")
    assert u_list["bound"] is True
    assert u_list["readable"] is False
    for field in ("workspace_id", "owner_user_id", "lead_analyst_user_id", "created_by_user_id"):
        assert u_list[field] is None
    for secret in (hidden_ws, secret_lead, secret_creator):
        assert secret not in list_resp.text

    # C8: unreadable bound unit detail stays 404 and leaks no secrets
    detail_resp = client.get("/api/v1/dalton/coverage/units/u-hidden-all-ids")
    assert detail_resp.status_code == 404
    for secret in (hidden_ws, secret_lead, secret_creator):
        assert secret not in detail_resp.text
