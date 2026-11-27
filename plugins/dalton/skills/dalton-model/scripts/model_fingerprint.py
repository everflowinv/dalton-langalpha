#!/usr/bin/env python3
"""Read-only baseline workbook fingerprinting and semantic line-item inference (modeling v2 §4.2, §8.3, §8.4).

Usage:
    python3 model_fingerprint.py <workbook.xlsx> --out <fingerprint.json> [--ticker JD]

Standalone script (stdlib + openpyxl + lxml only). Never mutates the input workbook.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import posixpath
import re
import sys
from typing import Any
import zipfile

from lxml import etree
import openpyxl
from openpyxl.formula.tokenizer import Token, Tokenizer
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.worksheet.formula import ArrayFormula

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import model_style  # noqa: E402

_SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_X14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_XML = etree.XMLParser(resolve_entities=False, no_network=True)

_EXCEL_STR_RE = re.compile(r'"(?:""|[^"])*"')
_PLUGIN_FUNC_RE = re.compile(
    r"(?:_xll\.ciqfunctions|\bCIQ\s*\(|\bBQL\s*\(|\bBDH\s*\(|\bBDP\s*\(|\bRTD\s*\(|\bWEBSERVICE\s*\()",
    re.IGNORECASE,
)
_DYNAMIC_FUNC_RE = re.compile(r"\b(?:INDIRECT|OFFSET|ADDRESS|EVALUATE)\s*\(", re.IGNORECASE)
_CELL_REF_RE = re.compile(
    r"^(?:(?:'((?:[^']|'')+)'|([^'!\[\]]+))!)?\$?([A-Za-z]{1,3})\$?(\d+)$"
)
_RANGE_REF_RE = re.compile(
    r"^(?:(?:'((?:[^']|'')+)'|([^'!\[\]]+))!)?\$?([A-Za-z]{1,3})\$?(\d+):\$?([A-Za-z]{1,3})\$?(\d+)$"
)
_COL_RANGE_RE = re.compile(
    r"^(?:(?:'((?:[^']|'')+)'|([^'!\[\]]+))!)?\$?([A-Za-z]{1,3}):\$?([A-Za-z]{1,3})$"
)
_ROW_RANGE_RE = re.compile(
    r"^(?:(?:'((?:[^']|'')+)'|([^'!\[\]]+))!)?\$?(\d+):\$?(\d+)$"
)
_STRUCT_REF_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_.]*)\s*\[((?:[^\[\]]|\[[^\[\]]*\])*)\]"
)
_NAME_TOKEN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]{1,})\b")
_EXCEL_BUILTIN_FUNCS = frozenset({
    "SUM", "AVERAGE", "AVG", "MIN", "MAX", "IF", "IFERROR", "IFNA", "AND", "OR", "NOT",
    "ABS", "ROUND", "ROUNDUP", "ROUNDDOWN", "INT", "MOD", "SQRT", "POWER", "LN", "LOG",
    "EXP", "COUNT", "COUNTA", "COUNTIF", "SUMIF", "SUMIFS", "SUBTOTAL", "INDEX", "MATCH",
    "VLOOKUP", "HLOOKUP", "XLOOKUP", "OFFSET", "INDIRECT", "CHOOSE", "NPV", "IRR", "XIRR",
    "TRUE", "FALSE", "NA", "ISNA", "ISERROR", "ISBLANK", "ISNUMBER", "TEXT", "VALUE",
    "LEFT", "RIGHT", "MID", "LEN", "CONCAT", "CONCATENATE", "TODAY", "NOW", "YEAR", "MONTH",
})

_CAGR_DELTA_RE = re.compile(
    r"(?:\bCAGR\b|\bYoY\b|\bQoQ\b|\bMoM\b|\bvs\.?\b|\bChg\b|\bDiff\b|Δ|同比|环比)",
    re.IGNORECASE,
)
_Q_FIRST_RE = re.compile(r"^\s*Q?([1-4])Q\s*['\-/\s]?(\d{2}|\d{4})\s*([AEFP])?\s*$", re.IGNORECASE)
_Q_PREFIX_RE = re.compile(r"^\s*Q([1-4])\s*['\-/\s]?(\d{2}|\d{4})\s*([AEFP])?\s*$", re.IGNORECASE)
_Y_FIRST_Q_RE = re.compile(r"^\s*(?:FY|CY)?\s*(\d{2}|\d{4})\s*['\-/\s]?Q([1-4])\s*([AEFP])?\s*$", re.IGNORECASE)
_ANNUAL_RE = re.compile(r"^\s*(?:FY|CY)?\s*'?((?:19|20)\d{2}|\d{2})\s*([AEFP])?\s*$", re.IGNORECASE)

_SINGLE_COMPANY_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+-model-v\d+\.xlsx$", re.IGNORECASE)
_TICKER_SUFFIX_RE = re.compile(r"\s*[-–—]\s*([A-Z0-9]{1,10})\s*$")
_SHARED_SHEET_KEYWORDS = ("readme", "changelog", "change log", "checks", "industry", "sector", "summary", "overview", "comp")
_CONSENSUS_SHEET_KEYWORDS = ("vs consensus", "street vs house", "consensus")

_MONTH_MAP = {
    "jan": 1, "january": 1, "1月": 1,
    "feb": 2, "february": 2, "2月": 2,
    "mar": 3, "march": 3, "3月": 3,
    "apr": 4, "april": 4, "4月": 4,
    "may": 5, "5月": 5,
    "jun": 6, "june": 6, "6月": 6,
    "jul": 7, "july": 7, "7月": 7,
    "aug": 8, "august": 8, "8月": 8,
    "sep": 9, "sept": 9, "september": 9, "9月": 9,
    "oct": 10, "october": 10, "10月": 10,
    "nov": 11, "november": 11, "11月": 11,
    "dec": 12, "december": 12, "12月": 12,
}

# §8.4 subtotal / total / ratio keywords
_SUBTOTAL_KEYWORDS_RE = re.compile(
    r"(?:\btotal\b|\bsubtotal\b|合计|小计|总收入|\bgross\s+profit\b|毛利"
    r"|\boperating\s+(?:income|profit)\b|营业利润|\bebitda\b|\bnet\s+(?:income|profit)\b|净利润)",
    re.IGNORECASE,
)
_RATIO_FMT_RE = re.compile(r"(?:%|\"?\s*x\"?|\\x|\bbps?\b|\"?\s*pp\"?)", re.IGNORECASE)
_EPS_LABEL_RE = re.compile(r"(?:\beps\b|\bepads\b|每股收益|每份.*收益)", re.IGNORECASE)
_BEGINNING_LABEL_RE = re.compile(r"(?:\bbeginning\b|期初)", re.IGNORECASE)
_ENDING_LABEL_RE = re.compile(r"(?:\bending\b|期末|截止|\bshares\b|股本|股数)", re.IGNORECASE)
_AVG_LABEL_RE = re.compile(r"(?:\baverage\b|\bavg\b|日均|月均|均值|\bmau\b|\bdau\b)", re.IGNORECASE)
_DEDUP_KPI_RE = re.compile(r"(?:\bannual\s+active\b|\baac\b|去重|年度活跃)", re.IGNORECASE)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_safe_value(val: Any) -> Any:
    """Normalize openpyxl/Python values (dates, datetimes, Decimals, RGB objects) to JSON-safe scalars."""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, time):
        return val.isoformat()
    if isinstance(val, timedelta):
        return val.total_seconds()
    if isinstance(val, Decimal):
        return float(val)
    return str(val)


def _serialize_color(color: Any) -> str | None:
    """Normalize an openpyxl ``Color`` object (RGB, theme, indexed) into a JSON-safe string."""
    if color is None:
        return None
    ctype = getattr(color, "type", None)
    if ctype == "rgb":
        rgb = getattr(color, "rgb", None)
        if isinstance(rgb, str) and rgb and rgb != "00000000":
            return rgb
        return None
    if ctype == "theme":
        theme = getattr(color, "theme", None)
        if theme is not None:
            tint = getattr(color, "tint", 0.0) or 0.0
            if abs(float(tint)) > 1e-6:
                return f"theme:{int(theme)}:{round(float(tint), 4)}"
            return f"theme:{int(theme)}"
        return None
    if ctype == "indexed":
        idx = getattr(color, "indexed", None)
        if idx is not None:
            return f"indexed:{int(idx)}"
        return None
    rgb = getattr(color, "rgb", None)
    if isinstance(rgb, str) and rgb and rgb != "00000000":
        return rgb
    return None


def _expand_year(raw: str) -> int:
    v = int(raw)
    if len(raw) == 4:
        return v
    return 2000 + v if v <= 79 else 1900 + v


def parse_period_header(val: Any) -> dict[str, Any] | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        if float(val).is_integer() and 1990 <= int(val) <= 2100:
            yr = int(val)
            return {
                "raw": str(yr),
                "kind": "annual",
                "year": yr,
                "quarter": None,
                "is_forecast": False,
                "normalized": f"FY{yr}",
            }
        return None
    text = " ".join(str(val).strip().split())
    if not text or _CAGR_DELTA_RE.search(text):
        return None
    for rx in (_Q_FIRST_RE, _Q_PREFIX_RE):
        m = rx.match(text)
        if m:
            q = int(m.group(1))
            yr = _expand_year(m.group(2))
            suf = (m.group(3) or "").upper()
            return {
                "raw": text,
                "kind": "quarter",
                "year": yr,
                "quarter": q,
                "is_forecast": suf in ("E", "F", "P"),
                "normalized": f"{q}Q{yr % 100:02d}",
            }
    m = _Y_FIRST_Q_RE.match(text)
    if m:
        yr = _expand_year(m.group(1))
        q = int(m.group(2))
        suf = (m.group(3) or "").upper()
        return {
            "raw": text,
            "kind": "quarter",
            "year": yr,
            "quarter": q,
            "is_forecast": suf in ("E", "F", "P"),
            "normalized": f"{q}Q{yr % 100:02d}",
        }
    m = _ANNUAL_RE.match(text)
    if m:
        yr_s = m.group(1)
        suf = (m.group(2) or "").upper()
        if len(yr_s) == 2 and not (text.upper().startswith(("FY", "CY")) or suf):
            return None
        yr = _expand_year(yr_s)
        if 1990 <= yr <= 2100:
            return {
                "raw": text,
                "kind": "annual",
                "year": yr,
                "quarter": None,
                "is_forecast": suf in ("E", "F", "P"),
                "normalized": f"FY{yr}",
            }
    return None


def _extract_formula_text(val: Any) -> str | None:
    if isinstance(val, str) and val.startswith("="):
        return val
    if isinstance(val, ArrayFormula) and isinstance(val.text, str) and val.text.strip():
        t = val.text.strip()
        return t if t.startswith("=") else "=" + t
    return None


def _has_external_operand(expr: str) -> str | None:
    if not expr or "[" not in expr:
        return None
    try:
        tokens = Tokenizer(expr if expr.startswith("=") else "=" + expr).items
    except Exception:
        cleaned = _EXCEL_STR_RE.sub('""', expr)
        if "[" in cleaned and "!" in cleaned:
            return cleaned
        return None
    for tok in tokens:
        if tok.type == Token.OPERAND and tok.subtype == Token.RANGE and "!" in tok.value:
            if "[" in tok.value.rsplit("!", 1)[0]:
                return tok.value
    return None


def _has_plugin_call(expr: str) -> bool:
    cleaned = _EXCEL_STR_RE.sub('""', expr)
    return bool(_PLUGIN_FUNC_RE.search(cleaned))


def _has_dynamic_ref(expr: str) -> bool:
    cleaned = _EXCEL_STR_RE.sub('""', expr)
    return bool(_DYNAMIC_FUNC_RE.search(cleaned))


def _extract_tables_index(wb: openpyxl.Workbook) -> dict[str, dict[str, Any]]:
    """Collect Excel Table definitions across all worksheets for structured reference resolution."""
    tables: dict[str, dict[str, Any]] = {}
    for ws in wb.worksheets:
        ws_tables = getattr(ws, "tables", None)
        if not ws_tables:
            continue
        for tbl in ws_tables.values():
            tname = str(getattr(tbl, "name", None) or getattr(tbl, "displayName", None) or "").strip()
            ref = str(getattr(tbl, "ref", "") or "").strip()
            if not tname or not ref or ":" not in ref:
                continue
            m = _RANGE_REF_RE.match(ref)
            if not m:
                continue
            c1 = column_index_from_string(m.group(3).upper())
            r1 = int(m.group(4))
            c2 = column_index_from_string(m.group(5).upper())
            r2 = int(m.group(6))
            c1, c2 = min(c1, c2), max(c1, c2)
            r1, r2 = min(r1, r2), max(r1, r2)
            cols_map: dict[str, int] = {}
            tbl_cols = list(getattr(tbl, "tableColumns", None) or ())
            if tbl_cols:
                for offset, tc in enumerate(tbl_cols):
                    cname = str(getattr(tc, "name", "") or "").strip()
                    if cname and (c1 + offset) <= c2:
                        cols_map[cname.lower()] = c1 + offset
            for ci in range(c1, c2 + 1):
                hv = ws.cell(r1, ci).value
                if isinstance(hv, str) and hv.strip():
                    cols_map.setdefault(hv.strip().lower(), ci)
            tables[tname.lower()] = {
                "name": tname,
                "sheet": ws.title,
                "ref": ref,
                "r1": r1,
                "c1": c1,
                "r2": r2,
                "c2": c2,
                "columns": cols_map,
            }
    return tables


def _resolve_structured_refs(
    expr: str,
    tables: dict[str, dict[str, Any]] | None,
) -> list[tuple[str, int, int, int, int]]:
    """Resolve Excel structured table references like ``Tbl[Consensus]`` into ``(sheet, r1, c1, r2, c2)`` rects."""
    if not expr or not tables or "[" not in expr:
        return []
    cleaned = _EXCEL_STR_RE.sub('""', expr)
    rects: list[tuple[str, int, int, int, int]] = []
    for m in _STRUCT_REF_RE.finditer(cleaned):
        tname = m.group(1).strip().lower()
        spec = m.group(2).strip()
        tbl = tables.get(tname)
        if not tbl:
            continue
        sh = tbl["sheet"]
        r1, c1, r2, c2 = int(tbl["r1"]), int(tbl["c1"]), int(tbl["r2"]), int(tbl["c2"])
        cols_map: dict[str, int] = tbl.get("columns") or {}
        if not spec or spec.lower() in ("#all", "#data", "#headers", "#totals"):
            rects.append((sh, r1, c1, r2, c2))
            continue
        # Extract bracketed or bare column names inside structured specifier
        inner_tokens = re.findall(r"\[([^\[\]]+)\]", spec)
        col_candidates = [
            tok.lstrip("@").strip().lower()
            for tok in (inner_tokens or [spec.lstrip("@").strip()])
            if tok and not tok.strip().startswith("#")
        ]
        matched_cols = [cols_map[cn] for cn in col_candidates if cn in cols_map]
        if matched_cols:
            rects.append((sh, r1, min(matched_cols), r2, max(matched_cols)))
        else:
            # Conservative fallback: treat unknown column selector on the table as spanning the whole table
            rects.append((sh, r1, c1, r2, c2))
    return rects


def extract_formula_targets(
    expr: str,
    current_sheet: str,
    *,
    tables: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[tuple[str, str]], list[tuple[str, int, int, int, int]]]:
    """Return ``(cell_refs, range_rects)`` for ``expr``, including >256-cell ranges & structured table refs."""
    refs: list[tuple[str, str]] = []
    rects: list[tuple[str, int, int, int, int]] = []
    if not expr:
        return refs, rects

    for rect in _resolve_structured_refs(expr, tables):
        rects.append(rect)
        sh, r1, c1, r2, c2 = rect
        if (c2 - c1 + 1) * (r2 - r1 + 1) <= 256:
            for rr in range(r1, r2 + 1):
                for cc in range(c1, c2 + 1):
                    refs.append((sh, f"{get_column_letter(cc)}{rr}"))

    try:
        tokens = Tokenizer(expr if expr.startswith("=") else "=" + expr).items
    except Exception:
        return refs, rects

    for tok in tokens:
        if tok.type != Token.OPERAND or tok.subtype != Token.RANGE:
            continue
        raw = tok.value.strip()
        if "[" in raw:
            # External link or structured reference (structured refs handled above)
            continue
        m_rng = _RANGE_REF_RE.match(raw)
        if m_rng:
            sh = (
                m_rng.group(1).replace("''", "'")
                if m_rng.group(1)
                else (m_rng.group(2) or current_sheet)
            )
            c1 = column_index_from_string(m_rng.group(3).upper())
            r1 = int(m_rng.group(4))
            c2 = column_index_from_string(m_rng.group(5).upper())
            r2 = int(m_rng.group(6))
            c1, c2 = min(c1, c2), max(c1, c2)
            r1, r2 = min(r1, r2), max(r1, r2)
            rects.append((sh, r1, c1, r2, c2))
            if (c2 - c1 + 1) * (r2 - r1 + 1) <= 256:
                for rr in range(r1, r2 + 1):
                    for cc in range(c1, c2 + 1):
                        refs.append((sh, f"{get_column_letter(cc)}{rr}"))
            continue
        m_col = _COL_RANGE_RE.match(raw)
        if m_col:
            sh = (
                m_col.group(1).replace("''", "'")
                if m_col.group(1)
                else (m_col.group(2) or current_sheet)
            )
            c1 = column_index_from_string(m_col.group(3).upper())
            c2 = column_index_from_string(m_col.group(4).upper())
            rects.append((sh, 1, min(c1, c2), 1048576, max(c1, c2)))
            continue
        m_row = _ROW_RANGE_RE.match(raw)
        if m_row:
            sh = (
                m_row.group(1).replace("''", "'")
                if m_row.group(1)
                else (m_row.group(2) or current_sheet)
            )
            r1, r2 = int(m_row.group(3)), int(m_row.group(4))
            rects.append((sh, min(r1, r2), 1, max(r1, r2), 16384))
            continue
        m_cell = _CELL_REF_RE.match(raw)
        if m_cell:
            sh = (
                m_cell.group(1).replace("''", "'")
                if m_cell.group(1)
                else (m_cell.group(2) or current_sheet)
            )
            col_u = m_cell.group(3).upper()
            row_i = int(m_cell.group(4))
            refs.append((sh, f"{col_u}{row_i}"))
    return refs, rects


def extract_formula_refs(expr: str, current_sheet: str) -> list[tuple[str, str]]:
    """Return list of ``(sheet_name, COORD)`` cell references inside ``expr``."""
    refs, _ = extract_formula_targets(expr, current_sheet)
    return refs


def _extract_name_tokens(expr: str) -> list[str]:
    """Return candidate defined-name identifiers referenced by ``expr``."""
    tokens_to_check: list[str] = []
    if not expr:
        return tokens_to_check
    try:
        for tok in Tokenizer(expr if expr.startswith("=") else "=" + expr).items:
            if tok.type == Token.OPERAND and tok.subtype == Token.RANGE:
                val = tok.value.strip()
                if "!" not in val and ":" not in val and "[" not in val:
                    tokens_to_check.append(val)
    except Exception:
        pass
    cleaned = _EXCEL_STR_RE.sub('""', expr)
    for m in _NAME_TOKEN_RE.finditer(cleaned):
        tok_s = m.group(1)
        if tok_s.upper() not in _EXCEL_BUILTIN_FUNCS:
            tokens_to_check.append(tok_s)
    return tokens_to_check


def expand_defined_names_in_expr(
    expr: str,
    current_sheet: str,
    defined_names: list[dict[str, str]],
    *,
    tables: dict[str, dict[str, Any]] | None = None,
) -> tuple[set[tuple[str, str]], list[tuple[str, int, int, int, int]], str | None, bool]:
    """Expand defined names recursively; return ``(cell_refs, range_rects, ext_ref_or_none, has_dynamic)``."""
    wb_names: dict[str, str] = {}
    sheet_names: dict[tuple[str, str], str] = {}
    for dn in defined_names:
        n_low = str(dn.get("name") or "").strip().lower()
        scope = str(dn.get("scope") or "workbook")
        d_expr = str(dn.get("expression") or "")
        if not n_low or not d_expr:
            continue
        if scope == "workbook":
            wb_names[n_low] = d_expr
        else:
            sheet_names[(scope, n_low)] = d_expr

    refs: set[tuple[str, str]] = set()
    rects: list[tuple[str, int, int, int, int]] = []
    visited: set[tuple[str, str]] = set()
    ext_hit: str | None = None
    has_dyn = _has_dynamic_ref(expr)

    def _walk(cur_expr: str, cur_sh: str) -> None:
        nonlocal ext_hit, has_dyn
        if _has_dynamic_ref(cur_expr):
            has_dyn = True
        if ext_hit is None:
            ext_op = _has_external_operand(cur_expr)
            if ext_op:
                ext_hit = ext_op
        c_refs, c_rects = extract_formula_targets(cur_expr, cur_sh, tables=tables)
        for rsh, rcoord in c_refs:
            refs.add((rsh, rcoord.upper()))
        rects.extend(c_rects)
        for tok_s in _extract_name_tokens(cur_expr):
            key_low = tok_s.lower()
            target_expr = sheet_names.get((cur_sh, key_low)) or wb_names.get(key_low)
            if target_expr is not None:
                vkey = (cur_sh, key_low)
                if vkey not in visited:
                    visited.add(vkey)
                    _walk(target_expr, cur_sh)

    _walk(expr, current_sheet)
    return refs, rects, ext_hit, has_dyn


def _tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return all strongly connected components representing circular references."""
    index = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    sccs: list[list[str]] = []

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 5000))

    def strongconnect(v: str) -> None:
        nonlocal index
        indices[v] = index
        lowlink[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(graph.get(v, ())):
            if w not in graph:
                continue
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or (len(comp) == 1 and comp[0] in graph.get(comp[0], ())):
                sccs.append(sorted(comp))

    for node in sorted(graph):
        if node not in indices:
            strongconnect(node)
    return sorted(sccs)


def _parse_unit_scale(text: str | None) -> float:
    if not text:
        return 1.0
    low = text.lower()
    if any(tok in low for tok in ("billion", " bn", "b)", "十亿")):
        return 1e9
    if any(tok in low for tok in ("mm", "million", "百万", " mn")):
        return 1e6
    if any(tok in low for tok in ("'000", "000)", "thousand", "千")):
        return 1e3
    return 1.0


def _parse_fy_end_month(ws: Any) -> tuple[int, bool]:
    for r in range(1, min(5, ws.max_row + 1)):
        for c in range(1, min(6, ws.max_column + 1)):
            v = ws.cell(r, c).value
            if not isinstance(v, str):
                continue
            m = re.search(r"fy\s*ends?\s+([A-Za-z]+|\d{1,2}月)", v, re.IGNORECASE)
            if m:
                key = m.group(1).lower()
                if key in _MONTH_MAP:
                    return _MONTH_MAP[key], True
            m2 = re.search(r"财年截止\s*(\d{1,2})\s*月", v)
            if m2:
                mo = int(m2.group(1))
                if 1 <= mo <= 12:
                    return mo, True
    return 12, False


def _detect_company_tags(wb: openpyxl.Workbook, requested_ticker: str | None) -> list[dict[str, Any]]:
    sheet_entries: list[dict[str, Any]] = []
    current_divider_tag: str | None = None
    distinct_tags: set[str] = set()

    for ws in wb.worksheets:
        name = ws.title
        low = name.lower().strip()
        vis = getattr(ws, "sheet_state", "visible") or "visible"
        tab_c = None
        if ws.sheet_properties and ws.sheet_properties.tabColor:
            tab_c = _serialize_color(ws.sheet_properties.tabColor)

        tag: str | None = None
        if name.endswith(">>"):
            tag = name[:-2].strip().upper() or None
            current_divider_tag = tag
        else:
            m = _TICKER_SUFFIX_RE.search(name)
            if m:
                tag = m.group(1).upper()
            elif any(k in low for k in _SHARED_SHEET_KEYWORDS):
                tag = None
            elif any(k in low for k in _CONSENSUS_SHEET_KEYWORDS):
                tag = requested_ticker.upper() if requested_ticker else current_divider_tag
            elif current_divider_tag is not None:
                tag = current_divider_tag
            elif requested_ticker:
                tag = requested_ticker.upper()

        if tag:
            distinct_tags.add(tag)
        sheet_entries.append(
            {
                "name": name,
                "visibility": vis,
                "tab_color": tab_c or None,
                "company_tag": tag,
            }
        )
    return sheet_entries


def _read_xml_metadata(xlsx_path: Path) -> dict[str, Any]:
    """Read calcPr, pane splits, col widths/outlines, row outlines, extLst/x14 CF, and raw XML caches."""
    out: dict[str, Any] = {
        "calc_properties": {
            "full_calc_on_load": False,
            "calc_mode": "auto",
            "iterate": False,
            "iterate_count": None,
            "iterate_delta": None,
        },
        "external_link_parts": [],
        "sheets": {},
    }
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        names = set(zf.namelist())
        out["external_link_parts"] = sorted(
            n for n in names if n.startswith("xl/externalLinks/") and n.endswith(".xml")
        )
        if "xl/workbook.xml" not in names or "xl/_rels/workbook.xml.rels" not in names:
            return out

        wb_root = etree.fromstring(zf.read("xl/workbook.xml"), _XML)
        calc = wb_root.find(f"{{{_SML}}}calcPr")
        if calc is not None:
            out["calc_properties"] = {
                "full_calc_on_load": (calc.get("fullCalcOnLoad") or "").strip().lower() in ("1", "true"),
                "calc_mode": calc.get("calcMode") or "auto",
                "iterate": (calc.get("iterate") or "").strip().lower() in ("1", "true"),
                "iterate_count": int(calc.get("iterateCount")) if (calc.get("iterateCount") or "").isdigit() else None,
                "iterate_delta": float(calc.get("iterateDelta")) if calc.get("iterateDelta") else None,
            }

        sst: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sst_root = etree.fromstring(zf.read("xl/sharedStrings.xml"), _XML)
            sst = ["".join(t.text or "" for t in si.iter(f"{{{_SML}}}t")) for si in sst_root.findall(f"{{{_SML}}}si")]

        rels = etree.fromstring(zf.read("xl/_rels/workbook.xml.rels"), _XML)
        targets = {r.get("Id"): r.get("Target", "") for r in rels}
        for sh in wb_root.iter(f"{{{_SML}}}sheet"):
            sname = sh.get("name") or ""
            tgt = targets.get(sh.get(_REL_ID), "")
            part = tgt.lstrip("/") if tgt.startswith("/") else posixpath.normpath(posixpath.join("xl", tgt))
            if part not in names:
                continue
            sroot = etree.fromstring(zf.read(part), _XML)

            pane = sroot.find(f".//{{{_SML}}}sheetViews/{{{_SML}}}sheetView/{{{_SML}}}pane")
            if pane is not None:
                freeze = {
                    "x_split": int(float(pane.get("xSplit") or 0)),
                    "y_split": int(float(pane.get("ySplit") or 0)),
                    "top_left_cell": pane.get("topLeftCell"),
                    "state": pane.get("state"),
                }
            else:
                freeze = {"x_split": 0, "y_split": 0, "top_left_cell": None, "state": None}

            col_layout: dict[str, dict[str, Any]] = {}
            cols_el = sroot.find(f"{{{_SML}}}cols")
            if cols_el is not None:
                for col_el in cols_el.findall(f"{{{_SML}}}col"):
                    c_min = int(col_el.get("min") or 1)
                    c_max = int(col_el.get("max") or c_min)
                    width = float(col_el.get("width")) if col_el.get("width") else None
                    hidden = (col_el.get("hidden") or "").lower() in ("1", "true")
                    outline = int(col_el.get("outlineLevel") or 0)
                    for ci in range(c_min, min(c_max, c_min + 200) + 1):
                        col_layout[get_column_letter(ci)] = {
                            "width": width,
                            "hidden": hidden,
                            "outline_level": outline,
                        }

            row_groups: dict[str, dict[str, Any]] = {}
            raw_cache: dict[str, tuple[str | None, Any]] = {}
            sheet_data = sroot.find(f"{{{_SML}}}sheetData")
            if sheet_data is not None:
                for row_el in sheet_data.findall(f"{{{_SML}}}row"):
                    r_str = row_el.get("r")
                    if r_str:
                        ol = int(row_el.get("outlineLevel") or 0)
                        hid = (row_el.get("hidden") or "").lower() in ("1", "true")
                        if ol > 0 or hid:
                            row_groups[r_str] = {"outline_level": ol, "hidden": hid}
                    for c_el in row_el.findall(f"{{{_SML}}}c"):
                        ref = (c_el.get("r") or "").upper()
                        if not ref:
                            continue
                        t = c_el.get("t")
                        v_el = c_el.find(f"{{{_SML}}}v")
                        if t == "inlineStr":
                            is_el = c_el.find(f"{{{_SML}}}is")
                            txt = "".join(x.text or "" for x in is_el.iter(f"{{{_SML}}}t")) if is_el is not None else ""
                            raw_cache[ref] = ("str", txt)
                        elif v_el is not None:
                            raw_t = "" if (v_el.text is None and t == "str") else v_el.text
                            if raw_t is None:
                                raw_cache[ref] = (t, None)
                            elif t == "s":
                                idx = int(raw_t) if raw_t.isdigit() else -1
                                raw_cache[ref] = ("s", sst[idx] if 0 <= idx < len(sst) else "")
                            elif t in ("str", "e"):
                                raw_cache[ref] = (t, raw_t)
                            elif t == "b":
                                raw_cache[ref] = ("b", raw_t == "1")
                            else:
                                try:
                                    num = float(raw_t)
                                    raw_cache[ref] = (t, int(num) if num.is_integer() and abs(num) < 1e15 else num)
                                except ValueError:
                                    raw_cache[ref] = (t, raw_t)

            std_cf: list[dict[str, Any]] = []
            for cf_el in sroot.findall(f"{{{_SML}}}conditionalFormatting"):
                std_cf.append(
                    {
                        "sqref": cf_el.get("sqref") or "",
                        "rule_count": len(cf_el.findall(f"{{{_SML}}}cfRule")),
                    }
                )
            x14_cf: list[dict[str, Any]] = []
            for cf_el in sroot.iter(f"{{{_X14}}}conditionalFormatting"):
                sq_el = cf_el.find(f"{{http://schemas.microsoft.com/office/excel/2006/main}}sqref")
                sqref = (sq_el.text or "").strip() if sq_el is not None else ""
                x14_cf.append(
                    {
                        "sqref": sqref,
                        "rule_count": len(cf_el.findall(f"{{{_X14}}}cfRule")),
                    }
                )

            out["sheets"][sname] = {
                "freeze_panes": freeze,
                "column_layout": col_layout,
                "row_groups": row_groups,
                "ext_cf_summary": {"standard_cf": std_cf, "x14_cf": x14_cf},
                "raw_cache": raw_cache,
            }
    return out


def is_subtotal_or_ratio_row(
    label: str,
    num_fmt: str | None = None,
    sample_formulas: list[str] | None = None,
    has_summary_border: bool = False,
) -> bool:
    """§8.4 predicate identifying subtotal, total, or ratio rows."""
    if label and _SUBTOTAL_KEYWORDS_RE.search(label):
        return True
    if num_fmt and _RATIO_FMT_RE.search(num_fmt):
        return True
    if has_summary_border:
        return True
    for expr in sample_formulas or ():
        up = expr.upper()
        if any(fn in up for fn in ("SUM(", "SUBTOTAL(")):
            # Vertical same-column aggregation (e.g. =SUM(G4:G8)) or horizontal multi-item
            if re.search(r"(?:SUM|SUBTOTAL)\s*\(\s*\$?([A-Z]{1,3})\$?\d+\s*:\s*\$?\1\$?\d+\s*\)", up):
                return True
        # Same-column addition/subtraction of multiple rows (=G4+G5 or =G6-G8)
        same_col_terms = re.findall(r"\$?([A-Z]{1,3})\$?(\d+)", up)
        if len(same_col_terms) >= 2:
            cols = {c for c, _ in same_col_terms}
            rows = {r for _, r in same_col_terms}
            if len(cols) == 1 and len(rows) >= 2 and any(op in up for op in ("+", "-", "/")):
                return True
        # YoY / ratio division across columns (=G6/F6-1)
        if "/" in up and len(same_col_terms) >= 2:
            return True
    return False


def _detect_annual_quarter_map(columns: dict[str, dict[str, Any]], fy_end_month: int) -> dict[str, list[str]]:
    """Map annual column letters to their 4 fiscal-quarter column letters."""
    q_by_year: dict[int, dict[int, str]] = defaultdict(dict)
    for col_l, meta in columns.items():
        if meta.get("kind") == "quarter" and meta.get("year") and meta.get("quarter"):
            q_by_year[int(meta["year"])][int(meta["quarter"])] = col_l

    mapping: dict[str, list[str]] = {}
    for col_l, meta in columns.items():
        if meta.get("kind") != "annual" or not meta.get("year"):
            continue
        yr = int(meta["year"])
        qs = q_by_year.get(yr, {})
        if all(q in qs for q in (1, 2, 3, 4)):
            mapping[col_l] = [qs[1], qs[2], qs[3], qs[4]]
    return mapping


def _infer_formula_annual_hint(
    expr: str | None,
    q_cols: list[str],
    row: int,
    ann_col: str,
) -> str | None:
    """Priority-2 annual formula shape hint (flow / ending_balance / average / ratio)."""
    if not expr or not expr.startswith("="):
        return None
    up = expr.upper().replace("$", "").replace(" ", "")
    if len(q_cols) == 4:
        q1, q2, q3, q4 = [q.upper() for q in q_cols]
        if f"SUM({q1}{row}:{q4}{row})" in up or all(f"{q}{row}" in up for q in (q1, q2, q3, q4)) and "+" in up:
            return "flow"
        if f"AVERAGE({q1}{row}:{q4}{row})" in up or f"AVG({q1}{row}:{q4}{row})" in up:
            return "average"
        if up == f"={q4}{row}":
            return "ending_balance"
    if f"{ann_col.upper()}" in up and "/" in up:
        return "ratio"
    return None


def _row_label_text(ws: Any, row: int) -> str:
    parts: list[str] = []
    for c in range(1, 5):
        v = ws.cell(row, c).value
        if isinstance(v, str) and v.strip() and not v.strip().startswith("="):
            parts.append(v.strip())
    return " | ".join(parts)


def _classify_section(label: str) -> str | None:
    low = label.lower()
    if any(k in low for k in ("income statement", "p&l", "利润表", "损益表")):
        return "income_statement"
    if any(k in low for k in ("balance sheet", "资产负债表")):
        return "balance_sheet"
    if any(k in low for k in ("cash flow", "现金流量表")):
        return "cash_flow"
    if any(k in low for k in ("segment", "分部", "non-gaap")):
        return "segment_reporting"
    if any(k in low for k in ("driver", "kpi", "operating", "业务", "指标", "运营")):
        return "operating_kpi"
    if any(k in low for k in ("guidance", "指引")):
        return "guidance"
    if any(k in low for k in ("consensus", "一致预期")):
        return "consensus"
    return None


def _detect_consensus_snapshot_cells(ws: Any, sheet_name: str, cells_data: dict[str, Any]) -> list[str]:
    """Identify isolated consensus snapshot cells on consensus sheets or labeled blocks."""
    snapshot_cells: list[str] = []
    low_name = sheet_name.lower()
    if any(k in low_name for k in _CONSENSUS_SHEET_KEYWORDS):
        # Scan all rows so both primary tables (rows 1..5) and appended isolated consensus blocks are detected
        active_snap_cols: set[int] = set()
        for r in range(1, ws.max_row + 1):
            row_header_hits: set[int] = set()
            row_has_any_header_word = False
            row_has_data_or_formula = False
            for c in range(1, ws.max_column + 1):
                v = ws.cell(r, c).value
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    row_has_data_or_formula = True
                    continue
                if isinstance(v, str):
                    vs = v.strip()
                    if vs.startswith("="):
                        row_has_data_or_formula = True
                        continue
                    vl = vs.lower()
                    if any(k in vl for k in ("period", "metric", "house", "diff", "期间", "指标", "本方", "差异")):
                        row_has_any_header_word = True
                    if any(k in vl for k in ("consensus", "外部预期", "一致预期", "street")):
                        if not any(ex in vl for ex in ("vs", "diff", "差异", "house", "我们", "rationale", "理由", "isolated reference zone")):
                            row_header_hits.add(c)
            if row_header_hits and not row_has_data_or_formula and (row_has_any_header_word or r <= 2):
                active_snap_cols = row_header_hits
                continue
            if active_snap_cols:
                for c in active_snap_cols:
                    coord = f"{get_column_letter(c)}{r}"
                    val = ws.cell(r, c).value
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        snapshot_cells.append(coord)
        return sorted(set(snapshot_cells))

    # Also support an isolated consensus reference block below a summary/model table
    in_consensus_block = False
    for r in range(1, ws.max_row + 1):
        lbl = _row_label_text(ws, r)
        low_lbl = lbl.lower()
        if any(k in low_lbl for k in ("consensus snapshot", "一致预期快照", "consensus reference")):
            in_consensus_block = True
            continue
        if in_consensus_block and lbl == "":
            in_consensus_block = False
            continue
        if in_consensus_block:
            for c in range(5, ws.max_column + 1):
                v = ws.cell(r, c).value
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    snapshot_cells.append(f"{get_column_letter(c)}{r}")
    return sorted(set(snapshot_cells))


def build_fingerprint(
    workbook_path: str | Path,
    *,
    ticker: str | None = None,
) -> dict[str, Any]:
    """Extract full v2 baseline fingerprint dictionary from ``workbook_path`` (read-only)."""
    path = Path(workbook_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"workbook not found: {path}")

    xml_meta = _read_xml_metadata(path)
    palette = model_style.extract_theme_palette(path)
    wb = openpyxl.load_workbook(path, data_only=False)

    try:
        sheet_order = _detect_company_tags(wb, ticker)
        distinct_company_tags = {
            s["company_tag"] for s in sheet_order if s.get("company_tag")
        }
        is_single_name = bool(_SINGLE_COMPANY_NAME_RE.match(path.name))
        naming_mode = (
            "sector_workbook"
            if (len(distinct_company_tags) > 1 or (not is_single_name and any(s["name"].endswith(">>") for s in sheet_order)))
            else "single_company"
        )

        # Defined names (workbook-scoped + sheet-scoped) and Excel tables
        tables_index = _extract_tables_index(wb)
        defined_names: list[dict[str, str]] = []
        ext_names: list[dict[str, str]] = []
        for name, dn in wb.defined_names.items():
            expr = str(dn.attr_text or "")
            defined_names.append({"name": name, "scope": "workbook", "expression": expr})
        for ws in wb.worksheets:
            for name, dn in getattr(ws, "defined_names", {}).items():
                expr = str(dn.attr_text or "")
                defined_names.append({"name": name, "scope": ws.title, "expression": expr})
        for dn_item in defined_names:
            scope_sh = "" if dn_item["scope"] == "workbook" else dn_item["scope"]
            _, _, dn_ext, _ = expand_defined_names_in_expr(
                dn_item["expression"], scope_sh, defined_names, tables=tables_index
            )
            if dn_ext:
                ext_names.append(dn_item)

        ext_cells: list[dict[str, str]] = []
        dep_graph: dict[str, set[str]] = {}
        sheets_fp: dict[str, Any] = {}
        sheet_tag_map = {s["name"]: s["company_tag"] for s in sheet_order}
        normalized_styles: dict[str, Any] = {}
        baseline_findings: list[dict[str, Any]] = []
        health_warnings: list[str] = []

        for ws in wb.worksheets:
            sname = ws.title
            s_xml = xml_meta["sheets"].get(sname, {})
            raw_cache = s_xml.get("raw_cache", {})
            fy_end_month, fy_explicit = _parse_fy_end_month(ws)
            a1_val = ws["A1"].value
            unit_text = str(a1_val).strip() if isinstance(a1_val, str) and a1_val.strip() else None
            unit_scale = _parse_unit_scale(unit_text)

            # Detect header row & period columns
            header_row: int | None = None
            best_hits = 0
            for r in range(1, min(6, ws.max_row + 1)):
                hits = sum(
                    1
                    for c in range(1, ws.max_column + 1)
                    if parse_period_header(ws.cell(r, c).value) is not None
                )
                if hits > best_hits:
                    best_hits = hits
                    header_row = r

            columns_meta: dict[str, dict[str, Any]] = {}
            source_note_col: int | None = None
            first_forecast_col: int | None = None
            if header_row is not None:
                for c in range(1, ws.max_column + 1):
                    col_l = get_column_letter(c)
                    hv = ws.cell(header_row, c).value
                    parsed = parse_period_header(hv)
                    if parsed is not None:
                        columns_meta[col_l] = {"col_index": c, **parsed}
                        if parsed["is_forecast"] and first_forecast_col is None:
                            first_forecast_col = c
                    elif isinstance(hv, str) and any(
                        k in hv.lower() for k in ("note", "source", "备注", "来源", "rationale")
                    ):
                        columns_meta[col_l] = {"col_index": c, "raw": hv.strip(), "kind": "notes"}
                        source_note_col = c
                    elif isinstance(hv, str) and _CAGR_DELTA_RE.search(hv):
                        columns_meta[col_l] = {"col_index": c, "raw": hv.strip(), "kind": "cagr"}
            if source_note_col is None:
                # Fallback: rightmost column containing red text starting with '<'
                for r, c, cell in model_style._iter_stored_cells(ws):
                    if isinstance(cell.value, str) and cell.value.strip().startswith("<"):
                        if source_note_col is None or c > source_note_col:
                            source_note_col = c

            annual_sum_quarters = _detect_annual_quarter_map(columns_meta, fy_end_month)
            consensus_snap_set = set(_detect_consensus_snapshot_cells(ws, sname, {}))

            # Walk rows for sections, anchors, line_item_map, and cells
            current_section = "general"
            line_item_map: dict[str, dict[str, Any]] = {}
            cells_meta: dict[str, dict[str, Any]] = {}
            anchors: dict[str, Any] = {
                "total_revenue_row": None,
                "gross_profit_row": None,
                "operating_income_row": None,
                "net_income_row": None,
                "diluted_shares_row": None,
                "beginning_shares_row": None,
                "ending_shares_row": None,
                "share_change_rows": [],
                "eps_row": None,
                "segment_op_rows": [],
                "segment_unalloc_row": None,
                "segment_total_op_row": None,
                "segment_rev_rows": [],
                "segment_rev_elim_row": None,
                "beginning_cash_row": None,
                "net_change_cash_row": None,
                "ending_cash_row": None,
                "bs_cash_row": None,
                "total_assets_row": None,
                "total_liabilities_row": None,
                "total_equity_row": None,
                "total_liab_equity_row": None,
                "consensus_snapshot_cells": sorted(consensus_snap_set),
            }

            row_cells_grouped: dict[int, list[tuple[int, Any]]] = defaultdict(list)
            for r, c, cell in model_style._iter_stored_cells(ws):
                row_cells_grouped[r].append((c, cell))

            legacy_blue_annual_formulas = 0
            link_colors: set[str] = set()
            forecast_formula_cnt = 0
            forecast_amount_const_cnt = 0

            for r in sorted(row_cells_grouped):
                lbl = _row_label_text(ws, r)
                a_fill = model_style.get_cell_fill_rgb(ws.cell(r, 1), palette)
                sec_guess = _classify_section(lbl)
                if sec_guess and (
                    a_fill in (model_style.SEC_REPORT_FILL, model_style.SEC_SEGMENT_FILL, model_style.SEC_SUBMODEL_FILL)
                    or len([1 for c, cl in row_cells_grouped[r] if c >= 5 and cl.value is not None]) == 0
                ):
                    current_section = sec_guess

                # Gather row formulas & dominant number format for line_item_map and §8.4
                row_formulas: list[str] = []
                row_num_fmt: str | None = None
                has_summary_border = False
                for c, cell in row_cells_grouped[r]:
                    col_l = get_column_letter(c)
                    if col_l in columns_meta and columns_meta[col_l].get("kind") in ("annual", "quarter"):
                        f_txt = _extract_formula_text(cell.value)
                        if f_txt:
                            row_formulas.append(f_txt)
                        if row_num_fmt is None and cell.number_format and cell.number_format != "General":
                            row_num_fmt = str(cell.number_format)
                        bdr = getattr(cell, "border", None)
                        if bdr and getattr(bdr.top, "style", None) == "thin" and getattr(bdr.bottom, "style", None) == "double":
                            has_summary_border = True

                subtotal_or_ratio = is_subtotal_or_ratio_row(
                    lbl,
                    num_fmt=row_num_fmt,
                    sample_formulas=row_formulas,
                    has_summary_border=has_summary_border,
                )

                # Semantic anchors (exact/clean heuristics)
                low_lbl = lbl.lower()
                if r > (header_row or 1) and lbl:
                    if anchors["total_revenue_row"] is None and re.search(r"\btotal\s+revenue\b|总收入|营业总收入", low_lbl) and "yoy" not in low_lbl and "%" not in low_lbl:
                        anchors["total_revenue_row"] = r
                    if anchors["gross_profit_row"] is None and re.search(r"\bgross\s+profit\b|毛利", low_lbl) and "margin" not in low_lbl and "%" not in low_lbl:
                        anchors["gross_profit_row"] = r
                    if anchors["operating_income_row"] is None and re.search(r"^(?:.*\|\s*)?(?:operating\s+(?:income|profit)|营业利润)$", low_lbl):
                        anchors["operating_income_row"] = r
                    if anchors["net_income_row"] is None and re.search(r"^(?:.*\|\s*)?(?:net\s+(?:income|profit)|归母净利润|净利润)$", low_lbl):
                        anchors["net_income_row"] = r
                    if anchors["diluted_shares_row"] is None and re.search(r"(?:diluted\s+shares|稀释.*股数|加权平均稀释股数)", low_lbl):
                        anchors["diluted_shares_row"] = r
                    if anchors["beginning_shares_row"] is None and re.search(r"(?:beginning\s+(?:diluted\s+)?shares|期初.*股[数本])", low_lbl):
                        anchors["beginning_shares_row"] = r
                    if anchors["ending_shares_row"] is None and re.search(r"(?:ending\s+(?:diluted\s+)?shares|期末.*股[数本])", low_lbl):
                        anchors["ending_shares_row"] = r
                    if re.search(r"(?:share\s+(?:issuance|repurchase|buyback)|net\s+change\s+in\s+shares|sbc\s+dilution|股[数本].*(?:变动|增发|回购|稀释))", low_lbl):
                        anchors["share_change_rows"].append(r)
                    if anchors["eps_row"] is None and _EPS_LABEL_RE.search(low_lbl):
                        anchors["eps_row"] = r
                    if current_section == "segment_reporting":
                        if re.search(r"(?:total\s+segment\s+operating|分部营业利润合计|分部利润合计)", low_lbl):
                            anchors["segment_total_op_row"] = r
                        elif re.search(r"(?:unallocated|eliminat|总部|抵销)", low_lbl) and re.search(r"(?:revenue|收入)", low_lbl):
                            anchors["segment_rev_elim_row"] = r
                        elif re.search(r"(?:unallocated|eliminat|总部|抵销)", low_lbl):
                            anchors["segment_unalloc_row"] = r
                        elif re.search(r"(?:operating\s+(?:profit|income)|营业利润)", low_lbl):
                            anchors["segment_op_rows"].append(r)
                        elif re.search(r"(?:revenue|收入)", low_lbl) and not re.search(r"(?:\btotal\b|合计|yoy|%|margin|growth)", low_lbl):
                            anchors["segment_rev_rows"].append(r)
                    if current_section == "cash_flow":
                        if re.search(r"(?:beginning\s+cash|期初现金)", low_lbl):
                            anchors["beginning_cash_row"] = r
                        elif re.search(r"(?:net\s+change\s+in\s+cash|现金净变动|现金及等价物净增加)", low_lbl):
                            anchors["net_change_cash_row"] = r
                        elif re.search(r"(?:ending\s+cash|期末现金)", low_lbl):
                            anchors["ending_cash_row"] = r
                    if current_section == "balance_sheet":
                        if anchors["bs_cash_row"] is None and re.search(r"(?:cash\s+and|现金及|货币资金)", low_lbl):
                            anchors["bs_cash_row"] = r
                        if anchors["total_assets_row"] is None and re.search(r"(?:total\s+assets|资产总计|总资产)", low_lbl):
                            anchors["total_assets_row"] = r
                        if anchors["total_liabilities_row"] is None and re.search(r"^(?:.*\|\s*)?(?:total\s+liabilities|负债合计|总负债)$", low_lbl):
                            anchors["total_liabilities_row"] = r
                        if anchors["total_equity_row"] is None and re.search(r"^(?:.*\|\s*)?(?:total\s+(?:shareholders'?\s+)?equity|所有者权益合计|净资产|总权益)$", low_lbl):
                            anchors["total_equity_row"] = r
                        if anchors["total_liab_equity_row"] is None and re.search(r"(?:total\s+liabilities\s*(?:&|and)\s*equity|负债和所有者权益总计|负债及权益总计)", low_lbl):
                            anchors["total_liab_equity_row"] = r

                # Line item semantic-first inference (§4.2 & §8.3)
                has_period_data = any(
                    get_column_letter(c) in columns_meta
                    and columns_meta[get_column_letter(c)].get("kind") in ("annual", "quarter")
                    and cl.value is not None
                    for c, cl in row_cells_grouped[r]
                )
                if r > (header_row or 1) and lbl and has_period_data:
                    sem_type: str | None = None
                    if _EPS_LABEL_RE.search(lbl):
                        sem_type = "eps"
                    elif row_num_fmt and _RATIO_FMT_RE.search(row_num_fmt):
                        sem_type = "ratio"
                    elif "%" in lbl or "margin" in low_lbl or "growth" in low_lbl or "yoy" in low_lbl or "rate" in low_lbl:
                        sem_type = "ratio"
                    elif _BEGINNING_LABEL_RE.search(lbl):
                        sem_type = "not_applicable"
                    elif current_section == "balance_sheet" or _ENDING_LABEL_RE.search(lbl):
                        sem_type = "ending_balance"
                    elif _DEDUP_KPI_RE.search(lbl):
                        sem_type = "dedup_kpi"
                    elif _AVG_LABEL_RE.search(lbl):
                        sem_type = "average"
                    elif current_section in ("income_statement", "cash_flow", "segment_reporting"):
                        sem_type = "flow"

                    # Priority 2: check existing annual-sum column formulas
                    formula_hints: set[str] = set()
                    for ann_col_l, q_cols in annual_sum_quarters.items():
                        ann_ci = column_index_from_string(ann_col_l)
                        ann_expr = _extract_formula_text(ws.cell(r, ann_ci).value)
                        hint = _infer_formula_annual_hint(ann_expr, q_cols, r, ann_col_l)
                        if hint:
                            formula_hints.add(hint)
                    formula_hint_type = sorted(formula_hints)[0] if len(formula_hints) == 1 else (
                        "mixed" if len(formula_hints) > 1 else None
                    )

                    conflict_reason: str | None = None
                    if sem_type in ("ending_balance", "dedup_kpi", "average") and "flow" in formula_hints:
                        final_row_type = "missing_evidence"
                        conflict_reason = (
                            f"semantic type {sem_type!r} conflicts with existing annual =SUM(quarters) formula"
                        )
                    elif sem_type is not None:
                        final_row_type = sem_type
                    elif formula_hint_type in ("flow", "ending_balance", "average", "ratio"):
                        final_row_type = formula_hint_type
                    else:
                        final_row_type = "not_applicable"

                    line_item_map[str(r)] = {
                        "row": r,
                        "label": lbl,
                        "section": current_section,
                        "row_type": final_row_type,
                        "semantic_type": sem_type,
                        "formula_hint_type": formula_hint_type,
                        "conflict_reason": conflict_reason,
                        "unit_scale": unit_scale,
                        "num_fmt": row_num_fmt,
                        "is_subtotal_or_ratio": subtotal_or_ratio,
                    }

                # Cell-level inventory
                row_note_text = (
                    str(ws.cell(r, source_note_col).value or "").strip()
                    if source_note_col is not None
                    else ""
                )
                for c, cell in row_cells_grouped[r]:
                    val = cell.value
                    if val is None:
                        continue
                    col_l = get_column_letter(c)
                    coord = f"{col_l}{r}"
                    col_info = columns_meta.get(col_l)
                    font_rgb = model_style.get_cell_font_rgb(cell, palette)
                    fill_rgb = model_style.get_cell_fill_rgb(cell, palette)
                    bdr = getattr(cell, "border", None)
                    has_hair = bool(
                        bdr and any(
                            getattr(getattr(bdr, side, None), "style", None) == "hair"
                            for side in ("left", "right", "top", "bottom")
                        )
                    )
                    role_norm = {
                        "font_rgb": font_rgb,
                        "fill_rgb": fill_rgb,
                        "bold": bool(cell.font and cell.font.bold),
                        "italic": bool(cell.font and cell.font.italic),
                        "has_hair_border": has_hair,
                        "num_fmt": str(cell.number_format or "General"),
                    }
                    cached_t, cached_val = raw_cache.get(coord, (None, None))
                    f_txt = _extract_formula_text(val)

                    if f_txt is not None:
                        exp_refs, _, ext_op, has_dyn = expand_defined_names_in_expr(
                            f_txt, sname, defined_names, tables=tables_index
                        )
                        if ext_op:
                            subcat = "external_link"
                            ext_cells.append({"sheet": sname, "cell": coord, "reference": ext_op})
                        elif _has_plugin_call(f_txt):
                            subcat = "plugin"
                        elif has_dyn:
                            subcat = "dynamic_ref"
                        else:
                            if any(rsh != sname for rsh, _ in exp_refs):
                                subcat = "cross_sheet"
                                link_colors.add(font_rgb)
                            else:
                                subcat = "same_sheet"
                        if exp_refs:
                            node_key = f"{sname}!{coord}"
                            dep_graph[node_key] = {f"{rsh}!{rcoord}" for rsh, rcoord in exp_refs}

                        if col_info and col_info.get("kind") == "annual" and not col_info.get("is_forecast") and font_rgb == model_style.ACTUAL_FONT:
                            legacy_blue_annual_formulas += 1
                        if col_info and col_info.get("is_forecast"):
                            forecast_formula_cnt += 1

                        cells_meta[coord] = {
                            "category": "formula",
                            "subcategory": subcat,
                            "formula": f_txt,
                            "cached_value": _json_safe_value(cached_val),
                            "cached_t": cached_t,
                            "role_norm": role_norm,
                            "has_comment": cell.comment is not None,
                            "is_subtotal_or_ratio_row": subtotal_or_ratio,
                        }
                    elif isinstance(val, (int, float)) and not isinstance(val, bool):
                        # §8.3 input subcategory inference
                        if coord in consensus_snap_set:
                            in_sub = "consensus_snapshot"
                        elif col_info and col_info.get("kind") in ("annual", "quarter"):
                            is_fc = bool(col_info.get("is_forecast"))
                            if not is_fc:
                                if fill_rgb in (model_style.HIST_EST_FILL_GRAY, model_style.HIST_EST_FILL_YELLOW) or "est" in row_note_text.lower() or "估算" in row_note_text:
                                    in_sub = "hist_estimate"
                                else:
                                    in_sub = "actual_input"
                            else:
                                if fill_rgb == model_style.ASSUMP_FILL:
                                    if current_section == "guidance" or "guidance" in row_note_text.lower() or "指引" in row_note_text:
                                        in_sub = "guidance_input"
                                    else:
                                        in_sub = "driver_input"
                                else:
                                    in_sub = "not_applicable"
                                    if r > (header_row or 1):
                                        forecast_amount_const_cnt += 1
                                        baseline_findings.append(
                                            {
                                                "kind": "baseline_forecast_constant",
                                                "sheet": sname,
                                                "cell": coord,
                                                "value": _json_safe_value(val),
                                            }
                                        )
                        else:
                            in_sub = "not_applicable"

                        cells_meta[coord] = {
                            "category": "input",
                            "subcategory": in_sub,
                            "formula": None,
                            "cached_value": _json_safe_value(val),
                            "cached_t": cached_t,
                            "role_norm": role_norm,
                            "has_comment": cell.comment is not None,
                            "is_subtotal_or_ratio_row": subtotal_or_ratio,
                        }
                    else:
                        cells_meta[coord] = {
                            "category": "label" if c <= 4 or r == header_row else "constant",
                            "subcategory": "consensus_snapshot" if coord in consensus_snap_set else "text",
                            "formula": None,
                            "cached_value": _json_safe_value(val),
                            "cached_t": cached_t,
                            "role_norm": role_norm,
                            "has_comment": cell.comment is not None,
                            "is_subtotal_or_ratio_row": subtotal_or_ratio,
                        }

            # Health pre-check (§8.8 item 2)
            pane = s_xml.get("freeze_panes") or {"x_split": 0, "y_split": 0, "top_left_cell": None, "state": None}
            if sname.lower().startswith("model"):
                if pane.get("x_split", 0) > 20 or pane.get("y_split", 0) > 20:
                    health_warnings.append(
                        f"sheet {sname!r} has abnormal freeze pane ({pane.get('x_split')}, {pane.get('y_split')})"
                    )
                if forecast_formula_cnt == 0 and forecast_amount_const_cnt > 10:
                    health_warnings.append(
                        f"sheet {sname!r} has {forecast_amount_const_cnt} forecast constants and 0 formulas (possible full rewrite corruption)"
                    )

            normalized_styles[sname] = {
                "has_legacy_blue_annual_formulas": legacy_blue_annual_formulas > 0,
                "cross_sheet_link_colors": sorted(link_colors),
            }
            sheets_fp[sname] = {
                "company_tag": sheet_tag_map.get(sname),
                "period_axis": {
                    "header_row": header_row,
                    "fiscal_year_end_month": fy_end_month,
                    "fiscal_year_end_explicit": fy_explicit,
                    "unit_text": unit_text,
                    "unit_scale": unit_scale,
                    "source_note_col": source_note_col,
                    "columns": columns_meta,
                    "annual_sum_quarters": annual_sum_quarters,
                },
                "freeze_panes": pane,
                "column_layout": s_xml.get("column_layout", {}),
                "row_groups": s_xml.get("row_groups", {}),
                "ext_cf_summary": s_xml.get("ext_cf_summary", {"standard_cf": [], "x14_cf": []}),
                "line_item_map": line_item_map,
                "semantic_anchors": anchors,
                "cells": cells_meta,
            }

        circular_baseline = _tarjan_scc(dep_graph)
        if xml_meta["calc_properties"].get("full_calc_on_load"):
            health_warnings.append("baseline workbook has fullCalcOnLoad=1")

        return {
            "schema_version": 2,
            "source_path": str(path),
            "source_sha256": file_sha256(path),
            "source_bytes": path.stat().st_size,
            "naming_mode": naming_mode,
            "calc_properties": xml_meta["calc_properties"],
            "theme_palette": {str(k): v for k, v in palette.items()},
            "normalized_styles": normalized_styles,
            "sheet_order": sheet_order,
            "defined_names": defined_names,
            "tables": tables_index,
            "external_links_baseline": {
                "parts": xml_meta["external_link_parts"],
                "cells": ext_cells,
                "names": ext_names,
            },
            "circular_baseline": circular_baseline,
            "baseline_findings": baseline_findings,
            "health_check": {
                "healthy": len(health_warnings) == 0,
                "warnings": health_warnings,
            },
            "sheets": sheets_fp,
        }
    finally:
        wb.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract v2 baseline fingerprint JSON from a financial model workbook (read-only)."
    )
    parser.add_argument("workbook", help="Path to baseline .xlsx workbook")
    parser.add_argument("--out", default=None, help="Optional output JSON path")
    parser.add_argument("--ticker", default=None, help="Optional target company ticker")
    args = parser.parse_args(argv)

    fp = build_fingerprint(args.workbook, ticker=args.ticker)
    text = json.dumps(fp, indent=2, ensure_ascii=False)
    if args.out:
        out_p = Path(args.out).expanduser().resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(text + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": "ok",
                    "out": str(out_p),
                    "source_sha256": fp["source_sha256"],
                    "naming_mode": fp["naming_mode"],
                    "sheets": len(fp["sheets"]),
                    "healthy": fp["health_check"]["healthy"],
                },
                ensure_ascii=False,
            )
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
