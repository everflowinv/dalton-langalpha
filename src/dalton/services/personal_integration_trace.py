"""Redaction helpers for personal-integration tool payloads in traces and transcripts.

Ensures private calendar/mail tool arguments, tool outputs, and subsequent model
inputs/outputs carrying that context are stripped before leaving the backend to
LangSmith or being written into stored thread transcripts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PERSONAL_INTEGRATION_TOOL_NAMES: frozenset[str] = frozenset(
    {"dalton_calendar", "dalton_mail"}
)
REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER = "[REDACTED_PERSONAL_INTEGRATION_DATA]"

_SAFE_TOOL_ARG_KEYS: frozenset[str] = frozenset(
    {"action", "provider", "all_day", "limit", "duration_minutes", "use_default_reminders"}
)


def is_personal_integration_tool(name: str | None) -> bool:
    return bool(name and str(name).strip() in PERSONAL_INTEGRATION_TOOL_NAMES)


def redact_personal_tool_args(args: Any) -> dict[str, Any]:
    """Return a redacted copy of personal-integration tool arguments."""
    if not isinstance(args, Mapping):
        return {"redacted": REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER}
    out: dict[str, Any] = {}
    for key, val in args.items():
        if str(key) in _SAFE_TOOL_ARG_KEYS:
            out[str(key)] = val
        else:
            out[str(key)] = REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER
    return out


def _message_obj_is_personal_tool(msg: Any) -> bool:
    if isinstance(msg, Mapping):
        if is_personal_integration_tool(msg.get("name")):
            return True
        kwargs = msg.get("kwargs")
        if isinstance(kwargs, Mapping):
            if is_personal_integration_tool(kwargs.get("name")):
                return True
            tcs = kwargs.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    if isinstance(tc, Mapping) and is_personal_integration_tool(tc.get("name")):
                        return True
        tcs_top = msg.get("tool_calls")
        if isinstance(tcs_top, list):
            for tc in tcs_top:
                if isinstance(tc, Mapping) and is_personal_integration_tool(tc.get("name")):
                    return True
        return False

    name = getattr(msg, "name", None)
    if is_personal_integration_tool(name):
        return True
    tool_calls = getattr(msg, "tool_calls", None)
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, Mapping) and is_personal_integration_tool(tc.get("name")):
                return True
    return False


def messages_contain_personal_integration_data(value: Any) -> bool:
    """Inspect serialized/live LangChain run inputs for any personal-integration tool context."""
    if value is None:
        return False
    if isinstance(value, Mapping):
        if _message_obj_is_personal_tool(value):
            return True
        for key in ("messages", "prompts", "input", "state"):
            if key in value and messages_contain_personal_integration_data(value[key]):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(messages_contain_personal_integration_data(item) for item in value)
    return _message_obj_is_personal_tool(value)


def _redact_serialized_message(msg: Any) -> Any:
    """Return a non-mutating redacted copy of a serialized or BaseMessage item."""
    if isinstance(msg, Mapping):
        copied = dict(msg)
        if "content" in copied:
            copied["content"] = REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER
        if "text" in copied:
            copied["text"] = REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER
        if isinstance(copied.get("tool_calls"), list):
            copied["tool_calls"] = [
                {
                    "id": tc.get("id"),
                    "name": tc.get("name"),
                    "args": redact_personal_tool_args(tc.get("args"))
                    if is_personal_integration_tool(tc.get("name"))
                    else {"redacted": REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER},
                }
                if isinstance(tc, Mapping)
                else REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER
                for tc in copied["tool_calls"]
            ]
        kwargs = copied.get("kwargs")
        if isinstance(kwargs, Mapping):
            new_kw = dict(kwargs)
            if "content" in new_kw:
                new_kw["content"] = REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER
            if isinstance(new_kw.get("tool_calls"), list):
                new_kw["tool_calls"] = [
                    {
                        "id": tc.get("id"),
                        "name": tc.get("name"),
                        "args": redact_personal_tool_args(tc.get("args"))
                        if is_personal_integration_tool(tc.get("name"))
                        else {"redacted": REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER},
                    }
                    if isinstance(tc, Mapping)
                    else REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER
                    for tc in new_kw["tool_calls"]
                ]
            # Preserve usage_metadata and response_metadata for LangSmith cost tracking
            copied["kwargs"] = new_kw
        return copied
    if isinstance(msg, list):
        return [_redact_serialized_message(item) for item in msg]
    if isinstance(msg, str):
        return REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER
    # Live BaseMessage instance
    msg_type = getattr(msg, "type", type(msg).__name__)
    return {
        "type": msg_type,
        "id": getattr(msg, "id", None),
        "name": getattr(msg, "name", None),
        "content": REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER,
    }


def redact_langsmith_tool_inputs(inputs: Any, tool_name: str | None = None) -> dict[str, Any]:
    """Redact inputs of a personal-integration tool run before LangSmith export."""
    if not isinstance(inputs, Mapping):
        return {
            "tool": tool_name or "personal_integration",
            "input": REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER,
            "redacted": True,
        }
    return {
        **redact_personal_tool_args(inputs),
        "redacted": True,
    }


def redact_langsmith_tool_outputs(outputs: Any) -> dict[str, Any]:
    """Redact outputs of a personal-integration tool run before LangSmith export."""
    _ = outputs
    return {
        "output": REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER,
        "redacted": True,
    }


def redact_langsmith_llm_inputs(inputs: Any) -> Any:
    """Redact LLM prompt/messages inputs once a turn touches personal integration data."""
    if not isinstance(inputs, Mapping):
        return {"redacted": REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER}
    out = dict(inputs)
    if "messages" in out:
        out["messages"] = _redact_serialized_message(out["messages"])
    if "prompts" in out:
        prompts = out["prompts"]
        if isinstance(prompts, list):
            out["prompts"] = [REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER for _ in prompts]
        else:
            out["prompts"] = REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER
    if "input" in out:
        out["input"] = REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER
    out["personal_integration_redacted"] = True
    return out


def redact_langsmith_llm_outputs(outputs: Any) -> Any:
    """Redact LLM generation text/content while preserving ``usage_metadata`` for cost accounting."""
    if not isinstance(outputs, Mapping):
        return outputs
    out = dict(outputs)
    gens = out.get("generations")
    if isinstance(gens, list):
        redacted_gens: list[Any] = []
        for group in gens:
            if not isinstance(group, list):
                redacted_gens.append(group)
                continue
            new_group: list[Any] = []
            for gen in group:
                if not isinstance(gen, Mapping):
                    new_group.append(gen)
                    continue
                g_copy = dict(gen)
                if "text" in g_copy:
                    g_copy["text"] = REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER
                if "message" in g_copy:
                    g_copy["message"] = _redact_serialized_message(g_copy["message"])
                new_group.append(g_copy)
            redacted_gens.append(new_group)
        out["generations"] = redacted_gens
    out["personal_integration_redacted"] = True
    return out


def redact_langsmith_chain_payload(payload: Any) -> Any:
    """Redact chain/graph state inputs or outputs when a turn holds personal integration data."""
    if not isinstance(payload, Mapping):
        return payload
    out = dict(payload)
    if "messages" in out:
        out["messages"] = REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER
    if "output" in out:
        out["output"] = REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER
    if "input" in out:
        out["input"] = REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER
    if isinstance(out.get("state"), Mapping):
        out["state"] = redact_langsmith_chain_payload(out["state"])
    out["personal_integration_redacted"] = True
    return out


__all__ = [
    "PERSONAL_INTEGRATION_TOOL_NAMES",
    "REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER",
    "is_personal_integration_tool",
    "messages_contain_personal_integration_data",
    "redact_langsmith_chain_payload",
    "redact_langsmith_llm_inputs",
    "redact_langsmith_llm_outputs",
    "redact_langsmith_tool_inputs",
    "redact_langsmith_tool_outputs",
    "redact_personal_tool_args",
]
