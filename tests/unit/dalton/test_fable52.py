"""Claude Fable 5.2 rides the existing Anthropic API and Claude OAuth providers.

The routes live only in the native overlay. These tests load the merged
catalogue and check the outgoing request, adaptive effort, prefix binding,
tool round-trip, and cash versus subscription accounting. Nothing here
calls the network: ``capture`` and ``Wire`` stop the client before a socket.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from src.dalton.llm.complete import compute_call_costs
from src.dalton.llm.notional_pricing import (
    build_notional_price_index,
    calculate_notional_cost,
    is_subscription_model,
    resolve_notional_card,
)
from src.dalton.llm.overlay import load_dalton_meta
from src.dalton.llm.role_policy import family_of, is_text_only
from src.llms.llm import LLM, create_llm
from src.llms.pricing_utils import calculate_total_cost, find_model_pricing
from src.llms.task_costs import task_cost
from src.utils.tracking.core import calculate_cost_from_per_call_records
from tests.unit.llms.test_request_lock import _request_of, capture
from tests.unit.ptc_agent.agent.prefix_stability import (
    BINDING_BETA,
    Wire,
    all_breaks,
    anthropic_model,
    reply,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
API = "claude-fable-5-2"
OAUTH = "claude-fable-5-2-oauth"
MODEL_ID = "claude-fable-5-2"
ROUTES = (API, OAUTH)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
API_BETA = (
    "thinking-binding-controls-2026-08-01,"
    "mid-conversation-system-clear-at-2026-08-21,"
    "compact-2026-09-04"
)
OAUTH_BETA = (
    "claude-code-20250219,oauth-2025-04-20,"
    "interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14,"
    + API_BETA
)
OFFICIAL_PRICING = {
    "input": 8.0,
    "cached_input": 0.2,
    "cache_5m": 10.0,
    "cache_1h": 16.0,
    "output": 40.0,
    "unit": "per_1m_tokens",
}
USAGE = {
    "input_tokens": 1_000_000,
    "output_tokens": 500_000,
    "cached_tokens": 200_000,
    "cache_5m_tokens": 100_000,
    "cache_1h_tokens": 50_000,
}
_SAMPLING = ("temperature", "top_p", "top_k")
_UNTOUCHED = (
    "src/llms/manifest/models.json",
    "src/llms/manifest/providers.json",
    "src/llms/manifest/task_costs.json",
    "tests/unit/llms/request_lock.json",
    "agent_config.yaml",
    "agent_config.dalton.yaml",
    "config.yaml",
    "src/dalton/dalton.yaml",
)


def _body_stays_adaptive(body: dict, effort: str) -> None:
    assert body["model"] == MODEL_ID
    assert body["max_tokens"] == 128000
    assert body["output_config"] == {"effort": effort}
    assert body["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
    for key in _SAMPLING:
        assert key not in body


def _not_forced(choice: object) -> None:
    kind = choice.get("type") if isinstance(choice, dict) else choice
    assert kind not in {"any", "tool"}


@tool
def lookup(symbol: str) -> str:
    """Look up a symbol."""
    return symbol


def test_merged_catalogue_registers_both_routes_without_touching_upstream():
    config = LLM.get_model_config()
    raw_models = json.loads((REPO_ROOT / "src/llms/manifest/models.json").read_text(encoding="utf-8"))
    raw_providers = json.loads((REPO_ROOT / "src/llms/manifest/providers.json").read_text(encoding="utf-8"))
    assert API not in raw_models
    assert OAUTH not in raw_models
    upstream_ids = [item["id"] for item in raw_providers["models"]["anthropic"]]
    assert MODEL_ID not in upstream_ids

    api = config.get_model_config(API)
    oauth = config.get_model_config(OAUTH)
    assert api is not None and oauth is not None
    for entry, provider in ((api, "anthropic"), (oauth, "claude-oauth")):
        assert entry["model_id"] == MODEL_ID
        assert entry["provider"] == provider
        assert entry["display_name"] == "Claude Fable 5.2"
        assert entry["visible"] is True
        assert entry["prompt_guidance"] == "lean"
        assert entry["speed"] == 3
        assert entry["intelligence"] == 5
        assert entry["context"] == 1_000_000
        assert entry["compaction_profile"] == "expansive"
        assert entry["input_modalities"] == ["text", "image", "pdf"]
        assert entry["parameters"]["max_tokens"] == 128000
        assert entry["parameters"]["thinking"]["type"] == "adaptive"
        assert entry["parameters"]["thinking"]["block_binding"] == {
            "prefix_mismatch_behavior": "drop_block"
        }
        assert entry["additional_betas"] == API_BETA.split(",")
        assert entry["reasoning"]["efforts"] == list(EFFORTS)
        assert entry["reasoning"]["default"] == "high"
        assert entry["reasoning"]["write"] == "parameters.output_config.effort"

    assert family_of(API) == "anthropic-claude"
    assert family_of(OAUTH) == "anthropic-claude"
    assert is_text_only(API) is False
    assert is_text_only(OAUTH) is False

    meta = load_dalton_meta()
    assert meta[API] == {"family": "anthropic-claude", "billing": "cash", "tool_calling": True}
    assert meta[OAUTH]["billing"] == "subscription"
    assert meta[OAUTH]["family"] == "anthropic-claude"
    assert meta[OAUTH]["tool_calling"] is True
    assert meta[OAUTH]["notional_ref"] == API

    cards = [card for card in config.manifest["models"]["anthropic"] if card.get("id") == MODEL_ID]
    assert len(cards) == 1
    assert cards[0]["pricing"] == OFFICIAL_PRICING
    assert cards[0]["context_window"] == 1_000_000
    assert cards[0]["max_output"] == 128000
    assert find_model_pricing(MODEL_ID, provider="anthropic") == OFFICIAL_PRICING
    assert find_model_pricing(MODEL_ID, provider="claude-oauth") == OFFICIAL_PRICING

    previous = find_model_pricing("claude-fable-5-1", provider="anthropic")
    assert previous["input"] == 10.0
    assert previous["cached_input"] == 0.25
    assert previous["cache_5m"] == 12.5
    assert previous["cache_1h"] == 20.0
    assert previous["output"] == 50.0


def test_shipped_defaults_and_protected_files_do_not_name_fable_52():
    dalton = (REPO_ROOT / "agent_config.dalton.yaml").read_text(encoding="utf-8")
    upstream = (REPO_ROOT / "agent_config.yaml").read_text(encoding="utf-8")
    assert 'name: "claude-opus-5-5-oauth"' in dalton
    assert 'name: "claude-opus-5-5"' in upstream
    for rel in _UNTOUCHED:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert MODEL_ID not in text, rel
    lock = json.loads((REPO_ROOT / "tests/unit/llms/request_lock.json").read_text(encoding="utf-8"))
    manifest = json.loads((REPO_ROOT / "src/llms/manifest/models.json").read_text(encoding="utf-8"))
    assert set(lock) == set(manifest)
    assert task_cost(API, EFFORTS) is None
    assert task_cost(OAUTH, EFFORTS) is None
    assert task_cost("claude-fable-5-1", EFFORTS) is not None


@pytest.mark.parametrize(
    ("route", "client_name", "billing", "pricing_provider", "beta"),
    [
        (API, "ChatAnthropic", "byok", "anthropic", API_BETA),
        (OAUTH, "ChatAnthropicOAuth", "oauth", "claude-oauth", OAUTH_BETA),
    ],
)
def test_default_request_is_adaptive_high(route, client_name, billing, pricing_provider, beta):
    pin = capture(route)
    assert "client_error" not in pin, pin
    assert pin["client"] == client_name
    assert pin["metadata"]["billing_type"] == billing
    assert pin["metadata"]["manifest_model"] == route
    assert pin["metadata"]["pricing_model_id"] == MODEL_ID
    assert pin["metadata"]["pricing_provider"] == pricing_provider
    assert pin["metadata"]["provider_route"] == pricing_provider
    assert pin["metadata"]["reasoning_effort"] == "high"

    request = pin["request"]
    assert request["method"] == "POST"
    assert request["url"] == "https://api.anthropic.com/v1/messages"
    assert request["headers"]["host"] == "api.anthropic.com"
    assert request["headers"]["anthropic-beta"] == beta
    assert BINDING_BETA in request["headers"]["anthropic-beta"].split(",")
    body = request["body"]
    _body_stays_adaptive(body, "high")
    assert body["stream"] is True
    assert "tool_choice" not in body
    assert any(item["name"] == "lock_probe" for item in body["tools"])
    if route == OAUTH:
        assert body["system"][0] == {
            "type": "text",
            "text": "You are Claude Code, Anthropic's official CLI for Claude.",
        }
        assert body["system"][1]["text"] == "You are a lock probe."
        assert request["headers"]["x-app"] == "cli"
    else:
        assert body["system"] == "You are a lock probe."


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("effort", EFFORTS)
def test_every_supported_effort_stays_adaptive(route, effort):
    request = _request_of(create_llm(route, api_key="sk-request-lock", reasoning_effort=effort))
    assert request is not None
    _body_stays_adaptive(request["body"], effort)
    _not_forced(request["body"].get("tool_choice"))
    assert BINDING_BETA in request["headers"]["anthropic-beta"].split(",")


@pytest.mark.parametrize("route", ROUTES)
def test_off_effort_clamps_to_low_and_thinking_stays_adaptive(route):
    request = _request_of(create_llm(route, api_key="sk-request-lock", reasoning_effort="none"))
    assert request is not None
    _body_stays_adaptive(request["body"], "low")


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_tool_roundtrip_sends_drop_block_and_the_binding_beta(monkeypatch, route):
    wire = Wire(
        [
            reply(1, tool=("lookup", {"symbol": "AAPL"}), model_name=MODEL_ID),
            reply(2, text="AAPL.", model_name=MODEL_ID),
        ]
    ).install(monkeypatch, stream=False)
    agent = create_agent(model=anthropic_model(route), tools=[lookup], system_prompt="Answer plainly.")
    await agent.ainvoke({"messages": [HumanMessage("Look up AAPL.")]})

    assert len(wire.bodies) == 2
    assert wire.transformations == [[], []]
    assert all_breaks(wire) == []
    for body, header in zip(wire.bodies, wire.headers, strict=True):
        _body_stays_adaptive(body, "high")
        _not_forced(body.get("tool_choice"))
        assert BINDING_BETA in (header or "").split(",")
        assert any(item.get("name") == "lookup" for item in body.get("tools") or [])
    blocks = [
        block
        for message in wire.bodies[1]["messages"]
        for block in (message.get("content") if isinstance(message.get("content"), list) else [])
        if isinstance(block, dict)
    ]
    assert any(block.get("type") == "tool_use" and block.get("name") == "lookup" for block in blocks)
    assert any(block.get("type") == "tool_result" for block in blocks)


def test_cash_bills_official_rates_and_oauth_cash_stays_zero():
    pricing = find_model_pricing(MODEL_ID, provider="anthropic")
    cash_bill = calculate_total_cost(
        input_tokens=USAGE["input_tokens"],
        output_tokens=USAGE["output_tokens"],
        cached_tokens=USAGE["cached_tokens"],
        cache_5m_tokens=USAGE["cache_5m_tokens"],
        cache_1h_tokens=USAGE["cache_1h_tokens"],
        pricing=pricing,
    )
    assert cash_bill["breakdown"]["input"]["cost"] == pytest.approx(5.2)
    assert cash_bill["breakdown"]["cached_input"]["cost"] == pytest.approx(0.04)
    assert cash_bill["breakdown"]["cache_5m_creation"]["cost"] == pytest.approx(1.0)
    assert cash_bill["breakdown"]["cache_1h_creation"]["cost"] == pytest.approx(0.8)
    assert cash_bill["breakdown"]["output"]["cost"] == pytest.approx(20.0)
    assert cash_bill["total_cost"] == pytest.approx(27.04)

    for billing in ("platform", "byok"):
        cash, notional = compute_call_costs(API, billing, USAGE)
        assert float(cash) == pytest.approx(27.04)
        assert notional == Decimal("0")
        assert is_subscription_model(API, billing) is False

    platform = calculate_cost_from_per_call_records(
        [{"model_name": API, "usage": dict(USAGE), "billing_type": "platform"}]
    )
    assert platform["platform_cost"] == pytest.approx(27.04)
    assert platform["total_cost"] == pytest.approx(27.04)
    assert platform["notional_cost"] == 0.0

    byok = calculate_cost_from_per_call_records(
        [{"model_name": API, "usage": dict(USAGE), "billing_type": "byok"}]
    )
    assert byok["platform_cost"] == 0.0
    assert byok["total_cost"] == pytest.approx(27.04)

    card = resolve_notional_card(OAUTH, index=build_notional_price_index())
    assert card == {"input": 8.0, "output": 40.0, "cached_input": 0.2}
    nominal, breakdown = calculate_notional_cost(
        card,
        input_tokens=USAGE["input_tokens"],
        output_tokens=USAGE["output_tokens"],
        cached_tokens=USAGE["cached_tokens"],
        cache_5m_tokens=USAGE["cache_5m_tokens"],
        cache_1h_tokens=USAGE["cache_1h_tokens"],
    )
    # Flat nominal cards keep input, output, and cache read. Cache-write rates
    # are dropped by the existing normalizer, the same way Fable 5.1's are.
    assert "cache_5m_creation" not in breakdown
    assert "cache_1h_creation" not in breakdown
    assert nominal == pytest.approx(26.44)

    assert is_subscription_model(OAUTH, "oauth") is True
    cash, notional = compute_call_costs(OAUTH, "oauth", USAGE)
    assert cash == Decimal("0")
    assert float(notional) == pytest.approx(26.44)

    oauth_bill = calculate_cost_from_per_call_records(
        [{"model_name": OAUTH, "usage": dict(USAGE), "billing_type": "oauth"}]
    )
    assert oauth_bill["platform_cost"] == 0.0
    assert oauth_bill["notional_cost"] == pytest.approx(26.44)
