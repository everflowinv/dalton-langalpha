"""A workflow the agent already started survives the mid-turn rebuild of a
stale prompt (#660, upstream 70c51bf9).

Dalton runs the scratchpad off, so a mid-turn summary runs on the call that
crossed the threshold, keeps no turn, and hands the next call the step under
way as a row. When that step is the one that launched a workflow, the row has
to carry the launch's own identity — the tool call id the registry keys the
run by, the arguments, and the task id the reply named — so the conversation
the model continues from names the same run. The run itself is a detached
task writer the summary never touches, and a re-execution of the same launch
call (a checkpoint replay of the turn the rebuild rewrote) reconnects to that
run instead of starting a second one.

Scope: what these tests verify is the launch identity surviving the rebuild
(the first test drives the real compaction middleware through a real
mid-turn rebuild) and the same-call-id launch dedup — one WorkflowRunSpec,
one registry row, no dispatch, on the reconnect. What they do not verify:
the journal, subagent report-backs and the final publish executing exactly
once in a real run — the second test's harness replaces WorkflowDriver with
a capturing mock and spawn_task_writer with a no-op, so zero of those
actually execute here; that once-only guarantee rests on the registry's
TaskWriterLive idempotency plus the journal replay suite
(tests/unit/dalton/test_workflow_journal_resume.py) and on the lead's
outside-sandbox integration checks, not on this file.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from ptc_agent.agent.main_state import MainAgentState
from ptc_agent.agent.middleware.compaction.compact import Summarizer
from ptc_agent.agent.middleware.compaction.middleware import CompactionMiddleware
from ptc_agent.agent.middleware.compaction import middleware as mw_module
from ptc_agent.agent.middleware.compaction.rebuild import TurnStartRebuilds
from ptc_agent.agent.middleware.runtime_context import tail as tail_module
from ptc_agent.agent.middleware.runtime_context.carrier import Carrier
from ptc_agent.agent.middleware.runtime_context.frozen_prompt import SystemPinMiddleware
from ptc_agent.agent.middleware.runtime_context.tail import RowCarrierMiddleware
from ptc_agent.agent.middleware.reasoning_compat import ReasoningCompatibilityMiddleware
from ptc_agent.agent.middleware.tool_surface import ToolSurfaceMiddleware
from ptc_agent.agent.prompts import release
from tests.unit.ptc_agent.agent.prefix_stability import (
    Wire,
    anthropic_model,
    api_drops,
    is_compaction_request,
    reply,
)

THRESHOLD = 10_000
THREAD = "t-midturn-wf"
CFG = {"configurable": {"thread_id": THREAD, "user_id": "u-1"}}
CARRY = "Your last step"
TASK_ID = "wf-k3x9"
LAUNCH_ARGS = {"workflow": "dalton-topic-research", "params": {"ticker": "META"}}
STARTED = (
    f"Workflow run started: **{TASK_ID}** (dalton-topic-research). "
    "You will be notified when it finishes."
)


@tool("get_data")
def get_data(query: str) -> str:
    """Look something up (stub)."""
    return f"data for {query}"


@tool("RunWorkflow")
def run_workflow(workflow: str, params: Any = None, description: str = None, resume: str = None) -> str:
    """Run a workflow script server-side to orchestrate subagents (stub)."""
    return STARTED


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(mw_module, "get_stream_writer", lambda: lambda payload: None)
    # Rows ride a user-role reminder on the routes Dalton deploys.
    monkeypatch.setattr(tail_module, "resolve_carrier", lambda model: Carrier("reminder"))
    # cl100k_base fetches its BPE file on first use; the blocked sockets of
    # the unit suite refuse that on a cold cache.
    from types import SimpleNamespace

    from ptc_agent.agent.middleware.compaction import utils as compaction_utils

    fake = SimpleNamespace(encode=lambda text: [0] * (len(text) // 4 + 1))
    monkeypatch.setattr(compaction_utils, "_get_tiktoken_encoder", lambda: fake)


def _agent(saver: InMemorySaver, system: str):
    return create_agent(
        model=anthropic_model(),
        tools=[get_data, run_workflow],
        system_prompt=system,
        middleware=[
            CompactionMiddleware(
                Summarizer(GenericFakeChatModel(messages=iter([])), limit=100_000),
                token_threshold=THRESHOLD,
                keep_tokens=THRESHOLD // 3,
                claude_keep_tokens=THRESHOLD // 3,
            ).with_turn_start_rebuilds(TurnStartRebuilds(0)),
            AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
            ToolSurfaceMiddleware(),
            RowCarrierMiddleware(),
            SystemPinMiddleware(),
            ReasoningCompatibilityMiddleware(),
        ],
        checkpointer=saver,
        state_schema=MainAgentState,
    )


def _measured(message: AIMessage, input_tokens: int) -> AIMessage:
    message.usage_metadata = {"input_tokens": input_tokens, "output_tokens": 50, "total_tokens": input_tokens + 50}
    return message


def _compaction_reply() -> AIMessage:
    return AIMessage(
        content=[{"type": "compaction", "content": "The user asked for a topic run on META.", "signature": "cmp-1"}],
        id="msg_cmp",
        response_metadata={"model_name": "claude-opus-5-5", "stop_reason": "compaction"},
        usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    )


def _messages(payload: dict) -> str:
    return json.dumps(payload["messages"])


def _last_results(payload: dict) -> list[str]:
    content = payload["messages"][-1]["content"]
    return [block["tool_use_id"] for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]


@pytest.mark.asyncio
async def test_a_mid_turn_rebuild_carries_the_started_workflow_by_its_call_and_task_id(
    monkeypatch, caplog
):
    """The summary the rebuild writes keeps none of the turn, so the launch
    step must reach the next call whole: the carried row names the same tool
    call id the registry keys the run by, the arguments as sent, and the task
    id the reply gave — the one run, not a paraphrase of it."""
    caplog.set_level("INFO")
    # The turn before the deploy, then the launch call that crosses the
    # threshold with its result.
    opening = [
        _measured(reply(1, tool=("get_data", {"query": "a"})), 2_000),
        _measured(reply(2, text="Starting the run."), 2_100),
        _measured(reply(3, tool=("RunWorkflow", LAUNCH_ARGS)), 9_995),
    ]
    wire = Wire([*opening, _compaction_reply(), _measured(reply(5, text="The run is under way."), 1_500)]).install(
        monkeypatch
    )
    saver = InMemorySaver()
    await _agent(saver, "Prompt A.").ainvoke({"messages": [HumanMessage("Cover META.")]}, CFG)
    monkeypatch.setattr(release, "source_digest", lambda: "the next deploy")
    await _agent(saver, "Prompt B.").ainvoke({"messages": [HumanMessage("Go on.")]}, CFG)

    summary, after = wire.payloads[3], wire.payloads[4]
    assert is_compaction_request(summary) and _last_results(summary) == ["toolu_03"]
    assert "rebuild=yes reason=mid_turn" in caplog.text

    # The next call runs on the new prompt, and the carried row is the only
    # place the launch appears: once, with call id, arguments and task id.
    assert "Prompt B." in json.dumps([block["text"] for block in after["system"]])
    carried = _messages(after)
    assert carried.count(TASK_ID) == 1
    assert '<tool_call tool=\\"RunWorkflow\\" id=\\"toolu_03\\">' in carried
    # The arguments as they were sent, escaped the way the row rides JSON.
    assert json.dumps(json.dumps(LAUNCH_ARGS))[1:-1] in carried
    assert CARRY in carried
    assert api_drops(wire) == {}

    # The checkpoint the thread now holds no longer offers the launch pair to
    # any later call: the summary event the state keeps makes every later
    # view start at the compaction block, and the row the model reads is the
    # only place the launch appears. A later turn proves it: the pair the
    # original AIMessage carried (`tool_use` block toolu_03) never re-enters
    # a request, and the task id appears once.
    wire.push(reply(6, text="Still waiting on it."))
    await _agent(saver, "Prompt B.").ainvoke({"messages": [HumanMessage("And now?")]}, CFG)
    later = _messages(wire.payloads[-1])
    assert later.count(TASK_ID) == 1
    assert '"type": "tool_use", "id": "toolu_03"' not in later
    state = (await _agent(saver, "Prompt B.").aget_state(CFG)).values
    event = state["_summarization_event"]
    assert event is not None and "topic run on META" in str(event["summary_message"].content)


# -- a re-execution of the same launch call reconnects to the one run ------------


@pytest.mark.asyncio
async def test_a_replayed_launch_call_reconnects_to_the_run_it_already_started(monkeypatch):
    """The turn the rebuild rewrote can replay its checkpoint: the same
    RunWorkflow call, with the same tool call id, must answer with the run it
    started — exactly one WorkflowRunSpec ever built (a second journal,
    dispatch or publish chain would each need a second driver run), one
    registry row, no subtask dispatched. The driver itself is a capturing
    mock here: this pins the launch dedup, not the once-only execution of
    what the one run would do."""
    from ptc_agent.agent.middleware.background_subagent.workflow.tool import (
        create_run_workflow_tool,
    )
    from tests.unit.dalton.test_workflow_journal_resume import (
        CapturingDriver,
        _tool_environment,
        make_fs_backend,
        make_ledger,
        make_prebuilt,
        make_tool_dispatcher,
    )
    from tests.unit.middleware.workflow.conftest import workflow_script

    _tool_environment(monkeypatch)
    script = workflow_script("return 1;", name="journal-test", description="journal test")
    backend = make_fs_backend()
    dispatcher = make_tool_dispatcher()
    dispatcher.registry.run_ledger = make_ledger([])

    tool = create_run_workflow_tool(
        dispatcher=dispatcher,
        backend=backend,
        thread_id="thread-1",
        short_thread_id="short-1",
        store=None,
        user_id="user-1",
        prebuilt_workflows=make_prebuilt({"journal-test": script}),
    )

    first = await tool.coroutine(workflow="journal-test", params={}, tool_call_id="call-launch-1")
    first_text = first.content if isinstance(first, ToolMessage) else first
    assert "started" in str(first_text)
    started = CapturingDriver.specs[-1].run_task

    # The checkpoint re-execution: the same call id, after the mid-turn
    # rebuild dropped the original pair from the thread's messages.
    again = await tool.coroutine(workflow="journal-test", params={}, tool_call_id="call-launch-1")
    again_text = str(again.content if isinstance(again, ToolMessage) else again)
    assert "already active" in again_text
    assert started.display_id in again_text
    assert len(CapturingDriver.specs) == 1, "the replay must not build a second run"
    assert len(dispatcher.registry._tasks) == 1
    assert dispatcher.dispatch.await_count == 0, "no subtask is dispatched by a reconnect"
