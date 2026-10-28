"""Killable subprocess execution for CPU-heavy compute (A1).

``asyncio.to_thread`` moves work off the event loop but not out of the GIL: a
long regex or C call in a worker thread still starves the loop for its whole
duration — the publish audit froze every request, ``/health`` included, for
20-90s stretches that way. This module runs one callable in a dedicated spawn
child instead, so:

- the GIL is out of the picture (a separate interpreter);
- a deadline is enforced by terminating the child, not by giving up on the
  wait while the computation keeps burning CPU;
- concurrency is capped, so a burst of audits cannot fork-bomb the host.

The cost model is one fresh child per call (spawn + module import, ~1-2s), so
this is for work measured in seconds and up — the audit phases, whole-page
HTML conversion, PDF text extraction — never for anything small. Callers pass
module-level functions only: the function and arguments cross the process
boundary by pickle.

The budget covers QUEUEING too: the deadline is converted to an absolute
``time.monotonic()`` instant before the executor hop, waiting for the
concurrency gate and for the child both stop at it, and a slot taken past the
deadline starts no child. Cancelling the await terminates the child (or skips
starting one) and releases the slot promptly instead of at budget exhaustion.

The child cap is ``DALTON_HEAVY_SUBPROCESS_LIMIT`` (default 2), a process-local
execution resource, not request-path state.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from src.observability.heavy_tasks import heavy_task

logger = logging.getLogger(__name__)

_CTX = multiprocessing.get_context("spawn")

# How long a terminated child gets to exit before SIGKILL.
_KILL_GRACE_S = 5.0

# Cadence at which gate/child waits wake up to re-check the deadline and the
# cancel event. Short enough to honor a cancel in well under a second.
_POLL_S = 0.25

# Grace added to the async-side timeout over the worker-side deadline: it only
# needs to cover the worker's last _POLL_S check and result handoff, never a
# child's kill grace (the worker reports its own timeout first).
_ASYNC_TIMEOUT_GRACE_S = 1.0

# Threads that block on the child pipe. One per in-flight child plus slack for
# the ones still draining after their awaiter was cancelled; the gate below,
# not this pool, is the real concurrency cap.
_WAIT_POOL = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="heavy-compute-wait"
)

_GATE: threading.Semaphore | None = None
_GATE_LOCK = threading.Lock()


class HeavyComputeTimeout(Exception):
    """The child was terminated after the deadline/timeout; its result is lost."""

    def __init__(self, label: str, budget_s: float) -> None:
        super().__init__(
            f"heavy compute '{label}' terminated after {budget_s:.1f}s budget"
        )
        self.label = label
        self.budget_s = budget_s


class HeavyComputeCancelled(Exception):
    """The awaiter cancelled; the child (if any) was terminated, the slot released."""

    def __init__(self, label: str) -> None:
        super().__init__(f"heavy compute '{label}' cancelled by its awaiter")
        self.label = label


class HeavyComputeError(Exception):
    """The callable raised in the child; type name and traceback come with it."""

    def __init__(self, label: str, exc_type: str, message: str, tb: str) -> None:
        super().__init__(f"heavy compute '{label}' raised {exc_type}: {message}\n{tb}")
        self.label = label
        self.exc_type = exc_type


def _gate() -> threading.Semaphore:
    global _GATE
    with _GATE_LOCK:
        if _GATE is None:
            try:
                limit = max(
                    1, min(8, int(os.environ.get("DALTON_HEAVY_SUBPROCESS_LIMIT", "2")))
                )
            except ValueError:
                limit = 2
            _GATE = threading.Semaphore(limit)
        return _GATE


def _child_main(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    conn: Any,
) -> None:
    """Run ``fn(*args, **kwargs)`` and send ``(True, result)`` or ``(False, error info)`` back.

    The error crosses as strings, not the exception object: pickling an
    arbitrary exception can itself fail and lose the original error.
    """
    try:
        payload = (True, fn(*args, **kwargs))
        conn.send(payload)
    except BaseException as e:  # noqa: BLE001 - the parent decides what to raise
        try:
            conn.send((False, type(e).__name__, str(e), traceback.format_exc()))
        except Exception as send_error:  # noqa: BLE001 - nothing left to try
            logging.getLogger(__name__).debug(
                "heavy-compute child could not report: %s", send_error
            )
    finally:
        try:
            conn.close()
        except Exception as close_error:  # noqa: BLE001 - best-effort fd cleanup
            logging.getLogger(__name__).debug(
                "heavy-compute child close failed: %s", close_error
            )


def _raise_if_cancelled(cancel: threading.Event, label: str) -> None:
    if cancel.is_set():
        raise HeavyComputeCancelled(label)


def _run_one(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    deadline_at: float | None,
    budget_s: float | None,
    label: str,
    cancel: threading.Event,
) -> Any:
    """Spawn the child, wait for its reply, enforce the deadline. Blocking.

    ``deadline_at`` is absolute (``time.monotonic()``); it bounds the GATE
    WAIT as well as the child run, so a queued task does not start with a
    spent budget. ``cancel`` is set by the async wrapper when its awaiter is
    cancelled: the wait loops check it at ``_POLL_S`` cadence, terminate any
    child, and leave the gate slot to the ``with``/``finally`` exits.
    """
    gate = _gate()
    while True:
        _raise_if_cancelled(cancel, label)
        remaining = None if deadline_at is None else deadline_at - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise HeavyComputeTimeout(label, budget_s or 0.0)
        wait = _POLL_S if remaining is None else min(_POLL_S, max(remaining, 0.001))
        if gate.acquire(timeout=wait):
            break
    try:
        with heavy_task(label):
            _raise_if_cancelled(cancel, label)
            # The queue may have eaten the whole budget; never start a child
            # that is already past its deadline.
            if deadline_at is not None and time.monotonic() >= deadline_at:
                raise HeavyComputeTimeout(label, budget_s or 0.0)
            parent_conn, child_conn = _CTX.Pipe(duplex=False)
            proc = _CTX.Process(
                target=_child_main, args=(fn, args, kwargs, child_conn), daemon=True
            )
            started = False
            try:
                proc.start()
                started = True
                child_conn.close()
                while True:
                    remaining = (
                        None if deadline_at is None else deadline_at - time.monotonic()
                    )
                    if remaining is not None and remaining <= 0:
                        raise HeavyComputeTimeout(label, budget_s or 0.0)
                    wait = (
                        _POLL_S
                        if remaining is None
                        else min(_POLL_S, max(remaining, 0.001))
                    )
                    if parent_conn.poll(wait):
                        break
                    _raise_if_cancelled(cancel, label)
                try:
                    reply = parent_conn.recv()
                except EOFError as e:
                    # The child died before replying (boot failure, OOM-kill).
                    raise HeavyComputeError(
                        label, type(e).__name__, "child exited without a reply", ""
                    ) from e
            finally:
                # On every exit — result, timeout, cancel, boot failure, closed
                # pipe — release the fd and make sure no child is left behind.
                parent_conn.close()
                if started:
                    if proc.is_alive():
                        proc.terminate()
                    proc.join(_KILL_GRACE_S)
                    if proc.is_alive():
                        proc.kill()
                        proc.join()
            if reply[0]:
                return reply[1]
            _, exc_type, message, tb = reply
            raise HeavyComputeError(label, exc_type, message, tb)
    finally:
        gate.release()


async def run_in_heavy_subprocess(
    fn: Callable[..., Any],
    /,
    *args: Any,
    timeout_s: float | None = None,
    deadline: float | None = None,
    label: str = "",
    **kwargs: Any,
) -> Any:
    """``await`` ``fn(*args, **kwargs)`` in a bounded, killable subprocess.

    ``deadline`` is a ``time.monotonic()`` instant; with both given the tighter
    one wins. The budget spans queueing for the concurrency gate. A deadline
    already passed raises :class:`HeavyComputeTimeout` without spawning.
    Cancelling the await terminates the child (or its queued start) and
    releases the slot promptly. ``timeout_s``/``deadline``/``label`` are
    reserved keyword names — a callable that needs one of them takes a thin
    module-level wrapper.
    """
    budget_s = timeout_s
    if deadline is not None:
        # Deadline instants are time.monotonic()-based (the audit builds them so).
        remaining = deadline - time.monotonic()
        budget_s = remaining if budget_s is None else min(budget_s, remaining)
    if budget_s is not None and budget_s <= 0:
        raise HeavyComputeTimeout(label or getattr(fn, "__name__", ""), 0.0)
    label = label or getattr(fn, "__name__", "heavy")
    deadline_at = None if budget_s is None else time.monotonic() + budget_s
    cancel = threading.Event()
    future = asyncio.get_running_loop().run_in_executor(
        _WAIT_POOL,
        _run_one,
        fn,
        args,
        kwargs,
        deadline_at,
        budget_s,
        label,
        cancel,
    )

    def _swallow_result(f: Any) -> None:
        # The outer timeout below can return before this future completes
        # (shield keeps it alive); consume its outcome so asyncio never logs
        # an unretrieved exception for a result nobody awaits anymore.
        if not f.cancelled():
            f.exception()

    future.add_done_callback(_swallow_result)
    try:
        if deadline_at is None:
            return await future
        # The async-side timeout is the QUEUE backstop: with every wait thread
        # busy, a task sits in the executor pool's own queue where NO deadline
        # check can run (it has not entered _run_one yet). The worker-side
        # deadline normally fires first; this one bounds the pool wait too.
        return await asyncio.wait_for(
            asyncio.shield(future),
            timeout=(deadline_at - time.monotonic()) + _ASYNC_TIMEOUT_GRACE_S,
        )
    except asyncio.TimeoutError:
        cancel.set()
        raise HeavyComputeTimeout(label, budget_s or 0.0) from None
    except asyncio.CancelledError:
        # Tell the worker thread to give up its slot and kill any child; the
        # cancellation itself propagates unchanged.
        cancel.set()
        raise
