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
    T1_JOB_KEY,
    T5_JOB_KEY,
    T18_JOB_KEY,
    T29_JOB_KEY,
    TaskEvaluationContext,
    evaluate_t1_t29_bbg_data_pull,
    evaluate_task_run,
    get_evaluator,
    previous_weekday,
)
from src.dalton.data_jobs.host_runner import (
    DataTaskHostBridgeClient,
    HostKillOutcome,
    HostRunOutcome,
    post_bridge_host_kill,
    post_bridge_host_run,
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
    bump_slot_generation_and_kill_host_task,
    check_scheduler_lock,
    claim_next_queued_run,
    enqueue_due_cron_tasks,
    execute_claimed_container_run,
    execute_claimed_host_run,
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

        # Runner check: T1 and T29 are host (mirror mode), all others are container
        if code in {"T1", "T29"}:
            assert entry.runner == "host"
            assert entry.host_mode == "mirror"
        else:
            assert entry.runner == "container"
            assert entry.host_mode is None

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
async def test_host_runner_tasks_enqueued_and_claimed() -> None:
    now = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)  # 10:00 EST (T29 schedule)
    conn_enq, state_enq = _make_autospec_db(
        [
            {
                "fetchall": [
                    {
                        "job_key": T29_JOB_KEY,
                        "cron_expr": "0 10 * * 1-5",
                        "timezone": "America/New_York",
                        "timeout_seconds": 900,
                        "enabled": True,
                        "paused_by_user": False,
                        "runner": "host",
                    }
                ]
            },
            {"rowcount": 0},  # UPDATE old queued SET skipped_superseded
            {"rowcount": 1, "fetchone": {"run_id": "run-host-enq-1"}},  # INSERT new queued run
        ]
    )
    enqueued = await enqueue_due_cron_tasks(conn_enq, now_utc=now)
    assert enqueued == ["run-host-enq-1"]
    assert "runner IN ('container', 'host')" in state_enq["executed"][0][0]

    # A queued host-runner row is claimed normally (and does not require container memory gate)
    conn_claim, state_claim = _make_autospec_db(
        [
            {"fetchone": {"slot_id": 1, "generation": 1, "slot_state": "idle", "active_run_id": None, "active_job_key": None}},
            {"fetchall": []},
            {
                "fetchall": [
                    {
                        "run_id": "run-host-1",
                        "job_key": T29_JOB_KEY,
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
            {"rowcount": 1, "fetchone": {"generation": 2}},  # UPDATE slot
            {"rowcount": 1},  # UPDATE run SET status = 'starting'
        ]
    )
    claimed = await claim_next_queued_run(conn_claim, now_utc=now, available_memory_mb=512)
    assert claimed is not None
    assert claimed.run_id == "run-host-1"
    assert claimed.job_key == T29_JOB_KEY
    assert claimed.runner == "host"
    assert claimed.host_mode == "mirror"
    assert claimed.generation == 2
    assert state_claim["tx_commits"] == 1


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


# ---------------------------------------------------------------------------
# 10. PR 0-2: Host execution (T1/T29), bridge host-run/host-kill, gate wrapper,
#     startup recovery, redaction parity, BBG mirror & run modes, and scheduler
# ---------------------------------------------------------------------------


def _make_valid_bbg_csv_content(num_rows: int = 260) -> str:
    """Build a valid 20-column BBG CSV matching `.agy/oc1/openclaw/scripts/bbg_export_to_csv.py:30-38`.

    Column pairs:
    - `cyc_price`: (0, 1), spacer at 2
    - `cyc_rsi14`: (3, 4), spacer at 5
    - `momentum_rsi`: (6, 7), spacer at 8
    - `qqq_vol_1m`: (9, 10), spacer at 11
    - `qqq_vol_3m`: (12, 13), spacer at 14
    - `ah_price`: (15, 16), spacer at 17
    - `ah_rsi30`: (18, 19)
    """
    header_cols = [
        "Date_GSSL",
        "PX_LAST_GSSL",
        "",
        "Date_RSI14",
        "RSI_14D_GSSL",
        "",
        "Date_GSMB",
        "RSI_14D_GSMB",
        "",
        "Date_IV1M",
        "1M_IV_QQQ",
        "",
        "Date_IV3M",
        "3M_IV_QQQ",
        "",
        "Date_GSCB",
        "PX_LAST_GSCB",
        "",
        "Date_RSI30",
        "RSI_30D_GSCB",
    ]
    assert len(header_cols) == 20
    lines = [",".join(header_cols)]
    base_day = date(2025, 1, 1)
    for idx in range(num_rows):
        d_str = (base_day + timedelta(days=idx)).isoformat()
        row = [
            d_str,
            f"{100.0 + idx * 0.1:.2f}",
            "",
            d_str,
            f"{50.0 + (idx % 20) * 0.5:.2f}",
            "",
            d_str,
            f"{48.0 + (idx % 15) * 0.5:.2f}",
            "",
            d_str,
            f"{18.5 + (idx % 10) * 0.2:.2f}",
            "",
            d_str,
            f"{20.0 + (idx % 10) * 0.2:.2f}",
            "",
            d_str,
            f"{110.0 + idx * 0.1:.2f}",
            "",
            d_str,
            f"{52.0 + (idx % 12) * 0.4:.2f}",
        ]
        lines.append(",".join(row))
    return "\n".join(lines) + "\n"


def _write_valid_xlsx_zip(path: Path) -> None:
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types></Types>")
        zf.writestr("xl/workbook.xml", "<workbook></workbook>")


def test_bridge_secret_redactor_matches_runner_wrapper_redactor() -> None:
    import scripts.dalton.researchdata_bridge as bridge_mod

    literals = ["super-secret-token-value-12345", "another_secret_987654"]
    wrapper_redactor = SecretRedactor(literals)
    bridge_redactor = bridge_mod.HostSecretRedactor(literals)

    samples = [
        "plain line with no secrets",
        "literal leak: super-secret-token-value-12345 and another_secret_987654",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload",
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "api_key=my_super_secret_api_key_value_999",
        "FEISHU_APP_SECRET: feishu_secret_abcdef1234567890",
        "TENCENT_COS_SECRET_KEY=cos_secret_key_value_1234567890",
    ]
    for sample in samples:
        assert bridge_redactor.redact_text(sample) == wrapper_redactor.redact_text(sample)


def test_t1_and_t29_evaluator_registration_and_validation() -> None:
    assert T1_JOB_KEY in EVALUATOR_REGISTRY
    assert T29_JOB_KEY in EVALUATOR_REGISTRY
    assert get_evaluator("T1") is evaluate_t1_t29_bbg_data_pull
    assert get_evaluator("T29") is evaluate_t1_t29_bbg_data_pull

    valid_stdout = (
        "some progress log\n"
        + json.dumps(
            {
                "ok": True,
                "lines": 253,
                "source": "openclaw_mirror",
                "path": "/var/dalton/openclaw-data/temp/bbg_data_latest.csv",
                "timestamp": "2026-11-07T08:05:00Z",
                "detail": {"counts": {}, "last_dates": {}},
            }
        )
        + "\n"
    )
    for job_key in (T1_JOB_KEY, T29_JOB_KEY):
        res_ok = evaluate_task_run(
            TaskEvaluationContext(job_key=job_key, exit_code=0, stdout=valid_stdout)
        )
        assert res_ok.ok is True
        assert res_ok.status == "succeeded"

        res_short = evaluate_task_run(
            TaskEvaluationContext(
                job_key=job_key,
                exit_code=0,
                stdout=json.dumps({"ok": True, "lines": 251}),
            )
        )
        assert res_short.ok is False
        assert res_short.status == "failed"
        assert "bbg_insufficient_lines:251" in (res_short.failure_reason or "")

        res_not_ok = evaluate_task_run(
            TaskEvaluationContext(
                job_key=job_key,
                exit_code=0,
                stdout=json.dumps({"ok": False, "lines": 300, "reason": "openclaw_not_updated"}),
            )
        )
        assert res_not_ok.ok is False
        assert res_not_ok.status == "failed"
        assert "openclaw_not_updated" in (res_not_ok.failure_reason or "")

        res_exit1 = evaluate_task_run(
            TaskEvaluationContext(
                job_key=job_key,
                exit_code=1,
                stdout=json.dumps({"ok": False, "lines": 0, "reason": "openclaw_not_updated"}),
            )
        )
        assert res_exit1.ok is False
        assert res_exit1.status == "failed"
        assert res_exit1.failure_reason == "openclaw_not_updated"


@pytest.mark.asyncio
async def test_bridge_gate_wrapper_exits_without_running_when_pipe_closed(
    tmp_path: Path,
) -> None:
    import scripts.dalton.researchdata_bridge as bridge_mod

    marker = tmp_path / "should_not_exist.txt"
    bridge_script = Path(bridge_mod.__file__).resolve()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(bridge_script),
        bridge_mod.HOST_TASK_GATE_FLAG,
        sys.executable,
        "-c",
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdin is not None
    # Close control pipe immediately without sending release token
    proc.stdin.close()
    rc = await asyncio.wait_for(proc.wait(), timeout=5.0)
    assert rc != 0
    assert not marker.exists(), "Gate wrapper must not execute target when pipe closes unreleased"


def _make_live_ps_autospec(
    bridge_mod: Any,
    tracked: dict[int, tuple[str, str]],
) -> Any:
    """Build a `create_autospec(bridge_mod._list_host_processes_ps)` that checks real `os.killpg(pgid, 0)` liveness."""

    def _side_effect() -> list[tuple[int, int, str, str]]:
        # First include any active runs currently registered on the bridge if present
        rows: list[tuple[int, int, str, str]] = []
        for pgid, (lstart, cmd) in list(tracked.items()):
            bridge_mod._reap_pgid_zombies(pgid)
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                tracked.pop(pgid, None)
                continue
            except PermissionError:
                pass
            rows.append((pgid, pgid, lstart, cmd))
        return rows

    return create_autospec(bridge_mod._list_host_processes_ps, side_effect=_side_effect)


@pytest.mark.asyncio
async def test_bridge_host_run_state_file_0600_generation_fence_host_kill_and_startup_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.dalton.researchdata_bridge as bridge_mod

    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "test-bridge-admin-token")
    cfg = bridge_mod.BridgeConfig()
    cfg.admin_token = "test-bridge-admin-token"
    cfg.data_tasks_data_root = tmp_path / "openclaw-data"
    cfg.host_task_sigterm_grace_s = 0.3

    # Verify default data_root derivation on a fresh BridgeConfig matches repo/var/dalton/openclaw-data
    fresh_cfg = bridge_mod.BridgeConfig()
    assert fresh_cfg.data_tasks_data_root == (
        Path(bridge_mod.__file__).resolve().parents[2] / "var" / "dalton" / "openclaw-data"
    )

    # Create a real executable script at <data_root>/scripts/bbg_remote_refresh.sh
    scripts_dir = cfg.data_tasks_data_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    refresh_script = scripts_dir / "bbg_remote_refresh.sh"
    refresh_script.write_text(
        "#!/usr/bin/env bash\n"
        'echo "{\\"ok\\": true, \\"lines\\": 260, \\"source\\": \\"bbg_remote\\", \\"env_ws\\": \\"$OPENCLAW_WORKSPACE\\", \\"no_direct\\": \\"$DALTON_NO_DIRECT_SEND\\"}"\n',
        encoding="utf-8",
    )
    refresh_script.chmod(0o755)

    tracked_procs: dict[int, tuple[str, str]] = {}

    def _ps_for_bridge() -> list[tuple[int, int, str, str]]:
        # Discover any newly spawned proc in bridge._host_running_procs or tracked_procs
        for p_id, proc_obj in list(bridge._host_running_procs.items()):
            if p_id not in tracked_procs and proc_obj.returncode is None:
                tracked_procs[p_id] = (
                    "Sat Nov  7 10:00:00 2026",
                    f"{sys.executable} {bridge_mod.__file__} {bridge_mod.HOST_TASK_GATE_FLAG}",
                )
        rows: list[tuple[int, int, str, str]] = []
        # Also check any newly spawned child process whose pgid is not yet in _host_running_procs
        for p_id, (lstart, cmd) in list(tracked_procs.items()):
            bridge_mod._reap_pgid_zombies(p_id)
            try:
                os.killpg(p_id, 0)
            except ProcessLookupError:
                tracked_procs.pop(p_id, None)
                continue
            except PermissionError:
                pass
            rows.append((p_id, p_id, lstart, cmd))
        return rows

    orig_create_subproc = asyncio.create_subprocess_exec

    async def _wrapped_create_subproc(*args: Any, **kwargs: Any) -> Any:
        proc = await orig_create_subproc(*args, **kwargs)
        pgid = int(proc.pid)
        tracked_procs[pgid] = (
            "Sat Nov  7 10:00:00 2026",
            " ".join(str(a) for a in args),
        )
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _wrapped_create_subproc)

    bridge = bridge_mod.ResearchDataBridge(cfg)
    bridge._host_ps_list_fn = create_autospec(
        bridge_mod._list_host_processes_ps, side_effect=_ps_for_bridge
    )

    # 1. Run with generation=5 succeeds and writes state file with mode 0600
    status_code, resp = await bridge.handle_data_task_host_run(
        {
            "generation": 5,
            "run_id": "run-gen-5",
            "job_key": T1_JOB_KEY,
            "host_mode": "run",
            "scheduled_slot_utc": "2026-11-07T10:00:00Z",
            "timeout": 30,
            "no_output_timeout": 15,
        }
    )
    assert status_code == 200
    assert resp["ok"] is True
    assert resp["exit_code"] == 0
    assert '"ok": true' in resp["log_tail"]
    assert str(cfg.data_tasks_data_root) in resp["log_tail"]

    state_file = cfg.data_tasks_data_root / ".dalton-host-runner-state.json"
    assert state_file.is_file()
    mode = stat.S_IMODE(state_file.stat().st_mode)
    assert mode == 0o600, f"Expected state file mode 0600, got {oct(mode)}"
    saved_state = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved_state["max_seen_generation"] == 5
    assert saved_state["active_runs"] == []

    # 2. Smaller generation (generation=4 < 5) is rejected with 409 stale_generation
    status_stale, resp_stale = await bridge.handle_data_task_host_run(
        {
            "generation": 4,
            "run_id": "run-gen-4",
            "job_key": T1_JOB_KEY,
            "host_mode": "run",
            "scheduled_slot_utc": "2026-11-07T10:00:00Z",
            "timeout": 30,
            "no_output_timeout": 15,
        }
    )
    assert status_stale == 409
    assert resp_stale["error"] == "stale_generation"
    assert resp_stale["max_seen_generation"] == 5

    # 3. Start a long-running sleep in `bbg_remote_refresh.sh`, then call `host-kill` with generation=6
    refresh_script.write_text(
        "#!/usr/bin/env bash\n"
        "sleep 60\n",
        encoding="utf-8",
    )
    run_task = asyncio.create_task(
        bridge.handle_data_task_host_run(
            {
                "generation": 5,
                "run_id": "run-sleeping",
                "job_key": T1_JOB_KEY,
                "host_mode": "run",
                "scheduled_slot_utc": "2026-11-07T10:00:00Z",
                "timeout": 60,
                "no_output_timeout": 60,
            }
        )
    )
    # Wait until the bridge registers the active pgid in `_host_active_runs`
    for _ in range(50):
        if bridge._host_active_runs:
            break
        await asyncio.sleep(0.05)
    assert len(bridge._host_active_runs) == 1
    active_pgid = next(iter(bridge._host_active_runs.keys()))
    assert bridge._host_active_runs[active_pgid]["lstart"] == "Sat Nov 7 10:00:00 2026"

    kill_status, kill_resp = await bridge.handle_data_task_host_kill(
        {"generation": 6, "run_id": "run-sleeping", "reason": "test_kill"}
    )
    assert kill_status == 200
    assert kill_resp["ok"] is True
    assert kill_resp["confirmed"] is True
    assert kill_resp["max_seen_generation"] == 6
    assert active_pgid in kill_resp["stopped_pgids"]

    # Process group must already be gone when host-kill returns
    with pytest.raises(ProcessLookupError):
        os.killpg(active_pgid, 0)

    await asyncio.wait_for(run_task, timeout=5.0)
    saved_after_kill = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved_after_kill["max_seen_generation"] == 6
    assert saved_after_kill["active_runs"] == []

    # 4. Startup recovery: spawn a real gate-wrapped sleep process group, record it in state file
    #    with its leader `lstart`, and verify a new bridge instance stops the process group and restores max_seen_generation
    bridge_script = Path(bridge_mod.__file__).resolve()
    orphan_proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(bridge_script),
        bridge_mod.HOST_TASK_GATE_FLAG,
        "sleep",
        "60",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    assert orphan_proc.stdin is not None
    orphan_proc.stdin.write(b"1\n")
    await orphan_proc.stdin.drain()
    orphan_proc.stdin.close()
    orphan_pgid = int(orphan_proc.pid)

    bridge._write_host_runner_state_file_sync(
        max_seen_generation=11,
        active_runs={
            orphan_pgid: {
                "pgid": orphan_pgid,
                "pid": orphan_pgid,
                "lstart": "Sat Nov 7 10:00:00 2026",
                "run_id": "run-orphan",
                "job_key": T1_JOB_KEY,
                "generation": 11,
                "host_mode": "run",
            }
        },
    )

    bridge2 = bridge_mod.ResearchDataBridge(cfg)
    bridge2._host_ps_list_fn = create_autospec(
        bridge_mod._list_host_processes_ps, side_effect=_ps_for_bridge
    )
    await bridge2.recover_host_runner_state_on_startup()
    try:
        await asyncio.wait_for(orphan_proc.wait(), timeout=2.0)
    except Exception:
        pass
    assert bridge2._host_max_seen_generation == 11
    assert bridge2._host_active_runs == {}
    with pytest.raises(ProcessLookupError):
        os.killpg(orphan_pgid, 0)


@pytest.mark.asyncio
async def test_bridge_bbg_mirror_mode_timeout_validation_failure_and_success(
    tmp_path: Path,
) -> None:
    import scripts.dalton.researchdata_bridge as bridge_mod

    openclaw_ws = tmp_path / "openclaw_ws"
    openclaw_temp = openclaw_ws / "temp"
    openclaw_temp.mkdir(parents=True, exist_ok=True)
    src_csv = openclaw_temp / "bbg_data_latest.csv"
    src_xlsx = openclaw_temp / "bbg_data_remote.xlsx"

    data_root = tmp_path / "dalton_data_root"
    dest_temp = data_root / "temp"
    dest_temp.mkdir(parents=True, exist_ok=True)
    dest_csv = dest_temp / "bbg_data_latest.csv"
    dest_xlsx = dest_temp / "bbg_data_remote.xlsx"

    slot_dt = datetime(2026, 11, 7, 15, 0, 0, tzinfo=timezone.utc)
    slot_ts = slot_dt.timestamp()

    empty_ps_fn = create_autospec(bridge_mod._list_host_processes_ps, return_value=[])

    # Case 1: OpenClaw CSV mtime is older than scheduled_slot_utc -> waits, prints progress lines, and exits 1 with openclaw_not_updated
    src_csv.write_text(_make_valid_bbg_csv_content(260), encoding="utf-8")
    _write_valid_xlsx_zip(src_xlsx)
    os.utime(src_csv, (slot_ts - 300, slot_ts - 300))

    fake_now = [slot_ts]

    def _clock() -> float:
        return fake_now[0]

    def _sleep(sec: float) -> None:
        fake_now[0] += max(sec, 50.0)

    exit_code_1, out_1 = bridge_mod.run_bbg_mirror_sync(
        data_root=data_root,
        openclaw_workspace=openclaw_ws,
        scheduled_slot_utc=slot_dt.isoformat(),
        timeout_s=120.0,
        poll_interval_s=15.0,
        clock_fn=_clock,
        sleep_fn=_sleep,
        ps_list_fn=empty_ps_fn,
    )
    assert exit_code_1 == 1
    out_lines_1 = out_1.strip().splitlines()
    assert any("waiting for openclaw update:" in ln for ln in out_lines_1)
    payload_1 = json.loads(out_lines_1[-1])
    assert payload_1["ok"] is False
    assert payload_1["reason"] == "openclaw_not_updated"

    # Case 2: Validation failure (e.g. < 252 rows, < 20 columns, or formula error) does NOT overwrite existing files in <data_root>/temp/
    dest_csv.write_text("existing-good-csv-content\n", encoding="utf-8")
    dest_xlsx.write_bytes(b"existing-good-xlsx-bytes")

    src_csv.write_text(_make_valid_bbg_csv_content(100), encoding="utf-8")  # only 100 rows (< 252)
    os.utime(src_csv, (slot_ts + 10, slot_ts + 10))
    fake_now[0] = slot_ts + 20

    exit_code_2, out_2 = bridge_mod.run_bbg_mirror_sync(
        data_root=data_root,
        openclaw_workspace=openclaw_ws,
        scheduled_slot_utc=slot_dt.isoformat(),
        timeout_s=120.0,
        poll_interval_s=0.01,
        clock_fn=_clock,
        sleep_fn=_sleep,
        ps_list_fn=empty_ps_fn,
    )
    assert exit_code_2 == 1
    payload_2 = json.loads(out_2.strip().splitlines()[-1])
    assert payload_2["ok"] is False
    assert dest_csv.read_text(encoding="utf-8") == "existing-good-csv-content\n"
    assert dest_xlsx.read_bytes() == b"existing-good-xlsx-bytes"

    # Verify CSV with < 20 columns (e.g. 14 columns) fails validation without fallback
    fourteen_col_lines = ["c" + str(i) for i in range(14)]
    bad_cols_csv = ",".join(fourteen_col_lines) + "\n" + "\n".join(
        ",".join(["2025-01-01", "100.0"] * 7) for _ in range(260)
    ) + "\n"
    src_csv.write_text(bad_cols_csv, encoding="utf-8")
    os.utime(src_csv, (slot_ts + 12, slot_ts + 12))
    exit_code_2col, out_2col = bridge_mod.run_bbg_mirror_sync(
        data_root=data_root,
        openclaw_workspace=openclaw_ws,
        scheduled_slot_utc=slot_dt.isoformat(),
        timeout_s=120.0,
        poll_interval_s=0.01,
        clock_fn=_clock,
        sleep_fn=_sleep,
        ps_list_fn=empty_ps_fn,
    )
    assert exit_code_2col == 1
    payload_2col = json.loads(out_2col.strip().splitlines()[-1])
    assert payload_2col["ok"] is False
    assert payload_2col["reason"] == "csv_columns_below_20:14"
    assert dest_csv.read_text(encoding="utf-8") == "existing-good-csv-content\n"

    # Also test corrupt xlsx zip does not overwrite existing files
    src_csv.write_text(_make_valid_bbg_csv_content(260), encoding="utf-8")
    os.utime(src_csv, (slot_ts + 15, slot_ts + 15))
    src_xlsx.write_bytes(b"not-a-valid-zip-archive")

    exit_code_2b, _ = bridge_mod.run_bbg_mirror_sync(
        data_root=data_root,
        openclaw_workspace=openclaw_ws,
        scheduled_slot_utc=slot_dt.isoformat(),
        timeout_s=120.0,
        poll_interval_s=0.01,
        clock_fn=_clock,
        sleep_fn=_sleep,
        ps_list_fn=empty_ps_fn,
    )
    assert exit_code_2b == 1
    assert dest_csv.read_text(encoding="utf-8") == "existing-good-csv-content\n"
    assert dest_xlsx.read_bytes() == b"existing-good-xlsx-bytes"

    # Case 3: OpenClaw's own `bbg_remote_refresh.sh` is still running on first 2 polls, then exits -> waits and succeeds
    _write_valid_xlsx_zip(src_xlsx)
    os.utime(src_csv, (slot_ts + 25, slot_ts + 25))
    fake_now[0] = slot_ts

    def _sleep_short(sec: float) -> None:
        fake_now[0] += min(sec, 5.0)

    ps_wait_openclaw = create_autospec(
        bridge_mod._list_host_processes_ps,
        side_effect=[
            [
                (
                    4444,
                    4444,
                    "Sat Nov  7 15:00:01 2026",
                    "bash /Users/everflow/.openclaw/workspace/scripts/bbg_remote_refresh.sh",
                )
            ],
            [
                (
                    4444,
                    4444,
                    "Sat Nov  7 15:00:01 2026",
                    "bash /Users/everflow/.openclaw/workspace/scripts/bbg_remote_refresh.sh",
                )
            ],
            [],
        ],
    )

    exit_code_3, out_3 = bridge_mod.run_bbg_mirror_sync(
        data_root=data_root,
        openclaw_workspace=openclaw_ws,
        scheduled_slot_utc=slot_dt.isoformat(),
        timeout_s=120.0,
        poll_interval_s=1.0,
        clock_fn=_clock,
        sleep_fn=_sleep_short,
        ps_list_fn=ps_wait_openclaw,
    )
    assert exit_code_3 == 0
    assert ps_wait_openclaw.call_count == 3
    out_3_lines = out_3.strip().splitlines()
    assert len(out_3_lines) == 3  # 2 progress lines + 1 final JSON line
    assert "openclaw_running=True" in out_3_lines[0]
    assert "openclaw_running=True" in out_3_lines[1]
    payload_3 = json.loads(out_3_lines[-1])
    assert payload_3["ok"] is True
    assert payload_3["lines"] == 261
    assert payload_3["source"] == "openclaw_mirror"
    assert payload_3["path"] == str(dest_csv)
    assert isinstance(payload_3["timestamp"], str) and payload_3["timestamp"].endswith("Z")
    assert set(payload_3["detail"]["counts"].keys()) == set(bridge_mod.BBG_REQUIRED_SERIES)
    assert all(c == 260 for c in payload_3["detail"]["counts"].values())
    assert set(payload_3["detail"]["last_dates"].keys()) == set(bridge_mod.BBG_REQUIRED_SERIES)


@pytest.mark.asyncio
async def test_scheduler_host_task_execution_success_timeout_reclaiming_and_bridge_unreachable(
    tmp_path: Path,
) -> None:
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    claimed = ClaimedTaskRun(
        run_id="run-host-exec-1",
        job_key=T29_JOB_KEY,
        generation=10,
        runner="host",
        command_argv=["bash", "/Users/everflow/.openclaw/workspace/scripts/bbg_remote_refresh.sh"],
        workdir="/Users/everflow/.openclaw/workspace",
        timeout_seconds=900,
        no_output_timeout_seconds=420,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=False,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=15),
        timezone="America/New_York",
        host_mode="mirror",
    )

    # 1. Normal host task execution -> succeeded
    host_client = create_autospec(DataTaskHostBridgeClient, instance=True)
    mirror_json = json.dumps(
        {
            "ok": True,
            "lines": 260,
            "source": "openclaw_mirror",
            "path": "/var/dalton/openclaw-data/temp/bbg_data_latest.csv",
            "timestamp": "2026-11-07T15:01:00Z",
            "detail": {"counts": {}, "last_dates": {}},
        }
    )
    host_client.run_host_task.return_value = HostRunOutcome(
        ok=True,
        status_code=200,
        exit_code=0,
        timed_out=False,
        timeout_kind=None,
        log_tail=mirror_json + "\n",
    )
    lock_conn_ok, _ = _make_autospec_db(
        [
            {"fetchone": (1,)},  # start_claimed_host_run pre-write
            {"fetchone": (1,)},  # start_claimed_host_run post-write
            {"fetchone": (1,)},  # before run_host_task
            {"fetchone": (1,)},  # finalize_task_run pre-write
            {"fetchone": (1,)},  # finalize_task_run before slot release
        ]
    )
    conn_ok, state_ok = _make_autospec_db(
        [
            {"rowcount": 1},  # UPDATE runs SET status = 'running'
            {"rowcount": 1},  # UPDATE runs SET status = 'succeeded'
            {"rowcount": 1, "fetchone": {"consecutive_failures": 0}},  # UPDATE defs
            {"rowcount": 1},  # UPDATE slot SET slot_state = 'idle'
        ]
    )
    committed_ok, cf_ok = await execute_claimed_host_run(
        conn_ok,
        lock_conn=lock_conn_ok,
        host_client=host_client,
        claimed=claimed,
        path_map=path_map,
    )
    assert committed_ok is True
    assert cf_ok == 0
    host_client.run_host_task.assert_awaited_once_with(
        generation=10,
        run_id="run-host-exec-1",
        job_key=T29_JOB_KEY,
        host_mode="mirror",
        scheduled_slot_utc=now.isoformat(),
        timeout=900,
        no_output_timeout=420,
    )
    assert state_ok["executed"][1][1]["terminal_status"] == "succeeded"
    assert "SET slot_state     = 'idle'" in state_ok["executed"][3][0]

    # 2. Timeout with confirmed host-kill: first increments generation in transaction, then calls
    #    host-kill with new generation, then writes timed_out and releases slot to 'idle'
    call_order: list[str] = []
    host_client_to = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client_to.run_host_task.return_value = HostRunOutcome(
        ok=False,
        status_code=200,
        exit_code=124,
        timed_out=True,
        timeout_kind="total",
        log_tail="timed out\n",
    )

    async def _on_kill_confirmed(
        *, generation: int, run_id: str | int | None = None, reason: str | None = None
    ) -> HostKillOutcome:
        call_order.append(f"host_kill:gen={generation}")
        return HostKillOutcome(
            ok=True,
            status_code=200,
            confirmed=True,
            max_seen_generation=generation,
            stopped_pgids=[12345],
            remaining_pgids=[],
        )

    host_client_to.kill_host_tasks.side_effect = _on_kill_confirmed
    lock_conn_to, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(8)])
    conn_to, state_to = _make_autospec_db(
        [
            {"rowcount": 1},  # start_claimed_host_run: UPDATE runs SET status='running'
            {"rowcount": 1, "fetchone": {"generation": 11}},  # bump_slot_generation: UPDATE slot SET generation = generation + 1
            {"rowcount": 1},  # finalize_task_run: UPDATE runs SET status='timed_out'
            {"rowcount": 1, "fetchone": {"consecutive_failures": 1}},  # UPDATE defs
            {"rowcount": 1},  # UPDATE slot SET slot_state = 'idle'
        ]
    )
    orig_tx_to = conn_to.transaction.side_effect

    @asynccontextmanager
    async def _tracked_tx_to():
        before_len = len(state_to["executed"])
        async with orig_tx_to():
            yield
        for sql, _ in state_to["executed"][before_len:]:
            if "SET generation = generation + 1" in sql:
                call_order.append("bump_generation_tx")
            elif "SET status          = %(terminal_status)s" in sql:
                call_order.append("terminal_status_tx")

    conn_to.transaction.side_effect = _tracked_tx_to

    committed_to, cf_to = await execute_claimed_host_run(
        conn_to,
        lock_conn=lock_conn_to,
        host_client=host_client_to,
        claimed=claimed,
        path_map=path_map,
    )
    assert committed_to is True
    assert cf_to == 1
    assert call_order == ["bump_generation_tx", "host_kill:gen=11", "terminal_status_tx"]
    assert state_to["executed"][2][1]["terminal_status"] == "timed_out"

    # 3. Timeout with UNCONFIRMED host-kill: keeps slot in 'reclaiming' and does NOT write terminal status
    host_client_unconf = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client_unconf.run_host_task.return_value = HostRunOutcome(
        ok=False,
        status_code=200,
        exit_code=124,
        timed_out=True,
        timeout_kind="total",
        log_tail="",
    )
    host_client_unconf.kill_host_tasks.return_value = HostKillOutcome(
        ok=False,
        status_code=200,
        confirmed=False,
        max_seen_generation=11,
        stopped_pgids=[],
        remaining_pgids=[99999],
        failure_reason="host_kill_unconfirmed",
    )
    lock_conn_unconf, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(8)])
    conn_unconf, state_unconf = _make_autospec_db(
        [
            {"rowcount": 1},  # start_claimed_host_run: UPDATE runs SET status='running'
            {"rowcount": 1, "fetchone": {"generation": 11}},  # bump_slot_generation
            {"rowcount": 1},  # finalize_task_run: UPDATE slot SET slot_state='reclaiming'
        ]
    )
    committed_unconf, cf_unconf = await execute_claimed_host_run(
        conn_unconf,
        lock_conn=lock_conn_unconf,
        host_client=host_client_unconf,
        claimed=claimed,
        path_map=path_map,
    )
    assert committed_unconf is False
    assert cf_unconf is None
    assert "SET slot_state = 'reclaiming'" in state_unconf["executed"][-1][0]
    assert not any(
        "SET status          = %(terminal_status)s" in sql for sql, _ in state_unconf["executed"]
    )

    # 4. Takeover recovery with unconfirmed host-kill keeps slot in 'reclaiming'; confirmed aborts and releases
    docker_client_takeover = create_autospec(DataTaskDockerClient, instance=True)
    docker_client_takeover.list_data_task_containers.return_value = []
    lock_conn_rec, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(8)])
    conn_rec_unconf, state_rec_unconf = _make_autospec_db(
        [
            {
                "fetchone": {
                    "slot_id": 1,
                    "generation": 11,
                    "slot_state": "running",
                    "active_run_id": "run-host-exec-1",
                    "active_job_key": T29_JOB_KEY,
                }
            },
            {
                "fetchone": {
                    "run_id": "run-host-exec-1",
                    "job_key": T29_JOB_KEY,
                    "status": "running",
                    "container_id": None,
                    "runner": "host",
                }
            },
            {"rowcount": 1, "fetchone": {"generation": 12}},  # bump_slot_generation
            {"rowcount": 1},  # UPDATE slot SET slot_state = 'reclaiming'
        ]
    )
    rec_outcome_unconf = await recover_on_takeover(
        conn_rec_unconf,
        lock_conn=lock_conn_rec,
        docker_client=docker_client_takeover,
        host_client=host_client_unconf,
        path_map=path_map,
    )
    assert rec_outcome_unconf == "reclaiming"
    assert "SET slot_state = 'reclaiming'" in state_rec_unconf["executed"][-1][0]

    # 5. Bridge unreachable / auth failure -> marks run 'failed' with clear reason and releases slot
    host_client_unreach = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client_unreach.run_host_task.return_value = HostRunOutcome(
        ok=False,
        status_code=0,
        exit_code=None,
        timed_out=False,
        timeout_kind=None,
        log_tail="",
        failure_reason="host_bridge_unreachable: ConnectError",
        bridge_unreachable=True,
        auth_failed=False,
    )
    lock_conn_unr, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(6)])
    conn_unr, state_unr = _make_autospec_db(
        [
            {"rowcount": 1},  # start_claimed_host_run
            {"rowcount": 1},  # finalize_task_run: UPDATE runs SET status='failed'
            {"rowcount": 1, "fetchone": {"consecutive_failures": 1}},  # UPDATE defs
            {"rowcount": 1},  # UPDATE slot SET slot_state='idle'
        ]
    )
    committed_unr, cf_unr = await execute_claimed_host_run(
        conn_unr,
        lock_conn=lock_conn_unr,
        host_client=host_client_unreach,
        claimed=claimed,
        path_map=path_map,
    )
    assert committed_unr is True
    assert cf_unr == 1
    assert state_unr["executed"][1][1]["terminal_status"] == "failed"
    assert "host_bridge_unreachable" in state_unr["executed"][1][1]["failure_reason"]
    assert "SET slot_state     = 'idle'" in state_unr["executed"][3][0]


@pytest.mark.asyncio
async def test_host_runner_client_and_bump_slot_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.data_jobs.host_runner as hr_mod

    monkeypatch.setattr(
        hr_mod,
        "get_dalton_bridge_base_url",
        create_autospec(hr_mod.get_dalton_bridge_base_url, return_value="http://127.0.0.1:18799"),
    )
    monkeypatch.setattr(
        hr_mod,
        "get_dalton_bridge_admin_token",
        create_autospec(hr_mod.get_dalton_bridge_admin_token, return_value=""),
    )

    # Missing DALTON_BRIDGE_ADMIN_TOKEN returns auth_failed=True without network call
    res_no_token = await post_bridge_host_run(
        generation=1,
        run_id="run-no-tok",
        job_key=T1_JOB_KEY,
        host_mode="mirror",
        scheduled_slot_utc="2026-11-07T10:00:00Z",
        timeout=60,
        no_output_timeout=30,
    )
    assert res_no_token.ok is False
    assert res_no_token.auth_failed is True
    assert "DALTON_BRIDGE_ADMIN_TOKEN" in (res_no_token.failure_reason or "")

    kill_no_token = await post_bridge_host_kill(generation=2, run_id="run-no-tok")
    assert kill_no_token.confirmed is False
    assert kill_no_token.auth_failed is True

    # Verify bump_slot_generation_and_kill_host_task increments generation and passes it to host-kill
    host_client = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client.kill_host_tasks.return_value = HostKillOutcome(
        ok=True,
        status_code=200,
        confirmed=True,
        max_seen_generation=42,
    )
    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)}, {"fetchone": (1,)}])
    conn, _ = _make_autospec_db([{"rowcount": 1, "fetchone": {"generation": 42}}])
    new_gen, kill_out = await bump_slot_generation_and_kill_host_task(
        conn,
        lock_conn=lock_conn,
        host_client=host_client,
        run_id="run-42",
        reason="timeout",
    )
    assert new_gen == 42
    assert kill_out.confirmed is True
    host_client.kill_host_tasks.assert_awaited_once_with(
        generation=42,
        run_id="run-42",
        reason="timeout",
    )


@pytest.mark.asyncio
async def test_bridge_mirror_mode_production_path_refreshes_no_output_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 1 & Item 7: Mirror mode runs through real gate wrapper subprocess and real timeout/no-output monitoring."""
    import scripts.dalton.researchdata_bridge as bridge_mod

    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "test-bridge-admin-token")
    cfg = bridge_mod.BridgeConfig()
    cfg.admin_token = "test-bridge-admin-token"
    cfg.data_tasks_data_root = tmp_path / "openclaw-data"
    cfg.ocw_root = tmp_path / "openclaw-ws"
    cfg.host_task_mirror_poll_interval_s = 0.12
    cfg.host_task_sigterm_grace_s = 0.2

    ocw_temp = cfg.ocw_root / "temp"
    ocw_temp.mkdir(parents=True, exist_ok=True)
    src_csv = ocw_temp / "bbg_data_latest.csv"
    src_xlsx = ocw_temp / "bbg_data_remote.xlsx"

    slot_dt = datetime(2026, 11, 7, 15, 0, 0, tzinfo=timezone.utc)
    slot_ts = slot_dt.timestamp()
    # Make CSV older than scheduled_slot_utc so mirror waits until `timeout - 60`
    src_csv.write_text(_make_valid_bbg_csv_content(260), encoding="utf-8")
    _write_valid_xlsx_zip(src_xlsx)
    os.utime(src_csv, (slot_ts - 600, slot_ts - 600))

    tracked_procs: dict[int, tuple[str, str]] = {}
    orig_create_subproc = asyncio.create_subprocess_exec

    async def _wrapped_create_subproc(*args: Any, **kwargs: Any) -> Any:
        proc = await orig_create_subproc(*args, **kwargs)
        pgid = int(proc.pid)
        tracked_procs[pgid] = (
            "Sat Nov  7 15:00:00 2026",
            " ".join(str(a) for a in args),
        )
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _wrapped_create_subproc)

    bridge = bridge_mod.ResearchDataBridge(cfg)
    bridge._host_ps_list_fn = _make_live_ps_autospec(bridge_mod, tracked_procs)

    # Wait deadline is timeout - 60 = 61.1 - 60 = 1.1s, while no_output_timeout is 0.4s (< 1.1s!).
    # Without progress lines every 0.12s, it would be killed at 0.4s with exit_code=125 (no_output).
    status_code, resp = await bridge.handle_data_task_host_run(
        {
            "generation": 1,
            "run_id": "run-mirror-prod-path",
            "job_key": T1_JOB_KEY,
            "host_mode": "mirror",
            "scheduled_slot_utc": slot_dt.isoformat(),
            "timeout": 61.1,
            "no_output_timeout": 0.4,
        }
    )
    assert status_code == 200
    assert resp["timed_out"] is False, f"Mirror wait was killed by timeout ({resp.get('timeout_kind')}): {resp}"
    assert resp["exit_code"] == 1
    out_lines = [ln for ln in resp["stdout"].splitlines() if ln.strip()]
    assert len(out_lines) >= 3  # multiple progress lines + final JSON line
    assert all("waiting for openclaw update:" in ln for ln in out_lines[:-1])
    final_payload = json.loads(out_lines[-1])
    assert final_payload["ok"] is False
    assert final_payload["reason"] == "openclaw_not_updated"


@pytest.mark.asyncio
async def test_host_runner_connect_error_vs_result_unknown_and_scheduler_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 2 & Item 7: ConnectError/ConnectTimeout releases slot directly; ReadTimeout/ReadError/RemoteProtocolError bumps generation and calls host-kill."""
    import httpx
    import src.dalton.data_jobs.host_runner as hr_mod

    monkeypatch.setattr(
        hr_mod,
        "get_dalton_bridge_base_url",
        create_autospec(hr_mod.get_dalton_bridge_base_url, return_value="http://127.0.0.1:18799"),
    )
    monkeypatch.setattr(
        hr_mod,
        "get_dalton_bridge_admin_token",
        create_autospec(hr_mod.get_dalton_bridge_admin_token, return_value="valid-admin-token"),
    )

    # 1. ConnectError -> bridge_unreachable
    async def _post_connect_err(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", _post_connect_err)
    out_conn_err = await post_bridge_host_run(
        generation=10,
        run_id="run-conn-err",
        job_key=T1_JOB_KEY,
        host_mode="mirror",
        scheduled_slot_utc="2026-11-07T10:00:00Z",
        timeout=60,
        no_output_timeout=30,
    )
    assert out_conn_err.ok is False
    assert out_conn_err.bridge_unreachable is True
    assert out_conn_err.result_unknown is False
    assert out_conn_err.failure_reason == "bridge_unreachable"

    # 2. ReadTimeout / ReadError / RemoteProtocolError -> bridge_result_unknown
    for err_cls in (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError):
        async def _post_after_sent(self: Any, *args: Any, _cls: Any = err_cls, **kwargs: Any) -> Any:
            raise _cls("connection dropped after request sent")

        monkeypatch.setattr(httpx.AsyncClient, "post", _post_after_sent)
        out_unknown = await post_bridge_host_run(
            generation=10,
            run_id="run-read-err",
            job_key=T1_JOB_KEY,
            host_mode="mirror",
            scheduled_slot_utc="2026-11-07T10:00:00Z",
            timeout=60,
            no_output_timeout=30,
        )
        assert out_unknown.ok is False
        assert out_unknown.result_unknown is True
        assert out_unknown.bridge_unreachable is False
        assert out_unknown.failure_reason == "bridge_result_unknown"

    # 3. Scheduler execute_claimed_host_run on bridge_result_unknown:
    #    (a) confirmed host-kill -> marks `failed` with `failure_reason='bridge_result_unknown'` and releases slot
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    claimed = ClaimedTaskRun(
        run_id="run-unknown-1",
        job_key=T1_JOB_KEY,
        generation=20,
        runner="host",
        command_argv=["bash", "/Users/everflow/.openclaw/workspace/scripts/bbg_remote_refresh.sh"],
        workdir="/Users/everflow/.openclaw/workspace",
        timeout_seconds=900,
        no_output_timeout_seconds=420,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=False,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=15),
        timezone="America/New_York",
        host_mode="mirror",
    )

    host_client_conf = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client_conf.run_host_task.return_value = HostRunOutcome(
        ok=False,
        status_code=0,
        exit_code=None,
        failure_reason="bridge_result_unknown",
        result_unknown=True,
    )
    host_client_conf.kill_host_tasks.return_value = HostKillOutcome(
        ok=True,
        confirmed=True,
        status_code=200,
        max_seen_generation=21,
        stopped_pgids=[54321],
        remaining_pgids=[],
    )
    lock_conn_1, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(8)])
    conn_1, state_1 = _make_autospec_db(
        [
            {"rowcount": 1},  # start_claimed_host_run
            {"rowcount": 1, "fetchone": {"generation": 21}},  # bump_slot_generation
            {"rowcount": 1},  # finalize_task_run: UPDATE runs SET status='failed'
            {"rowcount": 1, "fetchone": {"consecutive_failures": 1}},  # UPDATE defs
            {"rowcount": 1},  # UPDATE slot SET slot_state='idle'
        ]
    )
    committed_1, cf_1 = await execute_claimed_host_run(
        conn_1,
        lock_conn=lock_conn_1,
        host_client=host_client_conf,
        claimed=claimed,
        path_map=path_map,
    )
    assert committed_1 is True
    assert cf_1 == 1
    host_client_conf.kill_host_tasks.assert_awaited_once_with(
        generation=21,
        run_id="run-unknown-1",
        reason="bridge_result_unknown",
    )
    assert state_1["executed"][2][1]["terminal_status"] == "failed"
    assert state_1["executed"][2][1]["failure_reason"] == "bridge_result_unknown"
    assert "SET slot_state     = 'idle'" in state_1["executed"][4][0]

    #    (b) unconfirmed host-kill -> keeps slot in 'reclaiming' and does NOT write terminal status
    host_client_unconf = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client_unconf.run_host_task.return_value = HostRunOutcome(
        ok=False,
        status_code=0,
        exit_code=None,
        failure_reason="bridge_result_unknown",
        result_unknown=True,
    )
    host_client_unconf.kill_host_tasks.return_value = HostKillOutcome(
        ok=False,
        confirmed=False,
        status_code=200,
        max_seen_generation=22,
        stopped_pgids=[],
        remaining_pgids=[54321],
    )
    lock_conn_2, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(8)])
    conn_2, state_2 = _make_autospec_db(
        [
            {"rowcount": 1},  # start_claimed_host_run
            {"rowcount": 1, "fetchone": {"generation": 22}},  # bump_slot_generation
            {"rowcount": 1},  # finalize_task_run: UPDATE slot SET slot_state='reclaiming'
        ]
    )
    committed_2, cf_2 = await execute_claimed_host_run(
        conn_2,
        lock_conn=lock_conn_2,
        host_client=host_client_unconf,
        claimed=claimed,
        path_map=path_map,
    )
    assert committed_2 is False
    assert cf_2 is None
    assert "SET slot_state = 'reclaiming'" in state_2["executed"][-1][0]


@pytest.mark.asyncio
async def test_takeover_stops_host_task_even_when_container_stop_fails(
    tmp_path: Path,
) -> None:
    """Item 3 & Item 7: `recover_on_takeover` still bumps generation and calls `host-kill` for a non-terminal host run even if a leftover container fails to stop."""
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    docker_client = create_autospec(DataTaskDockerClient, instance=True)
    docker_client.list_data_task_containers.return_value = [
        {"container_id": "cid-stuck", "name": "dalton-dt-old", "running": True}
    ]
    # Container refuses to stop (`Running: True`)
    docker_client.inspect_container.return_value = {"State": {"Running": True}}

    host_client = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client.kill_host_tasks.return_value = HostKillOutcome(
        ok=True,
        confirmed=True,
        status_code=200,
        max_seen_generation=31,
        stopped_pgids=[77777],
        remaining_pgids=[],
    )

    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(12)])
    conn, state = _make_autospec_db(
        [
            {
                "fetchone": {
                    "slot_id": 1,
                    "generation": 30,
                    "slot_state": "running",
                    "active_run_id": "run-host-active",
                    "active_job_key": T1_JOB_KEY,
                }
            },
            {
                "fetchone": {
                    "run_id": "run-host-active",
                    "job_key": T1_JOB_KEY,
                    "status": "running",
                    "container_id": None,
                    "runner": "host",
                }
            },
            {"rowcount": 1, "fetchone": {"generation": 31}},  # bump_slot_generation
            {"rowcount": 1},  # UPDATE runs SET status = 'aborted'
            {"rowcount": 1},  # UPDATE defs SET consecutive_failures = consecutive_failures + 1
            {"rowcount": 1},  # UPDATE slot SET slot_state = 'reclaiming' (because container is still running)
        ]
    )

    outcome = await recover_on_takeover(
        conn,
        lock_conn=lock_conn,
        docker_client=docker_client,
        host_client=host_client,
        path_map=path_map,
    )
    assert outcome == "reclaiming"
    host_client.kill_host_tasks.assert_awaited_once_with(
        generation=31,
        run_id="run-host-active",
        reason="scheduler_takeover",
    )
    assert any("SET status         = 'aborted'" in sql for sql, _ in state["executed"])
    assert "SET slot_state = 'reclaiming'" in state["executed"][-1][0]


@pytest.mark.asyncio
async def test_process_identity_lstart_mismatch_not_killed_and_child_cmd_prevents_exit_and_ps_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4 & Item 7:
    - Reused `pgid` (`lstart` mismatch, even with `bbg_remote_refresh.sh` in command) is NOT killed and stale record is dropped;
    - Child process in group with different command (`sleep 60`) prevents exit confirmation and triggers `SIGKILL`;
    - `ps` failure (`None`) returns `confirmed=False` and preserves state record.
    """
    import signal
    import scripts.dalton.researchdata_bridge as bridge_mod

    killpg_calls: list[tuple[int, int]] = []

    def _record_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, int(sig)))

    monkeypatch.setattr(os, "killpg", create_autospec(os.killpg, side_effect=_record_killpg))

    cfg = bridge_mod.BridgeConfig()
    cfg.admin_token = "test-bridge-admin-token"
    cfg.data_tasks_data_root = tmp_path / "openclaw-data"
    cfg.host_task_sigterm_grace_s = 0.1

    # 1. Reused pgid (recorded lstart != current leader lstart, even when reused by OpenClaw's bbg_remote_refresh.sh):
    #    Must NOT call os.killpg, and must remove the stale record from the state file.
    bridge = bridge_mod.ResearchDataBridge(cfg)
    bridge._write_host_runner_state_file_sync(
        max_seen_generation=10,
        active_runs={
            4242: {
                "pgid": 4242,
                "pid": 4242,
                "lstart": "Sat Nov 7 08:00:00 2026",
                "run_id": "run-old",
                "job_key": T1_JOB_KEY,
                "generation": 10,
                "host_mode": "run",
            }
        },
    )
    bridge._host_ps_list_fn = create_autospec(
        bridge_mod._list_host_processes_ps,
        return_value=[
            (
                4242,
                4242,
                "Sat Nov  7 09:30:00 2026",  # different start time!
                "bash /Users/everflow/.openclaw/workspace/scripts/bbg_remote_refresh.sh",
            )
        ],
    )
    status_1, resp_1 = await bridge.handle_data_task_host_kill({"generation": 11})
    assert status_1 == 200
    assert resp_1["confirmed"] is True
    assert killpg_calls == [], "Must NOT kill pgid when leader lstart does not match recorded lstart"
    saved_1 = json.loads(bridge._host_runner_state_file().read_text(encoding="utf-8"))
    assert saved_1["active_runs"] == []
    assert saved_1["max_seen_generation"] == 11

    # 2. Group leader matches `lstart`, then after SIGTERM the leader exits but a child with a different
    #    command (`sleep 60`, no `bbg_remote_refresh.sh` in command) remains in `pgid` -> NOT exited, escalates to SIGKILL!
    killpg_calls.clear()
    # The fake process table follows the signals actually sent, not a call count,
    # so the result does not depend on how many times the grace loop polls.
    def _table_after_signals() -> list[tuple[int, int, str, str]]:
        sent = {sig for (pg, sig) in killpg_calls if pg == 5151}
        if int(signal.SIGKILL) in sent:
            return []
        child = (5152, 5151, "Sat Nov  7 10:00:01 2026", "sleep 60")
        if int(signal.SIGTERM) in sent:
            return [child]  # the leader exited on SIGTERM, its child ignores it
        return [
            (5151, 5151, "Sat Nov  7 10:00:00 2026", f"python {bridge_mod.HOST_TASK_GATE_FLAG}"),
            child,
        ]

    ps_child_remains = create_autospec(
        bridge_mod._list_host_processes_ps, side_effect=_table_after_signals
    )
    stopped_2, remaining_2 = bridge_mod._stop_and_verify_host_pgids_sync(
        [{"pgid": 5151, "lstart": "Sat Nov 7 10:00:00 2026"}],
        sigterm_grace_s=0.06,
        sigkill_wait_s=0.06,
        ps_list_fn=ps_child_remains,
    )
    assert stopped_2 == [5151]
    assert remaining_2 == []
    assert (5151, int(signal.SIGTERM)) in killpg_calls
    assert (5151, int(signal.SIGKILL)) in killpg_calls

    # 3. If `ps` fails (returns `None`), treat as unconfirmed: do NOT delete record and return `confirmed=False`
    killpg_calls.clear()
    bridge._write_host_runner_state_file_sync(
        max_seen_generation=12,
        active_runs={
            6161: {
                "pgid": 6161,
                "pid": 6161,
                "lstart": "Sat Nov 7 11:00:00 2026",
                "run_id": "run-6161",
                "job_key": T1_JOB_KEY,
                "generation": 12,
                "host_mode": "run",
            }
        },
    )
    bridge._host_ps_list_fn = create_autospec(
        bridge_mod._list_host_processes_ps,
        return_value=None,
    )
    status_3, resp_3 = await bridge.handle_data_task_host_kill({"generation": 13})
    assert status_3 == 200
    assert resp_3["confirmed"] is False
    assert resp_3["ok"] is False
    assert 6161 in resp_3["remaining_pgids"]
    saved_3 = json.loads(bridge._host_runner_state_file().read_text(encoding="utf-8"))
    assert len(saved_3["active_runs"]) == 1
    assert saved_3["active_runs"][0]["pgid"] == 6161


@pytest.mark.asyncio
async def test_corrupt_state_file_archives_fence_lost_scans_stops_and_recovers_on_host_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 5 & Item 7:
    - Corrupt state file is archived as `.dalton-host-runner-state.json.corrupt-<UTC>`;
    - Bridge enters `fence_lost` (`host-run` returns 409 `{"error": "fence_lost"}`);
    - Scans `ps` and stops only Dalton groups (`--dalton-host-task-gate` or `<data_root>/scripts/bbg_remote_refresh.sh`), never bare `bbg_remote_refresh.sh`;
    - Recovers from `fence_lost` after confirmed `host-kill`, setting `max_seen_generation` to the `host-kill` generation;
    - Scheduler `execute_claimed_host_run` on `fence_lost` increments generation + 1, calls `host-kill`, and retries `host-run` once.
    """
    import scripts.dalton.researchdata_bridge as bridge_mod

    killpg_calls: list[tuple[int, int]] = []
    alive_pgids: set[int] = {7001, 7002, 7003}

    def _record_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, int(sig)))
        alive_pgids.discard(pgid)

    monkeypatch.setattr(os, "killpg", create_autospec(os.killpg, side_effect=_record_killpg))

    cfg = bridge_mod.BridgeConfig()
    cfg.admin_token = "test-bridge-admin-token"
    cfg.data_tasks_data_root = tmp_path / "openclaw-data"
    cfg.host_task_sigterm_grace_s = 0.06
    cfg.data_tasks_data_root.mkdir(parents=True, exist_ok=True)

    state_file = cfg.data_tasks_data_root / ".dalton-host-runner-state.json"
    state_file.write_text("{corrupt-json-content", encoding="utf-8")

    dalton_script = str(cfg.data_tasks_data_root / "scripts" / "bbg_remote_refresh.sh")
    openclaw_script = "/Users/everflow/.openclaw/workspace/scripts/bbg_remote_refresh.sh"

    # First call during scan sees 3 groups:
    # - pgid 7001: Dalton gate wrapper -> MUST be stopped
    # - pgid 7002: Dalton data_root script -> MUST be stopped
    # - pgid 7003: OpenClaw's own script -> MUST NOT be stopped!
    all_scan_rows = [
        (7001, 7001, "Sat Nov  7 12:00:00 2026", f"python3 {bridge_mod.HOST_TASK_GATE_FLAG} spec"),
        (7002, 7002, "Sat Nov  7 12:00:01 2026", f"bash {dalton_script}"),
        (7003, 7003, "Sat Nov  7 12:00:02 2026", f"bash {openclaw_script}"),
    ]

    def _ps_scan_side_effect() -> list[tuple[int, int, str, str]]:
        return [r for r in all_scan_rows if r[1] in alive_pgids]

    bridge = bridge_mod.ResearchDataBridge(cfg)
    bridge._host_ps_list_fn = create_autospec(
        bridge_mod._list_host_processes_ps,
        side_effect=_ps_scan_side_effect,
    )

    rec = await bridge.recover_host_runner_state_on_startup()
    assert rec["fence_lost"] is True
    assert bridge._host_fence_lost is True
    assert state_file.is_file()
    persisted_fence_lost = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted_fence_lost["fence_lost"] is True
    assert persisted_fence_lost["max_seen_generation"] == 0
    assert persisted_fence_lost["active_runs"] == []
    corrupt_archives = list(cfg.data_tasks_data_root.glob(".dalton-host-runner-state.json.corrupt-*"))
    assert len(corrupt_archives) == 1
    assert corrupt_archives[0].read_text(encoding="utf-8") == "{corrupt-json-content"

    # Verify only pgids 7001 and 7002 were signaled, NEVER OpenClaw's 7003
    signaled_pgids = {p for p, _ in killpg_calls}
    assert signaled_pgids == {7001, 7002}
    assert 7003 not in signaled_pgids
    assert alive_pgids == {7003}

    # While in fence_lost, host-run returns 409 {"error": "fence_lost"}
    run_status, run_resp = await bridge.handle_data_task_host_run(
        {
            "generation": 15,
            "run_id": "run-during-fence-lost",
            "job_key": T1_JOB_KEY,
            "host_mode": "run",
            "scheduled_slot_utc": "2026-11-07T12:00:00Z",
            "timeout": 30,
            "no_output_timeout": 15,
        }
    )
    assert run_status == 409
    assert run_resp["error"] == "fence_lost"

    # Receiving a confirmed host-kill sets max_seen_generation and exits fence_lost
    kill_status, kill_resp = await bridge.handle_data_task_host_kill({"generation": 25})
    assert kill_status == 200
    assert kill_resp["confirmed"] is True
    assert kill_resp["max_seen_generation"] == 25
    assert bridge._host_fence_lost is False
    saved_recovered = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved_recovered["max_seen_generation"] == 25

    # Scheduler side: receiving `fence_lost` increments generation + 1, calls `host-kill`, and retries `host-run` once
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    claimed = ClaimedTaskRun(
        run_id="run-fence-retry",
        job_key=T1_JOB_KEY,
        generation=30,
        runner="host",
        command_argv=["bash", "/Users/everflow/.openclaw/workspace/scripts/bbg_remote_refresh.sh"],
        workdir="/Users/everflow/.openclaw/workspace",
        timeout_seconds=900,
        no_output_timeout_seconds=420,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=False,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=15),
        timezone="America/New_York",
        host_mode="mirror",
    )
    mirror_ok_json = json.dumps(
        {
            "ok": True,
            "lines": 260,
            "source": "openclaw_mirror",
            "path": "/var/dalton/openclaw-data/temp/bbg_data_latest.csv",
            "timestamp": "2026-11-07T15:01:00Z",
            "detail": {"counts": {}, "last_dates": {}},
        }
    )
    host_client_retry = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client_retry.run_host_task.side_effect = [
        HostRunOutcome(
            ok=False,
            status_code=409,
            exit_code=None,
            failure_reason="fence_lost",
            fence_lost=True,
        ),
        HostRunOutcome(
            ok=True,
            status_code=200,
            exit_code=0,
            log_tail=mirror_ok_json + "\n",
        ),
    ]
    host_client_retry.kill_host_tasks.return_value = HostKillOutcome(
        ok=True,
        confirmed=True,
        status_code=200,
        max_seen_generation=31,
    )
    lock_conn_retry, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(10)])
    conn_retry, state_retry = _make_autospec_db(
        [
            {"rowcount": 1},  # start_claimed_host_run
            {"rowcount": 1, "fetchone": {"generation": 31}},  # bump_slot_generation on fence_lost
            {"rowcount": 1},  # UPDATE runs SET generation = 31
            {"rowcount": 1},  # finalize_task_run: UPDATE runs SET status = 'succeeded'
            {"rowcount": 1, "fetchone": {"consecutive_failures": 0}},  # UPDATE defs
            {"rowcount": 1},  # UPDATE slot SET slot_state = 'idle'
        ]
    )
    committed_retry, cf_retry = await execute_claimed_host_run(
        conn_retry,
        lock_conn=lock_conn_retry,
        host_client=host_client_retry,
        claimed=claimed,
        path_map=path_map,
    )
    assert committed_retry is True
    assert cf_retry == 0
    assert host_client_retry.run_host_task.await_count == 2
    assert host_client_retry.run_host_task.await_args_list[0].kwargs["generation"] == 30
    assert host_client_retry.run_host_task.await_args_list[1].kwargs["generation"] == 31
    host_client_retry.kill_host_tasks.assert_awaited_once_with(
        generation=31,
        run_id="run-fence-retry",
        reason="fence_lost",
    )
    assert state_retry["executed"][3][1]["terminal_status"] == "succeeded"

    # If the second host-run after fence_lost recovery still fails, marks run `failed`
    claimed_fail2 = ClaimedTaskRun(
        run_id="run-fence-fail2",
        job_key=T1_JOB_KEY,
        generation=40,
        runner="host",
        command_argv=["bash", "/Users/everflow/.openclaw/workspace/scripts/bbg_remote_refresh.sh"],
        workdir="/Users/everflow/.openclaw/workspace",
        timeout_seconds=900,
        no_output_timeout_seconds=420,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=False,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=15),
        timezone="America/New_York",
        host_mode="mirror",
    )
    host_client_fail2 = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client_fail2.run_host_task.side_effect = [
        HostRunOutcome(
            ok=False,
            status_code=409,
            exit_code=None,
            failure_reason="fence_lost",
            fence_lost=True,
        ),
        HostRunOutcome(
            ok=False,
            status_code=409,
            exit_code=None,
            failure_reason="fence_lost",
            fence_lost=True,
        ),
    ]
    host_client_fail2.kill_host_tasks.return_value = HostKillOutcome(
        ok=True,
        confirmed=True,
        status_code=200,
        max_seen_generation=41,
    )
    lock_conn_f2, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(10)])
    conn_f2, state_f2 = _make_autospec_db(
        [
            {"rowcount": 1},  # start_claimed_host_run
            {"rowcount": 1, "fetchone": {"generation": 41}},  # bump_slot_generation
            {"rowcount": 1},  # UPDATE runs SET generation = 41
            {"rowcount": 1},  # finalize_task_run: UPDATE runs SET status = 'failed'
            {"rowcount": 1, "fetchone": {"consecutive_failures": 1}},  # UPDATE defs
            {"rowcount": 1},  # UPDATE slot SET slot_state = 'idle'
        ]
    )
    committed_f2, cf_f2 = await execute_claimed_host_run(
        conn_f2,
        lock_conn=lock_conn_f2,
        host_client=host_client_fail2,
        claimed=claimed_fail2,
        path_map=path_map,
    )
    assert committed_f2 is True
    assert cf_f2 == 1
    assert host_client_fail2.run_host_task.await_count == 2
    assert state_f2["executed"][3][1]["terminal_status"] == "failed"
    assert state_f2["executed"][3][1]["failure_reason"] == "fence_lost"



def test_real_ps_rows_parse_under_a_chinese_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bridge's own ps call must split lstart the same way whatever locale launchd hands it."""
    import subprocess as _sp
    import scripts.dalton.researchdata_bridge as bridge_mod

    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    proc = _sp.Popen(["sleep", "30"], start_new_session=True)
    try:
        rows = bridge_mod._list_host_processes_ps()
        if rows is None:
            pytest.skip("ps not runnable here (sandbox)")
        mine = [r for r in rows if r[0] == proc.pid]
        assert len(mine) == 1
        pid, pgid, lstart, command = mine[0]
        assert pgid == proc.pid
        assert command == "sleep 30"
        c_lstart = _sp.run(
            ["ps", "-o", "lstart=", "-p", str(proc.pid)],
            capture_output=True, text=True, check=True,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        ).stdout.strip()
        assert " ".join(lstart.split()) == " ".join(c_lstart.split())
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.asyncio
async def test_leader_exited_with_child_remaining_is_not_treated_as_stopped_and_kills_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Round 4 Item 1 & Item 3:
    When group leader (`pid == pgid`) is absent while the group still has processes:
    - If NO process in the group has a Dalton marker: do NOT send any signal, return `confirmed=False`
      (keep record in state file), and log an `ERROR` with `pgid` and group command lines;
    - If at least one process in the group has a Dalton marker (`--dalton-host-task-gate` or
      `<data_root>/scripts/bbg_remote_refresh.sh` / `bbg_export_to_csv.py`): stop and wait for
      the entire group to exit before confirming.
    """
    import logging
    import signal
    import scripts.dalton.researchdata_bridge as bridge_mod

    killpg_calls: list[tuple[int, int]] = []
    child_exits_on_kill = [False]
    child_alive = [True]
    child_cmd = ["sleep 300"]

    def _record_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, int(sig)))
        if child_exits_on_kill[0]:
            child_alive[0] = False

    monkeypatch.setattr(os, "killpg", create_autospec(os.killpg, side_effect=_record_killpg))

    cfg = bridge_mod.BridgeConfig()
    cfg.admin_token = "test-bridge-admin-token"
    cfg.data_tasks_data_root = tmp_path / "openclaw-data"
    cfg.host_task_sigterm_grace_s = 0.06

    bridge = bridge_mod.ResearchDataBridge(cfg)
    bridge._write_host_runner_state_file_sync(
        max_seen_generation=10,
        active_runs={
            8100: {
                "pgid": 8100,
                "pid": 8100,
                "lstart": "Sat Nov 7 10:00:00 2026",
                "run_id": "run-leader-gone",
                "job_key": T1_JOB_KEY,
                "generation": 10,
                "host_mode": "run",
            }
        },
    )

    def _ps_child_only() -> list[tuple[int, int, str, str]]:
        if child_alive[0]:
            return [(8105, 8100, "Sat Nov  7 10:00:02 2026", child_cmd[0])]
        return []

    bridge._host_ps_list_fn = create_autospec(
        bridge_mod._list_host_processes_ps,
        side_effect=_ps_child_only,
    )

    # 1. Leader absent and remaining child has NO Dalton marker ("sleep 300"):
    #    Must NOT send any signal, must return confirmed=False, keep record, and log ERROR
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        status_1, resp_1 = await bridge.handle_data_task_host_kill({"generation": 11})
    assert status_1 == 200
    assert resp_1["confirmed"] is False
    assert resp_1["ok"] is False
    assert resp_1["remaining_pgids"] == [8100]
    assert killpg_calls == [], "Must NOT signal group when leader is absent and no member has Dalton marker"
    assert any(
        rec.levelname == "ERROR" and "8100" in rec.getMessage() and "sleep 300" in rec.getMessage()
        for rec in caplog.records
    )
    saved_1 = json.loads(bridge._host_runner_state_file().read_text(encoding="utf-8"))
    assert [r["pgid"] for r in saved_1["active_runs"]] == [8100]

    # 2. Leader absent and remaining child HAS Dalton marker (`<data_root>/scripts/bbg_export_to_csv.py`):
    #    (a) If child refuses to exit -> signals are sent, confirmed=False
    export_script = str(cfg.data_tasks_data_root / "scripts" / "bbg_export_to_csv.py")
    child_cmd[0] = f"python3 {export_script} --input a.xlsx --output b.csv"
    status_2a, resp_2a = await bridge.handle_data_task_host_kill({"generation": 12})
    assert status_2a == 200
    assert resp_2a["confirmed"] is False
    assert resp_2a["remaining_pgids"] == [8100]
    assert (8100, int(signal.SIGTERM)) in killpg_calls
    assert (8100, int(signal.SIGKILL)) in killpg_calls

    #    (b) When child exits on signal -> confirmed=True and record removed
    killpg_calls.clear()
    child_exits_on_kill[0] = True
    status_2b, resp_2b = await bridge.handle_data_task_host_kill({"generation": 13})
    assert status_2b == 200
    assert resp_2b["confirmed"] is True
    assert resp_2b["ok"] is True
    assert resp_2b["stopped_pgids"] == [8100]
    assert resp_2b["remaining_pgids"] == []
    assert (8100, int(signal.SIGTERM)) in killpg_calls
    saved_2b = json.loads(bridge._host_runner_state_file().read_text(encoding="utf-8"))
    assert saved_2b["active_runs"] == []
    assert saved_2b["max_seen_generation"] == 13


@pytest.mark.asyncio
async def test_fence_lost_persisted_to_disk_blocks_host_run_and_rescans_on_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Item 2 & Round 4 Item 2:
    - Entering `fence_lost` first creates `<data_root>/.dalton-host-runner-fence-lost` (0600) and writes
      `{"fence_lost": true, "max_seen_generation": 0, "active_runs": [...]}` to `.dalton-host-runner-state.json`;
    - A subsequent bridge startup stays in `fence_lost`, blocks `host-run` (409), and re-scans/stops Dalton groups;
    - Only after a confirmed `host-kill` is the marker file removed and `fence_lost` written as `false`.
    """
    import scripts.dalton.researchdata_bridge as bridge_mod

    killpg_calls: list[tuple[int, int]] = []
    allow_exit = [False]
    alive_pgids: set[int] = {8201, 8202}

    def _record_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, int(sig)))
        if allow_exit[0]:
            alive_pgids.discard(pgid)

    monkeypatch.setattr(os, "killpg", create_autospec(os.killpg, side_effect=_record_killpg))

    cfg = bridge_mod.BridgeConfig()
    cfg.admin_token = "test-bridge-admin-token"
    cfg.data_tasks_data_root = tmp_path / "openclaw-data"
    cfg.host_task_sigterm_grace_s = 0.05
    cfg.data_tasks_data_root.mkdir(parents=True, exist_ok=True)

    state_file = cfg.data_tasks_data_root / ".dalton-host-runner-state.json"
    marker_file = cfg.data_tasks_data_root / ".dalton-host-runner-fence-lost"
    state_file.write_text("{corrupt-json", encoding="utf-8")

    dalton_script = str(cfg.data_tasks_data_root / "scripts" / "bbg_remote_refresh.sh")
    # 8201 has leader present; 8202 has leader absent (only child 8250 with Dalton script marker alive)
    scan_rows = [
        (8201, 8201, "Sat Nov  7 12:00:00 2026", f"python3 {bridge_mod.HOST_TASK_GATE_FLAG}"),
        (8250, 8202, "Sat Nov  7 12:00:05 2026", f"bash {dalton_script}"),
    ]

    def _ps_fn() -> list[tuple[int, int, str, str]]:
        return [r for r in scan_rows if r[1] in alive_pgids]

    # First bridge startup: creates marker file (0600), archives corrupt file, scans, persists fence_lost=True
    bridge1 = bridge_mod.ResearchDataBridge(cfg)
    bridge1._host_ps_list_fn = create_autospec(bridge_mod._list_host_processes_ps, side_effect=_ps_fn)
    rec1 = await bridge1.recover_host_runner_state_on_startup()
    assert rec1["fence_lost"] is True
    assert rec1["confirmed"] is False
    assert rec1["remaining_pgids"] == [8201, 8202]
    assert marker_file.is_file()
    assert stat.S_IMODE(marker_file.stat().st_mode) == 0o600
    marker_payload = json.loads(marker_file.read_text(encoding="utf-8"))
    assert "timestamp" in marker_payload and marker_payload["reason"] == "corrupt_state_file"

    persisted1 = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted1["fence_lost"] is True
    assert persisted1["max_seen_generation"] == 0
    assert persisted1["active_runs"] == [
        {"pgid": 8201, "lstart": "Sat Nov 7 12:00:00 2026"},
        {"pgid": 8202, "lstart": None},
    ]

    # Second bridge instance restarts -> sees marker file, stays in fence_lost and re-scans!
    killpg_calls.clear()
    allow_exit[0] = True
    bridge2 = bridge_mod.ResearchDataBridge(cfg)
    bridge2._host_ps_list_fn = create_autospec(bridge_mod._list_host_processes_ps, side_effect=_ps_fn)
    rec2 = await bridge2.recover_host_runner_state_on_startup()
    assert rec2["fence_lost"] is True
    assert bridge2._host_fence_lost is True
    assert {p for p, _ in killpg_calls} == {8201, 8202}
    assert rec2["stopped_pgids"] == [8201, 8202]
    assert rec2["remaining_pgids"] == []
    assert marker_file.is_file()

    # Even though 8201/8202 stopped on startup rescan, fence_lost remains True on disk until a confirmed host-kill
    persisted2 = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted2["fence_lost"] is True
    assert persisted2["max_seen_generation"] == 0
    assert persisted2["active_runs"] == []

    # host-run is still blocked with 409 fence_lost
    run_status, run_resp = await bridge2.handle_data_task_host_run(
        {
            "generation": 5,
            "run_id": "run-blocked",
            "job_key": T1_JOB_KEY,
            "host_mode": "run",
            "scheduled_slot_utc": "2026-11-07T12:00:00Z",
            "timeout": 30,
            "no_output_timeout": 15,
        }
    )
    assert run_status == 409
    assert run_resp["error"] == "fence_lost"

    # Confirmed host-kill removes marker file, clears fence_lost=False, and persists max_seen_generation=33
    kill_status, kill_resp = await bridge2.handle_data_task_host_kill({"generation": 33})
    assert kill_status == 200
    assert kill_resp["confirmed"] is True
    assert kill_resp["max_seen_generation"] == 33
    assert not marker_file.exists()
    persisted3 = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted3["fence_lost"] is False
    assert persisted3["max_seen_generation"] == 33
    assert persisted3["active_runs"] == []


@pytest.mark.asyncio
async def test_fence_lost_retry_rechecks_identity_and_skips_reused_non_dalton_pgids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Item 3 & Round 4 Item 1:
    On retry during `fence_lost`, previously remaining `pgid`s are re-verified:
    - `pgid` 8301 (recorded with `lstart: null` when leader was absent) whose `pgid` is now reused by a
      non-Dalton process (`pid == pgid == 8301` present, no Dalton marker in command) is NOT killed;
    - `pgid` 8302 (recorded with leader `lstart`) whose `pgid` is now reused by a process with a different
      `lstart` is NOT killed;
    - `pgid` 8303 (leader absent, child with Dalton marker `<data_root>/scripts/bbg_export_to_csv.py` still in group) IS killed.
    """
    import scripts.dalton.researchdata_bridge as bridge_mod

    killpg_calls: list[tuple[int, int]] = []
    alive_8303 = [True]

    def _record_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, int(sig)))
        if pgid == 8303:
            alive_8303[0] = False

    monkeypatch.setattr(os, "killpg", create_autospec(os.killpg, side_effect=_record_killpg))

    cfg = bridge_mod.BridgeConfig()
    cfg.admin_token = "test-bridge-admin-token"
    cfg.data_tasks_data_root = tmp_path / "openclaw-data"
    cfg.host_task_sigterm_grace_s = 0.05
    cfg.data_tasks_data_root.mkdir(parents=True, exist_ok=True)

    bridge = bridge_mod.ResearchDataBridge(cfg)
    bridge._write_fence_lost_marker_file_sync(reason="corrupt_state_file")
    bridge._write_host_runner_state_file_sync(
        fence_lost=True,
        max_seen_generation=0,
        active_runs={
            8301: {"pgid": 8301, "lstart": None},
            8302: {"pgid": 8302, "lstart": "Sat Nov 7 12:00:00 2026"},
            8303: {"pgid": 8303, "lstart": None},
        },
    )

    dalton_export = str(cfg.data_tasks_data_root / "scripts" / "bbg_export_to_csv.py")

    def _ps_on_retry() -> list[tuple[int, int, str, str]]:
        rows = [
            # 8301 reused by a non-Dalton leader (leader present, no Dalton marker, no recorded lstart)
            (8301, 8301, "Sat Nov  7 12:10:00 2026", "python3 /usr/local/bin/unrelated_daemon.py"),
            # 8302 reused by another process with different leader lstart
            (8302, 8302, "Sat Nov  7 12:11:00 2026", "bash /Users/everflow/.openclaw/workspace/scripts/bbg_remote_refresh.sh"),
        ]
        if alive_8303[0]:
            # 8303 still has its Dalton child (leader 8303 absent, child 8399 with Dalton marker alive)
            rows.append((8399, 8303, "Sat Nov  7 12:00:03 2026", f"python3 {dalton_export}"))
        return rows

    bridge._host_ps_list_fn = create_autospec(
        bridge_mod._list_host_processes_ps,
        side_effect=_ps_on_retry,
    )

    status, resp = await bridge.handle_data_task_host_kill({"generation": 40})
    assert status == 200
    assert resp["confirmed"] is True
    assert resp["max_seen_generation"] == 40
    signaled_pgids = {p for p, _ in killpg_calls}
    assert signaled_pgids == {8303}, f"Expected only 8303 to be signaled, got {signaled_pgids}"
    assert 8301 not in signaled_pgids
    assert 8302 not in signaled_pgids
    assert not bridge._host_runner_fence_lost_file().exists()

    saved = json.loads(bridge._host_runner_state_file().read_text(encoding="utf-8"))
    assert saved["fence_lost"] is False
    assert saved["max_seen_generation"] == 40
    assert saved["active_runs"] == []


@pytest.mark.asyncio
async def test_fence_lost_survives_bridge_exit_after_rename_before_new_state_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 2 & Item 3:
    Simulate bridge exiting right after renaming `.dalton-host-runner-state.json` to `.corrupt-<UTC>`
    and before writing the new `.dalton-host-runner-state.json` (or even if state file has `fence_lost: false`):
    because `<data_root>/.dalton-host-runner-fence-lost` was created FIRST, the next bridge startup
    still enters `fence_lost`, re-scans/stops Dalton groups, and blocks `host-run` with 409.
    """
    import scripts.dalton.researchdata_bridge as bridge_mod

    killpg_calls: list[tuple[int, int]] = []
    alive_pgids: set[int] = {8401}

    def _record_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, int(sig)))
        alive_pgids.discard(pgid)

    monkeypatch.setattr(os, "killpg", create_autospec(os.killpg, side_effect=_record_killpg))

    cfg = bridge_mod.BridgeConfig()
    cfg.admin_token = "test-bridge-admin-token"
    cfg.data_tasks_data_root = tmp_path / "openclaw-data"
    cfg.host_task_sigterm_grace_s = 0.05
    cfg.data_tasks_data_root.mkdir(parents=True, exist_ok=True)

    state_file = cfg.data_tasks_data_root / ".dalton-host-runner-state.json"
    marker_file = cfg.data_tasks_data_root / ".dalton-host-runner-fence-lost"
    state_file.write_text("{corrupt-state-before-crash", encoding="utf-8")

    # Simulate bridge1 crashing inside `_write_host_runner_state_file_sync` right after marker creation + archive rename
    bridge1 = bridge_mod.ResearchDataBridge(cfg)
    monkeypatch.setattr(
        bridge1,
        "_write_host_runner_state_file_sync",
        create_autospec(
            bridge1._write_host_runner_state_file_sync,
            side_effect=SystemExit("simulated crash before new state file is written"),
        ),
    )
    with pytest.raises(SystemExit):
        await bridge1.recover_host_runner_state_on_startup()

    # Verify the window state on disk: marker file exists, corrupt file was archived, `.dalton-host-runner-state.json` is missing!
    assert marker_file.is_file()
    assert not state_file.exists()
    corrupt_archives = list(cfg.data_tasks_data_root.glob(".dalton-host-runner-state.json.corrupt-*"))
    assert len(corrupt_archives) == 1

    # Now bridge2 starts up with NO `.dalton-host-runner-state.json` on disk, only `.dalton-host-runner-fence-lost`
    def _ps_fn() -> list[tuple[int, int, str, str]]:
        if 8401 in alive_pgids:
            return [(8401, 8401, "Sat Nov  7 12:20:00 2026", f"python3 {bridge_mod.HOST_TASK_GATE_FLAG}")]
        return []

    bridge2 = bridge_mod.ResearchDataBridge(cfg)
    bridge2._host_ps_list_fn = create_autospec(bridge_mod._list_host_processes_ps, side_effect=_ps_fn)
    rec2 = await bridge2.recover_host_runner_state_on_startup()
    assert rec2["fence_lost"] is True
    assert bridge2._host_fence_lost is True
    assert rec2["stopped_pgids"] == [8401]
    assert {p for p, _ in killpg_calls} == {8401}

    # `host-run` on bridge2 is still blocked with 409 `fence_lost`
    run_status, run_resp = await bridge2.handle_data_task_host_run(
        {
            "generation": 10,
            "run_id": "run-after-crash",
            "job_key": T1_JOB_KEY,
            "host_mode": "run",
            "scheduled_slot_utc": "2026-11-07T12:20:00Z",
            "timeout": 30,
            "no_output_timeout": 15,
        }
    )
    assert run_status == 409
    assert run_resp["error"] == "fence_lost"

    # Even if state file on disk has `fence_lost: false` while marker file still exists, a 3rd bridge startup still enters `fence_lost`
    state_file.write_text(
        json.dumps({"fence_lost": False, "max_seen_generation": 99, "pgids": [], "active_runs": []}),
        encoding="utf-8",
    )
    bridge3 = bridge_mod.ResearchDataBridge(cfg)
    bridge3._host_ps_list_fn = create_autospec(bridge_mod._list_host_processes_ps, return_value=[])
    rec3 = await bridge3.recover_host_runner_state_on_startup()
    assert rec3["fence_lost"] is True
    assert bridge3._host_fence_lost is True


@pytest.mark.asyncio
async def test_fence_lost_marker_write_failure_preserves_corrupt_state_file_and_returns_409(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Round 4 Item 2 & Item 3:
    When writing `.dalton-host-runner-fence-lost` fails:
    - Corrupt `.dalton-host-runner-state.json` is NOT renamed and NOT modified;
    - Bridge enters `fence_lost` in memory and logs an `ERROR`;
    - `host-run` returns HTTP 409 `{"error": "fence_lost"}`.
    """
    import logging
    import scripts.dalton.researchdata_bridge as bridge_mod

    cfg = bridge_mod.BridgeConfig()
    cfg.admin_token = "test-bridge-admin-token"
    cfg.data_tasks_data_root = tmp_path / "openclaw-data"
    cfg.data_tasks_data_root.mkdir(parents=True, exist_ok=True)

    state_file = cfg.data_tasks_data_root / ".dalton-host-runner-state.json"
    marker_file = cfg.data_tasks_data_root / ".dalton-host-runner-fence-lost"
    corrupt_raw = "{corrupt-do-not-rename-if-marker-fails"
    state_file.write_text(corrupt_raw, encoding="utf-8")

    bridge = bridge_mod.ResearchDataBridge(cfg)
    bridge._host_ps_list_fn = create_autospec(bridge_mod._list_host_processes_ps, return_value=[])
    monkeypatch.setattr(
        bridge,
        "_write_fence_lost_marker_file_sync",
        create_autospec(
            bridge._write_fence_lost_marker_file_sync,
            side_effect=OSError("disk full or permission denied"),
        ),
    )

    caplog.clear()
    with caplog.at_level(logging.ERROR):
        rec = await bridge.recover_host_runner_state_on_startup()

    assert rec["fence_lost"] is True
    assert rec["confirmed"] is False
    assert bridge._host_fence_lost is True
    assert not marker_file.exists()
    # Corrupt state file must remain untouched at its original path, with no `.corrupt-*` archive created
    assert state_file.is_file()
    assert state_file.read_text(encoding="utf-8") == corrupt_raw
    assert list(cfg.data_tasks_data_root.glob(".dalton-host-runner-state.json.corrupt-*")) == []
    assert any(rec_log.levelname == "ERROR" and "fence_lost marker" in rec_log.getMessage() for rec_log in caplog.records)

    run_status, run_resp = await bridge.handle_data_task_host_run(
        {
            "generation": 10,
            "run_id": "run-marker-failed",
            "job_key": T1_JOB_KEY,
            "host_mode": "run",
            "scheduled_slot_utc": "2026-11-07T12:30:00Z",
            "timeout": 30,
            "no_output_timeout": 15,
        }
    )
    assert run_status == 409
    assert run_resp["error"] == "fence_lost"
    assert state_file.read_text(encoding="utf-8") == corrupt_raw


def test_validate_bbg_mirror_files_short_data_rows_pass_with_accurate_counts(
    tmp_path: Path,
) -> None:
    """Round 3 Item 4 & Item 5:
    - Data rows with `< 20` columns do not fail validation; missing series columns on those rows
      are simply not counted (`bbg_export_to_csv.py:95-99`);
    - Validation succeeds when every series still has `>= 252` valid values, with exact counts;
    - Header row with `< 20` columns still fails immediately.
    """
    import scripts.dalton.researchdata_bridge as bridge_mod

    csv_path = tmp_path / "bbg_data_latest.csv"
    xlsx_path = tmp_path / "bbg_data_remote.xlsx"
    _write_valid_xlsx_zip(xlsx_path)

    header = [f"col_{i}" for i in range(20)]
    lines = [",".join(header)]
    # 255 full 20-column rows + 5 short 6-column rows (only series 0 `cyc_price` at (0,1) and series 1 `cyc_rsi14` at (3,4) present)
    for idx in range(255):
        day = f"2025-{(idx // 28) + 1:02d}-{(idx % 28) + 1:02d}"
        row = ["0"] * 20
        for d_col, v_col in bridge_mod.BBG_REQUIRED_SERIES.values():
            row[d_col] = day
            row[v_col] = f"{100.0 + idx:.2f}"
        lines.append(",".join(row))
    for idx in range(5):
        day = f"2026-01-0{idx + 1}"
        short_row = [day, "200.5", "", day, "55.2", ""]
        lines.append(",".join(short_row))

    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = bridge_mod.validate_bbg_mirror_files(csv_path, xlsx_path)
    assert res["ok"] is True, f"Expected short data rows CSV to pass validation, got: {res}"
    assert res["counts"]["cyc_price"] == 260
    assert res["counts"]["cyc_rsi14"] == 260
    assert res["counts"]["momentum_rsi"] == 255
    assert res["counts"]["qqq_vol_1m"] == 255
    assert res["counts"]["qqq_vol_3m"] == 255
    assert res["counts"]["ah_price"] == 255
    assert res["counts"]["ah_rsi30"] == 255
    assert res["last_dates"]["cyc_price"] == "2026-01-05"
    assert res["last_dates"]["ah_rsi30"] == "2025-10-03"

    # If full rows drop to 250 (< 252) + 5 short rows, series missing in short rows fail with series_below_252
    csv_path.write_text("\n".join([lines[0], *lines[1:251], *lines[256:]]) + "\n", encoding="utf-8")
    res_short_fail = bridge_mod.validate_bbg_mirror_files(csv_path, xlsx_path)
    assert res_short_fail["ok"] is False
    assert res_short_fail["reason"] == "series_below_252:momentum_rsi:250"

    # Header row with < 20 columns still fails immediately
    csv_path.write_text(",".join(header[:14]) + "\n" + "\n".join(lines[1:]) + "\n", encoding="utf-8")
    res_bad_header = bridge_mod.validate_bbg_mirror_files(csv_path, xlsx_path)
    assert res_bad_header["ok"] is False
    assert res_bad_header["reason"] == "csv_columns_below_20:14"



@pytest.mark.asyncio
async def test_a_finished_host_run_whose_group_is_not_confirmed_gone_keeps_the_slot(
    tmp_path: Path,
) -> None:
    """codex r4: a normal 200 host-run reply must not free the slot while the bridge
    still holds an unconfirmed process group; the scheduler kills and confirms first."""
    from src.dalton.data_jobs.host_runner import (
        DataTaskHostBridgeClient,
        HostKillOutcome,
        HostRunOutcome,
    )
    from src.dalton.data_jobs.scheduler import ClaimedTaskRun, execute_claimed_host_run
    from src.dalton.data_jobs.container_runner import build_path_map

    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    now = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    claimed = ClaimedTaskRun(
        run_id="run-unconfirmed-exit",
        job_key=T1_JOB_KEY,
        generation=30,
        runner="host",
        command_argv=["bash", "/Users/everflow/.openclaw/workspace/scripts/bbg_remote_refresh.sh"],
        workdir="/Users/everflow/.openclaw/workspace",
        timeout_seconds=900,
        no_output_timeout_seconds=420,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=False,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=15),
        timezone="America/New_York",
        host_mode="mirror",
    )
    host_client = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client.run_host_task.return_value = HostRunOutcome(
        ok=True, status_code=200, exit_code=0, exit_confirmed=False,
        stdout='{"ok": true, "lines": 300}\n',
    )
    host_client.kill_host_tasks.return_value = HostKillOutcome(
        ok=False, confirmed=False, status_code=200, max_seen_generation=31,
        stopped_pgids=[], remaining_pgids=[777],
    )
    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(8)])
    conn, state = _make_autospec_db(
        [
            {"rowcount": 1},  # start_claimed_host_run
            {"rowcount": 1, "fetchone": {"generation": 31}},  # bump_slot_generation
            {"rowcount": 1},  # finalize_task_run: UPDATE slot SET slot_state='reclaiming'
        ]
    )
    committed, cf = await execute_claimed_host_run(
        conn, lock_conn=lock_conn, host_client=host_client, claimed=claimed, path_map=path_map,
    )
    host_client.kill_host_tasks.assert_awaited_once_with(
        generation=31, run_id="run-unconfirmed-exit", reason="host_exit_unconfirmed",
    )
    assert committed is False
    assert cf is None
    assert "SET slot_state = 'reclaiming'" in state["executed"][-1][0]


@pytest.mark.asyncio
async def test_a_host_run_reply_without_exit_confirmed_counts_as_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    import src.dalton.data_jobs.host_runner as hr_mod

    monkeypatch.setattr(
        hr_mod, "get_dalton_bridge_base_url",
        create_autospec(hr_mod.get_dalton_bridge_base_url, return_value="http://127.0.0.1:18799"),
    )
    monkeypatch.setattr(
        hr_mod, "get_dalton_bridge_admin_token",
        create_autospec(hr_mod.get_dalton_bridge_admin_token, return_value="valid-admin-token"),
    )
    base = {"ok": True, "exit_code": 0, "timed_out": False, "killed_by_host_kill": False,
            "stdout": "", "stderr": "", "log_tail": ""}

    for body, expected in ((base, False), ({**base, "exit_confirmed": True}, True),
                           ({**base, "exit_confirmed": False}, False)):
        async def _post(self: Any, *args: Any, _body: dict = body, **kwargs: Any) -> Any:
            return httpx.Response(200, json=_body, request=httpx.Request("POST", "http://x"))

        monkeypatch.setattr(httpx.AsyncClient, "post", _post)
        out = await post_bridge_host_run(
            generation=1, run_id="r", job_key=T1_JOB_KEY, host_mode="mirror",
            scheduled_slot_utc="2026-11-07T10:00:00Z", timeout=60, no_output_timeout=30,
        )
        assert out.exit_confirmed is expected
