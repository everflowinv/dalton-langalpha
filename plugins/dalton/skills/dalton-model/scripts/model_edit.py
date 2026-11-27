#!/usr/bin/env python3
"""Controlled in-place workbook editor with coord_map, pre-publish donor cache restore, and recalc (modeling v2 §5.1, §8.2, §8.4).

Usage:
    python3 model_edit.py apply \
        --baseline <base.xlsx> \
        --ticker <TICKER> \
        --plan <edit_plan.json> \
        [--out <candidate.xlsx>] \
        [--workspace-dir <dir>] \
        [--fingerprint <fingerprint.json>] \
        [--coord-map-out <coord_map.json>] \
        [--donor-out <pre_openpyxl.xlsx>] \
        [--spec-out <keep_cache_spec.json>] \
        [--expected-baseline-sha256 <64-hex>] \
        [--skip-recalc] [--recalc-timeout 120]

    python3 model_edit.py next-path --workspace-dir <dir> --ticker <TICKER> [--baseline <base.xlsx>] [--naming-mode single_company|sector_workbook]

Standalone script (stdlib + openpyxl + lxml only).
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any
import zipfile

from lxml import etree
import openpyxl
from openpyxl.utils.cell import column_index_from_string, coordinate_from_string, get_column_letter

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import model_fingerprint  # noqa: E402
import model_readable  # noqa: E402
import model_style  # noqa: E402
import model_xlsx_hooks  # noqa: E402

_SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XML = etree.XMLParser(resolve_entities=False, no_network=True)
_SINGLE_V_RE = re.compile(r"^([A-Za-z0-9._-]+)-model-v(\d+)\.xlsx$", re.IGNORECASE)
_GENERIC_V_RE = re.compile(r"^(.*?)([_\-.]?v)(\d+)(\.xlsx)$", re.IGNORECASE)
_DATE_SUFFIX_RE = re.compile(r"^(.*?[_\-.]?)(\d{4}-\d{2}-\d{2})(\.xlsx)$", re.IGNORECASE)


def _locate_xlsx_script(name: str) -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "xlsx" / "scripts" / name,
    ]
    if len(here.parents) > 4:
        candidates.append(
            here.parents[4] / "langalpha_deliverables" / "skills" / "xlsx" / "scripts" / name
        )
    for cand in candidates:
        if cand.is_file():
            return cand
    raise FileNotFoundError(f"Could not locate xlsx script {name} in {candidates}")


def _load_module(mod_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 1. Version-chain output path generator (§3.1 & Core Rule 4)
# ---------------------------------------------------------------------------


def next_model_output_path(
    workspace_dir: str | Path,
    ticker: str,
    *,
    baseline_path: str | Path | None = None,
    naming_mode: str = "auto",
) -> Path:
    """Compute next non-overwriting output path under ``<workspace_dir>/model/``."""
    ws_dir = Path(workspace_dir).expanduser().resolve()
    model_dir = ws_dir / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    tk = ticker.strip().upper()

    resolved_mode = naming_mode
    base_p = Path(baseline_path).expanduser().resolve() if baseline_path else None
    if resolved_mode == "auto":
        if base_p is not None and not _SINGLE_V_RE.match(base_p.name):
            resolved_mode = "sector_workbook"
        else:
            resolved_mode = "single_company"

    if resolved_mode == "single_company":
        max_v = 0
        for p in model_dir.glob(f"{tk}-model-v*.xlsx"):
            m = _SINGLE_V_RE.match(p.name)
            if m and m.group(1).upper() == tk:
                max_v = max(max_v, int(m.group(2)))
        if base_p is not None:
            m = _SINGLE_V_RE.match(base_p.name)
            if m and m.group(1).upper() == tk:
                max_v = max(max_v, int(m.group(2)))
        return model_dir / f"{tk}-model-v{max_v + 1:02d}.xlsx"

    # Sector multi-company workbook: preserve original filename stem and increment version
    if base_p is None:
        return model_dir / f"{tk}-model-v01.xlsx"

    m_ver = _GENERIC_V_RE.match(base_p.name)
    if m_ver:
        prefix, sep_v, num_str, ext = m_ver.groups()
        width = max(2, len(num_str))
        cur_max = int(num_str)
        for p in model_dir.glob(f"{prefix}*{ext}"):
            mp = _GENERIC_V_RE.match(p.name)
            if mp and mp.group(1) == prefix:
                cur_max = max(cur_max, int(mp.group(3)))
        return model_dir / f"{prefix}{sep_v}{cur_max + 1:0{width}d}{ext}"

    stem = base_p.stem
    ext = base_p.suffix or ".xlsx"
    cur_max = 1
    for p in model_dir.glob(f"{stem}_v*{ext}"):
        mp = _GENERIC_V_RE.match(p.name)
        if mp and mp.group(1) == stem:
            cur_max = max(cur_max, int(mp.group(3)))
    return model_dir / f"{stem}_v{cur_max + 1:02d}{ext}"


# ---------------------------------------------------------------------------
# 2. Deterministic coordinate mapping (§5.1 & §8.3)
# ---------------------------------------------------------------------------


def map_coord(
    coord_map: dict[str, Any] | list[dict[str, Any]],
    sheet: str,
    old_row: int,
    old_col: int,
) -> tuple[int, int] | None:
    """Deterministically map ``(old_row, old_col)`` through ordered structural ``ops``."""
    ops = coord_map.get("ops", []) if isinstance(coord_map, dict) else list(coord_map)
    r, c = int(old_row), int(old_col)
    for op in ops:
        if op.get("sheet") != sheet:
            continue
        kind = op.get("op")
        count = int(op.get("count", 1))
        if kind == "insert_columns":
            at_col = (
                column_index_from_string(str(op["at"]))
                if isinstance(op["at"], str) and not str(op["at"]).isdigit()
                else int(op["at"])
            )
            if c >= at_col:
                c += count
        elif kind == "delete_columns":
            at_col = (
                column_index_from_string(str(op["at"]))
                if isinstance(op["at"], str) and not str(op["at"]).isdigit()
                else int(op["at"])
            )
            if at_col <= c < at_col + count:
                return None
            if c >= at_col + count:
                c -= count
        elif kind == "insert_rows":
            at_row = int(op["at"])
            if r >= at_row:
                r += count
        elif kind == "delete_rows":
            at_row = int(op["at"])
            if at_row <= r < at_row + count:
                return None
            if r >= at_row + count:
                r -= count
    return r, c


def map_coord_str(coord_map: dict[str, Any], sheet: str, old_coord: str) -> str | None:
    col_s, row_i = coordinate_from_string(old_coord.upper())
    mapped = map_coord(coord_map, sheet, row_i, column_index_from_string(col_s))
    if mapped is None:
        return None
    nr, nc = mapped
    return f"{get_column_letter(nc)}{nr}"


def expected_pane_splits(
    coord_map: dict[str, Any],
    sheet: str,
    old_x_split: int,
    old_y_split: int,
) -> tuple[int, int]:
    """Compute expected ``(x_split, y_split)`` after insertions/deletions above/left of freeze lines."""
    ops = coord_map.get("ops", [])
    xs, ys = int(old_x_split), int(old_y_split)
    for op in ops:
        if op.get("sheet") != sheet:
            continue
        kind = op.get("op")
        count = int(op.get("count", 1))
        if kind == "insert_columns" and xs > 0:
            at_col = (
                column_index_from_string(str(op["at"]))
                if isinstance(op["at"], str) and not str(op["at"]).isdigit()
                else int(op["at"])
            )
            if at_col <= xs:
                xs += count
        elif kind == "delete_columns" and xs > 0:
            at_col = (
                column_index_from_string(str(op["at"]))
                if isinstance(op["at"], str) and not str(op["at"]).isdigit()
                else int(op["at"])
            )
            if at_col <= xs:
                xs = max(0, xs - min(count, xs - at_col + 1))
        elif kind == "insert_rows" and ys > 0:
            at_row = int(op["at"])
            if at_row <= ys:
                ys += count
        elif kind == "delete_rows" and ys > 0:
            at_row = int(op["at"])
            if at_row <= ys:
                ys = max(0, ys - min(count, ys - at_row + 1))
    return xs, ys


def _extend_cf_sqref_for_appended_cols(xlsx_path: Path, sheet_name: str, added_cols: list[str]) -> None:
    """Extend standard ``<conditionalFormatting sqref>`` ranges when columns are appended at right (§8.8 #3)."""
    if not added_cols:
        return
    added_indices = sorted(column_index_from_string(c) for c in added_cols)
    min_added, max_added = added_indices[0], added_indices[-1]
    infos, payload = model_xlsx_hooks._read_package(xlsx_path)
    part = model_xlsx_hooks._sheet_parts(payload).get(sheet_name)
    if not part or part not in payload:
        return
    root = etree.fromstring(payload[part], _XML)
    changed = False
    rng_re = re.compile(r"^([A-Za-z]{1,3})(\d+):([A-Za-z]{1,3})(\d+)$")
    for cf_el in root.findall(f"{{{_SML}}}conditionalFormatting"):
        sqref = (cf_el.get("sqref") or "").strip()
        if not sqref:
            continue
        new_tokens = []
        for tok in sqref.split():
            m = rng_re.match(tok)
            if m:
                c1, r1, c2, r2 = m.groups()
                c2_i = column_index_from_string(c2)
                if c2_i == min_added - 1:
                    new_tokens.append(f"{c1}{r1}:{get_column_letter(max_added)}{r2}")
                    changed = True
                    continue
            new_tokens.append(tok)
        if changed:
            cf_el.set("sqref", " ".join(new_tokens))
    if changed:
        payload[part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        tmp = xlsx_path.with_name(xlsx_path.name + ".cf.tmp")
        with zipfile.ZipFile(tmp, "w") as zout:
            for info in infos:
                zout.writestr(info, payload[info.filename], compress_type=info.compress_type)
        os.replace(tmp, xlsx_path)


def _preserve_calcpr_from_donor(target_path: Path, donor_path: Path) -> None:
    """Ensure openpyxl's save did not alter ``<calcPr>`` relative to ``donor_path``."""
    with zipfile.ZipFile(donor_path, "r") as zf:
        donor_wb = etree.fromstring(zf.read("xl/workbook.xml"), _XML)
    donor_calc = donor_wb.find(f"{{{_SML}}}calcPr")
    infos, payload = model_xlsx_hooks._read_package(target_path)
    tgt_wb = etree.fromstring(payload["xl/workbook.xml"], _XML)
    tgt_calc = tgt_wb.find(f"{{{_SML}}}calcPr")
    if donor_calc is None:
        if tgt_calc is not None:
            tgt_wb.remove(tgt_calc)
    else:
        if tgt_calc is not None:
            tgt_wb.replace(tgt_calc, copy.deepcopy(donor_calc))
        else:
            model_xlsx_hooks._insert_wb_element_in_schema_order(tgt_wb, copy.deepcopy(donor_calc))
    payload["xl/workbook.xml"] = etree.tostring(tgt_wb, encoding="UTF-8", xml_declaration=True)
    tmp = target_path.with_name(target_path.name + ".cpr.tmp")
    with zipfile.ZipFile(tmp, "w") as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename], compress_type=info.compress_type)
    os.replace(tmp, target_path)


def _clone_cell_style(src_cell: Any, dst_cell: Any) -> None:
    if src_cell.has_style:
        dst_cell.font = copy.copy(src_cell.font)
        dst_cell.fill = copy.copy(src_cell.fill)
        dst_cell.border = copy.copy(src_cell.border)
        dst_cell.alignment = copy.copy(src_cell.alignment)
        dst_cell.number_format = src_cell.number_format
        dst_cell.protection = copy.copy(src_cell.protection)


def _build_post_edit_dep_graph(
    workbook_path: Path,
) -> tuple[
    dict[tuple[str, str], set[tuple[str, str]]],
    dict[tuple[str, str], list[tuple[str, int, int, int, int]]],
]:
    """Build lightweight post-edit formula dependency graph (with defined_names + tables expanded)."""
    wb = openpyxl.load_workbook(workbook_path, data_only=False)
    try:
        tables = model_fingerprint._extract_tables_index(wb)
        defined_names: list[dict[str, str]] = []
        for name, dn in wb.defined_names.items():
            defined_names.append({"name": name, "scope": "workbook", "expression": str(dn.attr_text or "")})
        for ws in wb.worksheets:
            for name, dn in getattr(ws, "defined_names", {}).items():
                defined_names.append({"name": name, "scope": ws.title, "expression": str(dn.attr_text or "")})

        cell_deps: dict[tuple[str, str], set[tuple[str, str]]] = {}
        rect_deps: dict[tuple[str, str], list[tuple[str, int, int, int, int]]] = {}
        for ws in wb.worksheets:
            sname = ws.title
            for r, c, cell in model_style._iter_stored_cells(ws):
                f_txt = model_fingerprint._extract_formula_text(cell.value)
                if f_txt is None:
                    continue
                coord = f"{get_column_letter(c)}{r}"
                refs, rects, _, _ = model_fingerprint.expand_defined_names_in_expr(
                    f_txt, sname, defined_names, tables=tables
                )
                node = (sname, coord)
                cell_deps[node] = refs
                if rects:
                    rect_deps[node] = rects
        return cell_deps, rect_deps
    finally:
        wb.close()


def _node_in_rects(
    node: tuple[str, str],
    rects: list[tuple[str, int, int, int, int]],
) -> bool:
    sh, coord = node
    col_s, row_i = coordinate_from_string(coord)
    col_i = column_index_from_string(col_s)
    for rsh, r1, c1, r2, c2 in rects:
        if rsh == sh and r1 <= row_i <= r2 and c1 <= col_i <= c2:
            return True
    return False


def _find_downstream_formulas(
    seed_nodes: set[tuple[str, str]],
    cell_deps: dict[tuple[str, str], set[tuple[str, str]]],
    rect_deps: dict[tuple[str, str], list[tuple[str, int, int, int, int]]],
) -> set[tuple[str, str]]:
    """Return all formula nodes that transitively depend on any node in ``seed_nodes``."""
    if not seed_nodes:
        return set()
    # Reverse adjacency for direct cell edges
    rev: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for src, targets in cell_deps.items():
        for tgt in targets:
            rev[tgt].add(src)

    # Pre-index rect dependencies by referenced sheet
    rect_by_sheet: dict[str, list[tuple[tuple[str, str], int, int, int, int]]] = defaultdict(list)
    for src, rects in rect_deps.items():
        for rsh, r1, c1, r2, c2 in rects:
            rect_by_sheet[rsh].append((src, r1, c1, r2, c2))

    reached: set[tuple[str, str]] = set()
    queue = list(seed_nodes)
    visited = set(seed_nodes)
    while queue:
        cur = queue.pop(0)
        # Direct cell dependents
        for dep in rev.get(cur, ()):
            if dep not in reached:
                reached.add(dep)
            if dep not in visited:
                visited.add(dep)
                queue.append(dep)
        # Range-rect dependents
        sh, coord = cur
        if sh in rect_by_sheet:
            col_s, row_i = coordinate_from_string(coord)
            col_i = column_index_from_string(col_s)
            for dep, r1, c1, r2, c2 in rect_by_sheet[sh]:
                if r1 <= row_i <= r2 and c1 <= col_i <= c2:
                    if dep not in reached:
                        reached.add(dep)
                    if dep not in visited:
                        visited.add(dep)
                        queue.append(dep)
    return reached


def _is_empty_cache_workbook(baseline_path: Path) -> bool:
    """Inspect baseline OOXML directly (independent of any external fingerprint) to detect empty-cache templates."""
    raw_states = model_xlsx_hooks.extract_raw_xml_cell_states(baseline_path)
    formula_count = 0
    cached_formula_count = 0
    for cells in raw_states.values():
        for meta in cells.values():
            if not meta.get("has_formula"):
                continue
            formula_count += 1
            if meta.get("has_v") and (not meta.get("v_is_empty") or meta.get("t") is not None):
                cached_formula_count += 1
    if formula_count > 0 and cached_formula_count == 0:
        return True
    if baseline_path.name == "default_model_template.xlsx" and cached_formula_count == 0:
        return True
    return False


def _is_empty_cache_template(
    fp: dict[str, Any],
    baseline_path: Path | None = None,
) -> bool:
    """Return True when the baseline workbook is an empty-cache template (e.g. default_model_template.xlsx).

    When ``baseline_path`` exists on disk, inspects the workbook OOXML directly so an external/stale
    fingerprint can never mask an empty-cache template.
    """
    if baseline_path is not None and baseline_path.is_file():
        return _is_empty_cache_workbook(baseline_path)
    formula_count = 0
    cached_formula_count = 0
    for sh_data in (fp.get("sheets") or {}).values():
        for cmeta in (sh_data.get("cells") or {}).values():
            if cmeta.get("category") != "formula":
                continue
            formula_count += 1
            if cmeta.get("cached_value") is not None or cmeta.get("cached_t") is not None:
                cached_formula_count += 1
    if formula_count > 0 and cached_formula_count == 0:
        return True
    if baseline_path is not None and baseline_path.name == "default_model_template.xlsx" and cached_formula_count == 0:
        return True
    return False


def _build_keep_cache_spec(
    fingerprint: dict[str, Any],
    coord_map: dict[str, Any],
    donor_path: Path,
    ticker: str,
    touched_formula_cells: dict[str, list[str]],
    *,
    touched_cells: dict[str, list[str]] | None = None,
    editing_path: Path | None = None,
    engine: str = "libreoffice",
    backfill_empty_formulas: bool = False,
) -> dict[str, Any]:
    tk = ticker.strip().upper()
    skip_sheets: list[str] = []
    for sh_entry in fingerprint.get("sheet_order") or []:
        tag = sh_entry.get("company_tag")
        if tag is not None and str(tag).upper() != tk:
            skip_sheets.append(sh_entry["name"])
    skip_sheets_set = set(skip_sheets)

    touched_all_map: dict[str, set[str]] = {
        sh: {str(c).upper() for c in coords}
        for sh, coords in (touched_cells or {}).items()
    }
    touched_f_map: dict[str, set[str]] = {
        sh: {str(c).upper() for c in coords}
        for sh, coords in (touched_formula_cells or {}).items()
    }

    skip_cells_map: dict[str, set[str]] = defaultdict(set)
    # 1. External link baseline cells (includes cells referencing external defined_names)
    for item in (fingerprint.get("external_links_baseline") or {}).get("cells") or []:
        sh = item.get("sheet")
        cell = item.get("cell")
        if sh and cell:
            mapped = map_coord_str(coord_map, sh, cell)
            if mapped:
                skip_cells_map[sh].add(mapped.upper())

    # 2. Plugin & external_link cells
    for sh_name, sh_data in (fingerprint.get("sheets") or {}).items():
        for coord, cmeta in (sh_data.get("cells") or {}).items():
            if cmeta.get("category") == "formula" and cmeta.get("subcategory") in ("plugin", "external_link"):
                mapped = map_coord_str(coord_map, sh_name, coord)
                if mapped:
                    skip_cells_map[sh_name].add(mapped.upper())

    # 3. Circular baseline SCC cells (mapped through coord_map; includes defined_names cycles)
    for scc in fingerprint.get("circular_baseline") or []:
        for node in scc:
            if "!" in node:
                sh_name, coord = node.rsplit("!", 1)
                mapped = map_coord_str(coord_map, sh_name, coord)
                if mapped:
                    skip_cells_map[sh_name].add(mapped.upper())

    # Remove any exempt coordinate that the user explicitly overwrote in this edit
    for sh_name in list(skip_cells_map):
        if sh_name in touched_all_map:
            skip_cells_map[sh_name] -= touched_all_map[sh_name]
        if not skip_cells_map[sh_name]:
            del skip_cells_map[sh_name]

    # Compute exempt_downstream_cells & recalc_target_cells from post-edit dependency graph
    exempt_downstream_map: dict[str, set[str]] = defaultdict(set)
    recalc_targets_map: dict[str, set[str]] = defaultdict(set)
    if editing_path is not None and editing_path.is_file():
        cell_deps, rect_deps = _build_post_edit_dep_graph(editing_path)
        exempt_seeds: set[tuple[str, str]] = {
            (sh, crd)
            for sh, crds in skip_cells_map.items()
            if sh not in skip_sheets_set
            for crd in crds
        }
        down_exempt = _find_downstream_formulas(exempt_seeds, cell_deps, rect_deps)
        for sh, crd in down_exempt:
            if sh not in skip_sheets_set and crd not in skip_cells_map.get(sh, set()) and crd not in touched_f_map.get(sh, set()):
                exempt_downstream_map[sh].add(crd)

        touched_seeds: set[tuple[str, str]] = {
            (sh, crd)
            for sh, crds in touched_all_map.items()
            if sh not in skip_sheets_set
            for crd in crds
        }
        for sh, crds in touched_f_map.items():
            if sh not in skip_sheets_set:
                for crd in crds:
                    recalc_targets_map[sh].add(crd)
                    touched_seeds.add((sh, crd))
        down_touched = _find_downstream_formulas(touched_seeds, cell_deps, rect_deps)
        for sh, crd in down_touched:
            if (
                sh not in skip_sheets_set
                and crd not in skip_cells_map.get(sh, set())
                and crd not in exempt_downstream_map.get(sh, set())
            ):
                recalc_targets_map[sh].add(crd)

    return {
        "cache_donor_path": str(donor_path),
        "engine": engine,
        "allow_external_links": True,
        "skip_sheets": sorted(set(skip_sheets)),
        "skip_cells": {sh: sorted(coords) for sh, coords in sorted(skip_cells_map.items()) if coords},
        "exempt_downstream_cells": {
            sh: sorted(coords) for sh, coords in sorted(exempt_downstream_map.items()) if coords
        },
        "touched_cells": {
            sh: sorted(coords) for sh, coords in sorted(touched_all_map.items()) if coords
        },
        "touched_formula_cells": {
            sh: sorted(set(c.upper() for c in coords))
            for sh, coords in sorted(touched_formula_cells.items())
            if coords
        },
        "recalc_target_cells": {
            sh: sorted(coords) for sh, coords in sorted(recalc_targets_map.items()) if coords
        },
        "backfill_empty_formulas": bool(backfill_empty_formulas),
        "from_default_template": bool(backfill_empty_formulas),
        "skip_error_writeback": True,
    }


def _merge_coord_maps(prior_cm: dict[str, Any], delta_cm: dict[str, Any]) -> dict[str, Any]:
    """Merge an incremental fix round's ``delta_cm`` onto a prior ``coord_map``."""
    delta_ops = list(delta_cm.get("ops") or [])

    def _map_cell_sets(prior_map: dict[str, list[str]], delta_map: dict[str, list[str]]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for sh, coords in (prior_map or {}).items():
            for c in coords or ():
                nc = map_coord_str(delta_cm, sh, str(c)) if delta_ops else str(c).upper()
                if nc:
                    out[sh].add(nc.upper())
        for sh, coords in (delta_map or {}).items():
            for c in coords or ():
                out[sh].add(str(c).upper())
        return out

    def _map_col_sets(prior_map: dict[str, list[str]], delta_map: dict[str, list[str]]) -> dict[str, list[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for sh, cols in (prior_map or {}).items():
            for col_l in cols or ():
                if delta_ops:
                    ci = column_index_from_string(str(col_l).upper())
                    mapped = map_coord(delta_cm, sh, 1, ci)
                    if mapped is not None:
                        out[sh].add(get_column_letter(mapped[1]))
                else:
                    out[sh].add(str(col_l).upper())
        for sh, cols in (delta_map or {}).items():
            for col_l in cols or ():
                out[sh].add(str(col_l).upper())
        return {sh: sorted(cs, key=column_index_from_string) for sh, cs in sorted(out.items()) if cs}

    def _map_row_sets(prior_map: dict[str, list[int]], delta_map: dict[str, list[int]]) -> dict[str, list[int]]:
        out: dict[str, set[int]] = defaultdict(set)
        for sh, rows in (prior_map or {}).items():
            for r in rows or ():
                if delta_ops:
                    mapped = map_coord(delta_cm, sh, int(r), 1)
                    if mapped is not None:
                        out[sh].add(mapped[0])
                else:
                    out[sh].add(int(r))
        for sh, rows in (delta_map or {}).items():
            for r in rows or ():
                out[sh].add(int(r))
        return {sh: sorted(rs) for sh, rs in sorted(out.items()) if rs}

    touched = _map_cell_sets(prior_cm.get("touched_cells") or {}, delta_cm.get("touched_cells") or {})
    touched_f = _map_cell_sets(prior_cm.get("touched_formula_cells") or {}, delta_cm.get("touched_formula_cells") or {})
    rollover = _map_cell_sets(prior_cm.get("rollover_actual_cells") or {}, delta_cm.get("rollover_actual_cells") or {})
    consensus = _map_cell_sets(
        prior_cm.get("consensus_snapshot_cells") or {},
        delta_cm.get("consensus_snapshot_cells") or {},
    )

    # Cells overwritten in this incremental round update their formula vs rollover classification
    for sh, d_cells in (delta_cm.get("touched_cells") or {}).items():
        d_formulas = {str(c).upper() for c in (delta_cm.get("touched_formula_cells") or {}).get(sh, ())}
        d_rollover = {str(c).upper() for c in (delta_cm.get("rollover_actual_cells") or {}).get(sh, ())}
        for raw_c in d_cells or ():
            cu = str(raw_c).upper()
            if cu in d_formulas:
                rollover[sh].discard(cu)
            else:
                touched_f[sh].discard(cu)
                if cu not in d_rollover:
                    rollover[sh].discard(cu)

    prior_donor = prior_cm.get("cache_donor_path")
    keep_prior_donor = bool(not delta_ops and prior_donor and Path(str(prior_donor)).is_file())

    merged = dict(delta_cm)
    merged["baseline_path"] = prior_cm.get("baseline_path") or delta_cm.get("baseline_path")
    merged["baseline_sha256"] = prior_cm.get("baseline_sha256") or delta_cm.get("baseline_sha256")
    merged["cache_donor_path"] = str(prior_donor) if keep_prior_donor else delta_cm.get("cache_donor_path")
    merged["ops"] = list(prior_cm.get("ops") or []) + delta_ops
    merged["touched_cells"] = {sh: sorted(cs) for sh, cs in sorted(touched.items()) if cs}
    merged["touched_formula_cells"] = {sh: sorted(cs) for sh, cs in sorted(touched_f.items()) if cs}
    merged["rollover_actual_cells"] = {sh: sorted(cs) for sh, cs in sorted(rollover.items()) if cs}
    merged["consensus_snapshot_cells"] = {sh: sorted(cs) for sh, cs in sorted(consensus.items()) if cs}
    merged["added_period_cols"] = _map_col_sets(
        prior_cm.get("added_period_cols") or {}, delta_cm.get("added_period_cols") or {}
    )
    merged["rollover_actual_cols"] = _map_col_sets(
        prior_cm.get("rollover_actual_cols") or {}, delta_cm.get("rollover_actual_cols") or {}
    )
    merged["added_rows"] = _map_row_sets(
        prior_cm.get("added_rows") or {}, delta_cm.get("added_rows") or {}
    )
    merged["from_default_template"] = bool(prior_cm.get("from_default_template")) or bool(
        delta_cm.get("from_default_template")
    )
    if prior_cm.get("mode"):
        merged["mode"] = prior_cm["mode"]
    return merged


def apply_edit_plan(
    *,
    baseline_path: str | Path,
    ticker: str,
    plan: dict[str, Any],
    output_path: str | Path | None = None,
    workspace_dir: str | Path | None = None,
    fingerprint: dict[str, Any] | None = None,
    coord_map_out: str | Path | None = None,
    donor_out: str | Path | None = None,
    spec_out: str | Path | None = None,
    expected_baseline_sha256: str | None = None,
    skip_recalc: bool = False,
    recalc_timeout: int = 120,
    recalc_mod_override: Any | None = None,
    mode: str = "auto",
    in_place: bool = False,
    prior_coord_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute P0 controlled edit plan and return structured result with ``coord_map``."""
    base = Path(baseline_path).expanduser().resolve()
    if not base.is_file():
        raise FileNotFoundError(f"baseline workbook not found: {base}")

    base_sha = model_fingerprint.file_sha256(base)
    if expected_baseline_sha256:
        want = expected_baseline_sha256.strip().lower()
        if base_sha != want:
            raise ValueError(
                f"baseline sha256 mismatch: expected {want}, got {base_sha} ({base})"
            )

    if fingerprint is not None:
        fp_sha = str(fingerprint.get("source_sha256") or "").strip().lower()
        if not fp_sha or fp_sha != base_sha:
            raise ValueError(
                f"fingerprint source_sha256 mismatch: expected {base_sha}, got {fp_sha or '<missing>'} ({base})"
            )
        fp = fingerprint
    else:
        fp = model_fingerprint.build_fingerprint(base, ticker=ticker)

    is_template_build = (
        str(mode).strip().lower() == "build"
        or str(plan.get("mode") or "").strip().lower() == "build"
        or _is_empty_cache_workbook(base)
    )
    if is_template_build and skip_recalc:
        raise ValueError(
            "Refusing --skip-recalc on default-template / build mode: recalculation is required so formulas do not ship with empty cached values"
        )

    if output_path is not None:
        cand = Path(output_path).expanduser().resolve()
    elif in_place:
        cand = base
    else:
        ws_root = Path(workspace_dir).expanduser().resolve() if workspace_dir else base.parent.parent
        cand = next_model_output_path(
            ws_root,
            ticker,
            baseline_path=base,
            naming_mode=fp.get("naming_mode", "auto"),
        )
    if cand == base and not in_place:
        raise ValueError("Refusing to overwrite read-only baseline workbook in place")
    if prior_coord_map is not None:
        orig_base_raw = prior_coord_map.get("baseline_path")
        if orig_base_raw and Path(str(orig_base_raw)).expanduser().resolve() == cand:
            raise ValueError("Refusing to overwrite original read-only baseline workbook in place")
    cand.parent.mkdir(parents=True, exist_ok=True)

    tk = ticker.strip().upper()
    non_target_sheets: set[str] = {
        str(entry["name"])
        for entry in (fp.get("sheet_order") or [])
        if entry.get("company_tag") is not None and str(entry["company_tag"]).upper() != tk
    }
    known_sheets: set[str] = set((fp.get("sheets") or {}).keys())

    # Validate target scope & consensus-snapshot zone BEFORE mutating donor or workbook (Codex R2 #3)
    ops = list(plan.get("ops") or [])
    for op in ops:
        op_sh = str(op.get("sheet") or "")
        if op_sh in non_target_sheets:
            raise ValueError(
                f"Refusing structural op {op.get('op')!r} on non-target company sheet {op_sh!r} for ticker {tk}"
            )
        if known_sheets and op_sh not in known_sheets:
            raise ValueError(f"Unknown sheet in structural ops: {op_sh!r}")
    for clone_spec in plan.get("clone_columns") or []:
        c_sh = str(clone_spec.get("sheet") or "")
        if c_sh in non_target_sheets:
            raise ValueError(
                f"Refusing column clone on non-target company sheet {c_sh!r} for ticker {tk}"
            )
    for edit in plan.get("cell_edits") or []:
        e_sh = str(edit.get("sheet") or "")
        if e_sh in non_target_sheets:
            raise ValueError(
                f"Refusing cell edit on non-target company sheet {e_sh!r} for ticker {tk}"
            )
        if edit.get("is_consensus_snapshot"):
            low_sh = e_sh.lower()
            if not any(k in low_sh for k in ("vs consensus", "street vs house", "consensus")):
                raise ValueError(
                    f"Refusing consensus snapshot write (is_consensus_snapshot=True) on non-consensus sheet {e_sh!r}"
                )

    donor_path = (
        Path(donor_out).expanduser().resolve()
        if donor_out
        else cand.parent / (
            f".{tk}-pre-openpyxl-{cand.stem}-inc.xlsx"
            if (in_place or prior_coord_map is not None)
            else f".{tk}-pre-openpyxl-{cand.stem}.xlsx"
        )
    )
    donor_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(base, donor_path)

    # Step 1: Structural ops via insert.py --preserve-calcpr (preserves all OOXML <v> caches!)
    if ops:
        insert_mod = _load_module("_dalton_xlsx_insert", _locate_xlsx_script("insert.py"))
        op_cmd_map = {
            "insert_columns": "columns",
            "insert_rows": "rows",
            "delete_columns": "delete-columns",
            "delete_rows": "delete-rows",
        }
        for op in ops:
            kind = op.get("op")
            if kind not in op_cmd_map:
                raise ValueError(f"Unsupported structural op: {kind!r}")
            argv = [
                op_cmd_map[kind],
                str(donor_path),
                "--sheet",
                str(op["sheet"]),
                "--at",
                str(op["at"]),
                "--count",
                str(int(op.get("count", 1))),
                "--preserve-calcpr",
            ]
            if op.get("copy_style_from") is not None and not kind.startswith("delete_"):
                argv.extend(["--copy-style-from", str(op["copy_style_from"])])
            rep = insert_mod.execute(argv)
            if rep.get("status") != "success":
                raise RuntimeError(f"insert.py failed on {op}: {rep}")

    coord_map: dict[str, Any] = {
        "schema_version": 1,
        "engine": "openpyxl",
        "ticker": tk,
        "baseline_path": str(base),
        "baseline_sha256": base_sha,
        "candidate_path": str(cand),
        "cache_donor_path": str(donor_path),
        "ops": ops,
        "touched_cells": {},
        "touched_formula_cells": {},
        "added_period_cols": {},
        "rollover_actual_cols": {},
        "rollover_actual_cells": {},
        "added_rows": {},
        "consensus_snapshot_cells": {},
    }

    # Normalize declared period / rollover / row metadata
    added_period_cols: dict[str, list[str]] = {
        sh: [str(c).upper() for c in cols]
        for sh, cols in (plan.get("added_period_cols") or {}).items()
    }
    rollover_actual_cols: dict[str, list[str]] = {
        sh: [str(c).upper() for c in cols]
        for sh, cols in (plan.get("rollover_actual_cols") or {}).items()
    }
    added_rows: dict[str, list[int]] = {
        sh: [int(r) for r in rows]
        for sh, rows in (plan.get("added_rows") or {}).items()
    }
    coord_map["added_period_cols"] = added_period_cols
    coord_map["rollover_actual_cols"] = rollover_actual_cols
    coord_map["added_rows"] = added_rows

    # Build lookup of baseline subtotal/ratio rows mapped to new row indices (§8.4)
    subtotal_rows_new: dict[str, set[int]] = defaultdict(set)
    for sh_name, sh_data in (fp.get("sheets") or {}).items():
        for row_s, rmeta in (sh_data.get("line_item_map") or {}).items():
            if rmeta.get("is_subtotal_or_ratio"):
                mapped = map_coord(coord_map, sh_name, int(row_s), 1)
                if mapped is not None:
                    subtotal_rows_new[sh_name].add(mapped[0])

    # Step 2: Controlled openpyxl edits on temporary editing copy (.editing.tmp.xlsx)
    editing_tmp = cand.with_name(f".{cand.stem}.editing.tmp.xlsx")
    shutil.copyfile(donor_path, editing_tmp)

    touched_cells: dict[str, set[str]] = defaultdict(set)
    touched_formulas: dict[str, set[str]] = defaultdict(set)
    rollover_actual_cells: dict[str, set[str]] = defaultdict(set)
    consensus_snap_cells: dict[str, set[str]] = defaultdict(set)

    wb = openpyxl.load_workbook(editing_tmp, data_only=False)
    try:
        # Optional column style cloning for appended period columns
        for clone_spec in plan.get("clone_columns") or []:
            sh = clone_spec["sheet"]
            ws = wb[sh]
            src_c = (
                column_index_from_string(str(clone_spec["src_col"]))
                if isinstance(clone_spec["src_col"], str) and not str(clone_spec["src_col"]).isdigit()
                else int(clone_spec["src_col"])
            )
            dst_c = (
                column_index_from_string(str(clone_spec["dst_col"]))
                if isinstance(clone_spec["dst_col"], str) and not str(clone_spec["dst_col"]).isdigit()
                else int(clone_spec["dst_col"])
            )
            model_style.clone_period_column_style(ws, src_c, dst_c)

        for edit in plan.get("cell_edits") or []:
            sh = edit["sheet"]
            if sh not in wb.sheetnames:
                raise ValueError(f"Unknown sheet in cell_edits: {sh!r}")
            ws = wb[sh]
            coord = str(edit["cell"]).upper()
            col_s, row_i = coordinate_from_string(coord)
            cell = ws[coord]

            if edit.get("style_from"):
                _clone_cell_style(ws[str(edit["style_from"]).upper()], cell)
            elif edit.get("style_role"):
                model_style.apply_cell_style(
                    cell,
                    str(edit["style_role"]),
                    is_percent=bool(edit.get("is_percent", False)),
                    is_total=bool(edit.get("is_total", False)),
                    is_link=bool(edit.get("is_link", False)),
                    is_ratio=bool(edit.get("is_ratio", False)),
                )

            new_val = edit.get("value")
            cell.value = new_val
            touched_cells[sh].add(coord)

            is_formula = isinstance(new_val, str) and new_val.startswith("=")
            if is_formula:
                touched_formulas[sh].add(coord)
            elif edit.get("is_consensus_snapshot"):
                consensus_snap_cells[sh].add(coord)
            elif col_s.upper() in rollover_actual_cols.get(sh, ()):
                # Only register detail / KPI rows in rollover_actual_cells (§8.4 guard!)
                if isinstance(new_val, (int, float)) and not isinstance(new_val, bool):
                    row_lbl = model_fingerprint._row_label_text(ws, row_i)
                    is_sub = (row_i in subtotal_rows_new.get(sh, set())) or model_fingerprint.is_subtotal_or_ratio_row(
                        row_lbl, num_fmt=str(cell.number_format or "")
                    )
                    if not is_sub:
                        rollover_actual_cells[sh].add(coord)

            if edit.get("note") is not None:
                note_col = edit.get("note_col")
                nc_i: int | None = None
                if note_col is not None:
                    nc_i = (
                        column_index_from_string(str(note_col))
                        if isinstance(note_col, str) and not str(note_col).isdigit()
                        else int(note_col)
                    )
                else:
                    base_note_col = (
                        fp.get("sheets", {})
                        .get(sh, {})
                        .get("period_axis", {})
                        .get("source_note_col")
                    )
                    if base_note_col is not None:
                        base_nc_i = (
                            column_index_from_string(str(base_note_col))
                            if isinstance(base_note_col, str) and not str(base_note_col).isdigit()
                            else int(base_note_col)
                        )
                        mapped_note = map_coord(coord_map, sh, 1, base_nc_i)
                        if mapped_note is not None:
                            nc_i = mapped_note[1]
                if nc_i is not None:
                    ncell = ws.cell(row_i, nc_i, str(edit["note"]))
                    model_style.apply_cell_style(ncell, "source_note")
                    touched_cells[sh].add(f"{get_column_letter(nc_i)}{row_i}")

        # Optional Changelog row append on README & Changelog / Change Log
        changelog = plan.get("changelog_entry")
        if isinstance(changelog, dict):
            rm_name = next(
                (n for n in wb.sheetnames if "readme" in n.lower() or "change" in n.lower()),
                None,
            )
            if rm_name is not None:
                ws_rm = wb[rm_name]
                next_r = ws_rm.max_row + 1
                vals_row = [
                    changelog.get("version", cand.stem),
                    changelog.get("date", ""),
                    changelog.get("baseline", base.name),
                    changelog.get("summary", ""),
                    changelog.get("drivers", ""),
                    changelog.get("consensus_rationale", ""),
                ]
                for idx, v in enumerate(vals_row, start=1):
                    if v != "" or idx <= 4:
                        ws_rm.cell(next_r, idx, v)
                        touched_cells[rm_name].add(f"{get_column_letter(idx)}{next_r}")

        wb.save(editing_tmp)
    finally:
        wb.close()

    coord_map["mode"] = "build" if is_template_build else "update"
    coord_map["from_default_template"] = is_template_build

    # Merge ONLY modified sheets from openpyxl's editing_tmp back into the post-insert donor package
    # so EVERY non-target / untouched sheet stays BYTE-FOR-BYTE identical to donor_path (incl. ignoredErrors, ph, s, SST)!
    modified_sheets: set[str] = set(touched_cells.keys()) | set(added_period_cols.keys())
    for clone_spec in plan.get("clone_columns") or []:
        if clone_spec.get("sheet"):
            modified_sheets.add(str(clone_spec["sheet"]))
    model_xlsx_hooks.merge_edited_sheets_into_donor(
        donor_path, editing_tmp, editing_tmp, modified_sheets=modified_sheets
    )

    # Preserve exact <calcPr> from donor after merge
    _preserve_calcpr_from_donor(editing_tmp, donor_path)

    # Extend conditionalFormatting sqref for any appended rightmost period cols (§8.8 #3)
    for sh, cols in added_period_cols.items():
        _extend_cf_sqref_for_appended_cols(editing_tmp, sh, cols)

    coord_map["touched_cells"] = {sh: sorted(cs) for sh, cs in sorted(touched_cells.items())}
    coord_map["touched_formula_cells"] = {sh: sorted(cs) for sh, cs in sorted(touched_formulas.items())}
    coord_map["rollover_actual_cells"] = {sh: sorted(cs) for sh, cs in sorted(rollover_actual_cells.items())}
    coord_map["consensus_snapshot_cells"] = {sh: sorted(cs) for sh, cs in sorted(consensus_snap_cells.items())}

    keep_cache_spec = _build_keep_cache_spec(
        fp,
        coord_map,
        donor_path,
        tk,
        coord_map["touched_formula_cells"],
        touched_cells=coord_map["touched_cells"],
        editing_path=editing_tmp,
        engine=str(plan.get("calc_engine") or "libreoffice"),
        backfill_empty_formulas=is_template_build,
    )
    if spec_out is not None:
        sp = Path(spec_out).expanduser().resolve()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(keep_cache_spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Step 3: Atomic pre-publish donor cache restore (zero LibreOffice dependency!)
    recalc_mod = (
        recalc_mod_override
        if recalc_mod_override is not None
        else _load_module("_dalton_xlsx_recalc", _locate_xlsx_script("recalc.py"))
    )
    donor_restore_res = model_xlsx_hooks.restore_donor_cache(
        editing_tmp, keep_cache_spec, recalc_mod=recalc_mod
    )
    # For default-template builds, enable fullCalcOnLoad=1 BEFORE atomic publish so even if
    # LibreOffice recalculation times out / is killed mid-flight, the published workbook on disk
    # is already readable and prompts Excel to recalculate on open.
    if is_template_build and not skip_recalc:
        model_xlsx_hooks.set_full_calc_on_load(editing_tmp, True)
    os.replace(editing_tmp, cand)

    ok_read, read_msg = model_readable.check_workbook_readable(cand)
    if not ok_read:
        raise ValueError(f"published candidate workbook unreadable before recalc: {read_msg} ({cand})")

    pre_recalc_sha = model_fingerprint.file_sha256(cand)
    coord_map["candidate_sha256"] = pre_recalc_sha
    coord_map["donor_cache_restored"] = True
    coord_map["kept_cache_count"] = donor_restore_res.get("kept_cache_count", 0)
    coord_map["recalc_result"] = {"status": "pending"}
    if coord_map_out is not None:
        cp = Path(coord_map_out).expanduser().resolve()
        cp.parent.mkdir(parents=True, exist_ok=True)
        pre_cm = _merge_coord_maps(prior_coord_map, coord_map) if prior_coord_map is not None else coord_map
        tmp_cm = cp.with_name(f".{cp.name}.{os.getpid()}.tmp")
        tmp_cm.write_text(json.dumps(pre_cm, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp_cm, cp)

    # Step 4: Temp-copy LibreOffice recalculation & selective non-error merge
    full_calc_fallback = False
    if skip_recalc:
        # Non-template --skip-recalc: enable fullCalcOnLoad=1 BEFORE hashing/reporting so Excel recalculates
        # on open and generated reports match the actual workbook state.
        model_xlsx_hooks.set_full_calc_on_load(cand, True)
        full_calc_fallback = True
        recalc_res: dict[str, Any] = {
            "status": "skipped",
            "engine": "libreoffice",
            "donor_cache_restored": True,
            "kept_cache_count": donor_restore_res.get("kept_cache_count", 0),
            "full_calc_on_load_enabled": True,
        }
    else:
        recalc_res = model_xlsx_hooks.recalculate_keep_cache(
            recalc_mod, cand, int(recalc_timeout), set(), keep_cache_spec
        )
        # For default-template builds: if recalculation failed or left unmatched target formulas,
        # keep fullCalcOnLoad=1 so Excel recalculates immediately on open; if it succeeded cleanly,
        # restore the donor <calcPr>.
        if is_template_build:
            if recalc_res.get("status") in ("recalc_failed", "incomplete"):
                model_xlsx_hooks.set_full_calc_on_load(cand, True)
                full_calc_fallback = True
                recalc_res["full_calc_on_load_enabled"] = True
            else:
                _preserve_calcpr_from_donor(cand, donor_path)

    cand_sha = model_fingerprint.file_sha256(cand)
    coord_map["candidate_sha256"] = cand_sha
    coord_map["donor_cache_restored"] = True
    coord_map["full_calc_on_load_enabled_on_recalc_failure"] = full_calc_fallback
    coord_map["full_calc_on_load_enabled_on_skip_recalc"] = bool(skip_recalc and full_calc_fallback)
    coord_map["kept_cache_count"] = recalc_res.get(
        "kept_cache_count", donor_restore_res.get("kept_cache_count", 0)
    )
    coord_map["recalc_result"] = recalc_res

    if prior_coord_map is not None:
        coord_map = _merge_coord_maps(prior_coord_map, coord_map)

    if coord_map_out is not None:
        cp = Path(coord_map_out).expanduser().resolve()
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp_cm = cp.with_name(f".{cp.name}.{os.getpid()}.tmp")
        tmp_cm.write_text(json.dumps(coord_map, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp_cm, cp)

    return {
        "status": "ok",
        "candidate_path": str(cand),
        "candidate_sha256": cand_sha,
        "baseline_path": str(coord_map.get("baseline_path") or base),
        "baseline_sha256": str(coord_map.get("baseline_sha256") or base_sha),
        "cache_donor_path": str(coord_map.get("cache_donor_path") or donor_path),
        "donor_cache_restored": True,
        "recalc_status": recalc_res.get("status"),
        "recalc_result": recalc_res,
        "coord_map_path": str(Path(coord_map_out).expanduser().resolve()) if coord_map_out else None,
        "coord_map": coord_map,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Controlled in-place Excel model editor (modeling v2 §5.1)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_next = sub.add_parser("next-path", help="Compute next model version output path")
    p_next.add_argument("--workspace-dir", required=True)
    p_next.add_argument("--ticker", required=True)
    p_next.add_argument("--baseline", default=None)
    p_next.add_argument("--naming-mode", default="auto", choices=["auto", "single_company", "sector_workbook"])

    p_apply = sub.add_parser("apply", help="Apply edit plan JSON to produce next candidate workbook")
    p_apply.add_argument("--baseline", required=True)
    p_apply.add_argument("--ticker", required=True)
    p_apply.add_argument("--plan", required=True, help="Path to edit plan JSON")
    p_apply.add_argument("--out", default=None)
    p_apply.add_argument("--workspace-dir", default=None)
    p_apply.add_argument("--fingerprint", default=None)
    p_apply.add_argument("--coord-map-out", default=None)
    p_apply.add_argument("--prior-coord-map", default=None)
    p_apply.add_argument("--in-place", action="store_true")
    p_apply.add_argument("--donor-out", default=None)
    p_apply.add_argument("--spec-out", default=None)
    p_apply.add_argument("--expected-baseline-sha256", default=None)
    p_apply.add_argument("--skip-recalc", action="store_true")
    p_apply.add_argument("--recalc-timeout", type=int, default=120)
    p_apply.add_argument("--mode", default="auto", choices=["auto", "build", "update"])

    args = parser.parse_args(argv)
    if args.cmd == "next-path":
        out = next_model_output_path(
            args.workspace_dir,
            args.ticker,
            baseline_path=args.baseline,
            naming_mode=args.naming_mode,
        )
        print(json.dumps({"status": "ok", "next_path": str(out)}, ensure_ascii=False))
        return 0

    if args.cmd == "apply":
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        fp = json.loads(Path(args.fingerprint).read_text(encoding="utf-8")) if args.fingerprint else None
        prior_cm = (
            json.loads(Path(args.prior_coord_map).read_text(encoding="utf-8"))
            if args.prior_coord_map and Path(args.prior_coord_map).is_file()
            else None
        )
        try:
            res = apply_edit_plan(
                baseline_path=args.baseline,
                ticker=args.ticker,
                plan=plan,
                output_path=args.out,
                workspace_dir=args.workspace_dir,
                fingerprint=fp,
                coord_map_out=args.coord_map_out,
                donor_out=args.donor_out,
                spec_out=args.spec_out,
                expected_baseline_sha256=args.expected_baseline_sha256,
                skip_recalc=args.skip_recalc,
                recalc_timeout=args.recalc_timeout,
                mode=args.mode,
                in_place=args.in_place,
                prior_coord_map=prior_cm,
            )
        except ValueError as exc:
            print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
        print(json.dumps({k: v for k, v in res.items() if k != "coord_map"}, indent=2, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
