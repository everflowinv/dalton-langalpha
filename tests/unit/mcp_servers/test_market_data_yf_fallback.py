"""Market-data MCP fallback behavior: yfinance ahead of FMP, and the 402 class.

Dalton's provider order (config.dalton.yaml market_data.providers) answers
free yfinance before FMP; an FMP 402 is the plan, not the request. These pin
both in the MCP servers' own chain (plugins/langalpha_market_data).
"""

from __future__ import annotations

from unittest.mock import create_autospec, patch

import pytest

from data_client.fmp import FMPClient, FMPRequestError
from mcp_servers._envelope import error_from_exception, error_from_upstream

_PRICE_MOD = "plugins.langalpha_market_data.price_data_mcp_server"
_FUND_MOD = "plugins.langalpha_market_data.fundamentals_mcp_server"

# Captured at import, before the suite-wide autouse stub replaces the module
# attribute: create_autospec needs the real function, not another mock.
import plugins.langalpha_market_data.price_data_mcp_server as _price_module  # noqa: E402

_REAL_YF_FETCH = _price_module._yf_fetch_history

# Epoch-ms rows in the shape `_yf_fetch_history` produces (normalize_bars input).
_YF_ROWS = [
    {"time": 1735862400000, "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000},
    {"time": 1735948800000, "open": 103.0, "high": 108.0, "low": 102.0, "close": 107.0, "volume": 1200},
]


def _fmp_client() -> FMPClient:
    return create_autospec(FMPClient, instance=True)


def _ginlix_miss(mod):
    return patch.object(mod._ginlix, "fetch_stock_data", new=create_autospec(
        mod._ginlix.fetch_stock_data, return_value=None
    ))


# ---------------------------------------------------------------------------
# get_stock_data: yfinance sits ahead of FMP
# ---------------------------------------------------------------------------


class TestGetStockDataOrder:
    @pytest.mark.asyncio
    async def test_yfinance_answers_before_fmp(self):
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        client = _fmp_client()
        client.get_stock_price.return_value = []
        with _ginlix_miss(mod), \
             patch.object(mod, "_yf_fetch_history", new=create_autospec(
                 _REAL_YF_FETCH, return_value=list(_YF_ROWS)
             )), \
             patch(f"{_PRICE_MOD}.get_fmp_client", return_value=client):
            result = await mod.get_stock_data("AAPL", interval="1day")

        assert result["source"] == "yfinance"
        # Ascending, oldest first, via the shared display-row conversion.
        assert result["data"][0]["date"] < result["data"][-1]["date"]
        assert result["data"][0]["close"] == 103.0
        client.get_stock_price.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unsupported_yfinance_interval_falls_through_to_fmp(self):
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        client = _fmp_client()
        client.get_intraday_chart.return_value = [
            {"date": "2025-01-02 10:00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
        ]
        with _ginlix_miss(mod), \
             patch(f"{_PRICE_MOD}.get_fmp_client", return_value=client):
            # 4hour: yfinance has no such bar, so the leg is skipped entirely.
            result = await mod.get_stock_data(
                "AAPL", interval="4hour", start_date="2025-01-01", end_date="2025-01-07"
            )

        assert result["source"] == "fmp"
        client.get_intraday_chart.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_yfinance_error_is_a_soft_miss_to_fmp(self):
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        client = _fmp_client()
        client.get_stock_price.return_value = [
            {"date": "2025-01-02", "open": 100, "high": 105, "low": 99, "close": 103, "volume": 1000},
        ]
        with _ginlix_miss(mod), \
             patch.object(mod, "_yf_fetch_history", new=create_autospec(
                 _REAL_YF_FETCH, side_effect=RuntimeError("yahoo down")
             )), \
             patch(f"{_PRICE_MOD}.get_fmp_client", return_value=client):
            result = await mod.get_stock_data("AAPL", interval="1day")

        assert result["source"] == "fmp"

    @pytest.mark.asyncio
    async def test_an_fmp_402_is_plan_not_covered(self):
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        client = _fmp_client()
        client.get_stock_price.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        with _ginlix_miss(mod), \
             patch.object(mod, "_yf_fetch_history", new=create_autospec(
                 _REAL_YF_FETCH, return_value=[]
             )), \
             patch(f"{_PRICE_MOD}.get_fmp_client", return_value=client):
            result = await mod.get_stock_data("DASH", interval="1day")

        assert result["error"] == "plan_not_covered"
        assert "换数据源" in result["detail"]
        assert "改参数重试无效" in result["detail"]


# ---------------------------------------------------------------------------
# get_financial_statements: 402 → yfinance with a basis note
# ---------------------------------------------------------------------------


class TestStatementsPlanFallback:
    @pytest.mark.asyncio
    async def test_a_402_is_answered_from_yfinance_with_a_note(self):
        import plugins.langalpha_market_data.fundamentals_mcp_server as mod

        client = _fmp_client()
        client.get_income_statement.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        canned = {"Total Revenue": {"2026-09-30": 1000, "2026-06-30": 900}}
        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod, "_yf_statements", new=create_autospec(
                 mod._yf_statements, return_value=canned
             )):
            result = await mod.get_financial_statements("3690.HK", statement_type="income")

        assert result["source"] == "yfinance"
        assert result["data"] == canned
        assert "口径可能与 FMP 不同" in result["note"]

    @pytest.mark.asyncio
    async def test_a_402_with_a_yfinance_miss_returns_the_plan_error(self):
        import plugins.langalpha_market_data.fundamentals_mcp_server as mod

        client = _fmp_client()
        client.get_income_statement.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod, "_yf_statements", new=create_autospec(
                 mod._yf_statements, return_value=None
             )):
            result = await mod.get_financial_statements("3690.HK", statement_type="income")

        assert result["error"] == "plan_not_covered"

    @pytest.mark.asyncio
    async def test_a_non_402_error_does_not_reach_for_yfinance(self):
        import plugins.langalpha_market_data.fundamentals_mcp_server as mod

        client = _fmp_client()
        client.get_income_statement.side_effect = FMPRequestError(
            "FMP API request failed (500)", status_code=500
        )
        yf_spy = create_autospec(mod._yf_statements, return_value={"x": {}})
        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod, "_yf_statements", new=yf_spy):
            result = await mod.get_financial_statements("AAPL", statement_type="income")

        assert result["error"] == "upstream_error"
        yf_spy.assert_not_called()


# ---------------------------------------------------------------------------
# The envelope's 402 mapping itself
# ---------------------------------------------------------------------------


class TestEnvelope402:
    def test_a_402_maps_to_plan_not_covered_with_advice(self):
        env = error_from_upstream("FMP API request failed (402)", status=402, symbol="DASH")
        assert env["error"] == "plan_not_covered"
        assert "FMP 套餐不覆盖该请求" in env["detail"]
        assert env["symbol"] == "DASH"

    def test_a_402_from_an_exception_keeps_the_original_detail(self):
        err = error_from_exception(
            FMPRequestError("FMP API request failed (402)", status_code=402),
            "statements failed",
            symbol="3690.HK",
        )
        assert err["error"] == "plan_not_covered"
        assert "FMP API request failed (402)" in err["detail"]

    @pytest.mark.parametrize(
        ("status", "code"),
        [(422, "invalid_argument"), (429, "rate_limited"), (500, "upstream_error")],
    )
    def test_other_statuses_keep_their_mapping(self, status, code):
        assert error_from_upstream("boom", status=status)["error"] == code
