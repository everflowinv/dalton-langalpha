"""Calendar degradation to Yahoo when FMP cannot answer.

The live yfinance column sets were confirmed against yfinance 1.7.0 on
2026-10-26: ``sp_earnings`` (``Symbol`` index; ``Event Start Date`` tz-aware
UTC, ``EPS Estimate``, ``Reported EPS``) and ``economic_event`` (``Event``
index; ``Region``, ``Event Time`` tz-aware UTC, ``Actual``, ``Expected``,
``Last``). The mapping tests lock those contracts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import create_autospec

import pandas as pd
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import src.server.app.calendar as calendar_mod
import src.tools.market_data.market_overview as market_overview
from src.dalton.market_data import calendar_fallback as cf
from src.data_client.base import FinancialDataSource
from src.data_client.fmp.fmp_client import FMPClient, FMPRequestError, FmpQuotaExhausted
from src.data_client.yfinance.financial_source import YFinanceFinancialSource
from src.server.models.calendar import EconomicEvent, EarningsEvent
from src.server.services.cache.earnings_cache_service import EarningsCacheService
from src.utils.cache import redis_cache
from src.utils.cache.redis_cache import RedisCacheClient

# 2026-10-26 19:00 UTC = 15:00 US/Eastern (EDT) that day: FMP's documented reset.
RESET = datetime(2026, 10, 26, 19, 0, tzinfo=timezone.utc)

FROM, TO = "2026-10-26", "2026-10-27"

YF_EARNINGS_ROWS = [
    {
        "symbol": "ACGL",
        "date": "2026-10-27",
        "epsActual": None,
        "epsEstimated": 2.3,
        "revenueActual": None,
        "revenueEstimated": None,
        "lastUpdated": None,
    },
    {
        "symbol": "AAPL",
        "date": "2026-10-27",
        "epsActual": 1.64,
        "epsEstimated": 1.6,
        "revenueActual": None,
        "revenueEstimated": None,
        "lastUpdated": None,
    },
]

YF_ECONOMIC_ROWS = [
    {
        "date": "2026-10-27 08:30:00",
        "country": "US",
        "event": "Core CPI MM",
        "currency": "",
        "previous": 0.1,
        "estimate": 0.2,
        "actual": None,
        "change": None,
        "impact": None,
        "changePercentage": None,
    },
]

SECTOR_ROWS = [
    {"sector": "Technology", "changePctStr": "+1.10%"},
    {"sector": "Energy", "changePctStr": "-0.40%"},
]


def _earnings_df(
    rows: list[tuple[str, Any, float, float]], *, utc: bool | None = True
) -> pd.DataFrame:
    """One ``sp_earnings`` page: (symbol, event datetime, eps est, eps actual).

    ``utc=True`` builds tz-aware timestamps, ``utc=False`` naive ones, ``None``
    leaves the column as raw strings.
    """
    moments = [r[1] for r in rows]
    if utc is True:
        moments = pd.to_datetime(moments, utc=True)
    elif utc is False:
        moments = pd.to_datetime(moments)
    return pd.DataFrame(
        {
            "Company": [f"{r[0]} Inc." for r in rows],
            "Marketcap": [1.0e12] * len(rows),
            "Event Name": ["Q3 2026 Earnings Announcement"] * len(rows),
            "Event Start Date": moments,
            "Timing": ["AMC"] * len(rows),
            "EPS Estimate": [r[2] for r in rows],
            "Reported EPS": [r[3] for r in rows],
            "Surprise(%)": [float("nan")] * len(rows),
        },
        index=pd.Index([r[0] for r in rows], name="Symbol"),
    )


def _economic_df(rows: list[tuple[str, str, str, float, float, float]]) -> pd.DataFrame:
    """One ``economic_event`` page: (event, region, iso utc datetime, actual, expected, last)."""
    return pd.DataFrame(
        {
            "Region": [r[1] for r in rows],
            "Event Time": pd.to_datetime([r[2] for r in rows], utc=True),
            "For": ["Oct"] * len(rows),
            "Actual": [r[3] for r in rows],
            "Expected": [r[4] for r in rows],
            "Last": [r[5] for r in rows],
            "Revised": [float("nan")] * len(rows),
        },
        index=pd.Index([r[0] for r in rows], name="Event"),
    )


def _fmp_cache(monkeypatch) -> Any:
    """A cold FMP earnings cache; returns the autospec for assertions."""
    cache = create_autospec(EarningsCacheService, instance=True)
    cache.get.return_value = None
    monkeypatch.setattr(calendar_mod, "_earnings_cache", cache)
    return cache


def _yf_cache(monkeypatch) -> Any:
    """No cached yfinance answer, a recording store; returns the store autospec."""
    fetch = create_autospec(cf.cached_yf_earnings, return_value=None)
    store = create_autospec(cf.store_yf_earnings)
    monkeypatch.setattr(cf, "cached_yf_earnings", fetch)
    monkeypatch.setattr(cf, "store_yf_earnings", store)
    return store


def _fmp(monkeypatch) -> Any:
    """FMPClient replaced by an autospec; returns the class mock."""
    fmp_cls = create_autospec(FMPClient)
    monkeypatch.setattr(calendar_mod, "FMPClient", fmp_cls)
    return fmp_cls


def _yf_fetch(monkeypatch, rows: list[dict[str, Any]] | None, error: Any | None = None):
    fn = "earnings_from_yfinance"
    spec = create_autospec(getattr(cf, fn))
    if error is not None:
        spec.side_effect = error
    else:
        spec.return_value = rows or []
    monkeypatch.setattr(cf, fn, spec)
    return spec


# ---------------------------------------------------------------------------
# Earnings calendar endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_earnings_quota_exhausted_falls_back_to_yfinance(monkeypatch):
    fmp_cache = _fmp_cache(monkeypatch)
    yf_store = _yf_cache(monkeypatch)
    fmp_cls = _fmp(monkeypatch)
    fmp_cls.return_value.get_earnings_calendar_by_date.side_effect = FmpQuotaExhausted(
        RESET
    )
    yf_fetch = _yf_fetch(monkeypatch, YF_EARNINGS_ROWS)

    resp = await calendar_mod.get_earnings_calendar(
        user_id="u1", from_date=FROM, to_date=TO
    )

    assert resp.count == 2
    assert resp.data == [EarningsEvent(**r) for r in YF_EARNINGS_ROWS]
    yf_fetch.assert_awaited_once_with(FROM, TO)
    fmp_cls.return_value.close.assert_awaited_once()
    # The fallback's answer goes to yfinance's own 1h cache, never FMP's 24h.
    yf_store.assert_awaited_once_with(YF_EARNINGS_ROWS, FROM, TO)
    fmp_cache.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_earnings_fmp_construction_failure_uses_yfinance(monkeypatch):
    _fmp_cache(monkeypatch)
    _yf_cache(monkeypatch)
    fmp_cls = _fmp(monkeypatch)
    fmp_cls.side_effect = ValueError("FMP API key required")
    yf_fetch = _yf_fetch(monkeypatch, YF_EARNINGS_ROWS)

    resp = await calendar_mod.get_earnings_calendar(
        user_id="u1", from_date=FROM, to_date=TO
    )

    assert resp.count == 2
    yf_fetch.assert_awaited_once_with(FROM, TO)


@pytest.mark.asyncio
async def test_earnings_fmp_success_never_calls_yfinance(monkeypatch):
    fmp_cache = _fmp_cache(monkeypatch)
    _yf_cache(monkeypatch)
    fmp_cls = _fmp(monkeypatch)
    fmp_rows = [
        {
            "symbol": "AAPL",
            "date": "2026-10-27",
            "epsActual": 1.64,
            "epsEstimated": 1.6,
            "revenueActual": 94930000000,
            "revenueEstimated": 94500000000,
            "lastUpdated": "2026-10-30",
        }
    ]
    fmp_cls.return_value.get_earnings_calendar_by_date.return_value = fmp_rows
    yf_fetch = _yf_fetch(monkeypatch, YF_EARNINGS_ROWS)

    resp = await calendar_mod.get_earnings_calendar(
        user_id="u1", from_date=FROM, to_date=TO
    )

    assert resp.count == 1
    assert resp.data[0].revenueActual == 94930000000
    yf_fetch.assert_not_awaited()
    fmp_cache.set.assert_awaited_once_with(fmp_rows, FROM, TO)


@pytest.mark.asyncio
async def test_earnings_both_sources_fail_returns_empty_not_500(monkeypatch):
    fmp_cache = _fmp_cache(monkeypatch)
    yf_store = _yf_cache(monkeypatch)
    fmp_cls = _fmp(monkeypatch)
    fmp_cls.return_value.get_earnings_calendar_by_date.side_effect = FmpQuotaExhausted(
        RESET
    )
    _yf_fetch(monkeypatch, None, error=ValueError("yahoo down"))

    resp = await calendar_mod.get_earnings_calendar(
        user_id="u1", from_date=FROM, to_date=TO
    )

    assert resp.data == []
    assert resp.count == 0
    fmp_cache.set.assert_not_awaited()
    yf_store.assert_not_awaited()


@pytest.mark.asyncio
async def test_earnings_cached_fmp_answer_skips_everything(monkeypatch):
    fmp_cache = _fmp_cache(monkeypatch)
    fmp_cache.get.return_value = YF_EARNINGS_ROWS
    yf_cold = _yf_cache(monkeypatch)
    fmp_cls = _fmp(monkeypatch)
    yf_fetch = _yf_fetch(monkeypatch, YF_EARNINGS_ROWS)

    resp = await calendar_mod.get_earnings_calendar(
        user_id="u1", from_date=FROM, to_date=TO
    )

    assert resp.count == 2
    fmp_cls.assert_not_called()
    yf_fetch.assert_not_awaited()
    yf_cold.assert_not_awaited()


@pytest.mark.asyncio
async def test_earnings_cached_yf_answer_skips_sources(monkeypatch):
    _fmp_cache(monkeypatch)
    yf_cache_get = create_autospec(cf.cached_yf_earnings, return_value=YF_EARNINGS_ROWS)
    monkeypatch.setattr(cf, "cached_yf_earnings", yf_cache_get)
    fmp_cls = _fmp(monkeypatch)
    yf_fetch = _yf_fetch(monkeypatch, YF_EARNINGS_ROWS)

    resp = await calendar_mod.get_earnings_calendar(
        user_id="u1", from_date=FROM, to_date=TO
    )

    assert resp.count == 2
    yf_cache_get.assert_awaited_once_with(FROM, TO)
    fmp_cls.assert_not_called()
    yf_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_earnings_non_degradable_fmp_error_propagates(monkeypatch):
    _fmp_cache(monkeypatch)
    _yf_cache(monkeypatch)
    fmp_cls = _fmp(monkeypatch)
    fmp_cls.return_value.get_earnings_calendar_by_date.side_effect = FMPRequestError(
        "Invalid symbol for FMP", status_code=400
    )
    yf_fetch = _yf_fetch(monkeypatch, YF_EARNINGS_ROWS)

    with pytest.raises(FMPRequestError):
        await calendar_mod.get_earnings_calendar(
            user_id="u1", from_date=FROM, to_date=TO
        )

    yf_fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_from,bad_to", [
    ("notadate", TO),
    ("2026-13-01", TO),  # month 13 is not an ISO date
    (FROM, "2026/10/27"),  # slashes are not ISO
    ("20261026", TO),  # compact form: fromisoformat accepts it, the sources do not
    (TO, FROM),  # inverted range
])
async def test_earnings_bad_range_is_422_before_any_source(bad_from, bad_to, monkeypatch):
    fmp_cache = _fmp_cache(monkeypatch)
    yf_cold = _yf_cache(monkeypatch)
    fmp_cls = _fmp(monkeypatch)
    yf_fetch = _yf_fetch(monkeypatch, YF_EARNINGS_ROWS)

    with pytest.raises(HTTPException) as exc_info:
        await calendar_mod.get_earnings_calendar(
            user_id="u1", from_date=bad_from, to_date=bad_to
        )

    assert exc_info.value.status_code == 422
    fmp_cls.assert_not_called()
    fmp_cache.get.assert_not_awaited()
    yf_cold.assert_not_awaited()
    yf_fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_from,bad_to", [("notadate", TO), (TO, FROM)])
async def test_economic_bad_range_is_422(bad_from, bad_to, monkeypatch):
    fmp_cls = _fmp(monkeypatch)
    yf_econ = create_autospec(cf.economic_from_yfinance, return_value=[])
    monkeypatch.setattr(cf, "economic_from_yfinance", yf_econ)

    with pytest.raises(HTTPException) as exc_info:
        await calendar_mod.get_economic_calendar(
            user_id="u1", from_date=bad_from, to_date=bad_to
        )

    assert exc_info.value.status_code == 422
    fmp_cls.assert_not_called()
    yf_econ.assert_not_awaited()


# ---------------------------------------------------------------------------
# fmp_degradable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc,degradable", [
    (FmpQuotaExhausted(RESET), True),
    (FMPRequestError("FMP API request timed out"), True),  # no status: transport/timeout
    (FMPRequestError("FMP API limit reached", status_code=429), True),  # Limit Reach body
    (FMPRequestError("FMP API request failed (502)", status_code=502), True),
    (FMPRequestError("Invalid symbol for FMP", status_code=400), False),
    (FMPRequestError("key refused", status_code=401), False),
    (FMPRequestError("plan", status_code=402), False),
    (ValueError("a programming error"), False),
])
def test_fmp_degradable_covers_only_source_failures(exc, degradable):
    assert cf.fmp_degradable(exc) is degradable


# ---------------------------------------------------------------------------
# Earnings mapping, timezone handling, paging
# ---------------------------------------------------------------------------


def test_yf_earnings_rows_maps_the_fmp_shape():
    df = _earnings_df(
        [
            ("ACGL", "2026-10-27 20:00:00+00:00", 2.3, float("nan")),
            ("AAPL", "2026-10-27 20:00:00+00:00", 1.6, 1.64),
        ]
    )
    rows = cf._yf_earnings_rows(df)
    assert rows == [
        {
            "symbol": "ACGL",
            "date": "2026-10-27",  # 20:00 UTC is 4 PM ET, same calendar day
            "epsActual": None,  # NaN (not yet reported) → null
            "epsEstimated": 2.3,
            "revenueActual": None,
            "revenueEstimated": None,
            "lastUpdated": None,
        },
        {
            "symbol": "AAPL",
            "date": "2026-10-27",
            "epsActual": 1.64,
            "epsEstimated": 1.6,
            "revenueActual": None,
            "revenueEstimated": None,
            "lastUpdated": None,
        },
    ]


def test_yf_earnings_rows_treats_naive_timestamps_as_utc():
    # 2026-10-27 01:30 naive(UTC) = 2026-10-26 21:30 EDT
    df = _earnings_df([("BMO", "2026-10-27 01:30:00", 1.0, 1.1)], utc=False)
    assert cf._yf_earnings_rows(df)[0]["date"] == "2026-10-26"


def test_yf_earnings_rows_parses_naive_strings_as_utc_first():
    # A bare string is parsed as UTC, then converted to ET — not kept as-is.
    df = _earnings_df([("STR", "2026-10-27 01:30:00", 1.0, 1.1)], utc=None)
    assert cf._yf_earnings_rows(df)[0]["date"] == "2026-10-26"


def test_yf_earnings_rows_skips_rows_without_a_time():
    df = _earnings_df([("OK", "2026-10-27 20:00:00+00:00", 1.0, 1.1)])
    df.loc["NOTIME", :] = df.loc["OK", :]
    df.loc["NOTIME", "Event Start Date"] = pd.NaT
    rows = cf._yf_earnings_rows(df)
    assert [r["symbol"] for r in rows] == ["OK"]


def test_yf_earnings_rows_empty_page():
    assert cf._yf_earnings_rows(None) == []
    assert cf._yf_earnings_rows(pd.DataFrame()) == []


def _full_page(moment: str = "2026-10-26 12:00:00+00:00") -> pd.DataFrame:
    return _earnings_df(
        [("SYM%03d" % i, moment, 1.0, float("nan")) for i in range(100)]
    )


@pytest.mark.asyncio
async def test_yf_fetch_widens_end_and_keeps_the_et_evening_of_the_end_date(monkeypatch):
    import yfinance as yf

    # 2026-10-28 01:00 UTC = 2026-10-27 21:00 EDT: the ET evening of TO, which
    # a UTC-midnight end bound would slice off.
    page = _earnings_df(
        [("AAPL", "2026-10-28 01:00:00+00:00", 1.6, 1.64)]
        + [("EARLY", "2026-10-26 12:00:00+00:00", 1.0, 1.0)]
        + [("LATE", "2026-10-29 12:00:00+00:00", 1.0, 1.0)]  # past TO → clipped
    )
    cal_cls = create_autospec(yf.Calendars)
    cal_cls.return_value.get_earnings_calendar.side_effect = [page]
    monkeypatch.setattr(yf, "Calendars", cal_cls)

    rows = await cf.earnings_from_yfinance(FROM, TO)

    # end is to_date + 2: the ET evening of the end day lands next UTC day
    # (01:00 EDT / 02:00 EST), so only +2 always clears it.
    cal_cls.assert_called_once_with(start=FROM, end="2026-10-29")
    call = cal_cls.return_value.get_earnings_calendar.call_args
    assert call.kwargs["offset"] == 0
    assert call.kwargs["filter_most_active"] is False
    assert [r["symbol"] for r in rows] == ["AAPL", "EARLY"]
    assert rows[0]["date"] == "2026-10-27"  # the ET-evening row survives


@pytest.mark.asyncio
async def test_yf_fetch_covers_a_winter_est_evening_on_the_end_date(monkeypatch):
    import yfinance as yf

    # 2027-01-16 02:00 UTC = 2027-01-15 21:00 EST (winter, UTC-5).
    page = _earnings_df([("CPI", "2027-01-16 02:00:00+00:00", 1.0, 1.0)])
    cal_cls = create_autospec(yf.Calendars)
    cal_cls.return_value.get_earnings_calendar.side_effect = [page]
    monkeypatch.setattr(yf, "Calendars", cal_cls)

    rows = await cf.earnings_from_yfinance("2027-01-13", "2027-01-15")

    cal_cls.assert_called_once_with(start="2027-01-13", end="2027-01-17")
    assert rows[0]["date"] == "2027-01-15"


@pytest.mark.asyncio
async def test_yf_fetch_stops_on_a_short_page(monkeypatch):
    import yfinance as yf

    short = _earnings_df([("AAPL", "2026-10-26 12:00:00+00:00", 1.6, 1.64)])
    cal_cls = create_autospec(yf.Calendars)
    cal_cls.return_value.get_earnings_calendar.side_effect = [_full_page(), short]
    monkeypatch.setattr(yf, "Calendars", cal_cls)

    rows = await cf.earnings_from_yfinance(FROM, TO)

    assert len(rows) == 101
    assert cal_cls.return_value.get_earnings_calendar.call_count == 2


@pytest.mark.asyncio
async def test_yf_fetch_treats_a_missing_page_as_the_last_page(monkeypatch):
    import yfinance as yf

    cal_cls = create_autospec(yf.Calendars)
    cal_cls.return_value.get_earnings_calendar.side_effect = [_full_page(), None]
    monkeypatch.setattr(yf, "Calendars", cal_cls)

    rows = await cf.earnings_from_yfinance(FROM, TO)

    assert len(rows) == 100  # the first page survives; None fails nothing


@pytest.mark.asyncio
async def test_yf_fetch_pages_to_20_and_warns_at_the_cap(monkeypatch, caplog):
    import yfinance as yf

    cal_cls = create_autospec(yf.Calendars)
    cal_cls.return_value.get_earnings_calendar.side_effect = [
        _full_page()
    ] * cf._YF_EARNINGS_MAX_PAGES
    monkeypatch.setattr(yf, "Calendars", cal_cls)

    with caplog.at_level("WARNING", logger="src.dalton.market_data.calendar_fallback"):
        rows = await cf.earnings_from_yfinance(FROM, TO)

    assert cal_cls.return_value.get_earnings_calendar.call_count == 20
    assert len(rows) == 2000
    assert "yf_truncated" in caplog.text
    assert f"window={FROM}..{TO}" in caplog.text


# ---------------------------------------------------------------------------
# Economic calendar: mapping, fetch, endpoint
# ---------------------------------------------------------------------------


def test_yf_economic_rows_maps_the_fmp_shape():
    df = _economic_df(
        [
            ("Core CPI MM", "US", "2026-10-27 12:30:00+00:00", float("nan"), 0.2, 0.1),
            ("Bank Rate", "GB", "2026-10-27 11:00:00+00:00", 4.25, 4.25, 4.0),
        ]
    )
    rows = cf._yf_economic_rows(df)
    assert rows == [
        {
            "date": "2026-10-27 08:30:00",  # ET wall clock
            "country": "US",
            "event": "Core CPI MM",
            "currency": "",  # Yahoo publishes none; the model requires the key
            "previous": 0.1,  # "Last" = prior release
            "estimate": 0.2,  # "Expected" = consensus
            "actual": None,  # NaN (not yet released) → null
            "change": None,  # not published by Yahoo
            "impact": None,  # not published by Yahoo
            "changePercentage": None,  # not published by Yahoo
        },
        {
            "date": "2026-10-27 07:00:00",
            "country": "GB",
            "event": "Bank Rate",
            "currency": "",
            "previous": 4.0,
            "estimate": 4.25,
            "actual": 4.25,
            "change": None,
            "impact": None,
            "changePercentage": None,
        },
    ]


def test_yf_economic_rows_skips_rows_without_a_time():
    df = _economic_df([("OK", "US", "2026-10-27 12:30:00+00:00", 1.0, 1.0, 1.0)])
    df.loc["NOTIME", :] = df.loc["OK", :]
    df.loc["NOTIME", "Event Time"] = pd.NaT
    assert [r["event"] for r in cf._yf_economic_rows(df)] == ["OK"]


@pytest.mark.asyncio
async def test_yf_economic_fetch_pages_with_full_limit_and_clips(monkeypatch):
    import yfinance as yf

    page = _economic_df(
        [
            ("Core CPI MM", "US", "2026-10-27 12:30:00+00:00", 0.2, 0.2, 0.1),
            ("Late Event", "SG", "2026-10-29 12:00:00+00:00", 1.0, 1.0, 1.0),  # past TO
        ]
    )
    cal_cls = create_autospec(yf.Calendars)
    cal_cls.return_value.get_economic_events_calendar.side_effect = [page]
    monkeypatch.setattr(yf, "Calendars", cal_cls)

    rows = await cf.economic_from_yfinance(FROM, TO)

    cal_cls.assert_called_once_with(start=FROM, end="2026-10-29")
    call = cal_cls.return_value.get_economic_events_calendar.call_args
    assert call.kwargs["limit"] == 100  # yfinance's own default is 12
    assert call.kwargs["offset"] == 0
    assert [r["event"] for r in rows] == ["Core CPI MM"]


@pytest.mark.asyncio
async def test_economic_quota_exhausted_falls_back_to_yfinance(monkeypatch):
    fmp_cls = _fmp(monkeypatch)
    fmp_cls.return_value.get_economic_calendar.side_effect = FmpQuotaExhausted(RESET)
    yf_econ = create_autospec(cf.economic_from_yfinance, return_value=YF_ECONOMIC_ROWS)
    monkeypatch.setattr(cf, "economic_from_yfinance", yf_econ)

    resp = await calendar_mod.get_economic_calendar(
        user_id="u1", from_date=FROM, to_date=TO
    )

    assert resp.count == 1
    assert resp.data == [EconomicEvent(**r) for r in YF_ECONOMIC_ROWS]
    yf_econ.assert_awaited_once_with(FROM, TO)
    fmp_cls.return_value.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_economic_fmp_unavailable_uses_yfinance(monkeypatch):
    fmp_cls = _fmp(monkeypatch)
    fmp_cls.side_effect = ValueError("FMP API key required")
    yf_econ = create_autospec(cf.economic_from_yfinance, return_value=YF_ECONOMIC_ROWS)
    monkeypatch.setattr(cf, "economic_from_yfinance", yf_econ)

    resp = await calendar_mod.get_economic_calendar(user_id="u1", from_date=FROM, to_date=TO)

    assert resp.count == 1
    yf_econ.assert_awaited_once()


@pytest.mark.asyncio
async def test_economic_non_degradable_fmp_error_propagates(monkeypatch):
    fmp_cls = _fmp(monkeypatch)
    fmp_cls.return_value.get_economic_calendar.side_effect = FMPRequestError(
        "FMP API request failed (402)", status_code=402
    )
    yf_econ = create_autospec(cf.economic_from_yfinance, return_value=YF_ECONOMIC_ROWS)
    monkeypatch.setattr(cf, "economic_from_yfinance", yf_econ)

    with pytest.raises(FMPRequestError):
        await calendar_mod.get_economic_calendar(user_id="u1", from_date=FROM, to_date=TO)

    yf_econ.assert_not_awaited()


@pytest.mark.asyncio
async def test_economic_both_fail_returns_empty(monkeypatch):
    fmp_cls = _fmp(monkeypatch)
    fmp_cls.return_value.get_economic_calendar.side_effect = FmpQuotaExhausted(RESET)
    yf_econ = create_autospec(
        cf.economic_from_yfinance, side_effect=ValueError("yahoo down")
    )
    monkeypatch.setattr(cf, "economic_from_yfinance", yf_econ)

    resp = await calendar_mod.get_economic_calendar(user_id="u1", from_date=FROM, to_date=TO)

    assert resp.data == []
    assert resp.count == 0


@pytest.mark.asyncio
async def test_economic_success_passes_through(monkeypatch):
    fmp_cls = _fmp(monkeypatch)
    fmp_cls.return_value.get_economic_calendar.return_value = [
        {
            "date": "2026-10-27 03:35:00",
            "country": "US",
            "event": "3-Month Bill Auction",
            "currency": "USD",
        }
    ]
    yf_econ = create_autospec(cf.economic_from_yfinance, return_value=[])
    monkeypatch.setattr(cf, "economic_from_yfinance", yf_econ)

    resp = await calendar_mod.get_economic_calendar(user_id="u1", from_date=FROM, to_date=TO)

    assert resp.count == 1
    assert resp.data[0].event == "3-Month Bill Auction"
    yf_econ.assert_not_awaited()


# ---------------------------------------------------------------------------
# The yfinance answer cache
# ---------------------------------------------------------------------------


def _redis(monkeypatch, client: Any) -> None:
    monkeypatch.setattr(
        redis_cache,
        "get_cache_client",
        create_autospec(redis_cache.get_cache_client, return_value=client),
    )


@pytest.mark.asyncio
async def test_yf_cache_round_trips_under_its_own_key(monkeypatch):
    client = create_autospec(RedisCacheClient, instance=True)
    client.get.return_value = json.dumps(YF_EARNINGS_ROWS)
    _redis(monkeypatch, client)

    assert await cf.cached_yf_earnings(FROM, TO) == YF_EARNINGS_ROWS
    client.get.assert_awaited_once_with(f"earnings:yf:{FROM}:{TO}")


@pytest.mark.asyncio
async def test_yf_cache_miss_and_redis_trouble_are_none(monkeypatch):
    client = create_autospec(RedisCacheClient, instance=True)
    client.get.return_value = None
    _redis(monkeypatch, client)
    assert await cf.cached_yf_earnings(FROM, TO) is None

    client.get.side_effect = ConnectionError("redis down")
    assert await cf.cached_yf_earnings(FROM, TO) is None


@pytest.mark.asyncio
async def test_yf_cache_stores_for_an_hour_and_never_empty(monkeypatch):
    client = create_autospec(RedisCacheClient, instance=True)
    _redis(monkeypatch, client)

    await cf.store_yf_earnings([], FROM, TO)  # empty is not an answer
    client.set.assert_not_awaited()

    await cf.store_yf_earnings(YF_EARNINGS_ROWS, FROM, TO)
    client.set.assert_awaited_once_with(
        f"earnings:yf:{FROM}:{TO}", json.dumps(YF_EARNINGS_ROWS), ttl=3600
    )


# ---------------------------------------------------------------------------
# Sector performance fallback
# ---------------------------------------------------------------------------


def _sector_provider(monkeypatch, answer: Any) -> Any:
    """Point the overview at a provider whose sector call raises or returns *answer*."""
    from src.data_client.financial_data_provider import FinancialDataProvider

    provider = create_autospec(FinancialDataProvider, instance=True)
    provider.financial = create_autospec(FinancialDataSource, instance=True)
    if isinstance(answer, BaseException):
        provider.financial.get_sector_performance.side_effect = answer
    else:
        provider.financial.get_sector_performance.return_value = answer
    get_provider = create_autospec(
        market_overview.get_financial_data_provider, return_value=provider
    )
    monkeypatch.setattr(market_overview, "get_financial_data_provider", get_provider)
    return provider


def _yf_sector_cls(monkeypatch, rows: list[dict[str, Any]] | None) -> Any:
    yf_cls = create_autospec(YFinanceFinancialSource)
    if rows is not None:
        yf_cls.return_value.get_sector_performance.return_value = rows
    import src.data_client.yfinance.financial_source as yf_fin

    monkeypatch.setattr(yf_fin, "YFinanceFinancialSource", yf_cls)
    return yf_cls


@pytest.mark.asyncio
async def test_sector_dateless_provider_failure_falls_back_to_yfinance(monkeypatch):
    _sector_provider(monkeypatch, FmpQuotaExhausted(RESET))
    yf_cls = _yf_sector_cls(monkeypatch, SECTOR_ROWS)

    content, artifact = await market_overview.fetch_sector_performance(date=None)

    assert "Technology" in content and "+1.10%" in content
    assert artifact["sectors"] == [
        {"sector": "Technology", "changePercentage": 1.1},
        {"sector": "Energy", "changePercentage": -0.4},
    ]
    yf_cls.return_value.get_sector_performance.assert_awaited_once()


@pytest.mark.asyncio
async def test_sector_dateless_fallback_failure_shows_no_data(monkeypatch):
    _sector_provider(monkeypatch, FmpQuotaExhausted(RESET))
    _yf_sector_cls(monkeypatch, [])

    content, artifact = await market_overview.fetch_sector_performance(date=None)

    # B2: the exhausted quota is named, not swallowed as an empty table.
    assert "额度已用完" in content
    assert artifact["sectors"] == []
    assert artifact["error"] == "fmp_quota_exhausted"


@pytest.mark.asyncio
async def test_sector_dated_failure_stays_fmp_only(monkeypatch):
    provider = _sector_provider(monkeypatch, FmpQuotaExhausted(RESET))
    yf_cls = _yf_sector_cls(monkeypatch, SECTOR_ROWS)

    content, artifact = await market_overview.fetch_sector_performance(date="2026-10-23")

    provider.financial.get_sector_performance.assert_awaited_once_with(
        target_date="2026-10-23"
    )
    yf_cls.return_value.get_sector_performance.assert_not_awaited()
    # B2: a dated request reports the quota, never yfinance's today snapshot.
    assert "额度已用完" in content
    assert "2026-10-23" in content
    assert artifact["sectors"] == []
    assert artifact["error"] == "fmp_quota_exhausted"


@pytest.mark.asyncio
async def test_sector_provider_success_skips_yfinance(monkeypatch):
    provider = _sector_provider(
        monkeypatch,
        [
            {"sector": "Technology", "changePctStr": "+1.10%", "date": "2026-10-23"},
            {"sector": "Energy", "changePctStr": "-0.40%", "date": "2026-10-23"},
        ],
    )
    yf_cls = _yf_sector_cls(monkeypatch, SECTOR_ROWS)

    content, _ = await market_overview.fetch_sector_performance(date="2026-10-23")

    provider.financial.get_sector_performance.assert_awaited_once_with(
        target_date="2026-10-23"
    )
    yf_cls.return_value.get_sector_performance.assert_not_awaited()
    assert "Technology" in content


def test_lone_future_from_defaults_to_a_week_after_it():
    assert calendar_mod._default_dates("2026-12-01", None) == ("2026-12-01", "2026-12-08")
    calendar_mod._validate_date_range(*calendar_mod._default_dates("2026-12-01", None))


@pytest.mark.asyncio
async def test_malformed_fmp_row_is_not_cached(monkeypatch):
    fmp_cache = _fmp_cache(monkeypatch)
    _yf_cache(monkeypatch)
    fmp_cls = _fmp(monkeypatch)
    fmp_cls.return_value.get_earnings_calendar_by_date.return_value = [{"symbol": None, "date": FROM}]

    with pytest.raises(ValidationError):
        await calendar_mod.get_earnings_calendar(user_id="u1", from_date=FROM, to_date=TO)

    fmp_cache.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_yfinance_row_is_not_cached(monkeypatch):
    _fmp_cache(monkeypatch)
    yf_store = _yf_cache(monkeypatch)
    fmp_cls = _fmp(monkeypatch)
    fmp_cls.return_value.get_earnings_calendar_by_date.side_effect = FmpQuotaExhausted(RESET)
    _yf_fetch(monkeypatch, [{"symbol": None, "date": FROM}])

    with pytest.raises(ValidationError):
        await calendar_mod.get_earnings_calendar(user_id="u1", from_date=FROM, to_date=TO)

    yf_store.assert_not_awaited()


def test_from_near_date_max_clamps_to_and_stays_valid():
    assert calendar_mod._default_dates("9999-12-28", None) == ("9999-12-28", "9999-12-31")
    calendar_mod._validate_date_range("9999-12-28", "9999-12-31")
