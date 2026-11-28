#!/usr/bin/env python3
"""美股一致预期快照（Yahoo，经 yfinance）：分期 EPS/收入、7/30/60/90 天修正轨迹、上下调家数、
目标价分布、财报日与最近几次财报后次日涨跌，并用一致 EPS 反推隐含税前利润和总费用空间。

只反映读取时的当前值，不提供历史 as-of；要看修正轨迹就定期存快照比较（scripts/stock_tracker.py）。

用法:
  bash scripts/consensus_estimates.sh --ticker META [--shares-bn 2.56] [--tax 0.16] [--md OUT.md]
  python3 scripts/data_gateway.py fetch estimates --ticker META

口径提醒：
- 一致 EPS 里可能同时有计入和不计入一次性项目（诉讼和解、资产减值）的分析师；
  当期 low-high 区间异常宽、或 90 天趋势陡降时，先拆口径再判断修正方向。
- 隐含总费用 = 一致收入 − EPS×股数/(1−税率) + 其他收入，股数和税率是假设，输出里写明来源。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def clean(x):
    """DataFrame/numpy 值转成可 JSON 的普通值，NaN 变 None。"""
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if hasattr(x, "item"):
        try:
            x = x.item()
        except Exception:
            pass
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    return x


def df_records(df) -> dict:
    if df is None or getattr(df, "empty", True):
        return {}
    return clean(df.to_dict(orient="index"))


def fetch_consensus(ticker: str) -> dict:
    import yfinance as yf

    # 被 stock_tracker 等旧环境 import 时，也使用包含币种字段的受管实现。
    if tuple(int(n) for n in yf.__version__.split(".")[:3]) < (1, 2, 2):
        import subprocess
        from pathlib import Path
        proc = subprocess.run(["bash", str(Path(__file__).with_suffix(".sh")),
                               "--ticker", ticker, "--consensus-only"],
                              capture_output=True, text=True, timeout=120)
        try:
            result = json.loads(proc.stdout)
        except ValueError as exc:
            raise RuntimeError("受管一致预期环境未返回 JSON") from exc
        return result

    t = yf.Ticker(ticker)
    out: dict = {"ticker": ticker, "fetched_at": datetime.now(ET).isoformat(), "source": "Yahoo Finance via yfinance",
                 "yfinance_version": yf.__version__}
    for name in ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions"):
        try:
            out[name] = df_records(getattr(t, name))
        except Exception as e:  # yfinance 字段时有时无，单项失败不影响其他项
            out[name] = {"error": str(e)[:200]}
    try:
        out["price_targets"] = clean(dict(t.analyst_price_targets or {}))
    except Exception as e:
        out["price_targets"] = {"error": str(e)[:200]}
    try:
        rec = t.recommendations
        out["recommendations"] = clean(rec.to_dict(orient="records")) if rec is not None else []
    except Exception as e:
        out["recommendations"] = {"error": str(e)[:200]}
    try:
        ed = t.get_earnings_dates(limit=8)
        out["earnings_dates"] = [{"date": idx.isoformat(), "eps_est": clean(r.get("EPS Estimate")),
                                  "eps_reported": clean(r.get("Reported EPS")), "surprise_pct": clean(r.get("Surprise(%)"))}
                                 for idx, r in ed.iterrows()]
    except Exception as e:
        out["earnings_dates"] = {"error": str(e)[:200]}
    try:
        h = yf.download([ticker, "QQQ"], period="3mo", auto_adjust=False, progress=False)["Close"].dropna()
        last, prev = h.iloc[-1], h.iloc[-2]
        five = h.iloc[-6] if len(h) >= 6 else h.iloc[0]
        out["price"] = {
            "as_of": h.index[-1].date().isoformat(),
            "close": round(float(last[ticker]), 2),
            "chg_1d_pct": round((last[ticker] / prev[ticker] - 1) * 100, 2),
            "chg_5d_pct": round((last[ticker] / five[ticker] - 1) * 100, 2),
            "qqq_chg_1d_pct": round((last["QQQ"] / prev["QQQ"] - 1) * 100, 2),
            "qqq_chg_5d_pct": round((last["QQQ"] / five["QQQ"] - 1) * 100, 2),
            "high_3m": round(float(h[ticker].max()), 2),
            "drawdown_from_3m_high_pct": round((last[ticker] / h[ticker].max() - 1) * 100, 2),
        }
    except Exception as e:
        out["price"] = {"error": str(e)[:200]}
    return out


def default_shares_tax(ticker: str) -> tuple[float | None, float | None, str]:
    """股数取 Yahoo 最近一季稀释股数，税率取最近一季 税费/税前利润；任一取不到返回 None。

    税率用季度而不用财年：财年里常有一次性税项（例如 Meta FY2025 有效税率 29.6%，同期指引 13% 至 16%），
    会把隐含费用空间算偏几百亿。季度和财年税率差 5 个百分点以上时在说明里提示改用公司指引。
    """
    import yfinance as yf

    t = yf.Ticker(ticker)
    shares = tax = None
    notes = []
    try:
        q = t.quarterly_income_stmt
        row = q.loc["Diluted Average Shares"].dropna()
        shares = round(float(row.iloc[0]) / 1e9, 3)
        notes.append(f"股数取 Yahoo {row.index[0].date()} 季度稀释平均股数")
        tp, pt = q.loc["Tax Provision"].dropna(), q.loc["Pretax Income"].dropna()
        tax = round(float(tp.iloc[0]) / float(pt.iloc[0]), 3)
        notes.append(f"税率取 Yahoo {tp.index[0].date()} 季度有效税率")
        a = t.income_stmt
        fy = float(a.loc["Tax Provision"].dropna().iloc[0]) / float(a.loc["Pretax Income"].dropna().iloc[0])
        if abs(fy - tax) >= 0.05:
            notes.append(f"最近财年有效税率 {fy:.1%} 与季度差距大，可能有一次性税项，建议按公司税率指引传 --tax")
    except Exception:
        pass
    return shares, tax, "；".join(notes)


def implied_model(cons: dict, shares: float, tax: float, other_income_bn: dict | None = None, source: str = "") -> dict:
    """用一致预期 EPS 反推税前利润和总费用空间，并给 EPS 敏感度。单位十亿美元。"""
    other = other_income_bn or {}
    out = {"assumptions": {"diluted_shares_bn": shares, "tax_rate": tax, "other_income_bn": other, "source": source,
                           "note": "隐含总费用 = 一致收入 − EPS×股数/(1−税率) + 其他收入；单位十亿美元"}}
    if not shares or shares <= 0 or not 0 <= tax < 1:
        return {"error": "股数须为正数，税率须满足 0≤税率<1；不自动使用异常有效税率"}
    ee, re_ = cons.get("earnings_estimate", {}), cons.get("revenue_estimate", {})
    for period, label in (("0y", "本财年"), ("+1y", "下一财年")):
        eps = (ee.get(period) or {}).get("avg")
        rev = (re_.get(period) or {}).get("avg")
        if eps is None or rev is None or rev == 0:
            continue
        currencies = [(ee.get(period) or {}).get("currency"), (re_.get(period) or {}).get("currency")]
        if currencies != ["USD", "USD"]:
            out.setdefault("warnings", []).append(f"{period} EPS/收入币种为 {currencies}，未确认同为 USD，跳过美元隐含模型")
            continue
        rev_bn = rev / 1e9
        pretax = eps * shares / (1 - tax)
        oth = other.get(period, 0) or 0
        out[period] = {
            "label": label,
            "consensus_revenue_bn": round(rev_bn, 1),
            "consensus_eps": round(eps, 2),
            "implied_pretax_bn": round(pretax, 1),
            "implied_total_expenses_bn": round(rev_bn - pretax + oth, 1),
            "implied_op_margin_pct": round((pretax - oth) / rev_bn * 100, 1),
            "eps_per_10bn_expense": round(-10 * (1 - tax) / shares, 2),
            "eps_per_1pct_revenue_no_cost": round(rev_bn * 0.01 * (1 - tax) / shares, 2),
        }
    return out


def post_earnings_moves(ticker: str, rows: list, n: int = 4) -> list:
    """最近 n 次财报后第一个交易日的涨跌（收盘对收盘，未复权 Close）。盘后发布算下一交易日。"""
    import yfinance as yf

    past = [r for r in rows if r.get("eps_reported") is not None][:n]
    if not past:
        return []
    h = yf.download(ticker, period="2y", auto_adjust=False, progress=False)["Close"].dropna()
    if hasattr(h, "columns"):
        h = h[ticker]
    out = []
    for r in past:
        d = datetime.fromisoformat(r["date"])
        day, after_close = d.date(), d.hour >= 16
        before = h[h.index.date <= day] if after_close else h[h.index.date < day]
        after = h[h.index.date > day] if after_close else h[h.index.date >= day]
        if len(before) and len(after):
            out.append({"earnings": day.isoformat(), "next_day_pct": round((after.iloc[0] / before.iloc[-1] - 1) * 100, 2)})
    return out


def snapshot(ticker: str, shares: float | None = None, tax: float | None = None, other_income_bn: dict | None = None) -> dict:
    cons = fetch_consensus(ticker)
    given_shares, given_tax = shares is not None, tax is not None
    notes = []
    if not (given_shares and given_tax):
        d_shares, d_tax, note = default_shares_tax(ticker)
        for part in filter(None, note.split("；")):
            is_share_note = part.startswith("股数")
            if (is_share_note and not given_shares) or (not is_share_note and not given_tax):
                notes.append(part)
        shares = shares if given_shares else d_shares
        tax = tax if given_tax else d_tax
    notes += [n for n, given in (("股数按命令行传入", given_shares), ("税率按命令行传入", given_tax)) if given]
    src = "；".join(notes)
    if shares and tax is not None:
        cons["implied_model"] = implied_model(cons, shares, tax, other_income_bn, src)
    else:
        cons["implied_model"] = {"error": "股数或税率取不到，传 --shares-bn / --tax"}
    rows = cons.get("earnings_dates") if isinstance(cons.get("earnings_dates"), list) else []
    try:
        cons["post_earnings_moves"] = post_earnings_moves(ticker, rows)
    except Exception as e:
        cons["post_earnings_moves"] = [{"error": str(e)[:200]}]
    return cons


def render_md(c: dict) -> str:
    L = [f"# {c['ticker']} 一致预期快照（{c['source']}，读取于 {c['fetched_at'][:16]} ET）", ""]
    p = c.get("price") or {}
    if "close" in p:
        L.append(f"- 收盘 {p['as_of']}：{p['close']}，1 日 {p['chg_1d_pct']}%（QQQ {p['qqq_chg_1d_pct']}%），"
                 f"5 日 {p['chg_5d_pct']}%（QQQ {p['qqq_chg_5d_pct']}%），距 3 个月高点 {p['drawdown_from_3m_high_pct']}%")
    for per in ("0q", "+1q", "0y", "+1y"):
        e = (c.get("earnings_estimate") or {}).get(per) or {}
        r = (c.get("revenue_estimate") or {}).get(per) or {}
        t = (c.get("eps_trend") or {}).get(per) or {}
        rv = (c.get("eps_revisions") or {}).get(per) or {}
        if not e:
            continue
        f2 = lambda v: None if v is None else round(v, 2)  # noqa: E731
        L.append(f"- {per}：EPS {f2(e.get('avg'))}（{e.get('low')} 至 {e.get('high')}，{e.get('numberOfAnalysts')} 家），"
                 f"收入 {r.get('avg') and round(r['avg'] / 1e9, 1)} 十亿 {r.get('currency') or '币种未提供'}；EPS 币种 {e.get('currency') or '未提供'}；EPS 7/30/90 天前 {f2(t.get('7daysAgo'))}/"
                 f"{f2(t.get('30daysAgo'))}/{f2(t.get('90daysAgo'))}；近 7 天上调 {rv.get('upLast7days')} 下调 {rv.get('downLast7Days')}，"
                 f"近 30 天上调 {rv.get('upLast30days')} 下调 {rv.get('downLast30days')}")
    pt = c.get("price_targets") or {}
    if "mean" in pt:
        L.append(f"- 目标价：均值 {round(pt['mean'], 1)}，中位 {pt.get('median')}，区间 {pt.get('low')} 至 {pt.get('high')}")
    nxt = [r for r in (c.get("earnings_dates") or []) if isinstance(r, dict) and r.get("eps_reported") is None]
    if nxt:
        L.append(f"- 下次财报：{nxt[-1]['date']}，EPS 预期 {nxt[-1]['eps_est']}")
    moves = [m for m in c.get("post_earnings_moves") or [] if "next_day_pct" in m]
    if moves:
        L.append("- 最近几次财报后次日涨跌：" + "，".join(f"{m['earnings']} {m['next_day_pct']:+.2f}%" for m in moves))
    im = c.get("implied_model") or {}
    for per in ("0y", "+1y"):
        m = im.get(per)
        if m:
            L.append(f"- {m['label']}隐含：一致收入 {m['consensus_revenue_bn']}、EPS {m['consensus_eps']} → 税前 {m['implied_pretax_bn']}、"
                     f"总费用 {m['implied_total_expenses_bn']}（十亿美元），经营利润率 {m['implied_op_margin_pct']}%；"
                     f"费用每多 100 亿 EPS {m['eps_per_10bn_expense']}，收入每多 1%（不加成本）EPS +{m['eps_per_1pct_revenue_no_cost']}")
    if im.get("assumptions"):
        a = im["assumptions"]
        L.append(f"- 假设：稀释股数 {a['diluted_shares_bn']} 十亿股、税率 {a['tax_rate']}、其他收入 {a['other_income_bn'] or 0}（{a['source']}）")
    for warning in im.get("warnings", []):
        L.append(f"- 模型缺口：{warning}")
    if im.get("error"):
        L.append(f"- 模型缺口：{im['error']}")
    return "\n".join(L) + "\n"


def snapshot_status(snap: dict) -> dict:
    """空表和字段异常都保留；行情成功不能掩盖一致预期缺失。"""
    fields = ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions",
              "price_targets", "recommendations", "earnings_dates", "price")
    statuses = {k: ("error" if isinstance(snap.get(k), dict) and "error" in snap[k]
                    else "ok" if snap.get(k) else "empty") for k in fields}
    core_ok = any(statuses[k] == "ok" for k in fields[:4])
    model = snap.get("implied_model") or {}
    complete = all(v == "ok" for v in statuses.values()) and not model.get("error") and not model.get("warnings")
    return {"ok": core_ok, "status": "error" if not core_ok else
            "ok" if complete else "partial", "field_status": statuses}


def main() -> int:
    # 旧的 python3 调用同样固定到受管环境，避免再次落回系统旧版本。
    import os
    from pathlib import Path
    default_code = Path(__file__).resolve().parent.parent
    raw_code = os.environ.get("DALTON_RESEARCH_CODE", "").strip()
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    code_root = Path(raw_code).expanduser().resolve() if raw_code else default_code.resolve()
    home_root = (
        Path(raw_home).expanduser().resolve()
        if raw_home
        else (Path.home() / "Library" / "Application Support" / "Everflow" / "research")
    )
    runtime = home_root / "venvs/consensus-estimates"
    if Path(sys.prefix).resolve() != runtime.resolve():
        os.execv("/bin/bash", ["bash", str(code_root / "scripts/consensus_estimates.sh"), *sys.argv[1:]])
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--shares-bn", type=float, help="稀释股数（十亿股）；不传取 Yahoo 最近一季")
    ap.add_argument("--tax", type=float, help="税率（0≤税率<1）；不传取 Yahoo 最近一季有效税率")
    ap.add_argument("--md", help="另写一份 Markdown 到这个路径")
    ap.add_argument("--json", action="store_true", help="兼容标志，输出始终是 JSON")
    ap.add_argument("--consensus-only", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if (a.shares_bn is not None and (not math.isfinite(a.shares_bn) or a.shares_bn <= 0)) or \
            (a.tax is not None and (not math.isfinite(a.tax) or not 0 <= a.tax < 1)):
        print(json.dumps({"ok": False, "status": "error", "error": "股数须为正数，税率须为0≤税率<1"}, ensure_ascii=False))
        return 1
    try:
        snap = fetch_consensus(a.ticker.upper()) if a.consensus_only else snapshot(a.ticker.upper(), a.shares_bn, a.tax)
    except ImportError:
        print(json.dumps({"ok": False, "error": "缺 yfinance", "hint": "运行 bash scripts/consensus_estimates.sh 修复独立环境"}, ensure_ascii=False))
        return 1
    if a.md:
        with open(a.md, "w") as f:
            f.write(render_md(snap))
    status = snapshot_status(snap)
    print(json.dumps({**snap, **status}, ensure_ascii=False, indent=1, default=str))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
