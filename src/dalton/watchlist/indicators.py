"""Self-contained split-adjusted market & technical indicators for personal watchlist (§6.1).

Zero dependency on external skill script directories. All calculations operate
on chronological split-adjusted daily bars (oldest -> newest) in the symbol's
exchange timezone.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

_EXCHANGE_TZ_BY_SUFFIX: tuple[tuple[str, str], ...] = (
    (".HK", "Asia/Hong_Kong"),
    (".SS", "Asia/Shanghai"),
    (".SZ", "Asia/Shanghai"),
    (".SH", "Asia/Shanghai"),
    (".BJ", "Asia/Shanghai"),
    (".T", "Asia/Tokyo"),
    (".JP", "Asia/Tokyo"),
    (".L", "Europe/London"),
)
_DEFAULT_EXCHANGE_TZ = "America/New_York"


_EXCHANGE_MIC_BY_SUFFIX: tuple[tuple[str, str], ...] = (
    (".HK", "XHKG"),
    (".SS", "XSHG"),
    (".SH", "XSHG"),
    (".SZ", "XSHE"),
    (".BJ", "XSHG"),
    (".T", "XTKS"),
    (".JP", "XTKS"),
    (".L", "XLON"),
)


def _is_exchange_trading_day(d: date, symbol: str | None) -> bool:
    """Use ``market_protocol.calendars`` for real holiday awareness, falling back to weekdays."""
    sym = str(symbol or "").strip().upper()
    mic = "XNYS"
    for suffix, mapped_mic in _EXCHANGE_MIC_BY_SUFFIX:
        if sym.endswith(suffix):
            mic = mapped_mic
            break
    try:
        from market_protocol.calendars import get_calendar

        cal = get_calendar(mic)
        return bool(cal.is_trading_day(d))
    except Exception:
        return d.weekday() < 5


def exchange_timezone_for_symbol(symbol: str | None) -> ZoneInfo:
    """Resolve the exchange ``ZoneInfo`` for ``symbol`` (defaults to ``America/New_York``)."""
    sym = str(symbol or "").strip().upper()
    for suffix, tz_name in _EXCHANGE_TZ_BY_SUFFIX:
        if sym.endswith(suffix):
            return ZoneInfo(tz_name)
    return ZoneInfo(_DEFAULT_EXCHANGE_TZ)


def _to_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _parse_date_in_tz(raw: Any, tz: ZoneInfo) -> date | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
        ts = float(raw)
        if abs(ts) > 1e11:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).date()
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        # Pure YYYY-MM-DD trade date string: keep exact calendar date without shifting
        if len(text) == 10:
            try:
                return date.fromisoformat(text)
            except ValueError:
                return None
        # Numeric epoch string
        try:
            ts = float(text)
            if math.isfinite(ts):
                if abs(ts) > 1e11:
                    ts /= 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).date()
        except ValueError:
            pass
        # ISO datetime string
        try:
            parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
            return parsed_dt.astimezone(tz).date()
        except ValueError:
            if len(text) >= 10:
                try:
                    return date.fromisoformat(text[:10])
                except ValueError:
                    return None
    return None


def _bar_date(bar: Mapping[str, Any], *, tz: ZoneInfo | None = None) -> date | None:
    eff_tz = tz or ZoneInfo(_DEFAULT_EXCHANGE_TZ)
    raw = bar.get("date")
    if raw is None:
        raw = bar.get("time")
    if raw is None:
        raw = bar.get("timestamp")
    return _parse_date_in_tz(raw, eff_tz)


def _bar_close(bar: Mapping[str, Any]) -> float | None:
    return _to_float(bar.get("close") if "close" in bar else bar.get("c"))


def _bar_high(bar: Mapping[str, Any]) -> float | None:
    return _to_float(bar.get("high") if "high" in bar else bar.get("h"))


def _bar_low(bar: Mapping[str, Any]) -> float | None:
    return _to_float(bar.get("low") if "low" in bar else bar.get("l"))


def _bar_volume(bar: Mapping[str, Any]) -> float | None:
    return _to_float(bar.get("volume") if "volume" in bar else bar.get("v"))


def wilder_rsi(closes: Sequence[float], period: int) -> float | None:
    """Compute Wilder's Relative Strength Index (RSI) over ``closes`` (oldest -> newest).

    Initializes average gain/loss as the simple mean over the first ``period``
    adjacent differences, then smooths via ``(prev * (N - 1) + cur) / N``.
    Returns ``None`` when fewer than ``period + 1`` valid closes are provided.
    """
    if period <= 0:
        return None
    valid = [c for c in closes if isinstance(c, (int, float)) and math.isfinite(float(c))]
    if len(valid) < period + 1:
        return None

    diffs = [valid[i] - valid[i - 1] for i in range(1, len(valid))]
    gains = [d if d > 0 else 0.0 for d in diffs]
    losses = [-d if d < 0 else 0.0 for d in diffs]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(diffs)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _pct_change(current: float | None, base: float | None) -> float | None:
    if current is None or base is None or base <= 0:
        return None
    return round(((current - base) / base) * 100.0, 2)


def compute_symbol_metrics(
    bars: Sequence[Mapping[str, Any]],
    *,
    symbol: str | None = None,
    latest_price: float | None = None,
    day_high: float | None = None,
    day_low: float | None = None,
    prev_close: float | None = None,
    chg_1d_override: float | None = None,
    currency: str | None = None,
    market_cap: float | None = None,
    next_earnings: str | None = None,
    quote_timestamp: Any = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Compute Batch-1 watchlist indicators from split-adjusted daily bars + live snapshot."""
    tz = exchange_timezone_for_symbol(symbol)
    raw_parsed: list[tuple[date | None, float, float | None, float | None, float | None]] = []
    for bar in bars:
        c = _bar_close(bar)
        if c is None or c <= 0:
            continue
        raw_parsed.append((_bar_date(bar, tz=tz), c, _bar_high(bar), _bar_low(bar), _bar_volume(bar)))

    # Deduplicate by trade date (keep latest entry per date) and sort ascending
    if raw_parsed and all(row[0] is not None for row in raw_parsed):
        deduped_by_dt: dict[date, tuple[date | None, float, float | None, float | None, float | None]] = {}
        for row in raw_parsed:
            assert row[0] is not None
            deduped_by_dt[row[0]] = row
        parsed = [deduped_by_dt[d] for d in sorted(deduped_by_dt.keys())]
    else:
        parsed = raw_parsed

    closes = [row[1] for row in parsed]
    price = _to_float(latest_price)
    if price is None and closes:
        price = closes[-1]

    last_bar_date = parsed[-1][0] if parsed else None
    quote_trade_date = _parse_date_in_tz(quote_timestamp, tz)
    eff_now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if eff_now.tzinfo is None:
        eff_now = eff_now.replace(tzinfo=timezone.utc)
    exchange_today = eff_now.astimezone(tz).date()

    # Anchor end-of-series to live price in exchange timezone using real exchange trading calendar
    eff_closes = list(closes)
    terminal_trade_date: date | None = last_bar_date
    if price is not None and eff_closes and last_bar_date is not None:
        if quote_trade_date is not None:
            if quote_trade_date == last_bar_date:
                eff_closes[-1] = price
                terminal_trade_date = quote_trade_date
            elif quote_trade_date > last_bar_date and _is_exchange_trading_day(quote_trade_date, symbol):
                eff_closes.append(price)
                terminal_trade_date = quote_trade_date
        else:
            if exchange_today == last_bar_date:
                eff_closes[-1] = price
                terminal_trade_date = exchange_today
            elif (
                exchange_today > last_bar_date
                and _is_exchange_trading_day(exchange_today, symbol)
                and abs(price - eff_closes[-1]) > 1e-9
            ):
                eff_closes.append(price)
                terminal_trade_date = exchange_today

    def _lookback_base(trading_days: int) -> float | None:
        if len(eff_closes) <= trading_days:
            return None
        return eff_closes[-(trading_days + 1)]

    chg_1d = _to_float(chg_1d_override)
    if chg_1d is None:
        base_1d = _to_float(prev_close) or _lookback_base(1)
        chg_1d = _pct_change(price, base_1d)

    chg_1w = _pct_change(price, _lookback_base(5))
    chg_1m = _pct_change(price, _lookback_base(21))
    chg_3m = _pct_change(price, _lookback_base(63))
    chg_1y = _pct_change(price, _lookback_base(252))

    # YTD: anchored to the last trading day of (ref_year - 1).
    # On holidays like Jan 1 (New Year's Day), exchange_today is already in the new year
    # even though last_bar_date was Dec 31 of the prior year -> YTD must be 0%, not prior year's gain.
    chg_ytd: float | None = None
    if parsed and price is not None:
        if now_utc is not None:
            ref_year = exchange_today.year
        elif (
            terminal_trade_date is not None
            and exchange_today >= terminal_trade_date
            and (exchange_today - terminal_trade_date).days <= 14
        ):
            ref_year = exchange_today.year
        else:
            ref_year = (terminal_trade_date or exchange_today).year
        prior_year_closes = [
            row[1] for row in parsed if row[0] is not None and row[0].year <= (ref_year - 1)
        ]
        if prior_year_closes:
            chg_ytd = _pct_change(price, prior_year_closes[-1])

    # 52W range (last 252 daily bars + intraday high/low)
    recent_252 = parsed[-252:]
    highs = [row[2] for row in recent_252 if row[2] is not None]
    lows = [row[3] for row in recent_252 if row[3] is not None]
    dh = _to_float(day_high)
    dl = _to_float(day_low)
    if dh is not None:
        highs.append(dh)
    if dl is not None:
        lows.append(dl)

    high_52w: float | None = None
    low_52w: float | None = None
    position_52w: float | None = None
    if len(recent_252) >= 2 and highs and lows:
        h52 = max(highs)
        l52 = min(lows)
        high_52w = round(h52, 4)
        low_52w = round(l52, 4)
        if price is not None and h52 > l52:
            raw_pos = ((price - l52) / (h52 - l52)) * 100.0
            position_52w = round(max(0.0, min(100.0, raw_pos)), 1)

    # Volume ratio (5D avg / 20D avg)
    vols = [row[4] for row in parsed if row[4] is not None and row[4] >= 0]
    vol_ratio_5_20: float | None = None
    if len(vols) >= 20:
        avg5 = sum(vols[-5:]) / 5.0
        avg20 = sum(vols[-20:]) / 20.0
        if avg20 > 0:
            vol_ratio_5_20 = round(avg5 / avg20, 2)

    return {
        "price": round(price, 4) if price is not None else None,
        "currency": currency,
        "chg_1d": chg_1d,
        "chg_1w": chg_1w,
        "chg_1m": chg_1m,
        "chg_3m": chg_3m,
        "chg_ytd": chg_ytd,
        "chg_1y": chg_1y,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "position_52w": position_52w,
        "rsi_14d": wilder_rsi(eff_closes, 14),
        "rsi_30d": wilder_rsi(eff_closes, 30),
        "vol_ratio_5_20": vol_ratio_5_20,
        "market_cap": _to_float(market_cap),
        "next_earnings": next_earnings,
    }
