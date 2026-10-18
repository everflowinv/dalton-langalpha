#!/usr/bin/env python3
"""
screen_movers.py — 周会全覆盖名单（Core + Watch）同组异动筛选脚本（PS.13）

功能：
1. 优先从 coverage.yaml（--coverage）读取全覆盖名单、层级（Core/Watch）、同组（peer_group/peers）、额外 ETF（etfs）与持仓标记；
   若未提供或文件不存在，自动回退到内置完整基准名单（TICKERS + EXTRA_PEERS）。
2. 自动跳过非上市私有公司（如 SPCX, OpenAI, Anthropic）。
3. 对每家覆盖公司计算：
   - 窗口复权收益（window_ret）
   - 同组中位数（peer_median，含同组未覆盖 peer 与组内额外 ETF）
   - 窗口超额（window_excess = window_ret - peer_median）
   - 窗口起点前 60 个交易日日超额标准差 σ（样本 > 20）
   - 每日超额（excess）、日 z 值（z）、单日量比（vol_ratio，对前 20 日均量）
   - 窗口 z 值（window_z = window_excess / (σ * sqrt(n))）
4. Watch 命中规则（原样移植）：
   - 窗口超额 >= ±6pp 或 |window_z| >= 2.0
   - 单日超额 >= ±5pp 或 |day_z| >= 3.0
   - 单日量比 >= 2.5x
5. 最新日线检查：若超过一半美股代码的最后交易日 < 预期终点交易日，ok=false 并输出 warnings。
6. 输出 JSON 结构对齐 PS.13 与 weekly-coverage-review workflow 的 SCREEN schema：
   {base, end, ok, warnings, bench, core, flagged, unflagged_watch, rows}
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from datetime import date, timedelta
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")

PRIVATE_TICKERS = {"SPCX", "OPENAI", "ANTHROPIC"}
PRIVATE_NAMES = {"SpaceX", "OpenAI", "Anthropic"}

# 内置回退名单：name -> (ticker, peer_group, sheet_section, tier)
BUILTIN_TICKERS: dict[str, tuple[str, str, str, str]] = {
    # Live Position
    "Google": ("GOOGL", "megacap", "Live Position", "Core"),
    "Amazon": ("AMZN", "megacap", "Live Position", "Core"),
    "DoorDash": ("DASH", "delivery", "Live Position", "Core"),
    "Affirm": ("AFRM", "bnpl", "Live Position", "Core"),
    "EchoStar": ("ECHO", "space", "Live Position", "Watch"),
    "D.R. Horton": ("DHI", "homebuilder", "Live Position", "Watch"),
    "唯品会": ("VIPS", "cn_ecom_vertical", "Live Position", "Watch"),
    "亚朵": ("ATAT", "cn_travel_local", "Live Position", "Core"),
    "Boss直聘": ("BZ", "cn_internet_small", "Live Position", "Core"),
    # US Mega Tech
    "Apple": ("AAPL", "megacap", "US Mega Tech", "Watch"),
    "Microsoft": ("MSFT", "megacap", "US Mega Tech", "Core"),
    "SpaceX": ("SPCX", "space", "US Mega Tech", "Core"),
    "Meta": ("META", "megacap", "US Mega Tech", "Core"),
    "Tesla": ("TSLA", "megacap", "US Mega Tech", "Watch"),
    # US Internet
    "Netflix": ("NFLX", "streaming_media", "US Internet", "Watch"),
    "Spotify": ("SPOT", "streaming_media", "US Internet", "Watch"),
    "MercadoLibre": ("MELI", "ecom", "US Internet", "Watch"),
    "Sea": ("SE", "ecom", "US Internet", "Watch"),
    "Coupang": ("CPNG", "ecom", "US Internet", "Watch"),
    "Grab": ("GRAB", "delivery", "US Internet", ""),
    "Uber": ("UBER", "delivery", "US Internet", "Core"),
    "Instacart": ("CART", "delivery", "US Internet", ""),
    "Lyft": ("LYFT", "delivery", "US Internet", ""),
    "Booking": ("BKNG", "online_travel", "US Internet", "Watch"),
    "Airbnb": ("ABNB", "online_travel", "US Internet", "Watch"),
    "Expedia": ("EXPE", "online_travel", "US Internet", "Watch"),
    "Roblox": ("RBLX", "games_adtech", "US Internet", "Watch"),
    "Duolingo": ("DUOL", "social_apps", "US Internet", "Watch"),
    "AppLovin": ("APP", "games_adtech", "US Internet", "Watch"),
    "Unity": ("U", "games_adtech", "US Internet", "Watch"),
    "Reddit": ("RDDT", "social_apps", "US Internet", "Watch"),
    "Pinterest": ("PINS", "social_apps", "US Internet", ""),
    "Snap": ("SNAP", "social_apps", "US Internet", ""),
    # US Financial
    "JPMorgan": ("JPM", "banks", "US Financial", "Watch"),
    "BofA": ("BAC", "banks", "US Financial", "Watch"),
    "Visa": ("V", "networks", "US Financial", "Core"),
    "Mastercard": ("MA", "networks", "US Financial", "Core"),
    "Shopify": ("SHOP", "ecom", "US Financial", "Watch"),
    "PayPal": ("PYPL", "payments", "US Financial", "Watch"),
    "Block": ("XYZ", "payments", "US Financial", "Watch"),
    "SoFi": ("SOFI", "fintech_lenders", "US Financial", "Watch"),
    "Klarna": ("KLAR", "bnpl", "US Financial", "Watch"),
    "Sezzle": ("SEZL", "bnpl", "US Financial", "Watch"),
    "Upstart": ("UPST", "fintech_lenders", "US Financial", "Watch"),
    "Robinhood": ("HOOD", "brokers", "US Financial", "Core"),
    "Interactive Brokers": ("IBKR", "brokers", "US Financial", "Core"),
    "Blackstone": ("BX", "alts", "US Financial", "Watch"),
    "KKR": ("KKR", "alts", "US Financial", "Watch"),
    "Apollo": ("APO", "alts", "US Financial", "Watch"),
    "S&P Global": ("SPGI", "ratings_info", "US Financial", "Core"),
    "Moody's": ("MCO", "ratings_info", "US Financial", "Core"),
    "Berkshire": ("BRK-B", "insurers", "US Financial", "Watch"),
    "Progressive": ("PGR", "insurers", "US Financial", "Watch"),
    "Marsh": ("MRSH", "ins_brokers", "US Financial", "Watch"),
    "Aon": ("AON", "ins_brokers", "US Financial", "Watch"),
    "Gallagher": ("AJG", "ins_brokers", "US Financial", "Watch"),
    "CME": ("CME", "exchanges", "US Financial", "Watch"),
    "ICE": ("ICE", "exchanges", "US Financial", "Watch"),
    "Cboe": ("CBOE", "exchanges", "US Financial", "Watch"),
    "Nasdaq": ("NDAQ", "exchanges", "US Financial", "Core"),
    # US Software
    "Palantir": ("PLTR", "infra_software", "US Software", "Watch"),
    "Salesforce": ("CRM", "app_software", "US Software", "Watch"),
    "ServiceNow": ("NOW", "app_software", "US Software", ""),
    "Cloudflare": ("NET", "infra_software", "US Software", "Watch"),
    "Huron": ("HURN", "it_services", "US Software", "Watch"),
    # Global Consumer & Homebuilder
    "Philip Morris": ("PM", "staples", "Global Consumer", "Watch"),
    "Walmart": ("WMT", "mass_retail", "Global Consumer", "Watch"),
    "Marriott": ("MAR", "lodging_cruise", "Global Consumer", "Watch"),
    "Hilton": ("HLT", "lodging_cruise", "Global Consumer", "Watch"),
    "Rocket Lab": ("RKLB", "space", "US Space", "Watch"),
    "Planet Labs": ("PL", "space", "US Space", "Watch"),
    "Quanta": ("PWR", "ai_construction", "AI Construction", "Watch"),
    "Comfort Systems": ("FIX", "ai_construction", "AI Construction", "Watch"),
    "Sterling": ("STRL", "ai_construction", "AI Construction", "Watch"),
    "Pulte": ("PHM", "homebuilder", "Homebuilder", "Watch"),
    "Toll Brothers": ("TOL", "homebuilder", "Homebuilder", "Watch"),
    # China Internet & Consumer
    "腾讯": ("0700.HK", "cn_mega", "China Internet", "Core"),
    "阿里巴巴": ("BABA", "cn_mega", "China Internet", "Core"),
    "拼多多": ("PDD", "cn_mega", "China Internet", "Core"),
    "网易": ("NTES", "cn_mega", "China Internet", "Core"),
    "美团": ("3690.HK", "cn_mega", "China Internet", "Core"),
    "京东": ("JD", "cn_mega", "China Internet", "Watch"),
    "百度": ("BIDU", "cn_mega", "China Internet", "Watch"),
    "携程": ("TCOM", "cn_travel_local", "China Internet", "Core"),
    "贝壳": ("BEKE", "cn_internet_small", "China Internet", "Core"),
    "富途": ("FUTU", "cn_brokers_exch", "China Internet", "Core"),
    "华住": ("HTHT", "cn_travel_local", "China Consumer", "Core"),
    "智谱": ("2513.HK", "cn_ai", "AI Native", "Core"),
}

EXTRA_PEERS: dict[str, list[str]] = {
    "space": ["UFO"],
    "homebuilder": ["XHB", "ITB"],
    "ai_construction": ["PAVE", "VRT"],
    "bnpl": ["PYPL", "XYZ", "UPST"],
    "online_travel": ["TCOM", "TRIP"],
    "app_software": ["IGV"],
    "infra_software": ["IGV"],
    "cn_mega": ["KWEB"],
    "cn_ecom_vertical": ["BABA", "PDD", "JD"],
    "cn_travel_local": ["KWEB"],
    "cn_internet_small": ["KWEB"],
    "cn_content": ["KWEB"],
    "cn_games": ["NTES", "0700.HK"],
    "cn_brokers_exch": ["HOOD", "IBKR"],
    "cn_logistics": ["KWEB"],
    "cn_health": ["KWEB"],
}

BENCH = ["SPY", "QQQ", "KWEB", "XLF", "XLY", "IWM", "IGV", "^HSI"]

# Watch 异动命中阈值（PS.13）
WK_EXCESS_PP = 0.06
WK_Z = 2.0
DAY_EXCESS_PP = 0.05
DAY_Z = 3.0
VOL_RATIO = 2.5


def load_universe_from_coverage(coverage_path: str | None) -> tuple[list[dict[str, Any]], dict[str, set[str]], list[str]]:
    """
    返回 (entries, peer_groups_map, skipped_private)
    每个 entry: {name, ticker, group, section, tier, held, workspace_dir, etfs, explicit_peers}
    """
    candidates = [Path(coverage_path)] if coverage_path else [
        Path("dalton/coverage.yaml"),
        Path("../Home/dalton/coverage.yaml"),
        Path("config/dalton/coverage.yaml"),
    ]
    cov_file = next((p for p in candidates if p.exists()), None)
    entries: list[dict[str, Any]] = []
    groups: dict[str, set[str]] = {}
    skipped_private: list[str] = []

    if cov_file:
        text = cov_file.read_text(encoding="utf-8")
        data = None
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(text)
        except Exception:
            try:
                data = json.loads(text)
            except Exception:
                data = None

        if isinstance(data, dict) and isinstance(data.get("companies"), list):
            for c in data["companies"]:
                t = str(c.get("ticker") or "").strip().upper()
                name = str(c.get("name") or t).strip()
                if not t or t in PRIVATE_TICKERS or name in PRIVATE_NAMES:
                    skipped_private.append(f"{name} ({t})")
                    continue
                raw_tier = str(c.get("tier") or "").strip().capitalize()
                tier = raw_tier if raw_tier in ("Core", "Watch") else ""
                group = str(c.get("peer_group") or "default").strip()
                sec = str(c.get("sheet_section") or "").strip()
                held = bool(c.get("held") or sec == "Live Position")
                ws_name = (c.get("workspace") or {}).get("name") if isinstance(c.get("workspace"), dict) else t
                ws_dir = f"../{ws_name or t}"
                peers_list = [str(p).upper() for p in (c.get("peers") or []) if str(p).upper() not in PRIVATE_TICKERS]
                etfs_list = [str(e).upper() for e in (c.get("etfs") or [])]

                groups.setdefault(group, set()).add(t)
                for p in peers_list:
                    groups[group].add(p)
                for e in etfs_list:
                    groups[group].add(e)

                entries.append({
                    "name": name,
                    "ticker": t,
                    "group": group,
                    "section": sec,
                    "tier": tier,
                    "held": held,
                    "workspace_dir": ws_dir,
                    "explicit_peers": peers_list,
                    "etfs": etfs_list,
                })

    if not entries:
        for name, (t, g, sec, tier) in BUILTIN_TICKERS.items():
            if t in PRIVATE_TICKERS or name in PRIVATE_NAMES:
                skipped_private.append(f"{name} ({t})")
                continue
            groups.setdefault(g, set()).add(t)
            entries.append({
                "name": name,
                "ticker": t,
                "group": g,
                "section": sec,
                "tier": tier,
                "held": sec == "Live Position",
                "workspace_dir": f"../{t}",
                "explicit_peers": [],
                "etfs": EXTRA_PEERS.get(g, []),
            })

    for g, extra in EXTRA_PEERS.items():
        if g in groups:
            groups[g].update(extra)

    return entries, groups, skipped_private


def summarize_event_hint(flags: list[dict[str, Any]]) -> str:
    if not flags:
        return ""
    parts = []
    for f in flags[:3]:
        ftype = f.get("type")
        if ftype == "window":
            exc = (f.get("excess") or 0.0) * 100.0
            z = f.get("z")
            parts.append(f"周超额 {exc:+.1f}pp" + (f" (z={z:.1f})" if z is not None else ""))
        elif ftype == "day":
            exc = (f.get("excess") or 0.0) * 100.0
            parts.append(f"{f.get('date')} 单日超额 {exc:+.1f}pp")
        elif ftype == "volume":
            parts.append(f"{f.get('date')} 量比 {f.get('vol_ratio', 0):.1f}x")
    return "；".join(parts)


def screen_from_frames(
    entries: list[dict[str, Any]],
    groups: dict[str, set[str]],
    adj_df,
    vol_df,
    base: str,
    end: str,
) -> tuple[list[dict[str, Any]], list[str], dict[str, float | None]]:
    import numpy as np
    import pandas as pd

    base_ts, end_ts = pd.Timestamp(base), pd.Timestamp(end)

    def get_series(sym: str, df) -> pd.Series:
        if sym not in df.columns:
            return pd.Series(dtype=float)
        s = df[sym].dropna()
        return s[s.index <= end_ts]

    adj = {sym: get_series(sym, adj_df) for sym in adj_df.columns}
    vol = {sym: get_series(sym, vol_df) for sym in vol_df.columns}
    rets = {sym: s.pct_change() for sym, s in adj.items() if len(s) > 1}

    def last_on_or_before(s: pd.Series, dt_ts: pd.Timestamp) -> tuple[pd.Timestamp | None, float | None]:
        sub = s[s.index <= dt_ts]
        return (sub.index[-1], float(sub.iloc[-1])) if len(sub) else (None, None)

    def window_ret(sym: str) -> float | None:
        s = adj.get(sym)
        if s is None or s.empty:
            return None
        d0, p0 = last_on_or_before(s, base_ts)
        d1, p1 = last_on_or_before(s, end_ts)
        if p0 is None or p1 is None or p0 == 0:
            return None
        if d1 <= d0:
            return 0.0
        return p1 / p0 - 1.0

    rows: list[dict[str, Any]] = []
    missing: list[str] = []

    for e in entries:
        tier = e["tier"]
        if not tier:
            continue
        t = e["ticker"]
        name = e["name"]
        g = e["group"]
        s = adj.get(t)
        if s is None or s.empty:
            missing.append(f"{name} ({t})")
            continue

        peers = sorted((groups.get(g, set()) | set(e.get("explicit_peers") or [])) - {t})
        w = window_ret(t)
        pw = [x for x in (window_ret(p) for p in peers) if x is not None]
        peer_med = float(np.median(pw)) if pw else None

        r = rets.get(t, pd.Series(dtype=float))
        peer_df = pd.DataFrame({p: rets[p] for p in peers if p in rets})
        peer_daily = peer_df.median(axis=1) if not peer_df.empty else pd.Series(dtype=float)
        exc = (r - peer_daily.reindex(r.index)).dropna()
        hist = exc[exc.index <= base_ts].tail(60)
        sd = float(hist.std(ddof=1)) if len(hist) > 20 else None
        if sd is not None and (sd <= 0 or math.isnan(sd)):
            sd = None

        v = vol.get(t, pd.Series(dtype=float))
        vhist = v[v.index <= base_ts].tail(20)
        vavg = float(vhist.mean()) if len(vhist) > 0 else None

        days: list[dict[str, Any]] = []
        for d in r.index[(r.index > base_ts) & (r.index <= end_ts)]:
            de = float(exc.loc[d]) if d in exc.index else None
            days.append({
                "date": str(d.date()),
                "ret": round(float(r.loc[d]), 6),
                "peer": round(float(peer_daily.loc[d]), 6) if (d in peer_daily.index and not pd.isna(peer_daily.loc[d])) else None,
                "excess": round(de, 6) if de is not None else None,
                "z": round(de / sd, 2) if (de is not None and sd) else None,
                "vol_ratio": round(float(v.loc[d]) / vavg, 2) if (vavg and vavg > 0 and d in v.index) else None,
            })

        wk_exc = (w - peer_med) if (w is not None and peer_med is not None) else None
        wk_z = (wk_exc / (sd * math.sqrt(max(len(days), 1)))) if (wk_exc is not None and sd) else None

        flags: list[dict[str, Any]] = []
        if tier == "Watch":
            if wk_exc is not None and (abs(wk_exc) >= WK_EXCESS_PP or (wk_z is not None and abs(wk_z) >= WK_Z)):
                flags.append({"type": "window", "excess": round(wk_exc, 4), "z": round(wk_z, 2) if wk_z is not None else None})
            for x in days:
                if x["excess"] is not None and (abs(x["excess"]) >= DAY_EXCESS_PP or (x["z"] is not None and abs(x["z"]) >= DAY_Z)):
                    flags.append({"type": "day", **x})
                elif x["vol_ratio"] is not None and x["vol_ratio"] >= VOL_RATIO:
                    flags.append({"type": "volume", **x})

        last_d, _ = last_on_or_before(s, end_ts)
        rows.append({
            "name": name,
            "ticker": t,
            "group": g,
            "section": e["section"],
            "tier": tier,
            "deep": tier == "Core",
            "held": bool(e.get("held")),
            "workspace_dir": e["workspace_dir"],
            "last_date": str(last_d.date()) if last_d is not None else None,
            "peers": peers,
            "window_ret": round(w, 6) if w is not None else None,
            "peer_median": round(peer_med, 6) if peer_med is not None else None,
            "window_excess": round(wk_exc, 6) if wk_exc is not None else None,
            "window_z": round(wk_z, 2) if wk_z is not None else None,
            "days": days,
            "flags": flags,
            "event_hint": summarize_event_hint(flags),
        })

    bench_rets = {b: (round(window_ret(b), 6) if window_ret(b) is not None else None) for b in BENCH}
    return rows, missing, bench_rets


def run_self_test() -> dict[str, Any]:
    import numpy as np
    import pandas as pd

    entries, groups, skipped = load_universe_from_coverage(None)
    dates = pd.bdate_range("2026-07-01", periods=70)
    rng = np.random.default_rng(7)
    syms = ["META", "GOOGL", "AMZN", "MSFT", "ECHO", "UFO", "SPY", "QQQ"]
    rets = pd.DataFrame(rng.normal(0.0005, 0.006, size=(70, len(syms))), index=dates, columns=syms)
    # 让 Watch 标的 ECHO 在最后一周暴涨 +12% 以触发 Watch flag
    rets.loc[dates[-3], "ECHO"] = 0.08
    rets.loc[dates[-2], "ECHO"] = 0.04
    adj_df = 100.0 * (1.0 + rets).cumprod()
    vol_df = pd.DataFrame(1_000_000, index=dates, columns=syms)

    sub_entries = [e for e in entries if e["ticker"] in {"META", "GOOGL", "AMZN", "MSFT", "ECHO"}]
    sub_groups = {"megacap": {"META", "GOOGL", "AMZN", "MSFT"}, "space": {"ECHO", "UFO"}}

    base_str = dates[-6].strftime("%Y-%m-%d")
    end_str = dates[-1].strftime("%Y-%m-%d")
    rows, _, _ = screen_from_frames(sub_entries, sub_groups, adj_df, vol_df, base_str, end_str)
    echo_row = next(r for r in rows if r["ticker"] == "ECHO")
    meta_row = next(r for r in rows if r["ticker"] == "META")

    checks = {
        "private_skipped": any("SPCX" in s for s in skipped),
        "core_identified": meta_row["deep"] is True,
        "watch_flagged": len(echo_row["flags"]) > 0 and bool(echo_row["event_hint"]),
    }
    return {"ok": all(checks.values()), "checks": checks, "skipped_private": skipped}


def main() -> int:
    ap = argparse.ArgumentParser(description="Dalton 周会全覆盖名单同组异动筛选脚本 (PS.13)")
    ap.add_argument("command", nargs="?", default="screen", choices=["screen", "self-test"])
    ap.add_argument("--coverage", help="coverage.yaml 路径（默认自动查找 dalton/coverage.yaml）")
    ap.add_argument("--base", help="窗口起点收盘日 YYYY-MM-DD（默认 end - 7 天）")
    ap.add_argument("--end", help="窗口终点收盘日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--out", help="将完整 JSON 结果写入该文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = ap.parse_args()

    if args.command == "self-test":
        res = run_self_test()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    import pandas as pd
    import yfinance as yf  # type: ignore

    end_str = args.end or date.today().isoformat()
    base_str = args.base or (date.fromisoformat(end_str) - timedelta(days=7)).isoformat()
    hist_start = (date.fromisoformat(base_str) - timedelta(days=140)).isoformat()

    entries, groups, skipped_private = load_universe_from_coverage(args.coverage)
    all_tickers = sorted({t for s in groups.values() for t in s if t not in PRIVATE_TICKERS} | set(BENCH))

    try:
        end_ts = pd.Timestamp(end_str)
        raw = yf.download(
            all_tickers,
            start=hist_start,
            end=(end_ts + pd.Timedelta(days=2)).strftime("%Y-%m-%d"),
            auto_adjust=False,
            group_by="ticker",
            progress=False,
            threads=True,
        )
        adj_dict, vol_dict = {}, {}
        if isinstance(raw.columns, pd.MultiIndex):
            lvl0 = set(raw.columns.get_level_values(0))
            for sym in all_tickers:
                if sym in lvl0:
                    if "Adj Close" in raw[sym].columns:
                        adj_dict[sym] = raw[sym]["Adj Close"]
                    if "Volume" in raw[sym].columns:
                        vol_dict[sym] = raw[sym]["Volume"]
        adj_df = pd.DataFrame(adj_dict)
        vol_df = pd.DataFrame(vol_dict)
        rows, missing, bench = screen_from_frames(entries, groups, adj_df, vol_df, base_str, end_str)
    except Exception as e:
        err_payload = {"ok": False, "base": base_str, "end": end_str, "core": [], "flagged": [], "warnings": [str(e)]}
        print(json.dumps(err_payload, ensure_ascii=False, indent=2))
        return 1

    # 检查美股最新一根日线是否就绪
    us_rows = [r for r in rows if "." not in r["ticker"]]
    stale = [r["ticker"] for r in us_rows if r["last_date"] and r["last_date"] < end_str]
    warnings_list: list[str] = []
    if skipped_private:
        warnings_list.append(f"已跳过非上市标的：{', '.join(skipped_private)}")
    if missing:
        warnings_list.append(f"无行情数据标的：{', '.join(missing)}")

    data_ok = True
    if us_rows and len(stale) > len(us_rows) / 2:
        data_ok = False
        warnings_list.append(
            f"{len(stale)}/{len(us_rows)} 只美股缺少 {end_str} 当日日线，最新日线尚未就绪（ok=false），请勿基于缺日数据下结论。"
        )

    core_rows = [r for r in rows if r["deep"]]
    core_rows.sort(key=lambda r: (0 if r.get("held") else 1, r["ticker"]))

    flagged_rows = [r for r in rows if not r["deep"] and r["flags"]]
    flagged_rows.sort(
        key=lambda r: (
            0 if r.get("held") else 1,
            -(
                abs(r["window_z"] or 0.0)
                + max([abs(f.get("z") or 0.0) for f in r["flags"]] + [0.0]) / 2.0
                + max([float(f.get("vol_ratio") or 0.0) for f in r["flags"]] + [0.0]) / 2.0
            ),
        )
    )

    unflagged_watch = [r for r in rows if not r["deep"] and not r["flags"]]
    unflagged_watch.sort(key=lambda r: (0 if r.get("held") else 1, r["ticker"]))

    result = {
        "ok": data_ok,
        "base": base_str,
        "end": end_str,
        "warnings": warnings_list,
        "stale_us_tickers": stale[:20],
        "bench": bench,
        "core": core_rows,
        "flagged": flagged_rows,
        "unflagged_watch": unflagged_watch,
        "rows": rows,
    }

    if args.out:
        out_p = Path(args.out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if data_ok else 1


if __name__ == "__main__":
    sys.exit(main())
