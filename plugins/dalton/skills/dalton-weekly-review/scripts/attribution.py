#!/usr/bin/env python3
"""
attribution.py — EPS 预期修正 vs 估值倍数归因脚本（PS.13 新设计）

核心公式（PS.13 lines 1612–1618）：
1. 对数分解：
     ln(P_end / P_base) = ln(EPS_end / EPS_base) + ln(PE_end / PE_base)
   其中 EPS 为远期 12 个月一致预期（NTM EPS），PE = P / NTM_EPS。
2. NTM EPS 时间加权插值：
     NTM_EPS = w * EPS_0y + (1 - w) * EPS_+1y
     w = 本财年剩余天数 / 365
   窗口起点的 EPS 优先取工作区 data/consensus/ 快照历史里离 base 最近的一份；
   没有快照时用一致预期接口的 7daysAgo 值，并标注“近似（7daysAgo）”。
3. 主导驱动判定（driver）：
   - 两项贡献同号时，绝对值大的为主导：占比 >= 70% 写“盈利驱动”或“估值驱动”，否则写“两者”；
   - 两项贡献异号时，写“相抵”（详情“盈利 Xpp、倍数 Ypp 相抵”）。
4. 负 EPS 或 EPS 接近 0（<= 0.05）的公司不做分解，写“NM，改用 EV/Sales 定性”。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

EPS_NEAR_ZERO_THRESHOLD = 0.05
DOMINANCE_SHARE_THRESHOLD = 0.70


def remaining_fiscal_year_weight(ref_date: date, fiscal_year_end_mmdd: str = "12-31") -> float:
    """
    计算参考日期 ref_date 距离本财年结束日的剩余天数占 365 天的权重 w = 本财年剩余天数 / 365。
    """
    parts = [int(x) for x in fiscal_year_end_mmdd.strip().split("-")]
    m, d = parts[0], parts[1]
    try:
        fye = date(ref_date.year, m, d)
    except ValueError:
        fye = date(ref_date.year, m, 28)
    if fye < ref_date:
        try:
            fye = date(ref_date.year + 1, m, d)
        except ValueError:
            fye = date(ref_date.year + 1, m, 28)
    rem_days = (fye - ref_date).days
    w = rem_days / 365.0
    return max(0.0, min(1.0, w))


def compute_ntm_eps(
    eps_0y: float | None,
    eps_1y: float | None,
    ref_date: date | str,
    fiscal_year_end: str = "12-31",
) -> tuple[float | None, float]:
    """
    按 w * EPS_0y + (1 - w) * EPS_+1y 插值计算 NTM EPS。
    返回 (ntm_eps, w)。
    """
    d = date.fromisoformat(ref_date[:10]) if isinstance(ref_date, str) else ref_date
    w = remaining_fiscal_year_weight(d, fiscal_year_end)
    if eps_0y is None and eps_1y is None:
        return None, round(w, 4)
    if eps_0y is not None and eps_1y is not None:
        return float(w * float(eps_0y) + (1.0 - w) * float(eps_1y)), round(w, 4)
    # 若仅有一个财年有值则退化使用该值
    val = float(eps_0y) if eps_0y is not None else float(eps_1y)  # type: ignore[arg-type]
    return val, round(w, 4)


def classify_driver(eps_contrib_pp: float, pe_contrib_pp: float) -> tuple[str, str]:
    """
    主导驱动判定（PS.13 line 1616）：
    - 两项贡献同号时，绝对值大的为主导，占比 >= 70% 写“盈利驱动”或“估值驱动”，否则写“两者”；
    - 异号时写“相抵”（附“盈利 X、倍数 Y 相抵”）。
    返回 (driver, driver_display)
    """
    abs_e = abs(eps_contrib_pp)
    abs_m = abs(pe_contrib_pp)
    total_abs = abs_e + abs_m
    if total_abs < 1e-9:
        return "两者", "无明显变动"

    if eps_contrib_pp * pe_contrib_pp >= -1e-9:
        e_share = abs_e / total_abs
        m_share = abs_m / total_abs
        if e_share >= DOMINANCE_SHARE_THRESHOLD:
            return "盈利驱动", "盈利驱动"
        if m_share >= DOMINANCE_SHARE_THRESHOLD:
            return "估值驱动", "估值驱动"
        return "两者", "两者"
    else:
        detail = f"盈利 {eps_contrib_pp:+.1f}pp、倍数 {pe_contrib_pp:+.1f}pp 相抵"
        return "相抵", detail


def decompose_price_move(
    ticker: str,
    p_base: float | None,
    p_end: float | None,
    eps_0y_base: float | None,
    eps_1y_base: float | None,
    eps_0y_end: float | None,
    eps_1y_end: float | None,
    base_date: str,
    end_date: str,
    fiscal_year_end: str = "12-31",
    base_eps_source: str = "snapshot",
    name: str | None = None,
) -> dict[str, Any]:
    """
    对单只股票执行 NTM EPS 插值与 ln(P_end/P_base) = ln(EPS_end/EPS_base) + ln(PE_end/PE_base) 归因分解。
    """
    ntm_base, w_base = compute_ntm_eps(eps_0y_base, eps_1y_base, base_date, fiscal_year_end)
    ntm_end, w_end = compute_ntm_eps(eps_0y_end, eps_1y_end, end_date, fiscal_year_end)

    if p_base is None or p_end is None or p_base <= 0 or p_end <= 0:
        return {
            "ticker": ticker,
            "name": name or ticker,
            "status": "missing_price",
            "driver": "数据缺失",
            "driver_display": "价格数据缺失",
        }

    simple_ret_pct = (p_end / p_base - 1.0) * 100.0
    log_ret_pp = math.log(p_end / p_base) * 100.0

    # 负 EPS 或 EPS 接近 0 的公司不做分解，写“NM，改用 EV/Sales 定性”
    if (
        ntm_base is None
        or ntm_end is None
        or ntm_base <= EPS_NEAR_ZERO_THRESHOLD
        or ntm_end <= EPS_NEAR_ZERO_THRESHOLD
    ):
        return {
            "ticker": ticker,
            "name": name or ticker,
            "status": "NM",
            "p_base": round(p_base, 2),
            "p_end": round(p_end, 2),
            "price_change_pct": round(simple_ret_pct, 2),
            "log_return_pp": round(log_ret_pp, 2),
            "ntm_eps_base": round(ntm_base, 3) if ntm_base is not None else None,
            "ntm_eps_end": round(ntm_end, 3) if ntm_end is not None else None,
            "w_base": w_base,
            "w_end": w_end,
            "pe_base": None,
            "pe_end": None,
            "eps_contrib_pp": None,
            "pe_contrib_pp": None,
            "closure_error_pp": 0.0,
            "driver": "NM，改用 EV/Sales 定性",
            "driver_display": "NM，改用 EV/Sales 定性",
            "base_eps_source": base_eps_source,
            "eps_revision_source": "暂缺",
            "multiple_explanation": "NM，改用 EV/Sales 定性",
        }

    pe_base = p_base / ntm_base
    pe_end = p_end / ntm_end

    eps_contrib_pp = math.log(ntm_end / ntm_base) * 100.0
    pe_contrib_pp = math.log(pe_end / pe_base) * 100.0
    closure_err = abs((eps_contrib_pp + pe_contrib_pp) - log_ret_pp)

    driver_code, driver_display = classify_driver(eps_contrib_pp, pe_contrib_pp)

    return {
        "ticker": ticker,
        "name": name or ticker,
        "status": "ok",
        "p_base": round(p_base, 2),
        "p_end": round(p_end, 2),
        "price_change_pct": round(simple_ret_pct, 2),
        "log_return_pp": round(log_ret_pp, 2),
        "ntm_eps_base": round(ntm_base, 3),
        "ntm_eps_end": round(ntm_end, 3),
        "w_base": w_base,
        "w_end": w_end,
        "pe_base": round(pe_base, 2),
        "pe_end": round(pe_end, 2),
        "eps_contrib_pp": round(eps_contrib_pp, 2),
        "pe_contrib_pp": round(pe_contrib_pp, 2),
        "closure_error_pp": round(closure_err, 6),
        "driver": driver_code,
        "driver_display": driver_display,
        "base_eps_source": base_eps_source,
        "eps_revision_source": "待子智能体从卖方研报补充（无则写暂缺）",
        "multiple_explanation": "原因暂缺",
    }


def extract_eps_from_snapshot(snap: dict[str, Any]) -> tuple[float | None, float | None]:
    ee = snap.get("earnings_estimate") if isinstance(snap.get("earnings_estimate"), dict) else {}
    e0 = (ee.get("0y") or {}).get("avg") if isinstance(ee.get("0y"), dict) else None
    e1 = (ee.get("+1y") or {}).get("avg") if isinstance(ee.get("+1y"), dict) else None
    if e0 is None or e1 is None:
        et = snap.get("eps_trend") if isinstance(snap.get("eps_trend"), dict) else {}
        if e0 is None and isinstance(et.get("0y"), dict):
            e0 = et["0y"].get("current")
        if e1 is None and isinstance(et.get("+1y"), dict):
            e1 = et["+1y"].get("current")
    return (float(e0) if e0 is not None else None), (float(e1) if e1 is not None else None)


def extract_7d_ago_eps(snap: dict[str, Any]) -> tuple[float | None, float | None]:
    et = snap.get("eps_trend") if isinstance(snap.get("eps_trend"), dict) else {}
    e0 = (et.get("0y") or {}).get("7daysAgo") if isinstance(et.get("0y"), dict) else None
    e1 = (et.get("+1y") or {}).get("7daysAgo") if isinstance(et.get("+1y"), dict) else None
    return (float(e0) if e0 is not None else None), (float(e1) if e1 is not None else None)


def load_snapshots_for_window(
    ticker: str,
    workspace_dir: Path | None,
    base_date: str,
    end_date: str,
) -> tuple[float | None, float | None, float | None, float | None, str]:
    """
    优先从公司工作区 data/consensus/*.json 读取离 base 最近的快照和离 end 最新的快照；
    若无历史快照，回退调用 yfinance 获取 current 与 7daysAgo（标注“近似”）。
    """
    if workspace_dir:
        cdir = workspace_dir / "data" / "consensus"
        if cdir.exists():
            files = sorted(cdir.glob("*.json"))
            snaps: list[tuple[str, dict[str, Any]]] = []
            for f in files:
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(d, dict):
                        ts_str = str(d.get("fetched_at") or f.stem)[:10]
                        snaps.append((ts_str, d))
                except Exception:
                    continue
            if len(snaps) >= 2:
                # 找离 base_date 最近的一份快照
                base_dt = date.fromisoformat(base_date[:10])
                base_snap = min(snaps, key=lambda item: abs((date.fromisoformat(item[0][:10]) - base_dt).days))[1]
                end_snap = snaps[-1][1]
                e0_b, e1_b = extract_eps_from_snapshot(base_snap)
                e0_e, e1_e = extract_eps_from_snapshot(end_snap)
                if (e0_b is not None or e1_b is not None) and (e0_e is not None or e1_e is not None):
                    return e0_b, e1_b, e0_e, e1_e, "snapshot"
            elif len(snaps) == 1:
                only_snap = snaps[0][1]
                e0_e, e1_e = extract_eps_from_snapshot(only_snap)
                e0_b, e1_b = extract_7d_ago_eps(only_snap)
                if (e0_b is not None or e1_b is not None) and (e0_e is not None or e1_e is not None):
                    return e0_b, e1_b, e0_e, e1_e, "近似（7daysAgo）"

    # 回退：直接通过 yfinance 读取 eps_trend 的 current 与 7daysAgo
    try:
        import yfinance as yf  # type: ignore
        t = yf.Ticker(ticker)
        et_df = getattr(t, "eps_trend", None)
        if et_df is not None and not getattr(et_df, "empty", True):
            rec = et_df.to_dict(orient="index")
            r0 = rec.get("0y") or {}
            r1 = rec.get("+1y") or {}
            e0_e, e1_e = r0.get("current"), r1.get("current")
            e0_b, e1_b = r0.get("7daysAgo"), r1.get("7daysAgo")
            return (
                float(e0_b) if e0_b is not None and not math.isnan(float(e0_b)) else None,
                float(e1_b) if e1_b is not None and not math.isnan(float(e1_b)) else None,
                float(e0_e) if e0_e is not None and not math.isnan(float(e0_e)) else None,
                float(e1_e) if e1_e is not None and not math.isnan(float(e1_e)) else None,
                "近似（7daysAgo）",
            )
    except Exception:
        pass
    return None, None, None, None, "missing"


def render_markdown_table(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| 公司 | 股价变化（对数变化） | EPS 预期贡献（pp） | 倍数贡献（pp） | 主导驱动 | EPS 口径来源 | 盈利预测变化来源 | 倍数变化解释 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        t_label = f"{r.get('name', r['ticker'])} ({r['ticker']})"
        if r.get("status") == "NM":
            p_str = f"{r.get('price_change_pct', 0):+.2f}% ({r.get('log_return_pp', 0):+.2f}pp)"
            lines.append(f"| {t_label} | {p_str} | NM | NM | {r['driver_display']} | {r.get('base_eps_source', '')} | 暂缺 | NM，改用 EV/Sales 定性 |")
        elif r.get("status") == "ok":
            p_str = f"{r['price_change_pct']:+.2f}% ({r['log_return_pp']:+.2f}pp)"
            e_str = f"{r['eps_contrib_pp']:+.2f}pp"
            m_str = f"{r['pe_contrib_pp']:+.2f}pp"
            lines.append(
                f"| {t_label} | {p_str} | {e_str} | {m_str} | {r['driver_display']} | "
                f"{r.get('base_eps_source', '')} | {r.get('eps_revision_source', '暂缺')} | {r.get('multiple_explanation', '原因暂缺')} |"
            )
        else:
            lines.append(f"| {t_label} | 数据缺失 | - | - | 数据缺失 | - | - | - |")
    return "\n".join(lines)


def run_self_test() -> dict[str, Any]:
    """
    自检：验证 NTM EPS 加权插值、对数分解闭合性（误差 < 0.1pp）、四类主导判定（盈利驱动 / 估值驱动 / 两者 / 相抵）及负 EPS 的 NM 处理。
    """
    # 1. 盈利驱动样例：股价从 100 涨到 110 (+9.531% log)，EPS 从 10 涨到 10.9 (+8.618% log)，倍数微扩 (+0.913% log)
    r_earn = decompose_price_move(
        ticker="META",
        p_base=100.0,
        p_end=110.0,
        eps_0y_base=10.0,
        eps_1y_base=10.0,
        eps_0y_end=10.9,
        eps_1y_end=10.9,
        base_date="2026-10-01",
        end_date="2026-10-08",
    )

    # 2. 估值驱动样例：股价从 100 涨到 110，EPS 从 10 涨到 10.1，主要由 PE 倍数驱动
    r_mult = decompose_price_move(
        ticker="AMZN",
        p_base=100.0,
        p_end=110.0,
        eps_0y_base=10.0,
        eps_1y_base=10.0,
        eps_0y_end=10.1,
        eps_1y_end=10.1,
        base_date="2026-10-01",
        end_date="2026-10-08",
    )

    # 3. 两者共同驱动样例：EPS +4% log，PE +4% log
    r_both = decompose_price_move(
        ticker="GOOGL",
        p_base=100.0,
        p_end=108.3287,
        eps_0y_base=10.0,
        eps_1y_base=10.0,
        eps_0y_end=10.4081,
        eps_1y_end=10.4081,
        base_date="2026-10-01",
        end_date="2026-10-08",
    )

    # 4. 相抵样例：EPS 上修 +5%，但股价跌 -3%（倍数收缩约 -8%）
    r_offset = decompose_price_move(
        ticker="MSFT",
        p_base=100.0,
        p_end=97.0,
        eps_0y_base=10.0,
        eps_1y_base=10.0,
        eps_0y_end=10.5,
        eps_1y_end=10.5,
        base_date="2026-10-01",
        end_date="2026-10-08",
    )

    # 5. 负 EPS / 近零 EPS -> NM
    r_nm = decompose_price_move(
        ticker="RIVN",
        p_base=15.0,
        p_end=14.0,
        eps_0y_base=-2.5,
        eps_1y_base=-1.8,
        eps_0y_end=-2.4,
        eps_1y_end=-1.7,
        base_date="2026-10-01",
        end_date="2026-10-08",
    )

    # 6. NTM EPS 跨期插值检验：年中 2026-07-02（距 12-31 剩 182 天，w = 182/365 ≈ 0.4986）
    ntm_val, w_val = compute_ntm_eps(10.0, 20.0, "2026-07-02", "12-31")
    expected_ntm = (182 / 365.0) * 10.0 + (1.0 - 182 / 365.0) * 20.0

    checks = {
        "earnings_driven": r_earn["driver"] == "盈利驱动",
        "multiple_driven": r_mult["driver"] == "估值驱动",
        "both_driven": r_both["driver"] == "两者",
        "offset_driven": r_offset["driver"] == "相抵" and "相抵" in r_offset["driver_display"],
        "nm_negative_eps": r_nm["status"] == "NM" and r_nm["driver"] == "NM，改用 EV/Sales 定性",
        "math_closure_under_0_1pp": all(
            abs((r["eps_contrib_pp"] + r["pe_contrib_pp"]) - r["log_return_pp"]) < 0.1
            for r in (r_earn, r_mult, r_both, r_offset)
        ),
        "ntm_interpolation_accurate": ntm_val is not None and abs(ntm_val - expected_ntm) < 1e-6,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "samples": [r_earn, r_mult, r_both, r_offset, r_nm],
        "markdown_preview": render_markdown_table([r_earn, r_mult, r_both, r_offset, r_nm]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Dalton EPS 预期修正 vs 估值倍数归因工具 (PS.13)")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "single", "self-test"])
    ap.add_argument("--screen", help="screen_movers.py 输出的 screen.json 路径")
    ap.add_argument("--ticker", help="单股计算模式下的股票代码")
    ap.add_argument("--p-base", type=float, help="单股模式：窗口起点收盘价")
    ap.add_argument("--p-end", type=float, help="单股模式：窗口终点收盘价")
    ap.add_argument("--eps0-base", type=float, help="单股模式：起点本财年(0y) EPS")
    ap.add_argument("--eps1-base", type=float, help="单股模式：起点下一财年(+1y) EPS")
    ap.add_argument("--eps0-end", type=float, help="单股模式：终点本财年(0y) EPS")
    ap.add_argument("--eps1-end", type=float, help="单股模式：终点下一财年(+1y) EPS")
    ap.add_argument("--base", default="2026-10-01", help="窗口起点日期 YYYY-MM-DD")
    ap.add_argument("--end", default="2026-10-08", help="窗口终点日期 YYYY-MM-DD")
    ap.add_argument("--fye", default="12-31", help="财年结束月日 MM-DD（默认 12-31）")
    ap.add_argument("--out", help="将归因 JSON 写入指定文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = ap.parse_args()

    if args.command == "self-test":
        res = run_self_test()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    if args.command == "single" or (args.ticker and args.p_base is not None and args.p_end is not None):
        row = decompose_price_move(
            ticker=args.ticker or "SAMPLE",
            p_base=args.p_base,
            p_end=args.p_end,
            eps_0y_base=args.eps0_base,
            eps_1y_base=args.eps1_base,
            eps_0y_end=args.eps0_end,
            eps_1y_end=args.eps1_end,
            base_date=args.base,
            end_date=args.end,
            fiscal_year_end=args.fye,
            base_eps_source="manual",
        )
        out_obj = {"ok": True, "rows": [row], "markdown_table": render_markdown_table([row])}
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(out_obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(out_obj, ensure_ascii=False, indent=2))
        return 0

    if not args.screen:
        ap.print_help()
        return 0

    scr = json.loads(Path(args.screen).read_text(encoding="utf-8"))
    base_str = scr.get("base") or args.base
    end_str = scr.get("end") or args.end
    targets = list(scr.get("core") or []) + list(scr.get("flagged") or [])

    import pandas as pd
    import yfinance as yf  # type: ignore

    tickers = sorted({r["ticker"] for r in targets if r.get("ticker")})
    raw_prices = yf.download(
        tickers,
        start=(date.fromisoformat(base_str) - pd.Timedelta(days=10)).isoformat(),
        end=(date.fromisoformat(end_str) + pd.Timedelta(days=2)).isoformat(),
        auto_adjust=False,
        group_by="ticker",
        progress=False,
        threads=True,
    )

    base_ts, end_ts = pd.Timestamp(base_str), pd.Timestamp(end_str)
    rows_out: list[dict[str, Any]] = []
    for item in targets:
        t = item["ticker"]
        ws_dir = Path(item["workspace_dir"]) if item.get("workspace_dir") else None
        p_base = p_end = None
        try:
            s = raw_prices[t]["Adj Close"].dropna() if isinstance(raw_prices.columns, pd.MultiIndex) else raw_prices["Adj Close"].dropna()
            sb = s[s.index <= base_ts]
            se = s[s.index <= end_ts]
            if len(sb) and len(se):
                p_base, p_end = float(sb.iloc[-1]), float(se.iloc[-1])
        except Exception:
            pass

        e0_b, e1_b, e0_e, e1_e, src_label = load_snapshots_for_window(t, ws_dir, base_str, end_str)
        row = decompose_price_move(
            ticker=t,
            p_base=p_base,
            p_end=p_end,
            eps_0y_base=e0_b,
            eps_1y_base=e1_b,
            eps_0y_end=e0_e,
            eps_1y_end=e1_e,
            base_date=base_str,
            end_date=end_str,
            base_eps_source=src_label,
            name=item.get("name"),
        )
        rows_out.append(row)

    payload = {
        "ok": True,
        "base": base_str,
        "end": end_str,
        "count": len(rows_out),
        "rows": rows_out,
        "markdown_table": render_markdown_table(rows_out),
    }
    if args.out:
        out_p = Path(args.out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
