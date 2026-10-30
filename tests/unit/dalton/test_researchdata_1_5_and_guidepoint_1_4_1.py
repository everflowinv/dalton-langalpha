"""Unit tests for researchdata 1.5.0 (other routes) + Guidepoint 1.4.1 adaptation."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

from scripts.dalton import researchdata_bridge as rb
from scripts.dalton import source_identity

REPO_ROOT = Path(__file__).resolve().parents[3]
COLLECT_SCRIPT_PATH = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-thesis-delta" / "scripts" / "collect.py"
)


def _load_collect_module():
    spec = importlib.util.spec_from_file_location("dalton_thesis_delta_collect_15", COLLECT_SCRIPT_PATH)
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


def _mcp_tool_response(
    req_id: Any, payload: dict[str, Any], *, is_error: bool = False
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "structuredContent": dict(payload),
            "isError": is_error,
        },
    }


# ===========================================================================
# Part A: Guidepoint 1.4.1
# ===========================================================================


@pytest.mark.parametrize("raw_rb", [30, "30", " 1 ", 100])
def test_guidepoint_recency_bias_valid_normalized_to_int_on_both_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_rb: Any
) -> None:
    expected_int = int(str(raw_rb).strip())
    norm_gp, _view_gp, err_gp = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "guidepoint",
            "operation": "search_library",
            "query": "AI server liquid cooling",
            "params": {"recency_bias": raw_rb},
        },
    )
    assert err_gp is None
    assert norm_gp["params"]["recency_bias"] == expected_int
    assert type(norm_gp["params"]["recency_bias"]) is int

    norm_all, _view_all, err_all = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "all",
            "operation": "search",
            "query": "AI server liquid cooling",
            "params": {"recency_bias": raw_rb},
        },
    )
    assert err_all is None
    assert norm_all["params"]["recency_bias"] == expected_int
    assert type(norm_all["params"]["recency_bias"]) is int

    bridge = _make_bridge(tmp_path, monkeypatch)
    gp_leg_args, gp_win_err = bridge._build_guidepoint_fanout_args(
        {
            "route": "all",
            "operation": "search",
            "query": "AI server liquid cooling",
            "params": {"recency_bias": raw_rb},
        }
    )
    assert gp_win_err is None
    assert gp_leg_args["params"]["recency_bias"] == expected_int
    assert type(gp_leg_args["params"]["recency_bias"]) is int


@pytest.mark.parametrize("bad_rb", [0, 101, -5, 3.5, True, False, "abc", "1y", [30]])
def test_guidepoint_recency_bias_invalid_rejected_on_both_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_rb: Any
) -> None:
    for route in ("guidepoint", "all"):
        _norm, _view, err = rb._normalize_and_validate_call_args(
            "research",
            {
                "route": route,
                "query": "AI server liquid cooling",
                "params": {"recency_bias": bad_rb},
            },
        )
        assert err is not None, f"route={route} should reject recency_bias={bad_rb!r}"
        assert err["error_category"] == "参数错误"
        assert "1–100" in err["error"]

    bridge = _make_bridge(tmp_path, monkeypatch)
    _gp_args, gp_err = bridge._build_guidepoint_fanout_args(
        {
            "route": "all",
            "operation": "search",
            "query": "AI server liquid cooling",
            "params": {"recency_bias": bad_rb},
        }
    )
    assert gp_err is not None
    assert gp_err["error_category"] == "参数错误"
    assert "1–100" in gp_err["error"]


@pytest.mark.parametrize("empty_rb", [None, "", "   "])
def test_guidepoint_recency_bias_none_or_empty_omitted_on_both_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty_rb: Any
) -> None:
    for route in ("guidepoint", "all"):
        norm, _view, err = rb._normalize_and_validate_call_args(
            "research",
            {
                "route": route,
                "query": "AI server liquid cooling",
                "params": {"recency_bias": empty_rb},
            },
        )
        assert err is None
        assert "recency_bias" not in (norm.get("params") or {})

    bridge = _make_bridge(tmp_path, monkeypatch)
    gp_leg_args, gp_err = bridge._build_guidepoint_fanout_args(
        {
            "route": "all",
            "operation": "search",
            "query": "AI server liquid cooling",
            "params": {"recency_bias": empty_rb},
        }
    )
    assert gp_err is None
    assert "recency_bias" not in (gp_leg_args.get("params") or {})


def test_guidepoint_keywords_validation_and_normalization_on_both_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)

    # 1. Valid list of strings and single string
    for raw_kw, expected_kw in (
        (["NVDA", "Nvidia"], ["NVDA", "Nvidia"]),
        ("NVDA", ["NVDA"]),
        ([" NVDA ", "  ", "Nvidia"], ["NVDA", "Nvidia"]),
    ):
        for route in ("guidepoint", "all"):
            norm, _view, err = rb._normalize_and_validate_call_args(
                "research",
                {"route": route, "query": "GPU demand", "params": {"keywords": raw_kw}},
            )
            assert err is None
            assert norm["params"]["keywords"] == expected_kw

        gp_leg_args, gp_err = bridge._build_guidepoint_fanout_args(
            {"route": "all", "operation": "search", "query": "GPU demand", "params": {"keywords": raw_kw}}
        )
        assert gp_err is None
        assert gp_leg_args["params"]["keywords"] == expected_kw

    # 2. Empty list, empty string, whitespace-only list -> omitted
    for empty_kw in ([], [""], ["  ", "\t"], "", "   ", None):
        for route in ("guidepoint", "all"):
            norm, _view, err = rb._normalize_and_validate_call_args(
                "research",
                {"route": route, "query": "GPU demand", "params": {"keywords": empty_kw}},
            )
            assert err is None
            assert "keywords" not in (norm.get("params") or {})

        gp_leg_args, gp_err = bridge._build_guidepoint_fanout_args(
            {"route": "all", "operation": "search", "query": "GPU demand", "params": {"keywords": empty_kw}}
        )
        assert gp_err is None
        assert "keywords" not in (gp_leg_args.get("params") or {})

    # 3. >10 items or non-string items -> rejected with [参数错误] (never silently truncated)
    eleven_kw = [f"kw_{i}" for i in range(11)]
    for bad_kw in (eleven_kw, [123], ["NVDA", 456], 123, {"a": "b"}):
        for route in ("guidepoint", "all"):
            _norm, _view, err = rb._normalize_and_validate_call_args(
                "research",
                {"route": route, "query": "GPU demand", "params": {"keywords": bad_kw}},
            )
            assert err is not None
            assert err["error_category"] == "参数错误"
            assert "1–10" in err["error"]

        _gp_args, gp_err = bridge._build_guidepoint_fanout_args(
            {"route": "all", "operation": "search", "query": "GPU demand", "params": {"keywords": bad_kw}}
        )
        assert gp_err is not None
        assert gp_err["error_category"] == "参数错误"
        assert "1–10" in gp_err["error"]


@pytest.mark.asyncio
async def test_guidepoint_query_length_over_1000_rejected_after_ticker_fold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Direct route="guidepoint" with >1000 chars (direct or after ticker fold) -> [参数错误]
    _norm, _view, err_direct = rb._normalize_and_validate_call_args(
        "research",
        {"route": "guidepoint", "query": "x" * 1001},
    )
    assert err_direct is not None
    assert err_direct["error_category"] == "参数错误"
    assert "1–1000" in err_direct["error"]

    _norm_fold, _view_fold, err_direct_fold = rb._normalize_and_validate_call_args(
        "research",
        {"route": "guidepoint", "ticker": "NVDA", "query": "q" * 998},
    )
    assert err_direct_fold is not None
    assert err_direct_fold["error_category"] == "参数错误"
    assert "1–1000" in err_direct_fold["error"]

    # 2. route="all" where query is 998 chars + ticker "NVDA" -> folded query is 1003 chars > 1000:
    #    Whole call is NOT rejected; only the Guidepoint leg is skipped ("status": "skipped"),
    #    while local 4-library and AlphaEngine execute normally.
    norm_all, _view_all, err_all = rb._normalize_and_validate_call_args(
        "research",
        {"route": "all", "operation": "search", "ticker": "NVDA", "query": "q" * 998},
    )
    assert err_all is None
    assert len(norm_all["query"]) == 1003

    bridge = _make_bridge(tmp_path, monkeypatch)
    gp_args, gp_err = bridge._build_guidepoint_fanout_args(
        {"route": "all", "operation": "search", "ticker": "NVDA", "query": "q" * 998}
    )
    assert gp_err is None
    assert len(gp_args["query"]) == 1003

    recorded_routes: list[str] = []

    async def _fake_stdio_rpc(
        reqs: list[dict[str, Any]], timeout_s: float = 30.0
    ) -> list[dict[str, Any]]:
        req = reqs[0]
        args = req["params"]["arguments"]
        r_name = args.get("route")
        recorded_routes.append(r_name)
        if r_name == "all":
            payload = {
                "ok": True,
                "status": "ok",
                "route": "all",
                "results": [
                    {
                        "document_id": "wiki:nvda-1",
                        "source": "company-wiki",
                        "title": "NVDA Wiki",
                        "published_at": "2026-10-20",
                    }
                ],
                "result_count": 1,
            }
        elif r_name == "alphaengine":
            payload = {
                "ok": True,
                "status": "ok",
                "route": "alphaengine",
                "results": [
                    {
                        "doc_id": "ae-nvda-1",
                        "title": "NVDA AE Doc",
                        "publish_date": "2026-10-21",
                    }
                ],
                "meta": {"cursor": None, "has_more": False, "total_upstream": 1},
            }
        else:
            raise AssertionError(f"Unexpected stdio RPC route: {r_name}")
        return [
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "structuredContent": payload,
                    "isError": False,
                },
            }
        ]

    mock_stdio = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_stdio_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", mock_stdio)

    resp = await bridge.handle_tools_call(
        99,
        {
            "name": "research",
            "arguments": {"route": "all", "operation": "search", "ticker": "NVDA", "query": "q" * 998},
        },
    )
    sc = resp["result"]["structuredContent"]
    assert sc["ok"] is True
    assert resp["result"]["isError"] is False
    assert set(recorded_routes) == {"all", "alphaengine"}
    assert "guidepoint" not in recorded_routes
    assert sc["guidepoint"]["status"] == "skipped"
    assert sc["guidepoint"]["reason"] == "query 超过 Guidepoint 1000 字上限，本腿未执行"
    assert sc["source_status"]["guidepoint"]["status"] == "skipped"
    assert sc["source_status"]["guidepoint"]["reason"] == "query 超过 Guidepoint 1000 字上限，本腿未执行"
    assert sc["source_status"]["local_all"]["ok"] is True
    assert sc["source_status"]["alphaengine"]["ok"] is True


def test_guidepoint_validation_errors_and_mcp_validation_classified_as_param_error() -> None:
    for msg in (
        "Guidepoint keywords 须为 1–10 个非空字符串；无筛选时省略或传 null",
        "Guidepoint recency_bias 须为 1–100 的整数或 null",
        "Guidepoint search_library 需要 1–1000 字符的 query；keywords 不能替代 query",
        "MCP_VALIDATION_INVALID_FIELD: recency_bias must be integer between 1 and 100",
    ):
        payload: dict[str, Any] = {"ok": False, "status": "error", "error": msg}
        rb._apply_error_and_empty_contract(
            "research", "guidepoint", "search_library", payload, {"route": "guidepoint"}
        )
        assert payload["error_category"] == "参数错误"
        assert msg in payload["error"]


# ===========================================================================
# Part B.1: SEC new nested parameters & max_chars ceiling
# ===========================================================================


@pytest.mark.asyncio
async def test_sec_per_operation_nested_params_and_max_chars_up_to_500k(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. attachments accepts accession, document_type, max_chars
    norm_att, _view, err_att = rb._normalize_and_validate_call_args(
        "finance",
        {
            "route": "sec",
            "operation": "attachments",
            "ticker": "NVDA",
            "params": {
                "accession": "0001045810-26-000024",
                "document_type": "EX-99.1",
                "max_chars": 120000,
            },
        },
    )
    assert err_att is None
    assert norm_att["params"]["max_chars"] == 120000

    # 2. document and params.document_type are mutually exclusive on attachments
    _n, _v, err_excl = rb._normalize_and_validate_call_args(
        "finance",
        {
            "route": "sec",
            "operation": "attachments",
            "ticker": "NVDA",
            "document": "q1fy27pr.htm",
            "params": {"document_type": "EX-99.1"},
        },
    )
    assert err_excl is not None
    assert err_excl["error_category"] == "参数错误"
    assert "不能同时使用" in err_excl["error"] or "互斥" in err_excl["error"]

    # 3. Invalid accession format rejected
    _n, _v, err_acc = rb._normalize_and_validate_call_args(
        "finance",
        {
            "route": "sec",
            "operation": "attachments",
            "ticker": "NVDA",
            "params": {"accession": "bad-accession"},
        },
    )
    assert err_acc is not None
    assert err_acc["error_category"] == "参数错误"
    assert "10-2-6 数字" in err_acc["error"]
    assert "0001326801-26-000010" in err_acc["error"]

    # 4. financials accepts statement in all|income|balance|cash, rejects others
    for valid_stmt in ("all", "income", "balance", "cash"):
        _n, _v, err_st = rb._normalize_and_validate_call_args(
            "finance",
            {
                "route": "sec",
                "operation": "financials",
                "ticker": "META",
                "params": {"statement": valid_stmt},
            },
        )
        assert err_st is None
    _n, _v, err_bad_st = rb._normalize_and_validate_call_args(
        "finance",
        {
            "route": "sec",
            "operation": "financials",
            "ticker": "META",
            "params": {"statement": "equity"},
        },
    )
    assert err_bad_st is not None
    assert err_bad_st["error_category"] == "参数错误"

    # 5. search-text accepts window (1..500000) and max_matches (1..100), requires query
    _n, _v, err_st_ok = rb._normalize_and_validate_call_args(
        "finance",
        {
            "route": "sec",
            "operation": "search-text",
            "ticker": "META",
            "query": "Restructuring",
            "params": {"window": 2000, "max_matches": 20},
        },
    )
    assert err_st_ok is None
    _n, _v, err_mm = rb._normalize_and_validate_call_args(
        "finance",
        {
            "route": "sec",
            "operation": "search-text",
            "ticker": "META",
            "query": "Restructuring",
            "params": {"max_matches": 101},
        },
    )
    assert err_mm is not None
    assert err_mm["error_category"] == "参数错误"

    # 6. Disallowed nested param for operation (e.g. statement on attachments, accession on financials)
    _n, _v, err_wrong_op = rb._normalize_and_validate_call_args(
        "finance",
        {
            "route": "sec",
            "operation": "financials",
            "ticker": "META",
            "params": {"accession": "0001045810-26-000024"},
        },
    )
    assert err_wrong_op is not None
    assert err_wrong_op["error_code"] == "unknown_parameters"

    # 7. handle_tools_call does NOT clamp SEC max_chars=120000 down to 30000
    bridge = _make_bridge(tmp_path, monkeypatch)
    forwarded_sec_args: list[dict[str, Any]] = []

    async def _rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        forwarded_sec_args.append(req["params"]["arguments"])
        return [_mcp_tool_response(req["id"], {"ok": True, "status": "ok", "item": "7", "text": "MD&A"})]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    resp = await bridge.handle_tools_call(
        "sec-mc-120k",
        {
            "name": "finance",
            "arguments": {
                "route": "sec",
                "operation": "read-item",
                "ticker": "META",
                "item": "7",
                "params": {"max_chars": 120000},
            },
        },
    )
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["ok"] is True
    assert "params_clamped" not in body
    assert forwarded_sec_args[0]["params"]["max_chars"] == 120000


# ===========================================================================
# Part B.2: estimates params.shares_bn / params.tax
# ===========================================================================


def test_estimates_shares_bn_and_tax_validation() -> None:
    norm, _view, err = rb._normalize_and_validate_call_args(
        "finance",
        {
            "route": "estimates",
            "ticker": "META",
            "params": {"shares_bn": 2.56, "tax": 0.16},
        },
    )
    assert err is None
    assert norm["params"] == {"shares_bn": 2.56, "tax": 0.16}

    # tax=0 is allowed (0 <= tax < 1)
    _norm0, _view0, err0 = rb._normalize_and_validate_call_args(
        "finance",
        {"route": "estimates", "ticker": "META", "params": {"tax": 0}},
    )
    assert err0 is None

    for bad_params in (
        {"shares_bn": 0},
        {"shares_bn": -1.5},
        {"shares_bn": True},
        {"shares_bn": float("inf")},
        {"tax": -0.01},
        {"tax": 1.0},
        {"tax": True},
        {"tax": False},
        {"tax": "0.16"},
    ):
        _n, _v, bad_err = rb._normalize_and_validate_call_args(
            "finance",
            {"route": "estimates", "ticker": "META", "params": bad_params},
        )
        assert bad_err is not None, f"Should reject {bad_params}"
        assert bad_err["error_category"] == "参数错误"

    est_shape = rb.ROUTE_CONTRACT_SPECS["estimates"]["return_shape"]
    assert "currency" in est_shape
    assert "field_status" in est_shape
    assert "partial" in est_shape


# ===========================================================================
# Part B.3: transcript incomplete coverage (coverage.complete=False)
# ===========================================================================


def test_transcript_incomplete_coverage_not_marked_no_matching_transcripts() -> None:
    # 1. coverage.complete=False with 0 transcripts and status="no_match" / reason="alphaengine_no_match"
    payload_incomplete: dict[str, Any] = {
        "ok": True,
        "status": "no_match",
        "reason": "alphaengine_no_match",
        "transcripts": [],
        "coverage": {
            "complete": False,
            "pages_scanned": 10,
            "page_size": 50,
            "continuation_params": {
                "query": "Meta Platforms|META",
                "cursor": "ae_cursor_p11",
                "sort": "relevance",
            },
        },
        "warnings": ["已达 10 页扫描上限，尚有后续结果未扫描"],
    }
    assert rb._is_transcript_no_match_payload(payload_incomplete) is False
    rb._apply_error_and_empty_contract(
        "research",
        "transcript",
        "list",
        payload_incomplete,
        {"route": "transcript", "ticker": "META"},
    )
    assert payload_incomplete["ok"] is True
    assert payload_incomplete["status"] == "partial"
    assert "empty_reason" not in payload_incomplete
    assert payload_incomplete["coverage"]["complete"] is False
    assert payload_incomplete["coverage"]["continuation_params"]["cursor"] == "ae_cursor_p11"
    assert payload_incomplete["warnings"] == ["已达 10 页扫描上限，尚有后续结果未扫描"]
    assert "本次有界检索未命中" in payload_incomplete["hint"]
    assert "route='alphaengine', operation='search_library'" in payload_incomplete["hint"]

    # 2. coverage.complete=True with 0 transcripts -> still confirmed no-match
    payload_complete: dict[str, Any] = {
        "ok": True,
        "status": "no_match",
        "reason": "alphaengine_no_match",
        "transcripts": [],
        "coverage": {"complete": True, "pages_scanned": 2},
    }
    assert rb._is_transcript_no_match_payload(payload_complete) is True
    rb._apply_error_and_empty_contract(
        "research",
        "transcript",
        "list",
        payload_complete,
        {"route": "transcript", "ticker": "META"},
    )
    assert payload_complete["status"] == "empty"
    assert payload_complete["empty_reason"] == "no_matching_transcripts"


# ===========================================================================
# Part B.4: Internal library partial + collect.py company-wiki truncation
# ===========================================================================


def test_internal_library_partial_with_empty_results_preserves_partial_and_omits_no_matching_records() -> None:
    payload: dict[str, Any] = {
        "ok": True,
        "status": "partial",
        "results": [],
        "warnings": ["语义检索不可用，已降为关键词检索"],
        "coverage": {"scope": "local_index", "documents": 120},
        "image_status": {"available": False, "reason": "ocr_pending"},
    }
    rb._apply_error_and_empty_contract(
        "research",
        "company-wiki",
        "search",
        payload,
        {"route": "company-wiki", "operation": "search", "query": "META"},
    )
    assert payload["ok"] is True
    assert payload["status"] == "partial"
    assert "empty_reason" not in payload
    assert payload["warnings"] == ["语义检索不可用，已降为关键词检索"]
    assert payload["coverage"] == {"scope": "local_index", "documents": 120}
    assert payload["image_status"] == {"available": False, "reason": "ocr_pending"}


def test_collect_company_wiki_truncation_marks_incomplete_and_blocks_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collect_mod = _load_collect_module()

    def _fake_bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        route = arguments.get("route")
        if route == "company-wiki":
            # First 50 items already seen, but list was truncated (status="partial" + warning)
            return {
                "ok": True,
                "status": "partial",
                "data": {
                    "ok": True,
                    "status": "partial",
                    "results": [{"document_id": "wiki:1", "company": "META", "title": "Old doc"}],
                    "warnings": ["列表有更多文档，请缩小筛选或提高 limit"],
                },
            }
        return {"ok": True, "status": "ok", "data": {"ok": True, "status": "ok", "results": []}}

    bridge_double = create_autospec(collect_mod.call_bridge_jsonrpc, side_effect=_fake_bridge)
    monkeypatch.setattr(collect_mod, "call_bridge_jsonrpc", bridge_double)

    meta = {"ticker": "META", "name": "Meta", "aliases": ["META"], "sec_forms": ["8-K"]}
    internal_data, ae_data = collect_mod.collect_internal_and_ae(
        meta=meta,
        since="2026-10-20",
        until="2026-10-28",
        seen={"wiki:1"},
        skip_ae=True,
    )
    assert internal_data["new"] == {}
    wiki_st = internal_data["status"]["company-wiki"]
    assert wiki_st["incomplete"] is True
    assert wiki_st["completed"] is False
    assert wiki_st["status"] == "incomplete"
    assert internal_data["status"]["guidepoint"]["scope"] == "摘录检索，非全量"

    failures = collect_mod.summarize_source_failures(
        internal_data, ae_data, {"status": [{"form": "8-K", "ok": True, "count": 0}], "new": {}}
    )
    assert any(f["source"] == "company-wiki" and "未完成" in f["error"] for f in failures)
    assert (
        collect_mod.can_short_circuit(
            auto_flags=[],
            internal_data=internal_data,
            ae_data=ae_data,
            sec_data={"status": [{"form": "8-K", "ok": True, "count": 0}], "new": {}},
        )
        is False
    )


# ===========================================================================
# Part B.5: company-wiki search requires query + route=all strips Guidepoint params
# ===========================================================================


@pytest.mark.asyncio
async def test_company_wiki_search_requires_query_and_route_all_strips_gp_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. company-wiki search without query -> rejected with hint to use operation='list'
    _n, _v, err_wiki_search = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "company-wiki",
            "operation": "search",
            "params": {"company": "META"},
        },
    )
    assert err_wiki_search is not None
    assert err_wiki_search["error_code"] == "missing_required_parameters"
    assert "query" in err_wiki_search["missing_parameters"]
    assert "operation='list'" in err_wiki_search["error"]

    # 2. company-wiki list with params.company -> allowed without query
    _n_list, _v_list, err_wiki_list = rb._normalize_and_validate_call_args(
        "research",
        {
            "route": "company-wiki",
            "operation": "list",
            "params": {"company": "META"},
        },
    )
    assert err_wiki_list is None

    # 3. route="all" strips keywords/recency_bias/start_date/end_date from local leg while keeping them on guidepoint leg
    bridge = _make_bridge(tmp_path, monkeypatch)
    recorded_by_route: dict[str, dict[str, Any]] = {}

    async def _rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        args = req["params"]["arguments"]
        recorded_by_route[args["route"]] = args
        return [_mcp_tool_response(req["id"], {"ok": True, "status": "ok", "results": []})]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    await bridge.handle_tools_call(
        "all-strip-gp",
        {
            "name": "research",
            "arguments": {
                "route": "all",
                "operation": "search",
                "query": "NVDA Blackwell rack yield",
                "params": {
                    "keywords": ["NVIDIA", "NVDA"],
                    "recency_bias": 30,
                    "start_date": "2026-01-01",
                    "end_date": "2026-10-01",
                },
            },
        },
    )
    assert "params" not in recorded_by_route["all"]
    assert recorded_by_route["guidepoint"]["params"] == {
        "keywords": ["NVIDIA", "NVDA"],
        "recency_bias": 30,
        "start_date": "2026-01-01",
        "end_date": "2026-10-01",
    }


# ===========================================================================
# Part B.6: sales-mail source declaration
# ===========================================================================


def test_sales_mail_declares_sources_and_infers_sales_notes_or_tmtb() -> None:
    payload = {
        "ok": True,
        "status": "ok",
        "results": [
            {
                "document_id": "mail:101",
                "title": "Morgan Stanley US Tech Sales Desk Note",
                "date": "2026-10-20",
                "original_source": "MS Sales",
            },
            {
                "document_id": "tmtb:29000179",
                "canonical_id": "alphaengine:29000179",
                "title": "TMTB Daily Wrap",
                "date": "2026-10-21",
            },
            {
                "document_id": "digest:2026-10-22",
                "layer": "digest",
                "title": "Daily Digest",
                "date": "2026-10-22",
            },
        ],
    }
    declared = source_identity.declare(
        "research",
        {"route": "sales-mail", "operation": "search", "query": "NVDA"},
        payload,
    )
    assert len(declared) == 3
    assert declared[0]["route"] == "sales-notes"
    assert declared[0]["document_id"] == "mail:101"
    assert declared[0]["identity"] == "broker_report"
    assert declared[1]["route"] == "tmtb"
    assert declared[1]["document_id"] == "alphaengine:29000179"
    assert declared[2]["route"] == "sales-notes"
    assert declared[2]["identity"] == "derived"


# ===========================================================================
# Part B.7: Gateway validation error classification vs auth errors
# ===========================================================================


@pytest.mark.parametrize(
    "route,error_text,expected_cat",
    [
        ("sec", "SEC attachments 的 document 与 document_type 不能同时使用", "参数错误"),
        ("sec", "accession 须为 SEC 完整编号 XXXXXXXXXX-XX-XXXXXX", "参数错误"),
        ("sec", "statement 须为 all|income|balance|cash", "参数错误"),
        ("sec", "需要 --query", "参数错误"),
        ("cn-hk", "cn-hk ask 的公司和日期请写入 query；结构化 ticker/start/end 用 filings，当前报价用 quote+ticker", "参数错误"),
        ("cn-hk", "cn-hk quote 只接受 ticker；当前报价不接受历史日期", "参数错误"),
        ("estimates", "shares_bn 须为正数，tax 须为 0≤税率<1", "参数错误"),
        ("company-wiki", "company-wiki search 需要 query", "参数错误"),
        ("company-wiki", "read 只接受 document/offset/max_chars，不接受搜索或列表过滤参数", "参数错误"),
        ("all", "未知内部资料参数: ['keywords']", "参数错误"),
        # Auth errors must stay "权限或额度", never misclassified as "参数错误"
        ("guidepoint", "Guidepoint 需要登录", "权限或额度"),
        ("xueqiu", "雪球用户未登录或 cookie 缺失或已过期", "权限或额度"),
        ("alphaengine", "E_DESKTOP_TOKEN_EXPIRED: 需要重新登录 AlphaEngine Desktop", "权限或额度"),
    ],
)
def test_gateway_validation_errors_vs_auth_errors_classification(
    route: str, error_text: str, expected_cat: str
) -> None:
    tool_name = (
        "finance"
        if route in ("sec", "cn-hk", "estimates")
        else ("community" if route == "xueqiu" else "research")
    )
    cat, _hint = rb._classify_error_category_and_hint(
        tool_name, route, "", {"ok": False, "status": "error", "error": error_text}
    )
    assert cat == expected_cat


# ===========================================================================
# Part B.8: estimates completeness & currency in collect.py
# ===========================================================================


def test_collect_estimates_completeness_currency_and_zero_eps() -> None:
    collect_mod = _load_collect_module()
    thr = collect_mod.load_thresholds(Path("."))

    # 1. All-empty estimates -> status="error", ok=False -> blocks short-circuit
    empty_cons = collect_mod._compute_consensus_status(
        {
            "ticker": "META",
            "fetched_at": "2026-10-28T21:00:00-04:00",
            "earnings_estimate": {},
            "revenue_estimate": {},
            "eps_trend": {},
            "eps_revisions": {},
            "price_targets": {},
            "price": {},
        }
    )
    assert empty_cons["ok"] is False
    assert empty_cons["status"] == "error"
    assert (
        collect_mod.can_short_circuit(
            auto_flags=[],
            internal_data={"status": {}, "new": {}},
            ae_data={"status": [], "new": {}},
            sec_data={"status": [], "new": {}},
            consensus_data=empty_cons,
        )
        is False
    )

    # 2. Valid original-currency (CNY) partial estimates (implied_model skipped USD, or price_targets empty):
    #    - preserves currency="CNY"
    #    - computes zero EPS (0.0) and within-snapshot deltas without discarding valid CNY estimates
    #    - still blocks zero-new short-circuit because status="partial"
    cny_partial_cons = collect_mod._compute_consensus_status(
        {
            "ticker": "BABA",
            "fetched_at": "2026-10-28T21:00:00-04:00",
            "earnings_estimate": {
                "0y": {"avg": 0.0, "low": -1.0, "high": 1.0, "numberOfAnalysts": 15, "currency": "CNY", "period_end": "2027-03-31"},
                "+1y": {"avg": 65.0, "low": 60.0, "high": 70.0, "numberOfAnalysts": 20, "currency": "CNY", "period_end": "2028-03-31"},
            },
            "revenue_estimate": {
                "0y": {"avg": 1000e9, "currency": "CNY", "period_end": "2027-03-31"},
                "+1y": {"avg": 1100e9, "currency": "CNY", "period_end": "2028-03-31"},
            },
            "eps_trend": {
                "0y": {"current": 0.0, "7daysAgo": 0.5},
                "+1y": {"current": 65.0, "7daysAgo": 64.0},
            },
            "eps_revisions": {
                "+1y": {"upLast7days": 3, "downLast7Days": 1},
            },
            "price_targets": {"mean": 120.0, "median": 118.0, "low": 95.0, "high": 150.0},
            "price": {"as_of": "2026-10-28", "close": 100.0, "chg_1d_pct": 0.5, "chg_5d_pct": 1.2},
            "implied_model": {
                "warnings": ["+1y EPS/收入币种为 ['CNY', 'CNY']，未确认同为 USD，跳过美元隐含模型"]
            },
        }
    )
    assert cny_partial_cons["ok"] is True
    assert cny_partial_cons["status"] == "partial"
    assert cny_partial_cons["currency"] == "CNY"

    # 跨快照比较自 R3b 起先按真实期间（period_end）对齐再计算。
    prev_cny_cons = {
        "ticker": "BABA",
        "fetched_at": "2026-10-27T21:00:00-04:00",
        "currency": "CNY",
        "earnings_estimate": {
            "0y": {"avg": 0.5, "currency": "CNY", "period_end": "2027-03-31"},
            "+1y": {"avg": 64.0, "currency": "CNY", "period_end": "2028-03-31"},
        },
        "revenue_estimate": {
            "+1y": {"avg": 1095e9, "currency": "CNY", "period_end": "2028-03-31"},
        },
        "price_targets": {"mean": 119.5},
    }
    deltas, _flags = collect_mod.compute_consensus_deltas(cny_partial_cons, prev_cny_cons, thr)
    # 0.0 EPS vs 0.5 is -100.0%, NOT None!
    assert deltas["eps_0y_vs_last_pct"] == -100.0
    assert deltas["eps_0y_vs_7d_pct"] == -100.0
    assert deltas["eps_+1y_vs_last_pct"] == 1.56

    # Cross-snapshot currency mismatch (CNY vs USD) on the SAME real period is guarded
    prev_usd_cons = {
        "ticker": "BABA",
        "fetched_at": "2026-10-27T21:00:00-04:00",
        "currency": "USD",
        "earnings_estimate": {"+1y": {"avg": 9.0, "currency": "USD", "period_end": "2028-03-31"}},
        "revenue_estimate": {"+1y": {"avg": 150e9, "currency": "USD", "period_end": "2028-03-31"}},
    }
    mismatch_deltas, _ = collect_mod.compute_consensus_deltas(cny_partial_cons, prev_usd_cons, thr)
    assert mismatch_deltas["currency_mismatch"] is True
    assert mismatch_deltas["eps_+1y_vs_last_pct"] is None
    assert mismatch_deltas["rev_+1y_vs_last_pct"] is None

    # Partial consensus is reported in summarize_source_failures and blocks short-circuit
    failures = collect_mod.summarize_source_failures(
        {"status": {}, "new": {}},
        {"status": [], "new": {}},
        {"status": [], "new": {}},
        cny_partial_cons,
    )
    assert any(f["source"] == "estimates" and "partial" in f["error"] for f in failures)
    assert (
        collect_mod.can_short_circuit(
            auto_flags=[],
            internal_data={"status": {}, "new": {}},
            ae_data={"status": [], "new": {}},
            sec_data={"status": [], "new": {}},
            consensus_data=cny_partial_cons,
        )
        is False
    )

    # Complete USD consensus allows short-circuit when all other sources are ok & 0 new
    usd_complete_cons = collect_mod._compute_consensus_status(
        {
            "ticker": "META",
            "fetched_at": "2026-10-28T21:00:00-04:00",
            "earnings_estimate": {"+1y": {"avg": 30.0, "currency": "USD"}},
            "revenue_estimate": {"+1y": {"avg": 200e9, "currency": "USD"}},
            "eps_trend": {"+1y": {"current": 30.0, "7daysAgo": 29.9}},
            "eps_revisions": {"+1y": {"upLast7days": 2, "downLast7Days": 0}},
            "price_targets": {"mean": 620.0},
            "price": {"as_of": "2026-10-28", "close": 590.0},
        }
    )
    assert usd_complete_cons["ok"] is True
    assert usd_complete_cons["status"] == "ok"
    assert usd_complete_cons["currency"] == "USD"
    assert (
        collect_mod.can_short_circuit(
            auto_flags=[],
            internal_data={"status": {}, "new": {}},
            ae_data={"status": [], "new": {}},
            sec_data={"status": [], "new": {}},
            consensus_data=usd_complete_cons,
        )
        is True
    )


# ===========================================================================
# Round 2 Tests (Items 1–5)
# ===========================================================================


@pytest.mark.parametrize("bad_stmt", [["income"], {}, 123, True, False, ""])
def test_sec_statement_and_nested_params_wrong_types_return_param_error_without_exception(
    bad_stmt: Any,
) -> None:
    # 1. financials statement with non-string / unhashable types (list, dict, int, bool, "")
    _n, _v, err_stmt = rb._normalize_and_validate_call_args(
        "finance",
        {
            "route": "sec",
            "operation": "financials",
            "ticker": "META",
            "params": {"statement": bad_stmt},
        },
    )
    assert err_stmt is not None
    assert err_stmt["error_category"] == "参数错误"
    assert "[参数错误]" in err_stmt["error"]

    # 2. attachments accession & document_type with non-string / unhashable types
    for field in ("accession", "document_type"):
        _n, _v, err_att = rb._normalize_and_validate_call_args(
            "finance",
            {
                "route": "sec",
                "operation": "attachments",
                "ticker": "META",
                "params": {field: bad_stmt},
            },
        )
        assert err_att is not None
        assert err_att["error_category"] == "参数错误"
        assert "[参数错误]" in err_att["error"]

    # 3. attachments with document="x.htm" + non-string params.document_type must not raise AttributeError
    _n, _v, err_both = rb._normalize_and_validate_call_args(
        "finance",
        {
            "route": "sec",
            "operation": "attachments",
            "ticker": "META",
            "document": "q1fy27pr.htm",
            "params": {"document_type": bad_stmt},
        },
    )
    assert err_both is not None
    assert err_both["error_category"] == "参数错误"

    # 4. integer SEC nested params (max_chars, window, max_matches) with wrong types
    for op, extra, int_field in (
        ("attachments", {}, "max_chars"),
        ("read-item", {"item": "7"}, "max_chars"),
        ("search-text", {"query": "capex"}, "window"),
        ("search-text", {"query": "capex"}, "max_matches"),
    ):
        for bad_int in (["1000"], {}, "1000", True, False, 1.5, 0, -5):
            _n, _v, err_int = rb._normalize_and_validate_call_args(
                "finance",
                {
                    "route": "sec",
                    "operation": op,
                    "ticker": "META",
                    **extra,
                    "params": {int_field: bad_int},
                },
            )
            assert err_int is not None
            assert err_int["error_category"] == "参数错误"
            assert "[参数错误]" in err_int["error"]


def test_transcript_incomplete_coverage_with_candidates_preserved_and_records_scanned() -> None:
    # 1. Candidates present + status=partial + coverage.complete=False + records_scanned=100
    cand_items = [
        {"doc_id": "ae-tr-1", "title": "NVDA FY27Q1 Earnings Call", "publish_date": "2026-05-25"},
        {"doc_id": "ae-tr-2", "title": "NVDA GTC Fireside Chat", "publish_date": "2026-03-19"},
    ]
    payload_with_cands: dict[str, Any] = {
        "ok": True,
        "status": "partial",
        "data": {
            "ok": True,
            "source": "alphaengine",
            "query": "NVDA|NVIDIA",
            "coverage": {
                "complete": False,
                "pages_scanned": 2,
                "records_scanned": 100,
                "warnings": ["AlphaEngine 第 3 页检索超时"],
                "continuation_params": {"query": "NVDA|NVIDIA", "cursor": "cur_p3"},
            },
            "transcripts": list(cand_items),
        },
    }
    rb._apply_error_and_empty_contract(
        "research",
        "transcript",
        "list",
        payload_with_cands,
        {"route": "transcript", "operation": "list", "ticker": "NVDA"},
    )
    assert payload_with_cands["ok"] is True
    assert payload_with_cands["status"] == "partial"
    assert "empty_reason" not in payload_with_cands
    assert payload_with_cands["data"]["transcripts"] == cand_items
    assert "未命中" not in payload_with_cands["hint"]
    assert "已找到 2 篇候选" in payload_with_cands["hint"]
    assert "扫描了 100 条" in payload_with_cands["hint"]
    assert "最新一场可能不在其中" in payload_with_cands["hint"]
    assert "continuation" in payload_with_cands["hint"]

    # 2. Candidates present + records_scanned missing (even if items_examined is present) -> do not fabricate number
    payload_no_count: dict[str, Any] = {
        "ok": True,
        "status": "partial",
        "data": {
            "ok": True,
            "coverage": {
                "complete": False,
                "items_examined": 999,
                "warnings": ["partial scan"],
            },
            "transcripts": [cand_items[0]],
        },
    }
    rb._apply_error_and_empty_contract(
        "research",
        "transcript",
        "list",
        payload_no_count,
        {"route": "transcript", "operation": "list", "ticker": "NVDA"},
    )
    assert payload_no_count["data"]["transcripts"] == [cand_items[0]]
    assert "未命中" not in payload_no_count["hint"]
    assert "已找到 1 篇候选，但检索未覆盖完整期间，最新一场可能不在其中" in payload_no_count["hint"]
    assert "999" not in payload_no_count["hint"]
    assert "扫描了" not in payload_no_count["hint"]

    # 3. Zero candidates + complete=False + records_scanned=100 -> says "本次有界检索未命中" with 100
    payload_zero_cands: dict[str, Any] = {
        "ok": True,
        "status": "partial",
        "data": {
            "ok": True,
            "coverage": {
                "complete": False,
                "pages_scanned": 2,
                "records_scanned": 100,
                "warnings": ["bounded scan"],
            },
            "transcripts": [],
        },
    }
    rb._apply_error_and_empty_contract(
        "research",
        "transcript",
        "list",
        payload_zero_cands,
        {"route": "transcript", "operation": "list", "ticker": "NVDA"},
    )
    assert "本次有界检索未命中" in payload_zero_cands["hint"]
    assert "100 条" in payload_zero_cands["hint"]
    assert "empty_reason" not in payload_zero_cands


def test_guidepoint_unauthorized_and_all_gateway_value_errors_classification() -> None:
    # 1. Exact auth payload from prompt -> "权限或额度"
    auth_payload: dict[str, Any] = {
        "ok": False,
        "status": "unauthorized",
        "error": "Guidepoint 需要登录",
    }
    cat_auth, _hint_auth = rb._classify_error_category_and_hint(
        "research", "guidepoint", "search_library", auth_payload
    )
    assert cat_auth == "权限或额度"
    rb._apply_error_and_empty_contract(
        "research", "guidepoint", "search_library", auth_payload, {"route": "guidepoint"}
    )
    assert auth_payload["error_category"] == "权限或额度"
    assert auth_payload["error"].startswith("[权限或额度]")

    # Additional auth shapes (forbidden, HTTP 401/403, 授权) -> "权限或额度"
    for auth_case in (
        {"ok": False, "status": "forbidden", "error": "Guidepoint 无权访问"},
        {"ok": False, "status": "error", "error": "HTTP 401 Unauthorized: 需要登录"},
        {"ok": False, "status": "error", "error": "HTTP 403 Forbidden: 未授权"},
    ):
        c, _h = rb._classify_error_category_and_hint(
            "research", "guidepoint", "search_library", dict(auth_case)
        )
        assert c == "权限或额度"

    # 2. Every gateway ValueError fragment (verbatim upstream wording; see
    # _UPSTREAM_VALIDATION_MESSAGES below for full sentences) in _GATEWAY_VALIDATION_ERROR_SUBSTRINGS & patterns -> "参数错误"
    for phrase in rb._GATEWAY_VALIDATION_ERROR_SUBSTRINGS:
        sample_err = f"{phrase} (sample)"
        assert rb._is_known_gateway_validation_error(sample_err) is True, f"Missed substring: {phrase}"
        cat, _ = rb._classify_error_category_and_hint(
            "research", "guidepoint", "search_library", {"ok": False, "status": "error", "error": sample_err}
        )
        assert cat == "参数错误", f"Phrase {phrase!r} classified as {cat!r}"

    for pat_sample in (
        "SEC financials 不接受 document；参数不会被静默忽略",
        "SEC financials params 仅支持 ['statement']",
        "cn-hk quote 不支持 --query；参数不会被静默忽略",
        "query 与 params-json.query 冲突",
        "sec 不支持 unknown-op；可用：['list-filings', 'financials']",
        "max_chars 超出范围（1–500000）",
    ):
        assert rb._is_known_gateway_validation_error(pat_sample) is True
        cat, _ = rb._classify_error_category_and_hint(
            "finance", "sec", "financials", {"ok": False, "status": "error", "error": pat_sample}
        )
        assert cat == "参数错误"


def test_sec_search_alias_requires_ticker_and_gateway_require_phrases() -> None:
    # 1. operation="search" is alias for "search-text" and must require ticker
    _norm, _view, err_no_ticker = rb._normalize_and_validate_call_args(
        "finance",
        {"route": "sec", "operation": "search", "query": "Restructuring"},
    )
    assert err_no_ticker is not None
    assert err_no_ticker["error_category"] == "参数错误"
    assert err_no_ticker["error_code"] == "missing_required_parameters"
    assert "ticker" in err_no_ticker["error"]

    # With ticker + query, operation="search" normalizes to "search-text" and passes
    norm_ok, _view_ok, err_ok = rb._normalize_and_validate_call_args(
        "finance",
        {"route": "sec", "operation": "search", "ticker": "META", "query": "Restructuring"},
    )
    assert err_ok is None
    assert norm_ok["operation"] == "search-text"
    assert norm_ok["ticker"] == "META"

    # 2. Gateway require() phrases classified as "参数错误"
    for req_phrase in (
        "需要 --ticker",
        "需要 --query",
        "需要 --concept",
        "需要 --item",
        "需要 --series",
        "需要 --start",
        "需要 --end",
        "需要 --dataset",
    ):
        cat, _ = rb._classify_error_category_and_hint(
            "finance", "sec", "search-text", {"ok": False, "status": "error", "error": req_phrase}
        )
        assert cat == "参数错误"



# Full messages as researchdata 1.5 raises them (rd/research_library.py,
# rd/data_gateway.py, Guidepoint 1.4.1), copied verbatim. Classifying these —
# not the bridge's own table — is what proves the table matches upstream.
_UPSTREAM_VALIDATION_MESSAGES = (
    "search_mode 须为 hybrid 或 keyword",
    "media 须为 all、text 或 images",
    "layer 须为 raw、digest 或 all",
    "indexed_since 须为 YYYY-MM-DD 或 UTC ISO 时间",
    "sources 仅支持 company-wiki、sales-notes、tmtb、funda-ai",
    "read 只接受 document、offset、max_chars 和 all 的 sources",
    "邮件/TMTB 不支持 wiki 专用筛选；公司名放 query",
    "需要 --ticker",
    "需要 --filter-type",
    "cn-hk filings 不接受 query；使用 ticker/start/end",
    "cn-hk quote 只接受 ticker；当前报价不接受历史日期",
    "accession 须为 SEC 完整编号 XXXXXXXXXX-XX-XXXXXX",
    "shares_bn 须为正数，tax 须为 0≤税率<1",
    "Guidepoint recency_bias 须为 1–100 的整数或 null",
    "非 search_library 操作的参数请通过 params-json 完整传入",
    "category 类型错误",
    "form 类型错误",
    "as_of 类型错误",
    "vintage 类型错误",
    "缺少 query",
    "max_chars 超出范围",
    "缺少 route",
    "timeout 超出范围",
    "分类必须为 ['research', 'finance', 'macro', 'community', 'news']",
)


@pytest.mark.parametrize("message", _UPSTREAM_VALIDATION_MESSAGES)
def test_verbatim_upstream_validation_messages_are_parameter_errors(message: str) -> None:
    cat, _hint = rb._classify_error_category_and_hint(
        "research", "company-wiki", "search", {"ok": False, "status": "error", "error": message}
    )
    assert cat == "参数错误", f"{message!r} classified as {cat!r}"


def test_auth_failure_is_not_swallowed_by_validation_fragments() -> None:
    cat, _hint = rb._classify_error_category_and_hint(
        "research", "guidepoint", "search_library",
        {"ok": False, "status": "unauthorized", "error": "Guidepoint 需要登录"},
    )
    assert cat == "权限或额度"
