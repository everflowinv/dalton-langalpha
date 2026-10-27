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
import plugins.langalpha_market_data.fundamentals_mcp_server as _fund_module  # noqa: E402
import plugins.langalpha_market_data.price_data_mcp_server as _price_module  # noqa: E402

_REAL_YF_FETCH = _price_module._yf_fetch_history
_REAL_YF_STATEMENTS = _fund_module._yf_statements

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

    @pytest.mark.asyncio
    async def test_intraday_window_preserves_exchange_time_and_filters_rows(self):
        import pandas as pd
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        # 09:30, 13:00, 14:30, 15:30 America/New_York on 2026-10-26 (EDT, UTC-4)
        idx = pd.DatetimeIndex(
            [
                "2026-10-26 09:30:00-04:00",
                "2026-10-26 13:00:00-04:00",
                "2026-10-26 14:30:00-04:00",
                "2026-10-26 15:30:00-04:00",
            ]
        )
        df = pd.DataFrame(
            {
                "Open": [100.0, 101.0, 102.0, 103.0],
                "High": [101.0, 102.0, 103.0, 104.0],
                "Low": [99.0, 100.0, 101.0, 102.0],
                "Close": [100.5, 101.5, 102.5, 103.5],
                "Volume": [100, 200, 300, 400],
            },
            index=idx,
        )
        ticker_inst = create_autospec(mod._yf.Ticker, instance=True)
        ticker_inst.history.return_value = df
        ticker_cls = create_autospec(mod._yf.Ticker, return_value=ticker_inst)
        client = _fmp_client()

        with _ginlix_miss(mod), \
             patch.object(mod, "_yf_fetch_history", new=_REAL_YF_FETCH), \
             patch.object(mod._yf, "Ticker", new=ticker_cls), \
             patch(f"{_PRICE_MOD}.get_fmp_client", return_value=client):
            result = await mod.get_stock_data(
                "AAPL",
                interval="5min",
                start_date="2026-10-26 13:00",
                end_date="2026-10-26 15:00",
            )

        assert result["source"] == "yfinance"
        assert [r["date"] for r in result["data"]] == [
            "2026-10-26 13:00:00",
            "2026-10-26 14:30:00",
        ]
        client.get_intraday_chart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_partial_yfinance_coverage_falls_through_to_fmp_when_fmp_succeeds(self):
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        # Request starts 2026-10-01, but yfinance only has bars from 2026-10-20
        # (1792503000000 = 2026-10-20 09:30:00 EDT).
        partial_rows = [
            {"time": 1792503000000, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 500}
        ]
        client = _fmp_client()
        client.get_intraday_chart.return_value = [
            {"date": "2026-10-01 09:30:00", "open": 95.0, "high": 96.0, "low": 94.0, "close": 95.5, "volume": 800},
            {"date": "2026-10-20 09:30:00", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 500},
        ]
        with _ginlix_miss(mod), \
             patch.object(mod, "_yf_fetch_history", new=create_autospec(
                 _REAL_YF_FETCH, return_value=partial_rows
             )), \
             patch(f"{_PRICE_MOD}.get_fmp_client", return_value=client):
            result = await mod.get_stock_data(
                "AAPL",
                interval="1min",
                start_date="2026-10-01 09:30",
                end_date="2026-10-26 16:00",
            )

        assert result["source"] == "fmp"
        assert len(result["data"]) == 2
        client.get_intraday_chart.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_partial_yfinance_coverage_returns_partial_with_note_when_fmp_fails(self):
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        partial_rows = [
            {"time": 1792503000000, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 500},
            {"time": 1792506600000, "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5, "volume": 600},
        ]
        client = _fmp_client()
        client.get_intraday_chart.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        with _ginlix_miss(mod), \
             patch.object(mod, "_yf_fetch_history", new=create_autospec(
                 _REAL_YF_FETCH, return_value=partial_rows
             )), \
             patch(f"{_PRICE_MOD}.get_fmp_client", return_value=client):
            result = await mod.get_stock_data(
                "AAPL",
                interval="1min",
                start_date="2026-10-01 09:30",
                end_date="2026-10-26 16:00",
            )

        assert result["source"] == "yfinance"
        assert len(result["data"]) == 2
        assert "2026-10-20 09:30:00" in result["note"]
        assert "2026-10-20 10:30:00" in result["note"]


# ---------------------------------------------------------------------------
# get_financial_statements: 402 → yfinance with FMP-shaped period records
# ---------------------------------------------------------------------------


class TestStatementsPlanFallback:
    @pytest.mark.asyncio
    async def test_a_402_is_answered_from_yfinance_with_fmp_period_records(self):
        import pandas as pd
        import plugins.langalpha_market_data.fundamentals_mcp_server as mod

        client = _fmp_client()
        client.get_income_statement.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        df = pd.DataFrame(
            {
                pd.Timestamp("2024-12-31"): [900.0, 180.0, 95.0, float("nan"), 111.0],
                pd.Timestamp("2025-12-31"): [1000.0, 220.0, 100.0, float("nan"), 222.0],
                pd.Timestamp("2023-12-31"): [800.0, 150.0, 90.0, 10.0, 333.0],
            },
            index=pd.Index(
                [
                    "Total Revenue",
                    "Net Income",
                    "Diluted Average Shares",
                    "Research And Development",
                    "Unmapped Custom Metric",
                ]
            ),
        )
        ticker_inst = create_autospec(mod._yf.Ticker, instance=True)
        ticker_inst.income_stmt = df
        ticker_cls = create_autospec(mod._yf.Ticker, return_value=ticker_inst)

        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod._yf, "Ticker", new=ticker_cls):
            result = await mod.get_financial_statements(
                "3690.HK", statement_type="income", period="annual", limit=2
            )

        assert result["source"] == "yfinance"
        assert result["count"] == 2
        assert len(result["data"]) == 2
        # Sorted newest-first, limit=2 applied
        assert [r["date"] for r in result["data"]] == ["2025-12-31", "2024-12-31"]
        first = result["data"][0]
        assert first["period"] == "FY"
        assert first["calendarYear"] == "2025"
        assert first["revenue"] == 1000.0
        assert first["netIncome"] == 220.0
        assert first["weightedAverageShsOutDil"] == 100.0
        # Missing/NaN Yahoo values and FMP-only fields are None (never "N/A")
        assert first["researchAndDevelopmentExpenses"] is None
        assert first["grossProfitRatio"] is None
        assert "N/A" not in first.values()
        # Unmapped Yahoo metrics are not injected into period records
        assert "Unmapped Custom Metric" not in first
        assert "口径" in result["note"]

    @pytest.mark.asyncio
    async def test_statement_type_all_returns_three_lists_and_total_count(self):
        import pandas as pd
        import plugins.langalpha_market_data.fundamentals_mcp_server as mod

        client = _fmp_client()
        client.get_income_statement.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        inc_df = pd.DataFrame(
            {"2025-09-30": [500.0, 50.0], "2025-06-30": [450.0, 45.0]},
            index=pd.Index(["Total Revenue", "Diluted Average Shares"]),
        )
        bal_df = pd.DataFrame(
            {"2025-09-30": [2000.0, 300.0], "2025-06-30": [1900.0, 280.0]},
            index=pd.Index(["Total Assets", "Total Debt"]),
        )
        cf_df = pd.DataFrame(
            {"2025-09-30": [120.0, -30.0], "2025-06-30": [110.0, -25.0]},
            index=pd.Index(["Operating Cash Flow", "Capital Expenditure"]),
        )
        ticker_inst = create_autospec(mod._yf.Ticker, instance=True)
        ticker_inst.quarterly_income_stmt = inc_df
        ticker_inst.quarterly_balance_sheet = bal_df
        ticker_inst.quarterly_cashflow = cf_df
        ticker_cls = create_autospec(mod._yf.Ticker, return_value=ticker_inst)

        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod._yf, "Ticker", new=ticker_cls):
            result = await mod.get_financial_statements(
                "3690.HK", statement_type="all", period="quarter", limit=1
            )

        assert result["source"] == "yfinance"
        assert result["count"] == 3
        data = result["data"]
        assert isinstance(data["income_statement"], list) and len(data["income_statement"]) == 1
        assert isinstance(data["balance_sheet"], list) and len(data["balance_sheet"]) == 1
        assert isinstance(data["cash_flow"], list) and len(data["cash_flow"]) == 1
        assert data["income_statement"][0]["period"] == "Q3"
        assert data["income_statement"][0]["weightedAverageShsOutDil"] == 50.0
        assert data["balance_sheet"][0]["totalAssets"] == 2000.0
        assert data["cash_flow"][0]["operatingCashFlow"] == 120.0

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
