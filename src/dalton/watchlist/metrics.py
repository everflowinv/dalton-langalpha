"""Public-data watchlist metrics service with chunking, bounded concurrency, and caching (§6.1–§6.2)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.dalton.database import coverage as coverage_db
from src.dalton.watchlist.indicators import compute_symbol_metrics

logger = logging.getLogger(__name__)

MAX_SNAPSHOT_CHUNK = 200
MAX_BAR_CONCURRENCY = 8
DAILY_BARS_CACHE_TTL_SECONDS = 4 * 3600  # 4 hours (§6.2)

_BARS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_INFLIGHT_BARS: dict[str, asyncio.Task[list[dict[str, Any]]]] = {}
_BARS_SEMAPHORE: asyncio.Semaphore | None = None


def _get_bars_semaphore() -> asyncio.Semaphore:
    global _BARS_SEMAPHORE
    if _BARS_SEMAPHORE is None:
        _BARS_SEMAPHORE = asyncio.Semaphore(MAX_BAR_CONCURRENCY)
    return _BARS_SEMAPHORE


def _clear_metrics_cache_for_tests() -> None:
    _BARS_CACHE.clear()
    _INFLIGHT_BARS.clear()


def _extract_next_earnings_map(mirror_row: dict[str, Any] | None) -> dict[str, str]:
    """Read upcoming earnings dates per ticker from the user's personal calendar mirror."""
    out: dict[str, str] = {}
    if not isinstance(mirror_row, dict):
        return out
    ledger = mirror_row.get("ledger")
    if not isinstance(ledger, dict):
        return out
    entries = ledger.get("entries") or ledger.get("companies") or {}
    today_iso = date.today().isoformat()
    if isinstance(entries, dict):
        items = list(entries.values())
    elif isinstance(entries, list):
        items = entries
    else:
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("ticker") or "").strip().upper()
        dt = str(
            item.get("earnings_date")
            or item.get("date")
            or item.get("next_earnings_date")
            or ""
        ).strip()
        if not sym or len(dt) < 10:
            continue
        dt_iso = dt[:10]
        if dt_iso >= today_iso:
            prev = out.get(sym)
            if prev is None or dt_iso < prev:
                out[sym] = dt_iso
    return out


async def _fetch_daily_bars_singleflight(symbol: str, *, user_id: str) -> list[dict[str, Any]]:
    now = time.monotonic()
    cached = _BARS_CACHE.get(symbol)
    if cached is not None and (now - cached[0]) < DAILY_BARS_CACHE_TTL_SECONDS:
        return cached[1]

    existing_task = _INFLIGHT_BARS.get(symbol)
    if existing_task is not None and not existing_task.done():
        return await existing_task

    async def _load() -> list[dict[str, Any]]:
        sem = _get_bars_semaphore()
        async with sem:
            from src.data_client import get_market_data_provider

            provider = await get_market_data_provider()
            from_dt = (date.today() - timedelta(days=420)).isoformat()
            to_dt = date.today().isoformat()
            bars = await provider.get_daily(
                symbol,
                from_date=from_dt,
                to_date=to_dt,
                is_index=False,
                user_id=user_id,
            )
            cleaned = [dict(b) for b in (bars or []) if isinstance(b, dict)]
            _BARS_CACHE[symbol] = (time.monotonic(), cleaned)
            return cleaned

    task = asyncio.create_task(_load())
    _INFLIGHT_BARS[symbol] = task
    try:
        return await task
    finally:
        if _INFLIGHT_BARS.get(symbol) is task:
            _INFLIGHT_BARS.pop(symbol, None)


async def _fetch_snapshots_chunked(
    symbols: Sequence[str],
    *,
    user_id: str,
) -> dict[str, dict[str, Any]]:
    """Fetch snapshots in chunks of <=200 using the public/configured market data provider."""
    if not symbols:
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        from src.data_client import get_market_data_provider

        provider = await get_market_data_provider()
    except Exception as exc:
        logger.debug("[WATCHLIST_METRICS] provider unavailable: %s", exc)
        return out

    deduped = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
    for i in range(0, len(deduped), MAX_SNAPSHOT_CHUNK):
        chunk = deduped[i : i + MAX_SNAPSHOT_CHUNK]
        try:
            snaps = await provider.get_snapshots(chunk, asset_type="stocks", user_id=user_id)
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


class WatchlistMetricsService:
    """Compute Batch-1 metrics for a user's personal watchlist (works on Standalone Core)."""

    async def get_metrics_for_user(
        self,
        user_id: str,
        *,
        symbols: Sequence[str] | None = None,
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
                }
            else:
                listed_symbols.append(sym)

        snaps = await _fetch_snapshots_chunked(listed_symbols, user_id=uid)

        pending_symbols: list[str] = []

        async def _compute_one(sym: str) -> tuple[str, dict[str, Any]]:
            snap = snaps.get(sym) or {}
            row = rows_by_ticker.get(sym) or {}
            extra_earn = row.get("earnings") if isinstance(row.get("earnings"), dict) else {}
            next_earn = earnings_by_ticker.get(sym) or (
                str(extra_earn.get("next_date") or "").strip()[:10] or None
            )
            try:
                bars = await _fetch_daily_bars_singleflight(sym, user_id=uid)
            except Exception as exc:
                logger.debug("[WATCHLIST_METRICS] bars failed for %s: %s", sym, exc)
                bars = []
                pending_symbols.append(sym)

            metric = compute_symbol_metrics(
                bars,
                latest_price=snap.get("price") or snap.get("last_price") or snap.get("close"),
                day_high=snap.get("day_high") or snap.get("high"),
                day_low=snap.get("day_low") or snap.get("low"),
                prev_close=snap.get("previous_close") or snap.get("prev_close"),
                chg_1d_override=snap.get("change_percent") or snap.get("changesPercentage"),
                currency=snap.get("currency"),
                market_cap=snap.get("market_cap") or snap.get("marketCap"),
                next_earnings=next_earn,
            )
            return sym, {
                "ticker": sym,
                "is_private": False,
                "status": "ok" if metric.get("price") is not None else "unavailable",
                **metric,
            }

        results = await asyncio.gather(*(_compute_one(sym) for sym in listed_symbols))
        for sym, payload in results:
            items[sym] = payload

        return {
            "ok": True,
            "items": items,
            "pending_symbols": pending_symbols,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
