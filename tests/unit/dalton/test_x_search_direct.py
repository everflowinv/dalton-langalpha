"""Direct xAI x_search: mapping, request body, token errors, cache, and mount."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest

from src.dalton.llm.overlay import NATIVE_OVERLAY_PATH, load_overlay_data
from src.dalton.llm.providers.xai_grok.client import ChatGrokOpenAI
from src.dalton.web_search.cache import search_cache_key
from src.dalton.web_search.x_search import map_x_search_response
from src.dalton.web_search.x_search_direct import (
    DEFAULT_TIMEOUT_SECONDS,
    DirectXSearchClient,
    X_SEARCH_MODEL,
    XSearchUnavailable,
    build_direct_x_search_tool,
    to_broker_payload,
)
from src.server.services.oauth_health import OAuthReauthRequired, valid_token
from src.utils.cache.redis_cache import RedisCacheClient

TOKEN = "xai-test-token"
STATUS = "https://x.com/NickRokke/status/2112925226740683159"
ANSWER = "NVDA discussion on X stays focused on data-center demand."


def _overlay_base() -> str:
    data = load_overlay_data(NATIVE_OVERLAY_PATH) or {}
    return str(data["providers"]["provider_config"]["xai-oauth"]["base_url"]).rstrip("/")


def _sample() -> dict[str, Any]:
    return {
        "id": "resp_x_1",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": ANSWER,
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": STATUS,
                                "title": "Nick on NVDA",
                            }
                        ],
                    }
                ],
            }
        ],
    }


class _Cache:
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


def test_responses_sample_maps_through_map_x_search_response() -> None:
    results, artifact = map_x_search_response(
        to_broker_payload(_sample()),
        query="NVDA",
    )
    assert len(results) == 1
    assert results[0]["type"] == "x_post"
    assert results[0]["author"] == "@NickRokke"
    assert results[0]["url"] == STATUS
    assert results[0]["title"] == "Nick on NVDA"
    assert isinstance(results[0]["date"], str)
    assert artifact["provider"] == "xai"
    assert artifact["answer"] == ANSWER
    assert "[1]" not in artifact["answer"]
    assert artifact["request_id"] == "resp_x_1"
    assert "no_evidence" not in artifact


def test_default_timeout_is_forty_five_seconds() -> None:
    assert DirectXSearchClient()._timeout == DEFAULT_TIMEOUT_SECONDS
    assert DEFAULT_TIMEOUT_SECONDS == 45.0


@pytest.mark.asyncio
async def test_request_uses_oauth_headers_x_search_tool_and_forty_five_second_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    recorded: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_sample())

    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            recorded["timeout"] = kwargs.get("timeout")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("src.dalton.web_search.x_search_direct.httpx.AsyncClient", _Client)
    cache = _Cache()
    cache.install(monkeypatch)
    client = DirectXSearchClient(token=TOKEN, transport=transport)
    tool = build_direct_x_search_tool(client=client)
    content, artifact = await tool.coroutine(
        query="NVDA",
        from_date="2026-01-01",
        to_date="2026-01-31",
        handles=["@nvidia"],
    )

    assert recorded["timeout"] == 45.0
    request = seen[0]
    base = _overlay_base()
    assert str(request.url) == f"{base}/responses"
    assert TOKEN not in str(request.url)
    assert TOKEN not in request.content.decode()
    body = json.loads(request.content)
    assert body["model"] == "grok-4.3"
    assert body["model"] == X_SEARCH_MODEL
    assert body["store"] is False
    assert body["stream"] is True
    assert body["max_turns"] == 2
    assert body["input"] == "NVDA"
    assert "inline_citations" not in body
    assert "inlineCitations" not in body
    assert body["tools"] == [
        {
            "type": "x_search",
            "allowed_x_handles": ["nvidia"],
            "from_date": "2026-01-01",
            "to_date": "2026-01-31",
        }
    ]

    reference = ChatGrokOpenAI(
        model="grok-4.3",
        api_key=TOKEN,
        base_url=base,
        session_id="fixed-session",
    )
    ref = {str(key).lower(): str(value) for key, value in dict(reference.default_headers).items()}
    got = {str(key).lower(): str(value) for key, value in request.headers.items()}
    assert got["authorization"] == ref["authorization"] == f"Bearer {TOKEN}"
    assert got["x-xai-token-auth"] == ref["x-xai-token-auth"]
    assert got["x-grok-client-version"] == ref["x-grok-client-version"]
    assert got["x-grok-model-override"] == ref["x-grok-model-override"] == "grok-4.3"
    assert got["x-client-request-id"]

    assert content[0]["type"] == "answer_box"
    assert content[0]["title"] == "X 搜索综合摘要（来源见下）"
    assert ANSWER in content[0]["content"]
    assert "[1]" not in content[0]["content"]
    assert content[1]["type"] == "x_post"
    assert content[1]["author"] == "@NickRokke"
    assert content[1]["url"] == STATUS
    assert artifact["search_engine"] == "xai"
    assert artifact["provider"] == "xai"
    assert cache.sets[0][1] == 900


@pytest.mark.asyncio
async def test_sse_response_completed_is_the_mapped_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = json.dumps({"type": "response.completed", "response": _sample()})
    sse = (
        'event: response.output_text.delta\n'
        'data: {"type":"response.output_text.delta","delta":"PARTIAL"}\n\n'
        f"event: response.completed\ndata: {completed}\n\n"
        "data: [DONE]\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    cache = _Cache()
    cache.install(monkeypatch)
    client = DirectXSearchClient(token=TOKEN, transport=httpx.MockTransport(handler))
    tool = build_direct_x_search_tool(client=client)
    content, artifact = await tool.coroutine(query="NVDA")
    assert artifact["answer"] == ANSWER
    assert "PARTIAL" not in artifact["answer"]
    assert content[1]["url"] == STATUS


@pytest.mark.asyncio
async def test_more_than_twenty_handles_is_a_parameter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_sample())

    cache = _Cache()
    cache.install(monkeypatch)
    client = DirectXSearchClient(token=TOKEN, transport=httpx.MockTransport(handler))
    tool = build_direct_x_search_tool(client=client)
    content, artifact = await tool.coroutine(
        query="NVDA",
        handles=[f"user{i}" for i in range(21)],
    )
    assert calls["n"] == 0
    assert "20" in content
    assert "参数错误" in content
    assert "code" not in artifact
    assert cache.sets == []


@pytest.mark.asyncio
async def test_missing_token_is_unavailable_and_does_not_call_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_sample())

    token_lookup = create_autospec(valid_token, return_value=None)
    monkeypatch.setattr("src.server.services.oauth_health.valid_token", token_lookup)
    monkeypatch.setattr(
        "src.dalton.settings.get_configured_pm_user_ids",
        lambda: ["pm-admin"],
    )
    cache = _Cache()
    cache.install(monkeypatch)
    client = DirectXSearchClient(transport=httpx.MockTransport(handler))
    tool = build_direct_x_search_tool(client=client)
    content, artifact = await tool.coroutine(query="NVDA")
    assert calls["n"] == 0
    assert content == "X 搜索暂不可用"
    assert "code" not in artifact
    assert "sign-in" not in content
    assert cache.sets == []
    token_lookup.assert_awaited()


@pytest.mark.asyncio
async def test_reauth_and_missing_admin_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _reauth(user_id: str, provider: str) -> None:
        raise OAuthReauthRequired(provider, "needs_reauth")

    monkeypatch.setattr("src.server.services.oauth_health.valid_token", _reauth)
    monkeypatch.setattr(
        "src.dalton.settings.get_configured_pm_user_ids",
        lambda: ["pm-admin"],
    )
    cache = _Cache()
    cache.install(monkeypatch)
    tool = build_direct_x_search_tool(client=DirectXSearchClient())
    content, artifact = await tool.coroutine(query="NVDA")
    assert content == "X 搜索暂不可用"
    assert "needs_reauth" not in content
    assert "code" not in artifact

    lookup = create_autospec(valid_token)
    monkeypatch.setattr("src.server.services.oauth_health.valid_token", lookup)
    monkeypatch.setattr("src.dalton.settings.get_configured_pm_user_ids", lambda: [])
    content, artifact = await tool.coroutine(query="NVDA again")
    assert content == "X 搜索暂不可用"
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limit_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow"}})

    cache = _Cache()
    cache.install(monkeypatch)
    client = DirectXSearchClient(token=TOKEN, transport=httpx.MockTransport(handler))
    content, artifact = await build_direct_x_search_tool(client=client).coroutine(query="NVDA")
    assert artifact["code"] == "PROVIDER_RATE_LIMITED"
    assert "限流" in content
    assert "slow" not in content
    assert cache.sets == []


@pytest.mark.asyncio
async def test_cache_hit_does_not_request_or_load_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_sample())

    class _Held:
        access_token = TOKEN

    lookup = create_autospec(valid_token, return_value=_Held())
    monkeypatch.setattr("src.server.services.oauth_health.valid_token", lookup)
    monkeypatch.setattr(
        "src.dalton.settings.get_configured_pm_user_ids",
        lambda: ["pm-admin"],
    )
    cache = _Cache()
    cache.install(monkeypatch)
    client = DirectXSearchClient(transport=httpx.MockTransport(handler))
    tool = build_direct_x_search_tool(client=client)
    first, _ = await tool.coroutine(query="NVDA", handles=["nvidia", "amd"])
    second, _ = await tool.coroutine(query="NVDA", handles=["nvidia", "amd"])
    await tool.coroutine(query="NVDA", handles=["amd", "nvidia"])
    assert calls["n"] == 2
    assert second == first
    assert cache.sets[0][0] != cache.sets[1][0]
    assert lookup.await_count == 2


@pytest.mark.asyncio
async def test_bad_request_is_a_parameter_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "unknown field max_turns"}})

    cache = _Cache()
    cache.install(monkeypatch)
    client = DirectXSearchClient(token=TOKEN, transport=httpx.MockTransport(handler))
    content, artifact = await build_direct_x_search_tool(client=client).coroutine(query="NVDA")
    assert artifact["code"] == "INVALID_REQUEST"
    assert "参数错误" in content
    assert "max_turns" not in content
    assert cache.sets == []


def test_create_agent_mounts_x_search_from_the_switch() -> None:
    from src.ptc_agent.agent.agent import PTCAgent

    source = inspect.getsource(PTCAgent.create_agent)
    start = source.index("x_search_tools")
    block = source[start : source.index("crawl_tools", start)]
    assert "x_search_enabled" in block
    assert "build_direct_x_search_tool" in block
    assert "openclaw" not in block
    assert "x_search import build_x_search_tool" not in block


def test_cache_key_keeps_list_boundaries_and_favicon() -> None:
    one_item = search_cache_key("xai", {"handles": ["a,b"]})
    two_items = search_cache_key("xai", {"handles": ["a", "b"]})
    assert one_item != two_items
    assert search_cache_key("xai", {"handles": ["a", "b"]}) != search_cache_key(
        "xai", {"handles": ["b", "a"]}
    )
    assert search_cache_key("xai", {"b": 1, "a": 2}) == search_cache_key(
        "xai", {"a": 2, "b": 1}
    )
    assert search_cache_key("gemini", {"include_favicon": True}) != search_cache_key(
        "gemini", {"include_favicon": False}
    )


@pytest.mark.asyncio
async def test_bad_handle_characters_are_a_parameter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_sample())

    cache = _Cache()
    cache.install(monkeypatch)
    client = DirectXSearchClient(token=TOKEN, transport=httpx.MockTransport(handler))
    tool = build_direct_x_search_tool(client=client)
    for handles in (["nv,idia"], ["user.name"], ["bad-name"], ["has space"], ["名字"]):
        content, artifact = await tool.coroutine(query="NVDA", handles=handles)
        assert "参数错误" in content
        assert "code" not in artifact
    assert calls["n"] == 0
    assert cache.sets == []


@pytest.mark.asyncio
async def test_include_favicon_changes_the_x_search_cache_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_sample())

    cache = _Cache()
    cache.install(monkeypatch)
    client = DirectXSearchClient(token=TOKEN, transport=httpx.MockTransport(handler))
    shown = build_direct_x_search_tool(include_favicon=True, client=client)
    hidden = build_direct_x_search_tool(include_favicon=False, client=client)
    await shown.coroutine(query="NVDA")
    await hidden.coroutine(query="NVDA")
    assert calls["n"] == 2
    assert cache.sets[0][0] != cache.sets[1][0]


@pytest.mark.asyncio
async def test_inline_citation_markers_are_stripped_from_the_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marked = _sample()
    marked["output"][0]["content"][0]["text"] = (
        "Keep [site](https://example.com/story) and [1](https://example.com/one) "
        "and [[x]](https://example.com/keep). "
        f"Drop [[1]]({STATUS}) and [[12]](https://x.com/other/status/1)."
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=marked)

    cache = _Cache()
    cache.install(monkeypatch)
    client = DirectXSearchClient(token=TOKEN, transport=httpx.MockTransport(handler))
    content, artifact = await build_direct_x_search_tool(client=client).coroutine(query="NVDA")
    answer = content[0]["content"]
    assert "[site](https://example.com/story)" in answer
    assert "[1](https://example.com/one)" in answer
    assert "[[x]](https://example.com/keep)" in answer
    assert "[[1]]" not in answer
    assert "[[12]]" not in answer
    assert "[[1]]" not in artifact["answer"]
    assert content[1]["url"] == STATUS
    assert cache.sets


_LIST_POST = "https://x.com/a/status/1"


def _list_item_response(*, with_citation: bool) -> dict[str, Any]:
    text = f"- 需求增长。[[1]]({_LIST_POST})"
    part: dict[str, Any] = {"type": "output_text", "text": text}
    if with_citation:
        part["annotations"] = [
            {"type": "url_citation", "url": _LIST_POST, "title": "需求"}
        ]
    return {
        "output": [
            {"type": "message", "role": "assistant", "content": [part]},
        ]
    }


@pytest.mark.asyncio
async def test_list_item_keeps_excerpt_and_hides_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_list_item_response(with_citation=True))

    _Cache().install(monkeypatch)
    client = DirectXSearchClient(token=TOKEN, transport=httpx.MockTransport(handler))
    content, artifact = await build_direct_x_search_tool(client=client).coroutine(query="NVDA")
    post = next(item for item in content if item.get("type") == "x_post")
    assert post["url"] == _LIST_POST
    assert post["summary_excerpt"]
    assert "需求增长" in post["summary_excerpt"]
    assert "[[1]](" not in content[0]["content"]
    assert "[[1]](" not in artifact["answer"]
    assert "no_evidence" not in artifact


@pytest.mark.asyncio
async def test_url_only_in_the_marker_is_still_a_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_list_item_response(with_citation=False))

    _Cache().install(monkeypatch)
    client = DirectXSearchClient(token=TOKEN, transport=httpx.MockTransport(handler))
    content, artifact = await build_direct_x_search_tool(client=client).coroutine(query="NVDA")
    post = next(item for item in content if item.get("type") == "x_post")
    assert post["url"] == _LIST_POST
    assert "no_evidence" not in artifact
    assert "[[1]](" not in content[0]["content"]
    assert "[[1]](" not in artifact["answer"]


def _terminal(status: str, text: str, *, error: object = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": status,
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }
    if error is not None:
        body["error"] = error
    return body


@pytest.mark.asyncio
async def test_failed_and_incomplete_responses_are_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = [
        httpx.Response(
            200,
            json=_terminal(
                "failed",
                "PARTIAL_FAILED",
                error={"message": "upstream-secret"},
            ),
        ),
        httpx.Response(
            200,
            text=(
                "data: "
                + json.dumps(
                    {
                        "type": "response.completed",
                        "response": _terminal("incomplete", "PARTIAL_INCOMPLETE"),
                    }
                )
                + "\n\ndata: [DONE]\n"
            ),
        ),
        httpx.Response(
            200,
            text=(
                "data: "
                + json.dumps(
                    {
                        "type": "response.incomplete",
                        "response": _terminal("incomplete", "PARTIAL_EVENT"),
                    }
                )
                + "\n\ndata: [DONE]\n"
            ),
        ),
        httpx.Response(
            200,
            json=_terminal(
                "completed",
                "PARTIAL_ERROR",
                error={"message": "upstream-secret"},
            ),
        ),
    ]
    pending = list(cases)

    def handler(request: httpx.Request) -> httpx.Response:
        return pending.pop(0)

    cache = _Cache()
    cache.install(monkeypatch)
    client = DirectXSearchClient(token=TOKEN, transport=httpx.MockTransport(handler))
    tool = build_direct_x_search_tool(client=client)
    with pytest.raises(XSearchUnavailable):
        to_broker_payload(_terminal("incomplete", "PARTIAL_INCOMPLETE"))
    for _case in cases:
        content, artifact = await tool.coroutine(query="NVDA")
        assert content == "X 搜索暂不可用"
        assert "code" not in artifact
        assert "PARTIAL_" not in content
        assert "upstream-secret" not in content
        assert artifact.get("error") == "X 搜索暂不可用"
    assert cache.sets == []
    assert pending == []


@pytest.mark.asyncio
async def test_dripping_sse_hits_the_wall_clock_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(_sample()).encode()

    class _Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            step = max(1, len(payload) // 8)
            for start in range(0, len(payload), step):
                await asyncio.sleep(0.02)
                yield payload[start : start + step]

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Drip())

    cache = _Cache()
    cache.install(monkeypatch)
    client = DirectXSearchClient(
        token=TOKEN,
        timeout_seconds=0.05,
        transport=httpx.MockTransport(handler),
    )
    content, artifact = await build_direct_x_search_tool(client=client).coroutine(query="NVDA")
    assert content == "X 搜索暂不可用"
    assert "code" not in artifact
    assert cache.sets == []
