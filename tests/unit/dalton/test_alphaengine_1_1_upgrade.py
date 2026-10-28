"""Unit tests for AlphaEngine 1.1.0 / researchdata 1.4.0 Dalton adaptation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

import scripts.dalton.alphaengine_usage_report as usage_report
import scripts.dalton.researchdata_bridge as rb
from scripts.dalton import source_identity

REPO_ROOT = Path(__file__).resolve().parents[3]

COLLECT_SCRIPT_PATH = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-thesis-delta" / "scripts" / "collect.py"
)


def _load_collect_module():
    spec = importlib.util.spec_from_file_location("dalton_thesis_delta_collect_ae11", COLLECT_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> rb.ResearchDataBridge:
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_ALPHAENGINE_USAGE_FILE", str(tmp_path / "alphaengine_usage.jsonl"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    dummy_py = tmp_path / "dummy_python"
    dummy_py.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_OCW_PYTHON", str(dummy_py))
    cfg = rb.BridgeConfig()
    return rb.ResearchDataBridge(cfg)


def _mcp_tool_response(req_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "structuredContent": dict(payload),
            "isError": not bool(payload.get("ok")),
        },
    }


# ---------------------------------------------------------------------------
# 1. 9 个操作都能通过桥的参数校验；`download_document` 被拒
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation,extra_args",
    [
        ("search_library", {"params": {"meeting_scope": "institution", "sort": "newest", "limit": 5}}),
        ("get_document", {"params": {"doc_id": "290001790954279_inst_134249", "content_field": "original_text", "offset": 0, "max_chars": 30000}}),
        ("get_document_info", {"params": {"doc_id": "290001790954279_inst_134249"}}),
        ("list_attachments", {"params": {"doc_id": "290001790954279_inst_134249"}}),
        ("list_recording_tasks", {"params": {"meeting_scope": "institution", "page": 1, "limit": 100}}),
        ("get_recording_status", {"params": {"meeting_scope": "institution"}}),
        ("get_recording_task", {"params": {"roadshow_id": "134249"}}),
        ("list_filters", {"params": {"filter_type": "document_types", "document_code": "summary"}}),
        ("auth_status", {}),
    ],
)
def test_nine_alphaengine_operations_pass_validation(operation: str, extra_args: dict[str, Any]) -> None:
    raw = {"route": "alphaengine", "operation": operation, **extra_args}
    norm, _view, err = rb._normalize_and_validate_call_args("research", raw)
    assert err is None, f"Operation {operation} unexpectedly rejected: {err}"
    assert norm["operation"] == operation


def test_download_document_is_rejected_by_bridge() -> None:
    _norm, _view, err = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "download_document",
            "params": {"doc_id": "290001790954279_inst_134249"},
        },
    )
    assert err is not None
    assert err["error_code"] == "invalid_operation"
    assert "download_document" not in err["accepted_operations"]
    assert len(err["accepted_operations"]) == 9


# ---------------------------------------------------------------------------
# 2. 每个操作的嵌套参数白名单：合法参数通过；companies / industries / pages /
#    search_library 上的 page 被拒，且错误信息里有对应的新参数名
# ---------------------------------------------------------------------------


def test_per_operation_nested_params_whitelist_accepts_valid_and_rejects_cross_op() -> None:
    # Full valid nested params on search_library
    _n, _v, err = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "宁德时代",
            "params": {
                "query": "宁德时代",
                "search_in": "title",
                "document_categories": [
                    "institution_meetings",
                    "personal_meetings",
                    "third_party_reports",
                    "expert_library",
                    "global_think_tank",
                ],
                "document_codes": ["summary"],
                "meeting_scope": "institution",
                "type_ids": ["IND_SECOND_5FA442AE"],
                "markets": ["HK"],
                "company_names": ["宁德时代"],
                "company_short_names": ["CATL"],
                "industry_names": ["电力设备"],
                "source_names": ["Morgan Stanley"],
                "source_ids": ["3001"],
                "first_regions": ["CN"],
                "second_regions": ["GD"],
                "stock_group_ids": ["g1"],
                "own_reservations_only": False,
                "optional_only": False,
                "date_from": "2026-10-01",
                "date_to": "2026-10-20",
                "sort": "relevance",
                "limit": 20,
                "cursor": "cur_abc",
                "include_snippets": True,
            },
        },
    )
    assert err is None

    # Cross-operation parameter: roadshow_id on get_document is rejected
    _n, _v, err_cross = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "get_document",
            "params": {"doc_id": "290001790954279_inst_134249", "roadshow_id": "134249"},
        },
    )
    assert err_cross is not None
    assert err_cross["error_code"] == "unknown_parameters"
    assert "roadshow_id" in err_cross["error"]
    assert "roadshow_id" not in err_cross["accepted_nested_parameters"]

    # auth_status takes no nested params
    _n, _v, err_auth = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "auth_status",
            "params": {"doc_id": "290001790954279_inst_134249"},
        },
    )
    assert err_auth is not None
    assert err_auth["error_code"] == "unknown_parameters"


@pytest.mark.parametrize(
    "operation,bad_params,expected_new_param",
    [
        ("search_library", {"companies": ["宁德时代"]}, "company_names"),
        ("search_library", {"industries": ["半导体"]}, "industry_names"),
        ("search_library", {"pages": 2}, "cursor"),
        ("search_library", {"page": 1}, "cursor"),
        ("get_recording_task", {"doc_id": "290001790954279_inst_134249"}, "roadshow_id"),
    ],
)
def test_deprecated_or_misplaced_params_rejected_with_replacement_hint(
    operation: str, bad_params: dict[str, Any], expected_new_param: str
) -> None:
    _norm, _view, err = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": operation,
            "params": bad_params,
        },
    )
    assert err is not None
    assert expected_new_param in err["error"] or expected_new_param in err.get("hint", "")


# ---------------------------------------------------------------------------
# 3. 排序：有关键词默认 relevance、`newest` 被拒；无关键词默认 newest、
#    显式 relevance 通过；`params.query` 同样算关键词
# ---------------------------------------------------------------------------


def test_alphaengine_search_library_sort_rules() -> None:
    # 1. With top-level query: defaults to relevance
    norm_q, _v, err_q = rb._normalize_and_validate_call_args(
        "research",
        {"route": "alphaengine", "operation": "search_library", "query": "NVDA"},
    )
    assert err_q is None
    assert norm_q["params"]["sort"] == "relevance"

    # 2. With top-level query and sort="newest": rejected with E_QUERY_SORT
    _n, _v, err_q_newest = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "NVDA",
            "params": {"sort": "newest"},
        },
    )
    assert err_q_newest is not None
    assert "E_QUERY_SORT" in err_q_newest["error"]
    assert "relevance" in err_q_newest["error"]

    # 3. With params.query and sort="newest": params.query counts as keyword -> rejected
    _n, _v, err_pq_newest = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "search_library",
            "params": {"query": "NVDA", "sort": "newest"},
        },
    )
    assert err_pq_newest is not None
    assert "E_QUERY_SORT" in err_pq_newest["error"]

    # 4. With params.query and no sort: defaults to relevance
    norm_pq, _v, err_pq = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "search_library",
            "params": {"query": "NVDA"},
        },
    )
    assert err_pq is None
    assert norm_pq["params"]["sort"] == "relevance"

    # 5. Without query (browse mode): defaults to newest
    norm_no_q, _v, err_no_q = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "search_library",
            "params": {"meeting_scope": "institution"},
        },
    )
    assert err_no_q is None
    assert norm_no_q["params"]["sort"] == "newest"

    # 6. Without query and explicit sort="relevance": allowed
    norm_no_q_rel, _v, err_no_q_rel = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "search_library",
            "params": {"meeting_scope": "institution", "sort": "relevance"},
        },
    )
    assert err_no_q_rel is None
    assert norm_no_q_rel["params"]["sort"] == "relevance"


# ---------------------------------------------------------------------------
# 4. 文档 ID：`290001790954279_inst_134249` 通过且原样传给上游；
#    `alphaengine:290001790954279_inst_134249` 剥一次前缀；
#    `../x`、`a/b`、`a.b`、`a%2F`、带空格的被拒
# ---------------------------------------------------------------------------


def test_alphaengine_doc_id_validation_and_prefix_stripping() -> None:
    # Exact string ID with _inst_ preserved
    norm1, _v, err1 = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "get_document",
            "params": {"doc_id": "290001790954279_inst_134249"},
        },
    )
    assert err1 is None
    assert norm1["params"]["doc_id"] == "290001790954279_inst_134249"

    # Strip one `alphaengine:` prefix and preserve `_inst_`
    norm2, _v, err2 = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "get_document",
            "document": "alphaengine:290001790954279_inst_134249",
        },
    )
    assert err2 is None
    assert norm2["params"]["doc_id"] == "290001790954279_inst_134249"

    # Matching top-level document (with prefix) and nested doc_id (without prefix) do not conflict
    norm3, _v, err3 = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "get_document_info",
            "document": "alphaengine:290001790954279_inst_134249",
            "params": {"doc_id": "290001790954279_inst_134249"},
        },
    )
    assert err3 is None
    assert norm3["params"]["doc_id"] == "290001790954279_inst_134249"


@pytest.mark.parametrize(
    "bad_doc_id",
    [
        "../x",
        "a/b",
        "a.b",
        "a%2F",
        "290001790954279 inst 134249",
        " 290001790954279_inst_134249",
        "290001790954279_inst_134249 ",
        "290001790954279_inst_134249\n",
        "alphaengine: 290001790954279_inst_134249",
        "alphaengine:alphaengine:290001790954279_inst_134249",
        "_leading_underscore",
    ],
)
def test_alphaengine_doc_id_rejects_unsafe_strings(bad_doc_id: str) -> None:
    _norm, _view, err = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "get_document",
            "params": {"doc_id": bad_doc_id},
        },
    )
    assert err is not None
    assert err["error_code"] == "invalid_doc_id"


@pytest.mark.parametrize(
    "valid_cf",
    [
        "auto",
        "original_text",
        "content",
        "digest",
        "digest_cn",
        "main_point",
        "question_answer",
    ],
)
def test_get_document_accepts_seven_valid_content_fields(valid_cf: str) -> None:
    norm, _view, err = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "get_document",
            "params": {"doc_id": "290001790954279_inst_134249", "content_field": valid_cf},
        },
    )
    assert err is None
    assert norm["params"]["content_field"] == valid_cf


@pytest.mark.parametrize(
    "invalid_cf",
    [
        "summary",
        "ai_summary",
        "qa",
        "transcript",
        "key_points",
        "polished_1",
        "translation",
        "translate_content",
        "translate_summary",
        "full_content",
        "text",
        "markdown",
    ],
)
def test_get_document_rejects_invalid_content_field_with_allowed_list(invalid_cf: str) -> None:
    _norm, _view, err = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "get_document",
            "params": {"doc_id": "290001790954279_inst_134249", "content_field": invalid_cf},
        },
    )
    assert err is not None
    assert err["error_code"] == "E_BAD_CONTENT_FIELD"
    assert err["error_category"] == "参数错误"
    assert "[参数错误]" in err["error"]
    assert "question_answer" in err["error"]
    assert err["accepted_content_fields"] == [
        "auto",
        "original_text",
        "content",
        "digest",
        "digest_cn",
        "main_point",
        "question_answer",
    ]


# ---------------------------------------------------------------------------
# 5. 录制任务日期：未来的成对日期原样进 `params`，顶层 start/end 被移除；
#    只给一边被拒；`search_library` 的日期规则不变
# ---------------------------------------------------------------------------


def test_list_recording_tasks_future_date_pair_and_single_sided_rejection() -> None:
    # Future paired dates via top-level start/end -> moved into params.date_from/date_to, top-level start/end removed
    norm, _view, err = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "list_recording_tasks",
            "start": "2099-05-01",
            "end": "2099-05-10",
            "params": {"meeting_scope": "institution", "page": 1, "limit": 50},
        },
    )
    assert err is None
    assert "start" not in norm
    assert "end" not in norm
    assert norm["params"]["date_from"] == "2099-05-01"
    assert norm["params"]["date_to"] == "2099-05-10"

    # Single-sided date on list_recording_tasks -> rejected (no auto-fill to today)
    _n1, _v1, err_one_side = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "list_recording_tasks",
            "params": {"date_from": "2099-05-01"},
        },
    )
    assert err_one_side is not None
    assert err_one_side["error_code"] == "E_BAD_DATE_RANGE"

    # search_library historical date rules remain unchanged: lone start auto-fills end to today, future end truncated
    norm_hist, _vh, err_hist = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "alphaengine",
            "operation": "search_library",
            "query": "NVDA",
            "start": "2026-01-01",
            "end": "2099-12-31",
        },
    )
    assert err_hist is None
    assert norm_hist["start"] == "2026-01-01"
    assert norm_hist["end"] == rb._bridge_today()


# ---------------------------------------------------------------------------
# 6. `route=all` 的 AlphaEngine 腿：`payload.alphaengine` 含 `cursor` / `has_more` /
#    `total_upstream` / `filters` 和下一页切换提示；`limit>50` 截到 50 并注明
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_all_alphaengine_leg_preserves_pagination_and_clamps_limit_50(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    seen_ae_args: list[dict[str, Any]] = []

    async def _rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        args = req["params"]["arguments"]
        route = args.get("route")
        if route == "all":
            return [
                _mcp_tool_response(
                    req["id"],
                    {"ok": True, "status": "ok", "results": [], "result_count": 0},
                )
            ]
        if route == "guidepoint":
            return [
                _mcp_tool_response(
                    req["id"],
                    {"ok": True, "status": "ok", "data": {"structuredContent": {"data": []}}},
                )
            ]
        seen_ae_args.append(dict(args))
        return [
            _mcp_tool_response(
                req["id"],
                {
                    "ok": True,
                    "status": "ok",
                    "data": {
                        "structuredContent": {
                            "query": "NVDA",
                            "total_upstream": 128,
                            "returned": 1,
                            "has_more": True,
                            "cursor": "eyJwYWdlIjoyfQ==",
                            "filters": {"meeting_scope": "institution"},
                            "results": [
                                {
                                    "doc_id": "290001790954279_inst_134249",
                                    "title": "NVDA Blackwell Supply Chain Call",
                                    "publish_time": "2026-10-20",
                                    "document_code": "summary",
                                }
                            ],
                        }
                    },
                },
            )
        ]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    resp = await bridge.handle_tools_call(
        "req-all-ae",
        {
            "name": "research",
            "arguments": {"route": "all", "operation": "search", "query": "NVDA", "limit": 80},
        },
    )
    body = json.loads(resp["result"]["content"][0]["text"])
    assert seen_ae_args and seen_ae_args[0]["limit"] == 50

    ae_sec = body["alphaengine"]
    assert ae_sec["ok"] is True
    assert ae_sec["cursor"] == "eyJwYWdlIjoyfQ=="
    assert ae_sec["has_more"] is True
    assert ae_sec["total_upstream"] == 128
    assert ae_sec["filters"] == {"meeting_scope": "institution"}
    assert 'route="alphaengine"' in ae_sec["next_page_hint"]
    assert ae_sec["limit_clamped"] == [80, 50]
    assert "50" in ae_sec["note"]


# ---------------------------------------------------------------------------
# 7. `collect.py`：cursor 翻页最多 5 页；中间空页继续翻；达到 5 页仍有
#    `has_more=true` 时标"未完成"且不触发短路
# ---------------------------------------------------------------------------


def test_collect_cursor_pagination_continues_past_empty_page_and_caps_at_5_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collect_mod = _load_collect_module()
    orig_call_bridge = collect_mod.call_bridge_jsonrpc
    meta = {
        "ticker": "META",
        "name": "Meta Platforms",
        "aliases": ["META", "Meta Platforms"],
        "sec_forms": ["8-K"],
    }

    # Scenario A: page 1 has_more=True, page 2 is empty with has_more=True, page 3 has item and has_more=False
    ae_cursors_seen: list[str | None] = []

    def _fake_bridge_3pages(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = arguments["route"]
        if route != "alphaengine":
            return {"ok": True, "status": "ok", "data": {"ok": True, "status": "ok", "results": []}}
        cur = (arguments.get("params") or {}).get("cursor")
        ae_cursors_seen.append(cur)
        if cur is None:
            return {
                "ok": True,
                "status": "ok",
                "data": {
                    "structuredContent": {
                        "results": [
                            {
                                "doc_id": "290001790954279_inst_134249",
                                "title": "Meta Call Page 1",
                                "title_cn": "Meta 会议第 1 页",
                                "publish_time": "2026-10-22",
                                "rank_date": "2026-10-22 18:00:00",
                                "type_show_name": "机构会议",
                                "institution_meeting_flag": 1,
                                "personal_meeting_flag": 0,
                                "snippet": "Page 1 snippet",
                            }
                        ],
                        "has_more": True,
                        "cursor": "cur_p2",
                        "total_upstream": 2,
                    }
                },
            }
        if cur == "cur_p2":
            # Intermediate empty page with has_more=True
            return {
                "ok": True,
                "status": "ok",
                "data": {
                    "structuredContent": {
                        "results": [],
                        "has_more": True,
                        "cursor": "cur_p3",
                        "total_upstream": 2,
                    }
                },
            }
        return {
            "ok": True,
            "status": "ok",
            "data": {
                "structuredContent": {
                    "results": [
                        {
                            "doc_id": "290001790954280_inst_134250",
                            "title": "Meta Call Page 3",
                            "publish_time": "2026-10-23",
                            "snippet": "Page 3 snippet",
                        }
                    ],
                    "has_more": False,
                    "cursor": None,
                    "total_upstream": 2,
                }
            },
        }

    bridge_double_a = create_autospec(collect_mod.call_bridge_jsonrpc, side_effect=_fake_bridge_3pages)
    monkeypatch.setattr(collect_mod, "call_bridge_jsonrpc", bridge_double_a)

    internal_a, ae_a = collect_mod.collect_internal_and_ae(
        meta=meta, since="2026-10-21", until="2026-10-24", seen=set(), skip_ae=False
    )
    assert ae_cursors_seen == [None, "cur_p2", "cur_p3"]
    assert internal_a["status"]["alphaengine"]["completed"] is True
    assert internal_a["status"]["alphaengine"]["incomplete"] is False
    assert "alphaengine:290001790954279_inst_134249" in ae_a["new"]
    assert "alphaengine:290001790954280_inst_134250" in ae_a["new"]
    saved_item = ae_a["new"]["alphaengine:290001790954279_inst_134249"]
    assert saved_item["rank_date"] == "2026-10-22 18:00:00"
    assert saved_item["title_cn"] == "Meta 会议第 1 页"
    assert saved_item["type_show_name"] == "机构会议"
    assert saved_item["institution_meeting_flag"] == 1
    assert saved_item["personal_meeting_flag"] == 0

    # Scenario B: 5 pages all return 0 new items with has_more=True -> stops at 5 pages, marks incomplete, blocks short-circuit
    ae_calls_b = 0

    def _fake_bridge_5pages_has_more(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        nonlocal ae_calls_b
        if arguments["route"] != "alphaengine":
            return {"ok": True, "status": "ok", "data": {"ok": True, "status": "ok", "results": []}}
        ae_calls_b += 1
        return {
            "ok": True,
            "status": "ok",
            "data": {
                "structuredContent": {
                    "results": [],
                    "has_more": True,
                    "cursor": f"cur_{ae_calls_b + 1}",
                    "total_upstream": 200,
                }
            },
        }

    bridge_double_b = create_autospec(orig_call_bridge, side_effect=_fake_bridge_5pages_has_more)
    monkeypatch.setattr(collect_mod, "call_bridge_jsonrpc", bridge_double_b)

    internal_b, ae_b = collect_mod.collect_internal_and_ae(
        meta=meta, since="2026-10-21", until="2026-10-24", seen=set(), skip_ae=False
    )
    assert ae_calls_b == 5
    assert internal_b["status"]["alphaengine"]["incomplete"] is True
    assert internal_b["status"]["alphaengine"]["completed"] is False
    assert internal_b["status"]["alphaengine"]["status"] == "incomplete"
    failures = collect_mod.summarize_source_failures(
        internal_b, ae_b, {"status": [], "new": {}}
    )
    assert any(f["source"] == "alphaengine" and "未完成" in f["error"] for f in failures)
    assert (
        collect_mod.can_short_circuit(
            auto_flags=[],
            internal_data=internal_b,
            ae_data=ae_b,
            sec_data={"status": [], "new": {}},
        )
        is False
    )


# ---------------------------------------------------------------------------
# 8. `source_identity.py`：`list_recording_tasks` / `get_document_info` /
#    `list_attachments` 不生成研究文档来源；`expert_summary` 映射为专家来源，
#    业绩会映射为 `company`，未知类型不默认判 `broker_report`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation,payload",
    [
        (
            "list_recording_tasks",
            {
                "ok": True,
                "data": {
                    "structuredContent": {
                        "results": [
                            {"id": "134249", "roadshow_id": "134249", "title": "机构路演", "record_status": 1}
                        ]
                    }
                },
            },
        ),
        (
            "get_document_info",
            {
                "ok": True,
                "data": {
                    "structuredContent": {
                        "doc_id": "290001790954279_inst_134249",
                        "title": "机构会议纪要",
                        "has_summary_content": True,
                    }
                },
            },
        ),
        (
            "list_attachments",
            {
                "ok": True,
                "data": {
                    "structuredContent": {
                        "doc_id": "290001790954279_inst_134249",
                        "attachment_count": 0,
                        "attachments": [],
                    }
                },
            },
        ),
        (
            "get_recording_status",
            {
                "ok": True,
                "data": {
                    "structuredContent": {
                        "doc_id": "290001790954279_inst_134249",
                        "record_status": 2,
                    }
                },
            },
        ),
        (
            "get_recording_task",
            {
                "ok": True,
                "data": {
                    "structuredContent": {
                        "roadshow_id": "134249",
                        "record_status": 2,
                    }
                },
            },
        ),
    ],
)
def test_non_document_alphaengine_operations_emit_no_declared_sources(
    operation: str, payload: dict[str, Any]
) -> None:
    declared = source_identity.declare(
        "research", {"route": "alphaengine", "operation": operation}, payload
    )
    assert declared == []


def test_alphaengine_source_identity_mapping_and_missing_publish_time() -> None:
    payload = {
        "ok": True,
        "gateway_at": "2026-10-28T08:00:00Z",
        "data": {
            "structuredContent": {
                "results": [
                    {
                        "doc_id": "290001790954279_inst_134249",
                        "title": "专家访谈：算力光模块供应链跟踪",
                        "document_code": "expert_summary",
                        "type_show_name": "专家库",
                        "publish_time": "2026-10-20",
                    },
                    {
                        "doc_id": "130000101576482",
                        "title": "Meta FY26Q3 业绩说明会",
                        "type_id": "IND_SECOND_5FA442AE",
                        "publish_time": "2026-10-25",
                    },
                    {
                        "doc_id": "99001122",
                        "title": "Morgan Stanley China Battery Note",
                        "document_code": "foreignReport",
                        "sources": ["Morgan Stanley"],
                        "publish_time": "2026-10-22",
                    },
                    {
                        "doc_id": "88009900_inst_777",
                        "title": "未知自定义分类材料（无发布日期）",
                        "document_code": "unrecognized_custom_code",
                        "type_show_name": "待定材料",
                    },
                ]
            }
        },
    }
    declared = source_identity.declare(
        "research",
        {"route": "alphaengine", "operation": "search_library"},
        payload,
    )
    assert len(declared) == 4
    assert declared[0]["document_id"] == "alphaengine:290001790954279_inst_134249"
    assert declared[0]["identity"] == "expert"
    assert declared[0]["tier"] == 5

    assert declared[1]["identity"] == "company"
    assert declared[1]["tier"] == 2

    assert declared[2]["identity"] == "broker_report"
    assert declared[2]["tier"] == 3

    # Unknown type stays unknown (not broker_report), and missing publish_time does NOT use gateway_at
    assert declared[3]["identity"] == "unknown"
    assert declared[3]["identity"] != "broker_report"
    assert declared[3]["tier"] is None
    assert declared[3]["as_of"] == ""
    assert declared[3]["as_of_basis"] == "unknown"


# ---------------------------------------------------------------------------
# 9. 用量账本：内层 `isError=true` 时 `ok=false` 且有 `error_category`；
#    `params.query` 算哈希；`count` 读自 `structuredContent`；
#    `get_document_info` / `list_attachments` 不计入重复全文阅读
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_ledger_unwraps_inner_is_error_and_distinguishes_metadata_from_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    call_queue: list[dict[str, Any]] = [
        # 1. Inner isError=true on search_library with params.query
        {
            "ok": True,
            "status": "ok",
            "data": {
                "isError": True,
                "content": [{"type": "text", "text": "E_CURSOR_EXPIRED: 游标已过期"}],
            },
        },
        # 2. Successful search_library with structuredContent.returned=3
        {
            "ok": True,
            "status": "ok",
            "data": {
                "structuredContent": {
                    "returned": 3,
                    "has_more": False,
                    "results": [
                        {"doc_id": "290001790954279_inst_134249", "title": "A"},
                        {"doc_id": "290001790954280_inst_134250", "title": "B"},
                        {"doc_id": "290001790954281_inst_134251", "title": "C"},
                    ],
                }
            },
        },
        # 3. get_document_info twice on the same doc_id (metadata only, not full-text read)
        {
            "ok": True,
            "status": "ok",
            "data": {
                "structuredContent": {
                    "doc_id": "290001790954279_inst_134249",
                    "title": "Doc Info",
                    "has_summary_content": True,
                }
            },
        },
        # 4. list_attachments on the same doc_id (metadata only)
        {
            "ok": True,
            "status": "ok",
            "data": {
                "structuredContent": {
                    "doc_id": "290001790954279_inst_134249",
                    "attachment_count": 2,
                    "attachments": [{"name": "a.pdf"}, {"name": "b.pdf"}],
                }
            },
        },
        # 5. Single get_document on the same doc_id with content_field="original_text"
        {
            "ok": True,
            "status": "ok",
            "data": {
                "structuredContent": {
                    "doc_id": "290001790954279_inst_134249",
                    "content_field": "original_text",
                    "available_content_fields": {"content": 0, "original_text": 1200},
                    "text": "会议正文内容",
                    "returned_chars": 6,
                    "offset": 100,
                    "next_offset": None,
                    "content_sha256": "deadbeef",
                }
            },
        },
    ]

    async def _rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        payload = call_queue.pop(0)
        return [_mcp_tool_response(requests[0]["id"], payload)]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    # Call 1: inner isError=True with params.query
    await bridge.handle_tools_call(
        "c1",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "params": {"query": "宁德时代机构会议", "sort": "relevance"},
            },
        },
    )
    # Call 2: search_library ok
    await bridge.handle_tools_call(
        "c2",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "search_library",
                "params": {"query": "宁德时代机构会议", "sort": "relevance"},
            },
        },
    )
    # Call 3: get_document_info
    await bridge.handle_tools_call(
        "c3",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "get_document_info",
                "params": {"doc_id": "290001790954279_inst_134249"},
            },
        },
    )
    # Call 4: list_attachments
    await bridge.handle_tools_call(
        "c4",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "list_attachments",
                "params": {"doc_id": "290001790954279_inst_134249"},
            },
        },
    )
    # Call 5: get_document (only 1 full-text read!)
    await bridge.handle_tools_call(
        "c5",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "get_document",
                "params": {
                    "doc_id": "290001790954279_inst_134249",
                    "content_field": "original_text",
                    "offset": 100,
                },
            },
        },
    )

    ledger_path = tmp_path / "alphaengine_usage.jsonl"
    entries = usage_report.load_entries([ledger_path])
    assert len(entries) == 5

    expected_q_hash = hashlib.sha256("宁德时代机构会议".encode()).hexdigest()[:12]
    # Entry 0: inner isError=True -> ok=False + error_category="参数错误" + params.query hash
    assert entries[0]["ok"] is False
    assert entries[0]["error_category"] == "参数错误"
    assert entries[0]["query_sha256_12"] == expected_q_hash
    assert entries[0]["count"] == 0

    # Entry 1: structuredContent.returned == 3
    assert entries[1]["ok"] is True
    assert entries[1]["count"] == 3
    assert entries[1]["query_sha256_12"] == expected_q_hash

    # Entry 2 & 3: metadata operations recorded as op="meta", not "read"
    assert entries[2]["op"] == "meta"
    assert entries[2]["operation"] == "get_document_info"
    assert entries[3]["op"] == "meta"
    assert entries[3]["operation"] == "list_attachments"
    assert entries[3]["count"] == 2

    # Entry 4: get_document recorded as op="read" with content_field and offset
    assert entries[4]["op"] == "read"
    assert entries[4]["operation"] == "get_document"
    assert entries[4]["content_field"] == "original_text"
    assert entries[4]["offset"] == 100

    # Usage report: get_document_info and list_attachments do NOT count toward repeated_doc_reads
    summary = usage_report.summarize(entries)
    assert summary["repeated_doc_reads"] == {}


# ---------------------------------------------------------------------------
# 10. 内部库误路由：传 `290001790954279_inst_134249` 或
#     `alphaengine:290001790954279_inst_134249` 提示改走 `route="alphaengine"`
#     且保留完整 ID；传 `tmtb:290001790954279_inst_134249` 正常走本地读取
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_library_misroute_detects_inst_id_and_allows_tmtb_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    forwarded_docs: list[str] = []

    async def _rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        args = req["params"]["arguments"]
        forwarded_docs.append(str(args.get("document") or ""))
        return [
            _mcp_tool_response(
                req["id"],
                {
                    "ok": True,
                    "status": "ok",
                    "document_id": args.get("document"),
                    "content": "Local TMTB content",
                },
            )
        ]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    for misrouted_doc in (
        "290001790954279_inst_134249",
        "alphaengine:290001790954279_inst_134249",
    ):
        resp = await bridge.handle_tools_call(
            "misroute-inst",
            {
                "name": "research",
                "arguments": {
                    "route": "tmtb",
                    "operation": "read",
                    "document": misrouted_doc,
                },
            },
        )
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert 'route="alphaengine"' in body["hint"]
        assert "290001790954279_inst_134249" in body["hint"]
        assert body["doc_id"] == "290001790954279_inst_134249"
    assert forwarded_docs == []

    # Valid local TMTB document with _inst_ suffix passes through to local library read
    resp_local = await bridge.handle_tools_call(
        "valid-tmtb-inst",
        {
            "name": "research",
            "arguments": {
                "route": "tmtb",
                "operation": "read",
                "document": "tmtb:290001790954279_inst_134249",
            },
        },
    )
    assert resp_local["result"]["isError"] is False
    body_local = json.loads(resp_local["result"]["content"][0]["text"])
    assert body_local["ok"] is True
    assert forwarded_docs == ["tmtb:290001790954279_inst_134249"]


# ---------------------------------------------------------------------------
# 11. 新错误码分类与恢复提示（E_QUERY_SORT、E_SCOPE_CONFLICT、
#     E_CURSOR_FILTER_MISMATCH、E_CURSOR_EXPIRED、E_SOURCE_NOT_RESOLVED、
#     E_UPSTREAM_CURSOR/PAGE/ATTACHMENTS、E_DESKTOP_*）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_text,expected_category,expected_hint_substr",
    [
        (
            "E_QUERY_SORT: 有关键词检索只支持 sort='relevance'",
            "参数错误",
            "relevance",
        ),
        (
            "E_SCOPE_CONFLICT: meeting_scope 与 document_categories 冲突",
            "参数错误",
            "meeting_scope",
        ),
        (
            "E_CURSOR_FILTER_MISMATCH: cursor 与当前筛选条件不一致",
            "参数错误",
            "cursor",
        ),
        (
            "E_CURSOR_EXPIRED: 分页游标已过期",
            "参数错误",
            "第一页",
        ),
        (
            'E_SOURCE_NOT_RESOLVED: 无法唯一匹配来源"MS"',
            "参数错误",
            "list_filters(filter_type=sources)",
        ),
        (
            "E_UPSTREAM_CURSOR: 上游未返回下一页游标",
            "上游不可用",
            "游标",
        ),
        (
            "E_UPSTREAM_PAGE: 上游录制任务分页异常",
            "上游不可用",
            "分页",
        ),
        (
            "E_UPSTREAM_ATTACHMENTS: 附件列表解析失败",
            "上游不可用",
            "附件",
        ),
        (
            "E_UPSTREAM_RESPONSE: 上游响应结构异常",
            "上游不可用",
            "响应异常",
        ),
        (
            "E_DESKTOP_TOKEN_EXPIRED: AlphaEngine Desktop 登录态已过期",
            "权限或额度",
            "登录",
        ),
        (
            "E_DESKTOP_NO_MODULE_PAGE: 未打开研究模块页面",
            "上游不可用",
            "Desktop",
        ),
        (
            "E_DESKTOP_NOT_RUNNING: AlphaEngine Desktop 未运行",
            "上游不可用",
            "Desktop",
        ),
    ],
)
def test_new_alphaengine_error_codes_classification_and_hints(
    error_text: str, expected_category: str, expected_hint_substr: str
) -> None:
    payload = {"ok": False, "status": "error", "error": error_text}
    category, hint = rb._classify_error_category_and_hint(
        "research", "alphaengine", "search_library", payload
    )
    assert category == expected_category
    assert expected_hint_substr in hint


def test_alphaengine_source_identity_does_not_default_summary_or_third_report_or_think_tank_to_sell_side() -> None:
    payload = {
        "ok": True,
        "data": {
            "structuredContent": {
                "results": [
                    {
                        "doc_id": "290001790954279_inst_134249",
                        "title": "某公司机构调研交流纪要",
                        "document_code": "summary",
                        "type_show_name": "会议纪要",
                        "institution_meeting_flag": True,
                        "publish_time": "2026-10-20",
                    },
                    {
                        "doc_id": "290001790954280_inst_134250",
                        "title": "个人录制电话会议",
                        "document_code": "summary",
                        "type_show_name": "个人会议",
                        "personal_meeting_flag": True,
                        "publish_time": "2026-10-21",
                    },
                    {
                        "doc_id": "33001122",
                        "title": "IDC 全球云计算市场追踪",
                        "document_code": "thirdReport",
                        "type_show_name": "三方报告",
                        "publish_time": "2026-10-22",
                    },
                    {
                        "doc_id": "44001122",
                        "title": "Brookings AI Policy Note",
                        "document_code": "globalThinkTank",
                        "type_show_name": "全球智库",
                        "publish_time": "2026-10-23",
                    },
                    {
                        "doc_id": "55001122",
                        "title": "中信证券：算力深度研究报告",
                        "document_code": "summary",
                        "type_name": "券商深度研究报告",
                        "publish_time": "2026-10-24",
                    },
                ]
            }
        },
    }
    declared = source_identity.declare(
        "research",
        {"route": "alphaengine", "operation": "search_library"},
        payload,
    )
    assert len(declared) == 5
    # 1 & 2: summary + institutional/personal meeting with no sell-side subtype -> unknown
    assert declared[0]["identity"] == "unknown"
    assert declared[0]["tier"] is None
    assert declared[1]["identity"] == "unknown"
    assert declared[1]["tier"] is None
    # 3 & 4: thirdReport and globalThinkTank -> unknown (never broker_report)
    assert declared[2]["identity"] == "unknown"
    assert declared[2]["tier"] is None
    assert declared[3]["identity"] == "unknown"
    assert declared[3]["tier"] is None
    # 5: summary with explicit broker research report subtype -> broker_report
    assert declared[4]["identity"] == "broker_report"
    assert declared[4]["tier"] == 3


def test_error_classification_priority_http_429_in_e_upstream_response_and_all_per_leg() -> None:
    # 1. Non-JSON HTTP 429 (and 401/403) wrapped in E_UPSTREAM_RESPONSE -> 权限或额度
    for status_code in (401, 403, 429):
        payload_http = {
            "ok": False,
            "status": "error",
            "error": f"E_UPSTREAM_RESPONSE: HTTP {status_code}, non-JSON response",
        }
        cat, _hint = rb._classify_error_category_and_hint(
            "research", "alphaengine", "search_library", payload_http
        )
        assert cat == "权限或额度", f"HTTP {status_code} wrapped in E_UPSTREAM_RESPONSE should be 权限或额度"

    # 2. route=all where AE has E_DESKTOP_TOKEN_EXPIRED and GP has timeout -> per-leg classification
    payload_all = {
        "ok": False,
        "status": "partial_error",
        "error": (
            "部分数据源调用失败 "
            "(alphaengine: E_DESKTOP_TOKEN_EXPIRED: AlphaEngine 登录态已过期; "
            "guidepoint: guidepoint timed out after 90s)"
        ),
        "source_status": {
            "local_all": {"ok": True, "status": "ok", "count": 2},
            "alphaengine": {
                "ok": False,
                "status": "failed",
                "error": "E_DESKTOP_TOKEN_EXPIRED: AlphaEngine 登录态已过期",
                "count": 0,
            },
            "guidepoint": {
                "ok": False,
                "status": "timeout",
                "error": "guidepoint timed out after 90s",
                "count": 0,
            },
        },
    }
    rb._apply_error_and_empty_contract("research", "all", "search", payload_all, {})
    assert payload_all["error_category"] == "上游不可用"
    assert payload_all["leg_categories"] == {
        "alphaengine": "权限或额度",
        "guidepoint": "上游不可用",
    }


@pytest.mark.asyncio
async def test_usage_ledger_defaults_content_field_to_auto_and_catalog_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)

    async def _rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        return [
            _mcp_tool_response(
                requests[0]["id"],
                {
                    "ok": True,
                    "status": "ok",
                    "data": {
                        "structuredContent": {
                            "metadata": {"doc_id": "290001790954279_inst_134249"},
                            "content_field": "auto",
                            "available_content_fields": {"content": 500},
                            "content_chars": 500,
                            "content_sha256": "abc123",
                            "offset": 0,
                            "returned_chars": 500,
                            "text": "会议纪要正文",
                            "next_offset": None,
                            "complete": True,
                        }
                    },
                },
            )
        ]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    resp = await bridge.handle_tools_call(
        "c-auto",
        {
            "name": "research",
            "arguments": {
                "route": "alphaengine",
                "operation": "get_document",
                "params": {"doc_id": "290001790954279_inst_134249"},
            },
        },
    )
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["ok"] is True
    assert body["complete"] is True

    entries = usage_report.load_entries([tmp_path / "alphaengine_usage.jsonl"])
    assert len(entries) == 1
    assert entries[0]["op"] == "read"
    assert entries[0]["content_field"] == "auto"

    # Verify catalog contract specs for return_shape, categories, limit, and company/region warnings
    ae_spec = rb.ROUTE_CONTRACT_SPECS["alphaengine"]
    ret_shape = ae_spec["return_shape"]
    assert '"metadata": {"doc_id": str' in ret_shape
    assert '"complete": bool' in ret_shape
    assert "无顶层 doc_id，也无 has_more" in ret_shape

    cat_desc = ae_spec["param_formats"]["params.document_categories"]
    for valid_cat in (
        "sell_side",
        "sell_side_comment",
        "sell_side_report",
        "domestic_report",
        "foreign_report",
        "meeting_minutes",
        "institution_meetings",
        "personal_meetings",
        "third_party_reports",
        "expert_library",
        "global_think_tank",
        "announcement",
        "news",
    ):
        assert valid_cat in cat_desc
    for bad_cat in ("summaries", "earnings_calls", "domestic_reports", "overseas_reports", "announcements"):
        assert bad_cat not in cat_desc

    limit_desc = ae_spec["param_formats"]["limit"]
    assert "默认只返回 3 条" in limit_desc
    assert "search_library 1-50（默认 20）" not in limit_desc

    for filter_param in (
        "params.company_names",
        "params.company_short_names",
        "params.stock_group_ids",
        "params.first_regions",
        "params.second_regions",
    ):
        assert "不建议作为主要筛选" in ae_spec["param_formats"][filter_param]
    for ex in ae_spec["examples"]:
        assert "watchlist_" not in ex
        assert "company_names" not in ex
        assert "stock_group_ids" not in ex
