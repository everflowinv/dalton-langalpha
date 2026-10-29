"""SEC call observability (backlog 三-4 + P0 fix1, 2026-10-28): every failed SEC
route call logs one structured line — correlation id, best-effort upstream HTTP
status, sanitized upstream error head, elapsed time, stdio attempt count,
ticker/form — and successful calls log elapsed at DEBUG."""

from __future__ import annotations

import asyncio
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

_CORR = "abc123def456"


def _log_records(
    caplog: pytest.LogCaptureFixture,
    payload: dict[str, Any] | None,
    *,
    arguments: dict[str, Any] | None = None,
    **kwargs: Any,
) -> list[str]:
    with caplog.at_level(logging.DEBUG, logger="researchdata_bridge"):
        rb._log_sec_call_outcome(
            corr_id=_CORR,
            operation="list-filings",
            arguments=arguments if arguments is not None else {"ticker": "META", "form": "10-K"},
            payload=payload,
            elapsed_ms=1234,
            stdio_attempts=2,
            **kwargs,
        )
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("sec_call_")]


def test_failure_line_carries_every_field(caplog: pytest.LogCaptureFixture) -> None:
    payload = {
        "ok": False,
        "status": "error",
        "error": "upstream rejected with HTTP 403 forbidden",
        "error_code": "E_UPSTREAM_API",
    }
    (message,) = _log_records(caplog, payload)
    assert message.startswith("sec_call_failed ")
    for fragment in (
        f"corr={_CORR}",
        "op=list-filings",
        "ticker=META",
        "form=10-K",
        "upstream_status=403",
        "elapsed_ms=1234",
        "attempts=2",
        "retries=1",
        "error_code=E_UPSTREAM_API",
        "error=upstream rejected with HTTP 403 forbidden",
    ):
        assert fragment in message, message


def test_failure_without_upstream_status_logs_dash_and_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {"ok": False, "status": "error", "error": "connection reset by peer"}
    (message,) = _log_records(caplog, payload)
    assert "upstream_status=-" in message
    assert "error=connection reset by peer" in message


def test_success_logs_elapsed_at_debug_with_no_error_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {"ok": True, "status": "ok", "data": {"items": []}}
    (message,) = _log_records(caplog, payload)
    assert message.startswith("sec_call_ok ")
    assert "elapsed_ms=1234" in message
    assert "ticker=META" in message
    assert "form=10-K" in message
    assert "error=" not in message


def test_explicit_status_code_field_beats_text_and_nested_locations_are_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {
        "ok": False,
        "error": "HTTP 500 textual",
        "data": {"structuredContent": {"status_code": 429}},
    }
    (message,) = _log_records(caplog, payload)
    assert "upstream_status=429" in message


def test_error_excerpt_and_code_are_sanitized_stripped_and_capped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {
        "ok": False,
        "error_code": "E_CODE_Bearer secret_token_123456\ninjected_line " + ("Z" * 80),
        "error": (
            "x" * 900
            + " Cookie: session=deadbeef; sid=priv"
            + " contact alice@example.com"
            + " https://upstream.example.com/path?api_key=sekrit&x=1"
        ),
    }
    (message,) = _log_records(caplog, payload)
    excerpt = message.split("error=", 1)[1]
    assert len(excerpt) <= 500
    assert "sekrit" not in excerpt
    assert "alice@example.com" not in excerpt
    assert "deadbeef" not in excerpt
    assert "secret_token_123456" not in message
    assert "\n" not in message


def test_timeout_without_payload_names_the_timeout_in_error_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    (message,) = _log_records(caplog, None, timeout_s=20.0)
    assert message.startswith("sec_call_failed ")
    assert "timed out after 20.0s" in message
    assert "upstream_status=-" in message
    assert "attempts=2 retries=1" in message


@pytest.mark.parametrize(
    ("err_text", "expected"),
    [
        ("HTTP 403 forbidden", 403),
        ("upstream http_502 after retry", 502),
        ("status_code=429 rate limited", 429),
        ("status: 503 unavailable", 503),
        ('{"code":"E_UPSTREAM_API","status_code":403,"message":"Forbidden"}', 403),
        ("{'code': 'E_UPSTREAM_API', 'status_code': 403, 'message': 'Forbidden'}", 403),
        ("E_UPSTREAM_API: no status mentioned", None),
        ("", None),
    ],
)
def test_extract_upstream_http_status_from_text(err_text: str, expected: int | None) -> None:
    assert rb._extract_upstream_http_status({"ok": False}, err_text) == expected


def test_form_falls_back_to_params_dict(caplog: pytest.LogCaptureFixture) -> None:
    (message,) = _log_records(
        caplog,
        {"ok": False, "error": "boom"},
        arguments={"ticker": "AAPL", "params": {"form": "8-K"}},
    )
    assert "ticker=AAPL" in message
    assert "form=8-K" in message


# ---------------------------------------------------------------- wiring


def _make_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> rb.ResearchDataBridge:
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    dummy_py = tmp_path / "dummy_python"
    dummy_py.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_OCW_PYTHON", str(dummy_py))
    cfg = rb.BridgeConfig()
    cfg.token = "test-mcp-token"
    cfg.admin_token = "test-admin-token"
    return rb.ResearchDataBridge(cfg)


def _stdio_response(ok: bool, error: Any) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"ok": ok, "status": "ok" if ok else "error"}
    if error:
        payload["error"] = error
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "isError": not ok,
            },
        }
    ]


@pytest.mark.asyncio
async def test_handle_tools_call_threads_corr_id_through_its_log_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    mock_rpc = create_autospec(bridge._run_stdio_rpc)
    mock_rpc.return_value = _stdio_response(False, "HTTP 429 too many requests")
    monkeypatch.setattr(bridge, "_run_stdio_rpc", mock_rpc)

    with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
        await bridge.handle_tools_call(
            1,
            {
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": "META",
                    "form": "10-K",
                },
            },
        )

    messages = [r.getMessage() for r in caplog.records]
    sec_failed = [m for m in messages if m.startswith("sec_call_failed")]
    tool_lines = [m for m in messages if m.startswith("tools/call ")]
    assert len(sec_failed) == 1, messages
    assert "upstream_status=429" in sec_failed[0]
    assert "ticker=META" in sec_failed[0]
    assert len(tool_lines) == 1, messages
    corr = sec_failed[0].split("corr=", 1)[1].split(" ", 1)[0]
    assert f"corr={corr}" in tool_lines[0]
    assert "status=error" in tool_lines[0]
    mock_rpc.assert_awaited_once()


@pytest.mark.asyncio
async def test_stdio_timeout_logs_only_one_sec_call_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    mock_rpc = create_autospec(bridge._run_stdio_rpc)
    mock_rpc.side_effect = asyncio.TimeoutError()
    monkeypatch.setattr(bridge, "_run_stdio_rpc", mock_rpc)

    with caplog.at_level(logging.WARNING, logger="researchdata_bridge"):
        await bridge.handle_tools_call(
            1,
            {
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": "META",
                    "form": "10-K",
                },
            },
        )

    sec_failed = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("sec_call_failed ")
    ]
    assert len(sec_failed) == 1, sec_failed


@pytest.mark.asyncio
async def test_jsonrpc_top_level_error_logs_sec_call_failed_with_sanitized_code_and_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    mock_rpc = create_autospec(bridge._run_stdio_rpc)
    mock_rpc.return_value = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32603,
                "message": "Internal RPC failure HTTP 502 api_key=top_level_secret_999\nline2",
            },
        }
    ]
    monkeypatch.setattr(bridge, "_run_stdio_rpc", mock_rpc)

    with caplog.at_level(logging.WARNING, logger="researchdata_bridge"):
        await bridge.handle_tools_call(
            1,
            {
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": "AAPL",
                    "form": "10-Q",
                },
            },
        )

    sec_failed = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("sec_call_failed ")
    ]
    assert len(sec_failed) == 1, sec_failed
    line = sec_failed[0]
    assert "error_code=-32603" in line
    assert "upstream_status=502" in line
    assert "Internal RPC failure" in line
    assert "top_level_secret_999" not in line
    assert "\n" not in line


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_field",
    [
        {"code": "E_UPSTREAM_API", "status_code": 403, "message": "Forbidden"},
        '{"code":"E_UPSTREAM_API","status_code":403,"message":"Forbidden"}',
    ],
)
async def test_structured_error_object_preserves_status_code_and_code_across_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_field: Any,
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    mock_rpc = create_autospec(bridge._run_stdio_rpc)
    mock_rpc.return_value = _stdio_response(False, error_field)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", mock_rpc)

    with caplog.at_level(logging.WARNING, logger="researchdata_bridge"):
        await bridge.handle_tools_call(
            1,
            {
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": "NVDA",
                    "form": "10-K",
                },
            },
        )

    sec_failed = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("sec_call_failed ")
    ]
    assert len(sec_failed) == 1, sec_failed
    line = sec_failed[0]
    assert "upstream_status=403" in line, line
    assert "error_code=E_UPSTREAM_API" in line, line


@pytest.mark.asyncio
async def test_failed_request_shares_same_corr_across_all_log_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)

    # Subprocess exits with code 1 and no JSON stdout -> logs warning in
    # _run_stdio_rpc, sec_call_failed in handle_tools_call, and Unhandled bridge
    # error in _handle_client.
    exec_mock = create_autospec(asyncio.create_subprocess_exec)

    async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = create_autospec(asyncio.subprocess.Process, instance=True)
        proc.returncode = 1
        proc.communicate.return_value = (b"", b"fatal crash")
        return proc

    exec_mock.side_effect = _fake_exec
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

    rpc_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": "META",
                    "form": "10-K",
                },
            },
        }
    ).encode("utf-8")
    raw_http = (
        b"POST /mcp HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Authorization: Bearer test-mcp-token\r\n"
        + f"Content-Length: {len(rpc_body)}\r\n\r\n".encode("ascii")
        + rpc_body
    )
    reader = asyncio.StreamReader()
    reader.feed_data(raw_http)
    reader.feed_eof()
    writer = create_autospec(asyncio.StreamWriter, instance=True)

    with caplog.at_level(logging.WARNING, logger="researchdata_bridge"):
        await rb._handle_client(bridge, reader, writer)

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) >= 3, messages
    assert any("subprocess returned no JSON response" in m for m in messages), messages
    assert any(m.startswith("sec_call_failed ") for m in messages), messages
    assert any(m.startswith("Unhandled bridge error:") for m in messages), messages

    corrs = set()
    for msg in messages:
        assert "corr=" in msg, msg
        corr_val = msg.split("corr=", 1)[1].split(")", 1)[0].split(" ", 1)[0]
        assert corr_val and corr_val != "-", msg
        corrs.add(corr_val)
    assert len(corrs) == 1, (corrs, messages)


@pytest.mark.asyncio
async def test_both_sec_call_failed_and_tools_call_strip_short_mcp_token_newline_and_esc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """P0 fix2 A: upstream error with `/mcp/0123456789abcdef0123456789abcdef`,
    newline, and ESC must be cleaned identically on both `sec_call_failed` and
    `tools/call` lines."""
    bridge = _make_bridge(tmp_path, monkeypatch)
    mock_rpc = create_autospec(bridge._run_stdio_rpc)
    dirty_error = (
        "upstream POST /mcp/0123456789abcdef0123456789abcdef/tools/call "
        "HTTP 502\n\x1b[31mforged_next_log_line=1"
    )
    mock_rpc.return_value = _stdio_response(False, dirty_error)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", mock_rpc)

    with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
        await bridge.handle_tools_call(
            1,
            {
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": "AAPL",
                    "form": "10-K",
                },
            },
        )

    messages = [r.getMessage() for r in caplog.records]
    sec_failed = [m for m in messages if m.startswith("sec_call_failed ")]
    tool_lines = [m for m in messages if m.startswith("tools/call ")]
    assert len(sec_failed) == 1, messages
    assert len(tool_lines) == 1, messages
    for line in (sec_failed[0], tool_lines[0]):
        assert "0123456789abcdef0123456789abcdef" not in line, line
        assert "/mcp/<redacted>" in line, line
        assert "\n" not in line, line
        assert "\x1b" not in line, line


@pytest.mark.asyncio
async def test_mcp_is_error_non_json_text_logs_sec_call_failed_403_and_tools_call_error_without_altering_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """P0 fix2 D: when SEC returns MCP tool error `result.isError=true` with
    plain text `Error calling tool finance: HTTP 403 Forbidden` (non-JSON, no
    top-level `error`), log `sec_call_failed` with `upstream_status=403`, log
    `status=error` (not `status=ok`) on `tools/call`, and return the original
    reply to the agent untouched."""
    bridge = _make_bridge(tmp_path, monkeypatch)
    mock_rpc = create_autospec(bridge._run_stdio_rpc)
    raw_reply = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": "Error calling tool finance: HTTP 403 Forbidden",
                }
            ],
            "isError": True,
        },
    }
    mock_rpc.return_value = [json.loads(json.dumps(raw_reply))]
    monkeypatch.setattr(bridge, "_run_stdio_rpc", mock_rpc)

    with caplog.at_level(logging.INFO, logger="researchdata_bridge"):
        resp = await bridge.handle_tools_call(
            1,
            {
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": "MSFT",
                    "form": "10-K",
                },
            },
        )

    assert resp == raw_reply
    messages = [r.getMessage() for r in caplog.records]
    sec_failed = [m for m in messages if m.startswith("sec_call_failed ")]
    tool_lines = [m for m in messages if m.startswith("tools/call ")]
    assert len(sec_failed) == 1, messages
    assert "upstream_status=403" in sec_failed[0], sec_failed[0]
    assert "HTTP 403 Forbidden" in sec_failed[0], sec_failed[0]
    assert len(tool_lines) == 1, messages
    assert "status=error" in tool_lines[0], tool_lines[0]
    assert "status=ok" not in tool_lines[0], tool_lines[0]

