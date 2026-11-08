"""Unit tests for OC-1 Phase 0 PR 0-1 (Dalton Data Task Layer Foundation)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any
from unittest.mock import create_autospec

import aiodocker
from aiodocker.containers import DockerContainers
from aiodocker.exceptions import DockerError
import psycopg
import pytest

from src.dalton.data_jobs.container_runner import (
    CONTAINER_MEMORY_BYTES,
    ContainerInspectionOutcome,
    DataTaskDockerClient,
    build_container_create_config,
    build_path_map,
    write_run_secrets_dir,
)
from src.dalton.data_jobs.evaluators import (
    EVALUATOR_REGISTRY,
    T5_JOB_KEY,
    T18_JOB_KEY,
    TaskEvaluationContext,
    evaluate_task_run,
    get_evaluator,
    previous_weekday,
)
from src.dalton.data_jobs.manifest import (
    load_data_task_manifest,
    sync_manifest_to_db,
)
from src.dalton.data_jobs.runner_wrapper import (
    DEFAULT_TMPFS_OUTBOX_DIR,
    EXIT_CODE_NO_OUTPUT_TIMEOUT,
    EXIT_CODE_TOTAL_TIMEOUT,
     TASK_NOTIFY_PAYLOAD_SOURCES,
    TIMEOUT_EXIT_CODE,
    SecretRedactor,
    build_child_env,
    load_secrets_from_dir,
    persist_redacted_outbox,
    run_wrapped_command,
)
from src.dalton.data_jobs.scheduler import (
    ClaimedTaskRun,
    SchedulerLockLostError,
    check_scheduler_lock,
    claim_next_queued_run,
    enqueue_due_cron_tasks,
    execute_claimed_container_run,
    expire_low_memory_runs,
    finalize_task_run,
    is_data_jobs_scheduler_enabled,
    monitor_container_until_exit,
    recover_on_takeover,
    release_scheduler_lock,
    resolve_task_vault_secrets,
    run_data_task_scheduler_loop,
    start_claimed_container_run,
    try_scheduler_lock,
)
from src.dalton.migrations.versions import d029_data_tasks
from src.dalton.settings import (
    get_dalton_config,
    get_data_task_min_free_mem_mb,
    is_data_jobs_enabled,
)

BASELINE_DOC_PATH = Path(__file__).resolve().parent / "fixtures" / "oc1_task_baseline.md"


def _make_autospec_db(cursor_side_effects: list[dict] | None = None):
    """Create autospecced psycopg AsyncConnection and AsyncCursor with transaction tracking."""
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.closed = False
    steps = list(cursor_side_effects or [])
    state = {
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
        current_step: dict = {}

        async def _execute(
            query: Any,
            params: Any = None,
            *,
            prepare: bool | None = None,
            binary: bool | None = None,
        ):
            nonlocal current_step
            sql_text = str(query)
            state["executed"].append((sql_text, params))
            current_step = steps.pop(0) if steps else {}
            cur.rowcount = current_step.get("rowcount", 1)
            if "error" in current_step and current_step["error"] is not None:
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


# ---------------------------------------------------------------------------
# 1. Migration d029_data_tasks tests
# ---------------------------------------------------------------------------


def test_d029_migration_metadata_and_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    assert d029_data_tasks.revision == "d029"
    assert d029_data_tasks.down_revision == "d028"

    executed_sql: list[str] = []
    monkeypatch.setattr(
        d029_data_tasks.op,
        "execute",
        create_autospec(d029_data_tasks.op.execute, side_effect=lambda sql: executed_sql.append(str(sql))),
    )

    d029_data_tasks.upgrade()
    combined_upgrade = "\n".join(executed_sql)
    assert "CREATE TABLE IF NOT EXISTS dalton_data_task_defs" in combined_upgrade
    assert "CREATE TABLE IF NOT EXISTS dalton_data_task_slot" in combined_upgrade
    assert "CREATE TABLE IF NOT EXISTS dalton_data_task_runs" in combined_upgrade
    assert "uq_dalton_data_task_runs_cron_slot" in combined_upgrade
    assert "uq_dalton_data_task_runs_one_queued_per_job" in combined_upgrade
    assert "config_version" in combined_upgrade
    assert "audit_history" in combined_upgrade
    assert "'skipped_superseded'" in combined_upgrade
    assert "'skipped_low_memory'" in combined_upgrade
    assert "'pending_dispatch'" in combined_upgrade

    executed_sql.clear()
    d029_data_tasks.downgrade()
    combined_downgrade = "\n".join(executed_sql)
    assert "DROP TABLE IF EXISTS dalton_data_task_runs" in combined_downgrade
    assert "DROP TABLE IF EXISTS dalton_data_task_slot" in combined_downgrade
    assert "DROP TABLE IF EXISTS dalton_data_task_defs" in combined_downgrade


# ---------------------------------------------------------------------------
# 2. Manifest vs TASK_BASELINE.md & §1.1 tests
# ---------------------------------------------------------------------------


def _parse_task_baseline_markdown() -> dict[str, dict[str, object]]:
    """Parse the 19-row baseline table directly from .agy/oc1/TASK_BASELINE.md (including multi-line T10)."""
    text = BASELINE_DOC_PATH.read_text(encoding="utf-8")
    table_body = text.split("|---|---|", 1)[1].split("\n\n注：", 1)[0].strip()

    # Each row starts with `| T<digits> |` at the start of a line
    raw_rows: list[str] = []
    for line in table_body.splitlines():
        if line.startswith("| T") and " | `" in line:
            raw_rows.append(line)
        elif raw_rows:
            raw_rows[-1] += "\n" + line

    rows: dict[str, dict[str, object]] = {}
    for raw_row in raw_rows:
        parts = [cell.strip() for cell in raw_row.strip().strip("|").split("|")]
        assert len(parts) == 8, f"Unexpected baseline row format: {raw_row}"
        code = parts[0]
        job_name = parts[1].strip("`")
        cron_expr = parts[2].strip("`")
        tz_name = parts[3]
        timeout_raw = parts[4].strip()
        timeout_sec = None if timeout_raw == "—" else int(timeout_raw)
        no_output_raw = parts[5].strip()
        no_output_sec = None if no_output_raw == "—" else int(no_output_raw)
        cwd = parts[6].strip("`")
        cmd = parts[7].strip("`")
        rows[code] = {
            "task_code": code,
            "job_key": job_name,
            "cron": cron_expr,
            "timezone": tz_name,
            "timeout_seconds": timeout_sec,
            "no_output_timeout_seconds": no_output_sec,
            "cwd": cwd,
            "command": cmd,
        }
    return rows


def test_manifest_matches_baseline_and_approved_changes() -> None:
    manifest = load_data_task_manifest()
    assert len(manifest.tasks) == 19

    baseline_rows = _parse_task_baseline_markdown()
    assert len(baseline_rows) == 19

    by_code = manifest.by_task_code()
    assert set(by_code.keys()) == set(baseline_rows.keys())

    for code, base in baseline_rows.items():
        entry = by_code[code]
        assert entry.enabled is False, f"{code} must be enabled=False in PR 0-1"
        assert entry.job_key == base["job_key"]
        assert entry.timeout_seconds == base["timeout_seconds"]
        assert entry.no_output_timeout_seconds == base["no_output_timeout_seconds"]
        assert entry.cwd == base["cwd"]
        assert entry.command == base["command"]
        assert "-lc" not in entry.command_argv, f"{code} must not use login shell (-lc)"

        # Runner check: T1 and T29 are host, all others are container
        if code in {"T1", "T29"}:
            assert entry.runner == "host"
        else:
            assert entry.runner == "container"

        # Cron & timezone check: active values match baseline (T28 cutover stored separately)
        if code == "T28":
            # Active schedule in shadow run matches baseline; post-cutover stored separately
            assert entry.cron == base["cron"] == "40 9 * * 1-5"
            assert entry.timezone == base["timezone"] == "America/New_York"
            assert entry.cutover_cron == "40 21 * * 1-5"
            assert entry.cutover_timezone == "Asia/Hong_Kong"
        elif code == "T44":
            # Approved change #2: T44 fills missing timezone as America/New_York
            assert entry.cron == base["cron"] == "40 21 * * 1-5"
            assert entry.timezone == "America/New_York"
        else:
            assert entry.cron == base["cron"], f"Cron mismatch on {code}"
            assert entry.timezone == base["timezone"], f"Timezone mismatch on {code}"

    # Verify secret_names declarations per Design §4.4.2
    assert list(by_code["T13"].secret_names) == [
        "TENCENT_COS_SECRET_ID",
        "TENCENT_COS_SECRET_KEY",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
    ]
    assert list(by_code["T25"].secret_names) == [
        "TENCENT_COS_SECRET_ID",
        "TENCENT_COS_SECRET_KEY",
    ]
    assert list(by_code["T52"].secret_names) == ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]
    assert list(by_code["T54"].secret_names) == ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]


@pytest.mark.asyncio
async def test_sync_manifest_to_db_restores_t25_redacted_python() -> None:
    manifest = load_data_task_manifest()
    by_code = manifest.by_task_code()
    t25 = by_code["T25"]
    assert "[REDACTED]" in t25.command
    resolved_argv = t25.resolved_command_argv()
    assert resolved_argv[0] == "sh"
    assert resolved_argv[1] == "-c"
    assert "[REDACTED]" not in resolved_argv[2]
    assert "/app/.venv/bin/python scripts/sentiment_dashboard_cos_upload.py" in resolved_argv[2]

    conn, state = _make_autospec_db()
    count = await sync_manifest_to_db(conn, manifest)
    assert count == 19
    assert len(state["executed"]) == 19

    # T28 syncs active baseline cron/timezone ("40 9 * * 1-5" / "America/New_York"), not cutover values
    t28_params = next(p for _, p in state["executed"] if p["job_key"] == "tw-leverage-daily")
    assert t28_params["cron_expr"] == "40 9 * * 1-5"
    assert t28_params["timezone"] == "America/New_York"


# ---------------------------------------------------------------------------
# 3. Scheduler core: advisory lock, claim rollback, unclaimable queue head,
#    queued vs low memory separation, startup guard, terminal guard, recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_advisory_lock_acquire_and_verify() -> None:
    conn, state = _make_autospec_db(
        [
            {"fetchone": (True,)},
            {"fetchone": (1,)},
            {"fetchone": None},
        ]
    )
    assert await try_scheduler_lock(conn) is True
    assert "pg_try_advisory_lock" in state["executed"][0][0]
    await check_scheduler_lock(conn)
    assert "SELECT 1" in state["executed"][1][0]
    with pytest.raises(SchedulerLockLostError):
        await check_scheduler_lock(conn)


@pytest.mark.asyncio
async def test_claim_transaction_rolls_back_when_slot_update_affects_zero_rows() -> None:
    now = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)
    conn, state = _make_autospec_db(
        [
            # 1. SELECT slot
            {"fetchone": {"slot_id": 1, "generation": 4, "slot_state": "idle", "active_run_id": None, "active_job_key": None}},
            # 2. expire_low_memory_runs SELECT
            {"fetchall": []},
            # 3. SELECT queued candidates
            {
                "fetchall": [
                    {
                        "run_id": "run-1",
                        "job_key": T5_JOB_KEY,
                        "scheduled_slot_utc": now,
                        "deadline_utc": now + timedelta(minutes=10),
                        "low_mem_since_utc": None,
                        "enabled": True,
                        "paused_by_user": False,
                        "runner": "container",
                        "command_argv": ["bash", "-lc", "echo ok"],
                        "workdir": "/Users/everflow/.openclaw/workspace",
                        "timeout_seconds": 300,
                        "no_output_timeout_seconds": 120,
                        "required_secrets": [],
                        "cos_mode": "skip",
                        "notify_enabled": False,
                    }
                ]
            },
            # 4. UPDATE dalton_data_task_slot -> rowcount = 0 (simulated race)
            {"rowcount": 0},
        ]
    )

    claimed = await claim_next_queued_run(conn, now_utc=now, available_memory_mb=2048)
    assert claimed is None
    assert state["tx_entries"] == 1
    assert state["tx_rollbacks"] == 1
    assert state["tx_commits"] == 0


@pytest.mark.asyncio
async def test_claim_transaction_rolls_back_when_run_update_affects_zero_rows() -> None:
    now = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)
    conn, state = _make_autospec_db(
        [
            # 1. SELECT slot
            {"fetchone": {"slot_id": 1, "generation": 4, "slot_state": "idle", "active_run_id": None, "active_job_key": None}},
            # 2. expire_low_memory_runs SELECT
            {"fetchall": []},
            # 3. SELECT queued candidates
            {
                "fetchall": [
                    {
                        "run_id": "run-1",
                        "job_key": T5_JOB_KEY,
                        "scheduled_slot_utc": now,
                        "deadline_utc": now + timedelta(minutes=10),
                        "low_mem_since_utc": None,
                        "enabled": True,
                        "paused_by_user": False,
                        "runner": "container",
                        "command_argv": ["bash", "-lc", "echo ok"],
                        "workdir": "/Users/everflow/.openclaw/workspace",
                        "timeout_seconds": 300,
                        "no_output_timeout_seconds": 120,
                        "required_secrets": [],
                        "cos_mode": "skip",
                        "notify_enabled": False,
                    }
                ]
            },
            # 4. UPDATE dalton_data_task_slot -> rowcount = 1, generation = 5
            {"rowcount": 1, "fetchone": {"generation": 5}},
            # 5. UPDATE dalton_data_task_runs -> rowcount = 0
            {"rowcount": 0},
            # 6. UPDATE dalton_data_task_runs SET status = 'cancelled' in fallback tx
            {"rowcount": 1},
        ]
    )

    claimed = await claim_next_queued_run(conn, now_utc=now, available_memory_mb=2048)
    assert claimed is None
    # First transaction rolled back when second UPDATE affected 0 rows
    assert state["tx_rollbacks"] == 1


@pytest.mark.asyncio
async def test_unclaimable_queue_head_is_cancelled_and_next_run_is_claimed() -> None:
    now = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)
    conn, state = _make_autospec_db(
        [
            # 1. SELECT slot
            {"fetchone": {"slot_id": 1, "generation": 7, "slot_state": "idle", "active_run_id": None, "active_job_key": None}},
            # 2. expire_low_memory_runs SELECT
            {"fetchall": []},
            # 3. SELECT queued candidates: head is disabled (enabled=False), second is enabled (enabled=True)
            {
                "fetchall": [
                    {
                        "run_id": "run-disabled-head",
                        "job_key": "us-market-close-summary",
                        "scheduled_slot_utc": now,
                        "deadline_utc": now + timedelta(minutes=5),
                        "low_mem_since_utc": None,
                        "enabled": False,  # unclaimable!
                        "paused_by_user": False,
                        "runner": "container",
                        "command_argv": ["bash", "-lc", "echo disabled"],
                        "workdir": "/Users/everflow/.openclaw/workspace",
                        "timeout_seconds": 240,
                        "no_output_timeout_seconds": 120,
                        "required_secrets": [],
                        "cos_mode": "skip",
                        "notify_enabled": False,
                    },
                    {
                        "run_id": "run-claimable-second",
                        "job_key": T5_JOB_KEY,
                        "scheduled_slot_utc": now,
                        "deadline_utc": now + timedelta(minutes=5),
                        "low_mem_since_utc": None,
                        "enabled": True,  # claimable!
                        "paused_by_user": False,
                        "runner": "container",
                        "command_argv": ["bash", "-lc", "echo T5"],
                        "workdir": "/Users/everflow/.openclaw/workspace",
                        "timeout_seconds": 300,
                        "no_output_timeout_seconds": 120,
                        "required_secrets": [],
                        "cos_mode": "skip",
                        "notify_enabled": False,
                    },
                ]
            },
            # 4. UPDATE dalton_data_task_runs SET status = 'cancelled' for head
            {"rowcount": 1},
            # 5. UPDATE dalton_data_task_slot for second run
            {"rowcount": 1, "fetchone": {"generation": 8}},
            # 6. UPDATE dalton_data_task_runs SET status = 'starting' for second run
            {"rowcount": 1},
        ]
    )

    claimed = await claim_next_queued_run(conn, now_utc=now, available_memory_mb=2048)
    assert claimed is not None
    assert claimed.run_id == "run-claimable-second"
    assert claimed.job_key == T5_JOB_KEY
    assert claimed.generation == 8
    assert state["tx_commits"] == 2

    cancel_sql, cancel_params = state["executed"][3]
    assert "status = 'cancelled'" in cancel_sql
    assert "WHERE run_id = %(run_id)s::uuid" in cancel_sql
    assert cancel_params["run_id"] == "run-disabled-head"


@pytest.mark.asyncio
async def test_queued_slot_wait_vs_low_memory_separation_and_superseded() -> None:
    now = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)

    # Case A: Slot is busy ('running') -> even if memory is low, do NOT mark low_mem_since_utc
    conn_busy, state_busy = _make_autospec_db(
        [
            {"fetchone": {"slot_id": 1, "generation": 3, "slot_state": "running", "active_run_id": "run-active", "active_job_key": T5_JOB_KEY}},
        ]
    )
    res_busy = await claim_next_queued_run(conn_busy, now_utc=now, available_memory_mb=512)
    assert res_busy is None
    assert len(state_busy["executed"]) == 1  # No low_mem_since_utc update when slot is busy

    # Case B: Slot is idle ('idle'), memory < 1280 MB -> sets low_mem_since_utc on candidate head
    conn_low, state_low = _make_autospec_db(
        [
            {"fetchone": {"slot_id": 1, "generation": 3, "slot_state": "idle", "active_run_id": None, "active_job_key": None}},
            {"fetchall": []},
            {
                "fetchall": [
                    {
                        "run_id": "run-low-mem",
                        "job_key": T5_JOB_KEY,
                        "scheduled_slot_utc": now,
                        "deadline_utc": now + timedelta(minutes=5),
                        "low_mem_since_utc": None,
                        "enabled": True,
                        "paused_by_user": False,
                        "runner": "container",
                        "command_argv": ["bash", "-lc", "echo T5"],
                        "workdir": "/Users/everflow/.openclaw/workspace",
                        "timeout_seconds": 300,
                        "no_output_timeout_seconds": 120,
                        "required_secrets": [],
                        "cos_mode": "skip",
                        "notify_enabled": False,
                    }
                ]
            },
            {"rowcount": 1},  # UPDATE low_mem_since_utc
        ]
    )
    res_low = await claim_next_queued_run(
        conn_low, now_utc=now, available_memory_mb=900, min_free_mem_mb=1280
    )
    assert res_low is None
    low_sql, low_params = state_low["executed"][3]
    assert "low_mem_since_utc = COALESCE(low_mem_since_utc, %(now_utc)s)" in low_sql
    assert "WHERE run_id = %(run_id)s::uuid" in low_sql
    assert low_params == {"run_id": "run-low-mem", "now_utc": now}

    # Case C: expire_low_memory_runs transitions only expired low_mem_since_utc IS NOT NULL runs
    # to skipped_low_memory and increments consecutive_failures
    conn_exp, state_exp = _make_autospec_db(
        [
            {"fetchall": [{"run_id": "run-low-mem", "job_key": T5_JOB_KEY}]},
            {"rowcount": 1},  # UPDATE status = 'skipped_low_memory'
            {"rowcount": 1},  # UPDATE consecutive_failures = consecutive_failures + 1
        ]
    )
    expired_ids = await expire_low_memory_runs(conn_exp, now_utc=now + timedelta(minutes=6))
    assert expired_ids == ["run-low-mem"]
    assert "status = 'skipped_low_memory'" in state_exp["executed"][1][0]
    assert "WHERE run_id = %(run_id)s::uuid" in state_exp["executed"][1][0]
    assert "consecutive_failures = consecutive_failures + 1" in state_exp["executed"][2][0]

    # Case D: enqueue_due_cron_tasks marks old queued run as skipped_superseded (no failure bump)
    conn_sup, state_sup = _make_autospec_db(
        [
            # 1. SELECT enabled defs: T18 runs every 5 min
            {
                "fetchall": [
                    {
                        "job_key": T18_JOB_KEY,
                        "cron_expr": "*/5 9-16 * * 1-5",
                        "timezone": "America/New_York",
                        "timeout_seconds": 240,
                        "enabled": True,
                        "paused_by_user": False,
                        "runner": "container",
                    }
                ]
            },
            # 2. UPDATE old queued run SET status = 'skipped_superseded'
            {"rowcount": 1},
            # 3. INSERT new queued run
            {"rowcount": 1, "fetchone": {"run_id": "run-new-slot"}},
        ]
    )
    # 2026-11-06 15:05 UTC is Friday 10:05 EST (within 9-16 * * 1-5)
    tick_utc = datetime(2026, 11, 6, 15, 5, 10, tzinfo=timezone.utc)
    enqueued = await enqueue_due_cron_tasks(conn_sup, now_utc=tick_utc)
    assert enqueued == ["run-new-slot"]
    sup_sql, _ = state_sup["executed"][1]
    assert "status = 'skipped_superseded'" in sup_sql
    assert "%(run_id)s::uuid" in state_sup["executed"][2][0]
    assert not any("consecutive_failures" in sql for sql, _ in state_sup["executed"])


@pytest.mark.asyncio
async def test_startup_update_zero_rows_removes_container_aborts_run_and_releases_slot(
    tmp_path: Path,
) -> None:
    path_map = build_path_map(
        host_var_dir=tmp_path / "host_var",
        backend_var_dir=tmp_path / "backend_var",
    )
    now = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)
    claimed = ClaimedTaskRun(
        run_id="run-start-abort",
        job_key=T5_JOB_KEY,
        generation=10,
        runner="container",
        command_argv=["bash", "-lc", "echo hi"],
        workdir="/Users/everflow/.openclaw/workspace",
        timeout_seconds=300,
        no_output_timeout_seconds=120,
        required_secrets=["TEST_SECRET"],
        cos_mode="skip",
        notify_enabled=False,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=5),
    )
    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.create_container.return_value = "cid-12345"

    lock_conn, _ = _make_autospec_db(
        [
            {"fetchone": (1,)},  # check_scheduler_lock before create
            {"fetchone": (1,)},  # check_scheduler_lock before UPDATE
            {"fetchone": (1,)},  # check_scheduler_lock before remove_container
            {"fetchone": (1,)},  # check_scheduler_lock before abort + slot release tx
        ]
    )
    conn, state = _make_autospec_db(
        [
            # 1. UPDATE dalton_data_task_runs ... WHERE status = 'starting' -> rowcount = 0!
            {"rowcount": 0},
            # 2. UPDATE dalton_data_task_runs SET status = 'aborted'
            {"rowcount": 1},
            # 3. UPDATE dalton_data_task_slot SET slot_state = 'idle'
            {"rowcount": 1},
        ]
    )

    container_id = await start_claimed_container_run(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        claimed=claimed,
        decrypted_secrets={"TEST_SECRET": "super-secret-value"},
        path_map=path_map,
    )
    assert container_id is None
    docker_client.create_container.assert_awaited_once()
    docker_client.remove_container.assert_awaited_once_with("cid-12345", force=True)
    docker_client.start_container.assert_not_awaited()
    assert not path_map.backend_run_secrets_dir("run-start-abort").exists()

    # Verify run marked aborted and slot reset to idle in the same transaction
    abort_sql, _ = state["executed"][1]
    slot_sql, _ = state["executed"][2]
    assert "SET status         = 'aborted'" in abort_sql
    assert "WHERE run_id = %(run_id)s::uuid" in abort_sql
    assert "SET slot_state     = 'idle'" in slot_sql
    assert "AND active_run_id = %(run_id)s::uuid" in slot_sql


@pytest.mark.asyncio
async def test_terminal_update_on_already_terminal_run_does_not_modify_failures_or_notify(
    tmp_path: Path,
) -> None:
    path_map = build_path_map(
        host_var_dir=tmp_path / "host_var",
        backend_var_dir=tmp_path / "backend_var",
    )
    now = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)
    claimed = ClaimedTaskRun(
        run_id="run-already-done",
        job_key=T5_JOB_KEY,
        generation=11,
        runner="container",
        command_argv=["bash", "-lc", "echo hi"],
        workdir="/Users/everflow/.openclaw/workspace",
        timeout_seconds=300,
        no_output_timeout_seconds=120,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=True,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=5),
    )

    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)}])
    # Simulate terminal UPDATE affecting 0 rows because run is already terminal
    conn, state = _make_autospec_db(
        [
            {"rowcount": 0},
        ]
    )

    from src.dalton.data_jobs.evaluators import TaskEvaluationResult

    committed, cf = await finalize_task_run(
        conn,
        lock_conn=lock_conn,
        docker_client=None,
        claimed=claimed,
        container_id=None,
        outcome=ContainerInspectionOutcome(
            container_id="cid-x",
            exit_code=1,
            oom_killed=False,
            running=False,
            log_tail="boom",
        ),
        evaluation=TaskEvaluationResult(ok=False, status="failed", failure_reason="exit_code_1"),
        outbox_payload={"text": "hello"},
        path_map=path_map,
    )
    assert committed is False
    assert cf is None
    assert state["tx_rollbacks"] == 1
    assert state["tx_commits"] == 0
    # Must NOT execute UPDATE on dalton_data_task_defs (consecutive_failures)
    assert not any("dalton_data_task_defs" in sql for sql, _ in state["executed"])


@pytest.mark.asyncio
async def test_terminal_update_writes_status_and_pending_dispatch_in_same_tx_before_container_rm(
    tmp_path: Path,
) -> None:
    path_map = build_path_map(
        host_var_dir=tmp_path / "host_var",
        backend_var_dir=tmp_path / "backend_var",
    )
    art_dir = path_map.backend_run_artifacts_dir("run-ok")
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "outbox.json").write_text(
        json.dumps({"text": "report ready"}), encoding="utf-8"
    )

    now = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)
    claimed = ClaimedTaskRun(
        run_id="run-ok",
        job_key="bloomberg-daily-data-ingest",  # default evaluator: exit_code == 0 -> succeeded
        generation=12,
        runner="container",
        command_argv=["bash", "-lc", "echo ok"],
        workdir="/Users/everflow/.openclaw/workspace",
        timeout_seconds=300,
        no_output_timeout_seconds=120,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=True,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=5),
    )

    call_order: list[str] = []
    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.create_container.return_value = "cid-ok"
    docker_client.collect_container_outcome.return_value = ContainerInspectionOutcome(
        container_id="cid-ok",
        exit_code=0,
        oom_killed=False,
        running=False,
        log_tail="ok\n",
    )

    async def _on_remove(cid: str, *, force: bool = True) -> None:
        call_order.append(f"remove_container:{cid}")

    docker_client.remove_container.side_effect = _on_remove

    lock_conn, _ = _make_autospec_db(
        [
            {"fetchone": (1,)},  # start: before create
            {"fetchone": (1,)},  # start: before UPDATE
            {"fetchone": (1,)},  # start: before start_container
            {"fetchone": (1,)},  # finalize: before terminal tx
            {"fetchone": (1,)},  # finalize: before remove_container
            {"fetchone": (1,)},  # finalize: before slot idle release
        ]
    )
    conn, state = _make_autospec_db(
        [
            # start_claimed_container_run:
            {"rowcount": 1},  # UPDATE runs SET status='running', container_id=...
            # finalize_task_run:
            {"rowcount": 1},  # UPDATE runs SET status='succeeded', notify_status='pending_dispatch'
            {"rowcount": 1, "fetchone": {"consecutive_failures": 0}},  # UPDATE defs SET consecutive_failures = 0
            {"rowcount": 1},  # UPDATE slot SET slot_state = 'idle'
        ]
    )

    orig_tx = conn.transaction.side_effect

    @asynccontextmanager
    async def _tracked_tx():
        async with orig_tx():
            yield
        call_order.append("tx_committed")

    conn.transaction.side_effect = _tracked_tx

    committed, cf = await execute_claimed_container_run(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        claimed=claimed,
        path_map=path_map,
    )
    assert committed is True
    assert cf == 0
    # Verify finalize ledger transaction committed BEFORE remove_container was called
    assert call_order.index("remove_container:cid-ok") > 1

    finalize_sql, finalize_params = state["executed"][1]
    assert "WHERE run_id = %(run_id)s::uuid" in finalize_sql
    assert "AND status IN ('starting', 'running')" in finalize_sql
    assert finalize_params["terminal_status"] == "succeeded"
    assert finalize_params["notify_status"] == "pending_dispatch"


@pytest.mark.asyncio
async def test_recover_on_takeover_does_not_alter_already_terminal_run(
    tmp_path: Path,
) -> None:
    path_map = build_path_map(
        host_var_dir=tmp_path / "host_var",
        backend_var_dir=tmp_path / "backend_var",
    )
    write_run_secrets_dir("run-already-succeeded", {"K": "V"}, path_map=path_map)

    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.list_data_task_containers.return_value = [
        {"container_id": "cid-leftover", "running": False, "run_id": "run-already-succeeded"}
    ]

    lock_conn, _ = _make_autospec_db(
        [
            {"fetchone": (1,)},  # start of recover_on_takeover
            {"fetchone": (1,)},  # before remove_container(cid-leftover, deduplicated)
            {"fetchone": (1,)},  # before slot idle reset
        ]
    )
    conn, state = _make_autospec_db(
        [
            # 1. SELECT slot
            {
                "fetchone": {
                    "slot_id": 1,
                    "generation": 5,
                    "slot_state": "running",
                    "active_run_id": "run-already-succeeded",
                    "active_job_key": T5_JOB_KEY,
                }
            },
            # 2. SELECT run status -> already 'succeeded' (terminal!)
            {
                "fetchone": {
                    "run_id": "run-already-succeeded",
                    "job_key": T5_JOB_KEY,
                    "status": "succeeded",
                    "container_id": "cid-leftover",
                }
            },
            # 3. UPDATE slot SET slot_state = 'idle'
            {"rowcount": 1},
        ]
    )

    outcome = await recover_on_takeover(
        conn, lock_conn=lock_conn, docker_client=docker_client, path_map=path_map
    )
    assert outcome == "cleaned_terminal"

    # Must NOT execute any UPDATE on dalton_data_task_runs or dalton_data_task_defs
    for sql, _ in state["executed"]:
        assert "UPDATE dalton_data_task_runs" not in sql
        assert "UPDATE dalton_data_task_defs" not in sql

    docker_client.remove_container.assert_any_await("cid-leftover", force=True)
    assert not path_map.backend_run_secrets_dir("run-already-succeeded").exists()
    assert "SET slot_state     = 'idle'" in state["executed"][-1][0]


# ---------------------------------------------------------------------------
# 4. Container executor configuration & path mapping tests
# ---------------------------------------------------------------------------


def test_container_create_config_and_path_mapping(tmp_path: Path) -> None:
    path_map = build_path_map(
        host_var_dir=tmp_path / "host" / "var" / "dalton",
        backend_var_dir=Path("/app/var/dalton"),
    )
    assert path_map.host_data_root == (
        tmp_path / "host" / "var" / "dalton" / "openclaw-data"
    )
    assert path_map.backend_data_root == Path("/app/var/dalton/openclaw-data")

    config = build_container_create_config(
        image="dalton-backend:latest",
        run_id="run-42",
        job_key=T5_JOB_KEY,
        generation=9,
        workdir="/Users/everflow/.openclaw/workspace/skills/cn-leverage-monitor",
        timeout_seconds=300,
        no_output_timeout_seconds=120,
        command_argv=["bash", "-lc", "python3 scripts/cn_leverage_monitor_cli.py daily"],
        notify_enabled=False,
        path_map=path_map,
    )

    assert config["Entrypoint"] == ["/usr/bin/tini"]
    assert config["Cmd"][:4] == [
        "--",
        "/app/.venv/bin/python",
        "-m",
        "src.dalton.data_jobs.runner_wrapper",
    ]
    assert config["HostConfig"]["AutoRemove"] is False
    assert config["HostConfig"]["Memory"] == CONTAINER_MEMORY_BYTES == 1073741824
    assert config["HostConfig"]["Tmpfs"] == {
        DEFAULT_TMPFS_OUTBOX_DIR: "rw,noexec,nosuid,size=16m"
    }
    assert config["Labels"]["dalton.run_id"] == "run-42"
    assert config["Labels"]["dalton.generation"] == "9"

    binds = config["HostConfig"]["Binds"]
    assert f"{path_map.host_data_root}:/Users/everflow/.openclaw/workspace:rw" in binds
    assert f"{path_map.host_run_secrets_dir('run-42')}:/run/dalton-secrets:ro" in binds

    env_map = dict(item.split("=", 1) for item in config["Env"])
    assert env_map["HOME"] == "/Users/everflow"
    assert env_map["TZ"] == "America/New_York"
    assert env_map["DALTON_NO_DIRECT_SEND"] == "1"
    assert env_map["DALTON_NOTIFY_DRY_RUN"] == "1"
    # Child outbox file points to tmpfs, not the persistent data volume
    assert env_map["DALTON_OUTBOX_FILE"] == f"{DEFAULT_TMPFS_OUTBOX_DIR}/outbox.json"
    assert "/app/.venv/bin" in env_map["PATH"]
    assert env_map["PYTHONPATH"] == "/app"


# ---------------------------------------------------------------------------
# 5. Container inner wrapper tests (redaction, env, outbox, timeouts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrapper_redacts_stdout_stderr_and_outbox_and_sets_env(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    secret_val = "sk-proj-TOPSECRET987654321"
    (secrets_dir / "OPENAI_API_KEY").write_text(secret_val, encoding="utf-8")

    secrets, redactor = load_secrets_from_dir(secrets_dir)
    assert secrets == {"OPENAI_API_KEY": secret_val}
    assert isinstance(redactor, SecretRedactor)
    assert redactor.redact_text(f"key={secret_val}") == "key=[REDACTED]"

    direct_outbox = tmp_path / "direct_outbox.json"
    persisted = persist_redacted_outbox(
        outbox_path=direct_outbox,
        redactor=redactor,
        explicit_payload={"token": secret_val},
    )
    assert persisted == {"token": "[REDACTED]"}
    assert secret_val not in direct_outbox.read_text(encoding="utf-8")

    child_env = build_child_env(
        base_env={
            "PATH": "/usr/bin",
            "PYTHONPATH": "/app:/Users/everflow/.openclaw/workspace/scripts",
        },
        workspace_dir=str(tmp_path),
        run_id="run-1",
        job_key=T5_JOB_KEY,
        notify_dry_run=True,
        secret_env=secrets,
    )
    assert child_env["DALTON_NO_DIRECT_SEND"] == "1"
    assert child_env["DALTON_NOTIFY_DRY_RUN"] == "1"
    assert child_env["DALTON_OUTBOX_FILE"] == f"{DEFAULT_TMPFS_OUTBOX_DIR}/outbox.json"
    assert child_env["PYTHONPATH"] == "/Users/everflow/.openclaw/workspace/scripts"

    post_cutover_env = build_child_env(
        base_env={"PYTHONPATH": "/app"},
        workspace_dir=str(tmp_path),
        run_id="run-1",
        job_key=T5_JOB_KEY,
        notify_dry_run=False,
        secret_env=secrets,
    )
    assert post_cutover_env["DALTON_NOTIFY_DRY_RUN"] == "0"
    assert "PYTHONPATH" not in post_cutover_env

    staging_tmpfs = tmp_path / "tmpfs_outbox"
    staging_tmpfs.mkdir()
    py_code = (
        "import os, sys, pathlib, json; "
        "print('stdout leak: ' + os.environ['OPENAI_API_KEY']); "
        "print('stderr leak: ' + os.environ['OPENAI_API_KEY'], file=sys.stderr); "
        "pathlib.Path(os.environ['DALTON_OUTBOX_FILE']).write_text("
        "json.dumps({'msg': 'token=' + os.environ['OPENAI_API_KEY']}), encoding='utf-8')"
    )
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    res = await run_wrapped_command(
        [sys.executable, "-c", py_code],
        run_id="run-wrap-1",
        job_key=T5_JOB_KEY,
        workdir=str(tmp_path),
        workspace_dir=str(tmp_path),
        secrets_dir=secrets_dir,
        outbox_tmp_dir=staging_tmpfs,
        notify_dry_run=True,
        timeout_seconds=10,
        no_output_timeout_seconds=10,
        stdout_sink=stdout_buf,
        stderr_sink=stderr_buf,
    )
    assert res.exit_code == 0
    # Child received staging tmpfs path, NOT persistent .dalton-runs path
    assert res.env_exported["DALTON_OUTBOX_FILE"] == str(staging_tmpfs / "outbox.json")
    assert not (staging_tmpfs / "outbox.json").exists()
    assert secret_val not in res.redacted_stdout
    assert secret_val not in res.redacted_stderr
    assert secret_val not in stdout_buf.getvalue()
    assert secret_val not in stderr_buf.getvalue()
    assert "[REDACTED]" in res.redacted_stdout
    assert "[REDACTED]" in res.redacted_stderr
    assert res.outbox_path is not None and res.outbox_path.is_file()
    saved_outbox = res.outbox_path.read_text(encoding="utf-8")
    assert secret_val not in saved_outbox
    assert "[REDACTED]" in saved_outbox


@pytest.mark.asyncio
async def test_wrapper_enforces_timeout_and_no_output_timeout_with_distinct_exit_codes(
    tmp_path: Path,
) -> None:
    secrets_dir = tmp_path / "empty_secrets"
    secrets_dir.mkdir()

    res_no_out = await run_wrapped_command(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        run_id="run-timeout-no-out",
        job_key=T5_JOB_KEY,
        workdir=str(tmp_path),
        workspace_dir=str(tmp_path),
        secrets_dir=secrets_dir,
        timeout_seconds=10,
        no_output_timeout_seconds=0.2,
    )
    assert res_no_out.timed_out is True
    assert res_no_out.timeout_kind == "no_output"
    assert res_no_out.exit_code == EXIT_CODE_NO_OUTPUT_TIMEOUT == 125

    res_total = await run_wrapped_command(
        [sys.executable, "-c", "import time; print('start', flush=True); time.sleep(5)"],
        run_id="run-timeout-total",
        job_key=T5_JOB_KEY,
        workdir=str(tmp_path),
        workspace_dir=str(tmp_path),
        secrets_dir=secrets_dir,
        timeout_seconds=0.2,
        no_output_timeout_seconds=10,
    )
    assert res_total.timed_out is True
    assert res_total.timeout_kind == "total"
    assert res_total.exit_code == EXIT_CODE_TOTAL_TIMEOUT == TIMEOUT_EXIT_CODE == 124

    # Evaluator maps 124 -> total_timed_out and 125 -> no_output_timed_out, both status='timed_out'
    eval_no_out = evaluate_task_run(
        TaskEvaluationContext(job_key=T5_JOB_KEY, exit_code=EXIT_CODE_NO_OUTPUT_TIMEOUT)
    )
    assert eval_no_out.status == "timed_out"
    assert eval_no_out.failure_reason == "no_output_timed_out"
    assert eval_no_out.details["timeout_kind"] == "no_output"

    eval_total = evaluate_task_run(
        TaskEvaluationContext(job_key=T5_JOB_KEY, exit_code=EXIT_CODE_TOTAL_TIMEOUT)
    )
    assert eval_total.status == "timed_out"
    assert eval_total.failure_reason == "total_timed_out"
    assert eval_total.details["timeout_kind"] == "total"


@pytest.mark.asyncio
async def test_t27_and_t32_stdout_notification_payload_extraction(tmp_path: Path) -> None:
    manifest = load_data_task_manifest()
    assert set(TASK_NOTIFY_PAYLOAD_SOURCES.keys()) == {t.job_key for t in manifest.tasks}
    assert TASK_NOTIFY_PAYLOAD_SOURCES["asia-leverage-crossview-daily"] == "stdout_markdown"
    assert TASK_NOTIFY_PAYLOAD_SOURCES["quantum-ew-monitor"] == "stdout_text"

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    secret_val = "SECRET-KEY-99999"
    (secrets_dir / "API_TOKEN").write_text(secret_val, encoding="utf-8")

    # T27: Markdown stdout -> outbox.json
    t27_res = await run_wrapped_command(
        [
            sys.executable,
            "-c",
            "import os; print('# 四地杠杆日报\\n\\n- A股: 正常 ' + os.environ['API_TOKEN'])",
        ],
        run_id="run-t27",
        job_key="asia-leverage-crossview-daily",
        workdir=str(tmp_path),
        workspace_dir=str(tmp_path),
        secrets_dir=secrets_dir,
        outbox_tmp_dir=tmp_path / "tmp_t27",
    )
    assert t27_res.outbox_payload is not None
    assert t27_res.outbox_payload["format"] == "markdown"
    assert "# 四地杠杆日报" in t27_res.outbox_payload["text"]
    assert secret_val not in t27_res.outbox_payload["text"]
    assert "[REDACTED]" in t27_res.outbox_payload["text"]

    # T32: Plain-text stdout -> outbox.json
    t32_res = await run_wrapped_command(
        [
            sys.executable,
            "-c",
            "import os; print('量子等权监控：未触发阈值 ' + os.environ['API_TOKEN'])",
        ],
        run_id="run-t32",
        job_key="quantum-ew-monitor",
        workdir=str(tmp_path),
        workspace_dir=str(tmp_path),
        secrets_dir=secrets_dir,
        outbox_tmp_dir=tmp_path / "tmp_t32",
    )
    assert t32_res.outbox_payload is not None
    assert t32_res.outbox_payload["format"] == "text"
    assert "量子等权监控" in t32_res.outbox_payload["text"]
    assert secret_val not in t32_res.outbox_payload["text"]


# ---------------------------------------------------------------------------
# 6. Evaluators framework + T5 & T18 tests
# ---------------------------------------------------------------------------


def test_evaluators_registry_and_t5_t18(tmp_path: Path) -> None:
    assert T5_JOB_KEY in EVALUATOR_REGISTRY
    assert T18_JOB_KEY in EVALUATOR_REGISTRY
    assert get_evaluator("T5") is not None
    assert get_evaluator("T18") is not None

    # Verify previous_weekday helper
    assert previous_weekday(date(2026, 11, 9)) == date(2026, 11, 6)  # Monday -> Friday
    assert previous_weekday(date(2026, 11, 7)) == date(2026, 11, 6)  # Saturday -> Friday
    assert previous_weekday(date(2026, 11, 6)) == date(2026, 11, 5)  # Friday -> Thursday

    # T5 setup: 4 core steps completed, soft step in aux & pipeline_status not completed -> succeeded with warnings
    state_dir = tmp_path / "state" / "cn-leverage"
    state_dir.mkdir(parents=True, exist_ok=True)
    pipeline_path = state_dir / "pipeline_status.json"
    pipeline_path.write_text(
        json.dumps(
            {
                "两融核心": {"status": "completed"},
                "股指期货基差": {"status": "completed"},
                "宽基ETF份额": {"status": "completed"},
                "量化中性拥挤/Unwind": {"status": "completed"},
                "北向资金": {"status": "skipped"},
                "微盘股相对强弱": {"status": "completed"},
                "可转债估值": {"status": "failed"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    unwind_csv = state_dir / "quant_unwind_signals.csv"
    unwind_csv.write_text("date,signal\n2026-11-06,0.12\n", encoding="utf-8")
    dash_html = tmp_path / "research-outputs" / "cn-leverage" / "index.html"
    dash_html.parent.mkdir(parents=True, exist_ok=True)
    dash_html.write_text("<html><body>dashboard</body></html>", encoding="utf-8")

    t5_stdout_ok = json.dumps(
        {
            "core": {"rows": 10},
            "futures": {"rows": 10},
            "etf": {"rows": 10},
            "limit_up": {"rows": 10},
            "aux": {
                "northbound": {"error": "endpoint deprecated"},
                "microcap": {"ok": True},
                "convertible_bonds": {"error": "eastmoney timeout"},
            },
            "quant_unwind": {"rows": 10},
        },
        indent=2,
    )
    t5_res_ok = evaluate_task_run(
        TaskEvaluationContext(
            job_key=T5_JOB_KEY,
            exit_code=0,
            stdout=t5_stdout_ok,
            data_root=tmp_path,
            reference_date=date(2026, 11, 9),  # Monday -> latest_date 2026-11-06 (Friday) is valid
        )
    )
    assert t5_res_ok.ok is True
    assert t5_res_ok.status == "succeeded"
    assert len(t5_res_ok.warnings) >= 2

    # T5 stale date: latest_date=2026-11-05 (Thursday) when reference_date=2026-11-09 (Monday) -> fails!
    unwind_csv.write_text("date,signal\n2026-11-05,0.12\n", encoding="utf-8")
    t5_res_stale = evaluate_task_run(
        TaskEvaluationContext(
            job_key=T5_JOB_KEY,
            exit_code=0,
            stdout=t5_stdout_ok,
            data_root=tmp_path,
            reference_date=date(2026, 11, 9),
        )
    )
    assert t5_res_stale.ok is False
    assert t5_res_stale.status == "failed"
    assert "t5_stale_unwind_date" in (t5_res_stale.failure_reason or "")
    unwind_csv.write_text("date,signal\n2026-11-06,0.12\n", encoding="utf-8")

    # T5 top-level error (e.g. core has "error") -> failed
    t5_stdout_fail = json.dumps(
        {
            "core": {"error": "margin fetch failed"},
            "futures": {"rows": 10},
            "etf": {"rows": 10},
            "limit_up": {"rows": 10},
            "aux": {},
            "quant_unwind": {"rows": 10},
        }
    )
    t5_res_fail = evaluate_task_run(
        TaskEvaluationContext(
            job_key=T5_JOB_KEY,
            exit_code=0,
            stdout=t5_stdout_fail,
            data_root=tmp_path,
            reference_date=date(2026, 11, 7),
        )
    )
    assert t5_res_fail.ok is False
    assert t5_res_fail.status == "failed"
    assert "t5_top_step_error:core" in (t5_res_fail.failure_reason or "")

    # T18: multi-line JSON + non-empty chart file under container workspace prefix -> succeeded
    chart_rel = "output/megacap_chart.png"
    chart_file = tmp_path / chart_rel
    chart_file.parent.mkdir(parents=True, exist_ok=True)
    chart_file.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")

    t18_stdout_ok = "Log line before JSON\n" + json.dumps(
        {
            "ok": True,
            "date": "2026-11-06",
            "chart": f"/Users/everflow/.openclaw/workspace/{chart_rel}",
        },
        indent=2,
    )
    t18_res_ok = evaluate_task_run(
        TaskEvaluationContext(
            job_key=T18_JOB_KEY,
            exit_code=0,
            stdout=t18_stdout_ok,
            data_root=tmp_path,
        )
    )
    assert t18_res_ok.ok is True
    assert t18_res_ok.status == "succeeded"

    # T18: missing chart file -> failed
    chart_file.unlink()
    t18_res_missing = evaluate_task_run(
        TaskEvaluationContext(
            job_key=T18_JOB_KEY,
            exit_code=0,
            stdout=t18_stdout_ok,
            data_root=tmp_path,
        )
    )
    assert t18_res_missing.ok is False
    assert t18_res_missing.status == "failed"
    assert "t18_chart_file_missing" in (t18_res_missing.failure_reason or "")


# ---------------------------------------------------------------------------
# 7. Switch off by default: loop does not start and creates no containers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_jobs_switch_default_off_does_not_start_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DALTON_DATA_JOBS_ENABLED", raising=False)
    get_dalton_config.cache_clear()

    assert is_data_jobs_enabled() is False
    assert is_data_jobs_scheduler_enabled() is False
    assert get_data_task_min_free_mem_mb() == 1280

    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    await run_data_task_scheduler_loop(docker_client=docker_client)
    docker_client.create_container.assert_not_awaited()


# ---------------------------------------------------------------------------
# 8. Round 2 review fixes: host runner exclusion, Vault secrets, aiodocker dict
#    filters, incremental log timeout, unconfirmed container stop, lock check on
#    cleanup, dedicated lock connection unlock/close, background loop execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_runner_tasks_not_enqueued_and_cancelled_on_claim() -> None:
    now = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)  # 10:00 EST (T29 schedule)
    conn_enq, state_enq = _make_autospec_db([{"fetchall": []}])
    enqueued = await enqueue_due_cron_tasks(conn_enq, now_utc=now)
    assert enqueued == []
    assert "runner = 'container'" in state_enq["executed"][0][0]

    # If a host-runner row is already queued in DB, claim_next_queued_run cancels it
    conn_claim, state_claim = _make_autospec_db(
        [
            {"fetchone": {"slot_id": 1, "generation": 1, "slot_state": "idle", "active_run_id": None, "active_job_key": None}},
            {"fetchall": []},
            {
                "fetchall": [
                    {
                        "run_id": "run-host-1",
                        "job_key": "bbg-data-pull-us",
                        "scheduled_slot_utc": now,
                        "deadline_utc": now + timedelta(minutes=15),
                        "low_mem_since_utc": None,
                        "enabled": True,
                        "paused_by_user": False,
                        "runner": "host",
                        "command_argv": ["bash", "/Users/everflow/.openclaw/workspace/scripts/bbg_remote_refresh.sh"],
                        "workdir": "/Users/everflow/.openclaw/workspace",
                        "timeout_seconds": 900,
                        "no_output_timeout_seconds": 420,
                        "required_secrets": [],
                        "cos_mode": "skip",
                        "notify_enabled": False,
                        "timezone": "America/New_York",
                    }
                ]
            },
            {"rowcount": 1},  # UPDATE SET status = 'cancelled'
        ]
    )
    claimed = await claim_next_queued_run(conn_claim, now_utc=now, available_memory_mb=2048)
    assert claimed is None
    cancel_sql, cancel_params = state_claim["executed"][3]
    assert "status = 'cancelled'" in cancel_sql
    assert cancel_params["cancel_reason"] == "host_runner_not_enabled"


@pytest.mark.asyncio
async def test_vault_secrets_loaded_from_primary_pm_admin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.dalton.data_jobs.scheduler as sched_mod

    monkeypatch.setattr(
        sched_mod,
        "get_configured_pm_user_ids",
        create_autospec(sched_mod.get_configured_pm_user_ids, return_value=["pm-admin-1", "pm-admin-2"]),
    )
    mock_get_secrets = create_autospec(
        sched_mod.get_user_secrets_decrypted,
        return_value={"TENCENT_COS_SECRET_ID": "cos-id-123"},
    )
    monkeypatch.setattr(sched_mod, "get_user_secrets_decrypted", mock_get_secrets)

    resolved = await resolve_task_vault_secrets(["TENCENT_COS_SECRET_ID"])
    assert resolved == {"TENCENT_COS_SECRET_ID": "cos-id-123"}
    mock_get_secrets.assert_awaited_once_with("pm-admin-1", names=["TENCENT_COS_SECRET_ID"])

    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)
    claimed = ClaimedTaskRun(
        run_id="run-sec-1",
        job_key="sentiment-dashboard-refresh",
        generation=1,
        runner="container",
        command_argv=["sh", "-lc", "echo ok"],
        workdir="/Users/everflow/.openclaw/workspace",
        timeout_seconds=900,
        no_output_timeout_seconds=300,
        required_secrets=["TENCENT_COS_SECRET_ID"],
        cos_mode="skip",
        notify_enabled=False,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=15),
    )
    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.create_container.return_value = "cid-sec"
    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)}, {"fetchone": (1,)}, {"fetchone": (1,)}])
    conn, _ = _make_autospec_db([{"rowcount": 1}])

    cid = await start_claimed_container_run(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        claimed=claimed,
        path_map=path_map,
    )
    assert cid == "cid-sec"
    secret_file = path_map.backend_run_secrets_dir("run-sec-1") / "TENCENT_COS_SECRET_ID"
    assert secret_file.read_text(encoding="utf-8") == "cos-id-123"


@pytest.mark.asyncio
async def test_aiodocker_list_containers_passes_dict_filter() -> None:
    mock_docker = create_autospec(aiodocker.Docker, instance=True)
    mock_containers = create_autospec(DockerContainers, instance=True)
    mock_containers.list.return_value = []
    mock_docker.containers = mock_containers

    client = DataTaskDockerClient(docker=mock_docker)
    res = await client.list_data_task_containers()
    assert res == []
    mock_containers.list.assert_awaited_once_with(
        all=True,
        filters={"label": ["dalton.data_task=1"]},
    )


@pytest.mark.asyncio
async def test_incremental_logs_prevent_false_no_output_timeout_past_16kib() -> None:
    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    full_16k = "A" * (16 * 1024)
    outcomes = [
        ContainerInspectionOutcome("cid-1", None, False, True, full_16k),
        ContainerInspectionOutcome("cid-1", None, False, True, full_16k),
        ContainerInspectionOutcome("cid-1", 0, False, False, full_16k),
    ]
    docker_client.collect_container_outcome.side_effect = outcomes
    docker_client.get_container_logs.side_effect = ["new chunk 1\n", "new chunk 2\n"]

    times = iter([0.0, 0.0, 6.0, 11.0])
    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)}, {"fetchone": (1,)}])

    outcome, timed_out = await monitor_container_until_exit(
        lock_conn=lock_conn,
        docker_client=docker_client,
        container_id="cid-1",
        timeout_seconds=60,
        no_output_timeout_seconds=8,  # would time out at t=11.0 if not refreshed at t=6.0!
        poll_interval_seconds=0.0,
        clock=lambda: next(times),
        wall_clock=lambda: 1700000000.0,
    )
    assert timed_out is False
    assert outcome.exit_code == 0
    docker_client.stop_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_container_stop_keeps_slot_in_reclaiming(tmp_path: Path) -> None:
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)
    claimed = ClaimedTaskRun(
        run_id="run-unstopped",
        job_key=T5_JOB_KEY,
        generation=3,
        runner="container",
        command_argv=["bash", "-lc", "sleep 100"],
        workdir="/Users/everflow/.openclaw/workspace",
        timeout_seconds=10,
        no_output_timeout_seconds=None,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=False,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=5),
    )
    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)}])
    conn, state = _make_autospec_db([{"rowcount": 1}])

    from src.dalton.data_jobs.evaluators import TaskEvaluationResult

    # Container is STILL running (running=True) after stop/kill failed
    committed, cf = await finalize_task_run(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        claimed=claimed,
        container_id="cid-unstopped",
        outcome=ContainerInspectionOutcome("cid-unstopped", None, False, True, ""),
        evaluation=TaskEvaluationResult(ok=False, status="timed_out", failure_reason="total_timed_out"),
        path_map=path_map,
    )
    assert committed is False
    assert cf is None
    assert len(state["executed"]) == 1
    assert "SET slot_state = 'reclaiming'" in state["executed"][0][0]
    assert not any("dalton_data_task_runs" in sql for sql, _ in state["executed"])
    docker_client.remove_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_lock_check_failure_blocks_cleanup_docker_operations(tmp_path: Path) -> None:
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)
    claimed = ClaimedTaskRun(
        run_id="run-lock-lost",
        job_key=T5_JOB_KEY,
        generation=4,
        runner="container",
        command_argv=["bash", "-lc", "echo hi"],
        workdir="/Users/everflow/.openclaw/workspace",
        timeout_seconds=300,
        no_output_timeout_seconds=None,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=False,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=5),
    )
    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.create_container.return_value = "cid-lock-lost"

    # First lock check (before create_container) succeeds; second lock check fails!
    lock_conn, _ = _make_autospec_db(
        [
            {"fetchone": (1,)},
            {"fetchone": None},  # lock lost!
        ]
    )
    conn, _ = _make_autospec_db([])

    with pytest.raises(SchedulerLockLostError):
        await start_claimed_container_run(
            conn,
            lock_conn=lock_conn,
            docker_client=docker_client,
            claimed=claimed,
            path_map=path_map,
        )
    # Must NOT call remove_container or start_container after losing the lock
    docker_client.remove_container.assert_not_awaited()
    docker_client.start_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedicated_lock_connection_unlocks_and_closes_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_conn, lock_state = _make_autospec_db(
        [
            {"fetchone": (True,)},  # try_scheduler_lock -> True
            {"fetchone": (1,)},  # check_scheduler_lock in recover_on_takeover
            {"fetchone": (1,)},  # check_scheduler_lock in loop raises below
            {"fetchone": (True,)},  # pg_advisory_unlock in finally
        ]
    )
    await release_scheduler_lock(lock_conn, close_conn=True)
    assert any("pg_advisory_unlock" in sql for sql, _ in lock_state["executed"])
    lock_conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_loop_executes_in_background_and_enqueues_while_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.data_jobs.scheduler as sched_mod

    monkeypatch.setattr(
        sched_mod,
        "is_data_jobs_scheduler_enabled",
        create_autospec(sched_mod.is_data_jobs_scheduler_enabled, return_value=True),
    )
    monkeypatch.setattr(
        sched_mod,
        "sync_manifest_to_db",
        create_autospec(sched_mod.sync_manifest_to_db, return_value=19),
    )
    monkeypatch.setattr(
        sched_mod,
        "recover_on_takeover",
        create_autospec(sched_mod.recover_on_takeover, return_value=None),
    )
    monkeypatch.setattr(
        sched_mod,
        "expire_low_memory_runs",
        create_autospec(sched_mod.expire_low_memory_runs, return_value=[]),
    )

    enqueue_calls: list[int] = []
    bg_started = asyncio.Event()
    bg_release = asyncio.Event()
    stop_event = asyncio.Event()

    async def _fake_enqueue(
        conn: Any,
        *,
        now_utc: datetime | None = None,
    ) -> list[str]:
        enqueue_calls.append(len(enqueue_calls) + 1)
        if len(enqueue_calls) >= 2:
            bg_release.set()
            stop_event.set()
        return []

    now = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)
    claimed_once = ClaimedTaskRun(
        run_id="run-bg-1",
        job_key=T5_JOB_KEY,
        generation=1,
        runner="container",
        command_argv=["bash", "-c", "echo bg"],
        workdir="/Users/everflow/.openclaw/workspace",
        timeout_seconds=300,
        no_output_timeout_seconds=None,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=False,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=5),
    )
    claim_calls = 0

    async def _fake_claim(
        conn: Any,
        *,
        now_utc: datetime | None = None,
        available_memory_mb: int | None = None,
        min_free_mem_mb: int | None = None,
    ) -> ClaimedTaskRun | None:
        nonlocal claim_calls
        claim_calls += 1
        return claimed_once if claim_calls == 1 else None

    async def _fake_exec(
        conn: Any,
        *,
        lock_conn: Any,
        docker_client: DataTaskDockerClient,
        claimed: ClaimedTaskRun,
        decrypted_secrets: Any = None,
        path_map: Any = None,
    ) -> tuple[bool, int | None]:
        bg_started.set()
        await bg_release.wait()
        return True, 0

    monkeypatch.setattr(
        sched_mod,
        "enqueue_due_cron_tasks",
        create_autospec(sched_mod.enqueue_due_cron_tasks, side_effect=_fake_enqueue),
    )
    monkeypatch.setattr(
        sched_mod,
        "claim_next_queued_run",
        create_autospec(sched_mod.claim_next_queued_run, side_effect=_fake_claim),
    )
    monkeypatch.setattr(
        sched_mod,
        "execute_claimed_container_run",
        create_autospec(sched_mod.execute_claimed_container_run, side_effect=_fake_exec),
    )

    lock_conn, lock_state = _make_autospec_db(
        [
            {"fetchone": (True,)},  # try_scheduler_lock
            {"fetchone": (1,)},
            {"fetchone": (1,)},
            {"fetchone": (1,)},
            {"fetchone": (1,)},
            {"fetchone": (1,)},
            {"fetchone": (True,)},  # pg_advisory_unlock
        ]
    )

    @asynccontextmanager
    async def _fake_db_conn_factory(conn: Any = None):
        work_conn, _ = _make_autospec_db([{"fetchone": {"slot_state": "idle"}}])
        yield work_conn

    async def _fake_lock_conn_factory(dsn: str | None = None) -> psycopg.AsyncConnection:
        return lock_conn

    db_conn_factory_mock = create_autospec(
        sched_mod.get_db_connection,
        side_effect=_fake_db_conn_factory,
    )
    lock_conn_factory_mock = create_autospec(
        sched_mod.open_dedicated_lock_connection,
        side_effect=_fake_lock_conn_factory,
    )

    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    await run_data_task_scheduler_loop(
        stop_event=stop_event,
        docker_client=docker_client,
        db_conn_factory=db_conn_factory_mock,
        lock_conn_factory=lock_conn_factory_mock,
        poll_interval_seconds=0.02,
    )

    # Main loop ran enqueue_due_cron_tasks a second time while _fake_exec was still running in background!
    assert len(enqueue_calls) >= 2
    assert claim_calls == 1
    assert any("pg_advisory_unlock" in sql for sql, _ in lock_state["executed"])
    lock_conn.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# 9. Round 3 fixes: takeover 404 & deduplication, non-login shell (-c) PATH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_on_takeover_deduplicates_containers_and_treats_404_as_removed(
    tmp_path: Path,
) -> None:
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")

    # 1. Verify DataTaskDockerClient.remove_container itself swallows Docker 404
    mock_docker = create_autospec(aiodocker.Docker, instance=True)
    mock_containers = create_autospec(DockerContainers, instance=True)
    mock_containers.get.side_effect = DockerError(404, "No such container: cid-missing")
    mock_docker.containers = mock_containers
    real_client = DataTaskDockerClient(docker=mock_docker)
    await real_client.remove_container("cid-missing", force=True)
    mock_containers.get.assert_awaited_once_with("cid-missing")

    # 2. Verify recover_on_takeover deduplicates run_cid ("cid-dup") and treats 404 on second
    #    remove_container call ("cid-gone") as cleanup complete, releasing the slot to 'idle'.
    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.list_data_task_containers.return_value = [
        {"container_id": "cid-dup", "running": False},
        {"container_id": "cid-gone", "running": False},
    ]
    docker_client.remove_container.side_effect = [
        None,
        DockerError(404, "No such container: cid-gone"),
    ]

    lock_conn, _ = _make_autospec_db(
        [
            {"fetchone": (1,)},  # start of recover_on_takeover
            {"fetchone": (1,)},  # before abort transaction
            {"fetchone": (1,)},  # before remove_container("cid-dup")
            {"fetchone": (1,)},  # before remove_container("cid-gone")
            {"fetchone": (1,)},  # before slot idle release
        ]
    )
    conn, state = _make_autospec_db(
        [
            # 1. SELECT slot
            {
                "fetchone": {
                    "slot_id": 1,
                    "generation": 6,
                    "slot_state": "running",
                    "active_run_id": "run-takeover-404",
                    "active_job_key": T5_JOB_KEY,
                }
            },
            # 2. SELECT run row (has container_id = "cid-dup", which is ALSO in leftover_containers)
            {
                "fetchone": {
                    "run_id": "run-takeover-404",
                    "job_key": T5_JOB_KEY,
                    "status": "running",
                    "container_id": "cid-dup",
                }
            },
            # 3. UPDATE dalton_data_task_runs SET status = 'aborted'
            {"rowcount": 1},
            # 4. UPDATE dalton_data_task_defs SET consecutive_failures = consecutive_failures + 1
            {"rowcount": 1},
            # 5. UPDATE dalton_data_task_slot SET slot_state = 'idle'
            {"rowcount": 1},
        ]
    )

    outcome = await recover_on_takeover(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        path_map=path_map,
    )
    assert outcome == "aborted"
    # "cid-dup" was only removed ONCE despite appearing in both run_row and leftover_containers,
    # and the second removal ("cid-gone") raised 404 which was treated as success!
    assert docker_client.remove_container.await_count == 2
    docker_client.remove_container.assert_any_await("cid-dup", force=True)
    docker_client.remove_container.assert_any_await("cid-gone", force=True)
    assert "SET slot_state     = 'idle'" in state["executed"][-1][0]
    assert not any("slot_state = 'reclaiming'" in sql for sql, _ in state["executed"])


@pytest.mark.asyncio
async def test_manifest_no_login_shell_and_wrapper_sh_c_preserves_prepended_venv_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = load_data_task_manifest()
    for entry in manifest.tasks:
        assert "-lc" not in entry.command_argv, (
            f"{entry.task_code} ({entry.job_key}) must not use -lc login shell"
        )
        for arg in entry.command_argv:
            assert arg != "-lc"

    fake_venv_bin = tmp_path / "fake_venv" / "bin"
    fake_venv_bin.mkdir(parents=True, exist_ok=True)
    fake_python3 = fake_venv_bin / "python3"
    fake_python3.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_python3.chmod(fake_python3.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    orig_path = os.environ.get("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("PATH", f"{fake_venv_bin}:{orig_path}")

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    res = await run_wrapped_command(
        ["sh", "-c", "command -v python3"],
        run_id="run-path-check",
        job_key=T18_JOB_KEY,
        workdir=str(tmp_path),
        workspace_dir=str(tmp_path),
        secrets_dir=secrets_dir,
        outbox_tmp_dir=tmp_path / "tmp_outbox",
    )
    assert res.exit_code == 0
    # The wrapper puts the image venv first; where it exists (inside the image) it wins.
    image_python3 = "/app/.venv/bin/python3"
    expected = image_python3 if os.access(image_python3, os.X_OK) else str(fake_python3)
    assert res.redacted_stdout.strip() == expected


def test_only_a_docker_404_counts_as_container_gone():
    from aiodocker.exceptions import DockerError

    from src.dalton.data_jobs.container_runner import is_container_not_found_error

    assert is_container_not_found_error(DockerError(404, "No such container: x"))
    assert not is_container_not_found_error(DockerError(500, "server error"))
    assert not is_container_not_found_error(FileNotFoundError("docker.sock not found"))
    assert not is_container_not_found_error(OSError("HTTP 404 from proxy"))
