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

from datetime import date, datetime
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
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
    """Attach a minimal valid OOXML ``xl/externalLinks/externalLink1.xml`` part to ``xlsx_path``."""
    ext_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<externalLink xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<externalBook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="rId1">'
        b'<sheetNames><sheetName val="FX"/></sheetNames>'
        b'</externalBook></externalLink>'
    )
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        infos = zf.infolist()
        payload = {i.filename: zf.read(i.filename) for i in infos}
    with zipfile.ZipFile(xlsx_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename])
        if "xl/externalLinks/externalLink1.xml" not in payload:
            zout.writestr("xl/externalLinks/externalLink1.xml", ext_xml)


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

    # Verify insert.py --preserve-calcpr kept fullCalcOnLoad disabled AND shifted Model - BABA!F6
    cand_fp = mfp.build_fingerprint(cand, ticker="JD")
    assert cand_fp["calc_properties"]["full_calc_on_load"] is False
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


@pytest.mark.skipif(
    not (os.environ.get("DALTON_TEST_PG_DSN") or os.environ.get("DATABASE_URL")),
    reason="No PostgreSQL test DSN configured (set DALTON_TEST_PG_DSN or DATABASE_URL; run on remote pgtest)",
)
def test_recover_baseline_real_postgres_integration(tmp_path: Path) -> None:
    """Principle #5: execute real PostgreSQL SQL queries via PostgresReadOnlyAdapter against a real PG DB."""
    import psycopg

    dsn = os.environ.get("DALTON_TEST_PG_DSN") or os.environ["DATABASE_URL"]
    schema = f"dalton_p0a_pgtest_{uuid.uuid4().hex[:10]}"
    baseline_bytes = FIXTURE_XLSX.read_bytes()
    baseline_sha = hashlib.sha256(baseline_bytes).hexdigest()
    ws_id = str(uuid.uuid4())
    att_ok_id = str(uuid.uuid4())
    att_out_id = str(uuid.uuid4())

    setup_conn = psycopg.connect(dsn, autocommit=True)
    try:
        with setup_conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema};")
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

        sep = "&" if "?" in dsn else "?"
        pg_dsn = f"{dsn}{sep}options=-csearch_path%3D{schema}"
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
