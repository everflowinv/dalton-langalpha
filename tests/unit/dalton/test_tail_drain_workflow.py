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
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest

from src.server.database.runs import subagent_runs as sr_db
from src.server.services import background_registry_store
from src.server.services.runs import coordinator, executor, subagent_collection
from src.server.services.writer_guard import GuardSessionLost, WriterGuard


class _FakeRegistry:
    """Hand-written registry double: owned tasks with hung writers.

    ``live_writers_where`` returns real never-completing Tasks (hashable,
    awaitable — what asyncio.wait in the drain requires) tagged with the
    task_id the ownership predicate reads.
    """

    def __init__(self, *task_ids: str):
        self._tasks = []
        for task_id in task_ids:
            task = asyncio.get_running_loop().create_task(asyncio.Event().wait())
            task.task_id = task_id
            self._tasks.append(task)

    async def live_writers_where(self, pred):
        return [t for t in self._tasks if pred(t)]

    def settle(self):
        for task in self._tasks:
            task.cancel()


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
    """Enter a tuple of patch contexts (keeps the with-blocks readable)."""

    def __init__(self, *, run_timeout: float, global_timeout: float):
        import src.config.settings as settings_mod

        self._patches = [
            patch.object(
                settings_mod,
                "get_workflow_orchestration_config",
                create_autospec(
                    settings_mod.get_workflow_orchestration_config,
                    return_value=SimpleNamespace(run_timeout=run_timeout),
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
    run_timeout counted from its start plus the settle margin — not the flat
    tail allowance and not the main turn's global workflow_timeout (both
    patched tiny here; the round-1 global-max shape would wait only 0.05s)."""
    monkeypatch.setattr(subagent_collection, "SUBAGENT_TAIL_TIMEOUT", 0.05)
    monkeypatch.setattr(executor.LocalRunExecutor, "_WORKFLOW_SETTLE_MARGIN_S", 0.05)
    captured: dict = {}

    async def _capture_wait(futures, timeout=None):
        captured["timeout"] = timeout
        raise _Probe

    store = _RegistryStore(_FakeRegistry("wf-1"))
    guard = SimpleNamespace(
        run_id="run-1",
        owns_task_ns=lambda task_id: True,
    )
    ex = executor.LocalRunExecutor()
    with (
        _drain_patches(store),
        patch("asyncio.wait", _capture_wait),
        patch.object(
            sr_db,
            "list_open_workflow_runs_for_thread",
            create_autospec(
                sr_db.list_open_workflow_runs_for_thread,
                return_value=[_workflow_row("wf-1", started=datetime.now(tz=UTC))],
            ),
        ),
        _Settings(run_timeout=0.6, global_timeout=0.05),
    ):
        with pytest.raises(_Probe):
            await ex._drain_run_subagent_writers("t-1", "run-1", guard)

    assert captured["timeout"] > 0.4  # ≈ run_timeout 0.6 + margin 0.05
    store._registry.settle()


@pytest.mark.asyncio
async def test_nonworkflow_writers_keep_the_flat_tail_allowance(monkeypatch):
    """No live root workflow: the drain waits the flat SUBAGENT_TAIL_TIMEOUT
    and nothing more — the global workflow_timeout must not stretch a wait
    for ordinary writers (round-1's global-max shape waited it out)."""
    monkeypatch.setattr(subagent_collection, "SUBAGENT_TAIL_TIMEOUT", 0.05)
    monkeypatch.setattr(executor.LocalRunExecutor, "_WORKFLOW_SETTLE_MARGIN_S", 0.05)
    captured: dict = {}

    async def _capture_wait(futures, timeout=None):
        captured["timeout"] = timeout
        raise _Probe

    store = _RegistryStore(_FakeRegistry("plain-1"))
    guard = SimpleNamespace(
        run_id="run-1",
        owns_task_ns=lambda task_id: True,
    )
    ex = executor.LocalRunExecutor()
    # A workflow CHILD row (no parent run) is an ordinary writer here: it
    # answers to child_timeout, not run_timeout.
    with (
        _drain_patches(store),
        patch("asyncio.wait", _capture_wait),
        patch.object(
            sr_db,
            "list_open_workflow_runs_for_thread",
            create_autospec(
                sr_db.list_open_workflow_runs_for_thread,
                return_value=[
                    _workflow_row("plain-1", started=datetime.now(tz=UTC),
                                  parent_run_id=None)
                ],
            ),
        ),
        _Settings(run_timeout=5.0, global_timeout=5.0),
    ):
        with pytest.raises(_Probe):
            await ex._drain_run_subagent_writers("t-1", "run-1", guard)

    assert captured["timeout"] <= 0.1  # the flat 0.05 allowance
    store._registry.settle()


@pytest.mark.asyncio
async def test_a_writer_past_its_own_deadline_discards_naming_the_basis(monkeypatch):
    """Fail-closed kept: a workflow writer past its own run_timeout window
    still raises, and the error names the deadline it actually used — the
    workflow's own run_timeout — not a fixed 1800s."""
    monkeypatch.setattr(subagent_collection, "SUBAGENT_TAIL_TIMEOUT", 0.02)
    monkeypatch.setattr(executor.LocalRunExecutor, "_WORKFLOW_SETTLE_MARGIN_S", 0.01)
    store = _RegistryStore(_FakeRegistry("wf-hung"))
    guard = SimpleNamespace(
        run_id="run-1",
        owns_task_ns=lambda task_id: True,
    )
    ex = executor.LocalRunExecutor()
    started_long_ago = datetime.now(tz=UTC) - timedelta(seconds=10)
    try:
        with (
            _drain_patches(store),
            patch.object(
                sr_db,
                "list_open_workflow_runs_for_thread",
                create_autospec(
                    sr_db.list_open_workflow_runs_for_thread,
                    return_value=[
                        _workflow_row("wf-hung", started=started_long_ago)
                    ],
                ),
            ),
            _Settings(run_timeout=0.02, global_timeout=0.02),
        ):
            drained = asyncio.create_task(
                ex._drain_run_subagent_writers("t-1", "run-1", guard)
            )
            try:
                await asyncio.wait_for(drained, timeout=5.0)
            except asyncio.TimeoutError as exc:
                assert "run_timeout" in str(exc)
                assert "1800" not in str(exc)
    finally:
        store._registry.settle()


@pytest.mark.asyncio
async def test_a_persistently_unreadable_workflow_ledger_fails_closed_at_a_fixed_deadline(monkeypatch):
    """fix2: when the open-workflow read keeps failing, the fallback
    deadline is anchored at the DRAIN'S start and never recomputed — each
    loop re-anchoring on ``now`` would postpone it forever and the session
    would never fail-closed. Here the read fails every snapshot and the
    writer is hung: the drain still raises, at the fixed anchor."""
    monkeypatch.setattr(subagent_collection, "SUBAGENT_TAIL_TIMEOUT", 0.02)
    monkeypatch.setattr(executor.LocalRunExecutor, "_WORKFLOW_SETTLE_MARGIN_S", 0.01)
    store = _RegistryStore(_FakeRegistry("hung-1"))
    guard = SimpleNamespace(
        run_id="run-1",
        owns_task_ns=lambda task_id: True,
    )
    ex = executor.LocalRunExecutor()

    async def _unreadable(thread_id):
        raise ConnectionError("workflow ledger down")

    try:
        with (
            _drain_patches(store),
            patch.object(
                sr_db,
                "list_open_workflow_runs_for_thread",
                create_autospec(
                    sr_db.list_open_workflow_runs_for_thread,
                    side_effect=_unreadable,
                ),
            ),
            _Settings(run_timeout=0.02, global_timeout=0.25),
        ):
            drained = asyncio.create_task(
                ex._drain_run_subagent_writers("t-1", "run-1", guard)
            )
            try:
                await asyncio.wait_for(drained, timeout=1.5)
            except asyncio.TimeoutError as exc:
                assert "from the drain's start" in str(exc)
    finally:
        store._registry.settle()


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
