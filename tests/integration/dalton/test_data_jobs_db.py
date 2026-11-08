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

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session", autouse=True)
def dalton_schema():
    """Override directory conftest autouse fixture; this module manages schema via `migrated_data_jobs_dsn` when `DALTON_DATA_JOBS_TEST_DSN` is set."""
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


def _ensure_alembic_upgraded_to_head(dsn: str) -> None:
    """Run real upstream (`alembic.ini`) and Dalton (`src/dalton/alembic.ini`) Alembic migrations to head, skipping any chain already at head."""
    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, pool

    project_root = Path(__file__).resolve().parents[3]
    if dsn.startswith("postgresql://"):
        sa_url = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    elif dsn.startswith("postgres://"):
        sa_url = dsn.replace("postgres://", "postgresql+psycopg://", 1)
    else:
        sa_url = dsn

    chains = [
        (
            project_root / "alembic.ini",
            project_root / "migrations",
            "alembic_version",
        ),
        (
            project_root / "src" / "dalton" / "alembic.ini",
            project_root / "src" / "dalton" / "migrations",
            "alembic_version_dalton",
        ),
    ]

    engine = create_engine(sa_url, poolclass=pool.NullPool)
    try:
        for ini_path, script_dir, version_table in chains:
            cfg = Config(str(ini_path))
            cfg.set_main_option("script_location", str(script_dir))
            cfg.set_main_option("sqlalchemy.url", sa_url.replace("%", "%%"))
            script = ScriptDirectory.from_config(cfg)
            expected_heads = set(script.get_heads())
            with engine.connect() as sa_conn:
                ctx = MigrationContext.configure(
                    sa_conn, opts={"version_table": version_table}
                )
                current_heads = set(ctx.get_current_heads())
            if current_heads != expected_heads:
                command.upgrade(cfg, "head")
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def migrated_data_jobs_dsn() -> str:
    """Session-scoped fixture that upgrades upstream and Dalton Alembic migrations on the test DB (or skips if already at head)."""
    dsn = _get_test_dsn()
    _ensure_alembic_upgraded_to_head(dsn)
    return dsn


async def _reset_data_task_tables(conn: psycopg.AsyncConnection) -> None:
    """Reset `dalton_data_task_runs`, `dalton_data_task_defs`, and `dalton_data_task_slot` between integration tests."""
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "TRUNCATE TABLE dalton_data_task_runs, dalton_data_task_defs CASCADE"
            )
            await cur.execute(
                """
                INSERT INTO dalton_data_task_slot (slot_id, generation, slot_state)
                VALUES (1, 0, 'idle')
                ON CONFLICT (slot_id) DO UPDATE
                SET generation = 0,
                    slot_state = 'idle',
                    active_run_id = NULL,
                    active_job_key = NULL,
                    updated_at = NOW()
                """
            )


@pytest.mark.asyncio
async def test_real_postgres_data_jobs_end_to_end(
    tmp_path: Path, migrated_data_jobs_dsn: str
) -> None:
    dsn = migrated_data_jobs_dsn
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

        # 2. Reset tables and sync manifest
        await _reset_data_task_tables(conn)
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
            await _reset_data_task_tables(conn)
        except Exception:
            pass
        await release_scheduler_lock(lock_conn1, close_conn=True)
        await release_scheduler_lock(lock_conn2, close_conn=True)
        await conn.close()


@pytest.mark.asyncio
async def test_real_postgres_concurrent_config_updates_and_multi_step_rollback(
    monkeypatch: pytest.MonkeyPatch,
    migrated_data_jobs_dsn: str,
) -> None:
    """Verify §7.1 item 5 & Round 2 items 1, 2, 4, 7, 8 against real PostgreSQL:
    - Uses real upstream + Dalton Alembic migrations (no manual table DDL);
    - Two separate database connections concurrently modifying the same data task serialize on the unified lock order (`lock_user_preferences` -> `dalton_data_task_defs FOR UPDATE`) without losing versions or audit entries;
    - `run_system_job_now` inserts a `queued` row with a generated UUID `run_id`, and a subsequent `enqueue_due_cron_tasks` tick does NOT supersede the manual queued row;
    - Consecutive multi-step rollbacks step backward along `prev_version` (`v4 -> v3 -> v2 -> v1`), restore `enabled` and PM `dalton_notify_routes`, cancel `queued` runs when rolling back to `enabled = FALSE`, and reject a 4th rollback at the earliest version.
    """
    import asyncio
    from contextlib import asynccontextmanager
    from uuid import UUID
    import src.dalton.autonomy.system_jobs as system_jobs_mod
    import src.dalton.notify.groups as groups_mod
    from src.server.database.user import upsert_user_preferences

    dsn = migrated_data_jobs_dsn
    pm_user_id = f"test-pm-oc1-{uuid4().hex[:8]}"

    admin_conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        await _reset_data_task_tables(admin_conn)
        manifest = load_data_task_manifest()
        async with admin_conn.transaction():
            await sync_manifest_to_db(admin_conn, manifest)

        # Seed PM user row in upstream `users` and personal notify targets in `user_preferences`
        async with admin_conn.transaction():
            async with admin_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO users (user_id, email, name, created_at, updated_at)
                    VALUES (%s, %s, %s, NOW(), NOW())
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (pm_user_id, f"{pm_user_id}@example.com", "Test PM"),
                )
            await upsert_user_preferences(
                user_id=pm_user_id,
                model_preference={
                    "dalton_notify_targets": ["feishu:user:ou_1", "feishu:user:ou_2"]
                },
                conn=admin_conn,
            )

        # Each `get_db_connection()` call opens a distinct real PostgreSQL connection
        @asynccontextmanager
        async def _real_db_conn(conn=None):
            if conn is not None:
                yield conn
                return
            c = await psycopg.AsyncConnection.connect(dsn)
            try:
                yield c
            finally:
                await c.close()

        monkeypatch.setenv("DALTON_PM_USER_IDS", pm_user_id)
        monkeypatch.setattr(
            system_jobs_mod,
            "get_db_connection",
            create_autospec(system_jobs_mod.get_db_connection, side_effect=_real_db_conn),
        )
        monkeypatch.setattr(
            system_jobs_mod,
            "get_user_tier",
            create_autospec(system_jobs_mod.get_user_tier, return_value="internal"),
        )
        # The outside-lock prefetch reads the PM's stored routes through the
        # global pool (get_user_preferences); point it at the test database.
        async def _real_get_prefs(user_id: str) -> dict | None:
            async with await psycopg.AsyncConnection.connect(dsn) as c:
                async with c.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT model_preference, other_preference FROM user_preferences WHERE user_id = %s",
                        (user_id,),
                    )
                    return await cur.fetchone()

        monkeypatch.setattr(
            system_jobs_mod,
            "get_user_preferences",
            create_autospec(system_jobs_mod.get_user_preferences, side_effect=_real_get_prefs),
        )
        # Route validation checks the actor's tier through access_tiers itself
        # (is_internal_or_admin), not through system_jobs' import of it.
        import src.dalton.access_tiers as access_tiers_mod

        monkeypatch.setattr(
            access_tiers_mod,
            "get_user_tier",
            create_autospec(access_tiers_mod.get_user_tier, return_value="internal"),
        )
        monkeypatch.setattr(
            system_jobs_mod,
            "invalidate_user_prefs_cache",
            create_autospec(system_jobs_mod.invalidate_user_prefs_cache, return_value=None),
        )
        monkeypatch.setattr(
            groups_mod,
            "discover_feishu_notify_groups",
            create_autospec(
                groups_mod.discover_feishu_notify_groups,
                return_value=[{"chat_id": "oc_g1", "name": "测试群"}],
            ),
        )

        # 1) Seed baseline v1 and verify two concurrent connections modifying `cn-leverage-daily`
        # serialize on `lock_user_preferences` + `SELECT ... FOR UPDATE` without losing versions:
        # First update brings task to v2 (`enabled=True`, `routes=["feishu:user:ou_1"]`).
        res_v2 = await system_jobs_mod.toggle_system_job(
            pm_user_id,
            "data:cn-leverage-daily",
            enabled=True,
            notify_routes=["feishu:user:ou_1"],
            actor_user_id="internal-user-1",
        )
        assert res_v2["config_version"] == 2
        assert res_v2["active_version"] == 2

        # Next, run two concurrent updates on separate connections while holding a brief row lock
        # on `admin_conn` so both connections contend on the locks simultaneously.
        async with admin_conn.transaction():
            async with admin_conn.cursor() as cur:
                await cur.execute(
                    "SELECT job_key FROM dalton_data_task_defs WHERE job_key = %s FOR UPDATE",
                    ("cn-leverage-daily",),
                )
                task_a = asyncio.create_task(
                    system_jobs_mod.toggle_system_job(
                        pm_user_id,
                        "data:cn-leverage-daily",
                        enabled=True,
                        notify_routes=["feishu:user:ou_1", "feishu:chat:oc_g1"],
                        actor_user_id="internal-user-2",
                    )
                )
                task_b = asyncio.create_task(
                    system_jobs_mod.toggle_system_job(
                        pm_user_id,
                        "data:cn-leverage-daily",
                        enabled=True,
                        notify_routes=["feishu:user:ou_2"],
                        actor_user_id="internal-user-3",
                    )
                )
                await asyncio.sleep(0.05)
            # Exiting `admin_conn.transaction()` releases the row lock so `task_a` and `task_b` race

        out_a, out_b = await asyncio.gather(task_a, task_b)
        versions_returned = sorted([out_a["config_version"], out_b["config_version"]])
        assert versions_returned == [3, 4]

        status_v4 = await system_jobs_mod.get_system_job_status(
            "internal-user-1", "data:cn-leverage-daily"
        )
        job_v4 = status_v4["job"]
        assert job_v4["config_version"] == 4
        assert job_v4["active_version"] == 4
        assert len(job_v4["audit_history"]) == 4
        assert [e["version"] for e in job_v4["audit_history"]] == [1, 2, 3, 4]
        assert [e["prev_version"] for e in job_v4["audit_history"]] == [None, 1, 2, 3]

        # 2) Enqueue a manual run while enabled=True -> inserts `queued` row with UUID `run_id`
        run_res = await system_jobs_mod.run_system_job_now(
            pm_user_id,
            "data:cn-leverage-daily",
            actor_user_id="internal-user-2",
        )
        assert run_res["status"] == "queued"
        queued_run_id = run_res["run_id"]
        assert str(UUID(str(queued_run_id))) == queued_run_id

        # Verify a subsequent cron slot enqueue does NOT supersede the manual queued run (Item 7)
        async with admin_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT cron_expr, timezone FROM dalton_data_task_defs WHERE job_key = %s",
                ("cn-leverage-daily",),
            )
            def_cron_row = await cur.fetchone()
        assert def_cron_row is not None
        from croniter import croniter
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(str(def_cron_row["timezone"]))
        next_local_slot = croniter(
            str(def_cron_row["cron_expr"]),
            datetime.now(timezone.utc).astimezone(tz) + timedelta(minutes=2),
        ).get_next(datetime)
        next_slot_utc = next_local_slot.astimezone(timezone.utc)
        cron_enqueued = await enqueue_due_cron_tasks(admin_conn, now_utc=next_slot_utc)
        assert len(cron_enqueued) == 0

        async with admin_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status, trigger_source, triggered_by_user_id FROM dalton_data_task_runs WHERE run_id = %s::uuid",
                (queued_run_id,),
            )
            manual_row_after_cron = await cur.fetchone()
        assert manual_row_after_cron is not None
        assert manual_row_after_cron["status"] == "queued"
        assert manual_row_after_cron["status"] != "skipped_superseded"
        assert manual_row_after_cron["trigger_source"] == "manual"
        assert manual_row_after_cron["triggered_by_user_id"] == "internal-user-2"

        # 3) Three consecutive rollbacks: v4 -> v3 (writes v5) -> v2 (writes v6) -> v1 (writes v7)
        rb1 = await system_jobs_mod.rollback_data_task_config(
            "internal-user-1", "cn-leverage-daily"
        )
        assert rb1["config_version"] == 5
        assert rb1["active_version"] == 5
        assert rb1["restored_from_version"] == 3
        assert rb1["job"]["enabled"] is True

        rb2 = await system_jobs_mod.rollback_data_task_config(
            "internal-user-2", "cn-leverage-daily"
        )
        assert rb2["config_version"] == 6
        assert rb2["active_version"] == 6
        assert rb2["restored_from_version"] == 2
        assert rb2["job"]["enabled"] is True
        assert rb2["job"]["notify_routes"] == ["feishu:user:ou_1"]

        rb3 = await system_jobs_mod.rollback_data_task_config(
            "internal-user-3", "cn-leverage-daily"
        )
        assert rb3["config_version"] == 7
        assert rb3["active_version"] == 7
        assert rb3["restored_from_version"] == 1
        assert rb3["job"]["enabled"] is False
        assert rb3["job"]["notify_routes"] == []
        assert rb3["job"]["can_rollback"] is False

        # Queued run must be cancelled when rolled back to `enabled = False`
        async with admin_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status, failure_reason FROM dalton_data_task_runs WHERE run_id = %s::uuid",
                (queued_run_id,),
            )
            q_row = await cur.fetchone()
        assert q_row is not None
        assert q_row["status"] == "cancelled"

        # 4) 4th rollback at earliest version raises ValueError
        with pytest.raises(ValueError, match="最早配置版本"):
            await system_jobs_mod.rollback_data_task_config(
                "internal-user-1", "cn-leverage-daily"
            )
    finally:
        try:
            async with admin_conn.transaction():
                async with admin_conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM user_preferences WHERE user_id = %s",
                        (pm_user_id,),
                    )
                    await cur.execute(
                        "DELETE FROM users WHERE user_id = %s",
                        (pm_user_id,),
                    )
        except Exception:
            pass
        try:
            await _reset_data_task_tables(admin_conn)
        except Exception:
            pass
        await admin_conn.close()

