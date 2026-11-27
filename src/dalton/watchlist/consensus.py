"""Public consensus valuation adapter (`WatchlistConsensusAdapter`) for Personal Watchlist (§6.3).

Design invariants:
- Works cleanly on Standalone Core with zero private service dependencies at import or runtime.
- Primary source: public `yfinance` (`Ticker.earnings_estimate` + `_analysis._earnings_trend` endDate).
- Optional public fallback: `FMP` (`get_analyst_estimates(..., period="annual")`) when `FMP_API_KEY` is configured
  (fills both full misses and single-year misses matched by actual fiscal year end date).
- Everflow researchdata-bridge consensus enrichment is deferred to a later batch (no dead branch here).
- Cross-process Redis 8-slot semaphore and singleflight dedup (§6.2), with automatic fallback to
  in-process concurrency bounds when Redis is unavailable.
- Explicit share-unit & FX verification: never treats `estimate_currency == quote_currency` as proof
  that EPS is reported per ADR unit, and never silently assumes 1:1 on ADRs.
- Distinguishes all 7 valuation cell states:
  `ok`, `loss`, `no_coverage`, `unit_unverified`, `stale`, `rate_limited`, `unlisted`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

CONSENSUS_CACHE_TTL_SECONDS = 12 * 3600  # 12 hours (§6.2)
CONSENSUS_STALE_TTL_SECONDS = 7 * 24 * 3600  # 7 days stale fallback window
CONSENSUS_FETCH_TIMEOUT_SECONDS = 8.0
FX_CACHE_TTL_SECONDS = 12 * 3600
FX_STALE_TTL_SECONDS = 7 * 24 * 3600
MAX_CONSENSUS_CONCURRENCY = 8

_REDIS_SEM_KEY_PREFIX = "dalton:watchlist:consensus:sem:slot:"
_REDIS_SEM_LEASE_MS = 15_000
_REDIS_SEM_WAIT_TIMEOUT_SECONDS = 10.0
_REDIS_SEM_POLL_SECONDS = 0.05

_REDIS_INFLIGHT_LOCK_TTL_MS = 15_000
_REDIS_INFLIGHT_WAIT_SECONDS = 9.0
_REDIS_INFLIGHT_POLL_SECONDS = 0.05

# 52/53-week fiscal calendars can shift fiscal year end date by up to ~10 days year over year.
_FISCAL_YEAR_MATCH_TOLERANCE_DAYS = 15

VALUATION_STATUSES: frozenset[str] = frozenset(
    {
        "ok",
        "loss",
        "no_coverage",
        "unit_unverified",
        "stale",
        "rate_limited",
        "unlisted",
    }
)


@dataclass(frozen=True)
class VerifiedAdrRatio:
    """Verified ADR conversion ratio with filing source and effective date."""

    shares_per_adr: float
    underlying_currency: str
    source: str
    effective_date: str


# Curated verified ADR ratios with issuer filing source and effective date.
# Per-company row `aliases.adr_ratio` or `extra.adr_ratio` overrides this table when present.
VERIFIED_ADR_SHARES_PER_UNIT: dict[str, VerifiedAdrRatio] = {
    "BABA": VerifiedAdrRatio(8.0, "CNY", "Alibaba Group Form 20-F (8-for-1 share split)", "2019-07-30"),
    "JD": VerifiedAdrRatio(2.0, "CNY", "JD.com Form 20-F", "2014-05-22"),
    "PDD": VerifiedAdrRatio(4.0, "CNY", "PDD Holdings Form 20-F", "2018-07-26"),
    "BIDU": VerifiedAdrRatio(8.0, "CNY", "Baidu Form 20-F (80-for-1 share subdivision)", "2021-03-01"),
    "NTES": VerifiedAdrRatio(5.0, "CNY", "NetEase Form 20-F (ADS ratio change)", "2020-10-02"),
    "TCOM": VerifiedAdrRatio(1.0, "CNY", "Trip.com Group Form 20-F (1:1 ordinary share)", "2021-03-18"),
    "NIO": VerifiedAdrRatio(1.0, "CNY", "NIO Inc. Form 20-F", "2018-09-12"),
    "XPEV": VerifiedAdrRatio(2.0, "CNY", "XPeng Inc. Form 20-F", "2020-08-27"),
    "LI": VerifiedAdrRatio(2.0, "CNY", "Li Auto Inc. Form 20-F", "2020-07-30"),
    "BEKE": VerifiedAdrRatio(3.0, "CNY", "KE Holdings Inc. Form 20-F", "2020-08-13"),
    "BILI": VerifiedAdrRatio(1.0, "CNY", "Bilibili Inc. Form 20-F", "2018-03-28"),
    "TME": VerifiedAdrRatio(2.0, "CNY", "Tencent Music Entertainment Form 20-F", "2018-12-12"),
    "ZTO": VerifiedAdrRatio(1.0, "CNY", "ZTO Express Form 20-F", "2016-10-27"),
    "VIPS": VerifiedAdrRatio(0.2, "CNY", "Vipshop Holdings Form 20-F (5 ADSs = 1 Class A share)", "2014-11-03"),
    "TSM": VerifiedAdrRatio(5.0, "TWD", "TSMC Form 20-F (1 ADS = 5 common shares)", "1997-10-08"),
    "UMC": VerifiedAdrRatio(5.0, "TWD", "United Microelectronics Form 20-F", "2000-09-19"),
    "ASX": VerifiedAdrRatio(2.0, "TWD", "ASE Technology Holding Form 20-F", "2018-04-30"),
    "ASML": VerifiedAdrRatio(1.0, "EUR", "ASML Holding Form 20-F (1 NY Registry Share = 1 ordinary)", "2007-10-01"),
    "SAP": VerifiedAdrRatio(1.0, "EUR", "SAP SE Form 20-F", "1998-08-03"),
    "NVO": VerifiedAdrRatio(1.0, "DKK", "Novo Nordisk Form 20-F (2-for-1 split)", "2023-09-20"),
    "AZN": VerifiedAdrRatio(0.5, "USD", "AstraZeneca Form 20-F (2 ADSs = 1 ordinary share)", "2015-07-27"),
    "GSK": VerifiedAdrRatio(2.0, "GBP", "GSK plc Form 20-F", "2022-07-18"),
    "SNY": VerifiedAdrRatio(0.5, "EUR", "Sanofi Form 20-F (2 ADSs = 1 ordinary share)", "2002-07-01"),
    "SONY": VerifiedAdrRatio(1.0, "JPY", "Sony Group Corp Form 20-F (5-for-1 common & ADS split)", "2024-10-08"),
    "TM": VerifiedAdrRatio(10.0, "JPY", "Toyota Motor Corp Form 20-F (5-for-1 ordinary share split)", "2021-10-01"),
    "HMC": VerifiedAdrRatio(3.0, "JPY", "Honda Motor Co. Form 20-F (3-for-1 common stock split)", "2023-10-01"),
    "MUFG": VerifiedAdrRatio(1.0, "JPY", "Mitsubishi UFJ Financial Group Form 20-F", "2001-04-02"),
    "SMFG": VerifiedAdrRatio(0.6, "JPY", "Sumitomo Mitsui Financial Group Form 20-F (3-for-1 split)", "2024-10-01"),
    "MFG": VerifiedAdrRatio(0.2, "JPY", "Mizuho Financial Group Form 20-F (5 ADSs = 1 share)", "2020-10-01"),
    "SHEL": VerifiedAdrRatio(2.0, "USD", "Shell plc Form 20-F (1 ADS = 2 ordinary shares)", "2022-01-29"),
    "BP": VerifiedAdrRatio(6.0, "USD", "BP p.l.c. Form 20-F (1 ADS = 6 ordinary shares)", "1998-12-31"),
    "HSBC": VerifiedAdrRatio(5.0, "USD", "HSBC Holdings plc Form 20-F (1 ADS = 5 ordinary shares)", "1999-07-16"),
    "UL": VerifiedAdrRatio(1.0, "EUR", "Unilever PLC Form 20-F", "2020-11-30"),
    "DEO": VerifiedAdrRatio(4.0, "USD", "Diageo plc Form 20-F (1 ADS = 4 ordinary shares)", "1997-12-17"),
    "BTI": VerifiedAdrRatio(1.0, "GBP", "British American Tobacco Form 20-F", "2017-02-14"),
    "RIO": VerifiedAdrRatio(1.0, "USD", "Rio Tinto plc Form 20-F", "2010-05-03"),
    "BHP": VerifiedAdrRatio(2.0, "USD", "BHP Group Ltd Form 20-F (1 ADS = 2 ordinary shares)", "2022-01-31"),
    "VALE": VerifiedAdrRatio(1.0, "USD", "Vale S.A. Form 20-F", "2002-03-21"),
    "PBR": VerifiedAdrRatio(2.0, "USD", "Petrobras Form 20-F (1 ADS = 2 common shares)", "2008-04-28"),
    "ITUB": VerifiedAdrRatio(1.0, "BRL", "Itau Unibanco Holding Form 20-F", "2009-05-15"),
    "INFY": VerifiedAdrRatio(1.0, "INR", "Infosys Limited Form 20-F", "1999-03-11"),
    "WIT": VerifiedAdrRatio(1.0, "INR", "Wipro Limited Form 20-F", "2000-10-19"),
    "HDB": VerifiedAdrRatio(3.0, "INR", "HDFC Bank Limited Form 20-F (1 ADS = 3 equity shares)", "2019-09-20"),
    "IBN": VerifiedAdrRatio(2.0, "INR", "ICICI Bank Limited Form 20-F (1 ADS = 2 equity shares)", "2000-03-28"),
    "ARM": VerifiedAdrRatio(1.0, "USD", "Arm Holdings plc Form 20-F (1 ADS = 1 ordinary share)", "2023-09-14"),
    "SE": VerifiedAdrRatio(1.0, "USD", "Sea Limited Form 20-F (1 ADS = 1 Class A ordinary share)", "2017-10-20"),
    "BUD": VerifiedAdrRatio(1.0, "USD", "Anheuser-Busch InBev Form 20-F", "2009-09-16"),
    "ERIC": VerifiedAdrRatio(1.0, "SEK", "Telefonaktiebolaget LM Ericsson Form 20-F", "2008-06-02"),
    "NOK": VerifiedAdrRatio(1.0, "EUR", "Nokia Corporation Form 20-F", "1994-07-01"),
}

_ADR_NAME_TOKENS: tuple[str, ...] = (
    " ADR",
    " ADS",
    "DEPOSITARY",
    "DEPOSITORY",
    "SPONSORED",
)

# Minor currency subunits -> (major_currency, multiplier_to_major)
_SUBUNIT_CURRENCIES: dict[str, tuple[str, float]] = {
    "GBP_MINOR": ("GBP", 0.01),
    "GBp": ("GBP", 0.01),
    "GBX": ("GBP", 0.01),
    "ILA": ("ILS", 0.01),
    "ZAC": ("ZAR", 0.01),
}

_CONSENSUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CONSENSUS_ERROR_COOLDOWN: dict[str, tuple[float, str]] = {}
_FX_CACHE: dict[str, tuple[float, float]] = {}
_FX_ERROR_COOLDOWN: dict[str, tuple[float, str]] = {}
_INFLIGHT_CONSENSUS: dict[str, asyncio.Task[dict[str, Any]]] = {}
_INFLIGHT_FX: dict[str, asyncio.Task[float | None]] = {}
_INFLIGHT_WARMUP: dict[str, asyncio.Task[None]] = {}
_CONSENSUS_SEMAPHORE: asyncio.Semaphore | None = None
CONSENSUS_ERROR_COOLDOWN_SECONDS = 60.0


class ConsensusRateLimitError(RuntimeError):
    """Raised when an upstream consensus or FX provider responds with HTTP 429 / rate-limit."""


def _get_consensus_semaphore() -> asyncio.Semaphore:
    global _CONSENSUS_SEMAPHORE
    if _CONSENSUS_SEMAPHORE is None:
        _CONSENSUS_SEMAPHORE = asyncio.Semaphore(MAX_CONSENSUS_CONCURRENCY)
    return _CONSENSUS_SEMAPHORE


def _clear_consensus_cache_for_tests() -> None:
    _CONSENSUS_CACHE.clear()
    _CONSENSUS_ERROR_COOLDOWN.clear()
    _FX_CACHE.clear()
    _FX_ERROR_COOLDOWN.clear()
    _INFLIGHT_CONSENSUS.clear()
    _INFLIGHT_FX.clear()
    _INFLIGHT_WARMUP.clear()


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _finite_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _positive_float(val: Any) -> float | None:
    num = _finite_float(val)
    return num if num is not None and num > 0 else None


def _positive_int(val: Any) -> int | None:
    num = _finite_float(val)
    if num is None or num <= 0:
        return None
    return int(round(num))


def _parse_iso_date(raw: Any) -> date | None:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()[:10]
    if len(s) != 10:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _epoch_to_iso_date(raw: Any) -> str | None:
    """Convert a Unix epoch timestamp (e.g. yfinance ``info['lastFiscalYearEnd']``) to ``YYYY-MM-DD``."""
    num = _finite_float(raw)
    if num is None or num <= 0:
        return None
    try:
        return datetime.fromtimestamp(num, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _dates_match_fiscal_year(d1: str | None, d2: str | None) -> bool:
    """Return True if two ISO fiscal-year-end dates match (within ±15 days for 52/53-week calendars)."""
    p1 = _parse_iso_date(d1)
    p2 = _parse_iso_date(d2)
    if p1 is None or p2 is None:
        return False
    return abs((p1 - p2).days) <= _FISCAL_YEAR_MATCH_TOLERANCE_DAYS


def _normalize_currency(raw: Any) -> tuple[str | None, float]:
    """Return ``(major_iso_currency, unit_scale_to_major)``.

    Handles minor subunits such as Yahoo's ``GBp`` / ``GBX`` (pence -> 0.01 GBP)
    and normalizes ``RMB`` / ``CNH`` to ``CNY``.
    """
    if not isinstance(raw, str):
        return None, 1.0
    s = raw.strip()
    if not s:
        return None, 1.0
    if s in _SUBUNIT_CURRENCIES:
        return _SUBUNIT_CURRENCIES[s]
    upper = s.upper()
    if upper in ("RMB", "CNH"):
        return "CNY", 1.0
    if upper == "GBX":
        return "GBP", 0.01
    return upper, 1.0


def infer_quote_currency(symbol: str) -> str:
    """Infer default trading currency from exchange ticker suffix when provider omits it."""
    sym = str(symbol or "").strip().upper()
    if sym.endswith(".HK"):
        return "HKD"
    if sym.endswith((".SS", ".SZ", ".SH")):
        return "CNY"
    if sym.endswith(".T"):
        return "JPY"
    if sym.endswith(".L"):
        return "GBP"
    if sym.endswith((".DE", ".PA", ".AS", ".MI", ".MC")):
        return "EUR"
    if sym.endswith(".SW"):
        return "CHF"
    if sym.endswith(".TW") or sym.endswith(".TWO"):
        return "TWD"
    if sym.endswith(".KS") or sym.endswith(".KQ"):
        return "KRW"
    if sym.endswith(".TO"):
        return "CAD"
    if sym.endswith(".AX"):
        return "AUD"
    return "USD"


def _is_us_listed_symbol(symbol: str) -> bool:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    # US tickers either have no exchange suffix or class share dot like BRK.B / BRK-B
    if "." not in sym:
        return True
    _, suffix = sym.rsplit(".", 1)
    return len(suffix) == 1 and suffix.isalpha()


def _extract_row_adr_ratio(coverage_row: Mapping[str, Any] | None) -> float | None:
    """Read optional verified ``adr_ratio`` / ``shares_per_adr`` from a coverage row."""
    if not isinstance(coverage_row, Mapping):
        return None
    aliases = coverage_row.get("aliases")
    if isinstance(aliases, Mapping):
        val = _positive_float(aliases.get("adr_ratio") or aliases.get("shares_per_adr"))
        if val is not None:
            return val
    extra = coverage_row.get("extra")
    if isinstance(extra, Mapping):
        val = _positive_float(extra.get("adr_ratio") or extra.get("shares_per_adr"))
        if val is not None:
            return val
    return _positive_float(coverage_row.get("adr_ratio") or coverage_row.get("shares_per_adr"))


def _extract_explicit_last_reported_fy_end(
    coverage_row: Mapping[str, Any] | None,
) -> str | None:
    """Extract an explicitly recorded reported fiscal year end date if present on a coverage row.

    NOTE: ``earnings.fiscal_year_end`` is only a month-day (``MM-DD``) and does NOT prove
    whether the latest fiscal year has been reported yet; real reported FY end dates come
    from financial statement data (yfinance ``lastFiscalYearEnd`` or FMP annual income statements).
    """
    if not isinstance(coverage_row, Mapping):
        return None
    earnings = coverage_row.get("earnings")
    if not isinstance(earnings, Mapping):
        return None
    for key in ("last_reported_fy_end", "last_fiscal_year_end"):
        parsed = _parse_iso_date(earnings.get(key))
        if parsed is not None:
            return parsed.isoformat()
    return None


def _is_rate_limit_exception(
    exc: BaseException,
    *,
    _seen: set[int] | None = None,
) -> bool:
    """Detect HTTP 429 / upstream rate-limit strictly by exception type or status_code == 429."""
    seen = _seen if _seen is not None else set()
    if id(exc) in seen:
        return False
    seen.add(id(exc))

    if isinstance(exc, ConsensusRateLimitError):
        return True

    try:
        from src.data_client.fmp.fmp_client import FmpQuotaExhausted

        if isinstance(exc, FmpQuotaExhausted):
            return True
    except Exception:
        pass

    try:
        from yfinance.exceptions import YFRateLimitError  # type: ignore

        if isinstance(exc, YFRateLimitError):
            return True
    except Exception:
        pass

    if getattr(exc, "status_code", None) == 429:
        return True

    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True

    for chained in (exc.__cause__, exc.__context__):
        if chained is not None and _is_rate_limit_exception(chained, _seen=seen):
            return True

    return False


def _extract_yfinance_trend_meta(ticker_obj: Any) -> tuple[dict[str, str], str | None]:
    """Extract period -> ``endDate`` map and optional ``earningsCurrency`` from yfinance internals."""
    try:
        analysis = getattr(ticker_obj, "_analysis", None)
        trend = getattr(analysis, "_earnings_trend", None)
    except Exception:
        return {}, None

    end_dates: dict[str, str] = {}
    earnings_currency: str | None = None

    if isinstance(trend, dict):
        module = trend.get(None)
        if not isinstance(module, dict):
            module = next(
                (m for m in trend.values() if isinstance(m, dict) and "trend" in m),
                None,
            )
        if isinstance(module, dict):
            raw_ccy = module.get("earningsCurrency") or module.get("currency")
            if isinstance(raw_ccy, str) and raw_ccy.strip():
                earnings_currency = raw_ccy.strip()
            trend = module.get("trend")
        else:
            trend = None

    if isinstance(trend, list):
        for item in trend:
            if not isinstance(item, dict):
                continue
            per = item.get("period")
            end = item.get("endDate")
            if isinstance(per, str) and per and isinstance(end, str) and end.strip():
                end_dates[per] = end.strip()[:10]
            if earnings_currency is None:
                est = item.get("earningsEstimate")
                if isinstance(est, dict):
                    item_ccy = est.get("earningsCurrency") or est.get("currency")
                    if isinstance(item_ccy, str) and item_ccy.strip():
                        earnings_currency = item_ccy.strip()
                item_top_ccy = item.get("earningsCurrency")
                if isinstance(item_top_ccy, str) and item_top_ccy.strip():
                    earnings_currency = item_top_ccy.strip()
    return end_dates, earnings_currency


def _is_adr_security(
    symbol: str,
    *,
    quote_type: str | None = None,
    security_name: str | None = None,
    quote_currency: str | None = None,
    estimate_currency: str | None = None,
    row_adr_ratio: float | None = None,
    is_adr_flag: bool | None = None,
) -> bool:
    """Determine whether ``symbol`` is an ADR / cross-market depositary receipt."""
    if is_adr_flag is True:
        return True
    if row_adr_ratio is not None:
        return True
    sym = str(symbol or "").strip().upper()
    if sym in VERIFIED_ADR_SHARES_PER_UNIT:
        return True
    qt = str(quote_type or "").strip().upper()
    if "ADR" in qt or qt in ("DR", "DEPOSITARY_RECEIPT"):
        return True
    name_upper = f" {str(security_name or '').strip().upper()}"
    if any(tok in name_upper for tok in _ADR_NAME_TOKENS):
        return True
    # US-listed security reporting in a non-USD currency is treated as cross-market/ADR
    # unless explicitly verified.
    if (
        _is_us_listed_symbol(sym)
        and quote_currency == "USD"
        and estimate_currency is not None
        and estimate_currency != "USD"
    ):
        return True
    return False


@dataclass(frozen=True)
class RawConsensusRecord:
    """Provider-neutral raw annual FY1/FY2 consensus snapshot before valuation math."""

    symbol: str
    fy1_eps: float | None = None
    fy2_eps: float | None = None
    fy1_end: str | None = None
    fy2_end: str | None = None
    estimate_currency: str | None = None
    quote_currency: str | None = None
    analyst_count: int | None = None
    provider: str | None = None
    as_of: str | None = None
    quote_type: str | None = None
    security_name: str | None = None
    eps_per_quote_unit_confirmed: bool = False
    is_adr: bool | None = None
    last_reported_fy_end: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RawConsensusRecord:
        return cls(
            symbol=str(data.get("symbol") or "").strip().upper(),
            fy1_eps=_finite_float(data.get("fy1_eps")),
            fy2_eps=_finite_float(data.get("fy2_eps")),
            fy1_end=str(data["fy1_end"])[:10] if data.get("fy1_end") else None,
            fy2_end=str(data["fy2_end"])[:10] if data.get("fy2_end") else None,
            estimate_currency=str(data["estimate_currency"]).strip()
            if data.get("estimate_currency")
            else None,
            quote_currency=str(data["quote_currency"]).strip()
            if data.get("quote_currency")
            else None,
            analyst_count=_positive_int(data.get("analyst_count")),
            provider=str(data["provider"]).strip() if data.get("provider") else None,
            as_of=str(data["as_of"]) if data.get("as_of") else None,
            quote_type=str(data["quote_type"]).strip() if data.get("quote_type") else None,
            security_name=str(data["security_name"]).strip()
            if data.get("security_name")
            else None,
            eps_per_quote_unit_confirmed=bool(data.get("eps_per_quote_unit_confirmed", False)),
            is_adr=data.get("is_adr") if isinstance(data.get("is_adr"), bool) else None,
            last_reported_fy_end=str(data["last_reported_fy_end"])[:10]
            if data.get("last_reported_fy_end")
            else None,
        )


def _resolve_adr_conversion(
    symbol: str,
    *,
    record: RawConsensusRecord,
    est_ccy: str | None,
    q_ccy: str | None,
    row_adr_ratio: float | None,
) -> tuple[bool, float | None, bool]:
    """Resolve ``(unit_verified, shares_per_quote_unit, needs_fx)`` for an ADR security.

    Strict share-unit rule (§6.3):
    1. Provider explicitly attests ``eps_per_quote_unit_confirmed=True`` (EPS is per ADR)
       with ``est_ccy == q_ccy`` -> 1:1, no FX needed.
    2. Coverage row has explicit ``aliases.adr_ratio`` / ``extra.adr_ratio`` -> apply ratio
       (+ FX if ``est_ccy != q_ccy``).
    3. Curated ``VERIFIED_ADR_SHARES_PER_UNIT`` has a verified record for ``symbol`` AND
       either ``shares_per_adr == 1.0`` or ``est_ccy == entry.underlying_currency`` -> apply
       verified ratio (+ FX if ``est_ccy != q_ccy``).
    4. Otherwise -> ``(False, None, False)`` (``unit_unverified``, displays ``—``).
    """
    sym = str(symbol or "").strip().upper()
    if record.eps_per_quote_unit_confirmed and est_ccy is not None and est_ccy == q_ccy:
        return True, 1.0, False

    if row_adr_ratio is not None:
        needs_fx = bool(est_ccy and q_ccy and est_ccy != q_ccy)
        return True, float(row_adr_ratio), needs_fx

    entry = VERIFIED_ADR_SHARES_PER_UNIT.get(sym)
    if entry is not None:
        if entry.shares_per_adr == 1.0 or (est_ccy is not None and est_ccy == entry.underlying_currency):
            needs_fx = bool(est_ccy and q_ccy and est_ccy != q_ccy)
            return True, float(entry.shares_per_adr), needs_fx

    return False, None, False


def _determine_fx_requirement(
    record: RawConsensusRecord | None,
    *,
    symbol: str,
    quote_currency_override: str | None = None,
    coverage_row: Mapping[str, Any] | None = None,
) -> tuple[bool, str | None, str | None]:
    """Return ``(needs_fx, from_ccy, to_ccy)`` after checking missing EPS and share-unit rules first."""
    if record is None or (record.fy1_eps is None and record.fy2_eps is None):
        return False, None, None

    sym = str(symbol or "").strip().upper()
    raw_q_ccy = quote_currency_override or record.quote_currency or infer_quote_currency(sym)
    q_ccy, _ = _normalize_currency(raw_q_ccy)
    raw_est_ccy = record.estimate_currency or raw_q_ccy
    est_ccy, _ = _normalize_currency(raw_est_ccy)

    if not est_ccy or not q_ccy or est_ccy == q_ccy:
        return False, est_ccy, q_ccy

    row_adr_ratio = _extract_row_adr_ratio(coverage_row)
    is_adr = _is_adr_security(
        sym,
        quote_type=record.quote_type,
        security_name=record.security_name,
        quote_currency=q_ccy,
        estimate_currency=est_ccy,
        row_adr_ratio=row_adr_ratio,
        is_adr_flag=record.is_adr,
    )
    if not is_adr:
        return True, est_ccy, q_ccy

    verified, _, needs_fx = _resolve_adr_conversion(
        sym,
        record=record,
        est_ccy=est_ccy,
        q_ccy=q_ccy,
        row_adr_ratio=row_adr_ratio,
    )
    return (verified and needs_fx), est_ccy, q_ccy


def _consensus_cache_key(symbol: str, provider_tag: str = "public") -> str:
    sym = str(symbol or "").strip().upper()
    prov = str(provider_tag or "public").strip().lower()
    return f"dalton:watchlist:consensus:v1:{prov}:{sym}"


def _fx_cache_key(from_ccy: str, to_ccy: str) -> str:
    return f"dalton:watchlist:fx:v1:{from_ccy.upper()}:{to_ccy.upper()}"


async def _get_redis_cache_client() -> Any:
    try:
        from src.utils.cache.redis_cache import get_cache_client

        client = get_cache_client()
        if client is not None and getattr(client, "enabled", False):
            return client
    except Exception:
        pass
    return None


@asynccontextmanager
async def _acquire_upstream_slot() -> AsyncIterator[None]:
    """Acquire a cross-process Redis semaphore slot (8-way instance-wide, §6.2) with local fallback."""
    local_sem = _get_consensus_semaphore()
    async with local_sem:
        redis_client = await _get_redis_cache_client()
        acquire_lock = getattr(redis_client, "acquire_lock", None) if redis_client is not None else None
        release_lock = getattr(redis_client, "release_lock", None) if redis_client is not None else None
        if not callable(acquire_lock) or not callable(release_lock):
            yield
            return

        token = uuid.uuid4().hex
        acquired_slot_key: str | None = None
        deadline = time.monotonic() + _REDIS_SEM_WAIT_TIMEOUT_SECONDS
        offset = random.randrange(MAX_CONSENSUS_CONCURRENCY)

        while True:
            redis_unavailable = False
            for idx in range(MAX_CONSENSUS_CONCURRENCY):
                slot_idx = (offset + idx) % MAX_CONSENSUS_CONCURRENCY
                slot_key = f"{_REDIS_SEM_KEY_PREFIX}{slot_idx}"
                try:
                    ok = await acquire_lock(slot_key, token, _REDIS_SEM_LEASE_MS)
                except Exception:
                    ok = None
                if ok is True:
                    acquired_slot_key = slot_key
                    break
                if ok is None:
                    # Redis unavailable or errored -> degrade gracefully to local semaphore
                    redis_unavailable = True
                    break
            if acquired_slot_key is not None or redis_unavailable:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for cross-process consensus semaphore slot")
            await asyncio.sleep(_REDIS_SEM_POLL_SECONDS)

        try:
            yield
        finally:
            if acquired_slot_key is not None:
                try:
                    await release_lock(acquired_slot_key, token)
                except Exception:
                    pass


def _is_record_rolled_past_fy(
    record_dict: Mapping[str, Any],
    *,
    today: date,
    last_reported_fy_end: str | None = None,
) -> bool:
    """Return True if a cached record's FY1 end date has rolled past ``today`` or ``last_reported_fy_end``."""
    fy1_end_str = str(record_dict.get("fy1_end") or "").strip()[:10]
    fy1_dt = _parse_iso_date(fy1_end_str)
    if fy1_dt is None:
        return False
    if fy1_dt < today:
        return True
    cutoff = _parse_iso_date(last_reported_fy_end or record_dict.get("last_reported_fy_end"))
    if cutoff is not None and fy1_dt <= cutoff:
        return True
    return False


async def _read_consensus_cache(
    symbol: str,
    *,
    provider_tag: str = "public",
    today: date | None = None,
    last_reported_fy_end: str | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(cached_record_dict, is_fresh)`` from L3 memory/Redis cache."""
    key = _consensus_cache_key(symbol, provider_tag)
    now_wall = time.time()
    ref_day = today or _utc_today()

    def _classify(fetched_at: float, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        age = now_wall - fetched_at
        if age >= CONSENSUS_STALE_TTL_SECONDS:
            return None, False
        rolled = _is_record_rolled_past_fy(
            payload, today=ref_day, last_reported_fy_end=last_reported_fy_end
        )
        is_fresh = (age < CONSENSUS_CACHE_TTL_SECONDS) and (not rolled)
        return payload, is_fresh

    mem = _CONSENSUS_CACHE.get(key)
    if mem is not None:
        fetched_at, payload = mem
        res, fresh = _classify(fetched_at, payload)
        if res is not None and fresh:
            return res, True
        # When memory cache is stale or rolled past FY, check Redis in case another worker refreshed it
        redis_client = await _get_redis_cache_client()
        if redis_client is not None:
            try:
                raw = await redis_client.get(key)
                parsed: Any = json.loads(raw) if isinstance(raw, str) and raw else raw
                if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict):
                    r_fetched = float(parsed.get("fetched_at") or 0.0)
                    r_data = dict(parsed["data"])
                    if r_fetched > fetched_at:
                        _CONSENSUS_CACHE[key] = (r_fetched, r_data)
                        return _classify(r_fetched, r_data)
            except Exception:
                pass
        if res is not None:
            return res, False

    redis_client = await _get_redis_cache_client()
    if redis_client is not None:
        try:
            raw = await redis_client.get(key)
            parsed = json.loads(raw) if isinstance(raw, str) and raw else raw
            if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict):
                fetched_at = float(parsed.get("fetched_at") or 0.0)
                data = dict(parsed["data"])
                _CONSENSUS_CACHE[key] = (fetched_at, data)
                return _classify(fetched_at, data)
        except Exception:
            pass
    return None, False


async def _write_consensus_cache(
    symbol: str,
    record_dict: Mapping[str, Any],
    *,
    provider_tag: str = "public",
) -> None:
    key = _consensus_cache_key(symbol, provider_tag)
    now_wall = time.time()
    data = dict(record_dict)
    _CONSENSUS_CACHE[key] = (now_wall, data)
    redis_client = await _get_redis_cache_client()
    if redis_client is not None:
        try:
            await redis_client.set(
                key,
                json.dumps({"fetched_at": now_wall, "data": data}, ensure_ascii=False, default=str),
                ttl=CONSENSUS_STALE_TTL_SECONDS,
            )
        except Exception:
            pass


async def _read_fx_cache(from_ccy: str, to_ccy: str) -> tuple[float | None, bool]:
    key = _fx_cache_key(from_ccy, to_ccy)
    now_wall = time.time()
    mem = _FX_CACHE.get(key)
    if mem is not None:
        fetched_at, rate = mem
        age = now_wall - fetched_at
        if age < FX_CACHE_TTL_SECONDS:
            return rate, True
        if age < FX_STALE_TTL_SECONDS:
            # Check Redis in case another worker already refreshed this pair
            redis_client = await _get_redis_cache_client()
            if redis_client is not None:
                try:
                    raw = await redis_client.get(key)
                    parsed: Any = json.loads(raw) if isinstance(raw, str) and raw else raw
                    if isinstance(parsed, dict):
                        r_rate = _positive_float(parsed.get("rate"))
                        r_fetched = float(parsed.get("fetched_at") or 0.0)
                        if r_rate is not None and r_fetched > fetched_at:
                            _FX_CACHE[key] = (r_fetched, r_rate)
                            r_age = now_wall - r_fetched
                            if r_age < FX_CACHE_TTL_SECONDS:
                                return r_rate, True
                            if r_age < FX_STALE_TTL_SECONDS:
                                return r_rate, False
                except Exception:
                    pass
            return rate, False

    redis_client = await _get_redis_cache_client()
    if redis_client is not None:
        try:
            raw = await redis_client.get(key)
            parsed = json.loads(raw) if isinstance(raw, str) and raw else raw
            if isinstance(parsed, dict):
                rate = _positive_float(parsed.get("rate"))
                fetched_at = float(parsed.get("fetched_at") or 0.0)
                if rate is not None:
                    _FX_CACHE[key] = (fetched_at, rate)
                    age = now_wall - fetched_at
                    if age < FX_CACHE_TTL_SECONDS:
                        return rate, True
                    if age < FX_STALE_TTL_SECONDS:
                        return rate, False
        except Exception:
            pass
    return None, False


async def _write_fx_cache(from_ccy: str, to_ccy: str, rate: float) -> None:
    key = _fx_cache_key(from_ccy, to_ccy)
    now_wall = time.time()
    _FX_CACHE[key] = (now_wall, rate)
    redis_client = await _get_redis_cache_client()
    if redis_client is not None:
        try:
            await redis_client.set(
                key,
                json.dumps({"fetched_at": now_wall, "rate": rate}),
                ttl=FX_STALE_TTL_SECONDS,
            )
        except Exception:
            pass


def _fetch_yfinance_fx_sync(from_ccy: str, to_ccy: str) -> float | None:
    import yfinance as yf  # type: ignore

    for pair, invert in (
        (f"{from_ccy}{to_ccy}=X", False),
        (f"{to_ccy}{from_ccy}=X", True),
    ):
        try:
            t = yf.Ticker(pair)
            fast = getattr(t, "fast_info", None)
            px = None
            if fast is not None:
                px = _positive_float(
                    fast.get("lastPrice")
                    if isinstance(fast, Mapping)
                    else getattr(fast, "last_price", None)
                )
            if px is None:
                hist = t.history(period="5d", auto_adjust=False)
                if hist is not None and not getattr(hist, "empty", True) and "Close" in hist.columns:
                    closes = hist["Close"].dropna()
                    if len(closes) > 0:
                        px = _positive_float(closes.iloc[-1])
            if px is not None:
                return (1.0 / px) if invert else px
        except Exception as exc:
            if _is_rate_limit_exception(exc):
                raise ConsensusRateLimitError(str(exc)) from exc
            continue
    return None


def _fetch_yfinance_consensus_sync(symbol: str) -> RawConsensusRecord | None:
    """Read FY1 (`0y`) and FY2 (`+1y`) consensus EPS and actual fiscal year ends via `yfinance`."""
    import yfinance as yf  # type: ignore

    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    try:
        t = yf.Ticker(sym)
        df = getattr(t, "earnings_estimate", None)
    except Exception as exc:
        if _is_rate_limit_exception(exc):
            raise ConsensusRateLimitError(str(exc)) from exc
        raise

    end_dates, trend_earnings_ccy = _extract_yfinance_trend_meta(t)
    rows: dict[str, dict[str, Any]] = {}
    if df is not None and not getattr(df, "empty", True):
        try:
            raw_dict = df.to_dict(orient="index")
            if isinstance(raw_dict, dict):
                for k, v in raw_dict.items():
                    if isinstance(v, Mapping):
                        rows[str(k).strip()] = dict(v)
        except Exception:
            rows = {}

    row_0y = rows.get("0y") or {}
    row_1y = rows.get("+1y") or {}

    fy1_eps = _finite_float(row_0y.get("avg"))
    fy2_eps = _finite_float(row_1y.get("avg"))
    fy1_end = end_dates.get("0y") or (
        str(row_0y.get("endDate"))[:10] if row_0y.get("endDate") else None
    )
    fy2_end = end_dates.get("+1y") or (
        str(row_1y.get("endDate"))[:10] if row_1y.get("endDate") else None
    )

    analyst_count = _positive_int(row_0y.get("numberOfAnalysts")) or _positive_int(
        row_1y.get("numberOfAnalysts")
    )

    info: Mapping[str, Any] = {}
    try:
        raw_info = getattr(t, "info", None)
        if isinstance(raw_info, Mapping):
            info = raw_info
    except Exception as exc:
        if _is_rate_limit_exception(exc):
            raise ConsensusRateLimitError(str(exc)) from exc
        info = {}

    raw_quote_ccy = info.get("currency") or infer_quote_currency(sym)
    raw_fin_ccy = info.get("financialCurrency")
    raw_info_earn_ccy = info.get("earningsCurrency")
    quote_type = str(info.get("quoteType") or "").strip() or None
    sec_name = str(info.get("longName") or info.get("shortName") or "").strip() or None
    last_reported_fy_end = _epoch_to_iso_date(info.get("lastFiscalYearEnd"))

    explicit_earn_ccy = trend_earnings_ccy or (
        str(raw_info_earn_ccy).strip() if raw_info_earn_ccy else None
    )
    effective_est_ccy = explicit_earn_ccy or (
        str(raw_fin_ccy).strip() if raw_fin_ccy else str(raw_quote_ccy).strip()
    )

    # IMPORTANT (§6.3): `earningsCurrency == quote_currency` does NOT prove EPS is reported per ADR.
    # yfinance provides no share-unit attestation, so `eps_per_quote_unit_confirmed` stays False.
    return RawConsensusRecord(
        symbol=sym,
        fy1_eps=fy1_eps,
        fy2_eps=fy2_eps,
        fy1_end=fy1_end,
        fy2_end=fy2_end,
        estimate_currency=effective_est_ccy or None,
        quote_currency=str(raw_quote_ccy).strip() if raw_quote_ccy else None,
        analyst_count=analyst_count if (fy1_eps is not None or fy2_eps is not None) else None,
        provider="yfinance",
        as_of=datetime.now(timezone.utc).isoformat(),
        quote_type=quote_type,
        security_name=sec_name,
        eps_per_quote_unit_confirmed=False,
        last_reported_fy_end=last_reported_fy_end,
    )


def _fmp_row_eps(row: Mapping[str, Any] | None) -> float | None:
    if not isinstance(row, Mapping):
        return None
    raw = row.get("epsAvg") if "epsAvg" in row else row.get("estimatedEpsAvg")
    return _finite_float(raw)


def _fmp_row_analysts(row: Mapping[str, Any] | None) -> int | None:
    if not isinstance(row, Mapping):
        return None
    raw = (
        row.get("numAnalystsEps")
        if "numAnalystsEps" in row
        else row.get("numberAnalystsEstimatedEps")
    )
    return _positive_int(raw)


def select_fmp_fy1_fy2_estimates(
    annual_estimates: list[Mapping[str, Any]],
    *,
    today: date | None = None,
    last_reported_fy_end: str | None = None,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """Select FY1 and FY2 annual FMP estimates by actual fiscal year end date (§6.3).

    Determines FY1/FY2 strictly by fiscal year end date BEFORE checking whether EPS is present,
    so a missing EPS in FY1 never promotes FY2 into FY1.
    Filters by ``date >= today`` (and strictly after ``last_reported_fy_end`` when known).
    """
    ref_day = (today or _utc_today()).isoformat()
    reported_cutoff_dt = _parse_iso_date(last_reported_fy_end)
    reported_cutoff = reported_cutoff_dt.isoformat() if reported_cutoff_dt is not None else None

    # Deduplicate by fiscal year end date without dropping null-EPS years; prefer non-null EPS if duplicate date.
    by_date: dict[str, Mapping[str, Any]] = {}
    for item in annual_estimates or []:
        if not isinstance(item, Mapping):
            continue
        dt_parsed = _parse_iso_date(item.get("date"))
        if dt_parsed is None:
            continue
        dt_str = dt_parsed.isoformat()
        existing = by_date.get(dt_str)
        if existing is None or (_fmp_row_eps(existing) is None and _fmp_row_eps(item) is not None):
            by_date[dt_str] = item

    candidates: list[tuple[str, Mapping[str, Any]]] = []
    for dt_str in sorted(by_date.keys()):
        if dt_str < ref_day:
            continue
        if reported_cutoff is not None and dt_str <= reported_cutoff:
            continue
        candidates.append((dt_str, by_date[dt_str]))

    if not candidates:
        return None, None
    fy1_dt, fy1_row = candidates[0]
    fy2_row = next((row for dt_str, row in candidates[1:] if dt_str > fy1_dt), None)
    return fy1_row, fy2_row


def _find_fmp_row_by_fy_end(
    annual_estimates: list[Mapping[str, Any]],
    target_fy_end: str | None,
) -> Mapping[str, Any] | None:
    """Find an FMP annual estimate row matching ``target_fy_end`` (within ±15 days)."""
    if not target_fy_end:
        return None
    best: Mapping[str, Any] | None = None
    for item in annual_estimates or []:
        if not isinstance(item, Mapping):
            continue
        dt_parsed = _parse_iso_date(item.get("date"))
        if dt_parsed is None:
            continue
        if _dates_match_fiscal_year(dt_parsed.isoformat(), target_fy_end):
            if best is None or (_fmp_row_eps(best) is None and _fmp_row_eps(item) is not None):
                best = item
    return best


def _merge_partial_consensus_record(
    primary: RawConsensusRecord,
    fallback: RawConsensusRecord,
    *,
    fallback_annual_rows: list[Mapping[str, Any]] | None = None,
) -> RawConsensusRecord:
    """Fill a single missing year (FY1 or FY2) in ``primary`` using ``fallback`` matched by fiscal year end."""
    p_ccy, _ = _normalize_currency(primary.estimate_currency)
    f_ccy, _ = _normalize_currency(fallback.estimate_currency)
    # Refuse to mix incompatible EPS currencies across FY1 and FY2
    if p_ccy and f_ccy and p_ccy != f_ccy:
        return primary

    fy1_eps = primary.fy1_eps
    fy1_end = primary.fy1_end
    fy2_eps = primary.fy2_eps
    fy2_end = primary.fy2_end
    analyst_count = primary.analyst_count
    filled_any = False

    rows = fallback_annual_rows or []

    if fy1_eps is None:
        matched_row = _find_fmp_row_by_fy_end(rows, fy1_end) if fy1_end else None
        cand_eps: float | None = None
        cand_end: str | None = None
        cand_analysts: int | None = None
        if matched_row is not None:
            cand_eps = _fmp_row_eps(matched_row)
            cand_end = str(matched_row.get("date"))[:10]
            cand_analysts = _fmp_row_analysts(matched_row)
        elif fy1_end is None:
            # No target FY1 end from primary: accept fallback FY1 only if strictly earlier than primary FY2
            if fallback.fy1_eps is not None and (
                fy2_end is None
                or fallback.fy1_end is None
                or fallback.fy1_end < fy2_end
            ):
                cand_eps = fallback.fy1_eps
                cand_end = fallback.fy1_end
                cand_analysts = fallback.analyst_count
        if cand_eps is not None:
            fy1_eps = cand_eps
            fy1_end = fy1_end or cand_end
            analyst_count = analyst_count or cand_analysts
            filled_any = True

    if fy2_eps is None:
        matched_row = _find_fmp_row_by_fy_end(rows, fy2_end) if fy2_end else None
        cand_eps = None
        cand_end = None
        cand_analysts = None
        if matched_row is not None:
            cand_eps = _fmp_row_eps(matched_row)
            cand_end = str(matched_row.get("date"))[:10]
            cand_analysts = _fmp_row_analysts(matched_row)
        elif fy2_end is None:
            # No target FY2 end from primary: pick fallback FY2 (or FY1) strictly after primary FY1
            if fallback.fy2_eps is not None and (
                fy1_end is None
                or fallback.fy2_end is None
                or fallback.fy2_end > fy1_end
            ):
                cand_eps = fallback.fy2_eps
                cand_end = fallback.fy2_end
                cand_analysts = fallback.analyst_count
            elif (
                fy1_end is not None
                and fallback.fy1_eps is not None
                and fallback.fy1_end is not None
                and fallback.fy1_end > fy1_end
            ):
                cand_eps = fallback.fy1_eps
                cand_end = fallback.fy1_end
                cand_analysts = fallback.analyst_count
        if cand_eps is not None:
            fy2_eps = cand_eps
            fy2_end = fy2_end or cand_end
            analyst_count = analyst_count or cand_analysts
            filled_any = True

    if not filled_any:
        return primary

    combined_provider = (
        f"{primary.provider}+{fallback.provider}"
        if primary.provider and fallback.provider and primary.provider != fallback.provider
        else (primary.provider or fallback.provider)
    )
    return RawConsensusRecord(
        symbol=primary.symbol,
        fy1_eps=fy1_eps,
        fy2_eps=fy2_eps,
        fy1_end=fy1_end,
        fy2_end=fy2_end,
        estimate_currency=primary.estimate_currency or fallback.estimate_currency,
        quote_currency=primary.quote_currency or fallback.quote_currency,
        analyst_count=analyst_count,
        provider=combined_provider,
        as_of=primary.as_of or fallback.as_of,
        quote_type=primary.quote_type,
        security_name=primary.security_name or fallback.security_name,
        eps_per_quote_unit_confirmed=primary.eps_per_quote_unit_confirmed and fallback.eps_per_quote_unit_confirmed,
        is_adr=primary.is_adr if primary.is_adr is not None else fallback.is_adr,
        last_reported_fy_end=primary.last_reported_fy_end or fallback.last_reported_fy_end,
    )


def _compute_valuation_from_record(
    record: RawConsensusRecord | None,
    *,
    symbol: str,
    latest_price: float | None,
    quote_currency_override: str | None = None,
    coverage_row: Mapping[str, Any] | None = None,
    fx_rate: float | None = None,
    is_private: bool = False,
    is_stale: bool = False,
    is_rate_limited: bool = False,
) -> dict[str, Any]:
    """Compute canonical valuation payload and 7-state statuses from a raw consensus record."""
    sym = str(symbol or "").strip().upper()
    row_adr_ratio = _extract_row_adr_ratio(coverage_row)

    def _empty_payload(
        status: str,
        *,
        reason: str | None = None,
        est_ccy: str | None = None,
        q_ccy: str | None = None,
        shares_per_unit: float | None = None,
        unit_verified: bool = False,
    ) -> dict[str, Any]:
        return {
            "ticker": sym,
            "fy1_eps": record.fy1_eps if record is not None else None,
            "fy2_eps": record.fy2_eps if record is not None else None,
            "fy1_end": record.fy1_end if record is not None else None,
            "fy2_end": record.fy2_end if record is not None else None,
            "estimate_currency": est_ccy,
            "quote_currency": q_ccy,
            "shares_per_quote_unit": shares_per_unit,
            "unit_verified": unit_verified,
            "analyst_count": record.analyst_count if record is not None else None,
            "provider": record.provider if record is not None else None,
            "as_of": record.as_of if record is not None else None,
            "fx_rate": fx_rate,
            "status": status,
            "pe_fy1": None,
            "pe_fy2": None,
            "eps_growth_fy2": None,
            "pe_fy1_status": status,
            "pe_fy2_status": status,
            "eps_growth_fy2_status": status,
            "status_reason": reason,
        }

    if is_private:
        return _empty_payload("unlisted", reason="未上市公司")

    if record is None:
        if is_rate_limited:
            return _empty_payload("rate_limited", reason="上游数据限流（429），请稍后重试")
        return _empty_payload("no_coverage", reason="暂无一致预期覆盖")

    raw_q_ccy = quote_currency_override or record.quote_currency or infer_quote_currency(sym)
    q_ccy, q_scale = _normalize_currency(raw_q_ccy)
    raw_est_ccy = record.estimate_currency or raw_q_ccy
    est_ccy, est_scale = _normalize_currency(raw_est_ccy)

    if record.fy1_eps is None and record.fy2_eps is None:
        if is_rate_limited:
            return _empty_payload(
                "rate_limited",
                reason="上游数据限流（429），请稍后重试",
                est_ccy=est_ccy,
                q_ccy=q_ccy,
            )
        return _empty_payload(
            "no_coverage",
            reason="暂无一致预期覆盖",
            est_ccy=est_ccy,
            q_ccy=q_ccy,
        )

    # Share-unit & ADR verification (§6.3.2)
    is_adr = _is_adr_security(
        sym,
        quote_type=record.quote_type,
        security_name=record.security_name,
        quote_currency=q_ccy,
        estimate_currency=est_ccy,
        row_adr_ratio=row_adr_ratio,
        is_adr_flag=record.is_adr,
    )

    shares_per_quote_unit: float | None = None
    unit_verified = False
    eff_fx: float | None = 1.0

    if not is_adr:
        shares_per_quote_unit = 1.0
        if est_ccy == q_ccy or est_ccy is None or q_ccy is None:
            eff_fx = 1.0
            unit_verified = True
        else:
            eff_fx = _positive_float(fx_rate)
            if eff_fx is not None:
                unit_verified = True
            else:
                if is_rate_limited:
                    return _empty_payload(
                        "rate_limited",
                        reason="即期汇率接口限流（429），请稍后重试",
                        est_ccy=est_ccy,
                        q_ccy=q_ccy,
                        shares_per_unit=1.0,
                        unit_verified=False,
                    )
                return _empty_payload(
                    "no_coverage",
                    reason=f"缺少 {est_ccy}->{q_ccy} 即期汇率",
                    est_ccy=est_ccy,
                    q_ccy=q_ccy,
                    shares_per_unit=1.0,
                    unit_verified=False,
                )
    else:
        adr_ok, adr_ratio, adr_needs_fx = _resolve_adr_conversion(
            sym,
            record=record,
            est_ccy=est_ccy,
            q_ccy=q_ccy,
            row_adr_ratio=row_adr_ratio,
        )
        if not adr_ok or adr_ratio is None:
            return _empty_payload(
                "unit_unverified",
                reason="待核对存托凭证换算比例",
                est_ccy=est_ccy,
                q_ccy=q_ccy,
                shares_per_unit=None,
                unit_verified=False,
            )
        shares_per_quote_unit = adr_ratio
        if not adr_needs_fx:
            eff_fx = 1.0
            unit_verified = True
        else:
            eff_fx = _positive_float(fx_rate)
            if eff_fx is not None:
                unit_verified = True
            else:
                if is_rate_limited:
                    return _empty_payload(
                        "rate_limited",
                        reason="即期汇率接口限流（429），请稍后重试",
                        est_ccy=est_ccy,
                        q_ccy=q_ccy,
                        shares_per_unit=shares_per_quote_unit,
                        unit_verified=False,
                    )
                return _empty_payload(
                    "unit_unverified",
                    reason=f"缺少 {est_ccy}->{q_ccy} 即期汇率，暂不折算",
                    est_ccy=est_ccy,
                    q_ccy=q_ccy,
                    shares_per_unit=shares_per_quote_unit,
                    unit_verified=False,
                )

    # Convert EPS into major quote currency per quote unit, and normalize price into major quote currency
    assert shares_per_quote_unit is not None and eff_fx is not None
    price_major = (
        float(latest_price) * q_scale
        if _positive_float(latest_price) is not None
        else None
    )

    def _calc_pe(eps_raw: float | None) -> tuple[float | None, str]:
        if eps_raw is None:
            return None, "no_coverage"
        if eps_raw <= 0:
            return None, "loss"
        if price_major is None:
            return None, "stale" if is_stale else "no_coverage"
        eps_quote_unit = eps_raw * est_scale * shares_per_quote_unit * eff_fx
        if eps_quote_unit <= 0:
            return None, "loss"
        pe = round(price_major / eps_quote_unit, 2)
        return pe, ("stale" if is_stale else "ok")

    pe_fy1, pe_fy1_status = _calc_pe(record.fy1_eps)
    pe_fy2, pe_fy2_status = _calc_pe(record.fy2_eps)

    eps_growth_fy2: float | None = None
    if record.fy1_eps is None or record.fy2_eps is None:
        eps_growth_status = "no_coverage"
    elif record.fy1_eps <= 0:
        eps_growth_status = "loss"
    else:
        eps_growth_fy2 = round(((record.fy2_eps - record.fy1_eps) / record.fy1_eps) * 100.0, 2)
        eps_growth_status = "stale" if is_stale else "ok"

    # Overall symbol valuation status
    if is_stale and (pe_fy1 is not None or pe_fy2 is not None or eps_growth_fy2 is not None):
        overall_status = "stale"
    elif pe_fy1_status == "ok" or pe_fy2_status == "ok":
        overall_status = "ok"
    elif pe_fy1_status == "loss" or pe_fy2_status == "loss":
        overall_status = "loss"
    elif is_stale:
        overall_status = "stale"
    else:
        overall_status = "no_coverage"

    return {
        "ticker": sym,
        "fy1_eps": record.fy1_eps,
        "fy2_eps": record.fy2_eps,
        "fy1_end": record.fy1_end,
        "fy2_end": record.fy2_end,
        "estimate_currency": est_ccy,
        "quote_currency": q_ccy,
        "shares_per_quote_unit": shares_per_quote_unit,
        "unit_verified": unit_verified,
        "analyst_count": record.analyst_count,
        "provider": record.provider,
        "as_of": record.as_of,
        "fx_rate": round(eff_fx, 6),
        "status": overall_status,
        "pe_fy1": pe_fy1,
        "pe_fy2": pe_fy2,
        "eps_growth_fy2": eps_growth_fy2,
        "pe_fy1_status": pe_fy1_status,
        "pe_fy2_status": pe_fy2_status,
        "eps_growth_fy2_status": eps_growth_status,
        "status_reason": (
            "缓存数据已过期，正在后台刷新"
            if overall_status == "stale"
            else ("盈利为负（NM）" if overall_status == "loss" else None)
        ),
    }


class WatchlistConsensusAdapter:
    """Public consensus valuation adapter (`yfinance` primary, optional `FMP` fallback)."""

    def __init__(
        self,
        *,
        yfinance_fetcher: Any = None,
        fmp_client: Any = None,
        fx_fetcher: Any = None,
        today_fn: Callable[[], date] | None = None,
    ) -> None:
        self._yfinance_fetcher = yfinance_fetcher
        self._fmp_client = fmp_client
        self._fx_fetcher = fx_fetcher
        self._today_fn = today_fn or _utc_today

    def _today(self) -> date:
        return self._today_fn()

    async def _fetch_from_yfinance(self, symbol: str) -> RawConsensusRecord | None:
        if self._yfinance_fetcher is not None:
            res = self._yfinance_fetcher(symbol)
            if asyncio.iscoroutine(res):
                return await res
            return res
        return await asyncio.wait_for(
            asyncio.to_thread(_fetch_yfinance_consensus_sync, symbol),
            timeout=CONSENSUS_FETCH_TIMEOUT_SECONDS,
        )

    async def _fetch_from_fmp_with_rows(
        self,
        symbol: str,
        *,
        last_reported_fy_end: str | None = None,
    ) -> tuple[RawConsensusRecord | None, list[Mapping[str, Any]]]:
        """Fetch annual estimates from FMP using a lifecycle-managed client (closed on success & error)."""
        injected_client = self._fmp_client
        owned_client: Any = None
        if injected_client is not None:
            client = injected_client
        else:
            api_key = os.environ.get("FMP_API_KEY", "").strip()
            if not api_key:
                return None, []
            try:
                from src.data_client.fmp.fmp_client import FMPClient

                owned_client = FMPClient(api_key=api_key)
                client = owned_client
            except Exception:
                return None, []

        try:
            try:
                estimates = await client.get_analyst_estimates(symbol, period="annual", limit=6)
            except Exception as exc:
                if _is_rate_limit_exception(exc):
                    raise ConsensusRateLimitError(str(exc)) from exc
                logger.debug("[WATCHLIST_CONSENSUS] FMP estimates failed for %s: %s", symbol, exc)
                return None, []

            if not isinstance(estimates, list) or not estimates:
                return None, []

            eff_reported_fy_end = last_reported_fy_end
            if eff_reported_fy_end is None:
                get_income = getattr(client, "get_income_statement", None)
                if callable(get_income):
                    try:
                        stmts = await get_income(symbol, period="annual", limit=1)
                        if isinstance(stmts, list) and stmts and isinstance(stmts[0], Mapping):
                            parsed_dt = _parse_iso_date(stmts[0].get("date"))
                            if parsed_dt is not None:
                                eff_reported_fy_end = parsed_dt.isoformat()
                    except Exception as exc:
                        if _is_rate_limit_exception(exc):
                            raise ConsensusRateLimitError(str(exc)) from exc

            fy1_row, fy2_row = select_fmp_fy1_fy2_estimates(
                estimates,
                today=self._today(),
                last_reported_fy_end=eff_reported_fy_end,
            )
            if fy1_row is None and fy2_row is None:
                return None, estimates

            fy1_eps = _fmp_row_eps(fy1_row)
            fy2_eps = _fmp_row_eps(fy2_row)
            fy1_end = str(fy1_row.get("date"))[:10] if fy1_row and fy1_row.get("date") else None
            fy2_end = str(fy2_row.get("date"))[:10] if fy2_row and fy2_row.get("date") else None
            analyst_count = _fmp_row_analysts(fy1_row) or _fmp_row_analysts(fy2_row)

            quote_ccy = infer_quote_currency(symbol)
            est_ccy: str | None = None
            sec_name: str | None = None
            is_adr_flag: bool | None = None
            get_profile = getattr(client, "get_profile", None)
            if callable(get_profile):
                try:
                    prof = await get_profile(symbol)
                    if isinstance(prof, list) and prof:
                        prof = prof[0]
                    if isinstance(prof, Mapping):
                        quote_ccy = str(prof.get("currency") or quote_ccy).strip()
                        est_ccy = str(
                            prof.get("reportedCurrency")
                            or prof.get("financialCurrency")
                            or prof.get("currency")
                            or ""
                        ).strip() or None
                        sec_name = str(prof.get("companyName") or "").strip() or None
                        if isinstance(prof.get("isAdr"), bool):
                            is_adr_flag = prof["isAdr"]
                except Exception as exc:
                    if _is_rate_limit_exception(exc):
                        raise ConsensusRateLimitError(str(exc)) from exc

            rec = RawConsensusRecord(
                symbol=str(symbol).strip().upper(),
                fy1_eps=fy1_eps,
                fy2_eps=fy2_eps,
                fy1_end=fy1_end,
                fy2_end=fy2_end,
                estimate_currency=est_ccy or quote_ccy,
                quote_currency=quote_ccy,
                analyst_count=analyst_count,
                provider="fmp",
                as_of=datetime.now(timezone.utc).isoformat(),
                security_name=sec_name,
                eps_per_quote_unit_confirmed=False,
                is_adr=is_adr_flag,
                last_reported_fy_end=eff_reported_fy_end,
            )
            return rec, estimates
        finally:
            if owned_client is not None:
                try:
                    await owned_client.close()
                except Exception:
                    pass

    async def _fetch_from_fmp(
        self,
        symbol: str,
        *,
        last_reported_fy_end: str | None = None,
    ) -> RawConsensusRecord | None:
        rec, _ = await self._fetch_from_fmp_with_rows(
            symbol, last_reported_fy_end=last_reported_fy_end
        )
        return rec

    async def fetch_spot_fx_rate(self, from_ccy: str, to_ccy: str) -> tuple[float | None, bool, bool]:
        """Return ``(rate, is_stale, is_rate_limited)`` for converting 1 ``from_ccy`` to ``to_ccy``."""
        src, _ = _normalize_currency(from_ccy)
        dst, _ = _normalize_currency(to_ccy)
        if not src or not dst or src == dst:
            return 1.0, False, False

        cached, is_fresh = await _read_fx_cache(src, dst)
        if cached is not None and is_fresh:
            return cached, False, False

        cache_key = _fx_cache_key(src, dst)
        existing = _INFLIGHT_FX.get(cache_key)
        if existing is not None and not existing.done():
            try:
                rate = await existing
                if rate is not None:
                    return rate, False, False
            except ConsensusRateLimitError:
                return (cached, True, False) if cached is not None else (None, False, True)
            except Exception:
                return (cached, True, False) if cached is not None else (None, False, False)

        async def _load_fx() -> float | None:
            redis_client = await _get_redis_cache_client()
            inflight_key = f"{cache_key}:inflight"
            lock_token = uuid.uuid4().hex
            held_inflight_lock = False

            if redis_client is not None and callable(getattr(redis_client, "acquire_lock", None)):
                try:
                    acquired = await redis_client.acquire_lock(
                        inflight_key, lock_token, _REDIS_INFLIGHT_LOCK_TTL_MS
                    )
                except Exception:
                    acquired = None
                if acquired is True:
                    held_inflight_lock = True
                elif acquired is False:
                    # Another worker is fetching this FX pair; wait for fresh cache before falling back
                    wait_deadline = time.monotonic() + _REDIS_INFLIGHT_WAIT_SECONDS
                    while time.monotonic() < wait_deadline:
                        await asyncio.sleep(_REDIS_INFLIGHT_POLL_SECONDS)
                        r_cached, r_fresh = await _read_fx_cache(src, dst)
                        if r_cached is not None and r_fresh:
                            return r_cached
                        try:
                            retry_acq = await redis_client.acquire_lock(
                                inflight_key, lock_token, _REDIS_INFLIGHT_LOCK_TTL_MS
                            )
                        except Exception:
                            retry_acq = None
                        if retry_acq is True:
                            held_inflight_lock = True
                            break
                        if retry_acq is None:
                            break

            try:
                async with _acquire_upstream_slot():
                    if self._fx_fetcher is not None:
                        res = self._fx_fetcher(src, dst)
                        rate_val = await res if asyncio.iscoroutine(res) else res
                    else:
                        rate_val = await asyncio.wait_for(
                            asyncio.to_thread(_fetch_yfinance_fx_sync, src, dst),
                            timeout=CONSENSUS_FETCH_TIMEOUT_SECONDS,
                        )
                    pos = _positive_float(rate_val)
                    if pos is not None:
                        _FX_ERROR_COOLDOWN.pop(cache_key, None)
                        await _write_fx_cache(src, dst, pos)
                    return pos
            except Exception as exc:
                if _is_rate_limit_exception(exc):
                    _FX_ERROR_COOLDOWN[cache_key] = (time.monotonic(), "rate_limited")
                    raise ConsensusRateLimitError(str(exc)) from exc
                raise
            finally:
                if held_inflight_lock and redis_client is not None:
                    try:
                        await redis_client.release_lock(inflight_key, lock_token)
                    except Exception:
                        pass

        task = asyncio.create_task(_load_fx())
        _INFLIGHT_FX[cache_key] = task

        def _cleanup(_t: asyncio.Task[Any]) -> None:
            if _INFLIGHT_FX.get(cache_key) is task:
                _INFLIGHT_FX.pop(cache_key, None)

        task.add_done_callback(_cleanup)
        try:
            rate = await task
            if rate is not None:
                return rate, False, False
            return (cached, True, False) if cached is not None else (None, False, False)
        except ConsensusRateLimitError:
            return (cached, True, False) if cached is not None else (None, False, True)
        except Exception:
            return (cached, True, False) if cached is not None else (None, False, False)

    async def _fetch_raw_consensus_singleflight(
        self,
        symbol: str,
        *,
        last_reported_fy_end: str | None = None,
    ) -> tuple[RawConsensusRecord | None, bool, bool]:
        """Return ``(record, is_stale, is_rate_limited)`` using L3 cache + cross-process singleflight."""
        sym = str(symbol or "").strip().upper()
        ref_today = self._today()
        cached_dict, is_fresh = await _read_consensus_cache(
            sym, today=ref_today, last_reported_fy_end=last_reported_fy_end
        )
        if cached_dict is not None and is_fresh:
            return RawConsensusRecord.from_dict(cached_dict), False, False

        cache_key = _consensus_cache_key(sym)
        existing = _INFLIGHT_CONSENSUS.get(cache_key)
        if existing is not None and not existing.done():
            try:
                loaded = await existing
                return RawConsensusRecord.from_dict(loaded), False, False
            except ConsensusRateLimitError:
                if cached_dict is not None:
                    return RawConsensusRecord.from_dict(cached_dict), True, False
                return None, False, True
            except Exception:
                if cached_dict is not None:
                    return RawConsensusRecord.from_dict(cached_dict), True, False
                return None, False, False

        async def _load() -> dict[str, Any]:
            redis_client = await _get_redis_cache_client()
            inflight_key = f"{cache_key}:inflight"
            lock_token = uuid.uuid4().hex
            held_inflight_lock = False

            if redis_client is not None and callable(getattr(redis_client, "acquire_lock", None)):
                try:
                    acquired = await redis_client.acquire_lock(
                        inflight_key, lock_token, _REDIS_INFLIGHT_LOCK_TTL_MS
                    )
                except Exception:
                    acquired = None
                if acquired is True:
                    held_inflight_lock = True
                elif acquired is False:
                    # Another worker is fetching this symbol; wait for its fresh write in Redis
                    wait_deadline = time.monotonic() + _REDIS_INFLIGHT_WAIT_SECONDS
                    while time.monotonic() < wait_deadline:
                        await asyncio.sleep(_REDIS_INFLIGHT_POLL_SECONDS)
                        r_cached, r_fresh = await _read_consensus_cache(
                            sym, today=ref_today, last_reported_fy_end=last_reported_fy_end
                        )
                        if r_cached is not None and r_fresh:
                            return r_cached
                        try:
                            retry_acq = await redis_client.acquire_lock(
                                inflight_key, lock_token, _REDIS_INFLIGHT_LOCK_TTL_MS
                            )
                        except Exception:
                            retry_acq = None
                        if retry_acq is True:
                            held_inflight_lock = True
                            break
                        if retry_acq is None:
                            break

            try:
                async with _acquire_upstream_slot():
                    rate_limited = False
                    had_transient_error = False
                    rec: RawConsensusRecord | None = None

                    # 1. Public primary: yfinance
                    try:
                        rec = await self._fetch_from_yfinance(sym)
                    except Exception as exc:
                        if _is_rate_limit_exception(exc):
                            rate_limited = True
                        else:
                            had_transient_error = True
                            logger.debug("[WATCHLIST_CONSENSUS] yfinance failed for %s: %s", sym, exc)

                    # 2. Optional public fallback: FMP (when yfinance missed either or both years)
                    if rec is None or rec.fy1_eps is None or rec.fy2_eps is None:
                        eff_reported = last_reported_fy_end or (
                            rec.last_reported_fy_end if rec is not None else None
                        )
                        try:
                            fmp_rec, fmp_rows = await self._fetch_from_fmp_with_rows(
                                sym, last_reported_fy_end=eff_reported
                            )
                            if fmp_rec is not None:
                                if rec is None or (rec.fy1_eps is None and rec.fy2_eps is None):
                                    if fmp_rec.fy1_eps is not None or fmp_rec.fy2_eps is not None:
                                        rec = fmp_rec
                                else:
                                    rec = _merge_partial_consensus_record(
                                        rec, fmp_rec, fallback_annual_rows=fmp_rows
                                    )
                        except ConsensusRateLimitError:
                            rate_limited = True

                    if rec is None and rate_limited:
                        _CONSENSUS_ERROR_COOLDOWN[sym] = (time.monotonic(), "rate_limited")
                        raise ConsensusRateLimitError(f"Rate limited fetching consensus for {sym}")

                    if rec is None:
                        if had_transient_error:
                            _CONSENSUS_ERROR_COOLDOWN[sym] = (time.monotonic(), "transient_error")
                            raise RuntimeError(f"Transient consensus fetch failure for {sym}")
                        rec = RawConsensusRecord(
                            symbol=sym,
                            quote_currency=infer_quote_currency(sym),
                            provider="yfinance",
                            as_of=datetime.now(timezone.utc).isoformat(),
                        )
                    _CONSENSUS_ERROR_COOLDOWN.pop(sym, None)
                    rec_dict = rec.to_dict()
                    if not rate_limited:
                        await _write_consensus_cache(sym, rec_dict)
                    return rec_dict
            finally:
                if held_inflight_lock and redis_client is not None:
                    try:
                        await redis_client.release_lock(inflight_key, lock_token)
                    except Exception:
                        pass

        task = asyncio.create_task(_load())
        _INFLIGHT_CONSENSUS[cache_key] = task

        def _cleanup(_t: asyncio.Task[Any]) -> None:
            if _INFLIGHT_CONSENSUS.get(cache_key) is task:
                _INFLIGHT_CONSENSUS.pop(cache_key, None)

        task.add_done_callback(_cleanup)
        try:
            loaded = await task
            return RawConsensusRecord.from_dict(loaded), False, False
        except ConsensusRateLimitError:
            if cached_dict is not None:
                return RawConsensusRecord.from_dict(cached_dict), True, False
            return None, False, True
        except Exception:
            if cached_dict is not None:
                return RawConsensusRecord.from_dict(cached_dict), True, False
            return None, False, False

    def schedule_warmup(
        self,
        symbol: str,
        *,
        quote_currency: str | None = None,
        coverage_row: Mapping[str, Any] | None = None,
        last_reported_fy_end: str | None = None,
    ) -> None:
        """Kick off bounded background consensus + conditional FX warm-up without blocking HTTP response."""
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        existing = _INFLIGHT_WARMUP.get(sym)
        if existing is not None and not existing.done():
            return

        eff_reported = last_reported_fy_end or _extract_explicit_last_reported_fy_end(coverage_row)

        async def _bg() -> None:
            try:
                rec, _, _ = await self._fetch_raw_consensus_singleflight(
                    sym, last_reported_fy_end=eff_reported
                )
                needs_fx, est_ccy, q_ccy = _determine_fx_requirement(
                    rec,
                    symbol=sym,
                    quote_currency_override=quote_currency,
                    coverage_row=coverage_row,
                )
                if needs_fx and est_ccy and q_ccy:
                    await self.fetch_spot_fx_rate(est_ccy, q_ccy)
            except Exception as exc:
                logger.debug("[WATCHLIST_CONSENSUS] background warmup failed for %s: %s", sym, exc)

        task = asyncio.create_task(_bg())
        _INFLIGHT_WARMUP[sym] = task

        def _cleanup(_t: asyncio.Task[Any]) -> None:
            if _INFLIGHT_WARMUP.get(sym) is task:
                _INFLIGHT_WARMUP.pop(sym, None)

        task.add_done_callback(_cleanup)

    async def get_cached_valuation(
        self,
        symbol: str,
        *,
        latest_price: float | None,
        quote_currency: str | None = None,
        coverage_row: Mapping[str, Any] | None = None,
        is_private: bool = False,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Return ``(valuation_dict, needs_warmup)`` without blocking on cold upstream fetches."""
        sym = str(symbol or "").strip().upper()
        if is_private:
            return (
                _compute_valuation_from_record(
                    None, symbol=sym, latest_price=None, is_private=True
                ),
                False,
            )

        explicit_reported = _extract_explicit_last_reported_fy_end(coverage_row)
        cached_dict, is_fresh = await _read_consensus_cache(
            sym, today=self._today(), last_reported_fy_end=explicit_reported
        )
        if cached_dict is None:
            cooldown = _CONSENSUS_ERROR_COOLDOWN.get(sym)
            if cooldown is not None and (time.monotonic() - cooldown[0]) < CONSENSUS_ERROR_COOLDOWN_SECONDS:
                err_kind = cooldown[1]
                return (
                    _compute_valuation_from_record(
                        None,
                        symbol=sym,
                        latest_price=latest_price,
                        quote_currency_override=quote_currency,
                        coverage_row=coverage_row,
                        is_private=False,
                        is_rate_limited=(err_kind == "rate_limited"),
                    ),
                    False,
                )
            return None, True

        rec = RawConsensusRecord.from_dict(cached_dict)

        # Check missing EPS & ADR share-unit verification BEFORE checking FX cache!
        # Missing EPS -> `no_coverage` immediately; unverified ADR -> `unit_unverified` immediately.
        needs_fx, est_ccy, q_ccy = _determine_fx_requirement(
            rec,
            symbol=sym,
            quote_currency_override=quote_currency,
            coverage_row=coverage_row,
        )

        fx_rate: float | None = 1.0
        fx_stale = False
        fx_rate_limited = False
        if needs_fx and est_ccy and q_ccy:
            cached_fx, fx_fresh = await _read_fx_cache(est_ccy, q_ccy)
            if cached_fx is None:
                fx_key = _fx_cache_key(est_ccy, q_ccy)
                fx_cd = _FX_ERROR_COOLDOWN.get(fx_key)
                if fx_cd is not None and (time.monotonic() - fx_cd[0]) < CONSENSUS_ERROR_COOLDOWN_SECONDS:
                    fx_rate = None
                    fx_rate_limited = fx_cd[1] == "rate_limited"
                else:
                    return None, True
            else:
                fx_rate = cached_fx
                fx_stale = not fx_fresh

        val = _compute_valuation_from_record(
            rec,
            symbol=sym,
            latest_price=latest_price,
            quote_currency_override=quote_currency,
            coverage_row=coverage_row,
            fx_rate=fx_rate,
            is_private=False,
            is_stale=(not is_fresh) or fx_stale,
            is_rate_limited=fx_rate_limited,
        )
        return val, ((not is_fresh) or fx_stale) and (not fx_rate_limited)

    async def get_valuation(
        self,
        symbol: str,
        *,
        latest_price: float | None,
        quote_currency: str | None = None,
        coverage_row: Mapping[str, Any] | None = None,
        is_private: bool = False,
    ) -> dict[str, Any]:
        """Fetch consensus (with cache + singleflight + optional FMP fallback) and compute valuation."""
        sym = str(symbol or "").strip().upper()
        if is_private:
            return _compute_valuation_from_record(
                None, symbol=sym, latest_price=None, is_private=True
            )

        last_reported_fy_end = _extract_explicit_last_reported_fy_end(coverage_row)
        rec, rec_stale, rec_rate_limited = await self._fetch_raw_consensus_singleflight(
            sym, last_reported_fy_end=last_reported_fy_end
        )

        if rec is None:
            return _compute_valuation_from_record(
                None,
                symbol=sym,
                latest_price=latest_price,
                quote_currency_override=quote_currency,
                coverage_row=coverage_row,
                is_private=False,
                is_rate_limited=rec_rate_limited,
            )

        fx_rate: float | None = 1.0
        fx_stale = False
        fx_rate_limited = False
        needs_fx, est_ccy, q_ccy = _determine_fx_requirement(
            rec,
            symbol=sym,
            quote_currency_override=quote_currency,
            coverage_row=coverage_row,
        )
        if needs_fx and est_ccy and q_ccy:
            fx_rate, fx_stale, fx_rate_limited = await self.fetch_spot_fx_rate(est_ccy, q_ccy)

        return _compute_valuation_from_record(
            rec,
            symbol=sym,
            latest_price=latest_price,
            quote_currency_override=quote_currency,
            coverage_row=coverage_row,
            fx_rate=fx_rate,
            is_private=False,
            is_stale=rec_stale or fx_stale,
            is_rate_limited=rec_rate_limited or fx_rate_limited,
        )


__all__ = [
    "CONSENSUS_CACHE_TTL_SECONDS",
    "CONSENSUS_STALE_TTL_SECONDS",
    "ConsensusRateLimitError",
    "FX_CACHE_TTL_SECONDS",
    "MAX_CONSENSUS_CONCURRENCY",
    "VALUATION_STATUSES",
    "VERIFIED_ADR_SHARES_PER_UNIT",
    "RawConsensusRecord",
    "VerifiedAdrRatio",
    "WatchlistConsensusAdapter",
    "_clear_consensus_cache_for_tests",
    "infer_quote_currency",
    "select_fmp_fy1_fy2_estimates",
]
