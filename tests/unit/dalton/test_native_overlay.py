"""Unit tests for Dalton native_overlay.json, Antigravity client/OAuth, and Muse client (P0N.2, G5.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.dalton.llm.overlay import NATIVE_OVERLAY_PATH, apply_model_overlay, load_dalton_meta, load_overlay_data
from src.dalton.llm.providers import (
    ProviderDisabledError,
    build_dalton_client,
    dalton_oauth_token_getter,
    ensure_provider_enabled,
    is_dalton_oauth_provider,
)
from src.dalton.llm.providers.antigravity import AntigravityApiError, ChatAntigravity, build_antigravity_client
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
    assert len(native_models) == 23
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
    # R6-10: raw cache_key must populate Muse session headers
    headers = getattr(muse_client, "default_headers", {}) or {}
    assert "x-tbh-session-id" in headers
    assert "x-meta-ai-gateway-session-id" in headers
    assert headers["x-tbh-session-id"] == headers["x-meta-ai-gateway-session-id"]


def test_oauth_dispatch_helpers():
    assert is_dalton_oauth_provider("antigravity-oauth")
    assert is_dalton_oauth_provider("muse-oauth")
    assert not is_dalton_oauth_provider("codex-oauth")
    assert dalton_oauth_token_getter("antigravity-oauth") is not None
    assert dalton_oauth_token_getter("muse-oauth") is not None
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
