"""Extended-hours quote enrichment and formatting helpers for Dalton (extended-hours-quotes).

Populates ginlix-data-compatible extended-hours fields on yfinance snapshots
from ``ticker.info`` when our own exchange calendar places the US equity in
pre-market or between the regular close and the next pre-market open (with
strict trade-time validation and graceful fallback to regular-only on any
error), and formats regular vs. extended-hours price/percentage segments in
``quote_format`` so previous-session closes are never mislabeled ``today``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from market_protocol import AssetClass, InstrumentRef, MarketPhase, to_canonical
from market_protocol.calendars import get_calendar
from market_protocol.symbology import UNKNOWN_MIC
from src.data_client.instrument_clock import clock_for, clock_for_ref
from src.data_client.normalize import is_us_class_share

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _finite(val: Any) -> float | None:
    if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
        return float(val)
    return None


def _epoch_sec_to_ms(val: Any) -> int | None:
    if isinstance(val, datetime):
        return int(val.timestamp() * 1000)
    if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val) and val > 0:
        # Yahoo's preMarketTime / postMarketTime / regularMarketTime are Unix seconds.
        return int(val * 1000) if val < 10_000_000_000 else int(val)
    return None


def _regular_date_iso(ts_ms: int | None, sym: str) -> str | None:
    if ts_ms is None or ts_ms <= 0:
        return None
    try:
        ref = to_canonical(sym, asset_class=AssetClass.EQUITY) if sym else None
        clock = clock_for_ref(ref) if ref is not None else clock_for(None)
        return (
            datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
            .astimezone(clock.tz)
            .date()
            .isoformat()
        )
    except Exception:
        return None


def _us_extended_window(
    sym: str, moment: datetime
) -> tuple[str, Any, date, int, int] | None:
    """Return ``(mode, cal, session_date, window_start_ms, window_end_ms)`` for US extended hours.

    *mode* is ``"pre"`` during today's pre-market ``[pre_open_ms, reg_open_ms)``,
    or ``"post"`` from the most recent regular close until the next trading day's
    pre-market open ``[last_reg_close_ms, next_pre_open_ms)`` (covering 16:00–20:00 ET,
    after 20:00 ET, overnight before 04:00 ET, and weekends/holidays).
    Returns ``None`` during ``REGULAR``/``LUNCH`` or for non-US / index symbols.
    """
    if not sym or sym.startswith("^"):
        return None
    ref = to_canonical(sym, asset_class=AssetClass.EQUITY)
    if ref.calendar_id != "XNYS" or ref.asset_class is AssetClass.INDEX:
        return None
    if ref.mic == UNKNOWN_MIC and not is_us_class_share(ref):
        return None
    cal = get_calendar("XNYS")
    phase = cal.phase_at(moment)
    if phase in (MarketPhase.REGULAR, MarketPhase.LUNCH):
        return None

    if phase == MarketPhase.PRE:
        today_date = moment.astimezone(cal.tz).date()
        ext_bounds = cal.extended_bounds_ms(today_date)
        reg_open_ms = cal.session_open_ms(today_date)
        if ext_bounds is None or reg_open_ms is None:
            return None
        return ("pre", cal, today_date, ext_bounds[0], reg_open_ms)

    moment_ms = int(moment.timestamp() * 1000)
    last_session_date = cal.latest_trading_date(moment)
    last_reg_close_ms = cal.session_close_ms(last_session_date)
    if last_reg_close_ms is None or moment_ms < last_reg_close_ms:
        return None
    next_pre_open_ms: int | None = None
    for offset in range(1, 22):
        cand = last_session_date + timedelta(days=offset)
        if cal.is_trading_day(cand):
            ext = cal.extended_bounds_ms(cand)
            next_pre_open_ms = ext[0] if ext is not None else cal.session_open_ms(cand)
            break
    if next_pre_open_ms is None or moment_ms >= next_pre_open_ms:
        return None
    return ("post", cal, last_session_date, last_reg_close_ms, next_pre_open_ms)


def enrich_yfinance_snapshot_extended_hours(
    snap: dict[str, Any],
    sym: str,
    ticker: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Populate ginlix-compatible extended-hours fields on a yfinance snapshot.

    Only fetches ``ticker.info`` when our own ``XNYS`` calendar places *sym* in
    pre-market or between the most recent regular close and the next pre-market
    open. Validates that Yahoo's ``marketState`` and ``preMarketTime`` /
    ``postMarketTime`` match that window. Any error or validation mismatch falls
    back cleanly to the regular snapshot without failing the quote.
    """
    base_snap = dict(snap)
    try:
        price = _finite(base_snap.get("price"))
        if price is None or price <= 0:
            return base_snap

        reg_as_of = _epoch_sec_to_ms(base_snap.get("as_of"))
        reg_trading_date = _regular_date_iso(reg_as_of, sym)
        base_snap["regular_close"] = round(price, 4)
        base_snap["regular_trading_change"] = base_snap.get("change")
        base_snap["regular_trading_change_percent"] = base_snap.get("change_percent")
        base_snap["regular_as_of"] = reg_as_of
        base_snap["regular_trading_date"] = reg_trading_date

        moment = now if now is not None else _now_utc()
        window = _us_extended_window(sym, moment)
        if window is None:
            return base_snap
        mode, cal, session_date, window_start_ms, window_end_ms = window

        info = ticker.info
        if not isinstance(info, Mapping):
            return base_snap

        if reg_as_of is None:
            reg_as_of = _epoch_sec_to_ms(info.get("regularMarketTime"))
            reg_trading_date = _regular_date_iso(reg_as_of, sym)
            base_snap["regular_as_of"] = reg_as_of
            base_snap["regular_trading_date"] = reg_trading_date

        state = str(info.get("marketState") or "").strip().upper()

        if mode == "pre":
            if state != "PRE":
                return base_snap
            pre_price = _finite(info.get("preMarketPrice"))
            if pre_price is None or pre_price <= 0:
                return base_snap
            pre_time_ms = _epoch_sec_to_ms(info.get("preMarketTime"))
            if pre_time_ms is None:
                return base_snap
            prev_session_close_ms = cal.session_close_ms(cal.previous_session(session_date))
            if prev_session_close_ms is not None and pre_time_ms <= prev_session_close_ms:
                return base_snap
            if reg_as_of is not None and pre_time_ms <= reg_as_of:
                return base_snap
            if not (window_start_ms <= pre_time_ms < window_end_ms):
                return base_snap

            # In pre-market, Yahoo's fast_info.lastPrice (price) is the most
            # recent regular close (yesterday's close). Align previous_close,
            # price, and regular_close to that close so early_trading_change is
            # anchored against previous_close identically to ginlix-data.
            settled_close = price
            early_change = pre_price - settled_close
            early_pct = (early_change / settled_close) * 100.0
            enriched = dict(base_snap)
            enriched.update(
                {
                    "price": round(settled_close, 4),
                    "regular_close": round(settled_close, 4),
                    "previous_close": round(settled_close, 4),
                    "last_trade_price": round(pre_price, 4),
                    "change": round(early_change, 4),
                    "change_percent": round(early_pct, 4),
                    "regular_trading_change": base_snap.get("change"),
                    "regular_trading_change_percent": base_snap.get("change_percent"),
                    "early_trading_change": round(early_change, 4),
                    "early_trading_change_percent": round(early_pct, 4),
                    "late_trading_change": None,
                    "late_trading_change_percent": None,
                    "market_status": "early_trading",
                    "as_of": pre_time_ms,
                    "regular_as_of": reg_as_of,
                    "regular_trading_date": reg_trading_date,
                    "regular_only": False,
                }
            )
            return enriched

        if mode == "post":
            if state not in ("POST", "POSTPOST"):
                return base_snap
            post_price = _finite(info.get("postMarketPrice"))
            if post_price is None or post_price <= 0:
                return base_snap
            post_time_ms = _epoch_sec_to_ms(info.get("postMarketTime"))
            if post_time_ms is None:
                return base_snap
            if not (window_start_ms <= post_time_ms < window_end_ms):
                return base_snap
            if reg_as_of is not None and post_time_ms < reg_as_of:
                return base_snap

            settled_close = price
            prev_close = _finite(base_snap.get("previous_close"))
            late_change = post_price - settled_close
            late_pct = (late_change / settled_close) * 100.0
            total_change = (post_price - prev_close) if prev_close else late_change
            total_pct = (total_change / prev_close * 100.0) if prev_close else late_pct
            enriched = dict(base_snap)
            enriched.update(
                {
                    "price": round(settled_close, 4),
                    "regular_close": round(settled_close, 4),
                    "last_trade_price": round(post_price, 4),
                    "change": round(total_change, 4),
                    "change_percent": round(total_pct, 4),
                    "regular_trading_change": base_snap.get("change"),
                    "regular_trading_change_percent": base_snap.get("change_percent"),
                    "early_trading_change": None,
                    "early_trading_change_percent": None,
                    "late_trading_change": round(late_change, 4),
                    "late_trading_change_percent": round(late_pct, 4),
                    "market_status": "late_trading",
                    "as_of": post_time_ms,
                    "regular_as_of": reg_as_of,
                    "regular_trading_date": reg_trading_date,
                    "regular_only": False,
                }
            )
            return enriched

        return base_snap
    except Exception:
        logger.debug("yfinance.extended_hours.enrich_failed | symbol=%s", sym, exc_info=True)
        return base_snap


def _is_us_equity(ref: InstrumentRef | None) -> bool:
    if ref is None:
        return True
    return ref.tz == "America/New_York" and ref.asset_class is not AssetClass.INDEX


def _regular_session_date(
    snap: Mapping[str, Any],
    ref: InstrumentRef | None,
) -> date | None:
    try:
        clock = clock_for_ref(ref) if ref is not None else clock_for(None)
        raw_reg_date = snap.get("regular_trading_date")
        if isinstance(raw_reg_date, str) and raw_reg_date:
            try:
                return date.fromisoformat(raw_reg_date)
            except ValueError:
                pass

        reg_ts = _epoch_sec_to_ms(snap.get("regular_as_of"))
        if reg_ts is not None:
            return (
                datetime.fromtimestamp(reg_ts / 1000.0, tz=timezone.utc)
                .astimezone(clock.tz)
                .date()
            )
    except Exception:
        pass
    return None


def _extended_trade_time_ms(snap: Mapping[str, Any]) -> int | None:
    trade_ts = _epoch_sec_to_ms(snap.get("last_trade_at"))
    if trade_ts is not None:
        return trade_ts
    if snap.get("regular_as_of") is not None and snap.get("regular_only") is False:
        return _epoch_sec_to_ms(snap.get("as_of"))
    return None


def _format_ext_time_local(ts_ms: int | None, ref: InstrumentRef | None) -> str | None:
    if ts_ms is None or ts_ms <= 0:
        return None
    try:
        clock = clock_for_ref(ref) if ref is not None else clock_for(None)
        dt_local = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).astimezone(clock.tz)
        if ref is None or ref.tz == "America/New_York":
            tz_label = "ET"
        else:
            tz_label = dt_local.strftime("%Z") or ref.tz
        return f"{dt_local.strftime('%H:%M')} {tz_label}"
    except Exception:
        return None


def _resolve_anchor_prev_close(
    snap: Mapping[str, Any],
    reg_close: float | None,
    prev_close: float | None,
    *,
    is_pre: bool,
) -> float | None:
    if is_pre:
        reg_change = _finite(snap.get("regular_trading_change"))
        if reg_close is not None and reg_change is not None:
            derived = reg_close - reg_change
            return round(derived, 4) if derived > 0 else None
        return None
    if prev_close is not None and prev_close > 0:
        return prev_close
    return None


def snapshot_quote_time_labels(
    snap: Mapping[str, Any],
    status: str,
    ref: InstrumentRef | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Return ``(reg_date_mmdd, ext_time_str, prev_close_date_mmdd)`` for company overview."""
    try:
        if ref is None and snap.get("symbol"):
            try:
                ref = to_canonical(str(snap["symbol"]), asset_class=AssetClass.EQUITY)
            except Exception:
                ref = None
        is_extended = status in ("early_trading", "late_trading") or (
            status == "closed" and snap.get("late_trading_change") is not None
        )
        reg_date = _regular_session_date(snap, ref)
        reg_date_mmdd = reg_date.strftime("%m-%d") if reg_date is not None else None
        ext_time_str = (
            _format_ext_time_local(_extended_trade_time_ms(snap), ref)
            if is_extended
            else None
        )
        prev_close_date_mmdd: str | None = None
        if reg_date is not None:
            reg_close = _finite(snap.get("regular_close"))
            if reg_close is None:
                reg_close = _finite(snap.get("price"))
            prev_close = _finite(snap.get("previous_close"))
            if (
                status == "early_trading"
                and reg_close is not None
                and prev_close is not None
                and abs(prev_close - reg_close) < 1e-6
            ):
                prev_close_date_mmdd = reg_date_mmdd
            else:
                cal_id = ref.calendar_id if ref is not None and ref.calendar_id else "XNYS"
                cal = get_calendar(cal_id)
                prev_session = cal.previous_session(reg_date)
                if prev_session is not None:
                    prev_close_date_mmdd = prev_session.strftime("%m-%d")
        return reg_date_mmdd, ext_time_str, prev_close_date_mmdd
    except Exception:
        return None, None, None


def _regular_move_basis(
    snap: Mapping[str, Any],
    ref: InstrumentRef | None,
    at: datetime | None,
    phase: MarketPhase | None,
    *,
    has_ext: bool,
    is_pre: bool = False,
) -> str:
    """Return ``'today'``, ``'prev close'``, or ``'last close'`` for the regular session move."""
    try:
        clock = clock_for_ref(ref) if ref is not None else clock_for(None)
        effective_now = at or _now_utc()
        venue_today = effective_now.astimezone(clock.tz).date()
        reg_date = _regular_session_date(snap, ref)
        if reg_date is not None:
            if reg_date == venue_today:
                return "today"
            return "prev close" if is_pre else "last close"
        as_of_ts = _epoch_sec_to_ms(snap.get("as_of"))
        if as_of_ts is not None:
            as_of_date = (
                datetime.fromtimestamp(as_of_ts / 1000.0, tz=timezone.utc)
                .astimezone(clock.tz)
                .date()
            )
            if as_of_date != venue_today:
                return "prev close" if is_pre else "last close"
            if (
                snap.get("regular_as_of") is None
                and not snap.get("regular_trading_date")
                and not _is_us_equity(ref)
                and not has_ext
            ):
                return "today"
    except Exception:
        pass

    if "regular_as_of" in snap or "regular_trading_date" in snap:
        return "last close"
    if (
        snap.get("market_status") == "open"
        or (not has_ext and phase == MarketPhase.REGULAR)
        or (snap.get("market_status") == "late_trading" and phase == MarketPhase.POST)
    ):
        return "today"
    return "last close"


def format_quote_change_parts(
    snap: Mapping[str, Any],
    ref: InstrumentRef | None,
    at: datetime | None,
    phase: MarketPhase | None,
    *,
    fallback_price: float | None = None,
    fmt_price: Callable[[float, InstrumentRef | None], str] | None = None,
) -> tuple[str | float | None, list[str]]:
    """Return ``(main_price_or_segment, change_and_ext_parts)`` for a quote line.

    For US equities off-hours, labels the regular close with ``close MM-DD`` or
    ``last close MM-DD``, anchors the regular move with ``vs prev close $X``, and
    labels the extended-hours segment with ``vs close`` / ``vs last close`` and
    ``@ HH:MM ET``. During regular hours or for non-US listings, preserves the
    existing layout and appends ``vs prev close $X`` when available.
    """
    price_fn = fmt_price or (lambda p, _ref: f"${p:,.2f}")
    reg_close = _finite(snap.get("regular_close"))
    if reg_close is None:
        reg_close = _finite(snap.get("price"))
    prev_close = _finite(snap.get("previous_close"))
    ext_price = _finite(snap.get("last_trade_price"))
    reg_pct = _finite(snap.get("regular_trading_change_percent"))
    total_pct = _finite(snap.get("change_percent"))
    early_pct = _finite(snap.get("early_trading_change_percent"))
    late_pct = _finite(snap.get("late_trading_change_percent"))
    status = snap.get("market_status")
    us_equity = _is_us_equity(ref)

    is_pre = us_equity and (
        status == "early_trading"
        or (status not in ("open", "late_trading", "closed") and phase == MarketPhase.PRE)
        or (
            early_pct is not None
            and late_pct is None
            and phase != MarketPhase.REGULAR
            and status not in ("open", "late_trading", "closed")
        )
    )
    is_post = (
        us_equity
        and not is_pre
        and (
            status in ("late_trading", "closed")
            or (status != "open" and phase in (MarketPhase.POST, MarketPhase.CLOSED))
            or (late_pct is not None and phase != MarketPhase.REGULAR and status != "open")
        )
    )

    has_ext = (
        reg_close is not None
        and ext_price is not None
        and fmt_price is not None
        and ((is_pre and early_pct is not None) or (is_post and late_pct is not None))
    )
    basis = _regular_move_basis(
        snap, ref, at, phase, has_ext=has_ext, is_pre=is_pre
    )
    reg_date = _regular_session_date(snap, ref)
    close_word = "close" if (not is_pre and basis == "today") else "last close"
    close_label = (
        f"{close_word} {reg_date.strftime('%m-%d')}" if reg_date is not None else close_word
    )

    ext_part: str | None = None
    if has_ext and ext_price is not None:
        ext_time_str = _format_ext_time_local(_extended_trade_time_ms(snap), ref)
        time_suffix = f" @ {ext_time_str}" if ext_time_str else ""
        if is_pre and early_pct is not None:
            ext_part = (
                f"pre-market {price_fn(ext_price, ref)} "
                f"{'+' if early_pct >= 0 else ''}{early_pct:.2f}% "
                f"vs {close_word}{time_suffix}"
            )
        elif is_post and late_pct is not None:
            ext_part = (
                f"after-hours {price_fn(ext_price, ref)} "
                f"{'+' if late_pct >= 0 else ''}{late_pct:.2f}% "
                f"vs {close_word}{time_suffix}"
            )

    if ext_part is not None and reg_pct is None and not is_pre:
        if reg_close is not None and prev_close is not None and prev_close > 0:
            reg_pct = ((reg_close - prev_close) / prev_close) * 100.0

    pct = (
        reg_pct
        if ext_part is not None
        else (total_pct if total_pct is not None else reg_pct)
    )
    anchor_prev = _resolve_anchor_prev_close(snap, reg_close, prev_close, is_pre=is_pre)

    if us_equity and (is_pre or is_post):
        close_price = reg_close if reg_close is not None else fallback_price
        reg_tokens: list[str] = []
        if close_price is not None:
            reg_tokens.append(f"{close_label} {price_fn(close_price, ref)}")
        if pct is not None:
            vs_part = (
                f"vs prev close {price_fn(anchor_prev, ref)}"
                if anchor_prev is not None
                else "vs prev close"
            )
            reg_tokens.append(f"{'+' if pct >= 0 else ''}{pct:.2f}% {vs_part}")
        main_seg = " ".join(reg_tokens) if reg_tokens else None
        out = [ext_part] if ext_part is not None else []
        return main_seg, out

    if us_equity:
        live_price = fallback_price if fallback_price is not None else reg_close
        reg_tokens = []
        if live_price is not None:
            reg_tokens.append(price_fn(live_price, ref))
        if pct is not None:
            pct_str = f"{'+' if pct >= 0 else ''}{pct:.2f}% {basis}"
            if anchor_prev is not None:
                pct_str += f" vs prev close {price_fn(anchor_prev, ref)}"
            reg_tokens.append(pct_str)
        main_seg = " ".join(reg_tokens) if reg_tokens else None
        return main_seg, []

    main_price = reg_close if ext_part is not None else fallback_price
    out = []
    if pct is not None:
        pct_str = f"{'+' if pct >= 0 else ''}{pct:.2f}% {basis}"
        if anchor_prev is not None:
            pct_str += f" vs prev close {price_fn(anchor_prev, ref)}"
        out.append(pct_str)
    if ext_part is not None:
        out.append(ext_part)
    return main_price, out
