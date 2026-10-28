#!/usr/bin/env python3
"""Dalton 一致预期隐含费用测算与 EPS 敏感度脚本（`implied_model.py`）。

移植自 OpenClaw `consensus_estimates.py implied_model`，纯 Python 标准库实现。

核心公式（单位：十亿 <币种> / 每股 <币种>）：
- implied_pretax = EPS * shares / (1 - tax)
- implied_total_expenses = rev - implied_pretax + other_income
- implied_op_margin_pct = (implied_pretax - other_income) / rev * 100
- eps_per_10bn_expense = -10 * (1 - tax) / shares
- eps_per_1pct_revenue = rev * 0.01 * (1 - tax) / shares

币种契约（对齐 researchdata 1.5）：只有某期间 EPS 与收入的 `currency` 都明确为 USD
才计算美元隐含模型；币种为其他或缺失（未知）时该期间不出美元数字，保留原币种
一致预期数值并在 warnings 写明原因。零 EPS 是有效值（None 才是缺失）；收入换算
统一按"原始值为报表币种元、除以 1e9 得十亿"，不按数值大小猜单位。

用法 1（直接传参测算单期）：
  python implied_model.py --rev-bn 205.0 --eps 25.5 --shares-bn 2.55 --tax 0.15 --period +1y

用法 2（读取 researchdata.finance(route="estimates") 保存的快照 JSON）：
  python implied_model.py --estimates-json data/consensus/2026-10-18_0900.json [--shares-bn 2.55] [--tax 0.15] [--md consensus.md]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any


def _norm_currency(value: Any) -> str | None:
    """字段币种规范化：缺失/非字符串返回 None（未知），其余去空格大写。"""
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return None


def _revenue_bn(row: dict[str, Any]) -> float | None:
    """收入换算为十亿：上游已给 `*_bn` 字段直接用；否则原始值是报表币种元，统一 /1e9。"""
    for key in ("avg_bn", "revenue_bn"):
        v = row.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    avg = row.get("avg")
    if isinstance(avg, (int, float)) and not isinstance(avg, bool):
        return float(avg) / 1e9
    return None


def calc_period_implied(
    rev_bn: float,
    eps: float,
    shares_bn: float,
    tax_rate: float,
    other_income_bn: float = 0.0,
    label: str = "下一财年",
    currency: str = "USD",
) -> dict[str, Any]:
    """用一致预期 EPS 与收入（十亿 `currency`）反推隐含税前利润、隐含总费用空间与 EPS 敏感度。"""
    if shares_bn <= 0:
        raise ValueError("shares_bn 必须大于 0")
    if not (0.0 <= tax_rate < 1.0):
        raise ValueError("tax_rate 必须在 [0, 1) 区间内")
    if rev_bn <= 0:
        raise ValueError("rev_bn 必须大于 0")

    implied_pretax = eps * shares_bn / (1.0 - tax_rate)
    implied_total_expenses = rev_bn - implied_pretax + other_income_bn
    implied_op_margin_pct = (implied_pretax - other_income_bn) / rev_bn * 100.0
    eps_per_10bn_expense = -10.0 * (1.0 - tax_rate) / shares_bn
    eps_per_1pct_revenue = rev_bn * 0.01 * (1.0 - tax_rate) / shares_bn

    return {
        "label": label,
        "currency": currency,
        "consensus_revenue_bn": round(rev_bn, 2),
        "consensus_eps": round(eps, 2),
        "implied_pretax_bn": round(implied_pretax, 2),
        "implied_total_expenses_bn": round(implied_total_expenses, 2),
        "implied_op_margin_pct": round(implied_op_margin_pct, 1),
        "eps_per_10bn_expense": round(eps_per_10bn_expense, 2),
        "eps_per_1pct_revenue": round(eps_per_1pct_revenue, 2),
        "eps_per_1pct_revenue_no_cost": round(eps_per_1pct_revenue, 2),
    }


def implied_model(
    cons: dict[str, Any],
    shares: float,
    tax: float,
    other_income_bn: dict[str, float] | None = None,
    source: str = "",
) -> dict[str, Any]:
    """从 estimates 快照字典计算 0y 与 +1y 的隐含费用空间与 EPS 敏感度。

    仅 EPS 与收入币种都确认为 USD 的期间计算美元模型；否则保留原币种一致预期
    数值并写明原因。快照的 `field_status` 与上游模型 `warnings` 原样透传。
    """
    other = other_income_bn or {}
    out: dict[str, Any] = {
        "layer": "model-derived",
        "assumptions": {
            "diluted_shares_bn": shares,
            "tax_rate": tax,
            "other_income_bn": other,
            "source": source,
            "note": "隐含总费用 = 一致收入 − EPS×股数/(1−税率) + 其他收入；单位十亿 <币种>，"
                    "仅 EPS 与收入均确认为 USD 的期间计算美元模型",
        },
        "warnings": [],
    }
    if isinstance(cons.get("field_status"), dict):
        out["field_status"] = cons["field_status"]
    upstream = cons.get("implied_model") if isinstance(cons.get("implied_model"), dict) else {}
    if isinstance(upstream.get("warnings"), list):
        out["upstream_warnings"] = list(upstream["warnings"])
    ee = cons.get("earnings_estimate") or {}
    re_ = cons.get("revenue_estimate") or {}
    for field, section in (("earnings_estimate", ee), ("revenue_estimate", re_)):
        if isinstance(section, dict) and section.get("error"):
            out["warnings"].append(f"{field} 字段读取失败：{section['error']}")
    for period, label in (("0y", "本财年"), ("+1y", "下一财年")):
        e_row = ee.get(period) if isinstance(ee.get(period), dict) else {}
        r_row = re_.get(period) if isinstance(re_.get(period), dict) else {}
        eps = e_row.get("avg")
        eps_cur = _norm_currency(e_row.get("currency"))
        rev_cur = _norm_currency(r_row.get("currency"))
        rev_bn = _revenue_bn(r_row)
        if eps is None or rev_bn is None or rev_bn == 0:
            continue  # None 才是缺失；EPS=0 是有效值，币种通过后照常计算
        if eps_cur != "USD" or rev_cur != "USD":
            cur_text = "/".join(c or "未知" for c in (eps_cur, rev_cur))
            out["warnings"].append(f"{period} EPS/收入币种为 {cur_text}，未做美元隐含测算")
            out[period] = {
                "label": label,
                "currency": {"eps": eps_cur or "未知", "revenue": rev_cur or "未知"},
                "consensus_revenue_bn": round(rev_bn, 2),
                "consensus_eps": round(float(eps), 2),
                "note": f"币种为 {cur_text}，未做美元隐含测算；保留原币种一致预期数值",
            }
            continue
        out[period] = calc_period_implied(
            rev_bn=rev_bn,
            eps=float(eps),
            shares_bn=shares,
            tax_rate=tax,
            other_income_bn=float(other.get(period, 0.0) or 0.0),
            label=label,
            currency="USD",
        )
    return out


def render_md(im: dict[str, Any], ticker: str = "") -> str:
    title = f"# {ticker + ' ' if ticker else ''}一致预期隐含费用测算（model-derived）"
    lines = [title, ""]
    a = im.get("assumptions") or {}
    if a:
        lines.append(
            f"- 假设：稀释股数 {a.get('diluted_shares_bn')} 十亿股、有效税率 {a.get('tax_rate')}、"
            f"其他收入 {a.get('other_income_bn') or 0} 十亿美元（美元模型专用）"
            + (f"（{a['source']}）" if a.get("source") else "")
        )
    for per in ("0y", "+1y"):
        m = im.get(per)
        if not isinstance(m, dict):
            continue
        cur = m.get("currency")
        if isinstance(cur, dict):
            # 币种未确认同为 USD 的期间：不出美元数字，保留原币种一致预期
            lines.append(
                f"- **{m['label']}（{per}）**：{m.get('note', '未做美元隐含测算')}；"
                f"一致收入 {m['consensus_revenue_bn']} 十亿{cur.get('revenue', '未知')}、"
                f"一致 EPS {m['consensus_eps']}（{cur.get('eps', '未知')}）"
            )
            continue
        unit = f"十亿{cur or 'USD'}"
        lines.append(
            f"- **{m['label']}（{per}）**：一致收入 {m['consensus_revenue_bn']} {unit}、一致 EPS {m['consensus_eps']} "
            f"→ 隐含税前利润 {m['implied_pretax_bn']} {unit}、隐含总费用空间 **{m['implied_total_expenses_bn']} {unit}**，"
            f"隐含经营利润率 {m['implied_op_margin_pct']}%；"
            f"敏感度：费用每多 100 亿{cur or 'USD'}对应 EPS {m['eps_per_10bn_expense']:+.2f}，"
            f"收入每多 1%（不加成本）对应 EPS {m['eps_per_1pct_revenue']:+.2f}"
        )
    fs = im.get("field_status") if isinstance(im.get("field_status"), dict) else {}
    bad = {k: v for k, v in fs.items() if v != "ok"}
    if bad:
        lines.append("- 上游字段状态：" + "、".join(f"{k}={v}" for k, v in bad.items()))
    for w in im.get("upstream_warnings", []) or []:
        lines.append(f"- 上游模型缺口：{w}")
    for w in im.get("warnings", []) or []:
        lines.append(f"- 模型缺口：{w}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Dalton 一致预期隐含费用空间与 EPS 敏感度测算")
    ap.add_argument("--estimates-json", default=None, help="researchdata.finance(route='estimates') 快照 JSON 路径")
    ap.add_argument("--ticker", default="", help="股票代码（用于 Markdown 标题）")
    ap.add_argument("--period", default="+1y", choices=["0y", "+1y"], help="直接传参模式下的期间标签（默认 +1y）")
    ap.add_argument("--rev-bn", type=float, default=None, help="一致预期收入（十亿美元）")
    ap.add_argument("--eps", type=float, default=None, help="一致预期 EPS（美元）")
    ap.add_argument("--shares-bn", type=float, default=None, help="稀释股数（十亿股）")
    ap.add_argument("--tax", type=float, default=None, help="有效税率（0–1 小数，如 0.15）")
    ap.add_argument("--other-income-bn", type=float, default=0.0, help="其他收入净额（十亿美元，默认 0）")
    ap.add_argument("--source", default="命令行输入", help="股数与税率假设来源说明")
    ap.add_argument("--md", default=None, help="可选：将 Markdown 摘要写入该文件路径")
    args = ap.parse_args()

    try:
        if args.estimates_json:
            raw = json.loads(pathlib.Path(args.estimates_json).read_text(encoding="utf-8"))
            data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
            existing_im = data.get("implied_model") or {}
            existing_assump = existing_im.get("assumptions") or {}
            shares = args.shares_bn if args.shares_bn is not None else existing_assump.get("diluted_shares_bn")
            tax = args.tax if args.tax is not None else existing_assump.get("tax_rate")
            if shares is None or tax is None:
                raise ValueError("快照中无默认股数/税率，请显式传入 --shares-bn 与 --tax")
            oth_map = existing_assump.get("other_income_bn") if isinstance(existing_assump.get("other_income_bn"), dict) else {}
            if args.other_income_bn:
                oth_map = {**oth_map, "0y": args.other_income_bn, "+1y": args.other_income_bn}
            res = implied_model(
                data,
                shares=float(shares),
                tax=float(tax),
                other_income_bn=oth_map,
                source=args.source if (args.shares_bn is not None or args.tax is not None) else existing_assump.get("source", ""),
            )
            ticker = args.ticker or str(data.get("ticker") or "")
        else:
            if args.rev_bn is None or args.eps is None or args.shares_bn is None or args.tax is None:
                raise ValueError("未传 --estimates-json 时，必须同时传入 --rev-bn、--eps、--shares-bn、--tax")
            label = "本财年" if args.period == "0y" else "下一财年"
            per_res = calc_period_implied(
                rev_bn=args.rev_bn,
                eps=args.eps,
                shares_bn=args.shares_bn,
                tax_rate=args.tax,
                other_income_bn=args.other_income_bn,
                label=label,
            )
            res = {
                "layer": "model-derived",
                "assumptions": {
                    "diluted_shares_bn": args.shares_bn,
                    "tax_rate": args.tax,
                    "other_income_bn": {args.period: args.other_income_bn},
                    "source": args.source,
                    "note": "隐含总费用 = 一致收入 − EPS×股数/(1−税率) + 其他收入；"
                            "单位十亿 <币种>，直接传参模式按美元口径计算",
                },
                args.period: per_res,
            }
            ticker = args.ticker
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1

    if args.md:
        md_path = pathlib.Path(args.md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_md(res, ticker=ticker), encoding="utf-8")

    print(json.dumps({"ok": True, **res}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
