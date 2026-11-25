#!/usr/bin/env python3
"""Mechanical Excel style helpers, workbook style profiler, and lineage recorder.

Standalone script (stdlib + openpyxl + lxml only) runnable inside a sandbox with
``python -I`` or imported by model build/update scripts.

Provides only mechanical utilities (no rigid cell-role or business-rule judgments):
- Default Dalton financial modeling style constants and cell styling helpers
- Adjacent period column style cloning
- Existing workbook style profile extraction (with fallback to defaults)
- Knowledge-layer period string normalization (e.g. ``2026E`` -> ``FY2026``)
- Plain bookkeeping record helper for ``model_lineage.json``

CLI usage:
    python model_style.py profile <workbook.xlsx> [--out style_profile.json]
    python model_style.py lineage --file <model_lineage.json> --output <out.xlsx> [--baseline <base.xlsx>] [--profile <profile.json>]
"""

from __future__ import annotations

import argparse
import colorsys
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any
import zipfile

from lxml import etree
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import column_index_from_string, get_column_letter

# ---------------------------------------------------------------------------
# 1. Default Style Constants (used when building from scratch or as fallback)
# ---------------------------------------------------------------------------

FONT_EN = "Arial"
FONT_ZH = "等线"
FONT_SIZE = 8

HEADER_FILL = "3366FF"
HEADER_FONT = "FFFFFF"

ACTUAL_FONT = "0000FF"

HIST_EST_FONT = "0000FF"
HIST_EST_FILL_GRAY = "EDEDED"
HIST_EST_FILL_YELLOW = "FFFFC8"

FORMULA_FONT = "000000"
TOTAL_BOLD = True
LINK_FONT = "008000"

ASSUMP_FONT = "0000FF"
ASSUMP_FILL = "FFFFC8"
ASSUMP_BORDER = "hair"

RATIO_ITALIC = True

SEC_REPORT_FILL = "99CCFF"
SEC_SEGMENT_FILL = "993300"
SEC_SEGMENT_FONT = "FFFFFF"
SEC_SUBMODEL_FILL = "FFCC99"

SOURCE_NOTE_FONT = "FF0000"
TARGET_PRICE_FONT = "C00000"

# Standard number formats
FMT_INT = r"_(#,##0_);\(#,##0\);_(?\-?_);@"
FMT_DEC1 = r"_(#,##0.0_);\(#,##0.0\);_(?\-?_);@"
FMT_DEC2 = r"_(#,##0.00_);\(#,##0.00\);_(?\-?_);@"
FMT_PCT = "0.0%"
FMT_PCT2 = "0.00%"
FMT_PP = '#,##0.0" pp"'
FMT_MULT = '#,##0.0" x"'
FMT_YM = 'yy"年"m"月"'

# Default column widths
DEFAULT_INDENT_COL_WIDTHS = (2.0, 2.0, 2.0)
DEFAULT_LABEL_COL_WIDTH = 28.0
DEFAULT_PERIOD_COL_WIDTH = 8.5
DEFAULT_SPACER_COL_WIDTH = 1.2

ALLOWED_DEFAULT_FONTS: tuple[str, ...] = (FONT_EN, FONT_ZH)

# OOXML theme parsing constants
_DRAWING_MAIN_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)
_THEME_SLOTS = (
    "lt1", "dk1", "lt2", "dk2",
    "accent1", "accent2", "accent3", "accent4", "accent5", "accent6",
    "hlink", "folHlink",
)
_DEFAULT_THEME_PALETTE: dict[int, str] = {
    0: "FFFFFF", 1: "000000", 2: "E7E6E6", 3: "44546A",
    4: "4472C4", 5: "ED7D31", 6: "A5A5A5", 7: "FFC000",
    8: "5B9BD5", 9: "70AD47", 10: "0563C1", 11: "954F72",
}
_INDEXED_COLORS: dict[int, str | None] = {
    0: "000000", 1: "FFFFFF", 2: "FF0000", 3: "00FF00", 4: "0000FF",
    5: "FFFF00", 8: "000000", 9: "FFFFFF", 10: "FF0000", 12: "0000FF",
    17: "008000", 22: "C0C0C0", 23: "808080", 64: None, 65: None,
}

_HEX6_RE = re.compile(r"^[0-9A-F]{6}$")
_HEX8_RE = re.compile(r"^[0-9A-F]{8}$")
_ZH_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")

# Knowledge-layer period normalization patterns
_CAGR_DELTA_RE = re.compile(
    r"(?:\bCAGR\b|\bYoY\b|\bQoQ\b|\bMoM\b|\bvs\.?\b|\bChg\b|\bDiff\b|Δ|同比|环比)",
    re.IGNORECASE,
)
_Q_FIRST_RE = re.compile(r"^\s*Q?([1-4])Q\s*['\-/\s]?(\d{2}|\d{4})\s*([AEFP])?\s*$", re.IGNORECASE)
_Q_PREFIX_RE = re.compile(r"^\s*Q([1-4])\s*['\-/\s]?(\d{2}|\d{4})\s*([AEFP])?\s*$", re.IGNORECASE)
_Y_FIRST_Q_RE = re.compile(r"^\s*(?:FY|CY)?\s*(\d{2}|\d{4})\s*['\-/\s]?Q([1-4])\s*([AEFP])?\s*$", re.IGNORECASE)
_ANNUAL_RE = re.compile(r"^\s*(?:FY|CY)?\s*'?((?:19|20)\d{2}|\d{2})\s*([AEFP])?\s*$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 2. Color Resolution (Theme + Tint -> RGB)
# ---------------------------------------------------------------------------


def apply_tint(rgb_hex: str, tint: float) -> str:
    """Apply OOXML tint adjustment to a 6-digit uppercase RGB hex string."""
    if not tint or abs(tint) < 1e-9:
        return rgb_hex.upper()
    r = int(rgb_hex[0:2], 16) / 255.0
    g = int(rgb_hex[2:4], 16) / 255.0
    b = int(rgb_hex[4:6], 16) / 255.0
    h, l_val, s = colorsys.rgb_to_hls(r, g, b)
    l_val = l_val * (1.0 + tint) if tint < 0.0 else l_val * (1.0 - tint) + tint
    l_val = max(0.0, min(1.0, l_val))
    r2, g2, b2 = colorsys.hls_to_rgb(h, l_val, s)
    return f"{round(r2 * 255):02X}{round(g2 * 255):02X}{round(b2 * 255):02X}"


def extract_theme_palette(xlsx_path: str | Path) -> dict[int, str]:
    """Read ``xl/theme/theme1.xml`` ``<a:clrScheme>`` into ``{theme_idx: RGB}``."""
    palette = dict(_DEFAULT_THEME_PALETTE)
    path = Path(xlsx_path)
    if not path.is_file():
        return palette
    try:
        with zipfile.ZipFile(path, "r") as zf:
            if "xl/theme/theme1.xml" not in zf.namelist():
                return palette
            root = etree.fromstring(zf.read("xl/theme/theme1.xml"), _XML_PARSER)
    except Exception:
        return palette

    clr_scheme = root.find(f".//{{{_DRAWING_MAIN_NS}}}clrScheme")
    if clr_scheme is None:
        return palette

    for idx, slot in enumerate(_THEME_SLOTS):
        el = clr_scheme.find(f"{{{_DRAWING_MAIN_NS}}}{slot}")
        if el is None:
            continue
        srgb = el.find(f"{{{_DRAWING_MAIN_NS}}}srgbClr")
        if srgb is not None and srgb.get("val"):
            val = srgb.get("val", "").strip().upper()
            if _HEX6_RE.match(val):
                palette[idx] = val
                continue
        sys_clr = el.find(f"{{{_DRAWING_MAIN_NS}}}sysClr")
        if sys_clr is not None:
            val = (sys_clr.get("lastClr") or "").strip().upper()
            if _HEX6_RE.match(val):
                palette[idx] = val
    return palette


def resolve_color_rgb(
    color: Any,
    palette: dict[int, str] | None = None,
    *,
    is_fill: bool = False,
) -> str | None:
    """Resolve an openpyxl ``Color`` object (rgb / theme+tint / indexed) to 6-hex RGB."""
    if color is None:
        return None
    ctype = getattr(color, "type", None)
    if ctype == "rgb":
        raw = getattr(color, "rgb", None)
        if not isinstance(raw, str):
            return None
        s = raw.strip().upper()
        if is_fill and s == "00000000":
            return None
        if _HEX6_RE.match(s):
            return s
        if _HEX8_RE.match(s):
            return s[2:]
        return None
    if ctype == "theme":
        theme_idx = getattr(color, "theme", None)
        if not isinstance(theme_idx, int):
            return None
        pal = palette if palette is not None else _DEFAULT_THEME_PALETTE
        base = pal.get(theme_idx)
        if base is None:
            return None
        tint = float(getattr(color, "tint", 0.0) or 0.0)
        return apply_tint(base, tint)
    if ctype == "indexed":
        idx = getattr(color, "indexed", None)
        return _INDEXED_COLORS.get(idx) if isinstance(idx, int) else None
    return None


def get_cell_font_rgb(cell: Any, palette: dict[int, str] | None = None) -> str:
    """Return resolved 6-hex font RGB, treating unset/automatic as ``000000``."""
    font = getattr(cell, "font", None)
    if font is None or font.color is None:
        return FORMULA_FONT
    resolved = resolve_color_rgb(font.color, palette, is_fill=False)
    return resolved if resolved is not None else FORMULA_FONT


def get_cell_fill_rgb(cell: Any, palette: dict[int, str] | None = None) -> str | None:
    """Return resolved 6-hex fill RGB for solid fills, or ``None`` if no fill."""
    fill = getattr(cell, "fill", None)
    if fill is None or not getattr(fill, "fill_type", None) or fill.fill_type == "none":
        return None
    return resolve_color_rgb(getattr(fill, "fgColor", None), palette, is_fill=True)


# ---------------------------------------------------------------------------
# 3. Knowledge-Layer Period Normalization Helper
# ---------------------------------------------------------------------------


def _expand_year(raw_yr: str) -> int:
    val = int(raw_yr)
    if len(raw_yr) == 4:
        return val
    return 2000 + val if val <= 79 else 1900 + val


def normalize_period_for_knowledge(raw_label: Any) -> str | None:
    """Convert workbook period header to ``common.schema.json`` period format.

    Examples: ``2026`` / ``2026E`` / ``2026F`` -> ``FY2026``;
    ``4Q26E`` / ``2026Q4E`` -> ``4Q26``; ``CAGR 25-28E`` -> ``None``.
    """
    if raw_label is None or isinstance(raw_label, bool):
        return None
    if isinstance(raw_label, (int, float)):
        if float(raw_label).is_integer() and 1990 <= int(raw_label) <= 2100:
            return f"FY{int(raw_label)}"
        return None
    text = " ".join(str(raw_label).strip().split())
    if not text or _CAGR_DELTA_RE.search(text):
        return None
    for rx in (_Q_FIRST_RE, _Q_PREFIX_RE):
        m = rx.match(text)
        if m:
            q = int(m.group(1))
            yr = _expand_year(m.group(2))
            return f"{q}Q{yr % 100:02d}"
    m = _Y_FIRST_Q_RE.match(text)
    if m:
        yr = _expand_year(m.group(1))
        q = int(m.group(2))
        return f"{q}Q{yr % 100:02d}"
    m = _ANNUAL_RE.match(text)
    if m:
        yr_str = m.group(1)
        suf = (m.group(2) or "").upper()
        if len(yr_str) == 2 and not (text.upper().startswith(("FY", "CY")) or suf):
            return None
        yr = _expand_year(yr_str)
        if 1990 <= yr <= 2100:
            return f"FY{yr}"
    return None


# ---------------------------------------------------------------------------
# 4. Default Style Profile & Existing Workbook Style Profiler
# ---------------------------------------------------------------------------


def default_style_profile() -> dict[str, Any]:
    """Return the default style profile (used for new models or profiler fallback)."""
    return {
        "source": "default",
        "workbook_defaults": {
            "dominant_font_en": FONT_EN,
            "dominant_font_zh": FONT_ZH,
            "allowed_fonts": list(ALLOWED_DEFAULT_FONTS),
            "dominant_font_size": float(FONT_SIZE),
            "header_fill": HEADER_FILL,
            "header_font_color": HEADER_FONT,
            "actual_font_color": ACTUAL_FONT,
            "formula_font_color": FORMULA_FONT,
            "link_font_color": LINK_FONT,
            "source_note_font_color": SOURCE_NOTE_FONT,
            "target_price_font_color": TARGET_PRICE_FONT,
            "assumption_style": {
                "font_color": ASSUMP_FONT,
                "fill_color": ASSUMP_FILL,
                "border_style": ASSUMP_BORDER,
            },
            "historical_estimate_fills": [HIST_EST_FILL_GRAY, HIST_EST_FILL_YELLOW],
            "section_fills": [SEC_REPORT_FILL, SEC_SEGMENT_FILL, SEC_SUBMODEL_FILL],
            "number_formats": [FMT_INT, FMT_DEC1, FMT_DEC2, FMT_PCT, FMT_PCT2, FMT_PP, FMT_MULT],
            "gridlines_hidden": True,
        },
        "sheet_overrides": {},
    }


def _top_mode(counts: dict[str, int], min_samples: int = 3) -> str | None:
    if not counts or sum(counts.values()) < min_samples:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def profile_workbook_style(xlsx_path: str | Path) -> dict[str, Any]:
    """Extract observed styles per sheet & block from an existing workbook.

    Falls back cleanly to ``default_style_profile()`` if the file cannot be read,
    so callers always receive a usable profile dict.
    """
    path = Path(xlsx_path)
    if not path.is_file():
        prof = default_style_profile()
        prof["fallback_reason"] = f"file not found: {path}"
        return prof

    try:
        palette = extract_theme_palette(path)
        wb = openpyxl.load_workbook(path, data_only=False)
    except Exception as exc:
        prof = default_style_profile()
        prof["fallback_reason"] = f"failed to read workbook: {exc}"
        return prof

    g_fonts: dict[str, int] = {}
    g_sizes: dict[str, int] = {}
    g_fills: dict[str, int] = {}
    g_num_font_colors: dict[str, int] = {}
    g_link_colors: dict[str, int] = {}
    g_assump_fills: dict[str, int] = {}
    g_assump_borders: dict[str, int] = {}
    g_nfmts: set[str] = set()

    sheet_overrides: dict[str, Any] = {}

    for ws in wb.worksheets:
        state = ws.sheet_state or "visible"
        s_fonts: dict[str, int] = {}
        s_sizes: dict[str, int] = {}
        s_fills: set[str] = set()
        s_nfmts: set[str] = set()
        s_section_fills: list[str] = []
        blocks: list[dict[str, Any]] = []
        non_empty = 0
        formula_count = 0
        numeric_count = 0

        if state == "visible":
            for row_cells in ws.iter_rows():
                r_idx = row_cells[0].row
                c1 = row_cells[0]
                c1_fill = get_cell_fill_rgb(c1, palette)
                if c1_fill and isinstance(c1.value, str) and c1.value.strip() and r_idx > 1:
                    if c1_fill not in s_section_fills:
                        s_section_fills.append(c1_fill)
                    if len(blocks) < 40:
                        blocks.append({
                            "row": r_idx,
                            "title": c1.value.strip()[:80],
                            "section_fill": c1_fill,
                        })

                for cell in row_cells:
                    v = cell.value
                    if v is None or (isinstance(v, str) and not v.strip()):
                        continue
                    non_empty += 1
                    f = cell.font
                    if f and f.name:
                        s_fonts[f.name] = s_fonts.get(f.name, 0) + 1
                        g_fonts[f.name] = g_fonts.get(f.name, 0) + 1
                    if f and f.size is not None:
                        sz = str(float(f.size))
                        s_sizes[sz] = s_sizes.get(sz, 0) + 1
                        g_sizes[sz] = g_sizes.get(sz, 0) + 1
                    fl_rgb = get_cell_fill_rgb(cell, palette)
                    if fl_rgb:
                        s_fills.add(fl_rgb)
                        g_fills[fl_rgb] = g_fills.get(fl_rgb, 0) + 1
                    fn_rgb = get_cell_font_rgb(cell, palette)
                    nf = str(cell.number_format or "General")
                    s_nfmts.add(nf)
                    g_nfmts.add(nf)

                    if isinstance(v, str) and v.startswith("="):
                        formula_count += 1
                        if "!" in v and fn_rgb != FORMULA_FONT:
                            g_link_colors[fn_rgb] = g_link_colors.get(fn_rgb, 0) + 1
                    elif isinstance(v, (int, float)) and not isinstance(v, bool):
                        numeric_count += 1
                        if fl_rgb is None and r_idx > 1:
                            g_num_font_colors[fn_rgb] = g_num_font_colors.get(fn_rgb, 0) + 1
                        elif fl_rgb and fn_rgb == ACTUAL_FONT:
                            g_assump_fills[fl_rgb] = g_assump_fills.get(fl_rgb, 0) + 1
                            b = cell.border
                            if b and b.left and b.left.style:
                                bs = b.left.style
                                g_assump_borders[bs] = g_assump_borders.get(bs, 0) + 1

        if state != "visible":
            sheet_type = "hidden_sheet"
        elif formula_count == 0 and numeric_count == 0 and non_empty <= 2:
            sheet_type = "blank_divider"
        else:
            sheet_type = "content_sheet"

        dom_size_str = _top_mode(s_sizes, min_samples=1)
        gridlines_hidden = bool(
            ws.views and ws.views.sheetView and ws.views.sheetView[0].showGridLines is False
        )
        col_widths = {
            str(k): round(float(dim.width), 2)
            for k, dim in ws.column_dimensions.items()
            if dim.width is not None
        }
        sheet_overrides[ws.title] = {
            "sheet_type": sheet_type,
            "sheet_state": state,
            "non_empty_cells": non_empty,
            "formula_cells": formula_count,
            "dominant_font_size": float(dom_size_str) if dom_size_str else None,
            "fonts_observed": sorted(s_fonts.keys()),
            "fills_observed": sorted(s_fills),
            "section_fills": s_section_fills,
            "number_formats_observed": sorted(s_nfmts),
            "freeze_panes": ws.freeze_panes,
            "gridlines_hidden": gridlines_hidden,
            "sample_col_widths": dict(list(col_widths.items())[:15]),
            "blocks": blocks,
        }

    dom_global_size_str = _top_mode(g_sizes, min_samples=1)
    defaults = default_style_profile()["workbook_defaults"]
    defaults.update({
        "allowed_fonts": sorted(set(ALLOWED_DEFAULT_FONTS) | {k for k, v in g_fonts.items() if v >= 5}),
        "dominant_font_size": float(dom_global_size_str) if dom_global_size_str else float(FONT_SIZE),
        "actual_font_color": _top_mode(g_num_font_colors) or ACTUAL_FONT,
        "link_font_color": _top_mode(g_link_colors) or LINK_FONT,
        "assumption_style": {
            "font_color": ASSUMP_FONT,
            "fill_color": _top_mode(g_assump_fills) or ASSUMP_FILL,
            "border_style": _top_mode(g_assump_borders) or ASSUMP_BORDER,
        },
        "number_formats": sorted(g_nfmts) or defaults["number_formats"],
    })

    return {
        "source": str(path),
        "sheet_count": len(wb.sheetnames),
        "theme_palette": {str(k): v for k, v in sorted(palette.items())},
        "workbook_defaults": defaults,
        "sheet_overrides": sheet_overrides,
    }


# ---------------------------------------------------------------------------
# 5. Mechanical Cell Styling & Adjacent Column Style Cloning
# ---------------------------------------------------------------------------


def _pick_font_name(value: Any, profile: dict[str, Any] | None = None) -> str:
    wb_def = (profile or {}).get("workbook_defaults") or {}
    if isinstance(value, str) and _ZH_CHAR_RE.search(value):
        return wb_def.get("dominant_font_zh") or FONT_ZH
    return wb_def.get("dominant_font_en") or FONT_EN


def apply_cell_style(
    cell: Any,
    role: str,
    *,
    profile: dict[str, Any] | None = None,
    sheet_name: str | None = None,
    is_percent: bool = False,
    is_total: bool = False,
    is_link: bool = False,
    is_ratio: bool = False,
    number_format: str | None = None,
    hist_est_variant: str = "yellow",
) -> None:
    """Apply Dalton default style (or extracted workbook profile) to an openpyxl cell."""
    active = profile or default_style_profile()
    wb_def = active.get("workbook_defaults") or {}
    sh_ov = (active.get("sheet_overrides") or {}).get(sheet_name or "", {})
    font_size = sh_ov.get("dominant_font_size") or wb_def.get("dominant_font_size") or FONT_SIZE
    font_name = _pick_font_name(cell.value, active)

    assump_cfg = wb_def.get("assumption_style") or {}
    assump_font_col = assump_cfg.get("font_color") or ASSUMP_FONT
    assump_fill_col = assump_cfg.get("fill_color") or ASSUMP_FILL
    assump_border = assump_cfg.get("border_style") or ASSUMP_BORDER

    if role == "header":
        cell.font = Font(name=font_name, size=font_size, bold=True, color=HEADER_FONT)
        cell.fill = PatternFill(fill_type="solid", fgColor=HEADER_FILL)
        cell.border = Border()
        cell.alignment = Alignment(horizontal="left" if cell.column == 1 else "right")
    elif role == "historical_actual":
        col = wb_def.get("actual_font_color") or ACTUAL_FONT
        cell.font = Font(name=font_name, size=font_size, bold=is_total, italic=is_ratio, color=col)
        cell.fill = PatternFill(fill_type=None)
        cell.border = Border()
    elif role == "historical_estimate":
        fill_hex = HIST_EST_FILL_GRAY if hist_est_variant == "gray" else HIST_EST_FILL_YELLOW
        cell.font = Font(
            name=font_name,
            size=font_size,
            bold=is_total,
            italic=is_percent or is_ratio,
            color=HIST_EST_FONT,
        )
        cell.fill = PatternFill(fill_type="solid", fgColor=fill_hex)
        if hist_est_variant == "yellow":
            side = Side(style=assump_border, color="000000")
            cell.border = Border(left=side, right=side, top=side, bottom=side)
        else:
            cell.border = Border()
    elif role == "driver_assumption":
        cell.font = Font(
            name=font_name,
            size=font_size,
            bold=is_total,
            italic=is_percent or is_ratio,
            color=assump_font_col,
        )
        cell.fill = PatternFill(fill_type="solid", fgColor=assump_fill_col)
        side = Side(style=assump_border, color="000000")
        cell.border = Border(left=side, right=side, top=side, bottom=side)
    elif role == "external_input":
        cell.font = Font(name=font_name, size=font_size, bold=is_total, italic=is_percent, color=ACTUAL_FONT)
        cell.fill = PatternFill(fill_type=None)
        cell.border = Border()
    elif role == "source_note":
        cell.font = Font(name=font_name, size=font_size, color=SOURCE_NOTE_FONT)
        cell.fill = PatternFill(fill_type=None)
        cell.border = Border()
    elif role == "target_price":
        cell.font = Font(name=font_name, size=font_size, bold=True, color=TARGET_PRICE_FONT)
        cell.fill = PatternFill(fill_type=None)
        cell.border = Border()
    elif role in ("sec_report", "sec_segment", "sec_submodel"):
        if role == "sec_report":
            cell.font = Font(name=font_name, size=font_size, bold=True, color=FORMULA_FONT)
            cell.fill = PatternFill(fill_type="solid", fgColor=SEC_REPORT_FILL)
        elif role == "sec_segment":
            cell.font = Font(name=font_name, size=font_size, bold=True, color=SEC_SEGMENT_FONT)
            cell.fill = PatternFill(fill_type="solid", fgColor=SEC_SEGMENT_FILL)
        else:
            cell.font = Font(name=font_name, size=font_size, bold=True, color=FORMULA_FONT)
            cell.fill = PatternFill(fill_type="solid", fgColor=SEC_SUBMODEL_FILL)
        cell.border = Border()
    else:
        color = (wb_def.get("link_font_color") or LINK_FONT) if is_link else FORMULA_FONT
        cell.font = Font(name=font_name, size=font_size, bold=is_total, italic=is_ratio, color=color)
        cell.fill = PatternFill(fill_type=None)
        cell.border = Border()

    if number_format is not None:
        cell.number_format = number_format
    elif is_percent or is_ratio:
        cell.number_format = FMT_PCT


def clone_period_column_style(
    ws: Any,
    src_col: int | str,
    dst_col: int | str,
    *,
    max_row: int | None = None,
    copy_width: bool = True,
    copy_outline: bool = False,
) -> None:
    """Clone cell styles and column width from an adjacent period column."""
    src_idx = column_index_from_string(src_col) if isinstance(src_col, str) else src_col
    dst_idx = column_index_from_string(dst_col) if isinstance(dst_col, str) else dst_col
    src_ltr = get_column_letter(src_idx)
    dst_ltr = get_column_letter(dst_idx)

    end_row = max_row if max_row is not None else (ws.max_row or 1)
    for r in range(1, end_row + 1):
        sc = ws.cell(r, src_idx)
        dc = ws.cell(r, dst_idx)
        if sc.has_style:
            dc.font = copy.copy(sc.font)
            dc.fill = copy.copy(sc.fill)
            dc.border = copy.copy(sc.border)
            dc.alignment = copy.copy(sc.alignment)
            dc.number_format = sc.number_format

    if copy_width and src_ltr in ws.column_dimensions:
        sdim = ws.column_dimensions[src_ltr]
        ddim = ws.column_dimensions[dst_ltr]
        if sdim.width is not None:
            ddim.width = sdim.width
        if copy_outline:
            ddim.outlineLevel = sdim.outlineLevel
            ddim.hidden = sdim.hidden


# ---------------------------------------------------------------------------
# 6. Plain Model Lineage Bookkeeping (`model_lineage.json`)
# ---------------------------------------------------------------------------


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def record_model_lineage(
    lineage_path: str | Path,
    *,
    output_path: str | Path,
    baseline_path: str | Path | None = None,
    profile_path: str | Path | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Append a plain bookkeeping entry (input, output, hashes, timestamp) to ``model_lineage.json``."""
    lp = Path(lineage_path)
    if lp.is_file():
        try:
            data = json.loads(lp.read_text(encoding="utf-8"))
        except Exception:
            data = {"records": []}
    else:
        data = {"records": []}

    out_p = Path(output_path)
    base_p = Path(baseline_path) if baseline_path else None
    entry = {
        "input_file": str(baseline_path) if baseline_path else None,
        "input_sha256": file_sha256(base_p) if (base_p and base_p.is_file()) else None,
        "output_file": str(output_path),
        "output_sha256": file_sha256(out_p) if out_p.is_file() else None,
        "profile_file": str(profile_path) if profile_path else None,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if note:
        entry["note"] = note

    records = list(data.get("records") or [])
    records.append(entry)
    data["latest"] = entry
    data["records"] = records

    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def load_model_lineage(lineage_path: str | Path) -> dict[str, Any]:
    return json.loads(Path(lineage_path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 7. CLI Entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mechanical Excel style profiler and model_lineage.json recorder."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prof = sub.add_parser("profile", help="Extract observed style profile JSON from an existing workbook")
    p_prof.add_argument("workbook", help="Source .xlsx workbook (read-only)")
    p_prof.add_argument("--out", help="Optional output JSON path")

    p_lin = sub.add_parser("lineage", help="Record input/output paths, hashes, and timestamp in model_lineage.json")
    p_lin.add_argument("--file", required=True, help="Path to model_lineage.json")
    p_lin.add_argument("--output", required=True, help="Output .xlsx path")
    p_lin.add_argument("--baseline", default=None, help="Optional input baseline .xlsx path")
    p_lin.add_argument("--profile", default=None, help="Optional style_profile.json path")
    p_lin.add_argument("--note", default=None, help="Optional note")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "profile":
        prof = profile_workbook_style(args.workbook)
        text = json.dumps(prof, indent=2, ensure_ascii=False)
        if args.out:
            out_p = Path(args.out)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(text + "\n", encoding="utf-8")
            print(json.dumps({
                "status": "ok",
                "out": str(out_p),
                "source": prof.get("source"),
                "sheet_count": prof.get("sheet_count", 0),
                "fallback_reason": prof.get("fallback_reason"),
            }, ensure_ascii=False))
        else:
            print(text)
        return 0

    if args.cmd == "lineage":
        res = record_model_lineage(
            args.file,
            output_path=args.output,
            baseline_path=args.baseline,
            profile_path=args.profile,
            note=args.note,
        )
        print(json.dumps(res["latest"], indent=2, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
