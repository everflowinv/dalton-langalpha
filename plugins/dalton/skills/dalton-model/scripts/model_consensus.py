#!/usr/bin/env python3
"""Isolated consensus snapshot writer and House-vs-Consensus diff generator (modeling v2 §5.2, §8.7).

Rules enforced:
- Independent modeling first, consensus comparison second.
- ``write-snapshot`` writes ONLY into the isolated consensus snapshot zone (never touches
  any House forecast cell in Model/Valuation/Key Metrics sheets) and preserves all
  existing OOXML ``<v>`` caches and ``<calcPr>``.
- ``diff`` aligns 5 accounting/reporting dimensions (FY end, currency, GAAP/Non-GAAP,
  ordinary vs ADS, reflects latest earnings), applies §5.2 / §8.7 thresholds (including
  the <3% margin breakeven ±150bp rule), binds output to live ``model_sha256``, and writes
  ``<R>/model_dump/consensus-diff.json`` + ``<R>/model_dump/consensus-diff.md``.

Usage:
    python3 model_consensus.py write-snapshot --model <model.xlsx> --consensus <consensus.json> [--sheet "vs Consensus"]
    python3 model_consensus.py diff --model <model.xlsx> --ticker <TICKER> --research-dir <R> [--consensus <consensus.json>] [--thresholds <json>]

Standalone script (stdlib + openpyxl + lxml only).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

import openpyxl

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import model_edit  # noqa: E402
import model_fingerprint  # noqa: E402
import model_style  # noqa: E402
import model_xlsx_hooks  # noqa: E402

DEFAULT_THRESHOLDS: dict[str, float] = {
    "revenue_pct": 0.05,
    "profit_eps_pct": 0.10,
    "segment_revenue_pct": 0.08,
    "opex_ratio_bp": 200.0,
    "breakeven_margin_abs": 0.03,
    "breakeven_margin_bp": 150.0,
}

_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "total_revenue": ("total revenue", "revenue", "总收入", "营业总收入", "营业收入"),
    "operating_income": ("operating income", "operating profit", "ebit", "营业利润"),
    "net_income": ("net income", "net profit", "归母净利润", "净利润"),
    "diluted_eps": ("diluted eps", "eps", "epads", "每股收益"),
    "gross_profit": ("gross profit", "毛利"),
    "operating_expenses_ratio": ("opex %", "operating expenses %", "费用率"),
}


def _normalize_metric_key(name: str) -> str:
    low = " ".join(str(name or "").strip().lower().split())
    for canon, aliases in _METRIC_ALIASES.items():
        if low == canon or any(a == low or a in low for a in aliases):
            return canon
    return low.replace(" ", "_")


def _metric_category(metric_key: str, raw_category: str | None = None) -> str:
    if raw_category:
        return str(raw_category).strip().lower()
    if metric_key == "total_revenue":
        return "revenue"
    if metric_key in ("operating_income", "net_income", "diluted_eps"):
        return "profit_eps"
    if "segment" in metric_key and "revenue" in metric_key:
        return "segment_revenue"
    if "ratio" in metric_key or "margin" in metric_key or "费用率" in metric_key:
        return "opex_ratio"
    return "profit_eps"


def _find_consensus_sheet(wb: openpyxl.Workbook, preferred: str | None = None) -> str | None:
    if preferred and preferred in wb.sheetnames:
        return preferred
    for name in wb.sheetnames:
        low = name.lower()
        if any(k in low for k in ("vs consensus", "street vs house", "consensus")):
            return name
    return None


def _is_house_sheet(sheet_name: str) -> bool:
    low = sheet_name.lower()
    return not any(k in low for k in ("vs consensus", "street vs house", "consensus"))


def write_consensus_snapshot(
    *,
    model_path: str | Path,
    consensus_data: dict[str, Any],
    sheet_name: str = "vs Consensus",
) -> dict[str, Any]:
    """Write external consensus reference figures ONLY into the isolated snapshot sheet/zone.

    Never edits any cell on House model/valuation/summary sheets. Preserves all
    pre-existing OOXML cached values ``<v>`` and ``<calcPr>``.
    """
    mpath = Path(model_path).expanduser().resolve()
    if not mpath.is_file():
        raise FileNotFoundError(f"model workbook not found: {mpath}")
    if _is_house_sheet(sheet_name):
        raise ValueError(
            f"Refusing to write consensus snapshot into non-consensus sheet {sheet_name!r}; "
            "snapshots must reside in an isolated consensus sheet/zone."
        )

    items = list(consensus_data.get("items") or consensus_data.get("estimates") or [])
    source_type = str(consensus_data.get("source_type") or consensus_data.get("source") or "Market Consensus")
    as_of_date = str(
        consensus_data.get("as_of_date")
        or consensus_data.get("fetched_at")
        or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )[:10]

    with tempfile.TemporaryDirectory(prefix="consensus_snap_") as tmp:
        donor_copy = Path(tmp) / "donor.xlsx"
        editing_copy = Path(tmp) / "editing.xlsx"
        shutil.copyfile(mpath, donor_copy)
        shutil.copyfile(mpath, editing_copy)

        wb = openpyxl.load_workbook(editing_copy, data_only=False)
        written_coords: list[str] = []
        try:
            target_sh = _find_consensus_sheet(wb, sheet_name)
            if target_sh is None:
                ws = wb.create_sheet(sheet_name)
                target_sh = sheet_name
                headers = [
                    "Period",
                    "Metric",
                    "House",
                    "Consensus Snapshot",
                    "Diff %",
                    "Source Type",
                    "As-Of Date",
                    "Rationale",
                ]
                for idx, h in enumerate(headers, start=1):
                    c = ws.cell(1, idx, h)
                    model_style.apply_cell_style(c, "header", is_left_aligned=(idx in (1, 2, 6, 7, 8)))
                ws.freeze_panes = "C2"
            else:
                ws = wb[target_sh]

            # Build existing (period, metric_key) -> row index lookup
            row_lookup: dict[tuple[str, str], int] = {}
            for r in range(2, ws.max_row + 1):
                p_val = str(ws.cell(r, 1).value or "").strip()
                m_val = str(ws.cell(r, 2).value or "").strip()
                if p_val and m_val:
                    row_lookup[(p_val.upper(), _normalize_metric_key(m_val))] = r

            for item in items:
                period = str(item.get("period") or "").strip()
                metric_label = str(item.get("metric") or item.get("label") or "").strip()
                cons_val = item.get("consensus")
                if not period or not metric_label or cons_val is None:
                    continue
                mkey = _normalize_metric_key(metric_label)
                row_idx = row_lookup.get((period.upper(), mkey))
                if row_idx is None:
                    row_idx = ws.max_row + 1
                    ws.cell(row_idx, 1, period)
                    ws.cell(row_idx, 2, metric_label)
                    if item.get("house_formula"):
                        hc = ws.cell(row_idx, 3, str(item["house_formula"]))
                        model_style.apply_cell_style(hc, "formula", is_link=True)
                    dc = ws.cell(
                        row_idx,
                        5,
                        f'=IF(OR(D{row_idx}="",D{row_idx}=0),"",(C{row_idx}-D{row_idx})/ABS(D{row_idx}))',
                    )
                    model_style.apply_cell_style(dc, "formula", is_ratio=True, is_percent=True)
                    row_lookup[(period.upper(), mkey)] = row_idx

                snap_cell = ws.cell(row_idx, 4, float(cons_val))
                model_style.apply_cell_style(snap_cell, "external_input")
                written_coords.append(f"D{row_idx}")

                src_cell = ws.cell(row_idx, 6, str(item.get("source_type") or source_type))
                model_style.apply_cell_style(src_cell, "source_note")
                dt_cell = ws.cell(row_idx, 7, str(item.get("as_of_date") or as_of_date))
                model_style.apply_cell_style(dt_cell, "source_note")
                if item.get("rationale") is not None:
                    ws.cell(row_idx, 8, str(item["rationale"]))

            wb.save(editing_copy)
        finally:
            wb.close()

        model_edit._preserve_calcpr_from_donor(editing_copy, donor_copy)
        # Restore ALL donor caches on every sheet (snapshot cells are numeric inputs, not formulas)
        spec = {
            "cache_donor_path": str(donor_copy),
            "skip_sheets": [s for s in wb.sheetnames if s != target_sh],
            "skip_cells": {},
            "touched_formula_cells": {},
        }
        model_xlsx_hooks.restore_donor_cache(editing_copy, spec)
        os.replace(editing_copy, mpath)

    return {
        "status": "ok",
        "model_path": str(mpath),
        "model_sha256": model_fingerprint.file_sha256(mpath),
        "sheet": target_sh,
        "consensus_snapshot_cells": {target_sh: sorted(set(written_coords))},
        "written_count": len(written_coords),
    }


def _extract_house_values_from_model(
    model_path: Path,
    ticker: str,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    """Read House cached values and reporting basis from ``model_path`` (read-only)."""
    fp = model_fingerprint.build_fingerprint(model_path, ticker=ticker)
    tk = ticker.strip().upper()

    # Locate target company's Model sheet
    target_sheet: str | None = None
    for s_entry in fp.get("sheet_order") or []:
        name = s_entry["name"]
        tag = s_entry.get("company_tag")
        if name.lower().startswith("model") and (tag is None or str(tag).upper() == tk):
            target_sheet = name
            break
    if target_sheet is None:
        for s_entry in fp.get("sheet_order") or []:
            if s_entry["name"].lower().startswith("model"):
                target_sheet = s_entry["name"]
                break

    house_map: dict[tuple[str, str], dict[str, Any]] = {}
    model_basis: dict[str, Any] = {
        "fiscal_year_end_month": 12,
        "unit_text": None,
        "currency": None,
        "accounting_basis": "GAAP",
        "share_basis": "ordinary",
    }

    if target_sheet and target_sheet in fp["sheets"]:
        sh_data = fp["sheets"][target_sheet]
        p_axis = sh_data.get("period_axis") or {}
        model_basis["fiscal_year_end_month"] = p_axis.get("fiscal_year_end_month", 12)
        unit_text = str(p_axis.get("unit_text") or "")
        model_basis["unit_text"] = unit_text
        if "RMB" in unit_text.upper() or "CNY" in unit_text.upper():
            model_basis["currency"] = "RMB"
        elif "HK" in unit_text.upper():
            model_basis["currency"] = "HKD"
        elif "US" in unit_text.upper() or "$" in unit_text:
            model_basis["currency"] = "USD"

        anchors = sh_data.get("semantic_anchors") or {}
        anchor_rows = {
            "total_revenue": anchors.get("total_revenue_row"),
            "gross_profit": anchors.get("gross_profit_row"),
            "operating_income": anchors.get("operating_income_row"),
            "net_income": anchors.get("net_income_row"),
            "diluted_eps": anchors.get("eps_row"),
        }
        cells = sh_data.get("cells") or {}
        cols = p_axis.get("columns") or {}

        for col_l, cinfo in cols.items():
            if cinfo.get("kind") not in ("annual", "quarter"):
                continue
            raw_p = str(cinfo.get("raw") or "").strip().upper()
            norm_p = str(cinfo.get("normalized") or raw_p).upper()
            rev_row = anchor_rows.get("total_revenue")
            rev_val = None
            if rev_row:
                rc = cells.get(f"{col_l}{rev_row}", {}).get("cached_value")
                if isinstance(rc, (int, float)):
                    rev_val = float(rc)

            for mkey, r_idx in anchor_rows.items():
                if not r_idx:
                    continue
                coord = f"{col_l}{r_idx}"
                cmeta = cells.get(coord)
                if not cmeta:
                    continue
                val = cmeta.get("cached_value")
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    margin = (
                        float(val) / rev_val
                        if (mkey in ("operating_income", "net_income") and rev_val and abs(rev_val) > 1e-9)
                        else None
                    )
                    entry = {
                        "value": float(val),
                        "sheet": target_sheet,
                        "cell": coord,
                        "margin": margin,
                        "revenue": rev_val,
                    }
                    house_map[(raw_p, mkey)] = entry
                    house_map[(norm_p, mkey)] = entry

    # Also read rationales already recorded in vs Consensus sheet
    rationales: dict[tuple[str, str], str] = {}
    wb = openpyxl.load_workbook(model_path, data_only=True)
    try:
        con_sh = _find_consensus_sheet(wb)
        if con_sh:
            ws = wb[con_sh]
            for r in range(2, ws.max_row + 1):
                p = str(ws.cell(r, 1).value or "").strip().upper()
                m = _normalize_metric_key(str(ws.cell(r, 2).value or ""))
                rat = str(ws.cell(r, 8).value or "").strip()
                if p and m and rat:
                    rationales[(p, m)] = rat
    finally:
        wb.close()

    model_basis["sheet_rationales"] = {f"{k[0]}|{k[1]}": v for k, v in rationales.items()}
    return house_map, model_basis


def compute_consensus_diff(
    *,
    model_path: str | Path,
    ticker: str,
    research_dir: str | Path,
    consensus_path: str | Path | None = None,
    consensus_data: dict[str, Any] | None = None,
    thresholds: dict[str, float] | None = None,
    rationales: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compute House-vs-Consensus diff bound to live ``model_sha256`` and write ``model_dump/consensus-diff.*``."""
    mpath = Path(model_path).expanduser().resolve()
    if not mpath.is_file():
        raise FileNotFoundError(f"model workbook not found: {mpath}")
    rdir = Path(research_dir).expanduser().resolve()
    dump_dir = rdir / "model_dump"
    dump_dir.mkdir(parents=True, exist_ok=True)

    model_sha = model_fingerprint.file_sha256(mpath)
    eff_thresholds = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        for k, v in thresholds.items():
            eff_thresholds[k] = float(v)

    data = consensus_data
    cpath_str: str | None = None
    if data is None and consensus_path is not None:
        cp = Path(consensus_path).expanduser().resolve()
        cpath_str = str(cp)
        if cp.is_file():
            data = json.loads(cp.read_text(encoding="utf-8"))

    json_out = dump_dir / "consensus-diff.json"
    md_out = dump_dir / "consensus-diff.md"

    if not data or not (data.get("items") or data.get("estimates")):
        report: dict[str, Any] = {
            "schema_version": 1,
            "status": "missing_evidence",
            "ticker": ticker.strip().upper(),
            "model_path": str(mpath),
            "model_sha256": model_sha,
            "consensus_source": cpath_str,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "basis_alignment": {"aligned": False, "warnings": ["No consensus data available"]},
            "thresholds": eff_thresholds,
            "items": [],
            "exceeds_count": 0,
            "missing_rationale_count": 0,
        }
        json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        md_out.write_text(
            f"# Consensus Diff ({ticker.upper()})\n\n"
            f"- Model: `{mpath.name}` (`{model_sha[:12]}`)\n"
            "- Status: `missing_evidence` (no public consensus data available)\n",
            encoding="utf-8",
        )
        return report

    house_map, model_basis = _extract_house_values_from_model(mpath, ticker)
    sheet_rats = model_basis.pop("sheet_rationales", {})

    # 5-dimension basis alignment check (§5.2 item 3)
    c_basis = data.get("basis") or {}
    basis_warnings: list[str] = []
    checks: dict[str, dict[str, Any]] = {}
    for dim, m_val, c_val in (
        ("fiscal_year_end_month", model_basis.get("fiscal_year_end_month"), c_basis.get("fiscal_year_end_month")),
        ("currency", model_basis.get("currency"), c_basis.get("currency")),
        ("accounting_basis", model_basis.get("accounting_basis"), c_basis.get("accounting_basis")),
        ("share_basis", model_basis.get("share_basis"), c_basis.get("share_basis")),
    ):
        aligned = True
        if c_val is not None and m_val is not None and str(c_val).upper() != str(m_val).upper():
            aligned = False
            basis_warnings.append(f"{dim} mismatch: model={m_val!r} vs consensus={c_val!r}")
        checks[dim] = {"model": m_val, "consensus": c_val, "aligned": aligned}

    reflects_latest = c_basis.get("reflects_latest_earnings")
    checks["reflects_latest_earnings"] = {
        "consensus": reflects_latest,
        "aligned": reflects_latest is not False,
    }
    if reflects_latest is False:
        basis_warnings.append("consensus snapshot does not yet reflect the latest earnings release")

    raw_items = list(data.get("items") or data.get("estimates") or [])
    diff_rows: list[dict[str, Any]] = []
    exceeds_count = 0
    missing_rationale_count = 0

    for item in raw_items:
        period = str(item.get("period") or "").strip()
        metric_lbl = str(item.get("metric") or item.get("label") or "").strip()
        mkey = _normalize_metric_key(metric_lbl)
        cons_val = item.get("consensus")
        if cons_val is None:
            continue
        cons_f = float(cons_val)
        cat = _metric_category(mkey, item.get("category"))

        p_up = period.upper()
        p_norm = (model_fingerprint.parse_period_header(period) or {}).get("normalized", p_up).upper()
        h_entry = house_map.get((p_up, mkey)) or house_map.get((p_norm, mkey))
        house_val = float(item["house"]) if item.get("house") is not None else (
            h_entry["value"] if h_entry else None
        )
        house_cell = f"{h_entry['sheet']}!{h_entry['cell']}" if h_entry else None
        house_margin = item.get("house_margin") if item.get("house_margin") is not None else (
            h_entry.get("margin") if h_entry else None
        )
        cons_margin = item.get("consensus_margin")
        if cons_margin is None and h_entry and h_entry.get("revenue") and mkey in ("operating_income", "net_income"):
            cons_margin = cons_f / h_entry["revenue"]

        diff_abs = (house_val - cons_f) if house_val is not None else None
        diff_pct = (
            (house_val - cons_f) / abs(cons_f)
            if (house_val is not None and abs(cons_f) > 1e-9)
            else None
        )

        # Threshold evaluation (§5.2 & §8.7)
        exceeds = False
        threshold_desc = ""
        metric_mode = "pct"
        diff_bp: float | None = None

        if house_val is not None:
            if cat == "revenue":
                thr = eff_thresholds["revenue_pct"]
                threshold_desc = f"±{thr * 100:.1f}%"
                exceeds = diff_pct is not None and abs(diff_pct) > thr
            elif cat == "segment_revenue":
                thr = eff_thresholds["segment_revenue_pct"]
                threshold_desc = f"±{thr * 100:.1f}%"
                exceeds = diff_pct is not None and abs(diff_pct) > thr
            elif cat == "opex_ratio":
                metric_mode = "bp"
                diff_bp = (house_val - cons_f) * 10000.0
                thr_bp = eff_thresholds["opex_ratio_bp"]
                threshold_desc = f"±{thr_bp:.0f}bp"
                exceeds = abs(diff_bp) > thr_bp
            else:
                # Profit / EPS: apply §8.7 breakeven margin guard when |margin| < 3%
                use_bp = (
                    mkey in ("operating_income", "net_income")
                    and house_margin is not None
                    and cons_margin is not None
                    and (
                        abs(float(house_margin)) < eff_thresholds["breakeven_margin_abs"]
                        or abs(float(cons_margin)) < eff_thresholds["breakeven_margin_abs"]
                    )
                )
                if use_bp:
                    metric_mode = "bp"
                    diff_bp = (float(house_margin) - float(cons_margin)) * 10000.0
                    thr_bp = eff_thresholds["breakeven_margin_bp"]
                    threshold_desc = f"±{thr_bp:.0f}bp (near breakeven)"
                    exceeds = abs(diff_bp) > thr_bp
                else:
                    thr = eff_thresholds["profit_eps_pct"]
                    threshold_desc = f"±{thr * 100:.1f}%"
                    exceeds = diff_pct is not None and abs(diff_pct) > thr

        rat_key = f"{p_up}|{mkey}"
        rationale = (
            (rationales or {}).get(rat_key)
            or item.get("rationale")
            or sheet_rats.get(rat_key)
            or ""
        ).strip()
        missing_rat = bool(exceeds and not rationale)
        if exceeds:
            exceeds_count += 1
        if missing_rat:
            missing_rationale_count += 1

        diff_rows.append(
            {
                "period": period,
                "metric": metric_lbl,
                "metric_key": mkey,
                "category": cat,
                "house": house_val,
                "house_cell": house_cell,
                "consensus": cons_f,
                "diff_abs": diff_abs,
                "diff_pct": diff_pct,
                "diff_bp": diff_bp,
                "comparison_mode": metric_mode,
                "threshold": threshold_desc,
                "exceeds_threshold": exceeds,
                "rationale": rationale,
                "missing_rationale": missing_rat,
                "source_type": str(item.get("source_type") or data.get("source_type") or "Market Consensus"),
            }
        )

    report = {
        "schema_version": 1,
        "status": "ok",
        "ticker": ticker.strip().upper(),
        "model_path": str(mpath),
        "model_sha256": model_sha,
        "consensus_source": cpath_str or data.get("source_type") or "inline",
        "as_of_date": data.get("as_of_date") or data.get("fetched_at"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "basis_alignment": {
            "aligned": len(basis_warnings) == 0,
            "checks": checks,
            "warnings": basis_warnings,
        },
        "thresholds": eff_thresholds,
        "items": diff_rows,
        "exceeds_count": exceeds_count,
        "missing_rationale_count": missing_rationale_count,
    }

    json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Render Markdown table
    lines = [
        f"# House vs Consensus Diff ({ticker.strip().upper()})",
        "",
        f"- **Model**: `{mpath.name}` (`sha256: {model_sha}`)",
        f"- **Basis alignment**: {'ALIGNED' if not basis_warnings else 'WARNINGS: ' + '; '.join(basis_warnings)}",
        f"- **Exceeds threshold**: {exceeds_count} | **Missing rationale**: {missing_rationale_count}",
        "",
        "| Period | Metric | House | Consensus | Diff | Threshold | Flag | Rationale |",
        "|---|---|---:|---:|---:|---|---|---|",
    ]
    for row in diff_rows:
        h_s = f"{row['house']:,.2f}" if row["house"] is not None else "N/A"
        c_s = f"{row['consensus']:,.2f}"
        if row["comparison_mode"] == "bp" and row["diff_bp"] is not None:
            d_s = f"{row['diff_bp']:+.0f}bp"
        elif row["diff_pct"] is not None:
            d_s = f"{row['diff_pct'] * 100:+.1f}%"
        else:
            d_s = "N/A"
        flag = "EXCEEDS" if row["exceeds_threshold"] else "OK"
        rat_s = row["rationale"] or ("*(missing rationale)*" if row["missing_rationale"] else "-")
        lines.append(
            f"| {row['period']} | {row['metric']} | {h_s} | {c_s} | {d_s} | {row['threshold']} | {flag} | {rat_s} |"
        )
    md_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consensus snapshot writer and diff tool (modeling v2 §5.2).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("write-snapshot", help="Write consensus snapshot ONLY to isolated consensus sheet")
    p_snap.add_argument("--model", required=True)
    p_snap.add_argument("--consensus", required=True)
    p_snap.add_argument("--sheet", default="vs Consensus")

    p_diff = sub.add_parser("diff", help="Generate <R>/model_dump/consensus-diff.{json,md}")
    p_diff.add_argument("--model", required=True)
    p_diff.add_argument("--ticker", required=True)
    p_diff.add_argument("--research-dir", required=True)
    p_diff.add_argument("--consensus", default=None)
    p_diff.add_argument("--thresholds", default=None, help="JSON string or file path for threshold overrides")

    args = parser.parse_args(argv)
    if args.cmd == "write-snapshot":
        cdata = json.loads(Path(args.consensus).read_text(encoding="utf-8"))
        res = write_consensus_snapshot(model_path=args.model, consensus_data=cdata, sheet_name=args.sheet)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "diff":
        thr = None
        if args.thresholds:
            tp = Path(args.thresholds)
            thr = json.loads(tp.read_text(encoding="utf-8")) if tp.is_file() else json.loads(args.thresholds)
        res = compute_consensus_diff(
            model_path=args.model,
            ticker=args.ticker,
            research_dir=args.research_dir,
            consensus_path=args.consensus,
            thresholds=thr,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
