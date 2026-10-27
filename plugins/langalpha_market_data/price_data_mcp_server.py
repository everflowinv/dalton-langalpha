#!/usr/bin/env python3
"""Price Data MCP Server.

Provides normalized OHLCV time series data and short sale analytics via MCP.

Design goals:
- Small, stable tool surface (high PTC value)
- Standard agent-facing envelope (see mcp_servers/AGENT_CONTRACT.md)
- Can run in sandbox (stdio) for OSS/dev
- Can be deployed externally (http/sse) for production

Tools:
- get_stock_data: stock OHLCV
- get_asset_data: stock/commodity/crypto/forex OHLCV
- get_short_data: short interest (bi-monthly) and short volume (daily)

Symbols are resolved at the boundary through market_protocol (canonical
identity, currency, timezone); prices are returned in major currency units
(GBX/pence venues converted to pounds).
"""

# NOTE: Tool docstrings in this file are hand-tuned agent prompt surface (parsed
# into agent prompts and generated sandbox wrappers) and are content-pinned by
# tests/unit/mcp_servers/test_agent_contract.py. Read mcp_servers/AGENT_CONTRACT.md
# before editing; intentional changes must regenerate agent_docstring_lock.json.

from __future__ import annotations

try:
    from _bootstrap import MCPServer  # script launch: mcp_servers/ is sys.path[0]
except ModuleNotFoundError:  # imported as a package module (tests)
    from mcp_servers._bootstrap import MCPServer

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone as _tz
from typing import Any, Literal, Optional

import httpx

from data_client.fmp import close_fmp_client, get_fmp_client
from data_client.lifespan import closing_lifespan
from data_client.ginlix_data import (
    DAILY_INTERVALS,
    close_ginlix_mcp_client,
    get_ginlix_mcp_client,
)
from data_client.market_data_provider import symbol_market
from data_client.normalize import minor_unit_scale, normalize_bars, scale_price, symbol_timezone  # DALTON-HOOK(market-data-fallback)
from data_client.ginlix_data.cn_source import GinlixDataCnSource
from data_client.ginlix_data.directory import NameDirectory
from market_protocol import is_family_index, to_canonical, to_display, to_legacy_api  # DALTON-HOOK(stock-move-upgrade)
from market_protocol.enums import AssetClass

try:
    from _envelope import (
        error_from_exception,
        error_from_upstream,
        make_error,
        make_response,
        normalize_interval,
    )
    from _yf_common import boundary as _yf_boundary
except ModuleNotFoundError:  # imported as a package module (tests)
    from mcp_servers._envelope import (
        error_from_exception,
        error_from_upstream,
        make_error,
        make_response,
        normalize_interval,
    )
    from mcp_servers._yf_common import boundary as _yf_boundary

from mcp_servers._schemas import (
    OBJECT,
    RECORDS,
    RECORDS_BY_KEY,
    STR,
    described,
    envelope_schema,
    output_model,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


_ginlix = get_ginlix_mcp_client()

# Intervals served by each family. Stock/index route through the ginlix-data
# (CN daily, US) + yfinance + FMP (global) fetch clients, which cover intraday
# + daily only.
# DALTON-HOOK(market-data-fallback): yfinance sits ahead of FMP (Dalton's
# market_data.providers order; see the _yf import block).
_STOCK_INTRADAY = {"1min", "5min", "15min", "30min", "1hour", "4hour"}
_STOCK_INTERVALS = _STOCK_INTRADAY | {"1day"}
# FMP commodity/crypto/forex intraday coverage is narrower.
_ASSET_INTRADAY = {"1min", "5min", "1hour"}
_ASSET_INTERVALS = _ASSET_INTRADAY | {"1day"}

#: Calendar days of daily bars an undated CN request gets. The CN source would
#: fill two years, the window its live cache entry covers; an agent's undated
#: question is about the recent tape, and FMP's own undated window keeps the
#: other listings about as short.
_CN_DEFAULT_DAILY_DAYS = 90


_lifespan = closing_lifespan(close_ginlix_mcp_client, close_fmp_client)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve(
    symbol: str, asset_class: Optional[AssetClass] = None
) -> tuple[str, str, Optional[str], Optional[str]]:
    """Resolve a caller symbol via the protocol registry.

    Returns ``(display, legacy, currency, timezone)`` — the canonical display
    spelling, the legacy provider spelling, ISO-4217 price currency, and IANA
    timezone. Falls back to the raw symbol with unknown currency/tz.
    """
    try:
        ref = to_canonical(symbol, asset_class=asset_class)
        legacy = (  # DALTON-HOOK(stock-move-upgrade)
            f"^{to_legacy_api(ref)}" if is_family_index(ref) else to_legacy_api(ref)
        )
        return to_display(ref), legacy, ref.price_currency, ref.tz
    except Exception:  # noqa: BLE001
        return symbol, symbol, None, None


def _resolve_interval(
    interval: str, served: set[str], *, symbol: str
) -> tuple[Optional[str], Optional[dict]]:
    """Normalize *interval* to canonical vocab, constrained to *served*.

    Returns ``(canonical, None)`` on success or ``(None, error_envelope)`` for
    unknown spellings and vocab this tool does not serve.
    """
    canonical = normalize_interval(interval)
    if canonical is None or canonical not in served:
        return None, make_error(
            "unsupported_interval",
            f"Interval {interval!r} is not supported by this tool.",
            symbol=symbol,
            interval=interval,
            supported=sorted(served),
        )
    return canonical, None


def _display_rows(
    rows: list[dict], symbol: str, *, intraday: bool, scale: float = 1.0
) -> list[dict]:
    """Raw provider rows → ascending ``{date, open, high, low, close, volume}``.

    Timestamps are localized to *symbol*'s exchange timezone; price fields are
    scaled to major units (``scale`` = 0.01 for GBX/pence venues). Volume is a
    share count and never scaled.
    """
    normalized = normalize_bars(rows, symbol, intraday=intraday)  # descending
    normalized.reverse()  # ascending, oldest-first (contract)
    if scale != 1.0:
        for row in normalized:
            for field in ("open", "high", "low", "close"):
                row[field] = scale_price(row[field], scale)
    return normalized


_names = NameDirectory(_ginlix.get_instruments_v2, deadline=2.0)
_cn = GinlixDataCnSource(_ginlix)


def _http_miss(exc: httpx.HTTPError) -> str:
    """The error class, and the status when there was one; never the URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{type(exc).__name__} {exc.response.status_code}"
    return type(exc).__name__


# DALTON-HOOK(market-data-fallback): Dalton's provider order (config.dalton.yaml
# market_data.providers) answers free yfinance ahead of FMP wherever both can
# serve, so FMP's ~250 calls/day free quota survives for what only FMP has. The
# sandbox ships MCP server processes neither config.yaml nor the config loader
# (only data_client + market_protocol, see _SANDBOX_INTERNAL_PACKAGES), and
# mcp.json passes no APP_ENV, so the order cannot be read from the deployed
# overlay — it is pinned here to match it.
try:
    import yfinance as _yf
except ImportError:  # an image without yfinance keeps the FMP chain
    _yf = None  # type: ignore[assignment]

_YF_INTERVALS: dict[str, str] = {
    "1min": "1m",
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "1hour": "1h",
    "1day": "1d",
}
#: Calendar days of history an undated request gets, keyed by yfinance
#: interval — inside Yahoo's caps (1m: 8d, sub-hour: 60d, 1h: 730d) and as
#: deep as the daily default the rest of the chain serves.
_YF_LOOKBACK_DAYS: dict[str, int] = {"1m": 6, "5m": 57, "15m": 57, "30m": 57, "1h": 727, "1d": 730}


def _yf_exclusive_end(end: str) -> str:
    """Advance an inclusive end date to yfinance's exclusive ``end`` bound."""
    try:
        return ((datetime.strptime(end.strip()[:10], "%Y-%m-%d") + timedelta(days=1)).date()).isoformat()
    except ValueError:
        return end


# DALTON-HOOK(market-data-fallback): interpret intraday start/end strings with
# HH:MM[:SS] in the instrument's exchange timezone and filter returned bars to
# the requested window.
def _yf_parse_exchange_bound_ms(
    bound: Optional[str], display: str, *, is_end: bool
) -> Optional[int]:
    if not bound:
        return None
    s = bound.strip().replace("T", " ")
    if not s:
        return None
    tz = symbol_timezone(display)
    dt: Optional[datetime] = None
    if len(s) == 10:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
            if is_end:
                dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        except ValueError:
            return None
    else:
        for fmt, pad_end_seconds in (("%Y-%m-%d %H:%M:%S", False), ("%Y-%m-%d %H:%M", True)):
            try:
                dt = datetime.strptime(s, fmt)
                if is_end and pad_end_seconds:
                    dt = dt.replace(second=59, microsecond=999999)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    return int(dt.replace(tzinfo=tz).timestamp() * 1000)


def _filter_display_rows_to_window(
    rows: list[dict],
    start_date: Optional[str],
    end_date: Optional[str],
    *,
    intraday: bool,
) -> list[dict]:
    if not rows or (not start_date and not end_date):
        return rows
    start_bound: Optional[str] = None
    end_bound: Optional[str] = None
    if start_date:
        s = start_date.strip().replace("T", " ")
        if intraday:
            start_bound = f"{s} 00:00:00" if len(s) == 10 else (f"{s}:00" if len(s) == 16 else s[:19])
        else:
            start_bound = s[:10]
    if end_date:
        e = end_date.strip().replace("T", " ")
        if intraday:
            end_bound = f"{e} 23:59:59" if len(e) == 10 else (f"{e}:59" if len(e) == 16 else e[:19])
        else:
            end_bound = e[:10]
    return [
        r
        for r in rows
        if (not start_bound or r["date"] >= start_bound)
        and (not end_bound or r["date"] <= end_bound)
    ]


def _yf_expected_session_day(display: str, start_day: date) -> Optional[date]:
    """The session an intraday start belongs to: its own day when that trades,
    else the next session (a weekend/holiday start gets the next open)."""
    try:
        from market_protocol.calendars import get_calendar

        cal = get_calendar(to_canonical(display).calendar_id)
    except Exception:  # noqa: BLE001 — unknown venue, caller falls back to a grace span
        return None
    day = start_day
    for _ in range(15):  # enough to bridge the longest venue holiday closure
        if cal.is_trading_day(day):
            return day
        day = day + timedelta(days=1)
    return None


# DALTON-HOOK(market-data-fallback): the first bar need not carry the requested
# start itself — a weekend start gets Monday's daily bar, a 13:01 intraday
# start the 13:05 bar (or the next session's open when 13:01 sits in a break).
# Only a real gap counts as uncovered (a capped 1m history serving the last
# 7 days of a two-month request); the old timestamp comparison read every
# off-hours start as a miss and burned FMP quota re-asking a served window.
def _yf_start_deadline_ms(display: str, start_date: str, interval: str) -> Optional[int]:
    """The latest first bar that still covers an intraday *start_date*.

    The start itself anchors it when it sits inside trading hours; otherwise
    the next session open does — the reopen when the start sits inside a
    venue's midday break, that session's open when it is earlier, the next
    session's open when it is past the close or on a non-trading day. One bar
    interval of slack rides on top (the 13:05 bar covers a 13:01 5min start).
    """
    start_ms = _yf_parse_exchange_bound_ms(start_date, display, is_end=False)
    if start_ms is None:
        return None
    try:
        from market_protocol.calendars import get_calendar, session_bounds

        ref = to_canonical(display)
        cal = get_calendar(ref.calendar_id)
    except Exception:  # noqa: BLE001 — no calendar: caller keeps day granularity
        return None
    day = datetime.fromtimestamp(start_ms / 1000, cal.tz).date()
    for _ in range(15):  # enough to bridge the longest venue holiday closure
        if cal.is_trading_day(day):
            bounds = session_bounds(ref.calendar_id, day.isoformat())
            if bounds and bounds[0] is not None:
                open_ms, close_ms, brk_start, brk_end = bounds
                if start_ms <= open_ms:
                    anchor = open_ms
                elif brk_start is not None and brk_end is not None and brk_start <= start_ms < brk_end:
                    anchor = brk_end
                elif start_ms < close_ms:
                    anchor = start_ms
                else:
                    anchor = None  # past this session's close: the next session's open
                if anchor is not None:
                    return anchor + _INTRADAY_SPAN_SECONDS.get(interval, 0) * 1000
        day = day + timedelta(days=1)
    return None


def _yf_covers_start(
    rows: list[dict],
    start_date: Optional[str],
    *,
    intraday: bool,
    display: str = "",
    interval: str = "",
) -> bool:
    if not start_date or not rows:
        return True
    s = start_date.strip().replace("T", " ")
    first_date = str(rows[0].get("date") or "").strip()
    if not first_date:
        return True
    try:
        start_day = date.fromisoformat(s[:10])
        first_day = date.fromisoformat(first_date[:10])
    except ValueError:
        return True
    if intraday:
        # DALTON-HOOK(market-data-fallback): within the start's own session
        # the bar clock matters too: a first bar at 14:00 does not cover a
        # 09:30 request even though the dates match. The deadline — the start
        # anchored to the next open when it sits off-hours (past the close,
        # a weekend, a holiday), plus one interval — subsumes the day check,
        # so it decides first; the day-level check alone cannot see that an
        # off-hours start belongs to the *next* session's first bar. Only
        # when the deadline cannot be computed (no venue calendar,
        # unparseable timestamps) does the day-granular verdict stand.
        deadline_ms = _yf_start_deadline_ms(display, s, interval)
        first_ms = _yf_parse_exchange_bound_ms(first_date, display, is_end=False)
        if deadline_ms is not None and first_ms is not None:
            return first_ms <= deadline_ms
        expected = _yf_expected_session_day(display, start_day)
        if expected is not None and first_day > expected:
            return False
    return (first_day - start_day).days <= 4


_INTRADAY_SPAN_SECONDS: dict[str, int] = {
    "1min": 60, "5min": 300, "15min": 900, "30min": 1800, "1hour": 3600,
}


def _drop_midday_break_bars(
    rows: list[dict], display: str, canonical: str
) -> list[dict]:
    """Mirror the main chain's midday-break placement (yfinance
    ``data_source._place_in_sessions``): a bar wholly inside a venue's break is
    Yahoo's zero-volume padding and is dropped; one that starts inside the
    break and ends past the reopen carries the reopen's trades and is
    re-anchored there. Venues without a break have no midday window and keep
    every bar. Built on ``market_protocol`` alone (the sandbox ships no ``src.``
    packages), so both legs of the chain drop the same bars.
    """
    try:
        from market_protocol.calendars import get_calendar, session_bounds

        ref = to_canonical(display)
        cal = get_calendar(ref.calendar_id)
    except Exception:  # noqa: BLE001 — no calendar, no break to filter
        return rows
    span_ms = _INTRADAY_SPAN_SECONDS.get(canonical, 0) * 1000
    breaks: dict[date, Optional[tuple[int, int]]] = {}
    placed: list[dict] = []
    for bar in rows:
        day = datetime.fromtimestamp(bar["time"] / 1000, cal.tz).date()
        if day not in breaks:
            bounds = session_bounds(ref.calendar_id, day.isoformat())
            breaks[day] = (bounds[2], bounds[3]) if bounds and bounds[2] is not None else None
        brk = breaks[day]
        if brk is None or not brk[0] <= bar["time"] < brk[1]:
            placed.append(bar)
        elif bar["time"] + span_ms > brk[1]:
            placed.append({**bar, "time": brk[1]})
    return placed


def _yf_fetch_history(
    display: str,
    canonical: str,
    start_date: Optional[str],
    end_date: Optional[str],
    *,
    extended_hours: bool = False,
) -> list[dict]:
    """Synchronous yfinance history → epoch-ms OHLCV rows (`normalize_bars` shape).

    Raises nothing the caller can't treat as a soft miss; an empty history
    returns ``[]`` so the chain falls through to FMP.
    """
    yf_symbol = _yf_boundary(display)[1]
    kwargs: dict[str, Any] = {
        "interval": _YF_INTERVALS[canonical],
        # Split-adjusted only, the whole chain's declared price treatment —
        # conventions never blend across a provider failover.
        "auto_adjust": False,
    }
    if extended_hours and canonical != "1day":
        kwargs["prepost"] = True
    if start_date:
        kwargs["start"] = start_date.strip()[:10]
    if end_date:
        kwargs["end"] = _yf_exclusive_end(end_date)
    if not start_date and not end_date:
        lookback = timedelta(days=_YF_LOOKBACK_DAYS.get(_YF_INTERVALS[canonical], 730))
        kwargs["start"] = (datetime.now(_tz.utc) - lookback).strftime("%Y-%m-%d")

    df = _yf.Ticker(yf_symbol).history(**kwargs)
    if df is None or df.empty:
        return []
    # A NaN-priced row (halted session, gap padding) must not propagate NaN
    # into the response — drop it here.
    df = df.copy().dropna(subset=["Open", "High", "Low", "Close"])
    rows: list[dict] = []
    for idx, row in df.iterrows():
        try:
            ts = int(idx.timestamp() * 1000)
        except (AttributeError, OSError, OverflowError, ValueError):
            continue
        volume = row["Volume"]
        rows.append(
            {
                "time": ts,
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                # NaN != NaN: index/sparse rows carry NaN volume.
                "volume": int(volume) if volume == volume else 0,
            }
        )
    # DALTON-HOOK(market-data-fallback): drop the break-time zero-volume bars
    # Yahoo pads CN/HK intraday with, the same placement rule the main
    # yfinance chain applies, so both legs return the same sessions — and do
    # it BEFORE the window filter below: a HK 12:30 hourly bar re-anchors to
    # the 13:00 reopen, so filtering first would drop it for a request that
    # starts at 13:00 and the session would begin at 14:00 instead.
    if canonical != "1day":
        rows = _drop_midday_break_bars(rows, display, canonical)
    start_ms = _yf_parse_exchange_bound_ms(start_date, display, is_end=False)
    end_ms = _yf_parse_exchange_bound_ms(end_date, display, is_end=True)
    return [
        r
        for r in rows
        if (start_ms is None or r["time"] >= start_ms)
        and (end_ms is None or r["time"] <= end_ms)
    ]


async def _fetch_stock_series(
    symbol: str,
    interval: str,
    start_date: Optional[str],
    end_date: Optional[str],
    extended_hours: bool = False,  # DALTON-HOOK(stock-move-upgrade)
) -> tuple[Optional[dict], Optional[dict]]:
    """Fetch stock OHLCV: ginlix-data (CN daily, then US) → yfinance → FMP fallback (global).

    DALTON-HOOK(market-data-fallback): the yfinance-before-FMP order matches
    Dalton's market_data.providers (see the _yf import block).

    Returns ``(make_response_kwargs, None)`` on success or ``(None, error)``.
    The success kwargs carry canonical ``symbol``/``interval``, ``currency``,
    ``timezone``, ``data`` (ascending), the resolved ``source``, and for
    CN and HK listings the local-language ``name``, whichever source served
    the bars.
    """
    resolved = _resolve(symbol)
    (name, _), (ok, err) = await asyncio.gather(
        _names.display_names(resolved[1]),
        _fetch_stock_bars(
            resolved, interval, start_date, end_date, extended_hours=extended_hours
        ),
    )
    if ok is not None and name:
        ok["name"] = name
    return ok, err


async def _fetch_stock_bars(
    resolved: tuple[str, str, Optional[str], Optional[str]],
    interval: str,
    start_date: Optional[str],
    end_date: Optional[str],
    extended_hours: bool = False,  # DALTON-HOOK(stock-move-upgrade)
) -> tuple[Optional[dict], Optional[dict]]:
    display, legacy, currency, timezone = resolved
    canonical, err = _resolve_interval(interval, _STOCK_INTERVALS, symbol=display)
    if err is not None:
        return None, err

    intraday = canonical not in DAILY_INTERVALS
    if intraday and (not start_date or not end_date):
        return None, make_error(
            "invalid_argument",
            "start_date and end_date are required for intraday intervals "
            "(YYYY-MM-DD or YYYY-MM-DD HH:MM).",
            symbol=display,
            interval=canonical,
        )

    # CN A-shares, daily bars only: ginlix-data's minute history is short, so
    # CN intraday stays on the existing fallbacks.
    # Empty/error falls through the chain unchanged.
    if not intraday and symbol_market(legacy) == "cn":
        # The window runs back from the end the caller gave, else from today.
        cn_start = start_date
        if not start_date:
            try:
                end = datetime.strptime((end_date or "")[:10], "%Y-%m-%d")
            except ValueError:
                end = datetime.now(_tz.utc)
            cn_start = (end - timedelta(days=_CN_DEFAULT_DAILY_DAYS)).strftime("%Y-%m-%d")
        try:
            bars = (await _cn.get_daily(legacy, cn_start, end_date)).bars
        except httpx.HTTPError as exc:
            # An upstream miss (a 503, a 429, a dropped connection) is expected and
            # the chain answers instead, so one line is enough.
            logger.warning("ginlix-data CN daily unavailable for %s: %s", display, _http_miss(exc))
            bars = []
        except Exception:  # noqa: BLE001 - soft miss, chain continues
            logger.warning("ginlix-data CN daily failed for %s", display, exc_info=True)
            bars = []
        if bars:
            return {
                "data": _display_rows(bars, legacy, intraday=False),
                "source": "tushare",
                "symbol": display,
                "interval": canonical,
                "currency": currency,
                "timezone": timezone,
            }, None

    # ginlix-data's US tier (US equities only; returns None for anything else).
    ginlix_result = (
        None
        if legacy.startswith("^")  # DALTON-HOOK(stock-move-upgrade)
        else await _ginlix.fetch_stock_data(legacy, canonical, start_date, end_date)
    )
    refused: Optional[dict] = None
    if isinstance(ginlix_result, dict):
        status = ginlix_result.get("status")
        err = error_from_upstream(
            ginlix_result.get("error", "ginlix-data request failed"),
            status=status,
            symbol=display,
            interval=canonical,
        )
        if status not in (401, 403):
            return None, err
        # A refused credential says nothing about the request, so FMP may still
        # answer it. The refusal stays the error unless FMP returns rows.
        logger.warning("ginlix-data refused the credential for %s: %s", display, err["detail"])
        refused = err
    elif ginlix_result is not None:
        # Already-normalized display rows, descending → flip to ascending.
        data = list(reversed(ginlix_result))
        return {
            "data": data,
            "source": "ginlix-data",
            "symbol": display,
            "interval": canonical,
            "currency": currency,
            "timezone": timezone,
        }, None

    # DALTON-HOOK(market-data-fallback): free yfinance answers ahead of FMP
    # (Dalton's market_data.providers order — see the _yf import block). Any
    # miss — no library, unsupported interval, empty history, an error — falls
    # through to FMP unchanged; partial coverage of start_date also tries FMP
    # first and falls back to the partial yfinance bars with a coverage note.
    scale = minor_unit_scale(legacy)
    partial_yf: Optional[dict] = None
    if _yf is not None and canonical in _YF_INTERVALS:
        try:
            yf_rows = await asyncio.to_thread(
                _yf_fetch_history,
                display,
                canonical,
                start_date,
                end_date,
                extended_hours=extended_hours,
            )
        except Exception:  # noqa: BLE001 - soft miss, chain continues
            logger.warning("yfinance history failed for %s", display, exc_info=True)
            yf_rows = []
        if yf_rows:
            yf_data = _filter_display_rows_to_window(
                _display_rows(yf_rows, legacy, intraday=intraday, scale=scale),
                start_date,
                end_date,
                intraday=intraday,
            )
            if yf_data:
                if _yf_covers_start(
                    yf_data,
                    start_date,
                    intraday=intraday,
                    display=display,
                    interval=canonical,
                ):
                    return {
                        "data": yf_data,
                        "source": "yfinance",
                        "symbol": display,
                        "interval": canonical,
                        "currency": currency,
                        "timezone": timezone,
                    }, None
                actual_start = yf_data[0]["date"]
                actual_end = yf_data[-1]["date"]
                # DALTON-HOOK(market-data-fallback): the FMP clause is stamped
                # when this partial answer is actually returned — a 200 with no
                # rows for the window is "FMP has no data there", not an
                # unavailable fallback.
                partial_yf = {
                    "data": yf_data,
                    "source": "yfinance",
                    "symbol": display,
                    "interval": canonical,
                    "currency": currency,
                    "timezone": timezone,
                    "note": (
                        f"yfinance partial coverage: actual covered range is "
                        f"{actual_start} to {actual_end} (requested start_date={start_date}"
                    ),
                }

    def _partial_with_outcome(outcome: str) -> tuple[dict, None]:
        return {**partial_yf, "note": f"{partial_yf['note']}; {outcome})"}, None

    # DALTON-HOOK(market-data-fallback): FMP fallback (all markets; converts GBX/pence to pounds).
    try:
        client = await get_fmp_client()
    except Exception:  # noqa: BLE001
        if partial_yf is not None:
            return _partial_with_outcome("FMP fallback unavailable")
        return None, refused or make_error(
            "client_unavailable",
            "FMP client unavailable (FMP_API_KEY not configured).",
            symbol=display,
        )

    try:
        if canonical in DAILY_INTERVALS:
            rows = await client.get_stock_price(legacy, from_date=start_date, to_date=end_date)
        else:
            rows = await client.get_intraday_chart(
                legacy,
                canonical,
                from_date=start_date,
                to_date=end_date,
                **({"extended": True} if extended_hours else {}),  # DALTON-HOOK(stock-move-upgrade)
            )
    except Exception as e:  # noqa: BLE001
        if partial_yf is not None:
            return _partial_with_outcome("FMP fallback unavailable")
        return None, refused or error_from_exception(
            e, "FMP fetch failed.", symbol=display, interval=canonical
        )
    if not isinstance(rows or [], list):
        # FMP answers some failures with a 200 and an error object, not rows.
        if partial_yf is not None:
            return _partial_with_outcome("FMP fallback unavailable")
        return None, refused or make_error(
            "upstream_error", "FMP fetch failed.", symbol=display, interval=canonical
        )

    data = _display_rows(rows or [], legacy, intraday=intraday, scale=scale)
    if not data and partial_yf is not None:  # DALTON-HOOK(market-data-fallback)
        # A 200 with no rows for the window: FMP has no data there, the
        # fallback itself is fine.
        return _partial_with_outcome("FMP 无该区间数据")
    if refused and not data:
        return None, refused
    return {
        "data": data,
        "source": "fmp",
        "symbol": display,
        "interval": canonical,
        "currency": currency,
        "timezone": timezone,
    }, None


# ---------------------------------------------------------------------------
# MCP server + tools
# ---------------------------------------------------------------------------

mcp = MCPServer("PriceDataMCP", lifespan=_lifespan)


_NAME_ECHO = described(STR, "Local-language company name; CN and HK listings only, omitted elsewhere.")

_OUT_GET_STOCK_DATA = output_model(
    "GetStockDataOut",
    envelope_schema(
        RECORDS,
        frame=("symbol", "interval", "currency", "timezone"),
        echo={"name": _NAME_ECHO},
    ),
)


@mcp.tool()
async def get_stock_data(
    symbol: str,
    interval: str = "1day",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    extended_hours: bool = False,  # DALTON-HOOK(stock-move-upgrade)
) -> _OUT_GET_STOCK_DATA:
    """Fetch OHLCV price bars for a stock; use for historical charts/analysis.

    Intervals: 1min|5min|15min|30min|1hour|4hour|1day ("1d"/"daily" accepted);
    start_date and end_date required for intraday.

    Args:
        symbol: US "AAPL", HK "0700.HK", CN "600519.SH", UK "VOD.L", futures "NQ=F", index "^TNX".
        interval: Bar size; one of the intervals above.
        start_date: "YYYY-MM-DD[ HH:MM]" start filter.
        end_date: "YYYY-MM-DD[ HH:MM]" end filter.
        extended_hours: Include pre/post-market intraday bars (default False).

    Returns:
        dict: {symbol, interval, currency, timezone, count, data, source,
        name?}. data: list of {date, open, high, low, close, volume} ascending
        (oldest first); date exchange-local "YYYY-MM-DD[ HH:MM:SS]"; prices in
        `currency` major units. name: local-language name (CN/HK only). On
        error: {error: <code>, detail}.
    """
    env, err = await _fetch_stock_series(
        symbol, interval, start_date, end_date, extended_hours=extended_hours
    )
    if err is not None:
        return err
    return make_response(**env)


_OUT_GET_ASSET_DATA = output_model(
    "GetAssetDataOut",
    envelope_schema(
        RECORDS,
        frame=("symbol", "interval", "currency", "timezone"),
        echo={"asset_type": STR, "name": _NAME_ECHO},
    ),
)


@mcp.tool()
async def get_asset_data(
    symbol: str,
    asset_type: str,
    interval: str = "1day",
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    extended_hours: bool = False,  # DALTON-HOOK(stock-move-upgrade)
) -> _OUT_GET_ASSET_DATA:
    """Fetch OHLCV bars for a stock, commodity, crypto, or forex pair.

    Intervals: 1min|5min|1hour|1day (stock adds 15min|30min|4hour); dates
    required for intraday.

    Args:
        symbol: Stock "AAPL", commodity "GCUSD"/"CL=F", crypto "BTCUSD", fx "EURUSD".
        asset_type: One of stock|commodity|crypto|forex.
        interval: Bar size; see intervals above.
        from_date: "YYYY-MM-DD[ HH:MM]" start filter.
        to_date: "YYYY-MM-DD[ HH:MM]" end filter.
        extended_hours: Include pre/post-market intraday bars (default False).

    Returns:
        dict: {symbol, asset_type, interval, currency, timezone, count, data,
        source, name?}. data: list of {date, open, high, low, close, volume}
        ascending (oldest first); exchange-local "YYYY-MM-DD[ HH:MM:SS]"; prices
        in `currency` major units. name: local-language name (CN/HK stock only).
        On error: {error: <code>, detail}.
    """
    at = asset_type.lower().strip()
    if at not in {"stock", "commodity", "crypto", "forex"}:
        return make_error(
            "invalid_argument",
            f"Invalid asset_type {asset_type!r}.",
            supported=["stock", "commodity", "crypto", "forex"],
        )

    if at == "stock":
        env, err = await _fetch_stock_series(  # DALTON-HOOK(stock-move-upgrade)
            symbol, interval, from_date, to_date, extended_hours=extended_hours  # DALTON-HOOK(stock-move-upgrade)
        )  # DALTON-HOOK(stock-move-upgrade)
        if err is not None:
            return err
        env["asset_type"] = at
        return make_response(**env)

    # commodity / crypto / forex — FMP direct (the provider chain and ginlix
    # cover equities/indexes only). Currency + timezone from canonical identity.
    hint = {"crypto": AssetClass.CRYPTO, "forex": AssetClass.FX}.get(at)
    display, _legacy, currency, timezone = _resolve(symbol, asset_class=hint)
    canonical, err = _resolve_interval(interval, _ASSET_INTERVALS, symbol=display)
    if err is not None:
        return err

    intraday = canonical not in DAILY_INTERVALS
    if intraday and (not from_date or not to_date):
        return make_error(
            "invalid_argument",
            "from_date and to_date are required for intraday intervals "
            "(YYYY-MM-DD or YYYY-MM-DD HH:MM).",
            symbol=display,
            interval=canonical,
        )

    try:
        client = await get_fmp_client()
    except Exception:  # noqa: BLE001
        return make_error(
            "client_unavailable",
            "FMP client unavailable (FMP_API_KEY not configured).",
            symbol=display,
        )

    try:
        ext_kw = {"extended": True} if extended_hours else {}  # DALTON-HOOK(stock-move-upgrade)
        if canonical in DAILY_INTERVALS:
            if at == "commodity":
                rows = await client.get_commodity_price(symbol, from_date=from_date, to_date=to_date)
            elif at == "crypto":
                rows = await client.get_crypto_price(symbol, from_date=from_date, to_date=to_date)
            else:
                rows = await client.get_forex_price(symbol, from_date=from_date, to_date=to_date)
        elif at == "commodity":
            rows = await client.get_commodity_intraday_chart(
                symbol, canonical, from_date=from_date, to_date=to_date, **ext_kw
            )
        elif at == "crypto":
            rows = await client.get_crypto_intraday_chart(
                symbol, canonical, from_date=from_date, to_date=to_date, **ext_kw
            )
        else:
            rows = await client.get_forex_intraday_chart(
                symbol, canonical, from_date=from_date, to_date=to_date, **ext_kw
            )
    except Exception as e:  # noqa: BLE001
        return error_from_exception(
            e, "FMP fetch failed.", symbol=display, interval=canonical
        )

    data = _display_rows(rows or [], symbol, intraday=intraday)
    return make_response(
        data,
        source="fmp",
        symbol=display,
        interval=canonical,
        currency=currency,
        timezone=timezone,
        asset_type=at,
    )


_OUT_GET_SHORT_DATA = output_model(
    "GetShortDataOut",
    envelope_schema(
        RECORDS_BY_KEY,
        frame=("symbol", "timezone"),
        echo={
            "data_type": STR,
            "errors": described(
                OBJECT, "Per-section upstream errors on partial success."
            ),
        },
    ),
)


@mcp.tool()
async def get_short_data(
    symbol: str,
    data_type: Literal["short_interest", "short_volume", "both"] = "both",
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: int = 20,
) -> _OUT_GET_SHORT_DATA:
    """Fetch short interest and/or short volume for a US stock (short-squeeze work).

    Short interest is FINRA bi-monthly (settlement_date); short volume is daily
    off-exchange (date). ginlix-data only.

    Args:
        symbol: Stock ticker, e.g. "AAPL".
        data_type: "short_interest", "short_volume", or "both" (default).
        from_date: "YYYY-MM-DD" start filter (optional).
        to_date: "YYYY-MM-DD" end filter (optional).
        limit: Max records per section (default 20, max 50000).

    Returns:
        dict: {symbol, timezone, count, data, source, data_type}. data is a dict
        of {short_interest, short_volume} lists (requested sections only), newest
        first. Record fields are vendor-native (e.g. settlement_date,
        short_volume_ratio). Partial failures appear in an `errors` map. On
        error: {error: <code>, detail}.
    """
    display, legacy, _currency, timezone = _resolve(symbol)

    if not await _ginlix.ensure():
        return make_error(
            "client_unavailable",
            "Short data requires ginlix-data (not configured).",
            symbol=display,
        )

    raw = await _ginlix.fetch_short_data(
        legacy, data_type=data_type, from_date=from_date, to_date=to_date, limit=limit,
    )

    data: dict[str, list] = {}
    errors: dict[str, str] = {}
    for section in ("short_interest", "short_volume"):
        if section in raw:
            data[section] = raw[section]
        if f"{section}_error" in raw:
            errors[section] = raw[f"{section}_error"]

    if not data:
        return make_error(
            "upstream_error",
            "; ".join(errors.values()) or "No short data returned.",
            symbol=display,
            data_type=data_type,
        )

    extra: dict[str, Any] = {"data_type": data_type}
    if errors:
        extra["errors"] = errors
    return make_response(
        data,
        source="ginlix-data",
        symbol=display,
        timezone=timezone,
        **extra,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
