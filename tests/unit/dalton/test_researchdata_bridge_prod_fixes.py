"""Production-fix batch 2 contract tests for the researchdata bridge.

Covers the `route='all'` mixed-failure classification, SEC no-match texts,
ticker folding on query-only routes, and Guidepoint's keyword list shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DALTON = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402


def _mixed_all_failure_payload() -> dict:
    return {
        "ok": False,
        "status": "partial_error",
        "error": (
            "部分数据源调用失败 "
            "(alphaengine: E_BAD_DATE_RANGE: date_from 和 date_to 必须成对提供; "
            "guidepoint: guidepoint timed out after 90s)"
        ),
        "source_status": {
            "local_all": {"ok": True, "status": "ok", "count": 3},
            "alphaengine": {
                "ok": False,
                "status": "failed",
                "error": "E_BAD_DATE_RANGE: date_from 和 date_to 必须成对提供",
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


def test_a_mixed_e_bad_and_timeout_failure_is_not_a_parameter_error():
    payload = _mixed_all_failure_payload()
    rb._apply_error_and_empty_contract("research", "all", "search", payload, {})
    assert payload["error_category"] == "上游不可用"
    # Per-leg categories so the agent can see which leg needs which fix.
    assert payload["leg_categories"] == {
        "alphaengine": "参数错误",
        "guidepoint": "上游不可用",
    }


def test_an_all_e_bad_failure_stays_a_parameter_error():
    payload = _mixed_all_failure_payload()
    payload["source_status"]["guidepoint"] = {
        "ok": False,
        "status": "failed",
        "error": "E_BAD_ARGS: unknown option --ticker",
        "count": 0,
    }
    payload["error"] = (
        "部分数据源调用失败 "
        "(alphaengine: E_BAD_DATE_RANGE: date_from 和 date_to 必须成对提供; "
        "guidepoint: E_BAD_ARGS: unknown option --ticker)"
    )
    rb._apply_error_and_empty_contract("research", "all", "search", payload, {})
    assert payload["error_category"] == "参数错误"
    assert "leg_categories" not in payload


def test_sec_no_matching_concepts_is_a_no_match_with_an_id_hint():
    payload = {
        "ok": False,
        "status": "error",
        "error": "No concepts matching 'RevenueFromContractWithCustomerExcludingAssessedTax'",
    }
    category, hint = rb._classify_error_category_and_hint(
        "finance", "sec", "search-concepts", payload
    )
    assert category == "没有匹配"
    assert "list-filings" in hint
    assert "不要猜" in hint


def test_sec_attachment_not_found_is_a_no_match():
    payload = {"ok": False, "status": "error", "error": "Requested attachment was not found"}
    category, hint = rb._classify_error_category_and_hint(
        "finance", "sec", "read-item", payload
    )
    assert category == "没有匹配"
    assert "暂不可用" not in hint


def test_sec_concept_not_found_in_data_error_is_no_match():
    payload = {
        "ok": False,
        "status": "error",
        "data": {
            "error": "Concept 'NoSuchConceptXyz123' not found. Use search-concepts first.",
        },
    }
    rb._apply_error_and_empty_contract(
        "finance",
        "sec",
        "facts",
        payload,
        {"route": "sec", "operation": "facts", "ticker": "AAPL", "concept": "NoSuchConceptXyz123"},
    )
    assert payload["error_category"] == "没有匹配"
    assert payload["hint"] == "先用 search-concepts / list-filings / attachments 拿到可用的名称或 ID 再读，不要猜"


def test_sec_object_not_found_variants_and_non_sec_isolation():
    for err_msg in (
        "Concept 'NoSuchConceptXyz123' not found. Use search-concepts first.",
        "Attachment 'ex10-1.htm' not found",
        "Filing '0000320193-25-000001' not found",
        "Item 'Item 1A' not found",
        "Accession '0000320193-25-000001' not found",
    ):
        payload = {"ok": False, "status": "error", "data": {"error": err_msg}}
        category, hint = rb._classify_error_category_and_hint(
            "finance", "sec", "facts", payload
        )
        assert category == "没有匹配", err_msg
        assert hint == "先用 search-concepts / list-filings / attachments 拿到可用的名称或 ID 再读，不要猜"

    # Generic upstream 404 or non-SEC route must NOT be misclassified as "没有匹配".
    upstream_outage = {
        "ok": False,
        "status": "error",
        "data": {"error": "HTTP 404 page not found from gateway"},
    }
    cat_outage, _ = rb._classify_error_category_and_hint(
        "finance", "sec", "facts", upstream_outage
    )
    assert cat_outage == "上游不可用"

    non_sec = {
        "ok": False,
        "status": "error",
        "data": {"error": "Concept 'NoSuchConceptXyz123' not found. Use search-concepts first."},
    }
    cat_non_sec, _ = rb._classify_error_category_and_hint(
        "research", "alphaengine", "search", non_sec
    )
    assert cat_non_sec == "上游不可用"

    # Top-level error fallback when data is a dict without an "error" key.
    top_level_sec_miss = {
        "ok": False,
        "error": "No concepts matching 'X'",
        "data": {"count": 0},
    }
    cat_top, _ = rb._classify_error_category_and_hint(
        "finance", "sec", "search-concepts", top_level_sec_miss
    )
    assert cat_top == "没有匹配"


def test_a_ticker_on_a_query_only_route_folds_into_the_query():
    normalized, _, err = rb._normalize_and_validate_call_args(
        "research",
        {"route": "alphaengine", "ticker": "NVDA", "query": "AI capex", "start": "2026-09-01", "end": "2026-10-23"},
    )
    assert err is None
    assert "ticker" not in normalized
    assert normalized["query"] == "NVDA AI capex"


def test_a_ticker_already_in_the_query_is_not_duplicated():
    normalized, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "tmtb", "ticker": "nvda", "query": "NVDA AI"}
    )
    assert err is None
    assert normalized["query"] == "NVDA AI"


def test_a_ticker_alone_becomes_the_query():
    normalized, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "sales-notes", "ticker": "META"}
    )
    assert err is None
    assert normalized["query"] == "META"


def test_transcript_keeps_its_native_ticker():
    # transcript's gateway takes --ticker; folding must not have widened to it.
    normalized, _, err = rb._normalize_and_validate_call_args(
        "research", {"route": "transcript", "ticker": "NVDA"}
    )
    assert err is None
    assert normalized["ticker"] == "NVDA"


def test_a_mixed_e_bad_with_param_keyword_and_timeout_is_still_upstream_unavailable():
    # One leg fails with "E_BAD_ARGS: 参数..." (matching both e_bad_signal and
    # other_param_signal) while another leg times out; route="all" classifies
    # per leg first and only marks the whole call 参数错误 if all failed legs are.
    payload = _mixed_all_failure_payload()
    payload["source_status"]["alphaengine"] = {
        "ok": False,
        "status": "failed",
        "error": "E_BAD_ARGS: 参数 format is invalid",
        "count": 0,
    }
    payload["error"] = (
        "部分数据源调用失败 "
        "(alphaengine: E_BAD_ARGS: 参数 format is invalid; "
        "guidepoint: guidepoint timed out after 90s)"
    )
    rb._apply_error_and_empty_contract("research", "all", "search", payload, {})
    assert payload["error_category"] == "上游不可用"
    assert payload["leg_categories"] == {
        "alphaengine": "参数错误",
        "guidepoint": "上游不可用",
    }


def test_short_ticker_t_is_not_dropped_by_substring_match_and_route_all_folds_ticker():
    normalized, _, err = rb._normalize_and_validate_call_args(
        "research",
        {"route": "all", "ticker": "T", "query": "dividend policy"},
    )
    assert err is None
    assert "ticker" not in normalized
    assert normalized["query"] == "T dividend policy"

    # Whole-word match (case-insensitive, non-alphanumeric boundary) is not duplicated.
    normalized_dup, _, err_dup = rb._normalize_and_validate_call_args(
        "research",
        {"route": "all", "ticker": "T", "query": "T dividend policy"},
    )
    assert err_dup is None
    assert "ticker" not in normalized_dup
    assert normalized_dup["query"] == "T dividend policy"


def test_the_catalog_still_advertises_ticker_with_the_folding_note():
    for route in rb.TICKER_FOLDS_INTO_QUERY_ROUTES:
        spec = rb.ROUTE_CONTRACT_SPECS[route]
        assert "ticker" in spec["accepted_params"], route
        assert "并入 query" in spec["param_formats"]["ticker"], route
