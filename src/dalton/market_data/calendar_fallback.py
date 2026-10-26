"""Calendar degradation to Yahoo when FMP cannot answer.

FMP's shared ~250 calls/day free quota runs out most days, so both calendar
endpoints serve from Yahoo instead of 500ing (lead decision 2026-10-26). This
module owns everything Yahoo-side — row mapping, paging, timezone handling,
and the yfinance answer cache — so the upstream endpoints hold calls only.

Yahoo column sets confirmed against yfinance 1.7.0 on 2026-10-26:
``sp_earnings`` pages are indexed by ``Symbol`` with ``Event Start Date``
(tz-aware UTC), ``EPS Estimate`` and ``Reported EPS``; ``economic_event``
pages are indexed by ``Event`` with ``Region``, ``Event Time`` (tz-aware
UTC), ``Actual``, ``Expected`` and ``Last``. Both calendars take an exclusive
UTC ``end``, and both sort/paginate 100 rows per call.

On the FMP side, degradation is limited to the source being unable to answer
(:func:`fmp_degradable` — quota, transport/timeout, 5xx); anything else is a
programming error and propagates to a 500. The Yahoo layer is the last
resort, so its callers may catch everything there.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from src.data_client.fmp.fmp_client import FMPRequestError, FmpQuotaExhausted
from src.utils.cache import redis_cache

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Yahoo caps one calendar page at 100 rows. Earnings pages arrive
# market-cap-desc — the head carries the names a dashboard needs — and 20
# pages (2000 rows) cover a two-week window; economic pages are unordered, so
# their cap is a flood guard.
_YF_PAGE = 100
_YF_EARNINGS_MAX_PAGES = 20
_YF_ECONOMIC_MAX_PAGES = 5

# yfinance answers are point-in-time (estimates drift through the day), so
# they cache for an hour under their own key — never in FMP's 24h cache.
_YF_EARNINGS_TTL = 3600
_YF_EARNINGS_KEY = "earnings:yf:{from_date}:{to_date}"


def fmp_degradable(exc: BaseException) -> bool:
    """Whether an FMP failure means the source cannot answer (degrade) or the
    request itself is bad (propagate).

    Per ``FMPClient._make_request``: timeouts and transport errors surface as
    ``FMPRequestError`` with no status; a 429 is the quota (``FmpQuotaExhausted``
    before the request, "Limit Reach" after it); upstream 5xx carry their
    status. The remaining 4xx (bad symbol, key/plan problems) are not
    degradation.
    """
    if isinstance(exc, FmpQuotaExhausted):
        return True
    return isinstance(exc, FMPRequestError) and (
        exc.status_code is None or exc.status_code == 429 or exc.status_code >= 500
    )


def _none_if_nan(val: Any) -> Optional[float]:
    """numpy/pandas NaN (no estimate or report yet) must serialize as null."""
    if hasattr(val, "item"):
        val = val.item()
    return val if isinstance(val, (int, float)) and val == val else None


def _as_et(when: Any) -> pd.Timestamp:
    """A Yahoo event instant in US/Eastern.

    Tz-naive values — and bare strings, parsed here — are taken as UTC first:
    a 20:00 UTC print is a 4 PM ET same-day release, and an ET-evening 21:00
    event is already the next UTC day.
    """
    ts = pd.Timestamp(when)
    try:
        return ts.tz_convert(_ET)
    except (AttributeError, TypeError):
        # tz_convert refuses a naive timestamp; Yahoo's instants are UTC.
        return ts.tz_localize("UTC").tz_convert(_ET)


def _et_date(when: Any) -> str:
    return _as_et(when).date().isoformat()


def _et_datetime(when: Any) -> str:
    return _as_et(when).strftime("%Y-%m-%d %H:%M:%S")


def _yf_end(to_date: str) -> str:
    """The exclusive UTC ``end`` bound that still covers to_date's ET evening.

    An event at 21:00 ET is 01:00 UTC the next day (02:00 in winter), so +2
    days always clears it; rows are clipped back to the ET window afterwards.
    """
    return (date.fromisoformat(to_date) + timedelta(days=2)).isoformat()


def _collect_pages(
    fetch_page: Callable[[int], Any],
    map_page: Callable[[Any], list[dict[str, Any]]],
    *,
    max_pages: int,
    label: str,
    from_date: str,
    to_date: str,
) -> list[dict[str, Any]]:
    """Rows from Yahoo's 100-row pages: stop on a short or missing page.

    A missing page (None) after good ones keeps what earlier pages returned;
    reaching the cap on full pages means truncation, which is logged rather
    than hidden.
    """
    rows: list[dict[str, Any]] = []
    for offset in range(0, _YF_PAGE * max_pages, _YF_PAGE):
        page = fetch_page(offset)
        rows.extend(map_page(page))
        if page is None or len(page) < _YF_PAGE:
            return rows
    logger.warning(
        "calendar_fallback.yf_truncated | calendar=%s window=%s..%s rows=%d",
        label,
        from_date,
        to_date,
        len(rows),
    )
    return rows


def _yf_earnings_rows(df: Any) -> list[dict[str, Any]]:
    """Map a yfinance ``sp_earnings`` page onto an FMP earnings-calendar row.

    Yahoo has EPS estimate/actual but no consensus revenue, so those keys
    stay None; the announcement date is the event's US/Eastern date.
    """
    rows: list[dict[str, Any]] = []
    if df is None or getattr(df, "empty", False):
        return rows
    for symbol, row in df.iterrows():
        when = row.get("Event Start Date")
        if when is None or pd.isna(when):
            continue
        rows.append(
            {
                "symbol": str(symbol),
                "date": _et_date(when),
                "epsActual": _none_if_nan(row.get("Reported EPS")),
                "epsEstimated": _none_if_nan(row.get("EPS Estimate")),
                "revenueActual": None,
                "revenueEstimated": None,
                "lastUpdated": None,
            }
        )
    return rows


def _yf_economic_rows(df: Any) -> list[dict[str, Any]]:
    """Map a yfinance ``economic_event`` page onto an FMP economic-calendar row.

    Yahoo publishes no currency (the response model requires the key, so it
    stays an empty string) and no impact/change/changePercentage; ``For`` and
    ``Revised`` have no model counterpart and are dropped.
    """
    rows: list[dict[str, Any]] = []
    if df is None or getattr(df, "empty", False):
        return rows
    for event, row in df.iterrows():
        when = row.get("Event Time")
        if when is None or pd.isna(when):
            continue
        rows.append(
            {
                "date": _et_datetime(when),
                "country": str(row.get("Region") or ""),
                "event": str(event),
                "currency": "",
                "previous": _none_if_nan(row.get("Last")),
                "estimate": _none_if_nan(row.get("Expected")),
                "actual": _none_if_nan(row.get("Actual")),
                "change": None,
                "impact": None,
                "changePercentage": None,
            }
        )
    return rows


def _window_rows(rows: list[dict[str, Any]], from_date: str, to_date: str) -> list[dict[str, Any]]:
    """Clip to the requested ET-date window: Yahoo bounds instants in UTC, the
    calendar a caller asked for is ET dates, and the widened end over-fetches
    by design."""
    return [r for r in rows if from_date <= r["date"][:10] <= to_date]


async def earnings_from_yfinance(from_date: str, to_date: str) -> list[dict[str, Any]]:
    """The earnings calendar from Yahoo, in the FMP row shape. Raises on failure."""
    end = _yf_end(to_date)

    def _fetch() -> list[dict[str, Any]]:
        import yfinance as yf

        cal = yf.Calendars(start=from_date, end=end)

        def page(offset: int) -> Any:
            return cal.get_earnings_calendar(
                filter_most_active=False,  # the most-actives filter drops the rest of the market
                limit=_YF_PAGE,
                offset=offset,
            )

        rows = _collect_pages(
            page,
            _yf_earnings_rows,
            max_pages=_YF_EARNINGS_MAX_PAGES,
            label="earnings",
            from_date=from_date,
            to_date=to_date,
        )
        return _window_rows(rows, from_date, to_date)

    return await asyncio.to_thread(_fetch)


async def economic_from_yfinance(from_date: str, to_date: str) -> list[dict[str, Any]]:
    """Economic events from Yahoo, in the FMP row shape. Raises on failure."""
    end = _yf_end(to_date)

    def _fetch() -> list[dict[str, Any]]:
        import yfinance as yf

        cal = yf.Calendars(start=from_date, end=end)

        def page(offset: int) -> Any:
            return cal.get_economic_events_calendar(limit=_YF_PAGE, offset=offset)

        rows = _collect_pages(
            page,
            _yf_economic_rows,
            max_pages=_YF_ECONOMIC_MAX_PAGES,
            label="economic",
            from_date=from_date,
            to_date=to_date,
        )
        return _window_rows(rows, from_date, to_date)

    return await asyncio.to_thread(_fetch)


async def cached_yf_earnings(
    from_date: str, to_date: str
) -> Optional[list[dict[str, Any]]]:
    """The cached yfinance earnings answer, or None; Redis trouble is a miss."""
    try:
        raw = await redis_cache.get_cache_client().get(
            _YF_EARNINGS_KEY.format(from_date=from_date, to_date=to_date)
        )
        if raw is not None:
            return json.loads(raw)
    except Exception:
        logger.debug("calendar_fallback.yf_earnings_cache.get.miss", exc_info=True)
    return None


async def store_yf_earnings(
    rows: list[dict[str, Any]], from_date: str, to_date: str
) -> None:
    """Cache a yfinance earnings answer for an hour; an empty answer is not one."""
    if not rows:
        return
    try:
        await redis_cache.get_cache_client().set(
            _YF_EARNINGS_KEY.format(from_date=from_date, to_date=to_date),
            json.dumps(rows),
            ttl=_YF_EARNINGS_TTL,
        )
    except Exception:
        logger.debug("calendar_fallback.yf_earnings_cache.set.failed", exc_info=True)
