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
from src.data_client.financial_data_provider import FinancialDataProvider
from src.data_client.fmp.financial_source import FMPFinancialSource
from src.data_client.market_data_provider import MarketDataProvider
from src.server.models.additional_context import AdditionalContext, WatchlistContext


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
    # 1. Initial full call returns snapshots immediately (grouped by US & HK buckets) and marks all pending
    res1 = await svc.get_metrics_for_user("u1", symbols=tickers, wait_for_cold=False)
    assert set(res1["pending_symbols"]) == set(tickers)
    assert len(snap_batches) == 2  # US chunk + HK chunk fetched concurrently
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
