"""Service layer for per-user coverage roster, rule groups, Home YAML rendering, and earnings calendar mirror."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from src.dalton.coverage.models import (
    build_coverage_doc,
    company_to_row,
    dump_coverage_yaml,
    extract_row_group_ids,
    infer_market,
    make_new_company_dict,
    normalize_source,
    normalize_tier,
    row_to_company,
    validate_earnings_calendar_ledger,
)
from src.dalton.database import coverage as coverage_db

logger = logging.getLogger(__name__)

GROUP_REFRESH_INTERVAL_DAYS = 85

US_MAINBOARD_EXCHANGES: tuple[str, ...] = ("NMS", "NYQ", "NGM", "NCM", "ASE")
MAINBOARD_EXCHANGES: frozenset[str] = frozenset(
    {
        *US_MAINBOARD_EXCHANGES,
        "PCX",
        "BTS",
        "HKG",
        "SHH",
        "SHZ",
        "NASDAQ",
        "NYSE",
        "AMEX",
        "HKSE",
        "HKEX",
        "SSE",
        "SZSE",
    }
)
OTC_EXCHANGES: frozenset[str] = frozenset(
    {"PNK", "OQB", "OQX", "OTC", "OTCMKTS", "YHD", "OEM", "NAE", "GREY"}
)


class CoverageDataSourceError(RuntimeError):
    """Structured error raised when market data providers (yfinance / FMP) fail."""

    def __init__(
        self,
        provider: str,
        reason: str,
        *,
        status_code: int | None = None,
    ) -> None:
        self.provider = str(provider or "yfinance")
        self.reason = str(reason or "upstream data source unavailable")
        self.status_code = status_code
        super().__init__(f"{self.provider} data source error: {self.reason}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "data_source_unavailable",
            "provider": self.provider,
            "reason": self.reason,
            "status_code": self.status_code,
            "message": str(self),
        }


def _is_pool_not_open_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "pool is not open" in msg
        or "not initialized" in msg
        or "connection pool" in msg
        or "no running event loop" in msg
    )


def _run_coro_sync(coro: Any) -> Any:
    """Run an async coroutine from a synchronous caller, handling active event loops safely."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result(timeout=15)


# ---------------------------------------------------------------------------
# Item 2: Per-user coverage read functions
# ---------------------------------------------------------------------------


async def get_user_coverage(
    user_id: str | None,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Return the user's coverage document in the exact structure of `coverage.yaml`."""
    uid = str(user_id or "").strip()
    if not uid:
        return build_coverage_doc([])
    try:
        rows = await coverage_db.list_coverage_rows(uid, conn=conn)
    except Exception as exc:
        if _is_pool_not_open_error(exc):
            logger.debug("[DALTON_COVERAGE] DB pool not open in get_user_coverage(%s): %s", uid, exc)
            return build_coverage_doc([])
        raise

    companies = [row_to_company(r) for r in rows]
    latest_ts: str | None = None
    for r in rows:
        u_at = r.get("updated_at")
        if isinstance(u_at, datetime):
            iso = u_at.astimezone(timezone.utc).isoformat(timespec="seconds")
            if latest_ts is None or iso > latest_ts:
                latest_ts = iso
        elif isinstance(u_at, str) and u_at:
            if latest_ts is None or u_at > latest_ts:
                latest_ts = u_at

    as_of = latest_ts[:10] if latest_ts and len(latest_ts) >= 10 else date.today().isoformat()
    return build_coverage_doc(companies, as_of=as_of, generated_at=latest_ts)


async def get_user_tier(
    user_id: str | None,
    ticker: str,
    *,
    conn: Any = None,
) -> str | None:
    """Return `'core'` or `'watch'` if `ticker` is in `user_id`'s coverage, else `None`."""
    uid = str(user_id or "").strip()
    sym = str(ticker or "").strip().upper()
    if not uid or not sym:
        return None
    try:
        row = await coverage_db.get_coverage_row(uid, sym, conn=conn)
    except Exception as exc:
        if _is_pool_not_open_error(exc):
            logger.debug("[DALTON_COVERAGE] DB pool not open in get_user_tier(%s, %s): %s", uid, sym, exc)
            return None
        raise
    if row is None:
        return None
    tier = str(row.get("tier") or "").strip().lower()
    return tier if tier in ("core", "watch") else None


def _extract_aliases_from_rows(
    rows: Sequence[Mapping[str, Any]],
    base_aliases: Mapping[str, tuple[str, ...]] | None = None,
) -> dict[str, tuple[str, ...]]:
    merged: dict[str, list[str]] = {
        k: list(v) for k, v in (base_aliases or {}).items()
    }
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        bucket = merged.setdefault(ticker, [])
        comp_name = str(row.get("name") or "").strip()
        if comp_name and comp_name.upper() != ticker and comp_name not in bucket:
            bucket.append(comp_name)
        aliases_obj = row.get("aliases")
        if isinstance(aliases_obj, Mapping):
            for s_name in aliases_obj.get("search_names") or []:
                s_clean = str(s_name or "").strip()
                if s_clean and s_clean.upper() != ticker and s_clean not in bucket:
                    bucket.append(s_clean)
            for key in ("adr", "earnings_tracker"):
                alt = str(aliases_obj.get(key) or "").strip()
                if alt and alt.upper() != ticker and alt not in bucket:
                    bucket.append(alt)
    return {k: tuple(v) for k, v in merged.items() if v}


async def get_user_aliases(
    user_id: str | None = None,
    *,
    conn: Any = None,
    include_supplemental: bool = True,
) -> dict[str, tuple[str, ...]]:
    """Return ticker -> alias tuple mapping for `user_id`.

    When `user_id` is None/empty (e.g. caller has no owner context), returns the
    union of all users' coverage aliases merged with `SUPPLEMENTAL_TICKER_ALIASES`.
    """
    base: dict[str, tuple[str, ...]] = {}
    if include_supplemental:
        try:
            from src.dalton.conversations.extractor import SUPPLEMENTAL_TICKER_ALIASES

            base = dict(SUPPLEMENTAL_TICKER_ALIASES)
        except Exception:
            base = {}

    uid = str(user_id or "").strip()
    try:
        if uid:
            rows = await coverage_db.list_coverage_rows(uid, conn=conn)
        else:
            rows = await coverage_db.list_all_coverage_rows(conn=conn)
    except Exception as exc:
        if _is_pool_not_open_error(exc):
            return base
        logger.warning("[DALTON_COVERAGE] get_user_aliases failed for user=%r: %s", uid or "*", exc)
        return base

    return _extract_aliases_from_rows(rows, base_aliases=base)


def get_user_coverage_sync(user_id: str | None) -> dict[str, Any]:
    """Synchronous wrapper around `get_user_coverage`."""
    try:
        return _run_coro_sync(get_user_coverage(user_id))
    except Exception as exc:
        if not _is_pool_not_open_error(exc):
            logger.warning("[DALTON_COVERAGE] get_user_coverage_sync failed: %s", exc)
        return build_coverage_doc([])


def get_user_tier_sync(user_id: str | None, ticker: str) -> str | None:
    """Synchronous wrapper around `get_user_tier`."""
    try:
        return _run_coro_sync(get_user_tier(user_id, ticker))
    except Exception as exc:
        if not _is_pool_not_open_error(exc):
            logger.warning("[DALTON_COVERAGE] get_user_tier_sync failed: %s", exc)
        return None


def get_user_aliases_sync(
    user_id: str | None = None,
    *,
    include_supplemental: bool = True,
) -> dict[str, tuple[str, ...]]:
    """Synchronous wrapper around `get_user_aliases`."""
    try:
        return _run_coro_sync(
            get_user_aliases(user_id, include_supplemental=include_supplemental)
        )
    except Exception as exc:
        if not _is_pool_not_open_error(exc):
            logger.warning("[DALTON_COVERAGE] get_user_aliases_sync failed: %s", exc)
        if include_supplemental:
            try:
                from src.dalton.conversations.extractor import SUPPLEMENTAL_TICKER_ALIASES

                return dict(SUPPLEMENTAL_TICKER_ALIASES)
            except Exception:
                return {}
        return {}


# ---------------------------------------------------------------------------
# Item 3: Render Home/dalton/coverage.yaml from DB & sync to Home sandbox
# ---------------------------------------------------------------------------


async def render_coverage_yaml(user_id: str, *, conn: Any = None) -> str:
    """Render `coverage.yaml` YAML string for `user_id` from `dalton_coverage`."""
    doc = await get_user_coverage(user_id, conn=conn)
    return dump_coverage_yaml(doc)


async def _resolve_home_sandbox_backend(user_id: str) -> Any:
    """Resolve a `SandboxBackend` for `user_id`'s Home workspace if reachable."""
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.core.paths import SandboxLayout
    from src.dalton.auth import resolve_caller_home_workspace_id
    from src.server.database.workspace import get_workspace
    from src.server.services.workspace_manager import WorkspaceManager

    home_ws_id = await resolve_caller_home_workspace_id(user_id)
    workspace_manager = WorkspaceManager.get_instance()
    session = await workspace_manager.get_session_for_workspace(
        str(home_ws_id), user_id=user_id
    )
    if not hasattr(session, "sandbox") or session.sandbox is None:
        raise RuntimeError(f"Sandbox session unavailable for Home workspace {home_ws_id}")
    row = await get_workspace(str(home_ws_id)) or {}
    dir_name = row.get("dir_name")
    layout = SandboxLayout(session.sandbox.working_dir).for_workspace(dir_name)
    return SandboxBackend(session.sandbox, layout.workspace)


async def sync_user_coverage_to_home(
    user_id: str,
    *,
    yaml_text: str | None = None,
    backend: Any = None,
    rel_path: str = "dalton/coverage.yaml",
) -> bool:
    """Best-effort write of rendered `coverage.yaml` into `user_id`'s Home sandbox.

    Logs and returns False on failure without raising.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return False
    try:
        rendered = yaml_text if yaml_text is not None else await render_coverage_yaml(uid)
        eff_backend = backend or await _resolve_home_sandbox_backend(uid)
        if hasattr(eff_backend, "awrite_text"):
            wrote = await eff_backend.awrite_text(rel_path, rendered)
            if wrote is False:
                raise RuntimeError(f"awrite_text returned False for {rel_path}")
        elif hasattr(eff_backend, "awrite"):
            await eff_backend.awrite(rel_path, rendered)
        elif hasattr(eff_backend, "write"):
            eff_backend.write(rel_path, rendered)
        else:
            raise RuntimeError("Unsupported sandbox backend interface")
        return True
    except Exception as exc:
        logger.info(
            "[DALTON_COVERAGE] skipped or failed writing Home/%s for user=%s: %s",
            rel_path,
            uid,
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Item 5: Auto-create per-user `dalton-earnings-calendar` Home automation
# ---------------------------------------------------------------------------


async def ensure_user_earnings_calendar_automation(user_id: str) -> dict[str, Any]:
    """Idempotently create `dalton-earnings-calendar` in `user_id`'s Home (Sat 09:00 Asia/Shanghai).

    If `dalton-earnings-calendar` already exists for `user_id`, does not recreate or overwrite it.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return {"ok": False, "error": "missing_user_id"}
    try:
        from src.dalton.autonomy.schedule import (
            build_global_director_specs,
            list_all_user_automations,
            schedule_dalton_automation_helper,
        )

        existing_rows = await list_all_user_automations(
            uid,
            name_prefix="dalton-earnings-calendar",
        )
        existing = next(
            (
                r
                for r in existing_rows
                if str(r.get("name") or "").strip().lower() == "dalton-earnings-calendar"
            ),
            None,
        )
        if existing is not None:
            return {
                "ok": True,
                "action": "exists",
                "automation_id": str(existing.get("automation_id") or ""),
                "name": "dalton-earnings-calendar",
            }

        specs = build_global_director_specs(timezone_name="Asia/Shanghai")
        cal_spec = next(
            (s for s in specs if s.get("name") == "dalton-earnings-calendar"),
            None,
        )
        if cal_spec is None:
            cal_spec = {
                "name": "dalton-earnings-calendar",
                "description": "Dalton 覆盖池财报日历周度刷新并排期 T-3 / T+0 / T+1",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 6",
                "timezone": "Asia/Shanghai",
                "workspace_id": "home",
                "thread_strategy": "new",
                "instruction": (
                    "请按 `.agents/skills/dalton-earnings-calendar/SKILL.md` 扫描覆盖池未来 21 天的财报披露日，"
                    "更新 `dalton/earnings_calendar.md` 并为即将披露的 Core 公司排期 T-3 / T+0 / T+1 任务。"
                ),
                "metadata": {
                    "dalton_managed": True,
                    "dalton_kind": "calendar",
                },
            }
        else:
            cal_spec = {**cal_spec, "timezone": "Asia/Shanghai", "workspace_id": "home"}

        return await schedule_dalton_automation_helper(
            "dalton-earnings-calendar",
            cal_spec,
            user_id=uid,
            workspace_id="home",
            role="chief_of_staff",
        )
    except Exception as exc:
        logger.warning(
            "[DALTON_COVERAGE] failed ensuring dalton-earnings-calendar automation for user=%s: %s",
            uid,
            exc,
        )
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Item 4: Ticker validation (FMP profile / researchdata quote) & Screener groups
# ---------------------------------------------------------------------------


def _map_exchange_or_country_to_market(
    ticker: str,
    *,
    exchange: str | None = None,
    country: str | None = None,
) -> str:
    sym = str(ticker or "").strip().upper()
    if sym.endswith(".HK"):
        return "HK"
    if sym.endswith((".SS", ".SZ", ".SH")):
        return "CN"
    ex = str(exchange or "").strip().upper()
    ct = str(country or "").strip().upper()
    if ex in ("HKSE", "HKEX", "HKG") or ct == "HK":
        return "HK"
    if ex in ("SHH", "SHZ", "SSE", "SZSE") or ct == "CN":
        return "CN"
    return infer_market(sym)


def _is_ticker_not_found_exception(exc: BaseException) -> bool:
    if isinstance(exc, KeyError):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "404",
            "not found",
            "quote not found",
            "no data found",
            "no fundamentals",
            "delisted",
            "no timezone",
            "currenttradingperiod",
            "may be delisted",
        )
    )


def _extract_http_status(exc: BaseException) -> int | None:
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None) or getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    msg = str(exc)
    for code in (401, 402, 403, 404, 429, 500, 502, 503, 504):
        if str(code) in msg:
            return code
    return None


def _safe_fast_info_get(fi: Any, key: str) -> Any:
    if fi is None:
        return None
    try:
        if hasattr(fi, "get"):
            return fi.get(key)
        return getattr(fi, key, None)
    except Exception:
        return None


def _lookup_via_yfinance_sync(ticker: str) -> dict[str, Any] | None:
    """Synchronous ticker validation and enrichment via `yfinance.Ticker`."""
    from market_protocol import display_spelling
    from src.data_client.yfinance.yahoo import yahoo_ticker

    sym = str(ticker or "").strip().upper()
    if not sym:
        return None

    try:
        tk = yahoo_ticker(sym)
        info = tk.info or {}
    except Exception as exc:
        if _is_ticker_not_found_exception(exc):
            return None
        raise CoverageDataSourceError(
            "yfinance",
            f"{type(exc).__name__} while looking up {sym}",
            status_code=_extract_http_status(exc),
        ) from exc

    fi = getattr(tk, "fast_info", None)
    quote_type = str(info.get("quoteType") or "").strip().upper()
    if quote_type == "NONE":
        return None

    exchange = str(
        info.get("exchange")
        or _safe_fast_info_get(fi, "exchange")
        or ""
    ).strip().upper()
    name = str(
        info.get("longName")
        or info.get("shortName")
        or info.get("displayName")
        or ""
    ).strip()
    price = (
        info.get("regularMarketPrice")
        or info.get("currentPrice")
        or info.get("previousClose")
        or _safe_fast_info_get(fi, "lastPrice")
        or _safe_fast_info_get(fi, "regularMarketPreviousClose")
        or _safe_fast_info_get(fi, "previousClose")
    )
    market_cap = info.get("marketCap") or _safe_fast_info_get(fi, "marketCap")

    has_identity = bool(name) or (
        quote_type in ("EQUITY", "ETF", "INDEX", "MUTUALFUND")
        and (price is not None or market_cap is not None or bool(exchange))
    )
    if not has_identity:
        return None

    canon_sym = display_spelling(str(info.get("symbol") or sym)).strip().upper() or sym
    country = str(info.get("country") or "").strip()
    sector = str(info.get("sector") or "").strip()
    industry = str(info.get("industry") or "").strip()
    market = _map_exchange_or_country_to_market(
        canon_sym, exchange=exchange, country=country
    )
    return {
        "ticker": sym,
        "name": name or sym,
        "market": market,
        "sector": sector,
        "industry": industry,
        "exchange": exchange,
        "market_cap": market_cap,
        "source_provider": "yfinance",
    }


async def _lookup_via_yfinance(ticker: str) -> dict[str, Any] | None:
    """Look up `ticker` via `yfinance.Ticker.info` / `fast_info`."""
    return await asyncio.to_thread(_lookup_via_yfinance_sync, ticker)


async def _lookup_via_fmp_profile(ticker: str) -> dict[str, Any] | None:
    """Optional FMP profile supplement when `FMP_API_KEY` is configured. Never logs or returns the key."""
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        return None
    from src.data_client.fmp.fmp_client import FMPClient

    client = FMPClient(api_key=api_key)
    try:
        rows = await client.get_profile(ticker)
        if isinstance(rows, list) and rows:
            first = rows[0]
            if isinstance(first, Mapping) and (
                first.get("symbol") or first.get("companyName") or first.get("name")
            ):
                sym = str(first.get("symbol") or ticker).strip().upper()
                name = str(
                    first.get("companyName") or first.get("name") or sym
                ).strip()
                exchange = str(
                    first.get("exchangeShortName") or first.get("exchange") or ""
                ).strip()
                country = str(first.get("country") or "").strip()
                sector = str(first.get("sector") or "").strip()
                industry = str(first.get("industry") or "").strip()
                market = _map_exchange_or_country_to_market(
                    sym, exchange=exchange, country=country
                )
                return {
                    "ticker": sym,
                    "name": name,
                    "market": market,
                    "sector": sector,
                    "industry": industry,
                    "exchange": exchange,
                    "market_cap": first.get("mktCap") or first.get("marketCap"),
                    "source_provider": "fmp",
                }
    except Exception as exc:
        logger.debug(
            "[DALTON_COVERAGE] FMP profile lookup unavailable for %s (%s)",
            ticker,
            type(exc).__name__,
        )
    finally:
        try:
            await client.close()
        except Exception:
            pass
    return None


async def validate_and_enrich_ticker(ticker: str) -> dict[str, Any] | None:
    """Validate that `ticker` exists (yfinance primary, FMP optional supplement) and return enriched metadata.

    Raises `CoverageDataSourceError` if market data sources fail due to provider/network errors,
    rather than silently treating a broken data source as "ticker not found".
    """
    sym = str(ticker or "").strip().upper()
    if not sym:
        return None

    yf_error: CoverageDataSourceError | None = None
    hit: dict[str, Any] | None = None
    try:
        hit = await _lookup_via_yfinance(sym)
    except CoverageDataSourceError as exc:
        yf_error = exc
        logger.warning(
            "[DALTON_COVERAGE] yfinance lookup failed for %s (%s), checking FMP fallback",
            sym,
            exc.reason,
        )

    if hit is not None:
        if (not hit.get("name") or hit.get("name") == sym or not hit.get("sector")) and (
            os.getenv("FMP_API_KEY") or ""
        ).strip():
            fmp_supp = await _lookup_via_fmp_profile(sym)
            if fmp_supp is not None:
                if not hit.get("name") or hit.get("name") == sym:
                    hit["name"] = fmp_supp.get("name") or hit["name"]
                if not hit.get("sector") and fmp_supp.get("sector"):
                    hit["sector"] = fmp_supp["sector"]
                if not hit.get("industry") and fmp_supp.get("industry"):
                    hit["industry"] = fmp_supp["industry"]
        return hit

    if (os.getenv("FMP_API_KEY") or "").strip():
        fmp_hit = await _lookup_via_fmp_profile(sym)
        if fmp_hit is not None:
            return fmp_hit

    if yf_error is not None:
        raise yf_error
    return None


async def validate_and_enrich_tickers(
    tickers: Sequence[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate a sequence of tickers, returning `(valid_enriched_list, not_found_tickers)`."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in tickers:
        sym = str(raw or "").strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            cleaned.append(sym)

    valid: list[dict[str, Any]] = []
    not_found: list[str] = []
    for sym in cleaned:
        info = await validate_and_enrich_ticker(sym)
        if info is not None:
            valid.append(info)
        else:
            not_found.append(sym)
    return valid, not_found


async def _user_workspace_tickers(user_id: str) -> dict[str, str]:
    """Return `{TICKER_UPPER: workspace_id}` for all non-deleted workspaces owned by `user_id`."""
    uid = str(user_id or "").strip()
    if not uid:
        return {}
    try:
        from src.server.database import workspace as ws_db

        rows, _ = await ws_db.list_workspaces(user_id=uid, limit=500)
        out: dict[str, str] = {}
        for r in rows or []:
            if not isinstance(r, Mapping):
                continue
            if str(r.get("status") or "").lower() == "deleted":
                continue
            name = str(r.get("name") or "").strip().upper()
            ws_id = str(r.get("workspace_id") or "").strip()
            if name and name != "HOME" and ws_id:
                out[name] = ws_id
        return out
    except Exception:
        return {}


async def list_user_coverage_summary(user_id: str) -> dict[str, Any]:
    """List the user's coverage (grouped by tier), rule groups, and nearest upcoming earnings dates."""
    uid = str(user_id or "").strip()
    rows = await coverage_db.list_coverage_rows(uid)
    groups = await coverage_db.list_coverage_groups(uid)
    mirror_row = await coverage_db.get_earnings_calendar_mirror(uid)

    companies: list[dict[str, Any]] = []
    core_list: list[dict[str, Any]] = []
    watch_list: list[dict[str, Any]] = []
    stale_list: list[dict[str, Any]] = []

    for r in rows:
        comp = row_to_company(r)
        gids = extract_row_group_ids(r)
        comp_with_meta = {
            **comp,
            "source": r.get("source"),
            "group_id": r.get("group_id") or (gids[0] if gids else None),
            "group_ids": gids,
            "group_stale": bool((r.get("extra") or {}).get("group_stale")),
        }
        companies.append(comp_with_meta)
        if comp_with_meta["tier"] == "core":
            core_list.append(comp_with_meta)
        else:
            watch_list.append(comp_with_meta)
        if comp_with_meta["group_stale"]:
            stale_list.append(comp_with_meta)

    upcoming_earnings: list[dict[str, Any]] = []
    today_iso = date.today().isoformat()
    if mirror_row and isinstance(mirror_row.get("ledger"), Mapping):
        entries = mirror_row["ledger"].get("entries")
        if isinstance(entries, Mapping):
            for sym, ent in entries.items():
                if not isinstance(ent, Mapping):
                    continue
                eff_date = str(ent.get("date") or ent.get("tentative_date") or "").strip()
                if eff_date and eff_date >= today_iso:
                    upcoming_earnings.append(
                        {
                            "ticker": str(sym).upper(),
                            "name": ent.get("name") or str(sym).upper(),
                            "date": eff_date,
                            "status": ent.get("status") or "pending",
                            "date_status": ent.get("date_status")
                            or ("confirmed" if ent.get("date") else "estimated"),
                            "timing": ent.get("timing") or "unknown",
                            "tier": str(ent.get("tier") or "watch").lower(),
                            "quarter": ent.get("quarter"),
                        }
                    )
            upcoming_earnings.sort(key=lambda x: (str(x.get("date") or ""), str(x.get("ticker") or "")))

    return {
        "ok": True,
        "user_id": uid,
        "counts": {
            "core": len(core_list),
            "watch": len(watch_list),
            "total": len(companies),
            "stale": len(stale_list),
            "groups": len(groups),
        },
        "core": core_list,
        "watch": watch_list,
        "stale": stale_list,
        "stale_tickers": [c["ticker"] for c in stale_list],
        "companies": companies,
        "groups": groups,
        "upcoming_earnings": upcoming_earnings[:20],
        "mirror_published_at": (
            mirror_row["published_at"].isoformat()
            if mirror_row and isinstance(mirror_row.get("published_at"), datetime)
            else (mirror_row or {}).get("published_at")
        ),
    }


def _build_upsert_rows_from_existing(
    *,
    uid: str,
    valid_items: Sequence[Mapping[str, Any]],
    existing_rows: Mapping[str, Mapping[str, Any]],
    ws_by_ticker: Mapping[str, str],
    norm_tier: str,
    norm_source: str,
    group_id: str | None,
) -> list[dict[str, Any]]:
    rows_to_upsert: list[dict[str, Any]] = []
    gid_clean = str(group_id).strip() if group_id else None
    for item in valid_items:
        sym = str(item["ticker"]).strip().upper()
        existing = existing_rows.get(sym)
        if existing is not None:
            comp = row_to_company(existing)
            prev_gids = extract_row_group_ids(existing)
            merged_gids = list(prev_gids)
            if gid_clean and gid_clean not in merged_gids:
                merged_gids.append(gid_clean)
            if norm_source == "group":
                eff_source = str(existing.get("source") or "chat")
                eff_group_id = existing.get("group_id") or (merged_gids[0] if merged_gids else gid_clean)
            else:
                comp["tier"] = norm_tier
                eff_source = norm_source
                eff_group_id = gid_clean or existing.get("group_id") or (merged_gids[0] if merged_gids else None)
            if item.get("name") and (not comp.get("name") or comp.get("name") == sym):
                comp["name"] = str(item["name"])
            if item.get("market") and not comp.get("market"):
                comp["market"] = str(item["market"])
            comp.pop("group_stale", None)
        else:
            peer_grp = str(item.get("industry") or item.get("sector") or "").strip()
            comp = make_new_company_dict(
                sym,
                name=str(item.get("name") or sym),
                tier=norm_tier,
                market=str(item.get("market") or infer_market(sym)),
                peer_group=peer_grp,
                has_active_workspace=sym in ws_by_ticker,
            )
            eff_source = norm_source
            eff_group_id = gid_clean
            merged_gids = [gid_clean] if gid_clean else []
        rows_to_upsert.append(
            company_to_row(
                comp,
                user_id=uid,
                source=eff_source,
                group_id=eff_group_id,
                group_ids=merged_gids,
            )
        )
    return rows_to_upsert


async def add_user_coverage_tickers(
    user_id: str,
    tickers: Sequence[str],
    *,
    tier: str = "watch",
    source: str = "chat",
    group_id: str | None = None,
    prevalidated: Mapping[str, Mapping[str, Any]] | None = None,
    conn: Any = None,
    ensure_automation: bool = True,
    sync_home: bool = True,
) -> dict[str, Any]:
    """Validate and add tickers to `user_id`'s coverage roster.

    Unresolvable tickers are returned in `not_found` and never written.
    Reading existing rows and upserting happen inside the same transaction and
    per-user advisory lock so manual tier/source cannot be overwritten by a group.
    Whenever the user's coverage is mutated and non-empty, idempotently ensures
    their `dalton-earnings-calendar` Home automation exists.
    """
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    norm_tier = normalize_tier(tier, default="watch")
    norm_source = normalize_source(source, default="chat")

    if prevalidated is not None:
        valid_items: list[dict[str, Any]] = []
        not_found: list[str] = []
        seen: set[str] = set()
        for raw in tickers:
            sym = str(raw or "").strip().upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            if sym in prevalidated:
                info = dict(prevalidated[sym])
                info["ticker"] = sym
                valid_items.append(info)
            else:
                not_found.append(sym)
    else:
        valid_items, not_found = await validate_and_enrich_tickers(tickers)

    if not valid_items:
        return {
            "ok": len(not_found) == 0,
            "added": [],
            "not_found": not_found,
            "tier": norm_tier,
            "automation_created": False,
            "home_synced": False,
        }

    ws_by_ticker = await _user_workspace_tickers(uid)

    async def _locked_read_and_upsert(c: Any) -> tuple[int, list[dict[str, Any]]]:
        await coverage_db.acquire_coverage_advisory_lock(c, uid)
        existing_rows = {
            str(r["ticker"]).upper(): r
            for r in await coverage_db.list_coverage_rows(uid, conn=c)
        }
        rows_to_upsert = _build_upsert_rows_from_existing(
            uid=uid,
            valid_items=valid_items,
            existing_rows=existing_rows,
            ws_by_ticker=ws_by_ticker,
            norm_tier=norm_tier,
            norm_source=norm_source,
            group_id=group_id,
        )
        return await coverage_db.upsert_coverage_rows(uid, rows_to_upsert, conn=c)

    if conn is not None:
        _prev_count, upserted = await _locked_read_and_upsert(conn)
    else:
        async with coverage_db.get_db_connection() as db_conn:
            async with db_conn.transaction():
                _prev_count, upserted = await _locked_read_and_upsert(db_conn)

    automation_res: dict[str, Any] = {}
    if ensure_automation and len(upserted) > 0:
        automation_res = await ensure_user_earnings_calendar_automation(uid)

    home_synced = await sync_user_coverage_to_home(uid) if sync_home else False
    added_companies = [row_to_company(r) for r in upserted]

    return {
        "ok": True,
        "added": added_companies,
        "added_tickers": [c["ticker"] for c in added_companies],
        "not_found": not_found,
        "tier": norm_tier,
        "automation": automation_res or None,
        "automation_created": automation_res.get("action") == "created",
        "home_synced": home_synced,
    }


async def remove_user_coverage_tickers(
    user_id: str,
    tickers: Sequence[str],
) -> dict[str, Any]:
    """Remove tickers from `user_id`'s coverage roster.

    If a removed ticker has an existing company workspace under `user_id`,
    notes that the workspace and its automations are not deleted.
    """
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    cleaned = [str(t or "").strip().upper() for t in tickers if str(t or "").strip()]
    if not cleaned:
        return {
            "ok": True,
            "removed": [],
            "removed_tickers": [],
            "workspaces_preserved": [],
            "notice": None,
            "workspace_notice": None,
            "home_synced": False,
        }

    ws_by_ticker = await _user_workspace_tickers(uid)
    preserved_workspaces = [t for t in cleaned if t in ws_by_ticker]

    removed = await coverage_db.delete_coverage_tickers(uid, cleaned)
    if removed and await coverage_db.count_coverage_rows(uid) > 0:
        await ensure_user_earnings_calendar_automation(uid)
    home_synced = await sync_user_coverage_to_home(uid)

    notice: str | None = None
    if preserved_workspaces:
        notice = (
            f"{', '.join(preserved_workspaces)} 已有公司 workspace，workspace 和自动任务不会被删除。"
        )

    return {
        "ok": True,
        "removed": removed,
        "removed_tickers": removed,
        "workspaces_preserved": preserved_workspaces,
        "notice": notice,
        "workspace_notice": notice,
        "home_synced": home_synced,
    }


async def set_user_coverage_tier(
    user_id: str,
    tickers: Sequence[str],
    tier: str,
) -> dict[str, Any]:
    """Update `tier` (`'core'` or `'watch'`) for existing tickers in `user_id`'s coverage."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    norm_tier = normalize_tier(tier, default="watch")
    cleaned = [str(t or "").strip().upper() for t in tickers if str(t or "").strip()]
    if not cleaned:
        return {"ok": True, "updated": [], "updated_tickers": [], "tier": norm_tier, "home_synced": False}

    updated_rows = await coverage_db.update_coverage_tier(uid, cleaned, norm_tier)
    updated_tickers = [str(r["ticker"]) for r in updated_rows]
    missing_tickers = [t for t in cleaned if t not in set(updated_tickers)]
    if updated_rows:
        await ensure_user_earnings_calendar_automation(uid)
    home_synced = await sync_user_coverage_to_home(uid)

    return {
        "ok": True,
        "updated": updated_tickers,
        "updated_tickers": updated_tickers,
        "missing_tickers": missing_tickers,
        "tier": norm_tier,
        "home_synced": home_synced,
    }


# ---------------------------------------------------------------------------
# Item 4 & 5: Screener Rule Groups (preview, add, remove, quarterly resolve)
# ---------------------------------------------------------------------------


_EXCHANGE_TO_YF_CODES: dict[str, tuple[str, ...]] = {
    "NASDAQ": ("NMS", "NGM", "NCM"),
    "NYSE": ("NYQ",),
    "AMEX": ("ASE",),
    "US": US_MAINBOARD_EXCHANGES,
    "HKEX": ("HKG",),
    "HKSE": ("HKG",),
    "HK": ("HKG",),
    "SSE": ("SHH",),
    "SZSE": ("SHZ",),
}

_COUNTRY_TO_YF_REGION: dict[str, str] = {
    "US": "us",
    "USA": "us",
    "UNITED STATES": "us",
    "HK": "hk",
    "HKG": "hk",
    "HONG KONG": "hk",
    "CN": "cn",
    "CHN": "cn",
    "CHINA": "cn",
}


def _normalize_company_stem(name: str) -> str:
    import re

    lowered = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", str(name or "").lower()).strip()
    if not lowered:
        return ""
    stop = {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "ltd",
        "limited",
        "plc",
        "co",
        "company",
        "holdings",
        "holding",
        "group",
        "adr",
        "ads",
        "ag",
        "nv",
        "sa",
        "se",
        "the",
    }
    tokens = [t for t in lowered.split() if t not in stop]
    return " ".join(tokens)


def _is_otc_or_non_mainboard_row(row: Mapping[str, Any]) -> bool:
    ex = str(row.get("exchange") or row.get("exchangeShortName") or "").strip().upper()
    if ex in OTC_EXCHANGES:
        return True
    if ex and ex not in MAINBOARD_EXCHANGES:
        return True
    qtype = str(row.get("quoteType") or "").strip().upper()
    if qtype and qtype != "EQUITY":
        return True
    return False


def _filter_mainboard_screener_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep only mainboard equity listings and remove OTC / duplicate secondary tickers."""
    from market_protocol import display_spelling

    filtered: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    seen_stems: set[str] = set()

    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        if _is_otc_or_non_mainboard_row(raw):
            continue
        raw_sym = str(raw.get("symbol") or raw.get("ticker") or "").strip()
        if not raw_sym:
            continue
        sym = display_spelling(raw_sym).strip().upper()
        if not sym or sym in seen_symbols:
            continue
        ex = str(raw.get("exchange") or raw.get("exchangeShortName") or "").strip().upper()
        # Reject unlabelled 5-letter F/Y OTC tickers on US venues
        if "." not in sym and len(sym) == 5 and sym.endswith(("F", "Y")) and not ex:
            continue

        name = str(
            raw.get("companyName")
            or raw.get("longName")
            or raw.get("shortName")
            or raw.get("name")
            or sym
        ).strip()
        stem = _normalize_company_stem(name)
        if stem and len(stem) >= 3 and stem in seen_stems:
            continue

        seen_symbols.add(sym)
        if stem and len(stem) >= 3:
            seen_stems.add(stem)

        item = dict(raw)
        item["symbol"] = sym
        item["companyName"] = name
        if ex:
            item["exchange"] = ex
        filtered.append(item)

    return filtered


def _build_yf_equity_query(criteria: Mapping[str, Any]) -> tuple[Any, int]:
    """Build `(EquityQuery, limit)` from `criteria`, restricting to mainboard exchanges."""
    from yfinance.screener.query import EquityQuery

    nodes: list[Any] = []
    country_raw = str(criteria.get("country") or criteria.get("market") or "US").strip().upper()
    region = _COUNTRY_TO_YF_REGION.get(country_raw, country_raw.lower() or "us")
    nodes.append(EquityQuery("EQ", ["region", region]))

    exchange_raw = str(criteria.get("exchange") or "").strip().upper()
    if exchange_raw:
        mapped_ex = list(_EXCHANGE_TO_YF_CODES.get(exchange_raw, (exchange_raw,)))
        ex_codes = [c for c in mapped_ex if c and c not in OTC_EXCHANGES]
    elif region == "us":
        ex_codes = list(US_MAINBOARD_EXCHANGES)
    elif region == "hk":
        ex_codes = ["HKG"]
    elif region == "cn":
        ex_codes = ["SHH", "SHZ"]
    else:
        ex_codes = []

    if len(ex_codes) == 1:
        nodes.append(EquityQuery("EQ", ["exchange", ex_codes[0]]))
    elif len(ex_codes) > 1:
        nodes.append(EquityQuery("IS-IN", ["exchange", *ex_codes]))

    sector = str(criteria.get("sector") or "").strip()
    if sector:
        nodes.append(EquityQuery("EQ", ["sector", sector]))

    industry = str(criteria.get("industry") or "").strip()
    if industry:
        nodes.append(EquityQuery("EQ", ["industry", industry]))

    mc_min = (
        criteria.get("market_cap_min")
        if criteria.get("market_cap_min") is not None
        else criteria.get("market_cap_more_than", criteria.get("marketCapMoreThan"))
    )
    if mc_min is not None:
        nodes.append(EquityQuery("GT", ["intradaymarketcap", float(mc_min)]))

    mc_max = (
        criteria.get("market_cap_max")
        if criteria.get("market_cap_max") is not None
        else criteria.get("market_cap_lower_than", criteria.get("marketCapLowerThan"))
    )
    if mc_max is not None:
        nodes.append(EquityQuery("LT", ["intradaymarketcap", float(mc_max)]))

    limit_val = criteria.get("limit")
    limit = min(max(int(limit_val) if limit_val is not None else 50, 1), 250)

    query = nodes[0] if len(nodes) == 1 else EquityQuery("AND", nodes)
    return query, limit


def _fetch_yf_screener_rows_sync(criteria: Mapping[str, Any]) -> list[dict[str, Any]]:
    from market_protocol import display_spelling
    from yfinance.screener.screener import screen as yf_screen

    try:
        query, limit = _build_yf_equity_query(criteria)
        result = yf_screen(
            query,
            size=limit,
            sortField="intradaymarketcap",
            sortAsc=False,
        )
    except Exception as exc:
        raise CoverageDataSourceError(
            "yfinance",
            f"{type(exc).__name__} in yf.screen",
            status_code=_extract_http_status(exc),
        ) from exc

    quotes = result.get("quotes", []) if isinstance(result, Mapping) else []
    raw_rows: list[dict[str, Any]] = []
    for q in quotes:
        if not isinstance(q, Mapping):
            continue
        sym = display_spelling(str(q.get("symbol") or "")).strip().upper()
        if not sym:
            continue
        raw_rows.append(
            {
                "symbol": sym,
                "companyName": str(
                    q.get("longName") or q.get("shortName") or q.get("displayName") or sym
                ).strip(),
                "exchange": str(q.get("exchange") or "").strip().upper(),
                "exchangeShortName": str(q.get("exchange") or "").strip().upper(),
                "quoteType": str(q.get("quoteType") or "EQUITY").strip().upper(),
                "country": str(q.get("region") or criteria.get("country") or "").strip().upper(),
                "sector": str(q.get("sector") or q.get("sectorDisp") or criteria.get("sector") or "").strip(),
                "industry": str(q.get("industry") or q.get("industryDisp") or criteria.get("industry") or "").strip(),
                "marketCap": q.get("marketCap") or q.get("intradaymarketcap"),
            }
        )
    return _filter_mainboard_screener_rows(raw_rows)


async def _fetch_yf_screener_rows(criteria: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Resolve `criteria` via `yf.screen(EquityQuery(...))` keeping only mainboard listings."""
    return await asyncio.to_thread(_fetch_yf_screener_rows_sync, criteria)


def _build_fmp_screener_filters(criteria: Mapping[str, Any]) -> dict[str, Any]:
    """Translate group `criteria` into `FMPClient.get_company_screener` filter kwargs."""
    filters: dict[str, Any] = {}
    if criteria.get("country"):
        filters["country"] = str(criteria["country"]).strip()
    if criteria.get("exchange"):
        filters["exchange"] = str(criteria["exchange"]).strip()
    if criteria.get("sector"):
        filters["sector"] = str(criteria["sector"]).strip()
    if criteria.get("industry"):
        filters["industry"] = str(criteria["industry"]).strip()

    mc_min = (
        criteria.get("market_cap_min")
        if criteria.get("market_cap_min") is not None
        else criteria.get("market_cap_more_than", criteria.get("marketCapMoreThan"))
    )
    if mc_min is not None:
        filters["marketCapMoreThan"] = float(mc_min)

    mc_max = (
        criteria.get("market_cap_max")
        if criteria.get("market_cap_max") is not None
        else criteria.get("market_cap_lower_than", criteria.get("marketCapLowerThan"))
    )
    if mc_max is not None:
        filters["marketCapLowerThan"] = float(mc_max)

    limit_val = criteria.get("limit")
    filters["limit"] = int(limit_val) if limit_val is not None else 50
    filters["isEtf"] = bool(criteria.get("is_etf", criteria.get("isEtf", False)))
    filters["isFund"] = bool(criteria.get("is_fund", criteria.get("isFund", False)))
    filters["isActivelyTrading"] = bool(
        criteria.get("is_actively_trading", criteria.get("isActivelyTrading", True))
    )
    return filters


async def _fetch_fmp_screener_rows(filters: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Invoke `FMPClient.get_company_screener` using `FMP_API_KEY` from environment."""
    api_key = (os.getenv("FMP_API_KEY") or "").strip()
    if not api_key:
        raise CoverageDataSourceError("fmp", "FMP_API_KEY is not configured")
    from src.data_client.fmp.fmp_client import FMPClient

    client = FMPClient(api_key=api_key)
    try:
        raw_res = await client.get_company_screener(**dict(filters))
        if isinstance(raw_res, list):
            return _filter_mainboard_screener_rows(
                [dict(r) for r in raw_res if isinstance(r, Mapping)]
            )
        return []
    except Exception as exc:
        raise CoverageDataSourceError(
            "fmp",
            f"{type(exc).__name__} in company-screener",
            status_code=_extract_http_status(exc),
        ) from exc
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def _fetch_screener_rows(criteria: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Resolve screener rows with `yfinance` (`yf.screen(EquityQuery)`) as primary and FMP as supplement."""
    try:
        rows = await _fetch_yf_screener_rows(criteria)
        return _filter_mainboard_screener_rows(rows)
    except CoverageDataSourceError as yf_exc:
        if (os.getenv("FMP_API_KEY") or "").strip():
            try:
                filters = _build_fmp_screener_filters(criteria)
                return await _fetch_fmp_screener_rows(filters)
            except Exception as fmp_exc:
                logger.warning(
                    "[DALTON_COVERAGE] FMP screener fallback also failed (%s)",
                    type(fmp_exc).__name__,
                )
        raise yf_exc


async def preview_coverage_group(
    label: str,
    criteria: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve `criteria` via `yf.screen(EquityQuery(...))` without writing to DB."""
    if not isinstance(criteria, Mapping):
        raise ValueError("criteria must be a JSON object")

    filters = _build_fmp_screener_filters(criteria)
    include_raw = criteria.get("include") or []
    exclude_raw = criteria.get("exclude") or []
    include_tickers = [
        str(t).strip().upper()
        for t in (include_raw if isinstance(include_raw, (list, tuple)) else [include_raw])
        if str(t).strip()
    ]
    exclude_set = {
        str(t).strip().upper()
        for t in (exclude_raw if isinstance(exclude_raw, (list, tuple)) else [exclude_raw])
        if str(t).strip()
    }

    screener_rows: list[dict[str, Any]] = []
    has_screener_filters = any(
        k in filters
        for k in (
            "country",
            "exchange",
            "sector",
            "industry",
            "marketCapMoreThan",
            "marketCapLowerThan",
        )
    )
    if has_screener_filters or not include_tickers:
        screener_rows = await _fetch_screener_rows(criteria)

    by_symbol: dict[str, dict[str, Any]] = {}
    for row in _filter_mainboard_screener_rows(screener_rows):
        sym = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        if not sym or sym in exclude_set or sym in by_symbol:
            continue
        name = str(row.get("companyName") or row.get("name") or sym).strip()
        exchange = str(row.get("exchangeShortName") or row.get("exchange") or "").strip()
        country = str(row.get("country") or "").strip()
        market = _map_exchange_or_country_to_market(sym, exchange=exchange, country=country)
        by_symbol[sym] = {
            "ticker": sym,
            "name": name,
            "market": market,
            "sector": str(row.get("sector") or "").strip(),
            "industry": str(row.get("industry") or "").strip(),
            "market_cap": row.get("marketCap"),
        }

    rejected_tickers: list[str] = []
    for inc_sym in include_tickers:
        if inc_sym in exclude_set or inc_sym in by_symbol:
            continue
        enriched = await validate_and_enrich_ticker(inc_sym)
        if enriched is not None:
            by_symbol[inc_sym] = enriched
        elif inc_sym not in rejected_tickers:
            rejected_tickers.append(inc_sym)

    companies = list(by_symbol.values())
    return {
        "ok": True,
        "preview_only": True,
        "label": str(label or "").strip(),
        "criteria": dict(criteria),
        "count": len(companies),
        "tickers": [c["ticker"] for c in companies],
        "companies": companies,
        "not_found": rejected_tickers,
        "rejected": rejected_tickers,
    }


async def add_coverage_group(
    user_id: str,
    *,
    label: str,
    criteria: Mapping[str, Any],
    default_tier: str = "watch",
    auto_refresh: bool = True,
) -> dict[str, Any]:
    """Create a coverage rule group and upsert its resolved companies with `source='group'`."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    clean_label = str(label or "").strip()
    if not clean_label:
        raise ValueError("label is required")
    norm_tier = normalize_tier(default_tier, default="watch")

    preview = await preview_coverage_group(clean_label, criteria)
    resolved_companies = preview.get("companies") or []
    rejected_tickers = list(preview.get("not_found") or [])
    prevalidated = {str(c["ticker"]).upper(): c for c in resolved_companies if c.get("ticker")}

    now_utc = datetime.now(timezone.utc)
    async with coverage_db.get_db_connection() as conn:
        async with conn.transaction():
            await coverage_db.acquire_coverage_advisory_lock(conn, uid)
            group_row = await coverage_db.insert_coverage_group(
                uid,
                label=clean_label,
                criteria=dict(criteria),
                default_tier=norm_tier,
                auto_refresh=bool(auto_refresh),
                last_resolved_at=now_utc,
                conn=conn,
            )
            group_id = str(group_row["group_id"])
            add_res = await add_user_coverage_tickers(
                uid,
                list(prevalidated.keys()),
                tier=norm_tier,
                source="group",
                group_id=group_id,
                prevalidated=prevalidated,
                conn=conn,
                ensure_automation=False,
                sync_home=False,
            )

    automation_res: dict[str, Any] = {}
    if prevalidated:
        automation_res = await ensure_user_earnings_calendar_automation(uid)
    home_synced = await sync_user_coverage_to_home(uid)

    return {
        "ok": True,
        "group": group_row,
        "resolved_count": len(prevalidated),
        "added_tickers": add_res.get("added_tickers", []),
        "not_found": rejected_tickers,
        "rejected": rejected_tickers,
        "automation": automation_res or None,
        "automation_created": automation_res.get("action") == "created",
        "home_synced": home_synced,
    }


async def remove_coverage_group(
    user_id: str,
    group_id: str,
) -> dict[str, Any]:
    """Delete a coverage group and remove coverage entries added exclusively by that group."""
    uid = str(user_id or "").strip()
    gid = str(group_id or "").strip()
    if not uid or not gid:
        raise ValueError("user_id and group_id are required")

    res = await coverage_db.delete_coverage_group_and_exclusive_members(uid, gid)
    if await coverage_db.count_coverage_rows(uid) > 0:
        await ensure_user_earnings_calendar_automation(uid)
    home_synced = await sync_user_coverage_to_home(uid)
    return {
        "ok": True,
        **res,
        "home_synced": home_synced,
    }


async def resolve_coverage_group(
    user_id: str,
    group_id: str,
    *,
    now: datetime | None = None,
    sync_home: bool = True,
) -> dict[str, Any]:
    """Re-resolve an existing coverage group for `user_id` under a single advisory lock.

    - Newly matching tickers not yet in the user's coverage are added with `default_tier` and `source='group'`.
    - Existing tickers that also match this group have `group_id` added to their `extra.group_ids` without overwriting manual tier/source.
    - Tickers in this group that no longer match are NOT deleted; instead `extra.group_stale = true` is set.
    - Tickers in this group that match again have `extra.group_stale` cleared.
    """
    uid = str(user_id or "").strip()
    gid = str(group_id or "").strip()
    if not uid or not gid:
        raise ValueError("user_id and group_id are required")

    group_row = await coverage_db.get_coverage_group(uid, gid)
    if group_row is None:
        raise ValueError(f"Coverage group {gid!r} not found")

    label = str(group_row.get("label") or "")
    criteria = group_row.get("criteria") or {}
    default_tier = normalize_tier(group_row.get("default_tier"), default="watch")

    preview = await preview_coverage_group(label, criteria)
    resolved_companies = preview.get("companies") or []
    rejected_tickers = list(preview.get("not_found") or [])
    matched_by_ticker = {
        str(c["ticker"]).upper(): c for c in resolved_companies if c.get("ticker")
    }
    matched_set = set(matched_by_ticker.keys())
    resolved_ts = now or datetime.now(timezone.utc)

    async with coverage_db.get_db_connection() as conn:
        async with conn.transaction():
            await coverage_db.acquire_coverage_advisory_lock(conn, uid)
            existing_rows = await coverage_db.list_coverage_rows(uid, conn=conn)
            existing_by_ticker = {str(r["ticker"]).upper(): r for r in existing_rows}

            new_tickers = [t for t in matched_by_ticker if t not in existing_by_ticker]
            tickers_needing_membership = [
                t
                for t in matched_by_ticker
                if t not in existing_by_ticker
                or gid not in extract_row_group_ids(existing_by_ticker[t])
            ]
            stale_candidates = [
                str(r["ticker"]).upper()
                for r in existing_rows
                if gid in extract_row_group_ids(r)
                and str(r["ticker"]).upper() not in matched_set
            ]
            unstale_candidates = [
                str(r["ticker"]).upper()
                for r in existing_rows
                if gid in extract_row_group_ids(r)
                and str(r["ticker"]).upper() in matched_set
                and bool((r.get("extra") or {}).get("group_stale"))
            ]

            if tickers_needing_membership:
                await add_user_coverage_tickers(
                    uid,
                    tickers_needing_membership,
                    tier=default_tier,
                    source="group",
                    group_id=gid,
                    prevalidated={t: matched_by_ticker[t] for t in tickers_needing_membership},
                    conn=conn,
                    ensure_automation=False,
                    sync_home=False,
                )

            marked_stale: list[str] = []
            if stale_candidates:
                marked_stale = await coverage_db.mark_coverage_rows_group_stale(
                    uid, stale_candidates, stale=True, conn=conn
                )

            if unstale_candidates:
                await coverage_db.mark_coverage_rows_group_stale(
                    uid, unstale_candidates, stale=False, conn=conn
                )

            updated_group = await coverage_db.update_coverage_group_resolved(
                uid, gid, last_resolved_at=resolved_ts, conn=conn
            )

    if matched_by_ticker or existing_rows:
        await ensure_user_earnings_calendar_automation(uid)
    home_synced = await sync_user_coverage_to_home(uid) if sync_home else False

    return {
        "ok": True,
        "group": updated_group or group_row,
        "matched_tickers": list(matched_by_ticker.keys()),
        "added_tickers": new_tickers,
        "stale_tickers": marked_stale,
        "not_found": rejected_tickers,
        "rejected": rejected_tickers,
        "home_synced": home_synced,
    }


def _is_group_due_for_refresh(
    group_row: Mapping[str, Any],
    *,
    now: datetime,
    threshold_days: int = GROUP_REFRESH_INTERVAL_DAYS,
) -> bool:
    if not group_row.get("auto_refresh", True):
        return False
    last_raw = group_row.get("last_resolved_at")
    if last_raw is None:
        return True
    if isinstance(last_raw, datetime):
        last_dt = last_raw if last_raw.tzinfo is not None else last_raw.replace(tzinfo=timezone.utc)
    elif isinstance(last_raw, str) and last_raw.strip():
        try:
            parsed = datetime.fromisoformat(last_raw.strip().replace("Z", "+00:00"))
            last_dt = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return True
    else:
        return True
    return (now - last_dt) > timedelta(days=threshold_days)


async def refresh_due_coverage_groups_for_preflight(
    user_id: str,
    *,
    now: datetime | None = None,
    sync_home: bool = False,
) -> dict[str, Any]:
    """Zero-token quarterly refresh of `auto_refresh=True` coverage groups (>85 days since `last_resolved_at`)."""
    uid = str(user_id or "").strip()
    if not uid:
        return {
            "refreshed_groups": [],
            "added_tickers": [],
            "stale_tickers": [],
            "summary_lines": [],
            "summary_text": "",
        }

    now_utc = now or datetime.now(timezone.utc)
    try:
        groups = await coverage_db.list_coverage_groups(uid)
    except Exception as exc:
        if not _is_pool_not_open_error(exc):
            logger.warning("[DALTON_COVERAGE] failed listing groups in preflight for %s: %s", uid, exc)
        return {
            "refreshed_groups": [],
            "added_tickers": [],
            "stale_tickers": [],
            "summary_lines": [],
            "summary_text": "",
        }

    refreshed_groups: list[dict[str, Any]] = []
    all_added: list[str] = []
    all_stale: list[str] = []
    summary_lines: list[str] = []

    for grp in groups:
        if not _is_group_due_for_refresh(grp, now=now_utc):
            continue
        gid = str(grp["group_id"])
        label = str(grp.get("label") or gid)
        try:
            res = await resolve_coverage_group(uid, gid, now=now_utc, sync_home=sync_home)
            added = list(res.get("added_tickers") or [])
            stale = list(res.get("stale_tickers") or [])
            all_added.extend(added)
            all_stale.extend(stale)
            refreshed_groups.append(
                {
                    "group_id": gid,
                    "label": label,
                    "added_tickers": added,
                    "stale_tickers": stale,
                }
            )
            parts: list[str] = []
            if added:
                parts.append(f"新纳入 {', '.join(added)}")
            if stale:
                parts.append(f"不再符合条件（已标 group_stale，未自动删除）{', '.join(stale)}")
            if not parts:
                parts.append("成分无变动")
            summary_lines.append(f"规则组「{label}」季度重解析：{'；'.join(parts)}")
        except Exception as exc:
            logger.warning(
                "[DALTON_COVERAGE] quarterly group refresh failed for user=%s group=%s: %s",
                uid,
                gid,
                exc,
            )

    return {
        "refreshed_groups": refreshed_groups,
        "added_tickers": all_added,
        "stale_tickers": all_stale,
        "summary_lines": summary_lines,
        "summary_text": "\n".join(summary_lines),
    }


# ---------------------------------------------------------------------------
# Item 5, 6, 7: Earnings Calendar Mirror publish & date-range query
# ---------------------------------------------------------------------------


async def publish_user_earnings_calendar_mirror(
    user_id: str,
    ledger: Mapping[str, Any],
    *,
    raw_bytes_len: int | None = None,
) -> dict[str, Any]:
    """Validate and store `earnings-calendar.json` into `dalton_earnings_calendar_mirror`."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    validated = validate_earnings_calendar_ledger(ledger, raw_bytes_len=raw_bytes_len)
    row = await coverage_db.upsert_earnings_calendar_mirror(uid, validated)
    pub_at = row.get("published_at")
    return {
        "ok": True,
        "user_id": uid,
        "entries_count": len(validated.get("entries") or {}),
        "reported_count": len(validated.get("reported") or []),
        "published_at": pub_at.isoformat() if isinstance(pub_at, datetime) else pub_at,
    }


async def query_user_earnings_calendar(
    user_id: str,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """Return mirrored earnings calendar entries for `user_id` falling within `[from_date, to_date]`."""
    uid = str(user_id or "").strip()
    if not uid:
        return {"data": [], "count": 0, "published_at": None, "mirror_empty": True}

    start_iso = str(from_date or "").strip()[:10] or None
    end_iso = str(to_date or "").strip()[:10] or None
    if start_iso:
        date.fromisoformat(start_iso)
    if end_iso:
        date.fromisoformat(end_iso)

    mirror_row = await coverage_db.get_earnings_calendar_mirror(uid)
    if mirror_row is None or not isinstance(mirror_row.get("ledger"), Mapping):
        return {"data": [], "count": 0, "published_at": None, "mirror_empty": True}

    cov_rows = await coverage_db.list_coverage_rows(uid)
    cov_by_ticker = {str(r["ticker"]).upper(): r for r in cov_rows}

    ledger = mirror_row["ledger"]
    entries = ledger.get("entries") if isinstance(ledger.get("entries"), Mapping) else {}
    reported = ledger.get("reported") if isinstance(ledger.get("reported"), list) else []

    items: list[dict[str, Any]] = []
    seen_ticker_date: set[tuple[str, str]] = set()

    def _in_window(d_str: str) -> bool:
        if not d_str or len(d_str) < 10:
            return False
        d_prefix = d_str[:10]
        if start_iso and d_prefix < start_iso:
            return False
        if end_iso and d_prefix > end_iso:
            return False
        return True

    for raw_sym, ent in entries.items():
        if not isinstance(ent, Mapping):
            continue
        sym = str(raw_sym or "").strip().upper()
        eff_date = str(ent.get("date") or ent.get("tentative_date") or "").strip()[:10]
        if not _in_window(eff_date):
            continue
        status_raw = str(ent.get("status") or "pending").strip().lower()
        date_status_raw = str(
            ent.get("date_status")
            or ("confirmed" if ent.get("date") and status_raw == "confirmed" else "estimated")
        ).strip().lower()
        if status_raw == "reported":
            display_status = "reported"
        elif status_raw == "confirmed" and date_status_raw == "confirmed":
            display_status = "confirmed"
        else:
            display_status = "pending"

        cov_row = cov_by_ticker.get(sym) or {}
        tier = str(ent.get("tier") or cov_row.get("tier") or "watch").strip().lower()
        name = str(ent.get("name") or cov_row.get("name") or sym).strip()
        timing = str(ent.get("timing") or "unknown").strip().upper()
        if timing not in ("BMO", "AMC"):
            timing = "unknown"

        seen_ticker_date.add((sym, eff_date))
        items.append(
            {
                "symbol": sym,
                "ticker": sym,
                "name": name,
                "date": eff_date,
                "status": display_status,
                "date_status": date_status_raw,
                "tier": tier,
                "timing": timing,
                "quarter": ent.get("quarter"),
                "release_time_et": ent.get("release_time_et"),
                "call_time_et": ent.get("call_time_et"),
                "source_url": ent.get("source_url"),
                "source_type": ent.get("source_type"),
            }
        )

    for rep in reported:
        if not isinstance(rep, Mapping):
            continue
        sym = str(rep.get("ticker") or rep.get("symbol") or "").strip().upper()
        rep_date = str(rep.get("date") or "").strip()[:10]
        if not sym or not _in_window(rep_date) or (sym, rep_date) in seen_ticker_date:
            continue
        cov_row = cov_by_ticker.get(sym) or {}
        tier = str(rep.get("tier") or cov_row.get("tier") or "watch").strip().lower()
        name = str(rep.get("name") or cov_row.get("name") or sym).strip()
        timing = str(rep.get("timing") or "unknown").strip().upper()
        if timing not in ("BMO", "AMC"):
            timing = "unknown"
        seen_ticker_date.add((sym, rep_date))
        items.append(
            {
                "symbol": sym,
                "ticker": sym,
                "name": name,
                "date": rep_date,
                "status": "reported",
                "date_status": "confirmed",
                "tier": tier,
                "timing": timing,
                "quarter": rep.get("quarter"),
                "release_time_et": rep.get("release_time_et"),
                "call_time_et": rep.get("call_time_et"),
                "source_url": rep.get("source_url"),
                "source_type": rep.get("source_type"),
            }
        )

    items.sort(key=lambda x: (str(x["date"]), 0 if x["tier"] == "core" else 1, str(x["symbol"])))
    pub_at = mirror_row.get("published_at")
    return {
        "data": items,
        "count": len(items),
        "published_at": pub_at.isoformat() if isinstance(pub_at, datetime) else pub_at,
        "mirror_empty": False,
    }


# ---------------------------------------------------------------------------
# Item 1: Seed user coverage from coverage.yaml
# ---------------------------------------------------------------------------


async def seed_user_coverage_from_yaml(
    user_id: str,
    yaml_path: Path,
    *,
    apply: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    """Seed `dalton_coverage` for `user_id` from a `coverage.yaml` file.

    - When `apply=False` (default dry-run), only parses and validates rows and returns counts.
    - When `apply=True`, upserts all rows in a single transaction under per-user advisory lock
      and asserts that the written row count matches the YAML company count.
    """
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    if not yaml_path.is_file():
        raise FileNotFoundError(f"Coverage YAML file not found: {yaml_path}")

    doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    companies = doc.get("companies") or []
    if not isinstance(companies, list):
        raise ValueError(f"Invalid 'companies' list in {yaml_path}")

    rows: list[dict[str, Any]] = []
    seen_tickers: set[str] = set()
    for idx, comp in enumerate(companies):
        if not isinstance(comp, Mapping):
            raise ValueError(
                f"Invalid company entry at index {idx} in {yaml_path}: expected mapping"
            )
        raw_ticker = str(comp.get("ticker") or "").strip().upper()
        if not raw_ticker:
            raise ValueError(
                f"Invalid company entry at index {idx} in {yaml_path}: missing ticker"
            )
        if raw_ticker in seen_tickers:
            raise ValueError(
                f"Duplicate ticker {raw_ticker!r} at index {idx} in {yaml_path}"
            )
        seen_tickers.add(raw_ticker)
        rows.append(company_to_row(comp, user_id=uid, source="seed"))

    core_count = sum(1 for r in rows if r["tier"] == "core")
    watch_count = sum(1 for r in rows if r["tier"] == "watch")

    if not apply:
        return {
            "ok": True,
            "mode": "dry_run",
            "user_id": uid,
            "source_file": str(yaml_path),
            "planned_count": len(rows),
            "core_count": core_count,
            "watch_count": watch_count,
            "written_count": 0,
        }

    expected_count = len(companies)

    async def _seed_in_tx(tx_conn: Any) -> tuple[int, list[dict[str, Any]]]:
        p_cnt, ups = await coverage_db.upsert_coverage_rows(
            uid,
            rows,
            conn=tx_conn,
            expected_unique_count=expected_count,
        )
        if len(ups) != expected_count:
            raise AssertionError(
                f"Written row count ({len(ups)}) does not match YAML company count ({expected_count})"
            )
        return p_cnt, ups

    if conn is not None:
        async with conn.transaction():
            _prev_count, upserted = await _seed_in_tx(conn)
    else:
        async with coverage_db.get_db_connection() as db_conn:
            async with db_conn.transaction():
                _prev_count, upserted = await _seed_in_tx(db_conn)

    automation_res: dict[str, Any] = {}
    if len(upserted) > 0:
        automation_res = await ensure_user_earnings_calendar_automation(uid)

    home_synced = await sync_user_coverage_to_home(uid)
    return {
        "ok": True,
        "mode": "applied",
        "user_id": uid,
        "source_file": str(yaml_path),
        "planned_count": len(rows),
        "core_count": core_count,
        "watch_count": watch_count,
        "written_count": len(upserted),
        "automation": automation_res or None,
        "home_synced": home_synced,
    }
