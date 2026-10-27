"""Empty-generation classification and grounding-redirect resolution for web search."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import create_autospec

import httpx
import pytest

from src.dalton.web_search.provider import (
    EMPTY_GENERATION_MESSAGE,
    BRIDGE_UNAVAILABLE_MESSAGE,
    OpenClawSearchBridgeUnavailableError,
    OpenClawSearchEmptyGenerationError,
    OpenClawSearchWrapper,
    _raise_for_bridge_error_payload,
    build_web_search_tool,
)
from src.dalton.web_search.redirects import (
    GROUNDING_REDIRECT_HOST,
    is_grounding_redirect,
    resolve_grounding_redirects,
)

GROUNDING_URL = (
    f"https://{GROUNDING_REDIRECT_HOST}/grounding-api-redirect/Ae%3D%3D"
)
SOURCE_URL = "https://www.reuters.com/markets/deals/nvidia-quarter"


def _ok_raw() -> dict:
    return {
        "ok": True,
        "provider": "gemini",
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "kind": "answer",
                            "provider": "gemini",
                            "content": "NVIDIA beat estimates.",
                            "citations": [{"title": "Reuters", "url": SOURCE_URL}],
                        }
                    ),
                }
            ]
        },
    }


class TestEmptyGenerationClassification:
    def test_no_final_answer_is_not_bridge_unavailable(self):
        """The observed PROVIDER_ERROR payload must classify as empty generation."""
        with pytest.raises(OpenClawSearchEmptyGenerationError) as exc_info:
            _raise_for_bridge_error_payload(
                {
                    "error": {
                        "code": "PROVIDER_ERROR",
                        "message": "Gemini search returned no final answer (STOP).",
                    }
                }
            )
        assert BRIDGE_UNAVAILABLE_MESSAGE not in str(exc_info.value)

    def test_other_provider_errors_stay_bridge_unavailable(self):
        with pytest.raises(OpenClawSearchBridgeUnavailableError):
            _raise_for_bridge_error_payload(
                {
                    "error": {
                        "code": "PROVIDER_ERROR",
                        "message": "upstream 500 while signing request",
                    }
                }
            )

    def test_generic_empty_response_wording_is_not_empty_generation(self):
        """Only the known "no final answer" wording (or an explicit code)
        classifies; a broader "empty response" message stays a bridge error."""
        with pytest.raises(OpenClawSearchBridgeUnavailableError):
            _raise_for_bridge_error_payload(
                {
                    "error": {
                        "code": "PROVIDER_ERROR",
                        "message": "provider returned an empty response",
                    }
                }
            )

    def test_explicit_empty_generation_code_classifies(self):
        with pytest.raises(OpenClawSearchEmptyGenerationError):
            _raise_for_bridge_error_payload(
                {"error": {"code": "no_final_answer", "message": ""}}
            )

    @pytest.mark.asyncio
    async def test_tool_retries_once_then_succeeds(self):
        wrapper = create_autospec(OpenClawSearchWrapper, instance=True)
        wrapper.raw_results.side_effect = [
            OpenClawSearchEmptyGenerationError(),
            _ok_raw(),
        ]
        tool = build_web_search_tool(max_results=5, wrapper=wrapper)
        content, artifact = await tool.coroutine(query="NVDA quarter")
        assert artifact.get("answer")
        assert wrapper.raw_results.await_count == 2

    @pytest.mark.asyncio
    async def test_tool_returns_rephrase_hint_after_failed_retry(self):
        wrapper = create_autospec(OpenClawSearchWrapper, instance=True)
        wrapper.raw_results.side_effect = [
            OpenClawSearchEmptyGenerationError(),
            OpenClawSearchEmptyGenerationError(),
        ]
        tool = build_web_search_tool(max_results=5, wrapper=wrapper)
        content, artifact = await tool.coroutine(query="NVDA quarter")
        assert content == EMPTY_GENERATION_MESSAGE
        assert "换个说法" in content
        assert BRIDGE_UNAVAILABLE_MESSAGE not in content
        assert artifact["code"] == "empty_generation"
        assert wrapper.raw_results.await_count == 2


def _error_response(message: str) -> dict:
    return {
        "ok": False,
        "error": {"code": "PROVIDER_ERROR", "message": message},
    }


def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


class TestGroundingRedirectResolution:
    def test_is_grounding_redirect(self):
        assert is_grounding_redirect(GROUNDING_URL)
        assert not is_grounding_redirect(SOURCE_URL)
        assert not is_grounding_redirect("https://vertexaisearch.cloud.google.com/other")

    @pytest.mark.asyncio
    async def test_source_url_returned_without_requesting_the_source(self):
        """The Location off the grounding host IS the answer: the source is
        never fetched, so a source that would 403 cannot fail the resolution."""
        source_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host != GROUNDING_REDIRECT_HOST:
                source_requests.append(str(request.url))
                return httpx.Response(403)
            return httpx.Response(302, headers={"location": SOURCE_URL})

        rows = [{"type": "page", "title": "Reuters", "url": GROUNDING_URL, "content": ""}]
        cards = [{"title": "Reuters", "url": GROUNDING_URL, "favicon": "", "snippet": ""}]
        await resolve_grounding_redirects(
            rows, cards, transport=_mock_transport(handler)
        )
        assert rows[0]["url"] == SOURCE_URL
        assert rows[0]["redirect_url"] == GROUNDING_URL
        assert "url_unresolved" not in rows[0]
        assert cards[0]["url"] == SOURCE_URL
        assert source_requests == []

    @pytest.mark.asyncio
    async def test_chain_through_second_grounding_hop(self):
        second_hop = f"https://{GROUNDING_REDIRECT_HOST}/grounding-api-redirect/stage2"

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("stage2"):
                return httpx.Response(302, headers={"location": SOURCE_URL})
            return httpx.Response(302, headers={"location": second_hop})

        rows = [{"type": "page", "title": "Reuters", "url": GROUNDING_URL, "content": ""}]
        await resolve_grounding_redirects(rows, [], transport=_mock_transport(handler))
        assert rows[0]["url"] == SOURCE_URL
        assert rows[0]["redirect_url"] == GROUNDING_URL

    @pytest.mark.asyncio
    async def test_one_never_ending_resolution_does_not_hold_the_batch(self):
        """A resolution that never lands is dropped at the batch deadline; the
        rest of the results still come back in time."""
        never_lands = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("2"):
                return httpx.Response(302, headers={"location": SOURCE_URL})
            await never_lands.wait()
            return httpx.Response(302, headers={"location": SOURCE_URL})

        rows = [
            {"type": "page", "title": "A", "url": GROUNDING_URL, "content": ""},
            {"type": "page", "title": "B", "url": GROUNDING_URL + "2", "content": ""},
        ]
        loop = asyncio.get_event_loop()
        started = loop.time()
        await resolve_grounding_redirects(
            rows, [], timeout_s=0.3, transport=_mock_transport(handler)
        )
        elapsed = loop.time() - started
        never_lands.set()

        assert elapsed < 2.0
        assert rows[0]["url_unresolved"] is True
        assert rows[0]["url"] == GROUNDING_URL
        assert rows[1]["url"] == SOURCE_URL

    @pytest.mark.asyncio
    async def test_timeout_keeps_original_link_and_marks_unresolved(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.5)
            return httpx.Response(302, headers={"location": SOURCE_URL})

        rows = [{"type": "page", "title": "Reuters", "url": GROUNDING_URL, "content": ""}]
        cards: list[dict] = []
        await resolve_grounding_redirects(
            rows, cards, timeout_s=0.05, transport=_mock_transport(handler)
        )
        assert rows[0]["url"] == GROUNDING_URL
        assert rows[0]["url_unresolved"] is True
        assert "redirect_url" not in rows[0]

    @pytest.mark.asyncio
    async def test_intranet_redirect_target_is_refused(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8080/admin"})

        rows = [{"type": "page", "title": "Evil", "url": GROUNDING_URL, "content": ""}]
        await resolve_grounding_redirects(
            rows, [], transport=_mock_transport(handler)
        )
        assert rows[0]["url"] == GROUNDING_URL
        assert rows[0]["url_unresolved"] is True

    @pytest.mark.asyncio
    async def test_non_grounding_links_are_untouched(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("no request expected")

        rows = [{"type": "page", "title": "Reuters", "url": SOURCE_URL, "content": ""}]
        await resolve_grounding_redirects(
            rows, [], transport=_mock_transport(handler)
        )
        assert rows[0]["url"] == SOURCE_URL
        assert "url_unresolved" not in rows[0]

    @pytest.mark.asyncio
    async def test_resolutions_run_concurrently(self):
        # The first link resolves only if the second request is already in
        # flight: run sequentially, the event never gets set within the wait
        # and the first row would come back unresolved.
        second_arrived = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host != GROUNDING_REDIRECT_HOST:
                return httpx.Response(200)
            if str(request.url).endswith("2"):
                second_arrived.set()
                return httpx.Response(302, headers={"location": SOURCE_URL})
            try:
                await asyncio.wait_for(second_arrived.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                return httpx.Response(500)
            return httpx.Response(302, headers={"location": SOURCE_URL})

        rows = [
            {"type": "page", "title": "A", "url": GROUNDING_URL, "content": ""},
            {"type": "page", "title": "B", "url": GROUNDING_URL + "2", "content": ""},
        ]
        await resolve_grounding_redirects(
            rows, [], timeout_s=4.0, transport=_mock_transport(handler)
        )
        assert all(row["url"] == SOURCE_URL for row in rows)
        assert not any(row.get("url_unresolved") for row in rows)
