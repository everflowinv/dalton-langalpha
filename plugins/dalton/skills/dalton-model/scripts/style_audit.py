#!/usr/bin/env python3
"""Dalton financial model static & post-recalc audit engine (style_audit.py).

Usage:
    python style_audit.py <model.xlsx>
        [--static-only]
        [--strict]
        [--sheet "<SheetName>"]
        [--baseline <baseline.xlsx>]
        [--profile <style_profile.json>]
        [--lineage <model_lineage.json>]
        [--mapping <mapping.md>]
        [--fiscal-year-end-month <1..12>]
        [--rate-band-bps <150>]
        [--warn-notes <notes.json>]

Modes & Execution Order:
    1. Pre-recalc static check (--static-only):
       Reads workbook XML, formulas, defined names, and styles directly (read-only).
       Reports external links/plugin functions, hidden #REF! broken refs, circular
       references, formula discipline, and style issues while skipping cache-dependent
       recalc and numeric tie-out checks. Use on read-only baseline workbooks with
       external links before cleanup.
    2. Full post-recalc audit (default):
       Run `.agents/skills/xlsx/scripts/recalc.py <new_model.xlsx>` first and verify
       its JSON reports `status == "success"`, then run `style_audit.py --strict`.

Checks Sheet Overall vs Script-Level Warnings & Exit Codes:
    - `Checks!C10` (`Overall`) summarizes ONLY the 8 Checks worksheet rows (`C2:C9`).
    - Worksheet `"WARN"` and `"N/A"` rows MUST document their reason in `Checks`
      column D (`"N/A"` is permitted ONLY on `segment_tieout`, `debt_rollforward`,
      and `bs_balance`). Missing column D reasons fail `checks_sheet_rollup`.
    - Script-level `warn` findings (e.g., `font_family`, `implied_rate_diagnostics`,
      `layout_and_grouping`) are emitted separately in JSON `script_warnings` with
      their required `explanation_location` (`Checks` column D or `README & Changelog`).
    - Exit codes & delivery gate:
        * Exit 1 (delivery blocked): invalid args / unreadable file, ANY `fail`-level
          finding (including `--strict` promoted checks or `Checks` `"FAIL"` /
          unexplained worksheet `"WARN"`/`"N/A"`), OR in `--strict` post-recalc mode
          when any script-level `warn` lacks an explanation in `Checks` column D,
          `README & Changelog`, or `--warn-notes`.
        * Exit 0 (delivery allowed): zero `fail` findings, and in `--strict` mode all
          worksheet `"WARN"`/`"N/A"` rows and all script-level `warn` findings have
          documented explanations (in non-strict or `--static-only` mode, unexplained
          script warnings are advisory and exit 0).
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import re
import sys
from typing import Any
import zipfile

from lxml import etree
import openpyxl
from openpyxl.formula.tokenizer import Token, Tokenizer
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.cell import coordinate_from_string

# Support `python -I style_audit.py ...` where script dir is omitted from sys.path
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import model_style as ms  # noqa: E402

# ---------------------------------------------------------------------------
# Constants & Patterns
# ---------------------------------------------------------------------------

MAX_EXAMPLES = 25
EXCEL_ERRORS: frozenset[str] = frozenset({
    "#DIV/0!",
    "#REF!",
    "#NAME?",
    "#VALUE!",
    "#N/A",
    "#NUM!",
    "#NULL!",
    "#SPILL!",
    "#CALC!",
    "#ERROR!",
})
_ERROR_RE = re.compile(r"^#(?:N/A|[A-Z0-9/]+[!?])$")

_PLUGIN_FUNC_RE = re.compile(
    r"(?:_xll\.ciqfunctions|\bCIQ\s*\(|\bBQL\s*\(|\bBDH\s*\(|\bBDP\s*\(|\bRTD\s*\(|\bWEBSERVICE\s*\()",
    re.IGNORECASE,
)
_CELL_REF_RE = re.compile(
    r"^(?:(?:'((?:[^']|'')+)'|([^'!\[\]]+))!)?\$?([A-Za-z]{1,3})\$?(\d+)$"
)
_RANGE_REF_RE = re.compile(
    r"^(?:(?:'((?:[^']|'')+)'|([^'!\[\]]+))!)?\$?([A-Za-z]{1,3})\$?(\d+):\$?([A-Za-z]{1,3})\$?(\d+)$"
)
_SHEET_CELL_FIND_RE = re.compile(
    r"(?:(?:'((?:[^']|'')+)'|([A-Za-z0-9_ .\-&>/]+))!)?\$?([A-Za-z]{1,3})\$?(\d+)(?::\$?([A-Za-z]{1,3})\$?(\d+))?"
)

# Required Checks sheet rows (Design §5.2)
REQUIRED_CHECKS: tuple[str, ...] = (
    "annual_vs_quarters",
    "segment_tieout",
    "cash_rollforward",
    "debt_rollforward",
    "share_rollforward",
    "bs_balance",
    "implied_rates",
    "summary_links",
)
EXEMPTABLE_NA_CHECKS: frozenset[str] = frozenset({
    "segment_tieout",
    "debt_rollforward",
    "bs_balance",
})
VALID_CHECK_STATUSES: frozenset[str] = frozenset({"OK", "WARN", "FAIL", "N/A"})

# Checks promoted from warn -> fail under --strict (§5.1)
STRICT_PROMOTED_CHECKS: frozenset[str] = frozenset({
    "foreign_template_style",
    "assumption_cell_style",
    "formula_number_format",
    "summary_table_links",
})

# Structural unit scale powers of 10 (§3.7 #3)
_UNIT_SCALES: frozenset[float] = frozenset({
    100.0,
    1000.0,
    10000.0,
    100000.0,
    1000000.0,
    1e8,
    1e9,
})
# Period conversion / annualization constants (§3.7 #2)
_PERIOD_CONV_NUMS: frozenset[float] = frozenset({4.0, 0.25, 12.0, 360.0, 365.0})

_ADS_ROW_RE = re.compile(
    r"(?:\bads\b|\badr\b|\bepads\b|存托|每份\s*ads|shares?\s*per\s*ads)",
    re.IGNORECASE,
)
_DUMMY_VERDICT_RE = re.compile(
    r'^=\s*IF\s*\(\s*(?:1|TRUE(?:\(\))?|0=0|1=1)\s*,',
    re.IGNORECASE,
)

# Explanation location map for script-level warnings (Codex r3 #5)
_WARN_EXPLANATION_LOCATIONS: dict[str, str] = {
    "implied_rate_diagnostics": "Checks!D8 (implied_rates row column D) or README & Changelog",
    "font_family": "Checks column D or README & Changelog",
    "actual_font_color": "Checks column D or README & Changelog",
    "assumption_cell_style": "Checks column D or README & Changelog",
    "formula_font_color": "Checks column D or README & Changelog",
    "derived_ratio_italic": "Checks column D or README & Changelog",
    "formula_number_format": "Checks column D or README & Changelog",
    "layout_and_grouping": "Checks column D or README & Changelog",
    "hidden_sheet_referenced": "Checks column D or README & Changelog",
    "ambiguous_cell_role": "Checks column D or README & Changelog",
    "segment_op_tieout": "Checks!B3/D3 (segment_tieout registration)",
    "foreign_template_style": "Checks column D or README & Changelog",
    "summary_table_links": "Checks!D9 (summary_links row column D)",
}

# ---------------------------------------------------------------------------
# Helper: Findings Accumulator
# ---------------------------------------------------------------------------


class FindingCollector:
    def __init__(self, strict: bool) -> None:
        self.strict = strict
        self._items: dict[str, dict[str, Any]] = {}

    def add(
        self,
        check: str,
        level: str,
        message: str,
        example: str | None = None,
        *,
        strict_level: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        eff_level = level
        if self.strict:
            if strict_level is not None:
                eff_level = strict_level
            elif check in STRICT_PROMOTED_CHECKS and level == "warn":
                eff_level = "fail"
        entry = self._items.get(check)
        if entry is None:
            entry = {
                "check": check,
                "level": eff_level,
                "count": 0,
                "message": message,
                "examples": [],
            }
            if details is not None:
                entry["details"] = details
            self._items[check] = entry
        else:
            # Escalate level if needed (fail > warn > info)
            rank = {"info": 0, "warn": 1, "fail": 2}
            if rank.get(eff_level, 0) > rank.get(entry["level"], 0):
                entry["level"] = eff_level
            if details:
                entry.setdefault("details", {}).update(details)

        entry["count"] += 1
        if example and len(entry["examples"]) < MAX_EXAMPLES and example not in entry["examples"]:
            entry["examples"].append(example)

    def list_findings(self) -> list[dict[str, Any]]:
        order = {"fail": 0, "warn": 1, "info": 2}
        return sorted(
            self._items.values(),
            key=lambda x: (order.get(x["level"], 9), x["check"]),
        )


# ---------------------------------------------------------------------------
# Helper: XML Cache Reader (distinguishes genuine "" from missing <v>)
# ---------------------------------------------------------------------------


def _read_xml_formula_caches(xlsx_path: Path) -> dict[str, dict[str, tuple[str | None, str | None]]]:
    """Read raw OOXML ``<c>`` cached values for every formula cell: ``{sheet: {coord: (t, text)}}``."""
    out: dict[str, dict[str, tuple[str | None, str | None]]] = {}
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        payload = {n: zf.read(n) for n in zf.namelist() if n.endswith(".xml") or n.endswith(".rels")}
    if "xl/workbook.xml" not in payload or "xl/_rels/workbook.xml.rels" not in payload:
        return out
    sp_map = ms._sheet_part_map(payload)
    for sh_name, part in sp_map.items():
        if part not in payload:
            continue
        root = etree.fromstring(payload[part], ms.XML_PARSER)
        cells: dict[str, tuple[str | None, str | None]] = {}
        for c in root.iter(f"{{{ms.SML}}}c"):
            if c.find(f"{{{ms.SML}}}f") is None:
                continue
            v = c.find(f"{{{ms.SML}}}v")
            t = c.get("t")
            if v is None:
                txt = None
            else:
                txt = "" if (v.text is None and t == "str") else v.text
            coord = (c.get("r") or "").upper()
            if coord:
                cells[coord] = (t, txt)
        out[sh_name] = cells
    return out


# ---------------------------------------------------------------------------
# Helper: External Links & Plugin Functions Check (4 layers, §5.2 & §9.3 #1)
# ---------------------------------------------------------------------------


def _check_external_and_plugin_links(
    xlsx_path: Path,
    wb: openpyxl.Workbook,
    collector: FindingCollector,
    stats: dict[str, Any],
) -> None:
    # Layer 1: ZIP package 4 sub-artifacts
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        names = zf.namelist()
        ext_parts = [n for n in names if n.startswith("xl/externalLinks/")]
        for ep in ext_parts:
            collector.add(
                "external_or_plugin_link",
                "fail",
                "Workbook contains external workbook links or third-party plugin functions (CIQ/BQL/BDH/RTD)",
                f"package:{ep}",
            )
        if "xl/workbook.xml" in names:
            wb_root = etree.fromstring(zf.read("xl/workbook.xml"), ms.XML_PARSER)
            ext_refs = list(wb_root.iter(f"{{{ms.SML}}}externalReference"))
            for er in ext_refs:
                rid = er.get(f"{{{ms.REL_NS}}}id", "")
                collector.add(
                    "external_or_plugin_link",
                    "fail",
                    "Workbook contains external workbook links or third-party plugin functions (CIQ/BQL/BDH/RTD)",
                    f"xl/workbook.xml:externalReference({rid})",
                )
        if "xl/_rels/workbook.xml.rels" in names:
            rels_root = etree.fromstring(zf.read("xl/_rels/workbook.xml.rels"), ms.XML_PARSER)
            for rel in rels_root:
                if "externalLink" in (rel.get("Type") or "") or "externalLinks/" in (rel.get("Target") or ""):
                    collector.add(
                        "external_or_plugin_link",
                        "fail",
                        "Workbook contains external workbook links or third-party plugin functions (CIQ/BQL/BDH/RTD)",
                        f"xl/_rels/workbook.xml.rels:{rel.get('Target')}",
                    )
        if "[Content_Types].xml" in names:
            ct_root = etree.fromstring(zf.read("[Content_Types].xml"), ms.XML_PARSER)
            for ov in ct_root.findall(f"{{{ms.CT_NS}}}Override"):
                pname = ov.get("PartName") or ""
                if "/xl/externalLinks/" in pname:
                    collector.add(
                        "external_or_plugin_link",
                        "fail",
                        "Workbook contains external workbook links or third-party plugin functions (CIQ/BQL/BDH/RTD)",
                        f"[Content_Types].xml:{pname}",
                    )

    # Layer 2: Defined names pointing externally
    all_names: list[tuple[str, str, str]] = [
        ("workbook", name, str(dn.value or "")) for name, dn in wb.defined_names.items()
    ]
    for ws in wb.worksheets:
        for name, dn in getattr(ws, "defined_names", {}).items():
            all_names.append((ws.title, name, str(dn.value or "")))
    for scope, name, expr in all_names:
        if _has_external_operand(expr):
            collector.add(
                "external_or_plugin_link",
                "fail",
                "Workbook contains external workbook links or third-party plugin functions (CIQ/BQL/BDH/RTD)",
                f"definedName({scope}):{name}={expr[:60]}",
            )

    # Layer 3 & 4: Cell formulas with [Book]Sheet!A1 or plugin functions
    plugin_counts: dict[str, int] = defaultdict(int)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if not (isinstance(v, str) and v.startswith("=")):
                    continue
                if _has_external_operand(v):
                    collector.add(
                        "external_or_plugin_link",
                        "fail",
                        "Workbook contains external workbook links or third-party plugin functions (CIQ/BQL/BDH/RTD)",
                        f"{ws.title}!{cell.coordinate} ({v[:60]})",
                    )
                m = _PLUGIN_FUNC_RE.search(v)
                if m:
                    tag = m.group(0).upper().replace("(", "").strip()
                    if "CIQ" in tag:
                        plugin_counts["CIQ"] += 1
                    elif "BQL" in tag:
                        plugin_counts["BQL"] += 1
                    elif "BDH" in tag:
                        plugin_counts["BDH"] += 1
                    elif "BDP" in tag:
                        plugin_counts["BDP"] += 1
                    else:
                        plugin_counts[tag] += 1
                    collector.add(
                        "external_or_plugin_link",
                        "fail",
                        "Workbook contains external workbook links or third-party plugin functions (CIQ/BQL/BDH/RTD)",
                        f"{ws.title}!{cell.coordinate} ({v[:60]})",
                    )
    if plugin_counts:
        stats["plugin_formula_counts"] = dict(sorted(plugin_counts.items()))


def _has_external_operand(expr: str) -> bool:
    if not expr or "[" not in expr:
        return False
    try:
        tokens = Tokenizer(expr if expr.startswith("=") else "=" + expr).items
    except Exception:
        return False
    for tok in tokens:
        if tok.type == Token.OPERAND and tok.subtype == Token.RANGE and "!" in tok.value:
            if "[" in tok.value.rsplit("!", 1)[0]:
                return True
    return False


# ---------------------------------------------------------------------------
# Helper: Broken #REF! Scan & Dependency Graph / Circular Reference Check
# ---------------------------------------------------------------------------


def _has_ref_error_token(expr: str) -> bool:
    if not expr or "#REF!" not in expr.upper():
        return False
    try:
        tokens = Tokenizer(expr if expr.startswith("=") else "=" + expr).items
    except Exception:
        return "#REF!" in expr.upper()
    for tok in tokens:
        if tok.type == Token.OPERAND and tok.subtype in (Token.ERROR, Token.RANGE):
            if "#REF!" in tok.value.upper():
                return True
    return False


def _parse_defined_name_targets(
    wb: openpyxl.Workbook,
) -> dict[tuple[str | None, str], list[tuple[str, int, int]]]:
    """Resolve defined names (workbook & sheet scoped) to cell coordinates for circularity graph."""
    raw_names: dict[tuple[str | None, str], str] = {}
    for name, dn in wb.defined_names.items():
        raw_names[(None, name.casefold())] = str(dn.value or "")
    for ws in wb.worksheets:
        for name, dn in getattr(ws, "defined_names", {}).items():
            raw_names[(ws.title.casefold(), name.casefold())] = str(dn.value or "")

    sheet_lookup = {ws.title.casefold(): ws.title for ws in wb.worksheets}
    resolved: dict[tuple[str | None, str], list[tuple[str, int, int]]] = {}

    def _resolve(key: tuple[str | None, str], visiting: frozenset[tuple[str | None, str]]) -> list[tuple[str, int, int]]:
        if key in resolved:
            return resolved[key]
        if key in visiting:
            return []
        expr = raw_names.get(key, "")
        if not expr:
            return []
        scope_cf = key[0]
        default_sheet = sheet_lookup.get(scope_cf, wb.sheetnames[0]) if scope_cf else wb.sheetnames[0]
        targets = _extract_formula_targets(
            expr if expr.startswith("=") else "=" + expr,
            default_sheet,
            sheet_lookup,
            raw_names,
            lambda nk: _resolve(nk, visiting | {key}),
            max_expand=256,
        )[0]
        resolved[key] = targets
        return targets

    for k in list(raw_names.keys()):
        _resolve(k, frozenset())
    return resolved


def _extract_formula_targets(
    formula: str,
    current_sheet: str,
    sheet_lookup: dict[str, str],
    raw_names: dict[tuple[str | None, str], str],
    resolve_name_fn: Any,
    *,
    max_expand: int = 512,
) -> tuple[list[tuple[str, int, int]], list[str]]:
    """Extract referenced ``(sheet, row, col)`` cells, including defined names and static ``OFFSET``."""
    targets: list[tuple[str, int, int]] = []
    unresolved_dynamic: list[str] = []
    try:
        tokens = Tokenizer(formula).items
    except Exception:
        return targets, unresolved_dynamic

    # Static OFFSET(anchor, row_off, col_off) resolver (Codex r2 疑似)
    for idx, tok in enumerate(tokens):
        if tok.type == Token.FUNC and tok.subtype == Token.OPEN:
            fname = tok.value.upper().rstrip("(")
            if fname == "INDIRECT":
                unresolved_dynamic.append("INDIRECT")
            elif fname == "OFFSET":
                # Collect top-level args inside OFFSET(...)
                args: list[list[Token]] = [[]]
                depth = 1
                j = idx + 1
                while j < len(tokens) and depth > 0:
                    t2 = tokens[j]
                    if t2.subtype == Token.OPEN:
                        depth += 1
                        args[-1].append(t2)
                    elif t2.subtype == Token.CLOSE:
                        depth -= 1
                        if depth > 0:
                            args[-1].append(t2)
                    elif t2.type == Token.SEP and t2.value == "," and depth == 1:
                        args.append([])
                    else:
                        args[-1].append(t2)
                    j += 1
                resolved_offset = False
                if len(args) >= 3 and len(args[0]) == 1:
                    anc = args[0][0]
                    if anc.type == Token.OPERAND and anc.subtype == Token.RANGE:
                        m = _CELL_REF_RE.match(anc.value)
                        if m:
                            r_off = _eval_simple_int_tokens(args[1])
                            c_off = _eval_simple_int_tokens(args[2])
                            if r_off is not None and c_off is not None:
                                sh = m.group(1).replace("''", "'") if m.group(1) else (m.group(2) or current_sheet)
                                sh_real = sheet_lookup.get(sh.casefold(), sh)
                                tr = int(m.group(4)) + r_off
                                tc = column_index_from_string(m.group(3)) + c_off
                                if tr >= 1 and tc >= 1:
                                    targets.append((sh_real, tr, tc))
                                    resolved_offset = True
                if not resolved_offset:
                    unresolved_dynamic.append("OFFSET")

    for tok in tokens:
        if tok.type != Token.OPERAND or tok.subtype != Token.RANGE:
            continue
        val = tok.value
        if "[" in val or "#REF!" in val.upper():
            continue
        m_cell = _CELL_REF_RE.match(val)
        if m_cell:
            sh = m_cell.group(1).replace("''", "'") if m_cell.group(1) else (m_cell.group(2) or current_sheet)
            sh_real = sheet_lookup.get(sh.casefold(), sh)
            targets.append((sh_real, int(m_cell.group(4)), column_index_from_string(m_cell.group(3))))
            continue
        m_rng = _RANGE_REF_RE.match(val)
        if m_rng:
            sh = m_rng.group(1).replace("''", "'") if m_rng.group(1) else (m_rng.group(2) or current_sheet)
            sh_real = sheet_lookup.get(sh.casefold(), sh)
            c1, c2 = sorted((column_index_from_string(m_rng.group(3)), column_index_from_string(m_rng.group(5))))
            r1, r2 = sorted((int(m_rng.group(4)), int(m_rng.group(6))))
            if (c2 - c1 + 1) * (r2 - r1 + 1) <= max_expand:
                for r in range(r1, r2 + 1):
                    for c in range(c1, c2 + 1):
                        targets.append((sh_real, r, c))
            continue
        # Bare defined name lookup (sheet scope first, then workbook scope)
        nm_cf = val.casefold()
        if (current_sheet.casefold(), nm_cf) in raw_names:
            targets.extend(resolve_name_fn((current_sheet.casefold(), nm_cf)))
        elif (None, nm_cf) in raw_names:
            targets.extend(resolve_name_fn((None, nm_cf)))

    return targets, unresolved_dynamic


def _eval_simple_int_tokens(toks: list[Token]) -> int | None:
    if not toks:
        return None
    if len(toks) == 1 and toks[0].type == Token.OPERAND and toks[0].subtype == Token.NUMBER:
        try:
            v = float(toks[0].value)
            return int(v) if v.is_integer() else None
        except ValueError:
            return None
    if (
        len(toks) == 2
        and toks[0].type == Token.OP_PRE
        and toks[0].value in ("+", "-")
        and toks[1].type == Token.OPERAND
        and toks[1].subtype == Token.NUMBER
    ):
        try:
            v = float(toks[1].value)
            if v.is_integer():
                return -int(v) if toks[0].value == "-" else int(v)
        except ValueError:
            return None
    return None


def _check_circular_references(
    wb: openpyxl.Workbook,
    collector: FindingCollector,
    stats: dict[str, Any],
) -> None:
    # 1. Iterative calculation flag must be disabled
    if getattr(wb.calculation, "iterate", False) is True:
        collector.add(
            "circular_reference",
            "fail",
            "Workbook has iterative calculation enabled or contains a formula circular dependency cycle",
            "workbook.calculation.iterate=True",
        )

    # 2. Build full-workbook directed dependency graph (including definedNames & static OFFSET)
    sheet_lookup = {ws.title.casefold(): ws.title for ws in wb.worksheets}
    raw_names: dict[tuple[str | None, str], str] = {}
    for name, dn in wb.defined_names.items():
        raw_names[(None, name.casefold())] = str(dn.value or "")
    for ws in wb.worksheets:
        for name, dn in getattr(ws, "defined_names", {}).items():
            raw_names[(ws.title.casefold(), name.casefold())] = str(dn.value or "")

    resolved_names = _parse_defined_name_targets(wb)
    graph: dict[tuple[str, int, int], list[tuple[str, int, int]]] = {}
    unresolved_dyn: list[str] = []

    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if not (isinstance(v, str) and v.startswith("=")):
                    continue
                node = (ws.title, cell.row, cell.column)
                targets, unres = _extract_formula_targets(
                    v,
                    ws.title,
                    sheet_lookup,
                    raw_names,
                    lambda nk: resolved_names.get(nk, []),
                )
                if targets:
                    graph[node] = targets
                if unres and len(unresolved_dyn) < MAX_EXAMPLES:
                    unresolved_dyn.append(f"{ws.title}!{cell.coordinate} ({','.join(set(unres))})")

    if unresolved_dyn:
        stats["unresolved_dynamic_refs"] = len(unresolved_dyn)

    # Iterative Tarjan's SCC algorithm (safe for very deep graphs without hitting recursion limit)
    index_counter = 0
    indices: dict[tuple[str, int, int], int] = {}
    lowlink: dict[tuple[str, int, int], int] = {}
    on_stack: set[tuple[str, int, int]] = set()
    stack: list[tuple[str, int, int]] = []
    cycles: list[list[tuple[str, int, int]]] = []

    for start in list(graph.keys()):
        if start in indices:
            continue
        call_stack: list[tuple[tuple[str, int, int], int]] = [(start, 0)]
        indices[start] = lowlink[start] = index_counter
        index_counter += 1
        stack.append(start)
        on_stack.add(start)

        while call_stack:
            v, ni = call_stack[-1]
            neighbors = graph.get(v, [])
            if ni < len(neighbors):
                call_stack[-1] = (v, ni + 1)
                w = neighbors[ni]
                if w not in graph:
                    continue
                if w not in indices:
                    indices[w] = lowlink[w] = index_counter
                    index_counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    call_stack.append((w, 0))
                elif w in on_stack:
                    if indices[w] < lowlink[v]:
                        lowlink[v] = indices[w]
            else:
                call_stack.pop()
                if call_stack:
                    parent = call_stack[-1][0]
                    if lowlink[v] < lowlink[parent]:
                        lowlink[parent] = lowlink[v]
                if lowlink[v] == indices[v]:
                    scc: list[tuple[str, int, int]] = []
                    while True:
                        w = stack.pop()
                        on_stack.remove(w)
                        scc.append(w)
                        if w == v:
                            break
                    if len(scc) > 1 or (len(scc) == 1 and scc[0] in graph.get(scc[0], [])):
                        cycles.append(scc)

    for scc in cycles:
        chain = " -> ".join(f"{s}!{get_column_letter(c)}{r}" for s, r, c in scc[:8])
        if len(scc) > 8:
            chain += f" -> ... ({len(scc)} cells)"
        collector.add(
            "circular_reference",
            "fail",
            "Workbook has iterative calculation enabled or contains a formula circular dependency cycle",
            chain,
        )


# ---------------------------------------------------------------------------
# Helper: Hidden Business Literals in Financial Output Formulas (§3.7, §5.2)
# ---------------------------------------------------------------------------


def find_hidden_business_literals(
    formula: str,
    *,
    sheet_name: str,
    row_label: str,
    is_cagr_col: bool = False,
) -> list[str]:
    """Identify business literals embedded inside a ``financial_output`` formula (§3.7).

    Exempts ONLY the 5 structural categories:
    1. Growth/complement ``1`` (``A/B - 1``, ``1 - rate``) and ``0`` in comparisons/fallback
    2. Period conversion / annualization (``*4``, ``/4``, ``*0.25``, ``*12``, ``/12``,
       ``*365``, ``/365``, ``*360``, ``/360``) and CAGR exponent ``^(1/N)``
    3. Decimal unit conversions (100, 1000, 10000, 1e6, 1e8, 1e9)
    4. ADS/ADR conversion factor on ADS/EPADS/ADR rows (e.g. ``*8``, ``/8``)
    5. Checks sheet tolerance constants
    Flags business arithmetic constants (``=E10*2``, ``=E10*1.15``) AND fixed amounts
    returned in ``IF`` branches (``=IF(E5>0, 500, 0)``).
    """
    if not isinstance(formula, str) or not formula.startswith("="):
        return []
    if sheet_name.strip().casefold() in ("checks", "check"):
        return []
    try:
        tokens = Tokenizer(formula).items
    except Exception:
        return []

    is_ads_row = bool(_ADS_ROW_RE.search(row_label))
    found: list[str] = []

    for i, tok in enumerate(tokens):
        if tok.type != Token.OPERAND or tok.subtype != Token.NUMBER:
            continue
        raw = tok.value.strip()
        is_pct = raw.endswith("%")
        try:
            val = float(raw.rstrip("%"))
        except ValueError:
            continue

        prev_tok = tokens[i - 1] if i > 0 else None
        next_tok = tokens[i + 1] if i + 1 < len(tokens) else None
        prev_val = prev_tok.value if prev_tok else ""
        next_val = next_tok.value if next_tok else ""

        # Percentages in financial_output formulas are business assumptions (not structural)
        if is_pct:
            found.append(raw)
            continue

        # 0 in comparisons (E5>0) or zero-guard fallback (IF(E5>0, ..., 0), MAX(0, ...)) is structural
        if val == 0.0:
            continue

        # Category 1: Growth/complement 1 (e.g. A/B - 1, 1 - tax_rate, ^(1/N))
        if val == 1.0:
            if prev_val in ("+", "-", "/", "(", "^") or next_val in ("+", "-", "/", ")"):
                continue

        # Category 2a: Exponent in CAGR / power: ^(1/N) or ^2
        if prev_val == "^" or (is_cagr_col and prev_val == "/" and i >= 2 and tokens[i - 2].value == "1"):
            continue
        if prev_val == "/" and i >= 2 and tokens[i - 2].value == "1":
            # Check if preceded by ^(...
            if i >= 4 and tokens[i - 4].value == "^":
                continue

        # Category 2b: Annualization / period conversion (*4, /4, *0.25, *12, /12, *365, /365, *360, /360)
        if val in _PERIOD_CONV_NUMS and (prev_val in ("*", "/") or next_val in ("*", "/")):
            continue

        # Category 3: Decimal unit scaling (100, 1000, 10000, 1e6, 1e8, 1e9)
        if val in _UNIT_SCALES and (prev_val in ("*", "/") or next_val in ("*", "/")):
            continue

        # Category 4: ADS/ADR share ratio on ADS/EPADS rows (e.g. *8, /8)
        if is_ads_row and val.is_integer() and 1 <= val <= 100 and (prev_val in ("*", "/") or next_val in ("*", "/")):
            continue

        # ROUND(expr, digits) 2nd argument is formatting precision, not business constant
        if prev_val == "," and next_val == ")" and val.is_integer() and 0 <= val <= 6:
            # Verify enclosing function is ROUND/ROUNDUP/ROUNDDOWN
            if _enclosing_func(tokens, i) in ("ROUND", "ROUNDUP", "ROUNDDOWN"):
                continue

        # OFFSET / INDEX row/col offset integers
        if _enclosing_func(tokens, i) in ("OFFSET", "INDEX", "SMALL", "LARGE", "RANK"):
            continue

        # Comparison threshold like >0 already handled; everything else (including =E10*2, =E10*1.15, IF(E5>0, 500, 0)) is flagged!
        found.append(raw)

    return found


def _enclosing_func(tokens: list[Token], idx: int) -> str | None:
    depth = 0
    for j in range(idx - 1, -1, -1):
        t = tokens[j]
        if t.subtype == Token.CLOSE:
            depth += 1
        elif t.subtype == Token.OPEN:
            if depth == 0:
                if t.type == Token.FUNC:
                    return t.value.upper().rstrip("(")
                return None
            depth -= 1
    return None


# ---------------------------------------------------------------------------
# Helper: Summed Year Checker (§3.1, §5.2, Codex r3 #1 & #2)
# ---------------------------------------------------------------------------


def _extract_same_sheet_refs(formula: str, current_sheet: str) -> list[tuple[int, int]]:
    """Return list of ``(row, col)`` referenced on the same sheet (expanding contiguous ranges)."""
    refs: list[tuple[int, int]] = []
    try:
        tokens = Tokenizer(formula).items
    except Exception:
        return refs
    cur_cf = current_sheet.casefold()
    for tok in tokens:
        if tok.type != Token.OPERAND or tok.subtype != Token.RANGE:
            continue
        val = tok.value
        if "[" in val:
            continue
        m_c = _CELL_REF_RE.match(val)
        if m_c:
            sh = m_c.group(1).replace("''", "'") if m_c.group(1) else m_c.group(2)
            if sh is None or sh.casefold() == cur_cf:
                refs.append((int(m_c.group(4)), column_index_from_string(m_c.group(3))))
            continue
        m_r = _RANGE_REF_RE.match(val)
        if m_r:
            sh = m_r.group(1).replace("''", "'") if m_r.group(1) else m_r.group(2)
            if sh is None or sh.casefold() == cur_cf:
                c1, c2 = sorted((column_index_from_string(m_r.group(3)), column_index_from_string(m_r.group(5))))
                r1, r2 = sorted((int(m_r.group(4)), int(m_r.group(6))))
                if (c2 - c1 + 1) * (r2 - r1 + 1) <= 64:
                    for r in range(r1, r2 + 1):
                        for c in range(c1, c2 + 1):
                            refs.append((r, c))
    return refs


def _is_four_quarter_sum_formula(
    formula: str,
    row: int,
    q_cols: list[int],
    current_sheet: str,
) -> bool:
    """True iff formula sums the exact 4 bound quarter cells on ``row`` (either SUM(Q1:Q4) or Q1+Q2+Q3+Q4)."""
    if not isinstance(formula, str) or not formula.startswith("="):
        return False
    refs = _extract_same_sheet_refs(formula, current_sheet)
    q_set = set(q_cols)
    ref_cols = [c for r, c in refs if r == row]
    # Allow summing the exact 4 bound quarter cells on the same row (or a different EPS row when FY EPS row sums Q1:Q4 EPS row like Model - BABA!S202 = SUM(BR200:BU200))
    if set(ref_cols) == q_set and len(ref_cols) == 4:
        fu = formula.upper()
        if "SUM(" in fu or "+" in fu:
            return True
    # Check if all 4 refs sit on ONE common row (e.g., BABA S202 = SUM(BR200:BU200))
    if len(refs) == 4:
        rows_used = {r for r, _ in refs}
        cols_used = {c for _, c in refs}
        if len(rows_used) == 1 and cols_used == q_set:
            fu = formula.upper()
            if "SUM(" in fu or "+" in fu:
                return True
    return False


def _is_q4_link_formula(formula: str, row: int, q4_col: int, current_sheet: str) -> bool:
    if not isinstance(formula, str) or not formula.startswith("="):
        return False
    refs = _extract_same_sheet_refs(formula, current_sheet)
    return len(refs) == 1 and refs[0] == (row, q4_col)


def _is_four_quarter_avg_formula(formula: str, row: int, q_cols: list[int], current_sheet: str) -> bool:
    if not isinstance(formula, str) or not formula.startswith("="):
        return False
    if "AVERAGE(" not in formula.upper():
        return False
    refs = _extract_same_sheet_refs(formula, current_sheet)
    return len(refs) == 4 and {c for r, c in refs if r == row} == set(q_cols)


def _classify_eps_formula_style(
    formula: str,
    row: int,
    ann_col: int,
    q_cols: list[int],
    current_sheet: str,
) -> str | None:
    """Return ``"sum_quarters"``, ``"ni_over_shares"``, or ``None`` if invalid annual EPS formula."""
    if _is_four_quarter_sum_formula(formula, row, q_cols, current_sheet):
        return "sum_quarters"
    if _is_derived_annual_ratio_formula(formula, ann_col, q_cols, current_sheet) and ms.is_derived_ratio_formula(formula):
        return "ni_over_shares"
    return None


def _is_same_col_aggregation_formula(formula: str, ann_col: int, current_sheet: str) -> bool:
    """True if formula aggregates/computes from other rows strictly within ``ann_col`` (e.g., Gross Profit = Rev - COGS, Total = SUM(sub-rows))."""
    if not isinstance(formula, str) or not formula.startswith("="):
        return False
    refs = _extract_same_sheet_refs(formula, current_sheet)
    if len(refs) < 2:
        return False
    return all(c == ann_col for _, c in refs)


def _is_derived_annual_ratio_formula(formula: str, ann_col: int, q_cols: list[int], current_sheet: str) -> bool:
    """True if derived ratio in summed year is computed from annual column cells (division or YoY pp diff), referencing no quarterly cols."""
    if not isinstance(formula, str) or not formula.startswith("="):
        return False
    refs = _extract_same_sheet_refs(formula, current_sheet)
    if len(refs) < 2:
        return False
    q_set = set(q_cols)
    if any(c in q_set for _, c in refs):
        return False
    if not any(c == ann_col for _, c in refs):
        return False
    # Allow ratio division (S9/R9-1, S19/S9) or rate difference (Q19-P19 for pp/YoY change)
    return ms.is_derived_ratio_formula(formula) or ("-" in formula and len(refs) == 2)


# ---------------------------------------------------------------------------
# Helper: Blank-Forecast & Discontinued Segment Registration Parser (§3.6, §5.2)
# ---------------------------------------------------------------------------


def load_registered_exemptions(
    wb: openpyxl.Workbook,
    *,
    mapping_path: Path | None = None,
    lineage_entry: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Collect registered blank-forecast / discontinued-segment exemptions from lineage, mapping.md, and README & Changelog."""
    regs: list[dict[str, str]] = []
    if lineage_entry:
        for item in lineage_entry.get("blank_forecast_registrations") or []:
            if isinstance(item, dict) and item.get("reason"):
                regs.append({
                    "sheet": str(item.get("sheet", "")).strip(),
                    "row": str(item.get("row", "")).strip(),
                    "period": str(item.get("period", "")).strip(),
                    "reason": str(item.get("reason", "")).strip(),
                })

    # Parse markdown table rows from mapping.md (columns: sheet | row | period | reason)
    if mapping_path and mapping_path.is_file():
        text = mapping_path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            s = line.strip()
            if not s.startswith("|") or s.startswith("|---") or s.startswith("| ---"):
                continue
            parts = [p.strip() for p in s.strip("|").split("|")]
            if len(parts) >= 4:
                joined = " ".join(parts).lower()
                if any(k in joined for k in ("留空", "未预测", "blank", "废止", "停用", "discontinued")):
                    regs.append({
                        "sheet": parts[0],
                        "row": parts[1],
                        "period": parts[2],
                        "reason": parts[3],
                    })

    # Parse README & Changelog worksheet if present
    for ws in wb.worksheets:
        if "readme" in ws.title.casefold() or "changelog" in ws.title.casefold():
            for row in ws.iter_rows(values_only=True):
                vals = [str(v).strip() for v in row if v is not None and str(v).strip()]
                if len(vals) >= 4:
                    joined = " ".join(vals).lower()
                    if any(k in joined for k in ("留空", "未预测", "blank", "废止", "停用", "discontinued")):
                        regs.append({
                            "sheet": vals[0],
                            "row": vals[1],
                            "period": vals[2],
                            "reason": vals[3],
                        })
    return regs


def is_registered_blank_or_discontinued(
    exemptions: list[dict[str, str]],
    *,
    sheet: str,
    row_label: str,
    period_raw: str,
    period_norm: str | None,
) -> bool:
    sh_cf = sheet.casefold()
    lbl_cf = row_label.casefold()
    per_candidates = {period_raw.casefold(), "all", "*"}
    if period_norm:
        per_candidates.add(period_norm.casefold())
    for reg in exemptions:
        if not reg.get("reason"):
            continue
        r_sh = reg.get("sheet", "").casefold()
        r_row = reg.get("row", "").casefold()
        r_per = reg.get("period", "").casefold()
        sh_ok = (not r_sh) or r_sh == sh_cf or r_sh in sh_cf
        row_ok = bool(r_row) and (r_row in lbl_cf or lbl_cf in r_row)
        per_ok = (not r_per) or (r_per in per_candidates) or any(p in r_per for p in per_candidates if p not in ("all", "*"))
        if sh_ok and row_ok and per_ok:
            return True
    return False


# ---------------------------------------------------------------------------
# Helper: Baseline Semantic Comparison (`formula_regression_vs_baseline`) (§5.2)
# ---------------------------------------------------------------------------


def _canonical_row_key(ws: Any, row: int, section_path: str) -> str:
    lbl = ms.extract_row_label(ws, row)
    norm_lbl = re.sub(r"\s+", " ", lbl).strip().casefold()
    return f"{section_path.casefold()}::{norm_lbl}"


def _build_section_paths(ws: Any, palette: dict[int, str]) -> dict[int, str]:
    """Track active section bar label (A-col colored headers) for every row."""
    cur_sec = "root"
    paths: dict[int, str] = {}
    for r in range(1, (ws.max_row or 0) + 1):
        c1 = ws.cell(r, 1)
        fl = ms.get_cell_fill_rgb(c1, palette)
        if fl and isinstance(c1.value, str) and c1.value.strip():
            if any(ms.is_color_close(fl, s) for s in (ms.SEC_REPORT_FILL, ms.SEC_SEGMENT_FILL, ms.SEC_SUBMODEL_FILL)):
                cur_sec = c1.value.strip()
        paths[r] = cur_sec
    return paths


def _check_baseline_regression(
    wb_new: openpyxl.Workbook,
    wb_base: openpyxl.Workbook,
    pmaps_new: dict[str, ms.PeriodMap],
    pmaps_base: dict[str, ms.PeriodMap],
    palette_new: dict[int, str],
    palette_base: dict[int, str],
    exemptions: list[dict[str, str]],
    collector: FindingCollector,
    stats: dict[str, Any],
    *,
    target_sheets: set[str] | None = None,
) -> None:
    checked = 0
    actual_switches = 0
    plugin_replaced = 0
    regressions = 0

    for sh_name in wb_base.sheetnames:
        if target_sheets and sh_name not in target_sheets:
            continue
        if sh_name not in wb_new.sheetnames:
            continue
        ws_b = wb_base[sh_name]
        ws_n = wb_new[sh_name]
        if ws_b.sheet_state != "visible":
            continue
        pm_b = pmaps_base[sh_name]
        pm_n = pmaps_new[sh_name]
        if not pm_b.main_columns:
            continue

        sec_b = _build_section_paths(ws_b, palette_base)
        sec_n = _build_section_paths(ws_n, palette_new)

        # Index new worksheet rows by canonical key -> list of row indices
        new_rows_by_key: dict[str, list[int]] = defaultdict(list)
        for r in range(1, (ws_n.max_row or 0) + 1):
            lbl = ms.extract_row_label(ws_n, r)
            if lbl:
                new_rows_by_key[_canonical_row_key(ws_n, r, sec_n[r])].append(r)

        # Index new worksheet columns by column_id AND normalized_period
        new_col_by_id: dict[str, ms.PeriodColumn] = {}
        new_col_by_norm: dict[str, list[ms.PeriodColumn]] = defaultdict(list)
        for hdr_r, cols in pm_n.columns_by_header_row.items():
            for pc in cols.values():
                new_col_by_id[pc.column_id] = pc
                if pc.normalized_period:
                    new_col_by_norm[pc.normalized_period].append(pc)

        for r_b in range(1, (ws_b.max_row or 0) + 1):
            if r_b in pm_b.all_header_rows():
                continue
            lbl_b = ms.extract_row_label(ws_b, r_b)
            if not lbl_b:
                continue
            key_b = _canonical_row_key(ws_b, r_b, sec_b[r_b])

            for c_b, pc_b in pm_b.main_columns.items():
                if not pc_b.is_forecast:
                    continue
                val_b = ws_b.cell(r_b, c_b).value
                if not (isinstance(val_b, str) and val_b.startswith("=")):
                    continue
                checked += 1

                # Match column in new workbook using column_id first (preserves duplicate headers like Subs - JD!R1/S1),
                # falling back to normalized_period (to catch forecast -> actual switches like 3Q26E -> 3Q26)
                pc_n = new_col_by_id.get(pc_b.column_id)
                if pc_n is None and pc_b.normalized_period:
                    cands = new_col_by_norm.get(pc_b.normalized_period, [])
                    if len(cands) == 1:
                        pc_n = cands[0]
                    elif len(cands) > pc_b.duplicate_index:
                        pc_n = cands[pc_b.duplicate_index]

                # Match row in new workbook
                matched_rows = new_rows_by_key.get(key_b, [])
                if not matched_rows or pc_n is None:
                    if is_registered_blank_or_discontinued(
                        exemptions,
                        sheet=sh_name,
                        row_label=lbl_b,
                        period_raw=pc_b.raw_label,
                        period_norm=pc_b.normalized_period,
                    ):
                        continue
                    regressions += 1
                    collector.add(
                        "formula_regression_vs_baseline",
                        "fail",
                        "Baseline forecast formula row or period is missing, hardcoded, blanked without registration, or mis-referenced",
                        f"{sh_name}!{get_column_letter(c_b)}{r_b} ({lbl_b}, {pc_b.raw_label}): missing in new workbook",
                    )
                    continue

                r_n = matched_rows[0]
                cell_n = ws_n.cell(r_n, pc_n.col_idx)
                val_n = cell_n.value

                # Legal transition 1: Forecast -> Actual switch (period header became historical actual)
                if not pc_n.is_forecast:
                    actual_switches += 1
                    continue

                # Legal transition 2: External plugin/link replaced with standard formula
                was_plugin = bool(_PLUGIN_FUNC_RE.search(val_b) or _has_external_operand(val_b))
                if was_plugin and isinstance(val_n, str) and val_n.startswith("="):
                    if not _PLUGIN_FUNC_RE.search(val_n) and not _has_external_operand(val_n):
                        plugin_replaced += 1
                        continue

                # Check still-forecast cell in new workbook
                if val_n is None or (isinstance(val_n, str) and not val_n.strip()):
                    if is_registered_blank_or_discontinued(
                        exemptions,
                        sheet=sh_name,
                        row_label=lbl_b,
                        period_raw=pc_n.raw_label,
                        period_norm=pc_n.normalized_period,
                    ):
                        continue
                    regressions += 1
                    collector.add(
                        "formula_regression_vs_baseline",
                        "fail",
                        "Baseline forecast formula row or period is missing, hardcoded, blanked without registration, or mis-referenced",
                        f"{sh_name}!{cell_n.coordinate} ({lbl_b}, {pc_n.raw_label}): formula became unregistered blank",
                    )
                elif isinstance(val_n, (int, float)) and not isinstance(val_n, bool):
                    regressions += 1
                    collector.add(
                        "formula_regression_vs_baseline",
                        "fail",
                        "Baseline forecast formula row or period is missing, hardcoded, blanked without registration, or mis-referenced",
                        f"{sh_name}!{cell_n.coordinate} ({lbl_b}, {pc_n.raw_label}): formula regressed to constant {val_n}",
                    )
                elif isinstance(val_n, str) and not val_n.startswith("="):
                    regressions += 1
                    collector.add(
                        "formula_regression_vs_baseline",
                        "fail",
                        "Baseline forecast formula row or period is missing, hardcoded, blanked without registration, or mis-referenced",
                        f"{sh_name}!{cell_n.coordinate} ({lbl_b}, {pc_n.raw_label}): formula regressed to text {val_n!r}",
                    )
                else:
                    # Both are formulas — verify PeriodMap period reference integrity for summed-year formulas
                    if pc_n.kind == "annual" and pc_n.col_idx in pm_n.annual_to_quarters:
                        q_cols = pm_n.annual_to_quarters[pc_n.col_idx]
                        if "SUM(" in str(val_n).upper():
                            refs = _extract_same_sheet_refs(str(val_n), sh_name)
                            q_refs = {c for _, c in refs if c in pm_n.main_columns and pm_n.main_columns[c].kind == "quarter"}
                            if q_refs and q_refs != set(q_cols):
                                regressions += 1
                                collector.add(
                                    "formula_regression_vs_baseline",
                                    "fail",
                                    "Baseline forecast formula row or period is missing, hardcoded, blanked without registration, or mis-referenced",
                                    f"{sh_name}!{cell_n.coordinate} ({lbl_b}, {pc_n.raw_label}): SUM references wrong quarter columns {[get_column_letter(c) for c in sorted(q_refs)]} vs expected {[get_column_letter(c) for c in q_cols]}",
                                )

    stats["baseline_formulas_checked"] = checked
    stats["verified_actual_switches"] = actual_switches
    stats["plugin_links_replaced"] = plugin_replaced
    stats["baseline_formula_regressions"] = regressions


# ---------------------------------------------------------------------------
# Helper: Checks Sheet Rollup & Independent Verification (§5.2, §9.3 #4)
# ---------------------------------------------------------------------------


def _find_checks_sheet(wb: openpyxl.Workbook) -> Any | None:
    for ws in wb.worksheets:
        if ws.title.strip().casefold() in ("checks", "check"):
            return ws
    return None


def _independent_eval_checks_row(
    check_id: str,
    b_text: str,
    c_formula: str,
    d_formula: Any,
    wb_val: openpyxl.Workbook,
) -> tuple[bool, str | None]:
    """Independently verify numeric discrepancy for registered cell pairs in Checks row (§9.3 #4)."""
    # Look for explicit cell references in D formula (e.g. =ABS(Model!G10-SUM(Model!H10:K10)))
    # or explicit pairs in B text like "Model!G10 vs SUM(Model!H10:K10)"
    # Verify that if C claims OK, any referenced diff in D is actually within tolerance
    if isinstance(d_formula, (int, float)) and not isinstance(d_formula, bool):
        tol = 0.05 if check_id == "bs_balance" else 0.5
        if abs(float(d_formula)) > tol:
            return (False, f"independent check diff={d_formula} exceeds tolerance {tol}")

    # Parse explicit "Sheet!CellA = Sheet!CellB" or "Sheet!CellA vs Sheet!CellB" in column B
    pair_re = re.compile(
        r"((?:'[^']+'|[A-Za-z0-9_ .\-]+)!)?\$?([A-Za-z]{1,3})\$?(\d+)\s*(?:=|==|vs\.?|-)\s*"
        r"((?:'[^']+'|[A-Za-z0-9_ .\-]+)!)?\$?([A-Za-z]{1,3})\$?(\d+)",
        re.IGNORECASE,
    )
    m = pair_re.search(b_text or "")
    if m:
        sh1 = (m.group(1) or "").rstrip("!").strip("'")
        sh2 = (m.group(4) or "").rstrip("!").strip("'") or sh1
        if sh1 in wb_val.sheetnames and sh2 in wb_val.sheetnames:
            v1 = wb_val[sh1][f"{m.group(2)}{m.group(3)}"].value
            v2 = wb_val[sh2][f"{m.group(5)}{m.group(6)}"].value
            if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
                diff = abs(float(v1) - float(v2))
                tol = 0.05 if check_id == "bs_balance" else max(0.5, 1e-4 * max(abs(float(v1)), abs(float(v2))))
                if diff > tol:
                    return (False, f"independent recompute |{sh1}!{m.group(2)}{m.group(3)}({v1}) - {sh2}!{m.group(5)}{m.group(6)}({v2})| = {diff:.4f} > {tol:.4f}")
    return (True, None)


def _check_checks_sheet(
    wb_f: openpyxl.Workbook,
    wb_v: openpyxl.Workbook | None,
    xml_caches: dict[str, dict[str, tuple[str | None, str | None]]],
    static_only: bool,
    collector: FindingCollector,
    stats: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Audit Checks worksheet completeness, dynamic formulas, Overall rollup, and independent tie-out."""
    checks_info: dict[str, dict[str, Any]] = {}
    ws_f = _find_checks_sheet(wb_f)
    if ws_f is None:
        collector.add(
            "checks_sheet_rollup",
            "fail",
            "Missing required Checks worksheet with 8 standard check rows and Overall rollup",
            "workbook:missing Checks sheet",
            details={"missing_checks": list(REQUIRED_CHECKS)},
        )
        stats["checks_sheet_fails"] = len(REQUIRED_CHECKS)
        stats["checks_sheet_warns"] = 0
        return checks_info

    ws_v = wb_v[ws_f.title] if wb_v is not None and ws_f.title in wb_v.sheetnames else None
    sheet_xml_cache = xml_caches.get(ws_f.title, {})

    missing_checks: list[str] = []
    uncalculated_checks: list[str] = []
    static_or_partial: list[str] = []
    failed_or_unexplained: list[str] = []

    row_by_id: dict[str, int] = {}
    for r in range(2, (ws_f.max_row or 1) + 1):
        cid = ws_f.cell(r, 1).value
        if isinstance(cid, str) and cid.strip():
            row_by_id[cid.strip().casefold()] = r

    checks_fails = 0
    checks_warns = 0

    for req_id in REQUIRED_CHECKS:
        r = row_by_id.get(req_id.casefold())
        if r is None:
            missing_checks.append(req_id)
            checks_fails += 1
            collector.add(
                "checks_sheet_rollup",
                "fail",
                "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                f"{ws_f.title}:missing required check_id '{req_id}'",
            )
            continue

        b_val = str(ws_f.cell(r, 2).value or "").strip()
        c_f = ws_f.cell(r, 3).value
        d_f = ws_f.cell(r, 4).value
        c_v = ws_v.cell(r, 3).value if ws_v is not None else None
        d_v = ws_v.cell(r, 4).value if ws_v is not None else None
        c_coord = f"C{r}"

        # Cached status lookup
        if c_v is None and c_coord in sheet_xml_cache:
            c_v = sheet_xml_cache[c_coord][1]

        checks_info[req_id] = {
            "row": r,
            "scope": b_val,
            "formula": c_f,
            "status": str(c_v).strip() if c_v is not None else None,
            "note": str(d_v if d_v is not None else (d_f or "")).strip(),
        }

        # 1. C must be a formula (not static string)
        if not (isinstance(c_f, str) and c_f.startswith("=")):
            static_or_partial.append(f"{ws_f.title}!{c_coord}({req_id}: static value {c_f!r})")
            checks_fails += 1
            collector.add(
                "checks_sheet_rollup",
                "fail",
                "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                f"{ws_f.title}!{c_coord} ({req_id}): status must be a dynamic formula, got {c_f!r}",
            )
            continue

        # 2. Reject dummy hardcoded verdict formulas like =IF(1,"OK","FAIL") (unless exemptable ="N/A")
        c_norm = c_f.strip().upper().replace(" ", "")
        is_na_formula = c_norm in ('="N/A"', "='N/A'")
        if is_na_formula:
            if req_id not in EXEMPTABLE_NA_CHECKS:
                failed_or_unexplained.append(f"{ws_f.title}!{c_coord}({req_id}: N/A not permitted)")
                checks_fails += 1
                collector.add(
                    "checks_sheet_rollup",
                    "fail",
                    "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                    f"{ws_f.title}!{c_coord} ({req_id}): N/A is only allowed for {sorted(EXEMPTABLE_NA_CHECKS)}",
                )
        else:
            has_cell_ref = bool(_extract_any_cell_refs(c_f))
            if _DUMMY_VERDICT_RE.match(c_f) or not has_cell_ref:
                static_or_partial.append(f"{ws_f.title}!{c_coord}({req_id}: dummy/unreferenced formula {c_f})")
                checks_fails += 1
                collector.add(
                    "checks_sheet_rollup",
                    "fail",
                    "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                    f"{ws_f.title}!{c_coord} ({req_id}): formula does not reference model cells ({c_f})",
                )

        # 3. Check cached value in post-recalc mode
        if not static_only:
            if c_v is None or str(c_v).strip() == "":
                uncalculated_checks.append(f"{ws_f.title}!{c_coord}({req_id})")
                checks_fails += 1
                collector.add(
                    "checks_sheet_rollup",
                    "fail",
                    "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                    f"{ws_f.title}!{c_coord} ({req_id}): uncalculated empty cache",
                )
            else:
                st = str(c_v).strip().upper()
                if st not in VALID_CHECK_STATUSES:
                    failed_or_unexplained.append(f"{ws_f.title}!{c_coord}({req_id}: invalid status {st})")
                    checks_fails += 1
                    collector.add(
                        "checks_sheet_rollup",
                        "fail",
                        "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                        f"{ws_f.title}!{c_coord} ({req_id}): status {st!r} outside closed set {sorted(VALID_CHECK_STATUSES)}",
                    )
                elif st == "FAIL":
                    failed_or_unexplained.append(f"{ws_f.title}!{c_coord}({req_id}: FAIL)")
                    checks_fails += 1
                    collector.add(
                        "checks_sheet_rollup",
                        "fail",
                        "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                        f"{ws_f.title}!{c_coord} ({req_id}): check status is FAIL",
                    )
                elif st == "WARN":
                    checks_warns += 1
                    note_txt = checks_info[req_id]["note"]
                    if not note_txt:
                        failed_or_unexplained.append(f"{ws_f.title}!D{r}({req_id}: WARN missing D-col reason)")
                        checks_fails += 1
                        collector.add(
                            "checks_sheet_rollup",
                            "fail",
                            "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                            f"{ws_f.title}!D{r} ({req_id}): WARN status requires explanation in column D",
                        )
                elif st == "N/A":
                    if req_id not in EXEMPTABLE_NA_CHECKS:
                        failed_or_unexplained.append(f"{ws_f.title}!{c_coord}({req_id}: N/A disallowed)")
                        checks_fails += 1
                        collector.add(
                            "checks_sheet_rollup",
                            "fail",
                            "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                            f"{ws_f.title}!{c_coord} ({req_id}): N/A not permitted on mandatory check",
                        )
                    elif not checks_info[req_id]["note"]:
                        failed_or_unexplained.append(f"{ws_f.title}!D{r}({req_id}: N/A missing D-col reason)")
                        checks_fails += 1
                        collector.add(
                            "checks_sheet_rollup",
                            "fail",
                            "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                            f"{ws_f.title}!D{r} ({req_id}): N/A status requires explanation in column D",
                        )
                elif st == "OK" and wb_v is not None:
                    # Independent recomputation verification (§9.3 #4)
                    ok_ind, err_msg = _independent_eval_checks_row(req_id, b_val, c_f, d_v, wb_v)
                    if not ok_ind:
                        failed_or_unexplained.append(f"{ws_f.title}!{c_coord}({req_id}: {err_msg})")
                        checks_fails += 1
                        collector.add(
                            "checks_sheet_rollup",
                            "fail",
                            "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                            f"{ws_f.title}!{c_coord} ({req_id}): claims OK but {err_msg}",
                        )

    # Check Overall row (row 10 or label 'Overall')
    ov_row = row_by_id.get("overall")
    if ov_row is None:
        missing_checks.append("Overall")
        checks_fails += 1
        collector.add(
            "checks_sheet_rollup",
            "fail",
            "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
            f"{ws_f.title}:missing Overall rollup row",
        )
    else:
        ov_f = ws_f.cell(ov_row, 3).value
        ov_v = ws_v.cell(ov_row, 3).value if ws_v is not None else None
        if not (isinstance(ov_f, str) and ov_f.startswith("=")):
            static_or_partial.append(f"{ws_f.title}!C{ov_row}(Overall static)")
            checks_fails += 1
            collector.add(
                "checks_sheet_rollup",
                "fail",
                "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                f"{ws_f.title}!C{ov_row}: Overall must be dynamic formula covering all 8 checks",
            )
        else:
            # Verify Overall covers all present check rows
            refs = _extract_same_sheet_refs(ov_f, ws_f.title)
            covered_rows = {r for r, c in refs if c == 3}
            req_rows = {row_by_id[rid.casefold()] for rid in REQUIRED_CHECKS if rid.casefold() in row_by_id}
            if req_rows and not req_rows.issubset(covered_rows):
                static_or_partial.append(f"{ws_f.title}!C{ov_row}(Overall partial rollup)")
                checks_fails += 1
                collector.add(
                    "checks_sheet_rollup",
                    "fail",
                    "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                    f"{ws_f.title}!C{ov_row}: Overall formula does not cover all 8 check rows (missing rows {sorted(req_rows - covered_rows)})",
                )
            if not static_only:
                if ov_v is None or str(ov_v).strip() == "":
                    uncalculated_checks.append(f"{ws_f.title}!C{ov_row}(Overall)")
                    checks_fails += 1
                    collector.add(
                        "checks_sheet_rollup",
                        "fail",
                        "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                        f"{ws_f.title}!C{ov_row}: Overall cache is empty",
                    )
                else:
                    ov_st = str(ov_v).strip().upper()
                    stats["checks_sheet_overall"] = ov_st
                    if ov_st == "FAIL":
                        checks_fails += 1
                        failed_or_unexplained.append(f"{ws_f.title}!C{ov_row}(Overall=FAIL)")
                        collector.add(
                            "checks_sheet_rollup",
                            "fail",
                            "Checks worksheet has missing checks, uncalculated values, static/dummy formulas, or failed/unexplained rows",
                            f"{ws_f.title}!C{ov_row}: Overall status is FAIL",
                        )

    if any((missing_checks, uncalculated_checks, static_or_partial, failed_or_unexplained)):
        # Attach categorized details to finding
        for item in collector.list_findings():
            if item["check"] == "checks_sheet_rollup":
                item["details"] = {
                    "missing_checks": missing_checks,
                    "uncalculated_checks": uncalculated_checks,
                    "static_or_partial_rollup": static_or_partial,
                    "check_failed_or_unexplained": failed_or_unexplained,
                }

    stats["checks_sheet_fails"] = checks_fails
    stats["checks_sheet_warns"] = checks_warns
    return checks_info


def _extract_any_cell_refs(formula: str) -> list[str]:
    refs: list[str] = []
    try:
        tokens = Tokenizer(formula).items
    except Exception:
        return refs
    for tok in tokens:
        if tok.type == Token.OPERAND and tok.subtype == Token.RANGE:
            if _CELL_REF_RE.match(tok.value) or _RANGE_REF_RE.match(tok.value):
                refs.append(tok.value)
    return refs


# ---------------------------------------------------------------------------
# Helper: Financial Identity Checks (Post-Recalc Cached Values, §5.2 & §9.3)
# ---------------------------------------------------------------------------


def _num(val: Any) -> float | None:
    if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(float(val)):
        return float(val)
    return None


def _check_financial_identities(
    wb_f: openpyxl.Workbook,
    wb_v: openpyxl.Workbook,
    pmaps: dict[str, ms.PeriodMap],
    checks_info: dict[str, dict[str, Any]],
    collector: FindingCollector,
    *,
    target_sheets: set[str] | None = None,
    rate_band_bps: float = 150.0,
) -> None:
    for ws_f in wb_f.worksheets:
        if ws_f.sheet_state != "visible":
            continue
        if target_sheets and ws_f.title not in target_sheets:
            continue
        if ws_f.title.strip().casefold() in ("checks", "check", "readme & changelog"):
            continue
        ws_v = wb_v[ws_f.title]
        pmap = pmaps[ws_f.title]
        if not pmap.main_columns:
            continue

        # Collect labeled rows for identity checks
        row_labels: dict[int, str] = {}
        for r in range(1, (ws_f.max_row or 0) + 1):
            if r in pmap.all_header_rows():
                continue
            lbl = ms.extract_row_label(ws_f, r)
            if lbl:
                row_labels[r] = lbl

        # --- Identity 1: annual_quarters_tieout ---
        if pmap.annual_to_quarters:
            ann_warn_note = (checks_info.get("annual_vs_quarters") or {}).get("note", "")
            ann_status = (checks_info.get("annual_vs_quarters") or {}).get("status", "")
            for ann_col, q_cols in pmap.annual_to_quarters.items():
                pc_ann = pmap.main_columns[ann_col]
                for r, lbl in row_labels.items():
                    f_val = ws_f.cell(r, ann_col).value
                    if not (isinstance(f_val, str) and f_val.startswith("=")):
                        continue
                    v_ann = _num(ws_v.cell(r, ann_col).value)
                    q_vals = [_num(ws_v.cell(r, qc).value) for qc in q_cols]
                    if v_ann is None or any(qv is None for qv in q_vals):
                        continue
                    attr, _ = ms.classify_row_attribute(
                        lbl, f_val, bound_q_cols=q_cols, col_idx=ann_col, is_summed_year=True
                    )
                    if attr == "flow":
                        expected = sum(q_vals)  # type: ignore[arg-type]
                        tol = max(0.5, 1e-4 * abs(v_ann))
                        if abs(v_ann - expected) > tol:
                            if not pc_ann.is_forecast and ann_status == "WARN" and ann_warn_note:
                                continue
                            collector.add(
                                "annual_quarters_tieout",
                                "fail",
                                "Annual flow/balance value disagrees with bound 4-quarter sum or Q4 balance",
                                f"{ws_f.title}!{get_column_letter(ann_col)}{r} ({lbl}, {pc_ann.raw_label}): annual={v_ann:.2f} vs sum(Q1..Q4)={expected:.2f}",
                            )
                    elif attr == "beginning_balance":
                        expected = q_vals[0]  # type: ignore[index]
                        tol = max(0.5, 1e-4 * abs(v_ann))
                        if abs(v_ann - expected) > tol:
                            if not pc_ann.is_forecast and ann_status == "WARN" and ann_warn_note:
                                continue
                            collector.add(
                                "annual_quarters_tieout",
                                "fail",
                                "Annual flow/balance value disagrees with bound 4-quarter sum or Q1/Q4 balance",
                                f"{ws_f.title}!{get_column_letter(ann_col)}{r} ({lbl}, {pc_ann.raw_label}): annual={v_ann:.2f} vs Q1={expected:.2f}",
                            )
                    elif attr == "point_in_time":
                        expected = q_vals[3]  # type: ignore[index]
                        tol = max(0.5, 1e-4 * abs(v_ann))
                        if abs(v_ann - expected) > tol:
                            if not pc_ann.is_forecast and ann_status == "WARN" and ann_warn_note:
                                continue
                            collector.add(
                                "annual_quarters_tieout",
                                "fail",
                                "Annual flow/balance value disagrees with bound 4-quarter sum or Q4 balance",
                                f"{ws_f.title}!{get_column_letter(ann_col)}{r} ({lbl}, {pc_ann.raw_label}): annual={v_ann:.2f} vs Q4={expected:.2f}",
                            )

        # --- Identity 2: segment_op_tieout ---
        _check_segment_tieout(ws_f, ws_v, pmap, row_labels, checks_info, collector)

        # --- Identity 3: cash_debt_share_rollforward (incl. §9.3 #3 cross-period & 3-statement linkage) ---
        _check_rollforwards(ws_f, ws_v, pmap, row_labels, collector)

        # --- Identity 4: balance_sheet_tieout ---
        _check_balance_sheet_tieout(ws_f, ws_v, pmap, row_labels, collector)

        # --- Diagnostics: implied_rate_diagnostics ---
        _check_implied_rates(ws_f, ws_v, pmap, row_labels, collector, rate_band_bps=rate_band_bps)


def _check_segment_tieout(
    ws_f: Any,
    ws_v: Any,
    pmap: ms.PeriodMap,
    row_labels: dict[int, str],
    checks_info: dict[str, dict[str, Any]],
    collector: FindingCollector,
) -> None:
    # Detect explicit segment total rows and group operating profit rows
    seg_total_rows = [
        r for r, l in row_labels.items()
        if re.search(r"(?:total\s+segment\s+(?:operating\s+(?:income|profit)|ebita|revenue)|分部(?:营业利润|收入)合计)", l, re.IGNORECASE)
    ]
    group_op_rows = [
        r for r, l in row_labels.items()
        if re.search(r"(?:^(?:group\s+|total\s+|consolidated\s+)?operating\s+(?:income|profit)$|集团营业利润|^营业利润$)", l.split("|")[-1].strip(), re.IGNORECASE)
    ]
    unalloc_rows = [
        r for r, l in row_labels.items()
        if re.search(r"(?:unallocated|corporate\s+overhead|eliminations?|未分配|总部费用|分部间抵销)", l, re.IGNORECASE)
    ]

    if not seg_total_rows or not group_op_rows:
        return

    if len(group_op_rows) > 1 and not (checks_info.get("segment_tieout") or {}).get("scope"):
        collector.add(
            "segment_op_tieout",
            "warn",
            "Multiple candidate group operating profit rows found; register exact segment-to-group row pairing in Checks!B3",
            f"{ws_f.title}: candidate group OP rows {group_op_rows}",
        )
        return

    g_row = group_op_rows[0]
    s_row = seg_total_rows[0]
    for col_idx, pc in pmap.main_columns.items():
        if not pc.is_forecast or pc.kind == "cagr_or_delta":
            continue
        gv = _num(ws_v.cell(g_row, col_idx).value)
        sv = _num(ws_v.cell(s_row, col_idx).value)
        if gv is None or sv is None:
            continue
        adj = sum(_num(ws_v.cell(ur, col_idx).value) or 0.0 for ur in unalloc_rows)
        diff = abs(gv - (sv + adj))
        tol = max(0.5, 1e-4 * abs(gv))
        if diff > tol:
            collector.add(
                "segment_op_tieout",
                "fail",
                "Group operating profit/revenue does not equal sum of segments plus unallocated/eliminations",
                f"{ws_f.title}!{pc.col_letter}{g_row} ({pc.raw_label}): group={gv:.2f} vs segments+adj={sv + adj:.2f}",
            )


def _find_single_row(row_labels: dict[int, str], pattern: str) -> int | None:
    rx = re.compile(pattern, re.IGNORECASE)
    matches = [r for r, l in row_labels.items() if rx.search(l)]
    return matches[0] if matches else None


def _check_rollforwards(
    ws_f: Any,
    ws_v: Any,
    pmap: ms.PeriodMap,
    row_labels: dict[int, str],
    collector: FindingCollector,
) -> None:
    annual_cols = sorted(
        [pc for pc in pmap.main_columns.values() if pc.kind == "annual"],
        key=lambda x: x.col_idx,
    )
    quarter_cols = sorted(
        [pc for pc in pmap.main_columns.values() if pc.kind == "quarter"],
        key=lambda x: x.col_idx,
    )

    # 1. Cash rollforward: Beg Cash + Net Cash Change = End Cash + cross-period continuity + BS cash tie-out
    beg_cash_r = _find_single_row(row_labels, r"(?:beginning\s+cash|期初现金)")
    end_cash_r = _find_single_row(row_labels, r"(?:ending\s+cash|期末现金)")
    net_chg_cash_r = _find_single_row(row_labels, r"(?:net\s+change\s+in\s+cash|change\s+in\s+cash|净现金变动|现金净变动)")
    bs_cash_r = _find_single_row(row_labels, r"(?:^|\|\s*)(?:cash\s+and\s+(?:cash\s+equivalents|short[- ]term\s+investments)|资产负债表现金|货币资金)$")

    if beg_cash_r and end_cash_r:
        for series_name, cols in (("annual", annual_cols), ("quarter", quarter_cols)):
            for idx, pc in enumerate(cols):
                if not pc.is_forecast:
                    continue
                beg_v = _num(ws_v.cell(beg_cash_r, pc.col_idx).value)
                end_v = _num(ws_v.cell(end_cash_r, pc.col_idx).value)
                # Within-period identity
                if beg_v is not None and end_v is not None and net_chg_cash_r is not None:
                    chg_v = _num(ws_v.cell(net_chg_cash_r, pc.col_idx).value)
                    if chg_v is not None:
                        tol = max(0.5, 1e-4 * max(abs(end_v), abs(beg_v)))
                        if abs((beg_v + chg_v) - end_v) > tol:
                            collector.add(
                                "cash_debt_share_rollforward",
                                "fail",
                                "Cash, debt, or ending share rollforward fails within-period, cross-period, or 3-statement linkage",
                                f"{ws_f.title}!{pc.col_letter}{end_cash_r} ({pc.raw_label}): ending cash={end_v:.2f} != beg({beg_v:.2f})+chg({chg_v:.2f})",
                            )
                # Cross-period continuity: Beg(t) == End(t-1) separately for annual & quarterly chains (§9.3 #3)
                if idx > 0 and beg_v is not None:
                    prev_pc = cols[idx - 1]
                    prev_end_v = _num(ws_v.cell(end_cash_r, prev_pc.col_idx).value)
                    if prev_end_v is not None:
                        tol = max(0.5, 1e-4 * max(abs(beg_v), abs(prev_end_v)))
                        if abs(beg_v - prev_end_v) > tol:
                            collector.add(
                                "cash_debt_share_rollforward",
                                "fail",
                                "Cash, debt, or ending share rollforward fails within-period, cross-period, or 3-statement linkage",
                                f"{ws_f.title}!{pc.col_letter}{beg_cash_r} ({series_name} {pc.raw_label}): beg cash={beg_v:.2f} != prev ending cash ({prev_pc.raw_label})={prev_end_v:.2f}",
                            )
                # 3-statement linkage: CF Ending Cash == BS Cash (§9.3 #3)
                if bs_cash_r and bs_cash_r != end_cash_r and end_v is not None:
                    bs_v = _num(ws_v.cell(bs_cash_r, pc.col_idx).value)
                    if bs_v is not None:
                        tol = max(0.5, 1e-4 * max(abs(end_v), abs(bs_v)))
                        if abs(end_v - bs_v) > tol:
                            collector.add(
                                "cash_debt_share_rollforward",
                                "fail",
                                "Cash, debt, or ending share rollforward fails within-period, cross-period, or 3-statement linkage",
                                f"{ws_f.title}!{pc.col_letter}{bs_cash_r} ({pc.raw_label}): BS cash={bs_v:.2f} != CF ending cash={end_v:.2f}",
                            )

    # 2. Debt rollforward (if modeled)
    beg_debt_r = _find_single_row(row_labels, r"(?:beginning\s+(?:total\s+)?debt|期初有息债)")
    end_debt_r = _find_single_row(row_labels, r"(?:ending\s+(?:total\s+)?debt|期末有息债)")
    debt_add_r = _find_single_row(row_labels, r"(?:new\s+debt|debt\s+issuance|新增债务)")
    debt_repay_r = _find_single_row(row_labels, r"(?:debt\s+repayment|repayment\s+of\s+debt|偿还债务)")
    if beg_debt_r and end_debt_r:
        for series_name, cols in (("annual", annual_cols), ("quarter", quarter_cols)):
            for idx, pc in enumerate(cols):
                if not pc.is_forecast:
                    continue
                beg_v = _num(ws_v.cell(beg_debt_r, pc.col_idx).value)
                end_v = _num(ws_v.cell(end_debt_r, pc.col_idx).value)
                if beg_v is not None and end_v is not None and (debt_add_r or debt_repay_r):
                    add_v = _num(ws_v.cell(debt_add_r, pc.col_idx).value) if debt_add_r else 0.0
                    rep_v = _num(ws_v.cell(debt_repay_r, pc.col_idx).value) if debt_repay_r else 0.0
                    if add_v is not None and rep_v is not None:
                        # Support both positive repayment (subtracted) and negative cash-flow sign (added)
                        exp_sub = beg_v + add_v - abs(rep_v)
                        tol = max(0.5, 1e-4 * max(abs(end_v), abs(beg_v)))
                        if abs(end_v - exp_sub) > tol:
                            collector.add(
                                "cash_debt_share_rollforward",
                                "fail",
                                "Cash, debt, or ending share rollforward fails within-period, cross-period, or 3-statement linkage",
                                f"{ws_f.title}!{pc.col_letter}{end_debt_r} ({pc.raw_label}): ending debt={end_v:.2f} != {exp_sub:.2f}",
                            )
                if idx > 0 and beg_v is not None:
                    prev_pc = cols[idx - 1]
                    prev_end_v = _num(ws_v.cell(end_debt_r, prev_pc.col_idx).value)
                    if prev_end_v is not None:
                        tol = max(0.5, 1e-4 * max(abs(beg_v), abs(prev_end_v)))
                        if abs(beg_v - prev_end_v) > tol:
                            collector.add(
                                "cash_debt_share_rollforward",
                                "fail",
                                "Cash, debt, or ending share rollforward fails within-period, cross-period, or 3-statement linkage",
                                f"{ws_f.title}!{pc.col_letter}{beg_debt_r} ({series_name} {pc.raw_label}): beg debt={beg_v:.2f} != prev ending debt={prev_end_v:.2f}",
                            )

    # 3. Ending Share rollforward & Buyback CF linkage (§3.4, §9.3 #3)
    beg_sh_r = _find_single_row(row_labels, r"(?:beginning\s+(?:total\s+)?shares|期初(?:总)?股数)")
    end_sh_r = _find_single_row(row_labels, r"(?:ending\s+(?:total\s+)?shares|期末(?:总)?股数)")
    sbc_sh_r = _find_single_row(row_labels, r"(?:sbc\s+(?:shares|dilution|issuance)|equity\s+vesting\s+shares|股权激励增发股数)")
    bb_sh_r = _find_single_row(row_labels, r"(?:(?:share\s+)?(?:buyback|repurchase)\s+shares|shares\s+repurchased|回购注销股数)")
    cf_bb_r = _find_single_row(row_labels, r"(?:share\s+(?:buyback|repurchase)\s+(?:cash|outflow|spend)|repurchase\s+of\s+(?:common|ordinary)\s+stock|股份回购现金支出|回购支出)")
    wt_sh_r = _find_single_row(row_labels, r"(?:weighted\s*(?:average|avg)\.?\s*diluted\s*shares|加权平均稀释股数)")
    eps_r = _find_single_row(row_labels, r"(?:\beps\b|\bepads\b|每股收益)")

    if beg_sh_r and end_sh_r:
        for series_name, cols in (("annual", annual_cols), ("quarter", quarter_cols)):
            for idx, pc in enumerate(cols):
                if not pc.is_forecast:
                    continue
                beg_v = _num(ws_v.cell(beg_sh_r, pc.col_idx).value)
                end_v = _num(ws_v.cell(end_sh_r, pc.col_idx).value)
                if beg_v is not None and end_v is not None and (sbc_sh_r or bb_sh_r):
                    sbc_v = _num(ws_v.cell(sbc_sh_r, pc.col_idx).value) if sbc_sh_r else 0.0
                    bb_v = _num(ws_v.cell(bb_sh_r, pc.col_idx).value) if bb_sh_r else 0.0
                    if sbc_v is not None and bb_v is not None:
                        exp_end = beg_v + abs(sbc_v) - abs(bb_v)
                        tol = max(0.5, 1e-4 * abs(end_v))
                        if abs(end_v - exp_end) > tol:
                            collector.add(
                                "cash_debt_share_rollforward",
                                "fail",
                                "Cash, debt, or ending share rollforward fails within-period, cross-period, or 3-statement linkage",
                                f"{ws_f.title}!{pc.col_letter}{end_sh_r} ({pc.raw_label}): ending shares={end_v:.2f} != beg+SBC-buyback={exp_end:.2f}",
                            )
                # Cross-period continuity for shares
                if idx > 0 and beg_v is not None:
                    prev_pc = cols[idx - 1]
                    prev_end_v = _num(ws_v.cell(end_sh_r, prev_pc.col_idx).value)
                    if prev_end_v is not None:
                        tol = max(0.5, 1e-4 * max(abs(beg_v), abs(prev_end_v)))
                        if abs(beg_v - prev_end_v) > tol:
                            collector.add(
                                "cash_debt_share_rollforward",
                                "fail",
                                "Cash, debt, or ending share rollforward fails within-period, cross-period, or 3-statement linkage",
                                f"{ws_f.title}!{pc.col_letter}{beg_sh_r} ({series_name} {pc.raw_label}): beg shares={beg_v:.2f} != prev ending shares={prev_end_v:.2f}",
                            )
                # Verify buyback shares formula references Cash Flow buyback line (§9.3 #3)
                if bb_sh_r and cf_bb_r and not (pc.kind == "annual" and pc.col_idx in pmap.annual_to_quarters):
                    bb_f = ws_f.cell(bb_sh_r, pc.col_idx).value
                    if isinstance(bb_f, str) and bb_f.startswith("="):
                        refs = _extract_same_sheet_refs(bb_f, ws_f.title)
                        if not any(r == cf_bb_r for r, _ in refs):
                            collector.add(
                                "cash_debt_share_rollforward",
                                "fail",
                                "Cash, debt, or ending share rollforward fails within-period, cross-period, or 3-statement linkage",
                                f"{ws_f.title}!{pc.col_letter}{bb_sh_r} ({pc.raw_label}): buyback shares formula does not reference cash flow buyback row {cf_bb_r}",
                            )
                # Verify EPS denominator references weighted average diluted shares (when not summing 4Q EPS)
                if eps_r and wt_sh_r and not (pc.kind == "annual" and pc.col_idx in pmap.annual_to_quarters):
                    eps_f = ws_f.cell(eps_r, pc.col_idx).value
                    if isinstance(eps_f, str) and eps_f.startswith("=") and "/" in eps_f:
                        refs = _extract_same_sheet_refs(eps_f, ws_f.title)
                        if not any(r == wt_sh_r for r, _ in refs):
                            collector.add(
                                "cash_debt_share_rollforward",
                                "fail",
                                "Cash, debt, or ending share rollforward fails within-period, cross-period, or 3-statement linkage",
                                f"{ws_f.title}!{pc.col_letter}{eps_r} ({pc.raw_label}): EPS denominator does not reference weighted average diluted shares row {wt_sh_r}",
                            )


def _check_balance_sheet_tieout(
    ws_f: Any,
    ws_v: Any,
    pmap: ms.PeriodMap,
    row_labels: dict[int, str],
    collector: FindingCollector,
) -> None:
    assets_r = _find_single_row(row_labels, r"(?:^|\|\s*)(?:total\s+assets|资产总计|总资产)$")
    liab_r = _find_single_row(row_labels, r"(?:^|\|\s*)(?:total\s+liabilities|负债合计|总负债)$")
    eq_r = _find_single_row(row_labels, r"(?:^|\|\s*)(?:total\s+(?:shareholders'?|stockholders'?)?\s*equity|股东权益合计|所有者权益合计|总权益)$")
    liab_eq_r = _find_single_row(row_labels, r"(?:total\s+liabilities\s*(?:and|&)\s*(?:shareholders'?|stockholders'?)?\s*equity|负债和(?:股东|所有者)权益总计)")

    if not assets_r:
        return
    if not liab_eq_r and not (liab_r and eq_r):
        return

    for col_idx, pc in pmap.main_columns.items():
        if pc.kind == "cagr_or_delta":
            continue
        av = _num(ws_v.cell(assets_r, col_idx).value)
        if av is None:
            continue
        if liab_eq_r:
            rhs = _num(ws_v.cell(liab_eq_r, col_idx).value)
        else:
            lv = _num(ws_v.cell(liab_r, col_idx).value)  # type: ignore[arg-type]
            ev = _num(ws_v.cell(eq_r, col_idx).value)  # type: ignore[arg-type]
            rhs = (lv + ev) if (lv is not None and ev is not None) else None
        if rhs is None:
            continue
        if abs(av - rhs) >= 0.05:
            collector.add(
                "balance_sheet_tieout",
                "fail",
                "Balance sheet fails to balance: |Total Assets - (Total Liabilities + Total Equity)| >= 0.05",
                f"{ws_f.title}!{pc.col_letter}{assets_r} ({pc.raw_label}): Assets={av:.2f} vs L+E={rhs:.2f} (diff={av - rhs:.2f})",
            )


def _check_implied_rates(
    ws_f: Any,
    ws_v: Any,
    pmap: ms.PeriodMap,
    row_labels: dict[int, str],
    collector: FindingCollector,
    *,
    rate_band_bps: float = 150.0,
) -> None:
    band = rate_band_bps / 10000.0
    int_inc_r = _find_single_row(row_labels, r"(?:^|\|\s*)(?:interest\s+income|利息收入)$")
    int_exp_r = _find_single_row(row_labels, r"(?:^|\|\s*)(?:interest\s+expense|利息支出)$")
    cash_r = _find_single_row(row_labels, r"(?:beginning\s+cash|ending\s+cash|cash\s+and\s+(?:cash\s+equivalents|short[- ]term\s+investments)|期初现金|期末现金|现金及短期投资)")
    debt_r = _find_single_row(row_labels, r"(?:beginning\s+(?:total\s+)?debt|ending\s+(?:total\s+)?debt|total\s+debt|期初有息债|期末有息债|总有息债)")

    annual_cols = sorted(
        [pc for pc in pmap.main_columns.values() if pc.kind == "annual"],
        key=lambda x: x.col_idx,
    )

    def _eval_pair(int_row: int, bal_row: int, label_kind: str) -> None:
        hist_rates: list[float] = []
        fcst_points: list[tuple[ms.PeriodColumn, float]] = []
        for idx, pc in enumerate(annual_cols):
            iv = _num(ws_v.cell(int_row, pc.col_idx).value)
            # Use beginning balance (or prior period ending balance) to match non-circular convention
            bv = _num(ws_v.cell(bal_row, pc.col_idx).value)
            if "beginning" not in row_labels[bal_row].lower() and "期初" not in row_labels[bal_row] and idx > 0:
                prev_bv = _num(ws_v.cell(bal_row, annual_cols[idx - 1].col_idx).value)
                if prev_bv is not None and prev_bv > 0:
                    bv = prev_bv
            if iv is None or bv is None or bv <= 0:
                continue
            rate = abs(iv) / bv if label_kind == "borrowing" and iv < 0 else iv / bv
            if not pc.is_forecast:
                if 0.0 <= rate <= 0.20:
                    hist_rates.append(rate)
            else:
                fcst_points.append((pc, rate))

        if not fcst_points:
            return
        hist_min = min(hist_rates) if hist_rates else None
        hist_max = max(hist_rates) if hist_rates else None

        for pc, rate in fcst_points:
            unit_suspect = rate < 0.0 or rate > 0.20
            out_of_hist = False
            if hist_min is not None and hist_max is not None:
                if rate < hist_min - band or rate > hist_max + band:
                    out_of_hist = True
            if unit_suspect:
                collector.add(
                    "implied_rate_diagnostics",
                    "warn",
                    "Forecast implied annualized interest rate deviates outside company historical range or triggers unit_or_row_mismatch_suspect",
                    f"{ws_f.title}!{pc.col_letter}{int_row} ({pc.raw_label} implied {label_kind} rate {rate*100:.2f}%: unit_or_row_mismatch_suspect)",
                    details={"unit_or_row_mismatch_suspect": True},
                )
            elif out_of_hist:
                collector.add(
                    "implied_rate_diagnostics",
                    "warn",
                    "Forecast implied annualized interest rate sits outside company historical range; explain rate environment change in Checks column D",
                    f"{ws_f.title}!{pc.col_letter}{int_row} ({pc.raw_label} implied {label_kind} rate {rate*100:.2f}% vs historical [{hist_min*100:.1f}%, {hist_max*100:.1f}%], band ±{int(rate_band_bps)} bps)",
                )

    if int_inc_r and cash_r:
        _eval_pair(int_inc_r, cash_r, "cash yield")
    if int_exp_r and debt_r:
        _eval_pair(int_exp_r, debt_r, "borrowing")


# ---------------------------------------------------------------------------
# Helper: Summary Table Links Check (§3.8, §5.2)
# ---------------------------------------------------------------------------


def _check_summary_table_links(
    wb_f: openpyxl.Workbook,
    pmaps: dict[str, ms.PeriodMap],
    collector: FindingCollector,
) -> None:
    # Identify candidate summary sheets
    summary_sheets = [
        ws for ws in wb_f.worksheets
        if ws.sheet_state == "visible"
        and re.search(r"(?:^summary$|^valuation|^peers$|^global\s+comps$|readme\s*&\s*changelog)", ws.title, re.IGNORECASE)
    ]
    if not summary_sheets:
        return

    rev_re = re.compile(r"(?:total\s+revenue|revenue|营业(?:总)?收入|总收入)", re.IGNORECASE)
    eps_re = re.compile(r"(?:\beps\b|\bepads\b|每股收益)", re.IGNORECASE)

    # Check if there is a dedicated Summary sheet with period columns
    explicit_summaries = [ws for ws in summary_sheets if ws.title.strip().casefold() == "summary"]
    target_ws_list = explicit_summaries if explicit_summaries else []
    if not target_ws_list:
        return

    for ws in target_ws_list:
        pmap = pmaps[ws.title]
        fcst_ann = [pc for pc in pmap.main_columns.values() if pc.kind == "annual" and pc.is_forecast]
        fcst_q = [pc for pc in pmap.main_columns.values() if pc.kind == "quarter" and pc.is_forecast]

        if not fcst_ann or not fcst_q:
            collector.add(
                "summary_table_links",
                "warn",
                "Summary sheet must include both annual and quarterly forecast series for Revenue and EPS/EPADS linked by formula to matching periods",
                f"{ws.title}: missing {'annual' if not fcst_ann else 'quarterly'} forecast period columns",
            )
            continue

        found_rev = False
        found_eps = False
        for r in range(1, (ws.max_row or 0) + 1):
            if r in pmap.all_header_rows():
                continue
            lbl = ms.extract_row_label(ws, r)
            if not lbl:
                continue
            is_rev = bool(rev_re.search(lbl)) and not ms._RATIO_LABEL_RE.search(lbl)
            is_eps = bool(eps_re.search(lbl)) and not ms._RATIO_LABEL_RE.search(lbl)
            if not (is_rev or is_eps):
                continue
            if is_rev:
                found_rev = True
            if is_eps:
                found_eps = True

            for pc in fcst_ann + fcst_q:
                val = ws.cell(r, pc.col_idx).value
                if not (isinstance(val, str) and val.startswith("=")):
                    collector.add(
                        "summary_table_links",
                        "warn",
                        "Summary sheet must include both annual and quarterly forecast series for Revenue and EPS/EPADS linked by formula to matching periods",
                        f"{ws.title}!{pc.col_letter}{r} ({lbl}, {pc.raw_label}): must be formula link, got {val!r}",
                    )
                else:
                    # Verify cross-sheet target column matches same normalized period
                    try:
                        tokens = Tokenizer(val).items
                    except Exception:
                        tokens = []
                    for tok in tokens:
                        if tok.type == Token.OPERAND and tok.subtype == Token.RANGE and "!" in tok.value:
                            m = _CELL_REF_RE.match(tok.value)
                            if m:
                                tsh = m.group(1).replace("''", "'") if m.group(1) else m.group(2)
                                if tsh and tsh in pmaps:
                                    tcol = column_index_from_string(m.group(3))
                                    trow = int(m.group(4))
                                    tpc = pmaps[tsh].column_for_cell(trow, tcol)
                                    if (
                                        tpc is not None
                                        and pc.normalized_period
                                        and tpc.normalized_period
                                        and tpc.normalized_period != pc.normalized_period
                                    ):
                                        collector.add(
                                            "summary_table_links",
                                            "warn",
                                            "Summary sheet must include both annual and quarterly forecast series for Revenue and EPS/EPADS linked by formula to matching periods",
                                            f"{ws.title}!{pc.col_letter}{r} ({pc.normalized_period}) links to mismatched period {tsh}!{m.group(3)}{trow} ({tpc.normalized_period})",
                                        )

        if not found_rev:
            collector.add(
                "summary_table_links",
                "warn",
                "Summary sheet must include both annual and quarterly forecast series for Revenue and EPS/EPADS linked by formula to matching periods",
                f"{ws.title}: missing Revenue row",
            )
        if not found_eps:
            collector.add(
                "summary_table_links",
                "warn",
                "Summary sheet must include both annual and quarterly forecast series for Revenue and EPS/EPADS linked by formula to matching periods",
                f"{ws.title}: missing EPS/EPADS row",
            )


# ---------------------------------------------------------------------------
# Helper: Script Warning Explanation Resolution (Codex r3 #5)
# ---------------------------------------------------------------------------


def _collect_explanation_corpus(
    wb_f: openpyxl.Workbook,
    wb_v: openpyxl.Workbook | None,
    warn_notes: dict[str, str] | None,
) -> tuple[str, dict[str, str]]:
    """Gather all explanation text from Checks column D, README & Changelog, and --warn-notes."""
    chunks: list[str] = []
    per_check_notes: dict[str, str] = {}

    ws_chk = _find_checks_sheet(wb_f)
    if ws_chk is not None:
        ws_v = wb_v[ws_chk.title] if wb_v is not None and ws_chk.title in wb_v.sheetnames else ws_chk
        for r in range(2, (ws_chk.max_row or 1) + 1):
            cid = str(ws_chk.cell(r, 1).value or "").strip().casefold()
            d_txt = str(ws_v.cell(r, 4).value or ws_chk.cell(r, 4).value or "").strip()
            if d_txt and not d_txt.startswith("="):
                chunks.append(d_txt)
                if cid:
                    per_check_notes[cid] = d_txt

    for ws in wb_f.worksheets:
        if "readme" in ws.title.casefold() or "changelog" in ws.title.casefold():
            for row in ws.iter_rows(values_only=True):
                for v in row:
                    if isinstance(v, str) and v.strip():
                        chunks.append(v.strip())

    if warn_notes:
        for k, v in warn_notes.items():
            if isinstance(v, str) and v.strip():
                per_check_notes[k.casefold()] = v.strip()
                chunks.append(f"{k}: {v.strip()}")

    return "\n".join(chunks).casefold(), per_check_notes


def _is_script_warn_explained(check: str, corpus_lower: str, per_check_notes: dict[str, str]) -> bool:
    if check.casefold() in per_check_notes and per_check_notes[check.casefold()]:
        return True
    if check.casefold() in corpus_lower:
        return True
    # Map script checks to corresponding Checks sheet row IDs
    alias_map = {
        "implied_rate_diagnostics": ("implied_rates", "rate", "yield", "利率", "收益率"),
        "summary_table_links": ("summary_links",),
        "segment_op_tieout": ("segment_tieout",),
    }
    for alias in alias_map.get(check, ()):
        if alias in per_check_notes and per_check_notes[alias]:
            return True
    if check == "implied_rate_diagnostics" and per_check_notes.get("implied_rates"):
        return True
    return False


# ---------------------------------------------------------------------------
# Main Audit Orchestrator
# ---------------------------------------------------------------------------


def audit_workbook(
    model_path: str | Path,
    *,
    static_only: bool = False,
    strict: bool = False,
    sheet: str | None = None,
    baseline_path: str | Path | None = None,
    profile_path: str | Path | None = None,
    lineage_path: str | Path | None = None,
    mapping_path: str | Path | None = None,
    fiscal_year_end_month: int | None = None,
    rate_band_bps: float = 150.0,
    warn_notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run full or static-only audit on ``model_path`` and return structured JSON dict."""
    mpath = Path(model_path)
    if not mpath.is_file():
        return {
            "status": "error",
            "strict": strict,
            "static_only": static_only,
            "file": str(mpath),
            "message": f"Workbook file not found: {mpath}",
        }

    # Resolve lineage if provided (Codex r3 #4)
    lineage_entry: dict[str, Any] | None = None
    if lineage_path and Path(lineage_path).is_file():
        lineage_entry = ms.resolve_lineage_entry(lineage_path, output_path=mpath)
        if lineage_entry:
            if baseline_path is None and lineage_entry.get("baseline_path"):
                baseline_path = lineage_entry["baseline_path"]
            if profile_path is None and lineage_entry.get("profile_path"):
                profile_path = lineage_entry["profile_path"]
            if fiscal_year_end_month is None and lineage_entry.get("fiscal_year_end_month"):
                fiscal_year_end_month = int(lineage_entry["fiscal_year_end_month"])

    # Determine active StyleProfile (strictly from baseline / pre-generated profile, NEVER self-profiled!)
    profile_source = "default"
    if profile_path and Path(profile_path).is_file():
        profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
        profile_source = "profile"
    elif baseline_path and Path(baseline_path).is_file():
        profile = ms.profile_workbook_style(baseline_path, fiscal_year_end_month=fiscal_year_end_month)
        profile_source = "baseline"
    else:
        profile = ms.default_style_profile()
        profile_source = "default"

    is_existing_model = profile_source in ("baseline", "profile")

    try:
        wb_f = openpyxl.load_workbook(mpath, data_only=False)
        wb_v = None if static_only else openpyxl.load_workbook(mpath, data_only=True)
    except Exception as exc:
        return {
            "status": "error",
            "strict": strict,
            "static_only": static_only,
            "file": str(mpath),
            "message": f"Failed to load workbook: {exc}",
        }

    if sheet and sheet not in wb_f.sheetnames:
        return {
            "status": "error",
            "strict": strict,
            "static_only": static_only,
            "file": str(mpath),
            "message": f"Sheet not found in workbook: {sheet!r}",
        }

    target_sheets = {sheet} if sheet else None
    palette = ms.extract_theme_palette(mpath)
    xml_caches = {} if static_only else _read_xml_formula_caches(mpath)

    collector = FindingCollector(strict=strict)
    stats: dict[str, Any] = {
        "sheets_audited": 0,
        "formula_cells": 0,
        "relative_assumption_formulas": 0,
        "baseline_formulas_checked": 0,
        "verified_actual_switches": 0,
        "plugin_links_replaced": 0,
        "baseline_formula_regressions": 0,
        "checks_sheet_fails": 0,
        "checks_sheet_warns": 0,
    }

    # Build PeriodMap for every sheet once (shared with profiler)
    col_identities_all = (lineage_entry or {}).get("column_identities") or {}
    pmaps: dict[str, ms.PeriodMap] = {}
    for ws in wb_f.worksheets:
        sh_fye = fiscal_year_end_month
        if sh_fye is None and is_existing_model:
            sh_ov = (profile.get("sheet_overrides") or {}).get(ws.title) or {}
            if sh_ov.get("fiscal_year_end_month"):
                sh_fye = int(sh_ov["fiscal_year_end_month"])
        pmaps[ws.title] = ms.build_period_map(
            ws,
            fiscal_year_end_month=sh_fye,
            column_identities=col_identities_all.get(ws.title),
        )

    exemptions = load_registered_exemptions(
        wb_f,
        mapping_path=Path(mapping_path) if mapping_path else None,
        lineage_entry=lineage_entry,
    )

    # 1. Basic & Recalc structural checks (workbook-wide)
    _check_external_and_plugin_links(mpath, wb_f, collector, stats)
    _check_circular_references(wb_f, collector, stats)

    # Hidden sheet reference tracking
    hidden_sheets = {ws.title.casefold(): ws.title for ws in wb_f.worksheets if ws.sheet_state != "visible"}
    referenced_hidden: set[str] = set()

    # Broken #REF! in formulas (scan cells first so real cell #REF! like Peers!I36 lead examples)
    for ws in wb_f.worksheets:
        if target_sheets and ws.title not in target_sheets:
            continue
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v.startswith("="):
                    if _has_ref_error_token(v):
                        collector.add(
                            "broken_formula_ref",
                            "fail",
                            "Formula or defined name contains broken #REF! reference token (even if wrapped in IFERROR/IFNA)",
                            f"{ws.title}!{cell.coordinate} ({v[:60]})",
                        )
                    if ws.sheet_state == "visible" and "!" in v and hidden_sheets:
                        try:
                            for tok in Tokenizer(v).items:
                                if tok.type == Token.OPERAND and tok.subtype == Token.RANGE and "!" in tok.value:
                                    q = tok.value.rsplit("!", 1)[0]
                                    if "[" not in q:
                                        if q.startswith("'") and q.endswith("'"):
                                            q = q[1:-1].replace("''", "'")
                                        if q.casefold() in hidden_sheets:
                                            referenced_hidden.add(hidden_sheets[q.casefold()])
                                            collector.add(
                                                "hidden_sheet_referenced",
                                                "warn",
                                                "Visible calculation sheet references a hidden or veryHidden worksheet",
                                                f"{ws.title}!{cell.coordinate} -> {hidden_sheets[q.casefold()]}",
                                            )
                        except Exception:
                            pass

    # Broken #REF! in defined names
    for name, dn in wb_f.defined_names.items():
        expr = str(dn.value or "")
        if _has_ref_error_token(expr):
            collector.add(
                "broken_formula_ref",
                "fail",
                "Formula or defined name contains broken #REF! reference token (even if wrapped in IFERROR/IFNA)",
                f"definedName(workbook):{name}={expr[:60]}",
            )
    for ws in wb_f.worksheets:
        for name, dn in getattr(ws, "defined_names", {}).items():
            expr = str(dn.value or "")
            if _has_ref_error_token(expr):
                collector.add(
                    "broken_formula_ref",
                    "fail",
                    "Formula or defined name contains broken #REF! reference token (even if wrapped in IFERROR/IFNA)",
                    f"definedName({ws.title}):{name}={expr[:60]}",
                )

    unreferenced_hidden = sorted(set(hidden_sheets.values()) - referenced_hidden)
    if unreferenced_hidden:
        stats["unreferenced_hidden_sheets"] = unreferenced_hidden

    # Load baseline workbook if given
    wb_base = None
    pmaps_base: dict[str, ms.PeriodMap] = {}
    palette_base: dict[int, str] = {}
    if baseline_path and Path(baseline_path).is_file():
        wb_base = openpyxl.load_workbook(baseline_path, data_only=False)
        palette_base = ms.extract_theme_palette(baseline_path)
        for ws_b in wb_base.worksheets:
            pmaps_base[ws_b.title] = ms.build_period_map(
                ws_b, fiscal_year_end_month=fiscal_year_end_month
            )

    # 2. Per-sheet Style, Formula Discipline, Summed Year, Cached Errors & Unrecalculated
    wb_def = profile.get("workbook_defaults") or ms.default_style_profile()["workbook_defaults"]
    allowed_fonts = set(wb_def.get("allowed_fonts") or ms.ALLOWED_DEFAULT_FONTS)
    allowed_fills = set(wb_def.get("allowed_fills") or ms.DEFAULT_ALLOWED_FILLS)
    actual_font_col = wb_def.get("actual_font_color") or ms.ACTUAL_FONT
    link_font_col = wb_def.get("link_font_color") or ms.LINK_FONT
    assump_cfg = wb_def.get("assumption_style") or {}
    assump_font_col = assump_cfg.get("font_color") or ms.ASSUMP_FONT
    assump_fill_col = assump_cfg.get("fill_color") or ms.ASSUMP_FILL
    assump_border = assump_cfg.get("border_style") or ms.ASSUMP_BORDER

    for ws in wb_f.worksheets:
        if ws.sheet_state != "visible":
            continue
        if target_sheets and ws.title not in target_sheets:
            continue
        sh_ov = (profile.get("sheet_overrides") or {}).get(ws.title) or {}

        # Skip true blank divider sheets (e.g. PDD>>, Tiger>>), NEVER skip non-empty >> sheets like Futu>>!
        non_empty = 0
        formulas_in_sh = 0
        numerics_in_sh = 0
        formula_rows: set[int] = set()
        for r_idx, row_vals in enumerate(ws.iter_rows(values_only=True), start=1):
            has_f = False
            for v in row_vals:
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                non_empty += 1
                if isinstance(v, str) and v.startswith("="):
                    formulas_in_sh += 1
                    has_f = True
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    numerics_in_sh += 1
            if has_f:
                formula_rows.add(r_idx)

        if formulas_in_sh == 0 and numerics_in_sh == 0 and non_empty <= 2:
            continue

        stats["sheets_audited"] += 1
        pmap = pmaps[ws.title]
        ws_v = wb_v[ws.title] if wb_v is not None else None
        sh_xml_cache = xml_caches.get(ws.title, {})

        # Per-sheet dominant font size (respects Futu>> 11pt override!)
        expected_font_size = float(
            sh_ov.get("dominant_font_size") or wb_def.get("dominant_font_size") or ms.FONT_SIZE
        )
        sh_allowed_fonts = allowed_fonts | set(sh_ov.get("fonts_observed") or [])
        sh_allowed_fills = allowed_fills | set(sh_ov.get("fills_observed") or [])
        sh_has_general_precedent = bool(is_existing_model and sh_ov.get("has_general_formula_nfmt"))
        is_special_meta_sheet = ws.title.strip().casefold() in ("checks", "check", "readme & changelog")

        # Track per-row EPS summed-year formula style for Codex r3 #2 (consistent per metric row!)
        row_eps_styles: dict[int, set[str]] = defaultdict(set)
        # Track existing row number formats across historical/other period columns
        row_period_nfmts: dict[int, set[str]] = defaultdict(set)

        if not is_special_meta_sheet and pmap.main_columns:
            for r_idx in range(1, (ws.max_row or 0) + 1):
                if r_idx in pmap.all_header_rows():
                    continue
                for c_idx, pc in pmap.main_columns.items():
                    if pc.kind == "cagr_or_delta":
                        continue
                    cell_tmp = ws.cell(r_idx, c_idx)
                    if cell_tmp.value is not None and not pc.is_forecast:
                        nf = str(cell_tmp.number_format or "General")
                        if nf != "General":
                            row_period_nfmts[r_idx].add(nf)

        # Layout checks (skip Checks/README)
        if not is_special_meta_sheet and pmap.main_columns:
            grid_hidden = bool(ws.views and ws.views.sheetView and ws.views.sheetView[0].showGridLines is False)
            if not grid_hidden:
                collector.add(
                    "layout_and_grouping",
                    "warn",
                    "Worksheet layout deviates from profile (gridlines, column order, early-column outline grouping, or freeze panes)",
                    f"{ws.title}: gridlines are visible (showGridLines != False)",
                )
            ann_cols = [pc.col_idx for pc in pmap.main_columns.values() if pc.kind == "annual"]
            q_cols_all = [pc.col_idx for pc in pmap.main_columns.values() if pc.kind == "quarter"]
            if ann_cols and q_cols_all and max(ann_cols) > min(q_cols_all):
                collector.add(
                    "layout_and_grouping",
                    "warn",
                    "Worksheet layout deviates from profile (gridlines, column order, early-column outline grouping, or freeze panes)",
                    f"{ws.title}: annual columns appear after quarterly columns",
                )
            # Check freeze row preservation when baseline exists
            if is_existing_model and sh_ov.get("freeze_panes") and ws.freeze_panes:
                try:
                    _, base_fr = coordinate_from_string(str(sh_ov["freeze_panes"]))
                    _, cur_fr = coordinate_from_string(str(ws.freeze_panes))
                    if base_fr != cur_fr:
                        collector.add(
                            "layout_and_grouping",
                            "warn",
                            "Worksheet layout deviates from profile (gridlines, column order, early-column outline grouping, or freeze panes)",
                            f"{ws.title}: freeze_panes row changed from {sh_ov['freeze_panes']} to {ws.freeze_panes}",
                        )
                except Exception:
                    pass

        # Scan every cell in worksheet
        for row_cells in ws.iter_rows():
            r_idx = row_cells[0].row
            if r_idx in pmap.all_header_rows():
                continue
            row_label = ms.extract_row_label(ws, r_idx)
            row_has_f = r_idx in formula_rows

            # Determine if this row is a mandatory forecast financial output row (per period kind: annual vs quarterly)
            fcst_cols_in_row: list[ms.PeriodColumn] = []
            if not is_special_meta_sheet and pmap.main_columns and row_label:
                if (
                    ms._FINANCIAL_AMOUNT_RE.search(row_label)
                    or ms._EPS_LABEL_RE.search(row_label)
                    or ms._CASH_FLOW_RE.search(row_label)
                    or ms._POINT_IN_TIME_RE.search(row_label)
                ) and not ms._RATIO_LABEL_RE.search(row_label):
                    for kind_k in ("annual", "quarter"):
                        kind_cols = [
                            pc for pc in pmap.main_columns.values()
                            if pc.is_forecast and pc.kind == kind_k
                        ]
                        if any(ws.cell(r_idx, pc.col_idx).value is not None for pc in kind_cols):
                            fcst_cols_in_row.extend(kind_cols)

            # Check mandatory forecast result cells left unregistered blank (Codex r2 #2 & r3 #1)
            if fcst_cols_in_row:
                for pc in fcst_cols_in_row:
                    v_chk = ws.cell(r_idx, pc.col_idx).value
                    if v_chk is None or (isinstance(v_chk, str) and not v_chk.strip()):
                        if not is_registered_blank_or_discontinued(
                            exemptions,
                            sheet=ws.title,
                            row_label=row_label,
                            period_raw=pc.raw_label,
                            period_norm=pc.normalized_period,
                        ):
                            collector.add(
                                "forecast_amount_hardcode",
                                "fail",
                                "Forecast financial output cell contains a hardcoded number or unregistered missing formula",
                                f"{ws.title}!{pc.col_letter}{r_idx} ({row_label}, {pc.raw_label}): mandatory forecast output cell is blank without registration",
                            )

            # Rightmost source note text on this row (for zero-assumption check, Codex r3 #1)
            row_note_txt = ""
            if pmap.source_note_col is not None:
                nv = ws.cell(r_idx, pmap.source_note_col).value
                if isinstance(nv, str) and nv.strip():
                    row_note_txt = nv.strip()
            else:
                # Fallback: check column right after last period col
                if pmap.main_columns:
                    after_c = max(pmap.main_columns.keys()) + 1
                    nv = ws.cell(r_idx, after_c).value
                    if isinstance(nv, str) and nv.strip():
                        row_note_txt = nv.strip()

            for cell in row_cells:
                val = cell.value
                if val is None or (isinstance(val, str) and not val.strip()):
                    continue

                is_formula = isinstance(val, str) and val.startswith("=")
                coord = cell.coordinate
                pc = pmap.column_for_cell(r_idx, cell.column)
                is_summed_year = bool(
                    pc is not None
                    and pc.kind == "annual"
                    and pc.is_forecast
                    and pc.col_idx in pmap.annual_to_quarters
                )

                if is_formula:
                    stats["formula_cells"] += 1

                # --- Cached error & unrecalculated (post-recalc mode only) ---
                cached_val = None
                if not static_only and is_formula:
                    cached_val = ws_v.cell(r_idx, cell.column).value if ws_v is not None else None
                    t_xml, v_xml = sh_xml_cache.get(coord, (None, None))
                    if cached_val is None and v_xml is None:
                        collector.add(
                            "unrecalculated",
                            "fail",
                            "Formula cell has no cached value (run recalc.py before full audit)",
                            f"{ws.title}!{coord} ({val[:40]})",
                        )
                    else:
                        err_str = None
                        if isinstance(cached_val, str) and _ERROR_RE.match(cached_val.strip()):
                            err_str = cached_val.strip()
                        elif t_xml == "e" and v_xml:
                            err_str = v_xml.strip()
                        if err_str:
                            collector.add(
                                "cached_error",
                                "fail",
                                "Formula cell evaluates to an Excel error value",
                                f"{ws.title}!{coord} ({err_str}: {val[:40]})",
                            )

                # --- Foreign template fill & font size / font family checks ---
                fl_rgb = ms.get_cell_fill_rgb(cell, palette)
                fn_rgb = ms.get_cell_font_rgb(cell, palette)
                font_obj = cell.font
                if fl_rgb and fl_rgb in ms.FOREIGN_TEMPLATE_FILLS and fl_rgb not in sh_allowed_fills:
                    collector.add(
                        "foreign_template_style",
                        "warn",
                        "Foreign template fill color or non-profile font size detected",
                        f"{ws.title}!{coord} (fill={fl_rgb})",
                    )
                if font_obj and font_obj.size is not None and not is_special_meta_sheet:
                    if abs(float(font_obj.size) - expected_font_size) > 0.5:
                        # Allow observed sizes on existing sheet
                        obs_sizes = sh_ov.get("font_sizes_observed") or []
                        if not (is_existing_model and float(font_obj.size) in obs_sizes):
                            collector.add(
                                "foreign_template_style",
                                "warn",
                                "Foreign template fill color or non-profile font size detected",
                                f"{ws.title}!{coord} (size={font_obj.size}pt vs expected {expected_font_size}pt)",
                            )
                if font_obj and font_obj.name and not is_special_meta_sheet:
                    if font_obj.name not in sh_allowed_fonts:
                        collector.add(
                            "font_family",
                            "warn",
                            "Cell font family is outside the active StyleProfile allowed fonts",
                            f"{ws.title}!{coord} (font={font_obj.name})",
                        )

                if is_special_meta_sheet:
                    continue

                # Classify cell role using shared §3.7 order (row purpose + period FIRST, style SECOND)
                role, sub_role = ms.classify_cell_role(
                    ws,
                    r_idx,
                    cell.column,
                    pmap,
                    palette=palette,
                    profile=profile,
                    row_has_formulas=row_has_f,
                )

                # --- Historical actual font color ---
                if sub_role == "historical_actual" and isinstance(val, (int, float)) and not isinstance(val, bool):
                    if not ms.is_color_close(fn_rgb, actual_font_col):
                        collector.add(
                            "actual_font_color",
                            "warn",
                            "Historical actual numeric cell font color deviates from profile actual color (default 0000FF)",
                            f"{ws.title}!{coord} (font_rgb={fn_rgb})",
                        )

                # --- Forecast financial_output hardcoded number check (§3.7, §5.2) ---
                if role == "financial_output" and not is_formula and pc is not None and pc.is_forecast:
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        # Summed year hardcode triggers summed_year_violation as well as forecast_amount_hardcode
                        collector.add(
                            "forecast_amount_hardcode",
                            "fail",
                            "Forecast financial output cell contains a hardcoded number (even if styled as assumption)",
                            f"{ws.title}!{coord} ({row_label}, {pc.raw_label}): hardcoded {val}",
                        )
                        if is_summed_year:
                            collector.add(
                                "summed_year_violation",
                                "fail",
                                "Summed annual forecast column violates 4-quarter aggregation, EPS, derived ratio, or zero-assumption rules",
                                f"{ws.title}!{coord} ({row_label}, {pc.raw_label}): hardcoded constant {val} in summed year",
                            )
                        continue

                # --- Driver assumption checks (style + zero-value note + relative assumption tracking) ---
                if role == "driver_assumption":
                    if is_formula and ms.is_legal_driver_assumption_formula(val):
                        stats["relative_assumption_formulas"] += 1

                    # Codex r3 #1: 0 or =0 in assumption cell requires source/reason note!
                    is_zero_val = (
                        (isinstance(val, (int, float)) and not isinstance(val, bool) and float(val) == 0.0)
                        or (isinstance(val, str) and re.match(r"^=\s*\(*\s*[+-]?0(?:\.0+)?%?\s*\)*\s*$", val))
                    )
                    if is_zero_val and sub_role != "historical_estimate":
                        if not row_note_txt:
                            collector.add(
                                "formula_literal",
                                "fail",
                                "Literal zero assumption (0 or =0) requires explicit evidence note in rightmost source note column",
                                f"{ws.title}!{coord} ({row_label}): zero assumption {val!r} missing source/evidence note",
                            )
                    elif is_formula and ms.is_pure_number_formula(val) and not is_zero_val:
                        # Non-zero bare literal formula =500 is disallowed
                        collector.add(
                            "formula_literal",
                            "fail",
                            "Formula body is a bare numeric literal",
                            f"{ws.title}!{coord} ({val})",
                        )

                    # Assumption visual styling check (non-summed forecast driver_assumption)
                    if sub_role != "historical_estimate" and pc is not None and pc.is_forecast:
                        style_errs: list[str] = []
                        if not ms.is_color_close(fn_rgb, assump_font_col):
                            style_errs.append(f"font={fn_rgb}")
                        if not ms.is_color_close(fl_rgb, assump_fill_col):
                            style_errs.append(f"fill={fl_rgb}")
                        if not ms.has_four_side_border(cell, assump_border):
                            style_errs.append(f"border!={assump_border}")
                        is_pct_cell = "%" in str(cell.number_format or "") or bool(ms._RATIO_LABEL_RE.search(row_label))
                        if is_pct_cell and not (font_obj and font_obj.italic):
                            style_errs.append("pct_not_italic")
                        if style_errs:
                            collector.add(
                                "assumption_cell_style",
                                "warn",
                                "Forecast driver assumption cell does not match active StyleProfile assumption styling",
                                f"{ws.title}!{coord} ({','.join(style_errs)})",
                            )
                    continue

                # --- Financial output formula checks ---
                if role == "financial_output" and is_formula:
                    # 1. Bare literal formula (=0, =500) on financial_output
                    if ms.is_pure_number_formula(val):
                        collector.add(
                            "formula_literal",
                            "fail",
                            "Financial output cell formula is a bare numeric literal",
                            f"{ws.title}!{coord} ({val})",
                        )
                    else:
                        # 2. Hidden business literals in financial_output formulas
                        lits = find_hidden_business_literals(
                            val,
                            sheet_name=ws.title,
                            row_label=row_label,
                            is_cagr_col=bool(pc and pc.kind == "cagr_or_delta"),
                        )
                        if lits:
                            collector.add(
                                "formula_hidden_literal",
                                "fail",
                                "Financial output formula embeds non-structural business literal constants",
                                f"{ws.title}!{coord} ({row_label}): literals={lits} in {val[:60]}",
                            )

                    # 3. Summed-year rules (§3.1, §5.2, Codex r3 #1 & #2)
                    if is_summed_year and pc is not None and not ms.is_cross_sheet_formula(val, ws.title):
                        q_cols = pmap.annual_to_quarters[pc.col_idx]
                        # Check residual assumption fill on summed year
                        if ms.is_styled_as_assumption(cell, palette, profile):
                            collector.add(
                                "summed_year_violation",
                                "fail",
                                "Summed annual forecast column must not retain assumption fill or driver input styling",
                                f"{ws.title}!{coord} ({row_label}, {pc.raw_label}): residual assumption styling in summed year",
                            )
                        attr, conflict = ms.classify_row_attribute(
                            row_label,
                            val,
                            bound_q_cols=q_cols,
                            col_idx=pc.col_idx,
                            is_summed_year=True,
                        )
                        if attr == "flow":
                            if not (
                                _is_four_quarter_sum_formula(val, r_idx, q_cols, ws.title)
                                or _is_same_col_aggregation_formula(val, pc.col_idx, ws.title)
                            ):
                                collector.add(
                                    "summed_year_violation",
                                    "fail",
                                    "Summed annual forecast flow row must sum its 4 bound fiscal quarters (SUM or 4-cell add) or aggregate same-year subtotals",
                                    f"{ws.title}!{coord} ({row_label}, {pc.raw_label}): {val[:60]}",
                                )
                        elif attr == "beginning_balance":
                            refs_beg = _extract_same_sheet_refs(val, ws.title)
                            is_q1_or_prev_ann = len(refs_beg) == 1 and (
                                refs_beg[0] == (r_idx, q_cols[0])
                                or (
                                    refs_beg[0][1] < pc.col_idx
                                    and refs_beg[0][1] in pmap.main_columns
                                    and pmap.main_columns[refs_beg[0][1]].kind == "annual"
                                )
                            )
                            if not is_q1_or_prev_ann:
                                collector.add(
                                    "summed_year_violation",
                                    "fail",
                                    "Summed annual forecast beginning balance row must link to Q1 or prior annual ending balance",
                                    f"{ws.title}!{coord} ({row_label}, {pc.raw_label}): {val[:60]}",
                                )
                        elif attr == "point_in_time":
                            if not (
                                _is_q4_link_formula(val, r_idx, q_cols[3], ws.title)
                                or _is_same_col_aggregation_formula(val, pc.col_idx, ws.title)
                            ):
                                collector.add(
                                    "summed_year_violation",
                                    "fail",
                                    "Summed annual forecast point-in-time balance row must reference its 4th bound quarter (=Q4) or aggregate same-year subtotals",
                                    f"{ws.title}!{coord} ({row_label}, {pc.raw_label}): {val[:60]}",
                                )
                        elif attr == "weighted_shares":
                            if not (
                                _is_four_quarter_avg_formula(val, r_idx, q_cols, ws.title)
                                or _is_q4_link_formula(val, r_idx, q_cols[3], ws.title)
                            ):
                                collector.add(
                                    "summed_year_violation",
                                    "fail",
                                    "Summed annual forecast weighted shares row must use AVERAGE(Q1:Q4) or =Q4",
                                    f"{ws.title}!{coord} ({row_label}, {pc.raw_label}): {val[:60]}",
                                )
                        elif attr == "eps":
                            eps_st = _classify_eps_formula_style(val, r_idx, pc.col_idx, q_cols, ws.title)
                            if eps_st is None:
                                collector.add(
                                    "summed_year_violation",
                                    "fail",
                                    "Summed annual forecast EPS/EPADS row must be annual NI/shares or sum of 4 quarterly EPS",
                                    f"{ws.title}!{coord} ({row_label}, {pc.raw_label}): {val[:60]}",
                                )
                            else:
                                row_eps_styles[r_idx].add(eps_st)
                        elif attr == "derived_ratio":
                            if not _is_derived_annual_ratio_formula(val, pc.col_idx, q_cols, ws.title):
                                collector.add(
                                    "summed_year_violation",
                                    "fail",
                                    "Summed annual forecast derived ratio row must be recomputed from annual amount cells",
                                    f"{ws.title}!{coord} ({row_label}, {pc.raw_label}): {val[:60]}",
                                )
                        elif conflict:
                            collector.add(
                                "ambiguous_cell_role",
                                "warn",
                                "Row attribute could not be reconciled cleanly between period, formula structure, and label",
                                f"{ws.title}!{coord} ({row_label}, {pc.raw_label}): {val[:50]}",
                            )

                    # 4. Formula font color (black for same-sheet, green for cross-sheet)
                    is_xlink = ms.is_cross_sheet_formula(val, ws.title)
                    is_hist_col = pc is not None and not pc.is_forecast
                    if not (is_existing_model and is_hist_col):
                        if is_xlink:
                            if not ms.is_color_close(fn_rgb, link_font_col):
                                collector.add(
                                    "formula_font_color",
                                    "warn",
                                    "Cross-sheet link formula should use link color (008000); same-sheet result formula should use black (000000)",
                                    f"{ws.title}!{coord} (cross-sheet link font={fn_rgb})",
                                )
                        else:
                            # Same-sheet result formula: allow black (000000) or dark-red target price (C00000)
                            is_tp = ms.is_color_close(fn_rgb, ms.TARGET_PRICE_FONT)
                            if not ms.is_color_close(fn_rgb, ms.FORMULA_FONT) and not is_tp:
                                collector.add(
                                    "formula_font_color",
                                    "warn",
                                    "Cross-sheet link formula should use link color (008000); same-sheet result formula should use black (000000)",
                                    f"{ws.title}!{coord} (same-sheet formula font={fn_rgb})",
                                )

                    # 5. Derived ratio italic check (allows green + italic simultaneously!)
                    if sub_role == "derived_ratio":
                        if not (font_obj and font_obj.italic):
                            collector.add(
                                "derived_ratio_italic",
                                "warn",
                                "Derived ratio formula cell should be italic (green+italic allowed for cross-sheet ratios)",
                                f"{ws.title}!{coord} ({row_label})",
                            )

                    # 6. Formula number format check (Codex r3 #6: preserve existing General precedent like Futu>>)
                    nfmt = str(cell.number_format or "General")
                    is_numeric_formula = True
                    if not static_only and cached_val is not None and isinstance(cached_val, str):
                        is_numeric_formula = False
                    elif '"' in val.replace('""', "") or "&" in val:
                        is_numeric_formula = False

                    if is_numeric_formula:
                        if nfmt == "General":
                            # Allow General on existing sheets that already carry General formulas (e.g., Futu>>)
                            if not sh_has_general_precedent:
                                collector.add(
                                    "formula_number_format",
                                    "warn",
                                    "Numeric result formula uses General format or inconsistent format across same row",
                                    f"{ws.title}!{coord} (number_format=General)",
                                )
                        elif pc is not None and pc.is_forecast and row_period_nfmts.get(r_idx):
                            if nfmt not in row_period_nfmts[r_idx]:
                                collector.add(
                                    "formula_number_format",
                                    "warn",
                                    "Numeric result formula uses General format or inconsistent format across same row",
                                    f"{ws.title}!{coord} (nfmt={nfmt!r} vs row historical {sorted(row_period_nfmts[r_idx])})",
                                )

        # Check per-row annual EPS consistency across summed years (Codex r3 #2)
        for eps_row, styles_used in row_eps_styles.items():
            if len(styles_used) > 1:
                lbl = ms.extract_row_label(ws, eps_row)
                collector.add(
                    "summed_year_violation",
                    "fail",
                    "Single EPS/EPADS metric row mixes different annual EPS calculation methods across summed years",
                    f"{ws.title}!row{eps_row} ({lbl}): mixes {sorted(styles_used)}",
                )

    # 3. Baseline semantic regression check
    if wb_base is not None:
        _check_baseline_regression(
            wb_f,
            wb_base,
            pmaps,
            pmaps_base,
            palette,
            palette_base,
            exemptions,
            collector,
            stats,
            target_sheets=target_sheets,
        )

    # 4. Checks sheet audit (structural in static-only, full + independent recompute post-recalc)
    checks_info = _check_checks_sheet(wb_f, wb_v, xml_caches, static_only, collector, stats)

    # 5. Summary table links check
    _check_summary_table_links(wb_f, pmaps, collector)

    # 6. Financial identities (post-recalc mode only)
    if not static_only and wb_v is not None:
        _check_financial_identities(
            wb_f,
            wb_v,
            pmaps,
            checks_info,
            collector,
            target_sheets=target_sheets,
            rate_band_bps=rate_band_bps,
        )

    # 7. Script-level warning explanation check (Codex r3 #5)
    findings = collector.list_findings()
    corpus_lower, per_check_notes = _collect_explanation_corpus(wb_f, wb_v, warn_notes)

    script_warnings: list[dict[str, Any]] = []
    unexplained_warns: list[str] = []
    for f_item in findings:
        if f_item["level"] == "warn":
            chk = f_item["check"]
            explained = _is_script_warn_explained(chk, corpus_lower, per_check_notes)
            loc = _WARN_EXPLANATION_LOCATIONS.get(chk, "Checks column D or README & Changelog")
            script_warnings.append({
                "check": chk,
                "count": f_item["count"],
                "explanation_location": loc,
                "explained": explained,
            })
            if not explained:
                unexplained_warns.append(f"{chk} (explain in {loc})")

    # In --strict post-recalc mode, unexplained script warnings block delivery (exit code 1)
    if strict and not static_only and unexplained_warns:
        collector.add(
            "unexplained_script_warning",
            "fail",
            "Strict delivery requires every script-level warn finding to be explained in Checks column D or README & Changelog",
            "; ".join(unexplained_warns[:MAX_EXAMPLES]),
        )
        findings = collector.list_findings()

    has_fail = any(f["level"] == "fail" for f in findings)
    if has_fail:
        status = "fail"
        delivery_decision = (
            "block_missing_warning_explanation"
            if any(f["check"] == "unexplained_script_warning" for f in findings)
            and all(f["check"] == "unexplained_script_warning" for f in findings if f["level"] == "fail")
            else "block"
        )
    elif unexplained_warns:
        status = "pass"
        delivery_decision = "pass_with_advisory_warnings"
    else:
        status = "pass"
        delivery_decision = "pass"

    return {
        "status": status,
        "delivery_decision": delivery_decision,
        "strict": strict,
        "static_only": static_only,
        "file": str(mpath),
        "baseline": str(baseline_path) if baseline_path else None,
        "profile_source": profile_source,
        "checks_sheet_overall": stats.get("checks_sheet_overall"),
        "stats": stats,
        "script_warnings": script_warnings,
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="style_audit.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Dalton financial model static & post-recalc audit engine.\n\n"
            "Checks!Overall vs Script Warnings & Exit Codes:\n"
            "  - Checks!C10 (Overall) summarizes ONLY the 8 Checks worksheet rows (C2:C9).\n"
            "  - Script-level warn findings are reported separately in `script_warnings`\n"
            "    with their required `explanation_location` (Checks col D or README & Changelog).\n"
            "  - Exit code 1 (delivery blocked): any `fail` finding, Checks FAIL / missing D-col\n"
            "    explanation for WARN/N/A, OR in --strict post-recalc mode any script-level warn\n"
            "    without an explanation in Checks col D / README & Changelog / --warn-notes.\n"
            "  - Exit code 0 (delivery allowed): zero `fail` findings (and all script warnings\n"
            "    explained when --strict post-recalc mode is active)."
        ),
    )
    parser.add_argument("model", help="Path to .xlsx workbook to audit")
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="Run pre-recalc static checks only (skip cached error, unrecalculated, and numeric tie-out checks)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Strict delivery gate: promote core style/summary checks to fail and require explanations for script warnings",
    )
    parser.add_argument("--sheet", default=None, help="Audit a single worksheet by name (workbook checks still run)")
    parser.add_argument("--baseline", default=None, help="Baseline pre-edit .xlsx workbook for regression & style comparison")
    parser.add_argument("--profile", default=None, help="Pre-extracted style_profile.json from baseline workbook")
    parser.add_argument("--lineage", default=None, help="Path to model_lineage.json to resolve baseline/profile/exemptions")
    parser.add_argument("--mapping", default=None, help="Path to mapping.md containing registered blank-forecast/discontinued rows")
    parser.add_argument("--fiscal-year-end-month", type=int, default=None, help="Explicit fiscal year-end month (1..12)")
    parser.add_argument("--rate-band-bps", type=float, default=150.0, help="Implied interest rate historical warning band in bps (default 150)")
    parser.add_argument("--warn-notes", default=None, help="Optional JSON file mapping warning check ids to rationale strings")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    warn_notes: dict[str, str] | None = None
    if args.warn_notes:
        warn_notes = json.loads(Path(args.warn_notes).read_text(encoding="utf-8"))

    report = audit_workbook(
        args.model,
        static_only=args.static_only,
        strict=args.strict,
        sheet=args.sheet,
        baseline_path=args.baseline,
        profile_path=args.profile,
        lineage_path=args.lineage,
        mapping_path=args.mapping,
        fiscal_year_end_month=args.fiscal_year_end_month,
        rate_band_bps=args.rate_band_bps,
        warn_notes=warn_notes,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
