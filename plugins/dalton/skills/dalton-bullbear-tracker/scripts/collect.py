#!/usr/bin/env python3
"""公司多空日跟踪的采集与提交。

通用逻辑来自同级技能 dalton-thesis-delta 的 collect.py
（仓库里是 plugins/dalton/skills，沙箱里是 .agents/skills，都按同级目录加载）。
本脚本不读不写旧 OpenClaw 根目录，也不调用 gateway、xreach 或子进程。

collect 只写本轮 packet 和一致预期快照。seen.json、last_run.json、updates.log
留到 commit。同一 run_id 已有完成的 packet 时直接读回，不覆盖、不重新取数。
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import math
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
SKILL_DIR = Path(__file__).resolve().parent.parent
GENERIC_COLLECT_PATH = SKILL_DIR.parent / "dalton-thesis-delta" / "scripts" / "collect.py"

# 直接调用采集函数时默认 MSFT，线上状态键和锁名由此推出。
TICKER = "MSFT"
EXCERPT_MAX = 6000
PARA_MAX = 1800
MAX_INTERNAL_PAGES = 5
MAX_AE_PAGES = 5
MAX_AE_REQUESTS_PER_QUERY = 40
MAX_READ_PAGES = 20
READ_CHARS = 30000
AE_LIMIT = 20
SEC_LIMIT = 3
SEC_LOOKBACK_DAYS = 2
AE_LOOKBACK_DAYS = 3
AE_MAX_BACKLOG_DAYS = 5
X_SEARCH_COUNT = 40
X_TIMELINE_COUNT = 20
FIRST_LOOKBACK_DAYS = 3
KEYWORD_ROUTES = ("tmtb", "sales-notes", "funda-ai")

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_RUN_STAMP_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})$")
_LOG_RUN_RE = re.compile(r"(?m)^.* \| (?P<run>[^|]+?) \| material=")
_TRUNCATION_TOKENS = ("列表有更多文档", "更多文档", "缩小筛选", "提高 limit")
_HEADER_RE = re.compile(r"(From|Subject|Date|AlphaEngine doc_id):")
_OY_RE = re.compile(r"0y\s*=\s*FY(\d{2,4})[^0-9]{0,80}?(\d{4})-(\d{2})\s*止")
_PY_RE = re.compile(r"\+1y\s*=\s*FY(\d{2,4})")
_QUOTA_MARKERS = ("额度", "E_DESKTOP", "E_UPSTREAM_API")
FUND_TERMS = re.compile(
    r"(?i)\b(capex|opex|eps|estimates?|consensus|street|guid(?:e|ance)|margins?|fcf|free cash flow|revenue|ebitda|"
    r"depreciation|d&a|multiple|p/?e|ev/|valuation|price target|upgrade|downgrade|buyback|dilution|arr|dau|mau|cpm|"
    r"impressions|backlog|rpo|roi|roic|unit economics|bps|y/y|yoy|q/q|qoq|10-q|8-k|run-?rate|whisper|bogey)\b"
    r"|\d+(?:\.\d+)?\s?%|\$\d"
)
_THRESHOLD_MAP = {
    "fy1_eps_vs_last_run_pct": "eps_fy1_vs_last_pct",
    "fy1_eps_vs_7d_pct": "eps_fy1_vs_7d_pct",
    "fy1_rev_vs_last_run_pct": "rev_fy1_or_q1_vs_last_pct",
    "pt_mean_vs_last_run_pct": "target_price_vs_last_pct",
    "fy1_down_revisions_7d": "down_revisions_7d_min",
}


class CollectError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _load_generic():
    if not GENERIC_COLLECT_PATH.is_file():
        raise CollectError(
            "generic_collect_missing",
            f"找不到同级通用采集模块 {GENERIC_COLLECT_PATH}",
        )
    name = "dalton_thesis_delta_collect_for_bullbear_tracker"
    spec = importlib.util.spec_from_file_location(name, GENERIC_COLLECT_PATH)
    if spec is None or spec.loader is None:
        raise CollectError("generic_collect_missing", f"无法加载 {GENERIC_COLLECT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_g = _load_generic()


def _load_tracker_config() -> Any:
    path = Path(__file__).resolve().parent / "tracker_config.py"
    name = "dalton_bullbear_tracker_config"
    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CollectError("tracker_config_missing", f"找不到共享配置模块 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LEGACY_VERBATIM_TICKERS = frozenset({"MSFT", "META", "AMZN", "GOOGL"})
_NEW_MARKET_SOURCE_KEYS: tuple[str, ...] = ("price", "consensus", "earnings_calendar")
_YFINANCE_NO_DATA_TYPES = frozenset({
    "YFTickerMissingError",
    "YFPricesMissingError",
    "YFTzMissingError",
    "YFEarningsDateMissing",
})
_YFINANCE_NO_DATA_MARKERS = (
    "no data found",
    "not found",
    "http error 404",
    "no fundamentals data",
    "possibly delisted",
    "symbol may be delisted",
    "no timezone found",
    "quote not found",
    "no earnings dates",
)


def supported_tickers() -> tuple[str, ...]:
    return _load_tracker_config().supported_tickers(skill_dir=SKILL_DIR)


def resolve_rules_path(
    ticker: str | None = None,
    workspace: Path | str | None = None,
    *,
    strict: bool = False,
) -> tuple[Path | None, str | None]:
    cfg_mod = _load_tracker_config()
    try:
        return cfg_mod.resolve_rules_path(
            ticker or TICKER,
            workspace=workspace,
            skill_dir=SKILL_DIR,
            strict=strict,
        )
    except cfg_mod.RulesPathError as exc:
        raise CollectError(exc.status, exc.message) from exc


def call_bridge_jsonrpc(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    return _g.call_bridge_jsonrpc(tool_name, arguments, timeout=timeout)


def _is_valid_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _has_explicit_new_market_switches(cfg: dict[str, Any]) -> bool:
    sources = cfg.get("sources")
    if isinstance(sources, dict):
        return any(k in sources for k in _NEW_MARKET_SOURCE_KEYS)
    return False


# 显式配置键白名单：严格对应 origin/dalton/main:plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py
# 实际读取过的配置键；旧四家（MSFT/META/AMZN/GOOGL）在加载配置与采集时过滤为此集合，忽略所有新增配置项。
LEGACY_COLLECT_CONFIG_KEYS: frozenset[str] = frozenset({
    "ae_company_names",
    "ae_max_searches",
    "ae_queries",
    "ae_search_in",
    "alias_terms",
    "aliases_regex",
    "excluded_documents",
    "extra_consensus_flags",
    "internal_limit",
    "internal_read_cap_per_route",
    "internal_routes",
    "model",
    "news_queries",
    "sec_forms",
    "thresholds",
    "wiki_min_alias_hits",
    "x_enrich_max",
    "x_keep_max",
    "x_pool_max",
    "x_queries",
})
# 仓库内旧四家配置将 internal_routes/sec_forms 放在 sources 下；旧四家只保留这两项列表并忽略 sources 下所有新开关。
LEGACY_COLLECT_SOURCES_KEYS: frozenset[str] = frozenset({"internal_routes", "sec_forms"})


def _filter_legacy_config(cfg: dict[str, Any]) -> dict[str, Any]:
    filtered = {k: v for k, v in cfg.items() if k in LEGACY_COLLECT_CONFIG_KEYS}
    if cfg.get("_dalton_cap_alphaengine") is False:
        filtered["_dalton_cap_alphaengine"] = False
    if cfg.get("_dalton_cap_researchdata_bridge") is False:
        filtered["_dalton_cap_researchdata_bridge"] = False
    disabled_routes = cfg.get("_dalton_cap_disabled_internal_routes")
    if isinstance(disabled_routes, list) and disabled_routes:
        filtered["_dalton_cap_disabled_internal_routes"] = list(disabled_routes)
    sources = cfg.get("sources")
    narrowed: dict[str, Any] = {}
    if isinstance(sources, dict):
        narrowed = {k: v for k, v in sources.items() if k in LEGACY_COLLECT_SOURCES_KEYS}
    # A config written for origin/dalton/main keeps these lists at the top level; carry
    # them into `sources`, where the collectors read them, unless `sources` has its own.
    for key in ("internal_routes", "sec_forms"):
        if key not in narrowed and isinstance(cfg.get(key), list):
            narrowed[key] = cfg[key]
    if narrowed:
        filtered["sources"] = narrowed
    return filtered


def _is_legacy_four_verbatim(ticker: str, cfg: dict[str, Any] | None = None) -> bool:
    """旧四家（MSFT/META/AMZN/GOOGL）按 ticker 固定走逐字兼容主分支流程，忽略新市场开关。"""
    _ = cfg
    return str(ticker or "").strip().upper() in LEGACY_VERBATIM_TICKERS


def _has_price(cons: dict[str, Any]) -> bool:
    price = cons.get("price") if isinstance(cons, dict) else None
    return isinstance(price, dict) and _is_valid_number(price.get("close")) and "error" not in price


def _period_has_valid_estimate(
    cons: dict[str, Any],
    period: str,
    table_keys: tuple[str, ...] = ("earnings_estimate", "revenue_estimate"),
) -> bool:
    """某一期间只有分析师人数 > 0 且存在非空预测数值（avg/low/high）时才算有效。"""
    if not isinstance(cons, dict):
        return False
    for key in table_keys:
        tbl = cons.get(key)
        if not isinstance(tbl, dict) or "error" in tbl:
            continue
        row = tbl.get(period)
        if not isinstance(row, dict):
            continue
        if not any(_is_valid_number(row.get(field)) for field in ("avg", "low", "high")):
            continue
        analysts = row.get("numberOfAnalysts")
        if _is_valid_number(analysts) and float(analysts) > 0:
            return True
    return False


def _valid_estimate_periods(
    cons: dict[str, Any],
    table_keys: tuple[str, ...] = ("earnings_estimate", "revenue_estimate"),
) -> set[str]:
    if not isinstance(cons, dict):
        return set()
    periods: set[str] = set()
    for key in table_keys:
        tbl = cons.get(key)
        if not isinstance(tbl, dict) or "error" in tbl:
            continue
        for per in tbl:
            per_s = str(per)
            if _period_has_valid_estimate(cons, per_s, (key,)):
                periods.add(per_s)
    return periods


def _has_estimates(cons: dict[str, Any]) -> bool:
    """Return True only when at least one period has analysts > 0 and non-null forecasts."""
    return bool(_valid_estimate_periods(cons))


def _has_earnings_calendar(cons: dict[str, Any]) -> bool:
    if not isinstance(cons, dict):
        return False
    cal = cons.get("earnings_dates")
    if isinstance(cal, list):
        return bool(cal)
    if isinstance(cal, dict):
        return bool(cal) and "error" not in cal
    return False


def _is_yfinance_no_data_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return False
    exc_name = type(exc).__name__
    if exc_name in {"YFDataException", "YFRateLimitError", "YFNotImplementedError", "YFInvalidPeriodError"}:
        return False
    if exc_name in _YFINANCE_NO_DATA_TYPES:
        return True
    msg = str(exc).lower()
    if any(token in msg for token in ("timeout", "timed out", "rate limit", "too many requests", "429", "500", "502", "503", "504", "connection")):
        return False
    return any(marker in msg for marker in _YFINANCE_NO_DATA_MARKERS)


def _error_text(exc: BaseException, limit: int = 200) -> str:
    msg = str(exc).strip()
    return (msg or type(exc).__name__)[:limit]


def _safe_price_snapshot_with_status(
    yf_mod: Any, ticker: str, benchmark: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """Safely build 3-month price summary and per-symbol fetch outcomes."""
    syms = list(dict.fromkeys([ticker, benchmark]))
    raw = yf_mod.download(syms, period="3mo", auto_adjust=False, progress=False)
    if raw is None or getattr(raw, "empty", True):
        return {"error": f"空行情表：{ticker}"}, {sym: "empty" for sym in syms}
    close = raw["Close"] if (isinstance(raw, dict) or (hasattr(raw, "__contains__") and "Close" in raw)) else raw
    if close is None or getattr(close, "empty", True):
        return {"error": f"空行情表：{ticker}"}, {sym: "empty" for sym in syms}
    close = close.dropna(how="all")
    if close is None or len(close) == 0:
        return {"error": f"空行情表：{ticker}"}, {sym: "empty" for sym in syms}
    sym_status: dict[str, str] = {}
    if hasattr(close, "columns"):
        if ticker not in close.columns:
            sym_status[ticker] = "missing_column"
            if benchmark != ticker:
                if benchmark in close.columns:
                    sym_status[benchmark] = "ok" if len(close[benchmark].dropna()) > 0 else "no_rows"
                else:
                    sym_status[benchmark] = "missing_column"
            return {"error": f"行情缺少 {ticker} 列"}, sym_status
        t_s = close[ticker].dropna()
        sym_status[ticker] = "ok" if len(t_s) > 0 else "no_rows"
        if benchmark in close.columns:
            b_s = close[benchmark].dropna()
            sym_status[benchmark] = "ok" if len(b_s) > 0 else "no_rows"
        elif ticker == benchmark:
            b_s = t_s
        else:
            b_s = None
            sym_status[benchmark] = "missing_column"
    else:
        t_s = close.dropna()
        b_s = t_s if ticker == benchmark else None
        sym_status[ticker] = "ok" if len(t_s) > 0 else "no_rows"
        if benchmark != ticker:
            sym_status[benchmark] = "missing_column"
    if len(t_s) == 0:
        sym_status[ticker] = "no_rows"
        return {"error": f"空行情：{ticker} 无有效收盘价"}, sym_status
    last_idx = t_s.index[-1]
    last_p = float(t_s.iloc[-1])
    as_of = last_idx.date().isoformat() if hasattr(last_idx, "date") else str(last_idx)[:10]
    if len(t_s) == 1:
        return {
            "as_of": as_of,
            "close": round(last_p, 2),
            "chg_1d_pct": None,
            "chg_5d_pct": None,
            "benchmark": benchmark,
            "bench_chg_1d_pct": None,
            "bench_chg_5d_pct": None,
            "high_3m": round(last_p, 2),
            "drawdown_from_3m_high_pct": 0.0,
        }, sym_status
    prev_idx = t_s.index[-2]
    prev_p = float(t_s.iloc[-2])
    chg_1d = round((last_p / prev_p - 1.0) * 100.0, 2) if prev_p else None
    b_chg_1d = None
    if b_s is not None and last_idx in b_s.index and prev_idx in b_s.index:
        b_last = float(b_s.loc[last_idx])
        b_prev = float(b_s.loc[prev_idx])
        if b_prev:
            b_chg_1d = round((b_last / b_prev - 1.0) * 100.0, 2)

    chg_5d = None
    b_chg_5d = None
    if len(t_s) >= 6:
        five_idx = t_s.index[-6]
        five_p = float(t_s.iloc[-6])
        if five_p:
            chg_5d = round((last_p / five_p - 1.0) * 100.0, 2)
        if b_s is not None and last_idx in b_s.index and five_idx in b_s.index:
            b_last = float(b_s.loc[last_idx])
            b_five = float(b_s.loc[five_idx])
            if b_five:
                b_chg_5d = round((b_last / b_five - 1.0) * 100.0, 2)

    high_3m = float(t_s.max())
    return {
        "as_of": as_of,
        "close": round(last_p, 2),
        "chg_1d_pct": chg_1d,
        "chg_5d_pct": chg_5d,
        "benchmark": benchmark,
        "bench_chg_1d_pct": b_chg_1d,
        "bench_chg_5d_pct": b_chg_5d,
        "high_3m": round(high_3m, 2),
        "drawdown_from_3m_high_pct": round((last_p / high_3m - 1.0) * 100.0, 2) if high_3m else None,
    }, sym_status


def _safe_earnings_dates(ticker_obj: Any) -> list[dict[str, Any]]:
    cal = getattr(ticker_obj, "earnings_dates", None)
    if cal is None or not isinstance(getattr(cal, "empty", None), bool) or cal.empty:
        return []
    frame = cal.head(4)
    if hasattr(frame, "reset_index"):
        frame = frame.reset_index()
    rows = _g.clean_val(frame.to_dict(orient="records"))
    return rows if isinstance(rows, list) else []


def _fetch_configured_market_data(
    ticker: str,
    benchmark: str = "QQQ",
    *,
    include_consensus: bool = True,
    include_price: bool = True,
    include_earnings_calendar: bool = False,
) -> dict[str, Any]:
    """Fetch only enabled market/consensus/calendar sources safely; empty/no-data is non-fatal."""
    fetched_at = now_et().isoformat()
    out: dict[str, Any] = {
        "_meta": {
            "route": "estimates",
            "fetched_at": fetched_at,
            "source": "Yahoo Finance via yfinance",
        },
        "ticker": ticker,
        "fetched_at": fetched_at,
        "source": "Yahoo Finance via yfinance",
        "currency": None,
        "earnings_estimate": {},
        "revenue_estimate": {},
        "eps_trend": {},
        "eps_revisions": {},
        "price_targets": {},
        "price": {},
    }
    if not (include_consensus or include_price or include_earnings_calendar):
        out["ok"] = True
        out["status"] = "empty"
        out["field_status"] = {
            "earnings_estimate": "skipped",
            "revenue_estimate": "skipped",
            "eps_trend": "skipped",
            "eps_revisions": "skipped",
            "price_targets": "skipped",
            "price": "skipped",
            "earnings_dates": "skipped",
        }
        out["earnings_dates"] = []
        out["_meta"]["field_status"] = dict(out["field_status"])
        out["_meta"]["status"] = "empty"
        return out

    earnings_dates: list[dict[str, Any]] = []
    cal_error: str | None = None
    price_symbols: dict[str, str] = {}
    try:
        import yfinance as yf  # type: ignore
    except Exception as exc:
        msg = _error_text(exc, 300)
        err = _error_text(exc, 200)
        if include_consensus:
            out["error"] = msg
        if include_price:
            out["price"] = {"error": err}
            price_symbols = {sym: f"error: {err}" for sym in list(dict.fromkeys([ticker, benchmark]))}
        if include_earnings_calendar:
            cal_error = err
        yf = None

    if yf is not None:
        if include_price:
            try:
                out["price"], price_symbols = _safe_price_snapshot_with_status(yf, ticker, benchmark)
            except Exception as exc:
                err = _error_text(exc)
                out["price"] = {"error": err}
                price_symbols = {sym: f"error: {err}" for sym in list(dict.fromkeys([ticker, benchmark]))}

        if include_consensus or include_earnings_calendar:
            t = None
            cons_init_error: str | None = None
            cal_init_error: str | None = None
            try:
                t = yf.Ticker(ticker)
            except Exception as exc:
                if include_earnings_calendar:
                    cal_init_error = _error_text(exc)
                if include_consensus and not _is_yfinance_no_data_error(exc):
                    cons_init_error = _error_text(exc, 300)

            if cons_init_error is not None:
                out["error"] = cons_init_error
            if cal_init_error is not None:
                cal_error = cal_init_error
            if t is not None:
                if include_consensus and cons_init_error is None:
                    for field in ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions"):
                        try:
                            df = getattr(t, field, None)
                            if df is not None and isinstance(getattr(df, "empty", None), bool) and not df.empty:
                                out[field] = _g.clean_val(df.to_dict(orient="index"))
                        except Exception as exc:
                            if not _is_yfinance_no_data_error(exc):
                                out[field] = {"error": _error_text(exc)}

                    period_ends = _g._extract_period_end_dates(t)
                    for field in ("earnings_estimate", "revenue_estimate"):
                        table = out.get(field)
                        if isinstance(table, dict) and period_ends:
                            for per, row in table.items():
                                if isinstance(row, dict) and period_ends.get(per):
                                    row["period_end"] = period_ends[per]

                    try:
                        targets = getattr(t, "analyst_price_targets", None)
                        if targets:
                            out["price_targets"] = _g.clean_val(dict(targets))
                    except Exception as exc:
                        if not _is_yfinance_no_data_error(exc):
                            out["price_targets"] = {"error": _error_text(exc)}

                if include_earnings_calendar and cal_init_error is None:
                    try:
                        earnings_dates = _safe_earnings_dates(t)
                    except Exception as exc:
                        cal_error = _error_text(exc)

    _g._compute_consensus_status(out)
    out["earnings_dates"] = earnings_dates
    fs = dict(out.get("field_status")) if isinstance(out.get("field_status"), dict) else {}
    if not include_consensus:
        for field in ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions", "price_targets"):
            fs[field] = "skipped"
    if not include_price:
        fs["price"] = "skipped"
    if not include_earnings_calendar:
        fs["earnings_dates"] = "skipped"
    elif cal_error:
        fs["earnings_dates"] = "error"
        out["earnings_dates_error"] = cal_error
    elif earnings_dates:
        fs["earnings_dates"] = "ok"
    else:
        fs["earnings_dates"] = "empty"

    out["field_status"] = fs
    cons_errors = any(
        include_consensus and fs.get(k) == "error"
        for k in ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions", "price_targets")
    )
    price_error = include_price and fs.get("price") == "error"
    enabled_errors = cons_errors or price_error or bool(out.get("error")) or bool(cal_error)

    if enabled_errors:
        has_any_ok = (
            (include_consensus and _has_estimates(out))
            or (include_price and _has_price(out))
            or (include_earnings_calendar and bool(earnings_dates))
        )
        out["ok"] = has_any_ok
        out["status"] = "partial" if has_any_ok else "error"
    else:
        has_enabled_data = (
            (include_consensus and _has_estimates(out))
            or (include_price and _has_price(out))
            or (include_earnings_calendar and bool(earnings_dates))
        )
        all_enabled_ok = (
            (not include_consensus or _has_estimates(out))
            and (not include_price or _has_price(out))
            and (not include_earnings_calendar or bool(earnings_dates))
        )
        out["ok"] = True
        if has_enabled_data and (all_enabled_ok or not include_consensus or _has_estimates(out)):
            out["status"] = "ok"
        else:
            out["status"] = "empty"
    if isinstance(out.get("_meta"), dict):
        out["_meta"]["status"] = out["status"]
        out["_meta"]["field_status"] = dict(fs)
        if include_price:
            out["_meta"]["price_symbols"] = price_symbols
    return out


def fetch_consensus_snapshot(ticker: str, benchmark: str = "QQQ") -> dict[str, Any]:
    return _fetch_configured_market_data(
        ticker,
        benchmark,
        include_consensus=True,
        include_price=True,
        include_earnings_calendar=True,
    )


_DEFAULT_FETCH_CONSENSUS = fetch_consensus_snapshot


def resolve_researchdata_transport() -> dict[str, Any]:
    return _g.resolve_researchdata_transport()


def now_et() -> datetime:
    return _g.now_et()


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def ae_state_key(ticker: str | None = None) -> str:
    """续采状态键。MSFT 线上键名是 msft_ae_state，由 ticker 小写推出。"""
    return f"{(ticker or TICKER).lower()}_ae_state"


def collect_lock_name(ticker: str | None = None) -> str:
    # 线上 MSFT 锁文件名是 .msft-collect.lock，由 ticker 小写推出，不要改成别的文件名。
    return f".{(ticker or TICKER).lower()}-collect.lock"


def research_config_path(ticker: str | None = None) -> Path:
    return SKILL_DIR / "config" / f"{(ticker or TICKER).upper()}.json"


def fail(status: str, message: str, **extra: Any) -> int:
    body: dict[str, Any] = {"ok": False, "status": status, "ticker": TICKER, "error": message}
    body.update(extra)
    return emit(body, 2)


def _inside(workspace: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(workspace.resolve())
    except (OSError, ValueError):
        return False
    return True


def _reject_openclaw(path: Path) -> None:
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise CollectError("workspace_unreadable", str(exc)[:200]) from exc
    if ".openclaw" in resolved.parts:
        raise CollectError("openclaw_root", "拒绝读写旧 OpenClaw 根目录")


def open_workspace(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    _reject_openclaw(path)
    resolved = path.resolve()
    _reject_openclaw(resolved)
    if not resolved.is_dir():
        raise CollectError("workspace_not_directory", f"工作区不是目录：{raw}")
    return resolved


def assert_no_escaping_symlinks(workspace: Path, root: Path) -> None:
    """工作区里 research/tracker 和 data/consensus 的符号链接不能指向外面。"""
    if not root.exists():
        return
    if root.is_symlink() and not _inside(workspace, root):
        raise CollectError("symlink_escape", f"目录指向工作区外：{root}")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames) + list(filenames):
            cand = Path(dirpath) / name
            if cand.is_symlink() and not _inside(workspace, cand):
                raise CollectError("symlink_escape", f"符号链接指向工作区外：{cand}")


def ensure_relative(workspace: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise CollectError("path_escape", f"拒绝越出工作区的路径：{relative}")
    current = workspace
    for part in relative.parts:
        if part in ("", ".", ".."):
            raise CollectError("path_escape", f"拒绝越出工作区的路径：{relative}")
        current = current / part
        if current.is_symlink() and not _inside(workspace, current):
            raise CollectError("symlink_escape", f"符号链接指向工作区外：{current}")
        if current.is_symlink():
            current = current.resolve()
            if not _inside(workspace, current):
                raise CollectError("symlink_escape", f"符号链接指向工作区外：{relative}")
    if not _inside(workspace, current):
        raise CollectError("path_escape", f"拒绝越出工作区的路径：{relative}")
    return current


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None or ".." in run_id:
        raise CollectError("bad_run_id", "run_id 只能是一段不含路径的字母、数字、点、下划线或连字符")
    return run_id


def parse_day(value: str, label: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise CollectError("bad_date", f"{label} 需要 YYYY-MM-DD") from exc


def atomic_write_text(workspace: Path, path: Path, text: str) -> None:
    if not _inside(workspace, path.parent):
        raise CollectError("path_escape", f"拒绝写入工作区外：{path}")
    if path.is_symlink():
        raise CollectError("symlink_escape", f"拒绝透过符号链接写入：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_json(workspace: Path, path: Path, obj: Any) -> None:
    atomic_write_text(
        workspace,
        path,
        json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n",
    )


@contextmanager
def workspace_lock(workspace: Path, tracker: Path) -> Iterator[None]:
    tracker.mkdir(parents=True, exist_ok=True)
    lock_path = tracker / collect_lock_name()
    if not _inside(workspace, lock_path):
        raise CollectError("path_escape", "锁文件越出工作区")
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def load_research_config() -> dict[str, Any]:
    cfg_mod = _load_tracker_config()
    data = cfg_mod.load_validated_config(TICKER, skill_dir=SKILL_DIR)
    if data is None:
        raise CollectError("unsupported_ticker", f"不支持的 ticker：{TICKER or '空'}")
    cfg = _filter_legacy_config(data) if _is_legacy_four_verbatim(TICKER) else data
    _parse_ae_queries(cfg)
    return cfg


def load_company(workspace: Path) -> dict[str, Any]:
    path = workspace / "data" / "company.json"
    if not path.exists():
        return {}
    full = ensure_relative(workspace, Path("data") / "company.json")
    try:
        data = json.loads(full.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectError("company_json_unreadable", str(exc)[:200]) from exc
    if not isinstance(data, dict):
        raise CollectError("company_json_unreadable", "data/company.json 不是对象")
    ticker = str(data.get("ticker") or TICKER).upper()
    if ticker != TICKER:
        raise CollectError("company_mismatch", f"工作区公司是 {ticker}，本采集器只处理 {TICKER}")
    return data


def benchmark_of(company: dict[str, Any]) -> str:
    benchmarks = company.get("benchmarks")
    if isinstance(benchmarks, list) and benchmarks:
        return str(benchmarks[0])
    return "QQQ"


def map_thresholds(raw: Any) -> dict[str, Any]:
    """把旧 MSFT 阈值键映射到通用 compute_consensus_deltas 的键。"""
    src = raw if isinstance(raw, dict) else {}
    mapped = {
        "eps_fy1_vs_last_pct": 1.0,
        "eps_fy1_vs_7d_pct": 2.0,
        "rev_fy1_or_q1_vs_last_pct": 0.7,
        "target_price_vs_last_pct": 2.0,
        "down_revisions_7d_min": 5,
        "price_1d_pct": 4.0,
        "price_5d_pct": 8.0,
        "any_new_sec_filing": True,
    }
    for old, new in _THRESHOLD_MAP.items():
        if old in src:
            mapped[new] = src[old]
    for key in ("price_1d_pct", "price_5d_pct", "any_new_sec_filing"):
        if key in src:
            mapped[key] = src[key]
    for key in list(mapped):
        if key in src:
            mapped[key] = src[key]
    return mapped


def _safe_load_research_config() -> dict[str, Any]:
    try:
        return load_research_config()
    except CollectError:
        return {}


def _sanitize_consensus_by_valid_periods(cons: dict[str, Any]) -> dict[str, Any]:
    """Keep only estimate/trend/revision periods with valid analyst count (> 0) and non-null forecasts."""
    if not isinstance(cons, dict):
        return {}
    valid_eps = _valid_estimate_periods(cons, ("earnings_estimate",))
    valid_rev = _valid_estimate_periods(cons, ("revenue_estimate",))
    valid_any = valid_eps | valid_rev
    cleaned = dict(cons)
    ee = cons.get("earnings_estimate")
    if isinstance(ee, dict) and "error" not in ee:
        cleaned["earnings_estimate"] = {k: v for k, v in ee.items() if str(k) in valid_eps}
    re_tbl = cons.get("revenue_estimate")
    if isinstance(re_tbl, dict) and "error" not in re_tbl:
        cleaned["revenue_estimate"] = {k: v for k, v in re_tbl.items() if str(k) in valid_rev}
    trend = cons.get("eps_trend")
    if isinstance(trend, dict) and "error" not in trend:
        cleaned["eps_trend"] = {k: v for k, v in trend.items() if str(k) in valid_eps}
    revs = cons.get("eps_revisions")
    if isinstance(revs, dict) and "error" not in revs:
        cleaned["eps_revisions"] = {k: v for k, v in revs.items() if str(k) in valid_eps}
    if not valid_any:
        cleaned["price_targets"] = {}
    return cleaned


def consensus_deltas(
    cur: dict[str, Any],
    prev: dict[str, Any] | None,
    thr: dict[str, Any],
    sec_new_count: int = 0,
    extra_flags: Any = None,
    *,
    legacy_verbatim: bool | None = None,
) -> tuple[dict[str, Any], list[str]]:
    if legacy_verbatim is None:
        is_legacy = _is_legacy_four_verbatim(TICKER, _safe_load_research_config())
    else:
        is_legacy = legacy_verbatim
    if is_legacy:
        safe_cur, safe_prev = cur, prev
    else:
        safe_cur = _sanitize_consensus_by_valid_periods(cur)
        safe_prev = _sanitize_consensus_by_valid_periods(prev) if prev is not None else None
    deltas, flags = _g.compute_consensus_deltas(safe_cur, safe_prev, thr, sec_new_count=sec_new_count)
    flags = list(flags)
    specs = _configured_extra_flags() if extra_flags is None else extra_flags
    _append_extra_flags(deltas, flags, thr, specs)
    return deltas, flags


def _configured_extra_flags() -> list[Any]:
    try:
        cfg = load_research_config()
    except CollectError:
        return []
    specs = cfg.get("extra_consensus_flags")
    return specs if isinstance(specs, list) else []


def _append_extra_flags(deltas: dict[str, Any], flags: list[str], thr: dict[str, Any], specs: Any) -> None:
    # 通用函数已用 eps_fy1_vs_last_pct 报过「本财年(0y) EPS 较上次运行」，这里不重复。
    # MSFT 的两条额外触发在 config/MSFT.json 的 extra_consensus_flags，标签和阈值键保持原样。
    if not isinstance(specs, list):
        return

    def add(key: str, limit: Any, label: str) -> None:
        val = deltas.get(key)
        if val is None:
            return
        if abs(float(val)) >= float(limit):
            flags.append(f"{label} {float(val):+.2f}%（阈值 ±{float(limit)}%）")

    for spec in specs:
        if not isinstance(spec, dict):
            continue
        delta = spec.get("delta")
        threshold = spec.get("threshold")
        label = spec.get("label")
        if not isinstance(delta, str) or not isinstance(threshold, str) or not isinstance(label, str):
            continue
        add(delta, thr.get(threshold, spec.get("default", 0)), label)


def _fy_label(raw: str) -> str:
    year = int(raw)
    if year < 100:
        return f"FY{year:02d}"
    return f"FY{year}"


def _fiscal_end_month(model: dict[str, Any]) -> int | None:
    raw = model.get("fiscal_year_end_month")
    if type(raw) is not int or not 1 <= raw <= 12:
        return None
    return raw


def _basis_year(model: dict[str, Any]) -> int | None:
    raw = model.get("basis_fiscal_year")
    if type(raw) is not int or raw < 1900:
        return None
    return raw


def other_income_for_fiscal_year(model: dict[str, Any], fiscal_year: int) -> Any:
    """配置里的 0y 属于 basis_fiscal_year，+1y 属于下一年。没有对应键时返回未知。"""
    other = model.get("other_income_bn") if isinstance(model.get("other_income_bn"), dict) else {}
    basis = _basis_year(model)
    if basis is None:
        return None
    offset = fiscal_year - basis
    if offset == 0:
        key = "0y"
    elif offset > 0:
        key = f"+{offset}y"
    else:
        key = f"{offset}y"
    if key not in other:
        return None
    return other[key]


def _years_from_fiscal_month(model: dict[str, Any]) -> list[dict[str, Any]]:
    """当前日期决定 0y 和 +1y 是哪两个财年。其他收入按基准财年换算，不跟着日历年滚动。"""
    month = _fiscal_end_month(model)
    if month is None:
        return []
    today = now_et().date()
    end_year = today.year if today.month <= month else today.year + 1
    years: list[dict[str, Any]] = []
    for offset, key in ((0, "0y"), (1, "+1y")):
        year = end_year + offset
        income = other_income_for_fiscal_year(model, year)
        item: dict[str, Any] = {
            "key": key,
            "fiscal_year": f"FY{year}",
            "period_end_prefix": f"{year}-{month:02d}",
            "other_income_bn": income,
        }
        if income is None:
            item["other_income_reason"] = f"FY{year} 没有其他收入假设，未计算隐含费用"
        years.append(item)
    return years


def applicable_years(model: dict[str, Any]) -> list[dict[str, Any]]:
    """从来源原文解析 0y/+1y 各自对应的财年末。

    来源里能解析出截止月时，其他收入仍取配置里的 0y 和 +1y。
    解析不到时用 fiscal_year_end_month 和当前日期得到实际财年，再减去 basis_fiscal_year 回到配置里的相对键。
    """
    source = str(model.get("source") or "")
    other = model.get("other_income_bn") if isinstance(model.get("other_income_bn"), dict) else {}
    match = _OY_RE.search(source)
    if not match:
        return _years_from_fiscal_month(model)
    month = match.group(3)
    years = [{
        "key": "0y",
        "fiscal_year": _fy_label(match.group(1)),
        "period_end_prefix": f"{match.group(2)}-{month}",
        "other_income_bn": other.get("0y"),
    }]
    plus = _PY_RE.search(source)
    if plus:
        fy = int(plus.group(1))
        end_year = fy if fy >= 100 else 2000 + fy
        years.append({
            "key": "+1y",
            "fiscal_year": _fy_label(plus.group(1)),
            "period_end_prefix": f"{end_year}-{month}",
            "other_income_bn": other.get("+1y"),
        })
    return years


def _period_row(table: Any, key: str) -> dict[str, Any]:
    if not isinstance(table, dict):
        return {}
    row = table.get(key)
    return row if isinstance(row, dict) else {}


def _valid_annual_period_pair(ee_valid: Any, re_valid: Any, per: str) -> bool:
    eps_row = _period_row(ee_valid, per)
    rev_row = _period_row(re_valid, per)
    if not eps_row or not rev_row:
        return False
    eps_end = _g._period_end_of(eps_row)
    rev_end = _g._period_end_of(rev_row)
    return eps_end is not None and rev_end is not None and eps_end[:10] == rev_end[:10]


def build_implied_model(
    cons: dict[str, Any],
    model: dict[str, Any],
    *,
    require_valid_periods: bool = False,
) -> dict[str, Any]:
    """沿用旧公式。其他收入只用于来源写明的财年，不随 0y/+1y 标签滚到下一年。"""
    if not isinstance(model, dict) or not model:
        return {
            "assumptions": {
                "diluted_shares_bn": None,
                "tax_rate": None,
                "other_income_bn": {},
                "source": "",
                "applicable_years": [],
                "note": "未配置隐含费用模型假设，标记为暂无",
            },
            "periods": {},
            "status": "empty",
            "summary": "暂无",
        }
    if require_valid_periods:
        cons = _sanitize_consensus_by_valid_periods(cons)
    shares = model.get("diluted_shares_bn")
    tax = model.get("tax_rate")
    other = model.get("other_income_bn") if isinstance(model.get("other_income_bn"), dict) else {}
    source = str(model.get("source") or "")
    years = applicable_years(model)
    assumptions = {
        "diluted_shares_bn": shares,
        "tax_rate": tax,
        "other_income_bn": other,
        "source": source,
        "applicable_years": years,
        "note": "隐含总费用 = 一致收入 − EPS×股数/(1−税率) + 其他收入；单位十亿美元。其他收入只用于来源写明的财年末。",
    }
    if require_valid_periods:
        ee_valid = cons.get("earnings_estimate") if isinstance(cons.get("earnings_estimate"), dict) else {}
        re_valid = cons.get("revenue_estimate") if isinstance(cons.get("revenue_estimate"), dict) else {}
        if not all(_valid_annual_period_pair(ee_valid, re_valid, per) for per in ("0y", "+1y")):
            return {
                "assumptions": assumptions,
                "periods": {},
                "status": "empty",
                "summary": "暂无",
            }
    out: dict[str, Any] = {"assumptions": assumptions, "periods": {}}
    try:
        shares_f = float(shares)
        tax_f = float(tax)
    except (TypeError, ValueError):
        out["error"] = "股数或税率缺失，未计算隐含费用"
        return out
    if shares_f <= 0 or not 0 <= tax_f < 1:
        out["error"] = "股数须为正数，税率须满足 0≤税率<1，未计算隐含费用"
        return out
    if not years:
        out["error"] = "来源原文里没有解析出 0y/+1y 对应的财年末，未计算隐含费用"
        for key in ("0y", "+1y"):
            out["periods"][key] = {"status": "applicable_years_unparsed", "reason": out["error"]}
        return out

    by_prefix = {item["period_end_prefix"]: item for item in years}
    ee = cons.get("earnings_estimate")
    re_ = cons.get("revenue_estimate")
    for key, label in (("0y", "本财年"), ("+1y", "下一财年")):
        eps_row = _period_row(ee, key)
        rev_row = _period_row(re_, key)
        eps_end = _g._period_end_of(eps_row)
        rev_end = _g._period_end_of(rev_row)
        entry: dict[str, Any] = {"label": label, "status": "ok"}
        if eps_end is None or rev_end is None:
            entry.update({"status": "missing_period", "reason": f"{key} 缺少 period_end，无法确认财年，未计算隐含费用"})
        elif eps_end[:7] != rev_end[:7]:
            entry.update({
                "status": "period_mismatch",
                "earnings_period_end": eps_end,
                "revenue_period_end": rev_end,
                "reason": f"{key} 的 EPS 期间 {eps_end} 与收入期间 {rev_end} 不是同一个月，未计算隐含费用",
            })
        else:
            prefix = eps_end[:7]
            entry["period_end"] = eps_end
            matched = by_prefix.get(prefix)
            currencies = [eps_row.get("currency"), rev_row.get("currency")]
            eps = eps_row.get("avg")
            rev = rev_row.get("avg")
            if matched is None:
                known = "、".join(f"{item['fiscal_year']} 截至 {item['period_end_prefix']}" for item in years)
                entry.update({
                    "status": "period_incompatible",
                    "reason": (
                        f"{key} 的 period_end {eps_end} 对不上来源里写明的财年（{known}），"
                        "未把来源里另一个财年的其他收入套到这个财年"
                    ),
                })
            elif currencies != ["USD", "USD"]:
                entry.update({
                    "status": "currency_incomparable",
                    "currencies": currencies,
                    "reason": f"{key} 的 EPS/收入币种为 {currencies}，需要两行都是 USD 才计算",
                })
            elif eps is None or rev is None or rev == 0:
                entry.update({"status": "missing_inputs", "reason": f"{key} 缺少 EPS 或收入，未计算隐含费用"})
            elif matched.get("other_income_bn") is None:
                entry.update({
                    "status": "other_income_missing",
                    "fiscal_year": matched.get("fiscal_year"),
                    "reason": f"{matched.get('fiscal_year')} 没有其他收入假设，未计算隐含费用",
                })
            else:
                oth = float(matched["other_income_bn"])
                rev_bn = float(rev) / 1e9
                pretax = float(eps) * shares_f / (1 - tax_f)
                entry.update({
                    "fiscal_year": matched["fiscal_year"],
                    "other_income_key": matched["key"],
                    "other_income_bn": oth,
                    "consensus_revenue_bn": round(rev_bn, 1),
                    "consensus_eps": round(float(eps), 2),
                    "implied_pretax_bn": round(pretax, 1),
                    "implied_total_expenses_bn": round(rev_bn - pretax + oth, 1),
                    "implied_op_margin_pct": round((pretax - oth) / rev_bn * 100, 1),
                    "eps_per_10bn_expense": round(-10 * (1 - tax_f) / shares_f, 2),
                    "eps_per_1pct_revenue_no_cost": round(rev_bn * 0.01 * (1 - tax_f) / shares_f, 2),
                })
        out["periods"][key] = entry
        if entry.get("status") == "ok":
            out[key] = entry
    return out


def excerpt(text: str, pat: re.Pattern[str], limit: int = EXCERPT_MAX) -> str:
    paras = re.split(r"\n\s*\n|(?<=\n)(?=[A-Z]{2,5}[:：/])", text)
    hits: list[str] = []
    dup: set[str] = set()
    for para in paras:
        cleaned = "\n".join(
            line for line in para.strip().splitlines() if _HEADER_RE.match(line) is None
        ).strip()
        if cleaned and cleaned not in dup and pat.search(cleaned):
            dup.add(cleaned)
            hits.append(cleaned)
    out: list[str] = []
    used = 0
    for hit in hits:
        hit = hit[:PARA_MAX]
        if used + len(hit) > limit:
            out.append("…（后续命中段落已截断，需要时用 read 全文）")
            break
        out.append(hit)
        used += len(hit)
    return "\n\n".join(out)


def unwrap_content(data: dict[str, Any]) -> dict[str, Any]:
    """通用提取不看对象自身顶层的 content 文本块，这里补上旧 MCP 封装。"""
    content = data.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                try:
                    parsed = json.loads(block["text"])
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return data


def extract_items_and_page(data: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(data, dict):
        return [], {"cursor": None, "has_more": False, "total_upstream": None, "returned": None}
    items = _g._extract_route_items(data)
    page = _g._extract_route_page_info(data)
    if not isinstance(items, list):
        items = []
    if items:
        return [item for item in items if isinstance(item, dict)], page
    unwrapped = unwrap_content(data)
    if unwrapped is not data:
        nested = _g._extract_route_items(unwrapped)
        page2 = _g._extract_route_page_info(unwrapped)
        if isinstance(nested, list) and nested:
            return [item for item in nested if isinstance(item, dict)], page2
        if page2.get("has_more") or page2.get("cursor"):
            return [], page2
    return [], page


def _payload_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found = [payload]
    structured = payload.get("structuredContent")
    if isinstance(structured, dict):
        found.append(structured)
    inner = payload.get("data")
    if isinstance(inner, dict):
        found.append(inner)
        nested = inner.get("structuredContent")
        if isinstance(nested, dict):
            found.append(nested)
    return found


def extract_document(data: Any) -> tuple[str, Any, dict[str, Any]]:
    bases: list[dict[str, Any]] = []
    if isinstance(data, dict):
        bases.append(data)
        unwrapped = unwrap_content(data)
        if unwrapped is not data:
            bases.append(unwrapped)
    for base in bases:
        for candidate in _payload_dicts(base):
            doc = candidate.get("document") if isinstance(candidate.get("document"), dict) else candidate
            if not isinstance(doc, dict):
                continue
            if not any(key in doc for key in ("content", "text", "next_offset")):
                continue
            text = doc.get("content") if isinstance(doc.get("content"), str) else doc.get("text")
            if not isinstance(text, str):
                text = ""
            nxt = doc.get("next_offset")
            if nxt is None:
                nxt = candidate.get("next_offset")
            meta = {
                key: value
                for key, value in doc.items()
                if key not in ("content", "text", "images") and not isinstance(value, (dict, list))
            }
            return text, nxt, meta
    return "", None, {}


def is_no_match(res: dict[str, Any], data: Any) -> bool:
    payloads = [data] if isinstance(data, dict) else []
    if isinstance(data, dict):
        unwrapped = unwrap_content(data)
        if unwrapped is not data:
            payloads.append(unwrapped)
    return any(_g._is_bridge_no_match(res, payload) for payload in payloads)


def is_cursor_invalid(res: dict[str, Any], data: Any) -> bool:
    payloads = [data] if isinstance(data, dict) else [{}]
    if isinstance(data, dict):
        unwrapped = unwrap_content(data)
        if unwrapped is not data:
            payloads.append(unwrapped)
    return any(_g._is_cursor_invalid_error(res, payload) for payload in payloads)


def is_upstream_cursor_error(res: dict[str, Any], data: Any) -> bool:
    """MCP 在上游声明还有数据但缺失或重复游标时返回 E_UPSTREAM_CURSOR。"""
    payloads: list[dict[str, Any]] = [res] if isinstance(res, dict) else []
    if isinstance(data, dict):
        payloads.append(data)
        unwrapped = unwrap_content(data)
        if unwrapped is not data:
            payloads.append(unwrapped)
    for payload in payloads:
        for candidate in _payload_dicts(payload):
            for key in ("error_code", "code", "error", "status", "message"):
                raw = candidate.get(key)
                if isinstance(raw, str) and "e_upstream_cursor" in raw.lower():
                    return True
    return False


def bridge_error(res: dict[str, Any], data: Any) -> str:
    if isinstance(data, dict) and data.get("error"):
        return str(data.get("error"))[:500]
    return str(res.get("error") or res.get("status") or "unknown_error")[:500]


def _iter_response_dicts(res: dict[str, Any], data: Any) -> list[dict[str, Any]]:
    """收集响应外层及常见封装层（structuredContent / data / content JSON 块）中的字典。"""
    out: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    queue: list[Any] = []
    if isinstance(res, dict):
        queue.append(res)
    if isinstance(data, dict):
        queue.append(data)
    while queue:
        cur = queue.pop(0)
        if not isinstance(cur, dict) or id(cur) in seen_ids:
            continue
        seen_ids.add(id(cur))
        out.append(cur)
        for key in ("structuredContent", "data"):
            nested = cur.get(key)
            if isinstance(nested, dict):
                queue.append(nested)
        content = cur.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    try:
                        parsed = json.loads(block["text"])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        queue.append(parsed)
    return out


def _is_partial_status(res: dict[str, Any], data: Any) -> bool:
    """响应任一层声明 status=partial 时返回 True（内层 status=error 不覆盖外层 partial）。"""
    for candidate in _iter_response_dicts(res, data):
        st = candidate.get("status")
        if isinstance(st, str) and st.strip().lower() == "partial":
            return True
    return False


def _known_truncation(res: dict[str, Any], data: Any) -> str | None:
    """沿用通用采集对 partial / 截断警告的判断。没有 cursor 时这种响应也是未完成。"""
    payload = data if isinstance(data, dict) else {}
    status_val, warnings = _g._extract_route_warnings_and_status(res, payload)
    if isinstance(data, dict):
        unwrapped = unwrap_content(data)
        if unwrapped is not data:
            u_status, u_warnings = _g._extract_route_warnings_and_status({}, unwrapped)
            if u_status and str(u_status).strip().lower() in ("partial", "incomplete", "未完成"):
                status_val = u_status
            for w in u_warnings:
                if w not in warnings:
                    warnings.append(w)
    if _is_partial_status(res, data):
        status_val = "partial"
    elif str(status_val or "").strip().lower() not in ("partial", "incomplete", "未完成"):
        for candidate in _iter_response_dicts(res, data):
            st = candidate.get("status")
            if isinstance(st, str) and st.strip().lower() in ("incomplete", "未完成"):
                status_val = st.strip()
                break
    status_text = str(status_val or "").strip().lower()
    matched = [item for item in warnings if any(token in item for token in _TRUNCATION_TOKENS)]
    if status_text not in ("partial", "incomplete", "未完成") and not matched:
        return None
    detail = "; ".join(matched or warnings) or f"status={status_text or 'partial'}"
    return f"未完成: 结果被截断或覆盖不完整（{detail}）"


def _parse_aware(value: Any) -> datetime | None:
    """把采集/提交时间收成美东时区。没有时区的字符串按美东理解，非法值返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET)


def _run_id_stamp(run_id: Any) -> datetime | None:
    """导入的 last_run 可能没有采集时间。YYYYMMDD-HHMM 形式的 run_id 当作美东时刻。"""
    if not isinstance(run_id, str):
        return None
    match = _RUN_STAMP_RE.fullmatch(run_id)
    if match is None:
        return None
    try:
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
            tzinfo=ET,
        )
    except ValueError:
        return None


def _order_key(record: dict[str, Any]) -> datetime | None:
    """同一 until 日内的先后。采集时间优先；导入记录没有它时用 run_id 里的时刻，提交时间只作最后兜底。"""
    if not isinstance(record, dict):
        return None
    parsed = _parse_aware(record.get("collected_at"))
    if parsed is not None:
        return parsed
    stamped = _run_id_stamp(record.get("run_id"))
    if stamped is not None:
        return stamped
    return _parse_aware(record.get("committed_at"))


def _coerce_collected_at(packet: dict[str, Any]) -> str | None:
    parsed = _parse_aware(packet.get("collected_at"))
    if parsed is None:
        parsed = _order_key(packet)
    return parsed.isoformat() if parsed is not None else None


def _is_newer_collection(packet: dict[str, Any], last_run: dict[str, Any]) -> bool:
    """until 更早的包不能当最新；同一天或更晚的 until 还要采集时刻严格更晚。"""
    if not last_run or not last_run.get("run_id"):
        return True
    packet_until = str(packet.get("until") or "")
    prev_until = str(last_run.get("until") or "")
    if prev_until and packet_until and packet_until < prev_until:
        return False
    new_key = _order_key(packet)
    old_key = _order_key(last_run)
    later_until = bool(packet_until and prev_until and packet_until > prev_until)
    if new_key is None and old_key is None:
        return later_until
    if old_key is None:
        return True
    if new_key is None:
        return False
    return new_key > old_key


def _log_has_run(text: str, run_id: str) -> bool:
    needle = f" | {run_id} | material="
    return any(needle in line for line in text.splitlines())


def _coerce_retry_since(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_day(value.strip()[:10], "internal_retry_since")
    except CollectError:
        return None


def _source_since(last_run: dict[str, Any], field: str, global_since: str) -> str:
    """上次跳过或失败时写下的实际采集日若早于全局 since，重新开启后从该日起采。"""
    saved = _coerce_day(last_run.get(field) if isinstance(last_run, dict) else None)
    if saved is None:
        return global_since
    return min(global_since, saved)


def _prev_source_collected_until(last_run: Any, field: str) -> str | None:
    """读取该来源上次真正成功采集到的日期。

    若 ``last_run`` 显式记录了 ``field``：有合法日期返回该日期，为 None/空表示从未成功采集，
    不回退到 ``last_run["until"]``；若不含 ``field``（早前的正常成功轮次），回退到 ``until``。
    """
    if not isinstance(last_run, dict):
        return None
    if field in last_run:
        return _coerce_day(last_run.get(field))
    return _coerce_day(last_run.get("until"))


def _resume_point(last_run: Any, field: str, packet: dict[str, Any]) -> str | None:
    """跳过或失败时保存的续采位置。

    有过成功采集就沿用那一天；从未成功采集（例如新工作区首轮即被跳过）时，记下本轮的起点，
    重新开启后从这一天补采，不随每天前进的全局窗口丢掉中间的日子。
    """
    prev = _prev_source_collected_until(last_run, field)
    if prev is not None:
        return prev
    return _coerce_day(packet.get("since"))


def _internal_since(last_run: dict[str, Any], global_since: str) -> str:
    """内部库可以停在更早的重试日或上次实际采集日。显式更早的全局 since 仍然取更早的那天。"""
    candidates = [global_since]
    if isinstance(last_run, dict):
        retry = _coerce_retry_since(last_run.get("internal_retry_since"))
        if retry is not None:
            candidates.append(retry)
        last_collected = _coerce_day(last_run.get("internal_last_collected_until"))
        if last_collected is not None:
            candidates.append(last_collected)
    return min(candidates)


def _internal_needs_retry(internal: dict[str, Any]) -> bool:
    if internal.get("pending"):
        return True
    status = internal.get("status") or {}
    if not isinstance(status, dict):
        return bool(internal.get("pending"))
    for row in status.values():
        if isinstance(row, dict) and (row.get("ok") is False or row.get("incomplete")):
            return True
    return False


def _day_shift(day_str: str, delta_days: int) -> str:
    return (date.fromisoformat(day_str) + timedelta(days=delta_days)).isoformat()


def _ae_overlap_start(covered_until: str) -> str:
    day = date.fromisoformat(str(covered_until).strip()[:10])
    return (day - timedelta(days=AE_LOOKBACK_DAYS)).isoformat()


def _completed_ae_start(default_start: str, covered_until: Any) -> str:
    """下一轮起点不晚于上次覆盖日，并保留 AE_LOOKBACK_DAYS 天回看重叠。"""
    if not isinstance(covered_until, str) or not covered_until.strip():
        return default_start
    try:
        covered_day = date.fromisoformat(covered_until.strip()[:10]).isoformat()
        overlap = _ae_overlap_start(covered_until)
    except ValueError:
        return default_start
    return min(default_start, overlap, covered_day)


def _ae_state_for_last_run(state: dict[str, Any]) -> dict[str, Any]:
    """写入 last_run.json 的 AE 状态：只保留覆盖日期与状态字段，不写 cursor。"""
    queries = state.get("queries")
    if not isinstance(queries, list):
        return {"queries": []}
    cleaned: list[dict[str, Any]] = []
    for row in queries:
        if not isinstance(row, dict):
            continue
        cleaned.append({
            key: value
            for key, value in row.items()
            if key not in ("cursor", "resumed_cursor")
        })
    return {"queries": cleaned}


def read_document(doc_id: str) -> dict[str, Any]:
    parts: list[str] = []
    offset: Any = 0
    meta: dict[str, Any] = {}
    pages = 0
    error: str | None = None
    more = False
    quota = False
    while pages < MAX_READ_PAGES:
        res = call_bridge_jsonrpc(
            "research",
            {
                "route": "all",
                "operation": "read",
                "document": doc_id,
                "params": {"offset": offset, "max_chars": READ_CHARS},
            },
        )
        pages += 1
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        text, nxt, meta_part = extract_document(data)
        if meta_part and not meta:
            meta = meta_part
        if not res.get("ok") and not text:
            error = bridge_error(res, data)
            quota = any(marker in error for marker in _QUOTA_MARKERS)
            more = False
            break
        parts.append(text)
        more = nxt not in (None, "", offset)
        if not more:
            break
        offset = nxt
    if more:
        error = "read_page_cap"
    incomplete = bool(error) or more
    return {
        "text": "".join(parts),
        "meta": meta,
        "pages": pages,
        "incomplete": incomplete,
        "error": error,
        "quota": quota,
        "ok": not incomplete,
    }


def _safe_doc_name(doc_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", doc_id)[:180]


def _read_state_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectError("seen_unreadable", f"{path.name} 无法读取") from exc
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise CollectError("seen_unreadable", f"{path.name} 必须是字符串列表")
    return data


def _read_state_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectError("state_unreadable", f"{path.name} 无法读取") from exc
    if not isinstance(data, dict):
        raise CollectError("state_unreadable", f"{path.name} 必须是对象")
    return data


def _local_state(workspace: Path, name: str, default: Any) -> Any:
    for relative in (Path("research") / "tracker" / name, Path(name)):
        candidate = workspace / relative
        if not candidate.exists():
            continue
        full = ensure_relative(workspace, relative)
        try:
            return json.loads(full.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CollectError("state_unreadable", f"{name} 无法读取") from exc
    return default


def _keyword_args(route: str, query: str, since: str, until: str, limit: int, cursor: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"media": "text", "search_mode": "keyword"}
    if cursor:
        params["cursor"] = cursor
    return {
        "route": route,
        "operation": "search",
        "query": query,
        "start": since,
        "end": until,
        "limit": limit,
        "params": params,
    }


def _wiki_args(since: str, cursor: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"indexed_since": since, "media": "text"}
    if cursor:
        params["cursor"] = cursor
    return {"route": "company-wiki", "operation": "list", "limit": 100, "params": params}


def _fetch_pages(tool: str, build_args, page_cap: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    ok = True
    error: str | None = None
    incomplete = False
    reason: str | None = None
    warnings: list[str] = []
    last_args: dict[str, Any] = {}
    while pages < page_cap:
        last_args = build_args(cursor)
        res = call_bridge_jsonrpc(tool, last_args)
        pages += 1
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        page_items, page = extract_items_and_page(data)
        _status, page_warnings = _g._extract_route_warnings_and_status(res, data if isinstance(data, dict) else {})
        for item in page_warnings:
            if item not in warnings:
                warnings.append(item)
        if not res.get("ok"):
            if is_no_match(res, data):
                break
            ok = False
            error = bridge_error(res, data)
            if is_cursor_invalid(res, data):
                cursor = None
                reason = f"游标失效，下一轮从同一窗口起点重查：{error}"
            incomplete = True
            break
        items.extend(page_items)
        has_more = bool(page.get("has_more"))
        cursor = page.get("cursor") if has_more else None
        trunc = _known_truncation(res, data)
        if has_more and cursor and pages < page_cap:
            continue
        if has_more and not cursor:
            incomplete = True
            reason = trunc or "上游 has_more 为真但没有下一页 cursor"
            cursor = None
        elif has_more and pages >= page_cap:
            incomplete = True
            reason = f"已达翻页上限 {page_cap}，仍有下一页"
        elif trunc:
            incomplete = True
            reason = trunc
            cursor = None
        break
    status = "error" if not ok else "incomplete" if incomplete else "ok"
    return {
        "items": items,
        "request_ok": ok,
        "status": status,
        "incomplete": incomplete,
        "error": error or reason,
        "warnings": warnings,
        "pages": pages,
        "cursor": cursor,
        "request": {key: value for key, value in last_args.items() if key != "params"} | {
            "params": last_args.get("params")
        },
    }


def _earnings_calendar_enabled(cfg: dict[str, Any]) -> bool:
    sources = cfg.get("sources")
    if isinstance(sources, dict):
        return sources.get("earnings_calendar") is True
    return False




def _researchdata_bridge_capability_enabled(cfg: dict[str, Any] | None = None) -> bool:
    """Whether the deployment's ``researchdata_bridge`` capability is on for this skill.

    Read exclusively from the backend-assembled config
    (``_dalton_cap_researchdata_bridge``), never from environment variables.
    """
    return not (isinstance(cfg, dict) and cfg.get("_dalton_cap_researchdata_bridge") is False)


def _alphaengine_capability_enabled(cfg: dict[str, Any] | None = None) -> bool:
    """Whether the deployment's ``alphaengine`` capability is on for this skill.

    The backend assembles the skill's config before it reaches the sandbox
    (``content_assembly.assemble_json_text``) and writes
    ``_dalton_cap_alphaengine=false`` there when the capability is off; that is
    the one channel the sandbox has, so the decision stays in
    ``src/dalton/capabilities.py`` and is not re-derived here.
    """
    if not _researchdata_bridge_capability_enabled(cfg):
        return False
    return not (isinstance(cfg, dict) and cfg.get("_dalton_cap_alphaengine") is False)


def _source_enabled(cfg: dict[str, Any], name: str) -> bool:
    if name == "earnings_calendar":
        return _earnings_calendar_enabled(cfg)
    sources = cfg.get("sources")
    if isinstance(sources, dict):
        if name not in sources:
            return True
        return bool(sources.get(name))
    return True


def _effective_internal_routes(cfg: dict[str, Any]) -> list[str]:
    sources = cfg.get("sources")
    if isinstance(sources, dict) and isinstance(sources.get("internal_routes"), list):
        return [str(r) for r in sources["internal_routes"]]
    return []


def _has_capability_skipped_internal_routes(cfg: dict[str, Any] | None = None) -> bool:
    """本轮配置中是否有内部路线因能力关闭被装配层过滤跳过。"""
    if not isinstance(cfg, dict):
        return False
    raw = cfg.get("_dalton_cap_disabled_internal_routes")
    return isinstance(raw, list) and bool(raw)


def _effective_sec_forms(cfg: dict[str, Any]) -> list[str]:
    sources = cfg.get("sources")
    if isinstance(sources, dict) and isinstance(sources.get("sec_forms"), list):
        return [str(f) for f in sources["sec_forms"]]
    return []


def _research_query(cfg: dict[str, Any]) -> str:
    raw = cfg.get("research_keywords")
    if isinstance(raw, list):
        terms = [str(term).strip() for term in raw if str(term).strip()]
        if terms:
            return " ".join(terms)
    elif isinstance(raw, str) and raw.strip():
        return raw.strip()
    return " ".join(str(term) for term in cfg.get("alias_terms") or [])


def _ticker_term_regex(ticker: str) -> str:
    clean = ticker.strip()
    upper_esc = re.escape(clean.upper())
    prefix = r"(?<![A-Za-z0-9_])" if (clean and (clean[0].isalnum() or clean[0] == "_")) else ""
    suffix = r"(?![A-Za-z0-9_])" if (clean and (clean[-1].isalnum() or clean[-1] == "_")) else ""
    return rf"{prefix}(?:\$(?i:{upper_esc})|{upper_esc}){suffix}"


def _alias_term_regex(term: str, actual_ticker: str = "") -> str:
    clean = term.strip()
    if actual_ticker and clean.upper() == actual_ticker.strip().upper():
        return _ticker_term_regex(actual_ticker)
    if clean.isascii() and any(ch.isalnum() for ch in clean):
        letters = "".join(ch for ch in clean if ch.isalpha())
        if clean.isalnum() and len(clean) <= 4 and letters:
            if clean == clean.upper() or clean == clean.lower():
                target = re.escape(clean.upper())
                return rf"(?<![A-Za-z0-9_])(?:\$(?i:{target})|{target})(?![A-Za-z0-9_])"
            return rf"(?<![A-Za-z0-9_])(?:\$(?i:{re.escape(clean)})|{re.escape(clean)})(?![A-Za-z0-9_])"
        prefix = r"(?<![A-Za-z0-9_])" if (clean[0].isalnum() or clean[0] == "_") else ""
        suffix = r"(?![A-Za-z0-9_])" if (clean[-1].isalnum() or clean[-1] == "_") else ""
        return rf"(?i:{prefix}{re.escape(clean)}{suffix})"
    return rf"(?i:{re.escape(clean)})"


def _aliases_pattern(cfg: dict[str, Any]) -> re.Pattern[str]:
    raw = cfg.get("aliases_regex")
    if isinstance(raw, str) and raw.strip():
        return re.compile(raw)
    actual_ticker = str(cfg.get("ticker") or TICKER).strip()
    patterns: list[str] = []
    if actual_ticker:
        patterns.append(_ticker_term_regex(actual_ticker))
    terms: list[str] = []
    for key in ("research_keywords", "alias_terms"):
        val = cfg.get(key)
        if isinstance(val, list):
            terms.extend(str(item).strip() for item in val if str(item).strip())
        elif isinstance(val, str) and val.strip():
            terms.extend(val.strip().split())
    # Only a term that is the ticker itself (any case) is folded into the strict ticker
    # pattern above; other aliases dedupe by their exact spelling.
    ticker_upper = actual_ticker.upper() if actual_ticker else ""
    for term in dict.fromkeys(terms):
        if ticker_upper and term.upper() == ticker_upper:
            continue
        patterns.append(_alias_term_regex(term, actual_ticker=actual_ticker))
    return re.compile("|".join(patterns))


def collect_internal(
    cfg: dict[str, Any],
    since: str,
    until: str,
    seen: set[str],
    workspace: Path,
    run_dir: Path,
) -> dict[str, Any]:
    if not _researchdata_bridge_capability_enabled(cfg):
        return {"status": {}, "new": {}, "irrelevant": {}, "pending": {}, "withheld": {}}
    pat = _aliases_pattern(cfg)
    excluded = {str(item) for item in cfg.get("excluded_documents") or []}
    query = _research_query(cfg)
    limit = int(cfg.get("internal_limit") or 60)
    cap = int(cfg.get("internal_read_cap_per_route") or 12)
    min_hits = int(cfg.get("wiki_min_alias_hits") or 2)
    found: dict[str, dict[str, Any]] = {}
    status: dict[str, Any] = {}
    stop_reads = False

    for route in _effective_internal_routes(cfg):
        route = str(route)
        if route == "company-wiki":
            fetched = _fetch_pages("research", lambda cursor: _wiki_args(since, cursor), MAX_INTERNAL_PAGES)
            operation = "list"
        elif route in KEYWORD_ROUTES:
            fetched = _fetch_pages(
                "research",
                lambda cursor, route=route: _keyword_args(route, query, since, until, limit, cursor),
                MAX_INTERNAL_PAGES,
            )
            operation = "keyword-search"
        else:
            status[route] = {"operation": "unsupported", "ok": False, "status": "error", "error": "未知内部路线"}
            continue
        returned = len(fetched["items"])
        status[route] = {
            "operation": operation,
            "ok": bool(fetched["request_ok"]),
            "status": fetched["status"],
            "returned": returned,
            "pages": fetched["pages"],
            "incomplete": fetched["incomplete"],
            "error": fetched["error"],
            "warnings": fetched.get("warnings") or [],
            "limit": 100 if route == "company-wiki" else limit,
            "read_cap": cap,
            "request": fetched["request"],
            "pending_reads": [],
        }
        if not fetched["request_ok"]:
            continue
        for row in fetched["items"]:
            doc_id = str(row.get("document_id") or "")
            if not doc_id or doc_id.startswith("image:") or doc_id in seen or doc_id in found:
                continue
            location = row.get("location")
            if doc_id in excluded or (location is not None and str(location) in excluded):
                continue
            owned = str(row.get("company") or "").upper() == TICKER
            found[doc_id] = {
                "route": route,
                "title": row.get("title"),
                "date": row.get("date"),
                "indexed_at": row.get("indexed_at"),
                "from": row.get("original_source"),
                "location": location,
                "score": row.get("keyword_hits") or 0,
                "owned": owned,
                "queries": [f"indexed_since {since}"] if route == "company-wiki" else ["alias-keyword"],
            }

    new: dict[str, Any] = {}
    irrelevant: dict[str, Any] = {}
    pending: dict[str, Any] = {}
    withheld: dict[str, Any] = {}
    counts: dict[str, int] = {}
    ordered = sorted(
        found.items(),
        key=lambda kv: (1 if kv[1].get("owned") else 0, kv[1].get("score") or 0, kv[1].get("date") or ""),
        reverse=True,
    )
    for doc_id, info in ordered:
        route = str(info["route"])
        counts[route] = counts.get(route, 0) + 1
        if counts[route] > cap:
            info["excerpt"] = None
            info["skipped"] = "超过单来源全文读取上限，只记标题"
            info["analyzed"] = False
            info["incomplete"] = True
            info["pending_reason"] = "read_cap"
            pending[doc_id] = info
            status[route]["pending_reads"].append(doc_id)
            status[route]["incomplete"] = True
            status[route]["status"] = "incomplete"
            if not status[route].get("error"):
                status[route]["error"] = "超过单来源全文读取上限，未读文档留在待处理"
            continue
        if stop_reads:
            info["analyzed"] = False
            info["incomplete"] = True
            info["pending_reason"] = "quota_stop"
            pending[doc_id] = info
            status[route]["pending_reads"].append(doc_id)
            status[route]["incomplete"] = True
            status[route]["status"] = "incomplete"
            continue
        read = read_document(doc_id)
        info["chars"] = len(read["text"])
        info["read_pages"] = read["pages"]
        if not read["ok"]:
            info["read_error"] = read["error"]
            info["incomplete"] = True
            info["pending_reason"] = "read_truncated" if read["error"] == "read_page_cap" else "read_failed"
            pending[doc_id] = info
            status[route]["pending_reads"].append(doc_id)
            status[route]["incomplete"] = True
            status[route]["status"] = "incomplete"
            status[route]["error"] = status[route].get("error") or read["error"]
            if read["quota"]:
                stop_reads = True
            continue
        text = read["text"]
        hits = len(pat.findall(text))
        info["alias_hits"] = hits
        excerpt_text = excerpt(text, pat) if text else ""
        info["excerpt"] = excerpt_text
        info["excerpt_chars"] = len(excerpt_text)
        info["analyzed"] = True
        if route == "company-wiki" and not info.get("owned") and hits < min_hits:
            if hits == 0:
                irrelevant[doc_id] = info
            else:
                info["pending_reason"] = "wiki_below_min_alias_hits"
                withheld[doc_id] = info
            continue
        if route != "company-wiki" and not pat.search(text):
            irrelevant[doc_id] = info
            continue
        relative = Path("research") / "tracker" / run_dir.name / "excerpts" / f"{_safe_doc_name(doc_id)}.txt"
        target = ensure_relative(workspace, relative)
        header = f"{doc_id} | {info.get('date')} | {info.get('title')} | {info.get('from')}\n\n"
        atomic_write_text(workspace, target, header + excerpt_text)
        info["excerpt_file"] = relative.as_posix()
        new[doc_id] = info
    return {
        "status": status,
        "new": new,
        "irrelevant": irrelevant,
        "pending": pending,
        "withheld": withheld,
    }


def _has_ae_state(last_run: dict[str, Any]) -> bool:
    state = last_run.get(ae_state_key()) if isinstance(last_run, dict) else None
    return isinstance(state, dict) and isinstance(state.get("queries"), list) and bool(state["queries"])


def _ae_prev_map(last_run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    state = last_run.get(ae_state_key()) if isinstance(last_run, dict) else None
    queries = state.get("queries") if isinstance(state, dict) else None
    if not isinstance(queries, list):
        return {}
    return {
        str(item.get("query")): item
        for item in queries
        if isinstance(item, dict) and isinstance(item.get("query"), str)
    }


def _legacy_ae_map(last_run: dict[str, Any], queries: list[str]) -> dict[str, dict[str, Any]]:
    """没有本 ticker 的续采状态时，用导入的覆盖日和未完成窗口初始化每一条查询。

    MSFT 的键名是 msft_ae_state。旧 cursor 一律忽略，窗口终点不再锁死，只保留已覆盖边界。
    """
    if not isinstance(last_run, dict) or _has_ae_state(last_run):
        return {}
    covered = _coerce_day(last_run.get("ae_covered_until"))
    resume = last_run.get("ae_resume") if isinstance(last_run.get("ae_resume"), dict) else None
    window = resume.get("window") if isinstance(resume, dict) and isinstance(resume.get("window"), dict) else {}
    start = _coerce_day(window.get("start") or window.get("since"))
    end = _coerce_day(window.get("end") or window.get("until"))
    found: dict[str, dict[str, Any]] = {}
    for query in queries:
        if start and end and start <= end:
            found[query] = {
                "query": query,
                "complete": False,
                "ok": True,
                "covered_until": covered if covered and covered < end else None,
                "window": {"start": start, "end": end},
            }
        elif covered:
            found[query] = {
                "query": query,
                "complete": True,
                "ok": True,
                "covered_until": covered,
            }
    return found


def _iter_ae_subwindows(pending_start: str, until: str) -> list[tuple[str, str]]:
    """按自然日从旧到新生成子窗口；首个子窗口为首个未覆盖日含 AE_LOOKBACK_DAYS 天回看，后续为单日。"""
    p_day = date.fromisoformat(pending_start)
    end_day = date.fromisoformat(until)
    if p_day > end_day:
        return []
    first_start = (p_day - timedelta(days=AE_LOOKBACK_DAYS)).isoformat()
    out: list[tuple[str, str]] = [(first_start, pending_start)]
    span = (end_day - p_day).days
    for offset in range(1, span + 1):
        d = (p_day + timedelta(days=offset)).isoformat()
        out.append((d, d))
    return out


def _normalize_ae_search_in(raw: Any) -> str | None:
    val = str(raw or "").strip()
    return val or None


def _parse_ae_queries(cfg: dict[str, Any]) -> list[tuple[str, str | None]]:
    """解析固定查询列表，兼容纯字符串与 ``{"query": ..., "search_in": ...}`` 对象。

    状态始终以查询词文本为键；配置中出现重复查询词时直接报配置错误，避免同一查询词
    重复计算请求预算并覆盖彼此状态。``search_in`` 非空时（例如 ``"title"``）透传到
    ``params.search_in``。
    """
    max_searches = int(cfg.get("ae_max_searches") or 2)
    fallback_search_in = cfg.get("ae_search_in")
    out: list[tuple[str, str | None]] = []
    seen_queries: set[str] = set()
    for raw in cfg.get("ae_queries") or []:
        if isinstance(raw, dict):
            query = str(raw.get("query") or "").strip()
            if not query:
                continue
            search_in = _normalize_ae_search_in(raw.get("search_in"))
        else:
            query = str(raw).strip()
            if not query:
                continue
            if isinstance(fallback_search_in, dict):
                search_in = _normalize_ae_search_in(fallback_search_in.get(query))
            else:
                search_in = _normalize_ae_search_in(fallback_search_in)
        if query in seen_queries:
            raise CollectError("duplicate_ae_query", f"ae_queries 包含重复查询词：{query}")
        seen_queries.add(query)
        out.append((query, search_in))
    return out[:max_searches]


def _format_ae_backlog_note(
    skip_since: str,
    skip_until: str,
    max_backlog_days: int = AE_MAX_BACKLOG_DAYS,
) -> str:
    return (
        f"AlphaEngine 积压超过 {max_backlog_days} 天，"
        f"已跳过 {skip_since} 至 {skip_until}，"
        "未逐日采集；需要时可按标题检索补看"
    )


def _scan_ae_pages(
    query: str,
    start: str,
    end: str,
    *,
    budget: int,
    max_requests: int | None = None,
    early_exit_total_limit: int | None = None,
    search_in: str | None = None,
) -> dict[str, Any]:
    """单窗口内连续翻页，cursor 仅在本轮内使用。

    第一页（含游标失效重扫后的首页）带 params.fresh=True。同轮遇到游标失效
    （e_cursor_expired / e_cursor_filter_mismatch）时，只要本查询本轮还有剩余请求次数
    （``max_requests``），第一次游标失效立即从第一页重扫；重扫后再次失效或本轮请求次数用尽
    则保留已取条目并按本轮未扫完返回，不把整段记为失败。响应带 ``stale: true`` 或
    ``cursor_unavailable`` 时保留已取条目入包，但不视为扫完。
    """
    limit_pages = max(1, int(budget))
    request_cap = max(limit_pages, int(max_requests)) if max_requests is not None else limit_pages
    window_limit = min(limit_pages, request_cap)
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    ok = True
    error: str | None = None
    incomplete = False
    incomplete_reason: str | None = None
    has_more = False
    cursor_restarted = False
    cursor_error: str | None = None
    cursor_exhausted = False
    stale = False
    stale_reason: str | None = None
    early_exit = False
    hit_page_cap = False
    budget_exhausted = False
    partial_hit = False
    upstream_cursor_hit = False
    last_arguments: dict[str, Any] = {}
    total_upstream: Any = None
    norm_search_in = _normalize_ae_search_in(search_in)

    while pages < window_limit:
        params: dict[str, Any] = {"sort": "relevance"}
        if norm_search_in:
            params["search_in"] = norm_search_in
        sent_cursor = cursor
        if sent_cursor:
            params["cursor"] = sent_cursor
        else:
            params["fresh"] = True
        arguments = {
            "route": "alphaengine",
            "operation": "search_library",
            "query": query,
            "start": start,
            "end": end,
            "limit": AE_LIMIT,
            "params": params,
        }
        last_arguments = arguments
        res = call_bridge_jsonrpc("research", arguments)
        pages += 1
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        page_items, page = extract_items_and_page(data)
        page_ok = bool(res.get("ok"))
        page_partial = _is_partial_status(res, data)
        page_partial_error: str | None = None
        if page.get("total_upstream") is not None:
            total_upstream = page.get("total_upstream")
        no_match = is_no_match(res, data)
        if no_match and (page_partial or page_items):
            # 带"没有匹配"标记但声明了 partial 或已返回条目时，保留条目并按部分响应进入按日续采。
            page_partial = True
        elif not page_ok and no_match:
            page_ok = True
            page_items = []
            page = {**page, "has_more": False, "cursor": None}

        if not page_ok:
            if sent_cursor and is_cursor_invalid(res, data):
                detail = bridge_error(res, data)
                cursor_error = detail
                if not cursor_restarted and pages < request_cap:
                    cursor_restarted = True
                    cursor = None
                    has_more = False
                    window_limit = min(request_cap, pages + limit_pages)
                    continue
                ok = True
                incomplete = True
                has_more = True
                cursor = None
                cursor_exhausted = True
                budget_exhausted = False
                if cursor_restarted:
                    incomplete_reason = f"游标失效重扫后仍未扫完（{detail}）"
                else:
                    incomplete_reason = f"游标失效且本轮查询请求次数已用完（{detail}）"
                break
            if page_partial:
                # 同时带 status=partial 和非空 error 时，按部分响应处理：保留该页已返回条目，进入按日续采。
                page_ok = True
                page_partial_error = bridge_error(res, data)
            elif is_upstream_cursor_error(res, data):
                detail = bridge_error(res, data)
                ok = True
                incomplete = True
                incomplete_reason = f"上游游标异常（{detail}）"
                upstream_cursor_hit = True
                has_more = False
                cursor = None
                budget_exhausted = False
                break
            else:
                ok = False
                error = bridge_error(res, data)
                incomplete = True
                has_more = False
                cursor = None
                budget_exhausted = False
                break

        stale_flag = _g._extract_stale_or_cursor_unavailable(res, data)
        if stale_flag is not None:
            tagged_items = [
                {**it, _g._AE_STALE_KEY: True} if isinstance(it, dict) else it
                for it in page_items
            ]
            items.extend(tagged_items)
            ok = False
            stale = True
            stale_reason = stale_flag
            error = "AlphaEngine 暂时不可用，返回的是缓存结果"
            incomplete = True
            incomplete_reason = "AlphaEngine 暂时不可用，返回的是缓存结果"
            has_more = bool(page.get("has_more"))
            cursor = None
            budget_exhausted = False
            break

        items.extend(page_items)
        page_has_more = bool(page.get("has_more"))
        next_cursor = page.get("cursor") if page_has_more else None
        trunc = _known_truncation(res, data)
        if page_partial:
            err_detail = page_partial_error
            if err_detail is None:
                raw_err = data.get("error") if isinstance(data, dict) else None
                if not raw_err:
                    raw_err = res.get("error")
                if isinstance(raw_err, str) and raw_err.strip():
                    err_detail = raw_err.strip()[:500]
            if trunc and err_detail and err_detail not in trunc:
                trunc = f"{trunc}（{err_detail}）"
            partial_hit = True
            incomplete = True
            incomplete_reason = trunc or (
                f"未完成: 结果被截断或覆盖不完整（{err_detail}）"
                if err_detail
                else "未完成: 结果被截断或覆盖不完整（status=partial）"
            )
            has_more = bool(page_has_more)
            cursor = None
            break

        if page_has_more and not next_cursor:
            incomplete = True
            incomplete_reason = trunc or "has_more 为真但没有 cursor"
            has_more = False
            cursor = None
            break
        if trunc:
            incomplete = True
            incomplete_reason = trunc
            has_more = bool(page_has_more)
            cursor = None
            break
        if not page_has_more:
            has_more = False
            cursor = None
            break

        has_more = True
        if (
            early_exit_total_limit is not None
            and isinstance(total_upstream, (int, float))
            and not isinstance(total_upstream, bool)
            and total_upstream > early_exit_total_limit
        ):
            early_exit = True
            incomplete = True
            incomplete_reason = f"{start}–{end} 窗口共 {int(total_upstream)} 条，超出单轮翻页上限"
            cursor = None
            break

        if pages >= window_limit:
            hit_page_cap = True
            incomplete = True
            cursor = None
            budget_exhausted = cursor_error is None
            if cursor_restarted:
                incomplete_reason = f"游标失效重扫后仍未扫完（{cursor_error}）"
            elif start < end:
                incomplete_reason = f"{start}–{end} 窗口超过 {limit_pages * AE_LIMIT} 条（达到翻页上限，{limit_pages} 页）"
            else:
                incomplete_reason = f"{end} 当天达到翻页上限（{limit_pages} 页）"
            break

        cursor = str(next_cursor)

    return {
        "items": items,
        "ok": ok,
        "completed": bool(ok and not incomplete),
        "incomplete": incomplete,
        "error": error,
        "incomplete_reason": incomplete_reason,
        "has_more": has_more,
        "pages": pages,
        "cursor_restarted": cursor_restarted,
        "cursor_error": cursor_error,
        "cursor_exhausted": cursor_exhausted,
        "stale": stale,
        "stale_reason": stale_reason,
        "early_exit": early_exit,
        "hit_page_cap": hit_page_cap,
        "budget_exhausted": budget_exhausted,
        "partial": partial_hit,
        "upstream_cursor_error": upstream_cursor_hit,
        "window": (start, end),
        "request": last_arguments,
    }


def _ae_item_day(item: Any) -> str | None:
    """只从发布日期字段（publish_date / publish_time）读取文档日期；rank_date、date 不参与。"""
    if not isinstance(item, dict):
        return None
    for key in ("publish_date", "publish_time"):
        day = _coerce_day(item.get(key))
        if day:
            return day
    return None


def _format_ae_incomplete_error(
    *,
    start: str,
    end: str,
    carried_covered: str | None,
    covered: str | None,
    ok: bool,
    stale: bool,
    partial_days: list[tuple[str, int]],
    raw_error: str | None,
    reason_base: str | None,
    cursor_restarted: bool,
    cursor_error: str | None,
    cursor_exhausted: bool,
    hit_page_cap: bool,
    hit_page_cap_window: tuple[str, str] | None,
    overflowed_window: tuple[str, str] | None,
    round_cap_hit: bool,
    round_cap_resume_day: str | None,
    page_limit: int,
    backlog_note: str | None = None,
) -> str:
    advanced = bool(covered and (not carried_covered or covered > carried_covered))
    if covered and covered < end:
        uncovered_note = f"未覆盖日期：{_day_shift(covered, 1)} 至 {end}"
    elif covered and covered >= end:
        uncovered_note = f"未覆盖回看窗口：{start} 至 {end}"
    else:
        uncovered_note = f"未覆盖日期：{start} 至 {end}"

    cap_items = page_limit * AE_LIMIT
    partial_prefix = (
        "；".join(f"{p_day} 当天超过翻页上限，只取了前 {p_count} 条" for p_day, p_count in partial_days)
        if partial_days
        else None
    )

    stop_prefix: str | None = None
    if stale:
        stop_prefix = "AlphaEngine 暂时不可用，返回的是缓存结果"
    elif not ok:
        stop_prefix = raw_error or "unknown_error"
    elif cursor_exhausted or cursor_error:
        if cursor_restarted:
            stop_prefix = f"游标失效重扫后仍未扫完（{cursor_error}）"
        else:
            stop_prefix = f"游标失效且本轮查询请求次数已用完（{cursor_error}）"
    elif round_cap_hit:
        resume_day = round_cap_resume_day or (
            _day_shift(covered, 1) if covered and covered < end else start
        )
        if overflowed_window is not None:
            ow_s, ow_e = overflowed_window
            stop_prefix = f"{ow_s}–{ow_e} 窗口超过 {cap_items} 条，本轮请求次数已用完，{resume_day} 起留待下一轮"
        else:
            stop_prefix = f"本轮请求次数已用完，{resume_day} 起留待下一轮"
    elif hit_page_cap:
        w_s, w_e = hit_page_cap_window or (start, end)
        if w_s < w_e:
            stop_prefix = f"{w_s}–{w_e} 窗口超过 {cap_items} 条（达到翻页上限，{page_limit} 页）本轮未扫完"
        else:
            stop_prefix = f"{w_e} 当天达到翻页上限（{page_limit} 页）本轮未扫完"
    elif not partial_prefix:
        stop_prefix = reason_base or "本轮未扫完"
    elif covered and covered < end and reason_base:
        stop_prefix = reason_base

    if partial_prefix and stop_prefix:
        body = f"{partial_prefix}；{stop_prefix}"
    else:
        body = partial_prefix or stop_prefix or "本轮未扫完"

    if not ok and not stale and not advanced and not partial_prefix:
        msg = body
    elif partial_prefix and not stop_prefix:
        if covered and covered < end:
            msg = f"{body}，覆盖边界已推进至 {covered}，{uncovered_note}"
        else:
            msg = f"{body}，覆盖边界已推进至 {covered}"
    elif advanced:
        msg = f"{body}，覆盖边界已推进至 {covered}，{uncovered_note}"
    else:
        msg = f"{body}，{uncovered_note}"

    if backlog_note:
        return f"{backlog_note}；{msg}"
    return msg


def collect_alphaengine(
    cfg: dict[str, Any],
    since: str,
    until: str,
    seen: set[str],
    last_run: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """每条查询在本轮内连续翻页（单窗口上限 MAX_AE_PAGES，单查询本轮总请求上限 MAX_AE_REQUESTS_PER_QUERY），
    cursor 不跨轮保存也不从旧状态读取。

    跨轮仅按 covered_until 推进窗口：待采起点距 until 超过 AE_MAX_BACKLOG_DAYS 时跳到
    until - AE_LOOKBACK_DAYS 并在备注写明跳过段；整段扫不完进入按日切分，首个含回看的
    [P-3, P] 子窗口若用完翻页预算则改为单日 [P, P] 重扫；单日窗口用完翻页预算时把边界推进到
    P 并继续扫下一天，直到全部完成或用完本查询本轮请求预算。
    """
    if not _source_enabled(cfg, "alphaengine") or not _alphaengine_capability_enabled(cfg):
        return {"status": [], "new": {}}, {"queries": []}

    pat = _aliases_pattern(cfg)
    names = {str(name) for name in cfg.get("ae_company_names") or []}
    query_specs = _parse_ae_queries(cfg)
    queries = [q for q, _ in query_specs]
    prev_map = _ae_prev_map(last_run) or _legacy_ae_map(last_run, queries)
    found: dict[str, Any] = {}
    statuses: list[dict[str, Any]] = []
    state_queries: list[dict[str, Any]] = []
    default_start = _day_shift(since, -AE_LOOKBACK_DAYS)
    page_limit = max(1, int(MAX_AE_PAGES))
    round_cap = max(1, int(MAX_AE_REQUESTS_PER_QUERY))
    single_day_cap = min(page_limit, round_cap)
    max_backlog_days = max(1, int(AE_MAX_BACKLOG_DAYS))

    for query, search_in in query_specs:
        prev = prev_map.get(query)
        carried_covered = _coerce_day(prev.get("covered_until")) if isinstance(prev, dict) else None
        if carried_covered:
            start = _completed_ae_start(default_start, carried_covered)
        elif isinstance(prev, dict) and isinstance(prev.get("window"), dict):
            prev_start = _coerce_day(prev["window"].get("start"))
            start = min(default_start, prev_start) if prev_start else default_start
        else:
            start = default_start
        end = until
        if start > end:
            start = end

        if carried_covered:
            same_day_rerun = carried_covered >= end
            pending_start = end if same_day_rerun else _day_shift(carried_covered, 1)
        else:
            same_day_rerun = False
            pending_start = min(end, _day_shift(start, AE_LOOKBACK_DAYS))

        skipped_backlog: dict[str, str] | None = None
        if not same_day_rerun and pending_start < end:
            backlog_span = (date.fromisoformat(end) - date.fromisoformat(pending_start)).days
            if backlog_span > max_backlog_days:
                orig_pending_start = pending_start
                jumped_start = _day_shift(end, -AE_LOOKBACK_DAYS)
                if jumped_start > orig_pending_start:
                    pending_start = jumped_start
                    skipped_backlog = {
                        "since": orig_pending_start,
                        "until": _day_shift(pending_start, -1),
                    }
                    start = _day_shift(pending_start, -AE_LOOKBACK_DAYS)
                    if start > end:
                        start = end

        multi_day = (not same_day_rerun) and (pending_start < end)
        tight_budget = (not same_day_rerun) and (round_cap <= 2)
        need_lookback_and_single = page_limit + single_day_cap
        single_day_skip_lookback = (
            (not same_day_rerun)
            and (not multi_day)
            and (round_cap < need_lookback_and_single)
        )
        skip_lookback = tight_budget or single_day_skip_lookback
        if not same_day_rerun and not multi_day:
            # 单个待采日时，仅当本轮预算足以兼顾「回看窗口 + 单日重扫」才带回看，
            # 否则跳过回看直接扫 [待采日, 待采日]，确保只有单日扫描能推进或记 partial_days。
            start = pending_start if skip_lookback else _day_shift(pending_start, -AE_LOOKBACK_DAYS)
            if start > end:
                start = end
        elif tight_budget:
            start = pending_start

        raw_items: list[dict[str, Any]] = []
        scans: list[dict[str, Any]] = []
        pages_total = 0
        covered: str | None = carried_covered
        partial_days: list[tuple[str, int]] = []
        overflowed_window: tuple[str, str] | None = None
        round_cap_hit = False
        round_cap_resume_day: str | None = None
        full_scan: dict[str, Any] | None = None

        if not tight_budget:
            if multi_day:
                step1_budget = max(1, min(page_limit - 1, round_cap - 1))
                step1_max_requests = max(1, round_cap - 1)
                early_limit: int | None = step1_budget * AE_LIMIT
            elif not same_day_rerun and start < end:
                # 单个待采日且带回看：保留至少 single_day_cap 次给单日重扫 [P, P]
                step1_budget = page_limit
                step1_max_requests = max(page_limit, round_cap - single_day_cap)
                early_limit = None
            else:
                step1_budget = single_day_cap
                step1_max_requests = round_cap
                early_limit = None
            full_scan = _scan_ae_pages(
                query,
                start,
                end,
                budget=step1_budget,
                max_requests=step1_max_requests,
                early_exit_total_limit=early_limit,
                search_in=search_in,
            )
            raw_items.extend(full_scan["items"])
            scans.append(full_scan)
            pages_total += int(full_scan["pages"])

        def _rescan_single_day(target_day: str, outer_window: tuple[str, str] | None) -> bool:
            """对 [target_day, target_day] 做单日重扫；返回 True 表示该日已推进且可继续后续日期。"""
            nonlocal pages_total, covered, round_cap_hit, round_cap_resume_day, overflowed_window
            remaining_round = round_cap - pages_total
            if remaining_round <= 0:
                round_cap_hit = True
                round_cap_resume_day = target_day
                if outer_window is not None:
                    overflowed_window = outer_window
                return False
            day_budget = min(page_limit, remaining_round)
            s_scan = _scan_ae_pages(
                query,
                target_day,
                target_day,
                budget=day_budget,
                max_requests=remaining_round,
                search_in=search_in,
            )
            raw_items.extend(s_scan["items"])
            scans.append(s_scan)
            pages_total += int(s_scan["pages"])
            if not s_scan["ok"] or s_scan["stale"]:
                return False
            if s_scan["completed"]:
                covered = max(covered, target_day) if covered else target_day
                return True
            if s_scan["budget_exhausted"] and (day_budget >= single_day_cap or covered == carried_covered):
                deduped_day = _g._dedupe_ae_items(s_scan["items"])
                covered = max(covered, target_day) if covered else target_day
                partial_days.append((target_day, len(deduped_day)))
                return True
            if (
                s_scan["hit_page_cap"]
                and day_budget < single_day_cap
                and not s_scan["cursor_exhausted"]
                and not s_scan.get("cursor_error")
            ):
                round_cap_hit = True
                round_cap_resume_day = target_day
                if outer_window is not None:
                    overflowed_window = outer_window
                return False
            return False

        if full_scan is not None and full_scan["completed"]:
            covered = max(carried_covered, end) if carried_covered else end
        elif full_scan is None or (full_scan["ok"] and not full_scan["stale"]):
            if not tight_budget and full_scan is not None and not same_day_rerun and not multi_day:
                if full_scan["budget_exhausted"]:
                    if start < end:
                        # 多日回看窗口 [P-3, P] 绝不推进 P 或记 partial_days，必须由 [P, P] 单日重扫决定
                        _rescan_single_day(end, (start, end))
                    else:
                        deduped_full = _g._dedupe_ae_items(full_scan["items"])
                        covered = max(carried_covered, end) if carried_covered else end
                        partial_days.append((end, len(deduped_full)))
                elif full_scan["partial"] or full_scan["upstream_cursor_error"]:
                    if pages_total >= round_cap:
                        if not full_scan["cursor_exhausted"] and not full_scan.get("cursor_error"):
                            round_cap_hit = True
                            round_cap_resume_day = end
                    elif not full_scan["cursor_exhausted"] and not full_scan.get("cursor_error"):
                        _rescan_single_day(end, None)
            elif tight_budget or multi_day:
                if pages_total >= round_cap:
                    if not (full_scan and (full_scan["cursor_exhausted"] or full_scan.get("cursor_error"))):
                        round_cap_hit = True
                        round_cap_resume_day = pending_start
                else:
                    round_had_full_cursor_err = bool(
                        full_scan and (full_scan["cursor_exhausted"] or full_scan.get("cursor_error"))
                    )
                    for sub_start, sub_end in _iter_ae_subwindows(pending_start, end):
                        remaining_round = round_cap - pages_total
                        if remaining_round <= 0:
                            round_cap_hit = True
                            round_cap_resume_day = sub_end
                            break
                        use_lookback = (
                            sub_start < sub_end
                            and not tight_budget
                            and remaining_round >= need_lookback_and_single
                        )
                        eff_start = sub_start if use_lookback else sub_end
                        day_budget = page_limit if use_lookback else min(page_limit, remaining_round)
                        scan_max_requests = (
                            max(day_budget, remaining_round - single_day_cap)
                            if use_lookback
                            else remaining_round
                        )
                        d_scan = _scan_ae_pages(
                            query,
                            eff_start,
                            sub_end,
                            budget=day_budget,
                            max_requests=scan_max_requests,
                            search_in=search_in,
                        )
                        raw_items.extend(d_scan["items"])
                        scans.append(d_scan)
                        pages_total += int(d_scan["pages"])
                        if not d_scan["ok"] or d_scan["stale"]:
                            break
                        if d_scan["completed"]:
                            covered = max(covered, sub_end) if covered else sub_end
                            continue
                        if eff_start < sub_end:
                            # 多日回看窗口 [P-3, P] 未扫完时绝不推进 P 或记 partial_days；
                            # 用完单窗口预算后交由 [P, P] 单日重扫决定，其余异常停在 P 之前。
                            if d_scan["budget_exhausted"]:
                                if _rescan_single_day(sub_end, (eff_start, sub_end)):
                                    continue
                                break
                            break
                        if (
                            d_scan["hit_page_cap"]
                            and day_budget < single_day_cap
                            and not d_scan["cursor_exhausted"]
                            and not d_scan.get("cursor_error")
                        ):
                            if d_scan["budget_exhausted"] and covered == carried_covered and not round_had_full_cursor_err:
                                deduped_day = _g._dedupe_ae_items(d_scan["items"])
                                covered = max(covered, sub_end) if covered else sub_end
                                partial_days.append((sub_end, len(deduped_day)))
                                continue
                            round_cap_hit = True
                            round_cap_resume_day = sub_end
                            break
                        if d_scan["budget_exhausted"] and day_budget >= single_day_cap:
                            deduped_day = _g._dedupe_ae_items(d_scan["items"])
                            covered = max(covered, sub_end) if covered else sub_end
                            partial_days.append((sub_end, len(deduped_day)))
                            continue
                        break

        items = _g._dedupe_ae_items(raw_items)
        ok = all(bool(s["ok"]) for s in scans)
        any_stale = any(bool(s["stale"]) for s in scans)
        last_scan = scans[-1]
        last_cursor_restarted = bool(last_scan["cursor_restarted"])
        last_cursor_error = last_scan.get("cursor_error")
        last_cursor_exhausted = bool(last_scan["cursor_exhausted"])
        raw_error = next((s["error"] for s in reversed(scans) if s.get("error")), None)
        reason_base = next(
            (s["incomplete_reason"] for s in reversed(scans) if s.get("incomplete_reason")),
            None,
        )
        advanced = bool(covered and (not carried_covered or covered > carried_covered))
        effective_skipped_backlog = skipped_backlog if (skipped_backlog is not None and advanced) else None
        backlog_note = (
            _format_ae_backlog_note(
                effective_skipped_backlog["since"],
                effective_skipped_backlog["until"],
                max_backlog_days,
            )
            if effective_skipped_backlog is not None
            else None
        )
        complete = bool(
            ok
            and not any_stale
            and not partial_days
            and ((full_scan["completed"] if full_scan is not None else False) if same_day_rerun else covered == end)
        )
        last_was_partial_day = bool(
            partial_days
            and isinstance(last_scan.get("window"), tuple)
            and last_scan["window"][1] == partial_days[-1][0]
            and last_scan.get("budget_exhausted")
        )
        hit_page_cap = (
            (not complete)
            and ok
            and not last_cursor_exhausted
            and not last_cursor_error
            and not round_cap_hit
            and not last_was_partial_day
            and bool(last_scan["hit_page_cap"])
        )
        has_more = (not complete) and ok and (
            bool(last_scan["has_more"]) or hit_page_cap or round_cap_hit or last_cursor_exhausted
        )

        if complete and carried_covered and start > carried_covered and effective_skipped_backlog is None and not skip_lookback:
            complete = False
            covered = carried_covered
            error: str | None = "窗口起点晚于已覆盖日期，未推进覆盖"
        elif complete:
            error = None
        else:
            error = _format_ae_incomplete_error(
                start=start,
                end=end,
                carried_covered=carried_covered,
                covered=covered,
                ok=ok,
                stale=any_stale,
                partial_days=partial_days,
                raw_error=raw_error,
                reason_base=reason_base,
                cursor_restarted=last_cursor_restarted,
                cursor_error=last_cursor_error,
                cursor_exhausted=last_cursor_exhausted,
                hit_page_cap=hit_page_cap,
                hit_page_cap_window=last_scan.get("window"),
                overflowed_window=overflowed_window,
                round_cap_hit=round_cap_hit,
                round_cap_resume_day=round_cap_resume_day,
                page_limit=page_limit,
                backlog_note=backlog_note,
            )

        if isinstance(covered, str) and not covered.strip():
            covered = None

        last_request = last_scan["request"]
        entry: dict[str, Any] = {
            "query": query,
            "window": {"start": start, "end": end},
            "cursor": None,
            "has_more": has_more,
            "complete": complete,
            "covered_until": covered,
            "ok": ok,
            "returned": len(items) if items else 0,
            "error": error,
            "resumed_cursor": None,
        }
        if partial_days:
            entry["partial_days"] = [d for d, _ in partial_days]
        if effective_skipped_backlog is not None:
            entry["skipped_backlog"] = effective_skipped_backlog
            entry["note"] = backlog_note
        state_queries.append(entry)
        status_row: dict[str, Any] = {
            "query": query,
            "ok": ok,
            "returned": entry["returned"],
            "has_more": has_more,
            "incomplete": not complete,
            "cursor": None,
            "window": entry["window"],
            "error": error,
            "request": last_request,
        }
        if partial_days:
            status_row["partial_days"] = [d for d, _ in partial_days]
        if effective_skipped_backlog is not None:
            status_row["skipped_backlog"] = effective_skipped_backlog
            status_row["note"] = backlog_note
        statuses.append(status_row)
        for row in items:
            raw_id = row.get("doc_id")
            if not raw_id:
                continue
            doc_id = str(raw_id)
            if not doc_id.startswith("alphaengine:"):
                doc_id = f"alphaengine:{doc_id}"
            if doc_id in seen or doc_id in found:
                continue
            companies = {str(name) for name in (row.get("companies") or [])}
            title = str(row.get("title") or "")
            if names and not (companies & names) and pat.search(title) is None:
                continue
            attribution = row.get("source_attribution")
            found[doc_id] = {
                "title": row.get("title"),
                "publish_time": row.get("publish_time"),
                "sources": row.get("sources"),
                "document_code": row.get("document_code"),
                "companies": row.get("companies"),
                "snippet": str(row.get("snippet") or "")[:1500],
                "reference_url": row.get("reference_url"),
                "source_attribution": attribution,
                "query": query,
            }
    ae_state = {"queries": state_queries}
    return {"status": statuses, "new": found}, ae_state


def collect_sec(cfg: dict[str, Any], since: str, seen: set[str]) -> dict[str, Any]:
    if not _researchdata_bridge_capability_enabled(cfg):
        return {"status": [], "new": {}}
    floor = (date.fromisoformat(since) - timedelta(days=SEC_LOOKBACK_DAYS)).isoformat()
    found: dict[str, Any] = {}
    statuses: list[dict[str, Any]] = []
    for form in _effective_sec_forms(cfg):
        tool_name, arguments = _g.build_sec_filing_request(TICKER, str(form), limit=SEC_LIMIT)
        res = call_bridge_jsonrpc(tool_name, arguments)
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        rows, _page = extract_items_and_page(data)
        ok = bool(res.get("ok"))
        error = None
        if not ok and is_no_match(res, data):
            ok = True
            rows = []
        elif not ok:
            error = bridge_error(res, data)
        trunc = _known_truncation(res, data) if ok else None
        if trunc:
            error = trunc
        statuses.append({
            "form": form,
            "ok": ok,
            "count": len(rows) if ok else 0,
            "incomplete": bool(trunc),
            "status": "incomplete" if trunc else ("ok" if ok else "error"),
            "error": error,
            "request": arguments,
            "floor": floor,
        })
        if not ok:
            continue
        for row in rows:
            accession = row.get("accession_no") or row.get("accession")
            if not accession:
                continue
            acc = str(accession)
            doc_id = acc if acc.startswith("sec:") else f"sec:{acc}"
            filing_date = str(row.get("filing_date") or "")
            if doc_id in seen or doc_id in found or filing_date < floor:
                continue
            found[doc_id] = {
                "form": row.get("form") or form,
                "filing_date": filing_date,
                "report_date": row.get("report_date"),
                "accession": acc,
            }
    return {"status": statuses, "new": found}


def _parse_x_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.strptime(value.strip(), "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET)


def _in_window(parsed: datetime | None, since: str, until: str) -> bool:
    if parsed is None:
        return True
    day = parsed.date().isoformat()
    return since <= day <= until


def _coerce_day(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError:
        return None


def _x_upstream_until(until: str) -> tuple[str, str]:
    """把美东自然日扩成上游仍能覆盖到当天结束的 UTC 日期。

    时间线按推文的 UTC 日历日做含当日过滤。搜索的 until: 排除该 UTC 日的零点。
    返回 (时间线 until, 搜索 until)。调用方仍用美东日期做最终过滤。
    """
    end = datetime.combine(date.fromisoformat(until) + timedelta(days=1), datetime.min.time(), tzinfo=ET)
    timeline = (end - timedelta(microseconds=1)).astimezone(UTC).date().isoformat()
    end_utc = end.astimezone(UTC)
    boundary = end_utc.date()
    if end_utc > datetime.combine(boundary, datetime.min.time(), tzinfo=UTC):
        boundary += timedelta(days=1)
    return timeline, boundary.isoformat()


def _metric(metrics: Any, key: str) -> int | None:
    if not isinstance(metrics, dict) or key not in metrics:
        return None
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _load_pool(workspace: Path, cfg: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    raw = _local_state(workspace, "x_pool.json", {})
    if not isinstance(raw, dict):
        raise CollectError("state_unreadable", "x_pool.json 必须是对象")
    eligible: list[str] = []
    for handle, info in raw.items():
        if not isinstance(info, dict) or info.get("active", True) is False:
            continue
        tickers = {str(item).upper() for item in (info.get("tickers") or [])}
        if tickers & {TICKER, "ALL"}:  # 本公司 ticker 或 ALL
            eligible.append(str(handle))
    return eligible[: int(cfg.get("x_pool_max") or 25)], raw


def _config_x_accounts(
    cfg: dict[str, Any], pool_handles: list[str], pool_raw: dict[str, Any]
) -> tuple[list[str], set[str]]:
    """Return ``(extra_handles_to_fetch, normalized_config_accounts)`` after pool truncation."""
    all_config_norm: set[str] = set()
    fetched_norm = {
        str(handle).strip().lstrip("@").lower()
        for handle in pool_handles
        if str(handle).strip().lstrip("@")
    }
    inactive_norm = {
        str(handle).strip().lstrip("@").lower()
        for handle, info in pool_raw.items()
        if isinstance(info, dict) and info.get("active", True) is False and str(handle).strip().lstrip("@")
    }
    extra_handles: list[str] = []
    seen_extra: set[str] = set()
    for account in cfg.get("x_accounts") or []:
        handle_str = str(account or "").strip().lstrip("@")
        if not handle_str:
            continue
        low = handle_str.lower()
        all_config_norm.add(low)
        if low in fetched_norm or low in inactive_norm or low in seen_extra:
            continue
        seen_extra.add(low)
        extra_handles.append(handle_str)
    return extra_handles, all_config_norm


def _load_authors(workspace: Path) -> dict[str, Any]:
    raw = _local_state(workspace, "x_authors.json", {})
    if not isinstance(raw, dict):
        raise CollectError("state_unreadable", "x_authors.json 必须是对象")
    return raw


def _x_items(res: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else None
    if isinstance(inner, dict) and isinstance(inner.get("items"), list):
        items = [item for item in inner["items"] if isinstance(item, dict)]
        page = _g._extract_route_page_info(data)
        if not page.get("has_more") and not page.get("cursor"):
            page = _g._extract_route_page_info(inner)
        return items, page
    return extract_items_and_page(data)


def collect_x(
    cfg: dict[str, Any],
    since: str,
    until: str,
    seen: set[str],
    workspace: Path,
    *,
    legacy_verbatim: bool | None = None,
) -> dict[str, Any]:
    if not _researchdata_bridge_capability_enabled(cfg):
        return {
            "status": [],
            "new": {},
            "noise_ids": [],
            "pool_size": 0,
            "queries": [],
            "outside_window": 0,
            "unprocessed_upstream": False,
            "profile_followup": [],
            "failures": [],
            "metrics_note": "",
            "note": "",
        }
    timeline_until, search_until = _x_upstream_until(until)
    raw: dict[str, dict[str, Any]] = {}
    statuses: list[dict[str, Any]] = []
    failures: list[str] = []
    unprocessed = False
    for query in cfg.get("x_queries") or []:
        arguments = {
            "route": "x-timeline",
            "operation": "search",
            "query": query,
            "since": since,
            "until": search_until,
            "count": X_SEARCH_COUNT,
        }
        res = call_bridge_jsonrpc("community", arguments)
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        if not res.get("ok"):
            error = bridge_error(res, data)
            statuses.append({"query": query, "operation": "search", "ok": False, "error": error, "request": arguments})
            failures.append(f"search: {error}")
            continue
        items, page = _x_items(res)
        if page.get("has_more"):
            unprocessed = True
        statuses.append({
            "query": query,
            "operation": "search",
            "ok": True,
            "count": len(items),
            "has_more": bool(page.get("has_more")),
            "request": arguments,
        })
        for item in items:
            post_id = item.get("id")
            if post_id is None:
                continue
            raw.setdefault(str(post_id), {"item": item, "via": "search", "pool_handle": None})

    pool_handles, pool = _load_pool(workspace, cfg)
    config_handles, config_accounts_norm = _config_x_accounts(cfg, pool_handles, pool)
    pool_keys = {str(handle).lower() for handle in pool}
    for via_kind, status_key, handle_list in (
        ("pool", "pool", pool_handles),
        ("config", "config", config_handles),
    ):
        for handle in handle_list:
            arguments = {
                "route": "x-timeline",
                "operation": "tweets",
                "handle": handle,
                "since": since,
                "until": timeline_until,
                "count": X_TIMELINE_COUNT,
            }
            res = call_bridge_jsonrpc("community", arguments)
            data = res.get("data") if isinstance(res.get("data"), dict) else {}
            if not res.get("ok"):
                error = bridge_error(res, data)
                statuses.append({status_key: handle, "operation": "tweets", "ok": False, "error": error, "request": arguments})
                failures.append(f"tweets {handle}: {error}")
                continue
            items, page = _x_items(res)
            if page.get("has_more"):
                unprocessed = True
            kept = 0
            for item in items:
                post_id = item.get("id")
                if post_id is None:
                    continue
                if not _in_window(_parse_x_time(item.get("created_at") or item.get("createdAt")), since, until):
                    continue
                kept += 1
                raw[str(post_id)] = {"item": item, "via": via_kind, "pool_handle": handle}
            statuses.append({
                status_key: handle,
                "operation": "tweets",
                "ok": True,
                "count": len(items),
                "kept": kept,
                "has_more": bool(page.get("has_more")),
                "request": arguments,
            })

    is_legacy = legacy_verbatim if legacy_verbatim is not None else _is_legacy_four_verbatim(TICKER, cfg)
    pat = _aliases_pattern(cfg)
    candidates = []
    outside = 0
    for post_id, packed in raw.items():
        item = packed["item"]
        if item.get("is_repost") or item.get("isRetweet"):
            continue
        if f"x:{post_id}" in seen:
            continue
        parsed = _parse_x_time(item.get("created_at") or item.get("createdAt"))
        if not _in_window(parsed, since, until):
            outside += 1
            continue
        text = str(item.get("text") or "")
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        likes = _metric(metrics, "likes")
        reposts = _metric(metrics, "reposts")
        author_raw = packed["pool_handle"] or item.get("author_handle") or item.get("username") or ""
        author_norm = str(author_raw).strip().lstrip("@").lower()
        is_config_author = author_norm in config_accounts_norm
        company_hits = len(pat.findall(text))
        fund_hits = len(FUND_TERMS.findall(text))
        if is_legacy:
            relevant = packed["via"] == "pool" or fund_hits > 0
        else:
            relevant = company_hits > 0 or fund_hits > 0
        candidates.append({
            "id": post_id,
            "via": packed["via"],
            "is_config_author": is_config_author,
            "relevant": relevant,
            "pool_handle": packed["pool_handle"],
            "item": item,
            "text": text,
            "company_hits": company_hits,
            "fund_hits": fund_hits,
            "engagement": (likes or 0) + 3 * (reposts or 0),
            "likes": likes,
            "reposts": reposts,
            "replies": _metric(metrics, "replies"),
            "views": _metric(metrics, "views"),
        })
    substantive = sorted(
        [row for row in candidates if row["relevant"]],
        key=lambda row: (row["via"] in ("pool", "config") or row["is_config_author"], row["engagement"]),
        reverse=True,
    )
    keep_max = int(cfg.get("x_keep_max") or 30)
    enrich_max = int(cfg.get("x_enrich_max") or 20)
    # 非旧四家时，三个来源的不相关帖子都不进入 noise_ids（也不写入 seen.json）。
    noise_rows = [row for row in candidates if not row["relevant"]] if is_legacy else []
    overflow = substantive[keep_max:]
    # 超过保留上限的候选按旧程序进入 noise，不写入 failed_sources。
    noise_ids = [f"x:{row['id']}" for row in noise_rows + overflow]
    authors = _load_authors(workspace)
    new: dict[str, Any] = {}
    followup: list[dict[str, Any]] = []
    for index, row in enumerate(substantive[:keep_max]):
        item = row["item"]
        handle = row["pool_handle"] or item.get("author_handle")
        cache = authors.get(str(handle).lower()) if isinstance(handle, str) else None
        if not isinstance(cache, dict):
            cache = {}
        enrich = index < enrich_max
        followers = cache.get("followers") if enrich and "followers" in cache else None
        bio = cache.get("bio") if enrich and "bio" in cache else None
        if enrich and (followers is None or bio is None):
            followup.append({
                "handle": handle,
                "id": f"x:{row['id']}",
                "reason": "本地作者缓存没有粉丝数或简介" if cache else "本地作者缓存没有这个账号",
            })
        elif not enrich and handle:
            followup.append({"handle": handle, "id": f"x:{row['id']}", "reason": "超过作者资料补充上限"})
        display = cache.get("handle") or handle
        new[f"x:{row['id']}"] = {
            "url": item.get("url") or f"https://x.com/{display or 'i'}/status/{row['id']}",
            "created_at": item.get("created_at") or item.get("createdAt"),
            "author": display,
            "author_name": item.get("author_name") or cache.get("name"),
            "followers": followers if isinstance(followers, (int, float)) and not isinstance(followers, bool) else None,
            "bio": bio if isinstance(bio, str) else None,
            "bookmarks": None,
            "profile_source": "x_authors_cache" if cache else "unavailable",
            "profile_fetched_at": cache.get("fetched_at") if cache else None,
            "in_pool": bool(display and str(display).lower() in pool_keys),
            "via": row["via"],
            "likes": row["likes"],
            "reposts": row["reposts"],
            "replies": row["replies"],
            "views": row["views"],
            "fund_hits": row["fund_hits"],
            "engagement": row["engagement"],
            "is_reply": item.get("is_reply"),
            "text": row["text"][:900],
        }
    return {
        "status": statuses,
        "new": new,
        "noise_ids": noise_ids,
        "pool_size": len(pool_handles),
        "queries": list(cfg.get("x_queries") or []),
        "outside_window": outside,
        "unprocessed_upstream": unprocessed,
        "profile_followup": followup,
        "failures": failures,
        "metrics_note": (
            "likes、reposts、replies、views 按 x-timeline 归一化结果保存；"
            "bookmarks 上游没有返回，记为 null，排序只用 likes+3*reposts。"
        ),
        "note": "专业投资人观点作多空论据，权重低于 TMTB、卖方邮件和 AlphaEngine；券商动作和公司数字回原始来源核对。",
    }


def failed_sources(
    internal: dict[str, Any],
    ae: dict[str, Any],
    sec: dict[str, Any],
    xdata: dict[str, Any],
    consensus: dict[str, Any],
    *,
    legacy_verbatim: bool = False,
) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for route, row in (internal.get("status") or {}).items():
        if not isinstance(row, dict):
            continue
        if not row.get("ok", False) or row.get("incomplete"):
            failures.append({"source": str(route), "error": str(row.get("error") or row.get("status") or "incomplete")})
    for row in ae.get("status") or []:
        if isinstance(row, dict) and (not row.get("ok", False) or row.get("incomplete")):
            failures.append({
                "source": "alphaengine",
                "error": f"{row.get('query')}: {row.get('error') or '未完成，覆盖日期不推进'}",
            })
    sec_bits = [
        f"{row.get('form')}: {row.get('error') or row.get('status') or 'failed'}"
        for row in sec.get("status") or []
        if isinstance(row, dict) and (not row.get("ok", False) or row.get("incomplete"))
    ]
    if sec_bits:
        failures.append({"source": "sec", "error": "; ".join(sec_bits)})
    if xdata.get("failures"):
        failures.append({"source": "x-timeline", "error": "; ".join(xdata["failures"])})
    cal_error = consensus.get("earnings_dates_error")
    fs = consensus.get("field_status") if isinstance(consensus.get("field_status"), dict) else {}
    if legacy_verbatim:
        if cal_error:
            failures.append({"source": "earnings_calendar", "error": str(cal_error)})
        non_cal_error = any(k != "earnings_dates" and v == "error" for k, v in fs.items()) or bool(
            consensus.get("error")
        )
        status_l = str(consensus.get("status") or "").strip().lower()
        if (not cal_error or non_cal_error) and (
            consensus.get("ok") is False or status_l in ("error", "partial", "incomplete")
        ):
            detail = str(consensus.get("error") or consensus.get("status") or "incomplete")
            if consensus.get("field_status") is not None:
                detail = f"{detail}: field_status={json.dumps(consensus.get('field_status'), ensure_ascii=False)}"
            failures.append({"source": "estimates", "error": detail})
        return failures

    if cal_error or fs.get("earnings_dates") == "error":
        failures.append({"source": "earnings_calendar", "error": str(cal_error or "earnings_calendar failed")})
    price_obj = consensus.get("price") if isinstance(consensus.get("price"), dict) else {}
    price_err = price_obj.get("error") if fs.get("price") == "error" or "error" in price_obj else None
    if price_err:
        failures.append({"source": "price", "error": str(price_err)})
    cons_fields = ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions", "price_targets")
    cons_enabled = (not fs) or any(fs.get(k) not in (None, "skipped") for k in cons_fields)
    cons_error = any(fs.get(k) == "error" for k in cons_fields) or bool(consensus.get("error"))
    status_l = str(consensus.get("status") or "").strip().lower()
    if cons_enabled and (
        cons_error
        or (
            not cal_error
            and not price_err
            and (consensus.get("ok") is False or status_l in ("error", "partial", "incomplete"))
        )
    ):
        detail = str(consensus.get("error") or consensus.get("status") or "incomplete")
        if consensus.get("field_status") is not None:
            detail = f"{detail}: field_status={json.dumps(consensus.get('field_status'), ensure_ascii=False)}"
        failures.append({"source": "estimates", "error": detail})
    return failures


def _previous_snapshot(
    workspace: Path,
    consensus_dir: Path,
    current: Path,
    hint: Any,
) -> dict[str, Any] | None:
    safe_hint = None
    if isinstance(hint, str) and hint and not hint.startswith("/") and ".." not in Path(hint).parts:
        try:
            full = ensure_relative(workspace, Path(hint))
        except CollectError:
            full = None
        if full is not None and _inside(consensus_dir, full) and full.resolve() != current.resolve() and full.is_file():
            safe_hint = hint
    loaded = _g.latest_previous_snapshot(consensus_dir, current, safe_hint, workspace)
    return loaded if isinstance(loaded, dict) else None


def render_packet_md(pk: dict[str, Any]) -> str:
    has_data_status = isinstance(pk.get("data_status"), dict)
    lines = [
        f"# {pk['ticker']} 跟踪数据包 {pk['run_id']}",
        "",
        f"- 窗口：{pk['since']} 至 {pk['until']}（美东）",
        f"- 采集时间：{pk.get('collected_at') or '未知'}",
        f"- 内部库重试起点：{pk.get('internal_retry_since') or '无'}",
        f"- 一致预期快照：`{pk.get('snapshot_file')}`",
        "- 是否允许短路结束（short_circuit_eligible）：false",
        "- 新闻和待核实线索仍要在研究步骤里核对，采集阶段不短路。",
    ]
    failures = pk.get("failed_sources") or []
    if failures:
        lines.append("- 取数失败或未完成：")
        for item in failures:
            lines.append(f"  - {item.get('source')}：{item.get('error')}")
    lines.extend(["", "## 自动触发项"])
    lines.extend([f"- {flag}" for flag in pk.get("auto_flags") or []] or ["- 无"])
    cons = pk.get("consensus") or {}
    price = cons.get("price") if isinstance(cons.get("price"), dict) else {}
    lines.extend(["", "## 一致预期"])
    if has_data_status:
        if _has_price(cons):
            c1d = f"{price.get('chg_1d_pct')}%" if price.get("chg_1d_pct") is not None else "暂无"
            c5d = f"{price.get('chg_5d_pct')}%" if price.get("chg_5d_pct") is not None else "暂无"
            lines.append(
                f"- 收盘 {price.get('as_of')}：{price.get('close')}，1 日 {c1d}，5 日 {c5d}"
            )
        else:
            lines.append(f"- 历史行情：请求失败（{price['error']}）" if "error" in price else "- 历史行情：暂无")
        if not _has_estimates(cons):
            lines.append("- 一致预期：暂无")
        fs_cal = (cons.get("field_status") or {}).get("earnings_dates") if isinstance(cons.get("field_status"), dict) else None
        if cons.get("earnings_dates_error"):
            lines.append(f"- 财报日历：请求失败（{cons['earnings_dates_error']}）")
        elif fs_cal == "skipped":
            lines.append("- 财报日历：已关闭")
        elif _has_earnings_calendar(cons):
            cal = cons.get("earnings_dates")
            if isinstance(cal, list):
                cal_items = [
                    str(item.get("Earnings Date") or item.get("date") or item)
                    if isinstance(item, dict)
                    else str(item)
                    for item in cal[:3]
                ]
                lines.append(f"- 财报日历：{'、'.join(cal_items)}")
            else:
                lines.append(f"- 财报日历：{json.dumps(cal, ensure_ascii=False)}")
        else:
            lines.append("- 财报日历：暂无")
    elif price:
        is_legacy_ticker = str(pk.get("ticker") or "").strip().upper() in LEGACY_VERBATIM_TICKERS
        if not is_legacy_ticker and "error" in price:
            lines.append(f"- 历史行情：请求失败（{price['error']}）")
        else:
            lines.append(
                f"- 收盘 {price.get('as_of')}：{price.get('close')}，1 日 {price.get('chg_1d_pct')}%，5 日 {price.get('chg_5d_pct')}%"
            )
    lines.append(f"- 修正差值：`{json.dumps(pk.get('deltas') or {}, ensure_ascii=False)}`")
    model = pk.get("implied_model") or {}
    lines.extend(["", "## 隐含费用"])
    if model.get("status") == "empty" and not (model.get("periods") or {}) and (has_data_status or model.get("summary") == "暂无"):
        lines.append("- 隐含费用：暂无")
    else:
        assumptions = model.get("assumptions") or {}
        if assumptions.get("source"):
            lines.append(f"- 来源：{assumptions['source']}")
        for item in assumptions.get("applicable_years") or []:
            income = item.get("other_income_bn")
            reason = item.get("other_income_reason")
            if reason:
                shown = "未知" if income is None else income
                lines.append(
                    f"- 假设对应财年：{item.get('key')} = {item.get('fiscal_year')}，截至 {item.get('period_end_prefix')}，其他收入 {shown}（{reason}）"
                )
            else:
                lines.append(
                    f"- 假设对应财年：{item.get('key')} = {item.get('fiscal_year')}，截至 {item.get('period_end_prefix')}，其他收入 {income}"
                )
        for key in ("0y", "+1y"):
            row = (model.get("periods") or {}).get(key) or {}
            if row.get("status") == "ok":
                lines.append(
                    f"- {row.get('label')}（{row.get('fiscal_year')}，period_end {row.get('period_end')}，其他收入取 {row.get('other_income_key')}）："
                    f"收入 {row.get('consensus_revenue_bn')}、EPS {row.get('consensus_eps')}、税前 {row.get('implied_pretax_bn')}、"
                    f"总费用 {row.get('implied_total_expenses_bn')}、经营利润率 {row.get('implied_op_margin_pct')}%、"
                    f"费用每多 100 亿 EPS {row.get('eps_per_10bn_expense')}、收入每多 1% EPS {row.get('eps_per_1pct_revenue_no_cost')}"
                )
            else:
                lines.append(f"- {key}：{row.get('reason') or row.get('status') or ('暂无' if has_data_status else '未计算')}")
        if model.get("error"):
            lines.append(f"- 模型状态：{model['error']}")

    internal = pk.get("internal") or {}
    lines.extend(["", "## 内部库"])
    for doc_id, info in (internal.get("new") or {}).items():
        lines.append(
            f"- {doc_id} | {info.get('date')} | {info.get('route')} | {info.get('title')} | {info.get('excerpt_file') or info.get('skipped')}"
        )
        if info.get("excerpt"):
            lines.append(info["excerpt"])
    if not internal.get("new"):
        lines.append("- 无新文档")
    lines.append(f"- 全文读完没有别名、记为无关：{len(internal.get('irrelevant') or {})} 篇")
    pending = internal.get("pending") or {}
    if pending:
        lines.append("- 读全文未完成，保持待处理，没有记成无关：")
        for doc_id, info in pending.items():
            lines.append(f"  - {doc_id} | {info.get('title')} | {info.get('pending_reason')} | {info.get('read_error')}")
    withheld = internal.get("withheld") or {}
    if withheld:
        lines.append("- wiki 全文别名不足 2 次，保留可见、不记成无关：")
        for doc_id, info in withheld.items():
            lines.append(f"  - {doc_id} | {info.get('title')} | 命中 {info.get('alias_hits')}")
    lines.append(f"- 来源状态：`{json.dumps(internal.get('status') or {}, ensure_ascii=False)}`")

    ae = pk.get("alphaengine") or {}
    lines.extend(["", "## AlphaEngine"])
    lines.append("固定查询：")
    for row in (pk.get(ae_state_key(str(pk.get("ticker") or TICKER))) or {}).get("queries") or []:
        window = row.get("window") or {}
        lines.append(
            f"- {row.get('query')} | 窗口 {window.get('start')} 至 {window.get('end')} | "
            f"complete={str(bool(row.get('complete'))).lower()} | cursor={row.get('cursor')}"
        )
        if not row.get("complete") and row.get("error"):
            lines.append(f"  备注：{row.get('error')}")
        elif row.get("note"):
            lines.append(f"  备注：{row.get('note')}")
    for doc_id, info in (ae.get("new") or {}).items():
        lines.append(f"- {doc_id} | {info.get('publish_time')} | {info.get('title')}")
        if info.get("snippet"):
            lines.append(f"  摘要：{info['snippet'][:600]}")
    if not ae.get("new"):
        lines.append("- 无")

    sec = pk.get("sec") or {}
    lines.extend(["", "## SEC"])
    for doc_id, info in (sec.get("new") or {}).items():
        lines.append(f"- {doc_id} | {info.get('form')} | {info.get('filing_date')}")
    if not sec.get("new"):
        lines.append("- 无")
    lines.append(f"- 状态：`{json.dumps(sec.get('status') or [], ensure_ascii=False)}`")

    news = pk.get("news") or {}
    lines.extend(["", "## 新闻"])
    lines.append(f"- 状态：{news.get('status')}，需要代理用 WebSearch/WebFetch 打开原文")
    lines.append(f"- 窗口：{news.get('since')} 至 {news.get('until')}")
    for query in news.get("queries") or []:
        lines.append(f"- {query}")

    xdata = pk.get("x") or {}
    lines.extend(["", "## X"])
    lines.append(f"- 账号池 {xdata.get('pool_size', 0)} 个；噪声 {len(xdata.get('noise_ids') or [])} 条，提交前不写入 seen")
    if xdata.get("unprocessed_upstream"):
        lines.append("- 上游还有未取回的候选。这些 id 不进入失败列表；已取回但超过保留上限的进入噪声。")
    lines.append(f"- {xdata.get('metrics_note')}")
    for query in xdata.get("queries") or []:
        lines.append(f"- 查询：{query}")
    for doc_id, info in (xdata.get("new") or {}).items():
        lines.append(
            f"- {doc_id} | {info.get('via')} | @{info.get('author')} | {info.get('created_at')} | "
            f"赞 {info.get('likes')} 转 {info.get('reposts')} 收藏 {info.get('bookmarks')} | 信号词 {info.get('fund_hits')}"
        )
        if info.get("bio"):
            lines.append(f"  简介：{info['bio']}")
        lines.append(f"  {info.get('text')}")
    if xdata.get("profile_followup"):
        lines.append("- 作者资料待补充：")
        for row in xdata["profile_followup"]:
            lines.append(f"  - {row.get('handle')} | {row.get('reason')}")
    if not xdata.get("new"):
        lines.append("- 无")
    lines.append(f"- 状态：`{json.dumps(xdata.get('status') or [], ensure_ascii=False)}`")
    lines.append("")
    lines.append("collect 不修改 seen.json、last_run.json、updates.log。")
    return "\n".join(lines) + "\n"


def _completed_packet(path: Path, run_id: str) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectError("packet_unreadable", "已有 packet.json 无法读取，没有覆盖") from exc
    if not isinstance(data, dict):
        raise CollectError("packet_exists_not_complete", "已有 packet.json 不是完成的数据包，没有覆盖")
    if data.get("collection_complete") is not True:
        raise CollectError("packet_exists_not_complete", "已有 packet.json 未标记完成，没有覆盖、也没有重新取数")
    if data.get("run_id") != run_id or str(data.get("ticker") or "").upper() != TICKER:
        raise CollectError("packet_run_mismatch", "已有 packet 的 run_id 或公司与本轮不一致，没有覆盖")
    return data


def _summary(
    workspace: Path,
    packet: dict[str, Any],
    recovered: bool,
    rules_path: Path | None = None,
) -> dict[str, Any]:
    run_id = str(packet["run_id"])
    tracker = workspace / "research" / "tracker" / run_id
    internal = packet.get("internal") or {}
    out: dict[str, Any] = {
        # 完成的数据包保留来源告警，供后续研究披露。
        "ok": True,
        "recovered": recovered,
        "ticker": TICKER,
        "run_id": run_id,
        "since": packet.get("since"),
        "until": packet.get("until"),
        "short_circuit_eligible": False,
        "failed_sources": packet.get("failed_sources") or [],
        "packet_json": (Path("research") / "tracker" / run_id / "packet.json").as_posix(),
        "packet_md": (Path("research") / "tracker" / run_id / "packet.md").as_posix(),
        "snapshot_file": packet.get("snapshot_file"),
        "counts": {
            "internal": len(internal.get("new") or {}),
            "internal_irrelevant": len(internal.get("irrelevant") or {}),
            "internal_pending": len(internal.get("pending") or {}),
            "alphaengine": len((packet.get("alphaengine") or {}).get("new") or {}),
            "sec": len((packet.get("sec") or {}).get("new") or {}),
            "x": len((packet.get("x") or {}).get("new") or {}),
            "x_noise": len((packet.get("x") or {}).get("noise_ids") or []),
        },
        "tracker_dir": str(tracker.relative_to(workspace)),
    }
    if rules_path is not None:
        out["rules_path"] = str(rules_path)
    return out


def _window(last_run: dict[str, Any], since_arg: str | None, today: str) -> tuple[str, str]:
    if since_arg:
        since = parse_day(since_arg, "since")
    elif last_run.get("until"):
        since = (date.fromisoformat(str(last_run["until"])) - timedelta(days=1)).isoformat()
    else:
        since = (date.fromisoformat(today) - timedelta(days=FIRST_LOOKBACK_DAYS)).isoformat()
    if since > today:
        raise CollectError("bad_date", "since 晚于本轮 until")
    return since, today


def cmd_collect(args: argparse.Namespace) -> int:
    workspace = open_workspace(args.workspace_dir)
    company = load_company(workspace)
    cfg = load_research_config()
    rules_path, _rules_source = resolve_rules_path(TICKER, workspace, strict=True)
    run_id = validate_run_id(args.run_id or now_et().strftime("%Y%m%d-%H%M"))
    paths = _g.tracker_paths(workspace)
    assert_no_escaping_symlinks(workspace, paths["tracker"])
    assert_no_escaping_symlinks(workspace, paths["consensus"])
    with workspace_lock(workspace, paths["tracker"]):
        packet_path = paths["tracker"] / run_id / "packet.json"
        existing = _completed_packet(packet_path, run_id)
        if existing is not None:
            md_path = paths["tracker"] / run_id / "packet.md"
            if not md_path.exists():
                atomic_write_text(workspace, md_path, render_packet_md(existing))
            return emit(_summary(workspace, existing, True, rules_path=rules_path))
        bridge_cap_on = _researchdata_bridge_capability_enabled(cfg)
        if bridge_cap_on:
            transport = resolve_researchdata_transport()
            if transport.get("mode") is None:
                return fail(
                    _g.RESEARCHDATA_TRANSPORT_UNCONFIGURED,
                    str(transport.get("error") or _g.RESEARCHDATA_TRANSPORT_UNCONFIGURED),
                    hint="本轮未采集任何来源",
                )
        last_run = _read_state_object(paths["last_run"])
        seen = set(_read_state_list(paths["seen"]))
        before = {
            "seen": paths["seen"].read_text(encoding="utf-8") if paths["seen"].exists() else None,
            "last": paths["last_run"].read_text(encoding="utf-8") if paths["last_run"].exists() else None,
            "log": paths["log"].read_text(encoding="utf-8") if paths["log"].exists() else None,
        }
        current = now_et()
        today = current.date().isoformat()
        since, until = _window(last_run, args.since, today)
        collected_at = current.isoformat()
        internal_since = _internal_since(last_run, since)
        sec_since = _source_since(last_run, "sec_last_collected_until", since)
        x_since = _source_since(last_run, "x_last_collected_until", since)
        run_dir = paths["tracker"] / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        legacy_verbatim = _is_legacy_four_verbatim(TICKER, cfg)
        if legacy_verbatim:
            cfg = _filter_legacy_config(cfg)
        has_explicit_switches = _has_explicit_new_market_switches(cfg)
        benchmark = benchmark_of(company)
        if legacy_verbatim:
            if fetch_consensus_snapshot is _DEFAULT_FETCH_CONSENSUS:
                consensus = _g.fetch_consensus_snapshot(TICKER, benchmark)
            else:
                consensus = fetch_consensus_snapshot(TICKER, benchmark=benchmark)
        else:
            inc_cons = _source_enabled(cfg, "consensus")
            inc_price = _source_enabled(cfg, "price")
            inc_cal = _earnings_calendar_enabled(cfg)
            if fetch_consensus_snapshot is _DEFAULT_FETCH_CONSENSUS:
                consensus = _fetch_configured_market_data(
                    TICKER,
                    benchmark,
                    include_consensus=inc_cons,
                    include_price=inc_price,
                    include_earnings_calendar=inc_cal,
                )
            elif inc_cons or inc_price or inc_cal:
                consensus = fetch_consensus_snapshot(TICKER, benchmark=benchmark)
                if isinstance(consensus, dict):
                    if not inc_cons:
                        for field in ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions", "price_targets"):
                            consensus[field] = {}
                    if not inc_price:
                        consensus["price"] = {}
                    if not inc_cal:
                        consensus["earnings_dates"] = []
            else:
                consensus = _fetch_configured_market_data(
                    TICKER,
                    benchmark,
                    include_consensus=False,
                    include_price=False,
                    include_earnings_calendar=False,
                )
        stamp = current.strftime("%Y-%m-%d_%H%M")
        snap_rel = Path("data") / "consensus" / f"{stamp}.json"
        snap_path = ensure_relative(workspace, snap_rel)
        previous = _previous_snapshot(workspace, paths["consensus"], snap_path, last_run.get("snapshot_file"))
        internal_ran = bridge_cap_on and _source_enabled(cfg, "internal") and bool(_effective_internal_routes(cfg))
        if internal_ran:
            internal = collect_internal(cfg, internal_since, until, seen, workspace, run_dir)
        else:
            internal = {"status": {}, "new": {}, "irrelevant": {}, "pending": {}, "withheld": {}}
        if bridge_cap_on and _source_enabled(cfg, "alphaengine") and _alphaengine_capability_enabled(cfg):
            ae, ae_state = collect_alphaengine(cfg, since, until, seen, last_run)
        else:
            ae, ae_state = {"status": [], "new": {}}, {"queries": []}
        if bridge_cap_on and _source_enabled(cfg, "sec"):
            sec = collect_sec(cfg, sec_since, seen)
        else:
            sec = {"status": [], "new": {}}
        if bridge_cap_on and _source_enabled(cfg, "x"):
            xdata = collect_x(cfg, x_since, until, seen, workspace, legacy_verbatim=legacy_verbatim)
        else:
            xdata = {
                "status": [],
                "new": {},
                "noise_ids": [],
                "pool_size": 0,
                "queries": [],
                "outside_window": 0,
                "unprocessed_upstream": False,
                "profile_followup": [],
                "failures": [],
                "metrics_note": "",
                "note": "",
            }
        thr = map_thresholds(cfg.get("thresholds"))
        raw_extra = cfg.get("extra_consensus_flags")
        extra_flags = raw_extra if isinstance(raw_extra, list) else []
        deltas, flags = consensus_deltas(
            consensus,
            previous,
            thr,
            sec_new_count=len(sec["new"]),
            extra_flags=extra_flags,
            legacy_verbatim=legacy_verbatim,
        )
        model = build_implied_model(
            consensus,
            cfg.get("model") or {},
            require_valid_periods=not legacy_verbatim,
        )
        failures = failed_sources(internal, ae, sec, xdata, consensus, legacy_verbatim=legacy_verbatim)
        news_enabled = _source_enabled(cfg, "news")
        prev_internal_retry = _coerce_retry_since(last_run.get("internal_retry_since"))
        if not internal_ran:
            next_internal_retry = prev_internal_retry
        else:
            round_internal_retry = internal_since if _internal_needs_retry(internal) else None
            if _has_capability_skipped_internal_routes(cfg):
                prev_internal_until = _coerce_day(last_run.get("internal_last_collected_until"))
                retry_candidates = [
                    d for d in (prev_internal_retry, prev_internal_until, round_internal_retry) if d is not None
                ]
                next_internal_retry = min(retry_candidates) if retry_candidates else None
            else:
                next_internal_retry = round_internal_retry
        packet: dict[str, Any] = {
            "ticker": TICKER,
            "run_id": run_id,
            "since": since,
            "until": until,
            "snapshot_file": snap_rel.as_posix(),
            "consensus": consensus,
            "implied_model": model,
            "deltas": deltas,
            "auto_flags": flags,
            "thresholds": thr,
            "internal": internal,
            "alphaengine": ae,
            "sec": sec,
            "x": xdata,
            "news": {
                "status": "pending_agent_search" if news_enabled else "skipped",
                "queries": list(cfg.get("news_queries") or []) if news_enabled else [],
                "since": since,
                "until": until,
                "requires_original_fetch": news_enabled,
            },
            "failed_sources": failures,
            "short_circuit_eligible": False,
            "collected_at": collected_at,
            "internal_since": internal_since,
            "internal_retry_since": next_internal_retry,
            ae_state_key(): ae_state,
            "limits": {
                "internal_limit": cfg.get("internal_limit"),
                "internal_read_cap_per_route": cfg.get("internal_read_cap_per_route"),
                "internal_pages": MAX_INTERNAL_PAGES,
                "read_pages": MAX_READ_PAGES,
                "excerpt_max": EXCERPT_MAX,
                "ae_max_searches": cfg.get("ae_max_searches"),
                "ae_limit": AE_LIMIT,
                "sec_limit": SEC_LIMIT,
                "x_search_count": X_SEARCH_COUNT,
                "x_timeline_count": X_TIMELINE_COUNT,
                "x_pool_max": cfg.get("x_pool_max"),
                "x_keep_max": cfg.get("x_keep_max"),
                "x_enrich_max": cfg.get("x_enrich_max"),
            },
            "collection_complete": True,
        }
        if not legacy_verbatim and has_explicit_switches:
            fs_cal = (consensus.get("field_status") or {}).get("earnings_dates") if isinstance(consensus.get("field_status"), dict) else None
            if not inc_cal or fs_cal == "skipped":
                cal_status = "已关闭"
            elif consensus.get("earnings_dates_error"):
                cal_status = "error"
            elif _has_earnings_calendar(consensus):
                cal_status = "ok"
            else:
                cal_status = "暂无"
            price_failed = (isinstance(consensus.get("price"), dict) and "error" in consensus["price"]) or (
                (consensus.get("field_status") or {}).get("price") == "error"
            )
            packet["data_status"] = {
                "price": "error" if price_failed else ("ok" if _has_price(consensus) else "暂无"),
                "consensus": "ok" if _has_estimates(consensus) else "暂无",
                "earnings_calendar": cal_status,
            }
        paths["consensus"].mkdir(parents=True, exist_ok=True)
        atomic_write_json(workspace, snap_path, consensus)
        atomic_write_text(workspace, run_dir / "packet.md", render_packet_md(packet))
        atomic_write_json(workspace, run_dir / "packet.json", packet)
        after = {
            "seen": paths["seen"].read_text(encoding="utf-8") if paths["seen"].exists() else None,
            "last": paths["last_run"].read_text(encoding="utf-8") if paths["last_run"].exists() else None,
            "log": paths["log"].read_text(encoding="utf-8") if paths["log"].exists() else None,
        }
        if after != before:
            raise CollectError("state_mutated", "collect 写入了 seen、last_run 或 updates.log")
        return emit(_summary(workspace, packet, False, rules_path=rules_path))


def _internal_uncommitted(info: Any) -> bool:
    """未读完、被跳过或明确标成未分析的文档不能进入 seen。"""
    if not isinstance(info, dict):
        return False
    if info.get("analyzed") is False:
        return True
    return bool(info.get("read_error") or info.get("incomplete") or info.get("skipped"))


def _commit_ids(packet: dict[str, Any]) -> set[str]:
    internal = packet.get("internal") if isinstance(packet.get("internal"), dict) else {}
    pending = set((internal.get("pending") or {}))
    withheld = set((internal.get("withheld") or {}))
    ids: set[str] = set()
    for doc_id, info in (internal.get("new") or {}).items():
        if doc_id in pending or doc_id in withheld or _internal_uncommitted(info):
            continue
        ids.add(str(doc_id))
    for doc_id, info in (internal.get("irrelevant") or {}).items():
        if _internal_uncommitted(info):
            continue
        ids.add(str(doc_id))
    for bucket in (
        (packet.get("alphaengine") or {}).get("new"),
        (packet.get("sec") or {}).get("new"),
        (packet.get("x") or {}).get("new"),
    ):
        if isinstance(bucket, dict):
            ids.update(str(doc_id) for doc_id in bucket)
    noise = (packet.get("x") or {}).get("noise_ids") if isinstance(packet.get("x"), dict) else []
    if isinstance(noise, list):
        ids.update(str(doc_id) for doc_id in noise if isinstance(doc_id, str))
    return ids


def _committed_ae_covered(last_run: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for query, row in _ae_prev_map(last_run).items():
        day = _coerce_day(row.get("covered_until"))
        if day:
            found[query] = day
    return found


def _validate_ae_state(state: dict[str, Any], last_run: dict[str, Any] | None = None) -> None:
    retained = _committed_ae_covered(last_run or {})
    queries = state.get("queries")
    if not isinstance(queries, list):
        raise CollectError("bad_ae_state", f"{ae_state_key()}.queries 必须是列表")
    _stop_markers = (
        "未覆盖日期：",
        "未覆盖回看窗口：",
        "留待下一轮",
        "游标失效",
        "缓存结果",
        "未扫完",
    )
    for row in queries:
        if not isinstance(row, dict) or not isinstance(row.get("query"), str):
            raise CollectError("bad_ae_state", "AE 查询状态缺少 query")
        window = row.get("window")
        if not isinstance(window, dict):
            raise CollectError("bad_ae_state", "AE 查询状态缺少 window")
        start = parse_day(str(window.get("start")), "ae.window.start")
        end = parse_day(str(window.get("end")), "ae.window.end")
        if start > end:
            raise CollectError("bad_ae_state", "AE 窗口起点晚于终点")

        covered = row.get("covered_until")
        is_complete = row.get("complete") is True
        if is_complete:
            if covered != end:
                raise CollectError("ae_covered_until_mismatch", "完成的 AE 查询只能把 covered_until 记到本窗口终点")
            covered_day: str | None = end
        elif covered not in (None, ""):
            covered_day = parse_day(str(covered), "ae.covered_until")
        else:
            covered_day = None

        skipped_backlog = row.get("skipped_backlog")
        skip_until: str | None = None
        if skipped_backlog is not None:
            if not isinstance(skipped_backlog, dict):
                raise CollectError("bad_ae_state", "AE skipped_backlog 必须是对象")
            skip_since = parse_day(str(skipped_backlog.get("since")), "ae.skipped_backlog.since")
            skip_until = parse_day(str(skipped_backlog.get("until")), "ae.skipped_backlog.until")
            if skip_since > skip_until or skip_until >= end:
                raise CollectError("bad_ae_state", "AE skipped_backlog 日期区间非法")
            if covered_day is None or skip_until >= covered_day:
                raise CollectError("bad_ae_state", "AE skipped_backlog 终点必须早于 covered_until")

        partial_days_raw = row.get("partial_days")
        partial_days: list[str] = []
        if partial_days_raw is not None:
            if not isinstance(partial_days_raw, list):
                raise CollectError("bad_ae_state", "AE partial_days 必须是列表")
            if is_complete and partial_days_raw:
                raise CollectError("bad_ae_state", "完成的 AE 查询不能包含 partial_days")
            for raw_day in partial_days_raw:
                parsed_day = parse_day(str(raw_day), "ae.partial_days")
                if parsed_day < start:
                    raise CollectError("ae_incomplete_advanced", "AE partial_days 不能早于窗口起点")
                if parsed_day > end:
                    raise CollectError("ae_incomplete_advanced", "AE partial_days 不能晚于窗口终点")
                if covered_day is None or parsed_day > covered_day:
                    raise CollectError("ae_incomplete_advanced", "AE partial_days 不能晚于 covered_until")
                if skip_until is not None and parsed_day <= skip_until:
                    raise CollectError("bad_ae_state", "AE partial_days 不能落在 skipped_backlog 区间内")
                partial_days.append(parsed_day)

        if not is_complete and covered_day is not None:
            # 同一天重新采集未完成时，保留已经提交的覆盖日期；单日或连续多日正常用完单日
            # 翻页预算越过窗口终点（含先跳过积压后推进至终点）时，允许 covered_until 记为 end。
            # 从更早的覆盖日非正常抬到本窗口终点，或超过终点，仍然拒绝。
            err_text = str(row.get("error") or "")
            no_stop_markers = not any(m in err_text for m in _stop_markers)
            clean_single_day_advance = (
                row.get("ok") is True
                and not row.get("stale")
                and not row.get("hit_page_cap")
                and row.get("cursor") in (None, "")
                and no_stop_markers
            )
            allow_partial_day_advance = clean_single_day_advance and (
                (
                    bool(partial_days)
                    and "当天超过翻页上限，只取了前" in err_text
                    and (end in partial_days or row.get("has_more") is False)
                )
                or (
                    "partial_days" not in row
                    and (
                        f"{end} 当天超过翻页上限，只取了前" in err_text
                        or f"{end} 超过翻页上限，只取了前" in err_text
                        or "当天超过翻页上限，只取了前" in err_text
                    )
                )
            )
            if covered_day > end or (
                covered_day == end
                and retained.get(row["query"], "") < covered_day
                and not allow_partial_day_advance
            ):
                raise CollectError("ae_incomplete_advanced", "未完成的 AE 查询不能把覆盖日期推进到窗口终点")


def _snapshot_for_commit(workspace: Path, packet: dict[str, Any]) -> str:
    raw = packet.get("snapshot_file")
    if not isinstance(raw, str) or not raw or raw.startswith("/") or "\\" in raw:
        raise CollectError("bad_snapshot", "snapshot_file 必须是工作区内的相对路径")
    relative = Path(raw)
    if relative.parts[:2] != ("data", "consensus"):
        raise CollectError("bad_snapshot", "snapshot_file 必须位于 data/consensus")
    full = ensure_relative(workspace, relative)
    if not full.is_file():
        raise CollectError("bad_snapshot", "snapshot_file 不存在")
    return relative.as_posix()


def cmd_commit(args: argparse.Namespace) -> int:
    if args.material not in ("yes", "no"):
        return fail("bad_material", "material 只能是 yes 或 no")
    summary = str(args.summary or "").strip()
    if not summary:
        return fail("bad_summary", "summary 不能为空")
    workspace = open_workspace(args.workspace_dir)
    load_company(workspace)
    run_id = validate_run_id(args.run_id)
    paths = _g.tracker_paths(workspace)
    packet_path = ensure_relative(workspace, Path("research") / "tracker" / run_id / "packet.json")
    ensure_relative(workspace, Path("research") / "tracker" / "seen.json")
    ensure_relative(workspace, Path("research") / "tracker" / "last_run.json")
    log_path = ensure_relative(workspace, Path("research") / "tracker" / "updates.log")
    if log_path.is_symlink():
        raise CollectError("symlink_escape", f"拒绝透过符号链接读写：{log_path}")
    with workspace_lock(workspace, paths["tracker"]):
        packet = _completed_packet(packet_path, run_id)
        if packet is None:
            return fail("packet_missing", f"找不到完成的数据包 {run_id}")
        if packet.get("run_id") != run_id:
            return fail("packet_run_mismatch", "packet 的 run_id 与参数不一致")
        snapshot = _snapshot_for_commit(workspace, packet)
        ae_key = ae_state_key()
        ae_state = packet.get(ae_key)
        if not isinstance(ae_state, dict):
            return fail("bad_ae_state", f"packet 缺少 {ae_key}")
        seen = _read_state_list(paths["seen"])
        last_run = _read_state_object(paths["last_run"])
        _validate_ae_state(ae_state, last_run)
        log_text = paths["log"].read_text(encoding="utf-8") if paths["log"].exists() else ""
        new_ids = _commit_ids(packet)
        merged_set = set(seen) | new_ids
        merged = sorted(merged_set)
        current = last_run.get("run_id") == run_id
        # 同一 run 已经写进 last_run 时只补日志和 seen，不把状态改回这轮。
        advance = _is_newer_collection(packet, last_run) and not current
        already_logged = _log_has_run(log_text, run_id)
        if not paths["seen"].exists() or merged_set != set(seen):
            atomic_write_json(workspace, paths["seen"], merged)
        if advance:
            ae_state_to_save = _ae_state_for_last_run(ae_state)
            if not ae_state_to_save.get("queries"):
                prev_ae_state = last_run.get(ae_key) if isinstance(last_run, dict) else None
                if isinstance(prev_ae_state, dict) and prev_ae_state.get("queries"):
                    ae_state_to_save = _ae_state_for_last_run(prev_ae_state)
            last_body = {
                "ticker": TICKER,
                "run_id": run_id,
                "committed_at": now_et().isoformat(),
                "collected_at": _coerce_collected_at(packet),
                "since": packet.get("since"),
                "until": packet.get("until"),
                "snapshot_file": snapshot,
                "material": args.material,
                "summary": summary,
                ae_key: ae_state_to_save,
            }
            if not ae_state_to_save.get("queries") and isinstance(last_run, dict):
                if last_run.get("ae_covered_until") is not None:
                    last_body["ae_covered_until"] = last_run["ae_covered_until"]
                if last_run.get("ae_resume") is not None:
                    last_body["ae_resume"] = last_run["ae_resume"]
            internal_obj = packet.get("internal")
            internal_skipped = (
                isinstance(internal_obj, dict)
                and "status" in internal_obj
                and not internal_obj.get("status")
            )
            internal_failed = isinstance(internal_obj, dict) and _internal_needs_retry(internal_obj)
            if internal_skipped:
                if isinstance(last_run, dict) and "internal_retry_since" in last_run:
                    last_body["internal_retry_since"] = last_run.get("internal_retry_since")
                elif "internal_retry_since" in packet:
                    last_body["internal_retry_since"] = _coerce_retry_since(packet.get("internal_retry_since"))
                last_body["internal_last_collected_until"] = _resume_point(
                    last_run, "internal_last_collected_until", packet
                )
            elif internal_failed:
                prev_retry = (
                    _coerce_retry_since(last_run.get("internal_retry_since"))
                    if isinstance(last_run, dict)
                    else None
                )
                pkt_retry = _coerce_retry_since(packet.get("internal_retry_since"))
                retry_candidates = [d for d in (prev_retry, pkt_retry) if d is not None]
                last_body["internal_retry_since"] = min(retry_candidates) if retry_candidates else None
                last_body["internal_last_collected_until"] = _resume_point(
                    last_run, "internal_last_collected_until", packet
                )
            elif "internal_retry_since" in packet:
                last_body["internal_retry_since"] = _coerce_retry_since(packet.get("internal_retry_since"))
            elif isinstance(last_run, dict) and "internal_retry_since" in last_run:
                last_body["internal_retry_since"] = last_run.get("internal_retry_since")

            sec_obj = packet.get("sec")
            sec_skipped = (
                isinstance(sec_obj, dict)
                and "status" in sec_obj
                and not sec_obj.get("status")
            )
            sec_failed = (
                isinstance(sec_obj, dict)
                and isinstance(sec_obj.get("status"), list)
                and any(
                    isinstance(row, dict) and (not row.get("ok", False) or row.get("incomplete"))
                    for row in sec_obj["status"]
                )
            )
            if sec_skipped or sec_failed:
                last_body["sec_last_collected_until"] = _resume_point(
                    last_run, "sec_last_collected_until", packet
                )

            x_obj = packet.get("x")
            x_skipped = (
                isinstance(x_obj, dict)
                and "status" in x_obj
                and not x_obj.get("status")
                and not x_obj.get("metrics_note")
            )
            x_failed = isinstance(x_obj, dict) and (
                bool(x_obj.get("failures"))
                or (
                    isinstance(x_obj.get("status"), list)
                    and any(isinstance(row, dict) and not row.get("ok", False) for row in x_obj["status"])
                )
            )
            if x_skipped or x_failed:
                last_body["x_last_collected_until"] = _resume_point(
                    last_run, "x_last_collected_until", packet
                )

            atomic_write_json(workspace, paths["last_run"], last_body)
        log_repaired = False
        # updates.log 是完成回执。比当前 last_run 更早的首次提交也记一行，否则该轮会一直算未完成。
        # 这一行不改 last_run，也不改其中的 AE 覆盖状态或内部库重试起点。
        if not already_logged:
            paths["log"].parent.mkdir(parents=True, exist_ok=True)
            with paths["log"].open("a", encoding="utf-8") as handle:
                handle.write(
                    f"{now_et().strftime('%Y-%m-%d %H:%M')} | {run_id} | material={args.material} | {summary}\n"
                )
            log_repaired = current and not advance
        packet_rel = (Path("research") / "tracker" / run_id / "packet.json").as_posix()
        body = {
            "ok": True,
            "committed": run_id,
            "material": args.material if advance or not current else last_run.get("material"),
            "seen_total": len(merged),
            "new_ids_marked_seen": len(merged_set - set(seen)),
            "packet_json": packet_rel,
            "last_run_file": "research/tracker/last_run.json",
            "updates_log": "research/tracker/updates.log",
            "stale": not advance and not current,
            "idempotent": current and already_logged and merged_set == set(seen),
            "log_repaired": log_repaired,
            "last_run_unchanged": not advance,
        }
        if advance:
            body["counts"] = _summary(workspace, packet, False)["counts"]
        return emit(body)


def _use_ticker(raw: str) -> str:
    chosen = str(raw or "").strip().upper()
    if chosen not in supported_tickers():
        raise CollectError("unsupported_ticker", f"不支持的 ticker：{chosen or '空'}")
    return chosen


def main(argv: list[str] | None = None) -> int:
    global TICKER
    parser = argparse.ArgumentParser(description="多空日跟踪采集与提交")
    sub = parser.add_subparsers(dest="cmd", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--ticker", required=True)
    collect.add_argument("--workspace-dir", required=True)
    collect.add_argument("--since")
    collect.add_argument("--run-id")
    commit = sub.add_parser("commit")
    commit.add_argument("--ticker", required=True)
    commit.add_argument("--workspace-dir", required=True)
    commit.add_argument("--run-id", required=True)
    commit.add_argument("--material", required=True)
    commit.add_argument("--summary", required=True)
    args = parser.parse_args(argv)
    previous = TICKER
    try:
        chosen = _use_ticker(args.ticker)
    except CollectError as exc:
        return fail(exc.status, exc.message, ticker=str(args.ticker or "").strip().upper())
    TICKER = chosen
    try:
        if args.cmd == "collect":
            return cmd_collect(args)
        return cmd_commit(args)
    except CollectError as exc:
        return fail(exc.status, exc.message)
    finally:
        TICKER = previous


if __name__ == "__main__":
    sys.exit(main())
