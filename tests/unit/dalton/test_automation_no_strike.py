"""Unit tests locking the P0F.3 / G4.4 no-strike contract for dalton-* automations."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.dalton.autonomy.backoff import (
    MAX_FAILURE_BACKOFF_SECONDS,
    MIN_FAILURE_BACKOFF_SECONDS,
    compute_failure_backoff_seconds,
)
from src.server.database.automation_executions import _count_strike


def test_compute_failure_backoff_seconds_bounds():
    """Verify exponential failure backoff starts at 1h (3600s) and caps at 7d (604800s)."""
    assert MIN_FAILURE_BACKOFF_SECONDS == 3600
    assert MAX_FAILURE_BACKOFF_SECONDS == 604800

    assert compute_failure_backoff_seconds(0) == 3600
    assert compute_failure_backoff_seconds(1) == 3600
    assert compute_failure_backoff_seconds(2) == 7200
    assert compute_failure_backoff_seconds(3) == 14400
    assert compute_failure_backoff_seconds(4) == 28800
    assert compute_failure_backoff_seconds(10) == 604800
    assert compute_failure_backoff_seconds(50) == 604800


@pytest.mark.asyncio
async def test_dalton_automation_20_consecutive_failures_never_disables():
    """P0F.3 #1: dalton-* automation failing 20 times in a row stays active and backs off next_run_at."""
    cur = AsyncMock()
    for i in range(1, 21):
        cur.fetchone.return_value = {"failure_count": i, "disabled": False}
        count = await _count_strike(cur, "dalton-meta-watch-id", fuse=False)
        assert count == i

    sql, params = cur.execute.call_args[0]
    assert "name NOT LIKE 'dalton-%%'" in sql
    assert "WHEN name LIKE 'dalton-%%' AND trigger_type <> 'price'" in sql
    assert params["fuse"] is False
    assert params["reason"] == "max_failures"


@pytest.mark.asyncio
async def test_non_dalton_automation_disables_on_max_failures():
    """P0F.3 #2: non-dalton-* automation keeps upstream behavior (disables on 3rd failure)."""
    cur = AsyncMock()
    cur.fetchone.return_value = {"failure_count": 3, "disabled": True}
    count = await _count_strike(cur, "user-custom-auto-id", fuse=False)
    assert count == 3
    sql, params = cur.execute.call_args[0]
    assert "failure_count + 1 >= max_failures AND name NOT LIKE 'dalton-%%'" in sql
    assert params["fuse"] is False
    assert params["reason"] == "max_failures"


@pytest.mark.asyncio
async def test_fuse_immediately_disables_dalton_automation_with_provider_auth():
    """P0F.3 #3: fuse=True immediately disables dalton-* automation with disable_reason='provider_auth'."""
    cur = AsyncMock()
    cur.fetchone.return_value = {"failure_count": 1, "disabled": True}
    count = await _count_strike(cur, "dalton-meta-watch-id", fuse=True)
    assert count == 1
    sql, params = cur.execute.call_args[0]
    assert "(%(fuse)s OR (failure_count + 1 >= max_failures AND name NOT LIKE 'dalton-%%'))" in sql
    assert params["fuse"] is True
    assert params["reason"] == "provider_auth"
