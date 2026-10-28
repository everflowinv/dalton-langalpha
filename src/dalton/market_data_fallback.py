"""yfinance first; FMP kept for what only it can answer.

The financial-data slot held FMP primary and asked yfinance only when FMP
could not answer, so the free tier's ~250 calls/day burned down on quotes,
searches and profiles that yfinance serves for free — and once it ran out,
every call ate a 429 before falling back (user decision 2026-10-24: yfinance
first, no paid FMP). Every method yfinance implements now asks it first; FMP
backs those up and alone serves the rest (revenue segments, dated sector
performance). An empty yfinance answer counts as "not answered" too — several
adapters return ``[]`` on a Yahoo miss — so FMP gets its turn; when FMP also
fails or comes back empty, yfinance's original result is returned rather than
an exception. The quota marker itself lives in ``FMPClient._make_request``
(``src/data_client/fmp/fmp_client.py``): a 429 or a "Limit Reach" body writes
one Redis marker shared by every worker, TTL'd to FMP's documented daily
reset (3 PM US/Eastern), and while it holds FMP requests raise
:class:`FmpQuotaExhausted` without a request. A 402 fails its call alone —
it may only mean the endpoint sits outside the plan.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from src.data_client.financial_data_provider import _close_all
from src.data_client.fmp.fmp_client import FMPRequestError, FmpQuotaExhausted

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


# Methods whose single-row answer IS a quote: a row without a usable price is
# no answer. ``get_realtime_quote`` alone — screener rows carry a ``price``
# key too, but a missing one there is a real screen result, not a dead quote.
_QUOTE_METHODS = frozenset({"get_realtime_quote"})


def _answered(result: Any, *, quote: bool = False) -> bool:
    """Whether yfinance's answer carries data a caller can use.

    ``None``, an empty list or dict count as "no answer" for every method.
    For a quote method, so does a lone row whose ``price`` is missing, None,
    ≤ 0 or NaN (the adapter emits ``price: 0`` when Yahoo has no print).
    """
    if result is None or result == [] or result == {}:
        return False
    if quote and isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        price = result[0].get("price")
        if price is None or (
            isinstance(price, (int, float)) and (price != price or price <= 0)
        ):
            return False
    return True


class _Fallback:
    """A protocol method answered per this source's per-method routing."""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, owner: type | None = None) -> Any:
        if obj is None:
            return self
        return functools.partial(obj._call, self.name)


# Methods yfinance answers with real data (each implemented in
# data_client/yfinance/financial_source.py): yfinance first, FMP the backup.
_YFINANCE_FIRST = frozenset({
    "get_company_profile",
    "get_realtime_quote",
    "get_income_statements",
    "get_cash_flows",
    "get_key_metrics",
    "get_financial_ratios",
    "get_price_performance",
    "get_analyst_price_targets",
    "get_analyst_ratings",
    "get_earnings_history",
    "screen_stocks",
    "search_stocks",
})

# Methods yfinance cannot answer at all: its revenue segments are a ``[]``
# stub, so FMP serves them directly with no yfinance detour.
_FMP_ONLY = frozenset({"get_revenue_by_segment"})


class FallbackFinancialSource:
    """FinancialDataSource with yfinance primary and FMP per method.

    A protocol method yfinance has no implementation for (AttributeError) or a
    new one this class does not know still lands on the yfinance-first path,
    where the miss hands the call to FMP — the same default as the flip.
    """

    def __init__(self, fmp: Any, yfinance: Any) -> None:
        self._fmp = fmp
        self._yfinance = yfinance

    async def _call(self, method: str, /, *args: Any, **kwargs: Any) -> Any:
        if method in _FMP_ONLY:
            return await self._fmp_only(method, *args, **kwargs)
        return await self._yfinance_first(method, *args, **kwargs)

    async def _yfinance_first(
        self, method: str, /, *args: Any, **kwargs: Any
    ) -> Any:
        try:
            result = await getattr(self._yfinance, method)(*args, **kwargs)
        except Exception as yf_exc:
            # Type only: the arguments may be a user's search text.
            logger.warning(
                "financial_data.fallback | method=%s from=yfinance to=fmp error=%s",
                method,
                type(yf_exc).__name__,
            )
            try:
                return await getattr(self._fmp, method)(*args, **kwargs)
            except FmpQuotaExhausted:
                # The shared marker says FMP's quota is gone, so there is
                # nothing to fall to; the yfinance failure is the answer.
                logger.warning(
                    "financial_data.fallback | method=%s yfinance failed; "
                    "fmp quota exhausted",
                    method,
                )
                raise yf_exc
        if _answered(result, quote=method in _QUOTE_METHODS):
            return result
        logger.warning(
            "financial_data.fallback | method=%s from=yfinance reason=empty to=fmp",
            method,
        )
        try:
            fmp_result = await getattr(self._fmp, method)(*args, **kwargs)
        except Exception as fmp_exc:  # noqa: BLE101 — yfinance's empty answer survives
            # DALTON-HOOK(market-data-fallback): FMP *unavailable* (quota
            # gone, transport, 5xx) after yfinance answered nothing has no
            # answer left to give — returning yfinance's [] would read as
            # "no such data" — so the failure, with its reason, is the
            # answer. A request-level FMP refusal (4xx) or an empty FMP
            # result below still defers to yfinance's answer.
            if fmp_cannot_answer(fmp_exc):
                logger.warning(
                    "financial_data.fallback | method=%s from=fmp error=%s after "
                    "empty yfinance (raising: no answer left)",
                    method,
                    type(fmp_exc).__name__,
                )
                raise
            logger.warning(
                "financial_data.fallback | method=%s from=fmp error=%s after "
                "empty yfinance",
                method,
                type(fmp_exc).__name__,
            )
            return result
        return fmp_result if _answered(fmp_result, quote=method in _QUOTE_METHODS) else result

    async def _fmp_only(self, method: str, /, *args: Any, **kwargs: Any) -> Any:
        # Only FMP serves these; a tripped quota marker raises
        # FmpQuotaExhausted from FMPClient._make_request without a request.
        return await getattr(self._fmp, method)(*args, **kwargs)

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
            return await self._fmp_only("get_sector_performance", target_date=target_date)
        return await self._call("get_sector_performance", target_date=target_date)

    async def close(self) -> None:
        await _close_all([("fmp", self._fmp), ("yfinance", self._yfinance)])
