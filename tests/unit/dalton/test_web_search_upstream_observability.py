"""Web search upstream observability (backlog 三-6 + P0 fix1, 2026-10-28): the
host bridge passes the broker frame's upstream status/code through as new
optional reply fields (or extracts HTTP status from error.message text when the
frame carries only code/message), the backend classifier rides them onto the
raised exception and its log lines (redacted & truncated), x_search preserves
HTTPError status on ok:false bodies and broker errors, and a reply served by a
fallback provider (last30days) logs a downgrade line with a redacted reason."""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from typing import Any
from unittest.mock import create_autospec

import pytest

from scripts.dalton.web_search_bridge import (
    WebSearchBridge,
    WebSearchBridgeConfig,
    _extract_upstream_error_signal,
    invoke_broker_x_search,
)
from src.dalton.web_search.provider import (
    OpenClawSearchBridgeUnavailableError,
    OpenClawSearchRequestError,
    OpenClawSearchWrapper,
    _raise_for_bridge_error_payload,
    build_web_search_tool,
)


def _make_bridge(monkeypatch: pytest.MonkeyPatch, tmp_path) -> WebSearchBridge:
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_ENV_FILE", str(empty_env))
    monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_TOKEN", "fake-bridge-token")
    monkeypatch.setenv("DALTON_WEB_SEARCH_BROKER_TOKEN", "fake-broker-token")
    return WebSearchBridge(WebSearchBridgeConfig())


# ------------------------------------------------- upstream signal extraction


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        ({"ok": False, "error": {"code": "PROVIDER_ERROR", "status": 429}}, (429, None)),
        ({"ok": False, "error": {"code": "PROVIDER_ERROR"}, "upstream_status": 503}, (503, None)),
        ({"ok": False, "upstream_status": 403, "upstream_code": "quota"}, (403, "quota")),
        (
            {
                "ok": False,
                "error": {"code": "PROVIDER_ERROR", "message": "upstream failed with HTTP 429"},
            },
            (429, None),
        ),
        (
            {
                "ok": False,
                "error": {"code": "PROVIDER_ERROR", "message": "provider returned status 503"},
            },
            (503, None),
        ),
        (
            {
                "ok": False,
                "error": {"code": "PROVIDER_ERROR", "message": "429 Too Many Requests from gemini"},
            },
            (429, None),
        ),
        ({"ok": False, "error": {"code": "PROVIDER_ERROR", "message": "no status"}}, (None, None)),
        ({"ok": False, "error": {"code": "E_X", "status": 99}}, (None, None)),
    ],
)
def test_extract_upstream_error_signal(frame: dict[str, Any], expected: tuple[Any, Any]) -> None:
    assert _extract_upstream_error_signal(frame) == expected


# ------------------------------------------------- bridge reply passthrough


@pytest.mark.asyncio
async def test_bridge_error_reply_gains_upstream_fields_from_message_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    import scripts.dalton.web_search_bridge as wsb

    bridge = _make_bridge(monkeypatch, tmp_path)
    mock_invoke = create_autospec(wsb.invoke_broker_over_socket)
    mock_invoke.return_value = {
        "schemaVersion": "0.1",
        "ok": False,
        "error": {"code": "PROVIDER_ERROR", "message": "gemini upstream returned HTTP 429"},
    }
    monkeypatch.setattr(wsb, "invoke_broker_over_socket", mock_invoke)

    with caplog.at_level(logging.INFO, logger="web_search_bridge"):
        status, reply = await bridge.handle_search({"query": "nvda", "count": 3})

    assert status == 502
    assert reply["ok"] is False
    assert reply["error"]["code"] == "PROVIDER_ERROR"
    assert reply["upstream_status"] == 429
    completed = [r.getMessage() for r in caplog.records if "search completed" in r.getMessage()]
    assert len(completed) == 1
    assert "upstream_status=429" in completed[0]


@pytest.mark.asyncio
async def test_bridge_error_reply_without_upstream_signal_logs_redacted_message_and_stays_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    import scripts.dalton.web_search_bridge as wsb

    bridge = _make_bridge(monkeypatch, tmp_path)
    frame = {
        "schemaVersion": "0.1",
        "ok": False,
        "error": {
            "code": "PROVIDER_ERROR",
            "message": "no final answer with api_key=leaked_msg_secret_123\ninjected=1 " + ("m" * 240),
        },
    }
    mock_invoke = create_autospec(wsb.invoke_broker_over_socket)
    mock_invoke.return_value = dict(frame)
    monkeypatch.setattr(wsb, "invoke_broker_over_socket", mock_invoke)

    with caplog.at_level(logging.INFO, logger="web_search_bridge"):
        status, reply = await bridge.handle_search({"query": "nvda", "count": 3})

    assert status == 502
    assert "upstream_status" not in reply
    assert "upstream_code" not in reply
    completed = [r.getMessage() for r in caplog.records if "search completed" in r.getMessage()]
    assert len(completed) == 1
    assert "message=" in completed[0]
    assert "leaked_msg_secret_123" not in completed[0]
    assert "\n" not in completed[0]
    msg_part = completed[0].split("message=", 1)[1]
    assert len(msg_part) <= 200


@pytest.mark.asyncio
async def test_bridge_broker_exception_error_payload_carries_upstream_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import scripts.dalton.web_search_bridge as wsb

    bridge = _make_bridge(monkeypatch, tmp_path)
    mock_invoke = create_autospec(wsb.invoke_broker_over_socket)
    mock_invoke.side_effect = wsb.WebSearchBrokerError(
        "OpenClaw broker returned HTTP 504", code="BROKER_HTTP_ERROR", upstream_status=504
    )
    monkeypatch.setattr(wsb, "invoke_broker_over_socket", mock_invoke)

    status, reply = await bridge.handle_search({"query": "nvda", "count": 3})
    assert status == 503
    assert reply["upstream_status"] == 504
    assert reply["error"]["code"] == "BROKER_HTTP_ERROR"


# ------------------------------------------------- x-search upstream passthrough


def test_invoke_broker_x_search_preserves_http_error_code_on_ok_false_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_urlopen = create_autospec(urllib.request.urlopen)
    err_body = json.dumps(
        {"ok": False, "error": {"code": "PROVIDER_ERROR", "message": "xai upstream error"}}
    ).encode("utf-8")
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url="http://127.0.0.1:18789/x-search",
        code=502,
        msg="Bad Gateway",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(err_body),
    )
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    reply = invoke_broker_x_search(
        broker_url="http://127.0.0.1:18789/x-search",
        broker_token="tok",
        payload={"query": "nvda"},
        timeout_ms=5000,
    )
    assert reply["ok"] is False
    assert reply["upstream_status"] == 502


@pytest.mark.asyncio
async def test_handle_x_search_broker_error_logs_and_returns_upstream_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    import scripts.dalton.web_search_bridge as wsb

    bridge = _make_bridge(monkeypatch, tmp_path)
    mock_invoke = create_autospec(wsb.invoke_broker_x_search)
    mock_invoke.side_effect = wsb.WebSearchBrokerError(
        "OpenClaw broker returned HTTP 503",
        code="BROKER_HTTP_ERROR",
        upstream_status=503,
        upstream_code="overloaded_Bearer secret_x_tok_999",
    )
    monkeypatch.setattr(wsb, "invoke_broker_x_search", mock_invoke)

    with caplog.at_level(logging.WARNING, logger="web_search_bridge"):
        status, reply = await bridge.handle_x_search({"query": "nvda", "count": 5})

    assert status == 503
    assert reply["upstream_status"] == 503
    assert reply["upstream_code"] == "overloaded_Bearer secret_x_tok_999"
    warns = [r.getMessage() for r in caplog.records if "x_search broker error" in r.getMessage()]
    assert len(warns) == 1
    assert "upstream_status=503" in warns[0]
    assert "secret_x_tok_999" not in warns[0]


# ------------------------------------------------- downgrade visibility


@pytest.mark.asyncio
async def test_last30days_reply_logs_downgrade_with_redacted_and_truncated_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    import scripts.dalton.web_search_bridge as wsb

    bridge = _make_bridge(monkeypatch, tmp_path)
    mock_invoke = create_autospec(wsb.invoke_broker_over_socket)
    mock_invoke.return_value = {
        "schemaVersion": "0.1",
        "ok": True,
        "provider": "last30days",
        "fallbackReason": 'gemini failed with "api_key": "secret_gemini_key_777"\nline2 ' + ("r" * 240),
        "providerRequestId": "req-fallback-1",
        "result": {"content": [{"type": "text", "text": "{}"}]},
    }
    monkeypatch.setattr(wsb, "invoke_broker_over_socket", mock_invoke)

    with caplog.at_level(logging.WARNING, logger="web_search_bridge"):
        status, reply = await bridge.handle_search({"query": "nvda", "count": 3})

    assert status == 200
    downgrade = [
        r.getMessage() for r in caplog.records if "provider_downgraded" in r.getMessage()
    ]
    assert len(downgrade) == 1
    message = downgrade[0]
    assert "expected=gemini" in message
    assert "effective=last30days" in message
    assert "reason_category=provider_fallback" in message
    assert "secret_gemini_key_777" not in message
    assert "\n" not in message
    reason_val = message.split("reason=", 1)[1]
    assert len(reason_val) <= 200


@pytest.mark.asyncio
async def test_expected_provider_reply_does_not_log_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    import scripts.dalton.web_search_bridge as wsb

    bridge = _make_bridge(monkeypatch, tmp_path)
    mock_invoke = create_autospec(wsb.invoke_broker_over_socket)
    mock_invoke.return_value = {
        "schemaVersion": "0.1",
        "ok": True,
        "provider": "gemini",
        "result": {"content": [{"type": "text", "text": "{}"}]},
    }
    monkeypatch.setattr(wsb, "invoke_broker_over_socket", mock_invoke)

    with caplog.at_level(logging.WARNING, logger="web_search_bridge"):
        await bridge.handle_search({"query": "nvda", "count": 3})

    assert not [r for r in caplog.records if "provider_downgraded" in r.getMessage()]


# ------------------------------------------------- backend classifier


def test_classifier_attaches_upstream_fields_to_request_error() -> None:
    with pytest.raises(OpenClawSearchRequestError) as exc_info:
        _raise_for_bridge_error_payload(
            {
                "ok": False,
                "error": {"code": "PROVIDER_RATE_LIMITED", "message": "slow down"},
                "upstream_status": 429,
                "upstream_code": "resource_exhausted",
            },
            http_status=429,
        )
    assert exc_info.value.upstream_status == 429
    assert exc_info.value.upstream_code == "resource_exhausted"


def test_classifier_attaches_upstream_fields_to_unavailable_error() -> None:
    with pytest.raises(OpenClawSearchBridgeUnavailableError) as exc_info:
        _raise_for_bridge_error_payload(
            {
                "ok": False,
                "error": {"code": "PROVIDER_ERROR", "message": "boom"},
                "upstream_status": 500,
            }
        )
    assert exc_info.value.upstream_status == 500
    assert getattr(exc_info.value, "upstream_code", None) is None


def test_classifier_without_upstream_fields_raises_cleanly() -> None:
    with pytest.raises(OpenClawSearchBridgeUnavailableError) as exc_info:
        _raise_for_bridge_error_payload(
            {"ok": False, "error": {"code": "PROVIDER_ERROR", "message": "boom"}}
        )
    assert getattr(exc_info.value, "upstream_status", None) is None


@pytest.mark.asyncio
async def test_tool_log_line_carries_redacted_and_truncated_upstream_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_wrapper = create_autospec(OpenClawSearchWrapper, instance=True)

    def _raise_rate_limited(*args: Any, **kwargs: Any) -> Any:
        exc = OpenClawSearchRequestError(
            "OpenClaw 网页搜索触发限流，请稍后重试", code="PROVIDER_RATE_LIMITED"
        )
        exc.upstream_status = 429
        exc.upstream_code = "resource_exhausted token=leaked_provider_tok_888\nline2 " + ("c" * 80)
        raise exc

    mock_wrapper.raw_results.side_effect = _raise_rate_limited
    tool = build_web_search_tool(max_results=5, wrapper=mock_wrapper)

    with caplog.at_level(logging.WARNING, logger="src.dalton.web_search.provider"):
        content, artifact = await tool.coroutine(query="ASML bookings")

    assert isinstance(content, str)
    request_errors = [
        r.getMessage() for r in caplog.records if "request error" in r.getMessage()
    ]
    assert len(request_errors) == 1
    message = request_errors[0]
    assert "PROVIDER_RATE_LIMITED" in message
    assert "upstream_status=429" in message
    assert "resource_exhausted" in message
    assert "leaked_provider_tok_888" not in message
    assert "\n" not in message
    code_tail = message.split("upstream_code=", 1)[1]
    assert len(code_tail) <= 64
    assert artifact["code"] == "PROVIDER_RATE_LIMITED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_code", "expected_clean_code"),
    [
        ("PROVIDER_RATE_LIMITED\n", "PROVIDER_RATE_LIMITED"),
        ("AUTH_REQUIRED ", "AUTH_REQUIRED"),
    ],
)
async def test_raw_error_code_controls_http_status_and_payload_while_log_is_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
    raw_code: str,
    expected_clean_code: str,
) -> None:
    """P0 fix2 E: status mapping and reply payload must use the uncleaned
    `exc.code` so `"PROVIDER_RATE_LIMITED\\n"` and `"AUTH_REQUIRED "` keep their
    baseline 502 status on both search endpoints; only the logged copy is
    sanitized."""
    import scripts.dalton.web_search_bridge as wsb

    bridge = _make_bridge(monkeypatch, tmp_path)

    mock_search = create_autospec(wsb.invoke_broker_over_socket)
    mock_search.return_value = {
        "schemaVersion": "0.1",
        "ok": False,
        "error": {"code": raw_code, "message": "broker failed"},
    }
    monkeypatch.setattr(wsb, "invoke_broker_over_socket", mock_search)

    with caplog.at_level(logging.INFO, logger="web_search_bridge"):
        status_ws, reply_ws = await bridge.handle_search({"query": "nvda", "count": 3})

    assert status_ws == 502
    assert reply_ws["error"]["code"] == raw_code
    ws_lines = [r.getMessage() for r in caplog.records if "status=error" in r.getMessage()]
    assert len(ws_lines) == 1
    assert f"error_code={expected_clean_code}" in ws_lines[0]
    assert "\n" not in ws_lines[0]

    caplog.clear()
    mock_x = create_autospec(wsb.invoke_broker_x_search)
    mock_x.return_value = {
        "schemaVersion": "0.1",
        "ok": False,
        "error": {"code": raw_code, "message": "x broker failed"},
    }
    monkeypatch.setattr(wsb, "invoke_broker_x_search", mock_x)

    with caplog.at_level(logging.INFO, logger="web_search_bridge"):
        status_x, reply_x = await bridge.handle_x_search({"query": "nvda", "count": 3})

    assert status_x == 502
    assert reply_x["error"]["code"] == raw_code
    x_lines = [r.getMessage() for r in caplog.records if "status=error" in r.getMessage()]
    assert len(x_lines) == 1
    assert f"error_code={expected_clean_code}" in x_lines[0]
    assert "\n" not in x_lines[0]

