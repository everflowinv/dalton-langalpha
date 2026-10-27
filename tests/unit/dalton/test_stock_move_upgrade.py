"""Unit tests for stock-move attribution upgrade (DALTON-HOOK(stock-move-upgrade)).

Locks three contracts:
1. Extended-hours (`extended_hours=True` -> `prepost=True` / `extended=True`) pass-through
   across yfinance data source, MarketDataProvider, direct tools, and MCP servers, plus
   extended-hours chart freshness (04:00-20:00 ET).
2. Asset-class resolution, calendar (`WEEKDAYS_24_5` for `*=F`), market routing (`other`
   for `*=F` and `DX-Y.NYB`), and mixed-batch `fetch_quote` handling for futures,
   commodities, rates, and volatility (`NQ=F`, `ES=F`, `YM=F`, `RTY=F`, `BZ=F`, `CL=F`,
   `GC=F`, `^TNX`, `^VIX`, `DX-Y.NYB`).
3. `move_quant.py` `--session pre|post|regular` extended-hours minute-bar calculation
   (exact `< close` regular / `>= close` post boundaries, early-close calendar handling,
   date-bounded downloads, and retention-limit error) with peer/ETF/futures benchmarks.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import date, datetime
from pathlib import Path
from unittest.mock import create_autospec, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import yfinance as yf

from market_protocol import AssetClass, market_of, to_canonical
from market_protocol.symbology import WEEKDAYS_24_5
from src.data_client.base import MarketDataSource
from src.data_client.fmp.data_source import FMPDataSource
from src.data_client.fmp.fmp_client import FMPClient
from src.data_client.instrument_clock import _classify
from src.data_client.market_data_provider import (
    MarketDataProvider,
    ProviderEntry,
    is_us_symbol,
    symbol_market,
)
from src.data_client.yfinance.data_source import _fetch_history
from src.data_client.yfinance.yahoo import yahoo_symbol
from src.tools.market_data.prices import fetch_daily_prices
from src.tools.market_data.quotes import fetch_quote

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MOVE_QUANT_PATH = (
    _REPO_ROOT
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-stock-move"
    / "scripts"
    / "move_quant.py"
)


def _load_move_quant():
    spec = importlib.util.spec_from_file_location("move_quant", _MOVE_QUANT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestExtendedHoursPassThrough:
    def test_yfinance_fetch_history_passes_prepost_only_on_intraday_when_enabled(self):
        mock_ticker = create_autospec(yf.Ticker, instance=True)
        mock_ticker.history.return_value = pd.DataFrame()

        with patch("src.data_client.yfinance.yahoo.yf.Ticker", autospec=True) as mock_ticker_cls:
            mock_ticker_cls.return_value = mock_ticker
            _fetch_history("GOOGL", "5m", "2026-10-20", "2026-10-21", extended_hours=True)
            assert mock_ticker.history.call_args.kwargs.get("prepost") is True

            mock_ticker.history.reset_mock()
            _fetch_history("GOOGL", "5m", "2026-10-20", "2026-10-21", extended_hours=False)
            assert "prepost" not in mock_ticker.history.call_args.kwargs

            mock_ticker.history.reset_mock()
            _fetch_history("GOOGL", "1d", "2026-10-01", "2026-10-21", extended_hours=True)
            assert "prepost" not in mock_ticker.history.call_args.kwargs

    @pytest.mark.asyncio
    async def test_market_data_provider_forwards_extended_hours_to_source(self):
        mock_source = create_autospec(MarketDataSource, instance=True)
        mock_source.get_intraday.return_value = [
            {"time": 1761033600000, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10}
        ]
        provider = MarketDataProvider([ProviderEntry(name="yfinance", source=mock_source)])

        await provider.get_intraday(
            "GOOGL", "5min", from_date="2026-10-20", to_date="2026-10-21", extended_hours=True
        )
        assert mock_source.get_intraday.await_args.kwargs.get("extended_hours") is True

        mock_source.get_intraday.reset_mock()
        await provider.get_intraday("GOOGL", "5min", from_date="2026-10-20", to_date="2026-10-21")
        assert "extended_hours" not in mock_source.get_intraday.await_args.kwargs

    def test_yf_price_mcp_passes_prepost_when_extended_hours_true(self):
        import plugins.yfinance.yf_price_mcp_server as yf_mcp

        mock_ticker = create_autospec(yf.Ticker, instance=True)
        idx = pd.to_datetime(["2026-10-21 08:00:00-04:00"])
        mock_ticker.history.return_value = pd.DataFrame(
            {"Open": [180.0], "High": [181.0], "Low": [179.5], "Close": [180.5], "Volume": [500]},
            index=idx,
        )
        mock_ticker.history_metadata = {"currency": "USD"}

        with patch("plugins.yfinance.yf_price_mcp_server.yf.Ticker", autospec=True) as mock_ticker_cls:
            mock_ticker_cls.return_value = mock_ticker
            res = yf_mcp.get_stock_history(
                "GOOGL", period="1d", interval="5min", extended_hours=True
            )
            assert res["count"] == 1
            assert mock_ticker.history.call_args.kwargs == {
                "period": "1d",
                "interval": "5m",
                "prepost": True,
            }

            mock_ticker.history.reset_mock()
            yf_mcp.get_multiple_stocks_history(
                ["GOOGL", "NQ=F"], period="1d", interval="5min", extended_hours=True
            )
            assert mock_ticker.history.call_args.kwargs == {
                "period": "1d",
                "interval": "5m",
                "prepost": True,
            }

    @pytest.mark.asyncio
    async def test_price_data_mcp_forwards_extended_hours_to_fmp(self):
        import plugins.langalpha_market_data.price_data_mcp_server as pd_mcp

        mock_fmp = create_autospec(FMPClient, instance=True)
        mock_fmp.get_intraday_chart.return_value = [
            {"date": "2026-10-21 08:00:00", "open": 180.0, "high": 181.0, "low": 179.0, "close": 180.5, "volume": 100}
        ]
        # DALTON-HOOK(market-data-fallback): yfinance sits ahead of FMP in the
        # chain; this test pins what FMP receives, so the yfinance leg misses.
        with patch.object(pd_mcp._ginlix, "fetch_stock_data", autospec=True) as mock_ginlix_fetch, patch.object(pd_mcp, "_yf_fetch_history", new=create_autospec(pd_mcp._yf_fetch_history, return_value=[])), patch("plugins.langalpha_market_data.price_data_mcp_server.get_fmp_client", autospec=True) as mock_get_fmp:
            mock_ginlix_fetch.return_value = None
            mock_get_fmp.return_value = mock_fmp
            res = await pd_mcp.get_stock_data(
                "GOOGL",
                interval="5min",
                start_date="2026-10-20",
                end_date="2026-10-21",
                extended_hours=True,
            )
            assert res["count"] == 1
            assert mock_fmp.get_intraday_chart.await_args.kwargs.get("extended") is True

    @pytest.mark.asyncio
    async def test_extended_hours_chart_freshness_at_0830_with_0400_last_bar_is_delayed_not_live_or_closed(self):
        et_tz = ZoneInfo("America/New_York")
        req_time = datetime(2026, 10, 21, 8, 30, 0, tzinfo=et_tz)
        last_bar_0400_ms = int(datetime(2026, 10, 21, 4, 0, 0, tzinfo=et_tz).timestamp() * 1000)

        mock_provider = create_autospec(MarketDataProvider, instance=True)
        mock_provider.get_daily_with_source.return_value = (
            [
                {"time": int(datetime(2026, 10, 19, 0, 0, tzinfo=et_tz).timestamp() * 1000), "open": 180.0, "high": 182.0, "low": 179.0, "close": 181.0, "volume": 1000},
                {"time": int(datetime(2026, 10, 20, 0, 0, tzinfo=et_tz).timestamp() * 1000), "open": 181.0, "high": 183.0, "low": 180.0, "close": 182.0, "volume": 1200},
            ],
            "yfinance",
            None,
        )
        intraday_bars = [
            {
                "time": last_bar_0400_ms - (5 - i) * 300_000,
                "open": 180.0,
                "high": 180.5,
                "low": 179.5,
                "close": 180.0,
                "volume": 100,
            }
            for i in range(6)
        ]
        mock_provider.get_intraday_with_source.return_value = (intraday_bars, "yfinance", None)
        mock_provider.get_snapshots.return_value = []

        def _fake_venue_local_time(ref, at=None):
            if at is not None:
                return at.astimezone(et_tz)
            return req_time

        with patch("src.tools.market_data.prices.get_market_data_provider", autospec=True) as mock_get_prov, patch("src.tools.market_data.prices.display_names", autospec=True) as mock_disp, patch("src.tools.market_data.prices.venue_local_time", autospec=True, side_effect=_fake_venue_local_time):
            mock_get_prov.return_value = mock_provider
            mock_disp.return_value = (None, None)
            _, artifact = await fetch_daily_prices("GOOGL", limit=2, extended_hours=True)

        cf = artifact["chart_freshness"]
        assert cf["label"] == "delayed"
        assert cf["label"] != "live"
        assert cf["closed"] is False
        assert cf["lag_s"] > 0

    @pytest.mark.asyncio
    async def test_extended_hours_chart_freshness_skips_non_us_stocks_and_us_holidays(self):
        from src.tools.market_data.display import resolve_ref
        from src.tools.market_data.prices import _apply_extended_hours_freshness
        from src.data_client.freshness import Freshness, FreshnessLabel

        et_tz = ZoneInfo("America/New_York")
        last_bar_ms = int(datetime(2026, 10, 21, 4, 0, 0, tzinfo=et_tz).timestamp() * 1000)
        base_fresh = Freshness(
            expected_latest=last_bar_ms,
            actual_latest=last_bar_ms,
            lag_s=0,
            label=FreshnessLabel.LIVE,
            measured=True,
            source="yfinance",
            interval="5min",
            closed=True,
        )

        # 1. HK stock ("0700.HK") is untouched even during 08:30 ET
        hk_ref = resolve_ref("0700.HK")
        hk_res = _apply_extended_hours_freshness(
            base_fresh,
            "5min",
            now=datetime(2026, 10, 21, 8, 30, 0, tzinfo=et_tz),
            symbol="0700.HK",
            ref=hk_ref,
        )
        assert hk_res.label == FreshnessLabel.LIVE
        assert hk_res.closed is True

        # 2. US NYSE holiday (2026-12-25 Christmas) is untouched even at 08:30 ET
        us_ref = resolve_ref("GOOGL")
        holiday_res = _apply_extended_hours_freshness(
            base_fresh,
            "5min",
            now=datetime(2026, 12, 25, 8, 30, 0, tzinfo=et_tz),
            symbol="GOOGL",
            ref=us_ref,
        )
        assert holiday_res.label == FreshnessLabel.LIVE
        assert holiday_res.closed is True


class TestFuturesCommoditiesRatesSymbologyAndRouting:
    @pytest.mark.parametrize(
        "sym",
        ["NQ=F", "ES=F", "YM=F", "RTY=F", "BZ=F", "CL=F", "GC=F"],
    )
    def test_futures_use_weekdays_24_5_calendar_and_other_market(self, sym: str):
        ref = to_canonical(sym)
        assert ref.symbol == sym
        assert ref.calendar_id == WEEKDAYS_24_5
        assert market_of(ref) == "other"
        assert symbol_market(sym) == "other"
        assert is_us_symbol(sym) is False
        assert yahoo_symbol(sym, is_index=True) == sym
        assert FMPDataSource._api_symbol(sym, is_index=True) == sym

    @pytest.mark.parametrize("sym,family", [("^TNX", "TNX"), ("^VIX", "VIX")])
    def test_rates_and_vix_resolve_as_us_calendar_indexes(self, sym: str, family: str):
        ref = to_canonical(sym)
        assert ref.asset_class is AssetClass.INDEX
        assert ref.index_family == family
        assert ref.calendar_id == "XNYS"
        clock = _classify(ref, is_index=True)
        assert getattr(clock, "daily_backstop", False) is True

    def test_dxy_routes_to_other_market(self):
        ref = to_canonical("DX-Y.NYB")
        assert ref.symbol == "DX-Y.NYB"
        assert market_of(ref) == "other"
        assert symbol_market("DX-Y.NYB") == "other"
        assert is_us_symbol("DX-Y.NYB") is False

    @pytest.mark.asyncio
    async def test_fetch_quote_splits_caret_indexes_from_stocks_and_futures_batch(self):
        mock_provider = create_autospec(MarketDataProvider, instance=True)

        async def _fake_get_snapshots(symbols, asset_type="stocks", user_id=None):
            return [
                {"symbol": s, "price": 100.0, "change": 1.0, "change_percent": 1.0, "previous_close": 99.0}
                for s in symbols
            ]

        mock_provider.get_snapshots.side_effect = _fake_get_snapshots

        with patch("src.tools.market_data.quotes.get_market_data_provider", autospec=True) as mock_get_prov, patch("src.tools.market_data.quotes.fill_quote_names", autospec=True), patch("src.server.services.cache.quote_daily_fallback.fill_from_daily", autospec=True, side_effect=lambda served, asked, uid: served):
            mock_get_prov.return_value = mock_provider
            content, artifact = await fetch_quote(
                ["GOOGL", "NQ=F", "CL=F", "^TNX", "^VIX", "DX-Y.NYB"],
                asset_type="stocks",
            )

        calls = mock_provider.get_snapshots.await_args_list
        assert len(calls) == 2
        assert calls[0].args[0] == ["GOOGL", "NQ=F", "CL=F", "DX-Y.NYB"]
        assert calls[0].kwargs["asset_type"] == "stocks"
        assert calls[1].args[0] == ["TNX", "VIX"]
        assert calls[1].kwargs["asset_type"] == "indices"
        returned_syms = {q["symbol"] for q in artifact["quotes"]}
        assert returned_syms == {"GOOGL", "NQ=F", "CL=F", "TNX", "VIX", "DX-Y.NYB"}
        assert "no data" not in content


class TestMoveQuantExtendedSessions:
    def test_pre_and_post_session_quant_and_fallback(self):
        mq = _load_move_quant()

        idx = pd.to_datetime([
            "2026-10-20 15:55:00-04:00",
            "2026-10-21 04:30:00-04:00",
            "2026-10-21 08:30:00-04:00",
            "2026-10-21 09:30:00-04:00",
            "2026-10-21 15:55:00-04:00",
            "2026-10-21 16:30:00-04:00",
            "2026-10-21 18:00:00-04:00",
        ])
        close_df = pd.DataFrame(
            {
                "GOOGL": [200.0, 196.0, 192.0, 193.0, 195.0, 191.1, 189.15],
                "META": [500.0, 497.5, 495.0, 496.0, 500.0, 498.0, 497.5],
                "AMZN": [200.0, 199.0, 198.0, 198.5, 200.0, 199.0, 199.0],
                "QQQ": [500.0, 497.5, 495.0, 496.0, 500.0, 498.0, 497.5],
                "NQ=F": [20000.0, 19900.0, 19800.0, 19850.0, 20000.0, 19920.0, 19900.0],
                "ES=F": [5800.0, 5780.0, 5742.0, 5750.0, 5800.0, 5780.0, 5771.0],
            },
            index=idx,
        )

        pre_res = mq.compute_session_quant_from_intraday(
            target="GOOGL",
            peers=["META", "AMZN", "0700.HK"],
            sector_etf="QQQ",
            close_df=close_df,
            session="pre",
            session_date="2026-10-21",
            futures=["NQ=F", "ES=F"],
        )
        assert pre_res["ok"] is True
        assert pre_res["data_ok"] is True
        assert pre_res["session"] == "pre"
        assert pre_res["base_close"] == 200.0
        assert pre_res["close"] == 192.0
        assert pre_res["change_pct"] == -4.0
        assert pre_res["peer_median"] == -1.0
        assert pre_res["etf_change"] == -1.0
        assert pre_res["futures_benchmark"] == "NQ=F"
        assert pre_res["futures_change"] == -1.0
        assert pre_res["alpha_vs_peers"] == -3.0
        assert pre_res["alpha_vs_futures"] == -3.0
        assert any("0700.HK" in w for w in pre_res["warnings"])

        post_res = mq.compute_session_quant_from_intraday(
            target="GOOGL",
            peers=["META", "AMZN"],
            sector_etf="QQQ",
            close_df=close_df,
            session="post",
            session_date="2026-10-21",
            futures=["NQ=F", "ES=F"],
        )
        assert post_res["ok"] is True
        assert post_res["session"] == "post"
        assert post_res["base_close"] == 195.0
        assert post_res["close"] == 189.15
        assert post_res["change_pct"] == -3.0
        assert post_res["futures_change"] == -0.5
        assert post_res["alpha_vs_futures"] == -2.5

        unsup = mq.compute_session_quant_from_intraday(
            target="0700.HK",
            peers=["BABA"],
            sector_etf="KWEB",
            close_df=close_df,
            session="pre",
        )
        assert unsup["ok"] is False
        assert unsup["unsupported_session"] is True
        assert "--session regular" in unsup["fallback_hint"]

    def test_post_market_boundary_includes_1600_bar_in_post_session_and_volume(self):
        mq = _load_move_quant()

        idx = pd.to_datetime([
            "2026-10-21 15:55:00-04:00",
            "2026-10-21 16:00:00-04:00",
            "2026-10-21 16:05:00-04:00",
        ])
        close_df = pd.DataFrame({"GOOGL": [100.0, 90.0, 90.0]}, index=idx)
        vol_df = pd.DataFrame({"GOOGL": [1000, 250, 150]}, index=idx)

        post_res = mq.compute_session_quant_from_intraday(
            target="GOOGL",
            peers=[],
            sector_etf=None,
            close_df=close_df,
            volume_df=vol_df,
            session="post",
            session_date="2026-10-21",
            futures=[],
        )
        assert post_res["ok"] is True
        assert post_res["base_close"] == 100.0
        assert post_res["close"] == 90.0
        assert post_res["change_pct"] == -10.0
        assert post_res["bar_count"] == 2
        assert post_res["session_volume"] == 400

    def test_early_close_calendar_1300_vs_fallback_warning(self):
        mq = _load_move_quant()

        idx = pd.to_datetime([
            "2026-11-27 12:55:00-05:00",
            "2026-11-27 13:00:00-05:00",
            "2026-11-27 15:55:00-05:00",
            "2026-11-27 16:05:00-05:00",
        ])
        close_df = pd.DataFrame({"GOOGL": [100.0, 95.0, 92.0, 90.0]}, index=idx)
        vol_df = pd.DataFrame({"GOOGL": [500, 100, 200, 300]}, index=idx)

        fake_xcals = types.ModuleType("exchange_calendars")

        class _FakeCal:
            def is_session(self, ds: str) -> bool:
                return True

            def session_close(self, ds: str) -> pd.Timestamp:
                return pd.Timestamp("2026-11-27 13:00:00-05:00")

        fake_xcals.get_calendar = lambda name: _FakeCal()

        with patch.dict(sys.modules, {"exchange_calendars": fake_xcals}):
            res_cal = mq.compute_session_quant_from_intraday(
                target="GOOGL",
                peers=[],
                sector_etf=None,
                close_df=close_df,
                volume_df=vol_df,
                session="post",
                session_date="2026-11-27",
                futures=[],
            )
        assert res_cal["ok"] is True
        assert res_cal["base_close"] == 100.0
        assert res_cal["close"] == 90.0
        assert res_cal["change_pct"] == -10.0
        assert res_cal["session_volume"] == 600
        assert "未校验提前收市日" not in res_cal["warnings"]

        with patch.dict(sys.modules, {"exchange_calendars": None, "pandas_market_calendars": None}):
            res_nocal = mq.compute_session_quant_from_intraday(
                target="GOOGL",
                peers=[],
                sector_etf=None,
                close_df=close_df,
                volume_df=vol_df,
                session="post",
                session_date="2026-11-27",
                futures=[],
            )
        assert res_nocal["ok"] is True
        assert res_nocal["base_close"] == 92.0
        assert res_nocal["session_volume"] == 300
        assert "未校验提前收市日" in res_nocal["warnings"]

    def test_pre_market_rejects_when_prior_day_has_only_1600_post_bar_and_zero_volume_warns(self):
        mq = _load_move_quant()

        # Only prior day 16:00 bar (post-market) + target day pre-market bar -> must fail
        idx_no_reg = pd.to_datetime([
            "2026-10-20 16:00:00-04:00",
            "2026-10-21 08:30:00-04:00",
        ])
        close_no_reg = pd.DataFrame({"GOOGL": [100.0, 95.0]}, index=idx_no_reg)
        res_no_reg = mq.compute_session_quant_from_intraday(
            target="GOOGL",
            peers=[],
            sector_etf=None,
            close_df=close_no_reg,
            session="pre",
            session_date="2026-10-21",
            futures=[],
        )
        assert res_no_reg["ok"] is False
        assert res_no_reg["data_ok"] is False
        assert "常规时段" in res_no_reg["error"]

        # All-zero session volume -> session_volume is None and warning added
        idx_zero_vol = pd.to_datetime([
            "2026-10-20 15:55:00-04:00",
            "2026-10-21 08:00:00-04:00",
            "2026-10-21 08:30:00-04:00",
        ])
        close_zv = pd.DataFrame({"GOOGL": [100.0, 96.0, 95.0]}, index=idx_zero_vol)
        vol_zv = pd.DataFrame({"GOOGL": [1000, 0, 0]}, index=idx_zero_vol)
        res_zv = mq.compute_session_quant_from_intraday(
            target="GOOGL",
            peers=[],
            sector_etf=None,
            close_df=close_zv,
            volume_df=vol_zv,
            session="pre",
            session_date="2026-10-21",
            futures=[],
        )
        assert res_zv["ok"] is True
        assert res_zv["session_volume"] is None
        assert "yfinance 未提供盘前/盘后成交量" in res_zv["warnings"]

    def test_default_target_date_and_stale_last_bar_warning(self):
        mq = _load_move_quant()

        default_target = mq._default_us_target_date()
        prev_dt = mq._previous_trading_day(default_target)
        prev_prev_dt = mq._previous_trading_day(prev_dt)

        # Data stops on prev_dt (missing default_target): must NOT silently compute prev_dt
        idx_stale = pd.to_datetime([
            f"{prev_prev_dt.isoformat()} 15:55:00-04:00",
            f"{prev_dt.isoformat()} 08:30:00-04:00",
            f"{prev_dt.isoformat()} 15:55:00-04:00",
        ])
        close_stale = pd.DataFrame({"GOOGL": [100.0, 98.0, 99.0]}, index=idx_stale)
        res_stale = mq.compute_session_quant_from_intraday(
            target="GOOGL",
            peers=[],
            sector_etf=None,
            close_df=close_stale,
            session="pre",
            session_date=None,
            futures=[],
        )
        assert res_stale["session_date"] == default_target.isoformat()
        assert res_stale["data_ok"] is False
        assert any(f"最新分钟线停在 {prev_dt.isoformat()}" in w for w in res_stale["warnings"])

    def test_calendar_non_importerror_falls_back_to_mcal_then_unverified_warning(self):
        mq = _load_move_quant()
        et_tz = ZoneInfo("America/New_York")

        broken_xcals = types.ModuleType("exchange_calendars")

        def _raise_runtime_error(name: str):
            raise RuntimeError("calendar internal failure")

        broken_xcals.get_calendar = _raise_runtime_error

        with patch.dict(sys.modules, {"exchange_calendars": broken_xcals, "pandas_market_calendars": None}):
            close_mins, checked = mq._resolve_regular_close_minutes(
                datetime(2026, 10, 21).date(), et_tz
            )
        assert close_mins == 16 * 60
        assert checked is False

    def test_run_analyze_downloads_target_date_range_and_rejects_expired_retention(self):
        from datetime import timedelta

        mq = _load_move_quant()
        today_et = datetime.now(ZoneInfo("America/New_York")).date()

        # Boundary: target_dt is exactly 60 days ago, so its prior trading day is >60 days ago
        boundary_dt = today_et - timedelta(days=60)
        boundary_res = mq.run_analyze(
            ticker="GOOGL",
            window=boundary_dt.isoformat(),
            session="pre",
        )
        assert boundary_res["ok"] is False
        assert "超出分钟线保留期" in boundary_res["error"]
        assert any("超出分钟线保留期" in w for w in boundary_res["warnings"])

        target_dt = mq._latest_us_trading_day_on_or_before(today_et - timedelta(days=5))
        prev_dt = mq._previous_trading_day(target_dt)
        idx = pd.to_datetime([
            f"{prev_dt.isoformat()} 15:55:00-04:00",
            f"{target_dt.isoformat()} 08:30:00-04:00",
        ])
        fake_raw = pd.DataFrame(
            {"Close": [100.0, 95.0], "Volume": [1000, 200]},
            index=idx,
        )
        with patch("yfinance.download", autospec=True) as mock_dl:
            mock_dl.return_value = fake_raw
            res = mq.run_analyze(
                ticker="GOOGL",
                window="1d",
                peers_arg="GOOGL",
                etf_arg="GOOGL",
                end_override=target_dt.isoformat(),
                session="pre",
                futures_arg="GOOGL",
            )
            assert res["ok"] is True
            assert res["session_date"] == target_dt.isoformat()
            assert res["change_pct"] == -5.0
            dl_kwargs = mock_dl.call_args.kwargs
            assert "period" not in dl_kwargs
            assert dl_kwargs["start"] == prev_dt.isoformat()
            assert dl_kwargs["end"] == (target_dt + timedelta(days=1)).isoformat()
            assert dl_kwargs["prepost"] is True


def test_trading_day_fallback_uses_stdlib_nyse_holidays_without_calendar_libs(monkeypatch):
    """With neither calendar library importable, NYSE holidays are still closed days."""
    import builtins

    mq = _load_move_quant()
    real_import = builtins.__import__

    def _no_calendars(name, *args, **kwargs):
        if name in ("exchange_calendars", "pandas_market_calendars"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_calendars)
    assert mq._is_us_trading_day(date(2026, 11, 26)) is False  # Thanksgiving
    assert mq._is_us_trading_day(date(2026, 12, 25)) is False  # Christmas
    assert mq._is_us_trading_day(date(2026, 10, 22)) is True
    assert mq._previous_trading_day(date(2026, 11, 27)) == date(2026, 11, 25)
