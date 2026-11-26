#!/usr/bin/env python3
"""Build one bull/bear tracker card file. Stdlib only. No network and no credentials."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
import copy
import importlib.util
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple


CARD_MARKDOWN_MAX_CHARS = 2000
CARD_BODY_MAX_CHARS = 3000
# Keep synchronized with FEISHU_CARD_MAX_BYTES / FEISHU_CARD_MAX_ELEMENTS /
# FEISHU_CARD_TABLE_LIMIT in src/dalton/data_jobs/notification_payloads.py and format.ts
FEISHU_CARD_MAX_BYTES = 30_720
FEISHU_CARD_MAX_ELEMENTS = 200
FEISHU_CARD_TABLE_LIMIT = 5
_GATEWAY_IMG_KEY_PROBE = "img_v3_" + ("x" * 57)
_TABLE_DELIM_CELL_RE = re.compile(r":?-{1,}:?")
_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_BLOCKQUOTE_STEP_RE = re.compile(r"^\s{0,3}> ?")
_LIST_MARKER_PREFIX_RE = re.compile(r"^(\s{0,3}(?:[-*+]|\d{1,9}[.)])\s+)")
_ATX_HEADING_PREFIX_RE = re.compile(r"^#{1,6}(?:\s|$)")
_SECTION_CONT_SUFFIX_RE = re.compile(r"（续）$")
_TABLE_ROW_TRUNCATED_SUFFIX = "（本行过长已截断）"
SKILL_DIR = Path(__file__).resolve().parent.parent


def _load_tracker_config() -> Any:
    path = Path(__file__).resolve().parent / "tracker_config.py"
    name = "dalton_bullbear_tracker_config"
    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"找不到共享配置模块 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def supported_tickers() -> tuple[str, ...]:
    return _load_tracker_config().supported_tickers(skill_dir=SKILL_DIR)

_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(")
_HTML_MEDIA_RE = re.compile(r"<\s*(?:img|table)\b", re.IGNORECASE)


class RenderError(Exception):
    def __init__(self, status: str, message: str, **extra: Any) -> None:
        self.status = status
        self.message = message
        self.extra = extra
        super().__init__(message)


def _strip_blockquote_prefix(line: str) -> tuple[int, str]:
    rem = line
    depth = 0
    while True:
        m = _BLOCKQUOTE_STEP_RE.match(rem)
        if not m:
            break
        depth += 1
        rem = rem[m.end() :]
    return depth, rem


def _split_table_row_cells(line: str) -> list[str]:
    stripped = line.strip()
    if "|" not in stripped:
        return []
    parts = re.split(r"(?<!\\)\|", stripped)
    if stripped.startswith("|") and parts:
        parts = parts[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|") and parts:
        parts = parts[:-1]
    return [cell.strip() for cell in parts]


def _is_table_delimiter_row(line: str, expected_cols: int) -> bool:
    if expected_cols <= 0:
        return False
    stripped = line.strip()
    if "|" not in stripped:
        return (
            expected_cols == 1
            and ":" in stripped
            and bool(_TABLE_DELIM_CELL_RE.fullmatch(stripped))
        )
    cells = _split_table_row_cells(stripped)
    return len(cells) == expected_cols and all(
        bool(_TABLE_DELIM_CELL_RE.fullmatch(cell)) for cell in cells
    )


def _is_table_body_row_inner(inner_line: str) -> bool:
    stripped = inner_line.strip()
    if not stripped or "|" not in stripped:
        return False
    if re.match(r"^(?:```|~~~|#{1,6}(?:\s|$))", stripped):
        return False
    return True


def _extract_markdown_segments(text: str) -> list[tuple[Any, ...]]:
    lines = text.splitlines()
    segments: list[tuple[Any, ...]] = []
    text_buf: list[str] = []
    fence_char = ""
    fence_len = 0

    def flush_text() -> None:
        if not text_buf:
            return
        joined = "\n".join(text_buf).strip()
        text_buf.clear()
        if joined:
            segments.append(("text", joined))

    i = 0
    while i < len(lines):
        line = lines[i]
        if fence_char:
            text_buf.append(line)
            close_m = re.match(rf"^\s*{re.escape(fence_char)}{{{fence_len},}}\s*$", line)
            if close_m:
                fence_char = ""
                fence_len = 0
            i += 1
            continue

        open_m = _FENCE_OPEN_RE.match(line)
        if open_m:
            marker = open_m.group(1)
            fence_char = marker[0]
            fence_len = len(marker)
            text_buf.append(line)
            i += 1
            continue

        hdr_depth, hdr_inner = _strip_blockquote_prefix(line)
        list_m = _LIST_MARKER_PREFIX_RE.match(hdr_inner)
        list_indent = len(list_m.group(1)) if list_m else 0
        hdr_content = hdr_inner[list_indent:] if list_m else hdr_inner
        hdr_leading_spaces = len(hdr_content) - len(hdr_content.lstrip(" "))
        if (
            i + 1 < len(lines)
            and "|" in hdr_content
            and hdr_leading_spaces < 4
            and not hdr_content.startswith("\t")
            and not _ATX_HEADING_PREFIX_RE.match(hdr_content.lstrip())
            and not _FENCE_OPEN_RE.match(hdr_content)
        ):
            sep_depth, sep_inner = _strip_blockquote_prefix(lines[i + 1])
            sep_ok = sep_depth == hdr_depth
            if sep_ok and list_indent > 0:
                sep_leading_spaces = len(sep_inner) - len(sep_inner.lstrip(" "))
                sep_ok = sep_leading_spaces >= list_indent
                sep_inner = sep_inner[list_indent:]
            hdr_cells = _split_table_row_cells(hdr_content) if sep_ok else []
            if hdr_cells and _is_table_delimiter_row(sep_inner, len(hdr_cells)):
                flush_text()
                j = i + 2
                while j < len(lines) and not _FENCE_OPEN_RE.match(lines[j]):
                    row_depth, row_inner = _strip_blockquote_prefix(lines[j])
                    if row_depth != hdr_depth:
                        break
                    if list_indent > 0:
                        row_leading_spaces = len(row_inner) - len(row_inner.lstrip(" "))
                        if row_leading_spaces < list_indent:
                            break
                        row_inner = row_inner[list_indent:]
                    if not _is_table_body_row_inner(row_inner):
                        break
                    j += 1
                segments.append(("table", line, lines[i + 1], lines[i + 2 : j]))
                i = j
                continue

        text_buf.append(line)
        i += 1

    flush_text()
    return segments


def count_markdown_tables(text: str) -> int:
    if "|" not in text:
        return 0
    return sum(1 for seg in _extract_markdown_segments(text) if seg[0] == "table")


def _count_card_tables(node: Any) -> int:
    if isinstance(node, Mapping):
        total = 0
        content = node.get("content")
        if isinstance(content, str):
            total += count_markdown_tables(content)
        for key, val in node.items():
            if key != "content" and isinstance(val, (Mapping, list)):
                total += _count_card_tables(val)
        return total
    if isinstance(node, list):
        return sum(_count_card_tables(item) for item in node)
    return 0


def _split_plain_text_blocks(text: str, max_chars: int) -> list[str]:
    remaining = text.strip()
    blocks: list[str] = []

    while remaining:
        if len(remaining) <= max_chars:
            blocks.append(remaining)
            break

        split_at = remaining.rfind("\n\n", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = remaining.rfind("\n", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = max_chars

        block = remaining[:split_at].rstrip()
        if block:
            blocks.append(block)
        remaining = remaining[split_at:].lstrip("\n")

    return blocks


def _split_table_rows_with_header(
    header_line: str,
    sep_line: str,
    rows: list[str],
    *,
    fits_text: Callable[[str], bool],
) -> list[str]:
    prefix = f"{header_line}\n{sep_line}"
    if not rows:
        return [prefix]
    chunks: list[str] = []
    buf: list[str] = []
    for row in rows:
        candidate = prefix + "\n" + "\n".join([*buf, row])
        if buf and not fits_text(candidate):
            chunks.append(prefix + "\n" + "\n".join(buf))
            buf = [row]
        else:
            buf.append(row)
    if buf:
        chunks.append(prefix + "\n" + "\n".join(buf))
    return chunks


def _truncate_table_row_to_fit(
    header_line: str,
    sep_line: str,
    row_line: str,
    *,
    fits_text: Callable[[str], bool],
) -> str:
    prefix = f"{header_line}\n{sep_line}\n"
    if fits_text(prefix + row_line):
        return prefix + row_line
    stripped = row_line.rstrip()
    closing_pipe = " |" if stripped.endswith("|") and not stripped.endswith("\\|") else ""
    marker = f"{_TABLE_ROW_TRUNCATED_SUFFIX}{closing_pipe}"
    lo, hi = 0, len(stripped)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate_row = stripped[:mid].rstrip() + marker
        if fits_text(prefix + candidate_row):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    truncated = stripped[:best].rstrip() + marker
    if fits_text(prefix + truncated):
        return prefix + truncated
    return prefix + marker


def _format_table_row(
    cells: list[str],
    *,
    quote_prefix: str = "",
    leading_pipe: bool = True,
    trailing_pipe: bool = True,
) -> str:
    inner = " | ".join(cells)
    if leading_pipe and trailing_pipe:
        return f"{quote_prefix}| {inner} |"
    if leading_pipe:
        return f"{quote_prefix}| {inner}"
    if trailing_pipe:
        return f"{quote_prefix}{inner} |"
    return f"{quote_prefix}{inner}"


def _split_oversized_table_row_to_fit(
    header_line: str,
    sep_line: str,
    row_line: str,
    *,
    fits_text: Callable[[str], bool],
) -> list[str]:
    prefix = f"{header_line}\n{sep_line}\n"
    if fits_text(prefix + row_line):
        return [prefix + row_line]

    row_depth, row_inner = _strip_blockquote_prefix(row_line)
    quote_prefix = "> " * row_depth
    stripped = row_inner.strip()
    leading_pipe = stripped.startswith("|")
    trailing_pipe = stripped.endswith("|") and not stripped.endswith("\\|")
    cells = _split_table_row_cells(stripped)
    if not cells:
        raise RenderError("table_unfit", "当前卡片上限无法容纳表头与一行表格数据")

    remaining = list(cells)
    chunks: list[str] = []
    while any(remaining):
        row_cells = [""] * len(remaining)
        progressed = False
        for col_idx, rem_cell in enumerate(remaining):
            if not rem_cell:
                continue
            row_cells[col_idx] = rem_cell
            cand_row = _format_table_row(
                row_cells,
                quote_prefix=quote_prefix,
                leading_pipe=leading_pipe,
                trailing_pipe=trailing_pipe,
            )
            if fits_text(prefix + cand_row):
                remaining[col_idx] = ""
                progressed = True
                continue

            lo, hi = 1, len(rem_cell)
            best = 0
            while lo <= hi:
                mid = (lo + hi) // 2
                row_cells[col_idx] = rem_cell[:mid]
                cand_part = _format_table_row(
                    row_cells,
                    quote_prefix=quote_prefix,
                    leading_pipe=leading_pipe,
                    trailing_pipe=trailing_pipe,
                )
                if fits_text(prefix + cand_part):
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best > 1 and rem_cell[best - 1] == "\\":
                best -= 1
            if best > 0:
                row_cells[col_idx] = rem_cell[:best]
                remaining[col_idx] = rem_cell[best:]
                progressed = True
            else:
                row_cells[col_idx] = ""
            break

        if not progressed:
            raise RenderError("table_unfit", "当前卡片上限无法容纳表头与一行表格数据")

        chunks.append(
            prefix
            + _format_table_row(
                row_cells,
                quote_prefix=quote_prefix,
                leading_pipe=leading_pipe,
                trailing_pipe=trailing_pipe,
            )
        )
    return chunks


def _split_table_to_fitting_chunks(
    header_line: str,
    sep_line: str,
    rows: list[str],
    *,
    fits_text: Callable[[str], bool],
) -> list[str]:
    out: list[str] = []
    for sub_tbl in _split_table_rows_with_header(
        header_line,
        sep_line,
        rows,
        fits_text=fits_text,
    ):
        if fits_text(sub_tbl):
            out.append(sub_tbl)
            continue
        sub_lines = sub_tbl.splitlines()
        sub_rows = sub_lines[2:] if len(sub_lines) > 2 else []
        if not sub_rows:
            raise RenderError("table_unfit", "当前卡片上限无法容纳表头与一行表格数据")
        for single_row in sub_rows:
            out.extend(
                _split_oversized_table_row_to_fit(
                    header_line,
                    sep_line,
                    single_row,
                    fits_text=fits_text,
                )
            )
    return out


def split_markdown_blocks(text: str, max_chars: int = CARD_MARKDOWN_MAX_CHARS) -> list[str]:
    """Split markdown without dropping content, repeating headers when tables split."""
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]

    units: list[str] = []
    for seg in _extract_markdown_segments(cleaned):
        if seg[0] == "table":
            _, hdr, sep, rows = seg
            full_table = hdr + "\n" + sep + ("\n" + "\n".join(rows) if rows else "")
            if len(full_table) <= max_chars:
                units.append(full_table)
            else:
                units.extend(
                    _split_table_rows_with_header(
                        hdr,
                        sep,
                        rows,
                        fits_text=lambda cand: len(cand) <= max_chars,
                    )
                )
        else:
            units.extend(_split_plain_text_blocks(seg[1], max_chars))

    blocks: list[str] = []
    current = ""
    for unit in units:
        if not current:
            current = unit
        elif len(current) + 2 + len(unit) <= max_chars:
            current = f"{current}\n\n{unit}"
        else:
            blocks.append(current)
            current = unit
    if current:
        blocks.append(current)
    return blocks


IMG_LINE_RE = re.compile(r"^IMG:(\S+)\s*$")


def content_to_elements(text: str) -> list[dict[str, Any]]:
    """文本转卡片元素; 行首 `IMG:/abs/path.png` 转为图片占位元素."""
    elements: list[dict[str, Any]] = []
    buf: list[str] = []

    def flush_text() -> None:
        chunk = "\n".join(buf).strip()
        buf.clear()
        if chunk:
            for block in split_markdown_blocks(chunk):
                elements.append({"tag": "markdown", "content": block})

    for line in text.split("\n"):
        m = IMG_LINE_RE.match(line.strip())
        if m:
            flush_text()
            elements.append({"tag": "__img__", "path": m.group(1)})
        else:
            buf.append(line)
    flush_text()
    return elements


NOTATION_SECTION_RE = re.compile(
    r"^(?:📝\s*)?(?:来源|信源|数据源|口径说明|口径限制|信源与数据口径|说明|备注|免责声明)$"
)
COLLAPSIBLE_SUFFIX_RE = re.compile(
    r"[\(（](?:折叠|点击展开)[\)）]$"
)


def auto_infer_tag(title: str) -> Optional[Tuple[str, str]]:
    """根据标题自动推断卡片顶部的业务标签与颜色."""
    t = title.lower()
    if "晨报" in title:
        return ("市场晨报", "blue")
    if "晚报" in title:
        return ("市场晚报", "purple")
    if "etf" in t:
        return ("ETF Tracking", "indigo")
    if "token" in t or "用量" in title:
        return ("用量与成本", "blue")
    if "ai" in t and ("投资" in title or "信号" in title or "日报" in title):
        return ("AI 投研", "purple")
    if "周报" in title or "weekly" in t or "tracker" in t:
        return ("周度 Tracking", "indigo")
    if "日报" in title or "daily" in t:
        return ("每日追踪", "blue")
    if "风险" in title or "risk" in t or "预警" in title or "alert" in t:
        return ("风险信号", "orange")
    if "监控" in title or "monitor" in t:
        return ("实时监控", "wathet")
    if "扫描" in title or "scan" in t:
        return ("趋势扫描", "turquoise")
    if "总结" in title or "summary" in t:
        return ("每日总结", "blue")
    return None


def _replace_card_img_placeholders_for_size(node: Any) -> Any:
    if isinstance(node, list):
        return [_replace_card_img_placeholders_for_size(item) for item in node]
    if isinstance(node, Mapping):
        if node.get("tag") == "__img__":
            raw_alt = node.get("alt")
            if isinstance(raw_alt, Mapping):
                alt_obj: dict[str, Any] = copy.deepcopy(dict(raw_alt))
            else:
                alt_obj = {
                    "tag": "plain_text",
                    "content": raw_alt if isinstance(raw_alt, str) else "",
                }
            return {
                "tag": "img",
                "img_key": _GATEWAY_IMG_KEY_PROBE,
                "alt": alt_obj,
                "mode": "fit_horizontal",
                "preview": True,
            }
        return {
            key: _replace_card_img_placeholders_for_size(val)
            for key, val in node.items()
        }
    return node


def feishu_card_serialized_utf8_bytes(card: Mapping[str, Any]) -> int:
    return len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def feishu_card_gateway_utf8_bytes(card: Mapping[str, Any]) -> int:
    return feishu_card_serialized_utf8_bytes(_replace_card_img_placeholders_for_size(card))


def is_feishu_card_within_envelope(
    card: Mapping[str, Any],
    *,
    max_bytes: int = FEISHU_CARD_MAX_BYTES,
    max_elements: int = FEISHU_CARD_MAX_ELEMENTS,
    max_tables: int = FEISHU_CARD_TABLE_LIMIT,
) -> bool:
    if feishu_card_gateway_utf8_bytes(card) > max_bytes:
        return False
    body = card.get("body")
    elements = body.get("elements") if isinstance(body, Mapping) else card.get("elements")
    if isinstance(elements, list):
        total_elements = len(elements)
        for element in elements:
            if (
                isinstance(element, Mapping)
                and element.get("tag") == "action"
                and isinstance(element.get("actions"), list)
            ):
                total_elements += len(element["actions"])
        if total_elements > max_elements:
            return False
    return _count_card_tables(card) <= max(1, int(max_tables))


def _parse_markdown_sections_and_subtitle(
    markdown: str,
    title: str,
    subtitle: str = "",
) -> tuple[list[dict[str, Any]], str]:
    sections: list[dict[str, Any]] = []
    current_section: dict[str, Any] = {"title": "", "content": []}

    def flush_current_section() -> None:
        nonlocal current_section
        content = current_section["content"]
        while content and not content[0].strip():
            content.pop(0)
        while content and not content[-1].strip():
            content.pop()
        section_title = str(current_section["title"] or "").strip()
        is_duplicate_card_title = bool(
            section_title and not content and title.strip().startswith(section_title)
        )
        if content or (section_title and not is_duplicate_card_title):
            sections.append(current_section)
        current_section = {"title": "", "content": []}

    lines = markdown.strip().split("\n")
    i = 0
    extracted_subtitle = ""

    while i < len(lines):
        line = lines[i]
        if line.startswith("# ") and not line.startswith("## "):
            if not subtitle and i + 1 < len(lines) and lines[i + 1].startswith("> "):
                extracted_subtitle = lines[i + 1][2:].strip()
                i += 2
                continue
            i += 1
            continue

        heading = re.match(r"^#{2,4}\s+(.+?)\s*$", line)
        if heading:
            flush_current_section()
            current_section = {"title": heading.group(1), "content": []}
            i += 1
            continue

        current_section["content"].append(line)
        i += 1

    flush_current_section()
    return sections, subtitle or extracted_subtitle


def _build_feishu_card_header(
    *,
    title: str,
    subtitle: str = "",
    header_template: str = "indigo",
    tag: str = "",
    tag_color: str = "",
) -> dict[str, Any]:
    header: dict[str, Any] = {
        "title": {"tag": "plain_text", "content": title},
        "template": header_template or "indigo",
    }
    if subtitle:
        header["subtitle"] = {"tag": "plain_text", "content": subtitle}
    if tag:
        header["text_tag_list"] = [
            {
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": tag},
                "color": tag_color or "blue",
            }
        ]
    else:
        inferred = auto_infer_tag(title)
        if inferred:
            header["text_tag_list"] = [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": inferred[0]},
                    "color": tag_color or inferred[1],
                }
            ]
    return header


def _sections_to_card_elements(
    sections: list[dict[str, Any]],
    *,
    raw_markdown: str = "",
) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    titled_sections = [s for s in sections if s["title"]]

    if not titled_sections:
        fallback_text = (
            raw_markdown.strip()
            if raw_markdown.strip()
            else "\n".join(
                "\n".join(s.get("content") or []).strip() for s in sections
            ).strip()
        )
        elements.extend(content_to_elements(fallback_text))
        return elements

    for section in sections:
        sec_title = section["title"].strip()
        section_text = "\n".join(section["content"]).strip()
        if not sec_title:
            if not section_text:
                continue
            elements.extend(content_to_elements(section_text))
            elements.append({"tag": "hr"})
            continue

        if NOTATION_SECTION_RE.match(_SECTION_CONT_SUFFIX_RE.sub("", sec_title).strip()):
            if elements and elements[-1].get("tag") != "hr":
                elements.append({"tag": "hr"})
            notation_lines = [
                f"<font color='grey'>{ln}</font>" if ln.strip() else ""
                for ln in f"**{sec_title}**\n{section_text}".splitlines()
            ]
            elements.append(
                {
                    "tag": "markdown",
                    "text_size": "notation",
                    "content": "\n".join(notation_lines),
                }
            )
            continue

        is_collapsible = bool(COLLAPSIBLE_SUFFIX_RE.search(sec_title)) or sec_title.startswith(
            ("例行披露", "长尾")
        )
        if is_collapsible:
            if elements and elements[-1].get("tag") != "hr":
                elements.append({"tag": "hr"})
            clean_title = COLLAPSIBLE_SUFFIX_RE.sub("", sec_title).strip()
            inner_elements = content_to_elements(section_text)
            elements.append(
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {
                        "title": {"tag": "markdown", "content": f"**{clean_title}**"},
                        "vertical_align": "center",
                        "padding": "4px 8px 4px 8px",
                        "icon": {
                            "tag": "standard_icon",
                            "token": "down-small-ccm_outlined",
                            "size": "12px 12px",
                        },
                        "icon_position": "right",
                        "icon_expanded_angle": -180,
                    },
                    "border": {"color": "grey", "corner_radius": "6px"},
                    "vertical_spacing": "6px",
                    "padding": "8px 8px 8px 8px",
                    "elements": inner_elements,
                }
            )
            continue

        section_content = f"**{sec_title}**\n\n{section_text}"
        elements.extend(content_to_elements(section_content))
        elements.append({"tag": "hr"})

    while elements and elements[-1].get("tag") == "hr":
        elements.pop()
    return elements


def _build_feishu_card(
    header: dict[str, Any],
    elements: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": header,
        "body": {
            "direction": "vertical",
            "vertical_spacing": "8px",
            "elements": elements,
        },
    }


def _split_oversized_section(
    sec_title: str,
    section_text: str,
    *,
    fits_fn: Callable[[list[dict[str, Any]]], bool],
) -> list[dict[str, Any]]:
    segments = _extract_markdown_segments(section_text.strip())
    if not segments:
        return [{"title": sec_title, "content": []}]

    def make_sec(part_index: int, lines: list[str]) -> dict[str, Any]:
        eff_title = sec_title if not sec_title or part_index == 0 else f"{sec_title}（续）"
        return {"title": eff_title, "content": lines}

    def fits_single(part_index: int, text: str) -> bool:
        sec = make_sec(part_index, text.splitlines() if text else [])
        return fits_fn([sec])

    def split_plain_unit(unit_text: str) -> list[str]:
        parts = [p for p in re.split(r"\n{2,}", unit_text.strip()) if p.strip()]
        if len(parts) <= 1:
            parts = [ln for ln in unit_text.splitlines() if ln]
        out: list[str] = []
        for unit in parts:
            if fits_single(0, unit) and fits_single(1, unit):
                out.append(unit)
                continue
            lines = unit.splitlines() if "\n" in unit else [unit]
            for ln in lines:
                if fits_single(0, ln) and fits_single(1, ln):
                    out.append(ln)
                    continue
                rem = ln
                while rem:
                    lo, hi = 1, len(rem)
                    best = 1
                    while lo <= hi:
                        mid = (lo + hi) // 2
                        cand = rem[:mid]
                        if fits_single(0, cand) and fits_single(1, cand):
                            best = mid
                            lo = mid + 1
                        else:
                            hi = mid - 1
                    out.append(rem[:best])
                    rem = rem[best:]
        return out

    normalized_units: list[str] = []
    for seg in segments:
        if seg[0] == "table":
            _, hdr, sep, rows = seg
            full_table = hdr + "\n" + sep + ("\n" + "\n".join(rows) if rows else "")
            if fits_single(0, full_table) and fits_single(1, full_table):
                normalized_units.append(full_table)
            else:
                normalized_units.extend(
                    _split_table_to_fitting_chunks(
                        hdr,
                        sep,
                        rows,
                        fits_text=lambda cand: fits_single(0, cand) and fits_single(1, cand),
                    )
                )
        else:
            normalized_units.extend(split_plain_unit(seg[1]))

    out_sections: list[dict[str, Any]] = []
    buf: list[str] = []
    sep = "\n\n"
    for unit in normalized_units:
        candidate_text = sep.join([*buf, unit]) if buf else unit
        if buf and not fits_single(len(out_sections), candidate_text):
            out_sections.append(make_sec(len(out_sections), sep.join(buf).splitlines()))
            buf = [unit]
        else:
            buf.append(unit)
    if buf:
        out_sections.append(make_sec(len(out_sections), sep.join(buf).splitlines()))
    return out_sections


def markdown_to_card_elements(
    markdown: str,
    title: str,
    subtitle: str = "",
    header_template: str = "indigo",
    tag: str = "",
    tag_color: str = "",
) -> dict[str, Any]:
    """将 markdown 内容转成飞书 Card Schema 2.0 卡片 JSON."""
    sections, effective_subtitle = _parse_markdown_sections_and_subtitle(
        markdown, title, subtitle
    )
    header = _build_feishu_card_header(
        title=title,
        subtitle=effective_subtitle,
        header_template=header_template,
        tag=tag,
        tag_color=tag_color,
    )
    elements = _sections_to_card_elements(sections, raw_markdown=markdown)
    return _build_feishu_card(header, elements)


def markdown_to_card_chunks(
    markdown: str,
    title: str,
    subtitle: str = "",
    header_template: str = "indigo",
    tag: str = "",
    tag_color: str = "",
    *,
    max_bytes: int = FEISHU_CARD_MAX_BYTES,
    max_elements: int = FEISHU_CARD_MAX_ELEMENTS,
    max_tables: int = FEISHU_CARD_TABLE_LIMIT,
) -> list[dict[str, Any]]:
    """Render Markdown into one or more Feishu Schema 2.0 cards within gateway limits."""
    sections, effective_subtitle = _parse_markdown_sections_and_subtitle(
        markdown, title, subtitle
    )
    single_header = _build_feishu_card_header(
        title=title,
        subtitle=effective_subtitle,
        header_template=header_template,
        tag=tag,
        tag_color=tag_color,
    )
    single_elements = _sections_to_card_elements(sections, raw_markdown=markdown)
    single_card = _build_feishu_card(single_header, single_elements)
    if is_feishu_card_within_envelope(
        single_card,
        max_bytes=max_bytes,
        max_elements=max_elements,
        max_tables=max_tables,
    ):
        return [single_card]

    probe_header = _build_feishu_card_header(
        title=f"{title}（999/999）",
        subtitle=effective_subtitle,
        header_template=header_template,
        tag=tag,
        tag_color=tag_color,
    )

    def _bucket_fits(bucket_sections: list[dict[str, Any]], *, fallback_md: str = "") -> bool:
        elems = _sections_to_card_elements(bucket_sections, raw_markdown=fallback_md)
        probe_card = _build_feishu_card(probe_header, elems)
        return is_feishu_card_within_envelope(
            probe_card,
            max_bytes=max_bytes,
            max_elements=max_elements,
            max_tables=max_tables,
        )

    titled_sections = [s for s in sections if s["title"]]
    active_sections = [
        s
        for s in sections
        if str(s.get("title") or "").strip() or "\n".join(s.get("content") or []).strip()
    ]
    atomic_sections: list[dict[str, Any]] = []
    if not titled_sections:
        for sub in _split_oversized_section(
            "",
            markdown.strip(),
            fits_fn=lambda cand: _bucket_fits(
                cand,
                fallback_md="\n".join(cand[0].get("content") or []),
            ),
        ):
            atomic_sections.append(sub)
    else:
        for sec in active_sections:
            if _bucket_fits([sec]):
                atomic_sections.append(sec)
            else:
                sec_title = str(sec["title"]).strip()
                sec_text = "\n".join(sec.get("content") or []).strip()
                atomic_sections.extend(
                    _split_oversized_section(
                        sec_title,
                        sec_text,
                        fits_fn=lambda cand: _bucket_fits(cand),
                    )
                )

    buckets: list[list[dict[str, Any]]] = []
    if not titled_sections:
        for sec in atomic_sections:
            buckets.append([sec])
    else:
        current_bucket: list[dict[str, Any]] = []
        for sec in atomic_sections:
            candidate = [*current_bucket, sec]
            if current_bucket and not _bucket_fits(candidate):
                buckets.append(current_bucket)
                current_bucket = [sec]
            else:
                current_bucket = candidate
        if current_bucket:
            buckets.append(current_bucket)

    total = len(buckets)
    cards: list[dict[str, Any]] = []
    for idx, bucket in enumerate(buckets, start=1):
        chunk_title = f"{title}（{idx}/{total}）" if total > 1 else title
        header = _build_feishu_card_header(
            title=chunk_title,
            subtitle=effective_subtitle,
            header_template=header_template,
            tag=tag,
            tag_color=tag_color,
        )
        if not titled_sections:
            elems = _sections_to_card_elements(
                [],
                raw_markdown="\n".join(bucket[0].get("content") or []),
            )
        else:
            elems = _sections_to_card_elements(bucket)
        cards.append(_build_feishu_card(header, elems))
    return cards


def card_markdown(md: str) -> str:
    """飞书卡片 markdown 的坑：Discord 的 <url> 写法不认、数字区间里的 ~ 可能被吃掉。"""
    md = re.sub(r"<(https?://[^>\s]+)>", r"\1", md)
    md = re.sub(r"(\d)\s*~\s*(\d)", r"\1至\2", md)
    return md.strip()


DEBATE_RE = re.compile(r"(?<![A-Za-z0-9])D(\d{1,2})(?![A-Za-z0-9])")


def gloss_debates(md: str, thesis: Path) -> tuple[str, list]:
    """卡片读者看不到 thesis：每条 Debate 第一次出现时要带括号说明。
    模型漏写时，用 thesis 里「### Dn 标题」补上（去掉标题自带的括号）。"""
    try:
        heads = dict(re.findall(r"^###\s*D(\d{1,2})\s+(.+?)\s*$", thesis.read_text(), re.M))
    except OSError:
        return md, []
    heads = {k: re.sub(r"[（(][^）)]*[）)]\s*$", "", v).strip() for k, v in heads.items()}
    urls = [u.span() for u in re.finditer(r"https?://[^\s)）>]+", md)]
    done, added, out, pos = set(), [], [], 0
    for m in DEBATE_RE.finditer(md):
        n = m.group(1)
        if n in done or n not in heads or any(a <= m.start() < b for a, b in urls):
            continue
        done.add(n)
        if re.match(r"\s*[（(]", md[m.end():]):
            continue
        out.append(md[pos:m.end()] + f"（{heads[n]}）")
        # 「D2 往……」补成「D2（……）往……」，去掉中文前多出来的空格
        pos = m.end() + 1 if re.match(r" [^\x00-\x7f]", md[m.end():m.end() + 2]) else m.end()
        added.append(f"D{n}")
    out.append(md[pos:])
    return "".join(out), added


def _inside(workspace: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(workspace.resolve())
    except (OSError, ValueError):
        return False
    return True


def open_workspace(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    resolved = path.resolve()
    if ".openclaw" in path.parts or ".openclaw" in resolved.parts:
        raise RenderError("openclaw_root", "拒绝读写旧目录")
    if not resolved.is_dir():
        raise RenderError("workspace_not_directory", "工作区不是目录")
    return resolved


def ensure_relative(workspace: Path, raw: str) -> Path:
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        raise RenderError("path_escape", "路径必须留在工作区内")
    current = workspace
    for part in relative.parts:
        if part in ("", ".", ".."):
            raise RenderError("path_escape", "路径必须留在工作区内")
        current = current / part
        if current.is_symlink() and not _inside(workspace, current):
            raise RenderError("symlink_escape", "符号链接指向工作区外")
        if current.is_symlink():
            current = current.resolve()
            if not _inside(workspace, current):
                raise RenderError("symlink_escape", "符号链接指向工作区外")
    if not _inside(workspace, current):
        raise RenderError("path_escape", "路径必须留在工作区内")
    return current


def parse_run_stamp(run_id: str) -> datetime:
    if not isinstance(run_id, str) or re.fullmatch(r"\d{8}-\d{4}", run_id) is None:
        raise RenderError("bad_run_id", "run_id 需要 YYYYMMDD-HHMM")
    try:
        return datetime.strptime(run_id, "%Y%m%d-%H%M")
    except ValueError as exc:
        raise RenderError("bad_run_id", "run_id 不是有效时间") from exc


def _unsupported(markdown: str) -> bool:
    if _MD_IMAGE_RE.search(markdown) or _HTML_MEDIA_RE.search(markdown):
        return True
    for line in markdown.splitlines():
        stripped = line.strip()
        if IMG_LINE_RE.match(stripped) or _TABLE_LINE_RE.match(line) or _TABLE_SEP_RE.match(line):
            return True
    return False


def _forbidden_payload(obj: Any) -> bool:
    if isinstance(obj, dict):
        if obj.get("tag") == "__img__" or "path" in obj or "attachment" in obj:
            return True
        return any(_forbidden_payload(value) for value in obj.values())
    if isinstance(obj, list):
        return any(_forbidden_payload(value) for value in obj)
    return False


def load_card_style(ticker: str = "MSFT", workspace: Path | str | None = None) -> dict[str, str]:
    _ = workspace
    chosen = str(ticker or "").strip().upper()
    cfg_mod = _load_tracker_config()
    if chosen not in cfg_mod.supported_tickers(skill_dir=SKILL_DIR):
        raise RenderError("unsupported_ticker", f"不支持的 ticker：{chosen or '空'}")
    data = cfg_mod.load_validated_config(chosen, skill_dir=SKILL_DIR)
    if data is None:
        raise RenderError("unsupported_ticker", f"不支持的 ticker：{chosen or '空'}")
    return {key: str(data["card"][key]) for key in cfg_mod.CARD_STYLE_KEYS}


def dump_card(card: Any) -> str:
    return json.dumps(card, ensure_ascii=False, indent=2) + "\n"


def prepare_body(markdown: str, thesis: Path) -> tuple[str, list[str]]:
    if _unsupported(markdown):
        raise RenderError("unsupported_content", "卡片不接受图片或表格")
    prepared, glossed = gloss_debates(card_markdown(markdown), thesis)
    if _unsupported(prepared):
        raise RenderError("unsupported_content", "卡片不接受图片或表格")
    if len(prepared) > CARD_BODY_MAX_CHARS:
        raise RenderError(
            "over_cap",
            f"正文 {len(prepared)} 字，超过 {CARD_BODY_MAX_CHARS} 字上限；压缩新信息和来源后重发",
            chars=len(prepared),
        )
    return prepared, [str(item) for item in glossed]


def render_card(
    workspace: Path | str,
    run_id: str,
    file_rel: str,
    output_rel: str,
    ticker: str = "MSFT",
) -> dict[str, Any]:
    root = open_workspace(str(workspace))
    style = load_card_style(ticker, workspace=root)
    stamp = parse_run_stamp(run_id)
    source = ensure_relative(root, file_rel)
    output = ensure_relative(root, output_rel)
    packet = ensure_relative(root, f"research/tracker/{run_id}/packet.json")
    if not packet.is_file() or packet.is_symlink():
        raise RenderError("packet_missing", "运行目录没有 packet.json")
    if source.is_symlink() or not source.is_file():
        raise RenderError("card_missing", "找不到卡片正文")
    prepared, glossed = prepare_body(source.read_text(encoding="utf-8"), root / "research" / "tracker" / "thesis.md")
    title = f"{style['title_prefix']}{stamp:%m-%d %H:%M} ET"
    cards = markdown_to_card_chunks(
        prepared,
        title=title,
        subtitle=style["subtitle"],
        header_template=style["header_template"],
        tag=style["tag"],
        tag_color=style["tag_color"],
    )
    for item in cards:
        if _forbidden_payload(item):
            raise RenderError("unsupported_content", "卡片不接受图片或表格")
    payload_obj: Any = cards[0] if len(cards) == 1 else cards
    text = dump_card(payload_obj)
    if output.is_symlink():
        raise RenderError("symlink_escape", "拒绝透过符号链接写入卡片")
    reused = False
    if output.exists():
        current = output.read_bytes()
        if current != text.encode("utf-8"):
            raise RenderError("card_conflict", "已有卡片与本次正文不同，保留原字节")
        reused = True
    else:
        _atomic_write(root, output, text)
    return {
        "ok": True,
        "reused": reused,
        "output": output_rel,
        "chars": len(prepared),
        "glossed": glossed,
    }


def _atomic_write(workspace: Path, path: Path, text: str) -> None:
    if not _inside(workspace, path.parent):
        raise RenderError("path_escape", "拒绝写入工作区外")
    if path.is_symlink():
        raise RenderError("symlink_escape", "拒绝透过符号链接写入卡片")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构造一张多空日跟踪卡片")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = render_card(args.workspace_dir, args.run_id, args.file, args.output, args.ticker)
    except RenderError as exc:
        payload: dict[str, Any] = {"ok": False, "status": exc.status, "error": exc.message}
        payload.update(exc.extra)
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 2
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
