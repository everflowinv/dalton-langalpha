"""R1 fix1: the guard tail drain waits each writer out to ITS OWN clock.

Production (10-28 晚, HOOD / Visa): a deep workflow ran as the turn's tail
writer; the drain's flat 1800s deadline discarded its session 30 minutes in
— the parent's next checkpoint hit "the connection is closed", its children
were fenced out with GuardSessionLost. Round 1 aligned the deadline with the
global workflow_timeout; this round sizes it per writer: a live ROOT
workflow run gets its own ``run_timeout`` window counted from its start
(plus a settle margin), every other writer keeps the flat 1800s tail
allowance, and a writer past its own deadline still discards (fail-closed),
with the timeout naming the deadline it actually used.

R3 (11-02, DASH Task-IbtxIg): an ORDINARY background Task outliving its
turn hit the same flat 1800s — fenced out as worker_lost 30 minutes in
while the Dalton overlay had raised workflow child_timeout to 5400s. The
ordinary allowance is now ``max(SUBAGENT_TAIL_TIMEOUT, child_timeout)`` —
the same ceiling a workflow child gets — in the guard drain, the stop
bill's settle wait, and the kill-path archive wait.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest

from ptc_agent.agent.middleware.background_subagent import (
    run_executor,
    subagent as subagent_mod,
    tail_drain,
)
from src.config.models import WorkflowOrchestrationConfig
from src.dalton.subagent_slots import SubagentSlotLease
from src.ptc_agent.agent.middleware.background_subagent.registry import (
    BackgroundTaskRegistry,
)
from src.ptc_agent.agent.middleware.background_subagent.task import BackgroundTask
from src.server.services import background_registry_store
from src.server.services.runs import coordinator, executor, subagent_collection
from src.server.services.writer_guard import GuardSessionLost, WriterGuard
from src.utils.tracking.per_call_token_tracker import PerCallTokenTracker


class _FakeRegistry:
    """The REAL BackgroundTaskRegistry holding real BackgroundTasks with hung
    writers, under fixed task ids so the drain can inspect their clocks.
    """

    def __init__(self, *task_ids: str, stop_at: float | None = None):
        self._registry = BackgroundTaskRegistry("t-1")
        self._handles = []
        self.tasks: dict[str, BackgroundTask] = {}
        for task_id in task_ids:
            handle = asyncio.get_running_loop().create_task(asyncio.Event().wait())
            self._handles.append(handle)
            task = BackgroundTask(
                tool_call_id=f"call-{task_id}",
                task_id=task_id,
                description=task_id,
                prompt="",
                subagent_type="workflow",
                asyncio_task=handle,
                stop_at=stop_at,
            )
            self.tasks[task_id] = task
            self._registry._tasks[task.tool_call_id] = task
            self._registry._task_id_to_tool_call_id[task_id] = task.tool_call_id

    async def live_writers_by_task(self, pred):
        return await self._registry.live_writers_by_task(pred)

    async def time_out_writers_where(self, pred):
        return await self._registry.time_out_writers_where(pred)

    def settle(self):
        for handle in self._handles:
            handle.cancel()


class _RegistryStore:
    def __init__(self, registry):
        self._registry = registry

    def get_instance(self):
        return self

    async def get_registry(self, thread_id):
        return self._registry


class _Probe(Exception):
    """Raised by the patched asyncio.wait to unwind the drain after capture."""


def _drain_patches(store: _RegistryStore):
    return patch.object(
        background_registry_store.BackgroundRegistryStore,
        "get_instance",
        create_autospec(
            background_registry_store.BackgroundRegistryStore.get_instance,
            return_value=store,
        ),
    )


def _workflow_row(
    task_id: str, *, started: datetime, parent_run_id: str | None = "run-parent"
) -> dict:
    return {
        "task_id": task_id,
        "task_run_id": f"tr-{task_id}",
        "parent_run_id": parent_run_id,
        "started_at": started,
        "status": "in_progress",
    }


class _Settings:
    """Enter a tuple of patch contexts (keeps the with-blocks readable).

    The orchestration double is the REAL WorkflowOrchestrationConfig: the
    ordinary-writer allowance reads ``child_timeout`` off it, and a loose
    namespace would hide a missing or mis-typed field until production.
    ``run_timeout`` must exceed ``child_timeout`` (model validator).
    """

    def __init__(
        self,
        *,
        run_timeout: int,
        global_timeout: float,
        child_timeout: int = 1800,
    ):
        import src.config.settings as settings_mod

        self._patches = [
            patch.object(
                settings_mod,
                "get_workflow_orchestration_config",
                create_autospec(
                    settings_mod.get_workflow_orchestration_config,
                    return_value=WorkflowOrchestrationConfig(
                        run_timeout=run_timeout,
                        child_timeout=child_timeout,
                    ),
                ),
            ),
            patch.object(
                settings_mod,
                "get_workflow_timeout",
                create_autospec(
                    settings_mod.get_workflow_timeout,
                    return_value=global_timeout,
                ),
            ),
        ]

    def __enter__(self):
        self._stack = ExitStack()
        for ctx in self._patches:
            self._stack.enter_context(ctx)
        return self

    def __exit__(self, *exc):
        self._stack.close()


@pytest.mark.asyncio
async def test_a_live_workflow_writer_gets_its_own_run_timeout_window(monkeypatch):
    """The drain's first wait carries the workflow's OWN window — its
    run_timeout (via task.stop_at) plus TAIL_DRAIN_SLACK_S — not the
    ordinary tail allowance (floored at child_timeout 1s here, the patched
    0.05s constant below it) and not the main turn's global workflow_timeout
    (0.05s)."""
    monkeypatch.setattr(subagent_collection, "SUBAGENT_TAIL_TIMEOUT", 0.05)
    monkeypatch.setattr(tail_drain, "TAIL_DRAIN_SLACK_S", 0.05)
    captured: dict = {}

    async def _capture_wait(futures, timeout=None):
        captured["timeout"] = timeout
        raise _Probe

    store = _RegistryStore(_FakeRegistry("wf-1", stop_at=time.time() + 2.0))
    guard = SimpleNamespace(
        run_id="run-1",
        owns_task_ns=lambda task_id: True,
    )
    ex = executor.LocalRunExecutor()
    with (
        _drain_patches(store),
        patch("asyncio.wait", _capture_wait),
        _Settings(run_timeout=2, global_timeout=0.05, child_timeout=1),
    ):
        with pytest.raises(_Probe):
            await ex._drain_run_subagent_writers("t-1", "run-1", guard)

    # ≈ run_timeout 2 + slack 0.05; the flat allowance would give 1.0
    assert captured["timeout"] > 1.6
    store._registry.settle()


@pytest.mark.asyncio
async def test_nonworkflow_writers_keep_the_flat_tail_allowance(monkeypatch):
    """A writer with its own 1.0s stop_at clock waits its own window plus
    slack — not the global workflow_timeout (5s)."""
    monkeypatch.setattr(subagent_collection, "SUBAGENT_TAIL_TIMEOUT", 0.05)
    monkeypatch.setattr(tail_drain, "TAIL_DRAIN_SLACK_S", 0.05)
    captured: dict = {}

    async def _capture_wait(futures, timeout=None):
        captured["timeout"] = timeout
        raise _Probe

    store = _RegistryStore(_FakeRegistry("plain-1", stop_at=time.time() + 1.0))
    guard = SimpleNamespace(
        run_id="run-1",
        owns_task_ns=lambda task_id: True,
    )
    ex = executor.LocalRunExecutor()
    with (
        _drain_patches(store),
        patch("asyncio.wait", _capture_wait),
        _Settings(run_timeout=2, global_timeout=5.0, child_timeout=1),
    ):
        with pytest.raises(_Probe):
            await ex._drain_run_subagent_writers("t-1", "run-1", guard)

    # The writer's own 1.0s clock (+ 0.05 slack), not the global 5s
    assert 0.9 < captured["timeout"] <= 1.06
    store._registry.settle()


@pytest.mark.asyncio
async def test_ordinary_tail_writer_waits_the_child_timeout_when_longer(monkeypatch):
    """Production 11-02 (DASH Task-IbtxIg): a plain background Task that
    outlived its turn was drained on the flat 1800s and fenced out as
    worker_lost 30 minutes in. Under drain_tail_writers, a writer with a
    5400s clock gets the full 5400s window before overdue, and
    subagent_collection.subagent_tail_timeout() is
    max(SUBAGENT_TAIL_TIMEOUT, child_timeout) = 5400s."""
    monkeypatch.setattr(tail_drain, "TAIL_DRAIN_SLACK_S", 0.0)
    monkeypatch.setattr(tail_drain, "SETTLED_UNWIND_S", 10000.0)
    captured: dict = {}

    async def _capture_wait(futures, timeout=None):
        captured["timeout"] = timeout
        raise _Probe

    store = _RegistryStore(_FakeRegistry("plain-1", stop_at=time.time() + 5400.0))
    guard = SimpleNamespace(
        run_id="run-1",
        owns_task_ns=lambda task_id: True,
    )
    ex = executor.LocalRunExecutor()
    try:
        with (
            _drain_patches(store),
            patch("asyncio.wait", _capture_wait),
            _Settings(run_timeout=16200, global_timeout=5400.0, child_timeout=5400),
        ):
            assert subagent_collection.subagent_tail_timeout() == 5400.0
            with pytest.raises(_Probe):
                await ex._drain_run_subagent_writers("t-1", "run-1", guard)

        assert 5000 < captured["timeout"] <= 5400  # child_timeout, not 1800
    finally:
        store._registry.settle()


@pytest.mark.asyncio
async def test_ordinary_tail_writer_at_the_upstream_default_keeps_1800s(monkeypatch):
    """Upstream default (child_timeout 1800s): the ordinary allowance stays
    at the flat 1800s — the max can raise the ceiling but never lower it."""
    monkeypatch.setattr(tail_drain, "TAIL_DRAIN_SLACK_S", 0.0)
    captured: dict = {}

    async def _capture_wait(futures, timeout=None):
        captured["timeout"] = timeout
        raise _Probe

    store = _RegistryStore(_FakeRegistry("plain-1", stop_at=time.time() + 1800.0))
    guard = SimpleNamespace(
        run_id="run-1",
        owns_task_ns=lambda task_id: True,
    )
    ex = executor.LocalRunExecutor()
    try:
        with (
            _drain_patches(store),
            patch("asyncio.wait", _capture_wait),
            _Settings(run_timeout=3600, global_timeout=1800.0, child_timeout=1800),
        ):
            assert subagent_collection.subagent_tail_timeout() == 1800.0
            with pytest.raises(_Probe):
                await ex._drain_run_subagent_writers("t-1", "run-1", guard)

        assert 1700 < captured["timeout"] <= 1800  # the flat 1800s allowance
    finally:
        store._registry.settle()


@pytest.mark.asyncio
async def test_stop_bill_keeps_the_upstream_allowance_under_a_longer_child_timeout():
    """The stop bill waits the whole batch, so it stays at the upstream 1800s
    even when child_timeout is 5400: settle_abandoned stamps a task finalized
    more than 30 minutes ago, and a longer batch wait would let a finished task
    leave the in-flight count before its usage row exists. The retained-task set is spied (not consulted) because its
    done-callback discards a finished collector before a name lookup could
    find it, and the collector must finish under the patches."""
    captured: dict = {}

    async def _capture_wait(futures, timeout=None):
        captured["timeout"] = timeout
        return (set(), set(futures))  # all writers still "hung"

    handle = asyncio.get_running_loop().create_task(asyncio.Event().wait())
    task = BackgroundTask(
        tool_call_id="call-bill-1",
        task_id="bill-1",
        description="bill-1",
        prompt="",
        subagent_type="general-purpose",
        asyncio_task=handle,
    )
    retained: list[asyncio.Task] = []
    retain = create_autospec(
        subagent_collection._retain_collector, side_effect=retained.append
    )
    persist = create_autospec(subagent_collection.persist_subagent_usage)
    try:
        with (
            patch.object(subagent_collection, "_retain_collector", retain),
            patch("asyncio.wait", _capture_wait),
            patch.object(subagent_collection, "persist_subagent_usage", persist),
            _Settings(run_timeout=16200, global_timeout=1.0, child_timeout=5400),
        ):
            await subagent_collection._bill_after_unwind(
                "run-bill", [task], "t-1", "ws-1", "u-1", False,
            )
            assert retained, "no bill collector was retained"
            await asyncio.wait_for(retained[0], timeout=5.0)

        assert captured["timeout"] == subagent_collection.SUBAGENT_TAIL_TIMEOUT == 1800.0
        assert persist.await_count == 1
    finally:
        handle.cancel()


@pytest.mark.asyncio
async def test_archive_waits_the_child_timeout_sized_allowance():
    """The kill path's archive wait matches too: a lane's events must not be
    read and persisted while its writer may still append under the longer
    allowance the guard drain would have held it to."""
    captured: dict = {}

    async def _capture_wait(futures, timeout=None):
        captured["timeout"] = timeout
        return (set(), set(futures))

    handle = asyncio.get_running_loop().create_task(asyncio.Event().wait())
    task = BackgroundTask(
        tool_call_id="call-arch-1",
        task_id="arch-1",
        description="arch-1",
        prompt="",
        subagent_type="general-purpose",
        asyncio_task=handle,
    )
    retained: list[asyncio.Task] = []
    retain = create_autospec(
        subagent_collection._retain_collector, side_effect=retained.append
    )
    killed = create_autospec(subagent_collection.killed_lane, return_value=[])
    try:
        with (
            patch.object(subagent_collection, "_retain_collector", retain),
            patch("asyncio.wait", _capture_wait),
            patch.object(subagent_collection, "killed_lane", killed),
            _Settings(run_timeout=16200, global_timeout=1.0, child_timeout=5400),
        ):
            subagent_collection._archive_after_unwind(
                "t-1", "run-arch", [task], "ws-1", None,
            )
            assert retained, "no archive collector was retained"
            await asyncio.wait_for(retained[0], timeout=5.0)

        assert 5000 < captured["timeout"] <= 5400
        assert killed.await_count == 1
    finally:
        handle.cancel()


@pytest.mark.asyncio
async def test_a_writer_past_its_own_deadline_discards_naming_the_basis(monkeypatch):
    """Fail-closed kept: a workflow writer past its own stop_at deadline that
    refuses to unwind when stopped raises TimeoutError naming run_id."""
    monkeypatch.setattr(tail_drain, "TAIL_DRAIN_SLACK_S", 0.01)
    monkeypatch.setattr(tail_drain, "CANCEL_UNWIND_TIMEOUT", 0.02)
    monkeypatch.setattr(tail_drain, "SUBAGENT_CLOSE_TIMEOUT_S", 0.0)

    async def _stubborn() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(5)

    fake_reg = _FakeRegistry("wf-hung", stop_at=time.time() - 10.0)
    fake_reg.settle()
    stubborn_handle = asyncio.create_task(_stubborn())
    fake_reg._handles = [stubborn_handle]
    fake_reg.tasks["wf-hung"].asyncio_task = stubborn_handle
    await asyncio.sleep(0)

    store = _RegistryStore(fake_reg)
    guard = SimpleNamespace(
        run_id="run-1",
        owns_task_ns=lambda task_id: True,
    )
    ex = executor.LocalRunExecutor()
    try:
        with (
            _drain_patches(store),
            _Settings(run_timeout=2, global_timeout=2, child_timeout=1),
        ):
            with pytest.raises(TimeoutError) as exc_info:
                await asyncio.wait_for(
                    ex._drain_run_subagent_writers("t-1", "run-1", guard),
                    timeout=5.0,
                )
            assert "run=run-1" in str(exc_info.value)
            assert "after a stop" in str(exc_info.value)
    finally:
        store._registry.settle()


@pytest.mark.asyncio
async def test_a_persistently_unreadable_workflow_ledger_fails_closed_at_a_fixed_deadline(monkeypatch):
    """When a writer has no stop_at of its own, drain_tail_writers anchors its
    fallback deadline at the drain's start (_longest_writer_clock) and never
    recomputes it on later loop iterations — so a stubborn writer still fails
    closed at the fixed anchor."""
    monkeypatch.setattr(tail_drain, "TAIL_DRAIN_SLACK_S", 0.01)
    monkeypatch.setattr(tail_drain, "CANCEL_UNWIND_TIMEOUT", 0.02)
    monkeypatch.setattr(tail_drain, "SUBAGENT_CLOSE_TIMEOUT_S", 0.0)
    monkeypatch.setattr(tail_drain, "_longest_writer_clock", lambda: 0.05)

    async def _stubborn() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(5)

    fake_reg = _FakeRegistry("hung-1", stop_at=None)
    fake_reg.settle()
    stubborn_handle = asyncio.create_task(_stubborn())
    fake_reg._handles = [stubborn_handle]
    fake_reg.tasks["hung-1"].asyncio_task = stubborn_handle
    await asyncio.sleep(0)

    store = _RegistryStore(fake_reg)
    guard = SimpleNamespace(
        run_id="run-1",
        owns_task_ns=lambda task_id: True,
    )
    ex = executor.LocalRunExecutor()
    try:
        with _drain_patches(store):
            with pytest.raises(TimeoutError) as exc_info:
                await asyncio.wait_for(
                    ex._drain_run_subagent_writers("t-1", "run-1", guard),
                    timeout=3.0,
                )
            assert "run=run-1" in str(exc_info.value)
    finally:
        store._registry.settle()


@pytest.mark.asyncio
async def test_subagent_stop_at_starts_only_after_slot_acquired(monkeypatch):
    """DALTON-HOOK(subagent-caps): task.stop_at is None while queued waiting
    for a concurrency slot and only starts counting from slot acquisition."""
    registry = BackgroundTaskRegistry("t-slot-stop-at")
    task = await registry.register(
        tool_call_id="tc-slot-clock",
        description="queued child",
        prompt="work",
        subagent_type="research",
    )
    lease = SubagentSlotLease(
        thread_id="t-slot-stop-at",
        task_id=task.task_id,
        slot_member=f"t-slot-stop-at:{task.task_id}",
        max_per_thread=1,
        max_global=1,
        poll_min_s=0.01,
        poll_max_s=0.01,
    )
    lease.status = "queued"
    lease.was_queued = True
    task.slot_lease = lease

    slot_open = False
    observed_while_queued: list[float | None] = []

    async def _try_acquire(mode: str = "acquire") -> bool:
        observed_while_queued.append(task.stop_at)
        if slot_open:
            lease._mark_acquired(fail_open=False)
            return True
        return False

    monkeypatch.setattr(
        lease,
        "try_acquire_once",
        create_autospec(lease.try_acquire_once, side_effect=_try_acquire),
    )

    class _FakeSubagent:
        async def astream(self, state, config, **kwargs):
            yield ("values", {"messages": []})

    run_task = asyncio.create_task(
        subagent_mod.arun_subagent_streaming(
            _FakeSubagent(),
            {"messages": []},
            {"configurable": {"thread_id": "t-slot-stop-at"}},
            registry=registry,
            tool_call_id="tc-slot-clock",
            time_limit=10.0,
        )
    )
    await asyncio.sleep(0.05)
    assert observed_while_queued
    assert all(val is None for val in observed_while_queued)
    assert task.stop_at is None

    slot_acquired_at = time.time()
    slot_open = True
    await asyncio.wait_for(run_task, timeout=2.0)

    assert task.stop_at is not None
    assert task.stop_at >= slot_acquired_at + 10.0
    await lease.release()


@pytest.mark.asyncio
async def test_subagent_timed_out_after_slot_queue_marks_timed_out_not_cancelled(
    monkeypatch,
):
    """DALTON-HOOK(subagent-caps): when a subagent is queued for a slot and
    times out (either via TIME_LIMIT_CANCEL from drain_tail_writers while queued
    or on its own stop_at clock after acquiring the slot), it settles with
    timed_out=True (terminal_status='timeout', ledger 'error') rather than
    cancelled=True (terminal_status='cancelled')."""
    monkeypatch.setattr("src.config.settings.get_workflow_timeout", lambda: 0.05)
    monkeypatch.setattr(
        "src.config.settings.get_workflow_orchestration_config",
        lambda: SimpleNamespace(run_timeout=0.05),
    )
    monkeypatch.setattr(tail_drain, "TAIL_DRAIN_SLACK_S", 0.0)
    monkeypatch.setattr(tail_drain, "CANCEL_UNWIND_TIMEOUT", 0.2)
    monkeypatch.setattr(tail_drain, "SUBAGENT_CLOSE_TIMEOUT_S", 0.0)

    class _Ledger:
        def __init__(self) -> None:
            self.statuses: list[str] = []
            self.failures: list[dict | None] = []

        async def finalize_task_run(self, task_run_id, status, **kwargs):
            self.statuses.append(status)
            self.failures.append(kwargs.get("failure"))
            return {"applied": True, "run": {"status": status}}

        async def append_run_end(self, task_run_id, **kwargs):
            pass

    # Case 1: subagent is still queued in wait_for_slot when drain_tail_writers
    # fires time_out_writers_where (TIME_LIMIT_CANCEL must not be swallowed by
    # abort_queued_subagent_on_cancel).
    registry = BackgroundTaskRegistry("t-queued-timeout")
    ledger = registry.run_ledger = _Ledger()
    task = await registry.register(
        tool_call_id="tc-queued-timeout",
        description="queued subagent",
        prompt="work",
        subagent_type="research",
    )
    task.task_run_id = "tr-queued-timeout"
    lease = SubagentSlotLease(
        thread_id="t-queued-timeout",
        task_id=task.task_id,
        slot_member=f"t-queued-timeout:{task.task_id}",
        max_per_thread=1,
        max_global=1,
        poll_min_s=0.02,
        poll_max_s=0.02,
    )
    lease.status = "queued"
    lease.was_queued = True
    task.slot_lease = lease

    async def _never_acquire(mode: str = "acquire") -> bool:
        return False

    monkeypatch.setattr(
        lease,
        "try_acquire_once",
        create_autospec(lease.try_acquire_once, side_effect=_never_acquire),
    )

    async def _queued_runner(_request):
        await lease.wait_for_slot(task, registry=registry)
        return "unreachable"

    writer = asyncio.create_task(
        run_executor._run_background_task(
            task,
            _queued_runner,
            None,
            PerCallTokenTracker(),
            "Subagent",
            registry=registry,
        )
    )
    task.asyncio_task = writer
    await asyncio.sleep(0.01)

    store = _RegistryStore(registry)
    guard = SimpleNamespace(
        run_id="run-1",
        owns_task_ns=lambda task_id: task_id == task.task_id,
    )
    ex = executor.LocalRunExecutor()
    with _drain_patches(store):
        await ex._drain_run_subagent_writers("t-queued-timeout", "run-1", guard)
    outcome = task.adopt_writer_outcome(writer)

    assert task.timed_out is True
    assert task.terminal_status == "timeout"
    assert task.terminal_status != "cancelled"
    assert outcome["status"] == "timeout"
    assert ledger.statuses == ["error"]
    assert ledger.failures[0]["error_type"] == "SubagentTimeLimitError"

    # Case 2: subagent queues for a slot, acquires it, and then times out on its
    # own clock after slot acquisition.
    registry2 = BackgroundTaskRegistry("t-post-queue-timeout")
    ledger2 = registry2.run_ledger = _Ledger()
    task2 = await registry2.register(
        tool_call_id="tc-post-queue-timeout",
        description="post-queue timeout subagent",
        prompt="work",
        subagent_type="research",
    )
    task2.task_run_id = "tr-post-queue-timeout"
    lease2 = SubagentSlotLease(
        thread_id="t-post-queue-timeout",
        task_id=task2.task_id,
        slot_member=f"t-post-queue-timeout:{task2.task_id}",
        max_per_thread=1,
        max_global=1,
        poll_min_s=0.01,
        poll_max_s=0.01,
    )
    lease2.status = "queued"
    lease2.was_queued = True
    task2.slot_lease = lease2

    async def _acquire_on_first_poll(mode: str = "acquire") -> bool:
        lease2._mark_acquired(fail_open=False)
        return True

    monkeypatch.setattr(
        lease2,
        "try_acquire_once",
        create_autospec(lease2.try_acquire_once, side_effect=_acquire_on_first_poll),
    )
    monkeypatch.setattr(subagent_mod, "SUBAGENT_STOP_GRACE_S", 0.0)

    class _HangingSubagent:
        async def astream(self, state, config, **kwargs):
            await asyncio.Event().wait()
            yield ("values", {"messages": []})

    async def _post_queue_runner(_request):
        return await subagent_mod.arun_subagent_streaming(
            _HangingSubagent(),
            {"messages": []},
            {"configurable": {"thread_id": "t-post-queue-timeout"}},
            registry=registry2,
            tool_call_id="tc-post-queue-timeout",
            time_limit=0.03,
        )

    writer2 = asyncio.create_task(
        run_executor._run_background_task(
            task2,
            _post_queue_runner,
            None,
            PerCallTokenTracker(),
            "Subagent",
            registry=registry2,
        )
    )
    task2.asyncio_task = writer2
    await asyncio.wait_for(writer2, timeout=2.0)
    outcome2 = task2.adopt_writer_outcome(writer2)

    assert task2.timed_out is True
    assert task2.terminal_status == "timeout"
    assert task2.terminal_status != "cancelled"
    assert outcome2["status"] == "timeout"
    assert ledger2.statuses == ["error"]
    assert ledger2.failures[0]["error_type"] == "SubagentTimeLimitError"


# ---------------------------------------------------------------------------
# The blast radius: a failed drain discards the session under live writers
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self):
        self.closed = False
        self.executed: list[str] = []

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return None

    async def close(self):
        self.closed = True


class _FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.putconns = 0

    async def putconn(self, conn):
        self.putconns += 1


async def _await_putconn(pool: _FakePool, calls: int = 1) -> None:
    for _ in range(200):
        if pool.putconns >= calls:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("guard teardown never returned its connection")


@pytest.mark.asyncio
async def test_a_failed_drain_closes_the_connection_under_live_writers():
    """The production symptom, link by link: a tail drain that raises (the
    pre-fix 1800s deadline under a live workflow) makes the teardown discard
    the session — the physical connection CLOSES (the parent's
    'the connection is closed') and every task-namespace fence falls with it
    (the children's GuardSessionLost)."""
    conn = _FakeConn()
    pool = _FakePool(conn)
    guard = WriterGuard(pool, conn, "t-1", "run-1")
    # Root + one child namespace held, as a turn with a live workflow has it.
    guard._shared_held = True
    guard._root_held = True
    guard._task_ns.add("task-1")
    handle = SimpleNamespace(guard=guard, run_id="run-1")

    async def _drain_raises():
        raise TimeoutError("tail drain deadline: 1 subagent writer(s) still running")

    coord = coordinator.RunCoordinator.get_instance()
    coord._teardown_guard(handle, tail_drain=_drain_raises)
    await _await_putconn(pool)

    assert conn.closed is True  # "the connection is closed" on the parent
    assert pool.putconns == 1
    # And the child's next checkpoint is fenced out with the exact wording
    # production saw: the session no longer owns that task namespace.
    with pytest.raises(GuardSessionLost, match="no longer owns that task namespace"):
        guard.check_write_ns(
            {"configurable": {"checkpoint_ns": "task:task-1|sub"}}
        )


@pytest.mark.asyncio
async def test_a_settled_drain_releases_the_session_clean():
    """The converse the fix restores: a drain that waits the writer out ends
    with a CLEAN release — unlock_all on the live connection, no close, the
    connection returned to the pool."""
    conn = _FakeConn()
    pool = _FakePool(conn)
    guard = WriterGuard(pool, conn, "t-1", "run-1")
    guard._shared_held = True
    guard._root_held = True
    guard._task_ns.add("task-1")
    handle = SimpleNamespace(guard=guard, run_id="run-1")

    async def _drain_settles():
        return None

    coord = coordinator.RunCoordinator.get_instance()
    coord._teardown_guard(handle, tail_drain=_drain_settles)
    await _await_putconn(pool)

    assert conn.closed is False
    assert any("pg_advisory_unlock_all" in sql for sql in conn.executed)
    assert pool.putconns == 1
