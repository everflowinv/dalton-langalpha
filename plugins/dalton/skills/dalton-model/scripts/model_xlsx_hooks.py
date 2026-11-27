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
import posixpath
import re
import shutil
import subprocess
import tempfile
from typing import Any
import zipfile

from lxml import etree

_SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL_ID = f"{{{_OFFICE_REL_NS}}}id"
_CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_DRAWING_REL_TYPE = f"{_OFFICE_REL_NS}/drawing"
_CHART_REL_TYPE = f"{_OFFICE_REL_NS}/chart"
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


def _norm_formula_xml(text: str | None) -> str:
    if not text:
        return ""
    s = "".join(str(text).strip().split())
    return s[1:] if s.startswith("=") else s


_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_SST_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"
_SST_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
_WS_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
_WS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
_STYLES_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
_STYLES_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"
_STYLE_SECTION_ORDER = (
    "numFmts",
    "fonts",
    "fills",
    "borders",
    "cellStyleXfs",
    "cellXfs",
    "cellStyles",
    "dxfs",
    "tableStyles",
    "colors",
    "extLst",
)


def set_full_calc_on_load(xlsx_path: str | Path, enabled: bool) -> None:
    """Atomically set or clear ``fullCalcOnLoad="1"`` on ``xl/workbook.xml`` ``<calcPr>``."""
    target = Path(xlsx_path).expanduser().resolve()
    infos, payload = _read_package(target)
    wb_root = etree.fromstring(payload["xl/workbook.xml"], _XML)
    calc = wb_root.find(f"{{{_SML}}}calcPr")
    if calc is None:
        if not enabled:
            return
        calc = etree.SubElement(wb_root, f"{{{_SML}}}calcPr")
    if enabled:
        calc.set("fullCalcOnLoad", "1")
    elif "fullCalcOnLoad" in calc.attrib:
        del calc.attrib["fullCalcOnLoad"]
    payload["xl/workbook.xml"] = etree.tostring(wb_root, encoding="UTF-8", xml_declaration=True)
    tmp = target.with_name(target.name + ".fcol.tmp")
    with zipfile.ZipFile(tmp, "w") as zout:
        for info in infos:
            zout.writestr(info, payload[info.filename], compress_type=info.compress_type)
    os.replace(tmp, target)


def _normalize_sml_tree(el: Any, *, strip_container_ws: bool = False) -> Any:
    """Clone an OOXML subtree with canonical default SpreadsheetML namespace to ignore prefix aliases."""
    nsmap = {None: _SML} if str(el.tag).startswith(f"{{{_SML}}}") else el.nsmap
    cloned = etree.Element(el.tag, attrib=dict(el.attrib), nsmap=nsmap)
    elem_children = [c for c in el if isinstance(c.tag, str)]
    if strip_container_ws and elem_children and el.tag != f"{{{_SML}}}t":
        cloned.text = None if (el.text is None or not el.text.strip()) else el.text
    else:
        cloned.text = el.text
    if strip_container_ws:
        cloned.tail = None if (el.tail is None or not el.tail.strip()) else el.tail
    else:
        cloned.tail = el.tail
    for child in elem_children:
        cloned.append(_normalize_sml_tree(child, strip_container_ws=strip_container_ws))
    return cloned


def _canonical_xml(el: Any) -> bytes:
    """Deterministic canonical XML bytes for exact ``<si>`` / ``<is>`` / element comparison."""
    return etree.tostring(_normalize_sml_tree(el), method="c14n", exclusive=True)


def canonical_sheet_xml(raw_xml: bytes) -> bytes:
    """Return Exclusive-C14N canonical XML bytes for a complete worksheet XML document."""
    root = etree.fromstring(raw_xml, _XML)
    return etree.tostring(_normalize_sml_tree(root, strip_container_ws=True), method="c14n", exclusive=True)


def diff_worksheet_xml_summary(expected_xml: bytes, actual_xml: bytes) -> list[str]:
    """Identify which top-level worksheet elements or cell XML nodes differ between two worksheet XMLs."""
    exp_root = etree.fromstring(expected_xml, _XML)
    act_root = etree.fromstring(actual_xml, _XML)
    diffs: list[str] = []

    if dict(exp_root.attrib) != dict(act_root.attrib):
        diffs.append("worksheet attributes")

    exp_children = [c for c in exp_root if isinstance(c.tag, str)]
    act_children = [c for c in act_root if isinstance(c.tag, str)]
    exp_by_tag: dict[str, list[Any]] = {}
    act_by_tag: dict[str, list[Any]] = {}
    for ch in exp_children:
        local = etree.QName(ch.tag).localname
        exp_by_tag.setdefault(local, []).append(ch)
    for ch in act_children:
        local = etree.QName(ch.tag).localname
        act_by_tag.setdefault(local, []).append(ch)

    for tag_name in sorted(set(exp_by_tag) | set(act_by_tag)):
        e_list = exp_by_tag.get(tag_name, [])
        a_list = act_by_tag.get(tag_name, [])
        e_c14n = [etree.tostring(_normalize_sml_tree(x, strip_container_ws=True), method="c14n", exclusive=True) for x in e_list]
        a_c14n = [etree.tostring(_normalize_sml_tree(x, strip_container_ws=True), method="c14n", exclusive=True) for x in a_list]
        if e_c14n != a_c14n:
            diffs.append(tag_name)

    if not diffs and canonical_sheet_xml(expected_xml) != canonical_sheet_xml(actual_xml):
        diffs.append("element_order_or_root")
    return diffs


def _is_plain_si(si_el: Any) -> bool:
    """Return True iff ``<si>`` contains a single plain ``<t>`` with no rich-text runs or phonetics."""
    children = list(si_el)
    return len(children) == 1 and children[0].tag == f"{{{_SML}}}t"


def _read_shared_string_elements(payload: dict[str, bytes]) -> list[Any]:
    raw = payload.get("xl/sharedStrings.xml")
    if not raw:
        return []
    root = etree.fromstring(raw, _XML)
    return list(root.findall(f"{{{_SML}}}si"))


def _read_shared_strings(payload: dict[str, bytes]) -> list[str]:
    return [
        "".join(t.text or "" for t in si.iter(f"{{{_SML}}}t"))
        for si in _read_shared_string_elements(payload)
    ]


class _SharedStringsMutator:
    """Maintain or create ``xl/sharedStrings.xml`` while preserving donor SST indices & rich-text ``<si>``."""

    def __init__(
        self,
        infos: list[zipfile.ZipInfo],
        payload: dict[str, bytes],
        *,
        seed_raw: bytes | None = None,
    ) -> None:
        self._infos = infos
        self._payload = payload
        self._seed_raw = seed_raw
        self._appended = False
        self.dirty = False
        self.donor_count = 0

        raw = seed_raw if seed_raw is not None else payload.get("xl/sharedStrings.xml")
        if raw:
            self._root: Any = etree.fromstring(raw, _XML)
            si_els = list(self._root.findall(f"{{{_SML}}}si"))
            self.strings: list[str] = [
                "".join(t.text or "" for t in si.iter(f"{{{_SML}}}t")) for si in si_els
            ]
            if seed_raw is not None:
                self.donor_count = len(si_els)
                if payload.get("xl/sharedStrings.xml") != seed_raw:
                    self.dirty = True
        else:
            self._root = None
            si_els = []
            self.strings = []

        self._c14n_index: dict[bytes, int] = {}
        self._plain_index: dict[str, int] = {}
        for idx, (si, text) in enumerate(zip(si_els, self.strings)):
            self._c14n_index.setdefault(_canonical_xml(si), idx)
            if _is_plain_si(si):
                self._plain_index.setdefault(text, idx)

    def _ensure_registration(self) -> None:
        if "xl/sharedStrings.xml" not in self._payload and not any(
            i.filename == "xl/sharedStrings.xml" for i in self._infos
        ):
            zi = zipfile.ZipInfo("xl/sharedStrings.xml")
            zi.compress_type = zipfile.ZIP_DEFLATED
            self._infos.append(zi)
        # Register Override in [Content_Types].xml
        ct_raw = self._payload.get("[Content_Types].xml")
        if ct_raw:
            ct_root = etree.fromstring(ct_raw, _XML)
            has_ov = any(
                ov.get("PartName") == "/xl/sharedStrings.xml"
                for ov in ct_root.findall(f"{{{_CT_NS}}}Override")
            )
            if not has_ov:
                etree.SubElement(
                    ct_root,
                    f"{{{_CT_NS}}}Override",
                    PartName="/xl/sharedStrings.xml",
                    ContentType=_SST_CONTENT_TYPE,
                )
                self._payload["[Content_Types].xml"] = etree.tostring(
                    ct_root, xml_declaration=True, encoding="UTF-8", standalone=True
                )
        # Register Relationship in xl/_rels/workbook.xml.rels
        rels_raw = self._payload.get("xl/_rels/workbook.xml.rels")
        if rels_raw:
            rels_root = etree.fromstring(rels_raw, _XML)
            has_rel = any(
                r.get("Type") == _SST_REL_TYPE
                for r in rels_root.findall(f"{{{_PKG_REL_NS}}}Relationship")
            )
            if not has_rel:
                used_ids = {r.get("Id") or "" for r in rels_root.findall(f"{{{_PKG_REL_NS}}}Relationship")}
                n = 1
                while f"rId{n}" in used_ids:
                    n += 1
                etree.SubElement(
                    rels_root,
                    f"{{{_PKG_REL_NS}}}Relationship",
                    Id=f"rId{n}",
                    Type=_SST_REL_TYPE,
                    Target="sharedStrings.xml",
                )
                self._payload["xl/_rels/workbook.xml.rels"] = etree.tostring(
                    rels_root, xml_declaration=True, encoding="UTF-8", standalone=True
                )

    def _ensure_part(self) -> None:
        if self._root is not None:
            self._ensure_registration()
            return
        self._root = etree.Element(f"{{{_SML}}}sst", count="0", uniqueCount="0")
        self._ensure_registration()
        self.dirty = True

    def intern_si(self, si_el: Any) -> int:
        """Intern a complete ``<si>`` element (preserving rich-text runs ``<r>``) without mutating donor indices."""
        c14n = _canonical_xml(si_el)
        existing = self._c14n_index.get(c14n)
        if existing is not None:
            return existing
        self._ensure_part()
        idx = len(self.strings)
        cloned = etree.fromstring(etree.tostring(si_el, encoding="UTF-8"), _XML)
        self._root.append(cloned)
        text = "".join(t.text or "" for t in cloned.iter(f"{{{_SML}}}t"))
        self.strings.append(text)
        self._c14n_index[c14n] = idx
        if _is_plain_si(cloned):
            self._plain_index.setdefault(text, idx)
        self._root.set("uniqueCount", str(len(self.strings)))
        cur_cnt = int(self._root.get("count") or len(self.strings))
        self._root.set("count", str(max(cur_cnt, len(self.strings))))
        self._appended = True
        self.dirty = True
        return idx

    def intern(self, text: str) -> int:
        existing = self._plain_index.get(text)
        if existing is not None:
            return existing
        self._ensure_part()
        idx = len(self.strings)
        si = etree.SubElement(self._root, f"{{{_SML}}}si")
        t_el = etree.SubElement(si, f"{{{_SML}}}t")
        if text != text.strip() or "\n" in text:
            t_el.set(_XML_SPACE, "preserve")
        t_el.text = text
        self.strings.append(text)
        self._c14n_index.setdefault(_canonical_xml(si), idx)
        self._plain_index[text] = idx
        self._root.set("uniqueCount", str(len(self.strings)))
        cur_cnt = int(self._root.get("count") or len(self.strings))
        self._root.set("count", str(max(cur_cnt, len(self.strings))))
        self._appended = True
        self.dirty = True
        return idx

    def flush(self) -> None:
        if self.dirty and self._root is not None:
            self._ensure_registration()
            if self._seed_raw is not None and not self._appended:
                self._payload["xl/sharedStrings.xml"] = self._seed_raw
            else:
                self._payload["xl/sharedStrings.xml"] = etree.tostring(
                    self._root, xml_declaration=True, encoding="UTF-8", standalone=True
                )


class _StylesMutator:
    """Append-only merger for ``xl/styles.xml`` that keeps donor indices ``0..donor_count-1`` immutable."""

    def __init__(self, infos: list[zipfile.ZipInfo], payload: dict[str, bytes]) -> None:
        self._infos = infos
        self._payload = payload
        self._seed_raw: bytes | None = payload.get("xl/styles.xml")
        self._appended = False
        self._root: Any = (
            etree.fromstring(self._seed_raw, _XML)
            if self._seed_raw
            else etree.Element(f"{{{_SML}}}styleSheet", nsmap={None: _SML})
        )

        # Index existing custom numFmts by formatCode and track max numFmtId >= 163
        self._numfmt_by_code: dict[str, int] = {}
        self._max_numfmt_id = 163
        nf_container = self._root.find(f"{{{_SML}}}numFmts")
        if nf_container is not None:
            for nf in nf_container.findall(f"{{{_SML}}}numFmt"):
                nid_s = nf.get("numFmtId") or ""
                code = nf.get("formatCode")
                if nid_s.isdigit():
                    nid = int(nid_s)
                    self._max_numfmt_id = max(self._max_numfmt_id, nid)
                    if code is not None:
                        self._numfmt_by_code.setdefault(code, nid)

        # Index indexed table sections by canonical XML
        self._section_index: dict[str, dict[bytes, int]] = {}
        self._section_count: dict[str, int] = {}
        for sec, item_tag in (
            ("fonts", "font"),
            ("fills", "fill"),
            ("borders", "border"),
            ("cellXfs", "xf"),
            ("dxfs", "dxf"),
        ):
            idx_map: dict[bytes, int] = {}
            cont = self._root.find(f"{{{_SML}}}{sec}")
            items = list(cont.findall(f"{{{_SML}}}{item_tag}")) if cont is not None else []
            for i, el in enumerate(items):
                idx_map.setdefault(_canonical_xml(el), i)
            self._section_index[sec] = idx_map
            self._section_count[sec] = len(items)

        csx_cont = self._root.find(f"{{{_SML}}}cellStyleXfs")
        self._cell_style_xfs_count = (
            len(csx_cont.findall(f"{{{_SML}}}xf")) if csx_cont is not None else 0
        )

    def _ensure_section(self, sec_name: str) -> Any:
        cont = self._root.find(f"{{{_SML}}}{sec_name}")
        if cont is not None:
            return cont
        cont = etree.Element(f"{{{_SML}}}{sec_name}", count="0")
        want_pos = _STYLE_SECTION_ORDER.index(sec_name) if sec_name in _STYLE_SECTION_ORDER else len(_STYLE_SECTION_ORDER)
        insert_at = len(self._root)
        for idx, ch in enumerate(self._root):
            if not isinstance(ch.tag, str):
                continue
            loc = etree.QName(ch.tag).localname
            pos = _STYLE_SECTION_ORDER.index(loc) if loc in _STYLE_SECTION_ORDER else len(_STYLE_SECTION_ORDER)
            if pos > want_pos:
                insert_at = idx
                break
        self._root.insert(insert_at, cont)
        self._appended = True
        return cont

    def intern_numfmt(self, format_code: str) -> int:
        existing = self._numfmt_by_code.get(format_code)
        if existing is not None:
            return existing
        cont = self._ensure_section("numFmts")
        self._max_numfmt_id += 1
        new_id = self._max_numfmt_id
        etree.SubElement(
            cont,
            f"{{{_SML}}}numFmt",
            numFmtId=str(new_id),
            formatCode=format_code,
        )
        cont.set("count", str(len(cont.findall(f"{{{_SML}}}numFmt"))))
        self._numfmt_by_code[format_code] = new_id
        self._appended = True
        return new_id

    def intern_section_item(self, sec_name: str, item_tag: str, el: Any) -> int:
        c14n = _canonical_xml(el)
        idx_map = self._section_index[sec_name]
        existing = idx_map.get(c14n)
        if existing is not None:
            return existing
        cont = self._ensure_section(sec_name)
        cloned = etree.fromstring(etree.tostring(el, encoding="UTF-8"), _XML)
        idx = self._section_count[sec_name]
        cont.append(cloned)
        self._section_count[sec_name] = idx + 1
        cont.set("count", str(idx + 1))
        idx_map[c14n] = idx
        self._appended = True
        return idx

    def build_remappers(
        self,
        edited_styles_raw: bytes | None,
    ) -> tuple[dict[int, int], dict[int, int]]:
        """Return ``(xf_map, dxf_map)`` mapping ``edited`` indices into this donor-seeded stylesheet."""
        if not edited_styles_raw:
            return {}, {}
        ed_root = etree.fromstring(edited_styles_raw, _XML)

        ed_numfmts: dict[int, str] = {}
        ed_nf_cont = ed_root.find(f"{{{_SML}}}numFmts")
        if ed_nf_cont is not None:
            for nf in ed_nf_cont.findall(f"{{{_SML}}}numFmt"):
                nid_s = nf.get("numFmtId") or ""
                code = nf.get("formatCode")
                if nid_s.isdigit() and code is not None:
                    ed_numfmts[int(nid_s)] = code

        def _extract_list(sec: str, tag_l: str) -> list[Any]:
            c = ed_root.find(f"{{{_SML}}}{sec}")
            return list(c.findall(f"{{{_SML}}}{tag_l}")) if c is not None else []

        ed_fonts = _extract_list("fonts", "font")
        ed_fills = _extract_list("fills", "fill")
        ed_borders = _extract_list("borders", "border")
        ed_xfs = _extract_list("cellXfs", "xf")
        ed_dxfs = _extract_list("dxfs", "dxf")

        font_cache: dict[int, int] = {}
        fill_cache: dict[int, int] = {}
        border_cache: dict[int, int] = {}
        xf_map: dict[int, int] = {}
        dxf_map: dict[int, int] = {}

        for old_i, xf_el in enumerate(ed_xfs):
            cloned = etree.fromstring(etree.tostring(xf_el, encoding="UTF-8"), _XML)
            nf_s = cloned.get("numFmtId")
            if nf_s and nf_s.isdigit():
                nf_id = int(nf_s)
                if nf_id >= 164 and nf_id in ed_numfmts:
                    cloned.set("numFmtId", str(self.intern_numfmt(ed_numfmts[nf_id])))
            f_s = cloned.get("fontId")
            if f_s and f_s.isdigit():
                fi = int(f_s)
                if 0 <= fi < len(ed_fonts):
                    if fi not in font_cache:
                        font_cache[fi] = self.intern_section_item("fonts", "font", ed_fonts[fi])
                    cloned.set("fontId", str(font_cache[fi]))
            fl_s = cloned.get("fillId")
            if fl_s and fl_s.isdigit():
                fli = int(fl_s)
                if 0 <= fli < len(ed_fills):
                    if fli not in fill_cache:
                        fill_cache[fli] = self.intern_section_item("fills", "fill", ed_fills[fli])
                    cloned.set("fillId", str(fill_cache[fli]))
            b_s = cloned.get("borderId")
            if b_s and b_s.isdigit():
                bi = int(b_s)
                if 0 <= bi < len(ed_borders):
                    if bi not in border_cache:
                        border_cache[bi] = self.intern_section_item("borders", "border", ed_borders[bi])
                    cloned.set("borderId", str(border_cache[bi]))
            xf_id_s = cloned.get("xfId")
            if xf_id_s and xf_id_s.isdigit():
                if self._cell_style_xfs_count <= 0:
                    del cloned.attrib["xfId"]
                elif int(xf_id_s) >= self._cell_style_xfs_count:
                    cloned.set("xfId", "0")
            xf_map[old_i] = self.intern_section_item("cellXfs", "xf", cloned)

        for old_di, dxf_el in enumerate(ed_dxfs):
            dxf_map[old_di] = self.intern_section_item("dxfs", "dxf", dxf_el)

        return xf_map, dxf_map

    def flush(self) -> None:
        if not self._appended and self._seed_raw is not None:
            return
        if "xl/styles.xml" not in self._payload and not any(
            i.filename == "xl/styles.xml" for i in self._infos
        ):
            zi = zipfile.ZipInfo("xl/styles.xml")
            zi.compress_type = zipfile.ZIP_DEFLATED
            self._infos.append(zi)
        self._payload["xl/styles.xml"] = etree.tostring(
            self._root, xml_declaration=True, encoding="UTF-8", standalone=True
        )


def _part_rels_path(part_path: str) -> str:
    d = posixpath.dirname(part_path)
    b = posixpath.basename(part_path)
    return posixpath.join(d, "_rels", f"{b}.rels") if d else f"_rels/{b}.rels"


def _worksheet_rels_path(sheet_part: str) -> str:
    return _part_rels_path(sheet_part)


def _resolve_rel_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


_PART_NUM_RE = re.compile(r"^(.*?)(\d+)$")


def _allocate_fresh_part_path(existing_parts: set[str], ed_part_path: str) -> str:
    """Allocate a non-colliding OOXML part path in ``existing_parts`` for a newly imported part."""
    norm = ed_part_path.lstrip("/")
    if norm not in existing_parts:
        return norm
    d = posixpath.dirname(norm)
    b = posixpath.basename(norm)
    stem, ext = posixpath.splitext(b)
    m = _PART_NUM_RE.match(stem)
    if m:
        prefix, num_s = m.group(1), m.group(2)
        n = int(num_s) + 1
    else:
        prefix = stem
        n = 1
    while True:
        cand = posixpath.join(d, f"{prefix}{n}{ext}") if d else f"{prefix}{n}{ext}"
        if cand not in existing_parts:
            return cand
        n += 1


def _copy_content_type_for_imported_part(
    donor_payload: dict[str, bytes],
    edited_payload: dict[str, bytes],
    ed_part_path: str,
    new_donor_path: str,
) -> None:
    """Ensure ``[Content_Types].xml`` in donor covers ``new_donor_path`` matching ``ed_part_path``."""
    d_ct_raw = donor_payload.get("[Content_Types].xml")
    e_ct_raw = edited_payload.get("[Content_Types].xml")
    if not d_ct_raw or not e_ct_raw:
        return
    d_ct = etree.fromstring(d_ct_raw, _XML)
    e_ct = etree.fromstring(e_ct_raw, _XML)
    changed = False

    ed_part_name = f"/{ed_part_path.lstrip('/')}"
    new_part_name = f"/{new_donor_path.lstrip('/')}"
    ed_ov_ct: str | None = None
    for ov in e_ct.findall(f"{{{_CT_NS}}}Override"):
        if ov.get("PartName") == ed_part_name:
            ed_ov_ct = ov.get("ContentType")
            break
    if ed_ov_ct:
        has_ov = any(ov.get("PartName") == new_part_name for ov in d_ct.findall(f"{{{_CT_NS}}}Override"))
        if not has_ov:
            etree.SubElement(
                d_ct,
                f"{{{_CT_NS}}}Override",
                PartName=new_part_name,
                ContentType=ed_ov_ct,
            )
            changed = True

    ext = posixpath.splitext(new_donor_path)[1].lstrip(".").lower()
    if ext:
        d_exts = {(df.get("Extension") or "").lower() for df in d_ct.findall(f"{{{_CT_NS}}}Default")}
        if ext not in d_exts:
            for df in e_ct.findall(f"{{{_CT_NS}}}Default"):
                if (df.get("Extension") or "").lower() == ext and df.get("ContentType"):
                    etree.SubElement(
                        d_ct,
                        f"{{{_CT_NS}}}Default",
                        Extension=ext,
                        ContentType=str(df.get("ContentType")),
                    )
                    changed = True
                    break

    if changed:
        donor_payload["[Content_Types].xml"] = etree.tostring(
            d_ct, xml_declaration=True, encoding="UTF-8", standalone=True
        )


def _import_new_part_into_donor(
    donor_infos: list[zipfile.ZipInfo],
    donor_payload: dict[str, bytes],
    edited_payload: dict[str, bytes],
    ed_part_path: str,
    imported_part_map: dict[str, str],
) -> str:
    """Import a genuinely new part (and recursively its sub-rels & target parts) into donor under fresh paths."""
    norm_ed = ed_part_path.lstrip("/")
    if norm_ed in imported_part_map:
        return imported_part_map[norm_ed]

    existing_parts = set(donor_payload) | {i.filename for i in donor_infos}
    new_path = _allocate_fresh_part_path(existing_parts, norm_ed)
    imported_part_map[norm_ed] = new_path

    zi = zipfile.ZipInfo(new_path)
    zi.compress_type = zipfile.ZIP_DEFLATED
    donor_infos.append(zi)
    donor_payload[new_path] = edited_payload[norm_ed]
    _copy_content_type_for_imported_part(donor_payload, edited_payload, norm_ed, new_path)

    ed_sub_rels = _part_rels_path(norm_ed)
    if ed_sub_rels in edited_payload:
        sub_root = etree.fromstring(edited_payload[ed_sub_rels], _XML)
        for rel in sub_root.findall(f"{{{_PKG_REL_NS}}}Relationship"):
            if (rel.get("TargetMode") or "").lower() == "external":
                continue
            tgt = rel.get("Target") or ""
            if not tgt:
                continue
            resolved_sub = _resolve_rel_target(norm_ed, tgt)
            if resolved_sub in edited_payload:
                new_sub_path = _import_new_part_into_donor(
                    donor_infos, donor_payload, edited_payload, resolved_sub, imported_part_map
                )
                rel.set("Target", posixpath.relpath(new_sub_path, posixpath.dirname(new_path)))
        new_sub_rels = _part_rels_path(new_path)
        if new_sub_rels not in donor_payload and not any(i.filename == new_sub_rels for i in donor_infos):
            zi_sr = zipfile.ZipInfo(new_sub_rels)
            zi_sr.compress_type = zipfile.ZIP_DEFLATED
            donor_infos.append(zi_sr)
        donor_payload[new_sub_rels] = etree.tostring(
            sub_root, xml_declaration=True, encoding="UTF-8", standalone=True
        )
    return new_path


_WS_TRAILING_ORDER = (
    "drawing",
    "legacyDrawing",
    "legacyDrawingHF",
    "drawingHF",
    "picture",
    "oleObjects",
    "controls",
    "webPublishItems",
    "tableParts",
    "extLst",
)


def _insert_ws_element_in_schema_order(sh_root: Any, elem: Any) -> None:
    """Insert a worksheet child element (e.g. ``<drawing>``) in ECMA-376 CT_Worksheet order."""
    local = etree.QName(elem.tag).localname
    if local in _WS_TRAILING_ORDER:
        after_tags = set(_WS_TRAILING_ORDER[_WS_TRAILING_ORDER.index(local) + 1 :])
        for idx, child in enumerate(list(sh_root)):
            if isinstance(child.tag, str) and etree.QName(child.tag).localname in after_tags:
                sh_root.insert(idx, elem)
                return
    sh_root.append(elem)


def extract_sheet_chart_bindings(payload: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    """Trace every worksheet's drawing/chart relationships and chart ``<c:f>`` data-range references."""
    out: dict[str, dict[str, Any]] = {}
    for sh_name, sh_part in _sheet_parts(payload).items():
        if sh_part not in payload:
            continue
        rels_part = _worksheet_rels_path(sh_part)
        rels_bytes = payload.get(rels_part)
        rel_by_id: dict[str, tuple[str, str, str, str]] = {}
        if rels_bytes:
            r_root = etree.fromstring(rels_bytes, _XML)
            for rel in r_root.findall(f"{{{_PKG_REL_NS}}}Relationship"):
                rid = rel.get("Id") or ""
                rtype = rel.get("Type") or ""
                rtgt = rel.get("Target") or ""
                rmode = (rel.get("TargetMode") or "").lower()
                resolved = rtgt if rmode == "external" else (_resolve_rel_target(sh_part, rtgt) if rtgt else "")
                if rid:
                    rel_by_id[rid] = (rtype, rtgt, rmode, resolved)

        sh_root = etree.fromstring(payload[sh_part], _XML)
        drawings: list[str] = []
        charts: list[dict[str, Any]] = []
        broken_refs: list[str] = []
        seen_drawing_rids: set[str] = set()

        for dr_el in sh_root.findall(f"{{{_SML}}}drawing"):
            rid = dr_el.get(_REL_ID) or ""
            seen_drawing_rids.add(rid)
            if not rid or rid not in rel_by_id:
                broken_refs.append(f"worksheet <drawing r:id={rid!r}> missing in {rels_part}")
                continue
            rtype, _rtgt, rmode, d_part = rel_by_id[rid]
            if rmode == "external" or not d_part or d_part not in payload:
                broken_refs.append(f"worksheet drawing rel {rid!r} target {d_part!r} missing from package")
                continue
            if rtype and rtype != _DRAWING_REL_TYPE:
                broken_refs.append(f"worksheet <drawing r:id={rid!r}> points to non-drawing rel type {rtype!r}")
                continue
            drawings.append(d_part)

            d_rels_part = _part_rels_path(d_part)
            d_rel_by_id: dict[str, tuple[str, str, str]] = {}
            if d_rels_part in payload:
                dr_rels_root = etree.fromstring(payload[d_rels_part], _XML)
                for drel in dr_rels_root.findall(f"{{{_PKG_REL_NS}}}Relationship"):
                    drid = drel.get("Id") or ""
                    drtype = drel.get("Type") or ""
                    drtgt = drel.get("Target") or ""
                    drmode = (drel.get("TargetMode") or "").lower()
                    dresolved = drtgt if drmode == "external" else (_resolve_rel_target(d_part, drtgt) if drtgt else "")
                    if drid:
                        d_rel_by_id[drid] = (drtype, drmode, dresolved)

            d_root = etree.fromstring(payload[d_part], _XML)
            chart_els = list(d_root.iter(f"{{{_CHART_NS}}}chart"))
            for ch_el in chart_els:
                crid = ch_el.get(_REL_ID) or ""
                if not crid or crid not in d_rel_by_id:
                    broken_refs.append(f"drawing {d_part!r} <c:chart r:id={crid!r}> missing in {d_rels_part}")
                    continue
                crtype, crmode, c_part = d_rel_by_id[crid]
                if crmode == "external" or not c_part or c_part not in payload:
                    broken_refs.append(f"drawing {d_part!r} chart rel {crid!r} target {c_part!r} missing from package")
                    continue
                if crtype and crtype != _CHART_REL_TYPE:
                    broken_refs.append(f"drawing {d_part!r} <c:chart r:id={crid!r}> points to non-chart rel type {crtype!r}")
                    continue
                c_root = etree.fromstring(payload[c_part], _XML)
                formulas = [
                    (f_el.text or "").strip()
                    for f_el in c_root.iter(f"{{{_CHART_NS}}}f")
                    if (f_el.text or "").strip()
                ]
                charts.append(
                    {
                        "drawing_part": d_part,
                        "chart_part": c_part,
                        "formulas": formulas,
                    }
                )

        # Also check any dangling drawing/chart rels declared in worksheet .rels
        for rid, (rtype, _rtgt, rmode, resolved) in rel_by_id.items():
            if rmode == "external":
                continue
            if rtype in (_DRAWING_REL_TYPE, _CHART_REL_TYPE) and (not resolved or resolved not in payload):
                broken_refs.append(f"dangling worksheet rel {rid!r} -> {resolved!r}")

        out[sh_name] = {
            "sheet_part": sh_part,
            "rels_part": rels_part,
            "rels_bytes": rels_bytes,
            "drawings": drawings,
            "charts": charts,
            "broken_refs": broken_refs,
        }
    return out


def _register_new_sheet_in_donor(
    donor_infos: list[zipfile.ZipInfo],
    donor_payload: dict[str, bytes],
    sheet_name: str,
) -> str:
    """Register a newly created worksheet part in donor's workbook.xml, workbook.xml.rels, and [Content_Types].xml."""
    existing_parts = set(donor_payload)
    n = 1
    while f"xl/worksheets/sheet{n}.xml" in existing_parts:
        n += 1
    new_part = f"xl/worksheets/sheet{n}.xml"
    zi = zipfile.ZipInfo(new_part)
    zi.compress_type = zipfile.ZIP_DEFLATED
    donor_infos.append(zi)

    # Register in xl/_rels/workbook.xml.rels
    rels_root = etree.fromstring(donor_payload["xl/_rels/workbook.xml.rels"], _XML)
    used_rids = {r.get("Id") or "" for r in rels_root.findall(f"{{{_PKG_REL_NS}}}Relationship")}
    rid_n = 1
    while f"rId{rid_n}" in used_rids:
        rid_n += 1
    new_rid = f"rId{rid_n}"
    etree.SubElement(
        rels_root,
        f"{{{_PKG_REL_NS}}}Relationship",
        Id=new_rid,
        Type=_WS_REL_TYPE,
        Target=f"worksheets/sheet{n}.xml",
    )
    donor_payload["xl/_rels/workbook.xml.rels"] = etree.tostring(
        rels_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    # Register in xl/workbook.xml
    wb_root = etree.fromstring(donor_payload["xl/workbook.xml"], _XML)
    sheets_el = wb_root.find(f"{{{_SML}}}sheets")
    if sheets_el is None:
        sheets_el = etree.SubElement(wb_root, f"{{{_SML}}}sheets")
    used_sids = {
        int(s.get("sheetId"))
        for s in sheets_el.findall(f"{{{_SML}}}sheet")
        if (s.get("sheetId") or "").isdigit()
    }
    new_sid = max(used_sids, default=0) + 1
    sh_el = etree.SubElement(sheets_el, f"{{{_SML}}}sheet", name=sheet_name, sheetId=str(new_sid))
    sh_el.set(_REL_ID, new_rid)
    donor_payload["xl/workbook.xml"] = etree.tostring(
        wb_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    # Register Override in [Content_Types].xml
    ct_raw = donor_payload.get("[Content_Types].xml")
    if ct_raw:
        ct_root = etree.fromstring(ct_raw, _XML)
        part_name = f"/{new_part}"
        if not any(ov.get("PartName") == part_name for ov in ct_root.findall(f"{{{_CT_NS}}}Override")):
            etree.SubElement(
                ct_root,
                f"{{{_CT_NS}}}Override",
                PartName=part_name,
                ContentType=_WS_CONTENT_TYPE,
            )
            donor_payload["[Content_Types].xml"] = etree.tostring(
                ct_root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

    return new_part


def merge_edited_sheets_into_donor(
    donor_path: str | Path,
    edited_path: str | Path,
    output_path: str | Path,
    modified_sheets: set[str] | list[str],
) -> dict[str, Any]:
    """Merge ONLY ``modified_sheets`` from openpyxl's ``edited_path`` back into ``donor_path``'s package.

    All sheets not listed in ``modified_sheets`` (including every non-target company sheet) remain
    BYTE-FOR-BYTE identical to ``donor_path``, including ``<ignoredErrors>``, cell ``ph``/``cm``/``vm``
    attributes, raw ``<v>`` formatting, ``s`` style indices, SST indices, drawings, and rels.
    """
    import posixpath

    donor_p = Path(donor_path).expanduser().resolve()
    edited_p = Path(edited_path).expanduser().resolve()
    out_p = Path(output_path).expanduser().resolve()

    donor_infos, donor_payload = _read_package(donor_p)
    _, edited_payload = _read_package(edited_p)

    donor_parts = _sheet_parts(donor_payload)
    edited_parts = _sheet_parts(edited_payload)

    styles_mut = _StylesMutator(donor_infos, donor_payload)
    xf_map, dxf_map = styles_mut.build_remappers(edited_payload.get("xl/styles.xml"))

    edited_sis = _read_shared_string_elements(edited_payload)
    sst_mut = _SharedStringsMutator(
        donor_infos,
        donor_payload,
        seed_raw=donor_payload.get("xl/sharedStrings.xml"),
    )

    merged_sheets: list[str] = []
    imported_part_map: dict[str, str] = {}
    for sh_name in sorted(set(modified_sheets)):
        ed_part = edited_parts.get(sh_name)
        if not ed_part or ed_part not in edited_payload:
            continue
        is_new_sheet = sh_name not in donor_parts
        d_part = donor_parts.get(sh_name)
        if d_part is None:
            d_part = _register_new_sheet_in_donor(donor_infos, donor_payload, sh_name)
            donor_parts[sh_name] = d_part
        orig_donor_sh_bytes = None if is_new_sheet else donor_payload.get(d_part)

        sh_root = etree.fromstring(edited_payload[ed_part], _XML)

        # Remap style indices on modified sheet from openpyxl's stylesheet into donor-seeded stylesheet
        if xf_map:
            for el in sh_root.iter(f"{{{_SML}}}c"):
                s_val = el.get("s")
                if s_val and s_val.isdigit():
                    mapped_s = xf_map.get(int(s_val))
                    if mapped_s is not None:
                        el.set("s", str(mapped_s))
            for el in sh_root.iter(f"{{{_SML}}}row"):
                s_val = el.get("s")
                if s_val and s_val.isdigit():
                    mapped_s = xf_map.get(int(s_val))
                    if mapped_s is not None:
                        el.set("s", str(mapped_s))
            for el in sh_root.iter(f"{{{_SML}}}col"):
                st_val = el.get("style")
                if st_val and st_val.isdigit():
                    mapped_s = xf_map.get(int(st_val))
                    if mapped_s is not None:
                        el.set("style", str(mapped_s))
        if dxf_map:
            for el in sh_root.iter(f"{{{_SML}}}cfRule"):
                d_val = el.get("dxfId")
                if d_val and d_val.isdigit():
                    mapped_d = dxf_map.get(int(d_val))
                    if mapped_d is not None:
                        el.set("dxfId", str(mapped_d))

        # Remap shared-string indices on modified sheet into donor-seeded SST
        if edited_sis:
            for c_el in sh_root.iter(f"{{{_SML}}}c"):
                if c_el.get("t") != "s":
                    continue
                v_el = c_el.find(f"{{{_SML}}}v")
                if v_el is not None and (v_el.text or "").isdigit():
                    old_idx = int(v_el.text)
                    if 0 <= old_idx < len(edited_sis):
                        new_idx = sst_mut.intern_si(edited_sis[old_idx])
                        v_el.text = str(new_idx)

        # Preserve donor's existing worksheet relationships, drawings, charts, and related parts verbatim!
        # NEVER overwrite donor's _rels with openpyxl's renumbered _rels (which collides with other sheets' drawings/charts).
        # Only genuinely NEW parts (e.g. new sheet or newly added drawing/hyperlink/comment) are imported under freshly
        # allocated non-colliding paths with remapped relationships.
        d_rels_part = _worksheet_rels_path(d_part)
        ed_rels_part = _worksheet_rels_path(ed_part)
        d_rels_raw = None if is_new_sheet else donor_payload.get(d_rels_part)
        ed_rels_raw = edited_payload.get(ed_rels_part)

        d_rels_root = etree.fromstring(d_rels_raw, _XML) if d_rels_raw else None
        donor_used_rids: set[str] = set()
        donor_ext_by_key: dict[tuple[str, str], str] = {}
        donor_int_by_type: dict[str, list[str]] = {}
        if d_rels_root is not None:
            for drel in d_rels_root.findall(f"{{{_PKG_REL_NS}}}Relationship"):
                did = drel.get("Id") or ""
                dtype = drel.get("Type") or ""
                dtgt = drel.get("Target") or ""
                dmode = (drel.get("TargetMode") or "").lower()
                if did:
                    donor_used_rids.add(did)
                if dmode == "external":
                    if did:
                        donor_ext_by_key.setdefault((dtype, dtgt), did)
                elif did:
                    donor_int_by_type.setdefault(dtype, []).append(did)

        def _alloc_rid() -> str:
            n = 1
            while f"rId{n}" in donor_used_rids:
                n += 1
            rid = f"rId{n}"
            donor_used_rids.add(rid)
            return rid

        rid_map: dict[str, str] = {}
        matched_int_cursor: dict[str, int] = {}
        donor_rels_changed = False

        if ed_rels_raw:
            ed_rels_root = etree.fromstring(ed_rels_raw, _XML)
            for erel in ed_rels_root.findall(f"{{{_PKG_REL_NS}}}Relationship"):
                eid = erel.get("Id") or ""
                etype = erel.get("Type") or ""
                etgt = erel.get("Target") or ""
                emode = (erel.get("TargetMode") or "").lower()
                if emode == "external":
                    existing_did = donor_ext_by_key.get((etype, etgt))
                    if existing_did:
                        if eid:
                            rid_map[eid] = existing_did
                    else:
                        if d_rels_root is None:
                            d_rels_root = etree.Element(f"{{{_PKG_REL_NS}}}Relationships", nsmap={None: _PKG_REL_NS})
                        new_rid = _alloc_rid()
                        etree.SubElement(
                            d_rels_root,
                            f"{{{_PKG_REL_NS}}}Relationship",
                            Id=new_rid,
                            Type=etype,
                            Target=etgt,
                            TargetMode="External",
                        )
                        donor_ext_by_key[(etype, etgt)] = new_rid
                        if eid:
                            rid_map[eid] = new_rid
                        donor_rels_changed = True
                    continue

                # Internal relationship: reuse donor's untouched relationship & part for existing sheets
                if not is_new_sheet:
                    d_list = donor_int_by_type.get(etype) or []
                    cur_i = matched_int_cursor.get(etype, 0)
                    if cur_i < len(d_list):
                        matched_int_cursor[etype] = cur_i + 1
                        if eid:
                            rid_map[eid] = d_list[cur_i]
                        continue

                # Genuinely new internal part -> import under a fresh non-colliding path & remap sub-rels
                if not etgt:
                    continue
                resolved_ed_tgt = _resolve_rel_target(ed_part, etgt)
                if resolved_ed_tgt not in edited_payload:
                    continue
                new_donor_tgt = _import_new_part_into_donor(
                    donor_infos, donor_payload, edited_payload, resolved_ed_tgt, imported_part_map
                )
                rel_target = posixpath.relpath(new_donor_tgt, posixpath.dirname(d_part))
                if d_rels_root is None:
                    d_rels_root = etree.Element(f"{{{_PKG_REL_NS}}}Relationships", nsmap={None: _PKG_REL_NS})
                new_rid = _alloc_rid()
                etree.SubElement(
                    d_rels_root,
                    f"{{{_PKG_REL_NS}}}Relationship",
                    Id=new_rid,
                    Type=etype,
                    Target=rel_target,
                )
                if eid:
                    rid_map[eid] = new_rid
                donor_rels_changed = True

        # Remap every r:* relationship-ID attribute inside the edited worksheet XML to donor's rId
        if rid_map:
            rel_ns_prefix = f"{{{_OFFICE_REL_NS}}}"
            for el in sh_root.iter():
                for attr_k, attr_v in list(el.attrib.items()):
                    if attr_k.startswith(rel_ns_prefix) and attr_v in rid_map:
                        el.set(attr_k, rid_map[attr_v])

        # If openpyxl dropped donor's <drawing> or <legacyDrawing> element on an existing sheet, restore it verbatim
        if orig_donor_sh_bytes:
            orig_sh_root = etree.fromstring(orig_donor_sh_bytes, _XML)
            for tag_local in ("drawing", "legacyDrawing"):
                fq = f"{{{_SML}}}{tag_local}"
                if sh_root.find(fq) is None:
                    for orig_el in orig_sh_root.findall(fq):
                        cloned_el = etree.fromstring(etree.tostring(orig_el, encoding="UTF-8"), _XML)
                        _insert_ws_element_in_schema_order(sh_root, cloned_el)

        donor_payload[d_part] = etree.tostring(
            sh_root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

        # Flush d_rels_part ONLY if genuinely new relationships were added; otherwise keep donor bytes untouched!
        if donor_rels_changed and d_rels_root is not None:
            if d_rels_part not in donor_payload and not any(i.filename == d_rels_part for i in donor_infos):
                zi_r = zipfile.ZipInfo(d_rels_part)
                zi_r.compress_type = zipfile.ZIP_DEFLATED
                donor_infos.append(zi_r)
            donor_payload[d_rels_part] = etree.tostring(
                d_rels_root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

        merged_sheets.append(sh_name)

    styles_mut.flush()
    sst_mut.flush()

    tmp = out_p.with_name(out_p.name + ".merge.tmp")
    with zipfile.ZipFile(tmp, "w") as zout:
        for info in donor_infos:
            zout.writestr(info, donor_payload[info.filename], compress_type=info.compress_type)
    os.replace(tmp, out_p)

    return {
        "status": "ok",
        "merged_sheets": merged_sheets,
        "preserved_sheets": sorted(s for s in donor_parts if s not in set(merged_sheets)),
    }


def extract_raw_xml_cell_states(
    xlsx_path: str | Path,
    sheet_names: set[str] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Read raw OOXML cell state (``t``, ``<v>``, SST index & canonical ``<si>``, ``<is>``, formula) per sheet."""
    _, payload = _read_package(Path(xlsx_path).expanduser().resolve())
    sst_els = _read_shared_string_elements(payload)
    sst_texts = ["".join(t.text or "" for t in si.iter(f"{{{_SML}}}t")) for si in sst_els]
    sst_c14n = [_canonical_xml(si) for si in sst_els]
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for name, part in _sheet_parts(payload).items():
        if sheet_names is not None and name not in sheet_names:
            continue
        if part not in payload:
            continue
        root = etree.fromstring(payload[part], _XML)
        cells: dict[str, dict[str, Any]] = {}
        for c in root.iter(f"{{{_SML}}}c"):
            ref = c.get("r")
            if not ref:
                continue
            t = c.get("t")
            f_el = c.find(f"{{{_SML}}}f")
            has_formula = f_el is not None
            f_text = f_el.text if f_el is not None else None
            v = c.find(f"{{{_SML}}}v")
            v_text: str | None = None
            v_is_empty = False
            resolved_str: str | None = None
            sst_index: int | None = None
            si_xml: bytes | None = None
            si_c14n: bytes | None = None
            is_xml: bytes | None = None
            is_c14n: bytes | None = None
            if t == "inlineStr":
                is_el = c.find(f"{{{_SML}}}is")
                if is_el is not None:
                    resolved_str = "".join(x.text or "" for x in is_el.iter(f"{{{_SML}}}t"))
                    is_xml = etree.tostring(is_el, encoding="UTF-8")
                    is_c14n = _canonical_xml(is_el)
                else:
                    resolved_str = ""
                v_text = resolved_str
            elif v is not None:
                if v.text is None or v.text == "":
                    v_is_empty = True
                    v_text = "" if t == "str" else None
                else:
                    v_text = v.text
                if t == "s" and v_text is not None and v_text.isdigit():
                    idx = int(v_text)
                    sst_index = idx
                    if 0 <= idx < len(sst_els):
                        resolved_str = sst_texts[idx]
                        si_xml = etree.tostring(sst_els[idx], encoding="UTF-8")
                        si_c14n = sst_c14n[idx]
                    else:
                        resolved_str = ""
            cells[ref.upper()] = {
                "t": t,
                "v": v_text,
                "has_v": v is not None,
                "v_is_empty": v_is_empty,
                "has_formula": has_formula,
                "formula": f_text,
                "resolved_str": resolved_str,
                "sst_index": sst_index,
                "si_xml": si_xml,
                "si_c14n": si_c14n,
                "is_xml": is_xml,
                "is_c14n": is_c14n,
                "c_c14n": _canonical_xml(c),
                "attrib": dict(c.attrib),
                "xml_bytes": etree.tostring(c, encoding="UTF-8"),
            }
        out[name] = cells
    return out


def _extract_donor_cells(
    donor_path: Path,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Read full cell cache & formula metadata ``{sheet: {COORD: {...}}}`` from donor OOXML."""
    return extract_raw_xml_cell_states(donor_path)


def _extract_donor_cache(
    donor_path: Path,
) -> dict[str, dict[str, tuple[str | None, str | None]]]:
    """Read ``{sheet: {COORD: (t_attr, v_text)}}`` for all cells with ``<v>`` in donor."""
    full = _extract_donor_cells(donor_path)
    out: dict[str, dict[str, tuple[str | None, str | None]]] = {}
    for sh, cmap in full.items():
        out[sh] = {
            ref: (meta["t"], meta["v"])
            for ref, meta in cmap.items()
            if meta["has_v"] and meta["v"] is not None
        }
    return out


def _apply_cached_value(cell_el: Any, t: str | None, text: str) -> None:
    """Write ``<v>`` and exact ``t`` attribute (including explicit ``t="n"``) onto an OOXML ``<c>`` element."""
    is_el = cell_el.find(f"{{{_SML}}}is")
    if is_el is not None and t != "inlineStr":
        cell_el.remove(is_el)
    if t == "inlineStr":
        v_old = cell_el.find(f"{{{_SML}}}v")
        if v_old is not None:
            cell_el.remove(v_old)
        if is_el is None:
            is_el = etree.SubElement(cell_el, f"{{{_SML}}}is")
        else:
            is_el.clear()
        t_el = etree.SubElement(is_el, f"{{{_SML}}}t")
        t_el.text = text
        cell_el.set("t", "inlineStr")
        return
    v = cell_el.find(f"{{{_SML}}}v")
    if v is None:
        v = etree.Element(f"{{{_SML}}}v")
        f_el = cell_el.find(f"{{{_SML}}}f")
        if f_el is not None:
            f_el.addnext(v)
        else:
            cell_el.append(v)
    v.text = text
    if t is not None:
        cell_el.set("t", t)
    elif "t" in cell_el.attrib:
        del cell_el.attrib["t"]


def _apply_exact_donor_cache(
    cell_el: Any,
    d_meta: dict[str, Any],
    sst_mut: _SharedStringsMutator,
) -> bool:
    """Restore exact donor ``<v>`` presence/emptiness/value, ``t`` attr, SST index & full ``<si>``/``<is>`` XML."""
    d_t = d_meta.get("t")
    d_v = d_meta.get("v")
    d_has_v = bool(d_meta.get("has_v"))
    d_v_empty = bool(d_meta.get("v_is_empty"))

    # Preserve shared-string cells with exact donor SST index & complete <si> (incl. rich-text <r> runs!)
    if not d_meta.get("has_formula") and d_t == "s":
        d_sst_idx = d_meta.get("sst_index")
        if d_sst_idx is not None and 0 <= int(d_sst_idx) < sst_mut.donor_count:
            want_idx_str = str(int(d_sst_idx))
            cur_v_el = cell_el.find(f"{{{_SML}}}v")
            if (
                cell_el.get("t") == "s"
                and cur_v_el is not None
                and cur_v_el.text == want_idx_str
                and cell_el.find(f"{{{_SML}}}is") is None
            ):
                return False
            _apply_cached_value(cell_el, "s", want_idx_str)
            return True
        if d_meta.get("si_xml"):
            idx = sst_mut.intern_si(etree.fromstring(d_meta["si_xml"], _XML))
            _apply_cached_value(cell_el, "s", str(idx))
            return True
        if d_meta.get("resolved_str") is not None:
            idx = sst_mut.intern(str(d_meta["resolved_str"]))
            _apply_cached_value(cell_el, "s", str(idx))
            return True

    # Preserve inlineStr cells with complete <is> XML (incl. rich-text <r> runs!)
    if not d_meta.get("has_formula") and d_t == "inlineStr":
        if d_meta.get("is_xml"):
            want_is = etree.fromstring(d_meta["is_xml"], _XML)
            cur_is = cell_el.find(f"{{{_SML}}}is")
            cur_v = cell_el.find(f"{{{_SML}}}v")
            if (
                cell_el.get("t") == "inlineStr"
                and cur_v is None
                and cur_is is not None
                and _canonical_xml(cur_is) == _canonical_xml(want_is)
            ):
                return False
            if cur_v is not None:
                cell_el.remove(cur_v)
            if cur_is is not None:
                cell_el.remove(cur_is)
            cell_el.append(want_is)
            cell_el.set("t", "inlineStr")
            return True
        _apply_cached_value(cell_el, "inlineStr", str(d_meta.get("resolved_str") or ""))
        return True

    # Distinguish missing <v> vs empty <v/> vs non-empty <v>
    if not d_has_v:
        changed = False
        v_old = cell_el.find(f"{{{_SML}}}v")
        if v_old is not None:
            cell_el.remove(v_old)
            changed = True
        is_old = cell_el.find(f"{{{_SML}}}is")
        if is_old is not None:
            cell_el.remove(is_old)
            changed = True
        if d_t is not None:
            if cell_el.get("t") != d_t:
                cell_el.set("t", d_t)
                changed = True
        elif "t" in cell_el.attrib:
            del cell_el.attrib["t"]
            changed = True
        return changed

    if d_v_empty:
        is_old = cell_el.find(f"{{{_SML}}}is")
        if is_old is not None:
            cell_el.remove(is_old)
        v_el = cell_el.find(f"{{{_SML}}}v")
        if v_el is None:
            v_el = etree.Element(f"{{{_SML}}}v")
            f_el = cell_el.find(f"{{{_SML}}}f")
            if f_el is not None:
                f_el.addnext(v_el)
            else:
                cell_el.append(v_el)
        v_el.text = None
        if d_t is not None:
            cell_el.set("t", d_t)
        elif "t" in cell_el.attrib:
            del cell_el.attrib["t"]
        return True

    if d_v is not None:
        _apply_cached_value(cell_el, d_t, str(d_v))
        return True
    return False


def _cell_has_nonempty_cache(cell_el: Any) -> bool:
    v = cell_el.find(f"{{{_SML}}}v")
    if v is None:
        return False
    t = cell_el.get("t")
    if t == "str" and v.text is None:
        return True
    return v.text is not None and v.text != ""


def _parse_coord_row_col(ref: str) -> tuple[int, int]:
    i = 0
    while i < len(ref) and ref[i].isalpha():
        i += 1
    col_s = ref[:i].upper()
    row_i = int(ref[i:]) if i < len(ref) and ref[i:].isdigit() else 0
    col_i = 0
    for ch in col_s:
        col_i = col_i * 26 + (ord(ch) - ord("A") + 1)
    return row_i, col_i


def _reinsert_missing_donor_cell(
    root: Any,
    ref: str,
    donor_meta: dict[str, Any],
    sst_mut: _SharedStringsMutator,
) -> bool:
    """Re-insert a cell present in donor but dropped by openpyxl (preserving ``t="s"`` via SST)."""
    sheet_data = root.find(f"{{{_SML}}}sheetData")
    if sheet_data is None:
        return False
    row_i, col_i = _parse_coord_row_col(ref)
    if row_i <= 0:
        return False

    # Find or create <row r="row_i"> in sorted row order
    target_row_el = None
    insert_before = None
    for r_el in sheet_data.findall(f"{{{_SML}}}row"):
        r_attr = r_el.get("r")
        if not r_attr or not r_attr.isdigit():
            continue
        ri = int(r_attr)
        if ri == row_i:
            target_row_el = r_el
            break
        if ri > row_i and insert_before is None:
            insert_before = r_el
            break
    if target_row_el is None:
        target_row_el = etree.Element(f"{{{_SML}}}row", r=str(row_i))
        if insert_before is not None:
            sheet_data.insert(sheet_data.index(insert_before), target_row_el)
        else:
            sheet_data.append(target_row_el)

    new_c = etree.fromstring(donor_meta["xml_bytes"], _XML)
    # Strip style index from donor since openpyxl may reindex cellXfs
    if "s" in new_c.attrib:
        del new_c.attrib["s"]
    # Preserve exact donor SST index when SST is seeded from donor; otherwise intern full <si>
    if not donor_meta["has_formula"] and donor_meta["t"] == "s":
        d_sst_idx = donor_meta.get("sst_index")
        if d_sst_idx is not None and 0 <= int(d_sst_idx) < sst_mut.donor_count:
            _apply_cached_value(new_c, "s", str(int(d_sst_idx)))
        elif donor_meta.get("si_xml"):
            idx = sst_mut.intern_si(etree.fromstring(donor_meta["si_xml"], _XML))
            _apply_cached_value(new_c, "s", str(idx))
        elif donor_meta.get("resolved_str") is not None:
            idx = sst_mut.intern(str(donor_meta["resolved_str"]))
            _apply_cached_value(new_c, "s", str(idx))

    # Insert <c> in column order inside <row>
    c_insert_before = None
    for existing_c in target_row_el.findall(f"{{{_SML}}}c"):
        _, ex_ci = _parse_coord_row_col((existing_c.get("r") or "").upper())
        if ex_ci > col_i:
            c_insert_before = existing_c
            break
    if c_insert_before is not None:
        target_row_el.insert(target_row_el.index(c_insert_before), new_c)
    else:
        target_row_el.append(new_c)
    return True


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
    """Atomically restore donor ``<v>`` and exact ``t`` (including ``t="n"`` & ``t="s"``) in pure OOXML.

    Runs with zero LibreOffice dependency. Restores cached values from
    ``spec["cache_donor_path"]`` for:
    - all cells in ``skip_sheets`` (preserving exact ``<v>``/absence-of-``<v>``, ``t``, and SST links,
      and restoring any cells dropped by openpyxl);
    - all cells listed in ``skip_cells[sheet]`` and ``exempt_downstream_cells[sheet]``, ONLY after
      verifying the cell still carries its exempt donor formula (also reinserting if dropped by openpyxl);
    - all untouched formula cells NOT listed in ``touched_formula_cells[sheet]`` / ``touched_cells[sheet]``.
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
    exempt_downstream = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("exempt_downstream_cells") or {}).items()
    }
    touched_formulas = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("touched_formula_cells") or {}).items()
    }
    touched_all = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("touched_cells") or {}).items()
    }

    _, donor_payload = _read_package(donor)
    donor_parts = _sheet_parts(donor_payload)
    donor_sst_raw = donor_payload.get("xl/sharedStrings.xml")
    donor_cells = _extract_donor_cells(donor)
    infos, payload = _read_package(target)
    old_target_sis = _read_shared_string_elements(payload)
    sst_mut = _SharedStringsMutator(infos, payload, seed_raw=donor_sst_raw)
    kept_cache_count = 0

    def _remap_target_sst_cell(c_el: Any) -> bool:
        """Remap a target-workbook ``t="s"`` cell from openpyxl's SST into the donor-seeded SST."""
        if donor_sst_raw is None or c_el.get("t") != "s":
            return False
        v_el = c_el.find(f"{{{_SML}}}v")
        if v_el is None or not (v_el.text or "").isdigit():
            return False
        old_idx = int(v_el.text)
        if 0 <= old_idx < len(old_target_sis):
            new_idx = sst_mut.intern_si(old_target_sis[old_idx])
            if str(new_idx) != v_el.text:
                v_el.text = str(new_idx)
                return True
        return False

    for name, part in _sheet_parts(payload).items():
        if part not in payload:
            continue
        is_skip_sheet = name in skip_sheets
        if is_skip_sheet:
            donor_part = donor_parts.get(name)
            if donor_part and donor_part in donor_payload and payload.get(part) == donor_payload[donor_part]:
                # Already byte-for-byte identical to post-insert donor: do NOT re-serialize!
                continue
        donor_sheet = donor_cells.get(name) or {}
        root = etree.fromstring(payload[part], _XML)
        sheet_skip_cells = skip_cells.get(name, set())
        sheet_exempt_down = exempt_downstream.get(name, set())
        sheet_touched_f = touched_formulas.get(name, set())
        sheet_touched_all = touched_all.get(name, set())
        changed = False
        seen_refs: set[str] = set()

        for c in root.iter(f"{{{_SML}}}c"):
            ref = (c.get("r") or "").upper()
            if not ref:
                continue
            seen_refs.add(ref)
            d_meta = donor_sheet.get(ref)
            if d_meta is None:
                if _remap_target_sst_cell(c):
                    changed = True
                continue

            f_el = c.find(f"{{{_SML}}}f")
            has_formula = f_el is not None
            cur_f_norm = _norm_formula_xml(f_el.text if f_el is not None else None)
            donor_f_norm = _norm_formula_xml(d_meta.get("formula"))
            d_t = d_meta.get("t")

            if is_skip_sheet:
                if _apply_exact_donor_cache(c, d_meta, sst_mut):
                    kept_cache_count += 1
                    changed = True
                continue

            # Target sheet rules:
            if not has_formula:
                # Never overwrite a cell edited to a constant (including former exempt coords!)
                if ref not in sheet_touched_all and not d_meta["has_formula"]:
                    if d_t in ("s", "inlineStr"):
                        if _apply_exact_donor_cache(c, d_meta, sst_mut):
                            kept_cache_count += 1
                            changed = True
                        continue
                    # Preserve explicit t="n" on untouched numeric constants if donor had it
                    if d_meta["has_v"] and d_meta.get("v") is not None:
                        if d_t is not None and c.get("t") != d_t and _cell_has_nonempty_cache(c):
                            _apply_cached_value(c, d_t, str(d_meta["v"]))
                            kept_cache_count += 1
                            changed = True
                    if _remap_target_sst_cell(c):
                        changed = True
                else:
                    if _remap_target_sst_cell(c):
                        changed = True
                continue

            # Formula cell on target sheet:
            is_exempt_coord = (ref in sheet_skip_cells) or (ref in sheet_exempt_down)
            if is_exempt_coord:
                # Verify this cell is STILL the donor's exempt formula (not overwritten to a new formula!)
                if d_meta["has_formula"] and (not donor_f_norm or cur_f_norm == donor_f_norm):
                    if _apply_exact_donor_cache(c, d_meta, sst_mut):
                        kept_cache_count += 1
                        changed = True
                continue

            # Untouched formula cell: restore exact donor <v> (or absence of <v>) and exact t
            if ref not in sheet_touched_f and ref not in sheet_touched_all:
                if d_meta["has_formula"] and (not donor_f_norm or cur_f_norm == donor_f_norm):
                    if _apply_exact_donor_cache(c, d_meta, sst_mut):
                        kept_cache_count += 1
                        changed = True

        # Restore any cell that openpyxl dropped completely:
        # - all cells on non-target skip_sheets
        # - exempt/plugin/external/circular cells on target sheets (unless explicitly touched)
        if is_skip_sheet:
            for d_ref, d_meta in donor_sheet.items():
                if d_ref not in seen_refs and (d_meta["has_v"] or d_meta["has_formula"] or d_meta["resolved_str"] is not None):
                    if _reinsert_missing_donor_cell(root, d_ref, d_meta, sst_mut):
                        kept_cache_count += 1
                        changed = True
        else:
            exempt_refs = (sheet_skip_cells | sheet_exempt_down) - sheet_touched_all
            for d_ref in sorted(exempt_refs):
                if d_ref not in seen_refs and d_ref in donor_sheet:
                    d_meta = donor_sheet[d_ref]
                    if d_meta["has_v"] or d_meta["has_formula"]:
                        if _reinsert_missing_donor_cell(root, d_ref, d_meta, sst_mut):
                            kept_cache_count += 1
                            changed = True

        if changed:
            payload[part] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

    sst_mut.flush()

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


def _values_differ(cur_t: str | None, cur_v: str | None, new_t: str | None, new_v: str | None) -> bool:
    if cur_v is None and new_v is None:
        return False
    if cur_t == "e" or new_t == "e":
        return str(cur_v) != str(new_v)
    try:
        a, b = float(cur_v or ""), float(new_v or "")
        return abs(a - b) > max(1e-9, 1e-9 * max(abs(a), abs(b)))
    except ValueError:
        return str(cur_v) != str(new_v)


def _transfer_values_selective(
    recalc_mod: Any,
    src: Path,
    dst: Path,
    values: dict[str, dict[str, tuple[str | None, str | None]]],
    formulas: dict[tuple[str, str], str],
    spec: dict[str, Any],
) -> tuple[int, int, int, int, list[str], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """Selectively merge LibreOffice values for round-affected formulas (and template builds); keep donor caches intact."""
    skip_sheets = set(spec.get("skip_sheets") or ())
    skip_cells = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("skip_cells") or {}).items()
    }
    exempt_downstream = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("exempt_downstream_cells") or {}).items()
    }
    touched_formulas = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("touched_formula_cells") or {}).items()
    }
    has_recalc_targets = "recalc_target_cells" in spec and spec["recalc_target_cells"] is not None
    recalc_targets = {
        str(sh): {str(coord).upper() for coord in (coords or ())}
        for sh, coords in (spec.get("recalc_target_cells") or {}).items()
    }
    backfill_empty_formulas = bool(spec.get("backfill_empty_formulas", False))
    skip_error_writeback = bool(spec.get("skip_error_writeback", True))

    infos, payload = recalc_mod.read_package(src)
    written = 0
    unmatched = 0
    kept_on_error = 0
    recalc_errors: list[dict[str, Any]] = []
    downstream_exempt_diffs: list[dict[str, Any]] = []
    unmatched_cells: list[dict[str, str]] = []

    for name, part in recalc_mod.sheet_parts(payload).items():
        if part not in payload:
            continue
        is_skip_sheet = name in skip_sheets
        root = etree.fromstring(payload[part], _XML)
        sheet_values = values.get(name, {})
        sheet_skip_cells = skip_cells.get(name, set())
        sheet_exempt_down = exempt_downstream.get(name, set())
        sheet_touched_f = touched_formulas.get(name, set())
        sheet_targets = recalc_targets.get(name, set())
        changed = False

        for c in root.iter(f"{{{_SML}}}c"):
            f = c.find(f"{{{_SML}}}f")
            if f is None:
                continue
            ref = c.get("r") or ""
            ref_u = ref.upper()
            is_direct_exempt = is_skip_sheet or (ref_u in sheet_skip_cells)
            is_down_exempt = (not is_direct_exempt) and (ref_u in sheet_exempt_down)
            exempt = is_direct_exempt or is_down_exempt

            is_touched_f = ref_u in sheet_touched_f
            is_downstream_target = has_recalc_targets and (ref_u in sheet_targets)
            if has_recalc_targets:
                is_target = is_touched_f or is_downstream_target or backfill_empty_formulas
            elif "touched_formula_cells" in spec:
                is_target = is_touched_f or backfill_empty_formulas
            else:
                is_target = True

            got = sheet_values.get(ref) or sheet_values.get(ref_u)
            if got is None or got[1] is None:
                if not exempt and is_target:
                    unmatched += 1
                    unmatched_cells.append({"sheet": name, "cell": ref})
                continue
            t, text = got
            is_err = _is_error_value(recalc_mod, t, text)
            cur_v_el = c.find(f"{{{_SML}}}v")
            cur_t = c.get("t")
            cur_v = cur_v_el.text if cur_v_el is not None else None
            has_donor = _cell_has_nonempty_cache(c)

            if is_direct_exempt:
                if is_err:
                    recalc_errors.append(
                        {
                            "sheet": name,
                            "cell": ref,
                            "error": text or "#ERROR!",
                            "formula": formulas.get((name, ref), ""),
                            "exempt": True,
                            "kept_donor_cache": True,
                        }
                    )
                continue

            if is_down_exempt:
                # Downstream of exempt cell (e.g. =F45 or =IFERROR(F45, 0)): NEVER overwrite donor <v>!
                if is_err:
                    recalc_errors.append(
                        {
                            "sheet": name,
                            "cell": ref,
                            "error": text or "#ERROR!",
                            "formula": formulas.get((name, ref), ""),
                            "exempt": True,
                            "downstream_exempt": True,
                            "kept_donor_cache": True,
                        }
                    )
                elif has_donor and _values_differ(cur_t, cur_v, t, text):
                    downstream_exempt_diffs.append(
                        {
                            "sheet": name,
                            "cell": ref,
                            "donor_value": cur_v,
                            "recalc_value": text,
                            "formula": formulas.get((name, ref), ""),
                        }
                    )
                kept_on_error += 1
                continue

            # Non-exempt formula on target sheet:
            if not is_target:
                continue

            if is_err:
                # ONLY newly added/modified formulas (or template-build empty formulas) leave their
                # computed error value in <c> so C13 flags them actionable. Unmodified downstream
                # formulas with non-empty donor <v> keep their donor <v>.
                if is_touched_f or (not skip_error_writeback) or (backfill_empty_formulas and not has_donor):
                    recalc_errors.append(
                        {
                            "sheet": name,
                            "cell": ref,
                            "error": text or "#ERROR!",
                            "formula": formulas.get((name, ref), ""),
                            "exempt": False,
                            "kept_donor_cache": False,
                            "touched_formula": is_touched_f,
                        }
                    )
                    _apply_cached_value(c, t, text)
                    written += 1
                    changed = True
                else:
                    recalc_errors.append(
                        {
                            "sheet": name,
                            "cell": ref,
                            "error": text or "#ERROR!",
                            "formula": formulas.get((name, ref), ""),
                            "exempt": False,
                            "kept_donor_cache": has_donor,
                            "touched_formula": False,
                        }
                    )
                    kept_on_error += 1
                continue

            # For legacy callers without recalc_target_cells/backfill_empty_formulas, do not backfill
            # untouched missing-cache formulas; DO write valid results for touched formulas,
            # round-affected downstream targets (recalc_target_cells), and template builds!
            if not is_touched_f and not is_downstream_target and not backfill_empty_formulas and not has_donor:
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
    return (
        written,
        unmatched,
        kept_on_error,
        charts_updated,
        chart_refs_skipped,
        recalc_errors,
        downstream_exempt_diffs,
        unmatched_cells,
    )


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
            downstream_exempt_diffs,
            unmatched_cells,
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

    final_status = (
        "incomplete"
        if unmatched > 0
        else ("errors_found" if non_exempt_errors else "success")
    )

    return {
        "status": final_status,
        "file": str(src),
        "total_formulas": len(formulas),
        "total_errors": len(recalc_errors),
        "non_exempt_errors": len(non_exempt_errors),
        "error_summary": summary,
        "errors": recalc_errors[: recalc_mod.MAX_LISTED],
        "downstream_exempt_diffs": downstream_exempt_diffs[: recalc_mod.MAX_LISTED],
        "unmatched_cells": unmatched_cells[: recalc_mod.MAX_LISTED],
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
