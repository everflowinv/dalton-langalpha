"""Unit tests for WatchlistConsensusAdapter under Standalone Core (§6.3).

Covers US equities, HK cross-currency equities, verified ADRs, unverified ADRs,
FMP annual fiscal-year matching fallback, all 7 valuation cell states, and
Standalone Core acceptance with zero private service calls.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.capabilities import CapabilityId, has_capability
from src.dalton.database import coverage as coverage_db
from src.dalton.watchlist import consensus as consensus_mod
from src.dalton.watchlist import metrics as metrics_mod
from src.data_client.fmp.fmp_client import FMPClient, FMPRequestError, FmpQuotaExhausted


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    consensus_mod._clear_consensus_cache_for_tests()
    metrics_mod._clear_metrics_cache_for_tests()
    # Force Standalone Core profile with all private capabilities disabled
    monkeypatch.setenv("APP_ENV", "core")
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.delenv("FMP_API_KEY", raising=False)


def test_standalone_core_has_no_researchdata_bridge_capability() -> None:
    assert has_capability(CapabilityId.RESEARCHDATA_BRIDGE) is False


def test_yfinance_trend_meta_extracts_both_1_7_list_and_1_8_dict_formats() -> None:
    class _FakeTicker17:
        _analysis = type(
            "_A",
            (),
            {
                "_earnings_trend": [
                    {"period": "0y", "endDate": "2027-01-31", "earningsEstimate": {"currency": "USD"}},
                    {"period": "+1y", "endDate": "2028-01-31"},
                ]
            },
        )()

    ends_17, ccy_17 = consensus_mod._extract_yfinance_trend_meta(_FakeTicker17())
    assert ends_17 == {"0y": "2027-01-31", "+1y": "2028-01-31"}
    assert ccy_17 == "USD"

    class _FakeTicker18:
        _analysis = type(
            "_A",
            (),
            {
                "_earnings_trend": {
                    None: {
                        "earningsCurrency": "CNY",
                        "trend": [
                            {"period": "0y", "endDate": "2026-12-31"},
                            {"period": "+1y", "endDate": "2027-12-31"},
                        ],
                    }
                }
            },
        )()

    ends_18, ccy_18 = consensus_mod._extract_yfinance_trend_meta(_FakeTicker18())
    assert ends_18 == {"0y": "2026-12-31", "+1y": "2027-12-31"}
    assert ccy_18 == "CNY"


def test_fmp_fiscal_year_end_matching_selects_fy1_and_fy2() -> None:
    estimates = [
        {"date": "2025-12-31", "epsAvg": 2.10, "numAnalystsEps": 30},
        {"date": "2028-12-31", "epsAvg": 4.80, "numAnalystsEps": 20},
        {"date": "2026-12-31", "epsAvg": 3.00, "numAnalystsEps": 42},
        {"date": "2027-12-31", "epsAvg": 3.90, "numAnalystsEps": 38},
    ]
    fy1, fy2 = consensus_mod.select_fmp_fy1_fy2_estimates(
        estimates,
        today=date(2026, 6, 15),
        last_reported_fy_end="2025-12-31",
    )
    assert fy1 is not None and fy1["date"] == "2026-12-31"
    assert fy2 is not None and fy2["date"] == "2027-12-31"

    # If 2026-12-31 was already reported, FY1 rolls to 2027-12-31 and FY2 to 2028-12-31
    fy1_r, fy2_r = consensus_mod.select_fmp_fy1_fy2_estimates(
        estimates,
        today=date(2026, 6, 15),
        last_reported_fy_end="2026-12-31",
    )
    assert fy1_r is not None and fy1_r["date"] == "2027-12-31"
    assert fy2_r is not None and fy2_r["date"] == "2028-12-31"


@pytest.mark.asyncio
async def test_consensus_us_hk_verified_adr_unverified_adr_and_all_7_states() -> None:
    """Verify US, HK cross-currency, verified ADR, unverified ADR, loss, no_coverage, stale, rate_limited, and unlisted."""
    records: dict[str, consensus_mod.RawConsensusRecord | None] = {
        # 1. US equity: NVDA (same currency USD, non-ADR) -> ok
        "NVDA": consensus_mod.RawConsensusRecord(
            symbol="NVDA",
            fy1_eps=4.50,
            fy2_eps=5.85,
            fy1_end="2027-01-31",
            fy2_end="2028-01-31",
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=48,
            provider="yfinance",
            as_of="2026-11-26T12:00:00Z",
            eps_per_quote_unit_confirmed=True,
        ),
        # 2. HK equity: 0700.HK (HKD quote, CNY EPS, 1:1 non-ADR -> FX convert CNY->HKD = 1.08) -> ok
        "0700.HK": consensus_mod.RawConsensusRecord(
            symbol="0700.HK",
            fy1_eps=25.0,
            fy2_eps=28.0,
            fy1_end="2026-12-31",
            fy2_end="2027-12-31",
            estimate_currency="CNY",
            quote_currency="HKD",
            analyst_count=35,
            provider="yfinance",
            as_of="2026-11-26T12:00:00Z",
        ),
        # 3. Verified ADR from curated table: TSM (5 ordinary TWD shares per ADR, TWD->USD = 0.031) -> ok
        "TSM": consensus_mod.RawConsensusRecord(
            symbol="TSM",
            fy1_eps=50.0,
            fy2_eps=60.0,
            fy1_end="2026-12-31",
            fy2_end="2027-12-31",
            estimate_currency="TWD",
            quote_currency="USD",
            analyst_count=22,
            provider="yfinance",
            as_of="2026-11-26T12:00:00Z",
            quote_type="EQUITY",
            security_name="Taiwan Semiconductor Manufacturing Company Limited ADR",
        ),
        # 4. Unverified ADR (not in curated table, foreign EPS, no row adr_ratio) -> unit_unverified!
        "XYZADR": consensus_mod.RawConsensusRecord(
            symbol="XYZADR",
            fy1_eps=10.0,
            fy2_eps=12.0,
            fy1_end="2026-12-31",
            fy2_end="2027-12-31",
            estimate_currency="JPY",
            quote_currency="USD",
            analyst_count=5,
            provider="yfinance",
            as_of="2026-11-26T12:00:00Z",
            quote_type="EQUITY",
            security_name="Unknown Foreign Corp Sponsored ADR",
        ),
        # 5. Loss-making company (FY1 EPS <= 0) -> loss (NM)
        "LOSS": consensus_mod.RawConsensusRecord(
            symbol="LOSS",
            fy1_eps=-1.25,
            fy2_eps=-0.40,
            fy1_end="2026-12-31",
            fy2_end="2027-12-31",
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=12,
            provider="yfinance",
            as_of="2026-11-26T12:00:00Z",
        ),
        # 6. No analyst coverage (small-cap HK) -> no_coverage
        "9999.HK": consensus_mod.RawConsensusRecord(
            symbol="9999.HK",
            fy1_eps=None,
            fy2_eps=None,
            quote_currency="HKD",
            provider="yfinance",
            as_of="2026-11-26T12:00:00Z",
        ),
    }

    async def _fake_yf(symbol: str) -> consensus_mod.RawConsensusRecord | None:
        if symbol == "LIMIT429":
            raise consensus_mod.ConsensusRateLimitError("HTTP 429 Too Many Requests")
        if symbol == "STALECO":
            raise consensus_mod.ConsensusRateLimitError("HTTP 429 Too Many Requests")
        return records.get(symbol)

    async def _fake_fx(from_ccy: str, to_ccy: str) -> float | None:
        return {("CNY", "HKD"): 1.08, ("TWD", "USD"): 0.031, ("JPY", "USD"): 0.0065}.get((from_ccy, to_ccy))

    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_fake_yf,
        fx_fetcher=_fake_fx,
    )

    # 1. US: NVDA @ $135 -> FY1 P/E = 30.0, FY2 P/E = 23.08, FY2 EPS growth = +30.0%
    nvda = await adapter.get_valuation("NVDA", latest_price=135.0, quote_currency="USD")
    assert nvda["status"] == "ok"
    assert nvda["unit_verified"] is True
    assert nvda["pe_fy1"] == 30.0
    assert nvda["pe_fy2"] == 23.08
    assert nvda["eps_growth_fy2"] == 30.0

    # 2. HK cross-currency: 0700.HK @ HK$432, FY1 EPS 25 CNY * 1.08 = 27 HKD -> P/E = 16.0
    hk = await adapter.get_valuation("0700.HK", latest_price=432.0, quote_currency="HKD")
    assert hk["status"] == "ok"
    assert hk["unit_verified"] is True
    assert hk["shares_per_quote_unit"] == 1.0
    assert hk["pe_fy1"] == 16.0
    assert hk["eps_growth_fy2"] == 12.0

    # 3. Verified ADR: TSM @ $155, FY1 EPS 50 TWD * 5 shares/ADR * 0.031 = $7.75 -> P/E = 20.0
    tsm = await adapter.get_valuation("TSM", latest_price=155.0, quote_currency="USD")
    assert tsm["status"] == "ok"
    assert tsm["unit_verified"] is True
    assert tsm["shares_per_quote_unit"] == 5.0
    assert tsm["pe_fy1"] == 20.0
    assert tsm["eps_growth_fy2"] == 20.0

    # 4. Unverified ADR: XYZADR -> unit_unverified, NEVER assumes 1:1
    unverified = await adapter.get_valuation("XYZADR", latest_price=26.0, quote_currency="USD")
    assert unverified["status"] == "unit_unverified"
    assert unverified["unit_verified"] is False
    assert unverified["pe_fy1"] is None
    assert unverified["pe_fy2"] is None
    assert unverified["eps_growth_fy2"] is None

    # 4b. Same ADR WITH explicit verified `aliases.adr_ratio = 2.0` -> ok! (10 JPY * 2 * 0.0065 = $0.13 -> P/E = 200.0)
    verified_override = await adapter.get_valuation(
        "XYZADR",
        latest_price=26.0,
        quote_currency="USD",
        coverage_row={"aliases": {"adr_ratio": 2.0}},
    )
    assert verified_override["status"] == "ok"
    assert verified_override["unit_verified"] is True
    assert verified_override["shares_per_quote_unit"] == 2.0
    assert verified_override["pe_fy1"] == 200.0

    # 5. Loss (EPS <= 0) -> loss (NM)
    loss = await adapter.get_valuation("LOSS", latest_price=25.0, quote_currency="USD")
    assert loss["status"] == "loss"
    assert loss["pe_fy1"] is None
    assert loss["pe_fy1_status"] == "loss"
    assert loss["eps_growth_fy2"] is None
    assert loss["eps_growth_fy2_status"] == "loss"

    # 6. No coverage -> no_coverage
    nocov = await adapter.get_valuation("9999.HK", latest_price=10.0, quote_currency="HKD")
    assert nocov["status"] == "no_coverage"
    assert nocov["pe_fy1"] is None

    # 7. Rate limited (no cache) -> rate_limited
    rl = await adapter.get_valuation("LIMIT429", latest_price=50.0, quote_currency="USD")
    assert rl["status"] == "rate_limited"
    assert rl["pe_fy1"] is None

    # 8. Stale fallback when cache expired & upstream 429s -> stale (preserves cached PE!)
    stale_key = consensus_mod._consensus_cache_key("STALECO")
    stale_rec = consensus_mod.RawConsensusRecord(
        symbol="STALECO",
        fy1_eps=5.0,
        fy2_eps=6.0,
        fy1_end="2026-12-31",
        fy2_end="2027-12-31",
        estimate_currency="USD",
        quote_currency="USD",
        analyst_count=15,
        provider="yfinance",
        as_of="2026-11-20T00:00:00Z",
    ).to_dict()
    consensus_mod._CONSENSUS_CACHE[stale_key] = (
        time.time() - consensus_mod.CONSENSUS_CACHE_TTL_SECONDS - 600,
        stale_rec,
    )
    stale = await adapter.get_valuation("STALECO", latest_price=100.0, quote_currency="USD")
    assert stale["status"] == "stale"
    assert stale["pe_fy1"] == 20.0
    assert stale["pe_fy1_status"] == "stale"

    # 9. Unlisted private company -> unlisted
    priv = await adapter.get_valuation("OPENAI", latest_price=None, is_private=True)
    assert priv["status"] == "unlisted"
    assert priv["pe_fy1"] is None


@pytest.mark.asyncio
async def test_fmp_fallback_used_when_yfinance_missing() -> None:
    """When yfinance returns no estimates, optional FMP annual estimates match by fiscal year end."""
    fmp_mock = create_autospec(FMPClient, instance=True)
    fmp_mock.get_analyst_estimates.return_value = [
        {"date": "2027-12-31", "epsAvg": 8.0, "numAnalystsEps": 25},
        {"date": "2028-12-31", "epsAvg": 10.0, "numAnalystsEps": 22},
    ]
    fmp_mock.get_income_statement.return_value = [{"date": "2026-12-31"}]
    fmp_mock.get_profile.return_value = [
        {"symbol": "MSFT", "currency": "USD", "reportedCurrency": "USD", "companyName": "Test Corp"}
    ]

    async def _empty_yf(_symbol: str) -> consensus_mod.RawConsensusRecord:
        return consensus_mod.RawConsensusRecord(symbol="MSFT", quote_currency="USD", provider="yfinance")

    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_empty_yf,
        fmp_client=fmp_mock,
        today_fn=lambda: date(2027, 2, 1),
    )
    res = await adapter.get_valuation("MSFT", latest_price=240.0, quote_currency="USD")
    assert res["status"] == "ok"
    assert res["provider"] == "fmp"
    assert res["pe_fy1"] == 30.0
    assert res["pe_fy2"] == 24.0
    assert res["eps_growth_fy2"] == 25.0
    assert res["fy1_end"] == "2027-12-31"
    assert res["fy2_end"] == "2028-12-31"


@pytest.mark.asyncio
async def test_adr_ratios_tm_10_metadata_and_currency_match_not_per_adr_proof() -> None:
    """TM is 10 ordinary shares/ADS with filing source+date; currency match alone NEVER proves per-ADR EPS."""
    # 1. Every entry in VERIFIED_ADR_SHARES_PER_UNIT has source and ISO effective_date; TM == 10.0
    assert consensus_mod.VERIFIED_ADR_SHARES_PER_UNIT["TM"].shares_per_adr == 10.0
    assert consensus_mod.VERIFIED_ADR_SHARES_PER_UNIT["TM"].underlying_currency == "JPY"
    for sym, entry in consensus_mod.VERIFIED_ADR_SHARES_PER_UNIT.items():
        assert isinstance(entry, consensus_mod.VerifiedAdrRatio), sym
        assert entry.shares_per_adr > 0
        assert entry.source.strip()
        assert len(entry.effective_date) == 10 and date.fromisoformat(entry.effective_date)

    # 2. TM @ $180, ordinary-share EPS = 250 JPY, FX JPY->USD = 0.006 -> per-ADR EPS = 250 * 10 * 0.006 = $15 -> P/E = 12.0
    records = {
        "TM": consensus_mod.RawConsensusRecord(
            symbol="TM",
            fy1_eps=250.0,
            fy2_eps=300.0,
            fy1_end="2027-03-31",
            fy2_end="2028-03-31",
            estimate_currency="JPY",
            quote_currency="USD",
            analyst_count=18,
            provider="yfinance",
            security_name="Toyota Motor Corporation ADR",
        ),
        # Unverified ADR where estimate_currency == quote_currency == USD, but provider did NOT confirm per-ADR!
        "USDADR": consensus_mod.RawConsensusRecord(
            symbol="USDADR",
            fy1_eps=5.0,
            fy2_eps=6.0,
            fy1_end="2026-12-31",
            fy2_end="2027-12-31",
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=9,
            provider="yfinance",
            security_name="Mystery Corp Sponsored ADR",
            eps_per_quote_unit_confirmed=False,
        ),
        # TSM (5 TWD shares/ADR) returning USD EPS without explicit per-ADR confirmation -> unit_unverified!
        "TSM": consensus_mod.RawConsensusRecord(
            symbol="TSM",
            fy1_eps=10.0,
            fy2_eps=12.0,
            fy1_end="2026-12-31",
            fy2_end="2027-12-31",
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=20,
            provider="yfinance",
            security_name="Taiwan Semiconductor ADR",
            eps_per_quote_unit_confirmed=False,
        ),
    }

    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=lambda s: records.get(s),
        fx_fetcher=lambda src, dst: 0.006 if (src, dst) == ("JPY", "USD") else None,
        today_fn=lambda: date(2026, 6, 15),
    )

    tm_val = await adapter.get_valuation("TM", latest_price=180.0, quote_currency="USD")
    assert tm_val["status"] == "ok"
    assert tm_val["shares_per_quote_unit"] == 10.0
    assert tm_val["pe_fy1"] == 12.0
    assert tm_val["pe_fy2"] == 10.0

    # Currency match (USD == USD) does NOT prove per-ADR unit -> unit_unverified!
    usd_adr = await adapter.get_valuation("USDADR", latest_price=100.0, quote_currency="USD")
    assert usd_adr["status"] == "unit_unverified"
    assert usd_adr["pe_fy1"] is None

    tsm_usd = await adapter.get_valuation("TSM", latest_price=150.0, quote_currency="USD")
    assert tsm_usd["status"] == "unit_unverified"
    assert tsm_usd["pe_fy1"] is None


@pytest.mark.asyncio
async def test_fiscal_year_selection_null_eps_single_year_fmp_fill_reported_fy_and_cross_fy_cache() -> None:
    """FY1/FY2 chosen by fiscal year end BEFORE dropping null EPS; single-year Yahoo miss filled by FMP; cross-FY cache invalidates."""
    # 1. Null EPS on FY1 does NOT promote FY2 into FY1
    fy1, fy2 = consensus_mod.select_fmp_fy1_fy2_estimates(
        [
            {"date": "2026-12-31", "epsAvg": None, "numAnalystsEps": 0},
            {"date": "2027-12-31", "epsAvg": 5.0, "numAnalystsEps": 20},
            {"date": "2028-12-31", "epsAvg": 6.5, "numAnalystsEps": 18},
        ],
        today=date(2026, 6, 15),
    )
    assert fy1 is not None and fy1["date"] == "2026-12-31" and fy1["epsAvg"] is None
    assert fy2 is not None and fy2["date"] == "2027-12-31" and fy2["epsAvg"] == 5.0

    # 2. Yahoo has FY1 only (missing FY2) -> FMP fills ONLY FY2 matched by fiscal year end
    fmp_mock = create_autospec(FMPClient, instance=True)
    fmp_mock.get_analyst_estimates.return_value = [
        {"date": "2027-01-31", "epsAvg": 4.40, "numAnalystsEps": 30},
        {"date": "2028-01-31", "epsAvg": 6.00, "numAnalystsEps": 28},
    ]
    fmp_mock.get_income_statement.return_value = [{"date": "2026-01-31"}]
    fmp_mock.get_profile.return_value = [
        {"symbol": "NVDA", "currency": "USD", "reportedCurrency": "USD", "companyName": "NVIDIA"}
    ]

    async def _partial_yf(_sym: str) -> consensus_mod.RawConsensusRecord:
        return consensus_mod.RawConsensusRecord(
            symbol="NVDA",
            fy1_eps=4.50,
            fy2_eps=None,
            fy1_end="2027-01-31",
            fy2_end="2028-01-31",
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=40,
            provider="yfinance",
            last_reported_fy_end="2026-01-31",
        )

    cur_day = {"d": date(2026, 11, 26)}
    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_partial_yf,
        fmp_client=fmp_mock,
        today_fn=lambda: cur_day["d"],
    )
    merged = await adapter.get_valuation("NVDA", latest_price=135.0, quote_currency="USD")
    assert merged["status"] == "ok"
    assert merged["provider"] == "yfinance+fmp"
    assert merged["fy1_eps"] == 4.50
    assert merged["fy2_eps"] == 6.00
    assert merged["pe_fy1"] == 30.0
    assert merged["pe_fy2"] == 22.5

    # 3. Cross-fiscal-year cache invalidation: advancing clock past fy1_end (2027-01-31 -> 2027-02-05)
    # forces a cache refresh even though age < 12h!
    yf_calls = {"n": 0}

    async def _rolled_yf(_sym: str) -> consensus_mod.RawConsensusRecord:
        yf_calls["n"] += 1
        return consensus_mod.RawConsensusRecord(
            symbol="NVDA",
            fy1_eps=6.00,
            fy2_eps=7.50,
            fy1_end="2028-01-31",
            fy2_end="2029-01-31",
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=42,
            provider="yfinance",
            last_reported_fy_end="2027-01-31",
        )

    cur_day["d"] = date(2027, 2, 5)
    adapter_rolled = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_rolled_yf,
        fmp_client=fmp_mock,
        today_fn=lambda: cur_day["d"],
    )
    # Non-blocking cached view reports stale + needs_warmup=True across FY boundary
    stale_val, needs_warm = await adapter_rolled.get_cached_valuation(
        "NVDA", latest_price=150.0, quote_currency="USD"
    )
    assert stale_val is not None and stale_val["status"] == "stale"
    assert needs_warm is True

    # Refresh fetches new fiscal year FY1=2028-01-31
    refreshed = await adapter_rolled.get_valuation("NVDA", latest_price=150.0, quote_currency="USD")
    assert yf_calls["n"] == 1
    assert refreshed["status"] == "ok"
    assert refreshed["fy1_end"] == "2028-01-31"
    assert refreshed["pe_fy1"] == 25.0


@pytest.mark.asyncio
async def test_nonblocking_metrics_cold_and_stale_fx_warmup_and_unverified_adr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real non-blocking `/metrics` entry warms cold & stale FX for cross-ccy stocks, and returns `unit_unverified` immediately for unverified ADRs."""
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(
            coverage_db.list_coverage_rows,
            return_value=[
                {"user_id": "u-fx", "ticker": "0700.HK", "tier": "core", "is_private": False},
                {"user_id": "u-fx", "ticker": "XYZADR", "tier": "watch", "is_private": False},
            ],
        ),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(coverage_db.get_earnings_calendar_mirror, return_value=None),
    )

    class _Provider:
        name = "yfinance"
        split_adjusted = True

        async def get_snapshots(self, symbols: Any, *, asset_type: str = "stocks", user_id: str = "") -> list[dict[str, Any]]:
            _ = (asset_type, user_id)
            px = {"0700.HK": (432.0, "HKD"), "XYZADR": (25.0, "USD")}
            return [{"symbol": s, "price": px[s][0], "previous_close": px[s][0], "currency": px[s][1]} for s in symbols if s in px]

        async def get_daily(self, symbol: str, *, from_date: str, to_date: str, is_index: bool = False, user_id: str = "") -> list[dict[str, Any]]:
            _ = (symbol, from_date, to_date, is_index, user_id)
            return [{"date": (date(2026, 1, 2) + timedelta(days=i)).isoformat(), "close": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "volume": 1000} for i in range(40)]

    monkeypatch.setattr(
        metrics_mod,
        "_resolve_provider_and_key",
        create_autospec(metrics_mod._resolve_provider_and_key, return_value=(_Provider(), "yfinance", True)),
    )

    fx_calls: list[tuple[str, str]] = []
    fx_rate_box = {"CNY_HKD": 1.08}

    async def _fake_fx(src: str, dst: str) -> float | None:
        fx_calls.append((src, dst))
        if (src, dst) == ("CNY", "HKD"):
            return fx_rate_box["CNY_HKD"]
        return None

    async def _fake_yf(symbol: str) -> consensus_mod.RawConsensusRecord:
        if symbol == "0700.HK":
            return consensus_mod.RawConsensusRecord(
                symbol="0700.HK",
                fy1_eps=25.0,
                fy2_eps=28.0,
                fy1_end="2027-12-31",
                fy2_end="2028-12-31",
                estimate_currency="CNY",
                quote_currency="HKD",
                analyst_count=35,
                provider="yfinance",
            )
        return consensus_mod.RawConsensusRecord(
            symbol="XYZADR",
            fy1_eps=10.0,
            fy2_eps=12.0,
            fy1_end="2027-12-31",
            fy2_end="2028-12-31",
            estimate_currency="JPY",
            quote_currency="USD",
            analyst_count=5,
            provider="yfinance",
            security_name="Unknown Corp Sponsored ADR",
        )

    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_fake_yf,
        fx_fetcher=_fake_fx,
        today_fn=lambda: date(2026, 11, 26),
    )
    svc = metrics_mod.WatchlistMetricsService(consensus_adapter=adapter)

    # 1. Cold consensus + cold FX -> returns immediately with both pending, warms consensus AND FX in background
    res1 = await svc.get_metrics_for_user("u-fx", wait_for_cold=False)
    assert set(res1["pending_valuation_symbols"]) == {"0700.HK", "XYZADR"}
    await asyncio.sleep(0.05)

    # 2. Second non-blocking call: 0700.HK has fresh P/E (16.0); XYZADR is immediately `unit_unverified` with ZERO JPY->USD FX calls!
    res2 = await svc.get_metrics_for_user("u-fx", wait_for_cold=False)
    assert res2["pending_valuation_symbols"] == []
    assert res2["items"]["0700.HK"]["valuation"]["status"] == "ok"
    assert res2["items"]["0700.HK"]["pe_fy1"] == 16.0
    assert res2["items"]["XYZADR"]["valuation"]["status"] == "unit_unverified"
    assert res2["items"]["XYZADR"]["pe_fy1"] is None
    assert ("JPY", "USD") not in fx_calls

    # 3. Fresh consensus + cold FX (evict FX only): non-blocking call schedules FX warmup and recovers on next call
    consensus_mod._FX_CACHE.clear()
    res3 = await svc.get_metrics_for_user("u-fx", wait_for_cold=False)
    assert res3["pending_valuation_symbols"] == ["0700.HK"]
    assert res3["items"]["XYZADR"]["valuation"]["status"] == "unit_unverified"
    await asyncio.sleep(0.05)
    res4 = await svc.get_metrics_for_user("u-fx", wait_for_cold=False)
    assert res4["pending_valuation_symbols"] == []
    assert res4["items"]["0700.HK"]["valuation"]["status"] == "ok"
    assert res4["items"]["0700.HK"]["pe_fy1"] == 16.0

    # 4. Fresh consensus + STALE FX: returns `stale` immediately AND refreshes FX in background
    fx_key = consensus_mod._fx_cache_key("CNY", "HKD")
    consensus_mod._FX_CACHE[fx_key] = (
        time.time() - consensus_mod.FX_CACHE_TTL_SECONDS - 120,
        1.08,
    )
    fx_rate_box["CNY_HKD"] = 1.12
    res5 = await svc.get_metrics_for_user("u-fx", wait_for_cold=False)
    assert res5["items"]["0700.HK"]["valuation"]["status"] == "stale"
    assert res5["pending_valuation_symbols"] == ["0700.HK"]
    await asyncio.sleep(0.05)
    res6 = await svc.get_metrics_for_user("u-fx", wait_for_cold=False)
    assert res6["pending_valuation_symbols"] == []
    assert res6["items"]["0700.HK"]["valuation"]["status"] == "ok"
    assert res6["items"]["0700.HK"]["pe_fy1"] == round(432.0 / (25.0 * 1.12), 2)


@pytest.mark.asyncio
async def test_fmp_client_closes_on_success_and_error_and_detects_429_by_type_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owned FMPClient always closes on success & exception; 429 is detected by exception type / status_code (never text)."""
    monkeypatch.setenv("FMP_API_KEY", "test-key-123")
    closed_count = {"n": 0}
    mode = {"behavior": "ok"}

    class _TrackedFmpClient:
        def __init__(self, api_key: str | None = None) -> None:
            self.api_key = api_key

        async def get_analyst_estimates(self, symbol: str, period: str = "annual", limit: int = 6) -> list[dict[str, Any]]:
            _ = (symbol, period, limit)
            if mode["behavior"] == "quota":
                # Message has NO "429" or "rate limit" substring — must be detected by FmpQuotaExhausted / status_code==429!
                raise FmpQuotaExhausted()
            if mode["behavior"] == "status429":
                raise FMPRequestError("opaque upstream refusal", status_code=429)
            if mode["behavior"] == "text_only":
                raise RuntimeError("harmless text mentioning 429")
            return [{"date": "2027-12-31", "epsAvg": 5.0, "numAnalystsEps": 12}]

        async def get_income_statement(self, symbol: str, period: str = "annual", limit: int = 1) -> list[dict[str, Any]]:
            _ = (symbol, period, limit)
            return []

        async def get_profile(self, symbol: str) -> list[dict[str, Any]]:
            return [{"symbol": symbol, "currency": "USD", "reportedCurrency": "USD"}]

        async def close(self) -> None:
            closed_count["n"] += 1

    import src.data_client.fmp.fmp_client as fmp_client_mod

    monkeypatch.setattr(fmp_client_mod, "FMPClient", _TrackedFmpClient)

    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=lambda _s: None,
        today_fn=lambda: date(2026, 6, 15),
    )

    # 1. Success path closes owned FMPClient
    mode["behavior"] = "ok"
    res_ok = await adapter.get_valuation("CLOS1", latest_price=100.0, quote_currency="USD")
    assert res_ok["status"] == "ok" and res_ok["pe_fy1"] == 20.0
    assert closed_count["n"] == 1

    # 2. FmpQuotaExhausted (no "429" in message) -> detected as rate_limited AND closed
    mode["behavior"] = "quota"
    res_q = await adapter.get_valuation("CLOS2", latest_price=100.0, quote_currency="USD")
    assert res_q["status"] == "rate_limited"
    assert closed_count["n"] == 2

    # 3. FMPRequestError(status_code=429) -> detected as rate_limited AND closed
    mode["behavior"] = "status429"
    res_429 = await adapter.get_valuation("CLOS3", latest_price=100.0, quote_currency="USD")
    assert res_429["status"] == "rate_limited"
    assert closed_count["n"] == 3

    # 4. Arbitrary text containing "429" without status_code==429 or rate-limit type -> NOT misclassified as rate_limited
    assert consensus_mod._is_rate_limit_exception(RuntimeError("harmless text mentioning 429")) is False
    mode["behavior"] = "text_only"
    res_txt = await adapter.get_valuation("CLOS4", latest_price=100.0, quote_currency="USD")
    assert res_txt["status"] == "no_coverage"
    assert closed_count["n"] == 4


@pytest.mark.asyncio
async def test_redis_cross_process_semaphore_singleflight_and_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared Redis provides 8-slot cross-process semaphore + cross-process singleflight; falls back locally if Redis is unavailable."""

    class _FakeSharedRedis:
        enabled = True

        def __init__(self) -> None:
            self.kv: dict[str, str] = {}
            self.locks: dict[str, str] = {}

        async def get(self, key: str) -> str | None:
            return self.kv.get(key)

        async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
            _ = ttl
            self.kv[key] = value
            return True

        async def acquire_lock(self, key: str, token: str, ttl_ms: int) -> bool | None:
            _ = ttl_ms
            if key in self.locks:
                return False
            self.locks[key] = token
            return True

        async def release_lock(self, key: str, token: str) -> None:
            if self.locks.get(key) == token:
                self.locks.pop(key, None)

    fake_redis = _FakeSharedRedis()
    redis_mock = create_autospec(consensus_mod._get_redis_cache_client, return_value=fake_redis)
    monkeypatch.setattr(consensus_mod, "_get_redis_cache_client", redis_mock)

    # 1. Cross-process singleflight: Worker A acquires Redis inflight lock; Worker B (with empty in-process maps) waits for Redis cache!
    fetch_calls = {"n": 0}
    gate = asyncio.Event()

    async def _slow_yf(sym: str) -> consensus_mod.RawConsensusRecord:
        fetch_calls["n"] += 1
        await gate.wait()
        return consensus_mod.RawConsensusRecord(
            symbol=sym,
            fy1_eps=5.0,
            fy2_eps=6.0,
            fy1_end="2027-12-31",
            fy2_end="2028-12-31",
            estimate_currency="USD",
            quote_currency="USD",
            provider="yfinance",
        )

    adapter_a = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_slow_yf, today_fn=lambda: date(2026, 11, 26)
    )
    adapter_b = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_slow_yf, today_fn=lambda: date(2026, 11, 26)
    )

    task_a = asyncio.create_task(adapter_a.get_valuation("SF1", latest_price=100.0, quote_currency="USD"))
    await asyncio.sleep(0.02)
    # Simulate separate worker process by clearing in-process singleflight & memory cache before Worker B runs
    consensus_mod._INFLIGHT_CONSENSUS.clear()
    consensus_mod._CONSENSUS_CACHE.clear()
    task_b = asyncio.create_task(adapter_b.get_valuation("SF1", latest_price=100.0, quote_currency="USD"))
    await asyncio.sleep(0.02)
    gate.set()

    val_a, val_b = await asyncio.gather(task_a, task_b)
    assert val_a["pe_fy1"] == 20.0
    assert val_b["pe_fy1"] == 20.0
    assert fetch_calls["n"] == 1  # Cross-process dedup -> only ONE upstream call!

    # 2. Cross-process 8-slot semaphore caps total concurrency at MAX_CONSENSUS_CONCURRENCY = 8
    active = {"cur": 0, "max": 0}

    async def _bounded_yf(sym: str) -> consensus_mod.RawConsensusRecord:
        active["cur"] += 1
        active["max"] = max(active["max"], active["cur"])
        await asyncio.sleep(0.04)
        active["cur"] -= 1
        return consensus_mod.RawConsensusRecord(
            symbol=sym,
            fy1_eps=4.0,
            fy2_eps=5.0,
            fy1_end="2027-12-31",
            fy2_end="2028-12-31",
            estimate_currency="USD",
            quote_currency="USD",
            provider="yfinance",
        )

    adapter_sem = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_bounded_yf, today_fn=lambda: date(2026, 11, 26)
    )
    # Also pre-occupy 2 Redis slots from "another worker" -> this worker can run at most 6 concurrently!
    fake_redis.locks[f"{consensus_mod._REDIS_SEM_KEY_PREFIX}0"] = "other-worker-0"
    fake_redis.locks[f"{consensus_mod._REDIS_SEM_KEY_PREFIX}1"] = "other-worker-1"
    await asyncio.gather(
        *(
            adapter_sem.get_valuation(f"SEM{i}", latest_price=80.0, quote_currency="USD")
            for i in range(10)
        )
    )
    assert active["max"] <= consensus_mod.MAX_CONSENSUS_CONCURRENCY - 2

    # 3. Redis unavailable (None) -> falls back cleanly to in-process semaphore
    redis_mock.return_value = None
    fallback_res = await adapter_sem.get_valuation("NOREDIS", latest_price=80.0, quote_currency="USD")
    assert fallback_res["status"] == "ok" and fallback_res["pe_fy1"] == 20.0


@pytest.mark.asyncio
async def test_standalone_core_metrics_service_end_to_end_us_hk_adr_missing_unlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core installation acceptance: US, HK, verified ADR, unverified ADR, missing estimates, and unlisted."""
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(
            coverage_db.list_coverage_rows,
            return_value=[
                {"user_id": "u-core", "ticker": "NVDA", "tier": "core", "is_private": False},
                {"user_id": "u-core", "ticker": "0700.HK", "tier": "core", "is_private": False},
                {"user_id": "u-core", "ticker": "TSM", "tier": "watch", "is_private": False},
                {"user_id": "u-core", "ticker": "NOEST.HK", "tier": "radar", "is_private": False},
                {"user_id": "u-core", "ticker": "SPCX", "tier": "core", "is_private": True},
            ],
        ),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(coverage_db.get_earnings_calendar_mirror, return_value=None),
    )

    class _CoreMarketProvider:
        name = "yfinance"
        split_adjusted = True

        async def get_snapshots(self, symbols: Any, *, asset_type: str = "stocks", user_id: str = "") -> list[dict[str, Any]]:
            _ = (asset_type, user_id)
            px_map = {
                "NVDA": (135.0, "USD"),
                "0700.HK": (432.0, "HKD"),
                "TSM": (155.0, "USD"),
                "NOEST.HK": (12.0, "HKD"),
            }
            return [
                {"symbol": s, "price": px_map[s][0], "previous_close": px_map[s][0], "currency": px_map[s][1]}
                for s in symbols
                if s in px_map
            ]

        async def get_daily(self, symbol: str, *, from_date: str, to_date: str, is_index: bool = False, user_id: str = "") -> list[dict[str, Any]]:
            _ = (symbol, from_date, to_date, is_index, user_id)
            return [
                {"date": (date(2026, 1, 2) + timedelta(days=i)).isoformat(), "close": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "volume": 1000}
                for i in range(40)
            ]

    monkeypatch.setattr(
        metrics_mod,
        "_resolve_provider_and_key",
        create_autospec(metrics_mod._resolve_provider_and_key, return_value=(_CoreMarketProvider(), "yfinance", True)),
    )

    async def _core_yf(symbol: str) -> consensus_mod.RawConsensusRecord:
        if symbol == "NVDA":
            return consensus_mod.RawConsensusRecord(symbol="NVDA", fy1_eps=4.5, fy2_eps=5.4, fy1_end="2027-01-31", fy2_end="2028-01-31", estimate_currency="USD", quote_currency="USD", analyst_count=40, provider="yfinance", eps_per_quote_unit_confirmed=True)
        if symbol == "0700.HK":
            return consensus_mod.RawConsensusRecord(symbol="0700.HK", fy1_eps=25.0, fy2_eps=27.5, fy1_end="2026-12-31", fy2_end="2027-12-31", estimate_currency="CNY", quote_currency="HKD", analyst_count=30, provider="yfinance")
        if symbol == "TSM":
            return consensus_mod.RawConsensusRecord(symbol="TSM", fy1_eps=50.0, fy2_eps=60.0, fy1_end="2026-12-31", fy2_end="2027-12-31", estimate_currency="TWD", quote_currency="USD", analyst_count=20, provider="yfinance", security_name="TSM ADR")
        return consensus_mod.RawConsensusRecord(symbol=symbol, quote_currency="HKD", provider="yfinance")

    async def _core_fx(src: str, dst: str) -> float | None:
        return {("CNY", "HKD"): 1.08, ("TWD", "USD"): 0.031}.get((src, dst))

    svc = metrics_mod.WatchlistMetricsService(
        consensus_adapter=consensus_mod.WatchlistConsensusAdapter(
            yfinance_fetcher=_core_yf,
            fx_fetcher=_core_fx,
        )
    )
    res = await svc.get_metrics_for_user("u-core", wait_for_cold=True)
    assert res["ok"] is True
    assert res["items"]["NVDA"]["pe_fy1"] == 30.0
    assert res["items"]["NVDA"]["valuation"]["status"] == "ok"
    assert res["items"]["0700.HK"]["pe_fy1"] == 16.0
    assert res["items"]["0700.HK"]["valuation"]["status"] == "ok"
    assert res["items"]["TSM"]["pe_fy1"] == 20.0
    assert res["items"]["TSM"]["valuation"]["status"] == "ok"
    assert res["items"]["NOEST.HK"]["pe_fy1"] is None
    assert res["items"]["NOEST.HK"]["valuation"]["status"] == "no_coverage"
    assert res["items"]["SPCX"]["is_private"] is True
    assert res["items"]["SPCX"]["valuation"]["status"] == "unlisted"
