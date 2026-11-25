#!/usr/bin/env python3
"""Advisory Excel financial model scanner (model_scan.py).

Read-only scanner that surfaces items worth reviewing without making rigid
pass/fail judgments. The analyst / model reviews each item in context of the
company's metrics, sign conventions, and fiscal calendar: fix genuine issues,
or note intentional choices in the changelog.

Usage:
    python model_scan.py <workbook.xlsx> [--sheet "<SheetName>"] [--recalc] [--max-items 50]

Exit codes:
    0   Scan completed (even when items are found — findings never fail the run).
    1   Scanner error only (file not found, invalid arguments, unreadable workbook,
        or unknown --sheet).

Advisory categories reported:
    1. forecast_constants
       Numeric constants in columns whose header looks like a forecast period
       (e.g. 2026E, 4Q26E, 2026Q4F). Lists both amount and assumption rows neutrally.
    2. possible_period_mismatch
       Rough header-based hints where a formula references period columns whose
       header years might not match the formula's own column (always marked as
       possible — verify against the company's fiscal year-end and cross-period logic).
    3. external_or_broken_or_circular
       External workbook links, third-party plugin functions (Capital IQ, BQL,
       BDH, BDP, RTD, WEBSERVICE), broken #REF! tokens (including inside IFERROR),
       and circular references (iterate=True or dependency cycles).
    4. recalc_errors
       Excel formula error values (#DIV/0!, #REF!, #NAME?, #VALUE!, #N/A, #NUM!,
       #NULL!, #SPILL!, #CALC!, #ERROR!) found in cached values or after running
       upstream xlsx/scripts/recalc.py on a disposable copy (--recalc).
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import warnings
import zipfile

from lxml import etree
import openpyxl
from openpyxl.formula.tokenizer import Token, Tokenizer
from openpyxl.utils import column_index_from_string, get_column_letter

_SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)

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

# Rough period header patterns (intentionally lightweight & non-exhaustive)
_CAGR_DELTA_RE = re.compile(
    r"(?:\bCAGR\b|\bYoY\b|\bQoQ\b|\bMoM\b|\bvs\.?\b|\bChg\b|\bDiff\b|Δ|同比|环比)",
    re.IGNORECASE,
)
_Q_FIRST_RE = re.compile(r"^\s*Q?([1-4])Q\s*['\-/\s]?(\d{2}|\d{4})\s*([AEFP])?\s*$", re.IGNORECASE)
_Q_PREFIX_RE = re.compile(r"^\s*Q([1-4])\s*['\-/\s]?(\d{2}|\d{4})\s*([AEFP])?\s*$", re.IGNORECASE)
_Y_FIRST_Q_RE = re.compile(r"^\s*(?:FY|CY)?\s*(\d{2}|\d{4})\s*['\-/\s]?Q([1-4])\s*([AEFP])?\s*$", re.IGNORECASE)
_ANNUAL_RE = re.compile(r"^\s*(?:FY|CY)?\s*'?((?:19|20)\d{2}|\d{2})\s*([AEFP])?\s*$", re.IGNORECASE)


def _expand_year(raw: str) -> int:
    v = int(raw)
    if len(raw) == 4:
        return v
    return 2000 + v if v <= 79 else 1900 + v


def _parse_header(val: Any) -> dict[str, Any] | None:
    """Roughly parse a column header cell into period metadata, or None."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        if float(val).is_integer() and 1990 <= int(val) <= 2100:
            return {"raw": str(int(val)), "kind": "annual", "year": int(val), "quarter": None, "is_forecast": False}
        return None
    text = " ".join(str(val).strip().split())
    if not text or _CAGR_DELTA_RE.search(text):
        return None
    for rx in (_Q_FIRST_RE, _Q_PREFIX_RE):
        m = rx.match(text)
        if m:
            suf = (m.group(3) or "").upper()
            return {
                "raw": text,
                "kind": "quarter",
                "year": _expand_year(m.group(2)),
                "quarter": int(m.group(1)),
                "is_forecast": suf in ("E", "F", "P"),
            }
    m = _Y_FIRST_Q_RE.match(text)
    if m:
        suf = (m.group(3) or "").upper()
        return {
            "raw": text,
            "kind": "quarter",
            "year": _expand_year(m.group(1)),
            "quarter": int(m.group(2)),
            "is_forecast": suf in ("E", "F", "P"),
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
            }
    return None


def _row_label(ws: Any, row: int) -> str:
    parts: list[str] = []
    for c in range(1, 5):
        v = ws.cell(row, c).value
        if isinstance(v, str) and v.strip() and not v.strip().startswith("="):
            parts.append(v.strip())
    return " | ".join(parts)


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


def _has_ref_error(expr: str) -> bool:
    if not expr or "#REF!" not in expr.upper():
        return False
    try:
        tokens = Tokenizer(expr if expr.startswith("=") else "=" + expr).items
    except Exception:
        return "#REF!" in expr.upper()
    return any(
        tok.type == Token.OPERAND
        and tok.subtype in (Token.ERROR, Token.RANGE)
        and "#REF!" in tok.value.upper()
        for tok in tokens
    )


def _detect_header_columns(ws: Any) -> tuple[int | None, dict[int, dict[str, Any]]]:
    """Find the primary period header row (scanning rows 1..5) and its parsed columns."""
    best_row: int | None = None
    best_cols: dict[int, dict[str, Any]] = {}
    max_r = min(ws.max_row or 0, 5)
    max_c = ws.max_column or 0
    for r in range(1, max_r + 1):
        cols: dict[int, dict[str, Any]] = {}
        for c in range(1, max_c + 1):
            val = ws.cell(r, c).value
            if isinstance(val, str) and val.startswith("="):
                continue
            parsed = _parse_header(val)
            if parsed is not None:
                cols[c] = parsed
        if len(cols) > len(best_cols):
            best_cols = cols
            best_row = r
    if len(best_cols) < 2:
        return None, {}
    return best_row, best_cols


def _extract_same_sheet_ref_cols(formula: str, current_sheet: str) -> list[int]:
    cols: list[int] = []
    try:
        tokens = Tokenizer(formula).items
    except Exception:
        return cols
    cur_cf = current_sheet.casefold()
    for tok in tokens:
        if tok.type != Token.OPERAND or tok.subtype != Token.RANGE or "[" in tok.value:
            continue
        m_c = _CELL_REF_RE.match(tok.value)
        if m_c:
            sh = m_c.group(1).replace("''", "'") if m_c.group(1) else m_c.group(2)
            if sh is None or sh.casefold() == cur_cf:
                cols.append(column_index_from_string(m_c.group(3)))
            continue
        m_r = _RANGE_REF_RE.match(tok.value)
        if m_r:
            sh = m_r.group(1).replace("''", "'") if m_r.group(1) else m_r.group(2)
            if sh is None or sh.casefold() == cur_cf:
                c1, c2 = sorted((
                    column_index_from_string(m_r.group(3)),
                    column_index_from_string(m_r.group(5)),
                ))
                if c2 - c1 + 1 <= 24:
                    cols.extend(range(c1, c2 + 1))
    return cols


def _detect_circular_references(wb: openpyxl.Workbook) -> list[dict[str, str]]:
    """Detect iterate=True flag and formula/defined-name dependency cycles via iterative Tarjan SCC."""
    found: list[dict[str, str]] = []
    if getattr(wb.calculation, "iterate", False) is True:
        found.append({
            "sheet": "<workbook>",
            "cell": "calcPr",
            "reason": "工作簿开启了迭代计算（iterate=True），可能存在循环引用",
        })

    sheet_lookup = {ws.title.casefold(): ws.title for ws in wb.worksheets}
    raw_names: dict[tuple[str | None, str], str] = {}
    for name, dn in wb.defined_names.items():
        raw_names[(None, name.casefold())] = str(dn.value or "")
    for ws in wb.worksheets:
        for name, dn in getattr(ws, "defined_names", {}).items():
            raw_names[(ws.title.casefold(), name.casefold())] = str(dn.value or "")

    def _parse_targets(expr: str, default_sheet: str, visiting_names: frozenset[tuple[str | None, str]]) -> list[tuple[str, int, int]]:
        targets: list[tuple[str, int, int]] = []
        try:
            tokens = Tokenizer(expr if expr.startswith("=") else "=" + expr).items
        except Exception:
            return targets
        for tok in tokens:
            if tok.type != Token.OPERAND or tok.subtype != Token.RANGE or "[" in tok.value or "#REF!" in tok.value.upper():
                continue
            mc = _CELL_REF_RE.match(tok.value)
            if mc:
                sh = mc.group(1).replace("''", "'") if mc.group(1) else (mc.group(2) or default_sheet)
                targets.append((sheet_lookup.get(sh.casefold(), sh), int(mc.group(4)), column_index_from_string(mc.group(3))))
                continue
            mr = _RANGE_REF_RE.match(tok.value)
            if mr:
                sh = mr.group(1).replace("''", "'") if mr.group(1) else (mr.group(2) or default_sheet)
                c1, c2 = sorted((column_index_from_string(mr.group(3)), column_index_from_string(mr.group(5))))
                r1, r2 = sorted((int(mr.group(4)), int(mr.group(6))))
                if (c2 - c1 + 1) * (r2 - r1 + 1) <= 256:
                    sh_real = sheet_lookup.get(sh.casefold(), sh)
                    for r in range(r1, r2 + 1):
                        for c in range(c1, c2 + 1):
                            targets.append((sh_real, r, c))
                continue
            nm_cf = tok.value.casefold()
            for key in ((default_sheet.casefold(), nm_cf), (None, nm_cf)):
                if key in raw_names and key not in visiting_names:
                    targets.extend(_parse_targets(raw_names[key], default_sheet, visiting_names | {key}))
                    break
        return targets

    graph: dict[tuple[str, int, int], list[tuple[str, int, int]]] = {}
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v.startswith("="):
                    tgts = _parse_targets(v, ws.title, frozenset())
                    if tgts:
                        graph[(ws.title, cell.row, cell.column)] = tgts

    idx_cnt = 0
    indices: dict[tuple[str, int, int], int] = {}
    lowlink: dict[tuple[str, int, int], int] = {}
    on_stack: set[tuple[str, int, int]] = set()
    stack: list[tuple[str, int, int]] = []

    for start in list(graph.keys()):
        if start in indices:
            continue
        call_stack: list[tuple[tuple[str, int, int], int]] = [(start, 0)]
        indices[start] = lowlink[start] = idx_cnt
        idx_cnt += 1
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
                    indices[w] = lowlink[w] = idx_cnt
                    idx_cnt += 1
                    stack.append(w)
                    on_stack.add(w)
                    call_stack.append((w, 0))
                elif w in on_stack and indices[w] < lowlink[v]:
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
                        chain = " -> ".join(f"{s}!{get_column_letter(c)}{r}" for s, r, c in scc[:6])
                        if len(scc) > 6:
                            chain += f" -> ... ({len(scc)} 格)"
                        found.append({
                            "sheet": scc[0][0],
                            "cell": f"{get_column_letter(scc[0][2])}{scc[0][1]}",
                            "reason": f"检测到公式循环引用：{chain}",
                        })
    return found


def _locate_recalc_script(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    here = Path(__file__).resolve()
    candidates = [
        Path.cwd() / ".agents" / "skills" / "xlsx" / "scripts" / "recalc.py",
        here.parents[3] / "langalpha_deliverables" / "skills" / "xlsx" / "scripts" / "recalc.py",
        here.parents[4] / "plugins" / "langalpha_deliverables" / "skills" / "xlsx" / "scripts" / "recalc.py",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _run_recalc_on_copy(
    src_path: Path,
    recalc_script: Path,
) -> tuple[list[dict[str, Any]], str]:
    """Copy workbook to a temp dir, run recalc.py on the copy, and collect error cells."""
    errors: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="model_scan_recalc_") as td:
        copy_path = Path(td) / src_path.name
        shutil.copy2(src_path, copy_path)
        proc = subprocess.run(
            [sys.executable, str(recalc_script), str(copy_path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if not proc.stdout.strip():
            return errors, f"recalc_failed: rc={proc.returncode} {proc.stderr.strip()[:200]}"
        try:
            res = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return errors, f"recalc_unparseable_output: {proc.stdout.strip()[:200]}"

        status = res.get("status", "unknown")
        if status == "error":
            return errors, f"recalc_declined: {res.get('message', 'unknown error')}"

        for err in res.get("errors") or []:
            errors.append({
                "sheet": err.get("sheet", ""),
                "cell": err.get("cell", ""),
                "reason": f"重算后出现错误值 {err.get('error', '')}",
                "formula": str(err.get("formula", ""))[:80],
            })
        note = f"recalc_ok (engine={res.get('engine')}, status={status}, total_errors={res.get('total_errors', len(errors))})"
        return errors, note


def scan_workbook(
    xlsx_path: str | Path,
    *,
    sheet: str | None = None,
    recalc: bool = False,
    recalc_script: str | None = None,
    max_items: int = 50,
) -> dict[str, Any]:
    """Run read-only advisory scan on ``xlsx_path`` and return structured JSON dict."""
    path = Path(xlsx_path)
    if not path.is_file():
        return {"status": "error", "file": str(path), "message": f"File not found: {path}"}

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        try:
            wb_f = openpyxl.load_workbook(path, data_only=False)
            wb_v = openpyxl.load_workbook(path, data_only=True)
        except Exception as exc:
            return {"status": "error", "file": str(path), "message": f"Failed to open workbook: {exc}"}

    if sheet and sheet not in wb_f.sheetnames:
        return {"status": "error", "file": str(path), "message": f"Sheet not found: {sheet!r}"}

    target_sheets = {sheet} if sheet else None

    buckets: dict[str, list[dict[str, Any]]] = {
        "forecast_constants": [],
        "possible_period_mismatch": [],
        "external_or_broken_or_circular": [],
        "recalc_errors": [],
    }
    totals: dict[str, int] = {k: 0 for k in buckets}

    def _add(cat: str, item: dict[str, Any]) -> None:
        totals[cat] += 1
        if len(buckets[cat]) < max_items:
            buckets[cat].append(item)

    # 1. Package-level external links check
    has_external_links = False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            ext_xmls = [n for n in names if n.startswith("xl/externalLinks/") and n.endswith(".xml")]
            if ext_xmls:
                has_external_links = True
                for ep in sorted(ext_xmls):
                    _add("external_or_broken_or_circular", {
                        "sheet": "<package>",
                        "cell": ep,
                        "reason": "工作簿压缩包包含外部链接部件（重算前通常需先清理或替换）",
                    })
            if "xl/workbook.xml" in names:
                wb_root = etree.fromstring(zf.read("xl/workbook.xml"), _XML_PARSER)
                ext_refs = list(wb_root.iter(f"{{{_SML}}}externalReference"))
                if ext_refs:
                    has_external_links = True
                    _add("external_or_broken_or_circular", {
                        "sheet": "<workbook>",
                        "cell": "xl/workbook.xml",
                        "reason": f"workbook.xml 声明了 {len(ext_refs)} 个外部工作簿引用（externalReference）",
                    })
    except Exception:
        pass

    # 2. Defined names: external links & #REF!
    all_dn: list[tuple[str, str, str]] = [
        ("workbook", name, str(dn.value or "")) for name, dn in wb_f.defined_names.items()
    ]
    for ws in wb_f.worksheets:
        for name, dn in getattr(ws, "defined_names", {}).items():
            all_dn.append((ws.title, name, str(dn.value or "")))
    for scope, name, expr in all_dn:
        if _has_external_operand(expr):
            has_external_links = True
            _add("external_or_broken_or_circular", {
                "sheet": f"definedName({scope})",
                "cell": name,
                "reason": f"命名区域指向外部工作簿：{expr[:70]}",
            })
        elif _has_ref_error(expr):
            _add("external_or_broken_or_circular", {
                "sheet": f"definedName({scope})",
                "cell": name,
                "reason": f"命名区域含断裂引用 #REF!：{expr[:70]}",
            })

    # 3. Circular references
    for circ in _detect_circular_references(wb_f):
        _add("external_or_broken_or_circular", circ)

    # 4. Per-sheet scan: forecast constants, possible period mismatch, plugins/#REF!, cached errors
    sheets_scanned = 0
    plugin_counts: dict[str, int] = defaultdict(int)

    for ws in wb_f.worksheets:
        if ws.sheet_state != "visible":
            continue
        if target_sheets and ws.title not in target_sheets:
            continue
        sheets_scanned += 1
        ws_v = wb_v[ws.title]
        hdr_row, period_cols = _detect_header_columns(ws)

        for row_cells in ws.iter_rows():
            r_idx = row_cells[0].row
            if hdr_row is not None and r_idx <= hdr_row:
                continue
            lbl = _row_label(ws, r_idx)

            for cell in row_cells:
                val = cell.value
                if val is None or (isinstance(val, str) and not val.strip()):
                    continue

                coord = cell.coordinate
                pinfo = period_cols.get(cell.column)

                if isinstance(val, str) and val.startswith("="):
                    # External link in formula
                    if _has_external_operand(val):
                        has_external_links = True
                        _add("external_or_broken_or_circular", {
                            "sheet": ws.title,
                            "cell": coord,
                            "reason": "公式引用外部工作簿",
                            "formula": val[:80],
                        })
                    # Plugin functions (CIQ / BQL / BDH / BDP / RTD / WEBSERVICE)
                    m_plug = _PLUGIN_FUNC_RE.search(val)
                    if m_plug:
                        tag = m_plug.group(0).upper().replace("(", "").strip()
                        key = "CIQ" if "CIQ" in tag else ("BQL" if "BQL" in tag else ("BDH" if "BDH" in tag else tag))
                        plugin_counts[key] += 1
                        _add("external_or_broken_or_circular", {
                            "sheet": ws.title,
                            "cell": coord,
                            "reason": f"公式包含外部插件函数（{key}），在无插件环境下无法求值",
                            "formula": val[:80],
                        })
                    # Broken #REF! (including inside IFERROR/IFNA)
                    if _has_ref_error(val):
                        _add("external_or_broken_or_circular", {
                            "sheet": ws.title,
                            "cell": coord,
                            "reason": "公式包含断裂引用 #REF!（即使外层套了 IFERROR/IFNA 也需核对）",
                            "formula": val[:80],
                        })

                    # Rough possible period mismatch hint (explicitly marked as "可能")
                    if pinfo is not None and pinfo.get("year") is not None:
                        ref_cols = _extract_same_sheet_ref_cols(val, ws.title)
                        ref_pcols = [period_cols[c] for c in ref_cols if c in period_cols]
                        if pinfo["kind"] == "annual" and ref_pcols:
                            q_refs = [rp for rp in ref_pcols if rp["kind"] == "quarter"]
                            if q_refs:
                                q_years = {rp["year"] for rp in q_refs if rp["year"] is not None}
                                if pinfo["year"] not in q_years or (
                                    len(q_years) > 1 and max(q_years) - min(q_years) > 1
                                ):
                                    _add("possible_period_mismatch", {
                                        "sheet": ws.title,
                                        "cell": coord,
                                        "reason": (
                                            f"可能存在期间不一致：列头为 {pinfo['raw']}，公式引用了季度列年份 {sorted(q_years)}"
                                            "（如属非 12 月结账财年跨年加总则属正常，请核对）"
                                        ),
                                        "formula": val[:80],
                                    })
                        elif pinfo["kind"] == "quarter" and ref_pcols:
                            # Flag only if a quarter formula exclusively references a column >= 2 years away
                            other_years = {
                                rp["year"] for rp in ref_pcols
                                if rp["year"] is not None and abs(rp["year"] - pinfo["year"]) >= 2
                            }
                            same_or_adj = any(
                                rp["year"] is not None and abs(rp["year"] - pinfo["year"]) <= 1
                                for rp in ref_pcols
                            )
                            if other_years and not same_or_adj:
                                _add("possible_period_mismatch", {
                                    "sheet": ws.title,
                                    "cell": coord,
                                    "reason": (
                                        f"可能存在期间不一致：列头为 {pinfo['raw']}，公式仅引用了相距两年以上的期间列 {sorted(other_years)}"
                                        "（请核对是否为刻意引用历史基准）"
                                    ),
                                    "formula": val[:80],
                                })

                    # Existing cached error value (when --recalc is not requested)
                    if not recalc:
                        cv = ws_v.cell(r_idx, cell.column).value
                        if isinstance(cv, str) and _ERROR_RE.match(cv.strip()):
                            _add("recalc_errors", {
                                "sheet": ws.title,
                                "cell": coord,
                                "reason": f"当前缓存值为错误 {cv.strip()}",
                                "formula": val[:80],
                            })

                elif isinstance(val, (int, float)) and not isinstance(val, bool):
                    # Forecast-period numeric constant (lists BOTH amount & assumption rows neutrally!)
                    if pinfo is not None and pinfo.get("is_forecast"):
                        has_fill = bool(cell.fill and cell.fill.fill_type and cell.fill.fill_type != "none")
                        style_hint = "带底色（可能为驱动假设）" if has_fill else "无底色"
                        _add("forecast_constants", {
                            "sheet": ws.title,
                            "cell": coord,
                            "row_label": lbl,
                            "period": pinfo["raw"],
                            "value": val,
                            "reason": f"预测列 {pinfo['raw']} 出现数字常数（{style_hint}；若是驱动假设或外部输入则属正常，若是预测金额结果请改用公式）",
                        })

    # 5. Optional recalculation on disposable copy (--recalc)
    recalc_ran = False
    recalc_note = "未运行重算（仅扫描原文件现有缓存；如需重算副本请加 --recalc）"
    if recalc:
        if has_external_links:
            recalc_note = "已跳过重算：工作簿含外部链接，recalc.py 默认拒绝含外链文件，请先清理外链再重算"
        else:
            rscript = _locate_recalc_script(recalc_script)
            if rscript is None:
                recalc_note = "未找到上游 xlsx/scripts/recalc.py，已跳过重算"
            else:
                recalc_errs, recalc_note = _run_recalc_on_copy(path, rscript)
                recalc_ran = True
                for err in recalc_errs:
                    if target_sheets and err.get("sheet") not in target_sheets:
                        continue
                    _add("recalc_errors", err)

    summary_parts = [
        f"扫描完成（{sheets_scanned} 张可见工作表）：",
        f"预测期数字常数 {totals['forecast_constants']} 处",
        f"可能期间不一致 {totals['possible_period_mismatch']} 处",
        f"外链/插件/#REF!/循环引用 {totals['external_or_broken_or_circular']} 处",
        f"错误值 {totals['recalc_errors']} 处。",
        "以上均为核对提示，请结合公司指标、正负号与财年逐条判断。",
    ]

    return {
        "status": "ok",
        "file": str(path),
        "sheet_filter": sheet,
        "sheets_scanned": sheets_scanned,
        "recalc_ran": recalc_ran,
        "recalc_note": recalc_note,
        "plugin_formula_counts": dict(sorted(plugin_counts.items())),
        "summary": " ".join(summary_parts),
        "counts": totals,
        "truncated": {k: totals[k] > len(buckets[k]) for k in buckets},
        "items": buckets,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="model_scan.py",
        description=(
            "Read-only advisory scanner for Excel financial models. "
            "Lists items worth reviewing without making pass/fail verdicts. "
            "Exits 0 whenever the scan completes, and 1 only on scanner/file errors."
        ),
    )
    parser.add_argument("workbook", help="Path to .xlsx workbook (read-only)")
    parser.add_argument("--sheet", default=None, help="Optional worksheet name to limit cell scan")
    parser.add_argument(
        "--recalc",
        action="store_true",
        help="Copy workbook to a temporary file and run upstream xlsx/scripts/recalc.py to check error values",
    )
    parser.add_argument("--recalc-script", default=None, help="Explicit path to xlsx/scripts/recalc.py")
    parser.add_argument("--max-items", type=int, default=50, help="Max listed items per category (default 50)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    report = scan_workbook(
        args.workbook,
        sheet=args.sheet,
        recalc=args.recalc,
        recalc_script=args.recalc_script,
        max_items=args.max_items,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
