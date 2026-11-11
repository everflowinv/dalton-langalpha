#!/usr/bin/env python3
"""Build one MSFT tracker card file. Stdlib only. No network and no credentials."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple


CARD_MARKDOWN_MAX_CHARS = 2000
CARD_BODY_MAX_CHARS = 3000
TICKER = "MSFT"
SUBTITLE = "Microsoft · 多空观点、盈利预测修正与催化剂"
HEADER_TEMPLATE = "indigo"
TAG_COLOR = "indigo"

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


def split_markdown_blocks(text: str, max_chars: int = CARD_MARKDOWN_MAX_CHARS) -> list[str]:
    """Split markdown without dropping content, preferring paragraph boundaries."""
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


def dump_card(card: dict[str, Any]) -> str:
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


def render_card(workspace: Path | str, run_id: str, file_rel: str, output_rel: str) -> dict[str, Any]:
    root = open_workspace(str(workspace))
    stamp = parse_run_stamp(run_id)
    source = ensure_relative(root, file_rel)
    output = ensure_relative(root, output_rel)
    packet = ensure_relative(root, f"research/tracker/{run_id}/packet.json")
    if not packet.is_file() or packet.is_symlink():
        raise RenderError("packet_missing", "运行目录没有 packet.json")
    if source.is_symlink() or not source.is_file():
        raise RenderError("card_missing", "找不到卡片正文")
    prepared, glossed = prepare_body(source.read_text(encoding="utf-8"), root / "research" / "tracker" / "thesis.md")
    title = f"📈 {TICKER} 跟踪｜{stamp:%m-%d %H:%M} ET"
    card = markdown_to_card_elements(
        prepared,
        title=title,
        subtitle=SUBTITLE,
        header_template=HEADER_TEMPLATE,
        tag=TICKER,
        tag_color=TAG_COLOR,
    )
    if _forbidden_payload(card):
        raise RenderError("unsupported_content", "卡片不接受图片或表格")
    text = dump_card(card)
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
    parser = argparse.ArgumentParser(description="构造一张 MSFT 跟踪卡片")
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = render_card(args.workspace_dir, args.run_id, args.file, args.output)
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
