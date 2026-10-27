"""Tests for LLM pricing utility functions.

Covers extract_base_model, calculate_tiered_cost, find_2d_pricing_rates,
get_input_cost, get_output_cost, calculate_total_cost, and related helpers
in src/llms/pricing_utils.py.
"""

import pytest

from src.llms.pricing_utils import (
    calculate_tiered_cost,
    calculate_total_cost,
    extract_base_model,
    find_2d_pricing_rates,
    get_cache_creation_cost,
    get_cache_storage_cost,
    get_input_cost,
    get_output_cost,
)


# ---------------------------------------------------------------------------
# extract_base_model
# ---------------------------------------------------------------------------


class TestExtractBaseModel:
    """Strip version suffixes from model names."""

    def test_openai_mmdd(self):
        assert extract_base_model("gpt-5-0905") == "gpt-5"

    def test_openai_yyyy_mm_dd(self):
        assert extract_base_model("gpt-5-2025-08-07") == "gpt-5"

    def test_claude_yyyymmdd(self):
        assert extract_base_model("claude-opus-4-1-20250805") == "claude-opus-4-1"

    def test_volcengine_yymmdd(self):
        assert extract_base_model("doubao-seed-1-6-250615") == "doubao-seed-1-6"

    def test_no_suffix(self):
        assert extract_base_model("minimax-m2") == "minimax-m2"

    def test_already_base(self):
        assert extract_base_model("gpt-5") == "gpt-5"

    def test_empty_string(self):
        assert extract_base_model("") == ""

    def test_model_with_dots(self):
        # Model names with dots should not be stripped
        assert extract_base_model("gpt-4.1-mini") == "gpt-4.1-mini"


# ---------------------------------------------------------------------------
# calculate_tiered_cost
# ---------------------------------------------------------------------------


class TestCalculateTieredCost:
    """Tiered pricing cost calculation."""

    def test_single_tier(self):
        tiers = [{"max_tokens": None, "rate": 1.0}]
        cost = calculate_tiered_cost(1_000_000, tiers)
        assert cost == pytest.approx(1.0)

    def test_two_tiers_within_first(self):
        tiers = [
            {"max_tokens": 32000, "rate": 0.80},
            {"max_tokens": None, "rate": 1.20},
        ]
        cost = calculate_tiered_cost(20000, tiers)
        assert cost == pytest.approx(20000 / 1_000_000 * 0.80)

    def test_two_tiers_spanning(self):
        tiers = [
            {"max_tokens": 32000, "rate": 0.80},
            {"max_tokens": 128000, "rate": 1.20},
            {"max_tokens": None, "rate": 2.40},
        ]
        # 50,000 tokens: first 32k at 0.80, next 18k at 1.20
        cost = calculate_tiered_cost(50000, tiers)
        expected = (32000 / 1_000_000 * 0.80) + (18000 / 1_000_000 * 1.20)
        assert cost == pytest.approx(expected)

    def test_zero_tokens(self):
        tiers = [{"max_tokens": None, "rate": 1.0}]
        assert calculate_tiered_cost(0, tiers) == 0.0

    def test_empty_tiers(self):
        assert calculate_tiered_cost(1000, []) == 0.0

    def test_negative_tokens(self):
        tiers = [{"max_tokens": None, "rate": 1.0}]
        assert calculate_tiered_cost(-100, tiers) == 0.0


# ---------------------------------------------------------------------------
# find_2d_pricing_rates
# ---------------------------------------------------------------------------


class TestFind2dPricingRates:
    """2D matrix pricing lookup."""

    MATRIX = [
        {"input_max": 32000, "output_max": 200, "input": 0.29, "output": 1.14, "cached_input": 0.057},
        {"input_max": 32000, "output_max": None, "input": 0.43, "output": 2.00, "cached_input": 0.086},
        {"input_max": None, "output_max": None, "input": 0.57, "output": 2.29, "cached_input": 0.11},
    ]

    def test_first_tier(self):
        rates = find_2d_pricing_rates(20000, 100, self.MATRIX)
        assert rates["input"] == 0.29
        assert rates["output"] == 1.14

    def test_second_tier(self):
        rates = find_2d_pricing_rates(20000, 500, self.MATRIX)
        assert rates["input"] == 0.43
        assert rates["output"] == 2.00

    def test_last_tier(self):
        rates = find_2d_pricing_rates(50000, 100, self.MATRIX)
        assert rates["input"] == 0.57
        assert rates["output"] == 2.29

    def test_empty_matrix(self):
        result = find_2d_pricing_rates(1000, 100, [])
        assert result is None


# ---------------------------------------------------------------------------
# get_input_cost
# ---------------------------------------------------------------------------


class TestGetInputCost:
    """Input cost calculation across pricing modes."""

    def test_flat_pricing(self):
        pricing = {"input": 2.50}
        regular, cached = get_input_cost(10000, pricing)
        assert regular == pytest.approx(10000 / 1_000_000 * 2.50)
        assert cached == 0.0

    def test_flat_with_cache(self):
        pricing = {"input": 2.50, "cached_input": 1.25}
        regular, cached = get_input_cost(10000, pricing, cached_tokens=3000)
        assert regular == pytest.approx(7000 / 1_000_000 * 2.50)
        assert cached == pytest.approx(3000 / 1_000_000 * 1.25)

    def test_2d_matrix_pricing(self):
        pricing = {
            "pricing_mode": "2d_matrix",
            "matrix": [
                {"input_max": None, "output_max": None, "input": 0.57, "output": 2.29, "cached_input": 0.11},
            ],
        }
        regular, cached = get_input_cost(10000, pricing, cached_tokens=0, output_tokens=100)
        assert regular == pytest.approx(10000 / 1_000_000 * 0.57)


# ---------------------------------------------------------------------------
# get_output_cost
# ---------------------------------------------------------------------------


class TestGetOutputCost:
    """Output cost calculation."""

    def test_flat_pricing(self):
        cost = get_output_cost(10000, {"output": 1.5})
        assert cost == pytest.approx(10000 / 1_000_000 * 1.5)

    def test_zero_tokens(self):
        assert get_output_cost(0, {"output": 1.5}) == 0.0

    def test_tiered_pricing(self):
        pricing = {
            "output_tiers": [
                {"max_tokens": 32000, "rate": 1.0},
                {"max_tokens": None, "rate": 2.0},
            ]
        }
        cost = get_output_cost(50000, pricing)
        expected = (32000 / 1_000_000 * 1.0) + (18000 / 1_000_000 * 2.0)
        assert cost == pytest.approx(expected)

    def test_input_dependent_pricing(self):
        pricing = {
            "output_pricing_mode": "input_dependent",
            "output_tiers": [
                {"max_tokens": 32000, "rate": 1.14},
                {"max_tokens": 128000, "rate": 1.71},
                {"max_tokens": None, "rate": 2.29},
            ],
        }
        # input_tokens=50000 falls in 32k-128k tier -> rate 1.71
        cost = get_output_cost(10000, pricing, input_tokens=50000)
        assert cost == pytest.approx(10000 / 1_000_000 * 1.71)

    def test_no_pricing_info(self):
        assert get_output_cost(10000, {}) == 0.0


# ---------------------------------------------------------------------------
# get_cache_storage_cost / get_cache_creation_cost
# ---------------------------------------------------------------------------


class TestCacheCosts:
    """Cache-related cost helpers."""

    def test_cache_storage(self):
        cost = get_cache_storage_cost(5000, {"cache_storage": 4.0})
        assert cost == pytest.approx(5000 / 1_000_000 * 4.0)

    def test_cache_storage_zero(self):
        assert get_cache_storage_cost(0, {"cache_storage": 4.0}) == 0.0

    def test_cache_creation(self):
        c5, c1 = get_cache_creation_cost(
            10000, 5000, {"cache_5m": 3.75, "cache_1h": 7.50}
        )
        assert c5 == pytest.approx(10000 / 1_000_000 * 3.75)
        assert c1 == pytest.approx(5000 / 1_000_000 * 7.50)

    def test_cache_creation_no_config(self):
        c5, c1 = get_cache_creation_cost(10000, 5000, {})
        assert c5 == 0.0
        assert c1 == 0.0


# ---------------------------------------------------------------------------
# calculate_total_cost
# ---------------------------------------------------------------------------


class TestCalculateTotalCost:
    """End-to-end cost calculation."""

    def test_no_pricing(self):
        result = calculate_total_cost(input_tokens=1000, output_tokens=500)
        assert result["total_cost"] == 0.0
        assert "error" in result

    def test_flat_pricing(self):
        pricing = {"input": 2.50, "output": 10.0}
        result = calculate_total_cost(
            input_tokens=10000,
            output_tokens=5000,
            pricing=pricing,
        )
        expected_input = 10000 / 1_000_000 * 2.50
        expected_output = 5000 / 1_000_000 * 10.0
        assert result["total_cost"] == pytest.approx(expected_input + expected_output)
        assert "input" in result["breakdown"]
        assert "output" in result["breakdown"]

    def test_with_cache(self):
        pricing = {"input": 2.50, "output": 10.0, "cached_input": 1.25}
        result = calculate_total_cost(
            input_tokens=10000,
            output_tokens=5000,
            cached_tokens=3000,
            pricing=pricing,
        )
        assert "cached_input" in result["breakdown"]
        assert result["breakdown"]["cached_input"]["tokens"] == 3000

    def test_cache_creation_subtracted_from_input(self):
        """Cache creation tokens must not be double-billed as regular input."""
        pricing = {"input": 5.0, "cached_input": 0.5, "cache_5m": 6.25, "output": 25.0}
        result = calculate_total_cost(
            input_tokens=28342,
            output_tokens=286,
            cached_tokens=20459,
            cache_5m_tokens=7882,
            pricing=pricing,
        )
        # adjusted_input = 28342 - 7882 = 20460
        # regular = 20460 - 20459 = 1 token at input rate
        assert result["breakdown"]["input"]["tokens"] == 1
        assert result["breakdown"]["input"]["cost"] == pytest.approx(1 / 1_000_000 * 5.0)
        assert result["breakdown"]["cached_input"]["tokens"] == 20459
        assert result["breakdown"]["cache_5m_creation"]["tokens"] == 7882

    def test_negative_adjusted_input_clamped(self):
        """Ensure cache_5m > input_tokens doesn't produce negative costs."""
        pricing = {"input": 5.0, "cache_5m": 6.25, "output": 25.0}
        result = calculate_total_cost(
            input_tokens=100,
            output_tokens=50,
            cache_5m_tokens=200,
            pricing=pricing,
        )
        # adjusted_input = max(0, 100 - 200) = 0 → no regular input cost
        assert "input" not in result["breakdown"]
        assert result["breakdown"]["cache_5m_creation"]["tokens"] == 200

    def test_no_cache_creation_pricing_keeps_tokens_in_input(self):
        """Providers without cache_5m/cache_1h pricing (e.g. DeepSeek) should bill
        cache creation tokens at the regular input rate, not subtract them."""
        pricing = {"input": 0.14, "output": 0.28, "cached_input": 0.028}
        result = calculate_total_cost(
            input_tokens=1400,  # langchain-anthropic normalized: 100 + 800 + 500
            output_tokens=50,
            cached_tokens=800,
            cache_5m_tokens=500,  # extracted from legacy cache_creation int
            pricing=pricing,
        )
        # No cache_5m pricing → 500 tokens stay in input, billed at input rate
        # regular = 1400 - 800 = 600 tokens at input rate
        assert result["breakdown"]["input"]["tokens"] == 600
        assert result["breakdown"]["input"]["cost"] == pytest.approx(600 / 1_000_000 * 0.14)
        assert result["breakdown"]["cached_input"]["tokens"] == 800
        assert "cache_5m_creation" not in result["breakdown"]

    def test_zero_tokens(self):
        pricing = {"input": 2.50, "output": 10.0}
        result = calculate_total_cost(pricing=pricing)
        assert result["total_cost"] == 0.0
        assert result["breakdown"] == {}

    # DALTON-HOOK(llm-overlay): subscription providers log DEBUG without WARNING and return pricing_type="subscription";
    # claude-oauth / codex-oauth are pay-per-token and still warn once per process on missing price;
    # cash models without provider still price normally; openrouter openai/gpt-oss-120b resolves and prices accurately.
    def test_subscription_providers_do_not_warn_and_return_subscription_pricing_type(self, caplog):
        import logging

        from src.llms.pricing_utils import find_model_pricing

        sub_cases = [
            ("gemini-4-argon-low", "antigravity-oauth"),
            ("gemini-3.8-flash-medium", "antigravity-oauth"),
            ("gemini-2.5-pro", "antigravity-oauth"),
            ("claude-opus-5-5-high", "antigravity-oauth"),
            ("gpt-oss-120b-medium", "antigravity-oauth"),
            ("muse-wonder-1.0", "muse-oauth"),
            ("muse-wonder-full-1.0", "muse-oauth"),
            ("muse-spark-1.3", "muse-oauth"),
            ("grok-4.7-build-fast", "xai-oauth"),
        ]
        with caplog.at_level(logging.DEBUG, logger="src.llms.pricing_utils"):
            for model_name, provider in sub_cases:
                card = find_model_pricing(model_name, provider=provider)
                assert card == {"pricing_type": "subscription"}

                cost_res = calculate_total_cost(
                    input_tokens=100_000,
                    output_tokens=20_000,
                    model_name=model_name,
                    provider=provider,
                )
                assert cost_res["total_cost"] == 0.0
                assert cost_res["pricing_type"] == "subscription"
                assert "pricing_missing" not in cost_res

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("antigravity-oauth" in m for m in debug_msgs)
        assert any("muse-oauth" in m for m in debug_msgs)
        assert any("xai-oauth" in m for m in debug_msgs)

    def test_non_subscription_oauth_providers_still_warn_once_and_record_zero(self, caplog):
        import logging

        from src.llms.pricing_utils import (
            _WARNED_MISSING_PRICING,
            find_model_pricing,
            is_subscription_provider,
        )

        for provider in ("claude-oauth", "codex-oauth"):
            assert is_subscription_provider(provider) is False
            missing_model = f"unpriced-{provider}-model-xyz"
            _WARNED_MISSING_PRICING.discard((missing_model, provider))
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="src.llms.pricing_utils"):
                res1 = calculate_total_cost(
                    input_tokens=5000,
                    output_tokens=1000,
                    model_name=missing_model,
                    provider=provider,
                )
                res2 = calculate_total_cost(
                    input_tokens=5000,
                    output_tokens=1000,
                    model_name=missing_model,
                    provider=provider,
                )
                assert find_model_pricing(missing_model, provider=provider) is None

            assert res1["total_cost"] == 0.0
            assert res1["pricing_missing"] is True
            assert "pricing_type" not in res1
            assert res2["pricing_missing"] is True
            matching_warnings = [
                r
                for r in caplog.records
                if r.levelno == logging.WARNING and missing_model in r.message
            ]
            assert len(matching_warnings) == 1

    def test_cash_model_without_provider_prices_normally_and_cash_card_not_overridden(self):
        from src.llms.pricing_utils import find_model_pricing

        # Calling without provider on a cash model must resolve the cash card and bill normally.
        card = find_model_pricing("claude-opus-5-5")
        assert card is not None
        assert card.get("pricing_type") != "subscription"
        assert card["input"] > 0

        res = calculate_total_cost(
            input_tokens=100_000,
            output_tokens=20_000,
            model_name="claude-opus-5-5",
        )
        assert res["total_cost"] > 0.0
        assert "pricing_type" not in res
        assert "pricing_missing" not in res

        # An already-resolved cash card must never be overridden by provider="antigravity-oauth".
        explicit_res = calculate_total_cost(
            input_tokens=100_000,
            output_tokens=20_000,
            pricing=card,
            model_name="claude-opus-5-5",
            provider="antigravity-oauth",
        )
        assert explicit_res["total_cost"] == pytest.approx(res["total_cost"])

    def test_tracking_layer_deduplicates_missing_pricing_warning_across_turns(self, caplog):
        import logging

        from src.llms.pricing_utils import _WARNED_MISSING_PRICING
        from src.utils.tracking.core import (
            _WARNED_TRACKING_MISSING_PRICING,
            calculate_cost_from_per_call_records,
        )

        missing_model = "unpriced-platform-model-xyz"
        _WARNED_MISSING_PRICING.discard((missing_model, "openrouter"))
        _WARNED_TRACKING_MISSING_PRICING.discard((missing_model, "openrouter"))
        records = [
            {
                "model_name": missing_model,
                "pricing_model_id": missing_model,
                "pricing_provider": "openrouter",
                "billing_type": "platform",
                "usage": {"input_tokens": 1000, "output_tokens": 500},
            }
        ]
        with caplog.at_level(logging.WARNING, logger="src.utils.tracking.core"):
            out1 = calculate_cost_from_per_call_records(records)
            out2 = calculate_cost_from_per_call_records(records)

        assert out1["total_cost"] == 0.0
        assert out2["total_cost"] == 0.0
        tracking_warnings = [
            r
            for r in caplog.records
            if r.name == "src.utils.tracking.core"
            and r.levelno == logging.WARNING
            and missing_model in r.message
        ]
        assert len(tracking_warnings) == 1

    def test_missing_cash_model_warns_once_per_model_provider_and_sets_pricing_missing(
        self, caplog
    ):
        import logging

        from src.llms.pricing_utils import _WARNED_MISSING_PRICING, find_model_pricing

        _WARNED_MISSING_PRICING.discard(("unpriced-cash-model-xyz", "openrouter"))
        with caplog.at_level(logging.WARNING, logger="src.llms.pricing_utils"):
            res1 = calculate_total_cost(
                input_tokens=5000,
                output_tokens=1000,
                model_name="unpriced-cash-model-xyz",
                provider="openrouter",
            )
            res2 = calculate_total_cost(
                input_tokens=5000,
                output_tokens=1000,
                model_name="unpriced-cash-model-xyz",
                provider="openrouter",
            )
            assert find_model_pricing("unpriced-cash-model-xyz", provider="openrouter") is None

        assert res1["total_cost"] == 0.0
        assert isinstance(res1["total_cost"], float)
        assert res1["pricing_missing"] is True
        assert res2["pricing_missing"] is True

        matching_warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "unpriced-cash-model-xyz" in r.message
        ]
        assert len(matching_warnings) == 1

    def test_openrouter_gpt_oss_120b_pricing_and_cost(self, caplog):
        import logging

        from src.llms.pricing_utils import find_model_pricing

        with caplog.at_level(logging.WARNING, logger="src.llms.pricing_utils"):
            pricing = find_model_pricing("openai/gpt-oss-120b", provider="openrouter")
            assert pricing is not None
            assert pricing["input"] == pytest.approx(0.15)
            assert pricing["output"] == pytest.approx(0.60)
            assert pricing["cached_input"] == pytest.approx(0.075)

            res = calculate_total_cost(
                input_tokens=1_000_000,
                output_tokens=500_000,
                cached_tokens=200_000,
                pricing=pricing,
            )
        # Regular input: 800k * $0.15/M = $0.12
        # Cached input:  200k * $0.075/M = $0.015
        # Output:        500k * $0.60/M = $0.30
        # Total:         $0.435
        assert res["total_cost"] == pytest.approx(0.435)
        assert "pricing_missing" not in res
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
