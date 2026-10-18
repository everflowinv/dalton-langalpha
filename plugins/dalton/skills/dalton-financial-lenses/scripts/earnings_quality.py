#!/usr/bin/env python3
"""Deterministic Stage 0 Earnings Quality calculator (PS.2).

Computes three core earnings quality metrics required by Stage 0 and Financial Lenses:
1. Sloan Ratio (Accruals Ratio):
   - Operating Sloan Ratio = (Net Income - CFO) / Average Total Assets
   - Total Sloan Ratio (Richardson / extended) = (Net Income - CFO - CFI) / Average Total Assets
   Flags high accrual risk when |Sloan Ratio| > 10% (0.10) or severe when > 25% (0.25).
2. CFO-to-NI Ratio (Operating Cash Conversion):
   - CFO / Net Income
   Flags weak cash conversion when Net Income > 0 and CFO / NI < 0.80 (80%).
3. Cash Conversion Cycle (CCC):
   - DIO = (Average Inventory / COGS) * period_days
   - DSO = (Average Accounts Receivable / Revenue) * period_days
   - DPO = (Average Accounts Payable / COGS) * period_days
   - CCC = DIO + DSO - DPO
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def compute_sloan_ratio(
    net_income: float,
    cfo: float,
    avg_total_assets: float,
    cfi: float | None = None,
) -> dict[str, Any]:
    """Compute operating and (optional) total Sloan accrual ratios."""
    if avg_total_assets <= 0:
        raise ValueError(f"avg_total_assets must be positive, got {avg_total_assets}")

    operating_accruals = float(net_income) - float(cfo)
    operating_sloan = operating_accruals / float(avg_total_assets)

    total_accruals: float | None = None
    total_sloan: float | None = None
    if cfi is not None:
        total_accruals = float(net_income) - float(cfo) - float(cfi)
        total_sloan = total_accruals / float(avg_total_assets)

    abs_op = abs(operating_sloan)
    if operating_sloan > 0.25:
        signal = "severe_accrual_warning"
    elif operating_sloan > 0.10:
        signal = "elevated_accrual_warning"
    elif abs_op <= 0.10:
        signal = "healthy_normal"
    else:
        signal = "conservative_cash_exceeds_earnings"

    return {
        "operating_accruals": round(operating_accruals, 2),
        "operating_sloan_ratio": round(operating_sloan, 4),
        "operating_sloan_pct": round(operating_sloan * 100.0, 2),
        "total_accruals": round(total_accruals, 2) if total_accruals is not None else None,
        "total_sloan_ratio": round(total_sloan, 4) if total_sloan is not None else None,
        "total_sloan_pct": round(total_sloan * 100.0, 2) if total_sloan is not None else None,
        "flag_gt_10pct": operating_sloan > 0.10,
        "signal": signal,
    }


def compute_cfo_to_ni(cfo: float, net_income: float) -> dict[str, Any]:
    """Compute CFO / Net Income cash conversion ratio and diagnostic status."""
    cfo_f = float(cfo)
    ni_f = float(net_income)
    if abs(ni_f) < 1e-9:
        return {
            "cfo": round(cfo_f, 2),
            "net_income": round(ni_f, 2),
            "cfo_to_ni_ratio": None,
            "cfo_to_ni_pct": None,
            "flag_weak_conversion": cfo_f <= 0,
            "signal": "zero_net_income",
        }

    if ni_f < 0:
        # Negative net income: ratio sign is misleading unless interpreted explicitly
        return {
            "cfo": round(cfo_f, 2),
            "net_income": round(ni_f, 2),
            "cfo_to_ni_ratio": round(cfo_f / ni_f, 4),
            "cfo_to_ni_pct": round((cfo_f / ni_f) * 100.0, 2),
            "flag_weak_conversion": cfo_f < 0,
            "signal": "positive_cfo_despite_loss" if cfo_f > 0 else "cash_burn_and_loss",
        }

    ratio = cfo_f / ni_f
    if ratio >= 1.0:
        signal = "strong_cash_conversion"
    elif ratio >= 0.80:
        signal = "acceptable_cash_conversion"
    elif ratio > 0.0:
        signal = "weak_cash_conversion_below_80pct"
    else:
        signal = "divergence_positive_ni_negative_cfo"

    return {
        "cfo": round(cfo_f, 2),
        "net_income": round(ni_f, 2),
        "cfo_to_ni_ratio": round(ratio, 4),
        "cfo_to_ni_pct": round(ratio * 100.0, 2),
        "flag_weak_conversion": ratio < 0.80,
        "signal": signal,
    }


def compute_ccc(
    *,
    dio: float | None = None,
    dso: float | None = None,
    dpo: float | None = None,
    inventory: float | None = None,
    accounts_receivable: float | None = None,
    accounts_payable: float | None = None,
    revenue: float | None = None,
    cogs: float | None = None,
    days_in_period: float = 365.0,
    prior_ccc_days: float | None = None,
) -> dict[str, Any]:
    """Compute Cash Conversion Cycle (CCC = DIO + DSO - DPO) in days."""
    if dio is None:
        if inventory is not None and cogs is not None and cogs > 0:
            dio = (float(inventory) / float(cogs)) * float(days_in_period)
        else:
            dio = 0.0

    if dso is None:
        if accounts_receivable is not None and revenue is not None and revenue > 0:
            dso = (float(accounts_receivable) / float(revenue)) * float(days_in_period)
        else:
            dso = 0.0

    if dpo is None:
        if accounts_payable is not None and cogs is not None and cogs > 0:
            dpo = (float(accounts_payable) / float(cogs)) * float(days_in_period)
        else:
            dpo = 0.0

    dio_val = round(float(dio), 2)
    dso_val = round(float(dso), 2)
    dpo_val = round(float(dpo), 2)
    ccc_days = round(dio_val + dso_val - dpo_val, 2)

    yoy_change_days: float | None = None
    flag_working_capital_deterioration = False
    if prior_ccc_days is not None:
        yoy_change_days = round(ccc_days - float(prior_ccc_days), 2)
        flag_working_capital_deterioration = yoy_change_days > 15.0

    return {
        "dio_days": dio_val,
        "dso_days": dso_val,
        "dpo_days": dpo_val,
        "ccc_days": ccc_days,
        "prior_ccc_days": round(float(prior_ccc_days), 2) if prior_ccc_days is not None else None,
        "yoy_change_days": yoy_change_days,
        "flag_working_capital_deterioration": flag_working_capital_deterioration,
    }


def compute_earnings_quality(
    *,
    net_income: float,
    cfo: float,
    avg_total_assets: float,
    cfi: float | None = None,
    dio: float | None = None,
    dso: float | None = None,
    dpo: float | None = None,
    inventory: float | None = None,
    accounts_receivable: float | None = None,
    accounts_payable: float | None = None,
    revenue: float | None = None,
    cogs: float | None = None,
    days_in_period: float = 365.0,
    prior_ccc_days: float | None = None,
) -> dict[str, Any]:
    """Compute unified Stage 0 Earnings Quality metrics (Sloan, CFO/NI, CCC)."""
    sloan = compute_sloan_ratio(net_income, cfo, avg_total_assets, cfi=cfi)
    cfo_ni = compute_cfo_to_ni(cfo, net_income)
    ccc = compute_ccc(
        dio=dio,
        dso=dso,
        dpo=dpo,
        inventory=inventory,
        accounts_receivable=accounts_receivable,
        accounts_payable=accounts_payable,
        revenue=revenue,
        cogs=cogs,
        days_in_period=days_in_period,
        prior_ccc_days=prior_ccc_days,
    )

    warnings: list[str] = []
    if sloan["flag_gt_10pct"]:
        warnings.append(
            f"High Sloan accrual ratio ({sloan['operating_sloan_pct']:.1f}% > 10%): earnings heavily driven by non-cash accruals"
        )
    if cfo_ni["flag_weak_conversion"]:
        warnings.append(
            f"Weak CFO/NI cash conversion ({cfo_ni['cfo_to_ni_pct']}% < 80% threshold)"
            if cfo_ni["cfo_to_ni_pct"] is not None
            else "Weak operating cash flow conversion"
        )
    if ccc["flag_working_capital_deterioration"]:
        warnings.append(
            f"CCC lengthened by +{ccc['yoy_change_days']:.1f} days (>15d threshold); check AR collection or inventory buildup"
        )

    if len(warnings) >= 2 or sloan["signal"] == "severe_accrual_warning":
        overall_quality = "weak"
    elif len(warnings) == 1:
        overall_quality = "moderate"
    else:
        overall_quality = "strong"

    return {
        "sloan": sloan,
        "cfo_to_ni": cfo_ni,
        "ccc": ccc,
        "overall_quality": overall_quality,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute Stage 0 Earnings Quality metrics: Sloan ratio, CFO/NI, and CCC."
    )
    parser.add_argument("--net-income", type=float, required=True, help="Net income")
    parser.add_argument("--cfo", type=float, required=True, help="Operating cash flow (CFO)")
    parser.add_argument("--avg-assets", type=float, required=True, help="Average total assets")
    parser.add_argument("--cfi", type=float, default=None, help="Investing cash flow (CFI, optional)")
    parser.add_argument("--dio", type=float, default=None, help="Days Inventory Outstanding (if precomputed)")
    parser.add_argument("--dso", type=float, default=None, help="Days Sales Outstanding (if precomputed)")
    parser.add_argument("--dpo", type=float, default=None, help="Days Payable Outstanding (if precomputed)")
    parser.add_argument("--inventory", type=float, default=None, help="Average inventory")
    parser.add_argument("--ar", type=float, default=None, help="Average accounts receivable")
    parser.add_argument("--ap", type=float, default=None, help="Average accounts payable")
    parser.add_argument("--revenue", type=float, default=None, help="Revenue")
    parser.add_argument("--cogs", type=float, default=None, help="Cost of goods sold (COGS)")
    parser.add_argument("--days", type=float, default=365.0, help="Days in period (default 365)")
    parser.add_argument("--prior-ccc", type=float, default=None, help="Prior period CCC in days (optional)")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    result = compute_earnings_quality(
        net_income=args.net_income,
        cfo=args.cfo,
        avg_total_assets=args.avg_assets,
        cfi=args.cfi,
        dio=args.dio,
        dso=args.dso,
        dpo=args.dpo,
        inventory=args.inventory,
        accounts_receivable=args.ar,
        accounts_payable=args.ap,
        revenue=args.revenue,
        cogs=args.cogs,
        days_in_period=args.days,
        prior_ccc_days=args.prior_ccc,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
