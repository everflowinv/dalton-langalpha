"""Pure Schema 2.0 Feishu card normalizer.

Mirrors ``integrations/dalton-channel-gateway/src/channels/feishu/card-normalize.ts``
so cards submitted via ``automation_report`` are normalized identically before
storage and callers receive normalized field paths as warnings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

_TEXT_ELEMENT_TAGS = frozenset({"text", "plain_text", "lark_md"})
_NESTED_ELEMENTS_CONTAINER_TAGS = frozenset(
    {"column", "collapsible_panel", "form", "interactive_container"}
)


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
            rebuilt.append(
                {
                    "tag": "markdown",
                    "content": _extract_text_element_content(raw_el),
                }
            )
            continue

        cloned: dict[str, Any] = deepcopy(dict(raw_el))
        if tag_lower == "column_set" and isinstance(cloned.get("columns"), list):
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
            )
        rebuilt.append(cloned)
    return rebuilt


def normalize_feishu_card(
    card: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Normalize one Feishu Schema 2.0 card without mutating the input.

    Returns ``(normalized_card, normalized_paths)`` where ``normalized_paths``
    contains only structural field paths (never card body text).
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
        )
        out["body"] = next_body

    if isinstance(out.get("elements"), list):
        out["elements"] = _normalize_element_list(
            out["elements"],
            "elements",
            normalized_paths,
        )

    return out, normalized_paths


def normalize_feishu_cards(
    cards: Sequence[Any],
) -> tuple[list[Any], list[str]]:
    """Normalize a list of Feishu cards and collect prefixed warning paths."""
    if len(cards) == 1 and isinstance(cards[0], list):
        inner_cards, inner_warnings = normalize_feishu_cards(cards[0])
        return [inner_cards], inner_warnings

    normalized_cards: list[Any] = []
    warnings: list[str] = []
    for idx, item in enumerate(cards):
        if isinstance(item, Mapping):
            norm_card, card_paths = normalize_feishu_card(item)
            normalized_cards.append(norm_card)
            for path in card_paths:
                warnings.append(f"cards[{idx}].{path}")
        else:
            normalized_cards.append(item)
    return normalized_cards, warnings


__all__ = ["normalize_feishu_card", "normalize_feishu_cards"]
