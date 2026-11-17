#!/usr/bin/env python3
"""PM Market State & Sector Rotation — L1 & L2 computation (L3-L5 omitted by design)."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from scripts.dalton.monitor_feeds.common import (
    CN_TZ,
    US_TZ,
    SourceStatusStore,
    atomic_write_json,
    format_source_statuses,
    load_json_safe,
    resolve_state_dir,
)
from scripts.dalton.monitor_feeds.pm_cockpit.collect import load_config

QUAD = {
    (1, 1): "领先",
    (1, 0): "转弱",
    (0, 0): "落后",
    (0, 1): "改善",
}


def read_prices(path: Path) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    df = pd.read_csv(path, index_col=0, parse_dates=True).sort_index()
    return df.dropna(how="all")


def pct_rank(series: pd.Series, lookback: int) -> float | None:
    s = series.dropna().iloc[-lookback:]
    if len(s) < 60:
        return None
    return float(s.rank(pct=True).iloc[-1] * 100.0)


def ret(series: pd.Series, days: int) -> float | None:
    s = series.dropna()
    if len(s) < days + 1:
        return None
    return float(s.iloc[-1] / s.iloc[-days - 1] - 1.0) * 100.0


def build_l1(px: pd.DataFrame, cfg: dict[str, Any]) -> dict[str, Any]:
    lb = int(cfg["scoring"]["zscore_lookback_days"])
    comps: list[dict[str, Any]] = []
    scored: list[float] = []
    for c in cfg["l1_us"]["components"]:
        try:
            if c["type"] == "ratio":
                if c["num"] not in px or c["den"] not in px:
                    continue
                s = (px[c["num"]] / px[c["den"]]).dropna()
                level = float(s.iloc[-1])
                disp = f"{level:.4g}"
            else:
                if c["num"] not in px:
                    continue
                p = px[c["num"]].dropna()
                s = (p / p.rolling(int(c["ma"])).mean() - 1.0) * 100.0
                s = s.dropna()
                level = float(s.iloc[-1])
                disp = f"{level:+.1f}%"
        except Exception:
            continue
        p_now = pct_rank(s, lb)
        if p_now is None:
            continue
        chg_1m = ret(s, 21)
        chg_1w = ret(s, 5)
        item = {
            "key": c["key"],
            "label": c["label"],
            "value": disp,
            "raw": level,
            "pctile": round(p_now, 1),
            "chg_1w_pct": None if chg_1w is None else round(chg_1w, 2),
            "chg_1m_pct": None if chg_1m is None else round(chg_1m, 2),
            "sign": int(c["sign"]),
            "note": c["note"],
        }
        comps.append(item)
        if c["sign"] != 0:
            scored.append(p_now if c["sign"] > 0 else 100.0 - p_now)

    score = round(float(np.mean(scored)), 1) if scored else None
    bands = cfg["scoring"]["regime_bands"]
    if score is None:
        regime, tone = "数据不足", "muted"
    elif score >= bands["offense"]:
        regime, tone = "进攻", "up"
    elif score <= bands["defense"]:
        regime, tone = "防守", "down"
    else:
        regime, tone = "均衡", "neutral"

    style: str | None = None
    gv = next((c for c in comps if c["key"] == "style_gv"), None)
    if gv and gv["chg_1m_pct"] is not None:
        style = "成长占优" if gv["chg_1m_pct"] > 0 else "价值占优"

    disp_sd = round(float(np.std(scored)), 1) if len(scored) > 2 else None
    if disp_sd is not None and disp_sd >= 28:
        conflict = "高分歧——各指标相互背离，市场状态未定型"
    elif disp_sd is not None and disp_sd >= 18:
        conflict = "中等分歧——主要矛盾尚未统一"
    else:
        conflict = "分项一致——多数指标指向相同方向"

    return {
        "score": score,
        "regime": regime,
        "tone": tone,
        "style_tilt": style,
        "components": comps,
        "n_scored": len(scored),
        "dispersion": disp_sd,
        "conflict": conflict,
    }


def build_rrg(
    px: pd.DataFrame,
    bench: str,
    members: dict[str, str],
    label: str,
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    w_win = int(cfg["rrg"]["rs_window"])
    m_win = int(cfg["rrg"]["mom_window"])
    tail = int(cfg["rrg"]["tail_len"])
    if bench not in px:
        return []
    b = px[bench]
    rows: list[dict[str, Any]] = []
    for tk, name in members.items():
        if tk not in px:
            continue
        s = px[tk].dropna()
        if len(s) < w_win + m_win + 30:
            continue
        rs = (s / b).dropna()
        rsr = 100.0 * rs / rs.rolling(w_win).mean()
        rsm = 100.0 * rsr / rsr.rolling(m_win).mean()
        d = pd.DataFrame({"rsr": rsr, "rsm": rsm}).dropna()
        if d.empty:
            continue
        x, y = float(d["rsr"].iloc[-1]), float(d["rsm"].iloc[-1])
        q = QUAD[(1 if x >= 100 else 0, 1 if y >= 100 else 0)]
        exc: dict[str, float | None] = {}
        for lbl, dd in (("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126)):
            r_s, r_b = ret(s, dd), ret(b, dd)
            exc[lbl] = None if (r_s is None or r_b is None) else round(r_s - r_b, 2)
        hi = float(s.iloc[-250:].max()) if len(s) >= 250 else float(s.max())
        r1d = ret(s, 1)
        r1w = ret(s, 5)
        rows.append(
            {
                "ticker": tk,
                "name": name,
                "group": label,
                "rsr": round(x, 2),
                "rsm": round(y, 2),
                "quadrant": q,
                "tail": [
                    [round(float(a), 2), round(float(c), 2)]
                    for a, c in zip(d["rsr"].iloc[-tail:], d["rsm"].iloc[-tail:])
                ],
                "exc": exc,
                "ret_1d": None if r1d is None else round(r1d, 2),
                "ret_1w": None if r1w is None else round(r1w, 2),
                "from_250d_high_pct": round((float(s.iloc[-1]) / hi - 1.0) * 100.0, 1),
                "last": round(float(s.iloc[-1]), 2),
            }
        )
    rows.sort(key=lambda r: -(r["exc"]["1M"] if r["exc"]["1M"] is not None else -999.0))
    for i, r in enumerate(rows, 1):
        r["rank_1m"] = i
    return rows


def rank_migration(
    px: pd.DataFrame, bench: str, members: dict[str, str], weeks: int = 8
) -> dict[str, dict[str, int]]:
    if bench not in px:
        return {}
    out: dict[str, dict[str, int]] = {}
    for w in range(weeks):
        off = w * 5
        end = len(px) - off
        if end < 90:
            continue
        sub = px.iloc[:end]
        scores: dict[str, float] = {}
        for tk in members:
            if tk not in sub:
                continue
            rs_, rb = ret(sub[tk].dropna(), 21), ret(sub[bench].dropna(), 21)
            if rs_ is None or rb is None:
                continue
            scores[tk] = rs_ - rb
        order = sorted(scores, key=lambda k: -scores[k])
        dt_str = str(sub.index[-1].date())
        out[dt_str] = {tk: i + 1 for i, tk in enumerate(order)}
    return dict(sorted(out.items()))


def _pctile_in(hist: list[Any], v: float | None) -> float | None:
    h = [float(x) for x in hist if x is not None and x == x]
    if len(h) < 20 or v is None or v != v:
        return None
    return round(sum(1 for x in h if x <= v) / len(h) * 100.0, 1)


def build_valuation(raw_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    wp = raw_dir / "cn_sw_val_weekly.csv"
    lp = raw_dir / "cn_sw_val_latest.csv"
    if not wp.is_file() or not lp.is_file():
        return {
            "available": False,
            "reason": "申万行业历史估值数据尚未就绪",
            "map": {},
        }
    hist = pd.read_csv(wp, dtype={"code": str})
    cur = pd.read_csv(lp, dtype={"code": str}).set_index("code")
    vc = cfg.get("valuation", {})
    min_obs = int(vc.get("min_obs", 104))
    cuts = vc.get("score_cuts", [20, 40, 60, 80])
    weights = vc.get("weights", {"pe": 0.4, "pb": 0.4, "dy": 0.2})

    out: dict[str, dict[str, Any]] = {}
    for code, g in hist.groupby("code"):
        if code not in cur.index:
            continue
        g = g.sort_values("date")
        row = cur.loc[code]
        n = int(len(g))
        pe, pb, dy = (
            float(row[k]) if row[k] == row[k] else None for k in ("pe", "pb", "dy")
        )
        pe_med = float(g["pe"].median()) if g["pe"].notna().any() else None
        pe_bad: str | None = None
        if pe is None or pe <= 0:
            pe_bad = "无有效市盈率（行业整体亏损或未披露）"
        elif pe_med and pe > pe_med * float(vc.get("pe_blowout_mult", 4.0)):
            pe_bad = f"市盈率 {pe:.0f} 倍达历史中位数 {pe_med:.0f} 倍的 {pe / pe_med:.1f} 倍，属盈利阶段性失真"
        elif pe > float(vc.get("pe_abs_cap", 300)):
            pe_bad = f"市盈率 {pe:.0f} 倍超过上限，不参与估值分位计算"

        parts: list[float] = []
        wts: list[float] = []
        detail: dict[str, Any] = {}
        if not pe_bad:
            p = _pctile_in(g["pe"].tolist(), pe)
            if p is not None:
                parts.append(p)
                wts.append(float(weights["pe"]))
                detail["pe_pctile"] = p
        p_pb = _pctile_in(g["pb"].tolist(), pb)
        if p_pb is not None:
            parts.append(p_pb)
            wts.append(float(weights["pb"]))
            detail["pb_pctile"] = p_pb
        p_dy = _pctile_in(g["dy"].tolist(), dy)
        if p_dy is not None:
            parts.append(100.0 - p_dy)
            wts.append(float(weights["dy"]))
            detail["dy_pctile"] = p_dy

        if not parts:
            out[str(code)] = {
                "score": None,
                "n_obs": n,
                "confidence": "无",
                "reason": pe_bad or "估值分项均不可用",
                "pe": pe,
                "pb": pb,
                "dy": dy,
            }
            continue

        v = float(np.average(parts, weights=wts))
        score = 5 - sum(1 for c in cuts if v > c)
        conf = "高" if n >= 156 else ("中" if n >= min_obs else "低")
        if len(parts) < 2:
            conf = {"高": "中", "中": "低", "低": "低"}[conf]
        out[str(code)] = {
            "score": int(score),
            "v_pctile": round(v, 1),
            "n_obs": n,
            "confidence": conf,
            "pe": pe,
            "pb": pb,
            "dy": dy,
            "pe_excluded": pe_bad,
            "window": f"{g['date'].iloc[0]}~{g['date'].iloc[-1]}",
            **detail,
        }

    scored = [v["score"] for v in out.values() if v.get("score")]
    return {
        "available": True,
        "map": out,
        "n": len(out),
        "n_scored": len(scored),
        "as_of": str(cur["date"].max()) if "date" in cur.columns else None,
        "window_start": cfg["l2_cn"].get("valuation_start"),
        "dist": {s: scored.count(s) for s in (5, 4, 3, 2, 1)},
    }


def build_cn(raw_dir: Path, val: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    hist = read_prices(raw_dir / "cn_sw_hist.csv")
    if hist is None:
        return {"available": False, "reason": "申万行业行情数据暂未采集"}
    hist.columns = [str(c) for c in hist.columns]
    meta_doc = load_json_safe(raw_dir / "cn_sw_meta.json")
    if not meta_doc:
        return {"available": False, "reason": "申万行业名录暂未采集"}
    meta = meta_doc.get("industries", {})
    bench = cfg["l2_cn"]["benchmark_label"]
    if bench not in hist.columns:
        return {"available": False, "reason": f"基准指数（{bench}）行情缺失"}
    amt = read_prices(raw_dir / "cn_sw_amount.csv")
    if amt is not None:
        amt.columns = [str(c) for c in amt.columns]

    def lvl_names(n: int) -> dict[str, str]:
        return {
            c: str(meta[c]["name"])
            for c in hist.columns
            if meta.get(c, {}).get("level") == n
        }

    rows_1 = build_rrg(hist, bench, lvl_names(1), "申万一级", cfg)
    rows_2 = build_rrg(hist, bench, lvl_names(2), "申万二级", cfg)
    for r in rows_2:
        r["group"] = meta.get(r["ticker"], {}).get("parent") or "其他"

    vmap = val.get("map", {})
    for r in rows_1 + rows_2:
        m = meta.get(r["ticker"], {})
        r["level"] = m.get("level")
        r["parent"] = m.get("parent")
        r["n_cons"] = m.get("n_cons")
        v = vmap.get(r["ticker"], {})
        for src, dst in (("pe", "pe_ttm"), ("pb", "pb"), ("dy", "div_yield")):
            x = v.get(src)
            r[dst] = round(float(x), 2) if isinstance(x, (int, float)) and x == x else None
        r["pe_hist_pctile"] = v.get("pe_pctile")
        r["pb_hist_pctile"] = v.get("pb_pctile")
        r["val_pctile"] = v.get("v_pctile")
        r["val_score"] = v.get("score")
        r["val_conf"] = v.get("confidence")
        r["pe_excluded"] = v.get("pe_excluded")
        s = hist[r["ticker"]].dropna()
        r["ret_1d"] = round(float(s.iloc[-1] / s.iloc[-2] - 1.0) * 100.0, 2) if len(s) > 1 else None
        if amt is not None and r["ticker"] in amt.columns:
            a = amt[r["ticker"]].dropna()
            r["amt_yi"] = round(float(a.iloc[-1]), 1) if len(a) else None
            r["amt_vs_20d"] = (
                round(float(a.iloc[-1] / a.iloc[-20:].mean()), 2)
                if len(a) >= 20 and a.iloc[-20:].mean()
                else None
            )

    parents = sorted({r["group"] for r in rows_2})
    return {
        "available": True,
        "benchmark": bench,
        "last_date": str(hist.index[-1].date()),
        "valuation_meta": {
            k: val.get(k)
            for k in ("available", "as_of", "window_start", "n_scored", "reason")
        },
        "rows_l1": rows_1,
        "rows_l2": rows_2,
        "parents": parents,
        "rrg_max_points": int(cfg["l2_cn"].get("rrg_max_points", 42)),
        "migration_l1": rank_migration(hist, bench, lvl_names(1)),
        "names_l1": lvl_names(1),
    }


def compute_panels(
    *,
    state_dir: str | Path | None = None,
    cfg_path: Path | None = None,
    sources_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = load_config(cfg_path)
    st_dir = resolve_state_dir("pm_cockpit", state_dir)
    raw_dir = st_dir / "raw"
    hist_dir = st_dir / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)

    px = read_prices(raw_dir / "us_prices.csv")
    meta = load_json_safe(raw_dir / "collect_meta.json", {}) or {}
    status_doc = SourceStatusStore(st_dir).load()
    sources = dict(status_doc.get("sources") or {})
    if isinstance(meta.get("sources"), dict):
        sources.update(meta["sources"])
    if isinstance(sources_override, dict):
        sources.update(sources_override)
    us_meta = sources.get("us") or meta.get("us") or {}
    cn_meta = sources.get("cn") or meta.get("cn") or {}
    val_meta = sources.get("cn_valuation") or meta.get("cn_valuation") or {}

    bench = cfg["l2_us"]["benchmark"]
    l2_rows: list[dict[str, Any]] = []
    if px is not None:
        for label, members in cfg["l2_us"]["groups"].items():
            l2_rows += build_rrg(px, bench, members, label, cfg)

    all_members = {k: v for g in cfg["l2_us"]["groups"].values() for k, v in g.items()}
    val = build_valuation(raw_dir, cfg)
    cn = build_cn(raw_dir, val, cfg)

    us_as_of = str(px.index[-1].date()) if px is not None and len(px) else None
    us_stale = (px is None) or (not us_meta.get("ok", True)) or bool(us_meta.get("stale"))
    cn_stale = (not cn.get("available")) or (not cn_meta.get("ok", True)) or bool(cn_meta.get("stale"))
    val_stale = (not val.get("available")) or (not val_meta.get("ok", True)) or bool(val_meta.get("stale"))
    us_reason = format_source_statuses(sources, only=("us",)) or us_meta.get("reason") or us_meta.get("error") or ("缺少美股行情数据" if px is None else None)
    cn_reason = format_source_statuses(sources, only=("cn", "cn_valuation")) or "；".join(
        x for x in (cn_meta.get("reason") or cn_meta.get("error"), val_meta.get("reason") or val_meta.get("error"), cn.get("reason") if not cn.get("available") else None) if x
    ) or None

    panels = {
        "generated_at_us": datetime.now(US_TZ).strftime("%Y-%m-%d %H:%M:%S") + " 美东时间",
        "generated_at_cn": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间",
        "us_data_as_of": us_as_of,
        "sources": sources,
        "us_status": {
            "stale": us_stale,
            "reason": us_reason,
            "last_success_at": us_meta.get("last_success_at") or us_as_of,
        },
        "cn_status": {
            "stale": cn_stale or val_stale,
            "reason": cn_reason,
            "last_success_at": cn_meta.get("last_success_at") or cn.get("last_date"),
        },
        "l1": build_l1(px, cfg) if px is not None else {"score": None, "regime": "数据不足", "tone": "muted", "components": [], "n_scored": 0, "dispersion": None, "conflict": "暂无美股数据"},
        "l2_us": {
            "benchmark": bench,
            "rows": l2_rows,
            "migration": rank_migration(px, bench, all_members) if px is not None else {},
            "names": all_members,
        },
        "l2_cn": cn,
    }

    atomic_write_json(st_dir / "panels.json", panels, indent=1)
    if us_as_of:
        atomic_write_json(hist_dir / f"{us_as_of}.json", panels)
    return panels


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute PM L1/L2 panels.json.")
    parser.add_argument("--state-dir", default=None)
    args = parser.parse_args()
    panels = compute_panels(state_dir=args.state_dir)
    print(
        json.dumps(
            {
                "us_data_as_of": panels["us_data_as_of"],
                "l1_score": panels["l1"]["score"],
                "l1_regime": panels["l1"]["regime"],
                "us_sectors": len(panels["l2_us"]["rows"]),
                "cn_available": panels["l2_cn"].get("available", False),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
