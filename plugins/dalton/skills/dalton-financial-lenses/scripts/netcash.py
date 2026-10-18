#!/usr/bin/env python3
"""Deterministic Net Cash (Definition A Basic & Definition B Strict) and Implied Operating Assets calculator (PS.2).

Definitions:
- Definition A (Basic Net Cash / 基础净现金):
    Gross Cash (Cash & Equivalents + Restricted Cash + Time Deposits + Short-Term Liquid MMF/Treasury/WM FVPL)
    minus Interest-Bearing Debt (Short-Term Debt + Current Portion of LT Debt + Long-Term Debt + Bonds/Convertibles,
    excluding IFRS 16 / ASC 842 Lease Liabilities which are disclosed separately).
- Definition B (Strict Net Cash / 严格净现金):
    Basic Net Cash minus included Quasi-Debt Deductions (Customer Advances / Contract Liabilities,
    Non-operating Other Payables, Long-Term Payables, Redeemable Preferreds / Puttable Minorities)
    minus included Non-Free Cash Deductions (Pledged/Restricted Cash, Customer Escrow, Trapped Cash).
- Implied Operating Assets (隐含经营资产):
    Market Cap - Net Cash - Hidden Assets (After Haircut)
    Computed under both Definition A (Basic) and Definition B (Strict).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def compute_netcash(
    *,
    cash_and_equivalents: float = 0.0,
    restricted_cash: float = 0.0,
    time_deposits: float = 0.0,
    short_term_investments: float = 0.0,
    short_term_debt: float = 0.0,
    current_long_term_debt: float = 0.0,
    long_term_debt: float = 0.0,
    convertible_debt: float = 0.0,
    lease_liabilities: float = 0.0,
    quasi_debt_items: list[dict[str, Any]] | None = None,
    non_free_cash_items: list[dict[str, Any]] | None = None,
    deduct_restricted_cash_in_strict: bool = True,
    current_assets: float | None = None,
    current_liabilities: float | None = None,
    other_payables: float | None = None,
    other_receivables: float | None = None,
    market_cap_usd: float | None = None,
    hidden_assets_gross_usd: float = 0.0,
    hidden_assets_after_haircut_usd: float = 0.0,
    fx_rate_to_usd: float = 1.0,
) -> dict[str, Any]:
    """Compute Basic Net Cash (Def A), Strict Net Cash (Def B), and Implied Operating Assets in USD."""
    if fx_rate_to_usd <= 0:
        raise ValueError(f"fx_rate_to_usd must be positive, got {fx_rate_to_usd}")

    def to_usd(val: float) -> float:
        return round(float(val) * fx_rate_to_usd, 2)

    cash_usd = to_usd(cash_and_equivalents)
    restricted_usd = to_usd(restricted_cash)
    deposits_usd = to_usd(time_deposits)
    st_inv_usd = to_usd(short_term_investments)
    gross_cash_usd = round(cash_usd + restricted_usd + deposits_usd + st_inv_usd, 2)

    st_debt_usd = to_usd(short_term_debt)
    cur_lt_debt_usd = to_usd(current_long_term_debt)
    lt_debt_usd = to_usd(long_term_debt)
    conv_debt_usd = to_usd(convertible_debt)
    interest_bearing_debt_usd = round(
        st_debt_usd + cur_lt_debt_usd + lt_debt_usd + conv_debt_usd, 2
    )
    lease_liabilities_usd = to_usd(lease_liabilities)

    # Definition A: Basic Net Cash
    basic_net_cash_usd = round(gross_cash_usd - interest_bearing_debt_usd, 2)

    # Definition B: Strict Net Cash deductions
    normalized_quasi: list[dict[str, Any]] = []
    quasi_deduction_usd = 0.0
    for item in quasi_debt_items or []:
        inc = bool(item.get("include", True))
        amt_usd = (
            round(float(item["amount_usd"]), 2)
            if "amount_usd" in item
            else to_usd(float(item.get("amount", 0.0)))
        )
        entry = {
            "item": str(item.get("item", "quasi_debt")),
            "amount_usd": amt_usd,
            "include": inc,
            "reason": str(item.get("reason", "")),
        }
        normalized_quasi.append(entry)
        if inc:
            quasi_deduction_usd = round(quasi_deduction_usd + amt_usd, 2)

    normalized_non_free: list[dict[str, Any]] = []
    non_free_deduction_usd = 0.0
    if non_free_cash_items is not None:
        for item in non_free_cash_items:
            inc = bool(item.get("include", True))
            amt_usd = (
                round(float(item["amount_usd"]), 2)
                if "amount_usd" in item
                else to_usd(float(item.get("amount", 0.0)))
            )
            entry = {
                "item": str(item.get("item", "restricted_or_trapped_cash")),
                "amount_usd": amt_usd,
                "include": inc,
                "reason": str(item.get("reason", "")),
            }
            normalized_non_free.append(entry)
            if inc:
                non_free_deduction_usd = round(non_free_deduction_usd + amt_usd, 2)
    elif deduct_restricted_cash_in_strict and restricted_usd > 0:
        normalized_non_free.append(
            {
                "item": "restricted_cash",
                "amount_usd": restricted_usd,
                "include": True,
                "reason": "Default strict deduction of restricted/pledged cash",
            }
        )
        non_free_deduction_usd = restricted_usd

    strict_net_cash_usd = round(
        basic_net_cash_usd - quasi_deduction_usd - non_free_deduction_usd, 2
    )

    # Ratios vs Market Cap & Implied Operating Assets
    basic_to_mcap_pct: float | None = None
    strict_to_mcap_pct: float | None = None
    hidden_gross_usd = round(float(hidden_assets_gross_usd), 2)
    hidden_after_usd = round(float(hidden_assets_after_haircut_usd), 2)
    hidden_to_mcap_pct: float | None = None
    implied_op_basic_usd: float | None = None
    implied_op_basic_pct: float | None = None
    implied_op_strict_usd: float | None = None
    implied_op_strict_pct: float | None = None

    if market_cap_usd is not None and market_cap_usd > 0:
        mcap = float(market_cap_usd)
        basic_to_mcap_pct = round((basic_net_cash_usd / mcap) * 100.0, 2)
        strict_to_mcap_pct = round((strict_net_cash_usd / mcap) * 100.0, 2)
        hidden_to_mcap_pct = round((hidden_after_usd / mcap) * 100.0, 2)
        implied_op_basic_usd = round(mcap - basic_net_cash_usd - hidden_after_usd, 2)
        implied_op_basic_pct = round((implied_op_basic_usd / mcap) * 100.0, 2)
        implied_op_strict_usd = round(mcap - strict_net_cash_usd - hidden_after_usd, 2)
        implied_op_strict_pct = round((implied_op_strict_usd / mcap) * 100.0, 2)

    # Working capital & balance sheet quality warnings
    wc_usd: float | None = None
    other_payable_ratio: float | None = None
    other_receivable_ratio: float | None = None
    warnings: list[str] = []

    if current_assets is not None and current_liabilities is not None:
        ca_usd = to_usd(current_assets)
        cl_usd = to_usd(current_liabilities)
        wc_usd = round(ca_usd - cl_usd, 2)
        if other_payables is not None and cl_usd > 0:
            op_usd = to_usd(other_payables)
            other_payable_ratio = round(op_usd / cl_usd, 4)
            if other_payable_ratio > 0.20:
                warnings.append(
                    f"Other payables / Current liabilities = {other_payable_ratio * 100:.1f}% (>20% threshold); inspect footnote for quasi-debt"
                )
        if other_receivables is not None and ca_usd > 0:
            or_usd = to_usd(other_receivables)
            other_receivable_ratio = round(or_usd / ca_usd, 4)
            if other_receivable_ratio > 0.20:
                warnings.append(
                    f"Other receivables / Current assets = {other_receivable_ratio * 100:.1f}% (>20% threshold); inspect footnote for related-party fund occupation"
                )

    # Large deposit + large loan ("大存大贷") check
    large_deposit_loan_flag = bool(
        gross_cash_usd > 0
        and interest_bearing_debt_usd > 0
        and (interest_bearing_debt_usd / gross_cash_usd >= 0.50)
        and (
            market_cap_usd is None
            or (
                market_cap_usd > 0
                and gross_cash_usd / market_cap_usd >= 0.15
                and interest_bearing_debt_usd / market_cap_usd >= 0.10
            )
        )
    )
    if large_deposit_loan_flag:
        warnings.append(
            "Co-existence of high cash and high interest-bearing debt (大存大贷); verify cash authenticity and interest yield vs debt cost"
        )

    # Cash quality classification
    total_strict_gap = quasi_deduction_usd + non_free_deduction_usd
    if large_deposit_loan_flag or (
        gross_cash_usd > 0 and total_strict_gap / gross_cash_usd > 0.35
    ):
        cash_quality = "complex"
    elif total_strict_gap > 0 or warnings:
        cash_quality = "moderate"
    else:
        cash_quality = "clean"

    return {
        "gross_cash_usd": gross_cash_usd,
        "gross_cash_breakdown_usd": {
            "cash_and_equivalents_usd": cash_usd,
            "restricted_cash_usd": restricted_usd,
            "time_deposits_usd": deposits_usd,
            "short_term_investments_usd": st_inv_usd,
        },
        "interest_bearing_debt_usd": interest_bearing_debt_usd,
        "debt_breakdown_usd": {
            "short_term_debt_usd": st_debt_usd,
            "current_long_term_debt_usd": cur_lt_debt_usd,
            "long_term_debt_usd": lt_debt_usd,
            "convertible_debt_usd": conv_debt_usd,
        },
        "lease_liabilities_usd": lease_liabilities_usd,
        "basic_net_cash_usd": basic_net_cash_usd,
        "basic_net_cash_to_mcap_pct": basic_to_mcap_pct,
        "quasi_debt_deductions_usd": quasi_deduction_usd,
        "quasi_debt_items": normalized_quasi,
        "non_free_cash_deductions_usd": non_free_deduction_usd,
        "non_free_cash_items": normalized_non_free,
        "strict_net_cash_usd": strict_net_cash_usd,
        "strict_net_cash_to_mcap_pct": strict_to_mcap_pct,
        "hidden_assets": {
            "gross_usd": hidden_gross_usd,
            "after_haircut_usd": hidden_after_usd,
            "after_haircut_to_mcap_pct": hidden_to_mcap_pct,
        },
        "implied_operating_assets": {
            "basic_usd": implied_op_basic_usd,
            "basic_to_mcap_pct": implied_op_basic_pct,
            "strict_usd": implied_op_strict_usd,
            "strict_to_mcap_pct": implied_op_strict_pct,
            "negative_ev_basic": (
                implied_op_basic_usd <= 0 if implied_op_basic_usd is not None else None
            ),
            "negative_ev_strict": (
                implied_op_strict_usd <= 0 if implied_op_strict_usd is not None else None
            ),
        },
        "working_capital": {
            "working_capital_usd": wc_usd,
            "other_payable_to_current_liabilities": other_payable_ratio,
            "other_receivable_to_current_assets": other_receivable_ratio,
        },
        "large_deposit_loan_flag": large_deposit_loan_flag,
        "cash_quality": cash_quality,
        "warnings": warnings,
    }


def _parse_deduction_arg(raw: str) -> dict[str, Any]:
    """Parse 'name:amount[:include[:reason]]' CLI argument."""
    parts = raw.split(":", 3)
    if len(parts) < 2:
        raise ValueError(f"Deduction item '{raw}' must be 'name:amount[:include[:reason]]'")
    name = parts[0].strip()
    amount = float(parts[1].strip())
    include = True
    if len(parts) >= 3 and parts[2].strip():
        include = parts[2].strip().lower() not in ("false", "0", "no", "exclude")
    reason = parts[3].strip() if len(parts) >= 4 else ""
    return {"item": name, "amount": amount, "include": include, "reason": reason}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute Basic Net Cash (Def A), Strict Net Cash (Def B), and Implied Operating Assets."
    )
    parser.add_argument("--cash", type=float, default=0.0, help="Cash and cash equivalents")
    parser.add_argument("--restricted-cash", type=float, default=0.0, help="Restricted / pledged cash")
    parser.add_argument("--time-deposits", type=float, default=0.0, help="Time deposits (>3M) & CDs")
    parser.add_argument("--st-investments", type=float, default=0.0, help="Short-term liquid MMF / T-bills / WM FVPL")
    parser.add_argument("--st-debt", type=float, default=0.0, help="Short-term borrowings")
    parser.add_argument("--cur-lt-debt", type=float, default=0.0, help="Current portion of long-term debt")
    parser.add_argument("--lt-debt", type=float, default=0.0, help="Long-term debt & bonds")
    parser.add_argument("--convertible-debt", type=float, default=0.0, help="Convertible bonds (debt host)")
    parser.add_argument("--lease-liabilities", type=float, default=0.0, help="Lease liabilities (disclosed, not deducted)")
    parser.add_argument(
        "--quasi-debt",
        action="append",
        default=[],
        help="Quasi-debt item 'name:amount[:include[:reason]]', repeatable",
    )
    parser.add_argument(
        "--non-free-cash",
        action="append",
        default=[],
        help="Non-free cash item 'name:amount[:include[:reason]]', repeatable",
    )
    parser.add_argument("--current-assets", type=float, default=None, help="Total current assets")
    parser.add_argument("--current-liabilities", type=float, default=None, help="Total current liabilities")
    parser.add_argument("--other-payables", type=float, default=None, help="Other payables & accruals")
    parser.add_argument("--other-receivables", type=float, default=None, help="Other receivables")
    parser.add_argument("--market-cap-usd", type=float, default=None, help="Market cap in USD (same unit scale)")
    parser.add_argument("--hidden-assets-gross-usd", type=float, default=0.0, help="Pre-haircut hidden assets in USD")
    parser.add_argument("--hidden-assets-after-usd", type=float, default=0.0, help="Post-haircut hidden assets in USD")
    parser.add_argument("--fx-rate", type=float, default=1.0, help="Reporting currency -> USD FX rate")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    quasi_items = [_parse_deduction_arg(x) for x in args.quasi_debt]
    non_free_items = [_parse_deduction_arg(x) for x in args.non_free_cash] if args.non_free_cash else None

    result = compute_netcash(
        cash_and_equivalents=args.cash,
        restricted_cash=args.restricted_cash,
        time_deposits=args.time_deposits,
        short_term_investments=args.st_investments,
        short_term_debt=args.st_debt,
        current_long_term_debt=args.cur_lt_debt,
        long_term_debt=args.lt_debt,
        convertible_debt=args.convertible_debt,
        lease_liabilities=args.lease_liabilities,
        quasi_debt_items=quasi_items,
        non_free_cash_items=non_free_items,
        current_assets=args.current_assets,
        current_liabilities=args.current_liabilities,
        other_payables=args.other_payables,
        other_receivables=args.other_receivables,
        market_cap_usd=args.market_cap_usd,
        hidden_assets_gross_usd=args.hidden_assets_gross_usd,
        hidden_assets_after_haircut_usd=args.hidden_assets_after_usd,
        fx_rate_to_usd=args.fx_rate,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
