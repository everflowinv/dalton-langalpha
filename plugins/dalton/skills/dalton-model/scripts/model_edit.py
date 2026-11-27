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
            tgt_wb.append(copy.deepcopy(donor_calc))
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


def _build_keep_cache_spec(
    fingerprint: dict[str, Any],
    coord_map: dict[str, Any],
    donor_path: Path,
    ticker: str,
    touched_formula_cells: dict[str, list[str]],
    *,
    engine: str = "libreoffice",
) -> dict[str, Any]:
    tk = ticker.strip().upper()
    skip_sheets: list[str] = []
    for sh_entry in fingerprint.get("sheet_order") or []:
        tag = sh_entry.get("company_tag")
        if tag is not None and str(tag).upper() != tk:
            skip_sheets.append(sh_entry["name"])

    skip_cells_map: dict[str, set[str]] = defaultdict(set)
    # 1. External link baseline cells
    for item in (fingerprint.get("external_links_baseline") or {}).get("cells") or []:
        sh = item.get("sheet")
        cell = item.get("cell")
        if sh and cell:
            mapped = map_coord_str(coord_map, sh, cell)
            if mapped:
                skip_cells_map[sh].add(mapped)

    # 2. Plugin cells (CIQ etc.)
    for sh_name, sh_data in (fingerprint.get("sheets") or {}).items():
        for coord, cmeta in (sh_data.get("cells") or {}).items():
            if cmeta.get("category") == "formula" and cmeta.get("subcategory") in ("plugin", "external_link"):
                mapped = map_coord_str(coord_map, sh_name, coord)
                if mapped:
                    skip_cells_map[sh_name].add(mapped)

    # 3. Circular baseline SCC cells (mapped through coord_map)
    for scc in fingerprint.get("circular_baseline") or []:
        for node in scc:
            if "!" in node:
                sh_name, coord = node.rsplit("!", 1)
                mapped = map_coord_str(coord_map, sh_name, coord)
                if mapped:
                    skip_cells_map[sh_name].add(mapped)

    return {
        "cache_donor_path": str(donor_path),
        "engine": engine,
        "allow_external_links": True,
        "skip_sheets": sorted(set(skip_sheets)),
        "skip_cells": {sh: sorted(coords) for sh, coords in sorted(skip_cells_map.items())},
        "touched_formula_cells": {
            sh: sorted(set(c.upper() for c in coords))
            for sh, coords in sorted(touched_formula_cells.items())
            if coords
        },
        "skip_error_writeback": True,
    }


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

    fp = fingerprint if fingerprint is not None else model_fingerprint.build_fingerprint(base, ticker=ticker)

    if output_path is not None:
        cand = Path(output_path).expanduser().resolve()
    else:
        ws_root = Path(workspace_dir).expanduser().resolve() if workspace_dir else base.parent.parent
        cand = next_model_output_path(
            ws_root,
            ticker,
            baseline_path=base,
            naming_mode=fp.get("naming_mode", "auto"),
        )
    if cand == base:
        raise ValueError("Refusing to overwrite read-only baseline workbook in place")
    cand.parent.mkdir(parents=True, exist_ok=True)

    tk = ticker.strip().upper()
    donor_path = (
        Path(donor_out).expanduser().resolve()
        if donor_out
        else cand.parent / f".{tk}-pre-openpyxl-{cand.stem}.xlsx"
    )
    donor_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(base, donor_path)

    # Step 1: Structural ops via insert.py --preserve-calcpr (preserves all OOXML <v> caches!)
    ops = list(plan.get("ops") or [])
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
                if note_col is None:
                    note_col = (
                        fp.get("sheets", {})
                        .get(sh, {})
                        .get("period_axis", {})
                        .get("source_note_col")
                    )
                if note_col is not None:
                    nc_i = (
                        column_index_from_string(str(note_col))
                        if isinstance(note_col, str) and not str(note_col).isdigit()
                        else int(note_col)
                    )
                    ncell = ws.cell(row_i, nc_i, str(edit["note"]))
                    model_style.apply_cell_style(ncell, "source_note")

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

    # Preserve exact <calcPr> from donor after openpyxl save
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
        engine=str(plan.get("calc_engine") or "libreoffice"),
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
    os.replace(editing_tmp, cand)

    # Step 4: Temp-copy LibreOffice recalculation & selective non-error merge
    if skip_recalc:
        recalc_res: dict[str, Any] = {
            "status": "skipped",
            "engine": "libreoffice",
            "donor_cache_restored": True,
            "kept_cache_count": donor_restore_res.get("kept_cache_count", 0),
        }
    else:
        recalc_res = model_xlsx_hooks.recalculate_keep_cache(
            recalc_mod, cand, int(recalc_timeout), set(), keep_cache_spec
        )

    cand_sha = model_fingerprint.file_sha256(cand)
    coord_map["candidate_sha256"] = cand_sha
    coord_map["donor_cache_restored"] = True
    coord_map["kept_cache_count"] = recalc_res.get(
        "kept_cache_count", donor_restore_res.get("kept_cache_count", 0)
    )
    coord_map["recalc_result"] = recalc_res

    if coord_map_out is not None:
        cp = Path(coord_map_out).expanduser().resolve()
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(coord_map, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        "status": "ok",
        "candidate_path": str(cand),
        "candidate_sha256": cand_sha,
        "baseline_path": str(base),
        "baseline_sha256": base_sha,
        "cache_donor_path": str(donor_path),
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
    p_apply.add_argument("--donor-out", default=None)
    p_apply.add_argument("--spec-out", default=None)
    p_apply.add_argument("--expected-baseline-sha256", default=None)
    p_apply.add_argument("--skip-recalc", action="store_true")
    p_apply.add_argument("--recalc-timeout", type=int, default=120)

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
        )
        print(json.dumps({k: v for k, v in res.items() if k != "coord_map"}, indent=2, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
