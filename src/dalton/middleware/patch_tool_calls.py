"""Dangling tool-call repair that does not claim the call was cancelled.

deepagents' ``PatchToolCallsMiddleware`` answers every tool call left without
a result with "was cancelled". A call is left dangling mostly when the process
dies mid-turn (a backend restart), and by then the tool has often already run:
on 2026-10-23 a turn's Edit, Task and check_draft calls had completed, the
model read "cancelled" and ran check_draft three more times. This subclass
keeps the upstream repair and only rewrites the text of the answers it adds
for well-formed calls, so the model checks the state before repeating them.
"""

from __future__ import annotations

from typing import Any

from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain.agents.middleware import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime

LOST_RESULT_TEMPLATE = (
    "Tool call {name} with id {tool_call_id} has no recorded result: the run was "
    "interrupted (for example by a server restart) before its result was saved. "
    "The call may have already run and taken effect. Before calling it again, "
    "check the current state (files, outputs, or a task's status via TaskOutput); "
    "do not assume it did nothing."
)


class DaltonPatchToolCallsMiddleware(PatchToolCallsMiddleware):
    """``PatchToolCallsMiddleware`` with a lost-result message for dangling calls."""

    @property
    def name(self) -> str:
        # The graph node is named after the middleware; keeping the upstream name
        # keeps checkpoints written before this swap resumable.
        return "PatchToolCallsMiddleware"

    def before_agent(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        update = super().before_agent(state, runtime)
        if not update:
            return update

        original = state["messages"]
        answered = {m.tool_call_id for m in original if m.type == "tool"}
        dangling = {
            call["id"]
            for m in original
            if isinstance(m, AIMessage)
            for call in m.tool_calls
            if call.get("id") is not None and call["id"] not in answered
        }
        if not dangling:
            return update

        patched: list[Any] = []
        for msg in update["messages"]:
            if isinstance(msg, ToolMessage) and msg.tool_call_id in dangling:
                msg = msg.model_copy(
                    update={
                        "content": LOST_RESULT_TEMPLATE.format(
                            name=msg.name or "unknown", tool_call_id=msg.tool_call_id
                        )
                    }
                )
            patched.append(msg)
        return {**update, "messages": patched}
