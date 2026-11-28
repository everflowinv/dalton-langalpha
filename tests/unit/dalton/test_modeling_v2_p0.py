"""Unit tests for Modeling v2 P0-a script layer.

Covers:
1. Everflow read-only baseline recovery (`recover_baseline_model.sh` / `_lib.py`):
   - Large upload channel (`dalton_chat_file_uploads.actual_sha256`)
   - Small inline attachment without `url`/`attachment_id` matched by sender, filename,
     size, time window and verified against `records[0].input_sha256` (SQLite test DB)
   - Encrypted `rclone` backup fallback (fake rclone binary + real AES-256-CBC decrypt)
2. Default template extraction (`extract_default_template.py`), bundled asset, and
   `src/dalton/settings.py` `DALTON_MODEL_DEFAULT_TEMPLATE_PATH` in everflow & core profiles
3. Unchanged clean multi-company baseline `model_check.py` -> `actionable == 0`
4. Quarter rollover (`rollover_actual_cells`) zero false positive vs subtotal/total/ratio
   constant -> `actionable`
5. Column insertion cross-sheet formula shift (`insert.py --preserve-calcpr` + `C14`
   post-insert donor verification) vs non-shift edit on non-target sheet -> `actionable`
6. Donor cache preservation (`restore_donor_cache` + `recalc.py --keep-cache-spec`) under
   both normal recalculation and simulated LibreOffice timeout / no-output exit
   (strictly using `create_autospec`), plus optional live `soffice` test
7. Fault detection: forecast constant (C03), consensus constant & `defined_names`
   indirect reference to snapshot (C04), freeze pane drift (C06), `fullCalcOnLoad=1` (C13),
   Group vs Segment disconnect (C11), semantic vs formula conflict (`missing_evidence`, C10)
8. `write-ledger` & `verify-done` (reports never generated -> `delivered_with_open_issues`,
   `fix_rounds` 0..2 allowed, `report_revision` does not consume `fix_rounds`, deep-update notes)
9. Consensus isolation (`model_consensus.py write-snapshot` & `diff`, 5-point basis alignment,
   breakeven margin ±150bp guard)
"""

from __future__ import annotations

import copy
from datetime import date, datetime
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from typing import Any
from unittest.mock import create_autospec
import uuid
import zipfile

from lxml import etree
import openpyxl
from openpyxl.styles import Color, Font, PatternFill
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.table import Table, TableStyleInfo
import pytest

from src.dalton import settings as dalton_settings

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "scripts"
XLSX_SCRIPTS_DIR = REPO_ROOT / "plugins" / "langalpha_deliverables" / "skills" / "xlsx" / "scripts"
PROD_OPS_DIR = REPO_ROOT / "scripts" / "dalton" / "ops" / "prod"
FIXTURE_XLSX = REPO_ROOT / "tests" / "fixtures" / "dalton" / "china_ecommerce_baseline_fixture.xlsx"
BUNDLED_TEMPLATE_XLSX = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "assets" / "default_model_template.xlsx"
)


def _load_mod(name: str, path: Path) -> Any:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


extract_tpl = _load_mod("extract_default_template", SCRIPTS_DIR / "extract_default_template.py")
mfp = _load_mod("model_fingerprint", SCRIPTS_DIR / "model_fingerprint.py")
medit = _load_mod("model_edit", SCRIPTS_DIR / "model_edit.py")
mcons = _load_mod("model_consensus", SCRIPTS_DIR / "model_consensus.py")
mchk = _load_mod("model_check", SCRIPTS_DIR / "model_check.py")
mhooks = _load_mod("model_xlsx_hooks", SCRIPTS_DIR / "model_xlsx_hooks.py")
recalc_mod = _load_mod("xlsx_recalc_p0", XLSX_SCRIPTS_DIR / "recalc.py")
insert_mod = _load_mod("xlsx_insert_p0", XLSX_SCRIPTS_DIR / "insert.py")
recover_lib = _load_mod("recover_baseline_model_lib", PROD_OPS_DIR / "recover_baseline_model_lib.py")

_REAL_SOFFICE_CALC = recalc_mod.soffice_calculate
_REAL_SHUTIL_WHICH = shutil.which


def _xml_cached_map(xlsx_path: Path, sheet_name: str) -> dict[str, tuple[str | None, str | None]]:
    """Read raw OOXML ``{COORD: (t_attr, v_text)}`` directly from worksheet XML."""
    _, payload = mhooks._read_package(xlsx_path)
    part = mhooks._sheet_parts(payload)[sheet_name]
    root = etree.fromstring(payload[part], mhooks._XML)
    out: dict[str, tuple[str | None, str | None]] = {}
    for c in root.iter(f"{{{mhooks._SML}}}c"):
        ref = (c.get("r") or "").upper()
        v = c.find(f"{{{mhooks._SML}}}v")
        if ref and v is not None:
            out[ref] = (c.get("t"), v.text)
    return out


def _copy_fixture_to_ws(tmp_path: Path, name: str = "China_Ecommerce_v01.xlsx") -> tuple[Path, Path, Path]:
    ws = tmp_path / "ws"
    model_dir = ws / "model"
    research_dir = ws / "research" / "2026-11-27-model-JD"
    model_dir.mkdir(parents=True, exist_ok=True)
    research_dir.mkdir(parents=True, exist_ok=True)
    dst = model_dir / name
    shutil.copyfile(FIXTURE_XLSX, dst)
    return ws, research_dir, dst


def _set_full_calc_on_load(xlsx_path: Path, value: str) -> None:
    infos, payload = mhooks._read_package(xlsx_path)
    wb_root = etree.fromstring(payload["xl/workbook.xml"], mhooks._XML)
    calc = wb_root.find(f"{{{mhooks._SML}}}calcPr")
    if calc is None:
        calc = etree.SubElement(wb_root, f"{{{mhooks._SML}}}calcPr")
    calc.set("fullCalcOnLoad", value)
    payload["xl/workbook.xml"] = etree.tostring(wb_root, encoding="UTF-8", xml_declaration=True)
    with zipfile.ZipFile(xlsx_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename])


def _inject_xml_v_values(xlsx_path: Path, updates: dict[str, dict[str, tuple[str | None, str]]]) -> None:
    infos, payload = mhooks._read_package(xlsx_path)
    parts = mhooks._sheet_parts(payload)
    for sh, cell_map in updates.items():
        part = parts[sh]
        root = etree.fromstring(payload[part], mhooks._XML)
        want = {k.upper(): v for k, v in cell_map.items()}
        for c in root.iter(f"{{{mhooks._SML}}}c"):
            ref = (c.get("r") or "").upper()
            if ref in want:
                mhooks._apply_cached_value(c, want[ref][0], want[ref][1])
        payload[part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(xlsx_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename])


# ---------------------------------------------------------------------------
# 1. Default template extraction & settings.py (dual profile)
# ---------------------------------------------------------------------------


def test_extract_default_template_and_settings_dual_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert FIXTURE_XLSX.is_file()
    assert BUNDLED_TEMPLATE_XLSX.is_file()

    out_tpl = tmp_path / "default_model_template.xlsx"
    res = extract_tpl.extract_default_template(FIXTURE_XLSX, out_tpl, company_tag="JD")
    assert res["status"] == "ok"
    assert res["template_readable"] is True
    assert res["ref_errors"] == 0
    assert res["plugin_calls"] == 0
    assert res["external_refs"] == 0
    assert res["sheets"] == ["README & Changelog", "Valuation", "Key Metrics", "Model", "Subs", "vs Consensus"]

    fp_tpl = mfp.build_fingerprint(out_tpl, ticker="TEST")
    assert fp_tpl["calc_properties"]["full_calc_on_load"] is False
    assert fp_tpl["external_links_baseline"]["cells"] == []
    assert fp_tpl["external_links_baseline"]["parts"] == []
    assert fp_tpl["circular_baseline"] == []

    # Dual-profile settings check: everflow & core resolve cleanly without Everflow private hardcodes
    monkeypatch.delenv("DALTON_MODEL_DEFAULT_TEMPLATE_PATH", raising=False)
    for prof, app_env in (("everflow", "dalton"), ("core", "core")):
        monkeypatch.setenv("DALTON_PROFILE", prof)
        monkeypatch.setenv("APP_ENV", app_env)
        assert dalton_settings.get_dalton_model_default_template_path() == ""
        resolved = dalton_settings.resolve_default_model_template_path()
        assert resolved == BUNDLED_TEMPLATE_XLSX
        assert resolved.is_file()

    monkeypatch.setenv("DALTON_MODEL_DEFAULT_TEMPLATE_PATH", str(out_tpl))
    assert dalton_settings.get_dalton_model_default_template_path() == str(out_tpl)
    assert dalton_settings.resolve_default_model_template_path() == out_tpl


# ---------------------------------------------------------------------------
# 2. Unchanged clean baseline -> actionable == 0
# ---------------------------------------------------------------------------


def _inject_external_link_part(xlsx_path: Path) -> None:
    """Attach a real OOXML ``xl/externalLinks/externalLink1.xml`` part with Content_Types & workbook rels."""
    ext_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<externalLink xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<externalBook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="rId1">'
        b'<sheetNames><sheetName val="FX"/></sheetNames>'
        b'</externalBook></externalLink>'
    )
    ext_rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink"
    ext_ct = "application/vnd.openxmlformats-officedocument.spreadsheetml.externalLink+xml"
    infos, payload = mhooks._read_package(xlsx_path)
    if "xl/externalLinks/externalLink1.xml" not in payload:
        zi = zipfile.ZipInfo("xl/externalLinks/externalLink1.xml")
        zi.compress_type = zipfile.ZIP_DEFLATED
        infos.append(zi)
    payload["xl/externalLinks/externalLink1.xml"] = ext_xml

    ct_raw = payload.get("[Content_Types].xml")
    if ct_raw:
        ct_root = etree.fromstring(ct_raw, mhooks._XML)
        if not any(ov.get("PartName") == "/xl/externalLinks/externalLink1.xml" for ov in ct_root.findall(f"{{{mhooks._CT_NS}}}Override")):
            etree.SubElement(
                ct_root,
                f"{{{mhooks._CT_NS}}}Override",
                PartName="/xl/externalLinks/externalLink1.xml",
                ContentType=ext_ct,
            )
            payload["[Content_Types].xml"] = etree.tostring(ct_root, xml_declaration=True, encoding="UTF-8", standalone=True)

    rels_raw = payload.get("xl/_rels/workbook.xml.rels")
    if rels_raw:
        rels_root = etree.fromstring(rels_raw, mhooks._XML)
        if not any(r.get("Type") == ext_rel_type for r in rels_root.findall(f"{{{mhooks._PKG_REL_NS}}}Relationship")):
            used_ids = {r.get("Id") or "" for r in rels_root.findall(f"{{{mhooks._PKG_REL_NS}}}Relationship")}
            n = 1
            while f"rId{n}" in used_ids:
                n += 1
            etree.SubElement(
                rels_root,
                f"{{{mhooks._PKG_REL_NS}}}Relationship",
                Id=f"rId{n}",
                Type=ext_rel_type,
                Target="externalLinks/externalLink1.xml",
            )
            payload["xl/_rels/workbook.xml.rels"] = etree.tostring(rels_root, xml_declaration=True, encoding="UTF-8", standalone=True)

    with zipfile.ZipFile(xlsx_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename])


def test_unchanged_baseline_has_zero_actionable_and_single_engine_note(tmp_path: Path) -> None:
    ws, rdir, base = _copy_fixture_to_ws(tmp_path)
    # Generate consensus diff bound to base so both enabled stages (check & consensus) complete
    mcons.compute_consensus_diff(
        model_path=base,
        ticker="JD",
        research_dir=rdir,
        consensus_data={
            "source_type": "Market Consensus",
            "as_of_date": "2026-11-27",
            "items": [{"period": "2026E", "metric": "Total Revenue", "consensus": 12000.0, "rationale": "Inline"}],
        },
    )
    res = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir,
        candidate_path=base,
        baseline_path=base,
        run_id="run-clean-01",
    )
    assert res["readable"] is True
    assert res["counts"]["actionable"] == 0, res["findings"]
    assert res["counts"]["baseline_legacy"] >= 1  # baseline circular pair preserved as legacy
    assert res["final_status"] == "pass"

    check_md = (rdir / "model_dump" / "model-check.md").read_text(encoding="utf-8")
    assert "本轮复算只用一个引擎" in check_md
    ledger = json.loads((rdir / "model_dump" / "model-verification.json").read_text(encoding="utf-8"))
    assert ledger["calc_engine"] == "libreoffice"
    assert ledger["independent_verify"] is False
    assert ledger["run_id"] == "run-clean-01"

    vd = mchk.verify_done(workspace_dir=ws, ticker="JD", research_dir=rdir, expected_run_id="run-clean-01")
    assert vd["ok"] is True, vd
    assert vd["final_status"] == "pass"


def test_realistic_dirty_baseline_zero_actionable_and_json_safe_fingerprint(tmp_path: Path) -> None:
    """Principle #1 & Codex #7 / Grok #1: realistic baseline with legacy errors, dates, theme tab,
    defined-name external link & cycle, and legacy same-value constant -> JSON-safe & actionable == 0."""
    ws, _, base = _copy_fixture_to_ws(tmp_path, name="China_Ecommerce_realistic_v01.xlsx")
    wb = openpyxl.load_workbook(base, data_only=False)
    ws_jd = wb["Model - JD"]
    # Theme-colored sheet tab (theme=4, tint=0.4) and native Excel datetime/date cells
    ws_jd.sheet_properties.tabColor = Color(theme=4, tint=0.4)
    ws_jd["B2"].value = datetime(2026, 11, 27, 9, 30, 0)
    ws_jd["C2"].value = date(2026, 11, 27)
    # Pre-existing #N/A, #DIV/0!, and #REF! inside IFERROR on untouched baseline
    ws_jd["D49"].value = "Legacy NA Metric"
    ws_jd["E49"].value = "=NA()"
    ws_jd["D50"].value = "Legacy Div0 Metric"
    ws_jd["E50"].value = "=1/0"
    ws_jd["D51"].value = "Legacy Wrapped Ref"
    ws_jd["E51"].value = "=IFERROR(#REF!,0)"
    # External link accessed via defined_names
    wb.defined_names.add(DefinedName(name="ExtFxRate", attr_text="[ExternalRates.xlsx]FX!$B$2"))
    ws_jd["D52"].value = "Defined Name Ext FX"
    ws_jd["E52"].value = "=ExtFxRate*1.0"
    # Circular reference routed through defined_names
    wb.defined_names.add(DefinedName(name="CircNamedB", attr_text="'Model - JD'!$E$54"))
    ws_jd["D53"].value = "Named Circ A"
    ws_jd["E53"].value = "=CircNamedB*0.5+1"
    ws_jd["D54"].value = "Named Circ B"
    ws_jd["E54"].value = "=E53*0.5+1"
    # Pre-existing same-value constant in baseline matching vs Consensus D2
    cons_d2 = wb["vs Consensus"]["D2"].value
    ws_jd["D55"].value = "Legacy Constant Ref"
    ws_jd["G55"].value = cons_d2
    wb.save(base)
    wb.close()

    _inject_external_link_part(base)
    _inject_xml_v_values(
        base,
        {
            "Model - JD": {
                "E49": ("e", "#N/A"),
                "E50": ("e", "#DIV/0!"),
                "E51": ("n", "0"),
                "E52": ("n", "7.25"),
                "E53": (None, "2.0"),
                "E54": (None, "2.0"),
            }
        },
    )

    # CLI JSON export must succeed cleanly with dates + theme tabColor (no TypeError!)
    fp_out = tmp_path / "realistic_fp.json"
    assert mfp.main([str(base), "--out", str(fp_out), "--ticker", "JD"]) == 0
    fp = json.loads(fp_out.read_text(encoding="utf-8"))
    assert fp["sheets"]["Model - JD"]["cells"]["B2"]["cached_value"] == "2026-11-27T09:30:00"
    assert str(fp["sheets"]["Model - JD"]["cells"]["C2"]["cached_value"]).startswith("2026-11-27")
    jd_order_entry = next(s for s in fp["sheet_order"] if s["name"] == "Model - JD")
    assert isinstance(jd_order_entry["tab_color"], str) and jd_order_entry["tab_color"].startswith("theme:4")
    # External link via defined_names is classified as external_link & in external_links_baseline
    assert fp["sheets"]["Model - JD"]["cells"]["E52"]["subcategory"] == "external_link"
    assert any(c["cell"] == "E52" for c in fp["external_links_baseline"]["cells"])
    # Circular reference via defined_names enters circular_baseline
    assert any("Model - JD!E53" in scc and "Model - JD!E54" in scc for scc in fp["circular_baseline"])

    # Unchanged realistic dirty baseline MUST yield actionable == 0!
    chk = mchk.run_checks(candidate_path=base, ticker="JD", baseline_path=base, fingerprint=fp)
    assert chk["counts"]["actionable"] == 0, chk["findings"]
    leg_cells = {(f["check_id"], f.get("cell")) for f in chk["findings"] if f["severity_class"] == "baseline_legacy"}
    assert ("C13", "E49") in leg_cells
    assert ("C13", "E50") in leg_cells
    assert ("C13", "E51") in leg_cells


# ---------------------------------------------------------------------------
# 3. Rollover actual detail exemption vs subtotal/total/ratio constant -> actionable
# ---------------------------------------------------------------------------


def test_rollover_actual_detail_exempt_and_subtotal_constant_actionable(tmp_path: Path) -> None:
    ws, rdir, base = _copy_fixture_to_ws(tmp_path)
    # 3a. Valid rollover: 2Q26E (col P) detail rows become actuals; subtotals stay formulas
    plan_ok = {
        "rollover_actual_cols": {"Model - JD": ["P"]},
        "cell_edits": [
            {"sheet": "Model - JD", "cell": "P1", "value": "2Q26"},
            {"sheet": "Model - JD", "cell": "P4", "value": 2450.0, "style_role": "historical_actual"},
            {"sheet": "Model - JD", "cell": "P5", "value": 590.0, "style_role": "historical_actual"},
        ],
        "changelog_entry": {
            "version": "v02",
            "date": "2026-11-27",
            "baseline": base.name,
            "summary": "Rolled 2Q26 detail actuals",
        },
    }
    edit_res = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan=plan_ok,
        workspace_dir=ws,
        skip_recalc=True,
    )
    cm = edit_res["coord_map"]
    assert "P4" in cm["rollover_actual_cells"]["Model - JD"]
    assert "P5" in cm["rollover_actual_cells"]["Model - JD"]

    # Update cached values on affected formula cells to reflect new 2Q26 actuals consistently
    cand_ok = Path(edit_res["candidate_path"])
    old_p4, old_p5 = 2420.0, 583.2
    d_p4, d_p5 = 2450.0 - old_p4, 590.0 - old_p5
    d_rev = d_p4 + d_p5
    orig_fp = mfp.build_fingerprint(base, ticker="JD")
    jd_cells = orig_fp["sheets"]["Model - JD"]["cells"]
    new_p6 = jd_cells["P6"]["cached_value"] + d_rev
    new_p9 = new_p6 - jd_cells["P8"]["cached_value"]
    new_p12 = new_p9 - jd_cells["P11"]["cached_value"]
    new_g4 = jd_cells["G4"]["cached_value"] + d_p4
    new_g5 = jd_cells["G5"]["cached_value"] + d_p5
    new_g6 = new_g4 + new_g5
    new_g9 = new_g6 - jd_cells["G8"]["cached_value"]
    new_g12 = new_g9 - jd_cells["G11"]["cached_value"]
    _inject_xml_v_values(
        cand_ok,
        {
            "Model - JD": {
                "P6": (None, str(new_p6)),
                "P9": (None, str(new_p9)),
                "P12": (None, str(new_p12)),
                "P21": (None, str(new_p12 - jd_cells["P19"]["cached_value"] - jd_cells["P20"]["cached_value"])),
                "G4": (None, str(new_g4)),
                "G5": (None, str(new_g5)),
                "G6": (None, str(new_g6)),
                "G9": (None, str(new_g9)),
                "G12": (None, str(new_g12)),
                "G21": (None, str(new_g12 - jd_cells["G19"]["cached_value"] - jd_cells["G20"]["cached_value"])),
            }
        },
    )

    chk_ok = mchk.run_checks(
        candidate_path=cand_ok,
        ticker="JD",
        baseline_path=base,
        coord_map=cm,
    )
    c03_ok = [f for f in chk_ok["findings"] if f["check_id"] == "C03" and f["severity_class"] == "actionable"]
    assert c03_ok == [], c03_ok
    assert chk_ok["counts"]["actionable"] == 0, chk_ok["findings"]

    # 3b. Bad rollover: subtotal row Total Revenue (P6) overwritten with constant -> actionable!
    plan_bad = {
        "rollover_actual_cols": {"Model - JD": ["P"]},
        "cell_edits": [
            {"sheet": "Model - JD", "cell": "P1", "value": "2Q26"},
            {"sheet": "Model - JD", "cell": "P6", "value": 3040.0},
        ],
        "changelog_entry": {"version": "v03", "date": "2026-11-27", "baseline": base.name, "summary": "Bad subtotal"},
    }
    bad_res = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan=plan_bad,
        workspace_dir=ws,
        skip_recalc=True,
    )
    # §8.4 guard in model_edit prevents P6 from entering rollover_actual_cells
    assert "P6" not in (bad_res["coord_map"]["rollover_actual_cells"].get("Model - JD") or [])
    chk_bad = mchk.run_checks(
        candidate_path=bad_res["candidate_path"],
        ticker="JD",
        baseline_path=base,
        coord_map=bad_res["coord_map"],
    )
    c03_bad = [f for f in chk_bad["findings"] if f["check_id"] == "C03" and f["severity_class"] == "actionable"]
    assert any(f["cell"] == "P6" for f in c03_bad), chk_bad["findings"]


# ---------------------------------------------------------------------------
# 4. Column insert cross-sheet shift (C14 passes) vs non-shift edit on non-target sheet
# ---------------------------------------------------------------------------


def test_column_insert_cross_sheet_shift_c14_passes_and_non_shift_fails(tmp_path: Path) -> None:
    ws, rdir, base = _copy_fixture_to_ws(tmp_path)
    # Insert 1 column before P on Model - JD (shifts P -> Q, so Model - BABA!F6 ='Model - JD'!P6 -> ='Model - JD'!Q6)
    plan_shift = {
        "ops": [
            {
                "op": "insert_columns",
                "sheet": "Model - JD",
                "at": "P",
                "count": 1,
                "copy_style_from": "O",
            }
        ],
        "cell_edits": [
            {"sheet": "Model - JD", "cell": "P1", "value": "1Q26 Adj"},
        ],
        "changelog_entry": {
            "version": "v02",
            "date": "2026-11-27",
            "baseline": base.name,
            "summary": "Inserted column P on Model - JD",
        },
    }
    edit_res = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan=plan_shift,
        workspace_dir=ws,
        skip_recalc=True,
    )
    cand = Path(edit_res["candidate_path"])
    donor = Path(edit_res["cache_donor_path"])

    # Verify insert.py --preserve-calcpr kept fullCalcOnLoad disabled on donor,
    # while non-template --skip-recalc enables fullCalcOnLoad=1 on candidate (R6 #2),
    # AND shifted Model - BABA!F6!
    donor_fp = mfp.build_fingerprint(donor, ticker="JD")
    assert donor_fp["calc_properties"]["full_calc_on_load"] is False
    cand_fp = mfp.build_fingerprint(cand, ticker="JD")
    assert cand_fp["calc_properties"]["full_calc_on_load"] is True
    assert cand_fp["sheets"]["Model - BABA"]["cells"]["F6"]["formula"] == "='Model - JD'!Q6"
    # Verify donor cache restored cell-by-cell on both Model - BABA and Subs - BABA
    assert _xml_cached_map(cand, "Model - BABA") == _xml_cached_map(donor, "Model - BABA")
    assert _xml_cached_map(cand, "Subs - BABA") == _xml_cached_map(donor, "Subs - BABA")

    chk = mchk.run_checks(
        candidate_path=cand,
        ticker="JD",
        baseline_path=base,
        coord_map=edit_res["coord_map"],
    )
    c14_actionable = [f for f in chk["findings"] if f["check_id"] == "C14" and f["severity_class"] == "actionable"]
    assert c14_actionable == [], c14_actionable
    assert chk["counts"]["actionable"] == 0, chk["findings"]

    # Now tamper with non-target sheet Model - BABA!E4 (non-shift constant edit) -> C14 actionable!
    tampered = ws / "model" / "China_Ecommerce_v03.xlsx"
    shutil.copyfile(cand, tampered)
    wb = openpyxl.load_workbook(tampered, data_only=False)
    wb["Model - BABA"]["E4"].value = 999999.0
    wb.save(tampered)
    wb.close()
    # Even if we restore donor cache on formula cells, constant E4 was modified
    chk_bad = mchk.run_checks(
        candidate_path=tampered,
        ticker="JD",
        baseline_path=base,
        coord_map=edit_res["coord_map"],
    )
    c14_bad = [f for f in chk_bad["findings"] if f["check_id"] == "C14" and f["severity_class"] == "actionable"]
    assert any(f["sheet"] == "Model - BABA" and f["cell"] == "E4" for f in c14_bad), c14_bad


def test_column_insert_shifts_auto_note_and_registers_touched_cells(tmp_path: Path) -> None:
    """Principle #3 & Codex #1: after inserting a column before P, auto-note writes to shifted X (not W)
    and registers the note coordinate in coord_map.touched_cells without overwriting W's forecast formula."""
    ws, _, base = _copy_fixture_to_ws(tmp_path)
    plan = {
        "ops": [
            {"op": "insert_columns", "sheet": "Model - JD", "at": "P", "count": 1, "copy_style_from": "O"}
        ],
        "cell_edits": [
            {"sheet": "Model - JD", "cell": "P1", "value": "1Q26 Adj"},
            {"sheet": "Model - JD", "cell": "Q4", "value": "=M4*(1+Q25)", "note": "<Updated post-insert note"},
        ],
        "changelog_entry": {
            "version": "v02",
            "date": "2026-11-27",
            "baseline": base.name,
            "summary": "Shifted note column check",
        },
    }
    res = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan=plan,
        workspace_dir=ws,
        skip_recalc=True,
    )
    cand = Path(res["candidate_path"])
    wb = openpyxl.load_workbook(cand, data_only=False)
    try:
        ws_jd = wb["Model - JD"]
        # Original col V (4Q27E) shifted to W, original note col W shifted to X!
        assert str(ws_jd["W4"].value).startswith("="), f"W4 forecast formula was overwritten: {ws_jd['W4'].value!r}"
        assert ws_jd["X4"].value == "<Updated post-insert note"
    finally:
        wb.close()
    assert "X4" in res["coord_map"]["touched_cells"]["Model - JD"]


# ---------------------------------------------------------------------------
# 5. Donor cache restore: normal recalc & simulated LibreOffice timeout / no-output exit
# ---------------------------------------------------------------------------


def test_donor_cache_preserved_on_normal_recalc_and_soffice_timeout_or_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 4 (Principle #2, Codex #2/#3, Grok #2/疑似): column insert + all 3 recalc paths preserve
    explicit t="n", non-target sheets (Model - BABA & Subs - BABA), CIQ, external links (incl. via
    defined_names + real externalLinks part), circular pairs, and =IFERROR(F45, 0) downstream of CIQ;
    editing an exempt cell to a constant is NOT clobbered; a newly added broken formula keeps its error."""
    ws, _, base = _copy_fixture_to_ws(tmp_path)
    wb = openpyxl.load_workbook(base, data_only=False)
    ws_jd = wb["Model - JD"]
    # Add IFERROR downstream of CIQ (G45 = IFERROR(F45, 0)), defined-name ext link (E49), and real externalLinks part
    wb.defined_names.add(DefinedName(name="ExtFxLink", attr_text="[ExternalRates.xlsx]FX!$B$2"))
    ws_jd["G45"].value = "=IFERROR(F45,0)"
    ws_jd["D49"].value = "FX via Name"
    ws_jd["E49"].value = "=ExtFxLink*1.0"
    wb.save(base)
    wb.close()
    _inject_external_link_part(base)
    # Put explicit t="n" on donor cells across both target & non-target sheets to verify t="n" fidelity!
    _inject_xml_v_values(
        base,
        {
            "Model - JD": {
                "E45": ("n", "38.5"),
                "F45": ("n", "38.5"),
                "G45": ("n", "38.5"),
                "E46": ("n", "7.25"),
                "E47": ("n", "12.5"),
                "E48": ("n", "12.5"),
                "E49": ("n", "7.25"),
                "E6": ("n", "10000.0"),
            },
            "Model - BABA": {
                "F4": ("n", "97200.0"),
                "E7": ("n", "105.25"),
            },
            "Subs - BABA": {
                "E3": ("n", "14580.0"),
            },
        },
    )

    # 5a. Normal recalc WITH column insert before P + newly added broken formula (H50) + edited G4
    def _fake_soffice_ok(src: Path, out_dir: Path, timeout: int) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        produced = out_dir / (src.stem + ".xlsx")
        shutil.copyfile(src, produced)
        _inject_xml_v_values(
            produced,
            {
                "Model - JD": {
                    "E45": ("e", "#NAME?"),
                    "F45": ("e", "#NAME?"),
                    "G45": (None, "0"),  # LibreOffice evaluates IFERROR(F45, 0) -> 0! Must NOT overwrite donor 38.5!
                    "E46": ("e", "#REF!"),
                    "E47": (None, "0"),
                    "E48": (None, "0"),
                    "E49": ("e", "#REF!"),
                    "G4": (None, "10500.5"),
                    "H50": ("e", "#NAME?"),  # Newly added broken formula -> must KEEP error for C13!
                },
                "Model - BABA": {
                    "E7": ("e", "#NAME?"),
                    "F4": ("e", "#REF!"),
                },
                "Subs - BABA": {
                    "E3": ("e", "#REF!"),
                },
            },
        )
        return produced

    soffice_stub = create_autospec(_REAL_SOFFICE_CALC, side_effect=_fake_soffice_ok)
    which_stub = create_autospec(_REAL_SHUTIL_WHICH, return_value="/usr/bin/soffice")
    monkeypatch.setattr(recalc_mod, "soffice_calculate", soffice_stub)
    monkeypatch.setattr(recalc_mod.shutil, "which", which_stub)

    plan_with_insert = {
        "ops": [
            {"op": "insert_columns", "sheet": "Model - JD", "at": "P", "count": 1, "copy_style_from": "O"}
        ],
        "cell_edits": [
            {"sheet": "Model - JD", "cell": "P1", "value": "1Q26 Adj"},
            {"sheet": "Model - JD", "cell": "G4", "value": "=SUM(O4:S4)"},
            {"sheet": "Model - JD", "cell": "H50", "value": "=UNKNOWN_NEW_FUNC(1)"},
        ],
        "changelog_entry": {"version": "v02", "date": "2026-11-27", "baseline": base.name, "summary": "Recalc + insert"},
    }
    res_ok = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan=plan_with_insert,
        workspace_dir=ws,
        recalc_mod_override=recalc_mod,
    )
    cand_ok = Path(res_ok["candidate_path"])
    donor_ok = Path(res_ok["cache_donor_path"])

    # Non-target company sheets (BOTH Model - BABA and Subs - BABA) match donor <v> AND t="n" cell-by-cell
    assert _xml_cached_map(cand_ok, "Model - BABA") == _xml_cached_map(donor_ok, "Model - BABA")
    assert _xml_cached_map(cand_ok, "Subs - BABA") == _xml_cached_map(donor_ok, "Subs - BABA")
    assert _xml_cached_map(cand_ok, "Model - BABA")["F4"] == ("n", "97200.0")
    assert _xml_cached_map(cand_ok, "Subs - BABA")["E3"] == ("n", "14580.0")

    jd_ok_cache = _xml_cached_map(cand_ok, "Model - JD")
    jd_donor_cache = _xml_cached_map(donor_ok, "Model - JD")
    assert jd_ok_cache["E45"] == jd_donor_cache["E45"] == ("n", "38.5")  # CIQ preserved with t="n"!
    assert jd_ok_cache["F45"] == jd_donor_cache["F45"] == ("n", "38.5")  # downstream preserved!
    assert jd_ok_cache["G45"] == jd_donor_cache["G45"] == ("n", "38.5")  # IFERROR(F45,0) NOT overwritten to 0!
    assert jd_ok_cache["E46"] == jd_donor_cache["E46"] == ("n", "7.25")  # external link preserved!
    assert jd_ok_cache["E49"] == jd_donor_cache["E49"] == ("n", "7.25")  # defined-name external link preserved!
    assert jd_ok_cache["E47"] == jd_donor_cache["E47"] == ("n", "12.5")  # circular A preserved!
    assert jd_ok_cache["E48"] == jd_donor_cache["E48"] == ("n", "12.5")  # circular B preserved!
    assert jd_ok_cache["E6"] == jd_donor_cache["E6"] == ("n", "10000.0")  # untouched formula kept t="n"!
    assert jd_ok_cache["G4"] == (None, "10500.5")  # edited formula merged!
    assert jd_ok_cache["H50"] == ("e", "#NAME?")  # newly added broken formula kept error!

    # C13 classifies F45 & G45 (IFERROR downstream diff) as baseline_legacy, and H50 (new broken formula) as actionable!
    chk_res = mchk.run_checks(
        candidate_path=cand_ok,
        ticker="JD",
        baseline_path=base,
        coord_map=res_ok["coord_map"],
    )
    c13_act = [f for f in chk_res["findings"] if f["check_id"] == "C13" and f["severity_class"] == "actionable"]
    c13_leg = [f for f in chk_res["findings"] if f["check_id"] == "C13" and f["severity_class"] == "baseline_legacy"]
    assert any(f.get("cell") == "H50" for f in c13_act), c13_act
    assert not any(f.get("cell") in ("E45", "F45", "G45", "E46", "E49", "E47", "E48") for f in c13_act), c13_act
    assert any(f.get("cell") == "F45" for f in c13_leg), c13_leg
    assert any(f.get("cell") == "G45" for f in c13_leg), c13_leg

    # Verify restore_donor_cache re-inserts a cell (including shared-string t="s") dropped from a skip_sheet
    dropped_copy = tmp_path / "dropped_baba.xlsx"
    shutil.copyfile(donor_ok, dropped_copy)
    infos_d, payload_d = mhooks._read_package(dropped_copy)
    baba_part = mhooks._sheet_parts(payload_d)["Model - BABA"]
    baba_root = etree.fromstring(payload_d[baba_part], mhooks._XML)
    for c_el in list(baba_root.iter(f"{{{mhooks._SML}}}c")):
        if (c_el.get("r") or "").upper() in ("D4", "F4"):
            c_el.getparent().remove(c_el)
    payload_d[baba_part] = etree.tostring(baba_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(dropped_copy, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos_d:
            zout.writestr(info, payload_d[info.filename])
    mhooks.restore_donor_cache(
        dropped_copy,
        {"cache_donor_path": str(donor_ok), "skip_sheets": ["Model - BABA"], "skip_cells": {}, "touched_formula_cells": {}},
    )
    wb_re = openpyxl.load_workbook(dropped_copy, data_only=False)
    try:
        assert wb_re["Model - BABA"]["D4"].value == "BABA Revenue"
        assert wb_re["Model - BABA"]["F4"].value == "=E4*1.08"
    finally:
        wb_re.close()
    assert _xml_cached_map(dropped_copy, "Model - BABA")["F4"] == ("n", "97200.0")
    # Verify overwriting a previously exempt coordinate (E45) to an actual constant is NOT clobbered by donor cache!
    res_overwrite_exempt = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "cell_edits": [{"sheet": "Model - JD", "cell": "E45", "value": 41.25}],
            "changelog_entry": {"version": "v05", "date": "2026-11-27", "baseline": base.name, "summary": "Replace CIQ with actual"},
        },
        output_path=ws / "model" / "China_Ecommerce_v05.xlsx",
        workspace_dir=ws,
        skip_recalc=True,
    )
    assert _xml_cached_map(Path(res_overwrite_exempt["candidate_path"]), "Model - JD")["E45"][1] == "41.25"

    # 5b. Simulated LibreOffice TimeoutExpired (with column insert)
    timeout_stub = create_autospec(
        _REAL_SOFFICE_CALC,
        side_effect=subprocess.TimeoutExpired(cmd=["soffice"], timeout=10),
    )
    monkeypatch.setattr(recalc_mod, "soffice_calculate", timeout_stub)
    res_to = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan=plan_with_insert,
        output_path=ws / "model" / "China_Ecommerce_v03.xlsx",
        workspace_dir=ws,
        recalc_mod_override=recalc_mod,
    )
    assert res_to["recalc_status"] == "recalc_failed"
    assert res_to["recalc_result"]["donor_cache_restored"] is True
    cand_to = Path(res_to["candidate_path"])
    donor_to = Path(res_to["cache_donor_path"])
    assert _xml_cached_map(cand_to, "Model - BABA") == _xml_cached_map(donor_to, "Model - BABA")
    assert _xml_cached_map(cand_to, "Subs - BABA") == _xml_cached_map(donor_to, "Subs - BABA")
    jd_to = _xml_cached_map(cand_to, "Model - JD")
    jd_to_d = _xml_cached_map(donor_to, "Model - JD")
    for coord_k in ("E45", "F45", "G45", "E46", "E47", "E48", "E49"):
        assert jd_to[coord_k] == jd_to_d[coord_k], f"timeout cache mismatch at {coord_k}"

    # 5c. Simulated LibreOffice exit with NO output workbook (with column insert)
    no_out_stub = create_autospec(
        _REAL_SOFFICE_CALC,
        side_effect=lambda src, out_dir, timeout: out_dir / "missing_output.xlsx",
    )
    monkeypatch.setattr(recalc_mod, "soffice_calculate", no_out_stub)
    res_no = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan=plan_with_insert,
        output_path=ws / "model" / "China_Ecommerce_v04.xlsx",
        workspace_dir=ws,
        recalc_mod_override=recalc_mod,
    )
    assert res_no["recalc_status"] == "recalc_failed"
    assert res_no["recalc_result"]["donor_cache_restored"] is True
    cand_no = Path(res_no["candidate_path"])
    donor_no = Path(res_no["cache_donor_path"])
    assert _xml_cached_map(cand_no, "Model - BABA") == _xml_cached_map(donor_no, "Model - BABA")
    assert _xml_cached_map(cand_no, "Subs - BABA") == _xml_cached_map(donor_no, "Subs - BABA")
    jd_no = _xml_cached_map(cand_no, "Model - JD")
    jd_no_d = _xml_cached_map(donor_no, "Model - JD")
    for coord_k in ("E45", "F45", "G45", "E46", "E47", "E48", "E49"):
        assert jd_no[coord_k] == jd_no_d[coord_k], f"no-output cache mismatch at {coord_k}"


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice soffice not installed in this environment")
def test_real_libreoffice_keep_cache_recalc_smoke(tmp_path: Path) -> None:
    ws, _, base = _copy_fixture_to_ws(tmp_path)
    res = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "cell_edits": [{"sheet": "Model - JD", "cell": "P25", "value": 0.12}],
            "changelog_entry": {"version": "v02", "date": "2026-11-27", "baseline": base.name, "summary": "Live LO"},
        },
        workspace_dir=ws,
    )
    assert res["recalc_status"] in ("success", "errors_found")
    assert _xml_cached_map(Path(res["candidate_path"]), "Model - BABA") == _xml_cached_map(
        Path(res["cache_donor_path"]), "Model - BABA"
    )


# ---------------------------------------------------------------------------
# 6. Fault detection: C03, C04 (constant & defined_names), C06, C13, C11, C10 conflict
# ---------------------------------------------------------------------------


def test_fault_detection_c03_c04_c06_c11_c13_and_c10_semantic_conflict(tmp_path: Path) -> None:
    ws, _, base = _copy_fixture_to_ws(tmp_path)
    base_fp = mfp.build_fingerprint(base, ticker="JD")
    cons_d2 = base_fp["sheets"]["vs Consensus"]["cells"]["D2"]["cached_value"]

    # Build a faulty candidate inheriting all baseline sheets
    cand = ws / "model" / "China_Ecommerce_v02.xlsx"
    shutil.copyfile(base, cand)
    wb = openpyxl.load_workbook(cand, data_only=False)
    ws_jd = wb["Model - JD"]

    # 1. C03 + C04 constant: overwrite 2026E Total Revenue (G6) with exact consensus snapshot D2 constant
    ws_jd["G6"].value = cons_d2
    # 2. C04 via defined_names: define name StreetNI -> 'vs Consensus'!$D$4 and reference it in 2026E Net Income (G14)
    wb.defined_names.add(DefinedName(name="StreetNI", attr_text="'vs Consensus'!$D$4"))
    ws_jd["G14"].value = "=StreetNI*1.0"
    # 3. C06 freeze pane drift: E2 (4,1) -> AY102 (50,101)
    ws_jd.freeze_panes = "AY102"
    # 4. C10 semantic conflict (missing_evidence, NOT actionable): Ending Active Merchants (row 29) uses =SUM(K29:N29)
    ws_jd["F29"].value = "=SUM(K29:N29)"
    # 5. Append changelog row so C01 doesn't mask anything
    wb["README & Changelog"].cell(3, 1, "v02")
    wb["README & Changelog"].cell(3, 4, "Fault injection test")
    wb.save(cand)
    wb.close()

    # 6. C13 fullCalcOnLoad=1
    _set_full_calc_on_load(cand, "1")
    # 7. C11 Group vs Segment Operating Profit disconnect on forecast col Q (3Q26E): Group OP = 900 vs seg sum ~170
    _inject_xml_v_values(
        cand,
        {
            "Model - JD": {
                "Q12": (None, "900.0"),
                "Q19": (None, "150.0"),
                "Q20": (None, "30.0"),
                "Q21": (None, "-10.0"),
            }
        },
    )

    cm = {
        "ops": [],
        "touched_cells": {"Model - JD": ["G6", "G14", "Q12"]},
        "added_period_cols": {},
        "rollover_actual_cols": {},
        "rollover_actual_cells": {},
        "cache_donor_path": str(base),
    }
    chk = mchk.run_checks(candidate_path=cand, ticker="JD", baseline_path=base, coord_map=cm)
    act_by_id = {f["check_id"] for f in chk["findings"] if f["severity_class"] == "actionable"}
    me_by_id = {f["check_id"] for f in chk["findings"] if f["severity_class"] == "missing_evidence"}

    assert "C03" in act_by_id, chk["findings"]
    assert any(f["check_id"] == "C03" and f["severity_class"] == "actionable" and f["cell"] == "G6" for f in chk["findings"])
    assert "C04" in act_by_id, chk["findings"]
    c04_cells = {f["cell"] for f in chk["findings"] if f["check_id"] == "C04" and f["severity_class"] == "actionable"}
    assert "G6" in c04_cells  # constant == consensus
    assert "G14" in c04_cells  # defined_names -> consensus snapshot
    assert any(f["check_id"] == "C06" and f["severity_class"] == "actionable" and f["sheet"] == "Model - JD" for f in chk["findings"])
    assert any(f["check_id"] == "C11" and f["severity_class"] == "actionable" and f["cell"] == "Q12" for f in chk["findings"])
    assert any(f["check_id"] == "C13" and f["severity_class"] == "actionable" and "fullCalcOnLoad" in f["message"] for f in chk["findings"])
    # C10 semantic-vs-formula conflict -> missing_evidence (NOT actionable!)
    assert "C10" in me_by_id, chk["findings"]
    assert not any(
        f["check_id"] == "C10" and f["severity_class"] == "actionable" and f.get("cell") == "D29"
        for f in chk["findings"]
    )

    # 8. Also verify C11 Group vs Segment profit disconnect on a NEWLY ADDED forecast column (Scenario 5)
    res_new_fc = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "ops": [{"op": "insert_columns", "sheet": "Model - JD", "at": "W", "count": 1, "copy_style_from": "V"}],
            "added_period_cols": {"Model - JD": ["W"]},
            "cell_edits": [
                {"sheet": "Model - JD", "cell": "W1", "value": "1Q28E"},
                {"sheet": "Model - JD", "cell": "W12", "value": "=W9-W11"},
                {"sheet": "Model - JD", "cell": "W19", "value": "=W12*0.9"},
                {"sheet": "Model - JD", "cell": "W20", "value": "=W12*0.2"},
                {"sheet": "Model - JD", "cell": "W21", "value": "=-10"},
            ],
            "changelog_entry": {"version": "v03", "date": "2026-11-27", "baseline": base.name, "summary": "New forecast col C11"},
        },
        workspace_dir=ws,
        skip_recalc=True,
    )
    cand_new_fc = Path(res_new_fc["candidate_path"])
    _inject_xml_v_values(
        cand_new_fc,
        {
            "Model - JD": {
                "W12": (None, "850.0"),
                "W19": (None, "200.0"),
                "W20": (None, "50.0"),
                "W21": (None, "-10.0"),
            }
        },
    )
    chk_new_fc = mchk.run_checks(
        candidate_path=cand_new_fc,
        ticker="JD",
        baseline_path=base,
        coord_map=res_new_fc["coord_map"],
    )
    assert any(
        f["check_id"] == "C11" and f["severity_class"] == "actionable" and f.get("cell") == "W12"
        for f in chk_new_fc["findings"]
    ), chk_new_fc["findings"]


def test_c04_structured_table_ref_large_range_and_period_metric_scoping(tmp_path: Path) -> None:
    """Codex #5: C04 detects >256-cell range & Excel Table structured refs to consensus snapshot,
    while NOT false-positiving on a different-period/different-metric constant."""
    ws, _, base = _copy_fixture_to_ws(tmp_path)
    cand = ws / "model" / "China_Ecommerce_c04_v02.xlsx"
    shutil.copyfile(base, cand)
    wb = openpyxl.load_workbook(cand, data_only=False)
    ws_con = wb["vs Consensus"]
    tbl = Table(displayName="ConsensusTbl", ref="A1:H5")
    tbl.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9")
    ws_con.add_table(tbl)

    ws_jd = wb["Model - JD"]
    # 1. >256-cell range reference hitting vs Consensus!D2..D5
    ws_jd["H6"].value = "=SUM('vs Consensus'!D2:D400)"
    # 2. Structured Excel Table reference hitting Consensus Snapshot column
    ws_jd["H14"].value = "=INDEX(ConsensusTbl[Consensus Snapshot],1)"
    # 3. Same numeric value as vs Consensus!D2 (2026E Total Revenue) written on DIFFERENT period (2027E Operating Income) -> should NOT trigger C04 constant match!
    cons_2026_rev = ws_con["D2"].value
    ws_jd["H12"].value = cons_2026_rev
    wb["README & Changelog"].cell(3, 1, "v02")
    wb["README & Changelog"].cell(3, 4, "C04 extended test")
    wb.save(cand)
    wb.close()

    chk = mchk.run_checks(
        candidate_path=cand,
        ticker="JD",
        baseline_path=base,
        coord_map={"ops": [], "touched_cells": {"Model - JD": ["H6", "H14", "H12"]}, "cache_donor_path": str(base)},
    )
    c04_cells = {f["cell"] for f in chk["findings"] if f["check_id"] == "C04" and f["severity_class"] == "actionable"}
    assert "H6" in c04_cells, chk["findings"]  # >256-cell range caught!
    assert "H14" in c04_cells, chk["findings"]  # structured table ref caught!
    assert "H12" not in c04_cells, chk["findings"]  # different period/metric NOT misflagged by C04!


def test_c02_c05_c06_c07_c08_c09_c10_c11_c12_extended_rules(tmp_path: Path) -> None:
    """Codex #8 & #9: direct positive & negative assertions for C02, C05, C06, C07, C08, C09, C10, C11, C12."""
    ws, _, base = _copy_fixture_to_ws(tmp_path)

    # First enrich base with an outline group, segment revenue rows, and share rollforward rows
    wb_b = openpyxl.load_workbook(base, data_only=False)
    ws_b = wb_b["Model - JD"]
    ws_b.row_dimensions[4].outlineLevel = 1
    ws_b.column_dimensions["K"].outlineLevel = 1
    # Add segment revenue rows under Segment Reporting (inserting at bottom of model in a clean test copy)
    ws_b.cell(50, 4, "Beginning Shares (MM)")
    ws_b.cell(51, 4, "Net Change in Shares")
    ws_b.cell(52, 4, "Ending Shares (MM)")
    for col_i in range(11, 23):
        ws_b.cell(50, col_i, 1500.0)
        ws_b.cell(51, col_i, -10.0)
        ws_b.cell(52, col_i, 1490.0)
    wb_b.save(base)
    wb_b.close()

    # --- Part A: Inject faults for C02, C05, C06, C07, C08, C10, C11 (added col), C12 ---
    cand = ws / "model" / "China_Ecommerce_ext_v02.xlsx"
    shutil.copyfile(base, cand)
    wb = openpyxl.load_workbook(cand, data_only=False)
    ws_jd = wb["Model - JD"]
    ws_val = wb["Valuation - JD"]

    # C02: change historical 1Q26 (O1) -> forecast 1Q26E, skip 2Q27E (T1 -> 4Q27E), make annual non-consecutive (H1 -> 2029E)
    ws_jd["O1"].value = "1Q26E"
    ws_jd["T1"].value = "3Q27E"
    ws_jd["U1"].value = "4Q27E"
    ws_jd["H1"].value = "2029E"
    # C05: break style on touched driver cell P25 (strip yellow fill & blue font -> black / no fill)
    ws_jd["P25"].value = 0.11
    ws_jd["P25"].font = Font(name="Arial", size=8, color="000000")
    ws_jd["P25"].fill = PatternFill(fill_type=None)
    # C06: alter existing column width of E (9.0 -> 20.0) and clear row 4 & col K outlineLevel
    ws_jd.column_dimensions["E"].width = 20.0
    ws_jd.row_dimensions[4].outlineLevel = 0
    ws_jd.column_dimensions["K"].outlineLevel = 0
    # C07: Valuation - JD!D3 (2027E) points to Model - JD!G6 (2026E) instead of H6
    ws_val["D3"].value = "='Model - JD'!G6"
    # C08: clear core item in rollover column P (P6 = None)
    ws_jd["P6"].value = None
    # C10: annual Gross Margin (G10) & EPS (G16) wrongly sum/average quarters instead of annual ratio
    ws_jd["G10"].value = "=AVERAGE(O10:R10)"
    ws_jd["G16"].value = "=SUM(O16:R16)"
    # C12: break share rollforward at Q52 (Beg 1500 + Chg -10 != End 1400)
    ws_jd["Q52"].value = 1400.0
    wb["README & Changelog"].cell(3, 1, "v02")
    wb["README & Changelog"].cell(3, 4, "Extended rules check")
    wb.save(cand)
    wb.close()

    chk = mchk.run_checks(
        candidate_path=cand,
        ticker="JD",
        baseline_path=base,
        coord_map={
            "ops": [],
            "touched_cells": {
                "Model - JD": ["O1", "T1", "H1", "P25", "P6", "G10", "G16", "Q52"],
                "Valuation - JD": ["D3"],
            },
            "rollover_actual_cols": {"Model - JD": ["P"]},
            "added_period_cols": {"Model - JD": ["Q"]},
            "cache_donor_path": str(base),
        },
    )
    act = [f for f in chk["findings"] if f["severity_class"] == "actionable"]
    act_ids = {f["check_id"] for f in act}
    assert "C02" in act_ids and any("changed to forecast" in f["message"] for f in act if f["check_id"] == "C02"), act
    assert any("Non-consecutive annual" in f["message"] for f in act if f["check_id"] == "C02"), act
    assert any(f["check_id"] == "C05" and f["cell"] == "P25" for f in act), act
    assert any(f["check_id"] == "C06" and "Existing column width" in f["message"] for f in act), act
    assert any(f["check_id"] == "C06" and "Row outline group" in f["message"] for f in act), act
    assert any(f["check_id"] == "C06" and "Column outline group" in f["message"] for f in act), act
    assert any(f["check_id"] == "C07" and f["sheet"] == "Valuation - JD" and f["cell"] == "D3" for f in act), act
    assert any(f["check_id"] == "C08" and f["cell"] == "P6" for f in act), act
    assert any(f["check_id"] == "C10" and f["cell"] == "G10" for f in act), act
    assert any(f["check_id"] == "C10" and f["cell"] == "G16" for f in act), act
    assert any(f["check_id"] == "C12" and f["cell"] == "Q52" for f in act), act

    # Also verify C11 Group vs Segment Revenue disconnect on an added_period_cols column (col Q)
    cand_srev = ws / "model" / "China_Ecommerce_segrev_v03.xlsx"
    shutil.copyfile(base, cand_srev)
    wb_sr = openpyxl.load_workbook(cand_srev, data_only=False)
    ws_sr = wb_sr["Model - JD"]
    # Add two segment revenue rows under Segment Reporting (rows 23 is blank right below row 22)
    ws_sr.insert_rows(23, amount=2)
    ws_sr.cell(23, 4, "Retail Segment Revenue")
    ws_sr.cell(24, 4, "Logistics Segment Revenue")
    ws_sr.cell(23, 17, 1000.0)  # Q23
    ws_sr.cell(24, 17, 200.0)   # Q24 -> sum 1200 != Total Revenue (~3300)
    wb_sr["README & Changelog"].cell(3, 1, "v03")
    wb_sr["README & Changelog"].cell(3, 4, "Segment revenue disconnect check")
    wb_sr.save(cand_srev)
    wb_sr.close()
    _inject_xml_v_values(cand_srev, {"Model - JD": {"Q6": (None, "3300.0")}})
    chk_srev = mchk.run_checks(
        candidate_path=cand_srev,
        ticker="JD",
        baseline_path=base,
        coord_map={
            "ops": [{"op": "insert_rows", "sheet": "Model - JD", "at": 23, "count": 2}],
            "touched_cells": {"Model - JD": ["Q23", "Q24"]},
            "added_period_cols": {"Model - JD": ["Q"]},
            "cache_donor_path": str(base),
        },
    )
    assert any(
        f["check_id"] == "C11" and f["severity_class"] == "actionable" and "Segment Revenue" in f["message"]
        for f in chk_srev["findings"]
    ), chk_srev["findings"]
    cand_c09 = ws / "model" / "China_Ecommerce_c09_v02.xlsx"
    shutil.copyfile(base, cand_c09)
    wb9 = openpyxl.load_workbook(cand_c09, data_only=False)
    ws9 = wb9["Model - JD"]
    # Add valid restricted-cash reconciliation note in rightmost col W on row 37 (BS Cash)
    ws9["W37"].value = "<Difference equals RMB 200MM restricted cash & time deposits bridge"
    wb9["README & Changelog"].cell(3, 1, "v02")
    wb9["README & Changelog"].cell(3, 4, "Cash bridge with note")
    wb9.save(cand_c09)
    wb9.close()
    _inject_xml_v_values(cand_c09, {"Model - JD": {"Q34": (None, "2500.0"), "Q37": (None, "2300.0")}})
    chk_c09_ok = mchk.run_checks(
        candidate_path=cand_c09,
        ticker="JD",
        baseline_path=base,
        coord_map={"ops": [], "touched_cells": {"Model - JD": ["Q37", "W37"]}, "cache_donor_path": str(base)},
    )
    assert not any(
        f["check_id"] == "C09" and f["severity_class"] == "actionable" and "Cash bridge" in f["message"]
        for f in chk_c09_ok["findings"]
    ), chk_c09_ok["findings"]

    # Remove reconciliation note -> C09 cash bridge MUST flag actionable!
    wb9b = openpyxl.load_workbook(cand_c09, data_only=False)
    wb9b["Model - JD"]["W37"].value = None
    wb9b.save(cand_c09)
    wb9b.close()
    _inject_xml_v_values(cand_c09, {"Model - JD": {"Q34": (None, "2500.0"), "Q37": (None, "2300.0")}})
    chk_c09_bad = mchk.run_checks(
        candidate_path=cand_c09,
        ticker="JD",
        baseline_path=base,
        coord_map={"ops": [], "touched_cells": {"Model - JD": ["Q37"]}, "cache_donor_path": str(base)},
    )
    assert any(
        f["check_id"] == "C09" and f["severity_class"] == "actionable" and "Cash bridge" in f["message"]
        for f in chk_c09_bad["findings"]
    ), chk_c09_bad["findings"]


# ---------------------------------------------------------------------------
# 7. write-ledger & verify-done (reports never generated, fix_rounds 0..2, report_revision)
# ---------------------------------------------------------------------------


def test_write_ledger_when_reports_never_generated_and_verify_done_rules(tmp_path: Path) -> None:
    ws, rdir, base = _copy_fixture_to_ws(tmp_path)

    # Simulate timeout before model-check.md or consensus-diff.* were ever written!
    for fr in (0, 1, 2):
        sub_rdir = ws / "research" / f"2026-11-27-model-JD-r{fr}"
        wl = mchk.write_fallback_ledger(
            workspace_dir=ws,
            ticker="JD",
            research_dir=sub_rdir,
            candidate_path=base,
            kind="modeling",
            failed_stages=["check", "consensus"],
            run_id=f"wf-run-{fr}",
            workflow="company-model",
            fix_rounds=fr,
            reason="check subagent timed out",
        )
        assert wl["final_status"] == "delivered_with_open_issues"
        assert not (sub_rdir / "model_dump" / "model-check.md").exists()
        assert (sub_rdir / "model_dump" / "model-verification.json").is_file()
        assert (sub_rdir / "model_dump" / "remaining-issues.md").is_file()

        vd = mchk.verify_done(
            workspace_dir=ws,
            ticker="JD",
            research_dir=sub_rdir,
            expected_run_id=f"wf-run-{fr}",
        )
        assert vd["ok"] is True, vd
        assert vd["final_status"] == "delivered_with_open_issues"
        assert vd["fix_rounds"] == fr
        assert vd["failed_stages"] == ["check", "consensus"]

    # report_revision appends a round WITHOUT consuming fix_rounds (stays at 2)
    rev_rdir = ws / "research" / "2026-11-27-model-JD-r2"
    wl_rev = mchk.write_fallback_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rev_rdir,
        candidate_path=base,
        kind="report_revision",
        failed_stages=["check", "consensus"],
        run_id="wf-run-2",
        workflow="company-model",
    )
    assert wl_rev["ledger"]["fix_rounds"] == 2
    assert len(wl_rev["ledger"]["rounds"]) == 2
    assert wl_rev["ledger"]["rounds"][-1]["kind"] == "report_revision"
    assert mchk.verify_done(
        workspace_dir=ws, ticker="JD", research_dir=rev_rdir, expected_run_id="wf-run-2"
    )["ok"] is True

    # Deep-update missing model-delta.md allowed when "model_delta" recorded in failed_stages
    deep_rdir = ws / "research" / "2026-11-27-deep-3Q26"
    deep_rdir.mkdir(parents=True, exist_ok=True)
    (deep_rdir / "mapping.md").write_text("# Mapping\n" + "x" * 60, encoding="utf-8")
    mchk.write_fallback_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=deep_rdir,
        candidate_path=base,
        kind="modeling",
        failed_stages=["check", "consensus", "model_delta"],
        run_id="deep-run-1",
        workflow="earnings-deep-update",
    )
    vd_deep = mchk.verify_done(
        workspace_dir=ws, ticker="JD", research_dir=deep_rdir, expected_run_id="deep-run-1"
    )
    assert vd_deep["ok"] is True, vd_deep

    # run_id mismatch must fail verify-done
    vd_mismatch = mchk.verify_done(
        workspace_dir=ws, ticker="JD", research_dir=deep_rdir, expected_run_id="wrong-run-id"
    )
    assert vd_mismatch["ok"] is False


def test_verify_done_rejects_stale_check_report_and_handles_missing_consensus(tmp_path: Path) -> None:
    """Principle #4, Codex #10 & Grok #3:
    - Missing consensus-diff.* -> recorded in failed_stages & delivered_with_open_issues -> verify-done passes.
    - Stale model-check.md from an earlier candidate hash CANNOT spoof a newer candidate in write-ledger or verify-done."""
    ws, rdir, base = _copy_fixture_to_ws(tmp_path)

    # 1. Check ONLY (no consensus-diff.* generated) -> consensus goes into failed_stages & delivered_with_open_issues
    chk_only = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir,
        candidate_path=base,
        baseline_path=base,
        run_id="run-no-cons",
    )
    assert chk_only["final_status"] == "delivered_with_open_issues"
    assert "consensus" in chk_only["failed_stages"]
    vd_no_cons = mchk.verify_done(workspace_dir=ws, ticker="JD", research_dir=rdir, expected_run_id="run-no-cons")
    assert vd_no_cons["ok"] is True, vd_no_cons
    assert vd_no_cons["final_status"] == "delivered_with_open_issues"
    assert "consensus" in vd_no_cons["failed_stages"]

    # 2. Repair produces a NEW candidate v02 (different SHA-256) while stale v01 model-check.md remains in model_dump/
    cand_v2 = ws / "model" / "China_Ecommerce_v02.xlsx"
    shutil.copyfile(base, cand_v2)
    wb = openpyxl.load_workbook(cand_v2, data_only=False)
    wb["README & Changelog"].cell(3, 1, "v02")
    wb["README & Changelog"].cell(3, 4, "New candidate after repair")
    wb.save(cand_v2)
    wb.close()

    # write_fallback_ledger MUST detect stale model-check.md (still holds v01 hash!) and mark "check" failed!
    wl = mchk.write_fallback_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir,
        candidate_path=cand_v2,
        kind="modeling",
        failed_stages=["recalc"],
        run_id="run-stale-test",
        fix_rounds=1,
    )
    assert "check" in wl["failed_stages"], wl
    assert "check" not in wl["ledger"]["rounds"][-1]["evidence"]

    # Even if someone tampers model-verification.json to claim "check" succeeded for v02, verify_done MUST reject it!
    ledger_p = rdir / "model_dump" / "model-verification.json"
    tampered = json.loads(ledger_p.read_text(encoding="utf-8"))
    tampered["failed_stages"] = ["consensus"]
    tampered["rounds"][-1]["failed_stages"] = ["consensus"]
    tampered["rounds"][-1]["evidence"]["check"] = {
        "path": "research/2026-11-27-model-JD/model_dump/model-check.md",
        "evidence_model_sha256": tampered["final_model_sha256"],
        "counts": {"actionable": 0, "baseline_legacy": 0, "missing_evidence": 0, "not_applicable": 0},
    }
    ledger_p.write_text(json.dumps(tampered, indent=2), encoding="utf-8")

    vd_stale = mchk.verify_done(workspace_dir=ws, ticker="JD", research_dir=rdir, expected_run_id="run-stale-test")
    assert vd_stale["ok"] is False
    assert any("stale model-check.md" in e for e in vd_stale["errors"]), vd_stale["errors"]

    # 3. Full 2-round `check` path where fix_round=2 still leaves actionable > 0 -> delivered_with_open_issues!
    rdir_r2 = ws / "research" / "2026-11-27-model-JD-max2"
    mcons.compute_consensus_diff(
        model_path=cand_v2,
        ticker="JD",
        research_dir=rdir_r2,
        consensus_data={
            "source_type": "Market Consensus",
            "as_of_date": "2026-11-27",
            "items": [{"period": "2026E", "metric": "Total Revenue", "consensus": 12000.0, "rationale": "Inline"}],
        },
    )
    _set_full_calc_on_load(cand_v2, "1")  # inject an actionable C13 issue that remains after round 2
    mcons.compute_consensus_diff(
        model_path=cand_v2,
        ticker="JD",
        research_dir=rdir_r2,
        consensus_data={
            "source_type": "Market Consensus",
            "as_of_date": "2026-11-27",
            "items": [{"period": "2026E", "metric": "Total Revenue", "consensus": 12000.0, "rationale": "Inline"}],
        },
    )
    res_r2 = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir_r2,
        candidate_path=cand_v2,
        baseline_path=base,
        run_id="run-max2-open",
        fix_round=2,
    )
    assert res_r2["counts"]["actionable"] >= 1
    assert res_r2["final_status"] == "delivered_with_open_issues"
    vd_r2 = mchk.verify_done(workspace_dir=ws, ticker="JD", research_dir=rdir_r2, expected_run_id="run-max2-open")
    assert vd_r2["ok"] is True, vd_r2
    assert vd_r2["final_status"] == "delivered_with_open_issues"
    assert vd_r2["fix_rounds"] == 2


# ---------------------------------------------------------------------------
# 8. Small inline attachment baseline recovery (SQLite test DB + bash script + rclone fallback)
# ---------------------------------------------------------------------------


def test_recover_baseline_inline_small_attachment_and_rclone_fallback(tmp_path: Path) -> None:
    baseline_bytes = FIXTURE_XLSX.read_bytes()
    baseline_sha = hashlib.sha256(baseline_bytes).hexdigest()
    wrong_bytes = b"not-the-real-baseline-workbook" + b"0" * (len(baseline_bytes) - 30)
    assert len(wrong_bytes) == len(baseline_bytes)

    ws_dir = tmp_path / "china_ecom_ws"
    lineage_path = ws_dir / "research" / "2026-10-01-model-JD" / "model_lineage.json"
    lineage_path.parent.mkdir(parents=True, exist_ok=True)
    lineage_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "input_file": "China_Ecommerce_Model.xlsx",
                        "input_sha256": baseline_sha,
                        "output_file": "model/China_Ecommerce_v02.xlsx",
                        "recorded_at": "2026-10-01T10:00:00+00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    # Build temporary SQLite test DB matching §8.5 schema
    db_path = tmp_path / "test_prod.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE workspaces (workspace_id TEXT PRIMARY KEY, user_id TEXT);
        CREATE TABLE conversation_threads (conversation_thread_id TEXT PRIMARY KEY, workspace_id TEXT, created_by TEXT);
        CREATE TABLE conversation_queries (conversation_thread_id TEXT, turn_index INT, created_at TEXT, metadata TEXT);
        CREATE TABLE conversation_responses (conversation_thread_id TEXT, turn_index INT, attempt_no INT, user_id TEXT);
        CREATE TABLE chat_attachments (
            attachment_id TEXT PRIMARY KEY, user_id TEXT, name TEXT, size INT,
            object_key TEXT, content BLOB, state TEXT, created_at TEXT, sent_at TEXT, cleaned_at TEXT
        );
        CREATE TABLE dalton_chat_file_uploads (
            upload_id TEXT PRIMARY KEY, workspace_id TEXT, filename TEXT, size_bytes INT,
            object_key TEXT, actual_sha256 TEXT, state TEXT, created_at TEXT, used_at TEXT, cleaned_at TEXT
        );
        """
    )
    ws_id = "11111111-2222-3333-4444-555555555555"
    conn.execute("INSERT INTO workspaces VALUES (?, ?)", (ws_id, "owner-u1"))
    conn.execute("INSERT INTO conversation_threads VALUES (?, ?, ?)", ("thr-1", ws_id, "creator-u2"))
    # Turn query with inline attachment metadata ONLY (no url, no attachment_id!)
    att_meta = json.dumps(
        {
            "attachments": [
                {
                    "name": "China_Ecommerce_Model.xlsx",
                    "type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "size": len(baseline_bytes),
                }
            ]
        }
    )
    conn.execute(
        "INSERT INTO conversation_queries VALUES (?, ?, ?, ?)",
        ("thr-1", 1, "2026-09-28T08:00:00+00:00", att_meta),
    )
    # Actual turn sender is sender-u3 (distinct from workspace owner & thread creator)
    conn.execute(
        "INSERT INTO conversation_responses VALUES (?, ?, ?, ?)",
        ("thr-1", 1, 1, "sender-u3"),
    )
    # Candidate 1: same sender, name, size, closer time, WRONG content hash -> must be skipped
    conn.execute(
        "INSERT INTO chat_attachments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "att-wrong-sha", "sender-u3", "China_Ecommerce_Model.xlsx", len(baseline_bytes),
            None, wrong_bytes, "sent", "2026-09-28T07:59:50+00:00", "2026-09-28T08:00:01+00:00", None,
        ),
    )
    # Candidate 2: wrong sender -> excluded by SQL filter
    conn.execute(
        "INSERT INTO chat_attachments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "att-other-user", "someone-else", "China_Ecommerce_Model.xlsx", len(baseline_bytes),
            None, baseline_bytes, "sent", "2026-09-28T07:59:55+00:00", "2026-09-28T08:00:00+00:00", None,
        ),
    )
    # Candidate 2b: right sender & right bytes, but OUTSIDE ±10 min time window -> MUST be excluded!
    conn.execute(
        "INSERT INTO chat_attachments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "att-outside-window", "sender-u3", "China_Ecommerce_Model.xlsx", len(baseline_bytes),
            None, baseline_bytes, "sent", "2026-09-28T07:00:00+00:00", "2026-09-28T07:15:00+00:00", None,
        ),
    )
    # Candidate 3: right sender, name, size, within time window, MATCHES records[0].input_sha256!
    conn.execute(
        "INSERT INTO chat_attachments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "att-real", "sender-u3", "China_Ecommerce_Model.xlsx", len(baseline_bytes),
            None, baseline_bytes, "sent", "2026-09-28T07:58:00+00:00", "2026-09-28T07:59:00+00:00", None,
        ),
    )
    conn.commit()
    conn.close()

    # Run the actual bash script end-to-end against our temporary SQLite DB!
    out_xlsx = tmp_path / "recovered_inline.xlsx"
    manifest_out = tmp_path / "recover_manifest.json"
    proc = subprocess.run(
        [
            "bash",
            str(PROD_OPS_DIR / "recover_baseline_model.sh"),
            "--workspace-id", ws_id,
            "--workspace-dir", str(ws_dir),
            "--sqlite-db", str(db_path),
            "--output", str(out_xlsx),
            "--manifest-out", str(manifest_out),
            "--rclone-remote", "",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["found"] is True
    assert payload["channel"] == "inline_chat_attachment_content"
    assert payload["sha256"] == baseline_sha
    assert payload["checked_candidates"] >= 2
    assert out_xlsx.read_bytes() == baseline_bytes

    # Also test encrypted rclone backup fallback using fake rclone binary + real openssl AES-256-CBC
    key_file = tmp_path / "backup.key"
    key_file.write_text("unit-test-secret-key-2026\n", encoding="utf-8")
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo(name="work/uploads/China_Ecommerce_Model.xlsx")
        ti.size = len(baseline_bytes)
        tf.addfile(ti, io.BytesIO(baseline_bytes))
    enc_file = tmp_path / "minio-2026-09-25.tar.gz.enc"
    enc_proc = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", f"file:{key_file}", "-out", str(enc_file)],
        input=tar_buf.getvalue(),
        check=True,
    )
    assert enc_proc.returncode == 0

    fake_rclone = tmp_path / "fake_rclone.sh"
    fake_rclone.write_text(
        f"""#!/usr/bin/env bash
if [[ "$1" == "lsf" && "$2" == "--dirs-only" ]]; then
  echo "2026-09-25/"
elif [[ "$1" == "lsf" && "$2" == "--files-only" ]]; then
  echo "minio-2026-09-25.tar.gz.enc"
elif [[ "$1" == "cat" ]]; then
  cat "{enc_file}"
fi
""",
        encoding="utf-8",
    )
    fake_rclone.chmod(0o755)
    out_rclone = tmp_path / "recovered_rclone.xlsx"
    res_rc = recover_lib.recover_baseline(
        workspace_id=ws_id,
        output_path=out_rclone,
        before_first_edit_ts="2026-10-01T10:00:00+00:00",
        expected_sha256=baseline_sha,
        expected_filename="China_Ecommerce_Model.xlsx",
        db=None,
        rclone_remote="fakegdrive",
        rclone_drive_dir="DaltonBackup",
        rclone_bin=str(fake_rclone),
        backup_key_file=key_file,
    )
    assert res_rc.found is True
    assert res_rc.channel == "rclone_encrypted_backup"
    assert out_rclone.read_bytes() == baseline_bytes

    # Also test daily PostgreSQL offsite backup fallback under db/<date>/*.sql.gz.enc (Codex #6)!
    pg_sql = (
        "COPY public.chat_attachments (attachment_id, user_id, name, size, content) FROM stdin;\n"
        f"att-db-backup\tsender-u3\tChina_Ecommerce_Model.xlsx\t{len(baseline_bytes)}\t\\\\x{baseline_bytes.hex()}\n"
        "\\.\n"
    ).encode("utf-8")
    sql_gz_enc = tmp_path / "dalton-2026-09-29.sql.gz.enc"
    subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", f"file:{key_file}", "-out", str(sql_gz_enc)],
        input=gzip.compress(pg_sql),
        check=True,
    )
    fake_rclone_db = tmp_path / "fake_rclone_db.sh"
    fake_rclone_db.write_text(
        f"""#!/usr/bin/env bash
if [[ "$1" == "lsf" && "$2" == "--dirs-only" ]]; then
  if [[ "$3" == *"/db/" ]]; then
    echo "2026-09-29/"
  fi
elif [[ "$1" == "lsf" && "$2" == "--files-only" ]]; then
  if [[ "$3" == *"/db/2026-09-29/" ]]; then
    echo "dalton-2026-09-29.sql.gz.enc"
  fi
elif [[ "$1" == "cat" ]]; then
  cat "{sql_gz_enc}"
fi
""",
        encoding="utf-8",
    )
    fake_rclone_db.chmod(0o755)
    out_db_bk = tmp_path / "recovered_db_backup.xlsx"
    res_db_bk = recover_lib.recover_baseline(
        workspace_id=ws_id,
        output_path=out_db_bk,
        before_first_edit_ts="2026-10-01T10:00:00+00:00",
        expected_sha256=baseline_sha,
        expected_filename="China_Ecommerce_Model.xlsx",
        db=None,
        rclone_remote="fakegdrive",
        rclone_drive_dir="DaltonBackup",
        rclone_bin=str(fake_rclone_db),
        backup_key_file=key_file,
    )
    assert res_db_bk.found is True
    assert res_db_bk.channel == "rclone_encrypted_db_backup"
    assert out_db_bk.read_bytes() == baseline_bytes

    # Also test Channel 1: large-file upload channel (`dalton_chat_file_uploads.actual_sha256` + object_store_dir)
    db_large_path = tmp_path / "test_large.sqlite3"
    conn_l = sqlite3.connect(db_large_path)
    conn_l.execute(
        """
        CREATE TABLE dalton_chat_file_uploads (
            upload_id TEXT PRIMARY KEY, workspace_id TEXT, filename TEXT, size_bytes INT,
            object_key TEXT, actual_sha256 TEXT, state TEXT, created_at TEXT, used_at TEXT, cleaned_at TEXT
        );
        """
    )
    conn_l.execute(
        "INSERT INTO dalton_chat_file_uploads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "up-1", ws_id, "China_Ecommerce_Model.xlsx", len(baseline_bytes),
            "uploads/up-1/China_Ecommerce_Model.xlsx", baseline_sha, "ready",
            "2026-09-25T08:00:00+00:00", None, None,
        ),
    )
    conn_l.commit()
    conn_l.close()
    obj_store = tmp_path / "minio_mirror"
    obj_file = obj_store / "uploads" / "up-1" / "China_Ecommerce_Model.xlsx"
    obj_file.parent.mkdir(parents=True, exist_ok=True)
    obj_file.write_bytes(baseline_bytes)
    out_large = tmp_path / "recovered_large.xlsx"
    db_large = recover_lib.SQLiteReadOnlyAdapter(db_large_path)
    try:
        res_large = recover_lib.recover_baseline(
            workspace_id=ws_id,
            output_path=out_large,
            before_first_edit_ts="2026-10-01T10:00:00+00:00",
            expected_sha256=baseline_sha,
            db=db_large,
            object_reader=recover_lib.build_object_reader(object_store_dir=obj_store),
        )
    finally:
        db_large.close()
    assert res_large.found is True
    assert res_large.channel == "large_file_upload"
    assert out_large.read_bytes() == baseline_bytes


def test_recover_baseline_postgres_sql_casts_and_datetime_binding() -> None:
    """Principle #5 & Codex #6: verify explicit ::timestamptz / ::text[] casts in PG SQL and
    that PostgresReadOnlyAdapter binds tz-aware datetime objects (never untyped strings)."""
    import psycopg

    # 1. Verify explicit casts in all three PG SQL constants
    assert "%(before_first_edit_ts)s::timestamptz" in recover_lib.SQL_PG_LARGE_UPLOADS
    assert "%(before_first_edit_ts)s::timestamptz" in recover_lib.SQL_PG_TURN_ATTACHMENTS
    assert "%(before_first_edit_ts)s::timestamptz" in recover_lib.SQL_PG_INLINE_CANDIDATES
    assert "%(turn_created_at)s::timestamptz + INTERVAL '5 minutes'" in recover_lib.SQL_PG_INLINE_CANDIDATES
    assert "%(turn_created_at)s::timestamptz - INTERVAL '10 minutes'" in recover_lib.SQL_PG_INLINE_CANDIDATES
    assert "%(sender_user_ids)s::text[]" in recover_lib.SQL_PG_INLINE_CANDIDATES

    # 2. Verify PostgresReadOnlyAdapter passes tz-aware datetime params using create_autospec
    executed: list[tuple[str, Any]] = []
    cur_mock = create_autospec(psycopg.Cursor, instance=True)
    cur_mock.__enter__.return_value = cur_mock
    cur_mock.fetchall.return_value = []
    cur_mock.execute.side_effect = lambda q, p=None: executed.append((str(q), p))

    conn_mock = create_autospec(psycopg.Connection, instance=True)
    conn_mock.cursor.return_value = cur_mock

    adapter = object.__new__(recover_lib.PostgresReadOnlyAdapter)
    adapter._conn = conn_mock
    adapter.fetch_inline_candidates(
        sender_user_ids=["sender-u3"],
        att_name="China_Ecommerce_Model.xlsx",
        att_size=12345,
        before_ts="2026-10-01T10:00:00Z",
        turn_created_at="2026-09-28T08:00:00+00:00",
    )
    assert len(executed) == 1
    _, params = executed[0]
    assert isinstance(params["before_first_edit_ts"], datetime)
    assert params["before_first_edit_ts"].tzinfo is not None
    assert isinstance(params["turn_created_at"], datetime)
    assert params["turn_created_at"].tzinfo is not None


def _extract_pg_dbname(dsn: str) -> str:
    """Extract effective database name using ``psycopg.conninfo.conninfo_to_dict`` (URI + query params & keyword DSN)."""
    raw = str(dsn or "").strip()
    if not raw:
        return ""
    try:
        from psycopg.conninfo import conninfo_to_dict

        info = conninfo_to_dict(raw)
        return str(info.get("dbname") or "").strip()
    except Exception:
        return ""


def _resolve_safe_test_pg_dsn() -> str | None:
    """Return explicit ``DALTON_TEST_PG_DSN`` ONLY if psycopg-resolved ``dbname`` ends with ``_test``."""
    dsn = os.environ.get("DALTON_TEST_PG_DSN", "").strip()
    if not dsn:
        return None
    dbname = _extract_pg_dbname(dsn)
    if not dbname or not dbname.endswith("_test"):
        return None
    return dsn


def _build_test_pg_dsn_with_search_path(dsn: str, schema: str) -> str:
    """Construct a valid psycopg conninfo string with ``search_path`` for both URI and keyword DSNs."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    info = conninfo_to_dict(dsn)
    existing_opts = str(info.get("options") or "").strip()
    merged_opts = f"{existing_opts} -csearch_path={schema}".strip()
    return make_conninfo(dsn, options=merged_opts)


def _assert_live_pg_is_test_db(conn: Any) -> str:
    """Double-check connected DB via ``SELECT current_database()`` BEFORE any DDL/write; abort unless ``*_test``."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        row = cur.fetchone()
    actual_db = str(row[0] if row else "").strip()
    if not actual_db.endswith("_test"):
        raise RuntimeError(
            f"Refusing to run PostgreSQL test DDL/writes against non-*_test database: {actual_db!r}"
        )
    return actual_db


@pytest.mark.skipif(
    _resolve_safe_test_pg_dsn() is None,
    reason="Requires explicit DALTON_TEST_PG_DSN whose psycopg-resolved dbname ends with '_test' (never falls back to DATABASE_URL)",
)
def test_recover_baseline_real_postgres_integration(tmp_path: Path) -> None:
    """Principle #5, R3 #1 & R4 #1: execute real PostgreSQL SQL queries ONLY against verified *_test DB."""
    import psycopg

    dsn = _resolve_safe_test_pg_dsn()
    assert dsn is not None
    schema = f"dalton_p0a_pgtest_{uuid.uuid4().hex[:10]}"
    baseline_bytes = FIXTURE_XLSX.read_bytes()
    baseline_sha = hashlib.sha256(baseline_bytes).hexdigest()
    ws_id = str(uuid.uuid4())
    att_ok_id = str(uuid.uuid4())
    att_out_id = str(uuid.uuid4())

    setup_conn = psycopg.connect(dsn, autocommit=True)
    schema_created = False
    try:
        # R4 #1: MUST verify live current_database() ends with _test BEFORE any DDL or writes!
        _assert_live_pg_is_test_db(setup_conn)
        with setup_conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema};")
            schema_created = True
            cur.execute(f"SET search_path TO {schema};")
            cur.execute(
                """
                CREATE TABLE workspaces (workspace_id uuid PRIMARY KEY, user_id text);
                CREATE TABLE conversation_threads (conversation_thread_id text PRIMARY KEY, workspace_id uuid, created_by text);
                CREATE TABLE conversation_queries (conversation_thread_id text, turn_index int, created_at timestamptz, metadata jsonb);
                CREATE TABLE conversation_responses (conversation_thread_id text, turn_index int, attempt_no int, user_id text);
                CREATE TABLE chat_attachments (
                    attachment_id uuid PRIMARY KEY, user_id text, name text, size bigint,
                    object_key text, content bytea, state text, created_at timestamptz, sent_at timestamptz, cleaned_at timestamptz
                );
                CREATE TABLE dalton_chat_file_uploads (
                    upload_id text PRIMARY KEY, workspace_id text, filename text, size_bytes bigint,
                    object_key text, actual_sha256 text, state text, created_at timestamptz, used_at timestamptz, cleaned_at timestamptz
                );
                """
            )
            cur.execute("INSERT INTO workspaces VALUES (%s::uuid, 'owner-u1')", (ws_id,))
            cur.execute("INSERT INTO conversation_threads VALUES ('thr-pg-1', %s::uuid, 'creator-u2')", (ws_id,))
            cur.execute(
                "INSERT INTO conversation_queries VALUES ('thr-pg-1', 1, '2026-09-28T08:00:00+00:00'::timestamptz, %s::jsonb)",
                (json.dumps({"attachments": [{"name": "China_Ecommerce_Model.xlsx", "size": len(baseline_bytes)}]}),),
            )
            cur.execute("INSERT INTO conversation_responses VALUES ('thr-pg-1', 1, 1, 'sender-u3')")
            cur.execute(
                """
                INSERT INTO chat_attachments VALUES
                  (%s::uuid, 'sender-u3', 'China_Ecommerce_Model.xlsx', %s, NULL, %s, 'sent',
                   '2026-09-28T07:58:00+00:00'::timestamptz, '2026-09-28T07:59:00+00:00'::timestamptz, NULL),
                  (%s::uuid, 'sender-u3', 'China_Ecommerce_Model.xlsx', %s, NULL, %s, 'sent',
                   '2026-09-28T06:00:00+00:00'::timestamptz, '2026-09-28T06:05:00+00:00'::timestamptz, NULL)
                """,
                (att_ok_id, len(baseline_bytes), baseline_bytes, att_out_id, len(baseline_bytes), baseline_bytes),
            )

        pg_dsn = _build_test_pg_dsn_with_search_path(dsn, schema)
        adapter = recover_lib.PostgresReadOnlyAdapter(pg_dsn)
        try:
            out_p = tmp_path / "pg_recovered.xlsx"
            res = recover_lib.recover_baseline(
                workspace_id=ws_id,
                output_path=out_p,
                before_first_edit_ts="2026-10-01T10:00:00+00:00",
                expected_sha256=baseline_sha,
                expected_filename="China_Ecommerce_Model.xlsx",
                db=adapter,
            )
            assert res.found is True
            assert res.channel == "inline_chat_attachment_content"
            assert res.sha256 == baseline_sha
            assert out_p.read_bytes() == baseline_bytes
        finally:
            adapter.close()
    finally:
        if schema_created:
            with setup_conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE;")
        setup_conn.close()


# ---------------------------------------------------------------------------
# 9. Consensus isolation: snapshot writes ONLY to consensus zone + diff output
# ---------------------------------------------------------------------------


def test_consensus_snapshot_isolation_and_diff_thresholds(tmp_path: Path) -> None:
    ws, rdir, base = _copy_fixture_to_ws(tmp_path)
    cand = ws / "model" / "China_Ecommerce_v02.xlsx"
    shutil.copyfile(base, cand)

    before_fp = mfp.build_fingerprint(cand, ticker="JD")
    before_jd_cells = before_fp["sheets"]["Model - JD"]["cells"]
    before_baba_cache = _xml_cached_map(cand, "Model - BABA")

    consensus_payload = {
        "source_type": "Market Consensus",
        "as_of_date": "2026-11-27",
        "basis": {
            "fiscal_year_end_month": 12,
            "currency": "RMB",
            "accounting_basis": "GAAP",
            "share_basis": "ordinary",
            "reflects_latest_earnings": True,
        },
        "items": [
            {
                "period": "2026E",
                "metric": "Total Revenue",
                "consensus": 10000.0,  # ~12% below House -> exceeds ±5%
                "rationale": "Stronger retail GMV momentum",
            },
            {
                "period": "2026E",
                "metric": "Operating Income",
                "consensus": 500.0,
                "house_margin": 0.020,  # <3% near-breakeven margin guard (±150bp)
                "consensus_margin": 0.025,  # diff = -50bp -> within ±150bp!
            },
            {
                "period": "2027E",
                "metric": "Net Income",
                "consensus": 400.0,  # far below House -> exceeds ±10%, no rationale -> missing_rationale
            },
        ],
    }

    # Refuses writing consensus snapshot into House Model sheet!
    with pytest.raises(ValueError, match="isolated consensus sheet"):
        mcons.write_consensus_snapshot(
            model_path=cand,
            consensus_data=consensus_payload,
            sheet_name="Model - JD",
        )

    # Writes ONLY to isolated vs Consensus sheet
    snap_res = mcons.write_consensus_snapshot(
        model_path=cand,
        consensus_data=consensus_payload,
        sheet_name="vs Consensus",
    )
    assert snap_res["status"] == "ok"
    assert snap_res["sheet"] == "vs Consensus"
    assert snap_res["layout_mode"] == "existing_table"
    assert snap_res["written_count"] == 3
    assert set(snap_res["consensus_snapshot_cells"]["vs Consensus"]) == {"D2", "D3", "D6"}
    assert "D2" in snap_res["touched_cells"]["vs Consensus"]
    assert "E2" in snap_res["refreshed_diff_cells"]

    # Verify target snapshot cell values AND refreshed diff formula <v> cache (no stale diff!)
    wb_chk = openpyxl.load_workbook(cand, data_only=False)
    try:
        assert wb_chk["vs Consensus"]["D2"].value == 10000.0
        assert wb_chk["vs Consensus"]["D3"].value == 500.0
        assert wb_chk["vs Consensus"]["D6"].value == 400.0
    finally:
        wb_chk.close()
    con_xml_after = _xml_cached_map(cand, "vs Consensus")
    house_2026_rev = float(before_jd_cells["G6"]["cached_value"])
    exp_diff_e2 = (house_2026_rev - 10000.0) / 10000.0
    assert abs(float(con_xml_after["E2"][1]) - exp_diff_e2) < 1e-6, con_xml_after["E2"]

    # House Model - JD forecast cells & non-target BABA caches are completely untouched!
    after_fp = mfp.build_fingerprint(cand, ticker="JD")
    after_jd_cells = after_fp["sheets"]["Model - JD"]["cells"]
    assert after_jd_cells == before_jd_cells
    assert _xml_cached_map(cand, "Model - BABA") == before_baba_cache

    # Test non-standard layout (reordered cols: Metric, Period, Consensus, House, Diff) AND
    # unrecognizable layout (House formula in col D!) -> must NOT overwrite House formulas!
    cand_reord = ws / "model" / "China_Ecommerce_reord_v03.xlsx"
    shutil.copyfile(base, cand_reord)
    wb_r = openpyxl.load_workbook(cand_reord, data_only=False)
    ws_cr = wb_r["vs Consensus"]
    # Clear and write reordered table: A=Metric, B=Period, C=Consensus Snapshot, D=House Formula, E=Diff %
    ws_cr.delete_rows(1, ws_cr.max_row)
    for idx, h in enumerate(["Metric", "Period", "Consensus Snapshot", "House", "Diff %"], start=1):
        ws_cr.cell(1, idx, h)
    ws_cr.cell(2, 1, "Total Revenue")
    ws_cr.cell(2, 2, "2026E")
    ws_cr.cell(2, 3, 9500.0)
    ws_cr.cell(2, 4, "='Model - JD'!G6")
    ws_cr.cell(2, 5, "=(D2-C2)/ABS(C2)")
    wb_r.save(cand_reord)
    wb_r.close()
    _inject_xml_v_values(cand_reord, {"vs Consensus": {"D2": (None, str(house_2026_rev)), "E2": (None, "0.99")}})
    res_reord = mcons.write_consensus_snapshot(
        model_path=cand_reord,
        consensus_data={"items": [{"period": "2026E", "metric": "Total Revenue", "consensus": 11000.0}]},
        sheet_name="vs Consensus",
    )
    assert res_reord["layout_mode"] == "existing_table"
    assert res_reord["consensus_snapshot_cells"]["vs Consensus"] == ["C2"]
    wb_r2 = openpyxl.load_workbook(cand_reord, data_only=False)
    try:
        assert wb_r2["vs Consensus"]["C2"].value == 11000.0
        assert wb_r2["vs Consensus"]["D2"].value == "='Model - JD'!G6"  # House formula untouched!
    finally:
        wb_r2.close()
    reord_xml = _xml_cached_map(cand_reord, "vs Consensus")
    assert abs(float(reord_xml["E2"][1]) - ((house_2026_rev - 11000.0) / 11000.0)) < 1e-6

    # Unrecognizable layout where col D holds a House formula -> must append isolated block below!
    cand_unrec = ws / "model" / "China_Ecommerce_unrec_v04.xlsx"
    shutil.copyfile(base, cand_unrec)
    wb_u = openpyxl.load_workbook(cand_unrec, data_only=False)
    ws_cu = wb_u["vs Consensus"]
    ws_cu.delete_rows(1, ws_cu.max_row)
    ws_cu.cell(1, 1, "Custom Col A")
    ws_cu.cell(1, 4, "Custom House Link")
    ws_cu.cell(2, 1, "2026E")
    ws_cu.cell(2, 4, "='Model - JD'!G6")
    wb_u.save(cand_unrec)
    wb_u.close()
    res_unrec = mcons.write_consensus_snapshot(
        model_path=cand_unrec,
        consensus_data={"items": [{"period": "2026E", "metric": "Total Revenue", "consensus": 11500.0, "house_formula": "='Model - JD'!G6"}]},
        sheet_name="vs Consensus",
    )
    assert res_unrec["layout_mode"] == "isolated_block"
    wb_u2 = openpyxl.load_workbook(cand_unrec, data_only=False)
    try:
        assert wb_u2["vs Consensus"]["D2"].value == "='Model - JD'!G6"  # Original House formula untouched!
        snap_coord = res_unrec["consensus_snapshot_cells"]["vs Consensus"][0]
        assert int(snap_coord[1:]) >= 6
        assert wb_u2["vs Consensus"][snap_coord].value == 11500.0
    finally:
        wb_u2.close()

    # Generate consensus-diff.{json,md} in <R>/model_dump/
    diff_res = mcons.compute_consensus_diff(
        model_path=cand,
        ticker="JD",
        research_dir=rdir,
        consensus_data=consensus_payload,
    )
    assert diff_res["status"] == "ok"
    assert diff_res["model_sha256"] == snap_res["model_sha256"]
    assert diff_res["basis_alignment"]["aligned"] is True
    assert (rdir / "model_dump" / "consensus-diff.json").is_file()
    assert (rdir / "model_dump" / "consensus-diff.md").is_file()

    by_metric = {(i["period"], i["metric_key"]): i for i in diff_res["items"]}
    # Total Revenue exceeds ±5% with rationale -> missing_rationale False
    assert by_metric[("2026E", "total_revenue")]["exceeds_threshold"] is True
    assert by_metric[("2026E", "total_revenue")]["missing_rationale"] is False
    # Operating Income near breakeven (<3% margin): -50bp within ±150bp -> exceeds False!
    assert by_metric[("2026E", "operating_income")]["comparison_mode"] == "bp"
    assert by_metric[("2026E", "operating_income")]["exceeds_threshold"] is False
    # 2027E Net Income exceeds ±10% with NO rationale -> missing_rationale True!
    assert by_metric[("2027E", "net_income")]["exceeds_threshold"] is True
    assert by_metric[("2027E", "net_income")]["missing_rationale"] is True
    assert diff_res["missing_rationale_count"] == 1


# ---------------------------------------------------------------------------
# 10. Round-3 targeted tests (Codex & Grok R2 review items + Prompt #1-#6)
# ---------------------------------------------------------------------------


def test_pg_test_dsn_guard_never_falls_back_to_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """R3 #1, R4 #1 & Codex R3 #1:
    - PG integration test only accepts explicit DALTON_TEST_PG_DSN whose psycopg-resolved dbname ends with _test.
    - URI path ending in _test with query param ?dbname=prod MUST be rejected.
    - Keyword DSN & URI DSN both build search_path via psycopg.conninfo.make_conninfo (never ?options=).
    - Live SELECT current_database() guard aborts immediately if connected DB does not end with _test."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    monkeypatch.delenv("DALTON_TEST_PG_DSN", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod:secret@prod.db/dalton_test")
    assert _resolve_safe_test_pg_dsn() is None

    monkeypatch.setenv("DALTON_TEST_PG_DSN", "postgresql://u:p@localhost:5432/dalton_prod")
    assert _resolve_safe_test_pg_dsn() is None

    # R4 #1 / Codex R3 #1: path ends with _test, but query parameter dbname= points to another DB -> REJECT!
    monkeypatch.setenv("DALTON_TEST_PG_DSN", "postgresql://u:p@localhost:5432/dalton_test?dbname=prod")
    assert _resolve_safe_test_pg_dsn() is None

    monkeypatch.setenv("DALTON_TEST_PG_DSN", "host=localhost port=5432 dbname=dalton_prod user=postgres")
    assert _resolve_safe_test_pg_dsn() is None

    monkeypatch.setenv("DALTON_TEST_PG_DSN", "postgresql://u:p@localhost:5432/dalton_test?sslmode=disable")
    assert _resolve_safe_test_pg_dsn() == "postgresql://u:p@localhost:5432/dalton_test?sslmode=disable"

    kw_dsn = "host=localhost port=5432 dbname=pg_unit_test user=postgres options=-cTimeZone=UTC"
    monkeypatch.setenv("DALTON_TEST_PG_DSN", kw_dsn)
    assert _resolve_safe_test_pg_dsn() == kw_dsn

    # Build search_path cleanly via psycopg make_conninfo for BOTH URI and keyword DSNs (no '?options='!)
    built_uri = _build_test_pg_dsn_with_search_path("postgresql://u:p@localhost:5432/dalton_test", "sch_a")
    assert "?options=" not in built_uri
    parsed_uri = conninfo_to_dict(built_uri)
    assert parsed_uri["dbname"] == "dalton_test"
    assert "-csearch_path=sch_a" in parsed_uri["options"]

    built_kw = _build_test_pg_dsn_with_search_path(kw_dsn, "sch_b")
    assert "?options=" not in built_kw
    parsed_kw = conninfo_to_dict(built_kw)
    assert parsed_kw["dbname"] == "pg_unit_test"
    assert "-cTimeZone=UTC" in parsed_kw["options"]
    assert "-csearch_path=sch_b" in parsed_kw["options"]

    # Live SELECT current_database() guard (using create_autospec)
    cur_bad = create_autospec(psycopg.Cursor, instance=True)
    cur_bad.__enter__.return_value = cur_bad
    cur_bad.__exit__.return_value = False
    cur_bad.fetchone.return_value = ("dalton_prod",)
    conn_bad = create_autospec(psycopg.Connection, instance=True)
    conn_bad.cursor.return_value = cur_bad
    with pytest.raises(RuntimeError, match="non-\\*_test"):
        _assert_live_pg_is_test_db(conn_bad)
    cur_bad.execute.assert_called_once_with("SELECT current_database()")

    cur_ok = create_autospec(psycopg.Cursor, instance=True)
    cur_ok.__enter__.return_value = cur_ok
    cur_ok.__exit__.return_value = False
    cur_ok.fetchone.return_value = ("dalton_test",)
    conn_ok = create_autospec(psycopg.Connection, instance=True)
    conn_ok.cursor.return_value = cur_ok
    assert _assert_live_pg_is_test_db(conn_ok) == "dalton_test"


def _xml_full_cell_states(xlsx_path: Path, sheet_name: str) -> dict[str, dict[str, Any]]:
    """Inspect raw XML cell state (t, has_v, v_text, has_is) for ALL cells on ``sheet_name``."""
    _, payload = mhooks._read_package(xlsx_path)
    sst = mhooks._read_shared_strings(payload)
    part = mhooks._sheet_parts(payload)[sheet_name]
    root = etree.fromstring(payload[part], mhooks._XML)
    out: dict[str, dict[str, Any]] = {}
    for c in root.iter(f"{{{mhooks._SML}}}c"):
        ref = (c.get("r") or "").upper()
        if not ref:
            continue
        v = c.find(f"{{{mhooks._SML}}}v")
        is_el = c.find(f"{{{mhooks._SML}}}is")
        t = c.get("t")
        resolved = None
        if t == "s" and v is not None and (v.text or "").isdigit():
            idx = int(v.text)
            resolved = sst[idx] if 0 <= idx < len(sst) else None
        out[ref] = {
            "t": t,
            "has_v": v is not None,
            "v_text": v.text if v is not None else None,
            "has_is": is_el is not None,
            "resolved_sst": resolved,
        }
    return out


def test_cache_fidelity_sst_missing_v_target_exempt_and_unmodified_downstream_lo_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 #2 / Codex #1 / Grok #1 & 疑似:
    - Non-target t="s" stays t="s" (never degrades to inlineStr) even when reinserted.
    - Missing <v> with t="n" stays missing <v> with t="n" (never becomes <v/>); empty <v/> stays empty <v/>.
    - Dropped exempt cell on target sheet is reinserted by restore_donor_cache.
    - Editing a driver where unmodified downstream XIRR/XLOOKUP errors in LO keeps baseline <v>,
      does NOT backfill missing-cache unmodified formulas, and classifies as baseline_legacy in C13."""
    ws, _, base = _copy_fixture_to_ws(tmp_path)
    wb = openpyxl.load_workbook(base, data_only=False)
    ws_jd = wb["Model - JD"]
    # Add unmodified downstream XIRR/XLOOKUP formula (P44) depending on driver P25, plus missing-cache formula (K44)
    ws_jd["D44"].value = "Implied XIRR Check"
    ws_jd["P44"].value = "=XIRR(P4:P5,P25:P26)"
    ws_jd["K44"].value = "=SUM(E4:F4)"
    wb.save(base)
    wb.close()

    # Craft raw XML on base (simulating real Excel workbook with sharedStrings.xml):
    # - Model - BABA!D4 uses shared string t="s", <v>0</v> in xl/sharedStrings.xml
    # - Model - JD!P44 has non-empty baseline <v>0.185</v>
    # - Model - JD!K44 has explicit t="n" and NO <v> element at all
    # - Model - JD!K45 has explicit t="n" and empty <v/>
    infos, payload = mhooks._read_package(base)
    parts = mhooks._sheet_parts(payload)
    sst_setup = mhooks._SharedStringsMutator(infos, payload)
    baba_root = etree.fromstring(payload[parts["Model - BABA"]], mhooks._XML)
    for c in baba_root.iter(f"{{{mhooks._SML}}}c"):
        if (c.get("r") or "").upper() == "D4":
            idx0 = sst_setup.intern("BABA Revenue")
            mhooks._apply_cached_value(c, "s", str(idx0))
    payload[parts["Model - BABA"]] = etree.tostring(baba_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    sst_setup.flush()

    jd_root = etree.fromstring(payload[parts["Model - JD"]], mhooks._XML)
    for c in jd_root.iter(f"{{{mhooks._SML}}}c"):
        ref = (c.get("r") or "").upper()
        if ref == "E45":
            mhooks._apply_cached_value(c, "n", "38.5")
        elif ref == "P44":
            mhooks._apply_cached_value(c, "n", "0.185")
        elif ref == "K44":
            v_old = c.find(f"{{{mhooks._SML}}}v")
            if v_old is not None:
                c.remove(v_old)
            c.set("t", "n")
    # Insert K45 formula cell with empty <v/> and t="n"
    sd = jd_root.find(f"{{{mhooks._SML}}}sheetData")
    r45 = next(r for r in sd.findall(f"{{{mhooks._SML}}}row") if r.get("r") == "45")
    c_k45 = etree.SubElement(r45, f"{{{mhooks._SML}}}c", r="K45", t="n")
    f_k45 = etree.SubElement(c_k45, f"{{{mhooks._SML}}}f")
    f_k45.text = "SUM(E4:F4)"
    etree.SubElement(c_k45, f"{{{mhooks._SML}}}v")  # empty <v/>
    payload[parts["Model - JD"]] = etree.tostring(jd_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(base, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename])

    # Confirm base preconditions (including exact t="s" + <v>0</v>)
    baba_before = _xml_full_cell_states(base, "Model - BABA")
    assert baba_before["D4"]["t"] == "s" and baba_before["D4"]["v_text"] == "0"
    assert baba_before["D4"]["resolved_sst"] == "BABA Revenue"
    jd_before = _xml_full_cell_states(base, "Model - JD")
    assert jd_before["K44"]["has_v"] is False and jd_before["K44"]["t"] == "n"
    assert jd_before["K45"]["has_v"] is True and jd_before["K45"]["v_text"] is None and jd_before["K45"]["t"] == "n"

    # Simulate LibreOffice returning #NAME? on unmodified downstream XIRR P44, "0" on missing-cache K44,
    # valid updated value on P4, and #DIV/0! on newly added broken formula H50
    def _fake_soffice_xirr(src: Path, out_dir: Path, timeout: int) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        produced = out_dir / (src.stem + ".xlsx")
        shutil.copyfile(src, produced)
        _inject_xml_v_values(
            produced,
            {
                "Model - JD": {
                    "P4": (None, "2850.0"),
                    "P44": ("e", "#NAME?"),
                    "K44": (None, "0"),
                    "H50": ("e", "#DIV/0!"),
                }
            },
        )
        return produced

    soffice_stub = create_autospec(_REAL_SOFFICE_CALC, side_effect=_fake_soffice_xirr)
    which_stub = create_autospec(_REAL_SHUTIL_WHICH, return_value="/usr/bin/soffice")
    monkeypatch.setattr(recalc_mod, "soffice_calculate", soffice_stub)
    monkeypatch.setattr(recalc_mod.shutil, "which", which_stub)

    res = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "cell_edits": [
                {"sheet": "Model - JD", "cell": "P25", "value": 0.14},
                {"sheet": "Model - JD", "cell": "H50", "value": "=1/0"},
            ],
            "changelog_entry": {"version": "v02", "date": "2026-11-27", "baseline": base.name, "summary": "Driver edit"},
        },
        workspace_dir=ws,
        recalc_mod_override=recalc_mod,
    )
    cand = Path(res["candidate_path"])
    jd_after = _xml_full_cell_states(cand, "Model - JD")
    baba_after = _xml_full_cell_states(cand, "Model - BABA")

    # 1. Non-target sheet t="s" stayed t="s" (NOT inlineStr!)
    assert baba_after["D4"]["t"] == "s" and baba_after["D4"]["has_is"] is False
    assert baba_after["D4"]["resolved_sst"] == "BABA Revenue"
    # 2. Missing <v> with t="n" stayed missing <v> with t="n" (NOT <v/> and NOT backfilled to 0!)
    assert jd_after["K44"]["has_v"] is False and jd_after["K44"]["t"] == "n"
    # 3. Empty <v/> stayed empty <v/> with t="n"
    assert jd_after["K45"]["has_v"] is True and jd_after["K45"]["v_text"] is None and jd_after["K45"]["t"] == "n"
    # 4. Unmodified downstream XIRR P44 KEPT baseline <v>0.185</v> instead of #NAME?!
    assert jd_after["P44"]["v_text"] == "0.185" and jd_after["P44"]["t"] == "n"
    # 5. Newly added broken formula H50 KEPT its error value!
    assert jd_after["H50"]["t"] == "e" and jd_after["H50"]["v_text"] == "#DIV/0!"

    # 6. C13 classifies P44 as baseline_legacy and H50 as actionable!
    chk = mchk.run_checks(candidate_path=cand, ticker="JD", baseline_path=base, coord_map=res["coord_map"])
    c13_act = [f for f in chk["findings"] if f["check_id"] == "C13" and f["severity_class"] == "actionable"]
    c13_leg = [f for f in chk["findings"] if f["check_id"] == "C13" and f["severity_class"] == "baseline_legacy"]
    assert any(f.get("cell") == "H50" for f in c13_act), c13_act
    assert not any(f.get("cell") == "P44" for f in c13_act), c13_act
    assert any(f.get("cell") == "P44" for f in c13_leg), c13_leg

    # 7. Dropped t="s" cell on non-target sheet AND dropped exempt CIQ cell on target sheet are reinserted cleanly
    dropped = tmp_path / "dropped_both.xlsx"
    shutil.copyfile(base, dropped)
    inf2, pay2 = mhooks._read_package(dropped)
    pts2 = mhooks._sheet_parts(pay2)
    for sh_rm, crd_rm in (("Model - BABA", "D4"), ("Model - JD", "E45")):
        rt = etree.fromstring(pay2[pts2[sh_rm]], mhooks._XML)
        for cel in list(rt.iter(f"{{{mhooks._SML}}}c")):
            if (cel.get("r") or "").upper() == crd_rm:
                cel.getparent().remove(cel)
        pay2[pts2[sh_rm]] = etree.tostring(rt, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(dropped, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in inf2:
            zout.writestr(info, pay2[info.filename])

    mhooks.restore_donor_cache(
        dropped,
        {
            "cache_donor_path": str(base),
            "skip_sheets": ["Model - BABA"],
            "skip_cells": {"Model - JD": ["E45"]},
            "touched_formula_cells": {},
        },
    )
    baba_re = _xml_full_cell_states(dropped, "Model - BABA")
    assert baba_re["D4"]["t"] == "s" and baba_re["D4"]["resolved_sst"] == "BABA Revenue"
    jd_re = _xml_full_cell_states(dropped, "Model - JD")
    assert "E45" in jd_re and jd_re["E45"]["v_text"] == "38.5" and jd_re["E45"]["t"] == "n"


def test_consensus_append_protects_summary_formulas_and_distinguishes_segments(tmp_path: Path) -> None:
    """Codex #2 & #6: appending to consensus sheet never overwrites trailing summary formulas,
    and Cloud Revenue / Retail Revenue / Total Revenue keep distinct segment+metric keys."""
    ws, rdir, base = _copy_fixture_to_ws(tmp_path)
    cand = ws / "model" / "China_Ecommerce_cons_r3_v02.xlsx"
    shutil.copyfile(base, cand)

    # 1. Verify distinct segment+metric keys
    assert mcons._normalize_metric_key("Total Revenue") == "total_revenue"
    assert mcons._normalize_metric_key("Cloud Revenue") == "cloud::revenue"
    assert mcons._normalize_metric_key("Retail Revenue") == "retail::revenue"
    assert mcons._normalize_metric_key("Revenue", segment="Cloud") == "cloud::revenue"
    assert mcons._metric_category("cloud::revenue") == "segment_revenue"

    # 2. Put a trailing summary row immediately after row 5 (row 6 has ONLY House=SUM and Diff=AVERAGE formulas)
    wb = openpyxl.load_workbook(cand, data_only=False)
    ws_con = wb["vs Consensus"]
    ws_con["C6"].value = "=SUM(C2:C5)"
    ws_con["E6"].value = "=AVERAGE(E2:E5)"
    wb.save(cand)
    wb.close()

    snap = mcons.write_consensus_snapshot(
        model_path=cand,
        consensus_data={
            "items": [
                {"period": "2026E", "metric": "Total Revenue", "consensus": 12000.0},
                {"period": "2026E", "metric": "Cloud Revenue", "consensus": 2100.0},
                {"period": "2026E", "metric": "Retail Revenue", "consensus": 9900.0},
            ]
        },
        sheet_name="vs Consensus",
    )
    assert snap["layout_mode"] == "isolated_block"
    # Existing Total Revenue updated in row 2, while new Cloud & Retail items went to isolated block AFTER row 6!
    wb2 = openpyxl.load_workbook(cand, data_only=False)
    try:
        ws2 = wb2["vs Consensus"]
        assert ws2["C6"].value == "=SUM(C2:C5)"  # Summary formula preserved!
        assert ws2["E6"].value == "=AVERAGE(E2:E5)"  # Diff formula preserved!
        snap_coords = snap["consensus_snapshot_cells"]["vs Consensus"]
        # Three distinct rows (no collision between Total / Cloud / Retail Revenue!)
        assert len(set(snap_coords)) == 3, snap_coords
        new_rows = [int(c[1:]) for c in snap_coords if c != "D2"]
        assert all(r >= 8 for r in new_rows), snap_coords
    finally:
        wb2.close()

    # 3. Verify C04 does NOT confuse Group Total Revenue constant with Cloud Revenue snapshot value (2100.0)!
    cand_c04 = ws / "model" / "China_Ecommerce_seg_c04_v03.xlsx"
    shutil.copyfile(cand, cand_c04)
    wb3 = openpyxl.load_workbook(cand_c04, data_only=False)
    wb3["Model - JD"]["G6"].value = 2100.0  # Matches Cloud Revenue snapshot, NOT Total Revenue (12000.0)!
    wb3["README & Changelog"].cell(3, 1, "v03")
    wb3["README & Changelog"].cell(3, 4, "Segment key C04 test")
    wb3.save(cand_c04)
    wb3.close()
    chk_seg = mchk.run_checks(
        candidate_path=cand_c04,
        ticker="JD",
        baseline_path=cand,
        coord_map={"ops": [], "touched_cells": {"Model - JD": ["G6"]}, "cache_donor_path": str(cand)},
    )
    c04_act = [f for f in chk_seg["findings"] if f["check_id"] == "C04" and f["severity_class"] == "actionable"]
    assert not any(f.get("cell") == "G6" for f in c04_act), c04_act


def test_target_scope_enforcement_and_c14_rejects_non_target_structural_ops(tmp_path: Path) -> None:
    """Codex #3: model_edit rejects ops/edits on non-target sheets and is_consensus_snapshot on forecast sheets;
    C14 independently rejects illegal structural ops targeting non-target sheets."""
    ws, _, base = _copy_fixture_to_ws(tmp_path)

    # 1. model_edit refuses structural op on non-target BABA sheet
    with pytest.raises(ValueError, match="non-target company sheet"):
        medit.apply_edit_plan(
            baseline_path=base,
            ticker="JD",
            plan={"ops": [{"op": "insert_columns", "sheet": "Model - BABA", "at": "F", "count": 1}]},
            workspace_dir=ws,
            skip_recalc=True,
        )

    # 2. model_edit refuses cell edit on non-target BABA sheet
    with pytest.raises(ValueError, match="non-target company sheet"):
        medit.apply_edit_plan(
            baseline_path=base,
            ticker="JD",
            plan={"cell_edits": [{"sheet": "Model - BABA", "cell": "F4", "value": 999.0}]},
            workspace_dir=ws,
            skip_recalc=True,
        )

    # 3. model_edit refuses is_consensus_snapshot on forecast sheet Model - JD
    with pytest.raises(ValueError, match="non-consensus sheet"):
        medit.apply_edit_plan(
            baseline_path=base,
            ticker="JD",
            plan={"cell_edits": [{"sheet": "Model - JD", "cell": "G6", "value": 12000.0, "is_consensus_snapshot": True}]},
            workspace_dir=ws,
            skip_recalc=True,
        )

    # 4. C14 independently rejects an illegal structural op on Model - BABA even with forged post-insert donor!
    forged_donor = tmp_path / "forged_donor.xlsx"
    shutil.copyfile(base, forged_donor)
    rep = insert_mod.execute(
        ["columns", str(forged_donor), "--sheet", "Model - BABA", "--at", "F", "--count", "1", "--preserve-calcpr"]
    )
    assert rep["status"] == "success"
    forged_cand = ws / "model" / "China_Ecommerce_forged_v02.xlsx"
    shutil.copyfile(forged_donor, forged_cand)
    chk = mchk.run_checks(
        candidate_path=forged_cand,
        ticker="JD",
        baseline_path=base,
        coord_map={
            "ops": [{"op": "insert_columns", "sheet": "Model - BABA", "at": "F", "count": 1}],
            "cache_donor_path": str(forged_donor),
        },
    )
    c14_act = [f for f in chk["findings"] if f["check_id"] == "C14" and f["severity_class"] == "actionable"]
    assert any("Illegal structural op" in f["message"] and f["sheet"] == "Model - BABA" for f in c14_act), c14_act


def test_c13_replaced_ciq_formula_is_actionable_and_sheet_qualified_defined_name_exempt(tmp_path: Path) -> None:
    """Codex #4: replacing a former CIQ cell with =1/0 is actionable in C13, while 'Rates'!ExtRate
    (sheet-qualified local defined name) is resolved as an external link and exempted."""
    ws, _, base = _copy_fixture_to_ws(tmp_path)

    # Enrich base with a sheet 'Rates' holding a sheet-scoped external defined name ExtRate
    wb_b = openpyxl.load_workbook(base, data_only=False)
    ws_rates = wb_b.create_sheet("Rates")
    ws_rates["A1"].value = "FX Rates"
    ws_rates.defined_names.add(DefinedName(name="ExtRate", attr_text="[ExternalFX.xlsx]FX!$B$2", localSheetId=wb_b.sheetnames.index("Rates")))
    ws_jd_b = wb_b["Model - JD"]
    ws_jd_b["D49"].value = "Sheet-Qualified ExtRate"
    ws_jd_b["E49"].value = "='Rates'!ExtRate*1.0"
    ws_jd_b["F49"].value = "=E49*2"
    wb_b.save(base)
    wb_b.close()
    _inject_xml_v_values(base, {"Model - JD": {"E49": ("n", "7.25"), "F49": ("n", "14.5")}})

    fp_b = mfp.build_fingerprint(base, ticker="JD")
    assert fp_b["sheets"]["Model - JD"]["cells"]["E49"]["subcategory"] == "external_link"
    assert any(
        c["sheet"] == "Model - JD" and c["cell"] == "E49"
        for c in fp_b["external_links_baseline"]["cells"]
    )

    # Replace former CIQ cell E45 with =1/0 (#DIV/0!) while leaving 'Rates'!ExtRate (E49/F49) intact
    cand = ws / "model" / "China_Ecommerce_c13_r3_v02.xlsx"
    shutil.copyfile(base, cand)
    wb_c = openpyxl.load_workbook(cand, data_only=False)
    wb_c["Model - JD"]["E45"].value = "=1/0"
    wb_c["README & Changelog"].cell(3, 1, "v02")
    wb_c["README & Changelog"].cell(3, 4, "Replace CIQ with 1/0")
    wb_c.save(cand)
    wb_c.close()
    _inject_xml_v_values(cand, {"Model - JD": {"E45": ("e", "#DIV/0!"), "E49": ("e", "#REF!"), "F49": ("e", "#REF!")}})

    chk = mchk.run_checks(
        candidate_path=cand,
        ticker="JD",
        baseline_path=base,
        coord_map={"ops": [], "touched_cells": {"Model - JD": ["E45"]}, "touched_formula_cells": {"Model - JD": ["E45"]}},
    )
    c13_act = [f for f in chk["findings"] if f["check_id"] == "C13" and f["severity_class"] == "actionable"]
    c13_leg = [f for f in chk["findings"] if f["check_id"] == "C13" and f["severity_class"] == "baseline_legacy"]
    assert any(f.get("cell") == "E45" for f in c13_act), c13_act
    assert not any(f.get("cell") in ("E49", "F49") for f in c13_act), c13_act
    assert any(f.get("cell") == "E49" for f in c13_leg), c13_leg
    assert any(f.get("cell") == "F49" for f in c13_leg), c13_leg


def test_c09_c10_c11_c12_baseline_relative_discrepancy_comparison(tmp_path: Path) -> None:
    """R3 #3 / Codex #5 / Grok #2: editing an unrelated driver (Q25) in a column with pre-existing
    C09/C11/C12 discrepancies MUST keep them baseline_legacy (actionable == 0); only worsening or new
    discrepancies become actionable."""
    ws, _, base = _copy_fixture_to_ws(tmp_path)
    # Inject pre-existing legacy discrepancies on col Q (3Q26E) & col G (2026E) into BASELINE:
    # - C10 Annual vs Quarters flow mismatch: G4=15000 vs sum(O4:R4) (~11000)
    # - C11 Segment OP disconnect: Q12=300 vs segments 150+30-10=170 (diff=+130)
    # - C09 Cash bridge mismatch: Q34=2500 vs Q37=2300 (diff=+200)
    # - C09 Balance Sheet imbalance: Q39=10500 vs Q42=10000 (diff=+500)
    # - C12 Cash rollforward break: Q32=2000 + Q33=100 != Q34=2500 (diff=-400)
    _inject_xml_v_values(
        base,
        {
            "Model - JD": {
                "G4": (None, "15000.0"),
                "Q12": (None, "300.0"),
                "Q19": (None, "150.0"),
                "Q20": (None, "30.0"),
                "Q21": (None, "-10.0"),
                "Q32": (None, "2000.0"),
                "Q33": (None, "100.0"),
                "Q34": (None, "2500.0"),
                "Q37": (None, "2300.0"),
                "Q39": (None, "10500.0"),
                "Q42": (None, "10000.0"),
            }
        },
    )

    # 1. Edit ONLY unrelated driver Q25 (+ changelog) -> all legacy discrepancies stay baseline_legacy!
    res_clean = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "cell_edits": [{"sheet": "Model - JD", "cell": "Q25", "value": 0.125}],
            "changelog_entry": {"version": "v02", "date": "2026-11-27", "baseline": base.name, "summary": "Unrelated driver edit"},
        },
        workspace_dir=ws,
        skip_recalc=True,
    )
    chk_clean = mchk.run_checks(
        candidate_path=res_clean["candidate_path"],
        ticker="JD",
        baseline_path=base,
        coord_map=res_clean["coord_map"],
    )
    assert chk_clean["counts"]["actionable"] == 0, [f for f in chk_clean["findings"] if f["severity_class"] == "actionable"]
    leg_ids = {f["check_id"] for f in chk_clean["findings"] if f["severity_class"] == "baseline_legacy"}
    assert {"C09", "C10", "C11", "C12"} <= leg_ids, chk_clean["findings"]

    # 2. Now WORSEN the C11 discrepancy at Q12 (300 -> 450, diff grows from +130 to +280) -> MUST be actionable!
    cand_worse = Path(res_clean["candidate_path"])
    _inject_xml_v_values(cand_worse, {"Model - JD": {"Q12": (None, "450.0")}})
    chk_worse = mchk.run_checks(
        candidate_path=cand_worse,
        ticker="JD",
        baseline_path=base,
        coord_map=res_clean["coord_map"],
    )
    assert any(
        f["check_id"] == "C11" and f["severity_class"] == "actionable" and f["cell"] == "Q12"
        for f in chk_worse["findings"]
    ), chk_worse["findings"]


def test_c02_missing_first_forecast_quarter_and_non_dec_march_fy_c10(tmp_path: Path) -> None:
    """R3 #5 & #6 / Codex #7 / Grok #4:
    - C02 catches a missing quarter immediately after latest actual quarter (2Q26A -> 4Q26E..4Q27E missing 3Q26E).
    - Non-Dec March fiscal year end (fy_end_month=3) maps calendar quarters 2Q25..1Q26 -> FY26 and 2Q26..1Q27 -> FY27,
      and C10 catches a mismatch on FY27E."""
    ws, _, base = _copy_fixture_to_ws(tmp_path)

    # Part A: 2Q26A (P1) followed directly by 4Q26E (Q1) .. 4Q27E (U1) -> missing 3Q26E MUST be C02 actionable!
    cand_c02 = ws / "model" / "China_Ecommerce_c02_gap_v02.xlsx"
    shutil.copyfile(base, cand_c02)
    wb = openpyxl.load_workbook(cand_c02, data_only=False)
    ws_jd = wb["Model - JD"]
    ws_jd["P1"].value = "2Q26A"
    ws_jd["Q1"].value = "4Q26E"
    ws_jd["R1"].value = "1Q27E"
    ws_jd["S1"].value = "2Q27E"
    ws_jd["T1"].value = "3Q27E"
    ws_jd["U1"].value = "4Q27E"
    ws_jd["V1"].value = None
    wb["README & Changelog"].cell(3, 1, "v02")
    wb["README & Changelog"].cell(3, 4, "Missing 3Q26E test")
    wb.save(cand_c02)
    wb.close()
    chk_c02 = mchk.run_checks(
        candidate_path=cand_c02,
        ticker="JD",
        baseline_path=base,
        coord_map={"ops": [], "touched_cells": {"Model - JD": ["P1", "Q1"]}, "cache_donor_path": str(base)},
    )
    c02_act = [f for f in chk_c02["findings"] if f["check_id"] == "C02" and f["severity_class"] == "actionable"]
    assert any("3Q26E" in f["message"] for f in c02_act), c02_act

    # Part B: March fiscal-year-end (fy_end_month=3) fixture where quarters cross two calendar years
    mar_base = ws / "model" / "BABA_MarFY_v01.xlsx"
    wb_m = openpyxl.Workbook()
    ws_rm = wb_m.active
    ws_rm.title = "README & Changelog"
    ws_rm["A1"].value = "Version"
    ws_rm["A2"].value = "v01"
    ws_m = wb_m.create_sheet("Model - BABA")
    ws_m["A1"].value = "RMB MM"
    ws_m["A2"].value = "FY ends Mar"
    headers_m = [
        (5, "FY26A"), (6, "FY27E"), (7, "FY28E"), (8, "FY29E"),
        (10, "2Q25A"), (11, "3Q25A"), (12, "4Q25A"), (13, "1Q26A"),
        (14, "2Q26E"), (15, "3Q26E"), (16, "4Q26E"), (17, "1Q27E"),
    ]
    for ci, lbl in headers_m:
        ws_m.cell(1, ci, lbl)
    ws_m["A3"].value = "Income Statement"
    ws_m["D4"].value = "Total Revenue"
    ws_m["E4"].value = "=SUM(J4:M4)"
    ws_m["F4"].value = "=SUM(N4:Q4)"
    ws_m["G4"].value = "=F4*1.1"
    ws_m["H4"].value = "=G4*1.1"
    for ci, val in ((10, 100.0), (11, 110.0), (12, 120.0), (13, 130.0), (14, 140.0), (15, 150.0), (16, 160.0), (17, 170.0)):
        ws_m.cell(4, ci, val)
    wb_m.save(mar_base)
    wb_m.close()
    _inject_xml_v_values(mar_base, {"Model - BABA": {"E4": (None, "460.0"), "F4": (None, "620.0"), "G4": (None, "682.0"), "H4": (None, "750.2")}})

    fp_mar = mfp.build_fingerprint(mar_base, ticker="BABA")
    p_axis = fp_mar["sheets"]["Model - BABA"]["period_axis"]
    assert p_axis["fiscal_year_end_month"] == 3
    assert p_axis["annual_sum_quarters"]["E"] == ["J", "K", "L", "M"]
    assert p_axis["annual_sum_quarters"]["F"] == ["N", "O", "P", "Q"]

    # Inject a C10 mismatch on FY27E (F4 = 999 instead of 140+150+160+170 = 620) -> MUST hit C10 actionable!
    mar_bad = ws / "model" / "BABA_MarFY_v02.xlsx"
    shutil.copyfile(mar_base, mar_bad)
    _inject_xml_v_values(mar_bad, {"Model - BABA": {"F4": (None, "999.0")}})
    chk_mar = mchk.run_checks(
        candidate_path=mar_bad,
        ticker="BABA",
        baseline_path=mar_base,
        coord_map={"ops": [], "touched_cells": {"Model - BABA": ["F4"]}, "cache_donor_path": str(mar_base)},
    )
    assert any(
        f["check_id"] == "C10" and f["severity_class"] == "actionable" and f["cell"] == "F4"
        for f in chk_mar["findings"]
    ), chk_mar["findings"]


def test_stale_consensus_diff_hash_is_stage_failure_only_and_cross_run_id_rejected(tmp_path: Path) -> None:
    """R3 #4 / Grok #3 / Codex #8:
    - Stale consensus-diff.json hash records ONLY failed_stages=['consensus'], NEVER actionable.
    - Same-hash model-check.md from an earlier run_id is rejected by write_fallback_ledger and verify_done;
      verify_done also enforces all 4 count classes."""
    ws, rdir, base = _copy_fixture_to_ws(tmp_path)
    dump_dir = rdir / "model_dump"
    dump_dir.mkdir(parents=True, exist_ok=True)

    # Part A: Stale consensus-diff.json with mismatched hash in research dir -> actionable == 0, consensus in failed_stages!
    (dump_dir / "consensus-diff.json").write_text(
        json.dumps({"status": "ok", "model_sha256": "0" * 64, "missing_rationale_count": 3, "items": []}),
        encoding="utf-8",
    )
    (dump_dir / "consensus-diff.md").write_text("# Stale diff\n000000000000\n", encoding="utf-8")

    res_a = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir,
        candidate_path=base,
        baseline_path=base,
        run_id="run-A",
    )
    assert res_a["counts"]["actionable"] == 0, res_a["findings"]
    assert "consensus" in res_a["failed_stages"]
    assert res_a["final_status"] == "delivered_with_open_issues"
    vd_a = mchk.verify_done(workspace_dir=ws, ticker="JD", research_dir=rdir, expected_run_id="run-A")
    assert vd_a["ok"] is True, vd_a

    # Part B: Rerun same day on the EXACT SAME candidate file (identical SHA-256!) under run-B
    wl_b = mchk.write_fallback_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir,
        candidate_path=base,
        kind="modeling",
        failed_stages=["consensus"],
        run_id="run-B",
    )
    # Old run-A model-check.md MUST NOT be accepted for run-B even though SHA-256 matches!
    assert "check" in wl_b["failed_stages"], wl_b
    assert "check" not in wl_b["ledger"]["rounds"][-1]["evidence"]

    # Tamper ledger to claim run-B completed check with run-A's model-check.md -> verify_done MUST fail!
    ledger_p = dump_dir / "model-verification.json"
    tampered = json.loads(ledger_p.read_text(encoding="utf-8"))
    tampered["failed_stages"] = ["consensus"]
    tampered["rounds"][-1]["failed_stages"] = ["consensus"]
    tampered["rounds"][-1]["evidence"]["check"] = {
        "path": "research/2026-11-27-model-JD/model_dump/model-check.md",
        "evidence_model_sha256": tampered["final_model_sha256"],
        "counts": res_a["counts"],
    }
    ledger_p.write_text(json.dumps(tampered, indent=2), encoding="utf-8")
    vd_b = mchk.verify_done(workspace_dir=ws, ticker="JD", research_dir=rdir, expected_run_id="run-B")
    assert vd_b["ok"] is False
    assert any("run_id" in e for e in vd_b["errors"]), vd_b["errors"]

    # Tamper one of the 4 counts (baseline_legacy) in ledger -> verify_done MUST reject count mismatch!
    res_c = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir,
        candidate_path=base,
        baseline_path=base,
        run_id="run-C",
    )
    assert res_c["counts"]["baseline_legacy"] >= 1
    leg_tamp = json.loads(ledger_p.read_text(encoding="utf-8"))
    leg_tamp["rounds"][-1]["evidence"]["check"]["counts"]["baseline_legacy"] += 99
    ledger_p.write_text(json.dumps(leg_tamp, indent=2), encoding="utf-8")
    vd_c = mchk.verify_done(workspace_dir=ws, ticker="JD", research_dir=rdir, expected_run_id="run-C")
    assert vd_c["ok"] is False
    assert any("baseline_legacy" in e for e in vd_c["errors"]), vd_c["errors"]


def test_recover_baseline_minio_xl_meta_part_and_pg_dump_custom_format(tmp_path: Path) -> None:
    """Codex #9 & Scenario 7:
    - Weekly MinIO volume backup recovery supports xl.meta inline XL2 payloads AND bitrot-prefixed part.1.
    - Daily pg_dump -Fc (PGDMP) uses compatible pg_restore and emits an explicit diagnostic (never silent SQL scan) when missing.
    - SQLite time-window filter strictly excludes outside-window candidates."""
    baseline_bytes = FIXTURE_XLSX.read_bytes()
    baseline_sha = hashlib.sha256(baseline_bytes).hexdigest()
    key_file = tmp_path / "backup.key"
    key_file.write_text("r3-minio-pgdmp-test-key\n", encoding="utf-8")
    ws_id = "11111111-2222-3333-4444-555555555555"

    def _encrypt_bytes(raw: bytes, out_path: Path) -> None:
        subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", f"file:{key_file}", "-out", str(out_path)],
            input=raw,
            check=True,
        )

    def _make_fake_rclone(script_path: Path, subdir: str, day: str, filename: str, enc_path: Path) -> None:
        script_path.write_text(
            f"""#!/usr/bin/env bash
if [[ "$1" == "lsf" && "$2" == "--dirs-only" && "$3" == *"/{subdir}/" ]]; then
  echo "{day}/"
elif [[ "$1" == "lsf" && "$2" == "--files-only" && "$3" == *"/{subdir}/{day}/" ]]; then
  echo "{filename}"
elif [[ "$1" == "cat" ]]; then
  cat "{enc_path}"
fi
""",
            encoding="utf-8",
        )
        script_path.chmod(0o755)

    # 1. MinIO XL2 inline xl.meta inside weekly tar.gz.enc
    # Construct valid XL2 header + msgpack map {"V": 2, "Inline": <bin32 baseline_bytes>}
    import struct as _st

    msgpack_inline = (
        b"\x82"  # fixmap(2)
        b"\xa1V\x02"  # "V": 2
        b"\xa6Inline"
        + b"\xc6" + _st.pack(">I", len(baseline_bytes)) + baseline_bytes
    )
    xl_meta_bytes = b"XL2 " + _st.pack("<HH", 1, 3) + msgpack_inline
    tar_xl = io.BytesIO()
    with tarfile.open(fileobj=tar_xl, mode="w:gz") as tf:
        ti = tarfile.TarInfo(name="dalton-uploads/attachments/att-1/China_Ecommerce_Model.xlsx/xl.meta")
        ti.size = len(xl_meta_bytes)
        tf.addfile(ti, io.BytesIO(xl_meta_bytes))
    enc_xl = tmp_path / "minio-xl-2026-09-25.tar.gz.enc"
    _encrypt_bytes(tar_xl.getvalue(), enc_xl)
    rc_xl = tmp_path / "rclone_xl.sh"
    _make_fake_rclone(rc_xl, "weekly", "2026-09-25", enc_xl.name, enc_xl)

    out_xl = tmp_path / "recovered_xl.xlsx"
    res_xl = recover_lib.recover_baseline(
        workspace_id=ws_id,
        output_path=out_xl,
        before_first_edit_ts="2026-10-01T10:00:00+00:00",
        expected_sha256=baseline_sha,
        expected_filename="China_Ecommerce_Model.xlsx",
        rclone_remote="fakegdrive",
        rclone_drive_dir="DaltonBackup",
        rclone_bin=str(rc_xl),
        backup_key_file=key_file,
    )
    assert res_xl.found is True and "minio-xl-inline:" in (res_xl.source_ref or "")
    assert out_xl.read_bytes() == baseline_bytes

    # 2. MinIO single-drive part.1 with 32-byte HighwayHash256 bitrot header inside weekly tar.gz.enc
    part1_bytes = (b"\xab" * 32) + baseline_bytes
    tar_part = io.BytesIO()
    with tarfile.open(fileobj=tar_part, mode="w:gz") as tf:
        ti_m = tarfile.TarInfo(name="dalton-uploads/uploads/up-1/China_Ecommerce_Model.xlsx/xl.meta")
        ti_m.size = 8
        tf.addfile(ti_m, io.BytesIO(b"XL2 \x01\x00\x03\x00"))
        ti_p = tarfile.TarInfo(name="dalton-uploads/uploads/up-1/China_Ecommerce_Model.xlsx/data-uuid/part.1")
        ti_p.size = len(part1_bytes)
        tf.addfile(ti_p, io.BytesIO(part1_bytes))
    enc_part = tmp_path / "minio-part-2026-09-25.tar.gz.enc"
    _encrypt_bytes(tar_part.getvalue(), enc_part)
    rc_part = tmp_path / "rclone_part.sh"
    _make_fake_rclone(rc_part, "weekly", "2026-09-25", enc_part.name, enc_part)

    out_part = tmp_path / "recovered_part.xlsx"
    res_part = recover_lib.recover_baseline(
        workspace_id=ws_id,
        output_path=out_part,
        before_first_edit_ts="2026-10-01T10:00:00+00:00",
        expected_sha256=baseline_sha,
        expected_filename="China_Ecommerce_Model.xlsx",
        rclone_remote="fakegdrive",
        rclone_drive_dir="DaltonBackup",
        rclone_bin=str(rc_part),
        backup_key_file=key_file,
    )
    assert res_part.found is True and "minio-part:" in (res_part.source_ref or "")
    assert out_part.read_bytes() == baseline_bytes

    # 3. Daily pg_dump -Fc (PGDMP custom format) with compatible pg_restore vs missing pg_restore diagnostic
    pgdmp_payload = b"PGDMP\x01\x0e\x00FAKE-CUSTOM-DUMP-BODY"
    enc_pgdmp = tmp_path / "dalton-2026-09-29.dump.enc"
    _encrypt_bytes(pgdmp_payload, enc_pgdmp)
    rc_pgdmp = tmp_path / "rclone_pgdmp.sh"
    _make_fake_rclone(rc_pgdmp, "db", "2026-09-29", enc_pgdmp.name, enc_pgdmp)

    fake_pg_restore = tmp_path / "fake_pg_restore.py"
    sql_fixture_file = tmp_path / "restored_copy.sql"
    sql_fixture_file.write_text(
        "COPY public.chat_attachments (attachment_id, user_id, name, size, content) FROM stdin;\n"
        f"att-pgdmp\tsender-u3\tChina_Ecommerce_Model.xlsx\t{len(baseline_bytes)}\t\\\\x{baseline_bytes.hex()}\n"
        "\\.\n",
        encoding="utf-8",
    )
    fake_pg_restore.write_text(
        f"""#!{sys.executable}
import sys
data = sys.stdin.buffer.read()
assert data.startswith(b"PGDMP"), "expected PGDMP custom format on stdin"
sys.stdout.write(open({str(sql_fixture_file)!r}, encoding="utf-8").read())
""",
        encoding="utf-8",
    )
    fake_pg_restore.chmod(0o755)

    out_pgdmp = tmp_path / "recovered_pgdmp.xlsx"
    res_pgdmp = recover_lib.recover_baseline(
        workspace_id=ws_id,
        output_path=out_pgdmp,
        before_first_edit_ts="2026-10-01T10:00:00+00:00",
        expected_sha256=baseline_sha,
        rclone_remote="fakegdrive",
        rclone_drive_dir="DaltonBackup",
        rclone_bin=str(rc_pgdmp),
        backup_key_file=key_file,
        pg_restore_bin=str(fake_pg_restore),
    )
    assert res_pgdmp.found is True and res_pgdmp.channel == "rclone_encrypted_db_backup"
    assert out_pgdmp.read_bytes() == baseline_bytes

    # Missing pg_restore & disabled docker compose on PGDMP MUST report diagnostic (never silently scan binary as SQL)
    res_pgdmp_miss = recover_lib.recover_baseline(
        workspace_id=ws_id,
        output_path=tmp_path / "missing_pgdmp.xlsx",
        before_first_edit_ts="2026-10-01T10:00:00+00:00",
        expected_sha256=baseline_sha,
        rclone_remote="fakegdrive",
        rclone_drive_dir="DaltonBackup",
        rclone_bin=str(rc_pgdmp),
        backup_key_file=key_file,
        pg_restore_bin="/nonexistent/pg_restore_bin",
        pg_restore_compose_service=None,
        pg_restore_docker_image=None,
    )
    assert res_pgdmp_miss.found is False
    assert any("PGDMP" in d and "pg_restore" in d for d in res_pgdmp_miss.diagnostics), res_pgdmp_miss.diagnostics

    # 3b. Container pg_restore fallback (`docker compose exec -T postgres pg_restore -f - --data-only -t chat_attachments`)
    # aligned with scripts/dalton/ops/prod/backup.sh when host pg_restore is absent!
    fake_docker = tmp_path / "fake_docker.py"
    docker_log = tmp_path / "fake_docker_args.json"
    fake_docker.write_text(
        f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
data = sys.stdin.buffer.read()
Path({str(docker_log)!r}).write_text(json.dumps({{"args": args, "cwd": os.getcwd(), "is_pgdmp": data.startswith(b"PGDMP")}}), encoding="utf-8")
assert args == ["compose", "exec", "-T", "postgres", "pg_restore", "-f", "-", "--data-only", "-t", "chat_attachments"], args
assert data.startswith(b"PGDMP")
sys.stdout.write(open({str(sql_fixture_file)!r}, encoding="utf-8").read())
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    out_comp = tmp_path / "recovered_pgdmp_compose.xlsx"
    res_comp = recover_lib.recover_baseline(
        workspace_id=ws_id,
        output_path=out_comp,
        before_first_edit_ts="2026-10-01T10:00:00+00:00",
        expected_sha256=baseline_sha,
        rclone_remote="fakegdrive",
        rclone_drive_dir="DaltonBackup",
        rclone_bin=str(rc_pgdmp),
        backup_key_file=key_file,
        pg_restore_bin="/nonexistent/pg_restore_bin",
        pg_restore_compose_service="postgres",
        pg_restore_compose_cwd=tmp_path,
        docker_bin=str(fake_docker),
    )
    assert res_comp.found is True and res_comp.channel == "rclone_encrypted_db_backup"
    assert out_comp.read_bytes() == baseline_bytes
    logged_dk = json.loads(docker_log.read_text(encoding="utf-8"))
    assert logged_dk["is_pgdmp"] is True
    assert Path(logged_dk["cwd"]).resolve() == tmp_path.resolve()

    # Also verify recover_baseline_model.sh shell wrapper forwards compose defaults cleanly!
    out_sh_comp = tmp_path / "recovered_sh_pgdmp_compose.xlsx"
    sh_proc = subprocess.run(
        [
            "bash",
            str(PROD_OPS_DIR / "recover_baseline_model.sh"),
            "--workspace-id",
            ws_id,
            "--output",
            str(out_sh_comp),
            "--before-ts",
            "2026-10-01T10:00:00+00:00",
            "--expected-sha256",
            baseline_sha,
            "--rclone-remote",
            "fakegdrive",
            "--rclone-drive-dir",
            "DaltonBackup",
            "--rclone-bin",
            str(rc_pgdmp),
            "--backup-key-file",
            str(key_file),
            "--pg-restore-bin",
            "/nonexistent/pg_restore_bin",
            "--docker-bin",
            str(fake_docker),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert sh_proc.returncode == 0, sh_proc.stderr + sh_proc.stdout
    assert out_sh_comp.read_bytes() == baseline_bytes

    # 4. Direct time-window exclusion assertion (Scenario 7 strictness)
    db_win = tmp_path / "win_only.sqlite3"
    c_w = sqlite3.connect(db_win)
    c_w.execute(
        """
        CREATE TABLE chat_attachments (
            attachment_id TEXT PRIMARY KEY, user_id TEXT, name TEXT, size INT,
            object_key TEXT, content BLOB, state TEXT, created_at TEXT, sent_at TEXT, cleaned_at TEXT
        );
        """
    )
    c_w.execute(
        "INSERT INTO chat_attachments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "att-outside-only", "sender-u3", "China_Ecommerce_Model.xlsx", len(baseline_bytes),
            None, baseline_bytes, "sent", "2026-09-28T07:00:00+00:00", "2026-09-28T07:15:00+00:00", None,
        ),
    )
    c_w.commit()
    c_w.close()
    adp_w = recover_lib.SQLiteReadOnlyAdapter(db_win)
    try:
        cands = adp_w.fetch_inline_candidates(
            sender_user_ids=["sender-u3"],
            att_name="China_Ecommerce_Model.xlsx",
            att_size=len(baseline_bytes),
            before_ts="2026-10-01T10:00:00+00:00",
            turn_created_at="2026-09-28T08:00:00+00:00",
        )
        assert cands == [], f"outside-window candidate should be excluded: {cands}"
    finally:
        adp_w.close()


# ---------------------------------------------------------------------------
# 11. Round-4 targeted tests (Codex & Grok R3 review items + Lead #1-#4)
# ---------------------------------------------------------------------------


def test_non_target_rich_text_sst_and_inline_str_preserved_and_checked_by_c14(tmp_path: Path) -> None:
    """R4 #2 & Codex R3 #2:
    - Non-target sheet keeps exact post-insert baseline SST index (<v>17</v>) and full rich-text <si> (partial bold).
    - Non-target sheet keeps full rich-text inlineStr <is><r><rPr><b/></rPr>...</is>.
    - C14 asserts raw OOXML cell state on non-target sheets (detects SST renumbering & rich-text loss even when display text matches)."""
    ws, _, base = _copy_fixture_to_ws(tmp_path)

    # Craft raw OOXML on base:
    # - xl/sharedStrings.xml with 18 entries (0..16 padding + index 17 rich-text partial-bold 'BABA Revenue')
    # - Model - BABA!D4 -> t="s" <v>17</v>
    # - Model - BABA!D5 -> t="inlineStr" <is><r><rPr><b/></rPr><t>BABA</t></r><r><t xml:space="preserve"> Cloud</t></r></is>
    infos, payload = mhooks._read_package(base)
    parts = mhooks._sheet_parts(payload)
    sst_mut = mhooks._SharedStringsMutator(infos, payload)
    for idx in range(17):
        sst_mut.intern(f"Dummy SST Padding {idx}")
    rich_si = etree.fromstring(
        f'<si xmlns="{mhooks._SML}">'
        '<r><rPr><b/></rPr><t>BABA</t></r>'
        '<r><t xml:space="preserve"> Revenue</t></r>'
        '</si>',
        mhooks._XML,
    )
    idx17 = sst_mut.intern_si(rich_si)
    assert idx17 == 17

    rich_is_xml = (
        f'<is xmlns="{mhooks._SML}">'
        '<r><rPr><b/></rPr><t>BABA</t></r>'
        '<r><t xml:space="preserve"> Cloud</t></r>'
        '</is>'
    )
    baba_root = etree.fromstring(payload[parts["Model - BABA"]], mhooks._XML)
    for c in baba_root.iter(f"{{{mhooks._SML}}}c"):
        ref = (c.get("r") or "").upper()
        if ref == "D4":
            mhooks._apply_cached_value(c, "s", "17")
        elif ref == "D5":
            for child in list(c):
                c.remove(child)
            c.set("t", "inlineStr")
            c.append(etree.fromstring(rich_is_xml, mhooks._XML))
    payload[parts["Model - BABA"]] = etree.tostring(baba_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    sst_mut.flush()
    with zipfile.ZipFile(base, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename])

    base_raw_baba = mhooks.extract_raw_xml_cell_states(base, {"Model - BABA"})["Model - BABA"]
    assert base_raw_baba["D4"]["t"] == "s" and base_raw_baba["D4"]["v"] == "17"
    assert b"<b" in (base_raw_baba["D4"]["si_c14n"] or b"")
    assert base_raw_baba["D5"]["t"] == "inlineStr"
    assert b"<b" in (base_raw_baba["D5"]["is_c14n"] or b"")

    # Apply a normal target-sheet edit (+ column insert + changelog string) via model_edit.apply_edit_plan
    res = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "ops": [{"op": "insert_columns", "sheet": "Model - JD", "at": "P", "count": 1, "copy_style_from": "O"}],
            "added_period_cols": {"Model - JD": ["P"]},
            "cell_edits": [
                {"sheet": "Model - JD", "cell": "P1", "value": "2Q26E_NEW"},
                {"sheet": "Model - JD", "cell": "Q25", "value": 0.115},
            ],
            "changelog_entry": {"version": "v02", "date": "2026-11-27", "baseline": base.name, "summary": "Rich text SST test"},
        },
        workspace_dir=ws,
        skip_recalc=True,
    )
    cand = Path(res["candidate_path"])
    cand_raw_baba = mhooks.extract_raw_xml_cell_states(cand, {"Model - BABA"})["Model - BABA"]

    # 1. Exact SST index <v>17</v> AND complete rich-text <si> (partial bold) preserved!
    assert cand_raw_baba["D4"]["t"] == "s"
    assert cand_raw_baba["D4"]["v"] == "17"
    assert cand_raw_baba["D4"]["si_c14n"] == base_raw_baba["D4"]["si_c14n"]
    # 2. Exact rich-text inlineStr <is> preserved!
    assert cand_raw_baba["D5"]["t"] == "inlineStr"
    assert cand_raw_baba["D5"]["is_c14n"] == base_raw_baba["D5"]["is_c14n"]
    # 3. Newly written changelog string on target sheet is still valid & readable
    wb_chk = openpyxl.load_workbook(cand, data_only=False)
    try:
        assert any(
            str(wb_chk["README & Changelog"].cell(r, 4).value or "") == "Rich text SST test"
            for r in range(1, wb_chk["README & Changelog"].max_row + 1)
        )
    finally:
        wb_chk.close()

    # 4. C14 passes with ZERO findings on properly restored candidate!
    chk_ok = mchk.run_checks(candidate_path=cand, ticker="JD", baseline_path=base, coord_map=res["coord_map"])
    c14_ok = [f for f in chk_ok["findings"] if f["check_id"] == "C14" and f["severity_class"] == "actionable"]
    assert c14_ok == [], c14_ok

    # 5. Tamper D4 to plain-text rebuilt <si> at index 0 (same display value!) & D5 to plain-text <is> -> C14 MUST flag BOTH!
    tampered = ws / "model" / "China_Ecommerce_rich_loss_v03.xlsx"
    shutil.copyfile(cand, tampered)
    t_infos, t_payload = mhooks._read_package(tampered)
    t_parts = mhooks._sheet_parts(t_payload)
    sst_root = etree.fromstring(t_payload["xl/sharedStrings.xml"], mhooks._XML)
    sis = sst_root.findall(f"{{{mhooks._SML}}}si")
    # Replace si[17] with plain text (same display string "BABA Revenue", lost partial bold!)
    sis[17].clear()
    etree.SubElement(sis[17], f"{{{mhooks._SML}}}t").text = "BABA Revenue"
    t_payload["xl/sharedStrings.xml"] = etree.tostring(sst_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    # Replace D5 <is> with plain text (same display string "BABA Cloud", lost partial bold!)
    tb_root = etree.fromstring(t_payload[t_parts["Model - BABA"]], mhooks._XML)
    for c in tb_root.iter(f"{{{mhooks._SML}}}c"):
        if (c.get("r") or "").upper() == "D5":
            mhooks._apply_cached_value(c, "inlineStr", "BABA Cloud")
    t_payload[t_parts["Model - BABA"]] = etree.tostring(tb_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in t_infos:
            zout.writestr(info, t_payload[info.filename])

    chk_bad = mchk.run_checks(candidate_path=tampered, ticker="JD", baseline_path=base, coord_map=res["coord_map"])
    c14_bad_cells = {f.get("cell") for f in chk_bad["findings"] if f["check_id"] == "C14" and f["severity_class"] == "actionable"}
    assert "D4" in c14_bad_cells, chk_bad["findings"]
    assert "D5" in c14_bad_cells, chk_bad["findings"]


def test_recalc_failed_marks_affected_balance_checks_missing_evidence_and_verifies_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex R3 #4: when LibreOffice times out (recalc_failed) after a legal quarter rollover write to P4,
    C10 marks stale formula G4=SUM(O4:R4) as missing_evidence (NOT actionable), delivers cleanly through
    verify_done, while structural/formula faults (e.g. subtotal constant P6) remain actionable."""
    ws, rdir, base = _copy_fixture_to_ws(tmp_path)

    def _timeout_soffice(src: Path, out_dir: Path, timeout: int) -> Path:
        raise subprocess.TimeoutExpired(cmd=["soffice"], timeout=timeout)

    monkeypatch.setattr(recalc_mod, "soffice_calculate", create_autospec(_REAL_SOFFICE_CALC, side_effect=_timeout_soffice))
    monkeypatch.setattr(recalc_mod.shutil, "which", create_autospec(_REAL_SHUTIL_WHICH, return_value="/usr/bin/soffice"))

    # Legal rollover actuals into detail rows P4 & P5; G4=SUM(O4:R4) keeps donor cache because LO timed out!
    cm_path = rdir / "model_dump" / "coord_map.json"
    cm_path.parent.mkdir(parents=True, exist_ok=True)
    res = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "rollover_actual_cols": {"Model - JD": ["P"]},
            "cell_edits": [
                {"sheet": "Model - JD", "cell": "P1", "value": "2Q26A"},
                {"sheet": "Model - JD", "cell": "P4", "value": 2900.0, "style_role": "historical_actual"},
                {"sheet": "Model - JD", "cell": "P5", "value": 1100.0, "style_role": "historical_actual"},
            ],
            "changelog_entry": {"version": "v02", "date": "2026-11-27", "baseline": base.name, "summary": "2Q26A rollover under LO timeout"},
        },
        workspace_dir=ws,
        coord_map_out=cm_path,
        recalc_mod_override=recalc_mod,
    )
    assert res["recalc_status"] == "recalc_failed"
    cand = Path(res["candidate_path"])

    # Generate aligned consensus-diff bound to cand so only recalc is in failed_stages
    mcons.compute_consensus_diff(
        model_path=cand,
        ticker="JD",
        research_dir=rdir,
        consensus_data={
            "source_type": "Market Consensus",
            "as_of_date": "2026-11-27",
            "items": [{"period": "2026E", "metric": "Total Revenue", "consensus": 12000.0, "rationale": "Inline"}],
        },
    )

    chk = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir,
        candidate_path=cand,
        baseline_path=base,
        coord_map_path=cm_path,
        run_id="run-recalc-timeout-01",
    )
    assert chk["counts"]["actionable"] == 0, [f for f in chk["findings"] if f["severity_class"] == "actionable"]
    assert any(
        f["check_id"] == "C10" and f["severity_class"] == "missing_evidence" and f.get("cell") == "G4"
        for f in chk["findings"]
    ), chk["findings"]
    assert "recalc" in chk["failed_stages"]
    assert chk["final_status"] == "delivered_with_open_issues"

    vd = mchk.verify_done(workspace_dir=ws, ticker="JD", research_dir=rdir, expected_run_id="run-recalc-timeout-01")
    assert vd["ok"] is True, vd
    assert vd["final_status"] == "delivered_with_open_issues"

    # Formula/structural faults (e.g. overwriting subtotal P6 with constant) STILL stay actionable under recalc_failed!
    res_bad = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "rollover_actual_cols": {"Model - JD": ["P"]},
            "cell_edits": [
                {"sheet": "Model - JD", "cell": "P1", "value": "2Q26A"},
                {"sheet": "Model - JD", "cell": "P6", "value": 4000.0},
            ],
            "changelog_entry": {"version": "v03", "date": "2026-11-27", "baseline": base.name, "summary": "Subtotal constant under LO timeout"},
        },
        workspace_dir=ws,
        recalc_mod_override=recalc_mod,
    )
    assert res_bad["recalc_status"] == "recalc_failed"
    chk_bad = mchk.run_checks(
        candidate_path=res_bad["candidate_path"],
        ticker="JD",
        baseline_path=base,
        coord_map=res_bad["coord_map"],
    )
    assert any(
        f["check_id"] == "C03" and f["severity_class"] == "actionable" and f.get("cell") == "P6"
        for f in chk_bad["findings"]
    ), chk_bad["findings"]


def test_consensus_unaligned_currency_basis_is_missing_evidence_not_actionable(tmp_path: Path) -> None:
    """Codex R3 #5: when consensus currency/basis mismatches model basis (e.g. USD vs RMB),
    diff skips threshold judgment and marks items missing_evidence; unchanged baseline stays actionable == 0."""
    ws, rdir, base = _copy_fixture_to_ws(tmp_path)

    diff_res = mcons.compute_consensus_diff(
        model_path=base,
        ticker="JD",
        research_dir=rdir,
        consensus_data={
            "source_type": "Street USD Consensus",
            "as_of_date": "2026-11-27",
            "basis": {
                "fiscal_year_end_month": 12,
                "currency": "USD",  # Model - JD is RMB MM!
                "accounting_basis": "GAAP",
                "share_basis": "ordinary",
                "reflects_latest_earnings": True,
            },
            "items": [
                {"period": "2026E", "metric": "Total Revenue", "consensus": 1650.0},  # Raw USD vs RMB 12000, NO rationale
            ],
        },
    )
    assert diff_res["basis_alignment"]["aligned"] is False
    assert diff_res["unaligned_count"] == 1
    assert diff_res["exceeds_count"] == 0
    assert diff_res["missing_rationale_count"] == 0
    assert diff_res["status"] == "missing_evidence"
    assert diff_res["items"][0]["status"] == "missing_evidence"
    assert diff_res["items"][0]["exceeds_threshold"] is False
    assert diff_res["items"][0]["missing_rationale"] is False

    # Same-hash unchanged baseline with this unaligned consensus-diff.json -> actionable == 0, C04 missing_evidence!
    chk = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir,
        candidate_path=base,
        baseline_path=base,
        run_id="run-unaligned-cons-01",
    )
    assert chk["counts"]["actionable"] == 0, chk["findings"]
    assert any(
        f["check_id"] == "C04" and f["severity_class"] == "missing_evidence"
        for f in chk["findings"]
    ), chk["findings"]
    assert chk["final_status"] == "pass"


def test_consensus_segment_metric_normalization_idempotent_double_write_and_diff_match(tmp_path: Path) -> None:
    """Codex R3 #6: metric='Cloud Revenue', segment='Cloud' normalizes identically to 'Cloud Revenue'
    and ('Revenue', segment='Cloud'); consecutive snapshot writes update in place (no duplicate rows)
    and diff matches model segment rows."""
    ws, rdir, base = _copy_fixture_to_ws(tmp_path)
    cand = ws / "model" / "China_Ecommerce_seg_r4_v02.xlsx"
    shutil.copyfile(base, cand)

    # 1. Unified normalization across both prefixed and explicit-segment inputs
    assert mcons._normalize_metric_key("Cloud Revenue", segment="Cloud") == "cloud::revenue"
    assert mcons._normalize_metric_key("Revenue", segment="Cloud") == "cloud::revenue"
    assert mcons._normalize_metric_key("Cloud Revenue") == "cloud::revenue"
    assert mcons._normalize_metric_key("Retail Revenue", segment="Retail") == "retail::revenue"
    assert mcons._normalize_metric_key("Cloud Segment Operating Income", segment="Cloud") == "cloud::operating_income"
    assert mcons._normalize_metric_key("Total Revenue", segment="Group") == "total_revenue"

    # 2. Write same segment snapshot TWICE consecutively (both prefixed+segment AND segment-only!) -> zero duplicate rows
    payload1 = {
        "source_type": "Market Consensus",
        "as_of_date": "2026-11-27",
        "items": [
            {"period": "2026E", "metric": "Cloud Revenue", "segment": "Cloud", "consensus": 2100.0},
            {"period": "2026E", "metric": "Retail Revenue", "segment": "Retail", "consensus": 9900.0},
        ],
    }
    snap1 = mcons.write_consensus_snapshot(model_path=cand, consensus_data=payload1, sheet_name="vs Consensus")
    wb1 = openpyxl.load_workbook(cand, data_only=False)
    max_row_1 = wb1["vs Consensus"].max_row
    wb1.close()

    payload2 = {
        "source_type": "Market Consensus",
        "as_of_date": "2026-11-28",
        "items": [
            {"period": "2026E", "metric": "Cloud Revenue", "segment": "Cloud", "consensus": 2150.0},
            {"period": "2026E", "metric": "Revenue", "segment": "Retail", "consensus": 10050.0},
        ],
    }
    snap2 = mcons.write_consensus_snapshot(model_path=cand, consensus_data=payload2, sheet_name="vs Consensus")
    wb2 = openpyxl.load_workbook(cand, data_only=False)
    max_row_2 = wb2["vs Consensus"].max_row
    wb2.close()

    assert snap1["consensus_snapshot_cells"]["vs Consensus"] == snap2["consensus_snapshot_cells"]["vs Consensus"]
    assert max_row_2 == max_row_1, f"Duplicate rows appended on second write: {max_row_1} -> {max_row_2}"

    # 3. Consensus diff matches Model - JD's Retail Revenue row (G4) when passing metric="Retail Revenue", segment="Retail"
    diff_res = mcons.compute_consensus_diff(
        model_path=cand,
        ticker="JD",
        research_dir=rdir,
        consensus_data={
            "source_type": "Market Consensus",
            "as_of_date": "2026-11-28",
            "items": [
                {"period": "2026E", "metric": "Retail Revenue", "segment": "Retail", "consensus": 10050.0, "rationale": "GMV mix"},
            ],
        },
    )
    assert len(diff_res["items"]) == 1
    row0 = diff_res["items"][0]
    assert row0["metric_key"] == "retail::revenue"
    assert row0["house"] is not None
    assert row0["house_cell"] == "Model - JD!G4"


def test_recover_baseline_minio_inline_bitrot_and_custom_ecbsize_parts(tmp_path: Path) -> None:
    """R4 #3, Codex R3 #3 & Grok 疑似:
    - MinIO XL2 v1.3 nested msgpack inline xl.meta with 32B HighwayHash256 bitrot header is stripped before SHA-256 check.
    - MinIO part.1 with non-default EcBSize declared inside sibling xl.meta nested V2Obj strips every block's 32B header."""
    import struct as _st

    baseline_bytes = FIXTURE_XLSX.read_bytes()
    baseline_sha = hashlib.sha256(baseline_bytes).hexdigest()
    key_file = tmp_path / "backup.key"
    key_file.write_text("r4-minio-bitrot-key\n", encoding="utf-8")
    ws_id = "22222222-3333-4444-5555-666666666666"

    def _mp_bin(b: bytes) -> bytes:
        if len(b) < 256:
            return b"\xc4" + bytes([len(b)]) + b
        if len(b) < 65536:
            return b"\xc5" + _st.pack(">H", len(b)) + b
        return b"\xc6" + _st.pack(">I", len(b)) + b

    def _mp_str(s: str) -> bytes:
        enc = s.encode("utf-8")
        assert len(enc) < 32
        return bytes([0xA0 | len(enc)]) + enc

    def _encrypt_tar(tar_bytes: bytes, out_path: Path) -> None:
        subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", f"file:{key_file}", "-out", str(out_path)],
            input=tar_bytes,
            check=True,
        )

    def _fake_rclone(script: Path, day: str, fname: str, enc_p: Path) -> None:
        script.write_text(
            f"""#!/usr/bin/env bash
if [[ "$1" == "lsf" && "$2" == "--dirs-only" && "$3" == *"/weekly/" ]]; then
  echo "{day}/"
elif [[ "$1" == "lsf" && "$2" == "--files-only" && "$3" == *"/weekly/{day}/" ]]; then
  echo "{fname}"
elif [[ "$1" == "cat" ]]; then
  cat "{enc_p}"
fi
""",
            encoding="utf-8",
        )
        script.chmod(0o755)

    # 1. XL2 v1.3 nested msgpack: outer bin(xlMetaV2{Versions:[{V2Obj: bin({EcBSize:1048576, CSumAlgo:1})}]})
    #    + trailing bin(inlineData{"data-dir-uuid": [32B HighwayHash256 bitrot header + baseline_bytes]})
    inner_v2obj = b"\x82" + _mp_str("EcBSize") + b"\xce" + _st.pack(">I", 1048576) + _mp_str("CSumAlgo") + b"\x01"
    outer_meta = b"\x81" + _mp_str("Versions") + b"\x91" + b"\x81" + _mp_str("V2Obj") + _mp_bin(inner_v2obj)
    bitrot_inline = (b"\x5a" * 32) + baseline_bytes
    inline_map = b"\x81" + _mp_str("data-dir-uuid") + _mp_bin(bitrot_inline)
    xl_meta_v13 = b"XL2 " + _st.pack("<HH", 1, 3) + _mp_bin(outer_meta) + _mp_bin(inline_map)

    tar1 = io.BytesIO()
    with tarfile.open(fileobj=tar1, mode="w:gz") as tf:
        ti = tarfile.TarInfo(name="dalton-uploads/attachments/att-inline/China_Ecommerce_Model.xlsx/xl.meta")
        ti.size = len(xl_meta_v13)
        tf.addfile(ti, io.BytesIO(xl_meta_v13))
    enc1 = tmp_path / "minio-v13-inline.tar.gz.enc"
    _encrypt_tar(tar1.getvalue(), enc1)
    rc1 = tmp_path / "rc_v13.sh"
    _fake_rclone(rc1, "2026-09-25", enc1.name, enc1)

    out1 = tmp_path / "rec_v13_inline.xlsx"
    res1 = recover_lib.recover_baseline(
        workspace_id=ws_id,
        output_path=out1,
        before_first_edit_ts="2026-10-01T10:00:00+00:00",
        expected_sha256=baseline_sha,
        expected_filename="China_Ecommerce_Model.xlsx",
        rclone_remote="fakegdrive",
        rclone_drive_dir="DaltonBackup",
        rclone_bin=str(rc1),
        backup_key_file=key_file,
    )
    assert res1.found is True and "minio-xl-inline:" in (res1.source_ref or "")
    assert out1.read_bytes() == baseline_bytes

    # 2. Multi-block part.1 with non-default EcBSize=4096 declared inside sibling xl.meta V2Obj
    custom_bsize = 4096
    inner_v2_custom = b"\x82" + _mp_str("EcBSize") + b"\xcd" + _st.pack(">H", custom_bsize) + _mp_str("CSumAlgo") + b"\x01"
    outer_custom = b"\x81" + _mp_str("Versions") + b"\x91" + b"\x81" + _mp_str("V2Obj") + _mp_bin(inner_v2_custom)
    xl_meta_custom = b"XL2 " + _st.pack("<HH", 1, 3) + _mp_bin(outer_custom)
    chunked_part1 = bytearray()
    for off in range(0, len(baseline_bytes), custom_bsize):
        chunked_part1.extend(b"\x99" * 32)
        chunked_part1.extend(baseline_bytes[off : off + custom_bsize])

    tar2 = io.BytesIO()
    with tarfile.open(fileobj=tar2, mode="w:gz") as tf:
        ti_m = tarfile.TarInfo(name="dalton-uploads/uploads/up-custom/China_Ecommerce_Model.xlsx/xl.meta")
        ti_m.size = len(xl_meta_custom)
        tf.addfile(ti_m, io.BytesIO(xl_meta_custom))
        ti_p = tarfile.TarInfo(name="dalton-uploads/uploads/up-custom/China_Ecommerce_Model.xlsx/dir-uuid/part.1")
        ti_p.size = len(chunked_part1)
        tf.addfile(ti_p, io.BytesIO(bytes(chunked_part1)))
    enc2 = tmp_path / "minio-custom-bsize.tar.gz.enc"
    _encrypt_tar(tar2.getvalue(), enc2)
    rc2 = tmp_path / "rc_custom.sh"
    _fake_rclone(rc2, "2026-09-25", enc2.name, enc2)

    out2 = tmp_path / "rec_custom_part.xlsx"
    res2 = recover_lib.recover_baseline(
        workspace_id=ws_id,
        output_path=out2,
        before_first_edit_ts="2026-10-01T10:00:00+00:00",
        expected_sha256=baseline_sha,
        expected_filename="China_Ecommerce_Model.xlsx",
        rclone_remote="fakegdrive",
        rclone_drive_dir="DaltonBackup",
        rclone_bin=str(rc2),
        backup_key_file=key_file,
    )
    assert res2.found is True and "minio-part:" in (res2.source_ref or "")
    assert out2.read_bytes() == baseline_bytes


MINIO_REAL_FIXTURE_TAR = REPO_ROOT / "tests" / "fixtures" / "dalton" / "minio_single_drive_fixture.tar.gz"
MINIO_REAL_FIXTURE_MANIFEST = REPO_ROOT / "tests" / "fixtures" / "dalton" / "minio_single_drive_fixture.json"


@pytest.mark.skipif(
    not (MINIO_REAL_FIXTURE_TAR.is_file() and MINIO_REAL_FIXTURE_MANIFEST.is_file()),
    reason="Real MinIO single-drive fixture not generated yet (run scripts/dalton/ops/prod/generate_minio_inline_fixture.sh outside sandbox)",
)
def test_recover_baseline_real_minio_single_drive_fixture(tmp_path: Path) -> None:
    """R4 #3: verify recovery against a real single-drive MinIO tar.gz generated with production MinIO image."""
    manifest = json.loads(MINIO_REAL_FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    tar_bytes = MINIO_REAL_FIXTURE_TAR.read_bytes()

    # 1. Small inline object inside real xl.meta (with real MinIO bitrot header)
    inline_sha = manifest["inline_object"]["sha256"]
    got_inline, ref_inline = recover_lib._recover_from_minio_tar_bytes(
        tar_bytes,
        expected_sha256=inline_sha,
        target_filename="China_Ecommerce_Model.xlsx",
    )
    assert got_inline is not None and "minio-xl-inline:" in (ref_inline or "")
    assert hashlib.sha256(got_inline).hexdigest() == inline_sha

    # 2. Larger >128KiB object in real <dataDir>/part.1 (with real MinIO HighwayHash256 bitrot header)
    part_sha = manifest["part_object"]["sha256"]
    got_part, ref_part = recover_lib._recover_from_minio_tar_bytes(
        tar_bytes,
        expected_sha256=part_sha,
        target_filename="China_Ecommerce_Large.xlsx",
    )
    assert got_part is not None and "minio-part:" in (ref_part or "")
    assert hashlib.sha256(got_part).hexdigest() == part_sha


@pytest.mark.skipif(
    _resolve_safe_test_pg_dsn() is None or shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
    reason="Requires DALTON_TEST_PG_DSN (*_test) plus host pg_dump and pg_restore binaries",
)
def test_recover_baseline_real_pg_dump_fc_roundtrip(tmp_path: Path) -> None:
    """Grok R3 #1 & R4 #4: real pg_dump -Fc -> encrypted stream -> pg_restore recovery round-trip on *_test DB."""
    import psycopg

    dsn = _resolve_safe_test_pg_dsn()
    assert dsn is not None
    schema = f"dalton_p0a_pgdmp_{uuid.uuid4().hex[:10]}"
    baseline_bytes = FIXTURE_XLSX.read_bytes()
    baseline_sha = hashlib.sha256(baseline_bytes).hexdigest()
    att_id = str(uuid.uuid4())

    conn = psycopg.connect(dsn, autocommit=True)
    schema_created = False
    try:
        _assert_live_pg_is_test_db(conn)
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema};")
            schema_created = True
            cur.execute(
                f"""
                CREATE TABLE {schema}.chat_attachments (
                    attachment_id uuid PRIMARY KEY, user_id text, name text, size bigint, content bytea
                );
                INSERT INTO {schema}.chat_attachments VALUES (%s::uuid, 'sender-u3', 'China_Ecommerce_Model.xlsx', %s, %s);
                """,
                (att_id, len(baseline_bytes), baseline_bytes),
            )

        dump_res = subprocess.run(
            ["pg_dump", "-Fc", "-t", f"{schema}.chat_attachments", dsn],
            capture_output=True,
            check=True,
        )
        assert dump_res.stdout.startswith(b"PGDMP")

        key_file = tmp_path / "backup.key"
        key_file.write_text("pgdmp-roundtrip-key\n", encoding="utf-8")
        enc_path = tmp_path / "dalton-pg-roundtrip.dump.enc"
        subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", f"file:{key_file}", "-out", str(enc_path)],
            input=dump_res.stdout,
            check=True,
        )
        blob, member = recover_lib.recover_from_encrypted_db_backup_stream(
            ["cat", str(enc_path)],
            key_file,
            expected_sha256=baseline_sha,
            pg_restore_compose_service=None,
        )
        assert blob == baseline_bytes and member is not None
    finally:
        if schema_created:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE;")
        conn.close()


# ---------------------------------------------------------------------------
# 12. Round-5 targeted tests (3 narrowed fixes)
# ---------------------------------------------------------------------------


def test_default_template_build_populates_formula_caches_and_recalc_failure_enables_full_calc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5 #1:
    - Building from default_model_template.xlsx writes valid LO results into affected template formulas
      (e.g. Model!O6/G6) even though template donor <v> is empty.
    - Incomplete recalc (unmatched target formulas) is NEVER reported as success.
    - Recalc timeout / no-output / incomplete turns ON fullCalcOnLoad=1, delivers as
      delivered_with_open_issues with 'recalc' in failed_stages, and writes
      '公式结果需在 Excel 中打开后重算' into remaining-issues.md.
    - Skipping recalc on a default-template build cannot pass with empty formula results."""
    ws = tmp_path / "ws_tpl"
    (ws / "model").mkdir(parents=True, exist_ok=True)
    rdir_ok = ws / "research" / "2026-11-27-build-ok"
    rdir_ok.mkdir(parents=True, exist_ok=True)
    tpl_copy = tmp_path / "default_model_template.xlsx"
    shutil.copyfile(BUNDLED_TEMPLATE_XLSX, tpl_copy)

    # 1. Working recalc: populate ALL formula cells in the fake LO output with real numeric / string caches
    def _fake_soffice_tpl_ok(src: Path, out_dir: Path, timeout: int) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        produced = out_dir / (src.stem + ".xlsx")
        shutil.copyfile(src, produced)
        infos_p, payload_p = mhooks._read_package(produced)
        for sh_n, part_p in mhooks._sheet_parts(payload_p).items():
            root_p = etree.fromstring(payload_p[part_p], mhooks._XML)
            for c_el in root_p.iter(f"{{{mhooks._SML}}}c"):
                if c_el.find(f"{{{mhooks._SML}}}f") is None:
                    continue
                ref_u = (c_el.get("r") or "").upper()
                if sh_n == "Model" and ref_u == "O6":
                    mhooks._apply_cached_value(c_el, None, "2500")
                elif sh_n == "Model" and ref_u == "G4":
                    mhooks._apply_cached_value(c_el, None, "2000")
                elif sh_n == "Model" and ref_u == "G5":
                    mhooks._apply_cached_value(c_el, None, "500")
                elif sh_n == "Model" and ref_u == "G6":
                    mhooks._apply_cached_value(c_el, None, "2500")
                elif sh_n == "vs Consensus" and ref_u.startswith("E"):
                    mhooks._apply_cached_value(c_el, "str", "")
                else:
                    mhooks._apply_cached_value(c_el, None, "0")
            payload_p[part_p] = etree.tostring(root_p, xml_declaration=True, encoding="UTF-8", standalone=True)
        with zipfile.ZipFile(produced, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info in infos_p:
                zout.writestr(info, payload_p[info.filename], compress_type=info.compress_type)
        return produced

    monkeypatch.setattr(recalc_mod, "soffice_calculate", create_autospec(_REAL_SOFFICE_CALC, side_effect=_fake_soffice_tpl_ok))
    monkeypatch.setattr(recalc_mod.shutil, "which", create_autospec(_REAL_SHUTIL_WHICH, return_value="/usr/bin/soffice"))

    plan_tpl = {
        "cell_edits": [
            {"sheet": "Model", "cell": "O4", "value": 2000.0},
            {"sheet": "Model", "cell": "O5", "value": 500.0},
        ],
        "changelog_entry": {
            "version": "v01",
            "date": "2026-11-27",
            "baseline": "default_model_template.xlsx",
            "summary": "Initial build from default template",
        },
    }
    cm_ok_p = rdir_ok / "model_dump" / "coord_map.json"
    res_ok = medit.apply_edit_plan(
        baseline_path=tpl_copy,
        ticker="DEMO",
        plan=plan_tpl,
        workspace_dir=ws,
        coord_map_out=cm_ok_p,
        recalc_mod_override=recalc_mod,
        mode="build",
    )
    assert res_ok["recalc_status"] == "success"
    assert res_ok["recalc_result"]["values_written"] > 0
    assert res_ok["recalc_result"]["unmatched"] == 0
    cand_ok = Path(res_ok["candidate_path"])
    model_cache_ok = _xml_cached_map(cand_ok, "Model")
    assert model_cache_ok["O6"][1] == "2500"
    assert model_cache_ok["G6"][1] == "2500"
    assert mfp.build_fingerprint(cand_ok, ticker="DEMO")["calc_properties"]["full_calc_on_load"] is False

    # 2. Incomplete recalc (LO leaves Model!G6 empty) -> NEVER reported as success; enables fullCalcOnLoad=1!
    def _fake_soffice_incomplete(src: Path, out_dir: Path, timeout: int) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        produced = out_dir / (src.stem + ".xlsx")
        shutil.copyfile(src, produced)
        _inject_xml_v_values(produced, {"Model": {"O6": (None, "2500")}})
        return produced

    monkeypatch.setattr(recalc_mod, "soffice_calculate", create_autospec(_REAL_SOFFICE_CALC, side_effect=_fake_soffice_incomplete))
    rdir_inc = ws / "research" / "2026-11-27-build-inc"
    cm_inc_p = rdir_inc / "model_dump" / "coord_map.json"
    res_inc = medit.apply_edit_plan(
        baseline_path=tpl_copy,
        ticker="DEMO",
        plan=plan_tpl,
        output_path=ws / "model" / "DEMO-model-v02.xlsx",
        workspace_dir=ws,
        coord_map_out=cm_inc_p,
        recalc_mod_override=recalc_mod,
        mode="build",
    )
    assert res_inc["recalc_status"] == "incomplete"
    assert res_inc["recalc_result"]["unmatched"] > 0
    assert any(u["cell"] == "G6" for u in res_inc["recalc_result"]["unmatched_cells"])
    cand_inc = Path(res_inc["candidate_path"])
    assert mfp.build_fingerprint(cand_inc, ticker="DEMO")["calc_properties"]["full_calc_on_load"] is True

    # 3. Timeout & No-output on default template build -> fullCalcOnLoad=1, delivered_with_open_issues,
    #    failed_stages includes 'recalc', remaining-issues.md states '公式结果需在 Excel 中打开后重算', verify_done ok!
    for idx_f, (label_f, stub_fn) in enumerate(
        (
            ("timeout", lambda src, out_dir, timeout: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd=["soffice"], timeout=timeout))),
            ("no_output", lambda src, out_dir, timeout: out_dir / "missing.xlsx"),
        ),
        start=3,
    ):
        monkeypatch.setattr(recalc_mod, "soffice_calculate", create_autospec(_REAL_SOFFICE_CALC, side_effect=stub_fn))
        rdir_f = ws / "research" / f"2026-11-27-build-{label_f}"
        cm_f_p = rdir_f / "model_dump" / "coord_map.json"
        res_f = medit.apply_edit_plan(
            baseline_path=tpl_copy,
            ticker="DEMO",
            plan=plan_tpl,
            output_path=ws / "model" / f"DEMO-model-v0{idx_f}.xlsx",
            workspace_dir=ws,
            coord_map_out=cm_f_p,
            recalc_mod_override=recalc_mod,
            mode="build",
        )
        assert res_f["recalc_status"] == "recalc_failed"
        cand_f = Path(res_f["candidate_path"])
        fp_f = mfp.build_fingerprint(cand_f, ticker="DEMO")
        assert fp_f["calc_properties"]["full_calc_on_load"] is True
        assert res_f["candidate_sha256"] == fp_f["source_sha256"]

        chk_f = mchk.write_check_and_ledger(
            workspace_dir=ws,
            ticker="DEMO",
            research_dir=rdir_f,
            candidate_path=cand_f,
            baseline_path=tpl_copy,
            coord_map_path=cm_f_p,
            run_id=f"run-tpl-{label_f}",
            mode="build",
        )
        assert "recalc" in chk_f["failed_stages"]
        assert chk_f["final_status"] == "delivered_with_open_issues"
        assert not any(
            f["check_id"] == "C13" and f["severity_class"] == "actionable"
            for f in chk_f["findings"]
        ), chk_f["findings"]
        rem_md = (rdir_f / "model_dump" / "remaining-issues.md").read_text(encoding="utf-8")
        assert "公式结果需在 Excel 中打开后重算" in rem_md
        vd_f = mchk.verify_done(
            workspace_dir=ws,
            ticker="DEMO",
            research_dir=rdir_f,
            expected_run_id=f"run-tpl-{label_f}",
        )
        assert vd_f["ok"] is True, vd_f
        assert vd_f["final_status"] == "delivered_with_open_issues"

    # 4. R6 #2: Skipping recalc on default-template / build mode MUST fail immediately and exit non-zero!
    out_skip = ws / "model" / "DEMO-model-v05.xlsx"
    with pytest.raises(ValueError, match="--skip-recalc"):
        medit.apply_edit_plan(
            baseline_path=tpl_copy,
            ticker="DEMO",
            plan=plan_tpl,
            output_path=out_skip,
            skip_recalc=True,
            mode="build",
        )
    assert not out_skip.exists()

    # Even with mode="auto", empty-cache default_model_template.xlsx refuses --skip-recalc!
    with pytest.raises(ValueError, match="--skip-recalc"):
        medit.apply_edit_plan(
            baseline_path=tpl_copy,
            ticker="DEMO",
            plan=plan_tpl,
            output_path=out_skip,
            skip_recalc=True,
            mode="auto",
        )
    assert not out_skip.exists()

    plan_json_p = tmp_path / "tpl_plan.json"
    plan_json_p.write_text(json.dumps(plan_tpl), encoding="utf-8")
    rc = medit.main(
        [
            "apply",
            "--baseline",
            str(tpl_copy),
            "--ticker",
            "DEMO",
            "--plan",
            str(plan_json_p),
            "--out",
            str(out_skip),
            "--skip-recalc",
            "--mode",
            "build",
        ]
    )
    assert rc != 0
    assert not out_skip.exists()


def test_editing_jd_preserves_baba_raw_xml_byte_for_byte_and_c14_catches_raw_xml_tamper(
    tmp_path: Path,
) -> None:
    """R5 #2:
    - Editing ONLY JD (both via model_edit and model_consensus.write_consensus_snapshot) leaves
      Model - BABA, Subs - BABA, and BABA>> worksheet XML BYTE-FOR-BYTE identical to the
      post-insert donor, including cell 'ph' attribute, raw numeric '<v>90000.00</v>', and '<ignoredErrors>'.
    - Deliberately mutating BABA (dropping 'ph', dropping '<ignoredErrors>', or altering raw '<v>' text
      while keeping identical numeric/string cell values) is caught by C14 as actionable."""
    ws, _, base = _copy_fixture_to_ws(tmp_path)

    # Inject Excel-native XML artifacts onto Model - BABA that openpyxl drops on whole-workbook save:
    # 1. ph="1" attribute on Model - BABA!D4
    # 2. non-canonical numeric formatting <v>90000.00</v> on Model - BABA!E4
    # 3. <ignoredErrors> element at worksheet level on Model - BABA
    infos_b, payload_b = mhooks._read_package(base)
    parts_b = mhooks._sheet_parts(payload_b)
    baba_part_b = parts_b["Model - BABA"]
    baba_root = etree.fromstring(payload_b[baba_part_b], mhooks._XML)
    for c_el in baba_root.iter(f"{{{mhooks._SML}}}c"):
        ref = (c_el.get("r") or "").upper()
        if ref == "D4":
            c_el.set("ph", "1")
        elif ref == "E4":
            v_el = c_el.find(f"{{{mhooks._SML}}}v")
            assert v_el is not None
            v_el.text = "90000.00"
    ign_el = etree.SubElement(baba_root, f"{{{mhooks._SML}}}ignoredErrors")
    etree.SubElement(ign_el, f"{{{mhooks._SML}}}ignoredError", sqref="E4:G4", numberStoredAsText="1")
    payload_b[baba_part_b] = etree.tostring(baba_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(base, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos_b:
            zout.writestr(info, payload_b[info.filename], compress_type=info.compress_type)

    # 1. Edit ONLY JD (with column insert on Model - JD + cell edits + changelog)
    res_ed = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "ops": [{"op": "insert_columns", "sheet": "Model - JD", "at": "P", "count": 1, "copy_style_from": "O"}],
            "added_period_cols": {"Model - JD": ["P"]},
            "cell_edits": [
                {"sheet": "Model - JD", "cell": "P1", "value": "1Q26 Adj"},
                {"sheet": "Model - JD", "cell": "Q25", "value": 0.115},
            ],
            "changelog_entry": {
                "version": "v02",
                "date": "2026-11-27",
                "baseline": base.name,
                "summary": "Edit JD only; BABA XML must stay byte-identical",
            },
        },
        workspace_dir=ws,
        skip_recalc=True,
    )
    cand = Path(res_ed["candidate_path"])
    donor = Path(res_ed["cache_donor_path"])

    # Assert BYTE-FOR-BYTE identity of all non-target BABA sheets against post-insert donor!
    _, donor_pay = mhooks._read_package(donor)
    _, cand_pay = mhooks._read_package(cand)
    donor_parts = mhooks._sheet_parts(donor_pay)
    cand_parts = mhooks._sheet_parts(cand_pay)
    for nt_sh in ("BABA>>", "Model - BABA", "Subs - BABA"):
        assert cand_pay[cand_parts[nt_sh]] == donor_pay[donor_parts[nt_sh]], f"{nt_sh} XML changed!"
    assert b'ph="1"' in cand_pay[cand_parts["Model - BABA"]]
    assert b"ignoredErrors" in cand_pay[cand_parts["Model - BABA"]]
    assert b">90000.00<" in cand_pay[cand_parts["Model - BABA"]]

    # Also verify write_consensus_snapshot keeps all non-consensus sheets (incl. Model - BABA) byte-identical!
    mcons.write_consensus_snapshot(
        model_path=cand,
        consensus_data={
            "source_type": "Market Consensus",
            "as_of_date": "2026-11-27",
            "items": [{"period": "2026E", "metric": "Total Revenue", "consensus": 12000.0}],
        },
        sheet_name="vs Consensus",
    )
    _, cand_after_cons = mhooks._read_package(cand)
    cand_parts_after = mhooks._sheet_parts(cand_after_cons)
    for nt_sh in ("BABA>>", "Model - BABA", "Subs - BABA"):
        assert cand_after_cons[cand_parts_after[nt_sh]] == donor_pay[donor_parts[nt_sh]], (
            f"{nt_sh} XML changed after write_consensus_snapshot!"
        )

    # C14 MUST pass with zero actionable findings on the clean candidate!
    chk_ok = mchk.run_checks(
        candidate_path=cand,
        ticker="JD",
        baseline_path=base,
        coord_map=res_ed["coord_map"],
    )
    c14_ok = [f for f in chk_ok["findings"] if f["check_id"] == "C14" and f["severity_class"] == "actionable"]
    assert c14_ok == [], c14_ok

    # 2. Deliberately tamper BABA raw XML in 3 subtle ways that preserve identical cell values -> C14 MUST catch ALL!
    # 2a. Remove <ignoredErrors> (all cells unchanged!)
    t_ign = ws / "model" / "China_Ecommerce_tamper_ign_v03.xlsx"
    shutil.copyfile(cand, t_ign)
    inf_i, pay_i = mhooks._read_package(t_ign)
    bp_i = mhooks._sheet_parts(pay_i)["Model - BABA"]
    r_i = etree.fromstring(pay_i[bp_i], mhooks._XML)
    for el in list(r_i.findall(f"{{{mhooks._SML}}}ignoredErrors")):
        r_i.remove(el)
    pay_i[bp_i] = etree.tostring(r_i, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(t_ign, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in inf_i:
            zout.writestr(info, pay_i[info.filename], compress_type=info.compress_type)
    chk_ign = mchk.run_checks(candidate_path=t_ign, ticker="JD", baseline_path=base, coord_map=res_ed["coord_map"])
    c14_ign = [f for f in chk_ign["findings"] if f["check_id"] == "C14" and f["severity_class"] == "actionable"]
    assert any(f.get("sheet") == "Model - BABA" and "ignoredErrors" in f["message"] for f in c14_ign), c14_ign

    # 2b. Remove ph="1" from Model - BABA!D4 and rewrite <v>90000.00</v> -> <v>90000</v> on E4 (same cell values!)
    t_cell = ws / "model" / "China_Ecommerce_tamper_cell_v04.xlsx"
    shutil.copyfile(cand, t_cell)
    inf_c, pay_c = mhooks._read_package(t_cell)
    bp_c = mhooks._sheet_parts(pay_c)["Model - BABA"]
    r_c = etree.fromstring(pay_c[bp_c], mhooks._XML)
    for c_el in r_c.iter(f"{{{mhooks._SML}}}c"):
        ref = (c_el.get("r") or "").upper()
        if ref == "D4" and "ph" in c_el.attrib:
            del c_el.attrib["ph"]
        elif ref == "E4":
            v_el = c_el.find(f"{{{mhooks._SML}}}v")
            assert v_el is not None
            v_el.text = "90000"
    pay_c[bp_c] = etree.tostring(r_c, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(t_cell, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in inf_c:
            zout.writestr(info, pay_c[info.filename], compress_type=info.compress_type)
    chk_cell = mchk.run_checks(candidate_path=t_cell, ticker="JD", baseline_path=base, coord_map=res_ed["coord_map"])
    c14_cell_coords = {
        f.get("cell") for f in chk_cell["findings"]
        if f["check_id"] == "C14" and f["severity_class"] == "actionable" and f.get("sheet") == "Model - BABA"
    }
    assert "D4" in c14_cell_coords, chk_cell["findings"]
    assert "E4" in c14_cell_coords, chk_cell["findings"]


def test_unchanged_model_never_triggers_c04_actionable_and_unit_scale_alignment(
    tmp_path: Path,
) -> None:
    """R5 #3 & Codex R4 #3:
    - Unchanged baseline model with above-threshold consensus diff (even with no rationale) has actionable == 0
      because C04 only flags round-edited/affected House forecasts.
    - Monetary unit scale is aligned before comparison: 3Q26E House 3134.8 RMB MM vs Consensus 3134800000 RMB
      with unit='RMB' aligns to 0% diff; without declared unit_scale, 10^6 magnitude gap is missing_evidence
      (never actionable).
    - Editing a driver (Q25) that affects 2026E Total Revenue (G6) WITH an aligned >5% diff and no rationale
      STILL triggers C04 actionable."""
    ws, rdir_same, base = _copy_fixture_to_ws(tmp_path)
    base_fp = mfp.build_fingerprint(base, ticker="JD")
    q3_house = float(base_fp["sheets"]["Model - JD"]["cells"]["Q6"]["cached_value"])  # RMB MM

    # 1. Unchanged model + consensus diff with explicit RMB raw unit (10^6 x House MM) -> aligned 0% diff, actionable == 0
    rdir_scaled = ws / "research" / "2026-11-27-cons-scaled"
    diff_scaled = mcons.compute_consensus_diff(
        model_path=base,
        ticker="JD",
        research_dir=rdir_scaled,
        consensus_data={
            "source_type": "Wind Consensus",
            "as_of_date": "2026-11-27",
            "basis": {"currency": "RMB", "unit": "RMB"},
            "items": [{"period": "3Q26E", "metric": "Total Revenue", "consensus": q3_house * 1e6}],
        },
    )
    assert diff_scaled["status"] == "ok"
    assert diff_scaled["basis_alignment"]["aligned"] is True
    assert diff_scaled["exceeds_count"] == 0
    assert diff_scaled["missing_rationale_count"] == 0
    assert abs(float(diff_scaled["items"][0]["diff_pct"])) < 1e-9
    chk_scaled = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir_scaled,
        candidate_path=base,
        baseline_path=base,
        run_id="run-c04-scaled",
    )
    assert chk_scaled["counts"]["actionable"] == 0, chk_scaled["findings"]
    assert chk_scaled["final_status"] == "pass"

    # 2. Unchanged model + raw 10^6 consensus WITHOUT declared unit_scale -> missing_evidence, actionable == 0
    rdir_unspec = ws / "research" / "2026-11-27-cons-unspec"
    diff_unspec = mcons.compute_consensus_diff(
        model_path=base,
        ticker="JD",
        research_dir=rdir_unspec,
        consensus_data={
            "source_type": "Unlabelled Consensus",
            "as_of_date": "2026-11-27",
            "basis": {"currency": "RMB"},
            "items": [{"period": "3Q26E", "metric": "Total Revenue", "consensus": q3_house * 1e6}],
        },
    )
    assert diff_unspec["status"] == "missing_evidence"
    assert diff_unspec["unaligned_count"] == 1
    assert diff_unspec["exceeds_count"] == 0
    assert diff_unspec["missing_rationale_count"] == 0
    chk_unspec = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir_unspec,
        candidate_path=base,
        baseline_path=base,
        run_id="run-c04-unspec",
    )
    assert chk_unspec["counts"]["actionable"] == 0, chk_unspec["findings"]
    assert any(f["check_id"] == "C04" and f["severity_class"] == "missing_evidence" for f in chk_unspec["findings"])

    # 3. Unchanged model + same-scale >5% gap with NO rationale (3Q26E has no sheet rationale) -> baseline_legacy, actionable == 0!
    diff_gap = mcons.compute_consensus_diff(
        model_path=base,
        ticker="JD",
        research_dir=rdir_same,
        consensus_data={
            "source_type": "Market Consensus",
            "as_of_date": "2026-11-27",
            "basis": {"currency": "RMB", "unit": "RMB MM"},
            "items": [{"period": "3Q26E", "metric": "Total Revenue", "consensus": 2000.0}],  # >30% off, NO rationale
        },
    )
    assert diff_gap["missing_rationale_count"] == 1
    chk_unch = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir_same,
        candidate_path=base,
        baseline_path=base,
        run_id="run-c04-unchanged",
    )
    assert chk_unch["counts"]["actionable"] == 0, chk_unch["findings"]
    assert any(f["check_id"] == "C04" and f["severity_class"] == "baseline_legacy" for f in chk_unch["findings"])
    assert chk_unch["final_status"] == "pass"

    # 4. Round-edited model where driver Q25 feeds Q6 (3Q26E Total Revenue) + aligned >5% gap without rationale
    #    MUST STILL trigger C04 actionable!
    rdir_ed = ws / "research" / "2026-11-27-cons-edited"
    cm_ed_p = rdir_ed / "model_dump" / "coord_map.json"
    res_ed = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "cell_edits": [{"sheet": "Model - JD", "cell": "Q25", "value": 0.15}],
            "changelog_entry": {"version": "v02", "date": "2026-11-27", "baseline": base.name, "summary": "Raise 3Q26E growth"},
        },
        workspace_dir=ws,
        coord_map_out=cm_ed_p,
        skip_recalc=True,
    )
    mcons.compute_consensus_diff(
        model_path=res_ed["candidate_path"],
        ticker="JD",
        research_dir=rdir_ed,
        consensus_data={
            "source_type": "Market Consensus",
            "as_of_date": "2026-11-27",
            "basis": {"currency": "RMB", "unit": "RMB MM"},
            "items": [{"period": "3Q26E", "metric": "Total Revenue", "consensus": 2000.0}],  # >30% off, NO rationale
        },
    )
    chk_ed = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir_ed,
        candidate_path=res_ed["candidate_path"],
        baseline_path=base,
        coord_map_path=cm_ed_p,
        run_id="run-c04-edited",
    )
    assert any(
        f["check_id"] == "C04" and f["severity_class"] == "actionable"
        for f in chk_ed["findings"]
    ), chk_ed["findings"]


def test_multi_company_charts_preserve_series_on_jd_edit_and_check_catches_misaligned_rels(
    tmp_path: Path,
) -> None:
    """R6 #1 (Codex high):
    - Multi-company workbook with charts on both Model - JD and Model - BABA where donor drawing/chart
      numbering differs from openpyxl's sheet-order numbering (JD -> rId2 -> drawing2/chart2, BABA -> rId1 -> drawing1/chart1).
    - Editing ONLY 1 cell on Model - JD leaves EVERY chart's referenced data range identical to baseline,
      preserves donor's worksheet .rels / drawings / charts verbatim, and remaps <drawing r:id="rId2"/> cleanly.
    - Importing a genuinely NEW chart on a new sheet allocates fresh non-colliding paths (drawing3/chart3)
      without clobbering donor drawings/charts.
    - Deliberately misaligning relationships (JD -> BABA's drawing1, BABA -> JD's drawing2, or dangling rId)
      is caught by model_check as actionable."""
    from openpyxl.chart import BarChart, Reference

    ws, _, base = _copy_fixture_to_ws(tmp_path)
    orig_base = tmp_path / "orig_base_for_charts.xlsx"
    shutil.copyfile(base, orig_base)

    # 1. Attach charts to both Model - JD and Model - BABA using openpyxl, restore donor caches, then swap donor drawing/chart
    #    part paths & add a non-drawing rId1 on Model - JD so donor has:
    #    - Model - JD   -> rId1 (printerSettings1.bin), rId2 -> ../drawings/drawing2.xml -> chart2.xml ('Model - JD'!$E$4:$G$4)
    #    - Model - BABA -> rId1 -> ../drawings/drawing1.xml -> chart1.xml ('Model - BABA'!$E$4:$G$4)
    wb = openpyxl.load_workbook(base, data_only=False)
    ws_jd = wb["Model - JD"]
    ch_jd = BarChart()
    ch_jd.title = "JD Revenue"
    ch_jd.add_data(Reference(ws_jd, min_col=5, min_row=4, max_col=7, max_row=4), from_rows=True)
    ws_jd.add_chart(ch_jd, "A52")

    ws_baba = wb["Model - BABA"]
    ch_baba = BarChart()
    ch_baba.title = "BABA Revenue"
    ch_baba.add_data(Reference(ws_baba, min_col=5, min_row=4, max_col=7, max_row=4), from_rows=True)
    ws_baba.add_chart(ch_baba, "A15")
    wb.save(base)
    wb.close()
    mhooks.restore_donor_cache(base, {"cache_donor_path": str(orig_base), "touched_cells": {"Model - JD": ["A1"]}})

    # Rewire donor ZIP so BABA owns drawing1/chart1 and JD owns drawing2/chart2 under rId2 (with rId1=printerSettings)
    infos_b, pay_b = mhooks._read_package(base)
    parts_b = mhooks._sheet_parts(pay_b)
    jd_part = parts_b["Model - JD"]
    baba_part = parts_b["Model - BABA"]
    jd_rels_p = mhooks._worksheet_rels_path(jd_part)
    baba_rels_p = mhooks._worksheet_rels_path(baba_part)

    # Swap drawing1 <-> drawing2 and chart1 <-> chart2 contents so drawing1/chart1 belong to BABA and drawing2/chart2 to JD
    pay_b["xl/drawings/drawing1.xml"], pay_b["xl/drawings/drawing2.xml"] = (
        pay_b["xl/drawings/drawing2.xml"],
        pay_b["xl/drawings/drawing1.xml"],
    )
    pay_b["xl/charts/chart1.xml"], pay_b["xl/charts/chart2.xml"] = (
        pay_b["xl/charts/chart2.xml"],
        pay_b["xl/charts/chart1.xml"],
    )
    # Point drawing1 -> chart1 (BABA) and drawing2 -> chart2 (JD)
    pay_b["xl/drawings/_rels/drawing1.xml.rels"] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart1.xml"/>'
        b"</Relationships>"
    )
    pay_b["xl/drawings/_rels/drawing2.xml.rels"] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart2.xml"/>'
        b"</Relationships>"
    )
    # Add dummy printerSettings1.bin so JD's drawing is rId2 while openpyxl will renumber it to rId1 on save!
    zi_ps = zipfile.ZipInfo("xl/printerSettings/printerSettings1.bin")
    zi_ps.compress_type = zipfile.ZIP_DEFLATED
    infos_b.append(zi_ps)
    pay_b["xl/printerSettings/printerSettings1.bin"] = b"PS"
    pay_b[jd_rels_p] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/printerSettings" Target="../printerSettings/printerSettings1.bin"/>'
        b'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing2.xml"/>'
        b"</Relationships>"
    )
    pay_b[baba_rels_p] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/>'
        b"</Relationships>"
    )
    jd_sh_root = etree.fromstring(pay_b[jd_part], mhooks._XML)
    for dr_el in jd_sh_root.findall(f"{{{mhooks._SML}}}drawing"):
        dr_el.set(mhooks._REL_ID, "rId2")
    pay_b[jd_part] = etree.tostring(jd_sh_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(base, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos_b:
            zout.writestr(info, pay_b[info.filename], compress_type=info.compress_type)

    base_bindings = mhooks.extract_sheet_chart_bindings(pay_b)
    assert base_bindings["Model - JD"]["charts"][0]["formulas"] == ["'Model - JD'!$E$4:$G$4"]
    assert base_bindings["Model - BABA"]["charts"][0]["formulas"] == ["'Model - BABA'!$E$4:$G$4"]
    assert base_bindings["Model - JD"]["drawings"] == ["xl/drawings/drawing2.xml"]
    assert base_bindings["Model - BABA"]["drawings"] == ["xl/drawings/drawing1.xml"]

    # 2. Edit ONLY 1 cell on Model - JD via apply_edit_plan
    res_ed = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "cell_edits": [{"sheet": "Model - JD", "cell": "Q25", "value": 0.125}],
            "changelog_entry": {
                "version": "v02",
                "date": "2026-11-27",
                "baseline": base.name,
                "summary": "Edit JD single cell with charts present",
            },
        },
        workspace_dir=ws,
        skip_recalc=True,
    )
    cand = Path(res_ed["candidate_path"])
    _, cand_pay = mhooks._read_package(cand)
    cand_bindings = mhooks.extract_sheet_chart_bindings(cand_pay)

    # Every chart on every sheet references the EXACT same data range as baseline!
    assert cand_bindings["Model - JD"]["broken_refs"] == []
    assert cand_bindings["Model - BABA"]["broken_refs"] == []
    assert cand_bindings["Model - JD"]["charts"][0]["formulas"] == base_bindings["Model - JD"]["charts"][0]["formulas"]
    assert cand_bindings["Model - BABA"]["charts"][0]["formulas"] == base_bindings["Model - BABA"]["charts"][0]["formulas"]
    assert cand_bindings["Model - JD"]["drawings"] == ["xl/drawings/drawing2.xml"]
    assert cand_bindings["Model - BABA"]["drawings"] == ["xl/drawings/drawing1.xml"]
    # Donor .rels for both JD and BABA stayed byte-for-byte untouched!
    assert cand_pay[jd_rels_p] == pay_b[jd_rels_p]
    assert cand_pay[baba_rels_p] == pay_b[baba_rels_p]

    # Clean check passes with 0 actionable!
    chk_ok = mchk.run_checks(candidate_path=cand, ticker="JD", baseline_path=base, coord_map=res_ed["coord_map"])
    assert chk_ok["counts"]["actionable"] == 0, chk_ok["findings"]

    # 3. Importing a genuinely NEW sheet with a new chart allocates fresh non-colliding drawing3/chart3!
    ed_new_chart = tmp_path / "edited_new_chart.xlsx"
    wb_n = openpyxl.Workbook()
    ws_new = wb_n.active
    ws_new.title = "Chart New"
    ws_new["A1"] = 10
    ws_new["B1"] = 20
    ch_new = BarChart()
    ch_new.add_data(Reference(ws_new, min_col=1, min_row=1, max_col=2, max_row=1), from_rows=True)
    ws_new.add_chart(ch_new, "A5")
    wb_n.save(ed_new_chart)
    wb_n.close()

    merged_new = tmp_path / "merged_with_new_chart.xlsx"
    mhooks.merge_edited_sheets_into_donor(base, ed_new_chart, merged_new, modified_sheets={"Chart New"})
    _, merged_pay = mhooks._read_package(merged_new)
    merged_bindings = mhooks.extract_sheet_chart_bindings(merged_pay)
    assert merged_bindings["Model - JD"]["charts"][0]["formulas"] == ["'Model - JD'!$E$4:$G$4"]
    assert merged_bindings["Model - BABA"]["charts"][0]["formulas"] == ["'Model - BABA'!$E$4:$G$4"]
    assert merged_bindings["Chart New"]["charts"][0]["formulas"] == ["'Chart New'!$A$1:$B$1"]
    assert merged_bindings["Chart New"]["drawings"] == ["xl/drawings/drawing3.xml"]
    assert merged_bindings["Chart New"]["charts"][0]["chart_part"] == "xl/charts/chart3.xml"

    # 4a. Tamper: misalign Model - JD's rels so JD's drawing points to BABA's drawing1 -> check catches actionable!
    t_jd_swap = ws / "model" / "China_Ecommerce_chart_swap_jd.xlsx"
    shutil.copyfile(cand, t_jd_swap)
    inf_s, pay_s = mhooks._read_package(t_jd_swap)
    pay_s[jd_rels_p] = pay_s[jd_rels_p].replace(b"drawing2.xml", b"drawing1.xml")
    with zipfile.ZipFile(t_jd_swap, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in inf_s:
            zout.writestr(info, pay_s[info.filename], compress_type=info.compress_type)
    chk_jd_swap = mchk.run_checks(candidate_path=t_jd_swap, ticker="JD", baseline_path=base, coord_map=res_ed["coord_map"])
    assert any(
        f["severity_class"] == "actionable" and ("Chart data series" in f["message"] or "cross-shared" in f["message"])
        for f in chk_jd_swap["findings"]
    ), chk_jd_swap["findings"]

    # 4b. Tamper: misalign Model - BABA's rels to point to JD's drawing2 -> C14 catches actionable!
    t_baba_swap = ws / "model" / "China_Ecommerce_chart_swap_baba.xlsx"
    shutil.copyfile(cand, t_baba_swap)
    inf_bs, pay_bs = mhooks._read_package(t_baba_swap)
    pay_bs[baba_rels_p] = pay_bs[baba_rels_p].replace(b"drawing1.xml", b"drawing2.xml")
    with zipfile.ZipFile(t_baba_swap, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in inf_bs:
            zout.writestr(info, pay_bs[info.filename], compress_type=info.compress_type)
    chk_baba_swap = mchk.run_checks(candidate_path=t_baba_swap, ticker="JD", baseline_path=base, coord_map=res_ed["coord_map"])
    assert any(
        f["check_id"] == "C14" and f["severity_class"] == "actionable" and f.get("sheet") == "Model - BABA"
        for f in chk_baba_swap["findings"]
    ), chk_baba_swap["findings"]

    # 4c. Tamper: point Model - JD <drawing> to a nonexistent rId99 -> check catches broken relationship!
    t_brk = ws / "model" / "China_Ecommerce_chart_broken_rid.xlsx"
    shutil.copyfile(cand, t_brk)
    inf_k, pay_k = mhooks._read_package(t_brk)
    r_k = etree.fromstring(pay_k[jd_part], mhooks._XML)
    for dr_el in r_k.findall(f"{{{mhooks._SML}}}drawing"):
        dr_el.set(mhooks._REL_ID, "rId99")
    pay_k[jd_part] = etree.tostring(r_k, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(t_brk, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in inf_k:
            zout.writestr(info, pay_k[info.filename], compress_type=info.compress_type)
    chk_brk = mchk.run_checks(candidate_path=t_brk, ticker="JD", baseline_path=base, coord_map=res_ed["coord_map"])
    assert any(
        f["severity_class"] == "actionable" and "Broken chart/drawing relationship" in f["message"]
        for f in chk_brk["findings"]
    ), chk_brk["findings"]


def test_skip_recalc_non_template_enables_full_calc_before_report_and_report_matches_file(
    tmp_path: Path,
) -> None:
    """R6 #2 (Codex high):
    - On non-template update with --skip-recalc, model_edit enables fullCalcOnLoad=1 BEFORE hashing/reporting.
    - model_check records 'recalc' in failed_stages (delivered_with_open_issues) and remaining-issues.md
      accurately reports fullCalcOnLoad=1 enabled matching the workbook on disk.
    - If a candidate workbook has fullCalcOnLoad OFF while recalc was skipped, C13 flags actionable and
      remaining-issues.md NEVER falsely claims fullCalcOnLoad=1 is enabled."""
    ws, rdir, base = _copy_fixture_to_ws(tmp_path)
    cm_p = rdir / "model_dump" / "coord_map.json"
    res = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "cell_edits": [{"sheet": "Model - JD", "cell": "Q25", "value": 0.12}],
            "changelog_entry": {
                "version": "v02",
                "date": "2026-11-27",
                "baseline": base.name,
                "summary": "Update with skip-recalc",
            },
        },
        workspace_dir=ws,
        coord_map_out=cm_p,
        skip_recalc=True,
    )
    cand = Path(res["candidate_path"])
    assert res["recalc_status"] == "skipped"
    assert res["recalc_result"]["full_calc_on_load_enabled"] is True
    assert mfp.build_fingerprint(cand, ticker="JD")["calc_properties"]["full_calc_on_load"] is True
    assert mfp.file_sha256(cand) == res["candidate_sha256"]

    chk = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir,
        candidate_path=cand,
        baseline_path=base,
        coord_map_path=cm_p,
        run_id="run-skip-update",
    )
    assert "recalc" in chk["failed_stages"]
    assert chk["final_status"] == "delivered_with_open_issues"
    assert chk["full_calc_on_load"] is True
    assert chk["counts"]["actionable"] == 0, chk["findings"]
    rem_md = (rdir / "model_dump" / "remaining-issues.md").read_text(encoding="utf-8")
    assert "已开启 `fullCalcOnLoad=1`" in rem_md
    vd = mchk.verify_done(workspace_dir=ws, ticker="JD", research_dir=rdir, expected_run_id="run-skip-update")
    assert vd["ok"] is True, vd

    # Consistency check: if fullCalcOnLoad is actually OFF in file while recalc was skipped,
    # remaining-issues.md MUST NOT falsely claim fullCalcOnLoad=1 is enabled!
    rdir_off = ws / "research" / "2026-11-27-skip-off"
    cand_off = ws / "model" / "China_Ecommerce_skip_off_v03.xlsx"
    shutil.copyfile(cand, cand_off)
    mhooks.set_full_calc_on_load(cand_off, False)
    assert mfp.build_fingerprint(cand_off, ticker="JD")["calc_properties"]["full_calc_on_load"] is False
    chk_off = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir_off,
        candidate_path=cand_off,
        baseline_path=base,
        coord_map_path=cm_p,
        run_id="run-skip-off",
    )
    assert chk_off["full_calc_on_load"] is False
    assert any(
        f["check_id"] == "C13" and f["severity_class"] == "actionable" and "without fullCalcOnLoad=1" in f["message"]
        for f in chk_off["findings"]
    ), chk_off["findings"]
    rem_off = (rdir_off / "model_dump" / "remaining-issues.md").read_text(encoding="utf-8")
    assert "已开启 `fullCalcOnLoad=1`" not in rem_off
    assert "未开启 `fullCalcOnLoad=1`" in rem_off


def test_yi_unit_scale_alignment_model_and_consensus_shared_parser(
    tmp_path: Path,
) -> None:
    """R6 #3 (Grok medium):
    - Model side parses '亿' / '亿元' as 1e8 via the exact same parser as consensus side.
    - When both model and consensus are in '亿元', scale_factor == 1.0 (no 1e8 mis-multiplication),
      and editing a driver on a 亿元 model does NOT trigger false C04 actionable.
    - Cross-unit alignment ('亿元' vs '元' and '亿元' vs '百万元') aligns to 0% diff.
    - Genuine >5% gap in '亿元' without rationale on a round-edited cell STILL triggers C04 actionable.
    - Unrecognized model unit never silently divides by 1.0 (produces missing_evidence)."""
    assert mfp.parse_unit_scale(None, "JD.com (RMB 亿元)") == (1e8, True)
    assert mfp.parse_unit_scale(None, "单位：亿") == (1e8, True)
    assert mfp.parse_unit_scale(None, "人民币十亿元") == (1e9, True)
    assert mfp.parse_unit_scale(None, "JD.com (RMB MM)") == (1e6, True)
    assert mfp.parse_unit_scale(None, "Unit: RMB") == (1.0, True)
    assert mfp.parse_unit_scale(None, "JD.com (单位：未知)") == (None, True)
    assert mfp._parse_unit_scale("JD.com (RMB 亿元)") == 1e8

    ws, _, base = _copy_fixture_to_ws(tmp_path)
    orig_base = tmp_path / "orig_base_for_yi.xlsx"
    shutil.copyfile(base, orig_base)
    # Set Model - JD!A1 unit header to 亿元 while preserving formula caches
    wb = openpyxl.load_workbook(base, data_only=False)
    wb["Model - JD"]["A1"].value = "JD.com Financial Model (单位：人民币亿元)"
    wb.save(base)
    wb.close()
    mhooks.restore_donor_cache(base, {"cache_donor_path": str(orig_base), "touched_cells": {"Model - JD": ["A1"]}})

    fp_yi = mfp.build_fingerprint(base, ticker="JD")
    assert fp_yi["sheets"]["Model - JD"]["period_axis"]["unit_scale"] == 1e8
    assert fp_yi["sheets"]["Model - JD"]["period_axis"]["unit_scale_parsed"] == 1e8
    q3_house = float(fp_yi["sheets"]["Model - JD"]["cells"]["Q6"]["cached_value"])

    # Edit driver Q25 feeding Q6 so Q6 is in-scope for C04!
    rdir_eq = ws / "research" / "2026-11-27-yi-eq"
    cm_p = rdir_eq / "model_dump" / "coord_map.json"
    res_ed = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "cell_edits": [{"sheet": "Model - JD", "cell": "Q25", "value": 0.13}],
            "changelog_entry": {"version": "v02", "date": "2026-11-27", "baseline": base.name, "summary": "Yi unit test"},
        },
        workspace_dir=ws,
        coord_map_out=cm_p,
        skip_recalc=True,
    )
    cand = Path(res_ed["candidate_path"])

    # 1. Both model & consensus in '亿元' with equal value -> scale_factor == 1.0, 0% diff, C04 actionable == 0!
    diff_yi = mcons.compute_consensus_diff(
        model_path=cand,
        ticker="JD",
        research_dir=rdir_eq,
        consensus_data={
            "source_type": "Wind Consensus",
            "as_of_date": "2026-11-27",
            "basis": {"currency": "RMB", "unit": "亿元"},
            "items": [{"period": "3Q26E", "metric": "Total Revenue", "consensus": q3_house}],
        },
    )
    assert diff_yi["status"] == "ok"
    assert diff_yi["exceeds_count"] == 0
    assert diff_yi["missing_rationale_count"] == 0
    assert abs(float(diff_yi["items"][0]["scale_factor"]) - 1.0) < 1e-12
    assert abs(float(diff_yi["items"][0]["diff_pct"])) < 1e-9
    chk_yi = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir_eq,
        candidate_path=cand,
        baseline_path=base,
        coord_map_path=cm_p,
        run_id="run-yi-eq",
    )
    assert not any(f["check_id"] == "C04" and f["severity_class"] == "actionable" for f in chk_yi["findings"]), chk_yi["findings"]

    # 2. Model '亿元' vs Consensus '元' (House * 1e8) & '百万元' (House * 100) -> 0% diff!
    rdir_cross = ws / "research" / "2026-11-27-yi-cross"
    diff_cross = mcons.compute_consensus_diff(
        model_path=cand,
        ticker="JD",
        research_dir=rdir_cross,
        consensus_data={
            "source_type": "Wind Consensus",
            "as_of_date": "2026-11-27",
            "basis": {"currency": "RMB"},
            "items": [
                {"period": "3Q26E", "metric": "Total Revenue", "consensus": q3_house * 1e8, "unit": "元"},
                {"period": "3Q26E", "metric": "Total Revenue", "consensus": q3_house * 100.0, "unit": "百万元"},
            ],
        },
    )
    assert diff_cross["status"] == "ok"
    assert diff_cross["exceeds_count"] == 0
    assert all(abs(float(it["diff_pct"])) < 1e-9 for it in diff_cross["items"])

    # 3. Both '亿元' with genuine >5% gap and no rationale on edited Q6 -> C04 actionable!
    rdir_gap = ws / "research" / "2026-11-27-yi-gap"
    diff_gap = mcons.compute_consensus_diff(
        model_path=cand,
        ticker="JD",
        research_dir=rdir_gap,
        consensus_data={
            "source_type": "Wind Consensus",
            "as_of_date": "2026-11-27",
            "basis": {"currency": "RMB", "unit": "亿"},
            "items": [{"period": "3Q26E", "metric": "Total Revenue", "consensus": q3_house * 0.7}],
        },
    )
    assert diff_gap["exceeds_count"] == 1
    assert diff_gap["missing_rationale_count"] == 1
    chk_gap = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir_gap,
        candidate_path=cand,
        baseline_path=base,
        coord_map_path=cm_p,
        run_id="run-yi-gap",
    )
    assert any(f["check_id"] == "C04" and f["severity_class"] == "actionable" for f in chk_gap["findings"]), chk_gap["findings"]

    # 4. Unrecognized model unit + consensus '亿元' -> missing_evidence (NEVER divide by 1.0)!
    cand_unk = ws / "model" / "China_Ecommerce_unk_unit_v03.xlsx"
    shutil.copyfile(cand, cand_unk)
    wb_u = openpyxl.load_workbook(cand_unk, data_only=False)
    wb_u["Model - JD"]["A1"].value = "JD.com (单位：神秘刻度)"
    wb_u.save(cand_unk)
    wb_u.close()
    mhooks.restore_donor_cache(cand_unk, {"cache_donor_path": str(cand), "touched_cells": {"Model - JD": ["A1"]}})
    rdir_unk = ws / "research" / "2026-11-27-yi-unk"
    diff_unk = mcons.compute_consensus_diff(
        model_path=cand_unk,
        ticker="JD",
        research_dir=rdir_unk,
        consensus_data={
            "source_type": "Wind Consensus",
            "as_of_date": "2026-11-27",
            "basis": {"unit": "亿元"},
            "items": [{"period": "3Q26E", "metric": "Total Revenue", "consensus": q3_house}],
        },
    )
    assert diff_unk["status"] == "missing_evidence"
    assert diff_unk["unaligned_count"] == 1
    assert diff_unk["exceeds_count"] == 0
    chk_unk = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=rdir_unk,
        candidate_path=cand_unk,
        baseline_path=base,
        coord_map_path=cm_p,
        run_id="run-yi-unk",
    )
    assert any(f["check_id"] == "C04" and f["severity_class"] == "missing_evidence" for f in chk_unk["findings"]), chk_unk["findings"]
    assert not any(f["check_id"] == "C04" and f["severity_class"] == "actionable" for f in chk_unk["findings"]), chk_unk["findings"]


# ---------------------------------------------------------------------------
# 13. Round-7 targeted tests (3 narrowed Codex R6 fixes)
# ---------------------------------------------------------------------------


def test_vml_hf_before_comment_vml_pairs_by_referencing_element(tmp_path: Path) -> None:
    """R7 #1 (Codex high, model_xlsx_hooks.py:1118):
    - Baseline worksheet has header/footer VML (rId1 -> vmlDrawing1.vml, <legacyDrawingHF r:id="rId1"/>)
      listed BEFORE comment VML (rId2 -> vmlDrawing2.vml, <legacyDrawing r:id="rId2"/>) in worksheet .rels.
    - Editing ONLY 1 cell on Model - JD keeps <legacyDrawing> attached to rId2 (comment VML),
      restores <legacyDrawingHF r:id="rId1"/> (HF VML) in schema order, leaves donor .rels untouched,
      and openpyxl still reads the JD comment cleanly.
    - When donor has ONLY <legacyDrawingHF> (no comments) and edited adds a new comment (<legacyDrawing>),
      pairing by referencing element does NOT hijack HF VML; it imports the new comment VML under a fresh rId.
    - When two <tablePart> entries swap order in edited, semantic table-name matching keeps each paired to its donor rId."""
    from openpyxl.comments import Comment

    ws, _, base = _copy_fixture_to_ws(tmp_path)
    orig_base = tmp_path / "orig_base_for_vml.xlsx"
    shutil.copyfile(base, orig_base)

    # 1. Add a comment on Model - JD!A5 via openpyxl and restore donor formula caches
    wb = openpyxl.load_workbook(base, data_only=False)
    wb["Model - JD"]["A5"].comment = Comment("JD analyst note", "Analyst")
    wb.save(base)
    wb.close()
    mhooks.restore_donor_cache(base, {"cache_donor_path": str(orig_base), "touched_cells": {"Model - JD": ["A5"]}})

    # 2. Rewrite Model - JD's ZIP parts so HF VML (rId1 -> vmlDrawing1.vml) precedes comment VML (rId2 -> vmlDrawing2.vml)
    infos_b, pay_b = mhooks._read_package(base)
    parts_b = mhooks._sheet_parts(pay_b)
    jd_part = parts_b["Model - JD"]
    jd_rels_p = mhooks._worksheet_rels_path(jd_part)

    # Locate the comment VML part & comments part generated by openpyxl
    rels_root = etree.fromstring(pay_b[jd_rels_p], mhooks._XML)
    vml_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/vmlDrawing"
    comment_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
    orig_vml_part: str | None = None
    orig_comment_tgt: str | None = None
    for rel in rels_root.findall(f"{{{mhooks._PKG_REL_NS}}}Relationship"):
        if rel.get("Type") == vml_type:
            orig_vml_part = mhooks._resolve_rel_target(jd_part, rel.get("Target") or "")
        elif rel.get("Type") == comment_type:
            orig_comment_tgt = rel.get("Target") or ""
    assert orig_vml_part and orig_vml_part in pay_b
    assert orig_comment_tgt

    comment_vml_bytes = pay_b[orig_vml_part] + b"\n<!-- COMMENT_VML_MARKER -->"
    hf_vml_bytes = (
        b'<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">'
        b"<!-- HF_VML_MARKER -->"
        b'<o:shapelayout v:ext="edit"><o:idmap v:ext="edit" data="2"/></o:shapelayout>'
        b"</xml>"
    )
    # Remove old generated vml part if its name differs from vmlDrawing1/2
    if orig_vml_part not in ("xl/drawings/vmlDrawing1.vml", "xl/drawings/vmlDrawing2.vml"):
        infos_b = [i for i in infos_b if i.filename != orig_vml_part]
        pay_b.pop(orig_vml_part, None)
    for vpath, vbytes in (
        ("xl/drawings/vmlDrawing1.vml", hf_vml_bytes),
        ("xl/drawings/vmlDrawing2.vml", comment_vml_bytes),
    ):
        if vpath not in pay_b:
            zi_v = zipfile.ZipInfo(vpath)
            zi_v.compress_type = zipfile.ZIP_DEFLATED
            infos_b.append(zi_v)
        pay_b[vpath] = vbytes

    pay_b[jd_rels_p] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/vmlDrawing" Target="../drawings/vmlDrawing1.vml"/>'
        b'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/vmlDrawing" Target="../drawings/vmlDrawing2.vml"/>'
        + f'<Relationship Id="rId3" Type="{comment_type}" Target="{orig_comment_tgt}"/>'.encode("utf-8")
        + b"</Relationships>"
    )
    jd_root = etree.fromstring(pay_b[jd_part], mhooks._XML)
    ld_el = jd_root.find(f"{{{mhooks._SML}}}legacyDrawing")
    assert ld_el is not None
    ld_el.set(mhooks._REL_ID, "rId2")
    ldhf_el = etree.Element(f"{{{mhooks._SML}}}legacyDrawingHF")
    ldhf_el.set(mhooks._REL_ID, "rId1")
    mhooks._insert_ws_element_in_schema_order(jd_root, ldhf_el)
    pay_b[jd_part] = etree.tostring(jd_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(base, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos_b:
            zout.writestr(info, pay_b[info.filename], compress_type=info.compress_type)

    # 3. Edit ONLY 1 cell on Model - JD via apply_edit_plan
    res = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan={
            "cell_edits": [{"sheet": "Model - JD", "cell": "Q25", "value": 0.135}],
            "changelog_entry": {
                "version": "v02",
                "date": "2026-11-27",
                "baseline": base.name,
                "summary": "Edit JD single cell with HF VML before comment VML",
            },
        },
        workspace_dir=ws,
        skip_recalc=True,
    )
    cand = Path(res["candidate_path"])
    _, cand_pay = mhooks._read_package(cand)

    # Donor .rels stays byte-for-byte untouched!
    assert cand_pay[jd_rels_p] == pay_b[jd_rels_p]

    cand_jd_root = etree.fromstring(cand_pay[jd_part], mhooks._XML)
    cand_ld = cand_jd_root.find(f"{{{mhooks._SML}}}legacyDrawing")
    cand_ldhf = cand_jd_root.find(f"{{{mhooks._SML}}}legacyDrawingHF")
    assert cand_ld is not None and cand_ld.get(mhooks._REL_ID) == "rId2"
    assert cand_ldhf is not None and cand_ldhf.get(mhooks._REL_ID) == "rId1"

    # Verify schema order: legacyDrawing precedes legacyDrawingHF
    cand_tags = [etree.QName(c.tag).localname for c in cand_jd_root if isinstance(c.tag, str)]
    assert cand_tags.index("legacyDrawing") < cand_tags.index("legacyDrawingHF")

    # Verify resolved targets match comment VML vs HF VML
    cand_rels = {
        r.get("Id"): mhooks._resolve_rel_target(jd_part, r.get("Target") or "")
        for r in etree.fromstring(cand_pay[jd_rels_p], mhooks._XML).findall(f"{{{mhooks._PKG_REL_NS}}}Relationship")
    }
    assert cand_rels[cand_ld.get(mhooks._REL_ID)] == "xl/drawings/vmlDrawing2.vml"
    assert b"COMMENT_VML_MARKER" in cand_pay["xl/drawings/vmlDrawing2.vml"]
    assert cand_rels[cand_ldhf.get(mhooks._REL_ID)] == "xl/drawings/vmlDrawing1.vml"
    assert b"HF_VML_MARKER" in cand_pay["xl/drawings/vmlDrawing1.vml"]

    # Openpyxl opens candidate cleanly and reads both edited cell and preserved comment
    wb_chk = openpyxl.load_workbook(cand, data_only=False)
    try:
        assert wb_chk["Model - JD"]["Q25"].value == 0.135
        assert wb_chk["Model - JD"]["A5"].comment is not None
        assert wb_chk["Model - JD"]["A5"].comment.text == "JD analyst note"
    finally:
        wb_chk.close()

    # 4. Bonus edge case: donor has ONLY <legacyDrawingHF r:id="rId1"/> (no comments), and edited adds a NEW comment!
    #    <legacyDrawing> MUST NOT hijack HF VML rId1; new comment VML is imported under fresh rId.
    donor_hf_only = tmp_path / "donor_hf_only.xlsx"
    shutil.copyfile(orig_base, donor_hf_only)
    inf_h, pay_h = mhooks._read_package(donor_hf_only)
    jd_p_h = mhooks._sheet_parts(pay_h)["Model - JD"]
    jd_r_h = mhooks._worksheet_rels_path(jd_p_h)
    zi_hf = zipfile.ZipInfo("xl/drawings/vmlDrawing1.vml")
    zi_hf.compress_type = zipfile.ZIP_DEFLATED
    inf_h.append(zi_hf)
    pay_h["xl/drawings/vmlDrawing1.vml"] = hf_vml_bytes
    if jd_r_h not in pay_h:
        zi_rh = zipfile.ZipInfo(jd_r_h)
        zi_rh.compress_type = zipfile.ZIP_DEFLATED
        inf_h.append(zi_rh)
    pay_h[jd_r_h] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/vmlDrawing" Target="../drawings/vmlDrawing1.vml"/>'
        b"</Relationships>"
    )
    sh_h = etree.fromstring(pay_h[jd_p_h], mhooks._XML)
    el_hf = etree.Element(f"{{{mhooks._SML}}}legacyDrawingHF")
    el_hf.set(mhooks._REL_ID, "rId1")
    mhooks._insert_ws_element_in_schema_order(sh_h, el_hf)
    pay_h[jd_p_h] = etree.tostring(sh_h, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(donor_hf_only, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in inf_h:
            zout.writestr(info, pay_h[info.filename], compress_type=info.compress_type)

    edited_with_new_cmt = tmp_path / "edited_new_cmt.xlsx"
    shutil.copyfile(donor_hf_only, edited_with_new_cmt)
    wb_nc = openpyxl.load_workbook(edited_with_new_cmt, data_only=False)
    wb_nc["Model - JD"]["A5"].comment = Comment("Brand new comment", "Analyst")
    wb_nc.save(edited_with_new_cmt)
    wb_nc.close()

    merged_hf_cmt = tmp_path / "merged_hf_plus_new_cmt.xlsx"
    mhooks.merge_edited_sheets_into_donor(
        donor_hf_only, edited_with_new_cmt, merged_hf_cmt, modified_sheets={"Model - JD"}
    )
    _, pay_m = mhooks._read_package(merged_hf_cmt)
    sh_m = etree.fromstring(pay_m[jd_p_h], mhooks._XML)
    m_ld = sh_m.find(f"{{{mhooks._SML}}}legacyDrawing")
    m_ldhf = sh_m.find(f"{{{mhooks._SML}}}legacyDrawingHF")
    assert m_ld is not None and m_ldhf is not None
    assert m_ldhf.get(mhooks._REL_ID) == "rId1"
    assert m_ld.get(mhooks._REL_ID) != "rId1"
    m_rels = {
        r.get("Id"): mhooks._resolve_rel_target(jd_p_h, r.get("Target") or "")
        for r in etree.fromstring(pay_m[jd_r_h], mhooks._XML).findall(f"{{{mhooks._PKG_REL_NS}}}Relationship")
    }
    assert m_rels[m_ldhf.get(mhooks._REL_ID)] == "xl/drawings/vmlDrawing1.vml"
    assert m_rels[m_ld.get(mhooks._REL_ID)] != "xl/drawings/vmlDrawing1.vml"
    assert m_rels[m_ld.get(mhooks._REL_ID)] in pay_m

    # 5. TableParts semantic pairing: when two <tablePart>s appear in reversed order in edited,
    #    each still pairs to its matching donor table relationship by table name.
    donor_tbl = tmp_path / "donor_tables.xlsx"
    edited_tbl = tmp_path / "edited_tables.xlsx"
    merged_tbl = tmp_path / "merged_tables.xlsx"
    tbl_rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/table"
    for target_p, order in ((donor_tbl, ("SalesTbl", "CostTbl")), (edited_tbl, ("CostTbl", "SalesTbl"))):
        shutil.copyfile(orig_base, target_p)
        inf_t, pay_t = mhooks._read_package(target_p)
        jp = mhooks._sheet_parts(pay_t)["Model - JD"]
        jrp = mhooks._worksheet_rels_path(jp)
        for idx_t, tname in enumerate(order, start=1):
            tpart = f"xl/tables/table{idx_t}.xml"
            zi_t = zipfile.ZipInfo(tpart)
            zi_t.compress_type = zipfile.ZIP_DEFLATED
            inf_t.append(zi_t)
            pay_t[tpart] = (
                f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<table xmlns="{mhooks._SML}" id="{idx_t}" name="{tname}" displayName="{tname}" ref="A1:B2"/>'
            ).encode("utf-8")
        if jrp not in pay_t:
            zi_jr = zipfile.ZipInfo(jrp)
            zi_jr.compress_type = zipfile.ZIP_DEFLATED
            inf_t.append(zi_jr)
        pay_t[jrp] = (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + f'<Relationship Id="rId1" Type="{tbl_rel_type}" Target="../tables/table1.xml"/>'.encode("utf-8")
            + f'<Relationship Id="rId2" Type="{tbl_rel_type}" Target="../tables/table2.xml"/>'.encode("utf-8")
            + b"</Relationships>"
        )
        sh_t = etree.fromstring(pay_t[jp], mhooks._XML)
        tp_container = etree.Element(f"{{{mhooks._SML}}}tableParts", count="2")
        for rid_s in ("rId1", "rId2"):
            tp_el = etree.SubElement(tp_container, f"{{{mhooks._SML}}}tablePart")
            tp_el.set(mhooks._REL_ID, rid_s)
        mhooks._insert_ws_element_in_schema_order(sh_t, tp_container)
        pay_t[jp] = etree.tostring(sh_t, xml_declaration=True, encoding="UTF-8", standalone=True)
        with zipfile.ZipFile(target_p, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info in inf_t:
                zout.writestr(info, pay_t[info.filename], compress_type=info.compress_type)

    mhooks.merge_edited_sheets_into_donor(donor_tbl, edited_tbl, merged_tbl, modified_sheets={"Model - JD"})
    _, pay_mt = mhooks._read_package(merged_tbl)
    jp_mt = mhooks._sheet_parts(pay_mt)["Model - JD"]
    sh_mt = etree.fromstring(pay_mt[jp_mt], mhooks._XML)
    tps = sh_mt.findall(f".//{{{mhooks._SML}}}tablePart")
    # Edited had [CostTbl (rId1), SalesTbl (rId2)]; donor had rId1=SalesTbl, rId2=CostTbl -> remapped to [rId2, rId1]!
    assert [el.get(mhooks._REL_ID) for el in tps] == ["rId2", "rId1"]


def test_external_fingerprint_cannot_bypass_skip_recalc_guard_on_default_template(
    tmp_path: Path,
) -> None:
    """R7 #2 (Codex high, model_edit.py:592):
    - Passing an external --fingerprint from another cached workbook to default_model_template.xlsx
      with --skip-recalc is rejected immediately (source_sha256 mismatch) in both Python API and CLI.
    - Even a forged fingerprint whose source_sha256 matches the default template (claiming cached values)
      CANNOT bypass the --skip-recalc guard because empty-cache detection inspects the baseline workbook directly.
    - A valid matching fingerprint on a cached non-template workbook still works cleanly."""
    ws, _, cached_base = _copy_fixture_to_ws(tmp_path)
    tpl_copy = tmp_path / "default_model_template.xlsx"
    shutil.copyfile(BUNDLED_TEMPLATE_XLSX, tpl_copy)

    cached_fp = mfp.build_fingerprint(cached_base, ticker="JD")
    tpl_fp = mfp.build_fingerprint(tpl_copy, ticker="DEMO")

    plan_tpl = {
        "cell_edits": [{"sheet": "Model", "cell": "O4", "value": 2000.0}],
        "changelog_entry": {
            "version": "v01",
            "date": "2026-11-27",
            "baseline": "default_model_template.xlsx",
            "summary": "Attempt skip-recalc bypass via external fingerprint",
        },
    }

    # 1. External fingerprint from cached workbook -> rejected immediately (source_sha256 mismatch)!
    out_bypass1 = ws / "model" / "DEMO-model-bypass1.xlsx"
    with pytest.raises(ValueError, match="fingerprint source_sha256 mismatch"):
        medit.apply_edit_plan(
            baseline_path=tpl_copy,
            ticker="DEMO",
            plan=plan_tpl,
            output_path=out_bypass1,
            fingerprint=cached_fp,
            skip_recalc=True,
            mode="auto",
        )
    assert not out_bypass1.exists()

    # CLI path with --fingerprint from another cached workbook -> non-zero exit & no output written!
    plan_p = tmp_path / "plan_tpl_r7.json"
    fp_p = tmp_path / "cached_fp_r7.json"
    plan_p.write_text(json.dumps(plan_tpl), encoding="utf-8")
    fp_p.write_text(json.dumps(cached_fp), encoding="utf-8")
    rc = medit.main(
        [
            "apply",
            "--baseline",
            str(tpl_copy),
            "--ticker",
            "DEMO",
            "--plan",
            str(plan_p),
            "--fingerprint",
            str(fp_p),
            "--out",
            str(out_bypass1),
            "--skip-recalc",
            "--mode",
            "auto",
        ]
    )
    assert rc != 0
    assert not out_bypass1.exists()

    # 2. Forged fingerprint with matching source_sha256 claiming populated caches -> STILL refused by direct baseline check!
    forged_fp = copy.deepcopy(cached_fp)
    forged_fp["source_sha256"] = tpl_fp["source_sha256"]
    out_bypass2 = ws / "model" / "DEMO-model-bypass2.xlsx"
    with pytest.raises(ValueError, match="--skip-recalc"):
        medit.apply_edit_plan(
            baseline_path=tpl_copy,
            ticker="DEMO",
            plan=plan_tpl,
            output_path=out_bypass2,
            fingerprint=forged_fp,
            skip_recalc=True,
            mode="auto",
        )
    assert not out_bypass2.exists()

    # 3. Missing source_sha256 in external fingerprint -> rejected!
    no_sha_fp = copy.deepcopy(cached_fp)
    no_sha_fp.pop("source_sha256", None)
    with pytest.raises(ValueError, match="fingerprint source_sha256 mismatch"):
        medit.apply_edit_plan(
            baseline_path=cached_base,
            ticker="JD",
            plan={"cell_edits": [{"sheet": "Model - JD", "cell": "Q25", "value": 0.12}]},
            output_path=ws / "model" / "JD-nosha.xlsx",
            fingerprint=no_sha_fp,
            skip_recalc=True,
        )

    # 4. Valid matching fingerprint on cached non-template workbook with skip_recalc=True succeeds!
    out_ok = ws / "model" / "JD-valid-fp-v02.xlsx"
    res_ok = medit.apply_edit_plan(
        baseline_path=cached_base,
        ticker="JD",
        plan={"cell_edits": [{"sheet": "Model - JD", "cell": "Q25", "value": 0.125}]},
        output_path=out_ok,
        workspace_dir=ws,
        fingerprint=cached_fp,
        skip_recalc=True,
    )
    assert res_ok["status"] == "ok"
    assert res_ok["recalc_status"] == "skipped"
    assert out_ok.is_file()


def _make_workbook_with_pivot_caches_and_no_calcpr(src_xlsx: Path, dst_xlsx: Path) -> None:
    """Create a valid non-template OOXML workbook with <pivotCaches> + <extLst> and NO <calcPr>."""
    shutil.copyfile(src_xlsx, dst_xlsx)
    infos, payload = mhooks._read_package(dst_xlsx)

    # 1. Add a minimal valid pivotCacheDefinition1.xml part
    pc_part = "xl/pivotCache/pivotCacheDefinition1.xml"
    pc_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<pivotCacheDefinition xmlns="{mhooks._SML}" '
        f'xmlns:r="{mhooks._OFFICE_REL_NS}" recordCount="0">'
        f'<cacheSource type="worksheet"><worksheetSource ref="A1:B2" sheet="Model - JD"/></cacheSource>'
        f'<cacheFields count="1"><cacheField name="Item" numFmtId="0"><sharedItems count="0"/></cacheField></cacheFields>'
        f"</pivotCacheDefinition>"
    ).encode("utf-8")
    zi_pc = zipfile.ZipInfo(pc_part)
    zi_pc.compress_type = zipfile.ZIP_DEFLATED
    infos.append(zi_pc)
    payload[pc_part] = pc_xml

    # 2. Register relationship in xl/_rels/workbook.xml.rels
    rels_root = etree.fromstring(payload["xl/_rels/workbook.xml.rels"], mhooks._XML)
    used_rids = {r.get("Id") or "" for r in rels_root.findall(f"{{{mhooks._PKG_REL_NS}}}Relationship")}
    pc_rid = "rId99"
    n = 99
    while pc_rid in used_rids:
        n += 1
        pc_rid = f"rId{n}"
    etree.SubElement(
        rels_root,
        f"{{{mhooks._PKG_REL_NS}}}Relationship",
        Id=pc_rid,
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/pivotCacheDefinition",
        Target="pivotCache/pivotCacheDefinition1.xml",
    )
    payload["xl/_rels/workbook.xml.rels"] = etree.tostring(
        rels_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    # 3. Register content type Override
    ct_root = etree.fromstring(payload["[Content_Types].xml"], mhooks._XML)
    etree.SubElement(
        ct_root,
        f"{{{mhooks._CT_NS}}}Override",
        PartName=f"/{pc_part}",
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.pivotCacheDefinition+xml",
    )
    payload["[Content_Types].xml"] = etree.tostring(
        ct_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    # 4. Remove <calcPr> from xl/workbook.xml and add <pivotCaches> + <extLst> in schema order
    wb_root = etree.fromstring(payload["xl/workbook.xml"], mhooks._XML)
    for existing_cp in wb_root.findall(f"{{{mhooks._SML}}}calcPr"):
        wb_root.remove(existing_cp)
    pcs_el = etree.Element(f"{{{mhooks._SML}}}pivotCaches")
    pc_el = etree.SubElement(pcs_el, f"{{{mhooks._SML}}}pivotCache", cacheId="1")
    pc_el.set(mhooks._REL_ID, pc_rid)
    mhooks._insert_wb_element_in_schema_order(wb_root, pcs_el)
    if wb_root.find(f"{{{mhooks._SML}}}extLst") is None:
        mhooks._insert_wb_element_in_schema_order(wb_root, etree.Element(f"{{{mhooks._SML}}}extLst"))
    payload["xl/workbook.xml"] = etree.tostring(
        wb_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    with zipfile.ZipFile(dst_xlsx, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename], compress_type=info.compress_type)


def test_calcpr_inserted_before_pivot_caches_in_ooxml_order_on_skip_recalc(
    tmp_path: Path,
) -> None:
    """R7 #3 (Codex medium, model_xlsx_hooks.py:182):
    - Non-template baseline without <calcPr> and with <pivotCaches> + <extLst> inserts <calcPr>
      in strict ECMA-376 CT_Workbook order (before <pivotCaches> and <extLst>) on --skip-recalc.
    - Resulting workbook opens cleanly in openpyxl (both data_only=False and data_only=True)."""
    ws, _, base = _copy_fixture_to_ws(tmp_path)
    pc_base = ws / "model" / "China_Ecommerce_Pivot_v01.xlsx"
    _make_workbook_with_pivot_caches_and_no_calcpr(base, pc_base)

    # 1. Direct set_full_calc_on_load check
    direct_copy = tmp_path / "direct_pivot_fcol.xlsx"
    shutil.copyfile(pc_base, direct_copy)
    mhooks.set_full_calc_on_load(direct_copy, True)
    _, pay_d = mhooks._read_package(direct_copy)
    wb_d = etree.fromstring(pay_d["xl/workbook.xml"], mhooks._XML)
    tags_d = [etree.QName(c.tag).localname for c in wb_d if isinstance(c.tag, str)]
    assert "calcPr" in tags_d and "pivotCaches" in tags_d and "extLst" in tags_d
    assert tags_d.index("sheets") < tags_d.index("calcPr") < tags_d.index("pivotCaches") < tags_d.index("extLst")
    if "definedNames" in tags_d:
        assert tags_d.index("definedNames") < tags_d.index("calcPr")
    ordered_idx_d = [mhooks._WB_CHILD_ORDER.index(t) for t in tags_d if t in mhooks._WB_CHILD_ORDER]
    assert ordered_idx_d == sorted(ordered_idx_d), tags_d

    # 2. Full apply_edit_plan(..., skip_recalc=True) flow
    res = medit.apply_edit_plan(
        baseline_path=pc_base,
        ticker="JD",
        plan={
            "cell_edits": [{"sheet": "Model - JD", "cell": "Q25", "value": 0.14}],
            "changelog_entry": {
                "version": "v02",
                "date": "2026-11-27",
                "baseline": pc_base.name,
                "summary": "Skip recalc with pivotCaches baseline",
            },
        },
        workspace_dir=ws,
        skip_recalc=True,
    )
    cand = Path(res["candidate_path"])
    assert res["recalc_status"] == "skipped"

    _, pay_c = mhooks._read_package(cand)
    wb_c = etree.fromstring(pay_c["xl/workbook.xml"], mhooks._XML)
    calc_el = wb_c.find(f"{{{mhooks._SML}}}calcPr")
    assert calc_el is not None and calc_el.get("fullCalcOnLoad") == "1"
    tags_c = [etree.QName(c.tag).localname for c in wb_c if isinstance(c.tag, str)]
    assert tags_c.index("sheets") < tags_c.index("calcPr") < tags_c.index("pivotCaches") < tags_c.index("extLst")
    if "definedNames" in tags_c:
        assert tags_c.index("definedNames") < tags_c.index("calcPr")
    ordered_idx_c = [mhooks._WB_CHILD_ORDER.index(t) for t in tags_c if t in mhooks._WB_CHILD_ORDER]
    assert ordered_idx_c == sorted(ordered_idx_c), tags_c

    # 3. Verify openpyxl opens candidate cleanly in both modes
    wb_f = openpyxl.load_workbook(cand, data_only=False)
    try:
        assert wb_f["Model - JD"]["Q25"].value == 0.14
        assert "Model - BABA" in wb_f.sheetnames
    finally:
        wb_f.close()

    wb_v = openpyxl.load_workbook(cand, data_only=True)
    try:
        assert wb_v["Model - JD"]["Q25"].value == 0.14
        assert wb_v["Model - BABA"]["E4"].value is not None
    finally:
        wb_v.close()


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice soffice not installed in this environment")
def test_calcpr_before_pivot_caches_opens_in_real_libreoffice(tmp_path: Path) -> None:
    """R7 #3 live LibreOffice check: candidate produced from pivotCaches baseline with --skip-recalc opens cleanly in soffice."""
    ws, _, base = _copy_fixture_to_ws(tmp_path)
    pc_base = ws / "model" / "China_Ecommerce_Pivot_LO_v01.xlsx"
    _make_workbook_with_pivot_caches_and_no_calcpr(base, pc_base)

    res = medit.apply_edit_plan(
        baseline_path=pc_base,
        ticker="JD",
        plan={"cell_edits": [{"sheet": "Model - JD", "cell": "Q25", "value": 0.14}]},
        workspace_dir=ws,
        skip_recalc=True,
    )
    cand = Path(res["candidate_path"])
    out_dir = tmp_path / "lo_out"
    produced = recalc_mod.soffice_calculate(cand, out_dir, timeout=60)
    assert produced.is_file()
    wb_lo = openpyxl.load_workbook(produced, data_only=True)
    try:
        assert wb_lo["Model - JD"]["Q25"].value == 0.14
    finally:
        wb_lo.close()


# ===========================================================================
# P0-b: Workflow assembly, company-model, deep-update/full-coverage migration,
#       check_done closure, sector single-track routing, CoS verbatim forward
# ===========================================================================


@pytest.mark.asyncio
async def test_p0b_content_assembly_js_include_confinement_cycles_and_prebuilt_resume(
    tmp_path: Path,
) -> None:
    """§3.3 / §8.8 #1: /* DALTON_INCLUDE:_shared/*.js */ expands recursively BEFORE
    DALTON_CAP pruning, stays confined to workflows/_shared/, detects cycles, passes
    PrebuiltWorkflowRegistry + QuickJS compile_check in Everflow & Core, and keeps
    resume script-snapshot matching consistent."""
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import compile_check
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        PrebuiltWorkflowRegistry,
        read_assembled_workflow_file,
    )
    from src.dalton.capabilities import CapabilityId
    from src.dalton.content_assembly import (
        ContentAssemblyError,
        assemble_js_text,
        read_assembled_text,
    )
    from src.dalton.workflows.journal import resolve_resume_source

    # 1. Recursive include + DALTON_CAP pruning order (Everflow vs Core)
    wf_root = tmp_path / "workflows"
    shared = wf_root / "_shared"
    shared.mkdir(parents=True)
    (shared / "leaf.js").write_text(
        "function leafSrc() {\n"
        "  /* DALTON_CAP:researchdata_bridge */\n"
        "  return 'bridge_estimates'\n"
        "  /* DALTON_CAP_ELSE */\n"
        "  return 'public_estimates'\n"
        "  /* /DALTON_CAP */\n"
        "}\n",
        encoding="utf-8",
    )
    (shared / "mid.js").write_text(
        "/* DALTON_INCLUDE:_shared/leaf.js */\nfunction mid() { return leafSrc() }\n",
        encoding="utf-8",
    )
    wf_file = wf_root / "demo" / "workflow.js"
    wf_file.parent.mkdir(parents=True)
    wf_file.write_text(
        "export const meta = { name: 'demo', description: 'demo' }\n"
        "/* DALTON_INCLUDE:_shared/mid.js */\n"
        "return { src: mid() }\n",
        encoding="utf-8",
    )

    core_js = read_assembled_text(wf_file, capabilities=frozenset())
    assert "DALTON_INCLUDE" not in core_js and "DALTON_CAP" not in core_js
    assert "public_estimates" in core_js and "bridge_estimates" not in core_js
    assert compile_check(core_js).name == "demo"

    ef_js = read_assembled_text(
        wf_file, capabilities=frozenset({CapabilityId.RESEARCHDATA_BRIDGE})
    )
    assert "bridge_estimates" in ef_js and "public_estimates" not in ef_js
    assert compile_check(ef_js).name == "demo"

    # 2. Confinement & malformed/missing/cycle rejection
    for bad in (
        "/* DALTON_INCLUDE:../outside.js */",
        "/* DALTON_INCLUDE:demo/workflow.js */",
        "/* DALTON_INCLUDE:/etc/passwd */",
        "/* DALTON_INCLUDE:_shared/missing.js */",
        "/* DALTON_INCLUDE:_shared/leaf.txt */",
        "/* DALTON_INCLUDE:_shared/unclosed",
    ):
        with pytest.raises(ContentAssemblyError):
            assemble_js_text(bad, path=wf_file, capabilities=frozenset())

    (shared / "cyc_a.js").write_text("/* DALTON_INCLUDE:_shared/cyc_b.js */\n", encoding="utf-8")
    (shared / "cyc_b.js").write_text("/* DALTON_INCLUDE:_shared/cyc_a.js */\n", encoding="utf-8")
    with pytest.raises(ContentAssemblyError, match="circular"):
        assemble_js_text("/* DALTON_INCLUDE:_shared/cyc_a.js */", path=wf_file, capabilities=frozenset())

    # 3. Shipped PrebuiltWorkflowRegistry loads company-model + deep + full-coverage cleanly in both profiles
    reg = PrebuiltWorkflowRegistry(root=REPO_ROOT)
    for wf_name in ("company-model", "earnings-deep-update", "full-coverage"):
        assert wf_name in reg.names(), reg.names()
        src = reg.get(wf_name)
        assert src is not None and "runModelingPipeline" in src and "DALTON_INCLUDE" not in src
        assert compile_check(src).name == wf_name
        # Core assembly of shipped workflow must contain zero proprietary words and pass compile_check
        core_shipped = read_assembled_text(
            REPO_ROOT / "workflows" / wf_name / "workflow.js",
            capabilities=frozenset(),
        )
        assert "DALTON_INCLUDE" not in core_shipped and "DALTON_CAP" not in core_shipped
        assert "researchdata" not in core_shipped.lower()
        assert compile_check(core_shipped).name == wf_name

    # 4. Resume script snapshot consistency with assembled company-model script
    assembled_cm = read_assembled_workflow_file(REPO_ROOT / "workflows" / "company-model" / "workflow.js")
    backend = create_autospec(SandboxBackend, instance=True)
    source_dir = ".agents/threads/12345678/workflows/run-prev-1"
    fs_map = {
        f"{source_dir}/workflow.js": assembled_cm,
        f"{source_dir}/status.json": json.dumps({"status": "failed", "run_id": "run-prev-1"}),
    }

    async def _aread(path: str) -> str | None:
        return fs_map.get(path)

    async def _aglob(pattern: str, root: str = ".") -> list[str]:
        return []

    async def _awrite(path: str, content: str) -> bool:
        fs_map[path] = content
        return True

    backend.aread_text.side_effect = _aread
    backend.aglob_paths.side_effect = _aglob
    backend.awrite_text.side_effect = _awrite

    from ptc_agent.agent.middleware.background_subagent.registry import BackgroundTaskRegistry

    fake_reg = create_autospec(BackgroundTaskRegistry, instance=True)
    fake_reg.run_ledger = None
    fake_reg.get_all_tasks.return_value = []

    resumed_dir = await resolve_resume_source(
        backend,
        fake_reg,
        source_dir=source_dir,
        resume="run-prev-1",
        script=assembled_cm,
        run_dir_for=lambda tid: f".agents/threads/12345678/workflows/{tid}",
        claimant="call-resume-1",
    )
    assert resumed_dir == source_dir


def _stamp_formula_caches(xlsx_path: Path, caches: dict[str, dict[str, str]]) -> str:
    """Write ``<v>`` cached values onto formula cells in an OOXML workbook so it mimics a saved Excel baseline."""
    from lxml import etree

    sml = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    infos, payload = mhooks._read_package(xlsx_path)
    parts = mhooks._sheet_parts(payload)
    for sh_name, cell_vals in caches.items():
        part = parts.get(sh_name)
        if not part or part not in payload:
            continue
        root = etree.fromstring(payload[part])
        for c_el in root.iter(f"{{{sml}}}c"):
            coord = (c_el.get("r") or "").upper()
            if coord in cell_vals:
                v_el = c_el.find(f"{{{sml}}}v")
                if v_el is None:
                    v_el = etree.SubElement(c_el, f"{{{sml}}}v")
                v_el.text = str(cell_vals[coord])
        payload[part] = etree.tostring(root, encoding="UTF-8", xml_declaration=True)
    tmp = xlsx_path.with_suffix(".cached.tmp.xlsx")
    with zipfile.ZipFile(tmp, "w") as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename], compress_type=info.compress_type)
    os.replace(tmp, xlsx_path)
    return mfp.file_sha256(xlsx_path)


def _write_simple_valid_model(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model - PDD"
    ws["A1"] = "(RMB MM)"
    ws["E1"] = "2025"
    ws["F1"] = "2026E"
    ws["D2"] = "Total Revenue"
    ws["E2"] = 1000
    ws["F2"] = "=E2*1.1"
    wb.save(path)
    return _stamp_formula_caches(path, {"Model - PDD": {"F2": "1100"}})


@pytest.mark.asyncio
async def test_p0b_company_model_workflow_pass_two_fix_rounds_and_timeout_fallback_ledger(
    tmp_path: Path,
) -> None:
    """Acceptance #4: company-model workflow runs in real QuickJS across:
    (1) clean pass with run_id read from .progress.json after Stage 1,
    (2) fix loop capped at strictly 2 rounds -> delivered_with_open_issues,
    (3) Stage 3 check timeout before model-check.md is ever created -> short
        writeFallbackLedger child (files ONLY model-verification.json + remaining-issues.md)
        writing real ledger that passes model_check.py verify-done."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )

    script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "company-model" / "workflow.js")
    r_rel = "research/2026-11-27-model-PDD"
    params = {
        "ticker": "PDD",
        "company": "PDD Holdings",
        "workspace_dir": ".",
        "as_of": "2026-11-27",
        "progress_path": f"{r_rel}/.progress.json",
    }

    async def _exec_case(handler: Any) -> tuple[Any, list[tuple[str, str]]]:
        calls: list[tuple[str, str]] = []
        spec_host = create_autospec(WorkflowHost, instance=True)

        async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
            label = str(opts.get("label") or "")
            calls.append((label, prompt))
            return handler(label, prompt, opts)

        spec_host.agent.side_effect = _agent
        spec_host.phase.side_effect = lambda _t: None
        spec_host.log.side_effect = lambda _m: None
        outcome = await run_workflow_script(
            script, params, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0)
        )
        assert outcome.status == "completed", outcome.error
        return outcome.result, calls

    # Case 1: Clean pass (0 fix rounds) + separate live run_id probe
    def _h_pass(label: str, prompt: str, _opts: dict[str, Any]) -> Any:
        if label == "建模准备":
            return {
                "files": [f"{r_rel}/model_dump/PDD-fingerprint.json"],
                "summary": "prep",
                "mode": "build",
                "baseline_path": None,
                "baseline_sha256": None,
                "fingerprint_path": f"{r_rel}/model_dump/PDD-fingerprint.json",
                "consensus_path": "data/consensus/2026-11-27_1000.json",
                "planned_output_path": "model/PDD-model-v01.xlsx",
                "baseline_health_warning": None,
            }
        if label == "读取运行ID":
            assert f"{r_rel}/.progress.json" in prompt
            return {"run_id": "run-cm-pass"}
        if label == "模型构建与落盘":
            return {
                "files": ["model/PDD-model-v01.xlsx", f"{r_rel}/model_dump/coord_map.json"],
                "summary": "built",
                "candidate_path": "model/PDD-model-v01.xlsx",
                "model_sha256": "a" * 64,
                "recalc_status": "ok",
            }
        if label == "核验候选落盘":
            return {
                "path": "model/PDD-model-v01.xlsx",
                "exists": True,
                "readable": True,
                "model_sha256": "a" * 64,
                "run_matched": True,
            }
        if label == "一致预期对照":
            return {
                "files": [
                    "model/PDD-model-v01.xlsx",
                    f"{r_rel}/model_dump/consensus-diff.md",
                    f"{r_rel}/model_dump/consensus-diff.json",
                ],
                "summary": "consensus ok",
                "candidate_path": "model/PDD-model-v01.xlsx",
                "model_sha256": "a" * 64,
                "recalc_status": "ok",
                "consensus_status": "ok",
                "consensus_diff_generated": True,
            }
        if label == "模型本轮范围检查":
            assert '--run-id "run-cm-pass"' in prompt
            return {
                "files": [
                    f"{r_rel}/model_dump/model-check.md",
                    f"{r_rel}/model_dump/model-verification.json",
                ],
                "summary": "0 actionable",
                "readable": True,
                "final_status": "pass",
                "model_sha256": "a" * 64,
                "actionable_count": 0,
                "actionable_issues": [],
                "baseline_legacy_issues": [],
                "missing_evidence_issues": [],
                "not_applicable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": None,
                "summary_10lines": "PDD v01 pass",
            }
        raise AssertionError(f"unexpected label: {label}")

    res_pass, calls_pass = await _exec_case(_h_pass)
    assert res_pass["ok"] is True and res_pass["status"] == "pass"
    assert res_pass["fix_rounds"] == 0 and res_pass["run_id"] == "run-cm-pass"
    assert [lbl for lbl, _ in calls_pass] == [
        "建模准备",
        "读取运行ID",
        "模型构建与落盘",
        "核验候选落盘",
        "一致预期对照",
        "模型本轮范围检查",
    ]

    # Case 2: Persistent actionable issues -> strictly capped at 2 fix rounds -> delivered_with_open_issues
    def _h_two_rounds(label: str, prompt: str, _opts: dict[str, Any]) -> Any:
        if label == "建模准备":
            return {"files": ["fp.json"], "summary": "prep", "mode": "update", "baseline_path": "model/PDD-model-v01.xlsx", "baseline_sha256": "1" * 64, "planned_output_path": "model/PDD-model-v02.xlsx"}
        if label == "读取运行ID":
            return {"run_id": "run-cm-2r"}
        if label == "模型构建与落盘":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "v02", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": "2" * 64, "recalc_status": "ok"}
        if label == "核验候选落盘":
            return {"path": "model/PDD-model-v02.xlsx", "exists": True, "readable": True, "model_sha256": "2" * 64, "run_matched": True}
        if label == "一致预期对照":
            return {
                "files": [
                    "model/PDD-model-v02.xlsx",
                    f"{r_rel}/model_dump/consensus-diff.md",
                    f"{r_rel}/model_dump/consensus-diff.json",
                ],
                "summary": "cons ok",
                "candidate_path": "model/PDD-model-v02.xlsx",
                "model_sha256": "2" * 64,
                "recalc_status": "ok",
                "consensus_status": "ok",
                "consensus_diff_generated": True,
            }
        if label == "模型本轮范围检查" or label.startswith("模型本轮范围复查 "):
            return {
                "files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "still actionable",
                "readable": True,
                "final_status": "delivered_with_open_issues",
                "model_sha256": "2" * 64,
                "actionable_count": 1,
                "actionable_issues": ["[C11] 集团与分部营业利润差额 -312 RMB MM"],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md",
            }
        if label.startswith("模型修改 "):
            # R2 #1: Fix round MUST target current candidate v02 (never v01), re-extract fingerprint, and pass --in-place --prior-coord-map!
            assert '--baseline "model/PDD-model-v02.xlsx"' in prompt
            assert '--baseline "model/PDD-model-v01.xlsx"' not in prompt
            assert "model_fingerprint.py" in prompt and "--in-place" in prompt and "--prior-coord-map" in prompt
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "fixed", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": "2" * 64, "recalc_status": "ok", "consensus_diff_generated": True}
        raise AssertionError(f"unexpected label: {label}")

    res_2r, calls_2r = await _exec_case(_h_two_rounds)
    labels_2r = [lbl for lbl, _ in calls_2r]
    assert res_2r["ok"] is True and res_2r["status"] == "delivered_with_open_issues"
    assert res_2r["fix_rounds"] == 2
    assert "模型修改 1" in labels_2r and "模型修改 2" in labels_2r and "模型修改 3" not in labels_2r
    assert labels_2r.count("模型本轮范围复查 1") == 1 and labels_2r.count("模型本轮范围复查 2") == 1

    # Case 3: Check times out BEFORE model-check.md is ever created -> short writeFallbackLedger executes real CLI from prompt -> passes verify-done
    ws_to = tmp_path / "ws_timeout"
    cand_path = ws_to / "model" / "PDD-model-v01.xlsx"
    cand_sha = _write_simple_valid_model(cand_path)

    def _h_timeout(label: str, prompt: str, _opts: dict[str, Any]) -> Any:
        if label == "建模准备":
            return {"files": ["fp.json"], "summary": "prep", "mode": "build", "planned_output_path": "model/PDD-model-v01.xlsx"}
        if label == "读取运行ID":
            return {"run_id": "run-cm-to"}
        if label == "模型构建与落盘":
            return {
                "files": ["model/PDD-model-v01.xlsx"],
                "summary": "v01",
                "candidate_path": "model/PDD-model-v01.xlsx",
                "model_sha256": cand_sha,
                "recalc_status": "ok",
            }
        if label == "核验候选落盘":
            return {
                "path": "model/PDD-model-v01.xlsx",
                "exists": True,
                "readable": True,
                "model_sha256": cand_sha,
                "run_matched": True,
            }
        if label == "一致预期对照":
            return {
                "files": ["model/PDD-model-v01.xlsx"],
                "summary": "cons skipped",
                "candidate_path": "model/PDD-model-v01.xlsx",
                "model_sha256": cand_sha,
                "recalc_status": "ok",
                "consensus_status": "missing",
                "consensus_diff_generated": False,
            }
        if label == "模型本轮范围检查":
            return None  # Simulated timeout before model-check.md was written!
        if label == "写入降级账本":
            import shlex as _shlex

            assert "model-verification.json" in prompt and "remaining-issues.md" in prompt
            assert "不要把未生成的阶段报告放进 files" in prompt
            # Parse & run the REAL write-ledger CLI from the prompt!
            m_cmd = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_check\.py write-ledger [^`]+)`", prompt)
            assert m_cmd, f"write-ledger CLI missing in prompt: {prompt}"
            argv = _shlex.split(m_cmd.group(1))[2:]
            # Rewrite --workspace to ws_to and --research-dir / --candidate relative to ws_to
            patched_argv = []
            i = 0
            while i < len(argv):
                tok = argv[i]
                if tok == "--workspace" and i + 1 < len(argv):
                    patched_argv.extend([tok, str(ws_to)])
                    i += 2
                elif tok in ("--research-dir", "--candidate", "--baseline") and i + 1 < len(argv):
                    val = argv[i + 1]
                    patched_argv.extend([tok, str((ws_to / val) if not Path(val).is_absolute() else Path(val))])
                    i += 2
                else:
                    patched_argv.append(tok)
                    i += 1
            rc = mchk.main(patched_argv)
            assert rc == 0
            led = json.loads((ws_to / r_rel / "model_dump" / "model-verification.json").read_text(encoding="utf-8"))
            return {
                "files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "fallback written",
                "final_status": led["final_status"],
                "model_sha256": led["final_model_sha256"],
                "run_id": led["run_id"],
            }
        raise AssertionError(f"unexpected label: {label}")

    res_to, calls_to = await _exec_case(_h_timeout)
    assert res_to["ok"] is True and res_to["status"] == "delivered_with_open_issues"
    assert "写入降级账本" in [lbl for lbl, _ in calls_to]
    assert not (ws_to / r_rel / "model_dump" / "model-check.md").exists()
    vd = mchk.verify_done(
        workspace_dir=ws_to,
        ticker="PDD",
        research_dir=ws_to / r_rel,
        expected_run_id="run-cm-to",
        expected_workflow="company-model",
    )
    assert vd["ok"] is True, vd["errors"]


@pytest.mark.asyncio
async def test_p0b_earnings_deep_update_timeout_before_model_delta_and_report_revision_fallback(
    tmp_path: Path,
) -> None:
    """Acceptance #4:
    (1) Deep-update times out after readable candidate & mapping.md, before model-delta.md:
        writeFallbackLedger records failed_stages=['model_delta'], mapping.md STILL gets
        two-round language review, model_dump/ is exempt, and real verify-done passes.
    (2) Report revision triggering shared reviseModelForReport uses label '模型联动修订'
        (isolated from _REVIEW_LABEL_RE), and timeout during report_revision calls
        writeFallbackLedger(kind='report_revision') and passes verify-done."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )
    from src.dalton.tools.check_done_tools import _REVIEW_LABEL_RE

    script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "earnings-deep-update" / "workflow.js")
    r_rel = "research/2026-11-27-deep-FY26Q3"
    params = {
        "ticker": "PDD",
        "company": "PDD Holdings",
        "quarter": "FY26Q3",
        "workspace_dir": ".",
        "release_date": "2026-11-27",
        "model_path": "model/PDD-model-v01.xlsx",
        "preview_predictions": None,
        "progress_path": f"{r_rel}/.progress.json",
    }

    ws = tmp_path / "deep_ws"
    base_v1 = ws / "model" / "PDD-model-v01.xlsx"
    v1_sha = _write_simple_valid_model(base_v1)
    # Do NOT pre-create v02 or v03! Let real CLI commands extracted from prompts build them!
    (ws / r_rel).mkdir(parents=True, exist_ok=True)
    (ws / r_rel / "mapping.md").write_text("财报披露与模型行项映射表：" + "映射条目" * 20, encoding="utf-8")
    report_file = ws / "drafts" / "2026-11-27-deep-FY26Q3.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("# PDD 深度点评\nmodel:model/PDD-model-v02.xlsx!Model - PDD!F2\n" + "内容" * 100, encoding="utf-8")
    cons_snap = ws / "data" / "consensus" / "2026-11-27_1000.json"
    cons_snap.parent.mkdir(parents=True, exist_ok=True)
    cons_snap.write_text(json.dumps({"ticker": "PDD", "as_of": "2026-11-27", "items": []}), encoding="utf-8")

    params["consensus_thresholds"] = {"revenue": 0.04}

    nine_pass = {
        "verdict": "pass",
        "summary": "ok",
        "must_fix": [],
        "items": [{"check": i, "result": "pass", "reason": "ok"} for i in range(1, 10)],
    }
    nine_revise = {
        "verdict": "revise",
        "summary": "adjust margin",
        "must_fix": ["update 2027E OPM"],
        "items": [{"check": i, "result": "pass" if i > 1 else "revise", "reason": "ok"} for i in range(1, 10)],
    }

    def _run_prompt_script(script_name: str, cmd_line: str) -> tuple[int, str]:
        import shlex as _shlex
        import subprocess as _sp

        tokens = _shlex.split(cmd_line)
        # Replace python3 .agents/skills/dalton-model/scripts/<script> with real repo script path
        real_script = str(REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "scripts" / script_name)
        proc = _sp.run(
            [sys.executable, real_script, *tokens[2:]],
            cwd=ws,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout + proc.stderr

    calls: list[tuple[str, str]] = []
    spec_host = create_autospec(WorkflowHost, instance=True)
    report_sha_after_rev: dict[str, str] = {}

    async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
        label = str(opts.get("label") or "")
        calls.append((label, prompt))
        if label == "建模准备":
            fp_out = ws / r_rel / "model_dump" / "PDD-fingerprint.json"
            fp_out.parent.mkdir(parents=True, exist_ok=True)
            fp = mfp.build_fingerprint(base_v1, ticker="PDD")
            fp_out.write_text(json.dumps(fp, ensure_ascii=False), encoding="utf-8")
            return {
                "files": [f"{r_rel}/model_dump/PDD-fingerprint.json", f"{r_rel}/mapping.md"],
                "summary": "prep",
                "mode": "update",
                "baseline_path": "model/PDD-model-v01.xlsx",
                "baseline_sha256": v1_sha,
                "fingerprint_path": f"{r_rel}/model_dump/PDD-fingerprint.json",
                "consensus_path": "data/consensus/2026-11-27_1000.json",
                "planned_output_path": "model/PDD-model-v02.xlsx",
            }
        if label == "读取运行ID":
            return {"run_id": "run-deep-1"}
        if label == "模型构建与落盘":
            plan_p = ws / r_rel / "model_dump" / "stage2_plan.json"
            plan_p.write_text(
                json.dumps(
                    {
                        "cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1050}],
                    }
                ),
                encoding="utf-8",
            )
            m_apply = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_edit\.py apply [^`]+)`", prompt)
            assert m_apply
            cmd = m_apply.group(1).replace("<edit_plan.json>", str(plan_p)) + " --skip-recalc"
            rc, out = _run_prompt_script("model_edit.py", cmd)
            assert rc == 0, out
            v2_p = ws / "model" / "PDD-model-v02.xlsx"
            assert v2_p.is_file()
            return {
                "files": ["model/PDD-model-v02.xlsx", f"{r_rel}/model_dump/coord_map.json"],
                "summary": "v02 built",
                "candidate_path": "model/PDD-model-v02.xlsx",
                "model_sha256": mfp.file_sha256(v2_p),
                "recalc_status": "ok",
            }
        if label == "核验候选落盘":
            v2_p = ws / "model" / "PDD-model-v02.xlsx"
            ok_v2 = v2_p.is_file() and medit.model_readable.check_workbook_readable(v2_p)[0]
            return {
                "path": "model/PDD-model-v02.xlsx",
                "exists": v2_p.is_file(),
                "readable": ok_v2,
                "model_sha256": mfp.file_sha256(v2_p) if ok_v2 else None,
                "run_matched": ok_v2,
            }
        if label == "一致预期对照":
            return {
                "files": ["model/PDD-model-v02.xlsx"],
                "summary": "cons skipped",
                "candidate_path": "model/PDD-model-v02.xlsx",
                "model_sha256": mfp.file_sha256(ws / "model" / "PDD-model-v02.xlsx"),
                "recalc_status": "ok",
                "consensus_status": "missing",
                "consensus_diff_generated": False,
            }
        if label == "模型本轮范围检查":
            m_chk = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_check\.py check [^`]+)`", prompt)
            assert m_chk
            rc, out = _run_prompt_script("model_check.py", m_chk.group(1))
            assert rc == 0, out
            led = json.loads((ws / r_rel / "model_dump" / "model-verification.json").read_text(encoding="utf-8"))
            return {
                "files": [
                    f"{r_rel}/model_dump/model-check.md",
                    f"{r_rel}/model_dump/model-verification.json",
                    f"{r_rel}/model_dump/remaining-issues.md",
                ],
                "summary": "check ok",
                "readable": True,
                "final_status": led["final_status"],
                "model_sha256": led["final_model_sha256"],
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md",
            }
        if label == "模型变动拆解":
            return None  # Timeout before model-delta.md is written!
        if label == "写入降级账本":
            # Parse & run the REAL write-ledger CLI directly from the prompt (reads --candidate & --failed-stages from prompt!)
            m_wl = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_check\.py write-ledger [^`]+)`", prompt)
            assert m_wl, f"missing write-ledger CLI in prompt: {prompt}"
            rc, out = _run_prompt_script("model_check.py", m_wl.group(1))
            assert rc == 0, out
            led = json.loads((ws / r_rel / "model_dump" / "model-verification.json").read_text(encoding="utf-8"))
            return {
                "files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "fallback ledger written",
                "final_status": led["final_status"],
                "model_sha256": led["final_model_sha256"],
                "run_id": led["run_id"],
            }
        if label == "审稿":
            return nine_revise
        if label == "计算联动新路径":
            m_next = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_edit\.py next-path [^`]+)`", prompt)
            assert m_next
            rc_n, out_n = _run_prompt_script("model_edit.py", m_next.group(1))
            assert rc_n == 0, out_n
            nxt_abs = Path(json.loads(out_n)["next_path"])
            nxt_rel = nxt_abs.relative_to(ws).as_posix()
            rec_p = ws / r_rel / "model_dump" / "rev-planned.json"
            rec_p.write_text(json.dumps({"next_path": nxt_rel}), encoding="utf-8")
            return {"files": [rec_p.relative_to(ws).as_posix()], "summary": "bound v03", "next_path": nxt_rel}
        if label == "模型联动修订":
            assert not _REVIEW_LABEL_RE.search(label), "reviseModelForReport label must not match review regex"
            # Verify R2 #2 & R3 #4: Dispatch 1 ONLY re-extracts fingerprint + applies model edit with pre-bound --out!
            assert "model_fingerprint.py" in prompt and "model/PDD-model-v02.xlsx" in prompt
            assert '--out "model/PDD-model-v03.xlsx"' in prompt
            assert "model_consensus.py diff" not in prompt
            m_fp = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_fingerprint\.py [^`]+)`", prompt)
            assert m_fp
            rc_fp, out_fp = _run_prompt_script("model_fingerprint.py", m_fp.group(1))
            assert rc_fp == 0, out_fp
            rev_plan = ws / r_rel / "model_dump" / "rev_plan.json"
            rev_plan.write_text(
                json.dumps({"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1088}]}),
                encoding="utf-8",
            )
            m_edit = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_edit\.py apply [^`]+)`", prompt)
            assert m_edit
            rc_e, out_e = _run_prompt_script(
                "model_edit.py",
                m_edit.group(1).replace("<report_rev_plan.json>", str(rev_plan)) + " --skip-recalc",
            )
            assert rc_e == 0, out_e
            v3_p = ws / "model" / "PDD-model-v03.xlsx"
            assert v3_p.is_file()
            return {
                "files": ["model/PDD-model-v03.xlsx", f"{r_rel}/model_dump/coord_map.json"],
                "summary": "revised to v03",
                "changed": True,
                "model_path": "model/PDD-model-v03.xlsx",
                "model_sha256": mfp.file_sha256(v3_p),
                "recalc_status": "ok",
            }
        if label == "核验联动候选落盘":
            v3_p = ws / "model" / "PDD-model-v03.xlsx"
            ok_v3 = v3_p.is_file() and medit.model_readable.check_workbook_readable(v3_p)[0]
            return {
                "path": "model/PDD-model-v03.xlsx",
                "exists": v3_p.is_file(),
                "readable": ok_v3,
                "model_sha256": mfp.file_sha256(v3_p) if ok_v3 else None,
                "run_matched": ok_v3,
            }
        if label == "模型联动差异刷新":
            assert not _REVIEW_LABEL_RE.search(label)
            assert '--consensus "data/consensus/2026-11-27_1000.json"' in prompt
            assert "--thresholds" in prompt
            return {
                "files": [f"{r_rel}/model_dump/consensus-diff.md"],
                "summary": "diff skipped",
                "consensus_diff_generated": False,
            }
        if label == "核验差异表落盘":
            return {"path": f"{r_rel}/model_dump/consensus-diff.json", "json_ok": False, "md_ok": False, "matched": False}
        if label == "模型联动报告同步":
            assert not _REVIEW_LABEL_RE.search(label)
            # Update report model: ref and measure real new report sha256 (R2 #5 & R4 #4)
            report_file.write_text(
                "# PDD 深度点评\nmodel:model/PDD-model-v03.xlsx!Model - PDD!F2\n" + "内容" * 100,
                encoding="utf-8",
            )
            new_r_sha = mfp.file_sha256(report_file)
            report_sha_after_rev["sha"] = new_r_sha
            return {
                "files": ["drafts/2026-11-27-deep-FY26Q3.md"],
                "summary": "synced report ref to v03",
                "report_sha256": new_r_sha,
            }
        if label == "核验报告同步落盘":
            new_r_sha = mfp.file_sha256(report_file)
            return {
                "path": "drafts/2026-11-27-deep-FY26Q3.md",
                "exists": report_file.is_file(),
                "synced": "model/PDD-model-v03.xlsx" in report_file.read_text(encoding="utf-8"),
                "report_sha256": new_r_sha,
            }
        if label == "模型联动修订检查":
            assert not _REVIEW_LABEL_RE.search(label)
            return None  # R2 #3: Check times out AFTER v03 was persisted!
        if label == "修订 1":
            # R2 #5: outer report revision prompt MUST check the NEW measured post-sync report hash!
            assert report_sha_after_rev["sha"] in prompt
            return {"files": ["drafts/2026-11-27-deep-FY26Q3.md"], "summary": label, "sha256": report_sha_after_rev["sha"]}
        if label == "复审 1" or label == "终稿审稿":
            return nine_pass
        if label == "产物核验":
            paths = [ln[2:].strip() for ln in prompt.splitlines() if ln.startswith("- ")]
            return {"files": [f"{p}|{'e' * 64}" for p in paths]}
        return {"files": ["drafts/2026-11-27-deep-FY26Q3.md"], "summary": label, "sha256": "c" * 64}

    spec_host.agent.side_effect = _agent
    spec_host.phase.side_effect = lambda _t: None
    spec_host.log.side_effect = lambda _m: None

    outcome = await run_workflow_script(
        script, params, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0)
    )
    assert outcome.status == "completed", outcome.error
    res = outcome.result
    assert res["ok"] is True, res["incomplete"]
    assert res["model_status"] == "delivered_with_open_issues"
    assert res["model_delta"] is None
    assert res["model_file"] == "model/PDD-model-v03.xlsx"

    # R2 #3 & #5: Verify fallback ledger recorded the NEW v03 candidate AND preserved model_delta!
    final_led = json.loads((ws / r_rel / "model_dump" / "model-verification.json").read_text(encoding="utf-8"))
    assert final_led["final_model_path"] == "model/PDD-model-v03.xlsx"
    assert final_led["final_model_sha256"] == mfp.file_sha256(ws / "model" / "PDD-model-v03.xlsx")
    assert final_led["rounds"][-1]["kind"] == "report_revision"
    assert "model_delta" in final_led["failed_stages"]
    assert "check" in final_led["failed_stages"]

    notes_prompt = next(p for lbl, p in calls if lbl == "笔记语言审查")
    assert f"{r_rel}/mapping.md" in notes_prompt
    assert f"{r_rel}/model-delta.md" not in notes_prompt
    assert "model_dump/" in notes_prompt

    ev_prompt = next(p for lbl, p in calls if lbl == "产物核验")
    assert f"{r_rel}/mapping.md.language.json" in ev_prompt
    assert f"{r_rel}/model-delta.md" not in ev_prompt
    assert f"{r_rel}/model_dump/model-verification.json" in ev_prompt

    # Real verify-done passes on the fallback ledger written during report_revision (missing model-delta.md allowed)
    vd = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=ws / r_rel,
        expected_run_id="run-deep-1",
        expected_workflow="earnings-deep-update",
    )
    assert vd["ok"] is True, vd["errors"]


@pytest.mark.asyncio
async def test_p0b_check_done_company_model_run_id_manual_exempt_dump_and_sector_inheritance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance #3 & #4 + §8.8 #5:
    - model_dump/ reports are exempt from language review (.language.json not required).
    - dalton_check_done verifies ledger run_id (passes matching, rejects mismatch), supports
      manual=true with run_id=null & freshness window.
    - In a sector workspace where PDD & JD own dedicated workspaces, dalton_check_done
      inherits model_path from args.json and applies the sector single-track pass-through."""
    import subprocess
    import shutil
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    import src.dalton.tools.check_done_tools as cd_mod
    import src.dalton.workflows.company_routing as cr_mod
    from src.dalton.coverage.identity import KIND_SECTOR, SOURCE_COMPAT, WorkspaceCoverageIdentity
    from src.dalton.publish.language import is_reviewable_deliverable

    # 1. model_dump/ is exempt from language review while deep notes remain reviewable
    assert is_reviewable_deliverable("research/2026-11-27-model-PDD/model_dump/model-check.md") is False
    assert is_reviewable_deliverable("research/2026-11-27-model-PDD/model_dump/remaining-issues.md") is False
    assert is_reviewable_deliverable("research/2026-11-27-deep-FY26Q3/mapping.md") is True

    # 2. Build a real sector workspace on disk with scripts + valid model + fallback ledger
    ws = tmp_path / "sector_ecommerce"
    scripts_dst = ws / ".agents" / "skills" / "dalton-model" / "scripts"
    scripts_dst.mkdir(parents=True, exist_ok=True)
    for src in (REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "scripts").glob("*.py"):
        shutil.copyfile(src, scripts_dst / src.name)
    xlsx_dst = ws / ".agents" / "skills" / "xlsx" / "scripts"
    xlsx_dst.mkdir(parents=True, exist_ok=True)
    for src in (REPO_ROOT / "plugins" / "langalpha_deliverables" / "skills" / "xlsx" / "scripts").glob("*.py"):
        shutil.copyfile(src, xlsx_dst / src.name)

    model_v1 = ws / "model" / "China_Ecommerce_v01.xlsx"
    _write_simple_valid_model(model_v1)
    model_v2 = ws / "model" / "China_Ecommerce_v02.xlsx"
    _write_simple_valid_model(model_v2)
    r_rel = "research/2026-11-27-model-JD"
    mchk.write_fallback_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=ws / r_rel,
        candidate_path=model_v2,
        kind="modeling",
        failed_stages=["check", "consensus"],
        run_id="run-sector-jd",
        workflow="company-model",
        fix_rounds=0,
        reason="check timed out",
    )

    # Record run status + args.json carrying model_path (so check_done inherits it!)
    run_dir = ws / ".agents" / "threads" / "11111111" / "workflows" / "run-sector-jd"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "status.json").write_text(
        json.dumps({"run_id": "run-sector-jd", "name": "company-model", "status": "completed", "updated_at": 100.0}),
        encoding="utf-8",
    )
    (run_dir / "args.json").write_text(
        json.dumps(
            {
                "ticker": "JD",
                "workspace_dir": ".",
                "as_of": "2026-11-27",
                "model_path": "model/China_Ecommerce_v01.xlsx",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "journal").mkdir(exist_ok=True)
    (run_dir / "journal" / "01.json").write_text(
        json.dumps({"label": "建模准备", "dispatches": [{"recorded_at": 50.0, "result": {"files": []}}]}),
        encoding="utf-8",
    )

    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = str(ws)

    async def _aread(path: str) -> str | None:
        p = (ws / path) if not Path(path).is_absolute() else Path(path)
        return p.read_text(encoding="utf-8") if p.is_file() else None

    async def _aglob(pattern: str, root: str = ".") -> list[str]:
        return sorted(p.relative_to(ws).as_posix() for p in ws.glob(pattern))

    run_env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }

    async def _aexec(command: str, *, timeout: int | None = None) -> Any:
        import re as _re

        # Handle GNU `stat -c %Y` portably on macOS hosts where BSD stat lacks `-c`.
        stat_m = _re.search(r"stat -c %Y\s+(.+)$", command.strip())
        if stat_m:
            raw_arg = stat_m.group(1).strip().strip("'\"")
            p = (ws / raw_arg) if not Path(raw_arg).is_absolute() else Path(raw_arg)

            class _StatR:
                output = f"{int(p.stat().st_mtime)}\n" if p.exists() else ""
                exit_code = 0 if p.exists() else 1

            return _StatR()
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=ws,
            env=run_env,
            capture_output=True,
            text=True,
            check=False,
        )

        class _R:
            output = proc.stdout + proc.stderr
            exit_code = proc.returncode

        return _R()

    backend.aread_text.side_effect = _aread
    backend.aglob_paths.side_effect = _aglob
    backend.aexecute.side_effect = _aexec

    # Mock sector workspace + JD owns dedicated company workspace + knowledge doc 'models' updated
    monkeypatch.setattr(
        cr_mod,
        "_resolve_workspace_company",
        create_autospec(
            cr_mod._resolve_workspace_company,
            return_value=(WorkspaceCoverageIdentity(KIND_SECTOR, SOURCE_COMPAT), True),
        ),
    )
    monkeypatch.setattr(
        cr_mod,
        "find_company_workspaces",
        create_autospec(
            cr_mod.find_company_workspaces,
            return_value=[{"workspace_id": "ws-jd-own", "user_id": "u1", "name": "京东-JD", "status": "running", "delegatable": True}],
        ),
    )
    monkeypatch.setattr(
        cd_mod,
        "_check_knowledge_versions",
        create_autospec(cd_mod._check_knowledge_versions, return_value={"models": True}),
    )

    # 2a. Calling dalton_check_done WITHOUT repeating model_path inherits it from args.json -> passes!
    res_ok = await cd_mod.run_check_done(
        workflow="company-model",
        params={"ticker": "JD", "workspace_dir": ".", "as_of": "2026-11-27"},
        backend=backend,
        workspace_id="ws-sector",
        user_id="u1",
        run_id="run-sector-jd",
        thread_id="11111111-2222-3333-4444-555555555555",
    )
    assert res_ok["done"] is True, res_ok["missing"]

    # 2b. Wrong run_id is rejected
    res_bad_rid = await cd_mod.run_check_done(
        workflow="company-model",
        params={"ticker": "JD", "workspace_dir": ".", "as_of": "2026-11-27", "model_path": "model/China_Ecommerce_v01.xlsx"},
        backend=backend,
        workspace_id="ws-sector",
        user_id="u1",
        run_id="run-other",
        thread_id="11111111-2222-3333-4444-555555555555",
    )
    assert res_bad_rid["done"] is False
    assert any("run_id" in m for m in res_bad_rid["missing"]), res_bad_rid["missing"]

    # 2c. Manual downgrade (no run dir, manual=true, fresh ledger with run_id=null) passes cleanly
    shutil.rmtree(ws / ".agents" / "threads")
    (ws / r_rel / "model_dump" / "model-verification.json").unlink(missing_ok=True)
    mchk.write_fallback_ledger(
        workspace_dir=ws,
        ticker="JD",
        research_dir=ws / r_rel,
        candidate_path=model_v2,
        kind="modeling",
        failed_stages=["check", "consensus"],
        run_id=None,
        workflow="company-model",
        fix_rounds=0,
        reason="manual check skipped",
    )
    res_manual = await cd_mod.run_check_done(
        workflow="company-model",
        params={
            "ticker": "JD",
            "workspace_dir": ".",
            "as_of": "2026-11-27",
            "model_path": "model/China_Ecommerce_v01.xlsx",
            "manual": True,
        },
        backend=backend,
        workspace_id="ws-sector",
        user_id="u1",
        thread_id="22222222-2222-3333-4444-555555555555",
    )
    assert res_manual["done"] is True, res_manual["missing"]

    # 2d. Without manual=true and no run dir -> rejected
    res_no_manual = await cd_mod.run_check_done(
        workflow="company-model",
        params={
            "ticker": "JD",
            "workspace_dir": ".",
            "as_of": "2026-11-27",
            "model_path": "model/China_Ecommerce_v01.xlsx",
        },
        backend=backend,
        workspace_id="ws-sector",
        user_id="u1",
        thread_id="22222222-2222-3333-4444-555555555555",
    )
    assert res_no_manual["done"] is False
    assert any("manual=true" in m for m in res_no_manual["missing"]), res_no_manual["missing"]


@pytest.mark.asyncio
async def test_p0b_cos_delegate_to_analyst_verbatim_user_request_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3.4 #2: delegate_to_analyst extracts the latest user message from state and
    appends <user_verbatim_request>...</user_verbatim_request> when not already
    quoted verbatim; chief_of_staff.md.j2 instructs serial per-company modeling dispatch."""
    from langchain_core.messages import AIMessage, HumanMessage
    from src.dalton.cos_verbatim import append_verbatim_user_request, extract_latest_user_message
    import src.tools.secretary.chief_of_staff as cos_mod
    from langgraph.types import Command

    # 1. Unit tests on helper
    raw_user = "请基于中国电商整本模型先更新 PDD 再更新 JD，保留所有历史公式与冻结窗格。"
    st = {"messages": [HumanMessage(content="早安"), AIMessage(content="你好"), HumanMessage(content=raw_user)]}
    assert extract_latest_user_message(st) == raw_user

    enriched = append_verbatim_user_request("请更新 PDD 财报模型。", st)
    assert "<user_verbatim_request>" in enriched and raw_user in enriched and "</user_verbatim_request>" in enriched
    # Already verbatim -> no duplicate wrapper
    already = append_verbatim_user_request(f"任务说明：{raw_user}", st)
    assert already == f"任务说明：{raw_user}" and "<user_verbatim_request>" not in already
    # Multimodal blocks
    st_mm = {"messages": [HumanMessage(content=[{"type": "text", "text": "保留 Arial 8 号字体"}, {"type": "image_url", "image_url": {"url": "x"}}])]}
    assert extract_latest_user_message(st_mm) == "保留 Arial 8 号字体"
    assert extract_latest_user_message({}) is None

    # 2. delegate_to_analyst forwards state into dispatch (using create_autospec)
    captured_calls: list[dict[str, Any]] = []

    async def _fake_dispatch(
        question: str,
        config: Any,
        workspace_id: str | None,
        thread_id: str | None,
        report_back: bool,
        tool_call_id: str,
        preapproved: bool = False,
        model: str | None = None,
        effort: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> Command:
        enriched_q = append_verbatim_user_request(question, state)
        captured_calls.append({"question": enriched_q, "state": state})
        return Command(update={"messages": []})

    fake_dispatch = create_autospec(cos_mod.dispatch, side_effect=_fake_dispatch)
    monkeypatch.setattr(cos_mod, "dispatch", fake_dispatch)

    await cos_mod.delegate_to_analyst.ainvoke(
        {
            "name": "delegate_to_analyst",
            "args": {"question": "更新 PDD 模型", "workspace_id": "ws-1", "state": st},
            "id": "call-cos-1",
            "type": "tool_call",
        },
        config={"configurable": {"user_id": "u1", "workspace_id": "ws-home"}},
    )
    assert len(captured_calls) == 1
    assert captured_calls[0]["state"] == st
    assert f"<user_verbatim_request>\n{raw_user}\n</user_verbatim_request>" in captured_calls[0]["question"]

    # 3. chief_of_staff.md.j2 guides serial per-company modeling dispatch
    cos_tpl = (
        REPO_ROOT
        / "src"
        / "ptc_agent"
        / "agent"
        / "prompts"
        / "templates"
        / "components"
        / "chief_of_staff.md.j2"
    ).read_text(encoding="utf-8")
    assert "DALTON-HOOK(cos-verbatim-forward)" in cos_tpl
    assert "一家公司一轮串行派发" in cos_tpl


def test_p0b_skill_docs_v2_conventions_and_core_assembly() -> None:
    """Verify updated skill docs preserve modeling conventions, require progress_path on
    company-model launch, and assemble with zero proprietary leaks in Core mode."""
    from src.dalton.content_assembly import read_assembled_text

    proprietary_blacklist = (
        "researchdata",
        "alphaengine",
        "guidepoint",
        "sales-notes",
        "tmtb",
        "funda-ai",
        "funda ai",
        "everflow",
    )
    skill_files = [
        REPO_ROOT / "plugins/dalton/skills/dalton-model/SKILL.md",
        REPO_ROOT / "plugins/dalton/skills/dalton-model/references/rollforward.md",
        REPO_ROOT / "plugins/dalton/skills/dalton-model/references/format-spec.md",
        REPO_ROOT / "plugins/dalton/skills/dalton-model/references/sanity-check.md",
        REPO_ROOT / "plugins/dalton/skills/dalton-earnings-deep/SKILL.md",
        REPO_ROOT / "plugins/dalton/skills/dalton-full-coverage/SKILL.md",
    ]
    for fp in skill_files:
        core_text = read_assembled_text(fp, capabilities=frozenset())
        assert "DALTON_CAP" not in core_text, fp
        lowered = core_text.lower()
        leaks = [w for w in proprietary_blacklist if w in lowered]
        assert not leaks, f"Core leak in {fp}: {leaks}"

    model_skill = (REPO_ROOT / "plugins/dalton/skills/dalton-model/SKILL.md").read_text(encoding="utf-8")
    assert "progress_path" in model_skill
    assert "research/{as_of}-model-{ticker}/.progress.json" in model_skill
    assert "下一完整财年" in model_skill and "至少两个完整财年" in model_skill
    assert "先独立建模" in model_skill
    assert "remaining-issues.md" in model_skill
    assert "model_edit.py" in model_skill and "model_check.py" in model_skill and "model_consensus.py" in model_skill
    # R2 #11: Manual downgrade documents the full write-ledger command with all required arguments
    for req_arg in ("write-ledger", "--workspace", "--ticker", "--research-dir", "--candidate", "--failed-stages"):
        assert req_arg in model_skill, f"SKILL.md missing {req_arg}"
    # R3 #8: Both SKILL.md and rollforward.md document full incremental targeted-fix command
    rollforward_doc = (REPO_ROOT / "plugins/dalton/skills/dalton-model/references/rollforward.md").read_text(encoding="utf-8")
    stages_js = (REPO_ROOT / "workflows/_shared/modeling_stages.js").read_text(encoding="utf-8")
    for doc_name, doc_txt in (
        ("dalton-model/SKILL.md", model_skill),
        ("references/rollforward.md", rollforward_doc),
        ("modeling_stages.js", stages_js),
    ):
        for req_flag in (
            "--ticker",
            "--plan",
            "--out",
            "--in-place",
            "--fingerprint",
            "--prior-coord-map",
            "--coord-map-out",
            "--expected-baseline-sha256",
        ):
            assert req_flag in doc_txt, f"{doc_name} missing {req_flag}"
    # Specifically verify Stage 2b consensus rework line carries --ticker, --plan, --fingerprint, --expected-baseline-sha256
    rework_line = next(ln for ln in stages_js.splitlines() if "consensus_rework_plan.json" in ln)
    for req_flag in (
        "--baseline",
        "--ticker",
        "--plan",
        "--out",
        "--in-place",
        "--fingerprint",
        "--prior-coord-map",
        "--coord-map-out",
        "--expected-baseline-sha256",
    ):
        assert req_flag in rework_line, f"Stage 2b rework line missing {req_flag}"


# ===========================================================================
# P0-b Round 2 regression & real-CLI acceptance tests (Items 1-12)
# ===========================================================================


@pytest.mark.asyncio
async def test_p0b_r2_fix_round_incremental_preserves_stage2_values_and_expanded_columns(
    tmp_path: Path,
) -> None:
    """R2 #1 & #12: Fix round targets current candidate v02 (NOT original v01),
    re-extracts fingerprint of v02, checks --expected-baseline-sha256, applies
    incremental fix via real model_edit.py --in-place --prior-coord-map, and
    preserves Stage 2 actuals and expanded columns in the real workbook."""
    import shlex as _shlex
    import subprocess as _sp
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )

    ws = tmp_path / "ws_fix_inc"
    base_v1 = ws / "model" / "PDD-model-v01.xlsx"
    base_v1.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws_rm = wb.active
    ws_rm.title = "README & Changelog"
    ws_rm["A1"] = "Version"
    ws_rm["B1"] = "Date"
    ws_rm["A2"] = "v01"
    ws_rm["B2"] = "2026-08-01"
    ws_m = wb.create_sheet("Model - PDD")
    ws_m["A1"] = "(RMB MM)"
    ws_m["E1"] = "2024"
    ws_m["F1"] = "2025"
    ws_m["G1"] = "2026E"
    ws_m["H1"] = "2027E"
    ws_m["D2"] = "Core Revenue"
    ws_m["E2"] = 800
    ws_m["F2"] = 1000
    ws_m["G2"] = "=F2*1.1"
    ws_m["H2"] = "=G2*1.1"
    ws_m["D3"] = "Total Revenue"
    ws_m["E3"] = "=E2"
    ws_m["F3"] = "=F2"
    ws_m["G3"] = "=G2"
    ws_m["H3"] = "=H2"
    wb.save(base_v1)
    v1_sha = _stamp_formula_caches(
        base_v1,
        {
            "Model - PDD": {
                "G2": "1100",
                "H2": "1210",
                "E3": "800",
                "F3": "1000",
                "G3": "1100",
                "H3": "1210",
            }
        },
    )

    r_rel = "research/2026-11-27-model-PDD"
    (ws / r_rel / "model_dump").mkdir(parents=True, exist_ok=True)
    cons_p = ws / "data" / "consensus" / "2026-11-27_1000.json"
    cons_p.parent.mkdir(parents=True, exist_ok=True)
    cons_p.write_text(json.dumps({"ticker": "PDD", "as_of": "2026-11-27", "items": []}), encoding="utf-8")

    def _run_cli(script_name: str, cmd_line: str) -> tuple[int, str]:
        toks = _shlex.split(cmd_line)
        real_s = str(REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "scripts" / script_name)
        proc = _sp.run([sys.executable, real_s, *toks[2:]], cwd=ws, capture_output=True, text=True, check=False)
        return proc.returncode, proc.stdout + proc.stderr

    script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "company-model" / "workflow.js")
    params = {
        "ticker": "PDD",
        "company": "PDD Holdings",
        "workspace_dir": ".",
        "as_of": "2026-11-27",
        "model_path": "model/PDD-model-v01.xlsx",
        "progress_path": f"{r_rel}/.progress.json",
    }

    spec_host = create_autospec(WorkflowHost, instance=True)
    fix_called = []

    async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
        label = str(opts.get("label") or "")
        if label == "建模准备":
            fp_out = ws / r_rel / "model_dump" / "PDD-fingerprint.json"
            fp = mfp.build_fingerprint(base_v1, ticker="PDD")
            fp_out.write_text(json.dumps(fp, ensure_ascii=False), encoding="utf-8")
            return {
                "files": [f"{r_rel}/model_dump/PDD-fingerprint.json"],
                "summary": "prep",
                "mode": "update",
                "baseline_path": "model/PDD-model-v01.xlsx",
                "baseline_sha256": v1_sha,
                "fingerprint_path": f"{r_rel}/model_dump/PDD-fingerprint.json",
                "consensus_path": "data/consensus/2026-11-27_1000.json",
                "planned_output_path": "model/PDD-model-v02.xlsx",
            }
        if label == "读取运行ID":
            return {"run_id": "run-fix-inc"}
        if label == "模型构建与落盘":
            # Expand a new forecast column I (2028E), update actual F2=1234, and intentionally hardcode G3=1357 (actionable C03!)
            s2_plan = ws / r_rel / "model_dump" / "s2_plan.json"
            s2_plan.write_text(
                json.dumps(
                    {
                        "ops": [{"op": "insert_columns", "sheet": "Model - PDD", "at": "I", "count": 1}],
                        "added_period_cols": {"Model - PDD": ["I"]},
                        "cell_edits": [
                            {"sheet": "Model - PDD", "cell": "I1", "value": "2028E"},
                            {"sheet": "Model - PDD", "cell": "F2", "value": 1234},
                            {"sheet": "Model - PDD", "cell": "G3", "value": 1357},
                            {"sheet": "Model - PDD", "cell": "I2", "value": "=H2*1.1"},
                            {"sheet": "Model - PDD", "cell": "I3", "value": "=I2"},
                        ],
                        "changelog_entry": {"version": "v02", "date": "2026-11-27", "summary": "Stage 2 update"},
                    }
                ),
                encoding="utf-8",
            )
            m_e = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_edit\.py apply [^`]+)`", prompt)
            assert m_e
            rc, out = _run_cli("model_edit.py", m_e.group(1).replace("<edit_plan.json>", str(s2_plan)) + " --skip-recalc")
            assert rc == 0, out
            v2_p = ws / "model" / "PDD-model-v02.xlsx"
            return {
                "files": ["model/PDD-model-v02.xlsx", f"{r_rel}/model_dump/coord_map.json"],
                "summary": "v02 built",
                "candidate_path": "model/PDD-model-v02.xlsx",
                "model_sha256": mfp.file_sha256(v2_p),
                "recalc_status": "ok",
            }
        if label == "核验候选落盘":
            v2_p = ws / "model" / "PDD-model-v02.xlsx"
            ok_v2 = v2_p.is_file() and medit.model_readable.check_workbook_readable(v2_p)[0]
            return {
                "path": "model/PDD-model-v02.xlsx",
                "exists": v2_p.is_file(),
                "readable": ok_v2,
                "model_sha256": mfp.file_sha256(v2_p) if ok_v2 else None,
                "run_matched": ok_v2,
            }
        if label == "一致预期对照":
            m_d = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_consensus\.py diff [^`]+)`", prompt)
            assert m_d
            rc, out = _run_cli("model_consensus.py", m_d.group(1))
            assert rc == 0, out
            return {
                "files": [
                    "model/PDD-model-v02.xlsx",
                    f"{r_rel}/model_dump/consensus-diff.md",
                    f"{r_rel}/model_dump/consensus-diff.json",
                ],
                "summary": "cons ok",
                "candidate_path": "model/PDD-model-v02.xlsx",
                "model_sha256": mfp.file_sha256(ws / "model" / "PDD-model-v02.xlsx"),
                "recalc_status": "ok",
                "consensus_status": "ok",
                "consensus_diff_generated": True,
            }
        if label == "模型本轮范围检查" or label.startswith("模型本轮范围复查 "):
            m_c = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_check\.py check [^`]+)`", prompt)
            assert m_c
            rc, out = _run_cli("model_check.py", m_c.group(1))
            assert rc == 0, out
            payload = json.loads(out)
            files = [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json"]
            if payload.get("remaining_issues_path"):
                files.append(f"{r_rel}/model_dump/remaining-issues.md")
            act = [f["message"] for f in payload["findings"] if f["severity_class"] == "actionable"]
            return {
                "files": files,
                "summary": f"actionable={len(act)}",
                "readable": payload["readable"],
                "final_status": payload["final_status"],
                "model_sha256": payload["candidate_sha256"],
                "actionable_count": len(act),
                "actionable_issues": act,
                "missing_required_sheets": payload.get("missing_required_sheets") or [],
                "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md" if payload.get("remaining_issues_path") else None,
                "summary_10lines": payload.get("summary_10lines") or "",
            }
        if label == "模型修改 1":
            fix_called.append(prompt)
            # Execute the REAL fingerprint + incremental apply + consensus diff from prompt!
            m_fp = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_fingerprint\.py [^`]+)`", prompt)
            assert m_fp
            rc_fp, out_fp = _run_cli("model_fingerprint.py", m_fp.group(1))
            assert rc_fp == 0, out_fp

            fix_plan = ws / r_rel / "model_dump" / "fix_plan_1.json"
            # Fix ONLY G3 back to formula "=G2" — do NOT re-specify F2=1234 or expanded col H!
            fix_plan.write_text(
                json.dumps({"cell_edits": [{"sheet": "Model - PDD", "cell": "G3", "value": "=G2"}]}),
                encoding="utf-8",
            )
            m_e = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_edit\.py apply [^`]+)`", prompt)
            assert m_e
            rc_e, out_e = _run_cli("model_edit.py", m_e.group(1).replace("<fix_plan_1.json>", str(fix_plan)) + " --skip-recalc")
            assert rc_e == 0, out_e

            m_d = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_consensus\.py diff [^`]+)`", prompt)
            assert m_d
            rc_d, out_d = _run_cli("model_consensus.py", m_d.group(1).replace("<最新候选路径>", "model/PDD-model-v02.xlsx"))
            assert rc_d == 0, out_d
            return {
                "files": [
                    "model/PDD-model-v02.xlsx",
                    f"{r_rel}/model_dump/consensus-diff.md",
                    f"{r_rel}/model_dump/consensus-diff.json",
                ],
                "summary": "fixed G3 formula",
                "candidate_path": "model/PDD-model-v02.xlsx",
                "model_sha256": mfp.file_sha256(ws / "model" / "PDD-model-v02.xlsx"),
                "recalc_status": "ok",
                "consensus_diff_generated": True,
            }
        raise AssertionError(f"unexpected label: {label}")

    spec_host.agent.side_effect = _agent
    spec_host.phase.side_effect = lambda _t: None
    spec_host.log.side_effect = lambda _m: None

    outcome = await run_workflow_script(
        script, params, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=15.0)
    )
    assert outcome.status == "completed", outcome.error
    assert len(fix_called) == 1
    assert outcome.result["ok"] is True

    # Open the REAL workbook and assert Stage 2 actual (F2=1234), expanded col (I1="2028E", I2="=H2*1.1"),
    # AND Fix Round 1 formula (G3="=G2") ALL survived!
    wb_final = openpyxl.load_workbook(ws / "model" / "PDD-model-v02.xlsx", data_only=False)
    try:
        ws_final = wb_final["Model - PDD"]
        assert ws_final["F2"].value == 1234, "Stage 2 actual value was lost in fix round!"
        assert ws_final["I1"].value == "2028E", "Stage 2 expanded column header was lost in fix round!"
        assert ws_final["I2"].value == "=H2*1.1", "Stage 2 expanded column formula was lost in fix round!"
        assert ws_final["G3"].value == "=G2", "Fix round 1 edit was not applied!"
    finally:
        wb_final.close()

    # Original read-only baseline v01 was never modified
    assert mfp.file_sha256(base_v1) == v1_sha

    vd = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=ws / r_rel,
        expected_run_id="run-fix-inc",
        expected_workflow="company-model",
    )
    assert vd["ok"] is True, vd["errors"]


@pytest.mark.asyncio
async def test_p0b_r2_downgrade_misjudgment_guards(tmp_path: Path) -> None:
    """R2 #4:
    (a) Stage 2a persists candidate, Stage 2b (consensus) times out -> NOT failed_unusable;
        delivers delivered_with_open_issues.
    (b) Fallback ledger child returns None -> ok=False, status=failed_unusable, unconfirmed
        paths excluded from artifacts.
    (c) No readable candidate -> writeFallbackLedger never called (no --candidate 'null')."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )

    script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "company-model" / "workflow.js")
    r_rel = "research/2026-11-27-model-PDD"
    params = {
        "ticker": "PDD",
        "company": "PDD Holdings",
        "workspace_dir": ".",
        "as_of": "2026-11-27",
        "progress_path": f"{r_rel}/.progress.json",
    }

    async def _run_with(handler: Any) -> tuple[Any, list[tuple[str, str]]]:
        calls: list[tuple[str, str]] = []
        spec_host = create_autospec(WorkflowHost, instance=True)

        async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
            lbl = str(opts.get("label") or "")
            calls.append((lbl, prompt))
            return handler(lbl, prompt)

        spec_host.agent.side_effect = _agent
        spec_host.phase.side_effect = lambda _t: None
        spec_host.log.side_effect = lambda _m: None
        out = await run_workflow_script(script, params, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
        assert out.status == "completed", out.error
        return out.result, calls

    # (a) Stage 2a succeeds, Stage 2b times out -> candidate is preserved, NOT failed_unusable!
    def _h_2b_timeout(label: str, prompt: str) -> Any:
        if label == "建模准备":
            return {"files": ["fp.json"], "summary": "prep", "mode": "build", "planned_output_path": "model/PDD-model-v01.xlsx"}
        if label == "读取运行ID":
            return {"run_id": "run-2b-to"}
        if label == "模型构建与落盘":
            return {"files": ["model/PDD-model-v01.xlsx"], "summary": "v01", "candidate_path": "model/PDD-model-v01.xlsx", "model_sha256": "a" * 64, "recalc_status": "ok"}
        if label == "核验候选落盘":
            return {"path": "model/PDD-model-v01.xlsx", "exists": True, "readable": True, "model_sha256": "a" * 64, "run_matched": True}
        if label == "一致预期对照":
            return None  # Consensus step timed out after candidate was already persisted!
        if label == "模型本轮范围检查":
            assert '--failed-stages "consensus"' in prompt
            return {
                "files": [
                    f"{r_rel}/model_dump/model-check.md",
                    f"{r_rel}/model_dump/model-verification.json",
                    f"{r_rel}/model_dump/remaining-issues.md",
                ],
                "summary": "delivered open",
                "readable": True,
                "final_status": "delivered_with_open_issues",
                "model_sha256": "a" * 64,
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md",
            }
        raise AssertionError(label)

    res_a, _ = await _run_with(_h_2b_timeout)
    assert res_a["ok"] is True and res_a["status"] == "delivered_with_open_issues"
    assert res_a["candidate_path"] == "model/PDD-model-v01.xlsx"
    assert "consensus" in res_a["failed_stages"]
    assert f"{r_rel}/model_dump/consensus-diff.md" not in res_a["artifacts"]

    # (b) Check times out AND fallback ledger child returns None -> ok=False, failed_unusable, no unconfirmed paths!
    def _h_fb_empty(label: str, _prompt: str) -> Any:
        if label == "建模准备":
            return {"files": ["fp.json"], "summary": "prep", "mode": "build", "planned_output_path": "model/PDD-model-v01.xlsx"}
        if label == "读取运行ID":
            return {"run_id": "run-fb-empty"}
        if label == "模型构建与落盘":
            return {"files": ["model/PDD-model-v01.xlsx"], "summary": "v01", "candidate_path": "model/PDD-model-v01.xlsx", "model_sha256": "a" * 64, "recalc_status": "ok"}
        if label == "核验候选落盘":
            return {"path": "model/PDD-model-v01.xlsx", "exists": True, "readable": True, "model_sha256": "a" * 64, "run_matched": True}
        if label == "一致预期对照":
            return {"files": ["model/PDD-model-v01.xlsx"], "summary": "ok", "candidate_path": "model/PDD-model-v01.xlsx", "model_sha256": "a" * 64, "consensus_diff_generated": False}
        if label in ("模型本轮范围检查", "写入降级账本"):
            return None
        raise AssertionError(label)

    res_b, calls_b = await _run_with(_h_fb_empty)
    assert res_b["ok"] is False
    assert res_b["status"] == "failed_unusable"
    assert f"{r_rel}/model_dump/model-verification.json" not in res_b["artifacts"]
    assert f"{r_rel}/model_dump/remaining-issues.md" not in res_b["artifacts"]
    assert all('--candidate "null"' not in p for _, p in calls_b)

    # (c) Stage 2a returns no candidate AND disk probe confirms absent -> writeFallbackLedger is NEVER called (no --candidate "null")
    def _h_no_cand(label: str, _prompt: str) -> Any:
        if label == "建模准备":
            return {"files": ["fp.json"], "summary": "prep", "mode": "build", "planned_output_path": "model/PDD-model-v01.xlsx"}
        if label == "读取运行ID":
            return {"run_id": "run-no-cand"}
        if label == "模型构建与落盘":
            return None
        if label == "核验候选落盘":
            return {"path": "model/PDD-model-v01.xlsx", "exists": False, "readable": False, "model_sha256": None}
        raise AssertionError(label)

    res_c, calls_c = await _run_with(_h_no_cand)
    assert res_c["ok"] is False and res_c["status"] == "failed_unusable"
    assert "写入降级账本" not in [lbl for lbl, _ in calls_c]


@pytest.mark.asyncio
async def test_p0b_r2_resume_rebinds_new_run_id_without_replaying_old_run_id(
    tmp_path: Path,
) -> None:
    """R2 #7: Live run_id probe is never replayed by WorkflowJournal, and a prior
    run's model-verification.json ledger is never replayed under a new run_id so
    resume re-binds the terminal ledger to the new run_id."""
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from src.dalton.workflows.journal import WorkflowJournal, artifact_records, prompt_digest

    ws = tmp_path / "ws_resume"
    r_rel = "research/2026-11-27-model-PDD"
    cand_p = ws / "model" / "PDD-model-v01.xlsx"
    _write_simple_valid_model(cand_p)

    # Run 1 writes a ledger bound to run-old-1
    mchk.write_fallback_ledger(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=ws / r_rel,
        candidate_path=cand_p,
        kind="modeling",
        failed_stages=["check", "consensus"],
        run_id="run-old-1",
        workflow="company-model",
        fix_rounds=0,
        reason="initial run",
    )
    ledger_rel = f"{r_rel}/model_dump/model-verification.json"
    rem_rel = f"{r_rel}/model_dump/remaining-issues.md"
    led_sha = mfp.file_sha256(ws / ledger_rel)
    rem_sha = mfp.file_sha256(ws / rem_rel)

    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = str(ws)
    fs_extra: dict[str, str] = {}

    async def _aread(path: str) -> str | None:
        if path in fs_extra:
            return fs_extra[path]
        p = ws / path
        return p.read_text(encoding="utf-8") if p.is_file() else None

    async def _awrite(path: str, text: str) -> bool:
        fs_extra[path] = text
        return True

    async def _aglob(pattern: str, root: str = ".") -> list[str]:
        from fnmatch import fnmatch

        return sorted(k for k in fs_extra if fnmatch(k, pattern))

    backend.aread_text.side_effect = _aread
    backend.awrite_text.side_effect = _awrite
    backend.aglob_paths.side_effect = _aglob
    backend.aexecute = None

    # 1. artifact_records refuses to mark {run_id}-only live query as replayable
    assert await artifact_records(backend, {"run_id": "run-old-1"}) is None

    # 2. Seed Run 1's journal with: (a) live run_id probe, (b) stale ledger dispatch, (c) legacy stage with run_id
    src_dir = ".agents/threads/12345678/workflows/run-old-1"
    rid_prompt = "read run_id"
    chk_prompt = "run model_check"
    s2_prompt = "build model"
    fs_extra[f"{src_dir}/journal/01.json"] = json.dumps(
        {
            "label": "读取运行ID",
            "dispatches": [
                {
                    "label": "读取运行ID",
                    "prompt_sha256": prompt_digest(rid_prompt),
                    "status": "ok",
                    "replayable": True,
                    "result": {"run_id": "run-old-1"},
                    "artifacts": [],
                }
            ],
        }
    )
    fs_extra[f"{src_dir}/journal/02.json"] = json.dumps(
        {
            "label": "模型本轮范围检查",
            "dispatches": [
                {
                    "label": "模型本轮范围检查",
                    "prompt_sha256": prompt_digest(chk_prompt),
                    "status": "ok",
                    "replayable": True,
                    "result": {"files": [ledger_rel, rem_rel], "summary": "ok"},
                    "artifacts": [
                        {"path": ledger_rel, "sha256": led_sha},
                        {"path": rem_rel, "sha256": rem_sha},
                    ],
                }
            ],
        }
    )
    cand_sha_txt = hashlib.sha256((ws / rem_rel).read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    fs_extra[f"{src_dir}/journal/03.json"] = json.dumps(
        {
            "label": "模型构建与落盘",
            "dispatches": [
                {
                    "label": "模型构建与落盘",
                    "prompt_sha256": prompt_digest(s2_prompt),
                    "status": "ok",
                    "replayable": True,
                    "result": {"files": [rem_rel], "summary": "ok", "run_id": "run-old-1"},
                    "artifacts": [{"path": rem_rel, "sha256": cand_sha_txt}],
                }
            ],
        }
    )

    journal_new = WorkflowJournal(
        backend,
        base_rel=".agents/threads/12345678/workflows/run-new-2",
        workflow="company-model",
        run_id="run-new-2",
        progress_path=f"{r_rel}/.progress.json",
        journal_source=src_dir,
    )

    # Live run_id probe MUST NOT replay
    assert await journal_new.find_replayable("读取运行ID", rid_prompt) is None
    # Terminal ledger bound to run-old-1 MUST NOT replay under run-new-2
    assert await journal_new.find_replayable("模型本轮范围检查", chk_prompt) is None
    # Legacy replayable stage carrying old run_id gets rebound to run-new-2
    replayed_s2 = await journal_new.find_replayable("模型构建与落盘", s2_prompt)
    assert replayed_s2 is not None
    assert replayed_s2["result"]["run_id"] == "run-new-2"


@pytest.mark.asyncio
async def test_p0b_r2_full_coverage_missing_required_sheets_blocks_before_draft(
    tmp_path: Path,
) -> None:
    """R2 #8: Full-coverage validates required sheets per model-spec.md before memo
    drafting; missing sheets are recorded in C01 actionable + remaining-issues.md
    and full-coverage/workflow.js blocks via gateBlock instead of silently drafting."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )

    ws = tmp_path / "ws_fc_sheets"
    r_rel = "research/2026-11-27-fc"
    cand = ws / "model" / "PDD-model-v01.xlsx"
    _write_simple_valid_model(cand)

    # 1. Direct model_check.py check on full-coverage (update mode) catches missing 4 Dalton sheets
    chk_upd = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=ws / r_rel,
        candidate_path=cand,
        workflow="full-coverage",
        mode="update",
        run_id="run-fc-sheets",
    )
    assert set(chk_upd["missing_required_sheets"]) == {"Facts", "Guidance vs Actual", "Street vs House", "Checks"}
    rem_txt = (ws / r_rel / "model_dump" / "remaining-issues.md").read_text(encoding="utf-8")
    assert "Facts" in rem_txt and "Checks" in rem_txt

    # 2. Direct model_check.py check on full-coverage (build mode) catches missing 15-sheet spec sheets
    chk_bld = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=ws / r_rel,
        candidate_path=cand,
        workflow="full-coverage",
        mode="build",
        run_id="run-fc-sheets",
    )
    assert "Cover" in chk_bld["missing_required_sheets"] and "DCF" in chk_bld["missing_required_sheets"]
    assert "Valuation Summary" in chk_bld["missing_required_sheets"]

    # 3. full-coverage/workflow.js blocks BEFORE 成稿 when required sheets are missing
    script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "full-coverage" / "workflow.js")
    params = {
        "ticker": "PDD",
        "company": "PDD Holdings",
        "workspace_dir": ".",
        "as_of": "2026-11-27",
        "progress_path": f"{r_rel}/.progress.json",
    }
    labels_seen: list[str] = []
    spec_host = create_autospec(WorkflowHost, instance=True)

    async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
        label = str(opts.get("label") or "")
        labels_seen.append(label)
        if label == "支线核验":
            ps = [ln[2:].split("（")[0].strip() for ln in prompt.splitlines() if ln.startswith("- ")]
            return {"files": [f"{p}|ok" for p in ps]}
        if label in ("数字底表", "frame") or label.startswith("支线 "):
            return {"files": [f"{r_rel}/facts.md"], "summary": "ok"}
        if label == "建模准备":
            return {"files": [f"{r_rel}/model_dump/PDD-fingerprint.json"], "summary": "prep", "mode": "update", "baseline_path": "model/PDD-model-v01.xlsx", "planned_output_path": "model/PDD-model-v02.xlsx"}
        if label == "读取运行ID":
            return {"run_id": "run-fc-sheets"}
        if label == "模型构建与落盘":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "v02", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": chk_upd["candidate_sha256"], "recalc_status": "ok"}
        if label == "核验候选落盘":
            return {"path": "model/PDD-model-v02.xlsx", "exists": True, "readable": True, "model_sha256": chk_upd["candidate_sha256"], "run_matched": True}
        if label == "一致预期对照":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "cons", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": chk_upd["candidate_sha256"], "consensus_diff_generated": False}
        if label == "完整覆盖页签核验":
            return {
                "candidate_path": "model/PDD-model-v02.xlsx",
                "status": "missing",
                "sheet_names": ["Model - PDD"],
                "missing_required_sheets": chk_upd["missing_required_sheets"],
                "summary": "missing required sheets",
            }
        if label == "写入降级账本":
            return {
                "files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "fb",
                "final_status": "delivered_with_open_issues",
                "model_sha256": chk_upd["candidate_sha256"],
                "run_id": "run-fc-sheets",
            }
        if label == "模型本轮范围检查" or label.startswith("模型本轮范围复查 "):
            return {
                "files": [
                    f"{r_rel}/model_dump/model-check.md",
                    f"{r_rel}/model_dump/model-verification.json",
                    f"{r_rel}/model_dump/remaining-issues.md",
                ],
                "summary": "missing required sheets",
                "readable": True,
                "final_status": "delivered_with_open_issues",
                "model_sha256": chk_upd["candidate_sha256"],
                "actionable_count": 1,
                "actionable_issues": ["full-coverage missing required sheets"],
                "missing_required_sheets": chk_upd["missing_required_sheets"],
                "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md",
            }
        raise AssertionError(f"unexpected label (must not reach drafting): {label}")

    spec_host.agent.side_effect = _agent
    spec_host.phase.side_effect = lambda _t: None
    spec_host.log.side_effect = lambda _m: None

    outcome = await run_workflow_script(script, params, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
    assert outcome.status == "completed", outcome.error
    res = outcome.result
    assert res["ok"] is False
    assert "缺少必需工作表" in res["blocked"]
    assert "Facts" in res["blocked"] and "Checks" in res["blocked"]
    assert "remaining-issues.md" in res["blocked"]
    assert "成稿" not in labels_seen


@pytest.mark.asyncio
async def test_p0b_r2_sector_two_companies_serial_pdd_then_jd_real_cli_and_check_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 #12: Real two-company serial update (PDD -> JD) on a multi-company sector
    workbook executing prompt-generated CLI commands, verifying version chain
    v01 -> v02 -> v03 preserves PDD's edits when JD updates, and passing both
    real verify-done and dalton_check_done."""
    import shlex as _shlex
    import shutil
    import subprocess as _sp
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )
    import src.dalton.tools.check_done_tools as cd_mod
    import src.dalton.workflows.company_routing as cr_mod
    from src.dalton.coverage.identity import KIND_SECTOR, SOURCE_COMPAT, WorkspaceCoverageIdentity

    ws = tmp_path / "sector_serial"
    scripts_dst = ws / ".agents" / "skills" / "dalton-model" / "scripts"
    scripts_dst.mkdir(parents=True, exist_ok=True)
    for src in (REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "scripts").glob("*.py"):
        shutil.copyfile(src, scripts_dst / src.name)
    xlsx_dst = ws / ".agents" / "skills" / "xlsx" / "scripts"
    xlsx_dst.mkdir(parents=True, exist_ok=True)
    for src in (REPO_ROOT / "plugins" / "langalpha_deliverables" / "skills" / "xlsx" / "scripts").glob("*.py"):
        shutil.copyfile(src, xlsx_dst / src.name)

    v1 = ws / "model" / "China_Ecommerce_v01.xlsx"
    v1.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws_rm = wb.active
    ws_rm.title = "README & Changelog"
    ws_rm["A1"] = "Version"
    ws_rm["A2"] = "v01"
    for tk, val in (("PDD", 1000), ("JD", 2000)):
        w = wb.create_sheet(f"Model - {tk}")
        w["A1"] = "(RMB MM)"
        w["E1"] = "2025"
        w["F1"] = "2026E"
        w["G1"] = "2027E"
        w["H1"] = "2028E"
        w["D2"] = "Total Revenue"
        w["E2"] = val
        w["F2"] = "=E2*1.1"
        w["G2"] = "=F2*1.1"
        w["H2"] = "=G2*1.1"
    wb.save(v1)
    _stamp_formula_caches(
        v1,
        {
            "Model - PDD": {"F2": "1100", "G2": "1210", "H2": "1331"},
            "Model - JD": {"F2": "2200", "G2": "2420", "H2": "2662"},
        },
    )

    cons_p = ws / "data" / "consensus" / "2026-11-27_1000.json"
    cons_p.parent.mkdir(parents=True, exist_ok=True)
    cons_p.write_text(json.dumps({"as_of": "2026-11-27", "items": []}), encoding="utf-8")

    run_env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"}

    def _run_cli(script_name: str, cmd_line: str) -> tuple[int, str]:
        toks = _shlex.split(cmd_line)
        real_s = str(scripts_dst / script_name)
        proc = _sp.run([sys.executable, real_s, *toks[2:]], cwd=ws, env=run_env, capture_output=True, text=True, check=False)
        return proc.returncode, proc.stdout + proc.stderr

    wf_script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "company-model" / "workflow.js")

    async def _run_company(ticker: str, start_rel: str, expected_sha: str | None, new_rev: int, run_id: str) -> dict[str, Any]:
        r_rel = f"research/2026-11-27-model-{ticker}"
        (ws / r_rel / "model_dump").mkdir(parents=True, exist_ok=True)
        params = {
            "ticker": ticker,
            "company": ticker,
            "workspace_dir": ".",
            "as_of": "2026-11-27",
            "model_path": start_rel,
            "expected_baseline_sha256": expected_sha,
            "progress_path": f"{r_rel}/.progress.json",
        }
        spec_host = create_autospec(WorkflowHost, instance=True)
        cand_rel_box: dict[str, str] = {}

        async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
            label = str(opts.get("label") or "")
            if label == "建模准备":
                m_fp = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_fingerprint\.py [^`]+)`", prompt)
                assert m_fp
                cmd = m_fp.group(1).replace("<起点模型>", start_rel)
                rc, out = _run_cli("model_fingerprint.py", cmd)
                assert rc == 0, out
                return {
                    "files": [f"{r_rel}/model_dump/{ticker}-fingerprint.json"],
                    "summary": "prep",
                    "mode": "update",
                    "baseline_path": start_rel,
                    "baseline_sha256": mfp.file_sha256(ws / start_rel),
                    "fingerprint_path": f"{r_rel}/model_dump/{ticker}-fingerprint.json",
                    "consensus_path": "data/consensus/2026-11-27_1000.json",
                    "planned_output_path": medit.next_model_output_path(
                        ws, ticker, baseline_path=ws / start_rel
                    ).resolve().relative_to(ws.resolve()).as_posix(),
                }
            if label == "读取运行ID":
                return {"run_id": run_id}
            if label == "模型构建与落盘":
                plan_p = ws / r_rel / "model_dump" / "plan.json"
                plan_p.write_text(
                    json.dumps(
                        {
                            "cell_edits": [{"sheet": f"Model - {ticker}", "cell": "E2", "value": new_rev}],
                            "changelog_entry": {"version": f"{ticker}-upd", "date": "2026-11-27", "summary": f"Update {ticker}"},
                        }
                    ),
                    encoding="utf-8",
                )
                m_e = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_edit\.py apply [^`]+)`", prompt)
                assert m_e
                rc, out = _run_cli("model_edit.py", m_e.group(1).replace("<edit_plan.json>", str(plan_p)) + " --skip-recalc")
                assert rc == 0, out
                res_e = json.loads(out)
                cand_rel = Path(res_e["candidate_path"]).resolve().relative_to(ws.resolve()).as_posix()
                cand_rel_box["path"] = cand_rel
                return {
                    "files": [cand_rel, f"{r_rel}/model_dump/coord_map.json"],
                    "summary": f"saved {cand_rel}",
                    "candidate_path": cand_rel,
                    "model_sha256": res_e["candidate_sha256"],
                    "recalc_status": "ok",
                }
            if label == "核验候选落盘":
                cand_rel = cand_rel_box["path"]
                cand_p_real = ws / cand_rel
                ok_c = cand_p_real.is_file() and medit.model_readable.check_workbook_readable(cand_p_real)[0]
                return {
                    "path": cand_rel,
                    "exists": cand_p_real.is_file(),
                    "readable": ok_c,
                    "model_sha256": mfp.file_sha256(cand_p_real) if ok_c else None,
                    "run_matched": ok_c,
                }
            if label == "一致预期对照":
                m_d = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_consensus\.py diff [^`]+)`", prompt)
                assert m_d
                rc, out = _run_cli("model_consensus.py", m_d.group(1))
                assert rc == 0, out
                cand_rel = cand_rel_box["path"]
                return {
                    "files": [cand_rel, f"{r_rel}/model_dump/consensus-diff.md", f"{r_rel}/model_dump/consensus-diff.json"],
                    "summary": "cons ok",
                    "candidate_path": cand_rel,
                    "model_sha256": mfp.file_sha256(ws / cand_rel),
                    "recalc_status": "ok",
                    "consensus_status": "ok",
                    "consensus_diff_generated": True,
                }
            if label == "模型本轮范围检查":
                m_c = re.search(r"`(python3 \.agents/skills/dalton-model/scripts/model_check\.py check [^`]+)`", prompt)
                assert m_c
                rc, out = _run_cli("model_check.py", m_c.group(1))
                assert rc == 0, out
                res_c = json.loads(out)
                files = [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json"]
                if res_c.get("remaining_issues_path"):
                    files.append(f"{r_rel}/model_dump/remaining-issues.md")
                return {
                    "files": files,
                    "summary": "checked",
                    "readable": res_c["readable"],
                    "final_status": res_c["final_status"],
                    "model_sha256": res_c["candidate_sha256"],
                    "actionable_count": res_c["counts"]["actionable"],
                    "actionable_issues": [],
                    "missing_required_sheets": [],
                    "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md" if res_c.get("remaining_issues_path") else None,
                    "summary_10lines": res_c.get("summary_10lines") or "",
                }
            raise AssertionError(label)

        spec_host.agent.side_effect = _agent
        spec_host.phase.side_effect = lambda _t: None
        spec_host.log.side_effect = lambda _m: None
        out = await run_workflow_script(wf_script, params, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=15.0))
        assert out.status == "completed", out.error
        assert out.result["ok"] is True

        # Record workflow run dir so real dalton_check_done verifies everything end-to-end
        run_dir = ws / ".agents" / "threads" / "33333333" / "workflows" / run_id
        (run_dir / "journal").mkdir(parents=True, exist_ok=True)
        (run_dir / "status.json").write_text(
            json.dumps({"run_id": run_id, "name": "company-model", "status": "completed", "updated_at": 100.0}),
            encoding="utf-8",
        )
        (run_dir / "args.json").write_text(
            json.dumps({"ticker": ticker, "workspace_dir": ".", "as_of": "2026-11-27", "model_path": start_rel}),
            encoding="utf-8",
        )
        (run_dir / "journal" / "01.json").write_text(
            json.dumps({"label": "建模准备", "dispatches": [{"recorded_at": 50.0, "result": {"files": []}}]}),
            encoding="utf-8",
        )
        return out.result

    # 1) PDD updates v01 -> v02 (E2: 1000 -> 1555)
    res_pdd = await _run_company("PDD", "model/China_Ecommerce_v01.xlsx", mfp.file_sha256(v1), 1555, "run-serial-pdd")
    assert res_pdd["candidate_path"] == "model/China_Ecommerce_v02.xlsx"
    # 2) JD updates v02 -> v03 (E2: 2000 -> 2666), passing v02 path + sha256
    res_jd = await _run_company("JD", res_pdd["candidate_path"], res_pdd["model_sha256"], 2666, "run-serial-jd")
    assert res_jd["candidate_path"] == "model/China_Ecommerce_v03.xlsx"

    # Assert v03 preserves BOTH PDD's v02 update AND JD's v03 update!
    wb_v3 = openpyxl.load_workbook(ws / "model" / "China_Ecommerce_v03.xlsx", data_only=False)
    try:
        assert wb_v3["Model - PDD"]["E2"].value == 1555, "JD update overwrote PDD's v02 edit!"
        assert wb_v3["Model - JD"]["E2"].value == 2666
    finally:
        wb_v3.close()

    # Run real dalton_check_done (including real verify-done + real sector symlink routing) for BOTH companies!
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = str(ws)

    async def _aread(path: str) -> str | None:
        p = (ws / path) if not Path(path).is_absolute() else Path(path)
        return p.read_text(encoding="utf-8") if p.is_file() else None

    async def _aglob(pattern: str, root: str = ".") -> list[str]:
        return sorted(p.relative_to(ws).as_posix() for p in ws.glob(pattern))

    async def _aexec(command: str, *, timeout: int | None = None) -> Any:
        proc = _sp.run(["bash", "-c", command], cwd=ws, env=run_env, capture_output=True, text=True, check=False)

        class _R:
            output = proc.stdout + proc.stderr
            exit_code = proc.returncode

        return _R()

    backend.aread_text.side_effect = _aread
    backend.aglob_paths.side_effect = _aglob
    backend.aexecute.side_effect = _aexec

    monkeypatch.setattr(
        cr_mod,
        "_resolve_workspace_company",
        create_autospec(
            cr_mod._resolve_workspace_company,
            return_value=(WorkspaceCoverageIdentity(KIND_SECTOR, SOURCE_COMPAT), True),
        ),
    )
    monkeypatch.setattr(
        cr_mod,
        "find_company_workspaces",
        create_autospec(
            cr_mod.find_company_workspaces,
            return_value=[{"workspace_id": "ws-member", "user_id": "u1", "name": "成员工作区", "status": "running", "delegatable": True}],
        ),
    )
    monkeypatch.setattr(
        cd_mod,
        "_check_knowledge_versions",
        create_autospec(cd_mod._check_knowledge_versions, return_value={"models": True}),
    )

    for tk, rid in (("PDD", "run-serial-pdd"), ("JD", "run-serial-jd")):
        cd_res = await cd_mod.run_check_done(
            workflow="company-model",
            params={"ticker": tk, "workspace_dir": ".", "as_of": "2026-11-27"},
            backend=backend,
            workspace_id="ws-sector",
            user_id="u1",
            run_id=rid,
            thread_id="33333333-4444-5555-6666-777777777777",
        )
        assert cd_res["done"] is True, (tk, cd_res["missing"])


def test_p0b_r2_cos_verbatim_pre_injection_whitespace_system_image_tag_escape_and_attachments() -> None:
    """R2 #10:
    - Captures raw pre-injection user text preserving leading/trailing whitespace & order.
    - Ignores server-injected <system-reminder> (directive / widget / watchlist).
    - System/notification turns and image-only turns do NOT append or leak older turns.
    - Escapes closing </user_verbatim_request> tags inside verbatim text.
    - Includes ONLY attachment filenames (basenames), never paths or URLs."""
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage, HumanMessage
    from src.dalton.cos_verbatim import (
        COS_TURN_VERBATIM_CONFIG_KEY,
        append_verbatim_user_request,
        capture_turn_verbatim_request,
    )
    from src.server.handlers.chat.request_prep import inject_inline_reminders

    # 1. Pre-injection capture preserves exact leading/trailing spaces & excludes later <system-reminder>
    raw_text = "  请更新 PDD 模型，保留 </user_verbatim_request> 与 </USER_VERBATIM_REQUEST> 原样。  "
    raw_msgs = [{"role": "user", "content": raw_text}]
    req = SimpleNamespace(
        hitl_response=None,
        report_source=None,
        wakeup=None,
        market_target=None,
        origin=None,
        additional_context=[
            SimpleNamespace(type="chat_upload", name="/tmp/uploads/China_Ecommerce_v01.xlsx"),
            SimpleNamespace(type="file", name="https://cdn.example.com/path/Q3_Release.pdf?token=secret"),
        ],
    )
    turn_atts = SimpleNamespace(files=[SimpleNamespace(name="work/uploads/notes.csv")])

    captured = capture_turn_verbatim_request(
        request=req,
        raw_messages=raw_msgs,
        turn_attachments=turn_atts,
        from_server=False,
        is_checkpoint_replay=False,
    )
    assert captured is not None
    assert captured["text"] == raw_text  # exact leading/trailing spaces preserved!
    assert captured["attachments"] == ["notes.csv", "China_Ecommerce_v01.xlsx", "Q3_Release.pdf"]

    # Now mutate raw_msgs with server inline reminder (watchlist / directive) — captured payload must stay clean!
    inject_inline_reminders(raw_msgs, ["\n<system-reminder>PRIVATE WATCHLIST: SECRET</system-reminder>"])
    assert "PRIVATE WATCHLIST" in raw_msgs[-1]["content"]

    cfg = {"configurable": {COS_TURN_VERBATIM_CONFIG_KEY: captured}}
    out = append_verbatim_user_request("更新 PDD 模型", state={"messages": [HumanMessage(content="old")]}, config=cfg)
    assert out.count("</user_verbatim_request>") == 1
    assert "&lt;/user_verbatim_request&gt;" in out
    assert "  请更新 PDD 模型" in out
    assert "原样。  " in out
    assert "PRIVATE WATCHLIST" not in out
    assert "China_Ecommerce_v01.xlsx" in out and "/tmp/uploads/" not in out
    assert "Q3_Release.pdf" in out and "token=secret" not in out and "https://" not in out
    assert "notes.csv" in out and "work/uploads/" not in out

    # 2. System notification trigger (from_server=True / wakeup / report_source) -> None, no append, no old-turn leak!
    older_state = {"messages": [HumanMessage(content="上一轮用户原话"), AIMessage(content="收到")]}
    for sys_req, srv_flag in (
        (SimpleNamespace(hitl_response=None, report_source=None, wakeup=None, market_target=None, origin=None, additional_context=[]), True),
        (SimpleNamespace(hitl_response=None, report_source={"id": "r1"}, wakeup=None, market_target=None, origin=None, additional_context=[]), False),
        (SimpleNamespace(hitl_response=None, report_source=None, wakeup={"id": "w1"}, market_target=None, origin=None, additional_context=[]), False),
        (SimpleNamespace(hitl_response=None, report_source=None, wakeup=None, market_target=None, origin={"type": "automation"}, additional_context=[]), False),
    ):
        cap_sys = capture_turn_verbatim_request(
            request=sys_req,
            raw_messages=[{"role": "user", "content": "系统唤醒通知正文"}],
            from_server=srv_flag,
        )
        assert cap_sys is None
        res_sys = append_verbatim_user_request(
            "派发任务",
            state=older_state,
            config={"configurable": {COS_TURN_VERBATIM_CONFIG_KEY: cap_sys}},
        )
        assert res_sys == "派发任务"

    # 3. Image-only user turn -> None, no append, no fallback to earlier turn's text!
    img_only_msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]}]
    cap_img = capture_turn_verbatim_request(request=req, raw_messages=img_only_msgs)
    assert cap_img is None
    assert (
        append_verbatim_user_request(
            "派发任务",
            state=older_state,
            config={"configurable": {COS_TURN_VERBATIM_CONFIG_KEY: cap_img}},
        )
        == "派发任务"
    )
    # Even without config (state-only fallback), an image-only latest HumanMessage must NOT walk back to earlier turns!
    img_state = {
        "messages": [
            HumanMessage(content="旧轮消息不应泄露"),
            AIMessage(content="好"),
            HumanMessage(content=[{"type": "image_url", "image_url": {"url": "x"}}]),
            AIMessage(content="calling tool"),
        ]
    }
    assert append_verbatim_user_request("派发任务", state=img_state) == "派发任务"


# ===========================================================================
# P0-b Round 3 regression & acceptance tests (Items 2-7)
# ===========================================================================


def test_p0b_r3_verify_done_bidirectional_failed_stages_rejects_top_only(tmp_path: Path) -> None:
    """R3 #2: verify-done enforces bidirectional consistency between top-level
    failed_stages and last-round failed_stages, and only exempts evidence for
    stages verified in the last round (top-only failed_stages cannot bypass)."""
    ws = tmp_path / "ws_bidir"
    rdir = ws / "research" / "2026-11-27-model-PDD"
    dump = rdir / "model_dump"
    dump.mkdir(parents=True, exist_ok=True)
    cand = ws / "model" / "PDD-model-v01.xlsx"
    sha = _write_simple_valid_model(cand)

    # (a) Spoofed ledger: last round failed_stages=[] while top-level marks check & consensus failed;
    # remaining-issues.md includes both stage names and sha prefix; both reports are missing!
    (dump / "remaining-issues.md").write_text(
        f"> - `[阶段降级记录]` failed_stages: check, consensus (candidate `{sha[:12]}`)\n",
        encoding="utf-8",
    )
    spoofed_top_only = {
        "schema_version": 2,
        "workflow": "company-model",
        "ticker": "PDD",
        "run_id": "run-r3-bidir",
        "calc_engine": "libreoffice",
        "independent_verify": False,
        "enabled_stages": ["check", "consensus"],
        "fix_rounds": 0,
        "failed_stages": ["check", "consensus"],
        "rounds": [
            {
                "round_index": 1,
                "kind": "modeling",
                "candidate_path": "model/PDD-model-v01.xlsx",
                "model_sha256": sha,
                "failed_stages": [],
                "evidence": {},
            }
        ],
        "final_status": "delivered_with_open_issues",
        "final_model_path": "model/PDD-model-v01.xlsx",
        "final_model_sha256": sha,
        "remaining_issues_path": "research/2026-11-27-model-PDD/model_dump/remaining-issues.md",
    }
    (dump / "model-verification.json").write_text(json.dumps(spoofed_top_only), encoding="utf-8")

    vd_top = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=rdir,
        expected_run_id="run-r3-bidir",
        expected_workflow="company-model",
    )
    assert vd_top["ok"] is False
    err_blob = "\n".join(vd_top["errors"])
    assert "missing from last round failed_stages" in err_blob
    assert "missing non-empty" in err_blob  # check & consensus evidence were NOT exempted!

    # (b) Reverse mismatch: last round marks check failed, top-level failed_stages=[] -> rejected
    spoofed_round_only = dict(spoofed_top_only)
    spoofed_round_only["failed_stages"] = []
    spoofed_round_only["rounds"] = [{**spoofed_top_only["rounds"][0], "failed_stages": ["check", "consensus"]}]
    (dump / "model-verification.json").write_text(json.dumps(spoofed_round_only), encoding="utf-8")
    vd_rnd = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=rdir,
        expected_run_id="run-r3-bidir",
        expected_workflow="company-model",
    )
    assert vd_rnd["ok"] is False
    assert any("missing from top-level failed_stages" in e for e in vd_rnd["errors"])

    # (c) Deep-update note exemption also refuses top-only model_delta
    deep_top_only = dict(spoofed_top_only)
    deep_top_only["workflow"] = "earnings-deep-update"
    deep_top_only["failed_stages"] = ["check", "consensus", "model_delta"]
    deep_top_only["rounds"] = [{**spoofed_top_only["rounds"][0], "failed_stages": ["check", "consensus"]}]
    (rdir / "mapping.md").write_text("财报行项映射" * 20, encoding="utf-8")
    (dump / "remaining-issues.md").write_text(
        f"> - `[阶段降级记录]` failed_stages: check, consensus, model_delta (candidate `{sha[:12]}`)\n",
        encoding="utf-8",
    )
    (dump / "model-verification.json").write_text(json.dumps(deep_top_only), encoding="utf-8")
    vd_deep = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=rdir,
        expected_run_id="run-r3-bidir",
        expected_workflow="earnings-deep-update",
    )
    assert vd_deep["ok"] is False
    assert any("model_delta" in e for e in vd_deep["errors"])


@pytest.mark.asyncio
async def test_p0b_r3_full_coverage_independent_sheet_gate_complete_missing_unverified_and_verify_done(
    tmp_path: Path,
) -> None:
    """R3 #3: full-coverage independently reads candidate .xlsx sheet names on disk
    before drafting (ignoring omitted/empty missing_required_sheets from check child
    and check timeouts), classifies complete / missing / unverified, blocks drafting
    and writes remaining-issues.md on missing or unverified, and verify-done re-checks."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )

    script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "full-coverage" / "workflow.js")
    r_rel = "research/2026-11-27-fc"
    params = {
        "ticker": "PDD",
        "company": "PDD Holdings",
        "workspace_dir": ".",
        "as_of": "2026-11-27",
        "progress_path": f"{r_rel}/.progress.json",
    }

    # Case A: Check child returns missing_required_sheets=[] (omitted/spoofed) on a candidate
    # that is ACTUALLY missing required full-coverage sheets on disk -> independent sheet gate
    # reads real disk workbook via model_check.py check-sheets -> blocks BEFORE drafting!
    ws_a = tmp_path / "ws_fc_a"
    cand_a = ws_a / "model" / "PDD-model-v01.xlsx"
    sha_a = _write_simple_valid_model(cand_a)

    # First verify check_required_sheets directly on missing-sheet workbook
    direct_miss = mchk.check_required_sheets(candidate_path=cand_a, workflow="full-coverage", mode="update")
    assert direct_miss["status"] == "missing"
    assert set(direct_miss["missing_required_sheets"]) == {"Facts", "Guidance vs Actual", "Street vs House", "Checks"}

    # Also verify terminal verify_done rejects missing sheets even on a write-ledger fallback!
    mchk.write_fallback_ledger(
        workspace_dir=ws_a,
        ticker="PDD",
        research_dir=ws_a / r_rel,
        candidate_path=cand_a,
        workflow="full-coverage",
        mode="update",
        failed_stages=["check", "consensus"],
        run_id="run-fc-r3-a",
    )
    rem_a = (ws_a / r_rel / "model_dump" / "remaining-issues.md").read_text(encoding="utf-8")
    assert "Facts" in rem_a and "Checks" in rem_a
    vd_miss = mchk.verify_done(
        workspace_dir=ws_a,
        ticker="PDD",
        research_dir=ws_a / r_rel,
        expected_run_id="run-fc-r3-a",
        expected_workflow="full-coverage",
    )
    assert vd_miss["ok"] is False
    assert any("missing required worksheets on disk" in e for e in vd_miss["errors"]), vd_miss["errors"]

    async def _run_fc(handler: Any) -> tuple[Any, list[str]]:
        labels: list[str] = []
        spec_host = create_autospec(WorkflowHost, instance=True)

        async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
            lbl = str(opts.get("label") or "")
            labels.append(lbl)
            return handler(lbl, prompt)

        spec_host.agent.side_effect = _agent
        spec_host.phase.side_effect = lambda _t: None
        spec_host.log.side_effect = lambda _m: None
        out = await run_workflow_script(script, params, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
        assert out.status == "completed", out.error
        return out.result, labels

    def _base_fc(lbl: str, prompt: str, *, mode: str = "update") -> Any:
        if lbl == "支线核验":
            ps = [ln[2:].split("（")[0].strip() for ln in prompt.splitlines() if ln.startswith("- ")]
            return {"files": [f"{p}|ok" for p in ps]}
        if lbl in ("数字底表", "frame") or lbl.startswith("支线 "):
            return {"files": [f"{r_rel}/facts.md"], "summary": "ok"}
        if lbl == "建模准备":
            return {"files": ["fp.json"], "summary": "prep", "mode": mode, "baseline_path": "model/PDD-model-v00.xlsx" if mode == "update" else None, "planned_output_path": "model/PDD-model-v01.xlsx"}
        if lbl == "读取运行ID":
            return {"run_id": "run-fc-r3"}
        if lbl == "模型构建与落盘":
            return {"files": ["model/PDD-model-v01.xlsx"], "summary": "v01", "candidate_path": "model/PDD-model-v01.xlsx", "model_sha256": sha_a, "recalc_status": "ok"}
        if lbl == "核验候选落盘":
            return {"path": "model/PDD-model-v01.xlsx", "exists": True, "readable": True, "model_sha256": sha_a, "run_matched": True}
        if lbl == "一致预期对照":
            return {"files": ["model/PDD-model-v01.xlsx"], "summary": "cons", "candidate_path": "model/PDD-model-v01.xlsx", "model_sha256": sha_a, "consensus_diff_generated": False}
        return None

    # Case A: Check child times out -> fallback ledger -> independent sheet probe runs real check_required_sheets -> blocks!
    def _h_check_timeout_missing(lbl: str, prompt: str) -> Any:
        base_r = _base_fc(lbl, prompt)
        if base_r is not None:
            return base_r
        if lbl == "模型本轮范围检查":
            return None  # Timeout!
        if lbl == "写入降级账本":
            res = mchk.write_fallback_ledger(
                workspace_dir=ws_a,
                ticker="PDD",
                research_dir=ws_a / r_rel,
                candidate_path=cand_a,
                workflow="full-coverage",
                mode="update",
                failed_stages=["check", "consensus"],
                run_id="run-fc-r3",
            )
            return {"files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"], "summary": "fb", "final_status": res["final_status"], "model_sha256": res["model_sha256"], "run_id": "run-fc-r3"}
        if lbl == "完整覆盖页签核验":
            return mchk.check_required_sheets(candidate_path=cand_a, workflow="full-coverage", mode="update")
        raise AssertionError(f"must not reach drafting: {lbl}")

    res_a, labels_a = await _run_fc(_h_check_timeout_missing)
    assert res_a["ok"] is False
    assert res_a["sheet_gate_status"] == "missing"
    assert "Facts" in res_a["missing_required_sheets"] and "Checks" in res_a["missing_required_sheets"]
    assert "成稿" not in labels_a

    # Case B: Check child returns pass with missing_required_sheets=[], AND independent sheet probe times out (None) -> unverified -> blocks!
    def _h_unverified(lbl: str, prompt: str) -> Any:
        base_r = _base_fc(lbl, prompt)
        if base_r is not None:
            return base_r
        if lbl == "模型本轮范围检查":
            return {"files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json"], "summary": "ok", "readable": True, "final_status": "pass", "model_sha256": sha_a, "actionable_count": 0, "missing_required_sheets": []}
        if lbl == "完整覆盖页签核验":
            return None  # Timed out -> unverified!
        if lbl == "写入降级账本":
            assert "sheet_check" in prompt
            return {"files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"], "summary": "unverified fb", "final_status": "delivered_with_open_issues", "model_sha256": sha_a, "run_id": "run-fc-r3"}
        raise AssertionError(f"must not reach drafting on unverified: {lbl}")

    res_b, labels_b = await _run_fc(_h_unverified)
    assert res_b["ok"] is False
    assert res_b["sheet_gate_status"] == "unverified"
    assert "未核验" in res_b["blocked"]
    assert "成稿" not in labels_b

    # Case C: Workbook WITH all required update sheets passes both check_required_sheets AND verify_done!
    ws_c = tmp_path / "ws_fc_c"
    cand_c = ws_c / "model" / "PDD-model-v01.xlsx"
    _write_simple_valid_model(cand_c)
    wb_c = openpyxl.load_workbook(cand_c)
    for req_sh in ("Facts", "Guidance vs Actual", "Street vs House", "Checks"):
        if req_sh not in wb_c.sheetnames:
            ws_sh = wb_c.create_sheet(req_sh)
            ws_sh["A1"] = req_sh
    wb_c.save(cand_c)
    wb_c.close()
    direct_ok = mchk.check_required_sheets(candidate_path=cand_c, workflow="full-coverage", mode="update")
    assert direct_ok["status"] == "complete" and direct_ok["missing_required_sheets"] == []
    mchk.write_fallback_ledger(
        workspace_dir=ws_c,
        ticker="PDD",
        research_dir=ws_c / r_rel,
        candidate_path=cand_c,
        workflow="full-coverage",
        mode="update",
        failed_stages=["check", "consensus"],
        run_id="run-fc-r3-c",
    )
    vd_ok = mchk.verify_done(
        workspace_dir=ws_c,
        ticker="PDD",
        research_dir=ws_c / r_rel,
        expected_run_id="run-fc-r3-c",
        expected_workflow="full-coverage",
        expected_mode="update",
    )
    assert vd_ok["ok"] is True, vd_ok["errors"]


@pytest.mark.asyncio
async def test_p0b_r3_report_revision_prebinds_next_path_splits_sync_and_records_actual_failed_stage(
    tmp_path: Path,
) -> None:
    """R3 #4: reviseModelForReport pre-computes next path into --out, Dispatch 1 ONLY
    edits/persists model, Dispatch 2 runs consensus diff + report sync, Dispatch 3 checks.
    Timeouts after model publish record the confirmed new candidate & actual failed stages."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )

    stages_src = (REPO_ROOT / "workflows" / "_shared" / "modeling_stages.js").read_text(encoding="utf-8")
    harness_js = f"{stages_src}\nreturn await reviseModelForReport(args)\n"

    # Case 1: Dispatch 1 succeeds (confirms v03), Dispatch 2 (diff + report sync) times out,
    # Dispatch 3 (check) times out -> fallback ledger uses CONFIRMED v03 and records
    # actual incomplete stages: consensus, report_sync, check!
    ws1 = tmp_path / "rev_ws1"
    r_rel = "research/2026-11-27-deep-FY26Q3"
    v2 = ws1 / "model" / "PDD-model-v02.xlsx"
    v3 = ws1 / "model" / "PDD-model-v03.xlsx"
    v2_sha = _write_simple_valid_model(v2)
    v3_sha = _write_simple_valid_model(v3)
    (ws1 / r_rel).mkdir(parents=True, exist_ok=True)
    (ws1 / r_rel / "mapping.md").write_text("映射内容" * 30, encoding="utf-8")
    (ws1 / r_rel / "model-delta.md").write_text("变动拆解" * 30, encoding="utf-8")

    rev_args = {
        "workspaceDir": ".",
        "ticker": "PDD",
        "researchDir": r_rel,
        "progressPath": f"{r_rel}/.progress.json",
        "workflow": "earnings-deep-update",
        "currentModelPath": "model/PDD-model-v02.xlsx",
        "currentModelSha256": v2_sha,
        "currentStatus": "pass",
        "currentArtifacts": [],
        "consensusPath": "data/consensus/snap.json",
        "priorFailedStages": [],
        "runId": "run-rev-r3",
        "reportPath": "drafts/2026-11-27-deep-FY26Q3.md",
        "reviewFeedback": {"verdict": "revise", "must_fix": ["adjust margin"]},
        "phaseLabel": "审稿",
    }

    async def _run_rev(handler: Any) -> tuple[Any, list[tuple[str, str]]]:
        seen: list[tuple[str, str]] = []
        spec_host = create_autospec(WorkflowHost, instance=True)

        async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
            lbl = str(opts.get("label") or "")
            seen.append((lbl, prompt))
            return handler(lbl, prompt)

        spec_host.agent.side_effect = _agent
        spec_host.phase.side_effect = lambda _t: None
        spec_host.log.side_effect = lambda _m: None
        out = await run_workflow_script(harness_js, rev_args, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
        assert out.status == "completed", out.error
        return out.result, seen

    def _h_sync_timeout(lbl: str, prompt: str) -> Any:
        if lbl == "计算联动新路径":
            return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
        if lbl == "模型联动修订":
            assert '--out "model/PDD-model-v03.xlsx"' in prompt
            assert "model_consensus.py diff" not in prompt
            return {"files": ["model/PDD-model-v03.xlsx"], "summary": "v03 saved", "changed": True, "model_path": "model/PDD-model-v03.xlsx", "model_sha256": v3_sha, "recalc_status": "ok"}
        if lbl == "核验联动候选落盘":
            return {"path": "model/PDD-model-v03.xlsx", "exists": True, "readable": True, "model_sha256": v3_sha, "run_matched": True}
        if lbl == "模型联动差异刷新":
            return None  # Timed out during diff refresh!
        if lbl == "核验差异表落盘":
            return {"path": f"{r_rel}/model_dump/consensus-diff.json", "json_ok": False, "md_ok": False, "matched": False}
        if lbl == "模型联动报告同步":
            return None  # Timed out during report sync!
        if lbl == "核验报告同步落盘":
            return {"path": "drafts/2026-11-27-deep-FY26Q3.md", "exists": False, "synced": False, "report_sha256": None}
        if lbl == "模型联动修订检查":
            assert '--candidate "model/PDD-model-v03.xlsx"' in prompt
            assert "consensus" in prompt and "report_sync" in prompt
            return None  # Check also timed out!
        if lbl == "写入降级账本":
            assert '--candidate "model/PDD-model-v03.xlsx"' in prompt
            m_fs = re.search(r'--failed-stages "([^"]+)"', prompt)
            assert m_fs
            fs = [s.strip() for s in m_fs.group(1).split(",") if s.strip()]
            assert set(fs) >= {"consensus", "report_sync", "check"}
            res = mchk.write_fallback_ledger(
                workspace_dir=ws1,
                ticker="PDD",
                research_dir=ws1 / r_rel,
                candidate_path=v3,
                baseline_path=v2,
                kind="report_revision",
                failed_stages=fs,
                run_id="run-rev-r3",
                workflow="earnings-deep-update",
            )
            return {"files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"], "summary": "fb", "final_status": res["final_status"], "model_sha256": res["model_sha256"], "run_id": "run-rev-r3"}
        raise AssertionError(lbl)

    res1, _ = await _run_rev(_h_sync_timeout)
    assert res1["ok"] is True and res1["modelPath"] == "model/PDD-model-v03.xlsx"
    assert res1["modelSha256"] == v3_sha
    assert set(res1["failedStages"]) >= {"consensus", "report_sync", "check"}
    vd1 = mchk.verify_done(workspace_dir=ws1, ticker="PDD", research_dir=ws1 / r_rel, expected_run_id="run-rev-r3", expected_workflow="earnings-deep-update")
    assert vd1["ok"] is True, vd1["errors"]

    # Case 2: Dispatch 1 times out BEFORE v03 is written -> stays on v02, records 'report_revision' (NOT 'check')!
    def _h_edit_timeout_no_file(lbl: str, prompt: str) -> Any:
        if lbl == "计算联动新路径":
            return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
        if lbl == "模型联动修订":
            return None
        if lbl == "核验联动候选落盘":
            return {"path": "model/PDD-model-v03.xlsx", "exists": False, "readable": False, "model_sha256": None}
        if lbl == "写入降级账本":
            assert '--candidate "model/PDD-model-v02.xlsx"' in prompt
            assert '--failed-stages "report_revision"' in prompt
            return {"files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"], "summary": "fb", "final_status": "delivered_with_open_issues", "model_sha256": v2_sha, "run_id": "run-rev-r3"}
        raise AssertionError(lbl)

    res2, _ = await _run_rev(_h_edit_timeout_no_file)
    assert res2["ok"] is True and res2["modelPath"] == "model/PDD-model-v02.xlsx"
    assert res2["failedStages"] == ["report_revision"]


@pytest.mark.asyncio
async def test_p0b_r3_initial_modeling_published_file_subagent_empty_return_not_failed_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 #5: Initial modeling binds output path before edit, model_edit.py publishes
    readable xlsx + coord_map before recalc, and if Stage 2a returns empty/None after
    publishing the file (e.g. timeout during recalc), parent probes disk and degrades
    to delivered_with_open_issues (recalc) instead of failed_unusable."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )

    # 1. Direct model_edit.py check: when recalc crashes/times out mid-flight, candidate
    # is ALREADY published & readable on disk AND coord_map.json is ALREADY written!
    ws_py = tmp_path / "ws_edit_crash"
    base = ws_py / "model" / "PDD-model-v01.xlsx"
    _write_simple_valid_model(base)
    cand = ws_py / "model" / "PDD-model-v02.xlsx"
    cm_out = ws_py / "research" / "coord_map.json"

    boom_recalc = create_autospec(
        medit.model_xlsx_hooks.recalculate_keep_cache,
        side_effect=RuntimeError("simulated timeout kill during recalc"),
    )
    monkeypatch.setattr(medit.model_xlsx_hooks, "recalculate_keep_cache", boom_recalc)

    with pytest.raises(RuntimeError, match="simulated timeout kill"):
        medit.apply_edit_plan(
            baseline_path=base,
            ticker="PDD",
            plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1234}]},
            output_path=cand,
            workspace_dir=ws_py,
            coord_map_out=cm_out,
            mode="update",
        )
    assert cand.is_file()
    ok_r, msg_r = medit.model_readable.check_workbook_readable(cand)
    assert ok_r, msg_r
    assert cm_out.is_file()
    pre_cm = json.loads(cm_out.read_text(encoding="utf-8"))
    assert pre_cm["candidate_sha256"] == mfp.file_sha256(cand)

    # 2. Full company-model workflow: Stage 2a publishes real readable v01 then returns None!
    ws = tmp_path / "ws_wf_published_empty"
    r_rel = "research/2026-11-27-model-PDD"
    script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "company-model" / "workflow.js")
    params = {
        "ticker": "PDD",
        "company": "PDD Holdings",
        "workspace_dir": ".",
        "as_of": "2026-11-27",
        "progress_path": f"{r_rel}/.progress.json",
    }

    spec_host = create_autospec(WorkflowHost, instance=True)
    labels: list[str] = []

    async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
        lbl = str(opts.get("label") or "")
        labels.append(lbl)
        if lbl == "建模准备":
            return {"files": [f"{r_rel}/model_dump/PDD-fingerprint.json"], "summary": "prep", "mode": "build", "planned_output_path": "model/PDD-model-v01.xlsx"}
        if lbl == "读取运行ID":
            return {"run_id": "run-r3-pub-empty"}
        if lbl == "模型构建与落盘":
            assert '--out "model/PDD-model-v01.xlsx"' in prompt
            # File IS published on disk before subagent times out during recalc and returns None!
            _write_simple_valid_model(ws / "model" / "PDD-model-v01.xlsx")
            return None
        if lbl == "核验候选落盘":
            p = ws / "model" / "PDD-model-v01.xlsx"
            ok, _ = medit.model_readable.check_workbook_readable(p)
            return {"path": "model/PDD-model-v01.xlsx", "exists": p.is_file(), "readable": ok, "model_sha256": mfp.file_sha256(p), "run_matched": ok}
        if lbl == "一致预期对照":
            return None
        if lbl == "模型本轮范围检查":
            assert '--failed-stages "recalc,consensus"' in prompt
            res = mchk.write_check_and_ledger(
                workspace_dir=ws,
                ticker="PDD",
                research_dir=ws / r_rel,
                candidate_path=ws / "model" / "PDD-model-v01.xlsx",
                workflow="company-model",
                mode="build",
                run_id="run-r3-pub-empty",
                failed_stages=["recalc", "consensus"],
            )
            return {
                "files": [
                    f"{r_rel}/model_dump/model-check.md",
                    f"{r_rel}/model_dump/model-verification.json",
                    f"{r_rel}/model_dump/remaining-issues.md",
                ],
                "summary": "delivered open",
                "readable": True,
                "final_status": res["final_status"],
                "model_sha256": res["candidate_sha256"],
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md",
            }
        raise AssertionError(lbl)

    spec_host.agent.side_effect = _agent
    spec_host.phase.side_effect = lambda _t: None
    spec_host.log.side_effect = lambda _m: None

    out = await run_workflow_script(script, params, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
    assert out.status == "completed", out.error
    res = out.result
    assert res["ok"] is True
    assert res["status"] == "delivered_with_open_issues"
    assert res["candidate_path"] == "model/PDD-model-v01.xlsx"
    assert "recalc" in res["failed_stages"]
    assert "核验候选落盘" in labels
    vd = mchk.verify_done(workspace_dir=ws, ticker="PDD", research_dir=ws / r_rel, expected_run_id="run-r3-pub-empty", expected_workflow="company-model")
    assert vd["ok"] is True, vd["errors"]


@pytest.mark.asyncio
async def test_p0b_r3_resume_after_real_write_snapshot_replays_without_incrementing_version(
    tmp_path: Path,
) -> None:
    """R3 #6: Executing real model_consensus.py write-snapshot mutates candidate .xlsx
    and coord_map.json in place. Resuming after interruption (either immediately after
    write-snapshot before Stage 2b ends, or after Stage 2b completes) replays Stage 2a
    without rebuilding or incrementing the model version; external tampering still invalidates."""
    from types import SimpleNamespace as _NS
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from src.dalton.workflows.journal import WorkflowJournal, prompt_digest

    ws = tmp_path / "ws_resume_snap"
    r_rel = "research/2026-11-27-model-PDD"
    dump_rel = f"{r_rel}/model_dump"
    base_p = ws / "model" / "PDD-model-v01.xlsx"
    _write_simple_valid_model(base_p)
    cand_rel = "model/PDD-model-v02.xlsx"
    cand_p = ws / cand_rel
    cm_rel = f"{dump_rel}/coord_map.json"
    cm_p = ws / cm_rel

    # Step 1 (Run 1, Stage 2a): Real model_edit.py apply produces v02.xlsx (H1) & coord_map.json (CM1)
    edit_res = medit.apply_edit_plan(
        baseline_path=base_p,
        ticker="PDD",
        plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1111}]},
        output_path=cand_p,
        workspace_dir=ws,
        coord_map_out=cm_p,
        skip_recalc=True,
        mode="update",
    )
    h1 = edit_res["candidate_sha256"]
    cm1 = mfp.file_sha256(cm_p)

    # Step 2 (Run 1, Stage 2b): Real model_consensus.py write-snapshot mutates v02.xlsx (H1->H2) & coord_map.json (CM1->CM2)!
    cons_json = ws / "data" / "consensus" / "2026-11-27_1000.json"
    cons_json.parent.mkdir(parents=True, exist_ok=True)
    cons_json.write_text(
        json.dumps(
            {
                "ticker": "PDD",
                "as_of_date": "2026-11-27",
                "source_type": "Market Consensus",
                "items": [{"period": "2026E", "metric": "Total Revenue", "consensus": 1200.0}],
            }
        ),
        encoding="utf-8",
    )
    rc_snap = mcons.main(
        [
            "write-snapshot",
            "--model",
            str(cand_p),
            "--consensus",
            str(cons_json),
            "--coord-map",
            str(cm_p),
        ]
    )
    assert rc_snap == 0
    h2 = mfp.file_sha256(cand_p)
    cm2 = mfp.file_sha256(cm_p)
    assert h2 != h1 and cm2 != cm1, "write-snapshot must actually mutate both xlsx and coord_map on disk"
    assert (ws / dump_rel / "consensus-snapshot-receipt.json").is_file()

    # Build autospec backend wired to real disk files + real sha256sum via aexecute
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = str(ws)
    extra_fs: dict[str, str] = {}

    async def _aread(path: str) -> str | None:
        if path in extra_fs:
            return extra_fs[path]
        p = ws / path
        return p.read_text(encoding="utf-8") if p.is_file() else None

    async def _awrite(path: str, text: str) -> bool:
        extra_fs[path] = text
        return True

    async def _aglob(pattern: str, root: str = ".") -> list[str]:
        from fnmatch import fnmatch

        return sorted(k for k in extra_fs if fnmatch(k, pattern))

    async def _aexec(cmd: str) -> Any:
        import shlex as _shlex

        # Emulate Linux sandbox `sha256sum -- <paths>` over real files on disk
        if "sha256sum -- " in cmd:
            tail = cmd.split("sha256sum -- ", 1)[1]
            lines = []
            for rel in _shlex.split(tail):
                p = ws / rel
                if not p.is_file():
                    return _NS(exit_code=1, output="")
                lines.append(f"{mfp.file_sha256(p)}  {rel}")
            return _NS(exit_code=0, output="\n".join(lines) + "\n")
        return _NS(exit_code=1, output="")

    backend.aread_text.side_effect = _aread
    backend.awrite_text.side_effect = _awrite
    backend.aglob_paths.side_effect = _aglob
    backend.aexecute.side_effect = _aexec

    src_dir = ".agents/threads/12345678/workflows/run-snap-1"
    s2a_prompt = "stage 2a build v02"
    s2b_prompt = "stage 2b consensus"
    extra_fs[f"{src_dir}/journal/02_stage2a.json"] = json.dumps(
        {
            "label": "模型构建与落盘",
            "dispatches": [
                {
                    "seq": 2,
                    "label": "模型构建与落盘",
                    "prompt_sha256": prompt_digest(s2a_prompt),
                    "status": "ok",
                    "replayable": True,
                    "result": {
                        "files": [cand_rel, cm_rel],
                        "summary": "v02 built",
                        "candidate_path": cand_rel,
                        "model_sha256": h1,
                        "recalc_status": "ok",
                    },
                    "artifacts": [
                        {"path": cand_rel, "sha256": h1},
                        {"path": cm_rel, "sha256": cm1},
                    ],
                }
            ],
        }
    )

    # Case A: Interrupted RIGHT AFTER real write-snapshot (before Stage 2b finished -> NO ok entry for Stage 2b!)
    journal_resume_a = WorkflowJournal(
        backend,
        base_rel=".agents/threads/12345678/workflows/run-snap-2a",
        workflow="company-model",
        run_id="run-snap-2a",
        progress_path=f"{r_rel}/.progress.json",
        journal_source=src_dir,
    )
    replayed_s2a = await journal_resume_a.find_replayable("模型构建与落盘", s2a_prompt)
    assert replayed_s2a is not None, "Stage 2a MUST replay via verified write-snapshot receipt without rebuilding!"
    assert replayed_s2a["result"]["candidate_path"] == cand_rel
    assert not (ws / "model" / "PDD-model-v03.xlsx").exists()

    # Case B: Interrupted AFTER Stage 2b finished (Stage 2b recorded ok with H2, CM2) -> BOTH Stage 2a & 2b replay!
    extra_fs[f"{src_dir}/journal/03_stage2b.json"] = json.dumps(
        {
            "label": "一致预期对照",
            "dispatches": [
                {
                    "seq": 3,
                    "label": "一致预期对照",
                    "prompt_sha256": prompt_digest(s2b_prompt),
                    "status": "ok",
                    "replayable": True,
                    "result": {
                        "files": [cand_rel, cm_rel],
                        "summary": "cons ok",
                        "candidate_path": cand_rel,
                        "model_sha256": h2,
                        "consensus_diff_generated": True,
                    },
                    "artifacts": [
                        {"path": cand_rel, "sha256": h2},
                        {"path": cm_rel, "sha256": cm2},
                    ],
                }
            ],
        }
    )
    journal_resume_b = WorkflowJournal(
        backend,
        base_rel=".agents/threads/12345678/workflows/run-snap-2b",
        workflow="company-model",
        run_id="run-snap-2b",
        progress_path=f"{r_rel}/.progress.json",
        journal_source=src_dir,
    )
    assert await journal_resume_b.find_replayable("模型构建与落盘", s2a_prompt) is not None
    assert await journal_resume_b.find_replayable("一致预期对照", s2b_prompt) is not None
    assert not (ws / "model" / "PDD-model-v03.xlsx").exists()

    # Case C: External tampering AFTER write-snapshot MUST invalidate replay!
    cand_p.write_bytes(cand_p.read_bytes() + b"tampered")
    journal_resume_c = WorkflowJournal(
        backend,
        base_rel=".agents/threads/12345678/workflows/run-snap-2c",
        workflow="company-model",
        run_id="run-snap-2c",
        progress_path=f"{r_rel}/.progress.json",
        journal_source=src_dir,
    )
    assert await journal_resume_c.find_replayable("模型构建与落盘", s2a_prompt) is None
    assert await journal_resume_c.find_replayable("一致预期对照", s2b_prompt) is None


@pytest.mark.asyncio
async def test_p0b_r3_cos_hitl_approval_resume_reuses_card_verbatim_and_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 #7: Approval card question (with <user_verbatim_request> + attachment basenames)
    is persisted in the checkpoint interrupt keyed by tool_call_id and reused verbatim on
    HITL resume even when cos_turn_verbatim_request is None and state has no raw text."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace as _NS
    import aiohttp
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from src.dalton.cos_verbatim import (
        COS_APPROVED_DISPATCH_PAYLOADS_CONFIG_KEY,
        COS_TURN_VERBATIM_CONFIG_KEY,
        load_approved_dispatch_payloads,
    )
    import src.tools.secretary.dispatch as disp_mod
    import src.server.services.report_back.handoff.reserve as reserve_mod
    import src.config.settings as cfg_settings

    raw_verbatim = "  请保持 Arial 8 号字体\n不要改历史列公式  "
    raw_attachments = ["Q3_earnings.pdf", "PDD_v01.xlsx"]
    ws_id = "11111111-1111-1111-1111-111111111111"
    home_ws_id = "22222222-2222-2222-2222-222222222222"
    call_id = "call-cos-hitl-1"
    other_call_id = "call-cos-hitl-2"

    # Common mocks via create_autospec
    async def _fake_owned_ws(wid: str, uid: str, *, is_continuation: bool = False) -> dict[str, Any]:
        return {"workspace_id": wid, "user_id": uid, "name": "PDD"}

    async def _fake_unreachable(_wid: str) -> None:
        return None

    monkeypatch.setattr(disp_mod, "_owned_workspace", create_autospec(disp_mod._owned_workspace, side_effect=_fake_owned_ws))
    monkeypatch.setattr(disp_mod, "_unreachable_computer", create_autospec(disp_mod._unreachable_computer, side_effect=_fake_unreachable))
    monkeypatch.setattr(cfg_settings, "background_dispatch_requires_token", lambda: False)

    # Turn 1: Before HITL pause -> hitl_confirm receives exact card payload with verbatim + attachments
    captured_card: dict[str, Any] = {}

    class _PauseSignal(Exception):
        pass

    def _hitl_pause(action_type: str, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        assert action_type == "ptc_agent"
        captured_card.update(payload)
        raise _PauseSignal()

    orig_hitl_confirm = disp_mod.hitl_confirm
    monkeypatch.setattr(disp_mod, "hitl_confirm", create_autospec(orig_hitl_confirm, side_effect=_hitl_pause))

    turn1_config = {
        "configurable": {
            "user_id": "user-1",
            "workspace_id": home_ws_id,
            "thread_id": "home-thread-1",
            COS_TURN_VERBATIM_CONFIG_KEY: {"text": raw_verbatim, "attachments": raw_attachments},
        }
    }
    with pytest.raises(_PauseSignal):
        await disp_mod.dispatch(
            question="更新 PDD 模型",
            config=turn1_config,
            workspace_id=ws_id,
            thread_id=None,
            report_back=True,
            tool_call_id=call_id,
            preapproved=False,
            state={"messages": []},
        )

    card_question = captured_card["question"]
    assert "<user_verbatim_request>" in card_question
    assert raw_verbatim in card_question
    assert "Attachments: Q3_earnings.pdf, PDD_v01.xlsx" in card_question

    # Simulate LangGraph checkpoint pending_writes storing two ptc_agent interrupts
    other_card_q = "更新 JD 模型\n\n<user_verbatim_request>\nJD 原话\n</user_verbatim_request>"
    fake_cp_tuple = _NS(
        pending_writes=[
            (
                "task-1",
                "__interrupt__",
                [
                    _NS(id="intr-1", value={"action_requests": [{"type": "ptc_agent", **captured_card}]}),
                    _NS(
                        id="intr-2",
                        value={
                            "action_requests": [
                                {
                                    "type": "ptc_agent",
                                    "tool_call_id": other_call_id,
                                    "question": other_card_q,
                                    "workspace_id": ws_id,
                                    "thread_id": None,
                                }
                            ]
                        },
                    ),
                ],
            )
        ]
    )
    checkpointer = create_autospec(BaseCheckpointSaver, instance=True)
    checkpointer.aget_tuple.return_value = fake_cp_tuple

    # Turn 2 (HITL resume): ptc_run loads approved payloads by tool_call_id while cos_turn_verbatim_request=None!
    loaded = await load_approved_dispatch_payloads(
        checkpointer,
        "home-thread-1",
        hitl_response={"intr-1": {"decisions": [{"type": "approve"}]}},
    )
    assert call_id in loaded and other_call_id not in loaded
    assert loaded[call_id]["question"] == card_question

    # Also verify loading BOTH when both are answered keeps each tool_call_id isolated
    loaded_both = await load_approved_dispatch_payloads(
        checkpointer,
        "home-thread-1",
        hitl_response={"intr-1": {}, "intr-2": {}},
    )
    assert loaded_both[call_id]["question"] == card_question
    assert loaded_both[other_call_id]["question"] == other_card_q

    # Now resume dispatch() with cos_turn_verbatim_request=None AND empty state (simulating HITL resume!)
    monkeypatch.setattr(
        disp_mod,
        "hitl_confirm",
        create_autospec(orig_hitl_confirm, return_value=(True, {"decisions": [{"type": "approve"}]})),
    )

    @asynccontextmanager
    async def _fake_reserve(*_a: Any, **_k: Any):
        slot = create_autospec(reserve_mod._DispatchSlot, instance=True)
        slot.error = None
        slot.wired = True
        slot.dispatch_gen = 1
        slot.commit.return_value = None
        yield slot

    monkeypatch.setattr(reserve_mod, "reserve", _fake_reserve)

    posted_payloads: list[dict[str, Any]] = []

    class _FakeResp:
        status = 200

        async def json(self) -> dict[str, Any]:
            return {"status": "dispatched", "run_id": "bg-run-1"}

        async def __aenter__(self) -> "_FakeResp":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    class _FakeClientSession:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def post(self, _url: str, *, json: dict[str, Any], **_k: Any) -> _FakeResp:
            posted_payloads.append(json)
            return _FakeResp()

        async def __aenter__(self) -> "_FakeClientSession":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    monkeypatch.setattr(aiohttp, "ClientSession", create_autospec(aiohttp.ClientSession, side_effect=_FakeClientSession))

    resume_config = {
        "configurable": {
            "user_id": "user-1",
            "workspace_id": home_ws_id,
            "thread_id": "home-thread-1",
            COS_TURN_VERBATIM_CONFIG_KEY: None,  # Exact HITL resume condition from ptc_run.py!
            COS_APPROVED_DISPATCH_PAYLOADS_CONFIG_KEY: loaded,
        }
    }
    cmd = await disp_mod.dispatch(
        question="更新 PDD 模型",
        config=resume_config,
        workspace_id=ws_id,
        thread_id=None,
        report_back=True,
        tool_call_id=call_id,
        preapproved=False,
        state={"messages": []},
    )
    assert len(posted_payloads) == 1
    sent_question = posted_payloads[0]["messages"][0]["content"]
    assert sent_question == card_question, "Approval card question MUST equal actual dispatched body verbatim!"
    assert raw_verbatim in sent_question
    assert "Attachments: Q3_earnings.pdf, PDD_v01.xlsx" in sent_question
    assert cmd is not None


@pytest.mark.asyncio
async def test_p0b_r4_sheet_gate_real_cli_schema_complete_reaches_draft_and_missing_always_rewrites_c01(
    tmp_path: Path,
) -> None:
    """R4 #2 (Codex #2, Grok #1 & #3):
    1) Real CLI output of `model_check.py check-sheets` validates verbatim against
       `_MODEL_SHEET_GATE_SCHEMA` (same field names: `candidate_path`, `summary`, etc.);
    2) Complete workbook on disk enters drafting (`成稿`) even when prior check child
       returned stale non-empty `missing_required_sheets` (`priorMissing`);
    3) `priorMissing` is only used when probe times out or returns no sheet names;
    4) Missing sheets ALWAYS rewrite `remaining-issues.md` with C01 missing sheet names
       even when `remaining_issues_path` already exists and `failed_stages` lacks `check`."""
    import jsonschema
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )

    script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "full-coverage" / "workflow.js")
    r_rel = "research/2026-11-27-fc"
    params = {
        "ticker": "PDD",
        "company": "PDD Holdings",
        "workspace_dir": ".",
        "as_of": "2026-11-27",
        "progress_path": f"{r_rel}/.progress.json",
    }

    # Build a complete update-mode workbook on disk (Facts, Guidance vs Actual, Street vs House, Checks)
    ws_ok = tmp_path / "ws_r4_sheet_ok"
    cand_ok = ws_ok / "model" / "PDD-model-v02.xlsx"
    _write_simple_valid_model(cand_ok)
    wb = openpyxl.load_workbook(cand_ok)
    for req_sh in ("Facts", "Guidance vs Actual", "Street vs House", "Checks"):
        if req_sh not in wb.sheetnames:
            ws_sh = wb.create_sheet(req_sh)
            ws_sh["A1"] = req_sh
    wb.save(cand_ok)
    wb.close()
    sha_ok = mfp.file_sha256(cand_ok)

    # 1) Execute REAL CLI `model_check.py check-sheets` and parse exact raw stdout JSON
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "model_check.py"),
            "check-sheets",
            "--candidate",
            str(cand_ok),
            "--workflow",
            "full-coverage",
            "--mode",
            "update",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    raw_cli_out = json.loads(proc.stdout)
    assert raw_cli_out["status"] == "complete"
    assert raw_cli_out["candidate_path"] == str(cand_ok.resolve())
    assert isinstance(raw_cli_out["summary"], str) and len(raw_cli_out["summary"]) >= 1
    assert raw_cli_out["missing_required_sheets"] == []

    # Validate raw CLI stdout directly against the actual JS `_MODEL_SHEET_GATE_SCHEMA`!
    schema_out = await run_workflow_script(
        (REPO_ROOT / "workflows" / "_shared" / "modeling_stages.js").read_text(encoding="utf-8")
        + "\nreturn _MODEL_SHEET_GATE_SCHEMA\n",
        {},
        create_autospec(WorkflowHost, instance=True),
        WorkflowLimits(memory_limit_mb=64, cpu_budget_s=5.0),
    )
    assert schema_out.status == "completed", schema_out.error
    sheet_gate_schema = schema_out.result
    jsonschema.validate(instance=raw_cli_out, schema=sheet_gate_schema)

    async def _run_fc(handler: Any) -> tuple[Any, list[str]]:
        labels: list[str] = []
        spec_host = create_autospec(WorkflowHost, instance=True)

        async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
            lbl = str(opts.get("label") or "")
            labels.append(lbl)
            res = handler(lbl, prompt, opts)
            # Enforce the real JSON schema passed by the workflow script on every non-null reply!
            if res is not None and isinstance(opts.get("schema"), dict):
                jsonschema.validate(instance=res, schema=opts["schema"])
            return res

        spec_host.agent.side_effect = _agent
        spec_host.phase.side_effect = lambda _t: None
        spec_host.log.side_effect = lambda _m: None
        out = await run_workflow_script(script, params, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
        assert out.status == "completed", out.error
        return out.result, labels

    # 2) Complete workbook + REAL CLI output verbatim + stale priorMissing=["Checks"] -> MUST enter drafting (`成稿`)!
    draft_reached = {"yes": False}

    def _h_complete_with_stale_prior(lbl: str, prompt: str, _opts: dict[str, Any]) -> Any:
        if lbl == "支线核验":
            ps = [ln[2:].split("（")[0].strip() for ln in prompt.splitlines() if ln.startswith("- ")]
            return {"files": [f"{p}|ok" for p in ps]}
        if lbl in ("数字底表", "frame") or lbl.startswith("支线 "):
            return {"files": [f"{r_rel}/facts.md"], "summary": "ok"}
        if lbl == "建模准备":
            return {"files": ["fp.json"], "summary": "prep", "mode": "update", "baseline_path": "model/PDD-model-v01.xlsx", "planned_output_path": "model/PDD-model-v02.xlsx"}
        if lbl == "读取运行ID":
            return {"run_id": "run-fc-r4-ok"}
        if lbl == "模型构建与落盘":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "v02", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": sha_ok, "recalc_status": "ok"}
        if lbl == "核验候选落盘":
            return {"path": "model/PDD-model-v02.xlsx", "exists": True, "readable": True, "model_sha256": sha_ok, "run_matched": True}
        if lbl == "一致预期对照":
            return {"files": ["model/PDD-model-v02.xlsx", f"{r_rel}/model_dump/consensus-diff.md", f"{r_rel}/model_dump/consensus-diff.json"], "summary": "cons", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": sha_ok, "consensus_diff_generated": True}
        if lbl == "模型本轮范围检查":
            # Stale/bogus priorMissing from check child MUST NOT block a disk-confirmed complete workbook!
            return {
                "files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json"],
                "summary": "pass with stale prior missing",
                "readable": True,
                "final_status": "pass",
                "model_sha256": sha_ok,
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": ["Checks"],
            }
        if lbl == "完整覆盖页签核验":
            return raw_cli_out  # Verbatim CLI stdout JSON!
        if lbl == "memo 探测":
            return {"files": ["./drafts/2026-11-27-memo.md|absent"]}
        if lbl == "成稿":
            draft_reached["yes"] = True
            return None  # Stop cleanly after proving drafting was reached
        return None

    _res_ok, labels_ok = await _run_fc(_h_complete_with_stale_prior)
    assert draft_reached["yes"] is True and "成稿" in labels_ok

    # 3) Missing workbook where model ALREADY delivered_with_open_issues (remaining_issues_path set,
    #    failed_stages=["consensus"] without "check") -> MUST STILL rewrite remaining-issues.md with C01 missing sheets!
    ws_miss = tmp_path / "ws_r4_sheet_miss"
    cand_miss = ws_miss / "model" / "PDD-model-v02.xlsx"
    sha_miss = _write_simple_valid_model(cand_miss)
    old_rem = ws_miss / r_rel / "model_dump" / "remaining-issues.md"
    old_rem.parent.mkdir(parents=True, exist_ok=True)
    old_rem.write_text("# old open issues without missing sheets\n", encoding="utf-8")

    proc_miss = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "model_check.py"),
            "check-sheets",
            "--candidate",
            str(cand_miss),
            "--workflow",
            "full-coverage",
            "--mode",
            "update",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_miss.returncode == 0, proc_miss.stderr
    raw_cli_miss = json.loads(proc_miss.stdout)
    jsonschema.validate(instance=raw_cli_miss, schema=sheet_gate_schema)
    assert raw_cli_miss["status"] == "missing"

    fallback_called = {"count": 0}

    def _h_missing_rewrites_c01(lbl: str, prompt: str, _opts: dict[str, Any]) -> Any:
        if lbl == "支线核验":
            ps = [ln[2:].split("（")[0].strip() for ln in prompt.splitlines() if ln.startswith("- ")]
            return {"files": [f"{p}|ok" for p in ps]}
        if lbl in ("数字底表", "frame") or lbl.startswith("支线 "):
            return {"files": [f"{r_rel}/facts.md"], "summary": "ok"}
        if lbl == "建模准备":
            return {"files": ["fp.json"], "summary": "prep", "mode": "update", "baseline_path": "model/PDD-model-v01.xlsx", "planned_output_path": "model/PDD-model-v02.xlsx"}
        if lbl == "读取运行ID":
            return {"run_id": "run-fc-r4-miss"}
        if lbl == "模型构建与落盘":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "v02", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": sha_miss, "recalc_status": "ok"}
        if lbl == "核验候选落盘":
            return {"path": "model/PDD-model-v02.xlsx", "exists": True, "readable": True, "model_sha256": sha_miss, "run_matched": True}
        if lbl == "一致预期对照":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "cons failed", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": sha_miss, "consensus_diff_generated": False}
        if lbl == "模型本轮范围检查":
            # delivered_with_open_issues with remaining_issues_path ALREADY set & failed_stages=["consensus"] (NO "check"!)
            return {
                "files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "open due to consensus only",
                "readable": True,
                "final_status": "delivered_with_open_issues",
                "model_sha256": sha_miss,
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md",
            }
        if lbl == "完整覆盖页签核验":
            return raw_cli_miss
        if lbl == "写入降级账本":
            fallback_called["count"] += 1
            fb = mchk.write_fallback_ledger(
                workspace_dir=ws_miss,
                ticker="PDD",
                research_dir=ws_miss / r_rel,
                candidate_path=cand_miss,
                workflow="full-coverage",
                mode="update",
                failed_stages=["consensus"],
                run_id="run-fc-r4-miss",
            )
            return {
                "files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "rewrote c01",
                "final_status": fb["final_status"],
                "model_sha256": fb["model_sha256"],
                "run_id": "run-fc-r4-miss",
            }
        raise AssertionError(f"must not reach drafting on missing sheets: {lbl}")

    res_miss, labels_miss = await _run_fc(_h_missing_rewrites_c01)
    assert res_miss["ok"] is False
    assert res_miss["sheet_gate_status"] == "missing"
    assert fallback_called["count"] == 1, "Missing sheets MUST rewrite remaining-issues.md even when remaining_issues_path already existed!"
    rewritten_rem = old_rem.read_text(encoding="utf-8")
    assert "C01" in rewritten_rem and "Facts" in rewritten_rem and "Checks" in rewritten_rem
    assert "成稿" not in labels_miss

    # 4) Probe timeout with non-empty priorMissing -> uses priorMissing to classify as missing (not unverified)
    def _h_timeout_with_prior(lbl: str, prompt: str, _opts: dict[str, Any]) -> Any:
        if lbl == "完整覆盖页签核验":
            return None  # Timed out
        if lbl == "模型本轮范围检查":
            return {
                "files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "missing checks",
                "readable": True,
                "final_status": "delivered_with_open_issues",
                "model_sha256": sha_miss,
                "actionable_count": 1,
                "actionable_issues": ["missing Checks"],
                "missing_required_sheets": ["Checks"],
                "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md",
            }
        return _h_missing_rewrites_c01(lbl, prompt, _opts)

    res_tp, _ = await _run_fc(_h_timeout_with_prior)
    assert res_tp["ok"] is False
    assert res_tp["sheet_gate_status"] == "missing"
    assert res_tp["missing_required_sheets"] == ["Checks"]


@pytest.mark.asyncio
async def test_p0b_r4_full_coverage_build_mode_propagates_to_report_revision_and_verify_done(
    tmp_path: Path,
) -> None:
    """R4 #3 (Codex #3 & #8): Effective modeling mode (`build` vs `update`) propagates
    from full-coverage into reviseModelForReport check, fallback ledger, and terminal
    verify-done; a build-mode full-coverage revision missing build-only sheets fails
    verify-done instead of downgrading to update's 4 sheets. SKILL.md manual check
    explicitly passes `--mode`."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )

    # 1. Verify SKILL.md manual range checks explicitly include `--mode <build|update>` with full params
    skill_text = (REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "SKILL.md").read_text(encoding="utf-8")
    assert "model_check.py check --workspace . --ticker <TICKER>" in skill_text
    assert "--workflow company-model --mode <build|update> --fix-round <0|1|2>" in skill_text
    assert "--workflow company-model --mode <build|update> --fix-round <1|2>" in skill_text
    assert "model_check.py write-ledger" in skill_text and "--mode <build|update>" in skill_text

    # 2. Full-coverage build-mode run enters report revision -> reviseModelForReport receives mode="build",
    #    passes `--mode "build"` to `model_check.py check` and `writeFallbackLedger`, and terminal
    #    `verify-done` requires all 15 build sheets + README & Changelog!
    ws = tmp_path / "ws_fc_build_rev"
    r_rel = "research/2026-11-27-fc"
    all_build_sheets = [
        "README & Changelog", "Cover", "Facts", "Revenue Model", "Income Statement",
        "Cash Flow Statement", "Balance Sheet", "Scenarios", "Guidance vs Actual",
        "Street vs House", "DCF Inputs", "DCF", "Sensitivity Analysis",
        "Comparable Companies", "Valuation Summary", "Checks",
    ]
    v1 = ws / "model" / "PDD-model-v01.xlsx"
    _write_simple_valid_model(v1)
    wb1 = openpyxl.load_workbook(v1)
    for sh in all_build_sheets:
        if sh not in wb1.sheetnames:
            wb1.create_sheet(sh)["A1"] = sh
    wb1.save(v1)
    wb1.close()
    v1_sha = mfp.file_sha256(v1)

    # Post-revision candidate v02 ONLY has the 4 update sheets (missing build-only sheets like DCF / Scenarios!)
    v2 = ws / "model" / "PDD-model-v02.xlsx"
    _write_simple_valid_model(v2)
    wb2 = openpyxl.load_workbook(v2)
    for sh in ("Facts", "Guidance vs Actual", "Street vs House", "Checks"):
        if sh not in wb2.sheetnames:
            wb2.create_sheet(sh)["A1"] = sh
    wb2.save(v2)
    wb2.close()
    v2_sha = mfp.file_sha256(v2)

    memo_p = ws / "drafts" / "2026-11-27-memo.md"
    memo_p.parent.mkdir(parents=True, exist_ok=True)
    memo_p.write_text("# PDD Memo\nmodel:model/PDD-model-v02.xlsx!Facts!A1\n", encoding="utf-8")
    memo_sha = mfp.file_sha256(memo_p)

    # Initial check writes ledger with mode="build" on v01
    mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=ws / r_rel,
        candidate_path=v1,
        workflow="full-coverage",
        mode="build",
        run_id="run-fc-build-r4",
    )

    script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "full-coverage" / "workflow.js")
    params = {
        "ticker": "PDD",
        "company": "PDD Holdings",
        "workspace_dir": ".",
        "as_of": "2026-11-27",
        "progress_path": f"{r_rel}/.progress.json",
    }
    nine_items = [{"check": i, "result": "pass", "reason": "ok", "lines": "L1"} for i in range(1, 10)]
    seen_rev_chk_prompt: list[str] = []
    seen_rev_fb_prompt: list[str] = []

    spec_host = create_autospec(WorkflowHost, instance=True)

    async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
        lbl = str(opts.get("label") or "")
        if lbl == "支线核验":
            ps = [ln[2:].split("（")[0].strip() for ln in prompt.splitlines() if ln.startswith("- ")]
            return {"files": [f"{p}|ok" for p in ps]}
        if lbl in ("数字底表", "frame") or lbl.startswith("支线 "):
            return {"files": [f"{r_rel}/facts.md"], "summary": "ok"}
        if lbl == "建模准备":
            return {"files": ["fp.json"], "summary": "prep", "mode": "build", "baseline_path": None, "planned_output_path": "model/PDD-model-v01.xlsx"}
        if lbl == "读取运行ID":
            return {"run_id": "run-fc-build-r4"}
        if lbl == "模型构建与落盘":
            return {"files": ["model/PDD-model-v01.xlsx"], "summary": "v01", "candidate_path": "model/PDD-model-v01.xlsx", "model_sha256": v1_sha, "recalc_status": "ok"}
        if lbl == "核验候选落盘":
            return {"path": "model/PDD-model-v01.xlsx", "exists": True, "readable": True, "model_sha256": v1_sha, "run_matched": True}
        if lbl == "一致预期对照":
            return {"files": ["model/PDD-model-v01.xlsx", f"{r_rel}/model_dump/consensus-diff.md", f"{r_rel}/model_dump/consensus-diff.json"], "summary": "cons", "candidate_path": "model/PDD-model-v01.xlsx", "model_sha256": v1_sha, "consensus_diff_generated": True}
        if lbl == "模型本轮范围检查":
            return {"files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json"], "summary": "pass", "readable": True, "final_status": "pass", "model_sha256": v1_sha, "actionable_count": 0, "missing_required_sheets": []}
        if lbl == "完整覆盖页签核验":
            return mchk.check_required_sheets(candidate_path=v1, workflow="full-coverage", mode="build")
        if lbl == "memo 探测":
            return {"files": [f"./drafts/2026-11-27-memo.md|ok|{memo_sha}"]}
        if lbl == "语言审查 1":
            return {"files": ["drafts/2026-11-27-memo.md"], "summary": "lang1", "sha256": memo_sha}
        if lbl == "独立审稿":
            return {"verdict": "revise", "summary": "update numbers", "must_fix": ["revise model"], "items": nine_items}
        if lbl == "计算联动新路径":
            return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v02", "next_path": "model/PDD-model-v02.xlsx"}
        if lbl == "模型联动修订":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "v02 saved", "changed": True, "model_path": "model/PDD-model-v02.xlsx", "model_sha256": v2_sha, "recalc_status": "ok"}
        if lbl == "核验联动候选落盘":
            return {"path": "model/PDD-model-v02.xlsx", "exists": True, "readable": True, "model_sha256": v2_sha, "run_matched": True}
        if lbl == "模型联动差异刷新":
            return {"files": [f"{r_rel}/model_dump/consensus-diff.md"], "summary": "diff", "consensus_diff_generated": False}
        if lbl == "核验差异表落盘":
            return {"path": f"{r_rel}/model_dump/consensus-diff.json", "json_ok": False, "md_ok": False, "matched": False}
        if lbl == "模型联动报告同步":
            return {"files": ["drafts/2026-11-27-memo.md"], "summary": "synced", "report_sha256": memo_sha}
        if lbl == "核验报告同步落盘":
            return {"path": "drafts/2026-11-27-memo.md", "exists": True, "synced": True, "report_sha256": memo_sha}
        if lbl == "模型联动修订检查":
            seen_rev_chk_prompt.append(prompt)
            # Simulate timeout so fallback ledger ALSO runs with mode="build"!
            return None
        if lbl == "写入降级账本":
            seen_rev_fb_prompt.append(prompt)
            fb = mchk.write_fallback_ledger(
                workspace_dir=ws,
                ticker="PDD",
                research_dir=ws / r_rel,
                candidate_path=v2,
                baseline_path=v1,
                kind="report_revision",
                failed_stages=["consensus", "check"],
                run_id="run-fc-build-r4",
                workflow="full-coverage",
                mode="build",
            )
            return {
                "files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "fb build",
                "final_status": fb["final_status"],
                "model_sha256": fb["model_sha256"],
                "run_id": "run-fc-build-r4",
            }
        return None  # Stop after revision model check

    spec_host.agent.side_effect = _agent
    spec_host.phase.side_effect = lambda _t: None
    spec_host.log.side_effect = lambda _m: None

    out = await run_workflow_script(script, params, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
    assert out.status == "completed", out.error
    assert seen_rev_chk_prompt and '--mode "build"' in seen_rev_chk_prompt[0], seen_rev_chk_prompt
    assert seen_rev_fb_prompt and '--mode "build"' in seen_rev_fb_prompt[0], seen_rev_fb_prompt

    # Terminal verify-done MUST read mode="build" and REJECT v02 for missing build-only sheets (DCF, Scenarios, etc.)!
    led = json.loads((ws / r_rel / "model_dump" / "model-verification.json").read_text(encoding="utf-8"))
    assert led["mode"] == "build"
    assert "DCF" in led["missing_required_sheets"] and "Scenarios" in led["missing_required_sheets"]
    vd = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=ws / r_rel,
        expected_run_id="run-fc-build-r4",
        expected_workflow="full-coverage",
    )
    assert vd["ok"] is False
    assert any("full-coverage (build)" in e and "DCF" in e for e in vd["errors"]), vd["errors"]

    # Also verify write_check_and_ledger(kind="report_revision", append_revision=True) preserves build mode!
    chk_rev = mchk.write_check_and_ledger(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=ws / r_rel,
        candidate_path=v2,
        baseline_path=v1,
        workflow="full-coverage",
        mode="build",
        kind="report_revision",
        append_revision=True,
        run_id="run-fc-build-r4",
    )
    assert chk_rev["ledger"]["mode"] == "build"
    assert "DCF" in chk_rev["missing_required_sheets"]


@pytest.mark.asyncio
async def test_p0b_r4_path_binding_gate_old_path_rejection_and_split_disk_evidence_sync(
    tmp_path: Path,
) -> None:
    """R4 #4 (Codex #4 & #5, Grok #2):
    1) Initial modeling: path binding failure refuses to dispatch `模型构建与落盘`;
    2) Report revision: path binding failure refuses to dispatch `模型联动修订`;
    3) Report revision: pre-bound path published on disk before edit timeout -> parent
       probes pre-bound path, recovers new candidate & real SHA-256 with `recalc`;
    4) Report revision: child returning `changed=true` with OLD path is rejected when
       pre-bound path is absent on disk;
    5) Diff refresh and report sync are dispatched separately and each settled strictly
       by on-disk evidence, with disk-measured `reportSha256`."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )

    stages_src = (REPO_ROOT / "workflows" / "_shared" / "modeling_stages.js").read_text(encoding="utf-8")
    rev_harness_js = f"{stages_src}\nreturn await reviseModelForReport(args)\n"
    cm_script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "company-model" / "workflow.js")

    r_rel = "research/2026-11-27-deep-FY26Q3"
    ws = tmp_path / "ws_r4_bind"
    v2 = ws / "model" / "PDD-model-v02.xlsx"
    v3 = ws / "model" / "PDD-model-v03.xlsx"
    v2_sha = _write_simple_valid_model(v2)
    v3_sha = _write_simple_valid_model(v3)

    # --- Case 1: Initial modeling path-binding failure -> NEVER dispatches 模型构建与落盘 ---
    cm_labels: list[str] = []
    cm_host = create_autospec(WorkflowHost, instance=True)

    async def _cm_bind_fail(prompt: str, opts: dict[str, Any]) -> Any:
        lbl = str(opts.get("label") or "")
        cm_labels.append(lbl)
        if lbl == "建模准备":
            return {"files": ["fp.json"], "summary": "prep", "mode": "update", "baseline_path": "model/PDD-model-v02.xlsx", "planned_output_path": None}
        if lbl == "读取运行ID":
            return {"run_id": "run-bind-fail-1"}
        if lbl == "绑定输出路径":
            return None  # Binding failed!
        raise AssertionError(f"MUST NOT dispatch edit after binding failure: {lbl}")

    cm_host.agent.side_effect = _cm_bind_fail
    cm_host.phase.side_effect = lambda _t: None
    cm_host.log.side_effect = lambda _m: None
    out_cm = await run_workflow_script(
        cm_script,
        {"ticker": "PDD", "company": "PDD", "workspace_dir": ".", "as_of": "2026-11-27", "progress_path": "research/2026-11-27-model-PDD/.progress.json"},
        cm_host,
        WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0),
    )
    assert out_cm.status == "completed", out_cm.error
    assert out_cm.result["ok"] is False and out_cm.result["status"] == "failed_unusable"
    assert "模型构建与落盘" not in cm_labels

    rev_args = {
        "workspaceDir": ".",
        "ticker": "PDD",
        "researchDir": r_rel,
        "progressPath": f"{r_rel}/.progress.json",
        "workflow": "earnings-deep-update",
        "mode": "update",
        "currentModelPath": "model/PDD-model-v02.xlsx",
        "currentModelSha256": v2_sha,
        "currentStatus": "pass",
        "currentArtifacts": [],
        "consensusPath": "data/consensus/snap.json",
        "priorFailedStages": [],
        "runId": "run-rev-r4",
        "reportPath": "drafts/2026-11-27-deep-FY26Q3.md",
        "reviewFeedback": {"verdict": "revise", "must_fix": ["fix rev"]},
        "phaseLabel": "审稿",
    }

    async def _run_rev(handler: Any) -> tuple[Any, list[str]]:
        lbls: list[str] = []
        spec_host = create_autospec(WorkflowHost, instance=True)

        async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
            lbl = str(opts.get("label") or "")
            lbls.append(lbl)
            return handler(lbl, prompt)

        spec_host.agent.side_effect = _agent
        spec_host.phase.side_effect = lambda _t: None
        spec_host.log.side_effect = lambda _m: None
        out = await run_workflow_script(rev_harness_js, rev_args, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
        assert out.status == "completed", out.error
        return out.result, lbls

    # --- Case 2: Report revision binding fails (returns same path as current) -> NEVER dispatches 模型联动修订 ---
    def _h_rev_bind_fail(lbl: str, prompt: str) -> Any:
        if lbl == "计算联动新路径":
            # Returning currentModelPath is invalid as next_path!
            return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "bad", "next_path": "model/PDD-model-v02.xlsx"}
        if lbl == "写入降级账本":
            assert '--candidate "model/PDD-model-v02.xlsx"' in prompt
            assert '--failed-stages "report_revision"' in prompt
            return {"files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"], "summary": "fb", "final_status": "delivered_with_open_issues", "model_sha256": v2_sha, "run_id": "run-rev-r4"}
        raise AssertionError(f"MUST NOT dispatch edit when next_path binding failed: {lbl}")

    res_bf, lbls_bf = await _run_rev(_h_rev_bind_fail)
    assert "模型联动修订" not in lbls_bf
    assert res_bf["ok"] is True and res_bf["modelPath"] == "model/PDD-model-v02.xlsx"
    assert res_bf["failedStages"] == ["report_revision"]

    # --- Case 3: Pre-bound v03 published on disk, then edit subagent times out -> parent probes & recovers v03 + recalc ---
    def _h_rev_pub_timeout(lbl: str, prompt: str) -> Any:
        if lbl == "计算联动新路径":
            return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
        if lbl == "模型联动修订":
            assert '--out "model/PDD-model-v03.xlsx"' in prompt
            return None  # Timed out AFTER v03 was already published on disk!
        if lbl == "核验联动候选落盘":
            assert "model/PDD-model-v03.xlsx" in prompt
            return {"path": "model/PDD-model-v03.xlsx", "exists": True, "readable": True, "model_sha256": v3_sha, "run_matched": True}
        if lbl == "模型联动差异刷新":
            return None  # Timed out AFTER writing consensus-diff on disk
        if lbl == "核验差异表落盘":
            return {"path": f"{r_rel}/model_dump/consensus-diff.json", "json_ok": True, "md_ok": True, "matched": True}
        if lbl == "模型联动报告同步":
            return None  # Timed out AFTER rewriting report on disk
        if lbl == "核验报告同步落盘":
            return {"path": "drafts/2026-11-27-deep-FY26Q3.md", "exists": True, "synced": True, "report_sha256": "9" * 64}
        if lbl == "模型联动修订检查":
            assert '--candidate "model/PDD-model-v03.xlsx"' in prompt
            assert '--failed-stages "recalc"' in prompt
            return {
                "files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "delivered open recalc",
                "readable": True,
                "final_status": "delivered_with_open_issues",
                "model_sha256": v3_sha,
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md",
            }
        raise AssertionError(lbl)

    res_pt, lbls_pt = await _run_rev(_h_rev_pub_timeout)
    assert res_pt["ok"] is True
    assert res_pt["modelPath"] == "model/PDD-model-v03.xlsx"
    assert res_pt["modelSha256"] == v3_sha
    assert res_pt["reportSha256"] == "9" * 64, "Must recover disk-measured report SHA even after sync subagent timeout!"
    assert res_pt["failedStages"] == ["recalc"], f"Disk-confirmed diff & report sync must NOT be marked failed: {res_pt['failedStages']}"
    assert "模型联动差异刷新" in lbls_pt and "模型联动报告同步" in lbls_pt

    # --- Case 4: Child spoofs `changed=true` returning OLD path v02 while pre-bound v03 does NOT exist ->
    # MUST NOT treat old path as new candidate AND MUST NOT abort workflow; writes fallback ledger on readable v02! ---
    def _h_rev_old_path_spoof(lbl: str, prompt: str) -> Any:
        if lbl == "计算联动新路径":
            return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
        if lbl == "模型联动修订":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "spoofed old", "changed": True, "model_path": "model/PDD-model-v02.xlsx", "model_sha256": v2_sha, "recalc_status": "ok"}
        if lbl == "核验联动候选落盘":
            assert "model/PDD-model-v03.xlsx" in prompt
            return {"path": "model/PDD-model-v03.xlsx", "exists": False, "readable": False, "model_sha256": None}
        if lbl == "写入降级账本":
            assert '--candidate "model/PDD-model-v02.xlsx"' in prompt
            assert '--failed-stages "report_revision"' in prompt
            return {
                "files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "fb on readable v02",
                "final_status": "delivered_with_open_issues",
                "model_sha256": v2_sha,
                "run_id": "run-rev-r4",
            }
        raise AssertionError(f"Must NOT continue diff/sync after missing new candidate: {lbl}")

    res_sp, lbls_sp = await _run_rev(_h_rev_old_path_spoof)
    assert res_sp["ok"] is True and res_sp["status"] == "delivered_with_open_issues"
    assert res_sp["changed"] is False
    assert res_sp["modelPath"] == "model/PDD-model-v02.xlsx"
    assert res_sp["modelSha256"] == v2_sha
    assert res_sp["failedStages"] == ["report_revision"]
    assert "写入降级账本" in lbls_sp
    assert "模型联动差异刷新" not in lbls_sp

    # --- Case 5: Diff fails (not on disk), Report sync succeeds on disk -> ONLY consensus fails, report_sync passes ---
    def _h_split_partial(lbl: str, prompt: str) -> Any:
        if lbl == "计算联动新路径":
            return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
        if lbl == "模型联动修订":
            return {"files": ["model/PDD-model-v03.xlsx"], "summary": "v03", "changed": True, "model_path": "model/PDD-model-v03.xlsx", "model_sha256": v3_sha, "recalc_status": "ok"}
        if lbl == "核验联动候选落盘":
            return {"path": "model/PDD-model-v03.xlsx", "exists": True, "readable": True, "model_sha256": v3_sha, "run_matched": True}
        if lbl == "模型联动差异刷新":
            return None
        if lbl == "核验差异表落盘":
            return {"path": f"{r_rel}/model_dump/consensus-diff.json", "json_ok": False, "md_ok": False, "matched": False}
        if lbl == "模型联动报告同步":
            return {"files": ["drafts/2026-11-27-deep-FY26Q3.md"], "summary": "synced", "report_sha256": "1" * 64}
        if lbl == "核验报告同步落盘":
            return {"path": "drafts/2026-11-27-deep-FY26Q3.md", "exists": True, "synced": True, "report_sha256": "8" * 64}
        if lbl == "模型联动修订检查":
            assert '--failed-stages "consensus"' in prompt
            assert "report_sync" not in prompt
            return {
                "files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "open consensus only",
                "readable": True,
                "final_status": "delivered_with_open_issues",
                "model_sha256": v3_sha,
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md",
            }
        raise AssertionError(lbl)

    res_part, _ = await _run_rev(_h_split_partial)
    assert res_part["ok"] is True
    assert res_part["failedStages"] == ["consensus"]
    assert res_part["reportSha256"] == "8" * 64, "Must use disk-measured report SHA rather than child-echoed SHA!"


@pytest.mark.asyncio
async def test_p0b_r4_real_workflow_resume_after_snapshot_and_inplace_rework_preserves_rework_and_updates_sha(
    tmp_path: Path,
) -> None:
    """R4 #5 (Codex #6, Grok #4): Real `WorkflowDriver` + `WorkflowJournal` resume of
    `company-model` after Stage 2b ran BOTH real `model_consensus.py write-snapshot`
    AND real `model_edit.py apply --in-place` (consensus rework) before interrupting.
    Verifies:
    1) Transition receipt chains `H1 -> H2 (snapshot) -> H3 (in-place rework)`;
    2) Real resumed workflow replays Stage 2a WITHOUT re-dispatching or bumping to v03;
    3) Replayed Stage 2a result has `model_sha256` updated to post-rework `H3`, so Stage 2b
       receives `H3` (not stale `H1`);
    4) In-place rework cell (`E2=1333`) in v02 AND original read-only baseline v01 (`E2=1000`)
       are BOTH preserved, and real `model_check.py verify-done` passes on Run 2."""
    import asyncio
    import fnmatch
    import shlex as _shlex
    from types import SimpleNamespace as _NS
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.middleware.background_subagent.registry import BackgroundTaskRegistry
    from ptc_agent.agent.middleware.background_subagent.workflow.driver import WorkflowDriver, WorkflowRunSpec
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import compile_check
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import read_assembled_workflow_file
    from src.config.models import WorkflowOrchestrationConfig

    ws = tmp_path / "ws_real_resume_rework"
    scripts_src = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "scripts"
    dst_scripts = ws / ".agents" / "skills" / "dalton-model" / "scripts"
    dst_scripts.mkdir(parents=True, exist_ok=True)
    for py_f in scripts_src.glob("*.py"):
        shutil.copy2(py_f, dst_scripts / py_f.name)
    r_rel = "research/2026-11-27-model-PDD"
    dump_rel = f"{r_rel}/model_dump"
    base_rel = "model/PDD-model-v01.xlsx"
    base_p = ws / base_rel
    base_sha = _write_simple_valid_model(base_p)
    cand_rel = "model/PDD-model-v02.xlsx"
    cand_p = ws / cand_rel
    cm_rel = f"{dump_rel}/coord_map.json"
    cm_p = ws / cm_rel
    fp_rel = f"{dump_rel}/PDD-fingerprint.json"
    cons_rel = "data/consensus/2026-11-27_1000.json"
    cons_p = ws / cons_rel
    cons_p.parent.mkdir(parents=True, exist_ok=True)
    cons_p.write_text(
        json.dumps(
            {
                "ticker": "PDD",
                "as_of_date": "2026-11-27",
                "source_type": "Market Consensus",
                "items": [{"period": "2026E", "metric": "Total Revenue", "consensus": 1350.0}],
            }
        ),
        encoding="utf-8",
    )

    # Real disk-backed SandboxBackend autospec (all files live under `ws`)
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = str(ws)

    async def _aread(path: str) -> str | None:
        p = ws / path
        return p.read_text(encoding="utf-8") if p.is_file() else None

    async def _awrite(path: str, text: str) -> bool:
        p = ws / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return True

    async def _aglob(pattern: str, root: str = ".") -> list[str]:
        found: list[str] = []
        base_root = ws if root in (".", "") else (ws / root)
        if not base_root.exists():
            return []
        for p in base_root.rglob("*"):
            if p.is_file():
                rel = p.relative_to(ws).as_posix()
                if fnmatch.fnmatch(rel, pattern):
                    found.append(rel)
        return sorted(found)

    async def _aexec(cmd: str) -> Any:
        if "sha256sum -- " in cmd:
            tail = cmd.split("sha256sum -- ", 1)[1]
            lines = []
            for rel in _shlex.split(tail):
                p = ws / rel
                if not p.is_file():
                    return _NS(exit_code=1, output="")
                lines.append(f"{mfp.file_sha256(p)}  {rel}")
            return _NS(exit_code=0, output="\n".join(lines) + "\n")
        return _NS(exit_code=1, output="")

    backend.aread_text.side_effect = _aread
    backend.awrite_text.side_effect = _awrite
    backend.aglob_paths.side_effect = _aglob
    backend.aexecute.side_effect = _aexec

    wf_script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "company-model" / "workflow.js")
    wf_meta = compile_check(wf_script)
    params = {
        "ticker": "PDD",
        "company": "PDD Holdings",
        "workspace_dir": ".",
        "as_of": "2026-11-27",
        "mode": "update",
        "model_path": base_rel,
        "progress_path": f"{r_rel}/.progress.json",
    }

    def _make_run_driver(
        run_id_str: str,
        handler: Any,
        *,
        journal_source: str | None = None,
    ) -> tuple[WorkflowDriver, list[tuple[str, str]]]:
        dispatched: list[tuple[str, str]] = []
        registry = BackgroundTaskRegistry()

        class _FakeDispatcher:
            subagent_types = frozenset({"general-purpose", "dalton-evidence"})

            async def dispatch(
                self,
                *,
                subagent_type: str,
                description: str,
                prompt: str,
                run_id: str | None = None,
                owner_task_id: str | None = None,
                base_configurable: dict[str, Any] | None = None,
                time_limit: float | None = None,
                **kwargs: Any,
            ) -> Any:
                dispatched.append((description, prompt))
                task = await registry.register(
                    tool_call_id=f"{run_id_str}-child-{len(dispatched)}",
                    description=description,
                    prompt=prompt[:200],
                    subagent_type=subagent_type,
                    run_id=run_id,
                )
                task.per_call_records = []

                async def _run_child() -> str:
                    res = handler(description, prompt)
                    if isinstance(res, Exception):
                        task.terminal_status = "error"
                        raise res
                    task.terminal_status = "completed"
                    return json.dumps(res, ensure_ascii=False)

                task.asyncio_task = asyncio.create_task(_run_child())
                return task

        run_task = _NS(
            task_id=run_id_str,
            display_id=run_id_str,
            task_run_id=None,
            spawned_run_id=run_id_str,
            terminal_status=None,
            stop_at=None,
            keep_straggler=lambda _w: None,
        )
        spec = WorkflowRunSpec(
            run_task=run_task,
            registry=registry,
            dispatcher=_FakeDispatcher(),
            backend=backend,
            thread_id="thread-r4-resume",
            short_thread_id="12345678",
            script=wf_script,
            script_args=params,
            meta=wf_meta,
            source="prebuilt",
            caps=WorkflowOrchestrationConfig(
                enabled=True,
                max_dispatches_per_run=30,
                max_concurrent_children=4,
                child_timeout=30.0,
                run_timeout=60.0,
                memory_limit_mb=64,
                cpu_budget_s=15.0,
            ),
            journal_source=journal_source,
            progress_path=f"{r_rel}/.progress.json",
        )
        return WorkflowDriver(spec), dispatched

    state_box: dict[str, str] = {}
    probe_results_run1: list[dict[str, Any]] = []
    probe_results_run2: list[dict[str, Any]] = []

    def _run_real_probe(prompt: str) -> dict[str, Any]:
        m = re.search(r"```bash\n(.*?)\n```", prompt, re.DOTALL)
        assert m, f"Missing bash command block in prompt:\n{prompt}"
        cmd = m.group(1).replace("python3 ", f"{sys.executable} ")
        proc = subprocess.run(["bash", "-c", cmd], cwd=ws, capture_output=True, text=True, check=False)
        assert proc.returncode == 0, f"Real probe failed: {proc.stdout}\n{proc.stderr}"
        return json.loads(proc.stdout.strip())

    # --- RUN 1: Runs Prep -> Stage 2a (real model_edit v01->v02 H1) -> Stage 2b (real write-snapshot H1->H2
    #     + real in-place consensus rework H2->H3), then CRASHES before Stage 2b finishes! ---
    def _run1_handler(label: str, prompt: str) -> Any:
        if label == "建模准备":
            fp_obj = mfp.build_fingerprint(base_p, ticker="PDD")
            (ws / fp_rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / fp_rel).write_text(json.dumps(fp_obj, indent=2), encoding="utf-8")
            return {
                "files": [fp_rel],
                "summary": "prep done",
                "mode": "update",
                "baseline_path": base_rel,
                "baseline_sha256": base_sha,
                "fingerprint_path": fp_rel,
                "consensus_path": cons_rel,
                "planned_output_path": cand_rel,
                "baseline_health_warning": None,
            }
        if label == "读取运行ID":
            prog_doc = json.loads((ws / r_rel / ".progress.json").read_text(encoding="utf-8"))
            return {"run_id": prog_doc["run_id"]}
        if label == "模型构建与落盘":
            edit1 = medit.apply_edit_plan(
                baseline_path=base_p,
                ticker="PDD",
                plan={
                    "cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1111}],
                    "changelog_entry": {"version": "v02", "date": "2026-11-27", "summary": "init v02"},
                },
                output_path=cand_p,
                workspace_dir=ws,
                fingerprint=json.loads((ws / fp_rel).read_text(encoding="utf-8")),
                coord_map_out=cm_p,
                skip_recalc=True,
                mode="update",
                progress_path=ws / r_rel / ".progress.json",
            )
            state_box["h1"] = edit1["candidate_sha256"]
            return {
                "files": [cand_rel, cm_rel],
                "summary": "v02 built",
                "candidate_path": cand_rel,
                "model_sha256": state_box["h1"],
                "recalc_status": "ok",
            }
        if label == "核验候选落盘":
            out = _run_real_probe(prompt)
            probe_results_run1.append(out)
            return out
        if label == "一致预期对照":
            # 1) Real write-snapshot (H1 -> H2)
            rc_s = mcons.main(
                ["write-snapshot", "--model", str(cand_p), "--consensus", str(cons_p), "--coord-map", str(cm_p)]
            )
            assert rc_s == 0
            state_box["h2"] = mfp.file_sha256(cand_p)
            # 2) Real in-place consensus rework (H2 -> H3, updating E2 from 1111 to 1333!)
            rework_fp = mfp.build_fingerprint(cand_p, ticker="PDD")
            prior_cm = json.loads(cm_p.read_text(encoding="utf-8"))
            edit_rework = medit.apply_edit_plan(
                baseline_path=cand_p,
                ticker="PDD",
                plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1333}]},
                output_path=cand_p,
                workspace_dir=ws,
                fingerprint=rework_fp,
                coord_map_out=cm_p,
                prior_coord_map=prior_cm,
                expected_baseline_sha256=state_box["h2"],
                skip_recalc=True,
                mode="update",
                in_place=True,
                progress_path=ws / r_rel / ".progress.json",
            )
            state_box["h3"] = edit_rework["candidate_sha256"]
            assert state_box["h1"] != state_box["h2"] != state_box["h3"]
            # Simulate crash/interruption right after in-place rework landed on disk!
            return RuntimeError("simulated interruption right after in-place rework")
        if label in ("模型本轮范围检查", "写入降级账本"):
            # Stop Run 1 cleanly as an interrupted run (failing remaining stages)
            return RuntimeError("interrupted")
        raise AssertionError(f"unexpected Run 1 label: {label}")

    driver1, dispatched1 = _make_run_driver("run-r4-1", _run1_handler)
    await driver1.run()
    assert len(probe_results_run1) == 1
    assert probe_results_run1[0]["run_matched"] is True
    assert probe_results_run1[0]["receipt_run_id"] == "run-r4-1"
    assert (ws / dump_rel / "consensus-snapshot-receipt.json").is_file()
    rcpt = json.loads((ws / dump_rel / "consensus-snapshot-receipt.json").read_text(encoding="utf-8"))
    assert rcpt["pre_snapshot_model_sha256"] == state_box["h1"]
    assert rcpt["post_snapshot_model_sha256"] == state_box["h3"]
    assert len(rcpt.get("transitions") or []) >= 2

    # --- RUN 2 (REAL WORKFLOW RESUME): Replays Prep & Stage 2a without rebuilding! ---
    run2_stage2b_prompts: list[str] = []

    def _run2_handler(label: str, prompt: str) -> Any:
        if label == "读取运行ID":
            prog_doc = json.loads((ws / r_rel / ".progress.json").read_text(encoding="utf-8"))
            return {"run_id": prog_doc["run_id"]}
        if label == "核验候选落盘":
            out = _run_real_probe(prompt)
            probe_results_run2.append(out)
            return out
        if label == "模型构建与落盘":
            raise AssertionError("Stage 2a MUST be replayed from journal + receipt, NEVER re-dispatched!")
        if label == "一致预期对照":
            run2_stage2b_prompts.append(prompt)
            # Run real diff against the reworked v02 (H3)
            rc_d = mcons.main(
                [
                    "diff",
                    "--model",
                    str(cand_p),
                    "--ticker",
                    "PDD",
                    "--research-dir",
                    str(ws / r_rel),
                    "--consensus",
                    str(cons_p),
                ]
            )
            assert rc_d == 0
            return {
                "files": [cand_rel, cm_rel, f"{dump_rel}/consensus-diff.md", f"{dump_rel}/consensus-diff.json"],
                "summary": "diff on reworked v02",
                "candidate_path": cand_rel,
                "model_sha256": mfp.file_sha256(cand_p),
                "recalc_status": "ok",
                "consensus_status": "ok",
                "consensus_diff_generated": True,
            }
        if label == "模型本轮范围检查":
            chk_res = mchk.write_check_and_ledger(
                workspace_dir=ws,
                ticker="PDD",
                research_dir=ws / r_rel,
                candidate_path=cand_p,
                baseline_path=base_p,
                fingerprint_path=ws / fp_rel,
                coord_map_path=cm_p,
                run_id="run-r4-2",
                workflow="company-model",
                mode="update",
            )
            files = [f"{dump_rel}/model-check.md", f"{dump_rel}/model-verification.json"]
            if chk_res.get("remaining_issues_path"):
                files.append(f"{dump_rel}/remaining-issues.md")
            return {
                "files": files,
                "summary": "checked reworked v02",
                "readable": chk_res["readable"],
                "final_status": chk_res["final_status"],
                "model_sha256": chk_res["candidate_sha256"],
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{dump_rel}/remaining-issues.md" if chk_res.get("remaining_issues_path") else None,
            }
        raise AssertionError(f"unexpected Run 2 label: {label}")

    run1_dir_rel = ".agents/threads/12345678/workflows/run-r4-1"
    driver2, dispatched2 = _make_run_driver("run-r4-2", _run2_handler, journal_source=run1_dir_rel)
    await driver2.run()

    dispatched2_labels = [lbl for lbl, _ in dispatched2]
    assert len(probe_results_run2) == 1
    assert probe_results_run2[0]["run_matched"] is True, probe_results_run2[0]
    assert probe_results_run2[0]["receipt_run_id"] == "run-r4-1"
    assert probe_results_run2[0]["model_sha256"] == state_box["h3"]
    assert "建模准备" not in dispatched2_labels, "Prep should replay"
    assert "模型构建与落盘" not in dispatched2_labels, "Stage 2a MUST replay via receipt chain!"
    assert "一致预期对照" in dispatched2_labels
    # Grok #4: Stage 2b prompt MUST carry updated post-rework SHA H3 (NOT stale H1)!
    assert state_box["h3"] in run2_stage2b_prompts[0], run2_stage2b_prompts[0]
    assert state_box["h1"] not in run2_stage2b_prompts[0]

    # Version v03 MUST NOT exist; v02 MUST preserve reworked E2=1333; original v01 MUST remain untouched (E2=1000)!
    assert not (ws / "model" / "PDD-model-v03.xlsx").exists()
    assert mfp.file_sha256(base_p) == base_sha
    wb_v1 = openpyxl.load_workbook(base_p, data_only=False)
    wb_v2 = openpyxl.load_workbook(cand_p, data_only=False)
    try:
        assert wb_v1["Model - PDD"]["E2"].value == 1000
        assert wb_v2["Model - PDD"]["E2"].value == 1333, "In-place rework result MUST be preserved after resume!"
    finally:
        wb_v1.close()
        wb_v2.close()

    vd = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=ws / r_rel,
        expected_run_id="run-r4-2",
        expected_workflow="company-model",
    )
    assert vd["ok"] is True, vd["errors"]


@pytest.mark.asyncio
async def test_p0b_r4_cos_hitl_reuses_settled_payload_and_refuses_dispatch_on_missing_bound_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4 #6 (Codex #7):
    1) `settle_resume_request` extracts `approved_dispatch_payloads` from the `newest`
       checkpoint it ALREADY read so `ptc_run` reuses it without a second checkpoint read;
    2) `load_approved_dispatch_payloads` returns `None` on checkpoint read failure (not `{}`);
    3) When an approved `delegate_to_analyst` call on HITL resume is missing its bound
       approval payload (read error or absent `tool_call_id`), `dispatch()` stops and
       returns a retryable error WITHOUT dispatching or falling back to raw tool args;
    4) Rejected card still declines cleanly even if bound payload is absent."""
    from types import SimpleNamespace as _NS
    import aiohttp
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from src.dalton.cos_verbatim import (
        COS_APPROVED_DISPATCH_PAYLOADS_CONFIG_KEY,
        COS_HITL_RESUME_CONFIG_KEY,
        COS_TURN_VERBATIM_CONFIG_KEY,
        load_approved_dispatch_payloads,
    )
    import src.server.handlers.checkpoint_handler as cp_handler
    from src.server.models.chat import ChatRequest, HITLDecision, HITLResponse
    import src.tools.secretary.dispatch as disp_mod
    import src.server.services.report_back.handoff.reserve as reserve_mod
    import src.config.settings as cfg_settings

    ws_id = "11111111-1111-1111-1111-111111111111"
    home_ws_id = "22222222-2222-2222-2222-222222222222"
    call_id = "call-cos-r4-1"
    card_question = "更新 PDD 模型\n\n<user_verbatim_request>\n请保留历史列\nAttachments: Q3.pdf\n</user_verbatim_request>"

    # 1. settle_resume_request stamps approved_dispatch_payloads from its ALREADY-read `newest` tuple
    fake_cp_tuple = _NS(
        config={"configurable": {"thread_id": "home-thread-r4", "checkpoint_id": "cp-1"}},
        checkpoint={"channel_values": {}},
        pending_writes=[
            (
                "task-1",
                "__interrupt__",
                [
                    _NS(
                        id="intr-r4-1",
                        value={
                            "action_requests": [
                                {
                                    "type": "ptc_agent",
                                    "tool_call_id": call_id,
                                    "question": card_question,
                                    "workspace_id": ws_id,
                                    "thread_id": None,
                                }
                            ]
                        },
                    )
                ],
            )
        ],
    )
    cp_saver = create_autospec(BaseCheckpointSaver, instance=True)
    cp_saver.aget_tuple.return_value = fake_cp_tuple

    monkeypatch.setattr(cp_handler, "get_checkpointer", lambda: cp_saver)
    monkeypatch.setattr(
        cp_handler.threads_read,
        "get_thread_branch_read",
        create_autospec(cp_handler.threads_read.get_thread_branch_read, return_value=_NS(run_horizon=7)),
    )
    monkeypatch.setattr(
        cp_handler,
        "turn_base_checkpoint_id",
        create_autospec(cp_handler.turn_base_checkpoint_id, return_value=None),
    )

    req = ChatRequest(
        workspace_id=home_ws_id,
        hitl_response={"intr-r4-1": HITLResponse(decisions=[HITLDecision(type="approve")])},
    )
    settled = await cp_handler.settle_resume_request("home-thread-r4", req)
    assert cp_saver.aget_tuple.call_count == 1
    assert settled.approved_dispatch_payloads is not None
    assert settled.approved_dispatch_payloads[call_id]["question"] == card_question

    # 2. Checkpoint read failure in load_approved_dispatch_payloads returns None (not {})
    failing_cp = create_autospec(BaseCheckpointSaver, instance=True)
    failing_cp.aget_tuple.side_effect = RuntimeError("db pool transient failure")
    assert await load_approved_dispatch_payloads(failing_cp, "home-thread-r4", hitl_response={"intr-r4-1": {}}) is None

    # 3. Approved dispatch on HITL resume with missing bound payload MUST stop and return retryable error!
    async def _fake_owned_ws(wid: str, uid: str, *, is_continuation: bool = False) -> dict[str, Any]:
        return {"workspace_id": wid, "user_id": uid, "name": "PDD"}

    async def _fake_unreachable(_wid: str) -> None:
        return None

    monkeypatch.setattr(disp_mod, "_owned_workspace", create_autospec(disp_mod._owned_workspace, side_effect=_fake_owned_ws))
    monkeypatch.setattr(disp_mod, "_unreachable_computer", create_autospec(disp_mod._unreachable_computer, side_effect=_fake_unreachable))
    monkeypatch.setattr(cfg_settings, "background_dispatch_requires_token", lambda: False)

    orig_hitl = disp_mod.hitl_confirm
    monkeypatch.setattr(
        disp_mod,
        "hitl_confirm",
        create_autospec(orig_hitl, return_value=(True, {"decisions": [{"type": "approve"}]})),
    )
    reserve_spy = create_autospec(reserve_mod.reserve, side_effect=AssertionError("MUST NOT reserve slot on missing payload"))
    monkeypatch.setattr(reserve_mod, "reserve", reserve_spy)
    post_spy = create_autospec(aiohttp.ClientSession, side_effect=AssertionError("MUST NOT POST on missing payload"))
    monkeypatch.setattr(aiohttp, "ClientSession", post_spy)

    for bad_payload_val in (None, {}, {"other-call-id": {"question": "other"}}):
        resume_cfg = {
            "configurable": {
                "user_id": "user-1",
                "workspace_id": home_ws_id,
                "thread_id": "home-thread-r4",
                COS_TURN_VERBATIM_CONFIG_KEY: None,
                COS_HITL_RESUME_CONFIG_KEY: True,
                COS_APPROVED_DISPATCH_PAYLOADS_CONFIG_KEY: bad_payload_val,
            }
        }
        cmd = await disp_mod.dispatch(
            question="更新 PDD 模型（模型缩写原参数）",
            config=resume_cfg,
            workspace_id=ws_id,
            thread_id=None,
            report_back=True,
            tool_call_id=call_id,
            preapproved=False,
            state={"messages": []},
        )
        msg = cmd.update["messages"][0]
        body = json.loads(msg.content)
        assert body["success"] is False
        assert "retry" in body["error"].lower() or "payload" in body["error"].lower(), body
        assert reserve_spy.call_count == 0
        assert post_spy.call_count == 0

    # 4. Rejected card still declines cleanly even when bound payload is None
    monkeypatch.setattr(
        disp_mod,
        "hitl_confirm",
        create_autospec(orig_hitl, return_value=(False, {"decisions": [{"type": "reject"}]})),
    )
    reject_cmd = await disp_mod.dispatch(
        question="更新 PDD 模型",
        config={
            "configurable": {
                "user_id": "user-1",
                "workspace_id": home_ws_id,
                "thread_id": "home-thread-r4",
                COS_TURN_VERBATIM_CONFIG_KEY: None,
                COS_HITL_RESUME_CONFIG_KEY: True,
                COS_APPROVED_DISPATCH_PAYLOADS_CONFIG_KEY: None,
            }
        },
        workspace_id=ws_id,
        thread_id=None,
        report_back=True,
        tool_call_id=call_id,
        preapproved=False,
        state={"messages": []},
    )
    rej_content = str(reject_cmd.update["messages"][0].content)
    assert "declined" in rej_content.lower()
    assert post_spy.call_count == 0


# ===========================================================================
# P0-b Round 5 narrowed fixes (Items 1-6)
# ===========================================================================


@pytest.mark.asyncio
async def test_p0b_r5_1_revise_model_probes_prebound_first_disk_checks_unchanged_and_degrades_on_missing_new(
    tmp_path: Path,
) -> None:
    """R5 #1:
    1) Pre-bound path is probed BEFORE trusting `changed=false`: if v03 is readable on disk
       while child reported `changed=false`, workflow proceeds on v03;
    2) When `changed=false` and v03 is absent, parent probes current v02 on disk and uses
       real disk SHA-256 (never child self-reported SHA);
    3) When child reports `changed=true` but pre-bound v03 is absent/unreadable, workflow
       writes fallback ledger on readable v02 (`delivered_with_open_issues`, `ok=True`) and
       continues report revision instead of aborting."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )

    stages_src = (REPO_ROOT / "workflows" / "_shared" / "modeling_stages.js").read_text(encoding="utf-8")
    rev_harness_js = f"{stages_src}\nreturn await reviseModelForReport(args)\n"

    ws = tmp_path / "ws_r5_1"
    r_rel = "research/2026-11-27-deep-FY26Q3"
    (ws / r_rel / "model_dump").mkdir(parents=True, exist_ok=True)
    v2 = ws / "model" / "PDD-model-v02.xlsx"
    v3 = ws / "model" / "PDD-model-v03.xlsx"
    v2_sha = _write_simple_valid_model(v2)
    v3_sha = _write_simple_valid_model(v3)
    assert v2_sha and v3_sha

    rev_args = {
        "workspaceDir": ".",
        "ticker": "PDD",
        "researchDir": r_rel,
        "progressPath": f"{r_rel}/.progress.json",
        "workflow": "earnings-deep-update",
        "mode": "update",
        "currentModelPath": "model/PDD-model-v02.xlsx",
        "currentModelSha256": v2_sha,
        "currentStatus": "pass",
        "currentArtifacts": [],
        "consensusPath": "data/consensus/snap.json",
        "priorFailedStages": [],
        "runId": "run-r5-1",
        "reportPath": "drafts/2026-11-27-deep-FY26Q3.md",
        "reviewFeedback": {"verdict": "revise", "must_fix": ["update assumption"]},
        "phaseLabel": "审稿",
    }

    async def _run_rev(handler: Any) -> tuple[Any, list[str]]:
        lbls: list[str] = []
        spec_host = create_autospec(WorkflowHost, instance=True)

        async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
            lbl = str(opts.get("label") or "")
            lbls.append(lbl)
            return handler(lbl, prompt)

        spec_host.agent.side_effect = _agent
        spec_host.phase.side_effect = lambda _t: None
        spec_host.log.side_effect = lambda _m: None
        out = await run_workflow_script(rev_harness_js, rev_args, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
        assert out.status == "completed", out.error
        return out.result, lbls

    # Case A: Child reports changed=false, but pre-bound v03 IS readable on disk -> must proceed on v03!
    def _h_false_but_published(lbl: str, prompt: str) -> Any:
        if lbl == "计算联动新路径":
            return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
        if lbl == "模型联动修订":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "claimed unchanged", "changed": False, "model_path": "model/PDD-model-v02.xlsx", "model_sha256": "a" * 64}
        if lbl == "核验联动候选落盘":
            assert "model/PDD-model-v03.xlsx" in prompt
            return {"path": "model/PDD-model-v03.xlsx", "exists": True, "readable": True, "model_sha256": v3_sha, "run_matched": True}
        if lbl == "模型联动差异刷新":
            return {"files": [f"{r_rel}/model_dump/consensus-diff.md", f"{r_rel}/model_dump/consensus-diff.json"], "summary": "diff", "consensus_diff_generated": True}
        if lbl == "核验差异表落盘":
            return {"path": f"{r_rel}/model_dump/consensus-diff.json", "json_ok": True, "md_ok": True, "matched": True}
        if lbl == "模型联动报告同步":
            return {"files": ["drafts/2026-11-27-deep-FY26Q3.md"], "summary": "sync", "report_sha256": "b" * 64}
        if lbl == "核验报告同步落盘":
            return {"path": "drafts/2026-11-27-deep-FY26Q3.md", "exists": True, "synced": True, "report_sha256": "c" * 64}
        if lbl == "模型联动修订检查":
            assert '--candidate "model/PDD-model-v03.xlsx"' in prompt
            assert '--failed-stages "recalc"' in prompt
            return {
                "files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "delivered",
                "readable": True,
                "final_status": "delivered_with_open_issues",
                "model_sha256": v3_sha,
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md",
            }
        raise AssertionError(lbl)

    res_a, lbls_a = await _run_rev(_h_false_but_published)
    assert res_a["ok"] is True and res_a["changed"] is True
    assert res_a["modelPath"] == "model/PDD-model-v03.xlsx"
    assert res_a["modelSha256"] == v3_sha
    assert "核验联动候选落盘" in lbls_a and "模型联动修订检查" in lbls_a

    # Case B: Child reports changed=false with spoofed SHA, v03 absent -> parent probes current v02 on disk & ignores self-reported SHA!
    spoof_sha = "f" * 64

    def _h_false_real_disk_sha(lbl: str, prompt: str) -> Any:
        if lbl == "计算联动新路径":
            return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
        if lbl == "模型联动修订":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "no change", "changed": False, "model_path": "model/PDD-model-v02.xlsx", "model_sha256": spoof_sha}
        if lbl == "核验联动候选落盘":
            return {"path": "model/PDD-model-v03.xlsx", "exists": False, "readable": False, "model_sha256": None}
        if lbl == "核验当前模型落盘":
            assert "model/PDD-model-v02.xlsx" in prompt
            return {"path": "model/PDD-model-v02.xlsx", "exists": True, "readable": True, "model_sha256": v2_sha}
        raise AssertionError(lbl)

    res_b, lbls_b = await _run_rev(_h_false_real_disk_sha)
    assert res_b["ok"] is True and res_b["changed"] is False
    assert res_b["modelPath"] == "model/PDD-model-v02.xlsx"
    assert res_b["modelSha256"] == v2_sha and res_b["modelSha256"] != spoof_sha
    assert "核验联动候选落盘" in lbls_b and "核验当前模型落盘" in lbls_b

    # Case C: End-to-end earnings-deep-update workflow: child claims changed=true without v03 on disk ->
    # writes real fallback ledger on v02 (`delivered_with_open_issues`) and report revision (`修订 1`) CONTINUES!
    v3.unlink()
    deep_script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "earnings-deep-update" / "workflow.js")
    deep_params = {
        "ticker": "PDD",
        "company": "PDD Holdings",
        "quarter": "FY26Q3",
        "workspace_dir": ".",
        "release_date": "2026-11-27",
        "model_path": "model/PDD-model-v02.xlsx",
        "preview_predictions": None,
        "progress_path": f"{r_rel}/.progress.json",
    }
    nine_items = [{"check": i, "result": "pass", "reason": "ok", "lines": "L1"} for i in range(1, 10)]
    seen_deep_labels: list[str] = []
    spec_host_deep = create_autospec(WorkflowHost, instance=True)

    async def _deep_agent(prompt: str, opts: dict[str, Any]) -> Any:
        lbl = str(opts.get("label") or "")
        seen_deep_labels.append(lbl)
        if lbl == "八节点评":
            return {"files": ["drafts/2026-11-27-deep-FY26Q3.md"], "summary": "draft", "sha256": "1" * 64}
        if lbl == "产物核验":
            paths = [ln[2:].strip() for ln in prompt.splitlines() if ln.startswith("- ") and "|" not in ln]
            return {"files": [f"{p}|{'e' * 64}" for p in paths]}
        if lbl == "建模准备":
            return {"files": [f"{r_rel}/mapping.md"], "summary": "prep", "mode": "update", "baseline_path": "model/PDD-model-v01.xlsx", "planned_output_path": "model/PDD-model-v02.xlsx"}
        if lbl == "读取运行ID":
            return {"run_id": "run-r5-deep"}
        if lbl == "模型构建与落盘":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "v02", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": v2_sha, "recalc_status": "ok"}
        if lbl == "核验候选落盘":
            return {"path": "model/PDD-model-v02.xlsx", "exists": True, "readable": True, "model_sha256": v2_sha, "run_matched": True}
        if lbl == "一致预期对照":
            return {"files": ["model/PDD-model-v02.xlsx", f"{r_rel}/model_dump/consensus-diff.md", f"{r_rel}/model_dump/consensus-diff.json"], "summary": "cons", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": v2_sha, "consensus_diff_generated": True}
        if lbl == "模型本轮范围检查":
            return {"files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json"], "summary": "pass", "readable": True, "final_status": "pass", "model_sha256": v2_sha, "actionable_count": 0, "missing_required_sheets": []}
        if lbl == "模型变动拆解":
            return {"files": [f"{r_rel}/model-delta.md"], "summary": "delta"}
        if lbl == "校准与对账":
            return {"files": [f"{r_rel}/reconcile.md"], "summary": "cal", "sha256": "2" * 64}
        if lbl == "语言审查 1":
            return {"files": ["drafts/2026-11-27-deep-FY26Q3.md"], "summary": "lang1", "sha256": "3" * 64}
        if lbl == "审稿":
            return {"verdict": "revise", "summary": "adjust model", "must_fix": ["fix eps"], "items": nine_items}
        if lbl == "计算联动新路径":
            return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
        if lbl == "模型联动修订":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "claimed changed without v03", "changed": True, "model_path": "model/PDD-model-v03.xlsx", "model_sha256": "e" * 64, "recalc_status": "ok"}
        if lbl == "核验联动候选落盘":
            return {"path": "model/PDD-model-v03.xlsx", "exists": False, "readable": False, "model_sha256": None}
        if lbl == "写入降级账本":
            fb = mchk.write_fallback_ledger(
                workspace_dir=ws,
                ticker="PDD",
                research_dir=ws / r_rel,
                candidate_path=v2,
                baseline_path=v2,
                kind="report_revision",
                failed_stages=["report_revision"],
                run_id="run-r5-deep",
                workflow="earnings-deep-update",
                mode="update",
            )
            return {
                "files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "fallback written",
                "final_status": fb["final_status"],
                "model_sha256": fb["model_sha256"],
                "run_id": "run-r5-deep",
            }
        if lbl == "修订 1":
            return {"files": ["drafts/2026-11-27-deep-FY26Q3.md"], "summary": "revised report", "sha256": "4" * 64}
        if lbl == "复审 1":
            return {"verdict": "pass", "summary": "ok", "must_fix": [], "items": nine_items}
        if lbl == "笔记语言审查":
            return {"files": [f"{r_rel}/reconcile.md"], "summary": "notes ok"}
        if lbl == "语言审查 2":
            return {"files": ["drafts/2026-11-27-deep-FY26Q3.md"], "summary": "lang2", "sha256": "4" * 64}
        if lbl == "知识层修订草案":
            return {"files": [f"{r_rel}/knowledge-revision.json"], "summary": "kr"}
        return {"files": [], "summary": "ok"}

    spec_host_deep.agent.side_effect = _deep_agent
    spec_host_deep.phase.side_effect = lambda _t: None
    spec_host_deep.log.side_effect = lambda _m: None
    deep_out = await run_workflow_script(deep_script, deep_params, spec_host_deep, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
    assert deep_out.status == "completed", deep_out.error
    assert "写入降级账本" in seen_deep_labels
    assert "修订 1" in seen_deep_labels and "复审 1" in seen_deep_labels, seen_deep_labels
    assert deep_out.result["ok"] is True, deep_out.result
    assert deep_out.result["model_status"] == "delivered_with_open_issues"
    assert deep_out.result["model_file"] == "model/PDD-model-v02.xlsx"


@pytest.mark.asyncio
async def test_p0b_r5_2_report_sync_probe_executes_real_command_and_requires_report_body(
    tmp_path: Path,
) -> None:
    """R5 #2: Report sync disk probe executes the REAL read-only python command from
    the prompt (never hand-fabricated probe results). Updating only `.memo-model.json`
    or `model-delta.md` while leaving report body without `model:` link or new path
    MUST return `synced=False` and record `report_sync`."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )

    stages_src = (REPO_ROOT / "workflows" / "_shared" / "modeling_stages.js").read_text(encoding="utf-8")
    rev_harness_js = f"{stages_src}\nreturn await reviseModelForReport(args)\n"

    ws = tmp_path / "ws_r5_2"
    r_rel = "research/2026-11-27-fc"
    (ws / r_rel / "model_dump").mkdir(parents=True, exist_ok=True)
    (ws / "drafts").mkdir(parents=True, exist_ok=True)
    v2 = ws / "model" / "PDD-model-v02.xlsx"
    v3 = ws / "model" / "PDD-model-v03.xlsx"
    v2_sha = _write_simple_valid_model(v2)
    v3_sha = _write_simple_valid_model(v3)
    memo_p = ws / "drafts" / "2026-11-27-memo.md"
    meta_p = ws / r_rel / ".memo-model.json"
    delta_p = ws / r_rel / "model-delta.md"

    def _run_real_probe_from_prompt(prompt: str) -> dict[str, Any]:
        m = re.search(r"```bash\n(.*?)\n```", prompt, re.DOTALL)
        assert m, f"Missing bash command block in prompt:\n{prompt}"
        proc = subprocess.run(
            ["bash", "-c", m.group(1)],
            cwd=ws,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"Probe command failed: {proc.stderr}\n{proc.stdout}"
        return json.loads(proc.stdout.strip())

    async def _run_case(body_text: str, meta_doc: dict[str, str], delta_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
        memo_p.write_text(body_text, encoding="utf-8")
        meta_p.write_text(json.dumps(meta_doc), encoding="utf-8")
        delta_p.write_text(delta_text, encoding="utf-8")
        probe_out_holder: list[dict[str, Any]] = []

        rev_args = {
            "workspaceDir": ".",
            "ticker": "PDD",
            "researchDir": r_rel,
            "progressPath": f"{r_rel}/.progress.json",
            "workflow": "full-coverage",
            "mode": "update",
            "currentModelPath": "model/PDD-model-v02.xlsx",
            "currentModelSha256": v2_sha,
            "currentStatus": "pass",
            "currentArtifacts": [],
            "consensusPath": "data/consensus/snap.json",
            "priorFailedStages": [],
            "runId": "run-r5-2",
            "reportPath": "drafts/2026-11-27-memo.md",
            "extraSyncPaths": [f"{r_rel}/.memo-model.json", f"{r_rel}/model-delta.md"],
            "reviewFeedback": {"verdict": "revise", "must_fix": ["sync"]},
            "phaseLabel": "修订",
        }
        spec_host = create_autospec(WorkflowHost, instance=True)

        async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
            lbl = str(opts.get("label") or "")
            if lbl == "计算联动新路径":
                return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
            if lbl == "模型联动修订":
                return {"files": ["model/PDD-model-v03.xlsx"], "summary": "v03", "changed": True, "model_path": "model/PDD-model-v03.xlsx", "model_sha256": v3_sha, "recalc_status": "ok"}
            if lbl == "核验联动候选落盘":
                return {"path": "model/PDD-model-v03.xlsx", "exists": True, "readable": True, "model_sha256": v3_sha, "run_matched": True}
            if lbl == "模型联动差异刷新":
                return {"files": [f"{r_rel}/model_dump/consensus-diff.md", f"{r_rel}/model_dump/consensus-diff.json"], "summary": "diff", "consensus_diff_generated": True}
            if lbl == "核验差异表落盘":
                return {"path": f"{r_rel}/model_dump/consensus-diff.json", "json_ok": True, "md_ok": True, "matched": True}
            if lbl == "模型联动报告同步":
                return {"files": ["drafts/2026-11-27-memo.md", f"{r_rel}/.memo-model.json"], "summary": "sync attempt", "report_sha256": "0" * 64}
            if lbl == "核验报告同步落盘":
                real_probe = _run_real_probe_from_prompt(prompt)
                probe_out_holder.append(real_probe)
                return real_probe
            if lbl == "模型联动修订检查":
                has_rs = "report_sync" in prompt
                return {
                    "files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                    "summary": "chk",
                    "readable": True,
                    "final_status": "delivered_with_open_issues" if has_rs else "pass",
                    "model_sha256": v3_sha,
                    "actionable_count": 0,
                    "actionable_issues": [],
                    "missing_required_sheets": [],
                    "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md" if has_rs else None,
                }
            raise AssertionError(lbl)

        spec_host.agent.side_effect = _agent
        spec_host.phase.side_effect = lambda _t: None
        spec_host.log.side_effect = lambda _m: None
        out = await run_workflow_script(rev_harness_js, rev_args, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
        assert out.status == "completed", out.error
        assert len(probe_out_holder) == 1
        return out.result, probe_out_holder[0]

    # 1. Extras updated to v03, report body STILL stale (no model: link and no v03 path) -> synced=False!
    res_stale, probe_stale = await _run_case(
        body_text="# 备忘录\n正文仍引用旧模型 model/PDD-model-v02.xlsx，未同步更新。\n",
        meta_doc={"path": "model/PDD-model-v03.xlsx", "sha256": v3_sha},
        delta_text="新模型为 model/PDD-model-v03.xlsx，EPS 上调。\n",
    )
    assert probe_stale["synced"] is False, probe_stale
    assert "report_sync" in res_stale["failedStages"]

    # 2. Report body updated with model: link to v03, but .memo-model.json still stale -> synced=False!
    res_meta_stale, probe_meta_stale = await _run_case(
        body_text="# 备忘录\n目标价见 model:model/PDD-model-v03.xlsx!Valuation!C12。\n",
        meta_doc={"path": "model/PDD-model-v02.xlsx", "sha256": v2_sha},
        delta_text="新模型为 model/PDD-model-v03.xlsx。\n",
    )
    assert probe_meta_stale["synced"] is False, probe_meta_stale
    assert "report_sync" in res_meta_stale["failedStages"]

    # 3. Both report body (model: link to v03) and extras synced -> synced=True with real SHA!
    res_link_ok, probe_link_ok = await _run_case(
        body_text="# 备忘录\n目标价见 model:model/PDD-model-v03.xlsx!Valuation!C12。\n",
        meta_doc={"path": "model/PDD-model-v03.xlsx", "sha256": v3_sha},
        delta_text="新模型为 model:model/PDD-model-v03.xlsx!Facts!A1。\n",
    )
    assert probe_link_ok["synced"] is True, probe_link_ok
    assert probe_link_ok["report_sha256"] == mfp.file_sha256(memo_p)
    assert "report_sync" not in res_link_ok["failedStages"]

    # 4. Report body contains new path string directly (no model: link) and extras synced -> synced=True!
    res_path_ok, probe_path_ok = await _run_case(
        body_text="# 备忘录\n估值已切换至 model/PDD-model-v03.xlsx。\n",
        meta_doc={"path": "model/PDD-model-v03.xlsx", "sha256": v3_sha},
        delta_text="新模型为 model/PDD-model-v03.xlsx。\n",
    )
    assert probe_path_ok["synced"] is True, probe_path_ok
    assert "report_sync" not in res_path_ok["failedStages"]


@pytest.mark.asyncio
async def test_p0b_r5_3_output_path_restricted_to_workspace_model_and_atomic_no_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5 #3:
    1) JS `_mValidModelOutPath` accepts only relative paths under `model/*.xlsx`;
    2) `model_edit.apply_edit_plan` verifies real path resides under `<workspace>/model/`
       (rejecting sibling dirs, traversal, and symlinks escaping `model/`);
    3) Non-in-place publish atomically refuses to overwrite an existing version file
       both pre-edit AND if created concurrently right before publish."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )

    stages_src = (REPO_ROOT / "workflows" / "_shared" / "modeling_stages.js").read_text(encoding="utf-8")
    js_check = (
        f"{stages_src}\n"
        "return {\n"
        "  ok: _mValidModelOutPath('model/PDD-model-v02.xlsx', 'model/PDD-model-v01.xlsx', '.'),\n"
        "  outside_drafts: _mValidModelOutPath('drafts/PDD-model-v02.xlsx', 'model/PDD-model-v01.xlsx', '.'),\n"
        "  outside_root: _mValidModelOutPath('PDD-model-v02.xlsx', 'model/PDD-model-v01.xlsx', '.'),\n"
        "  traversal: _mValidModelOutPath('model/../drafts/PDD-model-v02.xlsx', 'model/PDD-model-v01.xlsx', '.'),\n"
        "  same_base: _mValidModelOutPath('model/PDD-model-v01.xlsx', 'model/PDD-model-v01.xlsx', '.'),\n"
        "}\n"
    )
    spec_host = create_autospec(WorkflowHost, instance=True)
    js_out = await run_workflow_script(js_check, {}, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=5.0))
    assert js_out.status == "completed", js_out.error
    assert js_out.result["ok"] == "model/PDD-model-v02.xlsx"
    assert js_out.result["outside_drafts"] is None
    assert js_out.result["outside_root"] is None
    assert js_out.result["traversal"] is None
    assert js_out.result["same_base"] is None

    ws = tmp_path / "ws_r5_3"
    base = ws / "model" / "PDD-model-v01.xlsx"
    _write_simple_valid_model(base)
    fp = mfp.build_fingerprint(base, ticker="PDD")
    plan = {"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1230}]}

    # 2a. Outside workspace model/ (drafts/ or sibling path) -> rejected
    with pytest.raises(ValueError, match="outside workspace model/"):
        medit.apply_edit_plan(
            baseline_path=base,
            ticker="PDD",
            plan=plan,
            output_path=ws / "drafts" / "PDD-model-v02.xlsx",
            workspace_dir=ws,
            fingerprint=fp,
            skip_recalc=True,
        )

    # 2b. Symlink directory inside model/ pointing outside workspace -> rejected
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    evil_link = ws / "model" / "escape"
    evil_link.symlink_to(outside_dir)
    with pytest.raises(ValueError, match="outside workspace model/"):
        medit.apply_edit_plan(
            baseline_path=base,
            ticker="PDD",
            plan=plan,
            output_path=evil_link / "PDD-model-v02.xlsx",
            workspace_dir=ws,
            fingerprint=fp,
            skip_recalc=True,
        )

    # 3a. Already-existing version file -> rejected before publish and untouched
    existing_v2 = ws / "model" / "PDD-model-v02.xlsx"
    existing_v2_sha = _write_simple_valid_model(existing_v2)
    with pytest.raises(ValueError, match="existing model version"):
        medit.apply_edit_plan(
            baseline_path=base,
            ticker="PDD",
            plan=plan,
            output_path=existing_v2,
            workspace_dir=ws,
            fingerprint=fp,
            skip_recalc=True,
        )
    assert mfp.file_sha256(existing_v2) == existing_v2_sha

    # 3b. Concurrent creation AFTER pre-edit check, right before publish -> atomic os.link refuses overwrite!
    raced_v3 = ws / "model" / "PDD-model-v03.xlsx"
    orig_restore = medit.model_xlsx_hooks.restore_donor_cache
    raced_sentinel_sha: list[str] = []

    def _restore_with_concurrent_writer(*args: Any, **kwargs: Any) -> Any:
        res = orig_restore(*args, **kwargs)
        raced_sentinel_sha.append(_write_simple_valid_model(raced_v3))
        return res

    monkeypatch.setattr(
        medit.model_xlsx_hooks,
        "restore_donor_cache",
        create_autospec(orig_restore, side_effect=_restore_with_concurrent_writer),
    )
    with pytest.raises(ValueError, match="existing model version"):
        medit.apply_edit_plan(
            baseline_path=base,
            ticker="PDD",
            plan=plan,
            output_path=raced_v3,
            workspace_dir=ws,
            fingerprint=fp,
            skip_recalc=True,
        )
    assert raced_sentinel_sha and mfp.file_sha256(raced_v3) == raced_sentinel_sha[0]


def test_p0b_r5_4_verify_done_expected_mode_rejects_tampered_full_coverage_ledger(
    tmp_path: Path,
) -> None:
    """R5 #4 (CLI + Python): Full-coverage verify-done accepts `--expected-mode` and
    rejects a ledger whose `mode` was tampered from `build` to `update` even when
    the workbook has all 4 update sheets (still missing build-only sheets)."""
    ws = tmp_path / "ws_r5_4"
    rdir = ws / "research" / "2026-11-27-fc"
    v1 = ws / "model" / "PDD-model-v01.xlsx"
    _write_simple_valid_model(v1)
    wb = openpyxl.load_workbook(v1)
    for sh in ("README & Changelog", "Facts", "Guidance vs Actual", "Street vs House", "Checks"):
        if sh not in wb.sheetnames:
            wb.create_sheet(sh)["A1"] = sh
    wb.save(v1)
    wb.close()

    # Write a clean update-mode ledger on this 4-sheet workbook:
    # R6 #6: full-coverage verify-done without --expected-mode MUST FAIL CLOSED (never trust ledger self-report!)
    mchk.write_fallback_ledger(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=rdir,
        candidate_path=v1,
        kind="modeling",
        failed_stages=["check", "consensus"],
        run_id="run-r5-4",
        workflow="full-coverage",
        mode="update",
    )
    vd_without_mode = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=rdir,
        expected_run_id="run-r5-4",
        expected_workflow="full-coverage",
    )
    assert vd_without_mode["ok"] is False
    assert any("--expected-mode" in e for e in vd_without_mode["errors"]), vd_without_mode["errors"]

    # Explicit expected_mode="update" passes on matching 4-sheet update ledger
    vd_with_update = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=rdir,
        expected_run_id="run-r5-4",
        expected_workflow="full-coverage",
        expected_mode="update",
    )
    assert vd_with_update["ok"] is True, vd_with_update["errors"]

    # Invalid expected_mode fails closed
    vd_with_invalid = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=rdir,
        expected_run_id="run-r5-4",
        expected_workflow="full-coverage",
        expected_mode="auto",
    )
    assert vd_with_invalid["ok"] is False
    assert any("invalid expected_mode" in e for e in vd_with_invalid["errors"]), vd_with_invalid["errors"]

    # With expected_mode="build" (from this run's prepare step), MUST fail both mode mismatch AND missing build sheets!
    vd_with_build = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=rdir,
        expected_run_id="run-r5-4",
        expected_workflow="full-coverage",
        expected_mode="build",
    )
    assert vd_with_build["ok"] is False
    assert any("mode mismatch" in e for e in vd_with_build["errors"]), vd_with_build["errors"]
    assert any("full-coverage (build)" in e and "DCF" in e for e in vd_with_build["errors"]), vd_with_build["errors"]

    # CLI --expected-mode build returns non-zero exit code
    rc = mchk.main(
        [
            "verify-done",
            "--workspace",
            str(ws),
            "--ticker",
            "PDD",
            "--research-dir",
            str(rdir),
            "--expected-run-id",
            "run-r5-4",
            "--expected-workflow",
            "full-coverage",
            "--expected-mode",
            "build",
        ]
    )
    assert rc != 0


def test_p0b_r5_5_manual_commands_consistent_complete_with_workflow_and_mode() -> None:
    """R5 #5: Manual check, re-check, and fallback ledger commands across dalton-model,
    rollforward.md, dalton-full-coverage, and dalton-earnings-deep are complete commands
    with explicit `--workflow` and `--mode`, leaving no bare fragments."""
    model_skill = (REPO_ROOT / "plugins/dalton/skills/dalton-model/SKILL.md").read_text(encoding="utf-8")
    rollforward = (REPO_ROOT / "plugins/dalton/skills/dalton-model/references/rollforward.md").read_text(encoding="utf-8")
    fc_skill = (REPO_ROOT / "plugins/dalton/skills/dalton-full-coverage/SKILL.md").read_text(encoding="utf-8")
    deep_skill = (REPO_ROOT / "plugins/dalton/skills/dalton-earnings-deep/SKILL.md").read_text(encoding="utf-8")

    # 1. No bare fragment re-check commands remain
    assert "`model_check.py check --fix-round <1|2>`" not in rollforward
    assert "`model_check.py check --mode <build|update> --fix-round <1|2>`" not in model_skill

    # 2. dalton-model SKILL.md & rollforward.md share full parameter set with explicit --mode
    for doc_name, doc_text, mode_tok in (
        ("dalton-model/SKILL.md", model_skill, "--mode <build|update>"),
        ("rollforward.md", rollforward, "--mode update"),
    ):
        chk_cmds = re.findall(r"`(python3 \.agents/skills/dalton-model/scripts/model_check\.py check [^`]+)`", doc_text)
        assert len(chk_cmds) >= 2, (doc_name, chk_cmds)
        for cmd in chk_cmds:
            for flag in ("--workspace", "--ticker", "--research-dir", "--candidate", "--baseline", "--fingerprint", "--coord-map", "--workflow company-model", mode_tok, "--fix-round"):
                assert flag in cmd, (doc_name, flag, cmd)

    # 3. full-coverage & earnings-deep manual commands explicitly specify their own --workflow and --mode
    for doc_name, doc_text, wf_flag, mode_flag in (
        ("dalton-full-coverage/SKILL.md", fc_skill, "--workflow full-coverage", "--mode <build|update>"),
        ("dalton-earnings-deep/SKILL.md", deep_skill, "--workflow earnings-deep-update", "--mode update"),
    ):
        chk_cmds = re.findall(r"`(python3 \.agents/skills/dalton-model/scripts/model_check\.py check [^`]+)`", doc_text)
        wl_cmds = re.findall(r"`(python3 \.agents/skills/dalton-model/scripts/model_check\.py write-ledger [^`]+)`", doc_text)
        assert len(chk_cmds) >= 3, (doc_name, chk_cmds)
        assert len(wl_cmds) >= 1, (doc_name, wl_cmds)
        for cmd in chk_cmds:
            for flag in ("--workspace", "--ticker", "--research-dir", "--candidate", wf_flag, mode_flag):
                assert flag in cmd, (doc_name, flag, cmd)
        for cmd in wl_cmds:
            for flag in ("--workspace", "--ticker", "--research-dir", "--candidate", "--failed-stages", wf_flag, mode_flag):
                assert flag in cmd, (doc_name, flag, cmd)


def test_p0b_r5_6_cos_verbatim_filename_special_chars_and_reversible_tag_escape() -> None:
    """R5 #6:
    1) `_basename_only` preserves `#` and `?` in real filenames/local paths while still
       stripping scheme/host/query/fragment from URLs;
    2) `_escape_closing_tag` / `_unescape_closing_tag` is strictly reversible and preserves
       original casing and whitespace."""
    from types import SimpleNamespace
    from src.dalton.cos_verbatim import (
        COS_TURN_VERBATIM_CONFIG_KEY,
        _basename_only,
        _escape_closing_tag,
        _unescape_closing_tag,
        append_verbatim_user_request,
        capture_turn_verbatim_request,
    )

    # 1. Real filenames & local paths preserve `#` and `?`; URLs strip query & fragment
    assert _basename_only("Q3#final.xlsx") == "Q3#final.xlsx"
    assert _basename_only("/tmp/uploads/Q3#final.xlsx") == "Q3#final.xlsx"
    assert _basename_only("work/uploads/rev?v2#1.xlsx") == "rev?v2#1.xlsx"
    assert _basename_only(r"C:\Users\analyst\Q3#final?draft.xlsx") == "Q3#final?draft.xlsx"
    assert _basename_only("https://cdn.example.com/path/Q3_Release.pdf?token=secret#page=2") == "Q3_Release.pdf"
    assert _basename_only("s3://bucket/folder/Q3%23final.xlsx?X-Amz-Signature=abc") == "Q3#final.xlsx"

    # 2. Reversible closing-tag escaping preserves casing, whitespace, and literal entities
    samples = [
        "原文 </user_verbatim_request> 结束",
        "大写 </USER_VERBATIM_REQUEST> 与空格 </  User_Verbatim_Request\t > 保留",
        "混合字面量 &lt;/User_Verbatim_Request&gt; 与 &amp;lt;/user_verbatim_request&gt; 与 </USER_VERBATIM_REQUEST >",
    ]
    for s in samples:
        escaped = _escape_closing_tag(s)
        assert not re.search(r"</\s*user_verbatim_request\s*>", escaped, re.IGNORECASE), escaped
        assert _unescape_closing_tag(escaped) == s

    assert "&lt;/USER_VERBATIM_REQUEST&gt;" in _escape_closing_tag("</USER_VERBATIM_REQUEST>")
    assert "&lt;/  User_Verbatim_Request\t &gt;" in _escape_closing_tag("</  User_Verbatim_Request\t >")

    # 3. End-to-end capture + append preserves attachment `#`/`?` and tag casing/spaces
    req = SimpleNamespace(
        hitl_response=None,
        report_source=None,
        wakeup=None,
        market_target=None,
        origin=None,
        additional_context=[
            SimpleNamespace(type="chat_upload", name="/tmp/uploads/Q3#final.xlsx"),
            SimpleNamespace(type="file", name="work/notes?v2.pdf"),
            SimpleNamespace(type="file", name="https://cdn.example.com/a/Deck.pdf?token=secret"),
        ],
    )
    raw = "保留 </  USER_VERBATIM_REQUEST > 原样"
    cap = capture_turn_verbatim_request(request=req, raw_messages=[{"role": "user", "content": raw}])
    assert cap is not None
    assert cap["attachments"] == ["Q3#final.xlsx", "notes?v2.pdf", "Deck.pdf"]
    out = append_verbatim_user_request("更新模型", config={"configurable": {COS_TURN_VERBATIM_CONFIG_KEY: cap}})
    assert out.count("</user_verbatim_request>") == 1
    assert "&lt;/  USER_VERBATIM_REQUEST &gt;" in out
    assert "Attachments: Q3#final.xlsx, notes?v2.pdf, Deck.pdf" in out
    assert "token=secret" not in out


@pytest.mark.asyncio
async def test_p0b_r6_1_per_edit_staging_and_run_id_bound_publish_receipt_blocks_concurrent_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6 #1 + Grok low:
    1) Two concurrent editors competing for the same output version use distinct run_id-bound
       staging files; the winner writes a publish receipt with run_id/path/sha256 while the loser
       is rejected without clobbering the file or receipt;
    2) Real read-only disk probe rejects the foreign run's file for Run B (`run_matched=False`),
       so Run B never adopts Run A's candidate or rewrites report references;
    3) Grok low: on a run-matched readable candidate, `recalc` is recorded ONLY when
       `recalc_status == 'recalc_failed'` or omitted (not when `changed=False` with `recalc_status='ok'`)."""
    import shutil
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )

    ws = tmp_path / "ws_r6_1"
    r_rel = "research/2026-11-27-deep-FY26Q3"
    (ws / r_rel / "model_dump").mkdir(parents=True, exist_ok=True)
    scripts_src = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "scripts"
    dst_scripts = ws / ".agents" / "skills" / "dalton-model" / "scripts"
    dst_scripts.mkdir(parents=True, exist_ok=True)
    for py_f in scripts_src.glob("*.py"):
        shutil.copy2(py_f, dst_scripts / py_f.name)

    v2 = ws / "model" / "PDD-model-v02.xlsx"
    v3 = ws / "model" / "PDD-model-v03.xlsx"
    v2_sha = _write_simple_valid_model(v2)
    fp_v2 = mfp.build_fingerprint(v2, ticker="PDD")

    # 1. Two editors race for the same v03.xlsx: Run B enters first, Run A publishes v03 during Run B's edit!
    seen_staging_names: list[str] = []
    orig_restore = medit.model_xlsx_hooks.restore_donor_cache
    race_triggered = {"done": False}
    winner_res_holder: list[dict[str, Any]] = []

    def _race_restore(editing_path: Path, *args: Any, **kwargs: Any) -> Any:
        seen_staging_names.append(Path(editing_path).name)
        res = orig_restore(editing_path, *args, **kwargs)
        if not race_triggered["done"] and "run-r6-B" in Path(editing_path).name:
            race_triggered["done"] = True
            # Run A concurrently wins publishing v03.xlsx with its own isolated staging file & run_id
            win = medit.apply_edit_plan(
                baseline_path=v2,
                ticker="PDD",
                plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 9999}]},
                output_path=v3,
                workspace_dir=ws,
                fingerprint=fp_v2,
                skip_recalc=True,
                run_id="run-r6-A",
            )
            winner_res_holder.append(win)
        return res

    monkeypatch.setattr(
        medit.model_xlsx_hooks,
        "restore_donor_cache",
        create_autospec(orig_restore, side_effect=_race_restore),
    )

    with pytest.raises(ValueError, match="existing model version"):
        medit.apply_edit_plan(
            baseline_path=v2,
            ticker="PDD",
            plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1111}]},
            output_path=v3,
            workspace_dir=ws,
            fingerprint=fp_v2,
            skip_recalc=True,
            run_id="run-r6-B",
        )

    assert len(seen_staging_names) == 2
    assert seen_staging_names[0] != seen_staging_names[1]
    assert any("run-r6-B" in n for n in seen_staging_names)
    assert any("run-r6-A" in n for n in seen_staging_names)
    assert len(winner_res_holder) == 1
    v3_sha_a = winner_res_holder[0]["candidate_sha256"]
    assert mfp.file_sha256(v3) == v3_sha_a

    rcpt_p = medit.publish_receipt_path(v3)
    assert rcpt_p.is_file()
    rcpt = json.loads(rcpt_p.read_text(encoding="utf-8"))
    assert rcpt["run_id"] == "run-r6-A"
    assert rcpt["candidate_path"] == "model/PDD-model-v03.xlsx"
    assert rcpt["candidate_sha256"] == v3_sha_a

    # 2. Execute the REAL read-only bash probe from modeling_stages.js for Run B vs Run A
    stages_src = (REPO_ROOT / "workflows" / "_shared" / "modeling_stages.js").read_text(encoding="utf-8")
    rev_harness_js = f"{stages_src}\nreturn await reviseModelForReport(args)\n"

    def _run_real_probe(prompt: str) -> dict[str, Any]:
        m = re.search(r"```bash\n(.*?)\n```", prompt, re.DOTALL)
        assert m, prompt
        cmd = m.group(1).replace("python3 ", f"{sys.executable} ")
        proc = subprocess.run(["bash", "-c", cmd], cwd=ws, capture_output=True, text=True, check=False)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        return json.loads(proc.stdout.strip())

    async def _run_rev(run_id_val: str, edit_reply: Any) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
        lbls: list[str] = []
        probes: list[dict[str, Any]] = []
        spec_host = create_autospec(WorkflowHost, instance=True)

        async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
            lbl = str(opts.get("label") or "")
            lbls.append(lbl)
            if lbl == "计算联动新路径":
                return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
            if lbl == "模型联动修订":
                assert f'--progress-path "{r_rel}/.progress.json"' in prompt
                return edit_reply
            if lbl == "核验联动候选落盘":
                out = _run_real_probe(prompt)
                probes.append(out)
                return out
            if lbl == "写入降级账本":
                fb = mchk.write_fallback_ledger(
                    workspace_dir=ws,
                    ticker="PDD",
                    research_dir=ws / r_rel,
                    candidate_path=v2,
                    baseline_path=v2,
                    kind="report_revision",
                    failed_stages=["report_revision"],
                    run_id=run_id_val,
                    workflow="earnings-deep-update",
                    mode="update",
                )
                return {
                    "files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                    "summary": "fb",
                    "final_status": fb["final_status"],
                    "model_sha256": fb["model_sha256"],
                    "run_id": run_id_val,
                }
            if lbl == "模型联动差异刷新":
                return {"files": [f"{r_rel}/model_dump/consensus-diff.md", f"{r_rel}/model_dump/consensus-diff.json"], "summary": "diff", "consensus_diff_generated": True}
            if lbl == "核验差异表落盘":
                return {"path": f"{r_rel}/model_dump/consensus-diff.json", "json_ok": True, "md_ok": True, "matched": True}
            if lbl == "模型联动报告同步":
                return {"files": ["drafts/2026-11-27-deep-FY26Q3.md"], "summary": "sync", "report_sha256": "b" * 64}
            if lbl == "核验报告同步落盘":
                return {"path": "drafts/2026-11-27-deep-FY26Q3.md", "exists": True, "synced": True, "report_sha256": "c" * 64}
            if lbl == "模型联动修订检查":
                has_recalc = "recalc" in prompt
                return {
                    "files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                    "summary": "chk",
                    "readable": True,
                    "final_status": "delivered_with_open_issues" if has_recalc else "pass",
                    "model_sha256": v3_sha_a,
                    "actionable_count": 0,
                    "actionable_issues": [],
                    "missing_required_sheets": [],
                    "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md" if has_recalc else None,
                }
            raise AssertionError(lbl)

        spec_host.agent.side_effect = _agent
        spec_host.phase.side_effect = lambda _t: None
        spec_host.log.side_effect = lambda _m: None
        out = await run_workflow_script(
            rev_harness_js,
            {
                "workspaceDir": ".",
                "ticker": "PDD",
                "researchDir": r_rel,
                "progressPath": f"{r_rel}/.progress.json",
                "workflow": "earnings-deep-update",
                "mode": "update",
                "currentModelPath": "model/PDD-model-v02.xlsx",
                "currentModelSha256": v2_sha,
                "currentStatus": "pass",
                "currentArtifacts": [],
                "consensusPath": "data/consensus/snap.json",
                "priorFailedStages": [],
                "runId": run_id_val,
                "reportPath": "drafts/2026-11-27-deep-FY26Q3.md",
                "reviewFeedback": {"verdict": "revise", "must_fix": ["update"]},
                "phaseLabel": "审稿",
            },
            spec_host,
            WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0),
        )
        assert out.status == "completed", out.error
        return out.result, lbls, probes

    # Run B failed to write v03 (or timed out) while Run A published v03 -> Run B MUST NOT adopt v03!
    res_b, lbls_b, probes_b = await _run_rev("run-r6-B", None)
    assert probes_b[0]["exists"] is True and probes_b[0]["readable"] is True
    assert probes_b[0]["run_matched"] is False and probes_b[0]["receipt_run_id"] == "run-r6-A"
    assert res_b["changed"] is False
    assert res_b["modelPath"] == "model/PDD-model-v02.xlsx"
    assert "模型联动报告同步" not in lbls_b
    assert "写入降级账本" in lbls_b

    # Run A's own run_id matches receipt -> adopts v03; with changed=False & recalc_status="ok" (Grok low), recalc is NOT recorded!
    res_a_ok, _, probes_a = await _run_rev(
        "run-r6-A",
        {"files": ["model/PDD-model-v03.xlsx"], "summary": "ok", "changed": False, "model_path": "model/PDD-model-v03.xlsx", "model_sha256": v3_sha_a, "recalc_status": "ok"},
    )
    assert probes_a[0]["run_matched"] is True
    assert res_a_ok["changed"] is True and res_a_ok["modelPath"] == "model/PDD-model-v03.xlsx"
    assert "recalc" not in res_a_ok["failedStages"]
    assert res_a_ok["status"] == "pass"

    # Run A with recalc_status="recalc_failed" -> recalc IS recorded!
    res_a_fail, _, _ = await _run_rev(
        "run-r6-A",
        {"files": ["model/PDD-model-v03.xlsx"], "summary": "rf", "changed": True, "model_path": "model/PDD-model-v03.xlsx", "model_sha256": v3_sha_a, "recalc_status": "recalc_failed"},
    )
    assert "recalc" in res_a_fail["failedStages"]

    # Also verify --progress-path reads run_id from .progress.json into publish receipt
    prog_p = ws / r_rel / ".progress.json"
    prog_p.write_text(json.dumps({"run_id": "run-r6-prog"}), encoding="utf-8")
    v4 = ws / "model" / "PDD-model-v04.xlsx"
    res_prog = medit.apply_edit_plan(
        baseline_path=v2,
        ticker="PDD",
        plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 8888}]},
        output_path=v4,
        workspace_dir=ws,
        fingerprint=fp_v2,
        skip_recalc=True,
        progress_path=f"{r_rel}/.progress.json",
    )
    assert res_prog["run_id"] == "run-r6-prog"
    rcpt4 = json.loads(medit.publish_receipt_path(v4).read_text(encoding="utf-8"))
    assert rcpt4["run_id"] == "run-r6-prog" and rcpt4["candidate_sha256"] == res_prog["candidate_sha256"]


def test_p0b_r6_2_output_dir_rejects_symlink_model_dir_and_missing_workspace_dir(
    tmp_path: Path,
) -> None:
    """R6 #2:
    1) Workspace `model/` being a symlink (to external OR internal dir) is rejected by
       both `apply_edit_plan` and `next_model_output_path`;
    2) Omitting `workspace_dir` / `--workspace-dir` raises ValueError (and CLI non-zero exit)
       instead of walking ancestors for any directory named `model`."""
    # 1a. Workspace model/ is a symlink pointing outside workspace
    outside_dir = tmp_path / "outside_model_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    ws_ext = tmp_path / "ws_ext_sym"
    (ws_ext / "sources").mkdir(parents=True, exist_ok=True)
    (ws_ext / "model").symlink_to(outside_dir)
    base_src = ws_ext / "sources" / "upload.xlsx"
    _write_simple_valid_model(base_src)
    fp_src = mfp.build_fingerprint(base_src, ticker="PDD")
    plan = {"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1234}]}

    with pytest.raises(ValueError, match="symlink workspace model/"):
        medit.next_model_output_path(ws_ext, "PDD", baseline_path=base_src)

    with pytest.raises(ValueError, match="symlink workspace model/"):
        medit.apply_edit_plan(
            baseline_path=base_src,
            ticker="PDD",
            plan=plan,
            output_path=ws_ext / "model" / "PDD-model-v02.xlsx",
            workspace_dir=ws_ext,
            fingerprint=fp_src,
            skip_recalc=True,
        )
    assert not (outside_dir / "PDD-model-v02.xlsx").exists()

    # 1b. Workspace model/ is a symlink pointing to another directory inside workspace
    ws_int = tmp_path / "ws_int_sym"
    (ws_int / "drafts").mkdir(parents=True, exist_ok=True)
    (ws_int / "model").symlink_to(ws_int / "drafts")
    with pytest.raises(ValueError, match="symlink workspace model/"):
        medit.apply_edit_plan(
            baseline_path=base_src,
            ticker="PDD",
            plan=plan,
            output_path=ws_int / "model" / "PDD-model-v02.xlsx",
            workspace_dir=ws_int,
            fingerprint=fp_src,
            skip_recalc=True,
        )

    # 2. Omitting workspace_dir when an arbitrary ancestor is named `model` -> rejected in API and CLI!
    tricky_root = tmp_path / "model" / "external_project"
    tricky_base = tricky_root / "sources" / "upload.xlsx"
    _write_simple_valid_model(tricky_base)
    tricky_out = tricky_root / "sources" / "PDD-model-v02.xlsx"
    with pytest.raises(ValueError, match="--workspace-dir"):
        medit.apply_edit_plan(
            baseline_path=tricky_base,
            ticker="PDD",
            plan=plan,
            output_path=tricky_out,
            workspace_dir=None,
            skip_recalc=True,
        )
    assert not tricky_out.exists()

    plan_p = tmp_path / "plan_no_ws.json"
    plan_p.write_text(json.dumps(plan), encoding="utf-8")
    rc = medit.main(
        [
            "apply",
            "--baseline",
            str(tricky_base),
            "--ticker",
            "PDD",
            "--plan",
            str(plan_p),
            "--out",
            str(tricky_out),
            "--skip-recalc",
        ]
    )
    assert rc != 0
    assert not tricky_out.exists()


def test_p0b_r6_3_in_place_requires_same_file_and_verified_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6 #3: `--in-place` only writes back to the EXACT same workbook as `--baseline`
    with verified `--expected-baseline-sha256`, refusing to overwrite another version
    in `model/` or an unverified/mutated target."""
    ws = tmp_path / "ws_r6_3"
    v1 = ws / "model" / "PDD-model-v01.xlsx"
    v2 = ws / "model" / "PDD-model-v02.xlsx"
    v1_sha = _write_simple_valid_model(v1)
    v2_sha = _write_simple_valid_model(v2)
    fp_v2 = mfp.build_fingerprint(v2, ticker="PDD")
    plan = {"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 4321}]}

    # 1. --in-place with --out pointing to a DIFFERENT historical file (v01) -> rejected, v01 untouched!
    with pytest.raises(ValueError, match="output path differs from baseline"):
        medit.apply_edit_plan(
            baseline_path=v2,
            ticker="PDD",
            plan=plan,
            output_path=v1,
            workspace_dir=ws,
            fingerprint=fp_v2,
            expected_baseline_sha256=v2_sha,
            in_place=True,
            skip_recalc=True,
        )
    assert mfp.file_sha256(v1) == v1_sha

    # 2. --in-place without expected_baseline_sha256 -> rejected!
    with pytest.raises(ValueError, match="--expected-baseline-sha256"):
        medit.apply_edit_plan(
            baseline_path=v2,
            ticker="PDD",
            plan=plan,
            output_path=v2,
            workspace_dir=ws,
            fingerprint=fp_v2,
            in_place=True,
            skip_recalc=True,
        )
    assert mfp.file_sha256(v2) == v2_sha

    # 3. --in-place where target mutates concurrently right before publish -> rejected!
    orig_restore = medit.model_xlsx_hooks.restore_donor_cache
    mutated_sha_holder: list[str] = []

    def _mutate_target(*args: Any, **kwargs: Any) -> Any:
        res = orig_restore(*args, **kwargs)
        wb = openpyxl.load_workbook(v2)
        wb["Model - PDD"]["E2"] = 7777
        wb.save(v2)
        wb.close()
        mutated_sha_holder.append(mfp.file_sha256(v2))
        return res

    monkeypatch.setattr(
        medit.model_xlsx_hooks,
        "restore_donor_cache",
        create_autospec(orig_restore, side_effect=_mutate_target),
    )
    with pytest.raises(ValueError, match="target sha256 changed before publish"):
        medit.apply_edit_plan(
            baseline_path=v2,
            ticker="PDD",
            plan=plan,
            output_path=v2,
            workspace_dir=ws,
            fingerprint=fp_v2,
            expected_baseline_sha256=v2_sha,
            in_place=True,
            skip_recalc=True,
        )
    assert mutated_sha_holder and mfp.file_sha256(v2) == mutated_sha_holder[0]

    # 4. Valid --in-place on same file with matching live hash -> succeeds and updates publish receipt!
    monkeypatch.setattr(medit.model_xlsx_hooks, "restore_donor_cache", orig_restore)
    live_v2_sha = _write_simple_valid_model(v2)
    live_fp_v2 = mfp.build_fingerprint(v2, ticker="PDD")
    res_ok = medit.apply_edit_plan(
        baseline_path=v2,
        ticker="PDD",
        plan=plan,
        output_path=v2,
        workspace_dir=ws,
        fingerprint=live_fp_v2,
        expected_baseline_sha256=live_v2_sha,
        in_place=True,
        skip_recalc=True,
        run_id="run-r6-inplace",
    )
    assert res_ok["status"] == "ok"
    rcpt = json.loads(medit.publish_receipt_path(v2).read_text(encoding="utf-8"))
    assert rcpt["run_id"] == "run-r6-inplace"
    assert rcpt["candidate_sha256"] == res_ok["candidate_sha256"]


@pytest.mark.asyncio
async def test_p0b_r6_4_probe_timeout_retries_once_and_degrades_without_aborting_report_revision(
    tmp_path: Path,
) -> None:
    """R6 #4:
    1) Probe subagent timeout/empty return retries ONCE; if retry succeeds, workflow continues normally;
    2) If retry ALSO fails, treated as unverified (NOT missing/unreadable) -> delegates to fallback
       ledger command which reads current model from disk so report revision does NOT abort early."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )

    stages_src = (REPO_ROOT / "workflows" / "_shared" / "modeling_stages.js").read_text(encoding="utf-8")
    rev_harness_js = f"{stages_src}\nreturn await reviseModelForReport(args)\n"

    ws = tmp_path / "ws_r6_4"
    r_rel = "research/2026-11-27-deep-FY26Q3"
    (ws / r_rel / "model_dump").mkdir(parents=True, exist_ok=True)
    v2 = ws / "model" / "PDD-model-v02.xlsx"
    v2_sha = _write_simple_valid_model(v2)

    rev_args = {
        "workspaceDir": ".",
        "ticker": "PDD",
        "researchDir": r_rel,
        "progressPath": f"{r_rel}/.progress.json",
        "workflow": "earnings-deep-update",
        "mode": "update",
        "currentModelPath": "model/PDD-model-v02.xlsx",
        "currentModelSha256": v2_sha,
        "currentStatus": "pass",
        "currentArtifacts": [],
        "consensusPath": "data/consensus/snap.json",
        "priorFailedStages": [],
        "runId": "run-r6-4",
        "reportPath": "drafts/2026-11-27-deep-FY26Q3.md",
        "reviewFeedback": {"verdict": "revise", "must_fix": ["text only"]},
        "phaseLabel": "审稿",
    }

    # Case A: curProbe times out once then succeeds on retry -> returns unchanged ok=True!
    cur_probe_calls = {"n": 0}
    spec_host_a = create_autospec(WorkflowHost, instance=True)

    async def _agent_retry_ok(prompt: str, opts: dict[str, Any]) -> Any:
        lbl = str(opts.get("label") or "")
        if lbl == "计算联动新路径":
            return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
        if lbl == "模型联动修订":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "no change", "changed": False, "model_path": "model/PDD-model-v02.xlsx", "model_sha256": v2_sha}
        if lbl == "核验联动候选落盘":
            return {"path": "model/PDD-model-v03.xlsx", "exists": False, "readable": False, "model_sha256": None, "run_matched": False}
        if lbl == "核验当前模型落盘":
            cur_probe_calls["n"] += 1
            if cur_probe_calls["n"] == 1:
                return None  # First attempt times out!
            return {"path": "model/PDD-model-v02.xlsx", "exists": True, "readable": True, "model_sha256": v2_sha}
        raise AssertionError(lbl)

    spec_host_a.agent.side_effect = _agent_retry_ok
    spec_host_a.phase.side_effect = lambda _t: None
    spec_host_a.log.side_effect = lambda _m: None
    out_a = await run_workflow_script(rev_harness_js, rev_args, spec_host_a, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
    assert out_a.status == "completed", out_a.error
    assert cur_probe_calls["n"] == 2
    assert out_a.result["ok"] is True and out_a.result["changed"] is False
    assert out_a.result["status"] == "pass" and out_a.result["modelSha256"] == v2_sha

    # Case B: End-to-end earnings-deep-update where changed=false AND curProbe times out TWICE ->
    # retries once, falls back to write-ledger reading v02 on disk, and CONTINUES to 修订 1 & 复审 1!
    deep_script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "earnings-deep-update" / "workflow.js")
    deep_params = {
        "ticker": "PDD",
        "company": "PDD Holdings",
        "quarter": "FY26Q3",
        "workspace_dir": ".",
        "release_date": "2026-11-27",
        "model_path": "model/PDD-model-v02.xlsx",
        "preview_predictions": None,
        "progress_path": f"{r_rel}/.progress.json",
    }
    nine_items = [{"check": i, "result": "pass", "reason": "ok", "lines": "L1"} for i in range(1, 10)]
    seen_labels: list[str] = []
    spec_host_b = create_autospec(WorkflowHost, instance=True)

    async def _deep_cur_probe_timeout(prompt: str, opts: dict[str, Any]) -> Any:
        lbl = str(opts.get("label") or "")
        seen_labels.append(lbl)
        if lbl == "八节点评":
            return {"files": ["drafts/2026-11-27-deep-FY26Q3.md"], "summary": "draft", "sha256": "1" * 64}
        if lbl == "产物核验":
            paths = [ln[2:].strip() for ln in prompt.splitlines() if ln.startswith("- ") and "|" not in ln]
            return {"files": [f"{p}|{'e' * 64}" for p in paths]}
        if lbl == "建模准备":
            return {"files": [f"{r_rel}/mapping.md"], "summary": "prep", "mode": "update", "baseline_path": "model/PDD-model-v01.xlsx", "planned_output_path": "model/PDD-model-v02.xlsx"}
        if lbl == "读取运行ID":
            return {"run_id": "run-r6-4-deep"}
        if lbl == "模型构建与落盘":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "v02", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": v2_sha, "recalc_status": "ok"}
        if lbl == "核验候选落盘":
            return {"path": "model/PDD-model-v02.xlsx", "exists": True, "readable": True, "model_sha256": v2_sha, "run_matched": True}
        if lbl == "一致预期对照":
            return {"files": ["model/PDD-model-v02.xlsx", f"{r_rel}/model_dump/consensus-diff.md", f"{r_rel}/model_dump/consensus-diff.json"], "summary": "cons", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": v2_sha, "consensus_diff_generated": True}
        if lbl == "模型本轮范围检查":
            return {"files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json"], "summary": "pass", "readable": True, "final_status": "pass", "model_sha256": v2_sha, "actionable_count": 0, "missing_required_sheets": []}
        if lbl == "模型变动拆解":
            return {"files": [f"{r_rel}/model-delta.md"], "summary": "delta"}
        if lbl == "校准与对账":
            return {"files": [f"{r_rel}/reconcile.md"], "summary": "cal", "sha256": "2" * 64}
        if lbl == "语言审查 1":
            return {"files": ["drafts/2026-11-27-deep-FY26Q3.md"], "summary": "lang1", "sha256": "3" * 64}
        if lbl == "审稿":
            return {"verdict": "revise", "summary": "polish text", "must_fix": ["clarify wording"], "items": nine_items}
        if lbl == "计算联动新路径":
            return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
        if lbl == "模型联动修订":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "no model change", "changed": False, "model_path": "model/PDD-model-v02.xlsx", "model_sha256": v2_sha}
        if lbl == "核验联动候选落盘":
            return {"path": "model/PDD-model-v03.xlsx", "exists": False, "readable": False, "model_sha256": None, "run_matched": False}
        if lbl == "核验当前模型落盘":
            return None  # Both initial and retry time out!
        if lbl == "写入降级账本":
            fb = mchk.write_fallback_ledger(
                workspace_dir=ws,
                ticker="PDD",
                research_dir=ws / r_rel,
                candidate_path=v2,
                baseline_path=v2,
                kind="report_revision",
                failed_stages=["report_revision"],
                run_id="run-r6-4-deep",
                workflow="earnings-deep-update",
                mode="update",
            )
            return {
                "files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "fallback read v02 from disk",
                "final_status": fb["final_status"],
                "model_sha256": fb["model_sha256"],
                "run_id": "run-r6-4-deep",
            }
        if lbl == "修订 1":
            return {"files": ["drafts/2026-11-27-deep-FY26Q3.md"], "summary": "revised", "sha256": "4" * 64}
        if lbl == "复审 1":
            return {"verdict": "pass", "summary": "ok", "must_fix": [], "items": nine_items}
        if lbl == "笔记语言审查":
            return {"files": [f"{r_rel}/reconcile.md"], "summary": "notes ok"}
        if lbl == "语言审查 2":
            return {"files": ["drafts/2026-11-27-deep-FY26Q3.md"], "summary": "lang2", "sha256": "4" * 64}
        if lbl == "知识层修订草案":
            return {"files": [f"{r_rel}/knowledge-revision.json"], "summary": "kr"}
        return {"files": [], "summary": "ok"}

    spec_host_b.agent.side_effect = _deep_cur_probe_timeout
    spec_host_b.phase.side_effect = lambda _t: None
    spec_host_b.log.side_effect = lambda _m: None
    out_b = await run_workflow_script(deep_script, deep_params, spec_host_b, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
    assert out_b.status == "completed", out_b.error
    assert seen_labels.count("核验当前模型落盘") == 2
    assert "写入降级账本" in seen_labels
    assert "修订 1" in seen_labels and "复审 1" in seen_labels
    assert out_b.result["ok"] is True
    assert out_b.result["model_status"] == "delivered_with_open_issues"
    assert out_b.result["model_file"] == "model/PDD-model-v02.xlsx"


@pytest.mark.asyncio
async def test_p0b_r6_5_report_sync_probe_ignores_html_comments_and_rejects_suffix_paths(
    tmp_path: Path,
) -> None:
    """R6 #5: Executing the REAL read-only python command from `核验报告同步落盘`
    ignores HTML comments and enforces full-path boundaries (rejecting suffix matches
    like `archive/model/PDD-model-v03.xlsx` or `../model/PDD-model-v03.xlsx`)."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )

    stages_src = (REPO_ROOT / "workflows" / "_shared" / "modeling_stages.js").read_text(encoding="utf-8")
    rev_harness_js = f"{stages_src}\nreturn await reviseModelForReport(args)\n"

    ws = tmp_path / "ws_r6_5"
    r_rel = "research/2026-11-27-fc"
    (ws / r_rel / "model_dump").mkdir(parents=True, exist_ok=True)
    (ws / "drafts").mkdir(parents=True, exist_ok=True)
    v2 = ws / "model" / "PDD-model-v02.xlsx"
    v3 = ws / "model" / "PDD-model-v03.xlsx"
    v2_sha = _write_simple_valid_model(v2)
    v3_sha = _write_simple_valid_model(v3)
    memo_p = ws / "drafts" / "2026-11-27-memo.md"
    meta_p = ws / r_rel / ".memo-model.json"
    delta_p = ws / r_rel / "model-delta.md"

    def _run_real_probe(prompt: str) -> dict[str, Any]:
        m = re.search(r"```bash\n(.*?)\n```", prompt, re.DOTALL)
        assert m, prompt
        proc = subprocess.run(["bash", "-c", m.group(1)], cwd=ws, capture_output=True, text=True, check=False)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        return json.loads(proc.stdout.strip())

    async def _run_sync(body_text: str, delta_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
        memo_p.write_text(body_text, encoding="utf-8")
        meta_p.write_text(json.dumps({"path": "model/PDD-model-v03.xlsx", "sha256": v3_sha}), encoding="utf-8")
        delta_p.write_text(delta_text, encoding="utf-8")
        probes: list[dict[str, Any]] = []
        spec_host = create_autospec(WorkflowHost, instance=True)

        async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
            lbl = str(opts.get("label") or "")
            if lbl == "计算联动新路径":
                return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
            if lbl == "模型联动修订":
                return {"files": ["model/PDD-model-v03.xlsx"], "summary": "v03", "changed": True, "model_path": "model/PDD-model-v03.xlsx", "model_sha256": v3_sha, "recalc_status": "ok"}
            if lbl == "核验联动候选落盘":
                return {"path": "model/PDD-model-v03.xlsx", "exists": True, "readable": True, "model_sha256": v3_sha, "run_matched": True}
            if lbl == "模型联动差异刷新":
                return {"files": [f"{r_rel}/model_dump/consensus-diff.md", f"{r_rel}/model_dump/consensus-diff.json"], "summary": "diff", "consensus_diff_generated": True}
            if lbl == "核验差异表落盘":
                return {"path": f"{r_rel}/model_dump/consensus-diff.json", "json_ok": True, "md_ok": True, "matched": True}
            if lbl == "模型联动报告同步":
                return {"files": ["drafts/2026-11-27-memo.md"], "summary": "sync", "report_sha256": "0" * 64}
            if lbl == "核验报告同步落盘":
                res = _run_real_probe(prompt)
                probes.append(res)
                return res
            if lbl == "模型联动修订检查":
                has_rs = "report_sync" in prompt
                return {
                    "files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                    "summary": "chk",
                    "readable": True,
                    "final_status": "delivered_with_open_issues" if has_rs else "pass",
                    "model_sha256": v3_sha,
                    "actionable_count": 0,
                    "actionable_issues": [],
                    "missing_required_sheets": [],
                    "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md" if has_rs else None,
                }
            raise AssertionError(lbl)

        spec_host.agent.side_effect = _agent
        spec_host.phase.side_effect = lambda _t: None
        spec_host.log.side_effect = lambda _m: None
        out = await run_workflow_script(
            rev_harness_js,
            {
                "workspaceDir": ".",
                "ticker": "PDD",
                "researchDir": r_rel,
                "progressPath": f"{r_rel}/.progress.json",
                "workflow": "full-coverage",
                "mode": "update",
                "currentModelPath": "model/PDD-model-v02.xlsx",
                "currentModelSha256": v2_sha,
                "currentStatus": "pass",
                "currentArtifacts": [],
                "consensusPath": "data/consensus/snap.json",
                "priorFailedStages": [],
                "runId": "run-r6-5",
                "reportPath": "drafts/2026-11-27-memo.md",
                "extraSyncPaths": [f"{r_rel}/.memo-model.json", f"{r_rel}/model-delta.md"],
                "reviewFeedback": {"verdict": "revise", "must_fix": ["sync"]},
                "phaseLabel": "修订",
            },
            spec_host,
            WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0),
        )
        assert out.status == "completed", out.error
        return out.result, probes[0]

    # 1. New path appears ONLY inside an HTML comment -> MUST be synced=False!
    res_cmt, probe_cmt = await _run_sync(
        body_text="# 备忘录\n正文仍引用旧模型 model/PDD-model-v02.xlsx。\n<!-- 待同步 model/PDD-model-v03.xlsx 与 model:model/PDD-model-v03.xlsx!Valuation!C12 -->\n",
        delta_text="新模型为 model/PDD-model-v03.xlsx。\n",
    )
    assert probe_cmt["synced"] is False, probe_cmt
    assert "report_sync" in res_cmt["failedStages"]

    # 2. New path appears ONLY as suffix of another path (`archive/model/...`) -> MUST be synced=False!
    res_suf, probe_suf = await _run_sync(
        body_text="# 备忘录\n归档参考 archive/model/PDD-model-v03.xlsx 与 ../model/PDD-model-v03.xlsx，正文仍用旧模型。\n",
        delta_text="新模型为 model/PDD-model-v03.xlsx。\n",
    )
    assert probe_suf["synced"] is False, probe_suf
    assert "report_sync" in res_suf["failedStages"]

    # 3. Extra markdown note only mentions suffix path -> MUST be synced=False!
    res_esuf, probe_esuf = await _run_sync(
        body_text="# 备忘录\n估值已切换至 ./model/PDD-model-v03.xlsx。\n",
        delta_text="历史归档在 archive/model/PDD-model-v03.xlsx。\n",
    )
    assert probe_esuf["synced"] is False, probe_esuf
    assert "report_sync" in res_esuf["failedStages"]

    # 4. Exact visible path (with leading `./` and alongside an HTML comment) -> synced=True!
    res_ok, probe_ok = await _run_sync(
        body_text="# 备忘录\n<!-- note -->\n估值已切换至 `./model/PDD-model-v03.xlsx`。\n",
        delta_text="新模型为 model/PDD-model-v03.xlsx。\n",
    )
    assert probe_ok["synced"] is True, probe_ok
    assert "report_sync" not in res_ok["failedStages"]


def test_p0b_r6_7_cos_verbatim_urlsplit_valueerror_and_entity_casing_roundtrip() -> None:
    """R6 #7:
    1) Malformed URL-like attachment names that make `urlsplit` raise `ValueError` fall back
       to plain-filename handling without crashing message preprocessing;
    2) Escaped closing-tag entities (`&LT;...&GT;`, `&AMP;Lt;...&Gt;`) preserve original
       entity casing across `_escape_closing_tag` / `_unescape_closing_tag` round-trips."""
    from types import SimpleNamespace
    from src.dalton.cos_verbatim import (
        _basename_only,
        _escape_closing_tag,
        _unescape_closing_tag,
        capture_turn_verbatim_request,
    )

    # 1. URL-like invalid strings triggering urlsplit ValueError -> handled as plain filenames!
    assert _basename_only("https://bad[host/Q3#final.xlsx") == "Q3#final.xlsx"
    assert _basename_only("http://[::1/path/report?v=2.pdf") == "report?v=2.pdf"

    req = SimpleNamespace(
        hitl_response=None,
        report_source=None,
        wakeup=None,
        market_target=None,
        origin=None,
        additional_context=[
            SimpleNamespace(type="file", name="https://bad[host/Q3#final.xlsx"),
            SimpleNamespace(type="file", name="http://[::1/path/report?v=2.pdf"),
        ],
    )
    cap = capture_turn_verbatim_request(
        request=req,
        raw_messages=[{"role": "user", "content": "处理附件 &LT;/user_verbatim_request&GT;"}],
    )
    assert cap is not None
    assert cap["attachments"] == ["Q3#final.xlsx", "report?v=2.pdf"]

    # 2. Escaped entity casing is strictly preserved across round-trip!
    samples = [
        "&LT;/user_verbatim_request&GT;",
        "&Lt;/User_Verbatim_Request&Gt;",
        "&AMP;LT;/USER_VERBATIM_REQUEST&GT;",
        "&amp;AMP;Lt;/  User_Verbatim_Request &Gt;",
    ]
    for s in samples:
        escaped = _escape_closing_tag(s)
        assert not re.search(r"</\s*user_verbatim_request\s*>", escaped, re.IGNORECASE), escaped
        assert _unescape_closing_tag(escaped) == s, (s, escaped, _unescape_closing_tag(escaped))
    assert _escape_closing_tag("&LT;/user_verbatim_request&GT;") == "&amp;LT;/user_verbatim_request&GT;"
    assert _escape_closing_tag("&AMP;Lt;/User_Verbatim_Request&Gt;") == "&amp;AMP;Lt;/User_Verbatim_Request&Gt;"


# ===========================================================================
# P0-b Round 7 fixes (Items 1-2)
# ===========================================================================


@pytest.mark.asyncio
async def test_p0b_r7_1_candidate_requires_current_run_receipt_and_run_id_retry(
    tmp_path: Path,
) -> None:
    """R7 #1:
    1) Initial modeling (`模型构建与落盘`) ALWAYS verifies the publish receipt (path, sha256,
       run_id) via real disk probe before adopting `candidatePath`: when Run A published v02
       and Run B's subtask returns success JSON pointing to v02, Run B rejects it (`run_matched=False`);
    2) Targeted fix round (`模型修改 1`) verifies the publish receipt before updating `candidatePath`:
       returning a foreign run's candidate is rejected and keeps the previous verified candidate;
    3) Reading `run_id` retries once on failure; when `run_id` remains empty, both initial modeling
       and report revision treat the candidate as unverified (never `run_matched=True`) and refuse
       to adopt it."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )

    ws = tmp_path / "ws_r7_1"
    r_rel = "research/2026-11-27-model-PDD"
    (ws / r_rel / "model_dump").mkdir(parents=True, exist_ok=True)
    scripts_src = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "scripts"
    dst_scripts = ws / ".agents" / "skills" / "dalton-model" / "scripts"
    dst_scripts.mkdir(parents=True, exist_ok=True)
    for py_f in scripts_src.glob("*.py"):
        shutil.copy2(py_f, dst_scripts / py_f.name)

    v1 = ws / "model" / "PDD-model-v01.xlsx"
    v2 = ws / "model" / "PDD-model-v02.xlsx"
    v3 = ws / "model" / "PDD-model-v03.xlsx"
    v1_sha = _write_simple_valid_model(v1)
    fp_v1 = mfp.build_fingerprint(v1, ticker="PDD")

    # Run A legitimately publishes v02 with run_id="run-r7-A" (writes real .publish-receipt.json)
    res_a = medit.apply_edit_plan(
        baseline_path=v1,
        ticker="PDD",
        plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 2026}]},
        output_path=v2,
        workspace_dir=ws,
        fingerprint=fp_v1,
        skip_recalc=True,
        run_id="run-r7-A",
    )
    v2_sha_a = res_a["candidate_sha256"]

    # Run C legitimately publishes v03 with run_id="run-r7-C"
    res_c = medit.apply_edit_plan(
        baseline_path=v1,
        ticker="PDD",
        plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 3033}]},
        output_path=v3,
        workspace_dir=ws,
        fingerprint=fp_v1,
        skip_recalc=True,
        run_id="run-r7-C",
    )
    v3_sha_c = res_c["candidate_sha256"]

    def _run_real_probe(prompt: str) -> dict[str, Any]:
        m = re.search(r"```bash\n(.*?)\n```", prompt, re.DOTALL)
        assert m, prompt
        cmd = m.group(1).replace("python3 ", f"{sys.executable} ")
        proc = subprocess.run(["bash", "-c", cmd], cwd=ws, capture_output=True, text=True, check=False)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        return json.loads(proc.stdout.strip())

    cm_script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "company-model" / "workflow.js")
    cm_params = {
        "ticker": "PDD",
        "company": "PDD Holdings",
        "workspace_dir": ".",
        "as_of": "2026-11-27",
        "model_path": "model/PDD-model-v01.xlsx",
        "progress_path": f"{r_rel}/.progress.json",
    }

    # --- Case 1: Two runs competing for v02 — Run B's subtask returns SUCCESS JSON for v02,
    #     but the publish receipt on disk belongs to Run A -> MUST NOT adopt! ---
    labels_b: list[str] = []
    probes_b: list[dict[str, Any]] = []
    spec_host_b = create_autospec(WorkflowHost, instance=True)

    async def _agent_run_b(prompt: str, opts: dict[str, Any]) -> Any:
        lbl = str(opts.get("label") or "")
        labels_b.append(lbl)
        if lbl == "建模准备":
            return {
                "files": [f"{r_rel}/model_dump/PDD-fingerprint.json"],
                "summary": "prep",
                "mode": "update",
                "baseline_path": "model/PDD-model-v01.xlsx",
                "baseline_sha256": v1_sha,
                "planned_output_path": "model/PDD-model-v02.xlsx",
            }
        if lbl == "读取运行ID":
            return {"run_id": "run-r7-B"}
        if lbl == "模型构建与落盘":
            # Subtask falsely returns success JSON pointing to Run A's v02 with matching SHA & files!
            return {
                "files": ["model/PDD-model-v02.xlsx", f"{r_rel}/model_dump/coord_map.json"],
                "summary": "claimed v02 ok",
                "candidate_path": "model/PDD-model-v02.xlsx",
                "model_sha256": v2_sha_a,
                "recalc_status": "ok",
            }
        if lbl == "核验候选落盘":
            out = _run_real_probe(prompt)
            probes_b.append(out)
            return out
        raise AssertionError(f"Run B must stop before {lbl!r} after foreign receipt rejection")

    spec_host_b.agent.side_effect = _agent_run_b
    spec_host_b.phase.side_effect = lambda _t: None
    spec_host_b.log.side_effect = lambda _m: None
    out_b = await run_workflow_script(cm_script, cm_params, spec_host_b, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
    assert out_b.status == "completed", out_b.error
    assert len(probes_b) == 1
    assert probes_b[0]["exists"] is True and probes_b[0]["readable"] is True
    assert probes_b[0]["run_matched"] is False and probes_b[0]["receipt_run_id"] == "run-r7-A"
    assert out_b.result["ok"] is False
    assert out_b.result["status"] == "failed_unusable"
    assert out_b.result["candidate_path"] is None
    assert "一致预期对照" not in labels_b and "模型本轮范围检查" not in labels_b

    # --- Case 2: Targeted fix round (`模型修改 1`) returns foreign Run A's v02 ->
    #     rejected by real receipt probe; keeps Run C's verified v03 and writes fallback ledger! ---
    labels_c: list[str] = []
    probes_c: list[dict[str, Any]] = []
    fb_prompts_c: list[str] = []
    spec_host_c = create_autospec(WorkflowHost, instance=True)

    async def _agent_fix_foreign(prompt: str, opts: dict[str, Any]) -> Any:
        lbl = str(opts.get("label") or "")
        labels_c.append(lbl)
        if lbl == "建模准备":
            return {
                "files": [f"{r_rel}/model_dump/PDD-fingerprint.json"],
                "summary": "prep",
                "mode": "update",
                "baseline_path": "model/PDD-model-v01.xlsx",
                "baseline_sha256": v1_sha,
                "planned_output_path": "model/PDD-model-v03.xlsx",
            }
        if lbl == "读取运行ID":
            return {"run_id": "run-r7-C"}
        if lbl == "模型构建与落盘":
            return {
                "files": ["model/PDD-model-v03.xlsx"],
                "summary": "v03 ok",
                "candidate_path": "model/PDD-model-v03.xlsx",
                "model_sha256": v3_sha_c,
                "recalc_status": "ok",
            }
        if lbl == "核验候选落盘":
            out = _run_real_probe(prompt)
            probes_c.append(out)
            return out
        if lbl == "一致预期对照":
            return {
                "files": ["model/PDD-model-v03.xlsx", f"{r_rel}/model_dump/consensus-diff.md", f"{r_rel}/model_dump/consensus-diff.json"],
                "summary": "cons ok",
                "candidate_path": "model/PDD-model-v03.xlsx",
                "model_sha256": v3_sha_c,
                "consensus_diff_generated": True,
            }
        if lbl == "模型本轮范围检查":
            return {
                "files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "1 actionable",
                "readable": True,
                "final_status": "delivered_with_open_issues",
                "model_sha256": v3_sha_c,
                "actionable_count": 1,
                "actionable_issues": ["[C03] forecast constant"],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md",
            }
        if lbl == "模型修改 1":
            # Fix subtask returns Run A's v02 instead of Run C's receipted workbook!
            return {
                "files": ["model/PDD-model-v02.xlsx"],
                "summary": "pointed to foreign v02",
                "candidate_path": "model/PDD-model-v02.xlsx",
                "model_sha256": v2_sha_a,
                "recalc_status": "ok",
                "consensus_diff_generated": True,
            }
        if lbl == "写入降级账本":
            fb_prompts_c.append(prompt)
            return {
                "files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "fallback on v03",
                "final_status": "delivered_with_open_issues",
                "model_sha256": v3_sha_c,
                "run_id": "run-r7-C",
            }
        raise AssertionError(f"unexpected label in fix-foreign case: {lbl!r}")

    spec_host_c.agent.side_effect = _agent_fix_foreign
    spec_host_c.phase.side_effect = lambda _t: None
    spec_host_c.log.side_effect = lambda _m: None
    out_c = await run_workflow_script(cm_script, cm_params, spec_host_c, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
    assert out_c.status == "completed", out_c.error
    assert len(probes_c) == 2
    assert probes_c[0]["run_matched"] is True  # initial v03 matched run-r7-C
    assert probes_c[1]["run_matched"] is False and probes_c[1]["receipt_run_id"] == "run-r7-A"  # fix v02 rejected
    assert out_c.result["candidate_path"] == "model/PDD-model-v03.xlsx"
    assert "fix" in out_c.result["failed_stages"]
    assert fb_prompts_c and '--candidate "model/PDD-model-v03.xlsx"' in fb_prompts_c[0]

    # --- Case 3a: Reading run_id fails once then succeeds on retry -> adopts verified Run A candidate ---
    rid_calls_retry = {"n": 0}
    spec_host_retry = create_autospec(WorkflowHost, instance=True)

    async def _agent_rid_retry_ok(prompt: str, opts: dict[str, Any]) -> Any:
        lbl = str(opts.get("label") or "")
        if lbl == "建模准备":
            return {
                "files": [f"{r_rel}/model_dump/PDD-fingerprint.json"],
                "summary": "prep",
                "mode": "update",
                "baseline_path": "model/PDD-model-v01.xlsx",
                "baseline_sha256": v1_sha,
                "planned_output_path": "model/PDD-model-v02.xlsx",
            }
        if lbl == "读取运行ID":
            rid_calls_retry["n"] += 1
            return None if rid_calls_retry["n"] == 1 else {"run_id": "run-r7-A"}
        if lbl == "模型构建与落盘":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "v02", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": v2_sha_a, "recalc_status": "ok"}
        if lbl == "核验候选落盘":
            return _run_real_probe(prompt)
        if lbl == "一致预期对照":
            return {"files": ["model/PDD-model-v02.xlsx", f"{r_rel}/model_dump/consensus-diff.md", f"{r_rel}/model_dump/consensus-diff.json"], "summary": "cons", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": v2_sha_a, "consensus_diff_generated": True}
        if lbl == "模型本轮范围检查":
            return {"files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json"], "summary": "pass", "readable": True, "final_status": "pass", "model_sha256": v2_sha_a, "actionable_count": 0, "missing_required_sheets": []}
        raise AssertionError(lbl)

    spec_host_retry.agent.side_effect = _agent_rid_retry_ok
    spec_host_retry.phase.side_effect = lambda _t: None
    spec_host_retry.log.side_effect = lambda _m: None
    out_retry = await run_workflow_script(cm_script, cm_params, spec_host_retry, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
    assert out_retry.status == "completed", out_retry.error
    assert rid_calls_retry["n"] == 2
    assert out_retry.result["ok"] is True and out_retry.result["candidate_path"] == "model/PDD-model-v02.xlsx"
    assert out_retry.result["run_id"] == "run-r7-A"

    # --- Case 3b: Reading run_id fails BOTH times -> empty runId treated as unverified, NEVER adopts ---
    rid_calls_empty = {"n": 0}
    labels_empty: list[str] = []
    spec_host_empty = create_autospec(WorkflowHost, instance=True)

    async def _agent_rid_empty(prompt: str, opts: dict[str, Any]) -> Any:
        lbl = str(opts.get("label") or "")
        labels_empty.append(lbl)
        if lbl == "建模准备":
            return {
                "files": [f"{r_rel}/model_dump/PDD-fingerprint.json"],
                "summary": "prep",
                "mode": "update",
                "baseline_path": "model/PDD-model-v01.xlsx",
                "baseline_sha256": v1_sha,
                "planned_output_path": "model/PDD-model-v02.xlsx",
            }
        if lbl == "读取运行ID":
            rid_calls_empty["n"] += 1
            return None if rid_calls_empty["n"] == 1 else {"run_id": None}
        if lbl == "模型构建与落盘":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "v02", "candidate_path": "model/PDD-model-v02.xlsx", "model_sha256": v2_sha_a, "recalc_status": "ok"}
        raise AssertionError(f"Empty run_id must treat candidate as unverified without reaching {lbl!r}")

    spec_host_empty.agent.side_effect = _agent_rid_empty
    spec_host_empty.phase.side_effect = lambda _t: None
    spec_host_empty.log.side_effect = lambda _m: None
    out_empty = await run_workflow_script(cm_script, cm_params, spec_host_empty, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0))
    assert out_empty.status == "completed", out_empty.error
    assert rid_calls_empty["n"] == 2
    assert out_empty.result["ok"] is False and out_empty.result["status"] == "failed_unusable"
    assert out_empty.result["candidate_path"] is None
    assert "一致预期对照" not in labels_empty

    # --- Case 3c: reviseModelForReport with empty runId -> treated as unverified, does NOT adopt v02 ---
    stages_src = (REPO_ROOT / "workflows" / "_shared" / "modeling_stages.js").read_text(encoding="utf-8")
    rev_harness_js = f"{stages_src}\nreturn await reviseModelForReport(args)\n"
    rev_labels: list[str] = []
    spec_host_rev = create_autospec(WorkflowHost, instance=True)

    async def _agent_rev_empty_rid(prompt: str, opts: dict[str, Any]) -> Any:
        lbl = str(opts.get("label") or "")
        rev_labels.append(lbl)
        if lbl == "读取运行ID":
            return None
        if lbl == "计算联动新路径":
            return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v02", "next_path": "model/PDD-model-v02.xlsx"}
        if lbl == "模型联动修订":
            return {"files": ["model/PDD-model-v02.xlsx"], "summary": "v02", "changed": True, "model_path": "model/PDD-model-v02.xlsx", "model_sha256": v2_sha_a, "recalc_status": "ok"}
        if lbl == "写入降级账本":
            return {
                "files": [f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                "summary": "fb on v01",
                "final_status": "delivered_with_open_issues",
                "model_sha256": v1_sha,
                "run_id": None,
            }
        raise AssertionError(f"Empty run_id in reviseModelForReport must not reach {lbl!r}")

    spec_host_rev.agent.side_effect = _agent_rev_empty_rid
    spec_host_rev.phase.side_effect = lambda _t: None
    spec_host_rev.log.side_effect = lambda _m: None
    out_rev = await run_workflow_script(
        rev_harness_js,
        {
            "workspaceDir": ".",
            "ticker": "PDD",
            "researchDir": r_rel,
            "progressPath": f"{r_rel}/.progress.json",
            "workflow": "earnings-deep-update",
            "mode": "update",
            "currentModelPath": "model/PDD-model-v01.xlsx",
            "currentModelSha256": v1_sha,
            "currentStatus": "pass",
            "currentArtifacts": [],
            "consensusPath": "data/consensus/snap.json",
            "priorFailedStages": [],
            "runId": None,
            "reportPath": "drafts/2026-11-27-deep-FY26Q3.md",
            "reviewFeedback": {"verdict": "revise", "must_fix": ["update"]},
            "phaseLabel": "审稿",
        },
        spec_host_rev,
        WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0),
    )
    assert out_rev.status == "completed", out_rev.error
    assert rev_labels.count("读取运行ID") == 2
    assert out_rev.result["changed"] is False
    assert out_rev.result["modelPath"] == "model/PDD-model-v01.xlsx"
    assert "写入降级账本" in rev_labels
    assert "模型联动报告同步" not in rev_labels


@pytest.mark.asyncio
async def test_p0b_r7_2_report_sync_probe_ignores_unclosed_html_comment_to_eof(
    tmp_path: Path,
) -> None:
    """R7 #2: Unclosed `<!--` in report / extra markdown notes is ignored through EOF,
    so a new model path appearing only after an unclosed `<!--` never passes sync."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )

    stages_src = (REPO_ROOT / "workflows" / "_shared" / "modeling_stages.js").read_text(encoding="utf-8")
    rev_harness_js = f"{stages_src}\nreturn await reviseModelForReport(args)\n"

    ws = tmp_path / "ws_r7_2"
    r_rel = "research/2026-11-27-fc"
    (ws / r_rel / "model_dump").mkdir(parents=True, exist_ok=True)
    (ws / "drafts").mkdir(parents=True, exist_ok=True)
    v2 = ws / "model" / "PDD-model-v02.xlsx"
    v3 = ws / "model" / "PDD-model-v03.xlsx"
    v2_sha = _write_simple_valid_model(v2)
    v3_sha = _write_simple_valid_model(v3)
    memo_p = ws / "drafts" / "2026-11-27-memo.md"
    meta_p = ws / r_rel / ".memo-model.json"

    def _run_real_probe(prompt: str) -> dict[str, Any]:
        m = re.search(r"```bash\n(.*?)\n```", prompt, re.DOTALL)
        assert m, prompt
        proc = subprocess.run(["bash", "-c", m.group(1)], cwd=ws, capture_output=True, text=True, check=False)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        return json.loads(proc.stdout.strip())

    async def _run_sync(body_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
        memo_p.write_text(body_text, encoding="utf-8")
        meta_p.write_text(json.dumps({"path": "model/PDD-model-v03.xlsx", "sha256": v3_sha}), encoding="utf-8")
        probes: list[dict[str, Any]] = []
        spec_host = create_autospec(WorkflowHost, instance=True)

        async def _agent(prompt: str, opts: dict[str, Any]) -> Any:
            lbl = str(opts.get("label") or "")
            if lbl == "计算联动新路径":
                return {"files": [f"{r_rel}/model_dump/rev-planned.json"], "summary": "v03", "next_path": "model/PDD-model-v03.xlsx"}
            if lbl == "模型联动修订":
                return {"files": ["model/PDD-model-v03.xlsx"], "summary": "v03", "changed": True, "model_path": "model/PDD-model-v03.xlsx", "model_sha256": v3_sha, "recalc_status": "ok"}
            if lbl == "核验联动候选落盘":
                return {"path": "model/PDD-model-v03.xlsx", "exists": True, "readable": True, "model_sha256": v3_sha, "run_matched": True}
            if lbl == "模型联动差异刷新":
                return {"files": [f"{r_rel}/model_dump/consensus-diff.md", f"{r_rel}/model_dump/consensus-diff.json"], "summary": "diff", "consensus_diff_generated": True}
            if lbl == "核验差异表落盘":
                return {"path": f"{r_rel}/model_dump/consensus-diff.json", "json_ok": True, "md_ok": True, "matched": True}
            if lbl == "模型联动报告同步":
                return {"files": ["drafts/2026-11-27-memo.md"], "summary": "sync", "report_sha256": "0" * 64}
            if lbl == "核验报告同步落盘":
                res = _run_real_probe(prompt)
                probes.append(res)
                return res
            if lbl == "模型联动修订检查":
                has_rs = "report_sync" in prompt
                return {
                    "files": [f"{r_rel}/model_dump/model-check.md", f"{r_rel}/model_dump/model-verification.json", f"{r_rel}/model_dump/remaining-issues.md"],
                    "summary": "chk",
                    "readable": True,
                    "final_status": "delivered_with_open_issues" if has_rs else "pass",
                    "model_sha256": v3_sha,
                    "actionable_count": 0,
                    "actionable_issues": [],
                    "missing_required_sheets": [],
                    "remaining_issues_path": f"{r_rel}/model_dump/remaining-issues.md" if has_rs else None,
                }
            raise AssertionError(lbl)

        spec_host.agent.side_effect = _agent
        spec_host.phase.side_effect = lambda _t: None
        spec_host.log.side_effect = lambda _m: None
        out = await run_workflow_script(
            rev_harness_js,
            {
                "workspaceDir": ".",
                "ticker": "PDD",
                "researchDir": r_rel,
                "progressPath": f"{r_rel}/.progress.json",
                "workflow": "full-coverage",
                "mode": "update",
                "currentModelPath": "model/PDD-model-v02.xlsx",
                "currentModelSha256": v2_sha,
                "currentStatus": "pass",
                "currentArtifacts": [],
                "consensusPath": "data/consensus/snap.json",
                "priorFailedStages": [],
                "runId": "run-r7-2",
                "reportPath": "drafts/2026-11-27-memo.md",
                "extraSyncPaths": [f"{r_rel}/.memo-model.json"],
                "reviewFeedback": {"verdict": "revise", "must_fix": ["sync"]},
                "phaseLabel": "修订",
            },
            spec_host,
            WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0),
        )
        assert out.status == "completed", out.error
        return out.result, probes[0]

    # 1. Unclosed HTML comment at EOF containing new path & model: ref -> synced=False!
    res_unclosed, probe_unclosed = await _run_sync(
        "# 备忘录\n正文仍用旧模型 model/PDD-model-v02.xlsx。\n<!-- model/PDD-model-v03.xlsx\nmodel:model/PDD-model-v03.xlsx!Valuation!C12\n"
    )
    assert probe_unclosed["synced"] is False, probe_unclosed
    assert "report_sync" in res_unclosed["failedStages"]

    # 2. Closed comment followed by visible old text then unclosed comment -> synced=False!
    res_mixed, probe_mixed = await _run_sync(
        "# 备忘录\n<!-- 注释 -->\n正文未更新。\n<!-- 待改至 model/PDD-model-v03.xlsx"
    )
    assert probe_mixed["synced"] is False, probe_mixed
    assert "report_sync" in res_mixed["failedStages"]

    # 3. Visible new path BEFORE an unclosed trailing comment -> synced=True!
    res_before, probe_before = await _run_sync(
        "# 备忘录\n估值已切换至 model/PDD-model-v03.xlsx。\n<!-- 末尾草稿注释 model/PDD-model-v02.xlsx"
    )
    assert probe_before["synced"] is True, probe_before
    assert "report_sync" not in res_before["failedStages"]


# ===========================================================================
# P0-b Round 8 fixes (Items 1-2)
# ===========================================================================


@pytest.mark.asyncio
async def test_p0b_r8_1_resume_chain_receipt_adoption_and_fresh_dispatch_strictness(
    tmp_path: Path,
) -> None:
    """R8 #1:
    1) Prior run published candidate -> resumed under a new run_id (single-hop AND multi-hop
       Run 1 -> Run 2 -> Run 3): real disk probe traces `resumed_from` recursively, accepts
       the ancestor's receipt when path+hash match and the stage was replayed, and delivers;
    2) Freshly dispatched Stage 2a on a resumed run returning success JSON pointing to an
       ancestor run's receipt MUST still be rejected (`run_matched=False`);
    3) Receipt belonging to an unrelated run outside the resume chain is rejected even on replay."""
    import asyncio
    import fnmatch
    import shlex as _shlex
    from types import SimpleNamespace as _NS

    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.middleware.background_subagent.registry import BackgroundTaskRegistry
    from ptc_agent.agent.middleware.background_subagent.workflow.driver import (
        WorkflowDriver,
        WorkflowRunSpec,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import compile_check
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )
    from src.config.models import WorkflowOrchestrationConfig

    wf_script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "company-model" / "workflow.js")
    wf_meta = compile_check(wf_script)

    def _setup_ws(name: str) -> tuple[Path, str, str, Path, str, Path, str, Path, str, Path, str, Path, Any]:
        ws = tmp_path / name
        scripts_src = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "scripts"
        dst_scripts = ws / ".agents" / "skills" / "dalton-model" / "scripts"
        dst_scripts.mkdir(parents=True, exist_ok=True)
        for py_f in scripts_src.glob("*.py"):
            shutil.copy2(py_f, dst_scripts / py_f.name)
        r_rel = "research/2026-11-27-model-PDD"
        dump_rel = f"{r_rel}/model_dump"
        base_rel = "model/PDD-model-v01.xlsx"
        base_p = ws / base_rel
        base_sha = _write_simple_valid_model(base_p)
        cand_rel = "model/PDD-model-v02.xlsx"
        cand_p = ws / cand_rel
        cm_rel = f"{dump_rel}/coord_map.json"
        cm_p = ws / cm_rel
        fp_rel = f"{dump_rel}/PDD-fingerprint.json"
        cons_rel = "data/consensus/2026-11-27_1000.json"
        cons_p = ws / cons_rel
        cons_p.parent.mkdir(parents=True, exist_ok=True)
        cons_p.write_text(
            json.dumps(
                {
                    "ticker": "PDD",
                    "as_of_date": "2026-11-27",
                    "source_type": "Market Consensus",
                    "items": [{"period": "2026E", "metric": "Total Revenue", "consensus": 1100.0}],
                }
            ),
            encoding="utf-8",
        )
        backend = create_autospec(SandboxBackend, instance=True)
        backend.workspace_dir = str(ws)

        async def _aread(path: str) -> str | None:
            p = ws / path
            return p.read_text(encoding="utf-8") if p.is_file() else None

        async def _awrite(path: str, text: str) -> bool:
            p = ws / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            return True

        async def _aglob(pattern: str, root: str = ".") -> list[str]:
            found: list[str] = []
            base_root = ws if root in (".", "") else (ws / root)
            if not base_root.exists():
                return []
            for p in base_root.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(ws).as_posix()
                    if fnmatch.fnmatch(rel, pattern):
                        found.append(rel)
            return sorted(found)

        async def _aexec(cmd: str) -> Any:
            if "sha256sum -- " in cmd:
                tail = cmd.split("sha256sum -- ", 1)[1]
                lines = []
                for rel in _shlex.split(tail):
                    p = ws / rel
                    if not p.is_file():
                        return _NS(exit_code=1, output="")
                    lines.append(f"{mfp.file_sha256(p)}  {rel}")
                return _NS(exit_code=0, output="\n".join(lines) + "\n")
            return _NS(exit_code=1, output="")

        backend.aread_text.side_effect = _aread
        backend.awrite_text.side_effect = _awrite
        backend.aglob_paths.side_effect = _aglob
        backend.aexecute.side_effect = _aexec
        return ws, r_rel, dump_rel, base_p, base_sha, cand_p, cand_rel, cm_p, cm_rel, fp_rel, cons_rel, cons_p, backend

    def _run_real_probe(ws: Path, prompt: str) -> dict[str, Any]:
        m = re.search(r"```bash\n(.*?)\n```", prompt, re.DOTALL)
        assert m, prompt
        cmd = m.group(1).replace("python3 ", f"{sys.executable} ")
        proc = subprocess.run(["bash", "-c", cmd], cwd=ws, capture_output=True, text=True, check=False)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        return json.loads(proc.stdout.strip())

    def _make_driver(
        backend: Any,
        r_rel: str,
        base_rel: str,
        run_id_str: str,
        handler: Any,
        *,
        journal_source: str | None = None,
    ) -> tuple[WorkflowDriver, list[str]]:
        dispatched: list[str] = []
        registry = BackgroundTaskRegistry()

        class _FakeDispatcher:
            subagent_types = frozenset({"general-purpose", "dalton-evidence"})

            async def dispatch(
                self,
                *,
                subagent_type: str,
                description: str,
                prompt: str,
                run_id: str | None = None,
                owner_task_id: str | None = None,
                base_configurable: dict[str, Any] | None = None,
                time_limit: float | None = None,
                **kwargs: Any,
            ) -> Any:
                dispatched.append(description)
                task = await registry.register(
                    tool_call_id=f"{run_id_str}-child-{len(dispatched)}",
                    description=description,
                    prompt=prompt[:200],
                    subagent_type=subagent_type,
                    run_id=run_id,
                )
                task.per_call_records = []

                async def _run_child() -> str:
                    res = handler(description, prompt)
                    if isinstance(res, Exception):
                        task.terminal_status = "error"
                        raise res
                    task.terminal_status = "completed"
                    return json.dumps(res, ensure_ascii=False)

                task.asyncio_task = asyncio.create_task(_run_child())
                return task

        run_task = _NS(
            task_id=run_id_str,
            display_id=run_id_str,
            task_run_id=None,
            spawned_run_id=run_id_str,
            terminal_status=None,
            stop_at=None,
            keep_straggler=lambda _w: None,
        )
        params = {
            "ticker": "PDD",
            "company": "PDD Holdings",
            "workspace_dir": ".",
            "as_of": "2026-11-27",
            "mode": "update",
            "model_path": base_rel,
            "progress_path": f"{r_rel}/.progress.json",
        }
        spec = WorkflowRunSpec(
            run_task=run_task,
            registry=registry,
            dispatcher=_FakeDispatcher(),
            backend=backend,
            thread_id="thread-r8-resume",
            short_thread_id="88888888",
            script=wf_script,
            script_args=params,
            meta=wf_meta,
            source="prebuilt",
            caps=WorkflowOrchestrationConfig(
                enabled=True,
                max_dispatches_per_run=30,
                max_concurrent_children=4,
                child_timeout=30.0,
                run_timeout=60.0,
                memory_limit_mb=64,
                cpu_budget_s=15.0,
            ),
            journal_source=journal_source,
            progress_path=f"{r_rel}/.progress.json",
        )
        return WorkflowDriver(spec), dispatched

    # --- Case 1 & 2: Multi-hop resume (Run 1 publishes v02 & writes snapshot, crashes;
    #     Run 2 replays Stage 2a & crashes in Stage 2b; Run 3 resumes from Run 2 ->
    #     traces Run 3 -> Run 2 -> Run 1, adopts Run 1's receipt, delivers normally!) ---
    ws, r_rel, dump_rel, base_p, base_sha, cand_p, cand_rel, cm_p, cm_rel, fp_rel, cons_rel, cons_p, backend = _setup_ws(
        "ws_r8_multihop"
    )
    base_rel = "model/PDD-model-v01.xlsx"
    probes_m: list[dict[str, Any]] = []

    def _h_r1(lbl: str, prompt: str) -> Any:
        if lbl == "建模准备":
            fp_obj = mfp.build_fingerprint(base_p, ticker="PDD")
            (ws / fp_rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / fp_rel).write_text(json.dumps(fp_obj, indent=2), encoding="utf-8")
            return {
                "files": [fp_rel],
                "summary": "prep",
                "mode": "update",
                "baseline_path": base_rel,
                "baseline_sha256": base_sha,
                "fingerprint_path": fp_rel,
                "consensus_path": cons_rel,
                "planned_output_path": cand_rel,
                "baseline_health_warning": None,
            }
        if lbl == "读取运行ID":
            return {"run_id": json.loads((ws / r_rel / ".progress.json").read_text(encoding="utf-8"))["run_id"]}
        if lbl == "模型构建与落盘":
            ed = medit.apply_edit_plan(
                baseline_path=base_p,
                ticker="PDD",
                plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1150}]},
                output_path=cand_p,
                workspace_dir=ws,
                fingerprint=json.loads((ws / fp_rel).read_text(encoding="utf-8")),
                coord_map_out=cm_p,
                skip_recalc=True,
                mode="update",
                progress_path=ws / r_rel / ".progress.json",
            )
            return {
                "files": [cand_rel, cm_rel],
                "summary": "v02 built",
                "candidate_path": cand_rel,
                "model_sha256": ed["candidate_sha256"],
                "recalc_status": "ok",
            }
        if lbl == "核验候选落盘":
            out = _run_real_probe(ws, prompt)
            probes_m.append(out)
            return out
        if lbl == "一致预期对照":
            # Also exercise write-snapshot updating publish receipt SHA (no rework!) then crashing
            assert mcons.main(["write-snapshot", "--model", str(cand_p), "--consensus", str(cons_p), "--coord-map", str(cm_p)]) == 0
            return RuntimeError("crash right after write-snapshot")
        if lbl in ("模型本轮范围检查", "写入降级账本"):
            return RuntimeError("interrupted")
        raise AssertionError(lbl)

    d1, _ = _make_driver(backend, r_rel, base_rel, "run-r8-m1", _h_r1)
    await d1.run()
    assert probes_m[-1]["run_matched"] is True and probes_m[-1]["receipt_run_id"] == "run-r8-m1"

    # Run 2 resumes from Run 1: replays Prep & Stage 2a, passes real probe (single-hop), then crashes in Stage 2b
    def _h_r2(lbl: str, prompt: str) -> Any:
        if lbl == "读取运行ID":
            return {"run_id": json.loads((ws / r_rel / ".progress.json").read_text(encoding="utf-8"))["run_id"]}
        if lbl == "模型构建与落盘":
            raise AssertionError("Run 2 must replay Stage 2a")
        if lbl == "核验候选落盘":
            out = _run_real_probe(ws, prompt)
            probes_m.append(out)
            return out
        if lbl in ("一致预期对照", "模型本轮范围检查", "写入降级账本"):
            return RuntimeError("Run 2 interrupted before finish")
        raise AssertionError(lbl)

    d2, disp2 = _make_driver(
        backend, r_rel, base_rel, "run-r8-m2", _h_r2, journal_source=".agents/threads/88888888/workflows/run-r8-m1"
    )
    await d2.run()
    assert "模型构建与落盘" not in disp2
    assert probes_m[-1]["run_matched"] is True and probes_m[-1]["receipt_run_id"] == "run-r8-m1"

    # Run 3 resumes from Run 2 (2 hops away from Run 1!): traces Run 3 -> Run 2 -> Run 1 and delivers!
    def _h_r3(lbl: str, prompt: str) -> Any:
        if lbl == "读取运行ID":
            return {"run_id": json.loads((ws / r_rel / ".progress.json").read_text(encoding="utf-8"))["run_id"]}
        if lbl == "模型构建与落盘":
            raise AssertionError("Run 3 must replay Stage 2a")
        if lbl == "核验候选落盘":
            out = _run_real_probe(ws, prompt)
            probes_m.append(out)
            return out
        if lbl == "一致预期对照":
            assert mcons.main(["diff", "--model", str(cand_p), "--ticker", "PDD", "--research-dir", str(ws / r_rel), "--consensus", str(cons_p)]) == 0
            return {
                "files": [cand_rel, cm_rel, f"{dump_rel}/consensus-diff.md", f"{dump_rel}/consensus-diff.json"],
                "summary": "diff ok",
                "candidate_path": cand_rel,
                "model_sha256": mfp.file_sha256(cand_p),
                "recalc_status": "ok",
                "consensus_status": "ok",
                "consensus_diff_generated": True,
            }
        if lbl == "模型本轮范围检查":
            chk_res = mchk.write_check_and_ledger(
                workspace_dir=ws,
                ticker="PDD",
                research_dir=ws / r_rel,
                candidate_path=cand_p,
                baseline_path=base_p,
                fingerprint_path=ws / fp_rel,
                coord_map_path=cm_p,
                run_id="run-r8-m3",
                workflow="company-model",
                mode="update",
            )
            files = [f"{dump_rel}/model-check.md", f"{dump_rel}/model-verification.json"]
            if chk_res.get("remaining_issues_path"):
                files.append(f"{dump_rel}/remaining-issues.md")
            return {
                "files": files,
                "summary": "checked",
                "readable": chk_res["readable"],
                "final_status": chk_res["final_status"],
                "model_sha256": chk_res["candidate_sha256"],
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{dump_rel}/remaining-issues.md" if chk_res.get("remaining_issues_path") else None,
            }
        raise AssertionError(lbl)

    d3, disp3 = _make_driver(
        backend, r_rel, base_rel, "run-r8-m3", _h_r3, journal_source=".agents/threads/88888888/workflows/run-r8-m2"
    )
    await d3.run()
    assert "模型构建与落盘" not in disp3
    assert probes_m[-1]["run_matched"] is True and probes_m[-1]["receipt_run_id"] == "run-r8-m1"
    res3 = json.loads((ws / ".agents/threads/88888888/workflows/run-r8-m3/result.json").read_text(encoding="utf-8"))
    assert res3["ok"] is True and res3["candidate_path"] == cand_rel
    vd3 = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=ws / r_rel,
        expected_run_id="run-r8-m3",
        expected_workflow="company-model",
    )
    assert vd3["ok"] is True, vd3["errors"]

    # --- Case 3: Fresh dispatch on a resumed run returning success JSON pointing to ancestor's receipt -> REJECTED! ---
    ws_f, r_f, dump_f, base_pf, base_shaf, cand_pf, cand_relf, cm_pf, cm_relf, fp_relf, cons_relf, _, backend_f = _setup_ws(
        "ws_r8_fresh_strict"
    )
    probes_f: list[dict[str, Any]] = []

    def _h_f1(lbl: str, prompt: str) -> Any:
        if lbl == "建模准备":
            fp_obj = mfp.build_fingerprint(base_pf, ticker="PDD")
            (ws_f / fp_relf).parent.mkdir(parents=True, exist_ok=True)
            (ws_f / fp_relf).write_text(json.dumps(fp_obj, indent=2), encoding="utf-8")
            return {
                "files": [fp_relf],
                "summary": "prep",
                "mode": "update",
                "baseline_path": base_rel,
                "baseline_sha256": base_shaf,
                "fingerprint_path": fp_relf,
                "consensus_path": cons_relf,
                "planned_output_path": cand_relf,
                "baseline_health_warning": None,
            }
        if lbl == "读取运行ID":
            return {"run_id": json.loads((ws_f / r_f / ".progress.json").read_text(encoding="utf-8"))["run_id"]}
        if lbl == "模型构建与落盘":
            # Publishes v02 with run-r8-f1 receipt, then crashes BEFORE returning ok (not replayable!)
            medit.apply_edit_plan(
                baseline_path=base_pf,
                ticker="PDD",
                plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1200}]},
                output_path=cand_pf,
                workspace_dir=ws_f,
                fingerprint=json.loads((ws_f / fp_relf).read_text(encoding="utf-8")),
                coord_map_out=cm_pf,
                skip_recalc=True,
                mode="update",
                progress_path=ws_f / r_f / ".progress.json",
            )
            return RuntimeError("stage2a crashed after publishing")
        if lbl == "核验候选落盘":
            # Interrupt Run 1 right away before Stage 2b
            return RuntimeError("run1 interrupted")
        raise AssertionError(lbl)

    df1, _ = _make_driver(backend_f, r_f, base_rel, "run-r8-f1", _h_f1)
    await df1.run()

    def _h_f2(lbl: str, prompt: str) -> Any:
        if lbl == "读取运行ID":
            return {"run_id": json.loads((ws_f / r_f / ".progress.json").read_text(encoding="utf-8"))["run_id"]}
        if lbl == "模型构建与落盘":
            # Fresh dispatch in Run 2 returns success JSON pointing to Run 1's v02 WITHOUT republishing!
            return {
                "files": [cand_relf, cm_relf],
                "summary": "claimed ok without running model_edit",
                "candidate_path": cand_relf,
                "model_sha256": mfp.file_sha256(cand_pf),
                "recalc_status": "ok",
            }
        if lbl == "核验候选落盘":
            out = _run_real_probe(ws_f, prompt)
            probes_f.append(out)
            return out
        raise AssertionError(f"Fresh dispatch with stale ancestor receipt must stop before {lbl!r}")

    df2, disp_f2 = _make_driver(
        backend_f, r_f, base_rel, "run-r8-f2", _h_f2, journal_source=".agents/threads/88888888/workflows/run-r8-f1"
    )
    await df2.run()
    assert "建模准备" not in disp_f2, "Prep replayed (progress has resumed_from)"
    assert "模型构建与落盘" in disp_f2, "Stage 2a was freshly dispatched"
    assert len(probes_f) == 1
    assert probes_f[0]["run_matched"] is False and probes_f[0]["receipt_run_id"] == "run-r8-f1"
    res_f2 = json.loads((ws_f / ".agents/threads/88888888/workflows/run-r8-f2/result.json").read_text(encoding="utf-8"))
    assert res_f2["ok"] is False and res_f2["status"] == "failed_unusable"
    assert res_f2["candidate_path"] is None

    # --- Case 4: Unrelated foreign run_id outside resume chain is STILL rejected even on replay ---
    rcpt_p = medit.publish_receipt_path(cand_p)
    rcpt_doc = json.loads(rcpt_p.read_text(encoding="utf-8"))
    rcpt_doc["run_id"] = "run-unrelated-outsider"
    rcpt_p.write_text(json.dumps(rcpt_doc, indent=2), encoding="utf-8")
    probes_foreign: list[dict[str, Any]] = []

    def _h_r4(lbl: str, prompt: str) -> Any:
        if lbl == "读取运行ID":
            return {"run_id": json.loads((ws / r_rel / ".progress.json").read_text(encoding="utf-8"))["run_id"]}
        if lbl == "核验候选落盘":
            out = _run_real_probe(ws, prompt)
            probes_foreign.append(out)
            return out
        raise AssertionError(f"Foreign receipt must stop before {lbl!r}")

    d4, _ = _make_driver(
        backend, r_rel, base_rel, "run-r8-m4", _h_r4, journal_source=".agents/threads/88888888/workflows/run-r8-m2"
    )
    await d4.run()
    assert len(probes_foreign) == 1
    assert probes_foreign[0]["run_matched"] is False
    assert probes_foreign[0]["receipt_run_id"] == "run-unrelated-outsider"
    res4 = json.loads((ws / ".agents/threads/88888888/workflows/run-r8-m4/result.json").read_text(encoding="utf-8"))
    assert res4["ok"] is False and res4["status"] == "failed_unusable"


@pytest.mark.asyncio
async def test_p0b_r8_2_workspace_modeling_lock_excludes_concurrent_launches_and_releases_on_finish_or_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8 #2:
    1) `company-model`, `earnings-deep-update` (with `model_path`), and `full-coverage` acquire
       the per-workspace modeling lock at entry before entering modeling;
    2) While one modeling run holds the lock, concurrent launches (across threads or workflows in
       the same workspace, including true `asyncio.gather` races) are refused at entry with the
       readable message「这个工作区正在更新模型，请等它完成后再试」, without entering modeling
       or clobbering the active holder's progress;
    3) Non-modeling workflows (`bullbear-doc`, `earnings-deep-update` without `model_path`) and
       other workspaces are NOT blocked;
    4) Lock releases cleanly on completion / failure / cancellation, and recovers automatically
       after `expires_at` (with late release from an expired holder never clobbering a new holder)."""
    import asyncio
    from types import SimpleNamespace as _NS

    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.middleware.background_subagent.dispatch import SubagentDispatcher
    from ptc_agent.agent.middleware.background_subagent.middleware import BackgroundSubagentMiddleware
    from ptc_agent.agent.middleware.background_subagent.registry import BackgroundTaskRegistry
    from ptc_agent.agent.middleware.background_subagent.workflow import tool as wf_tool_mod
    from ptc_agent.agent.middleware.background_subagent.workflow.driver import (
        WorkflowDriver,
        WorkflowRunError,
        WorkflowRunSpec,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import compile_check
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import PrebuiltWorkflowRegistry
    from src.config.models import WorkflowOrchestrationConfig
    from src.dalton.workflows.modeling_lock import (
        MODELING_LOCK_BUSY_MESSAGE,
        MODELING_LOCK_PATH,
        release_modeling_lock,
    )

    ws = tmp_path / "ws_r8_lock"
    ws.mkdir(parents=True, exist_ok=True)
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = str(ws)

    async def _aread(path: str) -> str | None:
        p = ws / path
        return p.read_text(encoding="utf-8") if p.is_file() else None

    async def _awrite(path: str, text: str) -> bool:
        p = ws / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return True

    backend.aread_text.side_effect = _aread
    backend.awrite_text.side_effect = _awrite

    def _wf_js(name: str) -> str:
        return f"export const meta = {{ name: {json.dumps(name)}, description: {json.dumps(name)} }}\nreturn {{ ok: true }}\n"

    scripts = {
        "company-model": _wf_js("company-model"),
        "earnings-deep-update": _wf_js("earnings-deep-update"),
        "full-coverage": _wf_js("full-coverage"),
        "bullbear-doc": _wf_js("bullbear-doc"),
    }
    prebuilt = create_autospec(PrebuiltWorkflowRegistry, instance=True)
    prebuilt.get.side_effect = scripts.get
    prebuilt.names.side_effect = lambda: sorted(scripts)

    captured_specs: list[WorkflowRunSpec] = []
    monkeypatch.setattr(
        wf_tool_mod,
        "WorkflowDriver",
        lambda spec: (captured_specs.append(spec), create_autospec(WorkflowDriver, instance=True))[1],
    )

    async def _noop_spawn(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(wf_tool_mod, "spawn_task_writer", _noop_spawn)
    monkeypatch.setattr(
        "src.config.settings.get_workflow_orchestration_config",
        lambda: WorkflowOrchestrationConfig(),
    )
    monkeypatch.setattr(
        "src.dalton.workflows.as_of.resolve_launch_as_of",
        lambda _b, _n, p, _j: asyncio.sleep(0, result=p),
    )
    monkeypatch.setattr(
        "src.dalton.workflows.subject_kind.resolve_launch_subject_kind",
        lambda _n, p, _c: asyncio.sleep(0, result=p),
    )
    monkeypatch.setattr(
        "src.dalton.workflows.company_routing.resolve_launch_company_routing",
        lambda *_a, **_kw: asyncio.sleep(0, result=None),
    )

    active_ws_id = {"id": "ws-lock-1"}
    monkeypatch.setattr(
        wf_tool_mod,
        "get_config",
        lambda: {"metadata": {"run_id": "turn-1"}, "configurable": {"user_id": "u-1", "workspace_id": active_ws_id["id"]}},
    )

    def _make_tool(thread_id: str, short_id: str, target_backend: Any = backend) -> Any:
        mw = create_autospec(BackgroundSubagentMiddleware, instance=True)
        mw.registry = BackgroundTaskRegistry()
        mw.checkpointer = object()

        async def _admit(task: Any, **_kw: Any) -> str:
            return ""

        async def _abort(task: Any, _err: BaseException) -> None:
            task.mark_cancelled("aborted before spawn")

        mw.admit_task_run.side_effect = _admit
        mw._abort_admitted_run.side_effect = _abort
        dispatcher = create_autospec(SubagentDispatcher, instance=True)
        dispatcher.middleware = mw
        dispatcher.registry = mw.registry
        dispatcher.subagent_types = ["general-purpose"]
        return wf_tool_mod.create_run_workflow_tool(
            dispatcher=dispatcher,
            backend=target_backend,
            thread_id=thread_id,
            short_thread_id=short_id,
            store=None,
            user_id="u-1",
            prebuilt_workflows=prebuilt,
        )

    # 1. Concurrent `asyncio.gather` race on two threads in the SAME workspace:
    #    exactly ONE acquires the lock and starts, the other gets the readable refusal!
    tool_t1 = _make_tool("thread-a", "aaaa1111")
    tool_t2 = _make_tool("thread-b", "bbbb2222")
    r1, r2 = await asyncio.gather(
        tool_t1.coroutine(
            workflow="company-model",
            params={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
            tool_call_id="call-race-1",
        ),
        tool_t2.coroutine(
            workflow="full-coverage",
            params={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
            tool_call_id="call-race-2",
        ),
    )
    replies = [str(r1), str(r2)]
    busy_replies = [m for m in replies if MODELING_LOCK_BUSY_MESSAGE in m]
    assert len(busy_replies) == 1, replies
    assert len(captured_specs) == 1
    holder_spec = captured_specs[0]
    assert holder_spec.modeling_lock_held is True
    lock_doc = json.loads((ws / MODELING_LOCK_PATH).read_text(encoding="utf-8"))
    assert lock_doc["status"] == "locked"
    assert lock_doc["run_id"] == holder_spec.run_task.task_id
    assert lock_doc["expires_at"] > lock_doc["acquired_at"]

    # 2. While held, earnings-deep-update WITH model_path is refused,
    #    while earnings-deep-update WITHOUT model_path and bullbear-doc are NOT blocked!
    tool_t3 = _make_tool("thread-c", "cccc3333")
    refused_deep = await tool_t3.coroutine(
        workflow="earnings-deep-update",
        params={"ticker": "PDD", "quarter": "FY26Q3", "workspace_dir": ".", "release_date": "2026-11-27", "model_path": "model/PDD-model-v01.xlsx"},
        tool_call_id="call-deep-with-model",
    )
    assert MODELING_LOCK_BUSY_MESSAGE in str(refused_deep)
    assert len(captured_specs) == 1

    ok_deep_no_model = await tool_t3.coroutine(
        workflow="earnings-deep-update",
        params={"ticker": "PDD", "quarter": "FY26Q3", "workspace_dir": ".", "release_date": "2026-11-27"},
        tool_call_id="call-deep-no-model",
    )
    assert MODELING_LOCK_BUSY_MESSAGE not in str(ok_deep_no_model)
    assert len(captured_specs) == 2 and captured_specs[-1].modeling_lock_held is False

    ok_bullbear = await tool_t3.coroutine(
        workflow="bullbear-doc",
        params={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
        tool_call_id="call-bb",
    )
    assert MODELING_LOCK_BUSY_MESSAGE not in str(ok_bullbear)
    assert len(captured_specs) == 3

    # 3. Another workspace is NOT blocked!
    ws_other = tmp_path / "ws_r8_lock_other"
    ws_other.mkdir(parents=True, exist_ok=True)
    backend_other = create_autospec(SandboxBackend, instance=True)
    backend_other.workspace_dir = str(ws_other)

    async def _aread_other(path: str) -> str | None:
        p = ws_other / path
        return p.read_text(encoding="utf-8") if p.is_file() else None

    async def _awrite_other(path: str, text: str) -> bool:
        p = ws_other / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return True

    backend_other.aread_text.side_effect = _aread_other
    backend_other.awrite_text.side_effect = _awrite_other
    active_ws_id["id"] = "ws-lock-2"
    tool_other = _make_tool("thread-other", "oooo9999", target_backend=backend_other)
    ok_other = await tool_other.coroutine(
        workflow="company-model",
        params={"ticker": "META", "workspace_dir": ".", "as_of": "2026-11-27"},
        tool_call_id="call-other-ws",
    )
    assert MODELING_LOCK_BUSY_MESSAGE not in str(ok_other)
    assert len(captured_specs) == 4
    active_ws_id["id"] = "ws-lock-1"

    # 4. Real WorkflowDriver lifecycle: holds lock while running, blocks a second concurrent
    #    modeling WorkflowDriver without touching Run 1's .progress.json, and releases on finish!
    released_ok = await release_modeling_lock(
        backend,
        run_id=holder_spec.run_task.task_id,
        workflow=holder_spec.meta.name,
        workspace_id="ws-lock-1",
    )
    assert released_ok is True

    entered_run1 = asyncio.Event()
    unblock_run1 = asyncio.Event()
    run2_dispatched: list[str] = []
    prog_rel = "research/2026-11-27-model-PDD/.progress.json"
    cm_wait_script = (
        "export const meta = { name: 'company-model', description: 'cm' }\n"
        "await agent('step1', { label: '建模准备' })\n"
        "return { ok: true }\n"
    )
    cm_wait_meta = compile_check(cm_wait_script)

    def _make_live_driver(run_id_str: str, is_first: bool) -> WorkflowDriver:
        reg = BackgroundTaskRegistry()

        class _Disp:
            subagent_types = frozenset({"general-purpose"})

            async def dispatch(
                self,
                *,
                subagent_type: str,
                description: str,
                prompt: str,
                run_id: str | None = None,
                owner_task_id: str | None = None,
                base_configurable: dict[str, Any] | None = None,
                time_limit: float | None = None,
                **kwargs: Any,
            ) -> Any:
                if not is_first:
                    run2_dispatched.append(description)
                t = await reg.register(
                    tool_call_id=f"{run_id_str}-c",
                    description=description,
                    prompt=prompt,
                    subagent_type=subagent_type,
                    run_id=run_id,
                )
                t.per_call_records = []

                async def _work() -> str:
                    if is_first:
                        entered_run1.set()
                        await unblock_run1.wait()
                    t.terminal_status = "completed"
                    return "ok"

                t.asyncio_task = asyncio.create_task(_work())
                return t

        rt = _NS(
            task_id=run_id_str,
            display_id=run_id_str,
            task_run_id=None,
            spawned_run_id=run_id_str,
            terminal_status=None,
            stop_at=None,
            keep_straggler=lambda _w: None,
        )
        return WorkflowDriver(
            WorkflowRunSpec(
                run_task=rt,
                registry=reg,
                dispatcher=_Disp(),
                backend=backend,
                thread_id=f"thr-{run_id_str}",
                short_thread_id=run_id_str[:8],
                script=cm_wait_script,
                script_args={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
                meta=cm_wait_meta,
                source="prebuilt",
                base_configurable={"workspace_id": "ws-lock-1"},
                caps=WorkflowOrchestrationConfig(enabled=True, child_timeout=10.0, run_timeout=30.0),
                progress_path=prog_rel,
            )
        )

    live_d1 = _make_live_driver("run-live-1", is_first=True)
    live_d2 = _make_live_driver("run-live-2", is_first=False)
    t_d1 = asyncio.create_task(live_d1.run())
    await entered_run1.wait()

    # While live_d1 is running, live_d2 is rejected at entry, never dispatches, never overwrites .progress.json!
    with pytest.raises(WorkflowRunError, match=MODELING_LOCK_BUSY_MESSAGE):
        await live_d2.run()
    assert run2_dispatched == []
    prog_during = json.loads((ws / prog_rel).read_text(encoding="utf-8"))
    assert prog_during["run_id"] == "run-live-1" and prog_during["status"] == "running"

    # Let live_d1 finish -> lock is released automatically!
    unblock_run1.set()
    await t_d1
    lock_after = json.loads((ws / MODELING_LOCK_PATH).read_text(encoding="utf-8"))
    assert lock_after["status"] == "released" and lock_after["run_id"] == "run-live-1"

    # Now a subsequent launch succeeds!
    ok_after_release = await tool_t1.coroutine(
        workflow="company-model",
        params={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
        tool_call_id="call-after-release",
    )
    assert MODELING_LOCK_BUSY_MESSAGE not in str(ok_after_release)
    new_holder_id = captured_specs[-1].run_task.task_id

    # 5. Expired lock recovery + late release from expired holder must NOT clobber new holder!
    (ws / MODELING_LOCK_PATH).write_text(
        json.dumps(
            {
                "status": "locked",
                "run_id": "run-crashed-old",
                "workflow": "company-model",
                "acquired_at": 1.0,
                "expires_at": 2.0,
            }
        ),
        encoding="utf-8",
    )
    ok_after_expiry = await tool_t2.coroutine(
        workflow="full-coverage",
        params={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
        tool_call_id="call-after-expiry",
    )
    assert MODELING_LOCK_BUSY_MESSAGE not in str(ok_after_expiry)
    recovered_holder_id = captured_specs[-1].run_task.task_id
    assert recovered_holder_id != new_holder_id

    # Late release from the crashed old run must be a no-op!
    assert (
        await release_modeling_lock(
            backend, run_id="run-crashed-old", workflow="company-model", workspace_id="ws-lock-1"
        )
        is False
    )
    lock_still_held = json.loads((ws / MODELING_LOCK_PATH).read_text(encoding="utf-8"))
    assert lock_still_held["status"] == "locked" and lock_still_held["run_id"] == recovered_holder_id


@pytest.mark.asyncio
async def test_p0b_r9_1_modeling_uses_server_bound_run_id_and_isolates_deep_update_progress(
    tmp_path: Path,
) -> None:
    """R9 #1 (Codex P1):
    1) Modeled vs non-modeled ``earnings-deep-update`` runs in the same workspace,
       release_date, and quarter run in parallel using separate progress files
       (``.progress.json`` vs ``.progress.no-model.json``);
    2) Modeling stages receive server-bound ``run_id``, ``run_dir``, and ``resumed_from``
       directly (never dispatching ``读取运行ID`` or reading shared ``.progress.json``);
    3) Even if ``.progress.json`` is overwritten with another run's id mid-run, both a
       fresh modeled run and a resumed modeled run running alongside a non-modeled run
       stamp the server-bound ``run_id``, pass the real disk probe, and pass verify_done."""
    import asyncio
    from types import SimpleNamespace as _NS

    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.middleware.background_subagent.registry import BackgroundTaskRegistry
    from ptc_agent.agent.middleware.background_subagent.workflow.driver import (
        WorkflowDriver,
        WorkflowRunSpec,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import compile_check
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )
    from src.config.models import WorkflowOrchestrationConfig

    ws = tmp_path / "ws_r9_parallel_deep"
    ws.mkdir(parents=True, exist_ok=True)
    r_rel = "research/2026-11-27-deep-FY26Q3"
    dump_rel = f"{r_rel}/model_dump"
    base_rel = "model/PDD-model-v01.xlsx"
    cand_rel = "model/PDD-model-v02.xlsx"
    fp_rel = f"{dump_rel}/PDD-fingerprint.json"
    cm_rel = f"{dump_rel}/coord_map.json"
    cons_rel = "data/consensus/2026-11-27_1000.json"
    draft_rel = "drafts/2026-11-27-deep-FY26Q3.md"
    shared_prog_rel = f"{r_rel}/.progress.json"
    nomodel_prog_rel = f"{r_rel}/.progress.no-model.json"

    base_p = ws / base_rel
    base_p.parent.mkdir(parents=True, exist_ok=True)
    base_sha = _write_simple_valid_model(base_p)
    cand_p = ws / cand_rel
    cm_p = ws / cm_rel
    cons_p = ws / cons_rel
    cons_p.parent.mkdir(parents=True, exist_ok=True)
    cons_p.write_text(
        json.dumps(
            {
                "ticker": "PDD",
                "as_of_date": "2026-11-27",
                "source_type": "Market Consensus",
                "items": [{"period": "2026E", "metric": "Total Revenue", "consensus": 1100.0}],
            }
        ),
        encoding="utf-8",
    )
    skills_dst = ws / ".agents" / "skills" / "dalton-model" / "scripts"
    skills_dst.parent.mkdir(parents=True, exist_ok=True)
    if not skills_dst.exists():
        skills_dst.symlink_to(REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "scripts")

    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = str(ws)

    async def _aread(path: str) -> str | None:
        p = ws / path
        return p.read_text(encoding="utf-8") if p.is_file() else None

    async def _awrite(path: str, text: str) -> bool:
        p = ws / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return True

    async def _aglob(pattern: str, _root: str = ".") -> list[str]:
        return sorted(str(p.relative_to(ws)) for p in ws.glob(pattern))

    async def _aexec(cmd: str) -> Any:
        proc = subprocess.run(["bash", "-c", cmd], cwd=ws, capture_output=True, text=True, check=False)
        return _NS(exit_code=proc.returncode, output=proc.stdout)

    backend.aread_text.side_effect = _aread
    backend.awrite_text.side_effect = _awrite
    backend.aglob_paths.side_effect = _aglob
    backend.aexecute.side_effect = _aexec

    def _run_real_probe(prompt: str) -> dict[str, Any]:
        m = re.search(r"```bash\n(.*?)\n```", prompt, re.DOTALL)
        assert m, prompt
        cmd = m.group(1).replace("python3 ", f"{sys.executable} ")
        proc = subprocess.run(["bash", "-c", cmd], cwd=ws, capture_output=True, text=True, check=False)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        return json.loads(proc.stdout.strip())

    wf_script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "earnings-deep-update" / "workflow.js")
    wf_meta = compile_check(wf_script)

    def _touch_text(rel: str, body: str = "content\n" * 15) -> str:
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def _make_deep_driver(
        *,
        run_id_str: str,
        short_tid: str,
        model_path_arg: str | None,
        handler: Any,
        journal_source: str | None = None,
    ) -> tuple[WorkflowDriver, list[str]]:
        dispatched: list[str] = []
        reg = BackgroundTaskRegistry()

        class _Disp:
            subagent_types = frozenset({"general-purpose", "dalton-evidence", "dalton-reviewer"})

            async def dispatch(
                self,
                *,
                subagent_type: str,
                description: str,
                prompt: str,
                run_id: str | None = None,
                owner_task_id: str | None = None,
                base_configurable: dict[str, Any] | None = None,
                time_limit: float | None = None,
                **kwargs: Any,
            ) -> Any:
                dispatched.append(description)
                t = await reg.register(
                    tool_call_id=f"{run_id_str}-{len(dispatched)}",
                    description=description,
                    prompt=prompt[:200],
                    subagent_type=subagent_type,
                    run_id=run_id,
                    owner_task_id=owner_task_id,
                )
                t.per_call_records = []

                async def _work() -> str:
                    res = await handler(description, prompt)
                    if isinstance(res, Exception):
                        t.terminal_status = "error"
                        raise res
                    t.terminal_status = "completed"
                    return json.dumps(res, ensure_ascii=False)

                t.asyncio_task = asyncio.create_task(_work())
                return t

        rt = _NS(
            task_id=run_id_str,
            display_id=run_id_str,
            task_run_id=None,
            spawned_run_id=run_id_str,
            terminal_status=None,
            stop_at=None,
            keep_straggler=lambda _w: None,
        )
        params = {
            "ticker": "PDD",
            "company": "PDD Holdings",
            "quarter": "FY26Q3",
            "workspace_dir": ".",
            "release_date": "2026-11-27",
            "model_path": model_path_arg,
            "progress_path": shared_prog_rel,
        }
        spec = WorkflowRunSpec(
            run_task=rt,
            registry=reg,
            dispatcher=_Disp(),
            backend=backend,
            thread_id=f"thr-{short_tid}",
            short_thread_id=short_tid,
            script=wf_script,
            script_args=params,
            meta=wf_meta,
            source="prebuilt",
            base_configurable={"workspace_id": "ws-r9-1"},
            caps=WorkflowOrchestrationConfig(
                enabled=True,
                max_dispatches_per_run=40,
                max_concurrent_children=4,
                child_timeout=30.0,
                run_timeout=60.0,
                memory_limit_mb=64,
                cpu_budget_s=15.0,
            ),
            journal_source=journal_source,
            progress_path=shared_prog_rel,
        )
        return WorkflowDriver(spec), dispatched

    # Case A: Modeled and non-modeled earnings-deep-update run CONCURRENTLY in the same
    # workspace, release_date, and quarter, both passing shared_prog_rel!
    nomodel_started = asyncio.Event()
    modeled_done_prep = asyncio.Event()
    modeled_prompts: dict[str, str] = {}
    probes_a: list[dict[str, Any]] = []

    def _review_pass() -> dict[str, Any]:
        return {
            "verdict": "pass",
            "summary": "ok",
            "must_fix": [],
            "items": [{"check": i, "result": "pass", "reason": "ok", "lines": "1"} for i in range(1, 10)],
        }

    def _evidence_reply(prompt: str) -> dict[str, Any]:
        paths = [ln[2:].strip() for ln in prompt.splitlines() if ln.startswith("- ")]
        out = []
        for rel in paths:
            p = ws / rel
            if not p.is_file():
                _touch_text(rel)
            out.append(f"{rel}|{hashlib.sha256(p.read_bytes()).hexdigest()}")
        return {"files": out}

    async def _h_nomodel(lbl: str, prompt: str) -> Any:
        if lbl == "八节点评":
            nomodel_started.set()
            await modeled_done_prep.wait()
            sha = _touch_text(draft_rel, "# Deep draft\n" * 20)
            return {"files": [draft_rel], "summary": "draft", "sha256": sha}
        if lbl in ("语言审查 1", "语言审查 2"):
            _touch_text(f"{draft_rel}.language.json", '{"rounds": [1, 2]}')
            sha = hashlib.sha256((ws / draft_rel).read_bytes()).hexdigest()
            return {"files": [draft_rel, f"{draft_rel}.language.json"], "summary": "lang", "sha256": sha}
        if lbl == "校准与对账":
            for rel in (f"{r_rel}/reconcile.md", f"{r_rel}/reconcile.json", f"{r_rel}/thesis-calibration.md", f"{r_rel}/management-delta.md"):
                _touch_text(rel, '{"wrong":0,"partial":0}' if rel.endswith(".json") else "# note\n" * 15)
            sha = hashlib.sha256((ws / draft_rel).read_bytes()).hexdigest()
            return {"files": [draft_rel, f"{r_rel}/reconcile.md"], "summary": "calib", "sha256": sha}
        if lbl == "审稿":
            return _review_pass()
        if lbl == "笔记语言审查":
            for rel in (f"{r_rel}/reconcile.md", f"{r_rel}/thesis-calibration.md", f"{r_rel}/management-delta.md"):
                _touch_text(f"{rel}.language.json", '{"rounds": [1, 2]}')
            return {"files": [f"{r_rel}/reconcile.md.language.json"], "summary": "notes"}
        if lbl == "知识层修订草案":
            _touch_text(f"{r_rel}/knowledge-revision.json", '{"thesis": {}}')
            return {"files": [f"{r_rel}/knowledge-revision.json"], "summary": "kp"}
        if lbl == "产物核验":
            return _evidence_reply(prompt)
        raise AssertionError(f"nomodel unexpected: {lbl}")

    async def _h_modeled_a(lbl: str, prompt: str) -> Any:
        modeled_prompts[lbl] = prompt
        if lbl == "读取运行ID":
            raise AssertionError("Server-bound modeling run must NOT dispatch 读取运行ID")
        if lbl == "八节点评":
            sha = _touch_text(draft_rel, "# Deep draft\n" * 20)
            return {"files": [draft_rel], "summary": "draft", "sha256": sha}
        if lbl == "建模准备":
            await nomodel_started.wait()
            fp_obj = mfp.build_fingerprint(base_p, ticker="PDD")
            (ws / fp_rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / fp_rel).write_text(json.dumps(fp_obj, indent=2), encoding="utf-8")
            _touch_text(f"{r_rel}/mapping.md", "# mapping\n" * 15)
            modeled_done_prep.set()
            return {
                "files": [fp_rel, f"{r_rel}/mapping.md"],
                "summary": "prep",
                "mode": "update",
                "baseline_path": base_rel,
                "baseline_sha256": base_sha,
                "fingerprint_path": fp_rel,
                "consensus_path": cons_rel,
                "planned_output_path": cand_rel,
                "baseline_health_warning": None,
            }
        if lbl == "模型构建与落盘":
            # Verify prompt binds server run_id directly (NOT --progress-path)
            assert '--run-id "run-r9-model-A"' in prompt
            assert "--progress-path" not in prompt
            # Even if shared .progress.json is corrupted by an external writer right now,
            # model_edit uses explicit --run-id and ignores .progress.json!
            (ws / shared_prog_rel).write_text(
                json.dumps({"run_id": "run-corrupted-other", "workflow": "earnings-deep-update"}),
                encoding="utf-8",
            )
            ed = medit.apply_edit_plan(
                baseline_path=base_p,
                ticker="PDD",
                plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1180}]},
                output_path=cand_p,
                workspace_dir=ws,
                fingerprint=json.loads((ws / fp_rel).read_text(encoding="utf-8")),
                coord_map_out=cm_p,
                skip_recalc=True,
                mode="update",
                run_id="run-r9-model-A",
            )
            return {
                "files": [cand_rel, cm_rel],
                "summary": "v02 built",
                "candidate_path": cand_rel,
                "model_sha256": ed["candidate_sha256"],
                "recalc_status": "ok",
            }
        if lbl == "核验候选落盘":
            out = _run_real_probe(prompt)
            probes_a.append(out)
            return out
        if lbl == "一致预期对照":
            assert mcons.main(["write-snapshot", "--model", str(cand_p), "--consensus", str(cons_p), "--coord-map", str(cm_p)]) == 0
            assert mcons.main(["diff", "--model", str(cand_p), "--ticker", "PDD", "--research-dir", str(ws / r_rel), "--consensus", str(cons_p)]) == 0
            return {
                "files": [cand_rel, cm_rel, f"{dump_rel}/consensus-diff.md", f"{dump_rel}/consensus-diff.json"],
                "summary": "cons ok",
                "candidate_path": cand_rel,
                "model_sha256": mfp.file_sha256(cand_p),
                "recalc_status": "ok",
                "consensus_status": "ok",
                "consensus_diff_generated": True,
            }
        if lbl == "模型本轮范围检查":
            assert '--run-id "run-r9-model-A"' in prompt
            chk_res = mchk.write_check_and_ledger(
                workspace_dir=ws,
                ticker="PDD",
                research_dir=ws / r_rel,
                candidate_path=cand_p,
                baseline_path=base_p,
                fingerprint_path=ws / fp_rel,
                coord_map_path=cm_p,
                run_id="run-r9-model-A",
                workflow="earnings-deep-update",
                mode="update",
            )
            files = [f"{dump_rel}/model-check.md", f"{dump_rel}/model-verification.json"]
            if chk_res.get("remaining_issues_path"):
                files.append(f"{dump_rel}/remaining-issues.md")
            return {
                "files": files,
                "summary": "chk",
                "readable": chk_res["readable"],
                "final_status": chk_res["final_status"],
                "model_sha256": chk_res["candidate_sha256"],
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{dump_rel}/remaining-issues.md" if chk_res.get("remaining_issues_path") else None,
            }
        if lbl == "模型变动拆解":
            _touch_text(f"{r_rel}/model-delta.md", "# model delta\n" * 15)
            return {"files": [f"{r_rel}/model-delta.md"], "summary": "delta"}
        if lbl in ("语言审查 1", "语言审查 2"):
            _touch_text(f"{draft_rel}.language.json", '{"rounds": [1, 2]}')
            sha = hashlib.sha256((ws / draft_rel).read_bytes()).hexdigest()
            return {"files": [draft_rel, f"{draft_rel}.language.json"], "summary": "lang", "sha256": sha}
        if lbl == "校准与对账":
            for rel in (f"{r_rel}/reconcile.md", f"{r_rel}/reconcile.json", f"{r_rel}/thesis-calibration.md", f"{r_rel}/management-delta.md"):
                _touch_text(rel, '{"wrong":0,"partial":0}' if rel.endswith(".json") else "# note\n" * 15)
            sha = hashlib.sha256((ws / draft_rel).read_bytes()).hexdigest()
            return {"files": [draft_rel, f"{r_rel}/reconcile.md"], "summary": "calib", "sha256": sha}
        if lbl == "审稿":
            return _review_pass()
        if lbl == "笔记语言审查":
            for rel in (f"{r_rel}/reconcile.md", f"{r_rel}/thesis-calibration.md", f"{r_rel}/management-delta.md", f"{r_rel}/mapping.md", f"{r_rel}/model-delta.md"):
                _touch_text(f"{rel}.language.json", '{"rounds": [1, 2]}')
            return {"files": [f"{r_rel}/mapping.md.language.json"], "summary": "notes"}
        if lbl == "知识层修订草案":
            _touch_text(f"{r_rel}/knowledge-revision.json", '{"thesis": {}}')
            return {"files": [f"{r_rel}/knowledge-revision.json"], "summary": "kp"}
        if lbl == "产物核验":
            return _evidence_reply(prompt)
        raise AssertionError(f"modeled unexpected: {lbl}")

    drv_model_a, disp_model_a = _make_deep_driver(
        run_id_str="run-r9-model-A",
        short_tid="aaaa1111",
        model_path_arg=base_rel,
        handler=_h_modeled_a,
    )
    drv_nomodel_b, _ = _make_deep_driver(
        run_id_str="run-r9-nomodel-B",
        short_tid="bbbb2222",
        model_path_arg=None,
        handler=_h_nomodel,
    )

    assert drv_model_a._journal.progress_path == shared_prog_rel
    assert drv_nomodel_b._journal.progress_path == nomodel_prog_rel

    await asyncio.gather(drv_model_a.run(), drv_nomodel_b.run())

    assert "读取运行ID" not in disp_model_a
    assert len(probes_a) == 1 and probes_a[0]["run_matched"] is True
    assert probes_a[0]["receipt_run_id"] == "run-r9-model-A"
    prog_model_doc = json.loads((ws / shared_prog_rel).read_text(encoding="utf-8"))
    prog_nomodel_doc = json.loads((ws / nomodel_prog_rel).read_text(encoding="utf-8"))
    assert prog_model_doc["run_id"] == "run-r9-model-A" and prog_model_doc["status"] == "completed"
    assert prog_nomodel_doc["run_id"] == "run-r9-nomodel-B" and prog_nomodel_doc["status"] == "completed"
    vd_a = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=ws / r_rel,
        expected_run_id="run-r9-model-A",
        expected_workflow="earnings-deep-update",
    )
    assert vd_a["ok"] is True, vd_a["errors"]

    # Case B: Resumed modeled run in parallel with a non-modeled run while .progress.json
    # holds a completely wrong run_id -> server-bound run_dir & resumed_from still replay
    # Stage 2a and validate ancestor receipt without reading .progress.json!
    probes_b: list[dict[str, Any]] = []
    nomodel_started_2 = asyncio.Event()
    modeled_done_probe_2 = asyncio.Event()

    async def _h_nomodel_2(lbl: str, prompt: str) -> Any:
        if lbl == "八节点评":
            nomodel_started_2.set()
            await modeled_done_probe_2.wait()
        return await _h_nomodel(lbl, prompt)

    async def _h_modeled_resume(lbl: str, prompt: str) -> Any:
        if lbl in ("读取运行ID", "模型构建与落盘"):
            raise AssertionError(f"Resumed modeled run must not dispatch {lbl}")
        if lbl == "核验候选落盘":
            await nomodel_started_2.wait()
            # Overwrite shared .progress.json with wrong run_id right before probe!
            (ws / shared_prog_rel).write_text(
                json.dumps({"run_id": "run-wrong-clobbered", "resumed_from": None}),
                encoding="utf-8",
            )
            out = _run_real_probe(prompt)
            probes_b.append(out)
            modeled_done_probe_2.set()
            return out
        if lbl == "模型本轮范围检查":
            assert '--run-id "run-r9-model-A2"' in prompt
            chk_res = mchk.write_check_and_ledger(
                workspace_dir=ws,
                ticker="PDD",
                research_dir=ws / r_rel,
                candidate_path=cand_p,
                baseline_path=base_p,
                fingerprint_path=ws / fp_rel,
                coord_map_path=cm_p,
                run_id="run-r9-model-A2",
                workflow="earnings-deep-update",
                mode="update",
            )
            files = [f"{dump_rel}/model-check.md", f"{dump_rel}/model-verification.json"]
            if chk_res.get("remaining_issues_path"):
                files.append(f"{dump_rel}/remaining-issues.md")
            return {
                "files": files,
                "summary": "chk2",
                "readable": chk_res["readable"],
                "final_status": chk_res["final_status"],
                "model_sha256": chk_res["candidate_sha256"],
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{dump_rel}/remaining-issues.md" if chk_res.get("remaining_issues_path") else None,
            }
        return await _h_modeled_a(lbl, prompt)

    drv_model_res, disp_model_res = _make_deep_driver(
        run_id_str="run-r9-model-A2",
        short_tid="aaaa1111",
        model_path_arg=base_rel,
        handler=_h_modeled_resume,
        journal_source=".agents/threads/aaaa1111/workflows/run-r9-model-A",
    )
    drv_nomodel_2, _ = _make_deep_driver(
        run_id_str="run-r9-nomodel-B2",
        short_tid="bbbb2222",
        model_path_arg=None,
        handler=_h_nomodel_2,
    )
    await asyncio.gather(drv_model_res.run(), drv_nomodel_2.run())

    assert "模型构建与落盘" not in disp_model_res
    assert "读取运行ID" not in disp_model_res
    assert len(probes_b) == 1 and probes_b[0]["run_matched"] is True
    assert probes_b[0]["receipt_run_id"] == "run-r9-model-A"
    vd_b = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=ws / r_rel,
        expected_run_id="run-r9-model-A2",
        expected_workflow="earnings-deep-update",
    )
    assert vd_b["ok"] is True, vd_b["errors"]


@pytest.mark.asyncio
async def test_p0b_r9_2_short_lease_heartbeat_startup_cancel_and_delayed_stop_lock_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9 #2 (Codex P1 ×2):
    1) Lock uses a short lease (default 10 min, NOT 4h31m) and renews via heartbeat while running;
    2) Cancelling during startup while saving ``workflow.js`` or ``args.json`` (before driver
       takeover) releases the modeling lock cleanly so subsequent launches are not blocked;
    3) When stopping a modeling run is delayed (in-process writer slow to unwind OR sandbox
       writer process still alive), the driver does NOT release the lock prematurely: a new
       modeling entry is refused while the lease holds, and succeeds once the short lease expires."""
    import asyncio
    import time
    from types import SimpleNamespace as _NS

    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.middleware.background_subagent.dispatch import SubagentDispatcher
    from ptc_agent.agent.middleware.background_subagent.middleware import BackgroundSubagentMiddleware
    from ptc_agent.agent.middleware.background_subagent.registry import BackgroundTaskRegistry
    from ptc_agent.agent.middleware.background_subagent.workflow import tool as wf_tool_mod
    from ptc_agent.agent.middleware.background_subagent.workflow.driver import (
        WorkflowDriver,
        WorkflowRunSpec,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import compile_check
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import PrebuiltWorkflowRegistry
    from src.config.models import WorkflowOrchestrationConfig
    from src.dalton.workflows import modeling_lock as mlock

    # 1. Short lease even with large run_timeout (4h30m)
    long_caps = WorkflowOrchestrationConfig(run_timeout=16200.0)
    assert mlock.modeling_lock_ttl(long_caps) == mlock.DEFAULT_MODELING_LOCK_TTL_SECONDS == 600.0

    ws = tmp_path / "ws_r9_lock_lifecycle"
    ws.mkdir(parents=True, exist_ok=True)
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = str(ws)

    cancel_on_artifact: dict[str, str | None] = {"target": None}

    async def _aread(path: str) -> str | None:
        p = ws / path
        return p.read_text(encoding="utf-8") if p.is_file() else None

    async def _awrite(path: str, text: str) -> bool:
        target = cancel_on_artifact["target"]
        if target and path.endswith(f"/{target}"):
            raise asyncio.CancelledError("cancelled while saving startup artifact")
        p = ws / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return True

    backend.aread_text.side_effect = _aread
    backend.awrite_text.side_effect = _awrite

    # 2. Startup cancel while saving workflow.js and args.json releases lock!
    cm_js = "export const meta = { name: 'company-model', description: 'cm' }\nreturn { ok: true }\n"
    prebuilt = create_autospec(PrebuiltWorkflowRegistry, instance=True)
    prebuilt.get.side_effect = {"company-model": cm_js}.get
    prebuilt.names.side_effect = lambda: ["company-model"]

    captured_specs: list[WorkflowRunSpec] = []
    monkeypatch.setattr(
        wf_tool_mod,
        "WorkflowDriver",
        lambda spec: (captured_specs.append(spec), create_autospec(WorkflowDriver, instance=True))[1],
    )

    async def _noop_spawn(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(wf_tool_mod, "spawn_task_writer", _noop_spawn)
    monkeypatch.setattr(
        "src.config.settings.get_workflow_orchestration_config",
        lambda: WorkflowOrchestrationConfig(),
    )
    monkeypatch.setattr(
        "src.dalton.workflows.as_of.resolve_launch_as_of",
        lambda _b, _n, p, _j: asyncio.sleep(0, result=p),
    )
    monkeypatch.setattr(
        "src.dalton.workflows.subject_kind.resolve_launch_subject_kind",
        lambda _n, p, _c: asyncio.sleep(0, result=p),
    )
    monkeypatch.setattr(
        "src.dalton.workflows.company_routing.resolve_launch_company_routing",
        lambda *_a, **_kw: asyncio.sleep(0, result=None),
    )
    monkeypatch.setattr(
        wf_tool_mod,
        "get_config",
        lambda: {"metadata": {"run_id": "turn-r9"}, "configurable": {"user_id": "u-1", "workspace_id": "ws-r9-2"}},
    )

    def _make_tool(thread_id: str, short_id: str) -> Any:
        mw = create_autospec(BackgroundSubagentMiddleware, instance=True)
        mw.registry = BackgroundTaskRegistry()
        mw.checkpointer = object()

        async def _admit(task: Any, **_kw: Any) -> str:
            return ""

        async def _abort(task: Any, _err: BaseException) -> None:
            task.mark_cancelled("aborted before spawn")

        mw.admit_task_run.side_effect = _admit
        mw._abort_admitted_run.side_effect = _abort
        dispatcher = create_autospec(SubagentDispatcher, instance=True)
        dispatcher.middleware = mw
        dispatcher.registry = mw.registry
        dispatcher.subagent_types = ["general-purpose"]
        return wf_tool_mod.create_run_workflow_tool(
            dispatcher=dispatcher,
            backend=backend,
            thread_id=thread_id,
            short_thread_id=short_id,
            store=None,
            user_id="u-1",
            prebuilt_workflows=prebuilt,
        )

    tool = _make_tool("thr-startup-cancel", "stca1111")
    for artifact_name in ("workflow.js", "args.json"):
        cancel_on_artifact["target"] = artifact_name
        with pytest.raises(asyncio.CancelledError):
            await tool.coroutine(
                workflow="company-model",
                params={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
                tool_call_id=f"call-cancel-{artifact_name}",
            )
        lock_doc = json.loads((ws / mlock.MODELING_LOCK_PATH).read_text(encoding="utf-8"))
        assert lock_doc["status"] == "released", f"lock leaked when cancelled saving {artifact_name}"

    # Subsequent launch succeeds immediately after startup cancel!
    cancel_on_artifact["target"] = None
    ok_reply = await tool.coroutine(
        workflow="company-model",
        params={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
        tool_call_id="call-after-startup-cancel",
    )
    assert mlock.MODELING_LOCK_BUSY_MESSAGE not in str(ok_reply)
    assert await mlock.release_modeling_lock(
        backend,
        run_id=captured_specs[-1].run_task.task_id,
        workflow="company-model",
        workspace_id="ws-r9-2",
    )

    # 3. Heartbeat renews lease while driver is running!
    monkeypatch.setattr(mlock, "DEFAULT_MODELING_LOCK_TTL_SECONDS", 1.5)
    monkeypatch.setattr(mlock, "DEFAULT_MODELING_LOCK_HEARTBEAT_SECONDS", 0.06)
    monkeypatch.setattr(mlock, "DEFAULT_WRITER_EXIT_TIMEOUT_SECONDS", 0.06)

    cm_wait_js = (
        "export const meta = { name: 'company-model', description: 'cm' }\n"
        "await agent('write workbook', { label: '模型构建与落盘' })\n"
        "return { ok: true }\n"
    )
    cm_wait_meta = compile_check(cm_wait_js)

    hb_entered = asyncio.Event()
    hb_release = asyncio.Event()

    def _make_driver_with_child(
        run_id_str: str,
        *,
        on_entered: asyncio.Event,
        slow_unwind_event: asyncio.Event | None = None,
        finish_event: asyncio.Event | None = None,
    ) -> tuple[WorkflowDriver, BackgroundTaskRegistry]:
        reg = BackgroundTaskRegistry()

        class _Disp:
            subagent_types = frozenset({"general-purpose"})

            async def dispatch(
                self,
                *,
                subagent_type: str,
                description: str,
                prompt: str,
                run_id: str | None = None,
                owner_task_id: str | None = None,
                base_configurable: dict[str, Any] | None = None,
                time_limit: float | None = None,
                **kwargs: Any,
            ) -> Any:
                t = await reg.register(
                    tool_call_id=f"{run_id_str}-child",
                    description=description,
                    prompt=prompt,
                    subagent_type=subagent_type,
                    run_id=run_id,
                    owner_task_id=owner_task_id,
                )
                t.per_call_records = []

                async def _work() -> str:
                    on_entered.set()
                    try:
                        if finish_event is not None:
                            await finish_event.wait()
                        else:
                            await asyncio.sleep(30.0)
                    except asyncio.CancelledError:
                        if slow_unwind_event is not None:
                            # Simulate a delayed stop where the underlying writer is still unwinding!
                            await asyncio.shield(slow_unwind_event.wait())
                        raise
                    t.terminal_status = "completed"
                    return "ok"

                t.asyncio_task = asyncio.create_task(_work())
                return t

        rt = _NS(
            task_id=run_id_str,
            display_id=run_id_str,
            task_run_id=None,
            spawned_run_id=run_id_str,
            terminal_status=None,
            stop_at=None,
            keep_straggler=lambda _w: None,
        )
        drv = WorkflowDriver(
            WorkflowRunSpec(
                run_task=rt,
                registry=reg,
                dispatcher=_Disp(),
                backend=backend,
                thread_id=f"thr-{run_id_str}",
                short_thread_id=run_id_str[:8],
                script=cm_wait_js,
                script_args={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
                meta=cm_wait_meta,
                source="prebuilt",
                base_configurable={"workspace_id": "ws-r9-2"},
                caps=WorkflowOrchestrationConfig(enabled=True, child_timeout=10.0, run_timeout=30.0),
            )
        )
        return drv, reg

    drv_hb, _ = _make_driver_with_child("run-r9-hb", on_entered=hb_entered, finish_event=hb_release)
    hb_task = asyncio.create_task(drv_hb.run())
    await hb_entered.wait()
    initial_lock = json.loads((ws / mlock.MODELING_LOCK_PATH).read_text(encoding="utf-8"))
    await asyncio.sleep(0.18)
    renewed_lock = json.loads((ws / mlock.MODELING_LOCK_PATH).read_text(encoding="utf-8"))
    assert renewed_lock["status"] == "locked" and renewed_lock["run_id"] == "run-r9-hb"
    assert renewed_lock.get("renewed_at") is not None
    assert renewed_lock["expires_at"] > initial_lock["expires_at"]
    hb_release.set()
    await hb_task
    assert json.loads((ws / mlock.MODELING_LOCK_PATH).read_text(encoding="utf-8"))["status"] == "released"

    # 4a. Delayed stop (in-process writer slow to unwind): cancel driver -> lock NOT released ->
    #     new entry REFUSED while lease holds -> succeeds once short lease expires!
    slow_entered = asyncio.Event()
    allow_unwind = asyncio.Event()
    drv_slow, _ = _make_driver_with_child(
        "run-r9-slow-stop",
        on_entered=slow_entered,
        slow_unwind_event=allow_unwind,
    )
    slow_task = asyncio.create_task(drv_slow.run())
    await slow_entered.wait()
    slow_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await slow_task

    # Driver finished cancelling, but writer unwind timed out -> lock MUST remain held!
    lock_held_after_cancel = json.loads((ws / mlock.MODELING_LOCK_PATH).read_text(encoding="utf-8"))
    assert lock_held_after_cancel["status"] == "locked"
    assert lock_held_after_cancel["run_id"] == "run-r9-slow-stop"

    # New modeling entry is REFUSED while lease has not expired!
    refused_during_delay = await tool.coroutine(
        workflow="company-model",
        params={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
        tool_call_id="call-during-delayed-stop",
    )
    assert mlock.MODELING_LOCK_BUSY_MESSAGE in str(refused_during_delay)

    # Wait for the short lease to expire -> heartbeat was stopped, so new entry now SUCCEEDS!
    wait_for_expiry = max(lock_held_after_cancel["expires_at"] - time.time() + 0.05, 0.05)
    await asyncio.sleep(wait_for_expiry)
    allow_unwind.set()

    ok_after_short_lease = await tool.coroutine(
        workflow="company-model",
        params={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
        tool_call_id="call-after-short-lease-expiry",
    )
    assert mlock.MODELING_LOCK_BUSY_MESSAGE not in str(ok_after_short_lease)
    assert await mlock.release_modeling_lock(
        backend,
        run_id=captured_specs[-1].run_task.task_id,
        workflow="company-model",
        workspace_id="ws-r9-2",
    )

    # 4b. Delayed sandbox process stop: asyncio child unwinds immediately, but sandbox
    #     probe still reports __MODEL_WRITER_ALIVE__ -> lock stays held until lease expires!
    async def _probe_still_alive(_cmd: str) -> Any:
        return _NS(exit_code=0, output=mlock._MODEL_WRITER_ALIVE_SENTINEL)

    backend.aexecute.side_effect = _probe_still_alive
    sbx_entered = asyncio.Event()
    drv_sbx, _ = _make_driver_with_child("run-r9-sbx-alive", on_entered=sbx_entered)
    sbx_task = asyncio.create_task(drv_sbx.run())
    await sbx_entered.wait()
    sbx_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sbx_task

    sbx_lock = json.loads((ws / mlock.MODELING_LOCK_PATH).read_text(encoding="utf-8"))
    assert sbx_lock["status"] == "locked" and sbx_lock["run_id"] == "run-r9-sbx-alive"
    refused_sbx = await tool.coroutine(
        workflow="company-model",
        params={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
        tool_call_id="call-during-sbx-alive",
    )
    assert mlock.MODELING_LOCK_BUSY_MESSAGE in str(refused_sbx)
    await asyncio.sleep(max(sbx_lock["expires_at"] - time.time() + 0.05, 0.05))
    ok_sbx_after_expiry = await tool.coroutine(
        workflow="company-model",
        params={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
        tool_call_id="call-after-sbx-expiry",
    )
    assert mlock.MODELING_LOCK_BUSY_MESSAGE not in str(ok_sbx_after_expiry)


@pytest.mark.asyncio
async def test_p0b_r9_3_modeling_lock_advisory_fail_rules_and_run_id_holder_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9 #3 (Grok 中 & 疑似):
    1) When the DB inter-process advisory lock cannot be obtained (connection error or pool
       exhaustion), ``release_modeling_lock`` and ``renew_modeling_lock`` do NOT write the
       lock file unlocked (leaving the short lease to expire);
    2) When importing ``get_db_connection`` fails on the acquire side, ``acquire_modeling_lock``
       fails closed with ``ModelingLockBusy`` instead of falling back to an unlocked write;
    3) ``release_modeling_lock`` respects ``expires_at`` and refuses to overwrite an expired lock;
    4) Same ``tool_call_id`` with a DIFFERENT ``run_id`` is rejected as another holder; only
       an identical ``run_id`` counts as the same holder."""
    import builtins
    from contextlib import asynccontextmanager
    from types import SimpleNamespace as _NS

    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from src.dalton.workflows import modeling_lock as mlock
    from src.server.database import pool as db_pool

    ws = tmp_path / "ws_r9_lock_rules"
    ws.mkdir(parents=True, exist_ok=True)
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = str(ws)
    writes: list[str] = []

    async def _aread(path: str) -> str | None:
        p = ws / path
        return p.read_text(encoding="utf-8") if p.is_file() else None

    async def _awrite(path: str, text: str) -> bool:
        writes.append(path)
        p = ws / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return True

    backend.aread_text.side_effect = _aread
    backend.awrite_text.side_effect = _awrite

    # 1. Same tool_call_id with DIFFERENT run_id must be REJECTED; same run_id refreshes!
    now_clock = {"t": 1000.0}
    await mlock.acquire_modeling_lock(
        backend,
        run_id="run-owner-1",
        workflow="company-model",
        tool_call_id="shared-call-1",
        workspace_id="ws-r9-3",
        ttl_seconds=600.0,
        now_fn=lambda: now_clock["t"],
    )
    with pytest.raises(mlock.ModelingLockBusy):
        await mlock.acquire_modeling_lock(
            backend,
            run_id="run-intruder-2",
            workflow="company-model",
            tool_call_id="shared-call-1",
            workspace_id="ws-r9-3",
            ttl_seconds=600.0,
            now_fn=lambda: now_clock["t"] + 10.0,
        )
    lock_doc = json.loads((ws / mlock.MODELING_LOCK_PATH).read_text(encoding="utf-8"))
    assert lock_doc["run_id"] == "run-owner-1"

    # Same run_id is accepted as the same holder
    refreshed = await mlock.acquire_modeling_lock(
        backend,
        run_id="run-owner-1",
        workflow="company-model",
        tool_call_id="shared-call-1",
        workspace_id="ws-r9-3",
        ttl_seconds=600.0,
        now_fn=lambda: now_clock["t"] + 20.0,
    )
    assert refreshed["run_id"] == "run-owner-1" and refreshed["expires_at"] == 1620.0

    # 2. Release refuses to write after expires_at has passed
    writes.clear()
    assert (
        await mlock.release_modeling_lock(
            backend,
            run_id="run-owner-1",
            workflow="company-model",
            workspace_id="ws-r9-3",
            now_fn=lambda: 1620.0,
        )
        is False
    )
    assert writes == []

    # 3. With active run_ledger (multi-worker mode): DB advisory lock failure during
    #    release / renew MUST NOT write the lock file unlocked!
    ledger_reg = _NS(run_ledger=object())

    @asynccontextmanager
    async def _broken_db_conn():
        raise ConnectionError("pg pool exhausted")
        yield  # pragma: no cover

    monkeypatch.setattr(db_pool, "get_db_connection", _broken_db_conn)
    writes.clear()
    assert (
        await mlock.release_modeling_lock(
            backend,
            registry=ledger_reg,
            run_id="run-owner-1",
            workflow="company-model",
            workspace_id="ws-r9-3",
            now_fn=lambda: 1050.0,
        )
        is False
    )
    assert (
        await mlock.renew_modeling_lock(
            backend,
            registry=ledger_reg,
            run_id="run-owner-1",
            workflow="company-model",
            workspace_id="ws-r9-3",
            now_fn=lambda: 1050.0,
        )
        is False
    )
    assert writes == [], "release/renew must never write lock file when advisory lock is unavailable"
    still_locked = json.loads((ws / mlock.MODELING_LOCK_PATH).read_text(encoding="utf-8"))
    assert still_locked["status"] == "locked" and still_locked["run_id"] == "run-owner-1"

    # 4. Import failure of get_db_connection on acquire MUST fail closed with ModelingLockBusy!
    real_import = builtins.__import__

    def _fail_pool_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "src.server.database.pool":
            raise ImportError("simulated pool import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_pool_import)
    writes.clear()
    with pytest.raises(mlock.ModelingLockBusy):
        await mlock.acquire_modeling_lock(
            backend,
            registry=ledger_reg,
            run_id="run-import-fail",
            workflow="company-model",
            workspace_id="ws-r9-3",
            now_fn=lambda: 2000.0,
        )
    assert writes == []


@pytest.mark.asyncio
async def test_p0b_r9_4_snapshot_receipt_crash_between_writes_and_resume_chain_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9 #4 (Codex P1 + Grok 低):
    1) ``write-snapshot`` stages a durable transition record BEFORE replacing the workbook
       and does NOT swallow exceptions if committing the publish receipt fails;
    2) Interrupting between ``os.replace(editing_copy, mpath)`` and ``os.replace(tmp_rcpt, pub_rcpt)``
       leaves the workbook on disk with the new SHA while ``pub_rcpt`` still carries the pre-snapshot
       SHA + retryable ``pending_transition`` (and ``consensus-snapshot-receipt.json`` records the
       transition);
    3) Resuming after that crash replays Stage 2a and verifies the publish receipt via the recorded
       transition (both WITH and WITHOUT ``pending_transition``), retrying ``write-snapshot`` cleanly
       commits the receipt, and external tampering outside the transition chain is still rejected."""
    import asyncio
    import os as _os
    from types import SimpleNamespace as _NS

    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.middleware.background_subagent.registry import BackgroundTaskRegistry
    from ptc_agent.agent.middleware.background_subagent.workflow.driver import (
        WorkflowDriver,
        WorkflowRunSpec,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import compile_check
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )
    from src.config.models import WorkflowOrchestrationConfig

    ws = tmp_path / "ws_r9_snapshot_crash"
    ws.mkdir(parents=True, exist_ok=True)
    r_rel = "research/2026-11-27-model-PDD"
    dump_rel = f"{r_rel}/model_dump"
    base_rel = "model/PDD-model-v01.xlsx"
    cand_rel = "model/PDD-model-v02.xlsx"
    fp_rel = f"{dump_rel}/PDD-fingerprint.json"
    cm_rel = f"{dump_rel}/coord_map.json"
    cons_rel = "data/consensus/2026-11-27_1000.json"

    base_p = ws / base_rel
    base_p.parent.mkdir(parents=True, exist_ok=True)
    base_sha = _write_simple_valid_model(base_p)
    cand_p = ws / cand_rel
    cm_p = ws / cm_rel
    cons_p = ws / cons_rel
    cons_p.parent.mkdir(parents=True, exist_ok=True)
    cons_p.write_text(
        json.dumps(
            {
                "ticker": "PDD",
                "as_of_date": "2026-11-27",
                "source_type": "Market Consensus",
                "items": [{"period": "2026E", "metric": "Total Revenue", "consensus": 1125.0}],
            }
        ),
        encoding="utf-8",
    )
    skills_dst = ws / ".agents" / "skills" / "dalton-model" / "scripts"
    skills_dst.parent.mkdir(parents=True, exist_ok=True)
    if not skills_dst.exists():
        skills_dst.symlink_to(REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "scripts")

    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = str(ws)

    async def _aread(path: str) -> str | None:
        p = ws / path
        return p.read_text(encoding="utf-8") if p.is_file() else None

    async def _awrite(path: str, text: str) -> bool:
        p = ws / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return True

    async def _aglob(pattern: str, _root: str = ".") -> list[str]:
        return sorted(str(p.relative_to(ws)) for p in ws.glob(pattern))

    async def _aexec(cmd: str) -> Any:
        proc = subprocess.run(["bash", "-c", cmd], cwd=ws, capture_output=True, text=True, check=False)
        return _NS(exit_code=proc.returncode, output=proc.stdout)

    backend.aread_text.side_effect = _aread
    backend.awrite_text.side_effect = _awrite
    backend.aglob_paths.side_effect = _aglob
    backend.aexecute.side_effect = _aexec

    def _run_real_probe(prompt: str) -> dict[str, Any]:
        m = re.search(r"```bash\n(.*?)\n```", prompt, re.DOTALL)
        assert m, prompt
        cmd = m.group(1).replace("python3 ", f"{sys.executable} ")
        proc = subprocess.run(["bash", "-c", cmd], cwd=ws, capture_output=True, text=True, check=False)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        return json.loads(proc.stdout.strip())

    wf_script = read_assembled_workflow_file(REPO_ROOT / "workflows" / "company-model" / "workflow.js")
    wf_meta = compile_check(wf_script)

    def _make_driver(
        run_id_str: str,
        handler: Any,
        *,
        journal_source: str | None = None,
    ) -> tuple[WorkflowDriver, list[str]]:
        dispatched: list[str] = []
        reg = BackgroundTaskRegistry()

        class _Disp:
            subagent_types = frozenset({"general-purpose", "dalton-evidence"})

            async def dispatch(
                self,
                *,
                subagent_type: str,
                description: str,
                prompt: str,
                run_id: str | None = None,
                owner_task_id: str | None = None,
                base_configurable: dict[str, Any] | None = None,
                time_limit: float | None = None,
                **kwargs: Any,
            ) -> Any:
                dispatched.append(description)
                t = await reg.register(
                    tool_call_id=f"{run_id_str}-{len(dispatched)}",
                    description=description,
                    prompt=prompt[:200],
                    subagent_type=subagent_type,
                    run_id=run_id,
                    owner_task_id=owner_task_id,
                )
                t.per_call_records = []

                async def _work() -> str:
                    res = handler(description, prompt)
                    if isinstance(res, Exception):
                        t.terminal_status = "error"
                        raise res
                    t.terminal_status = "completed"
                    return json.dumps(res, ensure_ascii=False)

                t.asyncio_task = asyncio.create_task(_work())
                return t

        rt = _NS(
            task_id=run_id_str,
            display_id=run_id_str,
            task_run_id=None,
            spawned_run_id=run_id_str,
            terminal_status=None,
            stop_at=None,
            keep_straggler=lambda _w: None,
        )
        params = {
            "ticker": "PDD",
            "company": "PDD Holdings",
            "workspace_dir": ".",
            "as_of": "2026-11-27",
            "mode": "update",
            "model_path": base_rel,
            "progress_path": f"{r_rel}/.progress.json",
        }
        return (
            WorkflowDriver(
                WorkflowRunSpec(
                    run_task=rt,
                    registry=reg,
                    dispatcher=_Disp(),
                    backend=backend,
                    thread_id="thr-r9-snap",
                    short_thread_id="9999aaaa",
                    script=wf_script,
                    script_args=params,
                    meta=wf_meta,
                    source="prebuilt",
                    base_configurable={"workspace_id": "ws-r9-4"},
                    caps=WorkflowOrchestrationConfig(enabled=True, child_timeout=30.0, run_timeout=60.0),
                    journal_source=journal_source,
                    progress_path=f"{r_rel}/.progress.json",
                )
            ),
            dispatched,
        )

    pre_snap_sha_holder: dict[str, str] = {}
    post_snap_sha_holder: dict[str, str] = {}
    real_replace = _os.replace
    pub_rcpt_p = medit.publish_receipt_path(cand_p)

    # Run 1: Stage 2a publishes v02, then Stage 2b's write-snapshot crashes RIGHT BETWEEN
    # os.replace(editing_copy, cand_p) and os.replace(tmp_rcpt, pub_rcpt_p)!
    def _h_r1(lbl: str, prompt: str) -> Any:
        if lbl == "建模准备":
            fp_obj = mfp.build_fingerprint(base_p, ticker="PDD")
            (ws / fp_rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / fp_rel).write_text(json.dumps(fp_obj, indent=2), encoding="utf-8")
            return {
                "files": [fp_rel],
                "summary": "prep",
                "mode": "update",
                "baseline_path": base_rel,
                "baseline_sha256": base_sha,
                "fingerprint_path": fp_rel,
                "consensus_path": cons_rel,
                "planned_output_path": cand_rel,
                "baseline_health_warning": None,
            }
        if lbl == "模型构建与落盘":
            ed = medit.apply_edit_plan(
                baseline_path=base_p,
                ticker="PDD",
                plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1160}]},
                output_path=cand_p,
                workspace_dir=ws,
                fingerprint=json.loads((ws / fp_rel).read_text(encoding="utf-8")),
                coord_map_out=cm_p,
                skip_recalc=True,
                mode="update",
                run_id="run-r9-snap1",
            )
            pre_snap_sha_holder["sha"] = ed["candidate_sha256"]
            return {
                "files": [cand_rel, cm_rel],
                "summary": "v02 built",
                "candidate_path": cand_rel,
                "model_sha256": ed["candidate_sha256"],
                "recalc_status": "ok",
            }
        if lbl == "核验候选落盘":
            return _run_real_probe(prompt)
        if lbl == "一致预期对照":
            def _crash_on_final_rcpt_commit(src: Any, dst: Any) -> None:
                if Path(dst).resolve() == pub_rcpt_p.resolve() and not str(src).endswith(".pre.tmp"):
                    raise OSError("simulated crash between workbook replace and receipt commit")
                real_replace(src, dst)

            with monkeypatch.context() as mp:
                mp.setattr(mcons.os, "replace", _crash_on_final_rcpt_commit)
                with pytest.raises(OSError, match="simulated crash between workbook replace and receipt commit"):
                    mcons.main(["write-snapshot", "--model", str(cand_p), "--consensus", str(cons_p), "--coord-map", str(cm_p)])
            post_snap_sha_holder["sha"] = mfp.file_sha256(cand_p)
            return RuntimeError("interrupted after partial snapshot commit")
        if lbl in ("模型本轮范围检查", "写入降级账本"):
            return RuntimeError("interrupted")
        raise AssertionError(lbl)

    d1, _ = _make_driver("run-r9-snap1", _h_r1)
    await d1.run()

    pre_sha = pre_snap_sha_holder["sha"]
    post_sha = post_snap_sha_holder["sha"]
    assert post_sha != pre_sha
    rcpt_after_crash = json.loads(pub_rcpt_p.read_text(encoding="utf-8"))
    assert rcpt_after_crash["run_id"] == "run-r9-snap1"
    assert rcpt_after_crash["candidate_sha256"] == pre_sha
    assert rcpt_after_crash["pending_transition"]["pre_model_sha256"] == pre_sha
    assert rcpt_after_crash["pending_transition"]["post_model_sha256"] == post_sha

    # Subcase A: Also verify resume probe works even when pub_rcpt has ONLY old pre_sha
    # (no pending_transition), purely via consensus-snapshot-receipt.json transition chain!
    rcpt_without_pt = dict(rcpt_after_crash)
    rcpt_without_pt.pop("pending_transition", None)
    pub_rcpt_p.write_text(json.dumps(rcpt_without_pt, indent=2), encoding="utf-8")

    probes_no_pt: list[dict[str, Any]] = []

    def _h_probe_only(lbl: str, prompt: str) -> Any:
        if lbl == "模型构建与落盘":
            raise AssertionError("Must replay Stage 2a via consensus-snapshot-receipt.json")
        if lbl == "核验候选落盘":
            out = _run_real_probe(prompt)
            probes_no_pt.append(out)
            return out
        return RuntimeError("stop after probe")

    d_no_pt, disp_no_pt = _make_driver(
        "run-r9-snap-nopt",
        _h_probe_only,
        journal_source=".agents/threads/9999aaaa/workflows/run-r9-snap1",
    )
    await d_no_pt.run()
    assert "模型构建与落盘" not in disp_no_pt
    assert len(probes_no_pt) == 1
    assert probes_no_pt[0]["run_matched"] is True and probes_no_pt[0]["model_sha256"] == post_sha

    # Restore pending_transition to test full resume + retryable write-snapshot commit + delivery!
    pub_rcpt_p.write_text(json.dumps(rcpt_after_crash, indent=2), encoding="utf-8")
    probes_r2: list[dict[str, Any]] = []

    def _h_r2(lbl: str, prompt: str) -> Any:
        if lbl == "模型构建与落盘":
            raise AssertionError("Run 2 must replay Stage 2a")
        if lbl == "核验候选落盘":
            out = _run_real_probe(prompt)
            probes_r2.append(out)
            return out
        if lbl == "一致预期对照":
            # Retry write-snapshot with normal os.replace -> commits receipt and clears pending_transition!
            assert mcons.main(["write-snapshot", "--model", str(cand_p), "--consensus", str(cons_p), "--coord-map", str(cm_p)]) == 0
            assert mcons.main(["diff", "--model", str(cand_p), "--ticker", "PDD", "--research-dir", str(ws / r_rel), "--consensus", str(cons_p)]) == 0
            return {
                "files": [cand_rel, cm_rel, f"{dump_rel}/consensus-diff.md", f"{dump_rel}/consensus-diff.json"],
                "summary": "cons retried ok",
                "candidate_path": cand_rel,
                "model_sha256": mfp.file_sha256(cand_p),
                "recalc_status": "ok",
                "consensus_status": "ok",
                "consensus_diff_generated": True,
            }
        if lbl == "模型本轮范围检查":
            chk_res = mchk.write_check_and_ledger(
                workspace_dir=ws,
                ticker="PDD",
                research_dir=ws / r_rel,
                candidate_path=cand_p,
                baseline_path=base_p,
                fingerprint_path=ws / fp_rel,
                coord_map_path=cm_p,
                run_id="run-r9-snap2",
                workflow="company-model",
                mode="update",
            )
            files = [f"{dump_rel}/model-check.md", f"{dump_rel}/model-verification.json"]
            if chk_res.get("remaining_issues_path"):
                files.append(f"{dump_rel}/remaining-issues.md")
            return {
                "files": files,
                "summary": "checked",
                "readable": chk_res["readable"],
                "final_status": chk_res["final_status"],
                "model_sha256": chk_res["candidate_sha256"],
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": f"{dump_rel}/remaining-issues.md" if chk_res.get("remaining_issues_path") else None,
            }
        raise AssertionError(lbl)

    d2, disp_r2 = _make_driver(
        "run-r9-snap2",
        _h_r2,
        journal_source=".agents/threads/9999aaaa/workflows/run-r9-snap1",
    )
    await d2.run()
    assert "模型构建与落盘" not in disp_r2
    assert len(probes_r2) == 1 and probes_r2[0]["run_matched"] is True
    rcpt_committed = json.loads(pub_rcpt_p.read_text(encoding="utf-8"))
    assert rcpt_committed["run_id"] == "run-r9-snap1"
    assert rcpt_committed["candidate_sha256"] == mfp.file_sha256(cand_p)
    assert "pending_transition" not in rcpt_committed
    res2 = json.loads((ws / ".agents/threads/9999aaaa/workflows/run-r9-snap2/result.json").read_text(encoding="utf-8"))
    assert res2["ok"] is True and res2["candidate_path"] == cand_rel
    vd2 = mchk.verify_done(
        workspace_dir=ws,
        ticker="PDD",
        research_dir=ws / r_rel,
        expected_run_id="run-r9-snap2",
        expected_workflow="company-model",
    )
    assert vd2["ok"] is True, vd2["errors"]

    # Subcase B: Tampering with cand_p outside the recorded transition chain is STILL rejected!
    wb_tamper = openpyxl.load_workbook(cand_p)
    wb_tamper["Model - PDD"]["E2"] = 999999
    wb_tamper.save(cand_p)
    wb_tamper.close()
    tamper_probes: list[dict[str, Any]] = []

    def _h_tamper(lbl: str, prompt: str) -> Any:
        if lbl == "模型构建与落盘":
            return {
                "files": [cand_rel, cm_rel],
                "summary": "claimed ok",
                "candidate_path": cand_rel,
                "model_sha256": mfp.file_sha256(cand_p),
                "recalc_status": "ok",
            }
        if lbl == "核验候选落盘":
            out = _run_real_probe(prompt)
            tamper_probes.append(out)
            return out
        raise AssertionError(f"Tampered workbook must stop before {lbl}")

    d_tamper, _ = _make_driver(
        "run-r9-tamper",
        _h_tamper,
        journal_source=".agents/threads/9999aaaa/workflows/run-r9-snap1",
    )
    await d_tamper.run()
    assert len(tamper_probes) == 1 and tamper_probes[0]["run_matched"] is False
    res_tamper = json.loads((ws / ".agents/threads/9999aaaa/workflows/run-r9-tamper/result.json").read_text(encoding="utf-8"))
    assert res_tamper["ok"] is False and res_tamper["status"] == "failed_unusable"


@pytest.mark.asyncio
async def test_p0b_r10_1_nomodel_progress_path_single_resolution_from_tool_to_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R10 #1 (Grok 高):
    1) ``resolve_workflow_progress_path`` is idempotent for ``.progress.no-model.json``;
    2) Only one call site remains between ``tool.py`` and ``driver.py``;
    3) Launching non-modeled ``earnings-deep-update`` end-to-end through
       ``create_run_workflow_tool`` into the real ``WorkflowDriver`` writes
       ``.progress.no-model.json`` (never ``.progress.no-model.json.no-model``);
    4) Skill docs, closing hint, and non-modeled workflow ``resume_hint`` reference
       ``.progress.no-model.json``."""
    import asyncio

    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.middleware.background_subagent.dispatch import SubagentDispatcher
    from ptc_agent.agent.middleware.background_subagent.middleware import BackgroundSubagentMiddleware
    from ptc_agent.agent.middleware.background_subagent.registry import BackgroundTaskRegistry
    from ptc_agent.agent.middleware.background_subagent.workflow import tool as wf_tool_mod
    from ptc_agent.agent.middleware.background_subagent.workflow.driver import WorkflowDriver
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        PrebuiltWorkflowRegistry,
        read_assembled_workflow_file,
    )
    from src.config.models import WorkflowOrchestrationConfig
    from src.dalton.workflows import modeling_lock as mlock

    # 1. Idempotency of resolve_workflow_progress_path
    base_prog = "research/2026-11-27-deep-FY26Q3/.progress.json"
    nomodel_prog = "research/2026-11-27-deep-FY26Q3/.progress.no-model.json"
    once = mlock.resolve_workflow_progress_path("earnings-deep-update", {"model_path": None}, base_prog)
    twice = mlock.resolve_workflow_progress_path("earnings-deep-update", {"model_path": None}, once)
    assert once == nomodel_prog
    assert twice == nomodel_prog
    assert mlock.resolve_workflow_progress_path("earnings-deep-update", {"model_path": None}, ".progress.no-model.json") == ".progress.no-model.json"
    assert mlock.resolve_workflow_progress_path("earnings-deep-update", {"model_path": "model/PDD-model-v01.xlsx"}, base_prog) == base_prog

    # 2. End-to-end from create_run_workflow_tool -> real WorkflowDriver -> disk write
    ws = tmp_path / "ws_r10_nomodel_prog"
    ws.mkdir(parents=True, exist_ok=True)
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = str(ws)

    async def _aread(path: str) -> str | None:
        p = ws / path
        return p.read_text(encoding="utf-8") if p.is_file() else None

    async def _awrite(path: str, text: str) -> bool:
        p = ws / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return True

    backend.aread_text.side_effect = _aread
    backend.awrite_text.side_effect = _awrite

    deep_js = (
        "export const meta = { name: 'earnings-deep-update', description: 'deep' }\n"
        "await agent('write draft', { label: '八节点评' })\n"
        "return { ok: true }\n"
    )
    prebuilt = create_autospec(PrebuiltWorkflowRegistry, instance=True)
    prebuilt.get.side_effect = {"earnings-deep-update": deep_js}.get
    prebuilt.names.side_effect = lambda: ["earnings-deep-update"]

    created_drivers: list[WorkflowDriver] = []
    real_driver_cls = WorkflowDriver

    def _capture_driver(spec: Any) -> WorkflowDriver:
        drv = real_driver_cls(spec)
        created_drivers.append(drv)
        return drv

    spawned_tasks: list[asyncio.Task[Any]] = []

    async def _run_driver_spawn(_mw: Any, _run_task: Any, coro_fn: Any, **_kw: Any) -> None:
        task = asyncio.create_task(coro_fn())
        spawned_tasks.append(task)

    monkeypatch.setattr(wf_tool_mod, "WorkflowDriver", _capture_driver)
    monkeypatch.setattr(wf_tool_mod, "spawn_task_writer", _run_driver_spawn)
    monkeypatch.setattr(
        "src.config.settings.get_workflow_orchestration_config",
        lambda: WorkflowOrchestrationConfig(enabled=True, child_timeout=15.0, run_timeout=30.0),
    )
    monkeypatch.setattr(
        "src.dalton.workflows.as_of.resolve_launch_as_of",
        lambda _b, _n, p, _j: asyncio.sleep(0, result=p),
    )
    monkeypatch.setattr(
        "src.dalton.workflows.subject_kind.resolve_launch_subject_kind",
        lambda _n, p, _c: asyncio.sleep(0, result=p),
    )
    monkeypatch.setattr(
        "src.dalton.workflows.company_routing.resolve_launch_company_routing",
        lambda *_a, **_kw: asyncio.sleep(0, result=None),
    )
    monkeypatch.setattr(
        wf_tool_mod,
        "get_config",
        lambda: {"metadata": {"run_id": "turn-r10-1"}, "configurable": {"user_id": "u-1", "workspace_id": "ws-r10-1"}},
    )

    mw = create_autospec(BackgroundSubagentMiddleware, instance=True)
    mw.registry = BackgroundTaskRegistry()
    mw.checkpointer = None

    async def _admit(*_a: Any, **_kw: Any) -> str:
        return ""

    async def _abort(task: Any, _err: BaseException) -> None:
        task.mark_cancelled("abort")

    mw.admit_task_run.side_effect = _admit
    mw._abort_admitted_run.side_effect = _abort

    dispatcher = create_autospec(SubagentDispatcher, instance=True)
    dispatcher.middleware = mw
    dispatcher.registry = mw.registry
    dispatcher.subagent_types = ["general-purpose"]

    async def _dispatch_child(
        *,
        subagent_type: str,
        description: str,
        prompt: str,
        run_id: str | None = None,
        owner_task_id: str | None = None,
        base_configurable: dict[str, Any] | None = None,
        time_limit: float | None = None,
        **kwargs: Any,
    ) -> Any:
        t = await mw.registry.register(
            tool_call_id=f"child-{len(created_drivers)}-{description}",
            description=description,
            prompt=prompt,
            subagent_type=subagent_type,
            run_id=run_id,
            owner_task_id=owner_task_id,
        )
        t.per_call_records = []

        async def _work() -> str:
            t.terminal_status = "completed"
            return "ok"

        t.asyncio_task = asyncio.create_task(_work())
        return t

    dispatcher.dispatch.side_effect = _dispatch_child

    tool = wf_tool_mod.create_run_workflow_tool(
        dispatcher=dispatcher,
        backend=backend,
        thread_id="thr-r10-nomodel",
        short_thread_id="r10nm111",
        store=None,
        user_id="u-1",
        prebuilt_workflows=prebuilt,
    )

    for idx, passed_prog in enumerate((base_prog, nomodel_prog), start=1):
        reply = await tool.coroutine(
            workflow="earnings-deep-update",
            params={
                "ticker": "PDD",
                "company": "PDD Holdings",
                "quarter": "FY26Q3",
                "workspace_dir": ".",
                "release_date": "2026-11-27",
                "model_path": None,
                "progress_path": passed_prog,
            },
            tool_call_id=f"call-r10-nomodel-{idx}",
        )
        assert "started" in str(reply).lower() or "workflow" in str(reply).lower()
        await spawned_tasks[-1]

        drv = created_drivers[-1]
        assert drv._journal.progress_path == nomodel_prog
        assert (ws / nomodel_prog).is_file()
        assert not (ws / f"{nomodel_prog}.no-model").exists()
        assert not (ws / base_prog).exists()
        prog_doc = json.loads((ws / nomodel_prog).read_text(encoding="utf-8"))
        assert prog_doc["run_id"] == drv.wf_task_id
        assert prog_doc["status"] == "completed"

    # 3. Skill docs and workflow script align on .progress.no-model.json
    skill_text = (REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-earnings-deep" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert ".progress.no-model.json" in skill_text
    real_deep_js = read_assembled_workflow_file(REPO_ROOT / "workflows" / "earnings-deep-update" / "workflow.js")
    assert ".progress.no-model.json" in real_deep_js


@pytest.mark.asyncio
async def test_p0b_r10_2_procps_free_writer_probe_real_shell_missing_binary_alive_and_exited(
    tmp_path: Path,
) -> None:
    """R10 #2 (Codex P1 + Grok 高):
    1) Sandbox writer probe does not depend on ``pgrep``/``procps`` and has no
       ``|| echo GONE`` fallback;
    2) Real shell runs cover:
       - missing binary / non-zero exit / unexpected output -> fail closed (False);
       - real running processes (``model_edit.py``, ``model_consensus.py``,
         ``model_check.py``, ``soffice``) -> alive (False, lock kept);
       - exited processes -> gone (True, lock may release);
       - ``/proc/*/cmdline`` directory scan matching all 4 writer types and
         respecting explicit ``--run-id`` isolation."""
    import os
    from types import SimpleNamespace as _NS

    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from src.dalton.workflows import modeling_lock as mlock

    assert "pgrep" not in mlock._MODEL_WRITER_PROBE_CMD
    assert "|| echo" not in mlock._MODEL_WRITER_PROBE_CMD

    def _make_shell_backend(env_override: dict[str, str] | None = None) -> Any:
        backend = create_autospec(SandboxBackend, instance=True)
        backend.workspace_dir = str(tmp_path)

        async def _aexec(cmd: str) -> Any:
            proc = subprocess.run(
                ["/bin/bash", "-c", cmd],
                cwd=tmp_path,
                env=env_override,
                capture_output=True,
                text=True,
                check=False,
            )
            return _NS(exit_code=proc.returncode, output=proc.stdout)

        backend.aexecute.side_effect = _aexec
        return backend

    # Case 1: Missing binary (explicit nonexistent binary AND empty PATH so python3 is absent)
    shell_backend = _make_shell_backend()
    missing_bin_cmd = mlock.build_model_writer_probe_cmd(
        run_id="run-r10-probe",
        python_bin="nonexistent_dalton_probe_bin_xyz",
    )
    assert (
        await mlock.confirm_modeling_writers_exited(
            registry=None,
            owner_task_id="run-r10-probe",
            run_id="run-r10-probe",
            backend=shell_backend,
            check_sandbox_processes=True,
            probe_cmd=missing_bin_cmd,
            timeout=0.05,
        )
        is False
    )

    empty_path_backend = _make_shell_backend(env_override={"PATH": ""})
    assert (
        await mlock.confirm_modeling_writers_exited(
            registry=None,
            owner_task_id="run-r10-probe",
            run_id="run-r10-probe",
            backend=empty_path_backend,
            check_sandbox_processes=True,
            probe_cmd=mlock.build_model_writer_probe_cmd(run_id="run-r10-probe"),
            timeout=0.05,
        )
        is False
    )

    # Non-zero exit (unreadable/missing proc_root) & unexpected stdout both fail closed
    bad_proc_cmd = mlock.build_model_writer_probe_cmd(
        run_id="run-r10-probe",
        python_bin=sys.executable,
        proc_root=str(tmp_path / "no_such_proc_dir"),
    )
    assert (
        await mlock.confirm_modeling_writers_exited(
            registry=None,
            owner_task_id="run-r10-probe",
            run_id="run-r10-probe",
            backend=shell_backend,
            check_sandbox_processes=True,
            probe_cmd=bad_proc_cmd,
            timeout=0.05,
        )
        is False
    )
    assert (
        await mlock.confirm_modeling_writers_exited(
            registry=None,
            owner_task_id="run-r10-probe",
            run_id="run-r10-probe",
            backend=shell_backend,
            check_sandbox_processes=True,
            probe_cmd="echo unexpected_output_only",
            timeout=0.05,
        )
        is False
    )

    # Case 2: Real OS subprocesses — alive -> False; after exit -> True
    fake_bin_dir = tmp_path / "fake_writers"
    fake_bin_dir.mkdir(parents=True, exist_ok=True)
    sleeper_body = "import time\ntime.sleep(30)\n"
    for script_name in ("model_edit.py", "model_consensus.py", "model_check.py"):
        (fake_bin_dir / script_name).write_text(sleeper_body, encoding="utf-8")
    fake_soffice = fake_bin_dir / "soffice"
    fake_soffice.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    fake_soffice.chmod(0o755)

    real_probe_cmd = mlock.build_model_writer_probe_cmd(
        run_id="run-r10-live",
        python_bin=sys.executable,
    )

    spawn_cases = [
        [sys.executable, str(fake_bin_dir / "model_edit.py"), "apply", "--run-id", "run-r10-live"],
        [sys.executable, str(fake_bin_dir / "model_consensus.py"), "write-snapshot"],
        [sys.executable, str(fake_bin_dir / "model_check.py"), "check", "--run-id", "run-r10-live"],
        [str(fake_soffice), "--headless", "--convert-to", "xlsx"],
    ]
    for argv in spawn_cases:
        child = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            assert (
                await mlock.confirm_modeling_writers_exited(
                    registry=None,
                    owner_task_id="run-r10-live",
                    run_id="run-r10-live",
                    backend=shell_backend,
                    check_sandbox_processes=True,
                    probe_cmd=real_probe_cmd,
                    timeout=0.08,
                    poll_interval=0.02,
                )
                is False
            ), f"Expected ALIVE while {argv} is running"
        finally:
            child.terminate()
            child.wait(timeout=5)

        assert (
            await mlock.confirm_modeling_writers_exited(
                registry=None,
                owner_task_id="run-r10-live",
                run_id="run-r10-live",
                backend=shell_backend,
                check_sandbox_processes=True,
                probe_cmd=real_probe_cmd,
                timeout=0.2,
                poll_interval=0.02,
            )
            is True
        ), f"Expected GONE after {argv} exited"

    # Case 3: Real shell scan of /proc/*/cmdline layout (Linux sandbox path)
    fake_proc = tmp_path / "fake_proc"
    fake_proc.mkdir(parents=True, exist_ok=True)
    proc_probe_cmd = mlock.build_model_writer_probe_cmd(
        run_id="run-r10-proc",
        python_bin=sys.executable,
        proc_root=str(fake_proc),
    )

    # Another run's explicit --run-id does NOT block run-r10-proc
    other_pid_dir = fake_proc / "42001"
    other_pid_dir.mkdir(parents=True, exist_ok=True)
    (other_pid_dir / "cmdline").write_bytes(
        b"python3\x00.agents/skills/dalton-model/scripts/model_edit.py\x00apply\x00--run-id\x00run-other-999\x00"
    )
    assert (
        await mlock.confirm_modeling_writers_exited(
            registry=None,
            owner_task_id="run-r10-proc",
            run_id="run-r10-proc",
            backend=shell_backend,
            check_sandbox_processes=True,
            probe_cmd=proc_probe_cmd,
            timeout=0.1,
        )
        is True
    )

    # Each target writer in /proc/<pid>/cmdline reports ALIVE until removed
    proc_writer_entries = [
        ("42010", b"python3\x00.agents/skills/dalton-model/scripts/model_edit.py\x00--run-id=run-r10-proc\x00"),
        ("42011", b"python3\x00.agents/skills/dalton-model/scripts/model_consensus.py\x00write-snapshot\x00"),
        ("42012", b"python3\x00.agents/skills/dalton-model/scripts/model_check.py\x00--run-id\x00run-r10-proc\x00"),
        ("42013", b"/usr/lib/libreoffice/program/soffice.bin\x00--headless\x00--convert-to\x00xlsx\x00"),
    ]
    for pid_s, raw_cmd in proc_writer_entries:
        pdir = fake_proc / pid_s
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "cmdline").write_bytes(raw_cmd)
        assert (
            await mlock.confirm_modeling_writers_exited(
                registry=None,
                owner_task_id="run-r10-proc",
                run_id="run-r10-proc",
                backend=shell_backend,
                check_sandbox_processes=True,
                probe_cmd=proc_probe_cmd,
                timeout=0.05,
                poll_interval=0.01,
            )
            is False
        ), f"Expected /proc scan ALIVE for {raw_cmd!r}"
        os.unlink(pdir / "cmdline")
        pdir.rmdir()
        assert (
            await mlock.confirm_modeling_writers_exited(
                registry=None,
                owner_task_id="run-r10-proc",
                run_id="run-r10-proc",
                backend=shell_backend,
                check_sandbox_processes=True,
                probe_cmd=proc_probe_cmd,
                timeout=0.1,
            )
            is True
        )


@pytest.mark.asyncio
async def test_p0b_r10_3_heartbeat_renew_failure_stops_driver_and_publish_rechecks_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R10 #3 (Codex P1):
    1) When heartbeat lease renewal fails, ``WorkflowDriver`` immediately cancels
       in-flight writer child tasks, stops dispatching any further write steps,
       and fails the run;
    2) Before publishing/replacing a model workbook (``model_edit.apply_edit_plan``
       initial publish, recalc write-back, and ``model_consensus.write_consensus_snapshot``),
       lock ownership is re-verified and writes are refused once the lock is lost."""
    import asyncio
    import time
    from types import SimpleNamespace as _NS

    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.middleware.background_subagent.registry import BackgroundTaskRegistry
    from ptc_agent.agent.middleware.background_subagent.workflow.driver import (
        WorkflowDriver,
        WorkflowRunError,
        WorkflowRunSpec,
    )
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import compile_check
    from src.config.models import WorkflowOrchestrationConfig
    from src.dalton.workflows import modeling_lock as mlock

    # --- Part A: Heartbeat renew failure cancels active child and blocks next step ---
    monkeypatch.setattr(mlock, "DEFAULT_MODELING_LOCK_TTL_SECONDS", 2.0)
    monkeypatch.setattr(mlock, "DEFAULT_MODELING_LOCK_HEARTBEAT_SECONDS", 0.04)
    monkeypatch.setattr(mlock, "DEFAULT_WRITER_EXIT_TIMEOUT_SECONDS", 0.2)

    ws = tmp_path / "ws_r10_hb_loss"
    ws.mkdir(parents=True, exist_ok=True)
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = str(ws)

    fail_lock_writes = {"enabled": False}

    async def _aread(path: str) -> str | None:
        p = ws / path
        return p.read_text(encoding="utf-8") if p.is_file() else None

    async def _awrite(path: str, text: str) -> bool:
        if fail_lock_writes["enabled"] and path == mlock.MODELING_LOCK_PATH:
            return False
        p = ws / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return True

    async def _aexec_gone(_cmd: str) -> Any:
        return _NS(exit_code=0, output=mlock._MODEL_WRITER_GONE_SENTINEL)

    backend.aread_text.side_effect = _aread
    backend.awrite_text.side_effect = _awrite
    backend.aexecute.side_effect = _aexec_gone

    two_step_js = (
        "export const meta = { name: 'company-model', description: 'cm-hb' }\n"
        "try { await agent('step 1 write', { label: '模型构建与落盘' }) } catch (e) {}\n"
        "await agent('step 2 write', { label: '一致预期对照' })\n"
        "return { ok: true }\n"
    )
    two_step_meta = compile_check(two_step_js)

    reg = BackgroundTaskRegistry()
    step1_entered = asyncio.Event()
    step1_cancelled = asyncio.Event()
    dispatched_labels: list[str] = []

    class _Disp:
        subagent_types = frozenset({"general-purpose"})

        async def dispatch(
            self,
            *,
            subagent_type: str,
            description: str,
            prompt: str,
            run_id: str | None = None,
            owner_task_id: str | None = None,
            base_configurable: dict[str, Any] | None = None,
            time_limit: float | None = None,
            **kwargs: Any,
        ) -> Any:
            dispatched_labels.append(description)
            t = await reg.register(
                tool_call_id=f"hb-child-{len(dispatched_labels)}",
                description=description,
                prompt=prompt,
                subagent_type=subagent_type,
                run_id=run_id,
                owner_task_id=owner_task_id,
            )
            t.per_call_records = []

            async def _work() -> str:
                step1_entered.set()
                try:
                    await asyncio.sleep(30.0)
                except asyncio.CancelledError:
                    step1_cancelled.set()
                    raise
                t.terminal_status = "completed"
                return "ok"

            t.asyncio_task = asyncio.create_task(_work())
            return t

    rt = _NS(
        task_id="run-r10-hb-loss",
        display_id="run-r10-hb-loss",
        task_run_id=None,
        spawned_run_id="run-r10-hb-loss",
        terminal_status=None,
        stop_at=None,
        keep_straggler=lambda _w: None,
    )
    drv = WorkflowDriver(
        WorkflowRunSpec(
            run_task=rt,
            registry=reg,
            dispatcher=_Disp(),
            backend=backend,
            thread_id="thr-r10-hb",
            short_thread_id="r10hb111",
            script=two_step_js,
            script_args={"ticker": "PDD", "workspace_dir": ".", "as_of": "2026-11-27"},
            meta=two_step_meta,
            source="prebuilt",
            base_configurable={"workspace_id": "ws-r10-3"},
            caps=WorkflowOrchestrationConfig(enabled=True, child_timeout=15.0, run_timeout=30.0),
        )
    )

    run_task = asyncio.create_task(drv.run())
    await asyncio.wait_for(step1_entered.wait(), timeout=5.0)
    # Trigger heartbeat renewal failure while step 1 is in flight
    fail_lock_writes["enabled"] = True

    with pytest.raises(WorkflowRunError, match="续约失败"):
        await asyncio.wait_for(run_task, timeout=5.0)

    assert step1_cancelled.is_set(), "In-flight writer child must be cancelled when heartbeat renewal fails"
    assert dispatched_labels == ["模型构建与落盘"], f"Must not dispatch step 2 after lease loss: {dispatched_labels}"
    status_doc = json.loads((ws / ".agents/threads/r10hb111/workflows/run-r10-hb-loss/status.json").read_text(encoding="utf-8"))
    assert status_doc["status"] == "failed"

    # --- Part B: Publish-time lock ownership re-verification ---
    ws_pub = tmp_path / "ws_r10_pub_lock"
    ws_pub.mkdir(parents=True, exist_ok=True)
    base_p = ws_pub / "model" / "PDD-model-v01.xlsx"
    base_p.parent.mkdir(parents=True, exist_ok=True)
    _write_simple_valid_model(base_p)
    fp_obj = mfp.build_fingerprint(base_p, ticker="PDD")
    lock_file = ws_pub / mlock.MODELING_LOCK_PATH
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    # 1. Lock taken over by run-new -> run-old publish refused, file NOT created
    lock_file.write_text(
        json.dumps(
            {
                "status": "locked",
                "run_id": "run-new",
                "workflow": "company-model",
                "expires_at": time.time() + 600.0,
            }
        ),
        encoding="utf-8",
    )
    cand_refused = ws_pub / "model" / "PDD-model-v02.xlsx"
    with pytest.raises(ValueError, match="held by another run"):
        medit.apply_edit_plan(
            baseline_path=base_p,
            ticker="PDD",
            plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1234}]},
            output_path=cand_refused,
            workspace_dir=ws_pub,
            fingerprint=fp_obj,
            skip_recalc=True,
            mode="update",
            run_id="run-old",
        )
    assert not cand_refused.exists()

    # 2. Expired lease -> run-old publish refused
    lock_file.write_text(
        json.dumps(
            {
                "status": "locked",
                "run_id": "run-old",
                "workflow": "company-model",
                "expires_at": time.time() - 5.0,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="lease expired"):
        medit.apply_edit_plan(
            baseline_path=base_p,
            ticker="PDD",
            plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1234}]},
            output_path=cand_refused,
            workspace_dir=ws_pub,
            fingerprint=fp_obj,
            skip_recalc=True,
            mode="update",
            run_id="run-old",
        )
    assert not cand_refused.exists()

    # 3. Active unexpired lock owned by run-old -> publish SUCCEEDS
    lock_file.write_text(
        json.dumps(
            {
                "status": "locked",
                "run_id": "run-old",
                "workflow": "company-model",
                "expires_at": time.time() + 600.0,
            }
        ),
        encoding="utf-8",
    )
    cm_p = ws_pub / "research" / "r10" / "model_dump" / "coord_map.json"
    ed = medit.apply_edit_plan(
        baseline_path=base_p,
        ticker="PDD",
        plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1234}]},
        output_path=cand_refused,
        workspace_dir=ws_pub,
        fingerprint=fp_obj,
        coord_map_out=cm_p,
        skip_recalc=True,
        mode="update",
        run_id="run-old",
    )
    assert cand_refused.is_file()
    sha_after_publish = ed["candidate_sha256"]

    # 4. Consensus snapshot write also re-verifies lock ownership before replacing model
    cons_data = {
        "ticker": "PDD",
        "as_of_date": "2026-11-27",
        "source_type": "Market Consensus",
        "items": [{"period": "2026E", "metric": "Total Revenue", "consensus": 1300.0}],
    }
    lock_file.write_text(
        json.dumps(
            {
                "status": "locked",
                "run_id": "run-takeover",
                "workflow": "company-model",
                "expires_at": time.time() + 600.0,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="held by another run"):
        mcons.write_consensus_snapshot(
            model_path=cand_refused,
            consensus_data=cons_data,
            coord_map_path=cm_p,
        )
    assert mfp.file_sha256(cand_refused) == sha_after_publish

    # 5. Lock lost DURING recalc -> recalc write-back refuses to swap model
    lock_file.write_text(
        json.dumps(
            {
                "status": "locked",
                "run_id": "run-recalc-owner",
                "workflow": "company-model",
                "expires_at": time.time() + 600.0,
            }
        ),
        encoding="utf-8",
    )
    cand_recalc = ws_pub / "model" / "PDD-model-v03.xlsx"
    real_recalc_mod = medit._load_module(
        "_dalton_xlsx_recalc_r10_test",
        medit._locate_xlsx_script("recalc.py"),
    )

    def _steal_lock_during_soffice(src: Path, out_dir: Path, _timeout: int) -> Path:
        lock_file.write_text(
            json.dumps(
                {
                    "status": "locked",
                    "run_id": "run-stole-mid-recalc",
                    "workflow": "company-model",
                    "expires_at": time.time() + 600.0,
                }
            ),
            encoding="utf-8",
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / (src.stem + ".xlsx")
        shutil.copyfile(src, out_file)
        return out_file

    fake_recalc = create_autospec(real_recalc_mod, instance=False)
    fake_recalc.read_package.side_effect = real_recalc_mod.read_package
    fake_recalc.computed_values.side_effect = real_recalc_mod.computed_values
    fake_recalc.collect_formulas.side_effect = real_recalc_mod.collect_formulas
    fake_recalc.refresh_charts.side_effect = real_recalc_mod.refresh_charts
    fake_recalc.MAX_LISTED = real_recalc_mod.MAX_LISTED
    fake_recalc.shutil = _NS(which=lambda _name: "/usr/bin/soffice")
    fake_recalc.soffice_calculate.side_effect = _steal_lock_during_soffice

    with pytest.raises(ValueError, match="held by another run"):
        medit.apply_edit_plan(
            baseline_path=base_p,
            ticker="PDD",
            plan={"cell_edits": [{"sheet": "Model - PDD", "cell": "E2", "value": 1500}]},
            output_path=cand_recalc,
            workspace_dir=ws_pub,
            fingerprint=fp_obj,
            skip_recalc=False,
            mode="update",
            run_id="run-recalc-owner",
            recalc_mod_override=fake_recalc,
        )
