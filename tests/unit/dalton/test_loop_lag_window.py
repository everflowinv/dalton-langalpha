"""Unit tests for EventLoopLagMonitor 1.5s stall stack capture, sliding window, and heavy_task tags."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from src.dalton.observability.loop_lag_window import (
    STALL_THRESHOLD_ENV,
    LoopLagRingWindow,
    resolve_stall_threshold_s,
)
from src.dalton.observability.redis_diagnostics import (
    HEAVY_SERIALIZE_THRESHOLD_BYTES,
    dumps_with_heavy_task,
    encode_stream_frame_with_heavy_task,
)
from src.observability.heavy_tasks import heavy_task, running_tasks
from src.observability.loop_lag import (
    _STALL_THRESHOLD_S,
    _WARN_THRESHOLD_MS,
    EventLoopLagMonitor,
)


def test_default_thresholds_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _STALL_THRESHOLD_S == 1.5
    assert _WARN_THRESHOLD_MS == 300.0

    monkeypatch.delenv(STALL_THRESHOLD_ENV, raising=False)
    assert resolve_stall_threshold_s(None, _STALL_THRESHOLD_S) == 1.5

    monkeypatch.setenv(STALL_THRESHOLD_ENV, "5.0")
    assert resolve_stall_threshold_s(None, _STALL_THRESHOLD_S) == 5.0
    # Explicit constructor argument still wins over env.
    assert resolve_stall_threshold_s(0.2, _STALL_THRESHOLD_S) == 0.2

    monkeypatch.setenv(STALL_THRESHOLD_ENV, "not-a-number")
    assert resolve_stall_threshold_s(None, _STALL_THRESHOLD_S) == 1.5


def test_ring_window_5s_60s_and_over_300ms_counts() -> None:
    win = LoopLagRingWindow()
    t0 = 1000.0
    # Outside 60s window -> ignored
    win.record(999.0, now=t0 - 65.0)
    # Inside 60s window, outside 5s window
    win.record(420.0, now=t0 - 30.0)
    win.record(310.0, now=t0 - 10.0)
    win.record(50.0, now=t0 - 8.0)
    # Inside 5s window
    win.record(180.0, now=t0 - 2.0)
    win.record(350.0, now=t0 - 0.5)

    snap = win.snapshot(last_heartbeat=t0 - 0.25, now=t0)
    assert snap["max_5s_ms"] == pytest.approx(350.0, abs=0.1)
    assert snap["max_60s_ms"] == pytest.approx(420.0, abs=0.1)
    assert snap["over_300ms_count_60s"] == 3
    assert snap["last_heartbeat_age_ms"] == pytest.approx(250.0, abs=0.1)


@pytest.mark.asyncio
async def test_default_1_5s_stall_captures_stack_and_main_process_heavy_task(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Blocking the event loop past the default 1.5s threshold triggers stack capture
    and prints the main-process heavy_task tag in the watchdog warning."""
    monkeypatch.delenv(STALL_THRESHOLD_ENV, raising=False)
    monitor = EventLoopLagMonitor(stall_cooldown_s=60.0, interval_s=0.05)

    with caplog.at_level(logging.WARNING, logger="src.observability.loop_lag"):
        monitor.start()
        try:
            await asyncio.sleep(0.06)
            with heavy_task("redis.cache.serialize"):
                time.sleep(1.75)
            await asyncio.sleep(0.1)
            snap = monitor.snapshot_recent_lag()
        finally:
            await monitor.stop()

    stall_warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "heartbeat stalled" in r.getMessage()
    ]
    assert len(stall_warnings) == 1
    msg = stall_warnings[0]
    assert "(threshold 1.50 s)" in msg
    assert "heavy compute in flight:" in msg
    assert "redis.cache.serialize" in msg
    assert snap["max_5s_ms"] >= 1500.0
    assert snap["over_300ms_count_60s"] >= 1


def test_heavy_task_tags_for_large_cache_serialize_and_stream_encode() -> None:
    seen_tags: list[str] = []

    class _CapturingEncoder:
        """Minimal JSONEncoder-compatible hook to verify heavy_task is active during dumps."""

        def __new__(cls, *args, **kwargs):
            import json

            class _Enc(json.JSONEncoder):
                def default(self, obj):
                    seen_tags.extend(t.name for t in running_tasks())
                    return str(obj)

            return _Enc(*args, **kwargs)

    small_payload = {"k": "a" * 100, "marker": object()}
    dumps_with_heavy_task(small_payload, cls=_CapturingEncoder)
    assert "redis.cache.serialize" not in seen_tags

    large_payload = {"blob": "x" * (HEAVY_SERIALIZE_THRESHOLD_BYTES + 1024), "marker": object()}
    dumps_with_heavy_task(large_payload, cls=_CapturingEncoder)
    assert "redis.cache.serialize" in seen_tags

    large_frame = "id: 1\ndata: " + ("y" * HEAVY_SERIALIZE_THRESHOLD_BYTES) + "\n\n"
    encoded = encode_stream_frame_with_heavy_task(large_frame)
    assert encoded == large_frame.encode("utf-8")


def test_cjk_100k_chars_over_256kib_triggers_heavy_task() -> None:
    """~100,000 Chinese chars (~300 KiB UTF-8 > 256 KiB) must trigger heavy_task in both
    cache serialization and SSE frame encoding without pre-encoding."""
    from src.dalton.observability.redis_diagnostics import is_large_serialize_value

    cjk_text = "财" * 100_000  # 300,000 UTF-8 bytes > 262,144 (256 KiB)
    assert len(cjk_text.encode("utf-8")) > HEAVY_SERIALIZE_THRESHOLD_BYTES
    assert is_large_serialize_value(cjk_text) is True
    assert is_large_serialize_value({"summary": cjk_text}) is True

    seen_tags: list[str] = []

    class _CapturingEncoder:
        def __new__(cls, *args, **kwargs):
            import json

            class _Enc(json.JSONEncoder):
                def default(self, obj):
                    seen_tags.extend(t.name for t in running_tasks())
                    return str(obj)

            return _Enc(*args, **kwargs)

    dumps_with_heavy_task({"summary": cjk_text, "marker": object()}, cls=_CapturingEncoder)
    assert "redis.cache.serialize" in seen_tags

    encoded = encode_stream_frame_with_heavy_task(cjk_text)
    assert len(encoded) == 300_000
