"""Dalton live SSE control-event, block-phase, and synthetic-notice helper.

Keeps ``src/server/services/runs/sse_producer.py`` to thin 1–5 line delegation
hooks while centralizing:
- Per-attempt correlation between streamed ``AIMessageChunk.id`` (``lc_run--...``)
  and committed ``AIMessage.id`` so ``message_discarded`` only retracts the
  current attempt's chunk id and never misattributes an earlier prefix.
- ``continuation_link`` latching onto the live prefix id (stream id or final id),
  preserved across intermediate discarded retries.
- ``exhausted_notice`` emission merged and deduplicated by message id against
  LangGraph node-output ``AIMessage`` emission, deferring (never dropping) when
  no prefix has been streamed yet.
- Block-level ``phase`` splitting for mixed-phase Responses frames and metadata
  preservation (``phase`` + ``continued_from_message_id``) across thinking-leak
  buffering and release.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk

from src.dalton.channels.deliverable_text import (
    CONTINUED_FROM_KWARG,
    RETRIED_ATTEMPT_KWARG,
    is_main_agent_lane,
    resolve_live_continuation_id,
)
from src.dalton.history_phase import (
    _coalesce_phase_segments,
    split_content_blocks_by_phase,
)
from src.llms.content_utils import extract_content_with_type
from src.server.utils.content_normalizer import normalize_text_content
from src.server.utils.text_phase import block_phase

#: Marker stamped on synthetic exhausted notice AIMessages for cross-path dedup.
EXHAUSTED_NOTICE_KWARG = "dalton_exhausted_notice"
_EXHAUSTED_FAILURE_KWARG = "dalton_exhausted_failure"


def _lane_key(agent_name: str | None) -> str:
    return "main" if is_main_agent_lane(agent_name) else str(agent_name)


def _split_mixed_phase_content(
    content: Any,
    text_phase_tracker: Any,
    agent_name: str,
    message_id: str,
) -> tuple[list[tuple[str, str | None]], str | None] | None:
    """Split a list-content frame containing multiple distinct text phases.

    Returns ``None`` when the frame has 0 or 1 distinct text phase so the
    standard upstream normalization path runs untouched.
    """
    if not isinstance(content, list):
        return None
    memory = (
        text_phase_tracker._memory(agent_name, message_id)
        if text_phase_tracker is not None and hasattr(text_phase_tracker, "_memory")
        else {}
    )
    text_items: list[tuple[str, str | None]] = []
    reasonings: list[str] = []
    for block in content:
        if isinstance(block, str):
            if block:
                text_items.append((block, None))
            continue
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            idx = block.get("index")
            phase = block_phase(block)
            if phase is not None and isinstance(idx, int):
                memory[idx] = phase
            elif phase is None and isinstance(idx, int):
                phase = memory.get(idx)
            btext = block.get("text")
            if isinstance(btext, str) and btext:
                text_items.append((btext, phase))
            continue
        extracted, ctype = extract_content_with_type(block)
        if not extracted:
            continue
        if ctype == "reasoning":
            reasonings.append(extracted)
        else:
            text_items.append((extracted, block_phase(block)))

    distinct_phases = {p for _, p in text_items}
    if len(distinct_phases) <= 1:
        return None

    # Re-use Dalton's unified phase + opening-thinking splitter when blocks carry
    # explicit phases, preserving tracked phases on index-only deltas.
    normalized_blocks: list[dict[str, Any]] = []
    for seg_text, seg_phase in text_items:
        item: dict[str, Any] = {"type": "text", "text": seg_text}
        if seg_phase:
            item["phase"] = seg_phase
        normalized_blocks.append(item)
    segments, _ = split_content_blocks_by_phase(
        normalized_blocks, followed_by_tool_call=False
    )
    if len(segments) <= 1 and len({p for _, p in segments}) <= 1:
        # Even if thinking stripping left 1 segment, return it with its true phase
        # so upstream agreed_phase doesn't drop phase to None.
        return segments, ("\n\n".join(reasonings) or None)
    return segments, ("\n\n".join(reasonings) or None)


class DaltonStreamControl:
    """Per-run live SSE state for continuation links, discards, phase, and notice dedup."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.pending_continuation: dict[str, str] = {}
        self.bound_continuation: dict[tuple[str, str], str] = {}
        self._emitted_notice_ids: set[str] = set()
        self._seen_message_ids: set[str] = set()
        # Stream chunk id seen ONLY within the currently active model attempt.
        self._current_attempt_stream_id: dict[str, str] = {}
        self._current_attempt_no: dict[str, int] = {}
        # Mapping committed AIMessage.id -> live streamed chunk id (e.g. lc_run--...).
        self._final_to_stream_id: dict[str, dict[str, str]] = {}
        # Last known live prefix id (stream id or committed id) per lane.
        self._last_prefix_live_id: dict[str, str] = {}
        # Deferred exhausted notices waiting for non-streaming node output (flushed at stream end if unused).
        self._deferred_notices: dict[str, dict[str, Any]] = {}
        # Per-message (chunk_text, phase) blocks held while ThinkingLeakGate has not
        # settled to plain yet, so opening thinking tags spanning phases are filtered
        # as one message and each surviving slice restores its original block phase.
        self._thinking_blocks: dict[str, list[tuple[str, str | None]]] = {}
        self._thinking_cont: dict[str, str | None] = {}

    def _resolve_live_prefix_id(self, lane: str, cont_id: str | None) -> tuple[str | None, bool]:
        """Resolve a committed ``continued_from_message_id`` to the id actually seen on
        the live SSE stream, returning ``(live_id, already_streamed)``."""
        if not cont_id:
            return None, False
        if cont_id in self._seen_message_ids:
            return cont_id, True
        mapped = self._final_to_stream_id.get(lane, {}).get(cont_id)
        if mapped and mapped in self._seen_message_ids:
            return mapped, True
        curr_sid = self._current_attempt_stream_id.get(lane)
        if curr_sid and curr_sid in self._seen_message_ids:
            self._final_to_stream_id.setdefault(lane, {})[cont_id] = curr_sid
            return curr_sid, True
        last_live = self._last_prefix_live_id.get(lane)
        if last_live and last_live in self._seen_message_ids:
            return last_live, True
        return cont_id, False

    def handle_custom_event(
        self,
        event_data: dict[str, Any],
        agent_name: str,
        build_event: Callable[[str, dict[str, Any]], Any],
    ) -> tuple[bool, list[Any]]:
        """Process Dalton custom stream events.

        Returns ``(handled, frames)``.
        """
        event_type = event_data.get("type")
        lane = _lane_key(agent_name)

        if event_type == "attempt_start":
            attempt = event_data.get("attempt")
            if isinstance(attempt, int):
                self._current_attempt_no[lane] = attempt
            # New model attempt has not streamed any chunks yet.
            self._current_attempt_stream_id.pop(lane, None)
            return True, []

        if event_type == "continuation_link":
            cont_id = event_data.get(CONTINUED_FROM_KWARG)
            if isinstance(cont_id, str) and cont_id:
                curr_sid = self._current_attempt_stream_id.pop(lane, None)
                lane_map = self._final_to_stream_id.setdefault(lane, {})
                if (
                    curr_sid
                    and curr_sid != cont_id
                    and cont_id not in self._seen_message_ids
                    and cont_id not in lane_map
                ):
                    lane_map[cont_id] = curr_sid
                live_id, _ = self._resolve_live_prefix_id(lane, cont_id)
                effective_cont = live_id or cont_id
                self.pending_continuation[lane] = effective_cont
                self._last_prefix_live_id[lane] = effective_cont
            return True, []

        if event_type == "message_discarded":
            raw_id = event_data.get("id")
            final_id = raw_id if isinstance(raw_id, str) and raw_id else None
            ev_attempt = event_data.get("attempt")
            curr_attempt = self._current_attempt_no.get(lane)
            if ev_attempt is None or curr_attempt is None or ev_attempt == curr_attempt:
                stream_id = self._current_attempt_stream_id.pop(lane, None)
            else:
                stream_id = None

            discarded_ids = {x for x in (final_id, stream_id) if x}
            # Preserve any continuation latch that belongs to an earlier kept prefix;
            # only drop or restore if this discarded attempt had consumed it.
            if self.pending_continuation.get(lane) in discarded_ids:
                self.pending_continuation.pop(lane, None)
            restored_cont: str | None = None
            for d_id in discarded_ids:
                bound_prev = self.bound_continuation.pop((lane, d_id), None)
                if bound_prev and bound_prev not in discarded_ids:
                    restored_cont = bound_prev
                self._thinking_blocks.pop(d_id, None)
                self._thinking_cont.pop(d_id, None)
            if restored_cont and lane not in self.pending_continuation:
                self.pending_continuation[lane] = restored_cont

            effective_id = final_id or stream_id
            payload: dict[str, Any] = {
                "thread_id": self.thread_id,
                "agent": agent_name,
                "id": effective_id,
            }
            if stream_id and stream_id != effective_id:
                payload["stream_id"] = stream_id
                payload["ids"] = [effective_id, stream_id] if effective_id else [stream_id]
            return True, [build_event("message_discarded", payload)]

        if event_type == "exhausted_notice":
            raw_id = event_data.get("id")
            notice_id = raw_id if isinstance(raw_id, str) and raw_id else None
            if notice_id and notice_id in self._emitted_notice_ids:
                return True, []

            raw_cont = event_data.get(CONTINUED_FROM_KWARG)
            cont_id = raw_cont if isinstance(raw_cont, str) and raw_cont else None
            live_cont_id, prefix_streamed = self._resolve_live_prefix_id(lane, cont_id)

            # When chaining onto a prefix that has NOT yet appeared on the live stream
            # (non-streaming node returning [prefix..., notice] in order), defer rather
            # than drop: if LangGraph emits [prefix, notice] as AIMessages next, the
            # deferred copy is cancelled; otherwise flush_deferred_notices emits it at
            # stream end so it is never lost.
            if cont_id and not prefix_streamed:
                self._deferred_notices[notice_id or f"__lane__:{lane}"] = {
                    "agent": agent_name,
                    "lane": lane,
                    "id": notice_id,
                    "content": event_data.get("content", ""),
                    CONTINUED_FROM_KWARG: cont_id,
                }
                return True, []

            return True, [
                self._emit_notice_frame(
                    agent_name=agent_name,
                    lane=lane,
                    notice_id=notice_id,
                    content=str(event_data.get("content", "")),
                    cont_id=live_cont_id,
                    build_event=build_event,
                )
            ]

        return False, []

    def _emit_notice_frame(
        self,
        *,
        agent_name: str,
        lane: str,
        notice_id: str | None,
        content: str,
        cont_id: str | None,
        build_event: Callable[[str, dict[str, Any]], Any],
    ) -> Any:
        if notice_id:
            self._emitted_notice_ids.add(notice_id)
            self._deferred_notices.pop(notice_id, None)
        self._current_attempt_stream_id.pop(lane, None)
        payload: dict[str, Any] = {
            "thread_id": self.thread_id,
            "agent": agent_name,
            "id": notice_id,
            "role": "assistant",
            "content": content,
            "content_type": "text",
            "finish_reason": "stop",
        }
        if not cont_id:
            cont_id = self.pending_continuation.pop(lane, None)
        else:
            self.pending_continuation.pop(lane, None)
        if isinstance(cont_id, str) and cont_id:
            payload[CONTINUED_FROM_KWARG] = cont_id
            if notice_id:
                self.bound_continuation[(lane, notice_id)] = cont_id
        return build_event("message_chunk", payload)

    def flush_deferred_notices(
        self,
        build_event: Callable[[str, dict[str, Any]], Any],
    ) -> list[Any]:
        """Emit any deferred ``exhausted_notice`` that never arrived via node-output messages."""
        if not self._deferred_notices:
            return []
        frames: list[Any] = []
        for key, item in list(self._deferred_notices.items()):
            self._deferred_notices.pop(key, None)
            notice_id = item.get("id")
            if notice_id and notice_id in self._emitted_notice_ids:
                continue
            lane = item["lane"]
            raw_cont = item.get(CONTINUED_FROM_KWARG)
            live_cont, _ = self._resolve_live_prefix_id(lane, raw_cont)
            frames.append(
                self._emit_notice_frame(
                    agent_name=item["agent"],
                    lane=lane,
                    notice_id=notice_id,
                    content=str(item.get("content", "")),
                    cont_id=live_cont or raw_cont,
                    build_event=build_event,
                )
            )
        return frames

    def should_suppress_message(
        self,
        message_chunk: Any,
        agent_name: str,
        message_id: str | None,
    ) -> bool:
        """Track per-attempt streamed chunk ids and deduplicate synthetic notices /
        replayed complete AIMessages whose content already streamed under ``lc_run--``."""
        lane = _lane_key(agent_name)
        valid_id = isinstance(message_id, str) and bool(message_id) and message_id != "unknown"
        if isinstance(message_chunk, AIMessageChunk):
            if valid_id:
                self._current_attempt_stream_id[lane] = message_id  # type: ignore[assignment]
                self._seen_message_ids.add(message_id)  # type: ignore[arg-type]
            return False

        if isinstance(message_chunk, AIMessage):
            kwargs = getattr(message_chunk, "additional_kwargs", None) or {}
            if kwargs.get(RETRIED_ATTEMPT_KWARG):
                return True
            if valid_id and message_id in self._emitted_notice_ids:
                return True
            if valid_id and message_id in self._final_to_stream_id.get(lane, {}):
                # Already streamed via its attempt's lc_run-- chunks; skip duplicate node replay.
                return True
            if valid_id:
                self._seen_message_ids.add(message_id)  # type: ignore[arg-type]
            if kwargs.get(EXHAUSTED_NOTICE_KWARG) or kwargs.get(_EXHAUSTED_FAILURE_KWARG):
                if valid_id:
                    self._emitted_notice_ids.add(message_id)  # type: ignore[arg-type]
                    self._deferred_notices.pop(message_id, None)
                    self._deferred_notices.pop(f"__lane__:{lane}", None)
        return False

    def resolve_continuation(
        self,
        agent_name: str,
        message_id: str | None,
        message_chunk: Any,
    ) -> str | None:
        cont = resolve_live_continuation_id(
            self.pending_continuation,
            self.bound_continuation,
            agent_name,
            message_id,
            message_chunk,
        )
        if not cont:
            return None
        lane = _lane_key(agent_name)
        live_cont, _ = self._resolve_live_prefix_id(lane, cont)
        resolved = live_cont or cont
        if message_id:
            self.bound_continuation[(lane, message_id)] = resolved
        return resolved

    def _build_text_frame(
        self,
        *,
        agent_name: str,
        message_id: str,
        content: str,
        phase: str | None,
        continued_from: str | None,
        build_event: Callable[[str, dict[str, Any]], Any],
    ) -> Any:
        payload: dict[str, Any] = {
            "thread_id": self.thread_id,
            "agent": agent_name,
            "id": message_id,
            "role": "assistant",
            "content": content,
            "content_type": "text",
        }
        if phase:
            payload["phase"] = phase
        if continued_from:
            payload[CONTINUED_FROM_KWARG] = continued_from
        return build_event("message_chunk", payload)

    def feed_thinking(
        self,
        producer: Any,
        message_chunk: Any,
        *,
        agent_name: str,
        message_id: str,
        text_content: str,
        text_phase: str | None,
        live_continued_from: str | None,
    ) -> tuple[list[Any], str | None, str | None]:
        """Feed one text slice into ``producer._thinking_leak`` while preserving
        block-level ``phase`` across phase switches within the same message.

        Returns ``(leading_frames, remaining_text, remaining_phase)``.
        """
        thinking_leak = producer._thinking_leak
        mode = getattr(thinking_leak, "_mode", {}).get(message_id)
        if mode == "plain":
            return [], text_content, text_phase
        if mode == "drop":
            return [], None, text_phase

        if live_continued_from:
            self._thinking_cont[message_id] = live_continued_from
        blocks = self._thinking_blocks.setdefault(message_id, [])
        blocks.append((text_content, text_phase))

        out = thinking_leak.feed(message_id, text_content)
        if out is None:
            return [], None, text_phase

        # Opening hold resolved to plain: emit each phase slice under its own phase.
        held_blocks = self._thinking_blocks.pop(message_id, [])
        stored_cont = self._thinking_cont.pop(message_id, None) or live_continued_from
        segments = _coalesce_phase_segments(held_blocks)
        if not segments:
            return [], out, text_phase
        if len(segments) == 1:
            seg_text, seg_phase = segments[0]
            return [], seg_text, seg_phase

        leading_frames: list[Any] = []
        for seg_text, seg_phase in segments[:-1]:
            if not seg_text:
                continue
            if isinstance(message_chunk, AIMessageChunk):
                producer._streamed_content_ids.add(message_id)
            leading_frames.append(
                self._build_text_frame(
                    agent_name=agent_name,
                    message_id=message_id,
                    content=seg_text,
                    phase=seg_phase,
                    continued_from=stored_cont,
                    build_event=producer._build_sse_event,
                )
            )
        last_text, last_phase = segments[-1]
        return leading_frames, last_text, last_phase

    def settle_thinking(
        self,
        producer: Any,
        message_chunk: Any,
        event_stream_message: dict[str, Any],
        *,
        agent_name: str,
        message_id: str,
        tool_call: bool,
        fallback_phase: str | None,
        fallback_continued_from: str | None,
    ) -> tuple[bool, list[Any]]:
        """Settle ``producer._thinking_leak`` for ``message_id``, mapping stripped
        opening thinking spans back onto the original per-phase blocks.

        Returns ``(was_pending, leading_frames)``.
        """
        thinking_leak = producer._thinking_leak
        was_pending = bool(thinking_leak.pending(message_id))
        already = event_stream_message.get("content")
        visible = thinking_leak.settle(
            message_id,
            tool_call=tool_call,
            already_visible=already if isinstance(already, str) else None,
        )
        held_blocks = self._thinking_blocks.pop(message_id, None)
        stored_cont = self._thinking_cont.pop(message_id, None)
        cont = (
            event_stream_message.get(CONTINUED_FROM_KWARG)
            or stored_cont
            or fallback_continued_from
        )

        if not was_pending:
            if visible:
                event_stream_message["content"] = visible
                event_stream_message["content_type"] = "text"
                phase = event_stream_message.get("phase") or fallback_phase
                if phase:
                    event_stream_message["phase"] = phase
                if cont:
                    event_stream_message[CONTINUED_FROM_KWARG] = cont
            elif event_stream_message.get("content_type") == "text":
                event_stream_message.pop("content", None)
                event_stream_message.pop("content_type", None)
                event_stream_message.pop("phase", None)
            return False, []

        if not visible:
            if event_stream_message.get("content_type") == "text":
                event_stream_message.pop("content", None)
                event_stream_message.pop("content_type", None)
                event_stream_message.pop("phase", None)
            return True, []

        if held_blocks:
            normalized_blocks: list[dict[str, Any]] = [
                ({"type": "text", "text": t, "phase": p} if p else {"type": "text", "text": t})
                for t, p in held_blocks
            ]
            segments, _ = split_content_blocks_by_phase(
                normalized_blocks, followed_by_tool_call=tool_call
            )
            if not segments:
                if event_stream_message.get("content_type") == "text":
                    event_stream_message.pop("content", None)
                    event_stream_message.pop("content_type", None)
                    event_stream_message.pop("phase", None)
                return True, []

            leading_frames: list[Any] = []
            for seg_text, seg_phase in segments[:-1]:
                if not seg_text:
                    continue
                if isinstance(message_chunk, AIMessageChunk):
                    producer._streamed_content_ids.add(message_id)
                leading_frames.append(
                    self._build_text_frame(
                        agent_name=agent_name,
                        message_id=message_id,
                        content=seg_text,
                        phase=seg_phase,
                        continued_from=cont,
                        build_event=producer._build_sse_event,
                    )
                )
            last_text, last_phase = segments[-1]
            event_stream_message["content"] = last_text
            event_stream_message["content_type"] = "text"
            if last_phase:
                event_stream_message["phase"] = last_phase
            else:
                event_stream_message.pop("phase", None)
            if cont:
                event_stream_message[CONTINUED_FROM_KWARG] = cont
            return True, leading_frames

        event_stream_message["content"] = visible
        event_stream_message["content_type"] = "text"
        phase = event_stream_message.get("phase") or fallback_phase
        if phase:
            event_stream_message["phase"] = phase
        if cont:
            event_stream_message[CONTINUED_FROM_KWARG] = cont
        return True, []

    def frames_for_released(
        self,
        released: list[tuple[str, str]],
        thinking_agent: Mapping[str, str],
        build_event: Callable[[str, dict[str, Any]], Any],
    ) -> list[Any]:
        """Build SSE frames for messages released via ``release_except`` / ``release_all``,
        restoring each surviving slice's original block ``phase``."""
        frames: list[Any] = []
        for message_id, visible in released:
            agent_name = thinking_agent.get(message_id, "main")
            held_blocks = self._thinking_blocks.pop(message_id, None)
            stored_cont = self._thinking_cont.pop(message_id, None)
            if held_blocks:
                normalized_blocks: list[dict[str, Any]] = [
                    ({"type": "text", "text": t, "phase": p} if p else {"type": "text", "text": t})
                    for t, p in held_blocks
                ]
                segments, _ = split_content_blocks_by_phase(
                    normalized_blocks, followed_by_tool_call=False
                )
                for seg_text, seg_phase in segments:
                    if not seg_text:
                        continue
                    frames.append(
                        self._build_text_frame(
                            agent_name=agent_name,
                            message_id=message_id,
                            content=seg_text,
                            phase=seg_phase,
                            continued_from=stored_cont,
                            build_event=build_event,
                        )
                    )
                continue
            frames.append(
                self._build_text_frame(
                    agent_name=agent_name,
                    message_id=message_id,
                    content=visible,
                    phase=None,
                    continued_from=stored_cont,
                    build_event=build_event,
                )
            )
        return frames

    def extract_chunk_content(
        self,
        producer: Any,
        message_chunk: Any,
        *,
        agent_name: str,
        message_id: str,
        chunk_event_type: str,
        is_compaction: bool,
        is_inner_llm_chunk: bool,
        live_continued_from: str | None,
    ) -> tuple[list[Any], str | None, str | None, str | None]:
        """Extract ``(leading_frames, text_content, content_type, text_phase)`` for a
        live chunk, handling mixed-phase Responses frames and reasoning lifecycle in
        Dalton so ``sse_producer.py`` stays a thin delegation hook."""
        from src.dalton.settings import is_strict_deliverable_text_enabled

        if (
            not is_compaction
            and not is_inner_llm_chunk
            and isinstance(message_chunk, (AIMessage, AIMessageChunk))
            and is_strict_deliverable_text_enabled()
        ):
            split = _split_mixed_phase_content(
                message_chunk.content,
                producer._text_phase,
                agent_name,
                message_id,
            )
            if split is not None:
                segments, mixed_reasoning = split
                leading_frames: list[Any] = []
                if mixed_reasoning:
                    if agent_name not in producer.reasoning_active:
                        leading_frames.append(
                            producer._build_reasoning_signal(
                                agent_name, message_id, "start", is_compaction=is_compaction
                            )
                        )
                        producer.reasoning_active.add(agent_name)
                    leading_frames.append(
                        producer._build_sse_event(
                            chunk_event_type,
                            {
                                "thread_id": self.thread_id,
                                "agent": agent_name,
                                "id": message_id,
                                "role": "assistant",
                                "content": mixed_reasoning,
                                "content_type": "reasoning",
                            },
                        )
                    )
                    leading_frames.append(
                        producer._build_reasoning_signal(
                            agent_name, message_id, "complete", is_compaction=is_compaction
                        )
                    )
                    producer.reasoning_active.discard(agent_name)
                if not segments:
                    return leading_frames, None, None, None

                for seg_text, seg_phase in segments[:-1]:
                    if not seg_text:
                        continue
                    producer._thinking_agent[message_id] = agent_name
                    flushed, rem_text, rem_phase = self.feed_thinking(
                        producer,
                        message_chunk,
                        agent_name=agent_name,
                        message_id=message_id,
                        text_content=seg_text,
                        text_phase=seg_phase,
                        live_continued_from=live_continued_from,
                    )
                    leading_frames.extend(flushed)
                    if rem_text:
                        if isinstance(message_chunk, AIMessageChunk):
                            producer._streamed_content_ids.add(message_id)
                        leading_frames.append(
                            self._build_text_frame(
                                agent_name=agent_name,
                                message_id=message_id,
                                content=rem_text,
                                phase=rem_phase,
                                continued_from=live_continued_from,
                                build_event=producer._build_sse_event,
                            )
                        )
                last_text, last_phase = segments[-1]
                return leading_frames, last_text, ("text" if last_text else None), last_phase

        text_phase = producer._text_phase.resolve(agent_name, message_id, message_chunk.content)
        text_content, content_type = normalize_text_content(message_chunk.content)
        return [], text_content, content_type, text_phase

    def expand_saved_images_by_message(
        self,
        saved_images: Mapping[str, Mapping[str, str | None]],
    ) -> dict[str, dict[str, str]]:
        """Build per-message captured image URL map, aliasing committed message ids
        to their streamed ``lc_run--`` chunk ids so live consumers match by segment id."""
        by_msg: dict[str, dict[str, str]] = {}
        stream_aliases: dict[str, list[str]] = {}
        for lane_map in self._final_to_stream_id.values():
            for final_id, stream_id in lane_map.items():
                stream_aliases.setdefault(final_id, []).append(stream_id)
        for msg_id, images in saved_images.items():
            captured = {p: u for p, u in images.items() if isinstance(u, str) and u}
            if not captured:
                continue
            by_msg[msg_id] = dict(captured)
            for alias in stream_aliases.get(msg_id, ()):
                by_msg[alias] = dict(captured)
        return by_msg
