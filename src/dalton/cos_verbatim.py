"""Chief of Staff verbatim user request forwarding for ``delegate_to_analyst`` (§3.4).

When Home's Chief of Staff hands a task off to a workspace analyst, paraphrasing
can drop exact user formatting, file-scope, or metric requirements. This module
extracts the latest user message from the LangGraph state and appends a
``<user_verbatim_request>...</user_verbatim_request>`` block when the dispatch
``question`` does not already contain the verbatim text.
"""

from __future__ import annotations

from typing import Any


def _message_text(content: Any) -> str:
    """Extract plain UTF-8 text from a LangChain message ``content`` field."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                if item.get("type") in (None, "text", "input_text"):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
        return "\n".join(parts).strip()
    return ""


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


def extract_latest_user_message(state: dict[str, Any] | None) -> str | None:
    """Return the latest non-empty human/user message text from ``state``, or None."""
    if not isinstance(state, dict):
        return None
    messages = state.get("messages")
    if not isinstance(messages, (list, tuple)):
        return None
    for msg in reversed(messages):
        if not _is_user_message(msg):
            continue
        raw_content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        text = _message_text(raw_content)
        if text:
            return text
    return None


def append_verbatim_user_request(
    question: str, state: dict[str, Any] | None
) -> str:
    """Append ``<user_verbatim_request>`` to ``question`` unless already quoted verbatim."""
    if not isinstance(question, str):
        return question
    verbatim = extract_latest_user_message(state)
    if not verbatim:
        return question
    if verbatim in question:
        return question
    base = question.rstrip()
    block = f"<user_verbatim_request>\n{verbatim}\n</user_verbatim_request>"
    return f"{base}\n\n{block}" if base else block
