"""Unit tests for the ``dalton-model`` financial modeling skill (Batch 1 r3).

Tests cover:
- Skill registration (Core & Everflow profiles, 52 total / 30 core skills)
- 3-capability combination assembly (zero proprietary terms in Standalone Core)
- ``model_style.py`` default style writing, observed profile extraction & application
  (fonts, header fill, section fill), column style cloning (protection + MergedCell),
  sparse-cell safety, knowledge period normalization, and ordinary ``model_lineage.json``
- ``model_scan.py`` advisory scanning across all 4 categories on small synthetic
  workbooks: summarized repetitive parts/names/plugins/errors, unstyled forecast
  constant priority, headerless visible sheet reporting, ArrayFormula & Tokenizer
  plugin detection, above-header #REF!, explicit hidden --sheet, sparse-cell safety,
  repo-root ``--recalc`` locator, timeout & external-link fallback to cached errors,
  and exit code 0 on scan completion / 1 only on scanner errors
- Read-only smoke scan on local user sample workbooks (when present) verifying no
  crash, exit code 0, and unchanged source SHA-256
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import create_autospec
import zipfile

from lxml import etree
import openpyxl
from openpyxl.styles import Font, PatternFill, Protection
from openpyxl.styles.colors import Color
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.formula import ArrayFormula
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

#: The real function, captured at import so every stub specs it even after another test patched it.
_REAL_SUBPROCESS_RUN = subprocess.run


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


def _inject_cached_errors_and_ext_parts(
    xlsx_path: Path,
    *,
    sheet_xml_path: str = "xl/worksheets/sheet1.xml",
    cell_errors: dict[str, str] | None = None,
    external_link_parts: int = 0,
) -> None:
    """Patch cached error values and/or dummy externalLink parts into a saved .xlsx."""
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        infos = zf.infolist()
        payload = {i.filename: zf.read(i.filename) for i in infos}

    if cell_errors:
        root = etree.fromstring(payload[sheet_xml_path], ms._XML_PARSER)
        want = {k.upper(): v for k, v in cell_errors.items()}
        for c_el in root.iter(f"{{{mscan._SML}}}c"):
            ref = (c_el.get("r") or "").upper()
            if ref in want:
                c_el.set("t", "e")
                v_el = c_el.find(f"{{{mscan._SML}}}v")
                if v_el is None:
                    v_el = etree.SubElement(c_el, f"{{{mscan._SML}}}v")
                v_el.text = want[ref]
        payload[sheet_xml_path] = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    with zipfile.ZipFile(xlsx_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            zout.writestr(info.filename, payload[info.filename])
        for idx in range(1, external_link_parts + 1):
            zout.writestr(
                f"xl/externalLinks/externalLink{idx}.xml",
                b'<?xml version="1.0" encoding="UTF-8"?><externalLink/>',
            )


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


@pytest.mark.asyncio
async def test_dalton_model_stays_enabled_when_dependents_are_enabled(tmp_path: Path) -> None:
    """Modeling Batch 2 r2 (#2): dalton-full-coverage and dalton-earnings-deep
    declare `dalton-model` in `requires`, so disabling `dalton-model` alone
    keeps it synced in the sandbox while either dependent stays enabled."""
    from ptc_agent.agent.middleware.skills.dalton_registry import DALTON_SKILL_DEFINITIONS
    from ptc_agent.agent.middleware.skills.registry import (
        SKILL_REGISTRY,
        resolve_disabled_skills,
    )
    from ptc_agent.core.sandbox.assets import _collect_local_skill_names

    layer = "dalton-model"
    expected_dependents = {"dalton-full-coverage", "dalton-earnings-deep"}
    for dep in expected_dependents:
        assert layer in DALTON_SKILL_DEFINITIONS[dep].requires

    dependents = {name for name, skill in SKILL_REGISTRY.items() if layer in skill.requires}
    assert expected_dependents <= dependents
    assert layer not in resolve_disabled_skills({layer})
    assert resolve_disabled_skills({layer, *dependents}) == frozenset({layer, *dependents})

    for name in (layer, *expected_dependents):
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("body", encoding="utf-8")

    kept = await _collect_local_skill_names([str(tmp_path)], disabled=frozenset({layer}))
    assert kept == {layer, *expected_dependents}

    removed = await _collect_local_skill_names(
        [str(tmp_path)],
        disabled=frozenset({layer, *dependents}),
    )
    assert layer not in removed
    assert not (expected_dependents & removed)


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
    assert "`yf_analysis`" in est_line and "get_revenue_estimates" in est_line
    assert "yf_fundamentals" not in est_line
    assert "FMP" in est_line

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
    ws.cell(2, 5).protection = Protection(locked=False)
    assert ws.cell(2, 5).fill.fgColor.rgb.endswith(ms.ASSUMP_FILL)
    assert ws.cell(2, 5).font.italic is True
    assert ws.cell(2, 5).border.left.style == ms.ASSUMP_BORDER
    assert ws.cell(2, 5).border.bottom.style == ms.ASSUMP_BORDER

    ws.cell(3, 5, "=Model!D3*(1+E2)")
    ms.apply_cell_style(ws.cell(3, 5), "financial_output", is_link=True, is_total=True, number_format=ms.FMT_INT)
    assert ws.cell(3, 5).font.bold is True
    assert ws.cell(3, 5).font.color.rgb.endswith(ms.LINK_FONT)

    # Merge E4:F4 so F4 is a non-top-left MergedCell and must be skipped cleanly
    ws.merge_cells("E4:F4")
    ws.cell(4, 5, "Merged banner")

    # Clone column E styles (including protection) & width to column F
    ms.clone_period_column_style(ws, "E", "F", max_row=4)
    assert ws.column_dimensions["F"].width == 9.0
    assert ws.cell(2, 6).fill.fgColor.rgb.endswith(ms.ASSUMP_FILL)
    assert ws.cell(2, 6).protection.locked is False
    assert ws.cell(3, 6).number_format == ms.FMT_INT

    # Knowledge period normalization helper
    assert ms.normalize_period_for_knowledge("2026") == "FY2026"
    assert ms.normalize_period_for_knowledge("2026E") == "FY2026"
    assert ms.normalize_period_for_knowledge("2026F") == "FY2026"
    assert ms.normalize_period_for_knowledge("4Q26E") == "4Q26"
    assert ms.normalize_period_for_knowledge("2026Q4E") == "4Q26"
    assert ms.normalize_period_for_knowledge("CAGR 25-28E") is None


def test_model_style_profile_applies_observed_styles_and_fallback(tmp_path: Path) -> None:
    # Theme + tint resolution math
    palette = {1: "000000", 6: "A5A5A5"}
    assert ms.resolve_color_rgb(Color(theme=1, tint=0.0), palette) == "000000"
    gray = ms.resolve_color_rgb(Color(theme=6, tint=0.80), palette)
    assert gray is not None and abs(int(gray[0:2], 16) - 0xED) <= 4

    # Existing workbook with custom fonts, header fill, section fill, 11pt sheet, and sparse XFD5 cell
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

    c_hdr = ws_mod.cell(1, 5, "2026E")
    c_hdr.font = Font(name="Calibri", size=9, bold=True, color="FFFFFF")
    c_hdr.fill = PatternFill(fill_type="solid", fgColor="1F4E79")

    c_sec = ws_mod.cell(2, 1, "Income statement")
    c_sec.font = Font(name="Calibri", size=9, bold=True, color="000000")
    c_sec.fill = PatternFill(fill_type="solid", fgColor="D9E1F2")

    c_zh = ws_mod.cell(3, 4, "营业收入")
    c_zh.font = Font(name="微软雅黑", size=9)

    for r in range(3, 8):
        c = ws_mod.cell(r, 5, 100 + r)
        c.font = Font(name="Calibri", size=9, color="0000FF")
        c.number_format = ms.FMT_INT
    # Far-right sparse cell must not trigger dense rectangle materialization
    ws_mod["XFD5"] = "far"

    xlsx = tmp_path / "existing.xlsx"
    wb.save(xlsx)

    prof = ms.profile_workbook_style(xlsx)
    assert prof["sheet_count"] == 3
    assert prof["sheet_overrides"]["PDD>>"]["sheet_type"] == "blank_divider"
    assert prof["sheet_overrides"]["Futu>>"]["sheet_type"] == "content_sheet"
    assert prof["sheet_overrides"]["Futu>>"]["dominant_font_size"] == 11.0
    assert prof["sheet_overrides"]["Model"]["freeze_panes"] == "E3"
    assert prof["sheet_overrides"]["Model"]["gridlines_hidden"] is True
    assert prof["sheet_overrides"]["Model"]["dominant_font_en"] == "Calibri"
    assert prof["sheet_overrides"]["Model"]["dominant_font_zh"] == "微软雅黑"
    assert prof["sheet_overrides"]["Model"]["header_fill"] == "1F4E79"
    assert "D9E1F2" in prof["sheet_overrides"]["Model"]["section_fills"]
    assert prof["workbook_defaults"]["dominant_font_en"] == "Calibri"
    assert prof["workbook_defaults"]["header_fill"] == "1F4E79"

    # apply_cell_style uses extracted profile values (font, size, header fill, section fill)
    wb_new = openpyxl.Workbook()
    ws_new = wb_new.active
    c_new_val = ws_new.cell(1, 1, "=1+1")
    ms.apply_cell_style(c_new_val, "financial_output", profile=prof, sheet_name="Futu>>")
    assert c_new_val.font.size == 11.0

    c_new_hdr = ws_new.cell(1, 2, "2027E")
    ms.apply_cell_style(c_new_hdr, "header", profile=prof, sheet_name="Model")
    assert c_new_hdr.font.name == "Calibri"
    assert c_new_hdr.fill.fgColor.rgb.endswith("1F4E79")

    c_new_zh = ws_new.cell(2, 2, "利润表")
    ms.apply_cell_style(c_new_zh, "sec_report", profile=prof, sheet_name="Model")
    assert c_new_zh.font.name == "微软雅黑"
    # Unrecognized custom section fill D9E1F2 stays in section_fills/blocks instead of overwriting sec_report
    assert c_new_zh.fill.fgColor.rgb.endswith(ms.SEC_REPORT_FILL)
    assert "D9E1F2" in prof["workbook_defaults"]["section_fills"]

    # Without profile, apply_cell_style falls back cleanly to defaults
    c_def_hdr = ws_new.cell(1, 3, "2028E")
    ms.apply_cell_style(c_def_hdr, "header")
    assert c_def_hdr.font.name == ms.FONT_EN
    assert c_def_hdr.fill.fgColor.rgb.endswith(ms.HEADER_FILL)

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


def test_model_style_section_colors_match_known_defaults_not_order(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model - Futu"
    # Peach (FFCC99) appears BEFORE light blue (99CCFF), plus an unknown custom color (D9E1F2)
    c1 = ws.cell(2, 1, "Brokerage submodel")
    c1.fill = PatternFill(fill_type="solid", fgColor="FFCC99")
    c1.font = Font(name="Arial", size=8, bold=True, color="000000")
    c2 = ws.cell(4, 1, "Income statement")
    c2.fill = PatternFill(fill_type="solid", fgColor="99CCFF")
    c2.font = Font(name="Arial", size=8, bold=True, color="000000")
    c3 = ws.cell(6, 1, "Custom block")
    c3.fill = PatternFill(fill_type="solid", fgColor="D9E1F2")
    c3.font = Font(name="Arial", size=8, bold=True, color="1F4E79")

    xlsx = tmp_path / "sections.xlsx"
    wb.save(xlsx)

    prof = ms.profile_workbook_style(xlsx)
    sh = prof["sheet_overrides"]["Model - Futu"]
    assert sh["section_fills"] == ["FFCC99", "99CCFF", "D9E1F2"]
    assert sh["section_styles"]["sec_report"]["fill_color"] == "99CCFF"
    assert sh["section_styles"]["sec_submodel"]["fill_color"] == "FFCC99"
    assert sh["section_styles"]["sec_segment"]["fill_color"] == "993300"

    wb_out = openpyxl.Workbook()
    ws_out = wb_out.active
    c_rep = ws_out.cell(1, 1, "Brokerage business")
    ms.apply_cell_style(c_rep, "sec_report", profile=prof, sheet_name="Model - Futu")
    assert c_rep.fill.fgColor.rgb.endswith("99CCFF")
    c_sub = ws_out.cell(2, 1, "Sub block")
    ms.apply_cell_style(c_sub, "sec_submodel", profile=prof, sheet_name="Model - Futu")
    assert c_sub.fill.fgColor.rgb.endswith("FFCC99")


def test_model_style_parts_diff_detects_dropped_media_and_x14_after_openpyxl_roundtrip(
    tmp_path: Path,
) -> None:
    orig = tmp_path / "orig.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws["A1"] = "Revenue"
    ws["B1"] = 100
    ws2 = wb.create_sheet("Summary")
    ws2["A1"] = "Note"
    wb.save(orig)

    # Inject an EMF media image + drawing rel + x14 conditional formatting extLst into Model (sheet1.xml)
    with zipfile.ZipFile(orig, "r") as zf:
        infos = zf.infolist()
        payload = {i.filename: zf.read(i.filename) for i in infos}

    sheet1_xml = payload["xl/worksheets/sheet1.xml"].decode("utf-8")
    x14_block = (
        '<drawing r:id="rId1"/>'
        '<extLst>'
        '<ext uri="{78C0D931-6437-407d-A8EE-F0AAD7539E65}" '
        'xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main">'
        '<x14:conditionalFormattings>'
        '<x14:conditionalFormatting><x14:cfRule type="dataBar"/></x14:conditionalFormatting>'
        '</x14:conditionalFormattings>'
        '</ext>'
        '</extLst>'
    )
    sheet1_xml = sheet1_xml.replace(
        "<worksheet ",
        '<worksheet xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" ',
        1,
    ) if "xmlns:r=" not in sheet1_xml else sheet1_xml
    sheet1_xml = sheet1_xml.replace("</worksheet>", x14_block + "</worksheet>")
    payload["xl/worksheets/sheet1.xml"] = sheet1_xml.encode("utf-8")
    payload["xl/worksheets/_rels/sheet1.xml.rels"] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" '
        b'Target="../drawings/drawing1.xml"/>'
        b'</Relationships>'
    )
    payload["xl/drawings/drawing1.xml"] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"/>'
    )
    payload["xl/drawings/_rels/drawing1.xml.rels"] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        b'Target="../media/image1.emf"/>'
        b'</Relationships>'
    )
    payload["xl/media/image1.emf"] = b"FAKE-EMF-BYTES"

    with zipfile.ZipFile(orig, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for fn, raw in payload.items():
            zout.writestr(fn, raw)

    # Identical workbook diff reports no losses and exit 0
    same = ms.diff_workbook_parts(orig, orig)
    assert same["status"] == "ok"
    assert same["has_losses"] is False

    # Roundtrip with openpyxl AND insert README & Changelog at index 0 (shifting sheet XML numbers)
    edited = tmp_path / "edited.xlsx"
    wb_rt = openpyxl.load_workbook(orig)
    wb_rt.create_sheet("README & Changelog", 0)
    wb_rt.save(edited)

    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS_DIR / "model_style.py"), "parts-diff", str(orig), str(edited)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"parts-diff must exit 0 even with losses: {proc.stderr}"
    rep = json.loads(proc.stdout)
    assert rep["status"] == "ok"
    assert rep["has_losses"] is True
    assert rep["package_losses"]["media"] == 1
    assert rep["media_ext_losses"]["emf"] == 1
    assert rep["package_losses"]["drawings"] == 1
    assert rep["package_losses"]["x14_cf_total"] == 1
    assert "Model" in rep["affected_sheets_by_type"]["media"]
    assert "Model" in rep["affected_sheets_by_type"]["x14_conditional_formatting"]
    assert "README & Changelog" in rep["sheets_added"]
    assert "EMF" in rep["summary"] and "x14" in rep["summary"] and "Model" in rep["summary"]


def test_model_style_parts_diff_refuses_to_overwrite_input_workbook(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    orig = tmp_path / "orig.xlsx"
    new = tmp_path / "new.xlsx"
    wb = openpyxl.Workbook()
    wb.save(orig)
    wb.save(new)
    orig_hash = ms.file_sha256(orig)
    new_hash = ms.file_sha256(new)

    # 1. Resolved path matching orig_workbook (via redundant dot segment)
    rc_orig = ms.main(["parts-diff", str(orig), str(new), "--out", str(orig.parent / "." / orig.name)])
    out_orig = json.loads(capsys.readouterr().out)
    assert rc_orig != 0
    assert out_orig["status"] == "error"
    assert "拒绝写入" in out_orig["message"]
    assert ms.file_sha256(orig) == orig_hash

    # 2. Symlink resolving to new_workbook
    sym_new = tmp_path / "sym_new.xlsx"
    sym_new.symlink_to(new)
    rc_sym = ms.main(["parts-diff", str(orig), str(new), "--out", str(sym_new)])
    out_sym = json.loads(capsys.readouterr().out)
    assert rc_sym != 0
    assert out_sym["status"] == "error"
    assert ms.file_sha256(new) == new_hash

    # 3. Hardlink to new_workbook (different resolved path, caught by os.path.samefile)
    hard_new = tmp_path / "hard_new.xlsx"
    os.link(new, hard_new)
    rc_hard = ms.main(["parts-diff", str(orig), str(new), "--out", str(hard_new)])
    out_hard = json.loads(capsys.readouterr().out)
    assert rc_hard != 0
    assert out_hard["status"] == "error"
    assert ms.file_sha256(new) == new_hash

    # 4. Distinct --out target succeeds and writes JSON
    report_out = tmp_path / "parts_diff.json"
    rc_ok = ms.main(["parts-diff", str(orig), str(new), "--out", str(report_out)])
    out_ok = json.loads(capsys.readouterr().out)
    assert rc_ok == 0
    assert out_ok["status"] == "ok"
    assert report_out.is_file()
    assert json.loads(report_out.read_text(encoding="utf-8"))["status"] == "ok"


def _write_minimal_xlsx_zip(
    out_path: Path,
    *,
    sheet_specs: list[dict[str, Any]],
    extra_parts: dict[str, bytes] | None = None,
) -> None:
    """Write a minimal valid OOXML zip package for parts-diff unit tests."""
    wb_sheets_xml = "".join(
        f'<sheet name="{sp["name"]}" sheetId="{idx}" r:id="rId{idx}"/>'
        for idx, sp in enumerate(sheet_specs, start=1)
    )
    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{wb_sheets_xml}</sheets></workbook>"
    ).encode("utf-8")

    wb_rels_items = "".join(
        f'<Relationship Id="rId{idx}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{idx}.xml"/>'
        for idx in range(1, len(sheet_specs) + 1)
    )
    wb_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{wb_rels_items}</Relationships>"
    ).encode("utf-8")

    parts: dict[str, bytes] = {
        "xl/workbook.xml": wb_xml,
        "xl/_rels/workbook.xml.rels": wb_rels_xml,
    }

    for idx, sp in enumerate(sheet_specs, start=1):
        dv_xml = ""
        if sp.get("dv_count", 0):
            dvs = "".join('<dataValidation type="list" sqref="A1"/>' for _ in range(sp["dv_count"]))
            dv_xml = f"<dataValidations>{dvs}</dataValidations>"
        x14_xml = ""
        if sp.get("x14_count", 0):
            cfs = "".join(
                '<x14:conditionalFormatting><x14:cfRule type="dataBar"/></x14:conditionalFormatting>'
                for _ in range(sp["x14_count"])
            )
            x14_xml = (
                '<extLst><ext uri="{78C0D931-6437-407d-A8EE-F0AAD7539E65}" '
                'xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main">'
                f"<x14:conditionalFormattings>{cfs}</x14:conditionalFormattings>"
                "</ext></extLst>"
            )
        parts[f"xl/worksheets/sheet{idx}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData/>{dv_xml}{x14_xml}</worksheet>"
        ).encode("utf-8")

        sh_rels = sp.get("rels") or []
        if sh_rels:
            rel_xml = "".join(
                f'<Relationship Id="rId{r_i}" Type="{r_type}" Target="{r_tgt}"/>'
                for r_i, (r_type, r_tgt) in enumerate(sh_rels, start=1)
            )
            parts[f"xl/worksheets/_rels/sheet{idx}.xml.rels"] = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f"{rel_xml}</Relationships>"
            ).encode("utf-8")

    if extra_parts:
        parts.update(extra_parts)

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)


def test_model_style_parts_diff_separates_additions_and_removals_by_part_path(
    tmp_path: Path,
) -> None:
    drawing_rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"
    vml_rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/vmlDrawing"
    image_rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

    orig = tmp_path / "min_orig.xlsx"
    new = tmp_path / "min_new.xlsx"

    _write_minimal_xlsx_zip(
        orig,
        sheet_specs=[
            {
                "name": "SheetA",
                "dv_count": 1,
                "x14_count": 1,
                "rels": [
                    (drawing_rel_type, "../drawings/drawing1.xml"),
                    (vml_rel_type, "../drawings/vmlDrawing1.vml"),
                ],
            },
            {"name": "SheetB", "dv_count": 0, "x14_count": 0, "rels": []},
        ],
        extra_parts={
            "xl/drawings/drawing1.xml": b'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"/>',
            "xl/drawings/_rels/drawing1.xml.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{image_rel_type}" Target="../media/image1.png"/>'
                "</Relationships>"
            ).encode("utf-8"),
            "xl/drawings/vmlDrawing1.vml": b"<xml/>",
            "xl/media/image1.png": b"PNG-ORIG",
        },
    )

    _write_minimal_xlsx_zip(
        new,
        sheet_specs=[
            {"name": "SheetA", "dv_count": 0, "x14_count": 0, "rels": []},
            {
                "name": "SheetB",
                "dv_count": 1,
                "x14_count": 1,
                "rels": [
                    (drawing_rel_type, "../drawings/drawing2.xml"),
                    (vml_rel_type, "../drawings/vmlDrawing2.vml"),
                ],
            },
        ],
        extra_parts={
            "xl/drawings/drawing2.xml": b'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"/>',
            "xl/drawings/_rels/drawing2.xml.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{image_rel_type}" Target="../media/image2.png"/>'
                "</Relationships>"
            ).encode("utf-8"),
            "xl/drawings/vmlDrawing2.vml": b"<xml/>",
            "xl/media/image2.png": b"PNG-NEW",
        },
    )

    rep = ms.diff_workbook_parts(orig, new)
    assert rep["status"] == "ok"
    assert rep["has_losses"] is True
    assert rep["has_additions"] is True

    # Equal total counts must NOT cancel out to 0; removals and additions are listed separately
    assert rep["package_losses"]["media"] == 1
    assert rep["package_additions"]["media"] == 1
    assert rep["package_losses"]["drawings"] == 2
    assert rep["package_additions"]["drawings"] == 2
    assert rep["package_losses"]["data_validations_total"] == 1
    assert rep["package_additions"]["data_validations_total"] == 1
    assert rep["package_losses"]["x14_cf_total"] == 1
    assert rep["package_additions"]["x14_cf_total"] == 1
    assert rep["media_ext_losses"] == {"png": 1}
    assert rep["media_ext_additions"] == {"png": 1}

    # Drawing and media parts are listed by exact original zip paths
    assert rep["package_parts_removed"]["drawings"] == [
        "xl/drawings/drawing1.xml",
        "xl/drawings/vmlDrawing1.vml",
    ]
    assert rep["package_parts_added"]["drawings"] == [
        "xl/drawings/drawing2.xml",
        "xl/drawings/vmlDrawing2.vml",
    ]
    assert rep["package_parts_removed"]["media"] == ["xl/media/image1.png"]
    assert rep["package_parts_added"]["media"] == ["xl/media/image2.png"]

    loss_by_sheet = {s["sheet"]: s for s in rep["sheet_losses"]}
    add_by_sheet = {s["sheet"]: s for s in rep["sheet_additions"]}
    assert loss_by_sheet["SheetA"]["drawings_removed"] == [
        "xl/drawings/drawing1.xml",
        "xl/drawings/vmlDrawing1.vml",
    ]
    assert loss_by_sheet["SheetA"]["media_removed"] == ["xl/media/image1.png"]
    assert add_by_sheet["SheetB"]["drawings_added_parts"] == [
        "xl/drawings/drawing2.xml",
        "xl/drawings/vmlDrawing2.vml",
    ]
    assert add_by_sheet["SheetB"]["media_added_parts"] == ["xl/media/image2.png"]

    assert "xl/drawings/drawing1.xml" in rep["summary"]
    assert "xl/drawings/drawing2.xml" in rep["summary"]
    assert "xl/media/image1.png" in rep["summary"]
    assert "xl/media/image2.png" in rep["summary"]


def test_model_style_parts_diff_renamed_sheet_with_chart_omits_zero_part_deltas(
    tmp_path: Path,
) -> None:
    drawing_rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"
    chart_rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"

    shared_extra = {
        "xl/drawings/drawing1.xml": b'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"/>',
        "xl/drawings/_rels/drawing1.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{chart_rel_type}" Target="../charts/chart1.xml"/>'
            "</Relationships>"
        ).encode("utf-8"),
        "xl/charts/chart1.xml": b'<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"/>',
    }

    orig = tmp_path / "chart_orig.xlsx"
    new = tmp_path / "chart_renamed.xlsx"

    _write_minimal_xlsx_zip(
        orig,
        sheet_specs=[
            {"name": "ModelOld", "rels": [(drawing_rel_type, "../drawings/drawing1.xml")]},
        ],
        extra_parts=shared_extra,
    )
    _write_minimal_xlsx_zip(
        new,
        sheet_specs=[
            {"name": "ModelNew", "rels": [(drawing_rel_type, "../drawings/drawing1.xml")]},
        ],
        extra_parts=shared_extra,
    )

    rep = ms.diff_workbook_parts(orig, new)
    assert rep["status"] == "ok"
    assert rep["sheets_removed"] == ["ModelOld"]
    assert rep["sheets_added"] == ["ModelNew"]
    assert rep["package_losses"] == {}
    assert rep["package_additions"] == {}
    assert rep["affected_sheets_by_type"] == {}
    assert rep["added_sheets_by_type"] == {}
    assert "ModelOld" in rep["summary"]
    assert "ModelNew" in rep["summary"]
    assert "0 个" not in rep["summary"]
    assert "0 张" not in rep["summary"]
    assert "绘图部件减少" not in rep["summary"]
    assert "图表减少" not in rep["summary"]
    assert "绘图部件新增" not in rep["summary"]
    assert "图表新增" not in rep["summary"]
    assert "部件路径和各表的规则数量" in rep["summary"]
    assert "数量相同的规则替换" in rep["summary"]
    assert "同一部件内的内容变化" in rep["summary"]


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

    # 1. Forecast constants (unstyled amount constant prioritized ahead of filled assumption)
    ws.cell(2, 4, "Growth Assumption")
    c_assump = ws.cell(2, 9, 0.15)
    ms.apply_cell_style(c_assump, "driver_assumption", is_percent=True)
    ws.cell(3, 4, "Total Revenue")
    ws.cell(3, 9, 500.0)

    # 2. Possible period mismatch: 2027E column summing 2026E quarters I3:J3
    ws.cell(3, 7, "=SUM(I3:J3)")

    # 3. External/plugin/#REF!/circular:
    ws.cell(4, 4, "Plugins")
    ws.cell(4, 6, '=_xll.ciqfunctions.udf.CIQ("BABA","IQ_LASTSALEPRICE")')
    ws.cell(4, 7, '=BQL("BABA US Equity","PX_LAST")')
    ws.cell(4, 9, '=BDH("BABA US Equity","PX_LAST")')
    ws.cell(5, 4, "Broken Ref")
    ws.cell(5, 6, '=IFERROR(#REF!/E3-1,"")')
    ws.cell(6, 4, "External Link")
    ws.cell(6, 6, "=[OtherBook.xlsx]Sheet1!A1")
    wb.defined_names["ExtPrice"] = DefinedName("ExtPrice", attr_text="'[OtherBook.xlsx]Sheet1'!$B$2")
    ws.cell(10, 2, "=C10+1")
    ws.cell(10, 3, "=B10+1")

    # 4. Cached Excel error cell
    ws.cell(11, 4, "Error Cell")
    ws.cell(11, 6, "=1/0")
    wb.save(p)

    _inject_cached_errors_and_ext_parts(p, cell_errors={"F11": "#DIV/0!"})

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

    # Unstyled I3 appears before filled assumption I2 within Model sheet
    fc_items = rep["items"]["forecast_constants"]
    assert rep["counts"]["forecast_constants"] >= 2
    fc_cells = [(it["sheet"], it["cell"]) for it in fc_items]
    assert ("Model", "I3") in fc_cells and ("Model", "I2") in fc_cells
    assert fc_cells.index(("Model", "I3")) < fc_cells.index(("Model", "I2"))

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


def test_model_scan_summarizes_floods_and_covers_edge_cases(tmp_path: Path) -> None:
    p = tmp_path / "flood_and_edges.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"

    # Period header on row 2; place an IFERROR(#REF!) ABOVE header on row 1
    ws.cell(1, 2, '=IFERROR(#REF!,0)')
    for col, hdr in ((5, 2025), (6, "2026E"), (7, "2027E")):
        ws.cell(2, col, hdr)

    # String literal containing "CIQ(" must NOT be flagged as plugin
    ws.cell(3, 4, "String literal")
    ws.cell(3, 5, '="CIQ(not_a_plugin)"')

    # ArrayFormula storing BQL MUST be detected as plugin
    ws.cell(4, 4, "Array BQL")
    ws["F4"] = ArrayFormula("F4", '=BQL("AAPL US Equity","PX_LAST")')

    # 12 regular BQL plugin formulas + 60 filled assumptions + 1 unstyled constant at bottom
    for r in range(5, 17):
        ws.cell(r, 6, '=BQL("AAPL US Equity","PX_LAST")')
    for r in range(17, 77):
        c = ws.cell(r, 6, 0.1)
        ms.apply_cell_style(c, "driver_assumption", is_percent=True)
    ws.cell(78, 4, "Hardcoded Revenue")
    ws.cell(78, 6, 12345.0)  # unstyled constant after 60 assumptions

    # 20 broken defined names
    for i in range(20):
        wb.defined_names[f"BrokenName{i}"] = DefinedName(f"BrokenName{i}", attr_text="Model!#REF!")

    # Cached errors: 60 #NAME? + 1 #DIV/0!
    err_map: dict[str, str] = {}
    for r in range(80, 140):
        ws.cell(r, 5, "=UNKNOWN_FUNC()")
        err_map[f"E{r}"] = "#NAME?"
    ws.cell(141, 5, "=1/0")
    err_map["E141"] = "#DIV/0!"

    # Sparse far-right cell must not bloat memory
    ws["XFD5"] = "far"

    # Visible content sheet with NO period header in rows 1..5
    ws_nohdr = wb.create_sheet("NotesSheet")
    ws_nohdr["A10"] = "Sub-block title"
    ws_nohdr["B10"] = 42

    # Hidden sheet for explicit --sheet test
    ws_hid = wb.create_sheet("HiddenCalc")
    ws_hid.sheet_state = "hidden"
    ws_hid["A1"] = "=IFERROR(#REF!,0)"

    wb.save(p)
    _inject_cached_errors_and_ext_parts(p, cell_errors=err_map, external_link_parts=50)

    # Full scan (default max_items=50): every subtype stays visible!
    rep = mscan.scan_workbook(p, recalc=True)
    assert rep["status"] == "ok"
    # Skipped recalc due to external links MUST still keep cached errors!
    assert rep["recalc_ran"] is False
    assert "外链" in rep["recalc_note"] or "外部链接" in rep["recalc_note"]
    assert rep["counts"]["recalc_errors"] == 61

    # #DIV/0! appears ahead of summarized #NAME?
    err_items = rep["items"]["recalc_errors"]
    assert err_items[0]["error"] == "#DIV/0!" and err_items[0]["cell"] == "E141"
    name_grp = [it for it in err_items if it.get("error") == "#NAME?"]
    assert len(name_grp) == 1 and name_grp[0]["count"] == 60

    # External/broken category summarizes 50 parts + 20 broken names, and still shows B1 #REF! and BQL plugins
    ext_items = rep["items"]["external_or_broken_or_circular"]
    assert any(it.get("kind") == "external_link_parts" and it.get("count") == 50 for it in ext_items)
    assert any(it.get("kind") == "defined_name_ref" and it.get("count") == 20 for it in ext_items)
    assert any(it.get("cell") == "B1" and "#REF!" in it.get("reason", "") for it in ext_items)
    assert rep["plugin_formula_counts"].get("BQL") == 13  # 1 ArrayFormula + 12 regular
    assert "CIQ" not in rep["plugin_formula_counts"]  # string literal ignored

    # Unstyled constant F78 appears FIRST ahead of the 60 filled assumptions
    fc_items = rep["items"]["forecast_constants"]
    assert fc_items[0]["cell"] == "F78" and fc_items[0]["has_fill"] is False

    # Visible sheet without period header in rows 1..5 is listed in report and summary
    assert "NotesSheet" in rep["sheets_without_period_header"]
    assert "NotesSheet" in rep["summary"]

    # Explicit --sheet on hidden sheet scans it instead of returning 0 sheets
    rep_hid = mscan.scan_workbook(p, sheet="HiddenCalc")
    assert rep_hid["status"] == "ok"
    assert rep_hid["sheets_scanned"] == 1
    assert any(it.get("sheet") == "HiddenCalc" and it.get("cell") == "A1" for it in rep_hid["items"]["external_or_broken_or_circular"])


def test_model_scan_recalc_locator_copy_contract_and_timeout_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Locator finds the real repo recalc.py when running from repo root
    monkeypatch.chdir(REPO_ROOT)
    located = mscan._locate_recalc_script()
    expected_recalc = REPO_ROOT / "plugins" / "langalpha_deliverables" / "skills" / "xlsx" / "scripts" / "recalc.py"
    assert located is not None and located.resolve() == expected_recalc.resolve()

    p = tmp_path / "div_zero.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws["A1"] = "Value"
    ws["B1"] = "=10/0"
    wb.save(p)
    _inject_cached_errors_and_ext_parts(p, cell_errors={"B1": "#DIV/0!"})
    before_hash = ms.file_sha256(p)

    # 2. Deterministic check with create_autospec: recalc receives a disposable copy (not source)
    seen_args: list[list[str]] = []

    def _fake_ok_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_args.append(list(cmd))
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({
                "status": "errors_found",
                "engine": "ironcalc",
                "total_errors": 1,
                "errors": [{"sheet": "Model", "cell": "B1", "error": "#DIV/0!", "formula": "=10/0"}],
            }),
            stderr="",
        )

    mock_run_ok = create_autospec(_REAL_SUBPROCESS_RUN, side_effect=_fake_ok_run)
    monkeypatch.setattr(mscan.subprocess, "run", mock_run_ok)

    rep_ok = mscan.scan_workbook(p, recalc=True)
    assert rep_ok["status"] == "ok"
    assert rep_ok["recalc_ran"] is True
    assert len(seen_args) == 1
    assert Path(seen_args[0][1]).resolve() == expected_recalc.resolve()
    assert Path(seen_args[0][2]).resolve() != p.resolve(), "recalc.py must receive disposable copy"
    assert any(it["cell"] == "B1" and "#DIV/0!" in it["reason"] for it in rep_ok["items"]["recalc_errors"])
    assert ms.file_sha256(p) == before_hash

    # 3. TimeoutExpired is caught cleanly and falls back to cached errors with status="ok"
    def _fake_timeout_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

    mock_run_timeout = create_autospec(_REAL_SUBPROCESS_RUN, side_effect=_fake_timeout_run)
    monkeypatch.setattr(mscan.subprocess, "run", mock_run_timeout)

    rep_to = mscan.scan_workbook(p, recalc=True)
    assert rep_to["status"] == "ok"
    assert rep_to["recalc_ran"] is False
    assert "超时" in rep_to["recalc_note"]
    assert rep_to["counts"]["recalc_errors"] == 1
    assert any(it["cell"] == "B1" and "#DIV/0!" in it["reason"] for it in rep_to["items"]["recalc_errors"])


def test_model_scan_recalc_incomplete_and_upstream_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    p = tmp_path / "partial_and_trunc.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws["A1"] = "=1/0"
    ws["A2"] = '=1+"x"'
    wb.save(p)
    _inject_cached_errors_and_ext_parts(p, cell_errors={"A1": "#DIV/0!", "A2": "#VALUE!"})

    # Case A: upstream returns status="incomplete" -> partial recalc merges cached errors
    def _fake_incomplete(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({
                "status": "incomplete",
                "engine": "ironcalc",
                "unmatched": 3,
                "total_errors": 1,
                "error_summary": {"#DIV/0!": 1},
                "errors": [{"sheet": "Model", "cell": "A1", "error": "#DIV/0!", "formula": "=1/0"}],
                "truncated": False,
            }),
            stderr="",
        )

    monkeypatch.setattr(mscan.subprocess, "run", create_autospec(_REAL_SUBPROCESS_RUN, side_effect=_fake_incomplete))
    rep_inc = mscan.scan_workbook(p, recalc=True)
    assert rep_inc["status"] == "ok"
    assert rep_inc["recalc_ran"] is True
    assert rep_inc["recalc_complete"] is False
    assert rep_inc["recalc_status"] == "incomplete"
    assert "incomplete" in rep_inc["recalc_note"]
    assert "部分重算" in rep_inc["summary"]
    err_codes = {it["error"] for it in rep_inc["items"]["recalc_errors"]}
    assert "#DIV/0!" in err_codes and "#VALUE!" in err_codes

    # Case B: upstream truncates at 100 errors (100 #DIV/0! listed, 1 #REF! only in error_summary)
    listed_100 = [
        {"sheet": "Model", "cell": f"B{i}", "error": "#DIV/0!", "formula": "=1/0"}
        for i in range(1, 101)
    ]

    def _fake_truncated(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({
                "status": "errors_found",
                "engine": "ironcalc",
                "unmatched": 0,
                "total_errors": 101,
                "error_summary": {"#DIV/0!": 100, "#REF!": 1},
                "errors": listed_100,
                "truncated": True,
            }),
            stderr="",
        )

    monkeypatch.setattr(mscan.subprocess, "run", create_autospec(_REAL_SUBPROCESS_RUN, side_effect=_fake_truncated))
    rep_tr = mscan.scan_workbook(p, recalc=True)
    assert rep_tr["status"] == "ok"
    assert rep_tr["recalc_complete"] is True
    assert rep_tr["counts"]["recalc_errors"] == 101
    assert rep_tr["truncated"]["recalc_errors"] is True
    assert rep_tr["error_code_counts"] == {"#DIV/0!": 100, "#REF!": 1}
    assert any(it.get("error") == "#REF!" for it in rep_tr["items"]["recalc_errors"])


def test_model_scan_sheet_recalc_preserves_upstream_truncation_without_workbook_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    p = tmp_path / "sheet_trunc.xlsx"
    wb = openpyxl.Workbook()
    ws_other = wb.active
    ws_other.title = "OtherSheet"
    ws_other["A1"] = "=1/0"
    ws_target = wb.create_sheet("TargetSheet")
    ws_target["A1"] = '=1+"x"'
    wb.save(p)
    _inject_cached_errors_and_ext_parts(
        p,
        sheet_xml_path="xl/worksheets/sheet2.xml",
        cell_errors={"A1": "#VALUE!"},
    )

    other_100 = [
        {"sheet": "OtherSheet", "cell": f"B{i}", "error": "#DIV/0!", "formula": "=1/0"}
        for i in range(1, 101)
    ]

    # Case A: complete recalc truncated at first 100 errors (all on OtherSheet)
    def _fake_complete_other_truncated(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({
                "status": "errors_found",
                "engine": "ironcalc",
                "unmatched": 0,
                "total_errors": 150,
                "error_summary": {"#DIV/0!": 120, "#REF!": 30},
                "errors": other_100,
                "truncated": True,
            }),
            stderr="",
        )

    monkeypatch.setattr(
        mscan.subprocess,
        "run",
        create_autospec(_REAL_SUBPROCESS_RUN, side_effect=_fake_complete_other_truncated),
    )
    rep_sheet = mscan.scan_workbook(p, sheet="TargetSheet", recalc=True)
    assert rep_sheet["status"] == "ok"
    assert rep_sheet["recalc_ran"] is True
    assert rep_sheet["recalc_complete"] is True
    assert rep_sheet["counts"]["recalc_errors"] == 0
    assert rep_sheet["error_code_counts"] == {}
    assert rep_sheet["items"]["recalc_errors"] == []
    assert rep_sheet["truncated"]["recalc_errors"] is True
    assert rep_sheet["recalc_upstream_truncated"] is True
    assert "已取得明细中属于该表的数量" in rep_sheet["summary"]
    assert "可能不全" in rep_sheet["summary"]
    assert "可能不全" in rep_sheet["recalc_note"]
    assert "150" not in rep_sheet["summary"]
    assert "150" not in rep_sheet["recalc_note"]

    # Whole-workbook scan still uses whole-workbook total_errors & error_summary
    rep_all = mscan.scan_workbook(p, recalc=True)
    assert rep_all["counts"]["recalc_errors"] == 150
    assert rep_all["error_code_counts"] == {"#DIV/0!": 120, "#REF!": 30}
    assert rep_all["truncated"]["recalc_errors"] is True

    # Case B: incomplete recalc truncated on OtherSheet merges TargetSheet cached errors only
    def _fake_incomplete_other_truncated(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({
                "status": "incomplete",
                "engine": "ironcalc",
                "unmatched": 4,
                "total_errors": 150,
                "error_summary": {"#DIV/0!": 150},
                "errors": other_100,
                "truncated": True,
            }),
            stderr="",
        )

    monkeypatch.setattr(
        mscan.subprocess,
        "run",
        create_autospec(_REAL_SUBPROCESS_RUN, side_effect=_fake_incomplete_other_truncated),
    )
    rep_inc = mscan.scan_workbook(p, sheet="TargetSheet", recalc=True)
    assert rep_inc["status"] == "ok"
    assert rep_inc["recalc_complete"] is False
    assert rep_inc["recalc_status"] == "incomplete"
    assert rep_inc["counts"]["recalc_errors"] == 1
    assert rep_inc["error_code_counts"] == {"#VALUE!": 1}
    assert rep_inc["truncated"]["recalc_errors"] is True
    assert rep_inc["recalc_upstream_truncated"] is True
    assert "已取得明细中属于该表的数量" in rep_inc["summary"]
    assert "可能不全" in rep_inc["summary"]
    assert "可能不全" in rep_inc["recalc_note"]
    assert "150" not in rep_inc["summary"]
    assert "150" not in rep_inc["recalc_note"]


def test_model_scan_subtype_quotas_dynamic_array_strings_and_short_text_headerless(
    tmp_path: Path,
) -> None:
    p = tmp_path / "quotas_and_lows.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Main"
    # 55 separate circular cycles (A1<->B1 ... A55<->B55)
    for r in range(1, 56):
        ws.cell(r, 1, f"=B{r}+1")
        ws.cell(r, 2, f"=A{r}+1")
    # 1 formula #REF! + 1 BQL plugin + dynamic-array string literal edge cases
    ws["D1"] = "=IFERROR(#REF!,0)"
    ws["D2"] = '=BQL("BABA US Equity","PX_LAST")'
    ws["D3"] = '=IFERROR(A1#,"CIQ(")'
    ws["D4"] = '=IFERROR(A1#,"#REF!")'
    ws["D5"] = '=IFERROR(A1#+#REF!,"ok")'

    # Visible sheet with only 2 text cells (must still appear in sheets_without_period_header)
    ws_txt = wb.create_sheet("CoverNotes")
    ws_txt["A1"] = "Model Overview"
    ws_txt["A2"] = "Updated 2026-11-25"

    # 50 sheets with #DIV/0! + 1 sheet with #REF! (max_items=50 must still show #REF!)
    err_targets: list[tuple[str, str, str]] = []
    for i in range(1, 51):
        s = wb.create_sheet(f"S{i:02d}")
        s["A1"] = "=1/0"
        err_targets.append((f"xl/worksheets/sheet{i + 2}.xml", "A1", "#DIV/0!"))
    s_ref = wb.create_sheet("S51_Ref")
    s_ref["A1"] = "=NA()"
    err_targets.append(("xl/worksheets/sheet53.xml", "A1", "#REF!"))

    wb.save(p)
    # Patch cached errors into all 51 sheets + 1 externalLink part
    with zipfile.ZipFile(p, "r") as zf:
        infos = zf.infolist()
        payload = {i.filename: zf.read(i.filename) for i in infos}
    for xml_path, ref_cell, code in err_targets:
        root = etree.fromstring(payload[xml_path], ms._XML_PARSER)
        for c_el in root.iter(f"{{{mscan._SML}}}c"):
            if (c_el.get("r") or "").upper() == ref_cell:
                c_el.set("t", "e")
                v_el = c_el.find(f"{{{mscan._SML}}}v")
                if v_el is None:
                    v_el = etree.SubElement(c_el, f"{{{mscan._SML}}}v")
                v_el.text = code
        payload[xml_path] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            zout.writestr(info.filename, payload[info.filename])
        zout.writestr("xl/externalLinks/externalLink1.xml", b'<?xml version="1.0"?><externalLink/>')

    rep = mscan.scan_workbook(p, max_items=50)
    assert rep["status"] == "ok"

    # Every subtype present in external_or_broken_or_circular despite 55 circular cycles
    ext_kinds = {it.get("kind") for it in rep["items"]["external_or_broken_or_circular"]}
    assert {"circular_reference", "external_link_parts", "formula_ref_error", "plugin_formula"} <= ext_kinds
    # Dynamic array string literals ignored; real #REF! in D5 detected
    assert "CIQ" not in rep["plugin_formula_counts"]
    assert rep["plugin_formula_counts"].get("BQL") == 1
    ref_cells = {it.get("cell") for it in rep["items"]["external_or_broken_or_circular"] if it.get("kind") == "formula_ref_error"}
    assert "D1" in ref_cells and "D5" in ref_cells and "D4" not in ref_cells

    # Sheet 51's #REF! is NOT crowded out by 50 sheets of #DIV/0!
    err_items = rep["items"]["recalc_errors"]
    assert any(it.get("sheet") == "S51_Ref" and it.get("error") == "#REF!" for it in err_items)
    assert any(it.get("error") == "#DIV/0!" for it in err_items)
    assert rep["truncated"]["recalc_errors"] is True

    # Short 2-cell visible text sheet IS listed in sheets_without_period_header
    assert "CoverNotes" in rep["sheets_without_period_header"]


def test_model_scan_recalc_from_repo_root_real_cli(tmp_path: Path) -> None:
    p = tmp_path / "div_zero_real.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws["A1"] = "Value"
    ws["B1"] = "=10/0"
    wb.save(p)
    before_hash = ms.file_sha256(p)

    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS_DIR / "model_scan.py"), str(p), "--recalc"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert ms.file_sha256(p) == before_hash, "model_scan.py --recalc must not modify source workbook"
    rep = json.loads(proc.stdout)
    assert rep["status"] == "ok"
    assert "未找到 recalc.py" not in rep["recalc_note"]
    if rep["recalc_ran"]:
        assert any(it["cell"] == "B1" and "#DIV/0!" in it["reason"] for it in rep["items"]["recalc_errors"])


def test_model_scan_returns_nonzero_only_on_scanner_error(tmp_path: Path) -> None:
    proc_missing = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS_DIR / "model_scan.py"), str(tmp_path / "nope.xlsx")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_missing.returncode == 1
    assert json.loads(proc_missing.stdout)["status"] == "error"

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
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"model_scan.py crashed on {filename}: {proc.stderr}"
    rep = json.loads(proc.stdout)
    assert rep["status"] == "ok"
    assert isinstance(rep["summary"], str) and rep["summary"]
    assert ms.file_sha256(sample_path) == before_hash


_READABLE_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "plugins/dalton/skills/dalton-model/scripts/model_readable.py"
)


def _run_readable(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_READABLE_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_model_readable_accepts_a_workbook_with_a_chart_sheet(tmp_path: Path) -> None:
    from openpyxl.chart import BarChart, Reference

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    for row in ([1, 2], [3, 4], [5, 6]):
        ws.append(row)
    chart = BarChart()
    chart.add_data(Reference(ws, min_col=1, min_row=1, max_col=2, max_row=3))
    cs = wb.create_chartsheet("Chart")
    cs.add_chart(chart)
    target = tmp_path / "with-chart.xlsx"
    wb.save(target)

    result = _run_readable(str(target))
    assert result.returncode == 0, result.stderr


def test_model_readable_returns_one_when_the_path_is_missing() -> None:
    result = _run_readable()
    assert result.returncode == 1
    assert "missing workbook path" in result.stderr


def test_model_readable_rejects_a_truncated_worksheet(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    for i in range(50):
        ws.append([i, i * 2, f"row {i}"])
    good = tmp_path / "good.xlsx"
    wb.save(good)

    bad = tmp_path / "truncated.xlsx"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.startswith("xl/worksheets/") and item.filename.endswith(".xml"):
                data = data.replace(b"</worksheet>", b"")
            dst.writestr(item, data)

    assert _run_readable(str(good)).returncode == 0
    result = _run_readable(str(bad))
    assert result.returncode == 1
    assert "corrupt XML" in result.stderr


def test_model_readable_follows_relationships_to_a_relocated_sheet(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    wb.active.append([1, 2, 3])
    good = tmp_path / "good.xlsx"
    wb.save(good)

    moved = tmp_path / "moved-truncated.xlsx"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(moved, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            name = item.filename
            if name == "xl/worksheets/sheet1.xml":
                name = "xl/sheets/sheet1.xml"
                data = data.replace(b"</worksheet>", b"")
            elif name in ("xl/_rels/workbook.xml.rels", "[Content_Types].xml"):
                data = data.replace(b"worksheets/sheet1.xml", b"sheets/sheet1.xml")
            dst.writestr(name, data)

    result = _run_readable(str(moved))
    assert result.returncode == 1, result.stderr
    assert "corrupt XML" in result.stderr
