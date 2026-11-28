#!/usr/bin/env python3
"""PM Market State & Sector Rotation — L0 data collection (US yfinance + CN akshare)."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import time
from typing import Any

import pandas as pd

from scripts.dalton.monitor_feeds.common import (
    CN_TZ,
    US_TZ,
    SourceStatus,
    SourceStatusStore,
    atomic_write_json,
    completed_trading_weeks,
    inspect_trading_day,
    is_trading_day,
    load_json_safe,
    make_source_result,
    now_cn,
    now_us,
    pm_required_sources,
    resolve_market_trade_date,
    resolve_state_dir,
)

HERE = Path(__file__).resolve().parent
DEFAULT_CFG_PATH = HERE / "config.json"
SW_INCEPTION_PATH = HERE / "sw_index_inception.json"
_SW_INCEPTION_CACHE: dict[str, tuple[int, dict[str, str], dict[str, str]]] = {}


def load_config(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or DEFAULT_CFG_PATH).read_text(encoding="utf-8"))


def us_tickers(cfg: dict[str, Any]) -> list[str]:
    tickers: set[str] = set()
    for comp in cfg["l1_us"]["components"]:
        tickers.add(comp["num"])
        if comp.get("den"):
            tickers.add(comp["den"])
    tickers.add(cfg["l1_us"]["benchmark"])
    tickers.add(cfg["l2_us"]["benchmark"])
    for group in cfg["l2_us"]["groups"].values():
        tickers.update(group.keys())
    return sorted(tickers)


def _merge_csv(path: Path, new_cols: dict[str, dict[str, float]]) -> pd.DataFrame | None:
    if not new_cols:
        return None
    df_new = pd.DataFrame(new_cols)
    df_new.index = pd.to_datetime(df_new.index)
    if path.exists():
        old = pd.read_csv(path, index_col=0, parse_dates=True)
        df_new = df_new.combine_first(old)
    df_new = df_new.sort_index()
    df_new.index.name = "date"
    df_new.to_csv(path)
    return df_new


def fetch_us(
    cfg: dict[str, Any],
    *,
    raw_dir: Path,
    years: int = 3,
    yf_module: Any = None,
    now: datetime | None = None,
    as_of_date: date | str | None = None,
) -> dict[str, Any]:
    """Fetch US ETF/index Adj Close prices via yfinance (auto_adjust=False)."""
    if yf_module is None:
        import yfinance as yf

        yf_module = yf

    raw_dir.mkdir(parents=True, exist_ok=True)
    tk = us_tickers(cfg)
    cur_us = (now or now_us()).astimezone(US_TZ)
    ref_day = (
        date.fromisoformat(as_of_date)
        if isinstance(as_of_date, str)
        else (as_of_date or cur_us.date())
    )
    us_trading_today, us_cal_src, us_cal_note = inspect_trading_day("us", ref_day)
    target_day = resolve_market_trade_date("us", now=cur_us, as_of_date=as_of_date)
    target_str = target_day.isoformat()
    start = (cur_us - timedelta(days=int(365.25 * years))).strftime("%Y-%m-%d")
    df = yf_module.download(
        tk,
        start=start,
        auto_adjust=False,
        progress=False,
        group_by="column",
        threads=True,
    )
    if df is None or len(df) == 0:
        raise RuntimeError("yfinance 未返回有效美股行情")

    if isinstance(df.columns, pd.MultiIndex):
        field = "Adj Close" if "Adj Close" in df.columns.get_level_values(0) else "Close"
        px = df[field].copy()
    else:
        col = "Adj Close" if "Adj Close" in df.columns else "Close"
        px = df[[col]].rename(columns={col: tk[0]})
    px = px.dropna(how="all")
    if px.empty:
        raise RuntimeError("美股行情清洗后为空")

    px.index = pd.to_datetime(px.index)
    px = px.sort_index()
    # On US exchange holidays (e.g., Thanksgiving), futures such as HG=F/GC=F may still
    # produce a row with all equity ETFs NaN. Trim dates beyond the latest XNYS session
    # and drop any holiday-only partial rows.
    px = px[px.index.date <= target_day]
    present_tk = [c for c in tk if c in px.columns]
    if present_tk and not px.empty:
        holiday_partial = pd.Series(
            [
                (not is_trading_day("us", ts.date())) and bool(px.loc[ts, present_tk].isna().any())
                for ts in px.index
            ],
            index=px.index,
        )
        if holiday_partial.any():
            px = px[~holiday_partial]
    if px.empty:
        raise RuntimeError("美股行情清洗后为空")

    insufficient: list[str] = []
    stale_tickers: list[str] = []
    for c in tk:
        if c not in px.columns:
            insufficient.append(c)
            continue
        col_valid = pd.to_numeric(px[c], errors="coerce").dropna()
        col_valid = col_valid[col_valid.map(lambda v: math.isfinite(float(v)))]
        if len(col_valid) < 60:
            insufficient.append(c)
            continue
        c_last_date = str(col_valid.index[-1].date())
        if c_last_date != target_str:
            stale_tickers.append(c)

    px.index.name = "date"
    out_csv = raw_dir / "us_prices.csv"
    px.to_csv(out_csv)
    last_date = str(px.index[-1].date())
    err_parts: list[str] = []
    if insufficient:
        err_parts.append(f"部分标的样本不足: {', '.join(insufficient)}")
    if stale_tickers:
        err_parts.append(
            f"美股行情未到达目标交易日 {target_str}（最新 {last_date}，滞后标的 {len(stale_tickers)} 只: {', '.join(stale_tickers[:5])}）"
        )
    reason = "；".join(err_parts) if err_parts else None
    is_clean = not bool(err_parts)
    notes: list[str] = []
    if not us_trading_today and is_clean:
        notes.append(f"美股休市（{ref_day.isoformat()}），沿用最近交易日 {last_date} 数据")
    if us_cal_src == "weekday_fallback" and us_cal_note:
        notes.append(us_cal_note)
    combined_note = "；".join(notes) if notes else None
    src_status = make_source_result(
        "us",
        ok=is_clean,
        stale=not is_clean,
        reason=reason,
        as_of=last_date,
        label="美股行情",
        note=combined_note,
    )
    return {
        "ok": src_status.is_clean,
        "stale": src_status.stale,
        "rows": int(px.shape[0]),
        "cols": int(px.shape[1]),
        "last_date": last_date,
        "target_trade_date": target_str,
        "is_trading_day": us_trading_today,
        "insufficient": insufficient,
        "stale_tickers": stale_tickers,
        "error": reason,
        "note": combined_note,
        "calendar_note": us_cal_note if us_cal_src == "weekday_fallback" else None,
        "source_status": src_status.to_dict(),
    }


def _retry(fn, tag: str, errors: list[str], n: int = 4, wait: float = 2.0):
    last: Exception | None = None
    for i in range(n):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if i + 1 < n:
                time.sleep(wait * (i + 1))
    errors.append(f"{tag}: {last}")
    return None


def _normalize_iso_date(raw: Any) -> str | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    text = str(raw).strip()
    if not text or text.lower() in ("nan", "none", "nat"):
        return None
    cand = text[:10].replace("/", "-")
    try:
        return date.fromisoformat(cand).isoformat()
    except ValueError:
        return None


def _normalize_sw_industry_name(raw: Any) -> str:
    text = str(raw or "").strip()
    for suffix in ("Ⅱ", "II", "Ⅰ", "I"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)].strip()
            break
    return text


def _extract_inception_effective_date(entry: Any) -> str | None:
    if isinstance(entry, str):
        return _normalize_iso_date(entry)
    if not isinstance(entry, dict):
        return None
    val_start = _normalize_iso_date(entry.get("valuation_start"))
    inc_dt = _normalize_iso_date(
        entry.get("inception_date") or entry.get("publish_date") or entry.get("start_date")
    )
    if val_start and inc_dt:
        return max(val_start, inc_dt)
    return val_start or inc_dt


def load_sw_index_inception(
    path: str | Path | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Load verified Shenwan industry index inception/valuation-start dates from JSON.

    Returns ``(by_code, by_normalized_name)`` mapping to ISO ``YYYY-MM-DD`` dates.
    """
    env_override = os.environ.get("DALTON_SW_INCEPTION_PATH", "").strip()
    target = (
        Path(path).expanduser()
        if path is not None and str(path).strip()
        else (Path(env_override).expanduser() if env_override else SW_INCEPTION_PATH)
    )
    key = str(target)
    try:
        st = target.stat()
    except OSError:
        _SW_INCEPTION_CACHE.pop(key, None)
        return {}, {}

    cached = _SW_INCEPTION_CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime_ns:
        return cached[1], cached[2]

    raw = load_json_safe(target, {})
    by_code: dict[str, str] = {}
    by_name: dict[str, str] = {}
    if isinstance(raw, dict):
        raw_codes = raw.get("by_code")
        if isinstance(raw_codes, dict):
            for code_k, entry in raw_codes.items():
                c_clean = str(code_k).split(".")[0].strip()
                dt = _extract_inception_effective_date(entry)
                if c_clean and dt:
                    by_code[c_clean] = dt
                if isinstance(entry, dict) and entry.get("name") and dt:
                    nm_norm = _normalize_sw_industry_name(entry["name"])
                    if nm_norm and nm_norm not in by_name:
                        by_name[nm_norm] = dt
        raw_names = raw.get("by_name")
        if isinstance(raw_names, dict):
            for nm_k, entry in raw_names.items():
                nm_norm = _normalize_sw_industry_name(nm_k)
                dt = _extract_inception_effective_date(entry)
                if nm_norm and dt:
                    by_name[nm_norm] = dt
    _SW_INCEPTION_CACHE[key] = (st.st_mtime_ns, by_code, by_name)
    return by_code, by_name


def _lookup_sw_inception_date(
    code: str,
    name: str | None,
    *,
    by_code: dict[str, str],
    by_name: dict[str, str],
) -> str | None:
    c_clean = str(code).split(".")[0].strip()
    if c_clean in by_code:
        return by_code[c_clean]
    if name:
        nm_norm = _normalize_sw_industry_name(name)
        if nm_norm in by_name:
            return by_name[nm_norm]
    return None


def _extract_meta_publish_date(obj: Any) -> str | None:
    if obj is None:
        return None
    getter = obj.get if hasattr(obj, "get") else None
    if getter is None:
        return None
    for field in ("发布日期", "valuation_start", "inception_date", "publish_date", "start_date"):
        val = getter(field)
        norm = _normalize_iso_date(val)
        if norm is not None:
            return norm
    return None


def fetch_cn(
    cfg: dict[str, Any],
    *,
    raw_dir: Path,
    years: int = 3,
    ak_module: Any = None,
    progress_fn: Any = None,
    now: datetime | None = None,
    as_of_date: date | str | None = None,
) -> dict[str, Any]:
    """Fetch Shenwan L1/L2 index daily close & turnover + CSI 300 benchmark via akshare."""
    if ak_module is None:
        import akshare as ak

        ak_module = ak

    raw_dir.mkdir(parents=True, exist_ok=True)
    cn_cfg = cfg["l2_cn"]
    cur_cn = (now or now_cn()).astimezone(CN_TZ)
    ref_day = (
        date.fromisoformat(as_of_date)
        if isinstance(as_of_date, str)
        else (as_of_date or cur_cn.date())
    )
    cn_trading_today, cn_cal_src, cn_cal_note = inspect_trading_day("cn", ref_day)
    target_cn_day = resolve_market_trade_date("cn", now=cur_cn, as_of_date=as_of_date)
    target_cn_str = target_cn_day.isoformat()
    start_dt = cur_cn - timedelta(days=int(365.25 * years))
    start = start_dt.strftime("%Y-%m-%d")
    levels = cn_cfg.get("levels", ["first", "second"])
    min_cons = int(cn_cfg.get("min_constituents", 5))
    errors: list[str] = []

    bench_code = str(cn_cfg.get("benchmark_code") or "000300").strip()
    bench_sym = bench_code if bench_code.startswith(("sh", "sz")) else f"sh{bench_code}"
    bench_map: dict[str, float] | None = None
    lagging_bench_map: dict[str, float] | None = None
    bench_source: str | None = None
    bench_errors: list[str] = []
    for tag, fn in (
        (
            "bench_tx",
            lambda: ak_module.stock_zh_index_daily_tx(symbol=bench_sym).rename(
                columns={"date": "d", "close": "c"}
            ),
        ),
        (
            "bench_em",
            lambda: ak_module.stock_zh_index_daily_em(symbol=bench_sym).rename(
                columns={"date": "d", "close": "c"}
            ),
        ),
    ):
        b = _retry(fn, tag, bench_errors, n=3, wait=1.5)
        if b is not None and len(b):
            if not {"d", "c"}.issubset(b.columns):
                bench_errors.append(f"{tag}: 沪深300返回缺少必要列")
                continue
            b = b.copy()
            b["d"] = b["d"].astype(str).str[:10]
            b["c"] = pd.to_numeric(b["c"], errors="coerce")
            b = b[(b["d"] >= start) & (b["d"] <= target_cn_str)].dropna(subset=["c"])
            cand_map = {
                str(d): float(c)
                for d, c in zip(b["d"], b["c"])
                if math.isfinite(float(c)) and float(c) > 0
            }
            if not cand_map:
                bench_errors.append(f"{tag}: 无 {start} 至 {target_cn_str} 有效收盘记录")
                continue
            cand_last = max(cand_map.keys())
            if cand_last == target_cn_str:
                bench_map = cand_map
                bench_source = tag
                break
            if lagging_bench_map is None:
                lagging_bench_map = cand_map
            bench_errors.append(
                f"{tag}: 沪深300基准数据滞后（最新 {cand_last}，目标 {target_cn_str}）"
            )
    if not bench_map:
        errors.extend(bench_errors)

    prev_meta_ind = (load_json_safe(raw_dir / "cn_sw_meta.json", {}) or {}).get("industries") or {}
    inc_by_code, inc_by_name = load_sw_index_inception()
    prev_l1_codes = {
        str(k) for k, v in prev_meta_ind.items() if isinstance(v, dict) and v.get("level") == 1
    }
    prev_l2_codes = {
        str(k) for k, v in prev_meta_ind.items() if isinstance(v, dict) and v.get("level") == 2
    }

    info: dict[str, dict[str, Any]] = {}
    l1_seen: set[str] = set()
    l2_seen: set[str] = set()
    if "first" in levels:
        f = _retry(lambda: ak_module.sw_index_first_info(), "first_info", errors)
        l1_bad = 0
        l1_dups = 0
        if f is not None and len(f):
            for _, r in f.iterrows():
                code_l1 = str(r.get("行业代码", "")).split(".")[0].strip()
                name_l1 = str(r.get("行业名称", "")).strip()
                if not code_l1 or not code_l1.isdigit() or not name_l1:
                    l1_bad += 1
                    continue
                if code_l1 in l1_seen:
                    l1_dups += 1
                    continue
                l1_seen.add(code_l1)
                pub_dt = (
                    _extract_meta_publish_date(r)
                    or _lookup_sw_inception_date(
                        code_l1, name_l1, by_code=inc_by_code, by_name=inc_by_name
                    )
                    or _extract_meta_publish_date(prev_meta_ind.get(code_l1))
                )
                entry_l1: dict[str, Any] = {
                    "name": name_l1,
                    "level": 1,
                    "parent": None,
                    "n_cons": int(r["成份个数"]),
                    "pe_ttm": r["TTM(滚动)市盈率"],
                    "pb": r["市净率"],
                    "div_yield": r["静态股息率"],
                }
                if pub_dt is not None:
                    entry_l1["publish_date"] = pub_dt
                info[code_l1] = entry_l1
        if l1_bad or l1_dups:
            errors.append(f"申万一级行业列表异常 (无效 {l1_bad}, 重复 {l1_dups})")
        if not l1_seen and not any(e.startswith("first_info:") for e in errors):
            errors.append("申万一级行业列表为空")
        if prev_l1_codes and l1_seen:
            missing_l1 = sorted(prev_l1_codes - l1_seen)
            if missing_l1:
                errors.append(f"申万一级行业列表缺失代码 {len(missing_l1)} 个: {', '.join(missing_l1[:5])}")
    if "second" in levels:
        s = _retry(lambda: ak_module.sw_index_second_info(), "second_info", errors)
        l2_raw = len(s) if s is not None else 0
        l2_bad = 0
        l2_dups = 0
        if s is not None and len(s):
            for _, r in s.iterrows():
                code_l2 = str(r.get("行业代码", "")).split(".")[0].strip()
                name_l2 = str(r.get("行业名称", "")).strip()
                if not code_l2 or not code_l2.isdigit() or not name_l2:
                    l2_bad += 1
                    continue
                if code_l2 in l2_seen:
                    l2_dups += 1
                    continue
                l2_seen.add(code_l2)
                if int(r["成份个数"]) < min_cons:
                    continue
                pub_dt = (
                    _extract_meta_publish_date(r)
                    or _lookup_sw_inception_date(
                        code_l2, name_l2, by_code=inc_by_code, by_name=inc_by_name
                    )
                    or _extract_meta_publish_date(prev_meta_ind.get(code_l2))
                )
                entry_l2: dict[str, Any] = {
                    "name": name_l2,
                    "level": 2,
                    "parent": str(r["上级行业"]),
                    "n_cons": int(r["成份个数"]),
                    "pe_ttm": r["TTM(滚动)市盈率"],
                    "pb": r["市净率"],
                    "div_yield": r["静态股息率"],
                }
                if pub_dt is not None:
                    entry_l2["publish_date"] = pub_dt
                info[code_l2] = entry_l2
        if l2_bad or l2_dups:
            errors.append(f"申万二级行业列表异常 (无效 {l2_bad}, 重复 {l2_dups})")
        if l2_raw == 0 and not any(e.startswith("second_info:") for e in errors):
            errors.append("申万二级行业列表为空")
        if prev_l2_codes and l2_seen:
            l2_active = {k for k, v in info.items() if v.get("level") == 2}
            missing_l2 = sorted(prev_l2_codes - l2_active)
            if missing_l2:
                errors.append(f"申万二级行业列表缺失代码 {len(missing_l2)} 个: {', '.join(missing_l2[:5])}")

    hist_new: dict[str, dict[str, float]] = {}
    amount_new: dict[str, dict[str, float]] = {}
    stale_hist_codes: list[str] = []
    total_codes = len(info)
    for idx, code in enumerate(info, start=1):
        err_before = len(errors)
        h = _retry(
            lambda c=code: ak_module.index_hist_sw(symbol=c, period="day"),
            f"hist {code}",
            errors,
            n=3,
            wait=1.0,
        )
        if progress_fn is not None and (idx % 15 == 0 or idx == total_codes):
            progress_fn(f"[pm_cockpit] fetch_cn: {idx}/{total_codes}")
        if h is None or not len(h):
            if len(errors) == err_before:
                errors.append(f"hist {code}: 返回空历史")
            continue
        if not {"日期", "收盘"}.issubset(h.columns):
            errors.append(f"hist {code}: 缺少必要列(日期/收盘)")
            continue
        h = h.copy()
        h["日期"] = h["日期"].astype(str).str[:10]
        h["收盘"] = pd.to_numeric(h["收盘"], errors="coerce")
        h = h[(h["日期"] >= start) & (h["日期"] <= target_cn_str)].dropna(subset=["收盘"])
        code_hist = {
            str(d): float(c)
            for d, c in zip(h["日期"], h["收盘"])
            if math.isfinite(float(c)) and float(c) > 0
        }
        if not code_hist:
            errors.append(f"hist {code}: 无 {start} 至 {target_cn_str} 有效收盘记录")
            continue
        code_last = max(code_hist.keys())
        if code_last != target_cn_str:
            stale_hist_codes.append(code)
            errors.append(
                f"hist {code}: 申万行业日线滞后（最新 {code_last}，目标 {target_cn_str}）"
            )
            continue
        hist_new[code] = code_hist
        amt_col = h["成交额"] if "成交额" in h.columns else pd.Series(0.0, index=h.index)
        amount_new[code] = {
            str(d): float(a) if pd.notna(a) else 0.0 for d, a in zip(h["日期"], amt_col)
        }
        time.sleep(0.15)

    if (not bench_map and not lagging_bench_map) or (not hist_new and not stale_hist_codes):
        raise RuntimeError(
            "akshare 申万行业或沪深300基准拉取失败: " + "; ".join(errors[:5])
        )
    missing_hist = sorted(set(info.keys()) - set(hist_new.keys()) - set(stale_hist_codes))
    if missing_hist:
        errors.append(f"申万行业历史缺失 {len(missing_hist)} 个: {', '.join(missing_hist[:5])}")

    filtered_meta = {k: v for k, v in info.items() if k in hist_new}
    filtered_meta[cn_cfg["benchmark_label"]] = {
        "name": cn_cfg["benchmark_label"],
        "level": 0,
        "parent": None,
        "n_cons": 300,
    }

    h_df: pd.DataFrame | None = None
    if bench_map and hist_new:
        hist_to_merge = dict(hist_new)
        hist_to_merge[cn_cfg["benchmark_label"]] = bench_map
        h_df = _merge_csv(raw_dir / "cn_sw_hist.csv", hist_to_merge)
        _merge_csv(raw_dir / "cn_sw_amount.csv", amount_new)
    if not errors:
        last_date = target_cn_str
    elif h_df is not None and len(h_df):
        last_date = str(h_df.index[-1].date())
    elif lagging_bench_map:
        last_date = max(lagging_bench_map.keys())
    else:
        last_date = target_cn_str

    if not errors or (not (raw_dir / "cn_sw_meta.json").is_file() and hist_new):
        atomic_write_json(
            raw_dir / "cn_sw_meta.json",
            {"as_of": last_date or target_cn_str, "industries": filtered_meta},
            indent=1,
        )
    reason = "; ".join(errors[:3]) if errors else None
    notes: list[str] = []
    if not cn_trading_today and not errors and last_date:
        notes.append(f"A股休市（{ref_day.isoformat()}），沿用最近交易日 {last_date} 数据")
    if cn_cal_src == "weekday_fallback" and cn_cal_note:
        notes.append(cn_cal_note)
    combined_note = "；".join(notes) if notes else None
    src_status = make_source_result(
        "cn",
        ok=not bool(errors),
        stale=bool(errors),
        reason=reason,
        as_of=last_date,
        label="申万行业行情",
        note=combined_note,
    )
    return {
        "ok": src_status.is_clean,
        "stale": src_status.stale,
        "source": "akshare_sw_index",
        "bench_source": bench_source,
        "n_first": sum(1 for v in filtered_meta.values() if v.get("level") == 1),
        "n_second": sum(1 for v in filtered_meta.values() if v.get("level") == 2),
        "last_date": last_date,
        "target_trade_date": target_cn_str,
        "is_trading_day": cn_trading_today,
        "stale_hist_codes": stale_hist_codes,
        "n_errors": len(errors),
        "errors": errors[:5],
        "error": reason,
        "note": combined_note,
        "calendar_note": cn_cal_note if cn_cal_src == "weekday_fallback" else None,
        "source_status": src_status.to_dict(),
    }


def _expected_sw_codes_by_level(raw_dir: Path) -> dict[str, set[str]]:
    meta_ind = (load_json_safe(raw_dir / "cn_sw_meta.json", {}) or {}).get("industries") or {}
    return {
        "一级行业": {
            str(k) for k, v in meta_ind.items() if isinstance(v, dict) and v.get("level") == 1
        },
        "二级行业": {
            str(k) for k, v in meta_ind.items() if isinstance(v, dict) and v.get("level") == 2
        },
    }


def _sw_code_start_dates(raw_dir: Path) -> dict[str, str]:
    """Read per-code inception/valuation-start dates from sw_index_inception.json & cn_sw_meta.json."""
    meta_ind = (load_json_safe(raw_dir / "cn_sw_meta.json", {}) or {}).get("industries") or {}
    inc_by_code, inc_by_name = load_sw_index_inception()
    out: dict[str, str] = dict(inc_by_code)
    for code, entry in meta_ind.items():
        if not isinstance(entry, dict):
            continue
        c_str = str(code).split(".")[0].strip()
        if not c_str:
            continue
        table_dt = _lookup_sw_inception_date(
            c_str,
            str(entry.get("name") or "").strip() or None,
            by_code=inc_by_code,
            by_name=inc_by_name,
        )
        if table_dt is not None:
            out[c_str] = table_dt
            continue
        pub_dt = _extract_meta_publish_date(entry)
        if pub_dt is not None:
            out[c_str] = pub_dt
    return out


def _valid_val_rows(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    if not len(df) or not {"date", "code"}.issubset(df.columns):
        return pd.DataFrame(columns=cols)
    has_val = pd.Series(False, index=df.index)
    for col in ("pe", "pb", "dy"):
        if col in df.columns:
            has_val = has_val | pd.to_numeric(df[col], errors="coerce").notna()
    code_s = df["code"].astype(str).str.strip()
    code_ok = code_s.ne("") & code_s.ne("nan")
    return df[has_val & code_ok].copy()


def _first_seen_dates_by_code(valid_df: pd.DataFrame) -> dict[str, str]:
    if not len(valid_df):
        return {}
    dates_s = valid_df["date"].astype(str).str[:10]
    codes_s = valid_df["code"].astype(str).str.strip()
    return {
        str(code): str(min_dt)
        for code, min_dt in dates_s.groupby(codes_s).min().items()
        if code and code != "nan"
    }


def _required_codes_for_date(
    expected_codes: set[str],
    day_str: str,
    *,
    code_start_dates: dict[str, str] | None = None,
    valuation_start: str = "2021-12-31",
    grace_weeks: int = 4,
    observed_first_seen: dict[str, str] | None = None,
) -> set[str]:
    """Return Shenwan codes required on ``day_str``.

    - When a code has a metadata publish/start date in ``cn_sw_meta.json`` (or from the
      industry info API's ``发布日期`` field), it is required on and after that date.
    - When no metadata start date exists for a code, absence is relaxed ONLY within
      ``grace_weeks`` (4 weeks) after ``valuation_start`` (and before the code first
      appeared); after that initial 4-week window (or once the code has appeared),
      any missing week fails.
    """
    starts = code_start_dates or {}
    grace_end = (date.fromisoformat(valuation_start) + timedelta(weeks=grace_weeks)).isoformat()
    req: set[str] = set()
    for c in expected_codes:
        meta_start = starts.get(c)
        if meta_start:
            if day_str >= meta_start:
                req.add(c)
            continue
        if day_str > grace_end:
            req.add(c)
            continue
        if observed_first_seen is None or c not in observed_first_seen or day_str >= observed_first_seen[c]:
            req.add(c)
    return req


def fetch_cn_val_latest(
    *,
    raw_dir: Path,
    lookback_days: int = 12,
    ak_module: Any = None,
    now: datetime | None = None,
    as_of_date: date | str | None = None,
) -> dict[str, Any]:
    """Current PE/PB/DY snapshot from Shenwan daily analysis; strictly requires target trade date."""
    if ak_module is None:
        import akshare as ak

        ak_module = ak

    expected_by_sym = _expected_sw_codes_by_level(raw_dir)
    cur_cn = (now or now_cn()).astimezone(CN_TZ)
    end_day = resolve_market_trade_date("cn", now=cur_cn, as_of_date=as_of_date)
    target_str = end_day.isoformat()
    start_day = end_day - timedelta(days=lookback_days)
    frames: list[pd.DataFrame] = []
    for sym in ("一级行业", "二级行业"):
        d = ak_module.index_analysis_daily_sw(
            symbol=sym,
            start_date=start_day.strftime("%Y%m%d"),
            end_date=end_day.strftime("%Y%m%d"),
        )
        if d is None or not len(d):
            raise RuntimeError(f"申万行业估值日报({sym})返回空数据")
        target_rows: list[dict[str, Any]] = []
        target_codes: set[str] = set()
        all_codes: set[str] = set()
        all_dates: set[str] = set()
        for _, r in d.iterrows():
            c_str = str(r.get("指数代码", "")).split(".")[0].strip()
            dt_str = str(r.get("发布日期", "")).strip()[:10]
            if not c_str or c_str == "nan" or not dt_str:
                continue
            if dt_str > target_str:
                continue
            pe_v, pb_v, dy_v = r.get("市盈率"), r.get("市净率"), r.get("股息率")
            if pd.isna(pe_v) and pd.isna(pb_v) and pd.isna(dy_v):
                continue
            all_codes.add(c_str)
            all_dates.add(dt_str)
            if dt_str == target_str:
                target_codes.add(c_str)
                target_rows.append(
                    {
                        "date": dt_str,
                        "code": c_str,
                        "pe": pe_v,
                        "pb": pb_v,
                        "dy": dy_v,
                    }
                )
        if not all_codes:
            raise RuntimeError(f"申万行业估值日报({sym})缺少有效代码或指标")
        if not target_codes:
            latest_seen = max(all_dates) if all_dates else "无"
            raise RuntimeError(
                f"申万行业估值日报({sym})未到达目标交易日 {target_str}（最新仅到 {latest_seen}）"
            )
        exp_sym = expected_by_sym.get(sym) or set()
        if exp_sym:
            missing = sorted(exp_sym - target_codes)
            if missing:
                raise RuntimeError(
                    f"申万行业估值日报({sym})在目标交易日 {target_str} 缺失行业代码 {len(missing)} 个: {', '.join(missing[:5])}"
                )
        else:
            lagging = sorted(all_codes - target_codes)
            if lagging:
                raise RuntimeError(
                    f"申万行业估值日报({sym})在目标交易日 {target_str} 滞后行业代码 {len(lagging)} 个: {', '.join(lagging[:5])}"
                )
        frames.append(pd.DataFrame(target_rows))
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["code"], keep="last").sort_values("code")
    if out.empty:
        raise RuntimeError("申万行业估值日报解析后为空")
    out = out[["date", "code", "pe", "pb", "dy"]]
    out.to_csv(raw_dir / "cn_sw_val_latest.csv", index=False)
    return {
        "ok": True,
        "n_codes": int(len(out)),
        "as_of": target_str,
    }


def fetch_cn_valuation(
    cfg: dict[str, Any],
    *,
    raw_dir: Path,
    max_weeks: int = 0,
    ak_module: Any = None,
    progress_fn: Any = None,
    now: datetime | None = None,
    as_of_date: date | str | None = None,
) -> dict[str, Any]:
    """Incremental weekly Shenwan valuation tracked per (date, level) + latest snapshot."""
    if ak_module is None:
        import akshare as ak

        ak_module = ak

    raw_dir.mkdir(parents=True, exist_ok=True)
    start = str(cfg["l2_cn"].get("valuation_start", "2021-12-31"))
    out_path = raw_dir / "cn_sw_val_weekly.csv"
    prog_path = raw_dir / "cn_sw_val_progress.json"
    cols = ["date", "code", "pe", "pb", "dy"]
    levels = ("一级行业", "二级行业")
    expected_by_sym = _expected_sw_codes_by_level(raw_dir)
    code_start_dates = _sw_code_start_dates(raw_dir)
    cur_cn = (now or now_cn()).astimezone(CN_TZ)
    ref_day = (
        date.fromisoformat(as_of_date)
        if isinstance(as_of_date, str)
        else (as_of_date or cur_cn.date())
    )
    _, cn_cal_src, cn_cal_note = inspect_trading_day("cn", ref_day)

    old = pd.read_csv(out_path, dtype={"code": str}) if out_path.exists() else pd.DataFrame(columns=cols)
    prog_raw = load_json_safe(prog_path, {}) or {}
    done_map: dict[str, list[str]] = {
        str(k): list(v) for k, v in (prog_raw.get("done") or {}).items() if isinstance(v, list)
    }

    # Reconcile done_map strictly against actual saved rows on disk; if CSV is missing/empty,
    # reset done_map so every week/level is re-collected rather than trusting stale progress JSON.
    old_valid = _valid_val_rows(old, cols) if out_path.exists() else pd.DataFrame(columns=cols)
    first_seen_old = _first_seen_dates_by_code(old_valid)

    if out_path.exists() and len(old_valid):
        old_codes_by_date: dict[str, set[str]] = {
            str(day): set(grp["code"].astype(str)) for day, grp in old_valid.groupby("date")
        }
        for day_str, lvls in list(done_map.items()):
            have_codes = old_codes_by_date.get(day_str, set())
            kept_lvls: list[str] = []
            for sym in lvls:
                if not have_codes:
                    continue
                exp_sym = expected_by_sym.get(sym) or set()
                req_sym = (
                    _required_codes_for_date(
                        exp_sym,
                        day_str,
                        code_start_dates=code_start_dates,
                        valuation_start=start,
                        observed_first_seen=first_seen_old,
                    )
                    if exp_sym
                    else set()
                )
                if not (req_sym and not req_sym.issubset(have_codes)):
                    kept_lvls.append(sym)
            done_map[day_str] = kept_lvls
    else:
        done_map = {}
    if done_map != (prog_raw.get("done") or {}):
        atomic_write_json(prog_path, {"done": done_map}, indent=1)

    def _pair_finished(day_str: str, lvl: str) -> bool:
        return lvl in done_map.get(day_str, [])

    week_df = ak_module.index_analysis_week_month_sw(symbol="week")
    if week_df is None or not len(week_df) or "date" not in week_df.columns:
        raise RuntimeError("申万周频交易日列表为空")
    api_weeks = sorted(
        {
            dt
            for raw_d in week_df["date"].tolist()
            if (dt := _normalize_iso_date(raw_d)) is not None and dt >= start
        }
    )
    if not api_weeks:
        raise RuntimeError("申万周频无符合起始日期的交易周")
    target_cn_day = resolve_market_trade_date("cn", now=cur_cn, as_of_date=as_of_date)
    expected_weeks = completed_trading_weeks("cn", start, target_cn_day)
    latest_expected_week = expected_weeks[-1] if expected_weeks else None
    eligible_weeks = api_weeks[-max_weeks:] if max_weeks > 0 else api_weeks
    api_week_set = set(eligible_weeks)
    # Freshness gate: only the latest completed trading week is strictly required to appear in
    # Shenwan's catalog. Earlier historical weeks omitted by Shenwan itself (e.g. pre-holiday or
    # make-up weeks such as 2026-09-30) follow what the catalog actually lists; any week that IS
    # listed in the catalog still fails if its weekly detail payload cannot be fetched completely.
    missing_latest_weeks = (
        [latest_expected_week]
        if latest_expected_week is not None and latest_expected_week not in api_week_set
        else []
    )
    for mw in missing_latest_weeks:
        done_map.pop(mw, None)

    todo_weeks = [d for d in eligible_weeks if any(not _pair_finished(d, sym) for sym in levels)]

    rows: list[dict[str, Any]] = []
    deferred_checks: list[tuple[str, str, set[str]]] = []
    done_pairs = 0
    empty_pairs = 0
    errs: list[str] = []
    if missing_latest_weeks:
        errs.append(
            f"申万周报日期列表缺失最近一个已结束交易周 {missing_latest_weeks[0]}"
        )
    for idx, d in enumerate(todo_weeks):
        for sym in levels:
            if _pair_finished(d, sym):
                continue
            df = None
            fetch_exc: Exception | None = None
            for attempt in range(3):
                try:
                    df = ak_module.index_analysis_weekly_sw(
                        symbol=sym, date=d.replace("-", "")
                    )
                    fetch_exc = None
                    break
                except Exception as exc:
                    fetch_exc = exc
                    time.sleep(0.5 * (attempt + 1))

            if fetch_exc is not None or df is None:
                # Transient request failure: NEVER mark done/skip; retry on next run!
                errs.append(f"{d} {sym}: {str(fetch_exc or 'None response')[:120]}")
                continue

            if len(df) == 0:
                # Empty response stays pending & stale; never mark finished!
                empty_pairs += 1
                errs.append(f"{d} {sym}: 返回空数据")
                continue

            date_col = next(
                (col for col in ("发布日期", "日期", "date", "bargaindate") if col in df.columns),
                None,
            )
            if date_col is None:
                errs.append(f"{d} {sym}: 周频估值缺少发布日期字段")
                continue

            pair_rows: list[dict[str, Any]] = []
            sym_codes: set[str] = set()
            wrong_dates: set[str] = set()
            missing_date_rows = 0
            bad_rows = 0
            for _, r in df.iterrows():
                row_dt = _normalize_iso_date(r.get(date_col))
                if row_dt is None:
                    missing_date_rows += 1
                    continue
                if row_dt != d:
                    wrong_dates.add(row_dt)
                    continue
                code_str = str(r.get("指数代码", "")).split(".")[0].strip()
                if not code_str or code_str == "nan":
                    bad_rows += 1
                    continue
                pe_v, pb_v, dy_v = r.get("市盈率"), r.get("市净率"), r.get("股息率")
                if pd.isna(pe_v) and pd.isna(pb_v) and pd.isna(dy_v):
                    bad_rows += 1
                    continue
                sym_codes.add(code_str)
                pair_rows.append(
                    {
                        "date": row_dt,
                        "code": code_str,
                        "pe": pe_v,
                        "pb": pb_v,
                        "dy": dy_v,
                    }
                )
            if wrong_dates:
                # Wrong-week payload (e.g. previous week's report returned for requested week):
                # NEVER write under requested date d or mark done; keep pending and fail cn_valuation.
                sample_dt = ", ".join(sorted(wrong_dates)[:3])
                errs.append(f"{d} {sym}: 周报实际日期({sample_dt})与请求周 {d} 不符")
                continue
            if missing_date_rows > 0:
                errs.append(f"{d} {sym}: 周报含缺失或无效发布日期 {missing_date_rows} 条")
                continue
            if not pair_rows:
                empty_pairs += 1
                errs.append(f"{d} {sym}: 周频估值无有效数值行")
                continue
            if bad_rows > 0:
                # Save partial valid rows for observability, yet NEVER mark (d, sym) finished!
                rows.extend(pair_rows)
                errs.append(f"{d} {sym}: 含无效估值行 {bad_rows} 条")
                continue

            rows.extend(pair_rows)
            exp_sym = expected_by_sym.get(sym) or set()
            if not exp_sym or exp_sym.issubset(sym_codes):
                done_map.setdefault(d, [])
                if sym not in done_map[d]:
                    done_map[d].append(sym)
                done_pairs += 1
            else:
                # Defer missing-code check until all fetched weeks are merged so each
                # code's first-seen date is known across the full weekly dataset.
                deferred_checks.append((d, sym, sym_codes))

        time.sleep(0.05)
        if idx % 25 == 24 or idx == len(todo_weeks) - 1:
            if rows:
                new = pd.DataFrame(rows, columns=cols)
                old = pd.concat([old, new], ignore_index=True) if len(old) else new
                old = old.drop_duplicates(subset=["date", "code"], keep="last").sort_values(
                    ["date", "code"]
                )
                old.to_csv(out_path, index=False)
                rows = []
            atomic_write_json(prog_path, {"done": done_map}, indent=1)
            if progress_fn is not None:
                progress_fn(f"[pm_cockpit] fetch_cn_valuation: {idx + 1}/{len(todo_weeks)} weeks")

    if rows:
        new = pd.DataFrame(rows, columns=cols)
        old = pd.concat([old, new], ignore_index=True) if len(old) else new
        old = old.drop_duplicates(subset=["date", "code"], keep="last").sort_values(["date", "code"])
        old.to_csv(out_path, index=False)
        rows = []

    final_valid = _valid_val_rows(old, cols)
    first_seen_final = _first_seen_dates_by_code(final_valid)
    final_codes_by_date: dict[str, set[str]] = (
        {str(day): set(grp["code"].astype(str)) for day, grp in final_valid.groupby("date")}
        if len(final_valid)
        else {}
    )

    for d, sym, sym_codes in deferred_checks:
        exp_sym = expected_by_sym.get(sym) or set()
        req_sym = (
            _required_codes_for_date(
                exp_sym,
                d,
                code_start_dates=code_start_dates,
                valuation_start=start,
                observed_first_seen=first_seen_final,
            )
            if exp_sym
            else set()
        )
        missing_sym = sorted(req_sym - sym_codes) if req_sym else []
        if missing_sym:
            errs.append(
                f"{d} {sym}: 缺失代码 {len(missing_sym)} 个 ({', '.join(missing_sym[:5])})"
            )
            continue
        done_map.setdefault(d, [])
        if sym not in done_map[d]:
            done_map[d].append(sym)
        done_pairs += 1

    # Re-verify previously done eligible weeks against metadata start dates & grace window.
    for d in eligible_weeks:
        have_codes = final_codes_by_date.get(d, set())
        cur_lvls = done_map.get(d, [])
        if not cur_lvls:
            continue
        still_done: list[str] = []
        for sym in cur_lvls:
            exp_sym = expected_by_sym.get(sym) or set()
            req_sym = (
                _required_codes_for_date(
                    exp_sym,
                    d,
                    code_start_dates=code_start_dates,
                    valuation_start=start,
                    observed_first_seen=first_seen_final,
                )
                if exp_sym
                else set()
            )
            missing_sym = sorted(req_sym - have_codes) if req_sym else []
            if not have_codes or missing_sym:
                if missing_sym:
                    errs.append(
                        f"{d} {sym}: 缺失代码 {len(missing_sym)} 个 ({', '.join(missing_sym[:5])})"
                    )
                continue
            still_done.append(sym)
        done_map[d] = still_done
    atomic_write_json(prog_path, {"done": done_map}, indent=1)

    latest = None
    latest_err: str | None = None
    try:
        latest = fetch_cn_val_latest(
            raw_dir=raw_dir,
            ak_module=ak_module,
            now=now,
            as_of_date=as_of_date,
        )
    except Exception as exc:
        latest_err = f"最新估值日报失败: {exc}"
        errs.append(latest_err)

    all_required_weeks = sorted(set(eligible_weeks) | set(missing_latest_weeks))
    missing_pairs = [
        f"{d}/{sym}"
        for d in all_required_weeks
        for sym in levels
        if not _pair_finished(d, sym)
    ]
    is_stale = bool(errs or missing_pairs)
    reason = "; ".join(errs[:3]) if errs else (f"存在 {len(missing_pairs)} 项未完成周频估值" if missing_pairs else None)
    as_of = (latest or {}).get("as_of") or (str(old["date"].max()) if len(old) else None)
    val_note = cn_cal_note if (cn_cal_src == "weekday_fallback" and cn_cal_note) else None
    src_status = make_source_result(
        "cn_valuation",
        ok=not is_stale,
        stale=is_stale,
        reason=reason,
        as_of=as_of,
        label="申万行业估值",
        note=val_note,
    )
    return {
        "ok": src_status.is_clean,
        "stale": src_status.stale,
        "weeks_fetched_pairs": done_pairs,
        "weeks_empty_pairs": empty_pairs,
        "missing_weeks": missing_latest_weeks,
        "missing_pairs": len(missing_pairs),
        "n_rows": int(len(old)),
        "n_codes": int(old["code"].nunique()) if len(old) else 0,
        "first_week": str(old["date"].min()) if len(old) else None,
        "last_week": str(old["date"].max()) if len(old) else None,
        "latest": latest,
        "errors": errs[:5],
        "error": reason,
        "note": val_note,
        "calendar_note": val_note,
        "source_status": src_status.to_dict(),
    }


def _status_from_dict(name: str, label: str, d: dict[str, Any] | None) -> SourceStatus:
    raw = (d or {}).get("source_status")
    if isinstance(raw, dict):
        return SourceStatus.from_dict({**raw, "name": name, "label": raw.get("label") or label})
    ok = bool((d or {}).get("ok"))
    stale = bool((d or {}).get("stale")) or (not ok)
    return make_source_result(
        name,
        ok=ok and not stale,
        stale=stale,
        reason=(d or {}).get("error"),
        as_of=(d or {}).get("last_date"),
        label=label,
        note=(d or {}).get("note"),
    )


def collect_all(
    *,
    which: str = "all",
    state_dir: str | Path | None = None,
    cfg_path: Path | None = None,
    yf_module: Any = None,
    ak_module: Any = None,
    progress_fn: Any = None,
    commit_status: bool = True,
    now: datetime | None = None,
    as_of_date: date | str | None = None,
) -> dict[str, Any]:
    cfg = load_config(cfg_path)
    st_dir = resolve_state_dir("pm_cockpit", state_dir)
    raw_dir = st_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    meta_path = raw_dir / "collect_meta.json"
    meta: dict[str, Any] = load_json_safe(meta_path, {}) or {}
    status_store = SourceStatusStore(st_dir)
    meta["ts"] = (now or now_us()).astimezone(US_TZ).isoformat(timespec="seconds")

    collected_statuses: dict[str, SourceStatus] = {}
    if which in ("all", "us"):
        if progress_fn is not None:
            progress_fn("[pm_cockpit] stage us: fetching...")
        try:
            meta["us"] = fetch_us(
                cfg,
                raw_dir=raw_dir,
                yf_module=yf_module,
                now=now,
                as_of_date=as_of_date,
            )
            collected_statuses["us"] = _status_from_dict("us", "美股行情", meta["us"])
        except Exception as exc:
            collected_statuses["us"] = make_source_result(
                "us", ok=False, stale=True, reason=str(exc), label="美股行情"
            )
            meta["us"] = {"ok": False, "stale": True, "error": str(exc)}

    if which in ("all", "cn"):
        if progress_fn is not None:
            progress_fn("[pm_cockpit] stage cn: fetching Shenwan index history...")
        try:
            meta["cn"] = fetch_cn(
                cfg,
                raw_dir=raw_dir,
                ak_module=ak_module,
                progress_fn=progress_fn,
                now=now,
                as_of_date=as_of_date,
            )
            collected_statuses["cn"] = _status_from_dict("cn", "申万行业行情", meta["cn"])
        except Exception as exc:
            collected_statuses["cn"] = make_source_result(
                "cn", ok=False, stale=True, reason=str(exc), label="申万行业行情"
            )
            meta["cn"] = {"ok": False, "stale": True, "error": str(exc)}

    if which in ("all", "cn", "cnval"):
        if progress_fn is not None:
            progress_fn("[pm_cockpit] stage cnval: fetching Shenwan weekly valuation...")
        try:
            meta["cn_valuation"] = fetch_cn_valuation(
                cfg,
                raw_dir=raw_dir,
                ak_module=ak_module,
                progress_fn=progress_fn,
                now=now,
                as_of_date=as_of_date,
            )
            collected_statuses["cn_valuation"] = _status_from_dict(
                "cn_valuation", "申万行业估值", meta["cn_valuation"]
            )
        except Exception as exc:
            collected_statuses["cn_valuation"] = make_source_result(
                "cn_valuation", ok=False, stale=True, reason=str(exc), label="申万行业估值"
            )
            meta["cn_valuation"] = {"ok": False, "stale": True, "error": str(exc)}

    now_stamp = (now or now_cn()).astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间"
    req = list(pm_required_sources(which))
    status_doc = (
        status_store.update_sources(collected_statuses, required=req, now_str=now_stamp)
        if commit_status
        else status_store.preview_sources(collected_statuses, required=req, now_str=now_stamp)
    )
    for key in collected_statuses:
        if isinstance(meta.get(key), dict):
            meta[key]["last_success_at"] = status_doc["sources"][key]["last_success_at"]
    meta["sources"] = status_doc["sources"]
    meta["overall"] = status_doc["overall"]
    meta["required_sources"] = req
    meta["_pending_statuses"] = [s.to_dict() for s in collected_statuses.values()]

    atomic_write_json(meta_path, meta, indent=2)
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect PM L1/L2 market data.")
    parser.add_argument("which", nargs="?", default="all", choices=("all", "us", "cn", "cnval"))
    parser.add_argument("--state-dir", default=None)
    args = parser.parse_args()
    res = collect_all(which=args.which, state_dir=args.state_dir)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
