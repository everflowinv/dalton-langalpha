"""yfinance answers first; FMP backs it up and alone serves the rest.

Quota-marker mechanics (write on 429 / "Limit Reach", read in
``FMPClient._make_request``) live in ``tests/unit/data_client/test_fmp_client.py``.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any
from unittest.mock import create_autospec, patch

import pytest

from src.dalton import market_data_fallback as mdf
from src.dalton.market_data_fallback import (
    FallbackFinancialSource,
    fmp_cannot_answer,
)
from src.data_client.base import FinancialDataSource
from src.data_client.fmp.financial_source import FMPFinancialSource
from src.data_client.fmp.fmp_client import FMPRequestError, FmpQuotaExhausted
from src.data_client.yfinance.financial_source import YFinanceFinancialSource

YF_ROW = [{"symbol": "AAPL", "name": "Apple Inc."}]
# A quote method's answer must carry a usable price (see _answered).
YF_QUOTE_ROW = [{"symbol": "AAPL", "name": "Apple Inc.", "price": 189.5}]
FMP_ROW = [{"symbol": "AAPL", "price": 190.0}]

# 2026-10-24 19:00 UTC = 15:00 US/Eastern (EDT) that day: FMP's documented reset.
RESET = datetime(2026, 10, 24, 19, 0, tzinfo=timezone.utc)

# (method, args) for every yfinance-first protocol method. autospec enforces
# signatures, so each call shape has to be a legal one.
YF_METHOD_CALLS = [
    ("get_company_profile", ("AAPL",)),
    ("get_realtime_quote", ("AAPL",)),
    ("get_income_statements", ("AAPL",)),
    ("get_cash_flows", ("AAPL",)),
    ("get_key_metrics", ("AAPL",)),
    ("get_financial_ratios", ("AAPL",)),
    ("get_price_performance", ("AAPL",)),
    ("get_analyst_price_targets", ("AAPL",)),
    ("get_analyst_ratings", ("AAPL",)),
    ("get_earnings_history", ("AAPL",)),
    ("screen_stocks", ()),
    ("search_stocks", ("apple",)),
]


def _sources():
    fmp = create_autospec(FMPFinancialSource, instance=True)
    yf = create_autospec(YFinanceFinancialSource, instance=True)
    return fmp, yf, FallbackFinancialSource(fmp, yf)


def _call(method: str, args: tuple, source: FallbackFinancialSource) -> Any:
    kwargs = {"marketCapMoreThan": 1e12} if method == "screen_stocks" else {}
    return getattr(source, method)(*args, **kwargs)


# ---------------------------------------------------------------------------
# Per-method primary/secondary
# ---------------------------------------------------------------------------


def test_the_call_table_covers_every_yfinance_first_method():
    assert {m for m, _ in YF_METHOD_CALLS} == mdf._YFINANCE_FIRST


@pytest.mark.parametrize("method,args", YF_METHOD_CALLS)
@pytest.mark.asyncio
async def test_every_yfinance_method_answers_yfinance_first(method, args):
    fmp, yf, source = _sources()
    row = YF_QUOTE_ROW if method in mdf._QUOTE_METHODS else YF_ROW
    getattr(yf, method).return_value = row

    assert await _call(method, args, source) == row
    getattr(fmp, method).assert_not_awaited()
    getattr(yf, method).assert_awaited_once()


@pytest.mark.parametrize("method,args", YF_METHOD_CALLS)
@pytest.mark.asyncio
async def test_a_yfinance_failure_falls_back_to_fmp(method, args):
    fmp, yf, source = _sources()
    getattr(yf, method).side_effect = ValueError("yahoo down")
    getattr(fmp, method).return_value = FMP_ROW

    assert await _call(method, args, source) == FMP_ROW
    getattr(fmp, method).assert_awaited_once()


@pytest.mark.parametrize("method,args", YF_METHOD_CALLS)
@pytest.mark.asyncio
async def test_an_empty_yfinance_answer_falls_back_to_fmp(method, args):
    fmp, yf, source = _sources()
    getattr(yf, method).return_value = []
    getattr(fmp, method).return_value = FMP_ROW

    assert await _call(method, args, source) == FMP_ROW
    getattr(fmp, method).assert_awaited_once()


@pytest.mark.asyncio
async def test_fmp_empty_or_failed_returns_yfinances_original_empty_result():
    fmp, yf, source = _sources()
    yf.get_realtime_quote.return_value = []
    fmp.get_realtime_quote.return_value = []
    assert await source.get_realtime_quote("AAPL") == []

    fmp.get_realtime_quote.side_effect = FMPRequestError("upstream down")
    assert await source.get_realtime_quote("AAPL") == []


@pytest.mark.asyncio
async def test_fmp_quota_marker_returns_the_empty_yfinance_result():
    fmp, yf, source = _sources()
    yf.get_realtime_quote.return_value = []
    fmp.get_realtime_quote.side_effect = FmpQuotaExhausted(RESET)

    assert await source.get_realtime_quote("AAPL") == []


@pytest.mark.asyncio
async def test_fmp_quota_marker_surfaces_the_yfinance_failure():
    fmp, yf, source = _sources()
    yf.search_stocks.side_effect = ValueError("yahoo down")
    fmp.search_stocks.side_effect = FmpQuotaExhausted(RESET)

    with pytest.raises(ValueError, match="yahoo down"):
        await source.search_stocks("apple", limit=5)


@pytest.mark.parametrize("price", [None, 0, -1])
@pytest.mark.asyncio
async def test_a_zero_priced_quote_row_counts_as_empty(price):
    fmp, yf, source = _sources()
    yf.get_realtime_quote.return_value = [{"symbol": "AAPL", "price": price}]
    fmp.get_realtime_quote.return_value = FMP_ROW

    assert await source.get_realtime_quote("AAPL") == FMP_ROW
    fmp.get_realtime_quote.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_quote_row_without_a_price_key_is_not_an_answer():
    fmp, yf, source = _sources()
    yf.get_realtime_quote.return_value = [{"symbol": "AAPL"}]
    fmp.get_realtime_quote.return_value = FMP_ROW

    assert await source.get_realtime_quote("AAPL") == FMP_ROW
    fmp.get_realtime_quote.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_profile_with_a_name_stands_even_with_price_none():
    fmp, yf, source = _sources()
    row = [{"symbol": "AAPL", "companyName": "Apple Inc.", "price": None}]
    yf.get_company_profile.return_value = row

    assert await source.get_company_profile("AAPL") == row
    fmp.get_company_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_screener_row_with_an_empty_price_is_an_answer():
    fmp, yf, source = _sources()
    row = [{"symbol": "AAPL", "price": None}]
    yf.screen_stocks.return_value = row

    assert await source.screen_stocks(marketCapMoreThan=1e12) == row
    fmp.screen_stocks.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_single_row_without_a_price_key_is_an_answer():
    fmp, yf, source = _sources()
    yf.search_stocks.return_value = YF_ROW

    assert await source.search_stocks("apple", limit=5) == YF_ROW
    fmp.search_stocks.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_yfinance_empty_profile_falls_back_to_fmp(monkeypatch):
    """The real adapter's [] on an empty Yahoo info reaches FMP."""
    import yfinance as yf_mod

    import src.data_client.yfinance.financial_source as yf_fin

    ticker = create_autospec(yf_mod.Ticker, instance=True)
    ticker.info = {}  # _get_profile returns [] the moment info is empty
    monkeypatch.setattr(
        yf_fin,
        "yahoo_ticker",
        create_autospec(yf_fin.yahoo_ticker, return_value=ticker),
    )

    fmp = create_autospec(FMPFinancialSource, instance=True)
    fmp.get_company_profile.return_value = FMP_ROW
    source = FallbackFinancialSource(fmp, YFinanceFinancialSource())

    assert await source.get_company_profile("AAPL") == FMP_ROW
    fmp.get_company_profile.assert_awaited_once_with("AAPL")


@pytest.mark.asyncio
async def test_revenue_segments_go_straight_to_fmp():
    fmp, yf, source = _sources()
    fmp.get_revenue_by_segment.return_value = FMP_ROW

    assert await source.get_revenue_by_segment("AAPL") == FMP_ROW
    yf.get_revenue_by_segment.assert_not_awaited()
    fmp.get_revenue_by_segment.assert_awaited_once_with("AAPL")


@pytest.mark.asyncio
async def test_sector_performance_with_target_date_stays_fmp():
    fmp, yf, source = _sources()
    fmp.get_sector_performance.return_value = FMP_ROW

    assert await source.get_sector_performance(target_date="2025-01-15") == FMP_ROW
    fmp.get_sector_performance.assert_awaited_once_with(target_date="2025-01-15")
    yf.get_sector_performance.assert_not_awaited()

    with pytest.raises(ValueError, match="2025-01-15"):
        await YFinanceFinancialSource().get_sector_performance(target_date="2025-01-15")


@pytest.mark.asyncio
async def test_sector_performance_without_target_date_is_yfinance_first():
    fmp, yf, source = _sources()
    yf.get_sector_performance.return_value = YF_ROW

    assert await source.get_sector_performance() == YF_ROW
    fmp.get_sector_performance.assert_not_awaited()
    yf.get_sector_performance.assert_awaited_once_with(target_date=None)


def test_only_fmp_errors_count_as_unavailable():
    assert not fmp_cannot_answer(ValueError("x"))
    assert fmp_cannot_answer(FMPRequestError("timed out"))


def test_every_protocol_method_is_routed():
    methods = {
        name
        for name, member in inspect.getmembers(FinancialDataSource)
        if inspect.iscoroutinefunction(member) and name != "close"
    }
    assert methods <= set(vars(FallbackFinancialSource))


# ---------------------------------------------------------------------------
# Registry wiring and endpoint behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_registry_builds_yfinance_first_behind_fmp():
    import src.data_client.registry as registry

    registry._financial_data_provider = None
    try:
        with (
            patch.object(registry, "_fmp_available", return_value=True, autospec=True),
            patch.object(
                registry, "_yfinance_available", return_value=True, autospec=True
            ),
            patch.object(
                registry, "_ginlix_data_available", return_value=False, autospec=True
            ),
            patch("src.data_client.fmp.get_fmp_client", autospec=True),
        ):
            provider = await registry.get_financial_data_provider()
        source = provider.financial
        assert isinstance(source, FallbackFinancialSource)
        assert isinstance(source._yfinance, YFinanceFinancialSource)
        assert isinstance(source._fmp, FMPFinancialSource)
    finally:
        registry._financial_data_provider = None


@pytest.mark.asyncio
async def test_search_endpoint_errors_without_caching_when_yfinance_and_fmp_fail(
    monkeypatch,
):
    import httpx
    from fastapi import HTTPException
    from yfinance.data import YfData

    import src.data_client as data_client
    from src.data_client.financial_data_provider import FinancialDataProvider
    from src.server.app import market_data
    from src.utils.cache import redis_cache
    from src.utils.cache.redis_cache import RedisCacheClient

    fmp = create_autospec(FMPFinancialSource, instance=True)
    fmp.search_stocks.side_effect = FMPRequestError("quota", status_code=429)
    provider = FinancialDataProvider(
        financial=FallbackFinancialSource(fmp, YFinanceFinancialSource())
    )
    cache = create_autospec(RedisCacheClient, instance=True)
    cache.get.return_value = None

    monkeypatch.setattr(
        redis_cache,
        "get_cache_client",
        create_autospec(redis_cache.get_cache_client, return_value=cache),
    )
    monkeypatch.setattr(
        data_client,
        "get_financial_data_provider",
        create_autospec(data_client.get_financial_data_provider, return_value=provider),
    )

    bad_json_resp = httpx.Response(200, text="<html>bad gateway</html>")
    cache_get_mock = create_autospec(YfData.cache_get, return_value=bad_json_resp)
    monkeypatch.setattr(YfData, "cache_get", cache_get_mock)

    with pytest.raises(HTTPException) as exc_info:
        await market_data.search_stocks(
            user_id="u1", query="apple", limit=5, exchange=[]
        )

    assert exc_info.value.status_code == 500
    # No search result is cached as "no match" (the autospec FMP source
    # bypasses FMPClient, so no quota marker is written here either).
    cache.set.assert_not_awaited()

    empty_resp = httpx.Response(200, json={"quotes": []})
    cache_get_mock.return_value = empty_resp
    assert await YFinanceFinancialSource().search_stocks("zzzz", limit=5) == []


@pytest.mark.asyncio
async def test_fmp_search_raises_when_symbol_is_empty_and_name_is_429(monkeypatch):
    from src.data_client.fmp.fmp_client import FMPClient

    client = FMPClient(api_key="test")
    make_request = create_autospec(client._make_request)

    async def _side_effect(endpoint, params=None):
        if endpoint == "search-symbol":
            return []
        raise FMPRequestError("quota", status_code=429)

    make_request.side_effect = _side_effect
    monkeypatch.setattr(client, "_make_request", make_request)

    yf = create_autospec(YFinanceFinancialSource, instance=True)
    yf.search_stocks.side_effect = ValueError("yahoo down")
    source = FallbackFinancialSource(FMPFinancialSource(client), yf)

    with pytest.raises(FMPRequestError):
        await source.search_stocks("apple", limit=5)


@pytest.mark.asyncio
async def test_fmp_search_keeps_symbol_rows_when_name_is_429(monkeypatch):
    from src.data_client.fmp.fmp_client import FMPClient

    client = FMPClient(api_key="test")
    make_request = create_autospec(client._make_request)
    sym_rows = [{"symbol": "AAPL", "name": "Apple Inc."}]

    async def _side_effect(endpoint, params=None):
        if endpoint == "search-symbol":
            return sym_rows
        raise FMPRequestError("quota", status_code=429)

    make_request.side_effect = _side_effect
    monkeypatch.setattr(client, "_make_request", make_request)

    yf = create_autospec(YFinanceFinancialSource, instance=True)
    yf.search_stocks.side_effect = ValueError("yahoo down")
    source = FallbackFinancialSource(FMPFinancialSource(client), yf)

    assert await source.search_stocks("AAPL", limit=5) == sym_rows


@pytest.mark.asyncio
async def test_yfinance_analyst_price_targets_maps_consensus_to_mean(monkeypatch):
    import yfinance as yf_mod

    import src.data_client.yfinance.financial_source as yf_fin

    ticker = create_autospec(yf_mod.Ticker, instance=True)
    ticker.analyst_price_targets = {
        "current": 180.0,
        "high": 250.0,
        "low": 160.0,
        "mean": 215.5,
        "median": 210.0,
    }
    monkeypatch.setattr(
        yf_fin,
        "yahoo_ticker",
        create_autospec(yf_fin.yahoo_ticker, return_value=ticker),
    )

    rows = await YFinanceFinancialSource().get_analyst_price_targets("AAPL")
    assert rows == [
        {
            "symbol": "AAPL",
            "targetHigh": 250.0,
            "targetLow": 160.0,
            "targetMean": 215.5,
            "targetMedian": 210.0,
            "targetConsensus": 215.5,
        }
    ]
