"""Public-data watchlist metrics service with chunking, bounded concurrency, Redis/memory caching, and progressive warm-up (§6.1–§6.2)."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.dalton.database import coverage as coverage_db
from src.dalton.watchlist.consensus import (
    WatchlistConsensusAdapter,
    _clear_consensus_cache_for_tests,
)
from src.dalton.watchlist.indicators import compute_symbol_metrics

logger = logging.getLogger(__name__)

MAX_SNAPSHOT_CHUNK = 200
MAX_BAR_CONCURRENCY = 8
DAILY_BARS_CACHE_TTL_SECONDS = 4 * 3600  # 4 hours (§6.2)
MARKET_CAP_CACHE_TTL_SECONDS = 24 * 3600  # 24 hours

_BARS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_MARKET_CAP_CACHE: dict[str, tuple[float, float]] = {}
_INFLIGHT_BARS: dict[str, asyncio.Task[list[dict[str, Any]]]] = {}
_INFLIGHT_MCAP: dict[str, asyncio.Task[float | None]] = {}
_BARS_SEMAPHORE: asyncio.Semaphore | None = None


def _get_bars_semaphore() -> asyncio.Semaphore:
    global _BARS_SEMAPHORE
    if _BARS_SEMAPHORE is None:
        _BARS_SEMAPHORE = asyncio.Semaphore(MAX_BAR_CONCURRENCY)
    return _BARS_SEMAPHORE


def _clear_metrics_cache_for_tests() -> None:
    _BARS_CACHE.clear()
    _MARKET_CAP_CACHE.clear()
    _INFLIGHT_BARS.clear()
    _INFLIGHT_MCAP.clear()
    _clear_consensus_cache_for_tests()


def _bars_cache_key(
    symbol: str,
    *,
    provider_name: str = "default",
    split_adjusted: bool = True,
) -> str:
    sym = str(symbol or "").strip().upper()
    prov = str(provider_name or "default").strip().lower()
    return f"dalton:watchlist:bars:v1:{prov}:adj={int(bool(split_adjusted))}:{sym}"


async def _get_redis_cache_client() -> Any:
    try:
        from src.utils.cache.redis_cache import get_cache_client

        client = get_cache_client()
        if client is not None and getattr(client, "enabled", False):
            return client
    except Exception:
        pass
    return None


async def _read_bars_from_cache(cache_key: str) -> list[dict[str, Any]] | None:
    now = time.monotonic()
    mem = _BARS_CACHE.get(cache_key)
    if mem is not None and (now - mem[0]) < DAILY_BARS_CACHE_TTL_SECONDS and mem[1]:
        return mem[1]

    redis_client = await _get_redis_cache_client()
    if redis_client is not None:
        try:
            raw = await redis_client.get(cache_key)
            if isinstance(raw, str) and raw:
                parsed = json.loads(raw)
                if isinstance(parsed, list) and parsed:
                    cleaned = [dict(b) for b in parsed if isinstance(b, dict)]
                    if cleaned:
                        _BARS_CACHE[cache_key] = (now, cleaned)
                        return cleaned
            elif isinstance(raw, list) and raw:
                cleaned = [dict(b) for b in raw if isinstance(b, dict)]
                if cleaned:
                    _BARS_CACHE[cache_key] = (now, cleaned)
                    return cleaned
        except Exception:
            pass
    return None


async def _write_bars_to_cache(cache_key: str, bars: list[dict[str, Any]]) -> None:
    # Never cache empty bar lists or transient errors
    if not bars:
        return
    _BARS_CACHE[cache_key] = (time.monotonic(), bars)
    redis_client = await _get_redis_cache_client()
    if redis_client is not None:
        try:
            await redis_client.set(
                cache_key,
                json.dumps(bars, ensure_ascii=False, default=str),
                ttl=DAILY_BARS_CACHE_TTL_SECONDS,
            )
        except Exception:
            pass


def _extract_next_earnings_map(mirror_row: dict[str, Any] | None) -> dict[str, str]:
    """Read upcoming earnings dates per ticker from the user's personal calendar mirror.

    Supports both ``entries`` keyed by ticker (canonical ``calendar_state.py`` format)
    and list-shaped mirrors, reading confirmed ``date`` as well as ``tentative_date``.
    """
    out: dict[str, str] = {}
    if not isinstance(mirror_row, dict):
        return out
    ledger = mirror_row.get("ledger")
    if not isinstance(ledger, dict):
        return out
    entries = ledger.get("entries") or ledger.get("companies") or {}
    today_iso = date.today().isoformat()

    pairs: list[tuple[str, dict[str, Any]]] = []
    if isinstance(entries, dict):
        for key_sym, item in entries.items():
            if isinstance(item, dict):
                pairs.append((str(item.get("ticker") or key_sym or "").strip().upper(), item))
    elif isinstance(entries, list):
        for item in entries:
            if isinstance(item, dict):
                pairs.append((str(item.get("ticker") or "").strip().upper(), item))

    for sym, item in pairs:
        if not sym:
            continue
        dt = str(
            item.get("earnings_date")
            or item.get("date")
            or item.get("next_earnings_date")
            or item.get("tentative_date")
            or ""
        ).strip()
        if len(dt) < 10:
            continue
        dt_iso = dt[:10]
        if dt_iso >= today_iso:
            prev = out.get(sym)
            if prev is None or dt_iso < prev:
                out[sym] = dt_iso
    return out


async def _resolve_provider_and_key() -> tuple[Any, str, bool]:
    from src.data_client import get_market_data_provider

    provider = await get_market_data_provider()
    prov_name = (
        str(getattr(provider, "name", "") or type(provider).__name__ or "default").strip().lower()
    )
    split_adj = bool(getattr(provider, "split_adjusted", True))
    return provider, prov_name, split_adj


async def _fetch_daily_bars_singleflight(
    symbol: str,
    *,
    user_id: str,
    provider: Any = None,
    provider_name: str = "default",
    split_adjusted: bool = True,
) -> list[dict[str, Any]]:
    cache_key = _bars_cache_key(
        symbol,
        provider_name=provider_name,
        split_adjusted=split_adjusted,
    )
    cached = await _read_bars_from_cache(cache_key)
    if cached is not None:
        return cached

    existing_task = _INFLIGHT_BARS.get(cache_key)
    if existing_task is not None and not existing_task.done():
        return await existing_task

    async def _load() -> list[dict[str, Any]]:
        sem = _get_bars_semaphore()
        async with sem:
            eff_provider = provider
            if eff_provider is None:
                eff_provider, _, _ = await _resolve_provider_and_key()
            from_dt = (date.today() - timedelta(days=420)).isoformat()
            to_dt = date.today().isoformat()
            bars = await eff_provider.get_daily(
                symbol,
                from_date=from_dt,
                to_date=to_dt,
                is_index=False,
                user_id=user_id,
            )
            cleaned = [dict(b) for b in (bars or []) if isinstance(b, dict)]
            if cleaned:
                await _write_bars_to_cache(cache_key, cleaned)
            return cleaned

    task = asyncio.create_task(_load())
    _INFLIGHT_BARS[cache_key] = task

    def _cleanup(_t: asyncio.Task[Any]) -> None:
        if _INFLIGHT_BARS.get(cache_key) is task:
            _INFLIGHT_BARS.pop(cache_key, None)

    task.add_done_callback(_cleanup)
    return await task


def _schedule_bars_warmup(
    symbol: str,
    *,
    user_id: str,
    provider: Any = None,
    provider_name: str = "default",
    split_adjusted: bool = True,
) -> None:
    """Kick off bounded background bar warm-up without blocking the HTTP response."""
    cache_key = _bars_cache_key(
        symbol,
        provider_name=provider_name,
        split_adjusted=split_adjusted,
    )
    existing_task = _INFLIGHT_BARS.get(cache_key)
    if existing_task is not None and not existing_task.done():
        return

    async def _bg() -> list[dict[str, Any]]:
        try:
            sem = _get_bars_semaphore()
            async with sem:
                eff_provider = provider
                if eff_provider is None:
                    eff_provider, _, _ = await _resolve_provider_and_key()
                from_dt = (date.today() - timedelta(days=420)).isoformat()
                to_dt = date.today().isoformat()
                bars = await eff_provider.get_daily(
                    symbol,
                    from_date=from_dt,
                    to_date=to_dt,
                    is_index=False,
                    user_id=user_id,
                )
                cleaned = [dict(b) for b in (bars or []) if isinstance(b, dict)]
                if cleaned:
                    await _write_bars_to_cache(cache_key, cleaned)
                return cleaned
        except Exception as exc:
            logger.debug("[WATCHLIST_METRICS] background bars warmup failed for %s: %s", symbol, exc)
            return []

    task = asyncio.create_task(_bg())
    _INFLIGHT_BARS[cache_key] = task

    def _cleanup(_t: asyncio.Task[Any]) -> None:
        if _INFLIGHT_BARS.get(cache_key) is task:
            _INFLIGHT_BARS.pop(cache_key, None)

    task.add_done_callback(_cleanup)


def _positive_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) and num > 0 else None


async def _fetch_market_cap_cached(
    symbol: str,
    *,
    provider: Any = None,
    user_id: str = "",
) -> float | None:
    """Best-effort cacheable market-cap lookup via provider profile (never caches errors)."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    now = time.monotonic()
    cached = _MARKET_CAP_CACHE.get(sym)
    if cached is not None and (now - cached[0]) < MARKET_CAP_CACHE_TTL_SECONDS:
        return cached[1]

    existing = _INFLIGHT_MCAP.get(sym)
    if existing is not None and not existing.done():
        return await existing

    async def _load_mcap() -> float | None:
        try:
            eff_provider = provider
            if eff_provider is None:
                eff_provider, _, _ = await _resolve_provider_and_key()
            val: float | None = None
            for method_name in ("get_company_profile", "get_profile", "get_quote"):
                fn = getattr(eff_provider, method_name, None)
                if callable(fn):
                    try:
                        res = await fn(sym, user_id=user_id)
                    except TypeError:
                        res = await fn(sym)
                    if isinstance(res, list) and res:
                        res = res[0]
                    if isinstance(res, Mapping):
                        val = _positive_float(
                            res.get("market_cap")
                            or res.get("marketCap")
                            or res.get("mktCap")
                        )
                        if val is not None:
                            break
            if val is not None:
                _MARKET_CAP_CACHE[sym] = (time.monotonic(), val)
            return val
        except Exception:
            return None

    task = asyncio.create_task(_load_mcap())
    _INFLIGHT_MCAP[sym] = task

    def _cleanup(_t: asyncio.Task[Any]) -> None:
        if _INFLIGHT_MCAP.get(sym) is task:
            _INFLIGHT_MCAP.pop(sym, None)

    task.add_done_callback(_cleanup)
    return await task


async def _fetch_snapshots_chunked(
    symbols: Sequence[str],
    *,
    user_id: str,
    provider: Any = None,
) -> dict[str, dict[str, Any]]:
    """Fetch snapshots in chunks of <=200 using the public/configured market data provider."""
    if not symbols:
        return {}
    out: dict[str, dict[str, Any]] = {}
    eff_provider = provider
    if eff_provider is None:
        try:
            eff_provider, _, _ = await _resolve_provider_and_key()
        except Exception as exc:
            logger.debug("[WATCHLIST_METRICS] provider unavailable: %s", exc)
            return out

    deduped = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
    for i in range(0, len(deduped), MAX_SNAPSHOT_CHUNK):
        chunk = deduped[i : i + MAX_SNAPSHOT_CHUNK]
        try:
            snaps = await eff_provider.get_snapshots(chunk, asset_type="stocks", user_id=user_id)
        except Exception as exc:
            logger.debug("[WATCHLIST_METRICS] snapshot chunk failed: %s", exc)
            continue
        for snap in snaps or []:
            if not isinstance(snap, dict):
                continue
            sym = str(snap.get("symbol") or snap.get("ticker") or "").strip().upper()
            if sym:
                out[sym] = snap
    return out


def _build_full_roster_summary(
    items: Mapping[str, Mapping[str, Any]],
    rows_by_ticker: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Compute compact full-roster statistics (usable by right-panel chat beyond 120 rows)."""
    tier_counts = {"core": 0, "watch": 0, "radar": 0}
    listed_count = 0
    private_count = 0
    rsi_entries: list[tuple[float, str]] = []
    chg1d_entries: list[tuple[float, str]] = []
    upcoming_earnings: list[tuple[str, str]] = []

    for sym, item in items.items():
        row = rows_by_ticker.get(sym) or {}
        tier = str(row.get("tier") or "").strip().lower()
        if tier in tier_counts:
            tier_counts[tier] += 1
        if item.get("is_private"):
            private_count += 1
            continue
        listed_count += 1
        rsi = item.get("rsi_14d")
        if isinstance(rsi, (int, float)) and math.isfinite(float(rsi)):
            rsi_entries.append((float(rsi), sym))
        c1d = item.get("chg_1d")
        if isinstance(c1d, (int, float)) and math.isfinite(float(c1d)):
            chg1d_entries.append((float(c1d), sym))
        ne = item.get("next_earnings")
        if isinstance(ne, str) and len(ne) >= 10:
            upcoming_earnings.append((ne[:10], sym))

    rsi_entries.sort(key=lambda x: x[0])
    chg1d_entries.sort(key=lambda x: x[0])
    upcoming_earnings.sort(key=lambda x: (x[0], x[1]))

    return {
        "total_count": len(items),
        "listed_count": listed_count,
        "private_count": private_count,
        "tier_counts": tier_counts,
        "lowest_rsi_14d": [{"ticker": sym, "rsi_14d": val} for val, sym in rsi_entries[:5]],
        "highest_rsi_14d": [{"ticker": sym, "rsi_14d": val} for val, sym in reversed(rsi_entries[-5:])],
        "top_gainers_1d": [{"ticker": sym, "chg_1d": val} for val, sym in reversed(chg1d_entries[-5:])],
        "top_losers_1d": [{"ticker": sym, "chg_1d": val} for val, sym in chg1d_entries[:5]],
        "upcoming_earnings": [{"ticker": sym, "date": dt} for dt, sym in upcoming_earnings[:10]],
    }


class WatchlistMetricsService:
    """Compute metrics + consensus valuation for a user's personal watchlist (works on Standalone Core)."""

    def __init__(self, consensus_adapter: WatchlistConsensusAdapter | None = None) -> None:
        self._consensus = consensus_adapter or WatchlistConsensusAdapter()

    async def get_metrics_for_user(
        self,
        user_id: str,
        *,
        symbols: Sequence[str] | None = None,
        wait_for_cold: bool = False,
        include_valuation: bool = True,
    ) -> dict[str, Any]:
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id is required")

        rows = await coverage_db.list_coverage_rows(uid)
        rows_by_ticker = {
            str(r["ticker"]).strip().upper(): r
            for r in rows
            if isinstance(r, dict) and r.get("ticker")
        }

        if symbols is not None:
            requested = list(
                dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip())
            )
        else:
            requested = list(rows_by_ticker.keys())

        mirror_row = await coverage_db.get_earnings_calendar_mirror(uid)
        earnings_by_ticker = _extract_next_earnings_map(mirror_row)

        items: dict[str, dict[str, Any]] = {}
        listed_symbols: list[str] = []
        for sym in requested:
            row = rows_by_ticker.get(sym)
            if row is not None and bool(row.get("is_private", False)):
                unlisted_val = (
                    await self._consensus.get_valuation(
                        sym, latest_price=None, coverage_row=row, is_private=True
                    )
                    if include_valuation
                    else None
                )
                items[sym] = {
                    "ticker": sym,
                    "is_private": True,
                    "status": "unlisted",
                    "price": None,
                    "currency": None,
                    "chg_1d": None,
                    "chg_1w": None,
                    "chg_1m": None,
                    "chg_3m": None,
                    "chg_ytd": None,
                    "chg_1y": None,
                    "high_52w": None,
                    "low_52w": None,
                    "position_52w": None,
                    "rsi_14d": None,
                    "rsi_30d": None,
                    "vol_ratio_5_20": None,
                    "market_cap": None,
                    "next_earnings": None,
                    "pe_fy1": None,
                    "pe_fy2": None,
                    "eps_growth_fy2": None,
                    "valuation": unlisted_val,
                }
            else:
                listed_symbols.append(sym)

        provider: Any = None
        provider_name = "default"
        split_adjusted = True
        try:
            provider, provider_name, split_adjusted = await _resolve_provider_and_key()
        except Exception as exc:
            logger.debug("[WATCHLIST_METRICS] provider unavailable: %s", exc)

        snaps = await _fetch_snapshots_chunked(listed_symbols, user_id=uid, provider=provider)
        pending_symbols: list[str] = []
        pending_valuation_symbols: list[str] = []

        async def _compute_one(sym: str) -> tuple[str, dict[str, Any]]:
            snap = snaps.get(sym) or {}
            row = rows_by_ticker.get(sym) or {}
            extra_earn = row.get("earnings") if isinstance(row.get("earnings"), dict) else {}
            extra_meta = row.get("extra") if isinstance(row.get("extra"), dict) else {}
            next_earn = earnings_by_ticker.get(sym) or (
                str(extra_earn.get("next_date") or "").strip()[:10] or None
            )

            cache_key = _bars_cache_key(
                sym,
                provider_name=provider_name,
                split_adjusted=split_adjusted,
            )
            bars: list[dict[str, Any]] = []
            cached_bars = await _read_bars_from_cache(cache_key)
            if cached_bars is not None:
                bars = cached_bars
            elif wait_for_cold:
                try:
                    bars = await _fetch_daily_bars_singleflight(
                        sym,
                        user_id=uid,
                        provider=provider,
                        provider_name=provider_name,
                        split_adjusted=split_adjusted,
                    )
                except Exception as exc:
                    logger.debug("[WATCHLIST_METRICS] bars failed for %s: %s", sym, exc)
                    bars = []
                    pending_symbols.append(sym)
            else:
                # Non-blocking progressive response: return live quote immediately & warm bars in background
                pending_symbols.append(sym)
                if provider is not None:
                    _schedule_bars_warmup(
                        sym,
                        user_id=uid,
                        provider=provider,
                        provider_name=provider_name,
                        split_adjusted=split_adjusted,
                    )

            mcap = (
                _positive_float(snap.get("market_cap"))
                or _positive_float(snap.get("marketCap"))
                or _positive_float(extra_meta.get("market_cap"))
            )
            if mcap is None and provider is not None:
                now_m = time.monotonic()
                mcap_cached = _MARKET_CAP_CACHE.get(sym)
                if mcap_cached is not None and (now_m - mcap_cached[0]) < MARKET_CAP_CACHE_TTL_SECONDS:
                    mcap = mcap_cached[1]
                elif wait_for_cold:
                    mcap = await _fetch_market_cap_cached(sym, provider=provider, user_id=uid)
                else:
                    # Warm market_cap opportunistically without blocking response
                    if sym not in _INFLIGHT_MCAP or _INFLIGHT_MCAP[sym].done():
                        asyncio.create_task(_fetch_market_cap_cached(sym, provider=provider, user_id=uid))

            quote_ts = (
                snap.get("timestamp")
                or snap.get("as_of")
                or snap.get("last_trade_at")
                or snap.get("updated_at")
            )

            metric = compute_symbol_metrics(
                bars,
                symbol=sym,
                latest_price=snap.get("price") or snap.get("last_price") or snap.get("close"),
                day_high=snap.get("day_high") or snap.get("high"),
                day_low=snap.get("day_low") or snap.get("low"),
                prev_close=snap.get("previous_close") or snap.get("prev_close"),
                chg_1d_override=snap.get("change_percent") or snap.get("changesPercentage"),
                currency=snap.get("currency"),
                market_cap=mcap,
                next_earnings=next_earn,
                quote_timestamp=quote_ts,
            )

            valuation: dict[str, Any] | None = None
            if include_valuation:
                if wait_for_cold:
                    try:
                        valuation = await self._consensus.get_valuation(
                            sym,
                            latest_price=metric.get("price"),
                            quote_currency=metric.get("currency"),
                            coverage_row=row,
                            is_private=False,
                        )
                        if isinstance(valuation, dict) and valuation.get("status") == "loading":
                            pending_valuation_symbols.append(sym)
                    except Exception as exc:
                        logger.debug("[WATCHLIST_METRICS] valuation failed for %s: %s", sym, exc)
                else:
                    valuation, needs_val_warmup = await self._consensus.get_cached_valuation(
                        sym,
                        latest_price=metric.get("price"),
                        quote_currency=metric.get("currency"),
                        coverage_row=row,
                        is_private=False,
                    )
                    if needs_val_warmup:
                        pending_valuation_symbols.append(sym)
                        self._consensus.schedule_warmup(
                            sym,
                            quote_currency=metric.get("currency"),
                            coverage_row=row,
                        )

            status = (
                "ok"
                if metric.get("price") is not None
                else ("warming" if sym in pending_symbols else "unavailable")
            )
            return sym, {
                "ticker": sym,
                "is_private": False,
                "status": status,
                **metric,
                "pe_fy1": valuation.get("pe_fy1") if isinstance(valuation, dict) else None,
                "pe_fy2": valuation.get("pe_fy2") if isinstance(valuation, dict) else None,
                "eps_growth_fy2": (
                    valuation.get("eps_growth_fy2") if isinstance(valuation, dict) else None
                ),
                "valuation": valuation,
            }

        results = await asyncio.gather(*(_compute_one(sym) for sym in listed_symbols))
        for sym, payload in results:
            items[sym] = payload

        return {
            "ok": True,
            "items": items,
            "pending_symbols": pending_symbols,
            "pending_valuation_symbols": pending_valuation_symbols,
            "summary": _build_full_roster_summary(items, rows_by_ticker),
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
