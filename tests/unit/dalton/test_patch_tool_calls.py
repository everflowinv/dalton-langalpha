"""Dangling tool calls are reported as possibly run, not as cancelled."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from src.dalton.middleware.patch_tool_calls import DaltonPatchToolCallsMiddleware


def _call(name: str, call_id: str) -> dict:
    return {"name": name, "args": {}, "id": call_id, "type": "tool_call"}


def _run(messages):
    return DaltonPatchToolCallsMiddleware().before_agent({"messages": messages}, None)


def test_dangling_call_says_result_lost_and_may_have_run():
    ai = AIMessage(content="", tool_calls=[_call("check_draft", "c1"), _call("Edit", "c2")])
    done = ToolMessage(content="real result", name="Edit", tool_call_id="c2")
    update = _run([HumanMessage(content="go"), ai, done, HumanMessage(content="again")])

    msgs = update["messages"]
    assert isinstance(msgs[0], RemoveMessage)
    patched = [m for m in msgs if isinstance(m, ToolMessage) and m.tool_call_id == "c1"]
    assert len(patched) == 1
    text = patched[0].content
    assert "cancelled" not in text
    assert "check_draft" in text and "c1" in text
    assert "may have already run" in text
    assert "TaskOutput" in text
    assert "do not assume it did nothing" in text
    # The patch sits right after the AI message that issued the call.
    assert msgs.index(patched[0]) == msgs.index(ai) + 1


def test_answered_calls_and_other_messages_untouched():
    ai = AIMessage(content="", tool_calls=[_call("Edit", "c2"), _call("Task", "c3")])
    done = ToolMessage(content="real result", name="Edit", tool_call_id="c2")
    original = [HumanMessage(content="go"), ai, done]
    msgs = _run(original)["messages"][1:]

    assert msgs[0] is original[0]
    assert msgs[1] is ai
    answered = [m for m in msgs if isinstance(m, ToolMessage) and m.tool_call_id == "c2"]
    assert answered == [done] and answered[0] is done
    assert answered[0].content == "real result"


def test_no_dangling_calls_returns_none():
    ai = AIMessage(content="", tool_calls=[_call("Edit", "c2")])
    done = ToolMessage(content="ok", name="Edit", tool_call_id="c2")
    assert _run([HumanMessage(content="go"), ai, done]) is None


def test_invalid_tool_call_keeps_upstream_malformed_text():
    ai = AIMessage(
        content="",
        invalid_tool_calls=[
            {"name": "Edit", "args": "{bad", "id": "c9", "error": "x", "type": "invalid_tool_call"}
        ],
    )
    msgs = _run([HumanMessage(content="go"), ai])["messages"]
    patched = [m for m in msgs if isinstance(m, ToolMessage)]
    assert len(patched) == 1
    assert "malformed or truncated" in patched[0].content


def test_node_name_matches_upstream():
    assert DaltonPatchToolCallsMiddleware().name == "PatchToolCallsMiddleware"
