"""D-line (batch 5) contract tests for the researchdata bridge: the three
mis-classified upstream failures (AlphaEngine's single-doc not-found, an
unresolvable source name, Xueqiu's expired-login HTTP 400) and the failure
log's error category + sanitized excerpt (D3/D4)."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

SCRIPTS_DALTON = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402


def _classify(tool: str, route: str, operation: str, error: str) -> tuple[str, str]:
    payload = {"ok": False, "status": "error", "error": error}
    return rb._classify_error_category_and_hint(tool, route, operation, payload)


# ------------------------------------------------------------------------- D3

def test_alphaengine_doc_not_found_is_no_match_without_retry_hint():
    # The production text: one document that does not exist while other
    # documents in the same round read fine — the upstream answered, so this
    # is a no-match, and re-requesting the same document cannot help.
    category, hint = _classify(
        "research", "alphaengine", "get_document", "E_UPSTREAM_API: 数据不存在"
    )
    assert category == "没有匹配"
    assert "文档" in hint
    assert "重试" not in hint
    assert "暂不可用" not in hint


def test_unresolved_source_name_is_a_parameter_error_naming_list_filters():
    # The production text (a real task retried it 4 times as if it were an
    # outage): the caller's source name does not match exactly, and the
    # upstream message itself names the fix.
    category, hint = _classify(
        "research",
        "guidepoint",
        "search_library",
        'E_SOURCE_NOT_RESOLVED: 无法唯一匹配来源"J.P. Morgan"；'
        "可先调用 list_filters(filter_type=sources)",
    )
    assert category == "参数错误"
    assert "list_filters(filter_type=sources)" in hint
    assert "暂不可用" not in hint


def test_xueqiu_expired_login_400_is_permission_or_quota():
    # The production text: Xueqiu's comments API answers an expired login
    # cookie with an HTTP 400 (no 401/403, none of the existing login
    # wordings), and 5 straight batch requests failed on it.
    category, hint = _classify(
        "community",
        "xueqiu",
        "comments",
        "HTTP 400 用户未登录 — 雪球 Cookie 缺失或已过期",
    )
    assert category == "权限或额度"
    assert "雪球登录态失效" in hint
    assert "停止继续请求" in hint
    assert "改用其他" in hint
    assert "报告" in hint


def test_plain_http_400_stays_upstream_unavailable():
    # Guard for D3: not every HTTP 400 is an auth failure — only the login
    # wording is matched, never the status code.
    category, _hint = _classify(
        "community", "xueqiu", "comments", "HTTP 400 Bad Request"
    )
    assert category == "上游不可用"


# ------------------------------------------------------------------------- D4

def _make_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> rb.ResearchDataBridge:
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    # Never read the host's bridge env file (it carries the real proxy and token).
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
            "isError": not bool(payload.get("ok")),
        },
    }


def _tools_call_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        rec.getMessage()
        for rec in caplog.records
        if rec.getMessage().startswith("tools/call ")
    ]


@pytest.mark.asyncio
async def test_failure_log_carries_category_and_sanitized_error_excerpt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """D4: a failing tools/call logs the classified category and the head of
    the error message — sanitized, so a token inside the upstream text never
    reaches the log (the SEC list-filings cluster was debugged blind)."""
    bridge = _make_bridge(tmp_path, monkeypatch)
    error_text = (
        "upstream researchdata_mcp failed for sec list-filings: "
        "EDB connection refused (token ghp_AbCdEfGhIjKlMnOpQrSt) after 2 retries"
    )

    async def _rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        return [
            _mcp_tool_response(
                requests[0]["id"],
                {"ok": False, "status": "error", "error": error_text},
            )
        ]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
        resp = await bridge.handle_tools_call(
            "req-sec-fail",
            {
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": "META",
                },
            },
        )
    assert resp["result"]["isError"] is True

    lines = _tools_call_lines(caplog)
    assert lines, "expected a tools/call log line"
    line = lines[-1]
    assert "status=error" in line
    assert "error_category=上游不可用" in line
    assert "EDB connection refused" in line
    # The GitHub-token pattern is redacted before the excerpt is taken.
    assert "ghp_" not in line
    assert "[REDACTED]" in line


@pytest.mark.asyncio
async def test_failure_log_redacts_url_query_values_and_whole_cookie_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """D-r2: the failure log's excerpt drops URL query values (names kept),
    whole Cookie strings, emails and mainland-China phone numbers — the
    codex-found leaks, on the original texts."""
    bridge = _make_bridge(tmp_path, monkeypatch)
    error_text = (
        "upstream GET failed after retry: "
        "https://example.test/search?query=alice%40example.com&phone=13800138000 "
        "and Cookie=session=abc123; sid=privateSession456 rejected"
    )

    async def _rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        return [
            _mcp_tool_response(
                requests[0]["id"],
                {"ok": False, "status": "error", "error": error_text},
            )
        ]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
        await bridge.handle_tools_call(
            "req-pii",
            {
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": "META",
                },
            },
        )

    line = _tools_call_lines(caplog)[-1]
    assert "status=error" in line
    assert "error_category=上游不可用" in line
    # The error message's head is still there for debugging.
    assert "upstream GET failed" in line
    # Parameter names survive; their values do not.
    assert "query=…" in line
    assert "phone=…" in line
    assert "alice%40example.com" not in line
    assert "13800138000" not in line
    # The whole cookie string goes, not just its first pair.
    assert "session=abc123" not in line
    assert "privateSession456" not in line


@pytest.mark.asyncio
async def test_failure_log_redacts_plain_emails_and_phone_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    error_text = "contact alice@example.com or 13800138000 for the failed lookup"

    async def _rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        return [
            _mcp_tool_response(
                requests[0]["id"],
                {"ok": False, "status": "error", "error": error_text},
            )
        ]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
        await bridge.handle_tools_call(
            "req-pii-plain",
            {
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": "META",
                },
            },
        )

    line = _tools_call_lines(caplog)[-1]
    assert "alice@example.com" not in line
    assert "13800138000" not in line
    assert "error_category=上游不可用" in line
    assert "failed lookup" in line


@pytest.mark.asyncio
async def test_success_log_keeps_its_original_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)

    async def _rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        return [
            _mcp_tool_response(
                requests[0]["id"],
                {"ok": True, "status": "ok", "results": []},
            )
        ]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
        await bridge.handle_tools_call(
            "req-sec-ok",
            {
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": "META",
                },
            },
        )

    lines = _tools_call_lines(caplog)
    assert lines
    line = lines[-1]
    assert "status=ok" in line
    # No error fields on a success line — its shape is unchanged from before D4.
    assert "error_category=" not in line
    assert " error=" not in line


@pytest.mark.parametrize(
    "raw",
    [
        # An upper-case scheme the absolute-URL pattern used to miss.
        "HTTPS://example.test/search?query=alice%40example.com&phone=13800138000",
        # A relative request path, as requests/urllib3 print it.
        "Max retries exceeded with url: /search?query=alice%40example.com"
        "&phone=13800138000 (Caused by ConnectTimeoutError)",
    ],
)
def test_failure_log_drops_query_values_in_any_url_form(raw: str) -> None:
    excerpt = rb._sanitize_failure_log_excerpt(raw)
    assert "alice%40example.com" not in excerpt
    assert "13800138000" not in excerpt
    assert "query=…" in excerpt and "phone=…" in excerpt
