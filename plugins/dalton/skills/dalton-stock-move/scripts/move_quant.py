#!/usr/bin/env python3
"""
move_quant.py — 股价异动统一量化脚本（PS.10）

功能：
1. 价格收益使用复权收盘价（Adj Close）计算；展示使用未复权收盘价（Close）。
2. 窗口收益 = P_adj(窗口末最后一个收盘) / P_adj(窗口起点前最后一个收盘) - 1。
3. 同组收益 = 同组（不含目标）窗口收益的中位数；
   超额 vs 同组 = 目标窗口收益 - 同组中位数；
   超额 vs 板块 ETF = 目标窗口收益 - ETF 窗口收益。
4. 日超额 = 目标日收益 - 同组日收益中位数；
   σ = 窗口起点前 60 个交易日日超额的标准差（有效样本 > 20 才计算）；
   day_z = 最后一日超额 / σ；
   window_z = 窗口超额 / (σ * sqrt(窗口交易日数))。
5. 量比 (volume_vs_adtv) = 当日成交量 / 前 20 日均量（不含当日）。
6. 判定 (assessment)：
   - sector_beta：当且仅当 |窗口超额| < 3.0pp 且 |窗口 z| < 1.5
   - stock_specific：当 |窗口超额| >= 5.0pp 或 |窗口 z| >= 2.0
   - mixed：介于两者之间
7. 数据完备性检查 (data_ok)：当指定了 --end 或预期最新日线缺失时 data_ok=false，不能下结论。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# ── 判定阈值常量（PS.10 统一标准，第一次周会后可按误报率微调） ──────────────────
BETA_MAX_EXCESS_PP = 3.0     # |窗口超额| < 3 个百分点
BETA_MAX_Z = 1.5             # 且 |窗口 z| < 1.5 -> sector_beta
ALPHA_MIN_EXCESS_PP = 5.0    # |窗口超额| >= 5 个百分点
ALPHA_MIN_Z = 2.0            # 或 |窗口 z| >= 2.0 -> stock_specific
HIST_LOOKBACK_DAYS = 60      # 窗口起点前 60 个交易日计算日超额标准差 σ
MIN_HIST_SAMPLES = 20        # 历史样本数 > 20 才计算 σ 和 z 值
ADTV_LOOKBACK_DAYS = 20      # 当日成交量对比前 20 个交易日均量（不含当日）

PRIVATE_TICKERS = {"SPCX", "OPENAI", "ANTHROPIC"}
DEFAULT_FUTURES = ["NQ=F", "ES=F"]
NON_US_EXTENDED_UNSUPPORTED_SUFFIXES = (
    ".HK", ".SH", ".SS", ".SZ", ".T", ".L", ".KS", ".KQ", ".TW", ".TWO",
    ".DE", ".PA", ".AS", ".TO", ".AX", ".SI", ".NS", ".BO",
)


def supports_extended_session(symbol: str) -> tuple[bool, str | None]:
    """检查标的是否支持美股盘前/盘后（pre/post）分钟线计算。"""
    s = (symbol or "").strip().upper()
    if not s:
        return False, "标的代码为空"
    if s in PRIVATE_TICKERS:
        return False, f"未上市标的 {s} 不支持行情查询"
    if s.startswith("^"):
        return (
            False,
            f"指数标的 {s} 不含盘前/盘后分钟线（prepost），请改用指数期货（如 NQ=F、ES=F）或退回 --session regular。",
        )
    if any(s.endswith(sfx) for sfx in NON_US_EXTENDED_UNSUPPORTED_SUFFIXES):
        return (
            False,
            f"非美股标的 {s} 不支持美股盘前/盘后分钟线（pre/post），请退回使用 --session regular。",
        )
    return True, None

DEFAULT_PEER_MAP: dict[str, list[str]] = {
    "META": ["GOOGL", "AMZN", "MSFT", "AAPL", "SNAP", "PINS", "RDDT"],
    "GOOGL": ["META", "AMZN", "MSFT", "AAPL"],
    "AMZN": ["GOOGL", "META", "MSFT", "AAPL", "WMT", "MELI"],
    "MSFT": ["GOOGL", "AMZN", "META", "AAPL", "ORCL", "CRM"],
    "AAPL": ["MSFT", "GOOGL", "AMZN", "META"],
    "DASH": ["UBER", "CART", "LYFT", "GRAB"],
    "UBER": ["DASH", "LYFT", "CART", "GRAB"],
    "AFRM": ["KLAR", "SEZL", "PYPL", "XYZ", "UPST", "SOFI"],
    "NVDA": ["AMD", "AVGO", "MRVL", "INTC"],
    "AMD": ["NVDA", "AVGO", "MRVL", "INTC"],
    "AVGO": ["NVDA", "AMD", "MRVL", "ANET"],
    "ALAB": ["CRDO", "MRVL", "AVGO", "ANET"],
    "TSLA": ["RIVN", "LCID", "NIO", "XPEV", "LI"],
    "BABA": ["PDD", "JD", "BIDU", "TCOM", "NTES"],
    "PDD": ["BABA", "JD", "VIPS", "SE"],
    "JD": ["BABA", "PDD", "VIPS", "TCOM"],
    "TCOM": ["BKNG", "ABNB", "EXPE", "HTHT"],
    "HOOD": ["IBKR", "SCHW", "COIN", "SOFI"],
    "IBKR": ["HOOD", "SCHW", "RJF", "LPLA"],
    "V": ["MA", "AXP", "PYPL", "XYZ"],
    "MA": ["V", "AXP", "PYPL", "XYZ"],
    "SPGI": ["MCO", "MSCI", "TRI", "FDS", "VRSK"],
    "MCO": ["SPGI", "MSCI", "TRI", "FDS", "VRSK"],
    "NDAQ": ["ICE", "CME", "CBOE", "SPGI"],
}

GROUP_ETF_MAP: dict[str, str] = {
    "megacap": "QQQ",
    "us_megatech": "QQQ",
    "app_software": "IGV",
    "infra_software": "IGV",
    "it_services": "XLK",
    "streaming_media": "XLC",
    "social_apps": "XLC",
    "games_adtech": "XLC",
    "ecom": "XLY",
    "delivery": "XLY",
    "online_travel": "XLY",
    "banks": "XLF",
    "networks": "XLF",
    "payments": "XLF",
    "fintech_lenders": "XLF",
    "card_lenders": "XLF",
    "bnpl": "XLF",
    "brokers": "XLF",
    "alts": "XLF",
    "bureaus": "XLF",
    "ratings_info": "XLF",
    "insurers": "XLF",
    "ins_brokers": "XLF",
    "exchanges": "XLF",
    "consumer_brands": "XLY",
    "specialty_retail": "XLY",
    "restaurants": "XLY",
    "lodging_cruise": "XLY",
    "leisure": "XLY",
    "staples": "XLP",
    "mass_retail": "XLP",
    "food_ag": "XLP",
    "homebuilder": "XHB",
    "ai_construction": "PAVE",
    "space": "UFO",
    "cn_mega": "KWEB",
    "cn_ecom_vertical": "KWEB",
    "cn_travel_local": "KWEB",
    "cn_internet_small": "KWEB",
    "cn_content": "KWEB",
    "cn_games": "KWEB",
    "cn_brokers_exch": "KWEB",
    "cn_logistics": "KWEB",
    "cn_health": "KWEB",
    "cn_ai": "KWEB",
    "cn_cloud_idc": "KWEB",
}

SECTOR_ETF_MAP: dict[str, str] = {
    "Technology": "XLK",
    "Semiconductors": "SMH",
    "Communication Services": "XLC",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Healthcare": "XLV",
    "Financial Services": "XLF",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
    "Basic Materials": "XLB",
}


def load_coverage_yaml(path: str | Path | None) -> list[dict[str, Any]]:
    """读取 coverage.yaml 中的 companies 列表；支持 PyYAML 或轻量回退解析。"""
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    else:
        candidates.extend([
            Path("../Home/dalton/coverage.yaml"),
            Path("dalton/coverage.yaml"),
            Path("config/dalton/coverage.yaml"),
        ])
    target_path: Path | None = None
    for c in candidates:
        if c.exists():
            target_path = c
            break
    if not target_path:
        return []

    text = target_path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        if isinstance(data, dict) and isinstance(data.get("companies"), list):
            return data["companies"]
    except Exception:
        pass

    # 轻量解析回退（当环境无 PyYAML 时支持标准列表格式或 JSON 子集）
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("companies"), list):
            return data["companies"]
    except Exception:
        pass

    companies: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_companies = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if re.match(r"^companies\s*:", line):
            in_companies = True
            continue
        if in_companies and re.match(r"^[A-Za-z0-9_]+\s*:", line):
            in_companies = False
            continue
        if not in_companies:
            continue
        m_item = re.match(r"^\s*-\s*ticker\s*:\s*(.+)$", line)
        if m_item:
            if current:
                companies.append(current)
            current = {"ticker": m_item.group(1).strip().strip("'\"")}
            continue
        if current is not None:
            m_kv = re.match(r"^\s+([A-Za-z0-9_]+)\s*:\s*(.+)$", line)
            if m_kv:
                k, v = m_kv.group(1), m_kv.group(2).strip()
                if v.startswith("[") and v.endswith("]"):
                    items = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
                    current[k] = items
                else:
                    current[k] = v.strip("'\"")
    if current:
        companies.append(current)
    return companies


def resolve_peers_and_etf(
    ticker: str,
    explicit_peers: str | None = None,
    explicit_etf: str | None = None,
    coverage_path: str | None = None,
) -> tuple[list[str], str]:
    """按 PS.10 优先级确定同组代码列表与板块 ETF。"""
    t = ticker.upper().strip()
    peers: list[str] = []
    etf: str | None = explicit_etf.upper().strip() if explicit_etf else None

    if explicit_peers:
        peers = [p.strip().upper() for p in explicit_peers.split(",") if p.strip() and p.strip().upper() != t]

    companies = load_coverage_yaml(coverage_path)
    if companies and (not peers or not etf):
        entry = next((c for c in companies if str(c.get("ticker", "")).upper() == t), None)
        if entry:
            if not peers:
                raw_peers = entry.get("peers") or []
                if isinstance(raw_peers, list) and raw_peers:
                    peers = [str(p).upper() for p in raw_peers if str(p).upper() != t and str(p).upper() not in PRIVATE_TICKERS]
                elif entry.get("peer_group"):
                    pg = entry["peer_group"]
                    peers = [
                        str(c["ticker"]).upper()
                        for c in companies
                        if c.get("peer_group") == pg
                        and str(c.get("ticker", "")).upper() != t
                        and str(c.get("ticker", "")).upper() not in PRIVATE_TICKERS
                    ]
            if not etf:
                raw_etfs = entry.get("etfs") or []
                if isinstance(raw_etfs, list) and raw_etfs:
                    etf = str(raw_etfs[0]).upper()
                else:
                    pg = str(entry.get("peer_group") or "")
                    etf = GROUP_ETF_MAP.get(pg)

    if not peers:
        peers = [p for p in DEFAULT_PEER_MAP.get(t, []) if p != t and p not in PRIVATE_TICKERS]

    if not etf or not peers:
        try:
            import yfinance as yf  # type: ignore
            info = yf.Ticker(t).info or {}
            sector = info.get("sector", "")
            industry = info.get("industry", "")
            if not etf:
                if "Semiconductor" in industry:
                    etf = "SMH"
                else:
                    etf = SECTOR_ETF_MAP.get(sector, "QQQ" if t in DEFAULT_PEER_MAP else "SPY")
            if not peers:
                peers = [etf]
        except Exception:
            if not etf:
                etf = "QQQ" if t in DEFAULT_PEER_MAP else "SPY"
            if not peers:
                peers = [etf]

    return peers, (etf or "SPY")


def classify_move(alpha_vs_peers_pp: float | None, window_z: float | None) -> str:
    """
    判定规则（PS.10）：
    - sector_beta：当且仅当 |窗口超额| < 3 个百分点 且 |窗口 z| < 1.5
    - stock_specific：当 |窗口超额| >= 5 个百分点 或 |窗口 z| >= 2.0
    - mixed：中间情况
    """
    if alpha_vs_peers_pp is None:
        return "mixed"
    abs_exc = abs(alpha_vs_peers_pp)
    abs_z = abs(window_z) if window_z is not None else None

    if abs_exc >= ALPHA_MIN_EXCESS_PP or (abs_z is not None and abs_z >= ALPHA_MIN_Z):
        return "stock_specific"
    if abs_exc < BETA_MAX_EXCESS_PP and (abs_z is None or abs_z < BETA_MAX_Z):
        return "sector_beta"
    return "mixed"


def parse_window_spec(window_str: str, end_override: str | None = None) -> tuple[date, date | None, int | None, str]:
    """
    解析 --window 参数：
    返回 (end_date, start_date_or_None, n_trading_days_or_None, canonical_label)
    - '1d' / 'today' -> 取最近 1 个交易日（n_trading_days=1）
    - '5d' / '1w' / 'week' -> 取最近 5 个交易日（或距离上一周同一工作日）
    - '1m' / 'month' -> 取最近 21 个交易日
    - '3m' / 'quarter' -> 取最近 63 个交易日
    - 'YYYY-MM-DD' -> 单日窗口
    - 'YYYY-MM-DD to YYYY-MM-DD' -> 显式日期区间
    """
    w = (window_str or "1d").strip().lower()
    end_dt = date.fromisoformat(end_override) if end_override else date.today()

    if " to " in w:
        parts = [p.strip() for p in w.split(" to ", 1)]
        s_dt = date.fromisoformat(parts[0])
        e_dt = date.fromisoformat(parts[1])
        return e_dt, s_dt, None, f"{s_dt.isoformat()} to {e_dt.isoformat()}"

    if re.match(r"^\d{4}-\d{2}-\d{2}$", w):
        d = date.fromisoformat(w)
        return d, d, 1, w

    if w in ("1d", "today", "今天"):
        return end_dt, None, 1, "1d"
    if w in ("5d", "1w", "week", "这周", "上周", "本周", "最近"):
        return end_dt, None, 5, "1w"
    if w in ("1m", "month", "这个月", "本月"):
        return end_dt, None, 21, "1m"
    if w in ("3m", "quarter"):
        return end_dt, None, 63, "3m"
    m_nd = re.match(r"^(\d+)d$", w)
    if m_nd:
        n = max(1, int(m_nd.group(1)))
        return end_dt, None, n, f"{n}d"

    return end_dt, None, 5, w


def compute_quant_from_frames(
    target: str,
    peers: list[str],
    sector_etf: str,
    adj_close_df,
    close_df,
    volume_df,
    window_str: str = "1d",
    end_override: str | None = None,
    require_end_bar: bool = False,
) -> dict[str, Any]:
    """
    核心计算函数（可对真实 yfinance DataFrame 或测试 DataFrame 执行）。
    """
    import numpy as np
    import pandas as pd

    warnings_list: list[str] = []
    end_dt, start_dt, n_days, label = parse_window_spec(window_str, end_override)
    end_ts = pd.Timestamp(end_dt)

    if target not in adj_close_df.columns or adj_close_df[target].dropna().empty:
        return {
            "ok": False,
            "data_ok": False,
            "ticker": target,
            "window": label,
            "error": f"No price series found for {target}",
            "warnings": [f"Missing price series for {target}"],
        }

    t_adj = adj_close_df[target].dropna()
    t_adj = t_adj[t_adj.index <= end_ts]
    t_close = close_df[target].dropna().reindex(t_adj.index)

    if len(t_adj) < 2:
        return {
            "ok": False,
            "data_ok": False,
            "ticker": target,
            "window": label,
            "error": f"Insufficient price bars ({len(t_adj)}) for {target}",
            "warnings": ["Insufficient price history"],
        }

    last_bar_date = t_adj.index[-1].date()
    data_ok = True
    if require_end_bar and last_bar_date < end_dt:
        data_ok = False
        warnings_list.append(
            f"最新日线尚未就绪：目标终点 {end_dt.isoformat()}，但最新日线停在 {last_bar_date.isoformat()}（data_ok=false，不得下结论）。"
        )

    # 确定窗口内的交易日切片与窗口起点前最后一个交易日（基准日 base_idx）
    if start_dt is not None and n_days is None:
        start_ts = pd.Timestamp(start_dt)
        win_indices = t_adj.index[(t_adj.index >= start_ts) & (t_adj.index <= end_ts)]
        if len(win_indices) == 0:
            return {
                "ok": False,
                "data_ok": False,
                "ticker": target,
                "window": label,
                "error": f"No trading days in [{start_dt}, {end_dt}]",
                "warnings": ["No trading days in window"],
            }
        first_pos = t_adj.index.get_loc(win_indices[0])
        if first_pos == 0:
            return {
                "ok": False,
                "data_ok": False,
                "ticker": target,
                "window": label,
                "error": "No prior close bar before window start",
                "warnings": ["Missing prior close bar before window start"],
            }
        base_idx = t_adj.index[first_pos - 1]
    else:
        k = n_days or 1
        if len(t_adj) <= k:
            win_indices = t_adj.index[1:]
            base_idx = t_adj.index[0]
        else:
            win_indices = t_adj.index[-k:]
            base_idx = t_adj.index[-(k + 1)]

    end_idx = win_indices[-1]
    n_win_days = len(win_indices)

    def calc_window_ret_pct(sym: str) -> float | None:
        if sym not in adj_close_df.columns:
            return None
        s = adj_close_df[sym].dropna()
        s_before = s[s.index <= base_idx]
        s_end = s[s.index <= end_idx]
        if s_before.empty or s_end.empty:
            return None
        if s_end.index[-1] <= s_before.index[-1]:
            return 0.0
        p0 = float(s_before.iloc[-1])
        p1 = float(s_end.iloc[-1])
        if not p0:
            return None
        return (p1 / p0 - 1.0) * 100.0

    def calc_vol_vs_adtv(sym: str, bar_idx) -> tuple[int | None, float | None, float | None]:
        if sym not in volume_df.columns:
            return None, None, None
        v = volume_df[sym].dropna()
        v_up_to = v[v.index <= bar_idx]
        if len(v_up_to) < 2:
            return (int(v_up_to.iloc[-1]) if len(v_up_to) == 1 else None), None, None
        cur_vol = int(v_up_to.iloc[-1])
        prior_20 = v_up_to.iloc[:-1].tail(ADTV_LOOKBACK_DAYS)
        if prior_20.empty:
            return cur_vol, None, None
        adtv = float(prior_20.mean())
        ratio = (cur_vol / adtv) if adtv > 0 else None
        return cur_vol, adtv, ratio

    target_change_pct = calc_window_ret_pct(target)
    last_close_unadj = float(t_close.loc[end_idx])
    last_vol, adtv_20d, vol_vs_adtv = calc_vol_vs_adtv(target, end_idx)

    # 同组各成员表现
    peer_rows: list[dict[str, Any]] = []
    valid_peer_syms: list[str] = []
    for p in peers:
        if p == target:
            continue
        p_chg = calc_window_ret_pct(p)
        if p_chg is None:
            warnings_list.append(f"同组标的 {p} 缺少窗口行情数据，已跳过")
            continue
        valid_peer_syms.append(p)
        p_close_s = close_df[p].dropna() if p in close_df.columns else pd.Series(dtype=float)
        p_close_s = p_close_s[p_close_s.index <= end_idx]
        p_close = round(float(p_close_s.iloc[-1]), 2) if not p_close_s.empty else None
        _, _, p_vr = calc_vol_vs_adtv(p, end_idx)
        peer_rows.append({
            "ticker": p,
            "close": p_close,
            "change_pct": round(p_chg, 2),
            "volume_vs_adtv": round(p_vr, 2) if p_vr is not None else None,
        })

    peer_changes = [r["change_pct"] for r in peer_rows if r.get("change_pct") is not None]
    peer_median = float(np.median(peer_changes)) if peer_changes else None

    etf_change = calc_window_ret_pct(sector_etf) if sector_etf else None

    alpha_vs_peers = (target_change_pct - peer_median) if (target_change_pct is not None and peer_median is not None) else None
    alpha_vs_sector = (target_change_pct - etf_change) if (target_change_pct is not None and etf_change is not None) else None

    # 逐日收益率（百分数）与日超额序列
    rets_pct_df = adj_close_df.pct_change() * 100.0
    t_daily_ret = rets_pct_df[target].dropna()
    if valid_peer_syms:
        peer_daily_med = rets_pct_df[valid_peer_syms].median(axis=1)
    elif sector_etf and sector_etf in rets_pct_df.columns:
        peer_daily_med = rets_pct_df[sector_etf]
        warnings_list.append(f"无有效同组成份股，日超额回退使用板块 ETF ({sector_etf}) 计算")
    else:
        peer_daily_med = pd.Series(dtype=float)

    daily_excess = (t_daily_ret - peer_daily_med.reindex(t_daily_ret.index)).dropna()

    # 窗口起点前 60 个交易日日超额的标准差 σ（样本 > 20 才算）
    hist_excess = daily_excess[daily_excess.index <= base_idx].tail(HIST_LOOKBACK_DAYS)
    sigma_60d: float | None = None
    if len(hist_excess) > MIN_HIST_SAMPLES:
        val = float(hist_excess.std(ddof=1))
        if val > 0 and not math.isnan(val):
            sigma_60d = val
    else:
        warnings_list.append(f"窗口起点前历史日超额样本数 ({len(hist_excess)}) <= {MIN_HIST_SAMPLES}，不计算 z 值")

    daily_breakdown: list[dict[str, Any]] = []
    for d_idx in win_indices:
        d_close = float(t_close.loc[d_idx]) if d_idx in t_close.index else None
        d_ret = float(t_daily_ret.loc[d_idx]) if d_idx in t_daily_ret.index else None
        d_peer = (
            float(peer_daily_med.loc[d_idx])
            if (d_idx in peer_daily_med.index and not pd.isna(peer_daily_med.loc[d_idx]))
            else None
        )
        d_exc = float(daily_excess.loc[d_idx]) if d_idx in daily_excess.index else None
        d_z = (d_exc / sigma_60d) if (d_exc is not None and sigma_60d) else None
        d_vol, _, d_vr = calc_vol_vs_adtv(target, d_idx)
        daily_breakdown.append({
            "date": d_idx.strftime("%Y-%m-%d"),
            "close": round(d_close, 2) if d_close is not None else None,
            "change_pct": round(d_ret, 2) if d_ret is not None else None,
            "peer_median_pct": round(d_peer, 2) if d_peer is not None else None,
            "excess_pct": round(d_exc, 2) if d_exc is not None else None,
            "day_z": round(d_z, 2) if d_z is not None else None,
            "volume": d_vol,
            "volume_vs_adtv": round(d_vr, 2) if d_vr is not None else None,
        })

    last_day_z = daily_breakdown[-1]["day_z"] if daily_breakdown else None
    window_z: float | None = None
    if alpha_vs_peers is not None and sigma_60d and n_win_days >= 1:
        window_z = alpha_vs_peers / (sigma_60d * math.sqrt(n_win_days))

    assessment = classify_move(alpha_vs_peers, window_z)

    return {
        "ok": True,
        "session": "regular",
        "ticker": target,
        "close": round(last_close_unadj, 2),
        "change_pct": round(target_change_pct, 2) if target_change_pct is not None else None,
        "window": label,
        "base_date": base_idx.strftime("%Y-%m-%d"),
        "start_date": win_indices[0].strftime("%Y-%m-%d"),
        "end_date": end_idx.strftime("%Y-%m-%d"),
        "trading_days": n_win_days,
        "last_day_volume": last_vol,
        "adtv_20d": round(adtv_20d, 0) if adtv_20d is not None else None,
        "volume_vs_adtv": round(vol_vs_adtv, 2) if vol_vs_adtv is not None else None,
        "daily": daily_breakdown,
        "peers": peer_rows,
        "peer_median": round(peer_median, 2) if peer_median is not None else None,
        "sector_etf": sector_etf,
        "etf_change": round(etf_change, 2) if etf_change is not None else None,
        "alpha_vs_peers": round(alpha_vs_peers, 2) if alpha_vs_peers is not None else None,
        "alpha_vs_sector": round(alpha_vs_sector, 2) if alpha_vs_sector is not None else None,
        "sigma_60d_pct": round(sigma_60d, 2) if sigma_60d is not None else None,
        "day_z": round(last_day_z, 2) if last_day_z is not None else None,
        "window_z": round(window_z, 2) if window_z is not None else None,
        "assessment": assessment,
        "data_ok": data_ok,
        "warnings": warnings_list,
    }


INTRADAY_RETENTION_DAYS: dict[str, int] = {
    "1m": 7,
    "5m": 60,
    "15m": 60,
    "30m": 60,
    "60m": 730,
    "1h": 730,
}


def _is_us_trading_day(dt: date) -> bool:
    """判断 dt 是否为美股（XNYS/NYSE）交易日；无日历库时按工作日判断。"""
    ds = dt.isoformat()
    try:
        import exchange_calendars as xcals  # type: ignore

        cal = xcals.get_calendar("XNYS")
        if hasattr(cal, "is_session"):
            return bool(cal.is_session(ds))
    except Exception:
        pass
    try:
        import pandas_market_calendars as mcal  # type: ignore

        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=ds, end_date=ds)
        return not sched.empty
    except Exception:
        pass
    if dt.weekday() >= 5:
        return False
    holidays = _stdlib_nyse_holidays(dt.year)
    return holidays is None or dt not in holidays


def _stdlib_nyse_holidays(year: int) -> set[date] | None:
    """NYSE full-day holidays from the sibling earnings-calendar skill's stdlib calendar, or None if it is absent."""
    path = Path(__file__).resolve().parents[2] / "dalton-earnings-calendar" / "scripts" / "trading_days.py"
    if not path.is_file():
        return None
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_dalton_trading_days", path)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return set(mod.us_nyse_holidays(year))
    except Exception:
        return None


def _latest_us_trading_day_on_or_before(dt: date) -> date:
    cur = dt
    for _ in range(14):
        if _is_us_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    return cur


def _previous_trading_day(dt: date) -> date:
    return _latest_us_trading_day_on_or_before(dt - timedelta(days=1))


def _default_us_target_date(now_et: datetime | None = None) -> date:
    """未传 --end 时，目标日取美东当天；若当天为周末或节假日，取最近一个交易日。"""
    et_tz = ZoneInfo("America/New_York")
    cur_dt = (now_et or datetime.now(et_tz)).astimezone(et_tz).date()
    return _latest_us_trading_day_on_or_before(cur_dt)


def _resolve_regular_close_minutes(dt: date, et_tz: ZoneInfo) -> tuple[int, bool]:
    """查询美股日历获取 dt 当日常规收盘时刻（分钟数，默认 16:00=960，提前收市日 13:00=780）及是否完成校验。"""
    ds = dt.isoformat()
    try:
        import exchange_calendars as xcals  # type: ignore

        cal = xcals.get_calendar("XNYS")
        if cal.is_session(ds):
            close_ts = cal.session_close(ds).tz_convert(et_tz)
            return close_ts.hour * 60 + close_ts.minute, True
        return 16 * 60, True
    except Exception:
        pass

    try:
        import pandas_market_calendars as mcal  # type: ignore

        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=ds, end_date=ds)
        if not sched.empty and "market_close" in sched.columns:
            close_ts = sched["market_close"].iloc[0].tz_convert(et_tz)
            return close_ts.hour * 60 + close_ts.minute, True
        return 16 * 60, True
    except Exception:
        return 16 * 60, False


def compute_session_quant_from_intraday(
    target: str,
    peers: list[str],
    sector_etf: str | None,
    close_df,
    volume_df=None,
    session: str = "pre",
    session_date: str | None = None,
    futures: list[str] | None = None,
) -> dict[str, Any]:
    """
    基于含盘前盘后（prepost=True）的日内分钟线计算盘前（pre）或盘后（post）异动，
    并同步对照同组中位数、板块 ETF 与指数期货（NQ=F / ES=F）。
    """
    import numpy as np
    import pandas as pd

    sess = (session or "pre").strip().lower()
    if sess not in ("pre", "post"):
        return {
            "ok": False,
            "data_ok": False,
            "session": sess,
            "ticker": target,
            "error": f"Unsupported intraday session: {session} (expected pre or post)",
            "warnings": [f"Unsupported session {session}"],
        }

    ok_sym, reason = supports_extended_session(target)
    if not ok_sym:
        return {
            "ok": False,
            "data_ok": False,
            "session": sess,
            "ticker": target,
            "unsupported_session": True,
            "fallback_hint": "请改用 --session regular 计算常规交易时段涨跌。",
            "error": reason,
            "warnings": [reason or f"Unsupported extended session for {target}"],
        }

    warnings_list: list[str] = []
    et_tz = ZoneInfo("America/New_York")

    if close_df is None or close_df.empty or target not in close_df.columns:
        return {
            "ok": False,
            "data_ok": False,
            "session": sess,
            "ticker": target,
            "error": f"No intraday price series found for {target}",
            "warnings": [f"Missing intraday series for {target}"],
        }

    df_c = close_df.copy()
    if getattr(df_c.index, "tz", None) is None:
        df_c.index = pd.to_datetime(df_c.index).tz_localize(et_tz)
    else:
        df_c.index = pd.to_datetime(df_c.index).tz_convert(et_tz)
    df_c = df_c.sort_index()

    df_v = None
    if volume_df is not None and not volume_df.empty:
        df_v = volume_df.copy()
        if getattr(df_v.index, "tz", None) is None:
            df_v.index = pd.to_datetime(df_v.index).tz_localize(et_tz)
        else:
            df_v.index = pd.to_datetime(df_v.index).tz_convert(et_tz)
        df_v = df_v.sort_index()

    t_series = df_c[target].dropna()
    if t_series.empty:
        return {
            "ok": False,
            "data_ok": False,
            "session": sess,
            "ticker": target,
            "error": f"Empty intraday series for {target}",
            "warnings": [f"Empty intraday series for {target}"],
        }

    target_dt = date.fromisoformat(session_date) if session_date else _default_us_target_date()
    last_bar_dt = t_series.index[-1].date()
    data_ok = True
    if last_bar_dt != target_dt:
        data_ok = False
        warnings_list.append(f"最新分钟线停在 {last_bar_dt.isoformat()}")

    close_mins_cache: dict[date, int] = {}
    target_fail_reason: list[str] = []

    def _get_close_mins(d: date) -> int:
        if d not in close_mins_cache:
            c_mins, checked = _resolve_regular_close_minutes(d, et_tz)
            close_mins_cache[d] = c_mins
            if not checked and "未校验提前收市日" not in warnings_list:
                warnings_list.append("未校验提前收市日")
        return close_mins_cache[d]

    _get_close_mins(target_dt)

    def _slice_symbol_session(sym: str) -> dict[str, Any] | None:
        if sym not in df_c.columns:
            return None
        s = df_c[sym].dropna()
        if s.empty:
            return None

        dates = pd.Series(s.index.date, index=s.index)
        mins = pd.Series(s.index.hour * 60 + s.index.minute, index=s.index)

        if sess == "pre":
            # 盘前窗口：04:00 <= t < 09:30 ET；基准：前一交易日常规时段最后一根（09:30 <= t < 收盘时刻），不得退回更早交易日或其他时段
            sess_mask = (dates == target_dt) & (mins >= 4 * 60) & (mins < 9 * 60 + 30)
            sess_bars = s[sess_mask]
            prev_dt = _previous_trading_day(target_dt)
            prev_close_mins = _get_close_mins(prev_dt)
            base_bars = s[(dates == prev_dt) & (mins >= 9 * 60 + 30) & (mins < prev_close_mins)]
            if sess_bars.empty or base_bars.empty:
                if sym == target:
                    if base_bars.empty:
                        target_fail_reason.append(
                            f"{target} 缺少前一交易日 {prev_dt.isoformat()} 常规时段（09:30 <= t < 收盘）基准分钟线数据"
                        )
                    if sess_bars.empty:
                        target_fail_reason.append(
                            f"{target} 缺少 {target_dt.isoformat()} 盘前时段（04:00 <= t < 09:30 ET）分钟线数据"
                        )
                return None
        else:
            # 盘后窗口：收盘时刻 <= t < 20:00 ET；基准：当日常规时段最后一根（09:30 <= t < 收盘时刻），不得退回其他时段
            close_mins = _get_close_mins(target_dt)
            day_bars = s[dates == target_dt]
            day_mins = mins[dates == target_dt]
            sess_bars = day_bars[(day_mins >= close_mins) & (day_mins < 20 * 60)]
            base_bars = day_bars[(day_mins >= 9 * 60 + 30) & (day_mins < close_mins)]
            if sess_bars.empty or base_bars.empty:
                if sym == target:
                    if base_bars.empty:
                        target_fail_reason.append(
                            f"{target} 缺少当日 {target_dt.isoformat()} 常规时段（09:30 <= t < 收盘）基准分钟线数据"
                        )
                    if sess_bars.empty:
                        target_fail_reason.append(
                            f"{target} 缺少 {target_dt.isoformat()} 盘后时段（收盘 <= t < 20:00 ET）分钟线数据"
                        )
                return None

        p0 = float(base_bars.iloc[-1])
        p1 = float(sess_bars.iloc[-1])
        if not p0:
            return None
        chg_pct = (p1 / p0 - 1.0) * 100.0
        vol_sum: int | None = None
        if df_v is not None and sym in df_v.columns:
            v_slice = df_v[sym].reindex(sess_bars.index).dropna()
            if not v_slice.empty:
                raw_vol_sum = int(v_slice.sum())
                if raw_vol_sum == 0 and bool((v_slice == 0).all()):
                    vol_sum = None
                    if "yfinance 未提供盘前/盘后成交量" not in warnings_list:
                        warnings_list.append("yfinance 未提供盘前/盘后成交量")
                else:
                    vol_sum = raw_vol_sum
        return {
            "ticker": sym,
            "base_close": round(p0, 2),
            "close": round(p1, 2),
            "change_pct": round(chg_pct, 2),
            "base_timestamp": base_bars.index[-1].strftime("%Y-%m-%d %H:%M:%S%z"),
            "end_timestamp": sess_bars.index[-1].strftime("%Y-%m-%d %H:%M:%S%z"),
            "session_volume": vol_sum,
            "bar_count": int(len(sess_bars)),
        }

    t_info = _slice_symbol_session(target)
    if t_info is None:
        err_msg = "; ".join(target_fail_reason) or (
            f"No {sess}-market bars or prior regular close found for {target} on {target_dt.isoformat()}"
        )
        return {
            "ok": False,
            "data_ok": False,
            "session": sess,
            "ticker": target,
            "session_date": target_dt.isoformat(),
            "error": err_msg,
            "fallback_hint": "当前日期尚无盘前/盘后分钟线或对应常规时段基准收盘数据，请核对时段或退回 --session regular。",
            "warnings": [*warnings_list, *target_fail_reason],
        }

    peer_rows: list[dict[str, Any]] = []
    for p in peers:
        if p == target:
            continue
        ok_p, p_reason = supports_extended_session(p)
        if not ok_p:
            warnings_list.append(p_reason or f"同组标的 {p} 不支持 {sess} 时段，已跳过")
            continue
        p_info = _slice_symbol_session(p)
        if p_info is None:
            warnings_list.append(f"同组标的 {p} 缺少 {sess} 时段分钟线数据，已跳过")
            continue
        peer_rows.append({
            "ticker": p,
            "base_close": p_info["base_close"],
            "close": p_info["close"],
            "change_pct": p_info["change_pct"],
            "session_volume": p_info["session_volume"],
        })

    peer_changes = [r["change_pct"] for r in peer_rows if r.get("change_pct") is not None]
    peer_median = float(np.median(peer_changes)) if peer_changes else None

    etf_info = _slice_symbol_session(sector_etf) if sector_etf else None
    etf_change = etf_info["change_pct"] if etf_info is not None else None

    fut_list = futures if futures is not None else list(DEFAULT_FUTURES)
    futures_rows: list[dict[str, Any]] = []
    for f_sym in fut_list:
        f_info = _slice_symbol_session(f_sym)
        if f_info is None:
            warnings_list.append(f"期货标的 {f_sym} 缺少对应时段分钟线数据，已跳过")
            continue
        futures_rows.append({
            "ticker": f_sym,
            "base_close": f_info["base_close"],
            "close": f_info["close"],
            "change_pct": f_info["change_pct"],
        })

    primary_future = futures_rows[0]["ticker"] if futures_rows else None
    futures_change = futures_rows[0]["change_pct"] if futures_rows else None

    target_change_pct = t_info["change_pct"]
    alpha_vs_peers = (target_change_pct - peer_median) if peer_median is not None else None
    alpha_vs_sector = (target_change_pct - etf_change) if etf_change is not None else None
    alpha_vs_futures = (target_change_pct - futures_change) if futures_change is not None else None

    ref_alpha = alpha_vs_peers if alpha_vs_peers is not None else (
        alpha_vs_sector if alpha_vs_sector is not None else alpha_vs_futures
    )
    assessment = classify_move(ref_alpha, None)

    return {
        "ok": True,
        "data_ok": data_ok,
        "session": sess,
        "ticker": target,
        "session_date": target_dt.isoformat(),
        "base_timestamp": t_info["base_timestamp"],
        "end_timestamp": t_info["end_timestamp"],
        "base_close": t_info["base_close"],
        "close": t_info["close"],
        "change_pct": target_change_pct,
        "session_volume": t_info["session_volume"],
        "bar_count": t_info["bar_count"],
        "peers": peer_rows,
        "peer_median": round(peer_median, 2) if peer_median is not None else None,
        "sector_etf": sector_etf,
        "etf_change": round(etf_change, 2) if etf_change is not None else None,
        "futures": futures_rows,
        "futures_benchmark": primary_future,
        "futures_change": round(futures_change, 2) if futures_change is not None else None,
        "alpha_vs_peers": round(alpha_vs_peers, 2) if alpha_vs_peers is not None else None,
        "alpha_vs_sector": round(alpha_vs_sector, 2) if alpha_vs_sector is not None else None,
        "alpha_vs_futures": round(alpha_vs_futures, 2) if alpha_vs_futures is not None else None,
        "assessment": assessment,
        "warnings": warnings_list,
    }


def run_analyze(
    ticker: str,
    window: str = "1d",
    peers_arg: str | None = None,
    etf_arg: str | None = None,
    coverage_path: str | None = None,
    end_override: str | None = None,
    session: str = "regular",
    futures_arg: str | None = None,
    intraday_interval: str = "5m",
) -> dict[str, Any]:
    """拉取 yfinance 数据并执行统一异动量化分析（支持 regular / pre / post 时段）。"""
    import pandas as pd
    import yfinance as yf  # type: ignore

    t = ticker.upper().strip()
    sess = (session or "regular").strip().lower()
    peers, sector_etf = resolve_peers_and_etf(t, peers_arg, etf_arg, coverage_path)

    if sess in ("pre", "post"):
        ok_sym, reason = supports_extended_session(t)
        if not ok_sym:
            return {
                "ok": False,
                "data_ok": False,
                "session": sess,
                "ticker": t,
                "unsupported_session": True,
                "fallback_hint": "请改用 --session regular 计算常规交易时段涨跌。",
                "error": reason,
                "warnings": [reason or f"Unsupported extended session for {t}"],
            }
        explicit_date = bool(
            end_override
            or re.match(r"^\d{4}-\d{2}-\d{2}$", (window or "").strip())
            or " to " in (window or "").strip()
        )
        target_dt = (
            parse_window_spec(window, end_override)[0]
            if explicit_date
            else _default_us_target_date()
        )
        sess_date = target_dt.isoformat()
        prev_trading_dt = _previous_trading_day(target_dt)
        iv = (intraday_interval or "5m").strip().lower()
        max_retention_days = INTRADAY_RETENTION_DAYS.get(iv, 60)
        today_et = datetime.now(ZoneInfo("America/New_York")).date()
        span_age_days = max((today_et - prev_trading_dt).days, (today_et - target_dt).days)
        if span_age_days > max_retention_days:
            msg = (
                f"请求区间 {prev_trading_dt.isoformat()} 至 {target_dt.isoformat()} 距今 {span_age_days} 天，"
                f"超出分钟线保留期（{iv} 约 {max_retention_days} 天）"
            )
            return {
                "ok": False,
                "data_ok": False,
                "session": sess,
                "ticker": t,
                "session_date": target_dt.isoformat(),
                "error": msg,
                "fallback_hint": "请改用 --session regular 使用日线数据归因。",
                "warnings": [msg],
            }
        fut_syms = (
            [f.strip().upper() for f in futures_arg.split(",") if f.strip()]
            if futures_arg
            else list(DEFAULT_FUTURES)
        )
        supported_peers = [p for p in peers if supports_extended_session(p)[0]]
        symbols = sorted(
            {t}
            | set(supported_peers)
            | ({sector_etf} if sector_etf and supports_extended_session(sector_etf)[0] else set())
            | set(fut_syms)
        )
        dl_start = prev_trading_dt.isoformat()
        dl_end = (target_dt + timedelta(days=1)).isoformat()
        raw = yf.download(
            symbols,
            start=dl_start,
            end=dl_end,
            interval=iv,
            prepost=True,
            auto_adjust=False,
            group_by="ticker",
            progress=False,
            threads=True,
        )
        if raw is None or raw.empty:
            return {
                "ok": False,
                "data_ok": False,
                "session": sess,
                "ticker": t,
                "error": f"yfinance returned empty intraday (prepost) data for {symbols}",
                "warnings": ["yfinance returned empty intraday dataframe"],
            }

        def extract_intraday_field(field: str) -> pd.DataFrame:
            out = {}
            if isinstance(raw.columns, pd.MultiIndex):
                lvl0 = set(raw.columns.get_level_values(0))
                for s in symbols:
                    if s in lvl0 and field in raw[s].columns:
                        out[s] = raw[s][field]
            elif field in raw.columns:
                out[symbols[0] if len(symbols) == 1 else t] = raw[field]
            return pd.DataFrame(out)

        close_df = extract_intraday_field("Close")
        vol_df = extract_intraday_field("Volume")
        return compute_session_quant_from_intraday(
            target=t,
            peers=peers,
            sector_etf=sector_etf,
            close_df=close_df,
            volume_df=vol_df,
            session=sess,
            session_date=sess_date,
            futures=fut_syms,
        )

    end_dt, start_dt, n_days, _ = parse_window_spec(window, end_override)
    # 往前多拉约 140 个自然日以保证窗口起点前有至少 60 个交易日算 σ
    ref_start = start_dt if start_dt else (end_dt - timedelta(days=max((n_days or 5) * 2, 14)))
    dl_start = (ref_start - timedelta(days=140)).isoformat()
    dl_end = (end_dt + timedelta(days=2)).isoformat()

    symbols = sorted({t} | set(peers) | ({sector_etf} if sector_etf else set()))
    raw = yf.download(
        symbols,
        start=dl_start,
        end=dl_end,
        auto_adjust=False,
        group_by="ticker",
        progress=False,
        threads=True,
    )

    if raw is None or raw.empty:
        return {
            "ok": False,
            "data_ok": False,
            "ticker": t,
            "window": window,
            "error": f"yfinance returned empty data for {symbols}",
            "warnings": ["yfinance returned empty dataframe"],
        }

    def extract_field(field: str) -> pd.DataFrame:
        out = {}
        if isinstance(raw.columns, pd.MultiIndex):
            lvl0 = set(raw.columns.get_level_values(0))
            for s in symbols:
                if s in lvl0 and field in raw[s].columns:
                    out[s] = raw[s][field]
        else:
            if len(symbols) == 1 and field in raw.columns:
                out[symbols[0]] = raw[field]
        return pd.DataFrame(out)

    adj_df = extract_field("Adj Close")
    close_df = extract_field("Close")
    vol_df = extract_field("Volume")

    # 若显式指定了 --end 或单日 YYYY-MM-DD，要求最新日线到达该日（除非是周末自动回退前一日）
    require_end = bool(end_override or re.match(r"^\d{4}-\d{2}-\d{2}$", (window or "").strip()))
    return compute_quant_from_frames(
        target=t,
        peers=peers,
        sector_etf=sector_etf,
        adj_close_df=adj_df,
        close_df=close_df,
        volume_df=vol_df,
        window_str=window,
        end_override=end_override,
        require_end_bar=require_end,
    )


def run_self_test() -> dict[str, Any]:
    """自检：验证依赖导入与合成数据的量化算法（中位数、60d σ、day_z、window_z、三分类）。"""
    import numpy as np
    import pandas as pd

    dates = pd.bdate_range("2026-06-01", periods=75)
    rng = np.random.default_rng(42)
    # 构造 70 天温和波动 + 最后 1 天目标大跌 -6.5%、同组微跌 -0.5%
    base_rets = rng.normal(0.0005, 0.008, size=(75, 5))
    cols = ["META", "GOOGL", "AMZN", "MSFT", "QQQ"]
    rets_df = pd.DataFrame(base_rets, index=dates, columns=cols)
    rets_df.iloc[-1, 0] = -0.065  # META 单日 -6.5%
    rets_df.iloc[-1, 1:] = -0.005

    prices = 100.0 * (1.0 + rets_df).cumprod()
    vols = pd.DataFrame(1_000_000, index=dates, columns=cols)
    vols.iloc[-1, 0] = 2_800_000  # 2.8x ADTV

    res_alpha = compute_quant_from_frames(
        target="META",
        peers=["GOOGL", "AMZN", "MSFT"],
        sector_etf="QQQ",
        adj_close_df=prices,
        close_df=prices,
        volume_df=vols,
        window_str="1d",
        end_override=dates[-1].strftime("%Y-%m-%d"),
        require_end_bar=True,
    )

    # 构造板块共振：目标与同组同时跌 -4.2%
    rets_beta = rets_df.copy()
    rets_beta.iloc[-1, :] = -0.042
    prices_beta = 100.0 * (1.0 + rets_beta).cumprod()
    res_beta = compute_quant_from_frames(
        target="META",
        peers=["GOOGL", "AMZN", "MSFT"],
        sector_etf="QQQ",
        adj_close_df=prices_beta,
        close_df=prices_beta,
        volume_df=vols,
        window_str="1d",
        end_override=dates[-1].strftime("%Y-%m-%d"),
        require_end_bar=True,
    )

    # 构造盘前异动合成测试：前一交易日 15:55 收盘 100，当日盘前 08:30 GOOGL 跌至 96 (-4%)，同组与 NQ=F 跌 -1%
    pre_idx = pd.to_datetime([
        "2026-10-20 15:50:00-04:00",
        "2026-10-20 15:55:00-04:00",
        "2026-10-21 04:15:00-04:00",
        "2026-10-21 08:30:00-04:00",
    ])
    pre_close = pd.DataFrame(
        {
            "GOOGL": [100.0, 100.0, 98.0, 96.0],
            "META": [100.0, 100.0, 99.5, 99.0],
            "AMZN": [100.0, 100.0, 99.2, 99.0],
            "QQQ": [100.0, 100.0, 99.3, 98.8],
            "NQ=F": [20000.0, 20000.0, 19900.0, 19800.0],
            "ES=F": [5800.0, 5800.0, 5780.0, 5765.2],
        },
        index=pre_idx,
    )
    res_pre = compute_session_quant_from_intraday(
        target="GOOGL",
        peers=["META", "AMZN"],
        sector_etf="QQQ",
        close_df=pre_close,
        session="pre",
        session_date="2026-10-21",
        futures=["NQ=F", "ES=F"],
    )
    res_unsup = compute_session_quant_from_intraday(
        target="0700.HK",
        peers=[],
        sector_etf=None,
        close_df=pre_close,
        session="pre",
    )

    checks = {
        "alpha_classification": res_alpha["assessment"] == "stock_specific",
        "alpha_volume_ratio": res_alpha["volume_vs_adtv"] == 2.8,
        "alpha_excess_pp": abs(res_alpha["alpha_vs_peers"] - (-6.0)) < 0.05,
        "beta_classification": res_beta["assessment"] == "sector_beta",
        "beta_excess_zero": abs(res_beta["alpha_vs_peers"]) < 0.01,
        "data_ok_true": res_alpha["data_ok"] is True,
        "pre_session_change": res_pre["ok"] and abs(res_pre["change_pct"] - (-4.0)) < 0.01,
        "pre_session_futures": abs(res_pre["futures_change"] - (-1.0)) < 0.01,
        "unsupported_fallback": (res_unsup["ok"] is False and res_unsup.get("unsupported_session") is True),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "sample_alpha": res_alpha,
        "sample_beta": res_beta,
        "sample_pre": res_pre,
    }


def print_human(res: dict[str, Any]) -> None:
    if not res.get("ok"):
        print(f"ERROR: {res.get('error')}", file=sys.stderr)
        for w in res.get("warnings") or []:
            print(f"  WARN: {w}", file=sys.stderr)
        return
    print(f"=== {res['ticker']} ({res['window']}: {res.get('start_date')} -> {res.get('end_date')}) ===")
    print(f"Close: {res['close']} | Window Return (Adj): {res['change_pct']:+.2f}% | Vol/ADTV: {res.get('volume_vs_adtv')}x")
    print(
        f"Peer Median: {res.get('peer_median')}% | Sector ETF ({res.get('sector_etf')}): {res.get('etf_change')}% | "
        f"Alpha vs Peers: {res.get('alpha_vs_peers')}pp | Alpha vs ETF: {res.get('alpha_vs_sector')}pp"
    )
    print(
        f"60d Excess Sigma: {res.get('sigma_60d_pct')}% | day_z: {res.get('day_z')} | "
        f"window_z: {res.get('window_z')} | Assessment: {res.get('assessment')} | data_ok: {res.get('data_ok')}"
    )
    if res.get("warnings"):
        print("Warnings:", "; ".join(res["warnings"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Dalton 股价异动统一量化工具 (PS.10)")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")

    sub = parser.add_subparsers(dest="command")

    p_an = sub.add_parser("analyze", help="量化目标股票的区间涨跌、量比、同组中位数、60日超额 z 值与 beta/alpha 分类")
    p_an.add_argument("--ticker", required=True, help="目标股票代码，如 META")
    p_an.add_argument("--window", default="1d", help="时间窗口：1d, 1w, 1m, 3m, YYYY-MM-DD, 或 'YYYY-MM-DD to YYYY-MM-DD'")
    p_an.add_argument(
        "--session",
        default="regular",
        choices=["regular", "pre", "post"],
        help="交易时段：regular（默认日线窗口）、pre（盘前分钟线）、post（盘后分钟线）",
    )
    p_an.add_argument("--peers", help="覆盖同组列表（逗号分隔，如 GOOGL,AMZN,MSFT）")
    p_an.add_argument("--etf", help="覆盖板块 ETF（如 XLK, QQQ）")
    p_an.add_argument("--futures", help="覆盖指数期货列表（逗号分隔，默认 NQ=F,ES=F）")
    p_an.add_argument("--coverage", help="coverage.yaml 路径（默认自动查找 ../Home/dalton/coverage.yaml）")
    p_an.add_argument("--end", help="指定窗口终点日期 YYYY-MM-DD")
    p_an.add_argument("--out", help="将 JSON 结果同时写入指定文件路径")
    p_an.add_argument("--json", action="store_true", dest="sub_json", help="输出 JSON 格式")

    p_st = sub.add_parser("self-test", help="运行合成数据自检")
    p_st.add_argument("--json", action="store_true", dest="sub_json", help="输出 JSON 格式")

    args = parser.parse_args()
    as_json = bool(getattr(args, "json", False) or getattr(args, "sub_json", False))

    if args.command == "self-test":
        res = run_self_test()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    if args.command != "analyze":
        parser.print_help()
        return 0

    try:
        res = run_analyze(
            ticker=args.ticker,
            window=args.window,
            peers_arg=args.peers,
            etf_arg=args.etf,
            coverage_path=args.coverage,
            end_override=args.end,
            session=args.session,
            futures_arg=args.futures,
        )
    except Exception as e:
        res = {"ok": False, "data_ok": False, "ticker": args.ticker, "error": str(e), "warnings": [str(e)]}

    if getattr(args, "out", None):
        out_p = Path(args.out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    if as_json or True:
        # 默认输出标准 JSON（PS.10 契约），当不带 --json 时若用户希望看可读输出也可通过 JSON 读取
        if as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(res, ensure_ascii=False, indent=2))

    return 0 if (res.get("ok") and res.get("data_ok", False)) else 1


if __name__ == "__main__":
    sys.exit(main())
