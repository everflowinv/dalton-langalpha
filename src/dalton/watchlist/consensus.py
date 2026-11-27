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

# Hard total deadline for any upstream fetch executed inside a Redis slot/inflight lock.
# Strictly shorter than the 15s Redis lease so slow upstream calls (e.g. FMPClient's 30s httpx
# timeout across estimates/income/profile) are cancelled and release the slot before lease expiry.
_UPSTREAM_SLOT_HARD_TIMEOUT_SECONDS = 12.0

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


# Curated verified ADR ratios — only entries with verified issuer filing / depositary source and
# effective date are kept. Unverified or delisted/converted tickers (e.g. AZN, which transitioned
# from ADSs to direct NYSE ordinary share listing on 2026-02-02) are omitted so any unverified ADR
# renders `—` (`unit_unverified`) instead of guessing.
# Per-company row `aliases.adr_ratio` or `extra.adr_ratio` overrides this table when present.
VERIFIED_ADR_SHARES_PER_UNIT: dict[str, VerifiedAdrRatio] = {
    "BABA": VerifiedAdrRatio(
        8.0,
        "CNY",
        "Alibaba Group Form 20-F (1-to-8 ordinary share subdivision, 1 ADS = 8 ordinary shares)",
        "2019-07-30",
    ),
    "JD": VerifiedAdrRatio(
        2.0,
        "CNY",
        "JD.com, Inc. Form 20-F (1 ADS = 2 Class A ordinary shares)",
        "2014-05-22",
    ),
    "PDD": VerifiedAdrRatio(
        4.0,
        "CNY",
        "PDD Holdings Inc. Form 20-F (1 ADS = 4 Class A ordinary shares)",
        "2018-07-26",
    ),
    "BIDU": VerifiedAdrRatio(
        8.0,
        "CNY",
        "Baidu, Inc. Form 20-F (80-for-1 share subdivision, 1 ADS = 8 Class A ordinary shares)",
        "2021-03-01",
    ),
    "NTES": VerifiedAdrRatio(
        5.0,
        "CNY",
        "NetEase, Inc. Form 20-F (ADS ratio change, 1 ADS = 5 ordinary shares)",
        "2020-10-02",
    ),
    "TSM": VerifiedAdrRatio(
        5.0,
        "TWD",
        "TSMC Form 20-F (NYSE ADR program, 1 ADS = 5 common shares)",
        "1997-10-08",
    ),
    "ASML": VerifiedAdrRatio(
        1.0,
        "EUR",
        "ASML Holding N.V. Form 20-F (1 NY Registry Share = 1 ordinary share)",
        "2007-10-01",
    ),
    "NVO": VerifiedAdrRatio(
        1.0,
        "DKK",
        "Novo Nordisk A/S Form 20-F (2-for-1 ADR & B-share split, 1 ADR = 1 Class B share)",
        "2023-09-20",
    ),
    "SONY": VerifiedAdrRatio(
        1.0,
        "JPY",
        "Sony Group Corp. Form 20-F (5-for-1 common & ADS split, 1 ADS = 1 common share)",
        "2024-10-09",
    ),
    "TM": VerifiedAdrRatio(
        10.0,
        "JPY",
        "Toyota Motor Corp. Form 20-F / BNY Mellon DR (5-for-1 common split, 1 ADS = 10 common shares)",
        "2021-10-01",
    ),
    "HMC": VerifiedAdrRatio(
        3.0,
        "JPY",
        "Honda Motor Co., Ltd. Form 20-F (3-for-1 common stock split, 1 ADS = 3 common shares)",
        "2023-10-01",
    ),
    "SMFG": VerifiedAdrRatio(
        1.2,
        "JPY",
        "Sumitomo Mitsui Financial Group ADS ratio change notice (e20260513_03.pdf) / Citi DR CUSIP 86562M209 (2-for-1 common split, 5 ADSs = 6 common shares)",
        "2026-10-01",
    ),
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


class _ConsensusSingleflightBusyError(RuntimeError):
    """Raised when another worker still holds the Redis singleflight lock past the wait timeout."""


class _ConsensusEmptyCooldownError(RuntimeError):
    """Raised when upstream returned only rolled-past/unusable estimates and entered cooldown."""


def _canonical_symbol(symbol: str) -> str:
    """Normalize a symbol to the repository's canonical display spelling for caching & dedup.

    Examples: ``600519.SS`` -> ``600519.SH``, ``BRK.B`` -> ``BRK-B``, ``700.HK`` -> ``0700.HK``.
    """
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    try:
        from market_protocol import display_spelling

        return display_spelling(raw)
    except Exception:
        return raw


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


def _max_iso_date(*dates: str | None) -> str | None:
    """Return the latest parseable ISO ``YYYY-MM-DD`` date among *dates*, or ``None``."""
    best: date | None = None
    for raw in dates:
        parsed = _parse_iso_date(raw)
        if parsed is not None and (best is None or parsed > best):
            best = parsed
    return best.isoformat() if best is not None else None


def _is_eligible_fy_end(
    end_date_str: str | None,
    *,
    today: date,
    last_reported_fy_end: str | None = None,
) -> bool:
    """Unified FY eligibility rule across Yahoo and FMP: ``end_date >= today`` and ``> last_reported_fy_end``."""
    dt = _parse_iso_date(end_date_str)
    if dt is None:
        return False
    if dt < today:
        return False
    cutoff = _parse_iso_date(last_reported_fy_end)
    if cutoff is not None and dt <= cutoff:
        return False
    return True


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
            symbol=_canonical_symbol(str(data.get("symbol") or "")),
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
    3. Curated ``VERIFIED_ADR_SHARES_PER_UNIT`` has a verified record for ``symbol`` AND:
       - ``shares_per_adr == 1.0`` (1:1 ADR -> safe regardless of currency, + FX if ``est_ccy != q_ccy``), OR
       - ``est_ccy == entry.underlying_currency`` AND ``est_ccy != q_ccy`` (foreign reporting currency
         proves EPS is per ordinary share -> multiply by verified ratio and apply FX).
       When ``shares_per_adr != 1.0`` and ``est_ccy == q_ccy`` without explicit confirmation,
       we cannot prove whether EPS is per ordinary share or already per ADS -> ``unit_unverified``.
    4. Otherwise -> ``(False, None, False)`` (``unit_unverified``, displays ``—``).
    """
    sym = _canonical_symbol(symbol)
    if record.eps_per_quote_unit_confirmed and est_ccy is not None and est_ccy == q_ccy:
        return True, 1.0, False

    if row_adr_ratio is not None:
        needs_fx = bool(est_ccy and q_ccy and est_ccy != q_ccy)
        return True, float(row_adr_ratio), needs_fx

    entry = VERIFIED_ADR_SHARES_PER_UNIT.get(sym)
    if entry is not None:
        if entry.shares_per_adr == 1.0:
            needs_fx = bool(est_ccy and q_ccy and est_ccy != q_ccy)
            return True, 1.0, needs_fx
        if (
            est_ccy is not None
            and q_ccy is not None
            and est_ccy == entry.underlying_currency
            and est_ccy != q_ccy
        ):
            return True, float(entry.shares_per_adr), True

    return False, None, False


def _determine_fx_requirement(
    record: RawConsensusRecord | None,
    *,
    symbol: str,
    quote_currency_override: str | None = None,
    coverage_row: Mapping[str, Any] | None = None,
) -> tuple[bool, str | None, str | None]:
    """Return ``(needs_fx, from_ccy, to_ccy)`` after checking missing EPS, currency, and share-unit rules."""
    if record is None or (record.fy1_eps is None and record.fy2_eps is None):
        return False, None, None

    sym = _canonical_symbol(symbol)
    raw_q_ccy = quote_currency_override or record.quote_currency or infer_quote_currency(sym)
    q_ccy, _ = _normalize_currency(raw_q_ccy)
    # Never substitute quote currency when estimate_currency is unknown
    est_ccy, _ = _normalize_currency(record.estimate_currency)

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
    sym = _canonical_symbol(symbol)
    prov = str(provider_tag or "public").strip().lower()
    return f"dalton:watchlist:consensus:v1:{prov}:{sym}"


def _fx_cache_key(from_ccy: str, to_ccy: str) -> str:
    return f"dalton:watchlist:fx:v1:{from_ccy.upper()}:{to_ccy.upper()}"


def _consensus_cooldown_redis_key(symbol: str) -> str:
    return f"dalton:watchlist:consensus:cd:v1:{_canonical_symbol(symbol)}"


def _fx_cooldown_redis_key(fx_key: str) -> str:
    return f"{fx_key}:cd"


async def _write_consensus_cooldown(symbol: str, err_kind: str) -> None:
    sym = _canonical_symbol(symbol)
    _CONSENSUS_ERROR_COOLDOWN[sym] = (time.monotonic(), err_kind)
    redis_client = await _get_redis_cache_client()
    if redis_client is not None:
        try:
            await redis_client.set(
                _consensus_cooldown_redis_key(sym),
                json.dumps({"kind": err_kind}),
                ttl=int(CONSENSUS_ERROR_COOLDOWN_SECONDS),
            )
        except Exception:
            pass


async def _clear_consensus_cooldown(symbol: str) -> None:
    sym = _canonical_symbol(symbol)
    _CONSENSUS_ERROR_COOLDOWN.pop(sym, None)
    redis_client = await _get_redis_cache_client()
    if redis_client is not None and callable(getattr(redis_client, "delete", None)):
        try:
            await redis_client.delete(_consensus_cooldown_redis_key(sym))
        except Exception:
            pass


async def _read_consensus_cooldown(symbol: str) -> str | None:
    sym = _canonical_symbol(symbol)
    cd = _CONSENSUS_ERROR_COOLDOWN.get(sym)
    if cd is not None:
        if (time.monotonic() - cd[0]) < CONSENSUS_ERROR_COOLDOWN_SECONDS:
            return cd[1]
        _CONSENSUS_ERROR_COOLDOWN.pop(sym, None)
    redis_client = await _get_redis_cache_client()
    if redis_client is not None:
        try:
            raw = await redis_client.get(_consensus_cooldown_redis_key(sym))
            parsed: Any = json.loads(raw) if isinstance(raw, str) and raw else raw
            if isinstance(parsed, dict) and isinstance(parsed.get("kind"), str):
                return parsed["kind"]
        except Exception:
            pass
    return None


async def _write_fx_cooldown(fx_key: str, err_kind: str) -> None:
    _FX_ERROR_COOLDOWN[fx_key] = (time.monotonic(), err_kind)
    redis_client = await _get_redis_cache_client()
    if redis_client is not None:
        try:
            await redis_client.set(
                _fx_cooldown_redis_key(fx_key),
                json.dumps({"kind": err_kind}),
                ttl=int(CONSENSUS_ERROR_COOLDOWN_SECONDS),
            )
        except Exception:
            pass


async def _clear_fx_cooldown(fx_key: str) -> None:
    _FX_ERROR_COOLDOWN.pop(fx_key, None)
    redis_client = await _get_redis_cache_client()
    if redis_client is not None and callable(getattr(redis_client, "delete", None)):
        try:
            await redis_client.delete(_fx_cooldown_redis_key(fx_key))
        except Exception:
            pass


async def _read_fx_cooldown(fx_key: str) -> str | None:
    cd = _FX_ERROR_COOLDOWN.get(fx_key)
    if cd is not None:
        if (time.monotonic() - cd[0]) < CONSENSUS_ERROR_COOLDOWN_SECONDS:
            return cd[1]
        _FX_ERROR_COOLDOWN.pop(fx_key, None)
    redis_client = await _get_redis_cache_client()
    if redis_client is not None:
        try:
            raw = await redis_client.get(_fx_cooldown_redis_key(fx_key))
            parsed: Any = json.loads(raw) if isinstance(raw, str) and raw else raw
            if isinstance(parsed, dict) and isinstance(parsed.get("kind"), str):
                return parsed["kind"]
        except Exception:
            pass
    return None


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
    from src.data_client.yfinance.yahoo import yahoo_ticker

    for pair, invert in (
        (f"{from_ccy}{to_ccy}=X", False),
        (f"{to_ccy}{from_ccy}=X", True),
    ):
        try:
            t = yahoo_ticker(pair)
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


def _filter_consensus_record_fiscal_years(
    record: RawConsensusRecord,
    *,
    today: date,
    last_reported_fy_end: str | None = None,
) -> tuple[RawConsensusRecord, bool]:
    """Filter a record's FY1/FY2 slots by ``end >= today`` and ``end > last_reported_fy_end``.

    Returns ``(filtered_record, dropped_any_rolled_fy)``.
    Eligible null-EPS FY1 slots are preserved so FY2 is never promoted over an eligible FY1;
    rolled-past FY1 slots ARE dropped so an eligible FY2 becomes the new FY1.
    """
    eff_reported = _max_iso_date(last_reported_fy_end, record.last_reported_fy_end)
    dropped_any = False
    kept_dated: list[tuple[str, float | None]] = []
    kept_undated: list[float | None] = []

    for end_raw, eps_val in ((record.fy1_end, record.fy1_eps), (record.fy2_end, record.fy2_eps)):
        parsed = _parse_iso_date(end_raw)
        if parsed is not None:
            iso = parsed.isoformat()
            if _is_eligible_fy_end(iso, today=today, last_reported_fy_end=eff_reported):
                kept_dated.append((iso, eps_val))
            else:
                dropped_any = True
        elif eps_val is not None:
            kept_undated.append(eps_val)

    kept_dated.sort(key=lambda item: item[0])
    slots: list[tuple[str | None, float | None]] = list(kept_dated) + [
        (None, eps) for eps in kept_undated
    ]
    new_fy1_end, new_fy1_eps = slots[0] if len(slots) >= 1 else (None, None)
    new_fy2_end, new_fy2_eps = slots[1] if len(slots) >= 2 else (None, None)

    has_any_eps = new_fy1_eps is not None or new_fy2_eps is not None
    new_analysts = record.analyst_count if has_any_eps else None

    if (
        not dropped_any
        and new_fy1_end == record.fy1_end
        and new_fy1_eps == record.fy1_eps
        and new_fy2_end == record.fy2_end
        and new_fy2_eps == record.fy2_eps
        and eff_reported == record.last_reported_fy_end
        and new_analysts == record.analyst_count
    ):
        return record, False

    return (
        RawConsensusRecord(
            symbol=_canonical_symbol(record.symbol),
            fy1_eps=new_fy1_eps,
            fy2_eps=new_fy2_eps,
            fy1_end=new_fy1_end,
            fy2_end=new_fy2_end,
            estimate_currency=record.estimate_currency,
            quote_currency=record.quote_currency,
            analyst_count=new_analysts,
            provider=record.provider,
            as_of=record.as_of,
            quote_type=record.quote_type,
            security_name=record.security_name,
            eps_per_quote_unit_confirmed=record.eps_per_quote_unit_confirmed,
            is_adr=record.is_adr,
            last_reported_fy_end=eff_reported,
        ),
        dropped_any,
    )


def _fetch_yfinance_consensus_result_sync(
    symbol: str,
    *,
    today: date | None = None,
    last_reported_fy_end: str | None = None,
) -> tuple[RawConsensusRecord | None, bool]:
    """Read FY1 (`0y`) and FY2 (`+1y`) consensus EPS via ``yahoo_ticker()`` and apply unified FY filtering."""
    from src.data_client.yfinance.yahoo import yahoo_ticker

    sym = _canonical_symbol(symbol)
    if not sym:
        return None, False
    try:
        t = yahoo_ticker(sym)
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

    info: Mapping[str, Any] = {}
    try:
        raw_info = getattr(t, "info", None)
        if isinstance(raw_info, Mapping):
            info = raw_info
    except Exception as exc:
        if _is_rate_limit_exception(exc):
            raise ConsensusRateLimitError(str(exc)) from exc
        info = {}

    ref_today = today or _utc_today()
    info_reported_fy_end = _epoch_to_iso_date(info.get("lastFiscalYearEnd"))
    eff_reported_fy_end = _max_iso_date(last_reported_fy_end, info_reported_fy_end)

    # Unified FY filter BEFORE assigning FY1/FY2: keep only end_date >= today and > last_reported_fy_end.
    dropped_rolled_fy = False
    eligible_dated: list[tuple[str, float | None, int | None]] = []
    eligible_undated: list[tuple[None, float | None, int | None]] = []

    for period_key in ("0y", "+1y"):
        row = rows.get(period_key) or {}
        eps_val = _finite_float(row.get("avg"))
        raw_end = end_dates.get(period_key) or (
            str(row.get("endDate"))[:10] if row.get("endDate") else None
        )
        analysts = _positive_int(row.get("numberOfAnalysts"))
        parsed_end = _parse_iso_date(raw_end)
        if parsed_end is not None:
            iso_end = parsed_end.isoformat()
            if _is_eligible_fy_end(
                iso_end, today=ref_today, last_reported_fy_end=eff_reported_fy_end
            ):
                eligible_dated.append((iso_end, eps_val, analysts))
            else:
                dropped_rolled_fy = True
        elif eps_val is not None:
            eligible_undated.append((None, eps_val, analysts))

    eligible_dated.sort(key=lambda item: item[0])
    eligible_slots: list[tuple[str | None, float | None, int | None]] = list(eligible_dated) + list(
        eligible_undated
    )

    fy1_end, fy1_eps, fy1_analysts = eligible_slots[0] if len(eligible_slots) >= 1 else (None, None, None)
    fy2_end, fy2_eps, fy2_analysts = eligible_slots[1] if len(eligible_slots) >= 2 else (None, None, None)
    analyst_count = fy1_analysts or fy2_analysts

    raw_quote_ccy = info.get("currency") or infer_quote_currency(sym)
    raw_fin_ccy = info.get("financialCurrency")
    raw_info_earn_ccy = info.get("earningsCurrency")
    quote_type = str(info.get("quoteType") or "").strip() or None
    sec_name = str(info.get("longName") or info.get("shortName") or "").strip() or None

    explicit_earn_ccy = trend_earnings_ccy or (
        str(raw_info_earn_ccy).strip() if raw_info_earn_ccy else None
    )
    effective_est_ccy = explicit_earn_ccy or (
        str(raw_fin_ccy).strip() if raw_fin_ccy else str(raw_quote_ccy).strip()
    )

    # IMPORTANT (§6.3): `earningsCurrency == quote_currency` does NOT prove EPS is reported per ADR.
    # yfinance provides no share-unit attestation, so `eps_per_quote_unit_confirmed` stays False.
    rec = RawConsensusRecord(
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
        last_reported_fy_end=eff_reported_fy_end,
    )
    return rec, dropped_rolled_fy


def _fetch_yfinance_consensus_sync(
    symbol: str,
    *,
    today: date | None = None,
    last_reported_fy_end: str | None = None,
) -> RawConsensusRecord | None:
    """Read FY1 and FY2 consensus EPS via ``yahoo_ticker()`` with unified fiscal-year filtering."""
    rec, _ = _fetch_yfinance_consensus_result_sync(
        symbol, today=today, last_reported_fy_end=last_reported_fy_end
    )
    return rec


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


def _filter_fmp_eligible_estimates(
    annual_estimates: list[Mapping[str, Any]],
    *,
    today: date | None = None,
    last_reported_fy_end: str | None = None,
) -> list[Mapping[str, Any]]:
    """Deduplicate and filter FMP annual rows by ``date >= today`` and ``date > last_reported_fy_end``."""
    ref_day = today or _utc_today()

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

    eligible: list[Mapping[str, Any]] = []
    for dt_str in sorted(by_date.keys()):
        if _is_eligible_fy_end(dt_str, today=ref_day, last_reported_fy_end=last_reported_fy_end):
            eligible.append(by_date[dt_str])
    return eligible


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
    candidates = _filter_fmp_eligible_estimates(
        annual_estimates,
        today=today,
        last_reported_fy_end=last_reported_fy_end,
    )
    if not candidates:
        return None, None
    fy1_row = candidates[0]
    fy1_dt = _parse_iso_date(fy1_row.get("date"))
    fy1_iso = fy1_dt.isoformat() if fy1_dt is not None else ""
    fy2_row = next(
        (
            row
            for row in candidates[1:]
            if (p := _parse_iso_date(row.get("date"))) is not None and p.isoformat() > fy1_iso
        ),
        None,
    )
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
    today: date | None = None,
    last_reported_fy_end: str | None = None,
) -> RawConsensusRecord:
    """Fill a single missing year (FY1 or FY2) in ``primary`` using eligible ``fallback`` rows only."""
    p_ccy, _ = _normalize_currency(primary.estimate_currency)
    f_ccy, _ = _normalize_currency(fallback.estimate_currency)
    # Refuse to merge when fallback currency is unverified/missing or incompatible with primary
    if not f_ccy or (p_ccy and p_ccy != f_ccy):
        return primary

    ref_today = today or _utc_today()
    eff_reported = _max_iso_date(
        last_reported_fy_end,
        primary.last_reported_fy_end,
        fallback.last_reported_fy_end,
    )

    fy1_eps = primary.fy1_eps
    fy1_end = primary.fy1_end
    fy2_eps = primary.fy2_eps
    fy2_end = primary.fy2_end
    analyst_count = primary.analyst_count
    filled_any = False

    # Only search FMP rows that pass the unified FY eligibility filter (never resurrect excluded FYs!)
    eligible_rows = _filter_fmp_eligible_estimates(
        list(fallback_annual_rows or []),
        today=ref_today,
        last_reported_fy_end=eff_reported,
    )

    if fy1_eps is None:
        fy1_target_ok = fy1_end is None or _is_eligible_fy_end(
            fy1_end, today=ref_today, last_reported_fy_end=eff_reported
        )
        matched_row = (
            _find_fmp_row_by_fy_end(eligible_rows, fy1_end)
            if (fy1_end and fy1_target_ok)
            else None
        )
        cand_eps: float | None = None
        cand_end: str | None = None
        cand_analysts: int | None = None
        if matched_row is not None:
            cand_eps = _fmp_row_eps(matched_row)
            cand_end = str(matched_row.get("date"))[:10]
            cand_analysts = _fmp_row_analysts(matched_row)
        elif fy1_end is None:
            # No target FY1 end from primary: accept eligible fallback FY1 strictly earlier than primary FY2
            fb1_ok = fallback.fy1_end is None or _is_eligible_fy_end(
                fallback.fy1_end, today=ref_today, last_reported_fy_end=eff_reported
            )
            if (
                fb1_ok
                and fallback.fy1_eps is not None
                and (
                    fy2_end is None
                    or fallback.fy1_end is None
                    or (
                        fallback.fy1_end < fy2_end
                        and not _dates_match_fiscal_year(fallback.fy1_end, fy2_end)
                    )
                )
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
        fy2_target_ok = fy2_end is None or _is_eligible_fy_end(
            fy2_end, today=ref_today, last_reported_fy_end=eff_reported
        )
        matched_row = (
            _find_fmp_row_by_fy_end(eligible_rows, fy2_end)
            if (fy2_end and fy2_target_ok)
            else None
        )
        cand_eps = None
        cand_end = None
        cand_analysts = None
        if matched_row is not None:
            cand_eps = _fmp_row_eps(matched_row)
            cand_end = str(matched_row.get("date"))[:10]
            cand_analysts = _fmp_row_analysts(matched_row)
        elif fy2_end is None:
            # No target FY2 end from primary: pick eligible fallback FY2 (or FY1) strictly after primary FY1
            fb2_ok = fallback.fy2_end is None or _is_eligible_fy_end(
                fallback.fy2_end, today=ref_today, last_reported_fy_end=eff_reported
            )
            fb1_ok = fallback.fy1_end is None or _is_eligible_fy_end(
                fallback.fy1_end, today=ref_today, last_reported_fy_end=eff_reported
            )
            if (
                fb2_ok
                and fallback.fy2_eps is not None
                and (
                    fy1_end is None
                    or fallback.fy2_end is None
                    or (
                        fallback.fy2_end > fy1_end
                        and not _dates_match_fiscal_year(fallback.fy2_end, fy1_end)
                    )
                )
            ):
                cand_eps = fallback.fy2_eps
                cand_end = fallback.fy2_end
                cand_analysts = fallback.analyst_count
            elif (
                fb1_ok
                and fy1_end is not None
                and fallback.fy1_eps is not None
                and fallback.fy1_end is not None
                and fallback.fy1_end > fy1_end
                and not _dates_match_fiscal_year(fallback.fy1_end, fy1_end)
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
        symbol=_canonical_symbol(primary.symbol),
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
        last_reported_fy_end=eff_reported,
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
    is_loading: bool = False,
) -> dict[str, Any]:
    """Compute canonical valuation payload and 7-state statuses from a raw consensus record."""
    sym = _canonical_symbol(symbol)
    row_adr_ratio = _extract_row_adr_ratio(coverage_row)

    def _empty_payload(
        status: str,
        *,
        reason: str | None = None,
        est_ccy: str | None = None,
        q_ccy: str | None = None,
        shares_per_unit: float | None = None,
        unit_verified: bool = False,
        pending: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
        if pending:
            payload["pending"] = True
        return payload

    if is_private:
        return _empty_payload("unlisted", reason="未上市公司")

    if record is None:
        if is_loading:
            return _empty_payload(
                "loading",
                reason="正在加载一致预期，请稍后重试",
                pending=True,
            )
        if is_rate_limited:
            return _empty_payload("rate_limited", reason="上游数据限流（429），请稍后重试")
        return _empty_payload("no_coverage", reason="暂无一致预期覆盖")

    raw_q_ccy = quote_currency_override or record.quote_currency or infer_quote_currency(sym)
    q_ccy, q_scale = _normalize_currency(raw_q_ccy)
    # Never fall back to quote currency when estimate_currency is absent!
    est_ccy, est_scale = _normalize_currency(record.estimate_currency)

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

    if est_ccy is None:
        return _empty_payload(
            "unit_unverified",
            reason="缺少财报预测币种，暂不计算估值",
            est_ccy=None,
            q_ccy=q_ccy,
            shares_per_unit=None,
            unit_verified=False,
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
        if est_ccy == q_ccy or q_ccy is None:
            eff_fx = 1.0
            unit_verified = True
        else:
            eff_fx = _positive_float(fx_rate)
            if eff_fx is not None:
                unit_verified = True
            else:
                if is_loading:
                    return _empty_payload(
                        "loading",
                        reason="正在加载即期汇率，请稍后重试",
                        est_ccy=est_ccy,
                        q_ccy=q_ccy,
                        shares_per_unit=1.0,
                        unit_verified=False,
                        pending=True,
                    )
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
                if is_loading:
                    return _empty_payload(
                        "loading",
                        reason="正在加载即期汇率，请稍后重试",
                        est_ccy=est_ccy,
                        q_ccy=q_ccy,
                        shares_per_unit=shares_per_quote_unit,
                        unit_verified=False,
                        pending=True,
                    )
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

    async def _fetch_from_yfinance(
        self,
        symbol: str,
        *,
        last_reported_fy_end: str | None = None,
    ) -> tuple[RawConsensusRecord | None, bool]:
        if self._yfinance_fetcher is not None:
            res = self._yfinance_fetcher(symbol)
            rec = await res if asyncio.iscoroutine(res) else res
            return rec, False
        return await asyncio.wait_for(
            asyncio.to_thread(
                _fetch_yfinance_consensus_result_sync,
                symbol,
                today=self._today(),
                last_reported_fy_end=last_reported_fy_end,
            ),
            timeout=CONSENSUS_FETCH_TIMEOUT_SECONDS,
        )

    async def _fetch_from_fmp_with_rows(
        self,
        symbol: str,
        *,
        last_reported_fy_end: str | None = None,
    ) -> tuple[RawConsensusRecord | None, list[Mapping[str, Any]], bool]:
        """Fetch annual estimates from FMP using a lifecycle-managed client bounded below Redis lease.

        Returns ``(record, eligible_rows, had_rolled_past_only)``.
        Estimate currency is taken strictly from financial statements (``income_statement.reportedCurrency``)
        and NEVER falls back to ``profile.currency``.
        """
        sym = _canonical_symbol(symbol)
        injected_client = self._fmp_client
        owned_client: Any = None
        if injected_client is not None:
            client = injected_client
        else:
            api_key = os.environ.get("FMP_API_KEY", "").strip()
            if not api_key:
                return None, [], False
            try:
                from src.data_client.fmp.fmp_client import FMPClient

                owned_client = FMPClient(api_key=api_key)
                client = owned_client
            except Exception:
                return None, [], False

        async def _run_fmp_calls() -> tuple[RawConsensusRecord | None, list[Mapping[str, Any]], bool]:
            try:
                estimates = await client.get_analyst_estimates(sym, period="annual", limit=6)
            except Exception as exc:
                if _is_rate_limit_exception(exc):
                    raise ConsensusRateLimitError(str(exc)) from exc
                logger.debug("[WATCHLIST_CONSENSUS] FMP estimates failed for %s: %s", sym, exc)
                raise

            if not isinstance(estimates, list) or not estimates:
                return None, [], False

            eff_reported_fy_end = last_reported_fy_end
            est_ccy: str | None = None
            get_income = getattr(client, "get_income_statement", None)
            if callable(get_income):
                try:
                    stmts = await get_income(sym, period="annual", limit=1)
                    if isinstance(stmts, list) and stmts and isinstance(stmts[0], Mapping):
                        stmt0 = stmts[0]
                        parsed_dt = _parse_iso_date(stmt0.get("date"))
                        if parsed_dt is not None:
                            eff_reported_fy_end = _max_iso_date(
                                eff_reported_fy_end, parsed_dt.isoformat()
                            )
                        raw_rep_ccy = stmt0.get("reportedCurrency")
                        if isinstance(raw_rep_ccy, str) and raw_rep_ccy.strip():
                            est_ccy = raw_rep_ccy.strip()
                except Exception as exc:
                    if _is_rate_limit_exception(exc):
                        raise ConsensusRateLimitError(str(exc)) from exc

            eligible_rows = _filter_fmp_eligible_estimates(
                estimates,
                today=self._today(),
                last_reported_fy_end=eff_reported_fy_end,
            )
            fy1_row, fy2_row = select_fmp_fy1_fy2_estimates(
                eligible_rows,
                today=self._today(),
                last_reported_fy_end=eff_reported_fy_end,
            )
            if fy1_row is None and fy2_row is None:
                had_dated_raw = any(
                    isinstance(row, Mapping) and _parse_iso_date(row.get("date")) is not None
                    for row in estimates
                )
                return None, [], had_dated_raw

            fy1_eps = _fmp_row_eps(fy1_row)
            fy2_eps = _fmp_row_eps(fy2_row)
            fy1_end = str(fy1_row.get("date"))[:10] if fy1_row and fy1_row.get("date") else None
            fy2_end = str(fy2_row.get("date"))[:10] if fy2_row and fy2_row.get("date") else None
            analyst_count = _fmp_row_analysts(fy1_row) or _fmp_row_analysts(fy2_row)

            quote_ccy = infer_quote_currency(sym)
            sec_name: str | None = None
            is_adr_flag: bool | None = None
            get_profile = getattr(client, "get_profile", None)
            if callable(get_profile):
                try:
                    prof = await get_profile(sym)
                    if isinstance(prof, list) and prof:
                        prof = prof[0]
                    if isinstance(prof, Mapping):
                        # Official FMP /stable/profile exposes trading `currency`, `companyName`, `isAdr`
                        # — never read or fall back to `profile.currency` as the EPS reporting currency.
                        quote_ccy = str(prof.get("currency") or quote_ccy).strip()
                        sec_name = str(prof.get("companyName") or "").strip() or None
                        if isinstance(prof.get("isAdr"), bool):
                            is_adr_flag = prof["isAdr"]
                except Exception as exc:
                    if _is_rate_limit_exception(exc):
                        raise ConsensusRateLimitError(str(exc)) from exc

            rec = RawConsensusRecord(
                symbol=sym,
                fy1_eps=fy1_eps,
                fy2_eps=fy2_eps,
                fy1_end=fy1_end,
                fy2_end=fy2_end,
                estimate_currency=est_ccy,
                quote_currency=quote_ccy,
                analyst_count=analyst_count,
                provider="fmp",
                as_of=datetime.now(timezone.utc).isoformat(),
                security_name=sec_name,
                eps_per_quote_unit_confirmed=False,
                is_adr=is_adr_flag,
                last_reported_fy_end=eff_reported_fy_end,
            )
            return rec, eligible_rows, False

        try:
            return await asyncio.wait_for(
                _run_fmp_calls(),
                timeout=CONSENSUS_FETCH_TIMEOUT_SECONDS,
            )
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
        rec, _, _ = await self._fetch_from_fmp_with_rows(
            symbol, last_reported_fy_end=last_reported_fy_end
        )
        return rec

    async def _fetch_spot_fx_rate_with_status(
        self, from_ccy: str, to_ccy: str
    ) -> tuple[float | None, bool, bool, bool]:
        """Return ``(rate, is_stale, is_rate_limited, is_loading)`` for converting 1 ``from_ccy`` to ``to_ccy``."""
        src, _ = _normalize_currency(from_ccy)
        dst, _ = _normalize_currency(to_ccy)
        if not src or not dst or src == dst:
            return 1.0, False, False, False

        cached, is_fresh = await _read_fx_cache(src, dst)
        if cached is not None and is_fresh:
            return cached, False, False, False

        cache_key = _fx_cache_key(src, dst)
        existing = _INFLIGHT_FX.get(cache_key)
        if existing is not None and not existing.done():
            try:
                rate = await existing
                if rate is not None:
                    return rate, False, False, False
                return (cached, True, False, False) if cached is not None else (None, False, False, False)
            except _ConsensusSingleflightBusyError:
                return (cached, True, False, False) if cached is not None else (None, False, False, True)
            except ConsensusRateLimitError:
                return (cached, True, False, False) if cached is not None else (None, False, True, False)
            except Exception:
                return (cached, True, False, False) if cached is not None else (None, False, False, False)

        async def _load_fx() -> float | None:
            redis_client = await _get_redis_cache_client()
            inflight_key = f"{cache_key}:inflight"
            lock_token = uuid.uuid4().hex
            held_inflight_lock = False
            redis_degraded = False

            if redis_client is not None and callable(getattr(redis_client, "acquire_lock", None)):
                try:
                    acquired = await redis_client.acquire_lock(
                        inflight_key, lock_token, _REDIS_INFLIGHT_LOCK_TTL_MS
                    )
                except Exception:
                    acquired = None
                if acquired is True:
                    held_inflight_lock = True
                elif acquired is None:
                    redis_degraded = True
                elif acquired is False:
                    # Another worker is fetching this FX pair; wait up to _REDIS_INFLIGHT_WAIT_SECONDS.
                    # Never issue a duplicate upstream request if the other worker still holds the lock!
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
                            redis_degraded = True
                            break
                    if not held_inflight_lock and not redis_degraded:
                        r_cached, r_fresh = await _read_fx_cache(src, dst)
                        if r_cached is not None and r_fresh:
                            return r_cached
                        raise _ConsensusSingleflightBusyError(
                            f"Another worker still holds FX singleflight lock for {src}->{dst}"
                        )

            try:
                async with _acquire_upstream_slot():
                    async def _do_fx_upstream() -> Any:
                        if self._fx_fetcher is not None:
                            res = self._fx_fetcher(src, dst)
                            return await res if asyncio.iscoroutine(res) else res
                        return await asyncio.to_thread(_fetch_yfinance_fx_sync, src, dst)

                    rate_val = await asyncio.wait_for(
                        _do_fx_upstream(),
                        timeout=min(CONSENSUS_FETCH_TIMEOUT_SECONDS, _UPSTREAM_SLOT_HARD_TIMEOUT_SECONDS),
                    )
                    pos = _positive_float(rate_val)
                    if pos is not None:
                        await _clear_fx_cooldown(cache_key)
                        await _write_fx_cache(src, dst, pos)
                        return pos
                    await _write_fx_cooldown(cache_key, "empty")
                    return None
            except _ConsensusSingleflightBusyError:
                raise
            except Exception as exc:
                if _is_rate_limit_exception(exc):
                    await _write_fx_cooldown(cache_key, "rate_limited")
                    raise ConsensusRateLimitError(str(exc)) from exc
                await _write_fx_cooldown(cache_key, "error")
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
                return rate, False, False, False
            return (cached, True, False, False) if cached is not None else (None, False, False, False)
        except _ConsensusSingleflightBusyError:
            return (cached, True, False, False) if cached is not None else (None, False, False, True)
        except ConsensusRateLimitError:
            return (cached, True, False, False) if cached is not None else (None, False, True, False)
        except Exception:
            return (cached, True, False, False) if cached is not None else (None, False, False, False)

    async def fetch_spot_fx_rate(self, from_ccy: str, to_ccy: str) -> tuple[float | None, bool, bool]:
        """Return ``(rate, is_stale, is_rate_limited)`` for converting 1 ``from_ccy`` to ``to_ccy``."""
        rate, is_stale, is_rate_limited, _ = await self._fetch_spot_fx_rate_with_status(
            from_ccy, to_ccy
        )
        return rate, is_stale, is_rate_limited

    async def _fetch_raw_consensus_singleflight(
        self,
        symbol: str,
        *,
        last_reported_fy_end: str | None = None,
    ) -> tuple[RawConsensusRecord | None, bool, bool, bool]:
        """Return ``(record, is_stale, is_rate_limited, is_loading)`` using L3 cache + cross-process singleflight."""
        sym = _canonical_symbol(symbol)
        ref_today = self._today()
        cached_dict, is_fresh = await _read_consensus_cache(
            sym, today=ref_today, last_reported_fy_end=last_reported_fy_end
        )
        if cached_dict is not None and is_fresh:
            return RawConsensusRecord.from_dict(cached_dict), False, False, False

        cache_key = _consensus_cache_key(sym)
        existing = _INFLIGHT_CONSENSUS.get(cache_key)
        if existing is not None and not existing.done():
            try:
                loaded = await existing
                return RawConsensusRecord.from_dict(loaded), False, False, False
            except _ConsensusSingleflightBusyError:
                if cached_dict is not None:
                    return RawConsensusRecord.from_dict(cached_dict), True, False, False
                return None, False, False, True
            except ConsensusRateLimitError:
                if cached_dict is not None:
                    return RawConsensusRecord.from_dict(cached_dict), True, False, False
                return None, False, True, False
            except Exception:
                if cached_dict is not None:
                    return RawConsensusRecord.from_dict(cached_dict), True, False, False
                return None, False, False, False

        async def _load() -> dict[str, Any]:
            redis_client = await _get_redis_cache_client()
            inflight_key = f"{cache_key}:inflight"
            lock_token = uuid.uuid4().hex
            held_inflight_lock = False
            redis_degraded = False

            if redis_client is not None and callable(getattr(redis_client, "acquire_lock", None)):
                try:
                    acquired = await redis_client.acquire_lock(
                        inflight_key, lock_token, _REDIS_INFLIGHT_LOCK_TTL_MS
                    )
                except Exception:
                    acquired = None
                if acquired is True:
                    held_inflight_lock = True
                elif acquired is None:
                    redis_degraded = True
                elif acquired is False:
                    # Another worker is fetching this symbol; wait up to _REDIS_INFLIGHT_WAIT_SECONDS.
                    # If the other worker still holds the lock after timeout, return cache / loading
                    # instead of firing a duplicate upstream request!
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
                            redis_degraded = True
                            break
                    if not held_inflight_lock and not redis_degraded:
                        r_cached, r_fresh = await _read_consensus_cache(
                            sym, today=ref_today, last_reported_fy_end=last_reported_fy_end
                        )
                        if r_cached is not None and r_fresh:
                            return r_cached
                        raise _ConsensusSingleflightBusyError(
                            f"Another worker still holds consensus singleflight lock for {sym}"
                        )

            try:
                async with _acquire_upstream_slot():
                    async def _do_consensus_upstream() -> dict[str, Any]:
                        rate_limited = False
                        had_transient_error = False
                        had_rolled_past_fy = False
                        rec: RawConsensusRecord | None = None

                        # 1. Public primary: yfinance (filtered by unified FY eligibility rule)
                        try:
                            raw_rec, yf_sync_dropped = await self._fetch_from_yfinance(
                                sym, last_reported_fy_end=last_reported_fy_end
                            )
                            if yf_sync_dropped:
                                had_rolled_past_fy = True
                            if raw_rec is not None:
                                rec, rec_dropped = _filter_consensus_record_fiscal_years(
                                    raw_rec,
                                    today=ref_today,
                                    last_reported_fy_end=last_reported_fy_end,
                                )
                                if rec_dropped:
                                    had_rolled_past_fy = True
                        except Exception as exc:
                            if _is_rate_limit_exception(exc):
                                rate_limited = True
                            else:
                                had_transient_error = True
                                logger.debug(
                                    "[WATCHLIST_CONSENSUS] yfinance failed for %s: %s", sym, exc
                                )

                        # 2. Optional public fallback: FMP (when yfinance missed either or both years)
                        if rec is None or rec.fy1_eps is None or rec.fy2_eps is None:
                            eff_reported = _max_iso_date(
                                last_reported_fy_end,
                                rec.last_reported_fy_end if rec is not None else None,
                            )
                            try:
                                fmp_rec, fmp_eligible_rows, fmp_rolled_only = (
                                    await self._fetch_from_fmp_with_rows(
                                        sym, last_reported_fy_end=eff_reported
                                    )
                                )
                                if fmp_rolled_only:
                                    had_rolled_past_fy = True
                                if fmp_rec is not None:
                                    if rec is None or (
                                        rec.fy1_eps is None and rec.fy2_eps is None
                                    ):
                                        if (
                                            fmp_rec.fy1_eps is not None
                                            or fmp_rec.fy2_eps is not None
                                        ):
                                            rec = fmp_rec
                                    else:
                                        rec = _merge_partial_consensus_record(
                                            rec,
                                            fmp_rec,
                                            fallback_annual_rows=fmp_eligible_rows,
                                            today=ref_today,
                                            last_reported_fy_end=eff_reported,
                                        )
                            except ConsensusRateLimitError:
                                rate_limited = True
                            except Exception as exc:
                                had_transient_error = True
                                logger.debug(
                                    "[WATCHLIST_CONSENSUS] FMP fallback failed for %s: %s",
                                    sym,
                                    exc,
                                )

                        has_usable_eps = rec is not None and (
                            rec.fy1_eps is not None or rec.fy2_eps is not None
                        )

                        # Decide degradation strictly by whether usable EPS exists (fixes Yahoo empty record + FMP 429)
                        if not has_usable_eps and rate_limited:
                            await _write_consensus_cooldown(sym, "rate_limited")
                            raise ConsensusRateLimitError(
                                f"Rate limited fetching consensus for {sym}"
                            )

                        if not has_usable_eps and had_transient_error:
                            await _write_consensus_cooldown(sym, "error")
                            raise RuntimeError(f"Transient consensus fetch failure for {sym}")

                        if not has_usable_eps and had_rolled_past_fy:
                            # Never write a rolled-past record back to cache as a fresh success!
                            await _write_consensus_cooldown(sym, "empty")
                            raise _ConsensusEmptyCooldownError(
                                f"Only rolled-past fiscal years available for {sym}"
                            )

                        if rec is None:
                            rec = RawConsensusRecord(
                                symbol=sym,
                                quote_currency=infer_quote_currency(sym),
                                provider="yfinance",
                                as_of=datetime.now(timezone.utc).isoformat(),
                            )

                        rec_dict = rec.to_dict()
                        final_reported = _max_iso_date(
                            last_reported_fy_end, rec.last_reported_fy_end
                        )
                        if _is_record_rolled_past_fy(
                            rec_dict, today=ref_today, last_reported_fy_end=final_reported
                        ):
                            await _write_consensus_cooldown(sym, "empty")
                            raise _ConsensusEmptyCooldownError(
                                f"Refusing to cache rolled-past FY record for {sym}"
                            )

                        await _clear_consensus_cooldown(sym)
                        await _write_consensus_cache(sym, rec_dict)
                        return rec_dict

                    # Hard total deadline strictly shorter than the 15s Redis slot/inflight lease
                    return await asyncio.wait_for(
                        _do_consensus_upstream(),
                        timeout=_UPSTREAM_SLOT_HARD_TIMEOUT_SECONDS,
                    )
            except (_ConsensusSingleflightBusyError, ConsensusRateLimitError, _ConsensusEmptyCooldownError):
                raise
            except Exception as exc:
                if _is_rate_limit_exception(exc):
                    await _write_consensus_cooldown(sym, "rate_limited")
                    raise ConsensusRateLimitError(str(exc)) from exc
                await _write_consensus_cooldown(sym, "error")
                raise
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
            return RawConsensusRecord.from_dict(loaded), False, False, False
        except _ConsensusSingleflightBusyError:
            if cached_dict is not None:
                return RawConsensusRecord.from_dict(cached_dict), True, False, False
            return None, False, False, True
        except ConsensusRateLimitError:
            if cached_dict is not None:
                return RawConsensusRecord.from_dict(cached_dict), True, False, False
            return None, False, True, False
        except Exception:
            if cached_dict is not None:
                return RawConsensusRecord.from_dict(cached_dict), True, False, False
            return None, False, False, False

    def schedule_warmup(
        self,
        symbol: str,
        *,
        quote_currency: str | None = None,
        coverage_row: Mapping[str, Any] | None = None,
        last_reported_fy_end: str | None = None,
    ) -> None:
        """Kick off bounded background consensus + conditional FX warm-up without blocking HTTP response."""
        sym = _canonical_symbol(symbol)
        if not sym:
            return
        existing = _INFLIGHT_WARMUP.get(sym)
        if existing is not None and not existing.done():
            return

        eff_reported = last_reported_fy_end or _extract_explicit_last_reported_fy_end(coverage_row)

        async def _bg() -> None:
            try:
                rec, _, _, _ = await self._fetch_raw_consensus_singleflight(
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
        sym = _canonical_symbol(symbol)
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
            err_kind = await _read_consensus_cooldown(sym)
            if err_kind is not None:
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

        consensus_needs_warmup = False
        if not is_fresh:
            consensus_cd = await _read_consensus_cooldown(sym)
            consensus_needs_warmup = consensus_cd is None

        rec = RawConsensusRecord.from_dict(cached_dict)

        # Check missing EPS, missing estimate currency & ADR share-unit verification BEFORE checking FX cache!
        # Missing EPS -> `no_coverage` immediately; unverified ADR / missing est_ccy -> `unit_unverified` immediately.
        needs_fx, est_ccy, q_ccy = _determine_fx_requirement(
            rec,
            symbol=sym,
            quote_currency_override=quote_currency,
            coverage_row=coverage_row,
        )

        fx_rate: float | None = 1.0
        fx_stale = False
        fx_rate_limited = False
        fx_needs_warmup = False
        if needs_fx and est_ccy and q_ccy:
            cached_fx, fx_fresh = await _read_fx_cache(est_ccy, q_ccy)
            fx_key = _fx_cache_key(est_ccy, q_ccy)
            if cached_fx is None:
                fx_cd = await _read_fx_cooldown(fx_key)
                if fx_cd is not None:
                    fx_rate = None
                    fx_rate_limited = fx_cd == "rate_limited"
                    fx_needs_warmup = False
                else:
                    return None, True
            else:
                fx_rate = cached_fx
                fx_stale = not fx_fresh
                if fx_stale:
                    fx_cd = await _read_fx_cooldown(fx_key)
                    fx_needs_warmup = fx_cd is None

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
        return val, (consensus_needs_warmup or fx_needs_warmup)

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
        sym = _canonical_symbol(symbol)
        if is_private:
            return _compute_valuation_from_record(
                None, symbol=sym, latest_price=None, is_private=True
            )

        last_reported_fy_end = _extract_explicit_last_reported_fy_end(coverage_row)
        rec, rec_stale, rec_rate_limited, rec_loading = await self._fetch_raw_consensus_singleflight(
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
                is_loading=rec_loading,
            )

        fx_rate: float | None = 1.0
        fx_stale = False
        fx_rate_limited = False
        fx_loading = False
        needs_fx, est_ccy, q_ccy = _determine_fx_requirement(
            rec,
            symbol=sym,
            quote_currency_override=quote_currency,
            coverage_row=coverage_row,
        )
        if needs_fx and est_ccy and q_ccy:
            fx_rate, fx_stale, fx_rate_limited, fx_loading = (
                await self._fetch_spot_fx_rate_with_status(est_ccy, q_ccy)
            )

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
            is_loading=rec_loading or fx_loading,
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
