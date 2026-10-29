"""Unit tests for EventLoopLagMonitor stall stack watchdog thread."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from src.observability.loop_lag import (
    _STALL_MAX_FRAMES,
    _STALL_THRESHOLD_S,
    _STALL_WATCHDOG_ENV,
    EventLoopLagMonitor,
)


def _blocking_culprit_function(duration_s: float) -> None:
    time.sleep(duration_s)


def _deep_recursive_blocker(depth: int, duration_s: float) -> None:
    if depth <= 0:
        _blocking_culprit_function(duration_s)
        return
    _deep_recursive_blocker(depth - 1, duration_s)


# ------------------------------------------------ startup configuration log


@pytest.mark.asyncio
async def test_start_logs_watchdog_configuration_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    monitor = EventLoopLagMonitor(stall_threshold_s=0.08, stall_cooldown_s=60.0)
    with caplog.at_level(logging.INFO, logger="src.observability.loop_lag"):
        monitor.start()
        try:
            info_lines = [
                r.getMessage()
                for r in caplog.records
                if r.levelno == logging.INFO and "watchdog enabled=" in r.getMessage()
            ]
        finally:
            await monitor.stop()

    assert len(info_lines) == 1
    message = info_lines[0]
    assert "watchdog enabled=True" in message
    assert "stall_threshold_s=0.1" in message  # %.1f of 0.08
    assert "stall_cooldown_s=60.0" in message
    assert "all_thread_stacks=True" in message
    assert f"max_frames={_STALL_MAX_FRAMES}" in message


@pytest.mark.asyncio
async def test_start_logs_disabled_watchdog_and_starts_no_thread(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(_STALL_WATCHDOG_ENV, "0")
    monitor = EventLoopLagMonitor()
    with caplog.at_level(logging.INFO, logger="src.observability.loop_lag"):
        monitor.start()
        try:
            assert monitor._watchdog_thread is None
            info_lines = [
                r.getMessage()
                for r in caplog.records
                if "watchdog enabled=" in r.getMessage()
            ]
        finally:
            await monitor.stop()

    assert len(info_lines) == 1
    message = info_lines[0]
    assert "watchdog enabled=False" in message
    assert f"stall_threshold_s={_STALL_THRESHOLD_S:.1f}" in message
    assert "all_thread_stacks=False" in message


# ------------------------------------------------ below-stack-threshold note


@pytest.mark.asyncio
async def test_sub_threshold_stall_warning_says_why_no_stack_was_taken(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A 3.2s stall in production logged no stack and nothing said why: the
    # warn line fires from 1s but the stack only samples past the (here, high)
    # stall threshold — the warning must now say so itself.
    monitor = EventLoopLagMonitor(stall_threshold_s=30.0, interval_s=0.05)
    with caplog.at_level(logging.WARNING, logger="src.observability.loop_lag"):
        monitor.start()
        try:
            await asyncio.sleep(0.02)
            _blocking_culprit_function(1.15)
            # Let the sampler task wake past the block and compute its lag
            # before stop() cancels it.
            await asyncio.sleep(0.1)
        finally:
            await monitor.stop()

    lag_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "event loop stalled" in r.message
    ]
    assert len(lag_warnings) == 1
    message = lag_warnings[0].message
    assert "低于栈阈值 30.0 s，未采集" in message
    # No stack may accompany a below-threshold stall.
    assert "thread stacks" not in message


@pytest.mark.asyncio
async def test_disabled_watchdog_stall_warning_says_watchdog_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(_STALL_WATCHDOG_ENV, "0")
    monitor = EventLoopLagMonitor(interval_s=0.05)
    with caplog.at_level(logging.WARNING, logger="src.observability.loop_lag"):
        monitor.start()
        try:
            await asyncio.sleep(0.02)
            _blocking_culprit_function(1.15)
            # Let the sampler task wake past the block and compute its lag
            # before stop() cancels it.
            await asyncio.sleep(0.1)
        finally:
            await monitor.stop()

    lag_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "event loop stalled" in r.message
    ]
    assert len(lag_warnings) == 1
    assert "看门狗未启用，未采集栈" in lag_warnings[0].message


@pytest.mark.asyncio
async def test_watchdog_logs_blocking_function_stack_once_per_stall_and_respects_cooldown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    monitor = EventLoopLagMonitor(
        stall_threshold_s=0.08,
        stall_cooldown_s=60.0,
        interval_s=0.02,
    )
    with caplog.at_level(logging.WARNING, logger="src.observability.loop_lag"):
        monitor.start()
        try:
            assert monitor._watchdog_thread is not None
            assert monitor._watchdog_thread.is_alive()
            assert monitor._watchdog_thread.daemon is True

            await asyncio.sleep(0.03)
            # Block the event loop synchronously beyond threshold (0.08s) for several polls.
            _blocking_culprit_function(0.22)

            stall_warnings = [
                r
                for r in caplog.records
                if r.levelno == logging.WARNING and "heartbeat stalled" in r.message
            ]
            # Same stall is sampled only once despite lasting multiple watchdog polls.
            assert len(stall_warnings) == 1
            assert "_blocking_culprit_function" in stall_warnings[0].message

            # Let heartbeat advance, then stall again within the 60s cooldown window:
            # must NOT sample a second time.
            await asyncio.sleep(0.05)
            _blocking_culprit_function(0.18)

            stall_warnings_after = [
                r
                for r in caplog.records
                if r.levelno == logging.WARNING and "heartbeat stalled" in r.message
            ]
            assert len(stall_warnings_after) == 1
        finally:
            await monitor.stop()

    assert monitor._watchdog_thread is None


@pytest.mark.asyncio
async def test_watchdog_truncates_stack_to_40_frames(
    caplog: pytest.LogCaptureFixture,
) -> None:
    monitor = EventLoopLagMonitor(
        stall_threshold_s=0.06,
        stall_cooldown_s=0.0,
        interval_s=0.02,
    )
    with caplog.at_level(logging.WARNING, logger="src.observability.loop_lag"):
        monitor.start()
        try:
            await asyncio.sleep(0.03)
            _deep_recursive_blocker(55, 0.16)
        finally:
            await monitor.stop()

    stall_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "heartbeat stalled" in r.message
    ]
    assert len(stall_warnings) == 1
    msg = stall_warnings[0].message
    assert "_blocking_culprit_function" in msg
    # All-thread capture truncates EACH thread's stack to _STALL_MAX_FRAMES.
    sections = msg.split("--- ")
    per_thread_frames = [
        [line for line in section.splitlines() if line.lstrip().startswith('File "')]
        for section in sections[1:]
    ]
    assert per_thread_frames, "expected at least one labelled thread section"
    for frames in per_thread_frames:
        assert len(frames) <= _STALL_MAX_FRAMES
    assert any("(event loop)" in section.splitlines()[0] for section in sections[1:])


@pytest.mark.asyncio
async def test_watchdog_disabled_via_env_does_not_start_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_STALL_WATCHDOG_ENV, "0")
    monitor = EventLoopLagMonitor(
        stall_threshold_s=0.05,
        stall_cooldown_s=1.0,
        interval_s=0.02,
    )
    monitor.start()
    try:
        assert monitor._watchdog_thread is None
    finally:
        await monitor.stop()


@pytest.mark.asyncio
async def test_watchdog_releases_frame_reference_so_blocked_locals_are_gc_collected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import gc
    import weakref

    class _LargeLocalObject:
        def __init__(self) -> None:
            self.payload = bytearray(1024 * 64)

    def _blocker_with_large_local(duration_s: float) -> weakref.ReferenceType[_LargeLocalObject]:
        local_obj = _LargeLocalObject()
        ref = weakref.ref(local_obj)
        time.sleep(duration_s)
        return ref

    monitor = EventLoopLagMonitor(
        stall_threshold_s=0.06,
        stall_cooldown_s=60.0,
        interval_s=0.02,
    )
    with caplog.at_level(logging.WARNING, logger="src.observability.loop_lag"):
        monitor.start()
        try:
            await asyncio.sleep(0.03)
            ref = _blocker_with_large_local(0.18)
            stall_warnings = [
                r
                for r in caplog.records
                if r.levelno == logging.WARNING and "heartbeat stalled" in r.message
            ]
            assert len(stall_warnings) == 1
            assert "_blocker_with_large_local" in stall_warnings[0].message

            gc.collect()
            assert ref() is None, "Watchdog must not retain frame references to local objects"
        finally:
            await monitor.stop()


@pytest.mark.asyncio
async def test_watchdog_names_every_thread_and_lists_heavy_tasks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The stall report carries all threads' stacks (named, loop thread
    labelled) and the in-flight heavy compute, so a stall can be attributed."""
    import threading

    from src.observability.heavy_tasks import heavy_task

    def _blocker(duration_s: float) -> None:
        time.sleep(duration_s)

    worker_name = "heavy-suspect-thread"

    def _heavy_worker() -> None:
        with heavy_task("publish.audit.match"):
            time.sleep(0.3)

    monitor = EventLoopLagMonitor(
        stall_threshold_s=0.06,
        stall_cooldown_s=60.0,
        interval_s=0.02,
    )
    with caplog.at_level(logging.WARNING, logger="src.observability.loop_lag"):
        monitor.start()
        worker = threading.Thread(target=_heavy_worker, name=worker_name, daemon=True)
        try:
            await asyncio.sleep(0.03)
            worker.start()
            _blocker(0.22)
            stall_warnings = [
                r
                for r in caplog.records
                if r.levelno == logging.WARNING and "heartbeat stalled" in r.message
            ]
            assert len(stall_warnings) == 1
            msg = stall_warnings[0].message
            assert worker_name in msg, "worker thread must appear by name"
            assert "(event loop)" in msg, "the loop thread must be labelled"
            assert "heavy compute in flight" in msg
            assert "publish.audit.match" in msg
        finally:
            worker.join(timeout=5.0)
            await monitor.stop()
