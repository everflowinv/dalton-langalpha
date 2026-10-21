"""Unit tests for Dalton's OpenClaw web search broker protocol, bridge, and provider."""

from __future__ import annotations

import json
from unittest.mock import create_autospec

import httpx
import pytest

from scripts.dalton.web_search_bridge import (
    WebSearchBridge,
    WebSearchBridgeConfig,
    _extract_supplied_token,
)
from src.dalton.web_search.broker_protocol import (
    DEFAULT_CLIENT_ID,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_PROFILE_ID,
    DEFAULT_QUEUE_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_MS,
    HTTP_TIMEOUT_MARGIN_SECONDS,
    PROTOCOL_VERSION,
    WebSearchBrokerError,
    authentication_payload,
    build_core_request,
    canonical_json,
    compute_http_timeout_seconds,
    compute_mac,
    invoke_broker_over_socket,
    sign_request,
)
from src.dalton.web_search.provider import (
    ANSWER_ONLY_HEADING,
    BRIDGE_BUSY_MESSAGE,
    BRIDGE_UNAVAILABLE_MESSAGE,
    EMPTY_RESULTS_MESSAGE,
    OpenClawSearchBridgeUnavailableError,
    OpenClawSearchEmptyResultError,
    OpenClawSearchRequestError,
    OpenClawSearchWrapper,
    RATE_LIMITED_MESSAGE,
    build_web_search_tool,
    map_broker_response,
)
from src.tools.web.manifest import CAPABILITY_SEARCH, get_web_providers, providers_with_capability


class TestBrokerSignatureCompatibility:
    """Verify HMAC-SHA-256 signing matches `.agy/ref/auth.mjs` & `protocol.mjs`."""

    def test_known_vector_matches_reference_mac(self) -> None:
        secret = "0123456789abcdef" * 4
        core = build_core_request(
            query="NVIDIA Blackwell supply chain",
            count=5,
            date_after="2026-01-01",
            date_before="2026-10-21",
            timeout_ms=60000,
            call_ref="credential-use:dalton-web-search-0001",
            profile_id=DEFAULT_PROFILE_ID,
        )
        signed = sign_request(
            core,
            secret=secret,
            client_id=DEFAULT_CLIENT_ID,
            timestamp_ms=1760000000000,
            nonce="a1b2c3d4e5f60718293a4b5c6d7e8f90",
        )

        expected_unsigned_canonical = (
            '{"auth":{"clientId":"client:dalton-core",'
            '"nonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90",'
            '"scheme":"hmac-sha256-v1","timestampMs":1760000000000},'
            '"callRef":"credential-use:dalton-web-search-0001",'
            '"count":5,"dateAfter":"2026-01-01","dateBefore":"2026-10-21",'
            '"profileId":"profile:web-search",'
            '"query":"NVIDIA Blackwell supply chain",'
            '"schemaVersion":"0.1","timeoutMs":60000}'
        )
        assert canonical_json(authentication_payload(signed)) == expected_unsigned_canonical

        expected_mac = "698df2ed43dded69107c7296f2fb97e9f96fbaf9440a18924ddff2dded5ae6f3"
        assert signed["auth"]["mac"] == expected_mac
        assert compute_mac(secret, signed) == expected_mac

    def test_date_bounds_must_be_supplied_together(self) -> None:
        with pytest.raises(WebSearchBrokerError, match="supplied together") as exc_info:
            build_core_request(
                query="TSMC CoWoS",
                count=3,
                date_after="2026-01-01",
                date_before=None,
            )
        assert exc_info.value.code == "INVALID_REQUEST"

    @pytest.mark.parametrize(
        ("date_after", "date_before"),
        [
            (None, None),
            (20260101, "2026-10-21"),
            ("2026-01-01", 20261021),
            (["2026-01-01"], "2026-10-21"),
            ("2026/01/01", "2026-10-21"),
            ("2026-02-30", "2026-10-21"),
            ("2026-10-22", "2026-10-21"),
        ],
    )
    def test_non_string_or_invalid_date_bounds_raise_invalid_request(
        self, date_after: object, date_before: object
    ) -> None:
        with pytest.raises(WebSearchBrokerError) as exc_info:
            build_core_request(
                query="TSMC CoWoS",
                count=3,
                date_after=date_after,
                date_before=date_before,
            )
        assert exc_info.value.code == "INVALID_REQUEST"

    def test_http_timeout_derived_from_queue_plus_broker_timeout_plus_margin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert DEFAULT_QUEUE_TIMEOUT_SECONDS == 30.0
        assert HTTP_TIMEOUT_MARGIN_SECONDS == 15.0
        assert DEFAULT_TIMEOUT_MS == 60_000
        assert DEFAULT_HTTP_TIMEOUT_SECONDS == 105.0
        assert compute_http_timeout_seconds(45_000, queue_timeout_seconds=30.0) == 90.0

        monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_TOKEN", "fake-bridge-token")
        cfg = WebSearchBridgeConfig()
        assert cfg.http_timeout_s == DEFAULT_HTTP_TIMEOUT_SECONDS


class TestBrokerResultMapping:
    """Verify broker tool-result envelope mapping into LangAlpha web_search format."""

    def test_maps_gemini_answer_and_citations_without_copying_answer_into_empty_citations(
        self,
    ) -> None:
        inner_payload = {
            "kind": "answer",
            "provider": "gemini",
            "query": "Apple Inc SEC CIK",
            "tookMs": 1200,
            "content": (
                '\n<<<EXTERNAL_UNTRUSTED_CONTENT id="a67f653e1c3d3fea">>>\n'
                "Source: Web Search\n---\n"
                "The SEC CIK for Apple Inc. is 0000320193.\n"
                '<<<END_EXTERNAL_UNTRUSTED_CONTENT id="a67f653e1c3d3fea">>>'
            ),
            "citations": [
                {
                    "url": "https://www.sec.gov/edgar/searchedgar/companysearch",
                    "title": (
                        '\n<<<EXTERNAL_UNTRUSTED_CONTENT id="7a8ca2bfde406bdd">>>\n'
                        "Source: Web Search\n---\n"
                        "sec.gov\n"
                        '<<<END_EXTERNAL_UNTRUSTED_CONTENT id="7a8ca2bfde406bdd">>>'
                    ),
                },
                {
                    "url": "https://edgar.tools/cik/0000320193",
                    "title": "edgar.tools",
                    "snippet": "Apple Inc. CIK 0000320193 filing index.",
                    "publishedDate": "2026-10-01",
                },
            ],
        }
        broker_reply = {
            "schemaVersion": PROTOCOL_VERSION,
            "ok": True,
            "provider": "gemini",
            "providerRequestId": "req-gemini-001",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(inner_payload, ensure_ascii=False),
                    }
                ]
            },
        }

        cleaned, artifact = map_broker_response(
            broker_reply, query="Apple Inc SEC CIK", response_time=1.234
        )

        assert len(cleaned) == 2
        assert cleaned[0] == {
            "type": "page",
            "title": "sec.gov",
            "url": "https://www.sec.gov/edgar/searchedgar/companysearch",
            "content": "",
            "score": 0.0,
        }
        assert cleaned[1] == {
            "type": "page",
            "title": "edgar.tools",
            "url": "https://edgar.tools/cik/0000320193",
            "content": "Apple Inc. CIK 0000320193 filing index.",
            "score": 0.0,
            "date": "2026-10-01",
        }

        assert artifact["type"] == "web_search"
        assert artifact["search_engine"] == "openclaw"
        assert artifact["provider"] == "gemini"
        assert artifact["request_id"] == "req-gemini-001"
        assert artifact["total_results"] == 2
        assert artifact["answer"] == "The SEC CIK for Apple Inc. is 0000320193."
        assert artifact["results"][0]["title"] == "sec.gov"
        assert artifact["results"][0]["url"] == "https://www.sec.gov/edgar/searchedgar/companysearch"
        assert artifact["results"][0]["snippet"] == ""
        assert artifact["results"][1]["snippet"] == "Apple Inc. CIK 0000320193 filing index."

    def test_maps_list_results_payload(self) -> None:
        inner_payload = {
            "provider": "gemini",
            "answer": "Synthesized overview",
            "results": [
                {
                    "title": "Result One",
                    "url": "https://example.com/one",
                    "content": "First result snippet",
                    "score": 0.95,
                    "published_date": "2026-10-15",
                },
                {
                    "title": "Result Two Without Snippet",
                    "url": "https://example.com/two",
                },
            ],
        }
        broker_reply = {
            "schemaVersion": PROTOCOL_VERSION,
            "ok": True,
            "provider": "gemini",
            "providerRequestId": "req-list-002",
            "result": {
                "content": [{"type": "text", "text": json.dumps(inner_payload)}]
            },
        }

        cleaned, artifact = map_broker_response(
            broker_reply, query="test query", response_time=0.5
        )
        assert cleaned == [
            {
                "type": "page",
                "title": "Result One",
                "url": "https://example.com/one",
                "content": "First result snippet",
                "score": 0.95,
                "date": "2026-10-15",
            },
            {
                "type": "page",
                "title": "Result Two Without Snippet",
                "url": "https://example.com/two",
                "content": "",
                "score": 0.0,
            },
        ]
        assert artifact["search_engine"] == "openclaw"
        assert artifact["answer"] == "Synthesized overview"
        assert artifact["results"][0]["snippet"] == "First result snippet"
        assert artifact["results"][1]["snippet"] == ""

    @pytest.mark.asyncio
    async def test_answer_only_without_citations_does_not_fabricate_result_page(self) -> None:
        answer_only_reply = {
            "schemaVersion": PROTOCOL_VERSION,
            "ok": True,
            "provider": "gemini",
            "providerRequestId": "req-answer-only-001",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "kind": "answer",
                                "provider": "gemini",
                                "content": "Apple Inc.'s fiscal year ends on the last Saturday of September.",
                                "citations": [],
                            }
                        ),
                    }
                ]
            },
        }
        cleaned, artifact = map_broker_response(
            answer_only_reply, query="Apple fiscal year end"
        )
        assert cleaned == []
        assert artifact["results"] == []
        assert artifact["total_results"] == 0
        assert (
            artifact["answer"]
            == "Apple Inc.'s fiscal year ends on the last Saturday of September."
        )

        mock_wrapper = create_autospec(OpenClawSearchWrapper, instance=True)
        mock_wrapper.raw_results.return_value = answer_only_reply
        tool = build_web_search_tool(max_results=5, wrapper=mock_wrapper)
        content, tool_artifact = await tool.coroutine(query="Apple fiscal year end")
        assert isinstance(content, str)
        assert content.startswith(ANSWER_ONLY_HEADING)
        assert "Apple Inc.'s fiscal year ends on the last Saturday of September." in content
        assert tool_artifact["results"] == []
        assert (
            tool_artifact["answer"]
            == "Apple Inc.'s fiscal year ends on the last Saturday of September."
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "inner_payload",
        [
            {"kind": "answer", "provider": "gemini", "content": "", "citations": []},
            {"provider": "gemini", "results": [{}]},
            {"provider": "gemini", "citations": ["", "   ", {}]},
            {"provider": "gemini", "results": [{"url": "", "title": "  ", "content": ""}]},
        ],
    )
    async def test_empty_or_blank_items_payload_returns_explicit_empty_result_error(
        self, inner_payload: dict[str, object]
    ) -> None:
        empty_broker_reply = {
            "schemaVersion": PROTOCOL_VERSION,
            "ok": True,
            "provider": "gemini",
            "providerRequestId": "req-empty-001",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(inner_payload),
                    }
                ]
            },
        }
        with pytest.raises(OpenClawSearchEmptyResultError, match=EMPTY_RESULTS_MESSAGE):
            map_broker_response(empty_broker_reply, query="obscure query")

        mock_wrapper = create_autospec(OpenClawSearchWrapper, instance=True)
        mock_wrapper.raw_results.return_value = empty_broker_reply
        tool = build_web_search_tool(max_results=5, wrapper=mock_wrapper)
        content, artifact = await tool.coroutine(query="obscure query")
        assert content == EMPTY_RESULTS_MESSAGE
        assert artifact == {"error": EMPTY_RESULTS_MESSAGE, "query": "obscure query"}

    @pytest.mark.asyncio
    async def test_web_search_tool_uses_wrapper_and_documents_topic(self) -> None:
        mock_wrapper = create_autospec(OpenClawSearchWrapper, instance=True)
        mock_wrapper.raw_results.return_value = {
            "schemaVersion": PROTOCOL_VERSION,
            "ok": True,
            "provider": "gemini",
            "providerRequestId": "req-tool-001",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "kind": "answer",
                                "provider": "gemini",
                                "content": "NVIDIA Q3 revenue beat expectations.",
                                "citations": [
                                    {
                                        "title": "NVIDIA Investor Relations",
                                        "url": "https://investor.nvidia.com/news",
                                    }
                                ],
                            }
                        ),
                    }
                ]
            },
        }

        tool = build_web_search_tool(max_results=5, wrapper=mock_wrapper)
        assert "OpenClaw 搜索不区分 topic，忽略该参数" in (tool.description or "")
        content, artifact = await tool.coroutine(
            query="NVIDIA Q3 revenue",
            start_date="2026-10-01",
            end_date="2026-10-20",
        )

        mock_wrapper.raw_results.assert_awaited_once_with(
            query="NVIDIA Q3 revenue",
            max_results=5,
            date_after="2026-10-01",
            date_before="2026-10-20",
        )
        # The grounded answer leads the list as an answer_box the UI skips; results follow unchanged.
        assert isinstance(content, list)
        assert content[0] == {
            "type": "answer_box",
            "title": "OpenClaw 综合答案（来源见下）",
            "content": "NVIDIA Q3 revenue beat expectations.",
        }
        assert content[1]["title"] == "NVIDIA Investor Relations"
        assert content[1]["url"] == "https://investor.nvidia.com/news"
        assert content[1]["content"] == ""
        assert len(artifact["results"]) == 1
        assert artifact["answer"] == "NVIDIA Q3 revenue beat expectations."
        assert artifact["search_engine"] == "openclaw"


class TestBridgeUnavailableHandling:
    """Verify explicit error reporting, header-only auth, queue timeout, and manifest gating."""

    @pytest.mark.asyncio
    async def test_tool_returns_explicit_error_when_bridge_unreachable(self) -> None:
        mock_wrapper = create_autospec(OpenClawSearchWrapper, instance=True)
        mock_wrapper.raw_results.side_effect = OpenClawSearchBridgeUnavailableError(
            "All connection attempts failed"
        )

        tool = build_web_search_tool(max_results=5, wrapper=mock_wrapper)
        content, artifact = await tool.coroutine(query="Meta CAPEX guidance")

        assert isinstance(content, str)
        assert BRIDGE_UNAVAILABLE_MESSAGE in content
        assert content != []
        assert "error" in artifact
        assert BRIDGE_UNAVAILABLE_MESSAGE in artifact["error"]
        assert artifact["query"] == "Meta CAPEX guidance"

    @pytest.mark.asyncio
    async def test_tool_returns_explicit_error_on_httpx_connect_error(self) -> None:
        mock_wrapper = create_autospec(OpenClawSearchWrapper, instance=True)
        mock_wrapper.raw_results.side_effect = httpx.ConnectError(
            "Connection refused"
        )

        tool = build_web_search_tool(max_results=5, wrapper=mock_wrapper)
        content, artifact = await tool.coroutine(query="TSMC N2 yield")

        assert isinstance(content, str)
        assert BRIDGE_UNAVAILABLE_MESSAGE in content
        assert BRIDGE_UNAVAILABLE_MESSAGE in artifact["error"]

    @pytest.mark.asyncio
    async def test_missing_bridge_token_reports_bridge_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DALTON_WEB_SEARCH_BRIDGE_TOKEN", raising=False)
        tool = build_web_search_tool(max_results=5)
        content, artifact = await tool.coroutine(query="ASML bookings")

        assert isinstance(content, str)
        assert BRIDGE_UNAVAILABLE_MESSAGE in content
        assert BRIDGE_UNAVAILABLE_MESSAGE in artifact["error"]

    @pytest.mark.asyncio
    async def test_host_bridge_forwards_search_and_handles_unavailable_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_TOKEN", "fake-bridge-token")
        cfg = WebSearchBridgeConfig()
        bridge = WebSearchBridge(cfg)

        mock_invoke = create_autospec(invoke_broker_over_socket)
        mock_invoke.return_value = {
            "schemaVersion": PROTOCOL_VERSION,
            "ok": True,
            "provider": "gemini",
            "providerRequestId": "req-bridge-1",
            "result": {"content": [{"type": "text", "text": "{}"}]},
        }
        monkeypatch.setattr(
            "scripts.dalton.web_search_bridge.invoke_broker_over_socket",
            mock_invoke,
        )

        status, reply = await bridge.handle_search(
            {
                "query": "Broadcom AI ASIC",
                "count": 4,
                "dateAfter": "2026-09-01",
                "dateBefore": "2026-10-21",
                "timeoutMs": 45000,
            }
        )
        assert status == 200
        assert reply["ok"] is True
        mock_invoke.assert_called_once()
        call_kwargs = mock_invoke.call_args.kwargs
        assert call_kwargs["query"] == "Broadcom AI ASIC"
        assert call_kwargs["count"] == 4
        assert call_kwargs["date_after"] == "2026-09-01"
        assert call_kwargs["date_before"] == "2026-10-21"
        assert call_kwargs["timeout_ms"] == 45000

        mock_invoke.side_effect = WebSearchBrokerError(
            "web search broker socket is unavailable", code="BROKER_UNAVAILABLE"
        )
        status_err, reply_err = await bridge.handle_search(
            {"query": "Broadcom AI ASIC", "count": 4}
        )
        assert status_err == 503
        assert reply_err["ok"] is False
        assert reply_err["error"]["code"] == "BROKER_UNAVAILABLE"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_dates",
        [
            {"dateAfter": None, "dateBefore": None},
            {"dateAfter": None},
            {"dateBefore": None},
            {"dateAfter": "2026-01-01", "dateBefore": None},
            {"dateAfter": 20260101, "dateBefore": "2026-10-21"},
            {"dateAfter": "2026-01-01", "dateBefore": False},
            {"dateAfter": "2026/01/01", "dateBefore": "2026-10-21"},
            {"dateAfter": "2026-02-30", "dateBefore": "2026-10-21"},
            {"dateAfter": "2026-10-25", "dateBefore": "2026-10-21"},
        ],
    )
    async def test_host_bridge_rejects_bad_or_null_dates_with_400_not_503(
        self,
        monkeypatch: pytest.MonkeyPatch,
        bad_dates: dict[str, object],
    ) -> None:
        monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_TOKEN", "fake-bridge-token")
        cfg = WebSearchBridgeConfig()
        bridge = WebSearchBridge(cfg)

        status, reply = await bridge.handle_search(
            {
                "query": "Broadcom AI ASIC",
                "count": 3,
                **bad_dates,
            }
        )
        assert status == 400
        assert reply["ok"] is False
        assert reply["error"]["code"] == "INVALID_REQUEST"

    @pytest.mark.asyncio
    async def test_wrapper_and_tool_distinguish_bridge_error_codes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.tools.web.providers import _shared

        wrapper = OpenClawSearchWrapper(
            bridge_url="http://127.0.0.1:8961",
            bridge_token="fake-token",
        )
        tool = build_web_search_tool(max_results=5, wrapper=wrapper)

        def _make_status_error(status_code: int, body: dict[str, object] | str) -> httpx.HTTPStatusError:
            req = httpx.Request("POST", "http://127.0.0.1:8961/search")
            text = body if isinstance(body, str) else json.dumps(body)
            resp = httpx.Response(status_code, text=text, request=req)
            return httpx.HTTPStatusError(f"HTTP {status_code}", request=req, response=resp)

        mock_req_json = create_autospec(_shared.request_json)
        monkeypatch.setattr("src.dalton.web_search.provider.request_json", mock_req_json)

        # 1. bridge_busy -> "OpenClaw 网页搜索繁忙，请稍后重试"
        mock_req_json.side_effect = _make_status_error(
            503,
            {"ok": False, "error": {"code": "bridge_busy", "message": "concurrency queue timeout"}},
        )
        with pytest.raises(OpenClawSearchRequestError, match=BRIDGE_BUSY_MESSAGE) as busy_exc:
            await wrapper.raw_results("NVDA")
        assert busy_exc.value.code == "bridge_busy"
        content_busy, art_busy = await tool.coroutine(query="NVDA")
        assert content_busy == BRIDGE_BUSY_MESSAGE
        assert art_busy["code"] == "bridge_busy"

        # 2. INVALID_REQUEST -> "Search failed: 参数错误 ..."
        mock_req_json.side_effect = _make_status_error(
            400,
            {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "dateAfter is invalid"}},
        )
        content_param, art_param = await tool.coroutine(query="NVDA")
        assert isinstance(content_param, str)
        assert content_param.startswith("Search failed: 参数错误")
        assert "dateAfter is invalid" in content_param
        assert art_param["code"] == "INVALID_REQUEST"

        # 3. PROVIDER_RATE_LIMITED -> 限流提示
        mock_req_json.side_effect = _make_status_error(
            429,
            {"ok": False, "error": {"code": "PROVIDER_RATE_LIMITED", "message": "quota exceeded"}},
        )
        content_rl, art_rl = await tool.coroutine(query="NVDA")
        assert isinstance(content_rl, str)
        assert content_rl.startswith(RATE_LIMITED_MESSAGE)
        assert art_rl["code"] == "PROVIDER_RATE_LIMITED"

        # 4. Non-JSON 502 -> "OpenClaw 网页搜索桥不可用"
        mock_req_json.side_effect = _make_status_error(502, "Bad Gateway")
        content_unavail, _ = await tool.coroutine(query="NVDA")
        assert isinstance(content_unavail, str)
        assert content_unavail.startswith(BRIDGE_UNAVAILABLE_MESSAGE)

    @pytest.mark.asyncio
    async def test_host_bridge_returns_503_bridge_busy_when_semaphore_queue_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_TOKEN", "fake-bridge-token")
        monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_MAX_INFLIGHT", "1")
        monkeypatch.setenv("DALTON_WEB_SEARCH_BRIDGE_QUEUE_TIMEOUT_S", "0.02")
        cfg = WebSearchBridgeConfig()
        bridge = WebSearchBridge(cfg)

        await bridge.sem.acquire()
        try:
            status, reply = await bridge.handle_search(
                {"query": "Broadcom AI ASIC", "count": 2}
            )
        finally:
            bridge.sem.release()

        assert status == 503
        assert reply["ok"] is False
        assert reply["error"]["code"] == "bridge_busy"

    def test_bridge_token_extraction_accepts_headers_only(self) -> None:
        assert _extract_supplied_token({"authorization": "Bearer tok-123"}) == "tok-123"
        assert _extract_supplied_token({"x-bridge-token": "tok-456"}) == "tok-456"
        assert _extract_supplied_token({}) == ""

    def test_manifest_registers_openclaw_only_when_search_api_is_openclaw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.config.tool_settings import get_search_api

        mock_get_search_api = create_autospec(get_search_api)
        mock_get_search_api.return_value = "tavily"
        monkeypatch.setattr("src.config.tool_settings.get_search_api", mock_get_search_api)
        assert "openclaw" not in get_web_providers()
        assert "openclaw" not in providers_with_capability(CAPABILITY_SEARCH)

        mock_get_search_api.return_value = "openclaw"
        assert "openclaw" in get_web_providers()
        assert "openclaw" in providers_with_capability(CAPABILITY_SEARCH)
