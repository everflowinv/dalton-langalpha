"""Unit tests for unified deliverable-text extraction, projector, live SSE continuation, and outlet notices (§5)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from ptc_agent.agent.middleware.plan_close_out import CLOSE_OUT_KEY
from src.dalton.channels.deliverable_text import (
    AUTOMATION_EMPTY_AFTER_TOOLS_SUMMARY,
    AUTOMATION_FILES_ONLY_EXCERPT,
    CHANNEL_EMPTY_AFTER_TOOLS_NOTICE,
    CHANNEL_SEND_FAILED_AFTER_TOOLS_NOTICE,
    CONTINUED_FROM_KWARG,
    RETRIED_ATTEMPT_KWARG,
    build_channel_empty_delivery_notice,
    extract_deliverable_text_from_events,
    extract_deliverable_text_from_messages,
    resolve_automation_empty_excerpt,
    resolve_live_continuation_id,
)
from src.server.services.automation_excerpt import RunAnswer, read_run_answer
from src.server.services.history.committed import _main_text
from src.server.services.history.projector import (
    history_events_to_sse,
    messages_to_history_events,
)
from src.server.services.runs.sse_producer import RunSSEProducer

_VECTORS_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "dalton"
    / "deliverable_text_vectors.json"
)
_VECTORS = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))["vectors"]


@pytest.mark.parametrize("vec", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_shared_deliverable_text_vectors(vec: dict) -> None:
    res = extract_deliverable_text_from_events(vec["events"])
    assert res.text == vec["expected_text"]
    assert res.saw_business_tool_call is vec["expected_saw_business_tool_call"]


def test_projector_preserves_phase_close_out_continuation_and_skips_retried() -> None:
    messages = [
        HumanMessage(content="请分析 Q3 财报", id="h-1"),
        AIMessage(
            id="m-retried",
            content="═",
            additional_kwargs={RETRIED_ATTEMPT_KWARG: True},
        ),
        AIMessage(
            id="m-0",
            content=[
                {"type": "text", "text": "先看过程解说。", "phase": "commentary"},
                {"type": "text", "text": "前半段", "phase": "final_answer"},
            ],
        ),
        AIMessage(
            id="m-1",
            content="后半段",
            additional_kwargs={CONTINUED_FROM_KWARG: "m-0"},
        ),
        AIMessage(
            id="m-closeout",
            content="",
            tool_calls=[
                {
                    "id": "tc-plan",
                    "name": "UpdatePlan",
                    "args": {"steps": []},
                    "type": "tool_call",
                }
            ],
            additional_kwargs={CLOSE_OUT_KEY: True},
        ),
        ToolMessage(content="plan updated", tool_call_id="tc-plan", name="UpdatePlan"),
    ]

    history_events = messages_to_history_events(messages)
    sse_items = history_events_to_sse(history_events, thread_id="t-1")

    # Retried message is skipped on replay.
    assert not any(
        it["data"].get("id") == "m-retried" for it in sse_items
    )

    # Mixed phase was split into separate commentary + final_answer chunks.
    m0_chunks = [
        it["data"]
        for it in sse_items
        if it["event"] == "message_chunk" and it["data"].get("id") == "m-0"
    ]
    assert [(c.get("phase"), c.get("content")) for c in m0_chunks] == [
        ("commentary", "先看过程解说。"),
        ("final_answer", "前半段"),
    ]

    # Continuation carries continued_from_message_id.
    m1_chunks = [
        it["data"]
        for it in sse_items
        if it["event"] == "message_chunk" and it["data"].get("id") == "m-1"
    ]
    assert len(m1_chunks) == 1
    assert m1_chunks[0]["continued_from_message_id"] == "m-0"

    # Close-out tool_calls carries close_out=True.
    tc_events = [it["data"] for it in sse_items if it["event"] == "tool_calls"]
    assert len(tc_events) == 1
    assert tc_events[0].get("close_out") is True

    # Core extraction from replay SSE and direct checkpoint messages match byte-for-byte.
    from_replay = extract_deliverable_text_from_events(sse_items)
    from_checkpoint = extract_deliverable_text_from_messages(messages)
    assert from_replay.text == "前半段后半段"
    assert from_checkpoint.text == "前半段后半段"
    assert _main_text(sse_items) == "前半段后半段"
    assert from_replay.saw_business_tool_call is False
    assert from_checkpoint.saw_business_tool_call is False


@pytest.mark.asyncio
async def test_live_sse_producer_continuation_link_first_chunk_and_discard() -> None:
    """§5.5: continuation_link emitted before handler(request) stamps the VERY FIRST
    live message_chunk of msg_1 with continued_from_message_id='msg_0'."""
    producer = RunSSEProducer(thread_id="t-live", run_id="r-live")

    async def _fake_graph_stream():
        # Attempt 0: streamed fragment "═" then discarded
        yield (
            (),
            "messages",
            (
                AIMessageChunk(id="msg_bad", content="═"),
                {"langgraph_node": "model"},
            ),
        )
        yield (
            (),
            "custom",
            {"type": "message_discarded", "agent": "main", "id": "msg_bad"},
        )
        # Attempt 1: output_cap prefix msg_0
        yield (
            (),
            "messages",
            (
                AIMessageChunk(
                    id="msg_0",
                    content="前半段",
                    response_metadata={"finish_reason": "MAX_TOKENS"},
                ),
                {"langgraph_node": "model"},
            ),
        )
        # Middleware emits continuation_link BEFORE invoking continuation call
        yield (
            (),
            "custom",
            {
                "type": "continuation_link",
                "agent": "main",
                "continued_from_message_id": "msg_0",
            },
        )
        # Attempt 2: continuation tokens of msg_1 (first token is reasoning, then text)
        yield (
            (),
            "messages",
            (
                AIMessageChunk(
                    id="msg_1",
                    content=[{"type": "thinking", "thinking": "继续写"}],
                ),
                {"langgraph_node": "model"},
            ),
        )
        yield (
            (),
            "messages",
            (
                AIMessageChunk(id="msg_1", content="后"),
                {"langgraph_node": "model"},
            ),
        )
        yield (
            (),
            "messages",
            (
                AIMessageChunk(
                    id="msg_1",
                    content="半段",
                    response_metadata={"finish_reason": "STOP"},
                ),
                {"langgraph_node": "model"},
            ),
        )

    class _FakeGraph:
        async def astream(self, *_args, **_kwargs):
            async for item in _fake_graph_stream():
                yield item

    parsed_events: list[dict] = []
    async for raw_sse in producer.stream_workflow(_FakeGraph(), {}, {}):
        ev_type = None
        ev_data = None
        for line in raw_sse.strip().splitlines():
            if line.startswith("event: "):
                ev_type = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                ev_data = json.loads(line[len("data: ") :])
        if ev_type and isinstance(ev_data, dict):
            parsed_events.append({"event": ev_type, "data": ev_data})

    discard_events = [e for e in parsed_events if e["event"] == "message_discarded"]
    assert len(discard_events) == 1
    assert discard_events[0]["data"]["id"] == "msg_bad"

    msg1_text_chunks = [
        e["data"]
        for e in parsed_events
        if e["event"] == "message_chunk"
        and e["data"].get("id") == "msg_1"
        and e["data"].get("content_type") == "text"
    ]
    assert len(msg1_text_chunks) >= 1
    assert msg1_text_chunks[0]["continued_from_message_id"] == "msg_0"
    assert "".join(c["content"] for c in msg1_text_chunks) == "后半段"

    extracted = extract_deliverable_text_from_events(parsed_events)
    assert extracted.text == "前半段后半段"


def test_resolve_live_continuation_id_ignores_same_id() -> None:
    pending = {"main": "msg_0"}
    bound: dict[tuple[str, str], str] = {}
    # Trailing chunk of msg_0 itself must not consume pending link.
    assert (
        resolve_live_continuation_id(
            pending, bound, "main", "msg_0", AIMessageChunk(id="msg_0", content="")
        )
        is None
    )
    assert pending == {"main": "msg_0"}
    # First chunk of msg_1 consumes pending link and binds it.
    assert (
        resolve_live_continuation_id(
            pending, bound, "main", "msg_1", AIMessageChunk(id="msg_1", content="续")
        )
        == "msg_0"
    )
    assert pending == {}
    assert bound[("main", "msg_1")] == "msg_0"


def test_channel_empty_delivery_notice_four_branches() -> None:
    # 1. send_message went only to another chat (is_current=False -> sent_to_current_count=0, no current agent_send)
    assert (
        build_channel_empty_delivery_notice(
            saw_business_tool_call=True,
            sent_to_current_count=0,
            has_delivered_current_agent_send=False,
            has_failed_current_agent_send=False,
        )
        == CHANNEL_EMPTY_AFTER_TOOLS_NOTICE
    )
    # 2. send_message to current chat failed (status='failed', no delivered)
    assert (
        build_channel_empty_delivery_notice(
            saw_business_tool_call=True,
            sent_to_current_count=0,
            has_delivered_current_agent_send=False,
            has_failed_current_agent_send=True,
        )
        == CHANNEL_SEND_FAILED_AFTER_TOOLS_NOTICE
    )
    # 3. send_message to current chat delivered files-only (has_delivered_current_agent_send=True) -> skip
    assert (
        build_channel_empty_delivery_notice(
            saw_business_tool_call=True,
            sent_to_current_count=0,
            has_delivered_current_agent_send=True,
            has_failed_current_agent_send=False,
        )
        is None
    )
    # 4. send_message to current chat delivered text (sent_to_current_count=1) -> skip
    assert (
        build_channel_empty_delivery_notice(
            saw_business_tool_call=True,
            sent_to_current_count=1,
            has_delivered_current_agent_send=True,
            has_failed_current_agent_send=False,
        )
        is None
    )


def test_automation_empty_excerpt_resolution() -> None:
    assert (
        resolve_automation_empty_excerpt(
            extracted_text="",
            sent_text="推送内容",
            has_delivered_agent_send=True,
            has_delivered_files_only=False,
        )
        == "推送内容"
    )
    assert (
        resolve_automation_empty_excerpt(
            extracted_text="",
            sent_text=None,
            has_delivered_agent_send=True,
            has_delivered_files_only=True,
        )
        == AUTOMATION_FILES_ONLY_EXCERPT
    )
    assert (
        resolve_automation_empty_excerpt(
            extracted_text="",
            sent_text="失败的发送参数",
            has_delivered_agent_send=False,
            has_delivered_files_only=False,
            saw_business_tool_call=True,
        )
        == AUTOMATION_EMPTY_AFTER_TOOLS_SUMMARY
    )


@pytest.mark.asyncio
async def test_read_run_answer_strips_thinking_and_does_not_fallback_to_preamble() -> None:
    from src.server.services.history.reader import CheckpointHistoryReader

    turn_preamble_only = SimpleNamespace(
        run_id="run-1",
        messages=[
            HumanMessage("检查财报"),
            AIMessage(
                id="m1",
                content="我先去查财报。",
                tool_calls=[
                    {"id": "c1", "name": "sec_query", "args": {}, "type": "tool_call"}
                ],
            ),
            ToolMessage(content="done", tool_call_id="c1", name="sec_query"),
        ],
    )
    turn_thinking_leak = SimpleNamespace(
        run_id="run-2",
        messages=[
            HumanMessage("总结一下"),
            AIMessage(
                id="m2",
                content="<thinking>internal</thinking>结论：营收增长 18%。\n\n![img](charts/rev.png)",
                response_metadata={"captured_images": {"charts/rev.png": "https://cdn/rev.png"}},
            ),
        ],
    )

    reader = create_autospec(CheckpointHistoryReader, instance=True)
    reader.aget_run_turn = AsyncMock(
        side_effect=lambda _tid, rid: (
            turn_preamble_only if rid == "run-1" else turn_thinking_leak
        )
    )

    with patch(
        "src.server.services.history.reader.CheckpointHistoryReader.get_instance",
        return_value=reader,
    ):
        ans1 = await read_run_answer("t-1", "run-1")
        ans2 = await read_run_answer("t-1", "run-2")

    assert ans1 == RunAnswer(text=None, sent=None)
    assert ans2 == RunAnswer(
        text="结论：营收增长 18%。\n\n![img](https://cdn/rev.png)", sent=None
    )


def test_automation_final_text_prefers_extracted_text_over_sent(monkeypatch) -> None:
    from src.dalton.channels.deliverable_text import resolve_automation_final_text

    monkeypatch.setenv("DALTON_STRICT_DELIVERABLE_TEXT", "1")
    assert (
        resolve_automation_final_text(RunAnswer(text="正式最终结论", sent="较早的发送参数"))
        == "正式最终结论"
    )
    assert resolve_automation_final_text(RunAnswer(text=None, sent="直发内容")) == "直发内容"
    assert resolve_automation_final_text(RunAnswer(text="", sent=None)) is None

    monkeypatch.setenv("DALTON_STRICT_DELIVERABLE_TEXT", "0")
    assert (
        resolve_automation_final_text(RunAnswer(text="正式最终结论", sent="较早的发送参数"))
        == "较早的发送参数"
    )


def test_automation_excerpt_files_only_ignores_failed_text_send_and_text_send_uses_text_wording() -> None:
    from src.dalton.channels.deliverable_text import AUTOMATION_TEXT_DELIVERED_EXCERPT

    # Only attachment succeeded while text send failed: must NOT show failed text param!
    assert (
        resolve_automation_empty_excerpt(
            extracted_text="",
            sent_text="失败的文字参数",
            has_delivered_agent_send=True,
            has_delivered_text_agent_send=False,
            has_delivered_files_only=True,
        )
        == AUTOMATION_FILES_ONLY_EXCERPT
    )
    # Text send succeeded, but RunAnswer.sent is None (another tool ran afterward):
    # must NOT claim files-only!
    assert (
        resolve_automation_empty_excerpt(
            extracted_text="",
            sent_text=None,
            has_delivered_agent_send=True,
            has_delivered_text_agent_send=True,
            has_delivered_files_only=False,
        )
        == AUTOMATION_TEXT_DELIVERED_EXCERPT
    )


@pytest.mark.asyncio
async def test_automation_excerpt_binds_to_delivered_ledger_record_in_checkpoint() -> None:
    from src.dalton.channels.deliverable_text import (
        resolve_completed_automation_excerpt_from_db,
    )
    from src.dalton.database import channel_gateway as cg_db
    from src.server.services.history.reader import CheckpointHistoryReader

    turn = SimpleNamespace(
        run_id="run-auto-1",
        messages=[
            HumanMessage("发早报"),
            AIMessage(
                id="m1",
                content="",
                tool_calls=[
                    {"id": "tc-ok", "name": "send_message", "args": {"text": "成功送达的早报内容"}},
                    {"id": "tc-fail", "name": "send_message", "args": {"text": "失败的目标参数"}},
                ],
            ),
        ],
    )
    reader = create_autospec(CheckpointHistoryReader, instance=True)
    reader.aget_run_turn.return_value = turn
    db_status = create_autospec(
        cg_db.get_execution_agent_send_delivery_status,
        return_value={
            "has_delivered_agent_send": True,
            "has_delivered_text_agent_send": True,
            "has_delivered_files_only": False,
            "delivered_text_delivery_keys": ["run-auto-1:tc-ok"],
        },
    )

    with (
        patch("src.dalton.database.channel_gateway.get_execution_agent_send_delivery_status", db_status),
        patch("src.server.services.history.reader.CheckpointHistoryReader.get_instance", return_value=reader),
    ):
        excerpt = await resolve_completed_automation_excerpt_from_db(
            "00000000-0000-0000-0000-000000000001",
            answer=RunAnswer(text=None, sent="失败的目标参数"),
            excerpt=None,
            thread_id="th-auto-1",
            run_id="run-auto-1",
        )

    assert excerpt == "成功送达的早报内容"


def test_sandbox_image_links_consistent_across_live_replay_output_and_checkpoint() -> None:
    from src.server.services.history.committed import _span_texts
    from src.server.services.history.projector import (
        history_events_to_sse,
        messages_to_history_events,
    )
    from src.server.services.history.replay import facts as replay_facts

    img_map = {"charts/rev.png": "https://cdn.example/response-images/t/abc/rev.png"}
    msg = AIMessage(
        id="m-img",
        content="营收走势如下：\n\n![营收图](charts/rev.png)",
        response_metadata={"captured_images": dict(img_map)},
    )
    expected = "营收走势如下：\n\n![营收图](https://cdn.example/response-images/t/abc/rev.png)"

    # 1. Checkpoint / automation extractor
    ckpt = extract_deliverable_text_from_messages([msg], resolve_images=True)
    assert ckpt.text == expected

    # 2. Replay path
    replay_items = history_events_to_sse(messages_to_history_events([msg]), thread_id="th-img")
    replay_facts.apply_image_url_map(
        replay_items, img_map, by_message=replay_facts.recorded_by_message([msg])
    )
    assert extract_deliverable_text_from_events(replay_items).text == expected

    # 3. Thread output path (committed._span_texts)
    span = SimpleNamespace(messages=[msg], new_ui_records=[])
    committed_out = _span_texts(
        "th-img",
        {"resp-1": span},
        responses={"resp-1": {"replay_facts": {"images": dict(img_map)}}},
    )
    assert committed_out["resp-1"] == expected

    # 4. Live SSE path (message_chunk + run_end.images)
    from src.dalton.channels.deliverable_text import DeliverableTextAccumulator

    live_acc = DeliverableTextAccumulator()
    live_acc.push_event(
        "message_chunk",
        {
            "agent": "main",
            "id": "m-img",
            "content_type": "text",
            "content": "营收走势如下：\n\n![营收图](charts/rev.png)",
        },
    )
    live_acc.push_event("run_end", {"run_id": "run-img", "outcome": "completed", "images": dict(img_map)})
    assert live_acc.result().text == expected


@pytest.mark.asyncio
async def test_thread_output_does_not_attach_previous_turn_when_latest_empty(monkeypatch) -> None:
    from src.server.services import thread_output as to_mod

    rows = [
        {"conversation_response_id": "r-prev", "turn_index": 1, "status": "completed", "metadata": {}},
        {"conversation_response_id": "r-curr", "turn_index": 2, "status": "completed", "metadata": {}},
    ]

    async def _recent(_tid: str, limit: int = 1):
        return rows[-limit:]

    async def _committed(_tid: str, turns_map):
        return {rid: ("上一轮正文" if rid == "r-prev" else "") for rid in turns_map}

    monkeypatch.setenv("DALTON_STRICT_DELIVERABLE_TEXT", "1")
    with (
        patch.object(to_mod, "get_thread_by_id", create_autospec(to_mod.get_thread_by_id, return_value={"workspace_id": None, "current_status": "completed"})),
        patch.object(to_mod.tl_db, "get_active_run", create_autospec(to_mod.tl_db.get_active_run, return_value=None)),
        patch.object(to_mod, "get_recent_responses_for_thread", create_autospec(to_mod.get_recent_responses_for_thread, side_effect=_recent)),
        patch.object(to_mod, "committed_texts", create_autospec(to_mod.committed_texts, side_effect=_committed)),
    ):
        out = await to_mod.extract_text_from_thread("th-empty-curr", turns=1)

    assert out is not None
    assert out.answer == ""
    assert "上一轮正文" not in out.text
    assert "no text" in out.text

    # Rollback switch DALTON_STRICT_DELIVERABLE_TEXT=0 restores legacy lookback.
    monkeypatch.setenv("DALTON_STRICT_DELIVERABLE_TEXT", "0")
    with (
        patch.object(to_mod, "get_thread_by_id", create_autospec(to_mod.get_thread_by_id, return_value={"workspace_id": None, "current_status": "completed"})),
        patch.object(to_mod.tl_db, "get_active_run", create_autospec(to_mod.tl_db.get_active_run, return_value=None)),
        patch.object(to_mod, "get_recent_responses_for_thread", create_autospec(to_mod.get_recent_responses_for_thread, side_effect=_recent)),
        patch.object(to_mod, "committed_texts", create_autospec(to_mod.committed_texts, side_effect=_committed)),
    ):
        legacy_out = await to_mod.extract_text_from_thread("th-empty-curr", turns=1)

    assert legacy_out is not None
    assert "上一轮正文" in legacy_out.text
    assert legacy_out.answer == ""


@pytest.mark.asyncio
async def test_r3_live_sse_preserves_block_phase_on_mixed_frame_and_thinking_leak_release() -> None:
    from src.dalton.channels.deliverable_text import DeliverableTextAccumulator

    producer = RunSSEProducer(thread_id="th-r3-phase", run_id="run-r3-phase")
    live_acc = DeliverableTextAccumulator()

    # 1. Single Responses frame carrying BOTH commentary and final_answer blocks.
    mixed_frames = []
    async for frame in producer._process_message_chunk(
        AIMessageChunk(
            id="m-mixed",
            content=[
                {"type": "text", "text": "先核对一下数据。", "phase": "commentary"},
                {"type": "text", "text": "正式结论：营收增长 20%。", "phase": "final_answer"},
            ],
            response_metadata={"status": "completed"},
        ),
        "main",
        {"langgraph_node": "model"},
    ):
        mixed_frames.append(frame)
        live_acc.push_event(frame.event, frame.data)

    text_frames = [
        f.data
        for f in mixed_frames
        if f.event == "message_chunk" and f.data.get("content_type") == "text"
    ]
    assert [(d.get("phase"), d.get("content")) for d in text_frames] == [
        ("commentary", "先核对一下数据。"),
        ("final_answer", "正式结论：营收增长 20%。"),
    ]
    assert live_acc.result().text == "正式结论：营收增长 20%。"

    # 2. Commentary opening with <thinking> buffered until finish frame MUST keep phase="commentary".
    producer2 = RunSSEProducer(thread_id="th-r3-think", run_id="run-r3-think")
    live_acc2 = DeliverableTextAccumulator()
    released_frames = []
    for chunk in [
        AIMessageChunk(
            id="m-think-comm",
            content=[{"type": "text", "index": 0, "text": "", "phase": "commentary"}],
        ),
        AIMessageChunk(
            id="m-think-comm",
            content=[{"type": "text", "index": 0, "text": "<thinking>internal</thinking>过程解说"}],
        ),
        AIMessageChunk(
            id="m-think-comm",
            content="",
            response_metadata={"status": "completed"},
        ),
    ]:
        async for frame in producer2._process_message_chunk(
            chunk, "main", {"langgraph_node": "model"}
        ):
            released_frames.append(frame)
            live_acc2.push_event(frame.event, frame.data)

    comm_frames = [
        f.data
        for f in released_frames
        if f.event == "message_chunk" and f.data.get("content_type") == "text"
    ]
    assert len(comm_frames) == 1
    assert comm_frames[0]["phase"] == "commentary"
    assert comm_frames[0]["content"] == "过程解说"
    # Live channel deliverable text must drop commentary!
    assert live_acc2.result().text == ""


def test_r3_live_sse_resolves_per_message_captured_images_for_rewritten_same_path() -> None:
    from src.dalton.channels.deliverable_text import DeliverableTextAccumulator

    producer = RunSSEProducer(thread_id="th-r3-img", run_id="run-r3-img")
    m1 = AIMessage(
        id="m-1",
        content="第一版：\n![图](charts/price.png)\n",
        response_metadata={"captured_images": {"charts/price.png": "https://cdn/v1.png"}},
    )
    m2 = AIMessage(
        id="m-2",
        content="第二版：\n![图](charts/price.png)",
        additional_kwargs={CONTINUED_FROM_KWARG: "m-1"},
        response_metadata={"captured_images": {"charts/price.png": "https://cdn/v2.png"}},
    )
    producer._note_saved_images({"model": {"messages": [m1, m2]}})
    by_msg = producer.captured_images_by_message
    assert by_msg["m-1"] == {"charts/price.png": "https://cdn/v1.png"}
    assert by_msg["m-2"] == {"charts/price.png": "https://cdn/v2.png"}

    acc = DeliverableTextAccumulator()
    acc.push_event(
        "message_chunk",
        {"agent": "main", "id": "m-1", "content_type": "text", "content": "第一版：\n![图](charts/price.png)\n"},
    )
    acc.push_event(
        "message_chunk",
        {
            "agent": "main",
            "id": "m-2",
            "continued_from_message_id": "m-1",
            "content_type": "text",
            "content": "第二版：\n![图](charts/price.png)",
        },
    )
    acc.push_event(
        "run_end",
        {
            "run_id": "run-r3-img",
            "outcome": "completed",
            "images": producer.captured_images,
            "images_by_message": by_msg,
        },
    )
    expected = "第一版：\n![图](https://cdn/v1.png)\n第二版：\n![图](https://cdn/v2.png)"
    assert acc.result().text == expected
    assert extract_deliverable_text_from_messages([m1, m2], resolve_images=True).text == expected


@pytest.mark.asyncio
async def test_r3_automation_excerpt_uses_generic_text_notice_on_binding_failure_and_reuses_first_read() -> None:
    from src.dalton.channels.deliverable_text import (
        AUTOMATION_TEXT_DELIVERED_EXCERPT,
        resolve_completed_automation_excerpt_from_db,
    )
    from src.dalton.database import channel_gateway as cg_db
    from src.server.services.history.reader import CheckpointHistoryReader

    # Case A: second checkpoint read fails -> MUST NOT use unverified RunAnswer.sent!
    failing_reader = create_autospec(CheckpointHistoryReader, instance=True)
    failing_reader.aget_run_turn = AsyncMock(side_effect=RuntimeError("db timeout"))
    db_status = create_autospec(
        cg_db.get_execution_agent_send_delivery_status,
        return_value={
            "has_delivered_agent_send": True,
            "has_delivered_text_agent_send": True,
            "has_delivered_files_only": False,
            "delivered_text_delivery_keys": ["run-fail-bind:tc-ok"],
        },
    )
    with (
        patch("src.dalton.database.channel_gateway.get_execution_agent_send_delivery_status", db_status),
        patch("src.server.services.history.reader.CheckpointHistoryReader.get_instance", return_value=failing_reader),
    ):
        excerpt = await resolve_completed_automation_excerpt_from_db(
            "00000000-0000-0000-0000-000000000002",
            answer=RunAnswer(text=None, sent="失败的发送B参数"),
            excerpt=None,
            thread_id="th-bind-fail",
            run_id="run-fail-bind",
        )
    assert excerpt == AUTOMATION_TEXT_DELIVERED_EXCERPT

    # Case B: read_run_answer already read messages -> second checkpoint read is skipped!
    turn = SimpleNamespace(
        run_id="run-reuse-1",
        messages=[
            HumanMessage("发早报"),
            AIMessage(
                id="m1",
                content="",
                tool_calls=[
                    {"id": "tc-ok", "name": "send_message", "args": {"text": "成功送达的早报A"}},
                    {"id": "tc-fail", "name": "send_message", "args": {"text": "失败的发送B"}},
                ],
            ),
        ],
    )
    reuse_reader = create_autospec(CheckpointHistoryReader, instance=True)
    reuse_reader.aget_run_turn = AsyncMock(return_value=turn)
    db_status_reuse = create_autospec(
        cg_db.get_execution_agent_send_delivery_status,
        return_value={
            "has_delivered_agent_send": True,
            "has_delivered_text_agent_send": True,
            "has_delivered_files_only": False,
            "delivered_text_delivery_keys": ["run-reuse-1:tc-ok"],
        },
    )
    with (
        patch("src.dalton.database.channel_gateway.get_execution_agent_send_delivery_status", db_status_reuse),
        patch("src.server.services.history.reader.CheckpointHistoryReader.get_instance", return_value=reuse_reader),
        patch("src.server.services.runs.finalization.settled_images", AsyncMock(return_value={})),
    ):
        ans = await read_run_answer("th-reuse", "run-reuse-1")
        assert reuse_reader.aget_run_turn.await_count == 1
        excerpt_reused = await resolve_completed_automation_excerpt_from_db(
            "00000000-0000-0000-0000-000000000003",
            answer=ans,
            excerpt=None,
            thread_id="th-reuse",
            run_id="run-reuse-1",
        )
    assert excerpt_reused == "成功送达的早报A"
    assert reuse_reader.aget_run_turn.await_count == 1


@pytest.mark.asyncio
async def test_r3_committed_archived_stream_runs_strict_extraction_on_retained_fields(monkeypatch) -> None:
    from src.server.services.history import committed

    archive = [
        {"event": "message_chunk", "data": {"agent": "main", "id": "m1", "content": "先查一下工具。", "content_type": "text"}},
        {"event": "tool_calls", "data": {"agent": "main", "tool_calls": [{"id": "c1", "name": "Bash", "args": {}}]}},
        {"event": "message_chunk", "data": {"agent": "main", "id": "m2", "phase": "commentary", "content": "过程解说", "content_type": "text"}},
        {"event": "message_chunk", "data": {"agent": "main", "id": "m2", "phase": "final_answer", "content": "最终归档结论", "content_type": "text"}},
    ]
    rows = {"r-arch": {"replay_facts": {"archive": archive}}}

    monkeypatch.setenv("DALTON_STRICT_DELIVERABLE_TEXT", "1")
    with (
        patch.object(committed, "get_branch_pointer", AsyncMock(return_value=None)),
        patch.object(committed, "get_replay_responses", AsyncMock(return_value=rows)),
    ):
        strict_texts = await committed.committed_texts("t-arch", {"r-arch": 0})
    assert strict_texts == {"r-arch": "最终归档结论"}

    monkeypatch.setenv("DALTON_STRICT_DELIVERABLE_TEXT", "0")
    with (
        patch.object(committed, "get_branch_pointer", AsyncMock(return_value=None)),
        patch.object(committed, "get_replay_responses", AsyncMock(return_value=rows)),
    ):
        legacy_texts = await committed.committed_texts("t-arch", {"r-arch": 0})
    assert legacy_texts == {"r-arch": "先查一下工具。过程解说最终归档结论"}


@pytest.mark.asyncio
async def test_r4_cross_phase_thinking_tag_preserved_across_commentary_and_final_answer() -> None:
    from langchain_core.messages import AIMessageChunk

    from src.dalton.channels.deliverable_text import DeliverableTextAccumulator
    from src.server.services.history.projector import (
        history_events_to_sse,
        messages_to_history_events,
    )

    # 1. Exact Codex P1 #2 repro: <thinking> opens in commentary and closes in final_answer.
    producer = RunSSEProducer(thread_id="th-r4-xphase", run_id="run-r4-xphase")
    live_acc = DeliverableTextAccumulator()
    emitted_frames = []
    for chunk in [
        AIMessageChunk(
            id="m-xphase-1",
            content=[{"type": "text", "index": 0, "text": "<thinking>内部分析一", "phase": "commentary"}],
        ),
        AIMessageChunk(
            id="m-xphase-1",
            content=[{"type": "text", "index": 1, "text": "内部分析二</thinking>正式结论", "phase": "final_answer"}],
        ),
        AIMessageChunk(
            id="m-xphase-1",
            content="",
            response_metadata={"status": "completed"},
        ),
    ]:
        async for frame in producer._process_message_chunk(
            chunk, "main", {"langgraph_node": "model"}
        ):
            emitted_frames.append(frame)
            live_acc.push_event(frame.event, frame.data)

    text_frames = [
        (f.data.get("phase"), f.data.get("content"))
        for f in emitted_frames
        if f.event == "message_chunk" and f.data.get("content_type") == "text"
    ]
    assert text_frames == [("final_answer", "正式结论")]
    assert live_acc.result().text == "正式结论"

    # Parity with checkpoint and replay
    committed_msg = AIMessage(
        id="m-xphase-1",
        content=[
            {"type": "text", "text": "<thinking>内部分析一", "phase": "commentary"},
            {"type": "text", "text": "内部分析二</thinking>正式结论", "phase": "final_answer"},
        ],
    )
    assert extract_deliverable_text_from_messages([committed_msg]).text == "正式结论"
    replay_events = history_events_to_sse(
        messages_to_history_events([committed_msg]), thread_id="th-r4-xphase"
    )
    assert extract_deliverable_text_from_events(replay_events).text == "正式结论"

    # 2. Opening <thinking> closes inside commentary, followed by final_answer -> both phases restored.
    producer2 = RunSSEProducer(thread_id="th-r4-xphase-2", run_id="run-r4-xphase-2")
    live_acc2 = DeliverableTextAccumulator()
    emitted_frames2 = []
    for chunk in [
        AIMessageChunk(
            id="m-xphase-2",
            content=[{"type": "text", "index": 0, "text": "<thinking>隐藏思考</thinking>过程说明", "phase": "commentary"}],
        ),
        AIMessageChunk(
            id="m-xphase-2",
            content=[{"type": "text", "index": 1, "text": "正式结论二", "phase": "final_answer"}],
        ),
        AIMessageChunk(
            id="m-xphase-2",
            content="",
            response_metadata={"status": "completed"},
        ),
    ]:
        async for frame in producer2._process_message_chunk(
            chunk, "main", {"langgraph_node": "model"}
        ):
            emitted_frames2.append(frame)
            live_acc2.push_event(frame.event, frame.data)

    text_frames2 = [
        (f.data.get("phase"), f.data.get("content"))
        for f in emitted_frames2
        if f.event == "message_chunk" and f.data.get("content_type") == "text"
    ]
    assert text_frames2 == [("commentary", "过程说明"), ("final_answer", "正式结论二")]
    assert live_acc2.result().text == "正式结论二"


@pytest.mark.asyncio
async def test_r4_update_inbound_progress_returns_unresolved_agent_sends_and_resolved_images(
    monkeypatch,
) -> None:
    from contextlib import asynccontextmanager

    from src.dalton.database import channel_gateway as cg_db

    run_id = "44444444-4444-4444-8444-444444444444"

    class _Cur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, sql, params=()):
            self._last_sql = " ".join(str(sql).split())

        async def fetchone(self):
            s = self._last_sql
            if "AS in_flight" in s:
                return {"in_flight": False}
            if "AS has_unknown_text" in s:
                return {"has_unknown_text": False}
            if "AS cnt" in s:
                return {"cnt": 1}
            if "AS unresolved_current_agent_sends" in s:
                return {
                    "has_delivered_current_agent_send": True,
                    "has_delivered_current_files_only": False,
                    "has_failed_current_agent_send": False,
                    "has_unresolved_current_agent_send_images": True,
                    "unresolved_current_agent_sends": [
                        {
                            "delivery_key": f"{run_id}:tc-img",
                            "delivered_text": "图表如下：\n![图](charts/raw.png)",
                            "unresolved_image_paths": ["charts/raw.png"],
                        }
                    ],
                }
            if "replay_facts -> 'images'" in s:
                return {"images": {"charts/raw.png": "https://cdn.example/raw.png"}}
            return None

    class _Conn:
        @asynccontextmanager
        async def transaction(self):
            yield

        def cursor(self, **_kw):
            return _Cur()

    @asynccontextmanager
    async def _fake_conn(_conn=None):
        yield _Conn()

    monkeypatch.setattr(cg_db, "get_db_connection", _fake_conn)
    res = await cg_db.update_inbound_progress(run_id=run_id)
    assert res["has_unresolved_current_agent_send_images"] is True
    assert res["unresolved_current_agent_sends"] == [
        {
            "delivery_key": f"{run_id}:tc-img",
            "delivered_text": "图表如下：\n![图](charts/raw.png)",
            "unresolved_image_paths": ["charts/raw.png"],
        }
    ]
    assert res["resolved_run_images"] == {"charts/raw.png": "https://cdn.example/raw.png"}


@pytest.mark.asyncio
async def test_r5_output_cap_named_tool_call_malformed_then_continuation_three_way_parity() -> None:
    """Codex P1: output_cap -> Antigravity named tool_calls + MALFORMED_FUNCTION_CALL ->
    continuation succeeds. Live SSE, checkpoint, and replay must all retain 前半段后半段."""
    from src.dalton.channels.deliverable_text import DeliverableTextAccumulator

    producer = RunSSEProducer(thread_id="th-r5-malformed", run_id="run-r5-malformed")

    async def _fake_graph_stream():
        # Attempt 0: output_cap truncated prefix streamed under lc_run--0 (committed id msg_0)
        yield (
            (),
            "custom",
            {"type": "attempt_start", "agent": "main", "attempt": 0},
        )
        yield (
            (),
            "messages",
            (
                AIMessageChunk(
                    id="lc_run--0",
                    content="前半段",
                    response_metadata={"finish_reason": "MAX_TOKENS"},
                ),
                {"langgraph_node": "model"},
            ),
        )
        yield (
            (),
            "custom",
            {
                "type": "continuation_link",
                "agent": "main",
                "continued_from_message_id": "msg_0",
            },
        )
        # Attempt 1: Antigravity emits named tool_calls frame, then MALFORMED_FUNCTION_CALL
        yield (
            (),
            "custom",
            {"type": "attempt_start", "agent": "main", "attempt": 1},
        )
        yield (
            (),
            "messages",
            (
                AIMessageChunk(
                    id="lc_run--bad",
                    content="",
                    tool_calls=[
                        {
                            "id": "antigravity_call_1",
                            "name": "Write",
                            "args": {"file_path": "model.py", "content": "x = 1"},
                            "type": "tool_call",
                        }
                    ],
                ),
                {"langgraph_node": "model"},
            ),
        )
        yield (
            (),
            "messages",
            (
                AIMessageChunk(
                    id="lc_run--bad",
                    content="",
                    response_metadata={"finish_reason": "MALFORMED_FUNCTION_CALL"},
                ),
                {"langgraph_node": "model"},
            ),
        )
        # RetryLoopBudget re-emits continuation_link(msg_0) after sanitized malformed_call
        yield (
            (),
            "custom",
            {
                "type": "continuation_link",
                "agent": "main",
                "continued_from_message_id": "msg_0",
            },
        )
        # Attempt 2: continuation succeeds
        yield (
            (),
            "custom",
            {"type": "attempt_start", "agent": "main", "attempt": 2},
        )
        yield (
            (),
            "messages",
            (
                AIMessageChunk(
                    id="lc_run--2",
                    content="后半段",
                    response_metadata={"finish_reason": "STOP"},
                ),
                {"langgraph_node": "model"},
            ),
        )

    class _FakeGraph:
        async def astream(self, *_args, **_kwargs):
            async for item in _fake_graph_stream():
                yield item

    live_events: list[dict] = []
    live_acc = DeliverableTextAccumulator()
    async for raw_sse in producer.stream_workflow(_FakeGraph(), {}, {}):
        ev_type = None
        ev_data = None
        for line in raw_sse.strip().splitlines():
            if line.startswith("event: "):
                ev_type = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                ev_data = json.loads(line[len("data: ") :])
        if ev_type and isinstance(ev_data, dict):
            live_events.append({"event": ev_type, "data": ev_data})
            live_acc.push_event(ev_type, ev_data)

    # Producer emitted the discarded tool_call_result and kept continuation pointing at lc_run--0.
    discarded_tc = [
        e["data"]
        for e in live_events
        if e["event"] == "tool_call_result" and e["data"].get("discarded") is True
    ]
    assert len(discarded_tc) == 1
    assert discarded_tc[0]["tool_call_id"] == "antigravity_call_1"
    assert discarded_tc[0]["discarded_reason"] == "malformed_call"

    tail_chunks = [
        e["data"]
        for e in live_events
        if e["event"] == "message_chunk"
        and e["data"].get("id") == "lc_run--2"
        and e["data"].get("content_type") == "text"
    ]
    assert len(tail_chunks) == 1
    assert tail_chunks[0]["continued_from_message_id"] == "lc_run--0"

    # Live, checkpoint, and replay all agree byte-for-byte.
    live_res = live_acc.result()
    assert live_res.text == "前半段后半段"
    assert live_res.saw_business_tool_call is False

    checkpoint_messages = [
        HumanMessage(content="请输出完整分析", id="h-r5"),
        AIMessage(id="msg_0", content="前半段"),
        AIMessage(
            id="msg_2",
            content="后半段",
            additional_kwargs={CONTINUED_FROM_KWARG: "msg_0"},
        ),
    ]
    checkpoint_res = extract_deliverable_text_from_messages(checkpoint_messages)
    assert checkpoint_res.text == "前半段后半段"
    assert checkpoint_res.saw_business_tool_call is False

    replay_sse = history_events_to_sse(
        messages_to_history_events(checkpoint_messages), thread_id="th-r5-malformed"
    )
    replay_res = extract_deliverable_text_from_events(replay_sse)
    assert replay_res.text == "前半段后半段"
    assert replay_res.saw_business_tool_call is False


@pytest.mark.asyncio
async def test_r5_same_path_two_image_versions_delivery_context_finalize_and_i7(
    monkeypatch,
) -> None:
    """Grok Medium: same sandbox image path rewritten (v1 -> v2) in one turn must record
    delivery-time message id + captured_images, persist replay_facts.images_by_message,
    and return per-delivery captures in I-7 without overwriting v1 with global v2."""
    from contextlib import asynccontextmanager

    from src.dalton.database import channel_gateway as cg_db
    from src.dalton.llm.near_empty_reply import aattach_retry_send_dedup
    from src.server.database.runs import lifecycle as lc_db

    run_id = "55555555-5555-4555-8555-555555555555"
    producer = RunSSEProducer(thread_id="th-r5-img", run_id=run_id)

    m_v1 = AIMessage(
        id="msg-v1",
        content="第一版：\n![图](charts/price.png)",
        tool_calls=[
            {
                "id": "tc-v1",
                "name": "send_message",
                "args": {"text": "第一版：\n![图](charts/price.png)"},
                "type": "tool_call",
            }
        ],
        response_metadata={"captured_images": {"charts/price.png": "https://cdn.example/v1.png"}},
    )
    m_v2 = AIMessage(
        id="msg-v2",
        content="第二版：\n![图](charts/price.png)",
        tool_calls=[
            {
                "id": "tc-v2",
                "name": "send_message",
                "args": {"text": "第二版：\n![图](charts/price.png)"},
                "type": "tool_call",
            }
        ],
        response_metadata={"captured_images": {"charts/price.png": "https://cdn.example/v2.png"}},
    )
    producer._note_saved_images({"model": {"messages": [m_v1]}})
    # At the moment tc-v1 sends, /agent/send body receives msg-v1 + v1.png.
    body_v1 = await aattach_retry_send_dedup(
        {
            "thread_id": "th-r5-img",
            "run_id": run_id,
            "tool_call_id": "tc-v1",
            "text": "第一版：\n![图](charts/price.png)",
            "files": [],
        },
        run_id=run_id,
        tool_call_id="tc-v1",
        thread_id="th-r5-img",
    )
    assert body_v1["source_message_id"] == "msg-v1"
    assert body_v1["captured_images"] == {"charts/price.png": "https://cdn.example/v1.png"}

    # Later in the same turn, charts/price.png is overwritten with v2.png.
    producer._note_saved_images({"model": {"messages": [m_v2]}})
    body_v2 = await aattach_retry_send_dedup(
        {
            "thread_id": "th-r5-img",
            "run_id": run_id,
            "tool_call_id": "tc-v2",
            "text": "第二版：\n![图](charts/price.png)",
            "files": [],
        },
        run_id=run_id,
        tool_call_id="tc-v2",
        thread_id="th-r5-img",
    )
    assert body_v2["source_message_id"] == "msg-v2"
    assert body_v2["captured_images"] == {"charts/price.png": "https://cdn.example/v2.png"}

    # Verify finalize_run persists BOTH replay_facts.images (v2) and replay_facts.images_by_message (v1 & v2).
    captured_replay_facts: list[dict] = []

    class _FinalizeCur:
        rowcount = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, sql, params=()):
            self._last_sql = str(sql)
            if "UPDATE conversation_responses" in self._last_sql and "replay_facts" in self._last_sql:
                for p in params:
                    val = getattr(p, "obj", p)
                    if isinstance(val, dict) and "images_by_message" in val:
                        captured_replay_facts.append(val)
                    elif isinstance(val, str) and '"images_by_message"' in val:
                        captured_replay_facts.append(json.loads(val))

        async def fetchone(self):
            return {
                "conversation_response_id": run_id,
                "conversation_thread_id": "th-r5-img",
                "status": "completed",
                "run_seq": 1,
                "turn_index": 1,
                "metadata": {},
                "usage_settled_at": None,
                "replay_facts": {},
            }

        async def fetchall(self):
            return []

    class _FinalizeConn:
        info = SimpleNamespace(transaction_status=None)

        @asynccontextmanager
        async def transaction(self):
            yield

        def cursor(self, **_kw):
            return _FinalizeCur()

    @asynccontextmanager
    async def _fake_lc_conn(_conn=None):
        yield _FinalizeConn()

    monkeypatch.setattr(lc_db, "_lifecycle_connection", _fake_lc_conn)
    outcome = lc_db.RunOutcome(
        status="completed",
        images=producer.captured_images,
        images_by_message=producer.captured_images_by_message,
    )
    await lc_db.finalize_run(run_id=run_id, thread_id="th-r5-img", outcome=outcome)
    assert len(captured_replay_facts) == 1
    assert captured_replay_facts[0]["images"] == {"charts/price.png": "https://cdn.example/v2.png"}
    assert captured_replay_facts[0]["images_by_message"] == {
        "msg-v1": {"charts/price.png": "https://cdn.example/v1.png"},
        "msg-v2": {"charts/price.png": "https://cdn.example/v2.png"},
    }

    # Verify I-7 returns per-delivery captures without overwriting msg-v1 with global v2.
    class _I7Cur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, sql, params=()):
            self._last_sql = " ".join(str(sql).split())

        async def fetchone(self):
            s = self._last_sql
            if "AS in_flight" in s:
                return {"in_flight": False}
            if "AS has_unknown_text" in s:
                return {"has_unknown_text": False}
            if "AS cnt" in s:
                return {"cnt": 2}
            if "AS unresolved_current_agent_sends" in s:
                return {
                    "has_delivered_current_agent_send": True,
                    "has_delivered_current_files_only": False,
                    "has_failed_current_agent_send": False,
                    "has_unresolved_current_agent_send_images": True,
                    "unresolved_current_agent_sends": [
                        {
                            "delivery_key": f"{run_id}:tc-v1",
                            "delivered_text": "第一版：\n![图](charts/price.png)",
                            "unresolved_image_paths": ["charts/price.png"],
                            "source_message_id": "msg-v1",
                            "captured_images": {},
                        },
                        {
                            "delivery_key": f"{run_id}:tc-v2",
                            "delivered_text": "第二版：\n![图](charts/price.png)",
                            "unresolved_image_paths": ["charts/price.png"],
                            "source_message_id": "msg-v2",
                            "captured_images": {"charts/price.png": "https://cdn.example/v2.png"},
                        },
                    ],
                }
            if "replay_facts -> 'images'" in s:
                return {
                    "images": {"charts/price.png": "https://cdn.example/v2.png"},
                    "images_by_message": {
                        "msg-v1": {"charts/price.png": "https://cdn.example/v1.png"},
                        "msg-v2": {"charts/price.png": "https://cdn.example/v2.png"},
                    },
                }
            return None

    class _I7Conn:
        @asynccontextmanager
        async def transaction(self):
            yield

        def cursor(self, **_kw):
            return _I7Cur()

    @asynccontextmanager
    async def _fake_i7_conn(_conn=None):
        yield _I7Conn()

    monkeypatch.setattr(cg_db, "get_db_connection", _fake_i7_conn)
    i7_res = await cg_db.update_inbound_progress(run_id=run_id)
    assert i7_res["unresolved_current_agent_sends"][0]["source_message_id"] == "msg-v1"
    assert i7_res["unresolved_current_agent_sends"][0]["captured_images"] == {
        "charts/price.png": "https://cdn.example/v1.png"
    }
    assert i7_res["unresolved_current_agent_sends"][1]["source_message_id"] == "msg-v2"
    assert i7_res["unresolved_current_agent_sends"][1]["captured_images"] == {
        "charts/price.png": "https://cdn.example/v2.png"
    }
    assert i7_res["resolved_run_images_by_message"] == {
        "msg-v1": {"charts/price.png": "https://cdn.example/v1.png"},
        "msg-v2": {"charts/price.png": "https://cdn.example/v2.png"},
    }
