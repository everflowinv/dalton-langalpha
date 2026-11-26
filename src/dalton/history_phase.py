"""Split mixed-phase AIMessage content blocks into ordered per-phase text segments.

Pure helper module registered in ``src/server/services/history/replay/keys.py``
so that replay projection digests cover this code directly. Imports strictly
from modules already included in ``projection_sources()``.
"""

from __future__ import annotations

from typing import Any

from src.llms.content_utils import extract_content_with_type
from src.server.utils.text_phase import agreed_phase, block_phase
from src.server.utils.thinking_leak import visible_assistant_text


def split_content_blocks_by_phase(
    content: Any,
    *,
    followed_by_tool_call: bool = False,
) -> tuple[list[tuple[str, str | None]], str | None]:
    """Split ``AIMessage.content`` into ``([(segment_text, segment_phase), ...], reasoning)``.

    - When every non-empty text block agrees on one phase (including all ``None``),
      joins them into a single segment with that agreed phase, preserving exact
      behavior of ``projector.split_content_blocks``.
    - When a single ``AIMessage`` mixes phases (e.g. ``commentary`` followed by
      ``final_answer``), groups consecutive blocks sharing the same phase into
      separate ordered segments so strict share redaction can drop ``commentary``
      while keeping ``final_answer``.
    """
    if content is None:
        return [], None
    if isinstance(content, str):
        if not content:
            return [], None
        visible = visible_assistant_text(
            content, followed_by_tool_call=followed_by_tool_call
        )
        return ([(visible, None)] if visible else []), None

    blocks = content if isinstance(content, list) else [content]
    text_items: list[tuple[str, str | None]] = []
    reasonings: list[str] = []

    for block in blocks:
        if isinstance(block, str):
            if block:
                text_items.append((block, None))
            continue
        extracted, content_type = extract_content_with_type(block)
        if not extracted:
            continue
        if content_type == "reasoning":
            reasonings.append(extracted)
        else:
            text_items.append((extracted, block_phase(block)))

    reasoning = "\n\n".join(reasonings) or None
    if not text_items:
        return [], reasoning

    phases = [p for _, p in text_items]
    distinct_phases = set(phases)
    if len(distinct_phases) == 1:
        phase = agreed_phase(phases)
        joined = "".join(t for t, _ in text_items)
        visible = visible_assistant_text(
            joined, followed_by_tool_call=followed_by_tool_call
        )
        return ([(visible, phase)] if visible else []), reasoning

    # Mixed phases: coalesce consecutive runs with the same phase.
    grouped: list[tuple[list[str], str | None]] = []
    for text_part, phase in text_items:
        if grouped and grouped[-1][1] == phase:
            grouped[-1][0].append(text_part)
        else:
            grouped.append(([text_part], phase))

    segments: list[tuple[str, str | None]] = []
    for parts, phase in grouped:
        joined = "".join(parts)
        visible = visible_assistant_text(
            joined, followed_by_tool_call=followed_by_tool_call
        )
        if visible:
            segments.append((visible, phase))

    return segments, reasoning
