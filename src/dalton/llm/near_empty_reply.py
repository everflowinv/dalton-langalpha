"""Near-empty reply detection, retry budget control, and retry-dedup helpers (§6).

Defers all safety-refusal replies directly to HF2's
``src.dalton.middleware.refusal_fallback.is_refusal_reply`` so refusal retries
and notices remain untouched.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import threading
import unicodedata
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

from ptc_agent.agent.transcript.classify import human_kind
from ptc_agent.agent.transcript.render import visible_text
from src.dalton.channels.deliverable_text import (
    CONTINUED_FROM_KWARG,
    RETRIED_ATTEMPT_KWARG,
    _extract_ai_message_deliverable_text,
)
from src.dalton.channels.sse_control import EXHAUSTED_NOTICE_KWARG
from src.dalton.middleware.refusal_fallback import (
    handle_refusal_exhausted,
    is_refusal_reply,
)
from src.dalton.settings import is_near_empty_retry_enabled
from src.server.utils.text_phase import block_phase

#: Shared retry sub-budget for non-refusal ``empty_reply`` + ``near_empty_reply`` (§6.3).
EMPTY_OR_NEAR_EMPTY_MAX_RETRIES = 1

#: Maximum visible length for zero-semantic symbol/punctuation fragments (§6.3).
MAX_SYMBOL_ONLY_LEN = 16

#: Maximum visible length for isolated single-semantic-char fragments (§6.3).
MAX_SINGLE_SEMANTIC_LEN = 3

#: Short user input length threshold for Exemption A (§6.3).
SHORT_USER_INPUT_MAX_LEN = 8

#: Retry hint injected when the previous reply was a near-empty fragment (§6.3).
NEAR_EMPTY_REPLY_HINT = (
    "上一条回复仅包含极短残片，尚未回答用户问题或发起工具调用。"
    "请根据用户请求，直接输出完整回答或调用所需工具。"
)

#: User-facing failure notice when near-empty retries are exhausted (§6.3).
NEAR_EMPTY_REPLY_NOTICE = (
    "这一轮模型只输出了异常的极短片段，未能生成完整回复。"
    "如果本轮包含早期操作，请先核对结果，再决定是否重新发送。"
)

_EXPLICIT_DELIVERY_TOOLS = frozenset(
    {
        "send_message",
        "show_widget",
        "ShowWidget",
        "dalton_monitor",
        "schedule_wakeup",
        "delegate_to_analyst",
    }
)

_EXEMPT_BLOCK_TYPES = frozenset(
    {
        "compaction",
        "image",
        "image_url",
        "file",
        "document",
        "audio",
        "video",
        "tool_use",
        "function_call",
        "functionCall",
    }
)

_VALID_SINGLE_CHAR_WORDS = frozenset(
    {"是", "否", "对", "好", "行", "可", "有", "无", "在", "中", "Y", "N"}
)

#: Common single-emoji acknowledgements/reactions treated as valid short replies.
_VALID_REACTION_EMOJIS = frozenset(
    {
        "👍",
        "👎",
        "✅",
        "✔",
        "☑",
        "❌",
        "🙏",
        "👌",
        "🤝",
        "👏",
        "🙌",
        "💪",
        "🎉",
        "🔥",
        "💯",
        "❤",
        "🧡",
        "💛",
        "💚",
        "💙",
        "💜",
        "🫶",
        "🫡",
        "😊",
        "😄",
        "😃",
        "😁",
        "😉",
        "😎",
        "🥳",
        "🤔",
        "👀",
        "😅",
        "😂",
        "🤣",
        "🙂",
        "😌",
        "🤩",
        "✨",
        "⭐",
        "🌟",
        "🚀",
        "💡",
        "📌",
        "📝",
        "☕",
        "🍵",
    }
)

_SINGLE_CHAR_INTENT_PHRASES = (
    "是或否",
    "对不对",
    "好不好",
    "行不行",
    "只回",
    "只需回复",
    "只输出",
    "一个字",
    "单字",
    "yes or no",
    "true or false",
    "reply with one",
    "one word",
)

_OPTION_INTENT_RE = re.compile(r"(?:请选|选择|回复|输出)\s*[A-Da-d1-4]", re.IGNORECASE)

_SYMBOL_REQUEST_RE = re.compile(
    r"(?:符号|标点|分隔|框线|画线|分割线|横线|emoji|symbol|punctuation|separator|divider)",
    re.IGNORECASE,
)

_CALC_OR_COUNT_RE = re.compile(
    r"(?:几个|多少|几条|几次|几家|几只|几名|几项|几步|等于|计算|算一下|加|减|乘|除|"
    r"how\s+many|count|calculate|sum\s+of|what\s+is\s+\d|"
    r"\d\s*[+\-*/×÷=]\s*\d)",
    re.IGNORECASE,
)

_OPTION_LIST_RE = re.compile(r"(?:^|[\s\n（(])([A-D])[.、:：)）]", re.MULTILINE)

_STRIP_EDGE_CHARS = " \t\n\r。！？!?.,，、:：;；()（）[]【】<>《》\"'“”‘’`"

_SEND_STATUS_LINE_RE = re.compile(r"(?im)^\s*status\s*:\s*([a-z_]+)\b")

_DELIVERED_SEND_STATUSES = frozenset({"sent", "partial"})

_SANITIZED_KINDS = frozenset({"output_cap", "malformed_call"})

_SKIP_SANITIZE_BLOCK_TYPES = frozenset(
    {
        "tool_use",
        "function_call",
        "functionCall",
        "thinking",
        "reasoning",
        "redacted_thinking",
        "compaction",
        "image",
        "image_url",
        "file",
        "document",
        "audio",
        "video",
    }
)

#: Persisted checkpoint/DB metadata key for retry-generated ``send_message`` dedup.
RETRY_SEND_DEDUP_KWARG = "dalton_retry_send_dedup"

# Bounded process-local cache mapping (run_id, tool_call_id) -> RetrySendDedupInfo
# backed by checkpoint additional_kwargs + conversation_responses.metadata in Postgres.
_RETRY_SEND_DEDUP_MAX = 2048
_RETRY_SEND_DEDUP_LOCK = threading.Lock()
_RETRY_SEND_DEDUP_REGISTRY: OrderedDict[tuple[str, str], RetrySendDedupInfo] = OrderedDict()


@dataclass(frozen=True)
class RetrySendDedupInfo:
    """Idempotency metadata attached to a retry-generated ``send_message`` call."""

    delivery_key: str | None = None
    retry_dedup_by_target: bool = True
    payload_digest: str | None = None


def _normalize_workspace_id(val: Any) -> str:
    """Normalize a workspace identifier identically to ``deliverable-text.ts``."""
    if not isinstance(val, str):
        return ""
    s = val.strip()
    if not s:
        return ""
    try:
        return str(uuid.UUID(s))
    except ValueError:
        return s.lower()


def compute_send_payload_digest(
    text: Any,
    files: Any = None,
    *,
    default_workspace_id: Any = None,
) -> str:
    """Canonical SHA-256 digest of a ``send_message`` payload's text and attachments.

    Identifies each attachment by its resolved ``(workspace_id, path)`` pair so
    two same-named files from different workspaces never collide. Normalizes both
    string file paths (from ``AIMessage.tool_calls``) and file-ref dicts
    ``{"path": ..., "workspace_id": ...}`` (from ``tools.py`` / gateway) with the
    same sorting and JSON serialization as ``deliverable-text.ts``.
    """
    norm_text = text.strip() if isinstance(text, str) else ""
    fallback_ws = _normalize_workspace_id(default_workspace_id)
    norm_files: list[dict[str, str]] = []
    if isinstance(files, (list, tuple)):
        for item in files:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    norm_files.append({"path": s, "workspace_id": fallback_ws})
            elif isinstance(item, dict):
                raw_path = item.get("path") or item.get("file_path") or ""
                if isinstance(raw_path, str) and raw_path.strip():
                    item_ws = _normalize_workspace_id(item.get("workspace_id")) or fallback_ws
                    norm_files.append({"path": raw_path.strip(), "workspace_id": item_ws})
    norm_files.sort(key=lambda entry: (entry["workspace_id"], entry["path"]))
    canonical = json.dumps(
        {"files": norm_files, "text": norm_text},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_send_target(target: Any) -> str:
    if not isinstance(target, str):
        return ""
    return target.strip().lower()


def _is_explicit_delivery_tool(name: str | None) -> bool:
    if not isinstance(name, str) or not name:
        return False
    if name in _EXPLICIT_DELIVERY_TOOLS:
        return True
    return name.startswith("dalton_monitor")


def _has_exempt_content_block(ai_msg: AIMessage) -> bool:
    content = ai_msg.content
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in _EXEMPT_BLOCK_TYPES or "functionCall" in block:
            return True
    return False


def _turn_start_index(messages: Sequence[Any]) -> int:
    """Return index of the current turn's boundary message (or 0 if none found)."""
    from src.tools.wakeups.quiet import _last_boundary

    if not messages:
        return 0
    idx = _last_boundary(messages)
    return max(idx, 0)


def _extract_send_message_business_status(msg: ToolMessage) -> str | None:
    """Read the business delivery status (``sent``/``partial``/``failed``/``unknown``)
    from a ``send_message`` ``ToolMessage``."""
    artifact = getattr(msg, "artifact", None)
    if not isinstance(artifact, dict):
        kwargs = getattr(msg, "additional_kwargs", None) or {}
        if isinstance(kwargs.get("artifact"), dict):
            artifact = kwargs["artifact"]
    if isinstance(artifact, dict) and isinstance(artifact.get("status"), str):
        val = artifact["status"].strip().lower()
        if val:
            return val

    content = getattr(msg, "content", None)
    if isinstance(content, dict) and isinstance(content.get("status"), str):
        val = content["status"].strip().lower()
        if val:
            return val

    text = content if isinstance(content, str) else visible_text(content, attachments=False)
    if isinstance(text, str) and text.strip():
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict) and isinstance(parsed.get("status"), str):
                    val = parsed["status"].strip().lower()
                    if val:
                        return val
            except Exception:
                pass
        match = _SEND_STATUS_LINE_RE.search(stripped)
        if match:
            return match.group(1).strip().lower()
    return None


def _is_send_message_delivered(msg: ToolMessage) -> bool:
    """True only when ``send_message`` achieved actual business delivery (``sent``/``partial``)."""
    if getattr(msg, "status", None) == "error":
        return False
    biz_status = _extract_send_message_business_status(msg)
    if biz_status is not None:
        return biz_status in _DELIVERED_SEND_STATUSES
    # Unstructured legacy test stub without status field: reject obvious failure texts.
    content = getattr(msg, "content", None)
    text = content if isinstance(content, str) else ""
    lowered = text.lower()
    if any(tok in lowered for tok in ("nothing was sent", "failed", "not_allowed", "unavailable")):
        return False
    return True


def _has_completed_delivery_tool_in_turn(turn_messages: Sequence[Any]) -> bool:
    """Global pre-exemption (§6.3): True if an explicit delivery/orchestration tool
    actually succeeded after the current turn start."""
    if not turn_messages:
        return False
    start = _turn_start_index(turn_messages)
    window = turn_messages[start:]

    tool_names_by_id: dict[str, str] = {}
    for msg in window:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or ():
                if isinstance(tc, dict) and tc.get("id") and tc.get("name"):
                    tool_names_by_id[str(tc["id"])] = str(tc["name"])

    for msg in window:
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "status", None) == "error":
            continue
        name = getattr(msg, "name", None) or tool_names_by_id.get(
            str(getattr(msg, "tool_call_id", "") or "")
        )
        if not _is_explicit_delivery_tool(name):
            continue
        if name == "send_message":
            if _is_send_message_delivered(msg):
                return True
            continue
        return True
    return False


def _latest_user_prompt(turn_messages: Sequence[Any]) -> str:
    """Read the latest real user prompt (or user steer) from full turn state."""
    for msg in reversed(turn_messages):
        if not isinstance(msg, HumanMessage):
            continue
        kind = human_kind(msg)
        if kind in (
            "empty-call-retry",
            "runtime-context",
            "market-watch",
            "credit-gate",
            "orchestrator",
            "summarization",
        ):
            continue
        text = visible_text(msg.content, attachments=False)
        if kind == "steering" and "\n" in text:
            text = text.split("\n", 1)[1]
        text = text.strip()
        if text:
            return text
    return ""


def _semantic_chars(text: str) -> list[str]:
    """Return letter (L*) and number (N*) characters in ``text``."""
    return [
        ch
        for ch in text
        if unicodedata.category(ch)[:1] in ("L", "N")
    ]


def _is_valid_reaction_emoji_reply(clean_text: str) -> bool:
    """True when ``clean_text`` is a single common reaction/acknowledgement emoji."""
    stripped = clean_text.strip()
    if not stripped:
        return False
    # Remove variation selectors, zero-width joiners, and Fitzpatrick skin tone modifiers.
    normalized = "".join(
        ch
        for ch in stripped
        if ch not in ("\ufe0e", "\ufe0f", "\u200d")
        and not (0x1F3FB <= ord(ch) <= 0x1F3FF)
    )
    return normalized in _VALID_REACTION_EMOJIS


def _strip_single_edge_punct(text: str) -> str:
    s = text.strip()
    if len(s) >= 2 and s[0] in _STRIP_EDGE_CHARS:
        s = s[1:]
    if len(s) >= 2 and s[-1] in _STRIP_EDGE_CHARS:
        s = s[:-1]
    return s.strip()


def _user_explicitly_requested_symbol(user_prompt: str, clean_text: str) -> bool:
    if not user_prompt:
        return False
    compact = "".join(user_prompt.split())
    if len(compact) > MAX_SYMBOL_ONLY_LEN:
        return False
    if _SYMBOL_REQUEST_RE.search(user_prompt):
        return True
    if clean_text in user_prompt and any(
        kw in user_prompt.lower()
        for kw in ("输出", "回复", "打印", "返回", "echo", "output", "reply", "print")
    ):
        return True
    return False


def _matches_single_char_exemption_a(user_prompt: str) -> bool:
    if not user_prompt:
        return False
    compact = "".join(user_prompt.split())
    if 0 < len(compact) <= SHORT_USER_INPUT_MAX_LEN:
        return True
    lowered = user_prompt.lower()
    if any(phrase in lowered for phrase in _SINGLE_CHAR_INTENT_PHRASES):
        return True
    if _OPTION_INTENT_RE.search(user_prompt):
        return True
    return False


def _matches_single_char_exemption_b(clean_text: str, user_prompt: str) -> bool:
    core = _strip_single_edge_punct(clean_text)
    if core in _VALID_SINGLE_CHAR_WORDS or core.upper() in ("Y", "N"):
        return True
    if len(core) == 1 and core in "0123456789":
        if clean_text.endswith("."):
            return False
        if user_prompt and _CALC_OR_COUNT_RE.search(user_prompt):
            return True
    if len(core) == 1 and core.upper() in ("A", "B", "C", "D"):
        if user_prompt and len(set(_OPTION_LIST_RE.findall(user_prompt))) >= 2:
            return True
    return False


def deliverable_text_of_ai_message(ai_msg: AIMessage) -> str:
    """Return the non-commentary, opening-thinking-stripped deliverable text of ``ai_msg``."""
    return _extract_ai_message_deliverable_text(ai_msg, resolve_images=False)


def is_deliverable_reply_empty(ai_msg: AIMessage) -> bool:
    """True when ``ai_msg`` has no tool calls, no compaction block, and no deliverable text."""
    if getattr(ai_msg, "tool_calls", None) or getattr(ai_msg, "invalid_tool_calls", None):
        return False
    content = ai_msg.content
    if isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "compaction" for block in content
    ):
        return False
    return not deliverable_text_of_ai_message(ai_msg).strip()


def is_near_empty_reply(ai_msg: AIMessage, turn_messages: Sequence[Any]) -> bool:
    """Return True when ``ai_msg`` is a non-refusal, non-tool near-empty fragment (§6.3).

    Evaluates strictly the deliverable (non-``commentary``) text of ``ai_msg``.
    """
    # First line: always defer safety refusals to HF2's empty_reply / refusal branch.
    if is_refusal_reply(ai_msg):
        return False
    if not is_near_empty_retry_enabled():
        return False
    if getattr(ai_msg, "tool_calls", None) or getattr(ai_msg, "invalid_tool_calls", None):
        return False
    if _has_exempt_content_block(ai_msg):
        return False

    clean_text = deliverable_text_of_ai_message(ai_msg).strip()
    if not clean_text:
        return False

    # Global pre-exemption: an explicit delivery/orchestration tool actually succeeded this turn.
    if _has_completed_delivery_tool_in_turn(turn_messages):
        return False

    sem = _semantic_chars(clean_text)

    # Category 1: zero-semantic symbol/punctuation fragment within 1..16 chars.
    if len(sem) == 0:
        if _is_valid_reaction_emoji_reply(clean_text):
            return False
        if 1 <= len(clean_text) <= MAX_SYMBOL_ONLY_LEN:
            user_prompt = _latest_user_prompt(turn_messages)
            if _user_explicitly_requested_symbol(user_prompt, clean_text):
                return False
            return True
        return False

    # Category 2: isolated single semantic char within <= 3 chars.
    if len(sem) == 1 and len(clean_text) <= MAX_SINGLE_SEMANTIC_LEN:
        user_prompt = _latest_user_prompt(turn_messages)
        if _matches_single_char_exemption_a(user_prompt):
            return False
        if _matches_single_char_exemption_b(clean_text, user_prompt):
            return False
        return True

    return False


def is_non_refusal_empty_like(kind: str | None, ai_msg: Any) -> bool:
    """True when ``kind`` draws from the shared 1-retry non-refusal empty-like sub-budget."""
    return kind in ("empty_reply", "near_empty_reply") and not is_refusal_reply(ai_msg)


def sanitize_output_cap_reply(ai_msg: AIMessage) -> AIMessage | None:
    """Strip tool/thinking/attachment blocks from an output_cap/malformed_call reply
    while preserving block-level ``phase`` (so ``commentary`` is never flattened into
    deliverable text on replay or checkpoint extraction)."""
    content = ai_msg.content
    cont_from = (getattr(ai_msg, "additional_kwargs", None) or {}).get(CONTINUED_FROM_KWARG)
    extra_kwargs = {CONTINUED_FROM_KWARG: cont_from} if isinstance(cont_from, str) and cont_from else {}

    if isinstance(content, list):
        kept_blocks: list[dict[str, Any]] = []
        saw_phase = False
        for block in content:
            if isinstance(block, str):
                if block:
                    kept_blocks.append({"type": "text", "text": block})
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in _SKIP_SANITIZE_BLOCK_TYPES or "functionCall" in block:
                continue
            if btype == "text" or (btype is None and isinstance(block.get("text"), str)):
                btext = block.get("text")
                if not isinstance(btext, str) or not btext:
                    continue
                phase = block_phase(block)
                if phase:
                    saw_phase = True
                    kept_blocks.append({"type": "text", "text": btext, "phase": phase})
                else:
                    kept_blocks.append({"type": "text", "text": btext})

        combined_text = "".join(b["text"] for b in kept_blocks)
        if not combined_text.strip():
            return None
        sanitized_content: Any = kept_blocks if saw_phase else combined_text
        return AIMessage(
            content=sanitized_content,
            id=ai_msg.id,
            additional_kwargs=extra_kwargs,
            response_metadata=dict(getattr(ai_msg, "response_metadata", None) or {}),
            usage_metadata=getattr(ai_msg, "usage_metadata", None),
        )

    text = visible_text(content, attachments=False)
    if not text.strip():
        return None
    return AIMessage(
        content=text,
        id=ai_msg.id,
        additional_kwargs=extra_kwargs,
        response_metadata=dict(getattr(ai_msg, "response_metadata", None) or {}),
        usage_metadata=getattr(ai_msg, "usage_metadata", None),
    )


def last_prefix_message_id(prefix: Sequence[Any]) -> str | None:
    """Return the ``AIMessage.id`` of the last valid deliverable prefix segment."""
    for msg in reversed(prefix):
        if not isinstance(msg, AIMessage):
            continue
        kwargs = getattr(msg, "additional_kwargs", None) or {}
        if kwargs.get(RETRIED_ATTEMPT_KWARG):
            continue
        if not deliverable_text_of_ai_message(msg).strip():
            continue
        msg_id = getattr(msg, "id", None)
        if isinstance(msg_id, str) and msg_id:
            return msg_id
    return None


def emit_main_stream_event(event: dict[str, Any], *, is_subagent: bool) -> None:
    """Best-effort emission of an internal custom stream event for the main agent."""
    if is_subagent:
        return
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if callable(writer):
            writer(event)
    except Exception:
        return


def emit_attempt_start(attempt: int, *, is_subagent: bool) -> None:
    """Emit ``attempt_start`` before each model attempt so live SSE correlates
    streamed chunk ids strictly to the active attempt."""
    emit_main_stream_event(
        {"type": "attempt_start", "agent": "main", "attempt": attempt},
        is_subagent=is_subagent,
    )


def emit_message_discarded(ai_msg: Any, *, is_subagent: bool) -> None:
    """Emit ``message_discarded`` custom event when a streamed fragment is retracted."""
    msg_id = getattr(ai_msg, "id", None)
    if isinstance(msg_id, str) and msg_id:
        emit_main_stream_event(
            {"type": "message_discarded", "agent": "main", "id": msg_id},
            is_subagent=is_subagent,
        )


def emit_continuation_link_id(msg_id: str | None, *, is_subagent: bool) -> str | None:
    """Emit ``continuation_link`` for a specific prefix message id."""
    if isinstance(msg_id, str) and msg_id:
        emit_main_stream_event(
            {
                "type": "continuation_link",
                "agent": "main",
                CONTINUED_FROM_KWARG: msg_id,
            },
            is_subagent=is_subagent,
        )
        return msg_id
    return None


def emit_continuation_link(sanitized_msg: Any, *, is_subagent: bool) -> str | None:
    """Emit ``continuation_link`` before invoking the output_cap continuation model call."""
    return emit_continuation_link_id(
        getattr(sanitized_msg, "id", None), is_subagent=is_subagent
    )


def emit_exhausted_notice(notice_msg: Any, *, is_subagent: bool) -> None:
    """Emit synthetic exhausted notice onto the live SSE stream as ``exhausted_notice``."""
    content = getattr(notice_msg, "content", None)
    if isinstance(content, str) and content:
        kwargs = getattr(notice_msg, "additional_kwargs", None) or {}
        payload: dict[str, Any] = {
            "type": "exhausted_notice",
            "agent": "main",
            "id": getattr(notice_msg, "id", None),
            "content": content,
        }
        cont_id = kwargs.get(CONTINUED_FROM_KWARG)
        if isinstance(cont_id, str) and cont_id:
            payload[CONTINUED_FROM_KWARG] = cont_id
        emit_main_stream_event(payload, is_subagent=is_subagent)


def stamp_continued_from(ai_msg: Any, continued_from_id: str | None) -> None:
    """Record ``continued_from_message_id`` on the continuation AIMessage in-place."""
    if not continued_from_id or not isinstance(ai_msg, AIMessage):
        return
    if ai_msg.additional_kwargs is None:
        ai_msg.additional_kwargs = {}
    ai_msg.additional_kwargs[CONTINUED_FROM_KWARG] = continued_from_id


def mark_retried_attempt(ai_msg: Any) -> None:
    """Mark a superseded non-continuation retry attempt so replay and extractors skip it."""
    if not isinstance(ai_msg, AIMessage):
        return
    if ai_msg.additional_kwargs is None:
        ai_msg.additional_kwargs = {}
    ai_msg.additional_kwargs[RETRIED_ATTEMPT_KWARG] = True


def build_and_emit_exhausted_notice(
    *,
    last: AIMessage,
    kind: str,
    prefix: Sequence[Any],
    default_notices: dict[str, str],
    additional_kwargs: dict[str, Any],
    is_subagent: bool,
) -> AIMessage:
    """Construct the synthetic exhausted notice, chain it onto the last valid prefix
    segment if present, mark it for cross-path dedup, and emit the live custom event."""
    notice_content = (
        NEAR_EMPTY_REPLY_NOTICE if kind == "near_empty_reply" else default_notices[kind]
    )
    kwargs = dict(additional_kwargs)
    kwargs[EXHAUSTED_NOTICE_KWARG] = True
    prev_id = last_prefix_message_id(prefix)
    if prev_id:
        kwargs[CONTINUED_FROM_KWARG] = prev_id
    notice = AIMessage(
        content=notice_content,
        id=f"dalton-notice-{uuid.uuid4().hex[:12]}",
        additional_kwargs=kwargs,
        usage_metadata=getattr(last, "usage_metadata", None),
        response_metadata=dict(getattr(last, "response_metadata", None) or {}),
    )
    emit_exhausted_notice(notice, is_subagent=is_subagent)
    return notice


def _extract_run_id_from_config(config: dict[str, Any] | None) -> str | None:
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable") or {}
    meta = config.get("metadata") or {}
    for candidate in (
        configurable.get("run_id"),
        meta.get("run_id"),
        config.get("run_id"),
    ):
        if candidate:
            s = str(candidate).strip()
            if s:
                return s
    return None


def _extract_workspace_id_from_config(config: dict[str, Any] | None) -> str | None:
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    metadata = config.get("metadata")
    for candidate in (
        configurable.get("workspace_id") if isinstance(configurable, dict) else None,
        metadata.get("workspace_id") if isinstance(metadata, dict) else None,
        config.get("workspace_id"),
    ):
        if candidate:
            s = str(candidate).strip()
            if s:
                return s
    return None


def _prior_delivered_send_calls_by_target_and_digest(
    turn_messages: Sequence[Any],
    *,
    default_workspace_id: str | None = None,
) -> dict[tuple[str, str], str]:
    """Map ``(normalized_target, payload_digest)`` -> prior delivered ``send_message``
    ``tool_call_id`` in this turn (only when text + attachments match)."""
    if not turn_messages:
        return {}
    start = _turn_start_index(turn_messages)
    window = turn_messages[start:]

    call_meta_by_id: dict[str, tuple[str, str]] = {}
    for msg in window:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or ():
                if isinstance(tc, dict) and tc.get("name") == "send_message" and tc.get("id"):
                    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                    norm_target = _normalize_send_target(args.get("target"))
                    ws_id = args.get("workspace_id") or default_workspace_id
                    digest = compute_send_payload_digest(
                        args.get("text"),
                        args.get("files"),
                        default_workspace_id=ws_id,
                    )
                    call_meta_by_id[str(tc["id"])] = (norm_target, digest)

    delivered: dict[tuple[str, str], str] = {}
    for msg in window:
        if not isinstance(msg, ToolMessage):
            continue
        tc_id = str(getattr(msg, "tool_call_id", "") or "")
        if not tc_id or tc_id not in call_meta_by_id:
            continue
        if _is_send_message_delivered(msg):
            delivered[call_meta_by_id[tc_id]] = tc_id
    return delivered


def register_retry_send_dedup(
    ai_msg: AIMessage,
    turn_messages: Sequence[Any],
    *,
    config: dict[str, Any] | None,
    attempt: int,
) -> dict[str, dict[str, Any]]:
    """Record durable turn-level idempotency metadata for any ``send_message`` call
    generated during a retry (``attempt > 0``).

    Stamps ``ai_msg.additional_kwargs["dalton_retry_send_dedup"]`` (persisted into
    the LangGraph checkpoint), updates the process-local cache, and returns the
    per-tool-call records for async Postgres persistence.
    """
    if attempt <= 0:
        return {}
    tool_calls = getattr(ai_msg, "tool_calls", None) or ()
    send_calls = [
        tc for tc in tool_calls if isinstance(tc, dict) and tc.get("name") == "send_message"
    ]
    if not send_calls:
        return {}
    run_id = _extract_run_id_from_config(config)
    if not run_id:
        return {}
    turn_ws_id = _extract_workspace_id_from_config(config)
    prior_by_key = _prior_delivered_send_calls_by_target_and_digest(
        turn_messages, default_workspace_id=turn_ws_id
    )
    records: dict[str, dict[str, Any]] = {}
    with _RETRY_SEND_DEDUP_LOCK:
        for tc in send_calls:
            tc_id = str(tc.get("id") or "").strip()
            if not tc_id:
                continue
            args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            norm_target = _normalize_send_target(args.get("target"))
            ws_id = args.get("workspace_id") or turn_ws_id
            digest = compute_send_payload_digest(
                args.get("text"),
                args.get("files"),
                default_workspace_id=ws_id,
            )
            prior_tc_id = prior_by_key.get((norm_target, digest))
            key = f"{run_id}:{prior_tc_id}" if prior_tc_id else None
            info = RetrySendDedupInfo(
                delivery_key=key,
                retry_dedup_by_target=True,
                payload_digest=digest,
            )
            _RETRY_SEND_DEDUP_REGISTRY[(run_id, tc_id)] = info
            _RETRY_SEND_DEDUP_REGISTRY.move_to_end((run_id, tc_id))
            while len(_RETRY_SEND_DEDUP_REGISTRY) > _RETRY_SEND_DEDUP_MAX:
                _RETRY_SEND_DEDUP_REGISTRY.popitem(last=False)
            records[tc_id] = {
                "delivery_key": key,
                "retry_dedup_by_target": True,
                "payload_digest": digest,
            }
    if records and isinstance(ai_msg, AIMessage):
        if ai_msg.additional_kwargs is None:
            ai_msg.additional_kwargs = {}
        existing_map = ai_msg.additional_kwargs.get(RETRY_SEND_DEDUP_KWARG)
        merged = dict(existing_map) if isinstance(existing_map, dict) else {}
        merged.update(records)
        ai_msg.additional_kwargs[RETRY_SEND_DEDUP_KWARG] = merged
    return records


def _is_valid_uuid(val: str | None) -> bool:
    if not val:
        return False
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, TypeError):
        return False


async def persist_retry_send_dedup_to_db(
    run_id: str | None,
    records: dict[str, dict[str, Any]],
) -> bool:
    """Persist retry-generated ``send_message`` dedup markers onto
    ``conversation_responses.metadata['dalton_retry_send_dedup']`` so recovery
    and cross-worker execution preserve them."""
    rid = str(run_id or "").strip()
    if not rid or not records or not _is_valid_uuid(rid):
        return False
    try:
        from psycopg.types.json import Jsonb

        from src.server.database import pool

        async with pool.get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE conversation_responses
                    SET metadata = COALESCE(metadata, '{}'::jsonb)
                        || jsonb_build_object(
                            %s,
                            COALESCE(metadata -> %s, '{}'::jsonb) || %s::jsonb
                        )
                    WHERE conversation_response_id = %s::uuid
                    """,
                    (
                        RETRY_SEND_DEDUP_KWARG,
                        RETRY_SEND_DEDUP_KWARG,
                        Jsonb(records),
                        rid,
                    ),
                )
                return bool(cur.rowcount)
    except Exception:
        logger.debug(
            "[near_empty_reply] persist_retry_send_dedup_to_db skipped for run_id=%s",
            rid,
            exc_info=True,
        )
        return False


async def load_retry_send_dedup_from_db(
    run_id: str | None,
    tool_call_id: str | None,
) -> RetrySendDedupInfo | None:
    """Read a persisted retry-send dedup marker from ``conversation_responses.metadata``
    across the ``/retry`` attempt chain."""
    rid = str(run_id or "").strip()
    tcid = str(tool_call_id or "").strip()
    if not rid or not tcid or not _is_valid_uuid(rid):
        return None
    try:
        from psycopg.rows import dict_row

        from src.server.database import pool

        async with pool.get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    WITH RECURSIVE chain AS (
                        SELECT conversation_response_id, retry_of_run_id, metadata, 0 AS depth
                        FROM conversation_responses
                        WHERE conversation_response_id = %s::uuid
                        UNION ALL
                        SELECT r.conversation_response_id, r.retry_of_run_id, r.metadata, c.depth + 1
                        FROM conversation_responses r
                        JOIN chain c ON r.conversation_response_id = c.retry_of_run_id
                        WHERE c.depth < 16
                    )
                    SELECT metadata -> %s -> %s AS entry
                    FROM chain
                    WHERE metadata -> %s ? %s
                    ORDER BY depth ASC
                    LIMIT 1
                    """,
                    (
                        rid,
                        RETRY_SEND_DEDUP_KWARG,
                        tcid,
                        RETRY_SEND_DEDUP_KWARG,
                        tcid,
                    ),
                )
                row = await cur.fetchone()
                entry = row.get("entry") if isinstance(row, dict) else None
                if isinstance(entry, dict):
                    return RetrySendDedupInfo(
                        delivery_key=(
                            str(entry["delivery_key"]).strip()
                            if isinstance(entry.get("delivery_key"), str)
                            and entry["delivery_key"].strip()
                            else None
                        ),
                        retry_dedup_by_target=bool(entry.get("retry_dedup_by_target", True)),
                        payload_digest=(
                            str(entry["payload_digest"]).strip()
                            if isinstance(entry.get("payload_digest"), str)
                            and entry["payload_digest"].strip()
                            else None
                        ),
                    )
    except Exception:
        logger.debug(
            "[near_empty_reply] load_retry_send_dedup_from_db skipped for run_id=%s",
            rid,
            exc_info=True,
        )
    return None


async def _load_retry_send_dedup_from_checkpoint(
    thread_id: str | None,
    run_id: str | None,
    tool_call_id: str | None,
) -> RetrySendDedupInfo | None:
    """Fallback recovery lookup reading ``dalton_retry_send_dedup`` from the
    checkpointed ``AIMessage.additional_kwargs`` when process memory was lost."""
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
            if not isinstance(msg, AIMessage):
                continue
            dedup_map = (getattr(msg, "additional_kwargs", None) or {}).get(
                RETRY_SEND_DEDUP_KWARG
            )
            if isinstance(dedup_map, dict) and isinstance(dedup_map.get(tcid), dict):
                entry = dedup_map[tcid]
                return RetrySendDedupInfo(
                    delivery_key=(
                        str(entry["delivery_key"]).strip()
                        if isinstance(entry.get("delivery_key"), str)
                        and entry["delivery_key"].strip()
                        else None
                    ),
                    retry_dedup_by_target=bool(entry.get("retry_dedup_by_target", True)),
                    payload_digest=(
                        str(entry["payload_digest"]).strip()
                        if isinstance(entry.get("payload_digest"), str)
                        and entry["payload_digest"].strip()
                        else None
                    ),
                )
    except Exception:
        logger.debug(
            "[near_empty_reply] checkpoint retry-send dedup lookup failed for run_id=%s",
            rid,
            exc_info=True,
        )
    return None


def resolve_retry_send_dedup(
    run_id: str | None,
    tool_call_id: str | None,
) -> RetrySendDedupInfo | None:
    """Look up retry-send idempotency metadata registered for ``(run_id, tool_call_id)``."""
    rid = str(run_id or "").strip()
    tcid = str(tool_call_id or "").strip()
    if not rid or not tcid:
        return None
    with _RETRY_SEND_DEDUP_LOCK:
        return _RETRY_SEND_DEDUP_REGISTRY.get((rid, tcid))


def _apply_retry_send_dedup_info(
    body: dict[str, Any],
    info: RetrySendDedupInfo | None,
) -> dict[str, Any]:
    updated = dict(body)
    payload_digest = compute_send_payload_digest(
        body.get("text"),
        body.get("files"),
        default_workspace_id=body.get("workspace_id"),
    )
    updated["payload_digest"] = payload_digest
    if info is None:
        return updated
    if info.delivery_key and (
        not info.payload_digest or info.payload_digest == payload_digest
    ):
        updated["idempotency_key"] = info.delivery_key
    if info.retry_dedup_by_target:
        updated["retry_dedup_by_target"] = True
    return updated


def _apply_agent_send_image_context(
    body: dict[str, Any],
    ctx: tuple[str, dict[str, str]] | None,
) -> dict[str, Any]:
    if ctx is None:
        return body
    msg_id, captured = ctx
    if msg_id and not body.get("source_message_id"):
        body["source_message_id"] = msg_id
    if captured and not body.get("captured_images"):
        body["captured_images"] = dict(captured)
    return body


def attach_retry_send_dedup(
    body: dict[str, Any],
    *,
    run_id: str | None,
    tool_call_id: str | None,
) -> dict[str, Any]:
    """Attach payload digest, in-process retry-send idempotency fields, and delivery-time
    image capture context to ``POST /agent/send``."""
    from src.dalton.channels.deliverable_text import lookup_agent_send_image_context

    info = resolve_retry_send_dedup(run_id, tool_call_id)
    updated = _apply_retry_send_dedup_info(body, info)
    return _apply_agent_send_image_context(
        updated, lookup_agent_send_image_context(run_id, tool_call_id)
    )


async def aattach_retry_send_dedup(
    body: dict[str, Any],
    *,
    run_id: str | None,
    tool_call_id: str | None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Async version used by ``tools.py``: checks in-process cache, then durable Postgres
    ``conversation_responses.metadata``, then checkpoint ``AIMessage.additional_kwargs``,
    and attaches delivery-time ``source_message_id`` + ``captured_images`` for ``:imgfix``."""
    from src.dalton.settings import is_strict_deliverable_text_enabled

    if not is_near_empty_retry_enabled() and not is_strict_deliverable_text_enabled():
        return body

    from src.dalton.channels.deliverable_text import (
        alookup_agent_send_image_context,
        contains_unresolved_sandbox_image_path,
        lookup_agent_send_image_context,
    )

    info = resolve_retry_send_dedup(run_id, tool_call_id)
    if info is None:
        info = await load_retry_send_dedup_from_db(run_id, tool_call_id)
    if info is None and thread_id:
        info = await _load_retry_send_dedup_from_checkpoint(thread_id, run_id, tool_call_id)
    if info is not None and run_id and tool_call_id:
        with _RETRY_SEND_DEDUP_LOCK:
            _RETRY_SEND_DEDUP_REGISTRY[(str(run_id).strip(), str(tool_call_id).strip())] = info
    updated = _apply_retry_send_dedup_info(body, info)
    img_ctx = lookup_agent_send_image_context(run_id, tool_call_id)
    if (
        img_ctx is None
        and thread_id
        and contains_unresolved_sandbox_image_path(str(updated.get("text") or ""))
    ):
        img_ctx = await alookup_agent_send_image_context(thread_id, run_id, tool_call_id)
    return _apply_agent_send_image_context(updated, img_ctx)


class RetryLoopBudget:
    """Per-call state machine for ``EmptyToolCallRetryMiddleware`` sync/async loops.

    Keeps the upstream retry loop thin (1–5 line delegation hooks) while owning:
    - HF2 ``all_refused`` tracking and refusal-exhausted dispatch.
    - Non-refusal shared 1-retry sub-budget (``empty_reply`` + ``near_empty_reply``).
    - Output-cap continuation link stamping preserved across intermediate retries.
    - Per-attempt stream correlation (``attempt_start``).
    - Terminal-attempt sanitized prefix capture vs fragment retraction & policy dispatch.
    - Durable retry-generated ``send_message`` turn-level idempotency registration.
    """

    def __init__(self, max_retries: int) -> None:
        self.max_retries = max_retries
        self.all_refused = True
        self.empty_like_retries = 0
        self.pending_continued_from: str | None = None

    def begin_attempt(self, attempt: int, *, is_subagent: bool) -> None:
        emit_attempt_start(attempt, is_subagent=is_subagent)

    def on_response(self, ai_msg: AIMessage, injected: Sequence[Any] = ()) -> None:
        cont_from = self.pending_continued_from or last_prefix_message_id(injected)
        if cont_from and cont_from != getattr(ai_msg, "id", None):
            stamp_continued_from(ai_msg, cont_from)
        self.pending_continued_from = None
        self.all_refused = self.all_refused and is_refusal_reply(ai_msg)

    def on_success(
        self,
        ai_msg: AIMessage,
        turn_messages: Sequence[Any],
        *,
        config: dict[str, Any] | None,
        attempt: int,
        injected: Sequence[Any] = (),
    ) -> dict[str, dict[str, Any]]:
        if not (getattr(ai_msg, "additional_kwargs", None) or {}).get(CONTINUED_FROM_KWARG):
            prefix_id = last_prefix_message_id(injected)
            if prefix_id and prefix_id != getattr(ai_msg, "id", None):
                stamp_continued_from(ai_msg, prefix_id)
        return register_retry_send_dedup(ai_msg, turn_messages, config=config, attempt=attempt)

    async def aon_success(
        self,
        ai_msg: AIMessage,
        turn_messages: Sequence[Any],
        *,
        config: dict[str, Any] | None,
        attempt: int,
        injected: Sequence[Any] = (),
    ) -> None:
        records = self.on_success(
            ai_msg,
            turn_messages,
            config=config,
            attempt=attempt,
            injected=injected,
        )
        if records:
            await persist_retry_send_dedup_to_db(
                _extract_run_id_from_config(config),
                records,
            )

    def should_stop(self, attempt: int, kind: str, ai_msg: AIMessage) -> bool:
        non_refusal_empty_like = is_non_refusal_empty_like(kind, ai_msg)
        if attempt == self.max_retries or (
            non_refusal_empty_like and self.empty_like_retries >= EMPTY_OR_NEAR_EMPTY_MAX_RETRIES
        ):
            return True
        if non_refusal_empty_like:
            self.empty_like_retries += 1
        return False

    def advance_retry(
        self,
        middleware: Any,
        request: Any,
        ai_msg: AIMessage,
        kind: str,
        injected: list[Any],
        rehintable: bool,
        *,
        is_subagent: bool,
        has_tool_calls: Any,
    ) -> tuple[list[Any], bool]:
        """Advance one retry step, preserving any kept output_cap prefix continuation link
        across intermediate non-sanitized retries (empty / near_empty / tool_calls)."""
        hint_text = middleware._hint_for(kind, ai_msg)
        if kind in _SANITIZED_KINDS:
            sanitized = middleware._sanitize_output_cap_reply(ai_msg)
            cont_from = (
                emit_continuation_link(sanitized, is_subagent=is_subagent)
                if (
                    kind == "output_cap"
                    and sanitized is not None
                    and not has_tool_calls(ai_msg)
                    and deliverable_text_of_ai_message(sanitized).strip()
                )
                else None
            )
            added = middleware._inject_output_cap(request, sanitized, hint_text)
            new_injected = [*injected, *added]
            if cont_from:
                self.pending_continued_from = cont_from
            else:
                preserved = last_prefix_message_id(new_injected)
                self.pending_continued_from = preserved
                if preserved:
                    emit_continuation_link_id(preserved, is_subagent=is_subagent)
            return new_injected, False

        emit_message_discarded(ai_msg, is_subagent=is_subagent)
        mark_retried_attempt(ai_msg)
        if not injected or not rehintable:
            added = middleware._inject_hint(request, ai_msg, hint_text)
            new_injected = [*injected, *added]
        else:
            new_injected = middleware._rehint(request, injected, ai_msg, hint_text)

        # Keep the continuation chain pointing to the last valid prefix segment
        # even when an intermediate attempt was discarded (empty / near_empty / tool_calls).
        preserved = last_prefix_message_id(new_injected)
        self.pending_continued_from = preserved
        if preserved:
            emit_continuation_link_id(preserved, is_subagent=is_subagent)
        return new_injected, True

    def finalize_stopped_attempt(
        self,
        middleware: Any,
        kind: str,
        ai_msg: AIMessage,
        injected: list[Any],
        *,
        is_subagent: bool,
    ) -> list[Any]:
        if kind in _SANITIZED_KINDS:
            return [
                *injected,
                *middleware._build_output_cap_injection(
                    sanitize_output_cap_reply(ai_msg),
                    middleware._hint_for(kind, ai_msg),
                ),
            ]
        emit_message_discarded(ai_msg, is_subagent=is_subagent)
        mark_retried_attempt(ai_msg)
        return injected

    def finish_retry(
        self,
        middleware: Any,
        response: Any,
        kind: str | None,
        injected: list[Any],
        last_sanitized_kind: str | None,
        *,
        config: dict[str, Any],
        is_subagent: bool,
        default_notices: dict[str, str],
        turn_messages: Sequence[Any],
    ) -> Any:
        """Terminal retry policy dispatch (refusal exhaustion -> notice -> keep_injected)."""
        failure_kind = kind if kind in _SANITIZED_KINDS else (last_sanitized_kind or kind)
        refusal = self.handle_refusal_exhaustion(
            response,
            failure_kind=failure_kind,
            config=config,
            is_subagent=is_subagent,
        )
        if refusal is not None:
            return refusal
        if failure_kind in default_notices or failure_kind == "near_empty_reply":
            prefix = injected if last_sanitized_kind is not None else []
            return middleware._exhausted_failure(
                response,
                failure_kind,
                prefix=prefix,
                turn_messages=turn_messages,
            )
        return middleware._keep_injected(response, injected)

    def handle_refusal_exhaustion(
        self,
        response: Any,
        *,
        failure_kind: str | None,
        config: dict[str, Any],
        is_subagent: bool,
    ) -> Any | None:
        if failure_kind != "empty_reply":
            return None
        refusal = handle_refusal_exhausted(
            response,
            all_refused=self.all_refused,
            max_retries=self.max_retries,
            config=config,
        )
        if refusal is None:
            return None
        notice = refusal.result[0]
        if isinstance(notice, AIMessage):
            if notice.additional_kwargs is None:
                notice.additional_kwargs = {}
            notice.additional_kwargs[EXHAUSTED_NOTICE_KWARG] = True
            if not getattr(notice, "id", None):
                notice = notice.model_copy(update={"id": f"dalton-refusal-{uuid.uuid4().hex[:12]}"})
                refusal = dataclasses.replace(refusal, result=[notice, *refusal.result[1:]])
            emit_exhausted_notice(notice, is_subagent=is_subagent)
        return refusal


__all__ = [
    "EMPTY_OR_NEAR_EMPTY_MAX_RETRIES",
    "MAX_SINGLE_SEMANTIC_LEN",
    "MAX_SYMBOL_ONLY_LEN",
    "NEAR_EMPTY_REPLY_HINT",
    "NEAR_EMPTY_REPLY_NOTICE",
    "RETRY_SEND_DEDUP_KWARG",
    "RetryLoopBudget",
    "RetrySendDedupInfo",
    "SHORT_USER_INPUT_MAX_LEN",
    "aattach_retry_send_dedup",
    "attach_retry_send_dedup",
    "build_and_emit_exhausted_notice",
    "compute_send_payload_digest",
    "deliverable_text_of_ai_message",
    "emit_attempt_start",
    "emit_continuation_link",
    "emit_continuation_link_id",
    "emit_exhausted_notice",
    "emit_main_stream_event",
    "emit_message_discarded",
    "is_deliverable_reply_empty",
    "is_near_empty_reply",
    "is_non_refusal_empty_like",
    "last_prefix_message_id",
    "load_retry_send_dedup_from_db",
    "mark_retried_attempt",
    "persist_retry_send_dedup_to_db",
    "register_retry_send_dedup",
    "resolve_retry_send_dedup",
    "sanitize_output_cap_reply",
    "stamp_continued_from",
]
