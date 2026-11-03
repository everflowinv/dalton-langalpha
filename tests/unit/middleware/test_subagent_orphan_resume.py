"""A resume arbitrates "still running" against the run ledger, not the local
handle (the 2f1387ec sequence).

A writer whose guard connection died keeps running in its worker after the
recovery scanner finalized its run (worker_lost) and took the fence. Resume
used to answer "still running" off the live local task alone, so the parked
follow-up steers re-woke the thread for a resume that could only fail again,
while TaskOutput — reading the ledger — called the same task failed. Here the
ledger's terminal row proves the local writer is an orphan: it is stopped
(bounded) and the resume proceeds through the namespace lock. A writer that
is merely settling (its handler done, the terminal pipeline mid-flight) is
waited out, never cancelled; a run the ledger ended as cancelled is refused
as before; and an orphan whose terminal CAS lost never writes meta or
sentinel over the successor round.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from langchain_core.messages import ToolMessage

from ptc_agent.agent.middleware.background_subagent import task_actions
from ptc_agent.agent.middleware.background_subagent.middleware import (
    BackgroundSubagentMiddleware,
)
from ptc_agent.agent.middleware.background_subagent.registry import (
    BackgroundTask,
    BackgroundTaskRegistry,
)
from ptc_agent.agent.middleware.steering import SteeringMiddleware, TaskResume, _resume_call
from src.server.services.report_back.steer import SteeredReportInbox
from src.utils.stream_coalescing import DeltaCoalescer, StreamFrame


class FakeOwner:
    def __init__(self, grant: bool = True) -> None:
        self.grant = grant
        self.acquired: list[str] = []
        self.released: list[str] = []

    async def acquire_task_ns(self, task_id: str) -> bool:
        self.acquired.append(task_id)
        return self.grant

    async def release_task_ns(self, task_id: str) -> None:
        self.released.append(task_id)


class FakeLedger:
    """The coordinator's duck-typed surface, as the registry holds it, with
    the DB's own semantics: admitted rows start in_progress and are the
    latest chain, and the terminal CAS matches nothing once the row is
    already terminal (recovery, another worker)."""

    def __init__(self, latest: dict | None = None) -> None:
        self.latest = latest
        self.rows: dict[str, dict] = {}
        self.admitted: list[str] = []
        self.run_ids: list[str] = []
        self.finalized: list[tuple[str, str]] = []
        self.reads = 0
        self.run_ends: list[str] = []
        # The finalize SQL's cancel-intent override: a committed
        # cancel_requested_at finalizes the row as cancelled whatever the
        # writer asked for.
        self.cancel_intent = False

    async def get_latest_run(self, task_id: str) -> dict | None:
        self.reads += 1
        return self.latest

    async def start_task_run(self, **kwargs) -> str:
        run_id = f"tr-{len(self.run_ids) + 2}"
        self.run_ids.append(run_id)
        self.admitted.append(str(kwargs.get("cause")))
        self.rows[run_id] = {"task_run_id": run_id, "status": "in_progress"}
        self.latest = self.rows[run_id]
        return run_id

    async def finalize_task_run(
        self, task_run_id: str, status: str, **kwargs
    ) -> dict:
        row = self.rows.get(task_run_id)
        if row is None or row.get("status") != "in_progress":
            return {"applied": False, "run": dict(self.latest or {})}
        if self.cancel_intent:
            status = "cancelled"
        row = {**row, "status": status}
        self.rows[task_run_id] = row
        self.finalized.append((task_run_id, status))
        if (self.latest or {}).get("task_run_id") == task_run_id:
            self.latest = row
        return {"applied": True, "run": dict(row)}

    async def append_run_end(self, task_run_id: str, **kwargs) -> None:
        self.run_ends.append(task_run_id)
        return None

    async def mark_result_delivered(self, task_run_id: str) -> bool:
        return True


class _Request(SimpleNamespace):
    def override(self, **changes) -> "_Request":
        return _Request(**{**vars(self), **changes})


def _request(args: dict, tool_call_id: str = "tc-1") -> _Request:
    return _Request(
        tool_call={"name": "Task", "id": tool_call_id, "args": args},
        runtime=SimpleNamespace(
            config={
                "configurable": {"thread_id": "thread-x"},
                "metadata": {"run_id": "run-1"},
            }
        ),
    )


async def _ok_handler(_request) -> ToolMessage:
    return ToolMessage(content="done", tool_call_id="tc-1", name="Task")


def _resume_args(task_id: str = "abc123") -> dict:
    return {
        "action": "resume",
        "task_id": task_id,
        "description": "d",
        "prompt": "p",
        "subagent_type": "general-purpose",
    }


_RESUME_ARGS = _resume_args()


def _middleware(owner, ledger) -> BackgroundSubagentMiddleware:
    registry = BackgroundTaskRegistry(thread_id="")
    registry.run_ledger = ledger
    return BackgroundSubagentMiddleware(
        registry=registry,
        enabled=True,
        subagent_types={"general-purpose"},
        namespace_owner=owner,
    )


def _seed_orphan(mw, orphan: asyncio.Task) -> BackgroundTask:
    task = BackgroundTask(
        tool_call_id="tc-orig",
        task_id="abc123",
        description="d",
        prompt="p",
        subagent_type="general-purpose",
        agent_id="general-purpose:x",
        terminal_status=None,
        asyncio_task=orphan,
        task_run_id="tr-1",
    )
    mw.registry._tasks["tc-orig"] = task
    mw.registry._task_id_to_tool_call_id["abc123"] = "tc-orig"
    return task


async def _running_orphan(coro) -> asyncio.Task:
    """An orphan past its first await: cancel() before it starts would end a
    coroutine that never ran, and prove nothing about stopping a writer."""
    orphan = asyncio.create_task(coro)
    await asyncio.sleep(0)  # let it reach its suspension point
    return orphan


def _run_row(status: str, *, failure: dict | None = None) -> dict:
    """A latest-run row as ``get_task_run`` reads it (``SELECT *``): flat
    table columns, the failure payload one jsonb column."""
    return {
        "task_run_id": "tr-1",
        "thread_id": "thread-x",
        "task_id": "abc123",
        "status": status,
        "failure": failure,
        "cancel_requested_at": None,
        "finalized_at": None,
        "result_delivered_at": None,
    }


def _in_progress_run() -> dict:
    return _run_row("in_progress")


def _worker_lost_run() -> dict:
    return _run_row(
        "error",
        failure={
            "error": "worker_lost: no live executor holds this task's "
            "namespace fence",
            "error_type": "worker_lost",
        },
    )


def _cancelled_run(failure: dict | None = None) -> dict:
    return _run_row("cancelled", failure=failure)


# ---------------------------------------------------------------------------
# what keeps answering "still running"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "latest",
    [
        pytest.param(_in_progress_run(), id="ledger-in-progress"),
        pytest.param(None, id="no-ledger-row"),
    ],
)
async def test_a_running_or_missing_ledger_run_keeps_saying_still_running(latest):
    owner = FakeOwner()
    mw = _middleware(owner, FakeLedger(latest))
    mw.registry.write_task_meta = AsyncMock()
    orphan = await _running_orphan(asyncio.sleep(30))
    _seed_orphan(mw, orphan)
    try:
        result = await mw.awrap_tool_call(_request(_RESUME_ARGS), _ok_handler)
        assert "still running" in result.content
        assert not orphan.done()  # never stopped on the ledger's word alone
        assert owner.acquired == []
    finally:
        orphan.cancel()


@pytest.mark.asyncio
async def test_a_failed_ledger_read_never_stops_a_live_writer():
    """A DB hiccup must read as running: stopping a genuinely live writer on
    a failed read is the one unrecoverable mistake here."""
    ledger = FakeLedger(_worker_lost_run())

    async def _raise(task_id):
        raise RuntimeError("db")

    ledger.get_latest_run = _raise
    owner = FakeOwner()
    mw = _middleware(owner, ledger)
    orphan = await _running_orphan(asyncio.sleep(30))
    _seed_orphan(mw, orphan)
    try:
        result = await mw.awrap_tool_call(_request(_RESUME_ARGS), _ok_handler)
        assert "still running" in result.content
        assert not orphan.done()
    finally:
        orphan.cancel()


@pytest.mark.asyncio
async def test_a_stale_ledger_row_never_stops_a_rebound_task():
    """The read and the stop straddle an await: a task rebound to another
    round since the read (a sibling resume admitted and reset it) is not the
    stale row's orphan to kill."""
    ledger = FakeLedger(_worker_lost_run())
    mw = _middleware(FakeOwner(), ledger)
    orphan = await _running_orphan(asyncio.sleep(30))
    task = _seed_orphan(mw, orphan)
    task.task_run_id = "tr-9"  # the sibling's admitted round

    try:
        result = await mw.awrap_tool_call(_request(_RESUME_ARGS), _ok_handler)
        assert "still running" in result.content
        assert not orphan.done()
    finally:
        orphan.cancel()


# ---------------------------------------------------------------------------
# the orphan: terminal ledger row, live local writer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_stops_a_worker_lost_orphan_and_resumes_it():
    owner = FakeOwner()
    ledger = FakeLedger(_worker_lost_run())
    mw = _middleware(owner, ledger)
    mw.registry.write_task_meta = AsyncMock()
    orphan = await _running_orphan(asyncio.sleep(30))
    task = _seed_orphan(mw, orphan)

    result = await mw.awrap_tool_call(_request(_RESUME_ARGS), _ok_handler)

    assert "Resumed" in result.content
    assert orphan.cancelled() and orphan.done()
    assert ledger.admitted == ["resume"]  # a new run under the fence
    assert owner.acquired == ["abc123"]  # the namespace lock arbitrated
    await task.asyncio_task  # the resumed round's writer settles
    assert ledger.finalized == [("tr-2", "completed")]


@pytest.mark.asyncio
async def test_an_orphan_that_outlives_the_stop_cap_refuses_until_it_ends():
    """The wait is bounded: a writer still unwinding at the cap refuses the
    resume (a retry costs one call), and once it ends the retry resumes."""

    async def _slow_unwind():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)  # slower than the patched cap
            raise

    owner = FakeOwner()
    ledger = FakeLedger(_worker_lost_run())
    mw = _middleware(owner, ledger)
    mw.registry.write_task_meta = AsyncMock()
    orphan = await _running_orphan(_slow_unwind())
    _seed_orphan(mw, orphan)

    with patch.object(task_actions, "_ORPHAN_WRITER_STOP_TIMEOUT_S", 0.02):
        refused = await mw.awrap_tool_call(_request(_RESUME_ARGS), _ok_handler)
        assert refused.status == "error"
        assert "still stopping" in refused.content
        assert owner.acquired == []  # the fence was never taken

    with pytest.raises(asyncio.CancelledError):
        await orphan  # it ends on its own; nothing leaks

    result = await mw.awrap_tool_call(_request(_RESUME_ARGS, tool_call_id="tc-2"), _ok_handler)
    assert "Resumed" in result.content


@pytest.mark.asyncio
async def test_parallel_resumes_never_cancel_the_started_writer():
    """The claim spans the ledger read: a sibling resume whose read returns
    late must be refused at the gate — before its read — so it can never
    cancel the writer the first call started (codex 1)."""
    owner = FakeOwner()
    ledger = FakeLedger(_worker_lost_run())

    async def _delayed_read(task_id):
        await asyncio.sleep(0.05)  # the stale-read window, held wide open
        ledger.reads += 1
        return ledger.latest

    ledger.get_latest_run = _delayed_read
    mw = _middleware(owner, ledger)
    mw.registry.write_task_meta = AsyncMock()
    orphan = await _running_orphan(asyncio.sleep(30))
    task = _seed_orphan(mw, orphan)

    r1, r2 = await asyncio.gather(
        mw.awrap_tool_call(_request(_RESUME_ARGS, tool_call_id="tc-a"), _ok_handler),
        mw.awrap_tool_call(_request(_RESUME_ARGS, tool_call_id="tc-b"), _ok_handler),
    )
    contents = [r1.content, r2.content]

    assert sum("Resumed" in c for c in contents) == 1
    assert sum("already being resumed" in c for c in contents) == 1
    # The winner's early read and under-fence recheck; the sibling read none.
    assert ledger.reads == 2
    assert orphan.cancelled()
    await task.asyncio_task
    assert not task.asyncio_task.cancelled()  # the started writer ran its course


# ---------------------------------------------------------------------------
# a settling writer is waited out, never cancelled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_writer_mid_settle_is_waited_out_not_cancelled():
    """A normally-ending writer parked inside its terminal settle (ledger CAS
    committed, meta pending) is not an orphan: cancelling there would strand
    the fence and drop run_end. Resume waits it out and then proceeds."""
    owner = FakeOwner()
    ledger = FakeLedger()
    mw = _middleware(owner, ledger)

    gate = asyncio.Event()
    entered = asyncio.Event()
    meta_calls: list[str] = []

    async def _gated_meta(_task, status):
        meta_calls.append(status)
        if status != "running":  # the terminal write is the settle's
            entered.set()
            await gate.wait()

    mw.registry.write_task_meta = AsyncMock(side_effect=_gated_meta)

    launch = await mw.awrap_tool_call(
        _request({"description": "d", "prompt": "p"}, tool_call_id="tc-w"),
        _ok_handler,
    )
    assert "deployed" in launch.content
    task = mw.registry._tasks["tc-w"]
    await asyncio.wait_for(entered.wait(), timeout=5)  # wrapper is mid-settle
    writer = task.asyncio_task
    assert not writer.done() and writer.cancelling() == 0
    assert ledger.finalized == [(ledger.run_ids[0], "completed")]  # CAS applied

    resume = asyncio.create_task(
        mw.awrap_tool_call(
            _request(_resume_args(task.task_id), tool_call_id="tc-r"), _ok_handler
        )
    )
    for _ in range(3):
        await asyncio.sleep(0)  # past the read and the stop/no-stop decision
    assert writer.cancelling() == 0  # settling, not lost: no cancel was sent

    gate.set()
    reply = await asyncio.wait_for(resume, timeout=5)
    assert "Resumed" in reply.content
    assert writer.done() and not writer.cancelled()  # ended on its own terms
    # Its settle finished before the resume proceeded; the trailing entry is
    # the resumed round's own spawn.
    assert meta_calls[:2] == ["running", "completed"]
    assert ledger.admitted == ["init", "resume"]
    await task.asyncio_task


# ---------------------------------------------------------------------------
# a run the ledger ended as cancelled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_ledger_cancelled_run_is_stamped_cancelled_and_refused():
    """Recovery finalized the row as cancelled (a cancel intent this worker
    never saw land): the orphan is stopped with the cancelled seal and the
    resume is refused, as ``unresumable`` would."""
    owner = FakeOwner()
    ledger = FakeLedger(_cancelled_run())
    mw = _middleware(owner, ledger)
    orphan = await _running_orphan(asyncio.sleep(30))
    task = _seed_orphan(mw, orphan)

    result = await mw.awrap_tool_call(_request(_RESUME_ARGS), _ok_handler)

    assert "was cancelled and cannot be resumed" in result.content
    assert task.terminal_status == "cancelled"
    assert orphan.cancelled()
    assert owner.acquired == []
    assert ledger.admitted == []  # no run was ever admitted for it


@pytest.mark.asyncio
async def test_a_credit_stop_cancelled_run_still_resumes():
    """A credit stop is the one cancel meant to be re-entered."""
    owner = FakeOwner()
    ledger = FakeLedger(
        _cancelled_run(failure={"error": "out of credit", "error_type": "credit_stop"})
    )
    mw = _middleware(owner, ledger)
    mw.registry.write_task_meta = AsyncMock()
    orphan = await _running_orphan(asyncio.sleep(30))
    task = _seed_orphan(mw, orphan)

    result = await mw.awrap_tool_call(_request(_RESUME_ARGS), _ok_handler)

    assert "Resumed" in result.content
    assert orphan.cancelled()
    await task.asyncio_task


# ---------------------------------------------------------------------------
# an orphan whose terminal CAS lost writes nothing over the successor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lost_cas_settle_writes_no_meta_and_no_sentinel():
    """Recovery already ended the run (its CAS won) and a successor may have
    written "running" since: the orphan's settle must not overwrite the meta
    nor append an auto-id sentinel onto the task_id-scoped stream."""
    owner = FakeOwner()
    ledger = FakeLedger()
    mw = _middleware(owner, ledger)

    meta_calls: list[str] = []
    sentinels: list[str] = []
    retentions: list[str] = []

    async def _meta(_task, status):
        meta_calls.append(status)

    async def _sentinel(task):
        sentinels.append(task.task_id)

    async def _retention(task):
        retentions.append(task.task_id)

    mw.registry.write_task_meta = AsyncMock(side_effect=_meta)
    mw.registry.append_sentinel_for_task = AsyncMock(side_effect=_sentinel)
    mw.registry.stamp_terminal_retention_for_task = AsyncMock(side_effect=_retention)

    release = asyncio.Event()

    async def _handler(_request):
        await release.wait()
        return ToolMessage(content="done", tool_call_id="tc-w", name="Task")

    launch = await mw.awrap_tool_call(
        _request({"description": "d", "prompt": "p"}, tool_call_id="tc-w"), _handler
    )
    assert "deployed" in launch.content
    task = mw.registry._tasks["tc-w"]
    while not ledger.run_ids:  # admitted, writer running
        await asyncio.sleep(0)
    run_id = ledger.run_ids[0]
    # Recovery ends the run's row while the handler is still parked inside.
    ledger.rows[run_id]["status"] = "error"
    ledger.latest = {**ledger.rows[run_id], "status": "error"}
    release.set()

    await task.asyncio_task

    assert ledger.finalized == []  # the settle's CAS matched nothing
    assert meta_calls == ["running"]  # spawn's; the terminal meta was skipped
    assert sentinels == []  # the sentinel was skipped
    assert retentions == [task.task_id]  # the expiry clock still stamped
    assert owner.released == [task.task_id]  # and the fence still released


@pytest.mark.asyncio
async def test_stopping_an_orphan_drops_its_held_delta():
    """The "error" stamp is not a cancel, so ``_emit``'s kill seal (which a
    completed task's trailing frames must not trip) does not cover the stop:
    the held delta is dropped at the stop itself, and the unwinding writer's
    final flush finds nothing to spill past recovery's run_end."""
    registry = BackgroundTaskRegistry(thread_id="t")
    spills: list[dict] = []

    async def _spill(_task, records):
        spills.extend(records)

    registry._spill_records_to_redis = _spill

    def _task_with_held_delta() -> BackgroundTask:
        task = BackgroundTask(
            tool_call_id="tc-orphan",
            task_id="abc123",
            description="d",
            prompt="p",
            subagent_type="general-purpose",
            agent_id="general-purpose:x",
        )
        task.delta_coalescer = DeltaCoalescer()
        task.delta_coalescer.offer(
            StreamFrame("message_chunk", {"content": "partial answer", "content_type": "text"})
        )
        assert task.delta_coalescer.holding
        return task

    orphan = _task_with_held_delta()
    assert await task_actions._stop_orphaned_writer(orphan, "tc-1") is None
    assert not orphan.delta_coalescer.holding  # dropped, not flushed
    await registry.flush_held_delta(orphan)  # the unwind's final flush
    assert spills == []

    live = _task_with_held_delta()  # control: nothing stopped it
    await registry.flush_held_delta(live)
    assert len(spills) == 1  # the live writer's delta still goes out


@pytest.mark.asyncio
async def test_a_stopped_orphans_late_frames_do_not_land():
    """The seal outlives the held-delta drop: frames the stopped orphan's
    cancel-unwind still produces (a forwarder's finalize appends
    reasoning_signal) must not land after its run's run_end. Terminal
    bookkeeping (steering_returned) passes as a kill's does, and the resumed
    round's reset opens the stream again."""
    owner = FakeOwner()
    mw = _middleware(owner, FakeLedger(_worker_lost_run()))
    spills: list[dict] = []

    async def _spill(_task, records):
        spills.extend(records)

    mw.registry._spill_records_to_redis = _spill
    orphan = await _running_orphan(asyncio.sleep(30))
    task = _seed_orphan(mw, orphan)

    assert await task_actions._stop_orphaned_writer(task, "tc-1") is None
    assert task.stream_sealed is True

    # A frame the unwind emits after the stop: dropped, not spilled.
    await mw.registry.append_captured_event(
        task.tool_call_id,
        {"event": "reasoning_signal", "data": {"agent": "task:abc123", "phase": "complete"}},
    )
    assert task.captured_event_count == 0
    assert spills == []

    # Unwind bookkeeping is exempt, as it is under a kill's seal.
    await mw.registry.append_event_for_task(
        task,
        {"event": "steering_returned", "data": {"agent": "task:abc123"}},
        terminal=True,
    )
    assert task.captured_event_count == 1

    # The resumed round's reset opens the stream for its own writer; the
    # round-scoped counter restarted with the reset. (A non-delta event: a
    # delta would be held by the coalescer, seal or no seal.)
    await mw._reset_task_for_resume(task, task_run_id="tr-2", spawned_run_id="run-9")
    assert task.stream_sealed is False
    await mw.registry.append_captured_event(
        task.tool_call_id,
        {"event": "tool_calls", "data": {"agent": "task:abc123"}},
    )
    assert task.captured_event_count == 1
    assert len(spills) == 2  # the exempt terminal frame, then this one


@pytest.mark.asyncio
async def test_a_second_resume_of_a_ledger_cancelled_settling_task_is_still_refused():
    """The ledger's cancelled verdict survives the settling-writer route: the
    first resume refuses before the fence (no local stamp — the writer was
    only waited out), and the unified under-fence recheck refuses the second
    one and stamps the task, so the third is refused by the top-of-flow
    check. No resume of a cancelled run ever admits."""
    owner = FakeOwner()
    ledger = FakeLedger()
    mw = _middleware(owner, ledger)

    gate = asyncio.Event()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _gated_meta(_task, status):
        if status != "running":
            entered.set()
            await gate.wait()

    mw.registry.write_task_meta = AsyncMock(side_effect=_gated_meta)

    async def _handler(_request):
        await release.wait()
        return ToolMessage(content="done", tool_call_id="tc-w", name="Task")

    launch = await mw.awrap_tool_call(
        _request({"description": "d", "prompt": "p"}, tool_call_id="tc-w"), _handler
    )
    assert "deployed" in launch.content
    task = mw.registry._tasks["tc-w"]
    while not ledger.run_ids:  # admitted
        await asyncio.sleep(0)
    ledger.cancel_intent = True  # the durable cancel intent beats the settle
    release.set()
    await asyncio.wait_for(entered.wait(), timeout=5)  # parked mid-settle

    resume1 = asyncio.create_task(
        mw.awrap_tool_call(
            _request(_resume_args(task.task_id), tool_call_id="tc-r1"), _ok_handler
        )
    )
    for _ in range(3):
        await asyncio.sleep(0)  # past the ledger read, into the wait-out
    gate.set()  # the settle finishes; the first resume's wait ends on it
    first = await asyncio.wait_for(resume1, timeout=5)
    assert "was cancelled and cannot be resumed" in first.content
    assert task.terminal_status is None  # nothing stamped it yet
    assert ledger.admitted == ["init"]

    second = await mw.awrap_tool_call(
        _request(_resume_args(task.task_id), tool_call_id="tc-r2"), _ok_handler
    )
    assert "was cancelled and cannot be resumed" in second.content
    assert task.terminal_status == "cancelled"  # the recheck stamped it
    assert ledger.admitted == ["init"]  # still nothing admitted

    third = await mw.awrap_tool_call(
        _request(_resume_args(task.task_id), tool_call_id="tc-r3"), _ok_handler
    )
    assert "was cancelled and cannot be resumed" in third.content

    # init's admit/settle pair and the second resume's acquire/release pair.
    assert owner.acquired == [task.task_id, task.task_id]
    assert owner.released == [task.task_id, task.task_id]


@pytest.mark.asyncio
async def test_a_failed_ledger_recheck_under_the_fence_refuses_and_releases():
    """A read failure under the fence is not an absent row: admitting on an
    unreadable ledger could resume a run the ledger ended as cancelled, so
    the fence goes back and the model is told to retry."""
    owner = FakeOwner()
    ledger = FakeLedger(_worker_lost_run())

    async def _raise(task_id):
        raise RuntimeError("db")

    ledger.get_latest_run = _raise
    mw = _middleware(owner, ledger)
    mw.registry.write_task_meta = AsyncMock()
    _seed_orphan(mw, None)  # prior round gone: no live writer to arbitrate

    result = await mw.awrap_tool_call(_request(_RESUME_ARGS), _ok_handler)

    assert "could not be read. Try again" in result.content
    assert owner.acquired == ["abc123"]  # the fence was taken …
    assert owner.released == ["abc123"]  # … and given straight back
    assert ledger.admitted == []  # nothing was admitted on an unread ledger


# ---------------------------------------------------------------------------
# the sequence: one follow-up, one wake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_follow_up_on_a_worker_lost_task_wakes_the_thread_once():
    """The 2f1387ec sequence end to end at the middleware seam: the steering
    middleware calls Task(action="resume") for the parked follow-up rows, the
    resume succeeds through the ledger arbitration, and the rows are handed
    to the admitted run — released rows are what re-due the steer and woke
    the thread a second time."""
    owner = FakeOwner()
    ledger = FakeLedger(_worker_lost_run())
    mw = _middleware(owner, ledger)
    mw.registry.write_task_meta = AsyncMock()
    orphan = await _running_orphan(asyncio.sleep(30))
    task = _seed_orphan(mw, orphan)

    inbox = create_autospec(SteeredReportInbox, instance=True)
    inbox.hand_off_resumes.return_value = ["job-1"]

    call = _resume_call(
        [
            TaskResume(
                task_id="abc123",
                job_ids=["job-1"],
                entries=[{"input_id": "in-1", "content": "narrow it to Q3"}],
            )
        ],
        "run-A",
    )
    request = _Request(
        tool_call=call.tool_calls[0],
        state={"messages": [call]},
        runtime=SimpleNamespace(config={"configurable": {"thread_id": "thread-x"}}),
    )

    async def _handler(req):
        return await mw.awrap_tool_call(req, _ok_handler)

    reply = await SteeringMiddleware(inbox).awrap_tool_call(request, _handler)

    assert "Resumed" in reply.content  # the old code answered "still running"
    assert orphan.cancelled() and orphan.done()
    # The rows belong to the admitted run now; nothing goes back to pending,
    # so the steer executor has nothing to post a second wake for.
    inbox.hand_off_resumes.assert_awaited_once_with(
        "thread-x", "run-A", "abc123", ledger.run_ids[0], ["job-1"]
    )
    inbox.release_resumes.assert_not_awaited()
    inbox.confirm_resumes.assert_not_awaited()
    await task.asyncio_task
