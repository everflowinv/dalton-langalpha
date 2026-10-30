"""collect.py AlphaEngine 覆盖边界与续采状态（codex r2 #1/#2/#3）。

模型：last_run.json 持久化 ``ae_covered_until``（已完整覆盖到的日期，只在窗口
完整扫完时推进，与全局 until 解耦）与 ``ae_resume``（未扫完时的
``{window, cursor, query}``）。每轮窗口起点 = 覆盖边界（route 构造内部再前推
3 天安全回看），终点 = 本轮 until——没扫完的区间永远落在下一轮窗口里。

生产契约（dalton-conventions/references/evidence-and-sources.md:170）：cursor
只对签发它的那组筛选条件有效——续采必须原样重放签发窗口 + 查询词，日期等任
一变化返回 ``e_cursor_filter_mismatch``。本文件的 AE 替身据此把每个 cursor 绑
定到签发它的 (start, end, query)，不匹配按桥接层真实错误形状返回失配。

旧 last_run 无 ae_covered_until 时以旧 until 兜底；首次 `--no-ae` / 首页失败
也要在 commit 时固化该初始边界（不留 null）。
"""

from __future__ import annotations

import importlib.util
import json
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COLLECT_SCRIPT_PATH = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-thesis-delta" / "scripts" / "collect.py"
)
_REAL_URLOPEN = urllib.request.urlopen


def _load_collect_module():
    spec = importlib.util.spec_from_file_location("dalton_thesis_delta_collect", COLLECT_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collect_mod = _load_collect_module()
_REAL_NOW_ET = collect_mod.now_et


class _FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def _ae_page(doc_ids: list[str], cursor: str | None, has_more: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "ok",
        "results": [
            {"doc_id": did, "title": f"研报 {did}", "publish_time": "2026-10-29"} for did in doc_ids
        ],
        "cursor": cursor,
        "has_more": has_more,
        "total_upstream": 12,
    }


def _cursor_error_page(code: str, text: str) -> dict[str, Any]:
    """桥接层游标类错误的真实应答形状：ok=false + error_code + 提示文本。"""
    return {"ok": False, "status": "error", "error": text, "error_code": code}


def _expired_cursor_page() -> dict[str, Any]:
    return _cursor_error_page(
        "e_cursor_expired",
        "E_CURSOR_EXPIRED: 游标已失效或过期；请去掉 cursor 从第一页重新起始检索",
    )


def _filter_mismatch_page() -> dict[str, Any]:
    return _cursor_error_page(
        "e_cursor_filter_mismatch",
        "E_CURSOR_FILTER_MISMATCH: cursor 翻页时必须保持与上一页完全相同的 query 与筛选条件",
    )


class _CursorBoundAEBridge:
    """遵守生产契约的 AE 替身：每个 cursor 绑定到签发它的 (start, end, query)。

    请求携带的 cursor 与绑定不符（或已被 `mismatch_cursors` 点名）→
    e_cursor_filter_mismatch；`expire_cursors` 点名 → e_cursor_expired。成功页
    按脚本顺序发放（脚本耗尽后重复最后一页），页里签发的新 cursor 绑定到当前
    请求的 (start, end, query)。每次请求的 arguments 记进 ``requests``。"""

    def __init__(
        self,
        pages: list[dict[str, Any]],
        issued: dict[str, tuple[str, str, str]] | None = None,
        expire_cursors: set[str] | frozenset[str] = frozenset(),
        mismatch_cursors: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        self.pages = pages
        self.idx = 0
        self.issued: dict[str, tuple[str, str, str]] = dict(issued or {})
        self.expire_cursors = set(expire_cursors)
        self.mismatch_cursors = set(mismatch_cursors)
        self.requests: list[dict[str, Any]] = []

    def handle(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(json.loads(json.dumps(arguments)))
        key = (
            str(arguments.get("start") or ""),
            str(arguments.get("end") or ""),
            str(arguments.get("query") or ""),
        )
        cursor = (arguments.get("params") or {}).get("cursor")
        if cursor is not None:
            cursor = str(cursor)
            if cursor in self.expire_cursors:
                return _expired_cursor_page()
            if cursor in self.mismatch_cursors:
                return _filter_mismatch_page()
            if self.issued.get(cursor) != key:
                return _filter_mismatch_page()
        page = self.pages[min(self.idx, len(self.pages) - 1)]
        self.idx += 1
        if page.get("cursor"):
            self.issued[str(page["cursor"])] = key
        return page

    @property
    def ae_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r.get("route") == "alphaengine"]


def _install_bridge(monkeypatch: pytest.MonkeyPatch, bridge: _CursorBoundAEBridge) -> None:
    """alphaengine 路由交给绑定 cursor 的替身；其余 route 答成功空结果。"""

    def _urlopen(req: urllib.request.Request, timeout: int | None = None) -> _FakeHTTPResponse:
        body = json.loads(req.data.decode("utf-8"))
        arguments = (body.get("params") or {}).get("arguments") or {}
        if str(arguments.get("route") or "") == "alphaengine":
            payload = bridge.handle(arguments)
        else:
            payload = {"ok": True, "status": "ok", "results": []}
        wrapped = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "structuredContent": dict(payload),
                "isError": not bool(payload.get("ok", True)),
            },
        }
        return _FakeHTTPResponse(json.dumps(wrapped).encode("utf-8"))

    fake_urlopen = create_autospec(_REAL_URLOPEN, side_effect=_urlopen)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("DALTON_RESEARCHDATA_URL", "http://127.0.0.1:8961/rpc")


_META = {"ticker": "DASH", "name": "DoorDash", "aliases": ["DASH"], "sec_forms": ["8-K"]}


# --------------------------------------- 续采原样重放签发窗口（codex r2 #1）


def test_continuation_replays_issuing_window_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """续采必须原样使用签发 cursor 的完整原窗口：boundary C=10-29，cursor c5
    签发于 [10-26, 10-30]（窗口 10-29~10-30），本轮 until=10-31——第一个请求
    终点必须是 10-30 而不是本轮 10-31；原窗口扫完（覆盖边界推进到 10-30）、
    预算有余，再扫新窗口 [10-30, 10-31]（起点 10-27）。撤回本修复（旧 cursor
    配新截止日）即被替身按契约判 e_cursor_filter_mismatch，断言失败。"""
    bridge = _CursorBoundAEBridge(
        pages=[
            _ae_page(["ae-old-last"], cursor=None, has_more=False),  # 原窗口最后一页
            _ae_page(["ae-new"], cursor=None, has_more=False),  # 新窗口唯一一页
        ],
        issued={"c5": ("2026-10-26", "2026-10-30", "DoorDash")},
    )
    _install_bridge(monkeypatch, bridge)

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-10-31",
        until="2026-10-31",
        seen=set(),
        ae_covered_until="2026-10-29",
        ae_resume={
            "window": {"since": "2026-10-29", "until": "2026-10-30"},
            "cursor": "c5",
            "query": "DoorDash",
        },
    )

    reqs = bridge.ae_requests
    assert len(reqs) == 2
    assert reqs[0]["params"]["cursor"] == "c5"
    assert reqs[0]["start"] == "2026-10-26"  # 原窗口 since(10-29) 前推 3 天
    assert reqs[0]["end"] == "2026-10-30"  # 原窗口终点，不是本轮 until
    assert "cursor" not in (reqs[1].get("params") or {})
    assert reqs[1]["start"] == "2026-10-27"  # 新窗口起点 = 推进后的边界 10-30
    assert reqs[1]["end"] == "2026-10-31"

    st = internal["status"]["alphaengine"]
    assert st["completed"] is True
    assert st["ae_state"] == {"ran": True, "covered_until": "2026-10-31", "resume": None}
    assert {"alphaengine:ae-old-last", "alphaengine:ae-new"} == set(ae["new"])


def test_filter_mismatch_cursor_drops_and_rescans_original_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """e_cursor_filter_mismatch 与 e_cursor_expired 同样处理：丢弃 cursor、从
    原窗口起点重扫（同轮、预算共用）、seen 去重；原窗口扫完后继续扫新窗口。"""
    bridge = _CursorBoundAEBridge(
        pages=[
            _ae_page(["ae-r1", "ae-r2"], cursor=None, has_more=False),  # 原窗口重扫
            _ae_page(["ae-new"], cursor=None, has_more=False),  # 新窗口
        ],
        issued={"c5": ("2026-10-26", "2026-10-30", "DoorDash")},
        mismatch_cursors={"c5"},
    )
    _install_bridge(monkeypatch, bridge)

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-10-31",
        until="2026-10-31",
        seen=set(),
        ae_covered_until="2026-10-29",
        ae_resume={
            "window": {"since": "2026-10-29", "until": "2026-10-30"},
            "cursor": "c5",
            "query": "DoorDash",
        },
    )

    reqs = bridge.ae_requests
    assert len(reqs) == 3
    assert reqs[0]["params"]["cursor"] == "c5"
    assert reqs[0]["end"] == "2026-10-30"
    # 重扫从原窗口起点开始（不换窗口、不带 cursor）
    assert "cursor" not in (reqs[1].get("params") or {})
    assert reqs[1]["start"] == "2026-10-26" and reqs[1]["end"] == "2026-10-30"
    # 原窗口扫完、预算有余 → 新窗口
    assert reqs[2]["start"] == "2026-10-27" and reqs[2]["end"] == "2026-10-31"

    st = internal["status"]["alphaengine"]
    assert st["completed"] is True
    assert st["cursor_invalid_restart"] is True
    assert st["ae_state"] == {"ran": True, "covered_until": "2026-10-31", "resume": None}
    assert {"alphaengine:ae-r1", "alphaengine:ae-r2", "alphaengine:ae-new"} == set(ae["new"])


def test_expired_cursor_drops_and_rescans_original_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """e_cursor_expired：同轮弃 cursor 从原窗口起点重扫；原窗口扫完（边界推进
    到原窗口 until）后用剩余预算扫新窗口。"""
    bridge = _CursorBoundAEBridge(
        pages=[
            _ae_page(["ae-r1"], cursor=None, has_more=False),
            _ae_page(["ae-new"], cursor=None, has_more=False),
        ],
        issued={"c5": ("2026-10-25", "2026-10-29", "DoorDash")},
        expire_cursors={"c5"},
    )
    _install_bridge(monkeypatch, bridge)

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-10-30",
        until="2026-10-30",
        seen=set(),
        ae_covered_until="2026-10-28",
        ae_resume={
            "window": {"since": "2026-10-28", "until": "2026-10-29"},
            "cursor": "c5",
            "query": "DoorDash",
        },
    )

    reqs = bridge.ae_requests
    assert len(reqs) == 3
    assert reqs[0]["params"]["cursor"] == "c5"
    assert reqs[0]["start"] == "2026-10-25" and reqs[0]["end"] == "2026-10-29"
    assert "cursor" not in (reqs[1].get("params") or {})
    assert reqs[1]["start"] == "2026-10-25" and reqs[1]["end"] == "2026-10-29"
    assert reqs[2]["start"] == "2026-10-26" and reqs[2]["end"] == "2026-10-30"

    st = internal["status"]["alphaengine"]
    assert st["completed"] is True
    assert st["cursor_invalid_restart"] is True
    assert st["ae_state"] == {"ran": True, "covered_until": "2026-10-30", "resume": None}
    assert {"alphaengine:ae-r1", "alphaengine:ae-new"} == set(ae["new"])


def test_failed_continuation_retry_keeps_original_window_and_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带积压的一轮从 cursor 续采但首页即失败：resume 原样保留签发窗口与
    cursor（窗口终点不延伸），下一轮照常重试——积压不会因故障轮丢失。"""
    bridge = _CursorBoundAEBridge(
        pages=[{"ok": False, "status": "error", "error": "connection refused"}],
        issued={"c5": ("2026-10-25", "2026-10-29", "DoorDash")},
    )
    _install_bridge(monkeypatch, bridge)

    internal, _ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-10-30",
        until="2026-10-30",
        seen=set(),
        ae_covered_until="2026-10-28",
        ae_resume={
            "window": {"since": "2026-10-28", "until": "2026-10-29"},
            "cursor": "c5",
            "query": "DoorDash",
        },
    )

    reqs = bridge.ae_requests
    assert len(reqs) == 1 and reqs[0]["params"]["cursor"] == "c5"
    assert reqs[0]["end"] == "2026-10-29"  # 原窗口原样重放
    st = internal["status"]["alphaengine"]
    assert st["ae_state"]["covered_until"] == "2026-10-28"
    assert st["ae_state"]["resume"] == {
        "window": {"since": "2026-10-28", "until": "2026-10-29"},
        "cursor": "c5",
        "query": "DoorDash",
    }


def test_query_change_discards_cursor_and_rescans_from_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查询词变了：游标属于旧查询，作废；从覆盖边界起算的窗口起点重扫。"""
    bridge = _CursorBoundAEBridge(pages=[_ae_page(["ae-x"], cursor=None, has_more=False)])
    _install_bridge(monkeypatch, bridge)

    internal, _ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-10-30",
        until="2026-10-30",
        seen=set(),
        ae_covered_until="2026-10-28",
        ae_resume={
            "window": {"since": "2026-10-28", "until": "2026-10-29"},
            "cursor": "c5",
            "query": "旧公司名",
        },
    )

    reqs = bridge.ae_requests
    assert len(reqs) == 1
    assert "cursor" not in (reqs[0].get("params") or {})
    assert reqs[0]["start"] == "2026-10-25"
    st = internal["status"]["alphaengine"]
    assert "resumed_from_cursor" not in st
    assert st["ae_state"]["covered_until"] == "2026-10-30"


# ------------------------------- 预算末次失效保持未完成（codex r2 #2）


def test_cursor_expiry_on_last_budget_call_stays_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前 4 次成功、第 5 次（预算最后一次）cursor 失效：不得当作扫完——保持
    incomplete、覆盖边界不动、resume 记为无 cursor（下轮从原窗口起点重扫）、
    已取的前 4 页照常入包。撤掉本修复（失效即清空并置 completed）即失败。"""
    bridge = _CursorBoundAEBridge(
        pages=[_ae_page([f"ae-{i}"], cursor=f"c{i + 1}", has_more=True) for i in range(4)],
        expire_cursors={"c4"},
    )
    _install_bridge(monkeypatch, bridge)

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-10-29",
        until="2026-10-30",
        seen=set(),
        ae_covered_until="2026-10-29",
    )

    st = internal["status"]["alphaengine"]
    assert st["pages"] == 5
    assert st["completed"] is False
    assert st["incomplete"] is True
    assert st["ae_state"]["covered_until"] == "2026-10-29"  # 边界不推进
    assert st["ae_state"]["resume"] == {
        "window": {"since": "2026-10-29", "until": "2026-10-30"},
        "cursor": None,  # 无 cursor：下轮从原窗口起点重扫
        "query": "DoorDash",
    }
    # 已取的前 4 页照常入包
    assert {f"alphaengine:ae-{i}" for i in range(4)} == set(ae["new"])

    # 下一轮：resume 无 cursor → 原窗口起点重扫（不带 cursor）。
    bridge2 = _CursorBoundAEBridge(pages=[_ae_page(["ae-fin"], cursor=None, has_more=False)])
    _install_bridge(monkeypatch, bridge2)
    internal2, _ = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-10-31",
        until="2026-10-31",
        seen=set(),
        ae_covered_until="2026-10-29",
        ae_resume=st["ae_state"]["resume"],
    )
    reqs2 = bridge2.ae_requests
    assert reqs2[0]["start"] == "2026-10-26" and reqs2[0]["end"] == "2026-10-30"
    assert "cursor" not in (reqs2[0].get("params") or {})
    assert internal2["status"]["alphaengine"]["ae_state"]["covered_until"] == "2026-10-31"


# ------------------------------------------------- 达上限 / 首页失败


def test_cap_keeps_covered_boundary_and_records_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第 5 页后仍有更多：来源标 incomplete，覆盖边界停在原值（不推进），
    resume 带本轮窗口与第 5 页 cursor。"""
    bridge = _CursorBoundAEBridge(
        pages=[_ae_page([f"ae-{i}"], cursor=f"c{i + 1}", has_more=True) for i in range(8)]
    )
    _install_bridge(monkeypatch, bridge)

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-10-28",
        until="2026-10-30",
        seen=set(),
        ae_covered_until="2026-10-29",
    )

    st = internal["status"]["alphaengine"]
    assert st["incomplete"] is True
    assert st["completed"] is False
    assert st["cursor"] == "c5"
    assert st["pages"] == 5
    assert st["ae_state"] == {
        "ran": True,
        "covered_until": "2026-10-29",
        "resume": {
            "window": {"since": "2026-10-29", "until": "2026-10-30"},
            "cursor": "c5",
            "query": "DoorDash",
        },
    }
    reqs = bridge.ae_requests
    assert len(reqs) == 5
    assert all(r["start"] == "2026-10-26" and r["end"] == "2026-10-30" for r in reqs)
    assert {f"alphaengine:ae-{i}" for i in range(5)} == set(ae["new"])


def test_first_page_failure_keeps_boundary_and_records_cursorless_rescan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首页即失败（AE 不可用类故障）：覆盖边界不动，resume 记无 cursor 的原
    窗口重扫；commit 据此把边界固化（codex r2 #3 的失败路径）。"""
    bridge = _CursorBoundAEBridge(
        pages=[{"ok": False, "status": "error", "error": "connection refused"}]
    )
    _install_bridge(monkeypatch, bridge)

    internal, _ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-10-30",
        until="2026-10-30",
        seen=set(),
        ae_covered_until="2026-10-28",
    )

    st = internal["status"]["alphaengine"]
    assert st["ok"] is False
    assert st["ae_state"]["covered_until"] == "2026-10-28"
    assert st["ae_state"]["resume"] == {
        "window": {"since": "2026-10-28", "until": "2026-10-30"},
        "cursor": None,
        "query": "DoorDash",
    }


# ------------------------------------------------- cmd 级：跨日与状态固化


def _freeze_now(monkeypatch: pytest.MonkeyPatch, *args: int) -> None:
    # spec 必须用模块加载时的原函数：连续多轮冻结时 collect_mod.now_et 已是替身。
    monkeypatch.setattr(
        collect_mod,
        "now_et",
        create_autospec(_REAL_NOW_ET, return_value=datetime(*args, tzinfo=collect_mod.ET)),
    )


def _patch_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_snapshot = {
        "ticker": "DASH",
        "fetched_at": "2026-10-30T23:00:00-04:00",
        "earnings_estimate": {"+1y": {"avg": 4.49, "period_end": "2027-12-31"}},
    }
    monkeypatch.setattr(
        collect_mod,
        "fetch_consensus_snapshot",
        create_autospec(collect_mod.fetch_consensus_snapshot, return_value=fake_snapshot),
    )


class _Args:
    ticker = "DASH"
    workspace_dir = ""
    since = None
    run_id = ""
    no_ae = False
    no_web = True
    json = True
    material = "yes"
    summary = "测试提交"


def _run_collect_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_id: str, *, no_ae: bool = False
) -> dict[str, Any]:
    _Args.workspace_dir = str(tmp_path)
    _Args.run_id = run_id
    _Args.no_ae = no_ae
    assert collect_mod.cmd_collect(_Args()) == 0
    assert collect_mod.cmd_commit(_Args()) == 0
    tracker = tmp_path / "research" / "tracker"
    return json.loads((tracker / "last_run.json").read_text(encoding="utf-8"))


def test_multi_day_backlog_never_skips_intermediate_days(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """codex r2 #1 场景：跨日积压。第 1 天（10-30）翻满 5 页存 c5；第 2 天
    （10-31）从 c5 **原样重放 [10-26, 10-30]**（不是配新截止日）把旧窗口扫完、
    预算恰好用尽，新窗口 [10-30, 10-31] 留待下轮；第 3 天（11-01）扫
    [10-27, 11-01] 完成。每个中间日期都在某个被完整扫描的窗口里。"""
    _patch_snapshot(monkeypatch)
    tracker = tmp_path / "research" / "tracker"
    tracker.mkdir(parents=True)
    (tracker / "last_run.json").write_text(
        json.dumps({"ticker": "DASH", "until": "2026-10-29", "committed_at": "x"}),
        encoding="utf-8",
    )

    # 第 1 天：窗口 [10-29, 10-30]（请求 [10-26, 10-30]），5 页达上限。
    bridge1 = _CursorBoundAEBridge(
        pages=[_ae_page([f"ae-{i}"], cursor=f"c{i + 1}", has_more=True) for i in range(8)]
    )
    _install_bridge(monkeypatch, bridge1)
    _freeze_now(monkeypatch, 2026, 10, 30, 23, 0)
    last_run = _run_collect_commit(monkeypatch, tmp_path, "r1")
    assert last_run["ae_covered_until"] == "2026-10-29"
    assert last_run["ae_resume"] == {
        "window": {"since": "2026-10-29", "until": "2026-10-30"},
        "cursor": "c5",
        "query": "DoorDash",
    }
    assert all(r["start"] == "2026-10-26" and r["end"] == "2026-10-30" for r in bridge1.ae_requests)

    # 第 2 天：续采原样重放旧窗口（终点仍是 10-30），第 5 页扫完旧窗口——
    # 预算用尽，新窗口留到下轮；覆盖边界推进到旧窗口的 until（10-30）。
    # c5 是第 1 天的替身签发的，这里预登记同一绑定。
    bridge2 = _CursorBoundAEBridge(
        pages=[
            _ae_page(["ae-5"], cursor="c6", has_more=True),
            _ae_page(["ae-6"], cursor="c7", has_more=True),
            _ae_page(["ae-7"], cursor="c8", has_more=True),
            _ae_page(["ae-8"], cursor="c9", has_more=True),
            _ae_page(["ae-9"], cursor=None, has_more=False),
        ],
        issued={"c5": ("2026-10-26", "2026-10-30", "DoorDash")},
    )
    _install_bridge(monkeypatch, bridge2)
    _freeze_now(monkeypatch, 2026, 10, 31, 23, 0)
    last_run = _run_collect_commit(monkeypatch, tmp_path, "r2")
    r2_reqs = bridge2.ae_requests
    assert len(r2_reqs) == 5
    assert r2_reqs[0]["params"]["cursor"] == "c5"
    # 关键断言：续采终点是签发窗口的 10-30，不是本轮的 10-31。
    assert all(r["start"] == "2026-10-26" and r["end"] == "2026-10-30" for r in r2_reqs)
    assert last_run["ae_covered_until"] == "2026-10-30"
    assert last_run["ae_resume"] is None  # 新窗口无 cursor，下轮从边界起算

    # 第 3 天：窗口 [10-30, 11-01]（请求 [10-27, 11-01]），一页扫完。
    bridge3 = _CursorBoundAEBridge(pages=[_ae_page(["ae-last"], cursor=None, has_more=False)])
    _install_bridge(monkeypatch, bridge3)
    _freeze_now(monkeypatch, 2026, 11, 1, 23, 0)
    last_run = _run_collect_commit(monkeypatch, tmp_path, "r3")
    r3_reqs = bridge3.ae_requests
    assert r3_reqs[0]["start"] == "2026-10-27" and r3_reqs[0]["end"] == "2026-11-01"
    assert "cursor" not in (r3_reqs[0].get("params") or {})
    assert last_run["ae_covered_until"] == "2026-11-01"
    assert last_run["ae_resume"] is None


def test_old_state_no_ae_commits_derived_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """codex r2 #3：旧格式 last_run（只有 until=10/20）→ 10/30 执行 --no-ae 并
    commit——必须把由旧 until 推出的初始覆盖边界固化（10/20），不得写 null；
    10/31 AE 恢复后查询起点为 10/17（10/20 前推 3 天）。"""
    _patch_snapshot(monkeypatch)
    tracker = tmp_path / "research" / "tracker"
    tracker.mkdir(parents=True)
    (tracker / "last_run.json").write_text(
        json.dumps({"ticker": "DASH", "until": "2026-10-20", "committed_at": "x"}),
        encoding="utf-8",
    )

    bridge = _CursorBoundAEBridge(pages=[])
    _install_bridge(monkeypatch, bridge)
    _freeze_now(monkeypatch, 2026, 10, 30, 23, 0)
    last_run = _run_collect_commit(monkeypatch, tmp_path, "noae", no_ae=True)

    assert last_run["until"] == "2026-10-30"  # 全局 until 照常推进
    assert last_run["ae_covered_until"] == "2026-10-20"  # 覆盖边界固化为旧 until
    assert last_run["ae_resume"] is None
    assert not bridge.ae_requests
    packet = json.loads((tracker / "noae" / "packet.json").read_text(encoding="utf-8"))
    assert packet["ae_state"] == {"ran": False, "covered_until": "2026-10-20", "resume": None}

    # 10/31 AE 恢复：窗口起点 = 固化边界 10/20 前推 3 天 = 10/17。
    bridge2 = _CursorBoundAEBridge(pages=[_ae_page(["ae-back"], cursor=None, has_more=False)])
    _install_bridge(monkeypatch, bridge2)
    _freeze_now(monkeypatch, 2026, 10, 31, 23, 0)
    last_run2 = _run_collect_commit(monkeypatch, tmp_path, "back")
    assert bridge2.ae_requests[0]["start"] == "2026-10-17"
    assert bridge2.ae_requests[0]["end"] == "2026-10-31"
    assert last_run2["ae_covered_until"] == "2026-10-31"


def test_no_ae_commit_preserves_backlog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """新格式状态下带积压时执行一轮 --no-ae 并 commit：覆盖边界与续采积压
    原样保留，不清空。"""
    _patch_snapshot(monkeypatch)
    tracker = tmp_path / "research" / "tracker"
    tracker.mkdir(parents=True)
    backlog = {
        "window": {"since": "2026-10-28", "until": "2026-10-29"},
        "cursor": "c5",
        "query": "DoorDash",
    }
    (tracker / "last_run.json").write_text(
        json.dumps(
            {
                "ticker": "DASH",
                "until": "2026-10-29",
                "ae_covered_until": "2026-10-28",
                "ae_resume": backlog,
            }
        ),
        encoding="utf-8",
    )
    bridge = _CursorBoundAEBridge(pages=[])
    _install_bridge(monkeypatch, bridge)
    _freeze_now(monkeypatch, 2026, 10, 30, 23, 0)

    last_run = _run_collect_commit(monkeypatch, tmp_path, "noae", no_ae=True)

    assert not bridge.ae_requests
    assert last_run["ae_covered_until"] == "2026-10-28"
    assert last_run["ae_resume"] == backlog
    packet = json.loads((tracker / "noae" / "packet.json").read_text(encoding="utf-8"))
    assert packet["ae_state"] == {
        "ran": False,
        "covered_until": "2026-10-28",
        "resume": backlog,
    }


def test_old_packet_without_ae_state_commits_null_fields(tmp_path: Path) -> None:
    """旧 packet（无 ae_state 键）commit 不报错；无上一份状态时 ae 字段为 None，
    下一轮 collect 以旧 until / since 兜底。"""
    ws = tmp_path
    tracker = ws / "research" / "tracker"
    run_dir = tracker / "r2"
    run_dir.mkdir(parents=True)
    (run_dir / "packet.json").write_text(
        json.dumps({"ticker": "DASH", "run_id": "r2", "since": "a", "until": "b"}),
        encoding="utf-8",
    )

    class _ArgsOld:
        workspace_dir = str(ws)
        run_id = "r2"
        ticker = "DASH"
        material = "no"
        summary = "旧包"

    assert collect_mod.cmd_commit(_ArgsOld()) == 0
    last_run = json.loads((tracker / "last_run.json").read_text(encoding="utf-8"))
    assert last_run["ae_covered_until"] is None
    assert last_run["ae_resume"] is None
    covered, resume = collect_mod._ae_state_of(last_run)
    assert covered is None and resume is None


# --------------------------------------------------------------- packet 披露


def test_packet_md_discloses_unfinished_source() -> None:
    """packet.md 明确写出"本轮未扫完、覆盖边界停在、下轮续扫"，代理据此在报告
    里披露；无 cursor 的 resume（游标失效/首页失败）同样披露并说明下轮从窗口
    起点重扫；扫完时写"续采完成"。"""
    base_packet = {
        "ticker": "DASH",
        "run_id": "20261030-2300",
        "since": "2026-10-28",
        "until": "2026-10-30",
        "consensus": {},
        "deltas": {},
        "auto_flags": [],
        "internal": {
            "status": {
                "alphaengine": {
                    "ok": True,
                    "incomplete": True,
                    "cursor": "c5",
                    "ae_state": {
                        "ran": True,
                        "covered_until": "2026-10-29",
                        "resume": {
                            "window": {"since": "2026-10-29", "until": "2026-10-30"},
                            "cursor": "c5",
                            "query": "DoorDash",
                        },
                    },
                }
            },
            "new": {},
        },
        "alphaengine": {"status": [], "new": {}},
        "sec": {"status": [], "new": {}},
    }
    md = collect_mod.render_packet_md(base_packet)
    assert "alphaengine 本轮未扫完" in md
    assert "覆盖边界停在 2026-10-29" in md
    assert "`c5`" in md
    assert "下轮窗口仍从该边界起算" in md
    assert "不得当作" in md

    no_cursor_packet = json.loads(json.dumps(base_packet))
    no_cursor_packet["internal"]["status"]["alphaengine"].update(
        {"cursor": None,
         "ae_state": {
             "ran": True,
             "covered_until": "2026-10-29",
             "resume": {
                 "window": {"since": "2026-10-29", "until": "2026-10-30"},
                 "cursor": None,
                 "query": "DoorDash",
             },
         }}
    )
    md_nc = collect_mod.render_packet_md(no_cursor_packet)
    assert "无有效 cursor（本轮游标失效或首页即失败）" in md_nc
    assert "从窗口起点重扫" in md_nc

    done_packet = json.loads(json.dumps(base_packet))
    done_packet["internal"]["status"]["alphaengine"] = {
        "ok": True,
        "incomplete": False,
        "resumed_from_cursor": "c5",
        "ae_state": {"ran": True, "covered_until": "2026-10-30", "resume": None},
    }
    md_done = collect_mod.render_packet_md(done_packet)
    assert "续采完成" in md_done and "c5" in md_done
    assert "本轮未扫完" not in md_done
