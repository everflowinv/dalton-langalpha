"""Tests for Item 2: scheduler takeover abort monitor publish-failure registration & same-slot rerun."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import psycopg
import pytest

from src.dalton.data_jobs.container_runner import DataTaskDockerClient, build_path_map
from src.dalton.data_jobs.scheduler import claim_next_queued_run, recover_on_takeover
import src.dalton.monitors.service as monitor_service


def _make_autospec_db(cursor_side_effects: list[dict[str, Any]] | None = None):
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.closed = False
    steps = list(cursor_side_effects or [])
    state: dict[str, Any] = {
        "executed": [],
        "tx_entries": 0,
        "tx_commits": 0,
        "tx_rollbacks": 0,
    }

    @asynccontextmanager
    async def _transaction():
        state["tx_entries"] += 1
        try:
            yield
        except Exception:
            state["tx_rollbacks"] += 1
            raise
        else:
            state["tx_commits"] += 1

    conn.transaction.side_effect = _transaction

    @asynccontextmanager
    async def _cursor_ctx(*, binary: bool = False, row_factory: Any = None):
        cur = create_autospec(psycopg.AsyncCursor, instance=True)
        current_step: dict[str, Any] = {}

        async def _execute(
            query: Any,
            params: Any = None,
            *,
            prepare: bool | None = None,
            binary: bool | None = None,
        ):
            nonlocal current_step
            state["executed"].append((str(query), params))
            current_step = steps.pop(0) if steps else {}
            cur.rowcount = current_step.get("rowcount", 1)
            if current_step.get("error") is not None:
                raise current_step["error"]

        async def _fetchone():
            return current_step.get("fetchone", None)

        async def _fetchall():
            return current_step.get("fetchall", [])

        cur.execute.side_effect = _execute
        cur.fetchone.side_effect = _fetchone
        cur.fetchall.side_effect = _fetchall
        yield cur

    conn.cursor.side_effect = _cursor_ctx
    return conn, state


@pytest.mark.asyncio
async def test_takeover_abort_within_slot_registers_monitor_failure_and_schedules_one_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When takeover aborts a running container job whose slot is still valid, it registers
    the monitor publish failure and schedules one same-slot rerun."""
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 4, 21, 9, 10, 0, tzinfo=timezone.utc)
    slot_utc = datetime(2026, 4, 21, 9, 0, 0, tzinfo=timezone.utc)
    deadline_utc = slot_utc + timedelta(minutes=30)

    publish_spy = create_autospec(monitor_service.maybe_publish_data_job_monitor)
    monkeypatch.setattr(monitor_service, "maybe_publish_data_job_monitor", publish_spy)

    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.list_data_task_containers.return_value = [
        {"container_id": "cid-pm", "running": False}
    ]

    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(10)])
    conn, state = _make_autospec_db(
        [
            # 1. SELECT slot
            {
                "fetchone": {
                    "slot_id": 1,
                    "generation": 12,
                    "slot_state": "running",
                    "active_run_id": "run-pm-1",
                    "active_job_key": "pm-market-state",
                }
            },
            # 2. SELECT run + def row
            {
                "fetchone": {
                    "run_id": "run-pm-1",
                    "job_key": "pm-market-state",
                    "status": "running",
                    "container_id": "cid-pm",
                    "runner": "container",
                    "trigger_source": "cron",
                    "scheduled_slot_utc": slot_utc,
                    "deadline_utc": deadline_utc,
                    "triggered_by_user_id": None,
                    "enabled": True,
                    "paused_by_user": False,
                    "deleted_at": None,
                    "timeout_seconds": 1800,
                }
            },
            # 3. UPDATE dalton_data_task_runs SET status = 'aborted'
            {"rowcount": 1},
            # 4. UPDATE dalton_data_task_defs SET consecutive_failures = ...
            {"rowcount": 1},
            # 5. SELECT existing takeover retry for same slot -> none yet
            {"fetchone": None},
            # 6. INSERT queued rerun row -> returns new run_id
            {"rowcount": 1, "fetchone": {"run_id": "run-pm-1-retry"}},
            # 7. UPDATE dalton_data_task_slot SET slot_state = 'idle'
            {"rowcount": 1},
        ]
    )

    outcome = await recover_on_takeover(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        path_map=path_map,
        now_utc=now,
    )
    assert outcome == "aborted"

    # Must register monitor publish failure (same hook path as finalize_task_run)
    publish_spy.assert_awaited_once()
    call_kwargs = publish_spy.await_args.kwargs
    assert call_kwargs["job_key"] == "pm-market-state"
    assert call_kwargs["run_id"] == "run-pm-1"
    assert call_kwargs["terminal_status"] == "aborted"
    assert "scheduler_takeover_aborted" in str(call_kwargs["failure_reason"])

    # Must schedule exactly one same-slot rerun
    inserts = [
        (sql, params)
        for sql, params in state["executed"]
        if "INSERT INTO dalton_data_task_runs" in sql
    ]
    assert len(inserts) == 1
    _, insert_params = inserts[0]
    assert insert_params["job_key"] == "pm-market-state"
    assert insert_params["slot_utc"] == slot_utc
    assert insert_params["deadline_utc"] == deadline_utc
    assert insert_params["triggered_by_user_id"] == "system:takeover_retry"


@pytest.mark.asyncio
async def test_takeover_abort_does_not_schedule_second_rerun_for_same_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At most one takeover rerun per scheduled slot: a prior retry (or abort of the retry itself)
    must NOT enqueue another rerun."""
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 4, 21, 9, 15, 0, tzinfo=timezone.utc)
    slot_utc = datetime(2026, 4, 21, 9, 0, 0, tzinfo=timezone.utc)
    deadline_utc = slot_utc + timedelta(minutes=30)

    publish_spy = create_autospec(monitor_service.maybe_publish_data_job_monitor)
    monkeypatch.setattr(monitor_service, "maybe_publish_data_job_monitor", publish_spy)

    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.list_data_task_containers.return_value = []

    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(10)])
    conn, state = _make_autospec_db(
        [
            {
                "fetchone": {
                    "slot_id": 1,
                    "generation": 13,
                    "slot_state": "running",
                    "active_run_id": "run-pm-1-retry",
                    "active_job_key": "pm-market-state",
                }
            },
            {
                "fetchone": {
                    "run_id": "run-pm-1-retry",
                    "job_key": "pm-market-state",
                    "status": "running",
                    "container_id": "cid-pm-2",
                    "runner": "container",
                    "trigger_source": "manual",
                    "scheduled_slot_utc": slot_utc,
                    "deadline_utc": deadline_utc,
                    "triggered_by_user_id": "system:takeover_retry",
                    "enabled": True,
                    "paused_by_user": False,
                    "deleted_at": None,
                    "timeout_seconds": 1800,
                }
            },
            {"rowcount": 1},  # UPDATE runs -> aborted
            {"rowcount": 1},  # UPDATE slot -> idle
        ]
    )

    outcome = await recover_on_takeover(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        path_map=path_map,
        now_utc=now,
    )
    assert outcome == "aborted"
    assert not any("INSERT INTO dalton_data_task_runs" in sql for sql, _ in state["executed"])
    publish_spy.assert_awaited_once()
    assert "scheduler_takeover_aborted" in str(publish_spy.await_args.kwargs["failure_reason"])


@pytest.mark.asyncio
async def test_takeover_abort_when_slot_expired_updates_monitor_with_abort_details_and_no_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the aborted run's slot deadline has passed, do NOT schedule a rerun; update
    the monitor status and preserve abort details."""
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    slot_utc = datetime(2026, 4, 21, 9, 0, 0, tzinfo=timezone.utc)
    deadline_utc = slot_utc + timedelta(minutes=30)
    now_expired = deadline_utc + timedelta(minutes=5)

    publish_spy = create_autospec(monitor_service.maybe_publish_data_job_monitor)
    monkeypatch.setattr(monitor_service, "maybe_publish_data_job_monitor", publish_spy)

    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.list_data_task_containers.return_value = []

    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(10)])
    conn, state = _make_autospec_db(
        [
            {
                "fetchone": {
                    "slot_id": 1,
                    "generation": 14,
                    "slot_state": "running",
                    "active_run_id": "run-pm-expired",
                    "active_job_key": "pm-market-state",
                }
            },
            {
                "fetchone": {
                    "run_id": "run-pm-expired",
                    "job_key": "pm-market-state",
                    "status": "running",
                    "container_id": "cid-pm-expired",
                    "runner": "container",
                    "trigger_source": "cron",
                    "scheduled_slot_utc": slot_utc,
                    "deadline_utc": deadline_utc,
                    "triggered_by_user_id": None,
                    "enabled": True,
                    "paused_by_user": False,
                    "deleted_at": None,
                    "timeout_seconds": 1800,
                }
            },
            {"rowcount": 1},  # UPDATE runs -> aborted
            {"rowcount": 1},  # UPDATE defs -> consecutive_failures + 1
            {"rowcount": 1},  # UPDATE slot -> idle
        ]
    )

    outcome = await recover_on_takeover(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        path_map=path_map,
        now_utc=now_expired,
    )
    assert outcome == "aborted"
    assert not any("INSERT INTO dalton_data_task_runs" in sql for sql, _ in state["executed"])

    publish_spy.assert_awaited_once()
    kwargs = publish_spy.await_args.kwargs
    assert kwargs["job_key"] == "pm-market-state"
    assert kwargs["run_id"] == "run-pm-expired"
    assert kwargs["terminal_status"] == "aborted"
    reason = str(kwargs["failure_reason"])
    assert "scheduler_takeover_aborted" in reason
    assert "过期" in reason or "expired" in reason.lower()


@pytest.mark.asyncio
async def test_takeover_abort_does_not_rerun_when_db_already_has_slot_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if a cron run row is aborted in-window, an existing system:takeover_retry row
    for the same (job_key, scheduled_slot_utc) prevents enqueueing a second rerun."""
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 4, 21, 9, 12, 0, tzinfo=timezone.utc)
    slot_utc = datetime(2026, 4, 21, 9, 0, 0, tzinfo=timezone.utc)
    deadline_utc = slot_utc + timedelta(minutes=30)

    publish_spy = create_autospec(monitor_service.maybe_publish_data_job_monitor)
    monkeypatch.setattr(monitor_service, "maybe_publish_data_job_monitor", publish_spy)

    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.list_data_task_containers.return_value = []

    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(10)])
    conn, state = _make_autospec_db(
        [
            {
                "fetchone": {
                    "slot_id": 1,
                    "generation": 15,
                    "slot_state": "running",
                    "active_run_id": "run-pm-dup",
                    "active_job_key": "pm-market-state",
                }
            },
            {
                "fetchone": {
                    "run_id": "run-pm-dup",
                    "job_key": "pm-market-state",
                    "status": "running",
                    "container_id": "cid-pm-dup",
                    "runner": "container",
                    "trigger_source": "cron",
                    "scheduled_slot_utc": slot_utc,
                    "deadline_utc": deadline_utc,
                    "triggered_by_user_id": None,
                    "enabled": True,
                    "paused_by_user": False,
                    "deleted_at": None,
                }
            },
            {"rowcount": 1},  # UPDATE runs -> aborted
            {"rowcount": 1},  # UPDATE defs -> consecutive_failures + 1
            {"fetchone": {"run_id": "prior-retry-run-id"}},  # SELECT existing takeover retry
            {"rowcount": 1},  # UPDATE slot -> idle
        ]
    )

    outcome = await recover_on_takeover(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        path_map=path_map,
        now_utc=now,
    )
    assert outcome == "aborted"
    assert not any("INSERT INTO dalton_data_task_runs" in sql for sql, _ in state["executed"])
    publish_spy.assert_awaited_once()
    assert "补跑上限" in str(publish_spy.await_args.kwargs["failure_reason"])


@pytest.mark.asyncio
async def test_takeover_abort_and_rerun_enqueue_share_single_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aborting the run and enqueueing the takeover rerun MUST happen in the exact same DB transaction."""
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 4, 21, 9, 10, 0, tzinfo=timezone.utc)
    slot_utc = datetime(2026, 4, 21, 9, 0, 0, tzinfo=timezone.utc)
    deadline_utc = slot_utc + timedelta(minutes=30)

    publish_spy = create_autospec(monitor_service.maybe_publish_data_job_monitor)
    monkeypatch.setattr(monitor_service, "maybe_publish_data_job_monitor", publish_spy)

    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.list_data_task_containers.return_value = []

    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(10)])
    conn, state = _make_autospec_db(
        [
            {
                "fetchone": {
                    "slot_id": 1,
                    "generation": 20,
                    "slot_state": "running",
                    "active_run_id": "run-atomic-1",
                    "active_job_key": "pm-market-state",
                }
            },
            {
                "fetchone": {
                    "run_id": "run-atomic-1",
                    "job_key": "pm-market-state",
                    "status": "running",
                    "container_id": None,
                    "runner": "container",
                    "trigger_source": "cron",
                    "scheduled_slot_utc": slot_utc,
                    "deadline_utc": deadline_utc,
                    "triggered_by_user_id": None,
                    "enabled": True,
                    "paused_by_user": False,
                    "deleted_at": None,
                }
            },
            {"rowcount": 1},  # UPDATE runs -> aborted
            {"rowcount": 1},  # UPDATE defs -> consecutive_failures + 1
            {"fetchone": None},  # SELECT prior retry
            {"rowcount": 1, "fetchone": {"run_id": "run-atomic-retry"}},  # INSERT retry
            {"rowcount": 1},  # UPDATE slot -> idle
        ]
    )

    outcome = await recover_on_takeover(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        path_map=path_map,
        now_utc=now,
    )
    assert outcome == "aborted"
    # Exactly 2 transactions total: (1) abort + consecutive_failures + rerun INSERT, (2) slot -> idle.
    assert state["tx_commits"] == 2


@pytest.mark.asyncio
async def test_takeover_abort_distinguishes_blocked_by_other_queued_run_on_insert_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When INSERT ON CONFLICT DO NOTHING returns empty because another queued run (e.g. manual)
    occupies the per-job queued slot, monitor failure_reason must explicitly report the blocking run."""
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 4, 21, 9, 10, 0, tzinfo=timezone.utc)
    slot_utc = datetime(2026, 4, 21, 9, 0, 0, tzinfo=timezone.utc)
    deadline_utc = slot_utc + timedelta(minutes=30)

    publish_spy = create_autospec(monitor_service.maybe_publish_data_job_monitor)
    monkeypatch.setattr(monitor_service, "maybe_publish_data_job_monitor", publish_spy)

    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.list_data_task_containers.return_value = []

    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(10)])
    conn, _state = _make_autospec_db(
        [
            {
                "fetchone": {
                    "slot_id": 1,
                    "generation": 21,
                    "slot_state": "running",
                    "active_run_id": "run-blocked-1",
                    "active_job_key": "pm-market-state",
                }
            },
            {
                "fetchone": {
                    "run_id": "run-blocked-1",
                    "job_key": "pm-market-state",
                    "status": "running",
                    "container_id": None,
                    "runner": "container",
                    "trigger_source": "cron",
                    "scheduled_slot_utc": slot_utc,
                    "deadline_utc": deadline_utc,
                    "triggered_by_user_id": None,
                    "enabled": True,
                    "paused_by_user": False,
                    "deleted_at": None,
                }
            },
            {"rowcount": 1},  # UPDATE runs -> aborted
            {"rowcount": 1},  # UPDATE defs -> consecutive_failures + 1
            {"fetchone": None},  # SELECT prior takeover retry -> none
            {"rowcount": 0, "fetchone": None},  # INSERT ON CONFLICT DO NOTHING -> conflict!
            {
                "fetchone": {
                    "run_id": "run-manual-queued-99",
                    "trigger_source": "manual",
                    "triggered_by_user_id": "user-ops",
                    "scheduled_slot_utc": slot_utc,
                }
            },  # SELECT blocking queued row
            {"rowcount": 1},  # UPDATE slot -> idle
        ]
    )

    outcome = await recover_on_takeover(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        path_map=path_map,
        now_utc=now,
    )
    assert outcome == "aborted"
    publish_spy.assert_awaited_once()
    reason = str(publish_spy.await_args.kwargs["failure_reason"])
    assert "run-manual-queued-99" in reason
    assert "占槽" in reason


@pytest.mark.asyncio
async def test_takeover_replays_monitor_registration_when_already_aborted_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the process restarted after committing aborted+rerun but before monitor registration
    and slot idle reset, the next takeover sees status='aborted' (cleaned_terminal), does NOT
    modify runs/defs, and replays maybe_publish_data_job_monitor."""
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 4, 21, 9, 12, 0, tzinfo=timezone.utc)
    slot_utc = datetime(2026, 4, 21, 9, 0, 0, tzinfo=timezone.utc)
    deadline_utc = slot_utc + timedelta(minutes=30)

    publish_spy = create_autospec(monitor_service.maybe_publish_data_job_monitor)
    monkeypatch.setattr(monitor_service, "maybe_publish_data_job_monitor", publish_spy)

    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.list_data_task_containers.return_value = []

    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(10)])
    conn, state = _make_autospec_db(
        [
            {
                "fetchone": {
                    "slot_id": 1,
                    "generation": 22,
                    "slot_state": "running",
                    "active_run_id": "run-crashed-mid-takeover",
                    "active_job_key": "pm-market-state",
                }
            },
            {
                "fetchone": {
                    "run_id": "run-crashed-mid-takeover",
                    "job_key": "pm-market-state",
                    "status": "aborted",
                    "container_id": None,
                    "runner": "container",
                    "trigger_source": "cron",
                    "scheduled_slot_utc": slot_utc,
                    "deadline_utc": deadline_utc,
                    "triggered_by_user_id": None,
                    "enabled": True,
                    "paused_by_user": False,
                    "deleted_at": None,
                    "failure_reason": "scheduler_takeover_aborted",
                }
            },
            # Read-only lookup finds the retry already inserted in the prior atomic tx
            {"fetchone": {"run_id": "run-retry-already-queued"}},
            # UPDATE slot -> idle
            {"rowcount": 1},
        ]
    )

    outcome = await recover_on_takeover(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        path_map=path_map,
        now_utc=now,
    )
    assert outcome == "cleaned_terminal"
    assert not any("UPDATE dalton_data_task_runs" in sql for sql, _ in state["executed"])
    assert not any("UPDATE dalton_data_task_defs" in sql for sql, _ in state["executed"])
    publish_spy.assert_awaited_once()
    kwargs = publish_spy.await_args.kwargs
    assert kwargs["run_id"] == "run-crashed-mid-takeover"
    assert kwargs["terminal_status"] == "aborted"
    assert "run-retry-already-queued" in str(kwargs["failure_reason"])


@pytest.mark.asyncio
async def test_claim_next_queued_run_rejects_expired_takeover_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if a takeover retry was queued before expiry, claim_next_queued_run must refuse
    to start it once deadline_utc has passed, cancelling it and updating the bound monitor."""
    slot_utc = datetime(2026, 4, 21, 9, 0, 0, tzinfo=timezone.utc)
    deadline_utc = slot_utc + timedelta(minutes=30)
    now_past_deadline = deadline_utc + timedelta(seconds=10)

    publish_spy = create_autospec(monitor_service.maybe_publish_data_job_monitor)
    monkeypatch.setattr(monitor_service, "maybe_publish_data_job_monitor", publish_spy)

    conn, state = _make_autospec_db(
        [
            # 1. SELECT slot -> idle
            {
                "fetchone": {
                    "slot_id": 1,
                    "generation": 25,
                    "slot_state": "idle",
                    "active_run_id": None,
                    "active_job_key": None,
                }
            },
            # 2. expire_low_memory_runs SELECT
            {"fetchall": []},
            # 3. SELECT queued candidates -> expired takeover retry
            {
                "fetchall": [
                    {
                        "run_id": "run-expired-retry-1",
                        "job_key": "pm-market-state",
                        "trigger_source": "manual",
                        "triggered_by_user_id": "system:takeover_retry",
                        "scheduled_slot_utc": slot_utc,
                        "deadline_utc": deadline_utc,
                        "low_mem_since_utc": None,
                        "enabled": True,
                        "paused_by_user": False,
                        "runner": "container",
                        "command_argv": ["python3", "run.py"],
                        "workdir": "/tmp",
                        "timeout_seconds": 1800,
                        "no_output_timeout_seconds": 600,
                        "required_secrets": [],
                        "notify_enabled": True,
                        "timezone": "Asia/Shanghai",
                        "deleted_at": None,
                    }
                ]
            },
            # 4. UPDATE dalton_data_task_runs SET status = 'cancelled', failure_reason = 'takeover_retry_expired'
            {"rowcount": 1},
        ]
    )

    claimed = await claim_next_queued_run(
        conn,
        now_utc=now_past_deadline,
        available_memory_mb=4096,
    )
    assert claimed is None
    cancel_sqls = [sql for sql, _ in state["executed"] if "takeover_retry_expired" in sql]
    assert len(cancel_sqls) == 1
    # Slot must NOT have been claimed
    assert not any("slot_state     = 'running'" in sql for sql, _ in state["executed"])
    publish_spy.assert_awaited_once()
    assert publish_spy.await_args.kwargs["run_id"] == "run-expired-retry-1"
    assert "takeover_retry_expired" in str(publish_spy.await_args.kwargs["failure_reason"])


@pytest.mark.asyncio
async def test_takeover_enqueue_uses_db_clock_when_now_utc_is_none_and_handles_lock_wait_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Med #2: in production (now_utc=None), takeover retry INSERT must enforce `NOW() < %(deadline_utc)s`
    in SQL; if waiting on a lock crosses the deadline (0 rows inserted and no blocking queued run),
    it reports slot expired instead of retry_exhausted."""
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    slot_utc = datetime.now(timezone.utc) - timedelta(minutes=5)
    deadline_utc = datetime.now(timezone.utc) + timedelta(minutes=25)

    publish_spy = create_autospec(monitor_service.maybe_publish_data_job_monitor)
    monkeypatch.setattr(monitor_service, "maybe_publish_data_job_monitor", publish_spy)

    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.list_data_task_containers.return_value = []

    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(10)])
    conn, state = _make_autospec_db(
        [
            {
                "fetchone": {
                    "slot_id": 1,
                    "generation": 30,
                    "slot_state": "running",
                    "active_run_id": "run-lock-wait-exp",
                    "active_job_key": "pm-market-state",
                }
            },
            {
                "fetchone": {
                    "run_id": "run-lock-wait-exp",
                    "job_key": "pm-market-state",
                    "status": "running",
                    "container_id": None,
                    "runner": "container",
                    "trigger_source": "cron",
                    "scheduled_slot_utc": slot_utc,
                    "deadline_utc": deadline_utc,
                    "triggered_by_user_id": None,
                    "enabled": True,
                    "paused_by_user": False,
                    "deleted_at": None,
                }
            },
            {"rowcount": 1},  # UPDATE runs -> aborted
            {"rowcount": 1},  # UPDATE defs -> consecutive_failures + 1
            {"fetchone": None},  # SELECT prior takeover retry -> none
            {"rowcount": 0, "fetchone": None},  # INSERT ... WHERE NOW() < deadline_utc -> filtered out!
            {"fetchone": None},  # SELECT blocking queued row -> none (filtered by DB clock!)
            {"rowcount": 1},  # UPDATE slot -> idle
        ]
    )

    outcome = await recover_on_takeover(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        path_map=path_map,
        now_utc=None,
    )
    assert outcome == "aborted"
    insert_sqls = [sql for sql, _ in state["executed"] if "INSERT INTO dalton_data_task_runs" in sql]
    assert len(insert_sqls) == 1
    assert "WHERE NOW() < %(deadline_utc)s" in insert_sqls[0]
    publish_spy.assert_awaited_once()
    assert "已过期" in str(publish_spy.await_args.kwargs["failure_reason"])


@pytest.mark.asyncio
async def test_claim_second_expiry_branch_also_publishes_monitor_and_persists_failed_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Med #2: when takeover retry expires on the second atomic UPDATE check inside
    claim_next_queued_run, it shares the cancellation+monitor-publish helper, and if monitor
    publish fails, the pending record survives slot reset and replays on the next tick."""
    from src.dalton.data_jobs import scheduler as sched_mod

    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    monkeypatch.setattr(sched_mod, "build_path_map", lambda: path_map)

    slot_utc = datetime(2026, 4, 21, 9, 0, 0, tzinfo=timezone.utc)
    deadline_utc = slot_utc + timedelta(minutes=30)

    # First call to maybe_publish fails, second (replay) succeeds.
    publish_calls: list[dict[str, Any]] = []
    should_fail = {"flag": True}

    async def _flaky_publish(**kwargs: Any) -> None:
        publish_calls.append(dict(kwargs))
        if should_fail["flag"]:
            raise RuntimeError("monitor db briefly unavailable")

    monkeypatch.setattr(monitor_service, "maybe_publish_data_job_monitor", _flaky_publish)

    # Simulate ref_utc + pre-check passing (< deadline) while atomic claim UPDATE returns rowcount=0
    # because the clock crossed deadline_utc during lock wait.
    class _TickingDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            val = clock_ticks.pop(0) if clock_ticks else deadline_utc + timedelta(seconds=5)
            return val if tz is None else val.astimezone(tz)

    slot_utc = _TickingDatetime(2026, 4, 21, 9, 0, 0, tzinfo=timezone.utc)
    deadline_utc = _TickingDatetime(2026, 4, 21, 9, 30, 0, tzinfo=timezone.utc)
    clock_ticks = [
        _TickingDatetime(2026, 4, 21, 9, 29, 57, tzinfo=timezone.utc),  # ref_utc
        _TickingDatetime(2026, 4, 21, 9, 29, 58, tzinfo=timezone.utc),  # claim_check_now (passes!)
        _TickingDatetime(2026, 4, 21, 9, 29, 59, tzinfo=timezone.utc),  # claim_now_utc
        _TickingDatetime(2026, 4, 21, 9, 30, 1, tzinfo=timezone.utc),   # expired_at_claim -> True!
    ]

    monkeypatch.setattr(sched_mod, "datetime", _TickingDatetime)

    conn, state = _make_autospec_db(
        [
            {"fetchone": {"slot_id": 1, "generation": 40, "slot_state": "idle", "active_run_id": None, "active_job_key": None}},
            {"fetchall": []},
            {
                "fetchall": [
                    {
                        "run_id": "run-second-branch-exp",
                        "job_key": "pm-market-state",
                        "trigger_source": "manual",
                        "triggered_by_user_id": "system:takeover_retry",
                        "scheduled_slot_utc": slot_utc,
                        "deadline_utc": deadline_utc,
                        "low_mem_since_utc": None,
                        "enabled": True,
                        "paused_by_user": False,
                        "runner": "container",
                        "command_argv": ["python3", "run.py"],
                        "workdir": "/tmp",
                        "timeout_seconds": 1800,
                        "no_output_timeout_seconds": 600,
                        "required_secrets": [],
                        "notify_enabled": True,
                        "timezone": "Asia/Shanghai",
                        "deleted_at": None,
                    }
                ]
            },
            # Atomic claim transaction:
            {},  # maintenance gate: shared advisory xact lock
            {"rowcount": 1, "fetchone": {"generation": 41}},  # UPDATE slot -> running
            {"fetchone": {"enabled": True, "paused_by_user": False, "deleted_at": None}},  # SELECT def FOR SHARE
            {"rowcount": 0},  # UPDATE runs -> 0 rows (NOW() >= deadline_utc!) -> _ClaimRollback("takeover_retry_expired")
            # Shared expiry helper UPDATE runs -> cancelled:
            {"rowcount": 1},
        ]
    )

    claimed = await claim_next_queued_run(conn, now_utc=None, available_memory_mb=4096)
    assert claimed is None
    assert len(publish_calls) == 1
    assert publish_calls[0]["run_id"] == "run-second-branch-exp"
    assert "takeover_retry_expired" in publish_calls[0]["failure_reason"]

    # No local JSON pending_monitor_publishes.json file is created (retired in favor of PG lease queue).
    pending_file = path_map.backend_var_dir / "pending_monitor_publishes.json"
    assert not pending_file.exists()

    # On next tick / restart, replay_pending_monitor_publishes claims due rows from PostgreSQL and replays.
    should_fail["flag"] = False
    replay_conn, _replay_state = _make_autospec_db(
        [
            {
                "fetchall": [
                    {
                        "run_id": "run-second-branch-exp",
                        "job_key": "pm-market-state",
                        "status": "cancelled",
                        "failure_reason": "takeover_retry_expired",
                        "scheduled_slot_utc": slot_utc,
                        "completed_at": deadline_utc,
                        "monitor_publish_status": "pending",
                        "monitor_publish_attempts": 0,
                        "monitor_publish_claim_epoch": 0,
                        "monitor_publish_detail": {},
                    }
                ]
            },
            {"fetchone": {"monitor_publish_attempts": 1, "monitor_publish_claim_epoch": 1}},
            {"rowcount": 1},  # fenced settle_monitor_publish_run_tx
        ]
    )
    replayed = await sched_mod.replay_pending_monitor_publishes(
        replay_conn, path_map=path_map
    )
    assert replayed == 1
    assert len(publish_calls) == 2


@pytest.mark.asyncio
async def test_deferred_takeover_retry_persisted_when_blocked_and_enqueued_once_slot_frees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Med #3: when takeover retry is blocked by an existing queued run for the same job,
    the intent is persisted durably and enqueued by flush_pending_takeover_retries once the
    queue slot frees up before deadline_utc (deduplicated by original scheduled_slot_utc)."""
    from src.dalton.data_jobs.scheduler import flush_pending_takeover_retries

    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 4, 21, 9, 10, 0, tzinfo=timezone.utc)
    slot_utc = datetime(2026, 4, 21, 9, 0, 0, tzinfo=timezone.utc)
    deadline_utc = slot_utc + timedelta(minutes=30)

    publish_spy = create_autospec(monitor_service.maybe_publish_data_job_monitor)
    monkeypatch.setattr(monitor_service, "maybe_publish_data_job_monitor", publish_spy)

    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.list_data_task_containers.return_value = []

    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(10)])
    conn, _ = _make_autospec_db(
        [
            {"fetchone": {"slot_id": 1, "generation": 50, "slot_state": "running", "active_run_id": "run-blocked-defer", "active_job_key": "pm-market-state"}},
            {"fetchone": {"run_id": "run-blocked-defer", "job_key": "pm-market-state", "status": "running", "container_id": None, "runner": "container", "trigger_source": "cron", "scheduled_slot_utc": slot_utc, "deadline_utc": deadline_utc, "triggered_by_user_id": None, "enabled": True, "paused_by_user": False, "deleted_at": None}},
            {"rowcount": 1},  # UPDATE runs -> aborted
            {"rowcount": 1},  # UPDATE defs -> consecutive_failures + 1
            {"fetchone": None},  # SELECT prior takeover retry -> none
            {"rowcount": 0, "fetchone": None},  # INSERT -> blocked!
            {"fetchone": {"run_id": "run-overlap-slot-2", "trigger_source": "cron", "triggered_by_user_id": None, "scheduled_slot_utc": slot_utc + timedelta(minutes=5)}},
            {"rowcount": 1},  # UPDATE slot -> idle
        ]
    )

    outcome = await recover_on_takeover(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        path_map=path_map,
        now_utc=now,
    )
    assert outcome == "aborted"
    intent_file = path_map.backend_var_dir / "pending_takeover_retries.json"
    assert intent_file.is_file()
    assert "pm-market-state" in intent_file.read_text(encoding="utf-8")

    # Once the overlapping queued run finishes/claims and queue slot is free before deadline_utc,
    # flush_pending_takeover_retries enqueues the original slot's retry and clears the intent.
    flush_conn, flush_state = _make_autospec_db(
        [
            {"fetchone": {"enabled": True, "paused_by_user": False, "deleted_at": None}},  # SELECT def
            {"fetchone": None},  # SELECT existing same-slot takeover_retry -> none
            {"rowcount": 1, "fetchone": {"run_id": "run-deferred-retry-1"}},  # INSERT retry succeeds!
        ]
    )
    enqueued = await flush_pending_takeover_retries(
        flush_conn,
        path_map=path_map,
        now_utc=now + timedelta(minutes=5),
    )
    assert enqueued == 1
    inserts = [
        params
        for sql, params in flush_state["executed"]
        if "INSERT INTO dalton_data_task_runs" in sql
    ]
    assert len(inserts) == 1
    assert inserts[0]["job_key"] == "pm-market-state"
    assert inserts[0]["slot_utc"] == slot_utc
    assert inserts[0]["deadline_utc"] == deadline_utc
    assert inserts[0]["triggered_by_user_id"] == "system:takeover_retry"
    assert "pm-market-state" not in intent_file.read_text(encoding="utf-8")

