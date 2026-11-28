"""Unified core deliverable-text extractor and per-outlet empty-delivery notices.

Implements prod-audit-2 §5:
- Preserves block-level ``phase`` and drops ``commentary`` and reasoning blocks.
- Treats ``close_out == True`` (plan close-out ``UpdatePlan``) as non-resetting;
  any other business tool call resets accumulated text with NO fallback to
  pre-tool preamble.
- Isolates distinct ``AIMessage.id`` messages unless explicitly chained via
  ``continued_from_message_id`` (output-cap pure-text continuation or chained
  exhausted notice).
- Skips superseded non-continuation retry attempts (``dalton_retried_attempt``
  on checkpoints; ``message_discarded`` on live SSE, matching both final id and
  stream chunk id).
- Strips opening thinking-leak markers via ``visible_assistant_text(..., followed_by_tool_call=False)``
  and applies unified sandbox image-path replacement across live SSE, replay,
  thread output, and checkpoint/automation extractors.
- Provides deterministic empty-delivery notice helpers for channel, automation,
  and thread-output outlets.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import threading
from typing import Any

from langchain_core.messages import AIMessage

from ptc_agent.agent.middleware.image_capture import (
    IMAGE_MD_RE,
    is_sandbox_image_path,
    recorded_images,
    resolve_image_paths,
)
from ptc_agent.agent.middleware.plan_close_out import is_close_out_reply
from src.dalton.history_phase import split_content_blocks_by_phase
from src.server.utils.thinking_leak import visible_assistant_text

#: Stamped on superseded non-continuation retry attempts kept in injected history.
RETRIED_ATTEMPT_KWARG = "dalton_retried_attempt"

#: Stamped on pure-text output_cap continuation replies to link to their predecessor.
CONTINUED_FROM_KWARG = "continued_from_message_id"

#: Deterministic notices for outlets when business tools ran but final text is empty (§5.3).
CHANNEL_EMPTY_AFTER_TOOLS_NOTICE = (
    "已完成工具执行，但未生成最终文字总结；可回复“继续”获取结论，或在工作区查看执行结果。"
)
CHANNEL_SEND_FAILED_AFTER_TOOLS_NOTICE = (
    "已完成工具执行，但向当前会话发送结果未成功且未生成文字总结；"
    "可回复“继续”重发结论，或在工作区查看执行结果。"
)
AUTOMATION_EMPTY_AFTER_TOOLS_SUMMARY = "已完成工具步骤执行，未输出文字摘要。"
AUTOMATION_FILES_ONLY_EXCERPT = "已通过渠道发送附件结果。"
AUTOMATION_TEXT_DELIVERED_EXCERPT = "已通过渠道发送文字结果。"


@dataclass(frozen=True)
class DeliverableExtractionResult:
    """Pure core extraction output shared across live SSE, replay, output, and automation."""

    text: str
    saw_business_tool_call: bool


def is_main_agent_lane(agent: Any) -> bool:
    """Return True when ``agent`` belongs to the main agent lane (not a subagent task)."""
    if agent is None or agent == "":
        return True
    if not isinstance(agent, str):
        return False
    lowered = agent.strip().lower()
    if lowered.startswith("task:") or lowered.startswith("subagent:"):
        return False
    return True


def contains_unresolved_sandbox_image_path(text: str) -> bool:
    """True if markdown text still references a sandbox-relative image path."""
    if not text:
        return False
    return any(is_sandbox_image_path(m.group(2)) for m in IMAGE_MD_RE.finditer(text))


def _clean_visible_text(raw: str) -> str:
    if not raw:
        return ""
    cleaned = visible_assistant_text(raw, followed_by_tool_call=False)
    return cleaned if cleaned.strip() else ""


def _extract_ai_message_deliverable_text(
    message: AIMessage,
    *,
    resolve_images: bool = False,
    image_url_map: Mapping[str, str] | None = None,
) -> str:
    """Extract non-commentary, opening-thinking-stripped text from a single AIMessage."""
    segments, _reasoning = split_content_blocks_by_phase(
        message.content,
        followed_by_tool_call=False,
    )
    kept = [seg_text for seg_text, seg_phase in segments if seg_phase != "commentary"]
    if not kept:
        return ""
    joined = "".join(kept)
    cleaned = visible_assistant_text(joined, followed_by_tool_call=False)
    if not cleaned or not cleaned.strip():
        return ""
    if resolve_images:
        own = recorded_images(message)
        merged_map = {**(dict(image_url_map) if image_url_map else {}), **own}
        if merged_map:
            cleaned = resolve_image_paths(cleaned, merged_map)
    return cleaned


def extract_deliverable_text_from_messages(
    messages: Sequence[Any],
    *,
    resolve_images: bool = False,
    image_url_map: Mapping[str, str] | None = None,
) -> DeliverableExtractionResult:
    """Extract the final deliverable text from a turn's checkpoint messages."""
    active_parts: list[str] = []
    active_chain_ids: set[str] = set()
    active_msg_id: str | None = None
    saw_business_tool_call = False

    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        kwargs = getattr(msg, "additional_kwargs", None) or {}
        if kwargs.get(RETRIED_ATTEMPT_KWARG):
            continue

        if is_close_out_reply(msg):
            # Silent plan close-out UpdatePlan never resets the user's final answer.
            continue

        if getattr(msg, "tool_calls", None):
            saw_business_tool_call = True
            active_parts.clear()
            active_chain_ids.clear()
            active_msg_id = None
            continue

        text = _extract_ai_message_deliverable_text(
            msg,
            resolve_images=resolve_images,
            image_url_map=image_url_map,
        )
        if not text:
            continue

        raw_id = getattr(msg, "id", None)
        msg_id = raw_id if isinstance(raw_id, str) and raw_id else None
        raw_cont = kwargs.get(CONTINUED_FROM_KWARG)
        continued_from = raw_cont if isinstance(raw_cont, str) and raw_cont else None

        if continued_from and continued_from in active_chain_ids:
            active_parts.append(text)
            if msg_id:
                active_chain_ids.add(msg_id)
                active_msg_id = msg_id
        elif msg_id is not None and active_msg_id is not None and msg_id == active_msg_id:
            active_parts.append(text)
        else:
            active_parts = [text]
            active_chain_ids = {msg_id} if msg_id else set()
            active_msg_id = msg_id

    combined = "".join(active_parts)
    if not combined.strip():
        return DeliverableExtractionResult(
            text="", saw_business_tool_call=saw_business_tool_call
        )
    return DeliverableExtractionResult(
        text=combined, saw_business_tool_call=saw_business_tool_call
    )


def _extract_discarded_ids(data: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    for field in ("id", "stream_id"):
        val = data.get(field)
        if isinstance(val, str) and val:
            ids.add(val)
    raw_list = data.get("ids")
    if isinstance(raw_list, (list, tuple)):
        for item in raw_list:
            if isinstance(item, str) and item:
                ids.add(item)
    return ids


def _has_valid_tool_calls(raw_calls: Any) -> bool:
    """True only when ``tool_calls`` contains at least one valid business tool call."""
    if not isinstance(raw_calls, list):
        return False
    return any(
        isinstance(tc, Mapping)
        and isinstance(tc.get("name"), str)
        and bool(tc.get("name", "").strip())
        for tc in raw_calls
    )


def _ingest_image_map(target: dict[str, str], raw: Any) -> None:
    if not isinstance(raw, Mapping):
        return
    for k, v in raw.items():
        if isinstance(k, str) and k and isinstance(v, str) and v:
            target[k] = v


def _ingest_images_by_message(
    target: dict[str, dict[str, str]],
    raw: Any,
) -> None:
    if not isinstance(raw, Mapping):
        return
    for msg_id, mapping in raw.items():
        if not isinstance(msg_id, str) or not msg_id or not isinstance(mapping, Mapping):
            continue
        bucket = target.setdefault(msg_id, {})
        _ingest_image_map(bucket, mapping)


class DeliverableTextAccumulator:
    """Incremental SSE event accumulator mirroring ``deliverable-text.ts``."""

    def __init__(self, *, archive_mode: bool = False) -> None:
        # Ordered list of [msg_id | None, accumulated_text] in the active reply chain.
        self._segments: list[list[Any]] = []
        self._chain_ids: set[str] = set()
        self._active_msg_id: str | None = None
        self._discarded_ids: set[str] = set()
        self._image_url_map: dict[str, str] = {}
        self._images_by_message: dict[str, dict[str, str]] = {}
        self._close_out_tool_call_ids: set[str] = set()
        self._pending_tool_call_ids: set[str] = set()
        self._pending_tool_calls_by_msg: dict[str, set[str]] = {}
        self._has_pending_unnamed_tool_call: bool = False
        self._tool_call_to_msg_id: dict[str, str] = {}
        self._archive_mode = archive_mode
        self.saw_business_tool_call: bool = False

    def _reset_active(self) -> None:
        self._segments = []
        self._chain_ids = set()
        self._active_msg_id = None

    def _drop_pending_tool_call(self, tc_id: str | None) -> None:
        if tc_id:
            self._pending_tool_call_ids.discard(tc_id)
            for msg_tcs in list(self._pending_tool_calls_by_msg.values()):
                msg_tcs.discard(tc_id)
        else:
            self._has_pending_unnamed_tool_call = False

    def _confirm_business_tool_execution(self, tc_id: str | None = None) -> None:
        self._drop_pending_tool_call(tc_id)
        if tc_id is None:
            self._has_pending_unnamed_tool_call = False
        self.saw_business_tool_call = True
        self._reset_active()

    def set_image_url_map(self, images: Mapping[str, str] | None) -> None:
        _ingest_image_map(self._image_url_map, images)

    def set_images_by_message(
        self, images_by_message: Mapping[str, Mapping[str, str]] | None
    ) -> None:
        _ingest_images_by_message(self._images_by_message, images_by_message)

    def push_event(self, event_type: str, data: Mapping[str, Any] | None) -> None:
        if not isinstance(data, Mapping):
            return

        if event_type == "run_end":
            _ingest_image_map(self._image_url_map, data.get("images"))
            _ingest_images_by_message(
                self._images_by_message, data.get("images_by_message")
            )
            return

        if not is_main_agent_lane(data.get("agent")):
            return

        if event_type == "tool_calls":
            raw_calls = data.get("tool_calls")
            raw_id = data.get("id")
            msg_id = raw_id if isinstance(raw_id, str) and raw_id else None
            if data.get("close_out") is True:
                if isinstance(raw_calls, list):
                    for tc in raw_calls:
                        if isinstance(tc, Mapping):
                            tc_id = tc.get("id")
                            if isinstance(tc_id, str) and tc_id:
                                self._close_out_tool_call_ids.add(tc_id)
                return
            if not _has_valid_tool_calls(raw_calls):
                return
            if isinstance(raw_calls, list):
                for tc in raw_calls:
                    if not isinstance(tc, Mapping):
                        continue
                    name = tc.get("name")
                    if not isinstance(name, str) or not name.strip():
                        continue
                    tc_id = tc.get("id")
                    if isinstance(tc_id, str) and tc_id:
                        self._pending_tool_call_ids.add(tc_id)
                        if msg_id:
                            self._pending_tool_calls_by_msg.setdefault(msg_id, set()).add(tc_id)
                            self._tool_call_to_msg_id[tc_id] = msg_id
                    else:
                        self._has_pending_unnamed_tool_call = True
            if data.get("execution_started") is True or data.get("confirmed") is True:
                self._confirm_business_tool_execution()
            return

        if event_type == "tool_call_start":
            raw_tc_id = data.get("tool_call_id")
            tc_id = raw_tc_id if isinstance(raw_tc_id, str) and raw_tc_id else None
            if tc_id and tc_id in self._close_out_tool_call_ids:
                return
            self._confirm_business_tool_execution(tc_id)
            return

        if event_type == "tool_call_result":
            raw_tc_id = data.get("tool_call_id")
            tc_id = raw_tc_id if isinstance(raw_tc_id, str) and raw_tc_id else None
            if data.get("discarded") is True:
                self._drop_pending_tool_call(tc_id)
                return
            if tc_id and tc_id in self._close_out_tool_call_ids:
                return
            self._confirm_business_tool_execution(tc_id)
            return

        if event_type == "message_discarded":
            discarded = _extract_discarded_ids(data)
            if discarded:
                self._discarded_ids.update(discarded)
                self._segments = [
                    seg for seg in self._segments if seg[0] not in discarded
                ]
                self._chain_ids.difference_update(discarded)
                for d_id in discarded:
                    stale_tcs = self._pending_tool_calls_by_msg.pop(d_id, None)
                    if stale_tcs:
                        self._pending_tool_call_ids.difference_update(stale_tcs)
                if self._active_msg_id in discarded:
                    self._active_msg_id = (
                        self._segments[-1][0] if self._segments else None
                    )
            return

        if event_type not in ("message_chunk", "text", "content_block_delta", "message"):
            return

        if data.get("role") == "user":
            return

        content_type = data.get("content_type")
        if isinstance(content_type, str) and content_type != "text":
            return

        raw_id = data.get("id")
        msg_id = raw_id if isinstance(raw_id, str) and raw_id else None
        raw_images = data.get("images")
        if isinstance(raw_images, Mapping):
            if msg_id:
                _ingest_image_map(self._images_by_message.setdefault(msg_id, {}), raw_images)
            else:
                _ingest_image_map(self._image_url_map, raw_images)

        if data.get("phase") == "commentary":
            return

        if msg_id and msg_id in self._discarded_ids:
            return

        delta = ""
        for field in ("content", "text", "delta"):
            val = data.get(field)
            if isinstance(val, str):
                delta = val
                break

        raw_cont = data.get(CONTINUED_FROM_KWARG)
        continued_from = raw_cont if isinstance(raw_cont, str) and raw_cont else None

        if delta:
            if event_type == "message":
                if continued_from and continued_from in self._chain_ids:
                    self._segments.append([msg_id, delta])
                    if msg_id:
                        self._chain_ids.add(msg_id)
                        self._active_msg_id = msg_id
                else:
                    self._segments = [[msg_id, delta]]
                    self._chain_ids = {msg_id} if msg_id else set()
                    self._active_msg_id = msg_id
            elif msg_id is not None:
                if self._segments and self._active_msg_id == msg_id:
                    self._segments[-1][1] += delta
                elif continued_from and continued_from in self._chain_ids:
                    self._segments.append([msg_id, delta])
                    self._chain_ids.add(msg_id)
                    self._active_msg_id = msg_id
                else:
                    self._segments = [[msg_id, delta]]
                    self._chain_ids = {msg_id}
                    self._active_msg_id = msg_id
            else:
                if self._segments and self._active_msg_id is None:
                    self._segments[-1][1] += delta
                else:
                    self._segments.append([None, delta])
                    self._active_msg_id = None

    def result(self) -> DeliverableExtractionResult:
        raw = "".join(seg[1] for seg in self._segments)
        cleaned = _clean_visible_text(raw)
        if not cleaned:
            return DeliverableExtractionResult(
                text="",
                saw_business_tool_call=self.saw_business_tool_call,
            )
        if self._image_url_map or self._images_by_message:
            drop = len(raw) - len(cleaned) if raw.endswith(cleaned) else 0
            resolved_parts: list[str] = []
            for seg_id, seg_text in self._segments:
                if drop > 0:
                    if drop >= len(seg_text):
                        drop -= len(seg_text)
                        continue
                    piece = seg_text[drop:]
                    drop = 0
                else:
                    piece = seg_text
                if not piece:
                    continue
                own = self._images_by_message.get(seg_id) if seg_id else None
                seg_map = (
                    {**self._image_url_map, **own}
                    if own
                    else self._image_url_map
                )
                resolved_parts.append(
                    resolve_image_paths(piece, seg_map) if seg_map else piece
                )
            cleaned = "".join(resolved_parts)
        return DeliverableExtractionResult(
            text=cleaned,
            saw_business_tool_call=self.saw_business_tool_call,
        )


def extract_deliverable_text_from_events(
    events: Iterable[Mapping[str, Any] | tuple[str, Mapping[str, Any]]],
    *,
    image_url_map: Mapping[str, str] | None = None,
    images_by_message: Mapping[str, Mapping[str, str]] | None = None,
    archive_mode: bool = False,
) -> DeliverableExtractionResult:
    """Extract final deliverable text from SSE event dicts or ``(event_type, data)`` pairs."""
    acc = DeliverableTextAccumulator(archive_mode=archive_mode)
    if image_url_map:
        acc.set_image_url_map(image_url_map)
    if images_by_message:
        acc.set_images_by_message(images_by_message)
    for item in events:
        if isinstance(item, tuple) and len(item) == 2:
            ev_type, data = item
            if isinstance(ev_type, str) and isinstance(data, Mapping):
                acc.push_event(ev_type, data)
        elif isinstance(item, Mapping):
            ev_type = item.get("event")
            data = item.get("data")
            if isinstance(ev_type, str) and isinstance(data, Mapping):
                acc.push_event(ev_type, data)
    return acc.result()


def extract_deliverable_text_from_archived_events(
    events: Iterable[Mapping[str, Any] | tuple[str, Mapping[str, Any]]],
    *,
    image_url_map: Mapping[str, str] | None = None,
) -> DeliverableExtractionResult:
    """Strict extraction for ``replay_facts.archive`` streams based on retained event fields."""
    return extract_deliverable_text_from_events(
        events,
        image_url_map=image_url_map,
        archive_mode=True,
    )


def build_channel_empty_delivery_notice(
    *,
    saw_business_tool_call: bool,
    sent_to_current_count: int,
    has_delivered_current_agent_send: bool = False,
    has_failed_current_agent_send: bool = False,
) -> str | None:
    """Return the deterministic channel notice when extracted text is empty, or None to skip."""
    if has_delivered_current_agent_send or sent_to_current_count > 0:
        return None
    if not saw_business_tool_call:
        return None
    if has_failed_current_agent_send:
        return CHANNEL_SEND_FAILED_AFTER_TOOLS_NOTICE
    return CHANNEL_EMPTY_AFTER_TOOLS_NOTICE


def resolve_automation_final_text(answer: Any) -> str | None:
    """Order automation fallback text: ``extracted_text -> RunAnswer.sent -> None`` under strict mode (§5.3)."""
    from src.dalton.settings import is_strict_deliverable_text_enabled

    ans_text = getattr(answer, "text", None)
    ans_sent = getattr(answer, "sent", None)
    has_text = isinstance(ans_text, str) and bool(ans_text.strip())
    has_sent = isinstance(ans_sent, str) and bool(ans_sent.strip())
    if is_strict_deliverable_text_enabled():
        if has_text:
            return ans_text
        if has_sent:
            return ans_sent
        return None
    return ans_sent or ans_text


def extract_delivered_send_text_from_messages(
    messages: Sequence[Any],
    delivered_delivery_keys: Iterable[str],
    *,
    run_id: str | None = None,
) -> str | None:
    """Bind the automation summary to the text of a ``send_message`` call whose
    ledger ``delivery_key`` was actually delivered (``status IN ('sent', 'partial')``)."""
    keys = {str(k).strip() for k in delivered_delivery_keys if isinstance(k, str) and k.strip()}
    if not keys:
        return None
    cleaned_rid = str(run_id or "").strip()
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        for call in reversed(getattr(msg, "tool_calls", None) or ()):
            if not isinstance(call, dict) or call.get("name") != "send_message":
                continue
            tc_id = str(call.get("id") or "").strip()
            if not tc_id:
                continue
            matched = (
                (bool(cleaned_rid) and f"{cleaned_rid}:{tc_id}" in keys)
                or tc_id in keys
                or any(k.endswith(f":{tc_id}") for k in keys)
            )
            if not matched:
                continue
            args = call.get("args")
            text = args.get("text") if isinstance(args, dict) else None
            if isinstance(text, str) and text.strip():
                return text
    return None


def resolve_automation_empty_excerpt(
    *,
    extracted_text: str | None,
    sent_text: str | None,
    has_delivered_agent_send: bool,
    has_delivered_files_only: bool = False,
    has_delivered_text_agent_send: bool | None = None,
    delivered_sent_text: str | None = None,
    saw_business_tool_call: bool = False,
) -> str | None:
    """Resolve the automation ``result_excerpt`` source text when ``extracted_text`` is empty (§5.3).

    - When ``extracted_text`` is non-empty, return it as-is.
    - When ``has_delivered_agent_send`` is True:
      - If only attachments were delivered (``has_delivered_text_agent_send is False``),
        return ``AUTOMATION_FILES_ONLY_EXCERPT`` (never surface an unrelated failed text send).
      - If text was delivered (``has_delivered_text_agent_send is True``), prefer
        ``delivered_sent_text`` (bound to the succeeded ledger record), else ``sent_text``
        when unbound, else ``AUTOMATION_TEXT_DELIVERED_EXCERPT`` (never claim files-only).
      - Legacy callers omitting ``has_delivered_text_agent_send``: return ``sent_text`` if
        non-empty, else ``AUTOMATION_FILES_ONLY_EXCERPT`` when ``has_delivered_files_only``
        else ``AUTOMATION_TEXT_DELIVERED_EXCERPT``.
    - Otherwise (not delivered): return ``AUTOMATION_EMPTY_AFTER_TOOLS_SUMMARY`` when business
      tools ran or a send was attempted without delivery.
    """
    if isinstance(extracted_text, str) and extracted_text.strip():
        return extracted_text
    if has_delivered_agent_send:
        if has_delivered_text_agent_send is False:
            return AUTOMATION_FILES_ONLY_EXCERPT
        if has_delivered_text_agent_send is True:
            if isinstance(delivered_sent_text, str) and delivered_sent_text.strip():
                return delivered_sent_text
            # Never fall back to unverified RunAnswer.sent when ledger binding failed
            # or could not confirm which send_message call succeeded.
            return AUTOMATION_TEXT_DELIVERED_EXCERPT
        if isinstance(sent_text, str) and sent_text.strip():
            return sent_text
        if has_delivered_files_only:
            return AUTOMATION_FILES_ONLY_EXCERPT
        return AUTOMATION_TEXT_DELIVERED_EXCERPT
    if saw_business_tool_call or (isinstance(sent_text, str) and sent_text.strip()):
        return AUTOMATION_EMPTY_AFTER_TOOLS_SUMMARY
    return None


async def load_span_replay_responses(
    spans: Mapping[str, Any],
    loader: Any,
) -> Mapping[str, Any]:
    """Load ``conversation_responses`` rows only for spans whose assistant text
    references a sandbox image path, avoiding extra DB reads on text-only turns."""
    needing: list[str] = []
    for response_id, span in spans.items():
        for msg in getattr(span, "messages", None) or ():
            if not isinstance(msg, AIMessage):
                continue
            text = _extract_ai_message_deliverable_text(msg)
            if text and contains_unresolved_sandbox_image_path(text):
                needing.append(response_id)
                break
    if not needing:
        return {}
    return await loader(needing)


def apply_span_image_urls(
    items: list[dict[str, Any]],
    span: Any,
    response: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply replay's sandbox image URL mapping to projected turn items so
    thread output resolves sandbox image paths identically to replay (§5.3)."""
    from src.server.services.history.replay import facts as replay_facts
    from src.server.services.history.replay.turn import _collect_image_url_map

    ui_records = getattr(span, "new_ui_records", ()) or ()
    resp_facts = (response or {}).get("replay_facts") if isinstance(response, Mapping) else None
    url_map = {
        **_collect_image_url_map(ui_records),
        **replay_facts.images_of(resp_facts),
    }
    return replay_facts.apply_image_url_map(
        items,
        url_map,
        by_message=replay_facts.recorded_by_message(getattr(span, "messages", ()) or ()),
    )


def _continuation_lane_key(agent_name: str | None) -> str:
    """Normalize main-agent node names (e.g. ``model`` / ``model:...`` / ``agent``) to ``main``."""
    return "main" if is_main_agent_lane(agent_name) else str(agent_name)


def resolve_live_continuation_id(
    pending_by_agent: dict[str, str],
    bound_by_msg: dict[tuple[str, str], str],
    agent_name: str,
    message_id: str | None,
    message_chunk: Any,
) -> str | None:
    """Resolve and latch ``continued_from_message_id`` for a live SSE chunk."""
    lane = _continuation_lane_key(agent_name)
    if message_id:
        existing = bound_by_msg.get((lane, message_id))
        if existing:
            return existing
        pending = pending_by_agent.get(lane)
        if pending and message_id != pending:
            pending_by_agent.pop(lane, None)
            bound_by_msg[(lane, message_id)] = pending
            return pending
    kwargs = getattr(message_chunk, "additional_kwargs", None) or {}
    kw_cont = kwargs.get(CONTINUED_FROM_KWARG)
    if isinstance(kw_cont, str) and kw_cont:
        if message_id:
            bound_by_msg[(lane, message_id)] = kw_cont
        return kw_cont
    return None


_RUN_TURN_MSG_CACHE_MAX = 256
_RUN_TURN_MSG_CACHE_LOCK = threading.Lock()
_RUN_TURN_MSG_CACHE: OrderedDict[tuple[str, str], list[Any]] = OrderedDict()

_AGENT_SEND_IMAGE_CTX_MAX = 512
_AGENT_SEND_IMAGE_CTX_LOCK = threading.Lock()
_AGENT_SEND_IMAGE_CTX: OrderedDict[tuple[str, str], tuple[str, dict[str, str]]] = OrderedDict()


def record_agent_send_image_context(
    run_id: str | None,
    message: Any,
    captured: Mapping[str, str] | None = None,
) -> None:
    """Record ``(source_message_id, captured_images)`` at the moment a model message
    containing ``send_message`` tool call(s) is committed, so ``:imgfix`` always uses
    that delivery's capture rather than a later overwrite of the same sandbox path."""
    rid = str(run_id or "").strip()
    if not rid or not isinstance(message, AIMessage):
        return
    msg_id = str(getattr(message, "id", "") or "").strip()
    if not msg_id:
        return
    tool_calls = getattr(message, "tool_calls", None) or ()
    send_ids = [
        str(tc.get("id")).strip()
        for tc in tool_calls
        if isinstance(tc, Mapping)
        and tc.get("name") == "send_message"
        and isinstance(tc.get("id"), str)
        and tc.get("id", "").strip()
    ]
    if not send_ids:
        return
    raw_captured = captured if captured is not None else recorded_images(message)
    clean_captured = {
        str(k): str(v)
        for k, v in (raw_captured or {}).items()
        if isinstance(k, str) and k and isinstance(v, str) and v
    }
    with _AGENT_SEND_IMAGE_CTX_LOCK:
        for tc_id in send_ids:
            key = (rid, tc_id)
            _AGENT_SEND_IMAGE_CTX[key] = (msg_id, dict(clean_captured))
            _AGENT_SEND_IMAGE_CTX.move_to_end(key)
        while len(_AGENT_SEND_IMAGE_CTX) > _AGENT_SEND_IMAGE_CTX_MAX:
            _AGENT_SEND_IMAGE_CTX.popitem(last=False)


def lookup_agent_send_image_context(
    run_id: str | None,
    tool_call_id: str | None,
) -> tuple[str, dict[str, str]] | None:
    """Look up ``(source_message_id, captured_images)`` recorded for ``(run_id, tool_call_id)``."""
    rid = str(run_id or "").strip()
    tcid = str(tool_call_id or "").strip()
    if not rid or not tcid:
        return None
    with _AGENT_SEND_IMAGE_CTX_LOCK:
        entry = _AGENT_SEND_IMAGE_CTX.get((rid, tcid))
        if entry is None:
            return None
        return entry[0], dict(entry[1])


async def alookup_agent_send_image_context(
    thread_id: str | None,
    run_id: str | None,
    tool_call_id: str | None,
) -> tuple[str, dict[str, str]] | None:
    """Async lookup checking the in-process cache first, then the run's checkpoint turn."""
    cached = lookup_agent_send_image_context(run_id, tool_call_id)
    if cached is not None:
        return cached
    tid = str(thread_id or "").strip()
    rid = str(run_id or "").strip()
    tcid = str(tool_call_id or "").strip()
    if not tid or not rid or not tcid:
        return None
    try:
        from src.server.services.history.reader import CheckpointHistoryReader

        turn = await CheckpointHistoryReader.get_instance().aget_run_turn(tid, rid)
        if turn is None:
            return None
        for msg in reversed(getattr(turn, "messages", None) or ()):
            if not isinstance(msg, AIMessage) or not getattr(msg, "id", None):
                continue
            for tc in getattr(msg, "tool_calls", None) or ():
                if (
                    isinstance(tc, Mapping)
                    and tc.get("name") == "send_message"
                    and str(tc.get("id") or "").strip() == tcid
                ):
                    captured = recorded_images(msg)
                    record_agent_send_image_context(rid, msg, captured)
                    return str(msg.id), captured
    except Exception:
        return None
    return None


def cache_run_turn_messages(
    thread_id: str | None,
    run_id: str | None,
    messages: Sequence[Any],
) -> None:
    """Cache the first checkpoint read's messages so automation excerpt binding
    reuses them without a second checkpoint read."""
    tid = str(thread_id or "").strip()
    rid = str(run_id or "").strip()
    if not tid or not rid:
        return
    with _RUN_TURN_MSG_CACHE_LOCK:
        _RUN_TURN_MSG_CACHE[(tid, rid)] = list(messages)
        _RUN_TURN_MSG_CACHE.move_to_end((tid, rid))
        while len(_RUN_TURN_MSG_CACHE) > _RUN_TURN_MSG_CACHE_MAX:
            _RUN_TURN_MSG_CACHE.popitem(last=False)


def _take_cached_run_turn_messages(
    thread_id: str | None,
    run_id: str | None,
) -> list[Any] | None:
    tid = str(thread_id or "").strip()
    rid = str(run_id or "").strip()
    if not tid or not rid:
        return None
    with _RUN_TURN_MSG_CACHE_LOCK:
        return _RUN_TURN_MSG_CACHE.pop((tid, rid), None)


async def resolve_completed_automation_excerpt_from_db(
    execution_id: str,
    *,
    answer: Any,
    excerpt: str | None,
    thread_id: str | None = None,
    run_id: str | None = None,
    turn_messages: Sequence[Any] | None = None,
) -> str | None:
    """Populate automation ``result_excerpt`` from ``dalton_channel_deliveries`` when
    ``extracted_text`` is empty (§5.3 item 5)."""
    from src.dalton.settings import is_strict_deliverable_text_enabled
    from src.server.services.automation_excerpt import plain_excerpt

    if not is_strict_deliverable_text_enabled():
        return excerpt
    if excerpt:
        return excerpt
    answer_text = getattr(answer, "text", None)
    if isinstance(answer_text, str) and answer_text.strip():
        return plain_excerpt(answer_text) or None

    has_delivered = False
    has_delivered_text: bool | None = None
    has_files_only = False
    delivered_text_keys: list[str] = []
    try:
        from src.dalton.database.channel_gateway import (
            get_execution_agent_send_delivery_status,
        )

        status = await get_execution_agent_send_delivery_status(execution_id)
        has_delivered = bool(status.get("has_delivered_agent_send"))
        has_files_only = bool(status.get("has_delivered_files_only"))
        if "has_delivered_text_agent_send" in status:
            has_delivered_text = bool(status["has_delivered_text_agent_send"])
        raw_keys = status.get("delivered_text_delivery_keys")
        if isinstance(raw_keys, (list, tuple)):
            delivered_text_keys = [str(k) for k in raw_keys if k]
    except Exception:
        has_delivered = False
        has_delivered_text = None
        has_files_only = False

    delivered_sent_text: str | None = None
    if has_delivered and has_delivered_text and delivered_text_keys:
        msgs: Sequence[Any] | None = turn_messages
        if msgs is None and thread_id and run_id:
            msgs = _take_cached_run_turn_messages(thread_id, run_id)
        if msgs is None and thread_id and run_id:
            try:
                from src.server.services.history.reader import CheckpointHistoryReader

                turn = await CheckpointHistoryReader.get_instance().aget_run_turn(thread_id, run_id)
                if turn is not None:
                    msgs = turn.messages
            except Exception:
                msgs = None
        if msgs is not None:
            matched = extract_delivered_send_text_from_messages(
                msgs,
                delivered_text_keys,
                run_id=run_id,
            )
            delivered_sent_text = matched if matched is not None else ""
        else:
            delivered_sent_text = ""

    resolved = resolve_automation_empty_excerpt(
        extracted_text=answer_text,
        sent_text=getattr(answer, "sent", None),
        has_delivered_agent_send=has_delivered,
        has_delivered_files_only=has_files_only,
        has_delivered_text_agent_send=has_delivered_text,
        delivered_sent_text=delivered_sent_text,
        saw_business_tool_call=True,
    )
    return (plain_excerpt(resolved) or None) if resolved else None
