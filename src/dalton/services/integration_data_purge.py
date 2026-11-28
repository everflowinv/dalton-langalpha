"""Scheme 2A online personal-integration data purger (6 layers) and startup resume (§4.5(2)).

Re-entrant and idempotent: every layer can safely run again after a crash and
resume from ``dalton_integration_purge_ledger WHERE local_purge_status = 'running'``.
State lives exclusively in PostgreSQL; no process-local truth is kept.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
import inspect
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import anyio

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from ptc_agent.agent.middleware.background_subagent.redis_stream import (
    legacy_task_events_key,
    run_stream_key,
    task_meta_key,
    task_stream_key,
)
from ptc_agent.agent.middleware.compaction.utils import (
    COMPACTION_SUMMARY_KEY,
    SIDECAR_KEY,
    SUMMARIZED_KEY,
)
from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import (
    LocalPurgeStatus,
    PurgeLedgerRow,
    RemoteRevokeStatus,
    purge_lock_key,
    purge_thread_lock_key,
)
from src.dalton.integrations.lock_guard import (
    assert_no_advisory_lock_held,
    track_advisory_lock,
)
from src.llms.reasoning_payload import REASONING_KWARG_KEYS
from src.server.database import pool as db_pool
from src.server.database import session_lock
from src.server.database.pool import get_db_connection
from src.server.database.runs import lifecycle as run_lifecycle
from src.server.database.runs import subagent_runs
from src.server.services import writer_guard
from src.server.services.runs.cancel import publish_cancel_nudge
from src.server.services.runs.stream_writer import stream_key, stream_meta_key
from src.server.utils.checkpoint_helpers import get_checkpointer

if TYPE_CHECKING:
    from src.dalton.services.integration_revoke import RemoteRevoker

logger = logging.getLogger(__name__)

PURGED_PLACEHOLDER = "[已因断开个人连接清除相关个人数据]"
PURGED_TOOL_ARGS: dict[str, Any] = {"_purged": True}
PURGED_TOOL_ARGS_JSON = '{"_purged": true}'

@dataclass(frozen=True)
class TrackerPurgeSnapshot:
    """Pre-delete snapshot of the old tracker calendar connection for Layer 6b purge.

    Captured inside ``finish_disconnect_locked`` before ``delete_connection_upto_epoch``
    removes the connection row so local-only disconnects and accounts with remaining
    sibling services still have the old epoch, calendar ID, write scope, and tokens.
    """

    auth_epoch: int
    provider_account_id: str
    calendar_id: str | None
    granted_scopes: str
    can_write: bool
    access_token: str | None
    refresh_token: str | None
    expires_at: datetime | None

    def __repr__(self) -> str:
        return (
            f"TrackerPurgeSnapshot(auth_epoch={self.auth_epoch!r}, "
            f"provider_account_id={self.provider_account_id!r}, "
            f"can_write={self.can_write!r}, "
            f"has_access_token={bool(self.access_token)!r}, "
            f"has_refresh_token={bool(self.refresh_token)!r})"
        )


TrackerPurgeHandler = Callable[..., Awaitable[bool]]

# Extension point registered by Step 4 (earnings tracker calendar cleanup).
_tracker_purge_handler: TrackerPurgeHandler | None = None


def register_tracker_purge_handler(handler: TrackerPurgeHandler | None) -> None:
    """Register (or clear with ``None``) the Step 4 tracker calendar purge handler."""
    global _tracker_purge_handler
    _tracker_purge_handler = handler


def get_tracker_purge_handler() -> TrackerPurgeHandler | None:
    return _tracker_purge_handler


@dataclass
class LocalPurgeReport:
    purge_id: str
    skipped: bool = False
    threads_purged: list[str] = field(default_factory=list)
    checkpoints_updated: int = 0
    queries_redacted: int = 0
    steerings_redacted: int = 0
    outbox_rows_redacted: int = 0
    redis_keys_deleted: int = 0
    transcripts_deleted: int = 0
    search_rows_cleared: int = 0
    cards_deleted: int = 0
    store_rows_deleted: int = 0
    approvals_deleted: int = 0
    tracker_reconcile_needed: bool = False


# ---------------------------------------------------------------------------
# Pure Scheme 2A message / UI / interrupt redaction helpers (Layer 1)
# ---------------------------------------------------------------------------


def redact_tool_call_dict(tc: Mapping[str, Any]) -> dict[str, Any]:
    """Rewrite a LangChain ``tool_calls`` entry's args to ``{"_purged": True}``,
    preserving ``id``, ``name``, and ``type`` for replay pairing.
    """
    out: dict[str, Any] = {
        "name": tc.get("name", ""),
        "args": dict(PURGED_TOOL_ARGS),
        "id": tc.get("id"),
        "type": tc.get("type", "tool_call"),
    }
    return out


def redact_invalid_tool_call_dict(tc: Mapping[str, Any]) -> dict[str, Any]:
    """Rewrite an ``invalid_tool_calls`` entry's args, preserving ``id``, ``name``, ``type``."""
    return {
        "name": tc.get("name", ""),
        "args": PURGED_TOOL_ARGS_JSON,
        "id": tc.get("id"),
        "error": None,
        "type": tc.get("type", "invalid_tool_call"),
    }


def redact_additional_kwargs_tool_calls(raw_tcs: list[Any]) -> list[Any]:
    """Rewrite OpenAI-style ``additional_kwargs['tool_calls']`` arguments."""
    out: list[Any] = []
    for item in raw_tcs:
        if not isinstance(item, Mapping):
            out.append(item)
            continue
        new_item: dict[str, Any] = {
            k: v for k, v in item.items() if k in ("id", "type", "index")
        }
        fn = item.get("function")
        if isinstance(fn, Mapping):
            new_item["function"] = {
                "name": fn.get("name", ""),
                "arguments": PURGED_TOOL_ARGS_JSON,
            }
        elif "args" in item:
            new_item["args"] = dict(PURGED_TOOL_ARGS)
        out.append(new_item)
    return out


def redact_content_blocks(content: Any) -> Any:
    """Redact string or multimodal/structured content blocks to ``PURGED_PLACEHOLDER``
    while keeping tool-use block pairing metadata (``id``, ``name``, ``type``).
    """
    if isinstance(content, str):
        return PURGED_PLACEHOLDER
    if not isinstance(content, list):
        return PURGED_PLACEHOLDER
    out: list[Any] = []
    for block in content:
        if isinstance(block, str):
            out.append(PURGED_PLACEHOLDER)
            continue
        if not isinstance(block, Mapping):
            out.append(PURGED_PLACEHOLDER)
            continue
        btype = str(block.get("type") or "")
        if btype in ("tool_use", "server_tool_use", "tool_call"):
            cleaned: dict[str, Any] = {"type": btype}
            if "id" in block:
                cleaned["id"] = block["id"]
            if "name" in block:
                cleaned["name"] = block["name"]
            if "args" in block and "input" not in block:
                cleaned["args"] = dict(PURGED_TOOL_ARGS)
            else:
                cleaned["input"] = dict(PURGED_TOOL_ARGS)
            out.append(cleaned)
        elif btype in ("thinking", "reasoning", "redacted_thinking"):
            cleaned = {"type": btype}
            if "thinking" in block or btype == "thinking":
                cleaned["thinking"] = PURGED_PLACEHOLDER
            elif "reasoning" in block or btype == "reasoning":
                cleaned["reasoning"] = PURGED_PLACEHOLDER
            if "text" in block:
                cleaned["text"] = PURGED_PLACEHOLDER
            if "summary" in block:
                cleaned["summary"] = [{"type": "summary_text", "text": PURGED_PLACEHOLDER}]
            out.append(cleaned)
        elif btype == "tool_result":
            cleaned = {"type": btype, "content": PURGED_PLACEHOLDER}
            if "tool_use_id" in block:
                cleaned["tool_use_id"] = block["tool_use_id"]
            out.append(cleaned)
        else:
            cleaned = {"type": btype or "text", "text": PURGED_PLACEHOLDER}
            out.append(cleaned)
    return out


def _is_summary_msg(msg: Any) -> bool:
    if isinstance(msg, BaseMessage):
        ak = msg.additional_kwargs or {}
        return ak.get("lc_source") == "summarization"
    if isinstance(msg, Mapping):
        ak = msg.get("additional_kwargs") or {}
        return isinstance(ak, Mapping) and ak.get("lc_source") == "summarization"
    return False


def _summary_span_overlaps(msg: Any, min_turn: int, max_turn: int) -> bool:
    """Return True if a compaction summary message covers any turn in ``[min_turn, max_turn]``."""
    ak = (
        (msg.additional_kwargs or {})
        if isinstance(msg, BaseMessage)
        else (msg.get("additional_kwargs") or {})
        if isinstance(msg, Mapping)
        else {}
    )
    stamp = ak.get(SUMMARIZED_KEY) if isinstance(ak, Mapping) else None
    if not isinstance(stamp, Mapping):
        return False
    span = stamp.get("span")
    if (
        isinstance(span, (list, tuple))
        and len(span) == 2
        and isinstance(span[0], int)
        and isinstance(span[1], int)
    ):
        return int(span[1]) >= min_turn and int(span[0]) <= max_turn
    return False


def _msg_id(msg: Any) -> str | None:
    if isinstance(msg, BaseMessage):
        return str(msg.id) if msg.id is not None else None
    if isinstance(msg, Mapping):
        raw = msg.get("id")
        return str(raw) if raw is not None else None
    return None


def _context_summary_id(msg: Any) -> str | None:
    rm = (
        (msg.response_metadata or {})
        if isinstance(msg, BaseMessage)
        else (msg.get("response_metadata") or {})
        if isinstance(msg, Mapping)
        else {}
    )
    cs = rm.get(COMPACTION_SUMMARY_KEY) if isinstance(rm, Mapping) else None
    if isinstance(cs, Mapping) and cs.get("summary_message_id"):
        return str(cs["summary_message_id"])
    return None


def _redact_context_summary_dict(cs: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(cs)
    out["summary_text"] = PURGED_PLACEHOLDER
    out["summary_length"] = len(PURGED_PLACEHOLDER)
    return out


def _redact_reasoning_kwargs(ak: dict[str, Any]) -> None:
    """Redact suffix AIMessage reasoning fields using ``REASONING_KWARG_KEYS`` plus ``"thinking"``.

    String fields are replaced with ``PURGED_PLACEHOLDER``; structured values such as
    ``reasoning_details`` lists are deleted outright.
    """
    for rkey in (*REASONING_KWARG_KEYS, "thinking"):
        if rkey not in ak:
            continue
        val = ak[rkey]
        if isinstance(val, str):
            if val:
                ak[rkey] = PURGED_PLACEHOLDER
        elif val is not None:
            ak.pop(rkey, None)


def redact_message(
    msg: Any,
    *,
    in_suffix: bool,
    affected_summary_ids: set[str],
    min_turn: int,
    max_turn: int,
) -> tuple[Any, bool]:
    """Redact one message according to Scheme 2A. Returns ``(new_msg, changed)``.

    - If ``in_suffix`` is True, redacts full body, reasoning, tool_calls args,
      steering_delivered, tool output, and any compaction summary fields.
    - If ``in_suffix`` is False (pre-touch checkpoint), preserves pure research
      messages verbatim EXCEPT compaction summaries that merged suffix turns
      (copy 1: summary HumanMessage, copy 2: AIMessage.response_metadata.context_summary).
    """
    is_summary = _is_summary_msg(msg)
    mid = _msg_id(msg)
    summary_overlaps = is_summary and (
        in_suffix
        or (mid is not None and mid in affected_summary_ids)
        or _summary_span_overlaps(msg, min_turn, max_turn)
    )
    cs_id = _context_summary_id(msg)
    cs_affected = cs_id is not None and (in_suffix or cs_id in affected_summary_ids)

    if not in_suffix and not summary_overlaps and not cs_affected:
        return msg, False

    if isinstance(msg, BaseMessage):
        ak = dict(msg.additional_kwargs or {})
        rm = dict(msg.response_metadata or {})

        # Pre-touch AIMessage whose only overlap is a stamped context_summary
        if not in_suffix and not summary_overlaps and cs_affected:
            rm[COMPACTION_SUMMARY_KEY] = _redact_context_summary_dict(
                rm[COMPACTION_SUMMARY_KEY]
            )
            return msg.model_copy(update={"response_metadata": rm}), True

        # Summary HumanMessage (copy 1 of 3)
        if summary_overlaps:
            ak.pop(SIDECAR_KEY, None)
            if isinstance(ak.get("summarize_complete"), Mapping):
                sc = dict(ak["summarize_complete"])
                sc["summary_length"] = len(PURGED_PLACEHOLDER)
                ak["summarize_complete"] = sc
            return (
                msg.model_copy(
                    update={"content": PURGED_PLACEHOLDER, "additional_kwargs": ak}
                ),
                True,
            )

        # Suffix messages
        if isinstance(msg, HumanMessage):
            if isinstance(ak.get("steering_delivered"), Mapping):
                sd = dict(ak["steering_delivered"])
                if "content" in sd:
                    sd["content"] = PURGED_PLACEHOLDER
                if "text" in sd:
                    sd["text"] = PURGED_PLACEHOLDER
                ak["steering_delivered"] = sd
            return (
                msg.model_copy(
                    update={"content": PURGED_PLACEHOLDER, "additional_kwargs": ak}
                ),
                True,
            )

        if isinstance(msg, ToolMessage):
            update: dict[str, Any] = {
                "content": PURGED_PLACEHOLDER,
                "additional_kwargs": ak,
            }
            if getattr(msg, "artifact", None) is not None:
                update["artifact"] = None
            return msg.model_copy(update=update), True

        if isinstance(msg, AIMessage):
            new_content = redact_content_blocks(msg.content)
            new_tcs = [redact_tool_call_dict(tc) for tc in (msg.tool_calls or [])]
            new_invalid = [
                redact_invalid_tool_call_dict(tc)
                for tc in (msg.invalid_tool_calls or [])
            ]
            if isinstance(ak.get("tool_calls"), list):
                ak["tool_calls"] = redact_additional_kwargs_tool_calls(ak["tool_calls"])
            if isinstance(ak.get("function_call"), Mapping):
                ak["function_call"] = {
                    "name": ak["function_call"].get("name", ""),
                    "arguments": PURGED_TOOL_ARGS_JSON,
                }
            _redact_reasoning_kwargs(ak)
            if isinstance(rm.get(COMPACTION_SUMMARY_KEY), Mapping):
                rm[COMPACTION_SUMMARY_KEY] = _redact_context_summary_dict(
                    rm[COMPACTION_SUMMARY_KEY]
                )
            return (
                msg.model_copy(
                    update={
                        "content": new_content,
                        "tool_calls": new_tcs,
                        "invalid_tool_calls": new_invalid,
                        "additional_kwargs": ak,
                        "response_metadata": rm,
                    }
                ),
                True,
            )

        # Fallback for any other BaseMessage subtype in suffix
        return msg.model_copy(update={"content": PURGED_PLACEHOLDER}), True

    # Dict-shaped serialized message fallback
    if isinstance(msg, Mapping):
        out = dict(msg)
        ak = dict(out.get("additional_kwargs") or {})
        rm = dict(out.get("response_metadata") or {})
        if not in_suffix and not summary_overlaps and cs_affected:
            rm[COMPACTION_SUMMARY_KEY] = _redact_context_summary_dict(
                rm[COMPACTION_SUMMARY_KEY]
            )
            out["response_metadata"] = rm
            return out, True
        out["content"] = redact_content_blocks(out.get("content", ""))
        if isinstance(out.get("tool_calls"), list):
            out["tool_calls"] = [redact_tool_call_dict(tc) for tc in out["tool_calls"]]
        if isinstance(out.get("invalid_tool_calls"), list):
            out["invalid_tool_calls"] = [
                redact_invalid_tool_call_dict(tc) for tc in out["invalid_tool_calls"]
            ]
        if isinstance(ak.get("tool_calls"), list):
            ak["tool_calls"] = redact_additional_kwargs_tool_calls(ak["tool_calls"])
        _redact_reasoning_kwargs(ak)
        if summary_overlaps:
            ak.pop(SIDECAR_KEY, None)
        if isinstance(rm.get(COMPACTION_SUMMARY_KEY), Mapping):
            rm[COMPACTION_SUMMARY_KEY] = _redact_context_summary_dict(
                rm[COMPACTION_SUMMARY_KEY]
            )
        out["additional_kwargs"] = ak
        out["response_metadata"] = rm
        return out, True

    return msg, False


def _ui_record_id(record: Any) -> str | None:
    if isinstance(record, Mapping):
        raw = record.get("id")
        return str(raw) if raw is not None and str(raw) else None
    return None


def _collect_ui_records_from_value(value: Any) -> list[Any]:
    if isinstance(value, _DeltaSnapshot):
        return _collect_ui_records_from_value(value.value)
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, Mapping):
        return [value]
    return []


def _is_summary_id_affected(
    summary_id: str,
    *,
    affected_summary_ids: set[str],
    pre_touch_summary_ids: set[str] | None,
    fallback_in_suffix: bool,
) -> bool:
    if summary_id:
        if summary_id in affected_summary_ids:
            return True
        if pre_touch_summary_ids is not None:
            return summary_id not in pre_touch_summary_ids
        return fallback_in_suffix
    return fallback_in_suffix


def redact_ui_record(
    record: Any,
    *,
    in_suffix: bool,
    affected_summary_ids: set[str],
    pre_touch_summary_ids: set[str] | None = None,
) -> tuple[Any, bool]:
    """Redact copy 3 of compaction summaries in checkpoint ``ui`` records per entry
    (``metadata.replaced_summary.summary_text`` and ``props.summary_text``), plus
    text fields on suffix-born UI records while keeping pre-touch UI records intact.
    """
    if not isinstance(record, Mapping):
        return record, False
    metadata = record.get("metadata")
    props = record.get("props")
    meta_dict = dict(metadata) if isinstance(metadata, Mapping) else None
    props_dict = dict(props) if isinstance(props, Mapping) else None

    changed = False
    sum_id = str(meta_dict.get("summary_message_id") or "") if meta_dict else ""

    replaced = meta_dict.get("replaced_summary") if meta_dict else None
    if isinstance(replaced, Mapping):
        rep_id = str(replaced.get("id") or "")
        target_id = rep_id or sum_id
        if _is_summary_id_affected(
            target_id,
            affected_summary_ids=affected_summary_ids,
            pre_touch_summary_ids=pre_touch_summary_ids,
            fallback_in_suffix=in_suffix,
        ):
            new_rep = dict(replaced)
            if SIDECAR_KEY in new_rep:
                new_rep.pop(SIDECAR_KEY, None)
                changed = True
            if "summary_text" in new_rep or new_rep.get("action") == "summarize":
                new_rep["summary_text"] = PURGED_PLACEHOLDER
                new_rep["summary_length"] = len(PURGED_PLACEHOLDER)
                changed = True
            if changed:
                meta_dict["replaced_summary"] = new_rep  # type: ignore[index]
            if meta_dict is not None and SIDECAR_KEY in meta_dict:
                meta_dict.pop(SIDECAR_KEY, None)
                changed = True

    if props_dict is not None:
        if "summary_text" in props_dict or SIDECAR_KEY in props_dict:
            if _is_summary_id_affected(
                sum_id,
                affected_summary_ids=affected_summary_ids,
                pre_touch_summary_ids=pre_touch_summary_ids,
                fallback_in_suffix=in_suffix,
            ):
                if "summary_text" in props_dict:
                    props_dict["summary_text"] = PURGED_PLACEHOLDER
                    if "summary_length" in props_dict:
                        props_dict["summary_length"] = len(PURGED_PLACEHOLDER)
                    changed = True
                if SIDECAR_KEY in props_dict:
                    props_dict.pop(SIDECAR_KEY, None)
                    changed = True
                if meta_dict is not None and SIDECAR_KEY in meta_dict:
                    meta_dict.pop(SIDECAR_KEY, None)
                    changed = True
        if in_suffix:
            for tkey in ("text", "content", "message"):
                if isinstance(props_dict.get(tkey), str) and props_dict[tkey] != PURGED_PLACEHOLDER:
                    props_dict[tkey] = PURGED_PLACEHOLDER
                    changed = True

    if not changed:
        return record, False
    out = dict(record)
    if meta_dict is not None:
        out["metadata"] = meta_dict
    if props_dict is not None:
        out["props"] = props_dict
    return out, True


def redact_interrupt_payload(value: Any) -> Any:
    """Redact pending ``__interrupt__`` write payloads (keeping structural keys)."""
    if isinstance(value, str):
        return PURGED_PLACEHOLDER
    if isinstance(value, list):
        return [redact_interrupt_payload(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_interrupt_payload(v) for v in value)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in ("id", "type", "name", "interrupt_id", "tool_call_id", "ns"):
                out[k] = v
            elif k in ("args", "arguments", "params", "input", "tool_args"):
                out[k] = dict(PURGED_TOOL_ARGS)
            else:
                out[k] = redact_interrupt_payload(v)
        return out
    # LangGraph Interrupt dataclass/object with .value attribute
    if hasattr(value, "value"):
        try:
            new_val = redact_interrupt_payload(value.value)
            if hasattr(value, "model_copy"):
                return value.model_copy(update={"value": new_val})
            import dataclasses

            if dataclasses.is_dataclass(value):
                return dataclasses.replace(value, value=new_val)
        except Exception:
            return {"_purged": True, "text": PURGED_PLACEHOLDER}
    return value


def _collect_messages_from_value(value: Any) -> list[Any]:
    if isinstance(value, _DeltaSnapshot):
        return _collect_messages_from_value(value.value)
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, BaseMessage):
        return [value]
    return []


def _redact_messages_container(
    value: Any,
    *,
    pre_touch_msg_ids: set[str],
    affected_summary_ids: set[str],
    min_turn: int,
    max_turn: int,
    force_suffix: bool,
) -> tuple[Any, bool]:
    if isinstance(value, _DeltaSnapshot):
        inner, changed = _redact_messages_container(
            value.value,
            pre_touch_msg_ids=pre_touch_msg_ids,
            affected_summary_ids=affected_summary_ids,
            min_turn=min_turn,
            max_turn=max_turn,
            force_suffix=force_suffix,
        )
        return (_DeltaSnapshot(inner), True) if changed else (value, False)
    if isinstance(value, (list, tuple)):
        any_changed = False
        out: list[Any] = []
        for item in value:
            mid = _msg_id(item)
            in_suffix = force_suffix or (mid is None or mid not in pre_touch_msg_ids)
            redacted, ch = redact_message(
                item,
                in_suffix=in_suffix,
                affected_summary_ids=affected_summary_ids,
                min_turn=min_turn,
                max_turn=max_turn,
            )
            if ch:
                any_changed = True
            out.append(redacted)
        if not any_changed:
            return value, False
        return (tuple(out) if isinstance(value, tuple) else out), True
    mid = _msg_id(value)
    in_suffix = force_suffix or (mid is None or mid not in pre_touch_msg_ids)
    return redact_message(
        value,
        in_suffix=in_suffix,
        affected_summary_ids=affected_summary_ids,
        min_turn=min_turn,
        max_turn=max_turn,
    )


def _redact_ui_container(
    value: Any,
    *,
    affected_summary_ids: set[str],
    pre_touch_ui_ids: set[str] | None = None,
    pre_touch_summary_ids: set[str] | None = None,
    in_suffix: bool = False,
    force_suffix: bool = False,
) -> tuple[Any, bool]:
    if isinstance(value, _DeltaSnapshot):
        inner, changed = _redact_ui_container(
            value.value,
            affected_summary_ids=affected_summary_ids,
            pre_touch_ui_ids=pre_touch_ui_ids,
            pre_touch_summary_ids=pre_touch_summary_ids,
            in_suffix=in_suffix,
            force_suffix=force_suffix,
        )
        return (_DeltaSnapshot(inner), True) if changed else (value, False)

    def _rec_in_suffix(rec: Any) -> bool:
        if force_suffix:
            return True
        if pre_touch_ui_ids is not None:
            rid = _ui_record_id(rec)
            return rid is None or rid not in pre_touch_ui_ids
        return in_suffix

    if isinstance(value, list):
        any_changed = False
        out = []
        for rec in value:
            r2, ch = redact_ui_record(
                rec,
                in_suffix=_rec_in_suffix(rec),
                affected_summary_ids=affected_summary_ids,
                pre_touch_summary_ids=pre_touch_summary_ids,
            )
            if ch:
                any_changed = True
            out.append(r2)
        return (out, True) if any_changed else (value, False)
    return redact_ui_record(
        value,
        in_suffix=_rec_in_suffix(value),
        affected_summary_ids=affected_summary_ids,
        pre_touch_summary_ids=pre_touch_summary_ids,
    )


def _extract_summary_messages_from_event(value: Any) -> list[Any]:
    """Extract ``summary_message`` from a ``_summarization_event`` channel value."""
    if isinstance(value, _DeltaSnapshot):
        return _extract_summary_messages_from_event(value.value)
    if isinstance(value, Mapping):
        sm = value.get("summary_message")
        if sm is not None:
            return [sm]
    return []


def _redact_summary_message_entry(msg: Any) -> Any:
    """Replace a compaction summary message body with ``PURGED_PLACEHOLDER`` and drop ``SIDECAR_KEY``."""
    if isinstance(msg, BaseMessage):
        ak = dict(msg.additional_kwargs or {})
        ak.pop(SIDECAR_KEY, None)
        if isinstance(ak.get("summarize_complete"), Mapping):
            sc = dict(ak["summarize_complete"])
            sc["summary_length"] = len(PURGED_PLACEHOLDER)
            ak["summarize_complete"] = sc
        return msg.model_copy(
            update={"content": PURGED_PLACEHOLDER, "additional_kwargs": ak}
        )
    if isinstance(msg, Mapping):
        out = dict(msg)
        ak = dict(out.get("additional_kwargs") or {})
        ak.pop(SIDECAR_KEY, None)
        if isinstance(ak.get("summarize_complete"), Mapping):
            sc = dict(ak["summarize_complete"])
            sc["summary_length"] = len(PURGED_PLACEHOLDER)
            ak["summarize_complete"] = sc
        out["content"] = PURGED_PLACEHOLDER
        out["additional_kwargs"] = ak
        return out
    return msg


def _redact_summarization_event_container(
    value: Any,
    *,
    affected_summary_ids: set[str],
    pre_touch_summary_ids: set[str],
    min_turn: int,
    max_turn: int,
    force_suffix: bool = False,
    in_suffix: bool = False,
) -> tuple[Any, bool]:
    """Redact ``_summarization_event.summary_message`` when affected, keeping ``cutoff_index``,
    ``anchor_message_id``, and ``file_path`` intact. Pre-touch summaries are preserved verbatim.
    """
    if isinstance(value, _DeltaSnapshot):
        inner, changed = _redact_summarization_event_container(
            value.value,
            affected_summary_ids=affected_summary_ids,
            pre_touch_summary_ids=pre_touch_summary_ids,
            min_turn=min_turn,
            max_turn=max_turn,
            force_suffix=force_suffix,
            in_suffix=in_suffix,
        )
        return (_DeltaSnapshot(inner), True) if changed else (value, False)
    if not isinstance(value, Mapping):
        return value, False
    sm = value.get("summary_message")
    if sm is None:
        return value, False
    mid = _msg_id(sm)
    if mid is not None:
        affected = (
            force_suffix
            or _summary_span_overlaps(sm, min_turn, max_turn)
            or mid in affected_summary_ids
            or mid not in pre_touch_summary_ids
        )
    else:
        affected = (
            force_suffix
            or in_suffix
            or _summary_span_overlaps(sm, min_turn, max_turn)
        )
    if not affected:
        return value, False
    out = dict(value)
    out["summary_message"] = _redact_summary_message_entry(sm)
    return out, True


# ---------------------------------------------------------------------------
# Step 0: Cancel & drain in-flight runs on affected threads
# ---------------------------------------------------------------------------


async def _cancel_and_drain_thread(
    thread_id: str,
    *,
    min_turn: int,
    max_turn: int,
    timeout_s: float = 120.0,
) -> None:
    """Stamp durable cancellation on live runs/subagents on ``thread_id`` and wait for full WriterGuard drain.

    Always waits until exclusive ``writer_guard.thread_key`` can be acquired AND
    ``held_task_namespaces`` returns empty for ALL suffix subagent ``task_id`` values
    (not only currently active rows), closing the finalize-before-release window.

    Raises on cancellation failure or drain timeout so the ledger stays ``running``
    and the thread stays blocked until the resume loop retries.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT conversation_response_id::text AS run_id
                FROM conversation_responses
                WHERE conversation_thread_id = %s::uuid
                  AND status = 'in_progress'
                """,
                (thread_id,),
            )
            live_runs = [str(r["run_id"]) for r in await cur.fetchall()]

            await cur.execute(
                """
                SELECT sr.task_id, sr.task_run_id::text AS task_run_id, sr.status
                FROM subagent_runs sr
                LEFT JOIN conversation_responses r
                  ON r.conversation_response_id = sr.parent_run_id
                WHERE sr.thread_id = %s::uuid
                  AND (
                      r.turn_index IS NULL
                      OR r.turn_index BETWEEN %s AND %s
                  )
                """,
                (thread_id, min_turn, max_turn),
            )
            suffix_tasks = [dict(r) for r in await cur.fetchall()]

    for rid in live_runs:
        await run_lifecycle.request_run_cancel(
            rid, thread_id=thread_id, by="suspension"
        )
        assert_no_advisory_lock_held("purge.publish_cancel_nudge")
        await publish_cancel_nudge(thread_id, rid)

    suffix_task_ids: list[str] = []
    for task in suffix_tasks:
        tid_task = str(task.get("task_id") or "")
        trid = str(task.get("task_run_id") or "")
        task_status = task.get("status")
        if tid_task:
            suffix_task_ids.append(tid_task)
        if task_status in ("in_progress", "running"):
            if trid:
                await subagent_runs.request_task_run_cancel(trid, thread_id=thread_id)
            if tid_task:
                assert_no_advisory_lock_held("purge.publish_cancel_nudge")
                await publish_cancel_nudge(
                    thread_id, None, task_id=tid_task, task_run_id=trid or None
                )
    suffix_task_ids = sorted(set(suffix_task_ids))

    # Wait until exclusive T(thread) can be acquired AND no suffix task namespace is held.
    # Hold T(thread) on a dedicated out-of-pool session so held_task_namespaces() can still
    # borrow a pooled connection under POSTGRES_CONVERSATION_POOL_MAX=1 and closing the
    # session on exit guarantees T(thread) is released even under repeated cancellation.
    tkey = writer_guard.thread_key(thread_id)
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    lock_conn = await db_pool.open_session_connection()
    try:
        while True:
            cur = await lock_conn.execute("SELECT pg_try_advisory_lock(%s)", (tkey,))
            row = await cur.fetchone()
            if row and row[0]:
                try:
                    held = await writer_guard.held_task_namespaces(
                        thread_id, suffix_task_ids
                    )
                finally:
                    await session_lock.release_session_lock(lock_conn, tkey)
                if held is not None and len(held) == 0:
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.1, remaining))
    finally:
        await session_lock.await_settled(asyncio.ensure_future(lock_conn.close()))
    raise TimeoutError(
        f"Timed out waiting for writer drain on thread={thread_id} after {timeout_s:.1f}s"
    )


# ---------------------------------------------------------------------------
# Layer 1: Checkpoint pool redaction
# ---------------------------------------------------------------------------


def _resolve_checkpoint_turns(
    checkpoints: list[dict[str, Any]],
    run_turn_map: dict[str, int],
) -> dict[str, int | None]:
    """Assign each main-namespace ``checkpoint_id`` its ``turn_index`` via metadata & parent propagation."""
    by_id = {str(c["checkpoint_id"]): c for c in checkpoints}
    direct: dict[str, int | None] = {}
    for cid, c in by_id.items():
        meta = c.get("metadata") or {}
        ti = meta.get("turn_index")
        if isinstance(ti, int) and not isinstance(ti, bool):
            direct[cid] = ti
            continue
        rid = meta.get("run_id")
        if rid and str(rid) in run_turn_map:
            direct[cid] = run_turn_map[str(rid)]
            continue
        direct[cid] = None

    resolved: dict[str, int | None] = {}
    for cid in by_id:
        cur_id: str | None = cid
        seen: set[str] = set()
        found: int | None = None
        while cur_id and cur_id not in seen:
            seen.add(cur_id)
            if cur_id in resolved:
                found = resolved[cur_id]
                break
            if direct.get(cur_id) is not None:
                found = direct[cur_id]
                break
            parent = by_id.get(cur_id, {}).get("parent_checkpoint_id")
            cur_id = str(parent) if parent else None
        resolved[cid] = found
    return resolved


def _decode_checkpoint_payload(
    serde: Any,
    payload_type: str,
    payload_blob: Any,
    *,
    kind: str,
    thread_id: str,
    checkpoint_id: str,
    channel: str,
) -> Any:
    """Decode a checkpoint write/blob, stripping the raw exception chain on failure.

    Raising outside the ``except`` block guarantees neither ``__cause__`` nor
    ``__context__`` retains the underlying serializer exception (which may embed
    raw payload text slated for purge) when callers log with ``exc_info=True``.
    """
    err_type: str | None = None
    try:
        return serde.loads_typed((payload_type, bytes(payload_blob)))
    except Exception as exc:
        err_type = type(exc).__name__
    raise RuntimeError(
        f"Failed to decode checkpoint {kind} for thread_id={thread_id} "
        f"checkpoint_id={checkpoint_id} channel={channel} error_type={err_type}"
    )


async def _purge_thread_checkpoints(
    cp_conn: Any,
    serde: Any,
    *,
    thread_id: str,
    min_turn: int,
    max_turn: int,
    run_turn_map: dict[str, int],
    suffix_run_ids: set[str],
    suffix_task_ids: set[str],
) -> int:
    """Layer 1 checkpoint redaction for one thread (main namespace + suffix task namespaces)."""
    updated_count = 0
    async with cp_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                   checkpoint, metadata
            FROM checkpoints
            WHERE thread_id = %s
            ORDER BY checkpoint_ns ASC, checkpoint_id ASC
            """,
            (thread_id,),
        )
        all_cps = [dict(r) for r in await cur.fetchall()]
        if not all_cps:
            return 0

        main_cps = [c for c in all_cps if str(c["checkpoint_ns"]) == ""]
        cp_turns = _resolve_checkpoint_turns(main_cps, run_turn_map)

        pre_touch_cp_ids: set[str] = set()
        suffix_cp_ids: set[str] = set()
        post_range_cp_ids: set[str] = set()
        for c in main_cps:
            cid = str(c["checkpoint_id"])
            meta = c.get("metadata") or {}
            rid = str(meta.get("run_id") or "")
            ti = cp_turns.get(cid)
            if rid in suffix_run_ids or (ti is not None and min_turn <= ti <= max_turn):
                suffix_cp_ids.add(cid)
            elif ti is not None and ti < min_turn:
                pre_touch_cp_ids.add(cid)
            elif ti is not None and ti > max_turn:
                post_range_cp_ids.add(cid)
            else:
                # Unresolved checkpoint: if min_turn == 0 treat as suffix; otherwise preserve unless summary overlaps
                if min_turn == 0:
                    suffix_cp_ids.add(cid)
                else:
                    pre_touch_cp_ids.add(cid)

        # Determine which checkpoint_blobs versions are referenced by pre-touch / suffix / post-range checkpoints
        pre_blob_versions: set[tuple[str, str, str]] = set()
        suffix_blob_versions: set[tuple[str, str, str]] = set()
        post_blob_versions: set[tuple[str, str, str]] = set()
        blob_version_to_cp_id: dict[tuple[str, str, str], str] = {}
        suffix_task_namespaces = {f"task:{tid}" for tid in suffix_task_ids}

        for c in all_cps:
            ns = str(c["checkpoint_ns"])
            cid = str(c["checkpoint_id"])
            cp_obj = c.get("checkpoint") or {}
            ch_vers = cp_obj.get("channel_versions") if isinstance(cp_obj, Mapping) else {}
            if not isinstance(ch_vers, Mapping):
                continue
            for ch, ver in ch_vers.items():
                vkey = (ns, str(ch), str(ver))
                blob_version_to_cp_id.setdefault(vkey, cid)
                if ns == "":
                    if cid in pre_touch_cp_ids:
                        pre_blob_versions.add(vkey)
                    if cid in suffix_cp_ids:
                        suffix_blob_versions.add(vkey)
                    if cid in post_range_cp_ids:
                        post_blob_versions.add(vkey)

        # Load writes & blobs for this thread (including real compaction channel _summarization_event)
        await cur.execute(
            """
            SELECT checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob
            FROM checkpoint_writes
            WHERE thread_id = %s
              AND channel IN ('messages', 'ui', '__interrupt__', '_summarization_event')
              AND blob IS NOT NULL
            """,
            (thread_id,),
        )
        writes = [dict(r) for r in await cur.fetchall()]

        await cur.execute(
            """
            SELECT checkpoint_ns, channel, version, type, blob
            FROM checkpoint_blobs
            WHERE thread_id = %s
              AND channel IN ('messages', 'ui', '_summarization_event')
              AND blob IS NOT NULL
            """,
            (thread_id,),
        )
        blobs = [dict(r) for r in await cur.fetchall()]

        # Pass 1a: collect every message id, summary id, and ui record id present in any pre-touch checkpoint
        # (including blob versions shared between pre-touch and suffix checkpoints)
        pre_touch_msg_ids: set[str] = set()
        suffix_seen_msg_ids: set[str] = set()
        post_range_seen_msg_ids: set[str] = set()
        pre_touch_summary_ids: set[str] = set()
        pre_touch_ui_ids: set[str] = set()
        affected_summary_ids: set[str] = set()

        def _collect_pre_msgs(msgs: list[Any]) -> None:
            for m in msgs:
                mid = _msg_id(m)
                if mid:
                    pre_touch_msg_ids.add(mid)
                    if _is_summary_msg(m):
                        pre_touch_summary_ids.add(mid)
                cs_id = _context_summary_id(m)
                if cs_id:
                    pre_touch_summary_ids.add(cs_id)

        def _collect_pre_summarization_event(val: Any) -> None:
            for sm in _extract_summary_messages_from_event(val):
                mid = _msg_id(sm)
                if mid:
                    pre_touch_msg_ids.add(mid)
                    pre_touch_summary_ids.add(mid)

        def _collect_pre_ui(recs: list[Any]) -> None:
            for rec in recs:
                rid = _ui_record_id(rec)
                if rid:
                    pre_touch_ui_ids.add(rid)
                if isinstance(rec, Mapping):
                    meta = rec.get("metadata")
                    if isinstance(meta, Mapping):
                        sid = str(meta.get("summary_message_id") or "")
                        if sid:
                            pre_touch_summary_ids.add(sid)
                        rep = meta.get("replaced_summary")
                        if isinstance(rep, Mapping):
                            rep_id = str(rep.get("id") or "")
                            if rep_id:
                                pre_touch_summary_ids.add(rep_id)

        def _collect_affected_msgs(msgs: list[Any], *, force_suffix: bool) -> None:
            for m in msgs:
                mid = _msg_id(m)
                if _is_summary_msg(m):
                    if (
                        force_suffix
                        or _summary_span_overlaps(m, min_turn, max_turn)
                        or (mid is not None and mid not in pre_touch_summary_ids)
                    ):
                        if mid:
                            affected_summary_ids.add(mid)
                cs_id = _context_summary_id(m)
                if cs_id and (force_suffix or cs_id not in pre_touch_summary_ids):
                    affected_summary_ids.add(cs_id)

        def _collect_affected_summarization_event(val: Any, *, force_suffix: bool) -> None:
            for sm in _extract_summary_messages_from_event(val):
                mid = _msg_id(sm)
                if (
                    force_suffix
                    or _summary_span_overlaps(sm, min_turn, max_turn)
                    or (mid is not None and mid not in pre_touch_summary_ids)
                ):
                    if mid:
                        affected_summary_ids.add(mid)

        def _collect_affected_ui(recs: list[Any], *, force_suffix: bool) -> None:
            for rec in recs:
                if not isinstance(rec, Mapping):
                    continue
                meta = rec.get("metadata")
                if not isinstance(meta, Mapping):
                    continue
                sid = str(meta.get("summary_message_id") or "")
                if sid and (force_suffix or sid not in pre_touch_summary_ids):
                    affected_summary_ids.add(sid)
                rep = meta.get("replaced_summary")
                if isinstance(rep, Mapping):
                    rep_id = str(rep.get("id") or "")
                    if rep_id and (force_suffix or rep_id not in pre_touch_summary_ids):
                        affected_summary_ids.add(rep_id)

        decoded_writes: list[tuple[dict[str, Any], Any]] = []
        for w in writes:
            if not w["type"] or w["type"] == "empty":
                continue
            val = _decode_checkpoint_payload(
                serde,
                w["type"],
                w["blob"],
                kind="write",
                thread_id=thread_id,
                checkpoint_id=str(w["checkpoint_id"]),
                channel=str(w["channel"]),
            )
            decoded_writes.append((w, val))
            ns = str(w["checkpoint_ns"])
            cid = str(w["checkpoint_id"])
            if ns == "" and w["channel"] == "messages":
                if cid in suffix_cp_ids:
                    for m in _collect_messages_from_value(val):
                        mid = _msg_id(m)
                        if mid:
                            suffix_seen_msg_ids.add(mid)
                if cid in post_range_cp_ids:
                    for m in _collect_messages_from_value(val):
                        mid = _msg_id(m)
                        if mid:
                            post_range_seen_msg_ids.add(mid)
            is_pre = ns == "" and cid in pre_touch_cp_ids
            if not is_pre:
                continue
            if w["channel"] == "messages":
                _collect_pre_msgs(_collect_messages_from_value(val))
            elif w["channel"] == "ui":
                _collect_pre_ui(_collect_ui_records_from_value(val))
            elif w["channel"] == "_summarization_event":
                _collect_pre_summarization_event(val)

        decoded_blobs: list[tuple[dict[str, Any], Any]] = []
        for b in blobs:
            if not b["type"] or b["type"] == "empty":
                continue
            key = (str(b["checkpoint_ns"]), str(b["channel"]), str(b["version"]))
            ref_cid = blob_version_to_cp_id.get(key, str(b["version"]))
            val = _decode_checkpoint_payload(
                serde,
                b["type"],
                b["blob"],
                kind="blob",
                thread_id=thread_id,
                checkpoint_id=ref_cid,
                channel=str(b["channel"]),
            )
            decoded_blobs.append((b, val))
            if b["channel"] == "messages":
                if key in suffix_blob_versions:
                    for m in _collect_messages_from_value(val):
                        mid = _msg_id(m)
                        if mid:
                            suffix_seen_msg_ids.add(mid)
                if key in post_blob_versions:
                    for m in _collect_messages_from_value(val):
                        mid = _msg_id(m)
                        if mid:
                            post_range_seen_msg_ids.add(mid)
            if key not in pre_blob_versions:
                continue
            if b["channel"] == "messages":
                _collect_pre_msgs(_collect_messages_from_value(val))
            elif b["channel"] == "ui":
                _collect_pre_ui(_collect_ui_records_from_value(val))
            elif b["channel"] == "_summarization_event":
                _collect_pre_summarization_event(val)

        # Preserve messages strictly before min_turn AND messages born strictly after max_turn
        if post_range_seen_msg_ids:
            pre_touch_msg_ids.update(post_range_seen_msg_ids - suffix_seen_msg_ids)

        # Pass 1b: determine affected summary ids (span overlaps [min_turn, max_turn] OR not in pre-touch)
        for w, val in decoded_writes:
            ns = str(w["checkpoint_ns"])
            force_suf = ns in suffix_task_namespaces
            if w["channel"] == "messages":
                _collect_affected_msgs(_collect_messages_from_value(val), force_suffix=force_suf)
            elif w["channel"] == "ui":
                _collect_affected_ui(_collect_ui_records_from_value(val), force_suffix=force_suf)
            elif w["channel"] == "_summarization_event":
                _collect_affected_summarization_event(val, force_suffix=force_suf)

        for b, val in decoded_blobs:
            ns = str(b["checkpoint_ns"])
            force_suf = ns in suffix_task_namespaces
            if b["channel"] == "messages":
                _collect_affected_msgs(_collect_messages_from_value(val), force_suffix=force_suf)
            elif b["channel"] == "ui":
                _collect_affected_ui(_collect_ui_records_from_value(val), force_suffix=force_suf)
            elif b["channel"] == "_summarization_event":
                _collect_affected_summarization_event(val, force_suffix=force_suf)

        # Pass 2: rewrite checkpoint_writes
        for w, val in decoded_writes:
            ns = str(w["checkpoint_ns"])
            cid = str(w["checkpoint_id"])
            ch = str(w["channel"])
            is_suf = (ns == "" and cid in suffix_cp_ids) or (ns in suffix_task_namespaces)
            force_suf = ns in suffix_task_namespaces

            changed = False
            new_val = val
            if ch == "messages":
                new_val, changed = _redact_messages_container(
                    val,
                    pre_touch_msg_ids=pre_touch_msg_ids,
                    affected_summary_ids=affected_summary_ids,
                    min_turn=min_turn,
                    max_turn=max_turn,
                    force_suffix=force_suf,
                )
            elif ch == "ui":
                new_val, changed = _redact_ui_container(
                    val,
                    affected_summary_ids=affected_summary_ids,
                    pre_touch_ui_ids=pre_touch_ui_ids,
                    pre_touch_summary_ids=pre_touch_summary_ids,
                    force_suffix=force_suf,
                )
            elif ch == "_summarization_event":
                new_val, changed = _redact_summarization_event_container(
                    val,
                    affected_summary_ids=affected_summary_ids,
                    pre_touch_summary_ids=pre_touch_summary_ids,
                    min_turn=min_turn,
                    max_turn=max_turn,
                    force_suffix=force_suf,
                    in_suffix=is_suf,
                )
            elif ch == "__interrupt__" and is_suf:
                new_val = redact_interrupt_payload(val)
                changed = True

            if changed:
                new_type, new_blob = serde.dumps_typed(new_val)
                await cur.execute(
                    """
                    UPDATE checkpoint_writes
                    SET type = %s, blob = %s
                    WHERE thread_id = %s
                      AND checkpoint_ns = %s
                      AND checkpoint_id = %s
                      AND task_id = %s
                      AND idx = %s
                    """,
                    (
                        new_type,
                        new_blob,
                        thread_id,
                        ns,
                        cid,
                        w["task_id"],
                        w["idx"],
                    ),
                )
                updated_count += 1

        # Pass 3: rewrite checkpoint_blobs per entry (shared versions redact only affected entries)
        for b, val in decoded_blobs:
            ns = str(b["checkpoint_ns"])
            ch = str(b["channel"])
            ver = str(b["version"])
            key = (ns, ch, ver)
            force_suf = ns in suffix_task_namespaces

            changed = False
            new_val = val
            if ch == "messages":
                new_val, changed = _redact_messages_container(
                    val,
                    pre_touch_msg_ids=pre_touch_msg_ids,
                    affected_summary_ids=affected_summary_ids,
                    min_turn=min_turn,
                    max_turn=max_turn,
                    force_suffix=force_suf,
                )
            elif ch == "ui":
                new_val, changed = _redact_ui_container(
                    val,
                    affected_summary_ids=affected_summary_ids,
                    pre_touch_ui_ids=pre_touch_ui_ids,
                    pre_touch_summary_ids=pre_touch_summary_ids,
                    force_suffix=force_suf,
                )
            elif ch == "_summarization_event":
                new_val, changed = _redact_summarization_event_container(
                    val,
                    affected_summary_ids=affected_summary_ids,
                    pre_touch_summary_ids=pre_touch_summary_ids,
                    min_turn=min_turn,
                    max_turn=max_turn,
                    force_suffix=force_suf,
                    in_suffix=key not in pre_blob_versions,
                )

            if changed:
                new_type, new_blob = serde.dumps_typed(new_val)
                await cur.execute(
                    """
                    UPDATE checkpoint_blobs
                    SET type = %s, blob = %s
                    WHERE thread_id = %s
                      AND checkpoint_ns = %s
                      AND channel = %s
                      AND version = %s
                    """,
                    (new_type, new_blob, thread_id, ns, ch, ver),
                )
                updated_count += 1

        # Pass 4: delete unreferenced checkpoint_blobs & orphaned checkpoint_writes for this thread
        await cur.execute(
            """
            DELETE FROM checkpoint_blobs b
            WHERE b.thread_id = %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM checkpoints c
                  CROSS JOIN LATERAL jsonb_each_text(c.checkpoint->'channel_versions') v
                  WHERE c.thread_id = b.thread_id
                    AND c.checkpoint_ns = b.checkpoint_ns
                    AND v.key = b.channel
                    AND v.value = b.version
              )
            """,
            (thread_id,),
        )
        await cur.execute(
            """
            DELETE FROM checkpoint_writes w
            WHERE w.thread_id = %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM checkpoints c
                  WHERE c.thread_id = w.thread_id
                    AND c.checkpoint_ns = w.checkpoint_ns
                    AND c.checkpoint_id = w.checkpoint_id
              )
            """,
            (thread_id,),
        )

    return updated_count


def _redact_steering_inputs_list(items: Any) -> tuple[Any, bool]:
    """Redact ``conversation_responses.metadata.steering_inputs`` entries."""
    if not isinstance(items, list) or not items:
        return items, False
    out: list[Any] = []
    changed = False
    for entry in items:
        if isinstance(entry, Mapping):
            new_e = dict(entry)
            for key in ("content", "text", "message"):
                if key in new_e and new_e[key] != PURGED_PLACEHOLDER:
                    new_e[key] = PURGED_PLACEHOLDER
                    changed = True
            out.append(new_e)
        elif isinstance(entry, str) and entry != PURGED_PLACEHOLDER:
            out.append(PURGED_PLACEHOLDER)
            changed = True
        else:
            out.append(entry)
    return out, changed


# ---------------------------------------------------------------------------
# Layer 2 Redis key deletion helper
# ---------------------------------------------------------------------------


def collect_redis_keys_for_suffix(
    thread_id: str,
    suffix_run_ids: list[str],
    suffix_task_ids: list[str],
    suffix_task_run_ids: list[str],
) -> list[str]:
    """Build exact Redis stream & meta keys using existing upstream builders (no hardcoded strings)."""
    keys: list[str] = []
    for rid in suffix_run_ids:
        keys.append(stream_key(thread_id, rid))
        keys.append(stream_meta_key(thread_id, rid))
    for task_id in suffix_task_ids:
        keys.append(task_stream_key(thread_id, task_id))
        keys.append(task_meta_key(thread_id, task_id))
        keys.append(legacy_task_events_key(thread_id, task_id))
    for task_run_id in suffix_task_run_ids:
        keys.append(run_stream_key(thread_id, task_run_id))
    return list(dict.fromkeys(keys))


def is_redis_configured_for_purge() -> bool:
    """Return True if Redis cache is enabled by deployment configuration (config.yaml + REDIS_CACHE_ENABLED)."""
    from src.config.settings import is_redis_cache_enabled

    cache_enabled_env = os.getenv("REDIS_CACHE_ENABLED", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    return bool(is_redis_cache_enabled() and cache_enabled_env)


async def _resolve_redis_client_for_purge() -> Any:
    """Return a live Redis client when Redis is configured enabled for purge.

    If startup ``init_cache()`` failed during a transient outage,
    ``RedisCacheClient.connect()`` leaves ``cache.enabled = False`` on the
    singleton. Probe the existing client (or retry ``connect()`` if no client
    was created yet) so the same worker recovers and deletes keys once Redis
    is reachable again.
    """
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if cache.enabled and cache.client is not None:
        return cache.client

    client = getattr(cache, "client", None)
    ping_fn = getattr(client, "ping", None)
    if client is not None and callable(ping_fn):
        try:
            await ping_fn()
        except Exception as exc:
            raise RuntimeError(
                "Redis cache is configured enabled but client is unavailable for purge key deletion"
            ) from exc
        cache.enabled = True
        return client

    connect_fn = getattr(cache, "connect", None)
    if callable(connect_fn):
        cache.enabled = True
        try:
            await connect_fn()
        except Exception as exc:
            cache.enabled = False
            raise RuntimeError(
                "Redis cache is configured enabled but client is unavailable for purge key deletion"
            ) from exc
        if cache.enabled and getattr(cache, "client", None) is not None:
            return cache.client

    raise RuntimeError(
        "Redis cache is configured enabled but client is unavailable for purge key deletion"
    )


async def _delete_redis_keys(keys: list[str]) -> int:
    if not keys:
        return 0
    if not is_redis_configured_for_purge():
        return 0
    client = await _resolve_redis_client_for_purge()
    deleted = await client.delete(*keys)
    return int(deleted or 0)


# ---------------------------------------------------------------------------
# Layer 5: Conversation cards fallback & BaseStore memory cleanup
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ConversationCardDigestPlan:
    """Deferred conversation archive digest redraw plan collected inside the thread tx."""

    thread_id: str
    card_ids: list[str]
    affected_dates: list[str]
    retracted_stubs: list[dict[str, Any]]


async def _verify_purge_attempt_and_thread_blocked_tx(
    conn: Any,
    *,
    purge_id: str,
    claimed_attempt_gen: int | None,
    thread_id: str | None = None,
) -> bool:
    """Verify inside an open App DB transaction that ``purge_id`` is still ``running`` at
    ``claimed_attempt_gen`` (``FOR SHARE``) and, when ``thread_id`` is provided, that
    ``conversation_threads.personal_purge_in_progress`` is still ``TRUE`` (``FOR SHARE``).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        if claimed_attempt_gen is not None:
            await cur.execute(
                """
                SELECT local_purge_status, local_purge_attempt_gen
                FROM dalton_integration_purge_ledger
                WHERE purge_id = %s::uuid
                FOR SHARE
                """,
                (str(purge_id),),
            )
            lrow = await cur.fetchone()
            if (
                lrow is None
                or str(lrow["local_purge_status"]) != LocalPurgeStatus.RUNNING.value
                or int(lrow["local_purge_attempt_gen"]) != int(claimed_attempt_gen)
            ):
                return False

        if thread_id is not None:
            await cur.execute(
                """
                SELECT personal_purge_in_progress
                FROM conversation_threads
                WHERE conversation_thread_id = %s::uuid
                FOR SHARE
                """,
                (str(thread_id),),
            )
            trow = await cur.fetchone()
            if trow is None or not bool(trow.get("personal_purge_in_progress")):
                return False

    return True


async def _collect_conversation_card_purge_plan(
    conn: Any,
    thread_id: str,
) -> _ConversationCardDigestPlan | None:
    """Register historical archived cards for ``thread_id`` as ``retracted`` inside the
    thread transaction and return the digest locations to redraw AFTER releasing all
    advisory locks and DB connections.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT to_regclass('public.dalton_conversation_cards') AS tbl")
        tbl = await cur.fetchone()
        if not tbl or not tbl.get("tbl"):
            return None

        await cur.execute(
            """
            SELECT card_id, date_et, rendered_locations, tickers
            FROM dalton_conversation_cards
            WHERE session_id = %s
               OR session_key LIKE %s
            """,
            (thread_id, f"%{thread_id}%"),
        )
        matched = [dict(r) for r in await cur.fetchall()]
        if not matched:
            return None

        card_ids = [str(r["card_id"]) for r in matched]
        affected_dates = sorted(
            {str(r["date_et"])[:10] for r in matched if r.get("date_et") is not None}
        )
        # Mark matched cards retracted inside the transaction without touching sandboxes;
        # final DELETE happens in a short transaction strictly after sandbox redraw succeeds.
        await cur.execute(
            """
            UPDATE dalton_conversation_cards
            SET status = 'retracted'
            WHERE card_id = ANY(%s::text[])
            """,
            (card_ids,),
        )

    retracted_stubs = [
        {
            "card_id": str(r["card_id"]),
            "date_et": str(r["date_et"])[:10],
            "status": "retracted",
            "tickers": list(r.get("tickers") or []),
            "rendered_locations": list(r.get("rendered_locations") or []),
        }
        for r in matched
        if r.get("rendered_locations")
    ]
    return _ConversationCardDigestPlan(
        thread_id=thread_id,
        card_ids=card_ids,
        affected_dates=affected_dates,
        retracted_stubs=retracted_stubs,
    )


async def _execute_conversation_card_digest_redraw(
    plan: _ConversationCardDigestPlan,
    *,
    user_id: str,
) -> None:
    """Re-render / remove archived conversation Markdown digests strictly OUTSIDE all
    PostgreSQL advisory locks and open DB transactions.
    """
    assert_no_advisory_lock_held("purge.render_conversation_digests")
    remaining: list[dict[str, Any]] = []
    if plan.affected_dates:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT card_id, source_type, agent, session_id, session_key, generation,
                           channel, chat_type, event_ids, seq_range, ts, date_et, speaker,
                           speaker_role, tickers, entities, sectors, topic_label, type, claim,
                           figures, quote, quote_event_id, cited_sources, confidence, status,
                           supersedes, extractor_model, prompt_version, content_hash,
                           search_text, rendered_locations, files_written_at, created_at
                    FROM dalton_conversation_cards
                    WHERE date_et::text = ANY(%s::text[])
                      AND NOT (card_id = ANY(%s::text[]))
                    """,
                    (plan.affected_dates, plan.card_ids),
                )
                remaining = [dict(r) for r in await cur.fetchall()]

    from src.dalton.conversations.render import (
        render_and_write_conversation_digests,
    )

    # Pass conn=None so render.py acquires and releases its own short DB connections
    # and NEVER holds an open pool connection across sandbox I/O.
    await render_and_write_conversation_digests(
        [*remaining, *plan.retracted_stubs],
        target_dates=plan.affected_dates,
        user_id=user_id,
        conn=None,
    )


async def _purge_store_memories_for_thread(cp_conn: Any, thread_id: str) -> int:
    """Delete LangGraph ``BaseStore`` memory rows referencing ``thread_id``."""
    async with cp_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT to_regclass('public.store') AS tbl")
        tbl = await cur.fetchone()
        if not tbl or not tbl.get("tbl"):
            return 0
        await cur.execute(
            """
            DELETE FROM store
            WHERE (
                prefix LIKE '%%.memory%%'
                OR prefix LIKE 'memory%%'
            ) AND (
                key LIKE %s
                OR value->>'thread_id' = %s
                OR value->>'source_thread_id' = %s
                OR value::text LIKE %s
            )
            """,
            (f"%{thread_id}%", thread_id, thread_id, f"%{thread_id}%"),
        )
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# Main 2A Purger entry point
# ---------------------------------------------------------------------------


async def _resolve_thread_ranges(
    ledger: PurgeLedgerRow,
) -> dict[str, tuple[int, int]]:
    """Compute ``{thread_id: (min_turn, max_turn)}`` from ledger snapshot + unpurged touches."""
    ranges: dict[str, tuple[int, int]] = {}
    for tid, val in (ledger.purged_max_turn_by_thread or {}).items():
        if isinstance(val, Mapping) and "min_turn" in val and "max_turn" in val:
            ranges[str(tid)] = (int(val["min_turn"]), int(val["max_turn"]))
        elif isinstance(val, int):
            ranges[str(tid)] = (0, int(val))

    # Fill in min_turn from unpurged touches if any entry only stored an int max_turn
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            service_filter = "AND service = %(service)s" if ledger.service is not None else ""
            await cur.execute(
                f"""
                SELECT thread_id::text AS thread_id,
                       MIN(turn_index) AS min_turn,
                       MAX(turn_index) AS max_turn
                FROM dalton_thread_integration_touches
                WHERE user_id = %(user_id)s
                  AND provider = %(provider)s
                  {service_filter}
                  AND auth_epoch <= %(auth_epoch_upto)s
                  AND purged_at IS NULL
                GROUP BY thread_id
                """,
                {
                    "user_id": ledger.user_id,
                    "provider": ledger.provider,
                    "service": ledger.service,
                    "auth_epoch_upto": ledger.auth_epoch_upto,
                },
            )
            for row in await cur.fetchall():
                tid = str(row["thread_id"])
                lo = int(row["min_turn"])
                hi = int(row["max_turn"])
                if tid in ranges:
                    cur_lo, cur_hi = ranges[tid]
                    ranges[tid] = (min(cur_lo, lo), max(cur_hi, hi))
                else:
                    ranges[tid] = (lo, hi)
    return ranges


@asynccontextmanager
async def try_purge_ledger_lock(purge_id: str) -> AsyncIterator[bool]:
    """Acquire the per-ledger advisory lock on a dedicated out-of-pool session."""
    lock_key = purge_lock_key(purge_id)
    async with session_lock.try_own_session_lock(lock_key) as acquired:
        if acquired:
            with track_advisory_lock("purge_ledger", lock_key):
                yield True
        else:
            yield False


@asynccontextmanager
async def acquire_purge_thread_lock(
    thread_id: str,
    *,
    timeout_s: float = 120.0,
    poll_interval_s: float = 0.05,
) -> AsyncIterator[None]:
    """Hold per-thread purge mutex on a dedicated out-of-pool session until ``timeout_s``."""
    lock_key = purge_thread_lock_key(thread_id)
    conn = await db_pool.open_session_connection()
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    try:
        while True:
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
            row = await cur.fetchone()
            if row and row[0]:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out waiting for purge thread lock on thread={thread_id} after {timeout_s:.1f}s"
                )
            await asyncio.sleep(min(poll_interval_s, remaining))
        with track_advisory_lock("purge_thread", lock_key):
            yield
    finally:
        await session_lock.await_settled(asyncio.ensure_future(conn.close()))


async def run_local_purge(
    purge_id: str,
    *,
    cancel_drain_timeout_s: float = 120.0,
    _lock_held: bool = False,
    _claimed_attempt_gen: int | None = None,
    lease_seconds: int = 300,
    tracker_snapshot: TrackerPurgeSnapshot | None = None,
) -> LocalPurgeReport:
    """Execute the 6-layer Scheme 2A local online purge for ``purge_id`` (idempotent & re-entrant).

    Coordinates via durable ``local_purge_attempt_gen`` + ``local_purge_inflight_until``
    claims on ``dalton_integration_purge_ledger`` so no PostgreSQL advisory lock or
    dedicated session connection is ever held across Redis cancel nudges or stream key
    deletions. Raises if any required layer fails (and releases the durable lease for
    immediate retry) while keeping ``local_purge_status = 'running'`` and threads blocked.

    Commits thread unblocking (``personal_purge_in_progress = FALSE`` only when no other
    running ledger still covers the thread + second ``personal_purge_generation`` bump +
    ``purged_at = NOW()`` + ``local_purge_status = 'completed'``) atomically in one
    ``FOR UPDATE`` transaction after re-verifying ``local_purge_attempt_gen``.
    """
    claimed_gen = _claimed_attempt_gen
    if not _lock_held:
        async with try_purge_ledger_lock(purge_id) as acquired:
            if not acquired:
                return LocalPurgeReport(purge_id=str(purge_id), skipped=True)
            ledger = await db.get_purge_ledger(purge_id, decrypt_revoke_token=False)
            if ledger is None:
                raise LookupError(f"Purge ledger {purge_id} not found")
            if ledger.local_purge_status != LocalPurgeStatus.RUNNING:
                return LocalPurgeReport(purge_id=str(purge_id), skipped=True)
            claimed_gen = await db.claim_local_purge_attempt(
                purge_id, lease_seconds=lease_seconds
            )
            if claimed_gen is None:
                return LocalPurgeReport(purge_id=str(purge_id), skipped=True)
    return await _run_local_purge_body(
        purge_id,
        cancel_drain_timeout_s=cancel_drain_timeout_s,
        claimed_attempt_gen=claimed_gen,
        lease_seconds=lease_seconds,
        tracker_snapshot=tracker_snapshot,
    )


async def _purge_single_thread_layers(
    *,
    purge_id: str,
    claimed_attempt_gen: int | None,
    tid: str,
    min_turn: int,
    max_turn: int,
    user_id: str,
    cp_pool: Any,
    cp_serde: Any,
    report: LocalPurgeReport,
    thread_workspace_ids: dict[str, str | None],
    all_suffix_run_ids: list[str],
    blobs_to_release: list[tuple[str, str]],
    redis_keys_to_delete: list[str],
    card_digest_plans: list[_ConversationCardDigestPlan],
) -> bool:
    """Execute per-thread Layer 1, 2a, 2b, 3a, 4, 5a (plan registration), 5b under the
    thread purge lock.

    Re-verifies ``local_purge_attempt_gen`` and ``personal_purge_in_progress = TRUE``
    inside every destructive write phase's transaction so a stale worker resuming after
    a lease takeover and thread unblock NEVER mutates or deletes new-turn data.
    Returns ``False`` if superseded/unblocked (caller must abort immediately).
    """
    _ = user_id  # retained for signature clarity; card digest redraw runs unlocked in caller
    async with get_db_connection() as conn, conn.transaction():
        if not await _verify_purge_attempt_and_thread_blocked_tx(
            conn,
            purge_id=purge_id,
            claimed_attempt_gen=claimed_attempt_gen,
            thread_id=tid,
        ):
            return False
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT workspace_id::text AS workspace_id FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
                (tid,),
            )
            ws_row = await cur.fetchone()
            thread_workspace_ids[tid] = (
                str(ws_row["workspace_id"])
                if ws_row and ws_row.get("workspace_id")
                else None
            )

            await cur.execute(
                """
                SELECT conversation_response_id::text AS run_id,
                       turn_index,
                       metadata
                FROM conversation_responses
                WHERE conversation_thread_id = %s::uuid
                ORDER BY turn_index ASC, attempt_no ASC
                """,
                (tid,),
            )
            all_responses = [dict(r) for r in await cur.fetchall()]
            run_turn_map = {
                str(r["run_id"]): int(r["turn_index"])
                for r in all_responses
                if r.get("turn_index") is not None
            }
            suffix_responses = [
                r
                for r in all_responses
                if r.get("turn_index") is not None
                and min_turn <= int(r["turn_index"]) <= max_turn
            ]
            suffix_run_ids = [str(r["run_id"]) for r in suffix_responses]
            all_suffix_run_ids.extend(suffix_run_ids)

            suffix_task_ids: list[str] = []
            suffix_task_run_ids: list[str] = []
            if suffix_run_ids:
                await cur.execute(
                    """
                    SELECT task_id, task_run_id::text AS task_run_id
                    FROM subagent_runs
                    WHERE thread_id = %s::uuid
                      AND parent_run_id = ANY(%s::uuid[])
                    """,
                    (tid, suffix_run_ids),
                )
                sr_rows = [dict(r) for r in await cur.fetchall()]
                suffix_task_ids = sorted(
                    {str(r["task_id"]) for r in sr_rows if r.get("task_id")}
                )
                suffix_task_run_ids = sorted(
                    {str(r["task_run_id"]) for r in sr_rows if r.get("task_run_id")}
                )

    # Layer 1a: Checkpoints + Layer 5b BaseStore memories (on checkpointer DB)
    if cp_serde is not None:
        if isinstance(cp_pool, AsyncConnectionPool):
            async with get_db_connection() as vconn, vconn.transaction():
                if not await _verify_purge_attempt_and_thread_blocked_tx(
                    vconn,
                    purge_id=purge_id,
                    claimed_attempt_gen=claimed_attempt_gen,
                    thread_id=tid,
                ):
                    return False
            async with cp_pool.connection() as cp_conn, cp_conn.transaction():
                report.checkpoints_updated += await _purge_thread_checkpoints(
                    cp_conn,
                    cp_serde,
                    thread_id=tid,
                    min_turn=min_turn,
                    max_turn=max_turn,
                    run_turn_map=run_turn_map,
                    suffix_run_ids=set(suffix_run_ids),
                    suffix_task_ids=set(suffix_task_ids),
                )
                report.store_rows_deleted += await _purge_store_memories_for_thread(
                    cp_conn, tid
                )
        else:
            # Same DB fallback (e.g. when checkpointer shares app DB pool in tests)
            async with get_db_connection() as app_cp_conn, app_cp_conn.transaction():
                if not await _verify_purge_attempt_and_thread_blocked_tx(
                    app_cp_conn,
                    purge_id=purge_id,
                    claimed_attempt_gen=claimed_attempt_gen,
                    thread_id=tid,
                ):
                    return False
                async with app_cp_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("SELECT to_regclass('public.checkpoints') AS tbl")
                    has_cp = await cur.fetchone()
                if not (has_cp and has_cp.get("tbl")):
                    raise RuntimeError(
                        f"Checkpoints table public.checkpoints missing in same-DB fallback for thread_id={tid}"
                    )
                report.checkpoints_updated += await _purge_thread_checkpoints(
                    app_cp_conn,
                    cp_serde,
                    thread_id=tid,
                    min_turn=min_turn,
                    max_turn=max_turn,
                    run_turn_map=run_turn_map,
                    suffix_run_ids=set(suffix_run_ids),
                    suffix_task_ids=set(suffix_task_ids),
                )
                report.store_rows_deleted += await _purge_store_memories_for_thread(
                    app_cp_conn, tid
                )

    # App DB Layers 1b, 2a, 3a, 4, 5a (plan registration only — no sandbox I/O in tx)
    async with get_db_connection() as conn, conn.transaction():
        if not await _verify_purge_attempt_and_thread_blocked_tx(
            conn,
            purge_id=purge_id,
            claimed_attempt_gen=claimed_attempt_gen,
            thread_id=tid,
        ):
            return False
        async with conn.cursor(row_factory=dict_row) as cur:
            # Layer 1b: conversation_queries.content
            await cur.execute(
                """
                UPDATE conversation_queries
                SET content = %s
                WHERE conversation_thread_id = %s::uuid
                  AND turn_index BETWEEN %s AND %s
                """,
                (PURGED_PLACEHOLDER, tid, min_turn, max_turn),
            )
            report.queries_redacted += int(cur.rowcount or 0)

            # Layer 1c: conversation_responses.metadata.steering_inputs
            for r in suffix_responses:
                meta = r.get("metadata")
                if isinstance(meta, Mapping) and "steering_inputs" in meta:
                    new_steers, changed = _redact_steering_inputs_list(
                        meta.get("steering_inputs")
                    )
                    if changed:
                        new_meta = dict(meta)
                        new_meta["steering_inputs"] = new_steers
                        await cur.execute(
                            """
                            UPDATE conversation_responses
                            SET metadata = %s
                            WHERE conversation_response_id = %s::uuid
                            """,
                            (Jsonb(new_meta), str(r["run_id"])),
                        )
                        report.steerings_redacted += 1

            # Layer 2a: turn_slices, task_run_slices, provenance_records, replay_facts
            if suffix_run_ids:
                await cur.execute(
                    "DELETE FROM turn_slices WHERE conversation_response_id = ANY(%s::uuid[])",
                    (suffix_run_ids,),
                )
                await cur.execute(
                    """
                    UPDATE conversation_responses
                    SET replay_facts = NULL
                    WHERE conversation_response_id = ANY(%s::uuid[])
                    """,
                    (suffix_run_ids,),
                )
            if suffix_task_ids or suffix_task_run_ids:
                await cur.execute(
                    """
                    DELETE FROM task_run_slices
                    WHERE conversation_thread_id = %s::uuid
                      AND (
                          task_id = ANY(%s::text[])
                          OR task_run_id = ANY(%s::uuid[])
                      )
                    """,
                    (tid, suffix_task_ids, suffix_task_run_ids),
                )
            if suffix_task_run_ids:
                await cur.execute(
                    """
                    UPDATE subagent_runs
                    SET replay_facts = NULL
                    WHERE task_run_id = ANY(%s::uuid[])
                    """,
                    (suffix_task_run_ids,),
                )
            await cur.execute(
                """
                DELETE FROM provenance_records
                WHERE conversation_thread_id = %s::uuid
                  AND turn_index BETWEEN %s AND %s
                """,
                (tid, min_turn, max_turn),
            )

            # Layer 2b: hook_outbox delivered/returned/pending/failed steers
            if suffix_run_ids:
                await cur.execute(
                    """
                    UPDATE hook_outbox
                    SET payload = CASE
                            WHEN payload ? 'text'
                            THEN jsonb_set(
                                jsonb_set(payload, '{content}', to_jsonb(%s::text), true),
                                '{text}', to_jsonb(%s::text), true
                            )
                            ELSE jsonb_set(payload, '{content}', to_jsonb(%s::text), true)
                        END
                    WHERE conversation_thread_id = %s::uuid
                      AND hook_type = 'steer'
                      AND (
                          run_id = ANY(%s::uuid[])
                          OR payload->>'steered_run_id' = ANY(%s::text[])
                          OR payload->>'returned_run_id' = ANY(%s::text[])
                          OR payload->>'failed_run_id' = ANY(%s::text[])
                      )
                    """,
                    (
                        PURGED_PLACEHOLDER,
                        PURGED_PLACEHOLDER,
                        PURGED_PLACEHOLDER,
                        tid,
                        suffix_run_ids,
                        suffix_run_ids,
                        suffix_run_ids,
                        suffix_run_ids,
                    ),
                )
                report.outbox_rows_redacted += int(cur.rowcount or 0)

            # Terminate & redact pending or failed steers on this thread
            await cur.execute(
                """
                UPDATE hook_outbox
                SET status = 'done',
                    completed_at = COALESCE(completed_at, NOW()),
                    payload = jsonb_set(
                        CASE
                            WHEN payload ? 'text'
                            THEN jsonb_set(
                                jsonb_set(COALESCE(payload, '{}'::jsonb), '{content}', to_jsonb(%s::text), true),
                                '{text}', to_jsonb(%s::text), true
                            )
                            ELSE jsonb_set(COALESCE(payload, '{}'::jsonb), '{content}', to_jsonb(%s::text), true)
                        END,
                        '{outcome}',
                        '"purged"'::jsonb,
                        true
                    )
                WHERE conversation_thread_id = %s::uuid
                  AND hook_type = 'steer'
                  AND (
                      status IN ('pending', 'claimed', 'dead')
                      OR (status = 'done' AND payload->>'outcome' = 'failed')
                  )
                """,
                (
                    PURGED_PLACEHOLDER,
                    PURGED_PLACEHOLDER,
                    PURGED_PLACEHOLDER,
                    tid,
                ),
            )
            report.outbox_rows_redacted += int(cur.rowcount or 0)

            # Layer 3a: thread_transcripts & thread_transcript_files
            await cur.execute(
                """
                SELECT DISTINCT user_id, sha256
                FROM thread_transcript_files
                WHERE conversation_thread_id = %s::uuid
                  AND content IS NULL
                """,
                (tid,),
            )
            for brow in await cur.fetchall():
                blobs_to_release.append((str(brow["user_id"]), str(brow["sha256"])))

            await cur.execute(
                "DELETE FROM thread_transcript_files WHERE conversation_thread_id = %s::uuid",
                (tid,),
            )
            await cur.execute(
                "DELETE FROM thread_transcripts WHERE conversation_thread_id = %s::uuid",
                (tid,),
            )
            report.transcripts_deleted += int(cur.rowcount or 0)

            # Layer 4: thread_search_turns in closed interval [min_turn, max_turn]
            await cur.execute(
                """
                UPDATE thread_search_turns
                SET user_text = '',
                    answer_text = '',
                    answer_read = TRUE,
                    read_failures = 0,
                    retry_at = NULL,
                    read_at = clock_timestamp()
                WHERE conversation_thread_id = %s::uuid
                  AND turn_index BETWEEN %s AND %s
                """,
                (tid, min_turn, max_turn),
            )
            cleared_here = int(cur.rowcount or 0)
            # Also ensure every current settled response in [min_turn, max_turn] has an empty read placeholder
            await cur.execute(
                """
                INSERT INTO thread_search_turns (
                    conversation_thread_id, turn_index,
                    conversation_response_id, attempt_no,
                    user_text, answer_text, answer_read,
                    read_failures, retry_at, turn_at, read_at
                )
                SELECT r.conversation_thread_id, r.turn_index,
                       r.conversation_response_id, r.attempt_no,
                       '', '', TRUE,
                       0, NULL, r.created_at, clock_timestamp()
                FROM conversation_responses r
                WHERE r.conversation_thread_id = %s::uuid
                  AND r.turn_index BETWEEN %s AND %s
                  AND r.status <> 'in_progress'
                  AND NOT EXISTS (
                      SELECT 1 FROM conversation_responses n
                      WHERE n.conversation_thread_id = r.conversation_thread_id
                        AND n.turn_index = r.turn_index
                        AND n.status <> 'in_progress'
                        AND n.attempt_no > r.attempt_no
                  )
                ON CONFLICT (conversation_thread_id, turn_index) DO UPDATE
                SET conversation_response_id = EXCLUDED.conversation_response_id,
                    attempt_no = EXCLUDED.attempt_no,
                    user_text = '',
                    answer_text = '',
                    answer_read = TRUE,
                    read_failures = 0,
                    retry_at = NULL,
                    read_at = clock_timestamp()
                """,
                (tid, min_turn, max_turn),
            )
            report.search_rows_cleared += max(cleared_here, int(cur.rowcount or 0))

        # Layer 5a: dalton_conversation_cards fallback registration (sandbox digest redraw
        # runs strictly AFTER releasing thread advisory lock and DB connection)
        card_plan = await _collect_conversation_card_purge_plan(conn, tid)
        if card_plan is not None:
            card_digest_plans.append(card_plan)

    # Collect Layer 2c Redis keys for this thread
    redis_keys_to_delete.extend(
        collect_redis_keys_for_suffix(
            tid, suffix_run_ids, suffix_task_ids, suffix_task_run_ids
        )
    )
    return True


async def _run_local_purge_body(
    purge_id: str,
    *,
    cancel_drain_timeout_s: float = 120.0,
    claimed_attempt_gen: int | None = None,
    lease_seconds: int = 300,
    tracker_snapshot: TrackerPurgeSnapshot | None = None,
) -> LocalPurgeReport:
    report = LocalPurgeReport(purge_id=str(purge_id))
    completed_or_superseded = False
    try:
        ledger = await db.get_purge_ledger(purge_id, decrypt_revoke_token=False)
        if ledger is None:
            raise LookupError(f"Purge ledger {purge_id} not found")
        if ledger.local_purge_status != LocalPurgeStatus.RUNNING:
            completed_or_superseded = True
            report.skipped = True
            return report

        thread_ranges = await _resolve_thread_ranges(ledger)
        target_thread_ids = sorted(thread_ranges.keys())
        report.threads_purged = list(target_thread_ids)

        # Step 0: Cancel and drain any in-flight runs on affected threads.
        # Runs strictly outside try_purge_ledger_lock so publish_cancel_nudge never
        # holds a ledger advisory lock or dedicated PG session while waiting on Redis.
        for tid in target_thread_ids:
            lo, hi = thread_ranges[tid]
            await _cancel_and_drain_thread(
                tid, min_turn=lo, max_turn=hi, timeout_s=cancel_drain_timeout_s
            )

        # Collect per-thread suffix run_ids, task_ids, workspace_ids, deferred card digest plans
        all_suffix_run_ids: list[str] = []
        thread_workspace_ids: dict[str, str | None] = {}
        blobs_to_release: list[tuple[str, str]] = []
        redis_keys_to_delete: list[str] = []
        card_digest_plans: list[_ConversationCardDigestPlan] = []

        cp_pool = None
        cp_serde = None
        if target_thread_ids:
            checkpointer = get_checkpointer()
            cp_pool = getattr(checkpointer, "conn", None) if checkpointer is not None else None
            cp_serde = getattr(checkpointer, "serde", None) if checkpointer is not None else None
            if checkpointer is None or cp_serde is None:
                raise RuntimeError(
                    f"Checkpointer or serializer unavailable while purging purge_id={purge_id}"
                )

        for tid in target_thread_ids:
            min_turn, max_turn = thread_ranges[tid]
            async with acquire_purge_thread_lock(tid, timeout_s=cancel_drain_timeout_s):
                if claimed_attempt_gen is not None:
                    if not await db.renew_local_purge_attempt(
                        purge_id,
                        expected_attempt_gen=claimed_attempt_gen,
                        lease_seconds=lease_seconds,
                    ):
                        completed_or_superseded = True
                        report.skipped = True
                        return report
                still_active = await _purge_single_thread_layers(
                    purge_id=purge_id,
                    claimed_attempt_gen=claimed_attempt_gen,
                    tid=tid,
                    min_turn=min_turn,
                    max_turn=max_turn,
                    user_id=ledger.user_id,
                    cp_pool=cp_pool,
                    cp_serde=cp_serde,
                    report=report,
                    thread_workspace_ids=thread_workspace_ids,
                    all_suffix_run_ids=all_suffix_run_ids,
                    blobs_to_release=blobs_to_release,
                    redis_keys_to_delete=redis_keys_to_delete,
                    card_digest_plans=card_digest_plans,
                )
                if still_active is False:
                    completed_or_superseded = True
                    report.skipped = True
                    return report

        # Layer 5a completion: redraw / delete archived conversation Markdown digests
        # strictly OUTSIDE all advisory locks and DB connections, then mark deleted in
        # a short transaction after re-verifying attempt generation and thread block.
        for plan in card_digest_plans:
            if claimed_attempt_gen is not None:
                if not await db.renew_local_purge_attempt(
                    purge_id,
                    expected_attempt_gen=claimed_attempt_gen,
                    lease_seconds=lease_seconds,
                ):
                    completed_or_superseded = True
                    report.skipped = True
                    return report
            await _execute_conversation_card_digest_redraw(
                plan, user_id=ledger.user_id
            )
            async with get_db_connection() as card_conn, card_conn.transaction():
                if not await _verify_purge_attempt_and_thread_blocked_tx(
                    card_conn,
                    purge_id=purge_id,
                    claimed_attempt_gen=claimed_attempt_gen,
                    thread_id=plan.thread_id,
                ):
                    completed_or_superseded = True
                    report.skipped = True
                    return report
                async with card_conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM dalton_conversation_cards WHERE card_id = ANY(%s::text[])",
                        (plan.card_ids,),
                    )
                    report.cards_deleted += int(cur.rowcount or 0)

        # Layer 2c: Delete Redis event stream & meta keys strictly OUTSIDE all PG
        # advisory locks and dedicated session connections (renewing durable lease first).
        if redis_keys_to_delete:
            if claimed_attempt_gen is not None:
                if not await db.renew_local_purge_attempt(
                    purge_id,
                    expected_attempt_gen=claimed_attempt_gen,
                    lease_seconds=lease_seconds,
                ):
                    completed_or_superseded = True
                    report.skipped = True
                    return report
            assert_no_advisory_lock_held("purge.delete_redis_keys")
            report.redis_keys_deleted = await _delete_redis_keys(redis_keys_to_delete)

        # Layer 3b: Release now-unreferenced transcript blobs (reusing blob_conn so
        # no second pooled connection is ever borrowed under tiny pool limits).
        if blobs_to_release:
            from src.server.database.workspace_file_blobs import release_blob

            if claimed_attempt_gen is not None:
                if not await db.renew_local_purge_attempt(
                    purge_id,
                    expected_attempt_gen=claimed_attempt_gen,
                    lease_seconds=lease_seconds,
                ):
                    completed_or_superseded = True
                    report.skipped = True
                    return report
            async with get_db_connection() as blob_conn, blob_conn.transaction():
                if not await _verify_purge_attempt_and_thread_blocked_tx(
                    blob_conn,
                    purge_id=purge_id,
                    claimed_attempt_gen=claimed_attempt_gen,
                    thread_id=None,
                ):
                    completed_or_superseded = True
                    report.skipped = True
                    return report
                for b_user_id, b_sha in set(blobs_to_release):
                    try:
                        async with blob_conn.cursor(row_factory=dict_row) as cur:
                            await cur.execute(
                                """
                                SELECT last_referenced_at
                                FROM workspace_file_blobs
                                WHERE user_id = %s AND sha256 = %s AND condemned_at IS NULL
                                """,
                                (b_user_id, b_sha),
                            )
                            brow = await cur.fetchone()
                        if brow and brow.get("last_referenced_at") is not None:
                            await release_blob(
                                b_user_id,
                                b_sha,
                                brow["last_referenced_at"],
                                conn=blob_conn,
                            )
                    except Exception:
                        logger.warning(
                            "[purge] release_blob failed for user=%s sha=%s",
                            b_user_id,
                            b_sha,
                            exc_info=True,
                        )

        # Layer 6a: Delete approvals for disconnected service + ALL services in suffix runs
        async with get_db_connection() as appr_conn, appr_conn.transaction():
            if not await _verify_purge_attempt_and_thread_blocked_tx(
                appr_conn,
                purge_id=purge_id,
                claimed_attempt_gen=claimed_attempt_gen,
                thread_id=None,
            ):
                completed_or_superseded = True
                report.skipped = True
                return report
            report.approvals_deleted = await db.delete_approvals_for_disconnect(
                user_id=ledger.user_id,
                provider=ledger.provider,
                service=ledger.service,
                thread_run_ids=list(dict.fromkeys(all_suffix_run_ids)),
                conn=appr_conn,
            )

        # Layer 6b: Tracker calendar extension point (Step 4) or reconcile flag
        tracker_reconcile_needed = False
        handler = get_tracker_purge_handler()
        if handler is not None:
            try:
                if tracker_snapshot is not None:
                    sig = inspect.signature(handler)
                    accepts_snapshot = "snapshot" in sig.parameters or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD
                        for p in sig.parameters.values()
                    )
                    handled = (
                        await handler(ledger, snapshot=tracker_snapshot)
                        if accepts_snapshot
                        else await handler(ledger)
                    )
                else:
                    handled = await handler(ledger)
                tracker_reconcile_needed = not bool(handled)
            except Exception:
                logger.warning(
                    "[purge] tracker purge handler raised for purge_id=%s; marking tracker_reconcile_needed",
                    purge_id,
                    exc_info=True,
                )
                tracker_reconcile_needed = True
        else:
            configured_owner = (os.getenv("DALTON_TRACKER_CALENDAR_OWNER_ID") or "").strip()
            if (
                configured_owner
                and ledger.user_id == configured_owner
                and ledger.provider == "google"
                and ledger.service in ("calendar", None)
            ):
                tracker_reconcile_needed = True
        report.tracker_reconcile_needed = tracker_reconcile_needed

        # Final atomic commit: re-verify durable claim under short pg_advisory_xact_lock
        # on ONE pooled connection, then unblock threads + SECOND generation increment +
        # mark touches & ledger completed.
        superseded_before_commit = False
        async with get_db_connection() as conn, conn.transaction():
            p_lock_key = purge_lock_key(purge_id)
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT pg_advisory_xact_lock(%s)", (p_lock_key,))
                with track_advisory_lock("purge_ledger", p_lock_key):
                    if claimed_attempt_gen is not None:
                        await cur.execute(
                            """
                            SELECT local_purge_status, local_purge_attempt_gen
                            FROM dalton_integration_purge_ledger
                            WHERE purge_id = %s::uuid
                            FOR UPDATE
                            """,
                            (str(purge_id),),
                        )
                        lrow = await cur.fetchone()
                        if (
                            lrow is None
                            or str(lrow["local_purge_status"]) != LocalPurgeStatus.RUNNING.value
                            or int(lrow["local_purge_attempt_gen"]) != int(claimed_attempt_gen)
                        ):
                            superseded_before_commit = True

                    if not superseded_before_commit:
                        if target_thread_ids:
                            await cur.execute(
                                """
                                SELECT conversation_thread_id
                                FROM conversation_threads
                                WHERE conversation_thread_id = ANY(%s::uuid[])
                                ORDER BY conversation_thread_id ASC
                                FOR UPDATE
                                """,
                                (target_thread_ids,),
                            )
                            await cur.execute(
                                """
                                UPDATE conversation_threads t
                                SET personal_purge_in_progress = EXISTS (
                                        SELECT 1
                                        FROM dalton_integration_purge_ledger l
                                        WHERE l.local_purge_status = 'running'
                                          AND l.purge_id <> %s::uuid
                                          AND l.purged_max_turn_by_thread ? t.conversation_thread_id::text
                                    ),
                                    personal_purge_generation = personal_purge_generation + 1,
                                    updated_at = NOW()
                                WHERE t.conversation_thread_id = ANY(%s::uuid[])
                                """,
                                (str(purge_id), target_thread_ids),
                            )

                            service_filter = (
                                "AND service = %(service)s" if ledger.service is not None else ""
                            )
                            await cur.execute(
                                f"""
                                UPDATE dalton_thread_integration_touches
                                SET purged_at = NOW()
                                WHERE thread_id = ANY(%(thread_ids)s::uuid[])
                                  AND user_id = %(user_id)s
                                  AND provider = %(provider)s
                                  {service_filter}
                                  AND auth_epoch <= %(auth_epoch_upto)s
                                  AND purged_at IS NULL
                                """,
                                {
                                    "thread_ids": target_thread_ids,
                                    "user_id": ledger.user_id,
                                    "provider": ledger.provider,
                                    "service": ledger.service,
                                    "auth_epoch_upto": ledger.auth_epoch_upto,
                                },
                            )

                        attempt_clause = (
                            "AND local_purge_attempt_gen = %(expected_attempt_gen)s"
                            if claimed_attempt_gen is not None
                            else ""
                        )
                        await cur.execute(
                            f"""
                            UPDATE dalton_integration_purge_ledger
                            SET local_purge_status = 'completed',
                                local_completed_at = NOW(),
                                local_purge_inflight_until = NULL,
                                tracker_reconcile_needed = tracker_reconcile_needed OR %(tracker_reconcile)s,
                                completed_at = CASE
                                    WHEN remote_revoke_status IN ('succeeded', 'superseded', 'not_applicable')
                                    THEN COALESCE(completed_at, NOW())
                                    ELSE completed_at
                                END
                            WHERE purge_id = %(purge_id)s::uuid
                              AND local_purge_status = 'running'
                              {attempt_clause}
                            """,
                            {
                                "tracker_reconcile": tracker_reconcile_needed,
                                "purge_id": str(purge_id),
                                "expected_attempt_gen": claimed_attempt_gen,
                            },
                        )

        completed_or_superseded = True
        if superseded_before_commit:
            report.skipped = True
            return report

        # Layer 3c: Schedule re-export of redacted transcripts AFTER commit unblocks threads
        try:
            from src.server.services.transcripts import schedule_thread_export

            for tid in target_thread_ids:
                schedule_thread_export(tid, thread_workspace_ids.get(tid))
        except Exception:
            logger.warning(
                "[purge] schedule_thread_export failed after commit for purge_id=%s",
                purge_id,
                exc_info=True,
            )

        return report
    finally:
        if not completed_or_superseded and claimed_attempt_gen is not None:
            with anyio.CancelScope(shield=True):
                try:
                    await db.release_local_purge_attempt(
                        purge_id, expected_attempt_gen=claimed_attempt_gen
                    )
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Background resume loop (called from Dalton startup hook in setup.py)
# ---------------------------------------------------------------------------


async def run_personal_purge_resume_once(
    *,
    revoker: RemoteRevoker | None = None,
) -> list[str]:
    """Scan ``dalton_integration_purge_ledger WHERE local_purge_status = 'running'``
    and execute local 2A purge ONLY via mutex-protected ``finish_disconnect_locked(..., run_remote_revoke=False)``
    so slow or failing upstream remote revocations never delay local purge or shared-thread unblocking.

    When a caller explicitly injects ``revoker``, due remote revocations are run strictly AFTER
    all local 2A purges in the scan have finished and only for ``(user_id, provider)`` batches
    that have zero remaining ``running`` local purge ledgers.
    """
    from src.dalton.services.integration_revoke import process_revoke
    from src.dalton.services.user_integrations_lifecycle import finish_disconnect_locked

    resumed: list[str] = []
    pending_remote_rows: list[tuple[str, str, str]] = []
    try:
        pending = await db.list_running_local_purges()
    except Exception as exc:
        logger.warning(
            "[purge-resume] skipped personal purge resume scan: %s", exc
        )
        return resumed

    for row in pending:
        try:
            result = await finish_disconnect_locked(
                row.purge_id,
                revoker=None,
            )
            if result is not None:
                resumed.append(row.purge_id)
                if (
                    revoker is not None
                    and result.remote_revoke_status == RemoteRevokeStatus.PENDING
                ):
                    pending_remote_rows.append(
                        (row.purge_id, row.user_id, row.provider)
                    )
        except Exception:
            logger.warning(
                "[purge-resume] failed to resume purge_id=%s",
                row.purge_id,
                exc_info=True,
            )

    if revoker is not None and pending_remote_rows:
        try:
            still_running = await db.list_running_local_purges()
            running_batches = {(r.user_id, r.provider) for r in still_running}
        except Exception:
            running_batches = {(u, p) for _, u, p in pending_remote_rows}
        for pid, uid, prov in pending_remote_rows:
            if (uid, prov) in running_batches:
                continue
            try:
                await process_revoke(pid, revoker)
            except Exception:
                logger.warning(
                    "[purge-resume] post-local remote revoke deferred for purge_id=%s",
                    pid,
                    exc_info=True,
                )

    if resumed:
        logger.info(
            "[purge-resume] resumed %d incomplete personal integration purge(s)",
            len(resumed),
        )
    return resumed


async def run_personal_purge_resume_loop(
    *,
    revoker: RemoteRevoker | None = None,
    poll_interval_s: float = 60.0,
) -> None:
    """Background loop that immediately scans once at startup and retries every ``poll_interval_s``."""
    logger.info(
        "[purge-resume] background loop started (poll_interval_s=%.1f)",
        poll_interval_s,
    )
    while True:
        try:
            await run_personal_purge_resume_once(revoker=revoker)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[purge-resume] resume loop iteration failed", exc_info=True
            )
        await asyncio.sleep(poll_interval_s)


async def run_startup_personal_purge_resume(
    *,
    revoker: RemoteRevoker | None = None,
) -> list[str]:
    """Run one resume scan immediately (compat wrapper around ``run_personal_purge_resume_once``)."""
    return await run_personal_purge_resume_once(revoker=revoker)
