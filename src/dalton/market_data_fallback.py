"""FMP first, yfinance when FMP cannot answer.

The financial-data slot holds one source, and with ``FMP_API_KEY`` set it was
FMP alone: once the free quota ran out (429), or an endpoint sat outside the
plan (402), every search, quote and profile failed even though yfinance could
have answered. This keeps FMP primary and asks yfinance only when FMP's
failure says nothing about the request itself — quota, plan, auth, an outage
or a timeout. An empty answer or a 4xx about the arguments stays FMP's, so a
symbol FMP does not know is still "no match" rather than a second opinion.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from src.data_client.financial_data_provider import _close_all
from src.data_client.fmp.fmp_client import FMPRequestError

logger = logging.getLogger(__name__)

# Quota (429), plan (402), and a key FMP refuses (401/403).
_ACCOUNT_STATUSES = frozenset({401, 402, 403, 429})


def fmp_cannot_answer(exc: BaseException) -> bool:
    """Whether *exc* is FMP being unavailable rather than the request being bad.

    ``FMPRequestError`` without a status is a timeout or a transport failure.
    """
    if not isinstance(exc, FMPRequestError):
        return False
    status = exc.status_code
    return status is None or status in _ACCOUNT_STATUSES or status >= 500


class _Fallback:
    """A protocol method answered by the primary, else by the fallback."""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, owner: type | None = None) -> Any:
        if obj is None:
            return self
        return functools.partial(obj._call, self.name)


class FallbackFinancialSource:
    """FinancialDataSource that falls back from FMP to yfinance per call.

    No memory of past failures: workers share nothing, and one 429 costs a
    fast round trip, so every call asks FMP first and a restored quota is
    used the moment it returns.
    """

    def __init__(self, primary: Any, fallback: Any) -> None:
        self._primary = primary
        self._fallback = fallback

    async def _call(self, method: str, /, *args: Any, **kwargs: Any) -> Any:
        try:
            return await getattr(self._primary, method)(*args, **kwargs)
        except Exception as exc:
            if not fmp_cannot_answer(exc):
                raise
            # Status only: the arguments may be a user's search text.
            logger.warning(
                "financial_data.fallback | method=%s from=fmp to=yfinance status=%s",
                method,
                getattr(exc, "status_code", None),
            )
            return await getattr(self._fallback, method)(*args, **kwargs)

    get_company_profile = _Fallback()
    get_realtime_quote = _Fallback()
    get_income_statements = _Fallback()
    get_cash_flows = _Fallback()
    get_key_metrics = _Fallback()
    get_financial_ratios = _Fallback()
    get_price_performance = _Fallback()
    get_analyst_price_targets = _Fallback()
    get_analyst_ratings = _Fallback()
    get_earnings_history = _Fallback()
    get_revenue_by_segment = _Fallback()
    screen_stocks = _Fallback()
    search_stocks = _Fallback()

    async def get_sector_performance(
        self, target_date: str | None = None
    ) -> list[dict[str, Any]]:
        if target_date:
            # yfinance only has today's ETF snapshot; do not fall back and
            # pass today's performance off as historical target_date data.
            return await self._primary.get_sector_performance(target_date=target_date)
        return await self._call("get_sector_performance", target_date=target_date)

    async def close(self) -> None:
        await _close_all([("fmp", self._primary), ("yfinance", self._fallback)])
