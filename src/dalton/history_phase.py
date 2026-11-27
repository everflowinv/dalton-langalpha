"""Split mixed-phase AIMessage content blocks into ordered per-phase text segments.

Pure helper module registered in ``src/server/services/history/replay/keys.py``
so that replay projection digests cover this code directly. Imports strictly
from modules already included in ``projection_sources()``.
"""

from __future__ import annotations

import re
from typing import Any

from src.llms.content_utils import extract_content_with_type
from src.server.utils.text_phase import agreed_phase, block_phase
from src.server.utils.thinking_leak import visible_assistant_text

_OPEN_THINK_TAG_RE = re.compile(
    r"<\s*(thinking_mode|thinking)\s*>",
    re.IGNORECASE,
)
_THINK_TAG_TOKEN_RE = re.compile(
    r"<\s*(/)?\s*(thinking_mode|thinking)\s*>",
    re.IGNORECASE,
)


def _visible_assistant_text_nested(
    text: str,
    *,
    followed_by_tool_call: bool,
) -> str:
    """Strip opening thinking markers using full-message nesting depth.

    Unlike streaming ``visible_assistant_text``, cold projection has the
    complete message text: nested ``<thinking>a <thinking>b</thinking> c</thinking>``
    blocks must close at the matching outer tag rather than truncating at the
    first inner closer (which would otherwise leak ``c`` into ``turn_slices``
    and public replay).
    """
    if not text:
        return text

    size = len(text)
    pos = 0
    while pos < size and text[pos].isspace():
        pos += 1
    if pos >= size:
        return text

    if _OPEN_THINK_TAG_RE.match(text, pos) is None:
        return visible_assistant_text(
            text, followed_by_tool_call=followed_by_tool_call
        )

    cur = pos
    stripped_any = False
    while cur < size:
        ws_end = cur
        while ws_end < size and text[ws_end].isspace():
            ws_end += 1
        if ws_end >= size or _OPEN_THINK_TAG_RE.match(text, ws_end) is None:
            break
        stack: list[str] = []
        matched_end: int | None = None
        for m in _THINK_TAG_TOKEN_RE.finditer(text, ws_end):
            is_close = bool(m.group(1))
            tag_name = m.group(2).lower()
            if not is_close:
                stack.append(tag_name)
            else:
                if tag_name in stack:
                    while stack:
                        if stack.pop() == tag_name:
                            break
                if not stack:
                    matched_end = m.end()
                    break
        if matched_end is None:
            # Unclosed outer opening tag: never truncate at an inner closer.
            if not stripped_any:
                return text
            break
        stripped_any = True
        cur = matched_end

    if not stripped_any:
        return text
    if followed_by_tool_call:
        return ""
    rest = text[cur:]
    if rest.lstrip().lower().startswith("need more thinking"):
        return visible_assistant_text(rest, followed_by_tool_call=False)
    return rest


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
        visible = _visible_assistant_text_nested(
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
        visible = _visible_assistant_text_nested(
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
        visible = _visible_assistant_text_nested(
            joined, followed_by_tool_call=followed_by_tool_call
        )
        if visible:
            segments.append((visible, phase))

    return segments, reasoning


def build_ai_phase_text_specs(
    content: Any,
    *,
    is_close_out: bool,
    has_tool_calls: bool,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(text_event_data_list, reasoning)`` for ``projector._project_ai_message``."""
    if is_close_out:
        return [], None
    segments, reasoning = split_content_blocks_by_phase(
        content, followed_by_tool_call=has_tool_calls
    )
    specs: list[dict[str, Any]] = []
    last_idx = len(segments) - 1
    for idx, (seg_text, seg_phase) in enumerate(segments):
        data: dict[str, Any] = {
            "content": seg_text,
            "finish_reason": None if (has_tool_calls or idx != last_idx) else "stop",
        }
        if seg_phase:
            data["phase"] = seg_phase
        specs.append(data)
    return specs, reasoning


def build_ai_phase_events(
    content: Any,
    *,
    agent: str,
    message_id: str,
    is_close_out: bool,
    has_tool_calls: bool,
    event_cls: Any,
) -> list[Any]:
    """Build reasoning and phase-split text ``HistoryEvent``s for an ``AIMessage``."""
    text_specs, reasoning = build_ai_phase_text_specs(
        content,
        is_close_out=is_close_out,
        has_tool_calls=has_tool_calls,
    )
    events: list[Any] = []
    if reasoning:
        events.append(
            event_cls("reasoning-signal", agent, message_id, {"signal": "start"})
        )
        events.append(
            event_cls("reasoning", agent, message_id, {"content": reasoning})
        )
        events.append(
            event_cls("reasoning-signal", agent, message_id, {"signal": "complete"})
        )
    for text_data in text_specs:
        events.append(event_cls("text", agent, message_id, text_data))
    return events
