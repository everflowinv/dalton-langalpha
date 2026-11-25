#!/usr/bin/env python3
"""Dalton financial modeling style library, period mapper, profiler & safe XML patcher.

Standalone script (stdlib + openpyxl + lxml only) runnable directly inside a sandbox
with ``python -I`` or imported by ``style_audit.py`` and model build/update scripts.

CLI usage:
    python model_style.py profile <workbook.xlsx> [--out style_profile.json] [--fiscal-year-end-month M]
    python model_style.py clean-external-links <in.xlsx> --out <out.xlsx>
    python model_style.py part-diff <baseline.xlsx> <new.xlsx>
    python model_style.py lineage --file <model_lineage.json> --output <out.xlsx> [--baseline <base.xlsx>] [--profile <profile.json>]
"""

from __future__ import annotations

import argparse
import colorsys
import copy
from dataclasses import asdict, dataclass, field
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
from openpyxl.formula.tokenizer import Token, Tokenizer
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.cell import coordinate_from_string

# ---------------------------------------------------------------------------
# 1. Default Style Constants (Design §4.1, empirically calibrated)
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

# Standard number formats (§4.1)
FMT_INT = r"_(#,##0_);\(#,##0\);_(?\-?_);@"
FMT_DEC1 = r"_(#,##0.0_);\(#,##0.0\);_(?\-?_);@"
FMT_DEC2 = r"_(#,##0.00_);\(#,##0.00\);_(?\-?_);@"
FMT_PCT = "0.0%"
FMT_PCT2 = "0.00%"
FMT_PP = '#,##0.0" pp"'
FMT_MULT = '#,##0.0" x"'
FMT_YM = 'yy"年"m"月"'

# Default layout & grouping parameters (§4.1)
DEFAULT_INDENT_COL_WIDTHS = (2.0, 2.0, 2.0)
DEFAULT_LABEL_COL_WIDTH = 28.0
DEFAULT_PERIOD_COL_WIDTH = 8.5
DEFAULT_SPACER_COL_WIDTH = 1.2
DEFAULT_ANNUAL_COLLAPSE_YEARS = 3
DEFAULT_QUARTER_COLLAPSE_QUARTERS = 8
DEFAULT_RATIO_ROW_OUTLINE = True

# Foreign template fills (e.g., upstream xlsx skill defaults) flagged when not in active profile
FOREIGN_TEMPLATE_FILLS: frozenset[str] = frozenset({
    "1F4E79",
    "D9E1F2",
    "F2F2F2",
    "BDD7EE",
    "2F5597",
})

ALLOWED_DEFAULT_FONTS: frozenset[str] = frozenset({FONT_EN, FONT_ZH})
DEFAULT_ALLOWED_FILLS: frozenset[str] = frozenset({
    HEADER_FILL,
    HIST_EST_FILL_GRAY,
    HIST_EST_FILL_YELLOW,
    ASSUMP_FILL,
    SEC_REPORT_FILL,
    SEC_SEGMENT_FILL,
    SEC_SUBMODEL_FILL,
})

# XML namespaces
SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
DRAWING_MAIN_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
X14_NS = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
XM_NS = "http://schemas.microsoft.com/office/excel/2006/main"
XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)

# Theme color slot order in OOXML clrScheme (0..11)
_THEME_SLOTS = (
    "lt1",
    "dk1",
    "lt2",
    "dk2",
    "accent1",
    "accent2",
    "accent3",
    "accent4",
    "accent5",
    "accent6",
    "hlink",
    "folHlink",
)

# Default Office theme fallback palette when theme1.xml is absent
_DEFAULT_THEME_PALETTE: dict[int, str] = {
    0: "FFFFFF",
    1: "000000",
    2: "E7E6E6",
    3: "44546A",
    4: "4472C4",
    5: "ED7D31",
    6: "A5A5A5",
    7: "FFC000",
    8: "5B9BD5",
    9: "70AD47",
    10: "0563C1",
    11: "954F72",
}

# Minimal Excel legacy indexed color table for commonly observed indices
_INDEXED_COLORS: dict[int, str | None] = {
    0: "000000",
    1: "FFFFFF",
    2: "FF0000",
    3: "00FF00",
    4: "0000FF",
    5: "FFFF00",
    6: "FF00FF",
    7: "00FFFF",
    8: "000000",
    9: "FFFFFF",
    10: "FF0000",
    11: "00FF00",
    12: "0000FF",
    13: "FFFF00",
    17: "008000",
    22: "C0C0C0",
    23: "808080",
    64: None,  # System foreground (automatic)
    65: None,  # System background (automatic)
}

# ---------------------------------------------------------------------------
# 2. Theme + Tint -> RGB Color Resolution (§4.2 #2)
# ---------------------------------------------------------------------------

_HEX6_RE = re.compile(r"^[0-9A-F]{6}$")
_HEX8_RE = re.compile(r"^[0-9A-F]{8}$")
_ZH_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")


def apply_tint(rgb_hex: str, tint: float) -> str:
    """Apply OOXML tint adjustment to a 6-digit uppercase RGB hex string."""
    if not tint or abs(tint) < 1e-9:
        return rgb_hex.upper()
    r = int(rgb_hex[0:2], 16) / 255.0
    g = int(rgb_hex[2:4], 16) / 255.0
    b = int(rgb_hex[4:6], 16) / 255.0
    h, l_val, s = colorsys.rgb_to_hls(r, g, b)
    if tint < 0.0:
        l_val = l_val * (1.0 + tint)
    else:
        l_val = l_val * (1.0 - tint) + tint
    l_val = max(0.0, min(1.0, l_val))
    r2, g2, b2 = colorsys.hls_to_rgb(h, l_val, s)
    return f"{round(r2 * 255):02X}{round(g2 * 255):02X}{round(b2 * 255):02X}"


def extract_theme_palette(xlsx_path: str | Path) -> dict[int, str]:
    """Read ``xl/theme/theme1.xml`` ``<a:clrScheme>`` into a ``{theme_idx: RGB}`` dict."""
    palette = dict(_DEFAULT_THEME_PALETTE)
    path = Path(xlsx_path)
    if not path.is_file():
        return palette
    try:
        with zipfile.ZipFile(path, "r") as zf:
            if "xl/theme/theme1.xml" not in zf.namelist():
                return palette
            root = etree.fromstring(zf.read("xl/theme/theme1.xml"), XML_PARSER)
    except Exception:
        return palette

    clr_scheme = root.find(f".//{{{DRAWING_MAIN_NS}}}clrScheme")
    if clr_scheme is None:
        return palette

    for idx, slot in enumerate(_THEME_SLOTS):
        el = clr_scheme.find(f"{{{DRAWING_MAIN_NS}}}{slot}")
        if el is None:
            continue
        srgb = el.find(f"{{{DRAWING_MAIN_NS}}}srgbClr")
        if srgb is not None and srgb.get("val"):
            val = srgb.get("val", "").strip().upper()
            if _HEX6_RE.match(val):
                palette[idx] = val
                continue
        sys_clr = el.find(f"{{{DRAWING_MAIN_NS}}}sysClr")
        if sys_clr is not None:
            val = (sys_clr.get("lastClr") or "").strip().upper()
            if _HEX6_RE.match(val):
                palette[idx] = val
    return palette


def normalize_rgb(raw: str | None) -> str | None:
    """Normalize 6- or 8-digit ARGB/RGB hex string to 6-digit uppercase RGB."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().upper()
    if _HEX6_RE.match(s):
        return s
    if _HEX8_RE.match(s):
        # Ignore fully transparent 00000000 on fills/unset colors when caller filters fill_type
        return s[2:]
    return None


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
        return normalize_rgb(s)
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
        if isinstance(idx, int):
            return _INDEXED_COLORS.get(idx)
        return None
    return None


def get_cell_font_rgb(cell: Any, palette: dict[int, str] | None = None) -> str | None:
    """Return resolved 6-hex font RGB, treating unset/automatic as ``000000``."""
    font = getattr(cell, "font", None)
    if font is None or font.color is None:
        return FORMULA_FONT
    resolved = resolve_color_rgb(font.color, palette, is_fill=False)
    return resolved if resolved is not None else FORMULA_FONT


def get_cell_fill_rgb(cell: Any, palette: dict[int, str] | None = None) -> str | None:
    """Return resolved 6-hex fill RGB for solid fills, or ``None`` if no fill."""
    fill = getattr(cell, "fill", None)
    if fill is None or not getattr(fill, "fill_type", None):
        return None
    if fill.fill_type == "none":
        return None
    fg = getattr(fill, "fgColor", None)
    return resolve_color_rgb(fg, palette, is_fill=True)


def has_four_side_border(cell: Any, expected_style: str = ASSUMP_BORDER) -> bool:
    """Return True iff all 4 borders exist (matching ``expected_style`` when specified)."""
    border = getattr(cell, "border", None)
    if border is None:
        return False
    sides = (border.left, border.right, border.top, border.bottom)
    if expected_style:
        return all(s is not None and s.style == expected_style for s in sides)
    return all(s is not None and s.style is not None for s in sides)


def is_color_close(rgb_a: str | None, rgb_b: str | None, max_channel_diff: int = 12) -> bool:
    """Compare two 6-hex RGB strings allowing small theme+tint rounding differences."""
    if rgb_a is None or rgb_b is None:
        return rgb_a == rgb_b
    a = rgb_a.upper()
    b = rgb_b.upper()
    if a == b:
        return True
    if len(a) != 6 or len(b) != 6:
        return False
    return all(
        abs(int(a[i : i + 2], 16) - int(b[i : i + 2], 16)) <= max_channel_diff
        for i in (0, 2, 4)
    )


# ---------------------------------------------------------------------------
# 3. Shared Period Mapping (`PeriodMap`) (§3.1)
# ---------------------------------------------------------------------------

_CAGR_DELTA_RE = re.compile(
    r"(?:\bCAGR\b|\bYoY\b|\bQoQ\b|\bMoM\b|\bvs\.?\b|\bChg\b|\bDiff\b|\bVar\b|Δ|同比|环比|变动)",
    re.IGNORECASE,
)
_QUARTER_FIRST_RE = re.compile(
    r"^\s*(?:Q?([1-4])Q|Q([1-4]))\s*['\-/\s]?(\d{2}|\d{4})\s*([AEFP])?\s*(?:PF)?\s*$",
    re.IGNORECASE,
)
_YEAR_FIRST_Q_RE = re.compile(
    r"^\s*(?:FY|CY)?\s*(\d{2}|\d{4})\s*['\-/\s]?Q([1-4])\s*([AEFP])?\s*(?:PF)?\s*$",
    re.IGNORECASE,
)
_ANNUAL_RE = re.compile(
    r"^\s*(?:FY|CY)?\s*'?((?:19|20)\d{2}|\d{2})\s*([AEFP])?\s*(?:PF)?\s*$",
    re.IGNORECASE,
)
_FY_MONTH_HINT_RE = re.compile(
    r"(?:FY\s*ends?\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*|"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*(?:YE|year[- ]end)|"
    r"(\d{1,2})\s*月(?:底)?结账)",
    re.IGNORECASE,
)
_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _expand_year(raw_yr: str) -> int:
    val = int(raw_yr)
    if len(raw_yr) == 4:
        return val
    return 2000 + val if val <= 79 else 1900 + val


def parse_period_label(raw: Any) -> tuple[str, int | None, int | None, bool, str | None]:
    """Parse a header cell value into ``(kind, year, quarter, is_forecast, normalized_period)``.

    Returns:
        kind: ``"annual"``, ``"quarter"``, ``"cagr_or_delta"``, or ``"other"``
        year: 4-digit year or None
        quarter: 1..4 or None
        is_forecast: True if suffix is E/F/P
        normalized_period: ``FY2026`` / ``4Q26`` / None
    """
    if raw is None:
        return ("other", None, None, False, None)
    if isinstance(raw, bool):
        return ("other", None, None, False, None)
    if isinstance(raw, (int, float)):
        if float(raw).is_integer() and 1990 <= int(raw) <= 2100:
            yr = int(raw)
            return ("annual", yr, None, False, f"FY{yr}")
        return ("other", None, None, False, None)

    text = str(raw).strip()
    if not text:
        return ("other", None, None, False, None)
    collapsed = " ".join(text.split())

    # 1. CAGR / delta columns MUST be classified separately even if ending in 'E'
    if _CAGR_DELTA_RE.search(collapsed):
        return ("cagr_or_delta", None, None, False, None)

    # 2. Quarter-first: 4Q26E, 3Q26, Q4 26E, Q2 2026A
    m = _QUARTER_FIRST_RE.match(collapsed)
    if m:
        q = int(m.group(1) or m.group(2))
        yr = _expand_year(m.group(3))
        suf = (m.group(4) or "").upper()
        is_fcst = suf in ("E", "F", "P")
        return ("quarter", yr, q, is_fcst, f"{q}Q{yr % 100:02d}")

    # 3. Year-first quarter: 2026Q4E, FY26Q3, 2026Q2
    m = _YEAR_FIRST_Q_RE.match(collapsed)
    if m:
        yr = _expand_year(m.group(1))
        q = int(m.group(2))
        suf = (m.group(3) or "").upper()
        is_fcst = suf in ("E", "F", "P")
        return ("quarter", yr, q, is_fcst, f"{q}Q{yr % 100:02d}")

    # 4. Annual: 2025, 2026E, 2026F, 2026P, FY25A, FY2026E
    m = _ANNUAL_RE.match(collapsed)
    if m:
        yr_str = m.group(1)
        suf = (m.group(2) or "").upper()
        # Require 4 digits OR explicit FY/CY/suffix so random 2-digit numbers aren't years
        if len(yr_str) == 2 and not (
            collapsed.upper().startswith(("FY", "CY")) or suf
        ):
            return ("other", None, None, False, None)
        yr = _expand_year(yr_str)
        if 1990 <= yr <= 2100:
            is_fcst = suf in ("E", "F", "P")
            return ("annual", yr, None, is_fcst, f"FY{yr}")

    return ("other", None, None, False, None)


def normalize_period_for_knowledge(raw_label: Any) -> str | None:
    """Convert workbook period header to ``common.schema.json`` period format.

    ``2026`` / ``2026E`` / ``2026F`` -> ``FY2026``;
    ``4Q26E`` / ``2026Q4E`` -> ``4Q26``;
    ``CAGR 25-28E`` / non-matching -> ``None`` (omit optional field).
    """
    kind, _yr, _q, _fcst, norm = parse_period_label(raw_label)
    if kind in ("annual", "quarter"):
        return norm
    return None


@dataclass
class PeriodColumn:
    col_idx: int
    col_letter: str
    header_row: int
    raw_label: str
    kind: str  # "annual" | "quarter" | "cagr_or_delta"
    year: int | None
    quarter: int | None
    is_forecast: bool
    normalized_period: str | None
    column_id: str
    duplicate_index: int = 0
    bound_quarter_cols: list[int] = field(default_factory=list)


@dataclass
class PeriodMap:
    sheet_name: str
    main_header_row: int | None
    sub_header_rows: list[int]
    fiscal_year_end_month: int
    fiscal_year_end_quarter: int
    columns_by_header_row: dict[int, dict[int, PeriodColumn]]
    annual_to_quarters: dict[int, list[int]]  # annual_col_idx -> [q1_col, q2_col, q3_col, q4_col]
    source_note_col: int | None

    @property
    def main_columns(self) -> dict[int, PeriodColumn]:
        if self.main_header_row is None:
            return {}
        return self.columns_by_header_row.get(self.main_header_row, {})

    def column_for_cell(self, row: int, col: int) -> PeriodColumn | None:
        """Return the active ``PeriodColumn`` governing ``(row, col)`` (sub-block aware)."""
        active_hdr = self.active_header_row_for(row)
        if active_hdr is None:
            return None
        return self.columns_by_header_row.get(active_hdr, {}).get(col)

    def active_header_row_for(self, row: int) -> int | None:
        """Return the closest period header row strictly above ``row``."""
        candidates = [r for r in self.columns_by_header_row if r < row]
        if not candidates:
            return self.main_header_row
        return max(candidates)

    def all_header_rows(self) -> set[int]:
        return set(self.columns_by_header_row.keys())

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_name": self.sheet_name,
            "main_header_row": self.main_header_row,
            "sub_header_rows": self.sub_header_rows,
            "fiscal_year_end_month": self.fiscal_year_end_month,
            "fiscal_year_end_quarter": self.fiscal_year_end_quarter,
            "source_note_col": self.source_note_col,
            "annual_to_quarters": {
                get_column_letter(k): [get_column_letter(c) for c in v]
                for k, v in self.annual_to_quarters.items()
            },
            "columns": {
                str(r): [asdict(pc) for pc in sorted(cols.values(), key=lambda x: x.col_idx)]
                for r, cols in sorted(self.columns_by_header_row.items())
            },
        }


def _detect_fiscal_year_end_month(ws: Any, main_cols: dict[int, PeriodColumn]) -> int:
    """Detect fiscal year-end month from header notes or period-axis alignment (NEVER from SUM formulas)."""
    # 1. Explicit header note in top rows / columns A..E
    max_r = min(ws.max_row or 1, 15)
    max_c = min(ws.max_column or 1, 6)
    for r in range(1, max_r + 1):
        for c in range(1, max_c + 1):
            val = ws.cell(r, c).value
            if not isinstance(val, str):
                continue
            m = _FY_MONTH_HINT_RE.search(val)
            if m:
                mon_txt = (m.group(1) or m.group(2) or "").lower()[:3]
                if mon_txt in _MONTH_MAP:
                    return _MONTH_MAP[mon_txt]
                if m.group(3):
                    num = int(m.group(3))
                    if 1 <= num <= 12:
                        return num

    # 2. Period-axis alignment heuristic (strictly from period header columns, never formulas)
    quarters = sorted(
        [pc for pc in main_cols.values() if pc.kind == "quarter" and pc.year and pc.quarter],
        key=lambda x: x.col_idx,
    )
    annuals = [pc for pc in main_cols.values() if pc.kind == "annual" and pc.year]
    if len(quarters) >= 4 and annuals:
        last_q = quarters[-1]
        annual_years = {a.year for a in annuals}
        # Check if the quarterly axis ends on Q1/Q2/Q3 of a year that appears in annuals,
        # AND the forecast transition boundary aligns with the same quarter-end!
        if last_q.quarter in (1, 2, 3) and last_q.year in annual_years:
            # Verify no later quarter of that same year exists
            q_end = last_q.quarter
            # Check if the 4 trailing quarters form a consecutive fiscal year ending at q_end
            expected_seq = _fiscal_year_quarters(last_q.year, q_end)
            actual_tail = [(q.year, q.quarter) for q in quarters[-4:]]
            if actual_tail == expected_seq:
                return q_end * 3
    return 12


def _fiscal_year_quarters(fy_year: int, fy_end_quarter: int) -> list[tuple[int, int]]:
    """Return the ordered 4 ``(calendar_year, quarter)`` tuples belonging to ``fy_year``."""
    if fy_end_quarter == 4:
        return [(fy_year, 1), (fy_year, 2), (fy_year, 3), (fy_year, 4)]
    if fy_end_quarter == 1:
        return [(fy_year - 1, 2), (fy_year - 1, 3), (fy_year - 1, 4), (fy_year, 1)]
    if fy_end_quarter == 2:
        return [(fy_year - 1, 3), (fy_year - 1, 4), (fy_year, 1), (fy_year, 2)]
    if fy_end_quarter == 3:
        return [(fy_year - 1, 4), (fy_year, 1), (fy_year, 2), (fy_year, 3)]
    return [(fy_year, 1), (fy_year, 2), (fy_year, 3), (fy_year, 4)]


def _disambiguate_column_id(
    ws: Any,
    hdr_row: int,
    pc: PeriodColumn,
    dup_idx: int,
    total_dups: int,
    explicit_identities: dict[str, str] | None = None,
) -> str:
    """Build a stable column identity surviving column insertions (handles duplicate labels like Subs - JD!R1/S1)."""
    if explicit_identities:
        key_coord = f"{pc.col_letter}{hdr_row}"
        if key_coord in explicit_identities:
            return explicit_identities[key_coord]
        if pc.col_letter in explicit_identities:
            return explicit_identities[pc.col_letter]

    base = f"r{hdr_row}:{pc.kind}:{pc.raw_label}"
    if total_dups <= 1:
        return base

    # Check immediate subheader row (hdr_row + 1) for distinguishing label
    sub_val = ws.cell(hdr_row + 1, pc.col_idx).value
    if isinstance(sub_val, str) and sub_val.strip() and not sub_val.strip().startswith("="):
        return f"{base}|sub={sub_val.strip()}"

    # Check cross-sheet reference fingerprint in first 12 rows beneath header
    # (e.g. Subs - JD!R3 refs 'Model - JD'!R316 vs S3 refs 'Model - JD'!S316)
    max_scan = min(ws.max_row or hdr_row, hdr_row + 15)
    for r in range(hdr_row + 1, max_scan + 1):
        v = ws.cell(r, pc.col_idx).value
        if isinstance(v, str) and v.startswith("=") and "!" in v:
            # Extract sheet name + occurrence ordinal (not raw target col letter which shifts,
            # or combine relative ordinal to remain stable)
            return f"{base}|dup={dup_idx + 1}"

    return f"{base}|dup={dup_idx + 1}"


def _detect_source_note_col(ws: Any, last_period_col: int | None) -> int | None:
    """Identify the rightmost red-text / '<'-prefixed source note column."""
    if last_period_col is None:
        return None
    max_col = ws.max_column or 1
    if max_col <= last_period_col:
        return None
    best_col: int | None = None
    best_score = 0
    max_row = min(ws.max_row or 1, 600)
    for c in range(last_period_col + 1, min(max_col + 1, last_period_col + 6)):
        score = 0
        for r in range(1, max_row + 1):
            val = ws.cell(r, c).value
            if isinstance(val, str) and val.strip():
                txt = val.strip()
                if txt.startswith("<") or "source" in txt.lower() or "来源" in txt or "备注" in txt:
                    score += 3
                else:
                    score += 1
        if score > best_score:
            best_score = score
            best_col = c
    return best_col


def build_period_map(
    ws: Any,
    *,
    fiscal_year_end_month: int | None = None,
    column_identities: dict[str, str] | None = None,
) -> PeriodMap:
    """Build column-level period mapping for a worksheet and its sub-blocks (§3.1).

    Shared by both ``model_style.py`` (profiler) and ``style_audit.py`` (auditor)
    so period classification, fiscal-quarter binding, duplicate-header disambiguation,
    and CAGR separation are guaranteed identical.
    """
    max_row = ws.max_row or 0
    max_col = ws.max_column or 0
    columns_by_header_row: dict[int, dict[int, PeriodColumn]] = {}

    for r in range(1, max_row + 1):
        parsed_in_row: list[tuple[int, str, str, int | None, int | None, bool, str | None]] = []
        has_formula = False
        for c in range(1, max_col + 1):
            val = ws.cell(r, c).value
            if val is None:
                continue
            if isinstance(val, str) and val.startswith("="):
                has_formula = True
                continue
            kind, yr, q, is_fcst, norm = parse_period_label(val)
            if kind in ("annual", "quarter", "cagr_or_delta"):
                parsed_in_row.append((c, str(val).strip(), kind, yr, q, is_fcst, norm))

        # A period header row has >= 2 annual/quarter columns (or row 1 with >= 1) and no formulas
        period_only = [p for p in parsed_in_row if p[2] in ("annual", "quarter")]
        if has_formula and r != 1:
            continue
        if len(period_only) < (1 if r == 1 else 2):
            continue
        # For sub-header rows (r > 1), verify most non-empty numeric/string cells in data cols are period labels
        if r > 1:
            non_empty_data = 0
            for c in range(2, max_col + 1):
                v = ws.cell(r, c).value
                if v is not None and str(v).strip() != "":
                    non_empty_data += 1
            if non_empty_data > 0 and len(parsed_in_row) / non_empty_data < 0.5:
                continue

        # Build PeriodColumn entries with duplicate-label disambiguation
        label_counts: dict[str, int] = {}
        for _, raw_lbl, kind, *_ in parsed_in_row:
            k = f"{kind}:{raw_lbl}"
            label_counts[k] = label_counts.get(k, 0) + 1

        seen_counts: dict[str, int] = {}
        row_map: dict[int, PeriodColumn] = {}
        for col_idx, raw_lbl, kind, yr, q, is_fcst, norm in parsed_in_row:
            k = f"{kind}:{raw_lbl}"
            dup_idx = seen_counts.get(k, 0)
            seen_counts[k] = dup_idx + 1
            pc = PeriodColumn(
                col_idx=col_idx,
                col_letter=get_column_letter(col_idx),
                header_row=r,
                raw_label=raw_lbl,
                kind=kind,
                year=yr,
                quarter=q,
                is_forecast=is_fcst,
                normalized_period=norm,
                column_id="",
                duplicate_index=dup_idx,
            )
            pc.column_id = _disambiguate_column_id(
                ws, r, pc, dup_idx, label_counts[k], column_identities
            )
            row_map[col_idx] = pc
        columns_by_header_row[r] = row_map

    sorted_hdrs = sorted(columns_by_header_row.keys())
    main_header_row = sorted_hdrs[0] if sorted_hdrs else None
    sub_header_rows = sorted_hdrs[1:] if len(sorted_hdrs) > 1 else []
    main_cols = columns_by_header_row.get(main_header_row, {}) if main_header_row else {}

    fy_month = (
        fiscal_year_end_month
        if fiscal_year_end_month is not None
        else _detect_fiscal_year_end_month(ws, main_cols)
    )
    fy_end_q = ((fy_month - 1) // 3) + 1

    # Bind each annual column on the main header row to its 4 fiscal quarters (if all 4 exist)
    annual_to_quarters: dict[int, list[int]] = {}
    q_lookup: dict[tuple[int, int], int] = {}
    for pc in main_cols.values():
        if pc.kind == "quarter" and pc.year is not None and pc.quarter is not None:
            # Prefer first non-PF occurrence if duplicates exist
            if (pc.year, pc.quarter) not in q_lookup and "PF" not in pc.raw_label.upper():
                q_lookup[(pc.year, pc.quarter)] = pc.col_idx

    for pc in main_cols.values():
        if pc.kind == "annual" and pc.year is not None:
            expected_qs = _fiscal_year_quarters(pc.year, fy_end_q)
            if all(eq in q_lookup for eq in expected_qs):
                q_cols = [q_lookup[eq] for eq in expected_qs]
                annual_to_quarters[pc.col_idx] = q_cols
                pc.bound_quarter_cols = q_cols

    last_period_col = max(main_cols.keys()) if main_cols else None
    source_note_col = _detect_source_note_col(ws, last_period_col)

    return PeriodMap(
        sheet_name=ws.title,
        main_header_row=main_header_row,
        sub_header_rows=sub_header_rows,
        fiscal_year_end_month=fy_month,
        fiscal_year_end_quarter=fy_end_q,
        columns_by_header_row=columns_by_header_row,
        annual_to_quarters=annual_to_quarters,
        source_note_col=source_note_col,
    )


# ---------------------------------------------------------------------------
# 4. Shared Cell Role & Row Attribute Classifiers (§3.1, §3.7)
# ---------------------------------------------------------------------------

# Row label pattern sets (order-sensitive: specific patterns checked before generic ones)
_RATIO_LABEL_RE = re.compile(
    r"(?:\byoy\b|\bqoq\b|\bmom\b|\bcagr\b|\bgrowth\b|\bmargin\b|\bopm\b|\bgpm\b|\bnpm\b|"
    r"\btake\s*rate\b|\bmonetization\b|\bratio\b|\b%\s*of\b|\bas\s*%\b|(?:market|revenue|gmv)\s+share\b|\bmix\b|"
    r"\btax\s*rate\b|\beffective\s*tax\b|\binterest\s*rate\b|\byield\b|\breturn\b|\broe\b|\broa\b|\broic\b|"
    r"\bpenetration\b|\bconversion\b|\bretention\b|\bchurn\b|\bdiscount\s*rate\b|\bwacc\b|\bmultiple\b|"
    r"\bp/e\b|\bpe\b|\bev/|\boffshore\s*%|增速|增长率|利润率|毛利率|净利率|费用率|税率|利率|收益率|"
    r"货币化率|占比|比重|份额|比例|倍数|收益率)",
    re.IGNORECASE,
)
_EPS_LABEL_RE = re.compile(
    r"(?:\beps\b|\bepads\b|\bearnings\s*per\s*(?:share|ads|adr)\b|每股收益|每份\s*ads\s*收益)",
    re.IGNORECASE,
)
_WEIGHTED_SHARES_RE = re.compile(
    r"(?:weighted\s*(?:average|avg)\.?\s*(?:diluted|basic)?\s*(?:shares|ads)|"
    r"diluted\s*(?:shares|ads)(?:\s*outstanding)?|加权平均(?:稀释)?股[数本]|稀释股[数本])",
    re.IGNORECASE,
)
_CASH_FLOW_RE = re.compile(
    r"(?:cash\s*flow|\bocf\b|\bcfo\b|\bfcf\b|\bcapex\b|capital\s*expenditure|"
    r"change\s*in\s*(?:cash|working\s*capital)|net\s*cash\s*(?:from|used|provided|generated)|"
    r"repurchase|buyback|dividend|现金流|资本开支|资本支出|自由现金流|回购支出|分红支出)",
    re.IGNORECASE,
)
_BEGINNING_BAL_RE = re.compile(
    r"(?:beginning\s*(?:cash|debt|shares|balance|aum|clients|users|customers)|"
    r"期初现金|期初有息债|期初(?:总)?股数|期初余额)",
    re.IGNORECASE,
)
_POINT_IN_TIME_RE = re.compile(
    r"(?:ending\s*(?:cash|debt|shares|balance|aum|clients|users|customers)|"
    r"cash\s*and\s*(?:cash\s*equivalents|short[- ]term\s*investments)|"
    r"short[- ]term\s*investments|restricted\s*cash|net\s*cash|"
    r"total\s*(?:debt|assets|liabilities|equity|shareholders'?|stockholders'?)|"
    r"long[- ]term\s*debt|short[- ]term\s*debt|interest[- ]bearing\s*debt|"
    r"accounts\s*receivable|inventory|accounts\s*payable|deferred\s*revenue|"
    r"property,?\s*plant|goodwill|intangible\s*assets|book\s*value|\baum\b|"
    r"期末现金|现金及短期投资|期末有息债|总有息债|总资产|总负债|股东权益|期末(?:总)?股数|客户总资产)",
    re.IGNORECASE,
)
_FINANCIAL_AMOUNT_RE = re.compile(
    r"(?:\brevenue|\bsales\b|\bgmv\b|\bcogs\b|\bcost\s*of\b|\bgross\s*profit\b|"
    r"\boperating\s*(?:income|profit|loss|expense)|\bebit\b|\bebita\b|\bebitda\b|"
    r"\bnet\s*(?:income|profit|loss)\b|\bpre[- ]?tax\b|\bebt\b|\bincome\s*tax\b|"
    r"\binterest\s*(?:income|expense)\b|\bnet\s*interest\b|\bsbc\b|share[- ]based\s*comp|"
    r"\bd&a\b|depreciation|amortization|\bs&m\b|\bg&a\b|\br&d\b|fulfillment|"
    r"\btotal\s*(?:revenue|cost|expense|opex|income|profit)|target\s*price|equity\s*value|enterprise\s*value|"
    r"营业收入|总收入|主营收入|营业成本|毛利|营业利润|息税前利润|税前利润|所得税|净利润|归母净利润|"
    r"利息收入|利息支出|净利息|销售费用|管理费用|研发费用|履约费用|股份支付|折旧|摊销|目标价)",
    re.IGNORECASE,
)
_DRIVER_PARAM_LABEL_RE = re.compile(
    r"(?:\bTake\s*rate\b|\bARPU\b|\bASP\b|\bAOV\b|\bCAC\b|\bLTV\b|unit\s*cost|per\s*order|"
    r"average\s*price|commission\s*rate|fee\s*rate|tax\s*rate|interest\s*rate|yield|"
    r"payout\s*ratio|dilution\s*rate|buyback\s*price|vesting|assumption|driver|"
    r"单均|客单价|费率|税率|利率|收益率|派息率|假设|驱动|增速|利润率|费用率|占比|货币化率)",
    re.IGNORECASE,
)
_EXTERNAL_INPUT_SHEET_RE = re.compile(
    r"(?:^inputs?$|^assumptions?$|consensus|street\s*vs\.?\s*house|global\s*comps|comps)",
    re.IGNORECASE,
)
_EXTERNAL_INPUT_LABEL_RE = re.compile(
    r"(?:consensus|street|broker|bloomberg|factset|yahoo|fx\s*rate|exchange\s*rate|"
    r"share\s*price|spot\s*price|current\s*price|market\s*cap|ads\s*ratio|adr\s*ratio|"
    r"一致预期|卖方预期|汇率|当前股价|收盘价|回购授权|回购预算|摊销时间表)",
    re.IGNORECASE,
)

# Formula analysis helpers
_PURE_NUM_LITERAL_RE = re.compile(r"^=\s*\(*\s*[+-]?(?:\d+\.?\d*|\.\d+)%?\s*\)*\s*$")
_CELL_REF_RE = re.compile(
    r"^(?:(?:'((?:[^']|'')+)'|([^'!\[\]]+))!)?\$?([A-Za-z]{1,3})\$?(\d+)$"
)
_RANGE_REF_RE = re.compile(
    r"^(?:(?:'((?:[^']|'')+)'|([^'!\[\]]+))!)?\$?([A-Za-z]{1,3})\$?(\d+):\$?([A-Za-z]{1,3})\$?(\d+)$"
)


def extract_row_label(ws: Any, row: int, max_label_col: int = 4) -> str:
    """Concatenate non-empty text labels in columns A..D (up to first period col - 1)."""
    parts: list[str] = []
    for c in range(1, max_label_col + 1):
        v = ws.cell(row, c).value
        if isinstance(v, str) and v.strip() and not v.strip().startswith("="):
            parts.append(v.strip())
    return " | ".join(parts)


def is_cross_sheet_formula(formula: str, current_sheet: str) -> bool:
    """Return True iff formula has a RANGE operand pointing to a DIFFERENT worksheet.

    Excludes self-sheet references, strings containing ``!``, and bare workbook-scoped
    named ranges (like ``=S193/ex/S197`` which empirically use black font in user models).
    """
    if not isinstance(formula, str) or not formula.startswith("="):
        return False
    try:
        tokens = Tokenizer(formula).items
    except Exception:
        return False
    cur_cf = current_sheet.casefold()
    for tok in tokens:
        if tok.type == Token.OPERAND and tok.subtype == Token.RANGE and "!" in tok.value:
            qual = tok.value.rsplit("!", 1)[0]
            if "[" in qual:
                return True  # external workbook link
            if qual.startswith("'") and qual.endswith("'"):
                qual = qual[1:-1].replace("''", "'")
            if qual.casefold() != cur_cf:
                return True
    return False


def is_pure_number_formula(formula: str) -> bool:
    return bool(isinstance(formula, str) and _PURE_NUM_LITERAL_RE.match(formula))


def is_legal_driver_assumption_formula(formula: str) -> bool:
    """Check whether ``formula`` matches one of the 4 legal driver assumption forms (§3.7).

    1. Single baseline cell ± constant delta: ``=P272+0.05%``, ``=R138+0.3%``
    2. Single baseline cell * constant factor or *(1 ± delta): ``=P40*(1+2%)``, ``=P40*1.02``
    3. Direct link to comparable historical/same-col cell: ``=BE157``, ``=N69``
    4. Same-period comparable indicator ± constant delta: ``=P9-8%``
    Excludes derived ratios of two cells like ``=S9/R9-1`` or ``=S19/S9``.
    """
    if not isinstance(formula, str) or not formula.startswith("="):
        return False
    try:
        tokens = Tokenizer(formula).items
    except Exception:
        return False

    cell_refs: list[str] = []
    num_literals: list[str] = []
    funcs: list[str] = []
    ops: list[str] = []

    for tok in tokens:
        if tok.type == Token.FUNC:
            funcs.append(tok.value.upper().rstrip("("))
        elif tok.type == Token.OPERAND:
            if tok.subtype == Token.RANGE:
                if ":" in tok.value:
                    return False  # range aggregation is not a single-cell relative driver
                cell_refs.append(tok.value)
            elif tok.subtype == Token.NUMBER:
                num_literals.append(tok.value)
            elif tok.subtype == Token.TEXT:
                return False
            elif tok.subtype == Token.ERROR:
                return False
        elif tok.type in (Token.OP_IN, Token.OP_PRE, Token.OP_POST):
            ops.append(tok.value)

    if funcs:
        return False
    # Must reference exactly 1 cell (never 0 = bare literal, never 2+ = ratio of two amounts)
    if len(cell_refs) != 1:
        return False
    # Form 3: direct single-cell reference (=BE157, =N69, =-BE157)
    if len(num_literals) == 0:
        return all(op in ("+", "-") for op in ops)
    # Form 1 & 4: single cell +/- constant (=P272+0.05%, =P9-8%)
    if len(num_literals) == 1:
        return all(op in ("+", "-", "*", "/", "%") for op in ops)
    # Form 2: single cell * (1 +/- delta) (=P40*(1+2%))
    if len(num_literals) == 2:
        try:
            vals = [float(n.rstrip("%")) for n in num_literals]
        except ValueError:
            return False
        if 1.0 in vals and all(op in ("+", "-", "*", "/", "%") for op in ops):
            return True
    return False


def is_derived_ratio_formula(formula: str) -> bool:
    """Return True if formula computes a ratio/growth rate from 2+ cell references (e.g. ``=S9/R9-1``, ``=S19/S9``)."""
    if not isinstance(formula, str) or not formula.startswith("="):
        return False
    try:
        tokens = Tokenizer(formula).items
    except Exception:
        return False
    has_div = False
    ref_count = 0
    for tok in tokens:
        if tok.type == Token.OP_IN and tok.value == "/":
            has_div = True
        elif tok.type == Token.OPERAND and tok.subtype == Token.RANGE:
            ref_count += 1
    return has_div and ref_count >= 2


def is_styled_as_assumption(
    cell: Any,
    palette: dict[int, str] | None = None,
    profile: dict[str, Any] | None = None,
) -> bool:
    """Return True if cell carries the assumption fill/font visual styling."""
    fill_rgb = get_cell_fill_rgb(cell, palette)
    font_rgb = get_cell_font_rgb(cell, palette)
    expected_fill = ASSUMP_FILL
    expected_font = ASSUMP_FONT
    if profile:
        wb_def = profile.get("workbook_defaults") or {}
        assump = wb_def.get("assumption_style") or {}
        expected_fill = assump.get("fill_color") or ASSUMP_FILL
        expected_font = assump.get("font_color") or ASSUMP_FONT
    return (
        fill_rgb is not None
        and is_color_close(fill_rgb, expected_fill)
        and is_color_close(font_rgb, expected_font)
    )


def is_styled_as_hist_estimate(
    cell: Any,
    palette: dict[int, str] | None = None,
    profile: dict[str, Any] | None = None,
) -> bool:
    """Return True if cell carries historical estimate fill (EDEDED gray or FFFFC8 yellow)."""
    fill_rgb = get_cell_fill_rgb(cell, palette)
    if fill_rgb is None:
        return False
    allowed = [HIST_EST_FILL_GRAY, HIST_EST_FILL_YELLOW]
    if profile:
        wb_def = profile.get("workbook_defaults") or {}
        extra = wb_def.get("historical_estimate_fills") or []
        for f in extra:
            if f and f not in allowed:
                allowed.append(f)
    return any(is_color_close(fill_rgb, a, max_channel_diff=14) for a in allowed)


def classify_row_attribute(
    label: str,
    formula: str | None = None,
    *,
    bound_q_cols: list[int] | None = None,
    col_idx: int | None = None,
    is_summed_year: bool = False,
) -> tuple[str, bool]:
    """Determine row attribute for summed-year rules (§3.1) in order: period -> formula -> label.

    Returns ``(attribute, conflict_flag)`` where attribute is one of:
    ``"flow"``, ``"point_in_time"``, ``"weighted_shares"``, ``"eps"``, ``"derived_ratio"``, ``"unknown"``.
    """
    # Step A: Analyze strong label signals (carefully avoiding 'cash flow' vs 'ending cash' collision!)
    label_attr: str | None = None
    if label:
        if _EPS_LABEL_RE.search(label):
            label_attr = "eps"
        elif _WEIGHTED_SHARES_RE.search(label):
            label_attr = "weighted_shares"
        elif _RATIO_LABEL_RE.search(label):
            label_attr = "derived_ratio"
        elif _CASH_FLOW_RE.search(label):
            label_attr = "flow"
        elif _BEGINNING_BAL_RE.search(label):
            label_attr = "beginning_balance"
        elif _POINT_IN_TIME_RE.search(label):
            label_attr = "point_in_time"
        elif _FINANCIAL_AMOUNT_RE.search(label):
            label_attr = "flow"

    # Step B: Analyze formula structure (when formula present)
    formula_attr: str | None = None
    if isinstance(formula, str) and formula.startswith("="):
        fu = formula.upper().replace(" ", "")
        if is_derived_ratio_formula(formula):
            # Could be EPS (NI/shares) or derived_ratio (margin/YoY)
            formula_attr = "eps" if label_attr == "eps" else "derived_ratio"
        elif fu.startswith("=AVERAGE(") or fu.startswith("=AVG("):
            formula_attr = "weighted_shares"
        elif bound_q_cols and len(bound_q_cols) == 4:
            q_letters = [get_column_letter(c) for c in bound_q_cols]
            if re.match(rf"^=\$?{q_letters[3]}\$?\d+$", fu):
                formula_attr = "point_in_time"
            elif f"SUM(" in fu or all(ql in fu for ql in q_letters):
                formula_attr = "eps" if label_attr == "eps" else "flow"

    # Combine with explicit conflict detection
    if label_attr and formula_attr:
        if label_attr == formula_attr:
            return (label_attr, False)
        # Valid cross-compatibilities:
        # - weighted_shares allows =Q4 (point_in_time shape)
        if label_attr == "weighted_shares" and formula_attr == "point_in_time":
            return ("weighted_shares", False)
        # - eps allows both SUM(Q1:Q4) and NI/shares
        if label_attr == "eps" and formula_attr in ("flow", "derived_ratio"):
            return ("eps", False)
        # Strong label overrides bad formula in summed year (so Ending Cash written as SUM is caught!)
        if label_attr in ("point_in_time", "flow", "eps", "weighted_shares", "derived_ratio"):
            return (label_attr, True)

    if label_attr:
        return (label_attr, False)
    if formula_attr:
        return (formula_attr, False)
    return ("unknown", False)


def classify_cell_role(
    ws: Any,
    row: int,
    col: int,
    period_map: PeriodMap,
    *,
    palette: dict[int, str] | None = None,
    profile: dict[str, Any] | None = None,
    row_has_formulas: bool = False,
) -> tuple[str, str]:
    """Determine ``(role, sub_role)`` following §3.7 strict order: row purpose + period first, style second.

    Roles: ``"financial_output"``, ``"driver_assumption"``, ``"external_input"``, ``"not_applicable"``.
    """
    cell = ws.cell(row, col)
    val = cell.value

    # 1. Header rows, sub-block headers, label columns, source-note column
    if row in period_map.all_header_rows():
        return ("not_applicable", "period_header")
    if period_map.source_note_col is not None and col >= period_map.source_note_col:
        return ("not_applicable", "source_note")

    pc = period_map.column_for_cell(row, col)
    if pc is None:
        # Outside mapped period columns (e.g. label cols A..D or valuation summary cells)
        if col <= 4:
            return ("not_applicable", "label_or_section")
        if isinstance(val, str) and val.startswith("="):
            return ("financial_output", "non_period_formula")
        return ("not_applicable", "non_period_other")

    if pc.kind == "cagr_or_delta":
        if isinstance(val, str) and val.startswith("="):
            return ("financial_output", "derived_ratio")
        return ("not_applicable", "cagr_blank_or_text")

    if val is None or (isinstance(val, str) and not val.strip()):
        return ("not_applicable", "blank")
    if isinstance(val, str) and not val.startswith("="):
        return ("not_applicable", "text")

    label = extract_row_label(ws, row)
    is_formula = isinstance(val, str) and val.startswith("=")
    is_summed_year = pc.kind == "annual" and pc.col_idx in period_map.annual_to_quarters

    # 2. External input sheets / explicit external input rows
    if not is_formula and (
        _EXTERNAL_INPUT_SHEET_RE.search(ws.title)
        or _EXTERNAL_INPUT_LABEL_RE.search(label)
    ):
        return ("external_input", "static_external")

    # 3. Historical period columns
    if not pc.is_forecast:
        if is_styled_as_hist_estimate(cell, palette, profile):
            return ("driver_assumption", "historical_estimate")
        if is_formula:
            nfmt = str(cell.number_format or "")
            is_pct_or_ratio = bool(_RATIO_LABEL_RE.search(label)) or ("%" in nfmt)
            if is_pct_or_ratio and not _EPS_LABEL_RE.search(label) and (is_derived_ratio_formula(val) or _RATIO_LABEL_RE.search(label)):
                return ("financial_output", "derived_ratio")
            return ("financial_output", "historical_formula")
        return ("not_applicable", "historical_actual")

    # 4. Forecast period columns — Summed year ALWAYS financial_output (§3.1, §3.7)
    if is_summed_year:
        nfmt = str(cell.number_format or "")
        is_pct_or_ratio = bool(_RATIO_LABEL_RE.search(label)) or ("%" in nfmt)
        if is_formula and is_pct_or_ratio and not _EPS_LABEL_RE.search(label) and (is_derived_ratio_formula(val) or _RATIO_LABEL_RE.search(label)):
            return ("financial_output", "derived_ratio")
        return ("financial_output", "summed_year_output")

    # 5. Non-summed forecast period columns (forecast quarters & standalone forecast years like 2028E)
    # Check row purpose FIRST before looking at cell styling!
    # Ratio/driver qualifiers (YoY, Growth, Margin, Rate, %) take precedence over base nouns (Revenue, Net Income).
    is_eps_label = bool(_EPS_LABEL_RE.search(label))
    is_ratio_or_driver_label = (not is_eps_label) and bool(
        _RATIO_LABEL_RE.search(label) or _DRIVER_PARAM_LABEL_RE.search(label)
    )
    is_amount_or_balance_label = (not is_ratio_or_driver_label) and bool(
        is_eps_label
        or _WEIGHTED_SHARES_RE.search(label)
        or _CASH_FLOW_RE.search(label)
        or _BEGINNING_BAL_RE.search(label)
        or _POINT_IN_TIME_RE.search(label)
        or _FINANCIAL_AMOUNT_RE.search(label)
    )

    if is_formula:
        nfmt = str(cell.number_format or "")
        is_pct_or_ratio = bool(_RATIO_LABEL_RE.search(label)) or ("%" in nfmt)
        is_xlink = is_cross_sheet_formula(val, ws.title)
        if is_derived_ratio_formula(val) and is_pct_or_ratio and not is_eps_label:
            return ("financial_output", "derived_ratio")
        # Cross-sheet links without assumption styling are financial_output links (e.g., Model - Tiger!G95)
        if is_xlink and not is_styled_as_assumption(cell, palette, profile):
            if is_pct_or_ratio and not is_eps_label:
                return ("financial_output", "derived_ratio")
            return ("financial_output", "forecast_formula")
        if is_legal_driver_assumption_formula(val):
            # If row is clearly a driver/ratio row (not a financial result line), or styled as assumption on a driver row
            if is_ratio_or_driver_label and not is_amount_or_balance_label:
                return ("driver_assumption", "relative_driver_formula")
            if is_styled_as_assumption(cell, palette, profile) and not is_amount_or_balance_label:
                return ("driver_assumption", "relative_driver_formula")
        if is_pure_number_formula(val):
            if is_ratio_or_driver_label and not is_amount_or_balance_label:
                return ("driver_assumption", "literal_driver_formula")
            return ("financial_output", "literal_output_formula")
        return ("financial_output", "forecast_formula")

    # Numeric literal in a non-summed forecast column:
    # CRITICAL (§3.7): Financial amount/result rows are ALWAYS financial_output even if painted yellow+blue!
    if is_ratio_or_driver_label and not is_amount_or_balance_label:
        return ("driver_assumption", "driver_constant")
    if is_amount_or_balance_label:
        return ("financial_output", "forecast_hardcode")
    # Ambiguous unlabeled / custom metric row:
    # If other period columns on this same row have formulas (it's a calculated row), treat as financial_output;
    # otherwise if styled as assumption or has driver-like percentage format, treat as driver_assumption.
    nfmt = str(cell.number_format or "")
    if "%" in nfmt and not row_has_formulas:
        return ("driver_assumption", "driver_constant")
    if row_has_formulas:
        return ("financial_output", "forecast_hardcode")
    if is_styled_as_assumption(cell, palette, profile):
        return ("driver_assumption", "driver_constant")
    return ("financial_output", "forecast_hardcode")


# ---------------------------------------------------------------------------
# 5. Workbook Style Profiler (`profile_workbook_style`) (§4.2)
# ---------------------------------------------------------------------------


def _mode_if_locked(counts: dict[str, int], min_samples: int = 5, min_ratio: float = 0.60) -> str | None:
    total = sum(counts.values())
    if total < min_samples:
        return None
    top_val, top_cnt = max(counts.items(), key=lambda kv: kv[1])
    if top_cnt / total >= min_ratio:
        return top_val
    return None


def _inspect_package_parts(xlsx_path: Path) -> dict[str, Any]:
    """Inspect ZIP package for ``<extLst>``, media, charts, externalLinks, and definedNames."""
    extlst_parts: list[str] = []
    media_parts: list[str] = []
    chart_parts: list[str] = []
    external_link_parts: list[str] = []
    ext_refs_in_wb = 0
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        for name in zf.namelist():
            if name.startswith("xl/media/"):
                media_parts.append(name)
            elif name.startswith("xl/charts/") and name.endswith(".xml"):
                chart_parts.append(name)
            elif "externalLinks" in name:
                external_link_parts.append(name)
            if name.endswith(".xml"):
                data = zf.read(name)
                if b"extLst" in data:
                    extlst_parts.append(name)
                if name == "xl/workbook.xml":
                    root = etree.fromstring(data, XML_PARSER)
                    ext_refs_in_wb = sum(
                        1 for el in root.iter() if el.tag == f"{{{SML}}}externalReference"
                    )
    return {
        "extlst_parts": sorted(extlst_parts),
        "media_parts": sorted(media_parts),
        "chart_parts": sorted(chart_parts),
        "external_link_parts": sorted(external_link_parts),
        "external_references_in_workbook_xml": ext_refs_in_wb,
    }


def default_style_profile() -> dict[str, Any]:
    """Return the default style profile from §4.1 (used for brand-new models)."""
    return {
        "source": "default",
        "workbook_defaults": {
            "allowed_fonts": sorted(ALLOWED_DEFAULT_FONTS),
            "dominant_font_size": FONT_SIZE,
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
            "allowed_fills": sorted(DEFAULT_ALLOWED_FILLS),
            "section_fills": [SEC_REPORT_FILL, SEC_SEGMENT_FILL, SEC_SUBMODEL_FILL],
            "number_formats": [FMT_INT, FMT_DEC1, FMT_DEC2, FMT_PCT, FMT_PCT2, FMT_PP, FMT_MULT],
            "gridlines_hidden": True,
        },
        "sheet_overrides": {},
        "ambiguous_cells": [],
    }


def profile_workbook_style(
    xlsx_path: str | Path,
    *,
    fiscal_year_end_month: int | None = None,
) -> dict[str, Any]:
    """Extract full-workbook & per-sheet style profile without truncating sheets or rows (§4.2)."""
    path = Path(xlsx_path)
    palette = extract_theme_palette(path)
    pkg_info = _inspect_package_parts(path)
    wb = openpyxl.load_workbook(path, data_only=False)

    # First pass: collect sheet references from all visible sheets to identify referenced hidden sheets
    referenced_sheets: set[str] = set()
    sheet_stats: dict[str, tuple[int, int, int]] = {}  # title -> (non_empty, formulas, numeric)
    for ws in wb.worksheets:
        ne = fm = nm = 0
        for row in ws.iter_rows(values_only=True):
            for v in row:
                if v is None:
                    continue
                if isinstance(v, str):
                    if not v.strip():
                        continue
                    ne += 1
                    if v.startswith("="):
                        fm += 1
                        if ws.sheet_state == "visible" and "!" in v:
                            try:
                                for tok in Tokenizer(v).items:
                                    if tok.type == Token.OPERAND and tok.subtype == Token.RANGE and "!" in tok.value:
                                        q = tok.value.rsplit("!", 1)[0]
                                        if "[" not in q:
                                            if q.startswith("'") and q.endswith("'"):
                                                q = q[1:-1].replace("''", "'")
                                            referenced_sheets.add(q.casefold())
                            except Exception:
                                pass
                else:
                    ne += 1
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        nm += 1
        sheet_stats[ws.title] = (ne, fm, nm)

    # Global counters
    g_fonts: dict[str, int] = {}
    g_sizes: dict[str, int] = {}
    g_actual_colors: dict[str, int] = {}
    g_link_colors: dict[str, int] = {}
    g_assump_fills: dict[str, int] = {}
    g_assump_fonts: dict[str, int] = {}
    g_assump_borders: dict[str, int] = {}
    g_hist_est_fills: dict[str, int] = {}
    g_all_fills: set[str] = set(DEFAULT_ALLOWED_FILLS)
    g_all_nfmts: set[str] = set()

    sheet_overrides: dict[str, Any] = {}
    ambiguous_cells: list[dict[str, str]] = []

    for ws in wb.worksheets:
        ne, fm, nm = sheet_stats[ws.title]
        state = ws.sheet_state or "visible"
        if state != "visible":
            sheet_type = "hidden_sheet"
        elif fm == 0 and nm == 0 and ne <= 2:
            sheet_type = "blank_divider"
        else:
            sheet_type = "calculation_sheet"

        pmap = build_period_map(ws, fiscal_year_end_month=fiscal_year_end_month)

        # Sheet-level style stats
        s_fonts: dict[str, int] = {}
        s_sizes: dict[str, int] = {}
        s_fills: set[str] = set()
        s_nfmts: set[str] = set()
        s_formula_nfmts: set[str] = set()
        s_section_fills: set[str] = set()
        s_has_general_formula = False

        if sheet_type == "calculation_sheet":
            # Precompute which rows contain formulas
            formula_rows: set[int] = set()
            for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                if any(isinstance(v, str) and v.startswith("=") for v in row):
                    formula_rows.add(r_idx)

            for row_cells in ws.iter_rows():
                for cell in row_cells:
                    v = cell.value
                    if v is None or (isinstance(v, str) and not v.strip()):
                        continue
                    f = cell.font
                    if f and f.name:
                        s_fonts[f.name] = s_fonts.get(f.name, 0) + 1
                        g_fonts[f.name] = g_fonts.get(f.name, 0) + 1
                    if f and f.size is not None:
                        sz_key = str(float(f.size))
                        s_sizes[sz_key] = s_sizes.get(sz_key, 0) + 1
                        g_sizes[sz_key] = g_sizes.get(sz_key, 0) + 1
                    fl_rgb = get_cell_fill_rgb(cell, palette)
                    if fl_rgb:
                        s_fills.add(fl_rgb)
                        g_all_fills.add(fl_rgb)
                        if cell.column == 1 and cell.row > 1:
                            s_section_fills.add(fl_rgb)
                    nfmt = str(cell.number_format or "General")
                    s_nfmts.add(nfmt)
                    g_all_nfmts.add(nfmt)

                    if isinstance(v, str) and v.startswith("="):
                        s_formula_nfmts.add(nfmt)
                        if nfmt == "General":
                            s_has_general_formula = True

                    role, sub_role = classify_cell_role(
                        ws,
                        cell.row,
                        cell.column,
                        pmap,
                        palette=palette,
                        row_has_formulas=(cell.row in formula_rows),
                    )
                    fn_rgb = get_cell_font_rgb(cell, palette)

                    if sub_role == "historical_actual" and fn_rgb:
                        g_actual_colors[fn_rgb] = g_actual_colors.get(fn_rgb, 0) + 1
                    elif sub_role == "historical_estimate" and fl_rgb:
                        g_hist_est_fills[fl_rgb] = g_hist_est_fills.get(fl_rgb, 0) + 1
                    elif role == "driver_assumption":
                        if fl_rgb:
                            g_assump_fills[fl_rgb] = g_assump_fills.get(fl_rgb, 0) + 1
                        if fn_rgb:
                            g_assump_fonts[fn_rgb] = g_assump_fonts.get(fn_rgb, 0) + 1
                        b = cell.border
                        if b and b.left and b.left.style:
                            bs = b.left.style
                            g_assump_borders[bs] = g_assump_borders.get(bs, 0) + 1
                    elif role == "financial_output" and isinstance(v, str) and v.startswith("="):
                        if is_cross_sheet_formula(v, ws.title) and fn_rgb:
                            g_link_colors[fn_rgb] = g_link_colors.get(fn_rgb, 0) + 1
                    elif role == "financial_output" and sub_role == "forecast_hardcode":
                        if is_styled_as_assumption(cell, palette) and len(ambiguous_cells) < 50:
                            ambiguous_cells.append({
                                "sheet": ws.title,
                                "cell": cell.coordinate,
                                "reason": "forecast numeric constant styled as assumption on potential output row",
                            })

        # Outline groups & widths
        col_outline_groups: list[str] = []
        col_widths: dict[str, float] = {}
        for col_key, dim in ws.column_dimensions.items():
            if dim.width is not None:
                col_widths[str(col_key)] = round(float(dim.width), 2)
            if (dim.outlineLevel or 0) > 0:
                col_outline_groups.append(str(col_key))
        row_outline_count = sum(
            1 for dim in ws.row_dimensions.values() if (dim.outlineLevel or 0) > 0
        )

        dom_size = None
        if s_sizes:
            dom_size = float(max(s_sizes.items(), key=lambda kv: kv[1])[0])

        gridlines_hidden = False
        if ws.views and ws.views.sheetView:
            sv = ws.views.sheetView[0]
            if sv.showGridLines is False:
                gridlines_hidden = True

        sheet_overrides[ws.title] = {
            "sheet_type": sheet_type,
            "sheet_state": state,
            "referenced_by_visible": ws.title.casefold() in referenced_sheets,
            "non_empty_cells": ne,
            "formula_cells": fm,
            "numeric_cells": nm,
            "dominant_font_size": dom_size,
            "font_sizes_observed": sorted(float(k) for k in s_sizes),
            "fonts_observed": sorted(s_fonts.keys()),
            "fills_observed": sorted(s_fills),
            "section_fills": sorted(s_section_fills),
            "formula_number_formats": sorted(s_formula_nfmts),
            "has_general_formula_nfmt": s_has_general_formula,
            "freeze_panes": ws.freeze_panes,
            "gridlines_hidden": gridlines_hidden,
            "outline_col_count": len(col_outline_groups),
            "outline_row_count": row_outline_count,
            "source_note_col": pmap.source_note_col,
            "fiscal_year_end_month": pmap.fiscal_year_end_month,
            "period_headers": {
                "main_header_row": pmap.main_header_row,
                "sub_header_rows": pmap.sub_header_rows,
                "annual_to_quarters": {
                    get_column_letter(k): [get_column_letter(c) for c in v]
                    for k, v in pmap.annual_to_quarters.items()
                },
            },
        }

    # Build locked workbook_defaults (preferring observed mode >=5 samples & >=60%, else §4.1 defaults)
    allowed_fonts = sorted(set(ALLOWED_DEFAULT_FONTS) | {k for k, v in g_fonts.items() if v >= 5})
    dom_global_size_str = _mode_if_locked(g_sizes)
    dom_global_size = float(dom_global_size_str) if dom_global_size_str else float(FONT_SIZE)

    hist_est_fills = sorted(
        {HIST_EST_FILL_GRAY, HIST_EST_FILL_YELLOW}
        | {k for k, v in g_hist_est_fills.items() if v >= 3}
    )

    wb_defaults = {
        "allowed_fonts": allowed_fonts,
        "dominant_font_size": dom_global_size,
        "actual_font_color": _mode_if_locked(g_actual_colors) or ACTUAL_FONT,
        "formula_font_color": FORMULA_FONT,
        "link_font_color": _mode_if_locked(g_link_colors) or LINK_FONT,
        "source_note_font_color": SOURCE_NOTE_FONT,
        "target_price_font_color": TARGET_PRICE_FONT,
        "assumption_style": {
            "font_color": _mode_if_locked(g_assump_fonts) or ASSUMP_FONT,
            "fill_color": _mode_if_locked(g_assump_fills) or ASSUMP_FILL,
            "border_style": _mode_if_locked(g_assump_borders) or ASSUMP_BORDER,
        },
        "historical_estimate_fills": hist_est_fills,
        "allowed_fills": sorted(g_all_fills),
        "section_fills": [SEC_REPORT_FILL, SEC_SEGMENT_FILL, SEC_SUBMODEL_FILL],
        "number_formats": sorted(g_all_nfmts) or [FMT_INT, FMT_DEC1, FMT_DEC2, FMT_PCT],
        "gridlines_hidden": True,
    }

    return {
        "source": str(path),
        "sheet_count": len(wb.sheetnames),
        "theme_palette": {str(k): v for k, v in sorted(palette.items())},
        "package_info": pkg_info,
        "workbook_defaults": wb_defaults,
        "sheet_overrides": sheet_overrides,
        "ambiguous_cells": ambiguous_cells,
    }


# ---------------------------------------------------------------------------
# 6. Cell Writing Helpers & Column Style Cloner (§4.1, §4.3)
# ---------------------------------------------------------------------------


def _pick_font_name(value: Any, profile: dict[str, Any] | None = None) -> str:
    if isinstance(value, str) and _ZH_CHAR_RE.search(value):
        return FONT_ZH
    return FONT_EN


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
    """Apply Dalton house style (or extracted baseline StyleProfile) to an openpyxl cell."""
    wb_def = (profile or default_style_profile()).get("workbook_defaults", {})
    sh_ov = ((profile or {}).get("sheet_overrides") or {}).get(sheet_name or "", {})
    font_size = sh_ov.get("dominant_font_size") or wb_def.get("dominant_font_size") or FONT_SIZE
    font_name = _pick_font_name(cell.value, profile)

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
        cell.font = Font(name=font_name, size=font_size, bold=is_total, italic=is_percent or is_ratio, color=HIST_EST_FONT)
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
        # financial_output (same-sheet black or cross-sheet green; total bold; derived ratio italic)
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
    """Clone cell styles (font/fill/border/number_format/alignment) & column width from an adjacent period column."""
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
# 7. Package-Level XML Patch Writer & 4-Layer External Link Cleaner (§4.3, §9.3)
# ---------------------------------------------------------------------------


def _sheet_part_map(payload: dict[str, bytes]) -> dict[str, str]:
    wb_root = etree.fromstring(payload["xl/workbook.xml"], XML_PARSER)
    rels_root = etree.fromstring(payload["xl/_rels/workbook.xml.rels"], XML_PARSER)
    id_to_target = {r.get("Id"): r.get("Target", "") for r in rels_root}
    mapping: dict[str, str] = {}
    for sh in wb_root.iter(f"{{{SML}}}sheet"):
        rid = sh.get(f"{{{REL_NS}}}id")
        target = id_to_target.get(rid, "")
        part = (
            target.lstrip("/")
            if target.startswith("/")
            else posixpath.normpath(posixpath.join("xl", target))
        )
        mapping[sh.get("name", "")] = part
    return mapping


def _shift_a1_ref(ref: str, *, at_col: int, count: int) -> str:
    """Shift column letters >= ``at_col`` rightward by ``count`` inside an A1 cell/range token."""
    def _shift_part(part: str) -> str:
        m = re.match(r"^(\$?)([A-Za-z]{1,3})(\$?)(\d+)$", part)
        if not m:
            return part
        c_idx = column_index_from_string(m.group(2))
        if c_idx >= at_col:
            c_idx += count
        return f"{m.group(1)}{get_column_letter(c_idx)}{m.group(3)}{m.group(4)}"

    if ":" in ref:
        a, b = ref.split(":", 1)
        return f"{_shift_part(a)}:{_shift_part(b)}"
    return _shift_part(ref)


def shift_ext_and_chart_refs(
    payload: dict[str, bytes],
    *,
    sheet_name: str,
    at_col: int,
    count: int,
) -> dict[str, int]:
    """Update ``<extLst>`` conditional formatting ``sqref`` and chart formula refs after column insertion."""
    counts = {"ext_sqrefs_updated": 0, "chart_refs_updated": 0}
    sp_map = _sheet_part_map(payload)
    ws_part = sp_map.get(sheet_name)

    # 1. Update <extLst> sqref nodes in the target worksheet
    if ws_part and ws_part in payload:
        root = etree.fromstring(payload[ws_part], XML_PARSER)
        changed = False
        for ext_lst in root.findall(f"{{{SML}}}extLst"):
            for sq in ext_lst.iter():
                local = etree.QName(sq).localname if isinstance(sq.tag, str) else ""
                if local == "sqref" and sq.text:
                    tokens = sq.text.split()
                    new_tokens = [_shift_a1_ref(t, at_col=at_col, count=count) for t in tokens]
                    if new_tokens != tokens:
                        sq.text = " ".join(new_tokens)
                        counts["ext_sqrefs_updated"] += 1
                        changed = True
        if changed:
            payload[ws_part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    # 2. Update chart <c:f> formula references pointing to sheet_name
    chart_re = re.compile(
        r"^(?:'((?:[^']|'')+)'|([^'!\[\]]+))!(\$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?)$"
    )
    for part_name in list(payload.keys()):
        if not (part_name.startswith("xl/charts/") and part_name.endswith(".xml")):
            continue
        root = etree.fromstring(payload[part_name], XML_PARSER)
        changed = False
        for f_el in root.iter(f"{{{CHART_NS}}}f"):
            txt = (f_el.text or "").strip()
            m = chart_re.match(txt)
            if not m:
                continue
            sh = m.group(1).replace("''", "'") if m.group(1) else m.group(2)
            if sh.casefold() != sheet_name.casefold():
                continue
            shifted = _shift_a1_ref(m.group(3), at_col=at_col, count=count)
            if shifted != m.group(3):
                prefix = f"'{m.group(1)}'" if m.group(1) else m.group(2)
                f_el.text = f"{prefix}!{shifted}"
                counts["chart_refs_updated"] += 1
                changed = True
        if changed:
            payload[part_name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    return counts


def _clean_external_links_in_payload(payload: dict[str, bytes]) -> dict[str, int]:
    """Remove all 4 OOXML external-link artifacts from an in-memory ZIP payload (§9.3 #1).

    1. ``<externalReferences>`` in ``xl/workbook.xml``
    2. ``externalLink`` ``<Relationship>`` entries in ``xl/_rels/workbook.xml.rels``
    3. ``xl/externalLinks/*`` package parts (both XML and ``_rels/*.rels``)
    4. ``/xl/externalLinks/...`` ``<Override>`` declarations in ``[Content_Types].xml``
    Also strips ``xl/calcChain.xml`` so recalculation rebuilds cleanly.
    """
    removed = {
        "external_link_parts": 0,
        "workbook_external_refs": 0,
        "workbook_rels": 0,
        "content_types": 0,
    }

    # 3. Remove xl/externalLinks/* parts and calcChain
    for key in list(payload.keys()):
        if key.startswith("xl/externalLinks/"):
            del payload[key]
            removed["external_link_parts"] += 1
        elif key == "xl/calcChain.xml":
            del payload[key]

    # 1. Remove <externalReferences> from xl/workbook.xml
    if "xl/workbook.xml" in payload:
        wb_root = etree.fromstring(payload["xl/workbook.xml"], XML_PARSER)
        for ext_refs in list(wb_root.findall(f"{{{SML}}}externalReferences")):
            removed["workbook_external_refs"] += len(
                ext_refs.findall(f"{{{SML}}}externalReference")
            )
            wb_root.remove(ext_refs)
        payload["xl/workbook.xml"] = etree.tostring(
            wb_root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    # 2. Remove externalLink & calcChain rels from xl/_rels/workbook.xml.rels
    if "xl/_rels/workbook.xml.rels" in payload:
        rels_root = etree.fromstring(payload["xl/_rels/workbook.xml.rels"], XML_PARSER)
        for rel in list(rels_root):
            rtype = rel.get("Type", "")
            target = rel.get("Target", "")
            if "externalLink" in rtype or "externalLinks/" in target:
                rels_root.remove(rel)
                removed["workbook_rels"] += 1
            elif "calcChain" in rtype or target.endswith("calcChain.xml"):
                rels_root.remove(rel)
        payload["xl/_rels/workbook.xml.rels"] = etree.tostring(
            rels_root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    # 4. Remove /xl/externalLinks/ and calcChain overrides from [Content_Types].xml
    if "[Content_Types].xml" in payload:
        ct_root = etree.fromstring(payload["[Content_Types].xml"], XML_PARSER)
        for ov in list(ct_root.findall(f"{{{CT_NS}}}Override")):
            pname = ov.get("PartName", "")
            if "/xl/externalLinks/" in pname:
                ct_root.remove(ov)
                removed["content_types"] += 1
            elif pname == "/xl/calcChain.xml":
                ct_root.remove(ov)
        payload["[Content_Types].xml"] = etree.tostring(
            ct_root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    return removed


def _set_cell_xml_value(
    c_el: etree._Element,
    value: Any,
    *,
    style_idx: int | None = None,
) -> None:
    """Set formula / numeric / string / blank value on an OOXML ``<c>`` element."""
    if style_idx is not None:
        c_el.set("s", str(style_idx))
    for child in list(c_el):
        c_el.remove(child)
    if value is None or value == "":
        if "t" in c_el.attrib:
            del c_el.attrib["t"]
        return
    if isinstance(value, bool):
        c_el.set("t", "b")
        v = etree.SubElement(c_el, f"{{{SML}}}v")
        v.text = "1" if value else "0"
    elif isinstance(value, (int, float)):
        if "t" in c_el.attrib:
            del c_el.attrib["t"]
        v = etree.SubElement(c_el, f"{{{SML}}}v")
        v.text = repr(value)
    elif isinstance(value, str) and value.startswith("="):
        if "t" in c_el.attrib:
            del c_el.attrib["t"]
        f_el = etree.SubElement(c_el, f"{{{SML}}}f")
        f_el.text = value[1:]
    else:
        c_el.set("t", "inlineStr")
        is_el = etree.SubElement(c_el, f"{{{SML}}}is")
        t_el = etree.SubElement(is_el, f"{{{SML}}}t")
        t_el.text = str(value)


def patch_workbook_xml(
    src_xlsx: str | Path,
    out_xlsx: str | Path,
    cell_updates: dict[str, dict[str, Any]] | None = None,
    *,
    remove_external_links: bool = False,
    disable_iterative_calc: bool = True,
    shift_specs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Patch target cells & workbook XML directly inside the ZIP archive (§4.3 #3).

    Preserves ``<extLst>``, ``<legacyDrawing>``, ``xl/media/*`` (including ``.wmf``/``.emf``),
    charts, drawings, and every untouched package part byte-for-byte.

    ``cell_updates`` maps ``sheet_name -> {coord: value | {"value": ..., "style_idx": ...}}``.
    ``shift_specs`` is an optional list of ``{"sheet": str, "at_col": int, "count": int}``
    to shift ``<extLst>`` conditional formatting ``sqref`` and chart references.
    """
    src = Path(src_xlsx)
    out = Path(out_xlsx)
    if src.resolve() == out.resolve():
        raise ValueError("Refusing to overwrite baseline workbook in place; pass a distinct out_xlsx")

    with zipfile.ZipFile(src, "r") as zf:
        infos = zf.infolist()
        payload: dict[str, bytes] = {info.filename: zf.read(info.filename) for info in infos}

    sp_map = _sheet_part_map(payload)
    cells_patched = 0

    # 1. Patch worksheet <sheetData> cells without touching <extLst> or trailing nodes
    for sheet_name, updates in (cell_updates or {}).items():
        part = sp_map.get(sheet_name)
        if not part or part not in payload:
            raise KeyError(f"Worksheet not found in package: {sheet_name!r}")
        root = etree.fromstring(payload[part], XML_PARSER)
        sheet_data = root.find(f"{{{SML}}}sheetData")
        if sheet_data is None:
            sheet_data = etree.SubElement(root, f"{{{SML}}}sheetData")

        row_map: dict[int, etree._Element] = {}
        for row_el in sheet_data.findall(f"{{{SML}}}row"):
            r_attr = row_el.get("r")
            if r_attr and r_attr.isdigit():
                row_map[int(r_attr)] = row_el

        for coord, spec in updates.items():
            col_ltr, row_num = coordinate_from_string(coord.upper())
            col_num = column_index_from_string(col_ltr)
            norm_coord = f"{col_ltr}{row_num}"
            val = spec["value"] if isinstance(spec, dict) and "value" in spec else spec
            s_idx = spec.get("style_idx") if isinstance(spec, dict) else None

            row_el = row_map.get(row_num)
            if row_el is None:
                row_el = etree.Element(f"{{{SML}}}row", r=str(row_num))
                # Insert row in ascending order
                inserted = False
                for existing in sheet_data.findall(f"{{{SML}}}row"):
                    er = int(existing.get("r", "0"))
                    if er > row_num:
                        sheet_data.insert(sheet_data.index(existing), row_el)
                        inserted = True
                        break
                if not inserted:
                    sheet_data.append(row_el)
                row_map[row_num] = row_el

            c_el = None
            for existing_c in row_el.findall(f"{{{SML}}}c"):
                if (existing_c.get("r") or "").upper() == norm_coord:
                    c_el = existing_c
                    break
            if c_el is None:
                c_el = etree.Element(f"{{{SML}}}c", r=norm_coord)
                inserted = False
                for existing_c in row_el.findall(f"{{{SML}}}c"):
                    ec_ltr, _ = coordinate_from_string((existing_c.get("r") or "A1").upper())
                    if column_index_from_string(ec_ltr) > col_num:
                        row_el.insert(row_el.index(existing_c), c_el)
                        inserted = True
                        break
                if not inserted:
                    row_el.append(c_el)

            _set_cell_xml_value(c_el, val, style_idx=s_idx)
            cells_patched += 1

        payload[part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    # 2. Shift extLst / chart refs if requested
    shift_totals = {"ext_sqrefs_updated": 0, "chart_refs_updated": 0}
    for sp in shift_specs or []:
        res = shift_ext_and_chart_refs(
            payload,
            sheet_name=sp["sheet"],
            at_col=int(sp["at_col"]),
            count=int(sp["count"]),
        )
        shift_totals["ext_sqrefs_updated"] += res["ext_sqrefs_updated"]
        shift_totals["chart_refs_updated"] += res["chart_refs_updated"]

    # 3. Disable iterative calc & request full recalc
    if disable_iterative_calc and "xl/workbook.xml" in payload:
        wb_root = etree.fromstring(payload["xl/workbook.xml"], XML_PARSER)
        calc_pr = wb_root.find(f"{{{SML}}}calcPr")
        if calc_pr is None:
            calc_pr = etree.SubElement(wb_root, f"{{{SML}}}calcPr")
        calc_pr.set("iterate", "0")
        calc_pr.set("fullCalcOnLoad", "1")
        payload["xl/workbook.xml"] = etree.tostring(
            wb_root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    # 4. Optional 4-layer external link removal
    ext_removed = {}
    if remove_external_links:
        ext_removed = _clean_external_links_in_payload(payload)
    else:
        # Still remove stale calcChain if cells were patched
        if cells_patched > 0 and "xl/calcChain.xml" in payload:
            del payload["xl/calcChain.xml"]

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    written_keys: set[str] = set()
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            if info.filename in payload:
                zout.writestr(info.filename, payload[info.filename], compress_type=info.compress_type)
                written_keys.add(info.filename)
        for key, data in payload.items():
            if key not in written_keys:
                zout.writestr(key, data)
    os.replace(tmp, out)

    return {
        "status": "success",
        "source": str(src),
        "output": str(out),
        "cells_patched": cells_patched,
        "external_links_removed": ext_removed,
        "shift_totals": shift_totals,
        "part_diff": diff_package_parts(src, out),
    }


def diff_package_parts(baseline_xlsx: str | Path, new_xlsx: str | Path) -> dict[str, Any]:
    """Compare ZIP package parts, ``<extLst>``, media, charts, and externalLinks between two workbooks (§4.3)."""
    base_info = _inspect_package_parts(Path(baseline_xlsx))
    new_info = _inspect_package_parts(Path(new_xlsx))

    with zipfile.ZipFile(baseline_xlsx, "r") as zb:
        base_parts = set(zb.namelist())
    with zipfile.ZipFile(new_xlsx, "r") as zn:
        new_parts = set(zn.namelist())

    lost_extlst = sorted(set(base_info["extlst_parts"]) - set(new_info["extlst_parts"]))
    lost_media = sorted(set(base_info["media_parts"]) - set(new_info["media_parts"]))
    lost_charts = sorted(set(base_info["chart_parts"]) - set(new_info["chart_parts"]))

    return {
        "added_parts": sorted(new_parts - base_parts),
        "removed_parts": sorted(base_parts - new_parts),
        "extlst_preserved": len(lost_extlst) == 0,
        "lost_extlst_parts": lost_extlst,
        "media_preserved": len(lost_media) == 0,
        "lost_media_parts": lost_media,
        "charts_preserved": len(lost_charts) == 0,
        "lost_chart_parts": lost_charts,
        "baseline_external_link_parts": len(base_info["external_link_parts"]),
        "new_external_link_parts": len(new_info["external_link_parts"]),
    }


# ---------------------------------------------------------------------------
# 8. Shared Model Lineage Helpers (`model_lineage.json`) (§7.2, Codex r3 #4)
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
    output_sha256: str | None = None,
    baseline_path: str | Path | None = None,
    baseline_sha256: str | None = None,
    profile_path: str | Path | None = None,
    fiscal_year_end_month: int | None = None,
    column_identities: dict[str, dict[str, str]] | None = None,
    blank_forecast_registrations: list[dict[str, str]] | None = None,
    final_delivery: bool = True,
) -> dict[str, Any]:
    """Record a model build/revision entry and pin ``final_delivery`` in ``model_lineage.json`` (§7.2)."""
    lp = Path(lineage_path)
    if lp.is_file():
        data = json.loads(lp.read_text(encoding="utf-8"))
    else:
        data = {"schema_version": 1, "final_delivery": None, "entries": []}

    out_p = Path(output_path)
    out_hash = output_sha256 or (file_sha256(out_p) if out_p.is_file() else None)
    base_str = str(baseline_path) if baseline_path else None
    base_hash = baseline_sha256
    if base_hash is None and baseline_path and Path(baseline_path).is_file():
        base_hash = file_sha256(baseline_path)

    entry: dict[str, Any] = {
        "output_path": str(output_path),
        "output_sha256": out_hash,
        "baseline_path": base_str,
        "baseline_sha256": base_hash,
        "profile_path": str(profile_path) if profile_path else None,
        "fiscal_year_end_month": fiscal_year_end_month,
        "column_identities": column_identities or {},
        "blank_forecast_registrations": blank_forecast_registrations or [],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

    entries = [e for e in data.get("entries", []) if e.get("output_path") != entry["output_path"]]
    entries.append(entry)
    data["entries"] = entries

    if final_delivery:
        data["final_delivery"] = {
            "output_path": entry["output_path"],
            "output_sha256": entry["output_sha256"],
            "baseline_path": entry["baseline_path"],
            "baseline_sha256": entry["baseline_sha256"],
            "profile_path": entry["profile_path"],
            "recorded_at": entry["recorded_at"],
        }

    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def load_model_lineage(lineage_path: str | Path) -> dict[str, Any]:
    """Load ``model_lineage.json`` and validate presence of ``final_delivery``."""
    lp = Path(lineage_path)
    data = json.loads(lp.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid model_lineage.json: {lp}")
    return data


def resolve_lineage_entry(
    lineage_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Resolve the active lineage entry (matching ``output_path`` or ``final_delivery``)."""
    data = load_model_lineage(lineage_path)
    entries = data.get("entries") or []
    if output_path is not None:
        target = str(output_path)
        target_name = Path(output_path).name
        for e in reversed(entries):
            if e.get("output_path") == target or Path(e.get("output_path", "")).name == target_name:
                return e
    fd = data.get("final_delivery")
    if isinstance(fd, dict) and fd.get("output_path"):
        for e in reversed(entries):
            if e.get("output_path") == fd.get("output_path"):
                return e
        return fd
    return entries[-1] if entries else None


# ---------------------------------------------------------------------------
# 9. CLI Entrypoint
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dalton financial model style profiler, safe XML patcher & lineage utility."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prof = sub.add_parser("profile", help="Extract style profile JSON from an existing workbook")
    p_prof.add_argument("workbook", help="Path to source .xlsx workbook (read-only)")
    p_prof.add_argument("--out", help="Optional output JSON path")
    p_prof.add_argument("--fiscal-year-end-month", type=int, default=None)

    p_clean = sub.add_parser(
        "clean-external-links",
        help="Remove all 4 OOXML external-link layers into a new output copy (baseline stays read-only)",
    )
    p_clean.add_argument("workbook", help="Path to source .xlsx workbook")
    p_clean.add_argument("--out", required=True, help="Output .xlsx path")

    p_diff = sub.add_parser("part-diff", help="Compare package parts / extLst / media between two workbooks")
    p_diff.add_argument("baseline", help="Baseline .xlsx path")
    p_diff.add_argument("new", help="New .xlsx path")

    p_lin = sub.add_parser("lineage", help="Record or update final delivery entry in model_lineage.json")
    p_lin.add_argument("--file", required=True, help="Path to model_lineage.json")
    p_lin.add_argument("--output", required=True, help="Delivered output .xlsx path")
    p_lin.add_argument("--baseline", default=None, help="Actual input baseline .xlsx path (omit for new models)")
    p_lin.add_argument("--profile", default=None, help="Style profile JSON path")
    p_lin.add_argument("--fiscal-year-end-month", type=int, default=None)
    p_lin.add_argument("--draft", action="store_true", help="Record as intermediate draft without setting final_delivery")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.cmd == "profile":
        prof = profile_workbook_style(args.workbook, fiscal_year_end_month=args.fiscal_year_end_month)
        text = json.dumps(prof, indent=2, ensure_ascii=False)
        if args.out:
            out_p = Path(args.out)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(text + "\n", encoding="utf-8")
            print(json.dumps({
                "status": "success",
                "out": str(out_p),
                "sheet_count": prof["sheet_count"],
                "extlst_parts": len(prof["package_info"]["extlst_parts"]),
                "external_link_parts": len(prof["package_info"]["external_link_parts"]),
            }, ensure_ascii=False))
        else:
            print(text)
        return 0

    if args.cmd == "clean-external-links":
        res = patch_workbook_xml(
            args.workbook,
            args.out,
            cell_updates=None,
            remove_external_links=True,
            disable_iterative_calc=True,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "part-diff":
        res = diff_package_parts(args.baseline, args.new)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "lineage":
        res = record_model_lineage(
            args.file,
            output_path=args.output,
            baseline_path=args.baseline,
            profile_path=args.profile,
            fiscal_year_end_month=args.fiscal_year_end_month,
            final_delivery=not args.draft,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
