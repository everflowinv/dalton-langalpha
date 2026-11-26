"""Self-contained split-adjusted market & technical indicators for personal watchlist (§6.1).

Zero dependency on external skill script directories. All calculations operate
on chronological split-adjusted daily bars (oldest -> newest).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any


def _to_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _bar_date(bar: Mapping[str, Any]) -> date | None:
    raw = bar.get("date") or bar.get("time") or bar.get("timestamp")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
        ts = float(raw)
        if ts > 1e11:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts).date()
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(raw, str):
        text = raw.strip()
        if len(text) >= 10:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


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
    latest_price: float | None = None,
    day_high: float | None = None,
    day_low: float | None = None,
    prev_close: float | None = None,
    chg_1d_override: float | None = None,
    currency: str | None = None,
    market_cap: float | None = None,
    next_earnings: str | None = None,
) -> dict[str, Any]:
    """Compute Batch-1 watchlist indicators from split-adjusted daily bars + live snapshot."""
    parsed: list[tuple[date | None, float, float | None, float | None, float | None]] = []
    for bar in bars:
        c = _bar_close(bar)
        if c is None or c <= 0:
            continue
        parsed.append((_bar_date(bar), c, _bar_high(bar), _bar_low(bar), _bar_volume(bar)))

    # Sort ascending by date when dates are available
    if parsed and all(row[0] is not None for row in parsed):
        parsed.sort(key=lambda row: row[0] or date.min)

    closes = [row[1] for row in parsed]
    price = _to_float(latest_price)
    if price is None and closes:
        price = closes[-1]

    # Anchor end-of-series to live price when available for return & RSI consistency
    eff_closes = list(closes)
    if price is not None and eff_closes:
        # If snapshot price differs from last historical close, append only if today > last bar date
        last_dt = parsed[-1][0]
        if last_dt is not None and last_dt < date.today():
            eff_closes.append(price)

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

    # YTD: anchored to the last trading day of the prior calendar year
    chg_ytd: float | None = None
    if parsed and price is not None:
        ref_year = (parsed[-1][0] or date.today()).year
        prior_year_closes = [
            row[1] for row in parsed if row[0] is not None and row[0].year < ref_year
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
