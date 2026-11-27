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

from openpyxl.formula.tokenizer import Token, Tokenizer
from openpyxl.utils.cell import column_index_from_string, coordinate_from_string, get_column_letter

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

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
) -> tuple[set[tuple[str, str]], bool]:
    """Return ``(direct_refs, has_dynamic_ref)`` after recursively expanding defined names."""
    has_dyn = model_fingerprint._has_dynamic_ref(expr)
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
    visited_names: set[tuple[str, str]] = set()

    def _walk(cur_expr: str, cur_sh: str) -> None:
        nonlocal has_dyn
        if model_fingerprint._has_dynamic_ref(cur_expr):
            has_dyn = True
        for rsh, rcoord in model_fingerprint.extract_formula_refs(cur_expr, cur_sh):
            refs.add((rsh, rcoord.upper()))
        # Identify defined name tokens via openpyxl Tokenizer + fallback regex
        tokens_to_check: list[str] = []
        try:
            for tok in Tokenizer(cur_expr if cur_expr.startswith("=") else "=" + cur_expr).items:
                if tok.type == Token.OPERAND and tok.subtype == Token.RANGE:
                    val = tok.value.strip()
                    if "!" not in val and ":" not in val:
                        tokens_to_check.append(val)
        except Exception:
            pass
        cleaned = model_fingerprint._EXCEL_STR_RE.sub('""', cur_expr)
        for m in _NAME_TOKEN_RE.finditer(cleaned):
            tok_s = m.group(1)
            if tok_s.upper() not in _EXCEL_BUILTIN_FUNCS:
                tokens_to_check.append(tok_s)

        for tok_s in tokens_to_check:
            key_low = tok_s.lower()
            target_expr = sheet_names.get((cur_sh, key_low)) or wb_names.get(key_low)
            if target_expr is not None:
                vkey = (cur_sh, key_low)
                if vkey not in visited_names:
                    visited_names.add(vkey)
                    _walk(target_expr, cur_sh)

    _walk(expr, current_sheet)
    return refs, has_dyn


def _build_full_dependency_graph(
    cand_fp: dict[str, Any],
) -> tuple[dict[tuple[str, str], set[tuple[str, str]]], dict[tuple[str, str], str]]:
    """Build workbook-wide cell dependency graph (with defined_names expanded) & dynamic-ref map."""
    defined_names = cand_fp.get("defined_names") or []
    graph: dict[tuple[str, str], set[tuple[str, str]]] = {}
    dynamic_cells: dict[tuple[str, str], str] = {}

    for sh_name, sh_data in (cand_fp.get("sheets") or {}).items():
        for coord, cmeta in (sh_data.get("cells") or {}).items():
            if cmeta.get("category") != "formula":
                continue
            f_txt = cmeta.get("formula") or ""
            refs, has_dyn = _expand_defined_names_and_refs(f_txt, sh_name, defined_names)
            node = (sh_name, coord.upper())
            graph[node] = refs
            if has_dyn:
                dynamic_cells[node] = f_txt
    return graph, dynamic_cells


def _reaches_any(
    start: tuple[str, str],
    targets: set[tuple[str, str]],
    graph: dict[tuple[str, str], set[tuple[str, str]]],
) -> tuple[str, str] | None:
    """BFS through formula dependencies to check if ``start`` reaches any node in ``targets``."""
    seen: set[tuple[str, str]] = {start}
    queue = list(graph.get(start, ()))
    while queue:
        cur = queue.pop(0)
        if cur in targets:
            return cur
        if cur in seen:
            continue
        seen.add(cur)
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

    # C02 Period coverage & historical column preservation
    for sh_name in target_sheets:
        b_sh = (base_fp.get("sheets") or {}).get(sh_name)
        c_sh = (cand_fp.get("sheets") or {}).get(sh_name)
        if not c_sh:
            continue
        c_axis = c_sh.get("period_axis") or {}
        c_cols = c_axis.get("columns") or {}
        if b_sh:
            b_cols = (b_sh.get("period_axis") or {}).get("columns") or {}
            cand_raw_periods = {
                str(m.get("normalized") or m.get("raw") or "").upper()
                for m in c_cols.values()
                if m.get("kind") in ("annual", "quarter")
            }
            for col_l, meta in b_cols.items():
                if meta.get("kind") in ("annual", "quarter") and not meta.get("is_forecast"):
                    norm_p = str(meta.get("normalized") or meta.get("raw") or "").upper()
                    if norm_p and norm_p not in cand_raw_periods:
                        _add_finding(
                            findings,
                            "C02",
                            "actionable",
                            f"Historical period column {meta.get('raw')!r} ({col_l}) was removed",
                            sheet=sh_name,
                            cell=f"{col_l}{c_axis.get('header_row') or 1}",
                        )
        # Non-Dec FY label check
        if c_axis.get("fiscal_year_end_month", 12) != 12 and not c_axis.get("fiscal_year_end_explicit"):
            sev = "actionable" if mode == "build" else "baseline_legacy"
            _add_finding(
                findings,
                "C02",
                sev,
                f"Non-December fiscal year (ends month {c_axis.get('fiscal_year_end_month')}) not explicitly noted in header/A2",
                sheet=sh_name,
            )
        # Forecast span check on build or period-expansion rounds
        if sh_name.lower().startswith("model") and (mode == "build" or cm["added_period_cols"].get(sh_name)):
            fc_ann_years = sorted({
                int(m["year"]) for m in c_cols.values()
                if m.get("kind") == "annual" and m.get("is_forecast") and m.get("year")
            })
            fc_q = sorted({
                (int(m["year"]), int(m["quarter"])) for m in c_cols.values()
                if m.get("kind") == "quarter" and m.get("is_forecast") and m.get("year") and m.get("quarter")
            })
            if len(fc_ann_years) < 3:
                _add_finding(
                    findings,
                    "C02",
                    "actionable",
                    f"Annual forecast span covers only {len(fc_ann_years)} years ({fc_ann_years}); require >= 3 full forecast years",
                    sheet=sh_name,
                )
            if fc_q:
                first_fc_yr = fc_q[0][0]
                if (first_fc_yr + 1, 4) not in fc_q:
                    _add_finding(
                        findings,
                        "C02",
                        "actionable",
                        f"Quarterly forecast does not reach next fiscal year Q4 ({first_fc_yr + 1}Q4E)",
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
    dep_graph, dynamic_cells = _build_full_dependency_graph(cand_fp)
    snapshot_nodes: set[tuple[str, str]] = set()
    snapshot_values: list[tuple[tuple[str, str], float]] = []

    for sh_name, sh_data in (cand_fp.get("sheets") or {}).items():
        snap_coords = set((sh_data.get("semantic_anchors") or {}).get("consensus_snapshot_cells") or ())
        snap_coords.update(c.upper() for c in (cm["consensus_snapshot_cells"].get(sh_name) or ()))
        b_sh = (base_fp.get("sheets") or {}).get(sh_name)
        if b_sh:
            for old_sc in (b_sh.get("semantic_anchors") or {}).get("consensus_snapshot_cells") or ():
                nc = model_edit.map_coord_str(cm, sh_name, old_sc)
                if nc:
                    snap_coords.add(nc.upper())
        for sc in snap_coords:
            node = (sh_name, sc.upper())
            snapshot_nodes.add(node)
            cv = (sh_data.get("cells") or {}).get(sc.upper(), {}).get("cached_value")
            if isinstance(cv, (int, float)) and not isinstance(cv, bool):
                snapshot_values.append((node, float(cv)))

    # Check House forecast/result cells on non-consensus sheets
    for sh_name in target_sheets:
        if any(k in sh_name.lower() for k in ("consensus", "readme", "changelog")):
            continue
        sh_data = (cand_fp.get("sheets") or {}).get(sh_name)
        if not sh_data:
            continue
        cols = (sh_data.get("period_axis") or {}).get("columns") or {}
        hdr_r = (sh_data.get("period_axis") or {}).get("header_row") or 1
        anchors = sh_data.get("semantic_anchors") or {}
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
        for coord, c_cell in (sh_data.get("cells") or {}).items():
            cu = coord.upper()
            col_s, row_i = coordinate_from_string(cu)
            if row_i <= hdr_r:
                continue
            col_meta = cols.get(col_s)
            is_fc = bool(col_meta and col_meta.get("is_forecast"))
            if not is_fc and cu not in touched_cells.get(sh_name, set()):
                continue

            if c_cell.get("category") == "formula":
                hit = _reaches_any((sh_name, cu), snapshot_nodes, dep_graph)
                if hit is not None:
                    _add_finding(
                        findings, "C04", "actionable",
                        f"House forecast/driver formula {sh_name}!{cu} depends on consensus snapshot cell {hit[0]}!{hit[1]} (directly or via defined_names)",
                        sheet=sh_name, cell=cu,
                        details={"snapshot_ref": f"{hit[0]}!{hit[1]}", "formula": c_cell.get("formula")},
                    )
            elif c_cell.get("category") == "input" and (row_i in result_rows or c_cell.get("is_subtotal_or_ratio_row")):
                cv = c_cell.get("cached_value")
                if isinstance(cv, (int, float)) and not isinstance(cv, bool):
                    for snap_node, snap_val in snapshot_values:
                        if _num_close(cv, snap_val, rel_tol=1e-6, abs_tol=1e-6):
                            _add_finding(
                                findings, "C04", "actionable",
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

    # Consensus diff rationale & source check
    if consensus_diff is not None:
        if consensus_diff.get("status") == "missing_evidence":
            _add_finding(findings, "C04", "missing_evidence", "No consensus data available for diff comparison")
        elif consensus_diff.get("model_sha256") != cand_sha:
            _add_finding(
                findings, "C04", "actionable",
                f"consensus-diff.json hash ({consensus_diff.get('model_sha256')}) does not match current candidate ({cand_sha})",
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
        note_col = (c_sh.get("period_axis") or {}).get("source_note_col")
        b_comment_coords: set[str] = set()
        for old_c, bc in b_cells.items():
            if bc.get("has_comment"):
                nc = model_edit.map_coord_str(cm, sh_name, old_c)
                if nc:
                    b_comment_coords.add(nc.upper())
        for cu in sorted(touched_cells.get(sh_name, set())):
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

    # C06 Shifted freeze panes, column widths & outline groups
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

        # Added period column width consistency (±0.5 per §8.7)
        c_layout = c_sh.get("column_layout") or {}
        c_cols = (c_sh.get("period_axis") or {}).get("columns") or {}
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

    # C07 Unit label, summary sheet formulas & valuation sheet sync
    val_sheets = [s for s in target_sheets if any(k in s.lower() for k in ("valuation", "sotp"))]
    if not val_sheets:
        _add_finding(findings, "C07", "not_applicable", "No Valuation / SOTP sheet present")
    for sh_name in target_sheets:
        c_sh = (cand_fp.get("sheets") or {}).get(sh_name)
        if not c_sh:
            continue
        if sh_name.lower().startswith("model") and not (c_sh.get("period_axis") or {}).get("unit_text"):
            sev = "actionable" if (mode == "build" or touched_cells.get(sh_name)) else "baseline_legacy"
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

    # C08 Newly disclosed period required items & forecast blank discipline
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

        def _val(col_l: str, row_idx: int | None) -> float | None:
            if row_idx is None:
                return None
            v = c_cells.get(f"{col_l}{row_idx}", {}).get("cached_value")
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
                    if abs(ta - tle) > tol:
                        in_scope_col = col_l in sh_tcols
                        sev = "actionable" if in_scope_col else "baseline_legacy"
                        _add_finding(
                            findings, "C09", sev,
                            f"Balance sheet out of balance at {sh_name}!{col_l}: Assets={ta:.2f} vs L+E={tle:.2f} (diff={ta - tle:+.2f})",
                            sheet=sh_name, cell=f"{col_l}{ta_r}",
                        )
        # Cash bridge (CF ending cash vs BS cash)
        ec_r, bsc_r = anchors.get("ending_cash_row"), anchors.get("bs_cash_row")
        if ec_r is not None and bsc_r is not None:
            for col_l in sh_tcols:
                if col_l not in c_cols:
                    continue
                ec, bsc = _val(col_l, ec_r), _val(col_l, bsc_r)
                if ec is not None and bsc is not None:
                    tol = _unit_tolerance(unit_scale, max(abs(ec), abs(bsc)))
                    if abs(ec - bsc) > tol:
                        _add_finding(
                            findings, "C09", "actionable",
                            f"Cash bridge mismatch at {sh_name}!{col_l}: CF Ending Cash={ec:.2f} vs BS Cash={bsc:.2f}",
                            sheet=sh_name, cell=f"{col_l}{bsc_r}",
                        )

        # --- C10 Semantic line-item Annual == Quarterly aggregation ---
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
                ann_val = _val(ann_col, r_idx)
                q_vals = [_val(q, r_idx) for q in q_cols]
                if ann_val is None or any(v is None for v in q_vals):
                    continue
                q_floats = [float(v) for v in q_vals if v is not None]
                if rtype == "flow":
                    q_sum = sum(q_floats)
                    tol = _unit_tolerance(unit_scale, q_sum)
                    if abs(ann_val - q_sum) > tol:
                        _add_finding(
                            findings, "C10", "actionable",
                            f"Flow annual sum mismatch at {sh_name}!{ann_col}{r_idx} ({rmeta.get('label')}): annual={ann_val:.2f} vs sum({q_cols})={q_sum:.2f}",
                            sheet=sh_name, cell=f"{ann_col}{r_idx}",
                        )
                elif rtype == "ending_balance":
                    q4_val = q_floats[-1]
                    tol = _unit_tolerance(unit_scale, q4_val)
                    if abs(ann_val - q4_val) > tol:
                        _add_finding(
                            findings, "C10", "actionable",
                            f"Ending balance annual mismatch at {sh_name}!{ann_col}{r_idx} ({rmeta.get('label')}): annual={ann_val:.2f} vs Q4({q_cols[-1]})={q4_val:.2f}",
                            sheet=sh_name, cell=f"{ann_col}{r_idx}",
                        )
                elif rtype == "average":
                    q_avg = sum(q_floats) / 4.0
                    tol = _unit_tolerance(unit_scale, q_avg)
                    if abs(ann_val - q_avg) > tol:
                        _add_finding(
                            findings, "C10", "actionable",
                            f"Average KPI annual mismatch at {sh_name}!{ann_col}{r_idx}: annual={ann_val:.2f} vs avg={q_avg:.2f}",
                            sheet=sh_name, cell=f"{ann_col}{r_idx}",
                        )

        # --- C11 Group == Segments + Unallocated & Eliminations ---
        grp_op_r = anchors.get("operating_income_row")
        seg_op_rows = list(anchors.get("segment_op_rows") or [])
        seg_unalloc_r = anchors.get("segment_unalloc_row")
        if not seg_op_rows:
            _add_finding(findings, "C11", "not_applicable", f"No segment reporting rows on {sh_name!r}", sheet=sh_name)
        elif grp_op_r is None:
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
                if abs(g_op - seg_sum) > tol:
                    in_scope_col = col_l in sh_tcols
                    sev = "actionable" if in_scope_col else "baseline_legacy"
                    _add_finding(
                        findings, "C11", sev,
                        f"Group vs Segment Operating Profit disconnect at {sh_name}!{col_l}{grp_op_r}: Group={g_op:.2f} vs Segments+Unalloc={seg_sum:.2f} (diff={g_op - seg_sum:+.2f})",
                        sheet=sh_name, cell=f"{col_l}{grp_op_r}",
                    )

        # --- C12 Cash & Share rollforward ---
        bc_r, nc_r = anchors.get("beginning_cash_row"), anchors.get("net_change_cash_row")
        if bc_r is None or nc_r is None or ec_r is None:
            _add_finding(findings, "C12", "missing_evidence", f"Missing cash rollforward anchors on {sh_name!r}", sheet=sh_name)
        else:
            q_ordered = sorted(
                [(m["col_index"], col_l, m) for col_l, m in c_cols.items() if m.get("kind") == "quarter"],
                key=lambda x: x[0],
            )
            for idx_q, (_, col_l, cinfo) in enumerate(q_ordered):
                if col_l not in sh_tcols:
                    continue
                bc, nc_v, ec = _val(col_l, bc_r), _val(col_l, nc_r), _val(col_l, ec_r)
                if bc is not None and nc_v is not None and ec is not None:
                    tol = _unit_tolerance(unit_scale, ec)
                    if abs((bc + nc_v) - ec) > tol:
                        _add_finding(
                            findings, "C12", "actionable",
                            f"Cash rollforward broken at {sh_name}!{col_l}{ec_r}: Beg({bc:.2f}) + Chg({nc_v:.2f}) != End({ec:.2f})",
                            sheet=sh_name, cell=f"{col_l}{ec_r}",
                        )
                if idx_q > 0:
                    prev_col_l = q_ordered[idx_q - 1][1]
                    prev_ec = _val(prev_col_l, ec_r)
                    if bc is not None and prev_ec is not None:
                        tol = _unit_tolerance(unit_scale, prev_ec)
                        if abs(bc - prev_ec) > tol:
                            _add_finding(
                                findings, "C12", "actionable",
                                f"Beginning cash discontinuity at {sh_name}!{col_l}{bc_r}: Beg({bc:.2f}) != Prior End({prev_ec:.2f})",
                                sheet=sh_name, cell=f"{col_l}{bc_r}",
                            )

    # C13 fullCalcOnLoad, exempt cells, formula errors & new circular references
    base_full_calc = bool((base_fp.get("calc_properties") or {}).get("full_calc_on_load"))
    cand_full_calc = bool((cand_fp.get("calc_properties") or {}).get("full_calc_on_load"))
    if not base_full_calc and cand_full_calc:
        _add_finding(
            findings, "C13", "actionable",
            "Workbook <calcPr> has fullCalcOnLoad=1 enabled (was disabled in baseline)",
        )

    # Build exempt cell set (external links, plugins, mapped baseline circular SCCs)
    exempt_nodes: set[tuple[str, str]] = set()
    for item in (base_fp.get("external_links_baseline") or {}).get("cells") or []:
        nc = model_edit.map_coord_str(cm, item["sheet"], item["cell"])
        if nc:
            exempt_nodes.add((item["sheet"], nc.upper()))
    for sh_name, b_sh in (base_fp.get("sheets") or {}).items():
        for coord, b_cell in (b_sh.get("cells") or {}).items():
            if b_cell.get("category") == "formula" and b_cell.get("subcategory") in ("plugin", "external_link"):
                nc = model_edit.map_coord_str(cm, sh_name, coord)
                if nc:
                    exempt_nodes.add((sh_name, nc.upper()))
    mapped_base_sccs: set[frozenset[str]] = set()
    for scc in base_fp.get("circular_baseline") or []:
        mapped_nodes: set[str] = set()
        for node in scc:
            if "!" in node:
                sh_n, crd = node.rsplit("!", 1)
                nc = model_edit.map_coord_str(cm, sh_n, crd)
                if nc:
                    mapped_nodes.add(f"{sh_n}!{nc.upper()}")
                    exempt_nodes.add((sh_n, nc.upper()))
        if mapped_nodes:
            mapped_base_sccs.add(frozenset(mapped_nodes))

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

    # Formula errors (cached or recalc_result) + #REF! inside IFERROR
    recalc_errors = (cm.get("recalc_result") or {}).get("errors") or []
    reported_err_nodes: set[tuple[str, str]] = set()

    def _classify_error(sh: str, coord: str, err_code: str, formula_txt: str, *, exempt_hint: bool = False) -> None:
        node = (sh, coord.upper())
        if node in reported_err_nodes:
            return
        reported_err_nodes.add(node)
        is_exempt = (
            exempt_hint
            or (sh not in target_sheets)
            or (node in exempt_nodes)
            or (_reaches_any(node, exempt_nodes, dep_graph) is not None)
        )
        if is_exempt:
            _add_finding(
                findings, "C13", "baseline_legacy",
                f"Exempt/downstream-of-exempt formula error {err_code} at {sh}!{coord}",
                sheet=sh, cell=coord,
            )
        else:
            _add_finding(
                findings, "C13", "actionable",
                f"Formula error {err_code} at {sh}!{coord} ({formula_txt[:60]})",
                sheet=sh, cell=coord,
            )

    for re_item in recalc_errors:
        _classify_error(
            str(re_item.get("sheet") or ""),
            str(re_item.get("cell") or ""),
            str(re_item.get("error") or "#ERROR!"),
            str(re_item.get("formula") or ""),
            exempt_hint=bool(re_item.get("exempt")),
        )

    for sh_name, sh_data in (cand_fp.get("sheets") or {}).items():
        if sh_name not in target_sheets:
            continue
        for coord, c_cell in (sh_data.get("cells") or {}).items():
            if c_cell.get("category") != "formula":
                continue
            f_txt = str(c_cell.get("formula") or "")
            cv = c_cell.get("cached_value")
            ct = c_cell.get("cached_t")
            if ct == "e" or (isinstance(cv, str) and (cv in _EXCEL_ERRORS or _ERROR_SHAPE.match(cv))):
                _classify_error(sh_name, coord, str(cv or "#ERROR!"), f_txt)
            elif "#REF!" in model_fingerprint._EXCEL_STR_RE.sub('""', f_txt).upper():
                _classify_error(sh_name, coord, "#REF! (in formula)", f_txt)

    # C14 Multi-company non-target sheets preserved (expected formula = post-insert donor!)
    if not non_target_sheets:
        _add_finding(findings, "C14", "not_applicable", "Single-company workbook (no non-target company sheets)")
    else:
        donor_raw = cm.get("cache_donor_path")
        donor_fp = (
            model_fingerprint.build_fingerprint(donor_raw, ticker=tk)
            if (donor_raw and Path(donor_raw).is_file())
            else base_fp
        )
        all_base_sheets = [s["name"] for s in (base_fp.get("sheet_order") or [])]
        ops = list(cm.get("ops") or [])

        for nt_sh in non_target_sheets:
            exp_cells = (donor_fp.get("sheets") or {}).get(nt_sh, {}).get("cells") or {}
            act_cells = (cand_fp.get("sheets") or {}).get(nt_sh, {}).get("cells") or {}
            orig_cells = (base_fp.get("sheets") or {}).get(nt_sh, {}).get("cells") or {}

            # 1. Verify candidate == post-insert donor cell-by-cell (formulas, constants, <v> cache)
            all_coords = sorted(set(exp_cells) | set(act_cells))
            for coord in all_coords:
                ec = exp_cells.get(coord)
                ac = act_cells.get(coord)
                if ec is None or ac is None:
                    _add_finding(
                        findings, "C14", "actionable",
                        f"Non-target sheet cell added/removed at {nt_sh}!{coord}",
                        sheet=nt_sh, cell=coord,
                    )
                    continue
                if ec.get("category") != ac.get("category"):
                    _add_finding(
                        findings, "C14", "actionable",
                        f"Non-target sheet cell category changed at {nt_sh}!{coord}",
                        sheet=nt_sh, cell=coord,
                    )
                    continue
                if ec.get("category") == "formula":
                    if _norm_formula(ec.get("formula")) != _norm_formula(ac.get("formula")):
                        _add_finding(
                            findings, "C14", "actionable",
                            f"Non-target sheet formula modified at {nt_sh}!{coord}: expected {ec.get('formula')!r}, got {ac.get('formula')!r}",
                            sheet=nt_sh, cell=coord,
                        )
                    if not _num_close(ec.get("cached_value"), ac.get("cached_value"), rel_tol=1e-9):
                        _add_finding(
                            findings, "C14", "actionable",
                            f"Non-target sheet cached <v> changed/lost at {nt_sh}!{coord}: expected {ec.get('cached_value')!r}, got {ac.get('cached_value')!r}",
                            sheet=nt_sh, cell=coord,
                        )
                else:
                    if not _num_close(ec.get("cached_value"), ac.get("cached_value"), rel_tol=1e-9):
                        _add_finding(
                            findings, "C14", "actionable",
                            f"Non-target sheet constant modified at {nt_sh}!{coord}: expected {ec.get('cached_value')!r}, got {ac.get('cached_value')!r}",
                            sheet=nt_sh, cell=coord,
                        )

            # 2. Verify every donor vs original formula change came strictly from cross-sheet Shift.formula
            for coord, ec in exp_cells.items():
                if ec.get("category") != "formula":
                    continue
                oc = orig_cells.get(coord)
                if oc is None or oc.get("category") != "formula":
                    continue
                orig_f = _norm_formula(oc.get("formula"))
                donor_f = _norm_formula(ec.get("formula"))
                if orig_f != donor_f:
                    expected_shifted = _replay_formula_through_ops(orig_f, nt_sh, all_base_sheets, ops)
                    if _norm_formula(expected_shifted) != donor_f:
                        _add_finding(
                            findings, "C14", "actionable",
                            f"Non-shift formula change on non-target sheet {nt_sh}!{coord}: expected shifted {expected_shifted!r}, got {donor_f!r}",
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
) -> str:
    counts = result["counts"]
    lines = [
        f"# 模型格式与结构检查报告（{ticker.upper()}）",
        "",
        f"- **候选文件**：`{Path(result['candidate_path']).name}`（`sha256: {result['candidate_sha256']}`）",
        f"- **基线文件**：`{Path(result.get('baseline_path') or '').name}`（`sha256: {result.get('baseline_sha256')}`）",
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
    cons_diff = json.loads(cons_json_path.read_text(encoding="utf-8")) if cons_json_path.is_file() else None

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

    actionable_list = [f for f in res["findings"] if f["severity_class"] == "actionable"]
    check_md_path = dump_dir / "model-check.md"
    review_pack_path = dump_dir / "model-review-pack.md"
    rem_path = dump_dir / "remaining-issues.md"
    ledger_path = dump_dir / "model-verification.json"

    check_md_path.write_text(_render_check_md(res, ticker, f_stages), encoding="utf-8")
    summary_10 = _render_summary_10lines(res, ticker, cons_diff)
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

    existing = _load_existing_ledger(ledger_path) if (append_revision or kind == "report_revision") else None
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
    if cons_diff is not None:
        evidence["consensus"] = {
            "path": _rel_or_str(cons_json_path, ws),
            "md_path": _rel_or_str(dump_dir / "consensus-diff.md", ws),
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
    if "consensus" not in f_stages and cons_json.is_file():
        try:
            cd = json.loads(cons_json.read_text(encoding="utf-8"))
            evidence["consensus"] = {
                "path": _rel_or_str(cons_json, ws),
                "md_path": _rel_or_str(dump_dir / "consensus-diff.md", ws),
                "evidence_model_sha256": cd.get("model_sha256"),
                "status": cd.get("status"),
            }
        except Exception:
            pass
    check_md = dump_dir / "model-check.md"
    if "check" not in f_stages and check_md.is_file() and check_md.stat().st_size > 0:
        evidence["check"] = {
            "path": _rel_or_str(check_md, ws),
            "evidence_model_sha256": cand_sha,
        }

    round_entry = {
        "round_index": len(rounds) + 1,
        "kind": kind,
        "candidate_path": _rel_or_str(cand, ws),
        "model_sha256": cand_sha,
        "failed_stages": f_stages,
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

        # Verify each completed enabled stage has real on-disk artifact + aligned hash
        for st in enabled_stages:
            if st in round_failed or st in top_failed:
                continue
            if st == "check":
                chk_p = dump_dir / "model-check.md"
                if not chk_p.is_file() or chk_p.stat().st_size == 0:
                    errors.append(f"completed stage 'check' missing non-empty {chk_p}")
                ev_sha = (evidence.get("check") or {}).get("evidence_model_sha256")
                if ev_sha and final_model_sha and str(ev_sha).lower() != final_model_sha:
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
                    except Exception as exc:
                        errors.append(f"corrupt consensus-diff.json: {exc}")

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
