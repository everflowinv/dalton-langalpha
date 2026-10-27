"""Event-loop lag heartbeat.

A blocked event loop and a sick Redis are indistinguishable from the client
side: both surface as ``Timeout reading from redis`` because redis-py's socket
reader never gets scheduled to drain the reply. This sampler makes the
difference observable — if lag spikes alongside the timeout, the fault is
in-process (CPU-bound work on the loop), not on the wire.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from typing import Optional

from src.utils.concurrency import cancel_and_join

logger = logging.getLogger(__name__)

# Sampling cadence. Short enough to catch the sub-second stalls that precede a
# 5s socket timeout, cheap enough to leave running in production.
_INTERVAL_S = 0.5

# A stall this long is already enough to trip short Redis deadlines (the
# subagent spill uses 0.5s), so it is worth a line in the log on its own.
_WARN_THRESHOLD_MS = 1000.0

# Re-log suppression: a sustained stall would otherwise emit a line per sample.
_WARN_COOLDOWN_S = 10.0

# DALTON-HOOK(loop-stall-stack): daemon watchdog thread that samples the event
# loop thread's call stack via sys._current_frames() when heartbeat stalls >5s.
_STALL_WATCHDOG_ENV = "LOOP_LAG_WATCHDOG_ENABLED"
_STALL_THRESHOLD_S = 5.0
_STALL_COOLDOWN_S = 60.0
_STALL_MAX_FRAMES = 40


def _is_watchdog_enabled() -> bool:
    raw = os.environ.get(_STALL_WATCHDOG_ENV, "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


class EventLoopLagMonitor:
    """Per-worker singleton sampling how late a fixed-interval sleep returns."""

    _instance: Optional["EventLoopLagMonitor"] = None

    def __init__(
        self,
        *,
        stall_threshold_s: float | None = None,
        stall_cooldown_s: float | None = None,
        interval_s: float | None = None,
    ) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._last_warn = 0.0
        self.max_lag_ms = 0.0
        self._interval_s = interval_s  # DALTON-HOOK(loop-stall-stack)
        self._stall_threshold_s = stall_threshold_s
        self._stall_cooldown_s = stall_cooldown_s
        self._loop_thread_id: int | None = None
        self._last_heartbeat = 0.0
        self._heartbeat_seq = 0  # DALTON-HOOK(loop-stall-stack)
        self._sampled_heartbeat_seq = -1
        self._last_stall_sample_at = 0.0
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

    @classmethod
    def get_instance(cls) -> "EventLoopLagMonitor":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _touch_heartbeat(self) -> None:  # DALTON-HOOK(loop-stall-stack)
        self._last_heartbeat = time.monotonic()
        self._heartbeat_seq += 1

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._loop_thread_id = threading.get_ident()
        self._touch_heartbeat()
        self._task = asyncio.create_task(self._run(), name="event-loop-lag")
        if _is_watchdog_enabled() and (
            self._watchdog_thread is None or not self._watchdog_thread.is_alive()
        ):
            self._watchdog_stop.clear()
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name="event-loop-lag-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()

    async def stop(self) -> None:
        self._stopping.set()
        self._watchdog_stop.set()
        watchdog, self._watchdog_thread = self._watchdog_thread, None
        if watchdog is not None and watchdog.is_alive():
            watchdog.join(timeout=1.0)
        task, self._task = self._task, None
        if task is not None:
            await cancel_and_join(task)

    # DALTON-HOOK(loop-stall-stack): capture event-loop thread stack once per
    # stall (>5s without heartbeat) with a 60s cooldown, truncated to 40 frames.
    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.is_set():
            threshold_s = (
                self._stall_threshold_s
                if self._stall_threshold_s is not None
                else _STALL_THRESHOLD_S
            )
            cooldown_s = (
                self._stall_cooldown_s
                if self._stall_cooldown_s is not None
                else _STALL_COOLDOWN_S
            )
            poll_s = max(0.01, min(0.25, threshold_s / 5.0))
            if self._watchdog_stop.wait(timeout=poll_s):
                break
            if self._stopping.is_set():
                break

            hb_seq = self._heartbeat_seq
            hb_at = self._last_heartbeat
            if hb_at <= 0.0:
                continue
            now = time.monotonic()
            elapsed_s = now - hb_at
            if elapsed_s < threshold_s:
                continue
            if self._sampled_heartbeat_seq == hb_seq:
                continue
            if (
                self._last_stall_sample_at > 0.0
                and (now - self._last_stall_sample_at) < cooldown_s
            ):
                continue

            self._sampled_heartbeat_seq = hb_seq
            self._last_stall_sample_at = now
            tid = self._loop_thread_id
            frame = sys._current_frames().get(tid) if tid is not None else None
            try:
                if frame is not None:
                    extracted = traceback.extract_stack(frame, limit=_STALL_MAX_FRAMES)
                    stack_text = "".join(traceback.format_list(extracted)).rstrip()
                else:
                    stack_text = "<no frame found for event loop thread>"
            finally:
                # Immediately drop the frame reference so finished request frames
                # and their local objects are not retained until the next stall.
                frame = None
            logger.warning(
                "[loop-lag] event loop heartbeat stalled %.2f s "
                "(threshold %.2f s, thread_id=%s) — call stack (max %d frames):\n%s",
                elapsed_s,
                threshold_s,
                tid,
                _STALL_MAX_FRAMES,
                stack_text,
            )

    async def _run(self) -> None:
        from src.observability.metrics import event_loop_lag_ms

        while not self._stopping.is_set():
            interval_s = (  # DALTON-HOOK(loop-stall-stack)
                self._interval_s if self._interval_s is not None else _INTERVAL_S
            )
            self._touch_heartbeat()
            before = time.monotonic()
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                return
            self._touch_heartbeat()  # DALTON-HOOK(loop-stall-stack)
            lag_ms = (time.monotonic() - before - interval_s) * 1000.0
            if lag_ms < 0:
                lag_ms = 0.0
            self.max_lag_ms = max(self.max_lag_ms, lag_ms)
            try:
                event_loop_lag_ms.record(lag_ms)
            except Exception:
                pass
            if lag_ms >= _WARN_THRESHOLD_MS:
                now = time.monotonic()
                if now - self._last_warn >= _WARN_COOLDOWN_S:
                    self._last_warn = now
                    logger.warning(
                        "[loop-lag] event loop stalled %.0f ms "
                        "(sample interval %.0f ms) — Redis/SSE timeouts in this "
                        "window are in-process, not upstream",
                        lag_ms,
                        interval_s * 1000.0,  # DALTON-HOOK(loop-stall-stack)
                    )
