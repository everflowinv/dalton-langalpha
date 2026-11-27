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
