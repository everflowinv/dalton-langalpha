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


def _extract_donor_cache(
    donor_path: Path,
) -> dict[str, dict[str, tuple[str | None, str | None]]]:
    """Read ``{sheet: {COORD: (t_attr, v_text)}}`` for all cells with ``<v>`` in donor."""
    _, payload = _read_package(donor_path)
    out: dict[str, dict[str, tuple[str | None, str | None]]] = {}
    for name, part in _sheet_parts(payload).items():
        if part not in payload:
            continue
        root = etree.fromstring(payload[part], _XML)
        cells: dict[str, tuple[str | None, str | None]] = {}
        for c in root.iter(f"{{{_SML}}}c"):
            ref = c.get("r")
            if not ref:
                continue
            v = c.find(f"{{{_SML}}}v")
            if v is None:
                continue
            t = c.get("t")
            text = "" if v.text is None and t == "str" else v.text
            if text is None:
                continue
            cells[ref.upper()] = (t, text)
        out[name] = cells
    return out


def _apply_cached_value(cell_el: Any, t: str | None, text: str) -> None:
    """Write ``<v>`` and ``t`` attribute onto an OOXML ``<c>`` element."""
    v = cell_el.find(f"{{{_SML}}}v")
    if v is None:
        v = etree.Element(f"{{{_SML}}}v")
        f_el = cell_el.find(f"{{{_SML}}}f")
        if f_el is not None:
            f_el.addnext(v)
        else:
            cell_el.append(v)
    v.text = text
    if t in ("str", "e", "b", "s"):
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
    """Atomically restore donor ``<v>`` and ``t`` cache entries in pure OOXML.

    Runs with zero LibreOffice dependency. Restores cached values from
    ``spec["cache_donor_path"]`` for:
    - all formula cells (and any cells missing ``<v>``) in ``skip_sheets``;
    - all cells listed in ``skip_cells[sheet]``;
    - all formula cells NOT listed in ``touched_formula_cells[sheet]``.
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
    touched_formulas = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("touched_formula_cells") or {}).items()
    }

    donor_cache = _extract_donor_cache(donor)
    infos, payload = _read_package(target)
    kept_cache_count = 0

    for name, part in _sheet_parts(payload).items():
        if part not in payload:
            continue
        donor_sheet = donor_cache.get(name)
        if not donor_sheet:
            continue
        root = etree.fromstring(payload[part], _XML)
        is_skip_sheet = name in skip_sheets
        sheet_skip_cells = skip_cells.get(name, set())
        sheet_touched = touched_formulas.get(name, set())
        changed = False

        for c in root.iter(f"{{{_SML}}}c"):
            ref = (c.get("r") or "").upper()
            if not ref or ref not in donor_sheet:
                continue
            has_formula = c.find(f"{{{_SML}}}f") is not None
            should_restore = False
            if is_skip_sheet:
                if has_formula or not _cell_has_nonempty_cache(c):
                    should_restore = True
            elif ref in sheet_skip_cells:
                should_restore = True
            elif has_formula and ref not in sheet_touched:
                should_restore = True

            if not should_restore:
                continue
            d_t, d_text = donor_sheet[ref]
            if d_text is None:
                continue
            # Do not overwrite shared-string index cells (t="s") across different
            # SST tables when the cell is a plain text constant; formula cells
            # never use t="s" in OOXML (they use t="str").
            if not has_formula and d_t == "s":
                continue
            _apply_cached_value(c, d_t, d_text)
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


def _transfer_values_selective(
    recalc_mod: Any,
    src: Path,
    dst: Path,
    values: dict[str, dict[str, tuple[str | None, str | None]]],
    formulas: dict[tuple[str, str], str],
    spec: dict[str, Any],
) -> tuple[int, int, int, int, list[str], list[dict[str, Any]]]:
    """Merge non-error recalculated values while preserving donor cache on skipped/errored cells."""
    skip_sheets = set(spec.get("skip_sheets") or ())
    skip_cells = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("skip_cells") or {}).items()
    }
    skip_error_writeback = bool(spec.get("skip_error_writeback", True))

    infos, payload = recalc_mod.read_package(src)
    written = 0
    unmatched = 0
    kept_on_error = 0
    recalc_errors: list[dict[str, Any]] = []

    for name, part in recalc_mod.sheet_parts(payload).items():
        if part not in payload:
            continue
        root = etree.fromstring(payload[part], _XML)
        sheet_values = values.get(name, {})
        is_skip_sheet = name in skip_sheets
        sheet_skip_cells = skip_cells.get(name, set())
        changed = False

        for c in root.iter(f"{{{_SML}}}c"):
            f = c.find(f"{{{_SML}}}f")
            if f is None:
                continue
            ref = c.get("r") or ""
            ref_u = ref.upper()
            exempt = is_skip_sheet or (ref_u in sheet_skip_cells)
            got = sheet_values.get(ref) or sheet_values.get(ref_u)
            if got is None or got[1] is None:
                if not exempt:
                    unmatched += 1
                continue
            t, text = got
            is_err = _is_error_value(recalc_mod, t, text)
            if is_err:
                has_donor = _cell_has_nonempty_cache(c)
                recalc_errors.append(
                    {
                        "sheet": name,
                        "cell": ref,
                        "error": text or "#ERROR!",
                        "formula": formulas.get((name, ref), ""),
                        "exempt": exempt,
                        "kept_donor_cache": exempt or (skip_error_writeback and has_donor),
                    }
                )
            if exempt:
                continue
            if is_err and skip_error_writeback and _cell_has_nonempty_cache(c):
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
    return written, unmatched, kept_on_error, charts_updated, chart_refs_skipped, recalc_errors


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
