"""Real Postgres integration tests for OC-1 Phase 0 PR 0-1 (`dalton.data_jobs`).

Reads the test database DSN from ``DALTON_DATA_JOBS_TEST_DSN``; skips cleanly when unset.
Applies ``d029_data_tasks.upgrade()`` SQL directly on the target database before running
and cleans up via ``d029_data_tasks.downgrade()`` afterwards.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from unittest.mock import create_autospec
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from src.dalton.data_jobs.container_runner import (
    ContainerInspectionOutcome,
    DataTaskDockerClient,
    build_path_map,
)
from src.dalton.data_jobs.evaluators import (
    T5_JOB_KEY,
    T18_JOB_KEY,
    TaskEvaluationResult,
)
from src.dalton.data_jobs.host_runner import (
    DataTaskHostBridgeClient,
    HostRunOutcome,
)
from src.dalton.data_jobs.manifest import load_data_task_manifest, sync_manifest_to_db
from src.dalton.data_jobs.scheduler import (
    claim_next_queued_run,
    enqueue_due_cron_tasks,
    execute_claimed_host_run,
    finalize_task_run,
    open_dedicated_lock_connection,
    recover_on_takeover,
    release_scheduler_lock,
    start_claimed_container_run,
    try_scheduler_lock,
)
from src.dalton.migrations.versions import d029_data_tasks

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session", autouse=True)
def dalton_schema():
    """Override directory conftest autouse fixture; this module manages d029 directly via DALTON_DATA_JOBS_TEST_DSN."""
    yield


@pytest.fixture(autouse=True)
def cleanup_dalton_tables():
    """Override directory conftest autouse fixture so no connection is made when DALTON_DATA_JOBS_TEST_DSN is unset."""
    yield


def _get_test_dsn() -> str:
    dsn = os.environ.get("DALTON_DATA_JOBS_TEST_DSN", "").strip()
    if not dsn:
        pytest.skip("DALTON_DATA_JOBS_TEST_DSN is not set; skipping real Postgres integration tests")
    return dsn


async def _run_d029_upgrade(conn: psycopg.AsyncConnection) -> None:
    statements: list[str] = []
    orig_execute = d029_data_tasks.op.execute
    d029_data_tasks.op.execute = create_autospec(
        orig_execute,
        side_effect=lambda sql: statements.append(str(sql)),
    )
    try:
        d029_data_tasks.downgrade()
        d029_data_tasks.upgrade()
    finally:
        d029_data_tasks.op.execute = orig_execute

    async with conn.transaction():
        async with conn.cursor() as cur:
            for stmt in statements:
                await cur.execute(stmt)


async def _run_d029_downgrade(conn: psycopg.AsyncConnection) -> None:
    statements: list[str] = []
    orig_execute = d029_data_tasks.op.execute
    d029_data_tasks.op.execute = create_autospec(
        orig_execute,
        side_effect=lambda sql: statements.append(str(sql)),
    )
    try:
        d029_data_tasks.downgrade()
    finally:
        d029_data_tasks.op.execute = orig_execute

    async with conn.transaction():
        async with conn.cursor() as cur:
            for stmt in statements:
                await cur.execute(stmt)


@pytest.mark.asyncio
async def test_real_postgres_data_jobs_end_to_end(tmp_path: Path) -> None:
    dsn = _get_test_dsn()
    path_map = build_path_map(
        host_var_dir=tmp_path / "host_var",
        backend_var_dir=tmp_path / "backend_var",
    )

    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    lock_conn1 = await open_dedicated_lock_connection(dsn)
    lock_conn2 = await open_dedicated_lock_connection(dsn)

    try:
        # 1. Session advisory lock competition between two real dedicated connections
        await release_scheduler_lock(lock_conn1, close_conn=False)
        await release_scheduler_lock(lock_conn2, close_conn=False)

        assert await try_scheduler_lock(lock_conn1) is True
        assert await try_scheduler_lock(lock_conn2) is False

        # After holder unlocks & closes, the second connection acquires the lock
        await release_scheduler_lock(lock_conn1, close_conn=True)
        assert lock_conn1.closed
        assert await try_scheduler_lock(lock_conn2) is True

        # Re-open lock_conn1 as our active lock connection after releasing lock_conn2
        await release_scheduler_lock(lock_conn2, close_conn=True)
        lock_conn1 = await open_dedicated_lock_connection(dsn)
        assert await try_scheduler_lock(lock_conn1) is True

        # 2. Apply d029 migration upgrade SQL and sync manifest
        await _run_d029_upgrade(conn)
        manifest = load_data_task_manifest()
        async with conn.transaction():
            synced = await sync_manifest_to_db(conn, manifest)
        assert synced == 19

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT job_key, cron_expr, timezone, runner, enabled, required_secrets FROM dalton_data_task_defs ORDER BY job_key"
            )
            defs = await cur.fetchall()
        assert len(defs) == 19
        by_key = {row["job_key"]: row for row in defs}
        assert by_key["tw-leverage-daily"]["cron_expr"] == "40 9 * * 1-5"
        assert by_key["tw-leverage-daily"]["timezone"] == "America/New_York"
        assert by_key["bbg-data-pull-asia"]["runner"] == "host"
        assert by_key["bbg-data-pull-us"]["runner"] == "host"
        assert by_key["sentiment-dashboard-refresh"]["required_secrets"] == [
            "TENCENT_COS_SECRET_ID",
            "TENCENT_COS_SECRET_KEY",
        ]

        # 3. Enable T29 (host) and T18 (container)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE dalton_data_task_defs SET enabled = TRUE WHERE job_key IN (%s, %s)",
                    ("bbg-data-pull-us", T18_JOB_KEY),
                )

        # T18 cron is 30 8 * * 1-5 America/New_York -> 2026-11-06 13:30 UTC
        t18_slot_1 = datetime(2026, 11, 6, 13, 30, tzinfo=timezone.utc)
        enq_1 = await enqueue_due_cron_tasks(conn, now_utc=t18_slot_1)
        assert len(enq_1) == 1
        first_run_id = enq_1[0]

        # Supersede old queued run on next day's slot (2026-11-09 13:30 UTC)
        t18_slot_2 = datetime(2026, 11, 9, 13, 30, tzinfo=timezone.utc)
        enq_2 = await enqueue_due_cron_tasks(conn, now_utc=t18_slot_2)
        assert len(enq_2) == 1
        second_run_id = enq_2[0]
        assert second_run_id != first_run_id

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status, failure_reason FROM dalton_data_task_runs WHERE run_id = %(run_id)s::uuid",
                {"run_id": first_run_id},
            )
            first_row = await cur.fetchone()
        assert first_row is not None
        assert first_row["status"] == "skipped_superseded"

        # 4. Unclaimable head (disabled task) is cancelled before claiming eligible run
        disabled_run_id = str(uuid4())
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_data_task_runs (
                        run_id, job_key, trigger_source, scheduled_slot_utc, deadline_utc, status
                    ) VALUES
                        (%(r1)s::uuid, %(j1)s, 'manual', %(slot1)s, %(dl)s, 'queued')
                    """,
                    {
                        "r1": disabled_run_id,
                        "j1": T5_JOB_KEY,  # T5 is still enabled=FALSE
                        "slot1": t18_slot_1 - timedelta(minutes=10),
                        "dl": t18_slot_2 + timedelta(hours=1),
                    },
                )

        # Claim rollback branch 1: slot is idle on pre-check, but a BEFORE UPDATE trigger on
        # dalton_data_task_slot returns NULL (0 rows affected on the first UPDATE idle -> running).
        # The claim transaction must roll back and leave the candidate run in 'queued'.
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE OR REPLACE FUNCTION _test_block_slot_claim() RETURNS trigger AS $$
                    BEGIN
                        IF NEW.slot_state = 'running' THEN
                            RETURN NULL;
                        END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql;
                    CREATE TRIGGER trg_test_block_slot_claim
                    BEFORE UPDATE ON dalton_data_task_slot
                    FOR EACH ROW EXECUTE FUNCTION _test_block_slot_claim();
                    """
                )
        try:
            rolled_back_slot_claim = await claim_next_queued_run(
                conn, now_utc=t18_slot_2, available_memory_mb=2048
            )
            assert rolled_back_slot_claim is None
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT slot_state, generation FROM dalton_data_task_slot WHERE slot_id = 1"
                )
                slot_after_rb1 = await cur.fetchone()
                await cur.execute(
                    "SELECT status FROM dalton_data_task_runs WHERE run_id = %(run_id)s::uuid",
                    {"run_id": second_run_id},
                )
                run_after_rb1 = await cur.fetchone()
            assert slot_after_rb1 is not None
            assert slot_after_rb1["slot_state"] == "idle"
            assert slot_after_rb1["generation"] == 0
            assert run_after_rb1 is not None
            assert run_after_rb1["status"] == "queued"
        finally:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DROP TRIGGER IF EXISTS trg_test_block_slot_claim ON dalton_data_task_slot"
                    )
                    await cur.execute("DROP FUNCTION IF EXISTS _test_block_slot_claim()")

        # Claim rollback branch 2: trigger a BEFORE UPDATE on dalton_data_task_runs that blocks
        # transitioning to 'starting', forcing the second UPDATE in the claim tx to affect 0 rows
        # and rolling back the slot generation increment!
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE OR REPLACE FUNCTION _test_block_starting() RETURNS trigger AS $$
                    BEGIN
                        IF NEW.status = 'starting' THEN
                            RETURN NULL;
                        END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql;
                    CREATE TRIGGER trg_test_block_starting
                    BEFORE UPDATE ON dalton_data_task_runs
                    FOR EACH ROW EXECUTE FUNCTION _test_block_starting();
                    """
                )
        try:
            rolled_back_claim = await claim_next_queued_run(
                conn, now_utc=t18_slot_2, available_memory_mb=2048
            )
            assert rolled_back_claim is None
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT slot_state, generation FROM dalton_data_task_slot WHERE slot_id = 1")
                slot_after_rb = await cur.fetchone()
            assert slot_after_rb is not None
            assert slot_after_rb["slot_state"] == "idle"
            assert slot_after_rb["generation"] == 0
        finally:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("DROP TRIGGER IF EXISTS trg_test_block_starting ON dalton_data_task_runs")
                    await cur.execute("DROP FUNCTION IF EXISTS _test_block_starting()")

        # Verify disabled_run_id was cancelled
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM dalton_data_task_runs WHERE run_id = %(r1)s::uuid",
                {"r1": disabled_run_id},
            )
            disabled_row = await cur.fetchone()
        assert disabled_row is not None
        assert disabled_row["status"] == "cancelled"

        # Host task (T29 bbg-data-pull-us) is enqueued, claimed, and executed via bridge double
        t29_slot = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)  # 10:00 EST
        enq_host = await enqueue_due_cron_tasks(conn, now_utc=t29_slot)
        assert len(enq_host) == 1
        host_run_id = enq_host[0]

        claimed_host = await claim_next_queued_run(
            conn, now_utc=t29_slot, available_memory_mb=512
        )
        assert claimed_host is not None
        assert claimed_host.run_id == host_run_id
        assert claimed_host.job_key == "bbg-data-pull-us"
        assert claimed_host.runner == "host"
        assert claimed_host.host_mode == "mirror"
        assert claimed_host.generation == 1

        host_client = create_autospec(DataTaskHostBridgeClient, instance=True)
        host_client.run_host_task.return_value = HostRunOutcome(
            ok=True,
            status_code=200,
            exit_code=0,
            timed_out=False,
            timeout_kind=None,
            log_tail=json.dumps(
                {
                    "ok": True,
                    "lines": 260,
                    "source": "openclaw_mirror",
                    "path": "/var/dalton/openclaw-data/temp/bbg_data_latest.csv",
                    "timestamp": "2026-11-09T15:01:00Z",
                    "detail": {"counts": {}, "last_dates": {}},
                }
            )
            + "\n",
        )
        host_committed, host_cf = await execute_claimed_host_run(
            conn,
            lock_conn=lock_conn1,
            host_client=host_client,
            claimed=claimed_host,
            path_map=path_map,
        )
        assert host_committed is True
        assert host_cf == 0

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status, exit_code FROM dalton_data_task_runs WHERE run_id = %(r)s::uuid",
                {"r": host_run_id},
            )
            host_run_row = await cur.fetchone()
            await cur.execute(
                "SELECT slot_state, generation FROM dalton_data_task_slot WHERE slot_id = 1"
            )
            slot_after_host = await cur.fetchone()
        assert host_run_row is not None
        assert host_run_row["status"] == "succeeded"
        assert host_run_row["exit_code"] == 0
        assert slot_after_host is not None
        assert slot_after_host["slot_state"] == "idle"
        assert slot_after_host["generation"] == 1

        # Re-insert an eligible T18 queued run and claim it normally
        eligible_run_id = str(uuid4())
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_data_task_runs (
                        run_id, job_key, trigger_source, scheduled_slot_utc, deadline_utc, status
                    ) VALUES (
                        %(run_id)s::uuid, %(job_key)s, 'manual', %(slot)s, %(dl)s, 'queued'
                    )
                    """,
                    {
                        "run_id": eligible_run_id,
                        "job_key": T18_JOB_KEY,
                        "slot": t18_slot_2,
                        "dl": t18_slot_2 + timedelta(hours=1),
                    },
                )

        claimed = await claim_next_queued_run(conn, now_utc=t18_slot_2, available_memory_mb=2048)
        assert claimed is not None
        assert claimed.run_id == eligible_run_id
        assert claimed.generation == 2

        # 5. Startup UPDATE affecting 0 rows: aborts run, removes container, and releases slot to idle
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE dalton_data_task_runs SET status = 'cancelled' WHERE run_id = %(run_id)s::uuid",
                    {"run_id": claimed.run_id},
                )
        docker_client = create_autospec(DataTaskDockerClient, instance=True)
        docker_client.create_container.return_value = "cid-zero-row"
        started_cid = await start_claimed_container_run(
            conn,
            lock_conn=lock_conn1,
            docker_client=docker_client,
            claimed=claimed,
            decrypted_secrets={},
            path_map=path_map,
        )
        assert started_cid is None
        docker_client.remove_container.assert_awaited_once_with("cid-zero-row", force=True)
        docker_client.start_container.assert_not_awaited()

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT slot_state, active_run_id FROM dalton_data_task_slot WHERE slot_id = 1")
            slot_after_abort = await cur.fetchone()
        assert slot_after_abort is not None
        assert slot_after_abort["slot_state"] == "idle"
        assert slot_after_abort["active_run_id"] is None

        # 6. Full claim -> start -> finalize -> takeover recovery preserving terminal status
        run2_id = str(uuid4())
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_defs SET notify_enabled = TRUE WHERE job_key = %(job_key)s;
                    """,
                    {"job_key": T18_JOB_KEY},
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_data_task_runs (
                        run_id, job_key, trigger_source, scheduled_slot_utc, deadline_utc, status
                    ) VALUES (
                        %(run_id)s::uuid, %(job_key)s, 'manual', %(slot)s, %(dl)s, 'queued'
                    )
                    """,
                    {
                        "run_id": run2_id,
                        "job_key": T18_JOB_KEY,
                        "slot": t18_slot_2,
                        "dl": t18_slot_2 + timedelta(hours=1),
                    },
                )

        claimed2 = await claim_next_queued_run(conn, now_utc=t18_slot_2, available_memory_mb=2048)
        assert claimed2 is not None and claimed2.run_id == run2_id

        docker_client.reset_mock()
        docker_client.create_container.return_value = "cid-run2"
        cid2 = await start_claimed_container_run(
            conn,
            lock_conn=lock_conn1,
            docker_client=docker_client,
            claimed=claimed2,
            decrypted_secrets={},
            path_map=path_map,
        )
        assert cid2 == "cid-run2"
        docker_client.start_container.assert_awaited_once_with("cid-run2")

        committed, cf = await finalize_task_run(
            conn,
            lock_conn=lock_conn1,
            docker_client=docker_client,
            claimed=claimed2,
            container_id=cid2,
            outcome=ContainerInspectionOutcome("cid-run2", 0, False, False, "done\n"),
            evaluation=TaskEvaluationResult(ok=True, status="succeeded"),
            outbox_payload={"feishu_message": "ok"},
            path_map=path_map,
        )
        assert committed is True
        assert cf == 0

        # Calling finalize_task_run a second time on already-terminal run2 must roll back and not touch failures/notify
        committed_again, cf_again = await finalize_task_run(
            conn,
            lock_conn=lock_conn1,
            docker_client=docker_client,
            claimed=claimed2,
            container_id=None,
            outcome=ContainerInspectionOutcome("cid-run2", 1, False, False, "fail\n"),
            evaluation=TaskEvaluationResult(ok=False, status="failed", failure_reason="exit_1"),
            outbox_payload=None,
            path_map=path_map,
        )
        assert committed_again is False
        assert cf_again is None

        # Simulate takeover when slot still references already-terminal run2: must not alter terminal state
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_slot
                    SET slot_state = 'running', active_run_id = %(run_id)s::uuid, active_job_key = %(job_key)s
                    WHERE slot_id = 1
                    """,
                    {"run_id": run2_id, "job_key": T18_JOB_KEY},
                )
        docker_client.list_data_task_containers.return_value = []
        rec_outcome = await recover_on_takeover(
            conn, lock_conn=lock_conn1, docker_client=docker_client, path_map=path_map
        )
        assert rec_outcome == "cleaned_terminal"

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status, notify_status FROM dalton_data_task_runs WHERE run_id = %(run_id)s::uuid",
                {"run_id": run2_id},
            )
            run2_row = await cur.fetchone()
            await cur.execute(
                "SELECT consecutive_failures FROM dalton_data_task_defs WHERE job_key = %(job_key)s",
                {"job_key": T18_JOB_KEY},
            )
            def2_row = await cur.fetchone()
        assert run2_row is not None
        assert run2_row["status"] == "succeeded"
        assert run2_row["notify_status"] == "pending_dispatch"
        assert def2_row is not None
        assert def2_row["consecutive_failures"] == 0
    finally:
        try:
            await _run_d029_downgrade(conn)
        except Exception:
            pass
        await release_scheduler_lock(lock_conn1, close_conn=True)
        await release_scheduler_lock(lock_conn2, close_conn=True)
        await conn.close()
