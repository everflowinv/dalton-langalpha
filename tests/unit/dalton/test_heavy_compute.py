"""Direct tests for the killable heavy-compute subprocess runner (A1).

These exercise the REAL runner — no conftest doubles — so they assert what the
unit suite's inline double stands in for: off-process execution, termination at
budget (queueing included), error transport, the passed-deadline short circuit,
the child cap, and cancellation cleanup.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest

import src.dalton.heavy_compute as hc
from src.dalton.heavy_compute import (
    HeavyComputeCancelled,
    HeavyComputeError,
    HeavyComputeTimeout,
    run_in_heavy_subprocess,
)
from src.observability.heavy_tasks import heavy_task, running_tasks


def _boom() -> None:
    raise ValueError("kapow")


def _child_pid() -> int:
    return os.getpid()


def _join(a: str, *, b: str) -> str:
    return a + b


def _touch_file(path: str) -> str:
    with open(path, "w") as f:
        f.write("spawned")
    return path


@pytest.fixture
def one_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DALTON_HEAVY_SUBPROCESS_LIMIT", "1")
    monkeypatch.setattr(hc, "_GATE", None)  # re-read the env on next use


@pytest.mark.asyncio
async def test_runs_in_a_separate_process() -> None:
    child_pid = await run_in_heavy_subprocess(_child_pid, label="smoke.pid")
    assert child_pid != os.getpid()


@pytest.mark.asyncio
async def test_timeout_terminates_the_child() -> None:
    t0 = time.monotonic()
    with pytest.raises(HeavyComputeTimeout) as exc_info:
        await run_in_heavy_subprocess(
            time.sleep, 30, timeout_s=1.5, label="smoke.sleep"
        )
    # The kill must land promptly — 30s of sleep must not be waited out, and
    # the child must be gone, not abandoned with its GIL-free life of its own.
    assert time.monotonic() - t0 < 10.0
    assert exc_info.value.label == "smoke.sleep"
    assert exc_info.value.budget_s == 1.5


@pytest.mark.asyncio
async def test_budget_covers_the_gate_queue(one_slot: None) -> None:
    """A task queued behind the slot holder must not run past its own budget:
    the deadline is absolute from submission, and a slot granted too late
    starts no child."""
    t0 = time.monotonic()
    holder = asyncio.create_task(
        run_in_heavy_subprocess(time.sleep, 2.0, timeout_s=30, label="queue.holder")
    )
    await asyncio.sleep(0.3)  # holder owns the only slot
    with pytest.raises(HeavyComputeTimeout):
        await run_in_heavy_subprocess(
            _touch_file, "/tmp/should-not-spawn", timeout_s=1.0, label="queue.queued"
        )
    elapsed = time.monotonic() - t0
    assert (
        elapsed < 2.0
    ), "queued task must time out at its own deadline, not the holder's"
    assert not Path("/tmp/should-not-spawn").exists(), "must not spawn past its deadline"
    await holder


@pytest.mark.asyncio
async def test_child_exception_surfaces_with_type_and_traceback() -> None:
    with pytest.raises(HeavyComputeError) as exc_info:
        await run_in_heavy_subprocess(_boom, label="smoke.boom")
    assert exc_info.value.exc_type == "ValueError"
    assert "kapow" in str(exc_info.value)
    assert "_boom" in str(exc_info.value)  # traceback rides along


@pytest.mark.asyncio
async def test_past_deadline_raises_without_spawning() -> None:
    with pytest.raises(HeavyComputeTimeout):
        await run_in_heavy_subprocess(
            _child_pid, deadline=time.monotonic() - 1.0, label="smoke.past"
        )


@pytest.mark.asyncio
async def test_kwargs_reach_the_child() -> None:
    assert await run_in_heavy_subprocess(_join, "x", b="y") == "xy"


@pytest.mark.asyncio
async def test_concurrency_cap_serializes_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_HEAVY_SUBPROCESS_LIMIT", "2")
    monkeypatch.setattr(hc, "_GATE", None)  # re-read the env on next use
    t0 = time.monotonic()
    await asyncio.gather(
        *[
            run_in_heavy_subprocess(time.sleep, 1, timeout_s=30, label=f"cap.{i}")
            for i in range(4)
        ]
    )
    # Four 1s jobs through two slots: at least two waves.
    assert time.monotonic() - t0 >= 2.0


def test_task_registered_for_watchdog_while_running() -> None:
    # The watchdog's log line names in-flight heavy tasks; the registry must
    # show one while _run_one executes and none after.
    assert running_tasks() == []
    with heavy_task("probe"):
        running = running_tasks()
        assert len(running) == 1
        assert running[0].name == "probe"
        assert running[0].thread_name  # labelled with its thread
    assert running_tasks() == []


@pytest.mark.asyncio
async def test_run_one_registers_task_on_wait_thread() -> None:
    loop = asyncio.get_running_loop()
    before = running_tasks()
    # _run_one blocks; run it via the loop executor the same way the async
    # wrapper does, and sample the registry from the loop while it runs.
    task = loop.run_in_executor(
        None,
        hc._run_one,
        _child_pid,
        (),
        {},
        time.monotonic() + 10.0,
        10.0,
        "registry.probe",
        threading.Event(),
    )
    deadline = time.monotonic() + 5.0
    seen = False
    while time.monotonic() < deadline:
        names = [t.name for t in running_tasks()]
        if "registry.probe" in names:
            seen = True
            break
        await asyncio.sleep(0.02)
    assert await asyncio.wait_for(task, timeout=10.0) != os.getpid()
    assert seen, "in-flight heavy task was not registered for the watchdog"
    assert running_tasks() == before


@pytest.mark.asyncio
async def test_cancel_kills_child_and_releases_slot(one_slot: None) -> None:
    """Cancelling the await must terminate the child and free the slot within
    about a second — not at budget exhaustion."""
    victim = asyncio.create_task(
        run_in_heavy_subprocess(time.sleep, 30, timeout_s=60, label="cancel.victim")
    )
    # Wait until the victim actually holds the slot and its child is running.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if any(t.name == "cancel.victim" for t in running_tasks()):
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("victim never acquired the slot")

    t0 = time.monotonic()
    victim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await victim

    # The slot comes back promptly: a fresh task through the single slot
    # finishes well inside the victim's 60s budget. The registry clearing also
    # proves the child was reaped (the entry leaves only after join).
    probe = await asyncio.wait_for(
        run_in_heavy_subprocess(_child_pid, timeout_s=10, label="cancel.probe"), 5.0
    )
    assert probe != os.getpid()
    assert time.monotonic() - t0 < 5.0
    assert all(t.name != "cancel.victim" for t in running_tasks())


@pytest.mark.asyncio
async def test_cancel_while_queued_never_spawns(
    one_slot: None, tmp_path: Path
) -> None:
    """A task still waiting for the gate, once cancelled, must never start a
    child — even after the slot frees up for it."""
    marker = tmp_path / "queued-spawn-marker"
    holder = asyncio.create_task(
        run_in_heavy_subprocess(time.sleep, 2, timeout_s=30, label="cancel.holder")
    )
    await asyncio.sleep(0.3)  # holder owns the only slot
    queued = asyncio.create_task(
        run_in_heavy_subprocess(
            _touch_file, str(marker), timeout_s=30, label="cancel.queued"
        )
    )
    await asyncio.sleep(0.4)  # queued sits in the gate wait
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    await holder  # the slot frees here
    await asyncio.sleep(1.0)  # the cancelled worker re-checks within _POLL_S
    assert not marker.exists(), "a cancelled queued task must not spawn a child"
    assert all(t.name != "cancel.queued" for t in running_tasks())


@pytest.mark.asyncio
async def test_pool_queue_still_times_out_at_own_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ONE wait thread and one slot, the second task cannot even enter
    _run_one until the first finishes — no worker-side deadline check can run
    for it. The async-side timeout must fire at its own budget (plus grace),
    not after the holder releases the thread."""
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="heavy-test")
    monkeypatch.setattr(hc, "_WAIT_POOL", pool)
    monkeypatch.setenv("DALTON_HEAVY_SUBPROCESS_LIMIT", "1")
    monkeypatch.setattr(hc, "_GATE", None)
    try:
        holder = asyncio.create_task(
            run_in_heavy_subprocess(
                time.sleep, 3, timeout_s=30, label="pool.holder"
            )
        )
        await asyncio.sleep(0.3)  # the holder owns the only wait thread
        t0 = time.monotonic()
        with pytest.raises(HeavyComputeTimeout):
            await run_in_heavy_subprocess(
                _child_pid, timeout_s=1, label="pool.queued"
            )
        elapsed = time.monotonic() - t0
        # ~budget + 1s grace — nowhere near the holder's 3s.
        assert 1.0 <= elapsed < 2.8
        await holder
    finally:
        pool.shutdown(wait=True)


def test_cancelled_worker_raises_cancelled_for_the_future() -> None:
    # The worker thread surfaces HeavyComputeCancelled so the executor future
    # carries a truthful reason instead of looking like a timeout; set before
    # entry, it also proves no child is spawned on a pre-cancelled task.
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(HeavyComputeCancelled):
        hc._run_one(
            _child_pid,
            (),
            {},
            None,
            None,
            "cancel.precheck",
            cancel,
        )
