#!/usr/bin/env python3
"""Round-scoped 4-class model checker, fallback ledger writer, and verify-done validator (modeling v2 §3.2, §6.1, §6.4, §8.4, §8.7).

Subcommands:
    check         Run C01-C14 checks on round-scoped edits and write model-check.md,
                  model-review-pack.md, remaining-issues.md, and model-verification.json.
    write-ledger  Fallback ledger writer invoked when check/recalc/diff/note stages time out
                  or exit empty before generating stage reports.
    verify-done   Authoritative terminal contract verifier reading model-verification.json.

Standalone script (stdlib + openpyxl + lxml only).
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any

from openpyxl.utils.cell import column_index_from_string, coordinate_from_string, get_column_letter

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import model_consensus  # noqa: E402
import model_edit  # noqa: E402
import model_fingerprint  # noqa: E402
import model_readable  # noqa: E402
import model_style  # noqa: E402

_EXCEL_ERRORS = (
    "#DIV/0!", "#REF!", "#NAME?", "#VALUE!", "#N/A",
    "#NUM!", "#NULL!", "#ERROR!", "#SPILL!", "#CALC!",
)
_ERROR_SHAPE = re.compile(r"^#(?:N/A|[A-Z0-9/]+[!?])$")
_SINGLE_ENGINE_LINE = "本轮复算只用一个引擎"
_NAME_TOKEN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]{1,})\b")
_EXCEL_BUILTIN_FUNCS = frozenset({
    "SUM", "AVERAGE", "AVG", "MIN", "MAX", "IF", "IFERROR", "IFNA", "AND", "OR", "NOT",
    "ABS", "ROUND", "ROUNDUP", "ROUNDDOWN", "INT", "MOD", "SQRT", "POWER", "LN", "LOG",
    "EXP", "COUNT", "COUNTA", "COUNTIF", "SUMIF", "SUMIFS", "SUBTOTAL", "INDEX", "MATCH",
    "VLOOKUP", "HLOOKUP", "XLOOKUP", "OFFSET", "INDIRECT", "CHOOSE", "NPV", "IRR", "XIRR",
    "TRUE", "FALSE", "NA", "ISNA", "ISERROR", "ISBLANK", "ISNUMBER", "TEXT", "VALUE",
    "LEFT", "RIGHT", "MID", "LEN", "CONCAT", "CONCATENATE", "TODAY", "NOW", "YEAR", "MONTH",
})


def _unit_tolerance(unit_scale: float, benchmark_abs: float = 0.0) -> float:
    """Adaptive tolerance per §8.7."""
    if unit_scale >= 1e9:
        unit_tol = 0.002
    elif unit_scale >= 1e6:
        unit_tol = 1.0
    elif unit_scale >= 1e3:
        unit_tol = 10.0
    else:
        unit_tol = 1.0
    return max(unit_tol, 1e-4 * abs(float(benchmark_abs)))


def _add_finding(
    findings: list[dict[str, Any]],
    check_id: str,
    severity_class: str,
    message: str,
    *,
    sheet: str | None = None,
    cell: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    findings.append(
        {
            "check_id": check_id,
            "severity_class": severity_class,
            "sheet": sheet,
            "cell": cell,
            "message": message,
            "details": details or {},
        }
    )


def _norm_formula(expr: str | None) -> str:
    if not expr:
        return ""
    s = str(expr).strip()
    return s if s.startswith("=") else "=" + s


def _num_close(a: Any, b: Any, rel_tol: float = 1e-9, abs_tol: float = 1e-9) -> bool:
    if a is None and b is None:
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= max(abs_tol, rel_tol * max(abs(float(a)), abs(float(b))))
    return str(a) == str(b)


def _load_insert_shift_class() -> Any:
    p = model_edit._locate_xlsx_script("insert.py")
    spec = importlib.util.spec_from_file_location("_dalton_insert_shift_mod", p)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load insert.py from {p}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_dalton_insert_shift_mod"] = mod
    spec.loader.exec_module(mod)
    return mod.Shift


def _replay_formula_through_ops(
    formula_text: str,
    context_sheet: str,
    all_sheets: list[str],
    ops: list[dict[str, Any]],
) -> str:
    """Replay structural ops through ``insert.py:Shift.formula`` (exact §8.7 C14 rule)."""
    if not ops or not formula_text:
        return _norm_formula(formula_text)
    Shift = _load_insert_shift_class()
    body = formula_text[1:] if formula_text.startswith("=") else formula_text
    sheet_map = {s: f"xl/worksheets/sheet{i + 1}.xml" for i, s in enumerate(all_sheets)}
    for op in ops:
        kind = op.get("op")
        target_sheet = op.get("sheet")
        count = int(op.get("count", 1))
        if kind in ("insert_columns", "delete_columns"):
            axis = "column"
            at_val = (
                column_index_from_string(str(op["at"]))
                if isinstance(op["at"], str) and not str(op["at"]).isdigit()
                else int(op["at"])
            )
            delete = kind == "delete_columns"
        elif kind in ("insert_rows", "delete_rows"):
            axis = "row"
            at_val = int(op["at"])
            delete = kind == "delete_rows"
        else:
            continue
        shifter = Shift(target_sheet, sheet_map, axis, at_val, count, delete)
        body = shifter.formula(body, context_sheet)
    return "=" + body


def _expand_defined_names_and_refs(
    expr: str,
    current_sheet: str,
    defined_names: list[dict[str, str]],
    *,
    tables: dict[str, dict[str, Any]] | None = None,
) -> tuple[set[tuple[str, str]], list[tuple[str, int, int, int, int]], bool]:
    """Return ``(direct_refs, range_rects, has_dynamic_ref)`` after expanding defined names & tables."""
    refs, rects, _, has_dyn = model_fingerprint.expand_defined_names_in_expr(
        expr, current_sheet, defined_names, tables=tables
    )
    return refs, rects, has_dyn


def _build_full_dependency_graph(
    cand_fp: dict[str, Any],
) -> tuple[
    dict[tuple[str, str], set[tuple[str, str]]],
    dict[tuple[str, str], list[tuple[str, int, int, int, int]]],
    dict[tuple[str, str], str],
]:
    """Build workbook-wide cell & range dependency graph (expanding defined_names + tables) & dynamic-ref map."""
    defined_names = cand_fp.get("defined_names") or []
    tables = cand_fp.get("tables") or {}
    graph: dict[tuple[str, str], set[tuple[str, str]]] = {}
    rect_graph: dict[tuple[str, str], list[tuple[str, int, int, int, int]]] = {}
    dynamic_cells: dict[tuple[str, str], str] = {}

    for sh_name, sh_data in (cand_fp.get("sheets") or {}).items():
        for coord, cmeta in (sh_data.get("cells") or {}).items():
            if cmeta.get("category") != "formula":
                continue
            f_txt = cmeta.get("formula") or ""
            refs, rects, has_dyn = _expand_defined_names_and_refs(
                f_txt, sh_name, defined_names, tables=tables
            )
            node = (sh_name, coord.upper())
            graph[node] = refs
            if rects:
                rect_graph[node] = rects
            if has_dyn:
                dynamic_cells[node] = f_txt
    return graph, rect_graph, dynamic_cells


def _rect_hits_targets(
    rects: list[tuple[str, int, int, int, int]],
    target_coords_by_sheet: dict[str, list[tuple[int, int, str]]],
) -> tuple[str, str] | None:
    for rsh, r1, c1, r2, c2 in rects:
        for tr, tc, tcoord in target_coords_by_sheet.get(rsh, ()):
            if r1 <= tr <= r2 and c1 <= tc <= c2:
                return (rsh, tcoord)
    return None


def _index_target_coords(targets: set[tuple[str, str]]) -> dict[str, list[tuple[int, int, str]]]:
    idx: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for sh, coord in targets:
        try:
            col_s, row_i = coordinate_from_string(coord.upper())
            col_i = column_index_from_string(col_s)
            idx[sh].append((row_i, col_i, coord.upper()))
        except Exception:
            continue
    return idx


def _reaches_any(
    start: tuple[str, str],
    targets: set[tuple[str, str]],
    graph: dict[tuple[str, str], set[tuple[str, str]]],
    rect_graph: dict[tuple[str, str], list[tuple[str, int, int, int, int]]] | None = None,
    target_index: dict[str, list[tuple[int, int, str]]] | None = None,
) -> tuple[str, str] | None:
    """BFS through formula cell & range dependencies to check if ``start`` reaches any node in ``targets``."""
    if not targets:
        return None
    t_idx = target_index if target_index is not None else (_index_target_coords(targets) if rect_graph else None)
    if rect_graph and t_idx and start in rect_graph:
        hit = _rect_hits_targets(rect_graph[start], t_idx)
        if hit is not None:
            return hit

    seen: set[tuple[str, str]] = {start}
    queue = list(graph.get(start, ()))
    while queue:
        cur = queue.pop(0)
        if cur in targets:
            return cur
        if cur in seen:
            continue
        seen.add(cur)
        if rect_graph and t_idx and cur in rect_graph:
            hit = _rect_hits_targets(rect_graph[cur], t_idx)
            if hit is not None:
                return hit
        for nxt in graph.get(cur, ()):
            if nxt not in seen:
                queue.append(nxt)
    return None


def _target_sheets_for_ticker(fp: dict[str, Any], ticker: str) -> set[str]:
    tk = ticker.strip().upper()
    targets: set[str] = set()
    for entry in fp.get("sheet_order") or []:
        tag = entry.get("company_tag")
        if tag is None or str(tag).upper() == tk:
            targets.add(entry["name"])
    return targets


def _non_target_company_sheets(fp: dict[str, Any], ticker: str) -> list[str]:
    tk = ticker.strip().upper()
    out: list[str] = []
    for entry in fp.get("sheet_order") or []:
        tag = entry.get("company_tag")
        if tag is not None and str(tag).upper() != tk:
            out.append(entry["name"])
    return out


def _next_fq(year: int, quarter: int) -> tuple[int, int]:
    return (year + 1, 1) if quarter == 4 else (year, quarter + 1)


def _quarter_fy_fq(meta: dict[str, Any], fy_end_month: int) -> tuple[int, int]:
    if meta.get("fiscal_year") and meta.get("fiscal_quarter"):
        return int(meta["fiscal_year"]), int(meta["fiscal_quarter"])
    return model_fingerprint.quarter_to_fiscal_year(
        int(meta["year"]),
        int(meta["quarter"]),
        fy_end_month,
        is_fiscal_labeled=bool(meta.get("is_fiscal_labeled", False)),
        end_month=meta.get("end_month"),
    )


def _check_period_sequence(
    cols: dict[str, dict[str, Any]],
    *,
    fy_end_month: int = 12,
) -> dict[str, Any]:
    """Analyze forecast annual & quarterly coverage from the latest actual quarter (Prompt R3 #6 / Codex #7)."""
    has_any_q = any(m.get("kind") == "quarter" for m in cols.values())
    act_ann_years = sorted({
        int(m["year"]) for m in cols.values()
        if m.get("kind") == "annual" and not m.get("is_forecast") and m.get("year")
    })
    fc_ann_years = sorted({
        int(m["year"]) for m in cols.values()
        if m.get("kind") == "annual" and m.get("is_forecast") and m.get("year")
    })
    act_q = sorted({
        _quarter_fy_fq(m, fy_end_month)
        for m in cols.values()
        if m.get("kind") == "quarter" and not m.get("is_forecast") and m.get("year") and m.get("quarter")
    })
    fc_q = sorted({
        _quarter_fy_fq(m, fy_end_month)
        for m in cols.values()
        if m.get("kind") == "quarter" and m.get("is_forecast") and m.get("year") and m.get("quarter")
    })

    non_consec_ann = [
        (fc_ann_years[i], fc_ann_years[i + 1])
        for i in range(len(fc_ann_years) - 1)
        if fc_ann_years[i + 1] != fc_ann_years[i] + 1
    ]
    skipped_q: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for i in range(len(fc_q) - 1):
        if fc_q[i + 1] != _next_fq(fc_q[i][0], fc_q[i][1]):
            skipped_q.append((fc_q[i], fc_q[i + 1]))

    # Compute required full forecast interval starting immediately after the latest actual quarter:
    # - Quarterly: remaining quarters of current FY (if any) + all 4 quarters of next FY (through (last_ay + 1, 4))
    # - Annual: 3 complete forecast FYs starting at start_y ([start_y, start_y + 1, start_y + 2])
    expected_fc_q: list[tuple[int, int]] = []
    expected_fc_ann: list[int] = []
    if has_any_q and act_q:
        last_ay, last_aq = act_q[-1]
        cur = _next_fq(last_ay, last_aq)
        end_q = (last_ay + 1, 4)
        while cur <= end_q:
            expected_fc_q.append(cur)
            cur = _next_fq(cur[0], cur[1])
        start_y = expected_fc_q[0][0]
        expected_fc_ann = [start_y, start_y + 1, start_y + 2]
    elif has_any_q and fc_q:
        start_y = fc_q[0][0]
        cur = fc_q[0]
        end_q = (start_y + 1, 4)
        while cur <= end_q:
            expected_fc_q.append(cur)
            cur = _next_fq(cur[0], cur[1])
        expected_fc_ann = [start_y, start_y + 1, start_y + 2]
    elif act_ann_years:
        start_y = act_ann_years[-1] + 1
        expected_fc_ann = [start_y, start_y + 1, start_y + 2]

    fc_q_set = set(fc_q)
    fc_ann_set = set(fc_ann_years)
    missing_fc_q = [q for q in expected_fc_q if q not in fc_q_set]
    missing_fc_ann = [y for y in expected_fc_ann if y not in fc_ann_set]
    q_span_short = bool(expected_fc_q and expected_fc_q[-1] not in fc_q_set)

    return {
        "has_any_q": has_any_q,
        "act_q": act_q,
        "fc_ann_years": fc_ann_years,
        "fc_q": fc_q,
        "expected_fc_q": expected_fc_q,
        "expected_fc_ann": expected_fc_ann,
        "missing_fc_q": missing_fc_q,
        "missing_fc_ann": missing_fc_ann,
        "zero_fc_q": has_any_q and len(fc_q) == 0,
        "ann_span_short": len(fc_ann_years) < 3 or bool(missing_fc_ann),
        "non_consec_ann": non_consec_ann,
        "skipped_q": skipped_q,
        "q_span_short": q_span_short,
    }


def _fmt_category(num_fmt: str | None) -> str:
    s = str(num_fmt or "General").strip()
    if not s or s.lower() == "general" or s == "@":
        return "general"
    if "%" in s:
        return "percent"
    if re.search(r'(?:"\s*x"|\\x|\bx\b)', s, re.IGNORECASE):
        return "multiple"
    if any(ch in s for ch in ("0", "#")):
        return "number"
    return "other"


def _extract_snapshot_entries(
    cand_fp: dict[str, Any],
    base_fp: dict[str, Any],
    cm: dict[str, Any],
) -> tuple[set[tuple[str, str]], list[dict[str, Any]]]:
    """Build snapshot node set and structured ``(period_norm, metric_key, value)`` metadata."""
    snapshot_nodes: set[tuple[str, str]] = set()
    snapshot_entries: list[dict[str, Any]] = []

    for sh_name, sh_data in (cand_fp.get("sheets") or {}).items():
        snap_coords = set((sh_data.get("semantic_anchors") or {}).get("consensus_snapshot_cells") or ())
        snap_coords.update(c.upper() for c in (cm["consensus_snapshot_cells"].get(sh_name) or ()))
        b_sh = (base_fp.get("sheets") or {}).get(sh_name)
        if b_sh:
            for old_sc in (b_sh.get("semantic_anchors") or {}).get("consensus_snapshot_cells") or ():
                nc = model_edit.map_coord_str(cm, sh_name, old_sc)
                if nc:
                    snap_coords.add(nc.upper())
        if not snap_coords:
            continue

        cells = sh_data.get("cells") or {}
        # Detect period / metric / source / date columns per header row on this sheet
        row_hdrs: dict[int, dict[str, int]] = {}
        max_r = max((coordinate_from_string(c)[1] for c in cells), default=1)
        for r in range(1, max_r + 1):
            hdr_cols: dict[str, int] = {}
            for coord, cmeta in cells.items():
                cs, ri = coordinate_from_string(coord)
                if ri != r:
                    continue
                v = cmeta.get("cached_value")
                if not isinstance(v, str) or not v.strip():
                    continue
                vl = " ".join(v.strip().lower().split())
                ci = column_index_from_string(cs)
                if "period_col" not in hdr_cols and any(k in vl for k in ("period", "期间", "fiscal", "quarter")):
                    hdr_cols["period_col"] = ci
                elif "metric_col" not in hdr_cols and any(k in vl for k in ("metric", "line item", "item", "指标", "科目", "项目")):
                    hdr_cols["metric_col"] = ci
                elif "source_col" not in hdr_cols and any(k in vl for k in ("source", "来源")):
                    hdr_cols["source_col"] = ci
                elif "date_col" not in hdr_cols and any(k in vl for k in ("as-of", "as of", "date", "日期")):
                    hdr_cols["date_col"] = ci
            if "period_col" in hdr_cols and "metric_col" in hdr_cols:
                row_hdrs[r] = hdr_cols
        sorted_hdr_rows = sorted(row_hdrs)

        for sc in sorted(snap_coords):
            scu = sc.upper()
            node = (sh_name, scu)
            snapshot_nodes.add(node)
            cmeta = cells.get(scu) or {}
            cv = cmeta.get("cached_value")
            if not (isinstance(cv, (int, float)) and not isinstance(cv, bool)):
                continue
            _, row_i = coordinate_from_string(scu)
            # Find nearest preceding header row
            active_hdr = None
            for hr in sorted_hdr_rows:
                if hr < row_i:
                    active_hdr = row_hdrs[hr]
            p_norm: str | None = None
            m_key: str | None = None
            has_src_date = True
            if active_hdr:
                p_raw = cells.get(f"{get_column_letter(active_hdr['period_col'])}{row_i}", {}).get("cached_value")
                m_raw = cells.get(f"{get_column_letter(active_hdr['metric_col'])}{row_i}", {}).get("cached_value")
                if p_raw:
                    parsed_p = model_fingerprint.parse_period_header(p_raw)
                    p_norm = (parsed_p["normalized"] if parsed_p else str(p_raw).strip()).upper()
                if m_raw:
                    m_key = model_consensus._normalize_metric_key(str(m_raw))
                if "source_col" in active_hdr and "date_col" in active_hdr:
                    sv = str(cells.get(f"{get_column_letter(active_hdr['source_col'])}{row_i}", {}).get("cached_value") or "").strip()
                    dv = str(cells.get(f"{get_column_letter(active_hdr['date_col'])}{row_i}", {}).get("cached_value") or "").strip()
                    has_src_date = bool(sv and dv)
            snapshot_entries.append(
                {
                    "node": node,
                    "value": float(cv),
                    "period_norm": p_norm,
                    "metric_key": m_key,
                    "has_src_date": has_src_date,
                }
            )
    return snapshot_nodes, snapshot_entries


def _has_reconcile_note(text: str) -> bool:
    if not text:
        return False
    return bool(
        re.search(
            r"(?:restricted|time\s+deposit|short[\-\s]?term\s+inv|reconcil|bridge|offset|buyback|repurchase|sbc|dilut|受限|定期|调节|桥接|抵销|回购|增发|激励)",
            text,
            re.IGNORECASE,
        )
    )


def _cell_is_error(c_cell: dict[str, Any] | None) -> bool:
    if not c_cell:
        return False
    if c_cell.get("cached_t") == "e":
        return True
    cv = c_cell.get("cached_value")
    return isinstance(cv, str) and bool(cv in _EXCEL_ERRORS or _ERROR_SHAPE.match(cv))


def _formula_has_ref_literal(f_txt: str | None) -> bool:
    if not f_txt:
        return False
    return "#REF!" in model_fingerprint._EXCEL_STR_RE.sub('""', str(f_txt)).upper()


def _detect_scope_changes(
    base_fp: dict[str, Any],
    cand_fp: dict[str, Any],
    coord_map: dict[str, Any],
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    """Compute effective touched_cells, touched_cols, and rollover_actual_cells even if coord_map is sparse."""
    touched_cells: dict[str, set[str]] = defaultdict(set)
    for sh, cs in (coord_map.get("touched_cells") or {}).items():
        for c in cs:
            touched_cells[sh].add(str(c).upper())

    # Diff candidate vs mapped baseline cells so manual/unregistered edits are still caught!
    for sh_name, c_sh in (cand_fp.get("sheets") or {}).items():
        b_sh = (base_fp.get("sheets") or {}).get(sh_name)
        if b_sh is None:
            for coord in (c_sh.get("cells") or {}):
                touched_cells[sh_name].add(coord.upper())
            continue
        b_mapped: dict[str, dict[str, Any]] = {}
        for old_coord, b_cell in (b_sh.get("cells") or {}).items():
            nc = model_edit.map_coord_str(coord_map, sh_name, old_coord)
            if nc:
                b_mapped[nc.upper()] = b_cell
        for coord, c_cell in (c_sh.get("cells") or {}).items():
            cu = coord.upper()
            b_cell = b_mapped.get(cu)
            if b_cell is None:
                touched_cells[sh_name].add(cu)
                continue
            if c_cell.get("category") != b_cell.get("category"):
                touched_cells[sh_name].add(cu)
            elif c_cell.get("category") == "formula":
                if _norm_formula(c_cell.get("formula")) != _norm_formula(b_cell.get("formula")):
                    # Check if change is pure structural shift
                    ops = coord_map.get("ops") or []
                    if ops:
                        all_sh = [s["name"] for s in (base_fp.get("sheet_order") or [])]
                        exp_f = _replay_formula_through_ops(b_cell.get("formula") or "", sh_name, all_sh, ops)
                        if _norm_formula(c_cell.get("formula")) != _norm_formula(exp_f):
                            touched_cells[sh_name].add(cu)
                    else:
                        touched_cells[sh_name].add(cu)
            elif not _num_close(c_cell.get("cached_value"), b_cell.get("cached_value")):
                touched_cells[sh_name].add(cu)

    touched_cols: dict[str, set[str]] = defaultdict(set)
    for sh, cs in touched_cells.items():
        for coord in cs:
            col_s, _ = coordinate_from_string(coord)
            touched_cols[sh].add(col_s.upper())
    for sh, cols in (coord_map.get("added_period_cols") or {}).items():
        for col_s in cols:
            touched_cols[sh].add(str(col_s).upper())
    for sh, cols in (coord_map.get("rollover_actual_cols") or {}).items():
        for col_s in cols:
            touched_cols[sh].add(str(col_s).upper())

    rollover_cells: dict[str, set[str]] = defaultdict(set)
    for sh, cs in (coord_map.get("rollover_actual_cells") or {}).items():
        for c in cs:
            rollover_cells[sh].add(str(c).upper())

    return touched_cells, touched_cols, rollover_cells


def run_checks(
    *,
    candidate_path: str | Path,
    ticker: str,
    baseline_path: str | Path | None = None,
    fingerprint: dict[str, Any] | None = None,
    coord_map: dict[str, Any] | None = None,
    consensus_diff: dict[str, Any] | None = None,
    mode: str = "update",
) -> dict[str, Any]:
    """Execute deterministic C01-C14 checks and return structured 4-class findings."""
    cand = Path(candidate_path).expanduser().resolve()
    tk = ticker.strip().upper()
    findings: list[dict[str, Any]] = []
    optional_notices: list[str] = []

    # C01.1 Readability
    ok_read, read_msg = model_readable.check_workbook_readable(cand)
    if not ok_read:
        _add_finding(findings, "C01", "actionable", f"Candidate workbook unreadable: {read_msg}")
        return {
            "candidate_path": str(cand),
            "candidate_sha256": model_fingerprint.file_sha256(cand) if cand.is_file() else None,
            "readable": False,
            "findings": findings,
            "optional_notices": optional_notices,
            "dynamic_refs": [],
            "counts": {"actionable": 1, "baseline_legacy": 0, "missing_evidence": 0, "not_applicable": 0},
        }

    cand_fp = model_fingerprint.build_fingerprint(cand, ticker=tk)
    cand_sha = cand_fp["source_sha256"]

    if fingerprint is not None:
        base_fp = fingerprint
    elif baseline_path is not None:
        base_fp = model_fingerprint.build_fingerprint(baseline_path, ticker=tk)
    else:
        base_fp = cand_fp

    base_sha = base_fp["source_sha256"]
    cm = dict(coord_map or {})
    cm.setdefault("ops", [])
    cm.setdefault("touched_cells", {})
    cm.setdefault("added_period_cols", {})
    cm.setdefault("rollover_actual_cols", {})
    cm.setdefault("rollover_actual_cells", {})
    cm.setdefault("consensus_snapshot_cells", {})

    is_unchanged = (cand_sha == base_sha) and not cm.get("ops") and not any(cm["touched_cells"].values())
    touched_cells, touched_cols, rollover_cells = _detect_scope_changes(base_fp, cand_fp, cm)
    target_sheets = _target_sheets_for_ticker(cand_fp, tk)
    non_target_sheets = _non_target_company_sheets(base_fp, tk)

    # C01.2 Sheet order & C01.3 Changelog
    base_order = [s["name"] for s in (base_fp.get("sheet_order") or [])]
    cand_order = [s["name"] for s in (cand_fp.get("sheet_order") or [])]
    missing_sheets = [s for s in base_order if s not in cand_order]
    if missing_sheets:
        _add_finding(findings, "C01", "actionable", f"Missing baseline sheets: {', '.join(missing_sheets)}")
    else:
        common_in_cand = [s for s in cand_order if s in base_order]
        if common_in_cand != base_order:
            _add_finding(
                findings,
                "C01",
                "actionable",
                f"Sheet relative order changed: expected {base_order}, got {common_in_cand}",
            )

    if not is_unchanged:
        rm_sh = next((s for s in cand_order if "readme" in s.lower() or "change" in s.lower()), None)
        if rm_sh is None:
            _add_finding(findings, "C01", "actionable", "Missing README & Changelog / Change Log worksheet")
        else:
            base_rm_cells = (base_fp.get("sheets") or {}).get(rm_sh, {}).get("cells") or {}
            cand_rm_cells = (cand_fp.get("sheets") or {}).get(rm_sh, {}).get("cells") or {}
            base_max_r = max((coordinate_from_string(c)[1] for c in base_rm_cells), default=0)
            cand_max_r = max((coordinate_from_string(c)[1] for c in cand_rm_cells), default=0)
            if cand_max_r <= base_max_r:
                _add_finding(
                    findings,
                    "C01",
                    "actionable",
                    f"No new changelog row appended in {rm_sh!r} for this update",
                    sheet=rm_sh,
                )

    # C02 Period coverage, historical actual preservation & forecast sequence continuity
    for sh_name in target_sheets:
        b_sh = (base_fp.get("sheets") or {}).get(sh_name)
        c_sh = (cand_fp.get("sheets") or {}).get(sh_name)
        if not c_sh:
            continue
        c_axis = c_sh.get("period_axis") or {}
        c_cols = c_axis.get("columns") or {}
        c_hdr_r = c_axis.get("header_row") or 1
        period_headers_changed = False

        if b_sh:
            b_axis = b_sh.get("period_axis") or {}
            b_cols = b_axis.get("columns") or {}
            b_hdr_r = b_axis.get("header_row") or 1
            cand_actual_periods = {
                str(m.get("normalized") or m.get("raw") or "").upper()
                for m in c_cols.values()
                if m.get("kind") in ("annual", "quarter") and not m.get("is_forecast")
            }
            for col_l, meta in b_cols.items():
                if meta.get("kind") not in ("annual", "quarter"):
                    continue
                old_ci = int(meta.get("col_index") or column_index_from_string(col_l))
                mapped_pos = model_edit.map_coord(cm, sh_name, b_hdr_r, old_ci)
                mapped_col_l = get_column_letter(mapped_pos[1]) if mapped_pos is not None else None
                mapped_c_meta = c_cols.get(mapped_col_l) if mapped_col_l else None
                if not meta.get("is_forecast"):
                    norm_p = str(meta.get("normalized") or meta.get("raw") or "").upper()
                    if mapped_c_meta and mapped_c_meta.get("is_forecast"):
                        _add_finding(
                            findings,
                            "C02",
                            "actionable",
                            f"Historical actual period {meta.get('raw')!r} ({col_l}->{mapped_col_l}) was changed to forecast {mapped_c_meta.get('raw')!r}",
                            sheet=sh_name,
                            cell=f"{mapped_col_l}{c_hdr_r}",
                        )
                    elif norm_p and norm_p not in cand_actual_periods:
                        _add_finding(
                            findings,
                            "C02",
                            "actionable",
                            f"Historical period column {meta.get('raw')!r} ({col_l}) was removed or lost actual status",
                            sheet=sh_name,
                            cell=f"{mapped_col_l or col_l}{c_hdr_r}",
                        )
                if mapped_c_meta is None or str(mapped_c_meta.get("raw") or "") != str(meta.get("raw") or ""):
                    period_headers_changed = True
            if len(c_cols) != len(b_cols):
                period_headers_changed = True

        # Non-Dec FY label check
        if c_axis.get("fiscal_year_end_month", 12) != 12 and not c_axis.get("fiscal_year_end_explicit"):
            b_non_dec_unlabeled = bool(
                b_sh
                and (b_sh.get("period_axis") or {}).get("fiscal_year_end_month", 12) != 12
                and not (b_sh.get("period_axis") or {}).get("fiscal_year_end_explicit")
            )
            sev = "baseline_legacy" if (b_non_dec_unlabeled and mode != "build") else "actionable"
            _add_finding(
                findings,
                "C02",
                sev,
                f"Non-December fiscal year (ends month {c_axis.get('fiscal_year_end_month')}) not explicitly noted in header/A2",
                sheet=sh_name,
            )

        # Forecast span & continuity check (compare vs baseline so untouched legacy gaps stay baseline_legacy)
        if sh_name.lower().startswith("model") and (
            mode == "build" or cm["added_period_cols"].get(sh_name) or period_headers_changed
        ):
            c_fy_mo = int(c_axis.get("fiscal_year_end_month") or 12)
            b_axis_seq = (b_sh.get("period_axis") or {}) if b_sh else {}
            b_fy_mo = int(b_axis_seq.get("fiscal_year_end_month") or c_fy_mo)
            c_seq = _check_period_sequence(c_cols, fy_end_month=c_fy_mo)
            b_seq = _check_period_sequence(b_axis_seq.get("columns") or {}, fy_end_month=b_fy_mo)

            if c_seq["ann_span_short"]:
                b_same_ann = bool(
                    b_sh
                    and b_seq["ann_span_short"]
                    and b_seq["missing_fc_ann"] == c_seq["missing_fc_ann"]
                    and mode != "build"
                )
                sev = "baseline_legacy" if b_same_ann else "actionable"
                _add_finding(
                    findings, "C02", sev,
                    f"Annual forecast span incomplete ({c_seq['fc_ann_years']}; missing {c_seq['missing_fc_ann']}); require 3 full forecast years {c_seq['expected_fc_ann']}",
                    sheet=sh_name,
                )
            if c_seq["non_consec_ann"]:
                sev = "baseline_legacy" if (b_sh and b_seq["non_consec_ann"] == c_seq["non_consec_ann"] and mode != "build") else "actionable"
                _add_finding(
                    findings, "C02", sev,
                    f"Non-consecutive annual forecast years detected: {c_seq['fc_ann_years']}",
                    sheet=sh_name,
                )
            if c_seq["zero_fc_q"]:
                sev = "baseline_legacy" if (b_sh and b_seq["zero_fc_q"] and mode != "build") else "actionable"
                _add_finding(
                    findings, "C02", sev,
                    "Model sheet has quarterly columns but zero forecast quarters",
                    sheet=sh_name,
                )
            elif c_seq["missing_fc_q"]:
                b_miss_set = set(b_seq["missing_fc_q"]) if b_sh else set()
                new_missing_q = [q for q in c_seq["missing_fc_q"] if q not in b_miss_set]
                sev = "baseline_legacy" if (b_sh and not new_missing_q and mode != "build") else "actionable"
                miss_fmt = [f"{q}Q{y % 100:02d}E" for y, q in (new_missing_q or c_seq["missing_fc_q"])]
                _add_finding(
                    findings, "C02", sev,
                    f"Missing required forecast quarter(s) after latest actual quarter: {miss_fmt}",
                    sheet=sh_name,
                )
            if c_seq["skipped_q"]:
                sev = "baseline_legacy" if (b_sh and b_seq["skipped_q"] == c_seq["skipped_q"] and mode != "build") else "actionable"
                _add_finding(
                    findings, "C02", sev,
                    f"Skipped quarter(s) in quarterly forecast sequence: {c_seq['skipped_q']}",
                    sheet=sh_name,
                )

    # C03 Round-scoped formula protection & rollover actual auto-exemption
    for sh_name in target_sheets:
        if any(k in sh_name.lower() for k in ("readme", "changelog", "consensus")):
            continue
        c_sh = (cand_fp.get("sheets") or {}).get(sh_name)
        b_sh = (base_fp.get("sheets") or {}).get(sh_name)
        if not c_sh:
            continue
        c_axis = c_sh.get("period_axis") or {}
        c_cols = c_axis.get("columns") or {}
        hdr_r = c_axis.get("header_row") or 1
        c_cells = c_sh.get("cells") or {}
        c_lim = c_sh.get("line_item_map") or {}
        b_cells = (b_sh.get("cells") or {}) if b_sh else {}
        b_lim = (b_sh.get("line_item_map") or {}) if b_sh else {}

        # Map baseline cells -> candidate coords
        b_mapped_cells: dict[str, tuple[str, dict[str, Any]]] = {}
        for old_coord, b_cell in b_cells.items():
            nc = model_edit.map_coord_str(cm, sh_name, old_coord)
            if nc:
                b_mapped_cells[nc.upper()] = (old_coord.upper(), b_cell)

        roll_cols = {c.upper() for c in (cm["rollover_actual_cols"].get(sh_name) or ())}
        added_cols = {c.upper() for c in (cm["added_period_cols"].get(sh_name) or ())}
        sh_touched = touched_cells.get(sh_name, set())
        sh_roll_cells = rollover_cells.get(sh_name, set())

        for coord, c_cell in c_cells.items():
            cu = coord.upper()
            col_s, row_i = coordinate_from_string(cu)
            if row_i <= hdr_r:
                continue
            col_meta = c_cols.get(col_s)
            if not col_meta or col_meta.get("kind") not in ("annual", "quarter"):
                continue

            in_scope = (cu in sh_touched) or (col_s in roll_cols) or (col_s in added_cols)
            b_pair = b_mapped_cells.get(cu)
            b_cell = b_pair[1] if b_pair else None
            b_row_i = coordinate_from_string(b_pair[0])[1] if b_pair else row_i
            row_meta = c_lim.get(str(row_i)) or b_lim.get(str(b_row_i)) or {}
            is_sub_or_ratio = bool(
                row_meta.get("is_subtotal_or_ratio")
                or (b_cell and b_cell.get("is_subtotal_or_ratio_row"))
                or c_cell.get("is_subtotal_or_ratio_row")
            )

            # Case 1: Baseline was formula, candidate is now non-formula constant
            if b_cell is not None and b_cell.get("category") == "formula" and c_cell.get("category") != "formula":
                if not in_scope:
                    _add_finding(
                        findings, "C03", "baseline_legacy",
                        f"Untouched cell {sh_name}!{cu} is constant",
                        sheet=sh_name, cell=cu,
                    )
                    continue
                if col_s in roll_cols:
                    if is_sub_or_ratio:
                        _add_finding(
                            findings, "C03", "actionable",
                            f"Subtotal/total/ratio row converted to constant in rollover column {col_s}: {sh_name}!{cu}",
                            sheet=sh_name, cell=cu,
                        )
                    elif cu in sh_roll_cells:
                        # Explicitly registered detail rollover actual -> auto-exempt!
                        continue
                    else:
                        # Detail/KPI row in rollover column -> allowed
                        continue
                else:
                    # Plugin formula extracted to input+ref is checked by whether formula remains;
                    # here cell itself lost its formula outside rollover -> actionable!
                    _add_finding(
                        findings, "C03", "actionable",
                        f"Formula degraded to constant: {sh_name}!{cu} (was {b_cell.get('formula')})",
                        sheet=sh_name, cell=cu,
                    )
                continue

            # Case 2: Forecast column constant on a newly added/touched cell
            if col_meta.get("is_forecast") and c_cell.get("category") == "input":
                subcat = c_cell.get("subcategory")
                if subcat in ("driver_input", "guidance_input", "consensus_snapshot"):
                    continue
                if not in_scope:
                    _add_finding(
                        findings, "C03", "baseline_legacy",
                        f"Untouched baseline forecast constant at {sh_name}!{cu}",
                        sheet=sh_name, cell=cu,
                    )
                elif is_sub_or_ratio or row_meta.get("row_type") in ("flow", "eps", "ratio"):
                    # Check if this row normally uses formulas in peer forecast cols
                    has_peer_formula = any(
                        c_cells.get(f"{oc}{row_i}", {}).get("category") == "formula"
                        for oc, om in c_cols.items()
                        if oc != col_s and om.get("kind") == col_meta.get("kind") and om.get("is_forecast")
                    )
                    if is_sub_or_ratio or has_peer_formula:
                        _add_finding(
                            findings, "C03", "actionable",
                            f"Forecast amount/subtotal/ratio cell written as hard-coded constant: {sh_name}!{cu}",
                            sheet=sh_name, cell=cu,
                        )
                    else:
                        _add_finding(
                            findings, "C03", "not_applicable",
                            f"Unclassified forecast input at {sh_name}!{cu}",
                            sheet=sh_name, cell=cu,
                        )
                else:
                    _add_finding(
                        findings, "C03", "not_applicable",
                        f"Unclassified input at {sh_name}!{cu}",
                        sheet=sh_name, cell=cu,
                    )

    # C04 Consensus snapshot isolation & diff rationale
    dep_graph, rect_graph, dynamic_cells = _build_full_dependency_graph(cand_fp)
    base_dep_graph, base_rect_graph, _ = _build_full_dependency_graph(base_fp)
    snapshot_nodes, snapshot_entries = _extract_snapshot_entries(cand_fp, base_fp, cm)
    snap_target_idx = _index_target_coords(snapshot_nodes)

    base_snapshot_nodes: set[tuple[str, str]] = set()
    for bsh_name, bsh_data in (base_fp.get("sheets") or {}).items():
        for sc in (bsh_data.get("semantic_anchors") or {}).get("consensus_snapshot_cells") or ():
            base_snapshot_nodes.add((bsh_name, sc.upper()))
    base_snap_idx = _index_target_coords(base_snapshot_nodes)

    # Check snapshot metadata (source & as-of date) on touched snapshot cells
    for entry in snapshot_entries:
        s_sh, s_crd = entry["node"]
        if not entry.get("has_src_date"):
            in_scope_s = s_crd in touched_cells.get(s_sh, set())
            _add_finding(
                findings, "C04", "actionable" if in_scope_s else "baseline_legacy",
                f"Consensus snapshot cell {s_sh}!{s_crd} is missing source_type or as_of_date",
                sheet=s_sh, cell=s_crd,
            )

    # Check House forecast/result cells on non-consensus sheets
    for sh_name in target_sheets:
        if any(k in sh_name.lower() for k in ("consensus", "readme", "changelog")):
            continue
        sh_data = (cand_fp.get("sheets") or {}).get(sh_name)
        b_sh = (base_fp.get("sheets") or {}).get(sh_name)
        if not sh_data:
            continue
        cols = (sh_data.get("period_axis") or {}).get("columns") or {}
        hdr_r = (sh_data.get("period_axis") or {}).get("header_row") or 1
        anchors = sh_data.get("semantic_anchors") or {}
        lim = sh_data.get("line_item_map") or {}
        result_rows = {
            r for r in (
                anchors.get("total_revenue_row"),
                anchors.get("gross_profit_row"),
                anchors.get("operating_income_row"),
                anchors.get("net_income_row"),
                anchors.get("eps_row"),
            )
            if r is not None
        }
        sh_touched = touched_cells.get(sh_name, set())
        b_mapped_c04: dict[str, tuple[str, dict[str, Any]]] = {}
        if b_sh:
            for old_c, bc in (b_sh.get("cells") or {}).items():
                nc = model_edit.map_coord_str(cm, sh_name, old_c)
                if nc:
                    b_mapped_c04[nc.upper()] = (old_c.upper(), bc)

        for coord, c_cell in (sh_data.get("cells") or {}).items():
            cu = coord.upper()
            col_s, row_i = coordinate_from_string(cu)
            if row_i <= hdr_r:
                continue
            col_meta = cols.get(col_s)
            is_fc = bool(col_meta and col_meta.get("is_forecast"))
            is_touched = cu in sh_touched
            if not is_fc and not is_touched:
                continue

            b_pair = b_mapped_c04.get(cu)
            b_old_coord, b_cell = (b_pair[0], b_pair[1]) if b_pair else (None, None)

            if c_cell.get("category") == "formula":
                hit = _reaches_any((sh_name, cu), snapshot_nodes, dep_graph, rect_graph, snap_target_idx)
                if hit is not None:
                    was_legacy_dep = bool(
                        not is_touched
                        and b_old_coord is not None
                        and b_cell is not None
                        and b_cell.get("category") == "formula"
                        and _reaches_any(
                            (sh_name, b_old_coord), base_snapshot_nodes, base_dep_graph, base_rect_graph, base_snap_idx
                        ) is not None
                    )
                    _add_finding(
                        findings, "C04", "baseline_legacy" if was_legacy_dep else "actionable",
                        f"House forecast/driver formula {sh_name}!{cu} depends on consensus snapshot cell {hit[0]}!{hit[1]} (directly, via range/table, or via defined_names)",
                        sheet=sh_name, cell=cu,
                        details={"snapshot_ref": f"{hit[0]}!{hit[1]}", "formula": c_cell.get("formula")},
                    )
            elif c_cell.get("category") == "input" and (row_i in result_rows or c_cell.get("is_subtotal_or_ratio_row")):
                cv = c_cell.get("cached_value")
                if isinstance(cv, (int, float)) and not isinstance(cv, bool):
                    cell_period = str((col_meta or {}).get("normalized") or (col_meta or {}).get("raw") or "").upper()
                    row_lbl = str((lim.get(str(row_i)) or {}).get("label") or "")
                    cell_mkey = model_consensus._normalize_metric_key(row_lbl) if row_lbl else None
                    # Check whether baseline already had this exact constant (unchanged in this round)
                    was_legacy_const = bool(
                        not is_touched
                        and b_cell is not None
                        and b_cell.get("category") == "input"
                        and _num_close(cv, b_cell.get("cached_value"), rel_tol=1e-6, abs_tol=1e-6)
                    )
                    for entry in snapshot_entries:
                        snap_node = entry["node"]
                        snap_val = entry["value"]
                        if not _num_close(cv, snap_val, rel_tol=1e-6, abs_tol=1e-6):
                            continue
                        # Match by (period, metric_key) when snapshot carries period/metric metadata
                        sp = entry.get("period_norm")
                        sm = entry.get("metric_key")
                        if sp and cell_period and sp != cell_period:
                            continue
                        if sm and cell_mkey and sm != cell_mkey:
                            continue
                        if (not sp or not sm) and not is_touched:
                            continue
                        _add_finding(
                            findings, "C04", "baseline_legacy" if was_legacy_const else "actionable",
                            f"House result cell {sh_name}!{cu} is a hard-coded constant ({cv}) equal to consensus snapshot {snap_node[0]}!{snap_node[1]}",
                            sheet=sh_name, cell=cu,
                            details={"snapshot_ref": f"{snap_node[0]}!{snap_node[1]}", "value": cv},
                        )
                        break

    # Dynamic refs recorded as review notes (not actionable in P0; handed to P1 review pack)
    dynamic_ref_list: list[dict[str, str]] = []
    for (dsh, dcoord), df in sorted(dynamic_cells.items()):
        if dsh in target_sheets:
            dynamic_ref_list.append({"sheet": dsh, "cell": dcoord, "formula": df})

    # Consensus diff rationale & source check (Prompt R3 #4: hash mismatch -> stage failure ONLY, never actionable!)
    if consensus_diff is not None:
        if consensus_diff.get("status") == "missing_evidence":
            _add_finding(findings, "C04", "missing_evidence", "No consensus data available for diff comparison")
        elif str(consensus_diff.get("model_sha256") or "").lower() != str(cand_sha or "").lower():
            optional_notices.append(
                f"Stale consensus-diff.json hash ({consensus_diff.get('model_sha256')}) != candidate ({cand_sha}); recorded as stage failure only"
            )
        elif int(consensus_diff.get("missing_rationale_count") or 0) > 0:
            _add_finding(
                findings, "C04", "actionable",
                f"{consensus_diff['missing_rationale_count']} above-threshold consensus diff item(s) lack rationale",
            )

    # C05 Normalized style consistency & rightmost source notes + zero new comments
    for sh_name in target_sheets:
        c_sh = (cand_fp.get("sheets") or {}).get(sh_name)
        b_sh = (base_fp.get("sheets") or {}).get(sh_name)
        if not c_sh:
            continue
        c_cells = c_sh.get("cells") or {}
        b_cells = (b_sh.get("cells") or {}) if b_sh else {}
        c_axis = c_sh.get("period_axis") or {}
        c_cols = c_axis.get("columns") or {}
        hdr_r = c_axis.get("header_row") or 1
        note_col = c_axis.get("source_note_col")
        b_comment_coords: set[str] = set()
        b_mapped_c05: dict[str, dict[str, Any]] = {}
        for old_c, bc in b_cells.items():
            nc = model_edit.map_coord_str(cm, sh_name, old_c)
            if nc:
                b_mapped_c05[nc.upper()] = bc
                if bc.get("has_comment"):
                    b_comment_coords.add(nc.upper())

        is_data_sheet = not any(k in sh_name.lower() for k in ("readme", "changelog", "consensus"))
        added_cols_set = {str(c).upper() for c in (cm["added_period_cols"].get(sh_name) or ())}
        check_coords_c05 = set(touched_cells.get(sh_name, set()))
        for crd in c_cells:
            cs, ri = coordinate_from_string(crd)
            if cs.upper() in added_cols_set and ri > hdr_r:
                check_coords_c05.add(crd.upper())

        for cu in sorted(check_coords_c05):
            cc = c_cells.get(cu)
            if not cc:
                continue
            if cc.get("has_comment") and cu not in b_comment_coords:
                _add_finding(
                    findings, "C05", "actionable",
                    f"New Excel cell comment added at {sh_name}!{cu} (put notes in rightmost source column instead)",
                    sheet=sh_name, cell=cu,
                )
            if cc.get("category") == "input" and cc.get("subcategory") in ("hist_estimate", "guidance_input") and note_col:
                _, row_i = coordinate_from_string(cu)
                note_coord = f"{get_column_letter(int(note_col))}{row_i}"
                note_val = str(c_cells.get(note_coord, {}).get("cached_value") or "").strip()
                if not note_val.startswith("<"):
                    _add_finding(
                        findings, "C05", "actionable",
                        f"{cc.get('subcategory')} at {sh_name}!{cu} lacks '<' source note in rightmost column ({note_coord})",
                        sheet=sh_name, cell=cu,
                    )

            # Normalized style consistency check on touched/added period cells
            if not is_data_sheet:
                continue
            col_s, row_i = coordinate_from_string(cu)
            col_meta = c_cols.get(col_s)
            if row_i <= hdr_r or not col_meta or col_meta.get("kind") not in ("annual", "quarter"):
                continue
            rn = cc.get("role_norm") or {}
            bc = b_mapped_c05.get(cu)
            if bc and bc.get("category") == cc.get("category") and bc.get("role_norm") == rn and col_s not in added_cols_set:
                continue

            # Gather same-row peer cells of matching kind/role (excluding touched cells)
            peer_norms: list[dict[str, Any]] = []
            for oc, om in c_cols.items():
                if oc == col_s or om.get("kind") not in ("annual", "quarter"):
                    continue
                if bool(om.get("is_forecast")) != bool(col_meta.get("is_forecast")):
                    continue
                pc = c_cells.get(f"{oc}{row_i}")
                if pc and pc.get("category") == cc.get("category"):
                    peer_norms.append(pc.get("role_norm") or {})
            if not peer_norms:
                continue

            # 1. Number format category consistency (e.g. % vs number)
            peer_fmts = {_fmt_category(pn.get("num_fmt")) for pn in peer_norms if _fmt_category(pn.get("num_fmt")) != "general"}
            cur_fmt_cat = _fmt_category(rn.get("num_fmt"))
            if len(peer_fmts) == 1 and cur_fmt_cat != "general" and cur_fmt_cat not in peer_fmts:
                _add_finding(
                    findings, "C05", "actionable",
                    f"Style number format mismatch at {sh_name}!{cu}: got {rn.get('num_fmt')!r} ({cur_fmt_cat}), peers use {sorted(peer_fmts)[0]}",
                    sheet=sh_name, cell=cu,
                )

            # 2. Role color / fill consistency
            if cc.get("category") == "input":
                if col_meta.get("is_forecast"):
                    # Forecast driver input row whose peers use yellow fill FFFFC8 + blue font
                    peers_yellow = all(pn.get("fill_rgb") == model_style.ASSUMP_FILL for pn in peer_norms)
                    if peers_yellow and (rn.get("fill_rgb") != model_style.ASSUMP_FILL or rn.get("font_rgb") != model_style.ASSUMP_FONT):
                        _add_finding(
                            findings, "C05", "actionable",
                            f"Forecast driver cell {sh_name}!{cu} style drifted (font={rn.get('font_rgb')}, fill={rn.get('fill_rgb')}); peers use blue/{model_style.ASSUMP_FILL}",
                            sheet=sh_name, cell=cu,
                        )
                else:
                    # Historical actual input whose peers use blue font
                    peers_blue = all(pn.get("font_rgb") == model_style.ACTUAL_FONT for pn in peer_norms)
                    if peers_blue and rn.get("font_rgb") != model_style.ACTUAL_FONT:
                        _add_finding(
                            findings, "C05", "actionable",
                            f"Historical actual cell {sh_name}!{cu} font color {rn.get('font_rgb')} mismatches peer actual color {model_style.ACTUAL_FONT}",
                            sheet=sh_name, cell=cu,
                        )
            elif cc.get("category") == "formula" and (col_meta.get("is_forecast") or col_s in added_cols_set):
                peers_black = all(pn.get("font_rgb") == model_style.FORMULA_FONT for pn in peer_norms)
                if peers_black and rn.get("font_rgb") == model_style.ACTUAL_FONT:
                    _add_finding(
                        findings, "C05", "actionable",
                        f"Forecast/added formula cell {sh_name}!{cu} uses blue input font ({rn.get('font_rgb')}) instead of formula color {model_style.FORMULA_FONT}",
                        sheet=sh_name, cell=cu,
                    )

    # C06 Shifted freeze panes, column widths (added & existing) & row/col outline groups
    for sh_name in target_sheets:
        b_sh = (base_fp.get("sheets") or {}).get(sh_name)
        c_sh = (cand_fp.get("sheets") or {}).get(sh_name)
        if not b_sh or not c_sh:
            continue
        b_pane = b_sh.get("freeze_panes") or {}
        c_pane = c_sh.get("freeze_panes") or {}
        exp_x, exp_y = model_edit.expected_pane_splits(
            cm, sh_name, b_pane.get("x_split", 0), b_pane.get("y_split", 0)
        )
        cur_x, cur_y = int(c_pane.get("x_split", 0)), int(c_pane.get("y_split", 0))
        if (cur_x, cur_y) != (exp_x, exp_y):
            _add_finding(
                findings, "C06", "actionable",
                f"Freeze pane drifted on {sh_name!r}: expected ({exp_x}, {exp_y}), got ({cur_x}, {cur_y})",
                sheet=sh_name,
            )

        b_layout = b_sh.get("column_layout") or {}
        c_layout = c_sh.get("column_layout") or {}
        c_cols = (c_sh.get("period_axis") or {}).get("columns") or {}
        added_cols_c06 = {str(c).upper() for c in (cm["added_period_cols"].get(sh_name) or ())}

        # 1. Existing column widths & column outline groups preserved after shift
        for old_col_l, b_cinfo in b_layout.items():
            old_ci = column_index_from_string(old_col_l)
            mapped = model_edit.map_coord(cm, sh_name, 1, old_ci)
            if mapped is None:
                continue
            new_col_l = get_column_letter(mapped[1])
            if new_col_l in added_cols_c06:
                continue
            c_cinfo = c_layout.get(new_col_l) or {}
            bw = b_cinfo.get("width")
            cw = c_cinfo.get("width")
            if bw is not None and cw is not None and abs(float(cw) - float(bw)) > 0.5:
                _add_finding(
                    findings, "C06", "actionable",
                    f"Existing column width drifted at {sh_name}!{new_col_l} (was {old_col_l}): expected {float(bw):.2f}, got {float(cw):.2f}",
                    sheet=sh_name, cell=f"{new_col_l}1",
                )
            b_ol = int(b_cinfo.get("outline_level") or 0)
            c_ol = int(c_cinfo.get("outline_level") or 0)
            if b_ol > 0 and c_ol != b_ol:
                _add_finding(
                    findings, "C06", "actionable",
                    f"Column outline group lost/changed at {sh_name}!{new_col_l} (was {old_col_l}): expected outlineLevel={b_ol}, got {c_ol}",
                    sheet=sh_name, cell=f"{new_col_l}1",
                )

        # 2. Row outline groups preserved after shift
        b_rgroups = b_sh.get("row_groups") or {}
        c_rgroups = c_sh.get("row_groups") or {}
        for old_r_s, b_rinfo in b_rgroups.items():
            b_ol = int(b_rinfo.get("outline_level") or 0)
            if b_ol <= 0:
                continue
            mapped_r = model_edit.map_coord(cm, sh_name, int(old_r_s), 1)
            if mapped_r is None:
                continue
            new_r = mapped_r[0]
            c_ol = int((c_rgroups.get(str(new_r)) or {}).get("outline_level") or 0)
            if c_ol != b_ol:
                _add_finding(
                    findings, "C06", "actionable",
                    f"Row outline group lost/changed at {sh_name}!row {new_r} (was row {old_r_s}): expected outlineLevel={b_ol}, got {c_ol}",
                    sheet=sh_name, cell=f"A{new_r}",
                )

        # 3. Added period column width consistency (±0.5 per §8.7)
        for added_c in cm["added_period_cols"].get(sh_name) or ():
            ac_u = str(added_c).upper()
            w_new = (c_layout.get(ac_u) or {}).get("width")
            if w_new is None:
                continue
            ac_kind = (c_cols.get(ac_u) or {}).get("kind")
            peer_widths = [
                float((c_layout.get(oc) or {}).get("width"))
                for oc, om in c_cols.items()
                if oc != ac_u and om.get("kind") == ac_kind and (c_layout.get(oc) or {}).get("width") is not None
            ]
            if peer_widths:
                peer_med = sorted(peer_widths)[len(peer_widths) // 2]
                if abs(float(w_new) - peer_med) > 0.5:
                    _add_finding(
                        findings, "C06", "actionable",
                        f"Added period column {sh_name}!{ac_u} width {w_new:.2f} deviates from peer width {peer_med:.2f} (>0.5)",
                        sheet=sh_name, cell=f"{ac_u}1",
                    )

    # C07 Unit label, summary sheet formulas & valuation sheet FY reference sync
    val_sheets = [s for s in target_sheets if any(k in s.lower() for k in ("valuation", "sotp"))]
    if not val_sheets:
        _add_finding(findings, "C07", "not_applicable", "No Valuation / SOTP sheet present")

    # Index target Model sheet period columns for Valuation FY reference verification
    model_col_periods: dict[str, dict[str, str]] = {}
    base_model_col_periods: dict[str, dict[str, str]] = {}
    for m_sh in target_sheets:
        if m_sh.lower().startswith("model"):
            m_cols = ((cand_fp.get("sheets") or {}).get(m_sh, {}).get("period_axis") or {}).get("columns") or {}
            model_col_periods[m_sh] = {
                cl.upper(): str(cm_i.get("normalized") or cm_i.get("raw") or "").upper()
                for cl, cm_i in m_cols.items()
                if cm_i.get("kind") in ("annual", "quarter")
            }
            bm_cols = ((base_fp.get("sheets") or {}).get(m_sh, {}).get("period_axis") or {}).get("columns") or {}
            base_model_col_periods[m_sh] = {
                cl.upper(): str(bm_i.get("normalized") or bm_i.get("raw") or "").upper()
                for cl, bm_i in bm_cols.items()
                if bm_i.get("kind") in ("annual", "quarter")
            }

    for sh_name in target_sheets:
        c_sh = (cand_fp.get("sheets") or {}).get(sh_name)
        b_sh = (base_fp.get("sheets") or {}).get(sh_name)
        if not c_sh:
            continue
        if sh_name.lower().startswith("model") and not (c_sh.get("period_axis") or {}).get("unit_text"):
            b_missing_unit = bool(b_sh and not (b_sh.get("period_axis") or {}).get("unit_text"))
            sev = "baseline_legacy" if (b_missing_unit and mode != "build") else "actionable"
            _add_finding(findings, "C07", sev, f"Missing unit header (A1) on {sh_name!r}", sheet=sh_name, cell="A1")
        if any(k in sh_name.lower() for k in ("key metrics", "valuation", "sotp")):
            for cu in sorted(touched_cells.get(sh_name, set())):
                cc = (c_sh.get("cells") or {}).get(cu)
                _, row_i = coordinate_from_string(cu)
                if cc and row_i > 1 and cc.get("category") == "input" and cc.get("subcategory") not in ("driver_input", "guidance_input"):
                    _add_finding(
                        findings, "C07", "actionable",
                        f"Summary/Valuation cell {sh_name}!{cu} is a constant instead of a cross-sheet formula",
                        sheet=sh_name, cell=cu,
                    )
            # Verify Valuation/SOTP period columns reference matching fiscal years on Model sheets
            if any(k in sh_name.lower() for k in ("valuation", "sotp")):
                v_axis = c_sh.get("period_axis") or {}
                v_cols = v_axis.get("columns") or {}
                v_hdr_r = v_axis.get("header_row") or 1
                bv_cells = (b_sh.get("cells") or {}) if b_sh else {}
                bv_cols = ((b_sh.get("period_axis") or {}).get("columns") or {}) if b_sh else {}
                b_mapped_v: dict[str, tuple[str, dict[str, Any]]] = {}
                for old_vc, bvc in bv_cells.items():
                    nvc = model_edit.map_coord_str(cm, sh_name, old_vc)
                    if nvc:
                        b_mapped_v[nvc.upper()] = (old_vc.upper(), bvc)

                for coord, cc in (c_sh.get("cells") or {}).items():
                    cu = coord.upper()
                    v_col_s, v_row_i = coordinate_from_string(cu)
                    if v_row_i <= v_hdr_r or cc.get("category") != "formula":
                        continue
                    v_cm = v_cols.get(v_col_s)
                    if not v_cm or v_cm.get("kind") not in ("annual", "quarter"):
                        continue
                    v_norm = str(v_cm.get("normalized") or v_cm.get("raw") or "").upper()
                    refs = model_fingerprint.extract_formula_refs(cc.get("formula") or "", sh_name)
                    for rsh, rcrd in refs:
                        if rsh not in model_col_periods:
                            continue
                        rcol_s, _ = coordinate_from_string(rcrd.upper())
                        m_norm = model_col_periods[rsh].get(rcol_s.upper())
                        if m_norm and v_norm and m_norm != v_norm:
                            # Check if baseline already had the exact same FY mismatch
                            was_legacy_fy = False
                            bp = b_mapped_v.get(cu)
                            if bp and cu not in touched_cells.get(sh_name, set()):
                                b_old_c, b_vc = bp
                                b_col_s, _ = coordinate_from_string(b_old_c)
                                b_vnorm = str((bv_cols.get(b_col_s) or {}).get("normalized") or "").upper()
                                b_refs = model_fingerprint.extract_formula_refs(b_vc.get("formula") or "", sh_name)
                                for brsh, brcrd in b_refs:
                                    if brsh in base_model_col_periods:
                                        bm_norm = base_model_col_periods[brsh].get(coordinate_from_string(brcrd.upper())[0].upper())
                                        if bm_norm and b_vnorm and bm_norm != b_vnorm:
                                            was_legacy_fy = True
                            _add_finding(
                                findings, "C07", "baseline_legacy" if was_legacy_fy else "actionable",
                                f"Valuation cell {sh_name}!{cu} ({v_norm}) references {rsh}!{rcrd} ({m_norm}) — fiscal period mismatch",
                                sheet=sh_name, cell=cu,
                            )
                            break

    # C08 Newly disclosed period required items, historical preservation & forecast blank discipline
    for sh_name in target_sheets:
        b_sh = (base_fp.get("sheets") or {}).get(sh_name)
        c_sh = (cand_fp.get("sheets") or {}).get(sh_name)
        if not c_sh:
            continue
        c_cells = c_sh.get("cells") or {}
        c_axis = c_sh.get("period_axis") or {}
        c_cols = c_axis.get("columns") or {}
        hdr_r = c_axis.get("header_row") or 1
        note_col = c_axis.get("source_note_col")

        if b_sh:
            b_cols = (b_sh.get("period_axis") or {}).get("columns") or {}
            for old_coord, bc in (b_sh.get("cells") or {}).items():
                col_s, row_i = coordinate_from_string(old_coord)
                if row_i <= hdr_r:
                    continue
                bm = b_cols.get(col_s)
                if bm and bm.get("kind") in ("annual", "quarter") and not bm.get("is_forecast"):
                    if bc.get("cached_value") is not None:
                        nc = model_edit.map_coord_str(cm, sh_name, old_coord)
                        if nc and nc.upper() not in c_cells:
                            _add_finding(
                                findings, "C08", "actionable",
                                f"Historical value at {sh_name}!{nc} (was {old_coord}) was cleared",
                                sheet=sh_name, cell=nc,
                            )

        # Newly disclosed period (rollover_actual_cols) required items check
        roll_cols_c08 = [str(c).upper() for c in (cm["rollover_actual_cols"].get(sh_name) or ())]
        if roll_cols_c08 and sh_name.lower().startswith("model"):
            anchors_c08 = c_sh.get("semantic_anchors") or {}
            core_rows = {
                int(r) for r in (
                    anchors_c08.get("total_revenue_row"),
                    anchors_c08.get("gross_profit_row"),
                    anchors_c08.get("operating_income_row"),
                    anchors_c08.get("net_income_row"),
                    anchors_c08.get("eps_row"),
                    anchors_c08.get("total_assets_row"),
                    anchors_c08.get("ending_cash_row"),
                )
                if r is not None
            }
            q_cols_sorted = sorted(
                [(int(m["col_index"]), cl) for cl, m in c_cols.items() if m.get("kind") == "quarter"],
                key=lambda x: x[0],
            )
            q_col_pos = {cl: idx for idx, (_, cl) in enumerate(q_cols_sorted)}
            lim_c08 = c_sh.get("line_item_map") or {}
            for rc in roll_cols_c08:
                pos = q_col_pos.get(rc)
                prev_q = q_cols_sorted[pos - 1][1] if (pos is not None and pos > 0) else None
                for row_s, rmeta in lim_c08.items():
                    r_idx = int(row_s)
                    if r_idx <= hdr_r:
                        continue
                    # Only require rows that were populated in the immediately prior quarter
                    if prev_q and f"{prev_q}{r_idx}" not in c_cells:
                        continue
                    cur_c = c_cells.get(f"{rc}{r_idx}")
                    if cur_c is None or (cur_c.get("formula") is None and cur_c.get("cached_value") is None):
                        has_note = False
                        if note_col:
                            nv = str(c_cells.get(f"{get_column_letter(int(note_col))}{r_idx}", {}).get("cached_value") or "").strip()
                            has_note = bool(nv)
                        if r_idx in core_rows:
                            _add_finding(
                                findings, "C08", "actionable",
                                f"Core newly disclosed line item {rmeta.get('label')!r} is blank at {sh_name}!{rc}{r_idx}",
                                sheet=sh_name, cell=f"{rc}{r_idx}",
                            )
                        elif not rmeta.get("is_subtotal_or_ratio") and not has_note:
                            _add_finding(
                                findings, "C08", "missing_evidence",
                                f"Disclosed detail item {rmeta.get('label')!r} is blank in rollover column {sh_name}!{rc}{r_idx} without note",
                                sheet=sh_name, cell=f"{rc}{r_idx}",
                            )

        for cu in sorted(touched_cells.get(sh_name, set())):
            cc = c_cells.get(cu)
            if not cc:
                continue
            col_s, row_i = coordinate_from_string(cu)
            cm_col = c_cols.get(col_s)
            if cm_col and cm_col.get("is_forecast") and cc.get("category") == "input":
                if isinstance(cc.get("cached_value"), (int, float)) and float(cc["cached_value"]) == 0.0:
                    has_note = False
                    if note_col:
                        nv = str(c_cells.get(f"{get_column_letter(int(note_col))}{row_i}", {}).get("cached_value") or "").strip()
                        has_note = bool(nv)
                    if not has_note:
                        _add_finding(
                            findings, "C08", "actionable",
                            f"Bare 0 forecast input without explanatory note at {sh_name}!{cu}",
                            sheet=sh_name, cell=cu,
                        )

    # C09 Balance Sheet balance & cash bridge; C10 Annual=Quarterly; C11 Group=Segments; C12 Cash/Share rollforward
    # Prompt R3 #3 / Codex #5 / Grok #2: compare EVERY discrepancy item-by-item against the shifted baseline;
    # pre-existing discrepancies of equal or smaller magnitude stay baseline_legacy even when another cell in the column was edited!
    def _classify_discrepancy(
        cand_diff: float,
        base_diff: float | None,
        tol: float,
        base_tol: float | None = None,
    ) -> str:
        if base_diff is not None:
            bt = base_tol if base_tol is not None else tol
            if abs(base_diff) > bt and abs(cand_diff) <= abs(base_diff) + max(1e-6, 1e-6 * abs(base_diff)):
                return "baseline_legacy"
        return "actionable"

    for sh_name in target_sheets:
        if not sh_name.lower().startswith("model"):
            continue
        c_sh = (cand_fp.get("sheets") or {}).get(sh_name)
        if not c_sh:
            continue
        c_axis = c_sh.get("period_axis") or {}
        c_cols = c_axis.get("columns") or {}
        unit_scale = float(c_axis.get("unit_scale") or 1.0)
        c_cells = c_sh.get("cells") or {}
        anchors = c_sh.get("semantic_anchors") or {}
        lim = c_sh.get("line_item_map") or {}
        sh_tcols = touched_cols.get(sh_name, set())

        b_sh_m = (base_fp.get("sheets") or {}).get(sh_name)
        b_cells_m: dict[str, dict[str, Any]] = {}
        if b_sh_m:
            for old_c, bc in (b_sh_m.get("cells") or {}).items():
                nc = model_edit.map_coord_str(cm, sh_name, old_c)
                if nc:
                    b_cells_m[nc.upper()] = bc

        def _val(col_l: str, row_idx: int | None) -> float | None:
            if row_idx is None:
                return None
            v = c_cells.get(f"{col_l}{row_idx}", {}).get("cached_value")
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        def _b_val(col_l: str, row_idx: int | None) -> float | None:
            if row_idx is None:
                return None
            v = b_cells_m.get(f"{col_l}{row_idx}", {}).get("cached_value")
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        # --- C09 Balance Sheet ---
        ta_r = anchors.get("total_assets_row")
        tle_r = anchors.get("total_liab_equity_row")
        tl_r = anchors.get("total_liabilities_row")
        te_r = anchors.get("total_equity_row")
        if ta_r is None or (tle_r is None and (tl_r is None or te_r is None)):
            _add_finding(findings, "C09", "missing_evidence", f"Missing Balance Sheet total anchor rows on {sh_name!r}", sheet=sh_name)
        else:
            for col_l, cinfo in c_cols.items():
                if cinfo.get("kind") not in ("annual", "quarter"):
                    continue
                ta = _val(col_l, ta_r)
                tle = _val(col_l, tle_r)
                if tle is None and tl_r is not None and te_r is not None:
                    tl, te = _val(col_l, tl_r), _val(col_l, te_r)
                    if tl is not None and te is not None:
                        tle = tl + te
                if ta is not None and tle is not None:
                    tol = _unit_tolerance(unit_scale, ta)
                    cand_diff = ta - tle
                    if abs(cand_diff) > tol:
                        b_ta = _b_val(col_l, ta_r)
                        b_tle = _b_val(col_l, tle_r)
                        if b_tle is None and tl_r is not None and te_r is not None:
                            btl, bte = _b_val(col_l, tl_r), _b_val(col_l, te_r)
                            if btl is not None and bte is not None:
                                b_tle = btl + bte
                        base_diff = (b_ta - b_tle) if (b_ta is not None and b_tle is not None) else None
                        b_tol = _unit_tolerance(unit_scale, b_ta) if b_ta is not None else tol
                        sev = _classify_discrepancy(cand_diff, base_diff, tol, b_tol)
                        _add_finding(
                            findings, "C09", sev,
                            f"Balance sheet out of balance at {sh_name}!{col_l}: Assets={ta:.2f} vs L+E={tle:.2f} (diff={cand_diff:+.2f})",
                            sheet=sh_name, cell=f"{col_l}{ta_r}",
                        )
        # Cash bridge (CF ending cash vs BS cash) — honor restricted cash/deposit bridge rows or notes
        ec_r, bsc_r = anchors.get("ending_cash_row"), anchors.get("bs_cash_row")
        note_col_m = c_axis.get("source_note_col")
        bridge_rows = [
            int(rs) for rs, rm in lim.items()
            if re.search(r"(?:restricted\s+cash|time\s+deposit|short[\-\s]?term\s+inv|cash\s+bridge|受限现金|定期存款|桥接)", str(rm.get("label") or ""), re.IGNORECASE)
        ]

        def _row_note(row_idx: int | None) -> str:
            if row_idx is None or note_col_m is None:
                return ""
            return str(c_cells.get(f"{get_column_letter(int(note_col_m))}{row_idx}", {}).get("cached_value") or "").strip()

        if ec_r is not None and bsc_r is not None:
            for col_l, cinfo in c_cols.items():
                if cinfo.get("kind") not in ("annual", "quarter"):
                    continue
                ec, bsc = _val(col_l, ec_r), _val(col_l, bsc_r)
                if ec is not None and bsc is not None:
                    tol = _unit_tolerance(unit_scale, max(abs(ec), abs(bsc)))
                    cand_diff = ec - bsc
                    if abs(cand_diff) > tol:
                        # Check if a bridge row reconciles the difference
                        bridge_vals = [_val(col_l, br) for br in bridge_rows]
                        bridge_sum = sum(v for v in bridge_vals if v is not None)
                        reconciled_by_row = bool(
                            bridge_rows
                            and any(v is not None for v in bridge_vals)
                            and (abs(ec - (bsc + bridge_sum)) <= tol or abs(ec - (bsc - bridge_sum)) <= tol)
                        )
                        reconciled_by_note = _has_reconcile_note(_row_note(ec_r)) or _has_reconcile_note(_row_note(bsc_r))
                        if reconciled_by_row or reconciled_by_note:
                            continue
                        b_ec, b_bsc = _b_val(col_l, ec_r), _b_val(col_l, bsc_r)
                        base_diff = (b_ec - b_bsc) if (b_ec is not None and b_bsc is not None) else None
                        b_tol = _unit_tolerance(unit_scale, max(abs(b_ec), abs(b_bsc))) if (b_ec is not None and b_bsc is not None) else tol
                        sev = _classify_discrepancy(cand_diff, base_diff, tol, b_tol)
                        _add_finding(
                            findings, "C09", sev,
                            f"Cash bridge mismatch at {sh_name}!{col_l}: CF Ending Cash={ec:.2f} vs BS Cash={bsc:.2f}",
                            sheet=sh_name, cell=f"{col_l}{bsc_r}",
                        )

        # --- C10 Semantic line-item Annual == Quarterly aggregation (incl. ratio & EPS formulas) ---
        ann_q_map = c_axis.get("annual_sum_quarters") or {}
        for row_s, rmeta in sorted(lim.items(), key=lambda kv: int(kv[0])):
            r_idx = int(row_s)
            rtype = rmeta.get("row_type")
            if rtype == "missing_evidence":
                _add_finding(
                    findings, "C10", "missing_evidence",
                    f"Row {r_idx} ({rmeta.get('label')}) line-item semantic conflict: {rmeta.get('conflict_reason')}",
                    sheet=sh_name, cell=f"D{r_idx}",
                )
                continue
            if rtype in ("not_applicable", "dedup_kpi"):
                continue
            for ann_col, q_cols in ann_q_map.items():
                # Only check annual columns in round scope (annual col or any constituent quarter touched)
                if ann_col not in sh_tcols and not any(q in sh_tcols for q in q_cols):
                    continue
                if rtype in ("ratio", "eps"):
                    ann_coord = f"{ann_col}{r_idx}"
                    ann_cell = c_cells.get(ann_coord)
                    if not ann_cell:
                        continue
                    b_ann = b_cells_m.get(ann_coord)
                    ann_f = str(ann_cell.get("formula") or "")
                    if ann_cell.get("category") != "formula":
                        is_fc_ann = bool((c_cols.get(ann_col) or {}).get("is_forecast"))
                        if is_fc_ann or (ann_coord in touched_cells.get(sh_name, set())):
                            was_legacy_c = bool(b_ann and b_ann.get("category") != "formula" and ann_coord not in touched_cells.get(sh_name, set()))
                            _add_finding(
                                findings, "C10", "baseline_legacy" if was_legacy_c else "actionable",
                                f"Annual {rtype} cell {sh_name}!{ann_coord} ({rmeta.get('label')}) is not a formula",
                                sheet=sh_name, cell=ann_coord,
                            )
                        continue
                    # Reject annual ratio/EPS summing or averaging quarterly cells instead of annual numerator/denominator
                    up_f = ann_f.upper().replace("$", "").replace(" ", "")
                    q1, q4 = q_cols[0].upper(), q_cols[-1].upper()
                    bad_q_agg = (
                        f"SUM({q1}{r_idx}:{q4}{r_idx})" in up_f
                        or f"AVERAGE({q1}{r_idx}:{q4}{r_idx})" in up_f
                        or f"AVG({q1}{r_idx}:{q4}{r_idx})" in up_f
                        or (all(f"{q.upper()}{r_idx}" in up_f for q in q_cols) and "+" in up_f and "/" not in up_f)
                    )
                    if bad_q_agg:
                        was_legacy_agg = bool(
                            b_ann
                            and _norm_formula(b_ann.get("formula")) == _norm_formula(ann_f)
                            and ann_coord not in touched_cells.get(sh_name, set())
                        )
                        _add_finding(
                            findings, "C10", "baseline_legacy" if was_legacy_agg else "actionable",
                            f"Annual {rtype} cell {sh_name}!{ann_coord} ({rmeta.get('label')}) aggregates quarters ({ann_f}) instead of computing from annual column",
                            sheet=sh_name, cell=ann_coord,
                        )
                    continue

                ann_val = _val(ann_col, r_idx)
                q_vals = [_val(q, r_idx) for q in q_cols]
                if ann_val is None or any(v is None for v in q_vals):
                    continue
                q_floats = [float(v) for v in q_vals if v is not None]
                b_ann_val = _b_val(ann_col, r_idx)
                b_q_vals = [_b_val(q, r_idx) for q in q_cols]
                has_base_q = b_ann_val is not None and all(v is not None for v in b_q_vals)
                b_q_floats = [float(v) for v in b_q_vals if v is not None] if has_base_q else []

                if rtype == "flow":
                    q_sum = sum(q_floats)
                    tol = _unit_tolerance(unit_scale, q_sum)
                    cand_diff = ann_val - q_sum
                    if abs(cand_diff) > tol:
                        base_diff = (b_ann_val - sum(b_q_floats)) if has_base_q and b_ann_val is not None else None
                        b_tol = _unit_tolerance(unit_scale, sum(b_q_floats)) if has_base_q else tol
                        sev = _classify_discrepancy(cand_diff, base_diff, tol, b_tol)
                        _add_finding(
                            findings, "C10", sev,
                            f"Flow annual sum mismatch at {sh_name}!{ann_col}{r_idx} ({rmeta.get('label')}): annual={ann_val:.2f} vs sum({q_cols})={q_sum:.2f}",
                            sheet=sh_name, cell=f"{ann_col}{r_idx}",
                        )
                elif rtype == "ending_balance":
                    q4_val = q_floats[-1]
                    tol = _unit_tolerance(unit_scale, q4_val)
                    cand_diff = ann_val - q4_val
                    if abs(cand_diff) > tol:
                        base_diff = (b_ann_val - b_q_floats[-1]) if has_base_q and b_ann_val is not None else None
                        b_tol = _unit_tolerance(unit_scale, b_q_floats[-1]) if has_base_q else tol
                        sev = _classify_discrepancy(cand_diff, base_diff, tol, b_tol)
                        _add_finding(
                            findings, "C10", sev,
                            f"Ending balance annual mismatch at {sh_name}!{ann_col}{r_idx} ({rmeta.get('label')}): annual={ann_val:.2f} vs Q4({q_cols[-1]})={q4_val:.2f}",
                            sheet=sh_name, cell=f"{ann_col}{r_idx}",
                        )
                elif rtype == "average":
                    q_avg = sum(q_floats) / 4.0
                    tol = _unit_tolerance(unit_scale, q_avg)
                    cand_diff = ann_val - q_avg
                    if abs(cand_diff) > tol:
                        b_avg = sum(b_q_floats) / 4.0 if has_base_q else 0.0
                        base_diff = (b_ann_val - b_avg) if has_base_q and b_ann_val is not None else None
                        b_tol = _unit_tolerance(unit_scale, b_avg) if has_base_q else tol
                        sev = _classify_discrepancy(cand_diff, base_diff, tol, b_tol)
                        _add_finding(
                            findings, "C10", sev,
                            f"Average KPI annual mismatch at {sh_name}!{ann_col}{r_idx}: annual={ann_val:.2f} vs avg={q_avg:.2f}",
                            sheet=sh_name, cell=f"{ann_col}{r_idx}",
                        )

        # --- C11 Group == Segments + Unallocated & Eliminations (Operating Profit & Segment Revenue) ---
        grp_op_r = anchors.get("operating_income_row")
        seg_op_rows = list(anchors.get("segment_op_rows") or [])
        seg_unalloc_r = anchors.get("segment_unalloc_row")
        grp_rev_r = anchors.get("total_revenue_row")
        seg_rev_rows = list(anchors.get("segment_rev_rows") or [])
        seg_rev_elim_r = anchors.get("segment_rev_elim_row")

        if not seg_op_rows and not seg_rev_rows:
            _add_finding(findings, "C11", "not_applicable", f"No segment reporting rows on {sh_name!r}", sheet=sh_name)
        else:
            if seg_op_rows:
                if grp_op_r is None:
                    _add_finding(findings, "C11", "missing_evidence", f"Missing Group Operating Income anchor on {sh_name!r}", sheet=sh_name)
                else:
                    for col_l, cinfo in c_cols.items():
                        if cinfo.get("kind") not in ("annual", "quarter"):
                            continue
                        g_op = _val(col_l, grp_op_r)
                        s_vals = [_val(col_l, sr) for sr in seg_op_rows]
                        if g_op is None or any(v is None for v in s_vals):
                            continue
                        unalloc = _val(col_l, seg_unalloc_r) if seg_unalloc_r is not None else 0.0
                        seg_sum = sum(float(v) for v in s_vals if v is not None) + float(unalloc or 0.0)
                        tol = _unit_tolerance(unit_scale, g_op)
                        cand_diff = g_op - seg_sum
                        if abs(cand_diff) > tol:
                            if _has_reconcile_note(_row_note(grp_op_r)):
                                continue
                            b_g_op = _b_val(col_l, grp_op_r)
                            b_s_vals = [_b_val(col_l, sr) for sr in seg_op_rows]
                            if b_g_op is not None and all(v is not None for v in b_s_vals):
                                b_unalloc = _b_val(col_l, seg_unalloc_r) if seg_unalloc_r is not None else 0.0
                                b_seg_sum = sum(float(v) for v in b_s_vals if v is not None) + float(b_unalloc or 0.0)
                                base_diff: float | None = b_g_op - b_seg_sum
                                b_tol = _unit_tolerance(unit_scale, b_g_op)
                            else:
                                base_diff = None
                                b_tol = tol
                            sev = _classify_discrepancy(cand_diff, base_diff, tol, b_tol)
                            _add_finding(
                                findings, "C11", sev,
                                f"Group vs Segment Operating Profit disconnect at {sh_name}!{col_l}{grp_op_r}: Group={g_op:.2f} vs Segments+Unalloc={seg_sum:.2f} (diff={cand_diff:+.2f})",
                                sheet=sh_name, cell=f"{col_l}{grp_op_r}",
                            )
            if seg_rev_rows and grp_rev_r is not None:
                for col_l, cinfo in c_cols.items():
                    if cinfo.get("kind") not in ("annual", "quarter"):
                        continue
                    g_rev = _val(col_l, grp_rev_r)
                    sr_vals = [_val(col_l, sr) for sr in seg_rev_rows]
                    if g_rev is None or any(v is None for v in sr_vals):
                        continue
                    rev_elim = _val(col_l, seg_rev_elim_r) if seg_rev_elim_r is not None else 0.0
                    seg_rev_sum = sum(float(v) for v in sr_vals if v is not None) + float(rev_elim or 0.0)
                    tol = _unit_tolerance(unit_scale, g_rev)
                    cand_diff = g_rev - seg_rev_sum
                    if abs(cand_diff) > tol:
                        if _has_reconcile_note(_row_note(grp_rev_r)):
                            continue
                        b_g_rev = _b_val(col_l, grp_rev_r)
                        b_sr_vals = [_b_val(col_l, sr) for sr in seg_rev_rows]
                        if b_g_rev is not None and all(v is not None for v in b_sr_vals):
                            b_elim = _b_val(col_l, seg_rev_elim_r) if seg_rev_elim_r is not None else 0.0
                            b_seg_rev_sum = sum(float(v) for v in b_sr_vals if v is not None) + float(b_elim or 0.0)
                            base_diff = b_g_rev - b_seg_rev_sum
                            b_tol = _unit_tolerance(unit_scale, b_g_rev)
                        else:
                            base_diff = None
                            b_tol = tol
                        sev = _classify_discrepancy(cand_diff, base_diff, tol, b_tol)
                        _add_finding(
                            findings, "C11", sev,
                            f"Group vs Segment Revenue disconnect at {sh_name}!{col_l}{grp_rev_r}: Group={g_rev:.2f} vs Segments+Elim={seg_rev_sum:.2f} (diff={cand_diff:+.2f})",
                            sheet=sh_name, cell=f"{col_l}{grp_rev_r}",
                        )

        # --- C12 Cash & Share rollforward ---
        bc_r, nc_r = anchors.get("beginning_cash_row"), anchors.get("net_change_cash_row")
        q_ordered = sorted(
            [(m["col_index"], col_l, m) for col_l, m in c_cols.items() if m.get("kind") == "quarter"],
            key=lambda x: x[0],
        )
        if bc_r is None or nc_r is None or ec_r is None:
            _add_finding(findings, "C12", "missing_evidence", f"Missing cash rollforward anchors on {sh_name!r}", sheet=sh_name)
        else:
            for idx_q, (_, col_l, cinfo) in enumerate(q_ordered):
                bc, nc_v, ec = _val(col_l, bc_r), _val(col_l, nc_r), _val(col_l, ec_r)
                if bc is not None and nc_v is not None and ec is not None:
                    tol = _unit_tolerance(unit_scale, ec)
                    cand_diff = (bc + nc_v) - ec
                    if abs(cand_diff) > tol:
                        b_bc, b_nc, b_ec = _b_val(col_l, bc_r), _b_val(col_l, nc_r), _b_val(col_l, ec_r)
                        base_diff = ((b_bc + b_nc) - b_ec) if (b_bc is not None and b_nc is not None and b_ec is not None) else None
                        b_tol = _unit_tolerance(unit_scale, b_ec) if b_ec is not None else tol
                        sev = _classify_discrepancy(cand_diff, base_diff, tol, b_tol)
                        _add_finding(
                            findings, "C12", sev,
                            f"Cash rollforward broken at {sh_name}!{col_l}{ec_r}: Beg({bc:.2f}) + Chg({nc_v:.2f}) != End({ec:.2f})",
                            sheet=sh_name, cell=f"{col_l}{ec_r}",
                        )
                if idx_q > 0:
                    prev_col_l = q_ordered[idx_q - 1][1]
                    prev_ec = _val(prev_col_l, ec_r)
                    if bc is not None and prev_ec is not None:
                        tol = _unit_tolerance(unit_scale, prev_ec)
                        cand_diff = bc - prev_ec
                        if abs(cand_diff) > tol:
                            b_bc = _b_val(col_l, bc_r)
                            b_prev_ec = _b_val(prev_col_l, ec_r)
                            base_diff = (b_bc - b_prev_ec) if (b_bc is not None and b_prev_ec is not None) else None
                            b_tol = _unit_tolerance(unit_scale, b_prev_ec) if b_prev_ec is not None else tol
                            sev = _classify_discrepancy(cand_diff, base_diff, tol, b_tol)
                            _add_finding(
                                findings, "C12", sev,
                                f"Beginning cash discontinuity at {sh_name}!{col_l}{bc_r}: Beg({bc:.2f}) != Prior End({prev_ec:.2f})",
                                sheet=sh_name, cell=f"{col_l}{bc_r}",
                            )

        # Share rollforward (explicit beg/change/end rows per §6.1 C12 & §8.7)
        bs_r = anchors.get("beginning_shares_row")
        es_r = anchors.get("ending_shares_row")
        sc_rows = list(anchors.get("share_change_rows") or [])
        if bs_r is not None and es_r is not None:
            for idx_q, (_, col_l, cinfo) in enumerate(q_ordered):
                bs_v, es_v = _val(col_l, bs_r), _val(col_l, es_r)
                if idx_q > 0:
                    prev_col_l = q_ordered[idx_q - 1][1]
                    prev_es = _val(prev_col_l, es_r)
                    if bs_v is not None and prev_es is not None:
                        s_tol = max(1e-4 * abs(prev_es), 1e-6)
                        cand_diff = bs_v - prev_es
                        if abs(cand_diff) > s_tol and not _has_reconcile_note(_row_note(bs_r)):
                            b_bs_v, b_prev_es = _b_val(col_l, bs_r), _b_val(prev_col_l, es_r)
                            base_diff = (b_bs_v - b_prev_es) if (b_bs_v is not None and b_prev_es is not None) else None
                            b_tol = max(1e-4 * abs(b_prev_es), 1e-6) if b_prev_es is not None else s_tol
                            sev = _classify_discrepancy(cand_diff, base_diff, s_tol, b_tol)
                            _add_finding(
                                findings, "C12", sev,
                                f"Beginning shares discontinuity at {sh_name}!{col_l}{bs_r}: Beg({bs_v:.2f}) != Prior End({prev_es:.2f})",
                                sheet=sh_name, cell=f"{col_l}{bs_r}",
                            )
                if sc_rows and bs_v is not None and es_v is not None:
                    chg_vals = [_val(col_l, r_sc) for r_sc in sc_rows]
                    if all(v is not None for v in chg_vals):
                        chg_sum = sum(float(v) for v in chg_vals if v is not None)
                        s_tol = max(1e-4 * abs(bs_v), 1e-6)
                        cand_diff = (bs_v + chg_sum) - es_v
                        if abs(cand_diff) > s_tol and not _has_reconcile_note(_row_note(es_r)):
                            b_bs_v, b_es_v = _b_val(col_l, bs_r), _b_val(col_l, es_r)
                            b_chg_vals = [_b_val(col_l, r_sc) for r_sc in sc_rows]
                            if b_bs_v is not None and b_es_v is not None and all(v is not None for v in b_chg_vals):
                                base_diff = (b_bs_v + sum(float(v) for v in b_chg_vals if v is not None)) - b_es_v
                                b_tol = max(1e-4 * abs(b_bs_v), 1e-6)
                            else:
                                base_diff = None
                                b_tol = s_tol
                            sev = _classify_discrepancy(cand_diff, base_diff, s_tol, b_tol)
                            _add_finding(
                                findings, "C12", sev,
                                f"Share rollforward broken at {sh_name}!{col_l}{es_r}: Beg({bs_v:.2f}) + Chg({chg_sum:+.2f}) != End({es_v:.2f})",
                                sheet=sh_name, cell=f"{col_l}{es_r}",
                            )

    # C13 fullCalcOnLoad, exempt cells, baseline error comparison, downstream diffs & new circular references
    base_full_calc = bool((base_fp.get("calc_properties") or {}).get("full_calc_on_load"))
    cand_full_calc = bool((cand_fp.get("calc_properties") or {}).get("full_calc_on_load"))
    if not base_full_calc and cand_full_calc:
        _add_finding(
            findings, "C13", "actionable",
            "Workbook <calcPr> has fullCalcOnLoad=1 enabled (was disabled in baseline)",
        )

    # Build exempt cell set strictly from CANDIDATE formulas (Codex R2 #4):
    # never grant exemption to a coordinate whose original CIQ/ext formula was replaced with e.g. =1/0!
    mapped_base_sccs: set[frozenset[str]] = set()
    for scc in base_fp.get("circular_baseline") or []:
        mapped_nodes: set[str] = set()
        for node in scc:
            if "!" in node:
                sh_n, crd = node.rsplit("!", 1)
                nc = model_edit.map_coord_str(cm, sh_n, crd)
                if nc:
                    mapped_nodes.add(f"{sh_n}!{nc.upper()}")
        if mapped_nodes:
            mapped_base_sccs.add(frozenset(mapped_nodes))

    exempt_nodes: set[tuple[str, str]] = set()
    for item in (cand_fp.get("external_links_baseline") or {}).get("cells") or []:
        sh_e = item.get("sheet")
        crd_e = item.get("cell")
        if sh_e and crd_e:
            exempt_nodes.add((str(sh_e), str(crd_e).upper()))
    for sh_name, c_sh in (cand_fp.get("sheets") or {}).items():
        for coord, c_cell in (c_sh.get("cells") or {}).items():
            if c_cell.get("category") == "formula" and c_cell.get("subcategory") in ("plugin", "external_link"):
                exempt_nodes.add((sh_name, coord.upper()))
    for scc in cand_fp.get("circular_baseline") or []:
        scc_set = frozenset(scc)
        if scc_set in mapped_base_sccs or any(scc_set <= b for b in mapped_base_sccs):
            for node in scc:
                if "!" in node:
                    sh_n, crd = node.rsplit("!", 1)
                    exempt_nodes.add((sh_n, crd.upper()))
    exempt_idx = _index_target_coords(exempt_nodes)

    # Build shifted baseline error inventory (Principle #1: pre-existing baseline errors -> baseline_legacy!)
    base_cached_err_nodes: dict[tuple[str, str], str] = {}
    base_ref_formula_nodes: set[tuple[str, str]] = set()
    for sh_name, b_sh in (base_fp.get("sheets") or {}).items():
        for old_coord, b_cell in (b_sh.get("cells") or {}).items():
            if b_cell.get("category") != "formula":
                continue
            nc = model_edit.map_coord_str(cm, sh_name, old_coord)
            if not nc:
                continue
            node = (sh_name, nc.upper())
            if _cell_is_error(b_cell):
                base_cached_err_nodes[node] = str(b_cell.get("cached_value") or "#ERROR!")
            if _formula_has_ref_literal(b_cell.get("formula")):
                base_ref_formula_nodes.add(node)

    # Circular reference diff
    for scc in cand_fp.get("circular_baseline") or []:
        scc_set = frozenset(scc)
        if scc_set in mapped_base_sccs or any(scc_set <= b for b in mapped_base_sccs):
            _add_finding(
                findings, "C13", "baseline_legacy",
                f"Baseline circular reference preserved: {', '.join(scc[:5])}",
            )
        else:
            _add_finding(
                findings, "C13", "actionable",
                f"New circular reference introduced: {', '.join(scc[:5])}",
            )

    # Downstream-of-exempt cache deviations (e.g. =IFERROR(F45, 0) where LO computed 0 while donor kept 38.5)
    for ddiff in (cm.get("recalc_result") or {}).get("downstream_exempt_diffs") or []:
        dsh = str(ddiff.get("sheet") or "")
        dcrd = str(ddiff.get("cell") or "").upper()
        _add_finding(
            findings, "C13", "baseline_legacy",
            f"Downstream-of-exempt cache deviation preserved donor value at {dsh}!{dcrd}: donor={ddiff.get('donor_value')!r} vs recalc={ddiff.get('recalc_value')!r}",
            sheet=dsh, cell=dcrd,
        )

    # Formula errors (cached or recalc_result) + #REF! inside IFERROR
    recalc_errors = (cm.get("recalc_result") or {}).get("errors") or []
    reported_err_nodes: set[tuple[str, str]] = set()
    touched_formula_map: dict[str, set[str]] = {
        str(sh): {str(c).upper() for c in (cs or ())}
        for sh, cs in (cm.get("touched_formula_cells") or {}).items()
    }

    def _classify_error(
        sh: str,
        coord: str,
        err_code: str,
        formula_txt: str,
        *,
        is_ref_in_formula: bool = False,
        kept_donor_cache: bool = False,
        touched_formula: bool | None = None,
    ) -> None:
        cu = coord.upper()
        node = (sh, cu)
        if node in reported_err_nodes:
            return
        reported_err_nodes.add(node)
        is_exempt = (
            (sh not in target_sheets)
            or (node in exempt_nodes)
            or (_reaches_any(node, exempt_nodes, dep_graph, rect_graph, exempt_idx) is not None)
        )
        if is_exempt:
            _add_finding(
                findings, "C13", "baseline_legacy",
                f"Exempt/downstream-of-exempt formula error {err_code} at {sh}!{cu}",
                sheet=sh, cell=cu,
            )
            return
        # Unmodified downstream formula whose baseline non-empty <v> was preserved when LibreOffice errored
        # (e.g. XIRR/XLOOKUP unsupported in LO after driver edit; Prompt R3 #2 / Grok #1)
        is_touched_f = bool(
            touched_formula
            if touched_formula is not None
            else (cu in touched_formula_map.get(sh, set()) or cu in touched_cells.get(sh, set()))
        )
        if kept_donor_cache and not is_touched_f and cu not in touched_cells.get(sh, set()):
            _add_finding(
                findings, "C13", "baseline_legacy",
                f"Unmodified downstream formula LibreOffice error {err_code} at {sh}!{cu} (baseline <v> preserved)",
                sheet=sh, cell=cu,
            )
            return
        is_preexisting = (
            (is_ref_in_formula and node in base_ref_formula_nodes)
            or (not is_ref_in_formula and node in base_cached_err_nodes)
        )
        if is_preexisting and cu not in touched_cells.get(sh, set()):
            _add_finding(
                findings, "C13", "baseline_legacy",
                f"Pre-existing baseline formula error {err_code} at {sh}!{cu}",
                sheet=sh, cell=cu,
            )
            return
        _add_finding(
            findings, "C13", "actionable",
            f"New formula error {err_code} at {sh}!{cu} ({formula_txt[:60]})",
            sheet=sh, cell=cu,
        )

    for re_item in recalc_errors:
        _classify_error(
            str(re_item.get("sheet") or ""),
            str(re_item.get("cell") or ""),
            str(re_item.get("error") or "#ERROR!"),
            str(re_item.get("formula") or ""),
            kept_donor_cache=bool(re_item.get("kept_donor_cache")),
            touched_formula=(
                bool(re_item["touched_formula"])
                if "touched_formula" in re_item and re_item["touched_formula"] is not None
                else None
            ),
        )

    for sh_name, sh_data in (cand_fp.get("sheets") or {}).items():
        if sh_name not in target_sheets:
            continue
        for coord, c_cell in (sh_data.get("cells") or {}).items():
            if c_cell.get("category") != "formula":
                continue
            f_txt = str(c_cell.get("formula") or "")
            cv = c_cell.get("cached_value")
            if _cell_is_error(c_cell):
                _classify_error(sh_name, coord, str(cv or "#ERROR!"), f_txt)
            elif _formula_has_ref_literal(f_txt):
                _classify_error(sh_name, coord, "#REF! (in formula)", f_txt, is_ref_in_formula=True)

    # C14 Multi-company non-target sheets preserved:
    # Independently reject ANY structural op targeting a non-target sheet and compare candidate
    # directly against TRUE baseline (only permitting cross-sheet formula shifts from target-sheet ops; Codex R2 #3).
    if not non_target_sheets:
        _add_finding(findings, "C14", "not_applicable", "Single-company workbook (no non-target company sheets)")
    else:
        non_target_set = set(non_target_sheets)
        all_base_sheets = [s["name"] for s in (base_fp.get("sheet_order") or [])]
        ops = list(cm.get("ops") or [])
        target_ops: list[dict[str, Any]] = []
        for op in ops:
            op_sh = str(op.get("sheet") or "")
            if op_sh in non_target_set:
                _add_finding(
                    findings, "C14", "actionable",
                    f"Illegal structural op {op.get('op')!r} targeted non-target company sheet {op_sh!r}",
                    sheet=op_sh,
                )
            else:
                target_ops.append(op)

        for nt_sh in non_target_sheets:
            orig_cells = (base_fp.get("sheets") or {}).get(nt_sh, {}).get("cells") or {}
            act_cells = (cand_fp.get("sheets") or {}).get(nt_sh, {}).get("cells") or {}

            all_coords = sorted(set(orig_cells) | set(act_cells))
            for coord in all_coords:
                oc = orig_cells.get(coord)
                ac = act_cells.get(coord)
                if oc is None or ac is None:
                    _add_finding(
                        findings, "C14", "actionable",
                        f"Non-target sheet cell added/removed at {nt_sh}!{coord}",
                        sheet=nt_sh, cell=coord,
                    )
                    continue
                if oc.get("category") != ac.get("category"):
                    _add_finding(
                        findings, "C14", "actionable",
                        f"Non-target sheet cell category changed at {nt_sh}!{coord}",
                        sheet=nt_sh, cell=coord,
                    )
                    continue
                if oc.get("category") == "formula":
                    orig_f = _norm_formula(oc.get("formula"))
                    expected_f = (
                        _norm_formula(_replay_formula_through_ops(orig_f, nt_sh, all_base_sheets, target_ops))
                        if target_ops
                        else orig_f
                    )
                    act_f = _norm_formula(ac.get("formula"))
                    if act_f != expected_f:
                        _add_finding(
                            findings, "C14", "actionable",
                            f"Non-target sheet formula modified at {nt_sh}!{coord}: expected {expected_f!r}, got {act_f!r}",
                            sheet=nt_sh, cell=coord,
                        )
                    if not _num_close(oc.get("cached_value"), ac.get("cached_value"), rel_tol=1e-9):
                        _add_finding(
                            findings, "C14", "actionable",
                            f"Non-target sheet cached <v> changed/lost at {nt_sh}!{coord}: expected {oc.get('cached_value')!r}, got {ac.get('cached_value')!r}",
                            sheet=nt_sh, cell=coord,
                        )
                else:
                    if not _num_close(oc.get("cached_value"), ac.get("cached_value"), rel_tol=1e-9):
                        _add_finding(
                            findings, "C14", "actionable",
                            f"Non-target sheet constant modified at {nt_sh}!{coord}: expected {oc.get('cached_value')!r}, got {ac.get('cached_value')!r}",
                            sheet=nt_sh, cell=coord,
                        )

    # Optional 1-line notice for extLst/x14 or media/drawings (never actionable!)
    if baseline_path is not None and Path(baseline_path).is_file():
        pdiff = model_style.diff_workbook_parts(baseline_path, cand)
        if pdiff.get("has_losses"):
            optional_notices.append(str(pdiff.get("summary") or "Package parts/x14 CF changed"))

    counts = {
        "actionable": sum(1 for f in findings if f["severity_class"] == "actionable"),
        "baseline_legacy": sum(1 for f in findings if f["severity_class"] == "baseline_legacy"),
        "missing_evidence": sum(1 for f in findings if f["severity_class"] == "missing_evidence"),
        "not_applicable": sum(1 for f in findings if f["severity_class"] == "not_applicable"),
    }

    return {
        "candidate_path": str(cand),
        "candidate_sha256": cand_sha,
        "baseline_path": base_fp.get("source_path"),
        "baseline_sha256": base_sha,
        "readable": True,
        "calc_engine": "libreoffice",
        "independent_verify": False,
        "findings": findings,
        "counts": counts,
        "optional_notices": optional_notices,
        "dynamic_refs": dynamic_ref_list,
    }


def _render_remaining_issues_md(
    actionable: list[dict[str, Any]],
    failed_stages: list[str],
    candidate_sha256: str | None,
) -> str:
    lines = [
        "> ⚠️ **模型已按「带问题交付」完成版本登记，以下未决事项或降级阶段供审阅参考：**",
    ]
    for item in actionable:
        loc = f"`{item['sheet']}!{item['cell']}`" if (item.get("sheet") and item.get("cell")) else (
            f"`{item['sheet']}`" if item.get("sheet") else ""
        )
        prefix = f"[{item['check_id']} | 本轮可修未消]"
        lines.append(f"> - `{prefix}` {loc} {item['message']}".strip())
    short_sha = (candidate_sha256 or "unknown")[:12]
    for st in failed_stages:
        lines.append(
            f"> - `[阶段降级记录]` 阶段 `{st}` 执行失败或超时（对应候选模型 SHA-256: `{candidate_sha256 or 'unknown'}` / 前12位 `{short_sha}`）"
        )
    return "\n".join(lines) + "\n"


def _render_check_md(
    result: dict[str, Any],
    ticker: str,
    failed_stages: list[str],
    *,
    run_id: str | None = None,
) -> str:
    counts = result["counts"]
    meta_payload = {
        "candidate_sha256": result.get("candidate_sha256"),
        "baseline_sha256": result.get("baseline_sha256"),
        "run_id": run_id,
        "counts": counts,
        "failed_stages": list(failed_stages),
    }
    lines = [
        f"<!-- model-check-meta: {json.dumps(meta_payload, ensure_ascii=False, sort_keys=True)} -->",
        f"# 模型格式与结构检查报告（{ticker.upper()}）",
        "",
        f"- **候选文件**：`{Path(result['candidate_path']).name}`（`sha256: {result['candidate_sha256']}`）",
        f"- **基线文件**：`{Path(result.get('baseline_path') or '').name}`（`sha256: {result.get('baseline_sha256')}`）",
        *([f"- **运行 ID**：`{run_id}`"] if run_id else []),
        f"- **复算引擎**：`{result.get('calc_engine', 'libreoffice')}`（{_SINGLE_ENGINE_LINE}；`independent_verify: false`）",
        "",
        "## 四类问题统计",
        "",
        "| 类别 | 数量 | 说明 |",
        "|---|---:|---|",
        f"| `actionable`（本轮可修） | {counts['actionable']} | 交回建模修改（最多 2 轮） |",
        f"| `baseline_legacy`（基线遗留） | {counts['baseline_legacy']} | 原模型既有项或豁免格下游偏差，不进修改回路 |",
        f"| `missing_evidence`（缺证据） | {counts['missing_evidence']} | 缺锚点、语义冲突或无一致预期源 |",
        f"| `not_applicable`（不适用） | {counts['not_applicable']} | 无法推断子类或单业务无分部等 |",
        "",
    ]
    if failed_stages:
        lines.append(f"- **降级失败阶段**：`{', '.join(failed_stages)}`")
        lines.append("")
    if result["findings"]:
        lines.append("## 检查明细")
        lines.append("")
        for f in result["findings"]:
            loc = f"{f['sheet']}!{f['cell']}" if (f.get("sheet") and f.get("cell")) else (f.get("sheet") or "-")
            lines.append(f"- `[{f['check_id']} | {f['severity_class']}]` `{loc}`: {f['message']}")
        lines.append("")
    if result.get("optional_notices"):
        lines.append("## 可选提示（不产生 actionable）")
        lines.append("")
        for n in result["optional_notices"]:
            lines.append(f"- {n}")
        lines.append("")
    return "\n".join(lines)


def _parse_check_md_meta(check_md_path: Path) -> dict[str, Any] | None:
    """Parse self-recorded ``candidate_sha256`` and 4-class ``counts`` from ``model-check.md``."""
    if not check_md_path.is_file() or check_md_path.stat().st_size == 0:
        return None
    try:
        text = check_md_path.read_text(encoding="utf-8")
    except Exception:
        return None

    # 1. Machine-readable metadata comment written by _render_check_md
    m_meta = re.search(r"<!--\s*model-check-meta:\s*(\{.*?\})\s*-->", text)
    if m_meta:
        try:
            parsed = json.loads(m_meta.group(1))
            if isinstance(parsed, dict) and parsed.get("candidate_sha256"):
                sha = str(parsed["candidate_sha256"]).strip().lower()
                counts = parsed.get("counts") if isinstance(parsed.get("counts"), dict) else None
                return {
                    "candidate_sha256": sha,
                    "run_id": parsed.get("run_id"),
                    "counts": counts,
                }
        except Exception:
            pass

    # 2. Fallback: parse visible markdown candidate sha256 + 4-class counts table
    m_sha = re.search(r"候选文件[^`\n]*`[^`\n]+`[^`\n]*`sha256:\s*([0-9a-fA-F]{64})`", text)
    if not m_sha:
        m_sha = re.search(r"`sha256:\s*([0-9a-fA-F]{64})`", text)
    if not m_sha:
        return None
    sha = m_sha.group(1).lower()
    m_rid = re.search(r"运行\s*ID[^`\n]*`([^`\n]+)`", text)
    parsed_rid = m_rid.group(1).strip() if m_rid else None
    counts_out: dict[str, int] = {}
    for cls in ("actionable", "baseline_legacy", "missing_evidence", "not_applicable"):
        mc = re.search(rf"\|\s*`{cls}`[^|]*\|\s*(\d+)\s*\|", text)
        if mc:
            counts_out[cls] = int(mc.group(1))
    return {
        "candidate_sha256": sha,
        "run_id": parsed_rid,
        "counts": counts_out if len(counts_out) == 4 else None,
    }


def _render_summary_10lines(
    result: dict[str, Any],
    ticker: str,
    consensus_diff: dict[str, Any] | None = None,
) -> str:
    cand_name = Path(result["candidate_path"]).name
    base_name = Path(result.get("baseline_path") or cand_name).name
    base_sha8 = str(result.get("baseline_sha256") or "")[:8]
    cand_sha8 = str(result.get("candidate_sha256") or "")[:8]
    lines = [
        f"1. 模型文件：`{cand_name}`（`{cand_sha8}`）",
        f"2. 基线来源：`{base_name}`（`{base_sha8}`）| 本轮可修问题：`{result['counts']['actionable']}`",
    ]
    if consensus_diff and consensus_diff.get("items"):
        lines.append("3. 核心指标 vs 一致预期：")
        for idx, item in enumerate(consensus_diff["items"][:4], start=4):
            h = f"{item['house']:,.2f}" if item.get("house") is not None else "N/A"
            c = f"{item['consensus']:,.2f}"
            dp = f"{item['diff_pct'] * 100:+.1f}%" if item.get("diff_pct") is not None else "N/A"
            lines.append(f"{idx}. {item['period']} {item['metric']}：本方 {h} | 一致预期 {c}（{dp}）")
    else:
        lines.append("3. 核心指标对比：本次未附带一致预期差异明细。")
    lines.append(f"8. 复算说明：{_SINGLE_ENGINE_LINE}（LibreOffice，independent_verify=false）。")
    return "\n".join(lines)


def _render_review_pack_md(
    result: dict[str, Any],
    ticker: str,
    summary_10lines: str,
) -> str:
    lines = [
        f"# Read-Only Model Review Pack ({ticker.upper()})",
        "",
        f"- **model_sha256**: `{result['candidate_sha256']}`",
        "",
        "## 1. Key Summary",
        summary_10lines,
        "",
        "## 2. Dynamic References (`INDIRECT` / `OFFSET`)",
    ]
    dyn = result.get("dynamic_refs") or []
    if not dyn:
        lines.append("- None")
    else:
        for d in dyn[:30]:
            lines.append(f"- `{d['sheet']}!{d['cell']}`: `{d['formula']}`")
    lines.append("")
    return "\n".join(lines)


def _load_existing_ledger(ledger_path: Path) -> dict[str, Any] | None:
    if ledger_path.is_file():
        try:
            return json.loads(ledger_path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _rel_or_str(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return str(path)


def write_check_and_ledger(
    *,
    workspace_dir: str | Path,
    ticker: str,
    research_dir: str | Path,
    candidate_path: str | Path,
    baseline_path: str | Path | None = None,
    fingerprint_path: str | Path | None = None,
    coord_map_path: str | Path | None = None,
    run_id: str | None = None,
    workflow: str = "company-model",
    mode: str = "update",
    fix_round: int = 0,
    kind: str = "modeling",
    append_revision: bool = False,
    failed_stages: list[str] | None = None,
) -> dict[str, Any]:
    """Run C01-C14, write ``model_dump/{model-check.md,model-review-pack.md,remaining-issues.md,model-verification.json}``."""
    ws = Path(workspace_dir).expanduser().resolve()
    rdir = Path(research_dir).expanduser().resolve()
    dump_dir = rdir / "model_dump"
    dump_dir.mkdir(parents=True, exist_ok=True)
    cand = Path(candidate_path).expanduser().resolve()

    fp = json.loads(Path(fingerprint_path).read_text(encoding="utf-8")) if fingerprint_path else None
    cm = json.loads(Path(coord_map_path).read_text(encoding="utf-8")) if coord_map_path else None
    cons_json_path = dump_dir / "consensus-diff.json"
    cons_md_path = dump_dir / "consensus-diff.md"
    cons_diff = None
    if cons_json_path.is_file():
        try:
            cons_diff = json.loads(cons_json_path.read_text(encoding="utf-8"))
        except Exception:
            cons_diff = None

    res = run_checks(
        candidate_path=cand,
        ticker=ticker,
        baseline_path=baseline_path,
        fingerprint=fp,
        coord_map=cm,
        consensus_diff=cons_diff,
        mode=mode,
    )

    f_stages = [s.strip() for s in (failed_stages or []) if s.strip()]
    recalc_st = ((cm or {}).get("recalc_result") or {}).get("status")
    if recalc_st == "recalc_failed" and "recalc" not in f_stages:
        f_stages.append("recalc")

    # Principle #4: if consensus-diff.{json,md} was not generated for the current candidate,
    # record "consensus" in failed_stages so the run delivers cleanly as delivered_with_open_issues!
    cons_valid = bool(
        cons_diff is not None
        and cons_md_path.is_file()
        and cons_md_path.stat().st_size > 0
        and res.get("candidate_sha256")
        and str(cons_diff.get("model_sha256") or "").lower() == str(res["candidate_sha256"]).lower()
    )
    if not cons_valid and "consensus" not in f_stages:
        f_stages.append("consensus")

    actionable_list = [f for f in res["findings"] if f["severity_class"] == "actionable"]
    check_md_path = dump_dir / "model-check.md"
    review_pack_path = dump_dir / "model-review-pack.md"
    rem_path = dump_dir / "remaining-issues.md"
    ledger_path = dump_dir / "model-verification.json"

    existing = _load_existing_ledger(ledger_path) if (append_revision or kind == "report_revision") else None
    eff_run_id = run_id if run_id is not None else (existing or {}).get("run_id")

    check_md_path.write_text(_render_check_md(res, ticker, f_stages, run_id=eff_run_id), encoding="utf-8")
    summary_10 = _render_summary_10lines(res, ticker, cons_diff if cons_valid else None)
    review_pack_path.write_text(_render_review_pack_md(res, ticker, summary_10), encoding="utf-8")

    has_open = (len(actionable_list) > 0) or (len(f_stages) > 0)
    if not res["readable"]:
        final_status = "failed_unusable"
    elif has_open:
        final_status = "delivered_with_open_issues"
        rem_path.write_text(
            _render_remaining_issues_md(actionable_list, f_stages, res["candidate_sha256"]),
            encoding="utf-8",
        )
    else:
        final_status = "pass"
        if rem_path.exists():
            rem_path.unlink()

    rounds = list((existing or {}).get("rounds") or [])
    prev_fix_rounds = int((existing or {}).get("fix_rounds") or 0)
    eff_fix_rounds = prev_fix_rounds if kind == "report_revision" else max(prev_fix_rounds, int(fix_round))

    evidence: dict[str, Any] = {
        "check": {
            "path": _rel_or_str(check_md_path, ws),
            "evidence_model_sha256": res["candidate_sha256"],
            "counts": res["counts"],
        }
    }
    if cons_valid and cons_diff is not None:
        evidence["consensus"] = {
            "path": _rel_or_str(cons_json_path, ws),
            "md_path": _rel_or_str(cons_md_path, ws),
            "evidence_model_sha256": cons_diff.get("model_sha256"),
            "status": cons_diff.get("status"),
            "exceeds_count": cons_diff.get("exceeds_count", 0),
            "missing_rationale_count": cons_diff.get("missing_rationale_count", 0),
        }

    round_entry = {
        "round_index": len(rounds) + 1,
        "kind": kind,
        "candidate_path": _rel_or_str(cand, ws),
        "model_sha256": res["candidate_sha256"],
        "failed_stages": f_stages,
        "counts": res["counts"],
        "evidence": evidence,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    rounds.append(round_entry)

    ledger = {
        "schema_version": 2,
        "workflow": workflow,
        "ticker": ticker.strip().upper(),
        "run_id": run_id if run_id is not None else (existing or {}).get("run_id"),
        "baseline_path": _rel_or_str(Path(res["baseline_path"]), ws) if res.get("baseline_path") else None,
        "baseline_sha256": res.get("baseline_sha256"),
        "calc_engine": "libreoffice",
        "independent_verify": False,
        "enabled_stages": ["check", "consensus"],
        "fix_rounds": eff_fix_rounds,
        "failed_stages": f_stages,
        "rounds": rounds,
        "final_status": final_status,
        "final_model_path": _rel_or_str(cand, ws) if res["readable"] else None,
        "final_model_sha256": res["candidate_sha256"] if res["readable"] else None,
        "remaining_issues_path": _rel_or_str(rem_path, ws) if has_open else None,
        "summary_10lines": summary_10,
    }
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        **res,
        "final_status": final_status,
        "failed_stages": f_stages,
        "ledger_path": str(ledger_path),
        "check_md_path": str(check_md_path),
        "review_pack_path": str(review_pack_path),
        "remaining_issues_path": str(rem_path) if has_open else None,
        "summary_10lines": summary_10,
        "ledger": ledger,
    }


def write_fallback_ledger(
    *,
    workspace_dir: str | Path,
    ticker: str,
    research_dir: str | Path,
    candidate_path: str | Path,
    kind: str = "modeling",
    failed_stages: list[str] | None = None,
    run_id: str | None = None,
    workflow: str = "company-model",
    baseline_path: str | Path | None = None,
    fix_rounds: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Write ``model-verification.json`` & ``remaining-issues.md`` when stage reports were never created (§3.2)."""
    ws = Path(workspace_dir).expanduser().resolve()
    rdir = Path(research_dir).expanduser().resolve()
    dump_dir = rdir / "model_dump"
    dump_dir.mkdir(parents=True, exist_ok=True)
    cand = Path(candidate_path).expanduser().resolve()

    ok_read, read_msg = model_readable.check_workbook_readable(cand)
    cand_sha = model_fingerprint.file_sha256(cand) if cand.is_file() else None
    base_p = Path(baseline_path).expanduser().resolve() if baseline_path else None
    base_sha = model_fingerprint.file_sha256(base_p) if (base_p and base_p.is_file()) else None

    f_stages = [s.strip() for s in (failed_stages or []) if s.strip()]
    if not f_stages:
        f_stages = ["check"]

    ledger_path = dump_dir / "model-verification.json"
    rem_path = dump_dir / "remaining-issues.md"
    existing = _load_existing_ledger(ledger_path)

    rounds = list((existing or {}).get("rounds") or [])
    prev_fix = int((existing or {}).get("fix_rounds") or 0)
    eff_fix = prev_fix if kind == "report_revision" else (int(fix_rounds) if fix_rounds is not None else prev_fix)

    evidence: dict[str, Any] = {}
    cons_json = dump_dir / "consensus-diff.json"
    cons_md = dump_dir / "consensus-diff.md"
    cons_matched = False
    if "consensus" not in f_stages and cons_json.is_file() and cons_md.is_file() and cons_md.stat().st_size > 0:
        try:
            cd = json.loads(cons_json.read_text(encoding="utf-8"))
            if cand_sha and str(cd.get("model_sha256") or "").lower() == cand_sha.lower():
                evidence["consensus"] = {
                    "path": _rel_or_str(cons_json, ws),
                    "md_path": _rel_or_str(cons_md, ws),
                    "evidence_model_sha256": cd.get("model_sha256"),
                    "status": cd.get("status"),
                }
                cons_matched = True
        except Exception:
            pass
    if "consensus" not in f_stages and not cons_matched:
        f_stages.append("consensus")

    # Never blindly label a stale or cross-run model-check.md as current-run evidence (Codex R2 #8)!
    eff_run_id = run_id if run_id is not None else (existing or {}).get("run_id")
    check_md = dump_dir / "model-check.md"
    check_matched = False
    parsed_check_counts: dict[str, Any] | None = None
    _four_keys = ("actionable", "baseline_legacy", "missing_evidence", "not_applicable")
    if "check" not in f_stages and check_md.is_file() and check_md.stat().st_size > 0:
        parsed_meta = _parse_check_md_meta(check_md)
        if parsed_meta and cand_sha and str(parsed_meta.get("candidate_sha256") or "").lower() == cand_sha.lower():
            rpt_rid = parsed_meta.get("run_id")
            rid_ok = (not eff_run_id) or (rpt_rid == eff_run_id)
            rpt_cnts = parsed_meta.get("counts")
            cnts_ok = isinstance(rpt_cnts, dict) and all(
                k in rpt_cnts and isinstance(rpt_cnts[k], int) for k in _four_keys
            )
            if rid_ok and cnts_ok:
                parsed_check_counts = {k: int(rpt_cnts[k]) for k in _four_keys}
                evidence["check"] = {
                    "path": _rel_or_str(check_md, ws),
                    "evidence_model_sha256": parsed_meta["candidate_sha256"],
                    "run_id": rpt_rid,
                    "counts": parsed_check_counts,
                }
                check_matched = True
    if "check" not in f_stages and not check_matched:
        f_stages.append("check")

    round_entry = {
        "round_index": len(rounds) + 1,
        "kind": kind,
        "candidate_path": _rel_or_str(cand, ws),
        "model_sha256": cand_sha,
        "failed_stages": f_stages,
        **({"counts": parsed_check_counts} if parsed_check_counts is not None else {}),
        "evidence": evidence,
        "reason": reason or "stage timed out or returned empty before generating report",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    rounds.append(round_entry)

    final_status = "delivered_with_open_issues" if ok_read else "failed_unusable"
    rem_text = _render_remaining_issues_md([], f_stages, cand_sha)
    if reason:
        rem_text += f"> - `[补充说明]` {reason}\n"
    if not ok_read:
        rem_text += f"> - `[不可读错误]` {read_msg}\n"
    rem_path.write_text(rem_text, encoding="utf-8")

    ledger = {
        "schema_version": 2,
        "workflow": workflow or (existing or {}).get("workflow") or "company-model",
        "ticker": ticker.strip().upper(),
        "run_id": run_id if run_id is not None else (existing or {}).get("run_id"),
        "baseline_path": (
            _rel_or_str(base_p, ws) if base_p else (existing or {}).get("baseline_path")
        ),
        "baseline_sha256": base_sha or (existing or {}).get("baseline_sha256"),
        "calc_engine": "libreoffice",
        "independent_verify": False,
        "enabled_stages": list((existing or {}).get("enabled_stages") or ["check", "consensus"]),
        "fix_rounds": eff_fix,
        "failed_stages": f_stages,
        "rounds": rounds,
        "final_status": final_status,
        "final_model_path": _rel_or_str(cand, ws) if ok_read else None,
        "final_model_sha256": cand_sha if ok_read else None,
        "remaining_issues_path": _rel_or_str(rem_path, ws),
    }
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        "status": "ok",
        "final_status": final_status,
        "ledger_path": str(ledger_path),
        "remaining_issues_path": str(rem_path),
        "model_sha256": cand_sha,
        "failed_stages": f_stages,
        "ledger": ledger,
    }


def _resolve_ws_path(workspace: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    p = Path(raw)
    return p.resolve() if p.is_absolute() else (workspace / p).resolve()


def verify_done(
    *,
    workspace_dir: str | Path,
    ticker: str,
    research_dir: str | Path,
    expected_run_id: str | None = None,
    manual: bool = False,
) -> dict[str, Any]:
    """Authoritative 4-point closure verifier reading ``<R>/model_dump/model-verification.json`` (§3.2)."""
    ws = Path(workspace_dir).expanduser().resolve()
    rdir = Path(research_dir).expanduser().resolve()
    dump_dir = rdir / "model_dump"
    ledger_path = dump_dir / "model-verification.json"
    errors: list[str] = []

    if not ledger_path.is_file():
        return {"ok": False, "errors": [f"missing terminal ledger: {ledger_path}"]}

    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "errors": [f"corrupt model-verification.json: {exc}"]}

    # 1. run_id & manual downgrade check
    ledger_run_id = ledger.get("run_id")
    if expected_run_id:
        if ledger_run_id != expected_run_id:
            errors.append(
                f"run_id mismatch: expected {expected_run_id!r}, ledger has {ledger_run_id!r}"
            )
    elif not manual and ledger_run_id is None:
        # Allowed if no expected_run_id was enforced, unless caller specifically expects one
        pass

    # 4. fix_rounds (0..2 allowed) & final_status check
    fix_rounds = ledger.get("fix_rounds")
    if not isinstance(fix_rounds, int) or not (0 <= fix_rounds <= 2):
        errors.append(f"invalid fix_rounds={fix_rounds!r} (must be integer in 0..2)")

    final_status = str(ledger.get("final_status") or "")
    if final_status not in ("pass", "delivered_with_open_issues"):
        errors.append(f"terminal final_status={final_status!r} is not deliverable (expected pass or delivered_with_open_issues)")

    # 2. Model path, readability, and live SHA-256 alignment
    final_model_raw = ledger.get("final_model_path")
    final_model_sha = str(ledger.get("final_model_sha256") or "").strip().lower()
    final_model_p = _resolve_ws_path(ws, final_model_raw)
    if final_model_p is None or not final_model_p.is_file():
        errors.append(f"final_model_path missing on disk: {final_model_raw!r}")
    else:
        try:
            rel_model = final_model_p.relative_to(ws).as_posix()
            if not rel_model.startswith("model/"):
                errors.append(f"final_model_path must reside under workspace model/: got {rel_model!r}")
        except ValueError:
            errors.append(f"final_model_path escapes workspace directory: {final_model_p}")
        ok_read, read_msg = model_readable.check_workbook_readable(final_model_p)
        if not ok_read:
            errors.append(f"final_model_path unreadable: {read_msg}")
        else:
            disk_sha = model_fingerprint.file_sha256(final_model_p)
            if disk_sha != final_model_sha:
                errors.append(
                    f"final_model_sha256 mismatch: disk={disk_sha} vs ledger={final_model_sha}"
                )

    # Check remaining-issues.md when delivered_with_open_issues
    rem_path = dump_dir / "remaining-issues.md"
    rem_text = ""
    if final_status == "delivered_with_open_issues":
        if not rem_path.is_file() or rem_path.stat().st_size == 0:
            errors.append(f"delivered_with_open_issues requires non-empty {rem_path}")
        else:
            rem_text = rem_path.read_text(encoding="utf-8")

    # 3. Completed vs failed stages on last round (shared for modeling and report_revision!)
    rounds = list(ledger.get("rounds") or [])
    if not rounds:
        errors.append("ledger rounds[] is empty")
    else:
        last_round = rounds[-1]
        round_failed = list(last_round.get("failed_stages") or [])
        top_failed = list(ledger.get("failed_stages") or [])
        enabled_stages = list(ledger.get("enabled_stages") or ["check", "consensus"])
        evidence = last_round.get("evidence") or {}

        for st in round_failed:
            if st not in top_failed:
                errors.append(f"failed stage {st!r} missing from top-level failed_stages")
            if final_status != "delivered_with_open_issues":
                errors.append(f"round has failed stage {st!r} but final_status is {final_status!r}")
            if rem_text:
                if st not in rem_text:
                    errors.append(f"failed stage {st!r} not recorded in remaining-issues.md")
                if final_model_sha and final_model_sha[:12] not in rem_text:
                    errors.append("candidate model SHA-256 not recorded in remaining-issues.md")

        # Verify each completed enabled stage has real on-disk artifact + self-recorded hash & counts match!
        for st in enabled_stages:
            if st in round_failed or st in top_failed:
                continue
            if st == "check":
                chk_p = dump_dir / "model-check.md"
                if not chk_p.is_file() or chk_p.stat().st_size == 0:
                    errors.append(f"completed stage 'check' missing non-empty {chk_p}")
                else:
                    parsed_chk = _parse_check_md_meta(chk_p)
                    if parsed_chk is None:
                        errors.append(f"model-check.md is missing self-recorded candidate_sha256 metadata: {chk_p}")
                    else:
                        rpt_sha = str(parsed_chk.get("candidate_sha256") or "").lower()
                        if final_model_sha and rpt_sha != final_model_sha:
                            errors.append(
                                f"stale model-check.md: report sha256 ({rpt_sha}) != final_model_sha256 ({final_model_sha})"
                            )
                        # 3-way run_id consistency: expected_run_id, ledger run_id, and report run_id (Codex R2 #8)
                        rpt_rid = parsed_chk.get("run_id")
                        if expected_run_id and rpt_rid != expected_run_id:
                            errors.append(
                                f"stale model-check.md: report run_id ({rpt_rid!r}) != expected_run_id ({expected_run_id!r})"
                            )
                        elif ledger_run_id and rpt_rid != ledger_run_id:
                            errors.append(
                                f"stale model-check.md: report run_id ({rpt_rid!r}) != ledger run_id ({ledger_run_id!r})"
                            )
                        # Strict 4-class counts verification between report and ledger
                        _four_keys = ("actionable", "baseline_legacy", "missing_evidence", "not_applicable")
                        rpt_counts = parsed_chk.get("counts")
                        ledger_counts = (evidence.get("check") or {}).get("counts") or last_round.get("counts")
                        if not isinstance(rpt_counts, dict) or not all(
                            k in rpt_counts and isinstance(rpt_counts[k], int) for k in _four_keys
                        ):
                            errors.append("model-check.md is missing complete 4-class counts metadata")
                        elif not isinstance(ledger_counts, dict) or not all(
                            k in ledger_counts and isinstance(ledger_counts[k], int) for k in _four_keys
                        ):
                            errors.append("ledger is missing complete 4-class counts for completed stage 'check'")
                        else:
                            for k_cnt in _four_keys:
                                if int(rpt_counts[k_cnt]) != int(ledger_counts[k_cnt]):
                                    errors.append(
                                        f"model-check.md count mismatch for {k_cnt}: report={rpt_counts[k_cnt]} vs ledger={ledger_counts[k_cnt]}"
                                    )
                            if final_status == "pass" and int(rpt_counts["actionable"]) != 0:
                                errors.append(
                                    f"final_status='pass' conflicts with model-check.md actionable={rpt_counts['actionable']}"
                                )
                ev_sha = (evidence.get("check") or {}).get("evidence_model_sha256")
                if not ev_sha:
                    errors.append("completed stage 'check' missing evidence_model_sha256 in ledger")
                elif final_model_sha and str(ev_sha).lower() != final_model_sha:
                    errors.append(f"check evidence sha {ev_sha} != final_model_sha256 {final_model_sha}")
            elif st == "consensus":
                cj_p = dump_dir / "consensus-diff.json"
                cm_p = dump_dir / "consensus-diff.md"
                if not cj_p.is_file() or cj_p.stat().st_size == 0 or not cm_p.is_file() or cm_p.stat().st_size == 0:
                    errors.append(f"completed stage 'consensus' missing non-empty consensus-diff.json/.md in {dump_dir}")
                else:
                    try:
                        cd = json.loads(cj_p.read_text(encoding="utf-8"))
                        if final_model_sha and str(cd.get("model_sha256") or "").lower() != final_model_sha:
                            errors.append(
                                f"consensus-diff.json model_sha256 ({cd.get('model_sha256')}) != final_model_sha256 ({final_model_sha})"
                            )
                        cm_text = cm_p.read_text(encoding="utf-8")
                        if final_model_sha and final_model_sha[:12] not in cm_text:
                            errors.append(
                                f"consensus-diff.md does not record final_model_sha256 ({final_model_sha[:12]})"
                            )
                    except Exception as exc:
                        errors.append(f"corrupt consensus-diff.json: {exc}")
                ev_c_sha = (evidence.get("consensus") or {}).get("evidence_model_sha256")
                if not ev_c_sha:
                    errors.append("completed stage 'consensus' missing evidence_model_sha256 in ledger")
                elif final_model_sha and str(ev_c_sha).lower() != final_model_sha:
                    errors.append(f"consensus evidence sha {ev_c_sha} != final_model_sha256 {final_model_sha}")

        # Deep-update note rules (§3.2 item 3)
        if str(ledger.get("workflow") or "") == "earnings-deep-update":
            for note_stage, fname in (("mapping", "mapping.md"), ("model_delta", "model-delta.md")):
                np = rdir / fname
                exists_ok = np.is_file() and np.stat().st_size >= 50
                if final_status == "pass" and not exists_ok:
                    errors.append(f"earnings-deep-update pass requires >=50B {fname}")
                elif final_status == "delivered_with_open_issues" and not exists_ok:
                    if note_stage not in top_failed or note_stage not in rem_text:
                        errors.append(
                            f"missing {fname} on delivered_with_open_issues must record {note_stage!r} in failed_stages and remaining-issues.md"
                        )

    return {
        "ok": len(errors) == 0,
        "final_status": final_status,
        "fix_rounds": fix_rounds,
        "final_model_path": final_model_raw,
        "final_model_sha256": final_model_sha,
        "failed_stages": ledger.get("failed_stages") or [],
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Modeling v2 checker (check / write-ledger / verify-done)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_chk = sub.add_parser("check", help="Run C01-C14 checks and write verification artifacts")
    p_chk.add_argument("--workspace", required=True)
    p_chk.add_argument("--ticker", required=True)
    p_chk.add_argument("--research-dir", required=True)
    p_chk.add_argument("--candidate", required=True)
    p_chk.add_argument("--baseline", default=None)
    p_chk.add_argument("--fingerprint", default=None)
    p_chk.add_argument("--coord-map", default=None)
    p_chk.add_argument("--run-id", default=None)
    p_chk.add_argument("--workflow", default="company-model")
    p_chk.add_argument("--mode", default="update", choices=["build", "update", "auto"])
    p_chk.add_argument("--fix-round", type=int, default=0)
    p_chk.add_argument("--kind", default="modeling", choices=["modeling", "report_revision"])
    p_chk.add_argument("--append-revision", action="store_true")
    p_chk.add_argument("--failed-stages", default="")
    p_chk.add_argument("--summary", action="store_true")

    p_wl = sub.add_parser("write-ledger", help="Write fallback ledger & remaining-issues.md when reports were never generated")
    p_wl.add_argument("--workspace", required=True)
    p_wl.add_argument("--ticker", required=True)
    p_wl.add_argument("--research-dir", required=True)
    p_wl.add_argument("--candidate", required=True)
    p_wl.add_argument("--kind", default="modeling", choices=["modeling", "report_revision"])
    p_wl.add_argument("--failed-stages", required=True)
    p_wl.add_argument("--run-id", default=None)
    p_wl.add_argument("--workflow", default="company-model")
    p_wl.add_argument("--baseline", default=None)
    p_wl.add_argument("--fix-rounds", type=int, default=None)
    p_wl.add_argument("--reason", default=None)

    p_vd = sub.add_parser("verify-done", help="Verify terminal ledger, model readability, and evidence closure")
    p_vd.add_argument("--workspace", required=True)
    p_vd.add_argument("--ticker", required=True)
    p_vd.add_argument("--research-dir", required=True)
    p_vd.add_argument("--expected-run-id", default=None)
    p_vd.add_argument("--manual", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "check":
        f_list = [s.strip() for s in args.failed_stages.split(",") if s.strip()]
        res = write_check_and_ledger(
            workspace_dir=args.workspace,
            ticker=args.ticker,
            research_dir=args.research_dir,
            candidate_path=args.candidate,
            baseline_path=args.baseline,
            fingerprint_path=args.fingerprint,
            coord_map_path=args.coord_map,
            run_id=args.run_id,
            workflow=args.workflow,
            mode=args.mode,
            fix_round=args.fix_round,
            kind=args.kind,
            append_revision=args.append_revision,
            failed_stages=f_list,
        )
        if args.summary:
            print(res["summary_10lines"])
        else:
            out = {k: v for k, v in res.items() if k not in ("ledger",)}
            print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if res["readable"] else 1

    if args.cmd == "write-ledger":
        f_list = [s.strip() for s in args.failed_stages.split(",") if s.strip()]
        res = write_fallback_ledger(
            workspace_dir=args.workspace,
            ticker=args.ticker,
            research_dir=args.research_dir,
            candidate_path=args.candidate,
            kind=args.kind,
            failed_stages=f_list,
            run_id=args.run_id,
            workflow=args.workflow,
            baseline_path=args.baseline,
            fix_rounds=args.fix_rounds,
            reason=args.reason,
        )
        out = {k: v for k, v in res.items() if k != "ledger"}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if res["final_status"] != "failed_unusable" else 1

    if args.cmd == "verify-done":
        res = verify_done(
            workspace_dir=args.workspace,
            ticker=args.ticker,
            research_dir=args.research_dir,
            expected_run_id=args.expected_run_id,
            manual=args.manual,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res["ok"] else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
