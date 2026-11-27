"""Split mixed-phase AIMessage content blocks into ordered per-phase text segments.

Pure helper module registered in ``src/server/services/history/replay/keys.py``
so that replay projection digests cover this code directly. Imports strictly
from modules already included in ``projection_sources()``.
"""

from __future__ import annotations

from typing import Any

from src.dalton.thinking_spans import scan_opening_thinking_spans
from src.llms.content_utils import extract_content_with_type
from src.server.utils.text_phase import agreed_phase, block_phase
from src.server.utils.thinking_leak import decide_opening


def _subtract_spans_from_blocks(
    text_items: list[tuple[str, str | None]],
    strip_spans: tuple[tuple[int, int], ...],
) -> list[tuple[str, str | None]]:
    """Map character ranges in the concatenated message back onto individual blocks."""
    if not strip_spans:
        return text_items

    out: list[tuple[str, str | None]] = []
    offset = 0
    for text_part, phase in text_items:
        b_start = offset
        b_end = offset + len(text_part)
        offset = b_end
        if not text_part:
            continue

        cursor = b_start
        kept_slices: list[str] = []
        for s_start, s_end in strip_spans:
            if s_end <= cursor:
                continue
            if s_start >= b_end:
                break
            if s_start > cursor:
                kept_slices.append(text_part[cursor - b_start : s_start - b_start])
            cursor = max(cursor, min(s_end, b_end))
        if cursor < b_end:
            kept_slices.append(text_part[cursor - b_start : b_end - b_start])

        remaining = "".join(kept_slices)
        if remaining:
            out.append((remaining, phase))
    return out


def _coalesce_phase_segments(
    text_items: list[tuple[str, str | None]],
) -> list[tuple[str, str | None]]:
    if not text_items:
        return []
    phases = [p for _, p in text_items]
    if len(set(phases)) == 1:
        phase = agreed_phase(phases)
        joined = "".join(t for t, _ in text_items)
        return [(joined, phase)] if joined else []

    grouped: list[tuple[list[str], str | None]] = []
    for text_part, phase in text_items:
        if grouped and grouped[-1][1] == phase:
            grouped[-1][0].append(text_part)
        else:
            grouped.append(([text_part], phase))
    return [("".join(parts), phase) for parts, phase in grouped if "".join(parts)]


def _visible_assistant_text_nested(
    text: str,
    *,
    followed_by_tool_call: bool,
) -> str:
    """Strip opening thinking markers using full-message code-aware stack scanning."""
    if not text:
        return text
    segments, _ = split_content_blocks_by_phase(
        text, followed_by_tool_call=followed_by_tool_call
    )
    return "".join(seg for seg, _ in segments)


def split_content_blocks_by_phase(
    content: Any,
    *,
    followed_by_tool_call: bool = False,
) -> tuple[list[tuple[str, str | None]], str | None]:
    """Split ``AIMessage.content`` into ``([(segment_text, segment_phase), ...], reasoning)``.

    Scans the full concatenated text of the ``AIMessage`` across all phases first
    via ``scan_opening_thinking_spans`` so thinking blocks spanning ``commentary``
    and ``final_answer`` (or containing inline/fenced code or mismatched inner
    tags) are handled accurately:
    - When an opening thinking block is unclosed, all blocks are returned
      unchanged so strict share redaction can still recognize and drop the
      unclosed message.
    - When opening thinking block(s) close cleanly, the stripped character spans
      are mapped back to the original blocks before grouping by consecutive phase.
    """
    if content is None:
        return [], None

    text_items: list[tuple[str, str | None]] = []
    reasonings: list[str] = []

    if isinstance(content, str):
        if not content:
            return [], None
        text_items.append((content, None))
    else:
        blocks = content if isinstance(content, list) else [content]
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

    joined = "".join(t for t, _ in text_items)
    scan = scan_opening_thinking_spans(joined)
    if scan.unclosed:
        # Preserve all blocks unmodified so strict share filtering sees the
        # opening thinking tag and drops the entire unclosed message.
        return _coalesce_phase_segments(text_items), reasoning

    strip_spans: list[tuple[int, int]] = list(scan.strip_spans)
    tail_start = strip_spans[-1][1] if strip_spans else 0
    tail_text = joined[tail_start:]
    if tail_text:
        opening = decide_opening(tail_text)
        if opening is not None and opening.kind == "need":
            strip_spans.append((tail_start, tail_start + opening.end))

    if strip_spans:
        if followed_by_tool_call:
            return [], reasoning
        text_items = _subtract_spans_from_blocks(text_items, tuple(strip_spans))

    segments = _coalesce_phase_segments(text_items)
    if len(segments) <= 1:
        return segments, reasoning

    # When a message has multiple phase segments and did not open with a
    # message-level thinking block (e.g. plain commentary followed by a
    # final_answer segment that itself opens with <thinking>...</thinking>),
    # strip closed opening thinking blocks on each subsequent phase segment too.
    final_segments: list[tuple[str, str | None]] = []
    for seg_text, seg_phase in segments:
        seg_scan = scan_opening_thinking_spans(seg_text)
        if seg_scan.unclosed:
            final_segments.append((seg_text, seg_phase))
            continue
        seg_spans = list(seg_scan.strip_spans)
        seg_tail_start = seg_spans[-1][1] if seg_spans else 0
        seg_tail = seg_text[seg_tail_start:]
        if seg_tail:
            seg_opening = decide_opening(seg_tail)
            if seg_opening is not None and seg_opening.kind == "need":
                seg_spans.append((seg_tail_start, seg_tail_start + seg_opening.end))
        if seg_spans:
            if followed_by_tool_call:
                continue
            stripped = seg_text[seg_spans[-1][1] :]
            if stripped:
                final_segments.append((stripped, seg_phase))
        elif seg_text:
            final_segments.append((seg_text, seg_phase))
    return final_segments, reasoning


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
