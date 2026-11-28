"""Dalton safety-refusal detection and user-facing exhaustion notice.

Keeps refusal matching, channel/web surface detection, and user-facing notice
wording inside Dalton-owned code so upstream ``empty_call_retry.py`` stays a
minimal thin ``DALTON-HOOK(refusal-handling)`` delegate.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

# User-facing notices when all retry attempts ended with a model safety refusal.
REFUSAL_NOTICE_CHANNEL = (
    "模型拒绝回答这一轮。可能是对话里较早的内容触发了模型的安全拦截，"
    "与这次的问题无关。可以发送 /new 开始新对话后再问。"
)
REFUSAL_NOTICE_WEB = (
    "模型拒绝回答这一轮。可能是对话里较早的内容触发了模型的安全拦截，"
    "与这次的问题无关。可以新建对话后再问。"
)

# Web surfaces without a `/new` slash command.
_WEB_SURFACES = frozenset({"", "web", "market_view"})

# Exact stop_reason / finish_reason values indicating model safety interception:
# Anthropic ``stop_reason=refusal`` and Gemini ``finishReason`` safety blocks.
_REFUSAL_STOP_REASONS = frozenset(
    {"refusal", "safety", "prohibited_content", "blocklist", "spii"}
)


def refusal_stop_reason(ai_msg: Any) -> str | None:
    """Return the matched safety-refusal stop/finish reason, or ``None``."""
    if not isinstance(ai_msg, AIMessage):
        return None
    meta = getattr(ai_msg, "response_metadata", None) or {}
    for key in ("stop_reason", "finish_reason"):
        reason = meta.get(key)
        if isinstance(reason, str) and reason.strip().lower() in _REFUSAL_STOP_REASONS:
            return reason
    return None


def is_refusal_reply(ai_msg: Any) -> bool:
    """True when ``ai_msg`` ended on a safety-refusal reason without tool calls."""
    if refusal_stop_reason(ai_msg) is None:
        return False
    if getattr(ai_msg, "tool_calls", None) or getattr(ai_msg, "invalid_tool_calls", None):
        return False
    return True


def all_attempts_refused(attempts: Sequence[Any]) -> bool:
    """True when at least one attempt ran and every attempt was a safety refusal."""
    return bool(attempts) and all(is_refusal_reply(msg) for msg in attempts)


def supports_slash_new(config: Mapping[str, Any] | None) -> bool:
    """Whether the active turn surface supports sending ``/new``."""
    if not isinstance(config, Mapping):
        return False
    configurable = config.get("configurable")
    metadata = config.get("metadata")
    platform: Any = None
    if isinstance(configurable, Mapping):
        platform = configurable.get("platform")
    if not platform and isinstance(metadata, Mapping):
        platform = metadata.get("platform")
    surface_name = str(platform or "").strip().lower().split(":", 1)[0]
    return surface_name not in _WEB_SURFACES


def refusal_notice_text(config: Mapping[str, Any] | None) -> str:
    """Return the channel (``/new``) or web (``新建对话``) refusal notice."""
    return REFUSAL_NOTICE_CHANNEL if supports_slash_new(config) else REFUSAL_NOTICE_WEB


def build_refusal_notice_response(response: Any, config: Mapping[str, Any] | None) -> Any:
    """Replace the response's reply with the user-facing safety-refusal notice."""
    last = response.result[0]
    last_meta = dict(getattr(last, "response_metadata", None) or {})
    notice = AIMessage(
        content=refusal_notice_text(config),
        id=f"dalton-refusal-{uuid.uuid4().hex[:12]}",
        usage_metadata=getattr(last, "usage_metadata", None),
        response_metadata=last_meta,
    )
    return dataclasses.replace(response, result=[notice])


def handle_refusal_exhausted(
    response: Any,
    *,
    all_refused: bool,
    max_retries: int,
    config: Mapping[str, Any] | None,
) -> Any | None:
    """Return the user-facing refusal notice when every retry ended on safety refusal."""
    if not all_refused:
        return None
    configurable = config.get("configurable") if isinstance(config, Mapping) else None
    thread_id = configurable.get("thread_id") if isinstance(configurable, Mapping) else None
    last = response.result[0] if getattr(response, "result", None) else None
    logger.warning(
        "[RefusalNotice] thread=%s all %d attempts ended with safety refusal (reason=%s, max_retries=%d)",
        thread_id,
        1 + max_retries,
        refusal_stop_reason(last),
        max_retries,
    )
    return build_refusal_notice_response(response, config)


__all__ = [
    "REFUSAL_NOTICE_CHANNEL",
    "REFUSAL_NOTICE_WEB",
    "all_attempts_refused",
    "build_refusal_notice_response",
    "handle_refusal_exhausted",
    "is_refusal_reply",
    "refusal_notice_text",
    "refusal_stop_reason",
    "supports_slash_new",
]
