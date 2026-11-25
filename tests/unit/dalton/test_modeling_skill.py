"""Unit tests for the ``dalton-model`` financial modeling skill (Batch 1 r2).

Tests cover:
- Skill registration (Core & Everflow profiles, 52 total / 30 core skills)
- 3-capability combination assembly (zero proprietary terms in Standalone Core)
- ``model_style.py`` default style writing, column style cloning, knowledge period
  normalization, existing workbook style profiling with fallback to defaults,
  and ordinary ``model_lineage.json`` bookkeeping
- ``model_scan.py`` advisory scanning across all 4 categories on small synthetic
  workbooks (forecast constants, possible period mismatch, external links/plugins/
  #REF!/circular refs, and recalc errors on a disposable copy without modifying source),
  exit code 0 on scan completion and exit code 1 only on scanner errors
- Read-only smoke scan on local user sample workbooks (when present) verifying no
  crash, exit code 0, and unchanged source SHA-256
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
import zipfile

from lxml import etree
import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.styles.colors import Color
from openpyxl.workbook.defined_name import DefinedName
import pytest

from src.dalton.capabilities import CORE_CAPABILITY, CapabilityId
from src.dalton.content_assembly import read_assembled_text

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model"
SCRIPTS_DIR = SKILL_DIR / "scripts"
SAMPLES_DIR = REPO_ROOT / ".agy" / "modeling_samples"

PROPRIETARY_BLACKLIST: tuple[str, ...] = (
    "researchdata",
    "alphaengine",
    "guidepoint",
    "sales-notes",
    "tmtb",
    "funda-ai",
    "funda ai",
    "feishu",
    "weixin",
    "seed-asr",
    "dalton-13f-tracker",
    "everflow",
    "公司 wiki",
    "销售纪要",
    "自动入库",
    "/transcribe",
    "内部研究库",
    "基金 wiki",
)


def _load_script(name: str, path: Path) -> Any:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ms = _load_script("model_style", SCRIPTS_DIR / "model_style.py")
mscan = _load_script("model_scan", SCRIPTS_DIR / "model_scan.py")


# ---------------------------------------------------------------------------
# 1. Skill Registration & 3-Capability Assembly
# ---------------------------------------------------------------------------


def test_dalton_model_registration_core_and_everflow(monkeypatch: pytest.MonkeyPatch) -> None:
    from ptc_agent.agent.middleware.skills import dalton_registry as dr_mod
    from ptc_agent.agent.middleware.skills import registry as reg_mod

    assert ("dalton-model", "dalton-model", ("dalton-conventions", "research-conventions")) in dr_mod._DALTON_SKILL_SPECS
    assert dr_mod.DALTON_SKILL_CAPABILITY_MAP["dalton-model"] is CORE_CAPABILITY
    assert len(dr_mod._DALTON_SKILL_SPECS) == 52
    assert len(dr_mod.DALTON_SKILL_DEFINITIONS) == 52

    defn = dr_mod.DALTON_SKILL_DEFINITIONS["dalton-model"]
    assert defn.command == "dalton-model"
    assert defn.requires == ("dalton-conventions", "research-conventions")
    assert len(defn.description) <= 120
    assert "触发词" not in defn.description

    for profile in ("core", "everflow"):
        monkeypatch.setenv("DALTON_PROFILE", profile)
        monkeypatch.setenv("APP_ENV", "core" if profile == "core" else "dalton")
        sk = reg_mod.get_skill("dalton-model")
        assert sk is not None, f"get_skill('dalton-model') returned None in profile={profile}"
        assert sk.requires == ("dalton-conventions", "research-conventions")


def test_dalton_model_three_capability_combinations_assembly() -> None:
    md_files = sorted(SKILL_DIR.rglob("*.md"))
    assert len(md_files) == 5  # SKILL.md + 4 references/*.md
    conventions_md = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-conventions" / "SKILL.md"

    # 1. Core (frozenset()) — zero proprietary words across all skill docs + conventions
    core_caps: frozenset[CapabilityId] = frozenset()
    for fp in [*md_files, conventions_md]:
        text = read_assembled_text(fp, capabilities=core_caps)
        assert "DALTON_CAP" not in text, f"Unresolved DALTON_CAP in {fp}"
        lowered = text.lower()
        hits = [term for term in PROPRIETARY_BLACKLIST if term in lowered]
        assert not hits, f"Core leak in {fp}: {hits}"

    sanity_core = read_assembled_text(SKILL_DIR / "references" / "sanity-check.md", capabilities=core_caps)
    assert "yf_analysis.get_revenue_estimates" in sanity_core
    assert "get_earnings_estimates" in sanity_core
    assert "fundamentals.get_analyst_estimates" in sanity_core

    conv_core = read_assembled_text(conventions_md, capabilities=core_caps)
    assert ".agents/skills/dalton-model/SKILL.md" in conv_core
    est_line = [ln for ln in conv_core.splitlines() if "**卖方一致预期" in ln][0]
    assert "yf_analysis.get_revenue_estimates" in est_line
    assert "yf_fundamentals" not in est_line

    # 2. Bridge ON, AlphaEngine OFF — bridge consensus active, broker falls back to public, 0 alphaengine hits
    bridge_only = frozenset({CapabilityId.RESEARCHDATA_BRIDGE})
    for fp in md_files:
        text = read_assembled_text(fp, capabilities=bridge_only)
        assert "DALTON_CAP" not in text
        assert "alphaengine" not in text.lower(), f"alphaengine leaked in {fp}"
    sanity_bridge = read_assembled_text(SKILL_DIR / "references" / "sanity-check.md", capabilities=bridge_only)
    assert 'researchdata.finance(route="estimates"' in sanity_bridge
    assert "field_status" in sanity_bridge
    assert "web_search" in sanity_bridge

    # 3. Bridge ON + AlphaEngine ON — both internal blocks active, 0 markers
    both = frozenset({CapabilityId.RESEARCHDATA_BRIDGE, CapabilityId.ALPHAENGINE})
    sanity_both = read_assembled_text(SKILL_DIR / "references" / "sanity-check.md", capabilities=both)
    assert "DALTON_CAP" not in sanity_both
    assert 'researchdata.finance(route="estimates"' in sanity_both
    assert 'researchdata.research(route="alphaengine"' in sanity_both


# ---------------------------------------------------------------------------
# 2. Mechanical Style Helpers, Profile Extraction & Ordinary Lineage Record
# ---------------------------------------------------------------------------


def test_model_style_apply_defaults_clone_column_and_period_norm() -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws.column_dimensions["E"].width = 9.0

    ws.cell(1, 5, "2026E")
    ms.apply_cell_style(ws.cell(1, 5), "header")
    assert ws.cell(1, 5).fill.fgColor.rgb.endswith(ms.HEADER_FILL)
    assert ws.cell(1, 5).font.color.rgb.endswith(ms.HEADER_FONT)

    ws.cell(2, 5, 0.12)
    ms.apply_cell_style(ws.cell(2, 5), "driver_assumption", is_percent=True, number_format=ms.FMT_PCT)
    assert ws.cell(2, 5).fill.fgColor.rgb.endswith(ms.ASSUMP_FILL)
    assert ws.cell(2, 5).font.italic is True
    assert ws.cell(2, 5).border.left.style == ms.ASSUMP_BORDER
    assert ws.cell(2, 5).border.bottom.style == ms.ASSUMP_BORDER

    ws.cell(3, 5, "=Model!D3*(1+E2)")
    ms.apply_cell_style(ws.cell(3, 5), "financial_output", is_link=True, is_total=True, number_format=ms.FMT_INT)
    assert ws.cell(3, 5).font.bold is True
    assert ws.cell(3, 5).font.color.rgb.endswith(ms.LINK_FONT)

    # Clone column E styles & width to column F
    ms.clone_period_column_style(ws, "E", "F", max_row=3)
    assert ws.column_dimensions["F"].width == 9.0
    assert ws.cell(2, 6).fill.fgColor.rgb.endswith(ms.ASSUMP_FILL)
    assert ws.cell(3, 6).number_format == ms.FMT_INT

    # Knowledge period normalization helper
    assert ms.normalize_period_for_knowledge("2026") == "FY2026"
    assert ms.normalize_period_for_knowledge("2026E") == "FY2026"
    assert ms.normalize_period_for_knowledge("2026F") == "FY2026"
    assert ms.normalize_period_for_knowledge("4Q26E") == "4Q26"
    assert ms.normalize_period_for_knowledge("2026Q4E") == "4Q26"
    assert ms.normalize_period_for_knowledge("CAGR 25-28E") is None


def test_model_style_profile_and_fallback_to_default(tmp_path: Path) -> None:
    # Theme + tint resolution math
    palette = {1: "000000", 6: "A5A5A5"}
    assert ms.resolve_color_rgb(Color(theme=1, tint=0.0), palette) == "000000"
    gray = ms.resolve_color_rgb(Color(theme=6, tint=0.80), palette)
    assert gray is not None and abs(int(gray[0:2], 16) - 0xED) <= 4

    # Build small existing workbook with custom sheet styles (empty divider + 11pt valuation sheet + 8pt model sheet)
    wb = openpyxl.Workbook()
    ws_div = wb.active
    ws_div.title = "PDD>>"

    ws_val = wb.create_sheet("Futu>>")
    c_val = ws_val.cell(2, 2, "=100*1.1")
    c_val.font = Font(name="等线", size=11)
    c_val.number_format = "General"

    ws_mod = wb.create_sheet("Model")
    ws_mod.views.sheetView[0].showGridLines = False
    ws_mod.freeze_panes = "E3"
    ws_mod.cell(2, 1, "Income statement").fill = PatternFill(fill_type="solid", fgColor="99CCFF")
    for r in range(3, 8):
        c = ws_mod.cell(r, 5, 100 + r)
        c.font = Font(name="Arial", size=8, color="0000FF")
        c.number_format = ms.FMT_INT

    xlsx = tmp_path / "existing.xlsx"
    wb.save(xlsx)

    prof = ms.profile_workbook_style(xlsx)
    assert prof["sheet_count"] == 3
    assert prof["sheet_overrides"]["PDD>>"]["sheet_type"] == "blank_divider"
    assert prof["sheet_overrides"]["Futu>>"]["sheet_type"] == "content_sheet"
    assert prof["sheet_overrides"]["Futu>>"]["dominant_font_size"] == 11.0
    assert prof["sheet_overrides"]["Model"]["freeze_panes"] == "E3"
    assert prof["sheet_overrides"]["Model"]["gridlines_hidden"] is True
    assert "99CCFF" in prof["sheet_overrides"]["Model"]["section_fills"]

    # Applying style with extracted profile inherits sheet-level 11pt override for Futu>>
    wb_new = openpyxl.Workbook()
    ws_new = wb_new.active
    c_new = ws_new.cell(1, 1, "=1+1")
    ms.apply_cell_style(c_new, "financial_output", profile=prof, sheet_name="Futu>>")
    assert c_new.font.size == 11.0

    # Fallback cleanly to defaults when workbook is missing or corrupt
    missing_prof = ms.profile_workbook_style(tmp_path / "does_not_exist.xlsx")
    assert missing_prof["source"] == "default"
    assert missing_prof["workbook_defaults"]["dominant_font_size"] == float(ms.FONT_SIZE)
    assert "fallback_reason" in missing_prof

    corrupt = tmp_path / "corrupt.xlsx"
    corrupt.write_bytes(b"not-a-zip-file")
    corrupt_prof = ms.profile_workbook_style(corrupt)
    assert corrupt_prof["source"] == "default"
    assert "fallback_reason" in corrupt_prof


def test_model_lineage_ordinary_bookkeeping_record(tmp_path: Path) -> None:
    base = tmp_path / "base.xlsx"
    out = tmp_path / "v01.xlsx"
    base.write_bytes(b"base-bytes")
    out.write_bytes(b"out-bytes")

    lin = tmp_path / "model_lineage.json"
    data = ms.record_model_lineage(lin, output_path=out, baseline_path=base, note="3Q26 update")
    loaded = ms.load_model_lineage(lin)
    assert loaded["latest"]["input_file"] == str(base)
    assert loaded["latest"]["input_sha256"] == ms.file_sha256(base)
    assert loaded["latest"]["output_file"] == str(out)
    assert loaded["latest"]["output_sha256"] == ms.file_sha256(out)
    assert loaded["latest"]["note"] == "3Q26 update"
    assert len(data["records"]) == 1


# ---------------------------------------------------------------------------
# 3. Advisory Scanner (`model_scan.py`) Tests
# ---------------------------------------------------------------------------


def test_model_scan_reports_all_four_advisory_categories(tmp_path: Path) -> None:
    p = tmp_path / "scan_cases.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"

    # Period headers: E=2025, F=2026E, G=2027E, I=1Q26E, J=2Q26E
    for col, hdr in ((5, 2025), (6, "2026E"), (7, "2027E"), (9, "1Q26E"), (10, "2Q26E")):
        ws.cell(1, col, hdr)

    # 1. Forecast constants (both an assumption row and an amount row listed neutrally!)
    ws.cell(2, 4, "Growth Assumption")
    c_assump = ws.cell(2, 9, 0.15)
    ms.apply_cell_style(c_assump, "driver_assumption", is_percent=True)
    ws.cell(3, 4, "Total Revenue")
    ws.cell(3, 9, 500.0)

    # 2. Possible period mismatch: 2027E column summing 2026E quarters I3:J3
    ws.cell(3, 7, "=SUM(I3:J3)")

    # 3. External/plugin/#REF!/circular:
    #    - Plugin formulas (CIQ, BQL, BDH)
    ws.cell(4, 4, "Plugins")
    ws.cell(4, 6, '=_xll.ciqfunctions.udf.CIQ("BABA","IQ_LASTSALEPRICE")')
    ws.cell(4, 7, '=BQL("BABA US Equity","PX_LAST")')
    ws.cell(4, 9, '=BDH("BABA US Equity","PX_LAST")')
    #    - Broken #REF! hidden inside IFERROR
    ws.cell(5, 4, "Broken Ref")
    ws.cell(5, 6, '=IFERROR(#REF!/E3-1,"")')
    #    - External workbook reference in formula + definedName
    ws.cell(6, 4, "External Link")
    ws.cell(6, 6, "=[OtherBook.xlsx]Sheet1!A1")
    wb.defined_names["ExtPrice"] = DefinedName("ExtPrice", attr_text="'[OtherBook.xlsx]Sheet1'!$B$2")
    #    - Circular reference cycle (B10 <-> C10)
    ws.cell(10, 2, "=C10+1")
    ws.cell(10, 3, "=B10+1")

    # 4. Cached Excel error cell
    ws.cell(11, 4, "Error Cell")
    ws.cell(11, 6, "=1/0")
    wb.save(p)

    # Inject cached #DIV/0! into F11 AND an externalLink package part so all sub-paths are exercised
    with zipfile.ZipFile(p, "r") as zf:
        infos = zf.infolist()
        payload = {i.filename: zf.read(i.filename) for i in infos}
    root = etree.fromstring(payload["xl/worksheets/sheet1.xml"], ms._XML_PARSER)
    for c_el in root.iter(f"{{{mscan._SML}}}c"):
        if (c_el.get("r") or "").upper() == "F11":
            c_el.set("t", "e")
            v_el = c_el.find(f"{{{mscan._SML}}}v")
            if v_el is None:
                v_el = etree.SubElement(c_el, f"{{{mscan._SML}}}v")
            v_el.text = "#DIV/0!"
    payload["xl/worksheets/sheet1.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            zout.writestr(info.filename, payload[info.filename])

    # Run via CLI with python -I and verify exit code 0 + JSON + summary!
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS_DIR / "model_scan.py"), str(p)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"model_scan.py must exit 0 even when findings exist: {proc.stderr}"
    rep = json.loads(proc.stdout)
    assert rep["status"] == "ok"
    assert isinstance(rep["summary"], str) and "扫描完成" in rep["summary"]

    # Verify all 4 categories populated with sheet/cell/reason
    assert rep["counts"]["forecast_constants"] >= 2
    fc_cells = {(it["sheet"], it["cell"]) for it in rep["items"]["forecast_constants"]}
    assert ("Model", "I2") in fc_cells and ("Model", "I3") in fc_cells

    assert rep["counts"]["possible_period_mismatch"] >= 1
    pm_item = rep["items"]["possible_period_mismatch"][0]
    assert pm_item["cell"] == "G3" and "可能" in pm_item["reason"]

    ext_items = rep["items"]["external_or_broken_or_circular"]
    reasons = " | ".join(it["reason"] for it in ext_items)
    assert "CIQ" in reasons and "BQL" in reasons and "BDH" in reasons
    assert "#REF!" in reasons
    assert "外部工作簿" in reasons
    assert "循环引用" in reasons

    assert rep["counts"]["recalc_errors"] >= 1
    assert any(it["cell"] == "F11" and "#DIV/0!" in it["reason"] for it in rep["items"]["recalc_errors"])


def test_model_scan_recalc_on_disposable_copy_leaves_source_untouched(tmp_path: Path) -> None:
    p = tmp_path / "div_zero.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws["A1"] = "Value"
    ws["B1"] = "=10/0"
    wb.save(p)
    before_hash = ms.file_sha256(p)

    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS_DIR / "model_scan.py"), str(p), "--recalc"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert ms.file_sha256(p) == before_hash, "model_scan.py --recalc must not modify source workbook"
    rep = json.loads(proc.stdout)
    assert rep["status"] == "ok"
    if rep["recalc_ran"]:
        assert any(it["cell"] == "B1" and "#DIV/0!" in it["reason"] for it in rep["items"]["recalc_errors"])


def test_model_scan_returns_nonzero_only_on_scanner_error(tmp_path: Path) -> None:
    # Missing file -> exit 1
    proc_missing = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS_DIR / "model_scan.py"), str(tmp_path / "nope.xlsx")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_missing.returncode == 1
    assert json.loads(proc_missing.stdout)["status"] == "error"

    # Unknown --sheet -> exit 1
    p = tmp_path / "ok.xlsx"
    openpyxl.Workbook().save(p)
    proc_sheet = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS_DIR / "model_scan.py"), str(p), "--sheet", "NoSuchSheet"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_sheet.returncode == 1
    assert json.loads(proc_sheet.stdout)["status"] == "error"


@pytest.mark.parametrize("filename", ["ecom_20261008.xlsx", "brokerage_20260820.xlsx"])
def test_model_scan_real_samples_do_not_crash_and_exit_zero(filename: str) -> None:
    sample_path = SAMPLES_DIR / filename
    if not sample_path.is_file():
        pytest.skip(f"Private local sample workbook not present: {sample_path}")

    before_hash = ms.file_sha256(sample_path)
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS_DIR / "model_scan.py"), str(sample_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"model_scan.py crashed on {filename}: {proc.stderr}"
    rep = json.loads(proc.stdout)
    assert rep["status"] == "ok"
    assert isinstance(rep["summary"], str) and rep["summary"]
    assert ms.file_sha256(sample_path) == before_hash
