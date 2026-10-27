"""Market-data MCP fallback behavior: yfinance ahead of FMP, and the 402 class.

Dalton's provider order (config.dalton.yaml market_data.providers) answers
free yfinance before FMP; an FMP 402 is the plan, not the request. These pin
both in the MCP servers' own chain (plugins/langalpha_market_data).
"""

from __future__ import annotations

from datetime import datetime, timezone
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


def _epoch(year: int, month: int, day: int) -> int:
    """Yahoo's date fields are epoch seconds (UTC midnight of the date)."""
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())

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
    async def test_hk_hourly_lunch_bar_reanchors_before_the_window_filter(self):
        # HKEX breaks 12:00–13:00; Yahoo's hourly grid puts a 12:30 bar inside
        # it that carries the reopen's trades and re-anchors to 13:00. The
        # re-anchor must run before the request window filter, or a request
        # starting 13:00 drops that bar (12:30 < 13:00) and the session starts
        # at 14:00.
        import pandas as pd
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        idx = pd.DatetimeIndex(
            [
                "2026-10-26 10:30:00+08:00",
                "2026-10-26 11:30:00+08:00",
                "2026-10-26 12:30:00+08:00",
                "2026-10-26 14:00:00+08:00",
                "2026-10-26 15:00:00+08:00",
            ]
        )
        df = pd.DataFrame(
            {
                "Open": [400.0, 401.0, 402.0, 403.0, 404.0],
                "High": [401.0, 402.0, 403.0, 404.0, 405.0],
                "Low": [399.0, 400.0, 401.0, 402.0, 403.0],
                "Close": [400.5, 401.5, 402.5, 403.5, 404.5],
                "Volume": [100, 200, 300, 400, 500],
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
                "0700.HK",
                interval="1hour",
                start_date="2026-10-26 13:00",
                end_date="2026-10-26 15:00",
            )

        assert result["source"] == "yfinance"
        assert [r["date"] for r in result["data"]] == [
            "2026-10-26 13:00:00",
            "2026-10-26 14:00:00",
            "2026-10-26 15:00:00",
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
        # FMP errored (the 402), so the fallback itself is what was unavailable.
        assert "FMP fallback unavailable" in result["note"]
        assert "FMP 无该区间数据" not in result["note"]

    @pytest.mark.asyncio
    async def test_a_200_with_no_fmp_rows_says_fmp_has_no_data_for_the_range(self):
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        partial_rows = [
            {"time": 1792503000000, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 500}
        ]
        client = _fmp_client()
        client.get_intraday_chart.return_value = []
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
        # A 200 with no rows is FMP having no data in the window, not a broken
        # fallback — the note must not claim otherwise.
        assert "FMP 无该区间数据" in result["note"]
        assert "FMP fallback unavailable" not in result["note"]


# ---------------------------------------------------------------------------
# Start-coverage judgment and midday-break bars
# ---------------------------------------------------------------------------


class TestYfCoversStart:
    def test_a_weekend_start_is_covered_by_mondays_daily_bar(self):
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        # 2026-10-24 is a Saturday; the first daily bar is Monday the 26th.
        rows = [{"date": "2026-10-26"}]
        assert mod._yf_covers_start(rows, "2026-10-24", intraday=False, display="AAPL")

    def test_an_intraday_start_inside_the_1330_gap_is_still_covered(self):
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        # 13:01 sits between 5-minute bars; the 13:05 bar is coverage, not a gap.
        rows = [{"date": "2026-10-26 13:05:00"}]
        assert mod._yf_covers_start(
            rows, "2026-10-26 13:01", intraday=True, display="AAPL", interval="5min"
        )

    def test_a_weekend_intraday_start_gets_the_next_session_open(self):
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        rows = [{"date": "2026-10-26 09:30:00"}]
        assert mod._yf_covers_start(
            rows, "2026-10-24 10:00", intraday=True, display="AAPL", interval="5min"
        )

    def test_an_after_hours_friday_start_is_covered_by_mondays_open_bar(self):
        # 2026-10-23 is a Friday; 20:00 sits past the 16:00 close, so the
        # start belongs to Monday's open. The day-level check alone read
        # Monday's first bar as late (Monday > Friday) and burned FMP quota
        # re-asking a served window.
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        rows = [{"date": "2026-10-26 09:30:00"}]
        assert mod._yf_covers_start(
            rows, "2026-10-23 20:00", intraday=True, display="AAPL", interval="5min"
        )

    def test_a_post_close_start_is_covered_by_the_next_days_open_bar(self):
        # 17:00 is past the 16:00 close of 2026-10-26 (a Monday); the next
        # session's open bar on the 27th covers it.
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        rows = [{"date": "2026-10-27 09:30:00"}]
        assert mod._yf_covers_start(
            rows, "2026-10-26 17:00", intraday=True, display="AAPL", interval="5min"
        )

    def test_a_start_after_an_early_close_is_covered_by_the_next_session(self):
        # 2026-11-27 (the Friday after Thanksgiving) closes early at 13:00;
        # a 14:00 start that day is past the close, so Monday 11-30's open
        # bar covers it.
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        rows = [{"date": "2026-11-30 09:30:00"}]
        assert mod._yf_covers_start(
            rows, "2026-11-27 14:00", intraday=True, display="AAPL", interval="5min"
        )

    def test_an_after_hours_friday_start_with_a_tuesday_first_bar_is_a_gap(self):
        # The deadline for a Friday-20:00 start is Monday's open plus one
        # interval, so a first bar on Tuesday is a real gap, not slack.
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        rows = [{"date": "2026-10-27 09:30:00"}]
        assert not mod._yf_covers_start(
            rows, "2026-10-23 20:00", intraday=True, display="AAPL", interval="5min"
        )

    def test_an_afternoon_first_bar_does_not_cover_a_morning_start(self):
        # Date-only comparison called a day whose first bar is 14:00 covered
        # for a 09:30 request; the bar clock must matter inside the session.
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        rows = [{"date": "2026-10-26 14:00:00"}]
        assert not mod._yf_covers_start(
            rows, "2026-10-26 09:30", intraday=True, display="AAPL", interval="5min"
        )
        # The uncovered verdict routes to FMP first, then the partial-yfinance
        # answer with the actual covered range named — that path is exercised
        # end-to-end in TestGetStockDataOrder above.

    def test_a_capped_one_minute_history_of_a_two_month_request_is_not_covered(self):
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        # Yahoo caps 1m history near 7 days; a request from 2026-08-25 gets
        # bars only from 2026-10-20 — a real gap.
        rows = [{"date": "2026-10-20 09:30:00"}]
        assert not mod._yf_covers_start(
            rows, "2026-08-25 09:30", intraday=True, display="AAPL", interval="1min"
        )

    def test_a_daily_gap_beyond_four_days_is_not_covered(self):
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        rows = [{"date": "2026-10-20"}]
        assert not mod._yf_covers_start(rows, "2026-10-01", intraday=False, display="AAPL")


class TestMiddayBreakBars:
    @staticmethod
    def _ms(year, month, day, hour, minute):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return int(datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)

    def test_bars_inside_the_a_share_break_are_dropped(self):
        # The XSHG break on 2026-10-26 runs 11:30–13:00 Asia/Shanghai.
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        rows = [
            {"time": self._ms(2026, 10, 26, 9, 30)},
            {"time": self._ms(2026, 10, 26, 11, 25)},  # 5min bar ending exactly at the break
            {"time": self._ms(2026, 10, 26, 11, 35)},  # wholly inside the break
            {"time": self._ms(2026, 10, 26, 13, 0)},  # the reopen
        ]
        placed = mod._drop_midday_break_bars(rows, "600519.SH", "5min")
        assert [b["time"] for b in placed] == [
            self._ms(2026, 10, 26, 9, 30),
            self._ms(2026, 10, 26, 11, 25),
            self._ms(2026, 10, 26, 13, 0),
        ]

    def test_an_hourly_bar_straddling_the_break_is_reanchored_to_the_reopen(self):
        # Yahoo's hourly grid starts a 12:30 bar inside the break; it ends at
        # 13:30, past the reopen, so it carries the reopen's trades and moves
        # to 13:00 rather than being dropped. A fully-inside 11:30 bar goes.
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        rows = [
            {"time": self._ms(2026, 10, 26, 11, 30)},
            {"time": self._ms(2026, 10, 26, 12, 30)},
        ]
        placed = mod._drop_midday_break_bars(rows, "600519.SH", "1hour")
        assert [b["time"] for b in placed] == [self._ms(2026, 10, 26, 13, 0)]

    def test_a_venue_without_a_break_keeps_every_bar(self):
        import plugins.langalpha_market_data.price_data_mcp_server as mod

        rows = [{"time": self._ms(2026, 10, 26, 12, 0)}]
        placed = mod._drop_midday_break_bars(rows, "AAPL", "5min")
        assert placed == rows


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
        # A Dec-FYE issuer reporting in HKD: annual columns stay FY/<year>.
        ticker_inst.info = {
            "financialCurrency": "HKD",
            "lastFiscalYearEnd": _epoch(2025, 12, 31),
        }
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
        assert first["fiscalYear"] == "2025"
        assert first["reportedCurrency"] == "HKD"
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
        assert "period / fiscalYear 未知" not in result["note"]

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
        ticker_inst.info = {
            "financialCurrency": "HKD",
            "lastFiscalYearEnd": _epoch(2025, 12, 31),
        }
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
                 mod._yf_statements, return_value=(None, False)
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
        yf_spy = create_autospec(mod._yf_statements, return_value=({"x": {}}, False))
        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod, "_yf_statements", new=yf_spy):
            result = await mod.get_financial_statements("AAPL", statement_type="income")

        assert result["error"] == "upstream_error"
        yf_spy.assert_not_called()


# ---------------------------------------------------------------------------
# yfinance statements: reporting currency, fiscal labels, computed fields
# ---------------------------------------------------------------------------


class TestStatementsFiscalShape:
    @pytest.mark.asyncio
    async def test_reported_currency_is_the_financial_currency_not_the_listing_one(self):
        # A mainland issuer listed in HKD that reports in CNY: the quote
        # currency must not pose as the reporting currency.
        import pandas as pd
        import plugins.langalpha_market_data.fundamentals_mcp_server as mod

        client = _fmp_client()
        client.get_income_statement.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        df = pd.DataFrame(
            {"2025-06-30": [500.0]},
            index=pd.Index(["Total Revenue"]),
        )
        ticker_inst = create_autospec(mod._yf.Ticker, instance=True)
        ticker_inst.quarterly_income_stmt = df
        ticker_inst.info = {
            "financialCurrency": "CNY",
            "lastFiscalYearEnd": _epoch(2025, 6, 30),
        }
        ticker_cls = create_autospec(mod._yf.Ticker, return_value=ticker_inst)

        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod._yf, "Ticker", new=ticker_cls):
            result = await mod.get_financial_statements(
                "3690.HK", statement_type="income", period="quarter", limit=1
            )

        assert result["source"] == "yfinance"
        assert result["data"][0]["reportedCurrency"] == "CNY"

    @pytest.mark.asyncio
    async def test_quarter_labels_follow_the_issuer_fiscal_calendar(self):
        # MSFT's fiscal year ends June 30, so a quarter ending 2025-09-30 is
        # Q1 of FY2026 — not Q3 of 2025 as a calendar split would say.
        import pandas as pd
        import plugins.langalpha_market_data.fundamentals_mcp_server as mod

        client = _fmp_client()
        client.get_income_statement.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        df = pd.DataFrame(
            {"2025-09-30": [500.0]},
            index=pd.Index(["Total Revenue"]),
        )
        ticker_inst = create_autospec(mod._yf.Ticker, instance=True)
        ticker_inst.quarterly_income_stmt = df
        ticker_inst.info = {
            "financialCurrency": "USD",
            "lastFiscalYearEnd": _epoch(2026, 6, 30),
        }
        ticker_cls = create_autospec(mod._yf.Ticker, return_value=ticker_inst)

        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod._yf, "Ticker", new=ticker_cls):
            result = await mod.get_financial_statements(
                "MSFT", statement_type="income", period="quarter", limit=1
            )

        first = result["data"][0]
        assert first["period"] == "Q1"
        assert first["fiscalYear"] == "2026"
        assert first["calendarYear"] == "2025"
        assert "period / fiscalYear 未知" not in result["note"]

    @pytest.mark.asyncio
    async def test_a_week_based_fiscal_calendar_gets_no_period_labels(self):
        # A 52/53-week retailer: the fiscal year ends 2026-01-30 and quarters
        # end 2026-05-01 / 2026-07-31 — off the calendar month grid, so month
        # arithmetic read Q1 as Q2 (and 05-01/07-31 as the same Q2). No label
        # is published for any column, month-end or not, and the note names
        # the calendar as the reason.
        import pandas as pd
        import plugins.langalpha_market_data.fundamentals_mcp_server as mod

        client = _fmp_client()
        client.get_income_statement.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        df = pd.DataFrame(
            {
                "2026-07-31": [500.0],
                "2026-05-01": [400.0],
                "2026-01-30": [300.0],
            },
            index=pd.Index(["Total Revenue"]),
        )
        ticker_inst = create_autospec(mod._yf.Ticker, instance=True)
        ticker_inst.quarterly_income_stmt = df
        ticker_inst.info = {
            "financialCurrency": "USD",
            "lastFiscalYearEnd": _epoch(2026, 1, 30),
        }
        ticker_cls = create_autospec(mod._yf.Ticker, return_value=ticker_inst)

        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod._yf, "Ticker", new=ticker_cls):
            result = await mod.get_financial_statements(
                "WMT", statement_type="income", period="quarter", limit=3
            )

        assert result["source"] == "yfinance"
        assert [r["date"] for r in result["data"]] == [
            "2026-07-31", "2026-05-01", "2026-01-30",
        ]
        # Every column is unlabeled — even the 07-31 month-end one.
        assert all(r["period"] is None and r["fiscalYear"] is None for r in result["data"])
        assert all(r["calendarYear"] == r["date"][:4] for r in result["data"])
        assert "周制" in result["note"]
        assert "period / fiscalYear 未知" in result["note"]

    @pytest.mark.asyncio
    async def test_a_month_end_year_end_with_off_month_end_quarters_is_week_based(self):
        # lastFiscalYearEnd 2026-01-31 is itself a month-end, but the quarter
        # ending 2026-05-01 is not: a 52/53-week calendar whose year end
        # happened to land on a month-end. The verdict must fall before
        # serialization, so even the month-end column (2026-07-31, which
        # month arithmetic would label Q2 FY2027) carries no label.
        import pandas as pd
        import plugins.langalpha_market_data.fundamentals_mcp_server as mod

        client = _fmp_client()
        client.get_income_statement.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        df = pd.DataFrame(
            {
                "2026-07-31": [500.0],
                "2026-05-01": [400.0],
                "2026-01-31": [300.0],
            },
            index=pd.Index(["Total Revenue"]),
        )
        ticker_inst = create_autospec(mod._yf.Ticker, instance=True)
        ticker_inst.quarterly_income_stmt = df
        ticker_inst.info = {
            "financialCurrency": "USD",
            "lastFiscalYearEnd": _epoch(2026, 1, 31),
        }
        ticker_cls = create_autospec(mod._yf.Ticker, return_value=ticker_inst)

        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod._yf, "Ticker", new=ticker_cls):
            result = await mod.get_financial_statements(
                "COST", statement_type="income", period="quarter", limit=3
            )

        assert result["source"] == "yfinance"
        assert all(r["period"] is None and r["fiscalYear"] is None for r in result["data"])
        assert all(r["calendarYear"] == r["date"][:4] for r in result["data"])
        assert "周制" in result["note"]
        assert "period / fiscalYear 未知" in result["note"]

    @pytest.mark.asyncio
    async def test_the_week_based_verdict_reads_columns_the_limit_slice_drops(self):
        # Annual columns 2026-01-31 and 2025-01-31 are month-ends and would be
        # labeled FY; the off-month-end 2024-01-27 is what reveals the issuer
        # as week-based, and it is the oldest column — dropped by limit=2.
        # The verdict has to be read off the raw DataFrame, not the served
        # records, or the two month-end columns ship with FY labels.
        import pandas as pd
        import plugins.langalpha_market_data.fundamentals_mcp_server as mod

        client = _fmp_client()
        client.get_income_statement.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        df = pd.DataFrame(
            {
                "2026-01-31": [500.0],
                "2025-01-31": [400.0],
                "2024-01-27": [300.0],
            },
            index=pd.Index(["Total Revenue"]),
        )
        ticker_inst = create_autospec(mod._yf.Ticker, instance=True)
        ticker_inst.income_stmt = df
        ticker_inst.info = {
            "financialCurrency": "USD",
            "lastFiscalYearEnd": _epoch(2026, 1, 31),
        }
        ticker_cls = create_autospec(mod._yf.Ticker, return_value=ticker_inst)

        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod._yf, "Ticker", new=ticker_cls):
            result = await mod.get_financial_statements(
                "COST", statement_type="income", period="annual", limit=2
            )

        assert result["source"] == "yfinance"
        assert [r["date"] for r in result["data"]] == ["2026-01-31", "2025-01-31"]
        assert all(r["period"] is None and r["fiscalYear"] is None for r in result["data"])
        assert "周制" in result["note"]

    @pytest.mark.asyncio
    async def test_statement_type_all_judges_week_based_across_all_three_statements(self):
        # Only the balance sheet carries an off-month-end column; the income
        # and cash-flow columns are all month-ends. The verdict is joint, so
        # every record of every statement is unlabeled.
        import pandas as pd
        import plugins.langalpha_market_data.fundamentals_mcp_server as mod

        client = _fmp_client()
        client.get_income_statement.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        inc_df = pd.DataFrame(
            {"2026-07-31": [500.0], "2026-04-30": [450.0]},
            index=pd.Index(["Total Revenue"]),
        )
        bal_df = pd.DataFrame(
            {"2026-08-01": [2000.0], "2026-04-30": [1900.0]},
            index=pd.Index(["Total Assets"]),
        )
        cf_df = pd.DataFrame(
            {"2026-07-31": [120.0], "2026-04-30": [110.0]},
            index=pd.Index(["Operating Cash Flow"]),
        )
        ticker_inst = create_autospec(mod._yf.Ticker, instance=True)
        ticker_inst.quarterly_income_stmt = inc_df
        ticker_inst.quarterly_balance_sheet = bal_df
        ticker_inst.quarterly_cashflow = cf_df
        ticker_inst.info = {
            "financialCurrency": "USD",
            "lastFiscalYearEnd": _epoch(2026, 7, 31),
        }
        ticker_cls = create_autospec(mod._yf.Ticker, return_value=ticker_inst)

        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod._yf, "Ticker", new=ticker_cls):
            result = await mod.get_financial_statements(
                "COST", statement_type="all", period="quarter", limit=2
            )

        assert result["source"] == "yfinance"
        for key in ("income_statement", "balance_sheet", "cash_flow"):
            assert all(
                r["period"] is None and r["fiscalYear"] is None
                for r in result["data"][key]
            ), key
        assert "周制" in result["note"]

    @pytest.mark.asyncio
    async def test_without_fiscal_metadata_both_labels_are_none_and_noted(self):
        import pandas as pd
        import plugins.langalpha_market_data.fundamentals_mcp_server as mod

        client = _fmp_client()
        client.get_income_statement.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        df = pd.DataFrame(
            {"2025-09-30": [500.0]},
            index=pd.Index(["Total Revenue"]),
        )
        ticker_inst = create_autospec(mod._yf.Ticker, instance=True)
        ticker_inst.quarterly_income_stmt = df
        ticker_inst.info = {}
        ticker_cls = create_autospec(mod._yf.Ticker, return_value=ticker_inst)

        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod._yf, "Ticker", new=ticker_cls):
            result = await mod.get_financial_statements(
                "MSFT", statement_type="income", period="quarter", limit=1
            )

        first = result["data"][0]
        assert first["period"] is None
        assert first["fiscalYear"] is None
        assert first["calendarYear"] == "2025"
        assert "period / fiscalYear 未知" in result["note"]

    @pytest.mark.asyncio
    async def test_ratios_need_both_sides_and_ebit_never_borrows_operating_income(self):
        import pandas as pd
        import plugins.langalpha_market_data.fundamentals_mcp_server as mod

        client = _fmp_client()
        client.get_income_statement.side_effect = FMPRequestError(
            "FMP API request failed (402)", status_code=402
        )
        inc_df = pd.DataFrame(
            {
                "2025-09-30": [1000.0, 400.0, 300.0, 200.0, 250.0],
                "2025-06-30": [900.0, float("nan"), 280.0, 180.0, 220.0],
            },
            index=pd.Index(
                ["Total Revenue", "Gross Profit", "Operating Income", "Net Income", "EBITDA"]
            ),
        )
        bal_df = pd.DataFrame(
            {
                "2025-09-30": [100.0, 50.0, 500.0],
                "2025-06-30": [80.0, float("nan"), 400.0],
            },
            index=pd.Index(
                ["Cash And Cash Equivalents", "Other Short Term Investments", "Total Assets"]
            ),
        )
        ticker_inst = create_autospec(mod._yf.Ticker, instance=True)
        ticker_inst.quarterly_income_stmt = inc_df
        ticker_inst.quarterly_balance_sheet = bal_df
        ticker_inst.info = {"financialCurrency": "USD", "lastFiscalYearEnd": _epoch(2025, 12, 31)}
        ticker_cls = create_autospec(mod._yf.Ticker, return_value=ticker_inst)

        with patch(f"{_FUND_MOD}.get_fmp_client", return_value=client), \
             patch.object(mod._yf, "Ticker", new=ticker_cls):
            result = await mod.get_financial_statements(
                "AAPL", statement_type="all", period="quarter", limit=2
            )

        sep = result["data"]["income_statement"][0]  # 2025-09-30, every input present
        assert sep["grossProfitRatio"] == 0.4
        assert sep["operatingIncomeRatio"] == 0.3
        assert sep["netIncomeRatio"] == 0.2
        assert sep["ebitdaratio"] == 0.25
        # No EBIT row on Yahoo: Operating Income must not pose as EBIT.
        assert sep["ebit"] is None

        jun = result["data"]["income_statement"][1]  # 2025-06-30, gross profit missing
        assert jun["grossProfitRatio"] is None
        assert jun["operatingIncomeRatio"] == round(280.0 / 900.0, 6)

        bal_sep = result["data"]["balance_sheet"][0]  # cash + short-term both present
        assert bal_sep["cashAndShortTermInvestments"] == 150.0
        bal_jun = result["data"]["balance_sheet"][1]  # short-term missing: cash alone is not the sum
        assert bal_jun["cashAndShortTermInvestments"] is None
        assert "cashAndShortTermInvestments" in result["note"]
        assert "grossProfitRatio" in result["note"]


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
