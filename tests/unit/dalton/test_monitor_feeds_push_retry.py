"""Tests for Item 4: intraday monitor feed push retry & viewer staleness by last-received time."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

from scripts.dalton.monitor_feeds import common as feed_common
from scripts.dalton.monitor_feeds.run_feeds import FeedDaemon

BJ = ZoneInfo("Asia/Shanghai")


def test_failed_push_retries_on_next_tick_when_not_superseded_and_clears_on_success(
    tmp_path: Path,
) -> None:
    """When a push fails during trading hours, the next tick outside the trading window
    (which would otherwise skip) must retry pushing the unsuperseded pending snapshot."""
    pushed_calls: list[tuple[str, str, dict[str, Any], str | None]] = []
    fail_first = {"should_fail": True}

    def _fake_push(
        slug: str,
        data_key: str,
        payload: dict[str, Any],
        *,
        as_of: str | None = None,
        backend_url: str | None = None,
    ) -> dict[str, Any]:
        pushed_calls.append((slug, data_key, dict(payload), as_of))
        if fail_first["should_fail"]:
            fail_first["should_fail"] = False
            raise RuntimeError("502 Bad Gateway")
        return {"ok": True}

    def _fake_cn_collect() -> dict[str, Any]:
        return {
            "as_of": "2026-04-21 11:29:00",
            "as_of_iso": "2026-04-21T11:29:00+08:00",
            "trade_date": "2026-04-21",
            "stale": False,
            "val": 101.5,
        }

    push_spy = create_autospec(_fake_push, side_effect=_fake_push)
    cn_spy = create_autospec(_fake_cn_collect, side_effect=_fake_cn_collect)

    daemon = FeedDaemon(
        feeds=("cn",),
        backend_url="http://127.0.0.1:8000",
        interval_seconds=60.0,
        state_dir=tmp_path / "state",
        cn_collect_fn=cn_spy,
        push_fn=push_spy,
    )

    # Tick 1: inside morning trading session (11:29 BJ) -> push fails
    t1 = datetime(2026, 4, 21, 11, 29, 0, tzinfo=BJ)
    res1 = daemon.run_tick(now=t1)
    assert res1[0]["action"] == "push_failed"
    assert len(pushed_calls) == 1
    assert daemon.state["cn"].get("pending_push") is not None

    # Sleep computation during lunch break (12:00 BJ) must use short retry interval while pending_push exists
    t2 = datetime(2026, 4, 21, 12, 0, 10, tzinfo=BJ)
    sleep_s = daemon.compute_sleep_seconds_for("cn", now=t2)
    assert sleep_s <= 60.0

    # Tick 2: during lunch break (12:00 BJ, outside trading window) -> retries pending push without re-collecting
    res2 = daemon.run_tick(now=t2)
    assert res2[0]["action"] == "retried_push"
    assert cn_spy.call_count == 1  # did NOT re-collect during lunch break
    assert len(pushed_calls) == 2
    assert pushed_calls[1][2]["val"] == 101.5
    assert daemon.state["cn"].get("pending_push") is None

    # Tick 3: still lunch break -> now skips normally
    t3 = datetime(2026, 4, 21, 12, 5, 0, tzinfo=BJ)
    res3 = daemon.run_tick(now=t3)
    assert res3[0]["action"] == "skip"


def test_newer_snapshot_supersedes_pending_failed_push(tmp_path: Path) -> None:
    """When a push fails on tick 1 and tick 2 collects a newer snapshot, tick 2 pushes ONLY
    the newer snapshot (never re-pushing the superseded older snapshot) and clears pending_push."""
    pushed_as_of: list[str] = []
    call_idx = {"n": 0}

    def _fake_cn_collect() -> dict[str, Any]:
        call_idx["n"] += 1
        minute = 30 + call_idx["n"]
        return {
            "as_of": f"2026-04-21 10:{minute:02d}:00",
            "as_of_iso": f"2026-04-21T10:{minute:02d}:00+08:00",
            "trade_date": "2026-04-21",
            "stale": False,
            "seq": call_idx["n"],
        }

    def _fake_push(
        slug: str,
        data_key: str,
        payload: dict[str, Any],
        *,
        as_of: str | None = None,
        backend_url: str | None = None,
    ) -> dict[str, Any]:
        pushed_as_of.append(str(as_of))
        if payload.get("seq") == 1:
            raise RuntimeError("transient error on seq 1")
        return {"ok": True}

    daemon = FeedDaemon(
        feeds=("cn",),
        backend_url="http://127.0.0.1:8000",
        interval_seconds=60.0,
        state_dir=tmp_path / "state",
        cn_collect_fn=create_autospec(_fake_cn_collect, side_effect=_fake_cn_collect),
        push_fn=create_autospec(_fake_push, side_effect=_fake_push),
    )

    t1 = datetime(2026, 4, 21, 10, 31, 0, tzinfo=BJ)
    res1 = daemon.run_tick(now=t1)
    assert res1[0]["action"] == "push_failed"
    assert daemon.state["cn"].get("pending_push") is not None

    # Tick 2: still in trading window -> collects newer seq=2, supersedes seq=1, pushes seq=2 only
    t2 = datetime(2026, 4, 21, 10, 32, 0, tzinfo=BJ)
    res2 = daemon.run_tick(now=t2)
    assert res2[0]["action"] == "pushed"
    assert pushed_as_of == [
        "2026-04-21T10:31:00+08:00",
        "2026-04-21T10:32:00+08:00",
    ]
    assert daemon.state["cn"].get("pending_push") is None


def test_viewer_poller_js_judges_staleness_by_last_received_update_time() -> None:
    """build_grant_poller_js must track last received update time (body.updated_at / receive time)
    and emit a staleness warning referencing last received update time."""
    js = feed_common.build_grant_poller_js(
        data_key="intraday",
        on_data_fn="onData",
        on_error_fn="onErr",
        on_stale_fn="onStale",
        market="cn",
    )
    assert "updated_at" in js
    assert "lastReceived" in js
    assert "收到更新" in js
