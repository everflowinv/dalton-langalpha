"""Pure Schema 2.0 Feishu card normalizer.

Mirrors ``integrations/dalton-channel-gateway/src/channels/feishu/card-normalize.ts``
so cards submitted via ``automation_report`` are normalized identically before
storage and callers receive normalized field paths as warnings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import re
from typing import Any

_TEXT_ELEMENT_TAGS = frozenset({"text", "plain_text", "lark_md"})
_NESTED_ELEMENTS_CONTAINER_TAGS = frozenset(
    {"column", "collapsible_panel", "form", "interactive_container"}
)

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

_LINE_SPLIT_RE = re.compile(r"(\r?\n)")
_FENCE_OPEN_RE = re.compile(r"\A[ ]{0,3}(`{3,}|~{3,})(.*)\Z")
_FENCE_CLOSE_BACKTICK_RE = re.compile(r"\A[ ]{0,3}(`{3,})[ \t]*\Z")
_FENCE_CLOSE_TILDE_RE = re.compile(r"\A[ ]{0,3}(~{3,})[ \t]*\Z")
_RANGE_TILDE_RE = re.compile(r" ?[~～] ?")


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


def normalize_feishu_markdown_inline_code(content: str) -> str:
    """Remove decorative single-backtick inline code around financial figures,
    stock tickers, and reporting periods in Feishu card markdown content.

    Fenced code blocks and technical inline code (paths, commands, snake_case
    identifiers, etc.) remain untouched. Mirrors the gateway TypeScript helper.
    """
    if "`" not in content:
        return content
    parts = _LINE_SPLIT_RE.split(content)
    in_fence: tuple[str, int] | None = None
    for idx in range(0, len(parts), 2):
        line = parts[idx]
        if in_fence is not None:
            fence_char, fence_len = in_fence
            close_re = (
                _FENCE_CLOSE_BACKTICK_RE
                if fence_char == "`"
                else _FENCE_CLOSE_TILDE_RE
            )
            close_match = close_re.match(line)
            if close_match and len(close_match.group(1)) >= fence_len:
                in_fence = None
            continue
        open_match = _FENCE_OPEN_RE.match(line)
        if open_match:
            marker = open_match.group(1)
            info = open_match.group(2)
            fence_char = marker[0]
            if fence_char == "~" or "`" not in info:
                in_fence = (fence_char, len(marker))
                continue
        parts[idx] = _strip_inline_code_on_line(line)
    return "".join(parts)


def _read_tag_lower(node: Mapping[str, Any]) -> str:
    raw = node.get("tag")
    return raw.strip().lower() if isinstance(raw, str) else ""


def _extract_text_element_content(element: Mapping[str, Any]) -> str:
    content = element.get("content")
    if isinstance(content, str):
        return content
    text = element.get("text")
    if isinstance(text, Mapping) and isinstance(text.get("content"), str):
        return text["content"]
    if isinstance(text, str):
        return text
    return ""


def _normalize_header(
    header: Mapping[str, Any],
    normalized_paths: list[str],
) -> dict[str, Any]:
    out: dict[str, Any] = deepcopy(dict(header))

    if isinstance(out.get("title"), str):
        out["title"] = {"tag": "plain_text", "content": out["title"]}
        normalized_paths.append("header.title")

    if isinstance(out.get("subtitle"), str):
        out["subtitle"] = {"tag": "plain_text", "content": out["subtitle"]}
        normalized_paths.append("header.subtitle")

    text_tag_list = out.get("text_tag_list")
    if isinstance(text_tag_list, list):
        rebuilt: list[Any] = []
        for idx, entry in enumerate(text_tag_list):
            if isinstance(entry, str):
                normalized_paths.append(f"header.text_tag_list[{idx}]")
                rebuilt.append(
                    {
                        "tag": "text_tag",
                        "text": {"tag": "plain_text", "content": entry},
                    }
                )
                continue
            if not isinstance(entry, Mapping):
                rebuilt.append(entry)
                continue
            next_entry = dict(entry)
            raw_tag = next_entry.get("tag")
            if not isinstance(raw_tag, str) or not raw_tag.strip():
                next_entry["tag"] = "text_tag"
                normalized_paths.append(f"header.text_tag_list[{idx}].tag")
            if isinstance(next_entry.get("text"), str):
                next_entry["text"] = {
                    "tag": "plain_text",
                    "content": next_entry["text"],
                }
                normalized_paths.append(f"header.text_tag_list[{idx}].text")
            rebuilt.append(next_entry)
        out["text_tag_list"] = rebuilt

    return out


def _normalize_element_list(
    elements: Sequence[Any],
    path_prefix: str,
    normalized_paths: list[str],
    *,
    strip_inline_code: bool = False,
) -> list[Any]:
    rebuilt: list[Any] = []
    for idx, raw_el in enumerate(elements):
        el_path = f"{path_prefix}[{idx}]"
        if not isinstance(raw_el, Mapping):
            rebuilt.append(deepcopy(raw_el))
            continue

        tag_lower = _read_tag_lower(raw_el)
        if tag_lower in _TEXT_ELEMENT_TAGS:
            normalized_paths.append(el_path)
            raw_content = _extract_text_element_content(raw_el)
            rebuilt.append(
                {
                    "tag": "markdown",
                    "content": (
                        normalize_feishu_markdown_inline_code(raw_content)
                        if strip_inline_code
                        else raw_content
                    ),
                }
            )
            continue

        cloned: dict[str, Any] = deepcopy(dict(raw_el))
        if (
            strip_inline_code
            and tag_lower == "markdown"
            and isinstance(cloned.get("content"), str)
        ):
            next_content = normalize_feishu_markdown_inline_code(cloned["content"])
            if next_content != cloned["content"]:
                cloned["content"] = next_content
                normalized_paths.append(f"{el_path}.content")
        elif tag_lower == "column_set" and isinstance(cloned.get("columns"), list):
            next_cols: list[Any] = []
            for col_idx, col in enumerate(cloned["columns"]):
                if not isinstance(col, Mapping) or not isinstance(
                    col.get("elements"), list
                ):
                    next_cols.append(col)
                    continue
                next_col = dict(col)
                next_col["elements"] = _normalize_element_list(
                    col["elements"],
                    f"{el_path}.columns[{col_idx}].elements",
                    normalized_paths,
                    strip_inline_code=strip_inline_code,
                )
                next_cols.append(next_col)
            cloned["columns"] = next_cols
        elif tag_lower in _NESTED_ELEMENTS_CONTAINER_TAGS and isinstance(
            cloned.get("elements"), list
        ):
            cloned["elements"] = _normalize_element_list(
                cloned["elements"],
                f"{el_path}.elements",
                normalized_paths,
                strip_inline_code=strip_inline_code,
            )
        rebuilt.append(cloned)
    return rebuilt


def normalize_feishu_card(
    card: Mapping[str, Any],
    *,
    strip_inline_code: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Normalize one Feishu Schema 2.0 card without mutating the input.

    Returns ``(normalized_card, normalized_paths)`` where ``normalized_paths``
    contains only structural field paths (never card body text).
    ``strip_inline_code`` defaults to ``False`` so submission/storage digests
    keep the submitted card body intact; pass ``strip_inline_code=True`` for
    outbound display paths without a digest contract (e.g. completion notify).
    """
    normalized_paths: list[str] = []
    out: dict[str, Any] = deepcopy(dict(card))

    header = out.get("header")
    if isinstance(header, Mapping):
        out["header"] = _normalize_header(header, normalized_paths)

    body = out.get("body")
    if isinstance(body, Mapping) and isinstance(body.get("elements"), list):
        next_body = dict(body)
        next_body["elements"] = _normalize_element_list(
            body["elements"],
            "body.elements",
            normalized_paths,
            strip_inline_code=strip_inline_code,
        )
        out["body"] = next_body

    if isinstance(out.get("elements"), list):
        out["elements"] = _normalize_element_list(
            out["elements"],
            "elements",
            normalized_paths,
            strip_inline_code=strip_inline_code,
        )

    return out, normalized_paths


def normalize_feishu_cards(
    cards: Sequence[Any],
    *,
    strip_inline_code: bool = False,
) -> tuple[list[Any], list[str]]:
    """Normalize a list of Feishu cards and collect prefixed warning paths."""
    if len(cards) == 1 and isinstance(cards[0], list):
        inner_cards, inner_warnings = normalize_feishu_cards(
            cards[0], strip_inline_code=strip_inline_code
        )
        return [inner_cards], inner_warnings

    normalized_cards: list[Any] = []
    warnings: list[str] = []
    for idx, item in enumerate(cards):
        if isinstance(item, Mapping):
            norm_card, card_paths = normalize_feishu_card(
                item, strip_inline_code=strip_inline_code
            )
            normalized_cards.append(norm_card)
            for path in card_paths:
                warnings.append(f"cards[{idx}].{path}")
        else:
            normalized_cards.append(item)
    return normalized_cards, warnings


__all__ = [
    "normalize_feishu_card",
    "normalize_feishu_cards",
    "normalize_feishu_markdown_inline_code",
]
