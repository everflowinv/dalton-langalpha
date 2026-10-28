"""Unit tests for native xAI Grok OAuth (`xai-oauth`, RFC 8628 device code + `cli-chat-proxy.grok.com`).

All test doubles use `unittest.mock.create_autospec` and make zero network calls.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import create_autospec, patch

import httpx
import pytest

from src.dalton.settings import DEFAULT_XAI_GROK_CLIENT_VERSION
import redis.asyncio
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk

import src.dalton.app.oauth_native as oauth_native_mod
import src.dalton.budget.service as budget_svc
import src.dalton.llm.assist as assist_mod
import src.dalton.llm.complete as complete_mod
import src.dalton.llm.providers.xai_grok.oauth as xai_oauth_mod
import src.dalton.llm.role_policy as role_policy_mod
import src.dalton.services.provider_status as provider_status_mod
import src.llms.error_classification as err_cls_mod
import src.server.database.api_keys as api_keys_db
import src.server.database.oauth_tokens as oauth_tokens_db
from src.dalton.llm.overlay import load_dalton_meta
from src.dalton.llm.providers import build_dalton_client
from src.dalton.llm.providers.xai_grok.client import (
    ChatGrokOpenAI,
    classify_xai_error,
    flatten_tool_outputs_in_input,
    has_stream_started,
    mark_xai_exception,
)
import src.server.services.oauth_health as oauth_health_mod
import src.server.services.oauth_providers as oauth_providers_mod
import src.server.services.oauth_refresh_wait as oauth_refresh_wait_mod
from src.dalton.llm.providers.xai_grok.oauth import (
    XAI_REFRESH_HARD_TIMEOUT_S,
    XAI_REFRESH_LOCK_TTL_MS,
    discover_openid_endpoints,
    extract_token_record,
    fetch_xai_billing,
    fetch_xai_models,
    poll_device_authorization,
    poll_device_token_loop,
    refresh_tokens,
    request_device_code,
    save_xai_tokens,
    validate_xai_endpoint,
)
from src.llms.extension.dashscope import ResponsesStreamFailedError
from src.llms.llm import LLM
from src.server.services.llm.service import LLMService
from src.utils.cache.redis_cache import RedisCacheClient


def _make_jwt(claims: dict[str, Any]) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').decode("ascii").rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    return f"{header}.{payload}.sig"


def _make_response(
    status_code: int,
    *,
    json_data: Any = None,
    text: str = "",
    headers: dict[str, str] | None = None,
    url: str = "https://auth.x.ai/oauth2/token",
) -> httpx.Response:
    req = httpx.Request("POST", url)
    if json_data is not None:
        return httpx.Response(
            status_code,
            json=json_data,
            headers=headers,
            request=req,
        )
    return httpx.Response(
        status_code,
        text=text,
        headers=headers,
        request=req,
    )


# ---------------------------------------------------------------------------
# 1. OpenID discovery & strict x.ai / *.x.ai endpoint validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openid_discovery_and_host_validation() -> None:
    assert validate_xai_endpoint("https://x.ai/oauth/token") == "https://x.ai/oauth/token"
    assert (
        validate_xai_endpoint("https://auth.x.ai/oauth2/device")
        == "https://auth.x.ai/oauth2/device"
    )

    for bad_url in (
        "",
        "http://auth.x.ai/oauth2/token",
        "https://evil.com/oauth2/token",
        "https://auth.x.ai.evil.com/oauth2/token",
        "https://notx.ai/oauth2/token",
        "ftp://auth.x.ai/oauth2/token",
        # Boundary cases: backslash, %00 / null byte, userinfo, uppercase, port
        "https://auth.x.ai\\evil.com/oauth2/token",
        "https://evil.com\\@x.ai/oauth2/token",
        "https://auth.x.ai%5cevil.com/oauth2/token",
        "https://evil.com%00.x.ai/oauth2/token",
        "https://auth.x.ai%00.evil.com/oauth2/token",
        "https://auth.x.ai/oauth2/token%00",
        "https://auth.x.ai\x00/oauth2/token",
        "https://x.ai@evil.com/oauth2/token",
        "https://evil.com@x.ai/oauth2/token",
        "https://user:pass@auth.x.ai/oauth2/token",
        "HTTPS://auth.x.ai/oauth2/token",
        "https://AUTH.X.AI/oauth2/token",
        "https://Auth.X.Ai/oauth2/token",
        "https://auth.x.ai:8443/oauth2/token",
        "https://auth.x.ai:443/oauth2/token",
        "https://x.ai:8080/oauth2/token",
    ):
        with pytest.raises(ValueError, match="xAI OAuth"):
            validate_xai_endpoint(bad_url)

    mock_http = create_autospec(httpx.AsyncClient, instance=True)
    mock_http.get.return_value = _make_response(
        200,
        json_data={
            "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        },
        url="https://auth.x.ai/.well-known/openid-configuration",
    )
    eps = await discover_openid_endpoints(client=mock_http)
    assert eps == {
        "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }
    mock_http.get.assert_awaited_once_with(
        "https://auth.x.ai/.well-known/openid-configuration",
        headers={"Accept": "application/json"},
    )

    # Discovery response pointing token_endpoint to a non-x.ai host must be rejected
    mock_http.get.return_value = _make_response(
        200,
        json_data={
            "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
            "token_endpoint": "https://attacker.example.com/oauth2/token",
        },
    )
    with pytest.raises(ValueError, match="token_endpoint"):
        await discover_openid_endpoints(client=mock_http)


# ---------------------------------------------------------------------------
# 2. Device code request & 4 RFC 8628 polling states + JWT claim extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_code_and_four_polling_states(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DALTON_XAI_OAUTH_CLIENT_ID", "test-grok-build-client")

    mock_http = create_autospec(httpx.AsyncClient, instance=True)
    mock_http.post.return_value = _make_response(
        200,
        json_data={
            "device_code": "dev-code-123",
            "user_code": "GROK-9999",
            "verification_uri": "https://auth.x.ai/activate",
            "verification_uri_complete": "https://auth.x.ai/activate?user_code=GROK-9999",
            "expires_in": 300,
            "interval": 5,
        },
        url="https://auth.x.ai/oauth2/device/code",
    )

    dev = await request_device_code(
        device_authorization_endpoint="https://auth.x.ai/oauth2/device/code",
        token_endpoint="https://auth.x.ai/oauth2/token",
        client=mock_http,
    )
    assert dev["device_code"] == "dev-code-123"
    assert dev["user_code"] == "GROK-9999"
    assert dev["verification_url"] == "https://auth.x.ai/activate?user_code=GROK-9999"
    assert dev["interval"] == 5
    assert dev["expires_in"] == 300
    mock_http.post.assert_awaited_once_with(
        "https://auth.x.ai/oauth2/device/code",
        data={
            "client_id": "test-grok-build-client",
            "scope": "openid profile email offline_access grok-cli:access api:access",
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    # State 1: authorization_pending -> pending=True, interval unchanged (5)
    mock_http.post.return_value = _make_response(
        400,
        json_data={"error": "authorization_pending"},
    )
    st_pending = await poll_device_authorization(
        "dev-code-123",
        token_endpoint="https://auth.x.ai/oauth2/token",
        interval=5,
        client=mock_http,
    )
    assert st_pending == {
        "status": "authorization_pending",
        "pending": True,
        "interval": 5,
    }

    # State 2: slow_down -> pending=True, interval increased by +5s (5 -> 10)
    mock_http.post.return_value = _make_response(
        400,
        json_data={"error": "slow_down"},
    )
    st_slow = await poll_device_authorization(
        "dev-code-123",
        token_endpoint="https://auth.x.ai/oauth2/token",
        interval=5,
        client=mock_http,
    )
    assert st_slow == {
        "status": "slow_down",
        "pending": True,
        "interval": 10,
    }

    # State 3: access_denied -> raises ValueError
    mock_http.post.return_value = _make_response(
        400,
        json_data={"error": "access_denied"},
    )
    with pytest.raises(ValueError, match="access_denied"):
        await poll_device_authorization(
            "dev-code-123",
            token_endpoint="https://auth.x.ai/oauth2/token",
            interval=5,
            client=mock_http,
        )

    # State 4: expired_token -> raises ValueError
    mock_http.post.return_value = _make_response(
        400,
        json_data={"error": "expired_token"},
    )
    with pytest.raises(ValueError, match="expired_token"):
        await poll_device_authorization(
            "dev-code-123",
            token_endpoint="https://auth.x.ai/oauth2/token",
            interval=5,
            client=mock_http,
        )

    # Approved state + poll_device_token_loop: pending -> slow_down (+5s) -> approved
    future_exp = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
    fake_jwt = _make_jwt(
        {
            "sub": "xai-user-sub-42",
            "email": "analyst@dalton.fund",
            "name": "Dalton Analyst",
            "exp": future_exp,
        }
    )
    mock_http.post.side_effect = [
        _make_response(400, json_data={"error": "authorization_pending"}),
        _make_response(400, json_data={"error": "slow_down"}),
        _make_response(
            200,
            json_data={
                "access_token": fake_jwt,
                "refresh_token": "xai-ref-initial",
                # Omit expires_in to verify fallback to JWT exp claim
            },
        ),
    ]
    sleep_delays: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", create_autospec(asyncio.sleep, side_effect=_fake_sleep))

    loop_res = await poll_device_token_loop(
        "dev-code-123",
        token_endpoint="https://auth.x.ai/oauth2/token",
        interval=5.0,
        max_wait_seconds=300.0,
        client=mock_http,
    )
    assert sleep_delays == [5.0, 10.0]
    assert loop_res["pending"] is False
    assert loop_res["access"] == fake_jwt
    assert loop_res["refresh"] == "xai-ref-initial"
    assert loop_res["expires"] == future_exp
    assert loop_res["accountId"] == "xai-user-sub-42"
    assert loop_res["email"] == "analyst@dalton.fund"
    assert loop_res["displayName"] == "Dalton Analyst"
    assert loop_res["token_endpoint"] == "https://auth.x.ai/oauth2/token"


# ---------------------------------------------------------------------------
# 3. Token refresh: keep old refresh_token when omitted, Cloudflare challenge retry, Redis lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_tokens_keeps_old_refresh_and_retries_only_cloudflare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_XAI_OAUTH_CLIENT_ID", "test-grok-build-client")
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", create_autospec(asyncio.sleep, side_effect=_fake_sleep))

    mock_http = create_autospec(httpx.AsyncClient, instance=True)
    new_jwt = _make_jwt({"sub": "xai-sub-1", "email": "pm@dalton.fund"})

    # 2 Cloudflare challenges (1st via cf-mitigated header, 2nd via HTML body) -> 3rd succeeds without refresh_token
    mock_http.post.side_effect = [
        _make_response(
            403,
            text="challenge",
            headers={"cf-mitigated": "challenge"},
        ),
        _make_response(
            503,
            text="<!DOCTYPE html><html><body>Just a moment...</body></html>",
            headers={"content-type": "text/html; charset=UTF-8"},
        ),
        _make_response(
            200,
            json_data={
                "access_token": new_jwt,
                "expires_in": 7200,
                # No refresh_token in response -> must keep old refresh token
            },
        ),
    ]

    refreshed = await refresh_tokens(
        "old-refresh-token-keep-me",
        token_endpoint="https://auth.x.ai/oauth2/token",
        client=mock_http,
    )
    assert refreshed["access"] == new_jwt
    assert refreshed["refresh"] == "old-refresh-token-keep-me"
    assert refreshed["refresh_token"] == "old-refresh-token-keep-me"
    assert refreshed["expires_in"] == 7200
    assert refreshed["accountId"] == "xai-sub-1"
    assert refreshed["token_endpoint"] == "https://auth.x.ai/oauth2/token"
    assert sleep_calls == [0.25, 0.25]
    assert mock_http.post.await_count == 3

    # Non-Cloudflare 400 JSON error must NOT retry
    sleep_calls.clear()
    mock_http.post.reset_mock()
    mock_http.post.side_effect = [
        _make_response(
            400,
            json_data={"error": "invalid_grant"},
            headers={"content-type": "application/json"},
        )
    ]
    with pytest.raises(httpx.HTTPStatusError):
        await refresh_tokens(
            "bad-refresh-token",
            token_endpoint="https://auth.x.ai/oauth2/token",
            client=mock_http,
        )
    assert mock_http.post.await_count == 1
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_get_valid_token_redis_lock_and_upsert(monkeypatch: pytest.MonkeyPatch) -> None:
    """The xAI line serves through the unified refresh protocol: the registry
    adapter keeps the Cloudflare-retry refresh, a fresh TokenSet carries the
    SuperGrok plan, and the save is the conditional replace on the holder's
    row — never a blind upsert. The sign-in save (``save_xai_tokens``) still
    upserts, from the poll's connection lock."""
    monkeypatch.setenv("DALTON_XAI_OAUTH_CLIENT_ID", "test-grok-build-client")
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    stored_row = {
        "provider": "xai-oauth",
        "access_token": "expired-access",
        "refresh_token": "persisted-refresh",
        "account_id": "xai-sub-1",
        "email": "pm@dalton.fund",
        "plan_type": "supergrok",
        "expires_at": expired_at,
    }
    monkeypatch.setattr(
        oauth_health_mod,
        "get_oauth_tokens",
        create_autospec(oauth_health_mod.get_oauth_tokens, return_value=dict(stored_row)),
    )
    fake_replace = create_autospec(oauth_tokens_db.replace_refreshed_tokens, return_value=True)
    monkeypatch.setattr(oauth_health_mod, "replace_refreshed_tokens", fake_replace)
    fake_upsert = create_autospec(xai_oauth_mod.upsert_oauth_tokens, return_value=None)
    monkeypatch.setattr(xai_oauth_mod, "upsert_oauth_tokens", fake_upsert)

    fake_cache = create_autospec(RedisCacheClient(), instance=True)
    fake_cache.acquire_lock.return_value = True
    fake_cache.release_lock.return_value = None
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: fake_cache, return_value=fake_cache),
    )

    new_jwt = _make_jwt({"sub": "xai-sub-1", "email": "pm@dalton.fund"})
    rec = extract_token_record(
        {"access_token": new_jwt, "expires_in": 3600},
        token_endpoint="https://auth.x.ai/oauth2/token",
        fallback_refresh="persisted-refresh",
    )
    monkeypatch.setattr(
        xai_oauth_mod,
        "refresh_tokens",
        create_autospec(xai_oauth_mod.refresh_tokens, return_value=rec),
    )

    tok = await oauth_health_mod.valid_token("user-123", "xai-oauth")
    assert tok is not None
    assert tok.access_token == new_jwt
    # The CLI answer names no plan; the adapter stands the SuperGrok plan in.
    assert tok.plan_type == "supergrok"
    fake_cache.acquire_lock.assert_awaited_once()
    fake_cache.release_lock.assert_awaited_once()
    fake_replace.assert_awaited_once()
    replace_kwargs = fake_replace.call_args.kwargs
    assert replace_kwargs["spent_refresh_token"] == "persisted-refresh"
    assert fake_replace.call_args.args == ("user-123", "xai-oauth")
    assert replace_kwargs["plan_type"] == "supergrok"
    assert replace_kwargs["refresh_token"] == "persisted-refresh"
    # The unified refresh path never blindly upserts.
    fake_upsert.assert_not_awaited()

    # The sign-in save is a plain upsert (over the poll's connection lock).
    await save_xai_tokens("user-123", rec)
    assert fake_upsert.await_count == 1


# ---------------------------------------------------------------------------
# 4. Request headers & body assembly (x-grok-model-override, include, effort removal)
# ---------------------------------------------------------------------------


def test_grok_client_headers_and_body_assembly() -> None:
    llm = LLM("grok-4.7-oauth", api_key="fake-supergrok-jwt")
    client = build_dalton_client(llm, cache_key="thread-session-abc")
    assert isinstance(client, ChatGrokOpenAI)
    assert client.openai_api_base == "https://cli-chat-proxy.grok.com/v1"

    headers = client.default_headers or {}
    assert headers["Authorization"] == "Bearer fake-supergrok-jwt"
    assert headers["X-XAI-Token-Auth"] == "xai-grok-cli"
    assert headers["x-grok-client-version"] == DEFAULT_XAI_GROK_CLIENT_VERSION
    assert headers["x-grok-model-override"] == "grok-4.7"
    assert headers["x-client-request-id"] == "thread-session-abc"

    payload = client._get_request_payload([HumanMessage(content="Analyze NVDA")])
    assert payload["model"] == "grok-4.7"
    assert payload["stream"] is True
    assert payload["store"] is False
    assert payload["prompt_cache_key"] == "thread-session-abc"
    assert payload["max_output_tokens"] == 64000
    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "reasoning.encrypted_content" in payload["include"]

    # When model does not support reasoning effort, reasoning block is removed
    no_effort_client = ChatGrokOpenAI(
        model="grok-non-reasoning",
        api_key="fake-supergrok-jwt",
        session_id="sess-2",
        supports_reasoning_effort=False,
        is_reasoning_model=False,
        reasoning={"effort": "high", "summary": "auto"},
    )
    payload_no_effort = no_effort_client._get_request_payload([HumanMessage(content="ping")])
    assert "reasoning" not in payload_no_effort


# ---------------------------------------------------------------------------
# 5. Tool output flattening & moving images into a subsequent user message
# ---------------------------------------------------------------------------


def test_tool_output_flattening_and_image_extraction() -> None:
    raw_items = [
        {
            "type": "function_call_output",
            "call_id": "call_chart_1",
            "output": [
                {"type": "input_text", "text": "Chart generated:"},
                {"type": "text", "text": "Revenue YoY +42%"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
                },
            ],
        }
    ]
    flattened = flatten_tool_outputs_in_input(raw_items)
    assert len(flattened) == 2
    assert flattened[0] == {
        "type": "function_call_output",
        "call_id": "call_chart_1",
        "output": "Chart generated:\nRevenue YoY +42%",
    }
    assert flattened[1] == {
        "type": "message",
        "role": "user",
        "content": [
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
            }
        ],
    }

    # Also verify end-to-end through ChatGrokOpenAI._get_request_payload with LangChain ToolMessage
    client = ChatGrokOpenAI(
        model="grok-4.7",
        api_key="fake-jwt",
        session_id="sess-tool",
    )
    messages = [
        HumanMessage(content="Plot revenue"),
        AIMessage(
            content="",
            tool_calls=[{"name": "execute_code", "args": {"code": "plot()"}, "id": "call_99"}],
        ),
        ToolMessage(
            content=[
                {"type": "text", "text": "Done plotting"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
            tool_call_id="call_99",
        ),
    ]
    payload = client._get_request_payload(messages)
    input_items = payload["input"]
    fn_out_idx = next(
        i
        for i, item in enumerate(input_items)
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    )
    assert input_items[fn_out_idx]["output"] == "Done plotting"
    follow_up = input_items[fn_out_idx + 1]
    assert follow_up["type"] == "message"
    assert follow_up["role"] == "user"
    assert follow_up["content"] == [
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
    ]


# ---------------------------------------------------------------------------
# 6. Models & billing endpoints + error classification & failover wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_models_and_billing_and_error_classification() -> None:
    mock_http = create_autospec(httpx.AsyncClient, instance=True)
    mock_http.get.return_value = _make_response(
        200,
        json_data={
            "data": [
                {
                    "id": "grok-4.7",
                    "name": "Grok 4.7",
                    "context_window": 256000,
                    "supports_reasoning_effort": True,
                    "reasoning_efforts": ["low", "medium", "high"],
                }
            ]
        },
        url="https://cli-chat-proxy.grok.com/v1/models",
    )
    models = await fetch_xai_models("tok-abc", client=mock_http)
    assert len(models) == 1
    assert models[0]["id"] == "grok-4.7"
    assert models[0]["name"] == "Grok 4.7"
    assert models[0]["context_window"] == 256000
    assert "max_completion_tokens" not in models[0]
    assert models[0]["supports_reasoning_effort"] is True

    # Billing endpoint: 200 OK with config.creditUsagePercent
    mock_http.get.return_value = _make_response(
        200,
        json_data={
            "config": {"creditUsagePercent": 37.5, "used": 75, "monthlyLimit": 200},
            "currentPeriod": "2026-10",
            "subscription_tier": "supergrok",
        },
        url="https://cli-chat-proxy.grok.com/v1/billing?format=credits",
    )
    bill = await fetch_xai_billing("tok-abc", client=mock_http)
    assert bill["ok"] is True
    assert bill["token_expired"] is False
    assert bill["credit_usage_percent"] == 37.5
    assert bill["current_period"] == "2026-10"
    assert bill["subscription_tier"] == "supergrok"
    billing_headers = mock_http.get.call_args.kwargs["headers"]
    assert billing_headers["x-grok-client-mode"] == "cli"
    assert billing_headers["x-grok-client-version"] == DEFAULT_XAI_GROK_CLIENT_VERSION

    # Billing endpoint: 401/403 treated as token_expired
    mock_http.get.return_value = _make_response(401, text="Unauthorized")
    bill_exp = await fetch_xai_billing("tok-expired", client=mock_http)
    assert bill_exp["ok"] is False
    assert bill_exp["token_expired"] is True

    # Error classification: scoped ONLY to xAI (xai-oauth / sdk grok)
    for msg in (
        "You have used all credits for this billing cycle",
        "Reached monthly spending limit on your account",
        "You need a Grok subscription to access this model",
    ):
        err = mark_xai_exception(RuntimeError(msg))
        assert classify_xai_error(err, require_xai_origin=True) == "billing"
        assert err_cls_mod.is_retryable_error(err) is False
        assert complete_mod._classify_exception(err, provider="xai-oauth") == ("failed", "billing")
        assert (
            assist_mod._is_safe_to_fallback(
                complete_mod.DaltonCompleteResult(ok=False, status="failed", error_kind="billing")
            )
            is True
        )
        # Mid-stream billing failure (stream already started / yielded output) is NOT safe to fallback
        mid_err = mark_xai_exception(RuntimeError(msg), stream_started=True, chunks_received=3)
        assert complete_mod._classify_exception(mid_err, provider="xai-oauth") == (
            "indeterminate",
            "stream_cut",
        )
        assert (
            assist_mod._is_safe_to_fallback(
                complete_mod.DaltonCompleteResult(
                    ok=False,
                    status="failed",
                    error_kind="billing",
                    text="partial output",
                )
            )
            is False
        )
        assert (
            assist_mod._is_safe_to_fallback(
                complete_mod.DaltonCompleteResult(
                    ok=False,
                    status="failed",
                    error_kind="billing",
                    usage={"output_tokens": 12},
                )
            )
            is False
        )

    for msg in (
        "Rate limit exceeded, please try again shortly",
        "Too many requests sent in a short window",
    ):
        err = mark_xai_exception(RuntimeError(msg))
        assert classify_xai_error(err, require_xai_origin=True) == "rate_limit"
        assert err_cls_mod.is_retryable_error(err) is True
        assert complete_mod._classify_exception(err, provider="xai-oauth") == (
            "indeterminate",
            "rate_limit",
        )

    # Other provider returning 401 with "too many requests" keeps original classification (not retryable)
    other_401 = RuntimeError("HTTP 401 Unauthorized: too many requests")
    setattr(other_401, "status_code", 401)
    assert classify_xai_error(other_401, require_xai_origin=True) is None
    assert err_cls_mod.is_retryable_error(other_401) is False
    assert err_cls_mod.is_retryable_error(other_401, status_code=401) is False
    assert complete_mod._classify_exception(other_401, provider="openai") == ("failed", "http_401")

    # Even on xAI, rate-limit prose must NOT override an explicit non-retryable status (e.g. 401/403)
    xai_401_rate_prose = mark_xai_exception(RuntimeError("401 Unauthorized: too many requests"))
    setattr(xai_401_rate_prose, "status_code", 401)
    assert err_cls_mod.is_retryable_error(xai_401_rate_prose, status_code=401) is False
    assert complete_mod._classify_exception(xai_401_rate_prose, provider="xai-oauth") == (
        "failed",
        "http_401",
    )


# ---------------------------------------------------------------------------
# 7. Budget provider mapping & provider_status row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_mapping_and_provider_status_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # grok-*-oauth maps to xai-oauth before grok-* -> xai
    assert budget_svc._resolve_provider_for_model("grok-4.7-oauth", "oauth") == "xai-oauth"
    assert budget_svc._resolve_provider_for_model("grok-4.3-oauth", "oauth") == "xai-oauth"
    assert (
        budget_svc._resolve_provider_for_model("grok-4.7-build-fast-oauth", "oauth")
        == "xai-oauth"
    )
    assert budget_svc._resolve_provider_for_model("grok-future-oauth", "oauth") == "xai-oauth"
    assert budget_svc._resolve_provider_for_model("grok-4.7", "platform") == "xai"

    # build-fast notional_ref points to grok-4.7 with explanatory comment
    meta = load_dalton_meta() or {}
    bf_meta = meta.get("grok-4.7-build-fast-oauth") or {}
    assert bf_meta.get("notional_ref") == "grok-4.7"
    assert "grok-4.7" in str(bf_meta.get("comment") or "")
    assert bf_meta.get("supports_reasoning_effort") is False

    billing_type, prov = complete_mod.resolve_model_billing_and_provider("grok-4.7-oauth")
    assert billing_type == "oauth"
    assert prov == "xai-oauth"
    cash_usd, notional_usd = complete_mod.compute_call_costs(
        "grok-4.7-oauth",
        billing_type,
        {"input_tokens": 1000, "output_tokens": 500, "cached_tokens": 0},
    )
    assert cash_usd == Decimal("0")
    assert notional_usd > Decimal("0")

    assert role_policy_mod.family_of("grok-4.7-oauth") == "xai-grok"
    assert role_policy_mod.family_of("grok-4.3-oauth") == "xai-grok"
    assert role_policy_mod.family_of("grok-4.7-build-fast-oauth") == "xai-grok"

    # provider_status row for xai-oauth reads its billing through the unified
    # refresh protocol (valid_token), then fetch_xai_billing
    expires_dt = datetime.now(timezone.utc) + timedelta(hours=1)

    async def _fake_oauth_connection(user_id: str, provider: str) -> dict[str, Any]:
        if provider == "xai-oauth":
            return {
                "email": "analyst@dalton.fund",
                "plan_type": "supergrok",
                "expires_at": expires_dt,
            }
        return None

    monkeypatch.setattr(
        oauth_tokens_db,
        "get_oauth_connection",
        create_autospec(oauth_tokens_db.get_oauth_connection, side_effect=_fake_oauth_connection),
    )
    from src.server.services.oauth_protocol import TokenSet

    mock_get_valid_token = create_autospec(
        oauth_health_mod.valid_token,
        return_value=TokenSet(
            access_token="tok-xai-refreshed",
            refresh_token="",
            expires_at=expires_dt,
            plan_type="supergrok",
        ),
    )
    monkeypatch.setattr(
        oauth_health_mod,
        "valid_token",
        mock_get_valid_token,
    )
    mock_fetch_billing = create_autospec(
        xai_oauth_mod.fetch_xai_billing,
        return_value={
            "ok": True,
            "token_expired": False,
            "credit_usage_percent": 22.5,
            "used": 45,
            "monthly_limit": 200,
            "current_period": "2026-10",
            "subscription_tier": "supergrok",
        },
    )
    monkeypatch.setattr(
        xai_oauth_mod,
        "fetch_xai_billing",
        mock_fetch_billing,
    )
    monkeypatch.setattr(
        api_keys_db,
        "get_user_api_keys",
        create_autospec(
            api_keys_db.get_user_api_keys,
            return_value={"byok_enabled": False, "keys": {}, "base_urls": {}},
        ),
    )
    monkeypatch.setattr(
        provider_status_mod,
        "_fetch_usage_stats_24h",
        create_autospec(provider_status_mod._fetch_usage_stats_24h, return_value={}),
    )
    monkeypatch.setattr(
        provider_status_mod,
        "get_last_refresh_event",
        create_autospec(provider_status_mod.get_last_refresh_event, return_value=None),
    )
    monkeypatch.setattr(
        provider_status_mod,
        "list_recent_provider_events",
        create_autospec(provider_status_mod.list_recent_provider_events, return_value=[]),
    )
    monkeypatch.setattr(
        provider_status_mod,
        "count_provider_errors_24h",
        create_autospec(provider_status_mod.count_provider_errors_24h, return_value=0),
    )

    status_payload = await provider_status_mod.get_providers_status(user_id="local-dev-user")
    xai_oauth_row = next(
        p for p in status_payload["providers"] if p["provider"] == "xai-oauth"
    )
    mock_get_valid_token.assert_awaited_once_with("local-dev-user", "xai-oauth")
    mock_fetch_billing.assert_awaited_once_with("tok-xai-refreshed")
    assert xai_oauth_row["configured"] is True
    assert xai_oauth_row["billing"] == "subscription"
    assert xai_oauth_row["access_type"] == "oauth"
    assert xai_oauth_row["tos_risk"] == "high"
    assert xai_oauth_row["default_probe_model"] == "grok-4.7-oauth"
    assert xai_oauth_row["quota"]["credit_usage_percent"] == 22.5
    assert xai_oauth_row["credential"]["quota"]["subscription_tier"] == "supergrok"
    assert xai_oauth_row["warnings"] == []

    # When valid_token fails (refresh failed), warning "请重新连接" is emitted
    mock_get_valid_token.return_value = None
    status_failed = await provider_status_mod.get_providers_status(user_id="local-dev-user")
    xai_failed_row = next(
        p for p in status_failed["providers"] if p["provider"] == "xai-oauth"
    )
    assert xai_failed_row["credential"]["token_expired"] is True
    assert any("请重新连接" in w["message"] for w in xai_failed_row["warnings"])


# ---------------------------------------------------------------------------
# 8. Concurrent refresh lock, DB re-read after lock, lock ownership, discovery cache,
#    slow_down deadline preservation, and build-fast reasoning capability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_refresh_only_refreshes_once_and_verifies_lock_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the unified protocol, the xAI line's single-use refresh token is
    spent once however many turns ask together: one lock holder refreshes, the
    others wait for its save; and a holder whose pre-lock read went stale
    re-reads inside the lock and refreshes nobody's spent token."""
    monkeypatch.setenv("DALTON_XAI_OAUTH_CLIENT_ID", "test-grok-build-client")
    assert XAI_REFRESH_LOCK_TTL_MS >= 150_000
    assert not hasattr(xai_oauth_mod, "_TOKEN_META_CACHE")

    now = datetime.now(timezone.utc)
    db_state: dict[str, Any] = {
        "provider": "xai-oauth",
        "access_token": "expired-access-0",
        "refresh_token": "single-use-refresh-token",
        "account_id": "xai-sub-1",
        "email": "pm@dalton.fund",
        "plan_type": "supergrok",
        "expires_at": now - timedelta(minutes=2),
    }

    async def _fake_get_tokens(user_id: str, provider: str) -> dict[str, Any] | None:
        return dict(db_state)

    async def _fake_replace(
        user_id: str,
        provider: str,
        *,
        spent_refresh_token: str,
        access_token: str,
        refresh_token: str,
        account_id: str,
        email: str | None,
        plan_type: str | None,
        expires_at: datetime | None,
    ) -> bool:
        assert spent_refresh_token == "single-use-refresh-token"
        db_state["access_token"] = access_token
        db_state["refresh_token"] = refresh_token
        db_state["account_id"] = account_id
        db_state["email"] = email
        db_state["plan_type"] = plan_type
        db_state["expires_at"] = expires_at
        return True

    real_get_tokens = oauth_tokens_db.get_oauth_tokens
    mock_get_tokens = create_autospec(real_get_tokens, side_effect=_fake_get_tokens)
    monkeypatch.setattr(oauth_health_mod, "get_oauth_tokens", mock_get_tokens)
    monkeypatch.setattr(
        oauth_refresh_wait_mod,
        "get_oauth_tokens",
        create_autospec(real_get_tokens, side_effect=_fake_get_tokens),
    )
    monkeypatch.setattr(
        oauth_health_mod,
        "replace_refreshed_tokens",
        create_autospec(oauth_tokens_db.replace_refreshed_tokens, side_effect=_fake_replace),
    )

    # Stateful lock store verifying compare-and-delete ownership via redis.asyncio.Redis autospec
    locks: dict[str, str] = {}

    async def _acquire_lock(key: str, token: str, ttl_ms: int) -> bool:
        if key in locks:
            return False
        locks[key] = token
        return True

    async def _release_lock(key: str, token: str) -> None:
        if locks.get(key) == token:
            del locks[key]

    async def _redis_eval(script: str, numkeys: int, *keys_and_args: Any) -> int:
        key = str(keys_and_args[0])
        token = str(keys_and_args[1])
        if locks.get(key) != token:
            return 0
        if "del" in script:
            del locks[key]
        return 1

    fake_redis = create_autospec(redis.asyncio.Redis, instance=True)
    fake_redis.eval.side_effect = _redis_eval

    fake_cache = create_autospec(RedisCacheClient(), instance=True)
    fake_cache.enabled = True
    fake_cache.client = fake_redis
    fake_cache.acquire_lock.side_effect = _acquire_lock
    fake_cache.release_lock.side_effect = _release_lock
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: fake_cache, return_value=fake_cache),
    )

    refresh_tokens_used: list[str] = []
    new_jwt = _make_jwt({"sub": "xai-sub-1", "email": "pm@dalton.fund"})

    async def _fake_refresh(
        refresh_token: str,
        *,
        token_endpoint: str | None = None,
        user_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        refresh_tokens_used.append(refresh_token)
        await asyncio.sleep(0.05)
        return extract_token_record(
            {
                "access_token": new_jwt,
                "refresh_token": "rotated-refresh-token-2",
                "expires_in": 3600,
            },
            token_endpoint="https://auth.x.ai/oauth2/token",
        )

    mock_refresh = create_autospec(xai_oauth_mod.refresh_tokens, side_effect=_fake_refresh)
    monkeypatch.setattr(xai_oauth_mod, "refresh_tokens", mock_refresh)

    # 1) Two coroutines refreshing concurrently: only 1 refresh request is issued,
    #    old refresh_token is used only once, and both receive the new access token.
    res1, res2 = await asyncio.gather(
        oauth_health_mod.valid_token("user-123", "xai-oauth"),
        oauth_health_mod.valid_token("user-123", "xai-oauth"),
    )
    assert res1 is not None and res2 is not None
    assert res1.access_token == new_jwt
    assert res2.access_token == new_jwt
    assert mock_refresh.await_count == 1
    assert refresh_tokens_used == ["single-use-refresh-token"]
    assert locks == {}

    # 2) Worker B acquires lock AFTER Worker A already refreshed DB while Worker B had stale pre-lock read:
    #    DB re-read inside lock sees fresh token and does NOT call refresh_tokens a second time.
    call_seq = 0
    expired_snapshot = {
        **db_state,
        "access_token": "stale-access",
        "refresh_token": "single-use-refresh-token",
        "expires_at": now - timedelta(minutes=2),
    }

    async def _stale_then_fresh_get(user_id: str, provider: str) -> dict[str, Any] | None:
        nonlocal call_seq
        call_seq += 1
        if call_seq == 1:
            return dict(expired_snapshot)
        return dict(db_state)

    mock_get_tokens.side_effect = _stale_then_fresh_get
    res3 = await oauth_health_mod.valid_token("user-123", "xai-oauth")
    assert res3 is not None
    assert res3.access_token == new_jwt
    assert mock_refresh.await_count == 1  # Still only 1 refresh call total!



@pytest.mark.asyncio
async def test_per_user_redis_discovery_cache_and_slow_down_preserves_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_XAI_OAUTH_CLIENT_ID", "test-grok-build-client")

    redis_store: dict[str, tuple[str, int | None]] = {}

    async def _redis_get(name: Any) -> str | None:
        item = redis_store.get(str(name))
        return item[0] if item else None

    async def _redis_set(name: Any, value: Any, ex: Any = None, **kwargs: Any) -> bool:
        redis_store[str(name)] = (str(value), int(ex) if ex is not None else None)
        return True

    async def _redis_delete(*names: Any) -> int:
        for n in names:
            redis_store.pop(str(n), None)
        return 1

    async def _redis_ttl(name: Any) -> int:
        item = redis_store.get(str(name))
        return int(item[1] or 0) if item else -2

    fake_redis = create_autospec(redis.asyncio.Redis, instance=True)
    fake_redis.get.side_effect = _redis_get
    fake_redis.set.side_effect = _redis_set
    fake_redis.delete.side_effect = _redis_delete
    fake_redis.ttl.side_effect = _redis_ttl

    fake_cache = create_autospec(RedisCacheClient(), instance=True)
    fake_cache.enabled = True
    fake_cache.client = fake_redis

    # The poll's slow_down writeback is a compare-and-set on the value its
    # read saw, keeping the key's TTL — wired here over the same store.
    async def _swap(key: str, old_value: str, new_value: str) -> bool:
        item = redis_store.get(str(key))
        if item is None or item[0] != old_value:
            return False
        redis_store[str(key)] = (new_value, item[1])
        return True

    fake_cache.swap.side_effect = _swap
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: fake_cache, return_value=fake_cache),
    )

    mock_http = create_autospec(httpx.AsyncClient, instance=True)
    mock_http.get.return_value = _make_response(
        200,
        json_data={
            "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        },
        url="https://auth.x.ai/.well-known/openid-configuration",
    )

    # First discovery for user-A hits HTTP and caches in Redis with TTL 3600s
    eps1 = await discover_openid_endpoints(user_id="user-A", client=mock_http)
    assert eps1["token_endpoint"] == "https://auth.x.ai/oauth2/token"
    assert mock_http.get.await_count == 1
    assert "oauth:xai:discovery:user-A" in redis_store
    assert redis_store["oauth:xai:discovery:user-A"][1] == 3600

    # Second discovery for user-A reads from Redis cache without HTTP call
    eps2 = await discover_openid_endpoints(user_id="user-A", client=mock_http)
    assert eps2 == eps1
    assert mock_http.get.await_count == 1

    # Backend xai_device_poll on slow_down preserves original expires_at deadline instead of resetting 300s
    now_ts = int(datetime.now(timezone.utc).timestamp())
    original_expires_at = now_ts + 120  # 120s left out of initial 300s
    redis_store["oauth:xai:device:user-A"] = (
        json.dumps(
            {
                "device_code": "dev-1",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
                "interval": 5,
                "ttl": 300,
                "expires_at": original_expires_at,
            }
        ),
        120,
    )
    monkeypatch.setattr(
        oauth_native_mod,
        "xai_poll_device_authorization",
        create_autospec(
            oauth_native_mod.xai_poll_device_authorization,
            return_value={"status": "slow_down", "pending": True, "interval": 10},
        ),
    )
    poll_resp = await oauth_native_mod.xai_device_poll(user_id="user-A")
    assert poll_resp == {"pending": True, "interval": 10}
    saved_json, saved_ttl = redis_store["oauth:xai:device:user-A"]
    saved_device = json.loads(saved_json)
    assert saved_device["interval"] == 10
    assert saved_device["expires_at"] == original_expires_at
    assert saved_ttl is not None and 1 <= saved_ttl <= 120


@pytest.mark.asyncio
async def test_build_fast_and_explicit_param_strip_unsupported_reasoning() -> None:
    assert not hasattr(xai_oauth_mod, "_KNOWN_MODEL_CAPABILITIES")

    # 1) grok-4.7-build-fast-oauth declares supports_reasoning_effort=false in native_overlay.json
    llm_fast = LLM("grok-4.7-build-fast-oauth", api_key="fake-supergrok-jwt")
    client_fast = build_dalton_client(llm_fast, cache_key="sess-fast")
    assert isinstance(client_fast, ChatGrokOpenAI)
    assert client_fast.supports_reasoning_effort is False
    payload_fast = client_fast._get_request_payload([HumanMessage(content="quick build")])
    assert payload_fast["model"] == "grok-4.7-build-fast"
    assert "reasoning" not in payload_fast
    assert "include" not in payload_fast

    # 2) Explicit supports_reasoning_effort=False strips reasoning without any global dict
    dynamic_client = ChatGrokOpenAI(
        model="grok-lite-no-effort",
        api_key="fake-supergrok-jwt",
        session_id="sess-dyn",
        supports_reasoning_effort=False,
        is_reasoning_model=False,
        reasoning={"effort": "high", "summary": "auto"},
    )
    payload_dyn = dynamic_client._get_request_payload([HumanMessage(content="hello")])
    assert "reasoning" not in payload_dyn


@pytest.mark.asyncio
async def test_responses_stream_failed_error_zero_chunks_billing_fallback_and_nonzero_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ResponsesStreamFailedError has class attribute mid_stream = True
    assert ResponsesStreamFailedError.mid_stream is True

    # 1) 0 chunks + billing prose -> has_stream_started is False, classified as failed/billing,
    #    and assist_json falls back to the fallback model.
    zero_chunk_err = mark_xai_exception(
        ResponsesStreamFailedError("response.failed: You have used all credits for this month"),
        stream_started=False,
        chunks_received=0,
    )
    assert has_stream_started(zero_chunk_err) is False
    assert complete_mod._classify_exception(zero_chunk_err, provider="xai-oauth") == (
        "failed",
        "billing",
    )

    monkeypatch.setattr(
        assist_mod,
        "resolve_assist_model",
        create_autospec(assist_mod.resolve_assist_model, return_value="grok-4.7-oauth"),
    )
    monkeypatch.setattr(
        assist_mod,
        "resolve_assist_fallback_models",
        create_autospec(
            assist_mod.resolve_assist_fallback_models,
            return_value=["grok-4.3-oauth"],
        ),
    )

    call_models: list[str] = []

    async def _complete_zero_chunk_then_ok(
        *,
        user_id: str,
        system_prompt: str | None = None,
        user_prompt: str = "",
        response_schema: Any = None,
        slot: str = "background",
        request_model: str | None = None,
        is_byok: bool = True,
        reasoning_effort: str | None = None,
        disable_tracing: bool = True,
        return_token_usage: bool = True,
    ) -> Any:
        call_models.append(str(request_model))
        if request_model == "grok-4.7-oauth":
            raise zero_chunk_err
        return ({"verdict": "ok"}, {"input_tokens": 10, "output_tokens": 5})

    fake_llm_service = create_autospec(LLMService, instance=True)
    fake_llm_service.complete.side_effect = _complete_zero_chunk_then_ok

    comp_zero = await complete_mod.dalton_complete(
        llm_service=fake_llm_service,
        user_id="user-1",
        user_prompt="ping",
        request_model="grok-4.7-oauth",
        skip_journal=True,
    )
    assert comp_zero.ok is False
    assert comp_zero.status == "failed"
    assert comp_zero.error_kind == "billing"
    assert comp_zero.usage.get("stream_started") is False
    assert comp_zero.usage.get("chunks_received") == 0
    assert assist_mod._is_safe_to_fallback(comp_zero) is True

    call_models.clear()
    assist_res_zero = await assist_mod.assist_json(
        task="test_xai_billing_fb",
        instructions="Return JSON verdict",
        payload={"q": 1},
        schema={
            "type": "object",
            "properties": {"verdict": {"type": "string"}},
            "required": ["verdict"],
        },
        user_id="user-1",
        llm_service=fake_llm_service,
        skip_journal=True,
    )
    assert assist_res_zero.ok is True
    assert assist_res_zero.data == {"verdict": "ok"}
    assert assist_res_zero.model == "grok-4.3-oauth"
    assert call_models == ["grok-4.7-oauth", "grok-4.3-oauth"]

    # 2) >= 1 chunks + billing prose -> has_stream_started is True, does NOT fall back.
    partial_err = mark_xai_exception(
        ResponsesStreamFailedError("response.failed: You have used all credits for this month"),
        stream_started=True,
        chunks_received=2,
    )
    assert has_stream_started(partial_err) is True

    async def _complete_partial_fail(**kwargs: Any) -> Any:
        call_models.append(str(kwargs.get("request_model")))
        raise partial_err

    fake_llm_service.complete.side_effect = _complete_partial_fail
    call_models.clear()

    comp_partial = await complete_mod.dalton_complete(
        llm_service=fake_llm_service,
        user_id="user-1",
        user_prompt="ping",
        request_model="grok-4.7-oauth",
        skip_journal=True,
    )
    assert comp_partial.ok is False
    assert comp_partial.usage.get("stream_started") is True
    assert comp_partial.usage.get("chunks_received") == 2
    assert assist_mod._has_stream_output_started(comp_partial) is True
    assert assist_mod._is_safe_to_fallback(comp_partial) is False

    call_models.clear()
    assist_res_partial = await assist_mod.assist_json(
        task="test_xai_billing_no_fb",
        instructions="Return JSON verdict",
        payload={"q": 2},
        schema={
            "type": "object",
            "properties": {"verdict": {"type": "string"}},
            "required": ["verdict"],
        },
        user_id="user-1",
        llm_service=fake_llm_service,
        skip_journal=True,
    )
    assert assist_res_partial.ok is False
    assert call_models == ["grok-4.7-oauth"]  # Fallback was NOT attempted


@pytest.mark.asyncio
async def test_refresh_lock_expired_on_renew_and_hard_timeout_skip_db_write_and_preserve_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unified protocol's answers to the old refresher's worries, on the
    xAI line: the lease outlives the whole refresh; an answer naming no
    identity keeps the stored one; the save is conditional on the refresh
    token it spent, so a row another worker already replaced is not
    overwritten (the replaced row serves instead); and a refresh that
    outlives its deadline writes nothing and flags nothing."""
    monkeypatch.setenv("DALTON_XAI_OAUTH_CLIENT_ID", "test-grok-build-client")
    assert XAI_REFRESH_HARD_TIMEOUT_S == 120.0
    assert XAI_REFRESH_HARD_TIMEOUT_S < XAI_REFRESH_LOCK_TTL_MS / 1000.0
    # The registry entry carries the same pacing as the constants.
    xai_entry = oauth_providers_mod.SUBSCRIPTIONS["xai-oauth"]
    assert xai_entry.refresh_deadline_s == XAI_REFRESH_HARD_TIMEOUT_S
    assert xai_entry.lock_ttl_ms == XAI_REFRESH_LOCK_TTL_MS

    now = datetime.now(timezone.utc)
    expired_row: dict[str, Any] = {
        "provider": "xai-oauth",
        "access_token": "expired-tok",
        "refresh_token": "ref-tok-1",
        "account_id": "sub-1",
        "email": "original@dalton.fund",
        "plan_type": "supergrok",
        "expires_at": now - timedelta(minutes=5),
    }

    real_get_tokens = oauth_tokens_db.get_oauth_tokens
    mock_get_tokens = create_autospec(real_get_tokens, return_value=dict(expired_row))
    monkeypatch.setattr(oauth_health_mod, "get_oauth_tokens", mock_get_tokens)
    mock_replace = create_autospec(oauth_tokens_db.replace_refreshed_tokens, return_value=True)
    monkeypatch.setattr(oauth_health_mod, "replace_refreshed_tokens", mock_replace)

    lock_store: dict[str, str] = {}

    async def _acquire_lock(key: str, token: str, ttl_ms: int) -> bool:
        if key in lock_store:
            return False
        lock_store[key] = token
        return True

    async def _release_lock(key: str, token: str) -> None:
        if lock_store.get(key) == token:
            del lock_store[key]

    fake_cache = create_autospec(RedisCacheClient(), instance=True)
    fake_cache.acquire_lock.side_effect = _acquire_lock
    fake_cache.release_lock.side_effect = _release_lock
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client",
        create_autospec(lambda: fake_cache, return_value=fake_cache),
    )

    new_jwt_no_email = _make_jwt({"sub": "sub-1", "name": "Display Name Only"})

    async def _refresh_display_name_only(
        refresh_token: str,
        *,
        token_endpoint: str | None = None,
        user_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        return extract_token_record(
            {
                "access_token": new_jwt_no_email,
                "refresh_token": "ref-tok-2",
                "expires_in": 3600,
            },
            token_endpoint="https://auth.x.ai/oauth2/token",
        )

    mock_refresh = create_autospec(
        xai_oauth_mod.refresh_tokens, side_effect=_refresh_display_name_only
    )
    monkeypatch.setattr(xai_oauth_mod, "refresh_tokens", mock_refresh)

    # 1a) The answer names no identity: the stored email and SuperGrok plan ride
    #     the conditional save, and the save is guarded by the spent refresh token.
    res_saved = await oauth_health_mod.valid_token("user-exp", "xai-oauth")
    assert res_saved is not None
    assert res_saved.access_token == new_jwt_no_email
    assert res_saved.email == "original@dalton.fund"
    assert res_saved.plan_type == "supergrok"
    mock_replace.assert_awaited_once()
    replace_kwargs = mock_replace.call_args.kwargs
    assert replace_kwargs["spent_refresh_token"] == "ref-tok-1"
    assert replace_kwargs["refresh_token"] == "ref-tok-2"
    assert replace_kwargs["email"] == "original@dalton.fund"

    # 1b) The row moved on (a reconnect won the race): the conditional replace
    #     answers False, nothing is written over it, and the stored row serves.
    mock_replace.reset_mock()
    mock_replace.return_value = False
    other_worker_row = {
        **expired_row,
        "access_token": "other-worker-fresh-access",
        "refresh_token": "other-worker-refresh",
        "expires_at": now + timedelta(hours=1),
    }
    read_count = 0

    async def _expired_then_other_worker(user_id: str, provider: str) -> dict[str, Any] | None:
        nonlocal read_count
        read_count += 1
        # 1st read (pre-lock) and 2nd read (post-lock) see expired_row;
        # the read after the refused replace sees the row that moved on.
        if read_count <= 2:
            return dict(expired_row)
        return dict(other_worker_row)

    mock_get_tokens.side_effect = _expired_then_other_worker
    res_replaced = await oauth_health_mod.valid_token("user-exp", "xai-oauth")
    assert res_replaced is not None
    assert res_replaced.access_token == "other-worker-fresh-access"

    # 2) A refresh that outlives its deadline writes nothing and flags nothing:
    #    a timeout reads as transient, so no needs_reauth, no save.
    from dataclasses import replace as _dc_replace

    mock_replace.reset_mock()
    mock_get_tokens.side_effect = None
    mock_get_tokens.return_value = dict(expired_row)
    timed_out_entry = _dc_replace(xai_entry, refresh_deadline_s=0.02)
    monkeypatch.setattr(
        oauth_health_mod,
        "SUBSCRIPTIONS",
        {**oauth_providers_mod.SUBSCRIPTIONS, "xai-oauth": timed_out_entry},
    )

    async def _slow_refresh(
        refresh_token: str,
        *,
        token_endpoint: str | None = None,
        user_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return extract_token_record(
            {"access_token": new_jwt_no_email, "expires_in": 3600},
            token_endpoint="https://auth.x.ai/oauth2/token",
        )

    mock_refresh.side_effect = _slow_refresh
    with pytest.raises(oauth_health_mod.OAuthRefreshUnavailable):
        await oauth_health_mod.valid_token("user-exp", "xai-oauth")
    mock_replace.assert_not_awaited()



@pytest.mark.asyncio
async def test_chat_grok_non_streaming_ainvoke_aggregates_from_astream() -> None:
    client = ChatGrokOpenAI(
        model="grok-4.7",
        api_key="fake-supergrok-jwt",
        session_id="sess-nonstream",
    )
    client.streaming = False

    observed_kwargs: list[dict[str, Any]] = []

    async def _fake_astream(self_arg: Any, *args: Any, **kwargs: Any):
        observed_kwargs.append(dict(kwargs))
        yield ChatGenerationChunk(message=AIMessageChunk(content="Hello "))
        yield ChatGenerationChunk(message=AIMessageChunk(content="Grok!"))

    with patch.object(ChatGrokOpenAI, "_astream", autospec=True, side_effect=_fake_astream):
        result = await client.ainvoke([HumanMessage(content="greet")])

    assert isinstance(result, AIMessage)
    assert result.content == "Hello Grok!"
    assert len(observed_kwargs) == 1
    assert "run_manager" not in observed_kwargs[0]


def test_http_426_outdated_client_is_a_clean_failure():
    # cli-chat-proxy answers 426 to an outdated x-grok-client-version before doing any work.
    from src.dalton.llm.complete import _classify_exception

    class _Upgrade(Exception):
        status_code = 426

    exc = _Upgrade("Error code: 426 - {'error': 'Your Grok CLI version (1.0.4) is outdated.'}")
    assert _classify_exception(exc, provider="xai-oauth", sdk="grok") == ("failed", "http_426")
