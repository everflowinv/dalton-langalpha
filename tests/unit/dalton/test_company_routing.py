"""公司级研究的工作区路由（生产线程 ada64139 复盘，第 2 轮）。

锁定（第 2 轮六项决定）：
- 当前工作区（不论属主——同事共享的工作区）就是目标公司 → 照常放行，
  不查调用者自己的候选；
- 目标公司有自己的工作区（不是当前）→ CompanyRoutingRefused，指名那个
  工作区，引导切换/从研究总监委派，不建议相对路径；
- 目标公司没有工作区 → 通过，回执带"产物留在当前工作区"提示；
- 行业工作区（sector 单元 / sector 文档 / dalton-sector 任务任一标记）永
  不作为某家公司的"自己的工作区"——行业工作区研究成员公司时写下的
  company 文档不能让它冒充（agy 1）；
- 只查当前用户自己的候选：finder 只收到 configurable 的 user_id，真实
  枚举 SQL 按 user_id = %s 过滤（codex 2 / agy 2）；
- ticker 比较先经 cn/hk 探针归一（300750 == 300750.SZ、0700.HK == 00700、
  600519.SH == 600519.SS）（审查 3）；
- workspace_dir：有锚定根时绝对路径必须在根下，相对路径不得以 .. 开头；
  无锚定根时绝对路径不判（审查 4）；check_done 同检；
- check_done 提示只对公司级名单生效（agy 5）；文档只投影 ticker（agy 6）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import pytest

import src.dalton.workflows.company_routing as cr_mod
from src.dalton.coverage.identity import (
    KIND_COMPANY,
    KIND_SECTOR,
    KIND_UNCLASSIFIED,
    SOURCE_COMPAT,
    WorkspaceCoverageIdentity,
)
from src.dalton.workflows.company_routing import (
    COMPANY_WORKFLOWS,
    CompanyRoutingRefused,
    _ticker_key,
    _workspace_dir_escapes,
    find_company_workspaces,
    resolve_launch_company_routing,
    routing_hint_for_check,
)

U1 = "user-1"
U2 = "user-2"
WS_CURRENT = "ws-current"
WS_OTHER = "ws-other"
ANCHOR = "/home/daytona/workspace/农业"


def _ws(ws_id: str, name: str, user_id: str = U1) -> dict[str, Any]:
    # 只含真实投影里的列（workspaces 表的 SELECT 列集）。
    return {"workspace_id": ws_id, "user_id": user_id, "name": name, "status": "running"}


def _company_identity(ticker: str) -> WorkspaceCoverageIdentity:
    return WorkspaceCoverageIdentity(KIND_COMPANY, SOURCE_COMPAT, ticker)


# 模块加载时取真函数：替身helper 在同一测试里可能被调用多次，此时模块
# 属性已是上一个 mock，不能再拿它做 spec。
_REAL_FIND = cr_mod.find_company_workspaces
_REAL_CURRENT = cr_mod._resolve_workspace_company
from src.server.database.pool import get_db_connection as _REAL_POOL
from src.server.database.workspace import get_workspace as _REAL_GET_WORKSPACE


def _finder(
    monkeypatch: pytest.MonkeyPatch, result: list[dict[str, Any]] | Exception
) -> Any:
    if isinstance(result, Exception):
        fake = create_autospec(_REAL_FIND, side_effect=result)
    else:
        fake = create_autospec(_REAL_FIND, return_value=result)
    monkeypatch.setattr(cr_mod, "find_company_workspaces", fake)
    return fake


def _current(
    monkeypatch: pytest.MonkeyPatch,
    identity: WorkspaceCoverageIdentity | None | Exception,
    is_sector: bool = False,
) -> Any:
    if isinstance(identity, Exception):
        fake = create_autospec(_REAL_CURRENT, side_effect=identity)
    else:
        fake = create_autospec(_REAL_CURRENT, return_value=(identity, is_sector))
    monkeypatch.setattr(cr_mod, "_resolve_workspace_company", fake)
    return fake


# ---------------------------------------------------------------------------
# 决定 2：当前工作区（不论属主）就是目标公司 → 放行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_workspace_is_target_company_passes_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前工作区就是该公司（含同事共享的工作区）→ 放行，不查自己的候选。"""
    current = _current(monkeypatch, _company_identity("002714.SZ"))
    fake = _finder(monkeypatch, [_ws(WS_OTHER, "另一份同名")])
    note = await resolve_launch_company_routing(
        "initial-coverage",
        {"ticker": "002714.SZ", "workspace_dir": "."},
        # user 是 A，工作区属主是 B（共享）——身份判定只看当前工作区本身。
        {"user_id": U1, "workspace_id": WS_CURRENT},
    )
    assert note is None
    current.assert_awaited_once_with(WS_CURRENT)
    fake.assert_not_awaited()


@pytest.mark.asyncio
async def test_ticker_format_variance_still_matches_current_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """决定 3：参数与工作区身份的代码格式不同（裸码/后缀/前导零）也算同一家。"""
    for identity_ticker, param in [
        ("300750.SZ", "300750"),
        ("0700.HK", "00700"),
        ("600519.SS", "600519.SH"),
        ("NVDA", "nvda"),
    ]:
        _current(monkeypatch, _company_identity(identity_ticker))
        _finder(monkeypatch, [_ws(WS_OTHER, "诱饵")])
        note = await resolve_launch_company_routing(
            "full-coverage",
            {"ticker": param, "workspace_dir": "."},
            {"user_id": U1, "workspace_id": WS_CURRENT},
        )
        assert note is None, (identity_ticker, param)


def test_ticker_key_normalization_table() -> None:
    # A 股：裸 6 位按代码段推断交易所，.SH 折成 .SS。
    assert _ticker_key("300750") == _ticker_key("300750.SZ") == "300750.SZ"
    assert _ticker_key("600519.SH") == _ticker_key("600519") == "600519.SS"
    assert _ticker_key("sz002714") == "002714.SZ"
    # 港股：700 / 0700.HK / 00700 同键（5 位数字）。
    assert _ticker_key("0700.HK") == _ticker_key("00700") == "00700"
    # 其余市场原样大写；不同代码不得同键。
    assert _ticker_key("nvda") == "NVDA"
    assert _ticker_key("002714") != _ticker_key("000333")
    assert _ticker_key("") is None and _ticker_key(None) is None


# ---------------------------------------------------------------------------
# 决定 1 + 三分支：行业工作区永不冒充成员公司的"自己的工作区"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sector_workspace_with_member_company_doc_is_not_current_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agy 1：行业工作区研究成员公司时写下的 company 文档不能让它冒充
    "当前就是这家公司"——仍要走候选查找并拒绝。"""
    # 身份解析确实把它解析成公司（兼容规则），但行业标记为真。
    _current(monkeypatch, _company_identity("002714.SZ"), is_sector=True)
    _finder(monkeypatch, [_ws(WS_OTHER, "农业-牧原")])
    with pytest.raises(CompanyRoutingRefused, match="「农业-牧原」"):
        await resolve_launch_company_routing(
            "bullbear-doc",
            {"ticker": "002714.SZ", "workspace_dir": "."},
            {"user_id": U1, "workspace_id": WS_CURRENT},
        )


@pytest.mark.asyncio
async def test_company_with_another_workspace_is_refused_with_its_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _current(monkeypatch, WorkspaceCoverageIdentity(KIND_SECTOR, SOURCE_COMPAT))
    _finder(monkeypatch, [_ws(WS_OTHER, "农业-牧原")])
    with pytest.raises(CompanyRoutingRefused) as excinfo:
        await resolve_launch_company_routing(
            "full-coverage",
            {"ticker": "002714.SZ", "workspace_dir": "."},
            {"user_id": U1, "workspace_id": WS_CURRENT},
        )
    message = str(excinfo.value)
    # 指名目标工作区，给两条正路，不给相对路径的建议。
    assert "「农业-牧原」" in message
    assert "切换" in message
    assert "研究总监（Home）" in message
    assert "不要用相对路径" in message


@pytest.mark.asyncio
async def test_company_without_workspace_passes_with_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分支三：行业工作区研究尚未建工作区的成员公司是允许的；即便行业
    工作区带着这家公司的 company 文档（残留），也不改提示。"""
    _current(
        monkeypatch, _company_identity("002714.SZ"), is_sector=True
    )
    _finder(monkeypatch, [])
    note = await resolve_launch_company_routing(
        "initial-coverage",
        {"ticker": "002714", "workspace_dir": "."},
        {"user_id": U1, "workspace_id": WS_CURRENT},
    )
    assert note is not None
    assert "还没有自己的工作区" in note
    assert "留在当前工作区" in note


@pytest.mark.asyncio
async def test_only_the_callers_own_workspaces_are_consulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """finder 只拿 configurable 里的当前用户；别人的 user_id 无从传入。"""
    _current(monkeypatch, None)
    fake = _finder(monkeypatch, [])
    await resolve_launch_company_routing(
        "earnings-deep-update",
        {"ticker": "META"},
        {"user_id": U1, "workspace_id": WS_CURRENT},
    )
    fake.assert_awaited_once()
    assert fake.await_args.args == (U1, "META")

    # 手动核对路径同样只查当前用户。
    _current(monkeypatch, None)
    fake2 = _finder(monkeypatch, [])
    await routing_hint_for_check(
        {"ticker": "META"}, U2, WS_OTHER, workflow="initial-coverage"
    )
    fake2.assert_awaited_once()
    assert fake2.await_args.args == (U2, "META")


@pytest.mark.asyncio
async def test_non_company_workflows_and_missing_identity_skip_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _current(monkeypatch, None)
    fake = _finder(monkeypatch, [_ws(WS_OTHER, "农业-牧原")])
    # 非 company workflow（topic-research / weekly）不触发。
    assert (
        await resolve_launch_company_routing(
            "topic-research", {"ticker": "META"}, {"user_id": U1}
        )
        is None
    )
    # 无 ticker、无 user_id 同样不查库。
    assert (
        await resolve_launch_company_routing(
            "initial-coverage", {"as_of": "2026-11-01"}, {"user_id": U1}
        )
        is None
    )
    assert (
        await resolve_launch_company_routing(
            "initial-coverage", {"ticker": "META"}, {"workspace_id": WS_CURRENT}
        )
        is None
    )
    current.assert_not_awaited()
    fake.assert_not_awaited()


@pytest.mark.asyncio
async def test_db_failure_passes_through_without_blocking_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _current(monkeypatch, RuntimeError("pool timeout"))
    note = await resolve_launch_company_routing(
        "initial-coverage",
        {"ticker": "META"},
        {"user_id": U1, "workspace_id": WS_CURRENT},
    )
    assert note is None

    # 当前工作区正常解析、候选查找失败：同样放行。
    _current(monkeypatch, WorkspaceCoverageIdentity(KIND_UNCLASSIFIED, KIND_UNCLASSIFIED))
    _finder(monkeypatch, RuntimeError("pool timeout"))
    assert (
        await resolve_launch_company_routing(
            "initial-coverage",
            {"ticker": "META"},
            {"user_id": U1, "workspace_id": WS_CURRENT},
        )
        is None
    )
    assert (
        await routing_hint_for_check(
            {"ticker": "META"}, U1, WS_CURRENT, workflow="initial-coverage"
        )
        is None
    )


# ---------------------------------------------------------------------------
# 决定 4：产物根目录 workspace_dir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escaping_relative_workspace_dir_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ticker 路由通过（当前就是该公司），但产物根目录指向别处——同样拒绝。
    _current(monkeypatch, _company_identity("META"))
    with pytest.raises(CompanyRoutingRefused, match="越出了当前工作区"):
        await resolve_launch_company_routing(
            "initial-coverage",
            {"ticker": "META", "workspace_dir": "../农业-牧原"},
            {"user_id": U1, "workspace_id": WS_CURRENT},
        )
    # 分支三（公司尚无工作区）时同样不允许越出。
    _current(monkeypatch, None)
    _finder(monkeypatch, [])
    with pytest.raises(CompanyRoutingRefused, match="workspace_dir"):
        await resolve_launch_company_routing(
            "initial-coverage",
            {"ticker": "META", "workspace_dir": "../其它"},
            {"user_id": U1, "workspace_id": WS_CURRENT},
        )


@pytest.mark.asyncio
async def test_absolute_workspace_dir_judged_against_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有锚定根（backend.workspace_dir）时：根下（含子目录、含 `.` 归一）
    放行，根外（别的沙箱绝对路径）拒绝。"""
    _current(monkeypatch, _company_identity("META"))
    for ok_dir in (".", ANCHOR, f"{ANCHOR}/research"):
        note = await resolve_launch_company_routing(
            "initial-coverage",
            {"ticker": "META", "workspace_dir": ok_dir},
            {"user_id": U1, "workspace_id": WS_CURRENT},
            workspace_root=ANCHOR,
        )
        assert note is None, ok_dir
    for bad_dir in ("/home/daytona/workspace/农业-牧原", f"{ANCHOR}/../农业-牧原"):
        with pytest.raises(CompanyRoutingRefused, match="越出了当前工作区"):
            await resolve_launch_company_routing(
                "initial-coverage",
                {"ticker": "META", "workspace_dir": bad_dir},
                {"user_id": U1, "workspace_id": WS_CURRENT},
                workspace_root=ANCHOR,
            )


@pytest.mark.asyncio
async def test_absolute_workspace_dir_without_anchor_is_not_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无锚定根（后端不暴露 workspace_dir）：绝对路径无从判定、维持放行；
    相对路径的词法逃逸检查仍然生效。符号链接同属不解析之列（见模块
    docstring：要进沙箱 realpath，不在启动路径做）。"""
    _current(monkeypatch, _company_identity("META"))
    note = await resolve_launch_company_routing(
        "initial-coverage",
        {"ticker": "META", "workspace_dir": "/home/daytona/workspace/别的"},
        {"user_id": U1, "workspace_id": WS_CURRENT},
    )
    assert note is None
    assert _workspace_dir_escapes("../x") is True
    assert _workspace_dir_escapes("/abs/x") is False  # 无根时不判
    assert _workspace_dir_escapes("research/../drafts") is False
    assert _workspace_dir_escapes(None) is False


# ---------------------------------------------------------------------------
# dalton_check_done 手动路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_done_manual_path_gets_routing_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _current(monkeypatch, WorkspaceCoverageIdentity(KIND_SECTOR, SOURCE_COMPAT))
    _finder(monkeypatch, [_ws(WS_OTHER, "农业-牧原")])
    hint = await routing_hint_for_check(
        {"ticker": "002714.SZ", "manual": True},
        U1,
        WS_CURRENT,
        workflow="initial-coverage",
    )
    assert hint is not None
    assert "路由不符" in hint
    assert "「农业-牧原」" in hint
    assert "研究总监（Home）" in hint
    assert "不要用相对路径" in hint

    # ticker 就是当前工作区：不给指引，也不查候选。
    _current(monkeypatch, _company_identity("META"))
    fake = _finder(monkeypatch, [_ws(WS_OTHER, "诱饵")])
    assert (
        await routing_hint_for_check(
            {"ticker": "META"}, U1, WS_CURRENT, workflow="initial-coverage"
        )
        is None
    )
    fake.assert_not_awaited()

    # 公司尚无工作区、无 user_id：不给 ticker 指引。
    current2 = _current(monkeypatch, None)
    fake2 = _finder(monkeypatch, [_ws(WS_OTHER, "META")])
    assert (
        await routing_hint_for_check(
            {"ticker": "META"}, None, WS_CURRENT, workflow="initial-coverage"
        )
        is None
    )
    current2.assert_not_awaited()
    fake2.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_done_hint_gated_to_company_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agy 5：非公司级流程（topic-research 多传了 ticker）不给路由指引。"""
    _current(monkeypatch, None)
    fake = _finder(monkeypatch, [_ws(WS_OTHER, "农业-牧原")])
    assert (
        await routing_hint_for_check(
            {"ticker": "002714.SZ"}, U1, WS_CURRENT, workflow="topic-research"
        )
        is None
    )
    assert (
        await routing_hint_for_check(
            {"ticker": "002714.SZ"}, U1, WS_CURRENT, workflow="dalton-stock-move"
        )
        is None
    )
    fake.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_done_reports_escaping_workspace_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审查 4：ticker 对得上但产物根目录越出锚定根 → missing 指引。"""
    _current(monkeypatch, _company_identity("META"))
    hint = await routing_hint_for_check(
        {"ticker": "META", "workspace_dir": "/home/daytona/workspace/农业-牧原"},
        U1,
        WS_CURRENT,
        workflow="initial-coverage",
        workspace_root=ANCHOR,
    )
    assert hint is not None
    assert "越出了当前工作区" in hint

    # 锚定根下则无指引。
    assert (
        await routing_hint_for_check(
            {"ticker": "META", "workspace_dir": f"{ANCHOR}/research"},
            U1,
            WS_CURRENT,
            workflow="initial-coverage",
            workspace_root=ANCHOR,
        )
        is None
    )


@pytest.mark.asyncio
async def test_run_check_done_leads_missing_with_the_routing_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_check_done 把路由指引放 missing 首位（ada64139：先指路再列缺项）。"""
    from src.dalton.tools.check_done_tools import run_check_done

    _current(monkeypatch, WorkspaceCoverageIdentity(KIND_SECTOR, SOURCE_COMPAT))
    _finder(monkeypatch, [_ws(WS_OTHER, "农业-牧原")])
    result = await run_check_done(
        workflow="initial-coverage",
        params={
            "ticker": "002714.SZ",
            "workspace_dir": ".",
            "as_of": "2026-11-01",
            "manual": True,
        },
        backend=object(),  # 后续文件核对注定失败——本测试只看首位
        workspace_id=WS_CURRENT,
        user_id=U1,
    )
    assert result["done"] is False
    assert result["missing"], "路由不符时 missing 不得为空"
    assert "路由不符" in result["missing"][0]
    assert "「农业-牧原」" in result["missing"][0]


# ---------------------------------------------------------------------------
# 真实枚举与当前工作区解析：SQL、行业标记、ticker 投影
# ---------------------------------------------------------------------------


class _ConnCtx:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        return None


class _ConnProto:
    def cursor(self, row_factory: Any = None) -> Any: ...  # noqa: E704


class _RoutingCursorProto:
    async def __aenter__(self) -> Any: ...  # noqa: E704
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...  # noqa: E704
    async def execute(self, query: Any, params: Any = None) -> Any: ...  # noqa: E704
    async def fetchall(self) -> Any: ...  # noqa: E704


def _scripted_conn(monkeypatch: pytest.MonkeyPatch, rows_for: Any) -> list[tuple[str, tuple[Any, ...]]]:
    """接上假的 get_db_connection；``rows_for(sql)`` 决定每次 fetchall 的行。"""
    from src.server.database import pool as pool_mod

    executed: list[tuple[str, tuple[Any, ...]]] = []

    cur = create_autospec(_RoutingCursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    async def _exec(sql: str, params: Any = None) -> None:
        executed.append((" ".join(str(sql).split()), tuple(params or ())))

    async def _fetchall() -> Any:
        return rows_for(executed[-1][0], executed[-1][1], executed)

    cur.execute.side_effect = _exec
    cur.fetchall.side_effect = _fetchall

    conn = create_autospec(_ConnProto, instance=True)
    conn.cursor.return_value = cur
    ctx = _ConnCtx(conn)
    mock_pool = create_autospec(_REAL_POOL, side_effect=lambda conn=None: ctx)
    monkeypatch.setattr(pool_mod, "get_db_connection", mock_pool)
    return executed


@pytest.mark.asyncio
async def test_find_company_workspaces_excludes_sector_and_projects_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 find_company_workspaces（agy 1 + 6）：工作区查询按 user_id = %s
    过滤；文档只投影 ticker 且把 sector 文档一并取作行业标记；带着成员
    公司 company 文档的行业工作区不进候选。"""
    monkeypatch.setattr(cr_mod, "is_coverage_units_enabled", lambda: False)

    docs_by_ws = {
        WS_CURRENT: {
            # 行业工作区「农业」：sector 文档 + 研究成员公司时写下的 company 文档。
            "sector": {"workspace_id": WS_CURRENT, "doc_key": "sector", "content": {"ticker": None}},
            "company": {
                "workspace_id": WS_CURRENT,
                "doc_key": "company",
                "content": {"ticker": "002714.SZ"},
            },
        },
        WS_OTHER: {
            "company": {
                "workspace_id": WS_OTHER,
                "doc_key": "company",
                "content": {"ticker": "002714.SZ"},
            },
            "thesis": {
                "workspace_id": WS_OTHER,
                "doc_key": "thesis",
                "content": {"ticker": "002714.SZ"},
            },
        },
    }

    def rows_for(sql: str, params: tuple[Any, ...], _executed: Any) -> list[dict[str, Any]]:
        if "FROM workspaces" in sql:
            # SQL 过滤 + 参数绑定：本人 user_id 是唯一传入的属主。
            assert "user_id = %s" in sql
            assert params == (U1,)
            return [_ws(WS_CURRENT, "农业"), _ws(WS_OTHER, "农业-牧原")]
        if "FROM dalton_coverage_units" in sql:
            return []
        if "FROM automations" in sql:
            return []
        if "dalton_knowledge_versions" in sql:
            # 决定 6：只投影 ticker；sector 文档在 IN 清单里作行业标记。
            assert "jsonb_build_object('ticker'" in sql
            assert "'sector'" in sql
            rows = []
            for ws_id, docs in docs_by_ws.items():
                rows.extend(docs.values())
            return rows
        raise AssertionError(f"unexpected query: {sql}")

    _scripted_conn(monkeypatch, rows_for)

    matches = await find_company_workspaces(U1, "002714")
    assert [m["workspace_id"] for m in matches] == [WS_OTHER]
    assert matches[0]["name"] == "农业-牧原"

    # 对照：行业工作区若无任何行业标记（未初始化行业层、无行业任务），
    # 系统身份规则本身视其为公司工作区，路由只能遵循同一规则。
    docs_by_ws[WS_CURRENT].pop("sector")

    matches2 = await find_company_workspaces(U1, "002714")
    assert {m["workspace_id"] for m in matches2} == {WS_CURRENT, WS_OTHER}


@pytest.mark.asyncio
async def test_resolve_workspace_company_marks_sector_by_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 _resolve_workspace_company：三个行业标记（sector 文档、
    dalton-sector 自动化、sector 单元）任一命中即行业；带着成员公司
    company 文档的行业工作区拿到 (公司身份, 行业=True)——启动路径因此不会
    把它当作"当前就是这家公司"。"""
    from src.server.database import workspace as ws_db

    monkeypatch.setattr(cr_mod, "is_coverage_units_enabled", lambda: False)

    def _install(ws: dict[str, Any], docs: list[dict[str, Any]], autos: list[dict[str, Any]]) -> None:
        fake_ws = create_autospec(_REAL_GET_WORKSPACE, return_value=ws)
        monkeypatch.setattr(ws_db, "get_workspace", fake_ws)

        def rows_for(sql: str, params: tuple[Any, ...], _executed: Any) -> list[dict[str, Any]]:
            if "FROM automations" in sql:
                return autos
            if "dalton_knowledge_versions" in sql:
                return docs
            raise AssertionError(f"unexpected query: {sql}")

        _scripted_conn(monkeypatch, rows_for)

    agri = _ws(WS_CURRENT, "农业")

    # 1) sector 文档存在（即便同时带着成员公司的 company 文档）。
    _install(
        agri,
        [
            {"workspace_id": WS_CURRENT, "doc_key": "sector", "content": {"ticker": None}},
            {"workspace_id": WS_CURRENT, "doc_key": "company", "content": {"ticker": "002714.SZ"}},
        ],
        [],
    )
    identity, is_sector = await cr_mod._resolve_workspace_company(WS_CURRENT)
    assert identity is not None and identity.is_company
    assert identity.ticker == "002714.SZ"
    assert is_sector is True

    # 2) 无 sector 文档，但本工作区挂着 dalton-sector-* 自动化（严格
    #    workspace_id 匹配；legacy 空工作区行不算）。
    _install(
        agri,
        [{"workspace_id": WS_CURRENT, "doc_key": "company", "content": {"ticker": "002714.SZ"}}],
        [
            {"user_id": U1, "workspace_id": WS_CURRENT, "name": "dalton-sector-agri-delta", "metadata": {}},
            {"user_id": U1, "workspace_id": "", "name": "dalton-sector-legacy-agenda", "metadata": {}},
        ],
    )
    _, is_sector2 = await cr_mod._resolve_workspace_company(WS_CURRENT)
    assert is_sector2 is True

    # 3) 无任何行业标记：普通公司工作区。
    _install(
        _ws(WS_OTHER, "农业-牧原"),
        [{"workspace_id": WS_OTHER, "doc_key": "company", "content": {"ticker": "002714.SZ"}}],
        [],
    )
    identity3, is_sector3 = await cr_mod._resolve_workspace_company(WS_OTHER)
    assert identity3 is not None and identity3.is_company
    assert is_sector3 is False

    # 4) sector 单元（开关开启）：行业注册身份压过一切。
    monkeypatch.setattr(cr_mod, "is_coverage_units_enabled", lambda: True)
    from src.dalton.database import coverage_units as cu_db

    fake_unit = create_autospec(
        cu_db.get_active_unit_by_workspace,
        return_value={"unit_id": "u1", "kind": "sector", "workspace_id": WS_CURRENT, "ticker": None, "status": "active"},
    )
    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", fake_unit)
    _install(agri, [], [])
    _, is_sector4 = await cr_mod._resolve_workspace_company(WS_CURRENT)
    assert is_sector4 is True


def test_company_workflow_list_matches_the_contracts() -> None:
    """COMPANY_WORKFLOWS 恰好是全部 workflow 契约里 params_required 带 ticker
    的那几个——新增公司级 workflow 时名单必须跟着改。"""
    import json
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    ticker_workflows = set()
    for path in sorted((repo / "workflows").glob("*/contract.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if "ticker" in doc.get("params_required", []):
            ticker_workflows.add(doc["name"])
    assert ticker_workflows == set(COMPANY_WORKFLOWS)


@pytest.mark.asyncio
async def test_find_company_workspaces_skips_historical_duplicates_and_respects_colleague_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical duplicate workspaces are excluded; colleague workspaces only match when web-readable."""
    import src.dalton.autonomy.units_view as units_view_mod

    monkeypatch.setattr(cr_mod, "is_coverage_units_enabled", lambda: True)
    colleague_delegatable_ws = "c0c0c0c0-c0c0-4c0c-8c0c-c0c0c0c0c001"
    colleague_ro_ws = "c0c0c0c0-c0c0-4c0c-8c0c-c0c0c0c0c003"
    colleague_hidden_ws = "c0c0c0c0-c0c0-4c0c-8c0c-c0c0c0c0c002"
    dup_ws = "d0d0d0d0-d0d0-4d0d-8d0d-d0d0d0d0d001"

    docs_by_ws = {
        WS_CURRENT: {"workspace_id": WS_CURRENT, "doc_key": "company", "content": {"ticker": "NVDA"}},
        dup_ws: {"workspace_id": dup_ws, "doc_key": "company", "content": {"ticker": "NVDA"}},
        colleague_delegatable_ws: {"workspace_id": colleague_delegatable_ws, "doc_key": "company", "content": {"ticker": "NVDA"}},
        colleague_ro_ws: {"workspace_id": colleague_ro_ws, "doc_key": "company", "content": {"ticker": "NVDA"}},
        colleague_hidden_ws: {"workspace_id": colleague_hidden_ws, "doc_key": "company", "content": {"ticker": "NVDA"}},
    }

    async def _fake_colleague_access(uid: str) -> dict[str, tuple[bool, bool]]:
        return {
            colleague_delegatable_ws: (True, True),
            colleague_ro_ws: (True, False),
            colleague_hidden_ws: (False, False),
        }

    monkeypatch.setattr(
        units_view_mod,
        "load_colleague_workspace_access",
        create_autospec(units_view_mod.load_colleague_workspace_access, side_effect=_fake_colleague_access),
    )

    def rows_for(sql: str, params: tuple[Any, ...], _executed: Any) -> list[dict[str, Any]]:
        if "FROM workspaces" in sql and "user_id = %s" in sql:
            return [
                {**_ws(WS_CURRENT, "NVDA Canonical"), "config": {}},
                {
                    **_ws(dup_ws, "NVDA Old Dup"),
                    "config": {"dalton_historical_duplicate_of": WS_CURRENT},
                },
            ]
        if "FROM workspaces" in sql and "ANY(%s::uuid[])" in sql:
            wanted = {str(w) for w in (params[0] if params else [])}
            out = []
            if colleague_delegatable_ws in wanted:
                out.append({**_ws(colleague_delegatable_ws, "Colleague NVDA Delegatable"), "config": {}})
            if colleague_ro_ws in wanted:
                out.append({**_ws(colleague_ro_ws, "Colleague NVDA ReadOnly"), "config": {}})
            return out
        if "FROM dalton_coverage_units" in sql:
            return []
        if "FROM automations" in sql:
            return []
        if "dalton_knowledge_versions" in sql:
            wanted = {str(w) for w in (params[0] if params else [])}
            return [doc for ws_id, doc in docs_by_ws.items() if ws_id in wanted]
        raise AssertionError(f"unexpected query: {sql}")

    _scripted_conn(monkeypatch, rows_for)
    matches = await find_company_workspaces(U1, "NVDA")
    by_ws = {m["workspace_id"]: m for m in matches}
    assert set(by_ws) == {WS_CURRENT, colleague_delegatable_ws, colleague_ro_ws}
    assert by_ws[WS_CURRENT]["delegatable"] is True
    assert by_ws[colleague_delegatable_ws]["delegatable"] is True
    assert by_ws[colleague_ro_ws]["delegatable"] is False


@pytest.mark.asyncio
async def test_launch_routing_allows_readonly_colleague_with_hint_but_refuses_delegatable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 7 (Codex M5): read-only colleague workspace returns advisory hint without refusing;
    delegatable colleague workspace refuses with CompanyRoutingRefused."""
    colleague_ro = "c0c0c0c0-c0c0-4c0c-8c0c-c0c0c0c0c011"
    colleague_del = "c0c0c0c0-c0c0-4c0c-8c0c-c0c0c0c0c012"

    # Sector/other current workspace, only read-only colleague workspace matches NVDA
    _current(monkeypatch, None, is_sector=True)
    _finder(
        monkeypatch,
        [{"workspace_id": colleague_ro, "name": "Colleague RO NVDA", "delegatable": False}],
    )
    hint = await cr_mod.resolve_launch_company_routing(
        "full-coverage",
        {"ticker": "NVDA", "workspace_dir": "."},
        {"user_id": U1, "workspace_id": WS_CURRENT},
    )
    assert hint is not None
    assert "只读工作区" in hint
    assert "Colleague RO NVDA" in hint

    # Same setup with a delegatable colleague workspace raises CompanyRoutingRefused
    _current(monkeypatch, None, is_sector=True)
    _finder(
        monkeypatch,
        [{"workspace_id": colleague_del, "name": "Colleague Delegatable NVDA", "delegatable": True}],
    )
    with pytest.raises(cr_mod.CompanyRoutingRefused, match="Colleague Delegatable NVDA"):
        await cr_mod.resolve_launch_company_routing(
            "full-coverage",
            {"ticker": "NVDA", "workspace_dir": "."},
            {"user_id": U1, "workspace_id": WS_CURRENT},
        )


@pytest.mark.asyncio
async def test_flag_off_keeps_legacy_ticker_key_and_historical_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6 Item 6: with coverage_units disabled, _ticker_key does not fold BRK-B/BRK.B
    and historical duplicate workspaces remain included like baseline 49a56df4b."""
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "false")
    assert cr_mod._ticker_key("BRK-B") == "BRK-B"
    assert cr_mod._ticker_key("BRK.B") == "BRK.B"
    assert cr_mod._ticker_key("BRK-B") != cr_mod._ticker_key("BRK.B")

    import src.dalton.autonomy.units_view as units_view_mod

    dup_ws = "d0d0d0d0-d0d0-4d0d-8d0d-d0d0d0d0d001"
    monkeypatch.setattr(
        units_view_mod,
        "load_colleague_workspace_access",
        create_autospec(units_view_mod.load_colleague_workspace_access, return_value={}),
    )

    def rows_for(sql: str, params: Any, _executed: Any) -> list[dict[str, Any]]:
        if "FROM workspaces" in sql:
            return [
                {
                    **_ws(dup_ws, "NVDA Historical Dup"),
                    "config": {"coverage_unit_historical_duplicate": True},
                }
            ]
        if "FROM automations" in sql:
            return []
        if "dalton_knowledge_versions" in sql:
            return [{"workspace_id": dup_ws, "doc_key": "company", "content": {"ticker": "NVDA"}}]
        raise AssertionError(f"unexpected query: {sql}")

    _scripted_conn(monkeypatch, rows_for)
    matches = await find_company_workspaces(U1, "NVDA")
    assert [m["workspace_id"] for m in matches] == [dup_ws]


@pytest.mark.asyncio
async def test_flag_off_extract_ticker_does_not_classify_brk_b_from_workspace_name_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 7 Item 7a: when coverage_units is disabled, _extract_ticker_from_text preserves
    pre-PR behavior and does NOT classify 'BRK-B' from workspace name alone without docs."""
    from src.dalton.coverage import identity as id_mod

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "false")
    assert id_mod._extract_ticker_from_text("BRK-B") is None
    assert id_mod._extract_ticker_from_text("AAPL") == "AAPL"

    async def _no_db(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("flag off must not touch dalton_coverage_units")

    from src.dalton.database import coverage_units as cu_db

    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", _no_db)
    resolved = await id_mod.resolve_workspace_coverage_identity(
        {"workspace_id": WS_CURRENT, "name": "BRK-B", "config": {}},
        company_content=None,
        thesis_content=None,
        automations=[],
    )
    assert resolved.kind == "unclassified"
    assert resolved.is_company is False
    assert resolved.ticker is None


@pytest.mark.asyncio
async def test_historical_duplicate_cannot_launch_company_workflow_and_hints_only_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 8 Item 5: _resolve_workspace_company excludes dalton_historical_duplicate_of when
    coverage_units is enabled; launch refuses on historical duplicate even with matching docs,
    citing only caller-readable canonical workspaces (and hiding unreadable ones)."""
    import src.dalton.autonomy.units_view as units_view_mod
    from src.dalton.database import coverage_units as cu_db
    from src.server.database import workspace as ws_db

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(cr_mod, "is_coverage_units_enabled", lambda: True)

    dup_ws = "d0d0d0d0-d0d0-4d0d-8d0d-d0d0d0d0d099"
    canon_ws = "c0c0c0c0-c0c0-4c0c-8c0c-c0c0c0c0c099"
    hidden_ws = "88888888-8888-4888-8888-888888888899"

    dup_row = {
        **_ws(dup_ws, "NVDA"),
        "config": {"dalton_historical_duplicate_of": canon_ws},
    }
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(_REAL_GET_WORKSPACE, return_value=dup_row),
    )

    state = {"colleague_readable": True, "has_canon": True}

    monkeypatch.setattr(
        units_view_mod,
        "load_colleague_workspace_access",
        create_autospec(
            units_view_mod.load_colleague_workspace_access,
            side_effect=lambda _uid: {canon_ws: (state["colleague_readable"], True)} if state["has_canon"] else {},
        ),
    )

    def rows_for(sql: str, params: Any, _executed: Any) -> list[dict[str, Any]]:
        if "FROM dalton_coverage_units" in sql:
            return []
        if "FROM automations" in sql:
            return []
        if "dalton_knowledge_versions" in sql:
            return [
                {"workspace_id": dup_ws, "doc_key": "company", "content": {"ticker": "NVDA"}},
                {"workspace_id": canon_ws, "doc_key": "company", "content": {"ticker": "NVDA"}},
            ]
        if "FROM workspaces" in sql:
            if not state["has_canon"]:
                return [dup_row]
            if "user_id = %s" in sql:
                return [dup_row]
            # Extra colleague workspaces
            return [{**_ws(canon_ws, "Colleague NVDA Canonical", user_id="colleague"), "config": {}}]
        raise AssertionError(f"unexpected query: {sql}")

    _scripted_conn(monkeypatch, rows_for)
    monkeypatch.setattr(
        cu_db,
        "load_active_units_by_workspace",
        create_autospec(cu_db.load_active_units_by_workspace, return_value={}),
    )

    # 1) _resolve_workspace_company excludes historical duplicate when flag ON
    ident, is_sec = await cr_mod._resolve_workspace_company(dup_ws)
    assert ident is not None and ident.is_company is False and is_sec is False

    # 2) Readable colleague canonical workspace exists -> refuse citing it; check hint cites it too
    with pytest.raises(cr_mod.CompanyRoutingRefused) as exc_readable:
        await cr_mod.resolve_launch_company_routing(
            "full-coverage",
            {"ticker": "NVDA", "workspace_dir": "."},
            {"user_id": U1, "workspace_id": dup_ws},
        )
    assert "Colleague NVDA Canonical" in str(exc_readable.value)

    hint = await cr_mod.routing_hint_for_check(
        {"ticker": "NVDA", "workspace_dir": "."},
        U1,
        dup_ws,
        workflow="full-coverage",
    )
    assert hint is not None and "Colleague NVDA Canonical" in hint

    # 3) Canonical workspace belongs to colleague and is Web-hidden -> STILL refuse to launch
    #    from historical duplicate, and NEVER leak hidden workspace name/UUID!
    state["colleague_readable"] = False
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(_REAL_GET_WORKSPACE, return_value={**_ws(dup_ws, "NVDA"), "config": {"dalton_historical_duplicate_of": hidden_ws}}),
    )
    with pytest.raises(cr_mod.CompanyRoutingRefused) as exc_hidden:
        await cr_mod.resolve_launch_company_routing(
            "full-coverage",
            {"ticker": "NVDA", "workspace_dir": "."},
            {"user_id": U1, "workspace_id": dup_ws},
        )
    assert "历史副本" in str(exc_hidden.value)
    assert hidden_ws not in str(exc_hidden.value)
    assert "Colleague NVDA Canonical" not in str(exc_hidden.value)

    # 4) Even if candidate lookup (find_company_workspaces) raises on an already-identified
    #    historical duplicate, launch STILL refuses and routing_hint_for_check STILL warns (no names leaked).
    monkeypatch.setattr(
        cr_mod,
        "find_company_workspaces",
        create_autospec(cr_mod.find_company_workspaces, side_effect=RuntimeError("registry error")),
    )
    with pytest.raises(cr_mod.CompanyRoutingRefused) as exc_lookup_err:
        await cr_mod.resolve_launch_company_routing(
            "full-coverage",
            {"ticker": "NVDA", "workspace_dir": "."},
            {"user_id": U1, "workspace_id": dup_ws},
        )
    assert "历史副本" in str(exc_lookup_err.value)
    assert "Colleague NVDA Canonical" not in str(exc_lookup_err.value)
    assert hidden_ws not in str(exc_lookup_err.value)

    err_hint = await cr_mod.routing_hint_for_check(
        {"ticker": "NVDA", "workspace_dir": "."},
        U1,
        dup_ws,
        workflow="full-coverage",
    )
    assert err_hint is not None and "历史副本" in err_hint
    assert "Colleague NVDA Canonical" not in err_hint
    assert hidden_ws not in err_hint
