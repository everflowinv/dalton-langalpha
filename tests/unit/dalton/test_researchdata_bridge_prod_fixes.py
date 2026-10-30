"""Production-fix batch 2 contract tests for the researchdata bridge.

Covers the `route='all'` mixed-failure classification, SEC no-match texts,
ticker folding on query-only routes, and Guidepoint's keyword list shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
    # R2a fix1-2: the hint must not send the agent off to list-filings for an
    # ID — the failing call may be list-filings itself.
    assert "list-filings" not in hint
    assert "该条件下没有找到文件" in hint
    assert "放宽" in hint


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
    # R2a fix1-2: a confirmed SEC no-match is a successful empty result, not an
    # error the agent would retry or report as an outage.
    assert payload["ok"] is True
    assert payload["status"] == "empty"
    assert payload["empty_reason"] == "no_matching_records"
    assert payload["hint"] == "该条件下没有找到文件，可放宽表单 / 日期条件"
    assert not payload.get("error")


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
        assert hint == "该条件下没有找到文件，可放宽表单 / 日期条件"

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


# R2a-1 (2026-10-29 DASH 07:30 / NOW 08:55): upstream's "No matching <form>
# filing found" wording — a completed lookup with nothing to show — was missed
# by _SEC_OBJECT_NOT_FOUND_RE and classified 上游不可用 ("稍后重试"), so the agent
# retried a lookup that could never match.
def test_sec_no_matching_form_filing_wording_is_a_no_match():
    production_wordings = (
        "No matching 10-Q filing found",
        "No matching 10-K filings found",
        "No matching 8-K filing found",
        "No matching 20-F filings found",
        "No matching S-1 filing found",
        "No matching DEF 14A filings found",
        "No matching Form 4 filings found",
        "No matching 13F-HR filing found",
        "No matching filings",
        "No matching filings found",
        "no 10-Q filings found",
        "no 8-K filings found",
        "no DEF 14A filings found",
        "no Form 4 filings found",
        "no 13F-HR filings found",
    )
    for err_msg in production_wordings:
        payload = {"ok": False, "status": "error", "data": {"error": err_msg}}
        category, hint = rb._classify_error_category_and_hint(
            "finance", "sec", "list-filings", payload
        )
        assert category == "没有匹配", err_msg
        # The hint says "nothing under these conditions", never "retry later".
        assert "暂不可用" not in hint and "稍后重试" not in hint, err_msg

    # The same wording on the top-level error field classifies identically.
    top_level = {"ok": False, "status": "error", "error": "No matching 10-Q filing found"}
    category_top, _ = rb._classify_error_category_and_hint(
        "finance", "sec", "list-filings", top_level
    )
    assert category_top == "没有匹配"


def test_sec_no_matching_form_filing_contract_is_a_successful_empty_result():
    payload = {"ok": False, "status": "error", "data": {"error": "No matching 10-Q filing found"}}
    rb._apply_error_and_empty_contract(
        "finance",
        "sec",
        "list-filings",
        payload,
        {"route": "sec", "operation": "list-filings", "ticker": "DASH"},
    )
    # R2a fix1-2: same shape and field names as the generic no-match contract.
    assert payload["ok"] is True
    assert payload["status"] == "empty"
    assert payload["empty_reason"] == "no_matching_records"
    assert payload["hint"] == "该条件下没有找到文件，可放宽表单 / 日期条件"
    assert not payload.get("error")
    assert payload["data"] == {"ok": True}


def test_sec_no_match_is_a_successful_empty_result():
    """R2a fix1-2: production wordings (and both payload shapes) convert to
    `ok: true` + empty + `empty_reason`, never `ok: false`/`error`."""
    for payload in (
        {"ok": False, "status": "error", "data": {"error": "No matching 10-Q filing found"}},
        {"ok": False, "status": "error", "error": "no 8-K filings found"},
        {
            "ok": False,
            "status": "error",
            "data": {"error": "No matching DEF 14A filings found"},
        },
    ):
        p = dict(payload)
        rb._apply_error_and_empty_contract("finance", "sec", "list-filings", p, {"route": "sec"})
        assert p["ok"] is True, p
        assert p["status"] == "empty"
        assert p["empty_reason"] == "no_matching_records"
        assert p["hint"] == "该条件下没有找到文件，可放宽表单 / 日期条件"
        assert not p.get("error") and not p.get("error_category")

    # A genuine outage on the same route stays an error — only the no-match
    # wording converts.
    outage = {"ok": False, "status": "error", "data": {"error": "HTTP 502 Bad Gateway"}}
    rb._apply_error_and_empty_contract("finance", "sec", "list-filings", outage, {"route": "sec"})
    assert outage["ok"] is False
    assert outage.get("error_category") == "上游不可用"


# R2a fix1-3: every form in the thesis-delta filing monitor's DEFAULT_SEC_FORMS
# (8-K, 10-Q, 10-K, 6-K, 20-F, S-3ASR, 424B2, 424B5, FWP, 4, SC 13D, SC 13G)
# plus the /A amendments, in both upstream wordings, is a no-match.
SEC_MONITOR_FORMS = (
    "8-K",
    "10-Q",
    "10-K",
    "6-K",
    "20-F",
    "S-3ASR",
    "424B2",
    "424B5",
    "FWP",
    "4",
    "SC 13D",
    "SC 13G",
    "10-K/A",
    "10-Q/A",
    "8-K/A",
    "S-1/A",
    "SC 13D/A",
    "424B2/A",
    "6-K/A",
    "20-F/A",
    "S-3ASR/A",
)


@pytest.mark.parametrize("form", SEC_MONITOR_FORMS)
@pytest.mark.parametrize("template", ["No matching {f} filing found", "no {f} filings found"])
def test_every_monitor_form_and_amendment_is_a_no_match(form: str, template: str):
    err_msg = template.format(f=form)
    payload = {"ok": False, "status": "error", "data": {"error": err_msg}}
    category, hint = rb._classify_error_category_and_hint(
        "finance", "sec", "list-filings", payload
    )
    assert category == "没有匹配", err_msg
    contract = {"ok": False, "status": "error", "data": {"error": err_msg}}
    rb._apply_error_and_empty_contract(
        "finance", "sec", "list-filings", contract, {"route": "sec"}
    )
    assert contract["ok"] is True and contract["empty_reason"] == "no_matching_records", err_msg


def test_lowercase_prose_is_not_a_form_name():
    for err_msg in (
        "no matching quarterly filings found",
        "no matching annual filings found",
    ):
        payload = {"ok": False, "status": "error", "data": {"error": err_msg}}
        contract = dict(payload)
        rb._apply_error_and_empty_contract(
            "finance", "sec", "list-filings", contract, {"route": "sec"}
        )
        # Ordinary prose about filings is not a recognized no-match wording, so
        # it keeps the failure contract instead of converting to empty.
        assert contract["ok"] is False, err_msg


@pytest.mark.parametrize(
    "err_msg",
    (
        "HTTP 502 Bad Gateway from upstream",
        "upstream request timed out after 30s",
        "internal error while listing filings",
        "SEC upstream returned HTTP 500 (internal server error)",
        "connect error: connection refused",
    ),
)
def test_real_upstream_errors_are_not_misread_as_sec_no_match(err_msg):
    payload = {"ok": False, "status": "error", "data": {"error": err_msg}}
    category, _ = rb._classify_error_category_and_hint(
        "finance", "sec", "list-filings", payload
    )
    assert category != "没有匹配", err_msg
