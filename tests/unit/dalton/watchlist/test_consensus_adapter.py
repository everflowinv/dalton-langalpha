"""Unit tests for WatchlistConsensusAdapter under Standalone Core (§6.3).

Covers US equities, HK cross-currency equities, verified ADRs, unverified ADRs,
FMP annual fiscal-year matching fallback, all 7 valuation cell states, and
Standalone Core acceptance with zero private service calls.
"""

from __future__ import annotations

import asyncio
import threading
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
from src.utils.cache.redis_cache import RedisCacheClient


def _make_ttl_redis_mock() -> tuple[Any, dict[str, tuple[float | None, str]], dict[str, tuple[float, str]]]:
    """Create a ``create_autospec(RedisCacheClient, instance=True)`` double that enforces TTLs."""
    kv: dict[str, tuple[float | None, str]] = {}
    locks: dict[str, tuple[float, str]] = {}

    redis_mock = create_autospec(RedisCacheClient, instance=True)
    redis_mock.enabled = True

    def _prune() -> float:
        now = time.monotonic()
        for k in [k for k, (exp, _) in kv.items() if exp is not None and exp <= now]:
            kv.pop(k, None)
        for k in [k for k, (exp, _) in locks.items() if exp <= now]:
            locks.pop(k, None)
        return now

    async def _get(key: str) -> str | None:
        _prune()
        entry = kv.get(key)
        return entry[1] if entry is not None else None

    async def _set(key: str, value: Any, ttl: int | None = None) -> bool:
        now = _prune()
        exp = (now + float(ttl)) if (ttl is not None and ttl > 0) else None
        kv[key] = (exp, value)
        return True

    async def _delete(key: str, *more: str) -> bool:
        _prune()
        removed = False
        for k in (key, *more):
            if kv.pop(k, None) is not None:
                removed = True
        return removed

    async def _acquire_lock(key: str, token: str, ttl_ms: int) -> bool | None:
        now = _prune()
        if key in locks:
            return False
        locks[key] = (now + (ttl_ms / 1000.0), token)
        return True

    async def _release_lock(key: str, token: str) -> None:
        _prune()
        entry = locks.get(key)
        if entry is not None and entry[1] == token:
            locks.pop(key, None)

    async def _extend_lock(key: str, token: str, ttl_ms: int) -> bool:
        now = _prune()
        entry = locks.get(key)
        if entry is not None and entry[1] == token:
            locks[key] = (now + (ttl_ms / 1000.0), token)
            return True
        return False

    redis_mock.get.side_effect = _get
    redis_mock.set.side_effect = _set
    redis_mock.delete.side_effect = _delete
    redis_mock.acquire_lock.side_effect = _acquire_lock
    redis_mock.release_lock.side_effect = _release_lock
    redis_mock.extend_lock.side_effect = _extend_lock
    return redis_mock, kv, locks


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
    fmp_mock.get_income_statement.return_value = [
        {"date": "2026-12-31", "reportedCurrency": "USD"}
    ]
    fmp_mock.get_profile.return_value = [
        {"symbol": "MSFT", "currency": "USD", "companyName": "Test Corp", "isAdr": False}
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
    fmp_mock.get_income_statement.return_value = [
        {"date": "2026-01-31", "reportedCurrency": "USD"}
    ]
    fmp_mock.get_profile.return_value = [
        {"symbol": "NVDA", "currency": "USD", "companyName": "NVIDIA", "isAdr": False}
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

    def _build_fmp_client(api_key: str | None = None, **_kwargs: Any) -> Any:
        _ = api_key
        inst = create_autospec(FMPClient, instance=True)

        async def _get_est(symbol: str, period: str = "annual", limit: int = 6) -> list[dict[str, Any]]:
            _ = (symbol, period, limit)
            if mode["behavior"] == "quota":
                # Message has NO "429" or "rate limit" substring — must be detected by FmpQuotaExhausted / status_code==429!
                raise FmpQuotaExhausted()
            if mode["behavior"] == "status429":
                raise FMPRequestError("opaque upstream refusal", status_code=429)
            if mode["behavior"] == "text_only":
                raise RuntimeError("harmless text mentioning 429")
            return [{"date": "2027-12-31", "epsAvg": 5.0, "numAnalystsEps": 12}]

        async def _get_inc(symbol: str, period: str = "annual", limit: int = 1) -> list[dict[str, Any]]:
            _ = (symbol, period, limit)
            return [{"date": "2025-12-31", "reportedCurrency": "USD"}]

        async def _get_prof(symbol: str) -> list[dict[str, Any]]:
            return [{"symbol": symbol, "currency": "USD", "companyName": "Test Co", "isAdr": False}]

        async def _close() -> None:
            closed_count["n"] += 1

        inst.get_analyst_estimates.side_effect = _get_est
        inst.get_income_statement.side_effect = _get_inc
        inst.get_profile.side_effect = _get_prof
        inst.close.side_effect = _close
        return inst

    import src.data_client.fmp.fmp_client as fmp_client_mod

    monkeypatch.setattr(fmp_client_mod, "FMPClient", create_autospec(FMPClient, side_effect=_build_fmp_client))

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
    fake_redis, _kv, locks = _make_ttl_redis_mock()
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
    # Pre-occupy 2 Redis slots from "another worker" with live TTL -> this worker can run at most 6 concurrently!
    far_exp = time.monotonic() + 30.0
    locks[f"{consensus_mod._REDIS_SEM_KEY_PREFIX}0"] = (far_exp, "other-worker-0")
    locks[f"{consensus_mod._REDIS_SEM_KEY_PREFIX}1"] = (far_exp, "other-worker-1")
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


@pytest.mark.asyncio
async def test_cross_fy_yahoo_stale_0y_filtered_promoted_fmp_filled_and_rolled_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After fiscal year rollover, stale Yahoo `0y` is excluded before assigning FY1/FY2; FMP fills missing year; fully rolled-past records are never cached as fresh."""
    import pandas as pd
    import yfinance as yf
    import src.data_client.yfinance.yahoo as yahoo_mod

    # 1. Yahoo returns old 0y (2026-12-31) and +1y (2027-12-31) on today=2027-01-15
    fake_ticker = create_autospec(yf.Ticker, instance=True)
    fake_ticker.earnings_estimate = pd.DataFrame(
        {"avg": [4.0, 5.5], "numberOfAnalysts": [40, 35]},
        index=["0y", "+1y"],
    )
    fake_ticker._analysis = type(
        "_A",
        (),
        {
            "_earnings_trend": [
                {"period": "0y", "endDate": "2026-12-31", "earningsEstimate": {"currency": "USD"}},
                {"period": "+1y", "endDate": "2027-12-31"},
            ]
        },
    )()
    fake_ticker.info = {
        "currency": "USD",
        "financialCurrency": "USD",
        "quoteType": "EQUITY",
        "longName": "Rollover Corp",
    }
    monkeypatch.setattr(yahoo_mod, "yahoo_ticker", create_autospec(yahoo_mod.yahoo_ticker, return_value=fake_ticker))

    fmp_mock = create_autospec(FMPClient, instance=True)
    fmp_mock.get_analyst_estimates.return_value = [
        {"date": "2026-12-31", "epsAvg": 4.0, "numAnalystsEps": 40},
        {"date": "2027-12-31", "epsAvg": 5.4, "numAnalystsEps": 35},
        {"date": "2028-12-31", "epsAvg": 7.0, "numAnalystsEps": 30},
    ]
    fmp_mock.get_income_statement.return_value = [
        {"date": "2025-12-31", "reportedCurrency": "USD"}
    ]
    fmp_mock.get_profile.return_value = [
        {"symbol": "ROLL", "currency": "USD", "companyName": "Rollover Corp", "isAdr": False}
    ]

    adapter = consensus_mod.WatchlistConsensusAdapter(
        fmp_client=fmp_mock,
        today_fn=lambda: date(2027, 1, 15),
    )
    val = await adapter.get_valuation("ROLL", latest_price=110.0, quote_currency="USD")
    assert val["status"] == "ok"
    assert val["fy1_end"] == "2027-12-31"
    assert val["fy1_eps"] == 5.5
    assert val["fy2_end"] == "2028-12-31"
    assert val["fy2_eps"] == 7.0
    assert val["pe_fy1"] == 20.0
    assert val["provider"] == "yfinance+fmp"

    # 2. Both Yahoo 0y and +1y have rolled past today=2028-02-01 (no FMP) -> NEVER written as fresh success to cache!
    adapter_all_old = consensus_mod.WatchlistConsensusAdapter(
        today_fn=lambda: date(2028, 2, 1),
    )
    old_val = await adapter_all_old.get_valuation("ALLOLD", latest_price=100.0, quote_currency="USD")
    assert old_val["status"] == "no_coverage"
    assert old_val["pe_fy1"] is None
    assert consensus_mod._consensus_cache_key("ALLOLD") not in consensus_mod._CONSENSUS_CACHE
    # Cooldown is active so non-blocking view does NOT re-trigger warmup immediately
    cached_after, needs_warm = await adapter_all_old.get_cached_valuation(
        "ALLOLD", latest_price=100.0, quote_currency="USD"
    )
    assert cached_after is not None and cached_after["status"] == "no_coverage"
    assert needs_warm is False


def test_single_year_fmp_fill_never_resurrects_excluded_fiscal_year() -> None:
    """Single-year partial merge searches only eligible FMP rows and never fills back an excluded fiscal year."""
    primary = consensus_mod.RawConsensusRecord(
        symbol="PART",
        fy1_eps=None,
        fy2_eps=6.0,
        fy1_end="2026-12-31",  # already rolled past today=2027-01-15
        fy2_end="2027-12-31",
        estimate_currency="USD",
        quote_currency="USD",
        provider="yfinance",
        last_reported_fy_end="2026-12-31",
    )
    raw_fmp_rows = [
        {"date": "2026-12-31", "epsAvg": 4.2, "numAnalystsEps": 25},  # excluded!
        {"date": "2027-12-31", "epsAvg": 6.1, "numAnalystsEps": 25},
        {"date": "2028-12-31", "epsAvg": 7.5, "numAnalystsEps": 20},
    ]
    fallback = consensus_mod.RawConsensusRecord(
        symbol="PART",
        fy1_eps=6.1,
        fy2_eps=7.5,
        fy1_end="2027-12-31",
        fy2_end="2028-12-31",
        estimate_currency="USD",
        quote_currency="USD",
        provider="fmp",
        last_reported_fy_end="2026-12-31",
    )
    merged = consensus_mod._merge_partial_consensus_record(
        primary,
        fallback,
        fallback_annual_rows=raw_fmp_rows,
        today=date(2027, 1, 15),
        last_reported_fy_end="2026-12-31",
    )
    # Must NOT resurrect excluded 2026-12-31 (4.2); eligible merged FY1/FY2 are 2027-12-31 (primary 6.0) & 2028-12-31 (FMP 7.5)!
    assert "2026-12-31" not in (merged.fy1_end, merged.fy2_end)
    assert 4.2 not in (merged.fy1_eps, merged.fy2_eps)
    assert merged.fy1_end == "2027-12-31"
    assert merged.fy1_eps == 6.0
    assert merged.fy2_end == "2028-12-31"
    assert merged.fy2_eps == 7.5


@pytest.mark.asyncio
async def test_adr_azn_direct_ordinary_share_smfg_1_2_and_unverified_omitted() -> None:
    """AZN is removed (direct NYSE ordinary share since 2026-02-02), SMFG uses 1.2 (since 2026-10-01), unverified ADRs show unit_unverified."""
    assert "AZN" not in consensus_mod.VERIFIED_ADR_SHARES_PER_UNIT
    assert "AZN" not in consensus_mod.KNOWN_ADR_SYMBOLS
    assert "SMFG" in consensus_mod.VERIFIED_ADR_SHARES_PER_UNIT
    assert "SMFG" in consensus_mod.KNOWN_ADR_SYMBOLS
    smfg_entry = consensus_mod.VERIFIED_ADR_SHARES_PER_UNIT["SMFG"]
    assert smfg_entry.shares_per_adr == 1.2
    assert smfg_entry.underlying_currency == "JPY"
    assert smfg_entry.effective_date == "2026-10-01"
    assert smfg_entry.source.strip()
    assert "SHEL" not in consensus_mod.VERIFIED_ADR_SHARES_PER_UNIT
    assert "SHEL" in consensus_mod.KNOWN_ADR_SYMBOLS

    records = {
        # AZN direct NYSE ordinary shares (not ADR) -> 1:1, NO 0.5x scaling!
        "AZN": consensus_mod.RawConsensusRecord(
            symbol="AZN",
            fy1_eps=10.0,
            fy2_eps=11.0,
            fy1_end="2026-12-31",
            fy2_end="2027-12-31",
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=20,
            provider="yfinance",
            quote_type="EQUITY",
            security_name="AstraZeneca PLC",
        ),
        # SMFG ADR: ordinary EPS = 250 JPY, 1.2 shares/ADS, FX 0.006 -> per-ADS EPS = 1.8 USD -> @ $18 = 10.0x P/E
        "SMFG": consensus_mod.RawConsensusRecord(
            symbol="SMFG",
            fy1_eps=250.0,
            fy2_eps=300.0,
            fy1_end="2027-03-31",
            fy2_end="2028-03-31",
            estimate_currency="JPY",
            quote_currency="USD",
            analyst_count=14,
            provider="yfinance",
            quote_type="EQUITY",
            security_name="Sumitomo Mitsui Financial Group, Inc. ADR",
        ),
        # Removed/unverified ADR with plain corporate name (no ADR wording!) -> unit_unverified (`—`)
        "SHEL": consensus_mod.RawConsensusRecord(
            symbol="SHEL",
            fy1_eps=7.0,
            fy2_eps=7.5,
            fy1_end="2026-12-31",
            fy2_end="2027-12-31",
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=18,
            provider="yfinance",
            quote_type="EQUITY",
            security_name="Shell plc",
        ),
    }

    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=lambda s: records.get(s),
        fx_fetcher=lambda src, dst: 0.006 if (src, dst) == ("JPY", "USD") else None,
        today_fn=lambda: date(2026, 11, 26),
    )

    azn = await adapter.get_valuation("AZN", latest_price=150.0, quote_currency="USD")
    assert azn["status"] == "ok"
    assert azn["shares_per_quote_unit"] == 1.0
    assert azn["pe_fy1"] == 15.0  # NOT 30.0 (old 0.5 ratio)!

    smfg = await adapter.get_valuation("SMFG", latest_price=18.0, quote_currency="USD")
    assert smfg["status"] == "ok"
    assert smfg["shares_per_quote_unit"] == 1.2
    assert smfg["pe_fy1"] == 10.0  # 18 / (250 * 1.2 * 0.006) = 10.0, NOT 20.0 (old 0.6 ratio)!

    shel = await adapter.get_valuation("SHEL", latest_price=70.0, quote_currency="USD")
    assert shel["status"] == "unit_unverified"
    assert shel["pe_fy1"] is None


@pytest.mark.asyncio
async def test_fmp_fallback_currency_strictly_from_income_statement_never_profile() -> None:
    """FMP estimate currency comes strictly from financial statements (income statement reportedCurrency), never profile.currency."""
    # 1. Cross-currency HK stock: profile.currency=HKD, income_statement.reportedCurrency=CNY -> applies CNY->HKD FX!
    fmp_hk = create_autospec(FMPClient, instance=True)
    fmp_hk.get_analyst_estimates.return_value = [
        {"date": "2026-12-31", "epsAvg": 25.0, "numAnalystsEps": 30},
        {"date": "2027-12-31", "epsAvg": 28.0, "numAnalystsEps": 28},
    ]
    fmp_hk.get_income_statement.return_value = [
        {"date": "2025-12-31", "reportedCurrency": "CNY"}
    ]
    fmp_hk.get_profile.return_value = [
        {"symbol": "0700.HK", "currency": "HKD", "companyName": "Tencent Holdings", "isAdr": False}
    ]

    adapter_hk = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=lambda _s: None,
        fmp_client=fmp_hk,
        fx_fetcher=lambda src, dst: 1.08 if (src, dst) == ("CNY", "HKD") else None,
        today_fn=lambda: date(2026, 6, 15),
    )
    hk_val = await adapter_hk.get_valuation("0700.HK", latest_price=432.0, quote_currency="HKD")
    assert hk_val["status"] == "ok"
    assert hk_val["estimate_currency"] == "CNY"
    assert hk_val["quote_currency"] == "HKD"
    assert hk_val["pe_fy1"] == 16.0  # 432 / (25 * 1.08)

    # 2. Missing reportedCurrency in income_statement -> MUST keep estimate_currency=None and NOT fall back to profile.currency!
    fmp_no_ccy = create_autospec(FMPClient, instance=True)
    fmp_no_ccy.get_analyst_estimates.return_value = [
        {"date": "2026-12-31", "epsAvg": 25.0, "numAnalystsEps": 30},
    ]
    fmp_no_ccy.get_income_statement.return_value = [{"date": "2025-12-31"}]
    fmp_no_ccy.get_profile.return_value = [
        {"symbol": "9988.HK", "currency": "HKD", "companyName": "Alibaba Group", "isAdr": False}
    ]

    adapter_no_ccy = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=lambda _s: None,
        fmp_client=fmp_no_ccy,
        today_fn=lambda: date(2026, 6, 15),
    )
    missing_ccy_val = await adapter_no_ccy.get_valuation("9988.HK", latest_price=100.0, quote_currency="HKD")
    assert missing_ccy_val["estimate_currency"] is None
    assert missing_ccy_val["status"] == "unit_unverified"
    assert missing_ccy_val["pe_fy1"] is None


@pytest.mark.asyncio
async def test_redis_singleflight_wait_timeout_no_duplicate_fetch_and_hard_deadline_under_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When another worker holds the Redis inflight lock past wait timeout (>9s default), this worker sends ZERO upstream calls; upstream hard deadline is strictly below Redis lease."""
    # Verify default constants maintain the safety invariants
    assert consensus_mod._REDIS_INFLIGHT_WAIT_SECONDS == 9.0
    assert consensus_mod._UPSTREAM_SLOT_HARD_TIMEOUT_SECONDS < (consensus_mod._REDIS_SEM_LEASE_MS / 1000.0)
    assert consensus_mod._UPSTREAM_SLOT_HARD_TIMEOUT_SECONDS < (consensus_mod._REDIS_INFLIGHT_LOCK_TTL_MS / 1000.0)

    fake_redis, _kv, locks = _make_ttl_redis_mock()
    monkeypatch.setattr(
        consensus_mod,
        "_get_redis_cache_client",
        create_autospec(consensus_mod._get_redis_cache_client, return_value=fake_redis),
    )
    monkeypatch.setattr(consensus_mod, "_REDIS_INFLIGHT_WAIT_SECONDS", 0.12)
    monkeypatch.setattr(consensus_mod, "_REDIS_INFLIGHT_POLL_SECONDS", 0.02)

    fetch_calls = {"n": 0}
    fx_calls = {"n": 0}

    async def _counting_yf(sym: str) -> consensus_mod.RawConsensusRecord:
        fetch_calls["n"] += 1
        return consensus_mod.RawConsensusRecord(
            symbol=sym, fy1_eps=5.0, fy1_end="2027-12-31", estimate_currency="USD", quote_currency="USD"
        )

    async def _counting_fx(src: str, dst: str) -> float:
        _ = (src, dst)
        fx_calls["n"] += 1
        return 1.08

    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_counting_yf,
        fx_fetcher=_counting_fx,
        today_fn=lambda: date(2026, 11, 26),
    )

    # 1. Cold cache + another worker holds inflight lock past timeout -> ZERO upstream calls, returns loading!
    sym_key = consensus_mod._consensus_cache_key("BUSY1")
    locks[f"{sym_key}:inflight"] = (time.monotonic() + 30.0, "other-worker-lock")
    busy_cold = await adapter.get_valuation("BUSY1", latest_price=100.0, quote_currency="USD")
    assert fetch_calls["n"] == 0
    assert busy_cold["status"] == "loading"
    assert busy_cold.get("pending") is True

    # 2. Stale cache + another worker holds inflight lock past timeout -> ZERO upstream calls, returns stale cached PE!
    stale_key = consensus_mod._consensus_cache_key("BUSY2")
    consensus_mod._CONSENSUS_CACHE[stale_key] = (
        time.time() - consensus_mod.CONSENSUS_CACHE_TTL_SECONDS - 600,
        consensus_mod.RawConsensusRecord(
            symbol="BUSY2",
            fy1_eps=4.0,
            fy2_eps=5.0,
            fy1_end="2027-12-31",
            fy2_end="2028-12-31",
            estimate_currency="USD",
            quote_currency="USD",
            provider="yfinance",
        ).to_dict(),
    )
    locks[f"{stale_key}:inflight"] = (time.monotonic() + 30.0, "other-worker-lock")
    busy_stale = await adapter.get_valuation("BUSY2", latest_price=100.0, quote_currency="USD")
    assert fetch_calls["n"] == 0
    assert busy_stale["status"] == "stale"
    assert busy_stale["pe_fy1"] == 25.0

    # 3. FX singleflight lock held by another worker past timeout -> ZERO FX upstream calls!
    fx_key = consensus_mod._fx_cache_key("CNY", "HKD")
    locks[f"{fx_key}:inflight"] = (time.monotonic() + 30.0, "other-worker-fx")
    rate, is_stale, is_rl, is_loading = await adapter._fetch_spot_fx_rate_with_status("CNY", "HKD")
    assert fx_calls["n"] == 0
    assert rate is None and is_stale is False and is_rl is False and is_loading is True

    # 4. Slow FMP call exceeding hard upstream deadline is cancelled cleanly and releases the Redis slot
    monkeypatch.setattr(consensus_mod, "CONSENSUS_FETCH_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(consensus_mod, "_UPSTREAM_SLOT_HARD_TIMEOUT_SECONDS", 0.1)
    fmp_slow = create_autospec(FMPClient, instance=True)

    async def _hang(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(5.0)
        return []

    fmp_slow.get_analyst_estimates.side_effect = _hang
    adapter_slow = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=lambda _s: None,
        fmp_client=fmp_slow,
        today_fn=lambda: date(2026, 11, 26),
    )
    slow_res = await adapter_slow.get_valuation("SLOWFMP", latest_price=50.0, quote_currency="USD")
    assert slow_res["status"] == "no_coverage"
    # No Redis semaphore slots leaked!
    leaked_slots = [k for k in locks if k.startswith(consensus_mod._REDIS_SEM_KEY_PREFIX)]
    assert leaked_slots == []


@pytest.mark.asyncio
async def test_empty_and_non_429_errors_enter_cooldown_and_exit_pending_valuation_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty FX results and non-429 FX/consensus errors write cooldown so symbols stop looping in pending_valuation_symbols."""
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(
            coverage_db.list_coverage_rows,
            return_value=[
                {"user_id": "u-cd", "ticker": "0700.HK", "tier": "core", "is_private": False},
                {"user_id": "u-cd", "ticker": "ERRCO", "tier": "watch", "is_private": False},
            ],
        ),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(coverage_db.get_earnings_calendar_mirror, return_value=None),
    )

    class _SimpleProvider:
        name = "yfinance"
        split_adjusted = True

        async def get_snapshots(self, symbols: Any, *, asset_type: str = "stocks", user_id: str = "") -> list[dict[str, Any]]:
            _ = (asset_type, user_id)
            px = {"0700.HK": (400.0, "HKD"), "ERRCO": (50.0, "USD")}
            return [{"symbol": s, "price": px[s][0], "previous_close": px[s][0], "currency": px[s][1]} for s in symbols if s in px]

        async def get_daily(self, symbol: str, *, from_date: str, to_date: str, is_index: bool = False, user_id: str = "") -> list[dict[str, Any]]:
            _ = (symbol, from_date, to_date, is_index, user_id)
            return [{"date": (date(2026, 1, 2) + timedelta(days=i)).isoformat(), "close": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "volume": 1000} for i in range(40)]

    monkeypatch.setattr(
        metrics_mod,
        "_resolve_provider_and_key",
        create_autospec(metrics_mod._resolve_provider_and_key, return_value=(_SimpleProvider(), "yfinance", True)),
    )

    fx_mode = {"mode": "empty"}

    async def _failing_fx(src: str, dst: str) -> float | None:
        _ = (src, dst)
        if fx_mode["mode"] == "error":
            raise RuntimeError("FX upstream 503")
        return None  # empty FX result

    async def _yf_for_cd(sym: str) -> consensus_mod.RawConsensusRecord:
        if sym == "ERRCO":
            raise RuntimeError("Yahoo 500 error")
        return consensus_mod.RawConsensusRecord(
            symbol="0700.HK",
            fy1_eps=25.0,
            fy2_eps=28.0,
            fy1_end="2027-12-31",
            fy2_end="2028-12-31",
            estimate_currency="CNY",
            quote_currency="HKD",
            provider="yfinance",
        )

    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_yf_for_cd,
        fx_fetcher=_failing_fx,
        today_fn=lambda: date(2026, 11, 26),
    )
    svc = metrics_mod.WatchlistMetricsService(consensus_adapter=adapter)

    # 1. Cold call queues warmup; background fetches empty FX for 0700.HK and non-429 error for ERRCO
    r1 = await svc.get_metrics_for_user("u-cd", wait_for_cold=False)
    assert set(r1["pending_valuation_symbols"]) == {"0700.HK", "ERRCO"}
    await asyncio.sleep(0.05)

    # 2. Next poll: BOTH entered cooldown -> explicit no_coverage status AND pending_valuation_symbols is EMPTY!
    r2 = await svc.get_metrics_for_user("u-cd", wait_for_cold=False)
    assert r2["pending_valuation_symbols"] == []
    assert r2["items"]["0700.HK"]["valuation"]["status"] == "no_coverage"
    assert r2["items"]["ERRCO"]["valuation"]["status"] == "no_coverage"

    # 3. Stale FX + non-429 FX error -> enters FX cooldown, returns stale valuation with pending_valuation_symbols == []!
    consensus_mod._FX_ERROR_COOLDOWN.clear()
    fx_mode["mode"] = "error"
    fx_key = consensus_mod._fx_cache_key("CNY", "HKD")
    consensus_mod._FX_CACHE[fx_key] = (
        time.time() - consensus_mod.FX_CACHE_TTL_SECONDS - 120,
        1.08,
    )
    r3 = await svc.get_metrics_for_user("u-cd", wait_for_cold=False)
    assert r3["items"]["0700.HK"]["valuation"]["status"] == "stale"
    assert r3["pending_valuation_symbols"] == ["0700.HK"]
    await asyncio.sleep(0.05)
    r4 = await svc.get_metrics_for_user("u-cd", wait_for_cold=False)
    assert r4["items"]["0700.HK"]["valuation"]["status"] == "stale"
    assert r4["pending_valuation_symbols"] == []


@pytest.mark.asyncio
async def test_yahoo_empty_record_plus_fmp_429_enters_rate_limit_cooldown() -> None:
    """When Yahoo returns an empty RawConsensusRecord (no EPS) and FMP raises 429, rate-limit status & cooldown are preserved."""
    fmp_mock = create_autospec(FMPClient, instance=True)
    fmp_mock.get_analyst_estimates.side_effect = FmpQuotaExhausted()

    yf_calls = {"n": 0}

    async def _empty_yf_record(sym: str) -> consensus_mod.RawConsensusRecord:
        yf_calls["n"] += 1
        return consensus_mod.RawConsensusRecord(
            symbol=sym,
            fy1_eps=None,
            fy2_eps=None,
            quote_currency="USD",
            provider="yfinance",
        )

    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_empty_yf_record,
        fmp_client=fmp_mock,
        today_fn=lambda: date(2026, 11, 26),
    )
    res = await adapter.get_valuation("RLFALL", latest_price=100.0, quote_currency="USD")
    assert res["status"] == "rate_limited"
    assert res["pe_fy1"] is None
    assert yf_calls["n"] == 1

    # Non-blocking cached view reads rate_limited cooldown and does NOT request warmup!
    cached_val, needs_warm = await adapter.get_cached_valuation(
        "RLFALL", latest_price=100.0, quote_currency="USD"
    )
    assert cached_val is not None and cached_val["status"] == "rate_limited"
    assert needs_warm is False


@pytest.mark.asyncio
async def test_yahoo_ticker_mapping_and_canonical_cache_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consensus reuses yahoo_ticker() (600519.SH -> 600519.SS, BRK.B -> BRK-B) and deduplicates cache by canonical repo symbol."""
    import pandas as pd
    import yfinance as yf
    import src.data_client.yfinance.yahoo as yahoo_mod

    real_ticker_cls = yf.Ticker
    seen_yf_symbols: list[str] = []

    def _fake_yf_ticker(sym: str) -> Any:
        seen_yf_symbols.append(sym)
        t = create_autospec(real_ticker_cls, instance=True)
        t.earnings_estimate = pd.DataFrame(
            {"avg": [70.0, 80.0], "numberOfAnalysts": [25, 22]},
            index=["0y", "+1y"],
        )
        t._analysis = type(
            "_A",
            (),
            {
                "_earnings_trend": [
                    {"period": "0y", "endDate": "2027-12-31"},
                    {"period": "+1y", "endDate": "2028-12-31"},
                ]
            },
        )()
        ccy = "CNY" if sym.endswith(".SS") else "USD"
        t.info = {
            "currency": ccy,
            "financialCurrency": ccy,
            "quoteType": "EQUITY",
            "longName": sym,
        }
        return t

    monkeypatch.setattr(yahoo_mod.yf, "Ticker", create_autospec(real_ticker_cls, side_effect=_fake_yf_ticker))

    adapter = consensus_mod.WatchlistConsensusAdapter(today_fn=lambda: date(2026, 11, 26))

    # 1. 600519.SH calls yf.Ticker("600519.SS") and caches under canonical "600519.SH"
    v_sh = await adapter.get_valuation("600519.SH", latest_price=1400.0, quote_currency="CNY")
    assert seen_yf_symbols == ["600519.SS"]
    assert v_sh["status"] == "ok"
    assert v_sh["ticker"] == "600519.SH"
    assert v_sh["pe_fy1"] == 20.0

    # Querying via Yahoo spelling "600519.SS" hits the exact same canonical cache with ZERO extra upstream calls!
    v_ss = await adapter.get_valuation("600519.SS", latest_price=1400.0, quote_currency="CNY")
    assert seen_yf_symbols == ["600519.SS"]
    assert v_ss["ticker"] == "600519.SH"
    assert v_ss["pe_fy1"] == 20.0

    # 2. BRK.B calls yf.Ticker("BRK-B") and caches under canonical "BRK-B"
    v_dot = await adapter.get_valuation("BRK.B", latest_price=420.0, quote_currency="USD")
    assert seen_yf_symbols == ["600519.SS", "BRK-B"]
    assert v_dot["status"] == "ok"
    assert v_dot["ticker"] == "BRK-B"
    assert v_dot["pe_fy1"] == 6.0

    # Querying "BRK-B" hits the same canonical cache with ZERO extra upstream calls!
    v_dash = await adapter.get_valuation("BRK-B", latest_price=420.0, quote_currency="USD")
    assert seen_yf_symbols == ["600519.SS", "BRK-B"]
    assert v_dash["pe_fy1"] == 6.0


@pytest.mark.asyncio
async def test_known_adr_identity_separate_from_verified_ratio_shel_plain_name_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known ADR identity is stored separately from verified ratios; SHEL ('Shell plc', USD/USD) and ratio-removed ADRs stay unit_unverified."""
    assert "SHEL" in consensus_mod.KNOWN_ADR_SYMBOLS
    assert "SHEL" not in consensus_mod.VERIFIED_ADR_SHARES_PER_UNIT

    # Even if a ticker's ratio is deleted from VERIFIED_ADR_SHARES_PER_UNIT, KNOWN_ADR_SYMBOLS preserves ADR identity.
    monkeypatch.delitem(consensus_mod.VERIFIED_ADR_SHARES_PER_UNIT, "TM")
    assert "TM" in consensus_mod.KNOWN_ADR_SYMBOLS

    records = {
        "SHEL": consensus_mod.RawConsensusRecord(
            symbol="SHEL",
            fy1_eps=7.0,
            fy2_eps=7.5,
            fy1_end="2026-12-31",
            fy2_end="2027-12-31",
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=18,
            provider="yfinance",
            quote_type="EQUITY",
            security_name="Shell plc",
        ),
        "TM": consensus_mod.RawConsensusRecord(
            symbol="TM",
            fy1_eps=15.0,
            fy2_eps=16.0,
            fy1_end="2027-03-31",
            fy2_end="2028-03-31",
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=18,
            provider="yfinance",
            quote_type="EQUITY",
            security_name="Toyota Motor Corporation",
        ),
    }
    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=lambda s: records.get(s),
        today_fn=lambda: date(2026, 11, 26),
    )

    shel = await adapter.get_valuation("SHEL", latest_price=70.0, quote_currency="USD")
    assert shel["status"] == "unit_unverified"
    assert shel["unit_verified"] is False
    assert shel["pe_fy1"] is None

    tm = await adapter.get_valuation("TM", latest_price=180.0, quote_currency="USD")
    assert tm["status"] == "unit_unverified"
    assert tm["unit_verified"] is False
    assert tm["pe_fy1"] is None


@pytest.mark.asyncio
async def test_singleflight_lease_renewed_across_slot_queue_blocks_second_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Singleflight lock is renewed while waiting for a semaphore slot (>3s equivalent of 15s lease) and during upstream fetch so another worker never sends a duplicate request."""
    fake_redis, _kv, locks = _make_ttl_redis_mock()
    monkeypatch.setattr(
        consensus_mod,
        "_get_redis_cache_client",
        create_autospec(consensus_mod._get_redis_cache_client, return_value=fake_redis),
    )
    # Scale 15s lease / 12s hard deadline / >3s queue wait to fast test durations with exact same overrun ratio:
    # initial lease=240ms, queue wait=260ms (>100% of initial lease, well past 3s/15s=20%), request=200ms -> total 460ms >> 240ms.
    monkeypatch.setattr(consensus_mod, "_REDIS_INFLIGHT_LOCK_TTL_MS", 240)
    monkeypatch.setattr(consensus_mod, "_REDIS_INFLIGHT_WAIT_SECONDS", 0.15)
    monkeypatch.setattr(consensus_mod, "_REDIS_INFLIGHT_POLL_SECONDS", 0.02)
    monkeypatch.setattr(consensus_mod, "_REDIS_SEM_WAIT_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(consensus_mod, "_REDIS_SEM_POLL_SECONDS", 0.02)
    monkeypatch.setattr(consensus_mod, "_UPSTREAM_SLOT_HARD_TIMEOUT_SECONDS", 1.5)

    # Occupy all 8 Redis semaphore slots so Worker 1 must wait in the queue past the initial inflight lease TTL
    far_exp = time.monotonic() + 30.0
    for i in range(consensus_mod.MAX_CONSENSUS_CONCURRENCY):
        locks[f"{consensus_mod._REDIS_SEM_KEY_PREFIX}{i}"] = (far_exp, f"busy-other-{i}")

    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()
    fetch_calls = {"n": 0}

    async def _slow_yf(sym: str) -> consensus_mod.RawConsensusRecord:
        fetch_calls["n"] += 1
        fetch_started.set()
        await release_fetch.wait()
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

    adapter_w1 = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_slow_yf,
        today_fn=lambda: date(2026, 11, 26),
    )
    adapter_w2 = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_slow_yf,
        today_fn=lambda: date(2026, 11, 26),
    )

    sym_key = consensus_mod._consensus_cache_key("QWAIT")
    inflight_key = f"{sym_key}:inflight"

    w1_task = asyncio.create_task(adapter_w1.get_valuation("QWAIT", latest_price=100.0, quote_currency="USD"))

    # Keep all 8 slots full past the initial 240ms inflight lease (equivalent to >3s queue wait on 15s lease)
    await asyncio.sleep(0.28)
    assert not fetch_started.is_set()
    assert inflight_key in locks
    assert fake_redis.extend_lock.call_count >= 2

    # Free one slot so Worker 1 enters the upstream request
    locks.pop(f"{consensus_mod._REDIS_SEM_KEY_PREFIX}0", None)
    await asyncio.wait_for(fetch_started.wait(), timeout=1.0)

    # Simulate a second worker process attempting to fetch the same symbol while Worker 1 is past initial lease
    consensus_mod._INFLIGHT_CONSENSUS.clear()
    consensus_mod._CONSENSUS_CACHE.clear()
    w2_res = await adapter_w2.get_valuation("QWAIT", latest_price=100.0, quote_currency="USD")
    assert fetch_calls["n"] == 1  # Worker 2 did NOT send a duplicate upstream request!
    assert w2_res["status"] == "loading"

    # Let Worker 1 finish and verify clean result + released lock
    release_fetch.set()
    w1_res = await w1_task
    assert w1_res["status"] == "ok"
    assert w1_res["pe_fy1"] == 20.0
    assert fetch_calls["n"] == 1
    assert inflight_key not in locks


@pytest.mark.asyncio
async def test_yfinance_to_thread_timeout_keeps_slot_held_until_thread_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a yfinance worker thread exceeds timeout, the caller stops waiting but the semaphore slot stays held (and renewed) until the thread exits."""
    fake_redis, _kv, locks = _make_ttl_redis_mock()
    monkeypatch.setattr(
        consensus_mod,
        "_get_redis_cache_client",
        create_autospec(consensus_mod._get_redis_cache_client, return_value=fake_redis),
    )
    monkeypatch.setattr(consensus_mod, "CONSENSUS_FETCH_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(consensus_mod, "_UPSTREAM_SLOT_HARD_TIMEOUT_SECONDS", 0.14)
    monkeypatch.setattr(consensus_mod, "_REDIS_SEM_LEASE_MS", 180)

    thread_entered = threading.Event()
    release_thread = threading.Event()

    def _blocking_yf_sync(
        symbol: str,
        *,
        today: date | None = None,
        last_reported_fy_end: str | None = None,
    ) -> tuple[consensus_mod.RawConsensusRecord | None, bool]:
        _ = (today, last_reported_fy_end)
        thread_entered.set()
        release_thread.wait(timeout=5.0)
        return (
            consensus_mod.RawConsensusRecord(
                symbol=symbol,
                fy1_eps=5.0,
                fy1_end="2027-12-31",
                estimate_currency="USD",
                quote_currency="USD",
                provider="yfinance",
            ),
            False,
        )

    monkeypatch.setattr(
        consensus_mod,
        "_fetch_yfinance_consensus_result_sync",
        create_autospec(
            consensus_mod._fetch_yfinance_consensus_result_sync,
            side_effect=_blocking_yf_sync,
        ),
    )

    adapter = consensus_mod.WatchlistConsensusAdapter(today_fn=lambda: date(2026, 11, 26))
    try:
        res = await adapter.get_valuation("SLOWTH", latest_price=100.0, quote_currency="USD")
        assert thread_entered.is_set()
        assert res["status"] == "no_coverage"

        # Immediately after caller timeout, thread is still running -> BOTH Redis slot and local semaphore stay held!
        sem_slots = [k for k in locks if k.startswith(consensus_mod._REDIS_SEM_KEY_PREFIX)]
        assert len(sem_slots) == 1
        local_sem = consensus_mod._get_consensus_semaphore()
        assert local_sem._value == consensus_mod.MAX_CONSENSUS_CONCURRENCY - 1

        # Even past the initial 180ms Redis slot lease, renewal keeps the slot held while thread is still alive
        await asyncio.sleep(0.24)
        sem_slots_after_lease = [k for k in locks if k.startswith(consensus_mod._REDIS_SEM_KEY_PREFIX)]
        assert len(sem_slots_after_lease) == 1
        assert local_sem._value == consensus_mod.MAX_CONSENSUS_CONCURRENCY - 1
    finally:
        release_thread.set()

    # Wait explicitly for deferred slot-release tasks to finish after the thread exits
    if consensus_mod._PENDING_SLOT_RELEASES:
        await asyncio.gather(*list(consensus_mod._PENDING_SLOT_RELEASES))

    assert [k for k in locks if k.startswith(consensus_mod._REDIS_SEM_KEY_PREFIX)] == []
    assert consensus_mod._get_consensus_semaphore()._value == consensus_mod.MAX_CONSENSUS_CONCURRENCY


@pytest.mark.asyncio
async def test_concurrent_workers_reread_shared_cooldown_before_upstream_consensus_and_fx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When two workers warm up concurrently and Worker 1 writes failure/429 cooldown, Worker 2 re-reads shared cooldown after acquiring lock and makes ZERO upstream requests (consensus + FX)."""
    fake_redis, _kv, _locks = _make_ttl_redis_mock()
    monkeypatch.setattr(
        consensus_mod,
        "_get_redis_cache_client",
        create_autospec(consensus_mod._get_redis_cache_client, return_value=fake_redis),
    )
    monkeypatch.setattr(consensus_mod, "_REDIS_INFLIGHT_POLL_SECONDS", 0.01)

    # 1. Consensus concurrent warmup: Worker 1 fails -> writes shared cooldown -> Worker 2 does NOT call upstream!
    c_entered = asyncio.Event()
    c_release = asyncio.Event()
    c_calls = {"n": 0}

    async def _failing_yf(_sym: str) -> consensus_mod.RawConsensusRecord:
        c_calls["n"] += 1
        c_entered.set()
        await c_release.wait()
        raise consensus_mod.ConsensusRateLimitError("HTTP 429")

    adapter_w1 = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_failing_yf,
        today_fn=lambda: date(2026, 11, 26),
    )
    adapter_w2 = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_failing_yf,
        today_fn=lambda: date(2026, 11, 26),
    )

    adapter_w1.schedule_warmup("CDW1")
    w1_task = consensus_mod._INFLIGHT_WARMUP["CDW1"]
    await asyncio.wait_for(c_entered.wait(), timeout=1.0)

    # Simulate a second worker process (separate in-process maps, shared Redis)
    consensus_mod._INFLIGHT_WARMUP.clear()
    consensus_mod._INFLIGHT_CONSENSUS.clear()
    consensus_mod._CONSENSUS_ERROR_COOLDOWN.clear()
    adapter_w2.schedule_warmup("CDW1")
    w2_task = consensus_mod._INFLIGHT_WARMUP["CDW1"]

    await asyncio.sleep(0.03)
    c_release.set()
    await asyncio.gather(w1_task, w2_task)
    assert c_calls["n"] == 1

    # Direct get_valuation on Worker 2 also respects shared rate_limited cooldown with ZERO upstream calls
    consensus_mod._CONSENSUS_ERROR_COOLDOWN.clear()
    val_w2 = await adapter_w2.get_valuation("CDW1", latest_price=100.0, quote_currency="USD")
    assert c_calls["n"] == 1
    assert val_w2["status"] == "rate_limited"

    # 2. FX concurrent fetch: Worker 1 fails with 429 -> writes shared FX cooldown -> Worker 2 re-reads and does NOT call upstream!
    fx_entered = asyncio.Event()
    fx_release = asyncio.Event()
    fx_calls = {"n": 0}

    async def _failing_fx(src: str, dst: str) -> float | None:
        _ = (src, dst)
        fx_calls["n"] += 1
        fx_entered.set()
        await fx_release.wait()
        raise consensus_mod.ConsensusRateLimitError("FX 429")

    fx_w1 = consensus_mod.WatchlistConsensusAdapter(
        fx_fetcher=_failing_fx,
        today_fn=lambda: date(2026, 11, 26),
    )
    fx_w2 = consensus_mod.WatchlistConsensusAdapter(
        fx_fetcher=_failing_fx,
        today_fn=lambda: date(2026, 11, 26),
    )

    fx_task1 = asyncio.create_task(fx_w1._fetch_spot_fx_rate_with_status("JPY", "USD"))
    await asyncio.wait_for(fx_entered.wait(), timeout=1.0)

    consensus_mod._INFLIGHT_FX.clear()
    consensus_mod._FX_ERROR_COOLDOWN.clear()
    fx_task2 = asyncio.create_task(fx_w2._fetch_spot_fx_rate_with_status("JPY", "USD"))

    await asyncio.sleep(0.03)
    fx_release.set()
    r1, r2 = await asyncio.gather(fx_task1, fx_task2)
    assert fx_calls["n"] == 1
    assert r1 == (None, False, True, False)
    assert r2 == (None, False, True, False)


@pytest.mark.asyncio
async def test_short_cooldown_for_empty_missing_currency_partial_429_and_undated_fy_not_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty results, temporarily missing currency, and partial results after FMP 429 use short cooldown (no 12h cache) while preserving available year; undated FYs are never fresh."""
    import pandas as pd
    import yfinance as yf
    import src.data_client.yfinance.yahoo as yahoo_mod

    # 1. Ordinary empty result (no EPS, no error, no rollover) -> short cooldown, NO 12h cache
    fmp_empty = create_autospec(FMPClient, instance=True)
    fmp_empty.get_analyst_estimates.return_value = []
    adapter_empty = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=lambda s: consensus_mod.RawConsensusRecord(
            symbol=s, quote_currency="USD", provider="yfinance"
        ),
        fmp_client=fmp_empty,
        today_fn=lambda: date(2026, 11, 26),
    )
    empty_val = await adapter_empty.get_valuation("EMPTYCO", latest_price=50.0, quote_currency="USD")
    assert empty_val["status"] == "no_coverage"
    assert consensus_mod._consensus_cache_key("EMPTYCO") not in consensus_mod._CONSENSUS_CACHE
    assert await consensus_mod._read_consensus_cooldown("EMPTYCO") == "empty"
    cached_empty, needs_warm_empty = await adapter_empty.get_cached_valuation(
        "EMPTYCO", latest_price=50.0, quote_currency="USD"
    )
    assert cached_empty is not None and cached_empty["status"] == "no_coverage"
    assert needs_warm_empty is False

    # 2. Temporarily missing currency (FMP income statement fails non-429) -> short cooldown, NO 12h cache, unit_unverified displayed
    fmp_no_inc = create_autospec(FMPClient, instance=True)
    fmp_no_inc.get_analyst_estimates.return_value = [
        {"date": "2027-12-31", "epsAvg": 25.0, "numAnalystsEps": 30},
    ]
    fmp_no_inc.get_income_statement.side_effect = RuntimeError("FMP income 503")
    fmp_no_inc.get_profile.return_value = [
        {"symbol": "NOCCY", "currency": "HKD", "companyName": "NoCcy Corp", "isAdr": False}
    ]
    adapter_no_ccy = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=lambda _s: None,
        fmp_client=fmp_no_inc,
        today_fn=lambda: date(2026, 11, 26),
    )
    no_ccy_val = await adapter_no_ccy.get_valuation("NOCCY", latest_price=100.0, quote_currency="HKD")
    assert no_ccy_val["status"] == "unit_unverified"
    assert no_ccy_val["estimate_currency"] is None
    assert consensus_mod._consensus_cache_key("NOCCY") not in consensus_mod._CONSENSUS_CACHE
    assert await consensus_mod._read_consensus_cooldown("NOCCY") == "missing_currency"
    cached_no_ccy, needs_warm_no_ccy = await adapter_no_ccy.get_cached_valuation(
        "NOCCY", latest_price=100.0, quote_currency="HKD"
    )
    assert cached_no_ccy is not None and cached_no_ccy["status"] == "unit_unverified"
    assert needs_warm_no_ccy is False

    # 3. Partial result (Yahoo FY1 OK, missing FY2, FMP hits 429) -> short cooldown, NO 12h cache, FY1 still shown normally!
    fmp_429 = create_autospec(FMPClient, instance=True)
    fmp_429.get_analyst_estimates.side_effect = FmpQuotaExhausted()
    adapter_partial = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=lambda s: consensus_mod.RawConsensusRecord(
            symbol=s,
            fy1_eps=4.5,
            fy2_eps=None,
            fy1_end="2027-01-31",
            fy2_end="2028-01-31",
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=35,
            provider="yfinance",
        ),
        fmp_client=fmp_429,
        today_fn=lambda: date(2026, 11, 26),
    )
    part_val = await adapter_partial.get_valuation("PART429", latest_price=135.0, quote_currency="USD")
    assert part_val["status"] == "ok"
    assert part_val["pe_fy1"] == 30.0
    assert part_val["pe_fy2"] is None
    assert consensus_mod._consensus_cache_key("PART429") not in consensus_mod._CONSENSUS_CACHE
    assert await consensus_mod._read_consensus_cooldown("PART429") == "partial_rate_limited"

    # During short cooldown, cached view still displays FY1 normally without looping warmup
    cached_part, needs_warm_part = await adapter_partial.get_cached_valuation(
        "PART429", latest_price=135.0, quote_currency="USD"
    )
    assert cached_part is not None and cached_part["status"] == "ok"
    assert cached_part["pe_fy1"] == 30.0
    assert cached_part["pe_fy2"] is None
    assert needs_warm_part is False

    # Once short cooldown expires, cached view requests warmup again so missing FY2 can retry
    consensus_mod._CONSENSUS_ERROR_COOLDOWN.clear()
    cached_after_cd, needs_warm_after_cd = await adapter_partial.get_cached_valuation(
        "PART429", latest_price=135.0, quote_currency="USD"
    )
    assert cached_after_cd is None
    assert needs_warm_after_cd is True

    # 4. Undated fiscal years are never treated as fresh:
    # 4a) Yahoo 0y has EPS but no endDate while +1y has a valid endDate -> undated 0y dropped, +1y becomes FY1, FMP fills FY2
    from yfinance.scrapers.analysis import Analysis as YfAnalysis

    fake_ticker = create_autospec(yf.Ticker, instance=True)
    fake_ticker.earnings_estimate = pd.DataFrame(
        {"avg": [4.0, 5.5], "numberOfAnalysts": [30, 28]},
        index=["0y", "+1y"],
    )
    fake_analysis = create_autospec(YfAnalysis, instance=True)
    fake_analysis._earnings_trend = [
        {"period": "0y"},  # missing endDate!
        {"period": "+1y", "endDate": "2027-12-31"},
    ]
    fake_ticker._analysis = fake_analysis
    fake_ticker.info = {
        "currency": "USD",
        "financialCurrency": "USD",
        "quoteType": "EQUITY",
        "longName": "Undated ZeroY Corp",
    }
    monkeypatch.setattr(
        yahoo_mod,
        "yahoo_ticker",
        create_autospec(yahoo_mod.yahoo_ticker, return_value=fake_ticker),
    )

    fmp_fill = create_autospec(FMPClient, instance=True)
    fmp_fill.get_analyst_estimates.return_value = [
        {"date": "2027-12-31", "epsAvg": 5.4, "numAnalystsEps": 28},
        {"date": "2028-12-31", "epsAvg": 7.0, "numAnalystsEps": 25},
    ]
    fmp_fill.get_income_statement.return_value = [
        {"date": "2025-12-31", "reportedCurrency": "USD"}
    ]
    fmp_fill.get_profile.return_value = [
        {"symbol": "UNDAT", "currency": "USD", "companyName": "Undated ZeroY Corp", "isAdr": False}
    ]
    adapter_undated = consensus_mod.WatchlistConsensusAdapter(
        fmp_client=fmp_fill,
        today_fn=lambda: date(2026, 11, 26),
    )
    undated_val = await adapter_undated.get_valuation("UNDAT", latest_price=110.0, quote_currency="USD")
    assert undated_val["status"] == "ok"
    assert undated_val["fy1_end"] == "2027-12-31"
    assert undated_val["fy1_eps"] == 5.5
    assert undated_val["fy2_end"] == "2028-12-31"
    assert undated_val["fy2_eps"] == 7.0
    assert undated_val["provider"] == "yfinance+fmp"

    # 4b) Cached record lacking fy1_end is NEVER considered fresh!
    undated_key = consensus_mod._consensus_cache_key("NOCUTOFF")
    consensus_mod._CONSENSUS_CACHE[undated_key] = (
        time.time(),
        consensus_mod.RawConsensusRecord(
            symbol="NOCUTOFF",
            fy1_eps=5.0,
            fy1_end=None,
            estimate_currency="USD",
            quote_currency="USD",
            provider="yfinance",
        ).to_dict(),
    )
    cached_rec, is_fresh = await consensus_mod._read_consensus_cache(
        "NOCUTOFF", today=date(2026, 11, 26)
    )
    assert cached_rec is not None
    assert is_fresh is False


@pytest.mark.asyncio
async def test_slot_queue_timeout_keeps_pending_without_cooldown_while_issued_failure_enters_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5-1: Cold-start symbols timing out while waiting for a local/Redis slot stay pending (no failure cooldown, never `no_coverage`); only issued upstream failures write cooldown."""
    from src.data_client.market_data_provider import MarketDataProvider

    symbols = [f"SYM{i}" for i in range(consensus_mod.MAX_CONSENSUS_CONCURRENCY)] + ["Q8", "Q9"]
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(
            coverage_db.list_coverage_rows,
            return_value=[
                {"user_id": "u-q", "ticker": s, "tier": "watch", "is_private": False}
                for s in symbols
            ],
        ),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(coverage_db.get_earnings_calendar_mirror, return_value=None),
    )

    fake_provider = create_autospec(MarketDataProvider, instance=True)

    async def _get_snapshots(
        syms: Any, *, asset_type: str = "stocks", user_id: str = ""
    ) -> list[dict[str, Any]]:
        _ = (asset_type, user_id)
        return [
            {"symbol": s, "price": 100.0, "previous_close": 100.0, "currency": "USD"}
            for s in syms
        ]

    async def _get_daily(
        symbol: str,
        *,
        from_date: str,
        to_date: str,
        is_index: bool = False,
        user_id: str = "",
    ) -> list[dict[str, Any]]:
        _ = (symbol, from_date, to_date, is_index, user_id)
        return [
            {
                "date": (date(2026, 1, 2) + timedelta(days=i)).isoformat(),
                "close": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.0 + i,
                "volume": 1000,
            }
            for i in range(40)
        ]

    fake_provider.get_snapshots.side_effect = _get_snapshots
    fake_provider.get_daily.side_effect = _get_daily
    monkeypatch.setattr(
        metrics_mod,
        "_resolve_provider_and_key",
        create_autospec(
            metrics_mod._resolve_provider_and_key,
            return_value=(fake_provider, "yfinance", True),
        ),
    )

    # Short local slot wait timeout so Q8/Q9 time out in the queue while SYM0..SYM7 hold all 8 slots
    monkeypatch.setattr(consensus_mod, "_REDIS_SEM_WAIT_TIMEOUT_SECONDS", 0.08)

    release_first_batch = asyncio.Event()
    all_eight_entered = asyncio.Event()
    issued_symbols: list[str] = []

    async def _yf_for_queue(sym: str) -> consensus_mod.RawConsensusRecord:
        issued_symbols.append(sym)
        if sym.startswith("SYM"):
            if len([s for s in issued_symbols if s.startswith("SYM")]) >= consensus_mod.MAX_CONSENSUS_CONCURRENCY:
                all_eight_entered.set()
            await release_first_batch.wait()
            if sym == "SYM0":
                raise RuntimeError("Issued upstream 500 failure")
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

    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_yf_for_queue,
        today_fn=lambda: date(2026, 11, 26),
    )
    svc = metrics_mod.WatchlistMetricsService(consensus_adapter=adapter)

    # 1. Cold full-table fetch schedules all 10 symbols; 8 occupy local slots and Q8/Q9 queue behind them
    res1 = await svc.get_metrics_for_user("u-q", wait_for_cold=False)
    assert set(res1["pending_valuation_symbols"]) == set(symbols)
    await asyncio.wait_for(all_eight_entered.wait(), timeout=1.0)

    # Wait past local slot wait timeout (0.08s) while all 8 slots are still occupied
    await asyncio.sleep(0.14)
    assert "Q8" not in issued_symbols
    assert "Q9" not in issued_symbols
    # Queued symbols MUST NOT have entered failure cooldown!
    assert await consensus_mod._read_consensus_cooldown("Q8") is None
    assert await consensus_mod._read_consensus_cooldown("Q9") is None

    # 2. Next poll while queue is still congested: Q8/Q9 remain in pending_valuation_symbols and do NOT show `no_coverage`!
    res2 = await svc.get_metrics_for_user("u-q", wait_for_cold=False)
    assert "Q8" in res2["pending_valuation_symbols"]
    assert "Q9" in res2["pending_valuation_symbols"]
    assert res2["items"]["Q8"]["valuation"] is None
    assert res2["items"]["Q9"]["valuation"] is None

    # Direct wait_for_cold/get_valuation while slots are full returns loading (pending=True), NOT no_coverage
    sync_queued = await adapter.get_valuation("QSYNC", latest_price=100.0, quote_currency="USD")
    assert sync_queued["status"] == "loading"
    assert sync_queued.get("pending") is True
    assert await consensus_mod._read_consensus_cooldown("QSYNC") is None

    # 3. Release the first 8 slots: SYM0 (issued failure) enters cooldown -> no_coverage; SYM1..SYM7 & Q8/Q9 warm cleanly!
    release_first_batch.set()
    await asyncio.sleep(0.12)
    assert await consensus_mod._read_consensus_cooldown("SYM0") == "error"

    res3 = await svc.get_metrics_for_user("u-q", wait_for_cold=False)
    assert "SYM0" not in res3["pending_valuation_symbols"]
    assert res3["items"]["SYM0"]["valuation"]["status"] == "no_coverage"
    # Let any just-rescheduled warmup finish if Q8/Q9 hadn't completed yet
    if res3["pending_valuation_symbols"]:
        await asyncio.sleep(0.12)
        res3 = await svc.get_metrics_for_user("u-q", wait_for_cold=False)
    assert res3["pending_valuation_symbols"] == []
    assert res3["items"]["Q8"]["valuation"]["status"] == "ok"
    assert res3["items"]["Q8"]["valuation"]["pe_fy1"] == 20.0
    assert res3["items"]["Q9"]["valuation"]["status"] == "ok"
    assert res3["items"]["Q9"]["valuation"]["pe_fy1"] == 20.0

    # 4. Cross-process Redis semaphore congestion also stays loading without writing cooldown
    fake_redis, _kv, locks = _make_ttl_redis_mock()
    monkeypatch.setattr(
        consensus_mod,
        "_get_redis_cache_client",
        create_autospec(consensus_mod._get_redis_cache_client, return_value=fake_redis),
    )
    monkeypatch.setattr(consensus_mod, "_REDIS_SEM_POLL_SECONDS", 0.02)
    far_exp = time.monotonic() + 30.0
    for i in range(consensus_mod.MAX_CONSENSUS_CONCURRENCY):
        locks[f"{consensus_mod._REDIS_SEM_KEY_PREFIX}{i}"] = (far_exp, f"other-worker-{i}")

    redis_queued = await adapter.get_valuation("QREDIS", latest_price=100.0, quote_currency="USD")
    assert redis_queued["status"] == "loading"
    assert redis_queued.get("pending") is True
    assert await consensus_mod._read_consensus_cooldown("QREDIS") is None


@pytest.mark.asyncio
async def test_undated_yahoo_0y_fmp_fills_earlier_eligible_fy1_by_merged_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5-2: When Yahoo `0y` lacks endDate while `+1y` is dated, merging eligible end dates across Yahoo+FMP assigns FMP's earlier eligible fiscal year as FY1 and keeps Yahoo `+1y` as FY2."""
    import pandas as pd
    import yfinance as yf
    from yfinance.scrapers.analysis import Analysis as YfAnalysis
    import src.data_client.yfinance.yahoo as yahoo_mod

    fake_ticker = create_autospec(yf.Ticker, instance=True)
    fake_ticker.earnings_estimate = pd.DataFrame(
        {"avg": [4.0, 5.5], "numberOfAnalysts": [30, 28]},
        index=["0y", "+1y"],
    )
    fake_analysis = create_autospec(YfAnalysis, instance=True)
    fake_analysis._earnings_trend = [
        {"period": "0y"},  # missing endDate!
        {"period": "+1y", "endDate": "2027-12-31"},
    ]
    fake_ticker._analysis = fake_analysis
    fake_ticker.info = {
        "currency": "USD",
        "financialCurrency": "USD",
        "quoteType": "EQUITY",
        "longName": "Earlier FY1 Corp",
    }
    monkeypatch.setattr(
        yahoo_mod,
        "yahoo_ticker",
        create_autospec(yahoo_mod.yahoo_ticker, return_value=fake_ticker),
    )

    fmp_mock = create_autospec(FMPClient, instance=True)
    fmp_mock.get_analyst_estimates.return_value = [
        {"date": "2026-12-31", "epsAvg": 4.2, "numAnalystsEps": 32},  # earlier & eligible on 2026-11-26!
        {"date": "2027-12-31", "epsAvg": 5.4, "numAnalystsEps": 28},  # matches Yahoo +1y (Yahoo preferred -> 5.5)
        {"date": "2028-12-31", "epsAvg": 7.0, "numAnalystsEps": 25},  # FY3 -> must NOT become FY2!
    ]
    fmp_mock.get_income_statement.return_value = [
        {"date": "2025-12-31", "reportedCurrency": "USD"}
    ]
    fmp_mock.get_profile.return_value = [
        {"symbol": "EARLIER", "currency": "USD", "companyName": "Earlier FY1 Corp", "isAdr": False}
    ]

    adapter = consensus_mod.WatchlistConsensusAdapter(
        fmp_client=fmp_mock,
        today_fn=lambda: date(2026, 11, 26),
    )
    val = await adapter.get_valuation("EARLIER", latest_price=110.0, quote_currency="USD")
    assert val["status"] == "ok"
    assert val["fy1_end"] == "2026-12-31"
    assert val["fy1_eps"] == 4.2
    assert val["fy2_end"] == "2027-12-31"
    assert val["fy2_eps"] == 5.5
    assert val["pe_fy1"] == round(110.0 / 4.2, 2)
    assert val["pe_fy2"] == 20.0
    assert val["provider"] == "yfinance+fmp"

    # Also verify 52/53-week ±15-day alignment when Yahoo +1y is 2027-01-31 and FMP has 2026-12-31 + 2027-01-29 + 2028-01-29
    merged_52w = consensus_mod._merge_partial_consensus_record(
        consensus_mod.RawConsensusRecord(
            symbol="W52",
            fy1_eps=5.5,
            fy2_eps=None,
            fy1_end="2027-01-31",
            fy2_end=None,
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=20,
            provider="yfinance",
        ),
        consensus_mod.RawConsensusRecord(
            symbol="W52",
            fy1_eps=4.2,
            fy2_eps=5.4,
            fy1_end="2026-12-31",
            fy2_end="2027-01-29",
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=22,
            provider="fmp",
        ),
        fallback_annual_rows=[
            {"date": "2026-12-31", "epsAvg": 4.2, "numAnalystsEps": 22},
            {"date": "2027-01-29", "epsAvg": 5.4, "numAnalystsEps": 20},
            {"date": "2028-01-29", "epsAvg": 7.0, "numAnalystsEps": 18},
        ],
        today=date(2026, 11, 26),
        last_reported_fy_end="2025-12-31",
    )
    assert merged_52w.fy1_end == "2026-12-31"
    assert merged_52w.fy1_eps == 4.2
    assert merged_52w.fy2_end == "2027-01-31"
    assert merged_52w.fy2_eps == 5.5


@pytest.mark.asyncio
async def test_outer_slot_deadline_during_fmp_backfill_preserves_yahoo_year_with_partial_short_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5-3: When FMP is configured and the outer 12s slot deadline expires during FMP single-year backfill after Yahoo already returned 1 year, preserve that year with partial short cooldown (no 12h cache, never discarded as no_coverage)."""
    fake_redis, _kv, locks = _make_ttl_redis_mock()
    monkeypatch.setattr(
        consensus_mod,
        "_get_redis_cache_client",
        create_autospec(consensus_mod._get_redis_cache_client, return_value=fake_redis),
    )

    # Scale production 8s per-call / 12s outer slot deadline by 1/50 -> 0.16s / 0.24s:
    # Yahoo finishes in 0.12s (6s equiv) with FY1 only; FMP takes 1.0s -> outer 0.24s deadline fires
    # BEFORE FMP's own 0.16s timeout (which would finish at 0.28s).
    monkeypatch.setattr(consensus_mod, "CONSENSUS_FETCH_TIMEOUT_SECONDS", 0.16)
    monkeypatch.setattr(consensus_mod, "_UPSTREAM_SLOT_HARD_TIMEOUT_SECONDS", 0.24)

    async def _slow_partial_yf(sym: str) -> consensus_mod.RawConsensusRecord:
        await asyncio.sleep(0.12)
        return consensus_mod.RawConsensusRecord(
            symbol=sym,
            fy1_eps=4.5,
            fy2_eps=None,
            fy1_end="2027-01-31",
            fy2_end="2028-01-31",
            estimate_currency="USD",
            quote_currency="USD",
            analyst_count=35,
            provider="yfinance",
        )

    fmp_slow = create_autospec(FMPClient, instance=True)

    async def _hang_fmp(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(1.0)
        return []

    fmp_slow.get_analyst_estimates.side_effect = _hang_fmp

    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_slow_partial_yf,
        fmp_client=fmp_slow,
        today_fn=lambda: date(2026, 11, 26),
    )

    t0 = time.monotonic()
    val = await adapter.get_valuation("OUTER12", latest_price=135.0, quote_currency="USD")
    elapsed = time.monotonic() - t0

    # Outer deadline bounded the FMP backfill (FMP was actually called, and did not wait for its full 1.0s sleep)
    assert fmp_slow.get_analyst_estimates.call_count == 1
    assert elapsed < 0.35
    # Already-fetched Yahoo FY1 is preserved (NOT discarded into no_coverage!)
    assert val["status"] == "ok"
    assert val["pe_fy1"] == 30.0
    assert val["pe_fy2"] is None
    # Written to short cooldown (partial_error with snapshot), NOT 12h success cache
    assert consensus_mod._consensus_cache_key("OUTER12") not in consensus_mod._CONSENSUS_CACHE
    cd_kind, cd_rec = await consensus_mod._read_consensus_cooldown_entry("OUTER12")
    assert cd_kind == "partial_error"
    assert cd_rec is not None and cd_rec["fy1_eps"] == 4.5
    # No Redis semaphore slot leaked
    assert [k for k in locks if k.startswith(consensus_mod._REDIS_SEM_KEY_PREFIX)] == []

    # During short cooldown, cached view continues displaying FY1 without warmup loop
    cached_val, needs_warm = await adapter.get_cached_valuation(
        "OUTER12", latest_price=135.0, quote_currency="USD"
    )
    assert cached_val is not None and cached_val["status"] == "ok"
    assert cached_val["pe_fy1"] == 30.0
    assert cached_val["pe_fy2"] is None
    assert needs_warm is False

    # After short cooldown expires, cached view requests warmup again to retry FY2 backfill
    consensus_mod._CONSENSUS_ERROR_COOLDOWN.clear()
    await fake_redis.delete(consensus_mod._consensus_cooldown_redis_key("OUTER12"))
    after_cd, needs_warm_after = await adapter.get_cached_valuation(
        "OUTER12", latest_price=135.0, quote_currency="USD"
    )
    assert after_cd is None
    assert needs_warm_after is True


@pytest.mark.asyncio
async def test_earlier_fy_backfill_normalizes_subunit_currencies_and_blanks_unknown_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6-1: Per-source EPS currency units (GBp/GBP, ZAc/ZAR, ILA/ILS) are normalized before merging; indeterminate units render `—` (`unit_unverified`) for that year."""
    import pandas as pd
    import yfinance as yf
    from yfinance.scrapers.analysis import Analysis as YfAnalysis
    import src.data_client.yfinance.yahoo as yahoo_mod

    # 1. Exact regression: FMP FY1 £1 (GBP), Yahoo FY2 200p (GBp, 0y undated), price 2000p (GBp) -> FY1 P/E 20x, FY2 P/E 10x, EPS growth 100%
    fake_lse_ticker = create_autospec(yf.Ticker, instance=True)
    fake_lse_ticker.earnings_estimate = pd.DataFrame(
        {"avg": [100.0, 200.0], "numberOfAnalysts": [18, 16]},
        index=["0y", "+1y"],
    )
    fake_lse_analysis = create_autospec(YfAnalysis, instance=True)
    fake_lse_analysis._earnings_trend = [
        {"period": "0y"},  # missing endDate -> dropped so FMP backfills earlier eligible FY1
        {"period": "+1y", "endDate": "2027-12-31", "earningsEstimate": {"currency": "GBp"}},
    ]
    fake_lse_ticker._analysis = fake_lse_analysis
    fake_lse_ticker.info = {
        "currency": "GBp",
        "financialCurrency": "GBp",
        "quoteType": "EQUITY",
        "longName": "London Subunit Plc",
    }
    monkeypatch.setattr(
        yahoo_mod,
        "yahoo_ticker",
        create_autospec(yahoo_mod.yahoo_ticker, return_value=fake_lse_ticker),
    )

    fmp_gbp = create_autospec(FMPClient, instance=True)
    fmp_gbp.get_analyst_estimates.return_value = [
        {"date": "2026-12-31", "epsAvg": 1.0, "numAnalystsEps": 20},  # £1 in GBP
        {"date": "2027-12-31", "epsAvg": 2.05, "numAnalystsEps": 16},
    ]
    fmp_gbp.get_income_statement.return_value = [
        {"date": "2025-12-31", "reportedCurrency": "GBP"}
    ]
    fmp_gbp.get_profile.return_value = [
        {"symbol": "LSE1.L", "currency": "GBp", "companyName": "London Subunit Plc", "isAdr": False}
    ]

    adapter_lse = consensus_mod.WatchlistConsensusAdapter(
        fmp_client=fmp_gbp,
        today_fn=lambda: date(2026, 11, 26),
    )
    lse_val = await adapter_lse.get_valuation("LSE1.L", latest_price=2000.0, quote_currency="GBp")
    assert lse_val["status"] == "ok"
    assert lse_val["estimate_currency"] == "GBP"
    assert lse_val["quote_currency"] == "GBP"
    assert lse_val["fy1_end"] == "2026-12-31"
    assert lse_val["fy1_eps"] == 1.0
    assert lse_val["fy2_end"] == "2027-12-31"
    assert lse_val["fy2_eps"] == 2.0
    assert lse_val["pe_fy1"] == 20.0
    assert lse_val["pe_fy2"] == 10.0
    assert lse_val["eps_growth_fy2"] == 100.0
    assert lse_val["provider"] == "yfinance+fmp"

    # 2. Other subunit quote pairs: ZAc/ZAR and ILA/ILS
    for sym, sub_ccy, maj_ccy in (("JSE1.JO", "ZAc", "ZAR"), ("TASE1.TA", "ILA", "ILS")):
        merged_sub = consensus_mod._merge_partial_consensus_record(
            consensus_mod.RawConsensusRecord(
                symbol=sym,
                fy1_eps=200.0,
                fy2_eps=None,
                fy1_end="2027-12-31",
                fy2_end=None,
                estimate_currency=sub_ccy,
                quote_currency=sub_ccy,
                analyst_count=12,
                provider="yfinance",
            ),
            consensus_mod.RawConsensusRecord(
                symbol=sym,
                fy1_eps=1.0,
                fy2_eps=2.1,
                fy1_end="2026-12-31",
                fy2_end="2027-12-31",
                estimate_currency=maj_ccy,
                quote_currency=sub_ccy,
                analyst_count=14,
                provider="fmp",
            ),
            fallback_annual_rows=[
                {"date": "2026-12-31", "epsAvg": 1.0, "numAnalystsEps": 14},
                {"date": "2027-12-31", "epsAvg": 2.1, "numAnalystsEps": 12},
            ],
            today=date(2026, 11, 26),
            last_reported_fy_end="2025-12-31",
        )
        val_sub = consensus_mod._compute_valuation_from_record(
            merged_sub,
            symbol=sym,
            latest_price=2000.0,
            quote_currency_override=sub_ccy,
        )
        assert val_sub["status"] == "ok"
        assert val_sub["estimate_currency"] == maj_ccy
        assert val_sub["quote_currency"] == maj_ccy
        assert val_sub["pe_fy1"] == 20.0
        assert val_sub["pe_fy2"] == 10.0
        assert val_sub["eps_growth_fy2"] == 100.0

    # 3. Indeterminate currency unit on FMP FY1 (missing reportedCurrency) -> FY1 displays `—` (unit_unverified), Yahoo FY2 stays valid!
    fmp_no_ccy = create_autospec(FMPClient, instance=True)
    fmp_no_ccy.get_analyst_estimates.return_value = [
        {"date": "2026-12-31", "epsAvg": 1.0, "numAnalystsEps": 20},
        {"date": "2027-12-31", "epsAvg": 2.0, "numAnalystsEps": 16},
    ]
    fmp_no_ccy.get_income_statement.return_value = [{"date": "2025-12-31"}]  # missing reportedCurrency
    fmp_no_ccy.get_profile.return_value = [
        {"symbol": "LSE2.L", "currency": "GBp", "companyName": "London Unverified FY1", "isAdr": False}
    ]
    adapter_unv = consensus_mod.WatchlistConsensusAdapter(
        fmp_client=fmp_no_ccy,
        today_fn=lambda: date(2026, 11, 26),
    )
    unv_fy1 = await adapter_unv.get_valuation("LSE2.L", latest_price=2000.0, quote_currency="GBp")
    assert unv_fy1["fy1_end"] == "2026-12-31"
    assert unv_fy1["fy1_eps"] is None
    assert unv_fy1["pe_fy1"] is None
    assert unv_fy1["pe_fy1_status"] == "unit_unverified"
    assert unv_fy1["fy2_end"] == "2027-12-31"
    assert unv_fy1["fy2_eps"] == 2.0
    assert unv_fy1["pe_fy2"] == 10.0
    assert unv_fy1["pe_fy2_status"] == "ok"
    assert unv_fy1["eps_growth_fy2"] is None
    assert unv_fy1["eps_growth_fy2_status"] == "unit_unverified"
    assert await consensus_mod._read_consensus_cooldown("LSE2.L") == "partial_error"

    # 4. Indeterminate currency unit on primary Yahoo FY2 (unknown subunit "USc") while FMP FY1 GBP is verified -> FY1 ok, FY2 unit_unverified (`—`)!
    merged_primary_unv = consensus_mod._merge_partial_consensus_record(
        consensus_mod.RawConsensusRecord(
            symbol="LSE3.L",
            fy1_eps=200.0,
            fy2_eps=None,
            fy1_end="2027-12-31",
            fy2_end=None,
            estimate_currency="USc",  # unknown subunit -> indeterminate unit
            quote_currency="GBp",
            analyst_count=10,
            provider="yfinance",
        ),
        consensus_mod.RawConsensusRecord(
            symbol="LSE3.L",
            fy1_eps=1.0,
            fy2_eps=None,
            fy1_end="2026-12-31",
            fy2_end=None,
            estimate_currency="GBP",
            quote_currency="GBp",
            analyst_count=12,
            provider="fmp",
        ),
        fallback_annual_rows=[{"date": "2026-12-31", "epsAvg": 1.0, "numAnalystsEps": 12}],
        today=date(2026, 11, 26),
        last_reported_fy_end="2025-12-31",
    )
    val_primary_unv = consensus_mod._compute_valuation_from_record(
        merged_primary_unv,
        symbol="LSE3.L",
        latest_price=2000.0,
        quote_currency_override="GBp",
    )
    assert val_primary_unv["pe_fy1"] == 20.0
    assert val_primary_unv["pe_fy1_status"] == "ok"
    assert val_primary_unv["pe_fy2"] is None
    assert val_primary_unv["pe_fy2_status"] == "unit_unverified"
    assert val_primary_unv["eps_growth_fy2"] is None
    assert val_primary_unv["eps_growth_fy2_status"] == "unit_unverified"


@pytest.mark.asyncio
async def test_slow_redis_write_releases_upstream_slot_holds_singleflight_and_keeps_fetched_year_readable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """r6-2: Upstream slots are released before Redis cache/cooldown writes; slow Redis writes are bounded independently, keep same-symbol singleflight active until completion, log timeout, and keep fetched years readable."""
    import logging
    import pandas as pd
    import yfinance as yf
    from yfinance.scrapers.analysis import Analysis as YfAnalysis
    import src.data_client.yfinance.yahoo as yahoo_mod

    caplog.set_level(logging.WARNING, logger=consensus_mod.logger.name)
    monkeypatch.setattr(consensus_mod, "_REDIS_WRITE_TIMEOUT_SECONDS", 0.12)

    fake_redis, kv, locks = _make_ttl_redis_mock()
    orig_set = fake_redis.set.side_effect
    slow_keys: set[str] = set()
    write_started: dict[str, asyncio.Event] = {}

    async def _slow_set(key: str, value: Any, ttl: int | None = None) -> bool:
        if key in slow_keys:
            ev = write_started.get(key)
            if ev is not None:
                ev.set()
            await asyncio.sleep(5.0)
        return await orig_set(key, value, ttl=ttl)

    fake_redis.set.side_effect = _slow_set
    monkeypatch.setattr(
        consensus_mod,
        "_get_redis_cache_client",
        create_autospec(consensus_mod._get_redis_cache_client, return_value=fake_redis),
    )

    yf_calls: list[str] = []

    def _make_ticker(sym: str) -> Any:
        yf_calls.append(sym)
        ticker = create_autospec(yf.Ticker, instance=True)
        analysis = create_autospec(YfAnalysis, instance=True)
        if sym == "SLOWPART":
            ticker.earnings_estimate = pd.DataFrame(
                {"avg": [4.5], "numberOfAnalysts": [30]},
                index=["0y"],
            )
            analysis._earnings_trend = [
                {"period": "0y", "endDate": "2027-01-31", "earningsEstimate": {"currency": "USD"}}
            ]
        else:
            ticker.earnings_estimate = pd.DataFrame(
                {"avg": [5.0, 10.0], "numberOfAnalysts": [30, 28]},
                index=["0y", "+1y"],
            )
            analysis._earnings_trend = [
                {"period": "0y", "endDate": "2027-01-31", "earningsEstimate": {"currency": "USD"}},
                {"period": "+1y", "endDate": "2028-01-31", "earningsEstimate": {"currency": "USD"}},
            ]
        ticker._analysis = analysis
        ticker.info = {
            "currency": "USD",
            "financialCurrency": "USD",
            "quoteType": "EQUITY",
            "longName": f"{sym} Corp",
        }
        return ticker

    monkeypatch.setattr(
        yahoo_mod,
        "yahoo_ticker",
        create_autospec(yahoo_mod.yahoo_ticker, side_effect=_make_ticker),
    )

    # --- Part A: Success cache write path (Standalone Core, no FMP) ---
    cache_key = consensus_mod._consensus_cache_key("SLOWCACHE")
    inflight_key = f"{cache_key}:inflight"
    slow_keys.add(cache_key)
    write_started[cache_key] = asyncio.Event()

    adapter_core = consensus_mod.WatchlistConsensusAdapter(
        today_fn=lambda: date(2026, 11, 26),
    )
    t_slow_cache = asyncio.create_task(
        adapter_core.get_valuation("SLOWCACHE", latest_price=100.0, quote_currency="USD")
    )
    await asyncio.wait_for(write_started[cache_key].wait(), timeout=2.0)

    # While Redis cache write is stalled:
    # 1. Both Redis and local upstream semaphore slots are ALREADY released!
    assert [k for k in locks if k.startswith(consensus_mod._REDIS_SEM_KEY_PREFIX)] == []
    assert (
        consensus_mod._get_consensus_semaphore()._value
        == consensus_mod.MAX_CONSENSUS_CONCURRENCY
    )
    # 2. Same-symbol singleflight protection (Redis inflight lock + local task) stays held!
    assert inflight_key in locks
    assert await fake_redis.acquire_lock(inflight_key, "other-worker", 15_000) is False
    assert cache_key in consensus_mod._INFLIGHT_CONSENSUS
    # 3. Another symbol can proceed immediately without waiting on SLOWCACHE's Redis write!
    other_val = await adapter_core.get_valuation("OTHERCO", latest_price=100.0, quote_currency="USD")
    assert other_val["status"] == "ok"
    assert other_val["pe_fy1"] == 20.0
    # 4. Concurrent caller for SLOWCACHE coalesces on singleflight (no duplicate upstream fetch)
    t_coalesced = asyncio.create_task(
        adapter_core.get_valuation("SLOWCACHE", latest_price=100.0, quote_currency="USD")
    )
    # 5. In-memory snapshot is already readable immediately
    mid_cached, mid_warm = await adapter_core.get_cached_valuation(
        "SLOWCACHE", latest_price=100.0, quote_currency="USD"
    )
    assert mid_cached is not None and mid_cached["status"] == "ok"
    assert mid_cached["pe_fy1"] == 20.0
    assert mid_cached["pe_fy2"] == 10.0
    assert mid_warm is False

    # Bounded Redis write times out cleanly (~0.12s), releases singleflight lock, and logs warning
    res_cache, res_coalesced = await asyncio.wait_for(
        asyncio.gather(t_slow_cache, t_coalesced), timeout=2.0
    )
    assert res_cache["status"] == "ok" and res_cache["pe_fy1"] == 20.0 and res_cache["pe_fy2"] == 10.0
    assert res_coalesced["status"] == "ok" and res_coalesced["pe_fy1"] == 20.0
    assert inflight_key not in locks
    assert yf_calls.count("SLOWCACHE") == 1
    assert any("consensus cache write timed out" in rec.message for rec in caplog.records)

    # --- Part B: Partial cooldown write path (Yahoo FY1 fetched, FMP fails, cooldown Redis write stalls) ---
    cd_key = consensus_mod._consensus_cooldown_redis_key("SLOWPART")
    part_inflight_key = f"{consensus_mod._consensus_cache_key('SLOWPART')}:inflight"
    slow_keys.add(cd_key)
    write_started[cd_key] = asyncio.Event()

    fmp_fail = create_autospec(FMPClient, instance=True)
    fmp_fail.get_analyst_estimates.side_effect = RuntimeError("FMP 503 during backfill")

    adapter_partial = consensus_mod.WatchlistConsensusAdapter(
        fmp_client=fmp_fail,
        today_fn=lambda: date(2026, 11, 26),
    )
    t_slow_part = asyncio.create_task(
        adapter_partial.get_valuation("SLOWPART", latest_price=135.0, quote_currency="USD")
    )
    await asyncio.wait_for(write_started[cd_key].wait(), timeout=2.0)

    # While cooldown Redis write is stalled: slot is released, singleflight lock is held, Yahoo FY1 is already readable!
    assert [k for k in locks if k.startswith(consensus_mod._REDIS_SEM_KEY_PREFIX)] == []
    assert (
        consensus_mod._get_consensus_semaphore()._value
        == consensus_mod.MAX_CONSENSUS_CONCURRENCY
    )
    assert part_inflight_key in locks
    mid_part, mid_part_warm = await adapter_partial.get_cached_valuation(
        "SLOWPART", latest_price=135.0, quote_currency="USD"
    )
    assert mid_part is not None and mid_part["status"] == "ok"
    assert mid_part["pe_fy1"] == 30.0
    assert mid_part["pe_fy2"] is None
    assert mid_part_warm is False

    res_part = await asyncio.wait_for(t_slow_part, timeout=2.0)
    assert res_part["status"] == "ok"
    assert res_part["pe_fy1"] == 30.0
    assert res_part["pe_fy2"] is None
    assert part_inflight_key not in locks
    assert consensus_mod._consensus_cache_key("SLOWPART") not in consensus_mod._CONSENSUS_CACHE
    assert any("cooldown write (partial_error) timed out" in rec.message for rec in caplog.records)

    # Already-fetched Yahoo FY1 remains readable from cooldown snapshot after Redis write timeout
    after_part, after_part_warm = await adapter_partial.get_cached_valuation(
        "SLOWPART", latest_price=135.0, quote_currency="USD"
    )
    assert after_part is not None and after_part["status"] == "ok"
    assert after_part["pe_fy1"] == 30.0
    assert after_part["pe_fy2"] is None
    assert after_part_warm is False
    # Confirm Redis kv did not receive the timed-out writes (proving memory snapshot served the reads)
    assert cache_key not in kv
    assert cd_key not in kv
