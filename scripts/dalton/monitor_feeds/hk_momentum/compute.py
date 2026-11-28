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
import logging
import math
from pathlib import Path
import re
from typing import Any, Iterable
import warnings

import pandas as pd

from scripts.dalton.monitor_feeds.common import (
    HK_TZ,
    SourceStatus,
    SourceStatusStore,
    atomic_write_json,
    format_source_statuses,
    last_trading_day_of_month,
    load_json_safe,
    make_source_result,
    market_session_state,
    now_hk,
    previous_trading_day,
    resolve_market_trade_date,
    resolve_state_dir,
    trading_days_in_range,
)
from scripts.dalton.monitor_feeds.hk_momentum.fetch_data import (
    backfill_missing_suspensions,
    fetch_hsi_spot,
    fetch_spot_batch,
    is_spot_suspended,
    load_suspended_history,
    record_suspended_for_date,
    spot_meta_state,
)

warnings.filterwarnings("ignore", category=RuntimeWarning)
logger = logging.getLogger(__name__)

TOP_N = 50
MIN_TURNOVER_WAN = 100.0  # 3M median daily turnover >= 100万港元
MIN_PRICE = 0.10
SMALLCAP_LIMIT_E8 = 50.0  # 50亿港元
BASE = 1000.0
MAX_SUSPENDED_TRADING_DAYS = 20

_QUOTE_DATE_RE = re.compile(r"(\d{4})[/-]?(\d{2})[/-]?(\d{2})")


def load_panel(
    state_dir: Path,
    *,
    required_codes: set[str] | None = None,
    universe_df: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    uni_csv = state_dir / "universe.csv"
    if universe_df is not None:
        uni_all = universe_df.copy()
    elif uni_csv.is_file():
        uni_all = pd.read_csv(uni_csv, dtype={"code": str})
    else:
        raise RuntimeError("缺少成分文件 universe.csv（首跑尚未生成成分名单），无法加载行情面板")
    if "code" not in uni_all.columns or uni_all.empty:
        raise RuntimeError("港股成分名单为空，无法加载行情面板")
    uni_all["code"] = uni_all["code"].astype(str).str.strip()
    if "price" in uni_all.columns:
        uni = uni_all[uni_all["price"].notna()].set_index("code")
    else:
        uni = uni_all.iloc[0:0].set_index("code")
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
    *,
    excluded_codes: set[str] | None = None,
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
    uni_excl: set[str] = set()
    if "excluded" in uni.columns:
        uni_excl = {
            str(c).strip().zfill(5)
            for c in uni.index[uni["excluded"].fillna(False).astype(bool)]
            if str(c).strip()
        }
    all_excl = {str(c).strip().zfill(5) for c in (excluded_codes or ()) if str(c).strip()} | uni_excl
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
    if all_excl:
        elig = elig & (~px_ff.columns.astype(str).str.strip().str.zfill(5).isin(all_excl))
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
    if all_excl:
        df = df[~df.index.astype(str).str.strip().str.zfill(5).isin(all_excl)]
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
    segment_removals: list[dict[str, str]] | None = None,
) -> pd.Series:
    level = pd.Series(index=dates, dtype=float)
    lvl = float(initial_level)
    for k, (t_idx, codes) in enumerate(zip(rebal_positions, selections)):
        end_idx = rebal_positions[k + 1] if k + 1 < len(rebal_positions) else len(dates) - 1
        rem_map = (
            segment_removals[k]
            if (segment_removals is not None and k < len(segment_removals) and segment_removals[k])
            else {}
        )
        eff_positions_set: set[int] = set()
        for d_eff in rem_map.values():
            d_eff_s = str(d_eff)
            for p in range(t_idx, end_idx + 1):
                if str(dates[p]) >= d_eff_s:
                    if p > t_idx:
                        eff_positions_set.add(p)
                    break
        split_effs = sorted(eff_positions_set)
        sub_ranges: list[tuple[int, int, str]] = []
        if not split_effs:
            sub_ranges.append((t_idx, end_idx, str(dates[t_idx])))
        else:
            sub_ranges.append((t_idx, split_effs[0] - 1, str(dates[t_idx])))
            for s_i, eff_p in enumerate(split_effs):
                sub_anchor = eff_p - 1
                sub_end = (split_effs[s_i + 1] - 1) if (s_i + 1 < len(split_effs)) else end_idx
                sub_ranges.append((sub_anchor, sub_end, str(dates[eff_p])))
        for sub_anchor, sub_end, eff_date_str in sub_ranges:
            valid_codes = [
                c
                for c in codes
                if c in px_ff.columns
                and not (
                    (c in rem_map and str(rem_map[c]) <= eff_date_str)
                    or (str(c).strip().zfill(5) in rem_map and str(rem_map[str(c).strip().zfill(5)]) <= eff_date_str)
                )
            ]
            if not valid_codes:
                continue
            p0 = px_ff.iloc[sub_anchor][valid_codes]
            valid_codes = [c for c in valid_codes if pd.notna(p0[c]) and p0[c] > 0]
            if not valid_codes:
                continue
            seg = px_ff.iloc[sub_anchor : sub_end + 1][valid_codes]
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
        if not isinstance(rows, list) or not rows:
            return None, set()
        raw_map: dict[str, float] = {}
        for x in rows:
            if not isinstance(x, (list, tuple)) or len(x) < 3 or not x[0]:
                return None, set()
            d_str = str(x[0]).strip()
            if d_str in raw_map:
                return None, set()
            px = float(x[2])
            if not math.isfinite(px) or px <= 0:
                return None, set()
            raw_map[d_str] = px
        raw_dates = set(raw_map.keys())
        hsi = pd.Series(raw_map, dtype=float).reindex(common_dates)
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
    universe_df: pd.DataFrame | None = None,
    excluded_codes: Iterable[str] | None = None,
    backfill_suspensions: bool = False,
    yf_module: Any = None,
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
    frozen_exclusions: dict[str, Any] = dict(frozen.get("rebalance_exclusions") or {})
    frozen_series_raw: dict[str, Any] = frozen.get("frozen_series") or {}
    frozen_removals: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    raw_susp_rem = frozen.get("suspended_removals")
    if isinstance(raw_susp_rem, dict):
        for idx_k, r_map in raw_susp_rem.items():
            if not isinstance(r_map, dict):
                continue
            parsed_rmap: dict[str, dict[str, dict[str, Any]]] = {}
            for r_dt, c_map in r_map.items():
                if not isinstance(c_map, dict):
                    continue
                parsed_cmap: dict[str, dict[str, Any]] = {}
                for c_code, info_val in c_map.items():
                    c_norm = str(c_code).strip().zfill(5)
                    if not c_norm:
                        continue
                    if isinstance(info_val, dict) and info_val.get("effective_date"):
                        entry_copy = dict(info_val)
                        entry_copy["code"] = c_norm
                        entry_copy["effective_date"] = str(info_val["effective_date"]).strip()[:10]
                        parsed_cmap[c_norm] = entry_copy
                    elif isinstance(info_val, str) and info_val.strip():
                        parsed_cmap[c_norm] = {
                            "code": c_norm,
                            "effective_date": info_val.strip()[:10],
                            "note": "已停牌超过 20 个交易日，移出计算",
                        }
                if parsed_cmap:
                    parsed_rmap[str(r_dt)] = parsed_cmap
            if parsed_rmap:
                frozen_removals[str(idx_k)] = parsed_rmap
    elif isinstance(frozen.get("removed_suspended_codes"), list) and frozen_rebals:
        # Legacy migration: attach top-level removed_suspended_codes to the latest frozen rebalance
        legacy_last_rebal = sorted(frozen_rebals.keys())[-1]
        legacy_f_dates = frozen_series_raw.get("dates") or []
        legacy_eff = str(legacy_f_dates[-1]) if legacy_f_dates else legacy_last_rebal
        for idx_k, idx_codes in (frozen_rebals.get(legacy_last_rebal) or {}).items():
            if not isinstance(idx_codes, list):
                continue
            idx_code_set = {str(c).strip().zfill(5) for c in idx_codes if str(c).strip()}
            for rc in frozen.get("removed_suspended_codes") or []:
                rc_norm = str(rc).strip().zfill(5)
                if rc_norm and rc_norm in idx_code_set:
                    frozen_removals.setdefault(str(idx_k), {}).setdefault(legacy_last_rebal, {})[rc_norm] = {
                        "code": rc_norm,
                        "effective_date": legacy_eff,
                        "note": "已停牌超过 20 个交易日，移出计算",
                    }
    prev_suspended_on_last_date: set[str] = {
        str(c).strip().zfill(5)
        for c in (frozen.get("suspended_on_last_date") or [])
        if str(c).strip()
    }
    held_frozen_codes: set[str] = {
        str(c)
        for snap_map in frozen_rebals.values()
        if isinstance(snap_map, dict)
        for code_list in snap_map.values()
        if isinstance(code_list, list)
        for c in code_list
        if c
    }

    uni_csv = st_dir / "universe.csv"
    if universe_df is not None:
        uni_all_df = universe_df.copy()
        if uni_all_df.empty:
            raise RuntimeError("首次运行缺少历史 universe.csv 且本次未能获取港交所证券名单，无法计算动量指数")
    elif uni_csv.is_file():
        uni_all_df = pd.read_csv(uni_csv, dtype={"code": str})
        if uni_all_df.empty:
            raise RuntimeError("成分文件 universe.csv 为空，无法计算动量指数")
    else:
        raise RuntimeError("缺少成分文件 universe.csv 且未获取到本次证券名单，无法计算动量指数")

    uni, px, tv = load_panel(
        st_dir,
        required_codes=held_frozen_codes,
        universe_df=uni_all_df,
    )
    uni_spot_prices: dict[str, Any] = {
        str(r["code"]): r.get("price")
        for _, r in uni_all_df.iterrows()
        if pd.notna(r.get("code"))
    }
    ggt_json_path = st_dir / "ggt_list.json"
    ggt_raw = load_json_safe(ggt_json_path, {}) or {}
    if ggt_raw:
        ggt_codes = set(ggt_raw.keys())
    elif "is_ggt" in uni_all_df.columns:
        ggt_codes = set(
            uni_all_df.loc[uni_all_df["is_ggt"].fillna(False).astype(bool), "code"].astype(str)
        )
    else:
        ggt_codes = set()
    uni_meta = load_json_safe(st_dir / "universe_meta.json", {}) or {}
    excl_set: set[str] = {
        str(c).strip().zfill(5) for c in (excluded_codes or ()) if str(c).strip()
    }
    if "excluded" in uni_all_df.columns:
        excl_set.update(
            str(c).strip().zfill(5)
            for c in uni_all_df.loc[uni_all_df["excluded"].fillna(False).astype(bool), "code"]
            if pd.notna(c) and str(c).strip()
        )
    if isinstance(uni_meta, dict):
        for k_excl in ("excluded_codes", "ggt_skipped_codes"):
            raw_excl = uni_meta.get(k_excl)
            if isinstance(raw_excl, list):
                excl_set.update(str(c).strip().zfill(5) for c in raw_excl if str(c).strip())
    excluded_sorted = sorted(excl_set)
    excluded_reason = (
        (uni_meta.get("ggt_skipped_note") if isinstance(uni_meta, dict) else None)
        or (f"身份未确认排除 {len(excluded_sorted)} 只" if excluded_sorted else None)
    )
    px_ff = px.ffill()
    dates = list(px.index)
    if not dates:
        if uni.empty and not held_frozen_codes:
            raise RuntimeError("港股有效现货报价成分为空（可能非交易时段或现货行情失败），无法计算动量指数")
        raise RuntimeError("港股日线面板为空，无法计算动量指数")

    target_day = resolve_market_trade_date(
        "hk",
        now=now_hk() if as_of_date is None else None,
        as_of_date=as_of_date,
    )
    ref_date = target_day
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

        scores = momentum_scores(
            px_ff, px, tv, uni, t_idx, ggt_codes, excluded_codes=excl_set
        )
        if scores is None:
            rebal_incomplete = True
            continue
        rebal_pos.append(t_idx)
        frozen_rebals[d] = {}
        if excluded_sorted:
            frozen_exclusions[d] = {
                "codes": excluded_sorted,
                "reason": excluded_reason,
            }
        for key, filt in idx_defs.items():
            pool = filt(scores)
            top = pool.nlargest(min(TOP_N, len(pool)), "score")
            codes = [c for c in list(top.index) if str(c).strip().zfill(5) not in excl_set]
            if not codes:
                rebal_incomplete = True
            selections[key].append(codes)
            frozen_rebals[d][key] = codes
            rebal_entry: dict[str, Any] = {
                "date": d,
                "pool": int(len(pool)),
                "n": int(len(codes)),
                "frozen": False,
            }
            if excluded_sorted:
                rebal_entry["excluded_count"] = len(excluded_sorted)
                rebal_entry["excluded_codes"] = excluded_sorted
                rebal_entry["excluded_reason"] = excluded_reason
            rebal_info[key].append(rebal_entry)

    if not rebal_pos:
        raise RuntimeError("没有满足动量选股条件的调仓日（候选池为空或样本不足）")

    # Determine expected target trading day & verify raw un-ffilled coverage before freezing.
    target_str = target_day.isoformat()
    can_use_csv_spot, can_use_meta_suspended = spot_meta_state(uni_meta, target_str)
    spot_info_by_code: dict[str, dict[str, Any]] = {}
    if can_use_csv_spot:
        for _, r in uni_all_df.iterrows():
            c_raw = r.get("code")
            if pd.isna(c_raw):
                continue
            c_key = str(c_raw).strip()
            if not c_key:
                continue
            entry_spot: dict[str, Any] = {"price": r.get("price")}
            for col_name in ("prev_close", "volume", "turnover", "trading_status", "status", "quote_date", "ts"):
                if col_name in uni_all_df.columns:
                    val = r.get(col_name)
                    if pd.notna(val):
                        entry_spot[col_name] = val
            spot_info_by_code[c_key] = entry_spot
    if can_use_meta_suspended and isinstance(uni_meta, dict) and isinstance(uni_meta.get("suspended_spot"), dict):
        for scode, sentry in uni_meta["suspended_spot"].items():
            if isinstance(sentry, dict):
                c_key = str(scode).strip()
                if c_key:
                    merged_spot = dict(spot_info_by_code.get(c_key) or {})
                    merged_spot.update(sentry)
                    spot_info_by_code[c_key] = merged_spot

    # Merge per-trading-day confirmed suspension records from:
    # 1) suspended_history.json (written on every clean hk_spot run, even if freeze failed)
    # 2) frozen_history.json["suspended_by_date"] (persisted on prior freezes)
    # 3) legacy frozen_history.json["suspended_on_last_date"] (ONLY for frozen_last_date itself)
    # 4) current-run clean spot quotes for target_str
    confirmed_suspended_by_date: dict[str, set[str]] = load_suspended_history(st_dir)
    raw_frozen_sbd = frozen.get("suspended_by_date")
    if isinstance(raw_frozen_sbd, dict):
        for d_k, c_list in raw_frozen_sbd.items():
            d_s = str(d_k).strip()[:10]
            if d_s and isinstance(c_list, list):
                confirmed_suspended_by_date.setdefault(d_s, set()).update(
                    str(c).strip().zfill(5) for c in c_list if str(c).strip()
                )
    frozen_dates_raw = frozen_series_raw.get("dates") or []
    if frozen_dates_raw and prev_suspended_on_last_date:
        confirmed_suspended_by_date.setdefault(str(frozen_dates_raw[-1]), set()).update(
            prev_suspended_on_last_date
        )
    has_clean_target_spot = bool(can_use_csv_spot or can_use_meta_suspended)
    if has_clean_target_spot:
        target_spot_suspended = {
            str(c).strip().zfill(5)
            for c, ent in spot_info_by_code.items()
            if is_spot_suspended(ent, check_date=target_str)
        }
        has_target_date_spot_evidence = bool(can_use_meta_suspended) or any(
            (_extract_quote_date(ent.get("quote_date")) or _extract_quote_date(ent.get("ts"))) == target_str
            for ent in spot_info_by_code.values()
        )
        if has_target_date_spot_evidence:
            confirmed_suspended_by_date[target_str] = target_spot_suspended
            if persist:
                record_suspended_for_date(st_dir, target_str, target_spot_suspended)
        elif target_spot_suspended:
            confirmed_suspended_by_date.setdefault(target_str, set()).update(target_spot_suspended)
    else:
        # Degraded/stale spot for target_str MUST NOT treat target_str as confirmed suspended.
        confirmed_suspended_by_date.pop(target_str, None)

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
    uni_name_lookup: dict[str, str] = {}
    for _, r in uni_all_df.iterrows():
        c_raw = r.get("code")
        if pd.isna(c_raw):
            continue
        c_key = str(c_raw).strip()
        if not c_key:
            continue
        nm_cn = r.get("name_cn")
        nm_en = r.get("name")
        if pd.notna(nm_cn) and str(nm_cn).strip():
            uni_name_lookup[c_key] = str(nm_cn).strip()
        elif pd.notna(nm_en) and str(nm_en).strip():
            uni_name_lookup[c_key] = str(nm_en).strip()

    def _valid_pos_price(val: Any) -> bool:
        return (
            isinstance(val, (int, float))
            and not isinstance(val, bool)
            and math.isfinite(float(val))
            and float(val) > 0
        )

    def _is_confirmed_suspended_on(code: str, d_chk: str) -> bool:
        return str(code).strip().zfill(5) in confirmed_suspended_by_date.get(d_chk, set())

    def _classify_suspended(
        code: str,
        d_chk: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """Classify a missing daily bar strictly from per-trading-day suspension records.

        A missing bar on ``d_chk`` is exempted ONLY when ``code`` has a confirmed
        suspension record for ``d_chk`` AND every trading day after its last valid
        close up to ``d_chk`` (including days before ``last_f_date``) is confirmed
        suspended. The 21st suspended trading day is determined directly from that
        verified trading-day span.

        Returns:
        - ``("carry", info)`` when credibly suspended for 1..MAX_SUSPENDED_TRADING_DAYS
        - ``("remove", info)`` when credibly suspended for > MAX_SUSPENDED_TRADING_DAYS
        - ``("missing", None)`` when not confirmed suspended or no prior valid close exists
        """
        if not _is_confirmed_suspended_on(code, d_chk):
            return "missing", None
        if code not in px.columns:
            return "missing", None
        prior = px.loc[px.index < d_chk, code].dropna()
        if prior.empty:
            return "missing", None
        for idx_i in range(len(prior) - 1, -1, -1):
            val = float(prior.iloc[idx_i])
            if _valid_pos_price(val):
                last_d = str(prior.index[idx_i])
                susp_span = trading_days_in_range("hk", last_d, d_chk)
                gap = len(susp_span)
                if gap <= 0:
                    return "missing", None
                # Every trading day in (last_d, d_chk] — including days before
                # last_f_date — MUST have a per-day suspension confirmation.
                for d_span in susp_span:
                    if not _is_confirmed_suspended_on(code, d_span):
                        return "missing", None
                if 1 <= gap <= MAX_SUSPENDED_TRADING_DAYS:
                    return "carry", {
                        "code": code,
                        "name": uni_name_lookup.get(code, code),
                        "carried_close_date": last_d,
                        "carried_close": round(val, 4),
                        "suspended_trading_days": gap,
                        "note": f"停牌，沿用 {last_d} 日收盘价",
                    }
                if gap > MAX_SUSPENDED_TRADING_DAYS:
                    return "remove", {
                        "code": code,
                        "name": uni_name_lookup.get(code, code),
                        "carried_close_date": last_d,
                        "carried_close": round(val, 4),
                        "suspended_trading_days": gap,
                        "removal_effective_date": susp_span[MAX_SUSPENDED_TRADING_DAYS],
                        "note": "已停牌超过 20 个交易日，移出计算",
                    }
                return "missing", None
        return "missing", None

    rebal_dates_used: list[str] = [str(dates[rp]) for rp in rebal_pos]

    def _segments_for_day(d_chk: str, last_f_date: str | None) -> list[int] | None:
        pos = date_pos.get(d_chk)
        if pos is None:
            return None
        seg_idx = None
        for k_seg, rpos in enumerate(rebal_pos):
            if rpos <= pos:
                seg_idx = k_seg
        if seg_idx is None:
            return []
        # On a newly appended rebalance day, the outgoing basket earns the close-to-close
        # return into d_chk while the incoming basket anchors p0 at d_chk.
        if (
            rebal_pos[seg_idx] == pos
            and seg_idx > 0
            and (last_f_date is None or d_chk > last_f_date)
        ):
            return [seg_idx - 1, seg_idx]
        return [seg_idx]

    if backfill_suspensions:
        backfill_codes_by_date: dict[str, set[str]] = {}
        for key in idx_defs:
            has_frozen_k = bool(frozen_series_raw.get("dates") and frozen_series_raw.get(key))
            if not has_frozen_k:
                continue
            last_f_date_k = str(frozen_series_raw["dates"][-1])
            expected_tail_k = trading_days_in_range("hk", last_f_date_k, target_str)
            panel_tail_k = [d for d in dates if d > last_f_date_k and d < target_str]
            gap_days_k = sorted(
                d for d in (set(expected_tail_k) | set(panel_tail_k)) if d < target_str
            )
            key_rem_k = frozen_removals.get(key) or {}
            for d_chk in gap_days_k:
                seg_indices_k = _segments_for_day(d_chk, last_f_date_k)
                if not seg_indices_k:
                    continue
                day_codes = backfill_codes_by_date.setdefault(d_chk, set())
                for s_idx in seg_indices_k:
                    r_date = rebal_dates_used[s_idx]
                    r_rem_map = key_rem_k.get(r_date) or {}
                    for c in selections[key][s_idx]:
                        c_norm = str(c).strip().zfill(5)
                        existing_rem = r_rem_map.get(c_norm) or r_rem_map.get(c)
                        if isinstance(existing_rem, dict):
                            eff_dt = str(existing_rem.get("effective_date") or "")
                            if eff_dt and eff_dt <= d_chk:
                                continue
                        day_codes.add(c_norm)
        if backfill_codes_by_date:
            try:
                bf_res = backfill_missing_suspensions(
                    state_dir=st_dir,
                    target_date=target_str,
                    yf_module=yf_module,
                    codes_by_date=backfill_codes_by_date,
                )
                for d_rec, rec_codes in (bf_res.get("recorded") or {}).items():
                    d_s = str(d_rec).strip()[:10]
                    if d_s and d_s < target_str and isinstance(rec_codes, list):
                        confirmed_suspended_by_date.setdefault(d_s, set()).update(
                            str(c).strip().zfill(5) for c in rec_codes if str(c).strip()
                        )
            except Exception as exc:
                logger.warning("HK momentum backfill_missing_suspensions raised: %s", exc)

    def _missing_held_for_key(
        key: str,
        last_f_date: str | None,
        key_removals: dict[str, dict[str, dict[str, Any]]],
    ) -> tuple[list[str], list[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Verify every HKEX trading day after ``last_f_date`` for panel & held coverage."""
        if last_f_date is not None:
            expected_tail = trading_days_in_range("hk", last_f_date, target_str)
            panel_tail = [d for d in dates if d > last_f_date and d <= target_str]
            tail_d = sorted(set(expected_tail) | set(panel_tail))
            check_d = ([last_f_date] + tail_d) if tail_d else ([target_str] if target_str in date_pos else [])
        else:
            check_d = [target_str] if target_str in date_pos else []
        missing: set[str] = set()
        missing_days: set[str] = set()
        suspended_on_target: dict[str, dict[str, Any]] = {}
        latest_seg_idx = len(rebal_pos) - 1
        for d_chk in check_d:
            seg_indices = _segments_for_day(d_chk, last_f_date)
            if seg_indices is None:
                missing_days.add(d_chk)
                continue
            if not seg_indices:
                continue
            for s_idx in seg_indices:
                r_date = rebal_dates_used[s_idx]
                r_rem_map = key_removals.get(r_date) or {}
                seg_codes = selections[key][s_idx]
                for c in seg_codes:
                    c_norm = str(c).strip().zfill(5)
                    existing_rem = r_rem_map.get(c_norm) or r_rem_map.get(c)
                    if isinstance(existing_rem, dict):
                        eff_dt = str(existing_rem.get("effective_date") or "")
                        if eff_dt and eff_dt <= d_chk:
                            continue
                    if c not in px.columns or d_chk not in px.index:
                        missing.add(c)
                        continue
                    px_val = px.loc[d_chk, c]
                    if pd.isna(px_val) or not _valid_pos_price(float(px_val)):
                        kind, info = _classify_suspended(c, d_chk)
                        if kind == "carry" and info is not None:
                            if d_chk == target_str and s_idx == latest_seg_idx:
                                suspended_on_target[c] = info
                            continue
                        if kind == "remove" and info is not None:
                            rem_entry = dict(info)
                            rem_day = str(rem_entry.pop("removal_effective_date", d_chk))
                            rem_entry["code"] = c_norm
                            eff_date = min(max(rem_day, r_date), d_chk)
                            rem_entry["effective_date"] = eff_date
                            key_removals.setdefault(r_date, {})[c_norm] = rem_entry
                            r_rem_map = key_removals[r_date]
                            continue
                        missing.add(c)
                        continue
                    if (
                        d_chk == target_str
                        and not _valid_pos_price(uni_spot_prices.get(c))
                        and not is_spot_suspended(spot_info_by_code.get(c), check_date=target_str)
                    ):
                        missing.add(c)
        latest_rd = rebal_dates_used[-1]
        latest_rem_active = {
            c_code: dict(rem_info)
            for c_code, rem_info in (key_removals.get(latest_rd) or {}).items()
            if str(rem_info.get("effective_date") or "") <= target_str
        }
        return sorted(missing), sorted(missing_days), suspended_on_target, latest_rem_active

    missing_held_by_index: dict[str, list[str]] = {}
    suspended_by_index: dict[str, dict[str, dict[str, Any]]] = {}
    removed_suspended_by_index: dict[str, dict[str, dict[str, Any]]] = {}
    all_suspended_removals: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    active_selections: dict[str, list[list[str]]] = {}
    index_dates: dict[str, str | None] = {}

    for key in idx_defs:
        has_frozen = bool(frozen_series_raw.get("dates") and frozen_series_raw.get(key))
        f_ser = (
            pd.Series(
                frozen_series_raw[key],
                index=pd.Index(frozen_series_raw["dates"]),
                dtype=float,
            )
            if has_frozen
            else pd.Series(dtype=float)
        )
        last_f_date_key: str | None = str(f_ser.index[-1]) if has_frozen else None
        key_removals: dict[str, dict[str, dict[str, Any]]] = {
            r_dt: {c_k: dict(c_v) for c_k, c_v in c_map.items()}
            for r_dt, c_map in (frozen_removals.get(key) or {}).items()
        }
        missing_held, missing_days, suspended_map, removed_map = _missing_held_for_key(
            key, last_f_date_key, key_removals
        )
        missing_held_by_index[key] = list(missing_held)
        suspended_by_index[key] = suspended_map
        removed_suspended_by_index[key] = removed_map
        if key_removals:
            all_suspended_removals[key] = key_removals

        seg_rem_dates: list[dict[str, str]] = [
            {
                c_code: str(rem_info["effective_date"])
                for c_code, rem_info in (key_removals.get(rd) or {}).items()
                if rem_info.get("effective_date")
            }
            for rd in rebal_dates_used
        ]
        key_sels = [list(s) for s in selections[key]]
        if removed_map and key_sels:
            removed_set = set(removed_map.keys())
            key_sels[-1] = [c for c in key_sels[-1] if str(c).strip().zfill(5) not in removed_set]
            if not key_sels[-1]:
                comp_reasons.append(f"{key} 移出长期停牌成分后无剩余有效成分")
        if len(rebal_dates_used) >= 2 and rebal_dates_used[-1] == target_str:
            prev_rd = rebal_dates_used[-2]
            prev_rem_set = {
                c_code
                for c_code, rem_info in (key_removals.get(prev_rd) or {}).items()
                if str(rem_info.get("effective_date") or "") <= target_str
            }
            if prev_rem_set and not [
                c for c in selections[key][-2] if str(c).strip().zfill(5) not in prev_rem_set
            ]:
                comp_reasons.append(f"{key} 上期移出长期停牌成分后无剩余有效成分")
        active_selections[key] = key_sels
        lvl_full = build_index(
            px_ff,
            pd.Index(dates),
            rebal_pos,
            selections[key],
            segment_removals=seg_rem_dates,
        )

        if has_frozen and last_f_date_key is not None:
            if missing_days or missing_held:
                # NEVER reweight remaining constituents or append incomplete/gapped days!
                series[key] = f_ser
                valid_idx = f_ser.dropna().index
                index_dates[key] = str(valid_idx[-1]) if len(valid_idx) else None
                if missing_days:
                    day_msg = (
                        f"个股原始日线面板在补跑区间缺失 {len(missing_days)} 个交易日 "
                        f"(如 {', '.join(missing_days[:5])})"
                    )
                    if day_msg not in comp_reasons:
                        comp_reasons.append(day_msg)
                if missing_held:
                    comp_reasons.append(
                        f"{key} 持有成分缺少必要价格 {len(missing_held)} 只 (如 {', '.join(missing_held[:5])})"
                    )
                continue
            tail_dates = [d for d in lvl_full.index if d > last_f_date_key]
            if not tail_dates:
                series[key] = f_ser
            elif last_f_date_key in lvl_full.index and float(lvl_full.loc[last_f_date_key]) > 0:
                scale = float(f_ser.iloc[-1]) / float(lvl_full.loc[last_f_date_key])
                tail = lvl_full.loc[tail_dates] * scale
                series[key] = pd.concat([f_ser, tail])
            else:
                # Keep frozen series untouched rather than falling back to full recalc!
                series[key] = f_ser
                comp_reasons.append(
                    f"{key} 新增数据缺少最后冻结基准日 {last_f_date_key}，已保留冻结历史"
                )
        else:
            if missing_days:
                day_msg = (
                    f"个股原始日线面板缺少 {len(missing_days)} 个交易日 "
                    f"(如 {', '.join(missing_days[:5])})"
                )
                if day_msg not in comp_reasons:
                    comp_reasons.append(day_msg)
            if missing_held:
                comp_reasons.append(
                    f"{key} 持有成分缺少必要价格 {len(missing_held)} 只 (如 {', '.join(missing_held[:5])})"
                )
            series[key] = lvl_full
        valid_idx = series[key].dropna().index
        index_dates[key] = str(valid_idx[-1]) if len(valid_idx) else None

    common = series["smallcap"].index.intersection(series["nonggt"].index)
    out: dict[str, Any] = {"dates": list(common)}
    for key in idx_defs:
        s = series[key].reindex(common)
        if not frozen_series_raw.get(key):
            s = s / s.iloc[0] * BASE
        out[key] = [round(float(v), 2) if pd.notna(v) else None for v in s]
        series[key] = s

    # HSI benchmark: always preserve frozen history up to last valid frozen date; require raw (un-ffilled)
    # HSI records for EVERY date in the newly appended range before allowing completeness/freeze.
    f_hsi_valid = pd.Series(dtype=float)
    last_f_date: str | None = None
    if frozen_series_raw.get("dates") and frozen_series_raw.get("hsi"):
        f_hsi = pd.Series(
            frozen_series_raw["hsi"],
            index=pd.Index(frozen_series_raw["dates"]),
            dtype=float,
        )
        f_hsi_valid = f_hsi.dropna()
        if not f_hsi_valid.empty:
            last_f_date = str(f_hsi_valid.index[-1])

    expected_hsi_tail = (
        trading_days_in_range("hk", last_f_date, target_str)
        if last_f_date is not None and last_f_date < target_str
        else []
    )
    check_hsi_dates = sorted(
        set(str(d) for d in common if last_f_date is None or str(d) > last_f_date)
        | set(expected_hsi_tail)
    )
    hsi_fetched, raw_hsi_dates = _fetch_hsi_history(common, session=hsi_session)
    if hsi_fetched is None:
        hsi_reasons.append("恒生指数历史行情拉取失败")
    else:
        missing_hsi = [d for d in check_hsi_dates if d not in raw_hsi_dates]
        if target_str not in raw_hsi_dates:
            hsi_reasons.append(f"恒生指数原始记录缺少目标日期 {target_str}")
        missing_other = [d for d in missing_hsi if d != target_str]
        if missing_other:
            hsi_reasons.append(
                f"恒生指数新增区间原始记录缺失 {len(missing_other)} 天 (如 {', '.join(missing_other[:5])})"
            )

    if not f_hsi_valid.empty and last_f_date is not None:
        tail_dates = [d for d in common if d > last_f_date]
        if not tail_dates:
            out["hsi"] = [round(float(v), 2) if pd.notna(v) else None for v in f_hsi_valid.reindex(common)]
        elif (
            not hsi_reasons
            and hsi_fetched is not None
            and last_f_date in hsi_fetched.index
            and pd.notna(hsi_fetched.loc[last_f_date])
            and float(hsi_fetched.loc[last_f_date]) > 0
        ):
            scale = float(f_hsi_valid.iloc[-1]) / float(hsi_fetched.loc[last_f_date])
            combined_hsi = pd.concat([f_hsi_valid, hsi_fetched.loc[tail_dates] * scale]).reindex(common)
            out["hsi"] = [round(float(v), 2) if pd.notna(v) else None for v in combined_hsi]
        else:
            # Never overwrite frozen HSI or freeze ffill values; preserve frozen values only.
            out["hsi"] = [round(float(v), 2) if pd.notna(v) else None for v in f_hsi_valid.reindex(common)]
            if hsi_fetched is not None and not hsi_reasons:
                hsi_reasons.append(
                    f"恒生指数历史行情缺少基准日 {last_f_date}，已保留冻结历史"
                )
    else:
        out["hsi"] = [round(float(v), 2) if pd.notna(v) else None for v in hsi_fetched] if hsi_fetched is not None else None

    hsi_actual_date: str | None = None
    if isinstance(out.get("hsi"), list) and out.get("dates"):
        for d_val, h_val in zip(reversed(out["dates"]), reversed(out["hsi"])):
            if h_val is not None:
                hsi_actual_date = str(d_val)
                break
    if hsi_actual_date is None and raw_hsi_dates:
        hsi_actual_date = str(max(raw_hsi_dates))
    elif raw_hsi_dates and not hsi_reasons:
        # If HSI has fresh raw data beyond common intersection, report its actual latest date.
        latest_raw_hsi = str(max(raw_hsi_dates))
        if latest_raw_hsi > hsi_actual_date:
            hsi_actual_date = latest_raw_hsi
    index_dates["hsi"] = hsi_actual_date

    history_stale_reasons: list[str] = comp_reasons + hsi_reasons
    history_stale = bool(history_stale_reasons)

    last_scores = momentum_scores(
        px_ff, px, tv, uni, len(dates) - 1, ggt_codes, excluded_codes=excl_set
    )
    cons: dict[str, list[dict[str, Any]]] = {}
    for key in idx_defs:
        codes = active_selections[key][-1]
        susp_map = suspended_by_index.get(key) or {}
        eq_weight = (1.0 / len(codes)) if codes else 0.0
        detail = []
        for c in codes:
            row = last_scores.loc[c] if (last_scores is not None and c in last_scores.index) else None
            name_val = uni_name_lookup.get(c, c)
            if c in uni.index:
                name_val = str(uni.loc[c, "name_cn"] if pd.notna(uni.loc[c, "name_cn"]) else uni.loc[c, "name"])
            entry: dict[str, Any] = {
                "code": c,
                "name": name_val,
                "weight": eq_weight,
                "score": round(float(row["score"]), 2) if row is not None else None,
                "mktcap_e8": round(float(row["mktcap_e8"]), 1) if row is not None else None,
                "ret12": round(float(row["ret12"]) * 100.0, 1) if row is not None else None,
            }
            if c in susp_map:
                carry = susp_map[c]
                entry["suspended"] = True
                entry["carried_close_date"] = carry["carried_close_date"]
                entry["carried_close"] = carry["carried_close"]
                entry["suspended_trading_days"] = carry["suspended_trading_days"]
                entry["suspended_note"] = carry["note"]
            detail.append(entry)
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

    suspended_list_by_index: dict[str, list[dict[str, Any]]] = {
        key: [suspended_by_index[key][c] for c in sorted(suspended_by_index.get(key) or {})]
        for key in idx_defs
    }
    removed_suspended_list_by_index: dict[str, list[dict[str, Any]]] = {
        key: [removed_suspended_by_index[key][c] for c in sorted(removed_suspended_by_index.get(key) or {})]
        for key in idx_defs
    }
    uniq_suspended: dict[str, dict[str, Any]] = {}
    for items in suspended_list_by_index.values():
        for item in items:
            uniq_suspended.setdefault(item["code"], item)
    uniq_removed: dict[str, dict[str, Any]] = {}
    for items in removed_suspended_list_by_index.values():
        for item in items:
            uniq_removed.setdefault(item["code"], item)
    removed_codes_sorted = sorted(uniq_removed.keys())
    comp_note_parts: list[str] = []
    if not comp_reasons:
        for item in uniq_suspended.values():
            lbl = (
                f"{item['code']}({item['name']})"
                if item.get("name") and item["name"] != item["code"]
                else str(item["code"])
            )
            comp_note_parts.append(f"{lbl} {item['note']}")
        for item in uniq_removed.values():
            lbl = (
                f"{item['code']}({item['name']})"
                if item.get("name") and item["name"] != item["code"]
                else str(item["code"])
            )
            comp_note_parts.append(f"{lbl} {item['note']}")
    comp_note: str | None = "；".join(comp_note_parts) if comp_note_parts else None

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
        note=comp_note,
        removed_codes=removed_codes_sorted if removed_codes_sorted else None,
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
            extra_overall=(
                {"removed_suspended_codes": removed_codes_sorted} if removed_codes_sorted else None
            ),
        )
        resolved_last_success = status_doc["overall"]["last_success_at"]
    else:
        resolved_last_success = prev_meta.get("last_success_at")

    meta = {
        "updated": now_hk().isoformat(timespec="seconds"),
        "base_date": out["dates"][0],
        "last_date": out["dates"][-1],
        "index_dates": index_dates,
        "missing_held_by_index": missing_held_by_index,
        "suspended_constituents": suspended_list_by_index,
        "removed_suspended_constituents": removed_suspended_list_by_index,
        "removed_suspended_codes": removed_codes_sorted,
        "failure_details": list(history_stale_reasons),
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
        "excluded_codes": excluded_sorted,
        "excluded_reason": excluded_reason,
        "ggt_skipped_invalid": int(uni_meta.get("ggt_skipped_invalid") or 0) if isinstance(uni_meta, dict) else 0,
        "ggt_skipped_note": uni_meta.get("ggt_skipped_note") if isinstance(uni_meta, dict) else None,
        "source_statuses": compute_statuses,
        "params": {
            "top_n": TOP_N,
            "min_turnover_wan": MIN_TURNOVER_WAN,
            "min_turnover_basis": "近3个月日成交额中位数 ≥ 100 万港元",
            "smallcap_limit_e8": SMALLCAP_LIMIT_E8,
            "min_price": MIN_PRICE,
        },
    }
    frozen_payload: dict[str, Any] = {
        "launch_date": launch_date,
        "rebalance_snapshots": frozen_rebals,
        "frozen_series": out,
    }
    if frozen_exclusions:
        frozen_payload["rebalance_exclusions"] = frozen_exclusions
    if uniq_suspended:
        frozen_payload["suspended_on_last_date"] = sorted(uniq_suspended.keys())
    persisted_sbd = {
        d_k: sorted(codes)
        for d_k, codes in sorted(confirmed_suspended_by_date.items())
        if d_k <= target_str and codes
    }
    if persisted_sbd:
        frozen_payload["suspended_by_date"] = persisted_sbd
    if all_suspended_removals:
        frozen_payload["suspended_removals"] = all_suspended_removals
    if removed_codes_sorted:
        frozen_payload["removed_suspended_codes"] = removed_codes_sorted
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
            extra_items: list[SourceStatus] = []
            has_unclean_daily = any(
                not (bool(v.get("ok")) and (not bool(v.get("stale"))))
                for v in daily_sources.values()
                if isinstance(v, dict)
            )
            if month_end_failed_reason and not has_unclean_daily:
                extra_items.append(
                    make_source_result(
                        "month_end_rebalance",
                        ok=False,
                        stale=True,
                        reason=month_end_failed_reason,
                        label="月末调仓",
                    )
                )
            all_req = list(
                dict.fromkeys(
                    list(daily_sources.keys())
                    + [x.name for x in extra_items]
                    + ["hk_intraday_spot", "hsi_spot"]
                )
            )
            sdoc = store.update_sources(
                extra_items + [spot_st, hsi_spot_st],
                required=all_req,
                now_str=now_str,
                preserved_sources=daily_sources,
            )
            out_dict["sources"] = dict(sdoc["sources"])
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

    expected_q_date = _expected_hk_trade_date(cur, sess_state)
    series_last = str(series["dates"][-1])
    series_has_target = series_last == expected_q_date
    expected_prev_td = previous_trading_day("hk", cur.date()).isoformat()
    daily_series_lagging = (not series_has_target) and (series_last < expected_prev_td)

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

    out: dict[str, Any] = {
        **base_meta,
        "target_trade_date": expected_q_date,
        "daily_close_date": series_last,
    }

    any_index_stale = daily_series_lagging
    for key in ("smallcap", "nonggt"):
        members = [c["code"] for c in (cons.get(key) or [])]
        n_expected = len(members)
        fresh_quotes = {
            c: spot[c]
            for c in members
            if c in spot
            and (
                _extract_quote_date(spot[c].get("ts")) == expected_q_date
                or is_spot_suspended(spot[c], check_date=expected_q_date)
            )
        }
        missing_codes = [c for c in members if c not in fresh_quotes]
        coverage = (len(fresh_quotes) / n_expected) if n_expected else 0.0

        if series_has_target and len(series[key]) >= 1:
            key_spot_bad = n_expected > 0 and coverage < 0.5
            if key_spot_bad:
                any_index_stale = True
                cov_msg = f"{key} 有效当日报价不足 ({len(fresh_quotes)}/{n_expected}，期望日期 {expected_q_date})"
                stale_reasons.append(cov_msg)
                spot_reasons.append(cov_msg)
            lvl = series[key][-1]
            prev_lvl = series[key][-2] if len(series[key]) >= 2 else None
            pct_val = round((lvl / prev_lvl - 1.0) * 100.0, 2) if prev_lvl else None
            out[key] = {
                "level": lvl,
                "pct": pct_val,
                "prev_close_level": prev_lvl if prev_lvl is not None else lvl,
                "close_date": series_last,
                "n_quoted": len(fresh_quotes),
                "n_expected": n_expected,
                "missing_codes": missing_codes,
                "partial_quotes": bool(missing_codes),
                "stale": key_spot_bad,
                "source": "daily_close",
            }
            continue

        if daily_series_lagging:
            out[key] = _stale_entry(key, series[key][-1] if series.get(key) else None)
            continue

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

        chg = (
            sum(
                0.0 if is_spot_suspended(q, check_date=expected_q_date) else float(q["pct"])
                for q in fresh_quotes.values()
            )
            / n_expected
            / 100.0
        )
        last_close = float(series[key][-1])
        out[key] = {
            "level": round(last_close * (1.0 + chg), 2),
            "pct": round(chg * 100.0, 2),
            "prev_close_level": last_close,
            "close_date": expected_q_date,
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
        out["hsi"] = {
            "value": hsi_spot["value"],
            "pct": hsi_spot["pct"],
            "close_date": hsi_q_date,
            "stale": False,
        }
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
