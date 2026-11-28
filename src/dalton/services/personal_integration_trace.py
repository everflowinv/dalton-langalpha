"""Redaction helpers for personal-integration tool payloads in traces and transcripts.

Ensures private calendar/mail tool arguments, tool outputs, and subsequent model
inputs/outputs carrying that context are stripped before leaving the backend to
LangSmith or being written into stored thread transcripts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

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


def _tool_calls_contain_personal(tool_calls: Any) -> bool:
    if not isinstance(tool_calls, (list, tuple)):
        return False
    for tc in tool_calls:
        if isinstance(tc, Mapping):
            if is_personal_integration_tool(tc.get("name")):
                return True
            fn = tc.get("function")
            if isinstance(fn, Mapping) and is_personal_integration_tool(fn.get("name")):
                return True
        else:
            if is_personal_integration_tool(getattr(tc, "name", None)):
                return True
    return False


def _content_blocks_contain_personal_tool(content: Any) -> bool:
    if not isinstance(content, (list, tuple)):
        return False
    for block in content:
        if isinstance(block, Mapping):
            if block.get("type") in ("tool_use", "tool_call") and is_personal_integration_tool(
                block.get("name")
            ):
                return True
    return False


def _message_obj_is_personal_tool(msg: Any) -> bool:
    if isinstance(msg, Mapping):
        if is_personal_integration_tool(msg.get("name")):
            return True
        if _tool_calls_contain_personal(msg.get("tool_calls")):
            return True
        if _content_blocks_contain_personal_tool(msg.get("content")):
            return True
        add_kw = msg.get("additional_kwargs")
        if isinstance(add_kw, Mapping) and _tool_calls_contain_personal(add_kw.get("tool_calls")):
            return True
        kwargs = msg.get("kwargs")
        if isinstance(kwargs, Mapping):
            if is_personal_integration_tool(kwargs.get("name")):
                return True
            if _tool_calls_contain_personal(kwargs.get("tool_calls")):
                return True
            if _content_blocks_contain_personal_tool(kwargs.get("content")):
                return True
            kw_add = kwargs.get("additional_kwargs")
            if isinstance(kw_add, Mapping) and _tool_calls_contain_personal(
                kw_add.get("tool_calls")
            ):
                return True
        return False

    name = getattr(msg, "name", None)
    if is_personal_integration_tool(name):
        return True
    if _tool_calls_contain_personal(getattr(msg, "tool_calls", None)):
        return True
    if _content_blocks_contain_personal_tool(getattr(msg, "content", None)):
        return True
    add_kw = getattr(msg, "additional_kwargs", None)
    if isinstance(add_kw, Mapping) and _tool_calls_contain_personal(add_kw.get("tool_calls")):
        return True
    return False


def llm_outputs_contain_personal_tool_call(outputs: Any) -> bool:
    """Return True iff serialized LLM outputs invoke any personal-integration tool."""
    if not isinstance(outputs, Mapping):
        return False
    gens = outputs.get("generations")
    if not isinstance(gens, (list, tuple)):
        return False
    for group in gens:
        if not isinstance(group, (list, tuple)):
            continue
        for gen in group:
            if not isinstance(gen, Mapping):
                continue
            if _message_obj_is_personal_tool(gen.get("message")):
                return True
    return False


def run_contains_personal_integration_tool(messages: Any) -> bool:
    """Return True iff any message in a turn invokes or returns from a personal-integration tool."""
    if not isinstance(messages, (list, tuple)):
        return False
    return any(_message_obj_is_personal_tool(m) for m in messages)


async def read_thread_first_personal_segment(
    thread_id: str | None,
    *,
    is_task: bool = False,
    conn: Any = None,
) -> int | None:
    """Return the 1-based transcript segment number from which redaction applies, or ``None``.

    Uses persisted ``conversation_threads.has_personal_integration_data`` and the
    earliest ``turn_index`` in ``dalton_thread_integration_touches`` so later turns
    and task transcripts never leak personal calendar/mail content.
    """
    from src.dalton.settings import is_dalton_enabled
    from src.server.database.pool import get_db_connection
    from src.server.utils.pg_sanitize import normalize_uuid

    if not is_dalton_enabled():
        return None
    tid = normalize_uuid(thread_id) if thread_id else None
    if tid is None:
        return None
    try:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT COALESCE(has_personal_integration_data, FALSE)
                    FROM conversation_threads
                    WHERE conversation_thread_id = %s::uuid
                    """,
                    (tid,),
                )
                row = await cur.fetchone()
                if not row or not bool(row[0]):
                    return None
                if is_task:
                    return 1
                await cur.execute(
                    """
                    SELECT MIN(turn_index)
                    FROM dalton_thread_integration_touches
                    WHERE thread_id = %s::uuid
                    """,
                    (tid,),
                )
                t_row = await cur.fetchone()
                if t_row is not None and t_row[0] is not None:
                    return max(1, int(t_row[0]) + 1)
                return 1
    except RuntimeError as exc:
        if "pool is not open" in str(exc).lower():
            return None
        raise


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


def redact_langsmith_generic_tool_inputs(
    inputs: Any, tool_name: str | None = None
) -> dict[str, Any]:
    """Redact inputs of ANY subsequent tool run once a trace holds personal integration data."""
    if not isinstance(inputs, Mapping):
        return {
            "tool": tool_name or "tool",
            "input": REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER,
            "redacted": True,
        }
    return {
        **{str(k): REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER for k in inputs.keys()},
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


def graph_has_personal_integration_tool(graph: Any) -> bool:
    """Return True if a compiled LangGraph / RunnableBinding mounts any personal-integration tool."""
    cur = graph
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        tools_by_name = getattr(cur, "tools_by_name", None)
        if isinstance(tools_by_name, Mapping) and any(
            is_personal_integration_tool(name) for name in tools_by_name.keys()
        ):
            return True
        nodes = getattr(cur, "nodes", None)
        if isinstance(nodes, Mapping):
            tools_node = nodes.get("tools")
            if tools_node is not None and graph_has_personal_integration_tool(tools_node):
                return True
        next_cur = getattr(cur, "bound", None)
        if next_cur is None:
            next_cur = getattr(cur, "graph", None)
        cur = next_cur
    return False


def is_personal_calendar_capability_enabled() -> bool:
    """Return True when Dalton and at least one personal calendar capability are enabled."""
    from src.dalton.capabilities import CapabilityId, effective_capabilities
    from src.dalton.settings import is_dalton_enabled

    if not is_dalton_enabled():
        return False
    caps = effective_capabilities()
    return (
        CapabilityId.GOOGLE_CALENDAR in caps
        or CapabilityId.FEISHU_CALENDAR in caps
    )


def should_defer_langsmith_export(config: Mapping[str, Any] | None) -> bool:
    """Whether a LangSmith tracer should buffer runs until turn end before exporting.

    True for interactive turns where personal calendar capabilities are enabled and the
    calendar tool is loaded (or default PTC interactive turns without an explicit
    ``personal_calendar_tool_loaded=False``), and the thread is not already marked private.
    """
    if not isinstance(config, Mapping):
        return False
    cfg_part = (
        config.get("configurable")
        if isinstance(config.get("configurable"), Mapping)
        else {}
    )
    meta_part = (
        config.get("metadata")
        if isinstance(config.get("metadata"), Mapping)
        else {}
    )
    if cfg_part.get("has_personal_integration_data") or meta_part.get(
        "has_personal_integration_data"
    ):
        return False
    if not is_personal_calendar_capability_enabled():
        return False

    from src.dalton.tools.calendar_tools import _is_unattended_automation_turn

    if _is_unattended_automation_turn(config):  # type: ignore[arg-type]
        return False

    loaded = cfg_part.get("personal_calendar_tool_loaded")
    if loaded is None:
        loaded = meta_part.get("personal_calendar_tool_loaded")
    if loaded is False:
        return False
    if loaded is True:
        return True

    return bool(
        meta_part.get("workflow_type") == "ptc_agent"
        or (cfg_part.get("user_id") and cfg_part.get("thread_id"))
    )


class PersonalIntegrationTraceGate:
    """Shared per-turn LangSmith trace buffer/suppression state for personal integrations.

    - Already-private threads drop all runs immediately (`private=True`).
    - Interactive turns where personal calendar is enabled & loaded buffer all
      `create_run` / `update_run` operations (`defer=True`) from turn start.
      If the turn touches personal calendar at any point, the entire buffered trace
      is discarded without ever hitting LangSmith; if the turn finishes without
      touching personal calendar, the buffered operations are flushed in order.
    """

    def __init__(self, *, private: bool = False, defer: bool = False) -> None:
        self.private_flag: list[bool] = [bool(private)]
        self.defer: bool = bool(defer) and not bool(private)
        self._active_roots: set[UUID] = set()
        self._buffer: list[tuple[str, Any, Any]] = []

    @property
    def is_private(self) -> bool:
        return self.private_flag[0]

    def mark_private(self) -> None:
        self.private_flag[0] = True
        self.defer = False
        for _, _, run in self._buffer:
            extra = getattr(run, "extra", None)
            if isinstance(extra, dict):
                extra["__disabled"] = True
        self._buffer.clear()
        self._active_roots.clear()

    def enable_defer(self) -> None:
        if not self.is_private:
            self.defer = True

    def on_trace_start(self, run: Any) -> None:
        if (
            self.is_private
            or (
                getattr(run, "run_type", None) == "tool"
                and is_personal_integration_tool(getattr(run, "name", None))
            )
            or messages_contain_personal_integration_data(getattr(run, "inputs", None))
        ):
            self.mark_private()
            run.extra["__disabled"] = True
            return
        if self.defer and getattr(run, "parent_run_id", None) is None:
            run_id = getattr(run, "id", None)
            if run_id is not None:
                self._active_roots.add(run_id)

    def on_llm_end(self, run: Any) -> None:
        if self.is_private or llm_outputs_contain_personal_tool_call(
            getattr(run, "outputs", None)
        ):
            self.mark_private()
            run.extra["__disabled"] = True

    def on_tool_finished(self, run: Any) -> None:
        if self.is_private or is_personal_integration_tool(getattr(run, "name", None)):
            self.mark_private()
            run.extra["__disabled"] = True

    def should_persist_immediately(self, tracer: Any, run: Any) -> bool:
        if self.is_private:
            run.extra["__disabled"] = True
            return False
        if self.defer:
            if getattr(run, "parent_run_id", None) is None:
                run_id = getattr(run, "id", None)
                if run_id is not None:
                    self._active_roots.add(run_id)
            self._buffer.append(("create", tracer, run))
            return False
        return True

    def should_update_immediately(self, tracer: Any, run: Any) -> bool:
        if self.is_private:
            run.extra["__disabled"] = True
            return False
        if self.defer:
            self._buffer.append(("update", tracer, run))
            return False
        return True

    def on_run_finished(
        self,
        run: Any,
        *,
        flush_create: Callable[[Any, Any], None],
        flush_update: Callable[[Any, Any], None],
    ) -> None:
        run_id = getattr(run, "id", None)
        if self.is_private:
            self.mark_private()
            return
        if not self.defer:
            return
        is_root = getattr(run, "parent_run_id", None) is None or (
            run_id is not None and run_id in self._active_roots
        )
        if not is_root:
            return
        if run_id is not None:
            self._active_roots.discard(run_id)
        if self._active_roots:
            return
        pending = list(self._buffer)
        self._buffer.clear()
        for op, tracer, buffered_run in pending:
            if self.is_private:
                return
            if op == "create":
                flush_create(tracer, buffered_run)
            else:
                flush_update(tracer, buffered_run)


__all__ = [
    "PERSONAL_INTEGRATION_TOOL_NAMES",
    "PersonalIntegrationTraceGate",
    "REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER",
    "graph_has_personal_integration_tool",
    "is_personal_calendar_capability_enabled",
    "is_personal_integration_tool",
    "llm_outputs_contain_personal_tool_call",
    "messages_contain_personal_integration_data",
    "read_thread_first_personal_segment",
    "redact_langsmith_chain_payload",
    "redact_langsmith_generic_tool_inputs",
    "redact_langsmith_llm_inputs",
    "redact_langsmith_llm_outputs",
    "redact_langsmith_tool_inputs",
    "redact_langsmith_tool_outputs",
    "redact_personal_tool_args",
    "run_contains_personal_integration_tool",
    "should_defer_langsmith_export",
]
