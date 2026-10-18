#!/usr/bin/env python3
"""Compute Bear / Base / Bull scenario IRRs, probability-weighted IRR, and downside metrics.

Formula:
  scenario_irr = ((target_price / current_price) ** (1.0 / years)) - 1.0 + shareholder_yield
  weighted_irr = sum(prob_i * scenario_irr_i)   (when probabilities sum to 1.0)
  downside_drawdown = (bear_price / current_price) - 1.0
  upside_downside_ratio = (bull_price - current_price) / max(current_price - bear_price, 1e-9)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any, Dict, List, Optional, Sequence


def parse_slash_floats(raw: str, name: str, expected_len: int = 3) -> List[float]:
    parts = [p.strip() for p in raw.replace(",", "/").split("/") if p.strip()]
    if len(parts) != expected_len:
        raise ValueError(f"{name} must have {expected_len} values separated by '/' or ',', got: {raw!r}")
    return [float(x) for x in parts]


def compute_scenario_irr(
    current_price: float,
    years: float,
    prices: Sequence[float],
    probs: Optional[Sequence[float]] = None,
    shareholder_yield: float = 0.0,
    hurdle_rate: float = 0.15,
    eps: Optional[Sequence[float]] = None,
    multiples: Optional[Sequence[float]] = None,
) -> Dict[str, Any]:
    if current_price <= 0:
        raise ValueError(f"current_price must be > 0, got {current_price}")
    if years <= 0:
        raise ValueError(f"years must be > 0, got {years}")
    if len(prices) != 3:
        raise ValueError(f"prices must have 3 elements (Bear, Base, Bull), got {len(prices)}")
    if any(p <= 0 for p in prices):
        raise ValueError(f"all scenario target prices must be > 0, got {prices}")

    bear_p, base_p, bull_p = float(prices[0]), float(prices[1]), float(prices[2])

    labels = ["Bear", "Base", "Bull"]
    scenarios: Dict[str, Dict[str, Any]] = {}
    irr_list: List[float] = []

    for idx, label in enumerate(labels):
        tp = float(prices[idx])
        total_return = (tp / current_price) - 1.0
        price_cagr = ((tp / current_price) ** (1.0 / years)) - 1.0
        annual_irr = price_cagr + shareholder_yield
        irr_list.append(annual_irr)

        entry: Dict[str, Any] = {
            "target_price": round(tp, 4),
            "total_price_return": round(total_return, 6),
            "total_price_return_pct": f"{total_return * 100:+.1f}%",
            "price_cagr": round(price_cagr, 6),
            "shareholder_yield": round(shareholder_yield, 6),
            "irr": round(annual_irr, 6),
            "irr_pct": f"{annual_irr * 100:+.1f}%",
        }
        if eps is not None and multiples is not None:
            entry["eps"] = round(float(eps[idx]), 4)
            entry["multiple"] = round(float(multiples[idx]), 4)
        scenarios[label] = entry

    prob_mode = "示意性偏斜"
    weighted_irr: Optional[float] = None
    weighted_target_price: Optional[float] = None
    excess_vs_hurdle: Optional[float] = None
    passes_hurdle_gate: Optional[bool] = None

    if probs is not None:
        if len(probs) != 3:
            raise ValueError(f"probs must have 3 elements (Bear, Base, Bull), got {len(probs)}")
        if any(p < 0 for p in probs):
            raise ValueError(f"probabilities must be >= 0, got {probs}")
        prob_sum = sum(probs)
        if not math.isclose(prob_sum, 1.0, abs_tol=1e-4):
            raise ValueError(f"probabilities must sum to 1.0, got {prob_sum:.4f} ({probs})")

        prob_mode = "概率加权"
        for idx, label in enumerate(labels):
            scenarios[label]["probability"] = round(float(probs[idx]), 4)
            scenarios[label]["probability_pct"] = f"{probs[idx] * 100:.1f}%"

        weighted_irr = sum(float(probs[i]) * irr_list[i] for i in range(3))
        weighted_target_price = sum(float(probs[i]) * float(prices[i]) for i in range(3))
        excess_vs_hurdle = weighted_irr - hurdle_rate
        passes_hurdle_gate = weighted_irr > hurdle_rate

    downside_drawdown = (bear_p / current_price) - 1.0
    bull_upside = (bull_p / current_price) - 1.0
    base_upside = (base_p / current_price) - 1.0
    up_down_ratio = (
        (bull_upside / abs(downside_drawdown))
        if downside_drawdown < 0
        else None
    )

    return {
        "inputs": {
            "current_price": round(current_price, 4),
            "years": round(years, 4),
            "shareholder_yield": round(shareholder_yield, 6),
            "hurdle_rate": round(hurdle_rate, 6),
            "mode": prob_mode,
        },
        "scenarios": scenarios,
        "summary": {
            "mode": prob_mode,
            "bear_irr": round(irr_list[0], 6),
            "bear_irr_pct": f"{irr_list[0] * 100:+.1f}%",
            "base_irr": round(irr_list[1], 6),
            "base_irr_pct": f"{irr_list[1] * 100:+.1f}%",
            "bull_irr": round(irr_list[2], 6),
            "bull_irr_pct": f"{irr_list[2] * 100:+.1f}%",
            "weighted_target_price": round(weighted_target_price, 4) if weighted_target_price is not None else None,
            "weighted_irr": round(weighted_irr, 6) if weighted_irr is not None else None,
            "weighted_irr_pct": f"{weighted_irr * 100:+.1f}%" if weighted_irr is not None else "示意性偏斜（未赋概率）",
            "hurdle_rate": round(hurdle_rate, 6),
            "hurdle_rate_pct": f"{hurdle_rate * 100:.1f}%",
            "excess_vs_hurdle": round(excess_vs_hurdle, 6) if excess_vs_hurdle is not None else None,
            "excess_vs_hurdle_pct": f"{excess_vs_hurdle * 100:+.1f}%" if excess_vs_hurdle is not None else None,
            "passes_gate_5_hurdle": passes_hurdle_gate,
            "downside_max_drawdown": round(downside_drawdown, 6),
            "downside_max_drawdown_pct": f"{downside_drawdown * 100:+.1f}%",
            "base_upside": round(base_upside, 6),
            "bull_upside": round(bull_upside, 6),
            "upside_downside_ratio": round(up_down_ratio, 2) if up_down_ratio is not None else None,
        },
    }


def format_markdown_table(res: Dict[str, Any]) -> str:
    sc = res["scenarios"]
    sm = res["summary"]
    inp = res["inputs"]
    has_probs = inp["mode"] == "概率加权"

    lines = [
        f"### 三档情景与 IRR 测算（当前价: {inp['current_price']}，持有期: {inp['years']} 年，股东收益率: {inp['shareholder_yield']*100:.1f}%）",
        "",
        "| 情景 | 目标股价 | 累计涨跌幅 | 年化 IRR | 概率 |",
        "|---|---:|---:|---:|---:|",
    ]
    for label in ["Bear", "Base", "Bull"]:
        item = sc[label]
        prob_str = item.get("probability_pct", "示意性偏斜") if has_probs else "示意性偏斜"
        lines.append(
            f"| **{label}** | {item['target_price']:.2f} | {item['total_price_return_pct']} | {item['irr_pct']} | {prob_str} |"
        )
    lines.append("")
    if has_probs:
        gate_str = "PASS" if sm["passes_gate_5_hurdle"] else "FAIL"
        lines.append(
            f"- **概率加权目标价**：{sm['weighted_target_price']:.2f} | "
            f"**概率加权年化 IRR**：{sm['weighted_irr_pct']} "
            f"（vs 门槛 {sm['hurdle_rate_pct']}：{sm['excess_vs_hurdle_pct']}，Gate 5: **{gate_str}**）"
        )
    else:
        lines.append("- **概率模式**：示意性偏斜（未赋主观概率，不计算概率加权 IRR）")
    lines.append(
        f"- **Bear 最大回撤**：{sm['downside_max_drawdown_pct']} | "
        f"**Bull/Bear 上下行赔率**：{sm['upside_downside_ratio']}x"
    )
    return "\n".join(lines)


def run_self_test() -> int:
    # Required verification case: current=100, years=2, prices=80/130/170, probs=0.25/0.5/0.25, yield=0
    res = compute_scenario_irr(
        current_price=100.0,
        years=2.0,
        prices=[80.0, 130.0, 170.0],
        probs=[0.25, 0.50, 0.25],
        shareholder_yield=0.0,
        hurdle_rate=0.15,
    )
    base_irr = res["summary"]["base_irr"]
    expected_base = (130.0 / 100.0) ** 0.5 - 1.0  # 0.140175425...
    assert math.isclose(base_irr, expected_base, rel_tol=1e-5), f"Expected {expected_base}, got {base_irr}"
    assert res["summary"]["base_irr_pct"] == "+14.0%", f"Expected +14.0%, got {res['summary']['base_irr_pct']}"
    assert math.isclose(res["summary"]["downside_max_drawdown"], -0.20, abs_tol=1e-6)

    # Skew-only mode without probabilities ("示意性偏斜")
    res_skew = compute_scenario_irr(
        current_price=100.0,
        years=2.0,
        prices=[80.0, 130.0, 170.0],
        probs=None,
    )
    assert res_skew["summary"]["mode"] == "示意性偏斜"
    assert res_skew["summary"]["weighted_irr"] is None
    print("scenario_irr.py self-test PASSED (Base IRR = +14.0%).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calculate Bear/Base/Bull scenario IRRs, probability-weighted IRR, and downside drawdown."
    )
    parser.add_argument("cmd", nargs="?", choices=["self-test"], help="Optional 'self-test' subcommand.")
    parser.add_argument("--current", type=float, help="Current stock price (>0).")
    parser.add_argument("--years", type=float, default=2.0, help="Investment horizon in years (default: 2.0).")
    parser.add_argument(
        "--prices",
        type=str,
        help="Slash- or comma-separated target prices for Bear/Base/Bull, e.g. '80/130/170'.",
    )
    parser.add_argument(
        "--eps",
        type=str,
        help="Optional slash-separated Bear/Base/Bull EPS (used with --multiples if --prices is omitted).",
    )
    parser.add_argument(
        "--multiples",
        type=str,
        help="Optional slash-separated Bear/Base/Bull P/E multiples (used with --eps if --prices is omitted).",
    )
    parser.add_argument(
        "--probs",
        type=str,
        default=None,
        help="Slash- or comma-separated probabilities for Bear/Base/Bull summing to 1.0, e.g. '0.25/0.5/0.25'. Omit for '示意性偏斜'.",
    )
    parser.add_argument(
        "--yield",
        dest="shareholder_yield",
        type=float,
        default=0.0,
        help="Annual dividend + net buyback yield as decimal, e.g. 0.015 for 1.5%% (default: 0.0).",
    )
    parser.add_argument(
        "--hurdle",
        type=float,
        default=0.15,
        help="Annual hurdle rate as decimal (default: 0.15 for 15%%).",
    )
    parser.add_argument("--json", action="store_true", help="Print output as JSON.")
    parser.add_argument("--self-test", action="store_true", help="Run built-in unit self-test.")
    args = parser.parse_args()

    if args.self_test or args.cmd == "self-test":
        return run_self_test()

    if args.current is None:
        parser.error("--current is required (or run with --self-test).")

    eps_vals: Optional[List[float]] = None
    mult_vals: Optional[List[float]] = None

    if args.prices:
        price_vals = parse_slash_floats(args.prices, "--prices", 3)
        if args.eps and args.multiples:
            eps_vals = parse_slash_floats(args.eps, "--eps", 3)
            mult_vals = parse_slash_floats(args.multiples, "--multiples", 3)
    elif args.eps and args.multiples:
        eps_vals = parse_slash_floats(args.eps, "--eps", 3)
        mult_vals = parse_slash_floats(args.multiples, "--multiples", 3)
        price_vals = [eps_vals[i] * mult_vals[i] for i in range(3)]
    else:
        parser.error("Provide either --prices 'Bear/Base/Bull' or both --eps and --multiples.")

    prob_vals = parse_slash_floats(args.probs, "--probs", 3) if args.probs else None

    try:
        result = compute_scenario_irr(
            current_price=args.current,
            years=args.years,
            prices=price_vals,
            probs=prob_vals,
            shareholder_yield=args.shareholder_yield,
            hurdle_rate=args.hurdle,
            eps=eps_vals,
            multiples=mult_vals,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_markdown_table(result))

    return 0


if __name__ == "__main__":
    sys.exit(main())
