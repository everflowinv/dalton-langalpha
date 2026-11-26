#!/usr/bin/env python3
"""Convert a Markdown file into a Feishu Card Schema 2.0 JSON array for `automation_report`.

Reuses ``markdown_to_card_chunks`` from sibling ``render_card.py`` in
``dalton-bullbear-tracker/scripts/`` (stdlib only, no network, no credentials).
Automatically splits cards to satisfy Feishu limits (30,720 bytes, 200 elements,
5 tables per card), strips local image paths (including reference-style Markdown
images and backtick-quoted local paths) and ``IMG:`` markers, and appends a
``图表见 Dalton`` footer note when any image/local-path line is removed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_CARDS = 20
MAX_CARD_JSON_BYTES = 262_144
FEISHU_HARD_MAX_BYTES = 30_720
FEISHU_HARD_MAX_ELEMENTS = 200
FEISHU_HARD_MAX_TABLES = 5
CHARTS_IN_DALTON_NOTE = "图表见 Dalton"

_FORBIDDEN_KEYS = frozenset(
    {
        "chat_id",
        "open_id",
        "receive_id",
        "union_id",
        "user_id",
        "email",
        "download_url",
        "file_path",
        "local_path",
        "attachment",
        "attachments",
        "artifact",
        "artifacts",
        "images",
        "path",
    }
)
_URL_KEYS = frozenset(
    {"url", "default_url", "pc_url", "ios_url", "android_url", "href"}
)
_HTTP_URL_RE = re.compile(r"https?://[^\s)>\]\"'`]+", re.IGNORECASE)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]*)\)")
_MD_REF_DEF_RE = re.compile(
    r"^\s{0,3}\[([^\]]+)\]:\s*<?([^\s>\"']+)>?(?:\s+.*)?$"
)
_MD_REF_IMG_FULL_RE = re.compile(r"!\[([^\]]*)\]\[([^\]]*)\]")
_MD_REF_IMG_SHORT_RE = re.compile(r"!\[([^\]]+)\](?![(\[])")
_HTML_IMG_TAG_RE = re.compile(r"<\s*img\b([^>]*)>", re.IGNORECASE)
_HTML_SRC_ATTR_RE = re.compile(
    r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.IGNORECASE,
)
_LOCAL_REF_RE = re.compile(
    r"(?:^|[\s\"'(=`：:（(「【\[])(?:"
    r"file://"
    r"|/(?:Users|home|tmp|var|etc|private|opt)/"
    r"|[A-Za-z]:\\"
    r"|\./"
    r"|\.\./"
    r")",
    re.IGNORECASE,
)
_BACKTICK_ABS_PATH_RE = re.compile(
    r"`(?:"
    r"/(?:[^`\r\n/]+/)+[^`\r\n/]*"
    r"|/[^`\r\n/]+\.[A-Za-z0-9]{1,8}"
    r"|/(?:Users|home|tmp|var|etc|private|opt|mnt|srv|data|Volumes|root|workspace)(?:/[^`\r\n]*)?"
    r"|[A-Za-z]:\\[^`\r\n]*"
    r"|file://[^`\r\n]*"
    r"|\.{1,2}/[^`\r\n]*"
    r")`",
    re.IGNORECASE,
)
_LOCAL_IMAGE_PATH_RE = re.compile(
    r"(?:file://|[A-Za-z]:\\|/(?:[A-Za-z0-9._-]+/)+|\.{1,2}/|(?:[A-Za-z0-9._-]+/)+)"
    r"[A-Za-z0-9._\\/-]*\.(?:png|jpe?g|gif|webp|svg|bmp)"
    r"(?=$|[\s\"'`)\],;，。；）】>])",
    re.IGNORECASE,
)
_LOCAL_REF_DEF_TARGET_RE = re.compile(
    r"(?:^/|^[A-Za-z]:\\|^file://|^\.{1,2}/|[\\/].+\.(?:png|jpe?g|gif|webp|svg|bmp)$)",
    re.IGNORECASE,
)
_HEADING_PREFIX_RE = re.compile(r"^#{1,6}\s+")


def find_renderer() -> Path:
    """Locate sibling ``render_card.py`` inside ``dalton-bullbear-tracker/scripts/``."""
    here = Path(__file__).resolve()
    sibling = here.parent / "render_card.py"
    if sibling.is_file() and not sibling.is_symlink():
        return sibling
    raise FileNotFoundError(
        "shared card renderer (dalton-bullbear-tracker/scripts/render_card.py) is not available"
    )


def load_renderer_module() -> Any:
    """Load the shared ``render_card.py`` module without duplicating rendering logic."""
    path = find_renderer()
    name = "dalton_bullbear_shared_render_card"
    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError("shared card renderer is not available")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def extract_title_from_markdown(markdown: str) -> str:
    """Return the first non-empty line of ``markdown`` with leading ATX ``#`` stripped."""
    for raw_line in markdown.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        title = _HEADING_PREFIX_RE.sub("", stripped).strip()
        if title:
            return title
    raise ValueError("title is empty and markdown has no non-empty first line")


def _has_non_http_html_img(line: str) -> bool:
    for match in _HTML_IMG_TAG_RE.finditer(line):
        attrs = match.group(1) or ""
        src_m = _HTML_SRC_ATTR_RE.search(attrs)
        if src_m is None:
            return True
        src = (src_m.group(1) or src_m.group(2) or src_m.group(3) or "").strip()
        if not src.lower().startswith(("http://", "https://")):
            return True
    return False


def _collect_reference_definitions(lines: list[str]) -> dict[str, str]:
    ref_defs: dict[str, str] = {}
    for line in lines:
        m = _MD_REF_DEF_RE.match(line)
        if m:
            ref_defs[m.group(1).strip().casefold()] = m.group(2).strip()
    return ref_defs


def _line_has_local_ref_image(line: str, ref_defs: Mapping[str, str]) -> bool:
    for m in _MD_REF_IMG_FULL_RE.finditer(line):
        label = (m.group(2) or m.group(1)).strip().casefold()
        dest = ref_defs.get(label, "")
        if not dest.lower().startswith(("http://", "https://")):
            return True
    for m in _MD_REF_IMG_SHORT_RE.finditer(line):
        label = m.group(1).strip().casefold()
        if label in ref_defs and not ref_defs[label].lower().startswith(("http://", "https://")):
            return True
    return False


def _collect_local_image_ref_labels(lines: list[str], ref_defs: Mapping[str, str]) -> set[str]:
    labels: set[str] = set()
    for line in lines:
        for m in _MD_REF_IMG_FULL_RE.finditer(line):
            label = (m.group(2) or m.group(1)).strip().casefold()
            dest = ref_defs.get(label, "")
            if not dest.lower().startswith(("http://", "https://")):
                labels.add(label)
        for m in _MD_REF_IMG_SHORT_RE.finditer(line):
            label = m.group(1).strip().casefold()
            if label in ref_defs and not ref_defs[label].lower().startswith(("http://", "https://")):
                labels.add(label)
    return labels


def _classify_line(
    line: str,
    *,
    ref_defs: Mapping[str, str],
    local_img_labels: set[str],
) -> tuple[bool, bool]:
    """Return ``(drop_line, needs_charts_note)`` for one Markdown line."""
    folded = line.casefold()
    if "IMG:" in line or "__img__" in folded:
        return True, True

    for match in _MD_IMAGE_RE.finditer(line):
        dest = match.group(1).strip()
        if not dest.lower().startswith(("http://", "https://")):
            return True, True

    if _line_has_local_ref_image(line, ref_defs):
        return True, True

    ref_def_match = _MD_REF_DEF_RE.match(line)
    if ref_def_match:
        label = ref_def_match.group(1).strip().casefold()
        dest = ref_def_match.group(2).strip()
        if label in local_img_labels:
            return True, True
        if not dest.lower().startswith(("http://", "https://")) and (
            _LOCAL_REF_DEF_TARGET_RE.search(dest)
            or _LOCAL_IMAGE_PATH_RE.search(dest)
            or _LOCAL_REF_RE.search(dest)
        ):
            return True, True

    if _has_non_http_html_img(line):
        return True, True

    line_without_http = _HTTP_URL_RE.sub("", line)
    if _BACKTICK_ABS_PATH_RE.search(line_without_http):
        return True, True

    if _LOCAL_IMAGE_PATH_RE.search(line_without_http):
        return True, True

    if _LOCAL_REF_RE.search(line_without_http):
        return True, True

    return False, False


def sanitize_markdown_for_report(markdown: str) -> tuple[str, bool]:
    """Remove lines containing local image paths, ``IMG:`` markers, or local path refs.

    Returns ``(cleaned_markdown, stripped_any_image_or_local_path)``.
    """
    lines = markdown.splitlines()
    ref_defs = _collect_reference_definitions(lines)
    local_img_labels = _collect_local_image_ref_labels(lines, ref_defs)

    kept_lines: list[str] = []
    stripped_image = False
    for line in lines:
        drop, needs_note = _classify_line(
            line,
            ref_defs=ref_defs,
            local_img_labels=local_img_labels,
        )
        if drop:
            if needs_note:
                stripped_image = True
            continue
        kept_lines.append(line)

    cleaned = "\n".join(kept_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, stripped_image


def _charts_note_element() -> dict[str, Any]:
    return {
        "tag": "markdown",
        "text_size": "notation",
        "content": f"<font color='grey'>{CHARTS_IN_DALTON_NOTE}</font>",
    }


def _scan_card_string(value: str, *, url_key: bool) -> None:
    folded = value.casefold()
    if "__img__" in folded or "IMG:" in value:
        raise ValueError("card contains forbidden image placeholder")
    for match in _MD_IMAGE_RE.finditer(value):
        dest = match.group(1).strip()
        if not dest.lower().startswith(("https://", "http://")):
            raise ValueError("card contains non-http markdown image")
    ref_defs = _collect_reference_definitions(value.splitlines())
    if _line_has_local_ref_image(value, ref_defs):
        raise ValueError("card contains non-http reference markdown image")
    for dest in ref_defs.values():
        if not dest.lower().startswith(("https://", "http://")) and _LOCAL_REF_DEF_TARGET_RE.search(dest):
            raise ValueError("card contains local reference definition")
    value_without_http = _HTTP_URL_RE.sub("", value)
    if _BACKTICK_ABS_PATH_RE.search(value_without_http) or _LOCAL_REF_RE.search(value_without_http):
        raise ValueError("card contains local path reference")
    if url_key and not value.lower().startswith(("https://", "http://")):
        raise ValueError("card contains non-http url field")


def _scan_card_node(node: Any, *, url_key: bool = False) -> None:
    if node is None or isinstance(node, (bool, int, float)):
        return
    if isinstance(node, str):
        _scan_card_string(node, url_key=url_key)
        return
    if isinstance(node, list):
        for item in node:
            _scan_card_node(item)
        return
    if isinstance(node, Mapping):
        for key, val in node.items():
            if not isinstance(key, str) or key.casefold() in _FORBIDDEN_KEYS:
                raise ValueError(f"card contains forbidden key: {key!r}")
            _scan_card_node(val, url_key=key.casefold() in _URL_KEYS)
        return
    raise TypeError("card contains non-JSON value")


def _resolve_effective_limits(
    *,
    max_bytes: int | None,
    max_elements: int | None,
    max_tables: int | None,
) -> tuple[int, int, int]:
    eff_bytes = FEISHU_HARD_MAX_BYTES if max_bytes is None else int(max_bytes)
    eff_elements = FEISHU_HARD_MAX_ELEMENTS if max_elements is None else int(max_elements)
    eff_tables = FEISHU_HARD_MAX_TABLES if max_tables is None else int(max_tables)

    if eff_bytes <= 0 or eff_bytes > FEISHU_HARD_MAX_BYTES:
        raise ValueError(
            f"--max-bytes must be between 1 and {FEISHU_HARD_MAX_BYTES} (got {eff_bytes})"
        )
    if eff_elements <= 0 or eff_elements > FEISHU_HARD_MAX_ELEMENTS:
        raise ValueError(
            f"--max-elements must be between 1 and {FEISHU_HARD_MAX_ELEMENTS} (got {eff_elements})"
        )
    if eff_tables <= 0 or eff_tables > FEISHU_HARD_MAX_TABLES:
        raise ValueError(
            f"--max-tables must be between 1 and {FEISHU_HARD_MAX_TABLES} (got {eff_tables})"
        )
    return eff_bytes, eff_elements, eff_tables


def _validate_cards(
    cards: list[dict[str, Any]],
    *,
    renderer: Any,
    max_bytes: int,
    max_elements: int,
    max_tables: int,
) -> None:
    if not cards:
        raise ValueError("card list is empty")
    if len(cards) > MAX_CARDS:
        raise ValueError(f"card count {len(cards)} exceeds MAX_CARDS ({MAX_CARDS})")
    for idx, card in enumerate(cards, start=1):
        if not isinstance(card, dict) or card.get("schema") != "2.0":
            raise ValueError(f"card {idx} is not Feishu Card Schema 2.0")
        body = card.get("body")
        elements = body.get("elements") if isinstance(body, dict) else None
        if not isinstance(elements, list) or not elements:
            raise ValueError(f"card {idx} has empty body elements")
        _scan_card_node(card)
        if not renderer.is_feishu_card_within_envelope(
            card,
            max_bytes=max_bytes,
            max_elements=max_elements,
            max_tables=max_tables,
        ):
            raise ValueError(f"card {idx} exceeds requested card limits")
        if not renderer.is_feishu_card_within_envelope(
            card,
            max_bytes=FEISHU_HARD_MAX_BYTES,
            max_elements=FEISHU_HARD_MAX_ELEMENTS,
            max_tables=FEISHU_HARD_MAX_TABLES,
        ):
            raise ValueError(f"card {idx} exceeds Feishu hard card limits")
    total_bytes = len(
        json.dumps(cards, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if total_bytes > MAX_CARD_JSON_BYTES:
        raise ValueError(
            f"total card batch size {total_bytes} exceeds MAX_CARD_JSON_BYTES ({MAX_CARD_JSON_BYTES})"
        )


def markdown_to_report_cards(
    markdown: str,
    *,
    title: str,
    subtitle: str = "",
    header_template: str = "indigo",
    tag: str = "",
    tag_color: str = "",
    max_bytes: int | None = None,
    max_elements: int | None = None,
    max_tables: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Render Markdown into validated Feishu Schema 2.0 cards for ``automation_report``.

    Returns ``(cards, had_local_images)``.
    """
    eff_title = str(title or "").strip()
    if not eff_title:
        raise ValueError("title is empty")

    eff_max_bytes, eff_max_elements, eff_max_tables = _resolve_effective_limits(
        max_bytes=max_bytes,
        max_elements=max_elements,
        max_tables=max_tables,
    )
    renderer = load_renderer_module()

    cleaned_md, had_local_images = sanitize_markdown_for_report(markdown)
    if hasattr(renderer, "card_markdown") and cleaned_md:
        cleaned_md = renderer.card_markdown(cleaned_md)

    note_only = False
    if not cleaned_md:
        if had_local_images:
            cleaned_md = CHARTS_IN_DALTON_NOTE
            note_only = True
        else:
            raise ValueError("markdown content is empty")

    def _chunk(limit_bytes: int, limit_elements: int) -> list[dict[str, Any]]:
        return list(
            renderer.markdown_to_card_chunks(
                cleaned_md,
                title=eff_title,
                subtitle=subtitle,
                header_template=header_template,
                tag=tag,
                tag_color=tag_color,
                max_bytes=limit_bytes,
                max_elements=limit_elements,
                max_tables=eff_max_tables,
            )
        )

    cards = _chunk(eff_max_bytes, eff_max_elements)

    if had_local_images and not note_only:
        note_elem = _charts_note_element()
        cards[-1]["body"]["elements"].append(note_elem)
        if not renderer.is_feishu_card_within_envelope(
            cards[-1],
            max_bytes=eff_max_bytes,
            max_elements=eff_max_elements,
            max_tables=eff_max_tables,
        ):
            note_bytes = (
                len(json.dumps(note_elem, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                + 32
            )
            cards = _chunk(
                max(1024, eff_max_bytes - note_bytes),
                max(2, eff_max_elements - 1),
            )
            cards[-1]["body"]["elements"].append(note_elem)

    _validate_cards(
        cards,
        renderer=renderer,
        max_bytes=eff_max_bytes,
        max_elements=eff_max_elements,
        max_tables=eff_max_tables,
    )
    return cards, had_local_images


def convert_markdown_file(
    input_path: Path | str,
    *,
    title: str | None = None,
    subtitle: str = "",
    header_template: str = "indigo",
    tag: str = "",
    tag_color: str = "",
    max_bytes: int | None = None,
    max_elements: int | None = None,
    max_tables: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Read a Markdown file and return ``(cards, had_local_images)``."""
    src = Path(input_path)
    if src.is_symlink() or not src.is_file():
        raise FileNotFoundError(f"markdown file not found: {src}")
    raw_markdown = src.read_text(encoding="utf-8")
    eff_title = (
        str(title).strip()
        if title is not None
        else extract_title_from_markdown(raw_markdown)
    )
    return markdown_to_report_cards(
        raw_markdown,
        title=eff_title,
        subtitle=subtitle,
        header_template=header_template,
        tag=tag,
        tag_color=tag_color,
        max_bytes=max_bytes,
        max_elements=max_elements,
        max_tables=max_tables,
    )


def _atomic_write_text(path: Path, text: str) -> None:
    if path.is_symlink():
        raise ValueError("refusing to write through symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    replaced = False
    try:
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("incomplete write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_name, path)
        replaced = True
    finally:
        if not replaced:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="将 Markdown 文件转为可直接提交给 automation_report 的飞书 Card Schema 2.0 卡片 JSON 数组。"
    )
    parser.add_argument("markdown_file", help="输入 Markdown 文件路径")
    title_group = parser.add_mutually_exclusive_group()
    title_group.add_argument(
        "--title",
        default=None,
        help="卡片标题（省略时默认取文件首个非空行并去掉 # 前缀）",
    )
    title_group.add_argument(
        "--title-from-first-line",
        action="store_true",
        help="显式指定从输入文件首个非空行提取卡片标题（去掉 # 前缀）",
    )
    parser.add_argument("--subtitle", default="", help="可选副标题")
    parser.add_argument("--header-template", default="indigo", help="飞书卡片头色（默认 indigo）")
    parser.add_argument("--tag", default="", help="可选顶部标签文案")
    parser.add_argument("--tag-color", default="", help="可选顶部标签颜色")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help=f"单卡 UTF-8 字节上限（仅允许调低，最大 {FEISHU_HARD_MAX_BYTES}）",
    )
    parser.add_argument(
        "--max-elements",
        type=int,
        default=None,
        help=f"单卡元素上限（仅允许调低，最大 {FEISHU_HARD_MAX_ELEMENTS}）",
    )
    parser.add_argument(
        "--max-tables",
        type=int,
        default=None,
        help=f"单卡表格上限（仅允许调低，最大 {FEISHU_HARD_MAX_TABLES}）",
    )
    parser.add_argument(
        "--out",
        "-o",
        default=None,
        help="输出 JSON 文件路径（省略时直接将卡片数组打印到标准输出）",
    )
    args = parser.parse_args(argv)
    eff_title = None if args.title_from_first_line else args.title

    try:
        cards, had_local_images = convert_markdown_file(
            args.markdown_file,
            title=eff_title,
            subtitle=args.subtitle,
            header_template=args.header_template,
            tag=args.tag,
            tag_color=args.tag_color,
            max_bytes=args.max_bytes,
            max_elements=args.max_elements,
            max_tables=args.max_tables,
        )
    except Exception as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    rendered = json.dumps(cards, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        out_path = Path(args.out)
        try:
            _atomic_write_text(out_path, rendered)
        except Exception as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 2
        summary = {
            "ok": True,
            "output": str(out_path),
            "card_count": len(cards),
            "had_local_images": had_local_images,
        }
        sys.stdout.write(json.dumps(summary, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
