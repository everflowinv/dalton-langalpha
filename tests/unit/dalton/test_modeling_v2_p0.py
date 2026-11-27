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

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from typing import Any
from unittest.mock import create_autospec
import zipfile

from lxml import etree
import openpyxl
from openpyxl.workbook.defined_name import DefinedName
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


def test_unchanged_baseline_has_zero_actionable_and_single_engine_note(tmp_path: Path) -> None:
    ws, rdir, base = _copy_fixture_to_ws(tmp_path)
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
    # Verify donor cache restored cell-by-cell on Model - BABA
    assert _xml_cached_map(cand, "Model - BABA") == _xml_cached_map(donor, "Model - BABA")

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


# ---------------------------------------------------------------------------
# 5. Donor cache restore: normal recalc & simulated LibreOffice timeout / no-output exit
# ---------------------------------------------------------------------------


def test_donor_cache_preserved_on_normal_recalc_and_soffice_timeout_or_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, _, base = _copy_fixture_to_ws(tmp_path)

    # 5a. Normal recalc with create_autospec stub for soffice_calculate:
    # LibreOffice produces #NAME? for CIQ (E45) & downstream F45, #REF! for external (E46),
    # #NAME? for BABA CIQ (E7), and new computed value 9999.0 for edited formula G4.
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
                    "E46": ("e", "#REF!"),
                    "G4": (None, "10500.5"),
                },
                "Model - BABA": {
                    "E7": ("e", "#NAME?"),
                    "F4": ("e", "#REF!"),
                },
            },
        )
        return produced

    soffice_stub = create_autospec(_REAL_SOFFICE_CALC, side_effect=_fake_soffice_ok)
    which_stub = create_autospec(_REAL_SHUTIL_WHICH, return_value="/usr/bin/soffice")
    monkeypatch.setattr(recalc_mod, "soffice_calculate", soffice_stub)
    monkeypatch.setattr(recalc_mod.shutil, "which", which_stub)

    plan_normal = {
        "cell_edits": [
            {"sheet": "Model - JD", "cell": "G4", "value": "=SUM(O4:R4)"},
        ],
        "changelog_entry": {"version": "v02", "date": "2026-11-27", "baseline": base.name, "summary": "Recalc test"},
    }
    res_ok = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan=plan_normal,
        workspace_dir=ws,
        recalc_mod_override=recalc_mod,
    )
    cand_ok = Path(res_ok["candidate_path"])
    donor_ok = Path(res_ok["cache_donor_path"])

    # Non-target company sheets & CIQ/external exempt cells match donor <v> cell-by-cell
    assert _xml_cached_map(cand_ok, "Model - BABA") == _xml_cached_map(donor_ok, "Model - BABA")
    assert _xml_cached_map(cand_ok, "Subs - BABA") == _xml_cached_map(donor_ok, "Subs - BABA")
    jd_ok_cache = _xml_cached_map(cand_ok, "Model - JD")
    jd_donor_cache = _xml_cached_map(donor_ok, "Model - JD")
    assert jd_ok_cache["E45"] == jd_donor_cache["E45"] == (None, "38.5")  # CIQ preserved!
    assert jd_ok_cache["E46"] == jd_donor_cache["E46"] == (None, "7.25")  # external preserved!
    assert jd_ok_cache["F45"] == jd_donor_cache["F45"] == (None, "38.5")  # downstream error kept donor!
    assert jd_ok_cache["G4"] == (None, "10500.5")  # non-error computed value merged!

    # Downstream error from CIQ (F45) must be classified as baseline_legacy, NOT actionable!
    chk_downstream = mchk.run_checks(
        candidate_path=cand_ok,
        ticker="JD",
        baseline_path=base,
        coord_map=res_ok["coord_map"],
    )
    c13_act = [f for f in chk_downstream["findings"] if f["check_id"] == "C13" and f["severity_class"] == "actionable"]
    c13_leg = [f for f in chk_downstream["findings"] if f["check_id"] == "C13" and f["severity_class"] == "baseline_legacy"]
    assert c13_act == [], c13_act
    assert any(f.get("cell") == "F45" for f in c13_leg), c13_leg

    # 5b. Simulated LibreOffice TimeoutExpired
    timeout_stub = create_autospec(
        _REAL_SOFFICE_CALC,
        side_effect=subprocess.TimeoutExpired(cmd=["soffice"], timeout=10),
    )
    monkeypatch.setattr(recalc_mod, "soffice_calculate", timeout_stub)
    res_to = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan=plan_normal,
        output_path=ws / "model" / "China_Ecommerce_v03.xlsx",
        recalc_mod_override=recalc_mod,
    )
    assert res_to["recalc_status"] == "recalc_failed"
    assert res_to["recalc_result"]["donor_cache_restored"] is True
    cand_to = Path(res_to["candidate_path"])
    donor_to = Path(res_to["cache_donor_path"])
    assert _xml_cached_map(cand_to, "Model - BABA") == _xml_cached_map(donor_to, "Model - BABA")
    assert _xml_cached_map(cand_to, "Model - JD")["E45"] == _xml_cached_map(donor_to, "Model - JD")["E45"]

    # 5c. Simulated LibreOffice exit with NO output workbook
    no_out_stub = create_autospec(
        _REAL_SOFFICE_CALC,
        side_effect=lambda src, out_dir, timeout: out_dir / "missing_output.xlsx",
    )
    monkeypatch.setattr(recalc_mod, "soffice_calculate", no_out_stub)
    res_no = medit.apply_edit_plan(
        baseline_path=base,
        ticker="JD",
        plan=plan_normal,
        output_path=ws / "model" / "China_Ecommerce_v04.xlsx",
        recalc_mod_override=recalc_mod,
    )
    assert res_no["recalc_status"] == "recalc_failed"
    assert res_no["recalc_result"]["donor_cache_restored"] is True
    cand_no = Path(res_no["candidate_path"])
    donor_no = Path(res_no["cache_donor_path"])
    assert _xml_cached_map(cand_no, "Model - BABA") == _xml_cached_map(donor_no, "Model - BABA")
    assert _xml_cached_map(cand_no, "Model - JD")["E45"] == _xml_cached_map(donor_no, "Model - JD")["E45"]


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
    assert "C04" in act_by_id, chk["findings"]
    c04_cells = {f["cell"] for f in chk["findings"] if f["check_id"] == "C04" and f["severity_class"] == "actionable"}
    assert "G6" in c04_cells  # constant == consensus
    assert "G14" in c04_cells  # defined_names -> consensus snapshot
    assert "C06" in act_by_id, chk["findings"]
    assert "C11" in act_by_id, chk["findings"]
    assert "C13" in act_by_id, chk["findings"]
    # C10 semantic-vs-formula conflict -> missing_evidence (NOT actionable!)
    assert "C10" in me_by_id, chk["findings"]
    assert not any(
        f["check_id"] == "C10" and f["severity_class"] == "actionable" and f.get("cell") == "D29"
        for f in chk["findings"]
    )


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
    assert snap_res["written_count"] == 3

    # House Model - JD forecast cells & non-target BABA caches are completely untouched!
    after_fp = mfp.build_fingerprint(cand, ticker="JD")
    after_jd_cells = after_fp["sheets"]["Model - JD"]["cells"]
    assert after_jd_cells == before_jd_cells
    assert _xml_cached_map(cand, "Model - BABA") == before_baba_cache

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
