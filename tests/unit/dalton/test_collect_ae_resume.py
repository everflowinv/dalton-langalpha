"""collect.py AlphaEngine 包含式覆盖边界与两步按日切分续采测试。

替身规范：所有被测外部接口（urllib.request.urlopen、http.client.HTTPResponse、
now_et、fetch_consensus_snapshot）一律使用 ``create_autospec``，分页与协议校验
逻辑直接放进 ``side_effect`` 闭包中，不使用无规格自定义替身类。
"""

from __future__ import annotations

import http.client
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


def _make_http_response(payload: dict[str, Any]) -> Any:
    """用 create_autospec 构造 JSON-RPC HTTPResponse 上下文替身。"""
    wrapped = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "structuredContent": dict(payload),
            "isError": not bool(payload.get("ok", True)),
        },
    }
    resp = create_autospec(http.client.HTTPResponse, instance=True)
    resp.read.return_value = json.dumps(wrapped).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _ae_page(
    doc_ids: list[str],
    cursor: str | None,
    has_more: bool,
    *,
    total_upstream: int | None = None,
    publish_time: str = "2026-11-15",
    stale: bool = False,
    cursor_unavailable: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "status": "ok",
        "results": [
            {"doc_id": did, "title": f"研报 {did}", "publish_time": publish_time} for did in doc_ids
        ],
        "cursor": cursor,
        "has_more": has_more,
    }
    if total_upstream is not None:
        payload["total_upstream"] = total_upstream
    if stale:
        payload["stale"] = True
    if cursor_unavailable is not None:
        payload["cursor_unavailable"] = cursor_unavailable
    return payload


def _expired_cursor_page() -> dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "error_code": "e_cursor_expired",
        "error": "E_CURSOR_EXPIRED: 游标已失效或过期；请去掉 cursor 从第一页重新起始检索",
    }


def _filter_mismatch_page() -> dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "error_code": "e_cursor_filter_mismatch",
        "error": "E_CURSOR_FILTER_MISMATCH: cursor 翻页时必须保持与上一页完全相同的 query 与筛选条件",
    }


def _install_scripted_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    pages: list[dict[str, Any]],
    *,
    expire_cursors: set[str] | frozenset[str] = frozenset(),
    mismatch_cursors: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """在 urllib.request.urlopen 上安装 create_autospec 替身，按脚本顺序返回 AE 页。"""
    ae_requests: list[dict[str, Any]] = []
    issued: dict[str, tuple[str, str, str]] = {}
    state = {"idx": 0}
    expire_set = set(expire_cursors)
    mismatch_set = set(mismatch_cursors)

    def _side_effect(req: urllib.request.Request, timeout: int | None = None) -> Any:
        body = json.loads(req.data.decode("utf-8"))
        arguments = (body.get("params") or {}).get("arguments") or {}
        if str(arguments.get("route") or "") != "alphaengine":
            return _make_http_response({"ok": True, "status": "ok", "results": []})

        ae_requests.append(json.loads(json.dumps(arguments)))
        key = (
            str(arguments.get("start") or ""),
            str(arguments.get("end") or ""),
            str(arguments.get("query") or ""),
        )
        cursor = (arguments.get("params") or {}).get("cursor")
        if cursor is not None:
            cursor = str(cursor)
            if cursor in expire_set:
                return _make_http_response(_expired_cursor_page())
            if cursor in mismatch_set or issued.get(cursor) != key:
                return _make_http_response(_filter_mismatch_page())
        page = pages[min(state["idx"], len(pages) - 1)]
        state["idx"] += 1
        if page.get("cursor"):
            issued[str(page["cursor"])] = key
        return _make_http_response(page)

    monkeypatch.setattr(urllib.request, "urlopen", create_autospec(_REAL_URLOPEN, side_effect=_side_effect))
    monkeypatch.setenv("DALTON_RESEARCHDATA_URL", "http://127.0.0.1:8961/rpc")
    return ae_requests


def _install_date_range_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    docs_by_day: dict[str, list[str]],
    *,
    page_size: int = 1,
    total_upstream_mode: str = "large",
    fail_end_days: set[str] | frozenset[str] = frozenset(),
    stale_end_days: set[str] | frozenset[str] = frozenset(),
    cursor_unavail_end_days: set[str] | frozenset[str] = frozenset(),
    first_page_override: dict[tuple[str, str], list[tuple[str, str]]] | None = None,
) -> list[dict[str, Any]]:
    """在 urllib.request.urlopen 上安装按日期范围分页的 create_autospec 替身。

    ``total_upstream_mode``:
    - ``"large"``: 模拟上游真实多页总量（``total_pages * AE_PAGE_LIMIT``）
    - ``"missing"``: 不返回 ``total_upstream``（缺失）
    - ``"low"``: 用首页返回条数顶替 ``total_upstream``（模拟 MCP 上游缺 total 时的偏低回退）
    - ``"exact"``: 返回 ``len(matched)`` 实际条数
    """
    ae_requests: list[dict[str, Any]] = []
    cursors: dict[str, tuple[tuple[str, str, str], int]] = {}
    seq = {"n": 0}
    fail_days = set(fail_end_days)
    stale_days = set(stale_end_days)
    cur_unavail_days = set(cursor_unavail_end_days)
    overrides = dict(first_page_override or {})

    def _side_effect(req: urllib.request.Request, timeout: int | None = None) -> Any:
        body = json.loads(req.data.decode("utf-8"))
        arguments = (body.get("params") or {}).get("arguments") or {}
        if str(arguments.get("route") or "") != "alphaengine":
            return _make_http_response({"ok": True, "status": "ok", "results": []})

        ae_requests.append(json.loads(json.dumps(arguments)))
        start = str(arguments.get("start") or "")
        end = str(arguments.get("end") or "")
        query = str(arguments.get("query") or "")
        key = (start, end, query)

        if end in fail_days:
            return _make_http_response({"ok": False, "status": "error", "error": f"upstream timeout on {end}"})

        cursor = (arguments.get("params") or {}).get("cursor")
        if cursor is not None:
            meta = cursors.get(str(cursor))
            if meta is None:
                return _make_http_response(_expired_cursor_page())
            bound_key, offset = meta
            if bound_key != key:
                return _make_http_response(_filter_mismatch_page())
        else:
            offset = 0

        matched: list[tuple[str, str]] = []
        for day in sorted(docs_by_day):
            if start <= day <= end:
                for did in docs_by_day[day]:
                    matched.append((day, did))

        if offset == 0 and (start, end) in overrides:
            chunk = list(overrides[(start, end)])
            next_offset = page_size
        else:
            chunk = matched[offset : offset + page_size]
            next_offset = offset + len(chunk)

        has_more = next_offset < len(matched)
        next_cursor: str | None = None
        if has_more:
            seq["n"] += 1
            next_cursor = f"cur-{seq['n']}"
            cursors[next_cursor] = (key, next_offset)

        payload: dict[str, Any] = {
            "ok": True,
            "status": "ok" if chunk or has_more else "empty",
            "results": [
                {"doc_id": did, "title": f"研报 {did}", "publish_time": day} for day, did in chunk
            ],
            "cursor": next_cursor,
            "has_more": has_more,
        }
        if total_upstream_mode == "large":
            total_pages = (len(matched) + page_size - 1) // page_size if matched else 0
            payload["total_upstream"] = total_pages * collect_mod.AE_PAGE_LIMIT
        elif total_upstream_mode == "low":
            payload["total_upstream"] = len(chunk)
        elif total_upstream_mode == "exact":
            payload["total_upstream"] = len(matched)
        elif total_upstream_mode == "missing":
            pass
        else:
            raise ValueError(f"Unknown total_upstream_mode: {total_upstream_mode}")

        if end in stale_days:
            payload["stale"] = True
        if end in cur_unavail_days:
            payload["cursor"] = None
            payload["cursor_unavailable"] = "stale"
        return _make_http_response(payload)

    monkeypatch.setattr(urllib.request, "urlopen", create_autospec(_REAL_URLOPEN, side_effect=_side_effect))
    monkeypatch.setenv("DALTON_RESEARCHDATA_URL", "http://127.0.0.1:8961/rpc")
    return ae_requests


_META = {"ticker": "DASH", "name": "DoorDash", "aliases": ["DASH"], "sec_forms": ["8-K"]}


def _freeze_now(monkeypatch: pytest.MonkeyPatch, *args: int) -> None:
    monkeypatch.setattr(
        collect_mod,
        "now_et",
        create_autospec(_REAL_NOW_ET, return_value=datetime(*args, tzinfo=collect_mod.ET)),
    )


def _patch_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_snapshot = {
        "ticker": "DASH",
        "fetched_at": "2026-11-15T23:00:00-05:00",
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


# ------------------------------------------- 1. 整段 4 页不切分 & total_upstream 缺失/偏低


def test_full_window_4_pages_completes_without_splitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整段 4 页（不超 MAX_CURSOR_PAGES - 1）：一轮整段扫完不切分，同轮正常用 cursor 翻页。"""
    pages = [
        _ae_page([f"ae-{i}"], cursor=(f"c{i}" if i < 4 else None), has_more=(i < 4))
        for i in range(1, 5)
    ]
    reqs = _install_scripted_urlopen(monkeypatch, pages)

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-17",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    assert len(reqs) == 4
    assert all(r["start"] == "2026-11-09" and r["end"] == "2026-11-17" for r in reqs)
    assert [r.get("params", {}).get("cursor") for r in reqs] == [None, "c1", "c2", "c3"]

    st = internal["status"]["alphaengine"]
    assert st["ok"] is True
    assert st["completed"] is True
    assert st["incomplete"] is False
    assert st["pages"] == 4
    assert st["returned"] == 4
    assert st["ae_state"] == {"ran": True, "covered_until": "2026-11-17", "resume": None}
    assert set(ae["new"]) == {"alphaengine:ae-1", "alphaengine:ae-2", "alphaengine:ae-3", "alphaengine:ae-4"}


@pytest.mark.parametrize("total_upstream_mode", ["missing", "low"])
def test_missing_or_low_total_upstream_still_advances_every_round(
    monkeypatch: pytest.MonkeyPatch,
    total_upstream_mode: str,
) -> None:
    """total_upstream 缺失或偏低（等于首页条数）的多日多页窗口：整段最多用 4 次请求、
    至少留 1 次给按日阶段；连续两轮每轮都至少推进一天，每轮请求 <= 5。"""
    docs_by_day = {
        "2026-11-12": ["d12-1", "d12-2"],
        "2026-11-13": ["d13-1", "d13-2"],
        "2026-11-14": ["d14-1", "d14-2"],
        "2026-11-15": ["d15-1", "d15-2"],
    }
    covered = "2026-11-11"
    seen: set[str] = set()

    for _round in range(2):
        reqs = _install_date_range_urlopen(
            monkeypatch,
            docs_by_day,
            page_size=1,
            total_upstream_mode=total_upstream_mode,
        )
        internal, ae = collect_mod.collect_internal_and_ae(
            _META,
            since="2026-11-12",
            until="2026-11-15",
            seen=seen,
            ae_covered_until=covered,
        )
        st = internal["status"]["alphaengine"]
        assert st["ok"] is True
        assert st["pages"] <= collect_mod.MAX_CURSOR_PAGES
        assert len(reqs) == st["pages"]
        # 整段最多 4 次，第 5 次切入按日阶段
        assert len(reqs) == 5
        assert reqs[4]["end"] < "2026-11-15"
        new_cov = st["ae_state"]["covered_until"]
        assert new_cov > covered
        assert st["ae_state"]["ran"] is True
        assert st["ae_state"]["resume"] is None
        seen |= set(ae["new"])
        covered = new_cov


# ------------------------------------------- 2. total_upstream 很大：保留第 1 页后期文档 + 去重


def test_large_total_upstream_keeps_page1_future_day_doc_and_dedupes_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """total_upstream 很大时第 1 页后转按日：第 1 页包含一篇本轮按日阶段推进不到的后期
    日期文档（2026-11-17-hot）及一篇重叠文档（2026-11-12-p1），断言后期文档入包
    （删掉 items.extend(full_items) 必败）且 returned 按 doc_id 去重，总请求 <= 5。"""
    days = [f"2026-11-{d:02d}" for d in range(12, 18)]
    docs_by_day = {day: [f"{day}-p1", f"{day}-p2"] for day in days}
    reqs = _install_date_range_urlopen(
        monkeypatch,
        docs_by_day,
        page_size=1,
        total_upstream_mode="large",
        first_page_override={
            ("2026-11-09", "2026-11-17"): [
                ("2026-11-12", "2026-11-12-p1"),
                ("2026-11-17", "2026-11-17-hot"),
            ]
        },
    )

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-17",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    assert len(reqs) == 5
    assert (reqs[0]["start"], reqs[0]["end"]) == ("2026-11-09", "2026-11-17")
    assert (reqs[1]["start"], reqs[1]["end"]) == ("2026-11-09", "2026-11-12")
    assert (reqs[3]["start"], reqs[3]["end"]) == ("2026-11-13", "2026-11-13")

    st = internal["status"]["alphaengine"]
    assert st["ok"] is True
    assert st["incomplete"] is True
    assert st["pages"] == 5
    # 本轮按日只推进到 11-13，未到 11-17；2026-11-17-hot 只能来自整段第 1 页
    assert st["ae_state"] == {"ran": True, "covered_until": "2026-11-13", "resume": None}
    assert "alphaengine:2026-11-17-hot" in ae["new"]
    # 11-12-p1 在第 1 页和首子窗口重复出现，去重后共 5 篇（而非 6）
    assert st["returned"] == 5
    assert set(ae["new"]) == {
        "alphaengine:2026-11-12-p1",
        "alphaengine:2026-11-12-p2",
        "alphaengine:2026-11-13-p1",
        "alphaengine:2026-11-13-p2",
        "alphaengine:2026-11-17-hot",
    }


def test_multi_round_catchup_zero_loss_when_lookback_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7 天 × 每天 1 页（整段超上限，[start - 3, start] <= 4 页）：多轮推进无漏文档，终轮 completed=True。"""
    days = [f"2026-11-{d:02d}" for d in range(11, 18)]
    docs_by_day = {day: [f"{day}-doc"] for day in days}
    expected = {f"alphaengine:{day}-doc" for day in days}

    seen: set[str] = set()
    collected: set[str] = set()
    covered = "2026-11-10"
    until = "2026-11-17"
    rounds = 0

    while True:
        rounds += 1
        assert rounds <= 5
        _install_date_range_urlopen(
            monkeypatch,
            docs_by_day,
            page_size=1,
            total_upstream_mode="large",
        )
        internal, ae = collect_mod.collect_internal_and_ae(
            _META,
            since="2026-11-11",
            until=until,
            seen=seen,
            ae_covered_until=covered,
        )
        st = internal["status"]["alphaengine"]
        assert st["ok"] is True
        assert st["pages"] <= collect_mod.MAX_CURSOR_PAGES
        assert st["ae_state"]["resume"] is None
        new_cov = st["ae_state"]["covered_until"]
        assert new_cov > covered
        assert not (set(ae["new"]) & seen)
        seen |= set(ae["new"])
        collected |= set(ae["new"])
        covered = new_cov
        if st["completed"]:
            break

    assert covered == until
    assert collected == expected


# ------------------------------------------- 3. 回看：探测页全已见、第 4 页迟到研报不丢


def test_lookback_late_report_on_page4_not_lost_when_splitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探测页全已见且总量超限转按日；按日首子窗口 [start - 3, start] 含回看日期，第 4 页迟到研报不丢。"""
    docs_by_day = {
        "2026-11-09": ["seen-1"],
        "2026-11-10": ["seen-2", "seen-3", "late-report-1110"],
        "2026-11-13": ["d13-1", "d13-2", "d13-3"],
        "2026-11-14": ["d14-1", "d14-2", "d14-3"],
    }
    seen = {"alphaengine:seen-1", "alphaengine:seen-2", "alphaengine:seen-3"}
    reqs = _install_date_range_urlopen(
        monkeypatch,
        docs_by_day,
        page_size=1,
        total_upstream_mode="large",
    )

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-14",
        seen=seen,
        ae_covered_until="2026-11-11",
    )

    assert len(reqs) == 5
    assert (reqs[0]["start"], reqs[0]["end"]) == ("2026-11-09", "2026-11-14")
    assert all((r["start"], r["end"]) == ("2026-11-09", "2026-11-12") for r in reqs[1:])
    assert "alphaengine:late-report-1110" in ae["new"]
    st = internal["status"]["alphaengine"]
    assert st["ae_state"] == {"ran": True, "covered_until": "2026-11-12", "resume": None}
    assert "越过" not in (st.get("incomplete_reason") or "")


# ------------------------------------------- 4. 单日 4 页完整扫完；单日 8 页越过；已推进后遇大日停边界


def test_single_day_4_pages_completes_cleanly_within_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """剩 4 页预算时单日 4 页完整扫完不越过。"""
    docs_by_day = {
        "2026-11-12": ["d12-1", "d12-2", "d12-3", "d12-4"],
        "2026-11-13": ["d13-1", "d13-2", "d13-3"],
    }
    _install_date_range_urlopen(monkeypatch, docs_by_day, page_size=1, total_upstream_mode="large")

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-13",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    st = internal["status"]["alphaengine"]
    assert st["ok"] is True
    assert st["pages"] == 5
    assert st["incomplete"] is True
    assert st["ae_state"] == {"ran": True, "covered_until": "2026-11-12", "resume": None}
    assert "越过" not in (st.get("incomplete_reason") or "")
    assert set(ae["new"]) == {"alphaengine:d12-1", "alphaengine:d12-2", "alphaengine:d12-3", "alphaengine:d12-4"}


def test_single_day_8_pages_advances_past_day_and_names_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单日 8 页超出剩余全部预算：收下部分结果、越过该日、incomplete_reason 写明日期。"""
    docs_by_day = {
        "2026-11-12": [f"d12-{i}" for i in range(1, 9)],
        "2026-11-13": ["d13-1"],
    }
    _install_date_range_urlopen(monkeypatch, docs_by_day, page_size=1, total_upstream_mode="large")

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-13",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    st = internal["status"]["alphaengine"]
    assert st["ok"] is True
    assert st["completed"] is False
    assert st["incomplete"] is True
    assert st["pages"] == 5
    assert st["ae_state"] == {"ran": True, "covered_until": "2026-11-12", "resume": None}
    assert "2026-11-12" in (st.get("incomplete_reason") or "")
    assert "越过" in (st.get("incomplete_reason") or "")
    assert len(ae["new"]) == 4


def test_heavy_second_day_after_advancing_stops_at_advanced_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本轮已推进一天后遇到大日：停在已推进边界，不越过第 2 天。"""
    docs_by_day = {
        "2026-11-12": ["d12-1", "d12-2"],
        "2026-11-13": ["d13-1", "d13-2", "d13-3", "d13-4"],
        "2026-11-14": ["d14-1"],
    }
    _install_date_range_urlopen(monkeypatch, docs_by_day, page_size=1, total_upstream_mode="large")

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-14",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    st = internal["status"]["alphaengine"]
    assert st["ok"] is True
    assert st["incomplete"] is True
    assert st["pages"] == 5
    assert st["ae_state"] == {"ran": True, "covered_until": "2026-11-12", "resume": None}
    assert "2026-11-13" in (st.get("incomplete_reason") or "")
    assert "越过" not in (st.get("incomplete_reason") or "")
    assert {"alphaengine:d12-1", "alphaengine:d12-2", "alphaengine:d13-1", "alphaengine:d13-2"} <= set(ae["new"])


# ------------------------------------------- 5. ok: false / 首页失败 / 旧缓存 stale


def test_ok_false_with_no_match_category_is_failure_not_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """以 bridge 返回的 ok 为准：ok: false 即使带“没有匹配”分类也一律视为失败，不推进边界。"""
    _install_scripted_urlopen(
        monkeypatch,
        [
            {
                "ok": False,
                "status": "error",
                "error_category": "没有匹配",
                "error": "[没有匹配] no alphaengine records",
            }
        ],
    )

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-14",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    st = internal["status"]["alphaengine"]
    assert st["ok"] is False
    assert st["completed"] is False
    assert st["incomplete"] is True
    assert st["ae_state"] == {"ran": False, "covered_until": "2026-11-11", "resume": None}
    assert "[没有匹配]" in (st.get("incomplete_reason") or "")

    # 真正的空结果是 ok: true + 空列表 -> 正常完成并推进边界
    _install_scripted_urlopen(monkeypatch, [_ae_page([], cursor=None, has_more=False)])
    internal_ok, _ae_ok = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-14",
        seen=set(),
        ae_covered_until="2026-11-11",
    )
    st_ok = internal_ok["status"]["alphaengine"]
    assert st_ok["ok"] is True
    assert st_ok["completed"] is True
    assert st_ok["ae_state"] == {"ran": True, "covered_until": "2026-11-14", "resume": None}


def test_first_page_failure_records_actual_reason_and_packet_never_says_page_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整段首页即失败：incomplete_reason 写明实际失败原因，packet.md 渲染不得声称“已达本轮翻页上限”。"""
    _install_scripted_urlopen(
        monkeypatch,
        [{"ok": False, "status": "error", "error": "connection refused"}],
    )

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-14",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    st = internal["status"]["alphaengine"]
    assert st["ok"] is False
    assert st["incomplete"] is True
    assert "connection refused" in (st.get("incomplete_reason") or "")
    assert st["ae_state"] == {"ran": False, "covered_until": "2026-11-11", "resume": None}

    md = collect_mod.render_packet_md(
        {
            "ticker": "DASH",
            "run_id": "fail1",
            "since": "2026-11-12",
            "until": "2026-11-14",
            "consensus": {},
            "deltas": {},
            "auto_flags": [],
            "internal": internal,
            "alphaengine": ae,
            "sec": {"status": [], "new": {}},
        }
    )
    assert "connection refused" in md
    assert "已达本轮翻页上限" not in md


@pytest.mark.parametrize("mode", ["stale", "cursor_unavailable"])
def test_stale_or_cursor_unavailable_never_advances_boundary(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """整段或子窗口命中 stale / cursor_unavailable：保留已返回文档，但绝不推进边界、不写 skipped_backlog。"""
    # (a) 整段阶段
    _install_scripted_urlopen(
        monkeypatch,
        [
            _ae_page(
                ["cached-1"],
                cursor=None,
                has_more=(mode == "cursor_unavailable"),
                stale=(mode == "stale"),
                cursor_unavailable=("stale" if mode == "cursor_unavailable" else None),
            )
        ],
    )
    internal_full, ae_full = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-02",
        until="2026-11-21",
        seen=set(),
        ae_covered_until="2026-11-01",
    )
    st_full = internal_full["status"]["alphaengine"]
    assert st_full["ok"] is False
    assert st_full["ae_state"] == {"ran": False, "covered_until": "2026-11-01", "resume": None}
    assert "skipped_backlog" not in st_full
    assert set(ae_full["new"]) == {"alphaengine:cached-1"}

    # (b) 按日首子窗口阶段
    docs_by_day = {
        "2026-11-12": ["d12-1", "d12-2"],
        "2026-11-13": ["d13-1", "d13-2"],
        "2026-11-14": ["d14-1", "d14-2"],
    }
    _install_date_range_urlopen(
        monkeypatch,
        docs_by_day,
        page_size=1,
        total_upstream_mode="large",
        stale_end_days=({"2026-11-12"} if mode == "stale" else frozenset()),
        cursor_unavail_end_days=({"2026-11-12"} if mode == "cursor_unavailable" else frozenset()),
    )
    internal_sub, ae_sub = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-14",
        seen=set(),
        ae_covered_until="2026-11-11",
    )
    st_sub = internal_sub["status"]["alphaengine"]
    assert st_sub["ok"] is False
    assert st_sub["ae_state"] == {"ran": False, "covered_until": "2026-11-11", "resume": None}
    assert "2026-11-12" in (st_sub.get("incomplete_reason") or "")
    assert "alphaengine:d12-1" in ae_sub["new"]


# ------------------------------------------- 6. 同轮游标失效重扫：fresh=True / 不越过 / 不留 error


def test_same_round_cursor_expiry_restarts_with_fresh_true_and_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一轮第 2 页游标失效且有余量：重拉第 1 页带 params.fresh=True 穿透缓存并完成。"""
    reqs = _install_scripted_urlopen(
        monkeypatch,
        [
            _ae_page(["ae-1"], cursor="c1", has_more=True),
            _ae_page(["ae-1"], cursor="c2", has_more=True),
            _ae_page(["ae-2"], cursor=None, has_more=False),
        ],
        expire_cursors={"c1"},
    )

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-15",
        until="2026-11-17",
        seen=set(),
        ae_covered_until="2026-11-14",
    )

    assert len(reqs) == 4
    assert [r.get("params", {}).get("cursor") for r in reqs] == [None, "c1", None, "c2"]
    assert (reqs[2].get("params") or {}).get("fresh") is True
    assert "fresh" not in (reqs[3].get("params") or {})
    st = internal["status"]["alphaengine"]
    assert st["ok"] is True
    assert st["completed"] is True
    assert st["error"] is None
    assert st["cursor_invalid_restart"] is True
    assert st["ae_state"] == {"ran": True, "covered_until": "2026-11-17", "resume": None}
    assert set(ae["new"]) == {"alphaengine:ae-1", "alphaengine:ae-2"}


def test_cursor_restart_then_budget_cap_does_not_advance_and_keeps_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子窗口发生游标失效重扫后再打满预算：不算“正常预算耗尽”，不越过该日，error 为 None，
    incomplete_reason 保留具体游标失效原因。"""
    # start == until = 2026-11-12（预算 5）：第 1 页签 c1 -> 第 2 页 c1 失效 -> 第 3 页 fresh 重扫签 c2
    # -> 第 4 页签 c3 -> 第 5 页仍有 has_more=True
    reqs = _install_scripted_urlopen(
        monkeypatch,
        [
            _ae_page(["ae-1"], cursor="c1", has_more=True),
            _ae_page(["ae-1"], cursor="c2", has_more=True),
            _ae_page(["ae-2"], cursor="c3", has_more=True),
            _ae_page(["ae-3"], cursor="c4", has_more=True),
        ],
        expire_cursors={"c1"},
    )

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-12",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    assert len(reqs) == 5
    assert (reqs[2].get("params") or {}).get("fresh") is True
    st = internal["status"]["alphaengine"]
    assert st["ok"] is True
    assert st["error"] is None
    assert st["incomplete"] is True
    assert st["cursor_invalid_restart"] is True
    # 发生过游标失效重扫，绝不越过 2026-11-12
    assert st["ae_state"] == {"ran": False, "covered_until": "2026-11-11", "resume": None}
    assert "游标失效" in (st.get("incomplete_reason") or "")
    assert "E_CURSOR_EXPIRED" in (st.get("incomplete_reason") or "")
    assert set(ae["new"]) == {"alphaengine:ae-1", "alphaengine:ae-2", "alphaengine:ae-3"}


def test_double_cursor_expiry_never_sets_error_and_does_not_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重扫后再次遇到游标失效：与末页分开判断，绝不把游标失效写进 scan['error']，不越过边界。"""
    _install_scripted_urlopen(
        monkeypatch,
        [
            _ae_page(["ae-1"], cursor="c1", has_more=True),
            _ae_page(["ae-1"], cursor="c2", has_more=True),
        ],
        expire_cursors={"c1", "c2"},
    )

    internal, _ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-12",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    st = internal["status"]["alphaengine"]
    assert st["ok"] is True
    assert st["error"] is None
    assert st["incomplete"] is True
    assert st["ae_state"] == {"ran": False, "covered_until": "2026-11-11", "resume": None}
    assert "再次游标失效" in (st.get("incomplete_reason") or "")


def test_full_window_cursor_expiry_preserved_in_cross_day_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整段跨日路径中整段末页游标失效、按日阶段用剩 1 次完成首日：
    error 为 None，且汇总 incomplete_reason 保留具体游标失效原因。"""
    # start=11-12, until=11-14（多日，整段预算 4）：
    # 第 1-3 页成功，第 4 页 c3 游标失效；剩 1 页扫首子窗口 [11-09, 11-12] 完成（covered=11-12 < 11-14）
    _install_scripted_urlopen(
        monkeypatch,
        [
            _ae_page(["p1"], cursor="c1", has_more=True),
            _ae_page(["p2"], cursor="c2", has_more=True),
            _ae_page(["p3"], cursor="c3", has_more=True),
            _ae_page(["d12-ok"], cursor=None, has_more=False),
        ],
        expire_cursors={"c3"},
    )

    internal, _ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-14",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    st = internal["status"]["alphaengine"]
    assert st["ok"] is True
    assert st["error"] is None
    assert st["incomplete"] is True
    assert st["pages"] == 5
    assert st["ae_state"] == {"ran": True, "covered_until": "2026-11-12", "resume": None}
    assert "游标失效" in (st.get("incomplete_reason") or "")
    assert "E_CURSOR_EXPIRED" in (st.get("incomplete_reason") or "")


# ------------------------------------------- 7. 首次运行 & 7 天积压截断（截断当天不漏）


def test_first_run_inclusive_since_does_not_skip_start_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """首次运行（since 包含式）：先换算 covered_until = since - 1，起始日当天不漏扫。"""
    _patch_snapshot(monkeypatch)
    docs_by_day = {
        "2026-11-14": ["d14-1", "d14-2"],
        "2026-11-15": ["d15-1", "d15-2"],
        "2026-11-16": ["d16-1", "d16-2"],
        "2026-11-17": ["d17-1", "d17-2"],
    }
    reqs = _install_date_range_urlopen(monkeypatch, docs_by_day, page_size=1, total_upstream_mode="large")
    _freeze_now(monkeypatch, 2026, 11, 17, 23, 0)

    last_run = _run_collect_commit(monkeypatch, tmp_path, "first")

    assert (reqs[0]["start"], reqs[0]["end"]) == ("2026-11-11", "2026-11-17")
    assert (reqs[1]["start"], reqs[1]["end"]) == ("2026-11-11", "2026-11-14")
    assert last_run["ae_covered_until"] == "2026-11-15"
    assert last_run["ae_resume"] is None
    seen = set(json.loads((tmp_path / "research" / "tracker" / "seen.json").read_text(encoding="utf-8")))
    assert {"alphaengine:d14-1", "alphaengine:d14-2"} <= seen


def test_20_day_backlog_cap_includes_cutoff_day_doc_and_only_writes_when_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """积压 20 天（covered_until=11-01，until=11-21）：跳至 11-15（until - 6），
    在截断当天 2026-11-15 放一篇文档断言不漏；失败轮（ran=False）不写 skipped_backlog。"""
    docs_by_day = {
        "2026-11-05": ["old-skipped-doc"],
        "2026-11-15": ["cutoff-day-1115-doc", "cutoff-day-1115-doc2"],
        "2026-11-16": ["d16-1", "d16-2"],
        "2026-11-20": ["d20-1", "d20-2"],
    }
    reqs_ok = _install_date_range_urlopen(
        monkeypatch,
        docs_by_day,
        page_size=1,
        total_upstream_mode="large",
    )

    internal_ok, ae_ok = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-02",
        until="2026-11-21",
        seen=set(),
        ae_covered_until="2026-11-01",
    )
    assert (reqs_ok[0]["start"], reqs_ok[0]["end"]) == ("2026-11-12", "2026-11-21")
    assert (reqs_ok[1]["start"], reqs_ok[1]["end"]) == ("2026-11-12", "2026-11-15")
    assert "alphaengine:cutoff-day-1115-doc" in ae_ok["new"]
    assert "alphaengine:cutoff-day-1115-doc2" in ae_ok["new"]
    assert "alphaengine:old-skipped-doc" not in ae_ok["new"]
    st_ok = internal_ok["status"]["alphaengine"]
    assert st_ok["ae_state"] == {"ran": True, "covered_until": "2026-11-16", "resume": None}
    assert st_ok["skipped_backlog"] == {"since": "2026-11-02", "until": "2026-11-14"}

    # 首页失败（ran=False）：边界维持 11-01，不写 skipped_backlog
    _install_scripted_urlopen(
        monkeypatch,
        [{"ok": False, "status": "error", "error": "connection refused"}],
    )
    internal_fail, _ae_fail = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-02",
        until="2026-11-21",
        seen=set(),
        ae_covered_until="2026-11-01",
    )
    st_fail = internal_fail["status"]["alphaengine"]
    assert st_fail["ok"] is False
    assert st_fail["ae_state"] == {"ran": False, "covered_until": "2026-11-01", "resume": None}
    assert "skipped_backlog" not in st_fail


# ------------------------------------------- 8. 同日重跑 covered_until == until


def test_same_day_rerun_uses_early_exit_and_4_page_cap_without_moving_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同日重跑（covered_until == until）：不禁用首页总数判断，且整段最多 4 次请求；
    扫不完如实标未完成、边界不动，报告不说“已越过该日”。"""
    # (a) 首页 total_upstream 超限 -> 1 次即停
    reqs_early = _install_scripted_urlopen(
        monkeypatch,
        [_ae_page(["ae-1"], cursor="c1", has_more=True, total_upstream=200)],
    )
    internal_early, ae_early = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-17",
        until="2026-11-17",
        seen=set(),
        ae_covered_until="2026-11-17",
    )
    assert len(reqs_early) == 1
    assert (reqs_early[0]["start"], reqs_early[0]["end"]) == ("2026-11-14", "2026-11-17")
    st_early = internal_early["status"]["alphaengine"]
    assert st_early["incomplete"] is True
    assert st_early["ae_state"] == {"ran": True, "covered_until": "2026-11-17", "resume": None}
    assert "越过" not in (st_early.get("incomplete_reason") or "")

    # (b) 缺 total_upstream -> 最多 4 次请求（MAX_CURSOR_PAGES - 1）
    reqs_cap = _install_scripted_urlopen(
        monkeypatch,
        [_ae_page([f"ae-{i}"], cursor=f"c{i}", has_more=True) for i in range(1, 7)],
    )
    internal_cap, ae_cap = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-17",
        until="2026-11-17",
        seen=set(),
        ae_covered_until="2026-11-17",
    )
    assert len(reqs_cap) == 4
    st_cap = internal_cap["status"]["alphaengine"]
    assert st_cap["incomplete"] is True
    assert st_cap["ae_state"] == {"ran": True, "covered_until": "2026-11-17", "resume": None}
    assert "越过" not in (st_cap.get("incomplete_reason") or "")

    md = collect_mod.render_packet_md(
        {
            "ticker": "DASH",
            "run_id": "rerun",
            "since": "2026-11-17",
            "until": "2026-11-17",
            "consensus": {},
            "deltas": {},
            "auto_flags": [],
            "internal": internal_early,
            "alphaengine": ae_early,
            "sec": {"status": [], "new": {}},
        }
    )
    assert "越过" not in md
    assert "当前覆盖边界为 2026-11-17" in md


# ------------------------------------------- 9. 旧格式状态 & 旧 packet.json 迁移一致 + --no-ae + packet.md


def test_legacy_state_and_legacy_packet_commit_migrate_consistently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧格式 last_run.json 与升级前旧 packet.json 迁移结果一致，且不发送旧 cursor。"""
    ws_pkt = tmp_path / "ws_pkt"
    tracker_pkt = ws_pkt / "research" / "tracker"
    run_dir = tracker_pkt / "legacy-run"
    run_dir.mkdir(parents=True)
    (run_dir / "packet.json").write_text(
        json.dumps(
            {
                "ticker": "DASH",
                "run_id": "legacy-run",
                "since": "2026-11-12",
                "until": "2026-11-16",
                "ae_state": {
                    "ran": True,
                    "covered_until": "2026-11-15",
                    "resume": {
                        "window": {"since": "2026-11-12", "until": "2026-11-16"},
                        "cursor": "expired-legacy-cursor",
                        "query": "DoorDash",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    class _LegacyCommitArgs:
        workspace_dir = str(ws_pkt)
        run_id = "legacy-run"
        ticker = "DASH"
        material = "no"
        summary = "旧包迁移"

    assert collect_mod.cmd_commit(_LegacyCommitArgs()) == 0
    committed_last_run = json.loads((tracker_pkt / "last_run.json").read_text(encoding="utf-8"))
    assert committed_last_run["ae_covered_until"] == "2026-11-11"
    assert committed_last_run["ae_resume"] is None

    ws_col = tmp_path / "ws_col"
    tracker_col = ws_col / "research" / "tracker"
    tracker_col.mkdir(parents=True)
    (tracker_col / "last_run.json").write_text(
        json.dumps(
            {
                "ticker": "DASH",
                "until": "2026-11-16",
                "ae_covered_until": "2026-11-15",
                "ae_resume": {
                    "window": {"since": "2026-11-12", "until": "2026-11-16"},
                    "cursor": "expired-legacy-cursor",
                    "query": "DoorDash",
                },
            }
        ),
        encoding="utf-8",
    )
    migrated_cov, migrated_res = collect_mod._ae_state_of(
        json.loads((tracker_col / "last_run.json").read_text(encoding="utf-8"))
    )
    assert migrated_cov == committed_last_run["ae_covered_until"] == "2026-11-11"
    assert migrated_res is None

    _patch_snapshot(monkeypatch)
    reqs = _install_scripted_urlopen(
        monkeypatch,
        [_ae_page(["ae-recovered"], cursor=None, has_more=False)],
        expire_cursors={"expired-legacy-cursor"},
    )
    _freeze_now(monkeypatch, 2026, 11, 17, 23, 0)

    last_run_after = _run_collect_commit(monkeypatch, ws_col, "mig-collect")
    assert len(reqs) == 1
    assert "cursor" not in (reqs[0].get("params") or {})
    assert reqs[0]["start"] == "2026-11-09"
    assert reqs[0]["end"] == "2026-11-17"
    assert last_run_after["ae_covered_until"] == "2026-11-17"
    assert last_run_after["ae_resume"] is None


def test_old_state_no_ae_commits_derived_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧格式 last_run（只有 until=11-12）→ 11-16 执行 --no-ae 并 commit，随后恢复。"""
    _patch_snapshot(monkeypatch)
    tracker = tmp_path / "research" / "tracker"
    tracker.mkdir(parents=True)
    (tracker / "last_run.json").write_text(
        json.dumps({"ticker": "DASH", "until": "2026-11-12", "committed_at": "x"}),
        encoding="utf-8",
    )

    reqs1 = _install_scripted_urlopen(monkeypatch, [])
    _freeze_now(monkeypatch, 2026, 11, 16, 23, 0)
    last_run = _run_collect_commit(monkeypatch, tmp_path, "noae", no_ae=True)
    assert last_run["ae_covered_until"] == "2026-11-12"
    assert last_run["ae_resume"] is None
    assert not reqs1

    reqs2 = _install_scripted_urlopen(monkeypatch, [_ae_page(["ae-back"], cursor=None, has_more=False)])
    _freeze_now(monkeypatch, 2026, 11, 17, 23, 0)
    last_run2 = _run_collect_commit(monkeypatch, tmp_path, "back")
    assert reqs2[0]["start"] == "2026-11-10"
    assert reqs2[0]["end"] == "2026-11-17"
    assert last_run2["ae_covered_until"] == "2026-11-17"


def test_old_packet_without_ae_state_commits_null_fields(tmp_path: Path) -> None:
    """旧 packet（无 ae_state 键）commit 不报错；无上一份状态时 ae 字段为 None。"""
    tracker = tmp_path / "research" / "tracker"
    run_dir = tracker / "r2"
    run_dir.mkdir(parents=True)
    (run_dir / "packet.json").write_text(
        json.dumps({"ticker": "DASH", "run_id": "r2", "since": "a", "until": "b"}),
        encoding="utf-8",
    )

    class _ArgsOld:
        workspace_dir = str(tmp_path)
        run_id = "r2"
        ticker = "DASH"
        material = "no"
        summary = "旧包"

    assert collect_mod.cmd_commit(_ArgsOld()) == 0
    last_run = json.loads((tracker / "last_run.json").read_text(encoding="utf-8"))
    assert last_run["ae_covered_until"] is None
    assert last_run["ae_resume"] is None


def test_packet_md_discloses_actual_start_day_on_skipped_backlog() -> None:
    """packet.md 披露跳过积压时写明实际起扫日（skipped_backlog.until + 1 天）。"""
    pk = {
        "ticker": "DASH",
        "run_id": "20261121-2300",
        "since": "2026-11-02",
        "until": "2026-11-21",
        "consensus": {},
        "deltas": {},
        "auto_flags": [],
        "internal": {
            "status": {
                "alphaengine": {
                    "ok": True,
                    "completed": False,
                    "incomplete": True,
                    "incomplete_reason": "未完成: 单日 2026-11-15 在剩余预算内未扫完，已越过该日",
                    "skipped_backlog": {"since": "2026-11-02", "until": "2026-11-14"},
                    "ae_state": {"ran": True, "covered_until": "2026-11-15", "resume": None},
                }
            },
            "new": {},
        },
        "alphaengine": {"status": [], "new": {}},
        "sec": {"status": [], "new": {}},
    }
    md = collect_mod.render_packet_md(pk)
    assert "更早的积压已跳过" in md
    assert "2026-11-02 至 2026-11-14" in md
    assert "实际从 2026-11-15 起扫" in md
    assert "当前覆盖边界为 2026-11-15" in md
    assert "不得当作" in md


def test_day_phase_first_page_bypasses_bridge_cache_and_still_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整段用满 4 次仍未扫完后，按日阶段首页带 fresh=True 穿透 bridge 缓存，
    剩下的 1 次请求也能扫完首个子窗口并推进一天。"""
    pages = [
        _ae_page([f"w{i}"], cursor=f"wc{i}", has_more=True) for i in range(1, 5)
    ] + [_ae_page(["day1"], cursor=None, has_more=False, publish_time="2026-11-12")]
    reqs = _install_scripted_urlopen(monkeypatch, pages)

    internal, _ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-17",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    assert len(reqs) == 5
    assert [r.get("params", {}).get("fresh") for r in reqs] == [True, None, None, None, True]
    st = internal["status"]["alphaengine"]
    assert st["ae_state"]["covered_until"] == "2026-11-12"


def test_first_pages_bypass_bridge_cache_and_cursor_pages_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每个首页请求带 fresh=True（bridge 截取缓存可能返回 has_more 却无 cursor 的首页）；带 cursor 的续页不带。"""
    pages = [
        _ae_page(["s1"], cursor="real1", has_more=True),
        _ae_page(["s2"], cursor=None, has_more=False),
    ]
    reqs = _install_scripted_urlopen(monkeypatch, pages)

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-17",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    assert [r.get("params", {}).get("fresh") for r in reqs] == [True, None]
    assert [r.get("params", {}).get("cursor") for r in reqs] == [None, "real1"]
    st = internal["status"]["alphaengine"]
    assert st["completed"] is True
    assert st["ae_state"]["covered_until"] == "2026-11-17"
    assert set(ae["new"]) == {"alphaengine:s1", "alphaengine:s2"}


def test_round_level_cursor_restart_keeps_boundary_when_day_phase_runs_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整段阶段发生过游标失效重扫、按日阶段只剩 1 次请求且首个子窗口仍有更多页时，
    剩余预算是被重扫吃掉的：不越过首个子窗口，边界不动，并保留游标失效原因。"""
    pages = [
        _ae_page(["w1"], cursor="dead", has_more=True),
        # "dead" 失效后带 fresh=True 从首页重扫
        _ae_page(["w1"], cursor="r1", has_more=True),
        _ae_page(["w2"], cursor="r2", has_more=True),
        _ae_page(["w3"], cursor="r3", has_more=True),
        # 按日阶段首个子窗口：仍有更多页
        _ae_page(["d1"], cursor="dc1", has_more=True, publish_time="2026-11-12"),
    ]
    reqs = _install_scripted_urlopen(monkeypatch, pages, expire_cursors={"dead"})

    internal, _ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-17",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    assert len(reqs) == 5
    st = internal["status"]["alphaengine"]
    assert st["ae_state"]["covered_until"] == "2026-11-11"
    assert st["incomplete"] is True
    assert "游标失效" in str(st.get("incomplete_reason"))


def test_cursor_expiry_on_last_window_request_keeps_boundary_without_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整段第 4 次请求游标失效（已无重扫预算）后，按日阶段的 1 次请求未扫完首个子窗口：
    本轮发生过游标失效，边界不动，汇总写明游标失效原因。"""
    pages = [
        _ae_page(["w1"], cursor="c1", has_more=True),
        _ae_page(["w2"], cursor="c2", has_more=True),
        _ae_page(["w3"], cursor="c3", has_more=True),
        _ae_page(["d1"], cursor="dc1", has_more=True, publish_time="2026-11-12"),
    ]
    reqs = _install_scripted_urlopen(monkeypatch, pages, expire_cursors={"c3"})

    internal, _ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-17",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    assert len(reqs) == 5
    st = internal["status"]["alphaengine"]
    assert st["ae_state"]["covered_until"] == "2026-11-11"
    assert "游标失效" in str(st.get("incomplete_reason"))
    assert st.get("error") in (None, "")


def test_cursor_restart_failure_keeps_first_page_items_and_both_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首页成功、续页游标失效、重扫首页失败：首页条目仍入包，边界不动，
    失败说明同时写明重扫失败与此前的游标失效。"""
    pages = [
        _ae_page(["keep1"], cursor="x1", has_more=True),
        {"ok": False, "status": "timeout", "error": "alphaengine timed out after 60s"},
    ]
    _install_scripted_urlopen(monkeypatch, pages, expire_cursors={"x1"})

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-17",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    st = internal["status"]["alphaengine"]
    assert "alphaengine:keep1" in set(ae["new"])
    assert st["ae_state"]["covered_until"] == "2026-11-11"
    assert "游标失效" in str(st.get("error"))
    assert "timed out" in str(st.get("error"))


def test_dedupe_keeps_the_latest_version_of_a_document() -> None:
    """同一 doc_id 重复出现时保留最后取得的版本（例如重扫补齐了摘要），位置不变。"""
    first = {"doc_id": "d1", "title": "研报 d1", "snippet": ""}
    other = {"doc_id": "d2", "title": "研报 d2", "snippet": "x"}
    refreshed = {"doc_id": "d1", "title": "研报 d1", "snippet": "补齐的摘要"}
    out = collect_mod._dedupe_ae_items([first, other, refreshed])
    assert [o["doc_id"] for o in out] == ["d1", "d2"]
    assert out[0]["snippet"] == "补齐的摘要"


def test_dedupe_stale_copy_never_overrides_a_fresh_one() -> None:
    """过期缓存里的同一文档只补充缺失条目，不覆盖本轮正常取得的版本；标记不外泄。"""
    key = collect_mod._AE_STALE_KEY
    fresh = {"doc_id": "d1", "title": "研报 d1", "snippet": "正常摘要"}
    stale_same = {"doc_id": "d1", "title": "研报 d1", "snippet": "", key: True}
    stale_new = {"doc_id": "d9", "title": "研报 d9", "snippet": "", key: True}
    out = collect_mod._dedupe_ae_items([fresh, stale_same, stale_new])
    assert [o["doc_id"] for o in out] == ["d1", "d9"]
    assert out[0]["snippet"] == "正常摘要"
    assert all(key not in o for o in out)


def test_day_phase_stale_copy_keeps_window_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整段先取到带摘要的文档，按日阶段遇到上游故障返回同一文档的过期缓存：入包的仍是带摘要的版本。"""
    window = [
        {**_ae_page(["dup"], cursor="c1", has_more=True), "total_upstream": 500},
    ]
    window[0]["results"][0]["snippet"] = "正常摘要"
    stale_day = _ae_page(["dup"], cursor=None, has_more=False, publish_time="2026-11-12", stale=True)
    stale_day["results"][0]["snippet"] = ""
    _install_scripted_urlopen(monkeypatch, window + [stale_day])

    internal, ae = collect_mod.collect_internal_and_ae(
        _META,
        since="2026-11-12",
        until="2026-11-17",
        seen=set(),
        ae_covered_until="2026-11-11",
    )

    doc = ae["new"]["alphaengine:dup"]
    assert "正常摘要" in str(doc)
    assert internal["status"]["alphaengine"]["ae_state"]["covered_until"] == "2026-11-11"
