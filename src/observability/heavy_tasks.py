"""In-flight heavy compute tasks, for the loop-stall watchdog's log lines only.

The event-loop watchdog (`loop_lag.py`) fires on a stall knowing only that the
loop is stuck, not what was running. This registry lets the CPU-heavy paths
(publish-audit matching, HTML/PDF extraction) say "I started at T on thread X",
so a stall report can name the suspect beside every thread's stack.

It is diagnostics, never state: nothing on a request path may read it to
decide behavior (multi-worker rule — process-local memory is not truth). The
watchdog is the only reader.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class HeavyTask:
    """One heavy computation in flight. `started_monotonic` is time.monotonic()."""

    name: str
    thread_name: str
    thread_id: int
    started_monotonic: float


_lock = threading.Lock()
_tasks: dict[int, HeavyTask] = {}
_next_key = 0


def _register(name: str) -> int:
    global _next_key
    with _lock:
        key = _next_key
        _next_key += 1
        _tasks[key] = HeavyTask(
            name=name,
            thread_name=threading.current_thread().name,
            thread_id=threading.get_ident(),
            started_monotonic=time.monotonic(),
        )
        return key


def _unregister(key: int) -> None:
    with _lock:
        _tasks.pop(key, None)


@contextmanager
def heavy_task(name: str) -> Iterator[None]:
    """Mark `name` as running for the watchdog's stall reports."""
    key = _register(name)
    try:
        yield
    finally:
        _unregister(key)


def running_tasks() -> list[HeavyTask]:
    """Snapshot of the in-flight heavy tasks, oldest first. For logs only."""
    with _lock:
        return sorted(_tasks.values(), key=lambda t: t.started_monotonic)
