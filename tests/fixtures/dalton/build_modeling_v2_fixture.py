#!/usr/bin/env python3
"""Generate the small multi-company China Ecommerce baseline fixture workbook for P0-a tests.

Includes:
- ``README & Changelog`` (shared)
- ``Valuation - JD``, ``Key Metrics - JD``, ``Model - JD``, ``Subs - JD``, ``vs Consensus``
- ``BABA>>``, ``Model - BABA``, ``Subs - BABA`` (non-target company sheets with cross-sheet ref)
- CIQ plugin formula, external workbook link formula, and circular reference pair (all with cached <v>)
- Accurate pre-populated OOXML cached <v> values on every formula cell
- No ``fullCalcOnLoad="1"`` on ``<calcPr>``
"""

from __future__ import annotations

from pathlib import Path
import sys
import zipfile

from lxml import etree
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-model" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import model_style  # noqa: E402

_SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_XML = etree.XMLParser(resolve_entities=False, no_network=True)


def _hair() -> Border:
    s = Side(style="hair", color="000000")
    return Border(left=s, right=s, top=s, bottom=s)


def _hdr(cell, align: str = "right") -> None:
    cell.font = Font(name="Arial", size=8, bold=True, color="FFFFFF")
    cell.fill = PatternFill(fill_type="solid", start_color="3366FF", end_color="3366FF")
    cell.alignment = Alignment(horizontal=align, vertical="center")


def _sec(cell, fill: str, fg: str = "000000") -> None:
    cell.font = Font(name="Arial", size=8, bold=True, color=fg)
    cell.fill = PatternFill(fill_type="solid", start_color=fill, end_color=fill)


def _st(cell, *, color="000000", bold=False, italic=False, fill=None, hair=False, fmt=None) -> None:
    cell.font = Font(name="Arial", size=8, bold=bold, italic=italic, color=color)
    if fill:
        cell.fill = PatternFill(fill_type="solid", start_color=fill, end_color=fill)
    if hair:
        cell.border = _hair()
    if fmt:
        cell.number_format = fmt


def inject_cached_values_and_calcpr(
    xlsx_path: Path,
    cached: dict[str, dict[str, tuple[str | None, str]]],
    *,
    full_calc_on_load: bool = False,
    iterate: bool = True,
) -> None:
    """Inject OOXML ``<v>`` caches for formula cells and configure ``<calcPr>``."""
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        infos = zf.infolist()
        payload = {i.filename: zf.read(i.filename) for i in infos}

    wb_root = etree.fromstring(payload["xl/workbook.xml"], _XML)
    calc = wb_root.find(f"{{{_SML}}}calcPr")
    if calc is None:
        calc = etree.SubElement(wb_root, f"{{{_SML}}}calcPr")
    calc.set("calcId", "191029")
    if full_calc_on_load:
        calc.set("fullCalcOnLoad", "1")
    elif "fullCalcOnLoad" in calc.attrib:
        del calc.attrib["fullCalcOnLoad"]
    if iterate:
        calc.set("iterate", "1")
        calc.set("iterateCount", "100")
        calc.set("iterateDelta", "0.001")
    payload["xl/workbook.xml"] = etree.tostring(wb_root, encoding="UTF-8", xml_declaration=True)

    rels = etree.fromstring(payload["xl/_rels/workbook.xml.rels"], _XML)
    targets = {r.get("Id"): r.get("Target", "") for r in rels}
    sheet_to_part: dict[str, str] = {}
    for sh in wb_root.iter(f"{{{_SML}}}sheet"):
        tgt = targets.get(sh.get(_REL_ID), "")
        part = tgt.lstrip("/") if tgt.startswith("/") else f"xl/{tgt.lstrip('/')}"
        sheet_to_part[sh.get("name")] = part

    for sheet_name, cell_map in cached.items():
        part = sheet_to_part.get(sheet_name)
        if not part or part not in payload:
            continue
        root = etree.fromstring(payload[part], _XML)
        want = {k.upper(): v for k, v in cell_map.items()}
        for c_el in root.iter(f"{{{_SML}}}c"):
            ref = (c_el.get("r") or "").upper()
            if ref not in want:
                continue
            t_attr, v_text = want[ref]
            v_el = c_el.find(f"{{{_SML}}}v")
            if v_el is None:
                v_el = etree.Element(f"{{{_SML}}}v")
                f_el = c_el.find(f"{{{_SML}}}f")
                if f_el is not None:
                    f_el.addnext(v_el)
                else:
                    c_el.append(v_el)
            v_el.text = str(v_text)
            if t_attr in ("str", "e", "b"):
                c_el.set("t", t_attr)
            elif "t" in c_el.attrib:
                del c_el.attrib["t"]
        payload[part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    with zipfile.ZipFile(xlsx_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename], compress_type=info.compress_type)


def build_fixture(output_path: Path) -> Path:
    """Build the clean multi-company China Ecommerce baseline fixture workbook."""
    wb = openpyxl.Workbook()
    cached: dict[str, dict[str, tuple[str | None, str]]] = {}

    def _rec(sheet: str, coord: str, val: float | int | str, t: str | None = None) -> None:
        cached.setdefault(sheet, {})[coord] = (t, str(val))

    # 1. README & Changelog
    ws_rm = wb.active
    ws_rm.title = "README & Changelog"
    for i, h in enumerate(["Version", "Date", "Baseline", "Summary"], start=1):
        _hdr(ws_rm.cell(1, i, h), align="left")
    ws_rm.cell(2, 1, "v01")
    ws_rm.cell(2, 2, "2026-08-15")
    ws_rm.cell(2, 3, "Original upload")
    ws_rm.cell(2, 4, "Initial China Ecommerce baseline covering JD and BABA")
    ws_rm.freeze_panes = "A2"

    # 2. Valuation - JD
    ws_val = wb.create_sheet("Valuation - JD")
    ws_val.sheet_properties.tabColor = "4F81BD"
    ws_val["A1"] = "(RMB MM)"
    _hdr(ws_val["A1"], align="left")
    for i, h in enumerate(["Metric", "2026E", "2027E", "2028E"], start=2):
        _hdr(ws_val.cell(1, i, h))
    ws_val.cell(3, 2, "Total Revenue")
    ws_val.cell(4, 2, "Net Income")
    ws_val.cell(5, 2, "Diluted EPS")
    ws_val.freeze_panes = "C2"

    # 3. Key Metrics - JD
    ws_km = wb.create_sheet("Key Metrics - JD")
    ws_km.sheet_properties.tabColor = "4F81BD"
    ws_km["A1"] = "(RMB MM)"
    _hdr(ws_km["A1"], align="left")
    ann_headers = ["2024", "2025", "2026E", "2027E", "2028E"]
    for idx, p in enumerate(ann_headers, start=5):
        _hdr(ws_km.cell(1, idx, p))
    ws_km.cell(3, 4, "Total Revenue")
    ws_km.cell(4, 4, "Operating Income")
    ws_km.cell(5, 4, "Net Income")
    ws_km.freeze_panes = "E2"

    # 4. Model - JD
    ws_jd = wb.create_sheet("Model - JD")
    ws_jd.sheet_properties.tabColor = "4F81BD"
    ws_jd.views.sheetView[0].showGridLines = False
    ws_jd["A1"] = "(RMB MM)"
    _hdr(ws_jd["A1"], align="left")
    for ci in (2, 3, 4):
        _hdr(ws_jd.cell(1, ci, None))
    q_headers = [
        "1Q25", "2Q25", "3Q25", "4Q25",
        "1Q26", "2Q26E", "3Q26E", "4Q26E",
        "1Q27E", "2Q27E", "3Q27E", "4Q27E",
    ]
    for idx, p in enumerate(ann_headers, start=5):
        _hdr(ws_jd.cell(1, idx, p))
    ws_jd.column_dimensions["J"].width = 1.2
    for idx, p in enumerate(q_headers, start=11):
        _hdr(ws_jd.cell(1, idx, p))
    note_col = 23  # W
    _hdr(ws_jd.cell(1, note_col, "Notes"), align="left")
    ws_jd.cell(2, 1, "FY ends Dec")
    _st(ws_jd.cell(2, 1), italic=True)

    # Set widths
    for ci, w in ((1, 2.0), (2, 2.0), (3, 2.0), (4, 28.0), (note_col, 24.0)):
        ws_jd.column_dimensions[get_column_letter(ci)].width = w
    for ci in (*range(5, 10), *range(11, 23)):
        ws_jd.column_dimensions[get_column_letter(ci)].width = 9.0
    ws_jd.freeze_panes = "E2"

    # Row labels
    _sec(ws_jd.cell(3, 1, "Income Statement"), "99CCFF")
    labels_jd = {
        4: ("Retail Revenue", False, False, model_style.FMT_INT),
        5: ("Logistics Revenue", False, False, model_style.FMT_INT),
        6: ("Total Revenue", True, False, model_style.FMT_INT),
        7: ("Revenue YoY %", False, True, model_style.FMT_PCT),
        8: ("Cost of Revenue", False, False, model_style.FMT_INT),
        9: ("Gross Profit", True, False, model_style.FMT_INT),
        10: ("Gross Margin %", False, True, model_style.FMT_PCT),
        11: ("Operating Expenses", False, False, model_style.FMT_INT),
        12: ("Operating Income", True, False, model_style.FMT_INT),
        13: ("Operating Margin %", False, True, model_style.FMT_PCT),
        14: ("Net Income", True, False, model_style.FMT_INT),
        15: ("Diluted Shares (MM)", False, False, model_style.FMT_DEC1),
        16: ("Diluted EPS", True, False, model_style.FMT_DEC2),
        19: ("Retail Operating Profit", False, False, model_style.FMT_INT),
        20: ("Logistics Operating Profit", False, False, model_style.FMT_INT),
        21: ("Unallocated & Eliminations", False, False, model_style.FMT_INT),
        22: ("Total Segment Operating Profit", True, False, model_style.FMT_INT),
        25: ("Retail Growth % (Driver)", False, True, model_style.FMT_PCT),
        26: ("Logistics Growth % (Driver)", False, True, model_style.FMT_PCT),
        27: ("COGS % of Revenue (Driver)", False, True, model_style.FMT_PCT),
        28: ("OpEx % of Revenue (Driver)", False, True, model_style.FMT_PCT),
        29: ("Ending Active Merchants", False, False, model_style.FMT_INT),
        32: ("Beginning Cash", False, False, model_style.FMT_INT),
        33: ("Net Change in Cash", False, False, model_style.FMT_INT),
        34: ("Ending Cash", True, False, model_style.FMT_INT),
        37: ("Cash and Equivalents", False, False, model_style.FMT_INT),
        38: ("Other Assets", False, False, model_style.FMT_INT),
        39: ("Total Assets", True, False, model_style.FMT_INT),
        40: ("Total Liabilities", False, False, model_style.FMT_INT),
        41: ("Total Equity", False, False, model_style.FMT_INT),
        42: ("Total Liabilities & Equity", True, False, model_style.FMT_INT),
        45: ("Market Price (CIQ Plugin)", False, False, model_style.FMT_DEC2),
        46: ("FX Rate (External Link)", False, False, model_style.FMT_DEC2),
        47: ("Circular Interest A", False, False, model_style.FMT_DEC2),
        48: ("Circular Interest B", False, False, model_style.FMT_DEC2),
    }
    _sec(ws_jd.cell(18, 1, "Segment Reporting"), "993300", "FFFFFF")
    _sec(ws_jd.cell(24, 1, "Operating Drivers"), "FFCC99")
    _sec(ws_jd.cell(31, 1, "Cash Flow Statement"), "99CCFF")
    _sec(ws_jd.cell(36, 1, "Balance Sheet"), "99CCFF")
    _sec(ws_jd.cell(44, 1, "Reference & Legacy Plugin Items"), "99CCFF")

    for r, (lbl, bld, ita, _) in labels_jd.items():
        _st(ws_jd.cell(r, 4, lbl), bold=bld, italic=ita)

    # Numeric simulation to populate exact values & formulas
    # 2024 historical annual (col E=5)
    vals: dict[tuple[int, int], float] = {}
    vals[(4, 5)] = 8000.0
    vals[(5, 5)] = 2000.0
    vals[(6, 5)] = 10000.0
    vals[(8, 5)] = 8200.0
    vals[(9, 5)] = 1800.0
    vals[(11, 5)] = 1400.0
    vals[(12, 5)] = 400.0
    vals[(14, 5)] = 340.0
    vals[(15, 5)] = 1500.0
    vals[(19, 5)] = 380.0
    vals[(20, 5)] = 70.0
    vals[(21, 5)] = -50.0
    vals[(22, 5)] = 400.0
    vals[(29, 5)] = 500.0
    vals[(32, 5)] = 1800.0
    vals[(33, 5)] = 200.0
    vals[(34, 5)] = 2000.0
    vals[(37, 5)] = 2000.0
    vals[(38, 5)] = 3000.0
    vals[(39, 5)] = 5000.0
    vals[(40, 5)] = 2600.0
    vals[(41, 5)] = 2400.0
    vals[(42, 5)] = 5000.0

    # Write 2024 actuals (col 5 = E)
    for r in (4, 5, 8, 11, 14, 15, 19, 20, 21, 29, 32, 33, 38, 40, 41):
        c = ws_jd.cell(r, 5, vals[(r, 5)])
        _st(c, color="0000FF", fmt=labels_jd[r][3])
    for r, expr, val in (
        (6, "=E4+E5", 10000.0),
        (9, "=E6-E8", 1800.0),
        (10, "=E9/E6", 0.18),
        (12, "=E9-E11", 400.0),
        (13, "=E12/E6", 0.04),
        (16, "=E14/E15", 340.0 / 1500.0),
        (22, "=E19+E20+E21", 400.0),
        (34, "=E32+E33", 2000.0),
        (37, "=E34", 2000.0),
        (39, "=E37+E38", 5000.0),
        (42, "=E40+E41", 5000.0),
    ):
        c = ws_jd.cell(r, 5, expr)
        _st(c, color="0000FF", bold=labels_jd[r][1], italic=labels_jd[r][2], fmt=labels_jd[r][3])
        _rec("Model - JD", f"E{r}", val)

    # Quarterly data: 1Q25..4Q25 (cols 11..14, actuals), 1Q26 (col 15, actual), 2Q26E..4Q27E (cols 16..22, forecast)
    q_retail_base = [2100.0, 2200.0, 2300.0, 2400.0, 2310.0]  # 1Q25..1Q26 actuals
    q_log_base = [520.0, 540.0, 560.0, 580.0, 572.0]
    cash_running = 2000.0

    for idx, q_col in enumerate(range(11, 23)):
        col_l = get_column_letter(q_col)
        is_hist = q_col <= 15  # 1Q25..1Q26 actual, 2Q26E..4Q27E forecast
        if is_hist:
            ret = q_retail_base[idx]
            log = q_log_base[idx]
            cogs = round((ret + log) * 0.82, 2)
            opex = round((ret + log) * 0.135, 2)
            ni = round(((ret + log) - cogs - opex) * 0.85, 2)
            shares = 1480.0 - idx * 5.0
            merchants = 510.0 + idx * 10.0
            beg_cash = cash_running
            chg_cash = 60.0
            end_cash = beg_cash + chg_cash
            cash_running = end_cash
            other_assets = 3100.0 + idx * 20.0
            liab = 2650.0 + idx * 10.0
            eq = end_cash + other_assets - liab
            seg_ret = round(((ret + log) - cogs - opex) * 0.90, 2)
            seg_log = round(((ret + log) - cogs - opex) * 0.20, 2)
            seg_unalloc = round(((ret + log) - cogs - opex) - seg_ret - seg_log, 2)

            for r, v in (
                (4, ret), (5, log), (8, cogs), (11, opex), (14, ni), (15, shares),
                (19, seg_ret), (20, seg_log), (21, seg_unalloc), (29, merchants),
                (33, chg_cash), (38, other_assets), (40, liab), (41, eq),
            ):
                vals[(r, q_col)] = v
                c = ws_jd.cell(r, q_col, v)
                _st(c, color="0000FF", fmt=labels_jd[r][3])
            # Beginning cash: 1Q25 links to 2024 Ending Cash, others link to prev quarter
            if q_col == 11:
                ws_jd.cell(32, q_col, "=E34")
            else:
                ws_jd.cell(32, q_col, f"={get_column_letter(q_col - 1)}34")
            vals[(32, q_col)] = beg_cash
            _st(ws_jd.cell(32, q_col), fmt=model_style.FMT_INT)
            _rec("Model - JD", f"{col_l}32", beg_cash)
            # YoY driver rows in history (derived ratio)
            if q_col >= 15:
                prev_y_l = get_column_letter(q_col - 4)
                ws_jd.cell(25, q_col, f"={col_l}4/{prev_y_l}4-1")
                _st(ws_jd.cell(25, q_col), italic=True, fmt=model_style.FMT_PCT)
                _rec("Model - JD", f"{col_l}25", ret / vals[(4, q_col - 4)] - 1.0)
        else:
            # Forecast quarter: drivers in rows 25-28 (yellow FFFFC8 + hair border)
            prev_y_col = q_col - 4
            prev_y_l = get_column_letter(prev_y_col)
            prev_q_l = get_column_letter(q_col - 1)
            g_ret = 0.10
            g_log = 0.08
            cogs_pct = 0.815
            opex_pct = 0.130
            for r_d, v_d in ((25, g_ret), (26, g_log), (27, cogs_pct), (28, opex_pct)):
                vals[(r_d, q_col)] = v_d
                c = ws_jd.cell(r_d, q_col, v_d)
                _st(c, color="0000FF", italic=True, fill="FFFFC8", hair=True, fmt=model_style.FMT_PCT)

            ret = round(vals[(4, prev_y_col)] * (1.0 + g_ret), 4)
            log = round(vals[(5, prev_y_col)] * (1.0 + g_log), 4)
            tot_rev = ret + log
            cogs = round(tot_rev * cogs_pct, 4)
            gp = tot_rev - cogs
            opex = round(tot_rev * opex_pct, 4)
            op = gp - opex
            ni = round(op * 0.85, 4)
            shares = vals[(15, q_col - 1)]
            merchants = vals[(29, q_col - 1)] + 10.0
            beg_cash = cash_running
            chg_cash = round(ni * 0.8, 4)
            end_cash = beg_cash + chg_cash
            cash_running = end_cash
            other_assets = vals[(38, q_col - 1)]
            liab = vals[(40, q_col - 1)]
            eq = end_cash + other_assets - liab
            seg_ret = round(op * 0.90, 4)
            seg_log = round(op * 0.20, 4)
            seg_unalloc = round(op - seg_ret - seg_log, 4)

            formula_specs = [
                (4, f"={prev_y_l}4*(1+{col_l}25)", ret),
                (5, f"={prev_y_l}5*(1+{col_l}26)", log),
                (8, f"={col_l}6*{col_l}27", cogs),
                (11, f"={col_l}6*{col_l}28", opex),
                (14, f"={col_l}12*0.85", ni),
                (15, f"={prev_q_l}15", shares),
                (19, f"={col_l}12*0.9", seg_ret),
                (20, f"={col_l}12*0.2", seg_log),
                (21, f"={col_l}12-{col_l}19-{col_l}20", seg_unalloc),
                (29, f"={prev_q_l}29+10", merchants),
                (32, f"={prev_q_l}34", beg_cash),
                (33, f"={col_l}14*0.8", chg_cash),
                (38, f"={prev_q_l}38", other_assets),
                (40, f"={prev_q_l}40", liab),
                (41, f"={col_l}39-{col_l}40", eq),
            ]
            for r, expr, v in formula_specs:
                vals[(r, q_col)] = v
                c = ws_jd.cell(r, q_col, expr)
                _st(c, bold=labels_jd[r][1], italic=labels_jd[r][2], fmt=labels_jd[r][3])
                _rec("Model - JD", f"{col_l}{r}", v)

        # Common quarterly computed subtotals/ratios (both hist & forecast)
        tot_rev = vals[(4, q_col)] + vals[(5, q_col)]
        gp = tot_rev - vals[(8, q_col)]
        op = gp - vals[(11, q_col)]
        end_cash = vals[(32, q_col)] + vals[(33, q_col)]
        tot_assets = end_cash + vals[(38, q_col)]
        tot_le = vals[(40, q_col)] + vals[(41, q_col)]
        vals[(6, q_col)] = tot_rev
        vals[(9, q_col)] = gp
        vals[(12, q_col)] = op
        vals[(22, q_col)] = vals[(19, q_col)] + vals[(20, q_col)] + vals[(21, q_col)]
        vals[(34, q_col)] = end_cash
        vals[(37, q_col)] = end_cash
        vals[(39, q_col)] = tot_assets
        vals[(42, q_col)] = tot_le

        common_q = [
            (6, f"={col_l}4+{col_l}5", tot_rev),
            (9, f"={col_l}6-{col_l}8", gp),
            (10, f"={col_l}9/{col_l}6", gp / tot_rev),
            (12, f"={col_l}9-{col_l}11", op),
            (13, f"={col_l}12/{col_l}6", op / tot_rev),
            (16, f"={col_l}14/{col_l}15", vals[(14, q_col)] / vals[(15, q_col)]),
            (22, f"={col_l}19+{col_l}20+{col_l}21", vals[(22, q_col)]),
            (34, f"={col_l}32+{col_l}33", end_cash),
            (37, f"={col_l}34", end_cash),
            (39, f"={col_l}37+{col_l}38", tot_assets),
            (42, f"={col_l}40+{col_l}41", tot_le),
        ]
        if q_col >= 15:
            prev_y_l = get_column_letter(q_col - 4)
            common_q.append((7, f"={col_l}6/{prev_y_l}6-1", tot_rev / vals[(6, q_col - 4)] - 1.0))
        for r, expr, v in common_q:
            c = ws_jd.cell(r, q_col, expr)
            _st(c, bold=labels_jd[r][1], italic=labels_jd[r][2], fmt=labels_jd[r][3])
            _rec("Model - JD", f"{col_l}{r}", v)

    # Annual columns synthesized from 4 quarters: 2025 (F=6 -> K:N), 2026E (G=7 -> O:R), 2027E (H=8 -> S:V)
    for ann_col, q1_col, q4_col, is_legacy_blue in ((6, 11, 14, True), (7, 15, 18, False), (8, 19, 22, False)):
        col_l = get_column_letter(ann_col)
        prev_l = get_column_letter(ann_col - 1)
        q1_l = get_column_letter(q1_col)
        q4_l = get_column_letter(q4_col)
        f_color = "0000FF" if is_legacy_blue else "000000"

        for r in (4, 5, 8, 11, 14, 19, 20, 21, 33):
            v = sum(vals[(r, qc)] for qc in range(q1_col, q4_col + 1))
            vals[(r, ann_col)] = v
            c = ws_jd.cell(r, ann_col, f"=SUM({q1_l}{r}:{q4_l}{r})")
            _st(c, color=f_color, bold=labels_jd[r][1], fmt=labels_jd[r][3])
            _rec("Model - JD", f"{col_l}{r}", v)

        # Ending balances take Q4
        for r in (15, 29, 34, 37, 38, 40, 41):
            v = vals[(r, q4_col)]
            vals[(r, ann_col)] = v
            expr = f"={q4_l}{r}" if r != 37 else f"={col_l}34"
            if r == 34:
                expr = f"={col_l}32+{col_l}33"
            c = ws_jd.cell(r, ann_col, expr)
            _st(c, color=f_color, bold=labels_jd[r][1], fmt=labels_jd[r][3])
            _rec("Model - JD", f"{col_l}{r}", v)

        # Beginning cash = prior annual ending cash
        vals[(32, ann_col)] = vals[(34, ann_col - 1)]
        c = ws_jd.cell(32, ann_col, f"={prev_l}34")
        _st(c, color=f_color, fmt=model_style.FMT_INT)
        _rec("Model - JD", f"{col_l}32", vals[(32, ann_col)])

        # Totals & ratios
        vals[(6, ann_col)] = vals[(4, ann_col)] + vals[(5, ann_col)]
        vals[(9, ann_col)] = vals[(6, ann_col)] - vals[(8, ann_col)]
        vals[(12, ann_col)] = vals[(9, ann_col)] - vals[(11, ann_col)]
        vals[(22, ann_col)] = vals[(19, ann_col)] + vals[(20, ann_col)] + vals[(21, ann_col)]
        vals[(39, ann_col)] = vals[(37, ann_col)] + vals[(38, ann_col)]
        vals[(42, ann_col)] = vals[(40, ann_col)] + vals[(41, ann_col)]
        for r, expr, v in (
            (6, f"={col_l}4+{col_l}5", vals[(6, ann_col)]),
            (7, f"={col_l}6/{prev_l}6-1", vals[(6, ann_col)] / vals[(6, ann_col - 1)] - 1.0),
            (9, f"={col_l}6-{col_l}8", vals[(9, ann_col)]),
            (10, f"={col_l}9/{col_l}6", vals[(9, ann_col)] / vals[(6, ann_col)]),
            (12, f"={col_l}9-{col_l}11", vals[(12, ann_col)]),
            (13, f"={col_l}12/{col_l}6", vals[(12, ann_col)] / vals[(6, ann_col)]),
            (16, f"={col_l}14/{col_l}15", vals[(14, ann_col)] / vals[(15, ann_col)]),
            (22, f"={col_l}19+{col_l}20+{col_l}21", vals[(22, ann_col)]),
            (39, f"={col_l}37+{col_l}38", vals[(39, ann_col)]),
            (42, f"={col_l}40+{col_l}41", vals[(42, ann_col)]),
        ):
            c = ws_jd.cell(r, ann_col, expr)
            _st(c, color=f_color, bold=labels_jd[r][1], italic=labels_jd[r][2], fmt=labels_jd[r][3])
            _rec("Model - JD", f"{col_l}{r}", v)

    # Far annual 2028E (col I=9, no quarterly breakdown — uses annual driver cells)
    for r_d, v_d in ((25, 0.08), (26, 0.07), (27, 0.81), (28, 0.125)):
        vals[(r_d, 9)] = v_d
        _st(ws_jd.cell(r_d, 9, v_d), color="0000FF", italic=True, fill="FFFFC8", hair=True, fmt=model_style.FMT_PCT)
    vals[(4, 9)] = round(vals[(4, 8)] * 1.08, 4)
    vals[(5, 9)] = round(vals[(5, 8)] * 1.07, 4)
    vals[(6, 9)] = vals[(4, 9)] + vals[(5, 9)]
    vals[(8, 9)] = round(vals[(6, 9)] * 0.81, 4)
    vals[(9, 9)] = vals[(6, 9)] - vals[(8, 9)]
    vals[(11, 9)] = round(vals[(6, 9)] * 0.125, 4)
    vals[(12, 9)] = vals[(9, 9)] - vals[(11, 9)]
    vals[(14, 9)] = round(vals[(12, 9)] * 0.85, 4)
    vals[(15, 9)] = vals[(15, 8)]
    vals[(19, 9)] = round(vals[(12, 9)] * 0.90, 4)
    vals[(20, 9)] = round(vals[(12, 9)] * 0.20, 4)
    vals[(21, 9)] = round(vals[(12, 9)] - vals[(19, 9)] - vals[(20, 9)], 4)
    vals[(22, 9)] = vals[(12, 9)]
    vals[(29, 9)] = vals[(29, 8)] + 40.0
    vals[(32, 9)] = vals[(34, 8)]
    vals[(33, 9)] = round(vals[(14, 9)] * 0.8, 4)
    vals[(34, 9)] = vals[(32, 9)] + vals[(33, 9)]
    vals[(37, 9)] = vals[(34, 9)]
    vals[(38, 9)] = vals[(38, 8)]
    vals[(39, 9)] = vals[(37, 9)] + vals[(38, 9)]
    vals[(40, 9)] = vals[(40, 8)]
    vals[(41, 9)] = vals[(39, 9)] - vals[(40, 9)]
    vals[(42, 9)] = vals[(39, 9)]
    for r, expr in (
        (4, "=H4*(1+I25)"), (5, "=H5*(1+I26)"), (6, "=I4+I5"), (7, "=I6/H6-1"),
        (8, "=I6*I27"), (9, "=I6-I8"), (10, "=I9/I6"), (11, "=I6*I28"),
        (12, "=I9-I11"), (13, "=I12/I6"), (14, "=I12*0.85"), (15, "=H15"),
        (16, "=I14/I15"), (19, "=I12*0.9"), (20, "=I12*0.2"), (21, "=I12-I19-I20"),
        (22, "=I19+I20+I21"), (29, "=H29+40"), (32, "=H34"), (33, "=I14*0.8"),
        (34, "=I32+I33"), (37, "=I34"), (38, "=H38"), (39, "=I37+I38"),
        (40, "=H40"), (41, "=I39-I40"), (42, "=I40+I41"),
    ):
        c = ws_jd.cell(r, 9, expr)
        _st(c, bold=labels_jd[r][1], italic=labels_jd[r][2], fmt=labels_jd[r][3])
        val_r = vals.get((r, 9))
        if val_r is None:
            if r == 7:
                val_r = vals[(6, 9)] / vals[(6, 8)] - 1.0
            elif r == 10:
                val_r = vals[(9, 9)] / vals[(6, 9)]
            elif r == 13:
                val_r = vals[(12, 9)] / vals[(6, 9)]
            elif r == 16:
                val_r = vals[(14, 9)] / vals[(15, 9)]
        _rec("Model - JD", f"I{r}", val_r)

    # Special baseline cells: CIQ plugin, external link, circular pair
    ws_jd.cell(45, 5, '=CIQ("NASDAQ:JD","IQ_LASTSALEPRICE")')
    _rec("Model - JD", "E45", 38.5)
    ws_jd.cell(46, 5, "=[ExternalRates.xlsx]FX!B2")
    _rec("Model - JD", "E46", 7.25)
    ws_jd.cell(47, 5, "=E48*0.5+6.25")
    _rec("Model - JD", "E47", 12.5)
    ws_jd.cell(48, 5, "=E47*0.5+6.25")
    _rec("Model - JD", "E48", 12.5)
    # Downstream formula referencing CIQ plugin
    ws_jd.cell(45, 6, "=E45*1.0")
    _rec("Model - JD", "F45", 38.5)
    # Rightmost note examples
    _st(ws_jd.cell(4, note_col, "<10-Q / Company filings"), color="FF0000")
    _st(ws_jd.cell(25, note_col, "<House growth driver"), color="FF0000")

    # Populate Valuation - JD & Key Metrics - JD cross-sheet formulas
    for idx, col_l, c_idx in ((3, "G", 7), (4, "H", 8), (5, "I", 9)):
        for vr, mr in ((3, 6), (4, 14), (5, 16)):
            c = ws_val.cell(vr, idx, f"='Model - JD'!{col_l}{mr}")
            _st(c, color="008000", fmt=model_style.FMT_DEC2 if mr == 16 else model_style.FMT_INT)
            val_m = vals[(14, c_idx)] / vals[(15, c_idx)] if mr == 16 else vals[(mr, c_idx)]
            _rec("Valuation - JD", f"{get_column_letter(idx)}{vr}", val_m)

    for idx in range(5, 10):
        col_l = get_column_letter(idx)
        for kr, mr in ((3, 6), (4, 12), (5, 14)):
            c = ws_km.cell(kr, idx, f"='Model - JD'!{col_l}{mr}")
            _st(c, color="008000", fmt=model_style.FMT_INT)
            _rec("Key Metrics - JD", f"{col_l}{kr}", vals[(mr, idx)])

    # 5. Subs - JD
    ws_sjd = wb.create_sheet("Subs - JD")
    ws_sjd.sheet_properties.tabColor = "4F81BD"
    ws_sjd["A1"] = "(RMB MM)"
    _hdr(ws_sjd["A1"], align="left")
    ws_sjd.cell(3, 4, "JD Retail Check")
    ws_sjd.cell(3, 5, "='Model - JD'!G4")
    _rec("Subs - JD", "E3", vals[(4, 7)])
    ws_sjd.freeze_panes = "E2"

    # 6. vs Consensus
    ws_con = wb.create_sheet("vs Consensus")
    ws_con.sheet_properties.tabColor = "4F81BD"
    for i, h in enumerate(["Period", "Metric", "House", "Consensus Snapshot", "Diff %", "Source Type", "As-Of Date", "Rationale"], start=1):
        _hdr(ws_con.cell(1, i, h), align="left" if i in (1, 2, 6, 7, 8) else "right")
    con_rows = [
        (2, "2026E", "Total Revenue", "='Model - JD'!G6", vals[(6, 7)], round(vals[(6, 7)] * 0.99, 2), "Market Consensus", "2026-08-15", "Inline with street"),
        (3, "2026E", "Operating Income", "='Model - JD'!G12", vals[(12, 7)], round(vals[(12, 7)] * 0.98, 2), "Market Consensus", "2026-08-15", "Slightly higher margin"),
        (4, "2026E", "Net Income", "='Model - JD'!G14", vals[(14, 7)], round(vals[(14, 7)] * 0.98, 2), "Market Consensus", "2026-08-15", "Slightly higher margin"),
        (5, "2026E", "Diluted EPS", "='Model - JD'!G16", vals[(14, 7)] / vals[(15, 7)], round((vals[(14, 7)] / vals[(15, 7)]) * 0.98, 4), "Market Consensus", "2026-08-15", "Inline"),
    ]
    for r, per, met, h_expr, h_val, c_val, src_t, dt_s, rat in con_rows:
        ws_con.cell(r, 1, per)
        ws_con.cell(r, 2, met)
        _st(ws_con.cell(r, 3, h_expr), color="008000")
        _rec("vs Consensus", f"C{r}", h_val)
        _st(ws_con.cell(r, 4, c_val), color="0000FF")
        _st(ws_con.cell(r, 5, f"=(C{r}-D{r})/ABS(D{r})"), italic=True, fmt=model_style.FMT_PCT)
        _rec("vs Consensus", f"E{r}", (h_val - c_val) / abs(c_val))
        _st(ws_con.cell(r, 6, src_t), color="FF0000")
        _st(ws_con.cell(r, 7, dt_s), color="FF0000")
        ws_con.cell(r, 8, rat)
    ws_con.freeze_panes = "C2"

    # 7. BABA>> divider + Model - BABA + Subs - BABA (non-target company sheets)
    ws_div = wb.create_sheet("BABA>>")
    ws_div.sheet_properties.tabColor = "F79646"

    ws_baba = wb.create_sheet("Model - BABA")
    ws_baba.sheet_properties.tabColor = "F79646"
    ws_baba["A1"] = "(RMB MM)"
    _hdr(ws_baba["A1"], align="left")
    for idx, p in enumerate(["2025", "2026E", "2027E"], start=5):
        _hdr(ws_baba.cell(1, idx, p))
    ws_baba.cell(4, 4, "BABA Revenue")
    ws_baba.cell(4, 5, 90000.0)
    ws_baba.cell(4, 6, "=E4*1.08")
    _rec("Model - BABA", "F4", 97200.0)
    ws_baba.cell(4, 7, "=F4*1.07")
    _rec("Model - BABA", "G4", 104004.0)
    ws_baba.cell(5, 4, "Peer JD 2026E Revenue Ref")
    ws_baba.cell(5, 6, "='Model - JD'!G6")
    _rec("Model - BABA", "F5", vals[(6, 7)])
    ws_baba.cell(6, 4, "Peer JD 2Q26E Revenue Ref")
    ws_baba.cell(6, 6, "='Model - JD'!P6")
    _rec("Model - BABA", "F6", vals[(6, 16)])
    ws_baba.cell(7, 4, "BABA CIQ Price")
    ws_baba.cell(7, 5, '=CIQ("NYSE:BABA","IQ_LASTSALEPRICE")')
    _rec("Model - BABA", "E7", 105.25)
    ws_baba.freeze_panes = "E2"

    ws_sbaba = wb.create_sheet("Subs - BABA")
    ws_sbaba.sheet_properties.tabColor = "F79646"
    ws_sbaba["A1"] = "(RMB MM)"
    _hdr(ws_sbaba["A1"], align="left")
    ws_sbaba.cell(3, 4, "Cloud Revenue")
    ws_sbaba.cell(3, 5, "='Model - BABA'!F4*0.15")
    _rec("Subs - BABA", "E3", 14580.0)
    ws_sbaba.freeze_panes = "E2"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    wb.close()
    inject_cached_values_and_calcpr(output_path, cached, full_calc_on_load=False, iterate=True)
    return output_path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        REPO_ROOT / "tests" / "fixtures" / "dalton" / "china_ecommerce_baseline_fixture.xlsx"
    )
    build_fixture(out)
    print(f"Wrote fixture: {out}")
