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
       (e.g. 2026E, 4Q26E, 2026Q4F). Grouped by worksheet, with unstyled
       amount constants prioritized ahead of filled assumption cells.
    2. possible_period_mismatch
       Rough header-based hints where a formula references period columns whose
       header years might not match the formula's own column (always marked as
       possible — verify against the company's fiscal year-end and cross-period logic).
    3. external_or_broken_or_circular
       External workbook links, broken defined names, third-party plugin functions
       (Capital IQ, BQL, BDH, BDP, RTD, WEBSERVICE), broken #REF! tokens
       (including inside IFERROR), and circular references (iterate=True or
       dependency cycles). Subtypes are summarized and interleaved fairly so
       external parts, #REF!, circular references, and plugins all remain visible
       within ``max_items``.
    4. recalc_errors
       Excel formula error values (#DIV/0!, #REF!, #NAME?, #VALUE!, #N/A, #NUM!,
       #NULL!, #SPILL!, #CALC!, #ERROR!) summarized per (sheet, error_code) and
       interleaved across error codes (non-#NAME? prioritized). Distinguishes
       complete vs partial (``incomplete``) recalc, preserves upstream total
       error counts / error_summary / truncation, and retains cached errors
       whenever recalc is partial, skipped, fails, or times out.
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
from openpyxl.worksheet.formula import ArrayFormula

_SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)

_ERROR_RE = re.compile(r"^#(?:N/A|[A-Z0-9/]+[!?])$")
_EXCEL_STR_RE = re.compile(r'"(?:""|[^"])*"')
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

_EXAMPLE_LIMIT = 5


def _strip_excel_string_literals(expr: str) -> str:
    """Replace double-quoted Excel string literals with empty quotes."""
    return _EXCEL_STR_RE.sub('""', expr)


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


def _get_stored_cell_value(ws: Any, row: int, col: int) -> Any:
    raw = getattr(ws, "_cells", None)
    if isinstance(raw, dict):
        cell = raw.get((row, col))
        return cell.value if cell is not None else None
    return ws.cell(row, col).value


def _extract_formula_text(val: Any) -> str | None:
    """Return formula string starting with '=' for string or ArrayFormula values."""
    if isinstance(val, str) and val.startswith("="):
        return val
    if isinstance(val, ArrayFormula) and isinstance(val.text, str) and val.text.strip():
        t = val.text.strip()
        return t if t.startswith("=") else "=" + t
    return None


def _detect_plugin_function(formula: str) -> str | None:
    """Detect third-party plugin function calls using formula tokens (ignores string literals)."""
    try:
        tokens = Tokenizer(formula).items
    except Exception:
        cleaned = _strip_excel_string_literals(formula)
        m_plug = _PLUGIN_FUNC_RE.search(cleaned)
        if not m_plug:
            return None
        tag = m_plug.group(0).upper().replace("(", "").strip()
        if "CIQ" in tag:
            return "CIQ"
        if "BQL" in tag:
            return "BQL"
        if "BDH" in tag:
            return "BDH"
        return tag

    for tok in tokens:
        if tok.type == Token.FUNC and tok.subtype == Token.OPEN:
            m_plug = _PLUGIN_FUNC_RE.search(tok.value)
            if m_plug:
                tag = m_plug.group(0).upper().replace("(", "").strip()
                if "CIQ" in tag:
                    return "CIQ"
                if "BQL" in tag:
                    return "BQL"
                if "BDH" in tag:
                    return "BDH"
                return tag
    return None


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
        v = _get_stored_cell_value(ws, row, c)
        if isinstance(v, str) and v.strip() and not v.strip().startswith("="):
            parts.append(v.strip())
    return " | ".join(parts)


def _has_external_operand(expr: str) -> bool:
    if not expr or "[" not in expr:
        return False
    try:
        tokens = Tokenizer(expr if expr.startswith("=") else "=" + expr).items
    except Exception:
        cleaned = _strip_excel_string_literals(expr)
        return "[" in cleaned and "!" in cleaned
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
        cleaned = _strip_excel_string_literals(expr)
        return "#REF!" in cleaned.upper()
    return any(
        tok.type == Token.OPERAND
        and tok.subtype in (Token.ERROR, Token.RANGE)
        and "#REF!" in tok.value.upper()
        for tok in tokens
    )


def _detect_header_columns(ws: Any) -> tuple[int | None, dict[int, dict[str, Any]]]:
    """Find the primary period header row (scanning stored cells in rows 1..5)."""
    by_row: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
    for r, c, cell in _iter_stored_cells(ws):
        if r > 5:
            break
        val = cell.value
        if _extract_formula_text(val) is not None:
            continue
        parsed = _parse_header(val)
        if parsed is not None:
            by_row[r][c] = parsed
    best_row: int | None = None
    best_cols: dict[int, dict[str, Any]] = {}
    for r in sorted(by_row):
        cols = by_row[r]
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
            "kind": "circular_reference",
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
        for r, c, cell in _iter_stored_cells(ws):
            expr = _extract_formula_text(cell.value)
            if expr is not None:
                tgts = _parse_targets(expr, ws.title, frozenset())
                if tgts:
                    graph[(ws.title, r, c)] = tgts

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
                            "kind": "circular_reference",
                            "reason": f"检测到公式循环引用：{chain}",
                        })
    return found


def _locate_recalc_script(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    here = Path(__file__).resolve()
    candidates: list[Path] = [
        Path.cwd() / ".agents" / "skills" / "xlsx" / "scripts" / "recalc.py",
        Path.cwd() / "plugins" / "langalpha_deliverables" / "skills" / "xlsx" / "scripts" / "recalc.py",
    ]
    if len(here.parents) > 2:
        candidates.append(here.parents[2] / "xlsx" / "scripts" / "recalc.py")
    if len(here.parents) > 4:
        candidates.append(here.parents[4] / "langalpha_deliverables" / "skills" / "xlsx" / "scripts" / "recalc.py")
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _run_recalc_on_copy(
    src_path: Path,
    recalc_script: Path,
    timeout: int = 120,
) -> tuple[list[dict[str, Any]], str, str, dict[str, Any]]:
    """Copy workbook to a temp dir, run recalc.py on the copy, and collect error cells.

    Returns ``(errors, note, recalc_state, meta)`` where ``recalc_state`` is one of
    ``"complete"``, ``"incomplete"``, or ``"failed"``.
    """
    errors: list[dict[str, Any]] = []
    empty_meta: dict[str, Any] = {
        "total_errors": 0,
        "error_summary": {},
        "upstream_truncated": False,
        "unmatched": 0,
    }
    with tempfile.TemporaryDirectory(prefix="model_scan_recalc_") as td:
        copy_path = Path(td) / src_path.name
        shutil.copy2(src_path, copy_path)
        try:
            proc = subprocess.run(
                [sys.executable, str(recalc_script), str(copy_path)],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return (
                errors,
                f"recalc_timeout: recalc.py 超时（{timeout}s），已回退报告原文件现有缓存错误",
                "failed",
                empty_meta,
            )
        except OSError as exc:
            return (
                errors,
                f"recalc_failed: 无法启动 recalc.py（{exc}），已回退报告原文件现有缓存错误",
                "failed",
                empty_meta,
            )

        if not proc.stdout.strip():
            return (
                errors,
                f"recalc_failed: rc={proc.returncode} {proc.stderr.strip()[:200]}（已回退报告现有缓存错误）",
                "failed",
                empty_meta,
            )
        try:
            res = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return (
                errors,
                f"recalc_unparseable_output: {proc.stdout.strip()[:200]}（已回退报告现有缓存错误）",
                "failed",
                empty_meta,
            )

        status = str(res.get("status", "unknown"))
        if status == "error":
            return (
                errors,
                f"recalc_declined: {res.get('message', 'unknown error')}（已回退报告现有缓存错误）",
                "failed",
                empty_meta,
            )

        for err in res.get("errors") or []:
            err_code = str(err.get("error", "")).strip() or "#ERROR!"
            errors.append({
                "sheet": err.get("sheet", ""),
                "cell": err.get("cell", ""),
                "error": err_code,
                "reason": f"重算后出现错误值 {err_code}",
                "formula": str(err.get("formula", ""))[:80],
            })

        raw_summary = res.get("error_summary")
        err_summary = {
            str(k).strip(): int(v)
            for k, v in (raw_summary.items() if isinstance(raw_summary, dict) else ())
            if int(v) > 0
        }
        total_errors = int(res.get("total_errors", len(errors)) or 0)
        unmatched = int(res.get("unmatched", 0) or 0)
        upstream_truncated = bool(res.get("truncated")) or total_errors > len(errors)
        meta = {
            "status": status,
            "engine": res.get("engine"),
            "total_errors": total_errors,
            "error_summary": err_summary,
            "upstream_truncated": upstream_truncated,
            "unmatched": unmatched,
        }

        if status == "incomplete":
            note = (
                f"recalc_partial (engine={res.get('engine')}, status=incomplete, "
                f"unmatched={unmatched}, total_errors={total_errors})："
                "部分公式未能重算，已合并保留原文件现有缓存错误提示"
            )
            return errors, note, "incomplete", meta

        note = (
            f"recalc_ok (engine={res.get('engine')}, status={status}, "
            f"total_errors={total_errors}"
            + (f", upstream_truncated=true, listed={len(errors)}" if upstream_truncated else "")
            + ")"
        )
        return errors, note, "complete", meta


def _round_robin_pick(
    groups: list[list[dict[str, Any]]],
    limit: int,
) -> list[dict[str, Any]]:
    """Fairly pick up to ``limit`` items across groups in round-robin order."""
    if limit <= 0:
        return []
    picked: list[dict[str, Any]] = []
    idx = 0
    while len(picked) < limit:
        progressed = False
        for grp in groups:
            if idx < len(grp):
                picked.append(grp[idx])
                progressed = True
                if len(picked) >= limit:
                    break
        if not progressed:
            break
        idx += 1
    return picked


def _summarize_recalc_errors(
    raw_errors: list[dict[str, Any]],
    *,
    sheet_order: dict[str, int],
    max_items: int,
    source_label: str,
    upstream_error_summary: dict[str, int] | None = None,
    upstream_truncated: bool = False,
) -> tuple[list[dict[str, Any]], bool, dict[str, int]]:
    """Summarize errors per (sheet, error_code) and interleave across error codes.

    Non-``#NAME?`` error codes are prioritized ahead of ``#NAME?``. Any error code
    present in ``upstream_error_summary`` whose detail cells were cut off by
    upstream truncation is preserved as an explicit summary row so no error type
    is silently dropped.
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    listed_code_counts: dict[str, int] = defaultdict(int)
    for err in raw_errors:
        sh = str(err.get("sheet", ""))
        code = str(err.get("error", "")).strip() or "#ERROR!"
        grouped[(sh, code)].append(err)
        listed_code_counts[code] += 1

    # Merge upstream error_summary counts so totals per error code stay exact
    code_counts: dict[str, int] = dict(listed_code_counts)
    unlisted_code_rows: dict[str, dict[str, Any]] = {}
    if upstream_error_summary:
        for code, u_cnt in upstream_error_summary.items():
            listed = listed_code_counts.get(code, 0)
            code_counts[code] = max(listed, u_cnt)
            if listed == 0 and u_cnt > 0:
                unlisted_code_rows[code] = {
                    "sheet": "<recalc_summary>",
                    "cell": f"上游汇总 {u_cnt} 处（明细截断未返回坐标）",
                    "error": code,
                    "count": u_cnt,
                    "reason": (
                        f"上游重算汇总报告 {u_cnt} 处 {code} 错误"
                        f"（上游仅返回前 {len(raw_errors)} 条明细，该错误类型坐标被截断）"
                    ),
                }

    # Build per-error-code lists (each list sorted by sheet order), then round-robin across codes
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (sh, code) in sorted(
        grouped.keys(),
        key=lambda kv: (sheet_order.get(kv[0], 10**6), kv[0], kv[1]),
    ):
        rows = grouped[(sh, code)]
        if len(rows) == 1:
            by_code[code].append(dict(rows[0]))
        else:
            ex_cells = [r["cell"] for r in rows[:_EXAMPLE_LIMIT]]
            by_code[code].append({
                "sheet": sh,
                "cell": f"{ex_cells[0]} 等 {len(rows)} 格",
                "error": code,
                "count": len(rows),
                "examples": ex_cells,
                "reason": f"该表共 {len(rows)} 处{source_label}错误值 {code}，代表单元格：{', '.join(ex_cells)}",
                "formula": rows[0].get("formula", ""),
            })
    for code, row in unlisted_code_rows.items():
        by_code[code].append(row)

    ordered_codes = sorted(
        by_code.keys(),
        key=lambda c: (1 if c.upper() == "#NAME?" else 0, c),
    )
    code_groups = [by_code[c] for c in ordered_codes]
    total_groups = sum(len(g) for g in code_groups)
    picked = _round_robin_pick(code_groups, max_items)
    # Sort selected items cleanly: non-#NAME? first, then sheet order
    picked.sort(
        key=lambda it: (
            1 if str(it.get("error", "")).upper() == "#NAME?" else 0,
            sheet_order.get(str(it.get("sheet", "")), 10**6),
            str(it.get("sheet", "")),
            str(it.get("error", "")),
        )
    )
    truncated = upstream_truncated or (total_groups > len(picked))
    return picked, truncated, dict(sorted(code_counts.items()))


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
    sheet_order = {name: i for i, name in enumerate(wb_f.sheetnames)}

    ext_summary_items: list[dict[str, Any]] = []
    ext_raw_count = 0
    ext_parts_count = 0
    dn_ext_count = 0
    dn_ref_count = 0

    # 1. Package-level external links check (summarized into 1 entry for parts)
    has_external_links = False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            ext_xmls = sorted(n for n in names if n.startswith("xl/externalLinks/") and n.endswith(".xml"))
            if ext_xmls:
                has_external_links = True
                ext_parts_count = len(ext_xmls)
                ext_raw_count += len(ext_xmls)
                ex_parts = ext_xmls[:_EXAMPLE_LIMIT]
                ext_summary_items.append({
                    "sheet": "<package>",
                    "cell": ex_parts[0] if len(ext_xmls) == 1 else f"{ex_parts[0]} 等共 {len(ext_xmls)} 个",
                    "kind": "external_link_parts",
                    "count": len(ext_xmls),
                    "examples": ex_parts,
                    "reason": f"工作簿压缩包包含外部链接部件 {len(ext_xmls)} 个（重算前通常需先清理或替换）",
                })
            if "xl/workbook.xml" in names:
                wb_root = etree.fromstring(zf.read("xl/workbook.xml"), _XML_PARSER)
                ext_refs = list(wb_root.iter(f"{{{_SML}}}externalReference"))
                if ext_refs:
                    has_external_links = True
                    ext_raw_count += 1
                    ext_summary_items.append({
                        "sheet": "<workbook>",
                        "cell": "xl/workbook.xml",
                        "kind": "workbook_external_references",
                        "count": len(ext_refs),
                        "reason": f"workbook.xml 声明了 {len(ext_refs)} 个外部工作簿引用（externalReference）",
                    })
    except Exception:
        pass

    # 2. Defined names: external links & #REF! (summarized with count + examples)
    all_dn: list[tuple[str, str, str]] = [
        ("workbook", name, str(dn.value or "")) for name, dn in wb_f.defined_names.items()
    ]
    for ws in wb_f.worksheets:
        for name, dn in getattr(ws, "defined_names", {}).items():
            all_dn.append((ws.title, name, str(dn.value or "")))

    dn_ext_list: list[tuple[str, str, str]] = []
    dn_ref_list: list[tuple[str, str, str]] = []
    for scope, name, expr in all_dn:
        if _has_external_operand(expr):
            has_external_links = True
            dn_ext_list.append((scope, name, expr))
        elif _has_ref_error(expr):
            dn_ref_list.append((scope, name, expr))

    if dn_ext_list:
        dn_ext_count = len(dn_ext_list)
        ext_raw_count += dn_ext_count
        if dn_ext_count == 1:
            sc, nm, ex = dn_ext_list[0]
            ext_summary_items.append({
                "sheet": f"definedName({sc})",
                "cell": nm,
                "kind": "defined_name_external",
                "count": 1,
                "reason": f"命名区域指向外部工作簿：{ex[:70]}",
            })
        else:
            ex_names = [f"{sc}:{nm}" for sc, nm, _ in dn_ext_list[:_EXAMPLE_LIMIT]]
            ext_summary_items.append({
                "sheet": "<definedNames>",
                "cell": f"{dn_ext_list[0][1]} 等 {dn_ext_count} 个",
                "kind": "defined_name_external",
                "count": dn_ext_count,
                "examples": ex_names,
                "reason": f"共 {dn_ext_count} 个命名区域指向外部工作簿，代表项：{', '.join(ex_names)}",
            })

    if dn_ref_list:
        dn_ref_count = len(dn_ref_list)
        ext_raw_count += dn_ref_count
        if dn_ref_count == 1:
            sc, nm, ex = dn_ref_list[0]
            ext_summary_items.append({
                "sheet": f"definedName({sc})",
                "cell": nm,
                "kind": "defined_name_ref",
                "count": 1,
                "reason": f"命名区域含断裂引用 #REF!：{ex[:70]}",
            })
        else:
            ex_names = [f"{sc}:{nm}" for sc, nm, _ in dn_ref_list[:_EXAMPLE_LIMIT]]
            ext_summary_items.append({
                "sheet": "<definedNames>",
                "cell": f"{dn_ref_list[0][1]} 等 {dn_ref_count} 个",
                "kind": "defined_name_ref",
                "count": dn_ref_count,
                "examples": ex_names,
                "reason": f"共 {dn_ref_count} 个命名区域含断裂引用 #REF!，代表项：{', '.join(ex_names)}",
            })

    # 3. Circular references
    circular_items = _detect_circular_references(wb_f)
    ext_raw_count += len(circular_items)

    # 4. Per-sheet scan: forecast constants, possible period mismatch, plugins/#REF!, cached errors
    sheets_scanned = 0
    hidden_sheets_scanned: list[str] = []
    sheets_without_period_header: list[str] = []
    plugin_counts: dict[str, int] = defaultdict(int)

    fc_by_sheet_unstyled: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fc_by_sheet_filled: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fc_summary_by_sheet: dict[str, dict[str, int]] = {}
    fc_raw_count = 0

    pm_by_sheet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pm_raw_count = 0

    formula_ref_items: list[dict[str, Any]] = []
    formula_ext_by_sheet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    plugin_by_sheet_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    cached_error_rows: list[dict[str, Any]] = []

    for ws in wb_f.worksheets:
        if target_sheets is not None:
            if ws.title not in target_sheets:
                continue
            if ws.sheet_state != "visible":
                hidden_sheets_scanned.append(ws.title)
        elif ws.sheet_state != "visible":
            continue

        sheets_scanned += 1
        ws_v = wb_v[ws.title]
        hdr_row, period_cols = _detect_header_columns(ws)

        stored = _iter_stored_cells(ws)
        non_empty_cells = 0
        row_label_cache: dict[int, str] = {}

        for r_idx, c_idx, cell in stored:
            val = cell.value
            if val is None or (isinstance(val, str) and not val.strip()):
                continue
            non_empty_cells += 1

            coord = cell.coordinate
            pinfo = period_cols.get(c_idx)
            below_header = hdr_row is not None and r_idx > hdr_row

            expr = _extract_formula_text(val)
            if expr is not None:
                # External link in formula (always checked, even on/above header row)
                if _has_external_operand(expr):
                    has_external_links = True
                    ext_raw_count += 1
                    formula_ext_by_sheet[ws.title].append({
                        "sheet": ws.title,
                        "cell": coord,
                        "kind": "formula_external_link",
                        "reason": "公式引用外部工作簿",
                        "formula": expr[:80],
                    })

                # Plugin functions via Tokenizer (always checked, ignores string literals)
                plug_key = _detect_plugin_function(expr)
                if plug_key is not None:
                    plugin_counts[plug_key] += 1
                    ext_raw_count += 1
                    plugin_by_sheet_key[(ws.title, plug_key)].append({
                        "sheet": ws.title,
                        "cell": coord,
                        "kind": "plugin_formula",
                        "plugin": plug_key,
                        "reason": f"公式包含外部插件函数（{plug_key}），在无插件环境下无法求值",
                        "formula": expr[:80],
                    })

                # Broken #REF! including inside IFERROR/IFNA (always checked)
                if _has_ref_error(expr):
                    ext_raw_count += 1
                    formula_ref_items.append({
                        "sheet": ws.title,
                        "cell": coord,
                        "kind": "formula_ref_error",
                        "reason": "公式包含断裂引用 #REF!（即使外层套了 IFERROR/IFNA 也需核对）",
                        "formula": expr[:80],
                    })

                # Rough possible period mismatch hint (only below detected period header)
                if below_header and pinfo is not None and pinfo.get("year") is not None:
                    ref_cols = _extract_same_sheet_ref_cols(expr, ws.title)
                    ref_pcols = [period_cols[c] for c in ref_cols if c in period_cols]
                    if pinfo["kind"] == "annual" and ref_pcols:
                        q_refs = [rp for rp in ref_pcols if rp["kind"] == "quarter"]
                        if q_refs:
                            q_years = {rp["year"] for rp in q_refs if rp["year"] is not None}
                            if pinfo["year"] not in q_years or (
                                len(q_years) > 1 and max(q_years) - min(q_years) > 1
                            ):
                                pm_raw_count += 1
                                pm_by_sheet[ws.title].append({
                                    "sheet": ws.title,
                                    "cell": coord,
                                    "reason": (
                                        f"可能存在期间不一致：列头为 {pinfo['raw']}，公式引用了季度列年份 {sorted(q_years)}"
                                        "（如属非 12 月结账财年跨年加总则属正常，请核对）"
                                    ),
                                    "formula": expr[:80],
                                })
                    elif pinfo["kind"] == "quarter" and ref_pcols:
                        other_years = {
                            rp["year"] for rp in ref_pcols
                            if rp["year"] is not None and abs(rp["year"] - pinfo["year"]) >= 2
                        }
                        same_or_adj = any(
                            rp["year"] is not None and abs(rp["year"] - pinfo["year"]) <= 1
                            for rp in ref_pcols
                        )
                        if other_years and not same_or_adj:
                            pm_raw_count += 1
                            pm_by_sheet[ws.title].append({
                                "sheet": ws.title,
                                "cell": coord,
                                "reason": (
                                    f"可能存在期间不一致：列头为 {pinfo['raw']}，公式仅引用了相距两年以上的期间列 {sorted(other_years)}"
                                    "（请核对是否为刻意引用历史基准）"
                                ),
                                "formula": expr[:80],
                            })

                # Always collect existing cached error value (used unless complete --recalc succeeds)
                cv = _get_stored_cell_value(ws_v, r_idx, c_idx)
                if isinstance(cv, str) and _ERROR_RE.match(cv.strip()):
                    err_code = cv.strip()
                    cached_error_rows.append({
                        "sheet": ws.title,
                        "cell": coord,
                        "error": err_code,
                        "reason": f"当前缓存值为错误 {err_code}",
                        "formula": expr[:80],
                    })

            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                if below_header and pinfo is not None and pinfo.get("is_forecast"):
                    if r_idx not in row_label_cache:
                        row_label_cache[r_idx] = _row_label(ws, r_idx)
                    lbl = row_label_cache[r_idx]
                    has_fill = bool(cell.fill and cell.fill.fill_type and cell.fill.fill_type != "none")
                    style_hint = "带底色（可能为驱动假设）" if has_fill else "无底色（优先核对是否为金额常数）"
                    entry = {
                        "sheet": ws.title,
                        "cell": coord,
                        "row_label": lbl,
                        "period": pinfo["raw"],
                        "value": val,
                        "has_fill": has_fill,
                        "reason": f"预测列 {pinfo['raw']} 出现数字常数（{style_hint}；若是驱动假设或外部输入则属正常，若是预测金额结果请改用公式）",
                    }
                    fc_raw_count += 1
                    if has_fill:
                        fc_by_sheet_filled[ws.title].append(entry)
                    else:
                        fc_by_sheet_unstyled[ws.title].append(entry)

            elif isinstance(val, str) and _ERROR_RE.match(val.strip()):
                err_code = val.strip()
                cached_error_rows.append({
                    "sheet": ws.title,
                    "cell": coord,
                    "error": err_code,
                    "reason": f"单元格值为错误 {err_code}",
                    "formula": "",
                })

        # Any non-empty visible sheet without a period header in rows 1..5 is listed for review
        if hdr_row is None and ws.sheet_state == "visible" and non_empty_cells > 0:
            sheets_without_period_header.append(ws.title)

        n_un = len(fc_by_sheet_unstyled.get(ws.title, []))
        n_fi = len(fc_by_sheet_filled.get(ws.title, []))
        if n_un + n_fi > 0:
            fc_summary_by_sheet[ws.title] = {
                "unstyled": n_un,
                "filled": n_fi,
                "total": n_un + n_fi,
            }

    # Assemble forecast_constants: prioritize unstyled across sheets, present grouped by sheet
    fc_sheets = [sh for sh in wb_f.sheetnames if sh in fc_summary_by_sheet]
    picked_unstyled = _round_robin_pick(
        [fc_by_sheet_unstyled[sh] for sh in fc_sheets if fc_by_sheet_unstyled.get(sh)],
        max_items,
    )
    remaining_fc = max(0, max_items - len(picked_unstyled))
    picked_filled = _round_robin_pick(
        [fc_by_sheet_filled[sh] for sh in fc_sheets if fc_by_sheet_filled.get(sh)],
        remaining_fc,
    )
    picked_by_sheet_un: dict[str, list[dict[str, Any]]] = defaultdict(list)
    picked_by_sheet_fi: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in picked_unstyled:
        picked_by_sheet_un[it["sheet"]].append(it)
    for it in picked_filled:
        picked_by_sheet_fi[it["sheet"]].append(it)

    fc_items: list[dict[str, Any]] = []
    for sh in fc_sheets:
        fc_items.extend(picked_by_sheet_un.get(sh, []))
        fc_items.extend(picked_by_sheet_fi.get(sh, []))
    fc_truncated = fc_raw_count > len(fc_items)

    # Assemble possible_period_mismatch fairly across sheets, grouped by sheet
    pm_sheets = [sh for sh in wb_f.sheetnames if pm_by_sheet.get(sh)]
    pm_picked = _round_robin_pick([pm_by_sheet[sh] for sh in pm_sheets], max_items)
    pm_picked.sort(key=lambda it: sheet_order.get(it["sheet"], 10**6))
    pm_truncated = pm_raw_count > len(pm_picked)

    # Assemble external_or_broken_or_circular via round-robin across distinct subtypes
    # so no single subtype (e.g. 50 circular refs) can crowd out external parts, #REF!, or plugins
    formula_ext_summarized: list[dict[str, Any]] = []
    for sh in wb_f.sheetnames:
        rows = formula_ext_by_sheet.get(sh)
        if not rows:
            continue
        if len(rows) == 1:
            formula_ext_summarized.append(rows[0])
        else:
            ex_cells = [r["cell"] for r in rows[:_EXAMPLE_LIMIT]]
            formula_ext_summarized.append({
                "sheet": sh,
                "cell": f"{ex_cells[0]} 等 {len(rows)} 格",
                "kind": "formula_external_link",
                "count": len(rows),
                "examples": ex_cells,
                "reason": f"该表共 {len(rows)} 个公式引用外部工作簿，代表单元格：{', '.join(ex_cells)}",
                "formula": rows[0].get("formula", ""),
            })

    plugin_summarized: list[dict[str, Any]] = []
    for (sh, pkey), rows in sorted(
        plugin_by_sheet_key.items(),
        key=lambda kv: (sheet_order.get(kv[0][0], 10**6), kv[0][1]),
    ):
        if len(rows) == 1:
            plugin_summarized.append(rows[0])
        else:
            ex_cells = [r["cell"] for r in rows[:_EXAMPLE_LIMIT]]
            plugin_summarized.append({
                "sheet": sh,
                "cell": f"{ex_cells[0]} 等 {len(rows)} 格",
                "kind": "plugin_formula",
                "plugin": pkey,
                "count": len(rows),
                "examples": ex_cells,
                "reason": f"该表共 {len(rows)} 个外部插件函数（{pkey}），代表单元格：{', '.join(ex_cells)}",
                "formula": rows[0].get("formula", ""),
            })

    formula_ref_display: list[dict[str, Any]] = []
    ref_by_sheet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in formula_ref_items:
        ref_by_sheet[it["sheet"]].append(it)
    for sh in wb_f.sheetnames:
        rows = ref_by_sheet.get(sh)
        if not rows:
            continue
        if len(rows) <= _EXAMPLE_LIMIT:
            formula_ref_display.extend(rows)
        else:
            formula_ref_display.extend(rows[:_EXAMPLE_LIMIT])
            rest_cells = [r["cell"] for r in rows[_EXAMPLE_LIMIT : _EXAMPLE_LIMIT + 3]]
            formula_ref_display.append({
                "sheet": sh,
                "cell": f"{rest_cells[0]} 等其余 {len(rows) - _EXAMPLE_LIMIT} 格",
                "kind": "formula_ref_error_more",
                "count": len(rows) - _EXAMPLE_LIMIT,
                "examples": rest_cells,
                "reason": f"该表另有 {len(rows) - _EXAMPLE_LIMIT} 个公式包含断裂引用 #REF!",
            })

    subtype_groups = [
        grp
        for grp in (
            ext_summary_items,
            formula_ref_display,
            circular_items,
            formula_ext_summarized,
            plugin_summarized,
        )
        if grp
    ]
    total_ext_display = sum(len(g) for g in subtype_groups)
    ext_items = _round_robin_pick(subtype_groups, max_items)
    _kind_order = {
        "circular_reference": 0,
        "formula_ref_error": 1,
        "formula_ref_error_more": 2,
        "external_link_parts": 3,
        "workbook_external_references": 4,
        "defined_name_external": 5,
        "defined_name_ref": 6,
        "formula_external_link": 7,
        "plugin_formula": 8,
    }
    ext_items.sort(
        key=lambda it: (
            _kind_order.get(str(it.get("kind", "")), 99),
            sheet_order.get(str(it.get("sheet", "")), 10**6),
        )
    )
    ext_truncated = total_ext_display > len(ext_items)

    external_breakdown = {
        "external_link_parts": ext_parts_count,
        "defined_name_external": dn_ext_count,
        "defined_name_ref": dn_ref_count,
        "circular_references": len(circular_items),
        "formula_ref_errors": len(formula_ref_items),
        "formula_external_links": sum(len(v) for v in formula_ext_by_sheet.values()),
        "plugin_formulas": sum(plugin_counts.values()),
    }

    # 5. Optional recalculation on disposable copy (--recalc), distinguishing complete vs partial
    recalc_ran = False
    recalc_complete = False
    recalc_status = "not_requested"
    recalc_note = "未运行重算（仅扫描原文件现有缓存；如需重算副本请加 --recalc）"
    raw_error_rows = cached_error_rows
    error_source_label = "缓存"
    upstream_error_summary: dict[str, int] | None = None
    upstream_truncated = False
    reported_total_errors = len(cached_error_rows)

    if recalc:
        if has_external_links:
            recalc_status = "skipped_external_links"
            recalc_note = "已跳过重算：工作簿含外部链接，recalc.py 默认拒绝含外链文件，已回退报告原文件现有缓存错误"
        else:
            rscript = _locate_recalc_script(recalc_script)
            if rscript is None:
                recalc_status = "skipped_no_script"
                recalc_note = "未找到 recalc.py，已跳过重算并回退报告原文件现有缓存错误"
            else:
                recalc_errs, recalc_note, recalc_state, recalc_meta = _run_recalc_on_copy(path, rscript)
                recalc_status = recalc_state
                if recalc_state == "complete":
                    recalc_ran = True
                    recalc_complete = True
                    error_source_label = "重算"
                    filtered_recalc = [
                        err for err in recalc_errs
                        if not target_sheets or err.get("sheet") in target_sheets
                    ]
                    raw_error_rows = filtered_recalc
                    if target_sheets is None:
                        upstream_error_summary = recalc_meta.get("error_summary") or None
                        upstream_truncated = bool(recalc_meta.get("upstream_truncated"))
                        reported_total_errors = max(
                            len(filtered_recalc),
                            int(recalc_meta.get("total_errors", 0) or 0),
                        )
                    else:
                        reported_total_errors = len(filtered_recalc)
                elif recalc_state == "incomplete":
                    recalc_ran = True
                    recalc_complete = False
                    error_source_label = "部分重算+缓存"
                    filtered_recalc = [
                        err for err in recalc_errs
                        if not target_sheets or err.get("sheet") in target_sheets
                    ]
                    merged: list[dict[str, Any]] = list(filtered_recalc)
                    seen_keys = {
                        (str(e.get("sheet", "")), str(e.get("cell", "")), str(e.get("error", "")))
                        for e in merged
                    }
                    for ce in cached_error_rows:
                        k = (str(ce.get("sheet", "")), str(ce.get("cell", "")), str(ce.get("error", "")))
                        if k not in seen_keys:
                            merged.append(ce)
                            seen_keys.add(k)
                    raw_error_rows = merged
                    if target_sheets is None:
                        upstream_error_summary = recalc_meta.get("error_summary") or None
                        upstream_truncated = bool(recalc_meta.get("upstream_truncated"))
                        reported_total_errors = max(
                            len(merged),
                            int(recalc_meta.get("total_errors", 0) or 0),
                        )
                    else:
                        reported_total_errors = len(merged)

    err_items, err_truncated, error_code_counts = _summarize_recalc_errors(
        raw_error_rows,
        sheet_order=sheet_order,
        max_items=max_items,
        source_label=error_source_label,
        upstream_error_summary=upstream_error_summary,
        upstream_truncated=upstream_truncated,
    )
    reported_total_errors = max(reported_total_errors, sum(error_code_counts.values()))

    totals = {
        "forecast_constants": fc_raw_count,
        "possible_period_mismatch": pm_raw_count,
        "external_or_broken_or_circular": ext_raw_count,
        "recalc_errors": reported_total_errors,
    }

    summary_parts = [
        f"扫描完成（共 {sheets_scanned} 张工作表）：",
        f"预测期数字常数 {totals['forecast_constants']} 处（无底色 {sum(v['unstyled'] for v in fc_summary_by_sheet.values())} 处）",
        f"可能期间不一致 {totals['possible_period_mismatch']} 处",
        f"外链/插件/#REF!/循环引用 {totals['external_or_broken_or_circular']} 处",
        f"{error_source_label}错误值 {totals['recalc_errors']} 处。",
    ]
    if recalc_status == "incomplete":
        summary_parts.append("注意：本次为部分重算（incomplete），已同时保留原文件现有缓存错误。")
    if hidden_sheets_scanned:
        summary_parts.append(f"显式指定扫描的隐藏工作表：{', '.join(hidden_sheets_scanned)}。")
    if sheets_without_period_header:
        shown = ", ".join(sheets_without_period_header[:10])
        suffix = f" 等共 {len(sheets_without_period_header)} 张" if len(sheets_without_period_header) > 10 else ""
        summary_parts.append(f"未在前 5 行识别到期间表头的可见工作表（请自行核对）：{shown}{suffix}。")
    summary_parts.append("以上均为核对提示，请结合公司指标、正负号与财年逐条判断。")

    return {
        "status": "ok",
        "file": str(path),
        "sheet_filter": sheet,
        "sheets_scanned": sheets_scanned,
        "sheets_without_period_header": sheets_without_period_header,
        "recalc_ran": recalc_ran,
        "recalc_complete": recalc_complete,
        "recalc_status": recalc_status,
        "recalc_note": recalc_note,
        "plugin_formula_counts": dict(sorted(plugin_counts.items())),
        "external_breakdown": external_breakdown,
        "error_code_counts": error_code_counts,
        "forecast_constants_by_sheet": fc_summary_by_sheet,
        "summary": " ".join(summary_parts),
        "counts": totals,
        "truncated": {
            "forecast_constants": fc_truncated,
            "possible_period_mismatch": pm_truncated,
            "external_or_broken_or_circular": ext_truncated,
            "recalc_errors": err_truncated,
        },
        "items": {
            "forecast_constants": fc_items,
            "possible_period_mismatch": pm_picked,
            "external_or_broken_or_circular": ext_items,
            "recalc_errors": err_items,
        },
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
        help="Copy workbook to a temporary file and run xlsx/scripts/recalc.py to check error values",
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
