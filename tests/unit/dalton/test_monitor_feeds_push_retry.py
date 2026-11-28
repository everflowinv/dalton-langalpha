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
        attempt_seq: int | None = None,
        is_stale: bool = False,
        error: str | None = None,
        last_attempt_at: str | None = None,
    ) -> dict[str, Any]:
        del attempt_seq, is_stale, error, last_attempt_at
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
        attempt_seq: int | None = None,
        is_stale: bool = False,
        error: str | None = None,
        last_attempt_at: str | None = None,
    ) -> dict[str, Any]:
        del attempt_seq, is_stale, error, last_attempt_at
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


def test_collect_exception_retries_newer_pending_push_without_overwriting_with_older_last_good(
    tmp_path: Path,
) -> None:
    """When tick 2 collects a newer (stale) snapshot whose push fails (saved in pending_push),
    and tick 3's collection raises an exception, tick 3 must retry the newer pending_push
    directly rather than overwriting it with older last_good_payload stamped with a fresh as_of_iso."""
    pushed_calls: list[tuple[dict[str, Any], str | None]] = []
    tick = {"n": 0}

    def _fake_cn_collect() -> dict[str, Any]:
        tick["n"] += 1
        if tick["n"] == 1:
            return {
                "as_of": "2026-04-21 10:25:00",
                "as_of_iso": "2026-04-21T10:25:00+08:00",
                "trade_date": "2026-04-21",
                "stale": False,
                "val": 100.0,
            }
        if tick["n"] == 2:
            # Newer snapshot that happened to be stale (so NOT saved to last_good_payload)
            return {
                "as_of": "2026-04-21 10:31:00",
                "as_of_iso": "2026-04-21T10:31:00+08:00",
                "trade_date": "2026-04-21",
                "stale": True,
                "stale_reason": "partial source lag",
                "val": 105.0,
            }
        raise RuntimeError("upstream network timeout")

    def _fake_push(
        slug: str,
        data_key: str,
        payload: dict[str, Any],
        *,
        as_of: str | None = None,
        backend_url: str | None = None,
        attempt_seq: int | None = None,
        is_stale: bool = False,
        error: str | None = None,
        last_attempt_at: str | None = None,
    ) -> dict[str, Any]:
        del attempt_seq, is_stale, error, last_attempt_at
        pushed_calls.append((dict(payload), as_of))
        if len(pushed_calls) == 2:
            raise RuntimeError("502 Bad Gateway")
        return {"ok": True}

    daemon = FeedDaemon(
        feeds=("cn",),
        backend_url="http://127.0.0.1:8000",
        interval_seconds=60.0,
        state_dir=tmp_path / "state",
        cn_collect_fn=create_autospec(_fake_cn_collect, side_effect=_fake_cn_collect),
        push_fn=create_autospec(_fake_push, side_effect=_fake_push),
    )

    # Tick 1 (10:25): clean snapshot succeeds -> stored in last_good_payload (val=100.0)
    res1 = daemon.run_tick(now=datetime(2026, 4, 21, 10, 25, 0, tzinfo=BJ))
    assert res1[0]["action"] == "pushed"

    # Tick 2 (10:31): newer stale snapshot (val=105.0) fails push -> saved in pending_push
    res2 = daemon.run_tick(now=datetime(2026, 4, 21, 10, 31, 0, tzinfo=BJ))
    assert res2[0]["action"] == "push_failed"
    assert daemon.state["cn"]["pending_push"]["payload"]["val"] == 105.0

    # Tick 3 (10:32): collection raises -> must retry the 10:31 pending_push (val=105.0),
    # NOT push the older 10:25 last_good_payload (val=100.0) with a fake 10:32 timestamp.
    res3 = daemon.run_tick(now=datetime(2026, 4, 21, 10, 32, 0, tzinfo=BJ))
    assert res3[0]["action"] == "retried_push"
    assert len(pushed_calls) == 3
    retried_payload, retried_as_of = pushed_calls[2]
    assert retried_payload["val"] == 105.0
    assert retried_as_of == "2026-04-21T10:31:00+08:00"
    assert daemon.state["cn"].get("pending_push") is None


def test_upsert_monitor_data_conditional_as_of_idempotent_replay_and_stale_rejection(
    monkeypatch: Any,
) -> None:
    """Server upsert_monitor_data accepts strictly newer as_of, keeps updated_at unchanged on
    identical snapshot replay, and rejects older snapshots with HTTP 409."""
    import asyncio
    from contextlib import asynccontextmanager
    from datetime import UTC, timedelta

    import pytest
    from fastapi import HTTPException

    from src.dalton.monitors import db as monitor_db

    store: dict[tuple[str, str], dict[str, Any]] = {}
    clock = {"now": datetime(2026, 4, 21, 2, 30, 0, tzinfo=UTC)}

    class _FakeCursor:
        def __init__(self) -> None:
            self._row: dict[str, Any] | None = None

        async def __aenter__(self) -> "_FakeCursor":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            sql_norm = " ".join(sql.split()).upper()
            if sql_norm.startswith("SELECT MONITOR_ID::TEXT"):
                mid, dkey = params
                existing = store.get((mid, dkey))
                self._row = dict(existing) if existing is not None else None
            elif sql_norm.startswith("INSERT INTO DALTON_MONITOR_DATA"):
                mid, dkey, canonical, ts, digest, aseq, is_st, last_att, last_err = params
                new_row = {
                    "monitor_id": mid,
                    "data_key": dkey,
                    "payload": canonical,
                    "as_of": ts,
                    "sha256": digest,
                    "updated_at": clock["now"],
                    "attempt_seq": aseq,
                    "is_stale": is_st,
                    "last_attempt_at": last_att,
                    "last_error": last_err,
                }
                store[(mid, dkey)] = new_row
                self._row = dict(new_row)
            elif sql_norm.startswith("UPDATE DALTON_MONITOR_DATA"):
                canonical, ts, digest, aseq, is_st, last_att, last_err, mid, dkey = params
                existing = store[(mid, dkey)]
                new_row = {
                    **existing,
                    "payload": canonical,
                    "as_of": ts,
                    "sha256": digest,
                    "updated_at": clock["now"],
                    "attempt_seq": aseq,
                    "is_stale": is_st,
                    "last_attempt_at": last_att,
                    "last_error": last_err,
                }
                store[(mid, dkey)] = new_row
                self._row = dict(new_row)
            else:
                raise AssertionError(f"Unexpected SQL: {sql}")

        async def fetchone(self) -> dict[str, Any] | None:
            return self._row

    class _FakeConn:
        def cursor(self, row_factory: Any = None) -> _FakeCursor:
            return _FakeCursor()

        @asynccontextmanager
        async def transaction(self):
            yield

    @asynccontextmanager
    async def _fake_get_db_connection():
        yield _FakeConn()

    monkeypatch.setattr(monitor_db, "get_db_connection", _fake_get_db_connection)

    mid = "11111111-1111-1111-1111-111111111111"
    t1 = datetime(2026, 4, 21, 2, 30, 0, tzinfo=UTC)
    t2 = datetime(2026, 4, 21, 2, 31, 0, tzinfo=UTC)
    t0 = datetime(2026, 4, 21, 2, 29, 0, tzinfo=UTC)

    # 1. First write succeeds and records updated_at = t1_now
    entry1 = asyncio.run(
        monitor_db.upsert_monitor_data(mid, "intraday", {"v": 1}, as_of=t1)
    )
    first_updated_at = entry1.updated_at

    # 2. Identical snapshot replayed 5 minutes later -> succeeds idempotently WITHOUT refreshing updated_at
    clock["now"] = clock["now"] + timedelta(minutes=5)
    entry_replay = asyncio.run(
        monitor_db.upsert_monitor_data(mid, "intraday", {"v": 1}, as_of=t1)
    )
    assert entry_replay.updated_at == first_updated_at
    assert entry_replay.as_of == t1

    # 3. Newer snapshot succeeds and refreshes updated_at
    entry2 = asyncio.run(
        monitor_db.upsert_monitor_data(mid, "intraday", {"v": 2}, as_of=t2)
    )
    assert entry2.as_of == t2
    assert entry2.updated_at == clock["now"]

    # 4. Older snapshot is rejected with 409 Conflict and does not overwrite stored row
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            monitor_db.upsert_monitor_data(mid, "intraday", {"v": 0}, as_of=t0)
        )
    assert exc_info.value.status_code == 409
    assert store[(mid, "intraday")]["as_of"] == t2
    assert store[(mid, "intraday")]["updated_at"] == clock["now"]


def test_calendar_error_preserves_newer_pending_push_and_409_clears_stale_pending(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """r4 Med #1: calendar_error must NOT overwrite/clear a newer pending_push with last_good_payload,
    and an HTTP 409 stale-snapshot conflict from the server must clear the superseded pending_push."""
    from scripts.dalton.monitor_feeds import run_feeds
    from scripts.dalton.monitor_feeds.common import MonitorPushHttpError

    pushed: list[dict[str, Any]] = []

    def _recording_push(
        slug: str,
        data_key: str,
        payload: Any,
        *,
        as_of: str | None = None,
        backend_url: str | None = None,
        attempt_seq: int | None = None,
        is_stale: bool = False,
        error: str | None = None,
        last_attempt_at: str | None = None,
    ) -> dict[str, Any]:
        del attempt_seq, is_stale, error, last_attempt_at
        pushed.append({"slug": slug, "data_key": data_key, "as_of": as_of, "payload": payload})
        return {"ok": True}

    daemon = FeedDaemon(
        feeds=("cn",),
        backend_url="http://127.0.0.1:8000",
        interval_seconds=60.0,
        state_dir=tmp_path / "state",
        push_fn=_recording_push,
    )
    daemon.state["cn"]["last_good_payload"] = {
        "as_of": "2026-04-21 10:00:00",
        "as_of_iso": "2026-04-21T10:00:00+08:00",
        "value": "old",
    }
    daemon.state["cn"]["pending_push"] = {
        "slug": "cn-intraday",
        "data_key": "snapshot",
        "payload": {
            "as_of": "2026-04-21 10:05:00",
            "as_of_iso": "2026-04-21T10:05:00+08:00",
            "value": "newer_pending",
        },
        "as_of": "2026-04-21T10:05:00+08:00",
        "trade_date": "2026-04-21",
        "in_window": True,
        "is_closed_td": False,
        "needs_post_close": False,
        "current_daily_sig": "",
        "attempts": 1,
    }

    monkeypatch.setattr(
        run_feeds,
        "market_session_state",
        lambda market, cur: {
            "trade_date": "2026-04-21",
            "is_trading_day": False,
            "in_trading_window": False,
            "status": "calendar_error",
            "label": "交易日历不可用",
            "calendar_error": "calendar file missing",
        },
    )

    cur = datetime(2026, 4, 21, 10, 10, 0, tzinfo=BJ)
    res = daemon._collect_and_push_one("cn", cur, force=False)
    assert res["action"] == "retried_push"
    assert res["calendar_error"] == "calendar file missing"
    assert len(pushed) == 1
    assert pushed[0]["as_of"] == "2026-04-21T10:05:00+08:00"
    assert pushed[0]["payload"]["value"] == "newer_pending"
    assert "pending_push" not in daemon.state["cn"]

    # Now test HTTP 409 stale-snapshot conflict during pending retry -> clears pending_push immediately
    def _conflict_push(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise MonitorPushHttpError(409, "写入 cn-intraday/snapshot 失败 (HTTP 409): stale snapshot")

    daemon._push = _conflict_push
    daemon.state["cn"]["pending_push"] = {
        "slug": "cn-intraday",
        "data_key": "snapshot",
        "payload": {
            "as_of": "2026-04-21 10:05:00",
            "as_of_iso": "2026-04-21T10:05:00+08:00",
            "value": "superseded_pending",
        },
        "as_of": "2026-04-21T10:05:00+08:00",
        "trade_date": "2026-04-21",
        "in_window": False,
        "is_closed_td": False,
        "needs_post_close": False,
        "current_daily_sig": "",
        "attempts": 1,
    }
    monkeypatch.setattr(
        run_feeds,
        "market_session_state",
        lambda market, cur: {
            "trade_date": "2026-04-21",
            "is_trading_day": True,
            "in_trading_window": False,
            "status": "closed",
            "label": "已收盘",
            "calendar_error": None,
        },
    )
    daemon.state["cn"]["post_close_written_date"] = "2026-04-21"

    res_409 = daemon._collect_and_push_one("cn", cur, force=False)
    assert res_409["action"] == "stale_pending_cleared"
    assert "pending_push" not in daemon.state["cn"]
