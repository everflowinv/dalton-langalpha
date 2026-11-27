"""Unit tests for watchlist Wilder RSI, exchange-timezone bars/YTD, progressive cached metrics, and privacy context."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest
from pydantic import TypeAdapter

from src.dalton.database import coverage as coverage_db
from src.dalton.watchlist import indicators as ind_mod
from src.dalton.watchlist import metrics as metrics_mod
from src.dalton.watchlist import privacy as privacy_mod
from src.data_client.base import MarketDataSource
from src.data_client.financial_data_provider import FinancialDataProvider
from src.data_client.fmp.financial_source import FMPFinancialSource
from src.data_client.market_data_provider import MarketDataProvider, ProviderEntry
from src.server.models.additional_context import AdditionalContext, WatchlistContext
from src.server.services.cache.quote_cache_service import QuoteCacheService
from src.utils.cache.redis_cache import RedisCacheClient


def test_wilder_rsi_and_symbol_metrics_from_daily_bars() -> None:
    # Insufficient bars -> RSI is None
    assert ind_mod.wilder_rsi([100.0] * 14, 14) is None
    # Strictly rising closes -> RSI = 100.0
    rising = [100.0 + i for i in range(40)]
    assert ind_mod.wilder_rsi(rising, 14) == 100.0
    assert ind_mod.wilder_rsi(rising, 30) == 100.0

    # Build 260 chronological daily bars spanning last year into current year
    start = date(2025, 11, 1)
    bars: list[dict[str, Any]] = []
    price = 100.0
    for i in range(260):
        d = start + timedelta(days=i)
        price += 0.5 if i % 3 != 0 else -0.2
        bars.append(
            {
                "date": d.isoformat(),
                "close": round(price, 4),
                "high": round(price + 1.0, 4),
                "low": round(price - 1.0, 4),
                "volume": 1_000_000 if i < 255 else 2_000_000,
            }
        )

    out = ind_mod.compute_symbol_metrics(
        bars,
        symbol="NVDA",
        latest_price=bars[-1]["close"],
        currency="USD",
        market_cap=2_500_000_000_000,
        next_earnings="2026-12-01",
        now_utc=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
    )
    assert out["price"] == round(bars[-1]["close"], 4)
    assert out["chg_1d"] is not None
    assert out["chg_1w"] is not None
    assert out["chg_1m"] is not None
    assert out["chg_3m"] is not None
    assert out["chg_ytd"] is not None
    assert out["chg_1y"] is not None
    assert out["position_52w"] is not None
    assert 0.0 <= out["position_52w"] <= 100.0
    assert out["rsi_14d"] is not None
    assert out["rsi_30d"] is not None
    assert out["vol_ratio_5_20"] is not None
    assert out["vol_ratio_5_20"] > 1.0
    assert out["market_cap"] == 2_500_000_000_000
    assert out["next_earnings"] == "2026-12-01"


def test_r4_indicators_exchange_tz_weekend_no_duplicate_and_cross_year_ytd() -> None:
    """r2 Codex P1 / Grok H: exchange timezone bar dates, no Saturday phantom bar, no duplicate HK today bar, cross-year YTD."""
    # 1. HK equity in UTC container: 2026-05-14 16:00 UTC == 2026-05-15 00:00 Asia/Hong_Kong!
    hk_bars = [
        {"timestamp": int(datetime(2026, 5, 13, 16, 0, tzinfo=timezone.utc).timestamp()), "close": 400.0, "high": 405.0, "low": 395.0, "volume": 100},
        {"timestamp": int(datetime(2026, 5, 14, 16, 0, tzinfo=timezone.utc).timestamp()), "close": 410.0, "high": 415.0, "low": 405.0, "volume": 100},
    ]
    # Live quote at 2026-05-15 06:00 UTC (14:00 HKT, SAME HK trade date 2026-05-15) updates last close without duplicating a bar!
    out_hk = ind_mod.compute_symbol_metrics(
        hk_bars,
        symbol="0700.HK",
        latest_price=420.0,
        quote_timestamp="2026-05-15T06:00:00Z",
        now_utc=datetime(2026, 5, 15, 6, 0, tzinfo=timezone.utc),
    )
    # 1D change uses prior bar (400.0) -> (420 - 400)/400 = +5.0% (if duplicated, base would wrongly be 410.0!)
    assert out_hk["chg_1d"] == 5.0

    # 2. US Saturday: bars end Friday 2026-05-15; live quote on Saturday must NOT fabricate a Saturday bar!
    us_bars = [
        {"date": "2026-05-14", "close": 100.0, "high": 101.0, "low": 99.0, "volume": 100},
        {"date": "2026-05-15", "close": 110.0, "high": 111.0, "low": 109.0, "volume": 100},
    ]
    out_sat = ind_mod.compute_symbol_metrics(
        us_bars,
        symbol="NVDA",
        latest_price=110.0,
        now_utc=datetime(2026, 5, 16, 15, 0, tzinfo=timezone.utc),  # Saturday ET
    )
    assert out_sat["chg_1d"] == 10.0

    # 3. Cross-year YTD: historical bars end 2025-12-31, live quote on 2026-01-02 -> YTD anchored to 2025-12-31 (NOT 2024-12-31!)
    cross_bars = [
        {"date": "2024-12-31", "close": 50.0, "high": 51.0, "low": 49.0, "volume": 100},
        {"date": "2025-12-31", "close": 100.0, "high": 101.0, "low": 99.0, "volume": 100},
    ]
    out_ytd = ind_mod.compute_symbol_metrics(
        cross_bars,
        symbol="NVDA",
        latest_price=105.0,
        quote_timestamp="2026-01-02T15:00:00Z",
        now_utc=datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc),
    )
    assert out_ytd["chg_ytd"] == 5.0


@pytest.mark.asyncio
async def test_metrics_service_skips_private_and_enriches_earnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics_mod._clear_metrics_cache_for_tests()
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(
            coverage_db.list_coverage_rows,
            return_value=[
                {"user_id": "u1", "ticker": "NVDA", "tier": "core", "is_private": False},
                {"user_id": "u1", "ticker": "OPENAI", "tier": "core", "is_private": True},
            ],
        ),
    )
    # Real calendar_state.py dict keyed by ticker (plus tentative_date support!)
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(
            coverage_db.get_earnings_calendar_mirror,
            return_value={
                "ledger": {
                    "entries": {
                        "NVDA": {"status": "confirmed", "date": "2026-12-15"},
                        "AMD": {"status": "pending", "date": None, "tentative_date": "2026-12-20"},
                    }
                }
            },
        ),
    )
    assert metrics_mod._extract_next_earnings_map(
        {"ledger": {"entries": {"AMD": {"date": None, "tentative_date": "2026-12-20"}}}}
    ) == {"AMD": "2026-12-20"}

    snap_calls: list[list[str]] = []

    async def _get_snapshots(symbols: list[str], asset_type: str = "stocks", user_id: str | None = None) -> list[dict[str, Any]]:
        _ = (asset_type, user_id)
        snap_calls.append(list(symbols))
        return [{"symbol": "NVDA", "price": 140.0, "previous_close": 135.0, "currency": "USD"}]

    async def _get_daily(symbol: str, from_date: str | None = None, to_date: str | None = None, is_index: bool = False, user_id: str | None = None) -> list[dict[str, Any]]:
        _ = (symbol, from_date, to_date, is_index, user_id)
        return [
            {"date": (date(2026, 1, 2) + timedelta(days=i)).isoformat(), "close": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "volume": 1000}
            for i in range(40)
        ]

    fake_prov = create_autospec(MarketDataProvider, instance=True)
    fake_prov.get_snapshots.side_effect = _get_snapshots
    fake_prov.get_daily.side_effect = _get_daily

    fin_source = create_autospec(FMPFinancialSource, instance=True)
    fin_source.get_company_profile.return_value = [{"marketCap": 3.4e12, "currency": "USD"}]
    fake_fin_prov = FinancialDataProvider(financial=fin_source)

    monkeypatch.setattr(
        metrics_mod,
        "_resolve_provider_and_key",
        create_autospec(metrics_mod._resolve_provider_and_key, return_value=(fake_prov, "fmp", True)),
    )
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_financial_provider",
        create_autospec(metrics_mod._resolve_financial_provider, return_value=fake_fin_prov),
    )

    res = await metrics_mod.WatchlistMetricsService().get_metrics_for_user("u1", wait_for_cold=True)
    assert res["ok"] is True
    assert snap_calls == [["NVDA"]]  # Private OPENAI skipped!
    assert res["items"]["OPENAI"]["is_private"] is True
    assert res["items"]["OPENAI"]["status"] == "unlisted"
    assert res["items"]["NVDA"]["price"] == 140.0
    assert res["items"]["NVDA"]["market_cap"] == 3.4e12
    assert res["items"]["NVDA"]["next_earnings"] == "2026-12-15"
    assert res["items"]["NVDA"]["rsi_14d"] is not None
    assert res["summary"]["total_count"] == 2
    assert res["summary"]["upcoming_earnings"][0] == {"ticker": "NVDA", "date": "2026-12-15"}


@pytest.mark.asyncio
async def test_r4_metrics_no_empty_bar_cache_progressive_warmup_and_watchlist_context_privacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r2 Codex P1/P2: empty bars are never cached; progressive non-blocking return warms bars in background;
    WatchlistContext validates in AdditionalContext and is stripped on shared workspaces AND shared threads."""
    metrics_mod._clear_metrics_cache_for_tests()
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(
            coverage_db.list_coverage_rows,
            return_value=[{"user_id": "u1", "ticker": "TSM", "tier": "watch", "is_private": False}],
        ),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(coverage_db.get_earnings_calendar_mirror, return_value=None),
    )

    daily_calls = {"n": 0}

    class _FlakyThenWarmProvider:
        name = "yfinance"
        split_adjusted = True

        async def get_snapshots(self, symbols: Any, *, asset_type: str = "stocks", user_id: str = "") -> list[dict[str, Any]]:
            _ = (symbols, asset_type, user_id)
            return [{"symbol": "TSM", "price": 180.0, "previous_close": 175.0, "currency": "USD"}]

        async def get_daily(self, symbol: str, *, from_date: str, to_date: str, is_index: bool = False, user_id: str = "") -> list[dict[str, Any]]:
            _ = (symbol, from_date, to_date, is_index, user_id)
            daily_calls["n"] += 1
            if daily_calls["n"] == 1:
                return []  # transient empty response MUST NOT be cached!
            return [
                {"date": (date(2026, 1, 2) + timedelta(days=i)).isoformat(), "close": 140.0 + i, "high": 141.0 + i, "low": 139.0 + i, "volume": 1000}
                for i in range(40)
            ]

    prov = _FlakyThenWarmProvider()
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_provider_and_key",
        create_autospec(metrics_mod._resolve_provider_and_key, return_value=(prov, "yfinance", True)),
    )
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_financial_provider",
        create_autospec(
            metrics_mod._resolve_financial_provider,
            return_value=FinancialDataProvider(financial=None),
        ),
    )

    # First progressive call returns live snapshot immediately & lists TSM as pending
    res1 = await metrics_mod.WatchlistMetricsService().get_metrics_for_user("u1", wait_for_cold=False)
    assert res1["pending_symbols"] == ["TSM"]
    assert res1["items"]["TSM"]["price"] == 180.0
    await asyncio.sleep(0.05)  # let background task complete empty response
    # Empty response was NOT cached -> second progressive call triggers warm-up again (now succeeds!)
    res2 = await metrics_mod.WatchlistMetricsService().get_metrics_for_user("u1", wait_for_cold=False)
    assert res2["pending_symbols"] == ["TSM"]
    await asyncio.sleep(0.05)  # let 2nd background warm-up populate cache
    # Third call hits warm cache immediately with full RSI and empty pending_symbols!
    res3 = await metrics_mod.WatchlistMetricsService().get_metrics_for_user("u1", wait_for_cold=False)
    assert res3["pending_symbols"] == []
    assert res3["items"]["TSM"]["rsi_14d"] is not None

    # WatchlistContext validates cleanly in AdditionalContext union (no HTTP 422!)
    adapter = TypeAdapter(AdditionalContext)
    parsed = adapter.validate_python({"type": "watchlist_context", "content": "<watchlist_context>NVDA</watchlist_context>"})
    assert isinstance(parsed, WatchlistContext)
    reminder = privacy_mod.build_watchlist_context_reminder([parsed])
    assert reminder is not None and "<watchlist_context>NVDA</watchlist_context>" in reminder

    # Shared thread strips WatchlistContext even when workspace itself is NOT shared!
    monkeypatch.setattr(
        privacy_mod,
        "_is_thread_publicly_shared",
        create_autospec(privacy_mod._is_thread_publicly_shared, return_value=True),
    )
    req_shared_thread = SimpleNamespace(additional_context=[parsed, {"type": "directive", "content": "keep"}])
    await privacy_mod.sanitize_turn_watchlist_context(
        req_shared_thread,
        thread_id="t-shared",
        is_shared=False,
        user_id="u1",
        workspace_owner_id="u1",
    )
    assert len(req_shared_thread.additional_context) == 1
    assert req_shared_thread.additional_context[0]["type"] == "directive"


def test_r5_new_years_day_holiday_ytd_is_zero_not_prior_year_gain() -> None:
    """r5: On Jan 1 (exchange holiday), YTD anchors to Dec 31 of prior year (0%), not 2 years back (50%)."""
    bars = [
        {"date": "2024-12-31", "close": 100.0, "high": 101.0, "low": 99.0, "volume": 1000},
        {"date": "2025-12-30", "close": 148.0, "high": 149.0, "low": 147.0, "volume": 1000},
        {"date": "2025-12-31", "close": 150.0, "high": 151.0, "low": 149.0, "volume": 1000},
    ]
    res = ind_mod.compute_symbol_metrics(
        bars,
        symbol="NVDA",
        latest_price=150.0,
        now_utc=datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc),
    )
    assert res["chg_ytd"] == 0.0


@pytest.mark.asyncio
async def test_market_cap_from_financial_provider_currency_and_error_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Market cap reads financial.get_company_profile, propagates HKD currency, handles financial=None, and never caches exceptions."""
    metrics_mod._clear_metrics_cache_for_tests()

    # 1. Financial source returns marketCap + currency (AAPL USD & 0700.HK HKD) and caches 24h
    fin_source = create_autospec(FMPFinancialSource, instance=True)

    async def _profile(symbol: str) -> list[dict[str, Any]]:
        if symbol == "AAPL":
            return [{"symbol": "AAPL", "marketCap": 5167353167872, "currency": "USD"}]
        if symbol == "0700.HK":
            return [{"symbol": "0700.HK", "mktCap": 3111992426496, "currency": "HKD"}]
        return []

    fin_source.get_company_profile.side_effect = _profile
    fin_prov = FinancialDataProvider(financial=fin_source)

    cap_aapl, cur_aapl = await metrics_mod._fetch_market_cap_cached("AAPL", financial_provider=fin_prov)
    assert cap_aapl == 5167353167872.0
    assert cur_aapl == "USD"
    # Second lookup hits 24h cache without calling provider again
    cap_aapl2, cur_aapl2 = await metrics_mod._fetch_market_cap_cached("AAPL", financial_provider=fin_prov)
    assert (cap_aapl2, cur_aapl2) == (5167353167872.0, "USD")
    assert fin_source.get_company_profile.await_count == 1

    # 2. financial is None -> returns (None, None) cleanly
    none_fin_prov = FinancialDataProvider(financial=None)
    cap_none, cur_none = await metrics_mod._fetch_market_cap_cached("MSFT", financial_provider=none_fin_prov)
    assert (cap_none, cur_none) == (None, None)

    # 3. Exception raised -> returns (None, None) and is NOT cached (next call succeeds)
    flaky_source = create_autospec(FMPFinancialSource, instance=True)
    flaky_calls = {"n": 0}

    async def _flaky_profile(symbol: str) -> list[dict[str, Any]]:
        _ = symbol
        flaky_calls["n"] += 1
        if flaky_calls["n"] == 1:
            raise RuntimeError("transient upstream timeout")
        return [{"market_cap": 900_000_000_000, "currency": "USD"}]

    flaky_source.get_company_profile.side_effect = _flaky_profile
    flaky_prov = FinancialDataProvider(financial=flaky_source)
    assert await metrics_mod._fetch_market_cap_cached("META", financial_provider=flaky_prov) == (None, None)
    assert "META" not in metrics_mod._MARKET_CAP_CACHE
    assert await metrics_mod._fetch_market_cap_cached("META", financial_provider=flaky_prov) == (900_000_000_000.0, "USD")
    assert flaky_calls["n"] == 2

    # 4. HK stock end-to-end through WatchlistMetricsService carries HKD currency even when snapshot omits currency
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(
            coverage_db.list_coverage_rows,
            return_value=[{"user_id": "u1", "ticker": "0700.HK", "tier": "core", "is_private": False}],
        ),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(coverage_db.get_earnings_calendar_mirror, return_value=None),
    )
    mkt_prov = create_autospec(MarketDataProvider, instance=True)
    mkt_prov.get_snapshots.return_value = [{"symbol": "0700.HK", "price": 410.0, "previous_close": 405.0}]
    mkt_prov.get_daily.return_value = [
        {"date": (date(2026, 1, 2) + timedelta(days=i)).isoformat(), "close": 380.0 + i, "high": 385.0 + i, "low": 375.0 + i, "volume": 1000}
        for i in range(40)
    ]
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_provider_and_key",
        create_autospec(metrics_mod._resolve_provider_and_key, return_value=(mkt_prov, "yfinance", True)),
    )
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_financial_provider",
        create_autospec(metrics_mod._resolve_financial_provider, return_value=fin_prov),
    )

    out = await metrics_mod.WatchlistMetricsService().get_metrics_for_user("u1", wait_for_cold=True)
    hk_item = out["items"]["0700.HK"]
    assert hk_item["market_cap"] == 3111992426496.0
    assert hk_item["currency"] == "HKD"
    assert hk_item["market_cap_currency"] == "HKD"


@pytest.mark.asyncio
async def test_pending_subset_poll_warmup_dedup_and_bounded_mcap_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated pending-subset polls do not duplicate in-flight bars/mcap warmups, reuse short snapshot cache, and cap mcap concurrency."""
    metrics_mod._clear_metrics_cache_for_tests()
    tickers = ["AAPL", "MSFT", "NVDA", "META", "AMZN", "0700.HK"]
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(
            coverage_db.list_coverage_rows,
            return_value=[
                {"user_id": "u1", "ticker": sym, "tier": "core", "is_private": False}
                for sym in tickers
            ],
        ),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(coverage_db.get_earnings_calendar_mirror, return_value=None),
    )

    release_warmups = asyncio.Event()
    daily_started: dict[str, int] = {}
    mcap_started: dict[str, int] = {}
    active_mcap = 0
    max_active_mcap = 0
    snap_batches: list[list[str]] = []

    mkt_prov = create_autospec(MarketDataProvider, instance=True)

    async def _snaps(symbols: list[str], asset_type: str = "stocks", user_id: str | None = None) -> list[dict[str, Any]]:
        _ = (asset_type, user_id)
        snap_batches.append(list(symbols))
        return [{"symbol": s, "price": 100.0, "previous_close": 99.0} for s in symbols]

    async def _daily(symbol: str, from_date: str | None = None, to_date: str | None = None, is_index: bool = False, user_id: str | None = None) -> list[dict[str, Any]]:
        _ = (from_date, to_date, is_index, user_id)
        daily_started[symbol] = daily_started.get(symbol, 0) + 1
        await release_warmups.wait()
        return [
            {"date": (date(2026, 1, 2) + timedelta(days=i)).isoformat(), "close": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "volume": 1000}
            for i in range(40)
        ]

    mkt_prov.get_snapshots.side_effect = _snaps
    mkt_prov.get_daily.side_effect = _daily

    fin_source = create_autospec(FMPFinancialSource, instance=True)

    async def _profile(symbol: str) -> list[dict[str, Any]]:
        nonlocal active_mcap, max_active_mcap
        mcap_started[symbol] = mcap_started.get(symbol, 0) + 1
        active_mcap += 1
        max_active_mcap = max(max_active_mcap, active_mcap)
        try:
            await release_warmups.wait()
        finally:
            active_mcap -= 1
        return [{"marketCap": 1_000_000_000_000, "currency": "HKD" if symbol.endswith(".HK") else "USD"}]

    fin_source.get_company_profile.side_effect = _profile
    fin_prov = FinancialDataProvider(financial=fin_source)

    monkeypatch.setattr(
        metrics_mod,
        "_resolve_provider_and_key",
        create_autospec(metrics_mod._resolve_provider_and_key, return_value=(mkt_prov, "yfinance", True)),
    )
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_financial_provider",
        create_autospec(metrics_mod._resolve_financial_provider, return_value=fin_prov),
    )

    svc = metrics_mod.WatchlistMetricsService()
    # 1. Initial full call returns snapshots concurrently per symbol and marks all pending
    res1 = await svc.get_metrics_for_user("u1", symbols=tickers, wait_for_cold=False)
    assert set(res1["pending_symbols"]) == set(tickers)
    assert len(snap_batches) == len(tickers)
    assert all(len(batch) == 1 for batch in snap_batches)
    await asyncio.sleep(0.02)

    # Bounded market-cap concurrency <= MAX_MCAP_CONCURRENCY (4)
    assert max_active_mcap <= metrics_mod.MAX_MCAP_CONCURRENCY

    # 2. Repeated subset poll while warmups are still in flight:
    #    - only returns requested subset
    #    - reuses 15s snapshot cache (no new get_snapshots call)
    #    - does NOT spawn duplicate bars or market_cap warmup tasks
    snap_count_before = len(snap_batches)
    res_poll = await svc.get_metrics_for_user("u1", symbols=["0700.HK"], wait_for_cold=False)
    assert list(res_poll["items"].keys()) == ["0700.HK"]
    assert res_poll["pending_symbols"] == ["0700.HK"]
    assert len(snap_batches) == snap_count_before

    # Release all in-flight warmups and wait for completion
    release_warmups.set()
    await asyncio.sleep(0.05)

    # Every symbol ran get_daily and get_company_profile AT MOST ONCE (no duplicate warmup)
    assert all(cnt == 1 for cnt in daily_started.values())
    assert all(cnt == 1 for cnt in mcap_started.values())

    # 3. Subset poll after warmups complete returns full metrics & empty pending_symbols
    res_done = await svc.get_metrics_for_user("u1", symbols=["0700.HK"], wait_for_cold=False)
    assert res_done["pending_symbols"] == []
    assert res_done["items"]["0700.HK"]["market_cap"] == 1_000_000_000_000.0
    assert res_done["items"]["0700.HK"]["currency"] == "HKD"
    assert res_done["items"]["0700.HK"]["rsi_14d"] is not None


def test_r3_indicators_emit_price_source_and_price_ts() -> None:
    """Indicators explicitly label price_source ('snapshot' vs 'daily_close') and normalized epoch-ms price_ts."""
    bars = [
        {"date": "2026-05-14", "close": 100.0, "high": 102.0, "low": 98.0, "volume": 1000},
        {"date": "2026-05-15", "close": 105.0, "high": 106.0, "low": 101.0, "volume": 1200},
    ]
    snap_out = ind_mod.compute_symbol_metrics(
        bars,
        symbol="NVDA",
        latest_price=112.5,
        quote_timestamp="2026-05-15T18:30:00Z",
        now_utc=datetime(2026, 5, 15, 18, 30, tzinfo=timezone.utc),
    )
    assert snap_out["price"] == 112.5
    assert snap_out["price_source"] == "snapshot"
    assert snap_out["price_ts"] == int(datetime(2026, 5, 15, 18, 30, tzinfo=timezone.utc).timestamp() * 1000)

    daily_out = ind_mod.compute_symbol_metrics(
        bars,
        symbol="NVDA",
        latest_price=None,
        now_utc=datetime(2026, 5, 15, 18, 30, tzinfo=timezone.utc),
    )
    assert daily_out["price"] == 105.0
    assert daily_out["price_source"] == "daily_close"
    assert isinstance(daily_out["price_ts"], int) and daily_out["price_ts"] > 0

    empty_out = ind_mod.compute_symbol_metrics([], symbol="NVDA", latest_price=None)
    assert empty_out["price"] is None
    assert empty_out["price_source"] is None
    assert empty_out["price_ts"] is None


@pytest.mark.asyncio
async def test_r3_snapshot_per_symbol_timeout_isolation_and_pending_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One slow snapshot times out in isolation without discarding peers, enters snapshot_pending/pending_symbols even when bars+mcap are cached, is not cached, and retries alone."""
    metrics_mod._clear_metrics_cache_for_tests()
    monkeypatch.setattr(metrics_mod, "SNAPSHOT_SYMBOL_TIMEOUT_SECONDS", 0.05)

    tickers = ["AAPL", "MSFT", "0700.HK"]
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(
            coverage_db.list_coverage_rows,
            return_value=[
                {"user_id": "u1", "ticker": sym, "tier": "core", "is_private": False}
                for sym in tickers
            ],
        ),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(coverage_db.get_earnings_calendar_mirror, return_value=None),
    )

    # Pre-warm BOTH daily bars and market cap caches so ONLY snapshot timeout can put 0700.HK in pending_symbols
    warm_bars = [
        {"date": (date(2026, 1, 2) + timedelta(days=i)).isoformat(), "close": 380.0 + i, "high": 385.0 + i, "low": 375.0 + i, "volume": 1000}
        for i in range(40)
    ]
    for sym in tickers:
        key = metrics_mod._bars_cache_key(sym, provider_name="yfinance", split_adjusted=True)
        await metrics_mod._write_bars_to_cache(key, warm_bars)
        metrics_mod._MARKET_CAP_CACHE[sym] = (
            metrics_mod.time.monotonic(),
            2_000_000_000_000.0,
            "HKD" if sym.endswith(".HK") else "USD",
        )

    hk_attempts = {"n": 0}
    requested_batches: list[list[str]] = []
    mkt_prov = create_autospec(MarketDataProvider, instance=True)

    async def _snaps(symbols: list[str], asset_type: str = "stocks", user_id: str | None = None) -> list[dict[str, Any]]:
        _ = (asset_type, user_id)
        requested_batches.append(list(symbols))
        assert len(symbols) == 1
        sym = symbols[0]
        if sym == "0700.HK":
            hk_attempts["n"] += 1
            if hk_attempts["n"] == 1:
                await asyncio.sleep(0.25)  # exceeds per-symbol 0.05s timeout
            return [{"symbol": "0700.HK", "price": 432.5, "previous_close": 420.0, "currency": "HKD", "as_of": 1_764_151_200_000}]
        return [
            {
                "symbol": sym,
                "price": 210.0 if sym == "AAPL" else 440.0,
                "previous_close": 200.0 if sym == "AAPL" else 435.0,
                "currency": "USD",
                "as_of": 1_764_151_200_000,
            }
        ]

    mkt_prov.get_snapshots.side_effect = _snaps
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_provider_and_key",
        create_autospec(metrics_mod._resolve_provider_and_key, return_value=(mkt_prov, "yfinance", True)),
    )
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_financial_provider",
        create_autospec(
            metrics_mod._resolve_financial_provider,
            return_value=FinancialDataProvider(financial=None),
        ),
    )

    svc = metrics_mod.WatchlistMetricsService()
    res1 = await svc.get_metrics_for_user("u1", symbols=tickers, wait_for_cold=False)

    # AAPL and MSFT succeeded normally despite 0700.HK timing out
    assert res1["items"]["AAPL"]["price"] == 210.0
    assert res1["items"]["AAPL"]["price_source"] == "snapshot"
    assert res1["items"]["AAPL"]["price_ts"] == 1_764_151_200_000
    assert res1["items"]["AAPL"]["snapshot_pending"] is False
    assert res1["items"]["MSFT"]["price"] == 440.0
    assert res1["items"]["MSFT"]["price_source"] == "snapshot"
    assert res1["items"]["MSFT"]["snapshot_pending"] is False

    # 0700.HK fell back to daily_close, entered snapshot_pending + pending_symbols, and was NOT cached
    assert res1["items"]["0700.HK"]["price_source"] == "daily_close"
    assert res1["items"]["0700.HK"]["snapshot_pending"] is True
    assert res1["snapshot_pending_symbols"] == ["0700.HK"]
    assert res1["pending_symbols"] == ["0700.HK"]
    assert "0700.HK" not in metrics_mod._SNAPSHOT_CACHE
    assert "AAPL" in metrics_mod._SNAPSHOT_CACHE
    assert "MSFT" in metrics_mod._SNAPSHOT_CACHE

    # Subset poll retries ONLY 0700.HK and recovers live snapshot without re-fetching AAPL/MSFT
    requested_batches.clear()
    res2 = await svc.get_metrics_for_user("u1", symbols=["0700.HK"], wait_for_cold=False)
    assert requested_batches == [["0700.HK"]]
    assert res2["items"]["0700.HK"]["price"] == 432.5
    assert res2["items"]["0700.HK"]["price_source"] == "snapshot"
    assert res2["items"]["0700.HK"]["price_ts"] == 1_764_151_200_000
    assert res2["items"]["0700.HK"]["snapshot_pending"] is False
    assert res2["pending_symbols"] == []
    assert res2["snapshot_pending_symbols"] == []
    assert "0700.HK" in metrics_mod._SNAPSHOT_CACHE


@pytest.mark.asyncio
async def test_r5_selected_price_trade_ts_ignores_ginlix_bid_ask_as_of_and_mismatched_last_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5: price_ts uses the selected price's own trade timestamp — never Ginlix bid/ask as_of or a mismatched last_trade."""
    metrics_mod._clear_metrics_cache_for_tests()
    T1 = 1_764_151_200_000
    T2 = 1_764_151_260_000
    T3 = 1_764_151_320_000

    # 1. Direct helper checks across provider row shapes
    # Ginlix: price == last_trade_price -> use last_trade_at (T1), NOT newer bid/ask as_of (T3)
    assert (
        metrics_mod._extract_selected_price_trade_ts(
            {
                "source": "ginlix-data",
                "price": 200.0,
                "last_trade_price": 200.0,
                "last_trade_at": T1,
                "as_of": T3,
            },
            200.0,
        )
        == T1
    )
    # Ginlix: price (earlier session.close) != last_trade_price (odd-lot/extended print) -> do NOT stamp with T2 or T3
    assert (
        metrics_mod._extract_selected_price_trade_ts(
            {
                "source": "ginlix-data",
                "price": 189.96,
                "last_trade_price": 190.50,
                "last_trade_at": T2,
                "as_of": T3,
            },
            189.96,
        )
        is None
    )
    # Ginlix: bid/ask quote only (last_trade_at is None, as_of is T3) -> None (cannot beat a real trade)
    assert (
        metrics_mod._extract_selected_price_trade_ts(
            {
                "source": "ginlix-data",
                "price": 189.96,
                "last_trade_at": None,
                "as_of": T3,
            },
            189.96,
        )
        is None
    )
    # yfinance extended-hours: price is regular_close, regular_as_of is T1, as_of is extended print T2 -> T1
    assert (
        metrics_mod._extract_selected_price_trade_ts(
            {
                "source": "yfinance",
                "price": 317.28,
                "regular_close": 317.28,
                "regular_as_of": T1,
                "as_of": T2,
                "regular_only": False,
            },
            317.28,
        )
        == T1
    )
    # FMP / regular provider: as_of IS trade print time
    assert (
        metrics_mod._extract_selected_price_trade_ts(
            {"source": "fmp", "price": 150.0, "as_of": T1},
            150.0,
        )
        == T1
    )

    # 2. End-to-end through WatchlistMetricsService
    tickers = ["NVDA", "AAPL", "MSFT"]
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(
            coverage_db.list_coverage_rows,
            return_value=[
                {"user_id": "u1", "ticker": sym, "tier": "core", "is_private": False}
                for sym in tickers
            ],
        ),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(coverage_db.get_earnings_calendar_mirror, return_value=None),
    )
    mkt_prov = create_autospec(MarketDataProvider, instance=True)

    async def _snaps(symbols: list[str], asset_type: str = "stocks", user_id: str | None = None) -> list[dict[str, Any]]:
        _ = (asset_type, user_id)
        sym = symbols[0]
        if sym == "NVDA":
            return [
                {
                    "symbol": "NVDA",
                    "source": "ginlix-data",
                    "price": 140.0,
                    "last_trade_price": 140.0,
                    "last_trade_at": T1,
                    "as_of": T3,
                    "previous_close": 135.0,
                    "currency": "USD",
                }
            ]
        if sym == "AAPL":
            return [
                {
                    "symbol": "AAPL",
                    "source": "ginlix-data",
                    "price": 200.0,
                    "last_trade_price": 201.5,
                    "last_trade_at": T2,
                    "as_of": T3,
                    "previous_close": 198.0,
                    "currency": "USD",
                }
            ]
        return [
            {
                "symbol": "MSFT",
                "source": "yfinance",
                "price": 410.0,
                "regular_close": 410.0,
                "regular_as_of": T1,
                "as_of": T3,
                "regular_only": False,
                "previous_close": 405.0,
                "currency": "USD",
            }
        ]

    mkt_prov.get_snapshots.side_effect = _snaps
    mkt_prov.get_daily.return_value = []
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_provider_and_key",
        create_autospec(metrics_mod._resolve_provider_and_key, return_value=(mkt_prov, "ginlix-data", True)),
    )
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_financial_provider",
        create_autospec(
            metrics_mod._resolve_financial_provider,
            return_value=FinancialDataProvider(financial=None),
        ),
    )

    out = await metrics_mod.WatchlistMetricsService().get_metrics_for_user("u1", symbols=tickers, wait_for_cold=True)
    assert out["items"]["NVDA"]["price_ts"] == T1
    assert out["items"]["AAPL"]["price_ts"] is None
    assert out["items"]["MSFT"]["price_ts"] == T1


@pytest.mark.asyncio
async def test_r5_per_source_timeout_reserves_fallback_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5: A hanging primary source times out within per-source budget and leaves budget for the fallback source to succeed."""
    metrics_mod._clear_metrics_cache_for_tests()
    monkeypatch.setattr(metrics_mod, "SNAPSHOT_SOURCE_TIMEOUT_SECONDS", 0.04)
    monkeypatch.setattr(metrics_mod, "SNAPSHOT_SYMBOL_TIMEOUT_SECONDS", 0.25)

    primary_src = create_autospec(MarketDataSource, instance=True)
    fallback_src = create_autospec(MarketDataSource, instance=True)
    primary_called = {"n": 0}
    fallback_called = {"n": 0}

    async def _slow_primary(symbols: list[str], asset_type: str = "stocks", user_id: str | None = None) -> list[dict[str, Any]]:
        _ = (symbols, asset_type, user_id)
        primary_called["n"] += 1
        await asyncio.sleep(0.50)
        return []

    async def _fast_fallback(symbols: list[str], asset_type: str = "stocks", user_id: str | None = None) -> list[dict[str, Any]]:
        _ = (asset_type, user_id)
        fallback_called["n"] += 1
        return [{"symbol": symbols[0], "price": 195.5, "previous_close": 190.0, "as_of": 1_764_151_200_000}]

    primary_src.get_snapshots.side_effect = _slow_primary
    fallback_src.get_snapshots.side_effect = _fast_fallback

    provider = MarketDataProvider(
        entries=[
            ProviderEntry(name="ginlix-data", source=primary_src, markets={"all"}),
            ProviderEntry(name="fmp", source=fallback_src, markets={"all"}),
        ]
    )

    t0 = metrics_mod.time.monotonic()
    snaps, pending = await metrics_mod._fetch_snapshots_per_symbol(
        ["NVDA"],
        user_id="u1",
        provider=provider,
    )
    elapsed = metrics_mod.time.monotonic() - t0

    assert primary_called["n"] == 1
    assert fallback_called["n"] == 1
    assert pending == set()
    assert snaps["NVDA"]["price"] == 195.5
    assert snaps["NVDA"]["source"] == "fmp"
    assert elapsed < 0.20


@pytest.mark.asyncio
async def test_r5_cold_300_watchlist_partial_slow_sources_first_response_warmup_and_shared_singleflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5: 300-symbol cold watchlist with partial slow sources returns fast symbols within first-response budget,
    starts bars/mcap warmups without waiting for slow snapshots, and reuses shared snapshot cache + singleflight."""
    metrics_mod._clear_metrics_cache_for_tests()
    monkeypatch.setattr(metrics_mod, "SNAPSHOT_FIRST_RESPONSE_BUDGET_SECONDS", 0.15)
    monkeypatch.setattr(metrics_mod, "SNAPSHOT_SLOW_HANDOFF_SECONDS", 0.03)
    monkeypatch.setattr(metrics_mod, "SNAPSHOT_SOURCE_TIMEOUT_SECONDS", 1.5)
    monkeypatch.setattr(metrics_mod, "SNAPSHOT_SYMBOL_TIMEOUT_SECONDS", 2.0)

    tickers = [f"SYM{i:03d}" for i in range(1, 301)]
    redis_warm = set(tickers[:10])  # SYM001..SYM010 already warm in shared QuoteCacheService Redis
    shared_inflight_sym = tickers[10]  # SYM011 already in-flight in QuoteCacheService._inflight
    slow_syms = set(tickers[11:41])  # 30 slow symbols at the front of cold misses (SYM012..SYM041)
    fast_syms = set(tickers[41:])  # 259 fast symbols behind them (SYM042..SYM300)

    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(
            coverage_db.list_coverage_rows,
            return_value=[
                {"user_id": "u1", "ticker": sym, "tier": "core", "is_private": False}
                for sym in tickers
            ],
        ),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(coverage_db.get_earnings_calendar_mirror, return_value=None),
    )

    # Mock shared Redis cache pre-populated with SYM001..SYM010 under QuoteCacheService keys
    redis_store: dict[str, Any] = {}
    for sym in redis_warm:
        _, qkey = metrics_mod._resolve_quote_cache_key(sym)
        assert qkey is not None
        redis_store[qkey] = {"symbol": sym, "price": 500.0, "previous_close": 490.0, "as_of": 1_764_151_200_000}

    fake_redis = create_autospec(RedisCacheClient, instance=True)
    fake_redis.enabled = True
    fake_redis.mget.side_effect = lambda keys: [redis_store.get(k) for k in keys]
    fake_redis.get.side_effect = lambda k: redis_store.get(k)

    async def _redis_set(k: str, v: Any, ttl: int | None = None) -> bool:
        _ = ttl
        redis_store[k] = v
        return True

    fake_redis.set.side_effect = _redis_set
    monkeypatch.setattr(
        metrics_mod,
        "_get_redis_cache_client",
        create_autospec(metrics_mod._get_redis_cache_client, return_value=fake_redis),
    )

    # Pre-register SYM011 in QuoteCacheService._inflight to prove shared singleflight reuse
    qcs = QuoteCacheService.get_instance()
    _, inflight_qkey = metrics_mod._resolve_quote_cache_key(shared_inflight_sym)
    assert inflight_qkey is not None
    shared_fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    qcs._inflight[inflight_qkey] = shared_fut

    async def _resolve_shared_fut() -> None:
        await asyncio.sleep(0.01)
        if not shared_fut.done():
            shared_fut.set_result(
                {"symbol": shared_inflight_sym, "price": 600.0, "previous_close": 590.0, "as_of": 1_764_151_200_000}
            )

    resolve_shared_task = asyncio.create_task(_resolve_shared_fut())

    snap_calls: dict[str, int] = {}
    slow_release = asyncio.Event()
    slow_finished = False
    bars_start_times: list[float] = []
    mcap_start_times: list[float] = []

    mkt_prov = create_autospec(MarketDataProvider, instance=True)

    async def _snaps(symbols: list[str], asset_type: str = "stocks", user_id: str | None = None) -> list[dict[str, Any]]:
        nonlocal slow_finished
        _ = (asset_type, user_id)
        sym = symbols[0]
        snap_calls[sym] = snap_calls.get(sym, 0) + 1
        if sym in slow_syms:
            await slow_release.wait()
            slow_finished = True
            return [{"symbol": sym, "price": 88.0, "previous_close": 85.0, "as_of": 1_764_151_260_000}]
        await asyncio.sleep(0.001)
        return [{"symbol": sym, "price": 120.0, "previous_close": 115.0, "as_of": 1_764_151_200_000}]

    warm_bars = [
        {"date": (date(2026, 1, 2) + timedelta(days=i)).isoformat(), "close": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "volume": 1000}
        for i in range(40)
    ]

    async def _daily(symbol: str, from_date: str | None = None, to_date: str | None = None, is_index: bool = False, user_id: str | None = None) -> list[dict[str, Any]]:
        _ = (symbol, from_date, to_date, is_index, user_id)
        bars_start_times.append(metrics_mod.time.monotonic())
        await asyncio.sleep(0.001)
        return warm_bars

    mkt_prov.get_snapshots.side_effect = _snaps
    mkt_prov.get_daily.side_effect = _daily

    fin_source = create_autospec(FMPFinancialSource, instance=True)

    async def _profile(symbol: str) -> list[dict[str, Any]]:
        _ = symbol
        mcap_start_times.append(metrics_mod.time.monotonic())
        await asyncio.sleep(0.001)
        return [{"marketCap": 2_000_000_000_000, "currency": "USD"}]

    fin_source.get_company_profile.side_effect = _profile
    fin_prov = FinancialDataProvider(financial=fin_source)

    monkeypatch.setattr(
        metrics_mod,
        "_resolve_provider_and_key",
        create_autospec(metrics_mod._resolve_provider_and_key, return_value=(mkt_prov, "ginlix-data", True)),
    )
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_financial_provider",
        create_autospec(metrics_mod._resolve_financial_provider, return_value=fin_prov),
    )

    try:
        svc = metrics_mod.WatchlistMetricsService()
        t0 = metrics_mod.time.monotonic()
        res1 = await svc.get_metrics_for_user("u1", symbols=tickers, wait_for_cold=False)
        first_elapsed = metrics_mod.time.monotonic() - t0

        # 1. First response returns while slow snapshots are STILL blocked (bounded by first-response budget,
        #    nowhere near the 2.0s symbol timeout or wave-blocked 30-symbol latency)
        assert slow_finished is False
        assert first_elapsed < 1.0
        assert len(res1["items"]) == 300

        # 2. Shared Redis hits (10) + QuoteCacheService inflight (1) made ZERO upstream calls
        for sym in redis_warm:
            assert snap_calls.get(sym, 0) == 0
            assert res1["items"][sym]["price"] == 500.0
            assert res1["items"][sym]["snapshot_pending"] is False
        assert snap_calls.get(shared_inflight_sym, 0) == 0
        assert res1["items"][shared_inflight_sym]["price"] == 600.0
        assert res1["items"][shared_inflight_sym]["snapshot_pending"] is False

        # 3. All 259 fast symbols completed in first response despite 30 slow symbols sitting ahead of them!
        ready_fast = [sym for sym in fast_syms if res1["items"][sym]["price_source"] == "snapshot"]
        assert len(ready_fast) == len(fast_syms)
        assert all(res1["items"][sym]["snapshot_pending"] is False for sym in fast_syms)

        # 4. Daily bars and market-cap warmups started BEFORE any slow snapshot finished,
        #    and fast symbols already have RSI & market cap populated in the first response!
        assert len(bars_start_times) > 0
        assert len(mcap_start_times) > 0
        assert slow_finished is False
        assert any(res1["items"][sym]["rsi_14d"] is not None for sym in fast_syms)
        assert any(res1["items"][sym]["market_cap"] is not None for sym in fast_syms)

        # 5. Slow symbols are reported in snapshot_pending_symbols while still running in background singleflight
        assert set(res1["snapshot_pending_symbols"]) == slow_syms
        assert set(slow_syms).issubset(set(res1["pending_symbols"]))

        # 6. Subset poll while slow snapshots are still in flight joins existing singleflight (NO duplicate calls)
        res_mid = await svc.get_metrics_for_user("u1", symbols=sorted(slow_syms), wait_for_cold=False)
        assert all(snap_calls.get(sym, 0) == 1 for sym in slow_syms)
        assert set(res_mid["items"].keys()) == slow_syms

        # 7. Release background slow snapshots -> subset poll serves them from cache with zero new upstream calls
        slow_release.set()
        inflight_tasks = [t for sym, t in list(metrics_mod._INFLIGHT_SNAPSHOTS.items()) if sym in slow_syms]
        if inflight_tasks:
            await asyncio.gather(*inflight_tasks)
        res_after = await svc.get_metrics_for_user("u1", symbols=sorted(slow_syms), wait_for_cold=False)
        assert res_after["snapshot_pending_symbols"] == []
        assert all(res_after["items"][sym]["price"] == 88.0 for sym in slow_syms)
        assert all(snap_calls.get(sym, 0) == 1 for sym in slow_syms)
        # Newly fetched snapshots populate local _SNAPSHOT_CACHE without writing shared Redis quote keys
        _, sample_qkey = metrics_mod._resolve_quote_cache_key("SYM042")
        assert sample_qkey not in redis_store
        assert "SYM042" in metrics_mod._SNAPSHOT_CACHE
    finally:
        slow_release.set()
        qcs._inflight.pop(inflight_qkey, None)
        if not resolve_shared_task.done():
            resolve_shared_task.cancel()
        metrics_mod._clear_metrics_cache_for_tests()


def test_r6_non_ginlix_with_null_last_trade_at_uses_as_of() -> None:
    """r6 item 1: SnapshotData serializes last_trade_at: None for every provider; only source=='ginlix-data' ignores as_of."""
    T2 = 1_764_151_260_000
    for src in ("fmp", "tushare", "yfinance"):
        assert (
            metrics_mod._extract_selected_price_trade_ts(
                {
                    "source": src,
                    "price": 152.5,
                    "last_trade_at": None,
                    "regular_as_of": None,
                    "as_of": T2,
                },
                152.5,
            )
            == T2
        )
    # Ginlix with last_trade_at=None still refuses bid/ask composite as_of
    assert (
        metrics_mod._extract_selected_price_trade_ts(
            {
                "source": "ginlix-data",
                "price": 152.5,
                "last_trade_at": None,
                "as_of": T2,
            },
            152.5,
        )
        is None
    )


@pytest.mark.asyncio
async def test_r6_fallback_snapshot_does_not_write_shared_quote_cache_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 item 4: /metrics fallback snapshots populate local _SNAPSHOT_CACHE only and never write shared QuoteCacheService keys."""
    metrics_mod._clear_metrics_cache_for_tests()
    monkeypatch.setattr(metrics_mod, "SNAPSHOT_SOURCE_TIMEOUT_SECONDS", 0.04)
    monkeypatch.setattr(metrics_mod, "SNAPSHOT_SYMBOL_TIMEOUT_SECONDS", 0.25)

    redis_store: dict[str, Any] = {}
    redis_writes: list[str] = []
    fake_redis = create_autospec(RedisCacheClient, instance=True)
    fake_redis.enabled = True
    fake_redis.mget.side_effect = lambda keys: [redis_store.get(k) for k in keys]
    fake_redis.get.side_effect = lambda k: redis_store.get(k)

    async def _redis_set(k: str, v: Any, ttl: int | None = None) -> bool:
        _ = ttl
        redis_writes.append(k)
        redis_store[k] = v
        return True

    fake_redis.set.side_effect = _redis_set
    monkeypatch.setattr(
        metrics_mod,
        "_get_redis_cache_client",
        create_autospec(metrics_mod._get_redis_cache_client, return_value=fake_redis),
    )

    primary_src = create_autospec(MarketDataSource, instance=True)
    fallback_src = create_autospec(MarketDataSource, instance=True)
    primary_src.get_snapshots.return_value = []
    fallback_src.get_snapshots.return_value = [
        {"symbol": "NVDA", "price": 195.5, "previous_close": 190.0, "as_of": 1_764_151_200_000}
    ]
    provider = MarketDataProvider(
        entries=[
            ProviderEntry(name="ginlix-data", source=primary_src, markets={"all"}),
            ProviderEntry(name="yfinance", source=fallback_src, markets={"all"}),
        ]
    )

    snaps, pending = await metrics_mod._fetch_snapshots_per_symbol(
        ["NVDA"],
        user_id="u1",
        provider=provider,
    )
    assert pending == set()
    assert snaps["NVDA"]["price"] == 195.5
    assert snaps["NVDA"]["source"] == "yfinance"
    assert "NVDA" in metrics_mod._SNAPSHOT_CACHE

    _, nvda_qkey = metrics_mod._resolve_quote_cache_key("NVDA")
    assert nvda_qkey is not None
    assert nvda_qkey not in redis_store
    assert nvda_qkey not in redis_writes


@pytest.mark.asyncio
async def test_r6_roster_fallback_earnings_filters_expired_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 item 5: Saved roster earnings.next_date fallback is adopted only when >= today; expired dates return None."""
    metrics_mod._clear_metrics_cache_for_tests()
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(
            coverage_db.list_coverage_rows,
            return_value=[
                {
                    "user_id": "u1",
                    "ticker": "NVDA",
                    "tier": "core",
                    "is_private": False,
                    "earnings": {"next_date": "2020-01-15"},
                },
                {
                    "user_id": "u1",
                    "ticker": "AAPL",
                    "tier": "core",
                    "is_private": False,
                    "earnings": {"next_date": "2099-05-01"},
                },
            ],
        ),
    )
    # Calendar mirror does not contain NVDA or AAPL
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(coverage_db.get_earnings_calendar_mirror, return_value={"ledger": {"entries": {}}}),
    )

    mkt_prov = create_autospec(MarketDataProvider, instance=True)
    mkt_prov.get_snapshots.side_effect = lambda symbols, asset_type="stocks", user_id=None: [
        {"symbol": symbols[0], "price": 150.0, "previous_close": 148.0, "currency": "USD"}
    ]
    mkt_prov.get_daily.return_value = []
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_provider_and_key",
        create_autospec(metrics_mod._resolve_provider_and_key, return_value=(mkt_prov, "fmp", True)),
    )
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_financial_provider",
        create_autospec(
            metrics_mod._resolve_financial_provider,
            return_value=FinancialDataProvider(financial=None),
        ),
    )

    out = await metrics_mod.WatchlistMetricsService().get_metrics_for_user("u1", wait_for_cold=True)
    assert out["items"]["NVDA"]["next_earnings"] is None
    assert out["items"]["AAPL"]["next_earnings"] == "2099-05-01"
    assert out["summary"]["upcoming_earnings"] == [{"ticker": "AAPL", "date": "2099-05-01"}]


@pytest.mark.asyncio
async def test_r6_last_source_uses_full_remaining_symbol_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 item 6: Single/last source (including no-entries chain) uses full remaining symbol budget instead of 3s cap, allowing a 5s response to succeed."""
    metrics_mod._clear_metrics_cache_for_tests()

    # 1. Production 3s / 8s constants: verify single source receives >5s timeout (full ~8s budget)
    captured_timeouts: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def _recording_wait_for(aw: Any, timeout: float | None = None) -> Any:
        captured_timeouts.append(timeout)
        return await real_wait_for(aw, timeout=timeout)

    monkeypatch.setattr(
        metrics_mod.asyncio,
        "wait_for",
        create_autospec(asyncio.wait_for, side_effect=_recording_wait_for),
    )

    yf_src = create_autospec(MarketDataSource, instance=True)
    yf_src.get_snapshots.return_value = [
        {"symbol": "AAPL", "price": 210.0, "previous_close": 205.0, "as_of": 1_764_151_200_000}
    ]
    single_entry_provider = MarketDataProvider(
        entries=[ProviderEntry(name="yfinance", source=yf_src, markets={"all"})]
    )
    snap_single = await metrics_mod._fetch_symbol_snapshot_with_fallback(
        single_entry_provider, "AAPL", user_id="u1"
    )
    assert snap_single is not None and snap_single["price"] == 210.0
    assert len(captured_timeouts) == 1
    assert captured_timeouts[0] is not None and captured_timeouts[0] > 5.0

    # Also verify no-entries whole-chain branch gets full remaining budget (>5s)
    captured_timeouts.clear()
    plain_provider = create_autospec(MarketDataProvider, instance=True)
    plain_provider.get_snapshots.return_value = [
        {"symbol": "MSFT", "price": 420.0, "previous_close": 415.0, "as_of": 1_764_151_200_000}
    ]
    snap_plain = await metrics_mod._fetch_symbol_snapshot_with_fallback(
        plain_provider, "MSFT", user_id="u1"
    )
    assert snap_plain is not None and snap_plain["price"] == 420.0
    assert len(captured_timeouts) == 1
    assert captured_timeouts[0] is not None and captured_timeouts[0] > 5.0

    # 2. Real async sleep exceeding SNAPSHOT_SOURCE_TIMEOUT_SECONDS (5s/3s scaled to 0.08s/0.04s inside 0.25s symbol budget)
    monkeypatch.setattr(metrics_mod.asyncio, "wait_for", real_wait_for)
    monkeypatch.setattr(metrics_mod, "SNAPSHOT_SOURCE_TIMEOUT_SECONDS", 0.04)
    monkeypatch.setattr(metrics_mod, "SNAPSHOT_SYMBOL_TIMEOUT_SECONDS", 0.25)

    slow_single_src = create_autospec(MarketDataSource, instance=True)

    async def _slow_5s_equiv(
        symbols: list[str], asset_type: str = "stocks", user_id: str | None = None
    ) -> list[dict[str, Any]]:
        _ = (asset_type, user_id)
        await asyncio.sleep(0.08)  # exceeds 0.04s per-source cap, fits in 0.25s symbol budget
        return [{"symbol": symbols[0], "price": 215.5, "previous_close": 210.0, "as_of": 1_764_151_200_000}]

    slow_single_src.get_snapshots.side_effect = _slow_5s_equiv
    slow_single_provider = MarketDataProvider(
        entries=[ProviderEntry(name="yfinance", source=slow_single_src, markets={"all"})]
    )
    snaps_out, pending_out = await metrics_mod._fetch_snapshots_per_symbol(
        ["AAPL"], user_id="u1", provider=slow_single_provider
    )
    assert pending_out == set()
    assert snaps_out["AAPL"]["price"] == 215.5
    assert snaps_out["AAPL"]["source"] == "yfinance"


def test_r7_snapshot_data_preserves_regular_only_for_yahoo_premarket_and_fallback() -> None:
    """r7 item 1 & 2: REST SnapshotData preserves regular_only for both Yahoo pre-market enriched and fallback rows."""
    from datetime import datetime, timezone
    import yfinance as yf

    from src.dalton.extended_hours import enrich_yfinance_snapshot_extended_hours
    from src.server.models.market_data import SnapshotData

    # 2026-10-21 16:00 ET = 1792612800; 2026-10-22 08:31 ET = 1792672260.
    reg_close_sec = 1792612800
    pre_trade_sec = 1792672260
    pre_now = datetime.fromtimestamp(pre_trade_sec, tz=timezone.utc)
    base_yahoo_snap = {
        "symbol": "GOOG",
        "source": "yfinance",
        "price": 100.0,
        "previous_close": 98.0,
        "change": 2.0,
        "change_percent": 2.0408,
        "market_status": None,
        "early_trading_change_percent": None,
        "late_trading_change_percent": None,
        "as_of": reg_close_sec * 1000,
        "regular_only": True,
    }

    # 1. Yahoo pre-market enriched row serialized through REST SnapshotData keeps regular_only=False
    #    so price (yesterday's close 100) is timestamped by regular_as_of, never 8:31 pre-market as_of.
    pm_ticker = create_autospec(yf.Ticker, instance=True)
    pm_ticker.info = {
        "marketState": "PRE",
        "preMarketPrice": 101.0,
        "preMarketTime": pre_trade_sec,
        "regularMarketTime": reg_close_sec,
    }
    enriched = enrich_yfinance_snapshot_extended_hours(
        base_yahoo_snap, "GOOG", pm_ticker, now=pre_now
    )
    serialized_pm = SnapshotData(**enriched).model_dump()
    assert serialized_pm["regular_only"] is False
    assert serialized_pm["price"] == 100.0
    assert serialized_pm["regular_close"] == 100.0
    assert serialized_pm["as_of"] == pre_trade_sec * 1000
    assert serialized_pm["regular_as_of"] == reg_close_sec * 1000
    assert (
        metrics_mod._extract_selected_price_trade_ts(serialized_pm, serialized_pm["price"])
        == reg_close_sec * 1000
    )

    # 2. Core-install Yahoo pre-market fallback (no pre-market print yet) serialized through SnapshotData
    #    keeps regular_only=True, price=100 (yesterday close), regular_close=100, previous_close=98.
    fallback_ticker = create_autospec(yf.Ticker, instance=True)
    fallback_ticker.info = {
        "marketState": "PRE",
        "preMarketPrice": None,
        "preMarketTime": None,
        "regularMarketTime": reg_close_sec,
    }
    fallback = enrich_yfinance_snapshot_extended_hours(
        base_yahoo_snap, "GOOG", fallback_ticker, now=pre_now
    )
    serialized_fb = SnapshotData(**fallback).model_dump()
    assert serialized_fb["regular_only"] is True
    assert serialized_fb["price"] == 100.0
    assert serialized_fb["regular_close"] == 100.0
    assert serialized_fb["previous_close"] == 98.0
    assert serialized_fb["regular_trading_change"] == 2.0
    assert serialized_fb["regular_trading_change_percent"] == 2.0408
    assert serialized_fb["early_trading_change_percent"] is None
    assert serialized_fb["regular_as_of"] == reg_close_sec * 1000
