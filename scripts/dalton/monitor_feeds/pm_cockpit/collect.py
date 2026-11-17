#!/usr/bin/env python3
"""PM Market State & Sector Rotation — L0 data collection (US yfinance + CN akshare)."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
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
    load_json_safe,
    make_source_result,
    resolve_state_dir,
)

HERE = Path(__file__).resolve().parent
DEFAULT_CFG_PATH = HERE / "config.json"


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
) -> dict[str, Any]:
    """Fetch US ETF/index Adj Close prices via yfinance (auto_adjust=False)."""
    if yf_module is None:
        import yfinance as yf

        yf_module = yf

    raw_dir.mkdir(parents=True, exist_ok=True)
    tk = us_tickers(cfg)
    start = (datetime.now(US_TZ) - timedelta(days=int(365.25 * years))).strftime("%Y-%m-%d")
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

    missing = [
        c
        for c in tk
        if c not in px.columns or px[c].notna().sum() < 60 or pd.isna(px[c].iloc[-1])
    ]
    px.index.name = "date"
    out_csv = raw_dir / "us_prices.csv"
    px.to_csv(out_csv)
    last_date = str(px.index[-1].date())
    reason = f"部分标的样本不足: {', '.join(missing)}" if missing else None
    src_status = make_source_result(
        "us",
        ok=not bool(missing),
        stale=bool(missing),
        reason=reason,
        as_of=last_date,
        label="美股行情",
    )
    return {
        "ok": src_status.is_clean,
        "stale": src_status.stale,
        "rows": int(px.shape[0]),
        "cols": int(px.shape[1]),
        "last_date": last_date,
        "insufficient": missing,
        "error": reason,
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


def fetch_cn(
    cfg: dict[str, Any],
    *,
    raw_dir: Path,
    years: int = 3,
    ak_module: Any = None,
    progress_fn: Any = None,
) -> dict[str, Any]:
    """Fetch Shenwan L1/L2 index daily close & turnover + CSI 300 benchmark via akshare."""
    if ak_module is None:
        import akshare as ak

        ak_module = ak

    raw_dir.mkdir(parents=True, exist_ok=True)
    cn_cfg = cfg["l2_cn"]
    start = (datetime.now(CN_TZ) - timedelta(days=int(365.25 * years))).strftime("%Y-%m-%d")
    levels = cn_cfg.get("levels", ["first", "second"])
    min_cons = int(cn_cfg.get("min_constituents", 5))
    errors: list[str] = []

    bench_map: dict[str, float] | None = None
    bench_source: str | None = None
    for tag, fn in (
        (
            "bench_sina",
            lambda: ak_module.stock_zh_index_daily(symbol="sh000300")
            .reset_index()
            .rename(columns={"date": "d", "close": "c"}),
        ),
        (
            "bench_em",
            lambda: ak_module.stock_zh_index_daily_em(symbol="sh000300").rename(
                columns={"date": "d", "close": "c"}
            ),
        ),
    ):
        b = _retry(fn, tag, errors, n=3, wait=1.5)
        if b is not None and len(b):
            b["d"] = b["d"].astype(str).str[:10]
            b = b[b["d"] >= start]
            bench_map = {str(d): float(c) for d, c in zip(b["d"], b["c"])}
            bench_source = tag
            break

    prev_meta_ind = (load_json_safe(raw_dir / "cn_sw_meta.json", {}) or {}).get("industries") or {}
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
                info[code_l1] = {
                    "name": name_l1,
                    "level": 1,
                    "parent": None,
                    "n_cons": int(r["成份个数"]),
                    "pe_ttm": r["TTM(滚动)市盈率"],
                    "pb": r["市净率"],
                    "div_yield": r["静态股息率"],
                }
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
                info[code_l2] = {
                    "name": name_l2,
                    "level": 2,
                    "parent": str(r["上级行业"]),
                    "n_cons": int(r["成份个数"]),
                    "pe_ttm": r["TTM(滚动)市盈率"],
                    "pb": r["市净率"],
                    "div_yield": r["静态股息率"],
                }
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
        h = h.copy()
        h["日期"] = h["日期"].astype(str).str[:10]
        h = h[h["日期"] >= start].dropna(subset=["收盘"])
        if not len(h):
            errors.append(f"hist {code}: 无 {start} 以来有效收盘记录")
            continue
        hist_new[code] = {str(d): float(c) for d, c in zip(h["日期"], h["收盘"])}
        amount_new[code] = {
            str(d): float(a) if pd.notna(a) else 0.0 for d, a in zip(h["日期"], h["成交额"])
        }
        time.sleep(0.15)

    if not bench_map or not hist_new:
        raise RuntimeError(
            "akshare 申万行业或沪深300基准拉取失败: " + "; ".join(errors[:5])
        )
    missing_hist = sorted(set(info.keys()) - set(hist_new.keys()))
    if missing_hist:
        errors.append(f"申万行业历史缺失 {len(missing_hist)} 个: {', '.join(missing_hist[:5])}")

    filtered_meta = {k: v for k, v in info.items() if k in hist_new}
    filtered_meta[cn_cfg["benchmark_label"]] = {
        "name": cn_cfg["benchmark_label"],
        "level": 0,
        "parent": None,
        "n_cons": 300,
    }
    if not errors or not (raw_dir / "cn_sw_meta.json").is_file():
        atomic_write_json(
            raw_dir / "cn_sw_meta.json",
            {"as_of": datetime.now(CN_TZ).strftime("%Y-%m-%d"), "industries": filtered_meta},
            indent=1,
        )

    hist_new[cn_cfg["benchmark_label"]] = bench_map
    h_df = _merge_csv(raw_dir / "cn_sw_hist.csv", hist_new)
    _merge_csv(raw_dir / "cn_sw_amount.csv", amount_new)
    last_date = str(h_df.index[-1].date()) if h_df is not None and len(h_df) else None
    reason = "; ".join(errors[:3]) if errors else None
    src_status = make_source_result(
        "cn",
        ok=not bool(errors),
        stale=bool(errors),
        reason=reason,
        as_of=last_date,
        label="申万行业行情",
    )
    return {
        "ok": src_status.is_clean,
        "stale": src_status.stale,
        "source": "akshare_sw_index",
        "bench_source": bench_source,
        "n_first": sum(1 for v in filtered_meta.values() if v.get("level") == 1),
        "n_second": sum(1 for v in filtered_meta.values() if v.get("level") == 2),
        "last_date": last_date,
        "n_errors": len(errors),
        "errors": errors[:5],
        "error": reason,
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


def fetch_cn_val_latest(
    *,
    raw_dir: Path,
    lookback_days: int = 12,
    ak_module: Any = None,
) -> dict[str, Any]:
    """Current PE/PB/DY snapshot from Shenwan daily analysis (same source as weekly)."""
    if ak_module is None:
        import akshare as ak

        ak_module = ak

    expected_by_sym = _expected_sw_codes_by_level(raw_dir)
    end = datetime.now(CN_TZ)
    start = end - timedelta(days=lookback_days)
    frames: list[pd.DataFrame] = []
    for sym in ("一级行业", "二级行业"):
        d = ak_module.index_analysis_daily_sw(
            symbol=sym,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if d is None or not len(d):
            raise RuntimeError(f"申万行业估值日报({sym})返回空数据")
        sym_codes: set[str] = set()
        for _, r in d.iterrows():
            c_str = str(r.get("指数代码", "")).split(".")[0].strip()
            dt_str = str(r.get("发布日期", "")).strip()[:10]
            if not c_str or c_str == "nan" or not dt_str:
                continue
            if pd.isna(r.get("市盈率")) and pd.isna(r.get("市净率")) and pd.isna(r.get("股息率")):
                continue
            sym_codes.add(c_str)
        if not sym_codes:
            raise RuntimeError(f"申万行业估值日报({sym})缺少有效代码或指标")
        exp_sym = expected_by_sym.get(sym) or set()
        if exp_sym and not exp_sym.issubset(sym_codes):
            missing = sorted(exp_sym - sym_codes)
            raise RuntimeError(
                f"申万行业估值日报({sym})缺失行业代码 {len(missing)} 个: {', '.join(missing[:5])}"
            )
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    df["code"] = df["指数代码"].astype(str).str.split(".").str[0]
    df["date"] = df["发布日期"].astype(str).str[:10]
    df = df.sort_values("date").groupby("code", as_index=False).last()
    if df.empty:
        raise RuntimeError("申万行业估值日报解析后为空")
    out = df[["date", "code", "市盈率", "市净率", "股息率"]].rename(
        columns={"市盈率": "pe", "市净率": "pb", "股息率": "dy"}
    )
    out.to_csv(raw_dir / "cn_sw_val_latest.csv", index=False)
    return {
        "ok": True,
        "n_codes": int(len(out)),
        "as_of": str(out["date"].max()),
    }


def fetch_cn_valuation(
    cfg: dict[str, Any],
    *,
    raw_dir: Path,
    max_weeks: int = 0,
    ak_module: Any = None,
    progress_fn: Any = None,
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

    old = pd.read_csv(out_path, dtype={"code": str}) if out_path.exists() else pd.DataFrame(columns=cols)
    prog_raw = load_json_safe(prog_path, {}) or {}
    done_map: dict[str, list[str]] = {
        str(k): list(v) for k, v in (prog_raw.get("done") or {}).items() if isinstance(v, list)
    }

    # Reconcile done_map strictly against actual saved rows on disk; if CSV is missing/empty,
    # reset done_map so every week/level is re-collected rather than trusting stale progress JSON.
    old_valid = pd.DataFrame(columns=cols)
    if out_path.exists() and len(old) and {"date", "code"}.issubset(old.columns):
        has_val = pd.Series(False, index=old.index)
        for col in ("pe", "pb", "dy"):
            if col in old.columns:
                has_val = has_val | pd.to_numeric(old[col], errors="coerce").notna()
        code_ok = old["code"].astype(str).str.strip().ne("") & old["code"].astype(str).str.strip().ne("nan")
        old_valid = old[has_val & code_ok].copy()

    if out_path.exists() and len(old_valid):
        old_codes_by_date: dict[str, set[str]] = {
            str(day): set(grp["code"].astype(str)) for day, grp in old_valid.groupby("date")
        }
        for day_str, lvls in list(done_map.items()):
            have_codes = old_codes_by_date.get(day_str, set())
            done_map[day_str] = [
                sym
                for sym in lvls
                if bool(have_codes)
                and not (expected_by_sym.get(sym) and not expected_by_sym[sym].issubset(have_codes))
            ]
    else:
        done_map = {}
    if done_map != (prog_raw.get("done") or {}):
        atomic_write_json(prog_path, {"done": done_map}, indent=1)

    def _pair_finished(day_str: str, lvl: str) -> bool:
        return lvl in done_map.get(day_str, [])

    week_df = ak_module.index_analysis_week_month_sw(symbol="week")
    if week_df is None or not len(week_df) or "date" not in week_df.columns:
        raise RuntimeError("申万周频交易日列表为空")
    weeks = week_df["date"].astype(str).tolist()
    eligible_weeks = [d for d in weeks if d >= start]
    if not eligible_weeks:
        raise RuntimeError("申万周频无符合起始日期的交易周")
    if max_weeks > 0:
        eligible_weeks = eligible_weeks[-max_weeks:]

    todo_weeks = [d for d in eligible_weeks if any(not _pair_finished(d, sym) for sym in levels)]

    rows: list[dict[str, Any]] = []
    done_pairs = 0
    empty_pairs = 0
    errs: list[str] = []
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

            pair_rows: list[dict[str, Any]] = []
            sym_codes: set[str] = set()
            bad_rows = 0
            for _, r in df.iterrows():
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
                        "date": d,
                        "code": code_str,
                        "pe": pe_v,
                        "pb": pb_v,
                        "dy": dy_v,
                    }
                )
            if not pair_rows:
                empty_pairs += 1
                errs.append(f"{d} {sym}: 周频估值无有效数值行")
                continue
            exp_sym = expected_by_sym.get(sym) or set()
            missing_sym = sorted(exp_sym - sym_codes) if exp_sym else []
            if bad_rows > 0 or missing_sym:
                # Save partial valid rows for observability if useful, yet NEVER mark (d, sym) finished!
                rows.extend(pair_rows)
                detail = (
                    f"缺失代码 {len(missing_sym)} 个 ({', '.join(missing_sym[:5])})"
                    if missing_sym
                    else f"含无效估值行 {bad_rows} 条"
                )
                errs.append(f"{d} {sym}: {detail}")
                continue

            rows.extend(pair_rows)
            done_map.setdefault(d, [])
            if sym not in done_map[d]:
                done_map[d].append(sym)
            done_pairs += 1

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

    latest = None
    latest_err: str | None = None
    try:
        latest = fetch_cn_val_latest(raw_dir=raw_dir, ak_module=ak_module)
    except Exception as exc:
        latest_err = f"最新估值日报失败: {exc}"
        errs.append(latest_err)

    missing_pairs = [
        f"{d}/{sym}"
        for d in eligible_weeks
        for sym in levels
        if not _pair_finished(d, sym)
    ]
    is_stale = bool(errs or missing_pairs)
    reason = "; ".join(errs[:3]) if errs else (f"存在 {len(missing_pairs)} 项未完成周频估值" if missing_pairs else None)
    as_of = (latest or {}).get("as_of") or (str(old["date"].max()) if len(old) else None)
    src_status = make_source_result(
        "cn_valuation",
        ok=not is_stale,
        stale=is_stale,
        reason=reason,
        as_of=as_of,
        label="申万行业估值",
    )
    return {
        "ok": src_status.is_clean,
        "stale": src_status.stale,
        "weeks_fetched_pairs": done_pairs,
        "weeks_empty_pairs": empty_pairs,
        "missing_pairs": len(missing_pairs),
        "n_rows": int(len(old)),
        "n_codes": int(old["code"].nunique()) if len(old) else 0,
        "first_week": str(old["date"].min()) if len(old) else None,
        "last_week": str(old["date"].max()) if len(old) else None,
        "latest": latest,
        "errors": errs[:5],
        "error": reason,
        "source_status": src_status.to_dict(),
    }


def _status_from_dict(name: str, label: str, d: dict[str, Any] | None) -> SourceStatus:
    raw = (d or {}).get("source_status")
    if isinstance(raw, dict):
        return SourceStatus(
            name=name,
            ok=bool(raw.get("ok")),
            stale=bool(raw.get("stale")),
            reason=raw.get("reason"),
            as_of=raw.get("as_of"),
            label=raw.get("label") or label,
        )
    ok = bool((d or {}).get("ok"))
    stale = bool((d or {}).get("stale")) or (not ok)
    return make_source_result(
        name,
        ok=ok and not stale,
        stale=stale,
        reason=(d or {}).get("error"),
        as_of=(d or {}).get("last_date"),
        label=label,
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
) -> dict[str, Any]:
    cfg = load_config(cfg_path)
    st_dir = resolve_state_dir("pm_cockpit", state_dir)
    raw_dir = st_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    meta_path = raw_dir / "collect_meta.json"
    meta: dict[str, Any] = load_json_safe(meta_path, {}) or {}
    status_store = SourceStatusStore(st_dir)
    meta["ts"] = datetime.now(US_TZ).isoformat(timespec="seconds")

    collected_statuses: dict[str, SourceStatus] = {}
    if which in ("all", "us"):
        if progress_fn is not None:
            progress_fn("[pm_cockpit] stage us: fetching...")
        try:
            meta["us"] = fetch_us(cfg, raw_dir=raw_dir, yf_module=yf_module)
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
                cfg, raw_dir=raw_dir, ak_module=ak_module, progress_fn=progress_fn
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
                cfg, raw_dir=raw_dir, ak_module=ak_module, progress_fn=progress_fn
            )
            collected_statuses["cn_valuation"] = _status_from_dict(
                "cn_valuation", "申万行业估值", meta["cn_valuation"]
            )
        except Exception as exc:
            collected_statuses["cn_valuation"] = make_source_result(
                "cn_valuation", ok=False, stale=True, reason=str(exc), label="申万行业估值"
            )
            meta["cn_valuation"] = {"ok": False, "stale": True, "error": str(exc)}

    now_stamp = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间"
    req = (
        ["us", "cn", "cn_valuation"]
        if which == "all"
        else (["us"] if which == "us" else (["cn", "cn_valuation"] if which == "cn" else ["cn_valuation"]))
    )
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
