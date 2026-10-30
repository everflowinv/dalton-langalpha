"""The single coverage-identity resolver and its three call sites (sector P0, round 2).

tracking、看板、总监探针共用 `resolve_workspace_coverage_identity` 的结果。
锁定（codex r1 #2/#3/#4）：
- Home 与已登记 sector 单元无条件非公司——名称再像 ticker（"NVDA"、"AI"）也
  不能绕过，看板不进、议程待办不计数；
- 看板与探针的 ticker / 交易所时区一律取解析结果——"腾讯"绑定 company 单元
  0700.HK 时用 0700.HK 按港股时区计到期，不再从工作区名重取；
- 开关关闭时，只有 `dalton-<t>-*` 自动化、没有文档的老公司工作区照常出现；
- 测试替身只返回 `get_workspace()` 真实投影里存在的列（workspaces 表没有
  metadata/ticker 列，该兼容规则在生产经 get_workspace 不可达，规则本身保留）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import pytest

import src.dalton.autonomy.board as board_mod
import src.dalton.coverage.identity as identity_mod
from src.dalton.coverage.identity import (
    classify_workspace_coverage,
    resolve_workspace_coverage_identity,
    ticker_from_automation_rows,
)
from src.dalton.database.knowledge import latest_valid_version as _REAL_LATEST_VERSION
from src.server.database import workspace as ws_db
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.workspace import get_workspace as _REAL_GET_WORKSPACE
from src.server.database.workspace import _WS_COLUMNS

U1 = "user-1"
HOME_WS = get_home_workspace_id(U1)
HK = ZoneInfo("Asia/Hong_Kong")


def _ws(ws_id: str, name: str, user_id: str = U1) -> dict[str, Any]:
    # 只含 get_workspace() 真实投影中的列（codex r1 #6）。
    return {
        "workspace_id": ws_id,
        "user_id": user_id,
        "name": name,
        "status": "running",
    }


def _sector_unit(ws_id: str, slug: str = "ai-infrastructure") -> dict[str, Any]:
    return {
        "unit_id": "22222222-2222-2222-2222-222222222222",
        "kind": "sector",
        "owner_user_id": U1,
        "slug": slug,
        "workspace_id": ws_id,
        "ticker": None,
        "status": "active",
    }


def _company_unit(ws_id: str, ticker: str | None, slug: str = "mega-tech") -> dict[str, Any]:
    return {
        "unit_id": "33333333-3333-3333-3333-333333333333",
        "kind": "company",
        "owner_user_id": U1,
        "slug": slug,
        "workspace_id": ws_id,
        "ticker": ticker,
        "status": "active",
    }


# ---------------------------------------------------------------------------
# 名称 → ticker：只认整串即 ticker 的名字（bootstrap 约定），不再取首词
# ---------------------------------------------------------------------------


def test_extract_ticker_accepts_full_string_only() -> None:
    # 经 tracking 的再导出取（原语已搬到 identity），knowledge.py 同路径。
    from src.dalton.autonomy.tracking import _extract_ticker_from_text

    assert _extract_ticker_from_text("NVDA") == "NVDA"
    assert _extract_ticker_from_text("googl") == "GOOGL"
    assert _extract_ticker_from_text("0700.HK") == "0700.HK"
    assert _extract_ticker_from_text("600519.SS") == "600519.SS"
    assert _extract_ticker_from_text("BRK.B") == "BRK.B"
    # 首词是 ticker 形状的多词名字（行业/主题工作区）不再被当成公司
    assert _extract_ticker_from_text("AI Infrastructure") is None
    assert _extract_ticker_from_text("Digital Advertising") is None
    assert _extract_ticker_from_text("Home") is None
    assert _extract_ticker_from_text("GOOGL_2024") is None
    assert _extract_ticker_from_text("腾讯") is None
    assert _extract_ticker_from_text("") is None
    assert _extract_ticker_from_text(None) is None


def test_workspace_doubles_only_use_real_projection_columns() -> None:
    """codex r1 #6：本文件的替身不得补出 get_workspace() 投影之外的列。"""
    real = set(_WS_COLUMNS) | {
        "computer_root_dir",
        "computer_kind",
        "computer_provider_config",
        "files_restore_incomplete",
    }
    for col in _ws("ws-x", "NVDA"):
        assert col in real, col
    assert "metadata" not in real and "ticker" not in real


# ---------------------------------------------------------------------------
# 兼容规则分类（纯函数）：tracking 的历史顺序，任何一条命中即为公司
# ---------------------------------------------------------------------------


def test_compat_order_metadata_doc_name_automation() -> None:
    autos = [{"workspace_id": "ws-a", "name": "dalton-amzn-watch", "metadata": {}}]

    # 1. metadata ticker（生产 get_workspace 投影无此列，规则按历史顺序保留）
    with_meta = {**_ws("ws-a", "随便"), "metadata": {"ticker": "msft"}}
    i = classify_workspace_coverage(with_meta, company_content={"ticker": "NVDA"}, automations=autos)
    assert (i.kind, i.ticker, i.ticker_source) == ("company", "MSFT", "workspace_metadata")

    # 2. 公司文档 ticker
    i2 = classify_workspace_coverage(_ws("ws-a", "腾讯"), company_content={"ticker": "0700.HK"}, automations=autos)
    assert (i2.kind, i2.ticker, i2.ticker_source) == ("company", "0700.HK", "company_doc")

    # 2b. 公司文档无 ticker 时退到 thesis ticker
    i2b = classify_workspace_coverage(
        _ws("ws-a", "腾讯"), company_content={"exchange": "HKEX"}, thesis_content={"ticker": "9988.HK"}
    )
    assert (i2b.kind, i2b.ticker, i2b.ticker_source) == ("company", "9988.HK", "thesis_doc")

    # 3. 名称整串即 ticker（bootstrap 约定）
    i3 = classify_workspace_coverage(_ws("ws-a", "NVDA"), automations=autos)
    assert (i3.kind, i3.ticker, i3.ticker_source) == ("company", "NVDA", "workspace_name")

    # 4. 只有该工作区已有的 dalton-<t>-* 自动化
    i4 = classify_workspace_coverage(_ws("ws-a", "亚马逊"), automations=autos)
    assert (i4.kind, i4.ticker, i4.ticker_source) == ("company", "AMZN", "automation")

    # 都不中 → 未分类
    i5 = classify_workspace_coverage(_ws("ws-a", "AI Infrastructure"))
    assert (i5.kind, i5.ticker) == ("unclassified", None)


def test_classify_home_never_becomes_a_company() -> None:
    # 名称为 ticker 形状的 Home 也不是公司（round 2：无条件，先于一切规则）
    identity = classify_workspace_coverage(
        _ws(HOME_WS, "AI"), company_content={"ticker": "AI"}
    )
    assert identity.kind == "home"
    assert identity.is_home and not identity.is_company
    unbound = {**_ws(HOME_WS, "Home"), "status": "unbound"}
    assert classify_workspace_coverage(unbound).kind == "home"


def test_ticker_from_automation_rows_matches_tracking_rules() -> None:
    # 与 tracking 原循环同语义：按行序取第一个归属该工作区的行，行内
    # metadata.ticker 优先于任务名；空 workspace_id 的行按 legacy 规则归属
    # 任何工作区。
    rows = [
        {"workspace_id": "ws-other", "name": "dalton-msft-watch", "metadata": {"ticker": "MSFT"}},
        {"workspace_id": "ws-a", "name": "随便", "metadata": {"ticker": "amzn"}},
        {"workspace_id": "", "name": "dalton-googl-delta", "metadata": {}},
    ]
    assert ticker_from_automation_rows("ws-a", rows) == "AMZN"  # 命中本工作区行，metadata.ticker 优先
    assert ticker_from_automation_rows("ws-b", rows) == "GOOGL"  # 只剩空 ws 行，按名称归属

    no_empty_ws = [r for r in rows if r["workspace_id"]]
    assert ticker_from_automation_rows("ws-c", no_empty_ws) is None


# ---------------------------------------------------------------------------
# 完整解析：开关关闭不读表；开关打开已登记单元优先且 sector 无条件
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_with_flag_off_never_reads_the_units_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.database import coverage_units as cu_db

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("coverage_units table must not be read while the flag is off")

    monkeypatch.setattr(identity_mod, "is_coverage_units_enabled", lambda: False)
    mock_lookup = create_autospec(cu_db.get_active_unit_by_workspace, side_effect=_forbidden)
    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", mock_lookup)

    identity = await resolve_workspace_coverage_identity(
        _ws("ws-a", "AI Infrastructure"),
        company_content=None,
        thesis_content=None,
    )
    assert identity.kind == "unclassified"
    mock_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_registered_sector_beats_ticker_name_and_doc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identity_mod, "is_coverage_units_enabled", lambda: True)
    # 名为 NVDA、还带着遗留公司文档的工作区：sector 登记无条件生效
    identity = await resolve_workspace_coverage_identity(
        _ws("ws-a", "NVDA"),
        company_content={"ticker": "NVDA"},
        registered_units={"ws-a": _sector_unit("ws-a")},
    )
    assert identity.kind == "sector"
    assert identity.is_sector and not identity.is_company
    assert identity.ticker is None
    assert identity.source == "registered_unit"


@pytest.mark.asyncio
async def test_resolve_registered_company_ticker_beats_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identity_mod, "is_coverage_units_enabled", lambda: True)
    identity = await resolve_workspace_coverage_identity(
        _ws("ws-a", "谷歌"),
        company_content={"ticker": "META"},
        registered_units={"ws-a": _company_unit("ws-a", "GOOGL")},
    )
    assert (identity.kind, identity.source, identity.ticker, identity.ticker_source) == (
        "company",
        "registered_unit",
        "GOOGL",
        "unit",
    )


@pytest.mark.asyncio
async def test_resolve_registered_company_without_ticker_falls_back_to_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identity_mod, "is_coverage_units_enabled", lambda: True)
    identity = await resolve_workspace_coverage_identity(
        _ws("ws-a", "腾讯"),
        company_content={"ticker": "0700.HK"},
        registered_units={"ws-a": _company_unit("ws-a", None)},
    )
    assert identity.kind == "company"
    assert identity.source == "registered_unit"
    assert identity.ticker == "0700.HK"
    assert identity.ticker_source == "company_doc"


@pytest.mark.asyncio
async def test_resolve_with_flag_on_queries_when_no_map_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.database import coverage_units as cu_db

    monkeypatch.setattr(identity_mod, "is_coverage_units_enabled", lambda: True)
    mock_lookup = create_autospec(
        cu_db.get_active_unit_by_workspace,
        return_value=_company_unit("ws-m", "MSFT"),
    )
    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", mock_lookup)

    identity = await resolve_workspace_coverage_identity(
        _ws("ws-m", "微软"), company_content=None, thesis_content=None
    )
    assert identity.source == "registered_unit"
    assert identity.ticker == "MSFT"
    mock_lookup.assert_awaited_once_with("ws-m", conn=None)


@pytest.mark.asyncio
async def test_resolve_archived_or_absent_unit_falls_back_to_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.database import coverage_units as cu_db

    monkeypatch.setattr(identity_mod, "is_coverage_units_enabled", lambda: True)
    mock_lookup = create_autospec(cu_db.get_active_unit_by_workspace, return_value=None)
    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", mock_lookup)

    identity = await resolve_workspace_coverage_identity(
        _ws("ws-a", "腾讯"),
        company_content={"ticker": "0700.HK"},
        thesis_content=None,
    )
    assert (identity.kind, identity.source, identity.ticker) == ("company", "compat", "0700.HK")


# ---------------------------------------------------------------------------
# 自动跟踪入口 `_resolve_workspace_ticker`（统一解析器的薄封装）
# ---------------------------------------------------------------------------


def _install_workspace(monkeypatch: pytest.MonkeyPatch, ws: dict[str, Any] | None) -> None:
    mock_get = create_autospec(_REAL_GET_WORKSPACE, return_value=ws)
    monkeypatch.setattr(ws_db, "get_workspace", mock_get)


def _install_docs(
    monkeypatch: pytest.MonkeyPatch,
    company: dict[str, Any] | None = None,
    thesis: dict[str, Any] | None = None,
) -> None:
    from src.dalton.database import knowledge as db_knowledge

    async def _latest(ws_id: str, doc_key: str) -> dict[str, Any] | None:
        content = {"company": company, "thesis": thesis}.get(doc_key)
        return {"content": content} if content is not None else None

    mock_latest = create_autospec(_REAL_LATEST_VERSION, side_effect=_latest)
    monkeypatch.setattr(db_knowledge, "latest_valid_version", mock_latest)


def _install_units_lookup(
    monkeypatch: pytest.MonkeyPatch, unit: dict[str, Any] | None
) -> None:
    from src.dalton.database import coverage_units as cu_db

    monkeypatch.setattr(identity_mod, "is_coverage_units_enabled", lambda: True)
    mock_lookup = create_autospec(cu_db.get_active_unit_by_workspace, return_value=unit)
    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", mock_lookup)


@pytest.mark.asyncio
async def test_tracking_home_workspace_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.dalton.autonomy.tracking import _resolve_workspace_ticker

    _install_workspace(monkeypatch, _ws(HOME_WS, "Home"))
    _install_docs(monkeypatch)
    with pytest.raises(ValueError, match="Home workspace"):
        await _resolve_workspace_ticker(HOME_WS)


@pytest.mark.asyncio
async def test_tracking_topic_workspace_no_longer_yields_a_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收：普通主题工作区不再被当成公司——旧逻辑取首词会解析出 "AI"。"""
    from src.dalton.autonomy.tracking import _resolve_workspace_ticker

    _install_workspace(monkeypatch, _ws("ws-topic", "AI Infrastructure"))
    _install_docs(monkeypatch)
    with pytest.raises(ValueError, match="Unable to resolve company ticker"):
        await _resolve_workspace_ticker("ws-topic")


@pytest.mark.asyncio
async def test_tracking_exact_ticker_name_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """bootstrap 约定（工作区名 = ticker）不受影响：整串即 ticker 的名字仍解析。"""
    from src.dalton.autonomy.tracking import _resolve_workspace_ticker

    _install_workspace(monkeypatch, _ws("ws-nvda", "NVDA"))
    _install_docs(monkeypatch)
    assert await _resolve_workspace_ticker("ws-nvda") == "NVDA"


@pytest.mark.asyncio
async def test_tracking_company_doc_and_automation_paths_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.autonomy.tracking import _resolve_workspace_ticker

    # 公司文档优先于中文名（老公司工作区主路径）
    _install_workspace(monkeypatch, _ws("ws-tx", "腾讯"))
    _install_docs(monkeypatch, company={"ticker": "0700.HK"})
    assert await _resolve_workspace_ticker("ws-tx") == "0700.HK"

    # 无文档无 metadata 时仍回退到已有自动化任务（已验证的跟踪信息；
    # metadata/ticker 步骤因 workspaces 表无此列在生产经 get_workspace 不可达）
    _install_workspace(monkeypatch, _ws("ws-auto", "随便什么名字"))
    _install_docs(monkeypatch)
    rows = [
        {"workspace_id": "ws-auto", "name": "dalton-amzn-watch", "metadata": {}},
        {"workspace_id": "ws-auto", "name": "dalton-amzn-delta", "metadata": {"ticker": "AMZN"}},
    ]
    assert await _resolve_workspace_ticker("ws-auto", existing_rows=rows) == "AMZN"


@pytest.mark.asyncio
async def test_tracking_registered_sector_refuses_even_with_ticker_name_and_doc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_units_lookup(monkeypatch, _sector_unit("ws-sector"))
    from src.dalton.autonomy.tracking import _resolve_workspace_ticker

    # 名为 NVDA 且带公司文档：sector 登记无条件，拒绝解析 ticker
    _install_workspace(monkeypatch, _ws("ws-sector", "NVDA"))
    _install_docs(monkeypatch, company={"ticker": "NVDA"})
    with pytest.raises(ValueError, match="sector"):
        await _resolve_workspace_ticker("ws-sector")


@pytest.mark.asyncio
async def test_tracking_registered_company_ticker_beats_compat_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_units_lookup(monkeypatch, _company_unit("ws-reg", "GOOGL"))
    from src.dalton.autonomy.tracking import _resolve_workspace_ticker

    _install_workspace(monkeypatch, _ws("ws-reg", "谷歌"))
    _install_docs(monkeypatch, company={"ticker": "META"})
    assert await _resolve_workspace_ticker("ws-reg") == "GOOGL"


# ---------------------------------------------------------------------------
# 看板：身份与 ticker 一律来自统一解析器
# ---------------------------------------------------------------------------


class _CursorProto:
    async def __aenter__(self) -> Any: ...  # noqa: E704
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...  # noqa: E704
    async def execute(self, query: Any, params: Any = None) -> Any: ...  # noqa: E704
    async def fetchone(self) -> Any: ...  # noqa: E704
    async def fetchall(self) -> Any: ...  # noqa: E704


class _ConnProto:
    async def __aenter__(self) -> Any: ...  # noqa: E704
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...  # noqa: E704
    def cursor(self, *args: Any, **kwargs: Any) -> Any: ...  # noqa: E704


def _make_spec_conn(cur: Any) -> Any:
    conn = create_autospec(_ConnProto, instance=True)
    conn.__aenter__.return_value = conn
    conn.__aexit__.return_value = None
    conn.cursor.return_value = cur
    return conn


_AGENDA_DUE = {
    "next_validations": [
        {"id": "V-001", "what": "核验", "due": "2020-01-01", "status": "pending"}
    ]
}


def _assert_psycopg_placeholders(sql: str, params: Any) -> None:
    """psycopg 3 parses ``%`` in any query sent WITH params: a bare literal
    ``%`` (``LIKE 'dalton-%'``) raises ProgrammingError in production, which
    the board / probe swallow into an empty preload. The doubles enforce the
    same rule so such a query cannot pass here."""
    if params is None:
        return
    import re

    stray = re.sub(r"%(?:[sbt%]|\([A-Za-z_][A-Za-z0-9_]*\)[sbt])", "", sql)
    assert "%" not in stray, f"literal % in a parameterized query: {sql}"


def _eval_automations_sql(
    sql: str, params: Any, table: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按预载 SQL 的真实语义在纯 Python 层执行（codex r2 对替身的要求）：
    名称预筛 ``strpos(lower(name), 'dalton-') > 0``（精确规则由调用方按 tracking 的 strip().lower() 执行）、user 过滤（`= %s` 或
    `= ANY(%s)`）、探针的 workspace 过滤（含 NULL 归属）、``ORDER BY file_name``。
    替身因此对 SQL 的筛选与排序做真实处理，而不是按表名回固定列表；
    源 SQL 去掉任一条件时替身行为随之改变（撤修即失败）。
    """
    rows = list(table)
    p = list(params or [])
    if "user_id = %s" in sql:
        rows = [r for r in rows if str(r.get("user_id") or "") == str(p[0])]
    elif "user_id = ANY(%s)" in sql:
        users = {str(u) for u in p[0]}
        rows = [r for r in rows if str(r.get("user_id") or "") in users]
    if "workspace_id = ANY(%s::uuid[])" in sql:
        wss = {str(w) for w in p[1]}
        rows = [
            r
            for r in rows
            if not str(r.get("workspace_id") or "").strip() or str(r.get("workspace_id")) in wss
        ]
    # 名称条件按 SQL 文本原样执行：lower(btrim(name)) LIKE 是"去空白+忽略
    # 大小写"的前缀匹配；退回区分大小写的 name LIKE 时，大写名会被漏掉
    if "strpos(lower(name), 'dalton-') > 0" in sql:
        rows = [r for r in rows if "dalton-" in str(r.get("name") or "").lower()]
    elif "name LIKE 'dalton-%'" in sql:
        rows = [r for r in rows if str(r.get("name") or "").startswith("dalton-")]
    if "ORDER BY file_name" in sql:
        rows = sorted(rows, key=lambda r: str(r.get("file_name") or ""))
    return rows


def _board_fake_cursor(
    ws_rows: list[dict[str, Any]],
    kv_rows: list[dict[str, Any]],
    auto_rows: list[dict[str, Any]] | None = None,
) -> Any:
    cur = create_autospec(_CursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None
    last: dict[str, Any] = {"sql": "", "params": None}

    async def _exec(sql: str, params: Any = None) -> None:
        _assert_psycopg_placeholders(str(sql), params)
        last["sql"] = " ".join(str(sql).split())
        last["params"] = params

    cur.execute.side_effect = _exec

    def _fetchall() -> list[dict[str, Any]]:
        sql = last["sql"]
        if "FROM workspaces" in sql:
            return ws_rows
        if "dalton_knowledge_versions" in sql:
            return kv_rows
        if "FROM automations" in sql:
            return _eval_automations_sql(sql, last["params"], auto_rows or [])
        return []

    cur.fetchall.side_effect = _fetchall
    cur.fetchone.return_value = {"cnt": 0}
    return cur


def _install_board(monkeypatch: pytest.MonkeyPatch, cur: Any) -> None:
    conn = _make_spec_conn(cur)
    mock_budget = create_autospec(board_mod.get_budget_today)
    mock_budget.return_value = {"cap_usd": 10.0, "spent_usd": 1.0, "open_reserved_usd": 0.0}
    monkeypatch.setattr(board_mod, "get_budget_today", mock_budget)
    mock_cov = create_autospec(board_mod.get_user_coverage, return_value={})
    monkeypatch.setattr(board_mod, "get_user_coverage", mock_cov)
    monkeypatch.setattr(board_mod, "get_db_connection", lambda: conn)


def _install_board_units(monkeypatch: pytest.MonkeyPatch, units_by_ws: dict[str, Any]) -> None:
    from src.dalton.database import coverage_units as cu_db

    monkeypatch.setattr(board_mod, "is_coverage_units_enabled", lambda: True)
    mock_load = create_autospec(cu_db.load_active_units_by_workspace, return_value=units_by_ws)
    monkeypatch.setattr(board_mod, "load_active_units_by_workspace", mock_load)


@pytest.mark.asyncio
async def test_board_skips_home_and_topic_workspaces(monkeypatch: pytest.MonkeyPatch) -> None:
    ws_rows = [
        {**_ws(HOME_WS, "Home"), "description": "", "updated_at": None},
        {**_ws("ws-topic", "AI Infrastructure"), "description": "行业", "updated_at": None},
        {**_ws("ws-tx", "腾讯"), "description": "", "updated_at": None},
        {**_ws("ws-nvda", "NVDA"), "description": "", "updated_at": None},
    ]
    kv_rows = [
        # Home 与主题工作区也挂着到期验证：不能再进入公司候选
        {"workspace_id": HOME_WS, "doc_key": "agenda", "version_no": 1, "content": _AGENDA_DUE, "created_at": None},
        {"workspace_id": "ws-topic", "doc_key": "agenda", "version_no": 1, "content": _AGENDA_DUE, "created_at": None},
        {"workspace_id": "ws-tx", "doc_key": "company", "version_no": 3, "content": {"ticker": "0700.HK", "exchange": "HKEX"}, "created_at": None},
        {"workspace_id": "ws-tx", "doc_key": "agenda", "version_no": 2, "content": _AGENDA_DUE, "created_at": None},
    ]
    cur = _board_fake_cursor(ws_rows, kv_rows)
    _install_board(monkeypatch, cur)

    board = await board_mod.build_coverage_board(user_id=U1)
    listed = {c["workspace_id"]: c["ticker"] for c in board["companies"]}
    assert listed == {"ws-tx": "0700.HK", "ws-nvda": "NVDA"}
    validation_ws = {
        c["workspace_id"] for c in board["candidates"] if c["kind"] == "validation_check"
    }
    assert validation_ws == {"ws-tx"}


@pytest.mark.asyncio
async def test_board_flag_on_sector_unit_on_ticker_named_workspace_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r1 #2：sector 登记在名为 NVDA 的工作区上——round 1 的"名称像
    ticker 即放行"会把它当公司列出；统一解析后无条件排除。"""
    _install_board_units(monkeypatch, {"ws-nvda": _sector_unit("ws-nvda")})
    ws_rows = [{**_ws("ws-nvda", "NVDA"), "description": "", "updated_at": None}]
    kv_rows = [
        {"workspace_id": "ws-nvda", "doc_key": "agenda", "version_no": 1, "content": _AGENDA_DUE, "created_at": None},
    ]
    cur = _board_fake_cursor(ws_rows, kv_rows)
    _install_board(monkeypatch, cur)

    board = await board_mod.build_coverage_board(user_id=U1)
    assert board["companies"] == []
    assert board["candidates"] == []


@pytest.mark.asyncio
async def test_board_home_named_like_ticker_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    """codex r1 #2：名为 "AI" 的 Home 也不能凭名称进看板（round 1 会放行）。"""
    ws_rows = [{**_ws(HOME_WS, "AI"), "description": "", "updated_at": None}]
    cur = _board_fake_cursor(ws_rows, [])
    _install_board(monkeypatch, cur)

    board = await board_mod.build_coverage_board(user_id=U1)
    assert board["companies"] == []


@pytest.mark.asyncio
async def test_board_uses_registered_company_ticker_not_workspace_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r1 #3："腾讯"绑定 company 单元 0700.HK、无公司文档 → 看板 ticker
    必须是 0700.HK（round 1 会显示"腾讯"）。"""
    _install_board_units(monkeypatch, {"ws-tx": _company_unit("ws-tx", "0700.HK")})
    ws_rows = [{**_ws("ws-tx", "腾讯"), "description": "", "updated_at": None}]
    kv_rows = [
        {"workspace_id": "ws-tx", "doc_key": "agenda", "version_no": 2, "content": _AGENDA_DUE, "created_at": None},
    ]
    cur = _board_fake_cursor(ws_rows, kv_rows)
    _install_board(monkeypatch, cur)

    board = await board_mod.build_coverage_board(user_id=U1)
    assert [(c["workspace_id"], c["ticker"]) for c in board["companies"]] == [("ws-tx", "0700.HK")]
    due = [c for c in board["candidates"] if c["kind"] == "validation_check"]
    assert len(due) == 1 and due[0]["ticker"] == "0700.HK"


@pytest.mark.asyncio
async def test_board_flag_off_includes_automation_only_old_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r1 #4：开关关闭，中文名工作区、无文档、只有 dalton-amzn-* 自动化
    ——tracking 能回退出 AMZN，看板也必须照常列出（round 1 会判未分类跳过）。"""
    ws_rows = [{**_ws("ws-amzn", "亚马逊"), "description": "", "updated_at": None}]
    auto_rows = [
        {"user_id": U1, "workspace_id": "ws-amzn", "name": "dalton-amzn-watch", "metadata": {}},
        {"user_id": U1, "workspace_id": "ws-amzn", "name": "dalton-amzn-delta", "metadata": {}},
    ]
    cur = _board_fake_cursor(ws_rows, [], auto_rows)
    _install_board(monkeypatch, cur)

    board = await board_mod.build_coverage_board(user_id=U1)
    assert [(c["workspace_id"], c["ticker"]) for c in board["companies"]] == [("ws-amzn", "AMZN")]


@pytest.mark.asyncio
async def test_board_flag_off_never_queries_units_table(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.dalton.database import coverage_units as cu_db

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("units table must not be queried while the flag is off")

    mock_load = create_autospec(cu_db.load_active_units_by_workspace, side_effect=_forbidden)
    monkeypatch.setattr(board_mod, "load_active_units_by_workspace", mock_load)

    cur = _board_fake_cursor(
        [{**_ws("ws-tx", "腾讯"), "description": "", "updated_at": None}],
        [
            {"workspace_id": "ws-tx", "doc_key": "company", "version_no": 3, "content": {"ticker": "0700.HK"}, "created_at": None}
        ],
    )
    _install_board(monkeypatch, cur)

    board = await board_mod.build_coverage_board(user_id=U1)
    assert [c["ticker"] for c in board["companies"]] == ["0700.HK"]
    mock_load.assert_not_called()


# ---------------------------------------------------------------------------
# 总监探针：身份与 ticker 一律来自统一解析器，时区按解析出的 ticker 算
# ---------------------------------------------------------------------------


def _probe_fake_cursor(
    kv_rows: list[dict[str, Any]],
    ws_rows: dict[str, str],
    auto_rows: list[dict[str, Any]] | None = None,
    ws_meta: dict[str, dict[str, str]] | None = None,
) -> Any:
    cur = create_autospec(_CursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None
    last: dict[str, Any] = {"sql": "", "params": None}

    async def _exec(sql: str, params: Any = None) -> None:
        _assert_psycopg_placeholders(str(sql), params)
        last["sql"] = " ".join(str(sql).split())
        last["params"] = params

    cur.execute.side_effect = _exec

    def _fetchall() -> list[dict[str, Any]]:
        sql = last["sql"]
        if "dalton_knowledge_versions" in sql:
            meta = ws_meta or {}
            out: list[dict[str, Any]] = []
            for r in kv_rows:
                ws_id = r["workspace_id"]
                out.append(
                    {
                        "workspace_id": ws_id,
                        "doc_key": r["doc_key"],
                        "content": r["content"],
                        "ws_name": ws_rows.get(ws_id, ""),
                        "ws_user_id": (meta.get(ws_id) or {}).get("ws_user_id", U1),
                        "ws_status": (meta.get(ws_id) or {}).get("ws_status", "running"),
                    }
                )
            return out
        if "FROM automations" in sql:
            return _eval_automations_sql(sql, last["params"], auto_rows or [])
        return []

    cur.fetchall.side_effect = _fetchall
    cur.fetchone.return_value = {"cnt": 0}
    return cur


def _probe_patch_pool(monkeypatch: pytest.MonkeyPatch, conn: Any) -> None:
    monkeypatch.setattr("src.server.database.pool.get_db_connection", lambda: conn)


@pytest.mark.asyncio
async def test_director_probe_counts_only_company_agendas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.autonomy.probes import director as director_mod

    kv_rows = [
        {"workspace_id": HOME_WS, "doc_key": "agenda", "content": _AGENDA_DUE},
        {"workspace_id": "ws-topic", "doc_key": "agenda", "content": _AGENDA_DUE},
        {"workspace_id": "ws-tx", "doc_key": "agenda", "content": _AGENDA_DUE},
        {"workspace_id": "ws-tx", "doc_key": "company", "content": {"ticker": "0700.HK", "exchange": "HKEX"}},
    ]
    ws_names = {HOME_WS: "Home", "ws-topic": "AI Infrastructure", "ws-tx": "腾讯"}
    cur = _probe_fake_cursor(kv_rows, ws_names)
    conn = _make_spec_conn(cur)
    _probe_patch_pool(monkeypatch, conn)

    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    counts = await director_mod._check_db_actionable_work(
        now_dt=now, now_date=now.date(), ttl_hours=168, user_id=U1
    )
    # 只有腾讯（company 文档）的验证项计入；Home 与主题工作区不再按伪 ticker 计到期
    assert counts["due_validations"] == 1


@pytest.mark.asyncio
async def test_director_probe_flag_on_sector_on_ticker_named_workspace_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r1 #2：sector 登记在名为 NVDA 的工作区——round 1 会按名称把它当
    公司计待办；统一解析后不计数。"""
    from src.dalton.autonomy.probes import director as director_mod
    from src.dalton.database import coverage_units as cu_db

    monkeypatch.setattr(director_mod, "is_coverage_units_enabled", lambda: True)
    mock_load = create_autospec(
        cu_db.load_active_units_by_workspace,
        return_value={"ws-nvda": _sector_unit("ws-nvda")},
    )
    monkeypatch.setattr(director_mod, "load_active_units_by_workspace", mock_load)

    kv_rows = [
        {"workspace_id": "ws-nvda", "doc_key": "agenda", "content": _AGENDA_DUE},
        {"workspace_id": "ws-nvda", "doc_key": "company", "content": {"ticker": "NVDA"}},
    ]
    cur = _probe_fake_cursor(kv_rows, {"ws-nvda": "NVDA"})
    conn = _make_spec_conn(cur)
    _probe_patch_pool(monkeypatch, conn)

    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    counts = await director_mod._check_db_actionable_work(
        now_dt=now, now_date=now.date(), ttl_hours=168, user_id=U1
    )
    assert counts["due_validations"] == 0


@pytest.mark.asyncio
async def test_director_probe_registered_company_ticker_sets_hk_due_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r1 #3："腾讯"绑定 company 单元 0700.HK、无文档：date-only 验证项
    必须按港股当地 18:00 到期。round 1 用工作区名推断成美东，同一时刻计数 0。"""
    from src.dalton.autonomy.probes import director as director_mod
    from src.dalton.database import coverage_units as cu_db

    monkeypatch.setattr(director_mod, "is_coverage_units_enabled", lambda: True)
    mock_load = create_autospec(
        cu_db.load_active_units_by_workspace,
        return_value={"ws-tx": _company_unit("ws-tx", "0700.HK")},
    )
    monkeypatch.setattr(director_mod, "load_active_units_by_workspace", mock_load)

    kv_rows = [
        {
            "workspace_id": "ws-tx",
            "doc_key": "agenda",
            "content": {
                "next_validations": [
                    {"id": "V-001", "what": "核验", "due": "2026-10-28", "status": "pending"}
                ]
            },
        },
    ]
    cur = _probe_fake_cursor(kv_rows, {"ws-tx": "腾讯"})
    conn = _make_spec_conn(cur)
    _probe_patch_pool(monkeypatch, conn)

    hk_before = datetime(2026, 10, 28, 17, 59, tzinfo=HK)
    counts_before = await director_mod._check_db_actionable_work(
        now_dt=hk_before, now_date=hk_before.date(), ttl_hours=168, user_id=U1
    )
    assert counts_before["due_validations"] == 0

    hk_due = datetime(2026, 10, 28, 18, 0, tzinfo=HK)  # 美东此刻才 06:00
    counts_due = await director_mod._check_db_actionable_work(
        now_dt=hk_due, now_date=hk_due.date(), ttl_hours=168, user_id=U1
    )
    assert counts_due["due_validations"] == 1


@pytest.mark.asyncio
async def test_director_probe_flag_off_counts_automation_only_old_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r1 #4：开关关闭，"亚马逊"无文档、只有 dalton-amzn-watch 自动化
    ——tracking 能回退，总监探针的到期验证也要计数（round 1 不计）。"""
    from src.dalton.autonomy.probes import director as director_mod

    kv_rows = [
        {
            "workspace_id": "ws-amzn",
            "doc_key": "agenda",
            "content": {
                "next_validations": [
                    {"id": "V-001", "what": "核验", "due": "2026-10-28", "status": "pending"}
                ]
            },
        },
    ]
    auto_rows = [
        {"user_id": U1, "workspace_id": "ws-amzn", "name": "dalton-amzn-watch", "metadata": {}},
    ]
    cur = _probe_fake_cursor(kv_rows, {"ws-amzn": "亚马逊"}, auto_rows)
    conn = _make_spec_conn(cur)
    _probe_patch_pool(monkeypatch, conn)

    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    counts = await director_mod._check_db_actionable_work(
        now_dt=now, now_date=now.date(), ttl_hours=168, user_id=U1
    )
    assert counts["due_validations"] == 1


# ---------------------------------------------------------------------------
# Round 3（codex r2）：预载筛选/顺序与 tracking 一致、旧文档交易所不越权、
# unbound 旧 Home 的探针排除
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")


@pytest.mark.asyncio
async def test_board_and_probe_recognize_uppercase_dalton_automation_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r2 #1：tracking 按 strip().lower().startswith("dalton-") 过滤，预载
    的 lower(btrim(name)) LIKE 必须等价——经通用入口改名成大写
    `DALTON-AMZN-WATCH` 的老公司自动化不能漏。"""
    # 表中故意混入一条非 dalton 前缀的行，替身按名称条件真实过滤
    auto_table = [
        {"user_id": U1, "workspace_id": "ws-amzn", "name": "\tDALTON-AMZN-WATCH", "file_name": "dalton-amzn-watch", "metadata": {}},
        {"user_id": U1, "workspace_id": "ws-amzn", "name": "weekly-hyper-growth", "file_name": "weekly-hyper-growth", "metadata": {}},
    ]

    # 看板
    ws_rows = [{**_ws("ws-amzn", "亚马逊"), "description": "", "updated_at": None}]
    cur = _board_fake_cursor(ws_rows, [], auto_table)
    _install_board(monkeypatch, cur)
    board = await board_mod.build_coverage_board(user_id=U1)
    assert [(c["workspace_id"], c["ticker"]) for c in board["companies"]] == [("ws-amzn", "AMZN")]

    # 总监探针：有到期 agenda 时同样计入
    from src.dalton.autonomy.probes import director as director_mod

    kv_rows = [
        {
            "workspace_id": "ws-amzn",
            "doc_key": "agenda",
            "content": {
                "next_validations": [
                    {"id": "V-001", "what": "核验", "due": "2026-10-28", "status": "pending"}
                ]
            },
        },
    ]
    cur_p = _probe_fake_cursor(kv_rows, {"ws-amzn": "亚马逊"}, auto_table)
    conn = _make_spec_conn(cur_p)
    _probe_patch_pool(monkeypatch, conn)
    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    counts = await director_mod._check_db_actionable_work(
        now_dt=now, now_date=now.date(), ttl_hours=168, user_id=U1
    )
    assert counts["due_validations"] == 1


@pytest.mark.asyncio
async def test_same_workspace_multiple_automations_resolve_identically_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r2 #2：同一工作区保留 MSFT 与 AMZN 两条自动化时，tracking 按
    file_name 顺序取 AMZN——看板 / 探针 / tracking 三处必须同一个结果。表中
    故意按 MSFT 在前存放，替身按 ORDER BY file_name 真实排序。"""
    from src.dalton.autonomy.tracking import _resolve_workspace_ticker

    auto_table = [
        {"user_id": U1, "workspace_id": "ws-mix", "name": "dalton-msft-watch", "file_name": "dalton-msft-watch", "metadata": {}},
        {"user_id": U1, "workspace_id": "ws-mix", "name": "dalton-amzn-watch", "file_name": "dalton-amzn-watch", "metadata": {}},
    ]
    assert sorted(r["file_name"] for r in auto_table)[0] == "dalton-amzn-watch"

    # tracking：真实加载路径给的是 file_name 顺序（list_all_user_automations）
    ordered = sorted(auto_table, key=lambda r: r["file_name"])
    assert await _resolve_workspace_ticker("ws-mix", existing_rows=ordered) == "AMZN"

    # 看板与探针：预载 SQL 的 ORDER BY file_name 决定解析器取第一条
    cur = _board_fake_cursor(
        [{**_ws("ws-mix", "随便什么名字"), "description": "", "updated_at": None}], [], auto_table
    )
    _install_board(monkeypatch, cur)
    board = await board_mod.build_coverage_board(user_id=U1)
    assert [(c["workspace_id"], c["ticker"]) for c in board["companies"]] == [("ws-mix", "AMZN")]

    from src.dalton.autonomy.probes import director as director_mod

    kv_rows = [
        {
            "workspace_id": "ws-mix",
            "doc_key": "agenda",
            "content": {
                "next_validations": [
                    {"id": "V-001", "what": "核验", "due": "2026-10-28", "status": "pending"}
                ]
            },
        },
    ]
    cur_p = _probe_fake_cursor(kv_rows, {"ws-mix": "随便什么名字"}, auto_table)
    conn = _make_spec_conn(cur_p)
    _probe_patch_pool(monkeypatch, conn)
    now = datetime(2026, 10, 28, 23, 0, tzinfo=timezone.utc)  # 美东 19:00：AMZN(US) 当天已到期
    counts = await director_mod._check_db_actionable_work(
        now_dt=now, now_date=now.date(), ttl_hours=168, user_id=U1
    )
    assert counts["due_validations"] == 1
    # 反证顺序敏感：若按表存放顺序（MSFT 在前）解析会得到 MSFT——预载的
    # ORDER BY 正是把存放顺序归一到 file_name 顺序
    from src.dalton.coverage.identity import classify_workspace_coverage

    unordered_identity = classify_workspace_coverage(
        _ws("ws-mix", "随便什么名字"), automations=auto_table
    )
    assert unordered_identity.ticker == "MSFT"


def test_ticker_market_timezone_ignores_stale_doc_exchange() -> None:
    """codex r2 #3：文档的 exchange 只属于文档自己的 ticker。"""
    from src.dalton.autonomy.due import ticker_market_timezone

    # 绑定公司单元 BABA，旧文档仍是 9988.HK/HKEX → 按解析出的 ticker 推断（美东）
    assert str(ticker_market_timezone("BABA", {"ticker": "9988.HK", "exchange": "HKEX"})) == "America/New_York"
    # 文档 ticker 与解析一致时 exchange 照常生效
    assert str(ticker_market_timezone("9988.HK", {"ticker": "9988.HK", "exchange": "HKEX"})) == "Asia/Hong_Kong"
    # 没有文档 ticker 时后缀推断
    assert str(ticker_market_timezone("0700.HK", {"exchange": "HKEX"})) == "Asia/Hong_Kong"


@pytest.mark.asyncio
async def test_director_probe_registered_baba_due_on_et_not_stale_hkex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r2 #3 集成：单元 BABA + 旧文档 9988.HK/HKEX + date-only 验证项。
    round 2 会把 HKEX 套在 BABA 上（港股 18:00 判到期，比美东早 12 小时）。"""
    from src.dalton.autonomy.probes import director as director_mod
    from src.dalton.database import coverage_units as cu_db

    monkeypatch.setattr(director_mod, "is_coverage_units_enabled", lambda: True)
    mock_load = create_autospec(
        cu_db.load_active_units_by_workspace,
        return_value={"ws-baba": _company_unit("ws-baba", "BABA")},
    )
    monkeypatch.setattr(director_mod, "load_active_units_by_workspace", mock_load)

    kv_rows = [
        {
            "workspace_id": "ws-baba",
            "doc_key": "agenda",
            "content": {
                "next_validations": [
                    {"id": "V-001", "what": "核验", "due": "2026-10-28", "status": "pending"}
                ]
            },
        },
        {"workspace_id": "ws-baba", "doc_key": "company", "content": {"ticker": "9988.HK", "exchange": "HKEX"}},
    ]
    cur = _probe_fake_cursor(kv_rows, {"ws-baba": "阿里巴巴"})
    conn = _make_spec_conn(cur)
    _probe_patch_pool(monkeypatch, conn)

    # UTC 15:00（美东 11:00、香港 23:00）：按美东未到期；round 2 的旧 HKEX 已判到期
    et_not_due = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
    counts_not_due = await director_mod._check_db_actionable_work(
        now_dt=et_not_due, now_date=et_not_due.date(), ttl_hours=168, user_id=U1
    )
    assert counts_not_due["due_validations"] == 0

    # 美东当地 18:00 后到期
    et_due = datetime(2026, 10, 28, 22, 1, tzinfo=timezone.utc)  # 美东 18:01
    counts_due = await director_mod._check_db_actionable_work(
        now_dt=et_due, now_date=et_due.date(), ttl_hours=168, user_id=U1
    )
    assert counts_due["due_validations"] == 1


@pytest.mark.asyncio
async def test_director_probe_excludes_unbound_legacy_home_named_like_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r2 #4：账号合并遗留的旧 Home（status='unbound'、ID 不再匹配新
    所有者）名为 AI、带到期 agenda——探针必须靠真实 status 排除，不能按
    ticker 形状的名称计入公司待办。"""
    from src.dalton.autonomy.probes import director as director_mod

    old_home = "77777777-7777-7777-7777-777777777777"  # 非 U1 的 uuid5 Home
    kv_rows = [
        {
            "workspace_id": old_home,
            "doc_key": "agenda",
            "content": {
                "next_validations": [
                    {"id": "V-001", "what": "核验", "due": "2026-10-28", "status": "pending"}
                ]
            },
        },
    ]
    cur = _probe_fake_cursor(
        kv_rows,
        {old_home: "AI"},
        ws_meta={old_home: {"ws_user_id": U1, "ws_status": "unbound"}},
    )
    conn = _make_spec_conn(cur)
    _probe_patch_pool(monkeypatch, conn)

    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    counts = await director_mod._check_db_actionable_work(
        now_dt=now, now_date=now.date(), ttl_hours=168, user_id=U1
    )
    assert counts["due_validations"] == 0
