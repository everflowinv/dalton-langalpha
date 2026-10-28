"""The transport_lost abort keeps its retention contract but stops throwing
the run away: why the spill failed (exception type and message) is recorded
where the circuit opens and at the kill site, and the failure the parent
sees carries the last text the run wrote plus its transcript directory
(DALTON-HOOK(subagent-spill-recovery))."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from structlog.testing import capture_logs

from ptc_agent.agent.middleware.background_subagent import redis_stream
from ptc_agent.agent.middleware.background_subagent.middleware import (
    BackgroundSubagentMiddleware,
)
from ptc_agent.agent.middleware.background_subagent.registry import (
    BackgroundTaskRegistry,
    TransportLostError,
)
from ptc_agent.agent.middleware.background_subagent.subagent import (
    _last_written_text,
    arun_subagent_streaming,
)
from ptc_agent.agent.middleware.background_subagent.task import BackgroundTask

# A thread id whose first 8 chars name the transcript directory the failure
# must point the parent at.
THREAD_ID = "01234567-89ab-cdef-0123-456789abcdef"
TRANSCRIPT_DIR = ".agents/transcripts/01234567/tasks/"


class FakeOwner:
    async def acquire_task_ns(self, task_id: str) -> bool:
        return True

    async def release_task_ns(self, task_id: str) -> None:
        return None


class FakeLedger:
    def __init__(self) -> None:
        self.finalized: list[tuple] = []

    async def start_task_run(self, **kwargs) -> str:
        return "run-uuid-1"

    async def finalize_task_run(self, task_run_id, status, *, task_id=None, failure=None, **_):
        self.finalized.append((task_run_id, status, failure))
        return {"applied": True, "run": {"task_run_id": task_run_id, "status": status}}

    async def append_run_end(self, task_run_id, *, task_id, outcome, finalized_at=None):
        return None


class _Request(SimpleNamespace):
    pass


def _request(args: dict, tool_call_id: str = "tc-1") -> _Request:
    return _Request(
        tool_call={"name": "Task", "id": tool_call_id, "args": args},
        runtime=SimpleNamespace(
            config={
                "configurable": {"thread_id": "thread-x"},
                "metadata": {"run_id": "parent-run-1"},
            }
        ),
    )


def _middleware(owner, ledger) -> BackgroundSubagentMiddleware:
    registry = BackgroundTaskRegistry(thread_id=THREAD_ID)
    registry.run_ledger = ledger
    return BackgroundSubagentMiddleware(
        registry=registry,
        enabled=True,
        subagent_types={"general-purpose"},
        namespace_owner=owner,
        subagent_models=None,
    )


def _bare_task() -> BackgroundTask:
    return BackgroundTask(
        tool_call_id="tc-1",
        task_id="ab12cd",
        description="d",
        prompt="p",
        subagent_type="general-purpose",
    )


# --------------------------------------------------------------- why it tore

class TestSpillCircuitReason:
    def test_an_exception_opens_the_circuit_with_type_and_message(self):
        task = _bare_task()
        exc = ConnectionError("Connection reset by peer")
        with capture_logs() as logs:
            redis_stream._open_spill_circuit(task, phase="exception", exc=exc, seq=7)
        assert task.redis_write_failed is True
        assert (
            task.redis_write_failure_reason
            == "ConnectionError: Connection reset by peer"
        )
        record = next(l for l in logs if l.get("event") == "subagent_event_spill_failed")
        assert record["error_type"] == "ConnectionError"
        assert record["error"] == "ConnectionError: Connection reset by peer"
        assert record["phase"] == "exception"

    def test_a_non_exception_failure_records_its_reason(self):
        task = _bare_task()
        reason = "asyncio.TimeoutError: spill did not answer in 10s"
        redis_stream._open_spill_circuit(task, phase="timeout", reason=reason, seq=3)
        assert task.redis_write_failed is True
        assert task.redis_write_failure_reason == reason


# ------------------------------------------------------- the last written text

class TestLastWrittenText:
    def test_takes_the_last_assistant_text_not_tool_output(self):
        state = {
            "messages": [
                AIMessage(content="earlier segment"),
                ToolMessage(content="tool output", tool_call_id="a"),
                AIMessage(content="final words"),
            ]
        }
        assert _last_written_text(state) == "final words"

    def test_takes_only_the_text_blocks_of_a_thinking_plus_text_message(self):
        """D-r2: a message that mixes thinking with text is typed reasoning
        whole, which skipped the run's actual last words — the text blocks
        are what count, the thinking blocks are not."""
        state = {
            "messages": [
                AIMessage(content="an earlier message"),
                AIMessage(
                    content=[
                        {"type": "thinking", "thinking": "internal scratch"},
                        {"type": "text", "text": "the visible answer"},
                    ]
                ),
            ]
        }
        assert _last_written_text(state) == "the visible answer"

    def test_a_reasoning_only_message_is_skipped(self):
        state = {
            "messages": [
                AIMessage(content="the answer"),
                AIMessage(
                    content=[
                        {
                            "type": "reasoning",
                            "summary": [{"type": "summary_text", "text": "a thought"}],
                        }
                    ]
                ),
            ]
        }
        assert _last_written_text(state) == "the answer"

    def test_skips_an_empty_tail(self):
        state = {"messages": [AIMessage(content="the answer"), AIMessage(content="")]}
        assert _last_written_text(state) == "the answer"

    def test_none_without_any_assistant_text(self):
        assert _last_written_text({"messages": [ToolMessage(content="x", tool_call_id="a")]}) is None
        assert _last_written_text(None) is None
        assert _last_written_text({"messages": "not-a-list"}) is None


# ------------------------------------------------------------- the abort site

class _GraphThatTears:
    """Yields one state snapshot, then tears its own spill circuit."""

    def __init__(self, task: BackgroundTask) -> None:
        self._task = task

    async def astream(self, state, config, stream_mode=None, durability=None):
        yield ("values", {"messages": [AIMessage(content="analysis through step 90")]})
        self._task.redis_write_failed = True
        self._task.redis_write_failure_reason = "ConnectionError: reset by peer"
        yield ("values", {"messages": []})


@pytest.mark.asyncio
async def test_abort_logs_the_reason_and_stashes_the_last_output():
    """The kill log names why the spill failed, and the last text the run
    wrote is stashed on the task for the failure payload to carry."""
    registry = BackgroundTaskRegistry(thread_id="")
    task = await registry.register("tc-1", "d", "p", "general-purpose")
    graph = _GraphThatTears(task)

    with capture_logs() as logs:
        with pytest.raises(TransportLostError):
            await arun_subagent_streaming(
                graph, {"messages": []}, {"configurable": {}},
                registry=registry, tool_call_id="tc-1",
            )

    assert task.last_output_text == "analysis through step 90"
    record = next(
        l for l in logs
        if l.get("event") == "subagent event spill failed; aborting run"
    )
    assert record["reason"] == "ConnectionError: reset by peer"
    assert record["task_id"] == task.task_id


# ------------------------------------------------- the failure the parent sees

@pytest.mark.asyncio
async def test_finished_torn_failure_carries_answer_and_transcript_path():
    """A run that finished over a torn stream settles transport_lost (the
    retention contract, unchanged) — but its own answer and its transcript
    directory ride the failure on every surface, so the work is recoverable."""
    ledger = FakeLedger()
    mw = _middleware(FakeOwner(), ledger)

    async def _torn_handler(_request) -> ToolMessage:
        task = mw.registry._tasks["tc-1"]
        task.redis_write_failed = True
        task.redis_write_failure_reason = "ConnectionError: reset by peer"
        return ToolMessage(content="90 steps of finished analysis", tool_call_id="tc-1", name="Task")

    await mw.awrap_tool_call(_request({"description": "d", "prompt": "p"}), _torn_handler)
    task = mw.registry._tasks["tc-1"]
    await task.asyncio_task
    task.adopt_writer_outcome(task.asyncio_task)

    (_run_id, status, failure) = ledger.finalized[0]
    assert status == "error"
    assert failure["error_type"] == "transport_lost"
    assert failure["error"].startswith("transport_lost:")
    assert "90 steps of finished analysis" in failure["error"]
    assert TRANSCRIPT_DIR in failure["error"]
    assert "read it to recover the completed work" in failure["error"]
    # The in-memory result the same worker's TaskOutput renders says the same.
    assert "90 steps of finished analysis" in task.result["error"]
    assert TRANSCRIPT_DIR in task.result["error"]


@pytest.mark.asyncio
async def test_abort_failure_carries_stashed_last_output_and_transcript_path():
    """The abort path (a TransportLostError out of the astream loop) settles
    with the same recovery note, its last text coming from the abort stash."""
    ledger = FakeLedger()
    mw = _middleware(FakeOwner(), ledger)

    async def _aborting_handler(_request):
        task = mw.registry._tasks["tc-1"]
        task.redis_write_failed = True
        task.redis_write_failure_reason = "ConnectionError: reset by peer"
        task.last_output_text = "analysis through step 88"
        raise TransportLostError(
            "transport_lost: subagent event spill failed; aborting"
        )

    await mw.awrap_tool_call(_request({"description": "d", "prompt": "p"}), _aborting_handler)
    task = mw.registry._tasks["tc-1"]
    await task.asyncio_task

    (_run_id, status, failure) = ledger.finalized[0]
    assert status == "error"
    assert failure["error_type"] == "transport_lost"
    assert "analysis through step 88" in failure["error"]
    assert TRANSCRIPT_DIR in failure["error"]


@pytest.mark.asyncio
async def test_finished_torn_failure_carries_a_command_answer():
    """D-r2: a normal Task handler returns a Command carrying the final
    ToolMessage, not a ToolMessage — the torn failure must read it, joining
    the content's text blocks rather than putting a Python repr of the list
    in the parent-facing excerpt."""
    from langgraph.types import Command

    ledger = FakeLedger()
    mw = _middleware(FakeOwner(), ledger)

    async def _torn_command_handler(_request):
        mw.registry._tasks["tc-1"].redis_write_failed = True
        return Command(
            update={
                "messages": [
                    AIMessage(
                        content=[
                            {"type": "thinking", "thinking": "scratch"},
                            {"type": "text", "text": "interim"},
                        ]
                    ),
                    ToolMessage(
                        content=[
                            {"type": "text", "text": "the final report, "},
                            {"type": "text", "text": "conclusion intact"},
                        ],
                        tool_call_id="tc-1",
                        name="Task",
                    ),
                ]
            }
        )

    await mw.awrap_tool_call(
        _request({"description": "d", "prompt": "p"}), _torn_command_handler
    )
    task = mw.registry._tasks["tc-1"]
    await task.asyncio_task

    (_run_id, status, failure) = ledger.finalized[0]
    assert status == "error"
    assert failure["error_type"] == "transport_lost"
    assert "the final report, conclusion intact" in failure["error"]
    # The block list joined as text, never str()'d as a Python list.
    assert "{'type': 'text'" not in failure["error"]
    assert "[{" not in failure["error"].split("transport_lost")[1]


@pytest.mark.asyncio
async def test_a_torn_abort_with_nothing_to_recover_keeps_the_plain_contract():
    """No thread id (CLI) and no written text on the abort path: the failure
    stays the bare retention-contract wording, unchanged."""
    registry = BackgroundTaskRegistry(thread_id="")
    registry.run_ledger = FakeLedger()
    mw = BackgroundSubagentMiddleware(
        registry=registry,
        enabled=True,
        subagent_types={"general-purpose"},
        namespace_owner=FakeOwner(),
        subagent_models=None,
    )

    async def _aborting_silent(_request):
        registry._tasks["tc-1"].redis_write_failed = True
        raise TransportLostError(
            "transport_lost: subagent event spill failed; aborting"
        )

    await mw.awrap_tool_call(_request({"description": "d", "prompt": "p"}), _aborting_silent)
    task = registry._tasks["tc-1"]
    await task.asyncio_task

    (run_id, status, failure) = registry.run_ledger.finalized[0]
    assert failure["error_type"] == "transport_lost"
    assert "Recoverable work" not in failure["error"]
    assert failure["error"].startswith("transport_lost:")
