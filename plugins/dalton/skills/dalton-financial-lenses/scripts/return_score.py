#!/usr/bin/env python3
"""Deterministic shareholder return score calculator (0-20 scale).

Ported from findata-return (`findata_return_cli.py`) per PS.2 specification:
- R1 (TTM): ttm_div + ttm_buyback * true_return_share
- R2 (3Y avg): sum(annual_3y) / 3.0 (or None if company has < 3 years of listing history)
- R3 (12M fwd): fwd_total * (credibility / 100.0)
- R = max(available of R1, R2, R3); driver in {"R1", "R2", "R3"}
- base_score = min(R, 20.0)
- sustainability adjustments summed and clamped to [-1.0, +2.0]
- final_score = round(clamp(base_score + adj, 0.0, 20.0), 1)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

VALID_CREDIBILITY = (100, 90, 75, 60, 50)

CREDIBILITY_TIER_ZH: dict[int, str] = {
    100: "管理层明确金额或比例，且已有董事会决议并正在执行",
    90: "管理层明确比例或金额，且当前已有执行",
    75: "明确方向性目标但无精确数字，过往兑现记录良好",
    60: "方向性表态，历史执行一般",
    50: "无明确政策，按近年趋势外推",
}


def _load_anchors_payload(raw_anchors: str) -> dict[str, Any]:
    s = raw_anchors.strip()
    if s == "-":
        data = json.load(sys.stdin)
    elif s.startswith("{"):
        data = json.loads(s)
    else:
        data = json.loads(Path(s).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("--anchors JSON must be an object")
    return data


def _extract_anchor_fields(anchors_obj: dict[str, Any]) -> dict[str, Any]:
    fx_val: float | None = None
    if isinstance(anchors_obj.get("fx"), dict) and anchors_obj["fx"].get("rate") is not None:
        fx_val = float(anchors_obj["fx"]["rate"])
    elif anchors_obj.get("fx_rate_to_usd") is not None:
        fx_val = float(anchors_obj["fx_rate_to_usd"])
    elif anchors_obj.get("fx_rate") is not None:
        fx_val = float(anchors_obj["fx_rate"])

    mcap_mn_val: float | None = None
    if isinstance(anchors_obj.get("market_cap"), dict):
        mc_dict = anchors_obj["market_cap"]
        raw_mc = mc_dict.get("market_cap_usd_mn", mc_dict.get("market_cap_usd"))
        if raw_mc is not None:
            mcap_mn_val = float(raw_mc)
    elif anchors_obj.get("market_cap_usd_mn") is not None:
        mcap_mn_val = float(anchors_obj["market_cap_usd_mn"])

    return {
        "ticker": anchors_obj.get("ticker"),
        "period_end": anchors_obj.get("period_end"),
        "as_of": anchors_obj.get("as_of"),
        "fx_rate_to_usd": fx_val,
        "market_cap_usd_mn": round(mcap_mn_val, 2) if mcap_mn_val is not None else None,
    }


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
    annual: Sequence[float] | None,
    fwd_total: float,
    credibility: int,
    adjustments: Sequence[tuple[float, str] | dict[str, Any]] | None = None,
    credibility_reason: str | None = None,
    annual_na_reason: str | None = None,
) -> dict[str, Any]:
    """Compute deterministic 0-20 shareholder return score per PS.2."""
    if not (0.0 <= true_return_share <= 1.0):
        raise ValueError("true_return_share must be in [0, 1]")
    if int(credibility) not in VALID_CREDIBILITY:
        raise ValueError(f"credibility must be one of {VALID_CREDIBILITY}")

    cred_int = int(credibility)
    cred_desc = (credibility_reason or "").strip() or CREDIBILITY_TIER_ZH[cred_int]

    annual_list: list[float] | None = None
    r2: float | None = None
    if annual is not None and len(annual) > 0:
        if len(annual) != 3:
            raise ValueError("annual must have 3 values (most recent 3 fiscal years) or be None/N/A")
        annual_list = [float(x) for x in annual]
        r2 = sum(annual_list) / 3.0

    norm_adjustments: list[tuple[float, str]] = []
    for item in adjustments or []:
        if isinstance(item, dict):
            norm_adjustments.append((float(item["delta"]), str(item["reason"]).strip()))
        else:
            d, r = item
            norm_adjustments.append((float(d), str(r).strip()))

    r1 = float(ttm_div) + float(ttm_buyback) * float(true_return_share)
    r3 = float(fwd_total) * (cred_int / 100.0)

    candidates: list[tuple[str, float]] = [("R1", r1)]
    if r2 is not None:
        candidates.append(("R2", r2))
    candidates.append(("R3", r3))

    r = max(val for _, val in candidates)
    driver = next(name for name, val in candidates if val == r)
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
    r2_str = (
        f"R2(三年均值)={r2:.2f}%"
        if r2 is not None
        else f"R2(三年均值)=N/A（{annual_na_reason or '上市不足三年'}）"
    )
    rationale = (
        f"R1(TTM 净回报率)={r1:.2f}%，{r2_str}，"
        f"R3(前瞻折算，可信度 {cred_int}%：{cred_desc})={r3:.2f}%；"
        f"取最高 {driver}={r:.2f}% 对应基础分 {base:.2f}；"
        f"可持续性调整 {_fmt_signed(adj)}（{adj_text}）；"
        f"最终得分 {final_rounded:.1f}/20。"
    )

    return {
        "inputs": {
            "ttm_div_pct": float(ttm_div),
            "ttm_buyback_pct": float(ttm_buyback),
            "true_return_share": float(true_return_share),
            "annual_3y_pct": annual_list,
            "annual_total_return_pct": annual_list,
            "fwd_total_pct": float(fwd_total),
            "credibility_pct": cred_int,
            "credibility_reason": cred_desc,
        },
        "R1": round(r1, 2),
        "R2": round(r2, 2) if r2 is not None else None,
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
        "anchors_source": "manual",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic 0-20 shareholder return score calculator (PS.2)"
    )
    parser.add_argument(
        "--anchors",
        default=None,
        help="Path to anchors.py JSON output file, '-' for stdin, or inline JSON object string",
    )
    parser.add_argument(
        "--market-cap-usd-mn",
        type=float,
        default=None,
        help="Manual market cap in USD millions (mutually exclusive with --anchors)",
    )
    parser.add_argument(
        "--market-cap-usd",
        type=float,
        default=None,
        help="Manual market cap in USD (mutually exclusive with --anchors)",
    )
    parser.add_argument(
        "--fx-rate",
        type=float,
        default=None,
        help="Manual reporting currency -> USD FX rate (mutually exclusive with --anchors)",
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
        help="Comma-separated 3 fiscal year total return yields (pct), e.g. '11.24,11.88,15.05', or 'N/A[:reason]'",
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
        "--credibility-reason",
        type=str,
        default=None,
        help="Optional explanation for credibility tier selection",
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
        has_manual = (
            args.market_cap_usd_mn is not None
            or args.market_cap_usd is not None
            or args.fx_rate is not None
        )
        if args.anchors and has_manual:
            raise ValueError(
                "Cannot specify both --anchors and manual --market-cap-usd-mn/--market-cap-usd/--fx-rate"
            )
        anchors_obj = _load_anchors_payload(args.anchors) if args.anchors else None
        anchor_meta = _extract_anchor_fields(anchors_obj) if anchors_obj is not None else {}

        annual_raw = args.annual.strip()
        annual_vals: list[float] | None
        annual_na_reason: str | None = None
        if annual_raw.upper().startswith(("N/A", "NA", "NULL", "NONE")):
            annual_vals = None
            if ":" in annual_raw:
                annual_na_reason = annual_raw.split(":", 1)[1].strip()
        else:
            annual_vals = [float(x.strip()) for x in annual_raw.split(",") if x.strip()]
        adjustments = parse_adjust(args.adjust)
        result = compute_return_score(
            ttm_div=args.ttm_div,
            ttm_buyback=args.ttm_buyback,
            true_return_share=args.true_return_share,
            annual=annual_vals,
            fwd_total=args.fwd_total,
            credibility=args.credibility,
            adjustments=adjustments,
            credibility_reason=args.credibility_reason,
            annual_na_reason=annual_na_reason,
        )
        if anchors_obj is not None:
            result["anchors_source"] = "anchors"
            result["ticker"] = anchor_meta.get("ticker")
            result["period_end"] = anchor_meta.get("period_end")
            result["market_cap_usd_mn"] = anchor_meta.get("market_cap_usd_mn")
            result["fx_rate_to_usd"] = anchor_meta.get("fx_rate_to_usd")
            result["anchors"] = {
                "ticker": anchor_meta.get("ticker"),
                "period_end": anchor_meta.get("period_end"),
                "as_of": anchor_meta.get("as_of"),
                "market_cap_usd_mn": anchor_meta.get("market_cap_usd_mn"),
                "fx_rate_to_usd": anchor_meta.get("fx_rate_to_usd"),
            }
        else:
            result["anchors_source"] = "manual"
            if args.market_cap_usd_mn is not None or args.market_cap_usd is not None:
                raw_mc = args.market_cap_usd_mn if args.market_cap_usd_mn is not None else args.market_cap_usd
                result["market_cap_usd_mn"] = round(float(raw_mc), 2) if raw_mc is not None else None
            if args.fx_rate is not None:
                result["fx_rate_to_usd"] = float(args.fx_rate)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
