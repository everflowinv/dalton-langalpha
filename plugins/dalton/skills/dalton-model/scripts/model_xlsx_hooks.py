#!/usr/bin/env python3
"""Dalton OOXML hooks delegated from upstream ``insert.py`` and ``recalc.py``.

Kept in Dalton-owned space so upstream ``insert.py`` and ``recalc.py`` carry only
1-5 line thin ``DALTON-HOOK`` delegates while remaining runnable both in the repo
and inside sandboxes (stdlib + lxml + openpyxl only, no ``src.dalton`` import).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
import zipfile

from lxml import etree

_SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_XML = etree.XMLParser(resolve_entities=False, no_network=True)


def restore_preserved_calcpr(package: Any) -> None:
    """Restore ``xl/workbook.xml`` ``<calcPr>`` to its pre-insert ``fullCalcOnLoad`` state."""
    orig_bytes = package.original.get("xl/workbook.xml")
    if not orig_bytes:
        return
    orig_root = etree.fromstring(orig_bytes, _XML)
    orig_calc = orig_root.find(f"{{{_SML}}}calcPr")

    cur_wb = package.workbook
    cur_calc = cur_wb.find(f"{{{_SML}}}calcPr")
    if orig_calc is None:
        if cur_calc is not None:
            cur_wb.remove(cur_calc)
        return

    if cur_calc is None:
        cur_calc = etree.Element(f"{{{_SML}}}calcPr")
        cur_wb.append(cur_calc)

    orig_full = orig_calc.get("fullCalcOnLoad")
    if orig_full is None:
        if "fullCalcOnLoad" in cur_calc.attrib:
            del cur_calc.attrib["fullCalcOnLoad"]
    else:
        cur_calc.set("fullCalcOnLoad", orig_full)


def execute_preserve_calcpr(insert_mod: Any, argv: list[str]) -> dict[str, Any]:
    """Run ``insert.py:execute`` while preserving the original workbook's ``<calcPr>``."""
    clean_argv = [a for a in argv if a != "--preserve-calcpr"]
    filename, _op, flags, _axis, _at, _count, _delete, _copy_from = insert_mod.parse_args(clean_argv)
    source = Path(filename).expanduser().resolve()
    destination = Path(flags.get("--out", str(source))).expanduser().resolve()

    orig_wb_bytes: bytes | None = None
    if source.is_file():
        with zipfile.ZipFile(source, "r") as zf:
            if "xl/workbook.xml" in zf.namelist():
                orig_wb_bytes = zf.read("xl/workbook.xml")

    report = insert_mod.execute(clean_argv)
    if flags.get("--dry-run") or report.get("status") != "success" or orig_wb_bytes is None:
        return report

    orig_calc = etree.fromstring(orig_wb_bytes, _XML).find(f"{{{_SML}}}calcPr")
    orig_full = orig_calc.get("fullCalcOnLoad") if orig_calc is not None else None

    infos, payload = _read_package(destination)
    wb_root = etree.fromstring(payload["xl/workbook.xml"], _XML)
    cur_calc = wb_root.find(f"{{{_SML}}}calcPr")
    changed = False
    if orig_calc is None:
        if cur_calc is not None:
            wb_root.remove(cur_calc)
            changed = True
    else:
        if cur_calc is None:
            cur_calc = etree.Element(f"{{{_SML}}}calcPr")
            wb_root.append(cur_calc)
            changed = True
        if orig_full is None:
            if "fullCalcOnLoad" in cur_calc.attrib:
                del cur_calc.attrib["fullCalcOnLoad"]
                changed = True
        elif cur_calc.get("fullCalcOnLoad") != orig_full:
            cur_calc.set("fullCalcOnLoad", orig_full)
            changed = True

    if changed:
        payload["xl/workbook.xml"] = etree.tostring(
            wb_root, encoding="UTF-8", xml_declaration=True
        )
        tmp = destination.with_name(destination.name + ".calcpr.tmp")
        with zipfile.ZipFile(tmp, "w") as zout:
            for info in infos:
                zout.writestr(info, payload[info.filename], compress_type=info.compress_type)
        os.replace(tmp, destination)

    return report


def _sheet_parts(payload: dict[str, bytes]) -> dict[str, str]:
    import posixpath

    wb_xml = etree.fromstring(payload["xl/workbook.xml"], _XML)
    rels = etree.fromstring(payload["xl/_rels/workbook.xml.rels"], _XML)
    targets = {r.get("Id"): r.get("Target") for r in rels}
    parts: dict[str, str] = {}
    for sheet in wb_xml.iter(f"{{{_SML}}}sheet"):
        target = targets.get(sheet.get(_REL_ID), "")
        part = (
            target.lstrip("/")
            if target.startswith("/")
            else posixpath.normpath(posixpath.join("xl", target))
        )
        name = sheet.get("name")
        if name:
            parts[name] = part
    return parts


def _read_package(path: Path) -> tuple[list[zipfile.ZipInfo], dict[str, bytes]]:
    with zipfile.ZipFile(path, "r") as zf:
        infos = zf.infolist()
        return infos, {i.filename: zf.read(i.filename) for i in infos}


def _norm_formula_xml(text: str | None) -> str:
    if not text:
        return ""
    s = "".join(str(text).strip().split())
    return s[1:] if s.startswith("=") else s


def _read_shared_strings(payload: dict[str, bytes]) -> list[str]:
    raw = payload.get("xl/sharedStrings.xml")
    if not raw:
        return []
    root = etree.fromstring(raw, _XML)
    return ["".join(t.text or "" for t in si.iter(f"{{{_SML}}}t")) for si in root.findall(f"{{{_SML}}}si")]


def _extract_donor_cells(
    donor_path: Path,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Read full cell cache & formula metadata ``{sheet: {COORD: {...}}}`` from donor OOXML."""
    _, payload = _read_package(donor_path)
    sst = _read_shared_strings(payload)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for name, part in _sheet_parts(payload).items():
        if part not in payload:
            continue
        root = etree.fromstring(payload[part], _XML)
        cells: dict[str, dict[str, Any]] = {}
        for c in root.iter(f"{{{_SML}}}c"):
            ref = c.get("r")
            if not ref:
                continue
            t = c.get("t")
            f_el = c.find(f"{{{_SML}}}f")
            has_formula = f_el is not None
            f_text = f_el.text if f_el is not None else None
            v = c.find(f"{{{_SML}}}v")
            v_text: str | None = None
            resolved_str: str | None = None
            if t == "inlineStr":
                is_el = c.find(f"{{{_SML}}}is")
                resolved_str = "".join(x.text or "" for x in is_el.iter(f"{{{_SML}}}t")) if is_el is not None else ""
                v_text = resolved_str
            elif v is not None:
                v_text = "" if (v.text is None and t == "str") else v.text
                if t == "s" and v_text is not None and v_text.isdigit():
                    idx = int(v_text)
                    resolved_str = sst[idx] if 0 <= idx < len(sst) else ""
            cells[ref.upper()] = {
                "t": t,
                "v": v_text,
                "has_v": v is not None,
                "has_formula": has_formula,
                "formula": f_text,
                "resolved_str": resolved_str,
                "xml_bytes": etree.tostring(c, encoding="UTF-8"),
            }
        out[name] = cells
    return out


def _extract_donor_cache(
    donor_path: Path,
) -> dict[str, dict[str, tuple[str | None, str | None]]]:
    """Read ``{sheet: {COORD: (t_attr, v_text)}}`` for all cells with ``<v>`` in donor."""
    full = _extract_donor_cells(donor_path)
    out: dict[str, dict[str, tuple[str | None, str | None]]] = {}
    for sh, cmap in full.items():
        out[sh] = {
            ref: (meta["t"], meta["v"])
            for ref, meta in cmap.items()
            if meta["has_v"] and meta["v"] is not None
        }
    return out


def _apply_cached_value(cell_el: Any, t: str | None, text: str) -> None:
    """Write ``<v>`` and exact ``t`` attribute (including explicit ``t="n"``) onto an OOXML ``<c>`` element."""
    is_el = cell_el.find(f"{{{_SML}}}is")
    if is_el is not None and t != "inlineStr":
        cell_el.remove(is_el)
    if t == "inlineStr":
        v_old = cell_el.find(f"{{{_SML}}}v")
        if v_old is not None:
            cell_el.remove(v_old)
        if is_el is None:
            is_el = etree.SubElement(cell_el, f"{{{_SML}}}is")
        else:
            is_el.clear()
        t_el = etree.SubElement(is_el, f"{{{_SML}}}t")
        t_el.text = text
        cell_el.set("t", "inlineStr")
        return
    v = cell_el.find(f"{{{_SML}}}v")
    if v is None:
        v = etree.Element(f"{{{_SML}}}v")
        f_el = cell_el.find(f"{{{_SML}}}f")
        if f_el is not None:
            f_el.addnext(v)
        else:
            cell_el.append(v)
    v.text = text
    if t is not None:
        cell_el.set("t", t)
    elif "t" in cell_el.attrib:
        del cell_el.attrib["t"]


def _cell_has_nonempty_cache(cell_el: Any) -> bool:
    v = cell_el.find(f"{{{_SML}}}v")
    if v is None:
        return False
    t = cell_el.get("t")
    if t == "str" and v.text is None:
        return True
    return v.text is not None and v.text != ""


def _parse_coord_row_col(ref: str) -> tuple[int, int]:
    i = 0
    while i < len(ref) and ref[i].isalpha():
        i += 1
    col_s = ref[:i].upper()
    row_i = int(ref[i:]) if i < len(ref) and ref[i:].isdigit() else 0
    col_i = 0
    for ch in col_s:
        col_i = col_i * 26 + (ord(ch) - ord("A") + 1)
    return row_i, col_i


def _reinsert_missing_donor_cell(root: Any, ref: str, donor_meta: dict[str, Any]) -> bool:
    """Re-insert a cell present in donor but dropped by openpyxl on an untouched non-target sheet."""
    sheet_data = root.find(f"{{{_SML}}}sheetData")
    if sheet_data is None:
        return False
    row_i, col_i = _parse_coord_row_col(ref)
    if row_i <= 0:
        return False

    # Find or create <row r="row_i"> in sorted row order
    target_row_el = None
    insert_before = None
    for r_el in sheet_data.findall(f"{{{_SML}}}row"):
        r_attr = r_el.get("r")
        if not r_attr or not r_attr.isdigit():
            continue
        ri = int(r_attr)
        if ri == row_i:
            target_row_el = r_el
            break
        if ri > row_i and insert_before is None:
            insert_before = r_el
            break
    if target_row_el is None:
        target_row_el = etree.Element(f"{{{_SML}}}row", r=str(row_i))
        if insert_before is not None:
            sheet_data.insert(sheet_data.index(insert_before), target_row_el)
        else:
            sheet_data.append(target_row_el)

    new_c = etree.fromstring(donor_meta["xml_bytes"], _XML)
    # Strip style index from donor since openpyxl may reindex cellXfs
    if "s" in new_c.attrib:
        del new_c.attrib["s"]
    # Convert donor SST index into inlineStr so sharedStrings table remains valid
    if not donor_meta["has_formula"] and donor_meta["t"] == "s":
        _apply_cached_value(new_c, "inlineStr", str(donor_meta.get("resolved_str") or ""))

    # Insert <c> in column order inside <row>
    c_insert_before = None
    for existing_c in target_row_el.findall(f"{{{_SML}}}c"):
        _, ex_ci = _parse_coord_row_col((existing_c.get("r") or "").upper())
        if ex_ci > col_i:
            c_insert_before = existing_c
            break
    if c_insert_before is not None:
        target_row_el.insert(target_row_el.index(c_insert_before), new_c)
    else:
        target_row_el.append(new_c)
    return True


def _is_error_value(recalc_mod: Any, t: str | None, text: str | None) -> bool:
    if t == "e":
        return True
    if isinstance(text, str) and text:
        if text in recalc_mod.EXCEL_ERRORS or recalc_mod.ERROR_SHAPE.match(text):
            return True
    return False


def restore_donor_cache(
    target_path: str | Path,
    spec: dict[str, Any],
    *,
    recalc_mod: Any | None = None,
) -> dict[str, Any]:
    """Atomically restore donor ``<v>`` and exact ``t`` (including ``t="n"``) in pure OOXML.

    Runs with zero LibreOffice dependency. Restores cached values from
    ``spec["cache_donor_path"]`` for:
    - all cells in ``skip_sheets`` (preserving exact ``<v>`` and ``t``, and restoring dropped cells);
    - all cells listed in ``skip_cells[sheet]`` and ``exempt_downstream_cells[sheet]``, ONLY after
      verifying the cell still carries its exempt donor formula (never overwriting a cell edited to a constant);
    - all untouched formula cells NOT listed in ``touched_formula_cells[sheet]`` / ``touched_cells[sheet]``.
    """
    target = Path(target_path).expanduser().resolve()
    donor_raw = spec.get("cache_donor_path")
    if not donor_raw:
        raise ValueError("keep-cache spec is missing 'cache_donor_path'")
    donor = Path(donor_raw).expanduser().resolve()
    if not donor.is_file():
        raise FileNotFoundError(f"cache donor workbook not found: {donor}")

    skip_sheets = set(spec.get("skip_sheets") or ())
    skip_cells = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("skip_cells") or {}).items()
    }
    exempt_downstream = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("exempt_downstream_cells") or {}).items()
    }
    touched_formulas = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("touched_formula_cells") or {}).items()
    }
    touched_all = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("touched_cells") or {}).items()
    }

    donor_cells = _extract_donor_cells(donor)
    infos, payload = _read_package(target)
    target_sst = _read_shared_strings(payload)
    kept_cache_count = 0

    for name, part in _sheet_parts(payload).items():
        if part not in payload:
            continue
        donor_sheet = donor_cells.get(name)
        if not donor_sheet:
            continue
        root = etree.fromstring(payload[part], _XML)
        is_skip_sheet = name in skip_sheets
        sheet_skip_cells = skip_cells.get(name, set())
        sheet_exempt_down = exempt_downstream.get(name, set())
        sheet_touched_f = touched_formulas.get(name, set())
        sheet_touched_all = touched_all.get(name, set())
        changed = False
        seen_refs: set[str] = set()

        for c in root.iter(f"{{{_SML}}}c"):
            ref = (c.get("r") or "").upper()
            if not ref:
                continue
            seen_refs.add(ref)
            d_meta = donor_sheet.get(ref)
            if d_meta is None:
                continue

            f_el = c.find(f"{{{_SML}}}f")
            has_formula = f_el is not None
            cur_f_norm = _norm_formula_xml(f_el.text if f_el is not None else None)
            donor_f_norm = _norm_formula_xml(d_meta.get("formula"))
            d_t = d_meta.get("t")
            d_v = d_meta.get("v")

            if is_skip_sheet:
                if has_formula:
                    if d_meta["has_v"] and d_v is not None:
                        _apply_cached_value(c, d_t, d_v)
                        kept_cache_count += 1
                        changed = True
                else:
                    if d_t == "s":
                        # Verify target cell's resolved string matches donor; do not blindly copy SST index
                        cur_t = c.get("t")
                        cur_v_el = c.find(f"{{{_SML}}}v")
                        cur_str = None
                        if cur_t == "s" and cur_v_el is not None and (cur_v_el.text or "").isdigit():
                            idx = int(cur_v_el.text)
                            if 0 <= idx < len(target_sst):
                                cur_str = target_sst[idx]
                        elif cur_t == "inlineStr":
                            is_el = c.find(f"{{{_SML}}}is")
                            cur_str = "".join(x.text or "" for x in is_el.iter(f"{{{_SML}}}t")) if is_el is not None else ""
                        if cur_str != d_meta.get("resolved_str") and d_meta.get("resolved_str") is not None:
                            _apply_cached_value(c, "inlineStr", str(d_meta["resolved_str"]))
                            kept_cache_count += 1
                            changed = True
                    elif d_meta["has_v"] and d_v is not None:
                        _apply_cached_value(c, d_t, d_v)
                        kept_cache_count += 1
                        changed = True
                continue

            # Target sheet rules:
            if not d_meta["has_v"] or d_v is None:
                continue
            if not has_formula:
                # Never overwrite a cell edited to a constant (including former exempt coords!)
                if ref not in sheet_touched_all and d_t != "s" and not d_meta["has_formula"]:
                    # Preserve explicit t="n" on untouched numeric constants if donor had it
                    if d_t is not None and c.get("t") != d_t and _cell_has_nonempty_cache(c):
                        _apply_cached_value(c, d_t, d_v)
                        kept_cache_count += 1
                        changed = True
                continue

            # Formula cell on target sheet:
            is_exempt_coord = (ref in sheet_skip_cells) or (ref in sheet_exempt_down)
            if is_exempt_coord:
                # Verify this cell is STILL the donor's exempt formula (not overwritten to a new formula!)
                if d_meta["has_formula"] and (not donor_f_norm or cur_f_norm == donor_f_norm):
                    _apply_cached_value(c, d_t, d_v)
                    kept_cache_count += 1
                    changed = True
                continue

            # Untouched formula cell: restore exact donor <v> and t
            if ref not in sheet_touched_f and ref not in sheet_touched_all:
                if d_meta["has_formula"] and (not donor_f_norm or cur_f_norm == donor_f_norm):
                    _apply_cached_value(c, d_t, d_v)
                    kept_cache_count += 1
                    changed = True

        # For non-target skip_sheets, also restore any cell that openpyxl dropped completely
        if is_skip_sheet:
            for d_ref, d_meta in donor_sheet.items():
                if d_ref not in seen_refs and (d_meta["has_v"] or d_meta["has_formula"] or d_meta["resolved_str"] is not None):
                    if _reinsert_missing_donor_cell(root, d_ref, d_meta):
                        kept_cache_count += 1
                        changed = True

        if changed:
            payload[part] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

    charts_updated = 0
    if recalc_mod is not None and hasattr(recalc_mod, "refresh_charts"):
        charts_updated, _ = recalc_mod.refresh_charts(payload)

    tmp = target.with_name(target.name + ".donor.tmp")
    with zipfile.ZipFile(tmp, "w") as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename], compress_type=info.compress_type)
    os.replace(tmp, target)
    return {
        "status": "ok",
        "file": str(target),
        "donor_cache_restored": True,
        "kept_cache_count": kept_cache_count,
        "charts_updated": charts_updated,
    }


def _values_differ(cur_t: str | None, cur_v: str | None, new_t: str | None, new_v: str | None) -> bool:
    if cur_v is None and new_v is None:
        return False
    if cur_t == "e" or new_t == "e":
        return str(cur_v) != str(new_v)
    try:
        a, b = float(cur_v or ""), float(new_v or "")
        return abs(a - b) > max(1e-9, 1e-9 * max(abs(a), abs(b)))
    except ValueError:
        return str(cur_v) != str(new_v)


def _transfer_values_selective(
    recalc_mod: Any,
    src: Path,
    dst: Path,
    values: dict[str, dict[str, tuple[str | None, str | None]]],
    formulas: dict[tuple[str, str], str],
    spec: dict[str, Any],
) -> tuple[int, int, int, int, list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Selectively merge LibreOffice values ONLY for round-affected formulas; keep donor caches intact."""
    skip_sheets = set(spec.get("skip_sheets") or ())
    skip_cells = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("skip_cells") or {}).items()
    }
    exempt_downstream = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("exempt_downstream_cells") or {}).items()
    }
    touched_formulas = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("touched_formula_cells") or {}).items()
    }
    has_recalc_targets = "recalc_target_cells" in spec and spec["recalc_target_cells"] is not None
    recalc_targets = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("recalc_target_cells") or {}).items()
    }
    skip_error_writeback = bool(spec.get("skip_error_writeback", True))

    infos, payload = recalc_mod.read_package(src)
    written = 0
    unmatched = 0
    kept_on_error = 0
    recalc_errors: list[dict[str, Any]] = []
    downstream_exempt_diffs: list[dict[str, Any]] = []

    for name, part in recalc_mod.sheet_parts(payload).items():
        if part not in payload:
            continue
        root = etree.fromstring(payload[part], _XML)
        sheet_values = values.get(name, {})
        is_skip_sheet = name in skip_sheets
        sheet_skip_cells = skip_cells.get(name, set())
        sheet_exempt_down = exempt_downstream.get(name, set())
        sheet_touched_f = touched_formulas.get(name, set())
        sheet_targets = recalc_targets.get(name, set())
        changed = False

        for c in root.iter(f"{{{_SML}}}c"):
            f = c.find(f"{{{_SML}}}f")
            if f is None:
                continue
            ref = c.get("r") or ""
            ref_u = ref.upper()
            is_direct_exempt = is_skip_sheet or (ref_u in sheet_skip_cells)
            is_down_exempt = (not is_direct_exempt) and (ref_u in sheet_exempt_down)
            exempt = is_direct_exempt or is_down_exempt
            got = sheet_values.get(ref) or sheet_values.get(ref_u)
            if got is None or got[1] is None:
                if not exempt and (not has_recalc_targets or ref_u in sheet_targets or ref_u in sheet_touched_f):
                    unmatched += 1
                continue
            t, text = got
            is_err = _is_error_value(recalc_mod, t, text)
            cur_v_el = c.find(f"{{{_SML}}}v")
            cur_t = c.get("t")
            cur_v = cur_v_el.text if cur_v_el is not None else None
            has_donor = _cell_has_nonempty_cache(c)

            if is_direct_exempt:
                if is_err:
                    recalc_errors.append(
                        {
                            "sheet": name,
                            "cell": ref,
                            "error": text or "#ERROR!",
                            "formula": formulas.get((name, ref), ""),
                            "exempt": True,
                            "kept_donor_cache": True,
                        }
                    )
                continue

            if is_down_exempt:
                # Downstream of exempt cell (e.g. =F45 or =IFERROR(F45, 0)): NEVER overwrite donor <v>!
                if is_err:
                    recalc_errors.append(
                        {
                            "sheet": name,
                            "cell": ref,
                            "error": text or "#ERROR!",
                            "formula": formulas.get((name, ref), ""),
                            "exempt": True,
                            "downstream_exempt": True,
                            "kept_donor_cache": True,
                        }
                    )
                elif has_donor and _values_differ(cur_t, cur_v, t, text):
                    downstream_exempt_diffs.append(
                        {
                            "sheet": name,
                            "cell": ref,
                            "donor_value": cur_v,
                            "recalc_value": text,
                            "formula": formulas.get((name, ref), ""),
                        }
                    )
                kept_on_error += 1
                continue

            # Non-exempt formula on target sheet: only write if affected this round or missing cache
            is_touched_f = ref_u in sheet_touched_f
            is_target = (
                is_touched_f
                or (ref_u in sheet_targets)
                or (not has_donor)
                or (not has_recalc_targets)
            )
            if not is_target:
                continue

            if is_err:
                # For newly added / modified formulas (or missing donor cache), KEEP the error value
                # so model_check.py C13 classifies it as actionable rather than hiding it behind 0/stale!
                if is_touched_f or (not has_donor) or (not skip_error_writeback) or (ref_u in sheet_targets):
                    recalc_errors.append(
                        {
                            "sheet": name,
                            "cell": ref,
                            "error": text or "#ERROR!",
                            "formula": formulas.get((name, ref), ""),
                            "exempt": False,
                            "kept_donor_cache": False,
                        }
                    )
                    _apply_cached_value(c, t, text)
                    written += 1
                    changed = True
                else:
                    recalc_errors.append(
                        {
                            "sheet": name,
                            "cell": ref,
                            "error": text or "#ERROR!",
                            "formula": formulas.get((name, ref), ""),
                            "exempt": False,
                            "kept_donor_cache": True,
                        }
                    )
                    kept_on_error += 1
                continue

            _apply_cached_value(c, t, text)
            written += 1
            changed = True

        if changed:
            payload[part] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

    charts_updated, chart_refs_skipped = recalc_mod.refresh_charts(payload)
    tmp = dst.with_name(dst.name + ".tmp")
    with zipfile.ZipFile(tmp, "w") as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename], compress_type=info.compress_type)
    os.replace(tmp, dst)
    return (
        written,
        unmatched,
        kept_on_error,
        charts_updated,
        chart_refs_skipped,
        recalc_errors,
        downstream_exempt_diffs,
    )


def recalculate_keep_cache(
    recalc_mod: Any,
    src: Path,
    timeout: int,
    flags: set[str],
    keep_cache_spec: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run Step 1 (donor cache restore) then Step 2 (temp-copy LibreOffice selective merge)."""
    spec = dict(keep_cache_spec or {})
    kept_cache_count = 0
    donor_restored = False
    if spec.get("cache_donor_path") and "--check-only" not in flags:
        res = restore_donor_cache(src, spec, recalc_mod=recalc_mod)
        kept_cache_count = int(res.get("kept_cache_count", 0))
        donor_restored = bool(res.get("donor_cache_restored", True))

    if "--restore-donor-only" in flags:
        return {
            "status": "success",
            "file": str(src),
            "engine": "donor_only",
            "donor_cache_restored": donor_restored,
            "kept_cache_count": kept_cache_count,
            "written": "--check-only" not in flags,
        }

    formulas, names, linked_part = recalc_mod.collect_formulas(src)
    linked_cells = recalc_mod.external_references(formulas)
    linked_names = recalc_mod.external_names(names)
    has_external = linked_part or bool(linked_cells) or bool(linked_names)
    allow_external = bool(spec.get("allow_external_links", True)) or ("--force" in flags)
    if has_external and not allow_external:
        return {
            "status": "recalc_failed",
            "file": str(src),
            "engine": str(spec.get("engine") or "libreoffice"),
            "donor_cache_restored": donor_restored,
            "kept_cache_count": kept_cache_count,
            "reason": "workbook has external links and allow_external_links is false",
            "external_links": True,
            "written": False,
        }

    engine = str(spec.get("engine") or "libreoffice").strip().lower()
    if engine not in ("libreoffice", "auto"):
        engine = "libreoffice"

    with tempfile.TemporaryDirectory(prefix="recalc_kc_") as tmp:
        tmp_dir = Path(tmp)
        work = tmp_dir / src.name
        shutil.copyfile(src, work)

        values = None
        used_engine = "libreoffice"
        if engine == "auto":
            values = recalc_mod.ironcalc_values(work, formulas, timeout)
            if values is not None:
                used_engine = "ironcalc"

        if values is None:
            used_engine = "libreoffice"
            if recalc_mod.shutil.which("soffice") is None:
                return {
                    "status": "recalc_failed",
                    "file": str(src),
                    "engine": "libreoffice",
                    "donor_cache_restored": donor_restored,
                    "kept_cache_count": kept_cache_count,
                    "reason": "soffice (LibreOffice) is not on PATH",
                    "total_formulas": len(formulas),
                    "external_links": bool(has_external),
                    "written": False,
                }
            shutil.copyfile(src, work)
            captured_out = io.StringIO()
            try:
                with contextlib.redirect_stdout(captured_out):
                    produced = recalc_mod.soffice_calculate(work, tmp_dir / "out", timeout)
                if produced is None or not Path(produced).is_file():
                    return {
                        "status": "recalc_failed",
                        "file": str(src),
                        "engine": "libreoffice",
                        "donor_cache_restored": donor_restored,
                        "kept_cache_count": kept_cache_count,
                        "reason": "LibreOffice exited without producing an output workbook",
                        "total_formulas": len(formulas),
                        "external_links": bool(has_external),
                        "written": False,
                    }
            except (SystemExit, subprocess.TimeoutExpired, Exception) as exc:
                reason = f"{type(exc).__name__}: {exc}"
                raw_out = captured_out.getvalue().strip()
                if raw_out:
                    try:
                        parsed = json.loads(raw_out)
                        if isinstance(parsed, dict) and parsed.get("message"):
                            reason = str(parsed["message"])
                    except Exception:
                        reason = raw_out[-500:]
                return {
                    "status": "recalc_failed",
                    "file": str(src),
                    "engine": "libreoffice",
                    "donor_cache_restored": donor_restored,
                    "kept_cache_count": kept_cache_count,
                    "reason": reason,
                    "total_formulas": len(formulas),
                    "external_links": bool(has_external),
                    "written": False,
                }
            _, produced_payload = recalc_mod.read_package(Path(produced))
            values = recalc_mod.computed_values(produced_payload)

        patched = tmp_dir / ("patched" + src.suffix)
        (
            written,
            unmatched,
            kept_on_error,
            charts_updated,
            chart_refs_skipped,
            recalc_errors,
            downstream_exempt_diffs,
        ) = _transfer_values_selective(recalc_mod, src, patched, values, formulas, spec)
        kept_cache_count += kept_on_error

        written_back = "--check-only" not in flags
        if written_back:
            staged = src.with_name(src.name + ".recalc.tmp")
            shutil.copyfile(patched, staged)
            os.replace(staged, src)

    non_exempt_errors = [e for e in recalc_errors if not e.get("exempt")]
    summary: dict[str, int] = {}
    for err in recalc_errors:
        code = str(err.get("error") or "#ERROR!")
        summary[code] = summary.get(code, 0) + 1

    return {
        "status": "errors_found" if non_exempt_errors else "success",
        "file": str(src),
        "total_formulas": len(formulas),
        "total_errors": len(recalc_errors),
        "non_exempt_errors": len(non_exempt_errors),
        "error_summary": summary,
        "errors": recalc_errors[: recalc_mod.MAX_LISTED],
        "downstream_exempt_diffs": downstream_exempt_diffs[: recalc_mod.MAX_LISTED],
        "truncated": len(recalc_errors) > recalc_mod.MAX_LISTED,
        "values_written": written,
        "unmatched": unmatched,
        "kept_cache_count": kept_cache_count,
        "donor_cache_restored": donor_restored,
        "charts_updated": charts_updated,
        "chart_refs_skipped": chart_refs_skipped,
        "engine": used_engine,
        "external_links": bool(has_external),
        "written": written_back,
    }


def recalc_main_keep_cache(recalc_mod: Any, argv: list[str]) -> None:
    """CLI handler delegated from ``recalc.py`` when keep-cache flags are present."""
    spec_path: str | None = None
    remaining: list[str] = []
    for arg in argv:
        if arg.startswith("--keep-cache-spec="):
            spec_path = arg.split("=", 1)[1]
        else:
            remaining.append(arg)

    args = [a for a in remaining if not a.startswith("--")]
    flags = {a for a in remaining if a.startswith("--")}
    allowed_flags = set(recalc_mod.FLAGS) | {"--restore-donor-only"}
    unknown = sorted(flags.difference(allowed_flags))
    if unknown:
        recalc_mod.fail(f"unknown option: {', '.join(unknown)}")
    if not args:
        recalc_mod.fail("usage: recalc.py <file.xlsx> [timeout_seconds] [--keep-cache-spec=<json>] [--restore-donor-only]")
    src = Path(args[0]).expanduser().resolve()
    try:
        timeout = int(args[1]) if len(args) > 1 else 60
    except ValueError:
        recalc_mod.fail(f"timeout must be a whole number of seconds, not {args[1]!r}")
    if not src.exists():
        recalc_mod.fail(f"file not found: {src}")

    spec_data: dict[str, Any] | None = None
    if spec_path:
        sp = Path(spec_path).expanduser().resolve()
        if not sp.is_file():
            recalc_mod.fail(f"keep-cache spec not found: {sp}")
        spec_data = json.loads(sp.read_text(encoding="utf-8"))

    try:
        report = recalculate_keep_cache(recalc_mod, src, timeout, flags, spec_data)
    except Exception as exc:
        print(json.dumps({"status": "error", "file": str(src), "message": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(1) from exc
    print(json.dumps(report, indent=2))
