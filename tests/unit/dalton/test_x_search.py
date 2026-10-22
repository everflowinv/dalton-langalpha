"""Unit tests for Dalton OpenClaw X search (`x_search` direct tool + host bridge `/x-search`)."""

from __future__ import annotations

import asyncio
import http.client
import io
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import scripts.dalton.web_search_bridge as bridge_mod
from scripts.dalton.web_search_bridge import (
    WebSearchBridge,
    WebSearchBridgeConfig,
    WebSearchBrokerError,
    _normalize_x_handles,
    invoke_broker_over_socket,
    invoke_broker_x_search,
)
from src.dalton.web_search.provider import (
    OpenClawSearchBridgeUnavailableError,
    OpenClawSearchRequestError,
)
from src.dalton.web_search.x_search import (
    OpenClawXSearchWrapper,
    X_ANSWER_ONLY_HEADING,
    X_ANSWER_WITH_SOURCES_HEADING,
    X_BRIDGE_BUSY_MESSAGE,
    X_BRIDGE_UNAVAILABLE_MESSAGE,
    X_EMPTY_RESULTS_MESSAGE,
    X_RATE_LIMITED_MESSAGE,
    X_UNTRUSTED_NOTICE,
    build_x_search_tool,
    decode_snowflake_date,
    map_x_search_response,
)


def test_decode_snowflake_date_with_real_url_and_bounds() -> None:
    """Real URL `https://x.com/NickRokke/status/2112925226740683159` decodes to 2026-10-20..2026-10-22 UTC."""
    broker_reply = {
        "ok": True,
        "provider": "xai",
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '{"query":"GOOG","provider":"xai",'
                        '"content":"<<<EXTERNAL_UNTRUSTED_CONTENT id=\\"1\\">>>\\nSource: X Search\\n---\\n'
                        "Summary text.\\n"
                        '<<<END_EXTERNAL_UNTRUSTED_CONTENT id=\\"1\\">>>",'
                        '"citations":["https://x.com/NickRokke/status/2112925226740683159"]}'
                    ),
                }
            ]
        },
    }
    results, _ = map_x_search_response(broker_reply, query="GOOG")
    assert len(results) == 1
    assert results[0]["author"] == "@NickRokke"
    assert results[0]["url"] == "https://x.com/NickRokke/status/2112925226740683159"
    decoded_date = results[0]["date"]
    assert isinstance(decoded_date, str)
    assert "2026-10-20T00:00:00Z" <= decoded_date <= "2026-10-22T23:59:59Z"
    assert results[0]["summary_excerpt"] is None
    assert "content" not in results[0]

    # Out-of-bounds snowflake IDs return None
    assert decode_snowflake_date("123") is None
    assert decode_snowflake_date("9999999999999999999999999") is None


def test_codex_shared_citation_line_and_date_from_snowflake_only() -> None:
    """Codex case: `市场正在讨论 2026-11-01 的听证会。[1][2]` + 2 status URLs.

    Both posts' `date` values come from snowflake ID decoding (neither is 2026-11-01),
    and both `summary_excerpt` values are None because the sentence cites both `[1][2]`.
    """
    broker_reply = {
        "ok": True,
        "provider": "xai",
        "result": {
            "details": {
                "query": "GOOG hearing",
                "provider": "xai",
                "fromDate": "2026-10-20",
                "toDate": "2026-10-22",
                "content": "市场正在讨论 2026-11-01 的听证会。[1][2]",
                "citations": [
                    "https://x.com/NickRokke/status/2112925226740683159",
                    "https://twitter.com/elerianm/status/2112800000000000000",
                    "https://x.com/sundarpichai",
                    "https://www.reuters.com/technology/google-2026-10-22/",
                ],
            }
        },
    }

    results, _ = map_x_search_response(broker_reply, query="GOOG hearing")
    assert len(results) == 2

    assert results[0]["author"] == "@NickRokke"
    assert results[0]["date"] == decode_snowflake_date("2112925226740683159")
    assert results[0]["date"] is not None and not results[0]["date"].startswith("2026-11-01")
    assert results[0]["summary_excerpt"] is None

    assert results[1]["author"] == "@elerianm"
    assert results[1]["date"] == decode_snowflake_date("2112800000000000000")
    assert results[1]["date"] is not None and not results[1]["date"].startswith("2026-11-01")
    assert results[1]["summary_excerpt"] is None


def test_ten_or_more_citations_do_not_cross_match_footnote_1_and_10() -> None:
    """With >=10 citations, `[10]` must not match citation 1, and `/status/id` uses digit boundaries."""
    citations = [
        f"https://x.com/user{i}/status/{2112900000000000000 + i}"
        for i in range(1, 11)
    ]
    broker_reply = {
        "ok": True,
        "provider": "xai",
        "result": {
            "details": {
                "query": "GOOG",
                "provider": "xai",
                "content": (
                    "总体讨论集中在云业务增速。\n"
                    "- 第十条引用的专门观点 [10]\n"
                    "- 第一条引用的专门观点 [1]"
                ),
                "citations": citations,
            }
        },
    }

    results, _ = map_x_search_response(broker_reply, query="GOOG")
    assert len(results) == 10
    assert results[0]["author"] == "@user1"
    assert results[0]["summary_excerpt"] == "第一条引用的专门观点"
    for idx in range(1, 9):
        assert results[idx]["summary_excerpt"] is None
    assert results[9]["author"] == "@user10"
    assert results[9]["summary_excerpt"] == "第十条引用的专门观点"


@pytest.mark.asyncio
async def test_x_search_tool_places_answer_box_first_with_untrusted_notice_and_normalizes_handles() -> None:
    mock_wrapper = create_autospec(OpenClawXSearchWrapper, instance=True)
    mock_wrapper.raw_results.return_value = {
        "ok": True,
        "provider": "xai",
        "result": {
            "details": {
                "query": "GOOG premarket",
                "provider": "xai",
                "content": (
                    "@sundarpichai addressing cloud capacity "
                    "https://x.com/sundarpichai/status/2112925226740683159"
                ),
                "citations": ["https://x.com/sundarpichai/status/2112925226740683159"],
            }
        },
    }

    tool_fn = build_x_search_tool(wrapper=mock_wrapper)
    content, artifact = await tool_fn.coroutine(
        query="  GOOG premarket  ",
        from_date="2026-10-20",
        to_date="2026-10-22",
        handles=["@sundarpichai", "googlecloud"],
    )

    mock_wrapper.raw_results.assert_awaited_once_with(
        query="GOOG premarket",
        from_date="2026-10-20",
        to_date="2026-10-22",
        handles=["sundarpichai", "googlecloud"],
    )
    assert isinstance(content, list)
    assert len(content) == 2
    assert content[0]["type"] == "answer_box"
    assert content[0]["title"] == X_ANSWER_WITH_SOURCES_HEADING
    assert content[0]["content"].startswith(f"{X_UNTRUSTED_NOTICE}\n")
    assert "addressing cloud capacity" in content[0]["content"]
    assert content[1]["type"] == "x_post"
    assert content[1]["author"] == "@sundarpichai"
    assert content[1]["date"] == decode_snowflake_date("2112925226740683159")
    # Prose, not a per-post list item, so no excerpt; the summary itself is in the answer_box.
    assert content[1]["summary_excerpt"] is None
    assert content[1]["url"] == "https://x.com/sundarpichai/status/2112925226740683159"
    assert artifact["total_results"] == 1


@pytest.mark.asyncio
async def test_x_search_tool_uses_x_specific_error_messages() -> None:
    mock_wrapper = create_autospec(OpenClawXSearchWrapper, instance=True)
    mock_wrapper.raw_results.return_value = {
        "ok": True,
        "provider": "xai",
        "result": {
            "details": {
                "query": "NVDA",
                "provider": "xai",
                "content": "Overall sentiment is bullish with no direct post links.",
                "citations": [],
            }
        },
    }

    tool_fn = build_x_search_tool(wrapper=mock_wrapper)
    content, _ = await tool_fn.coroutine(query="NVDA")
    assert isinstance(content, str)
    assert content.startswith(f"{X_ANSWER_ONLY_HEADING}\n{X_UNTRUSTED_NOTICE}\n")

    # Parameter validation error (> 20 handles)
    bad_handles = [f"user{i}" for i in range(21)]
    err_content, err_art = await tool_fn.coroutine(query="NVDA", handles=bad_handles)
    assert "参数错误" in err_content
    assert "error" in err_art

    # Empty result error
    mock_wrapper.raw_results.return_value = {
        "ok": True,
        "provider": "xai",
        "result": {"details": {"query": "NVDA", "provider": "xai", "content": "", "citations": []}},
    }
    empty_content, _ = await tool_fn.coroutine(query="NVDA")
    assert empty_content == X_EMPTY_RESULTS_MESSAGE

    # Busy -> X-specific message, never "网页搜索"
    mock_wrapper.raw_results.side_effect = OpenClawSearchRequestError(
        X_BRIDGE_BUSY_MESSAGE, code="bridge_busy"
    )
    busy_content, busy_art = await tool_fn.coroutine(query="NVDA")
    assert busy_content == X_BRIDGE_BUSY_MESSAGE
    assert "网页搜索" not in busy_content
    assert busy_art["code"] == "bridge_busy"

    # Rate limit -> X-specific message, never "网页搜索"
    mock_wrapper.raw_results.side_effect = OpenClawSearchRequestError(
        X_RATE_LIMITED_MESSAGE, code="PROVIDER_RATE_LIMITED"
    )
    rl_content, rl_art = await tool_fn.coroutine(query="NVDA")
    assert rl_content == X_RATE_LIMITED_MESSAGE
    assert "网页搜索" not in rl_content
    assert rl_art["code"] == "PROVIDER_RATE_LIMITED"

    # Bridge unavailable -> X-specific message, even if underlying error carried web prefix
    mock_wrapper.raw_results.side_effect = OpenClawSearchBridgeUnavailableError("connection refused")
    unavail_content, _ = await tool_fn.coroutine(query="NVDA")
    assert unavail_content.startswith(X_BRIDGE_UNAVAILABLE_MESSAGE)
    assert "网页搜索" not in unavail_content


def test_bridge_normalize_handles_and_token_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(WebSearchBrokerError) as exc_info:
        _normalize_x_handles("not-a-list", "allowed_x_handles")
    assert exc_info.value.code == "INVALID_REQUEST"
    assert "must be a list" in str(exc_info.value)

    with pytest.raises(WebSearchBrokerError) as exc_info:
        _normalize_x_handles([123], "allowed_x_handles")
    assert exc_info.value.code == "INVALID_REQUEST"

    with pytest.raises(WebSearchBrokerError) as exc_info:
        _normalize_x_handles(["@@@"], "allowed_x_handles")
    assert exc_info.value.code == "INVALID_REQUEST"

    with pytest.raises(WebSearchBrokerError) as exc_info:
        _normalize_x_handles([f"u{i}" for i in range(21)], "allowed_x_handles")
    assert exc_info.value.code == "INVALID_REQUEST"

    monkeypatch.delenv("DALTON_BROKER_TOKEN", raising=False)
    monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_TOKEN", "bridge-secret")
    bad_token_file = tmp_path / "broker.token"
    bad_token_file.write_text("secret\n", encoding="utf-8")
    bad_token_file.chmod(0o644)
    monkeypatch.setenv("DALTON_BROKER_TOKEN_PATH", str(bad_token_file))

    cfg = WebSearchBridgeConfig()
    bridge = WebSearchBridge(cfg)
    with pytest.raises(WebSearchBrokerError) as exc_info:
        bridge._load_broker_bearer_token()
    assert exc_info.value.code == "UNSAFE_STATE_FILE"
    assert str(bad_token_file) not in str(exc_info.value)

    real_token_file = tmp_path / "real.token"
    real_token_file.write_text("secret\n", encoding="utf-8")
    real_token_file.chmod(0o600)
    symlink_token = tmp_path / "symlink.token"
    symlink_token.symlink_to(real_token_file)
    monkeypatch.setenv("DALTON_BROKER_TOKEN_PATH", str(symlink_token))
    cfg_sym = WebSearchBridgeConfig()
    bridge_sym = WebSearchBridge(cfg_sym)
    with pytest.raises(WebSearchBrokerError) as exc_info:
        bridge_sym._load_broker_bearer_token()
    assert exc_info.value.code == "UNSAFE_STATE_FILE"

    bad_token_file.write_text("   \n", encoding="utf-8")
    bad_token_file.chmod(0o600)
    with pytest.raises(WebSearchBrokerError) as exc_info:
        bridge._load_broker_bearer_token()
    assert exc_info.value.code == "BROKER_AUTH_UNAVAILABLE"


def test_invoke_broker_x_search_real_implementation_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real `invoke_broker_x_search` implementation using `create_autospec(urllib.request.urlopen)`."""
    mock_urlopen = create_autospec(urllib.request.urlopen)
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    # 1. HTTP error with structured JSON error payload -> returns parsed dict
    err_fp = io.BytesIO(
        b'{"ok":false,"error":{"code":"PROVIDER_RATE_LIMITED","message":"rate limit"}}'
    )
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url="http://127.0.0.1:18789/dalton-broker/v1/x-search",
        code=429,
        msg="Too Many Requests",
        hdrs=Message(),
        fp=err_fp,
    )
    parsed_err = invoke_broker_x_search(
        broker_url="http://127.0.0.1:18789/dalton-broker/v1",
        broker_token="tok",
        payload={"query": "GOOG"},
        timeout_ms=5000,
    )
    assert parsed_err["ok"] is False
    assert parsed_err["error"]["code"] == "PROVIDER_RATE_LIMITED"

    # 2. HTTP error with non-JSON body -> raises WebSearchBrokerError(code="BROKER_HTTP_ERROR")
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url="http://127.0.0.1:18789/dalton-broker/v1/x-search",
        code=502,
        msg="Bad Gateway",
        hdrs=Message(),
        fp=io.BytesIO(b"<html>bad gateway</html>"),
    )
    with pytest.raises(WebSearchBrokerError) as exc_info:
        invoke_broker_x_search(
            broker_url="http://127.0.0.1:18789/dalton-broker/v1",
            broker_token="tok",
            payload={"query": "GOOG"},
            timeout_ms=5000,
        )
    assert exc_info.value.code == "BROKER_HTTP_ERROR"

    # 3. Connection error (URLError) -> raises WebSearchBrokerError(code="BROKER_UNAVAILABLE")
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")
    with pytest.raises(WebSearchBrokerError) as exc_info:
        invoke_broker_x_search(
            broker_url="http://127.0.0.1:18789/dalton-broker/v1",
            broker_token="tok",
            payload={"query": "GOOG"},
            timeout_ms=5000,
        )
    assert exc_info.value.code == "BROKER_UNAVAILABLE"

    # 4. Response exceeding max_response_bytes -> raises WebSearchBrokerError(code="FRAME_TOO_LARGE")
    mock_resp_large = create_autospec(http.client.HTTPResponse, instance=True)
    mock_resp_large.__enter__.return_value = mock_resp_large
    mock_resp_large.read.return_value = b"x" * 128
    mock_urlopen.side_effect = None
    mock_urlopen.return_value = mock_resp_large
    with pytest.raises(WebSearchBrokerError) as exc_info:
        invoke_broker_x_search(
            broker_url="http://127.0.0.1:18789/dalton-broker/v1",
            broker_token="tok",
            payload={"query": "GOOG"},
            timeout_ms=5000,
            max_response_bytes=64,
        )
    assert exc_info.value.code == "FRAME_TOO_LARGE"

    # 5. Non-JSON response -> raises WebSearchBrokerError(code="INVALID_JSON")
    mock_resp_bad_json = create_autospec(http.client.HTTPResponse, instance=True)
    mock_resp_bad_json.__enter__.return_value = mock_resp_bad_json
    mock_resp_bad_json.read.return_value = b"not-json"
    mock_urlopen.return_value = mock_resp_bad_json
    with pytest.raises(WebSearchBrokerError) as exc_info:
        invoke_broker_x_search(
            broker_url="http://127.0.0.1:18789/dalton-broker/v1",
            broker_token="tok",
            payload={"query": "GOOG"},
            timeout_ms=5000,
        )
    assert exc_info.value.code == "INVALID_JSON"


@pytest.mark.asyncio
async def test_bridge_mixed_web_and_x_search_queue_on_shared_semaphore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "broker.token"
    token_file.write_text("broker-secret-token\n", encoding="utf-8")
    token_file.chmod(0o600)

    monkeypatch.delenv("DALTON_BROKER_TOKEN", raising=False)
    monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_MAX_INFLIGHT", "1")
    monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_QUEUE_TIMEOUT_S", "5.0")
    monkeypatch.setenv("DALTON_BROKER_TOKEN_PATH", str(token_file))

    cfg = WebSearchBridgeConfig()
    bridge = WebSearchBridge(cfg)

    order: list[str] = []
    mock_web_invoke = create_autospec(invoke_broker_over_socket)
    mock_x_invoke = create_autospec(invoke_broker_x_search)

    def _web_side_effect(**kwargs: object) -> dict:
        q = str(kwargs["query"])
        order.append(f"start:web:{q}")
        import time as _time

        _time.sleep(0.06)
        order.append(f"end:web:{q}")
        return {
            "ok": True,
            "provider": "gemini",
            "result": {"content": [{"type": "text", "text": '{"content":"web ok","citations":[]}'}]},
        }

    def _x_side_effect(**kwargs: object) -> dict:
        assert kwargs["broker_token"] == "broker-secret-token"
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        q = str(payload["query"])
        order.append(f"start:x:{q}")
        import time as _time

        _time.sleep(0.06)
        order.append(f"end:x:{q}")
        return {
            "ok": True,
            "provider": "xai",
            "result": {"details": {"query": q, "content": "x ok", "citations": []}},
        }

    mock_web_invoke.side_effect = _web_side_effect
    mock_x_invoke.side_effect = _x_side_effect
    monkeypatch.setattr(bridge_mod, "invoke_broker_over_socket", mock_web_invoke)
    monkeypatch.setattr(bridge_mod, "invoke_broker_x_search", mock_x_invoke)

    (s_web, r_web), (s_x, r_x) = await asyncio.gather(
        bridge.handle_search({"query": "q_web", "count": 3}),
        bridge.handle_x_search({"query": "q_x"}),
    )

    assert s_web == 200 and r_web["ok"] is True
    assert s_x == 200 and r_x["ok"] is True
    assert order in (
        ["start:web:q_web", "end:web:q_web", "start:x:q_x", "end:x:q_x"],
        ["start:x:q_x", "end:x:q_x", "start:web:q_web", "end:web:q_web"],
    )


@pytest.mark.asyncio
async def test_bridge_retries_when_broker_returns_bridge_busy_and_times_out_when_exhausted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "broker.token"
    token_file.write_text("broker-secret-token\n", encoding="utf-8")
    token_file.chmod(0o600)

    monkeypatch.delenv("DALTON_BROKER_TOKEN", raising=False)
    monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_MAX_INFLIGHT", "1")
    monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_QUEUE_TIMEOUT_S", "0.25")
    monkeypatch.setenv("DALTON_BROKER_TOKEN_PATH", str(token_file))

    cfg = WebSearchBridgeConfig()
    bridge = WebSearchBridge(cfg)

    mock_x_invoke = create_autospec(invoke_broker_x_search)
    busy_reply = {
        "ok": False,
        "error": {"code": "bridge_busy", "message": "OpenClaw broker concurrency limit reached"},
    }
    ok_reply = {
        "ok": True,
        "provider": "xai",
        "result": {"details": {"query": "GOOG", "content": "recovered", "citations": []}},
    }
    mock_x_invoke.side_effect = [busy_reply, busy_reply, ok_reply]
    monkeypatch.setattr(bridge_mod, "invoke_broker_x_search", mock_x_invoke)

    status_ok, payload_ok = await bridge.handle_x_search({"query": "GOOG"})
    assert status_ok == 200
    assert payload_ok["ok"] is True
    assert mock_x_invoke.call_count == 3

    mock_x_busy = create_autospec(invoke_broker_x_search)
    mock_x_busy.return_value = busy_reply
    monkeypatch.setattr(bridge_mod, "invoke_broker_x_search", mock_x_busy)

    status_busy, payload_busy = await bridge.handle_x_search({"query": "GOOG"})
    assert status_busy == 503
    assert payload_busy["ok"] is False
    assert payload_busy["error"]["code"] == "bridge_busy"
    assert mock_x_busy.call_count >= 2


# Sentence-level attribution (lead, round 3 review): a line may hold several sentences and a
# sentence may wrap across lines; neither may hand an uncited claim or a shared sentence to a post.
_U1 = "https://x.com/a/status/2112925226740683159"
_U2 = "https://x.com/b/status/2112609917127590040"


def test_summary_excerpt_takes_only_the_cited_sentence_of_a_line() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    # Running prose gets no excerpt at all: the uncited first sentence must never ride along.
    got = _extract_unique_summary_excerpt("公司已发布财报。市场讨论听证会。[1]", _U1, "2112925226740683159", 1)
    assert got is None


def test_summary_excerpt_empty_when_one_sentence_wraps_across_lines_citing_two_posts() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    text = "讨论听证会[1]，\n并关注监管影响[2]。"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None
    assert _extract_unique_summary_excerpt(text, _U2, "2112609917127590040", 2) is None


def test_summary_excerpt_english_sentences_with_trailing_footnotes() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    text = "Pichai says Cloud is forgoing revenue. [1] Analysts disagree. [2]"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None
    assert _extract_unique_summary_excerpt(text, _U2, "2112609917127590040", 2) is None


def test_summary_excerpt_keeps_a_multi_sentence_list_item_quote_whole() -> None:
    """xAI's real shape: one list item per post, a multi-sentence quote, a `[[n]](url)` citation link."""
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    text = (
        "- **@a** (high engagement: 52 likes):  \n"
        "  “AI demand is outrunning compute. Cloud is forgoing revenue. Capex is guided up…”"
        f"[[1]]({_U1})\n"
        f"- **@b** adds: “Compute is the scarcest asset.”[[2]]({_U2})"
    )
    got = _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1)
    assert got is not None
    assert got.startswith("@a (high engagement: 52 likes):")
    assert "AI demand is outrunning compute. Cloud is forgoing revenue. Capex is guided up" in got
    assert "[" not in got and "**" not in got and "scarcest" not in got


def test_summary_excerpt_keeps_leading_dates_and_numbers() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    got = _extract_unique_summary_excerpt(f"- 10/21 已发布财报。[[1]]({_U1})", _U1, "2112925226740683159", 1)
    assert got == "10/21 已发布财报。"



def test_summary_excerpt_none_for_codex_prose_counterexamples() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    for text in (
        f"甲。[[1]]({_U1})乙。",
        "Revenue grew in 2025. Demand fell. [1]",
        "According to Acme Inc. [1] and Beta [2], demand rose.",
    ):
        assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None, text



def test_summary_excerpt_stops_at_unindented_prose_after_the_cited_item() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    text = f"- **@a** says: “Compute is scarce.”[[1]]({_U1})\nOverall, sentiment turned negative after earnings."
    got = _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1)
    assert got == "@a says: “Compute is scarce.”"



def test_summary_excerpt_item_ends_after_bare_footnote_or_url_citation() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    got = _extract_unique_summary_excerpt("- 甲认为需求增长。[1]\n总体市场情绪转弱。", _U1, "2112925226740683159", 1)
    assert got == "甲认为需求增长。"
    got = _extract_unique_summary_excerpt(f"- 甲认为需求增长。 {_U1}\n总体市场情绪转弱。", _U1, "2112925226740683159", 1)
    assert got == "甲认为需求增长。"


def test_summary_excerpt_none_when_list_item_cites_a_second_post_on_the_next_line() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    text = f"- 两位作者都认为需求增长。[[1]]({_U1})\n[[2]]({_U2})"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None
    assert _extract_unique_summary_excerpt(text, _U2, "2112609917127590040", 2) is None



def test_summary_excerpt_year_at_line_start_is_not_a_list_item() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    assert _extract_unique_summary_excerpt("2025. Demand fell. [1]", _U1, "2112925226740683159", 1) is None
    assert _extract_unique_summary_excerpt(f"2025. Demand fell. [[1]]({_U1})", _U1, "2112925226740683159", 1) is None
    # A real 1-2 digit ordered item still works.
    assert _extract_unique_summary_excerpt(f"1. Demand fell. [[1]]({_U1})", _U1, "2112925226740683159", 1) == "Demand fell."



def test_summary_excerpt_item_ends_after_www_status_url() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    u = "https://www.x.com/a/status/2112925226740683159"
    got = _extract_unique_summary_excerpt(f"- 需求增长。 {u}\n总体情绪转弱。", u, "2112925226740683159", 1)
    assert got == "需求增长。"


def test_summary_excerpt_none_when_indented_continuation_after_blank_line_cites_another_post() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    text = "- 两位作者都认为需求增长。[1]\n\n  [2]"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None



def test_summary_excerpt_citation_only_line_with_trailing_stop_still_joins_the_item() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    text = f"- 两位作者都认为需求增长。[[1]]({_U1})\n[[2]]({_U2})。"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None


def test_summary_excerpt_non_status_url_with_prose_is_not_a_citation_line() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    text = f"- 需求增长。[[1]]({_U1})\nhttps://reuters.com/a。总体市场情绪转弱。"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) == "需求增长。"



def test_summary_excerpt_url_query_suffix_does_not_swallow_following_prose() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    text = f"- 需求增长。[[1]]({_U1})\n{_U1}?s=20。总体市场情绪转弱。"
    got = _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1)
    assert got is None or "总体市场情绪转弱" not in got


def test_summary_excerpt_bracketed_second_citation_joins_the_item() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    text = f"- 两位作者都认为需求增长。[[1]]({_U1})\n（[[2]]({_U2})）。"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None



def test_summary_excerpt_status_url_with_query_then_prose_is_not_a_citation_line() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    text = f"- 需求增长。[[1]]({_U1})\n{_U1}?s=20。总体市场情绪转弱。"
    got = _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1)
    assert got == "需求增长。"


def test_summary_excerpt_quoted_second_citation_line_joins_the_item() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    text = f"- 两位作者都认为需求增长。[[1]]({_U1})\n「[[2]]({_U2})」…"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None



def test_summary_excerpt_crlf_citation_only_line_joins_the_item() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    text = "- 两位作者都认为需求增长。[1]\r\n[2]\r\n总体情绪转弱。"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None


def test_summary_excerpt_second_bare_status_url_with_path_and_query_joins_the_item() -> None:
    from src.dalton.web_search.x_search import _extract_unique_summary_excerpt

    for suffix in ("/photo/1?s=20", "?s=20#details"):
        text = f"- 两位作者都认为需求增长。[[1]]({_U1})\n{_U2}{suffix}\n总体情绪转弱。"
        assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None, suffix
