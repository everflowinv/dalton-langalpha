"""Unit tests for EventLoopLagMonitor stall stack watchdog thread."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from src.observability.loop_lag import (
    _STALL_MAX_FRAMES,
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
    file_frame_lines = [
        line for line in msg.splitlines() if line.lstrip().startswith('File "')
    ]
    assert len(file_frame_lines) <= _STALL_MAX_FRAMES


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
