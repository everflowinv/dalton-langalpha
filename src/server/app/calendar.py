"""Calendar endpoints — economic releases and earnings announcements."""

# DALTON-HOOK(market-data-fallback): FMP's shared ~250 calls/day free quota
# runs out most days, so both calendars degrade to Yahoo (mapped and paged in
# src/dalton/market_data/calendar_fallback.py) instead of 500ing — only a
# Yahoo failure too returns an (empty) 200. Degradation fires on
# quota/missing-key/transport/5xx alone (fmp_degradable there); other errors
# are programming mistakes and propagate.

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from src.dalton.market_data import calendar_fallback
from src.data_client.fmp.fmp_client import FMPClient
from src.server.models.calendar import (
    EarningsCalendarResponse,
    EarningsEvent,
    EconomicCalendarResponse,
    EconomicEvent,
)
from src.server.services.cache.earnings_cache_service import EarningsCacheService
from src.server.utils.api import CurrentUserId

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/calendar", tags=["Calendar"])

_earnings_cache = EarningsCacheService()
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")  # DALTON-HOOK(market-data-fallback)


def _default_dates(
    from_date: Optional[str], to_date: Optional[str]
) -> tuple[str, str]:
    """Fill in missing dates: from defaults to today, to to from+7."""
    if not from_date:
        from_date = date.today().isoformat()
    if not to_date:
        # DALTON-HOOK(market-data-fallback): a week after `from`, not after
        # today, so a lone future `from` is not rejected as an inverted range.
        try:
            to_date = (date.fromisoformat(from_date) + timedelta(days=7)).isoformat()
        except (ValueError, OverflowError):
            # A malformed `from` is rejected by the range check that follows;
            # one within a week of date.max gets that week clamped to date.max.
            to_date = (
                date.max.isoformat()
                if _ISO_DAY.fullmatch(from_date or "")
                else (date.today() + timedelta(days=7)).isoformat()
            )
    return from_date, to_date


# DALTON-HOOK(market-data-fallback): with degradation in place a malformed
# range must not be swallowed into an empty 200 — it is rejected 422 before
# any data source or cache is touched.
def _validate_date_range(from_date: str, to_date: str) -> None:
    """422 on a malformed or inverted range — the caller's mistake, not a data gap."""
    # Strict YYYY-MM-DD: fromisoformat also takes 20261026 and week dates,
    # which the data sources and the cache keys would not agree on.
    try:
        if not (_ISO_DAY.fullmatch(from_date) and _ISO_DAY.fullmatch(to_date)):
            raise ValueError
        start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=422, detail="from/to must be ISO dates (YYYY-MM-DD)"
        ) from None
    if start > end:
        raise HTTPException(
            status_code=422,
            detail=f"from ({from_date}) must not be after to ({to_date})",
        )


@router.get("/economic", response_model=EconomicCalendarResponse)
async def get_economic_calendar(
    user_id: CurrentUserId,
    from_date: Optional[str] = Query(
        None, alias="from", description="Start date (YYYY-MM-DD). Defaults to today."
    ),
    to_date: Optional[str] = Query(
        None, alias="to", description="End date (YYYY-MM-DD). Defaults to from+7."
    ),
) -> EconomicCalendarResponse:
    """Get upcoming and past economic data releases (GDP, CPI, etc.)."""
    from_date, to_date = _default_dates(from_date, to_date)
    _validate_date_range(from_date, to_date)

    fmp_client: Optional[FMPClient] = None
    try:
        fmp_client = FMPClient()
    except (ValueError, ImportError) as e:
        logger.warning("Economic calendar: FMP unavailable (%s); trying yfinance", e)

    raw: Optional[list] = None
    if fmp_client is not None:
        try:
            try:
                raw = await fmp_client.get_economic_calendar(
                    from_date=from_date, to_date=to_date
                )
            finally:
                await fmp_client.close()
        except Exception as e:
            # DALTON-HOOK(market-data-fallback): only FMP being unable to
            # answer (quota, transport, 5xx) degrades to Yahoo; anything else
            # is a programming error and keeps its 500.
            if not calendar_fallback.fmp_degradable(e):
                raise
            logger.error("Economic calendar: FMP failed (%s); trying yfinance", e)

    if raw is not None:
        items: list = raw or []
    else:
        # DALTON-HOOK(market-data-fallback): Yahoo's economic events stand in
        # for FMP's; a Yahoo failure too degrades to an empty list, not a 500.
        try:
            items = await calendar_fallback.economic_from_yfinance(
                from_date, to_date
            )
        except Exception as e:
            logger.error(
                "Economic calendar: yfinance also failed (%s); returning empty", e
            )
            items = []

    events = [EconomicEvent(**item) for item in items]
    return EconomicCalendarResponse(data=events, count=len(events))


@router.get("/earnings", response_model=EarningsCalendarResponse)
async def get_earnings_calendar(
    user_id: CurrentUserId,
    from_date: Optional[str] = Query(
        None, alias="from", description="Start date (YYYY-MM-DD). Defaults to today."
    ),
    to_date: Optional[str] = Query(
        None, alias="to", description="End date (YYYY-MM-DD). Defaults to from+7."
    ),
) -> EarningsCalendarResponse:
    """Get upcoming and past earnings announcements with EPS and revenue data."""
    from_date, to_date = _default_dates(from_date, to_date)
    _validate_date_range(from_date, to_date)

    # FMP's 24h cache first, then yfinance's 1h one — a yfinance answer never
    # enters the FMP cache.
    cached = await _earnings_cache.get(from_date, to_date)
    if cached is None:
        # DALTON-HOOK(market-data-fallback): yfinance answers cache an hour
        # under their own key, never in FMP's 24h cache.
        cached = await calendar_fallback.cached_yf_earnings(from_date, to_date)
    if cached is not None:
        events = [EarningsEvent(**item) for item in cached]
        return EarningsCalendarResponse(data=events, count=len(events))

    fmp_client: Optional[FMPClient] = None
    try:
        fmp_client = FMPClient()
    except (ValueError, ImportError) as e:
        logger.warning("Earnings calendar: FMP unavailable (%s); trying yfinance", e)

    raw: Optional[list] = None
    if fmp_client is not None:
        try:
            try:
                raw = await fmp_client.get_earnings_calendar_by_date(
                    from_date=from_date, to_date=to_date
                )
            finally:
                await fmp_client.close()
        except Exception as e:
            # DALTON-HOOK(market-data-fallback): only FMP being unable to
            # answer (quota, transport, 5xx) degrades to Yahoo; anything else
            # is a programming error and keeps its 500.
            if not calendar_fallback.fmp_degradable(e):
                raise
            logger.error("Earnings calendar: FMP failed (%s); trying yfinance", e)

    # Rows are validated before either cache stores them, so a malformed
    # upstream row fails this request without poisoning later ones.
    if raw is not None:
        items: list = raw or []
        events = [EarningsEvent(**item) for item in items]
        await _earnings_cache.set(items, from_date, to_date)
    else:
        # DALTON-HOOK(market-data-fallback): Yahoo's calendar stands in for
        # FMP's; only a Yahoo failure too returns the empty 200 below.
        try:
            items = await calendar_fallback.earnings_from_yfinance(
                from_date, to_date
            )
        except Exception as e:
            logger.error(
                "Earnings calendar: yfinance also failed (%s); returning empty", e
            )
            return EarningsCalendarResponse(data=[], count=0)
        events = [EarningsEvent(**item) for item in items]
        await calendar_fallback.store_yf_earnings(items, from_date, to_date)

    return EarningsCalendarResponse(data=events, count=len(events))
