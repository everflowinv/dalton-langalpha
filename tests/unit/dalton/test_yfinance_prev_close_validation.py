"""B1: date-aware previous-close validation on the yfinance snapshot path.

Locks the 2525.HK regression (2026-10): Yahoo froze the daily tail flat at
zero volume and yfinance's ``regularMarketPreviousClose`` rode that tail, so
the quote showed a stale −16.61%. The previous close must come from the last
valid session (traded volume) before the quote's own trading date, a conflict
with fast_info must surface as unknown (never as a wrong percent), and a
missing previous close must never be reported as a 0.0% day.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import create_autospec, patch

import pandas as pd
import pytest
import yfinance as yf

from src.data_client.yfinance.data_source import (
    _fetch_single_snapshot,
    _snapshot_from_history,
)
from src.data_client.yfinance.yahoo import validated_previous_close


@pytest.fixture(autouse=True)
def _isolated_prev_close_daily_cache():
    """The snapshot path memoizes daily bars per (symbol, trading date); these
    tests stub different frames for the same symbol and must not share them."""
    from src.data_client.yfinance import data_source as _ds

    _ds._clear_prev_close_daily_cache()
    yield
    _ds._clear_prev_close_daily_cache()


def _frozen_hk_rows() -> list[tuple[str, float, float]]:
    # The exported 2525.HK history: a real close 10/12, Yahoo's frozen fill
    # 10/13–10/23 (flat 15.47, zero volume), then the resumed session 10/26.
    rows = [("2026-10-08", 15.6, 2_000_000), ("2026-10-09", 15.5, 1_800_000),
            ("2026-10-12", 15.47, 2_400_000)]
    rows += [("2026-10-13", 15.47, 0.0), ("2026-10-16", 15.47, 0.0),
             ("2026-10-19", 15.47, 0.0), ("2026-10-20", 15.47, 0.0),
             ("2026-10-21", 15.47, 0.0), ("2026-10-22", 15.47, 0.0),
             ("2026-10-23", 15.47, 0.0)]
    rows.append(("2026-10-26", 13.57, 48_924_388))
    return rows


def test_frozen_tail_conflict_returns_unknown():
    # 10/27 quote; the last valid session before it closed 13.57, while the
    # fast_info number still rides the frozen 15.47 — neither is trusted.
    prev = validated_previous_close(15.47, _frozen_hk_rows(), "2026-10-27")
    assert prev is None


def test_agreeing_fast_info_value_is_kept():
    prev = validated_previous_close(13.57, _frozen_hk_rows(), "2026-10-27")
    # 13.57 vs daily 13.57: confirmed.
    assert prev == 13.57


def test_resumption_day_previous_close_skips_frozen_rows():
    # On the resumed session itself, the previous *valid* session is 10/12.
    prev = validated_previous_close(15.47, _frozen_hk_rows(), "2026-10-26")
    assert prev == 15.47


def test_missing_fast_info_prev_uses_daily_valid_row():
    prev = validated_previous_close(None, _frozen_hk_rows(), "2026-10-27")
    assert prev == 13.57


def test_all_frozen_before_quote_is_unknown():
    # A window whose every row is a zero-volume fill leaves no valid previous
    # session — but the same shape is an index's or FX pair's honest window
    # (they carry no volume at all), so the date-adjacent row stands and the
    # fast_info agreement decides; a genuine conflict is what returns None.
    rows = [("2026-10-19", 15.47, 0.0), ("2026-10-20", 15.47, 0.0)]
    assert validated_previous_close(15.47, rows, "2026-10-21") == 15.47
    assert validated_previous_close(16.10, rows, "2026-10-21") is None


def test_volumeless_instrument_uses_date_adjacent_row():
    # Indexes carry no volume anywhere in the window; the bar before the
    # quote's date is its previous session, not a "frozen" fill.
    rows = [("2026-10-24", 5_801.0, 0.0), ("2026-10-27", 5_822.0, 0.0)]
    prev = validated_previous_close(5_801.0, rows, "2026-10-27")
    assert prev == 5_801.0


def test_unavailable_history_returns_unknown():
    # Round-2 fix: with no daily bars to cross-check against, the fast_info
    # number is exactly the unverified tail that once read −16.61% — the
    # previous close and the change come back unknown, never unconfirmed.
    assert validated_previous_close(340.28, None, "2026-10-27") is None


def _history_frame(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _, _ in rows])
    closes = [c for _, c, _ in rows]
    volumes = [v for _, _, v in rows]
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes,
         "Volume": volumes},
        index=idx,
    )


def _stub_ticker(rows_1mo: list[tuple[str, float, float]] | None,
                 fast_info: dict[str, object],
                 regular_market_time: int | None = 1793088499) -> object:
    # 1793088499 = 2026-10-27 16:08:19 HKT, the exported quote's print time.
    ticker = create_autospec(yf.Ticker, instance=True)
    ticker.fast_info = fast_info
    ticker.history_metadata = (
        {"regularMarketTime": regular_market_time} if regular_market_time is not None else {}
    )
    frame = _history_frame(rows_1mo) if rows_1mo is not None else None
    ticker.history.side_effect = (
        lambda *a, **kw: frame
        if frame is not None and kw.get("period") == "1mo"
        else pd.DataFrame()
    )
    return ticker


def test_fetch_single_snapshot_reports_unknown_on_frozen_conflict():
    # 2026-10-27 16:08 HKT print (1780099700 ≈ 08:08 UTC) at 12.90 while the
    # stale previous close still says 15.47 → the −16.61% must not be shown.
    ticker = _stub_ticker(
        _frozen_hk_rows(),
        fast_info={
            "lastPrice": 12.90,
            "regularMarketPreviousClose": 15.47,
            "open": 13.40,
            "dayHigh": 13.55,
            "dayLow": 12.80,
            "lastVolume": 30_000_000,
        },
    )
    from src.dalton import extended_hours
    from src.data_client.yfinance import data_source as yf_ds

    with (
        patch(
            "src.data_client.yfinance.data_source.yahoo_ticker",
            new=create_autospec(yf_ds.yahoo_ticker, return_value=ticker),
        ),
        patch(
            "src.dalton.extended_hours.enrich_yfinance_snapshot_extended_hours",
            new=create_autospec(
                extended_hours.enrich_yfinance_snapshot_extended_hours,
                side_effect=lambda snap, sym, tk: snap,
            ),
        ),
    ):
        snap = _fetch_single_snapshot("2525.HK")
    assert snap is not None
    assert snap["price"] == 12.90
    assert snap["previous_close"] is None
    assert snap["change"] is None
    assert snap["change_percent"] is None


def test_fetch_single_snapshot_normal_day_unchanged():
    # One valid session before the quote, fast_info agrees → same numbers the
    # old code produced.
    from src.dalton import extended_hours
    from src.data_client.yfinance import data_source as yf_ds

    ticker = _stub_ticker(
        [("2026-10-26", 13.57, 48_924_388), ("2026-10-27", 12.90, 30_000_000)],
        fast_info={
            "lastPrice": 12.90,
            "regularMarketPreviousClose": 13.57,
            "open": 13.40,
            "dayHigh": 13.55,
            "dayLow": 12.80,
            "lastVolume": 30_000_000,
        },
    )
    with (
        patch(
            "src.data_client.yfinance.data_source.yahoo_ticker",
            new=create_autospec(yf_ds.yahoo_ticker, return_value=ticker),
        ),
        patch(
            "src.dalton.extended_hours.enrich_yfinance_snapshot_extended_hours",
            new=create_autospec(
                extended_hours.enrich_yfinance_snapshot_extended_hours,
                side_effect=lambda snap, sym, tk: snap,
            ),
        ),
    ):
        snap = _fetch_single_snapshot("2525.HK")
    assert snap is not None
    assert snap["previous_close"] == 13.57
    assert snap["change"] == -0.67
    assert snap["change_percent"] == round((12.90 - 13.57) / 13.57 * 100, 4)


def test_fetch_single_snapshot_unavailable_history_reports_unknown():
    # Round-2 fix: the 1mo history fetch returns nothing (timeout/empty), so
    # the fast_info previous close cannot be confirmed — the snapshot must
    # not publish it or a change percent built on it.
    from src.dalton import extended_hours
    from src.data_client.yfinance import data_source as yf_ds

    ticker = _stub_ticker(
        None,
        fast_info={
            "lastPrice": 12.90,
            "regularMarketPreviousClose": 15.47,
            "open": 13.40,
            "dayHigh": 13.55,
            "dayLow": 12.80,
            "lastVolume": 30_000_000,
        },
    )
    with (
        patch(
            "src.data_client.yfinance.data_source.yahoo_ticker",
            new=create_autospec(yf_ds.yahoo_ticker, return_value=ticker),
        ),
        patch(
            "src.dalton.extended_hours.enrich_yfinance_snapshot_extended_hours",
            new=create_autospec(
                extended_hours.enrich_yfinance_snapshot_extended_hours,
                side_effect=lambda snap, sym, tk: snap,
            ),
        ),
    ):
        snap = _fetch_single_snapshot("2525.HK")
    assert snap is not None
    assert snap["price"] == 12.90
    assert snap["previous_close"] is None
    assert snap["change"] is None
    assert snap["change_percent"] is None


def test_snapshot_from_history_skips_frozen_previous_row():
    # The KeyError fallback path builds its snapshot from 5d bars; the frozen
    # row must not become the previous close, and a missing previous close
    # must not be filled in as a 0% day.
    from src.dalton import extended_hours

    ticker = create_autospec(yf.Ticker, instance=True)
    rows = [
        ("2026-10-20", 15.47, 0.0),
        ("2026-10-23", 15.47, 0.0),
        ("2026-10-26", 13.57, 48_924_388),
    ]
    ticker.history.return_value = _history_frame(rows)
    with patch(
        "src.dalton.extended_hours.enrich_yfinance_snapshot_extended_hours",
        new=create_autospec(
            extended_hours.enrich_yfinance_snapshot_extended_hours,
            side_effect=lambda snap, sym, tk: snap,
        ),
    ):
        snap = _snapshot_from_history("2525.HK", ticker)
    assert snap is not None
    assert snap["price"] == 13.57
    assert snap["previous_close"] is None
    assert snap["change"] is None
    assert snap["change_percent"] is None
    assert datetime.fromtimestamp(snap["as_of"] / 1000, tz=timezone.utc) is not None
