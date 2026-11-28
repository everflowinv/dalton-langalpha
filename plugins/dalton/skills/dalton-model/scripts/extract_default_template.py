#!/usr/bin/env python3
"""Verify a recovered baseline model and extract the desensitized default template (modeling v2 §4.1).

Usage:
    python3 extract_default_template.py <baseline.xlsx> --out <default_model_template.xlsx> \
        [--company-tag JD] [--before-ts 2026-11-01T00:00:00Z] [--verify-only] [--report-out report.json]

Standalone script (stdlib + openpyxl + lxml only).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
import zipfile

from lxml import etree
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import model_readable  # noqa: E402
import model_style  # noqa: E402

_SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XML = etree.XMLParser(resolve_entities=False, no_network=True)
_PLUGIN_RE = re.compile(
    r"(?:_xll\.ciqfunctions|\bCIQ\s*\(|\bBQL\s*\(|\bBDH\s*\(|\bBDP\s*\(|\bRTD\s*\(|\bWEBSERVICE\s*\()",
    re.IGNORECASE,
)
_EXT_REF_RE = re.compile(r"\[[^\]]+\][^!]+!")
_FORECAST_HEADER_RE = re.compile(r"(?:20\d{2}|[1-4]Q\d{2}|Q[1-4]\d{2})[EFP]\b", re.IGNORECASE)
_TICKER_SUFFIX_RE = re.compile(r"\s*[-–—]\s*([A-Z0-9]{1,8})\s*$")


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _read_calc_pr(xlsx_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        if "xl/workbook.xml" not in zf.namelist():
            return {"full_calc_on_load": False, "present": False}
        root = etree.fromstring(zf.read("xl/workbook.xml"), _XML)
    calc = root.find(f"{{{_SML}}}calcPr")
    if calc is None:
        return {"full_calc_on_load": False, "present": False}
    raw_full = (calc.get("fullCalcOnLoad") or "").strip().lower()
    return {
        "present": True,
        "full_calc_on_load": raw_full in ("1", "true"),
        "calc_mode": calc.get("calcMode") or "auto",
        "iterate": (calc.get("iterate") or "").strip().lower() in ("1", "true"),
    }


def _sheet_pane_splits(xlsx_path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        wb_root = etree.fromstring(zf.read("xl/workbook.xml"), _XML)
        rels = etree.fromstring(zf.read("xl/_rels/workbook.xml.rels"), _XML)
        rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        targets = {r.get("Id"): r.get("Target", "") for r in rels}
        for sh in wb_root.iter(f"{{{_SML}}}sheet"):
            name = sh.get("name") or ""
            tgt = targets.get(sh.get(rel_ns), "")
            part = tgt.lstrip("/") if tgt.startswith("/") else f"xl/{tgt.lstrip('/')}"
            if part not in zf.namelist():
                continue
            sroot = etree.fromstring(zf.read(part), _XML)
            pane = sroot.find(f".//{{{_SML}}}sheetViews/{{{_SML}}}sheetView/{{{_SML}}}pane")
            if pane is not None:
                out[name] = {
                    "x_split": int(float(pane.get("xSplit") or 0)),
                    "y_split": int(float(pane.get("ySplit") or 0)),
                    "top_left_cell": pane.get("topLeftCell"),
                    "state": pane.get("state"),
                }
            else:
                out[name] = {"x_split": 0, "y_split": 0, "top_left_cell": None, "state": None}
    return out


def verify_baseline_structure(
    source_path: str | Path,
    *,
    before_ts: str | None = None,
    company_tag: str | None = None,
) -> dict[str, Any]:
    """Verify baseline structural health before template extraction (§4.1 step 2)."""
    path = Path(source_path).expanduser().resolve()
    ok, msg = model_readable.check_workbook_readable(path)
    issues: list[str] = []
    if not ok:
        return {"ok": False, "path": str(path), "issues": [f"unreadable: {msg}"]}

    calc_pr = _read_calc_pr(path)
    if calc_pr.get("full_calc_on_load"):
        issues.append("calcPr forces fullCalcOnLoad=1 (corrupted/rewritten workbook signature)")

    wb = openpyxl.load_workbook(path, data_only=False)
    try:
        modified_dt = getattr(wb.properties, "modified", None)
        if before_ts:
            cutoff = _parse_iso(before_ts)
            if isinstance(modified_dt, datetime):
                mod_aware = (
                    modified_dt if modified_dt.tzinfo else modified_dt.replace(tzinfo=timezone.utc)
                )
                if cutoff and mod_aware > cutoff:
                    issues.append(
                        f"workbook modified timestamp {mod_aware.isoformat()} is after before_ts {cutoff.isoformat()}"
                    )

        panes = _sheet_pane_splits(path)
        palette = model_style.extract_theme_palette(path)

        model_sheets = [
            ws for ws in wb.worksheets
            if ws.title.lower().startswith("model")
            and (company_tag is None or company_tag.upper() in ws.title.upper())
        ]
        if not model_sheets:
            model_sheets = [ws for ws in wb.worksheets if "readme" not in ws.title.lower()][:1]

        fonts_seen: set[tuple[str, float]] = set()
        fills_seen: set[str] = set()
        forecast_formula_count = 0
        pane_checked = False

        for ws in model_sheets:
            pane = panes.get(ws.title, {})
            xs, ys = pane.get("x_split", 0), pane.get("y_split", 0)
            if xs > 0 or ys > 0:
                pane_checked = True
                if xs > 15 or ys > 15:
                    issues.append(
                        f"sheet {ws.title!r} has abnormal freeze pane split ({xs}, {ys})"
                    )

            forecast_cols: set[int] = set()
            for row_idx in range(1, min(6, ws.max_row + 1)):
                for col_idx in range(1, ws.max_column + 1):
                    v = ws.cell(row_idx, col_idx).value
                    if isinstance(v, str) and _FORECAST_HEADER_RE.search(v.strip()):
                        forecast_cols.add(col_idx)

            for r, c, cell in model_style._iter_stored_cells(ws):
                if cell.font and cell.font.name:
                    fonts_seen.add((cell.font.name, float(cell.font.size or 0)))
                fill_rgb = model_style.get_cell_fill_rgb(cell, palette)
                if fill_rgb:
                    fills_seen.add(fill_rgb)
                if c in forecast_cols and r > 1 and isinstance(cell.value, str) and cell.value.startswith("="):
                    forecast_formula_count += 1

        if forecast_formula_count == 0:
            issues.append("no formulas found in forecast columns of model sheet(s)")
        has_native_font = any(
            name in ("Arial", "等线", "DengXian") and abs(sz - 8.0) <= 1.0
            for name, sz in fonts_seen
        )
        if not has_native_font:
            issues.append("missing native Arial/等线 8pt font signature")
        expected_slots = {
            model_style.HEADER_FILL,
            model_style.ASSUMP_FILL,
            model_style.SEC_REPORT_FILL,
        }
        if not (fills_seen & expected_slots):
            issues.append("missing standard Dalton color slots (3366FF / FFFFC8 / 99CCFF)")

        return {
            "ok": len(issues) == 0,
            "path": str(path),
            "sha256": _file_sha256(path),
            "calc_pr": calc_pr,
            "model_sheets": [ws.title for ws in model_sheets],
            "forecast_formula_count": forecast_formula_count,
            "pane_checked": pane_checked,
            "fonts_observed": sorted({f"{n}:{s:g}" for n, s in fonts_seen}),
            "fills_observed": sorted(fills_seen),
            "issues": issues,
        }
    finally:
        wb.close()


def _detect_source_tokens(wb: openpyxl.Workbook, company_tag: str | None) -> list[str]:
    tokens: set[str] = set()
    if company_tag:
        tokens.add(company_tag.strip())
    for name in wb.sheetnames:
        m = _TICKER_SUFFIX_RE.search(name)
        if m:
            tokens.add(m.group(1))
        if name.endswith(">>"):
            t = name[:-2].strip()
            if t:
                tokens.add(t)
    return sorted((t for t in tokens if len(t) >= 2), key=len, reverse=True)


def _desensitize_text(text: str, tokens: list[str]) -> str:
    out = text
    for tok in tokens:
        out = re.sub(rf"\b{re.escape(tok)}\b", "<TICKER>", out, flags=re.IGNORECASE)
    return out


def _strip_calc_full_on_load(xlsx_path: Path) -> None:
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        infos = zf.infolist()
        payload = {i.filename: zf.read(i.filename) for i in infos}

    wb_root = etree.fromstring(payload["xl/workbook.xml"], _XML)
    calc = wb_root.find(f"{{{_SML}}}calcPr")
    if calc is not None and "fullCalcOnLoad" in calc.attrib:
        del calc.attrib["fullCalcOnLoad"]
    payload["xl/workbook.xml"] = etree.tostring(wb_root, encoding="UTF-8", xml_declaration=True)

    # Ensure zero externalLinks parts remain in template package
    drop_parts = {
        name for name in payload
        if name.startswith("xl/externalLinks/")
    }
    tmp = xlsx_path.with_name(xlsx_path.name + ".tpl.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            if info.filename in drop_parts:
                continue
            zout.writestr(info, payload[info.filename])
    os.replace(tmp, xlsx_path)


def _clone_style_profile(source_path: Path, company_tag: str | None) -> dict[str, Any]:
    """Read observed visual style (header fill, fonts, widths, section fills) from baseline."""
    palette = model_style.extract_theme_palette(source_path)
    profile = {
        "font_en": model_style.FONT_EN,
        "font_zh": model_style.FONT_ZH,
        "font_size": model_style.FONT_SIZE,
        "header_fill": model_style.HEADER_FILL,
        "header_font": model_style.HEADER_FONT,
        "assump_fill": model_style.ASSUMP_FILL,
        "sec_report_fill": model_style.SEC_REPORT_FILL,
        "sec_segment_fill": model_style.SEC_SEGMENT_FILL,
        "sec_submodel_fill": model_style.SEC_SUBMODEL_FILL,
        "freeze_panes": "E2",
        "label_col_width": model_style.DEFAULT_LABEL_COL_WIDTH,
        "period_col_width": model_style.DEFAULT_PERIOD_COL_WIDTH,
        "unit_text": "(RMB MM)",
        "section_labels": [],
    }
    wb = openpyxl.load_workbook(source_path, data_only=False)
    try:
        target_ws = None
        for ws in wb.worksheets:
            if ws.title.lower().startswith("model"):
                if company_tag is None or company_tag.upper() in ws.title.upper():
                    target_ws = ws
                    break
        if target_ws is None and wb.worksheets:
            target_ws = wb.worksheets[0]
        if target_ws is not None:
            if target_ws.freeze_panes:
                profile["freeze_panes"] = str(target_ws.freeze_panes)
            a1 = target_ws["A1"]
            if isinstance(a1.value, str) and a1.value.strip():
                profile["unit_text"] = a1.value.strip()
            hf = model_style.get_cell_fill_rgb(a1, palette)
            if hf:
                profile["header_fill"] = hf
            if a1.font and a1.font.name:
                profile["font_en"] = a1.font.name
            if a1.font and a1.font.size:
                profile["font_size"] = float(a1.font.size)
            d_dim = target_ws.column_dimensions.get("D")
            if d_dim and d_dim.width:
                profile["label_col_width"] = float(d_dim.width)
            e_dim = target_ws.column_dimensions.get("E")
            if e_dim and e_dim.width:
                profile["period_col_width"] = float(e_dim.width)
            tokens = _detect_source_tokens(wb, company_tag)
            sec_labels: list[str] = []
            for r in range(2, min(target_ws.max_row + 1, 200)):
                c = target_ws.cell(r, 1)
                fill = model_style.get_cell_fill_rgb(c, palette)
                if fill in (
                    model_style.SEC_REPORT_FILL,
                    model_style.SEC_SEGMENT_FILL,
                    model_style.SEC_SUBMODEL_FILL,
                ) and isinstance(c.value, str):
                    sec_labels.append(_desensitize_text(c.value.strip(), tokens))
            profile["section_labels"] = sec_labels
    finally:
        wb.close()
    return profile


def _hair_border() -> Border:
    s = Side(style=model_style.ASSUMP_BORDER, color="000000")
    return Border(left=s, right=s, top=s, bottom=s)


def _style_header(cell: Any, font_en: str, font_size: float, header_fill: str, align: str = "right") -> None:
    cell.font = Font(name=font_en, size=font_size, bold=True, color=model_style.HEADER_FONT)
    cell.fill = PatternFill(fill_type="solid", start_color=header_fill, end_color=header_fill)
    cell.alignment = Alignment(horizontal=align, vertical="center")


def _style_section(cell: Any, font_en: str, font_size: float, fill_rgb: str, font_rgb: str = "000000") -> None:
    cell.font = Font(name=font_en, size=font_size, bold=True, color=font_rgb)
    cell.fill = PatternFill(fill_type="solid", start_color=fill_rgb, end_color=fill_rgb)


def _style_cell(
    cell: Any,
    *,
    font_en: str,
    font_size: float,
    color: str = model_style.FORMULA_FONT,
    bold: bool = False,
    italic: bool = False,
    fill_rgb: str | None = None,
    hair: bool = False,
    num_fmt: str | None = None,
) -> None:
    cell.font = Font(name=font_en, size=font_size, bold=bold, italic=italic, color=color)
    if fill_rgb:
        cell.fill = PatternFill(fill_type="solid", start_color=fill_rgb, end_color=fill_rgb)
    if hair:
        cell.border = _hair_border()
    if num_fmt:
        cell.number_format = num_fmt


def extract_default_template(
    source_path: str | Path,
    output_path: str | Path,
    *,
    company_tag: str | None = None,
    before_ts: str | None = None,
) -> dict[str, Any]:
    """Verify baseline and build desensitized single-company default template workbook."""
    src = Path(source_path).expanduser().resolve()
    dst = Path(output_path).expanduser().resolve()
    verification = verify_baseline_structure(src, before_ts=before_ts, company_tag=company_tag)
    if not verification["ok"]:
        return {"status": "error", "verification": verification, "output": None}

    prof = _clone_style_profile(src, company_tag)
    f_en = prof["font_en"]
    f_sz = prof["font_size"]
    h_fill = prof["header_fill"]

    wb = openpyxl.Workbook()
    # 1. README & Changelog
    ws_readme = wb.active
    ws_readme.title = "README & Changelog"
    ws_readme.views.sheetView[0].showGridLines = False
    headers_readme = ["Version", "Date", "Baseline", "Scope", "Key Driver Changes & Rationale", "Consensus Diff Rationale"]
    for idx, text in enumerate(headers_readme, start=1):
        c = ws_readme.cell(1, idx, text)
        _style_header(c, f_en, f_sz, h_fill, align="left")
        ws_readme.column_dimensions[get_column_letter(idx)].width = 24.0 if idx >= 4 else 14.0
    ws_readme.cell(2, 1, "v01")
    ws_readme.cell(2, 2, "<YYYY-MM-DD>")
    ws_readme.cell(2, 3, "default_model_template.xlsx")
    ws_readme.cell(2, 4, "Initial single-company standard template (desensitized)")
    for col in range(1, 7):
        _style_cell(ws_readme.cell(2, col), font_en=f_en, font_size=f_sz)
    ws_readme.freeze_panes = "A2"

    # 2. Valuation
    ws_val = wb.create_sheet("Valuation")
    ws_val.views.sheetView[0].showGridLines = False
    ws_val["A1"] = "(Valuation Summary)"
    _style_header(ws_val["A1"], f_en, f_sz, h_fill, align="left")
    for idx, label in enumerate(["Metric", "FY1E", "FY2E", "FY3E", "Notes"], start=2):
        _style_header(ws_val.cell(1, idx, label), f_en, f_sz, h_fill)
    ws_val.cell(3, 2, "Total Revenue")
    ws_val.cell(3, 3, "=Model!G6")
    ws_val.cell(3, 4, "=Model!H6")
    ws_val.cell(3, 5, "=Model!I6")
    ws_val.cell(4, 2, "Net Income")
    ws_val.cell(4, 3, "=Model!G14")
    ws_val.cell(4, 4, "=Model!H14")
    ws_val.cell(4, 5, "=Model!I14")
    ws_val.cell(5, 2, "Diluted EPS")
    ws_val.cell(5, 3, "=Model!G16")
    ws_val.cell(5, 4, "=Model!H16")
    ws_val.cell(5, 5, "=Model!I16")
    ws_val.cell(6, 2, "Target P/E (x)")
    ws_val.cell(6, 4, None)
    _style_cell(ws_val.cell(6, 4), font_en=f_en, font_size=f_sz, color=model_style.ASSUMP_FONT, fill_rgb=model_style.ASSUMP_FILL, hair=True, num_fmt=model_style.FMT_MULT)
    ws_val.cell(7, 2, "Implied Target Price")
    ws_val.cell(7, 4, "=D5*D6")
    _style_cell(ws_val.cell(7, 4), font_en=f_en, font_size=f_sz, color=model_style.TARGET_PRICE_FONT, bold=True, num_fmt=model_style.FMT_DEC2)
    for r in (3, 4, 5):
        _style_cell(ws_val.cell(r, 2), font_en=f_en, font_size=f_sz, bold=True)
        for c_i in (3, 4, 5):
            fmt = model_style.FMT_DEC2 if r == 5 else model_style.FMT_INT
            _style_cell(ws_val.cell(r, c_i), font_en=f_en, font_size=f_sz, color=model_style.LINK_FONT, num_fmt=fmt)
    ws_val.column_dimensions["A"].width = 2.0
    ws_val.column_dimensions["B"].width = 24.0
    for col_l in ("C", "D", "E"):
        ws_val.column_dimensions[col_l].width = 12.0
    ws_val.column_dimensions["F"].width = 24.0
    ws_val.freeze_panes = "C2"

    # 3. Key Metrics
    ws_km = wb.create_sheet("Key Metrics")
    ws_km.views.sheetView[0].showGridLines = False
    ws_km["A1"] = prof["unit_text"]
    _style_header(ws_km["A1"], f_en, f_sz, h_fill, align="left")
    periods_ann = ["2024", "2025", "2026E", "2027E", "2028E"]
    for idx, p in enumerate(periods_ann, start=5):
        _style_header(ws_km.cell(1, idx, p), f_en, f_sz, h_fill)
    ws_km.cell(3, 4, "Total Revenue")
    ws_km.cell(4, 4, "Operating Income")
    ws_km.cell(5, 4, "Net Income")
    ws_km.cell(6, 4, "Diluted EPS")
    for r, m_row, fmt in ((3, 6, model_style.FMT_INT), (4, 12, model_style.FMT_INT), (5, 14, model_style.FMT_INT), (6, 16, model_style.FMT_DEC2)):
        _style_cell(ws_km.cell(r, 4), font_en=f_en, font_size=f_sz, bold=True)
        for idx in range(5, 10):
            col_l = get_column_letter(idx)
            c = ws_km.cell(r, idx, f"=Model!{col_l}{m_row}")
            _style_cell(c, font_en=f_en, font_size=f_sz, color=model_style.LINK_FONT, num_fmt=fmt)
    ws_km.column_dimensions["A"].width = 2.0
    ws_km.column_dimensions["B"].width = 2.0
    ws_km.column_dimensions["C"].width = 2.0
    ws_km.column_dimensions["D"].width = prof["label_col_width"]
    for idx in range(5, 10):
        ws_km.column_dimensions[get_column_letter(idx)].width = prof["period_col_width"]
    ws_km.freeze_panes = "E2"

    # 4. Model (core single-company template skeleton — zero company business numbers)
    ws_m = wb.create_sheet("Model")
    ws_m.views.sheetView[0].showGridLines = False
    ws_m["A1"] = prof["unit_text"]
    _style_header(ws_m["A1"], f_en, f_sz, h_fill, align="left")
    for c_i in (2, 3, 4):
        _style_header(ws_m.cell(1, c_i, None), f_en, f_sz, h_fill)
    periods_q = [
        "1Q25", "2Q25", "3Q25", "4Q25",
        "1Q26E", "2Q26E", "3Q26E", "4Q26E",
        "1Q27E", "2Q27E", "3Q27E", "4Q27E",
    ]
    for idx, p in enumerate(periods_ann, start=5):
        _style_header(ws_m.cell(1, idx, p), f_en, f_sz, h_fill)
    # Column J (10) = narrow spacer
    ws_m.column_dimensions["J"].width = model_style.DEFAULT_SPACER_COL_WIDTH
    for idx, p in enumerate(periods_q, start=11):
        _style_header(ws_m.cell(1, idx, p), f_en, f_sz, h_fill)
    note_col = 11 + len(periods_q)  # col 23 = W
    _style_header(ws_m.cell(1, note_col, "Notes"), f_en, f_sz, h_fill, align="left")
    ws_m.cell(2, 1, "FY ends Dec")
    _style_cell(ws_m.cell(2, 1), font_en=f_en, font_size=f_sz, italic=True)

    # Sections & skeleton formulas (all values blank — zero company numbers)
    _style_section(ws_m.cell(3, 1, "Income Statement"), f_en, f_sz, prof["sec_report_fill"])
    row_defs = [
        (4, "Segment 1 Revenue", "flow", False, False, model_style.FMT_INT),
        (5, "Segment 2 Revenue", "flow", False, False, model_style.FMT_INT),
        (6, "Total Revenue", "flow", True, False, model_style.FMT_INT),
        (7, "Revenue YoY %", "ratio", False, True, model_style.FMT_PCT),
        (8, "Cost of Revenue", "flow", False, False, model_style.FMT_INT),
        (9, "Gross Profit", "flow", True, False, model_style.FMT_INT),
        (10, "Gross Margin %", "ratio", False, True, model_style.FMT_PCT),
        (11, "Operating Expenses", "flow", False, False, model_style.FMT_INT),
        (12, "Operating Income", "flow", True, False, model_style.FMT_INT),
        (13, "Operating Margin %", "ratio", False, True, model_style.FMT_PCT),
        (14, "Net Income", "flow", True, False, model_style.FMT_INT),
        (15, "Diluted Shares (MM)", "ending_balance", False, False, model_style.FMT_DEC1),
        (16, "Diluted EPS", "eps", True, False, model_style.FMT_DEC2),
    ]
    for r, lbl, _rtype, is_bold, is_italic, fmt in row_defs:
        _style_cell(ws_m.cell(r, 4, lbl), font_en=f_en, font_size=f_sz, bold=is_bold, italic=is_italic)
        # Annual summary formulas for FY25 (F=6 -> K:N), FY26E (G=7 -> O:R), FY27E (H=8 -> S:V)
        for ann_col, q_start, q_end in ((6, "K", "N"), (7, "O", "R"), (8, "S", "V")):
            col_l = get_column_letter(ann_col)
            prev_l = get_column_letter(ann_col - 1)
            if r in (4, 5, 8, 11):
                ws_m.cell(r, ann_col, f"=SUM({q_start}{r}:{q_end}{r})")
            elif r == 6:
                ws_m.cell(r, ann_col, f"={col_l}4+{col_l}5")
            elif r == 7:
                ws_m.cell(r, ann_col, f"=IF({prev_l}6=0,0,{col_l}6/{prev_l}6-1)")
            elif r == 9:
                ws_m.cell(r, ann_col, f"={col_l}6-{col_l}8")
            elif r == 10:
                ws_m.cell(r, ann_col, f"=IF({col_l}6=0,0,{col_l}9/{col_l}6)")
            elif r == 12:
                ws_m.cell(r, ann_col, f"={col_l}9-{col_l}11")
            elif r == 13:
                ws_m.cell(r, ann_col, f"=IF({col_l}6=0,0,{col_l}12/{col_l}6)")
            elif r == 14:
                ws_m.cell(r, ann_col, f"=SUM({q_start}{r}:{q_end}{r})")
            elif r == 15:
                ws_m.cell(r, ann_col, f"={q_end}15")
            elif r == 16:
                ws_m.cell(r, ann_col, f"=IF({col_l}15=0,0,{col_l}14/{col_l}15)")
            _style_cell(ws_m.cell(r, ann_col), font_en=f_en, font_size=f_sz, bold=is_bold, italic=is_italic, num_fmt=fmt)

        # Quarterly structural formulas
        for q_col in range(11, 11 + len(periods_q)):
            col_l = get_column_letter(q_col)
            if r == 6:
                ws_m.cell(r, q_col, f"={col_l}4+{col_l}5")
            elif r == 9:
                ws_m.cell(r, q_col, f"={col_l}6-{col_l}8")
            elif r == 10:
                ws_m.cell(r, q_col, f"=IF({col_l}6=0,0,{col_l}9/{col_l}6)")
            elif r == 12:
                ws_m.cell(r, q_col, f"={col_l}9-{col_l}11")
            elif r == 13:
                ws_m.cell(r, q_col, f"=IF({col_l}6=0,0,{col_l}12/{col_l}6)")
            elif r == 16:
                ws_m.cell(r, q_col, f"=IF({col_l}15=0,0,{col_l}14/{col_l}15)")
            _style_cell(ws_m.cell(r, q_col), font_en=f_en, font_size=f_sz, bold=is_bold, italic=is_italic, num_fmt=fmt)

    # Segment section, Cash Flow section, Balance Sheet section headers
    _style_section(ws_m.cell(18, 1, "Segment Reporting"), f_en, f_sz, prof["sec_segment_fill"], model_style.SEC_SEGMENT_FONT)
    _style_section(ws_m.cell(23, 1, "Operating Drivers"), f_en, f_sz, prof["sec_submodel_fill"])
    _style_cell(ws_m.cell(24, 4, "Segment 1 YoY Growth %"), font_en=f_en, font_size=f_sz, italic=True)
    for q_col in range(15, 11 + len(periods_q)):
        _style_cell(
            ws_m.cell(24, q_col, None),
            font_en=f_en,
            font_size=f_sz,
            color=model_style.ASSUMP_FONT,
            italic=True,
            fill_rgb=prof["assump_fill"],
            hair=True,
            num_fmt=model_style.FMT_PCT,
        )
    _style_cell(ws_m.cell(24, note_col, "<Assumption source"), font_en=f_en, font_size=f_sz, color=model_style.SOURCE_NOTE_FONT)

    _style_section(ws_m.cell(26, 1, "Cash Flow Statement"), f_en, f_sz, prof["sec_report_fill"])
    _style_cell(ws_m.cell(27, 4, "Beginning Cash"), font_en=f_en, font_size=f_sz)
    _style_cell(ws_m.cell(28, 4, "Net Change in Cash"), font_en=f_en, font_size=f_sz)
    _style_cell(ws_m.cell(29, 4, "Ending Cash"), font_en=f_en, font_size=f_sz, bold=True)
    for q_col in range(12, 11 + len(periods_q)):
        prev_l = get_column_letter(q_col - 1)
        col_l = get_column_letter(q_col)
        ws_m.cell(27, q_col, f"={prev_l}29")
        ws_m.cell(29, q_col, f"={col_l}27+{col_l}28")

    _style_section(ws_m.cell(31, 1, "Balance Sheet"), f_en, f_sz, prof["sec_report_fill"])
    _style_cell(ws_m.cell(32, 4, "Total Assets"), font_en=f_en, font_size=f_sz, bold=True)
    _style_cell(ws_m.cell(33, 4, "Total Liabilities"), font_en=f_en, font_size=f_sz)
    _style_cell(ws_m.cell(34, 4, "Total Equity"), font_en=f_en, font_size=f_sz)
    _style_cell(ws_m.cell(35, 4, "Total Liabilities & Equity"), font_en=f_en, font_size=f_sz, bold=True)
    for col_i in (*range(5, 10), *range(11, 11 + len(periods_q))):
        col_l = get_column_letter(col_i)
        ws_m.cell(35, col_i, f"={col_l}33+{col_l}34")

    for c_i, w in ((1, 2.0), (2, 2.0), (3, 2.0), (4, prof["label_col_width"])):
        ws_m.column_dimensions[get_column_letter(c_i)].width = w
    for c_i in (*range(5, 10), *range(11, 11 + len(periods_q))):
        ws_m.column_dimensions[get_column_letter(c_i)].width = prof["period_col_width"]
    ws_m.column_dimensions[get_column_letter(note_col)].width = 24.0
    ws_m.freeze_panes = prof["freeze_panes"]

    # 5. Subs (business sub-model sheet)
    ws_subs = wb.create_sheet("Subs")
    ws_subs.views.sheetView[0].showGridLines = False
    ws_subs["A1"] = prof["unit_text"]
    _style_header(ws_subs["A1"], f_en, f_sz, h_fill, align="left")
    for idx, p in enumerate(periods_ann, start=5):
        _style_header(ws_subs.cell(1, idx, p), f_en, f_sz, h_fill)
    _style_section(ws_subs.cell(3, 1, "Sub-Model Breakdown"), f_en, f_sz, prof["sec_submodel_fill"])
    ws_subs.freeze_panes = "E2"

    # 6. vs Consensus (with isolated external snapshot column)
    ws_cons = wb.create_sheet("vs Consensus")
    ws_cons.views.sheetView[0].showGridLines = False
    cons_headers = [
        "Period",
        "Metric",
        "House",
        "Consensus Snapshot",
        "Diff %",
        "Source Type",
        "As-Of Date",
        "Rationale",
    ]
    for idx, text in enumerate(cons_headers, start=1):
        _style_header(ws_cons.cell(1, idx, text), f_en, f_sz, h_fill, align="left" if idx in (1, 2, 6, 7, 8) else "right")
        ws_cons.column_dimensions[get_column_letter(idx)].width = 20.0 if idx in (2, 6, 8) else 14.0
    sample_rows = [
        (2, "2026E", "Total Revenue", "=Model!G6"),
        (3, "2026E", "Operating Income", "=Model!G12"),
        (4, "2026E", "Net Income", "=Model!G14"),
        (5, "2026E", "Diluted EPS", "=Model!G16"),
        (6, "2027E", "Total Revenue", "=Model!H6"),
        (7, "2027E", "Net Income", "=Model!H14"),
    ]
    for r, per, met, house_f in sample_rows:
        _style_cell(ws_cons.cell(r, 1, per), font_en=f_en, font_size=f_sz)
        _style_cell(ws_cons.cell(r, 2, met), font_en=f_en, font_size=f_sz)
        _style_cell(ws_cons.cell(r, 3, house_f), font_en=f_en, font_size=f_sz, color=model_style.LINK_FONT, num_fmt=model_style.FMT_DEC2 if "EPS" in met else model_style.FMT_INT)
        # Snapshot cell: blue font, no fill (external_input)
        _style_cell(ws_cons.cell(r, 4, None), font_en=f_en, font_size=f_sz, color=model_style.ACTUAL_FONT, num_fmt=model_style.FMT_DEC2 if "EPS" in met else model_style.FMT_INT)
        _style_cell(ws_cons.cell(r, 5, f"=IF(OR(D{r}=\"\",D{r}=0),\"\",(C{r}-D{r})/ABS(D{r}))"), font_en=f_en, font_size=f_sz, italic=True, num_fmt=model_style.FMT_PCT)
        _style_cell(ws_cons.cell(r, 6, ""), font_en=f_en, font_size=f_sz, color=model_style.SOURCE_NOTE_FONT)
        _style_cell(ws_cons.cell(r, 7, ""), font_en=f_en, font_size=f_sz, color=model_style.SOURCE_NOTE_FONT)
        _style_cell(ws_cons.cell(r, 8, ""), font_en=f_en, font_size=f_sz)
    ws_cons.freeze_panes = "C2"

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".extract.tmp")
    wb.save(tmp)
    wb.close()
    _strip_calc_full_on_load(tmp)
    os.replace(tmp, dst)

    # Verify extracted template has zero external links, zero plugin calls, zero #REF!
    tpl_ok, tpl_msg = model_readable.check_workbook_readable(dst)
    tpl_wb = openpyxl.load_workbook(dst, data_only=False)
    ref_errors = 0
    plugin_calls = 0
    ext_refs = 0
    try:
        for ws in tpl_wb.worksheets:
            for _, _, cell in model_style._iter_stored_cells(ws):
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    if "#REF!" in cell.value.upper():
                        ref_errors += 1
                    if _PLUGIN_RE.search(cell.value):
                        plugin_calls += 1
                    if _EXT_REF_RE.search(cell.value):
                        ext_refs += 1
    finally:
        tpl_wb.close()

    return {
        "status": "ok" if (tpl_ok and ref_errors == 0 and plugin_calls == 0 and ext_refs == 0) else "error",
        "output": str(dst),
        "output_sha256": _file_sha256(dst),
        "sheets": ["README & Changelog", "Valuation", "Key Metrics", "Model", "Subs", "vs Consensus"],
        "template_readable": tpl_ok,
        "template_message": tpl_msg,
        "ref_errors": ref_errors,
        "plugin_calls": plugin_calls,
        "external_refs": ext_refs,
        "verification": verification,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify baseline workbook structure and extract desensitized default_model_template.xlsx."
    )
    parser.add_argument("baseline", help="Path to verified baseline .xlsx workbook")
    parser.add_argument("--out", default=None, help="Output path for default_model_template.xlsx")
    parser.add_argument("--company-tag", default=None, help="Optional company tag to profile (e.g. JD)")
    parser.add_argument("--before-ts", default=None, help="Optional ISO8601 cutoff timestamp")
    parser.add_argument("--verify-only", action="store_true", help="Only verify baseline structure without writing template")
    parser.add_argument("--report-out", default=None, help="Optional JSON report path")
    args = parser.parse_args(argv)

    if args.verify_only:
        res = verify_baseline_structure(
            args.baseline,
            before_ts=args.before_ts,
            company_tag=args.company_tag,
        )
        ok = bool(res.get("ok"))
    else:
        if not args.out:
            print(json.dumps({"status": "error", "message": "--out is required unless --verify-only"}))
            return 1
        res = extract_default_template(
            args.baseline,
            args.out,
            company_tag=args.company_tag,
            before_ts=args.before_ts,
        )
        ok = res.get("status") == "ok"

    text = json.dumps(res, indent=2, ensure_ascii=False)
    if args.report_out:
        rp = Path(args.report_out).expanduser().resolve()
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
