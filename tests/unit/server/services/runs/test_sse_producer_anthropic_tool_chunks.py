"""Anthropic's streamed tool arguments name their call.

Anthropic sends a call's name and id once, on its ``tool_use`` block, then
streams the arguments as ``input_json_delta`` blocks that carry neither. The
client hides and labels a preparing call by name, so the producer puts the
name and id on every chunk it emits. Block indexes restart at 0 on every model
call, so a call must never inherit what an earlier one left at its index.
"""

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage

from src.server.services.runs.sse_producer import (
    _DISCARDED_INTERRUPTED_TEXT,
    _DISCARDED_MALFORMED_TEXT,
    _DISCARDED_TRUNCATED_TEXT,
    RunSSEProducer,
)

# A retry or fallback inside one model task streams in the same lane.
LANE = "model:task-1"


def _tool_use(msg_id, index, name, call_id):
    return AIMessageChunk(
        content=[{"type": "tool_use", "id": call_id, "name": name, "input": {}, "index": index}],
        id=msg_id,
    )


def _delta(msg_id, index, partial_json):
    return AIMessageChunk(
        content=[{"type": "input_json_delta", "partial_json": partial_json, "index": index}],
        id=msg_id,
    )


def _finish(msg_id):
    return AIMessageChunk(content=[], id=msg_id, response_metadata={"stop_reason": "tool_use"})


async def _emit(producer, *chunks):
    frames = []
    for chunk in chunks:
        async for frame in producer._process_message_chunk(chunk, LANE, {"langgraph_node": "model"}):
            frames.append(frame)
    return frames


def _arg_chunks(frames):
    return [
        {k: chunk[k] for k in ("name", "id", "index", "args")}
        for f in frames
        if f.event == "tool_call_chunks"
        for chunk in f.data["tool_call_chunks"]
    ]


def _tool_calls(frames):
    return [call for f in frames if f.event == "tool_calls" for call in f.data["tool_calls"]]


def _producer():
    return RunSSEProducer(thread_id="t-anthropic", run_id="r-anthropic")


@pytest.mark.asyncio
async def test_each_argument_chunk_names_its_call_and_the_next_call_its_own():
    producer = _producer()
    first = await _emit(
        producer,
        _tool_use("m1", 0, "TodoWrite", "toolu_a"),
        _delta("m1", 0, ""),
        _delta("m1", 0, '{"todos": '),
        _delta("m1", 0, "[]}"),
        _finish("m1"),
    )
    second = await _emit(
        producer,
        _tool_use("m2", 0, "Bash", "toolu_b"),
        _delta("m2", 0, '{"command": '),
        _delta("m2", 0, '"ls"}'),
        _finish("m2"),
    )

    assert _arg_chunks(first) == [
        {"name": "TodoWrite", "id": "toolu_a", "index": 0, "args": ""},
        {"name": "TodoWrite", "id": "toolu_a", "index": 0, "args": '{"todos": '},
        {"name": "TodoWrite", "id": "toolu_a", "index": 0, "args": "[]}"},
    ]
    assert _arg_chunks(second) == [
        {"name": "Bash", "id": "toolu_b", "index": 0, "args": '{"command": '},
        {"name": "Bash", "id": "toolu_b", "index": 0, "args": '"ls"}'},
    ]
    assert [(c["name"], c["id"], c["args"]) for c in _tool_calls(first + second)] == [
        ("TodoWrite", "toolu_a", {"todos": []}),
        ("Bash", "toolu_b", {"command": "ls"}),
    ]
    assert producer.anthropic_tool_call_state == {}


@pytest.mark.asyncio
async def test_a_retry_takes_nothing_from_the_call_whose_stream_died():
    producer = _producer()
    # The first attempt dies mid-arguments, with no finish, leaving calls at
    # index 0 and 1; the retry writes one call, at index 0.
    await _emit(
        producer,
        _tool_use("m1", 0, "TodoWrite", "toolu_dead_0"),
        _delta("m1", 0, '{"todos": [{"content": "Che'),
        _tool_use("m1", 1, "Read", "toolu_dead_1"),
        _delta("m1", 1, '{"file_path": "/tmp/a'),
    )
    retry = await _emit(
        producer,
        _tool_use("m2", 0, "Bash", "toolu_retry"),
        _delta("m2", 0, '{"command": "ls"}'),
        _finish("m2"),
    )

    assert _arg_chunks(retry) == [
        {"name": "Bash", "id": "toolu_retry", "index": 0, "args": '{"command": "ls"}'},
    ]
    assert [(c["name"], c["id"], c["args"]) for c in _tool_calls(retry)] == [
        ("Bash", "toolu_retry", {"command": "ls"}),
    ]
    assert producer.anthropic_tool_call_state == {}


@pytest.mark.asyncio
async def test_lanes_keep_their_own_calls_at_the_same_index():
    # Another lane's message is not an earlier attempt at this one's call.
    producer = _producer()
    frames = []
    for lane, chunk in (
        ("model:task-1", _tool_use("m1", 0, "Bash", "toolu_main")),
        ("research:sub-1", _tool_use("s1", 0, "WebSearch", "toolu_sub")),
        ("model:task-1", _delta("m1", 0, '{"command": "ls"}')),
        ("research:sub-1", _delta("s1", 0, '{"query": "x"}')),
    ):
        async for frame in producer._process_message_chunk(chunk, lane, {}):
            frames.append(frame)

    assert [
        (f.data["agent"], f.data["tool_call_chunks"][0]["name"], f.data["tool_call_chunks"][0]["id"])
        for f in frames
    ] == [
        ("model:task-1", "Bash", "toolu_main"),
        ("research:sub-1", "WebSearch", "toolu_sub"),
    ]


async def _claude_call_dies(producer):
    # A Claude call streams its arguments and dies before its finish.
    await _emit(
        producer,
        _tool_use("m1", 0, "Bash", "toolu_dead"),
        _delta("m1", 0, '{"command": "ls"}'),
    )


@pytest.mark.asyncio
async def test_a_plain_answer_from_a_fallback_sends_no_call_for_the_dead_one():
    # The fallback is another provider (a Responses API model) in the same lane.
    producer = _producer()
    await _claude_call_dies(producer)
    fallback = await _emit(
        producer,
        AIMessageChunk(content=[{"type": "text", "text": "Done.", "index": 0}], id="resp_1"),
        AIMessageChunk(content=[], id="resp_1", response_metadata={"status": "completed"}),
    )

    assert _tool_calls(fallback) == []
    assert [f.data["finish_reason"] for f in fallback if "finish_reason" in f.data] == ["stop"]
    assert producer.anthropic_tool_call_state == {}


@pytest.mark.asyncio
async def test_a_fallback_sends_its_own_call_and_not_the_dead_one():
    producer = _producer()
    await _claude_call_dies(producer)
    fallback = await _emit(
        producer,
        AIMessageChunk(
            content=[{
                "type": "function_call", "name": "Read", "call_id": "call_1",
                "arguments": '{"file_path": "a.md"}', "index": 0,
            }],
            id="resp_1",
        ),
        AIMessageChunk(content=[], id="resp_1", response_metadata={"status": "completed"}),
    )

    assert [(c["name"], c["id"], c["args"]) for c in _tool_calls(fallback)] == [
        ("Read", "call_1", {"file_path": "a.md"}),
    ]
    assert producer.anthropic_tool_call_state == {}


@pytest.mark.asyncio
async def test_responses_api_incomplete_clears_accumulated_tool_call_state():
    # DALTON-HOOK(empty-turn-guard): a Responses API message that streams
    # function_call arguments and ends with status="incomplete"
    # (max_output_tokens) must drop its accumulated call so the retried text
    # message's "completed" never emits a stale Write tool_calls event.
    producer = _producer()
    first = await _emit(
        producer,
        AIMessageChunk(
            content=[{
                "type": "function_call", "name": "Write", "call_id": "call_trunc",
                "arguments": '{"file_path": "a.py"', "index": 0,
            }],
            id="resp_1",
        ),
        AIMessageChunk(
            content=[{"type": "function_call", "arguments": ', "content": "half', "index": 0}],
            id="resp_1",
        ),
        AIMessageChunk(
            content=[],
            id="resp_1",
            response_metadata={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            },
        ),
    )
    second = await _emit(
        producer,
        AIMessageChunk(content=[{"type": "text", "text": "已分段完成。", "index": 0}], id="resp_2"),
        AIMessageChunk(content=[], id="resp_2", response_metadata={"status": "completed"}),
    )

    assert _tool_calls(first + second) == []
    assert [f.data["finish_reason"] for f in second if "finish_reason" in f.data] == ["stop"]
    assert producer.function_call_state == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish_reason",
    ["MAX_TOKENS", "MALFORMED_FUNCTION_CALL"],
)
async def test_antigravity_emitted_tool_calls_settled_as_not_executed(finish_reason):
    # DALTON-HOOK(empty-turn-guard): Antigravity emits complete tool_calls on
    # an earlier chunk before finishReason arrives; when finish_reason is
    # output_cap or malformed_call, SSE must emit tool_call_result(status="error")
    # so the UI marks those cards failed/not-executed.
    producer = _producer()
    frames = await _emit(
        producer,
        AIMessageChunk(
            content="我将写入文件",
            id="ag-1",
            tool_calls=[
                {
                    "name": "Write",
                    "args": {"file_path": "model.py", "content": "x = 1"},
                    "id": "antigravity_call_1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessageChunk(
            content="",
            id="ag-1",
            response_metadata={"finish_reason": finish_reason},
        ),
    )

    assert [(c["name"], c["id"]) for c in _tool_calls(frames)] == [("Write", "antigravity_call_1")]
    results = [f.data for f in frames if f.event == "tool_call_result"]
    assert len(results) == 1
    assert results[0]["tool_call_id"] == "antigravity_call_1"
    assert results[0]["status"] == "error"
    assert results[0]["discarded"] is True
    expected_reason, expected_text = (
        ("output_cap", _DISCARDED_TRUNCATED_TEXT)
        if finish_reason == "MAX_TOKENS"
        else ("malformed_call", _DISCARDED_MALFORMED_TEXT)
    )
    assert results[0]["discarded_reason"] == expected_reason
    assert results[0]["content"] == expected_text
    assert "已自动重试" not in results[0]["content"]
    assert producer._emitted_tool_calls == {}


@pytest.mark.asyncio
async def test_antigravity_stream_interrupted_before_finish_settles_old_calls_on_retry():
    # DALTON-HOOK(empty-turn-guard): when a stream breaks mid-call after emitting
    # tool_calls without finish_reason and a retry opens a new message_id in the
    # same agent lane, settle the orphaned calls as not executed and clear them.
    producer = _producer()
    frames = await _emit(
        producer,
        AIMessageChunk(
            content="",
            id="ag-broken-1",
            tool_calls=[
                {
                    "name": "Write",
                    "args": {"file_path": "model.py", "content": "x = 1"},
                    "id": "ag_interrupted_call",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessageChunk(content="重试正文。", id="ag-retry-2"),
        AIMessageChunk(content="", id="ag-retry-2", response_metadata={"finish_reason": "STOP"}),
    )

    assert [(c["name"], c["id"]) for c in _tool_calls(frames)] == [("Write", "ag_interrupted_call")]
    results = [f.data for f in frames if f.event == "tool_call_result"]
    assert len(results) == 1
    assert results[0]["tool_call_id"] == "ag_interrupted_call"
    assert results[0]["status"] == "error"
    assert results[0]["discarded"] is True
    assert results[0]["discarded_reason"] == "interrupted"
    assert results[0]["content"] == _DISCARDED_INTERRUPTED_TEXT
    assert producer._emitted_tool_calls == {}


@pytest.mark.asyncio
async def test_flush_pending_tool_calls_clears_remaining_at_turn_end():
    producer = _producer()
    await _emit(
        producer,
        AIMessageChunk(
            content="",
            id="ag-orphan",
            tool_calls=[
                {
                    "name": "Write",
                    "args": {"file_path": "end.py", "content": "pass"},
                    "id": "ag_orphan_call",
                    "type": "tool_call",
                }
            ],
        ),
    )
    assert producer._emitted_tool_calls

    end_frames = producer.flush_pending_tool_calls()

    assert len(end_frames) == 1
    assert end_frames[0].event == "tool_call_result"
    assert end_frames[0].data["tool_call_id"] == "ag_orphan_call"
    assert end_frames[0].data["status"] == "error"
    assert end_frames[0].data["discarded"] is True
    assert end_frames[0].data["discarded_reason"] == "interrupted"
    assert end_frames[0].data["content"] == _DISCARDED_INTERRUPTED_TEXT
    assert producer._emitted_tool_calls == {}


@pytest.mark.asyncio
async def test_executed_write_from_tools_node_after_missing_finish_reason_does_not_emit_discarded():
    # DALTON-HOOK(empty-turn-guard): when Antigravity emits Write without a
    # finishReason and the tools node subsequently runs and emits a ToolMessage,
    # _emitted_tool_calls must drop that call immediately so turn-end never
    # overwrites the successful result with a discarded error.
    producer = _producer()
    frames = await _emit(
        producer,
        AIMessageChunk(
            content="",
            id="ag-exec-1",
            tool_calls=[
                {
                    "name": "Write",
                    "args": {"file_path": "ok.py", "content": "x = 1"},
                    "id": "ag_exec_call",
                    "type": "tool_call",
                }
            ],
        ),
    )
    async for frame in producer._process_message_chunk(
        ToolMessage(content="Wrote ok.py", tool_call_id="ag_exec_call", status="success"),
        "tools:task-2",
        {"langgraph_node": "tools"},
    ):
        frames.append(frame)

    end_frames = producer.flush_pending_tool_calls()
    frames.extend(end_frames)

    results = [f.data for f in frames if f.event == "tool_call_result"]
    assert len(results) == 1
    assert results[0]["tool_call_id"] == "ag_exec_call"
    assert results[0]["status"] == "success"
    assert results[0].get("discarded") is not True
    assert producer._emitted_tool_calls == {}


@pytest.mark.asyncio
async def test_stream_error_flushes_unsettled_emitted_tool_calls_before_error_event():
    # DALTON-HOOK(empty-turn-guard): if a stream emits tool_calls without
    # finishReason and subsequent retries fail before emitting any new chunk,
    # the error path must flush discarded tool_call_result before the error frame.
    producer = _producer()

    class _FailingGraph:
        checkpointer = None

        async def astream(self, *_args, **_kwargs):
            yield (
                (),
                "messages",
                (
                    AIMessageChunk(
                        content="",
                        id="ag-fail-1",
                        tool_calls=[
                            {
                                "name": "Write",
                                "args": {"file_path": "dead.py", "content": "x = 1"},
                                "id": "ag_dead_call",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    {"langgraph_node": "model"},
                ),
            )
            raise RuntimeError("stream died before retry header")

    emitted = []
    with pytest.raises(RuntimeError, match="stream died before retry header"):
        async for frame in producer._stream_frames(_FailingGraph(), None, {"configurable": {"thread_id": "t-1"}}):
            if frame is not None:
                emitted.append(frame)

    events = [f.event for f in emitted]
    assert "tool_calls" in events
    assert "tool_call_result" in events
    assert "error" in events
    assert events.index("tool_call_result") < events.index("error")
    discarded = [f.data for f in emitted if f.event == "tool_call_result"][0]
    assert discarded["tool_call_id"] == "ag_dead_call"
    assert discarded["status"] == "error"
    assert discarded["discarded"] is True
    assert discarded["discarded_reason"] == "interrupted"
    assert discarded["content"] == _DISCARDED_INTERRUPTED_TEXT
    assert producer._emitted_tool_calls == {}
