"""End-to-end tests for compacted Feishu DM near-empty / empty / continuation / refusal
sequences across middleware, live SSE producer, checkpoint replay, and deliverable-text extraction (§6.5)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.config import get_config

from ptc_agent.agent.middleware.tool import empty_call_retry
from ptc_agent.agent.middleware.tool.empty_call_retry import (
    _EMPTY_REPLY_NOTICE,
    EmptyToolCallRetryMiddleware,
)
from src.dalton.channels.deliverable_text import (
    extract_deliverable_text_from_events,
    extract_deliverable_text_from_messages,
)
from src.dalton.llm import near_empty_reply as ner_mod
from src.dalton.llm.near_empty_reply import NEAR_EMPTY_REPLY_NOTICE
from src.dalton.middleware.refusal_fallback import REFUSAL_NOTICE_CHANNEL
from src.server.services.history.projector import (
    history_events_to_sse,
    messages_to_history_events,
)
from src.server.services.runs.sse_producer import RunSSEProducer

_ORIG_EMIT_MAIN_STREAM_EVENT = ner_mod.emit_main_stream_event


def _feishu_dm_config() -> dict:
    return {
        "configurable": {
            "thread_id": "feishu-compacted-dm",
            "checkpoint_ns": "",
            "platform": "feishu",
        },
        "metadata": {"origin": "user", "platform": "feishu"},
    }


def _compacted_request() -> SimpleNamespace:
    # Simulates post-CompactionMiddleware state: request.messages carries only the
    # summary, while request.state["messages"] holds the real user long prompt.
    summary_msg = HumanMessage(
        content="[Summary of earlier conversation]",
        additional_kwargs={"lc_source": "summarization"},
    )
    full_user_msg = HumanMessage(
        content="请基于刚才拉取的 2026 年三季报数据，详细分析公司毛利率变化、现金流与未来两季度业绩指引。"
    )
    req = SimpleNamespace(
        messages=[summary_msg],
        state={"messages": [full_user_msg]},
    )
    return req


async def _run_sequence(monkeypatch, replies: list[AIMessage]) -> dict:
    monkeypatch.setattr(
        empty_call_retry,
        "get_config",
        create_autospec(get_config, return_value=_feishu_dm_config()),
    )

    live_graph_events: list[tuple] = []

    def _capture_custom(event: dict, *, is_subagent: bool) -> None:
        if not is_subagent:
            live_graph_events.append(((), "custom", event))

    monkeypatch.setattr(
        ner_mod,
        "emit_main_stream_event",
        create_autospec(_ORIG_EMIT_MAIN_STREAM_EVENT, side_effect=_capture_custom),
    )

    call_idx = 0

    async def _handler_sig(request):
        return ModelResponse(result=[])

    async def _streaming_handler(_request):
        nonlocal call_idx
        msg = replies[call_idx]
        call_idx += 1
        # Simulate token streaming during handler(request) BEFORE middleware inspects result.
        live_graph_events.append(
            (
                (),
                "messages",
                (
                    AIMessageChunk(
                        id=msg.id,
                        content=msg.content,
                        response_metadata=dict(msg.response_metadata or {}),
                    ),
                    {"langgraph_node": "model"},
                ),
            )
        )
        return ModelResponse(result=[msg])

    handler = create_autospec(_handler_sig, side_effect=_streaming_handler)
    mw = EmptyToolCallRetryMiddleware(max_retries=2)
    req = _compacted_request()
    response = await mw.awrap_model_call(req, handler)

    producer = RunSSEProducer(thread_id="feishu-compacted-dm", run_id="run-e2e")

    class _FakeGraph:
        async def astream(self, *_args, **_kwargs):
            for item in live_graph_events:
                yield item

    live_sse: list[dict] = []
    async for raw in producer.stream_workflow(_FakeGraph(), {}, {}):
        ev_t = None
        ev_d = None
        for line in raw.strip().splitlines():
            if line.startswith("event: "):
                ev_t = line[7:].strip()
            elif line.startswith("data: "):
                ev_d = json.loads(line[6:])
        if ev_t and isinstance(ev_d, dict):
            live_sse.append({"event": ev_t, "data": ev_d})

    checkpoint_msgs = [req.state["messages"][0], *response.result]
    replay_sse = history_events_to_sse(
        messages_to_history_events(checkpoint_msgs), thread_id="feishu-compacted-dm"
    )

    return {
        "call_count": handler.await_count,
        "live_sse": live_sse,
        "replay_sse": replay_sse,
        "live_text": extract_deliverable_text_from_events(live_sse).text,
        "replay_text": extract_deliverable_text_from_events(replay_sse).text,
        "checkpoint_text": extract_deliverable_text_from_messages(checkpoint_msgs).text,
    }


@pytest.mark.asyncio
async def test_compacted_feishu_dm_symbol_then_full_answer(monkeypatch) -> None:
    out = await _run_sequence(
        monkeypatch,
        [
            AIMessage(id="msg_0", content="═", response_metadata={"stop_reason": "end_turn"}),
            AIMessage(
                id="msg_1",
                content="三季报毛利率提升 2.1pct，经营现金流同比增 35%。",
                response_metadata={"stop_reason": "end_turn"},
            ),
        ],
    )
    assert out["call_count"] == 2
    assert any(
        e["event"] == "message_discarded" and e["data"].get("id") == "msg_0"
        for e in out["live_sse"]
    )
    assert (
        out["live_text"]
        == out["replay_text"]
        == out["checkpoint_text"]
        == "三季报毛利率提升 2.1pct，经营现金流同比增 35%。"
    )


@pytest.mark.asyncio
async def test_compacted_feishu_dm_symbol_then_empty_and_empty_then_symbol(
    monkeypatch,
) -> None:
    out1 = await _run_sequence(
        monkeypatch,
        [
            AIMessage(id="msg_0", content="═", response_metadata={"stop_reason": "end_turn"}),
            AIMessage(id="msg_1", content="", response_metadata={"stop_reason": "end_turn"}),
        ],
    )
    assert out1["call_count"] == 2
    assert out1["live_text"] == out1["replay_text"] == out1["checkpoint_text"] == _EMPTY_REPLY_NOTICE

    out2 = await _run_sequence(
        monkeypatch,
        [
            AIMessage(id="msg_0", content="", response_metadata={"stop_reason": "end_turn"}),
            AIMessage(id="msg_1", content="═", response_metadata={"stop_reason": "end_turn"}),
        ],
    )
    assert out2["call_count"] == 2
    assert (
        out2["live_text"]
        == out2["replay_text"]
        == out2["checkpoint_text"]
        == NEAR_EMPTY_REPLY_NOTICE
    )


@pytest.mark.asyncio
async def test_compacted_feishu_dm_symbol_then_output_cap_then_continuation(
    monkeypatch,
) -> None:
    out = await _run_sequence(
        monkeypatch,
        [
            AIMessage(id="msg_0", content="═", response_metadata={"stop_reason": "end_turn"}),
            AIMessage(id="msg_1", content="前半段", response_metadata={"finish_reason": "MAX_TOKENS"}),
            AIMessage(id="msg_2", content="后半段", response_metadata={"finish_reason": "STOP"}),
        ],
    )
    assert out["call_count"] == 3
    assert any(
        e["event"] == "message_discarded" and e["data"].get("id") == "msg_0"
        for e in out["live_sse"]
    )
    assert any(
        e["event"] == "message_chunk"
        and e["data"].get("id") == "msg_2"
        and e["data"].get("continued_from_message_id") == "msg_1"
        for e in out["live_sse"]
    )
    assert out["live_text"] == out["replay_text"] == out["checkpoint_text"] == "前半段后半段"


@pytest.mark.asyncio
async def test_compacted_feishu_dm_all_refusal_keeps_hf2_notice(monkeypatch) -> None:
    out = await _run_sequence(
        monkeypatch,
        [
            AIMessage(id="r_0", content="", response_metadata={"stop_reason": "refusal"}),
            AIMessage(id="r_1", content="", response_metadata={"stop_reason": "refusal"}),
            AIMessage(id="r_2", content="", response_metadata={"stop_reason": "refusal"}),
        ],
    )
    assert out["call_count"] == 3
    assert (
        out["live_text"]
        == out["replay_text"]
        == out["checkpoint_text"]
        == REFUSAL_NOTICE_CHANNEL
    )
