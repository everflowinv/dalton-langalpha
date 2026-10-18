"""Time-based exponential backoff for Dalton preflight probes and transient errors (PK.18).

Hard rule (README §2 #2):
- Backoff is strictly time-based (2m -> 10m -> 30m -> 2h cap).
- Never cap retry count or call count; never auto-disable a Dalton task due to transient probe errors.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

BACKOFF_SCHEDULE_MINUTES: tuple[int, ...] = (2, 10, 30, 120)
MIN_FAILURE_BACKOFF_SECONDS: int = 3600
MAX_FAILURE_BACKOFF_SECONDS: int = 604800  # 7 days


def compute_failure_backoff_seconds(failure_count: int) -> int:
    """Compute time-based exponential backoff in seconds for automation execution failures (1h -> 7d cap).

    Never disables the automation; only delays ``next_run_at``.
    """
    exp = max(0, int(failure_count) - 1)
    wait_sec = MIN_FAILURE_BACKOFF_SECONDS * (2 ** min(exp, 10))
    return max(MIN_FAILURE_BACKOFF_SECONDS, min(MAX_FAILURE_BACKOFF_SECONDS, wait_sec))


def next_backoff_minutes(attempt: int) -> int:
    """Return backoff wait duration in minutes for 1-based attempt number (capped at 120m)."""
    idx = max(0, min(attempt - 1, len(BACKOFF_SCHEDULE_MINUTES) - 1))
    return BACKOFF_SCHEDULE_MINUTES[idx]


def is_in_backoff(
    watch_state: dict[str, Any],
    probe_name: str,
    now: Optional[datetime] = None,
) -> tuple[bool, Optional[str]]:
    """Return (True, not_before_iso) if ``probe_name`` is currently within its backoff window."""
    now_dt = now or datetime.now(timezone.utc)
    backoff_map = (watch_state or {}).get("probe_backoff") or {}
    entry = backoff_map.get(probe_name)
    if not isinstance(entry, dict):
        return False, None
    not_before_raw = entry.get("not_before")
    if not not_before_raw:
        return False, None
    try:
        nb = datetime.fromisoformat(str(not_before_raw).replace("Z", "+00:00"))
        if nb.tzinfo is None:
            nb = nb.replace(tzinfo=timezone.utc)
        if now_dt < nb:
            return True, nb.isoformat()
    except ValueError:
        return False, None
    return False, None


def record_probe_backoff(
    watch_state: dict[str, Any],
    probe_name: str,
    error: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Increment time-based backoff state for ``probe_name`` in ``watch_state``."""
    now_dt = now or datetime.now(timezone.utc)
    state = dict(watch_state or {})
    backoff_map = dict(state.get("probe_backoff") or {})
    prev = backoff_map.get(probe_name) or {}
    attempt = int(prev.get("attempt") or 0) + 1
    wait_mins = next_backoff_minutes(attempt)
    not_before = now_dt + timedelta(minutes=wait_mins)
    backoff_map[probe_name] = {
        "attempt": attempt,
        "wait_minutes": wait_mins,
        "not_before": not_before.isoformat(),
        "last_error": str(error)[:240],
        "updated_at": now_dt.isoformat(),
    }
    state["probe_backoff"] = backoff_map
    return state


def clear_probe_backoff(
    watch_state: dict[str, Any],
    probe_name: str,
) -> dict[str, Any]:
    """Clear backoff state for ``probe_name`` after a clean probe execution."""
    state = dict(watch_state or {})
    backoff_map = dict(state.get("probe_backoff") or {})
    if probe_name in backoff_map:
        backoff_map.pop(probe_name, None)
        state["probe_backoff"] = backoff_map
    return state
