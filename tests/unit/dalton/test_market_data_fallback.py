"""FMP stays primary; yfinance answers only when FMP cannot."""

from __future__ import annotations

import inspect
from unittest.mock import create_autospec, patch

import pytest

from src.dalton.market_data_fallback import FallbackFinancialSource, fmp_cannot_answer
from src.data_client.base import FinancialDataSource
from src.data_client.fmp.financial_source import FMPFinancialSource
from src.data_client.fmp.fmp_client import FMPRequestError
from src.data_client.yfinance.financial_source import YFinanceFinancialSource

YF_ROW = [{"symbol": "AAPL", "name": "Apple Inc."}]


def _sources():
    fmp = create_autospec(FMPFinancialSource, instance=True)
    yf = create_autospec(YFinanceFinancialSource, instance=True)
    return fmp, yf, FallbackFinancialSource(fmp, yf)


@pytest.mark.parametrize("status", [401, 402, 403, 429, 500, 503, None])
@pytest.mark.asyncio
async def test_a_failure_fmp_cannot_answer_goes_to_yfinance(status):
    fmp, yf, source = _sources()
    fmp.search_stocks.side_effect = FMPRequestError("nope", status_code=status)
    yf.search_stocks.return_value = YF_ROW

    assert await source.search_stocks("apple", limit=5) == YF_ROW
    yf.search_stocks.assert_awaited_once_with("apple", limit=5)


@pytest.mark.parametrize("status", [400, 404])
@pytest.mark.asyncio
async def test_a_bad_request_stays_fmps_error(status):
    fmp, yf, source = _sources()
    fmp.get_company_profile.side_effect = FMPRequestError("bad", status_code=status)

    with pytest.raises(FMPRequestError):
        await source.get_company_profile("AAPL")
    yf.get_company_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_healthy_fmp_answers_even_when_empty():
    fmp, yf, source = _sources()
    fmp.get_realtime_quote.return_value = []

    assert await source.get_realtime_quote("ZZZZ") == []
    yf.get_realtime_quote.assert_not_awaited()


def test_only_fmp_errors_count_as_unavailable():
    assert not fmp_cannot_answer(ValueError("x"))
    assert fmp_cannot_answer(FMPRequestError("timed out"))


def test_every_protocol_method_falls_back():
    methods = {
        name
        for name, member in inspect.getmembers(FinancialDataSource)
        if inspect.iscoroutinefunction(member) and name != "close"
    }
    assert methods <= set(vars(FallbackFinancialSource))


@pytest.mark.asyncio
async def test_the_registry_puts_yfinance_behind_fmp():
    import src.data_client.registry as registry

    registry._financial_data_provider = None
    try:
        with (
            patch.object(registry, "_fmp_available", return_value=True),
            patch.object(registry, "_yfinance_available", return_value=True),
            patch.object(registry, "_ginlix_data_available", return_value=False),
            patch("src.data_client.fmp.get_fmp_client", autospec=True),
        ):
            provider = await registry.get_financial_data_provider()
        assert isinstance(provider.financial, FallbackFinancialSource)
    finally:
        registry._financial_data_provider = None


@pytest.mark.asyncio
async def test_search_endpoint_errors_without_caching_when_fmp_429_and_yfinance_fails(
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
    cache.set.assert_not_awaited()

    empty_resp = httpx.Response(200, json={"quotes": []})
    cache_get_mock.return_value = empty_resp
    assert await YFinanceFinancialSource().search_stocks("zzzz", limit=5) == []


@pytest.mark.asyncio
async def test_fmp_search_falls_back_when_symbol_is_empty_and_name_is_429(monkeypatch):
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
    yf.search_stocks.return_value = YF_ROW
    source = FallbackFinancialSource(FMPFinancialSource(client), yf)

    assert await source.search_stocks("apple", limit=5) == YF_ROW
    yf.search_stocks.assert_awaited_once_with("apple", limit=5)


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
    source = FallbackFinancialSource(FMPFinancialSource(client), yf)

    assert await source.search_stocks("AAPL", limit=5) == sym_rows
    yf.search_stocks.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_sector_performance_with_target_date_does_not_fall_back():
    fmp, yf, source = _sources()
    fmp.get_sector_performance.side_effect = FMPRequestError("plan", status_code=402)

    with pytest.raises(FMPRequestError):
        await source.get_sector_performance(target_date="2025-01-15")
    yf.get_sector_performance.assert_not_awaited()

    with pytest.raises(ValueError, match="2025-01-15"):
        await YFinanceFinancialSource().get_sector_performance(target_date="2025-01-15")


@pytest.mark.asyncio
async def test_sector_performance_without_target_date_falls_back_to_yfinance():
    fmp, yf, source = _sources()
    sector_rows = [{"sector": "Technology", "changePctStr": "+1.20%"}]
    fmp.get_sector_performance.side_effect = FMPRequestError("quota", status_code=429)
    yf.get_sector_performance.return_value = sector_rows

    assert await source.get_sector_performance() == sector_rows
    yf.get_sector_performance.assert_awaited_once_with(target_date=None)
