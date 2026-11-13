"""Unit tests for the read-only Discord history route."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"

if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402

_REAL_CREATE_SUBPROCESS_EXEC = asyncio.create_subprocess_exec
TOKEN = "BotSecretToken_9f3aDISCORD"
MARKER = "DISCORD_BODY_MARKER"
CHANNEL = "1482004383544639531"
AUTHOR_SECRET = "DISCORD_AUTHOR_SECRET"
PROXY_URL_SECRET = "proxy-userinfo-secret"
CDN_URL = "https://cdn.discordapp.com/attachments/1/note.pdf?sig=1"


def _make_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> rb.ResearchDataBridge:
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    for name in (
        "https_proxy",
        "HTTPS_PROXY",
        "http_proxy",
        "HTTP_PROXY",
        "ALL_PROXY",
        "DALTON_BRIDGE_SUBPROCESS_PROXY",
        "DALTON_DISCORD_BOT_TOKEN",
        "DALTON_DISCORD_HISTORY_CHANNELS",
    ):
        monkeypatch.delenv(name, raising=False)
    dummy_py = tmp_path / "dummy_python"
    dummy_py.write_text("#!/bin/sh\n", encoding="utf-8")
    dummy_xreach = tmp_path / "xreach"
    dummy_xreach.write_text("#!/bin/sh\n", encoding="utf-8")
    nodebin = tmp_path / "nodebin"
    nodebin.mkdir(parents=True, exist_ok=True)
    tmeet_bin = nodebin / "tmeet"
    tmeet_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    gws_bin = nodebin / "gws"
    gws_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_OCW_PYTHON", str(dummy_py))
    monkeypatch.setenv("DALTON_XREACH_BIN", str(dummy_xreach))
    monkeypatch.setenv("DALTON_TMEET_BIN", str(tmeet_bin))
    monkeypatch.setenv("DALTON_GWS_BIN", str(gws_bin))
    return rb.ResearchDataBridge(rb.BridgeConfig())


def _allow(monkeypatch: pytest.MonkeyPatch, channels: str = CHANNEL) -> None:
    monkeypatch.setenv("DALTON_DISCORD_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("DALTON_DISCORD_HISTORY_CHANNELS", channels)


def _response(
    status: int,
    payload: Any,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers or {},
        json=payload,
        request=httpx.Request("GET", "https://discord.com/api/v10/channels/0/messages"),
    )


def _install(
    bridge: rb.ResearchDataBridge,
    items: list[Any],
) -> tuple[Any, list[float]]:
    client = create_autospec(httpx.AsyncClient, instance=True)
    client.get.side_effect = list(items)
    bridge._discord_http = client
    delays: list[float] = []

    async def _sleep(delay: float) -> None:
        delays.append(float(delay))

    bridge._discord_sleep = _sleep
    return client, delays


async def _call(bridge: rb.ResearchDataBridge, args: dict[str, Any]) -> dict[str, Any]:
    resp = await bridge.handle_tools_call(
        1,
        {"name": "research", "arguments": {"route": "discord-history", **args}},
    )
    return json.loads(resp["result"]["content"][0]["text"])


def _url(client: Any, index: int = 0) -> str:
    called = client.get.await_args_list[index]
    if called.args:
        return str(called.args[0])
    return str(called.kwargs["url"])


def _headers(client: Any, index: int = 0) -> dict[str, str]:
    called = client.get.await_args_list[index]
    return dict(called.kwargs["headers"])


def test_route_stays_out_of_shared_guides() -> None:
    assert "discord-history" not in rb.RESEARCH_EVIDENCE_AND_ROUTE_GUIDE
    assert "discord-history" not in rb.INTERNAL_LIBRARY_ROUTES
    assert "discord-history" in rb.TOOL_VALID_ROUTES["research"]
    spec = rb.ROUTE_CONTRACT_SPECS["discord-history"]
    assert spec["operations"] == ["list_messages"]
    assert spec["accepted_params"] == ["channel_id", "after", "limit"]
    assert spec["required_params"] == {"list_messages": ["channel_id"]}


def test_health_probe_is_a_short_list_messages_call() -> None:
    from src.dalton.data_map.health import build_route_probe_spec
    from tests.unit.dalton.test_bridge_callers_contract import assert_valid_bridge_call

    tool_name, args = build_route_probe_spec("discord-history", "research")
    assert tool_name == "research"
    assert args["route"] == "discord-history"
    assert args["operation"] == "list_messages"
    assert args["channel_id"] == "0"
    assert args["limit"] == 1
    assert_valid_bridge_call(tool_name, args)


@pytest.mark.asyncio
async def test_health_loop_leaves_discord_history_unprobed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.data_map import health as health_mod

    fake_resolve = create_autospec(
        health_mod.resolve_researchdata_endpoint_and_token,
        return_value=("http://127.0.0.1:9/mcp", "token", set()),
    )
    monkeypatch.setattr(health_mod, "resolve_researchdata_endpoint_and_token", fake_resolve)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _call_bridge(tool: str, arguments: dict[str, Any], timeout_s: float = 0) -> dict[str, Any]:
        calls.append((tool, dict(arguments)))
        return {
            "routes": [
                {"id": "discord-history", "category": "research", "kind": "bridge_readonly"}
            ]
        }

    async def _probe(**kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    async def _probe_conv() -> dict[str, Any]:
        return {"ok": True}

    rows, _catalog, _handoff = await health_mod._execute_probes(
        clamped_timeout=5,
        clamped_concurrency=2,
        call_bridge=_call_bridge,
        probe_web=_probe,
        probe_x=_probe,
        probe_market=_probe,
        probe_sec_filings=None,
        probe_conv=_probe_conv,
    )
    row = next(item for item in rows if item.get("source_id") == "discord-history")
    assert row["status"] == "unprobed"
    assert row["detail"]["reason"] == "Discord 历史不探测"
    assert calls == [("catalog", {})]


@pytest.mark.asyncio
async def test_whitelist_and_arguments_reject_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    _allow(monkeypatch)
    client, _delays = _install(bridge, [])

    async def _rejected(args: dict[str, Any], hint: str) -> None:
        body = await _call(bridge, args)
        dumped = json.dumps(body, ensure_ascii=False)
        assert body["ok"] is False
        assert body["status"] == "invalid_arguments"
        assert body["error_code"] == "invalid_arguments"
        assert body["error_category"] == "参数错误"
        assert body["hint"] == hint
        assert body["_sources"] == []
        assert MARKER not in dumped
        assert TOKEN not in dumped

    await _rejected(
        {"operation": "list_messages", "channel_id": "999"},
        "频道不在 DALTON_DISCORD_HISTORY_CHANNELS 白名单。",
    )
    monkeypatch.setenv("DALTON_DISCORD_HISTORY_CHANNELS", "")
    await _rejected(
        {"operation": "list_messages", "channel_id": CHANNEL},
        "频道不在 DALTON_DISCORD_HISTORY_CHANNELS 白名单。",
    )
    _allow(monkeypatch)
    await _rejected(
        {"operation": "list_messages", "channel_id": "12a"},
        "channel_id 需要是 1 到 32 位数字。",
    )
    await _rejected(
        {"operation": "list_messages", "channel_id": 123},
        "channel_id 需要是 1 到 32 位数字。",
    )
    await _rejected(
        {"operation": "list_messages"},
        "channel_id 需要是 1 到 32 位数字。",
    )
    await _rejected(
        {"operation": "list_messages", "channel_id": CHANNEL, "after": "12a"},
        "after 需要是数字雪花 ID 字符串。",
    )
    await _rejected(
        {"operation": "list_messages", "channel_id": CHANNEL, "after": 100},
        "after 需要是数字雪花 ID 字符串。",
    )
    await _rejected(
        {"operation": "list_messages", "channel_id": CHANNEL, "after": True},
        "after 需要是数字雪花 ID 字符串。",
    )
    for limit in (0, 101, True, 1.5, "10"):
        await _rejected(
            {"operation": "list_messages", "channel_id": CHANNEL, "limit": limit},
            "limit 需要是 1 到 100 的整数。",
        )
    # An unknown operation is stopped by the shared route-contract check first.
    unknown = await _call(bridge, {"operation": "search", "channel_id": CHANNEL})
    assert unknown["ok"] is False
    assert unknown["status"] == "invalid_arguments"
    assert unknown["error_code"] == "invalid_operation"
    assert client.get.await_count == 0


@pytest.mark.asyncio
async def test_missing_token_is_source_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    monkeypatch.setenv("DALTON_DISCORD_HISTORY_CHANNELS", CHANNEL)
    client, _delays = _install(bridge, [])
    body = await _call(
        bridge, {"operation": "list_messages", "channel_id": CHANNEL, "limit": 1}
    )
    dumped = json.dumps(body, ensure_ascii=False)
    assert body["ok"] is False
    assert body["status"] == "source_missing"
    assert body["error_code"] == "source_missing"
    assert body["error_category"] == "上游不可用"
    assert "DALTON_DISCORD_BOT_TOKEN" in body["hint"]
    assert body["_sources"] == []
    assert client.get.await_count == 0
    assert TOKEN not in dumped


@pytest.mark.asyncio
async def test_http_errors_do_not_echo_the_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    _allow(monkeypatch)
    caplog.set_level(logging.WARNING, logger="researchdata_bridge")
    secret_body = {"message": MARKER, "token": TOKEN}

    client, _delays = _install(bridge, [_response(401, secret_body)])
    denied = await _call(bridge, {"operation": "list_messages", "channel_id": CHANNEL, "limit": 1})
    assert denied["status"] == "not_authorized"
    assert denied["error_code"] == "not_authorized"
    assert denied["error_category"] == "权限或额度"
    assert denied["hint"] == "Discord 拒绝了这次读取。"
    assert denied["_sources"] == []

    client, _delays = _install(bridge, [_response(403, secret_body)])
    forbidden = await _call(bridge, {"operation": "list_messages", "channel_id": CHANNEL, "limit": 1})
    assert forbidden["status"] == "not_authorized"
    assert forbidden["hint"] == "Discord 拒绝了这次读取。"

    client, _delays = _install(bridge, [_response(503, secret_body)])
    upstream = await _call(bridge, {"operation": "list_messages", "channel_id": CHANNEL, "limit": 1})
    assert upstream["status"] == "upstream_error"
    assert upstream["error_code"] == "upstream_error"
    assert upstream["error_category"] == "上游不可用"
    assert upstream["hint"] == "Discord 历史读取失败，HTTP 状态 503。"
    assert upstream["_sources"] == []

    client, _delays = _install(bridge, [_response(200, secret_body)])
    not_a_list = await _call(bridge, {"operation": "list_messages", "channel_id": CHANNEL, "limit": 1})
    assert not_a_list["status"] == "upstream_error"
    assert "HTTP 状态" not in not_a_list["hint"]

    dumped = json.dumps(
        [denied, forbidden, upstream, not_a_list],
        ensure_ascii=False,
    )
    assert MARKER not in dumped
    assert TOKEN not in dumped
    assert MARKER not in caplog.text
    assert TOKEN not in caplog.text
    assert client.get.await_count == 1


@pytest.mark.asyncio
async def test_rate_limit_retries_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    _allow(monkeypatch)
    caplog.set_level(logging.WARNING, logger="researchdata_bridge")
    secret_body = {"message": MARKER, "retry_after": 30, "token": TOKEN}

    client, delays = _install(
        bridge,
        [
            _response(429, secret_body, headers={"Retry-After": "2.5"}),
            _response(200, [{"id": "20", "content": "kept", "timestamp": "2026-11-13T00:00:00+00:00"}]),
        ],
    )
    body = await _call(bridge, {"operation": "list_messages", "channel_id": CHANNEL, "limit": 1})
    assert body["ok"] is True
    assert body["data"]["messages"][0]["id"] == "20"
    assert delays == [2.5]
    assert client.get.await_count == 2
    assert _url(client, 0) == _url(client, 1)

    client, delays = _install(
        bridge,
        [
            _response(429, {"retry_after": 9, "message": MARKER}, headers={"Retry-After": "120"}),
            _response(429, {"message": MARKER, "token": TOKEN}),
        ],
    )
    limited = await _call(bridge, {"operation": "list_messages", "channel_id": CHANNEL, "limit": 1})
    assert limited["status"] == "rate_limited"
    assert limited["error_code"] == "rate_limited"
    assert limited["error_category"] == "权限或额度"
    assert limited["hint"] == "退避一次后仍然被限流。"
    assert delays == [60.0]
    assert client.get.await_count == 2

    client, delays = _install(
        bridge,
        [
            _response(429, {"retry_after": 4, "message": MARKER}),
            _response(200, []),
        ],
    )
    await _call(bridge, {"operation": "list_messages", "channel_id": CHANNEL, "limit": 1})
    assert delays == [4.0]

    client, delays = _install(
        bridge,
        [
            _response(429, {"message": MARKER}),
            _response(200, []),
        ],
    )
    await _call(bridge, {"operation": "list_messages", "channel_id": CHANNEL, "limit": 1})
    assert delays == [1.0]
    dumped = json.dumps(limited, ensure_ascii=False)
    assert MARKER not in dumped
    assert TOKEN not in dumped
    assert TOKEN not in caplog.text
    assert MARKER not in caplog.text


@pytest.mark.asyncio
async def test_timeout_and_transport_errors_hide_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    _allow(monkeypatch)
    caplog.set_level(logging.WARNING, logger="researchdata_bridge")

    _install(bridge, [httpx.TimeoutException(f"slow {TOKEN} {MARKER}")])
    timed = await _call(bridge, {"operation": "list_messages", "channel_id": CHANNEL, "limit": 1})
    assert timed["status"] == "timeout"
    assert timed["error_code"] == "timeout"
    assert timed["error_category"] == "上游不可用"
    assert timed["hint"] == "Discord 历史读取超时。"
    assert timed["_sources"] == []

    _install(bridge, [httpx.ConnectError(f"connect {TOKEN} {MARKER}")])
    failed = await _call(bridge, {"operation": "list_messages", "channel_id": CHANNEL, "limit": 1})
    assert failed["status"] == "upstream_error"
    assert failed["hint"] == "Discord 历史读取失败。"
    dumped = json.dumps([timed, failed], ensure_ascii=False)
    assert TOKEN not in dumped
    assert MARKER not in dumped
    assert TOKEN not in caplog.text
    assert MARKER not in caplog.text
    assert "TimeoutException" in caplog.text
    assert "HTTPError" in caplog.text


@pytest.mark.asyncio
async def test_messages_are_normalized_sorted_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    _allow(monkeypatch)
    caplog.set_level(logging.WARNING, logger="researchdata_bridge")
    raw = [
        {
            "id": 300,
            "timestamp": "2026-11-13T15:04:05.000000+00:00",
            "content": f"alpha {TOKEN} omega",
            "author": {"id": "42", "username": AUTHOR_SECRET, "bot": False},
            "attachments": [
                {
                    "id": "7",
                    "filename": "note.pdf",
                    "content_type": "application/pdf",
                    "size": 12,
                    "url": CDN_URL,
                    "proxy_url": "https://media.discordapp.net/DISCORD_PROXY_URL_DROP",
                    "height": 10,
                }
            ],
            "embeds": [
                {"description": "DISCORD_EMBED_SECRET"},
                {
                    "url": "https://example.test/a",
                    "title": "Title",
                    "description": "DISCORD_EMBED_SECRET",
                },
            ],
        },
        {"id": "100", "content": "older note", "timestamp": "2026-11-12T01:02:03+00:00"},
        "not-a-message",
    ]
    client, _delays = _install(bridge, [_response(200, raw)])
    body = await _call(
        bridge,
        {"operation": "list_messages", "channel_id": CHANNEL, "after": "50", "limit": 3},
    )
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert "empty_reason" not in body
    assert body["data"]["has_more"] is True
    assert body["data"]["message_count"] == 2
    messages = body["data"]["messages"]
    assert [item["id"] for item in messages] == ["100", "300"]
    assert all(isinstance(item["id"], str) for item in messages)
    older = messages[0]
    assert older["author_is_bot"] is False
    assert older["author_id"] == ""
    newer = messages[1]
    assert newer["content"] == "alpha [redacted] omega"
    assert newer["author_id"] == "42"
    assert newer["author_is_bot"] is False
    assert set(newer["attachments"][0]) == {"id", "filename", "content_type", "size", "url"}
    assert newer["attachments"][0]["url"] == CDN_URL
    assert newer["embeds"] == [{"url": "https://example.test/a", "title": "Title"}]
    sources = body["_sources"]
    assert len(sources) == 2
    by_id = {item["document_id"]: item for item in sources}
    older_source = by_id[f"discord-history:{CHANNEL}:100"]
    newer_source = by_id[f"discord-history:{CHANNEL}:300"]
    assert older_source["as_of"] == "2026-11-12"
    assert newer_source["title"] == "alpha [redacted] omega"
    assert newer_source["as_of"] == "2026-11-13"
    for source in sources:
        assert source["identity"] == "internal_note"
        assert source["private"] is True
        assert source["publisher"] == "Discord"
        assert source["source_type"] == "community_post"
        assert source["url"] is None
        dumped_source = json.dumps(source, ensure_ascii=False)
        assert "cdn.discordapp.com" not in dumped_source
        assert CDN_URL not in dumped_source
    dumped = json.dumps(body, ensure_ascii=False)
    assert TOKEN not in dumped
    assert AUTHOR_SECRET not in dumped
    assert "DISCORD_PROXY_URL_DROP" not in dumped
    assert "DISCORD_EMBED_SECRET" not in dumped
    assert TOKEN not in caplog.text
    headers = _headers(client)
    assert headers["Authorization"] == f"Bot {TOKEN}"
    assert headers["User-Agent"] == "DaltonResearchDataBridge/1.0"
    assert "after=50" in _url(client)
    assert "limit=3" in _url(client)


@pytest.mark.asyncio
async def test_default_limit_empty_after_and_whitespace_whitelist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    _allow(monkeypatch, " 111 , 222 ")
    client, _delays = _install(bridge, [_response(200, [])])
    body = await _call(bridge, {"operation": "list_messages", "channel_id": "111", "after": "   "})
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert body["data"]["messages"] == []
    assert body["data"]["has_more"] is False
    assert body["data"]["message_count"] == 0
    assert "empty_reason" not in body
    first_url = _url(client)
    assert first_url.startswith("https://discord.com/api/v10/channels/111/messages?")
    assert "limit=100" in first_url
    assert "after=" not in first_url

    client, _delays = _install(
        bridge,
        [_response(200, [{"id": "8", "content": "one", "timestamp": "2026-11-13T00:00:00Z"}])],
    )
    short = await _call(bridge, {"operation": "list_messages", "channel_id": "222", "limit": 100})
    assert short["data"]["has_more"] is False
    assert short["data"]["message_count"] == 1
    assert "after=" not in _url(client)


@pytest.mark.asyncio
async def test_catalog_injects_discord_history_without_calling_discord(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    monkeypatch.setenv("DALTON_DISCORD_BOT_TOKEN", TOKEN)
    client, _delays = _install(bridge, [])
    commands: list[tuple[list[str], dict[str, Any]]] = []

    def _mcp_response(req_id: Any) -> dict[str, Any]:
        payload = {
            "ok": True,
            "status": "ok",
            "routes": [{"id": "alphaengine", "category": "research", "operations": ["search_library"]}],
        }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False},
        }

    exec_mock = create_autospec(_REAL_CREATE_SUBPROCESS_EXEC)

    async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        commands.append(([str(part) for part in cmd], kwargs))
        proc = create_autospec(asyncio.subprocess.Process, instance=True)
        proc.returncode = 0
        proc.pid = None

        async def _communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
            lines = (input or b"").decode("utf-8").splitlines()
            reqs = [json.loads(line) for line in lines if line.strip()]
            proc.returncode = 0
            return (json.dumps(_mcp_response(reqs[0]["id"])) + "\n").encode("utf-8"), b""

        proc.communicate.side_effect = _communicate
        return proc

    exec_mock.side_effect = _fake_exec
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)
    listed = await bridge.handle_tools_call(2, {"name": "catalog", "arguments": {}})
    listed_body = json.loads(listed["result"]["content"][0]["text"])
    assert any(item.get("id") == "discord-history" for item in listed_body["routes"])
    single = await bridge.handle_tools_call(
        3, {"name": "catalog", "arguments": {"route": "discord-history"}}
    )
    single_body = json.loads(single["result"]["content"][0]["text"])
    assert single_body["route_count"] == 1
    entry = single_body["routes"][0]
    spec = rb.ROUTE_CONTRACT_SPECS["discord-history"]
    assert entry["id"] == "discord-history"
    assert entry["required_params"] == spec["required_params"]
    assert entry["accepted_params"] == spec["accepted_params"]
    flat = [part for cmd, _kwargs in commands for part in cmd]
    assert TOKEN not in flat
    for _cmd, kwargs in commands:
        env = kwargs.get("env") or {}
        assert "DALTON_DISCORD_BOT_TOKEN" not in env
        assert TOKEN not in json.dumps(env, ensure_ascii=False)
    assert client.get.await_count == 0


@pytest.mark.asyncio
async def test_proxy_prefers_https_proxy_and_is_not_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="researchdata_bridge")
    bridge = _make_bridge(tmp_path, monkeypatch)
    proxy = f"http://user:{PROXY_URL_SECRET}@127.0.0.1:7897"
    monkeypatch.setenv("https_proxy", proxy)
    monkeypatch.setenv("HTTP_PROXY", "http://plain.example:1")
    assert rb._discord_proxy(bridge.cfg) == proxy
    _allow(monkeypatch)
    _install(bridge, [_response(200, [])])
    await _call(bridge, {"operation": "list_messages", "channel_id": CHANNEL, "limit": 1})
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(name, raising=False)
    bridge.cfg.subprocess_proxy = "http://cfg-proxy.example:7897"
    assert rb._discord_proxy(bridge.cfg) == "http://cfg-proxy.example:7897"
    assert PROXY_URL_SECRET not in caplog.text
    assert "cfg-proxy.example" not in caplog.text


def test_community_subprocess_env_strips_bot_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    monkeypatch.setenv("DALTON_DISCORD_BOT_TOKEN", TOKEN)
    env = bridge._build_community_subprocess_env()
    assert "DALTON_DISCORD_BOT_TOKEN" not in env
    assert TOKEN not in json.dumps(env, ensure_ascii=False)
