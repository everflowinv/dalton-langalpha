#!/usr/bin/env python3
"""Deterministic shareholder return score calculator (0-20 scale).

Ported from findata-return (`findata_return_cli.py`) per PS.2 specification:
- R1 (TTM): ttm_div + ttm_buyback * true_return_share
- R2 (3Y avg): sum(annual_3y) / 3.0
- R3 (12M fwd): fwd_total * (credibility / 100.0)
- R = max(R1, R2, R3); driver in {"R1", "R2", "R3"}
- base_score = min(R, 20.0)
- sustainability adjustments summed and clamped to [-1.0, +2.0]
- final_score = round(clamp(base_score + adj, 0.0, 20.0), 1)
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

VALID_CREDIBILITY = (100, 90, 75, 60, 50)


def parse_adjust(items: Sequence[str] | None) -> list[tuple[float, str]]:
    """Parse ['+1:FCFF覆盖1.8x', '-0.5:举债回购'] into [(1.0, 'FCFF覆盖1.8x'), ...]."""
    out: list[tuple[float, str]] = []
    for raw in items or []:
        if ":" not in raw:
            raise ValueError(f"--adjust must be '<delta>:<reason>', got {raw!r}")
        delta_s, reason = raw.split(":", 1)
        reason_clean = reason.strip()
        if not reason_clean:
            raise ValueError(f"--adjust requires non-empty reason, got {raw!r}")
        out.append((float(delta_s), reason_clean))
    return out


def _fmt_signed(val: float) -> str:
    """Format signed float without truncating .5 to .0 (fixes PS.2 rationale_zh bug)."""
    if abs(val - round(val)) < 1e-9:
        return f"{int(round(val)):+d}"
    return f"{val:+.1f}"


def compute_return_score(
    ttm_div: float,
    ttm_buyback: float,
    true_return_share: float,
    annual: Sequence[float],
    fwd_total: float,
    credibility: int,
    adjustments: Sequence[tuple[float, str] | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute deterministic 0-20 shareholder return score per PS.2."""
    if not (0.0 <= true_return_share <= 1.0):
        raise ValueError("true_return_share must be in [0, 1]")
    if len(annual) != 3:
        raise ValueError("annual must have 3 values (most recent 3 fiscal years)")
    if int(credibility) not in VALID_CREDIBILITY:
        raise ValueError(f"credibility must be one of {VALID_CREDIBILITY}")

    cred_int = int(credibility)
    annual_list = [float(x) for x in annual]

    norm_adjustments: list[tuple[float, str]] = []
    for item in adjustments or []:
        if isinstance(item, dict):
            norm_adjustments.append((float(item["delta"]), str(item["reason"]).strip()))
        else:
            d, r = item
            norm_adjustments.append((float(d), str(r).strip()))

    r1 = float(ttm_div) + float(ttm_buyback) * float(true_return_share)
    r2 = sum(annual_list) / 3.0
    r3 = float(fwd_total) * (cred_int / 100.0)

    r = max(r1, r2, r3)
    driver = "R1" if r == r1 else ("R2" if r == r2 else "R3")
    base = min(r, 20.0)

    adj_raw = sum(d for d, _ in norm_adjustments)
    adj = max(-1.0, min(2.0, adj_raw))
    final = max(0.0, min(20.0, base + adj))
    final_rounded = round(final + 1e-9, 1)

    adj_text = (
        "；".join(f"{_fmt_signed(d)}（{reason}）" for d, reason in norm_adjustments)
        if norm_adjustments
        else "无调整"
    )
    rationale = (
        f"R1(TTM 净回报率)={r1:.2f}%，R2(三年均值)={r2:.2f}%，"
        f"R3(前瞻折算，可信度 {cred_int}%)={r3:.2f}%；"
        f"取三者最高 {driver}={r:.2f}% 对应基础分 {base:.2f}；"
        f"可持续性调整 {_fmt_signed(adj)}（{adj_text}）；"
        f"最终得分 {final_rounded:.1f}/20。"
    )

    return {
        "inputs": {
            "ttm_div_pct": float(ttm_div),
            "ttm_buyback_pct": float(ttm_buyback),
            "true_return_share": float(true_return_share),
            "annual_3y_pct": annual_list,
            "fwd_total_pct": float(fwd_total),
            "credibility_pct": cred_int,
        },
        "R1": round(r1, 2),
        "R2": round(r2, 2),
        "R3": round(r3, 2),
        "R": round(r, 2),
        "driver": driver,
        "base_score": round(base, 2),
        "adjustment": round(adj, 2),
        "adjustment_raw": round(adj_raw, 2),
        "adjustment_clamped": abs(adj - adj_raw) > 1e-9,
        "adjustments": [
            {"delta": d, "reason": r_str} for d, r_str in norm_adjustments
        ],
        "final_score": final_rounded,
        "max_score": 20,
        "rationale_zh": rationale,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic 0-20 shareholder return score calculator (PS.2)"
    )
    parser.add_argument("--ttm-div", type=float, required=True, help="TTM dividend yield (pct)")
    parser.add_argument("--ttm-buyback", type=float, required=True, help="TTM gross buyback yield (pct)")
    parser.add_argument(
        "--true-return-share",
        type=float,
        required=True,
        help="Cancellation + true-return share in [0, 1]",
    )
    parser.add_argument(
        "--annual",
        type=str,
        required=True,
        help="Comma-separated 3 fiscal year total return yields (pct), e.g. '11.24,11.88,15.05'",
    )
    parser.add_argument("--fwd-total", type=float, required=True, help="12M forward expected yield (pct)")
    parser.add_argument(
        "--credibility",
        type=int,
        choices=VALID_CREDIBILITY,
        required=True,
        help="Policy credibility weight (pct)",
    )
    parser.add_argument(
        "--adjust",
        action="append",
        default=[],
        help="Sustainability adjustment '<delta>:<reason>', repeatable",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Emit JSON output (default)",
    )
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    normalized_argv: list[str] = []
    i = 0
    while i < len(raw_argv):
        if raw_argv[i] == "--adjust" and i + 1 < len(raw_argv) and raw_argv[i + 1].startswith("-"):
            normalized_argv.append(f"--adjust={raw_argv[i + 1]}")
            i += 2
        else:
            normalized_argv.append(raw_argv[i])
            i += 1
    args = parser.parse_args(normalized_argv)

    try:
        annual_vals = [float(x.strip()) for x in args.annual.split(",") if x.strip()]
        adjustments = parse_adjust(args.adjust)
        result = compute_return_score(
            ttm_div=args.ttm_div,
            ttm_buyback=args.ttm_buyback,
            true_return_share=args.true_return_share,
            annual=annual_vals,
            fwd_total=args.fwd_total,
            credibility=args.credibility,
            adjustments=adjustments,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
