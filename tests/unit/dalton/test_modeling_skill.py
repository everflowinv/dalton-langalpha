"""Unit & closed-loop tests for the ``dalton-model`` financial modeling skill (Batch 1).

Covers Design §9.1 and §9.3 acceptance criteria plus Codex r3 refinements:
- Skill registration & 3-capability combination assembly (zero proprietary leaks in Core)
- Shared ``PeriodMap`` (non-calendar FY, duplicate headers, mixed suffixes, CAGR, knowledge norm)
- Theme+tint RGB resolution, full-sheet profiling, non-empty ``Futu>>`` 11pt + General preservation
- Package-level XML patcher preserving ``<extLst>`` & ``.wmf`` media + shifting ext/chart refs
- 4-layer external-link cleanup & reopenability
- ``model_lineage.json`` final_delivery tracking & baseline resolution
- Real benchmark cell suite (``Model - Futu!P35``, ``Model - JD!BI157``, ``Model - JD!Q272``,
  ``Model - BABA!S200/S202``, ``Subs - JD!R1/S1``, ``Model - Tiger!G95``, ``Model - PDD!N16``,
  ``EPADS *8``, noted zero) + painted-assumption hardcoded amount rejection
- Baseline legal transitions (actual switch, plugin replacement, registered blank-forecast)
  vs formula regressions
- Hidden literals (incl. ``=E10*2`` & ``IF`` branch literal), summed-year rules, circular refs
  (iterate flag, direct cycle, definedName, static OFFSET), Checks independent recompute &
  dummy-formula rejection, script-level warnings vs ``Checks!Overall`` exit codes
- Cross-period rollforward continuity, 3-statement cash/buyback linkage, BS balance,
  segment tie-out, implied rate historical band (150 bps) & unit-mismatch suspect,
  summary links, foreign template fill, and ``recalc.py`` -> ``style_audit.py --strict`` loop.
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
from openpyxl.utils import get_column_letter
import pytest

from src.dalton.capabilities import CORE_CAPABILITY, CapabilityId
from src.dalton.content_assembly import read_assembled_text

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model"
SCRIPTS_DIR = SKILL_DIR / "scripts"
RECALC_SCRIPT = REPO_ROOT / "plugins" / "langalpha_deliverables" / "skills" / "xlsx" / "scripts" / "recalc.py"

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


def _load_script_module(name: str, path: Path) -> Any:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ms = _load_script_module("model_style", SCRIPTS_DIR / "model_style.py")
sa = _load_script_module("style_audit", SCRIPTS_DIR / "style_audit.py")


# ---------------------------------------------------------------------------
# Helpers: Synthetic Compliant Workbook Builder & XML Cache Injector
# ---------------------------------------------------------------------------


def _inject_cached_values(xlsx_path: Path, cache_map: dict[str, dict[str, tuple[str | None, str]]]) -> None:
    """Write OOXML cached ``<v>`` values into formula cells so offline tests run without LibreOffice."""
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        infos = zf.infolist()
        payload = {i.filename: zf.read(i.filename) for i in infos}

    sp_map = ms._sheet_part_map(payload)
    for sh_name, cells in cache_map.items():
        part = sp_map[sh_name]
        root = etree.fromstring(payload[part], ms.XML_PARSER)
        for c_el in root.iter(f"{{{ms.SML}}}c"):
            coord = (c_el.get("r") or "").upper()
            if coord not in cells:
                continue
            t_attr, v_txt = cells[coord]
            if t_attr is not None:
                c_el.set("t", t_attr)
            elif "t" in c_el.attrib:
                del c_el.attrib["t"]
            v_el = c_el.find(f"{{{ms.SML}}}v")
            if v_el is None:
                v_el = etree.SubElement(c_el, f"{{{ms.SML}}}v")
            v_el.text = v_txt
        payload[part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    with zipfile.ZipFile(xlsx_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename], compress_type=info.compress_type)


def _add_valid_checks_sheet(wb: openpyxl.Workbook, model_sheet: str = "Model", summary_sheet: str = "Summary") -> dict[str, tuple[str | None, str]]:
    """Add a fully compliant 10-row Checks worksheet and return its cached values."""
    ws = wb.create_sheet("Checks")
    ws.views.sheetView[0].showGridLines = False
    ws.append(["check_id", "scope", "status", "diff_or_note"])
    rows = [
        ("annual_vs_quarters", f"{model_sheet}!F3 = SUM({model_sheet}!H3:K3)", f'=IF(ABS({model_sheet}!F3-SUM({model_sheet}!H3:K3))<0.5,"OK","FAIL")', "0"),
        ("segment_tieout", "Single business line", '="N/A"', "Single-segment model; no segment breakdown"),
        ("cash_rollforward", f"{model_sheet}!F10", f'=IF(ABS({model_sheet}!F10-{model_sheet}!K10)<0.5,"OK","FAIL")', "0"),
        ("debt_rollforward", "Zero debt company", '="N/A"', "No interest-bearing debt"),
        ("share_rollforward", f"{model_sheet}!F13", f'=IF(ABS({model_sheet}!F13-{model_sheet}!K13)<0.5,"OK","FAIL")', "0"),
        ("bs_balance", "Simplified cash rollforward", '="N/A"', "Simplified cash/share rollforward without full BS"),
        ("implied_rates", f"{model_sheet}!F7", f'=IF(ABS({model_sheet}!F7/{model_sheet}!E10-0.025)<0.015,"OK","WARN")', "Implied cash yield within historical band"),
        ("summary_links", f"{summary_sheet}!F3 = {model_sheet}!F3", f'=IF(ABS({summary_sheet}!F3-{model_sheet}!F3)<0.5,"OK","FAIL")', "0"),
        ("Overall", "Rollup of C2:C9", '=IF(COUNTIF(C2:C9,"FAIL")>0,"FAIL",IF(COUNTIF(C2:C9,"WARN")>0,"WARN","OK"))', ""),
    ]
    for r_i, (cid, sc, fm, note) in enumerate(rows, start=2):
        ws.cell(r_i, 1, cid)
        ws.cell(r_i, 2, sc)
        ws.cell(r_i, 3, fm)
        ws.cell(r_i, 4, note)
        for c_i in range(1, 5):
            ws.cell(r_i, c_i).font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE)
    for c_i in range(1, 5):
        ms.apply_cell_style(ws.cell(1, c_i), "header")

    return {
        "C2": ("str", "OK"),
        "C3": ("str", "N/A"),
        "C4": ("str", "OK"),
        "C5": ("str", "N/A"),
        "C6": ("str", "OK"),
        "C7": ("str", "N/A"),
        "C8": ("str", "OK"),
        "C9": ("str", "OK"),
        "C10": ("str", "OK"),
    }


def _build_clean_minimal_model(path: Path, *, inject_cache: bool = True) -> dict[str, dict[str, tuple[str | None, str]]]:
    """Create a minimal 100% compliant model (Model + Summary + Checks) for closed-loop tests."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws.views.sheetView[0].showGridLines = False
    ws.freeze_panes = "E2"

    # Row 1 headers: A1 unit, E=2025, F=2026E (summed), G=spacer, H..K=1Q26E..4Q26E, L=source note
    headers = {1: "(RMB MM)", 5: 2025, 6: "2026E", 8: "1Q26E", 9: "2Q26E", 10: "3Q26E", 11: "4Q26E", 12: "Source"}
    for col, val in headers.items():
        c = ws.cell(1, col, val)
        ms.apply_cell_style(c, "header" if col != 12 else "source_note")

    # Row 2: Revenue YoY driver (2025 actual 10%, Q1..Q4 assump 10%, 2026E derived =F3/E3-1)
    ws.cell(2, 4, "Revenue YoY").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, italic=True)
    ws.cell(2, 5, 0.10)
    ms.apply_cell_style(ws.cell(2, 5), "historical_actual", is_ratio=True, number_format=ms.FMT_PCT)
    ws.cell(2, 6, "=F3/E3-1")
    ms.apply_cell_style(ws.cell(2, 6), "financial_output", is_ratio=True, number_format=ms.FMT_PCT)
    for qc in (8, 9, 10, 11):
        ws.cell(2, qc, 0.10)
        ms.apply_cell_style(ws.cell(2, qc), "driver_assumption", is_percent=True, number_format=ms.FMT_PCT)

    # Row 3: Total Revenue (2025=400, Q1..Q4 = 110 via formula =E3/4*(1+H2), 2026E = SUM(H3:K3))
    ws.cell(3, 4, "Total Revenue").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, bold=True)
    ws.cell(3, 5, 400.0)
    ms.apply_cell_style(ws.cell(3, 5), "historical_actual", is_total=True, number_format=ms.FMT_INT)
    ws.cell(3, 6, "=SUM(H3:K3)")
    ms.apply_cell_style(ws.cell(3, 6), "financial_output", is_total=True, number_format=ms.FMT_INT)
    for qc in (8, 9, 10, 11):
        ql = get_column_letter(qc)
        ws.cell(3, qc, f"=$E$3/4*(1+{ql}2)")
        ms.apply_cell_style(ws.cell(3, qc), "financial_output", is_total=True, number_format=ms.FMT_INT)

    # Row 4: Operating Margin driver (20%)
    ws.cell(4, 4, "Operating Margin").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, italic=True)
    ws.cell(4, 5, 0.20)
    ms.apply_cell_style(ws.cell(4, 5), "historical_actual", is_ratio=True, number_format=ms.FMT_PCT)
    ws.cell(4, 6, "=F5/F3")
    ms.apply_cell_style(ws.cell(4, 6), "financial_output", is_ratio=True, number_format=ms.FMT_PCT)
    for qc in (8, 9, 10, 11):
        ws.cell(4, qc, 0.20)
        ms.apply_cell_style(ws.cell(4, qc), "driver_assumption", is_percent=True, number_format=ms.FMT_PCT)

    # Row 5: Operating Income
    ws.cell(5, 4, "Operating Income").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, bold=True)
    ws.cell(5, 5, 80.0)
    ms.apply_cell_style(ws.cell(5, 5), "historical_actual", is_total=True, number_format=ms.FMT_INT)
    ws.cell(5, 6, "=SUM(H5:K5)")
    ms.apply_cell_style(ws.cell(5, 6), "financial_output", is_total=True, number_format=ms.FMT_INT)
    for qc in (8, 9, 10, 11):
        ql = get_column_letter(qc)
        ws.cell(5, qc, f"={ql}3*{ql}4")
        ms.apply_cell_style(ws.cell(5, qc), "financial_output", is_total=True, number_format=ms.FMT_INT)

    # Row 6: Deposit Rate driver (2.5%)
    ws.cell(6, 4, "Deposit Interest Rate").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, italic=True)
    ws.cell(6, 5, 0.025)
    ms.apply_cell_style(ws.cell(6, 5), "historical_actual", is_ratio=True, number_format=ms.FMT_PCT)
    ws.cell(6, 6, "=F7/E10")
    ms.apply_cell_style(ws.cell(6, 6), "financial_output", is_ratio=True, number_format=ms.FMT_PCT)
    for qc in (8, 9, 10, 11):
        ws.cell(6, qc, 0.025)
        ms.apply_cell_style(ws.cell(6, qc), "driver_assumption", is_percent=True, number_format=ms.FMT_PCT)

    # Row 7: Interest Income (non-circular beginning cash * rate * 0.25)
    ws.cell(7, 4, "Interest Income").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE)
    ws.cell(7, 5, 25.0)
    ms.apply_cell_style(ws.cell(7, 5), "historical_actual", number_format=ms.FMT_INT)
    ws.cell(7, 6, "=SUM(H7:K7)")
    ms.apply_cell_style(ws.cell(7, 6), "financial_output", number_format=ms.FMT_INT)
    for qc in (8, 9, 10, 11):
        ql = get_column_letter(qc)
        ws.cell(7, qc, f"={ql}9*{ql}6*0.25")
        ms.apply_cell_style(ws.cell(7, qc), "financial_output", number_format=ms.FMT_INT)

    # Row 8: Net Income = OP + Interest
    ws.cell(8, 4, "Net Income").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, bold=True)
    ws.cell(8, 5, 105.0)
    ms.apply_cell_style(ws.cell(8, 5), "historical_actual", is_total=True, number_format=ms.FMT_INT)
    ws.cell(8, 6, "=SUM(H8:K8)")
    ms.apply_cell_style(ws.cell(8, 6), "financial_output", is_total=True, number_format=ms.FMT_INT)
    for qc in (8, 9, 10, 11):
        ql = get_column_letter(qc)
        ws.cell(8, qc, f"={ql}5+{ql}7")
        ms.apply_cell_style(ws.cell(8, qc), "financial_output", is_total=True, number_format=ms.FMT_INT)

    # Row 9: Beginning Cash (annual F9 = E10; Q1 H9 = E10, Q2..Q4 = prev ending cash)
    ws.cell(9, 4, "Beginning Cash").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE)
    ws.cell(9, 5, 1000.0)
    ms.apply_cell_style(ws.cell(9, 5), "historical_actual", number_format=ms.FMT_INT)
    ws.cell(9, 6, "=E10")
    ms.apply_cell_style(ws.cell(9, 6), "financial_output", number_format=ms.FMT_INT)
    ws.cell(9, 8, "=E10")
    ms.apply_cell_style(ws.cell(9, 8), "financial_output", number_format=ms.FMT_INT)
    for qc in (9, 10, 11):
        prev_l = get_column_letter(qc - 1)
        ws.cell(9, qc, f"={prev_l}10")
        ms.apply_cell_style(ws.cell(9, qc), "financial_output", number_format=ms.FMT_INT)

    # Row 10: Ending Cash (= Beg Cash + Net Income; summed year = K10)
    ws.cell(10, 4, "Ending Cash").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, bold=True)
    ws.cell(10, 5, 1000.0)
    ms.apply_cell_style(ws.cell(10, 5), "historical_actual", is_total=True, number_format=ms.FMT_INT)
    ws.cell(10, 6, "=K10")
    ms.apply_cell_style(ws.cell(10, 6), "financial_output", is_total=True, number_format=ms.FMT_INT)
    for qc in (8, 9, 10, 11):
        ql = get_column_letter(qc)
        ws.cell(10, qc, f"={ql}9+{ql}8")
        ms.apply_cell_style(ws.cell(10, qc), "financial_output", is_total=True, number_format=ms.FMT_INT)

    # Row 11: Net Change in Cash (= Net Income; summed year = SUM(H11:K11))
    ws.cell(11, 4, "Net Change in Cash").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE)
    ws.cell(11, 5, 105.0)
    ms.apply_cell_style(ws.cell(11, 5), "historical_actual", number_format=ms.FMT_INT)
    ws.cell(11, 6, "=SUM(H11:K11)")
    ms.apply_cell_style(ws.cell(11, 6), "financial_output", number_format=ms.FMT_INT)
    for qc in (8, 9, 10, 11):
        ql = get_column_letter(qc)
        ws.cell(11, qc, f"={ql}8")
        ms.apply_cell_style(ws.cell(11, qc), "financial_output", number_format=ms.FMT_INT)

    # Row 12: Beginning Shares / Row 13: Ending Shares / Row 14: Weighted Average Diluted Shares / Row 15: Diluted EPS
    ws.cell(12, 4, "Beginning Shares").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE)
    ws.cell(12, 5, 100.0)
    ms.apply_cell_style(ws.cell(12, 5), "historical_actual", number_format=ms.FMT_DEC1)
    ws.cell(12, 6, "=E13")
    ms.apply_cell_style(ws.cell(12, 6), "financial_output", number_format=ms.FMT_DEC1)
    ws.cell(12, 8, "=E13")
    ms.apply_cell_style(ws.cell(12, 8), "financial_output", number_format=ms.FMT_DEC1)
    for qc in (9, 10, 11):
        ws.cell(12, qc, f"={get_column_letter(qc - 1)}13")
        ms.apply_cell_style(ws.cell(12, qc), "financial_output", number_format=ms.FMT_DEC1)

    ws.cell(13, 4, "Ending Shares").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE)
    ws.cell(13, 5, 100.0)
    ms.apply_cell_style(ws.cell(13, 5), "historical_actual", number_format=ms.FMT_DEC1)
    ws.cell(13, 6, "=K13")
    ms.apply_cell_style(ws.cell(13, 6), "financial_output", number_format=ms.FMT_DEC1)
    for qc in (8, 9, 10, 11):
        ql = get_column_letter(qc)
        ws.cell(13, qc, f"={ql}12")
        ms.apply_cell_style(ws.cell(13, qc), "financial_output", number_format=ms.FMT_DEC1)

    ws.cell(14, 4, "Weighted Average Diluted Shares").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE)
    ws.cell(14, 5, 100.0)
    ms.apply_cell_style(ws.cell(14, 5), "historical_actual", number_format=ms.FMT_DEC1)
    ws.cell(14, 6, "=AVERAGE(H14:K14)")
    ms.apply_cell_style(ws.cell(14, 6), "financial_output", number_format=ms.FMT_DEC1)
    for qc in (8, 9, 10, 11):
        ql = get_column_letter(qc)
        ws.cell(14, qc, f"=AVERAGE({ql}12,{ql}13)")
        ms.apply_cell_style(ws.cell(14, qc), "financial_output", number_format=ms.FMT_DEC1)

    ws.cell(15, 4, "Diluted EPS").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, bold=True)
    ws.cell(15, 5, 1.05)
    ms.apply_cell_style(ws.cell(15, 5), "historical_actual", is_total=True, number_format=ms.FMT_DEC2)
    ws.cell(15, 6, "=F8/F14")
    ms.apply_cell_style(ws.cell(15, 6), "financial_output", is_total=True, number_format=ms.FMT_DEC2)
    for qc in (8, 9, 10, 11):
        ql = get_column_letter(qc)
        ws.cell(15, qc, f"={ql}8/{ql}14")
        ms.apply_cell_style(ws.cell(15, qc), "financial_output", is_total=True, number_format=ms.FMT_DEC2)

    # Summary sheet (annual + quarterly forecast links matching periods)
    ws_s = wb.create_sheet("Summary")
    ws_s.views.sheetView[0].showGridLines = False
    ws_s.freeze_panes = "E2"
    for col, val in headers.items():
        if col == 12:
            continue
        c = ws_s.cell(1, col, val)
        ms.apply_cell_style(c, "header")
    ws_s.cell(3, 4, "Total Revenue").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, bold=True)
    ws_s.cell(4, 4, "Diluted EPS").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, bold=True)
    for col in (5, 6, 8, 9, 10, 11):
        cl = get_column_letter(col)
        ws_s.cell(3, col, f"=Model!{cl}3")
        ms.apply_cell_style(ws_s.cell(3, col), "financial_output", is_link=True, is_total=True, number_format=ms.FMT_INT)
        ws_s.cell(4, col, f"=Model!{cl}15")
        ms.apply_cell_style(ws_s.cell(4, col), "financial_output", is_link=True, is_total=True, number_format=ms.FMT_DEC2)

    chk_cache = _add_valid_checks_sheet(wb)
    wb.calculation.iterate = False
    wb.save(path)

    # Compute exact cached values for Model & Summary
    q_beg = [1000.0]
    q_int, q_ni, q_end = [], [], []
    for _ in range(4):
        bc = q_beg[-1]
        ii = bc * 0.025 * 0.25
        ni = 22.0 + ii
        ec = bc + ni
        q_int.append(ii)
        q_ni.append(ni)
        q_end.append(ec)
        q_beg.append(ec)
    ann_int = sum(q_int)
    ann_ni = sum(q_ni)
    ann_end = q_end[-1]

    model_cache: dict[str, tuple[str | None, str]] = {
        "F2": (None, "0.1"),
        "F3": (None, "440"),
        "F4": (None, "0.2"),
        "F5": (None, "88"),
        "F6": (None, repr(ann_int / 1000.0)),
        "F7": (None, repr(ann_int)),
        "F8": (None, repr(ann_ni)),
        "F9": (None, "1000"),
        "F10": (None, repr(ann_end)),
        "F11": (None, repr(ann_ni)),
        "F12": (None, "100"),
        "F13": (None, "100"),
        "F14": (None, "100"),
        "F15": (None, repr(ann_ni / 100.0)),
    }
    summary_cache: dict[str, tuple[str | None, str]] = {
        "E3": (None, "400"), "E4": (None, "1.05"),
        "F3": (None, "440"), "F4": (None, repr(ann_ni / 100.0)),
    }
    for i, qc in enumerate((8, 9, 10, 11)):
        ql = get_column_letter(qc)
        model_cache[f"{ql}3"] = (None, "110")
        model_cache[f"{ql}5"] = (None, "22")
        model_cache[f"{ql}7"] = (None, repr(q_int[i]))
        model_cache[f"{ql}8"] = (None, repr(q_ni[i]))
        model_cache[f"{ql}9"] = (None, repr(q_beg[i]))
        model_cache[f"{ql}10"] = (None, repr(q_end[i]))
        model_cache[f"{ql}11"] = (None, repr(q_ni[i]))
        model_cache[f"{ql}12"] = (None, "100")
        model_cache[f"{ql}13"] = (None, "100")
        model_cache[f"{ql}14"] = (None, "100")
        model_cache[f"{ql}15"] = (None, repr(q_ni[i] / 100.0))
        summary_cache[f"{ql}3"] = (None, "110")
        summary_cache[f"{ql}4"] = (None, repr(q_ni[i] / 100.0))

    caches = {"Model": model_cache, "Summary": summary_cache, "Checks": chk_cache}
    if inject_cache:
        _inject_cached_values(path, caches)
    return caches


# ---------------------------------------------------------------------------
# 1. Skill Registration & Three Capability Combinations (§9.1 #1, §9.3 #6)
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

    # Combo 1: Standalone Core (frozenset()) — zero proprietary hits across skill + references + conventions
    core_caps: frozenset[CapabilityId] = frozenset()
    for fp in [*md_files, conventions_md]:
        text = read_assembled_text(fp, capabilities=core_caps)
        assert "DALTON_CAP" not in text, f"Unresolved marker in {fp}"
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

    # Combo 2: Bridge ON, AlphaEngine OFF — bridge consensus active, broker falls back to public, 0 alphaengine hits
    bridge_only = frozenset({CapabilityId.RESEARCHDATA_BRIDGE})
    for fp in md_files:
        text = read_assembled_text(fp, capabilities=bridge_only)
        assert "DALTON_CAP" not in text
        assert "alphaengine" not in text.lower(), f"alphaengine leaked in {fp} when disabled"
    sanity_bridge = read_assembled_text(SKILL_DIR / "references" / "sanity-check.md", capabilities=bridge_only)
    assert 'researchdata.finance(route="estimates"' in sanity_bridge
    assert "web_search" in sanity_bridge

    # Combo 3: Bridge ON + AlphaEngine ON — both internal blocks active, 0 markers
    both = frozenset({CapabilityId.RESEARCHDATA_BRIDGE, CapabilityId.ALPHAENGINE})
    sanity_both = read_assembled_text(SKILL_DIR / "references" / "sanity-check.md", capabilities=both)
    assert "DALTON_CAP" not in sanity_both
    assert 'researchdata.finance(route="estimates"' in sanity_both
    assert 'researchdata.research(route="alphaengine"' in sanity_both


# ---------------------------------------------------------------------------
# 2. PeriodMap, Theme+Tint RGB, Profiling, XML Patcher, External Links & Lineage
# ---------------------------------------------------------------------------


def test_period_map_non_calendar_fy_duplicates_suffixes_and_cagr() -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model - BABA"
    ws.cell(1, 1, "(RMB MM)")
    ws.cell(2, 1, "FY ends March")
    # Annuals with mixed suffixes + duplicate 2026E + CAGR + cross-calendar quarters (2Q26E..1Q27E)
    hdrs = [
        (5, 2025),
        (6, "2026A"),
        (7, "2027E"),
        (8, "2027E"),  # duplicate label (like Subs - JD!R1/S1)
        (9, "2028F"),
        (10, "2029P"),
        (12, "CAGR 25-28E"),
        (14, "2Q26E"),
        (15, "3Q26E"),
        (16, "4Q26E"),
        (17, "1Q27E"),
    ]
    for col, lbl in hdrs:
        ws.cell(1, col, lbl)

    pmap = ms.build_period_map(ws)
    assert pmap.fiscal_year_end_month == 3
    assert pmap.fiscal_year_end_quarter == 1
    assert pmap.main_columns[5].is_forecast is False
    assert pmap.main_columns[6].is_forecast is False
    assert pmap.main_columns[7].is_forecast is True
    assert pmap.main_columns[9].is_forecast is True
    assert pmap.main_columns[10].is_forecast is True
    assert pmap.main_columns[12].kind == "cagr_or_delta"
    assert pmap.main_columns[12].is_forecast is False
    # Duplicate 2027E headers must have distinct stable column_ids
    assert pmap.main_columns[7].column_id != pmap.main_columns[8].column_id
    # Cross-calendar fiscal year binding: 2027E -> [2Q26E, 3Q26E, 4Q26E, 1Q27E]
    assert pmap.annual_to_quarters[7] == [14, 15, 16, 17]

    # Knowledge normalization matches common.schema.json regex
    assert ms.normalize_period_for_knowledge("2026") == "FY2026"
    assert ms.normalize_period_for_knowledge("2026E") == "FY2026"
    assert ms.normalize_period_for_knowledge("2026F") == "FY2026"
    assert ms.normalize_period_for_knowledge("4Q26E") == "4Q26"
    assert ms.normalize_period_for_knowledge("2026Q4E") == "4Q26"
    assert ms.normalize_period_for_knowledge("CAGR 25-28E") is None


def test_theme_tint_resolution_and_full_sheet_style_profiling(tmp_path: Path) -> None:
    # Verify theme+tint resolution via palette directly
    from openpyxl.styles.colors import Color

    palette = {1: "000000", 6: "A5A5A5"}
    assert ms.resolve_color_rgb(Color(theme=1, tint=0.0), palette) == "000000"
    gray_est = ms.resolve_color_rgb(Color(theme=6, tint=0.80), palette)
    dark_hdr = ms.resolve_color_rgb(Color(theme=6, tint=-0.25), palette)
    assert ms.is_color_close(gray_est, "EDEDED", max_channel_diff=4)
    assert not ms.is_color_close(dark_hdr, "EDEDED", max_channel_diff=30)

    # Build multi-sheet workbook with: empty PDD>>, non-empty 11pt Futu>> with General formulas,
    # Subs with row-60 blue sub-header, row-400 segment block, and cross-sheet green italic formula
    wb = openpyxl.Workbook()
    ws_div = wb.active
    ws_div.title = "PDD>>"

    ws_futu_div = wb.create_sheet("Futu>>")
    for r in range(1, 6):
        c = ws_futu_div.cell(r, 2, f"=100+{r}")
        c.font = Font(name="等线", size=11)
        c.number_format = "General"

    ws_subs = wb.create_sheet("Subs - JD")
    ws_subs.views.sheetView[0].showGridLines = False
    ws_subs.cell(1, 5, 2025)
    ws_subs.cell(1, 6, "2026E")
    # Row 60 sub-block header (99CCFF fill with years)
    for idx, yr in enumerate((2023, 2024, 2025, 2026), start=5):
        c = ws_subs.cell(60, idx, yr)
        c.fill = PatternFill(fill_type="solid", fgColor="99CCFF")
        c.font = Font(name="Arial", size=8, bold=True)
    # Row 400 deep segment block (must NOT be truncated at row 300!)
    ws_subs.cell(400, 1, "Deep Segment Block").fill = PatternFill(fill_type="solid", fgColor="FFCC99")
    ws_subs.cell(401, 4, "Interest rate")
    c_ratio = ws_subs.cell(401, 6, "='Futu>>'!B1/100")
    c_ratio.font = Font(name="Arial", size=8, italic=True, color="008000")
    c_ratio.number_format = "0.0%"

    xlsx = tmp_path / "profile_test.xlsx"
    wb.save(xlsx)

    prof = ms.profile_workbook_style(xlsx)
    assert prof["sheet_overrides"]["PDD>>"]["sheet_type"] == "blank_divider"
    assert prof["sheet_overrides"]["Futu>>"]["sheet_type"] == "calculation_sheet"
    assert prof["sheet_overrides"]["Futu>>"]["dominant_font_size"] == 11.0
    assert prof["sheet_overrides"]["Futu>>"]["has_general_formula_nfmt"] is True
    assert 60 in prof["sheet_overrides"]["Subs - JD"]["period_headers"]["sub_header_rows"]
    assert "FFCC99" in prof["sheet_overrides"]["Subs - JD"]["section_fills"]


def test_patch_workbook_xml_preserves_extlst_wmf_and_shifts_refs(tmp_path: Path) -> None:
    src = tmp_path / "with_ext.xlsx"
    out = tmp_path / "patched.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws.cell(1, 5, "2026E")
    ws.cell(2, 5, 100)
    wb.calculation.iterate = True
    wb.save(src)

    # Inject <extLst> conditional formatting with <xm:sqref>, a dummy .wmf media part, and a chart part
    with zipfile.ZipFile(src, "r") as zf:
        payload = {n: zf.read(n) for n in zf.namelist()}

    ws_root = etree.fromstring(payload["xl/worksheets/sheet1.xml"], ms.XML_PARSER)
    ext_lst = etree.SubElement(ws_root, f"{{{ms.SML}}}extLst")
    ext = etree.SubElement(ext_lst, f"{{{ms.SML}}}ext", uri="{78C0D931-6437-407d-A8EE-F3AAD75D9E65}")
    cfs = etree.SubElement(ext, f"{{{ms.X14_NS}}}conditionalFormattings")
    cf = etree.SubElement(cfs, f"{{{ms.X14_NS}}}conditionalFormatting")
    sq = etree.SubElement(cf, f"{{{ms.XM_NS}}}sqref")
    sq.text = "E2:F10"
    payload["xl/worksheets/sheet1.xml"] = etree.tostring(ws_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    payload["xl/media/image1.wmf"] = b"FAKE-WMF-BINARY-HEADER-12345"
    payload["xl/charts/chart1.xml"] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">'
        b'<c:chart><c:plotArea><c:ser><c:val><c:numRef><c:f>Model!$E$2:$F$2</c:f></c:numRef></c:val></c:ser></c:plotArea></c:chart>'
        b'</c:chartSpace>'
    )

    with zipfile.ZipFile(src, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for k, v in payload.items():
            zout.writestr(k, v)

    res = ms.patch_workbook_xml(
        src,
        out,
        cell_updates={"Model": {"F2": "=E2*2"}},
        disable_iterative_calc=True,
        shift_specs=[{"sheet": "Model", "at_col": 5, "count": 1}],
    )
    assert res["status"] == "success"
    assert res["part_diff"]["extlst_preserved"] is True
    assert res["part_diff"]["media_preserved"] is True
    assert res["part_diff"]["charts_preserved"] is True
    assert res["shift_totals"]["ext_sqrefs_updated"] == 1
    assert res["shift_totals"]["chart_refs_updated"] == 1

    with zipfile.ZipFile(out, "r") as zf:
        assert zf.read("xl/media/image1.wmf") == b"FAKE-WMF-BINARY-HEADER-12345"
        ws_xml = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "F2:G10" in ws_xml
        ch_xml = zf.read("xl/charts/chart1.xml").decode("utf-8")
        assert "Model!$F$2:$G$2" in ch_xml

    # Verify patched workbook reopens cleanly with openpyxl and iterate is disabled
    wb_re = openpyxl.load_workbook(out, data_only=False)
    assert wb_re["Model"]["F2"].value == "=E2*2"
    assert not bool(wb_re.calculation.iterate)


def test_clean_external_links_four_layers_and_reopen(tmp_path: Path) -> None:
    src = tmp_path / "with_ext_links.xlsx"
    out = tmp_path / "cleaned.xlsx"
    _build_clean_minimal_model(src, inject_cache=True)

    # Inject all 4 OOXML externalLink layers
    with zipfile.ZipFile(src, "r") as zf:
        payload = {n: zf.read(n) for n in zf.namelist()}

    wb_root = etree.fromstring(payload["xl/workbook.xml"], ms.XML_PARSER)
    ext_refs = etree.SubElement(wb_root, f"{{{ms.SML}}}externalReferences")
    etree.SubElement(ext_refs, f"{{{ms.SML}}}externalReference", {f"{{{ms.REL_NS}}}id": "rId99"})
    payload["xl/workbook.xml"] = etree.tostring(wb_root, xml_declaration=True, encoding="UTF-8", standalone=True)

    rels_root = etree.fromstring(payload["xl/_rels/workbook.xml.rels"], ms.XML_PARSER)
    etree.SubElement(
        rels_root,
        f"{{{ms.PKG_REL_NS}}}Relationship",
        Id="rId99",
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink",
        Target="externalLinks/externalLink1.xml",
    )
    payload["xl/_rels/workbook.xml.rels"] = etree.tostring(rels_root, xml_declaration=True, encoding="UTF-8", standalone=True)

    ct_root = etree.fromstring(payload["[Content_Types].xml"], ms.XML_PARSER)
    etree.SubElement(
        ct_root,
        f"{{{ms.CT_NS}}}Override",
        PartName="/xl/externalLinks/externalLink1.xml",
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.externalLink+xml",
    )
    payload["[Content_Types].xml"] = etree.tostring(ct_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    payload["xl/externalLinks/externalLink1.xml"] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<externalLink xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<externalBook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="rId1"/>'
        b'</externalLink>'
    )
    payload["xl/externalLinks/_rels/externalLink1.xml.rels"] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLinkPath" Target="other.xlsx" TargetMode="External"/>'
        b'</Relationships>'
    )

    with zipfile.ZipFile(src, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for k, v in payload.items():
            zout.writestr(k, v)

    # Static pre-check MUST catch all 4 layers
    pre_rep = sa.audit_workbook(src, static_only=True)
    ext_finding = next(f for f in pre_rep["findings"] if f["check"] == "external_or_plugin_link")
    assert ext_finding["count"] >= 4

    # Clean all 4 layers
    clean_res = ms.patch_workbook_xml(src, out, remove_external_links=True)
    assert clean_res["external_links_removed"]["external_link_parts"] == 2
    assert clean_res["external_links_removed"]["workbook_external_refs"] == 1
    assert clean_res["external_links_removed"]["workbook_rels"] == 1
    assert clean_res["external_links_removed"]["content_types"] == 1
    assert clean_res["part_diff"]["new_external_link_parts"] == 0

    post_rep = sa.audit_workbook(out, static_only=True)
    assert not any(f["check"] == "external_or_plugin_link" for f in post_rep["findings"])
    wb_re = openpyxl.load_workbook(out, data_only=False)
    assert "Model" in wb_re.sheetnames


def test_model_lineage_records_final_delivery_and_resolves_baseline(tmp_path: Path) -> None:
    v02 = tmp_path / "ACME-model-v02.xlsx"
    v03 = tmp_path / "ACME-model-v03.xlsx"
    v04 = tmp_path / "ACME-model-v04.xlsx"
    _build_clean_minimal_model(v02)
    _build_clean_minimal_model(v03)
    _build_clean_minimal_model(v04)

    lin_path = tmp_path / "model_lineage.json"
    ms.record_model_lineage(lin_path, output_path=v03, baseline_path=v02, final_delivery=False)
    data = ms.record_model_lineage(lin_path, output_path=v04, baseline_path=v02, final_delivery=True)

    assert data["final_delivery"]["output_path"] == str(v04)
    assert data["final_delivery"]["output_sha256"] == ms.file_sha256(v04)
    assert data["final_delivery"]["baseline_path"] == str(v02)

    # Audit using --lineage MUST use v02 as baseline (NOT v03!)
    rep = sa.audit_workbook(v04, strict=True, lineage_path=lin_path)
    assert rep["baseline"] == str(v02)
    assert rep["status"] == "pass"


# ---------------------------------------------------------------------------
# 3. Real Benchmark Complex Cells + Painted Hardcode (§9.1 #3, §9.3 #5, Codex r3 #2/#6)
# ---------------------------------------------------------------------------


def test_real_complex_benchmark_cells_pass_and_painted_hardcode_fails(tmp_path: Path) -> None:
    """Fixed benchmark combining 5 real sample patterns + Futu>> General + painted hardcode failure."""
    base_path = tmp_path / "benchmark_base.xlsx"
    model_path = tmp_path / "benchmark_model.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model - Bench"
    ws.views.sheetView[0].showGridLines = False
    ws.freeze_panes = "E2"

    # Non-calendar March FY headers: E=2026 (hist), F=2027E (summed), G=2028E (standalone),
    # I..L = 2Q26E..1Q27E, M = source note
    hdrs = {1: "(RMB MM)", 5: 2026, 6: "2027E", 7: "2028E", 9: "2Q26E", 10: "3Q26E", 11: "4Q26E", 12: "1Q27E", 13: "Source"}
    for col, val in hdrs.items():
        ms.apply_cell_style(ws.cell(1, col, val), "header" if col != 13 else "source_note")
    ws.cell(2, 1, "3月底结账").font = Font(name=ms.FONT_ZH, size=ms.FONT_SIZE)

    # Row 3: Net Income (flow: summed year = SUM(I3:L3), standalone 2028E = F3*(1+G4))
    ws.cell(3, 4, "Net Income").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, bold=True)
    ws.cell(3, 5, 400.0); ms.apply_cell_style(ws.cell(3, 5), "historical_actual", is_total=True, number_format=ms.FMT_INT)
    ws.cell(3, 6, "=SUM(I3:L3)"); ms.apply_cell_style(ws.cell(3, 6), "financial_output", is_total=True, number_format=ms.FMT_INT)
    ws.cell(3, 7, "=F3*(1+G4)"); ms.apply_cell_style(ws.cell(3, 7), "financial_output", is_total=True, number_format=ms.FMT_INT)
    for qc in (9, 10, 11, 12):
        ql = get_column_letter(qc)
        ws.cell(3, qc, f"=$E$3/4*(1+{ql}4)")
        ms.apply_cell_style(ws.cell(3, qc), "financial_output", is_total=True, number_format=ms.FMT_INT)

    # Row 4: Net Income Growth (driver assumption: relative formulas =I4+0.05% like Model - JD!Q272, =E4 like Model - JD!BI157)
    ws.cell(4, 4, "Net Income Growth").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, italic=True)
    ws.cell(4, 5, 0.10); ms.apply_cell_style(ws.cell(4, 5), "historical_actual", is_ratio=True, number_format=ms.FMT_PCT)
    ws.cell(4, 6, "=F3/E3-1"); ms.apply_cell_style(ws.cell(4, 6), "financial_output", is_ratio=True, number_format=ms.FMT_PCT)
    ws.cell(4, 7, "=F4+0.05%"); ms.apply_cell_style(ws.cell(4, 7), "driver_assumption", is_percent=True, number_format=ms.FMT_PCT)
    ws.cell(4, 9, "=E4"); ms.apply_cell_style(ws.cell(4, 9), "driver_assumption", is_percent=True, number_format=ms.FMT_PCT)  # BI157 pattern
    ws.cell(4, 10, "=I4+0.05%"); ms.apply_cell_style(ws.cell(4, 10), "driver_assumption", is_percent=True, number_format=ms.FMT_PCT)  # Q272 pattern
    ws.cell(4, 11, "=J4*1.02"); ms.apply_cell_style(ws.cell(4, 11), "driver_assumption", is_percent=True, number_format=ms.FMT_PCT)  # *1.02 pattern
    ws.cell(4, 12, "=K4*(1+2%)"); ms.apply_cell_style(ws.cell(4, 12), "driver_assumption", is_percent=True, number_format=ms.FMT_PCT)

    # Row 5: Offshore % derived (=E3/E3) & Row 6: Offshore % driver =G5-8% (Model - Futu!P35 pattern!)
    ws.cell(5, 4, "Total Offshore %").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, italic=True)
    ws.cell(5, 5, 0.50); ms.apply_cell_style(ws.cell(5, 5), "historical_actual", is_ratio=True, number_format=ms.FMT_PCT)
    ws.cell(5, 6, "=F3/F3"); ms.apply_cell_style(ws.cell(5, 6), "financial_output", is_ratio=True, number_format=ms.FMT_PCT)
    ws.cell(5, 7, "=G3/F3"); ms.apply_cell_style(ws.cell(5, 7), "financial_output", is_ratio=True, number_format=ms.FMT_PCT)
    for qc in (9, 10, 11, 12):
        ql = get_column_letter(qc)
        ws.cell(5, qc, f"={ql}3/$E$3")
        ms.apply_cell_style(ws.cell(5, qc), "financial_output", is_ratio=True, number_format=ms.FMT_PCT)

    ws.cell(6, 4, "Offshore %").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, italic=True)
    ws.cell(6, 5, 0.42); ms.apply_cell_style(ws.cell(6, 5), "historical_actual", is_ratio=True, number_format=ms.FMT_PCT)
    ws.cell(6, 6, "=F5/F5"); ms.apply_cell_style(ws.cell(6, 6), "financial_output", is_ratio=True, number_format=ms.FMT_PCT)
    ws.cell(6, 7, "=G5-8%"); ms.apply_cell_style(ws.cell(6, 7), "driver_assumption", is_percent=True, number_format=ms.FMT_PCT)  # Futu!P35!
    for qc in (9, 10, 11, 12):
        ql = get_column_letter(qc)
        ws.cell(6, qc, f"={ql}5-8%")
        ms.apply_cell_style(ws.cell(6, qc), "driver_assumption", is_percent=True, number_format=ms.FMT_PCT)

    # Row 7: Weighted Average Diluted Shares & Row 8: EPADS (Method A: NI / Shares * 8 like BABA!S200 + ADS *8)
    # Row 9: FY EPADS (Method B: SUM(I8:L8) like BABA!S202 coexisting on SAME sheet! Codex r3 #2)
    ws.cell(7, 4, "Weighted Average Diluted Shares").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE)
    ws.cell(7, 5, 800.0); ms.apply_cell_style(ws.cell(7, 5), "historical_actual", number_format=ms.FMT_DEC1)
    ws.cell(7, 6, "=AVERAGE(I7:L7)"); ms.apply_cell_style(ws.cell(7, 6), "financial_output", number_format=ms.FMT_DEC1)
    ws.cell(7, 7, "=F7"); ms.apply_cell_style(ws.cell(7, 7), "financial_output", number_format=ms.FMT_DEC1)
    for qc in (9, 10, 11, 12):
        ws.cell(7, qc, "=$E$7"); ms.apply_cell_style(ws.cell(7, qc), "financial_output", number_format=ms.FMT_DEC1)

    ws.cell(8, 4, "Non-GAAP EPADS (USD)").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, bold=True)
    ws.cell(8, 5, 4.00); ms.apply_cell_style(ws.cell(8, 5), "historical_actual", is_total=True, number_format=ms.FMT_DEC2)
    ws.cell(8, 6, "=F3/F7*8"); ms.apply_cell_style(ws.cell(8, 6), "financial_output", is_total=True, number_format=ms.FMT_DEC2)  # BABA!S200 + *8
    ws.cell(8, 7, "=G3/G7*8"); ms.apply_cell_style(ws.cell(8, 7), "financial_output", is_total=True, number_format=ms.FMT_DEC2)
    for qc in (9, 10, 11, 12):
        ql = get_column_letter(qc)
        ws.cell(8, qc, f"={ql}3/{ql}7*8")
        ms.apply_cell_style(ws.cell(8, qc), "financial_output", is_total=True, number_format=ms.FMT_DEC2)

    ws.cell(9, 4, "FY Non-GAAP EPADS (USD)").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, bold=True)
    ws.cell(9, 5, 4.00); ms.apply_cell_style(ws.cell(9, 5), "historical_actual", is_total=True, number_format=ms.FMT_DEC2)
    ws.cell(9, 6, "=SUM(I8:L8)"); ms.apply_cell_style(ws.cell(9, 6), "financial_output", is_total=True, number_format=ms.FMT_DEC2)  # BABA!S202!
    ws.cell(9, 7, "=G8"); ms.apply_cell_style(ws.cell(9, 7), "financial_output", is_total=True, number_format=ms.FMT_DEC2)

    # Row 10: Noted zero assumption (with source note in col 13) + gray historical estimate (=E3, PDD!N16 pattern)
    ws.cell(10, 4, "Payout Ratio").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, italic=True)
    ws.cell(10, 5, "=E5"); ms.apply_cell_style(ws.cell(10, 5), "historical_estimate", hist_est_variant="gray", is_percent=True, number_format=ms.FMT_PCT)
    ws.cell(10, 7, 0.0); ms.apply_cell_style(ws.cell(10, 7), "driver_assumption", is_percent=True, number_format=ms.FMT_PCT)
    ws.cell(10, 13, "<FY25 20-F: board suspended dividend"); ms.apply_cell_style(ws.cell(10, 13), "source_note")

    # Second sheet: Subs - JD with duplicate 2026E headers & green+italic cross-sheet ratio (Model - Tiger!G95)
    ws_sub = wb.create_sheet("Subs - JD")
    ws_sub.views.sheetView[0].showGridLines = False
    ws_sub.cell(1, 5, 2025); ms.apply_cell_style(ws_sub.cell(1, 5), "header")
    ws_sub.cell(1, 6, "2026E"); ms.apply_cell_style(ws_sub.cell(1, 6), "header")
    ws_sub.cell(1, 7, "2026E"); ms.apply_cell_style(ws_sub.cell(1, 7), "header")
    ws_sub.cell(3, 4, "Interest rate").font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE, italic=True)
    ws_sub.cell(3, 5, "='Model - Bench'!E5")
    ms.apply_cell_style(ws_sub.cell(3, 5), "financial_output", is_link=True, is_ratio=True, number_format=ms.FMT_PCT)  # Tiger!G95!
    ws_sub.cell(3, 6, "='Model - Bench'!F5")
    ms.apply_cell_style(ws_sub.cell(3, 6), "financial_output", is_link=True, is_ratio=True, number_format=ms.FMT_PCT)
    ws_sub.cell(3, 7, "='Model - Bench'!G5")
    ms.apply_cell_style(ws_sub.cell(3, 7), "financial_output", is_link=True, is_ratio=True, number_format=ms.FMT_PCT)

    # Third sheet: Futu>> with 11pt General formula (Codex r3 #6)
    ws_fg = wb.create_sheet("Futu>>")
    ws_fg.views.sheetView[0].showGridLines = False
    c_tp = ws_fg.cell(2, 2, "='Model - Bench'!F8*10")
    c_tp.font = Font(name="等线", size=11, color=ms.LINK_FONT)
    c_tp.number_format = "General"

    # Valid Checks sheet
    ws_c = wb.create_sheet("Checks")
    ws_c.views.sheetView[0].showGridLines = False
    ws_c.append(["check_id", "scope", "status", "diff_or_note"])
    chk_rows = [
        ("annual_vs_quarters", "Model - Bench!F3", "=IF(ABS('Model - Bench'!F3-SUM('Model - Bench'!I3:L3))<0.5,\"OK\",\"FAIL\")", "0"),
        ("segment_tieout", "N/A", '="N/A"', "Single benchmark line"),
        ("cash_rollforward", "Model - Bench!F3", "=IF('Model - Bench'!F3>0,\"OK\",\"FAIL\")", "0"),
        ("debt_rollforward", "N/A", '="N/A"', "No debt"),
        ("share_rollforward", "Model - Bench!F7", "=IF('Model - Bench'!F7>0,\"OK\",\"FAIL\")", "0"),
        ("bs_balance", "N/A", '="N/A"', "No full BS"),
        ("implied_rates", "Model - Bench!F5", "=IF('Model - Bench'!F5>=0,\"OK\",\"WARN\")", "OK"),
        ("summary_links", "Subs - JD!F3", "=IF('Subs - JD'!F3='Model - Bench'!F5,\"OK\",\"FAIL\")", "0"),
        ("Overall", "Rollup", '=IF(COUNTIF(C2:C9,"FAIL")>0,"FAIL",IF(COUNTIF(C2:C9,"WARN")>0,"WARN","OK"))', ""),
    ]
    for ri, (cid, sc, fm, nt) in enumerate(chk_rows, start=2):
        ws_c.cell(ri, 1, cid); ws_c.cell(ri, 2, sc); ws_c.cell(ri, 3, fm); ws_c.cell(ri, 4, nt)
        for ci in range(1, 5):
            ws_c.cell(ri, ci).font = Font(name=ms.FONT_EN, size=ms.FONT_SIZE)
    for ci in range(1, 5):
        ms.apply_cell_style(ws_c.cell(1, ci), "header")

    wb.save(base_path)
    wb.save(model_path)

    caches = {
        "Model - Bench": {
            "F3": (None, "440.6"), "G3": (None, "485.3"), "I3": (None, "110"), "J3": (None, "110.05"), "K3": (None, "110.27"), "L3": (None, "110.28"),
            "F4": (None, "0.1015"), "G4": (None, "0.102"), "I4": (None, "0.1"), "J4": (None, "0.1005"), "K4": (None, "0.1025"), "L4": (None, "0.1045"),
            "F5": (None, "1.0"), "G5": (None, "1.1"), "I5": (None, "0.275"), "J5": (None, "0.275"), "K5": (None, "0.275"), "L5": (None, "0.275"),
            "F6": (None, "1.0"), "G6": (None, "1.02"), "I6": (None, "0.195"), "J6": (None, "0.195"), "K6": (None, "0.195"), "L6": (None, "0.195"),
            "F7": (None, "800"), "G7": (None, "800"), "I7": (None, "800"), "J7": (None, "800"), "K7": (None, "800"), "L7": (None, "800"),
            "F8": (None, "4.406"), "G8": (None, "4.853"), "I8": (None, "1.10"), "J8": (None, "1.1005"), "K8": (None, "1.1027"), "L8": (None, "1.1028"),
            "F9": (None, "4.406"), "G9": (None, "4.853"),
            "E10": (None, "0.5"),
        },
        "Subs - JD": {"E3": (None, "0.5"), "F3": (None, "1.0"), "G3": (None, "1.1")},
        "Futu>>": {"B2": (None, "44.06")},
        "Checks": {
            "C2": ("str", "OK"), "C3": ("str", "N/A"), "C4": ("str", "OK"), "C5": ("str", "N/A"),
            "C6": ("str", "OK"), "C7": ("str", "N/A"), "C8": ("str", "OK"), "C9": ("str", "OK"), "C10": ("str", "OK"),
        },
    }
    _inject_cached_values(model_path, caches)

    # Extract baseline profile so Futu>> 11pt & General precedent are preserved (Codex r3 #6)
    prof_path = tmp_path / "bench_profile.json"
    prof_path.write_text(json.dumps(ms.profile_workbook_style(base_path)), encoding="utf-8")

    rep_ok = sa.audit_workbook(model_path, strict=True, profile_path=prof_path)
    assert rep_ok["status"] == "pass", f"Expected clean benchmark pass, got: {rep_ok['findings']}"

    # Now paint Net Income forecast cell G3 as assumption (blue + yellow + hair) with hardcoded amount 999!
    wb_bad = openpyxl.load_workbook(model_path, data_only=False)
    c_bad = wb_bad["Model - Bench"]["G3"]
    c_bad.value = 999.0
    ms.apply_cell_style(c_bad, "driver_assumption", number_format=ms.FMT_INT)
    bad_path = tmp_path / "benchmark_painted_hardcode.xlsx"
    wb_bad.save(bad_path)
    _inject_cached_values(bad_path, caches)

    rep_bad = sa.audit_workbook(bad_path, strict=True, profile_path=prof_path)
    assert rep_bad["status"] == "fail"
    assert any(f["check"] == "forecast_amount_hardcode" for f in rep_bad["findings"])


# ---------------------------------------------------------------------------
# 4. Baseline Legal Transitions vs Formula Regressions (Codex r3 #1)
# ---------------------------------------------------------------------------


def test_baseline_legal_transitions_and_regressions(tmp_path: Path) -> None:
    base = tmp_path / "base.xlsx"
    good = tmp_path / "good.xlsx"
    bad = tmp_path / "bad.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws.views.sheetView[0].showGridLines = False
    ws.cell(1, 5, "2026E"); ms.apply_cell_style(ws.cell(1, 5), "header")
    ws.cell(1, 6, "3Q26E"); ms.apply_cell_style(ws.cell(1, 6), "header")
    ws.cell(1, 7, "4Q26E"); ms.apply_cell_style(ws.cell(1, 7), "header")
    ws.cell(1, 8, "1Q26"); ms.apply_cell_style(ws.cell(1, 8), "header")
    ws.cell(1, 9, "2Q26"); ms.apply_cell_style(ws.cell(1, 9), "header")

    ws.cell(2, 4, "Total Revenue")
    ws.cell(2, 5, "=H2+I2+F2+G2")
    ws.cell(2, 6, "=I2*1.05")
    ws.cell(2, 7, "=F2*1.05")
    ws.cell(2, 8, 100); ws.cell(2, 9, 105)

    # Row 3: FX gain/loss (will become registered blank-forecast in good, unregistered blank in bad)
    ws.cell(3, 4, "FX Gain Loss")
    ws.cell(3, 5, "=H3+I3+F3+G3")
    ws.cell(3, 6, "=I3")
    ws.cell(3, 7, "=F3")
    ws.cell(3, 8, 2); ws.cell(3, 9, -1)

    # Row 4: Plugin formula to be replaced
    ws.cell(4, 4, "Spot Price")
    ws.cell(4, 7, '=_xll.ciqfunctions.udf.CIQ("BABA","IQ_LASTSALEPRICE")')
    wb.save(base)

    # Good update: 3Q26E -> 3Q26 actual, FX Gain Loss blanked WITH registration (exempt from regression & summed_year!),
    # plugin formula replaced with standard formula referencing input cell
    wb_g = openpyxl.load_workbook(base, data_only=False)
    ws_g = wb_g["Model"]
    ws_g.cell(1, 6, "3Q26")  # Forecast -> Actual switch
    ws_g.cell(2, 6, 110); ms.apply_cell_style(ws_g.cell(2, 6), "historical_actual", number_format=ms.FMT_INT)
    ws_g.cell(3, 5).value = None
    ws_g.cell(3, 6).value = 3
    ws_g.cell(3, 7).value = None  # FX Gain Loss blanked in forecast 4Q26E & 2026E
    ws_g.cell(10, 4, "Spot Price Input"); ws_g.cell(10, 7, 95.0)
    ws_g.cell(4, 7, "=G10")
    wb_g.save(good)

    mapping_md = tmp_path / "mapping.md"
    mapping_md.write_text(
        "| Sheet | Row | Period | Reason |\n"
        "|---|---|---|---|\n"
        "| Model | FX Gain Loss | all | 预测期汇兑损益未预测留空 |\n",
        encoding="utf-8",
    )

    rep_g = sa.audit_workbook(good, static_only=True, baseline_path=base, mapping_path=mapping_md)
    assert rep_g["stats"]["verified_actual_switches"] >= 1
    assert rep_g["stats"]["plugin_links_replaced"] >= 1
    assert rep_g["stats"]["baseline_formula_regressions"] == 0
    assert not any(f["check"] == "formula_regression_vs_baseline" for f in rep_g["findings"])
    # Registered blank-forecast must NOT trigger summed_year_violation either (Codex r3 #1)!
    assert not any(
        f["check"] == "summed_year_violation" and any("FX Gain Loss" in ex for ex in f["examples"])
        for f in rep_g["findings"]
    )

    # Bad update: 4Q26E Revenue turned into constant + FX Gain Loss blanked WITHOUT registration
    wb_b = openpyxl.load_workbook(base, data_only=False)
    ws_b = wb_b["Model"]
    ws_b.cell(2, 7).value = 120.0  # formula -> constant regression
    ws_b.cell(3, 7).value = None   # unregistered blank regression
    wb_b.save(bad)

    rep_b = sa.audit_workbook(bad, static_only=True, baseline_path=base)
    assert rep_b["stats"]["baseline_formula_regressions"] >= 2
    assert any(f["check"] == "formula_regression_vs_baseline" for f in rep_b["findings"])


# ---------------------------------------------------------------------------
# 5. Violation Interception Suite (§9.1 #3 items 1–11, §9.3 #3–6)
# ---------------------------------------------------------------------------


def test_violations_hidden_literals_summed_year_zero_note_and_broken_ref(tmp_path: Path) -> None:
    p = tmp_path / "violations.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws.views.sheetView[0].showGridLines = False
    for col, val in ((5, 2025), (6, "2026E"), (7, "2027E"), (9, "1Q26E"), (10, "2Q26E"), (11, "3Q26E"), (12, "4Q26E"),
                     (13, "1Q27E"), (14, "2Q27E"), (15, "3Q27E"), (16, "4Q27E")):
        ms.apply_cell_style(ws.cell(1, col, val), "header")

    # 1. Hidden literals: =E2*2, =E2*1.15, and IF branch fixed amount =IF(E2>0,500,0)
    ws.cell(2, 4, "Total Revenue")
    ws.cell(2, 5, 100.0)
    ws.cell(2, 6, "=E2*2")
    ws.cell(2, 9, "=E2*1.15")
    ws.cell(2, 10, "=IF(E2>0,500,0)")
    ws.cell(2, 11, "=I2")
    ws.cell(2, 12).value = None  # mandatory forecast result cell left unregistered blank!

    # 2. Ending Cash mistakenly summed (=SUM(I3:L3)) -> summed_year_violation
    ws.cell(3, 4, "Ending Cash")
    ws.cell(3, 6, "=SUM(I3:L3)")
    for qc in (9, 10, 11, 12):
        ws.cell(3, qc, "=E2")

    # 3. Single EPS row mixing two calculation methods across summed years 2026E & 2027E (Codex r3 #2)
    ws.cell(4, 4, "Diluted EPS")
    ws.cell(4, 6, "=F2/F5")
    ws.cell(4, 7, "=SUM(M4:P4)")
    for qc in range(9, 17):
        ws.cell(4, qc, "=I2/I5")
    ws.cell(5, 4, "Weighted Average Diluted Shares")
    ws.cell(5, 6, "=AVERAGE(I5:L5)"); ws.cell(5, 7, "=AVERAGE(M5:P5)")
    for qc in range(9, 17):
        ws.cell(5, qc, "=100")

    # 4. Unnoted zero assumption 0 without rightmost note (Codex r3 #1)
    ws.cell(6, 4, "Tax Rate")
    c_zero = ws.cell(6, 9, 0.0)
    ms.apply_cell_style(c_zero, "driver_assumption", is_percent=True, number_format=ms.FMT_PCT)

    # 5. Hidden #REF! inside IFERROR + plugin formulas (CIQ/BQL/BDH)
    ws.cell(7, 4, "GMV Growth")
    ws.cell(7, 9, '=IFERROR(#REF!/E2-1,"")')
    ws.cell(8, 4, "External Plugin")
    ws.cell(8, 9, '=_xll.ciqfunctions.udf.CIQ("PDD","IQ_LASTSALEPRICE")')
    ws.cell(8, 10, '=BQL("PDD US Equity","PX_LAST")')
    ws.cell(8, 11, '=BDH("PDD US Equity","PX_LAST")')

    wb.save(p)
    rep = sa.audit_workbook(p, static_only=True, strict=True)
    assert rep["status"] == "fail"
    checks_hit = {f["check"]: f for f in rep["findings"]}
    assert "formula_hidden_literal" in checks_hit
    assert any("500" in ex for ex in checks_hit["formula_hidden_literal"]["examples"])
    assert any("2" in ex for ex in checks_hit["formula_hidden_literal"]["examples"])
    assert "summed_year_violation" in checks_hit
    assert any("Ending Cash" in ex for ex in checks_hit["summed_year_violation"]["examples"])
    assert any("mixes" in ex for ex in checks_hit["summed_year_violation"]["examples"])
    assert "forecast_amount_hardcode" in checks_hit
    assert any("blank without registration" in ex for ex in checks_hit["forecast_amount_hardcode"]["examples"])
    assert "formula_literal" in checks_hit
    assert any("missing source/evidence note" in ex for ex in checks_hit["formula_literal"]["examples"])
    assert "broken_formula_ref" in checks_hit
    assert "external_or_plugin_link" in checks_hit


def test_circular_reference_direct_defined_name_and_static_offset(tmp_path: Path) -> None:
    # Case A: iterate=True + direct cash-interest cycle
    p1 = tmp_path / "circ_direct.xlsx"
    wb1 = openpyxl.Workbook()
    ws1 = wb1.active
    ws1.title = "Model"
    wb1.calculation.iterate = True
    ws1["B2"] = "=B3*0.03"  # Interest = Avg Cash * rate
    ws1["B3"] = "=100+B2"   # Cash = 100 + Interest
    wb1.save(p1)
    rep1 = sa.audit_workbook(p1, static_only=True)
    c1 = next(f for f in rep1["findings"] if f["check"] == "circular_reference")
    assert c1["count"] >= 2

    # Case B: cycle through definedName & static OFFSET (Codex r2 疑似 / §9.3 #6)
    p2 = tmp_path / "circ_offset_dn.xlsx"
    wb2 = openpyxl.Workbook()
    ws2 = wb2.active
    ws2.title = "Model"
    wb2.calculation.iterate = False
    from openpyxl.workbook.defined_name import DefinedName

    wb2.defined_names["LoopPtr"] = DefinedName("LoopPtr", attr_text="Model!$C$5")
    ws2["B5"] = "=LoopPtr+1"
    ws2["C5"] = "=OFFSET(B5,0,0)+1"
    wb2.save(p2)
    rep2 = sa.audit_workbook(p2, static_only=True)
    assert any(f["check"] == "circular_reference" for f in rep2["findings"])


def test_checks_sheet_rollup_dummy_formula_independent_recalc_and_script_warnings(tmp_path: Path) -> None:
    # 1. Unrecalculated workbook (openpyxl save without cached values) -> unrecalculated fail
    p_uncalc = tmp_path / "uncalc.xlsx"
    _build_clean_minimal_model(p_uncalc, inject_cache=False)
    rep_uncalc = sa.audit_workbook(p_uncalc, strict=True)
    assert rep_uncalc["status"] == "fail"
    assert any(f["check"] == "unrecalculated" for f in rep_uncalc["findings"])

    # 2. Dummy formula =IF(1,"OK","FAIL") & independent recomputation catches fake OK (§9.3 #4)
    p_fake = tmp_path / "fake_checks.xlsx"
    caches = _build_clean_minimal_model(p_fake, inject_cache=True)
    wb_f = openpyxl.load_workbook(p_fake, data_only=False)
    ws_chk = wb_f["Checks"]
    ws_chk["C2"] = '=IF(1,"OK","FAIL")'  # Dummy formula not referencing real cells!
    ws_chk["B9"] = "Summary!F3 = Model!F3"  # Registered pair
    wb_f.save(p_fake)
    # Tamper Summary!F3 cached value to disagree with Model!F3 while Checks!C9 claims OK
    caches["Summary"]["F3"] = (None, "9999")
    _inject_cached_values(p_fake, caches)

    rep_fake = sa.audit_workbook(p_fake, strict=True)
    chk_f = next(f for f in rep_fake["findings"] if f["check"] == "checks_sheet_rollup")
    assert any("does not reference model cells" in ex for ex in chk_f["examples"])
    assert any("independent recompute" in ex for ex in chk_f["examples"])

    # 3. Script-level warn (font_family) vs Checks!Overall (Codex r3 #5)
    p_warn = tmp_path / "script_warn.xlsx"
    caches_w = _build_clean_minimal_model(p_warn, inject_cache=True)
    wb_w = openpyxl.load_workbook(p_warn, data_only=False)
    wb_w["Model"]["D3"].font = Font(name="Calibri", size=8, bold=True)  # triggers script warn font_family
    wb_w.save(p_warn)
    _inject_cached_values(p_warn, caches_w)

    # Without explanation: Checks!Overall is still OK, script_warnings lists font_family(explained=False),
    # --strict blocks delivery with exit code 1 (unexplained_script_warning)
    rep_unexp = sa.audit_workbook(p_warn, strict=True)
    assert rep_unexp["checks_sheet_overall"] == "OK"
    assert any(sw["check"] == "font_family" and sw["explained"] is False for sw in rep_unexp["script_warnings"])
    assert rep_unexp["status"] == "fail"
    assert rep_unexp["delivery_decision"] == "block_missing_warning_explanation"

    # Once explained in README & Changelog (or Checks col D), --strict passes!
    wb_w2 = openpyxl.load_workbook(p_warn, data_only=False)
    ws_rm = wb_w2.create_sheet("README & Changelog")
    ws_rm["A1"] = "font_family: legacy Calibri kept on label cell D3 from source template"
    wb_w2.save(p_warn)
    _inject_cached_values(p_warn, caches_w)

    rep_exp = sa.audit_workbook(p_warn, strict=True)
    assert rep_exp["status"] == "pass"
    assert rep_exp["delivery_decision"] == "pass"
    assert any(sw["check"] == "font_family" and sw["explained"] is True for sw in rep_exp["script_warnings"])


def test_cross_period_rollforward_3_statement_bs_rates_and_summary_links(tmp_path: Path) -> None:
    p = tmp_path / "identities_fail.xlsx"
    caches = _build_clean_minimal_model(p, inject_cache=True)
    wb = openpyxl.load_workbook(p, data_only=False)
    ws = wb["Model"]

    # Break Q2 beginning cash continuity (I9 = 999 instead of H10), add mismatched BS Cash,
    # add full BS out of balance, add Buyback Shares not referencing CF Buyback,
    # add Segment Total OP != Group OP, and distort Interest Income (unit mismatch 45%)
    ws["I9"] = "=H10"
    ws.cell(16, 4, "Cash and Cash Equivalents")  # BS Cash != CF Ending Cash
    ws.cell(16, 6, "=F10")
    ws.cell(17, 4, "Total Assets"); ws.cell(17, 6, "=1000")
    ws.cell(18, 4, "Total Liabilities"); ws.cell(18, 6, "=400")
    ws.cell(19, 4, "Total Shareholders Equity"); ws.cell(19, 6, "=500")  # Assets 1000 != 900
    ws.cell(20, 4, "Total Segment Operating Income"); ws.cell(20, 6, "=F5")
    ws.cell(21, 4, "Share Buyback Cash"); ws.cell(21, 8, "=10")
    ws.cell(22, 4, "Share Buyback Shares"); ws.cell(22, 8, "=H3/100")  # does NOT reference row 21!

    # Break Summary link period mismatch (Summary F3 points to Model!H3 instead of Model!F3)
    wb["Summary"]["F3"] = "=Model!H3"
    # Inject foreign template fill 1F4E79
    ws["D2"].fill = PatternFill(fill_type="solid", fgColor="1F4E79")
    wb.save(p)

    # Inject caches that violate each identity
    caches["Model"]["I9"] = (None, "500")  # Q2 beg cash 500 != Q1 end cash (~1028.25)
    caches["Model"]["F16"] = (None, "888")  # BS cash 888 != CF ending cash
    caches["Model"]["F17"] = (None, "1000")
    caches["Model"]["F18"] = (None, "400")
    caches["Model"]["F19"] = (None, "500")
    caches["Model"]["F20"] = (None, "200")  # Segment OP 200 != Group OP 88
    caches["Model"]["F7"] = (None, "450")   # 450 / 1000 = 45% -> unit_or_row_mismatch_suspect!
    caches["Model"]["H21"] = (None, "10")
    caches["Model"]["H22"] = (None, "1.1")
    _inject_cached_values(p, caches)

    rep = sa.audit_workbook(p, strict=True)
    found = {f["check"]: f for f in rep["findings"]}
    assert "cash_debt_share_rollforward" in found
    assert any("beg cash" in ex for ex in found["cash_debt_share_rollforward"]["examples"])
    assert any("BS cash" in ex for ex in found["cash_debt_share_rollforward"]["examples"])
    assert any("cash flow buyback row" in ex for ex in found["cash_debt_share_rollforward"]["examples"])
    assert "balance_sheet_tieout" in found
    assert "segment_op_tieout" in found
    assert "implied_rate_diagnostics" in found
    assert found["implied_rate_diagnostics"].get("details", {}).get("unit_or_row_mismatch_suspect") is True
    assert "summary_table_links" in found
    assert found["summary_table_links"]["level"] == "fail"
    assert "foreign_template_style" in found
    assert found["foreign_template_style"]["level"] == "fail"


def test_implied_rate_historical_band_150bps_warning(tmp_path: Path) -> None:
    p = tmp_path / "rate_band.xlsx"
    caches = _build_clean_minimal_model(p, inject_cache=True)
    # Historical 2025 implied yield = 25/1000 = 2.5%; set 2026E Interest Income = 2.0 (0.2%, >150bps below 2.5%)
    caches["Model"]["F7"] = (None, "2.0")
    _inject_cached_values(p, caches)

    rep = sa.audit_workbook(p, strict=False)
    rate_f = next(f for f in rep["findings"] if f["check"] == "implied_rate_diagnostics")
    assert rate_f["level"] == "warn"
    assert "unit_or_row_mismatch_suspect" not in (rate_f.get("details") or {})


def test_recalc_py_closed_loop_with_style_audit(tmp_path: Path) -> None:
    """Closed-loop test: build workbook -> recalc.py -> verify status=='success' -> style_audit.py --strict."""
    p = tmp_path / "closed_loop.xlsx"
    _build_clean_minimal_model(p, inject_cache=False)

    proc = subprocess.run(
        [sys.executable, str(RECALC_SCRIPT), str(p)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"No spreadsheet calculation engine (IronCalc/LibreOffice) available: {proc.stdout or proc.stderr}")
    recalc_out = json.loads(proc.stdout)
    assert recalc_out["status"] == "success", f"recalc.py failed: {recalc_out}"

    audit_proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS_DIR / "style_audit.py"), str(p), "--strict"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert audit_proc.returncode == 0, f"style_audit.py --strict failed:\n{audit_proc.stdout}\n{audit_proc.stderr}"
    audit_out = json.loads(audit_proc.stdout)
    assert audit_out["status"] == "pass"
    assert audit_out["checks_sheet_overall"] == "OK"
