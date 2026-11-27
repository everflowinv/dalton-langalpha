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
import re
import shutil
import sys
import tempfile
from typing import Any
import zipfile

from lxml import etree
import openpyxl
from openpyxl.utils import get_column_letter

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

_GROUP_METRIC_EXACT: dict[str, tuple[str, ...]] = {
    "total_revenue": (
        "total revenue",
        "revenue",
        "net revenue",
        "net revenues",
        "total net revenues",
        "group revenue",
        "consolidated revenue",
        "总收入",
        "营业总收入",
        "营业收入",
        "集团总收入",
        "集团收入",
    ),
    "operating_income": (
        "operating income",
        "operating profit",
        "group operating income",
        "group operating profit",
        "consolidated operating income",
        "ebit",
        "营业利润",
        "集团营业利润",
    ),
    "net_income": (
        "net income",
        "net profit",
        "group net income",
        "net income attributable to shareholders",
        "归母净利润",
        "净利润",
    ),
    "diluted_eps": (
        "diluted eps",
        "eps",
        "epads",
        "diluted epads",
        "每股收益",
        "稀释每股收益",
    ),
    "gross_profit": ("gross profit", "group gross profit", "毛利", "毛利润"),
    "operating_expenses_ratio": ("opex %", "operating expenses %", "opex ratio", "费用率"),
}

_SEGMENT_METRIC_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("operating income", "operating_income"),
    ("operating profit", "operating_income"),
    ("gross profit", "gross_profit"),
    ("net revenues", "revenue"),
    ("net revenue", "revenue"),
    ("revenue", "revenue"),
    ("ebitda", "ebitda"),
    ("ebit", "operating_income"),
    ("margin", "margin"),
    ("营业利润", "operating_income"),
    ("毛利", "gross_profit"),
    ("收入", "revenue"),
)

_GROUP_PREFIX_WORDS = frozenset({"total", "group", "consolidated", "net", "总", "集团", "合并", "营业"})
_SEG_FILLER_RE = re.compile(r"\b(?:segment|division|business)\b|分部|业务", re.IGNORECASE)


def _slugify_key(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", text.strip().lower()).strip("_")


def _normalize_metric_key(name: str, segment: str | None = None) -> str:
    """Normalize ``(segment, metric)`` while preserving distinct segment identities.

    Ensures ``Cloud Revenue`` (``cloud::revenue``), ``Retail Revenue`` (``retail::revenue``),
    and ``Total Revenue`` (``total_revenue``) NEVER collide across snapshot writes, diff, or C04.
    """
    raw = str(name or "").strip()
    # Strip leading hierarchy prefixes joined by '|' (e.g. 'Segment Reporting | Cloud Revenue')
    if "|" in raw:
        parts = [p.strip() for p in raw.split("|") if p.strip()]
        raw = parts[-1] if parts else raw
    low = " ".join(raw.lower().split())

    if segment and str(segment).strip():
        seg_clean = _SEG_FILLER_RE.sub(" ", str(segment).strip().lower())
        seg_slug = _slugify_key(seg_clean)
        base_m = _normalize_metric_key(low)
        if base_m == "total_revenue":
            base_m = "revenue"
        return f"{seg_slug}::{base_m}" if seg_slug else base_m

    # 1. Exact group-level metric match
    for canon, aliases in _GROUP_METRIC_EXACT.items():
        if low == canon or low in aliases:
            return canon

    # 2. Segment-qualified metric (e.g. "Cloud Revenue", "Retail Segment Revenue", "JD Retail Operating Profit")
    for suffix, base_metric in _SEGMENT_METRIC_SUFFIXES:
        if low.endswith(suffix) and len(low) > len(suffix):
            prefix = low[: -len(suffix)].strip(" -:|/")
            if not prefix:
                continue
            prefix_tokens = set(prefix.split())
            if prefix in _GROUP_PREFIX_WORDS or (prefix_tokens and prefix_tokens <= _GROUP_PREFIX_WORDS):
                return "total_revenue" if base_metric == "revenue" else base_metric
            seg_clean = _SEG_FILLER_RE.sub(" ", prefix)
            seg_slug = _slugify_key(seg_clean)
            if seg_slug:
                return f"{seg_slug}::{base_metric}"

    return _slugify_key(low)


def _metric_category(metric_key: str, raw_category: str | None = None) -> str:
    if raw_category:
        return str(raw_category).strip().lower()
    if metric_key == "total_revenue":
        return "revenue"
    if metric_key.endswith("::revenue") or ("segment" in metric_key and "revenue" in metric_key):
        return "segment_revenue"
    if metric_key in ("operating_income", "net_income", "diluted_eps") or metric_key.endswith("::operating_income"):
        return "profit_eps"
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


def _detect_consensus_table_layouts(ws: Any) -> list[dict[str, Any]]:
    """Detect all safe consensus comparison tables on ``ws`` by scanning header rows.

    Returns a list of layout dicts ``{header_row, data_start_row, data_end_row, period_col,
    metric_col, house_col, consensus_col, diff_col, source_col, date_col, rationale_col}``
    that pass safety checks (distinct columns, no formulas in writable snapshot/metadata cols).
    """
    header_rows: list[dict[str, Any]] = []
    for r in range(1, ws.max_row + 1):
        cols: dict[str, int] = {}
        for c in range(1, ws.max_column + 1):
            v = ws.cell(r, c).value
            if not isinstance(v, str) or not v.strip():
                continue
            vl = " ".join(v.strip().lower().split())
            if "period_col" not in cols and any(k in vl for k in ("period", "期间", "fiscal", "quarter")):
                cols["period_col"] = c
            elif "metric_col" not in cols and any(k in vl for k in ("metric", "line item", "item", "指标", "科目", "项目")):
                cols["metric_col"] = c
            elif "diff_col" not in cols and any(k in vl for k in ("diff", "vs", "差异", "spread", "variance")):
                if "isolated reference zone" not in vl:
                    cols["diff_col"] = c
            elif "house_col" not in cols and any(k in vl for k in ("house", "our est", "model", "本方", "我们")):
                cols["house_col"] = c
            elif "consensus_col" not in cols and any(k in vl for k in ("consensus", "street", "外部预期", "一致预期")):
                if not any(ex in vl for ex in ("vs", "diff", "差异", "house", "我们", "rationale", "理由", "isolated reference zone")):
                    cols["consensus_col"] = c
            elif "source_col" not in cols and any(k in vl for k in ("source", "来源")):
                cols["source_col"] = c
            elif "date_col" not in cols and any(k in vl for k in ("as-of", "as of", "date", "日期")):
                cols["date_col"] = c
            elif "rationale_col" not in cols and any(k in vl for k in ("rationale", "reason", "note", "理由", "说明")):
                cols["rationale_col"] = c
        if {"period_col", "metric_col", "consensus_col"} <= set(cols):
            header_rows.append({"header_row": r, **cols})

    layouts: list[dict[str, Any]] = []
    for idx, hdr in enumerate(header_rows):
        h_row = int(hdr["header_row"])
        next_h = int(header_rows[idx + 1]["header_row"]) if idx + 1 < len(header_rows) else ws.max_row + 1
        # Determine contiguous end of this table block
        data_end = h_row
        for r in range(h_row + 1, next_h):
            p_v = ws.cell(r, hdr["period_col"]).value
            m_v = ws.cell(r, hdr["metric_col"]).value
            c_v = ws.cell(r, hdr["consensus_col"]).value
            if p_v is None and m_v is None and c_v is None:
                break
            data_end = r

        # Safety guard: consensus_col MUST NOT contain formulas (would indicate House/diff formula col!)
        cons_col = int(hdr["consensus_col"])
        has_formula_in_cons = False
        for r in range(h_row + 1, data_end + 1):
            val = ws.cell(r, cons_col).value
            if isinstance(val, str) and val.strip().startswith("="):
                has_formula_in_cons = True
                break
        if has_formula_in_cons:
            continue

        # Also validate optional writable metadata cols don't contain formulas or collide with house/diff
        protected_cols = {hdr.get("house_col"), hdr.get("diff_col")} - {None}
        safe_layout = dict(hdr)
        for opt_k in ("source_col", "date_col", "rationale_col"):
            oc = safe_layout.get(opt_k)
            if oc is None:
                continue
            if oc in protected_cols:
                safe_layout.pop(opt_k, None)
                continue
            for r in range(h_row + 1, data_end + 1):
                ov = ws.cell(r, int(oc)).value
                if isinstance(ov, str) and ov.strip().startswith("="):
                    safe_layout.pop(opt_k, None)
                    break

        safe_layout["data_start_row"] = h_row + 1
        safe_layout["data_end_row"] = data_end
        layouts.append(safe_layout)
    return layouts


def _eval_simple_diff_formula(
    expr: str,
    house_coord: str,
    cons_coord: str,
    house_val: float | None,
    cons_val: float,
) -> float | None:
    """Evaluate standard House-vs-Consensus diff formulas directly without LibreOffice."""
    if not expr or not expr.startswith("=") or house_val is None:
        return None
    norm = "".join(expr[1:].upper().replace("$", "").split())
    h = house_coord.upper()
    c = cons_coord.upper()
    # Unwrap IF(OR(C="",C=0),"",<inner>) or IFERROR(<inner>,...)
    m_if = re.match(r'^IF\(OR\([^)]+\),"",(.+)\)$', norm)
    if m_if:
        norm = m_if.group(1)
    m_ife = re.match(r'^IFERROR\((.+),[^,]+\)$', norm)
    if m_ife:
        norm = m_ife.group(1)

    if norm in (f"({h}-{c})/ABS({c})", f"({h}-{c})/{c}"):
        denom = abs(cons_val) if "ABS(" in norm else cons_val
        return (house_val - cons_val) / denom if abs(denom) > 1e-12 else None
    if norm in (f"({h}/{c})-1", f"{h}/{c}-1"):
        return (house_val / cons_val) - 1.0 if abs(cons_val) > 1e-12 else None
    if norm in (f"{h}-{c}", f"({h}-{c})"):
        return house_val - cons_val
    if norm in (f"{c}-{h}", f"({c}-{h})"):
        return cons_val - house_val
    return None


def _refresh_consensus_diff_caches(
    xlsx_path: Path,
    sheet_name: str,
    donor_path: Path,
    diff_targets: list[dict[str, Any]],
) -> list[str]:
    """Recompute or clear stale ``<v>`` caches on diff formula cells affected by snapshot updates."""
    if not diff_targets:
        return []
    donor_cells = model_xlsx_hooks._extract_donor_cells(donor_path)
    infos, payload = model_xlsx_hooks._read_package(xlsx_path)
    parts = model_xlsx_hooks._sheet_parts(payload)
    part = parts.get(sheet_name)
    if not part or part not in payload:
        return []

    root = etree.fromstring(payload[part], model_xlsx_hooks._XML)
    sml = model_xlsx_hooks._SML
    by_ref = {(c.get("r") or "").upper(): c for c in root.iter(f"{{{sml}}}c") if c.get("r")}
    refreshed: list[str] = []
    changed = False

    def _resolve_house_val(house_ref: str) -> float | None:
        # First check current sheet cell <v> (already restored from donor for house_col)
        hc_el = by_ref.get(house_ref)
        if hc_el is not None:
            v_el = hc_el.find(f"{{{sml}}}v")
            if v_el is not None and v_el.text is not None:
                try:
                    return float(v_el.text)
                except ValueError:
                    pass
            # If house cell is a simple cross-sheet link (e.g. ='Model - JD'!G6), read referenced donor <v>
            f_el = hc_el.find(f"{{{sml}}}f")
            if f_el is not None and f_el.text:
                refs = model_fingerprint.extract_formula_refs("=" + f_el.text.lstrip("="), sheet_name)
                if len(refs) == 1:
                    rsh, rcrd = refs[0]
                    dm = (donor_cells.get(rsh) or {}).get(rcrd.upper())
                    if dm and dm.get("v") is not None:
                        try:
                            return float(dm["v"])
                        except ValueError:
                            pass
        # Fallback to donor's cache on this consensus sheet
        dm_self = (donor_cells.get(sheet_name) or {}).get(house_ref)
        if dm_self and dm_self.get("v") is not None:
            try:
                return float(dm_self["v"])
            except ValueError:
                pass
        return None

    for tgt in diff_targets:
        diff_ref = str(tgt["diff_coord"]).upper()
        c_el = by_ref.get(diff_ref)
        if c_el is None:
            continue
        f_el = c_el.find(f"{{{sml}}}f")
        if f_el is None or not f_el.text:
            continue
        expr = "=" + f_el.text.lstrip("=")
        h_coord = tgt.get("house_coord")
        c_coord = tgt["cons_coord"]
        c_val = float(tgt["cons_val"])
        h_val = _resolve_house_val(str(h_coord).upper()) if h_coord else None

        # Also refresh house_col cache if it was newly written and missing <v>
        if h_coord and h_val is not None:
            hc_el = by_ref.get(str(h_coord).upper())
            if hc_el is not None and hc_el.find(f"{{{sml}}}f") is not None and not model_xlsx_hooks._cell_has_nonempty_cache(hc_el):
                model_xlsx_hooks._apply_cached_value(hc_el, None, repr(h_val))
                changed = True

        new_diff = (
            _eval_simple_diff_formula(expr, str(h_coord), str(c_coord), h_val, c_val)
            if h_coord
            else None
        )
        if new_diff is not None:
            model_xlsx_hooks._apply_cached_value(c_el, None, repr(new_diff))
            refreshed.append(diff_ref)
            changed = True
        else:
            # Never leave a stale pre-update diff cached value behind!
            v_el = c_el.find(f"{{{sml}}}v")
            if v_el is not None:
                c_el.remove(v_el)
                if "t" in c_el.attrib:
                    del c_el.attrib["t"]
                refreshed.append(diff_ref)
                changed = True

    if changed:
        payload[part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        tmp = xlsx_path.with_name(xlsx_path.name + ".consdiff.tmp")
        with zipfile.ZipFile(tmp, "w") as zout:
            for info in infos:
                zout.writestr(info, payload[info.filename], compress_type=info.compress_type)
        os.replace(tmp, xlsx_path)
    return sorted(set(refreshed))


def write_consensus_snapshot(
    *,
    model_path: str | Path,
    consensus_data: dict[str, Any],
    sheet_name: str = "vs Consensus",
    coord_map_path: str | Path | None = None,
) -> dict[str, Any]:
    """Write external consensus reference figures ONLY into the isolated snapshot sheet/zone.

    - Auto-detects actual column order on an existing consensus sheet and verifies writable columns
      contain no formulas; if layout cannot be safely recognized, appends a new isolated reference
      zone below existing content without overwriting any existing House formulas.
    - Never edits any cell on House model/valuation/summary sheets.
    - Preserves all pre-existing OOXML cached values ``<v>`` and ``<calcPr>``, while refreshing
      cached values of diff formula cells affected by the updated snapshot.
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
        touched_coords: list[str] = []
        touched_formula_coords: list[str] = []
        diff_targets: list[dict[str, Any]] = []
        layout_mode = "existing_table"
        try:
            target_sh = _find_consensus_sheet(wb, sheet_name)
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
            def _open_isolated_block() -> dict[str, Any]:
                banner_r = max(ws.max_row, 1) + 2
                hdr_r = banner_r + 1
                banner_c = ws.cell(banner_r, 1, "Consensus Snapshot (Isolated Reference Zone)")
                model_style.apply_cell_style(banner_c, "section_report")
                touched_coords.append(f"A{banner_r}")
                for idx, h in enumerate(headers, start=1):
                    c = ws.cell(hdr_r, idx, h)
                    model_style.apply_cell_style(c, "header")
                    c.alignment = openpyxl.styles.Alignment(
                        horizontal="left" if idx in (1, 2, 6, 7, 8) else "right", vertical="center"
                    )
                    touched_coords.append(f"{get_column_letter(idx)}{hdr_r}")
                return {
                    "header_row": hdr_r,
                    "data_start_row": hdr_r + 1,
                    "data_end_row": hdr_r,
                    "period_col": 1,
                    "metric_col": 2,
                    "house_col": 3,
                    "consensus_col": 4,
                    "diff_col": 5,
                    "source_col": 6,
                    "date_col": 7,
                    "rationale_col": 8,
                }

            if target_sh is None:
                ws = wb.create_sheet(sheet_name)
                target_sh = sheet_name
                layout_mode = "new_sheet"
                for idx, h in enumerate(headers, start=1):
                    c = ws.cell(1, idx, h)
                    model_style.apply_cell_style(c, "header")
                    c.alignment = openpyxl.styles.Alignment(
                        horizontal="left" if idx in (1, 2, 6, 7, 8) else "right", vertical="center"
                    )
                    touched_coords.append(f"{get_column_letter(idx)}1")
                ws.freeze_panes = "C2"
                layout = {
                    "header_row": 1,
                    "data_start_row": 2,
                    "data_end_row": 1,
                    "period_col": 1,
                    "metric_col": 2,
                    "house_col": 3,
                    "consensus_col": 4,
                    "diff_col": 5,
                    "source_col": 6,
                    "date_col": 7,
                    "rationale_col": 8,
                }
            else:
                ws = wb[target_sh]
                detected = _detect_consensus_table_layouts(ws)
                if detected:
                    # Prefer the last isolated reference block if one already exists, else the first valid table
                    layout = detected[-1]
                    layout_mode = "existing_table"
                else:
                    # Unrecognizable or formula-colliding layout -> create brand-new isolated zone below!
                    layout_mode = "isolated_block"
                    layout = _open_isolated_block()

            def _layout_cols(lay: dict[str, Any]) -> tuple[int, int, int | None, int, int | None, int | None, int | None, int | None]:
                return (
                    int(lay["period_col"]),
                    int(lay["metric_col"]),
                    lay.get("house_col"),
                    int(lay["consensus_col"]),
                    lay.get("diff_col"),
                    lay.get("source_col"),
                    lay.get("date_col"),
                    lay.get("rationale_col"),
                )

            def _row_conflicts_for_append(r_cand: int, lay: dict[str, Any]) -> bool:
                """Check ALL planned write positions & table span on ``r_cand`` before appending (Codex R2 #2)."""
                cols_in_lay = [
                    int(lay[k])
                    for k in ("period_col", "metric_col", "house_col", "consensus_col", "diff_col", "source_col", "date_col", "rationale_col")
                    if lay.get(k) is not None
                ]
                if not cols_in_lay:
                    return True
                for c_idx in range(min(cols_in_lay), max(cols_in_lay) + 1):
                    if ws.cell(r_cand, c_idx).value is not None:
                        return True
                return False

            def _existing_row_has_formula_conflict(r_exist: int, lay: dict[str, Any], item_obj: dict[str, Any]) -> bool:
                check_cols = [int(lay["consensus_col"])]
                for opt_k in ("source_col", "date_col"):
                    if lay.get(opt_k) is not None:
                        check_cols.append(int(lay[opt_k]))
                if lay.get("rationale_col") is not None and item_obj.get("rationale") is not None:
                    check_cols.append(int(lay["rationale_col"]))
                for c_idx in check_cols:
                    val = ws.cell(r_exist, c_idx).value
                    if isinstance(val, str) and val.strip().startswith("="):
                        return True
                return False

            p_col, m_col, h_col, c_col, d_col, s_col, dt_col, rat_col = _layout_cols(layout)

            # Build existing (period, metric_key) -> row index lookup strictly within detected table
            row_lookup: dict[tuple[str, str], int] = {}
            next_row = int(layout["data_end_row"]) + 1
            for r in range(int(layout["data_start_row"]), int(layout["data_end_row"]) + 1):
                p_val = str(ws.cell(r, p_col).value or "").strip()
                m_val = str(ws.cell(r, m_col).value or "").strip()
                if p_val and m_val:
                    row_lookup[(p_val.upper(), _normalize_metric_key(m_val))] = r

            # Pre-validate append rows: if any prospective append row collides with existing formulas/content
            # (e.g. a summary row right below the table carrying House/Diff formulas), open an isolated zone!
            isolated_layout: dict[str, Any] | None = None
            isolated_next_row = 0
            isolated_lookup: dict[tuple[str, str], int] = {}

            def _ensure_isolated_append() -> dict[str, Any]:
                nonlocal isolated_layout, isolated_next_row, layout_mode
                if isolated_layout is None:
                    isolated_layout = _open_isolated_block()
                    isolated_next_row = int(isolated_layout["data_start_row"])
                    layout_mode = "isolated_block"
                return isolated_layout

            for item in items:
                period = str(item.get("period") or "").strip()
                metric_label = str(item.get("metric") or item.get("label") or "").strip()
                segment_label = str(item.get("segment") or "").strip() or None
                cons_val = item.get("consensus")
                if not period or not metric_label or cons_val is None:
                    continue
                display_metric = (
                    f"{segment_label} {metric_label}"
                    if (segment_label and segment_label.lower() not in metric_label.lower())
                    else metric_label
                )
                mkey = _normalize_metric_key(metric_label, segment=segment_label)
                row_idx = row_lookup.get((period.upper(), mkey))
                active_lay = layout
                use_isolated = False

                if row_idx is not None and _existing_row_has_formula_conflict(row_idx, layout, item):
                    use_isolated = True
                    row_idx = None
                elif row_idx is None:
                    if isolated_layout is not None or (
                        layout_mode == "existing_table" and _row_conflicts_for_append(next_row, layout)
                    ):
                        use_isolated = True

                if use_isolated:
                    active_lay = _ensure_isolated_append()
                    row_idx = isolated_lookup.get((period.upper(), mkey))

                ap_col, am_col, ah_col, ac_col, ad_col, as_col, adt_col, arat_col = _layout_cols(active_lay)
                is_new_row = False
                if row_idx is None:
                    is_new_row = True
                    if use_isolated:
                        row_idx = isolated_next_row
                        isolated_next_row += 1
                    else:
                        row_idx = next_row
                        next_row += 1
                    ws.cell(row_idx, ap_col, period)
                    ws.cell(row_idx, am_col, display_metric)
                    touched_coords.append(f"{get_column_letter(ap_col)}{row_idx}")
                    touched_coords.append(f"{get_column_letter(am_col)}{row_idx}")
                    if ah_col is not None and item.get("house_formula"):
                        hc = ws.cell(row_idx, int(ah_col), str(item["house_formula"]))
                        model_style.apply_cell_style(hc, "formula", is_link=True)
                        hc_ref = f"{get_column_letter(int(ah_col))}{row_idx}"
                        touched_coords.append(hc_ref)
                        touched_formula_coords.append(hc_ref)
                    if ad_col is not None and ah_col is not None:
                        c_let = get_column_letter(ac_col)
                        h_let = get_column_letter(int(ah_col))
                        d_let = get_column_letter(int(ad_col))
                        dc = ws.cell(
                            row_idx,
                            int(ad_col),
                            f'=IF(OR({c_let}{row_idx}="",{c_let}{row_idx}=0),"",({h_let}{row_idx}-{c_let}{row_idx})/ABS({c_let}{row_idx}))',
                        )
                        model_style.apply_cell_style(dc, "formula", is_ratio=True, is_percent=True)
                        dc_ref = f"{d_let}{row_idx}"
                        touched_coords.append(dc_ref)
                        touched_formula_coords.append(dc_ref)
                    if use_isolated:
                        isolated_lookup[(period.upper(), mkey)] = row_idx
                    else:
                        row_lookup[(period.upper(), mkey)] = row_idx

                cons_coord = f"{get_column_letter(ac_col)}{row_idx}"
                snap_cell = ws.cell(row_idx, ac_col, float(cons_val))
                model_style.apply_cell_style(snap_cell, "external_input")
                written_coords.append(cons_coord)
                touched_coords.append(cons_coord)

                if as_col is not None:
                    src_cell = ws.cell(row_idx, int(as_col), str(item.get("source_type") or source_type))
                    model_style.apply_cell_style(src_cell, "source_note")
                    touched_coords.append(f"{get_column_letter(int(as_col))}{row_idx}")
                if adt_col is not None:
                    dt_cell = ws.cell(row_idx, int(adt_col), str(item.get("as_of_date") or as_of_date))
                    model_style.apply_cell_style(dt_cell, "source_note")
                    touched_coords.append(f"{get_column_letter(int(adt_col))}{row_idx}")
                if arat_col is not None and item.get("rationale") is not None:
                    ws.cell(row_idx, int(arat_col), str(item["rationale"]))
                    touched_coords.append(f"{get_column_letter(int(arat_col))}{row_idx}")

                # Track any diff formula cell on this row so its cached value is refreshed, not left stale!
                if ad_col is not None:
                    d_coord = f"{get_column_letter(int(ad_col))}{row_idx}"
                    d_val = ws.cell(row_idx, int(ad_col)).value
                    if isinstance(d_val, str) and d_val.strip().startswith("="):
                        touched_formula_coords.append(d_coord)
                        diff_targets.append(
                            {
                                "diff_coord": d_coord,
                                "house_coord": f"{get_column_letter(int(ah_col))}{row_idx}" if ah_col is not None else None,
                                "cons_coord": cons_coord,
                                "cons_val": float(cons_val),
                                "is_new_row": is_new_row,
                            }
                        )

            wb.save(editing_copy)
            sheet_names = list(wb.sheetnames)
        finally:
            wb.close()

        model_edit._preserve_calcpr_from_donor(editing_copy, donor_copy)
        # Restore donor caches on ALL untouched sheets and untouched formulas, excluding updated diff formulas
        spec = {
            "cache_donor_path": str(donor_copy),
            "skip_sheets": [s for s in sheet_names if s != target_sh],
            "skip_cells": {},
            "touched_cells": {target_sh: sorted(set(touched_coords))},
            "touched_formula_cells": {target_sh: sorted(set(touched_formula_coords))},
        }
        model_xlsx_hooks.restore_donor_cache(editing_copy, spec)
        refreshed_diffs = _refresh_consensus_diff_caches(
            editing_copy, target_sh, donor_copy, diff_targets
        )
        os.replace(editing_copy, mpath)

    new_sha = model_fingerprint.file_sha256(mpath)
    touched_sorted = sorted(set(touched_coords))
    snap_sorted = sorted(set(written_coords))
    if coord_map_path is not None:
        cp = Path(coord_map_path).expanduser().resolve()
        if cp.is_file():
            cm = json.loads(cp.read_text(encoding="utf-8"))
            cm_touched = set(cm.setdefault("touched_cells", {}).get(target_sh) or ())
            cm_touched.update(touched_sorted)
            cm["touched_cells"][target_sh] = sorted(cm_touched)
            cm_snap = set(cm.setdefault("consensus_snapshot_cells", {}).get(target_sh) or ())
            cm_snap.update(snap_sorted)
            cm["consensus_snapshot_cells"][target_sh] = sorted(cm_snap)
            cm["candidate_sha256"] = new_sha
            cp.write_text(json.dumps(cm, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        "status": "ok",
        "model_path": str(mpath),
        "model_sha256": new_sha,
        "sheet": target_sh,
        "layout_mode": layout_mode,
        "consensus_snapshot_cells": {target_sh: snap_sorted},
        "touched_cells": {target_sh: touched_sorted},
        "refreshed_diff_cells": refreshed_diffs,
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
        anchor_rows: dict[str, int | None] = {
            "total_revenue": anchors.get("total_revenue_row"),
            "gross_profit": anchors.get("gross_profit_row"),
            "operating_income": anchors.get("operating_income_row"),
            "net_income": anchors.get("net_income_row"),
            "diluted_eps": anchors.get("eps_row"),
        }
        lim = sh_data.get("line_item_map") or {}
        for row_s, rmeta in lim.items():
            lbl = str(rmeta.get("label") or "").strip()
            if not lbl:
                continue
            r_mkey = _normalize_metric_key(lbl)
            if r_mkey and r_mkey not in anchor_rows:
                anchor_rows[r_mkey] = int(row_s)
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

    # Also read rationales already recorded in vs Consensus sheet (auto-detected layouts)
    rationales: dict[tuple[str, str], str] = {}
    wb = openpyxl.load_workbook(model_path, data_only=True)
    try:
        con_sh = _find_consensus_sheet(wb)
        if con_sh:
            ws = wb[con_sh]
            layouts = _detect_consensus_table_layouts(ws)
            if layouts:
                for lay in layouts:
                    rat_c = lay.get("rationale_col")
                    if rat_c is None:
                        continue
                    for r in range(int(lay["data_start_row"]), int(lay["data_end_row"]) + 1):
                        p = str(ws.cell(r, int(lay["period_col"])).value or "").strip().upper()
                        m = _normalize_metric_key(str(ws.cell(r, int(lay["metric_col"])).value or ""))
                        rat = str(ws.cell(r, int(rat_c)).value or "").strip()
                        if p and m and rat:
                            rationales[(p, m)] = rat
            else:
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
        segment_lbl = str(item.get("segment") or "").strip() or None
        mkey = _normalize_metric_key(metric_lbl, segment=segment_lbl)
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
    p_snap.add_argument("--coord-map", default=None, help="Optional coord_map.json to register written cells")

    p_diff = sub.add_parser("diff", help="Generate <R>/model_dump/consensus-diff.{json,md}")
    p_diff.add_argument("--model", required=True)
    p_diff.add_argument("--ticker", required=True)
    p_diff.add_argument("--research-dir", required=True)
    p_diff.add_argument("--consensus", default=None)
    p_diff.add_argument("--thresholds", default=None, help="JSON string or file path for threshold overrides")

    args = parser.parse_args(argv)
    if args.cmd == "write-snapshot":
        cdata = json.loads(Path(args.consensus).read_text(encoding="utf-8"))
        res = write_consensus_snapshot(
            model_path=args.model,
            consensus_data=cdata,
            sheet_name=args.sheet,
            coord_map_path=args.coord_map,
        )
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
