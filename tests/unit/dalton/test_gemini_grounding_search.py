"""Gemini grounding web search: mapping, errors, cache, and provider selection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest

from src.dalton.web_search.gemini_grounding import (
    DEFAULT_TIMEOUT_SECONDS,
    GeminiGroundingClient,
    OpenClawSearchEmptyGenerationError,
    build_web_search_tool,
    to_broker_payload,
)
from src.utils.cache.redis_cache import RedisCacheClient

REPO_ROOT = Path(__file__).resolve().parents[3]
API_KEY = "gemini-test-key-not-in-url"
REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
SOURCE = "https://news.example/nvda"
ANSWER = "NVIDIA reported record data-center revenue."


def _sample(*, redirect: bool = True) -> dict[str, Any]:
    if redirect:
        chunks = [
            {"web": {"uri": REDIRECT, "title": "Redirected source"}},
            {"web": {"uri": "https://example.com/nvda", "title": "Example NVDA"}},
        ]
    else:
        chunks = [
            {"web": {"uri": "https://example.com/first", "title": "First"}},
            {"web": {"uri": "https://example.com/second", "title": "Second"}},
        ]
    return {
        "responseId": "resp-gemini-1",
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": ANSWER},
                        {"thought": True, "text": "hidden thought"},
                    ]
                },
                "groundingMetadata": {
                    "webSearchQueries": ["NVDA earnings"],
                    "groundingChunks": chunks,
                    "groundingSupports": [{"groundingChunkIndices": [1, 0]}],
                },
            }
        ],
    }


class _Cache:
    """In-memory stand-in with the real ``RedisCacheClient`` get/set signature."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.sets: list[tuple[str, int | None]] = []
        self.client = create_autospec(RedisCacheClient, instance=True)

        def fake_get(key: str) -> Any:
            return self.store.get(key)

        def fake_set(key: str, value: Any, ttl: int | None = None) -> bool:
            self.store[key] = json.loads(json.dumps(value))
            self.sets.append((key, ttl))
            return True

        self.client.get.side_effect = fake_get
        self.client.set.side_effect = fake_set

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "src.dalton.web_search.cache.get_cache_client",
            lambda: self.client,
        )


def _tool(handler, *, max_results: int = 5, redirect_handler=None):
    client = GeminiGroundingClient(
        api_key=API_KEY,
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(handler),
    )
    redirect_transport = (
        httpx.MockTransport(redirect_handler) if redirect_handler is not None else None
    )
    return build_web_search_tool(
        max_results=max_results,
        client=client,
        redirect_transport=redirect_transport,
    )


def test_sample_orders_citations_by_supports() -> None:
    broker = to_broker_payload(_sample(), query="NVDA earnings", limit=10)
    inner = json.loads(broker["result"]["content"][0]["text"])
    assert "web_search_queries" not in inner
    assert [item["url"] for item in inner["citations"]] == [
        "https://example.com/nvda",
        REDIRECT,
    ]
    assert broker["providerRequestId"] == "resp-gemini-1"
    assert "hidden thought" not in inner["answer"]


def test_missing_metadata_and_text_is_empty_generation() -> None:
    with pytest.raises(OpenClawSearchEmptyGenerationError):
        to_broker_payload(
            {"candidates": [{"content": {"parts": [{"text": "  "}]}}]},
            query="NVDA",
            limit=5,
        )


def test_default_timeout_is_sixty_seconds() -> None:
    assert GeminiGroundingClient(api_key=API_KEY)._timeout == DEFAULT_TIMEOUT_SECONDS
    assert DEFAULT_TIMEOUT_SECONDS == 60.0


@pytest.mark.asyncio
async def test_generate_content_maps_answer_cards_and_resolved_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_sample())

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "vertexaisearch.cloud.google.com"
        return httpx.Response(302, headers={"location": SOURCE})

    cache = _Cache()
    cache.install(monkeypatch)
    tool = _tool(handler, redirect_handler=redirect_handler)
    content, artifact = await tool.coroutine(
        query="NVDA earnings",
        start_date="2026-01-01",
        end_date="2026-01-31",
        topic="news",
    )

    request = seen[0]
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent"
    )
    assert request.headers["x-goog-api-key"] == API_KEY
    assert API_KEY not in str(request.url)
    assert API_KEY not in request.content.decode()
    body = json.loads(request.content)
    assert body["tools"] == [{"google_search": {}}]
    sent = body["contents"][0]["parts"][0]["text"]
    assert "NVDA earnings" in sent
    assert "Prefer sources published from 2026-01-01 through 2026-01-31." in sent
    assert "This is a preference, not a hard cutoff." in sent
    assert "Use at most 5 sources." in sent
    assert "news" not in sent

    assert content[0]["type"] == "answer_box"
    assert content[0]["title"] == "综合答案（来源见下）"
    assert content[0]["content"] == ANSWER
    assert content[1]["url"] == "https://example.com/nvda"
    assert content[1]["title"] == "Example NVDA"
    assert content[2]["url"] == SOURCE
    assert content[2]["redirect_url"] == REDIRECT
    assert "hidden thought" not in content[0]["content"]

    assert artifact["type"] == "web_search"
    assert artifact["query"] == "NVDA earnings"
    assert artifact["search_engine"] == "gemini_grounding"
    assert artifact["provider"] == "gemini"
    assert artifact["total_results"] == 2
    assert artifact["answer"] == ANSWER
    assert artifact["request_id"] == "resp-gemini-1"
    assert isinstance(artifact["response_time"], float)
    assert [card["url"] for card in artifact["results"]] == [
        "https://example.com/nvda",
        SOURCE,
    ]
    assert artifact["results"][0]["title"] == "Example NVDA"
    assert "snippet" in artifact["results"][0]
    assert len(cache.sets) == 1
    assert cache.sets[0][1] == 900


@pytest.mark.asyncio
async def test_count_is_clamped_into_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["contents"][0]["parts"][0]["text"])
        return httpx.Response(200, json=_sample(redirect=False))

    cache = _Cache()
    cache.install(monkeypatch)
    high = _tool(handler, max_results=50)
    await high.coroutine(query="NVDA")
    low = _tool(handler, max_results=0)
    await low.coroutine(query="NVDA")
    assert "Use at most 10 sources." in seen[0]
    assert "Use at most 1 sources." in seen[1]


@pytest.mark.asyncio
async def test_rate_limit_and_bad_request_and_other_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _Cache()
    cache.install(monkeypatch)

    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "slow down"}},
        )

    content, artifact = await _tool(limited).coroutine(query="NVDA")
    assert artifact["code"] == "PROVIDER_RATE_LIMITED"
    assert "限流" in content
    assert "slow down" not in content
    assert cache.sets == []

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"status": "INVALID_ARGUMENT", "message": "bad query"}},
        )

    content, artifact = await _tool(bad).coroutine(query="NVDA")
    assert artifact["code"] == "INVALID_REQUEST"
    assert "参数错误" in content
    assert "bad query" not in content
    assert cache.sets == []

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": {"message": "backend exploded"}})

    content, artifact = await _tool(down).coroutine(query="NVDA")
    assert "网页搜索暂不可用" in content
    assert "code" not in artifact
    assert "exploded" not in content
    assert cache.sets == []


@pytest.mark.asyncio
async def test_empty_generation_retries_once_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"candidates": [{"content": {"parts": []}}]})
        return httpx.Response(200, json=_sample(redirect=False))

    cache = _Cache()
    cache.install(monkeypatch)
    content, artifact = await _tool(handler).coroutine(query="NVDA")
    assert calls["n"] == 2
    assert artifact["answer"] == ANSWER
    assert content[0]["title"] == "综合答案（来源见下）"


@pytest.mark.asyncio
async def test_empty_generation_twice_returns_empty_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"candidates": []})

    cache = _Cache()
    cache.install(monkeypatch)
    content, artifact = await _tool(handler).coroutine(query="NVDA")
    assert calls["n"] == 2
    assert artifact["code"] == "empty_generation"
    assert "error" in artifact
    assert cache.sets == []


@pytest.mark.asyncio
async def test_cache_hit_skips_the_request_and_key_changes_with_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_sample(redirect=False))

    cache = _Cache()
    cache.install(monkeypatch)
    tool = _tool(handler)
    first, _ = await tool.coroutine(query="NVDA earnings")
    second, _ = await tool.coroutine(query="NVDA earnings")
    await tool.coroutine(query="AMD earnings")
    assert calls["n"] == 2
    assert second == first
    assert len(cache.sets) == 2
    assert cache.sets[0][0] != cache.sets[1][0]


@pytest.mark.asyncio
async def test_include_favicon_changes_the_cache_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_sample(redirect=False))

    cache = _Cache()
    cache.install(monkeypatch)
    client = GeminiGroundingClient(
        api_key=API_KEY,
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(handler),
    )
    shown = build_web_search_tool(max_results=3, include_favicon=True, client=client)
    hidden = build_web_search_tool(max_results=3, include_favicon=False, client=client)
    await shown.coroutine(query="NVDA earnings")
    await hidden.coroutine(query="NVDA earnings")
    assert calls["n"] == 2
    assert cache.sets[0][0] != cache.sets[1][0]


@pytest.mark.asyncio
async def test_missing_key_does_not_call_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_sample(redirect=False))

    cache = _Cache()
    cache.install(monkeypatch)
    client = GeminiGroundingClient(api_key="", transport=httpx.MockTransport(handler))
    tool = build_web_search_tool(max_results=3, client=client)
    content, artifact = await tool.coroutine(query="NVDA")
    assert calls["n"] == 0
    assert content == "网页搜索暂不可用"
    assert "code" not in artifact
    assert cache.sets == []


def test_dalton_yaml_selects_gemini_grounding(monkeypatch: pytest.MonkeyPatch) -> None:
    """The overlay stays on OpenClaw until a key exists. The selector still
    returns the Gemini tool when ``search_api`` is ``gemini_grounding``.
    """
    import yaml

    from src.tools.web.search import get_web_search_tool

    data = yaml.safe_load((REPO_ROOT / "agent_config.dalton.yaml").read_text(encoding="utf-8"))
    assert data["search_api"] == "openclaw"
    assert data["x_search"]["enabled"] is True
    assert data["web_search"]["model"] == "gemini-2.5-flash"

    monkeypatch.setattr("src.tools.web.search.get_search_api", lambda: "gemini_grounding")
    monkeypatch.setattr("src.config.tool_settings.get_search_api", lambda: "gemini_grounding")
    tool = get_web_search_tool(max_search_results=3)
    assert tool.name == "WebSearch"
    assert "该搜索不区分 topic" in (tool.description or "")


def test_gemini_provider_is_registered_only_when_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools.web.manifest import get_web_providers
    from src.tools.web.search import _PROVIDER_BUILDERS

    monkeypatch.setattr("src.config.tool_settings.get_search_api", lambda: "tavily")
    assert "gemini_grounding" not in get_web_providers()
    assert "gemini_grounding" not in _PROVIDER_BUILDERS
    assert "openclaw" not in get_web_providers()

    monkeypatch.setattr("src.config.tool_settings.get_search_api", lambda: "gemini_grounding")
    spec = get_web_providers()["gemini_grounding"]
    assert spec.env_key == "GEMINI_API_KEY"
    assert spec.display_name == "Gemini Grounding"
    assert "gemini_grounding" in _PROVIDER_BUILDERS
    assert "openclaw" not in get_web_providers()


@pytest.mark.asyncio
async def test_get_web_search_tool_returns_the_gemini_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", API_KEY)
    monkeypatch.setattr("src.tools.web.search.get_search_api", lambda: "gemini_grounding")
    monkeypatch.setattr("src.config.tool_settings.get_search_api", lambda: "gemini_grounding")
    monkeypatch.setattr(
        "src.dalton.web_search.gemini_grounding.configured_gemini_search_model",
        lambda: "gemini-2.5-flash",
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_sample(redirect=False))

    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("src.dalton.web_search.gemini_grounding.httpx.AsyncClient", _Client)
    _Cache().install(monkeypatch)

    from src.tools.web.search import get_web_search_tool

    tool = get_web_search_tool(max_search_results=3)
    assert tool.name == "WebSearch"
    assert "该搜索不区分 topic" in (tool.description or "")
    _content, artifact = await tool.coroutine(
        query="NVDA earnings",
        start_date="2026-01-01",
        end_date="2026-01-31",
    )
    assert artifact["search_engine"] == "gemini_grounding"
    assert artifact["provider"] == "gemini"
    assert seen[0].headers["x-goog-api-key"] == API_KEY
    assert API_KEY not in str(seen[0].url)
    assert "gemini-2.5-flash:generateContent" in str(seen[0].url)
