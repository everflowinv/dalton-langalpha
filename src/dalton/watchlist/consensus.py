"""Public consensus valuation adapter (`WatchlistConsensusAdapter`) for Personal Watchlist (§6.3).

Design invariants:
- Works cleanly on Standalone Core with zero private service dependencies at import or runtime.
- Primary source: public `yfinance` (`Ticker.earnings_estimate` + `_analysis._earnings_trend` endDate).
- Optional public fallback: `FMP` (`get_analyst_estimates(..., period="annual")`) when `FMP_API_KEY` is configured.
- Optional Everflow enhancement strictly behind `if has_capability(CapabilityId.RESEARCHDATA_BRIDGE):` lazy import.
- Explicit share-unit & FX verification: never silently assumes 1:1 on ADRs with foreign-currency EPS.
- Distinguishes all 7 valuation cell states:
  `ok`, `loss`, `no_coverage`, `unit_unverified`, `stale`, `rate_limited`, `unlisted`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any

from src.dalton.capabilities import CapabilityId, has_capability

logger = logging.getLogger(__name__)

CONSENSUS_CACHE_TTL_SECONDS = 12 * 3600  # 12 hours (§6.2)
CONSENSUS_STALE_TTL_SECONDS = 7 * 24 * 3600  # 7 days stale fallback window
CONSENSUS_FETCH_TIMEOUT_SECONDS = 8.0
FX_CACHE_TTL_SECONDS = 12 * 3600
FX_STALE_TTL_SECONDS = 7 * 24 * 3600
MAX_CONSENSUS_CONCURRENCY = 8

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

# Curated verified ADR ratios: ticker -> (shares_per_adr, underlying_reporting_currency)
# Verified against depositary bank / exchange filings; per-company row `aliases.adr_ratio`
# or `extra.adr_ratio` overrides this table when present.
VERIFIED_ADR_SHARES_PER_UNIT: dict[str, tuple[float, str]] = {
    "BABA": (8.0, "CNY"),
    "JD": (2.0, "CNY"),
    "PDD": (4.0, "CNY"),
    "BIDU": (8.0, "CNY"),
    "NTES": (5.0, "CNY"),
    "TCOM": (1.0, "CNY"),
    "NIO": (1.0, "CNY"),
    "XPEV": (2.0, "CNY"),
    "LI": (2.0, "CNY"),
    "BEKE": (3.0, "CNY"),
    "BILI": (1.0, "CNY"),
    "TME": (2.0, "CNY"),
    "ZTO": (1.0, "CNY"),
    "VIPS": (0.2, "CNY"),
    "YUMC": (1.0, "USD"),
    "TSM": (5.0, "TWD"),
    "UMC": (5.0, "TWD"),
    "ASX": (2.0, "TWD"),
    "ASML": (1.0, "EUR"),
    "SAP": (1.0, "EUR"),
    "NVO": (1.0, "DKK"),
    "AZN": (0.5, "USD"),
    "GSK": (2.0, "GBP"),
    "SNY": (0.5, "EUR"),
    "SONY": (1.0, "JPY"),
    "TM": (2.0, "JPY"),
    "HMC": (3.0, "JPY"),
    "MUFG": (1.0, "JPY"),
    "SMFG": (0.2, "JPY"),
    "MFG": (0.2, "JPY"),
    "SHEL": (2.0, "USD"),
    "BP": (6.0, "USD"),
    "HSBC": (5.0, "USD"),
    "UL": (1.0, "EUR"),
    "DEO": (4.0, "USD"),
    "BTI": (1.0, "GBP"),
    "RIO": (1.0, "USD"),
    "BHP": (2.0, "USD"),
    "VALE": (1.0, "USD"),
    "PBR": (2.0, "USD"),
    "ITUB": (1.0, "BRL"),
    "INFY": (1.0, "INR"),
    "WIT": (1.0, "INR"),
    "HDB": (3.0, "INR"),
    "IBN": (2.0, "INR"),
    "ARM": (1.0, "USD"),
    "SE": (1.0, "USD"),
    "BUD": (1.0, "USD"),
    "ERIC": (1.0, "SEK"),
    "NOK": (1.0, "EUR"),
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
_INFLIGHT_CONSENSUS: dict[str, asyncio.Task[dict[str, Any]]] = {}
_INFLIGHT_FX: dict[str, asyncio.Task[float | None]] = {}
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
    _INFLIGHT_CONSENSUS.clear()
    _INFLIGHT_FX.clear()


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


def _is_rate_limit_exception(exc: Exception) -> bool:
    cls_name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return (
        isinstance(exc, ConsensusRateLimitError)
        or "ratelimit" in cls_name
        or "429" in msg
        or "too many requests" in msg
        or "rate limit" in msg
    )


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
    # unless proven otherwise by explicit quote-currency EPS confirmation.
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
        )


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


async def _read_consensus_cache(
    symbol: str,
    *,
    provider_tag: str = "public",
) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(cached_record_dict, is_fresh)`` from L3 memory/Redis cache."""
    key = _consensus_cache_key(symbol, provider_tag)
    now_wall = time.time()

    mem = _CONSENSUS_CACHE.get(key)
    if mem is not None:
        fetched_at, payload = mem
        age = now_wall - fetched_at
        if age < CONSENSUS_CACHE_TTL_SECONDS:
            return payload, True
        if age < CONSENSUS_STALE_TTL_SECONDS:
            return payload, False

    redis_client = await _get_redis_cache_client()
    if redis_client is not None:
        try:
            raw = await redis_client.get(key)
            parsed: Any = json.loads(raw) if isinstance(raw, str) and raw else raw
            if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict):
                fetched_at = float(parsed.get("fetched_at") or 0.0)
                data = dict(parsed["data"])
                _CONSENSUS_CACHE[key] = (fetched_at, data)
                age = now_wall - fetched_at
                if age < CONSENSUS_CACHE_TTL_SECONDS:
                    return data, True
                if age < CONSENSUS_STALE_TTL_SECONDS:
                    return data, False
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
            return rate, False

    redis_client = await _get_redis_cache_client()
    if redis_client is not None:
        try:
            raw = await redis_client.get(key)
            parsed: Any = json.loads(raw) if isinstance(raw, str) and raw else raw
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

    explicit_earn_ccy = trend_earnings_ccy or (
        str(raw_info_earn_ccy).strip() if raw_info_earn_ccy else None
    )
    quote_ccy_norm, _ = _normalize_currency(raw_quote_ccy)
    explicit_earn_norm, _ = _normalize_currency(explicit_earn_ccy)
    fin_ccy_norm, _ = _normalize_currency(raw_fin_ccy)

    # Only mark `eps_per_quote_unit_confirmed` when the provider explicitly confirms
    # `earningsCurrency` matches `quote_currency` (Condition ① of §6.3).
    eps_confirmed_in_quote = bool(
        explicit_earn_norm is not None
        and quote_ccy_norm is not None
        and explicit_earn_norm == quote_ccy_norm
    )
    effective_est_ccy = explicit_earn_ccy or (
        str(raw_fin_ccy).strip() if raw_fin_ccy else str(raw_quote_ccy).strip()
    )

    if fy1_eps is None and fy2_eps is None:
        return RawConsensusRecord(
            symbol=sym,
            estimate_currency=effective_est_ccy or None,
            quote_currency=str(raw_quote_ccy).strip() if raw_quote_ccy else None,
            provider="yfinance",
            as_of=datetime.now(timezone.utc).isoformat(),
            quote_type=quote_type,
            security_name=sec_name,
            eps_per_quote_unit_confirmed=eps_confirmed_in_quote,
        )

    return RawConsensusRecord(
        symbol=sym,
        fy1_eps=fy1_eps,
        fy2_eps=fy2_eps,
        fy1_end=fy1_end,
        fy2_end=fy2_end,
        estimate_currency=effective_est_ccy or None,
        quote_currency=str(raw_quote_ccy).strip() if raw_quote_ccy else None,
        analyst_count=analyst_count,
        provider="yfinance",
        as_of=datetime.now(timezone.utc).isoformat(),
        quote_type=quote_type,
        security_name=sec_name,
        eps_per_quote_unit_confirmed=eps_confirmed_in_quote,
    )


def select_fmp_fy1_fy2_estimates(
    annual_estimates: list[Mapping[str, Any]],
    *,
    today: date | None = None,
    last_reported_fy_end: str | None = None,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """Select FY1 and FY2 annual FMP estimates matched by actual fiscal year end date (§6.3).

    Sorts annual records ascending by ``date`` and chooses the earliest record with
    ``date >= today`` (and strictly after ``last_reported_fy_end`` when known) as FY1,
    and the immediately following fiscal year record as FY2.
    """
    ref_day = (today or date.today()).isoformat()
    reported_cutoff = str(last_reported_fy_end or "").strip()[:10] or None

    dated: list[tuple[str, Mapping[str, Any]]] = []
    for item in annual_estimates or []:
        if not isinstance(item, Mapping):
            continue
        dt_str = str(item.get("date") or "").strip()[:10]
        if len(dt_str) != 10:
            continue
        eps = _finite_float(item.get("epsAvg") if "epsAvg" in item else item.get("estimatedEpsAvg"))
        if eps is None:
            continue
        dated.append((dt_str, item))

    dated.sort(key=lambda pair: pair[0])

    candidates: list[tuple[str, Mapping[str, Any]]] = []
    for dt_str, item in dated:
        if dt_str < ref_day:
            continue
        if reported_cutoff is not None and dt_str <= reported_cutoff:
            continue
        candidates.append((dt_str, item))

    if not candidates:
        return None, None
    fy1_dt, fy1_row = candidates[0]
    fy2_row = next((row for dt_str, row in candidates[1:] if dt_str > fy1_dt), None)
    return fy1_row, fy2_row


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
        # ADR / cross-market depositary receipt:
        # Condition ①: Provider explicitly confirms EPS is already per quote unit in quote_currency
        if record.eps_per_quote_unit_confirmed and est_ccy == q_ccy:
            shares_per_quote_unit = 1.0
            eff_fx = 1.0
            unit_verified = True
        else:
            # Condition ②: Verified `shares_per_adr` from row override or curated table
            verified_ratio = row_adr_ratio
            if verified_ratio is None and sym in VERIFIED_ADR_SHARES_PER_UNIT:
                verified_ratio = VERIFIED_ADR_SHARES_PER_UNIT[sym][0]

            if verified_ratio is None:
                return _empty_payload(
                    "unit_unverified",
                    reason="待核对存托凭证换算比例",
                    est_ccy=est_ccy,
                    q_ccy=q_ccy,
                    shares_per_unit=None,
                    unit_verified=False,
                )
            shares_per_quote_unit = float(verified_ratio)
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
    """Public consensus valuation adapter (`yfinance` primary, optional `FMP`, optional bridge)."""

    def __init__(
        self,
        *,
        yfinance_fetcher: Any = None,
        fmp_client: Any = None,
        fx_fetcher: Any = None,
    ) -> None:
        self._yfinance_fetcher = yfinance_fetcher
        self._fmp_client = fmp_client
        self._fx_fetcher = fx_fetcher

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

    async def _fetch_from_fmp(
        self,
        symbol: str,
        *,
        last_reported_fy_end: str | None = None,
    ) -> RawConsensusRecord | None:
        client = self._fmp_client
        if client is None:
            api_key = os.environ.get("FMP_API_KEY", "").strip()
            if not api_key:
                return None
            try:
                from src.data_client.fmp.fmp_client import FMPClient

                client = FMPClient(api_key=api_key)
            except Exception:
                return None

        try:
            estimates = await client.get_analyst_estimates(symbol, period="annual", limit=6)
        except Exception as exc:
            if _is_rate_limit_exception(exc):
                raise ConsensusRateLimitError(str(exc)) from exc
            logger.debug("[WATCHLIST_CONSENSUS] FMP estimates failed for %s: %s", symbol, exc)
            return None

        if not isinstance(estimates, list) or not estimates:
            return None
        fy1_row, fy2_row = select_fmp_fy1_fy2_estimates(
            estimates, last_reported_fy_end=last_reported_fy_end
        )
        if fy1_row is None and fy2_row is None:
            return None

        fy1_eps = _fmp_row_eps(fy1_row)
        fy2_eps = _fmp_row_eps(fy2_row)
        fy1_end = str(fy1_row.get("date"))[:10] if fy1_row and fy1_row.get("date") else None
        fy2_end = str(fy2_row.get("date"))[:10] if fy2_row and fy2_row.get("date") else None
        analyst_count = _fmp_row_analysts(fy1_row) or _fmp_row_analysts(fy2_row)

        quote_ccy = infer_quote_currency(symbol)
        # FMP estimates are reported in the company's financial reporting currency (if profile is available)
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

        return RawConsensusRecord(
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
        )

    async def _fetch_from_optional_bridge(self, symbol: str) -> RawConsensusRecord | None:
        """Everflow-only enhancement strictly behind `CapabilityId.RESEARCHDATA_BRIDGE`."""
        if not has_capability(CapabilityId.RESEARCHDATA_BRIDGE):
            return None
        try:
            # Lazy import only when bridge capability is active; Standalone Core never enters here.
            from src.dalton.settings import get_researchdata_base_url

            base_url = get_researchdata_base_url()
            if not base_url:
                return None
        except Exception:
            return None
        return None

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
            sem = _get_consensus_semaphore()
            async with sem:
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
                    await _write_fx_cache(src, dst, pos)
                return pos

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
        """Return ``(record, is_stale, is_rate_limited)`` using L3 cache + singleflight."""
        sym = str(symbol or "").strip().upper()
        cached_dict, is_fresh = await _read_consensus_cache(sym)
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
            sem = _get_consensus_semaphore()
            async with sem:
                rate_limited = False
                had_transient_error = False
                rec: RawConsensusRecord | None = None

                # 1. Public primary: yfinance
                try:
                    rec = await self._fetch_from_yfinance(sym)
                except ConsensusRateLimitError:
                    rate_limited = True
                except Exception as exc:
                    had_transient_error = True
                    logger.debug("[WATCHLIST_CONSENSUS] yfinance failed for %s: %s", sym, exc)

                # 2. Optional public fallback: FMP (when yfinance had no EPS)
                if rec is None or (rec.fy1_eps is None and rec.fy2_eps is None):
                    try:
                        fmp_rec = await self._fetch_from_fmp(
                            sym, last_reported_fy_end=last_reported_fy_end
                        )
                        if fmp_rec is not None and (
                            fmp_rec.fy1_eps is not None or fmp_rec.fy2_eps is not None
                        ):
                            rec = fmp_rec
                    except ConsensusRateLimitError:
                        rate_limited = True

                # 3. Optional Everflow enhancement (never runs in Standalone Core)
                if rec is None or (rec.fy1_eps is None and rec.fy2_eps is None):
                    bridge_rec = await self._fetch_from_optional_bridge(sym)
                    if bridge_rec is not None:
                        rec = bridge_rec

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
        last_reported_fy_end: str | None = None,
    ) -> None:
        """Kick off bounded background consensus warm-up without blocking HTTP response."""
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        cache_key = _consensus_cache_key(sym)
        existing = _INFLIGHT_CONSENSUS.get(cache_key)
        if existing is not None and not existing.done():
            return

        async def _bg() -> None:
            try:
                await self._fetch_raw_consensus_singleflight(
                    sym, last_reported_fy_end=last_reported_fy_end
                )
            except Exception as exc:
                logger.debug("[WATCHLIST_CONSENSUS] background warmup failed for %s: %s", sym, exc)

        asyncio.create_task(_bg())

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

        cached_dict, is_fresh = await _read_consensus_cache(sym)
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
        raw_q_ccy = quote_currency or rec.quote_currency or infer_quote_currency(sym)
        q_ccy, _ = _normalize_currency(raw_q_ccy)
        est_ccy, _ = _normalize_currency(rec.estimate_currency or raw_q_ccy)

        fx_rate: float | None = 1.0
        fx_stale = False
        if est_ccy and q_ccy and est_ccy != q_ccy:
            cached_fx, fx_fresh = await _read_fx_cache(est_ccy, q_ccy)
            if cached_fx is None:
                return None, True
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
        )
        return val, (not is_fresh) or fx_stale

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

        earnings_meta = (
            coverage_row.get("earnings")
            if isinstance(coverage_row, Mapping) and isinstance(coverage_row.get("earnings"), Mapping)
            else {}
        )
        last_reported_fy_end = (
            str(earnings_meta.get("last_fiscal_year_end") or "").strip()[:10] or None
        )

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

        raw_q_ccy = quote_currency or rec.quote_currency or infer_quote_currency(sym)
        q_ccy, _ = _normalize_currency(raw_q_ccy)
        est_ccy, _ = _normalize_currency(rec.estimate_currency or raw_q_ccy)

        fx_rate: float | None = 1.0
        fx_stale = False
        fx_rate_limited = False
        if (
            est_ccy
            and q_ccy
            and est_ccy != q_ccy
            and (rec.fy1_eps is not None or rec.fy2_eps is not None)
        ):
            # Only fetch FX when share unit will actually be converted
            row_adr_ratio = _extract_row_adr_ratio(coverage_row)
            is_adr = _is_adr_security(
                sym,
                quote_type=rec.quote_type,
                security_name=rec.security_name,
                quote_currency=q_ccy,
                estimate_currency=est_ccy,
                row_adr_ratio=row_adr_ratio,
                is_adr_flag=rec.is_adr,
            )
            needs_fx = (not is_adr) or (
                row_adr_ratio is not None or sym in VERIFIED_ADR_SHARES_PER_UNIT
            )
            if needs_fx:
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
    "WatchlistConsensusAdapter",
    "_clear_consensus_cache_for_tests",
    "infer_quote_currency",
    "select_fmp_fy1_fy2_estimates",
]
