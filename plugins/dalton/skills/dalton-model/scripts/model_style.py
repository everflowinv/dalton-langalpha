#!/usr/bin/env python3
"""Mechanical Excel style helpers, workbook style profiler, parts-diff, and lineage recorder.

Standalone script (stdlib + openpyxl + lxml only) runnable inside a sandbox with
``python -I`` or imported by model build/update scripts.

Provides only mechanical utilities (no rigid cell-role or business-rule judgments):
- Default Dalton financial modeling style constants and cell styling helpers
- Adjacent period column style cloning (including protection, skipping MergedCell)
- Existing workbook style profile extraction (with fallback to defaults)
- Read-only OOXML package parts diff (``parts-diff``) to detect dropped media,
  drawings, charts, external links, ``<extLst>`` / x14 conditional formatting,
  data validations, and sparklines
- Knowledge-layer period string normalization (e.g. ``2026E`` -> ``FY2026``)
- Plain bookkeeping record helper for ``model_lineage.json``

CLI usage:
    python model_style.py profile <workbook.xlsx> [--out style_profile.json]
    python model_style.py parts-diff <orig.xlsx> <new.xlsx> [--out parts_diff.json]
    python model_style.py lineage --file <model_lineage.json> --output <out.xlsx> [--baseline <base.xlsx>] [--profile <profile.json>]
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import colorsys
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import sys
from typing import Any
import zipfile

from lxml import etree
import openpyxl
from openpyxl.cell.cell import MergedCell
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

# OOXML namespaces & parsing constants
_SML_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_DOC_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_X14_NS = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
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
_KNOWN_SECTION_FILL_TO_ROLE: dict[str, str] = {
    SEC_REPORT_FILL: "sec_report",
    SEC_SEGMENT_FILL: "sec_segment",
    SEC_SUBMODEL_FILL: "sec_submodel",
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


def _iter_stored_cells(ws: Any) -> list[tuple[int, int, Any]]:
    """Iterate only cells actually stored in an openpyxl worksheet (avoids sparse explosion)."""
    raw = getattr(ws, "_cells", None)
    if isinstance(raw, dict):
        return [(r, c, cell) for (r, c), cell in sorted(raw.items())]
    out: list[tuple[int, int, Any]] = []
    for row in ws.iter_rows():
        for cell in row:
            out.append((cell.row, cell.column, cell))
    return out


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


def _default_section_styles() -> dict[str, dict[str, str]]:
    return {
        "sec_report": {"fill_color": SEC_REPORT_FILL, "font_color": FORMULA_FONT},
        "sec_segment": {"fill_color": SEC_SEGMENT_FILL, "font_color": SEC_SEGMENT_FONT},
        "sec_submodel": {"fill_color": SEC_SUBMODEL_FILL, "font_color": FORMULA_FONT},
    }


def _build_section_styles_from_observed(
    observed_pairs: list[tuple[str, str]],
) -> dict[str, dict[str, str]]:
    """Map observed column-A section colors to roles ONLY by known default fill match.

    Unrecognized custom section fills are NOT assigned to fixed roles by order;
    they remain in ``section_fills`` and ``blocks`` for the caller to pick explicitly.
    """
    styles = _default_section_styles()
    seen_roles: set[str] = set()
    for fill_rgb, font_rgb in observed_pairs:
        role = _KNOWN_SECTION_FILL_TO_ROLE.get(fill_rgb.upper())
        if role is not None and role not in seen_roles:
            styles[role] = {"fill_color": fill_rgb.upper(), "font_color": font_rgb.upper()}
            seen_roles.add(role)
    return styles


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
            "section_styles": _default_section_styles(),
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

    Iterates only actually stored cells (sparse-safe) and records observed
    dominant English/Chinese fonts, header fill/font, section bar fills/blocks,
    and number formats. Known default section colors map to their corresponding
    roles; unrecognized section fills stay in ``section_fills`` and ``blocks``
    for explicit selection. Falls back cleanly to ``default_style_profile()``
    if the file cannot be read.
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
    g_fonts_en: dict[str, int] = {}
    g_fonts_zh: dict[str, int] = {}
    g_sizes: dict[str, int] = {}
    g_fills: dict[str, int] = {}
    g_header_fills: dict[str, int] = {}
    g_header_fonts: dict[str, int] = {}
    g_num_font_colors: dict[str, int] = {}
    g_link_colors: dict[str, int] = {}
    g_assump_fills: dict[str, int] = {}
    g_assump_borders: dict[str, int] = {}
    g_sec_pairs_counts: dict[tuple[str, str], int] = {}
    g_nfmts: set[str] = set()

    sheet_overrides: dict[str, Any] = {}

    for ws in wb.worksheets:
        state = ws.sheet_state or "visible"
        s_fonts: dict[str, int] = {}
        s_fonts_en: dict[str, int] = {}
        s_fonts_zh: dict[str, int] = {}
        s_sizes: dict[str, int] = {}
        s_fills: set[str] = set()
        s_header_fills: dict[str, int] = {}
        s_header_fonts: dict[str, int] = {}
        s_nfmts: set[str] = set()
        s_section_fills: list[str] = []
        s_sec_pairs: list[tuple[str, str]] = []
        blocks: list[dict[str, Any]] = []
        non_empty = 0
        formula_count = 0
        numeric_count = 0

        if state == "visible":
            for r_idx, c_idx, cell in _iter_stored_cells(ws):
                v = cell.value
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                non_empty += 1

                fl_rgb = get_cell_fill_rgb(cell, palette)
                fn_rgb = get_cell_font_rgb(cell, palette)

                if c_idx == 1 and fl_rgb and isinstance(v, str) and v.strip() and r_idx > 1:
                    if fl_rgb not in s_section_fills:
                        s_section_fills.append(fl_rgb)
                        s_sec_pairs.append((fl_rgb, fn_rgb))
                    g_sec_pairs_counts[(fl_rgb, fn_rgb)] = g_sec_pairs_counts.get((fl_rgb, fn_rgb), 0) + 1
                    if len(blocks) < 40:
                        blocks.append({
                            "row": r_idx,
                            "title": v.strip()[:80],
                            "section_fill": fl_rgb,
                            "section_font": fn_rgb,
                        })

                if r_idx == 1 and fl_rgb:
                    s_header_fills[fl_rgb] = s_header_fills.get(fl_rgb, 0) + 1
                    g_header_fills[fl_rgb] = g_header_fills.get(fl_rgb, 0) + 1
                    s_header_fonts[fn_rgb] = s_header_fonts.get(fn_rgb, 0) + 1
                    g_header_fonts[fn_rgb] = g_header_fonts.get(fn_rgb, 0) + 1

                f = cell.font
                if f and f.name:
                    s_fonts[f.name] = s_fonts.get(f.name, 0) + 1
                    g_fonts[f.name] = g_fonts.get(f.name, 0) + 1
                    if isinstance(v, str) and _ZH_CHAR_RE.search(v):
                        s_fonts_zh[f.name] = s_fonts_zh.get(f.name, 0) + 1
                        g_fonts_zh[f.name] = g_fonts_zh.get(f.name, 0) + 1
                    else:
                        s_fonts_en[f.name] = s_fonts_en.get(f.name, 0) + 1
                        g_fonts_en[f.name] = g_fonts_en.get(f.name, 0) + 1
                if f and f.size is not None:
                    sz = str(float(f.size))
                    s_sizes[sz] = s_sizes.get(sz, 0) + 1
                    g_sizes[sz] = g_sizes.get(sz, 0) + 1
                if fl_rgb:
                    s_fills.add(fl_rgb)
                    g_fills[fl_rgb] = g_fills.get(fl_rgb, 0) + 1
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
        sheet_ov: dict[str, Any] = {
            "sheet_type": sheet_type,
            "sheet_state": state,
            "non_empty_cells": non_empty,
            "formula_cells": formula_count,
            "dominant_font_size": float(dom_size_str) if dom_size_str else None,
            "dominant_font_en": _top_mode(s_fonts_en, min_samples=1),
            "dominant_font_zh": _top_mode(s_fonts_zh, min_samples=1),
            "header_fill": _top_mode(s_header_fills, min_samples=1),
            "header_font_color": _top_mode(s_header_fonts, min_samples=1),
            "fonts_observed": sorted(s_fonts.keys()),
            "fills_observed": sorted(s_fills),
            "section_fills": s_section_fills,
            "number_formats_observed": sorted(s_nfmts),
            "freeze_panes": ws.freeze_panes,
            "gridlines_hidden": gridlines_hidden,
            "sample_col_widths": dict(list(col_widths.items())[:15]),
            "blocks": blocks,
        }
        if s_sec_pairs:
            sheet_ov["section_styles"] = _build_section_styles_from_observed(s_sec_pairs)
        sheet_overrides[ws.title] = sheet_ov

    dom_global_size_str = _top_mode(g_sizes, min_samples=1)
    top_sec_pairs = [
        pair for pair, _ in sorted(g_sec_pairs_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    observed_sec_fills: list[str] = []
    for fl, _ in top_sec_pairs:
        if fl not in observed_sec_fills:
            observed_sec_fills.append(fl)

    defaults = default_style_profile()["workbook_defaults"]
    defaults.update({
        "dominant_font_en": _top_mode(g_fonts_en, min_samples=1) or FONT_EN,
        "dominant_font_zh": _top_mode(g_fonts_zh, min_samples=1) or FONT_ZH,
        "allowed_fonts": sorted(set(ALLOWED_DEFAULT_FONTS) | {k for k, v in g_fonts.items() if v >= 5}),
        "dominant_font_size": float(dom_global_size_str) if dom_global_size_str else float(FONT_SIZE),
        "header_fill": _top_mode(g_header_fills, min_samples=1) or HEADER_FILL,
        "header_font_color": _top_mode(g_header_fonts, min_samples=1) or HEADER_FONT,
        "actual_font_color": _top_mode(g_num_font_colors) or ACTUAL_FONT,
        "link_font_color": _top_mode(g_link_colors) or LINK_FONT,
        "assumption_style": {
            "font_color": ASSUMP_FONT,
            "fill_color": _top_mode(g_assump_fills) or ASSUMP_FILL,
            "border_style": _top_mode(g_assump_borders) or ASSUMP_BORDER,
        },
        "section_fills": observed_sec_fills or defaults["section_fills"],
        "section_styles": _build_section_styles_from_observed(top_sec_pairs),
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


def _pick_font_name(
    value: Any,
    profile: dict[str, Any] | None = None,
    sheet_name: str | None = None,
) -> str:
    wb_def = (profile or {}).get("workbook_defaults") or {}
    sh_ov = ((profile or {}).get("sheet_overrides") or {}).get(sheet_name or "", {})
    if isinstance(value, str) and _ZH_CHAR_RE.search(value):
        return sh_ov.get("dominant_font_zh") or wb_def.get("dominant_font_zh") or FONT_ZH
    return sh_ov.get("dominant_font_en") or wb_def.get("dominant_font_en") or FONT_EN


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
    """Apply Dalton default style (or extracted workbook profile) to an openpyxl cell.

    Supported ``role`` values:
    - ``"header"``: period header row / A1 unit cell (uses profile header fill/font when given).
    - ``"historical_actual"``: reported historical actual (blue font, no fill/border).
    - ``"historical_estimate"``: historical estimate (``hist_est_variant="yellow"`` or ``"gray"``).
    - ``"driver_assumption"``: forecast driver assumptions AND direct management guidance amounts
      (blue font, pale yellow fill, hair border).
    - ``"external_input"``: static reference input without assumption fill/border (blue font).
    - ``"source_note"``: rightmost source/notes column (red font).
    - ``"target_price"``: valuation target price output (dark red bold).
    - ``"sec_report"`` / ``"sec_segment"`` / ``"sec_submodel"``: section bar headers
      (``section_styles`` in a profile only maps known default colors and does not
      infer custom section roles; when maintaining an existing sheet, locate matching
      header rows via ``profile``'s ``blocks`` and copy reference cell styles or use
      ``clone_period_column_style``).
    - ``"formula"`` (or any other value such as ``"financial_output"``): same-sheet formula
      (black), cross-sheet link (``is_link=True``, green), derived ratio (``is_ratio=True``,
      italic), subtotal/total (``is_total=True``, bold).
    """
    active = profile or default_style_profile()
    wb_def = active.get("workbook_defaults") or {}
    sh_ov = (active.get("sheet_overrides") or {}).get(sheet_name or "", {})
    font_size = sh_ov.get("dominant_font_size") or wb_def.get("dominant_font_size") or FONT_SIZE
    font_name = _pick_font_name(cell.value, active, sheet_name)

    assump_cfg = wb_def.get("assumption_style") or {}
    assump_font_col = assump_cfg.get("font_color") or ASSUMP_FONT
    assump_fill_col = assump_cfg.get("fill_color") or ASSUMP_FILL
    assump_border = assump_cfg.get("border_style") or ASSUMP_BORDER

    if role == "header":
        hdr_fill = sh_ov.get("header_fill") or wb_def.get("header_fill") or HEADER_FILL
        hdr_font = sh_ov.get("header_font_color") or wb_def.get("header_font_color") or HEADER_FONT
        cell.font = Font(name=font_name, size=font_size, bold=True, color=hdr_font)
        cell.fill = PatternFill(fill_type="solid", fgColor=hdr_fill)
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
        col = wb_def.get("actual_font_color") or ACTUAL_FONT
        cell.font = Font(name=font_name, size=font_size, bold=is_total, italic=is_percent, color=col)
        cell.fill = PatternFill(fill_type=None)
        cell.border = Border()
    elif role == "source_note":
        col = wb_def.get("source_note_font_color") or SOURCE_NOTE_FONT
        cell.font = Font(name=font_name, size=font_size, color=col)
        cell.fill = PatternFill(fill_type=None)
        cell.border = Border()
    elif role == "target_price":
        col = wb_def.get("target_price_font_color") or TARGET_PRICE_FONT
        cell.font = Font(name=font_name, size=font_size, bold=True, color=col)
        cell.fill = PatternFill(fill_type=None)
        cell.border = Border()
    elif role in ("sec_report", "sec_segment", "sec_submodel"):
        # section_styles only maps known default colors; for existing sheets with custom
        # section fills, locate reference rows via profile's blocks and copy cell styles.
        sh_sec = (sh_ov.get("section_styles") or {}).get(role)
        wb_sec = (wb_def.get("section_styles") or {}).get(role)
        default_sec = _default_section_styles()[role]
        sec_cfg = sh_sec or wb_sec or default_sec
        cell.font = Font(
            name=font_name,
            size=font_size,
            bold=True,
            color=sec_cfg.get("font_color") or default_sec["font_color"],
        )
        cell.fill = PatternFill(
            fill_type="solid",
            fgColor=sec_cfg.get("fill_color") or default_sec["fill_color"],
        )
        cell.border = Border()
    else:
        color = (wb_def.get("link_font_color") or LINK_FONT) if is_link else (
            wb_def.get("formula_font_color") or FORMULA_FONT
        )
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
    """Clone full cell styles (including protection) and column width from a period column.

    Skips destination cells that are non-top-left ``MergedCell`` instances.
    """
    src_idx = column_index_from_string(src_col) if isinstance(src_col, str) else src_col
    dst_idx = column_index_from_string(dst_col) if isinstance(dst_col, str) else dst_col
    src_ltr = get_column_letter(src_idx)
    dst_ltr = get_column_letter(dst_idx)

    end_row = max_row if max_row is not None else (ws.max_row or 1)
    for r in range(1, end_row + 1):
        sc = ws.cell(r, src_idx)
        dc = ws.cell(r, dst_idx)
        if isinstance(dc, MergedCell):
            continue
        if sc.has_style:
            dc.font = copy.copy(sc.font)
            dc.fill = copy.copy(sc.fill)
            dc.border = copy.copy(sc.border)
            dc.alignment = copy.copy(sc.alignment)
            dc.protection = copy.copy(sc.protection)
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
# 6. Read-Only OOXML Parts Diff (`parts-diff`)
# ---------------------------------------------------------------------------


def _resolve_rel_target(base_part: str, target: str) -> str:
    """Resolve a relative OOXML relationship Target against ``base_part``."""
    t = target.replace("\\", "/")
    if t.startswith("/"):
        return posixpath.normpath(t.lstrip("/"))
    base_dir = posixpath.dirname(base_part)
    return posixpath.normpath(posixpath.join(base_dir, t))


def _rels_path_for(part: str) -> str:
    d = posixpath.dirname(part)
    b = posixpath.basename(part)
    return f"{d}/_rels/{b}.rels" if d else f"_rels/{b}.rels"


def _read_rels(zf: zipfile.ZipFile, part: str, name_set: set[str]) -> dict[str, tuple[str, str]]:
    """Return ``{rId: (rel_type, resolved_target)}`` for ``part``."""
    rp = _rels_path_for(part)
    if rp not in name_set:
        return {}
    out: dict[str, tuple[str, str]] = {}
    try:
        root = etree.fromstring(zf.read(rp), _XML_PARSER)
    except Exception:
        return out
    for rel in root.iter(f"{{{_PKG_REL_NS}}}Relationship"):
        rid = rel.get("Id") or ""
        rtype = rel.get("Type") or ""
        target = rel.get("Target") or ""
        if not rid or not target:
            continue
        if (rel.get("TargetMode") or "").lower() == "external":
            continue
        out[rid] = (rtype, _resolve_rel_target(part, target))
    return out


def _inspect_workbook_parts(xlsx_path: Path) -> dict[str, Any]:
    """Read-only zip/XML inventory of OOXML parts and per-sheet attachments."""
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        names = sorted(zf.namelist())
        name_set = set(names)

        media = [
            n for n in names
            if n.startswith("xl/media/") and not n.endswith("/") and "/_rels/" not in n
        ]
        media_by_ext: dict[str, int] = defaultdict(int)
        for m in media:
            ext = Path(m).suffix.lower().lstrip(".") or "unknown"
            media_by_ext[ext] += 1

        drawings = [
            n for n in names
            if n.startswith("xl/drawings/") and not n.endswith("/") and "/_rels/" not in n
        ]
        charts = [
            n for n in names
            if n.startswith("xl/charts/") and n.endswith(".xml") and "/_rels/" not in n
        ]
        ext_links = [
            n for n in names
            if n.startswith("xl/externalLinks/") and n.endswith(".xml") and "/_rels/" not in n
        ]
        pivots = [
            n for n in names
            if n.startswith("xl/pivotTables/") and n.endswith(".xml") and "/_rels/" not in n
        ]
        slicers = [
            n for n in names
            if n.startswith("xl/slicers/") and n.endswith(".xml") and "/_rels/" not in n
        ]
        threaded_comments = [
            n for n in names
            if n.startswith("xl/threadedComments/") and n.endswith(".xml") and "/_rels/" not in n
        ]
        has_vba = "xl/vbaProject.bin" in name_set
        vba_parts = ["xl/vbaProject.bin"] if has_vba else []

        # Map worksheet XML parts to worksheet titles via workbook.xml + rels
        wb_rels = _read_rels(zf, "xl/workbook.xml", name_set)
        sheet_order: list[str] = []
        sheet_info: dict[str, dict[str, Any]] = {}

        if "xl/workbook.xml" in name_set:
            try:
                wb_root = etree.fromstring(zf.read("xl/workbook.xml"), _XML_PARSER)
            except Exception:
                wb_root = None
            if wb_root is not None:
                for sh_el in wb_root.iter(f"{{{_SML_NS}}}sheet"):
                    title = sh_el.get("name") or ""
                    rid = sh_el.get(f"{{{_REL_DOC_NS}}}id") or ""
                    if not title or rid not in wb_rels:
                        continue
                    sh_part = wb_rels[rid][1]
                    if sh_part not in name_set:
                        continue
                    sheet_order.append(title)

                    # Trace drawings, media, charts, pivots attached to this sheet
                    sh_rels = _read_rels(zf, sh_part, name_set)
                    sh_drawings: set[str] = set()
                    sh_charts: set[str] = set()
                    sh_media: set[str] = set()
                    sh_pivots: set[str] = set()
                    for _, tgt in sh_rels.values():
                        if tgt.startswith("xl/drawings/") and "/_rels/" not in tgt:
                            sh_drawings.add(tgt)
                            for _, dtgt in _read_rels(zf, tgt, name_set).values():
                                if dtgt.startswith("xl/media/") and "/_rels/" not in dtgt:
                                    sh_media.add(dtgt)
                                elif (
                                    dtgt.startswith("xl/charts/")
                                    and dtgt.endswith(".xml")
                                    and "/_rels/" not in dtgt
                                ):
                                    sh_charts.add(dtgt)
                        elif tgt.startswith("xl/charts/") and tgt.endswith(".xml") and "/_rels/" not in tgt:
                            sh_charts.add(tgt)
                        elif tgt.startswith("xl/pivotTables/") and tgt.endswith(".xml") and "/_rels/" not in tgt:
                            sh_pivots.add(tgt)

                    sh_media_by_ext: dict[str, int] = defaultdict(int)
                    for m in sh_media:
                        ext = Path(m).suffix.lower().lstrip(".") or "unknown"
                        sh_media_by_ext[ext] += 1

                    # Inspect worksheet XML for <extLst>, x14 conditional formatting, data validations, sparklines
                    has_extlst = False
                    ext_uris: list[str] = []
                    x14_cf_count = 0
                    dv_count = 0
                    sparkline_count = 0
                    try:
                        sh_root = etree.fromstring(zf.read(sh_part), _XML_PARSER)
                        for el in sh_root.iter():
                            tag = el.tag if isinstance(el.tag, str) else ""
                            if tag.endswith("}extLst") or tag == "extLst":
                                has_extlst = True
                            elif tag.endswith("}ext") or tag == "ext":
                                uri = el.get("uri")
                                if uri and uri not in ext_uris:
                                    ext_uris.append(uri)
                            elif tag == f"{{{_X14_NS}}}conditionalFormatting":
                                x14_cf_count += 1
                            elif tag in (f"{{{_SML_NS}}}dataValidation", f"{{{_X14_NS}}}dataValidation"):
                                dv_count += 1
                            elif tag == f"{{{_X14_NS}}}sparklineGroup":
                                sparkline_count += 1
                    except Exception:
                        pass

                    sheet_info[title] = {
                        "part": sh_part,
                        "media": sorted(sh_media),
                        "media_count": len(sh_media),
                        "media_by_ext": dict(sorted(sh_media_by_ext.items())),
                        "drawings": sorted(sh_drawings),
                        "drawings_count": len(sh_drawings),
                        "charts": sorted(sh_charts),
                        "charts_count": len(sh_charts),
                        "pivots": sorted(sh_pivots),
                        "pivots_count": len(sh_pivots),
                        "has_extlst": has_extlst,
                        "ext_uris": sorted(ext_uris),
                        "x14_cf_count": x14_cf_count,
                        "data_validations_count": dv_count,
                        "sparklines_count": sparkline_count,
                    }

    return {
        "package_parts": {
            "media": media,
            "drawings": drawings,
            "charts": charts,
            "external_links": ext_links,
            "pivot_tables": pivots,
            "slicers": slicers,
            "threaded_comments": threaded_comments,
            "vba_project": vba_parts,
        },
        "package_counts": {
            "media": len(media),
            "media_by_ext": dict(sorted(media_by_ext.items())),
            "drawings": len(drawings),
            "charts": len(charts),
            "external_links": len(ext_links),
            "pivot_tables": len(pivots),
            "slicers": len(slicers),
            "threaded_comments": len(threaded_comments),
            "vba_project": 1 if has_vba else 0,
            "sheets_with_extlst": sum(1 for v in sheet_info.values() if v["has_extlst"]),
            "sheets_with_x14_cf": sum(1 for v in sheet_info.values() if v["x14_cf_count"] > 0),
            "x14_cf_total": sum(v["x14_cf_count"] for v in sheet_info.values()),
            "data_validations_total": sum(v["data_validations_count"] for v in sheet_info.values()),
            "sparklines_total": sum(v["sparklines_count"] for v in sheet_info.values()),
        },
        "sheet_order": sheet_order,
        "sheets": sheet_info,
    }


def _count_media_by_ext(media_paths: list[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for m in media_paths:
        ext = Path(m).suffix.lower().lstrip(".") or "unknown"
        counts[ext] += 1
    return dict(sorted(counts.items()))


def diff_workbook_parts(orig_path: str | Path, new_path: str | Path) -> dict[str, Any]:
    """Compare OOXML part paths and per-sheet XML rule counts between two workbooks.

    Read-only advisory tool: compares package/sheet part paths (media, drawings,
    charts, externalLinks, pivot tables, slicers, comments, VBA) and per-sheet
    rule counts (``<extLst>``, x14 conditional formatting, data validations,
    sparklines) separately without cancelling additions against removals.
    Equal-count rule replacements on the same sheet and content changes inside an
    unchanged part path are outside the scope of this comparison.
    """
    p_orig = Path(orig_path)
    p_new = Path(new_path)
    if not p_orig.is_file():
        return {"status": "error", "message": f"Original file not found: {p_orig}"}
    if not p_new.is_file():
        return {"status": "error", "message": f"New file not found: {p_new}"}

    try:
        inv_orig = _inspect_workbook_parts(p_orig)
    except Exception as exc:
        return {"status": "error", "message": f"Failed to read original workbook zip: {exc}"}
    try:
        inv_new = _inspect_workbook_parts(p_new)
    except Exception as exc:
        return {"status": "error", "message": f"Failed to read new workbook zip: {exc}"}

    oc = inv_orig["package_counts"]
    nc = inv_new["package_counts"]
    op = inv_orig["package_parts"]
    np_parts = inv_new["package_parts"]

    # Package-level path-based removals and additions (never cancel additions against removals)
    pkg_losses: dict[str, int] = {}
    pkg_additions: dict[str, int] = {}
    pkg_parts_removed: dict[str, list[str]] = {}
    pkg_parts_added: dict[str, list[str]] = {}
    for key in (
        "media",
        "drawings",
        "charts",
        "external_links",
        "pivot_tables",
        "slicers",
        "threaded_comments",
        "vba_project",
    ):
        orig_set = set(op.get(key) or ())
        new_set = set(np_parts.get(key) or ())
        removed = sorted(orig_set - new_set)
        added = sorted(new_set - orig_set)
        if removed:
            pkg_losses[key] = len(removed)
            pkg_parts_removed[key] = removed
        if added:
            pkg_additions[key] = len(added)
            pkg_parts_added[key] = added

    media_ext_losses = _count_media_by_ext(pkg_parts_removed.get("media", []))
    media_ext_additions = _count_media_by_ext(pkg_parts_added.get("media", []))

    orig_sheets = inv_orig["sheets"]
    new_sheets = inv_new["sheets"]
    sheets_removed = [s for s in inv_orig["sheet_order"] if s not in new_sheets]
    sheets_added = [s for s in inv_new["sheet_order"] if s not in orig_sheets]

    # Sheet-XML-derived counts aggregated per sheet (additions on one sheet never cancel removals on another)
    all_sheet_titles: list[str] = list(inv_orig["sheet_order"])
    for title in inv_new["sheet_order"]:
        if title not in orig_sheets:
            all_sheet_titles.append(title)

    sheets_with_extlst_lost = 0
    sheets_with_extlst_added = 0
    sheets_with_x14_cf_lost = 0
    sheets_with_x14_cf_added = 0
    x14_cf_lost_total = 0
    x14_cf_added_total = 0
    dv_lost_total = 0
    dv_added_total = 0
    sparklines_lost_total = 0
    sparklines_added_total = 0

    for title in all_sheet_titles:
        os_i = orig_sheets.get(title)
        ns_i = new_sheets.get(title)

        o_extlst = bool(os_i["has_extlst"]) if os_i else False
        n_extlst = bool(ns_i["has_extlst"]) if ns_i else False
        if o_extlst and not n_extlst:
            sheets_with_extlst_lost += 1
        elif n_extlst and not o_extlst:
            sheets_with_extlst_added += 1

        o_x14 = int(os_i["x14_cf_count"]) if os_i else 0
        n_x14 = int(ns_i["x14_cf_count"]) if ns_i else 0
        if o_x14 > 0 and n_x14 == 0:
            sheets_with_x14_cf_lost += 1
        elif n_x14 > 0 and o_x14 == 0:
            sheets_with_x14_cf_added += 1
        if o_x14 > n_x14:
            x14_cf_lost_total += o_x14 - n_x14
        elif n_x14 > o_x14:
            x14_cf_added_total += n_x14 - o_x14

        o_dv = int(os_i["data_validations_count"]) if os_i else 0
        n_dv = int(ns_i["data_validations_count"]) if ns_i else 0
        if o_dv > n_dv:
            dv_lost_total += o_dv - n_dv
        elif n_dv > o_dv:
            dv_added_total += n_dv - o_dv

        o_sp = int(os_i["sparklines_count"]) if os_i else 0
        n_sp = int(ns_i["sparklines_count"]) if ns_i else 0
        if o_sp > n_sp:
            sparklines_lost_total += o_sp - n_sp
        elif n_sp > o_sp:
            sparklines_added_total += n_sp - o_sp

    for key, lost_val, added_val in (
        ("sheets_with_extlst", sheets_with_extlst_lost, sheets_with_extlst_added),
        ("sheets_with_x14_cf", sheets_with_x14_cf_lost, sheets_with_x14_cf_added),
        ("x14_cf_total", x14_cf_lost_total, x14_cf_added_total),
        ("data_validations_total", dv_lost_total, dv_added_total),
        ("sparklines_total", sparklines_lost_total, sparklines_added_total),
    ):
        if lost_val > 0:
            pkg_losses[key] = lost_val
        if added_val > 0:
            pkg_additions[key] = added_val

    sheet_losses: list[dict[str, Any]] = []
    sheet_additions: list[dict[str, Any]] = []
    affected_by_type: dict[str, list[str]] = defaultdict(list)
    added_sheets_by_type: dict[str, list[str]] = defaultdict(list)

    removed_media_set = set(pkg_parts_removed.get("media", ()))
    removed_drawings_set = set(pkg_parts_removed.get("drawings", ()))
    removed_charts_set = set(pkg_parts_removed.get("charts", ()))
    removed_pivots_set = set(pkg_parts_removed.get("pivot_tables", ()))
    added_media_set = set(pkg_parts_added.get("media", ()))
    added_drawings_set = set(pkg_parts_added.get("drawings", ()))
    added_charts_set = set(pkg_parts_added.get("charts", ()))
    added_pivots_set = set(pkg_parts_added.get("pivot_tables", ()))

    for title in inv_orig["sheet_order"]:
        os_i = orig_sheets[title]
        if title not in new_sheets:
            if set(os_i["media"]) & removed_media_set:
                affected_by_type["media"].append(title)
            if set(os_i["drawings"]) & removed_drawings_set:
                affected_by_type["drawings"].append(title)
            if set(os_i["charts"]) & removed_charts_set:
                affected_by_type["charts"].append(title)
            if set(os_i["pivots"]) & removed_pivots_set:
                affected_by_type["pivot_tables"].append(title)
            if os_i["has_extlst"]:
                affected_by_type["extlst"].append(title)
            if os_i["x14_cf_count"] > 0:
                affected_by_type["x14_conditional_formatting"].append(title)
            if os_i["data_validations_count"] > 0:
                affected_by_type["data_validations"].append(title)
            if os_i["sparklines_count"] > 0:
                affected_by_type["sparklines"].append(title)
            continue

        ns_i = new_sheets[title]
        loss: dict[str, Any] = {}
        gain: dict[str, Any] = {}

        sh_media_removed = sorted(set(os_i["media"]) - set(ns_i["media"]))
        sh_media_added = sorted(set(ns_i["media"]) - set(os_i["media"]))
        if sh_media_removed:
            ext_d = _count_media_by_ext(sh_media_removed)
            loss["media_lost"] = len(sh_media_removed)
            loss["media_removed"] = sh_media_removed
            if ext_d:
                loss["media_ext_lost"] = ext_d
            affected_by_type["media"].append(title)
        if sh_media_added:
            ext_a = _count_media_by_ext(sh_media_added)
            gain["media_added"] = len(sh_media_added)
            gain["media_added_parts"] = sh_media_added
            if ext_a:
                gain["media_ext_added"] = ext_a
            added_sheets_by_type["media"].append(title)

        sh_drawings_removed = sorted(set(os_i["drawings"]) - set(ns_i["drawings"]))
        sh_drawings_added = sorted(set(ns_i["drawings"]) - set(os_i["drawings"]))
        if sh_drawings_removed:
            loss["drawings_lost"] = len(sh_drawings_removed)
            loss["drawings_removed"] = sh_drawings_removed
            affected_by_type["drawings"].append(title)
        if sh_drawings_added:
            gain["drawings_added"] = len(sh_drawings_added)
            gain["drawings_added_parts"] = sh_drawings_added
            added_sheets_by_type["drawings"].append(title)

        sh_charts_removed = sorted(set(os_i["charts"]) - set(ns_i["charts"]))
        sh_charts_added = sorted(set(ns_i["charts"]) - set(os_i["charts"]))
        if sh_charts_removed:
            loss["charts_lost"] = len(sh_charts_removed)
            loss["charts_removed"] = sh_charts_removed
            affected_by_type["charts"].append(title)
        if sh_charts_added:
            gain["charts_added"] = len(sh_charts_added)
            gain["charts_added_parts"] = sh_charts_added
            added_sheets_by_type["charts"].append(title)

        sh_pivots_removed = sorted(set(os_i["pivots"]) - set(ns_i["pivots"]))
        sh_pivots_added = sorted(set(ns_i["pivots"]) - set(os_i["pivots"]))
        if sh_pivots_removed:
            loss["pivots_lost"] = len(sh_pivots_removed)
            loss["pivots_removed"] = sh_pivots_removed
            affected_by_type["pivot_tables"].append(title)
        if sh_pivots_added:
            gain["pivots_added"] = len(sh_pivots_added)
            gain["pivots_added_parts"] = sh_pivots_added
            added_sheets_by_type["pivot_tables"].append(title)

        ext_uris_lost = sorted(set(os_i["ext_uris"]) - set(ns_i["ext_uris"]))
        ext_uris_added = sorted(set(ns_i["ext_uris"]) - set(os_i["ext_uris"]))
        if os_i["has_extlst"] and not ns_i["has_extlst"]:
            loss["extlst_lost"] = True
            affected_by_type["extlst"].append(title)
        elif ext_uris_lost:
            loss["ext_uris_lost"] = ext_uris_lost
            affected_by_type["extlst"].append(title)

        if ns_i["has_extlst"] and not os_i["has_extlst"]:
            gain["extlst_added"] = True
            added_sheets_by_type["extlst"].append(title)
        elif ext_uris_added:
            gain["ext_uris_added"] = ext_uris_added
            added_sheets_by_type["extlst"].append(title)

        if os_i["x14_cf_count"] > ns_i["x14_cf_count"]:
            loss["x14_cf_lost"] = os_i["x14_cf_count"] - ns_i["x14_cf_count"]
            affected_by_type["x14_conditional_formatting"].append(title)
        elif ns_i["x14_cf_count"] > os_i["x14_cf_count"]:
            gain["x14_cf_added"] = ns_i["x14_cf_count"] - os_i["x14_cf_count"]
            added_sheets_by_type["x14_conditional_formatting"].append(title)

        if os_i["data_validations_count"] > ns_i["data_validations_count"]:
            loss["data_validations_lost"] = os_i["data_validations_count"] - ns_i["data_validations_count"]
            affected_by_type["data_validations"].append(title)
        elif ns_i["data_validations_count"] > os_i["data_validations_count"]:
            gain["data_validations_added"] = ns_i["data_validations_count"] - os_i["data_validations_count"]
            added_sheets_by_type["data_validations"].append(title)

        if os_i["sparklines_count"] > ns_i["sparklines_count"]:
            loss["sparklines_lost"] = os_i["sparklines_count"] - ns_i["sparklines_count"]
            affected_by_type["sparklines"].append(title)
        elif ns_i["sparklines_count"] > os_i["sparklines_count"]:
            gain["sparklines_added"] = ns_i["sparklines_count"] - os_i["sparklines_count"]
            added_sheets_by_type["sparklines"].append(title)

        if loss:
            sheet_losses.append({"sheet": title, **loss})
        if gain:
            sheet_additions.append({"sheet": title, **gain})

    for title in sheets_added:
        ns_i = new_sheets[title]
        if set(ns_i["media"]) & added_media_set:
            added_sheets_by_type["media"].append(title)
        if set(ns_i["drawings"]) & added_drawings_set:
            added_sheets_by_type["drawings"].append(title)
        if set(ns_i["charts"]) & added_charts_set:
            added_sheets_by_type["charts"].append(title)
        if set(ns_i["pivots"]) & added_pivots_set:
            added_sheets_by_type["pivot_tables"].append(title)
        if ns_i["has_extlst"]:
            added_sheets_by_type["extlst"].append(title)
        if ns_i["x14_cf_count"] > 0:
            added_sheets_by_type["x14_conditional_formatting"].append(title)
        if ns_i["data_validations_count"] > 0:
            added_sheets_by_type["data_validations"].append(title)
        if ns_i["sparklines_count"] > 0:
            added_sheets_by_type["sparklines"].append(title)

    def _fmt_sheets(sh_list: list[str]) -> str:
        if not sh_list:
            return ""
        shown = "、".join(sh_list[:8])
        suffix = f" 等共 {len(sh_list)} 张表" if len(sh_list) > 8 else f"（共 {len(sh_list)} 张表）"
        return f"涉及 {shown}{suffix}"

    def _fmt_parts(parts: list[str], limit: int = 5) -> str:
        if not parts:
            return ""
        shown = "、".join(parts[:limit])
        return f"{shown} 等共 {len(parts)} 个" if len(parts) > limit else shown

    summary_clauses: list[str] = []
    media_lost_cnt = pkg_losses.get("media") or sum(s.get("media_lost", 0) for s in sheet_losses)
    if media_lost_cnt > 0:
        removed_media = pkg_parts_removed.get("media") or sorted(
            {p for s in sheet_losses for p in s.get("media_removed", ())}
        )
        ext_map = media_ext_losses or _count_media_by_ext(removed_media)
        ext_txt = "、".join(f"{cnt} 张 {ext.upper()}" for ext, cnt in sorted(ext_map.items()))
        parts_txt = _fmt_parts(removed_media)
        inner = "；".join(p for p in (ext_txt, parts_txt) if p)
        detail = f"（{inner}）" if inner else ""
        sh_txt = _fmt_sheets(affected_by_type.get("media", []))
        summary_clauses.append(f"嵌入图片减少 {media_lost_cnt} 张{detail}" + (f"，{sh_txt}" if sh_txt else ""))
    drawings_lost_cnt = pkg_losses.get("drawings") or sum(s.get("drawings_lost", 0) for s in sheet_losses)
    if drawings_lost_cnt > 0:
        removed_drawings = pkg_parts_removed.get("drawings") or sorted(
            {p for s in sheet_losses for p in s.get("drawings_removed", ())}
        )
        parts_txt = _fmt_parts(removed_drawings)
        detail = f"（{parts_txt}）" if parts_txt else ""
        sh_txt = _fmt_sheets(affected_by_type.get("drawings", []))
        summary_clauses.append(f"绘图部件减少 {drawings_lost_cnt} 个{detail}" + (f"，{sh_txt}" if sh_txt else ""))
    charts_lost_cnt = pkg_losses.get("charts") or sum(s.get("charts_lost", 0) for s in sheet_losses)
    if charts_lost_cnt > 0:
        removed_charts = pkg_parts_removed.get("charts") or sorted(
            {p for s in sheet_losses for p in s.get("charts_removed", ())}
        )
        parts_txt = _fmt_parts(removed_charts)
        detail = f"（{parts_txt}）" if parts_txt else "（xl/charts）"
        sh_txt = _fmt_sheets(affected_by_type.get("charts", []))
        summary_clauses.append(f"图表减少 {charts_lost_cnt} 个{detail}" + (f"，{sh_txt}" if sh_txt else ""))
    x14_lost_cnt = pkg_losses.get("x14_cf_total") or sum(s.get("x14_cf_lost", 0) for s in sheet_losses)
    if x14_lost_cnt > 0:
        sh_list = affected_by_type.get("x14_conditional_formatting", [])
        sh_txt = _fmt_sheets(sh_list)
        summary_clauses.append(f"扩展条件格式（x14）减少 {x14_lost_cnt} 处" + (f"，{sh_txt}" if sh_txt else ""))
    elif pkg_losses.get("sheets_with_extlst") or affected_by_type.get("extlst"):
        sh_list = affected_by_type.get("extlst", [])
        sh_txt = _fmt_sheets(sh_list)
        summary_clauses.append("工作表扩展节点（<extLst>）减少" + (f"，{sh_txt}" if sh_txt else ""))
    if pkg_losses.get("data_validations_total"):
        sh_txt = _fmt_sheets(affected_by_type.get("data_validations", []))
        summary_clauses.append(f"数据验证减少 {pkg_losses['data_validations_total']} 条" + (f"，{sh_txt}" if sh_txt else ""))
    if pkg_losses.get("sparklines_total"):
        sh_txt = _fmt_sheets(affected_by_type.get("sparklines", []))
        summary_clauses.append(f"迷你图（sparkline）减少 {pkg_losses['sparklines_total']} 组" + (f"，{sh_txt}" if sh_txt else ""))
    pivots_lost_cnt = pkg_losses.get("pivot_tables") or sum(s.get("pivots_lost", 0) for s in sheet_losses)
    if pivots_lost_cnt > 0:
        sh_txt = _fmt_sheets(affected_by_type.get("pivot_tables", []))
        summary_clauses.append(f"数据透视表减少 {pivots_lost_cnt} 个" + (f"，{sh_txt}" if sh_txt else ""))
    if pkg_losses.get("slicers"):
        summary_clauses.append(f"切片器减少 {pkg_losses['slicers']} 个")
    if pkg_losses.get("threaded_comments"):
        summary_clauses.append(f"线索批注（threadedComments）减少 {pkg_losses['threaded_comments']} 处")
    if pkg_losses.get("vba_project"):
        summary_clauses.append("VBA 宏工程（vbaProject.bin）丢失")
    if pkg_losses.get("external_links"):
        summary_clauses.append(f"外部链接部件（xl/externalLinks）减少 {pkg_losses['external_links']} 个（若为主动清理外链则属预期）")
    if sheets_removed:
        summary_clauses.append(f"原工作表被移除：{'、'.join(sheets_removed)}")

    addition_clauses: list[str] = []
    media_added_cnt = pkg_additions.get("media") or sum(s.get("media_added", 0) for s in sheet_additions)
    if media_added_cnt > 0:
        added_media = pkg_parts_added.get("media") or sorted(
            {p for s in sheet_additions for p in s.get("media_added_parts", ())}
        )
        ext_map = media_ext_additions or _count_media_by_ext(added_media)
        ext_txt = "、".join(f"{cnt} 张 {ext.upper()}" for ext, cnt in sorted(ext_map.items()))
        parts_txt = _fmt_parts(added_media)
        inner = "；".join(p for p in (ext_txt, parts_txt) if p)
        detail = f"（{inner}）" if inner else ""
        sh_txt = _fmt_sheets(added_sheets_by_type.get("media", []))
        addition_clauses.append(f"嵌入图片新增 {media_added_cnt} 张{detail}" + (f"，{sh_txt}" if sh_txt else ""))
    drawings_added_cnt = pkg_additions.get("drawings") or sum(s.get("drawings_added", 0) for s in sheet_additions)
    if drawings_added_cnt > 0:
        added_drawings = pkg_parts_added.get("drawings") or sorted(
            {p for s in sheet_additions for p in s.get("drawings_added_parts", ())}
        )
        parts_txt = _fmt_parts(added_drawings)
        detail = f"（{parts_txt}）" if parts_txt else ""
        sh_txt = _fmt_sheets(added_sheets_by_type.get("drawings", []))
        addition_clauses.append(f"绘图部件新增 {drawings_added_cnt} 个{detail}" + (f"，{sh_txt}" if sh_txt else ""))
    charts_added_cnt = pkg_additions.get("charts") or sum(s.get("charts_added", 0) for s in sheet_additions)
    if charts_added_cnt > 0:
        added_charts = pkg_parts_added.get("charts") or sorted(
            {p for s in sheet_additions for p in s.get("charts_added_parts", ())}
        )
        parts_txt = _fmt_parts(added_charts)
        detail = f"（{parts_txt}）" if parts_txt else "（xl/charts）"
        sh_txt = _fmt_sheets(added_sheets_by_type.get("charts", []))
        addition_clauses.append(f"图表新增 {charts_added_cnt} 个{detail}" + (f"，{sh_txt}" if sh_txt else ""))
    x14_added_cnt = pkg_additions.get("x14_cf_total") or sum(s.get("x14_cf_added", 0) for s in sheet_additions)
    if x14_added_cnt > 0:
        sh_list = added_sheets_by_type.get("x14_conditional_formatting", [])
        sh_txt = _fmt_sheets(sh_list)
        addition_clauses.append(f"扩展条件格式（x14）新增 {x14_added_cnt} 处" + (f"，{sh_txt}" if sh_txt else ""))
    elif pkg_additions.get("sheets_with_extlst") or added_sheets_by_type.get("extlst"):
        sh_list = added_sheets_by_type.get("extlst", [])
        sh_txt = _fmt_sheets(sh_list)
        addition_clauses.append("工作表扩展节点（<extLst>）新增" + (f"，{sh_txt}" if sh_txt else ""))
    if pkg_additions.get("data_validations_total"):
        sh_txt = _fmt_sheets(added_sheets_by_type.get("data_validations", []))
        addition_clauses.append(f"数据验证新增 {pkg_additions['data_validations_total']} 条" + (f"，{sh_txt}" if sh_txt else ""))
    if pkg_additions.get("sparklines_total"):
        sh_txt = _fmt_sheets(added_sheets_by_type.get("sparklines", []))
        addition_clauses.append(f"迷你图（sparkline）新增 {pkg_additions['sparklines_total']} 组" + (f"，{sh_txt}" if sh_txt else ""))
    pivots_added_cnt = pkg_additions.get("pivot_tables") or sum(s.get("pivots_added", 0) for s in sheet_additions)
    if pivots_added_cnt > 0:
        sh_txt = _fmt_sheets(added_sheets_by_type.get("pivot_tables", []))
        addition_clauses.append(f"数据透视表新增 {pivots_added_cnt} 个" + (f"，{sh_txt}" if sh_txt else ""))
    if pkg_additions.get("slicers"):
        addition_clauses.append(f"切片器新增 {pkg_additions['slicers']} 个")
    if pkg_additions.get("threaded_comments"):
        addition_clauses.append(f"线索批注（threadedComments）新增 {pkg_additions['threaded_comments']} 处")
    if pkg_additions.get("vba_project"):
        addition_clauses.append("VBA 宏工程（vbaProject.bin）新增")
    if pkg_additions.get("external_links"):
        addition_clauses.append(f"外部链接部件（xl/externalLinks）新增 {pkg_additions['external_links']} 个")
    if sheets_added:
        addition_clauses.append(f"工作表：{'、'.join(sheets_added)}")

    has_losses = bool(summary_clauses)
    has_additions = bool(addition_clauses)
    scope_note = "（仅比较部件路径和各表的规则数量；数量相同的规则替换、同一部件内的内容变化超出比较范围。）"
    if has_losses:
        summary = "部件路径与各表规则数量对比发现新文件相对原文件有以下减少：" + "；".join(summary_clauses) + "。"
    else:
        summary = "部件路径与各表规则数量对比完成：未发现图片、绘图、图表、扩展条件格式（<extLst>/x14）、数据验证或其他关键部件的路径缺失或数量减少。"
    if addition_clauses:
        summary += " 新增部分：" + "；".join(addition_clauses) + "。"
    summary += scope_note

    return {
        "status": "ok",
        "orig_file": str(p_orig),
        "new_file": str(p_new),
        "has_losses": has_losses,
        "has_additions": has_additions,
        "summary": summary,
        "orig_counts": oc,
        "new_counts": nc,
        "package_losses": pkg_losses,
        "package_additions": pkg_additions,
        "package_parts_removed": pkg_parts_removed,
        "package_parts_added": pkg_parts_added,
        "media_ext_losses": media_ext_losses,
        "media_ext_additions": media_ext_additions,
        "sheets_removed": sheets_removed,
        "sheets_added": sheets_added,
        "affected_sheets_by_type": dict(affected_by_type),
        "added_sheets_by_type": dict(added_sheets_by_type),
        "sheet_losses": sheet_losses,
        "sheet_additions": sheet_additions,
    }


# ---------------------------------------------------------------------------
# 7. Plain Model Lineage Bookkeeping (`model_lineage.json`)
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
# 8. CLI Entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mechanical Excel style profiler, parts-diff tool, and model_lineage.json recorder."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prof = sub.add_parser("profile", help="Extract observed style profile JSON from an existing workbook")
    p_prof.add_argument("workbook", help="Source .xlsx workbook (read-only)")
    p_prof.add_argument("--out", help="Optional output JSON path")

    p_diff = sub.add_parser(
        "parts-diff",
        help="Compare OOXML parts (media, drawings, charts, externalLinks, <extLst>/x14, data validations) between two workbooks",
    )
    p_diff.add_argument("orig_workbook", help="Original baseline .xlsx workbook (read-only)")
    p_diff.add_argument("new_workbook", help="New/edited .xlsx workbook (read-only)")
    p_diff.add_argument("--out", help="Optional output JSON path")

    p_lin = sub.add_parser("lineage", help="Record input/output paths, hashes, and timestamp in model_lineage.json")
    p_lin.add_argument("--file", required=True, help="Path to model_lineage.json")
    p_lin.add_argument("--output", required=True, help="Output .xlsx path")
    p_lin.add_argument("--baseline", default=None, help="Optional input baseline .xlsx path")
    p_lin.add_argument("--profile", default=None, help="Optional style_profile.json path")
    p_lin.add_argument("--note", default=None, help="Optional note")

    return parser


def _paths_refer_to_same_file(p1: Path, p2: Path) -> bool:
    """Return True if ``p1`` and ``p2`` resolve to the same path or existing inode."""
    try:
        if p1.resolve() == p2.resolve():
            return True
    except OSError:
        pass
    try:
        if p1.exists() and p2.exists() and os.path.samefile(p1, p2):
            return True
    except OSError:
        pass
    return False


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

    if args.cmd == "parts-diff":
        if args.out:
            out_p = Path(args.out)
            orig_p = Path(args.orig_workbook)
            new_p = Path(args.new_workbook)
            if _paths_refer_to_same_file(out_p, orig_p) or _paths_refer_to_same_file(out_p, new_p):
                print(json.dumps({
                    "status": "error",
                    "message": f"拒绝写入：--out 目标路径（{out_p}）与输入工作簿相同，已中止以避免覆盖输入文件。",
                }, ensure_ascii=False))
                return 1
        diff = diff_workbook_parts(args.orig_workbook, args.new_workbook)
        text = json.dumps(diff, indent=2, ensure_ascii=False)
        if args.out and diff.get("status") == "ok":
            out_p = Path(args.out)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(text + "\n", encoding="utf-8")
            print(json.dumps({
                "status": "ok",
                "out": str(out_p),
                "has_losses": diff.get("has_losses", False),
                "has_additions": diff.get("has_additions", False),
                "summary": diff.get("summary", ""),
            }, ensure_ascii=False))
        else:
            print(text)
        return 0 if diff.get("status") == "ok" else 1

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
