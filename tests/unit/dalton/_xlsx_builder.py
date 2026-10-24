"""Minimal stdlib-only ``.xlsx`` builder for unit tests."""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass
from xml.sax.saxutils import escape, quoteattr

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_DOC_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

_COORD_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)$")


@dataclass(frozen=True)
class Cell:
    """Specification for a single worksheet cell."""

    value: int | float | str | bool | None = None
    _: KW_ONLY
    t: str | None = None
    formula: str | None = None
    num_fmt: str | None = None


CellInput = Cell | int | float | str | bool


def _parse_coord(coord: str) -> tuple[str, int, int]:
    m = _COORD_RE.match(coord.strip())
    if not m:
        raise ValueError(f"Invalid cell coordinate: {coord!r}")
    col_str = m.group(1).upper()
    row_idx = int(m.group(2))
    col_idx = 0
    for ch in col_str:
        col_idx = col_idx * 26 + (ord(ch) - ord("A") + 1)
    return f"{col_str}{row_idx}", row_idx, col_idx


def _coerce_cell(raw: CellInput) -> Cell:
    if isinstance(raw, Cell):
        return raw
    if isinstance(raw, bool):
        return Cell(raw, t="b")
    if isinstance(raw, (int, float)):
        return Cell(raw)
    if isinstance(raw, str):
        if raw.startswith("="):
            return Cell(None, formula=raw[1:])
        return Cell(raw, t="s")
    raise TypeError(f"Unsupported cell input: {raw!r}")


def build_xlsx(
    sheets: Mapping[str, Mapping[str, CellInput]],
    *,
    shared_strings: Sequence[str] | None = None,
    extra_entries: Mapping[str, bytes | str] | None = None,
) -> bytes:
    """Build an in-memory ``.xlsx`` archive using only the Python standard library."""
    sst_list: list[str] = list(shared_strings or [])
    sst_index: dict[str, int] = {s: idx for idx, s in enumerate(sst_list)}

    custom_fmts: list[str] = []
    fmt_to_style_idx: dict[str, int] = {}

    normalized_sheets: list[tuple[str, dict[int, list[tuple[int, str, Cell]]]]] = []

    for sheet_name, raw_cells in sheets.items():
        rows_map: dict[int, list[tuple[int, str, Cell]]] = {}
        for raw_coord, raw_val in raw_cells.items():
            norm_coord, row_idx, col_idx = _parse_coord(raw_coord)
            cell = _coerce_cell(raw_val)
            if (
                cell.t == "s"
                and isinstance(cell.value, str)
                and cell.value not in sst_index
            ):
                sst_index[cell.value] = len(sst_list)
                sst_list.append(cell.value)
            if cell.num_fmt is not None and cell.num_fmt not in fmt_to_style_idx:
                fmt_to_style_idx[cell.num_fmt] = len(custom_fmts) + 1
                custom_fmts.append(cell.num_fmt)
            rows_map.setdefault(row_idx, []).append((col_idx, norm_coord, cell))
        for row_cells in rows_map.values():
            row_cells.sort(key=lambda item: item[0])
        normalized_sheets.append((sheet_name, rows_map))

    has_sst = bool(sst_list)
    has_styles = bool(custom_fmts)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. [Content_Types].xml
        ct_parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            f'<Types xmlns="{_CT_NS}">',
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '<Default Extension="xml" ContentType="application/xml"/>',
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        ]
        for idx in range(1, len(normalized_sheets) + 1):
            ct_parts.append(
                f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            )
        if has_styles:
            ct_parts.append(
                '<Override PartName="/xl/styles.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            )
        if has_sst:
            ct_parts.append(
                '<Override PartName="/xl/sharedStrings.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            )
        ct_parts.append("</Types>")
        zf.writestr("[Content_Types].xml", "".join(ct_parts))

        # 2. _rels/.rels
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{_PKG_REL_NS}">'
            f'<Relationship Id="rId1" Type="{_REL_DOC_NS}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        )
        zf.writestr("_rels/.rels", rels_xml)

        # 3. xl/workbook.xml & xl/_rels/workbook.xml.rels
        wb_parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            f'<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_DOC_NS}">',
            "<sheets>",
        ]
        wb_rels_parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            f'<Relationships xmlns="{_PKG_REL_NS}">',
        ]
        for idx, (sheet_name, _) in enumerate(normalized_sheets, start=1):
            wb_parts.append(
                f'<sheet name={quoteattr(sheet_name)} sheetId="{idx}" r:id="rId{idx}"/>'
            )
            wb_rels_parts.append(
                f'<Relationship Id="rId{idx}" Type="{_REL_DOC_NS}/worksheet" Target="worksheets/sheet{idx}.xml"/>'
            )
        next_rid = len(normalized_sheets) + 1
        if has_styles:
            wb_rels_parts.append(
                f'<Relationship Id="rId{next_rid}" Type="{_REL_DOC_NS}/styles" Target="styles.xml"/>'
            )
            next_rid += 1
        if has_sst:
            wb_rels_parts.append(
                f'<Relationship Id="rId{next_rid}" Type="{_REL_DOC_NS}/sharedStrings" Target="sharedStrings.xml"/>'
            )
        wb_parts.append("</sheets></workbook>")
        wb_rels_parts.append("</Relationships>")
        zf.writestr("xl/workbook.xml", "".join(wb_parts))
        zf.writestr("xl/_rels/workbook.xml.rels", "".join(wb_rels_parts))

        # 4. Optional xl/styles.xml
        if has_styles:
            style_parts = [
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                f'<styleSheet xmlns="{_MAIN_NS}">',
                f'<numFmts count="{len(custom_fmts)}">',
            ]
            for idx, fmt_code in enumerate(custom_fmts):
                style_parts.append(
                    f'<numFmt numFmtId="{164 + idx}" formatCode={quoteattr(fmt_code)}/>'
                )
            style_parts.append("</numFmts>")
            style_parts.append(f'<cellXfs count="{1 + len(custom_fmts)}">')
            style_parts.append('<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>')
            for idx in range(len(custom_fmts)):
                style_parts.append(
                    f'<xf numFmtId="{164 + idx}" fontId="0" fillId="0" borderId="0" applyNumberFormat="1"/>'
                )
            style_parts.append("</cellXfs></styleSheet>")
            zf.writestr("xl/styles.xml", "".join(style_parts))

        # 5. Optional xl/sharedStrings.xml
        if has_sst:
            sst_parts = [
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                f'<sst xmlns="{_MAIN_NS}" count="{len(sst_list)}" uniqueCount="{len(sst_list)}">',
            ]
            for s in sst_list:
                sst_parts.append(f"<si><t>{escape(s)}</t></si>")
            sst_parts.append("</sst>")
            zf.writestr("xl/sharedStrings.xml", "".join(sst_parts))

        # 6. xl/worksheets/sheetN.xml
        for idx, (_, rows_map) in enumerate(normalized_sheets, start=1):
            ws_parts = [
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                f'<worksheet xmlns="{_MAIN_NS}">',
                "<sheetData>",
            ]
            for row_idx in sorted(rows_map):
                ws_parts.append(f'<row r="{row_idx}">')
                for _, coord, cell in rows_map[row_idx]:
                    attrs = [f'r="{coord}"']
                    if cell.num_fmt is not None:
                        attrs.append(f's="{fmt_to_style_idx[cell.num_fmt]}"')
                    if cell.t is not None:
                        attrs.append(f't={quoteattr(cell.t)}')
                    children: list[str] = []
                    if cell.formula is not None:
                        f_expr = cell.formula.removeprefix("=")
                        children.append(f"<f>{escape(f_expr)}</f>")
                    if cell.t == "inlineStr":
                        if cell.value is not None:
                            children.append(
                                f"<is><t>{escape(str(cell.value))}</t></is>"
                            )
                    else:
                        v_str: str | None
                        if cell.value is None:
                            v_str = None
                        elif cell.t == "s":
                            if isinstance(cell.value, int) and not isinstance(
                                cell.value, bool
                            ):
                                v_str = str(cell.value)
                            else:
                                v_str = str(sst_index[str(cell.value)])
                        elif cell.t == "b" or isinstance(cell.value, bool):
                            v_str = "1" if bool(cell.value) else "0"
                        else:
                            v_str = str(cell.value)
                        if v_str is not None:
                            if v_str == "":
                                children.append("<v/>")
                            else:
                                children.append(f"<v>{escape(v_str)}</v>")
                    attr_str = " ".join(attrs)
                    if children:
                        ws_parts.append(f"<c {attr_str}>{''.join(children)}</c>")
                    else:
                        ws_parts.append(f"<c {attr_str}/>")
                ws_parts.append("</row>")
            ws_parts.append("</sheetData></worksheet>")
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", "".join(ws_parts))

        if extra_entries:
            for path, payload in extra_entries.items():
                zf.writestr(path, payload)

    return buf.getvalue()
