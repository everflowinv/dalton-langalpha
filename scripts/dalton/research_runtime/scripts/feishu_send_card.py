#!/usr/bin/env python3
"""
飞书消息卡片发送脚本

用法:
    python3 feishu_send_card.py --chat-id oc_xxx --title "标题" --content "markdown内容"
    python3 feishu_send_card.py --chat-id oc_xxx --title "标题" --file /path/to/content.md
    python3 feishu_send_card.py --chat-id oc_xxx --card-json '{"type":"template",...}'
    # 发私聊：接收方 id 传 open_id，并显式指定类型
    python3 feishu_send_card.py --chat-id ou_xxx --receive-id-type open_id \
        --title "标题" --subtitle "窗口与口径" --file /path/to/content.md

卡片模板:
    Market Digest 使用预定义模板，自动将 markdown 转成分段卡片
"""

import argparse
import copy
import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Optional, Tuple


CARD_MARKDOWN_MAX_CHARS = 2000
FEISHU_CARD_MAX_BYTES = 30_720
FEISHU_CARD_MAX_ELEMENTS = 200
FEISHU_CARD_TABLE_LIMIT = 5
_GATEWAY_IMG_KEY_PROBE = "img_v3_" + ("x" * 57)

_TABLE_DELIM_CELL_RE = re.compile(r":?-{1,}:?")
_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_BLOCKQUOTE_STEP_RE = re.compile(r"^\s{0,3}> ?")
_LIST_MARKER_PREFIX_RE = re.compile(r"^(\s{0,3}(?:[-*+]|\d{1,9}[.)])\s+)")
_ATX_HEADING_PREFIX_RE = re.compile(r"^#{1,6}(?:\s|$)")
_TABLE_ROW_TRUNCATED_SUFFIX = "（本行过长已截断）"


def _strip_blockquote_prefix(line: str) -> tuple[int, str]:
    """Return ``(quote_depth, inner_line)`` after stripping leading GFM ``>`` markers."""
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
    """Split Markdown into ``("text", str)`` and ``("table", hdr, sep, rows)`` segments."""
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
    """Count GFM Markdown tables in ``text`` outside fenced code blocks."""
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


def feishu_card_within_table_limit(
    card: Mapping[str, Any],
    *,
    max_tables: int = FEISHU_CARD_TABLE_LIMIT,
) -> bool:
    """Return True when ``card`` contains at most ``max_tables`` Markdown tables."""
    return _count_card_tables(card) <= max(1, int(max_tables))


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
    """Truncate one oversized table row so ``header + sep + row`` fits with headers intact."""
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


def _split_table_to_fitting_chunks(
    header_line: str,
    sep_line: str,
    rows: list[str],
    *,
    fits_text: Callable[[str], bool],
) -> list[str]:
    """Split a table across chunks, repeating headers and truncating single oversized rows."""
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
            out.append(sub_tbl)
            continue
        for single_row in sub_rows:
            out.append(
                _truncate_table_row_to_fit(
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


def _replace_card_img_placeholders_for_size(node: Any) -> Any:
    """Clone ``node`` replacing ``__img__`` placeholders with gateway ``img`` shape."""
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
    """Measure the compact UTF-8 byte size of ``card``."""
    return len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def feishu_card_gateway_utf8_bytes(card: Mapping[str, Any]) -> int:
    """Measure ``card`` UTF-8 bytes after conservative gateway ``__img__`` expansion."""
    return feishu_card_serialized_utf8_bytes(_replace_card_img_placeholders_for_size(card))


def is_feishu_card_within_envelope(
    card: Mapping[str, Any],
    *,
    max_bytes: int = FEISHU_CARD_MAX_BYTES,
    max_elements: int = FEISHU_CARD_MAX_ELEMENTS,
    max_tables: int = FEISHU_CARD_TABLE_LIMIT,
) -> bool:
    """Return True when ``card`` fits within gateway byte, element, and table ceilings."""
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
            elif (
                isinstance(element, Mapping)
                and element.get("tag") == "collapsible_panel"
                and isinstance(element.get("elements"), list)
            ):
                total_elements += len(element["elements"])
        if total_elements > max_elements:
            return False
    return feishu_card_within_table_limit(card, max_tables=max_tables)


def _read_legacy_openclaw_feishu_credentials() -> tuple[str, str]:
    """Legacy fallback during transition: read channels.feishu from openclaw.json if present."""
    config_path = os.path.expanduser(
        os.environ.get("DALTON_OPENCLAW_CONFIG", "").strip() or "~/.openclaw/openclaw.json"  # 兼容过渡期回退
    )
    if not os.path.isfile(config_path):
        return "", ""
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        config = json.loads(content)
    except json.JSONDecodeError:
        content = re.sub(r"//.*$", "", content, flags=re.MULTILINE)
        content = re.sub(r",\s*([}\]])", r"\1", content)
        config = json.loads(content)
    feishu = config.get("channels", {}).get("feishu", {})
    app_id = str(feishu.get("appId") or "").strip()
    app_secret = str(feishu.get("appSecret") or "").strip()
    if (not app_id or not app_secret) and isinstance(feishu.get("accounts"), dict):
        default_acc = feishu["accounts"].get("default", {})
        if isinstance(default_acc, dict):
            app_id = app_id or str(default_acc.get("appId") or "").strip()
            app_secret = app_secret or str(default_acc.get("appSecret") or "").strip()
    return app_id, app_secret


def get_tenant_token() -> str:
    """优先从环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET 获取凭证，读不到才回退旧的 openclaw.json。"""
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        legacy_id, legacy_secret = _read_legacy_openclaw_feishu_credentials()
        app_id = app_id or legacy_id
        app_secret = app_secret or legacy_secret

    if not app_id or not app_secret:
        raise ValueError("Missing FEISHU_APP_ID or FEISHU_APP_SECRET in environment (and legacy openclaw.json fallback)")

    # 获取 tenant_access_token
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(url, data, headers={"Content-Type": "application/json"})

    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())

    if result.get("code") != 0:
        raise ValueError(f"Failed to get token: code={result.get('code')} msg={result.get('msg')}")

    return result["tenant_access_token"]


def upload_image(path: str, token: str) -> str:
    """上传本地图片到飞书, 返回 img_key (需要应用有 im:resource 权限)."""
    import uuid

    with open(path, "rb") as f:
        data = f.read()
    fname = os.path.basename(path)
    boundary = uuid.uuid4().hex
    parts = [
        (f'--{boundary}\r\nContent-Disposition: form-data; name="image_type"'
         f'\r\n\r\nmessage\r\n').encode(),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
         f'filename="{fname}"\r\nContent-Type: image/png\r\n\r\n').encode()
        + data + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/images",
        body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    if result.get("code") != 0:
        raise ValueError(f"Image upload failed for {path}: {result}")
    return result["data"]["image_key"]


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


def resolve_image_placeholders(card: dict[str, Any], token: str) -> None:
    """把 __img__ 占位元素上传并替换为飞书 img 元素，递归支持 Card 2.0 及折叠面板."""
    def _resolve_list(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        resolved = []
        for el in elements:
            if el.get("tag") == "__img__":
                img_key = upload_image(el["path"], token)
                resolved.append({
                    "tag": "img",
                    "img_key": img_key,
                    "alt": {"tag": "plain_text", "content": ""},
                    "mode": "fit_horizontal",
                    "preview": True,
                })
            elif el.get("tag") == "collapsible_panel" and "elements" in el:
                el["elements"] = _resolve_list(el["elements"])
                resolved.append(el)
            else:
                resolved.append(el)
        return resolved

    if "body" in card and isinstance(card["body"], dict) and "elements" in card["body"]:
        card["body"]["elements"] = _resolve_list(card["body"]["elements"])
    if "elements" in card and isinstance(card["elements"], list):
        card["elements"] = _resolve_list(card["elements"])


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


def markdown_to_card_elements(markdown: str, title: str, subtitle: str = "",
                              header_template: str = "indigo",
                              tag: str = "", tag_color: str = "") -> dict[str, Any]:
    """
    将 markdown 内容转成飞书 Card Schema 2.0 卡片 JSON
    
    卡片结构:
    - schema: "2.0"
    - config: wide_screen_mode, update_multi
    - header: 标题、副标题（原生呈现于彩条下方）、配色模板、业务标签
    - body: 各章节 markdown 块、折叠面板、说明性脚注（notation）、图片与分隔线
    """
    sections: list[dict[str, Any]] = []
    current_section = {"title": "", "content": []}

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

    lines = markdown.strip().split('\n')
    i = 0
    extracted_subtitle = ""

    while i < len(lines):
        line = lines[i]
        
        # 跳过顶级文档标题 (# 开头)
        if line.startswith('# ') and not line.startswith('## '):
            if not subtitle and i + 1 < len(lines) and lines[i + 1].startswith('> '):
                extracted_subtitle = lines[i + 1][2:].strip()
                i += 2
                continue
            i += 1
            continue

        # 二至四级标题都开始新章节；四级标题用于栏目内的独立子区块
        heading = re.match(r'^#{2,4}\s+(.+?)\s*$', line)
        if heading:
            flush_current_section()
            current_section = {
                "title": heading.group(1),
                "content": []
            }
            i += 1
            continue

        current_section["content"].append(line)
        i += 1

    flush_current_section()

    effective_subtitle = subtitle or extracted_subtitle

    # 构建 Header (Card Schema 2.0)
    header: dict[str, Any] = {
        "title": {
            "tag": "plain_text",
            "content": title
        },
        "template": header_template or "indigo"
    }
    if effective_subtitle:
        header["subtitle"] = {
            "tag": "plain_text",
            "content": effective_subtitle
        }

    # 业务标签
    if tag:
        header["text_tag_list"] = [{
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": tag},
            "color": tag_color or "blue"
        }]
    else:
        inferred = auto_infer_tag(title)
        if inferred:
            header["text_tag_list"] = [{
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": inferred[0]},
                "color": tag_color or inferred[1]
            }]

    # 构建 Body Elements
    elements: list[dict[str, Any]] = []
    titled_sections = [s for s in sections if s["title"]]

    if not titled_sections:
        # 无 ## 章节：全部内容作为普通 markdown 块
        elements.extend(content_to_elements(markdown.strip()))
    else:
        for section in sections:
            sec_title = section["title"].strip()
            if not sec_title:
                continue

            section_text = '\n'.join(section["content"]).strip()

            # 1. 脚注/信源/说明类栏目：使用 notation 次要字号呈现，不抢占主干焦点
            if NOTATION_SECTION_RE.match(sec_title):
                if elements and elements[-1].get("tag") != "hr":
                    elements.append({"tag": "hr"})
                notation_lines = [
                    f"<font color='grey'>{l}</font>" if l.strip() else ""
                    for l in f"**{sec_title}**\n{section_text}".splitlines()
                ]
                elements.append({
                    "tag": "markdown",
                    "text_size": "notation",
                    "content": "\n".join(notation_lines)
                })
                continue

            # 2. 折叠面板栏目：以 (折叠) / (点击展开) 结尾或例行类栏目
            is_collapsible = bool(COLLAPSIBLE_SUFFIX_RE.search(sec_title)) or sec_title.startswith(("例行披露", "长尾"))
            if is_collapsible:
                if elements and elements[-1].get("tag") != "hr":
                    elements.append({"tag": "hr"})
                clean_title = COLLAPSIBLE_SUFFIX_RE.sub("", sec_title).strip()
                inner_elements = content_to_elements(section_text)
                elements.append({
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {
                        "title": {
                            "tag": "markdown",
                            "content": f"**{clean_title}**"
                        },
                        "vertical_align": "center",
                        "padding": "4px 8px 4px 8px",
                        "icon": {
                            "tag": "standard_icon",
                            "token": "down-small-ccm_outlined",
                            "size": "12px 12px"
                        },
                        "icon_position": "right",
                        "icon_expanded_angle": -180
                    },
                    "border": {"color": "grey", "corner_radius": "6px"},
                    "vertical_spacing": "6px",
                    "padding": "8px 8px 8px 8px",
                    "elements": inner_elements
                })
                continue

            # 3. 常规业务章节
            section_content = f"**{sec_title}**\n\n{section_text}"
            elements.extend(content_to_elements(section_content))
            elements.append({"tag": "hr"})

        # 移除末尾多余分隔线
        while elements and elements[-1].get("tag") == "hr":
            elements.pop()

    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "update_multi": True
        },
        "header": header,
        "body": {
            "direction": "vertical",
            "vertical_spacing": "8px",
            "elements": elements
        }
    }


def _upgrade_card_envelope_if_needed(card: Mapping[str, Any]) -> dict[str, Any]:
    card_dict = copy.deepcopy(dict(card))
    if card_dict.get("schema") != "2.0" and "elements" in card_dict:
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": card_dict.get("header", {}),
            "body": {
                "direction": "vertical",
                "vertical_spacing": "8px",
                "elements": list(card_dict.get("elements") or []),
            },
        }
    return card_dict


def split_feishu_card_to_chunks(
    card: Mapping[str, Any],
    *,
    max_bytes: int = FEISHU_CARD_MAX_BYTES,
    max_elements: int = FEISHU_CARD_MAX_ELEMENTS,
    max_tables: int = FEISHU_CARD_TABLE_LIMIT,
) -> list[dict[str, Any]]:
    """Split a pre-built Schema 2.0 (or 1.0) card into gateway-compliant chunks."""
    normalized = _upgrade_card_envelope_if_needed(card)
    if is_feishu_card_within_envelope(
        normalized,
        max_bytes=max_bytes,
        max_elements=max_elements,
        max_tables=max_tables,
    ):
        return [normalized]

    header_base = copy.deepcopy(dict(normalized.get("header") or {}))
    config_base = copy.deepcopy(
        dict(normalized.get("config") or {"wide_screen_mode": True, "update_multi": True})
    )
    body_raw = normalized.get("body") if isinstance(normalized.get("body"), Mapping) else {}
    body_meta = {k: copy.deepcopy(v) for k, v in body_raw.items() if k != "elements"}
    raw_elements = list(body_raw.get("elements") or [])

    def _numbered_header(title_suffix: str) -> dict[str, Any]:
        hdr = copy.deepcopy(header_base)
        title_obj = hdr.get("title")
        if isinstance(title_obj, dict) and isinstance(title_obj.get("content"), str):
            title_obj["content"] = f"{title_obj['content']}{title_suffix}"
        elif title_suffix:
            hdr["title"] = {"tag": "plain_text", "content": title_suffix.strip("（）")}
        return hdr

    probe_header = _numbered_header("（999/999）")

    def _make_card(hdr: dict[str, Any], elems: list[dict[str, Any]]) -> dict[str, Any]:
        cleaned = list(elems)
        while cleaned and isinstance(cleaned[0], Mapping) and cleaned[0].get("tag") == "hr":
            cleaned.pop(0)
        while cleaned and isinstance(cleaned[-1], Mapping) and cleaned[-1].get("tag") == "hr":
            cleaned.pop()
        if not cleaned:
            cleaned = list(elems)
        return {
            "schema": "2.0",
            "config": copy.deepcopy(config_base),
            "header": hdr,
            "body": {**copy.deepcopy(body_meta), "elements": cleaned},
        }

    def _elems_fit(elems: list[dict[str, Any]]) -> bool:
        return is_feishu_card_within_envelope(
            _make_card(probe_header, elems),
            max_bytes=max_bytes,
            max_elements=max_elements,
            max_tables=max_tables,
        )

    def _split_markdown_element(
        el: dict[str, Any],
        *,
        fits_fn: Callable[[list[dict[str, Any]]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        fit_check = fits_fn or _elems_fit
        content = str(el.get("content") or "")
        template = {k: v for k, v in el.items() if k != "content"}
        if fit_check([{**template, "content": content}]):
            return [el]

        segments = _extract_markdown_segments(content)
        units: list[str] = []
        for seg in segments:
            if seg[0] == "table":
                _, hdr, sep, rows = seg
                full_tbl = hdr + "\n" + sep + ("\n" + "\n".join(rows) if rows else "")
                if fit_check([{**template, "content": full_tbl}]):
                    units.append(full_tbl)
                else:
                    units.extend(
                        _split_table_to_fitting_chunks(
                            hdr,
                            sep,
                            rows,
                            fits_text=lambda cand: fit_check([{**template, "content": cand}]),
                        )
                    )
            else:
                para_parts = [p for p in re.split(r"\n{2,}", seg[1].strip()) if p.strip()]
                for part in para_parts or [seg[1]]:
                    if fit_check([{**template, "content": part}]):
                        units.append(part)
                    else:
                        for ln in part.splitlines() or [part]:
                            if fit_check([{**template, "content": ln}]):
                                units.append(ln)
                            else:
                                rem = ln
                                while rem:
                                    lo, hi = 1, len(rem)
                                    best = 1
                                    while lo <= hi:
                                        mid = (lo + hi) // 2
                                        if fit_check([{**template, "content": rem[:mid]}]):
                                            best = mid
                                            lo = mid + 1
                                        else:
                                            hi = mid - 1
                                    units.append(rem[:best])
                                    rem = rem[best:]

        out: list[dict[str, Any]] = []
        buf: list[str] = []
        for u in units:
            cand = "\n\n".join([*buf, u]) if buf else u
            if buf and not fit_check([{**template, "content": cand}]):
                out.append({**copy.deepcopy(template), "content": "\n\n".join(buf)})
                buf = [u]
            else:
                buf.append(u)
        if buf:
            out.append({**copy.deepcopy(template), "content": "\n\n".join(buf)})
        return out

    atomic_elements: list[dict[str, Any]] = []
    for el in raw_elements:
        if not isinstance(el, dict):
            continue
        if _elems_fit([el]):
            atomic_elements.append(el)
            continue
        if el.get("tag") == "markdown" and isinstance(el.get("content"), str):
            atomic_elements.extend(_split_markdown_element(el))
            continue
        if el.get("tag") == "collapsible_panel" and isinstance(el.get("elements"), list):
            def _panel_with(items: list[dict[str, Any]], idx: int, panel_el: dict[str, Any] = el) -> dict[str, Any]:
                panel_copy = copy.deepcopy(panel_el)
                panel_copy["elements"] = items
                if idx > 0:
                    p_title = ((panel_copy.get("header") or {}).get("title") or {})
                    if isinstance(p_title, dict) and isinstance(p_title.get("content"), str):
                        p_title["content"] = f"{p_title['content']}（续）"
                return panel_copy

            def _panel_elems_fit(items: list[dict[str, Any]]) -> bool:
                return _elems_fit([_panel_with(items, 0)]) and _elems_fit([_panel_with(items, 1)])

            inner_atomic: list[dict[str, Any]] = []
            for inner_el in el["elements"]:
                if isinstance(inner_el, dict) and inner_el.get("tag") == "markdown" and isinstance(inner_el.get("content"), str):
                    inner_atomic.extend(_split_markdown_element(inner_el, fits_fn=_panel_elems_fit))
                elif isinstance(inner_el, dict):
                    inner_atomic.append(inner_el)
            panel_buf: list[dict[str, Any]] = []
            panel_idx = 0

            for item in inner_atomic:
                cand_panel = _panel_with([*panel_buf, item], panel_idx)
                if panel_buf and not _elems_fit([cand_panel]):
                    atomic_elements.append(_panel_with(panel_buf, panel_idx))
                    panel_idx += 1
                    panel_buf = [item]
                else:
                    panel_buf.append(item)
            if panel_buf:
                atomic_elements.append(_panel_with(panel_buf, panel_idx))
            continue
        atomic_elements.append(el)

    buckets: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for el in atomic_elements:
        if el.get("tag") == "hr" and not current:
            continue
        cand = [*current, el]
        if current and not _elems_fit(cand):
            buckets.append(current)
            current = [] if el.get("tag") == "hr" else [el]
        else:
            current = cand
    if current:
        buckets.append(current)

    total = len(buckets)
    result_cards = [
        _make_card(_numbered_header(f"（{idx}/{total}）" if total > 1 else ""), bucket)
        for idx, bucket in enumerate(buckets, start=1)
    ]
    for idx, chunk_card in enumerate(result_cards, start=1):
        if not is_feishu_card_within_envelope(
            chunk_card,
            max_bytes=max_bytes,
            max_elements=max_elements,
            max_tables=max_tables,
        ):
            raise ValueError(
                f"Feishu card chunk {idx}/{len(result_cards)} cannot be split within gateway envelope "
                f"({feishu_card_gateway_utf8_bytes(chunk_card)} bytes, max {max_bytes})"
            )
    return result_cards


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
    """Render Markdown into gateway-compliant Feishu Schema 2.0 card chunks."""
    card = markdown_to_card_elements(
        markdown=markdown,
        title=title,
        subtitle=subtitle,
        header_template=header_template,
        tag=tag,
        tag_color=tag_color,
    )
    return split_feishu_card_to_chunks(
        card,
        max_bytes=max_bytes,
        max_elements=max_elements,
        max_tables=max_tables,
    )


RECEIVE_ID_TYPES = ["chat_id", "open_id", "user_id", "union_id", "email"]


def send_card(chat_id: str, card: dict[str, Any], token: str,
              receive_id_type: str = "chat_id") -> dict[str, Any]:
    """发送消息卡片。receive_id_type 决定 receive_id 是群还是个人。"""

    url = ("https://open.feishu.cn/open-apis/im/v1/messages"
           f"?receive_id_type={receive_id_type}")

    payload = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False)
    }
    
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        url, 
        data, 
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}"
        }
    )
    
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _mirror_to_discord(chat_id, receive_id_type, card, label):
    """发到 Eve tracking 群成功后同步一份到 Discord；导入失败如实返回 import_error。"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from eve_tracking_mirror import mirror_after_feishu
    except Exception as exc:  # noqa: BLE001
        print(f"[eve-tracking-mirror] 未加载，Discord 未同步（飞书已发送成功，不要重发飞书）：{exc}",
              file=sys.stderr)
        return {"status": "import_error", "error": f"{type(exc).__name__}: {exc}"}
    return mirror_after_feishu(chat_id, receive_id_type, card, label)


def main():
    parser = argparse.ArgumentParser(description="发送飞书消息卡片 (Card Schema 2.0)")
    parser.add_argument("--chat-id", required=False, default="",
                        help="接收方 id：群 chat_id（默认），或按 --receive-id-type 传 open_id 等")
    parser.add_argument("--receive-id-type", default="chat_id", choices=RECEIVE_ID_TYPES,
                        help="接收方 id 类型，默认 chat_id；发私聊传 open_id")
    parser.add_argument("--title", help="卡片标题")
    parser.add_argument("--subtitle", help="标题下的小字备注，放窗口与口径这类元信息")
    parser.add_argument("--tag", help="卡片右上角业务标签文案（如：市场晨报、周度 Tracking）")
    parser.add_argument("--tag-color", default="", help="标签颜色（blue/purple/orange/indigo/wathet/green/grey等）")
    parser.add_argument("--header-template", default="indigo",
                        help="卡片标题栏配色：indigo(默认)/blue/turquoise/green/orange/red/carmine/violet/purple/grey 等")
    parser.add_argument("--content", help="Markdown 内容（直接传入）")
    parser.add_argument("--file", help="Markdown 内容文件路径")
    parser.add_argument("--card-json", help="完整的卡片 JSON（跳过模板转换）")
    parser.add_argument("--card-json-file", help="完整卡片 JSON 的文件路径（跳过模板转换；大卡片别塞进命令行参数）")
    parser.add_argument("--dry-run", action="store_true", help="只打印卡片 JSON，不执行发送网络请求")
    parser.add_argument("--json", action="store_true", help="JSON 输出模式")
    
    args = parser.parse_args()
    
    try:
        if args.card_json or args.card_json_file:
            # 直接使用传入的卡片 JSON
            if args.card_json_file:
                with open(args.card_json_file, "r", encoding="utf-8") as f:
                    card = json.load(f)
            else:
                card = json.loads(args.card_json)
            # 兼容：如果传入的是 1.0 卡片（顶层含 elements 且无 schema: 2.0），自动平滑升级为 2.0
            if card.get("schema") != "2.0" and "elements" in card:
                card = {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True, "update_multi": True},
                    "header": card.get("header", {}),
                    "body": {
                        "direction": "vertical",
                        "vertical_spacing": "8px",
                        "elements": card.get("elements", [])
                    }
                }
            cards = split_feishu_card_to_chunks(card)
        else:
            # 从 markdown 构建卡片
            if args.file:
                with open(args.file, 'r', encoding='utf-8') as f:
                    content = f.read()
            elif args.content:
                content = args.content
            else:
                raise ValueError("Must provide --content, --file, or --card-json")
            
            title = args.title or "Market Digest"
            cards = markdown_to_card_chunks(
                markdown=content,
                title=title,
                subtitle=args.subtitle or "",
                header_template=args.header_template,
                tag=args.tag or "",
                tag_color=args.tag_color or ""
            )
        
        # 如果是 dry-run 模式，直接打印卡片 JSON 并退出，不请求网络
        if args.dry_run:
            payload = cards[0] if len(cards) == 1 else cards
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        if not args.chat_id:
            raise ValueError("--chat-id is required when not running in --dry-run mode")

        token = get_tenant_token()
        results: list[dict[str, Any]] = []
        mirrors: list[Any] = []
        for card_chunk in cards:
            mirror_source = json.loads(json.dumps(card_chunk))  # 图片占位上传后只剩 img_key，同步要在这之前取
            resolve_image_placeholders(card_chunk, token)
            res = send_card(args.chat_id, card_chunk, token, args.receive_id_type)
            results.append(res)
            if res.get("code") == 0:
                m = _mirror_to_discord(args.chat_id, args.receive_id_type, mirror_source, args.title or "")
                if m is not None:
                    mirrors.append(m)
            else:
                break
        result = results[-1]
        all_ok = len(results) == len(cards) and all(r.get("code") == 0 for r in results)
        mirror = mirrors[-1] if mirrors else None
        
        import_failed = any(isinstance(m, dict) and m.get("status") == "import_error" for m in mirrors)
        if args.json:
            output = {
                "status": "error" if (not all_ok or import_failed) else "ok",
                "feishu_sent": all_ok,
                "message_id": results[0].get("data", {}).get("message_id") if results else None,
                "message_ids": [r.get("data", {}).get("message_id") for r in results if r.get("code") == 0],
                "cards_sent": len(results),
                "result": result,
            }
            if import_failed:
                output["error"] = "eve_tracking_mirror_import_failed"
            if mirror is not None:
                output["discord_mirror"] = {k: mirror.get(k) for k in ("status", "messages_total", "error")}
            print(json.dumps(output, ensure_ascii=False, indent=2))
            if not all_ok or import_failed:
                sys.exit(1)
        else:
            if all_ok:
                print(f"✅ 卡片发送成功: {results[0].get('data', {}).get('message_id')}")
                if import_failed:
                    print("❌ eve_tracking_mirror 导入失败，Discord 未同步", file=sys.stderr)
                    sys.exit(1)
            else:
                print(f"❌ 发送失败: {result}")
                sys.exit(1)
    
    except Exception as e:
        if args.json:
            print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        else:
            print(f"❌ 错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
