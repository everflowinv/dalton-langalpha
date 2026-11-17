#!/usr/bin/env python3
"""HK Momentum Index — index computation & intraday valuation.

Fixes vs legacy implementation:
1. ``month_ends()`` strictly excludes any unfinished/ongoing month (uses XHKG
   exchange calendar ``last_trading_day_of_month``), preventing daily re-selection
   of the current month's constituents.
2. Persists ``frozen_history.json``: past rebalance selections and past daily
   index levels up to ``frozen_last_date`` are locked and never retroactively
   rewritten by subsequent daily runs; ``launch_date`` records when live
   point-in-time freezing started vs earlier backfilled dates.
3. Missing/stale spot quotes during intraday valuation are checked against
   the expected HK trading date, never silently reweighted over remaining names,
   and mark ``stale=True`` if coverage < 50% (otherwise missing names hold 0%
   change in equal-weight buy-and-hold, with ``partial_quotes=True`` &
   ``missing_codes`` recorded).
4. Turnover filter label and metadata match the median calculation
   (``近3个月日成交额中位数 ≥ 100 万港元``).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import math
from pathlib import Path
import re
from typing import Any
import warnings

import pandas as pd

from scripts.dalton.monitor_feeds.common import (
    HK_TZ,
    SourceStatus,
    SourceStatusStore,
    atomic_write_json,
    format_source_statuses,
    is_trading_day,
    last_trading_day_of_month,
    load_json_safe,
    make_source_result,
    market_session_state,
    now_hk,
    previous_trading_day,
    resolve_state_dir,
)
from scripts.dalton.monitor_feeds.hk_momentum.fetch_data import (
    fetch_hsi_spot,
    fetch_spot_batch,
)

warnings.filterwarnings("ignore", category=RuntimeWarning)

TOP_N = 50
MIN_TURNOVER_WAN = 100.0  # 3M median daily turnover >= 100万港元
MIN_PRICE = 0.10
SMALLCAP_LIMIT_E8 = 50.0  # 50亿港元
BASE = 1000.0

_QUOTE_DATE_RE = re.compile(r"(\d{4})[/-]?(\d{2})[/-]?(\d{2})")


def load_panel(
    state_dir: Path,
    *,
    required_codes: set[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    uni_all = pd.read_csv(state_dir / "universe.csv", dtype={"code": str})
    uni = uni_all[uni_all["price"].notna()].set_index("code")
    hist_dir = state_dir / "hist"
    load_codes = list(uni.index)
    if required_codes:
        seen = set(load_codes)
        for c in sorted(required_codes):
            if c not in seen:
                load_codes.append(c)
                seen.add(c)
    closes: dict[str, pd.Series] = {}
    turns: dict[str, pd.Series] = {}
    for code in load_codes:
        p = hist_dir / f"{code}.csv"
        if not p.is_file():
            continue
        try:
            df = pd.read_csv(p)
        except Exception:
            continue
        if len(df) < 30:
            continue
        df = df.drop_duplicates("date").set_index("date")
        closes[code] = df["close"]
        turns[code] = df["turnover_wan"]
    px = pd.DataFrame(closes).sort_index()
    tv = pd.DataFrame(turns).reindex(px.index)
    valid = px.notna().sum(axis=1) >= max(50, 0.2 * px.shape[1])
    px, tv = px[valid], tv[valid]
    return uni, px, tv


def month_ends(
    dates: list[str],
    *,
    as_of_date: date | None = None,
) -> list[str]:
    """Return completed HKEX month-end dates in ``dates`` (never an ongoing month)."""
    if not dates:
        return []
    s = pd.Series(dates, index=pd.to_datetime(dates))
    grp = s.groupby([s.index.year, s.index.month]).last()
    ref_date = as_of_date or pd.Timestamp(dates[-1]).date()
    completed: list[str] = []
    for (year, month), last_str in grp.items():
        last_dt = pd.Timestamp(last_str).date()
        if (year, month) > (ref_date.year, ref_date.month):
            continue
        try:
            sched_last = last_trading_day_of_month("hk", int(year), int(month))
        except ValueError:
            sched_last = last_dt
        if last_dt >= sched_last:
            completed.append(str(last_str))
    return completed


def momentum_scores(
    px_ff: pd.DataFrame,
    raw_px: pd.DataFrame,
    tv: pd.DataFrame,
    uni: pd.DataFrame,
    t_idx: int,
    ggt_codes: set[str],
) -> pd.DataFrame | None:
    if t_idx < 273:
        return None
    window = px_ff.iloc[: t_idx + 1]
    p_t = window.iloc[-1]
    p_1m = window.iloc[-22]
    p_7m = window.iloc[-148]
    p_13m = window.iloc[-274]
    ret6 = p_1m / p_7m - 1.0
    ret12 = p_1m / p_13m - 1.0
    rets = window.iloc[-253:].pct_change()
    vol = rets.std() * math.sqrt(252)

    raw_win = raw_px.iloc[: t_idx + 1]
    bars_12m = raw_win.iloc[-252:].notna().sum()
    last10 = raw_win.iloc[-10:].notna().sum()
    tv_3m = tv.iloc[: t_idx + 1].iloc[-63:].median()
    listed = raw_win.notna().sum() >= 274
    elig = (
        listed
        & (bars_12m >= 151)
        & (last10 > 0)
        & (p_t >= MIN_PRICE)
        & (tv_3m >= MIN_TURNOVER_WAN)
        & vol.notna()
        & (vol > 0.01)
        & ret6.notna()
        & ret12.notna()
    )
    df = pd.DataFrame(
        {
            "price": p_t,
            "ret6": ret6,
            "ret12": ret12,
            "vol": vol,
            "tv3m_wan": tv_3m,
            "elig": elig,
        }
    )
    shares_e8 = uni["mktcap_e8"] / uni["price"]
    df["mktcap_e8"] = shares_e8.reindex(df.index) * df["price"]
    df["is_ggt"] = df.index.isin(ggt_codes)
    df = df[df["elig"] & df["mktcap_e8"].notna()]
    if len(df) < 20:
        return None

    def zsc(x: pd.Series) -> pd.Series:
        sd = x.std()
        if not sd or sd != sd:
            return x * 0.0
        return ((x - x.mean()) / sd).clip(-3.0, 3.0)

    ra6, ra12 = df["ret6"] / df["vol"], df["ret12"] / df["vol"]
    df["score"] = 0.5 * zsc(ra6) + 0.5 * zsc(ra12)
    return df


def build_index(
    px_ff: pd.DataFrame,
    dates: pd.Index,
    rebal_positions: list[int],
    selections: list[list[str]],
    *,
    initial_level: float = BASE,
) -> pd.Series:
    level = pd.Series(index=dates, dtype=float)
    lvl = float(initial_level)
    for k, (t_idx, codes) in enumerate(zip(rebal_positions, selections)):
        end_idx = rebal_positions[k + 1] if k + 1 < len(rebal_positions) else len(dates) - 1
        valid_codes = [c for c in codes if c in px_ff.columns]
        if not valid_codes:
            continue
        p0 = px_ff.iloc[t_idx][valid_codes]
        valid_codes = [c for c in valid_codes if pd.notna(p0[c]) and p0[c] > 0]
        if not valid_codes:
            continue
        seg = px_ff.iloc[t_idx : end_idx + 1][valid_codes]
        rel = seg.div(p0[valid_codes], axis=1).ffill().fillna(1.0).mean(axis=1)
        seg_level = lvl * rel / rel.iloc[0]
        level.loc[seg_level.index] = seg_level
        lvl = float(seg_level.iloc[-1])
    return level.dropna()


def _fetch_hsi_history(
    common_dates: pd.Index, session: Any = None
) -> tuple[pd.Series | None, set[str]]:
    import requests

    try:
        s = session or requests.Session()
        if hasattr(s, "trust_env"):
            s.trust_env = False
        r = s.get(
            "https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=hkHSI,day,2023-06-01,,900,",
            timeout=15,
        )
        rows = r.json()["data"]["hkHSI"]["day"]
        raw_map = {str(x[0]): float(x[2]) for x in rows if x and x[0]}
        raw_dates = set(raw_map.keys())
        hsi = pd.Series(raw_map).reindex(common_dates).ffill()
        valid = hsi.dropna()
        if valid.empty:
            return None, set()
        return hsi / valid.iloc[0] * BASE, raw_dates
    except Exception:
        return None, set()


def persist_compute_result(
    st_dir: Path,
    computed: dict[str, Any],
    *,
    meta_override: dict[str, Any] | None = None,
) -> None:
    """Persist computed daily series, frozen history, constituents, and metadata atomically."""
    atomic_write_json(st_dir / "index_series.json", computed["series"])
    atomic_write_json(st_dir / "frozen_history.json", computed["frozen_payload"])
    atomic_write_json(st_dir / "constituents.json", computed["constituents"])
    atomic_write_json(st_dir / "index_meta.json", meta_override or computed["meta"])


def compute_all(
    *,
    state_dir: str | Path | None = None,
    as_of_date: date | None = None,
    hsi_session: Any = None,
    freeze_on_stale: bool = True,
    update_last_success: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    st_dir = resolve_state_dir("hk_momentum", state_dir)
    frozen_path = st_dir / "frozen_history.json"
    if frozen_path.is_file():
        try:
            frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"frozen_history.json 文件损坏，拒绝回退全量重算: {exc}") from exc
        if not isinstance(frozen, dict):
            raise RuntimeError("frozen_history.json 格式无效，拒绝回退全量重算")
    else:
        frozen = {}

    frozen_rebals: dict[str, dict[str, list[str]]] = frozen.get("rebalance_snapshots") or {}
    frozen_series_raw: dict[str, Any] = frozen.get("frozen_series") or {}
    held_frozen_codes: set[str] = {
        str(c)
        for snap_map in frozen_rebals.values()
        if isinstance(snap_map, dict)
        for code_list in snap_map.values()
        if isinstance(code_list, list)
        for c in code_list
        if c
    }

    uni, px, tv = load_panel(st_dir, required_codes=held_frozen_codes)
    uni_all_df = pd.read_csv(st_dir / "universe.csv", dtype={"code": str})
    uni_spot_prices: dict[str, Any] = {
        str(r["code"]): r["price"]
        for _, r in uni_all_df.iterrows()
        if pd.notna(r.get("code"))
    }
    ggt_raw = load_json_safe(st_dir / "ggt_list.json", {}) or {}
    ggt_codes = set(ggt_raw.keys())
    px_ff = px.ffill()
    dates = list(px.index)
    if not dates:
        raise RuntimeError("港股日线面板为空，无法计算动量指数")

    ref_date = as_of_date or now_hk().astimezone(HK_TZ).date()
    mes = [d for d in month_ends(dates, as_of_date=ref_date) if dates.index(d) >= 273]
    if not mes:
        raise RuntimeError("有效历史交易日不足（需满 274 个交易日及至少 1 个完整月末调仓日）")
    date_pos = {d: i for i, d in enumerate(dates)}

    launch_date: str = frozen.get("launch_date") or dates[-1]

    idx_defs = {
        "smallcap": lambda df: df[df["mktcap_e8"] < SMALLCAP_LIMIT_E8],
        "nonggt": lambda df: df[~df["is_ggt"]],
    }
    selections: dict[str, list[list[str]]] = {k: [] for k in idx_defs}
    rebal_pos: list[int] = []
    rebal_info: dict[str, list[dict[str, Any]]] = {k: [] for k in idx_defs}

    # Track completeness of month-end rebalances (non-empty selection for each index).
    rebal_incomplete = False
    for d in mes:
        t_idx = date_pos[d]
        if d in frozen_rebals:
            rebal_pos.append(t_idx)
            for key in idx_defs:
                codes = list(frozen_rebals[d].get(key) or [])
                if not codes:
                    rebal_incomplete = True
                selections[key].append(codes)
                rebal_info[key].append({"date": d, "pool": None, "n": len(codes), "frozen": True})
            continue

        scores = momentum_scores(px_ff, px, tv, uni, t_idx, ggt_codes)
        if scores is None:
            rebal_incomplete = True
            continue
        rebal_pos.append(t_idx)
        frozen_rebals[d] = {}
        for key, filt in idx_defs.items():
            pool = filt(scores)
            top = pool.nlargest(min(TOP_N, len(pool)), "score")
            codes = list(top.index)
            if not codes:
                rebal_incomplete = True
            selections[key].append(codes)
            frozen_rebals[d][key] = codes
            rebal_info[key].append({"date": d, "pool": int(len(pool)), "n": int(len(top)), "frozen": False})

    if not rebal_pos:
        raise RuntimeError("没有满足动量选股条件的调仓日（候选池为空或样本不足）")

    # Determine expected target trading day & verify raw un-ffilled coverage before freezing.
    target_day = ref_date if is_trading_day("hk", ref_date) else previous_trading_day("hk", ref_date)
    target_str = target_day.isoformat()
    raw_px_dates = set(str(d) for d in px.index)
    px_latest_date = str(px.index[-1])

    # Build series, NEVER overwriting frozen intervals even if new history lacks anchor or HSI fails.
    series: dict[str, pd.Series] = {}
    comp_reasons: list[str] = []
    hsi_reasons: list[str] = []

    if target_str not in raw_px_dates:
        comp_reasons.append(f"个股原始日线面板缺少目标日期 {target_str}")
    if px_latest_date != target_str:
        comp_reasons.append(
            f"个股日线面板最新日期为 {px_latest_date}，未达目标日期 {target_str}"
        )
    is_target_month_end = target_day == last_trading_day_of_month("hk", target_day.year, target_day.month)
    if rebal_incomplete or (is_target_month_end and target_str not in mes):
        comp_reasons.append(f"当月调仓结果不完整 ({target_str})")
    def _valid_pos_price(val: Any) -> bool:
        return (
            isinstance(val, (int, float))
            and not isinstance(val, bool)
            and math.isfinite(float(val))
            and float(val) > 0
        )

    def _missing_held_for_key(key: str, last_f_date: str | None) -> list[str]:
        """Verify all active held constituents have valid prices (no silent drop/reweight)."""
        if last_f_date is not None:
            tail_d = [d for d in dates if d > last_f_date and d <= target_str]
            check_d = ([last_f_date] + tail_d) if tail_d else ([target_str] if target_str in date_pos else [])
        else:
            check_d = [target_str] if target_str in date_pos else []
        missing: set[str] = set()
        for d_chk in check_d:
            pos = date_pos.get(d_chk)
            if pos is None:
                continue
            seg_idx = None
            for k_seg, rpos in enumerate(rebal_pos):
                if rpos <= pos:
                    seg_idx = k_seg
            if seg_idx is None:
                continue
            seg_codes = selections[key][seg_idx]
            for c in seg_codes:
                if c not in px.columns or d_chk not in px.index:
                    missing.add(c)
                    continue
                px_val = px.loc[d_chk, c]
                if pd.isna(px_val) or not _valid_pos_price(float(px_val)):
                    missing.add(c)
                    continue
                if d_chk == target_str and not _valid_pos_price(uni_spot_prices.get(c)):
                    missing.add(c)
        return sorted(missing)

    for key in idx_defs:
        lvl_full = build_index(px_ff, pd.Index(dates), rebal_pos, selections[key])
        if frozen_series_raw.get("dates") and frozen_series_raw.get(key):
            f_ser = pd.Series(
                frozen_series_raw[key],
                index=pd.Index(frozen_series_raw["dates"]),
                dtype=float,
            )
            last_f_date = str(f_ser.index[-1])
            missing_held = _missing_held_for_key(key, last_f_date)
            if missing_held:
                # NEVER reweight remaining constituents or append incomplete day!
                series[key] = f_ser
                comp_reasons.append(
                    f"{key} 持有成分缺少必要价格 {len(missing_held)} 只 (如 {', '.join(missing_held[:5])})"
                )
                continue
            tail_dates = [d for d in lvl_full.index if d > last_f_date]
            if not tail_dates:
                series[key] = f_ser
            elif last_f_date in lvl_full.index and float(lvl_full.loc[last_f_date]) > 0:
                scale = float(f_ser.iloc[-1]) / float(lvl_full.loc[last_f_date])
                tail = lvl_full.loc[tail_dates] * scale
                series[key] = pd.concat([f_ser, tail])
            else:
                # Keep frozen series untouched rather than falling back to full recalc!
                series[key] = f_ser
                comp_reasons.append(
                    f"{key} 新增数据缺少最后冻结基准日 {last_f_date}，已保留冻结历史"
                )
        else:
            missing_held = _missing_held_for_key(key, None)
            if missing_held:
                comp_reasons.append(
                    f"{key} 持有成分缺少必要价格 {len(missing_held)} 只 (如 {', '.join(missing_held[:5])})"
                )
            series[key] = lvl_full

    common = series["smallcap"].index.intersection(series["nonggt"].index)
    out: dict[str, Any] = {"dates": list(common)}
    for key in idx_defs:
        s = series[key].reindex(common)
        if not frozen_series_raw.get(key):
            s = s / s.iloc[0] * BASE
        out[key] = [round(float(v), 2) if pd.notna(v) else None for v in s]
        series[key] = s

    # HSI benchmark: always preserve frozen history up to last valid frozen date; backfill gaps when recovered.
    # Require target_str in un-ffilled raw_hsi_dates before considering series complete/freezable.
    hsi_fetched, raw_hsi_dates = _fetch_hsi_history(common, session=hsi_session)
    if hsi_fetched is None:
        hsi_reasons.append("恒生指数历史行情拉取失败")
    elif target_str not in raw_hsi_dates:
        hsi_reasons.append(f"恒生指数原始记录缺少目标日期 {target_str}")
    if frozen_series_raw.get("dates") and frozen_series_raw.get("hsi"):
        f_hsi = pd.Series(
            frozen_series_raw["hsi"],
            index=pd.Index(frozen_series_raw["dates"]),
            dtype=float,
        )
        f_hsi_valid = f_hsi.dropna()
        if not f_hsi_valid.empty:
            last_f_date = str(f_hsi_valid.index[-1])
            tail_dates = [d for d in common if d > last_f_date]
            if not tail_dates:
                out["hsi"] = [round(float(v), 2) if pd.notna(v) else None for v in f_hsi_valid.reindex(common)]
            elif (
                hsi_fetched is not None
                and last_f_date in hsi_fetched.index
                and pd.notna(hsi_fetched.loc[last_f_date])
                and float(hsi_fetched.loc[last_f_date]) > 0
            ):
                scale = float(f_hsi_valid.iloc[-1]) / float(hsi_fetched.loc[last_f_date])
                combined_hsi = pd.concat([f_hsi_valid, hsi_fetched.loc[tail_dates] * scale]).reindex(common)
                out["hsi"] = [round(float(v), 2) if pd.notna(v) else None for v in combined_hsi]
            else:
                # Never overwrite frozen HSI with None; keep frozen values for recorded dates.
                out["hsi"] = [round(float(v), 2) if pd.notna(v) else None for v in f_hsi_valid.reindex(common)]
                if hsi_fetched is not None:
                    hsi_reasons.append(
                        f"恒生指数历史行情缺少基准日 {last_f_date}，已保留冻结历史"
                    )
        else:
            out["hsi"] = [round(float(v), 2) if pd.notna(v) else None for v in hsi_fetched] if hsi_fetched is not None else None
    else:
        out["hsi"] = [round(float(v), 2) if pd.notna(v) else None for v in hsi_fetched] if hsi_fetched is not None else None

    history_stale_reasons: list[str] = comp_reasons + hsi_reasons
    history_stale = bool(history_stale_reasons)

    last_scores = momentum_scores(px_ff, px, tv, uni, len(dates) - 1, ggt_codes)
    cons: dict[str, list[dict[str, Any]]] = {}
    for key in idx_defs:
        codes = selections[key][-1]
        detail = []
        for c in codes:
            row = last_scores.loc[c] if (last_scores is not None and c in last_scores.index) else None
            name_val = c
            if c in uni.index:
                name_val = str(uni.loc[c, "name_cn"] if pd.notna(uni.loc[c, "name_cn"]) else uni.loc[c, "name"])
            detail.append(
                {
                    "code": c,
                    "name": name_val,
                    "score": round(float(row["score"]), 2) if row is not None else None,
                    "mktcap_e8": round(float(row["mktcap_e8"]), 1) if row is not None else None,
                    "ret12": round(float(row["ret12"]) * 100.0, 1) if row is not None else None,
                }
            )
        detail.sort(key=lambda x: -(x["score"] if x["score"] is not None else -99.0))
        cons[key] = detail

    perf: dict[str, dict[str, float]] = {}
    for key in idx_defs:
        s = series[key]
        n_years = max(len(s) / 245.0, 1 / 245.0)
        perf[key] = {
            "level": round(float(s.iloc[-1]), 1),
            "total_ret_pct": round((float(s.iloc[-1]) / BASE - 1.0) * 100.0, 1),
            "cagr_pct": round(((float(s.iloc[-1]) / BASE) ** (1.0 / n_years) - 1.0) * 100.0, 1),
            "vol_pct": round(float(s.pct_change().std() * math.sqrt(245.0)) * 100.0, 1),
            "max_dd_pct": round(float(((s / s.cummax()) - 1.0).min()) * 100.0, 1),
        }

    prev_meta = load_json_safe(st_dir / "index_meta.json", {}) or {}
    now_str = now_hk().strftime("%Y-%m-%d %H:%M:%S")
    completeness_ok = not history_stale
    should_persist = bool(out["dates"]) and completeness_ok and freeze_on_stale

    hsi_st = make_source_result(
        "hsi_history",
        ok=not bool(hsi_reasons),
        stale=bool(hsi_reasons),
        reason="；".join(hsi_reasons) if hsi_reasons else None,
        as_of=target_str,
        label="恒生指数历史",
    )
    comp_st = make_source_result(
        "index_compute",
        ok=not bool(comp_reasons),
        stale=bool(comp_reasons),
        reason="；".join(comp_reasons) if comp_reasons else None,
        as_of=target_str,
        label="动量日度计算",
    )
    compute_statuses = {
        "hsi_history": hsi_st.to_dict(),
        "index_compute": comp_st.to_dict(),
    }

    if update_last_success:
        status_doc = SourceStatusStore(st_dir).update_sources(
            [hsi_st, comp_st],
            required=["hsi_history", "index_compute"],
            now_str=f"{now_str} 北京时间",
        )
        resolved_last_success = status_doc["overall"]["last_success_at"]
    else:
        resolved_last_success = prev_meta.get("last_success_at")

    meta = {
        "updated": now_hk().isoformat(timespec="seconds"),
        "base_date": out["dates"][0],
        "last_date": out["dates"][-1],
        "launch_date": launch_date,
        "last_rebalance_date": mes[-1],
        "rebalances": {k: rebal_info[k] for k in idx_defs},
        "perf": perf,
        "series_stale": history_stale,
        "series_stale_reason": "；".join(history_stale_reasons) if history_stale_reasons else None,
        "stale": history_stale,
        "stale_reason": "；".join(history_stale_reasons) if history_stale_reasons else None,
        "completeness_ok": completeness_ok,
        "froze": bool(persist and should_persist),
        "ok": completeness_ok,
        "last_success_at": resolved_last_success,
        "source_statuses": compute_statuses,
        "params": {
            "top_n": TOP_N,
            "min_turnover_wan": MIN_TURNOVER_WAN,
            "min_turnover_basis": "近3个月日成交额中位数 ≥ 100 万港元",
            "smallcap_limit_e8": SMALLCAP_LIMIT_E8,
            "min_price": MIN_PRICE,
        },
    }
    frozen_payload = {
        "launch_date": launch_date,
        "rebalance_snapshots": frozen_rebals,
        "frozen_series": out,
    }
    result: dict[str, Any] = {
        "series": out,
        "constituents": cons,
        "meta": meta,
        "frozen_payload": frozen_payload,
        "can_freeze": bool(should_persist),
    }
    if persist and should_persist:
        persist_compute_result(st_dir, result)
    return result


def _extract_quote_date(ts_raw: Any) -> str | None:
    if not ts_raw:
        return None
    m = _QUOTE_DATE_RE.search(str(ts_raw))
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _expected_hk_trade_date(cur: datetime, sess_state: dict[str, Any] | None = None) -> str:
    d = cur.date()
    hm = cur.hour * 60 + cur.minute
    sess = sess_state or market_session_state("hk", cur)
    am_start = int(sess["am_start"]) if sess.get("am_start") is not None else (9 * 60 + 30)
    if sess.get("is_trading_day") and hm >= am_start:
        return d.isoformat()
    return previous_trading_day("hk", d).isoformat()


def compute_intraday_snapshot(
    *,
    state_dir: str | Path | None = None,
    daily_state_dir: str | Path | None = None,
    now: datetime | None = None,
    session: Any = None,
    persist_status: bool = True,
    series_override: dict[str, Any] | None = None,
    constituents_override: dict[str, Any] | None = None,
    meta_override: dict[str, Any] | None = None,
    daily_sources_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute HK momentum intraday valuation with pre-open, cutoff-date & quote-date checks."""
    st_dir = resolve_state_dir("hk_momentum", state_dir)
    if daily_state_dir and (
        (Path(daily_state_dir) / "constituents.json").is_file()
        or (Path(daily_state_dir) / "status.json").is_file()
    ):
        read_dir = Path(daily_state_dir)
    else:
        read_dir = st_dir
    live_cache_path = st_dir / "live.json"
    prev_live = load_json_safe(live_cache_path, {}) or {}
    if daily_sources_override is not None:
        daily_sources = {
            k: dict(v)
            for k, v in daily_sources_override.items()
            if isinstance(v, dict) and k not in ("hk_intraday_spot", "hsi_spot")
        }
    else:
        daily_status_doc = SourceStatusStore(read_dir).load()
        daily_sources = {
            k: dict(v)
            for k, v in (daily_status_doc.get("sources") or {}).items()
            if isinstance(v, dict) and k not in ("hk_intraday_spot", "hsi_spot")
        }
    disk_meta = meta_override if meta_override is not None else (load_json_safe(read_dir / "index_meta.json", {}) or {})
    month_end_failed_reason = (
        str(disk_meta.get("stale_reason"))
        if disk_meta.get("month_end_failed") and disk_meta.get("stale_reason")
        else None
    )

    cur = (now or now_hk()).astimezone(HK_TZ)
    sess_state = market_session_state("hk", cur)
    cons = constituents_override if constituents_override is not None else load_json_safe(read_dir / "constituents.json")
    series = series_override if series_override is not None else load_json_safe(read_dir / "index_series.json")
    now_str = cur.strftime("%Y-%m-%d %H:%M:%S")

    base_meta = {
        "as_of": now_str,
        "as_of_iso": cur.isoformat(timespec="seconds"),
        "tz": "北京时间",
        "trade_date": sess_state["trade_date"],
        "is_trading_day": sess_state["is_trading_day"],
        "windows": sess_state.get("windows") or [],
        "trading": sess_state["in_trading_window"],
        "market_status": sess_state["status"],
        "market_status_label": sess_state["label"],
        "using_default_schedule": sess_state.get("using_default_schedule", False),
        "session_note": sess_state.get("session_note"),
        "month_end_failed": bool(disk_meta.get("month_end_failed")),
    }

    def _stale_entry(key: str, last_close: Any = None) -> dict[str, Any] | None:
        prev_entry = dict(prev_live.get(key) or {})
        if prev_entry:
            prev_entry["stale"] = True
            return prev_entry
        if last_close is None:
            return None
        return {
            "level": last_close,
            "pct": None,
            "prev_close_level": last_close,
            "n_quoted": 0,
            "n_expected": len(cons.get(key) or []) if cons else 0,
            "missing_codes": [],
            "partial_quotes": False,
            "stale": True,
            "source": "stale_close",
        }

    def _finalize(
        out_dict: dict[str, Any],
        spot_st: Any,
        hsi_spot_st: Any,
    ) -> dict[str, Any]:
        # Merge daily failing sources + intraday SourceStatus objects into one overall evaluation
        stale_daily_banner = format_source_statuses(daily_sources)
        if month_end_failed_reason or stale_daily_banner or not spot_st.is_clean or not hsi_spot_st.is_clean:
            out_dict["stale"] = True
        reasons = []
        if month_end_failed_reason:
            reasons.append(month_end_failed_reason)
        if stale_daily_banner and stale_daily_banner not in (month_end_failed_reason or ""):
            reasons.append(stale_daily_banner)
        if out_dict.get("stale_reason"):
            for part in str(out_dict["stale_reason"]).split("；"):
                if part and part not in reasons:
                    reasons.append(part)
        out_dict["stale_reason"] = "；".join(reasons) if reasons else None
        intraday_map = {
            "hk_intraday_spot": spot_st.to_dict(),
            "hsi_spot": hsi_spot_st.to_dict(),
        }
        out_dict["intraday_source_statuses"] = intraday_map
        if persist_status:
            store = SourceStatusStore(st_dir)
            prior_overall_ok = (
                store.load().get("overall", {}).get("last_success_at")
                or prev_live.get("last_success_at")
            )
            daily_objs = [
                SourceStatus.from_dict(v)
                for v in daily_sources.values()
                if isinstance(v, dict) and v.get("name")
            ]
            if month_end_failed_reason and not any(not d.is_clean for d in daily_objs):
                daily_objs.append(
                    make_source_result(
                        "month_end_rebalance",
                        ok=False,
                        stale=True,
                        reason=month_end_failed_reason,
                        label="月末调仓",
                    )
                )
            elif out_dict["stale"] and not spot_st.is_clean and hsi_spot_st.is_clean and not daily_objs:
                pass
            all_req = list(dict.fromkeys([d.name for d in daily_objs] + ["hk_intraday_spot", "hsi_spot"]))
            sdoc = store.update_sources(
                daily_objs + [spot_st, hsi_spot_st],
                required=all_req,
                now_str=now_str,
            )
            merged_sources = dict(daily_sources)
            for k in ("hk_intraday_spot", "hsi_spot"):
                if k in sdoc["sources"]:
                    merged_sources[k] = sdoc["sources"][k]
            out_dict["sources"] = merged_sources
            out_dict["overall"] = sdoc["overall"]
            if (not out_dict["stale"]) and bool(sdoc["overall"].get("ok")):
                out_dict["last_success_at"] = sdoc["overall"]["last_success_at"]
            else:
                out_dict["last_success_at"] = prior_overall_ok
        else:
            merged_sources = dict(daily_sources)
            merged_sources.update(intraday_map)
            out_dict["sources"] = merged_sources
        if not out_dict["stale"]:
            atomic_write_json(live_cache_path, out_dict)
        return out_dict

    if sess_state.get("calendar_error"):
        cal_msg = str(sess_state["calendar_error"])
        return _finalize(
            {
                **base_meta,
                "stale": True,
                "stale_reason": cal_msg,
                "last_success_at": prev_live.get("last_success_at"),
                "smallcap": _stale_entry("smallcap"),
                "nonggt": _stale_entry("nonggt"),
                "hsi": _stale_entry("hsi"),
            },
            make_source_result("hk_intraday_spot", ok=False, stale=True, reason=cal_msg, label="港股成分实时报价"),
            make_source_result("hsi_spot", ok=False, stale=True, reason=cal_msg, label="恒指实时报价"),
        )

    if not cons or not series or not series.get("dates"):
        msg = "缺少港股动量日度收盘数据与成分列表"
        return _finalize(
            {
                **base_meta,
                "stale": True,
                "stale_reason": msg,
                "last_success_at": prev_live.get("last_success_at"),
                "smallcap": _stale_entry("smallcap"),
                "nonggt": _stale_entry("nonggt"),
                "hsi": _stale_entry("hsi"),
            },
            make_source_result("hk_intraday_spot", ok=False, stale=True, reason=msg, label="港股成分实时报价"),
            make_source_result("hsi_spot", ok=False, stale=True, reason=msg, label="恒指实时报价"),
        )

    if sess_state["status"] == "pre_open":
        pre_msg = "港股盘前尚未开盘（09:30 后计算当日盘中估值）"
        return _finalize(
            {
                **base_meta,
                "stale": True,
                "stale_reason": pre_msg,
                "last_success_at": prev_live.get("last_success_at"),
                "smallcap": _stale_entry("smallcap", series["smallcap"][-1] if series.get("smallcap") else None),
                "nonggt": _stale_entry("nonggt", series["nonggt"][-1] if series.get("nonggt") else None),
                "hsi": _stale_entry("hsi"),
            },
            make_source_result("hk_intraday_spot", ok=False, stale=True, reason=pre_msg, label="港股成分实时报价"),
            make_source_result("hsi_spot", ok=False, stale=True, reason=pre_msg, label="恒指实时报价"),
        )

    today_hkt = cur.date().isoformat()
    series_last = str(series["dates"][-1])
    series_has_today = series_last == today_hkt
    expected_prev_td = previous_trading_day("hk", cur.date()).isoformat()
    daily_series_lagging = (not series_has_today) and (series_last < expected_prev_td)
    expected_q_date = _expected_hk_trade_date(cur, sess_state)

    codes = sorted({c["code"] for k in ("smallcap", "nonggt") for c in (cons.get(k) or [])})
    spot, spot_errs = fetch_spot_batch(codes, session=session)
    hsi_spot, hsi_err = fetch_hsi_spot(session=session)

    stale_reasons: list[str] = []
    spot_reasons: list[str] = []
    if spot_errs:
        stale_reasons.extend(spot_errs[:3])
        spot_reasons.extend(spot_errs[:3])
    if daily_series_lagging:
        lag_msg = f"日频收盘数据滞后（当前至 {series_last}，期望至少截至上一交易日 {expected_prev_td}），暂停计算最新点位"
        stale_reasons.append(lag_msg)
        spot_reasons.append(lag_msg)

    out: dict[str, Any] = {**base_meta, "daily_close_date": series_last}

    any_index_stale = daily_series_lagging
    for key in ("smallcap", "nonggt"):
        if series_has_today and len(series[key]) >= 2:
            lvl, prev_lvl = series[key][-1], series[key][-2]
            out[key] = {
                "level": lvl,
                "pct": round((lvl / prev_lvl - 1.0) * 100.0, 2) if prev_lvl else 0.0,
                "prev_close_level": prev_lvl,
                "n_quoted": len(cons.get(key) or []),
                "n_expected": len(cons.get(key) or []),
                "missing_codes": [],
                "partial_quotes": False,
                "stale": False,
                "source": "daily_close",
            }
            continue

        if daily_series_lagging:
            out[key] = _stale_entry(key, series[key][-1] if series.get(key) else None)
            continue

        members = [c["code"] for c in (cons.get(key) or [])]
        n_expected = len(members)
        fresh_quotes = {
            c: spot[c]
            for c in members
            if c in spot and _extract_quote_date(spot[c].get("ts")) == expected_q_date
        }
        missing_codes = [c for c in members if c not in fresh_quotes]
        coverage = (len(fresh_quotes) / n_expected) if n_expected else 0.0
        if coverage < 0.5:
            any_index_stale = True
            cov_msg = f"{key} 有效当日报价不足 ({len(fresh_quotes)}/{n_expected}，期望日期 {expected_q_date})"
            stale_reasons.append(cov_msg)
            spot_reasons.append(cov_msg)
            out[key] = _stale_entry(key, series[key][-1])
            if out[key] and not prev_live.get(key):
                out[key]["n_quoted"] = len(fresh_quotes)
                out[key]["missing_codes"] = missing_codes
                out[key]["partial_quotes"] = True
            continue

        chg = sum(q["pct"] for q in fresh_quotes.values()) / n_expected / 100.0
        last_close = float(series[key][-1])
        out[key] = {
            "level": round(last_close * (1.0 + chg), 2),
            "pct": round(chg * 100.0, 2),
            "prev_close_level": last_close,
            "n_quoted": len(fresh_quotes),
            "n_expected": n_expected,
            "missing_codes": missing_codes,
            "partial_quotes": bool(missing_codes),
            "stale": False,
            "source": "intraday",
        }

    hsi_q_date = _extract_quote_date((hsi_spot or {}).get("ts"))
    hsi_reason: str | None = None
    if hsi_spot and hsi_q_date == expected_q_date:
        out["hsi"] = {"value": hsi_spot["value"], "pct": hsi_spot["pct"], "stale": False}
    else:
        any_index_stale = True
        hsi_reason = (
            hsi_err or f"恒指报价缺少有效日期或非 {expected_q_date} (ts={((hsi_spot or {}).get('ts') or '无')})"
        )
        stale_reasons.append(hsi_reason)
        prev_hsi = dict(prev_live.get("hsi") or {})
        if prev_hsi:
            prev_hsi["stale"] = True
        out["hsi"] = prev_hsi or None

    out["stale"] = any_index_stale
    out["stale_reason"] = "；".join(stale_reasons) if stale_reasons else None
    spot_st = make_source_result(
        "hk_intraday_spot",
        ok=not bool(spot_reasons),
        stale=bool(spot_reasons),
        reason="；".join(spot_reasons) if spot_reasons else None,
        as_of=now_str,
        label="港股成分实时报价",
    )
    hsi_st = make_source_result(
        "hsi_spot",
        ok=not bool(hsi_reason),
        stale=bool(hsi_reason),
        reason=hsi_reason,
        as_of=now_str,
        label="恒指实时报价",
    )
    return _finalize(out, spot_st, hsi_st)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute HK Momentum Index daily series & constituents.")
    parser.add_argument("--state-dir", default=None)
    args = parser.parse_args()
    res = compute_all(state_dir=args.state_dir)
    print(
        json.dumps(
            {
                "base_date": res["meta"]["base_date"],
                "last_date": res["meta"]["last_date"],
                "launch_date": res["meta"]["launch_date"],
                "perf": res["meta"]["perf"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
