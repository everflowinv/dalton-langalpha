"""collect.py 的来源结果解析（batch 5 / F 线）：SEC 嵌套桥接响应解包、桥接
"没有匹配"分类、Form 4 目录写法与 Guidepoint 问答正文保留。

全部用桥接的真实 HTTP 响应形状在内存重放（_call_bridge_http →
_normalize_dict_result → 采集函数），不调用真实桥接。
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COLLECT_SCRIPT_PATH = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-thesis-delta" / "scripts" / "collect.py"
)

# Captured at import, before any monkeypatch replaces the module attribute:
# create_autospec needs the real function, not another mock.
_REAL_URLOPEN = urllib.request.urlopen


def _load_collect_module():
    spec = importlib.util.spec_from_file_location("dalton_thesis_delta_collect", COLLECT_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _bridge_http_body(payload: dict[str, Any]) -> bytes:
    """桥接 _make_tool_result_response 的真实 HTTP 响应体：content 文本块即
    enriched payload（_sources/_declared_sources 前置 + MCP 服务器 payload）。"""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
                ],
                "structuredContent": dict(payload),
                "isError": not bool(payload.get("ok", True)),
            },
        }
    ).encode("utf-8")


def _install_bridge_http(
    monkeypatch: pytest.MonkeyPatch, route_payloads: dict[str, dict[str, Any]]
) -> None:
    """按 route 分发内存桥接响应；未给出的 route 一律答成功空结果。"""

    def _urlopen(req: urllib.request.Request, timeout: int | None = None) -> _FakeHTTPResponse:
        body = json.loads(req.data.decode("utf-8"))
        arguments = (body.get("params") or {}).get("arguments") or {}
        route = str(arguments.get("route") or "")
        payload = route_payloads.get(
            route, {"ok": True, "status": "ok", "data": {"results": []}}
        )
        return _FakeHTTPResponse(_bridge_http_body(payload))

    fake_urlopen = create_autospec(_REAL_URLOPEN, side_effect=_urlopen)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("DALTON_RESEARCHDATA_URL", "http://127.0.0.1:8961/rpc")


_META = {"ticker": "META", "name": "Meta", "aliases": ["META"], "sec_forms": ["8-K"]}


# ---------------------------------------------------------------------------
# 项 1：SEC 嵌套响应解包
# ---------------------------------------------------------------------------


def test_sec_nested_bridge_payload_collects_the_filing(monkeypatch: pytest.MonkeyPatch) -> None:
    """桥接成功响应把公告放在 data.rows（MCP 服务器 payload 嵌套层，真实列表在
    res["data"]["data"]["rows"]）：采集必须拿到那 1 条 8-K，而不是计成 0 条。"""
    collect_mod = _load_collect_module()
    _install_bridge_http(
        monkeypatch,
        {
            "sec": {
                "_declared_sources": [
                    {"identity": "sec.edgar.list-filings", "as_of": "2026-10-28"}
                ],
                "_sources": [{"identity": "sec.edgar.list-filings", "as_of": "2026-10-28"}],
                "ok": True,
                "status": "ok",
                "ticker": "META",
                "data": {
                    "rows": [
                        {
                            "accession_no": "0001652044-26-000098",
                            "form": "8-K",
                            "filing_date": "2026-10-27",
                            "report_date": "2026-10-27",
                        }
                    ]
                },
                "normalized_params": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": "META",
                },
            }
        },
    )

    out = collect_mod.collect_sec_filings(_META, since="2026-10-25", seen=set())
    assert out["status"] == [{"form": "8-K", "ok": True, "count": 1}]
    assert out["new"] == {
        "sec:0001652044-26-000098": {
            "form": "8-K",
            "filing_date": "2026-10-27",
            "report_date": "2026-10-27",
            "accession": "0001652044-26-000098",
        }
    }


def test_sec_top_level_rows_payload_still_collects(monkeypatch: pytest.MonkeyPatch) -> None:
    """桥接 SEC return_shape 的另一分支（顶层 rows）同样解包成功。"""
    collect_mod = _load_collect_module()
    _install_bridge_http(
        monkeypatch,
        {
            "sec": {
                "ok": True,
                "status": "ok",
                "ticker": "META",
                "rows": [
                    {
                        "accession_no": "0001652044-26-000099",
                        "form": "8-K",
                        "filing_date": "2026-10-26",
                        "report_date": "2026-09-30",
                    }
                ],
            }
        },
    )

    out = collect_mod.collect_sec_filings(_META, since="2026-10-25", seen=set())
    assert out["status"] == [{"form": "8-K", "ok": True, "count": 1}]
    assert "sec:0001652044-26-000099" in out["new"]


# ---------------------------------------------------------------------------
# 项 2：桥接"没有匹配"分类 + Form 4 目录写法
# ---------------------------------------------------------------------------


def test_sec_classified_no_match_is_a_successful_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """桥接已把"没有匹配"单独分类（ok=false + error_category="没有匹配"）：
    列表查询的无匹配记为成功的空结果，不得进 failed_sources 报成
    "SEC 数据缺口"。"""
    collect_mod = _load_collect_module()
    _install_bridge_http(
        monkeypatch,
        {
            "sec": {
                "_declared_sources": [],
                "_sources": [],
                "ok": False,
                "status": "error",
                "error": "[没有匹配] no filings matching form '4' found",
                "error_category": "没有匹配",
                "hint": (
                    "先用 search-concepts / list-filings / attachments 拿到可用的名称或 "
                    "ID 再读，不要猜"
                ),
            }
        },
    )

    out = collect_mod.collect_sec_filings(_META, since="2026-10-25", seen=set())
    st = out["status"][0]
    assert st["form"] == "8-K"
    assert st["ok"] is True
    assert st["count"] == 0
    assert st.get("empty_reason")
    assert out["new"] == {}
    # 无匹配不再是失败来源，也不再阻断短路判断
    assert collect_mod.summarize_source_failures({}, {}, out) == []
    assert (
        collect_mod.can_short_circuit(
            auto_flags=[],
            internal_data={"status": {}, "new": {}},
            ae_data={"status": [], "new": {}},
            sec_data=out,
        )
        is True
    )


def test_sec_confirmed_empty_reason_stays_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """桥接确认空结果契约（ok=true + empty_reason）：保持成功空结果并记录原因。"""
    collect_mod = _load_collect_module()
    _install_bridge_http(
        monkeypatch,
        {
            "sec": {
                "ok": True,
                "status": "empty",
                "empty_reason": "no_matching_records",
                "ticker": "META",
                "results": [],
                "hint": "路由 'sec' 查询成功但无匹配记录；可调整 query 关键词重试。",
            }
        },
    )

    out = collect_mod.collect_sec_filings(_META, since="2026-10-25", seen=set())
    assert out["status"] == [
        {"form": "8-K", "ok": True, "count": 0, "empty_reason": "no_matching_records"}
    ]
    assert collect_mod.summarize_source_failures({}, {}, out) == []


def test_sec_real_failures_stay_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实的超时/权限错误仍按取数失败记录，进 failed_sources。"""
    collect_mod = _load_collect_module()
    for error, category in (
        ("[上游不可用] researchdata_bridge timed out after 120s on finance/sec", "上游不可用"),
        ("[权限或额度] sec: quota exceeded", "权限或额度"),
    ):
        _install_bridge_http(
            monkeypatch,
            {
                "sec": {
                    "ok": False,
                    "status": "error",
                    "error": error,
                    "error_category": category,
                }
            },
        )
        out = collect_mod.collect_sec_filings(_META, since="2026-10-25", seen=set())
        st = out["status"][0]
        assert st["ok"] is False, error
        assert st["count"] == 0, error
        assert st["error"] == error
        failures = collect_mod.summarize_source_failures({}, {}, out)
        assert failures == [{"source": "sec", "error": f"8-K:{error}"}], error


def test_default_sec_forms_follow_the_bridge_catalog() -> None:
    """内幕交易表单按桥接 ROUTE_CONTRACT_SPECS 的 param_formats 写作 EDGAR
    表单代码 "4"（10-K | 10-Q | 8-K | 20-F | 4），不是 "Form 4"。"""
    collect_mod = _load_collect_module()
    assert "4" in collect_mod.DEFAULT_SEC_FORMS
    assert "Form 4" not in collect_mod.DEFAULT_SEC_FORMS
    tool_name, args = collect_mod.build_sec_filing_request("META", "4")
    assert tool_name == "finance"
    assert args["form"] == "4"


# ---------------------------------------------------------------------------
# 项 3：Guidepoint 问答正文保留
# ---------------------------------------------------------------------------


def _guidepoint_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    """guidepoint search_library 的真实返回形状：条目在 data.structuredContent.data。"""
    return {
        "_declared_sources": [{"identity": "guidepoint", "as_of": "2026-10-28"}],
        "_sources": [{"identity": "guidepoint", "as_of": "2026-10-28"}],
        "ok": True,
        "status": "ok",
        "data": {"structuredContent": {"data": items}},
        "normalized_params": {"route": "guidepoint", "operation": "search_library"},
    }


def test_guidepoint_qa_body_is_kept_and_aggregated_per_interview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实返回项的 question/answer/context 必须随包保留并按访谈聚合——
    Guidepoint 没有全文读取，丢在采集里就永久丢失。真实形状的 id 每条问答
    独立（qapair:library_ts:<uuid>_N），规范化后归到同一访谈条目，且每条
    问答保留自己的原始 id。"""
    collect_mod = _load_collect_module()
    uuid = "640F2FFC-173A-4028-9651-AB8B5383C138"
    items = [
        {
            "id": f"qapair:library_ts:{uuid}_1",
            "transcript_name": "Meta: Entering the AI Cloud",
            "date": "2026-07-02",
            "reference_url": f"https://platform.guidepoint.com/transcript?id={uuid}",
            "question": "Reels 广告加载率本季度环比变化如何？",
            "answer": "环比继续提升，主因库存扩容和 Pacing 改进（受访者原话转述）。",
            "context": "Respondent: 前媒体代理商投放负责人，2024 年离职",
        },
        {
            "id": f"qapair:library_ts:{uuid}_8",
            "transcript_name": "Meta: Entering the AI Cloud",
            "date": "2026-07-02",
            "reference_url": f"https://platform.guidepoint.com/transcript?id={uuid}",
            "question": "广告主预算有向短视频转移的迹象吗？",
            "answer": "有，但节奏慢于管理层口径。",
        },
    ]
    _install_bridge_http(monkeypatch, {"guidepoint": _guidepoint_payload(items)})

    internal, ae = collect_mod.collect_internal_and_ae(
        _META, since="2026-06-25", until="2026-07-05", seen=set(), skip_ae=True
    )
    assert internal["status"]["guidepoint"]["ok"] is True
    assert internal["status"]["guidepoint"]["returned"] == 2
    # 同一访谈的两条问答（qapair id 仅尾缀不同）聚合成一个条目
    assert set(internal["new"]) == {f"guidepoint:{uuid}"}
    entry = internal["new"][f"guidepoint:{uuid}"]
    assert entry["route"] == "guidepoint"
    assert entry["title"] == "Meta: Entering the AI Cloud"
    assert entry["date"] == "2026-07-02"
    assert entry["from"] == "Guidepoint"
    assert [qa["question"] for qa in entry["qa"]] == [
        "Reels 广告加载率本季度环比变化如何？",
        "广告主预算有向短视频转移的迹象吗？",
    ]
    assert entry["qa"][0]["answer"] == "环比继续提升，主因库存扩容和 Pacing 改进（受访者原话转述）。"
    assert entry["qa"][0]["context"] == "Respondent: 前媒体代理商投放负责人，2024 年离职"
    assert "context" not in entry["qa"][1]
    # 每条问答保留自己的原始 id（qapair 级，区别于访谈级条目 key）
    assert [qa["id"] for qa in entry["qa"]] == [
        f"qapair:library_ts:{uuid}_1",
        f"qapair:library_ts:{uuid}_8",
    ]
    assert ae["new"] == {}


def test_guidepoint_reference_url_is_kept_as_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guidepoint search_library 的链接字段是 reference_url（来源声明读同一
    字段）；采集保留它作为问答引用，packet.md 里有"引用"行。"""
    collect_mod = _load_collect_module()
    items = [
        {
            "id": "library_ts:gp-778899",
            "transcript_name": "China EV Battery Supply Chain & Pricing",
            "date": "2026-04-15",
            "reference_url": "https://new.guidepoint.com/transcripts/gp-778899",
            "question": "商户续约率有变化吗？",
            "answer": "小型商户流失略升。",
        }
    ]
    _install_bridge_http(monkeypatch, {"guidepoint": _guidepoint_payload(items)})

    internal, _ = collect_mod.collect_internal_and_ae(
        _META, since="2026-04-10", until="2026-04-20", seen=set(), skip_ae=True
    )
    entry = internal["new"]["guidepoint:gp-778899"]
    assert (
        entry["qa"][0]["citation"] == "https://new.guidepoint.com/transcripts/gp-778899"
    )
    # 引用要进 packet.md（分析师读的增量包）
    packet = {
        "ticker": "META",
        "run_id": "20260415-0310",
        "since": "2026-04-10",
        "until": "2026-04-20",
        "snapshot_file": "data/consensus/2026-04-15_0310.json",
        "failed_sources": [],
        "short_circuit_eligible": False,
        "auto_flags": [],
        "deltas": {},
        "consensus": {},
        "internal": internal,
        "alphaengine": {"status": [], "new": {}},
        "sec": {"status": [], "new": {}},
    }
    md = collect_mod.render_packet_md(packet)
    assert "引用：https://new.guidepoint.com/transcripts/gp-778899" in md


def test_guidepoint_qa_reaches_packet_md(monkeypatch: pytest.MonkeyPatch) -> None:
    """问答正文要进 packet.md（分析师读的增量包），不是只进 packet.json。"""
    collect_mod = _load_collect_module()
    items = [
        {
            "id": "guidepoint:LIB-TS-8842",
            "transcript_name": "Expert call: Meta Reels advertising",
            "date": "2026-10-26",
            "question": "Reels 广告加载率本季度环比变化如何？",
            "answer": "环比继续提升，主因库存扩容和 Pacing 改进（受访者原话转述）。",
            "context": "Respondent: 前媒体代理商投放负责人",
        }
    ]
    _install_bridge_http(monkeypatch, {"guidepoint": _guidepoint_payload(items)})
    internal, _ = collect_mod.collect_internal_and_ae(
        _META, since="2026-10-20", until="2026-10-28", seen=set(), skip_ae=True
    )

    packet = {
        "ticker": "META",
        "run_id": "20261028-0310",
        "since": "2026-10-20",
        "until": "2026-10-28",
        "snapshot_file": "data/consensus/2026-10-28_0310.json",
        "failed_sources": [],
        "short_circuit_eligible": False,
        "auto_flags": [],
        "deltas": {},
        "consensus": {},
        "internal": internal,
        "alphaengine": {"status": [], "new": {}},
        "sec": {"status": [], "new": {}},
    }
    md = collect_mod.render_packet_md(packet)
    assert "Reels 广告加载率本季度环比变化如何？" in md
    assert "环比继续提升，主因库存扩容和 Pacing 改进（受访者原话转述）。" in md
    assert "Respondent: 前媒体代理商投放负责人" in md
