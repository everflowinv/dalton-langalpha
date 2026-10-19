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
    minus included Non-Free Cash Deductions (Restricted Cash, Pledged Deposits, Customer Escrow, Trapped Cash).
- Implied Operating Assets (隐含经营资产):
    Market Cap - Net Cash - Hidden Assets (After Haircut)
    Computed under both Definition A (Basic) and Definition B (Strict).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

RESTRICTED_CASH_ALIASES = {
    "restricted_cash",
    "restricted cash",
    "restricted",
    "受限现金",
    "受限资金",
}


def _normalize_deduction_item(
    item: dict[str, Any],
    default_name: str,
    fx_rate_to_usd: float,
) -> dict[str, Any]:
    inc = bool(item.get("include", True))
    reason = str(item.get("reason", "")).strip()
    name = str(item.get("item", default_name)).strip() or default_name

    raw_local = item.get("value_local", item.get("amount"))
    raw_usd = item.get("value_usd", item.get("amount_usd"))

    if raw_local is not None:
        val_local = round(float(raw_local), 2)
        val_usd = (
            round(float(raw_usd), 2)
            if raw_usd is not None
            else round(val_local * fx_rate_to_usd, 2)
        )
    elif raw_usd is not None:
        val_usd = round(float(raw_usd), 2)
        val_local = round(val_usd / fx_rate_to_usd, 2)
    else:
        val_local = 0.0
        val_usd = 0.0

    return {
        "item": name,
        "value_local": val_local,
        "value_usd": val_usd,
        "amount_usd": val_usd,
        "include": inc,
        "reason": reason,
    }


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
    trapped_cash_items: list[dict[str, Any]] | None = None,
    deduct_restricted_cash_in_strict: bool = True,
    include_restricted_cash_in_free: bool = False,
    restricted_cash_free_reason: str = "",
    current_assets: float | None = None,
    current_liabilities: float | None = None,
    other_payables: float | None = None,
    other_receivables: float | None = None,
    market_cap_usd_mn: float | None = None,
    market_cap_usd: float | None = None,
    hidden_assets_gross_usd: float = 0.0,
    hidden_assets_after_haircut_usd: float = 0.0,
    fx_rate_to_usd: float = 1.0,
) -> dict[str, Any]:
    """Compute Basic Net Cash (Def A), Strict Net Cash (Def B), and Implied Operating Assets in USD."""
    if fx_rate_to_usd <= 0:
        raise ValueError(f"fx_rate_to_usd must be positive, got {fx_rate_to_usd}")

    if include_restricted_cash_in_free and not str(restricted_cash_free_reason).strip():
        raise ValueError(
            "include_restricted_cash_in_free=True requires a non-empty restricted_cash_free_reason"
        )

    def to_usd(val: float) -> float:
        return round(float(val) * fx_rate_to_usd, 2)

    cash_local = round(float(cash_and_equivalents), 2)
    restricted_local = round(float(restricted_cash), 2)
    deposits_local = round(float(time_deposits), 2)
    st_inv_local = round(float(short_term_investments), 2)

    cash_usd = to_usd(cash_local)
    restricted_usd = to_usd(restricted_local)
    deposits_usd = to_usd(deposits_local)
    st_inv_usd = to_usd(st_inv_local)
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
        entry = _normalize_deduction_item(item, "quasi_debt", fx_rate_to_usd)
        normalized_quasi.append(entry)
        if entry["include"]:
            quasi_deduction_usd = round(quasi_deduction_usd + entry["value_usd"], 2)

    combined_non_free_input: list[dict[str, Any]] = []
    if non_free_cash_items:
        combined_non_free_input.extend(non_free_cash_items)
    if trapped_cash_items:
        combined_non_free_input.extend(trapped_cash_items)

    normalized_non_free: list[dict[str, Any]] = []
    has_explicit_restricted_entry = False
    for item in combined_non_free_input:
        entry = _normalize_deduction_item(item, "restricted_or_trapped_cash", fx_rate_to_usd)
        if entry["item"].lower() in RESTRICTED_CASH_ALIASES:
            has_explicit_restricted_entry = True
            if not entry["include"] and not entry["reason"]:
                raise ValueError(
                    "Excluding restricted_cash from strict net cash requires a non-empty reason"
                )
        normalized_non_free.append(entry)

    # Always deduct restricted_cash in strict net cash even when custom non_free_cash_items are passed,
    # unless explicitly overridden with a reason.
    if restricted_usd > 0 and not has_explicit_restricted_entry:
        should_deduct_restricted = (
            deduct_restricted_cash_in_strict and not include_restricted_cash_in_free
        )
        restricted_reason = (
            str(restricted_cash_free_reason).strip()
            if include_restricted_cash_in_free
            else "Default strict deduction of restricted/pledged cash"
        )
        restricted_entry = {
            "item": "restricted_cash",
            "value_local": restricted_local,
            "value_usd": restricted_usd,
            "amount_usd": restricted_usd,
            "include": should_deduct_restricted,
            "reason": restricted_reason,
        }
        normalized_non_free.insert(0, restricted_entry)

    non_free_deduction_usd = round(
        sum(e["value_usd"] for e in normalized_non_free if e["include"]), 2
    )

    strict_net_cash_usd = round(
        basic_net_cash_usd - quasi_deduction_usd - non_free_deduction_usd, 2
    )

    # Resolve market cap and ensure unit scale alignment with balance sheet figures
    raw_mcap = market_cap_usd_mn if market_cap_usd_mn is not None else market_cap_usd
    resolved_mcap_same_unit: float | None = None
    resolved_mcap_usd_mn: float | None = None
    bs_scale_ref = max(abs(gross_cash_usd), abs(interest_bearing_debt_usd))

    if raw_mcap is not None and float(raw_mcap) > 0:
        mcap_val = float(raw_mcap)
        if mcap_val >= 1e8 and 0 < bs_scale_ref < 1e7:
            # Market cap was passed in raw USD while balance sheet is in USD millions
            resolved_mcap_same_unit = round(mcap_val / 1e6, 2)
            resolved_mcap_usd_mn = resolved_mcap_same_unit
        elif mcap_val < 1e7 and bs_scale_ref >= 1e8:
            # Market cap was passed in USD millions while balance sheet is in raw USD
            resolved_mcap_usd_mn = round(mcap_val, 2)
            resolved_mcap_same_unit = round(mcap_val * 1e6, 2)
        else:
            resolved_mcap_same_unit = round(mcap_val, 2)
            resolved_mcap_usd_mn = (
                round(mcap_val / 1e6, 2) if mcap_val >= 1e8 else round(mcap_val, 2)
            )

    basic_to_mcap_pct: float | None = None
    strict_to_mcap_pct: float | None = None
    hidden_gross_usd = round(float(hidden_assets_gross_usd), 2)
    hidden_after_usd = round(float(hidden_assets_after_haircut_usd), 2)
    hidden_to_mcap_pct: float | None = None
    implied_op_basic_usd: float | None = None
    implied_op_basic_pct: float | None = None
    implied_op_strict_usd: float | None = None
    implied_op_strict_pct: float | None = None

    if resolved_mcap_same_unit is not None and resolved_mcap_same_unit > 0:
        mcap = resolved_mcap_same_unit
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

    # Large deposit + large loan ("大存大贷") check (aligned with netcash.md §3.1: debt/cash >= 30%)
    large_deposit_loan_flag = bool(
        gross_cash_usd > 0
        and interest_bearing_debt_usd > 0
        and (interest_bearing_debt_usd / gross_cash_usd >= 0.30)
        and (
            resolved_mcap_same_unit is None
            or (
                resolved_mcap_same_unit > 0
                and gross_cash_usd / resolved_mcap_same_unit >= 0.15
                and interest_bearing_debt_usd / resolved_mcap_same_unit >= 0.10
            )
        )
    )
    if large_deposit_loan_flag:
        warnings.append(
            "Co-existence of high cash and high interest-bearing debt (大存大贷); verify cash authenticity and interest yield vs debt cost"
        )

    # Cash quality classification
    total_strict_gap = round(quasi_deduction_usd + non_free_deduction_usd, 2)
    if large_deposit_loan_flag or (
        gross_cash_usd > 0 and total_strict_gap / gross_cash_usd > 0.35
    ):
        cash_quality = "complex"
    elif total_strict_gap > 0 or warnings:
        cash_quality = "moderate"
    else:
        cash_quality = "clean"

    return {
        "market_cap_usd_mn": resolved_mcap_usd_mn,
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
        "basic": {
            "definition": "Definition A (Basic Net Cash)",
            "cash_side_usd": gross_cash_usd,
            "debt_side_usd": interest_bearing_debt_usd,
            "net_cash_usd": basic_net_cash_usd,
            "net_cash_to_market_cap_pct": basic_to_mcap_pct,
        },
        "strict": {
            "definition": "Definition B (Strict Net Cash)",
            "basic_net_cash_usd": basic_net_cash_usd,
            "quasi_debt_deductions_usd": quasi_deduction_usd,
            "non_free_cash_deductions_usd": non_free_deduction_usd,
            "total_deductions_usd": total_strict_gap,
            "net_cash_usd": strict_net_cash_usd,
            "net_cash_to_market_cap_pct": strict_to_mcap_pct,
            "large_deposit_loan_flag": large_deposit_loan_flag,
            "lease_liabilities_usd": lease_liabilities_usd,
        },
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
        "fx_rate_to_usd": fx_rate_to_usd,
        "anchors_source": "manual",
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
    return {
        "item": name,
        "value_local": amount,
        "amount": amount,
        "include": include,
        "reason": reason,
    }


def _load_input_payload(raw_input: str) -> dict[str, Any]:
    s = raw_input.strip()
    if s == "-":
        data = json.load(sys.stdin)
    elif s.startswith("{"):
        data = json.loads(s)
    else:
        data = json.loads(Path(s).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("--input JSON must be an object")
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
        "market_cap_usd_mn": mcap_mn_val,
    }


def _coerce_items_list(val: Any) -> list[dict[str, Any]] | None:
    if val is None:
        return None
    if not isinstance(val, list):
        raise ValueError(f"Deduction items must be a list, got {type(val).__name__}")
    out: list[dict[str, Any]] = []
    for elem in val:
        if isinstance(elem, str):
            out.append(_parse_deduction_arg(elem))
        elif isinstance(elem, dict):
            out.append(dict(elem))
        else:
            raise ValueError(f"Invalid deduction item: {elem!r}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute Basic Net Cash (Def A), Strict Net Cash (Def B), and Implied Operating Assets."
    )
    parser.add_argument(
        "--input",
        "-i",
        default=None,
        help="Path to JSON input file, '-' for stdin, or inline JSON object string",
    )
    parser.add_argument(
        "--anchors",
        default=None,
        help="Path to anchors.py JSON output file, '-' for stdin, or inline JSON object string",
    )
    parser.add_argument("--cash", type=float, default=None, help="Cash and cash equivalents")
    parser.add_argument("--restricted-cash", type=float, default=None, help="Restricted / pledged cash")
    parser.add_argument("--time-deposits", type=float, default=None, help="Time deposits (>3M) & CDs")
    parser.add_argument("--st-investments", type=float, default=None, help="Short-term liquid MMF / T-bills / WM FVPL")
    parser.add_argument("--st-debt", type=float, default=None, help="Short-term borrowings")
    parser.add_argument("--cur-lt-debt", type=float, default=None, help="Current portion of long-term debt")
    parser.add_argument("--lt-debt", type=float, default=None, help="Long-term debt & bonds")
    parser.add_argument("--convertible-debt", type=float, default=None, help="Convertible bonds (debt host)")
    parser.add_argument("--lease-liabilities", type=float, default=None, help="Lease liabilities (disclosed, not deducted)")
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
    parser.add_argument(
        "--include-restricted-cash-in-free",
        action="store_true",
        help="Do not deduct restricted cash in strict net cash (requires --restricted-cash-free-reason)",
    )
    parser.add_argument(
        "--restricted-cash-free-reason",
        default="",
        help="Reason for keeping restricted cash in strict net cash",
    )
    parser.add_argument("--current-assets", type=float, default=None, help="Total current assets")
    parser.add_argument("--current-liabilities", type=float, default=None, help="Total current liabilities")
    parser.add_argument("--other-payables", type=float, default=None, help="Other payables & accruals")
    parser.add_argument("--other-receivables", type=float, default=None, help="Other receivables")
    parser.add_argument(
        "--market-cap-usd-mn",
        type=float,
        default=None,
        help="Market cap in USD millions (manual override; mutually exclusive with --anchors)",
    )
    parser.add_argument(
        "--market-cap-usd",
        type=float,
        default=None,
        help="Market cap in USD (same unit scale or millions; mutually exclusive with --anchors)",
    )
    parser.add_argument("--hidden-assets-gross-usd", type=float, default=None, help="Pre-haircut hidden assets in USD")
    parser.add_argument("--hidden-assets-after-usd", type=float, default=None, help="Post-haircut hidden assets in USD")
    parser.add_argument("--fx-rate", type=float, default=None, help="Reporting currency -> USD FX rate (manual override; mutually exclusive with --anchors)")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    try:
        payload: dict[str, Any] = _load_input_payload(args.input) if args.input else {}
        anchors_obj: dict[str, Any] | None = None
        if args.anchors:
            anchors_obj = _load_input_payload(args.anchors)
        elif isinstance(payload.get("anchors"), dict):
            anchors_obj = payload["anchors"]

        has_manual_cli_or_payload = (
            args.market_cap_usd_mn is not None
            or args.market_cap_usd is not None
            or args.fx_rate is not None
            or payload.get("market_cap_usd_mn") is not None
            or payload.get("market_cap_usd") is not None
            or payload.get("fx_rate") is not None
            or payload.get("fx_rate_to_usd") is not None
        )
        if anchors_obj is not None and has_manual_cli_or_payload:
            raise ValueError(
                "Cannot specify both --anchors and manual --market-cap-usd-mn/--market-cap-usd/--fx-rate"
            )

        def _pick_float(cli_val: float | None, *keys: str, default: float | None = None) -> float | None:
            if cli_val is not None:
                return float(cli_val)
            for k in keys:
                if k in payload and payload[k] is not None:
                    return float(payload[k])
            return default

        anchor_meta = _extract_anchor_fields(anchors_obj) if anchors_obj is not None else {}
        fx_default = anchor_meta.get("fx_rate_to_usd") or 1.0
        fx_rate_val = _pick_float(args.fx_rate, "fx_rate_to_usd", "fx_rate", default=fx_default) or 1.0

        mcap_mn_default = anchor_meta.get("market_cap_usd_mn")
        mcap_usd_mn_val = _pick_float(
            args.market_cap_usd_mn,
            "market_cap_usd_mn",
            default=float(mcap_mn_default) if mcap_mn_default is not None else None,
        )
        mcap_usd_val = _pick_float(args.market_cap_usd, "market_cap_usd", default=None)

        quasi_from_payload = _coerce_items_list(
            payload.get("quasi_debt_items", payload.get("quasi_debt"))
        )
        non_free_from_payload = _coerce_items_list(
            payload.get("non_free_cash_items", payload.get("non_free_cash"))
        )
        trapped_from_payload = _coerce_items_list(payload.get("trapped_cash_items"))

        quasi_items = (quasi_from_payload or []) + [_parse_deduction_arg(x) for x in args.quasi_debt]
        non_free_cli = [_parse_deduction_arg(x) for x in args.non_free_cash]
        if non_free_from_payload is not None or non_free_cli:
            non_free_items: list[dict[str, Any]] | None = (non_free_from_payload or []) + non_free_cli
        else:
            non_free_items = None

        inc_restricted_free = bool(
            args.include_restricted_cash_in_free
            or payload.get("include_restricted_cash_in_free", False)
        )
        restricted_free_reason = (
            args.restricted_cash_free_reason
            or str(payload.get("restricted_cash_free_reason", ""))
        )

        result = compute_netcash(
            cash_and_equivalents=_pick_float(args.cash, "cash_and_equivalents", "cash", default=0.0) or 0.0,
            restricted_cash=_pick_float(args.restricted_cash, "restricted_cash", default=0.0) or 0.0,
            time_deposits=_pick_float(args.time_deposits, "time_deposits", default=0.0) or 0.0,
            short_term_investments=_pick_float(
                args.st_investments, "short_term_investments", "st_investments", default=0.0
            ) or 0.0,
            short_term_debt=_pick_float(args.st_debt, "short_term_debt", "st_debt", default=0.0) or 0.0,
            current_long_term_debt=_pick_float(
                args.cur_lt_debt, "current_long_term_debt", "cur_lt_debt", default=0.0
            ) or 0.0,
            long_term_debt=_pick_float(args.lt_debt, "long_term_debt", "lt_debt", default=0.0) or 0.0,
            convertible_debt=_pick_float(args.convertible_debt, "convertible_debt", default=0.0) or 0.0,
            lease_liabilities=_pick_float(args.lease_liabilities, "lease_liabilities", default=0.0) or 0.0,
            quasi_debt_items=quasi_items,
            non_free_cash_items=non_free_items,
            trapped_cash_items=trapped_from_payload,
            deduct_restricted_cash_in_strict=bool(
                payload.get("deduct_restricted_cash_in_strict", True)
            ),
            include_restricted_cash_in_free=inc_restricted_free,
            restricted_cash_free_reason=restricted_free_reason,
            current_assets=_pick_float(args.current_assets, "current_assets", default=None),
            current_liabilities=_pick_float(args.current_liabilities, "current_liabilities", default=None),
            other_payables=_pick_float(args.other_payables, "other_payables", default=None),
            other_receivables=_pick_float(args.other_receivables, "other_receivables", default=None),
            market_cap_usd_mn=mcap_usd_mn_val,
            market_cap_usd=mcap_usd_val,
            hidden_assets_gross_usd=_pick_float(
                args.hidden_assets_gross_usd, "hidden_assets_gross_usd", default=0.0
            ) or 0.0,
            hidden_assets_after_haircut_usd=_pick_float(
                args.hidden_assets_after_usd,
                "hidden_assets_after_haircut_usd",
                "hidden_assets_after_usd",
                default=0.0,
            ) or 0.0,
            fx_rate_to_usd=fx_rate_val,
        )
        if anchors_obj is not None:
            result["anchors_source"] = "anchors"
            result["ticker"] = anchor_meta.get("ticker")
            result["period_end"] = anchor_meta.get("period_end")
            result["fx_rate_to_usd"] = fx_rate_val
            result["anchors"] = {
                "ticker": anchor_meta.get("ticker"),
                "period_end": anchor_meta.get("period_end"),
                "as_of": anchor_meta.get("as_of"),
                "market_cap_usd_mn": result["market_cap_usd_mn"],
                "fx_rate_to_usd": fx_rate_val,
            }
        else:
            result["anchors_source"] = "manual"
            result["fx_rate_to_usd"] = fx_rate_val
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
