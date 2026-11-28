"""Lock-free sliding window and threshold helpers for ``EventLoopLagMonitor``.

Diagnostics-only per-worker context (never consulted by request paths for business
state). Provides O(1) append and read-only snapshots of 5s / 60s event-loop lag
windows alongside dynamic ``LOOP_LAG_STALL_THRESHOLD_S`` override resolution.
"""

from __future__ import annotations

from collections import deque
import os
import time
from typing import Any

STALL_THRESHOLD_ENV = "LOOP_LAG_STALL_THRESHOLD_S"
OVER_THRESHOLD_MS = 300.0
_WINDOW_SHORT_S = 5.0
_WINDOW_LONG_S = 60.0


def resolve_stall_threshold_s(
    explicit: float | None,
    default_s: float = 1.5,
) -> float:
    """Return effective stall-stack threshold in seconds.

    Priority: explicit constructor override > valid positive
    ``LOOP_LAG_STALL_THRESHOLD_S`` env var > ``default_s``.
    """
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get(STALL_THRESHOLD_ENV, "").strip()
    if raw:
        try:
            parsed = float(raw)
            if parsed > 0.0:
                return parsed
        except ValueError:
            pass
    return float(default_s)


class LoopLagRingWindow:
    """Lock-free bounded ring buffer storing ``(monotonic_ts, lag_ms)`` samples."""

    def __init__(self, *, maxlen: int = 4096) -> None:
        self._samples: deque[tuple[float, float]] = deque(maxlen=max(16, int(maxlen)))

    def record(self, lag_ms: float, *, now: float | None = None) -> None:
        ts = time.monotonic() if now is None else float(now)
        val = max(0.0, float(lag_ms))
        self._samples.append((ts, val))

    def snapshot(
        self,
        last_heartbeat: float,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = time.monotonic() if now is None else float(now)
        cutoff_5s = ts - _WINDOW_SHORT_S
        cutoff_60s = ts - _WINDOW_LONG_S
        max_5s = 0.0
        max_60s = 0.0
        over_300ms_60s = 0
        # Snapshot tuple copy without locking (deque iteration/copy is atomic in CPython
        # and tolerates concurrent single-writer append).
        for sample_ts, lag_ms in tuple(self._samples):
            if sample_ts < cutoff_60s:
                continue
            if lag_ms > max_60s:
                max_60s = lag_ms
            if lag_ms >= OVER_THRESHOLD_MS:
                over_300ms_60s += 1
            if sample_ts >= cutoff_5s and lag_ms > max_5s:
                max_5s = lag_ms
        hb_age_ms = (
            max(0.0, (ts - float(last_heartbeat)) * 1000.0)
            if last_heartbeat and last_heartbeat > 0.0
            else 0.0
        )
        return {
            "max_5s_ms": round(max_5s, 2),
            "max_60s_ms": round(max_60s, 2),
            "over_300ms_count_60s": over_300ms_60s,
            "last_heartbeat_age_ms": round(hb_age_ms, 2),
        }
