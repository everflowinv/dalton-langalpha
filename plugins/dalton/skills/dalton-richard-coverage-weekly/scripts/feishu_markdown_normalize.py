"""Pure Markdown inline-code normalizer for Feishu cards and docs.

Removes decorative single-backtick inline code around financial figures,
stock tickers, and reporting periods in prose while leaving Markdown code
blocks (both fenced and 4-space/tab indented), HTML blocks, double-backtick
spans, escaped backticks, and technical inline code untouched.

Mirrors ``normalizeFeishuMarkdownInlineCode`` in
``integrations/dalton-channel-gateway/src/channels/feishu/card-normalize.ts``
and is mirrored identically in
``plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/feishu_markdown_normalize.py``
so sandbox skill scripts share the exact same normalization logic.
"""

from __future__ import annotations

import re
from typing import Any

_QUANTITY_CURR_PREFIX = (
    r"(?:US\$|HK\$|CN\$|NT\$|A\$|C\$|S\$|[$€£¥￥]|"
    r"(?:USD|HKD|CNY|RMB|EUR|GBP|JPY|TWD|KRW) )"
)
_QUANTITY_PREFIX = (
    rf"(?:[+\-−]?{_QUANTITY_CURR_PREFIX}|{_QUANTITY_CURR_PREFIX}[+\-−]|[+\-−])?"
)
_QUANTITY_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_QUANTITY_SUFFIX = (
    r"(?:%|％| ?(?:bps|bp|ppts|ppt|pp)|[xX]|"
    r"(?:[KkMmBbTt]|[mM][nN]|[bB][nN]|[tT][nN])"
    r"(?: ?(?:USD|HKD|CNY|RMB|EUR|GBP|JPY|TWD|KRW))?|"
    r" ?(?:USD|HKD|CNY|RMB|EUR|GBP|JPY|TWD|KRW)|"
    r" ?(?:千|万|百万|千万|亿|十亿|百亿|千亿|万亿)?"
    r"(?:美元|港元|港币|人民币|欧元|日元|英镑|美金|元|倍|个百分点|百分点)|"
    r" ?(?:千|万|百万|千万|亿|十亿|百亿|千亿|万亿))?"
)
_QUANTITY_ATOM = rf"{_QUANTITY_PREFIX}{_QUANTITY_NUM}{_QUANTITY_SUFFIX}"
_RANGE_SEP = r"(?: ?(?:[-–—~～]|至|到) ?| to )"

_INLINE_CODE_QUANTITY_RE = re.compile(
    rf"\A{_QUANTITY_ATOM}(?:{_RANGE_SEP}{_QUANTITY_ATOM})?\Z"
)
_INLINE_CODE_TICKER_RE = re.compile(
    r"\A(?:[A-Z]{1,6}|[A-Z]{1,5}\.[A-Z]|"
    r"\d{3,6}\.(?:HK|SH|SS|SZ|BJ|TW|TWO|KS|KQ|T|JP|SI|KL|BK|AX|NS|BO|US)|"
    r"[A-Z][A-Z0-9]{0,5}\."
    r"(?:HK|US|N|O|OQ|L|PA|DE|AS|SW|MI|MC|ST|CO|HE|OL|VI|BR|LS|TO|AX|NZ|SI|"
    r"NS|BO|KL|BK|JK|SA|MX|KS|KQ|TW|TWO|T|JP))\Z"
)
_PERIOD_ATOM = (
    r"(?:(?:19|20)\d{2} ?(?:Q[1-4]|H[12]|M(?:0?[1-9]|1[0-2])|FY|CY|YTD)[EAPF]?|"
    r"[1-4]Q(?:\d{2}|(?:19|20)\d{2})[EAPF]?|"
    r"[12]H(?:\d{2}|(?:19|20)\d{2})[EAPF]?|"
    r"(?:FY|CY)(?: ?(?:'?\d{2}|(?:19|20)\d{2})(?:Q[1-4]|H[12])?)[EAPF]?|"
    r"Q[1-4](?: ?(?:'?\d{2}|(?:19|20)\d{2}))?[EAPF]?|"
    r"H[12](?: ?(?:'?\d{2}|(?:19|20)\d{2}))?[EAPF]?|"
    r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))"
)
_INLINE_CODE_PERIOD_RE = re.compile(
    rf"\A{_PERIOD_ATOM}(?:{_RANGE_SEP}{_PERIOD_ATOM})?\Z"
)

_LINE_SPLIT_RE = re.compile(r"(\r\n|\n|\r)")
_FENCE_OPEN_RE = re.compile(r"\A[ ]{0,3}(`{3,}|~{3,})(.*)\Z")
_FENCE_CLOSE_BACKTICK_RE = re.compile(r"\A[ ]{0,3}(`{3,})[ \t]*\Z")
_FENCE_CLOSE_TILDE_RE = re.compile(r"\A[ ]{0,3}(~{3,})[ \t]*\Z")
_ATX_HEADING_RE = re.compile(r"\A[ ]{0,3}#{1,6}(?:[ \t]|\Z)")
_THEMATIC_BREAK_RE = re.compile(
    r"\A[ ]{0,3}(?:(?:-[ \t]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})\Z"
)
_BLOCKQUOTE_PREFIX_RE = re.compile(r"\A[ ]{0,3}>[ ]?")
_LIST_MARKER_RE = re.compile(r"\A([ ]{0,3})([-+*]|\d{1,9}[.)])([ \t]+|\Z)")
_RANGE_TILDE_RE = re.compile(r" ?[~～] ?")

_MD_PARSER: Any = None
try:
    from markdown_it import MarkdownIt as _MarkdownIt

    _MD_PARSER = _MarkdownIt("commonmark").enable("table")
except ImportError:
    _MD_PARSER = None


def _should_strip_inline_code_backticks(inner: str) -> bool:
    if not inner or inner != inner.strip() or "$" in inner:
        return False
    return bool(
        _INLINE_CODE_QUANTITY_RE.match(inner)
        or _INLINE_CODE_TICKER_RE.match(inner)
        or _INLINE_CODE_PERIOD_RE.match(inner)
    )


def _strip_inline_code_on_line(line: str) -> str:
    if "`" not in line:
        return line
    out: list[str] = []
    i = 0
    length = len(line)
    while i < length:
        tick_pos = line.find("`", i)
        if tick_pos == -1:
            out.append(line[i:])
            break
        out.append(line[i:tick_pos])

        run_end = tick_pos
        while run_end < length and line[run_end] == "`":
            run_end += 1
        run_len = run_end - tick_pos

        backslashes = 0
        bs_pos = tick_pos - 1
        while bs_pos >= 0 and line[bs_pos] == "\\":
            backslashes += 1
            bs_pos -= 1
        if backslashes % 2 == 1:
            out.append(line[tick_pos:run_end])
            i = run_end
            continue

        search_pos = run_end
        close_start = -1
        while search_pos < length:
            next_tick = line.find("`", search_pos)
            if next_tick == -1:
                break
            next_end = next_tick
            while next_end < length and line[next_end] == "`":
                next_end += 1
            if next_end - next_tick == run_len:
                close_start = next_tick
                break
            search_pos = next_end

        if close_start == -1:
            out.append(line[tick_pos:run_end])
            i = run_end
            continue

        close_end = close_start + run_len
        inner = line[run_end:close_start]
        if run_len == 1 and _should_strip_inline_code_backticks(inner):
            out.append(_RANGE_TILDE_RE.sub("至", inner))
        else:
            out.append(line[tick_pos:close_end])
        i = close_end
    return "".join(out)


def _leading_columns(line: str) -> int:
    col = 0
    for ch in line:
        if ch == " ":
            col += 1
        elif ch == "\t":
            col = (col // 4 + 1) * 4
        else:
            break
    return col


def _strip_blockquotes(line: str) -> str:
    cur = line
    while True:
        match = _BLOCKQUOTE_PREFIX_RE.match(cur)
        if not match:
            return cur
        cur = cur[match.end() :]


def _normalize_with_markdown_it(content: str, parser: Any) -> str:
    tokens = parser.parse(content)
    inline_lines: set[int] = set()
    for tok in tokens:
        if tok.type != "inline" or not tok.map or not tok.children:
            continue
        if any(
            child.type == "code_inline" and child.markup == "`"
            for child in tok.children
        ):
            start_line, end_line = tok.map
            for line_idx in range(start_line, end_line):
                inline_lines.add(line_idx)
    if not inline_lines:
        return content

    parts = _LINE_SPLIT_RE.split(content)
    num_lines = (len(parts) + 1) // 2
    for line_idx in inline_lines:
        if 0 <= line_idx < num_lines:
            part_idx = line_idx * 2
            parts[part_idx] = _strip_inline_code_on_line(parts[part_idx])
    return "".join(parts)


def _normalize_stdlib(content: str) -> str:
    """CommonMark-aware fallback when ``markdown_it`` is unavailable (e.g. minimal sandbox)."""
    parts = _LINE_SPLIT_RE.split(content)
    in_fence: tuple[str, int] | None = None
    prev_is_paragraph = False
    list_content_indent: int | None = None

    for idx in range(0, len(parts), 2):
        line = parts[idx]
        inner = _strip_blockquotes(line)

        if in_fence is not None:
            fence_char, fence_len = in_fence
            close_re = (
                _FENCE_CLOSE_BACKTICK_RE
                if fence_char == "`"
                else _FENCE_CLOSE_TILDE_RE
            )
            trimmed_for_close = (
                inner[list_content_indent:]
                if list_content_indent is not None
                and _leading_columns(inner) >= list_content_indent
                and inner.startswith(" " * list_content_indent)
                else inner
            )
            close_match = close_re.match(trimmed_for_close)
            if close_match and len(close_match.group(1)) >= fence_len:
                in_fence = None
                prev_is_paragraph = False
            continue

        if not inner.strip():
            prev_is_paragraph = False
            continue

        cols = _leading_columns(inner)
        if list_content_indent is not None and cols < list_content_indent:
            list_content_indent = None

        base_indent = (
            list_content_indent
            if (list_content_indent is not None and not prev_is_paragraph)
            else 0
        )

        # Indented code block (>= 4 columns beyond container base, cannot interrupt a paragraph).
        if not prev_is_paragraph and (cols - base_indent) >= 4:
            continue

        candidate = (
            inner[base_indent:]
            if base_indent > 0 and inner.startswith(" " * base_indent)
            else inner
        )
        open_match = _FENCE_OPEN_RE.match(candidate)
        if open_match:
            marker = open_match.group(1)
            info = open_match.group(2)
            fence_char = marker[0]
            if fence_char == "~" or "`" not in info:
                in_fence = (fence_char, len(marker))
                prev_is_paragraph = False
                continue

        if _THEMATIC_BREAK_RE.match(candidate):
            prev_is_paragraph = False
            list_content_indent = None
            continue

        if _ATX_HEADING_RE.match(candidate):
            parts[idx] = _strip_inline_code_on_line(line)
            prev_is_paragraph = False
            list_content_indent = None
            continue

        list_match = _LIST_MARKER_RE.match(candidate)
        if list_match:
            prefix_cols = len(list_match.group(1)) + len(list_match.group(2))
            ws_after = list_match.group(3)
            after_col = prefix_cols
            for ch in ws_after:
                if ch == " ":
                    after_col += 1
                elif ch == "\t":
                    after_col = (after_col // 4 + 1) * 4
            gap = after_col - prefix_cols
            rest = candidate[list_match.end() :]
            if rest and gap >= 5:
                # List item whose first line starts an indented code block (`-     code`).
                list_content_indent = base_indent + prefix_cols + 1
                prev_is_paragraph = False
                continue
            list_content_indent = base_indent + (after_col if rest else prefix_cols + 1)
            prev_is_paragraph = bool(rest)
            parts[idx] = _strip_inline_code_on_line(line)
            continue

        prev_is_paragraph = True
        parts[idx] = _strip_inline_code_on_line(line)

    return "".join(parts)


def normalize_feishu_markdown_inline_code(content: str) -> str:
    """Remove decorative single-backtick inline code around financial figures,
    stock tickers, and reporting periods in Feishu card/doc markdown content.

    Markdown code blocks (both fenced and 4-space/tab indented), HTML blocks,
    multi-backtick code spans, escaped backticks, and technical inline code
    (paths, commands, snake_case identifiers, etc.) remain untouched.
    """
    if "`" not in content:
        return content
    if _MD_PARSER is not None:
        return _normalize_with_markdown_it(content, _MD_PARSER)
    return _normalize_stdlib(content)


__all__ = ["normalize_feishu_markdown_inline_code"]
