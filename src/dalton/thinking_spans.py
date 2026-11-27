"""Pure opening thinking-tag scanner shared by cold replay projection and strict share redaction.

Registered in ``src/server/services/history/replay/keys.py`` so changes to this
scanner automatically invalidate stored ``turn_slices`` rows. Imports stdlib only.
"""

from __future__ import annotations

import re
from typing import NamedTuple

_OPEN_THINK_TAG_RE = re.compile(
    r"<\s*(thinking_mode|thinking)\s*>",
    re.IGNORECASE,
)
_THINK_TAG_TOKEN_RE = re.compile(
    r"<\s*(/)?\s*(thinking_mode|thinking)\s*>",
    re.IGNORECASE,
)
_BLANK_LINE_RE = re.compile(r"\n[ \t\r]*\n")


class OpeningThinkingScan(NamedTuple):
    """Result of scanning opening ``<thinking>`` / ``<thinking_mode>`` blocks.

    - ``strip_spans``: character ranges ``(start, end)`` to drop when all opening
      thinking blocks closed cleanly.
    - ``unclosed``: True when an opening thinking block at the start of the
      message never found a matching closing tag on the top of the stack.
    """

    strip_spans: tuple[tuple[int, int], ...]
    unclosed: bool


class ThinkingSpansScan(NamedTuple):
    """Result of scanning all ``<thinking>`` / ``<thinking_mode>`` spans in a full message.

    - ``strip_spans``: character ranges ``(start, end)`` of every closed
      outermost thinking block outside Markdown code fences and inline code spans.
    - ``unclosed_start``: start index of an unclosed outermost opening thinking
      tag (if any), else ``None``.
    """

    strip_spans: tuple[tuple[int, int], ...]
    unclosed_start: int | None


def _strip_blockquote_prefix(line: str) -> tuple[int, str]:
    """Strip leading CommonMark blockquote markers ``>`` from ``line``.

    Returns ``(quote_depth, remainder)``. Each blockquote marker allows up to 3
    leading spaces, a ``>``, and an optional single space after ``>``.
    """
    pos = 0
    n = len(line)
    depth = 0
    while pos < n:
        sp = 0
        while pos + sp < n and line[pos + sp] == " " and sp < 3:
            sp += 1
        if pos + sp < n and line[pos + sp] == ">":
            pos += sp + 1
            depth += 1
            if pos < n and line[pos] == " ":
                pos += 1
        else:
            break
    return depth, line[pos:]


def _skip_fenced_code_block(text: str, start: int) -> int | None:
    """If ``text[start:]`` begins a Markdown fenced code block (including inside ``>`` blockquotes), return index after it."""
    n = len(text)
    line_end = text.find("\n", start)
    if line_end == -1:
        line_end = n
    first_line = text[start:line_end]
    q_depth, rem = _strip_blockquote_prefix(first_line)

    j = 0
    rem_len = len(rem)
    spaces = 0
    while j < rem_len and rem[j] == " " and spaces < 3:
        j += 1
        spaces += 1
    if j >= rem_len or rem[j] not in ("`", "~"):
        return None
    ch = rem[j]
    k = j
    while k < rem_len and rem[k] == ch:
        k += 1
    fence_len = k - j
    if fence_len < 3:
        return None
    info = rem[k:]
    # CommonMark: info string of a backtick code fence cannot contain backticks.
    if ch == "`" and "`" in info:
        return None

    scan = line_end + 1 if line_end < n else n
    while scan < n:
        next_nl = text.find("\n", scan)
        if next_nl == -1:
            next_nl = n
        line_str = text[scan:next_nl]
        if q_depth > 0:
            sub_depth, sub_rem = _strip_blockquote_prefix(line_str)
            if sub_depth < q_depth:
                # Blockquote ended before a closing fence; do not consume this line.
                return scan
            if sub_depth == q_depth:
                stripped = sub_rem.lstrip(" ")
                if (
                    len(sub_rem) - len(stripped) <= 3
                    and stripped.startswith(ch * fence_len)
                    and stripped.lstrip(ch).strip() == ""
                ):
                    return next_nl + 1 if next_nl < n else n
        else:
            stripped = line_str.lstrip(" ")
            if (
                len(line_str) - len(stripped) <= 3
                and stripped.startswith(ch * fence_len)
                and stripped.lstrip(ch).strip() == ""
            ):
                return next_nl + 1 if next_nl < n else n
        scan = next_nl + 1 if next_nl < n else n
    return n


def _skip_inline_code_span(text: str, start: int) -> tuple[int, bool]:
    """Scan a backtick run starting at ``start``.

    Returns ``(next_index, matched_code_span)``. Inline code spans do not cross
    paragraph blank lines. When no matching closing run of exact length exists,
    advances past the opening backtick run so inner backticks are not re-matched.
    """
    n = len(text)
    k = start
    while k < n and text[k] == "`":
        k += 1
    tick_len = k - start

    # Limit search to current paragraph (CommonMark code spans cannot span blank lines).
    blank = _BLANK_LINE_RE.search(text, k)
    limit = blank.start() if blank is not None else n

    search = k
    while search < limit:
        nxt = text.find("`", search, limit)
        if nxt == -1:
            break
        end_t = nxt
        while end_t < limit and text[end_t] == "`":
            end_t += 1
        if end_t - nxt == tick_len:
            return end_t, True
        search = end_t
    return k, False


def scan_opening_thinking_spans(text: str) -> OpeningThinkingScan:
    """Scan opening ``<thinking>`` / ``<thinking_mode>`` blocks at the start of ``text``.

    Rules:
    - Only inspects thinking tags at the start of the message (after optional
      leading whitespace, including consecutive opening thinking blocks).
    - Ignores tags inside Markdown inline code spans (backticks) and fenced
      code blocks.
    - A closing tag pops ONLY when its name matches the top of the tag stack;
      mismatched closing tags are treated as ordinary thinking body text.
    - Returns ``OpeningThinkingScan(strip_spans=(), unclosed=True)`` when any
      opening block lacks a matching closing tag.
    """
    if not text:
        return OpeningThinkingScan(strip_spans=(), unclosed=False)

    n = len(text)
    pos = 0
    while pos < n and text[pos].isspace():
        pos += 1
    if pos >= n or _OPEN_THINK_TAG_RE.match(text, pos) is None:
        return OpeningThinkingScan(strip_spans=(), unclosed=False)

    cur = pos
    stripped_any = False
    while cur < n:
        ws_end = cur
        while ws_end < n and text[ws_end].isspace():
            ws_end += 1
        if ws_end >= n or _OPEN_THINK_TAG_RE.match(text, ws_end) is None:
            break

        stack: list[str] = []
        matched_end: int | None = None
        i = ws_end
        at_line_start = i == 0 or text[i - 1] == "\n"

        while i < n:
            if stack and at_line_start:
                fence_end = _skip_fenced_code_block(text, i)
                if fence_end is not None:
                    i = fence_end
                    at_line_start = True
                    continue

            ch = text[i]
            if stack and ch == "`":
                next_i, _matched = _skip_inline_code_span(text, i)
                at_line_start = next_i > 0 and text[next_i - 1] == "\n"
                i = next_i
                continue

            if ch == "<":
                m = _THINK_TAG_TOKEN_RE.match(text, i)
                if m is not None:
                    is_close = bool(m.group(1))
                    tag_name = m.group(2).lower()
                    if not is_close:
                        stack.append(tag_name)
                    else:
                        if stack and stack[-1] == tag_name:
                            stack.pop()
                    i = m.end()
                    at_line_start = False
                    if not stack:
                        matched_end = i
                        break
                    continue

            at_line_start = ch == "\n"
            i += 1

        if matched_end is None:
            return OpeningThinkingScan(strip_spans=(), unclosed=True)

        stripped_any = True
        cur = matched_end

    if not stripped_any:
        return OpeningThinkingScan(strip_spans=(), unclosed=False)
    return OpeningThinkingScan(strip_spans=((0, cur),), unclosed=False)


def scan_all_thinking_spans(text: str) -> ThinkingSpansScan:
    """Scan all ``<thinking>`` / ``<thinking_mode>`` spans anywhere in ``text``.

    Rules:
    - Skips Markdown fenced code blocks (including inside ``>`` blockquotes) and
      inline backtick code spans both outside and inside thinking blocks so
      literal tags inside code never falsely open or close a thinking block.
    - A closing tag pops ONLY when its name matches the top of the tag stack;
      mismatched closing tags inside an open thinking block are ignored.
    - Returns ``ThinkingSpansScan(strip_spans=..., unclosed_start=...)`` where
      ``strip_spans`` lists every closed outermost thinking span ``(start, end)``
      and ``unclosed_start`` is the start offset of a trailing unclosed thinking
      block (or ``None``).
    """
    if not text or "<" not in text:
        return ThinkingSpansScan(strip_spans=(), unclosed_start=None)

    n = len(text)
    i = 0
    at_line_start = True
    stack: list[str] = []
    span_start: int | None = None
    spans: list[tuple[int, int]] = []

    while i < n:
        if at_line_start:
            fence_end = _skip_fenced_code_block(text, i)
            if fence_end is not None:
                i = fence_end
                at_line_start = True
                continue

        ch = text[i]
        if ch == "`":
            next_i, _matched = _skip_inline_code_span(text, i)
            at_line_start = next_i > 0 and text[next_i - 1] == "\n"
            i = next_i
            continue

        if ch == "<":
            m = _THINK_TAG_TOKEN_RE.match(text, i)
            if m is not None:
                is_close = bool(m.group(1))
                tag_name = m.group(2).lower()
                if not is_close:
                    if not stack:
                        span_start = i
                    stack.append(tag_name)
                else:
                    if stack and stack[-1] == tag_name:
                        stack.pop()
                        if not stack and span_start is not None:
                            spans.append((span_start, m.end()))
                            span_start = None
                i = m.end()
                at_line_start = False
                continue

        at_line_start = ch == "\n"
        i += 1

    unclosed_start = span_start if stack and span_start is not None else None
    return ThinkingSpansScan(strip_spans=tuple(spans), unclosed_start=unclosed_start)
