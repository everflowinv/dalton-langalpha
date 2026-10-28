"""Unit tests for the yfinance data source — no network."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, NonCallableMagicMock, PropertyMock, create_autospec, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import yfinance as yf

from src.dalton.extended_hours import _now_utc  # DALTON-HOOK(extended-hours-quotes)
from src.data_client.yfinance.data_source import (
    YFinanceDataSource,
    _fetch_history,
    _fetch_single_snapshot,  # DALTON-HOOK(extended-hours-quotes)
    _place_in_sessions,
)
from src.data_client.yfinance.yahoo import yahoo_ticker


@pytest.fixture(autouse=True)
def _isolated_prev_close_daily_cache():
    """These stubs carry per-test daily frames; the snapshot path's (symbol,
    trading date) memo must not leak one test's bars into another."""
    from src.data_client.yfinance import data_source as _ds

    _ds._clear_prev_close_daily_cache()
    yield
    _ds._clear_prev_close_daily_cache()


def _history_kwargs(**call_kwargs) -> dict:
    """Run _fetch_history with a stubbed Ticker; return the kwargs it passed."""
    ticker = MagicMock()
    ticker.history.return_value = None  # empty result — we only care about kwargs
    with patch("src.data_client.yfinance.yahoo.yf.Ticker", return_value=ticker):
        _fetch_history("0700.HK", "1m", **call_kwargs)
    return ticker.history.call_args.kwargs


def test_fetch_history_end_date_is_inclusive():
    """to_date is inclusive per the provider contract, but yfinance's ``end``
    is an exclusive exchange-local midnight. Without the +1-day shift, a window
    ending on the current venue date silently drops today's live session —
    non-US charts froze on the prior session (frozen 0700.HK chart bug).
    """
    kwargs = _history_kwargs(start="2026-07-03", end="2026-07-06")
    assert kwargs["start"] == "2026-07-03"
    assert kwargs["end"] == "2026-07-07"


def test_fetch_history_open_ended_window_has_no_end_bound():
    kwargs = _history_kwargs(start="2026-07-03", end=None)
    assert kwargs["start"] == "2026-07-03"
    assert "end" not in kwargs


def test_fetch_history_unparseable_end_passes_through():
    kwargs = _history_kwargs(start=None, end="garbage")
    assert kwargs["end"] == "garbage"


def _cst(hhmm: str) -> int:
    """Epoch ms of 2026-09-30 HH:MM in Shanghai; the base is that midnight."""
    h, m = map(int, hhmm.split(":"))
    return 1790697600000 + (h * 60 + m) * 60_000


def test_a_shares_drop_yahoo_lunch_padding_and_start_the_afternoon_at_the_reopen():
    """Yahoo padded 11:30-13:00 with flat zero-volume 5m bars (66 a day, not
    48), and its hourly grid put the 13:00-13:30 trading in a 12:30 bar."""
    five = [{"time": _cst(t), "volume": v} for t, v in (("11:25", 9), ("11:30", 0), ("12:55", 0), ("13:00", 7))]
    assert [b["time"] for b in _place_in_sessions(five, "000001.SZ", False, "5min")] == [
        _cst("11:25"), _cst("13:00"),
    ]
    hourly = [{"time": _cst(t), "volume": 1} for t in ("10:30", "11:30", "12:30", "13:30")]
    assert [b["time"] for b in _place_in_sessions(hourly, "000001.SZ", False, "1hour")] == [
        _cst("10:30"), _cst("13:00"), _cst("13:30"),
    ]
    us = [{"time": _cst("12:00"), "volume": 1}]
    assert _place_in_sessions(us, "AAPL", False, "5min") == us


@pytest.mark.asyncio
async def test_get_snapshots_returns_bare_index_symbol():
    """Indices are queried from Yahoo with a caret ("^GSPC"), but the snapshot
    must echo back the bare requested symbol ("GSPC") so the provider chain
    matches it instead of dropping it as unrequested. Regression for #287.
    """
    def fake_fetch(sym: str) -> dict:
        # Mirrors the real fetch: echoes whatever symbol it was queried with.
        return {"symbol": sym, "price": 5000.0}

    with patch(
        "src.data_client.yfinance.data_source._fetch_single_snapshot",
        side_effect=fake_fetch,
    ):
        result = await YFinanceDataSource().get_snapshots(
            ["GSPC"], asset_type="indices"
        )

    assert result == [{"symbol": "GSPC", "price": 5000.0}]


@pytest.mark.asyncio
async def test_get_snapshots_preserves_order_and_drops_failures():
    """Symbol restoration stays aligned when a fetch returns None."""
    def fake_fetch(sym: str) -> dict | None:
        return None if sym == "^IXIC" else {"symbol": sym, "price": 1.0}

    with patch(
        "src.data_client.yfinance.data_source._fetch_single_snapshot",
        side_effect=fake_fetch,
    ):
        result = await YFinanceDataSource().get_snapshots(
            ["GSPC", "IXIC", "DJI"], asset_type="indices"
        )

    assert [r["symbol"] for r in result] == ["GSPC", "DJI"]


@pytest.mark.asyncio
async def test_get_snapshots_skips_only_the_symbol_yahoo_cannot_be_asked_for():
    """An unspellable symbol is a miss, not a failure of the batch; a batch of
    nothing else is refused with the reason."""
    def fake_fetch(sym: str) -> dict:
        return {"symbol": sym, "price": 1.0}

    with patch(
        "src.data_client.yfinance.data_source._fetch_single_snapshot",
        side_effect=fake_fetch,
    ) as fetch:
        result = await YFinanceDataSource().get_snapshots(["AAPL", "EUR/USD", "MSFT"])
        with pytest.raises(ValueError, match="EUR/USD"):
            await YFinanceDataSource().get_snapshots(["EUR/USD"])

    assert [r["symbol"] for r in result] == ["AAPL", "MSFT"]
    assert [c.args[0] for c in fetch.call_args_list] == ["AAPL", "MSFT"]


@pytest.mark.asyncio
async def test_get_snapshots_stocks_pass_symbol_through_unchanged():
    """Stocks aren't caret-prefixed; the symbol is returned as requested."""
    def fake_fetch(sym: str) -> dict:
        return {"symbol": sym, "price": 190.0}

    with patch(
        "src.data_client.yfinance.data_source._fetch_single_snapshot",
        side_effect=fake_fetch,
    ):
        result = await YFinanceDataSource().get_snapshots(["AAPL"])

    assert result == [{"symbol": "AAPL", "price": 190.0}]


# DALTON-HOOK(extended-hours-quotes): Lock extended-hours snapshot mapping (pre/post/none) and info failure fallback.
def _make_stub_ticker(
    fast_info: dict[str, object],
    info: object,
    regular_market_time: int | None = 1792612800,
) -> tuple[yf.Ticker, PropertyMock]:
    ticker = create_autospec(yf.Ticker, instance=True)
    ticker.fast_info = fast_info
    ticker.history_metadata = (
        {"regularMarketTime": regular_market_time} if regular_market_time is not None else {}
    )
    # DALTON-HOOK(yfinance-prev-close): a 1mo daily frame whose last valid bar
    # before the quote's own session closes at the fast_info previous close,
    # so these mapping tests exercise the extended-hours layer over a
    # confirmed previous close, not the "cannot verify" fallback.
    frame = _confirming_daily_frame(fast_info, regular_market_time)
    if frame is not None:
        ticker.history.side_effect = (
            lambda *a, **kw: frame if kw.get("period") == "1mo" else pd.DataFrame()
        )
    info_prop = (
        PropertyMock(side_effect=info)
        if isinstance(info, Exception)
        else PropertyMock(return_value=info)
    )
    object.__dict__["__class__"].__set__(
        ticker,
        type("_TickerInstanceMock", (NonCallableMagicMock,), {"info": info_prop}),
    )
    return ticker, info_prop


def _confirming_daily_frame(
    fast_info: dict[str, object], regular_market_time: int | None
) -> pd.DataFrame | None:
    prev = fast_info.get("regularMarketPreviousClose", fast_info.get("previousClose"))
    if regular_market_time is None or not isinstance(prev, (int, float)) or not math.isfinite(prev):
        return None
    quote_day = datetime.fromtimestamp(
        regular_market_time, tz=ZoneInfo("America/New_York")
    ).date()
    days = [quote_day - timedelta(days=offset) for offset in (3, 2, 1)]
    closes = [float(prev)] * len(days)
    volumes = [1_000_000] * len(days)
    last = fast_info.get("lastPrice")
    if isinstance(last, (int, float)) and math.isfinite(last):
        days.append(quote_day)
        closes.append(float(last))
        volumes.append(1_000_000)
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": volumes},
        index=pd.DatetimeIndex([pd.Timestamp(d) for d in days]),
    )


def test_fetch_single_snapshot_maps_pre_market_fields():
    # DALTON-HOOK(extended-hours-quotes): Pre-market aligns previous_close/price/regular_close to yesterday's close.
    # 2026-10-21 16:00 ET = 1792612800; 2026-10-22 08:04 ET = 1792670640.
    pre_now = datetime.fromtimestamp(1792670640, tz=timezone.utc)
    stub_ticker, info_prop = _make_stub_ticker(
        fast_info={
            "lastPrice": 330.95,
            "regularMarketPreviousClose": 336.40,
            "open": 335.0,
            "dayHigh": 336.0,
            "dayLow": 330.0,
            "lastVolume": 19_200_000,
        },
        info={
            "marketState": "PRE",
            "preMarketPrice": 328.50,
            # Pass an intentionally off preMarketChangePercent to verify we compute from prices.
            "preMarketChangePercent": -99.0,
            "preMarketTime": 1792670640,
        },
        regular_market_time=1792612800,
    )
    mock_yahoo_ticker = create_autospec(yahoo_ticker, return_value=stub_ticker)
    mock_now = create_autospec(_now_utc, return_value=pre_now)
    with (
        patch("src.data_client.yfinance.data_source.yahoo_ticker", new=mock_yahoo_ticker),
        patch("src.dalton.extended_hours._now_utc", new=mock_now),
    ):
        snap = _fetch_single_snapshot("GOOG")

    assert info_prop.call_count == 1
    assert snap is not None
    assert snap["price"] == 330.95
    assert snap["regular_close"] == 330.95
    assert snap["previous_close"] == 330.95
    assert snap["last_trade_price"] == 328.50
    assert snap["early_trading_change"] == -2.45
    assert snap["early_trading_change_percent"] == -0.7403
    assert snap["regular_trading_change"] == -5.45
    assert snap["regular_trading_change_percent"] == -1.6201
    assert snap["change"] == -2.45
    assert snap["change_percent"] == -0.7403
    assert snap["late_trading_change"] is None
    assert snap["late_trading_change_percent"] is None
    assert snap["market_status"] == "early_trading"
    assert snap["regular_only"] is False
    assert snap["as_of"] == 1792670640 * 1000
    assert snap["regular_as_of"] == 1792612800 * 1000
    assert snap["regular_trading_date"] == "2026-10-21"


def test_fetch_single_snapshot_maps_post_market_fields():
    # DALTON-HOOK(extended-hours-quotes): Post-market works in 16:00-20:00 ET, 20:30 ET, next day 02:00 ET, and Saturday 12:00 ET.
    # Thursday 2026-10-22 16:00 ET = 1792699200; trade at 17:00 ET = 1792702800.
    # Checked at: 17:15 ET (1792703700), 20:30 ET (1792715400), Friday 02:00 ET (1792735200).
    for now_ts, market_state in (
        (1792703700, "POST"),
        (1792715400, "POSTPOST"),
        (1792735200, "POSTPOST"),
    ):
        stub_ticker, info_prop = _make_stub_ticker(
            fast_info={
                "lastPrice": 330.95,
                "regularMarketPreviousClose": 336.40,
                "lastVolume": 19_200_000,
            },
            info={
                "marketState": market_state,
                "postMarketPrice": 333.76,
                "postMarketChangePercent": 99.0,
                "postMarketTime": 1792702800,
            },
            regular_market_time=1792699200,
        )
        mock_yahoo_ticker = create_autospec(yahoo_ticker, return_value=stub_ticker)
        mock_now = create_autospec(
            _now_utc, return_value=datetime.fromtimestamp(now_ts, tz=timezone.utc)
        )
        with (
            patch("src.data_client.yfinance.data_source.yahoo_ticker", new=mock_yahoo_ticker),
            patch("src.dalton.extended_hours._now_utc", new=mock_now),
        ):
            snap = _fetch_single_snapshot("GOOG")

        assert info_prop.call_count == 1
        assert snap is not None
        assert snap["price"] == 330.95
        assert snap["regular_close"] == 330.95
        assert snap["previous_close"] == 336.40
        assert snap["last_trade_price"] == 333.76
        assert snap["late_trading_change"] == 2.81
        assert snap["late_trading_change_percent"] == 0.8491
        assert snap["regular_trading_change"] == -5.45
        assert snap["regular_trading_change_percent"] == -1.6201
        assert snap["change"] == -2.64
        assert snap["change_percent"] == -0.7848
        assert snap["early_trading_change"] is None
        assert snap["early_trading_change_percent"] is None
        assert snap["market_status"] == "late_trading"
        assert snap["regular_only"] is False
        assert snap["as_of"] == 1792702800 * 1000
        assert snap["regular_as_of"] == 1792699200 * 1000
        assert snap["regular_trading_date"] == "2026-10-22"

    # Saturday 2026-10-24 12:00 ET (1792857600) with Friday 2026-10-23 close (16:00 ET = 1792785600)
    # and Friday after-hours print (18:30 ET = 1792794600).
    sat_ticker, sat_info_prop = _make_stub_ticker(
        fast_info={
            "lastPrice": 330.95,
            "regularMarketPreviousClose": 336.40,
            "lastVolume": 19_200_000,
        },
        info={
            "marketState": "POSTPOST",
            "postMarketPrice": 333.76,
            "postMarketTime": 1792794600,
        },
        regular_market_time=1792785600,
    )
    mock_sat_ticker = create_autospec(yahoo_ticker, return_value=sat_ticker)
    mock_sat_now = create_autospec(
        _now_utc, return_value=datetime.fromtimestamp(1792857600, tz=timezone.utc)
    )
    with (
        patch("src.data_client.yfinance.data_source.yahoo_ticker", new=mock_sat_ticker),
        patch("src.dalton.extended_hours._now_utc", new=mock_sat_now),
    ):
        sat_snap = _fetch_single_snapshot("GOOG")

    assert sat_info_prop.call_count == 1
    assert sat_snap is not None
    assert sat_snap["market_status"] == "late_trading"
    assert sat_snap["regular_only"] is False
    assert sat_snap["last_trade_price"] == 333.76
    assert sat_snap["as_of"] == 1792794600 * 1000
    assert sat_snap["regular_trading_date"] == "2026-10-23"


def test_fetch_single_snapshot_keeps_regular_only_when_no_extended_data_or_info_fails():
    # DALTON-HOOK(extended-hours-quotes): Regular hours skip info (call_count == 0); stale times and errors fall back.
    # 1) Regular hours (2026-10-22 14:30 ET = 1792693800): info getter is never called.
    reg_ticker, reg_info_prop = _make_stub_ticker(
        fast_info={"lastPrice": 330.95, "regularMarketPreviousClose": 336.40, "lastVolume": 100},
        info={"marketState": "REGULAR"},
        regular_market_time=1792693800,
    )
    mock_yahoo_ticker = create_autospec(yahoo_ticker, return_value=reg_ticker)
    mock_reg_now = create_autospec(
        _now_utc, return_value=datetime.fromtimestamp(1792693800, tz=timezone.utc)
    )
    with (
        patch("src.data_client.yfinance.data_source.yahoo_ticker", new=mock_yahoo_ticker),
        patch("src.dalton.extended_hours._now_utc", new=mock_reg_now),
    ):
        reg_snap = _fetch_single_snapshot("GOOG")

    assert reg_info_prop.call_count == 0
    assert reg_snap is not None
    assert reg_snap["market_status"] is None
    assert reg_snap["early_trading_change_percent"] is None
    assert reg_snap["late_trading_change_percent"] is None
    assert reg_snap["regular_only"] is True
    assert "last_trade_price" not in reg_snap

    # 2) Pre-market where info returns a valid dict but preMarketTime is not after regular close: rejected.
    pre_now = datetime.fromtimestamp(1792670640, tz=timezone.utc)
    stale_pre_ticker, stale_pre_prop = _make_stub_ticker(
        fast_info={"lastPrice": 330.95, "regularMarketPreviousClose": 336.40, "lastVolume": 100},
        info={"marketState": "PRE", "preMarketPrice": 328.50, "preMarketTime": 1792612800},
        regular_market_time=1792612800,
    )
    mock_stale_pre = create_autospec(yahoo_ticker, return_value=stale_pre_ticker)
    mock_pre_now = create_autospec(_now_utc, return_value=pre_now)
    with (
        patch("src.data_client.yfinance.data_source.yahoo_ticker", new=mock_stale_pre),
        patch("src.dalton.extended_hours._now_utc", new=mock_pre_now),
    ):
        stale_pre_snap = _fetch_single_snapshot("GOOG")

    assert stale_pre_prop.call_count == 1
    assert stale_pre_snap is not None
    assert stale_pre_snap["regular_only"] is True
    assert stale_pre_snap["as_of"] == 1792612800 * 1000

    # 3) Post-market where info returns a valid dict but postMarketTime is earlier than regular close: rejected.
    post_now = datetime.fromtimestamp(1792715400, tz=timezone.utc)  # 20:30 ET
    stale_post_ticker, stale_post_prop = _make_stub_ticker(
        fast_info={"lastPrice": 330.95, "regularMarketPreviousClose": 336.40, "lastVolume": 100},
        info={"marketState": "POSTPOST", "postMarketPrice": 333.76, "postMarketTime": 1792699100},
        regular_market_time=1792699200,
    )
    mock_stale_post = create_autospec(yahoo_ticker, return_value=stale_post_ticker)
    mock_post_now = create_autospec(_now_utc, return_value=post_now)
    with (
        patch("src.data_client.yfinance.data_source.yahoo_ticker", new=mock_stale_post),
        patch("src.dalton.extended_hours._now_utc", new=mock_post_now),
    ):
        stale_post_snap = _fetch_single_snapshot("GOOG")

    assert stale_post_prop.call_count == 1
    assert stale_post_snap is not None
    assert stale_post_snap["regular_only"] is True
    assert stale_post_snap["as_of"] == 1792699200 * 1000

    # 4) Pre-market where ticker.info raises: falls back to regular snapshot, never None.
    err_ticker, err_prop = _make_stub_ticker(
        fast_info={"lastPrice": 330.95, "regularMarketPreviousClose": 336.40, "lastVolume": 100},
        info=RuntimeError("Yahoo quoteSummary HTTP 500"),
        regular_market_time=1792612800,
    )
    mock_err_ticker = create_autospec(yahoo_ticker, return_value=err_ticker)
    with (
        patch("src.data_client.yfinance.data_source.yahoo_ticker", new=mock_err_ticker),
        patch("src.dalton.extended_hours._now_utc", new=mock_pre_now),
    ):
        fallback_snap = _fetch_single_snapshot("GOOG")

    assert err_prop.call_count == 1
    assert fallback_snap is not None
    assert fallback_snap["price"] == 330.95
    assert fallback_snap["market_status"] is None
    assert fallback_snap["early_trading_change_percent"] is None
    assert fallback_snap["late_trading_change_percent"] is None
    assert fallback_snap["regular_only"] is True
