"""Thought signatures reach Gemini on the call they belong to, or as its stand-in.

Gemini validates every functionCall of the current turn: a Claude call (which
carries none), a text part's signature replayed onto a call, or a signature
from a concurrent turn on the same cached client all failed with 400
"missing a thought_signature" / "Corrupted thought signature".
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.dalton.llm.providers.antigravity.client import (
    _CALL_SIGNATURES_KEY,
    _SKIP_SIGNATURE,
    _THOUGHT_SIGNATURES_KEY,
    ChatAntigravity,
    _messages_to_gemini,
)


def _frame(parts, finish=True):
    cand = {"content": {"parts": parts}}
    if finish:
        cand["finishReason"] = "STOP"
    return {"response": {"candidates": [cand]}}


def _calls(contents):
    return [p for c in contents for p in c["parts"] if "functionCall" in p]


def _flat_parts(contents):
    return [p for c in contents for p in c["parts"]]


def _history(extra):
    return [
        HumanMessage(content="go"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "a", "args": {}, "id": "c1"},
                {"name": "b", "args": {}, "id": "c2"},
            ],
            additional_kwargs=extra,
        ),
        ToolMessage(content="1", tool_call_id="c1"),
        ToolMessage(content="2", tool_call_id="c2"),
    ]


def test_signatures_are_captured_per_call():
    client = ChatAntigravity(model="gemini-4-argon-high", api_key="fake")
    chunks = client._chunks_from_frame(
        _frame(
            [
                {"text": "plan", "thoughtSignature": "sig-text"},
                {"functionCall": {"name": "a", "args": {}, "id": "c1"}, "thoughtSignature": "sig-c1"},
                {"functionCall": {"name": "b", "args": {}, "id": "c2"}},
            ]
        )
    )
    msg = client._final_message(chunks)
    assert msg.additional_kwargs[_THOUGHT_SIGNATURES_KEY] == ["sig-text", "sig-c1"]
    assert msg.additional_kwargs[_CALL_SIGNATURES_KEY] == {"c1": "sig-c1"}


def test_gemini_gets_each_calls_own_signature_or_the_stand_in():
    _, contents = _messages_to_gemini(
        _history({_THOUGHT_SIGNATURES_KEY: ["sig-text", "sig-c1"], _CALL_SIGNATURES_KEY: {"c1": "sig-c1"}}),
        gemini_target=True,
    )
    calls = _calls(contents)
    assert calls[0]["thoughtSignature"] == "sig-c1"
    assert calls[1]["thoughtSignature"] == _SKIP_SIGNATURE


def test_a_call_nothing_signed_gets_the_stand_in_on_gemini():
    # A Claude call, made earlier in the turn the fallback moved to Gemini.
    _, contents = _messages_to_gemini(_history({}), gemini_target=True)
    assert [c["thoughtSignature"] for c in _calls(contents)] == [_SKIP_SIGNATURE, _SKIP_SIGNATURE]


def test_an_unmapped_list_is_not_matched_to_calls_on_gemini():
    _, contents = _messages_to_gemini(
        _history({_THOUGHT_SIGNATURES_KEY: ["sig-text", "sig-c1"]}), gemini_target=True
    )
    parts = [p for c in contents for p in c["parts"]]
    assert [c["thoughtSignature"] for c in _calls(contents)] == [_SKIP_SIGNATURE, _SKIP_SIGNATURE]
    assert not any(set(p) == {"thoughtSignature"} for p in parts)


def test_claude_keeps_the_previous_replay():
    _, contents = _messages_to_gemini(_history({_THOUGHT_SIGNATURES_KEY: ["s1"]}), gemini_target=False)
    calls = _calls(contents)
    assert calls[0]["thoughtSignature"] == "s1"
    assert "thoughtSignature" not in calls[1]


def test_concurrent_streams_on_one_client_keep_their_own_signatures():
    client = ChatAntigravity(model="gemini-4-argon-high", api_key="fake")
    turn_a = [
        _frame([{"functionCall": {"name": "a", "args": {}, "id": "ca"}, "thoughtSignature": "sig-a"}], finish=False),
        _frame([{"text": "done-a"}]),
    ]
    turn_b = [
        _frame([{"functionCall": {"name": "b", "args": {}, "id": "cb"}, "thoughtSignature": "sig-b"}], finish=False),
        _frame([{"text": "done-b"}]),
    ]
    frames = {"a": iter(turn_a), "b": iter(turn_b)}
    client._stream_raw = lambda payload: frames[payload["_turn"]]  # type: ignore[method-assign]
    original = client._build_payload
    stream_a = client._stream([HumanMessage(content="a")])
    stream_b = client._stream([HumanMessage(content="b")])
    client._build_payload = lambda messages, **_: {"_turn": messages[0].content}  # type: ignore[method-assign]
    # Interleave the two turns frame by frame.
    out_a, out_b = [next(stream_a)], [next(stream_b)]
    out_a += list(stream_a)
    out_b += list(stream_b)
    client._build_payload = original  # type: ignore[method-assign]
    msg_a = client._final_message([g.message for g in out_a])
    msg_b = client._final_message([g.message for g in out_b])
    assert msg_a.additional_kwargs[_CALL_SIGNATURES_KEY] == {"ca": "sig-a"}
    assert msg_b.additional_kwargs[_CALL_SIGNATURES_KEY] == {"cb": "sig-b"}


def test_an_anthropic_message_sends_each_call_once_signed():
    # The Anthropic SDK names a call as a tool_use block and in tool_calls.
    msg = AIMessage(
        content=[{"type": "text", "text": "x"}, {"type": "tool_use", "id": "c1", "name": "a", "input": {}}],
        tool_calls=[{"name": "a", "args": {}, "id": "c1"}],
    )
    _, contents = _messages_to_gemini([HumanMessage(content="go"), msg], gemini_target=True)
    calls = _calls(contents)
    assert len(calls) == 1
    assert calls[0]["thoughtSignature"] == _SKIP_SIGNATURE


def test_a_call_only_in_content_is_signed_too():
    msg = AIMessage(content=[{"type": "tool_use", "id": "c9", "name": "a", "input": {}}])
    _, contents = _messages_to_gemini([HumanMessage(content="go"), msg], gemini_target=True)
    assert [c["thoughtSignature"] for c in _calls(contents)] == [_SKIP_SIGNATURE]


def test_gemini_puts_the_first_unmapped_signature_on_the_first_unsigned_call():
    # The signature rode a text part; the calls themselves carry none. The
    # pre-mapping behavior (list[0] → first call) measured working on Gemini
    # Flash, so the first call takes it and the rest keep the stand-in.
    _, contents = _messages_to_gemini(
        _history({_THOUGHT_SIGNATURES_KEY: ["sig-text"], _CALL_SIGNATURES_KEY: {}}),
        gemini_target=True,
    )
    calls = _calls(contents)
    assert calls[0]["thoughtSignature"] == "sig-text"
    assert calls[1]["thoughtSignature"] == _SKIP_SIGNATURE
    assert not any(set(p) == {"thoughtSignature"} for p in _flat_parts(contents))


def test_gemini_first_unsigned_call_gets_the_text_signature_a_later_call_keeps_its_own():
    _, contents = _messages_to_gemini(
        _history({
            _THOUGHT_SIGNATURES_KEY: ["sig-text", "sig-c2"],
            _CALL_SIGNATURES_KEY: {"c2": "sig-c2"},
        }),
        gemini_target=True,
    )
    calls = _calls(contents)
    assert calls[0]["thoughtSignature"] == "sig-text"
    assert calls[1]["thoughtSignature"] == "sig-c2"
    assert not any(set(p) == {"thoughtSignature"} for p in _flat_parts(contents))


def test_gemini_keeps_the_stand_in_when_the_first_call_is_mapped():
    # First call signed by the map: the unmapped signature stays off the calls
    # (a replayed text signature on the second call is what Gemini refuses).
    _, contents = _messages_to_gemini(
        _history({_THOUGHT_SIGNATURES_KEY: ["sig-text"], _CALL_SIGNATURES_KEY: {"c1": "sig-c1"}}),
        gemini_target=True,
    )
    calls = _calls(contents)
    assert calls[0]["thoughtSignature"] == "sig-c1"
    assert calls[1]["thoughtSignature"] == _SKIP_SIGNATURE
    assert any(set(p) == {"thoughtSignature"} for p in _flat_parts(contents))


def test_a_non_gemini_target_never_takes_the_first_call_rule():
    _, contents = _messages_to_gemini(
        _history({_THOUGHT_SIGNATURES_KEY: ["sig-text"], _CALL_SIGNATURES_KEY: {}}),
        gemini_target=False,
    )
    assert all("thoughtSignature" not in c for c in _calls(contents))
    assert any(set(p) == {"thoughtSignature"} for p in _flat_parts(contents))


def test_an_integer_call_id_is_captured_by_its_string_id():
    client = ChatAntigravity(model="gemini-4-argon-high", api_key="fake")
    chunks = client._chunks_from_frame(
        _frame([{"functionCall": {"name": "a", "args": {}, "id": 12345}, "thoughtSignature": "sig-int"}])
    )
    msg = client._final_message(chunks)
    assert msg.additional_kwargs[_CALL_SIGNATURES_KEY] == {"12345": "sig-int"}

    # And the replay finds it: capture, tool_calls, and lookup all spell the
    # id the same way now.
    _, contents = _messages_to_gemini(
        [
            HumanMessage(content="go"),
            AIMessage(
                content="",
                tool_calls=[{"name": "a", "args": {}, "id": "12345"}],
                additional_kwargs=msg.additional_kwargs,
            ),
            ToolMessage(content="1", tool_call_id="12345"),
        ],
        gemini_target=True,
    )
    assert _calls(contents)[0]["thoughtSignature"] == "sig-int"
