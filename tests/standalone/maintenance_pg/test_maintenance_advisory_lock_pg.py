"""Real PostgreSQL advisory-lock acceptance tests for maintenance admission gate (§6.4b (1) #2)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import psycopg
from psycopg import sql as pg_sql
import pytest

from src.dalton.maintenance_gate import (
    MAINTENANCE_SHARED_LOCK_SQL,
    MaintenanceAdmissionPausedError,
    enforce_maintenance_admission_in_txn,
    read_maintenance_state,
    update_maintenance_state_under_db_lock,
)
from src.server.models.chat import ChatRequest
from src.server.services.runs import admission


async def _connect_in_schema(dsn: str, schema_name: str) -> psycopg.AsyncConnection:
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=False)
    async with conn.cursor() as cur:
        await cur.execute(
            pg_sql.SQL("SET search_path TO {}").format(pg_sql.Identifier(schema_name))
        )
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_pg_exclusive_pause_waits_for_in_flight_shared_txn_commit_and_sees_row(
    maintenance_pg_env: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conn A holds shared advisory lock before commit; host pause (Conn B) blocks until A commits and then sees A's in_progress row."""
    dsn, schema_name = maintenance_pg_env
    state_file = tmp_path / "maintenance-admission.json"
    monkeypatch.setenv("DALTON_MAINTENANCE_ADMISSION_FILE", str(state_file))

    conn_a = await _connect_in_schema(dsn, schema_name)
    conn_obs = await _connect_in_schema(dsn, schema_name)
    try:
        entered_before_commit = asyncio.Event()
        allow_commit = asyncio.Event()

        async def _conn_a_start_txn() -> None:
            async with conn_a.transaction():
                async with conn_a.cursor() as cur:
                    await cur.execute(MAINTENANCE_SHARED_LOCK_SQL)
                    state = read_maintenance_state(state_file)
                    assert state.paused is False
                    await cur.execute(
                        "INSERT INTO conversation_threads (conversation_thread_id, workspace_id, current_status) "
                        "VALUES (%s, %s, 'in_progress')",
                        ("thr-pg-a", "ws-normal-pg"),
                    )
                    await cur.execute(
                        "INSERT INTO conversation_responses (conversation_response_id, conversation_thread_id, status) "
                        "VALUES (%s, %s, 'in_progress')",
                        ("run-pg-a", "thr-pg-a"),
                    )
                entered_before_commit.set()
                await allow_commit.wait()

        task_a = asyncio.create_task(_conn_a_start_txn())
        await asyncio.wait_for(entered_before_commit.wait(), timeout=5.0)

        # Conn B: real host-side synchronous pause taking PG exclusive advisory lock + write_maintenance_state_atomic
        task_b = asyncio.create_task(
            asyncio.to_thread(
                update_maintenance_state_under_db_lock,
                paused=True,
                allow_workspace_ids=[],
                pause_host_direct=True,
                reason="pg_advisory_lock_test",
                operation="switch",
                state_file=state_file,
                dsn=dsn,
            )
        )

        # While Conn A has not committed, Conn B MUST remain blocked on pg_advisory_xact_lock
        await asyncio.sleep(0.3)
        assert not task_b.done(), "Exclusive pause returned before Conn A committed!"
        assert read_maintenance_state(state_file).paused is False

        # Release Conn A -> commits -> Conn B unblocks, writes paused gate, and commits
        allow_commit.set()
        await asyncio.wait_for(task_a, timeout=5.0)
        paused_state = await asyncio.wait_for(task_b, timeout=5.0)
        assert paused_state.paused is True
        assert paused_state.revision == 1
        assert paused_state.pause_host_direct is True

        # Immediately after Conn B returns, Conn A's committed in_progress row is visible to idle checks
        async with conn_obs.cursor() as cur:
            await cur.execute(
                "SELECT current_status FROM conversation_threads WHERE conversation_thread_id = %s",
                ("thr-pg-a",),
            )
            row = await cur.fetchone()
            assert row is not None and row[0] == "in_progress"
        await conn_obs.commit()

        # Conn C: after pause lands with allow_workspace_ids=[], BOTH normal & rehearsal workspaces roll back
        for ws in ("ws-normal-pg", "ws-rehearsal-pg"):
            conn_c = await _connect_in_schema(dsn, schema_name)
            try:
                with pytest.raises(MaintenanceAdmissionPausedError) as exc_info:
                    async with conn_c.transaction():
                        async with conn_c.cursor() as cur:
                            await cur.execute(
                                "INSERT INTO conversation_threads (conversation_thread_id, workspace_id, current_status) "
                                "VALUES (%s, %s, 'in_progress')",
                                (f"thr-blocked-{ws}", ws),
                            )
                        await enforce_maintenance_admission_in_txn(
                            conn_c, workspace_id=ws, state_file=state_file
                        )
                assert exc_info.value.status_code == 503
                assert exc_info.value.detail["error_code"] == "maintenance_in_progress"
                async with conn_obs.cursor() as cur:
                    await cur.execute(
                        "SELECT 1 FROM conversation_threads WHERE conversation_thread_id = %s",
                        (f"thr-blocked-{ws}",),
                    )
                    assert await cur.fetchone() is None
                await conn_obs.commit()
            finally:
                await conn_c.close()

        # Allowlist rehearsal workspace -> rehearsal commits while normal workspace still rolls back
        allow_state = await asyncio.to_thread(
            update_maintenance_state_under_db_lock,
            paused=True,
            allow_workspace_ids=["ws-rehearsal-pg"],
            pause_host_direct=False,
            reason="rehearsal_verify",
            operation="switch",
            state_file=state_file,
            dsn=dsn,
        )
        assert allow_state.revision == 2

        conn_rehearsal = await _connect_in_schema(dsn, schema_name)
        try:
            async with conn_rehearsal.transaction():
                async with conn_rehearsal.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO conversation_threads (conversation_thread_id, workspace_id, current_status) "
                        "VALUES (%s, %s, 'in_progress')",
                        ("thr-rehearsal-ok", "ws-rehearsal-pg"),
                    )
                await enforce_maintenance_admission_in_txn(
                    conn_rehearsal, workspace_id="ws-rehearsal-pg", state_file=state_file
                )
            async with conn_obs.cursor() as cur:
                await cur.execute(
                    "SELECT current_status FROM conversation_threads WHERE conversation_thread_id = %s",
                    ("thr-rehearsal-ok",),
                )
                row = await cur.fetchone()
                assert row is not None and row[0] == "in_progress"
            await conn_obs.commit()
        finally:
            await conn_rehearsal.close()
    finally:
        await conn_a.close()
        await conn_obs.close()


@pytest.mark.asyncio
async def test_pg_real_admission_start_checks_blocks_pause_until_commit_and_rolls_back_after_pause(
    maintenance_pg_env: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise real `admission._start_checks` inside real PostgreSQL transactions for both pre-pause race & post-pause rejection."""
    dsn, schema_name = maintenance_pg_env
    state_file = tmp_path / "maintenance-admission.json"
    monkeypatch.setenv("DALTON_MAINTENANCE_ADMISSION_FILE", str(state_file))

    req = ChatRequest(messages=[{"role": "user", "content": "Hello"}])
    conn_a = await _connect_in_schema(dsn, schema_name)
    conn_c = await _connect_in_schema(dsn, schema_name)
    conn_obs = await _connect_in_schema(dsn, schema_name)
    try:
        checks_done = asyncio.Event()
        allow_commit = asyncio.Event()

        async def _in_flight_start_checks() -> None:
            async with conn_a.transaction():
                async with conn_a.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO conversation_threads (conversation_thread_id, workspace_id, current_status) "
                        "VALUES (%s, %s, 'in_progress')",
                        ("thr-start-checks-a", "ws-start-normal"),
                    )
                await admission._start_checks(
                    req,
                    conn_a,
                    thread_id="thr-start-checks-a",
                    run_id="run-start-checks-a",
                    workspace_id="ws-start-normal",
                )
                checks_done.set()
                await allow_commit.wait()

        task_a = asyncio.create_task(_in_flight_start_checks())
        await asyncio.wait_for(checks_done.wait(), timeout=5.0)

        pause_task = asyncio.create_task(
            asyncio.to_thread(
                update_maintenance_state_under_db_lock,
                paused=True,
                allow_workspace_ids=["ws-start-rehearsal"],
                pause_host_direct=True,
                reason="real_start_checks_test",
                operation="switch",
                state_file=state_file,
                dsn=dsn,
            )
        )
        await asyncio.sleep(0.3)
        assert not pause_task.done(), "Pause did not wait for real admission._start_checks txn to commit"

        allow_commit.set()
        await asyncio.wait_for(task_a, timeout=5.0)
        await asyncio.wait_for(pause_task, timeout=5.0)

        # Verify A's committed row is visible
        async with conn_obs.cursor() as cur:
            await cur.execute(
                "SELECT current_status FROM conversation_threads WHERE conversation_thread_id = %s",
                ("thr-start-checks-a",),
            )
            row = await cur.fetchone()
            assert row is not None and row[0] == "in_progress"
        await conn_obs.commit()

        # Post-pause: normal workspace rejected by real admission._start_checks and rolled back
        with pytest.raises(MaintenanceAdmissionPausedError):
            async with conn_c.transaction():
                async with conn_c.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO conversation_threads (conversation_thread_id, workspace_id, current_status) "
                        "VALUES (%s, %s, 'in_progress')",
                        ("thr-start-checks-c", "ws-start-normal"),
                    )
                await admission._start_checks(
                    req,
                    conn_c,
                    thread_id="thr-start-checks-c",
                    run_id="run-start-checks-c",
                    workspace_id="ws-start-normal",
                )
        async with conn_obs.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM conversation_threads WHERE conversation_thread_id = %s",
                ("thr-start-checks-c",),
            )
            assert await cur.fetchone() is None
        await conn_obs.commit()

        # Post-pause: allowlisted rehearsal workspace passes real admission._start_checks and commits
        async with conn_c.transaction():
            async with conn_c.cursor() as cur:
                await cur.execute(
                    "INSERT INTO conversation_threads (conversation_thread_id, workspace_id, current_status) "
                    "VALUES (%s, %s, 'in_progress')",
                    ("thr-start-checks-rehearsal", "ws-start-rehearsal"),
                )
            await admission._start_checks(
                req,
                conn_c,
                thread_id="thr-start-checks-rehearsal",
                run_id="run-start-checks-rehearsal",
                workspace_id="ws-start-rehearsal",
            )
        async with conn_obs.cursor() as cur:
            await cur.execute(
                "SELECT current_status FROM conversation_threads WHERE conversation_thread_id = %s",
                ("thr-start-checks-rehearsal",),
            )
            row = await cur.fetchone()
            assert row is not None and row[0] == "in_progress"
        await conn_obs.commit()
    finally:
        await conn_a.close()
        await conn_c.close()
        await conn_obs.close()
