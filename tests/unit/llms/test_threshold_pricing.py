"""Threshold (whole-request) long-context pricing semantics.

OpenAI GPT-5.6 bills long-context requests threshold-style — "Prompts with
>272K input tokens are priced at 2x input and 1.5x output for the full
request" — while the default ``input_tiers`` engine path is progressive
(each tier range at its own rate). ``input_pricing_mode: "input_dependent"``
opts a model into threshold input billing, per-tier ``cache_5m`` rates carry
the long-context cache-write price, and tier selection always uses the raw
prompt total so the cache-write subtraction can't demote a long request to
the short tier. These tests pin the engine semantics with synthetic pricing
dicts; manifest structure is asserted in test_model_pricing_inheritance.py.
"""

import pytest

from src.llms.pricing_utils import (
    calculate_total_cost,
    get_cache_creation_cost,
    get_input_cost,
)

# Synthetic rates (not manifest values): 5/10 input, 0.5/1.0 cached,
# 6.25/12.5 cache write, boundary at 272K.
THRESHOLD_PRICING = {
    "input_tiers": [
        {"max_tokens": 272_000, "rate": 5.0, "cached_input": 0.5, "cache_5m": 6.25},
        {"max_tokens": None, "rate": 10.0, "cached_input": 1.0, "cache_5m": 12.5},
    ],
    "cache_5m": 6.25,
    "output_tiers": [
        {"max_tokens": 272_000, "rate": 30.0},
        {"max_tokens": None, "rate": 45.0},
    ],
    "output_pricing_mode": "input_dependent",
    "input_pricing_mode": "input_dependent",
    "unit": "per_1m_tokens",
}


class TestThresholdInputMode:
    def test_short_context_bills_low_tier(self):
        regular, cached = get_input_cost(100_000, THRESHOLD_PRICING)
        assert regular == pytest.approx(100_000 / 1e6 * 5.0)
        assert cached == 0.0

    def test_long_context_bills_whole_request_at_high_tier(self):
        # Progressive would give 272K*5 + 28K*10 = $1.64; threshold gives $3.00.
        regular, _ = get_input_cost(300_000, THRESHOLD_PRICING)
        assert regular == pytest.approx(300_000 / 1e6 * 10.0)

    def test_boundary_stays_in_low_tier(self):
        regular, _ = get_input_cost(272_000, THRESHOLD_PRICING)
        assert regular == pytest.approx(272_000 / 1e6 * 5.0)

    def test_without_flag_stays_progressive(self):
        pricing = {k: v for k, v in THRESHOLD_PRICING.items() if k != "input_pricing_mode"}
        regular, _ = get_input_cost(300_000, pricing)
        assert regular == pytest.approx((272_000 * 5.0 + 28_000 * 10.0) / 1e6)

    def test_cached_reads_follow_the_selected_tier(self):
        _, cached = get_input_cost(300_000, THRESHOLD_PRICING, cached_tokens=250_000)
        assert cached == pytest.approx(250_000 / 1e6 * 1.0)


class TestTieredCacheWrites:
    def test_short_context_write_rate(self):
        cost_5m, _ = get_cache_creation_cost(
            50_000, 0, THRESHOLD_PRICING, tier_selector_tokens=100_000
        )
        assert cost_5m == pytest.approx(50_000 / 1e6 * 6.25)

    def test_long_context_write_rate(self):
        cost_5m, _ = get_cache_creation_cost(
            50_000, 0, THRESHOLD_PRICING, tier_selector_tokens=300_000
        )
        assert cost_5m == pytest.approx(50_000 / 1e6 * 12.5)

    def test_flat_fallback_without_selector(self):
        cost_5m, _ = get_cache_creation_cost(50_000, 0, THRESHOLD_PRICING)
        assert cost_5m == pytest.approx(50_000 / 1e6 * 6.25)

    def test_flat_fallback_when_tier_lacks_write_rate(self):
        # Anthropic-shaped pricing: per-tier cached_input but flat cache_5m.
        pricing = {
            "input_tiers": [
                {"max_tokens": 200_000, "rate": 3.0, "cached_input": 0.3},
                {"max_tokens": None, "rate": 6.0, "cached_input": 0.6},
            ],
            "cache_5m": 3.75,
        }
        cost_5m, _ = get_cache_creation_cost(
            50_000, 0, pricing, tier_selector_tokens=300_000
        )
        assert cost_5m == pytest.approx(50_000 / 1e6 * 3.75)


class TestRawPromptTierSelection:
    def test_write_subtraction_cannot_demote_the_tier(self):
        # 300K prompt, 290K of it written to cache: adjusted input is 10K but
        # the request is still long-context — every component must use the
        # high tier.
        result = calculate_total_cost(
            input_tokens=300_000,
            output_tokens=10_000,
            cache_5m_tokens=290_000,
            pricing=THRESHOLD_PRICING,
        )
        breakdown = result["breakdown"]
        assert breakdown["cache_5m_creation"]["cost"] == pytest.approx(290_000 / 1e6 * 12.5)
        assert breakdown["input"]["cost"] == pytest.approx(10_000 / 1e6 * 10.0)
        assert breakdown["output"]["cost"] == pytest.approx(10_000 / 1e6 * 45.0)

    def test_end_to_end_long_context_total(self):
        # The reviewer's canonical case: 300K in / 10K out, no cache.
        # Progressive engine gave $2.09; the correct threshold total is $3.45.
        result = calculate_total_cost(
            input_tokens=300_000, output_tokens=10_000, pricing=THRESHOLD_PRICING
        )
        assert result["total_cost"] == pytest.approx(3.45)

    def test_end_to_end_short_context_total(self):
        result = calculate_total_cost(
            input_tokens=100_000, output_tokens=10_000, pricing=THRESHOLD_PRICING
        )
        assert result["total_cost"] == pytest.approx(100_000 / 1e6 * 5.0 + 10_000 / 1e6 * 30.0)


# DALTON-HOOK(notional-pricing): pin Muse Ember 1.0's 200K threshold nominal pricing.
class TestMuseEmberNominalThresholdPricing:
    """Pin Meta Muse Ember 1.0's 200K whole-request threshold nominal pricing."""

    @pytest.fixture
    def ember_card(self):
        from src.dalton.llm.notional_pricing import resolve_notional_card

        card = resolve_notional_card("muse-ember-1.0-oauth")
        assert card is not None
        return card

    @pytest.mark.parametrize(
        "input_tokens,cached_tokens,output_tokens,expected_usd",
        [
            (199_999, 0, 1_000, 0.0204999),
            (200_000, 0, 1_000, 0.0205),
            (200_001, 0, 1_000, 0.0615003),
            (200_000, 150_000, 1_000, 0.0061),
            (200_001, 150_001, 1_000, 0.018300012),
            (200_000, 200_000, 0, 0.0008),
            (200_001, 200_001, 0, 0.002400012),
            (100, 0, 300_000, 0.15001),
        ],
    )
    def test_muse_ember_threshold_boundary_and_cached_cases(
        self,
        ember_card,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        expected_usd: float,
    ):
        from src.dalton.llm.notional_pricing import calculate_notional_cost

        total_cost, _ = calculate_notional_cost(
            ember_card,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )
        assert total_cost == pytest.approx(expected_usd, rel=1e-9, abs=1e-12)

        raw = calculate_total_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            pricing=ember_card,
        )
        assert raw["total_cost"] == pytest.approx(expected_usd, rel=1e-9, abs=1e-12)

    def test_two_150k_calls_vs_single_300k_call(self, ember_card):
        from src.dalton.llm.notional_pricing import calculate_notional_cost

        call_1, _ = calculate_notional_cost(
            ember_card, input_tokens=150_000, output_tokens=0, cached_tokens=0
        )
        call_2, _ = calculate_notional_cost(
            ember_card, input_tokens=150_000, output_tokens=0, cached_tokens=0
        )
        assert call_1 == pytest.approx(0.015, rel=1e-9)
        assert call_1 + call_2 == pytest.approx(0.03, rel=1e-9)

        single_300k, _ = calculate_notional_cost(
            ember_card, input_tokens=300_000, output_tokens=0, cached_tokens=0
        )
        assert single_300k == pytest.approx(0.09, rel=1e-9)

    def test_reasoning_tokens_in_output_counted_once(self, ember_card):
        """Responses API includes reasoning tokens inside output_tokens; they must be billed once."""
        from langchain_core.messages import AIMessage
        from src.dalton.llm.notional_pricing import calculate_notional_cost
        from src.llms.token_counter import extract_token_usage

        msg = AIMessage(
            content="result",
            usage_metadata={
                "input_tokens": 200_000,
                "output_tokens": 1_000,
                "total_tokens": 201_000,
                "output_token_details": {"reasoning": 600},
            },
        )
        usage = extract_token_usage(msg)
        assert usage["input_tokens"] == 200_000
        assert usage["output_tokens"] == 1_000
        assert usage.get("reasoning_tokens") == 600

        cost_usd, _ = calculate_notional_cost(
            ember_card,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cached_tokens=usage.get("cached_tokens", 0),
        )
        # 200k * 0.10/1M + 1k * 0.50/1M = 0.0205 (reasoning 600 is part of 1k output, not added again)
        assert cost_usd == pytest.approx(0.0205, rel=1e-9)

