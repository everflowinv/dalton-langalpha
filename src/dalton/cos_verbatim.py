"""Chief of Staff verbatim user request forwarding for ``delegate_to_analyst`` (§3.4).

When Home's Chief of Staff hands a task off to a workspace analyst, paraphrasing
can drop exact user formatting, file-scope, or metric requirements. This module
captures the raw pre-injection user text (preserving original characters and
whitespace) plus attachment basenames for the current turn, and appends a
``<user_verbatim_request>...</user_verbatim_request>`` block when the dispatch
``question`` does not already contain the verbatim text.
"""

from __future__ import annotations

import inspect
import logging
import os
import posixpath
import re
from typing import Any

logger = logging.getLogger(__name__)

COS_TURN_VERBATIM_CONFIG_KEY = "cos_turn_verbatim_request"
COS_APPROVED_DISPATCH_PAYLOADS_CONFIG_KEY = "cos_approved_dispatch_payloads"
COS_HITL_RESUME_CONFIG_KEY = "cos_is_hitl_resume"

_MISSING_APPROVED_PAYLOAD_ERROR = (
    "Approved hand-off payload is temporarily unavailable (missing approval-bound "
    "question from checkpoint); nothing was dispatched. Please retry the approval."
)

_CLOSING_TAG_RE = re.compile(r"</\s*user_verbatim_request\s*>", re.IGNORECASE)
_SYSTEM_REMINDER_RE = re.compile(
    r"\s*<system-reminder>.*?</system-reminder>\s*",
    re.DOTALL | re.IGNORECASE,
)


def _escape_closing_tag(text: str) -> str:
    """Escape any closing ``</user_verbatim_request>`` tag inside verbatim text."""
    return _CLOSING_TAG_RE.sub("&lt;/user_verbatim_request&gt;", text)


def _basename_only(raw_name: Any) -> str | None:
    """Return ONLY the filename component (no directory/URL), or None."""
    if not isinstance(raw_name, str):
        return None
    cleaned = raw_name.strip()
    if not cleaned:
        return None
    # Strip query/fragment if a URL slipped through, then take basename across POSIX & Windows separators.
    no_query = cleaned.split("?", 1)[0].split("#", 1)[0].rstrip("/\\")
    base = posixpath.basename(no_query.replace("\\", "/"))
    base = os.path.basename(base)
    return base or None


def _is_user_message(msg: Any) -> bool:
    """Return True if ``msg`` represents a human/user message."""
    if isinstance(msg, dict):
        role = str(msg.get("role") or msg.get("type") or "").strip().lower()
        return role in ("user", "human")
    msg_type = str(getattr(msg, "type", "") or "").strip().lower()
    if msg_type in ("human", "user"):
        return True
    role = str(getattr(msg, "role", "") or "").strip().lower()
    return role in ("human", "user")


def _extract_raw_text_parts(content: Any) -> list[str]:
    """Extract raw text fragments in original order WITHOUT stripping whitespace."""
    if isinstance(content, str):
        return [content] if content != "" else []
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item != "":
                    parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in (None, "text", "input_text"):
                    text = item.get("text")
                    if isinstance(text, str) and text != "":
                        parts.append(text)
            elif hasattr(item, "type"):
                if getattr(item, "type", None) in (None, "text", "input_text"):
                    text = getattr(item, "text", None)
                    if isinstance(text, str) and text != "":
                        parts.append(text)
        return parts
    return []


def _join_raw_parts(parts: list[str]) -> str:
    """Join multiple raw text blocks in order without stripping any leading/trailing whitespace."""
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "\n".join(parts)


def _is_system_triggered_request(
    request: Any,
    *,
    from_server: bool = False,
    is_checkpoint_replay: bool = False,
) -> bool:
    """Return True when the turn was NOT initiated by fresh user input."""
    if from_server or is_checkpoint_replay:
        return True
    if getattr(request, "hitl_response", None):
        return True
    for attr in ("report_source", "wakeup", "market_target"):
        if getattr(request, attr, None):
            return True
    origin = getattr(request, "origin", None)
    if origin is not None:
        origin_type = (
            origin.get("type")
            if isinstance(origin, dict)
            else getattr(origin, "type", None)
        )
        if origin_type and str(origin_type).strip().lower() != "user":
            return True
    return False


def _collect_attachment_filenames(
    request: Any,
    turn_attachments: Any = None,
) -> list[str]:
    """Collect attachment basenames in order without paths, URLs, or payloads."""
    names: list[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        base = _basename_only(raw)
        if base and base not in seen:
            seen.add(base)
            names.append(base)

    if turn_attachments is not None:
        for att in getattr(turn_attachments, "files", None) or ():
            _add(getattr(att, "name", None) or getattr(att, "filename", None))

    additional = getattr(request, "additional_context", None) or ()
    for ctx in additional:
        if isinstance(ctx, dict):
            ctx_type = str(ctx.get("type") or "").strip().lower()
            if ctx_type in ("chat_upload", "file", "image", "pdf", "attachment"):
                _add(ctx.get("name") or ctx.get("filename"))
        else:
            ctx_type = str(getattr(ctx, "type", "") or "").strip().lower()
            if ctx_type in ("chat_upload", "file", "image", "pdf", "attachment"):
                _add(getattr(ctx, "name", None) or getattr(ctx, "filename", None))
    return names


def capture_turn_verbatim_request(
    *,
    request: Any,
    raw_messages: list[dict[str, Any]] | None,
    turn_attachments: Any = None,
    from_server: bool = False,
    is_checkpoint_replay: bool = False,
) -> dict[str, Any] | None:
    """Capture this turn's raw pre-injection user text and attachment basenames.

    Must be called BEFORE ``prepare_skill_contexts``, ``attach_request_files``,
    and ``inject_inline_reminders`` mutate ``messages``. Returns ``None`` when
    the turn is system/notification-triggered or carries no user text (e.g.
    image-only input).
    """
    if _is_system_triggered_request(
        request,
        from_server=from_server,
        is_checkpoint_replay=is_checkpoint_replay,
    ):
        return None

    source_msgs = raw_messages if raw_messages is not None else (getattr(request, "messages", None) or [])
    if not source_msgs:
        return None
    last_msg = source_msgs[-1]
    if not _is_user_message(last_msg):
        return None

    raw_content = (
        last_msg.get("content")
        if isinstance(last_msg, dict)
        else getattr(last_msg, "content", None)
    )
    parts = _extract_raw_text_parts(raw_content)
    text = _join_raw_parts(parts)
    if not text or not text.strip():
        return None

    attachments = _collect_attachment_filenames(request, turn_attachments)
    return {"text": text, "attachments": attachments}


def _extract_from_state_current_turn(state: dict[str, Any] | None) -> tuple[str | None, list[str]]:
    """Fallback when no pre-injection payload was passed via ``config``.

    Inspects ONLY the most recent human/user message in ``state["messages"]``
    (never walking back to an earlier turn when the current turn's human
    message has no user text), strips server-injected ``<system-reminder>``
    blocks if present, and preserves the user's original characters & whitespace.
    """
    if not isinstance(state, dict):
        return None, []
    messages = state.get("messages")
    if not isinstance(messages, (list, tuple)):
        return None, []
    for msg in reversed(messages):
        if not _is_user_message(msg):
            continue
        # Stop at the latest human/user message — never fall back to an older turn!
        extra_kw = (
            msg.get("additional_kwargs")
            if isinstance(msg, dict)
            else getattr(msg, "additional_kwargs", None)
        )
        if isinstance(extra_kw, dict):
            if extra_kw.get("is_system") or extra_kw.get("system_notification"):
                return None, []
            origin = extra_kw.get("origin")
            if isinstance(origin, dict) and str(origin.get("type") or "").strip().lower() not in ("", "user"):
                return None, []

        raw_content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        parts = _extract_raw_text_parts(raw_content)
        cleaned_parts: list[str] = []
        for p in parts:
            if "<system-reminder>" in p.lower():
                stripped = _SYSTEM_REMINDER_RE.sub("", p)
                if stripped and stripped.strip():
                    cleaned_parts.append(stripped)
            else:
                cleaned_parts.append(p)
        text = _join_raw_parts(cleaned_parts)
        if not text or not text.strip():
            return None, []

        att_names: list[str] = []
        seen: set[str] = set()
        if isinstance(extra_kw, dict):
            for att in extra_kw.get("attachments") or ():
                raw_n = att.get("name") if isinstance(att, dict) else att
                base = _basename_only(raw_n)
                if base and base not in seen:
                    seen.add(base)
                    att_names.append(base)
        return text, att_names
    return None, []


def _resolve_turn_verbatim(
    state: dict[str, Any] | None,
    config: Any = None,
) -> tuple[str | None, list[str]]:
    """Resolve ``(verbatim_text, attachment_filenames)`` for the current turn."""
    if isinstance(config, dict):
        configurable = config.get("configurable")
        if isinstance(configurable, dict) and COS_TURN_VERBATIM_CONFIG_KEY in configurable:
            payload = configurable[COS_TURN_VERBATIM_CONFIG_KEY]
            if not isinstance(payload, dict):
                return None, []
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                return None, []
            atts: list[str] = []
            seen: set[str] = set()
            for raw in payload.get("attachments") or ():
                base = _basename_only(raw)
                if base and base not in seen:
                    seen.add(base)
                    atts.append(base)
            return text, atts
    return _extract_from_state_current_turn(state)


def extract_latest_user_message(
    state: dict[str, Any] | None,
    *,
    config: Any = None,
) -> str | None:
    """Return this turn's raw user message text (pre-injection when ``config`` carries it), or None."""
    text, _atts = _resolve_turn_verbatim(state, config=config)
    return text


def append_verbatim_user_request(
    question: str,
    state: dict[str, Any] | None = None,
    *,
    config: Any = None,
) -> str:
    """Append ``<user_verbatim_request>`` to ``question`` unless already quoted verbatim."""
    if not isinstance(question, str):
        return question
    verbatim, attachments = _resolve_turn_verbatim(state, config=config)
    if not verbatim:
        return question
    if verbatim in question and all(name in question for name in attachments):
        return question
    safe_text = _escape_closing_tag(verbatim)
    if attachments:
        safe_atts = ", ".join(_escape_closing_tag(name) for name in attachments)
        body = f"{safe_text}\nAttachments: {safe_atts}"
    else:
        body = safe_text
    base = question.rstrip()
    block = f"<user_verbatim_request>\n{body}\n</user_verbatim_request>"
    return f"{base}\n\n{block}" if base else block


def extract_approved_dispatch_payloads_from_checkpoint(
    checkpoint_tuple: Any,
    *,
    hitl_response: Any = None,
) -> dict[str, dict[str, Any]]:
    """Extract approval-card ``ptc_agent`` dispatch payloads keyed by ``tool_call_id``
    from a LangGraph checkpoint tuple's pending interrupts."""
    if checkpoint_tuple is None:
        return {}
    from src.server.utils.checkpoint_helpers import interrupt_records

    pending = interrupt_records(getattr(checkpoint_tuple, "pending_writes", None))
    if not pending:
        return {}
    answered_ids = (
        {str(k) for k in hitl_response.keys()}
        if isinstance(hitl_response, dict) and hitl_response
        else None
    )
    payloads: dict[str, dict[str, Any]] = {}
    for rec in pending:
        rec_id = rec.get("id")
        if answered_ids is not None and rec_id is not None and str(rec_id) not in answered_ids:
            continue
        val = rec.get("value")
        if not isinstance(val, dict):
            continue
        action_reqs = val.get("action_requests")
        if not isinstance(action_reqs, list):
            continue
        for req in action_reqs:
            if not isinstance(req, dict) or req.get("type") != "ptc_agent":
                continue
            call_id = req.get("tool_call_id")
            card_question = req.get("question")
            if isinstance(call_id, str) and call_id.strip() and isinstance(card_question, str):
                payloads[call_id] = {
                    "question": card_question,
                    "workspace_id": req.get("workspace_id"),
                    "thread_id": req.get("thread_id"),
                }
    return payloads


async def load_approved_dispatch_payloads(
    checkpointer: Any,
    thread_id: str | None,
    *,
    checkpoint_id: str | None = None,
    hitl_response: Any = None,
) -> dict[str, dict[str, Any]] | None:
    """Load approval-bound ``ptc_agent`` dispatch payloads from ``checkpointer``
    so HITL resume reuses the exact card text (with verbatim block & attachments)
    without regenerating.

    Returns ``None`` (rather than ``{}``) when the checkpoint cannot be read so
    callers distinguish a read failure from an empty interrupt map and never
    silently fall back to raw tool arguments."""
    if checkpointer is None or not thread_id:
        return None
    cfg: dict[str, Any] = {"configurable": {"thread_id": str(thread_id)}}
    if checkpoint_id:
        cfg["configurable"]["checkpoint_id"] = str(checkpoint_id)
    try:
        aget = getattr(checkpointer, "aget_tuple", None)
        if aget is not None:
            maybe = aget(cfg)
            cp_tuple = await maybe if inspect.isawaitable(maybe) else maybe
        else:
            get = getattr(checkpointer, "get_tuple", None)
            cp_tuple = get(cfg) if get is not None else None
    except Exception:
        logger.warning(
            "Failed to load approved dispatch payloads from checkpoint",
            extra={"thread_id": thread_id, "checkpoint_id": checkpoint_id},
            exc_info=True,
        )
        return None
    if cp_tuple is None:
        return None
    return extract_approved_dispatch_payloads_from_checkpoint(
        cp_tuple, hitl_response=hitl_response
    )


def resolve_dispatch_question(
    question: str,
    state: dict[str, Any] | None = None,
    *,
    config: Any = None,
    tool_call_id: str | None = None,
) -> str:
    """Reuse the approval-bound dispatch question for ``tool_call_id`` on HITL
    resume; otherwise enrich ``question`` with this turn's verbatim user request."""
    if isinstance(tool_call_id, str) and tool_call_id.strip() and isinstance(config, dict):
        configurable = config.get("configurable")
        if isinstance(configurable, dict):
            approved_map = configurable.get(COS_APPROVED_DISPATCH_PAYLOADS_CONFIG_KEY)
            if isinstance(approved_map, dict):
                saved = approved_map.get(tool_call_id)
                if isinstance(saved, dict) and isinstance(saved.get("question"), str):
                    return saved["question"]
    return append_verbatim_user_request(question, state, config=config)


def resolve_approved_dispatch_question(
    question: str,
    *,
    config: Any = None,
    tool_call_id: str | None = None,
) -> tuple[str, str | None]:
    """Verify and return the approval-bound question for an approved HITL
    ``ptc_agent`` hand-off.

    When the turn is a HITL resume (``cos_is_hitl_resume`` or
    ``cos_approved_dispatch_payloads`` present in ``config["configurable"]``),
    missing or unreadable bound payload for ``tool_call_id`` returns a
    retryable error message instead of silently falling back to the raw tool
    argument."""
    if not isinstance(config, dict):
        return question, None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return question, None
    is_hitl_resume = bool(configurable.get(COS_HITL_RESUME_CONFIG_KEY)) or (
        COS_APPROVED_DISPATCH_PAYLOADS_CONFIG_KEY in configurable
    )
    if not is_hitl_resume:
        return question, None
    approved_map = configurable.get(COS_APPROVED_DISPATCH_PAYLOADS_CONFIG_KEY)
    if (
        isinstance(approved_map, dict)
        and isinstance(tool_call_id, str)
        and tool_call_id.strip()
    ):
        saved = approved_map.get(tool_call_id)
        if isinstance(saved, dict) and isinstance(saved.get("question"), str) and saved["question"].strip():
            return saved["question"], None
    return question, _MISSING_APPROVED_PAYLOAD_ERROR
