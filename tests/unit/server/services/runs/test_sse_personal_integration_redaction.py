"""Unit tests for SSE event log redaction on threads carrying personal integration data."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessageChunk, ToolMessage
import pytest

from src.dalton.services import personal_integration_guard as guard_mod
from src.server.services.runs import sse_producer as sse_mod
from src.server.services.runs.sse_producer import RunSSEProducer


class _FakeGraph:
    def __init__(self, events: list[tuple[tuple[str, ...], str, Any]]) -> None:
        self._events = events

    async def astream(self, *_args: Any, **_kwargs: Any):
        for ev in self._events:
            yield ev


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture(autouse=True)
def _clean_runtime_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DALTON_ENABLED", "1")
    monkeypatch.setattr(sse_mod, "SSE_EVENT_LOG_ENABLED", True)
    guard_mod.clear_thread_personal_private_runtime()
    yield
    guard_mod.clear_thread_personal_private_runtime()


def _attach_sse_log_capture() -> _CaptureHandler:
    handler = _CaptureHandler()
    sse_mod.sse_logger.addHandler(handler)
    sse_mod.sse_logger.setLevel(logging.INFO)
    return handler


@pytest.mark.asyncio
async def test_sse_log_omits_tool_result_and_assistant_body_from_first_personal_touch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First personal-calendar touch stops SSE log body recording while UI still gets full events."""
    tid = str(uuid4())
    rid = str(uuid4())
    secret_event_title = "绝密董事会并购评审-2026Q4"
    secret_assistant_reply = "你今天下午三点有「绝密董事会并购评审-2026Q4」。"

    async def _initially_not_private(_thread_id: str, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr(guard_mod, "is_thread_personal_private", _initially_not_private)

    # Simulate a Turn-1 stream: tool_calls -> personal_integration_notice (first touch)
    # -> dalton_calendar ToolMessage -> assistant AIMessageChunk.
    tool_result_payload = json.dumps(
        {"ok": True, "events": [{"summary": secret_event_title}]},
        ensure_ascii=False,
    )
    graph = _FakeGraph(
        [
            (
                (),
                "custom",
                {
                    "type": "personal_integration_notice",
                    "provider": "google",
                    "service": "calendar",
                    "turn_index": 0,
                    "message": guard_mod.FIRST_TOUCH_NOTICE_CALENDAR,
                },
            ),
            (
                (),
                "messages",
                (
                    ToolMessage(
                        content=tool_result_payload,
                        name="dalton_calendar",
                        tool_call_id="tc-cal-1",
                    ),
                    {"langgraph_node": "tools"},
                ),
            ),
            (
                (),
                "messages",
                (
                    AIMessageChunk(
                        content=secret_assistant_reply,
                        id="ai-msg-1",
                    ),
                    {"langgraph_node": "model"},
                ),
            ),
        ]
    )

    capture = _attach_sse_log_capture()
    try:
        producer = RunSSEProducer(thread_id=tid, run_id=rid)
        emitted = [frame async for frame in producer.stream_workflow(graph, {}, {})]
    finally:
        sse_mod.sse_logger.removeHandler(capture)

    # 1. Owner UI receives full tool result and full assistant reply untouched.
    joined_ui = "\n".join(emitted)
    assert "event: tool_call_result" in joined_ui
    assert secret_event_title in joined_ui
    assert "event: message_chunk" in joined_ui
    assert secret_assistant_reply in joined_ui

    # 2. SSE log records sequence id & event type, NEVER tool result or assistant body.
    joined_logs = "\n".join(capture.messages)
    assert "event: tool_call_result" in joined_logs
    assert "event: message_chunk" in joined_logs
    assert secret_event_title not in joined_logs
    assert secret_assistant_reply not in joined_logs
    for log_entry in capture.messages:
        if "event: tool_call_result" in log_entry or "event: message_chunk" in log_entry:
            assert log_entry.startswith("id: ")
            assert "data:" not in log_entry


@pytest.mark.asyncio
async def test_sse_log_omits_body_on_subsequent_turn_of_personal_private_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subsequent turn on an already-private thread omits all SSE log bodies from the start."""
    tid = str(uuid4())
    rid = str(uuid4())
    secret_reply = "根据刚才查到的私人日程，下午四点需要见医生。"

    async def _already_private(thread_id: str, **_kw: Any) -> bool:
        return thread_id == tid

    monkeypatch.setattr(guard_mod, "is_thread_personal_private", _already_private)

    graph = _FakeGraph(
        [
            (
                (),
                "messages",
                (
                    AIMessageChunk(content=secret_reply, id="ai-msg-2"),
                    {"langgraph_node": "model"},
                ),
            ),
        ]
    )

    capture = _attach_sse_log_capture()
    try:
        producer = RunSSEProducer(thread_id=tid, run_id=rid)
        emitted = [frame async for frame in producer.stream_workflow(graph, {}, {})]
    finally:
        sse_mod.sse_logger.removeHandler(capture)

    assert secret_reply in "\n".join(emitted)
    assert capture.messages
    assert secret_reply not in "\n".join(capture.messages)
    for log_entry in capture.messages:
        assert log_entry.startswith("id: ")
        assert "event: " in log_entry
        assert "data:" not in log_entry


@pytest.mark.asyncio
async def test_sse_log_keeps_full_payload_for_non_private_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regular non-private threads retain normal SSE event logging."""
    tid = str(uuid4())
    rid = str(uuid4())
    normal_reply = "公开财报显示营收同比增长 12%。"

    async def _not_private(_thread_id: str, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr(guard_mod, "is_thread_personal_private", _not_private)

    graph = _FakeGraph(
        [
            (
                (),
                "messages",
                (
                    AIMessageChunk(content=normal_reply, id="ai-msg-pub"),
                    {"langgraph_node": "model"},
                ),
            ),
        ]
    )

    capture = _attach_sse_log_capture()
    try:
        producer = RunSSEProducer(thread_id=tid, run_id=rid)
        emitted = [frame async for frame in producer.stream_workflow(graph, {}, {})]
    finally:
        sse_mod.sse_logger.removeHandler(capture)

    assert normal_reply in "\n".join(emitted)
    assert normal_reply in "\n".join(capture.messages)


def _one_reply_graph(text: str) -> _FakeGraph:
    return _FakeGraph(
        [((), "messages", (AIMessageChunk(content=text, id="ai-msg-x"), {"langgraph_node": "model"}))]
    )


@pytest.mark.asyncio
async def test_no_private_lookup_when_sse_log_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the SSE event log off the turn never queries the thread's private state."""
    monkeypatch.setattr(sse_mod, "SSE_EVENT_LOG_ENABLED", False)
    calls: list[str] = []

    async def _lookup(thread_id: str, **_kw: Any) -> bool:
        calls.append(thread_id)
        return False

    monkeypatch.setattr(guard_mod, "is_thread_personal_private", _lookup)
    producer = RunSSEProducer(thread_id=str(uuid4()), run_id=str(uuid4()))
    emitted = [frame async for frame in producer.stream_workflow(_one_reply_graph("普通回答"), {}, {})]

    assert "普通回答" in "\n".join(emitted)
    assert calls == []


@pytest.mark.asyncio
async def test_failed_private_lookup_keeps_turn_and_redacts_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """A database error in the lookup keeps the turn running and logs ids and types only."""

    async def _pool_timeout(_thread_id: str, **_kw: Any) -> bool:
        raise TimeoutError("pool timeout")

    monkeypatch.setattr(guard_mod, "is_thread_personal_private", _pool_timeout)
    capture = _attach_sse_log_capture()
    try:
        producer = RunSSEProducer(thread_id=str(uuid4()), run_id=str(uuid4()))
        emitted = [frame async for frame in producer.stream_workflow(_one_reply_graph("下午的安排"), {}, {})]
    finally:
        sse_mod.sse_logger.removeHandler(capture)

    assert "下午的安排" in "\n".join(emitted)
    assert capture.messages
    assert all("data:" not in m for m in capture.messages)
