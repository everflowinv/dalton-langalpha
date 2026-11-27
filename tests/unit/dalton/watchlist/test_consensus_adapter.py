"""Unit tests for WatchlistConsensusAdapter under Standalone Core (§6.3).

Covers US equities, HK cross-currency equities, verified ADRs, unverified ADRs,
FMP annual fiscal-year matching fallback, all 7 valuation cell states, and
Standalone Core acceptance with zero private service calls.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.capabilities import CapabilityId, has_capability
from src.dalton.database import coverage as coverage_db
from src.dalton.watchlist import consensus as consensus_mod
from src.dalton.watchlist import metrics as metrics_mod


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

    class _FakeFmp:
        async def get_analyst_estimates(self, symbol: str, period: str = "annual", limit: int = 6) -> list[dict[str, Any]]:
            _ = (symbol, period, limit)
            return [
                {"date": "2027-12-31", "epsAvg": 8.0, "numAnalystsEps": 25},
                {"date": "2028-12-31", "epsAvg": 10.0, "numAnalystsEps": 22},
            ]

        async def get_profile(self, symbol: str) -> list[dict[str, Any]]:
            return [{"symbol": symbol, "currency": "USD", "reportedCurrency": "USD", "companyName": "Test Corp"}]

    fmp_mock = create_autospec(_FakeFmp, instance=True)
    fmp_mock.get_analyst_estimates.side_effect = _FakeFmp().get_analyst_estimates
    fmp_mock.get_profile.side_effect = _FakeFmp().get_profile

    async def _empty_yf(_symbol: str) -> consensus_mod.RawConsensusRecord:
        return consensus_mod.RawConsensusRecord(symbol="MSFT", quote_currency="USD", provider="yfinance")

    adapter = consensus_mod.WatchlistConsensusAdapter(
        yfinance_fetcher=_empty_yf,
        fmp_client=fmp_mock,
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
