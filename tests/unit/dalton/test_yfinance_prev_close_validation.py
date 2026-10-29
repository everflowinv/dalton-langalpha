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
from src.data_client.yfinance.financial_source import (
    YFinanceFinancialSource,
    _get_realtime_quote,
    _get_single_sector_perf,
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


def test_resumption_day_previous_close_is_unknown():
    """On the resumed session itself the previous close is unknown, not 10/12's.

    2525.HK resumed 2026-10-26 after a multi-session suspension. Yahoo filled
    the suspended sessions with flat zero-volume bars; the last bar that
    actually traded (10/12, 15.47) matched fast_info's previous close, so the
    pre-B5 code adopted it and quoted a phoney −12.28% day (the real move was
    ~+8.6% — the exchange's resumption reference was not the pre-suspension
    close). Zero-volume fills between the last traded bar and the quote date
    mean sessions in between were never fetched, so no bar can stand as the
    previous session's close. This assertion changed from `== 15.47` to `is
    None` because the old one locked the wrong behavior, not to fit new code;
    the normal-day and next-day cases above keep their original expectations.
    """
    prev = validated_previous_close(15.47, _frozen_hk_rows(), "2026-10-26")
    assert prev is None


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
                 regular_market_time: int | None = 1793088499,
                 info: dict[str, object] | None = None) -> object:
    # 1793088499 = 2026-10-27 16:08:19 HKT, the exported quote's print time.
    ticker = create_autospec(yf.Ticker, instance=True)
    ticker.fast_info = fast_info
    ticker.info = info if info is not None else {"longName": "Test Corp"}
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


def test_fetch_single_snapshot_resumption_day_reports_unknown():
    # The production symptom (2026-10-26): the quote prints on the resumed
    # session at 13.57 while fast_info's previous close still says the frozen
    # 15.47, which the last traded bar (10/12) confirms — the snapshot must
    # not publish that pair as a −12.28% day (the real move was ~+8.6%).
    # 1793002099 = 2026-10-26 16:08:19 HKT, the resumed session's print time.
    ticker = _stub_ticker(
        _frozen_hk_rows(),
        fast_info={
            "lastPrice": 13.57,
            "regularMarketPreviousClose": 15.47,
            "open": 13.40,
            "dayHigh": 13.55,
            "dayLow": 12.80,
            "lastVolume": 48_924_388,
        },
        regular_market_time=1793002099,
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
    assert snap["price"] == 13.57
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


# ---------------------------------------------------------------------------
# P4a: company overview (_get_realtime_quote) & sector ETF (_get_single_sector_perf)
# ---------------------------------------------------------------------------


def _stale_hk_rows() -> list[tuple[str, float, float]]:
    # Daily bars froze / stopped updating at 2026-10-12 (15.47), while a
    # 2026-10-27 quote carries a newer fast_info previous close (13.57).
    return [
        ("2026-10-08", 15.6, 2_000_000),
        ("2026-10-09", 15.5, 1_800_000),
        ("2026-10-12", 15.47, 2_400_000),
    ]


def test_realtime_quote_reports_unknown_on_2525_hk_frozen_conflict():
    # 2525.HK on 2026-10-27: price 12.90, fast_info still rides the frozen
    # 15.47 while 10/26 closed 13.57 → must return None, never -16.61% or 0.0.
    from src.data_client.yfinance import financial_source as yf_fin

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
    with patch(
        "src.data_client.yfinance.financial_source.yahoo_ticker",
        new=create_autospec(yf_fin.yahoo_ticker, return_value=ticker),
    ):
        rows = _get_realtime_quote("2525.HK")
    assert len(rows) == 1
    q = rows[0]
    assert q["price"] == 12.90
    assert q["previousClose"] is None
    assert q["change"] is None
    assert q["changePercentage"] is None


def test_realtime_quote_resumption_day_reports_unknown():
    # 2525.HK on 2026-10-26 (resumption day itself): zero-volume fills sit
    # between 10/12 and 10/26, so previousClose and changePercentage are None.
    from src.data_client.yfinance import financial_source as yf_fin

    ticker = _stub_ticker(
        _frozen_hk_rows(),
        fast_info={
            "lastPrice": 13.57,
            "regularMarketPreviousClose": 15.47,
            "open": 13.40,
            "dayHigh": 13.55,
            "dayLow": 12.80,
            "lastVolume": 48_924_388,
        },
        regular_market_time=1793002099,
    )
    with patch(
        "src.data_client.yfinance.financial_source.yahoo_ticker",
        new=create_autospec(yf_fin.yahoo_ticker, return_value=ticker),
    ):
        q = _get_realtime_quote("2525.HK")[0]
    assert q["price"] == 13.57
    assert q["previousClose"] is None
    assert q["change"] is None
    assert q["changePercentage"] is None


@pytest.mark.parametrize("fast_prev", [None, 13.57])
def test_realtime_quote_post_resumption_uses_last_real_trading_day(
    fast_prev: float | None,
):
    # After 10/13–10/23 suspension (0-volume fills) and 10/26 resumption
    # (13.57, traded volume), a 10/27 quote takes 10/26 (13.57) as previousClose.
    from src.data_client.yfinance import financial_source as yf_fin

    fast_info: dict[str, object] = {
        "lastPrice": 12.90,
        "open": 13.40,
        "dayHigh": 13.55,
        "dayLow": 12.80,
        "lastVolume": 30_000_000,
    }
    if fast_prev is not None:
        fast_info["regularMarketPreviousClose"] = fast_prev
    ticker = _stub_ticker(_frozen_hk_rows(), fast_info=fast_info)
    with patch(
        "src.data_client.yfinance.financial_source.yahoo_ticker",
        new=create_autospec(yf_fin.yahoo_ticker, return_value=ticker),
    ):
        q = _get_realtime_quote("2525.HK")[0]
    assert q["price"] == 12.90
    assert q["previousClose"] == 13.57
    assert q["change"] == -0.67
    assert q["changePercentage"] == round((12.90 - 13.57) / 13.57 * 100, 4)


def test_realtime_quote_stale_daily_bars_reports_unknown():
    # Stale daily history ending 2026-10-12 (15.47) disagrees with fast_info's
    # 13.57 on 2026-10-27 → previousClose and change come back None.
    from src.data_client.yfinance import financial_source as yf_fin

    ticker = _stub_ticker(
        _stale_hk_rows(),
        fast_info={
            "lastPrice": 12.90,
            "regularMarketPreviousClose": 13.57,
        },
    )
    with patch(
        "src.data_client.yfinance.financial_source.yahoo_ticker",
        new=create_autospec(yf_fin.yahoo_ticker, return_value=ticker),
    ):
        q = _get_realtime_quote("2525.HK")[0]
    assert q["price"] == 12.90
    assert q["previousClose"] is None
    assert q["change"] is None
    assert q["changePercentage"] is None


@pytest.mark.parametrize("fast_prev", [15.47, None])
def test_realtime_quote_empty_history_reports_unknown_not_zero(
    fast_prev: float | None,
):
    # Empty/unavailable daily history: previousClose, change, changePercentage
    # must be None, never coerced to 0.0.
    from src.data_client.yfinance import financial_source as yf_fin

    fast_info: dict[str, object] = {"lastPrice": 12.90}
    if fast_prev is not None:
        fast_info["regularMarketPreviousClose"] = fast_prev
    ticker = _stub_ticker(None, fast_info=fast_info)
    with patch(
        "src.data_client.yfinance.financial_source.yahoo_ticker",
        new=create_autospec(yf_fin.yahoo_ticker, return_value=ticker),
    ):
        q = _get_realtime_quote("2525.HK")[0]
    assert q["price"] == 12.90
    assert q["previousClose"] is None
    assert q["change"] is None
    assert q["changePercentage"] is None


def test_realtime_quote_normal_symbol_unchanged():
    from src.data_client.yfinance import financial_source as yf_fin

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
    with patch(
        "src.data_client.yfinance.financial_source.yahoo_ticker",
        new=create_autospec(yf_fin.yahoo_ticker, return_value=ticker),
    ):
        q = _get_realtime_quote("2525.HK")[0]
    assert q["price"] == 12.90
    assert q["previousClose"] == 13.57
    assert q["change"] == -0.67
    assert q["changePercentage"] == round((12.90 - 13.57) / 13.57 * 100, 4)


@pytest.mark.asyncio
async def test_company_overview_formats_unknown_prev_close_without_zero_or_error():
    # Upper-layer company overview formatting: when previousClose/change are
    # None, _provider_price_line omits the move (never "+0.00%" or "-16.61%"),
    # the Previous Close row is omitted, and the artifact quote carries None.
    from src.data_client.financial_data_provider import FinancialDataProvider
    from src.data_client.market_data_provider import MarketDataProvider
    from src.data_client.yfinance import financial_source as yf_fin
    from src.tools.market_data import company as company_mod

    ticker = _stub_ticker(
        _frozen_hk_rows(),
        fast_info={
            "lastPrice": 12.90,
            "regularMarketPreviousClose": 15.47,
            "open": 13.40,
            "dayHigh": 13.55,
            "dayLow": 12.80,
            "lastVolume": 30_000_000,
            "currency": "HKD",
            "exchange": "HKG",
            "marketCap": 5_000_000_000,
        },
        info={
            "longName": "HesHeng Corp",
            "sector": "Industrials",
            "industry": "Machinery",
        },
    )
    fin_provider = FinancialDataProvider(financial=YFinanceFinancialSource())
    mkt_provider = create_autospec(MarketDataProvider, instance=True)
    mkt_provider.get_snapshots.return_value = []

    with (
        patch(
            "src.data_client.yfinance.financial_source.yahoo_ticker",
            new=create_autospec(yf_fin.yahoo_ticker, return_value=ticker),
        ),
        patch(
            "src.tools.market_data.company.get_financial_data_provider",
            new=create_autospec(
                company_mod.get_financial_data_provider, return_value=fin_provider
            ),
        ),
        patch(
            "src.tools.market_data.company.get_market_data_provider",
            new=create_autospec(
                company_mod.get_market_data_provider, return_value=mkt_provider
            ),
        ),
        patch(
            "src.tools.market_data.company.display_names",
            new=create_autospec(company_mod.display_names, return_value=(None, None)),
        ),
        patch(
            "src.tools.market_data.company._fmp_request",
            new=create_autospec(company_mod._fmp_request, return_value=[]),
        ),
    ):
        content, artifact = await company_mod.fetch_company_overview("2525.HK")
        rest_artifact = await company_mod.fetch_company_overview_data("2525.HK")

    assert "**Price:** HK$12.90" in content
    assert "-16.61%" not in content
    assert "0.00%" not in content
    assert "| Previous Close |" not in content
    for art in (artifact, rest_artifact):
        assert art["quote"]["price"] == 12.90
        assert art["quote"]["previousClose"] is None
        assert art["quote"]["change"] is None
        assert art["quote"]["changePct"] is None


def test_sector_etf_frozen_conflict_and_resumption_day_return_unknown():
    from src.data_client.yfinance import financial_source as yf_fin

    # 10/27 frozen-tail conflict (15.47 vs 13.57)
    conflict_ticker = _stub_ticker(
        _frozen_hk_rows(),
        fast_info={"lastPrice": 12.90, "regularMarketPreviousClose": 15.47},
    )
    with patch(
        "src.data_client.yfinance.financial_source.yahoo_ticker",
        new=create_autospec(yf_fin.yahoo_ticker, return_value=conflict_ticker),
    ):
        assert _get_single_sector_perf("Technology", "XLK") is None

    # 10/26 resumption day itself
    resumption_ticker = _stub_ticker(
        _frozen_hk_rows(),
        fast_info={"lastPrice": 13.57, "regularMarketPreviousClose": 15.47},
        regular_market_time=1793002099,
    )
    with patch(
        "src.data_client.yfinance.financial_source.yahoo_ticker",
        new=create_autospec(yf_fin.yahoo_ticker, return_value=resumption_ticker),
    ):
        assert _get_single_sector_perf("Technology", "XLK") is None


@pytest.mark.parametrize("fast_prev", [None, 13.57])
def test_sector_etf_post_resumption_uses_last_real_trading_day(
    fast_prev: float | None,
):
    from src.data_client.yfinance import financial_source as yf_fin

    fast_info: dict[str, object] = {"lastPrice": 12.90}
    if fast_prev is not None:
        fast_info["regularMarketPreviousClose"] = fast_prev
    ticker = _stub_ticker(_frozen_hk_rows(), fast_info=fast_info)
    with patch(
        "src.data_client.yfinance.financial_source.yahoo_ticker",
        new=create_autospec(yf_fin.yahoo_ticker, return_value=ticker),
    ):
        row = _get_single_sector_perf("Technology", "XLK")
    assert row == {"sector": "Technology", "changePctStr": "-4.94%"}


def test_sector_etf_stale_history_returns_unknown():
    from src.data_client.yfinance import financial_source as yf_fin

    stale_ticker = _stub_ticker(
        _stale_hk_rows(),
        fast_info={"lastPrice": 12.90, "regularMarketPreviousClose": 13.57},
    )
    with patch(
        "src.data_client.yfinance.financial_source.yahoo_ticker",
        new=create_autospec(yf_fin.yahoo_ticker, return_value=stale_ticker),
    ):
        assert _get_single_sector_perf("Technology", "XLK") is None


# Separate from the stale case: in one test the second call hit the first
# one's cached daily history and never read the empty history at all.
def test_sector_etf_empty_history_returns_unknown():
    from src.data_client.yfinance import financial_source as yf_fin

    empty_ticker = _stub_ticker(
        None,
        fast_info={"lastPrice": 203.0, "regularMarketPreviousClose": 200.0},
    )
    with patch(
        "src.data_client.yfinance.financial_source.yahoo_ticker",
        new=create_autospec(yf_fin.yahoo_ticker, return_value=empty_ticker),
    ):
        assert _get_single_sector_perf("Technology", "XLK") is None


def test_sector_etf_normal_day_unchanged():
    from src.data_client.yfinance import financial_source as yf_fin

    ticker = _stub_ticker(
        [("2026-10-26", 200.0, 10_000_000), ("2026-10-27", 203.0, 12_000_000)],
        fast_info={"lastPrice": 203.0, "regularMarketPreviousClose": 200.0},
    )
    with patch(
        "src.data_client.yfinance.financial_source.yahoo_ticker",
        new=create_autospec(yf_fin.yahoo_ticker, return_value=ticker),
    ):
        row = _get_single_sector_perf("Technology", "XLK")
    assert row == {"sector": "Technology", "changePctStr": "+1.50%"}


@pytest.mark.asyncio
async def test_sector_performance_omits_unverified_etf_and_keeps_valid_sectors():
    # Original behavior in YFinanceFinancialSource.get_sector_performance:
    # an ETF whose previous close is unknown returns None and is omitted,
    # while verified ETFs render unchanged without 0% or errors.
    from src.data_client.financial_data_provider import FinancialDataProvider
    from src.data_client.yfinance import financial_source as yf_fin
    from src.tools.market_data import market_overview as mo_mod

    valid_ticker = _stub_ticker(
        [("2026-10-26", 200.0, 10_000_000), ("2026-10-27", 203.0, 12_000_000)],
        fast_info={"lastPrice": 203.0, "regularMarketPreviousClose": 200.0},
    )
    frozen_ticker = _stub_ticker(
        _frozen_hk_rows(),
        fast_info={"lastPrice": 12.90, "regularMarketPreviousClose": 15.47},
    )

    def _pick_ticker(sym: str) -> object:
        return valid_ticker if sym == "XLV" else frozen_ticker

    fin_provider = FinancialDataProvider(financial=YFinanceFinancialSource())
    with (
        patch(
            "src.data_client.yfinance.financial_source.yahoo_ticker",
            new=create_autospec(yf_fin.yahoo_ticker, side_effect=_pick_ticker),
        ),
        patch(
            "src.tools.market_data.market_overview.get_financial_data_provider",
            new=create_autospec(
                mo_mod.get_financial_data_provider, return_value=fin_provider
            ),
        ),
    ):
        content, artifact = await mo_mod.fetch_sector_performance()

    assert "Healthcare" in content
    assert "+1.50%" in content
    assert "Technology" not in content
    assert "-16.61%" not in content
    assert artifact == {
        "type": "sector_performance",
        "sectors": [{"sector": "Healthcare", "changePercentage": 1.5}],
    }

