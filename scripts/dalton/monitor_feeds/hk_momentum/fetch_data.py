#!/usr/bin/env python3
"""HK Momentum Index — data collector (HKEX universe, SZSE southbound list, Tencent quotes)."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
from datetime import date, datetime, timedelta
import io
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import time
from typing import Any, Iterable

import requests

from scripts.dalton.monitor_feeds.common import (
    HK_TZ,
    SourceStatus,
    atomic_write_json,
    atomic_write_text,
    load_json_safe,
    make_source_result,
    now_hk,
    previous_trading_day,
    resolve_market_trade_date,
    resolve_state_dir,
    trading_days_in_range,
)

HKEX_XLSX_URL = (
    "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
)
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}
logger = logging.getLogger(__name__)

HIST_START = "2023-06-01"
# Tencent returns the latest N bars and ignores the start date; up to ~1500 come
# back whole (above ~1600 it falls back to 640). 1200 covers HIST_START with room.
HIST_BARS = 1200
MAX_HIST_FAIL_RATIO = 0.03


def _sess(proxy: str = "") -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update(UA)
    clean_proxy = (proxy or "").strip()
    if clean_proxy:
        s.proxies = {"http": clean_proxy, "https": clean_proxy}
    return s


def resolve_hkex_proxy(explicit: str | None = None) -> str:
    if explicit is not None:
        return explicit.strip()
    return (
        os.environ.get("HK_MOMENTUM_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or ""
    ).strip()


def fetch_hkex_universe(*, state_dir: Path, proxy: str = "", session: Any = None):
    import pandas as pd

    path = state_dir / "hkex_los.xlsx"
    s = session or _sess(proxy=proxy)
    r = s.get(HKEX_XLSX_URL, timeout=60)
    r.raise_for_status()
    path.write_bytes(r.content)
    df = pd.read_excel(path, header=2)
    df = df[df["Category"] == "Equity"].copy()
    df["code"] = df["Stock Code"].astype(int).apply(lambda x: f"{x:05d}")
    df = df[df["Stock Code"].astype(int) <= 9999]
    return df[["code", "Name of Securities", "Sub-Category", "Trading Currency"]].rename(
        columns={
            "Name of Securities": "name",
            "Sub-Category": "subcat",
            "Trading Currency": "ccy",
        }
    )


class GGTListResult(dict[str, str]):
    """Code-to-name mapping returned by ``fetch_ggt_list`` with validation counters."""

    def __init__(
        self,
        *args: Any,
        missing_cn_name: int = 0,
        skipped_invalid: int = 0,
        skipped_codes: Iterable[str] = (),
        skipped_reasons: dict[str, int] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.missing_cn_name = int(missing_cn_name)
        self.skipped_invalid = int(skipped_invalid)
        self.skipped_codes = tuple(sorted({str(c).strip().zfill(5) for c in skipped_codes if str(c).strip()}))
        self.skipped_reasons = {str(k): int(v) for k, v in (skipped_reasons or {}).items() if int(v) > 0}


def _format_ggt_skip_note(skipped_invalid: int, skipped_reasons: dict[str, int] | None) -> str:
    if skipped_reasons:
        items = [(k, v) for k, v in skipped_reasons.items() if v > 0]
        if len(items) == 1:
            detail = items[0][0]
        else:
            detail = "、".join(f"{k} {v} 只" for k, v in items)
    else:
        detail = "记录字段无效"
    return f"跳过 {skipped_invalid} 只（{detail}）"


def fetch_ggt_list(*, session: Any = None) -> GGTListResult:
    """Fetch complete SZSE Southbound list across all pages; allow English fallback and skip invalid rows."""
    s = session or _sess()
    codes: dict[str, str] = {}
    page = 1
    expected_pages: int | None = None
    expected_records: int | None = None
    total_rows = 0
    missing_cn_name = 0
    skipped_invalid = 0
    skipped_codes: list[str] = []
    skipped_codes_set: set[str] = set()
    skipped_reasons: dict[str, int] = {}
    while True:
        url = (
            "https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
            f"&CATALOGID=SGT_GGTBDQD&PAGENO={page}&random={random.random()}"
        )
        j = s.get(url, timeout=15).json()
        if not isinstance(j, list) or not j or not isinstance(j[0], dict):
            raise RuntimeError(f"港股通名单第 {page} 页响应格式无效")
        block = j[0]
        meta = block.get("metadata")
        if not isinstance(meta, dict) or meta.get("pagecount") is None or isinstance(meta.get("pagecount"), bool):
            raise RuntimeError(f"港股通名单第 {page} 页缺少 pagecount 元数据")
        try:
            pc = int(meta["pagecount"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"港股通名单第 {page} 页 pagecount 无效: {meta.get('pagecount')!r}") from exc
        if pc <= 0:
            raise RuntimeError(f"港股通名单总页数无效: {meta.get('pagecount')!r}")
        if expected_pages is None:
            expected_pages = pc
        elif pc != expected_pages:
            raise RuntimeError(f"港股通名单第 {page} 页总页数不一致: {pc} != {expected_pages}")

        rc_raw = meta.get("recordcount")
        if rc_raw is None or isinstance(rc_raw, bool):
            raise RuntimeError(f"港股通名单第 {page} 页缺少 recordcount 元数据")
        try:
            rc = int(rc_raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"港股通名单第 {page} 页 recordcount 无效: {rc_raw!r}") from exc
        if rc <= 0:
            raise RuntimeError(f"港股通名单总条数无效: {rc_raw!r}")
        if expected_records is None:
            expected_records = rc
        elif rc != expected_records:
            raise RuntimeError(f"港股通名单第 {page} 页总条数不一致: {rc} != {expected_records}")

        data = block.get("data")
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"港股通名单第 {page} 页返回空列表")
        total_rows += len(data)
        for row in data:
            if not isinstance(row, dict):
                raise RuntimeError(f"港股通名单第 {page} 页包含无效记录: {row!r}")
            raw_code = row.get("zqdm")
            raw_cn = row.get("zqjc")
            raw_en = row.get("zqywjc")
            code = str(raw_code if raw_code is not None else "").strip()
            name_cn = str(raw_cn if raw_cn is not None else "").strip()
            name_en = str(raw_en if raw_en is not None else "").strip()
            if not code.isdigit() or len(code) > 5 or int(code) <= 0:
                skipped_invalid += 1
                skipped_reasons["代码无效"] = skipped_reasons.get("代码无效", 0) + 1
                continue
            padded = code.zfill(5)
            if not name_cn and not name_en:
                skipped_invalid += 1
                skipped_reasons["中英文名称均为空"] = skipped_reasons.get("中英文名称均为空", 0) + 1
                if padded not in skipped_codes_set:
                    skipped_codes_set.add(padded)
                    skipped_codes.append(padded)
                # Ensure ambiguous code cannot stay confirmed if an earlier row appeared valid
                codes.pop(padded, None)
                continue
            if name_cn:
                name = name_cn
            else:
                name = name_en
                missing_cn_name += 1
            if padded in codes or padded in skipped_codes_set:
                raise RuntimeError(f"港股通名单第 {page} 页出现重复代码: {padded}")
            codes[padded] = name
        if page >= expected_pages:
            break
        page += 1
        time.sleep(0.15)
    if not codes:
        raise RuntimeError(f"港股通名单全部记录无效（总 {total_rows} 条，跳过 {skipped_invalid} 条）")
    if len(codes) + skipped_invalid != total_rows:
        raise RuntimeError(
            f"港股通名单有效与跳过记录数({len(codes)}+{skipped_invalid})与总记录数({total_rows})不符"
        )
    if expected_records is not None and total_rows != expected_records:
        raise RuntimeError(f"港股通名单总记录数({total_rows})与 recordcount({expected_records})不符")
    return GGTListResult(
        codes,
        missing_cn_name=missing_cn_name,
        skipped_invalid=skipped_invalid,
        skipped_codes=skipped_codes,
        skipped_reasons=skipped_reasons,
    )


_QUOTE_DATE_RE = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})")
_SUSPENDED_STATUS_TOKENS = frozenset(
    {"S", "SUSPENDED", "SUSPEND", "H", "HALT", "HALTED", "TRADING_HALT", "停牌"}
)
_DELISTED_STATUS_TOKENS = frozenset(
    {"D", "DELIST", "DELISTED", "退市", "除牌"}
)
SUSPENDED_HISTORY_FILE = "suspended_history.json"


def _is_explicit_delisted_status(raw: Any) -> bool:
    if raw is None or isinstance(raw, bool):
        return False
    text = str(raw).strip()
    if not text or text.lower() in ("nan", "none", "0", "0.0", "0.00", "0.000", "n", "normal"):
        return False
    upper = text.upper()
    if upper in _DELISTED_STATUS_TOKENS:
        return True
    return ("退市" in text) or ("除牌" in text) or ("DELIST" in upper)


def _load_suspended_history_raw(
    st_dir: Path,
) -> tuple[dict[str, set[str]], dict[str, dict[str, str]]]:
    """Load ``(by_date, sources_by_date)`` from ``suspended_history.json``."""
    raw = load_json_safe(st_dir / SUSPENDED_HISTORY_FILE, {})
    if not isinstance(raw, dict):
        return {}, {}
    by_date = raw.get("by_date") if isinstance(raw.get("by_date"), dict) else raw
    out: dict[str, set[str]] = {}
    for d_k, codes_val in by_date.items():
        d_s = str(d_k).strip()[:10]
        try:
            datetime.strptime(d_s, "%Y-%m-%d")
        except ValueError:
            continue
        if isinstance(codes_val, list):
            out[d_s] = {
                str(c).strip().zfill(5) for c in codes_val if str(c).strip()
            }
    raw_sources = raw.get("sources")
    sources_out: dict[str, dict[str, str]] = {}
    if isinstance(raw_sources, dict):
        for d_k, src_map in raw_sources.items():
            d_s = str(d_k).strip()[:10]
            try:
                datetime.strptime(d_s, "%Y-%m-%d")
            except ValueError:
                continue
            if isinstance(src_map, dict):
                cleaned = {
                    str(c).strip().zfill(5): str(s).strip()
                    for c, s in src_map.items()
                    if str(c).strip() and str(s).strip()
                }
                if cleaned:
                    sources_out[d_s] = cleaned
    return out, sources_out


def load_suspended_history(st_dir: Path) -> dict[str, set[str]]:
    """Load per-trading-day confirmed suspended code records from ``suspended_history.json``."""
    by_date, _ = _load_suspended_history_raw(st_dir)
    return by_date


def record_suspended_for_date(
    st_dir: Path,
    trade_date: str,
    suspended_codes: Iterable[str],
    *,
    source: str = "hk_spot",
    merge: bool = False,
) -> dict[str, list[str]]:
    """Persist confirmed suspended codes for ``trade_date`` into ``suspended_history.json``."""
    d_s = str(trade_date).strip()[:10]
    datetime.strptime(d_s, "%Y-%m-%d")
    existing, existing_sources = _load_suspended_history_raw(st_dir)
    norm_codes = {
        str(c).strip().zfill(5) for c in suspended_codes if str(c).strip()
    }
    if merge:
        existing.setdefault(d_s, set()).update(norm_codes)
        if norm_codes:
            date_srcs = existing_sources.setdefault(d_s, {})
            for c in sorted(norm_codes):
                date_srcs[c] = source
    else:
        existing[d_s] = norm_codes
        if norm_codes:
            existing_sources[d_s] = {c: source for c in sorted(norm_codes)}
        else:
            existing_sources.pop(d_s, None)
    by_date_sorted = {d_k: sorted(codes) for d_k, codes in sorted(existing.items())}
    payload: dict[str, Any] = {
        "by_date": by_date_sorted,
        "updated": now_hk().isoformat(timespec="seconds"),
    }
    sources_sorted = {
        d_k: dict(sorted(s_map.items()))
        for d_k, s_map in sorted(existing_sources.items())
        if s_map
    }
    if sources_sorted:
        payload["sources"] = sources_sorted
    atomic_write_json(st_dir / SUSPENDED_HISTORY_FILE, payload, indent=1)
    return by_date_sorted


def _hk_yahoo_symbol(code: str) -> str:
    """Format an HK stock code (e.g. ``'00626'``) to Yahoo's 4-digit ``.HK`` ticker (``'0626.HK'``)."""
    digits = str(code).strip().lstrip("0") or "0"
    return f"{digits.zfill(4)}.HK"


def _normalize_yf_index_date(idx_val: Any) -> str | None:
    if isinstance(idx_val, datetime):
        dt_val = idx_val.astimezone(HK_TZ) if idx_val.tzinfo is not None else idx_val
        return dt_val.strftime("%Y-%m-%d")
    if isinstance(idx_val, date):
        return idx_val.isoformat()
    return _normalize_quote_date(idx_val)


def _extract_yahoo_daily_bars(
    df: Any,
) -> dict[str, tuple[float, float, float, float, float]]:
    """Extract ``{YYYY-MM-DD: (open, high, low, close, volume)}`` from a ``yfinance.Ticker.history()`` DataFrame.

    Rows with any missing/non-finite/non-positive OHLC price or missing/negative volume are skipped.
    """
    if df is None or getattr(df, "empty", True):
        return {}
    cols = getattr(df, "columns", None)
    required_cols = ("Open", "High", "Low", "Close", "Volume")
    if cols is None or any(col not in cols for col in required_cols):
        return {}
    bars: dict[str, tuple[float, float, float, float, float]] = {}
    for idx_val, row in df.iterrows():
        d_str = _normalize_yf_index_date(idx_val)
        if d_str is None:
            continue
        open_v = _finite_float_or_none(row["Open"])
        high_v = _finite_float_or_none(row["High"])
        low_v = _finite_float_or_none(row["Low"])
        close_v = _finite_float_or_none(row["Close"])
        vol_v = _finite_float_or_none(row["Volume"])
        if (
            open_v is None
            or open_v <= 0
            or high_v is None
            or high_v <= 0
            or low_v is None
            or low_v <= 0
            or close_v is None
            or close_v <= 0
            or vol_v is None
            or vol_v < 0
        ):
            continue
        bars[d_str] = (open_v, high_v, low_v, close_v, vol_v)
    return bars


def _existing_valid_close_dates(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    dates: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                d_str = str(row.get("date") or "").strip()[:10]
                if not d_str:
                    continue
                datetime.strptime(d_str, "%Y-%m-%d")
                px = _finite_float_or_none(row.get("close"))
                if px is not None and px > 0:
                    dates.add(d_str)
    except Exception:
        return set()
    return dates


def backfill_missing_suspensions(
    *,
    state_dir: str | Path | None = None,
    target_date: str | None = None,
    yf_module: Any = None,
    codes_by_date: dict[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Backfill per-day suspension records from Yahoo daily bars for missed trading days.

    Inspects constituents participating on each catch-up trading day (either supplied via
    ``codes_by_date`` from ``compute_all`` — including constituents newly added by a
    month-end rebalance inside the catch-up window — or falling back to saved held codes
    for trading days strictly between ``frozen_last_date`` and ``target_date``).
    A day is recorded in ``suspended_history.json`` (with source ``yahoo_daily``) ONLY when
    Yahoo daily (fetched with ``keepna=True, repair=False``) reports explicit ``Volume == 0``
    (not NaN) AND ``Open == High == Low == Close == previous_trading_day_close`` with all
    OHLC prices present; otherwise the day remains unrecorded and fails downstream checks.
    """
    st_dir = resolve_state_dir("hk_momentum", state_dir)
    target_str = str(target_date or _target_hk_trade_date()).strip()[:10]
    frozen = load_json_safe(st_dir / "frozen_history.json", {})
    if not isinstance(frozen, dict):
        frozen = {}

    dates_by_code: dict[str, set[str]] = {}
    if codes_by_date is not None:
        for d_raw, c_iter in codes_by_date.items():
            d_s = str(d_raw).strip()[:10]
            try:
                datetime.strptime(d_s, "%Y-%m-%d")
            except ValueError:
                continue
            if d_s >= target_str:
                continue
            for c in c_iter:
                c_norm = str(c).strip().zfill(5)
                if c_norm:
                    dates_by_code.setdefault(c_norm, set()).add(d_s)
    else:
        frozen_series = frozen.get("frozen_series")
        frozen_dates = frozen_series.get("dates") if isinstance(frozen_series, dict) else None
        if not isinstance(frozen_dates, list) or not frozen_dates:
            return {"checked": 0, "recorded": {}, "errors": []}
        frozen_last_date = str(frozen_dates[-1]).strip()[:10]
        gap_days = [
            d for d in trading_days_in_range("hk", frozen_last_date, target_str) if d < target_str
        ]
        if not gap_days:
            return {"checked": 0, "recorded": {}, "errors": []}
        held_codes = _load_held_codes(st_dir)
        if not held_codes:
            return {"checked": 0, "recorded": {}, "errors": []}
        for code in held_codes:
            dates_by_code[code] = set(gap_days)

    if not dates_by_code:
        return {"checked": 0, "recorded": {}, "errors": []}

    confirmed_by_date: dict[str, set[str]] = load_suspended_history(st_dir)
    raw_frozen_sbd = frozen.get("suspended_by_date")
    if isinstance(raw_frozen_sbd, dict):
        for d_k, c_list in raw_frozen_sbd.items():
            d_s = str(d_k).strip()[:10]
            if d_s and isinstance(c_list, list):
                confirmed_by_date.setdefault(d_s, set()).update(
                    str(c).strip().zfill(5) for c in c_list if str(c).strip()
                )

    hist_dir = st_dir / "hist"
    candidates: dict[str, list[str]] = {}
    for code in sorted(dates_by_code):
        bar_dates = _existing_valid_close_dates(hist_dir / f"{code}.csv")
        if not bar_dates:
            continue
        needed = sorted(
            d
            for d in dates_by_code[code]
            if d not in bar_dates and code not in confirmed_by_date.get(d, set())
        )
        if needed:
            candidates[code] = needed

    if not candidates:
        return {"checked": 0, "recorded": {}, "errors": []}

    if yf_module is None:
        import yfinance as yf

        yf_module = yf

    recorded: dict[str, list[str]] = {}
    errors: list[str] = []
    for code, needed_days in candidates.items():
        symbol = _hk_yahoo_symbol(code)
        first_prev = previous_trading_day("hk", date.fromisoformat(needed_days[0]))
        yf_start = (first_prev - timedelta(days=5)).isoformat()
        yf_end = (date.fromisoformat(needed_days[-1]) + timedelta(days=2)).isoformat()
        try:
            ticker = yf_module.Ticker(symbol)
            hist_df = ticker.history(
                start=yf_start,
                end=yf_end,
                interval="1d",
                auto_adjust=False,
                keepna=True,
                repair=False,
            )
        except Exception as exc:
            logger.warning("Yahoo daily backfill failed for %s (%s): %s", code, symbol, exc)
            errors.append(f"{code}({symbol}): {exc}")
            continue
        yahoo_bars = _extract_yahoo_daily_bars(hist_df)
        for d_chk in needed_days:
            prev_d = previous_trading_day("hk", date.fromisoformat(d_chk)).isoformat()
            cur_bar = yahoo_bars.get(d_chk)
            prev_bar = yahoo_bars.get(prev_d)
            if cur_bar is None or prev_bar is None:
                continue
            cur_open, cur_high, cur_low, cur_close, cur_vol = cur_bar
            _, _, _, prev_close, _ = prev_bar
            if cur_vol == 0.0 and all(
                math.isclose(px_val, prev_close, rel_tol=1e-9, abs_tol=1e-6)
                for px_val in (cur_open, cur_high, cur_low, cur_close)
            ):
                record_suspended_for_date(
                    st_dir, d_chk, [code], source="yahoo_daily", merge=True
                )
                recorded.setdefault(d_chk, []).append(code)
    return {"checked": len(candidates), "recorded": recorded, "errors": errors}


def _is_explicit_suspended_status(raw: Any) -> bool:
    if raw is None or isinstance(raw, bool):
        return False
    text = str(raw).strip()
    if not text or text.lower() in ("nan", "none", "0", "0.0", "0.00", "0.000", "n", "normal"):
        return False
    upper = text.upper()
    if upper in _SUSPENDED_STATUS_TOKENS:
        return True
    return ("停牌" in text) or ("SUSPEND" in upper) or ("HALT" in upper)


def _finite_float_or_none(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _normalize_quote_date(val: Any) -> str | None:
    if val is None or isinstance(val, bool):
        return None
    text = str(val).strip()
    if not text or text.lower() in ("nan", "none", "nat"):
        return None
    m = _QUOTE_DATE_RE.search(text)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def is_spot_suspended(
    spot_entry: dict[str, Any] | None,
    *,
    check_date: str | None = None,
) -> bool:
    """Shared credible suspension check used by both HK fetch and index computation.

    A security is treated as suspended ONLY when the current-run spot quote matches
    ``check_date`` (when provided) AND either:
    1. The quote source explicitly reports a suspended/halted trading status, OR
    2. Spot volume is explicitly ``0`` (NOT empty/None, with ``0`` or absent turnover) AND
       price equals previous close.
    Stored boolean ``suspended`` flags, missing bars, empty volumes, stale/old-date quotes,
    or missing quotes always return False.
    """
    if not isinstance(spot_entry, dict):
        return False
    q_date = _normalize_quote_date(spot_entry.get("quote_date")) or _extract_quote_date(
        spot_entry.get("ts")
    )
    if check_date is not None and q_date != check_date:
        return False
    if _is_explicit_suspended_status(spot_entry.get("trading_status")) or _is_explicit_suspended_status(
        spot_entry.get("status")
    ):
        return True
    px = _finite_float_or_none(spot_entry.get("price"))
    prev_close = _finite_float_or_none(spot_entry.get("prev_close"))
    vol = _finite_float_or_none(spot_entry.get("volume"))
    if px is None or prev_close is None or vol is None:
        return False
    if px <= 0 or prev_close <= 0 or vol != 0.0:
        return False
    turnover = _finite_float_or_none(spot_entry.get("turnover"))
    if turnover is not None and turnover != 0.0:
        return False
    return math.isclose(px, prev_close, rel_tol=1e-9, abs_tol=1e-6)


def spot_meta_state(uni_meta: Any, target_date: str) -> tuple[bool, bool]:
    """Return ``(can_use_csv_spot, can_use_meta_suspended_spot)`` for ``target_date``.

    When ``universe.csv`` was preserved from a prior run (``preserved_previous_universe=True``)
    or ``hk_spot`` degraded or ``target_trade_date`` does not match ``target_date``, callers
    MUST NOT read old suspension flags or price/volume columns from ``universe.csv``.
    """
    if not isinstance(uni_meta, dict) or not uni_meta:
        return True, False
    preserved_prev = bool(uni_meta.get("preserved_previous_universe"))
    meta_td = uni_meta.get("target_trade_date")
    date_matches = meta_td in (None, target_date)
    src_map = uni_meta.get("source_statuses")
    hk_spot_st = src_map.get("hk_spot") if isinstance(src_map, dict) else None
    if isinstance(hk_spot_st, dict):
        hk_spot_clean = bool(hk_spot_st.get("ok")) and (not bool(hk_spot_st.get("stale")))
    else:
        hk_spot_clean = not bool(uni_meta.get("stale"))
    can_use_csv_spot = (not preserved_prev) and hk_spot_clean and date_matches
    can_use_meta_suspended = hk_spot_clean and date_matches
    return can_use_csv_spot, can_use_meta_suspended


def _extract_tencent_trading_status(fields: list[str]) -> str:
    for idx in (40, 57):
        if len(fields) > idx and _is_explicit_suspended_status(fields[idx]):
            return str(fields[idx]).strip()
    return ""


def _extract_quote_date(ts_raw: Any) -> str | None:
    if not ts_raw:
        return None
    m = _QUOTE_DATE_RE.search(str(ts_raw))
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _target_hk_trade_date(
    as_of_date: date | str | None = None,
    *,
    now: datetime | None = None,
) -> str:
    cur = (now or now_hk()) if as_of_date is None else None
    return resolve_market_trade_date("hk", now=cur, as_of_date=as_of_date).isoformat()


def fetch_spot_batch(
    codes: list[str],
    *,
    session: Any = None,
    batch_size: int = 60,
    expected_date: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Tencent batch quotes. Returns ({code: quote_dict}, errors)."""
    s = session or _sess()
    out: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    seen_codes: set[str] = set()
    dup_codes: list[str] = []
    bad_date_codes: list[str] = []
    for i in range(0, len(codes), batch_size):
        chunk = codes[i : i + batch_size]
        q = ",".join(f"r_hk{c}" for c in chunk)
        try:
            r = s.get(f"https://qt.gtimg.cn/q={q}", timeout=15)
            r.encoding = "gbk"
            for line in r.text.strip().split(";"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                var, payload = line.split("=", 1)
                code = var.split("_hk")[-1].strip()
                if code in seen_codes:
                    dup_codes.append(code)
                    out.pop(code, None)
                    continue
                seen_codes.add(code)
                f = payload.strip('"').split("~")
                if len(f) < 46:
                    continue
                trading_status = _extract_tencent_trading_status(f)
                explicit_susp = _is_explicit_suspended_status(trading_status)
                if f[3] in ("", "0.000") and not explicit_susp:
                    continue
                try:
                    prev_close = float(f[4]) if f[4] else None
                    if prev_close is not None and not math.isfinite(prev_close):
                        continue
                    if f[3] in ("", "0.000") and explicit_susp:
                        px = prev_close if (prev_close is not None and prev_close > 0) else 0.0
                    else:
                        px = float(f[3])
                    if not explicit_susp and (not math.isfinite(px) or px <= 0):
                        continue
                    vol_vals: list[float] = []
                    bad_vol = False
                    for v_idx in (6, 36):
                        if len(f) > v_idx and f[v_idx].strip() != "":
                            v_num = float(f[v_idx])
                            if not math.isfinite(v_num):
                                bad_vol = True
                                break
                            vol_vals.append(v_num)
                    if bad_vol:
                        continue
                    volume: float | None = max(vol_vals) if vol_vals else None
                    if len(f) > 37 and f[37].strip() != "":
                        turnover: float | None = float(f[37])
                        if not math.isfinite(turnover):
                            continue
                    else:
                        turnover = None
                    mktcap = float(f[45]) if f[45] else None
                    pct = float(f[32]) if f[32] else 0.0
                    if not math.isfinite(pct) or (
                        mktcap is not None and not math.isfinite(mktcap)
                    ):
                        continue
                    q_date = _extract_quote_date(f[30])
                    entry: dict[str, Any] = {
                        "name_cn": f[1],
                        "price": px if px > 0 else None,
                        "prev_close": prev_close,
                        "volume": volume,
                        "turnover": turnover,
                        "mktcap_e8": mktcap,
                        "pct": pct,
                        "ts": f[30],
                        "quote_date": q_date,
                        "trading_status": trading_status,
                    }
                    if expected_date is not None and q_date != expected_date:
                        bad_date_codes.append(code)
                        continue
                    entry["suspended"] = is_spot_suspended(entry, check_date=expected_date)
                    out[code] = entry
                except (ValueError, IndexError):
                    continue
        except Exception as exc:
            errors.append(f"batch_{i}: {exc}")
        if i + batch_size < len(codes):
            time.sleep(0.12)
    if dup_codes:
        uniq_dups = sorted(set(dup_codes))
        errors.append(
            f"港股现货响应包含重复代码 ({len(uniq_dups)} 只，如 {', '.join(uniq_dups[:5])})"
        )
    if bad_date_codes:
        uniq_bad = sorted(set(bad_date_codes))
        errors.append(
            f"港股现货报价日期非目标交易日 {expected_date} ({len(uniq_bad)} 只，如 {', '.join(uniq_bad[:5])})"
        )
    return out, errors


def fetch_hsi_spot(*, session: Any = None) -> tuple[dict[str, Any] | None, str | None]:
    s = session or _sess()
    try:
        r = s.get("https://qt.gtimg.cn/q=r_hkHSI", timeout=10)
        r.encoding = "gbk"
        payload = r.text.split("=", 1)[-1].strip().strip('"; ')
        f = payload.split("~")
        return {
            "value": float(f[3]),
            "pct": float(f[32]),
            "ts": f[30] if len(f) > 30 else "",
        }, None
    except Exception as exc:
        return None, f"恒指实时报价失败: {exc}"


def _load_held_codes(st_dir: Path) -> set[str]:
    """Return active portfolio constituent codes from frozen_history.json / constituents.json."""
    held: set[str] = set()
    legacy_removed: set[str] = set()
    has_per_index_removals = False
    frozen = load_json_safe(st_dir / "frozen_history.json", {})
    if isinstance(frozen, dict):
        susp_removals = frozen.get("suspended_removals")
        if isinstance(susp_removals, dict) and susp_removals:
            has_per_index_removals = True
        else:
            raw_rem = frozen.get("removed_suspended_codes")
            if isinstance(raw_rem, list):
                legacy_removed.update(str(c).strip().zfill(5) for c in raw_rem if str(c).strip())
        rebals = frozen.get("rebalance_snapshots")
        if isinstance(rebals, dict) and rebals:
            last_rebal_date = sorted(rebals.keys())[-1]
            last_rebal = rebals.get(last_rebal_date)
            if isinstance(last_rebal, dict):
                for idx_key, codes in last_rebal.items():
                    if not isinstance(codes, list):
                        continue
                    idx_removed: set[str] = set()
                    if has_per_index_removals and isinstance(susp_removals, dict):
                        idx_map = susp_removals.get(idx_key)
                        if isinstance(idx_map, dict):
                            rem_for_rebal = idx_map.get(last_rebal_date)
                            if isinstance(rem_for_rebal, dict):
                                idx_removed = {
                                    str(c).strip().zfill(5)
                                    for c in rem_for_rebal.keys()
                                    if str(c).strip()
                                }
                    for c in codes:
                        if not c:
                            continue
                        norm_c = str(c).strip().zfill(5)
                        if norm_c not in idx_removed:
                            held.add(norm_c)
    cons = load_json_safe(st_dir / "constituents.json", {})
    if isinstance(cons, dict):
        for items in cons.values():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("code"):
                        held.add(str(item["code"]).strip().zfill(5))
    return held if has_per_index_removals else (held - legacy_removed)


def build_universe(
    *,
    state_dir: str | Path | None = None,
    proxy: str | None = None,
    session: Any = None,
    as_of_date: date | str | None = None,
):
    """Build HK universe + GGT list + spot prices; never overwrites prior files on any failure."""
    import pandas as pd

    st_dir = resolve_state_dir("hk_momentum", state_dir)
    meta_path = st_dir / "universe_meta.json"
    uni_csv = st_dir / "universe.csv"
    ggt_json = st_dir / "ggt_list.json"
    errors: list[str] = []
    source_statuses: dict[str, SourceStatus] = {}
    now_str = now_hk().strftime("%Y-%m-%d %H:%M:%S") + " 北京时间"
    target_trade_date = _target_hk_trade_date(as_of_date)

    held_codes = _load_held_codes(st_dir)
    uni: pd.DataFrame | None = None
    try:
        uni = fetch_hkex_universe(
            state_dir=st_dir, proxy=resolve_hkex_proxy(proxy), session=session
        )
        if uni is None or len(uni) == 0:
            raise RuntimeError("港交所证券列表为空")
        if "code" not in uni.columns or "name" not in uni.columns:
            raise RuntimeError("港交所证券列表缺少必要列")
        code_series = uni["code"].astype(str).str.strip()
        if (code_series == "").any() or code_series.duplicated().any():
            raise RuntimeError("港交所证券列表包含空代码或重复代码")
        if "subcat" in uni.columns and not uni["subcat"].astype(str).str.contains("Main Board", na=False).any():
            raise RuntimeError("港交所证券列表缺少主板股票记录")
        if len(held_codes) >= 10 and len(set(code_series) & held_codes) < int(0.8 * len(held_codes)):
            raise RuntimeError(
                f"港交所证券列表不完整（仅命中 {len(set(code_series) & held_codes)}/{len(held_codes)} 只持有成分）"
            )
        source_statuses["hkex_universe"] = make_source_result(
            "hkex_universe", ok=True, stale=False, as_of=now_str, label="港交所证券列表"
        )
    except Exception as exc:
        uni = None
        err = f"港交所证券列表拉取失败: {exc}"
        errors.append(err)
        source_statuses["hkex_universe"] = make_source_result(
            "hkex_universe", ok=False, stale=True, reason=err, label="港交所证券列表"
        )

    ggt: dict[str, str] | None = None
    ggt_missing_cn = 0
    ggt_skipped_invalid = 0
    ggt_skipped_codes: list[str] = []
    ggt_skipped_reasons: dict[str, int] = {}
    ggt_skip_note: str | None = None
    try:
        ggt = fetch_ggt_list(session=session)
        ggt_missing_cn = int(getattr(ggt, "missing_cn_name", 0) or 0)
        ggt_skipped_invalid = int(getattr(ggt, "skipped_invalid", 0) or 0)
        ggt_skipped_codes = sorted(set(getattr(ggt, "skipped_codes", ()) or ()))
        ggt_skipped_reasons = dict(getattr(ggt, "skipped_reasons", {}) or {})
        ggt_notes: list[str] = []
        if ggt_missing_cn > 0:
            ggt_notes.append(f"缺中文简称 {ggt_missing_cn} 只")
        if ggt_skipped_invalid > 0:
            ggt_skip_note = _format_ggt_skip_note(ggt_skipped_invalid, ggt_skipped_reasons)
            ggt_notes.append(ggt_skip_note)
        source_statuses["ggt_list"] = make_source_result(
            "ggt_list",
            ok=True,
            stale=False,
            as_of=now_str,
            label="深交所港股通名单",
            note="，".join(ggt_notes) if ggt_notes else None,
            counts={
                "valid": len(ggt),
                "missing_cn_name": ggt_missing_cn,
                "skipped_invalid": ggt_skipped_invalid,
            },
        )
    except Exception as exc:
        err = f"港股通名单拉取失败: {exc}"
        errors.append(err)
        source_statuses["ggt_list"] = make_source_result(
            "ggt_list", ok=False, stale=True, reason=err, label="深交所港股通名单"
        )

    if uni is None:
        uni = pd.read_csv(uni_csv, dtype={"code": str}) if uni_csv.is_file() else pd.DataFrame(
            columns=["code", "name", "subcat", "ccy", "is_ggt", "excluded", "price", "mktcap_e8", "name_cn"]
        )
    if ggt is None:
        old_ggt = load_json_safe(ggt_json, {}) or {}
        ggt = old_ggt if isinstance(old_ggt, dict) else {}

    uni["is_ggt"] = uni["code"].isin(ggt.keys())
    uni["excluded"] = uni["code"].isin(ggt_skipped_codes)
    codes_to_spot = sorted(set(uni["code"]) | held_codes)
    spot: dict[str, dict[str, Any]] = {}
    spot_errs: list[str] = []
    if codes_to_spot:
        spot, spot_errs = fetch_spot_batch(
            codes_to_spot, session=session, expected_date=target_trade_date
        )
        if spot_errs:
            errors.extend(spot_errs[:5])
    else:
        spot_errs = ["标的列表为空，无法拉取现货报价"]
        errors.extend(spot_errs)

    def _entry_quote_date(entry: dict[str, Any]) -> str | None:
        return _normalize_quote_date(entry.get("quote_date")) or _extract_quote_date(entry.get("ts"))

    wrong_date_codes = sorted(
        c for c, entry in spot.items() if _entry_quote_date(entry) != target_trade_date
    )
    fresh_spot = {
        c: entry for c, entry in spot.items() if _entry_quote_date(entry) == target_trade_date
    }
    suspended_spot = {
        c: entry
        for c, entry in fresh_spot.items()
        if is_spot_suspended(entry, check_date=target_trade_date)
    }
    uni["price"] = uni["code"].map(lambda c: fresh_spot.get(c, {}).get("price"))
    uni["prev_close"] = uni["code"].map(lambda c: fresh_spot.get(c, {}).get("prev_close"))
    uni["volume"] = uni["code"].map(lambda c: fresh_spot.get(c, {}).get("volume"))
    uni["turnover"] = uni["code"].map(lambda c: fresh_spot.get(c, {}).get("turnover"))
    uni["trading_status"] = uni["code"].map(lambda c: fresh_spot.get(c, {}).get("trading_status", ""))
    uni["suspended"] = uni["code"].map(lambda c: c in suspended_spot)
    uni["quote_date"] = uni["code"].map(lambda c: _entry_quote_date(fresh_spot.get(c, {})))
    uni["mktcap_e8"] = uni["code"].map(lambda c: fresh_spot.get(c, {}).get("mktcap_e8"))
    uni["name_cn"] = uni["code"].map(lambda c: fresh_spot.get(c, {}).get("name_cn"))

    n_equity = int(len(uni))
    n_quoted = int(uni["price"].notna().sum()) if "price" in uni and len(uni) else 0
    spot_reasons = list(spot_errs[:5]) if spot_errs else []
    if wrong_date_codes and not any("日期非目标交易日" in msg for msg in spot_reasons):
        date_err = (
            f"港股现货报价日期非目标交易日 {target_trade_date} "
            f"({len(wrong_date_codes)} 只，如 {', '.join(wrong_date_codes[:5])})"
        )
        errors.append(date_err)
        spot_reasons.append(date_err)
    if n_equity == 0 or n_quoted == 0 or (n_quoted * 2 < n_equity):
        cov_err = f"港股现货报价覆盖不足 ({n_quoted}/{n_equity})"
        errors.append(cov_err)
        spot_reasons.append(cov_err)

    missing_held_spot = sorted(
        c
        for c in held_codes
        if c not in suspended_spot
        and not (
            isinstance(fresh_spot.get(c, {}).get("price"), (int, float))
            and not isinstance(fresh_spot.get(c, {}).get("price"), bool)
            and math.isfinite(float(fresh_spot[c]["price"]))
            and float(fresh_spot[c]["price"]) > 0
        )
    )
    if missing_held_spot:
        sample = ", ".join(missing_held_spot[:5])
        held_err = f"已持有成分缺少现货报价 {len(missing_held_spot)} 只 (如 {sample})"
        errors.append(held_err)
        spot_reasons.append(held_err)

    if spot_reasons:
        source_statuses["hk_spot"] = make_source_result(
            "hk_spot", ok=False, stale=True, reason="；".join(spot_reasons), label="港股现货行情"
        )
    else:
        source_statuses["hk_spot"] = make_source_result(
            "hk_spot", ok=True, stale=False, as_of=target_trade_date, label="港股现货行情"
        )

    hk_spot_clean = source_statuses["hk_spot"].is_clean
    trusted_suspended_spot = suspended_spot if hk_spot_clean else {}
    if hk_spot_clean:
        record_suspended_for_date(st_dir, target_trade_date, trusted_suspended_spot.keys())
    degraded = any(not st.is_clean for st in source_statuses.values())
    if not degraded:
        uni.to_csv(uni_csv, index=False)
        atomic_write_json(ggt_json, ggt)
    elif uni_csv.is_file():
        # Preserve prior clean universe.csv & ggt_list.json for caller consistency
        uni = pd.read_csv(uni_csv, dtype={"code": str})
    meta = {
        "updated": now_hk().isoformat(timespec="seconds"),
        "target_trade_date": target_trade_date,
        "n_equity": n_equity,
        "n_ggt": int(uni["is_ggt"].sum()) if "is_ggt" in uni else 0,
        "ggt_missing_cn_name": ggt_missing_cn,
        "ggt_skipped_invalid": ggt_skipped_invalid,
        "ggt_skipped_codes": ggt_skipped_codes,
        "ggt_skipped_reasons": ggt_skipped_reasons,
        "ggt_skipped_note": ggt_skip_note,
        "excluded_codes": ggt_skipped_codes,
        "suspended_codes": sorted(trusted_suspended_spot.keys()),
        "suspended_spot": {
            c: {
                "price": e.get("price"),
                "prev_close": e.get("prev_close"),
                "volume": e.get("volume"),
                "turnover": e.get("turnover"),
                "trading_status": e.get("trading_status", ""),
                "quote_date": e.get("quote_date"),
                "ts": e.get("ts"),
            }
            for c, e in trusted_suspended_spot.items()
        },
        "n_quoted": n_quoted,
        "errors": errors,
        "stale": degraded,
        "preserved_previous_universe": bool(degraded and uni_csv.is_file()),
        "source_statuses": {k: v.to_dict() for k, v in source_statuses.items()},
    }
    atomic_write_json(meta_path, meta)
    return uni, meta


class HistBars(list):
    """Validated bars from HIST_START on, plus the upstream window they came from.

    ``raw_count`` is how many bars the response held and ``first_date`` the
    earliest valid date in it, before HIST_START included: whether the window
    rolled stored dates off is judged on the response, not on the kept bars.
    """

    raw_count: int = 0
    first_date: str | None = None
    all_before_start: bool = False


class NoAdjustedHistory(RuntimeError):
    """Tencent has only unadjusted bars for the code (typically delisted)."""

    def __init__(self, code: str) -> None:
        super().__init__(f"历史行情无前复权数据 ({code})")
        self.code = code


def fetch_hist_one(
    sess: Any,
    code: str,
    *,
    start: str = HIST_START,
    end: str | None = None,
    count: int = HIST_BARS,
) -> list[list[Any]]:
    end = end or now_hk().date().isoformat()
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?"
        f"param=hk{code},day,{start},{end},{count},qfq"
    )
    r = sess.get(url, timeout=20)
    j = r.json()
    d = j.get("data", {}).get(f"hk{code}", {})
    # Forward-adjusted bars only: falling back to the plain "day" series would
    # mix an unadjusted basis into a file that is rewritten whole.
    rows = d.get("qfqday") or []
    if not rows:
        if d.get("day"):
            raise NoAdjustedHistory(code)
        raise RuntimeError(f"历史行情返回空列表 ({code})")
    out = HistBars()
    out.raw_count = len(rows)
    seen_dates: set[str] = set()
    bad_rows = 0
    valid_before_start = False
    for row in rows:
        try:
            d_str = str(row[0]).strip()
            datetime.strptime(d_str, "%Y-%m-%d")
            if d_str in seen_dates:
                bad_rows += 1
                continue
            seen_dates.add(d_str)
            if out.first_date is None or d_str < out.first_date:
                out.first_date = d_str
            if d_str < HIST_START:
                # Bars before HIST_START are never written; a bad one there
                # must not sink the bars that are kept.
                if not valid_before_start:
                    try:
                        close = float(row[2])
                        valid_before_start = math.isfinite(close) and close > 0
                    except (ValueError, IndexError, TypeError):
                        pass
                continue
            o, c, h, lo, vol = float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
            if (
                not math.isfinite(o)
                or not math.isfinite(c)
                or c <= 0
                or not math.isfinite(h)
                or not math.isfinite(lo)
                or not math.isfinite(vol)
            ):
                bad_rows += 1
                continue
            rec: list[Any] = [d_str, o, c, h, lo, vol]
            turn: float | None = None
            for x in row[6:]:
                if isinstance(x, str):
                    try:
                        turn = float(x)
                    except ValueError:
                        pass
            rec.append(turn if turn is not None else 0.0)
            out.append(rec)
        except (ValueError, IndexError, TypeError):
            bad_rows += 1
            continue
    # A listing whose valid bars all predate HIST_START is not an error: it
    # simply has nothing in the panel's range.
    if bad_rows > 0 or (not out and not valid_before_start):
        raise RuntimeError(
            f"历史行情记录不完整或含重复/无效日期 ({code}: total={len(rows)}, valid={len(out)}, bad={bad_rows})"
        )
    out.all_before_start = bool((not out) and valid_before_start)
    return out


def _load_spot_info_for_date(
    st_dir: Path,
    target_date: str,
    spot_override: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load current-date spot evidence from universe_meta.json / universe.csv for suspension checks."""
    out: dict[str, dict[str, Any]] = {}
    uni_meta = load_json_safe(st_dir / "universe_meta.json", {})
    can_use_csv_spot, can_use_meta_suspended = spot_meta_state(uni_meta, target_date)
    uni_csv = st_dir / "universe.csv"
    if can_use_csv_spot and uni_csv.is_file():
        try:
            with uni_csv.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    c_raw = str(row.get("code") or "").strip()
                    if not c_raw:
                        continue
                    c_norm = c_raw.zfill(5)
                    ent: dict[str, Any] = {}
                    for k in ("price", "prev_close", "volume", "turnover"):
                        v_raw = row.get(k)
                        if v_raw is not None and str(v_raw).strip() != "":
                            ent[k] = _finite_float_or_none(v_raw)
                    for k in ("trading_status", "status", "quote_date", "ts"):
                        v_raw = row.get(k)
                        if v_raw is not None and str(v_raw).strip() not in ("", "nan", "None"):
                            ent[k] = str(v_raw).strip()
                    out[c_norm] = ent
        except Exception:
            pass
    if can_use_meta_suspended and isinstance(uni_meta, dict) and isinstance(uni_meta.get("suspended_spot"), dict):
        for scode, sentry in uni_meta["suspended_spot"].items():
            if not isinstance(sentry, dict):
                continue
            c_norm = str(scode).strip().zfill(5)
            if not c_norm:
                continue
            merged = dict(out.get(c_norm) or {})
            merged.update(sentry)
            out[c_norm] = merged
    if isinstance(spot_override, dict):
        for scode, sentry in spot_override.items():
            if not isinstance(sentry, dict):
                continue
            c_norm = str(scode).strip().zfill(5)
            if not c_norm:
                continue
            merged = dict(out.get(c_norm) or {})
            merged.update(sentry)
            out[c_norm] = merged
    return out


def _existing_csv_dates(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    dates: set[str] = set()
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        for ln in lines[1:]:
            d_str = ln.split(",", 1)[0].strip()
            datetime.strptime(d_str, "%Y-%m-%d")
            dates.add(d_str)
    except Exception:
        return set()
    return dates


def _load_clean_hkex_listed_codes(st_dir: Path, target_date: str) -> set[str] | None:
    """Return currently listed HKEX equity codes when today's ``hkex_universe`` is clean.

    Returns ``None`` if ``universe_meta.json`` is missing, stale/preserved, for another
    date, or ``hkex_universe`` failed — callers MUST NOT infer delisting without clean
    current-day HKEX listing evidence.
    """
    uni_meta = load_json_safe(st_dir / "universe_meta.json", {})
    if not isinstance(uni_meta, dict) or not uni_meta:
        return None
    if bool(uni_meta.get("preserved_previous_universe")):
        return None
    if uni_meta.get("target_trade_date") != target_date:
        return None
    src_map = uni_meta.get("source_statuses")
    hkex_st = src_map.get("hkex_universe") if isinstance(src_map, dict) else None
    if not isinstance(hkex_st, dict) or not (bool(hkex_st.get("ok")) and not bool(hkex_st.get("stale"))):
        return None
    uni_csv = st_dir / "universe.csv"
    if not uni_csv.is_file():
        return None
    listed: set[str] = set()
    try:
        with uni_csv.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                c_raw = str(row.get("code") or "").strip()
                if c_raw:
                    listed.add(c_raw.zfill(5))
    except Exception:
        return None
    return listed if listed else None


def _is_confirmed_delisted(
    code_norm: str,
    *,
    spot_entry: dict[str, Any] | None,
    check_date: str,
    delisted_set: set[str],
    clean_hkex_listed: set[str] | None,
) -> bool:
    """True iff delisting is independently confirmed (explicit set, spot status, or clean HKEX list)."""
    if code_norm in delisted_set:
        return True
    if isinstance(spot_entry, dict):
        q_date = _normalize_quote_date(spot_entry.get("quote_date")) or _extract_quote_date(
            spot_entry.get("ts")
        )
        if q_date == check_date and (
            _is_explicit_delisted_status(spot_entry.get("trading_status"))
            or _is_explicit_delisted_status(spot_entry.get("status"))
        ):
            return True
    if clean_hkex_listed is not None and code_norm not in clean_hkex_listed:
        return True
    return False


def download_history(
    codes: list[str],
    *,
    state_dir: str | Path | None = None,
    full: bool = False,
    workers: int = 8,
    session: Any = None,
    target_date: str | None = None,
    held_codes: Iterable[str] | None = None,
    spot_info: dict[str, dict[str, Any]] | None = None,
    delisted_codes: Iterable[str] | None = None,
) -> dict[str, Any]:
    st_dir = resolve_state_dir("hk_momentum", state_dir)
    hist_dir = st_dir / "hist"
    hist_dir.mkdir(parents=True, exist_ok=True)
    today = target_date or _target_hk_trade_date()
    spot_map = _load_spot_info_for_date(st_dir, today, spot_override=spot_info)
    delisted_set = {str(c).strip().zfill(5) for c in (delisted_codes or ()) if str(c).strip()}
    clean_hkex_listed = _load_clean_hkex_listed_codes(st_dir, today)

    todo: list[str] = []
    for c in codes:
        p = hist_dir / f"{c}.csv"
        if full or not p.is_file():
            todo.append(c)
        else:
            try:
                lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
                if len(lines) <= 1:
                    todo.append(c)
                    continue
                last = lines[-1].split(",")[0].strip()
                if last < today or (last > today and today not in _existing_csv_dates(p)):
                    todo.append(c)
            except Exception:
                todo.append(c)

    errs: list[str] = []
    done = 0

    def _work(code: str) -> bool:
        s = session or _sess()
        p = hist_dir / f"{code}.csv"
        prev_dates = _existing_csv_dates(p)
        code_norm = str(code).strip().zfill(5)
        for attempt in range(3):
            try:
                try:
                    rows = fetch_hist_one(s, code)
                except NoAdjustedHistory:
                    spot_entry = spot_map.get(code_norm) or spot_map.get(code)
                    if prev_dates and (
                        is_spot_suspended(spot_entry, check_date=today)
                        or _is_confirmed_delisted(
                            code_norm,
                            spot_entry=spot_entry,
                            check_date=today,
                            delisted_set=delisted_set,
                            clean_hkex_listed=clean_hkex_listed,
                        )
                    ):
                        # Independently confirmed delisted or suspended on target_date:
                        # preserve the stored adjusted file without mixing unadjusted bars.
                        return True
                    # Unconfirmed missing qfqday: keep stored file untouched, but count
                    # as download failure (participating in 3% threshold & held guard).
                    raise
                new_dates = [r[0] for r in rows]
                new_date_set = set(new_dates)
                if len(new_date_set) != len(rows):
                    raise RuntimeError(f"历史响应含重复日期 ({code})")
                first = getattr(rows, "first_date", None) or (min(new_dates) if new_dates else None)
                if prev_dates and first:
                    # Tencent returns the latest HIST_BARS bars. Only a full
                    # window can roll older stored dates off; a short response
                    # owes every stored date. Either way the response must
                    # overlap what is stored.
                    window_start = first
                    if window_start > max(prev_dates):
                        raise RuntimeError(f"历史响应与已有记录不重叠 ({code})")
                    raw_count = getattr(rows, "raw_count", len(rows))
                    kept = {d for d in prev_dates if d >= HIST_START}
                    owed = kept if raw_count < HIST_BARS else {d for d in kept if d >= window_start}
                    missing_old = owed - new_date_set
                    if missing_old:
                        raise RuntimeError(
                            f"历史响应丢失原有日期 ({code}: 缺失 {len(missing_old)} 天)"
                        )
                if not getattr(rows, "all_before_start", False):
                    last_bar_date = max(new_dates) if new_dates else None
                    if last_bar_date != today:
                        spot_entry = spot_map.get(code_norm) or spot_map.get(code)
                        if not is_spot_suspended(spot_entry, check_date=today):
                            raise RuntimeError(
                                f"历史行情未到达目标交易日 {today} ({code}: 最新 {last_bar_date or '无'})"
                            )
                if first and getattr(rows, "raw_count", len(rows)) >= HIST_BARS and first > HIST_START:
                    # Only a full window that no longer reaches HIST_START needs
                    # attention; a short history is just a later listing.
                    logger.warning(
                        "hk_momentum history window starts %s, after HIST_START %s (%s)",
                        first, HIST_START, code,
                    )
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["date", "open", "close", "high", "low", "volume", "turnover_wan"])
                # Keep the panel's start at HIST_START: earlier bars would move
                # the first rebalance before the HSI history the index needs.
                w.writerows([r for r in rows if r[0] >= HIST_START])
                atomic_write_text(p, buf.getvalue())
                return True
            except Exception:
                time.sleep(0.2 + attempt * 0.2)
        errs.append(code)
        return False

    if todo:
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            for ok in ex.map(_work, todo):
                if ok:
                    done += 1

    total_universe = max(len(codes), len(todo))
    fail_ratio = (len(errs) / total_universe) if total_universe > 0 else (1.0 if errs else 0.0)
    held_set = (
        {str(c).strip().zfill(5) for c in held_codes if str(c).strip()}
        if held_codes is not None
        else _load_held_codes(st_dir)
    )
    err_norm = {str(c).strip().zfill(5) for c in errs if str(c).strip()}
    failed_held = sorted(err_norm & held_set)

    if not errs:
        hist_st = make_source_result(
            "hk_history",
            ok=True,
            stale=False,
            as_of=today,
            label="港股历史日线",
            counts={"failed": 0, "total": total_universe, "todo": len(todo)},
        )
    elif fail_ratio <= MAX_HIST_FAIL_RATIO and not failed_held:
        partial_note = (
            f"历史日线下载失败 {len(errs)}/{total_universe} 只"
            f"（{fail_ratio * 100:.1f}% ≤ 3%，均非当前持有成分）"
        )
        hist_st = make_source_result(
            "hk_history",
            ok=True,
            stale=False,
            as_of=today,
            label="港股历史日线",
            note=partial_note,
            counts={
                "failed": len(errs),
                "total": total_universe,
                "todo": len(todo),
                "failed_held": 0,
            },
        )
    else:
        if failed_held:
            reason = (
                f"历史日线下载失败 {len(errs)}/{total_universe} 只"
                f"（含当前持有成分 {len(failed_held)} 只，如 {', '.join(failed_held[:5])}）"
            )
        else:
            reason = (
                f"历史日线下载失败 {len(errs)}/{total_universe} 只"
                f"（{fail_ratio * 100:.1f}%，超过 3% 阈值）"
            )
        hist_st = make_source_result(
            "hk_history",
            ok=False,
            stale=True,
            reason=reason,
            label="港股历史日线",
            counts={
                "failed": len(errs),
                "total": total_universe,
                "todo": len(todo),
                "failed_held": len(failed_held),
            },
        )
    progress = {
        "done": done,
        "total": len(todo),
        "total_codes": total_universe,
        "errors": errs[:50],
        "n_errors": len(errs),
        "fail_ratio": round(fail_ratio, 6),
        "failed_held": failed_held,
        "stale": not hist_st.is_clean,
        "note": hist_st.note,
        "source_status": hist_st.to_dict(),
        "ts": now_hk().astimezone(HK_TZ).isoformat(timespec="seconds"),
        "finished": True,
    }
    atomic_write_json(st_dir / "hist_progress.json", progress)
    return progress


def main() -> int:
    import pandas as pd

    parser = argparse.ArgumentParser(description="Fetch HKEX universe, GGT list, and HK qfq history.")
    parser.add_argument("cmd", nargs="?", default="all", choices=("universe", "history", "all"))
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    st_dir = resolve_state_dir("hk_momentum", args.state_dir)
    if args.cmd in ("universe", "all"):
        uni, _ = build_universe(state_dir=st_dir, proxy=args.proxy)
    else:
        uni_path = st_dir / "universe.csv"
        if not uni_path.is_file():
            raise RuntimeError(f"缺少成分文件 {uni_path}，请先运行 universe 抓取生成成分名单")
        uni = pd.read_csv(uni_path, dtype={"code": str})
    if args.cmd in ("history", "all"):
        codes = list(uni.loc[uni["price"].notna(), "code"]) if "price" in uni else list(uni["code"])
        res = download_history(codes, state_dir=st_dir, full=args.full)
        backfill_missing_suspensions(state_dir=st_dir)
        print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
