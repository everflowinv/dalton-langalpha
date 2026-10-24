"""Unit tests for Dalton native_overlay.json, Antigravity client/OAuth, and Muse client (P0N.2, G5.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.dalton.llm.overlay import NATIVE_OVERLAY_PATH, load_dalton_meta, load_overlay_data
from src.dalton.llm.providers import (
    ProviderDisabledError,
    build_dalton_client,
    dalton_oauth_token_getter,
    ensure_provider_enabled,
    is_dalton_oauth_provider,
)
from src.dalton.llm.providers.antigravity import AntigravityApiError, ChatAntigravity
from src.dalton.llm.providers.antigravity.client import (
    _THOUGHT_SIGNATURES_KEY,
    _messages_to_gemini,
)
from src.dalton.llm.providers.antigravity.oauth import parse_callback_input
from src.dalton.llm.providers.muse import build_muse_client
from src.llms.llm import LLM, ModelConfig
from src.llms.pricing_utils import find_model_pricing

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_native_overlay_does_not_collide_with_upstream():
    upstream_models = json.loads((REPO_ROOT / "src/llms/manifest/models.json").read_text(encoding="utf-8"))
    upstream_providers = json.loads((REPO_ROOT / "src/llms/manifest/providers.json").read_text(encoding="utf-8"))
    native = load_overlay_data(NATIVE_OVERLAY_PATH)
    assert native is not None

    native_models = native.get("models") or {}
    assert len(native_models) == 28
    overlap = set(native_models) & set(upstream_models)
    assert not overlap, f"Native overlay models collide with upstream: {overlap}"

    native_prov_cfg = (native.get("providers") or {}).get("provider_config") or {}
    overlap_prov = set(native_prov_cfg) & set(upstream_providers.get("provider_config") or {})
    assert not overlap_prov, f"Native overlay provider_config collides with upstream: {overlap_prov}"


def test_every_native_overlay_model_resolves_provider_and_pricing_or_notional():
    mc = ModelConfig()
    dalton_meta = load_dalton_meta()
    native = load_overlay_data(NATIVE_OVERLAY_PATH) or {}

    for key, entry in (native.get("models") or {}).items():
        provider = entry.get("provider")
        assert provider, f"{key} missing provider"
        pinfo = mc.get_provider_info(provider)
        assert pinfo, f"{key}: provider {provider} not found in flat_providers"
        assert "sdk" in pinfo, f"{key}: provider {provider} missing sdk"

        meta = dalton_meta.get(key) or {}
        assert meta.get("family"), f"{key} missing dalton_meta.family"
        billing = meta.get("billing")
        assert billing in ("subscription", "cash"), f"{key} invalid billing={billing!r}"
        if billing == "cash":
            pricing = find_model_pricing(entry.get("model_id", key), provider=provider)
            assert pricing is not None, f"Cash model {key} must have a price card"
        else:
            has_notional = bool(meta.get("notional_ref") or meta.get("notional_pricing"))
            assert has_notional, f"Subscription model {key} must have notional_ref or notional_pricing"


def test_contributor_models_hidden_and_forbidden_for_roles():
    mc = ModelConfig()
    dalton_meta = load_dalton_meta()
    for key in ("muse-spark-1.3-contributor-oauth", "muse-wonder-full-1.0-contributor-oauth"):
        entry = mc.get_model_config(key)
        assert entry is not None
        assert entry.get("visible") is False
        assert dalton_meta.get(key, {}).get("forbidden_for_roles") is True


def test_collision_is_logged_and_ignored(caplog):
    llm_cfg = {"claude-opus-5-5": {"model_id": "original", "provider": "anthropic"}}
    manifest = {"provider_config": {"anthropic": {"sdk": "anthropic"}}, "models": {}}
    from src.dalton.llm.overlay import _merge_single_overlay

    fake_overlay = {
        "models": {"claude-opus-5-5": {"model_id": "hacked", "provider": "anthropic"}},
        "providers": {"provider_config": {"anthropic": {"sdk": "openai"}}},
    }
    with caplog.at_level("ERROR"):
        _merge_single_overlay(fake_overlay, "test", llm_cfg, manifest)
    assert llm_cfg["claude-opus-5-5"]["model_id"] == "original"
    assert manifest["provider_config"]["anthropic"]["sdk"] == "anthropic"
    assert any("collides" in rec.message for rec in caplog.records)


def test_build_antigravity_and_muse_clients_with_session_headers():
    ag_llm = LLM("gemini-3.8-flash-low-oauth", api_key="fake-ag-token")
    ag_client = build_dalton_client(ag_llm, cache_key="thread-123")
    assert isinstance(ag_client, ChatAntigravity)
    assert ag_client.model == "gemini-3.8-flash-low"

    muse_llm = LLM("muse-spark-1.3-oauth", api_key="fake-muse-token")
    muse_client = build_dalton_client(muse_llm, cache_key="thread-123")
    assert muse_client is not None
    # Fixed headers stay on default_headers; session headers are attached per request in extra_headers
    default_hdrs = getattr(muse_client, "default_headers", {}) or {}
    assert "x-tbh-session-id" not in default_hdrs
    payload = muse_client._get_request_payload("ping")
    headers = payload.get("extra_headers") or {}
    assert "x-tbh-session-id" in headers
    assert "x-meta-ai-gateway-session-id" in headers
    assert headers["x-tbh-session-id"] == headers["x-meta-ai-gateway-session-id"]


def test_oauth_dispatch_helpers():
    assert is_dalton_oauth_provider("antigravity-oauth")
    assert is_dalton_oauth_provider("muse-oauth")
    assert is_dalton_oauth_provider("xai-oauth")
    assert not is_dalton_oauth_provider("codex-oauth")
    assert dalton_oauth_token_getter("antigravity-oauth") is not None
    assert dalton_oauth_token_getter("muse-oauth") is not None
    assert dalton_oauth_token_getter("xai-oauth") is not None
    assert dalton_oauth_token_getter("claude-oauth") is None


def test_provider_kill_switch(monkeypatch):
    monkeypatch.setenv("DALTON_DISABLED_PROVIDERS", "muse-oauth")
    with pytest.raises(ProviderDisabledError):
        ensure_provider_enabled("muse-oauth")
    with pytest.raises(ProviderDisabledError):
        LLM("muse-spark-1.3-oauth", api_key="fake-token").get_llm()


def test_antigravity_signatures_cleared_per_turn_and_reasoning_not_doubled():
    client = ChatAntigravity(model="gemini-3.8-flash-low", api_key="fake")
    frame = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"thought": True, "text": "thinking step", "thoughtSignature": "sig-turn-1"},
                            {"text": "final answer"},
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "thoughtsTokenCount": 7,
                "totalTokenCount": 22,
            },
        }
    }
    chunks = client._chunks_from_frame(frame)
    msg = client._final_message(chunks)
    # R6-4: reasoning_content must not be doubled when thought + text appear in the same frame
    assert msg.additional_kwargs["reasoning_content"] == "thinking step"
    assert msg.content == "final answer"
    assert msg.additional_kwargs[_THOUGHT_SIGNATURES_KEY] == ["sig-turn-1"]
    # R6-13: output_tokens includes thoughtsTokenCount (5 + 7 = 12)
    assert msg.usage_metadata["output_tokens"] == 12
    assert msg.usage_metadata["total_tokens"] == 22

    # R6-1: next turn on the same client instance must not retain turn 1's signatures
    monkeypatch_frames = [
        {
            "response": {
                "candidates": [
                    {
                        "content": {"parts": [{"text": "turn 2 answer"}]},
                        "finishReason": "STOP",
                    }
                ]
            }
        }
    ]
    client._stream_raw = lambda payload: iter(monkeypatch_frames)  # type: ignore[method-assign]
    res2 = client._generate([HumanMessage(content="hello")])
    msg2 = res2.generations[0].message
    assert _THOUGHT_SIGNATURES_KEY not in msg2.additional_kwargs


def test_antigravity_tool_message_resolves_name_from_ai_message():
    # R6-3: ToolMessage with name=None resolves function name via tool_call_id
    messages = [
        HumanMessage(content="check price"),
        AIMessage(
            content="",
            tool_calls=[{"name": "get_stock_price", "args": {"ticker": "META"}, "id": "call_abc123"}],
            additional_kwargs={_THOUGHT_SIGNATURES_KEY: ["sig-1"]},
        ),
        ToolMessage(content='{"price": 612.34}', tool_call_id="call_abc123"),
    ]
    _, contents = _messages_to_gemini(messages)
    assert len(contents) == 3
    fn_resp = contents[2]["parts"][0]["functionResponse"]
    assert fn_resp["name"] == "get_stock_price"
    assert fn_resp["response"] == {"price": 612.34}


def test_antigravity_api_error_carries_status_code():
    # R6-2: AntigravityApiError has status_code and SUBSCRIPTION_REQUIRED includes numeric status
    err = ChatAntigravity._error_from_status(
        403,
        json.dumps(
            {
                "error": {
                    "message": "no license",
                    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SUBSCRIPTION_REQUIRED"}],
                }
            }
        ),
    )
    assert isinstance(err, AntigravityApiError)
    assert err.status_code == 403
    assert "403" in str(err)


def test_antigravity_parse_callback_input():
    # R6-5 & R6-7: manual callback URL parser requires both code and state
    code, state, redirect_uri = parse_callback_input(
        "http://localhost:54321/callback?code=4/0AeanS0xyz&state=st_12345&scope=openid"
    )
    assert code == "4/0AeanS0xyz"
    assert state == "st_12345"
    assert redirect_uri == "http://localhost:54321/callback"

    with pytest.raises(ValueError, match="state"):
        parse_callback_input("http://localhost:54321/callback?code=4/0AeanS0xyz")


# ═══════════════════════════════════════════════════════════════════════════════
# Muse Code CLI 1.4.12 API Key Minting, UUIDv7 Headers, Brotli, Legacy & 401 Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_muse_headers_main_and_subagent_wire_transport_and_traceparent():
    import re
    import uuid
    import httpx
    from src.dalton.llm.providers.muse.client import (
        MUSE_CLI_USER_AGENT,
        build_muse_session_headers,
        derive_muse_uuid7,
    )
    from src.llms.llm import narrow_prompt_cache_key

    captured_requests: list[httpx.Request] = []

    sse_body = (
        'event: response.output_text.delta\n'
        'data: {"type":"response.output_text.delta","delta":"PONG"}\n\n'
        'event: response.completed\n'
        'data: {"type":"response.completed","response":{"id":"resp_1","object":"response","status":"completed",'
        '"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"PONG"}]}],'
        '"usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}\n\n'
    )

    async def _mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body.encode("utf-8"),
        )

    transport = httpx.MockTransport(_mock_handler)
    async with httpx.AsyncClient(transport=transport) as async_http_client:
        muse_llm = LLM(
            "muse-spark-1.3-oauth",
            api_key="minted-muse-key",
            http_async_client=async_http_client,
        )
        main_client = build_muse_client(muse_llm, cache_key="thread-main-001")
        assert main_client.max_retries == 5

        # Fixed headers are on default_headers; session headers are NOT on default_headers
        fixed_headers = dict(main_client.default_headers or {})
        assert fixed_headers["User-Agent"] == MUSE_CLI_USER_AGENT
        assert "1.4.10" not in fixed_headers["User-Agent"]
        assert fixed_headers["x-client-id"] == "tbh:exec"
        assert fixed_headers["x-tbh-agent-role"] == "main"
        assert fixed_headers["x-tbh-contributor-mode"] == "0"
        assert "x-tbh-session-id" not in {k.lower() for k in fixed_headers}

        contrib_llm = LLM("muse-spark-1.3-contributor-oauth", api_key="minted-muse-key")
        contrib_client = build_muse_client(contrib_llm, cache_key="thread-main-001")
        assert (contrib_client.default_headers or {})["x-tbh-contributor-mode"] == "1"

        # 1. Fire main call through httpx.MockTransport and inspect actual wire headers
        await main_client.ainvoke([HumanMessage(content="ping")])
        assert len(captured_requests) == 1
        main_req = captured_requests[0]
        assert main_req.headers["user-agent"] == MUSE_CLI_USER_AGENT
        assert main_req.headers["x-client-id"] == "tbh:exec"
        assert main_req.headers["x-tbh-agent-role"] == "main"
        assert main_req.headers["x-tbh-contributor-mode"] == "0"

        main_session_id = main_req.headers["x-tbh-session-id"]
        assert uuid.UUID(main_session_id).version == 7
        assert main_req.headers["x-tbh-thread-id"] == main_session_id
        assert main_req.headers["x-meta-ai-gateway-session-id"] == main_session_id
        assert main_req.headers["x-tbh-root-session-id"] == main_session_id
        assert "x-tbh-parent-thread-id" not in main_req.headers
        assert derive_muse_uuid7("thread-main-001") == main_session_id

        tp1 = main_req.headers["traceparent"]
        m1 = re.fullmatch(r"00-([0-9a-f]{32})-([0-9a-f]{16})-01", tp1)
        assert m1 is not None, f"Invalid traceparent: {tp1}"
        assert m1.group(1) == main_session_id.replace("-", "")

        # 2. Subagent call via narrow_prompt_cache_key (shares parent's HTTP client):
        #    must send subagent session headers on the wire!
        sub_client = narrow_prompt_cache_key(main_client, "research")
        assert "x-tbh-session-id" not in {k.lower() for k in (sub_client.default_headers or {})}
        await sub_client.ainvoke([HumanMessage(content="sub-ping")])
        assert len(captured_requests) == 2
        sub_req = captured_requests[1]

        sub_session_id = sub_req.headers["x-tbh-session-id"]
        assert uuid.UUID(sub_session_id).version == 7
        assert sub_session_id != main_session_id
        assert sub_req.headers["x-tbh-thread-id"] == sub_session_id
        assert sub_req.headers["x-meta-ai-gateway-session-id"] == sub_session_id
        assert sub_req.headers["x-tbh-root-session-id"] == main_session_id
        assert sub_req.headers["x-tbh-parent-thread-id"] == main_session_id

        sub_tp = sub_req.headers["traceparent"]
        sub_m = re.fullmatch(r"00-([0-9a-f]{32})-([0-9a-f]{16})-01", sub_tp)
        assert sub_m is not None
        assert sub_m.group(1) == sub_session_id.replace("-", "")
        assert sub_m.group(2) != m1.group(2)

        # 3. Probe / keyless client (no cache_key): generates a fresh UUIDv7 per request
        probe_llm = LLM(
            "muse-spark-1.3-oauth",
            api_key="minted-muse-key",
            http_async_client=async_http_client,
        )
        probe_client = build_muse_client(probe_llm, cache_key=None)
        await probe_client.ainvoke([HumanMessage(content="probe-1")])
        await probe_client.ainvoke([HumanMessage(content="probe-2")])
        probe_req_1 = captured_requests[2]
        probe_req_2 = captured_requests[3]
        pid1 = probe_req_1.headers["x-tbh-session-id"]
        pid2 = probe_req_2.headers["x-tbh-session-id"]
        assert uuid.UUID(pid1).version == 7
        assert uuid.UUID(pid2).version == 7
        assert pid1 != pid2
        assert probe_req_1.headers["x-tbh-thread-id"] == pid1
        assert probe_req_1.headers["x-meta-ai-gateway-session-id"] == pid1
        assert probe_req_1.headers["x-tbh-root-session-id"] == pid1
        assert "x-tbh-parent-thread-id" not in probe_req_1.headers

    # 4. Nested subagent call: root stays main_session_id, parent is 1st-level subagent's thread-id
    nested_headers = build_muse_session_headers(
        "thread-main-001:research:leaf",
        root_cache_key="thread-main-001",
        parent_cache_key="thread-main-001:research",
        is_subagent=True,
    )
    leaf_id = nested_headers["x-tbh-session-id"]
    assert uuid.UUID(leaf_id).version == 7
    assert leaf_id not in (main_session_id, sub_session_id)
    assert nested_headers["x-tbh-root-session-id"] == main_session_id
    assert nested_headers["x-tbh-parent-thread-id"] == sub_session_id


def test_muse_mint_response_gzip_decoding():
    # The mint endpoint answers gzip when br is not advertised (httpx omits br without brotli installed).
    import gzip
    import httpx

    payload = {
        "api_key": "muse_br_minted_key_abc",
        "base_url": "https://api.meta.ai/v1",
        "is_subs_active": True,
        "subs_tier_name": "Max",
        "user_email": "researcher@dalton.test",
        "subs_usage": {
            "window": {"used_percent": 18.5, "window_duration_mins": 300, "resets_at": "2026-10-23T08:00:00Z"},
            "weekly": {"used_percent": 42.0, "resets_at": "2026-10-27T00:00:00Z"},
            "tier": "Max",
        },
    }
    raw_json = json.dumps(payload).encode("utf-8")
    compressed = gzip.compress(raw_json)
    assert compressed != raw_json

    resp = httpx.Response(
        200,
        headers={"content-encoding": "gzip", "content-type": "application/json"},
        content=compressed,
    )
    assert resp.json() == payload


@pytest.mark.asyncio
async def test_muse_poll_mints_api_key_and_caches_metadata(monkeypatch: pytest.MonkeyPatch):
    from datetime import datetime, timezone
    from unittest.mock import create_autospec, patch
    import gzip
    import httpx
    import redis.asyncio as aioredis
    import src.dalton.app.oauth_native as oauth_native_mod
    import src.dalton.llm.providers.muse.oauth as muse_oauth_mod
    from src.utils.cache.redis_cache import RedisCacheClient

    mint_response_payload = {
        "api_key": "minted_muse_api_key_xyz",
        "base_url": "https://api.meta.ai/v1",
        "has_payment_method": True,
        "require_payment": False,
        "is_subs_active": True,
        "can_subscribe": False,
        "show_subs_upsell": False,
        "user_full_name": "Dalton Analyst",
        "user_email": "analyst@dalton.test",
        "user_avatar_url": "https://example.com/a.png",
        "payment_method": "card",
        "action_url": None,
        "subs_tier_id": "tier_pro",
        "subs_tier_name": "Pro",
        "is_subs_upgrade_available": True,
        "subs_usage": {
            "window": {"used_percent": 12.0, "window_duration_mins": 300, "resets_at": "2026-10-23T09:00:00Z"},
            "weekly": {"used_percent": 34.5, "resets_at": "2026-10-28T00:00:00Z"},
            "tier": "Pro",
        },
    }
    compressed_mint_body = gzip.compress(
        json.dumps(mint_response_payload).encode("utf-8")
    )

    recorded_requests: list[tuple[str, dict, Any]] = []

    async def _fake_post(self, url: str, *, data: Any = None, json: Any = None, headers: Any = None, **kwargs: Any) -> httpx.Response:
        recorded_requests.append((url, dict(headers or {}), data if data is not None else json))
        if url == muse_oauth_mod.MUSE_TOKEN_URL:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"access_token": "transient_oauth_access_token_do_not_persist", "token_type": "Bearer"},
            )
        if url == muse_oauth_mod.MUSE_MINT_KEY_URL:
            return httpx.Response(
                200,
                headers={"content-encoding": "gzip", "content-type": "application/json"},
                content=compressed_mint_body,
            )
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(
        httpx.AsyncClient,
        "post",
        create_autospec(httpx.AsyncClient.post, side_effect=_fake_post),
    )

    upserted_calls: list[dict] = []

    async def _capture_upsert(**kwargs):
        upserted_calls.append(kwargs)

    monkeypatch.setattr(
        muse_oauth_mod,
        "upsert_oauth_tokens",
        create_autospec(muse_oauth_mod.upsert_oauth_tokens, side_effect=_capture_upsert),
    )
    monkeypatch.setattr(
        muse_oauth_mod,
        "invalidate_oauth_active_cache",
        create_autospec(muse_oauth_mod.invalidate_oauth_active_cache, return_value=None),
    )
    monkeypatch.setattr(
        muse_oauth_mod,
        "_record_provider_event_safe",
        create_autospec(muse_oauth_mod._record_provider_event_safe, return_value=None),
    )

    redis_store: dict[str, tuple[str, int | None]] = {
        "oauth:muse:device:user-1": (json.dumps({"device_code": "dev_code_123"}), 600),
    }
    fake_cache = create_autospec(RedisCacheClient, instance=True)
    fake_cache.enabled = True
    fake_redis_client = create_autospec(aioredis.Redis, instance=True)

    async def _r_get(name: str):
        item = redis_store.get(name)
        return item[0] if item else None

    async def _r_set(name: str, value: Any, ex: int | None = None, **kwargs: Any):
        redis_store[name] = (str(value), ex)
        return True

    async def _r_del(*names: str):
        for n in names:
            redis_store.pop(n, None)
        return 1

    fake_redis_client.get.side_effect = _r_get
    fake_redis_client.set.side_effect = _r_set
    fake_redis_client.delete.side_effect = _r_del
    fake_cache.client = fake_redis_client

    with patch(
        "src.utils.cache.redis_cache.get_cache_client",
        autospec=True,
        return_value=fake_cache,
    ):
        poll_resp = await oauth_native_mod.muse_device_poll(user_id="user-1")
    assert poll_resp["success"] is True

    # Verify both step 3 (device token) and step 4 (mint_api_key) were called
    assert len(recorded_requests) == 2
    mint_url, mint_headers, mint_body = recorded_requests[1]
    assert mint_url == "https://api.meta.ai/muse-code/key"
    assert mint_headers["Authorization"] == "Bearer transient_oauth_access_token_do_not_persist"
    assert mint_headers["x-api-version"] == "1.0.0"
    assert mint_headers["user-agent"] == "muse-build/0.1.0"
    assert mint_body == {"onboard": False}

    # Verify upsert_oauth_tokens stored the minted api_key (NOT the transient access_token)
    assert len(upserted_calls) == 1
    saved = upserted_calls[0]
    assert saved["user_id"] == "user-1"
    assert saved["provider"] == "muse-oauth"
    assert saved["access_token"] == "minted_muse_api_key_xyz"
    assert "transient_oauth_access_token_do_not_persist" not in str(saved)
    assert saved["refresh_token"] == ""
    assert saved["account_id"] == muse_oauth_mod.MUSE_API_KEY_ACCOUNT_ID
    assert saved["plan_type"] == "Pro"
    assert saved["email"] == "analyst@dalton.test"
    days_out = (saved["expires_at"] - datetime.now(timezone.utc)).days
    assert days_out >= 360

    # Verify Redis cached base_url and subs_usage with 1-day TTL (and no invalidation flags)
    meta_raw, ttl = redis_store["oauth:muse:meta:user-1"]
    assert ttl == 86400
    meta = json.loads(meta_raw)
    assert meta["base_url"] == "https://api.meta.ai/v1"
    assert meta["is_subs_active"] is True
    assert meta["subs_tier_name"] == "Pro"
    assert meta["subs_usage"]["window"]["used_percent"] == 12.0
    assert meta["subs_usage"]["weekly"]["used_percent"] == 34.5
    assert "key_fp" not in meta


@pytest.mark.asyncio
async def test_muse_mint_failure_aborts_connection_and_never_echoes_upstream_body(monkeypatch: pytest.MonkeyPatch, caplog):
    from unittest.mock import create_autospec, patch
    import httpx
    import redis.asyncio as aioredis
    from fastapi import HTTPException
    import src.dalton.app.oauth_native as oauth_native_mod
    import src.dalton.llm.providers.muse.oauth as muse_oauth_mod
    from src.utils.cache.redis_cache import RedisCacheClient

    secret_oauth_token = "secret_oauth_token_never_leak_999"
    upstream_token_like = "EAA_upstream_token_like_secret_778899"

    async def _fake_post_fail(self, url: str, *, data: Any = None, json: Any = None, headers: Any = None, **kwargs: Any) -> httpx.Response:
        if url == muse_oauth_mod.MUSE_TOKEN_URL:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"access_token": secret_oauth_token, "token_type": "Bearer"},
            )
        return httpx.Response(
            403,
            headers={"content-type": "application/json"},
            json={
                "error": "invalid_grant",
                "error_description": f"Token {upstream_token_like} rejected by gateway",
                "message": f"Subscription required for {upstream_token_like}",
            },
        )

    monkeypatch.setattr(
        httpx.AsyncClient,
        "post",
        create_autospec(httpx.AsyncClient.post, side_effect=_fake_post_fail),
    )

    upsert_spy = create_autospec(muse_oauth_mod.upsert_oauth_tokens, return_value=None)
    monkeypatch.setattr(muse_oauth_mod, "upsert_oauth_tokens", upsert_spy)

    fake_cache = create_autospec(RedisCacheClient, instance=True)
    fake_cache.enabled = True
    fake_redis_client = create_autospec(aioredis.Redis, instance=True)

    async def _r_get(name: str):
        return json.dumps({"device_code": "dev_code_fail"})

    async def _r_del(*names: str):
        return 1

    fake_redis_client.get.side_effect = _r_get
    fake_redis_client.delete.side_effect = _r_del
    fake_cache.client = fake_redis_client

    with patch(
        "src.utils.cache.redis_cache.get_cache_client",
        autospec=True,
        return_value=fake_cache,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await oauth_native_mod.muse_device_poll(user_id="user-fail")

    assert exc_info.value.status_code == 400
    detail_str = str(exc_info.value.detail)
    assert "换发失败" in detail_str
    assert "HTTP 403: invalid_grant" in detail_str
    assert secret_oauth_token not in detail_str
    assert upstream_token_like not in detail_str
    assert secret_oauth_token not in caplog.text
    assert upstream_token_like not in caplog.text
    upsert_spy.assert_not_called()


@pytest.mark.asyncio
async def test_mark_muse_key_invalid_if_current_atomic_sql():
    from contextlib import asynccontextmanager
    from unittest.mock import create_autospec, patch
    from psycopg import AsyncConnection, AsyncCursor
    import src.dalton.database.muse_tokens as muse_tokens_mod

    executed: list[tuple[str, Any]] = []
    cur = create_autospec(AsyncCursor, instance=True)

    async def _execute(
        query: Any,
        params: Any = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> Any:
        executed.append((str(query), params))
        return cur

    cur.execute.side_effect = _execute
    cur.rowcount = 1

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cur

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor_cm

    @asynccontextmanager
    async def _get_db_conn():
        yield conn

    with (
        patch.object(
            muse_tokens_mod,
            "get_db_connection",
            autospec=True,
            side_effect=_get_db_conn,
        ),
        patch.object(
            muse_tokens_mod,
            "get_encryption_key",
            autospec=True,
            return_value="enc-secret-key",
        ),
    ):
        ok = await muse_tokens_mod.mark_muse_key_invalid_if_current("cred-user-1", "failed-key-xyz")
        assert ok is True
        assert len(executed) == 1
        sql, params = executed[0]
        assert "UPDATE user_oauth_tokens" in sql
        assert "SET account_id = 'muse-api-key:invalid', updated_at = NOW()" in sql
        assert "WHERE user_id = %s AND provider = 'muse-oauth'" in sql
        assert "AND pgp_sym_decrypt(access_token, %s) = %s" in sql
        assert "expires_at" not in sql
        assert params == ("cred-user-1", "enc-secret-key", "failed-key-xyz")

        cur.rowcount = 0
        ok_stale = await muse_tokens_mod.mark_muse_key_invalid_if_current("cred-user-1", "stale-key")
        assert ok_stale is False


@pytest.mark.asyncio
async def test_muse_reconnected_new_key_not_invalidated_by_stale_401_and_empty_key_noop():
    from unittest.mock import patch
    import src.dalton.database.muse_tokens as muse_tokens_mod
    import src.dalton.llm.providers.muse.oauth as muse_oauth_mod

    old_key = "muse_old_api_key_before_reconnect"
    new_key = "muse_new_api_key_after_reconnect"

    # Pin operator -> credential holder so the test holds under team sharing too.
    with (
        patch(
            "src.dalton.credentials.credential_user_id",
            autospec=True,
            side_effect=lambda uid: f"holder:{uid}",
        ),
        patch.object(
            muse_tokens_mod,
            "mark_muse_key_invalid_if_current",
            autospec=True,
            side_effect=lambda cred_uid, key: key == new_key,
        ) as atomic_update_mock,
        patch.object(
            muse_oauth_mod,
            "invalidate_oauth_active_cache",
            autospec=True,
            return_value=None,
        ) as invalidate_cache_mock,
        patch.object(
            muse_oauth_mod,
            "_record_provider_event_safe",
            autospec=True,
            return_value=None,
        ) as record_event_mock,
    ):
        # 1. Empty api_key returns immediately without calling the DB
        res_empty = await muse_oauth_mod.mark_muse_token_invalid("user-race", "")
        assert res_empty is False
        atomic_update_mock.assert_not_called()
        invalidate_cache_mock.assert_not_called()
        record_event_mock.assert_not_called()

        # 2. Interleaved reconnect + stale 401: atomic update returns False for old_key -> no event, no cache invalidation
        res_stale = await muse_oauth_mod.mark_muse_token_invalid("user-race", old_key, reason="invalid_api_key")
        assert res_stale is False
        atomic_update_mock.assert_called_once_with("holder:user-race", old_key)
        invalidate_cache_mock.assert_not_called()
        record_event_mock.assert_not_called()

        # 3. Current key 401: atomic update returns True -> invalidates active cache and records auth_expired event
        res_current = await muse_oauth_mod.mark_muse_token_invalid("user-race", new_key, reason="invalid_api_key")
        assert res_current is True
        assert atomic_update_mock.call_count == 2
        invalidate_cache_mock.assert_called_once_with("holder:user-race")
        record_event_mock.assert_called_once()


@pytest.mark.asyncio
async def test_muse_legacy_unminted_token_rejected_and_status_shows_usage_and_reconnect(monkeypatch: pytest.MonkeyPatch):
    from datetime import datetime, timedelta, timezone
    from unittest.mock import create_autospec
    import src.dalton.llm.providers.muse.oauth as muse_oauth_mod
    import src.dalton.services.provider_status as ps_mod
    import src.server.database.oauth_tokens as oauth_db

    real_get_oauth_status = oauth_db.get_oauth_status
    real_get_muse_cached_meta = muse_oauth_mod.get_muse_cached_meta

    # 1. Legacy token (saved at 05:38 without mint_api_key marker -> account_id == "")
    legacy_row = {
        "access_token": "legacy_raw_oauth_access_token",
        "refresh_token": "",
        "account_id": "",
        "email": None,
        "plan_type": "muse",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    monkeypatch.setattr(
        muse_oauth_mod,
        "get_oauth_tokens",
        create_autospec(muse_oauth_mod.get_oauth_tokens, return_value=legacy_row),
    )
    monkeypatch.setattr(
        muse_oauth_mod,
        "get_muse_cached_meta",
        create_autospec(real_get_muse_cached_meta, return_value=None),
    )
    monkeypatch.setattr(
        muse_oauth_mod,
        "_record_provider_event_safe",
        create_autospec(muse_oauth_mod._record_provider_event_safe, return_value=None),
    )

    valid_tok = await muse_oauth_mod.get_valid_token("user-legacy")
    assert valid_tok is None

    # 2. Provider status page with legacy token -> configured=False and warning prompts "请重新连接 Muse"
    monkeypatch.setattr(
        oauth_db,
        "get_oauth_status",
        create_autospec(
            real_get_oauth_status,
            return_value={"connected": True, "account_id": "", "email": None, "plan_type": "muse"},
        ),
    )
    monkeypatch.setattr(
        ps_mod,
        "_fetch_usage_stats_24h",
        create_autospec(ps_mod._fetch_usage_stats_24h, return_value={}),
    )
    monkeypatch.setattr(
        ps_mod,
        "list_recent_provider_events",
        create_autospec(ps_mod.list_recent_provider_events, return_value=[]),
    )
    monkeypatch.setattr(
        ps_mod,
        "count_provider_errors_24h",
        create_autospec(ps_mod.count_provider_errors_24h, return_value=0),
    )
    monkeypatch.setattr(
        ps_mod,
        "get_last_refresh_event",
        create_autospec(ps_mod.get_last_refresh_event, return_value=None),
    )

    status_legacy = await ps_mod.get_providers_status(user_id="user-legacy")
    muse_legacy_row = next(p for p in status_legacy["providers"] if p["provider"] == "muse-oauth")
    assert muse_legacy_row["configured"] is False
    assert muse_legacy_row["credential"].get("reconnect_required") is True
    assert any("请重新连接 Muse" in w["message"] for w in muse_legacy_row["warnings"])

    # 3. Provider status page with valid minted key & cached mint response -> shows subscription & usage
    cached_meta = {
        "base_url": "https://api.meta.ai/v1",
        "is_subs_active": True,
        "subs_tier_name": "Pro",
        "subs_usage": {
            "window": {"used_percent": 25.0, "window_duration_mins": 300, "resets_at": "2026-10-23T12:00:00Z"},
            "weekly": {"used_percent": 60.5, "resets_at": "2026-10-29T00:00:00Z"},
            "tier": "Pro",
        },
    }
    monkeypatch.setattr(
        oauth_db,
        "get_oauth_status",
        create_autospec(
            real_get_oauth_status,
            return_value={
                "connected": True,
                "account_id": muse_oauth_mod.MUSE_API_KEY_ACCOUNT_ID,
                "email": "analyst@dalton.test",
                "plan_type": "Pro",
            },
        ),
    )
    monkeypatch.setattr(
        muse_oauth_mod,
        "get_muse_cached_meta",
        create_autospec(real_get_muse_cached_meta, return_value=cached_meta),
    )

    status_active = await ps_mod.get_providers_status(user_id="user-active")
    muse_active_row = next(p for p in status_active["providers"] if p["provider"] == "muse-oauth")
    assert muse_active_row["configured"] is True
    assert muse_active_row["is_subs_active"] is True
    assert muse_active_row["subs_tier_name"] == "Pro"
    assert muse_active_row["subs_usage"]["window"]["used_percent"] == 25.0
    assert muse_active_row["subs_usage"]["window"]["resets_at"] == "2026-10-23T12:00:00Z"
    assert muse_active_row["subs_usage"]["weekly"]["used_percent"] == 60.5
    assert muse_active_row["subs_usage"]["weekly"]["resets_at"] == "2026-10-29T00:00:00Z"


@pytest.mark.asyncio
async def test_muse_401_invalid_api_key_classified_as_auth_expired_and_outer_complete_does_not_remark(monkeypatch: pytest.MonkeyPatch):
    from unittest.mock import create_autospec, patch
    from ptc_agent.config.agent import CredentialSource
    from src.server.services.llm.user_models import ModelSource
    import src.dalton.llm.complete as complete_mod
    from src.dalton.llm.providers.muse.client import (
        ChatMuseOpenAI,
        MuseAuthExpiredError,
        is_muse_auth_expired_error,
    )
    import src.dalton.llm.providers.muse.oauth as muse_oauth_mod
    import src.dalton.services.provider_status as ps_mod
    import src.server.services.llm.clients as llm_clients_mod

    class _Structured401Error(RuntimeError):
        def __init__(self) -> None:
            super().__init__("Authentication failed")
            self.status_code = 401
            self.body = {"error": {"code": "invalid_api_key", "message": "Invalid API key"}}

    class _Non401SubstringError(RuntimeError):
        def __init__(self) -> None:
            super().__init__("upstream gateway 401 unauthorized text on api.meta.ai muse")
            self.status_code = 403
            self.body = {"error": {"code": "permission_denied"}}
            self.error_kind = "auth_expired"

    # Strict check: 403 or error_kind=="auth_expired" without 401/invalid_api_key must NOT match
    assert is_muse_auth_expired_error(_Structured401Error()) is True
    assert is_muse_auth_expired_error(_Non401SubstringError()) is False

    # ChatMuseOpenAI._handle_muse_401 is the ONLY caller of mark_muse_token_invalid,
    # and attaches 16-char failed_key_fp (never raw key) to MuseAuthExpiredError
    secret_key = "muse_client_secret_key_for_401_test"
    real_client = ChatMuseOpenAI(
        model="muse-spark-1.3",
        api_key=secret_key,
        muse_user_id="user-1",
    )
    with patch(
        "src.dalton.llm.providers.muse.client.mark_muse_token_invalid",
        autospec=True,
        return_value=True,
    ) as client_mark_mock:
        with pytest.raises(MuseAuthExpiredError) as exc_info:
            await real_client._handle_muse_401(_Structured401Error())
        client_mark_mock.assert_called_once_with("user-1", secret_key, reason="invalid_api_key")
        assert len(exc_info.value.failed_key_fp) == 16
        assert exc_info.value.failed_key_fp == muse_oauth_mod._muse_key_fp(secret_key)
        assert secret_key not in str(exc_info.value)
        assert secret_key not in exc_info.value.failed_key_fp

    # Outer dalton_complete and probe_provider receiving MuseAuthExpiredError must NOT call mark_muse_token_invalid again
    failing_client = create_autospec(ChatMuseOpenAI, instance=True)
    failing_client.ainvoke.side_effect = MuseAuthExpiredError(
        failed_key_fp=muse_oauth_mod._muse_key_fp(secret_key),
    )

    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(complete_mod.inspect_journal_state, return_value=("proceed", None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(complete_mod.claim_journal_invocation, return_value=("proceed", {"attempt": 1})),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_failed",
        create_autospec(complete_mod.complete_journal_failed, return_value=None),
    )
    monkeypatch.setattr(
        complete_mod,
        "record_provider_event",
        create_autospec(complete_mod.record_provider_event, return_value=None),
    )
    monkeypatch.setattr(
        ps_mod,
        "record_provider_event",
        create_autospec(ps_mod.record_provider_event, return_value=None),
    )
    monkeypatch.setattr(
        llm_clients_mod,
        "resolve_model_client",
        create_autospec(
            llm_clients_mod.resolve_model_client,
            return_value=llm_clients_mod.ResolvedClient(
                failing_client,
                ModelSource.SYSTEM,
                CredentialSource.OAUTH,
            ),
        ),
    )

    with patch(
        "src.dalton.llm.providers.muse.oauth.mark_muse_token_invalid",
        autospec=True,
    ) as outer_mark_mock:
        muse_probe = await ps_mod.probe_provider("muse-oauth", user_id="user-1", model="muse-spark-1.3-oauth")
        assert muse_probe["ok"] is False
        assert muse_probe["error_kind"] == "auth_expired"
        assert "请重新连接 Muse" in muse_probe["message"]
        assert outer_mark_mock.call_count == 0

    # Non-Muse provider (xai) 401 -> remains http_401, unaffected
    failing_client.ainvoke.side_effect = _Structured401Error()
    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(complete_mod._check_cash_budget_admission, return_value=(True, None, None)),
    )
    xai_probe = await ps_mod.probe_provider("xai", user_id="user-1", model="grok-4.7")
    assert xai_probe["ok"] is False
    assert xai_probe["error_kind"] == "http_401"


@pytest.mark.asyncio
async def test_glm_coding_models_overlay_provider_and_httpx_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anthropic
    import httpx
    from langchain_anthropic import ChatAnthropic
    from src.llms.llm import create_llm

    monkeypatch.setattr(LLM, "_model_config", None)
    mc = ModelConfig()
    dalton_meta = load_dalton_meta()

    for key, expected_id, expected_modalities in (
        ("glm-5.3-plan", "glm-5.3", ["text"]),
        ("glm-5.3-flash-plan", "glm-5.3-flash", ["text", "image", "pdf"]),
    ):
        cfg = mc.get_model_config(key)
        assert cfg is not None
        assert cfg["provider"] == "z-ai-cn-coding"
        assert cfg["model_id"] == expected_id
        assert cfg["context"] == 1000000
        assert cfg["input_modalities"] == expected_modalities
        assert cfg["parameters"]["max_tokens"] == 128000
        assert cfg["parameters"]["thinking"] == {"type": "enabled"}
        assert "extra_body" not in cfg["parameters"]
        assert cfg["reasoning"] == {
            "efforts": ["low", "high", "max"],
            "default": "low",
            "write": "parameters.output_config.effort",
            "on": {"parameters.thinking.type": "enabled"},
        }

        meta = dalton_meta.get(key) or {}
        assert meta == {
            "family": "zai-glm",
            "billing": "subscription",
            "notional_ref": f"{expected_id}-cn",
            "tool_calling": True,
        }

    pinfo = mc.get_provider_info("z-ai-cn-coding")
    assert pinfo["sdk"] == "anthropic"
    assert pinfo["base_url"] == "https://open.bigmodel.cn/api/anthropic"
    assert pinfo["env_key"] == "ZAI_CN_CODING_API_KEY"
    assert pinfo["access_type"] == "coding_plan"

    # Platform key resolution from .env (ZAI_CN_CODING_API_KEY) without BYOK, intercepted by httpx.MockTransport
    dummy_token = "unit-test-zai-coding-token"
    monkeypatch.setenv("ZAI_CN_CODING_API_KEY", dummy_token)
    client = create_llm("glm-5.3-plan")
    assert isinstance(client, ChatAnthropic)
    assert client.model == "glm-5.3"
    assert client.anthropic_api_url == "https://open.bigmodel.cn/api/anthropic"

    captured_requests: list[httpx.Request] = []

    def _make_sse(text: str, in_tok: int = 12, out_tok: int = 6) -> bytes:
        events = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_glm_test_01",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "glm-5.3",
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": in_tok,
                            "output_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "checking ping"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": text},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        return "".join(
            f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            for ev, data in events
        ).encode("utf-8")

    async def _handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_make_sse("PONG", in_tok=12, out_tok=6),
        )

    mock_http = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="https://open.bigmodel.cn/api/anthropic",
    )
    client.__dict__["_async_client"] = anthropic.AsyncAnthropic(
        api_key=dummy_token,
        base_url="https://open.bigmodel.cn/api/anthropic",
        http_client=mock_http,
    )
    try:
        resp = await client.ainvoke([HumanMessage(content="ping")])
    finally:
        await mock_http.aclose()

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert str(req.url) == "https://open.bigmodel.cn/api/anthropic/v1/messages"
    assert "open.bigmodel.cn/api/anthropic" in str(req.url)
    assert req.headers.get("x-api-key") == dummy_token
    body = json.loads(req.content.decode("utf-8"))
    assert body["model"] == "glm-5.3"
    assert body["thinking"] == {"type": "enabled"}
    assert body["output_config"] == {"effort": "low"}
    assert resp.usage_metadata["input_tokens"] == 12
    assert resp.usage_metadata["output_tokens"] == 6


@pytest.mark.asyncio
async def test_glm_coding_plan_only_accepts_own_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify z-ai-cn-coding only accepts its own BYOK key or ZAI_CN_CODING_API_KEY env var,
    never falling back to parent z-ai-cn or sibling variants."""
    from unittest.mock import create_autospec
    from langchain_anthropic import ChatAnthropic
    from ptc_agent.config.agent import CredentialSource
    import src.server.database.api_keys as api_keys_mod
    import src.server.services.llm.user_models as user_models_mod
    from src.llms.llm import LLM, MissingProviderKeyError, create_llm
    from src.server.services.llm.clients import (
        _candidate_slugs,
        resolve_byok_llm_client,
        resolve_model_client,
    )

    monkeypatch.setattr(LLM, "_model_config", None)
    mc = LLM.get_model_config()

    # Candidate walk for coding_plan variant is strictly isolated to its own slug
    assert _candidate_slugs("z-ai-cn-coding", mc) == ["z-ai-cn-coding"]
    # Non-coding-plan parent still walks its family
    assert "z-ai" in _candidate_slugs("z-ai-cn", mc)

    monkeypatch.setattr(
        user_models_mod,
        "get_custom_model_config",
        create_autospec(user_models_mod.get_custom_model_config, return_value=None),
    )

    parent_token = "parent-zai-cn-cash-token"
    own_byok_token = "own-zai-cn-coding-byok-token"
    env_coding_token = "env-zai-cn-coding-token"
    orig_get_byok = api_keys_mod.get_byok_configs_for_providers

    # Case 1: Parent z-ai-cn has BYOK key, z-ai-cn-coding has none, env is unset -> no client / unconfigured
    monkeypatch.delenv("ZAI_CN_CODING_API_KEY", raising=False)

    async def _byok_only_parent(user_id: str, providers: list[str]) -> dict:
        store = {"z-ai-cn": {"api_key": parent_token, "base_url": None}}
        return {p: store[p] for p in providers if p in store}

    monkeypatch.setattr(
        api_keys_mod,
        "get_byok_configs_for_providers",
        create_autospec(
            orig_get_byok,
            side_effect=_byok_only_parent,
        ),
    )

    byok_client = await resolve_byok_llm_client("u-glm", "glm-5.3-plan", True)
    assert byok_client is None

    rc_unconfigured = await resolve_model_client(
        "u-glm",
        "glm-5.3-plan",
        is_byok=True,
        allow_platform_fallback=True,
    )
    assert rc_unconfigured.client is None
    assert rc_unconfigured.credential_source == CredentialSource.NONE

    with pytest.raises(MissingProviderKeyError):
        create_llm("glm-5.3-plan")

    # Case 2: z-ai-cn-coding has its own BYOK key -> uses its own BYOK key
    async def _byok_own_and_parent(user_id: str, providers: list[str]) -> dict:
        store = {
            "z-ai-cn": {"api_key": parent_token, "base_url": None},
            "z-ai-cn-coding": {"api_key": own_byok_token, "base_url": None},
        }
        return {p: store[p] for p in providers if p in store}

    monkeypatch.setattr(
        api_keys_mod,
        "get_byok_configs_for_providers",
        create_autospec(
            orig_get_byok,
            side_effect=_byok_own_and_parent,
        ),
    )

    rc_own_byok = await resolve_model_client(
        "u-glm",
        "glm-5.3-plan",
        is_byok=True,
        allow_platform_fallback=True,
    )
    assert isinstance(rc_own_byok.client, ChatAnthropic)
    assert rc_own_byok.credential_source == CredentialSource.BYOK
    assert rc_own_byok.client.anthropic_api_key.get_secret_value() == own_byok_token
    assert rc_own_byok.client.anthropic_api_url == "https://open.bigmodel.cn/api/anthropic"

    # Case 3: Only env ZAI_CN_CODING_API_KEY is set (even if parent z-ai-cn has a BYOK key) -> uses env key
    monkeypatch.setattr(
        api_keys_mod,
        "get_byok_configs_for_providers",
        create_autospec(
            orig_get_byok,
            side_effect=_byok_only_parent,
        ),
    )
    monkeypatch.setenv("ZAI_CN_CODING_API_KEY", env_coding_token)

    rc_env = await resolve_model_client(
        "u-glm",
        "glm-5.3-plan",
        is_byok=True,
        allow_platform_fallback=True,
    )
    assert isinstance(rc_env.client, ChatAnthropic)
    assert rc_env.credential_source == CredentialSource.PLATFORM
    assert rc_env.client.anthropic_api_key.get_secret_value() == env_coding_token
    assert rc_env.client.anthropic_api_url == "https://open.bigmodel.cn/api/anthropic"
