"""Assistant text hides an opening harness marker on the live stream and on replay.

The checkpoint message is not rewritten. A message that opens with a marker
is hidden when a tool call follows. A final reply drops only that opening
tag or opening paragraph. A tag later in the message stays.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from src.server.services.history.projector import messages_to_history_events
from src.server.services.runs.sse_producer import RunSSEProducer
from src.server.utils.thinking_leak import (
    OPENING_HOLD_LIMIT,
    THINKING_LEAK_MARKERS,
    CardTextFilter,
    ThinkingLeakGate,
    visible_assistant_text,
    visible_card_text,
)

_TAG = "<thinking_mode>interleaved</thinking_mode>"
_THINKING = "<thinking>secret</thinking>"
_NEED = "NEED MORE THINKING: let me plan."
_TOOL = {"name": "web_search", "args": {"query": "x"}, "id": "toolu_1", "type": "tool_call"}


def _with_tool(content: str, message_id: str = "ai-1") -> AIMessage:
    return AIMessage(content=content, id=message_id, tool_calls=[_TOOL])


def _text_events(message: AIMessage) -> list[str]:
    events = messages_to_history_events([message], agent="main")
    return [event.data["content"] for event in events if event.kind == "text"]


@pytest.mark.parametrize(
    "opening",
    [
        _TAG,
        _THINKING,
        _NEED,
        "  <THINKING_MODE>X</THINKING_MODE>",
        "need more thinking: a plan",
    ],
)
def test_transitional_text_that_opens_with_a_marker_is_hidden(opening):
    body = opening + " and then a long plan"
    assert visible_assistant_text(body, followed_by_tool_call=True) == ""
    assert _text_events(_with_tool(body)) == []


def test_normal_transitional_text_is_unchanged():
    body = "I'll look that up."
    assert visible_assistant_text(body, followed_by_tool_call=True) == body
    tagged = f"I'll look that up. {_TAG}"
    assert visible_assistant_text(tagged, followed_by_tool_call=True) == tagged
    assert _text_events(_with_tool(body)) == [body]
    assert _text_events(_with_tool(tagged)) == [tagged]


def test_a_final_reply_drops_only_an_opening_marker():
    body = f"Answer {_TAG} {_THINKING} done."
    assert visible_assistant_text(body, followed_by_tool_call=False) == body
    need = f"{_NEED}\n\nAnswer {_TAG} done."
    assert visible_assistant_text(need, followed_by_tool_call=False) == f"Answer {_TAG} done."
    assert visible_assistant_text(_NEED, followed_by_tool_call=False) == ""
    unclosed = "<thinking_mode>still open and visible"
    assert visible_assistant_text(unclosed, followed_by_tool_call=False) == unclosed
    assert visible_assistant_text(unclosed, followed_by_tool_call=True) == unclosed
    assert _text_events(AIMessage(content=body, id="ai-1")) == [body]
    assert _text_events(AIMessage(content=need, id="ai-1")) == [f"Answer {_TAG} done."]
    assert _text_events(AIMessage(content=_NEED, id="ai-1")) == []


def test_a_reasoning_block_is_not_treated_as_the_leak():
    message = AIMessage(
        content=[
            {"type": "thinking", "thinking": "let me think"},
            {"type": "text", "text": f"Answer {_TAG} done."},
        ],
        id="ai-1",
    )
    events = messages_to_history_events([message], agent="main")
    reasoning = [event.data["content"] for event in events if event.kind == "reasoning"]
    text = [event.data["content"] for event in events if event.kind == "text"]
    assert reasoning == ["let me think"]
    assert text == [f"Answer {_TAG} done."]


def test_the_checkpoint_message_is_not_rewritten():
    raw = f"{_TAG} plan"
    message = _with_tool(raw)
    messages_to_history_events([message], agent="main")
    assert message.content == raw


def test_marker_openings_live_in_one_constant():
    assert THINKING_LEAK_MARKERS[0] == "<thinking_mode>"
    assert "need more thinking" in THINKING_LEAK_MARKERS
    assert "<thinking>" in THINKING_LEAK_MARKERS


def test_a_marker_split_across_chunks_is_hidden_when_a_tool_call_follows():
    gate = ThinkingLeakGate()
    assert gate.feed("m", "<thin") is None
    assert gate.feed("m", "king_mode>interleaved</thinking_mode> plan") is None
    assert gate.settle("m", tool_call=True, already_visible=None) is None
    assert gate.feed("m", "later text") is None


def test_a_marker_split_across_chunks_keeps_the_final_reply():
    gate = ThinkingLeakGate()
    assert gate.feed("m", "<thinking") is None
    assert gate.feed("m", "_mode>secret</thinking_mode>The answer.") is None
    assert gate.settle("m", tool_call=False, already_visible=None) == "The answer."


def test_an_opening_need_paragraph_split_across_chunks_drops_only_that_paragraph():
    gate = ThinkingLeakGate()
    assert gate.feed("m", "NEED MORE ") is None
    assert gate.feed("m", "THINKING: let me plan.\n\nThe answer.") is None
    assert gate.settle("m", tool_call=False, already_visible=None) == "The answer."


def test_messages_do_not_share_a_hold():
    gate = ThinkingLeakGate()
    assert gate.feed("marked", _TAG) is None
    assert gate.feed("plain", "hello") == "hello"


def _producer() -> RunSSEProducer:
    return RunSSEProducer(thread_id="thread-1", run_id="run-1")


async def _frames(
    producer: RunSSEProducer, *chunks: AIMessage | AIMessageChunk | ToolMessage
):
    frames = []
    for chunk in chunks:
        async for frame in producer._process_message_chunk(
            chunk, "agent", {"langgraph_node": "model"}
        ):
            frames.append(frame)
    return frames


def _shown(frames) -> list[str]:
    return [
        frame.data["content"]
        for frame in frames
        if frame.data.get("content_type") == "text" and frame.data.get("content")
    ]


def _tool_use(message_id: str) -> AIMessageChunk:
    return AIMessageChunk(
        content=[
            {
                "type": "tool_use",
                "id": "toolu_a",
                "name": "Bash",
                "input": {},
                "index": 0,
            }
        ],
        id=message_id,
    )


def _delta(message_id: str) -> AIMessageChunk:
    return AIMessageChunk(
        content=[{"type": "input_json_delta", "partial_json": '{"command":"ls"}', "index": 0}],
        id=message_id,
    )


def _finish_tools(message_id: str) -> AIMessageChunk:
    return AIMessageChunk(
        content="", id=message_id, response_metadata={"stop_reason": "tool_use"}
    )


@pytest.mark.asyncio
async def test_the_live_stream_hides_a_marker_split_across_chunks_before_a_tool_call():
    producer = _producer()
    frames = await _frames(
        producer,
        AIMessageChunk(content="<thin", id="m1"),
        AIMessageChunk(content="king_mode>interleaved</thinking_mode>", id="m1"),
        _tool_use("m1"),
        _delta("m1"),
        _finish_tools("m1"),
    )
    assert _shown(frames) == []
    assert any(frame.event == "tool_calls" for frame in frames)
    assert all(_TAG not in (frame.data.get("content") or "") for frame in frames)


@pytest.mark.asyncio
async def test_the_live_stream_keeps_normal_text_that_a_tool_call_follows():
    producer = _producer()
    frames = await _frames(
        producer,
        AIMessageChunk(content="I'll look that up.", id="m1"),
        _tool_use("m1"),
        _delta("m1"),
        _finish_tools("m1"),
    )
    assert _shown(frames) == ["I'll look that up."]


@pytest.mark.asyncio
async def test_the_live_stream_strips_a_tag_from_a_final_reply():
    producer = _producer()
    frames = await _frames(
        producer,
        AIMessageChunk(content="<thinking", id="m1"),
        AIMessageChunk(content="_mode>secret</thinking_mode>The answer.", id="m1"),
        AIMessageChunk(content="", id="m1", response_metadata={"stop_reason": "end_turn"}),
    )
    assert _shown(frames) == ["The answer."]


@pytest.mark.asyncio
async def test_a_one_shot_final_reply_drops_only_the_opening_marker():
    producer = _producer()
    frames = await _frames(
        producer,
        AIMessage(
            content=f"{_TAG}The answer. {_THINKING}",
            id="m1",
            response_metadata={"stop_reason": "end_turn"},
        ),
    )
    assert _shown(frames) == [f"The answer. {_THINKING}"]


@pytest.mark.asyncio
async def test_a_one_shot_opening_need_paragraph_drops_only_that_paragraph():
    producer = _producer()
    frames = await _frames(
        producer,
        AIMessage(
            content=f"{_NEED}\n\nThe answer.",
            id="m1",
            response_metadata={"stop_reason": "end_turn"},
        ),
    )
    assert _shown(frames) == ["The answer."]


@pytest.mark.asyncio
async def test_the_final_message_does_not_repeat_text_the_chunks_already_held():
    producer = _producer()
    full = f"{_TAG}The answer."
    frames = await _frames(
        producer,
        AIMessageChunk(content=f"{_TAG}The ", id="m1"),
        AIMessageChunk(content="answer.", id="m1"),
        AIMessage(content=full, id="m1", response_metadata={"stop_reason": "end_turn"}),
    )
    assert _shown(frames) == ["The answer."]


@pytest.mark.asyncio
async def test_a_compaction_chunk_is_not_filtered():
    producer = _producer()
    chunk = AIMessageChunk(content=_TAG, id="c1")
    frames = []
    async for frame in producer._process_message_chunk(
        chunk, "agent", {"langgraph_node": "model"}, is_compaction=True
    ):
        frames.append(frame)
    assert any(_TAG in (frame.data.get("content") or "") for frame in frames)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ["<thinking>secret</thinking>", "NEED MORE THINKING: let me plan."],
)
async def test_a_tool_result_that_opens_with_a_marker_is_shown(content):
    producer = _producer()
    frames = await _frames(
        producer,
        ToolMessage(content=content, tool_call_id="toolu_1", id="t1"),
    )
    assert _shown(frames) == [content]


@pytest.mark.asyncio
async def test_a_mid_body_tag_is_kept_on_the_live_stream_and_on_replay():
    body = "Answer <thinking>secret</thinking>"
    producer = _producer()
    frames = await _frames(
        producer,
        AIMessageChunk(content="Answer ", id="m1"),
        AIMessageChunk(content="<thinking>secret</thinking>", id="m1"),
        AIMessageChunk(content="", id="m1", response_metadata={"stop_reason": "end_turn"}),
    )
    live = "".join(_shown(frames))
    replay = "".join(_text_events(AIMessage(content=body, id="m1")))
    assert live == replay == body


@pytest.mark.asyncio
async def test_a_need_phrase_after_ordinary_text_matches_one_shot():
    parts = ("We ", "NEED MORE THINKING about this.")
    body = "".join(parts)
    producer = _producer()
    frames = await _frames(
        producer,
        AIMessageChunk(content=parts[0], id="m1"),
        AIMessageChunk(content=parts[1], id="m1"),
        AIMessageChunk(content="", id="m1", response_metadata={"stop_reason": "end_turn"}),
    )
    live = "".join(_shown(frames))
    replay = "".join(_text_events(AIMessage(content=body, id="m1")))
    assert live == replay == body
    assert visible_assistant_text(body, followed_by_tool_call=False) == body


@pytest.mark.asyncio
async def test_a_later_message_releases_text_held_without_a_finish():
    producer = _producer()
    frames = await _frames(
        producer,
        AIMessageChunk(content=f"{_TAG}The answer.", id="m1"),
        AIMessageChunk(content="Next.", id="m2"),
    )
    assert _shown(frames) == ["The answer.", "Next."]


@pytest.mark.asyncio
async def test_a_complete_message_without_finish_metadata_releases_held_text():
    producer = _producer()
    frames = await _frames(
        producer,
        AIMessage(content=f"{_TAG}The answer.", id="m1"),
    )
    assert _shown(frames) == ["The answer."]


@pytest.mark.asyncio
async def test_flush_open_thinking_releases_a_held_body():
    producer = _producer()
    frames = await _frames(
        producer,
        AIMessageChunk(content=f"{_TAG}The answer.", id="m1"),
    )
    assert _shown(frames) == []
    flushed = producer.flush_open_thinking()
    assert [frame.data["content"] for frame in flushed] == ["The answer."]


def test_card_text_keeps_a_mid_body_tag_and_a_later_need_phrase():
    body = (
        "Hello <thinking_mode>interleaved</thinking_mode>\n\n"
        "NEED MORE THINKING: plan.\n\n"
        "Done"
    )
    assert visible_card_text(body) == body
    assert visible_card_text("I'll look that up.") == "I'll look that up."


def test_card_text_strips_only_an_opening_marker():
    assert visible_card_text(f"{_TAG}The answer. {_THINKING}") == f"The answer. {_THINKING}"
    assert visible_card_text(f"{_NEED}\n\nDone") == "Done"
    assert visible_card_text(_NEED) == ""


def test_card_chunks_keep_a_need_phrase_that_does_not_open_the_message():
    parts = ("We ", "NEED MORE THINKING about this.")
    body = "".join(parts)
    card = CardTextFilter()
    assert card.feed(parts[0]) == parts[0]
    assert card.feed(parts[1]) == parts[1]
    assert card.flush() is None
    assert visible_card_text(body) == body


def test_card_chunks_strip_an_opening_marker_the_same_way():
    body = f"{_TAG}\n\n{_NEED}\n\nDone"
    card = CardTextFilter()
    assert card.feed("<thinking_mode>inter") is None
    assert card.feed("leaved</thinking_mode>\n\n") == "\n\n"
    assert card.feed(f"{_NEED}\n\nDone") == f"{_NEED}\n\nDone"
    assert card.flush() is None
    assert visible_card_text(body) == f"\n\n{_NEED}\n\nDone"

    need = CardTextFilter()
    assert need.feed("NEED MORE ") is None
    assert need.feed("THINKING: let me plan.\n\nDone") == "Done"
    assert need.flush() is None


def test_an_unclosed_opening_past_the_hold_limit_is_shown():
    head = "<thinking>" + ("x" * (OPENING_HOLD_LIMIT - len("<thinking>")))
    assert len(head) == OPENING_HOLD_LIMIT
    body = head + "x</thinking>kept"
    assert visible_assistant_text(body, followed_by_tool_call=False) == body
    assert visible_assistant_text(body, followed_by_tool_call=True) == body
    assert visible_card_text(body) == body

    gate = ThinkingLeakGate()
    assert gate.feed("m", head) is None
    assert gate.feed("m", "x</thinking>kept") == body
    assert gate.feed("m", " more") == " more"

    card = CardTextFilter()
    assert card.feed(head) is None
    assert card.feed("x</thinking>kept") == body
    assert card.feed(" more") == " more"


def test_a_card_keeps_text_after_a_blank_line_that_ends_the_chunk():
    body = "NEED MORE THINKING: plan.\n\nDone"
    card = CardTextFilter()
    assert card.feed("NEED MORE THINKING: plan.\n\n") is None
    assert card.feed("Done") == "Done"
    assert card.flush() is None
    assert visible_card_text(body) == "Done"


@pytest.mark.asyncio
async def test_a_crlf_blank_line_ends_the_opening_paragraph_everywhere():
    body = "NEED MORE THINKING: plan.\r\n\r\nThe answer."
    shown = "The answer."
    assert visible_assistant_text(body, followed_by_tool_call=False) == shown
    assert visible_card_text(body) == shown
    assert "".join(_text_events(AIMessage(content=body, id="m1"))) == shown

    producer = _producer()
    frames = await _frames(
        producer,
        AIMessageChunk(content="NEED MORE THINKING: plan.\r\n", id="m1"),
        AIMessageChunk(content="\r\nThe answer.", id="m1"),
        AIMessageChunk(content="", id="m1", response_metadata={"stop_reason": "end_turn"}),
    )
    assert "".join(_shown(frames)) == shown

    card = CardTextFilter()
    assert card.feed("NEED MORE THINKING: plan.\r\n") is None
    assert card.feed("\r\nThe answer.") == shown
    assert card.flush() is None


def test_a_tag_that_closes_inside_the_hold_window_is_still_a_marker():
    opener = "<thinking>"
    closer = "</thinking>"
    room = OPENING_HOLD_LIMIT - len(opener) - len(closer)
    body = f"{opener}{'q' * room}{closer}Z"
    assert len(opener) + room + len(closer) == OPENING_HOLD_LIMIT
    assert visible_assistant_text(body, followed_by_tool_call=False) == "Z"
    assert visible_assistant_text(body, followed_by_tool_call=True) == ""
    assert visible_card_text(body) == "Z"
