"""Real-PostgreSQL dual-connection concurrency tests for Batch 3 archive & move fences.

Marked ``integration`` so the lead can run them outside the sandbox against a
live PostgreSQL database:
    uv run pytest -m integration tests/integration/test_cos_archive_concurrency_db.py -v
"""

from __future__ import annotations

import asyncio
import uuid

from unittest.mock import create_autospec

import pytest

from src.dalton.services import archive_lifecycle
from src.server.database import computer as computer_db
from src.server.database import workspace as workspace_db
from src.server.database import workspace_move as move_db
from src.server.database.workspace_move import MoveRefused
from src.server.services.computer_errors import (
    ArchiveScopeChangedError,
    ComputerBusyError,
)
from src.server.services.workspace_manager import WorkspaceManager


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _seed_user_and_computers(get_conn):
    user_id = f"it-cos-b3-{uuid.uuid4().hex[:12]}"
    async with get_conn() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO users (user_id, email) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user_id, f"{user_id}@example.test"),
        )
    comp_a = await computer_db.create_computer(
        user_id,
        kind="daytona",
        name=f"Archive-A-{uuid.uuid4().hex[:6]}",
        status="stopped",
        provider_ref=f"sbx-a-{uuid.uuid4().hex[:8]}",
    )
    comp_b = await computer_db.create_computer(
        user_id,
        kind="daytona",
        name=f"Target-B-{uuid.uuid4().hex[:6]}",
        status="stopped",
        provider_ref=f"sbx-b-{uuid.uuid4().hex[:8]}",
    )
    ws_1 = await workspace_db.create_workspace_on_computer(
        user_id,
        f"NVDA-{uuid.uuid4().hex[:6]}",
        str(comp_a["computer_id"]),
    )
    assert ws_1 is not None
    return user_id, str(comp_a["computer_id"]), str(comp_b["computer_id"]), str(ws_1["workspace_id"])


async def _cleanup_user(get_conn, user_id: str):
    async with get_conn() as conn, conn.transaction():
        await conn.execute("DELETE FROM workspace_moves WHERE user_id = %s", (user_id,))
        await conn.execute("DELETE FROM workspaces WHERE user_id = %s", (user_id,))
        await conn.execute("DELETE FROM computers WHERE user_id = %s", (user_id,))
        await conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))


async def test_dual_conn_snapshot_drift_and_stale_staged_move_block_archive(
    patched_get_db_connection,
):
    user_id, comp_a, comp_b, ws_1 = await _seed_user_and_computers(
        patched_get_db_connection
    )
    try:
        snapshot = await computer_db.get_live_workspaces_for_archive_snapshot(comp_a)
        assert len(snapshot) == 1

        # 1. Another connection adds a new workspace on comp_a -> snapshot mismatch aborts claim.
        ws_2 = await workspace_db.create_workspace_on_computer(
            user_id, f"AAPL-{uuid.uuid4().hex[:6]}", comp_a
        )
        assert ws_2 is not None
        with pytest.raises(ArchiveScopeChangedError):
            await computer_db.claim_computer_for_archive(
                comp_a, expected_workspaces=snapshot, expected_workspace_id=ws_1
            )

        # 2. Refresh snapshot, insert a stale 'staged' move (heartbeat 10m ago) -> archive stays blocked.
        fresh_snapshot = await computer_db.get_live_workspaces_for_archive_snapshot(comp_a)
        async with patched_get_db_connection() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO workspace_moves (
                    move_id, user_id, workspace_id, source_computer_id,
                    target_computer_id, make_primary, from_backup, phase,
                    runner_id, source_folder, heartbeat_at
                ) VALUES (
                    %s, %s, %s, %s, %s, FALSE, FALSE, 'staged',
                    %s, 'NVDA', NOW() - interval '10 minutes'
                )
                """,
                (str(uuid.uuid4()), user_id, ws_1, comp_a, comp_b, str(uuid.uuid4())),
            )
        with pytest.raises(ComputerBusyError):
            await computer_db.claim_computer_for_archive(
                comp_a, expected_workspaces=fresh_snapshot, expected_workspace_id=ws_1
            )

        # Same for stale 'committed' move.
        async with patched_get_db_connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE workspace_moves SET phase = 'committed' WHERE user_id = %s",
                (user_id,),
            )
        with pytest.raises(ComputerBusyError):
            await computer_db.claim_computer_for_archive(
                comp_a, expected_workspaces=fresh_snapshot, expected_workspace_id=ws_1
            )
    finally:
        await _cleanup_user(patched_get_db_connection, user_id)


async def test_dual_conn_archiving_fence_blocks_create_start_and_move_and_survives_startup_reset(
    patched_get_db_connection, monkeypatch
):
    user_id, comp_a, comp_b, ws_1 = await _seed_user_and_computers(
        patched_get_db_connection
    )
    try:
        snapshot = await computer_db.get_live_workspaces_for_archive_snapshot(comp_a)
        claimed = await computer_db.claim_computer_for_archive(
            comp_a, expected_workspaces=snapshot, expected_workspace_id=ws_1
        )
        assert claimed["status"] == "stopping"
        assert (claimed.get("config") or {}).get("archiving") is True

        # While archiving, new workspace creation on comp_a is rejected by SQL fence.
        created = await workspace_db.create_workspace_on_computer(
            user_id, f"MSFT-{uuid.uuid4().hex[:6]}", comp_a
        )
        assert created is None

        # Moving out of comp_a is also blocked by source-computer FOR UPDATE + archiving check.
        with pytest.raises(MoveRefused) as exc_out:
            await move_db.claim_workspace_move(
                user_id=user_id, workspace_id=ws_1, target_computer_id=comp_b
            )
        assert exc_out.value.code == "busy"

        # Startup recovery with live heartbeat skips writeback.
        wm = object.__new__(WorkspaceManager)
        hb_mock = create_autospec(
            archive_lifecycle.computer_stop_heartbeat_present, return_value=True
        )
        monkeypatch.setattr(archive_lifecycle, "computer_stop_heartbeat_present", hb_mock)
        assert await wm.recover_archiving_computers_on_startup() == 0
        row_still_stopping = await computer_db.get_computer(comp_a)
        assert row_still_stopping is not None
        assert row_still_stopping["status"] == "stopping"
        assert (row_still_stopping.get("config") or {}).get("archiving_owner")
        assert (row_still_stopping.get("config") or {}).get("archiving_claimed_at")

        # Claim committed, heartbeat not created yet (returns False) within TTL ->
        # startup recovery MUST NOT take over.
        hb_mock.return_value = False
        assert await wm.recover_archiving_computers_on_startup() == 0
        row_in_grace = await computer_db.get_computer(comp_a)
        assert row_in_grace is not None
        assert row_in_grace["status"] == "stopping"

        # Age the claim past STOP_HEARTBEAT_TTL_S -> startup recovery resets ONLY
        # the computer row to 'stopped', preserving config.archiving=true and NOT
        # shadow-writing live workspaces.
        async with patched_get_db_connection() as conn, conn.transaction():
            await conn.execute(
                """
                UPDATE computers
                SET config = jsonb_set(
                        config,
                        '{archiving_claimed_at}',
                        to_jsonb(to_char((NOW() - interval '60 seconds') AT TIME ZONE 'UTC',
                                         'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'))
                    ),
                    updated_at = NOW() - interval '60 seconds'
                WHERE computer_id = %s
                """,
                (comp_a,),
            )
        recovered = await wm.recover_archiving_computers_on_startup()
        assert recovered >= 1
        row_after_restart = await computer_db.get_computer(comp_a)
        assert row_after_restart is not None
        assert row_after_restart["status"] == "stopped"
        assert (row_after_restart.get("config") or {}).get("archiving") is True
        ws_after_restart = await workspace_db.get_workspace(ws_1)
        assert ws_after_restart is not None
        assert ws_after_restart["status"] == "stopping"

        # Start claim is atomically refused by SQL fence while config.archiving=true.
        start_claim = await computer_db.try_claim_computer_for_start(
            comp_a, from_status="stopped"
        )
        assert start_claim is None

        # Fence STILL blocks new workspace creation until provider terminal state clears it.
        created_after_restart = await workspace_db.create_workspace_on_computer(
            user_id, f"GOOG-{uuid.uuid4().hex[:6]}", comp_a
        )
        assert created_after_restart is None

        # Once terminal provider state clears fence, create and start succeed again.
        assert await computer_db.clear_computer_archiving(comp_a) is True
        ws_after_clear = await workspace_db.get_workspace(ws_1)
        assert ws_after_clear is not None
        assert ws_after_clear["status"] == "stopped"
        created_after_clear = await workspace_db.create_workspace_on_computer(
            user_id, f"GOOG-{uuid.uuid4().hex[:6]}", comp_a
        )
        assert created_after_clear is not None
        start_after_clear = await computer_db.try_claim_computer_for_start(
            comp_a, from_status="stopped"
        )
        assert start_after_clear is not None
        assert start_after_clear["status"] == "starting"
    finally:
        await _cleanup_user(patched_get_db_connection, user_id)


async def test_dual_conn_move_and_archive_serialize_without_deadlock(
    patched_get_db_connection,
):
    user_id, comp_a, comp_b, ws_1 = await _seed_user_and_computers(
        patched_get_db_connection
    )
    try:
        snapshot = await computer_db.get_live_workspaces_for_archive_snapshot(comp_a)
        results = await asyncio.gather(
            computer_db.claim_computer_for_archive(
                comp_a, expected_workspaces=snapshot, expected_workspace_id=ws_1
            ),
            move_db.claim_workspace_move(
                user_id=user_id, workspace_id=ws_1, target_computer_id=comp_b
            ),
            return_exceptions=True,
        )
        # Exactly one wins, the other gets a clean domain refusal (no deadlock).
        archive_res, move_res = results
        if isinstance(archive_res, Exception):
            assert isinstance(archive_res, ComputerBusyError)
            assert isinstance(move_res, dict)
        else:
            assert isinstance(move_res, MoveRefused)
            assert move_res.code == "busy"
    finally:
        await _cleanup_user(patched_get_db_connection, user_id)


async def test_dual_conn_staging_and_archive_interleave_without_deadlock(
    patched_get_db_connection,
):
    """Interleave ``stage_workspace_move`` and ``claim_computer_for_archive`` on two workspaces
    whose ``created_at`` order is opposite to their ``workspace_id`` order.

    With unified ``ORDER BY workspace_id FOR UPDATE`` locking, both transactions
    serialize cleanly on the first workspace row with zero PostgreSQL deadlock.
    """
    user_id, comp_a, comp_b, ws_1 = await _seed_user_and_computers(
        patched_get_db_connection
    )
    try:
        ws_2_row = await workspace_db.create_workspace_on_computer(
            user_id, f"AAPL-{uuid.uuid4().hex[:6]}", comp_a
        )
        assert ws_2_row is not None
        ws_2 = str(ws_2_row["workspace_id"])

        # Force created_at order to be the reverse of workspace_id order.
        low_id, high_id = sorted([ws_1, ws_2])
        async with patched_get_db_connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE workspaces SET created_at = NOW() - interval '2 hours' WHERE workspace_id = %s",
                (high_id,),
            )
            await conn.execute(
                "UPDATE workspaces SET created_at = NOW() - interval '1 hour' WHERE workspace_id = %s",
                (low_id,),
            )

        move_id = str(uuid.uuid4())
        runner_id = str(uuid.uuid4())
        async with patched_get_db_connection() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO workspace_moves (
                    move_id, user_id, workspace_id, source_computer_id,
                    target_computer_id, make_primary, from_backup, phase,
                    runner_id, source_folder, heartbeat_at
                ) VALUES (
                    %s, %s, %s, %s, %s, FALSE, FALSE, 'backing_up',
                    %s, 'NVDA', NOW()
                )
                """,
                (move_id, user_id, low_id, comp_a, comp_b, runner_id),
            )

        snapshot = await computer_db.get_live_workspaces_for_archive_snapshot(comp_a)
        assert [w["workspace_id"] for w in snapshot] == [low_id, high_id]

        # Hold a row lock on high_id on a separate connection so both
        # stage_workspace_move and claim_computer_for_archive enter lock wait
        # simultaneously while traversing workspaces in ORDER BY workspace_id order.
        gate_locked = asyncio.Event()
        release_gate = asyncio.Event()
        holder_pid_box: list[int] = []

        async def _hold_high_row():
            try:
                async with patched_get_db_connection() as conn, conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT pg_backend_pid() AS pid")
                        pid_row = await cur.fetchone()
                        if pid_row is not None:
                            holder_pid_box.append(int(pid_row["pid"]))
                        await cur.execute(
                            "SELECT 1 FROM workspaces WHERE workspace_id = %s FOR UPDATE",
                            (high_id,),
                        )
                    gate_locked.set()
                    await release_gate.wait()
            finally:
                gate_locked.set()

        holder = asyncio.create_task(_hold_high_row())
        stage_task: asyncio.Task | None = None
        archive_task: asyncio.Task | None = None
        try:
            await asyncio.wait_for(gate_locked.wait(), timeout=5.0)
            if holder.done():
                holder.result()
            assert holder_pid_box, "holder backend pid should be recorded"
            holder_pid = holder_pid_box[0]

            stage_task = asyncio.create_task(
                move_db.stage_workspace_move(
                    move_id, runner_id, source_folder="NVDA", leftovers_path=None
                )
            )
            archive_task = asyncio.create_task(
                computer_db.claim_computer_for_archive(
                    comp_a, expected_workspaces=snapshot, expected_workspace_id=low_id
                )
            )
            # Wait deterministically until BOTH business transactions are actually
            # blocked in PostgreSQL's lock wait chain rooted at holder_pid
            # (ignoring unrelated lock waits elsewhere in the database).
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            waiting_count = 0
            async with patched_get_db_connection() as obs_conn:
                while loop.time() < deadline:
                    async with obs_conn.cursor() as cur:
                        await cur.execute(
                            """
                            WITH RECURSIVE wait_chain(pid) AS (
                                SELECT a.pid
                                FROM pg_stat_activity a
                                WHERE a.datname = current_database()
                                  AND a.state = 'active'
                                  AND a.wait_event_type = 'Lock'
                                  AND %(holder_pid)s = ANY(pg_blocking_pids(a.pid))
                                UNION
                                SELECT a.pid
                                FROM pg_stat_activity a
                                JOIN wait_chain w ON w.pid = ANY(pg_blocking_pids(a.pid))
                                WHERE a.datname = current_database()
                                  AND a.state = 'active'
                                  AND a.wait_event_type = 'Lock'
                            )
                            SELECT COUNT(DISTINCT pid) AS waiting_count
                            FROM wait_chain
                            """,
                            {"holder_pid": holder_pid},
                        )
                        row = await cur.fetchone()
                        waiting_count = int(row["waiting_count"]) if row is not None else 0
                    if waiting_count >= 2:
                        break
                    await asyncio.sleep(0.02)
            assert waiting_count >= 2, (
                f"Expected both stage and archive transactions to enter PG lock wait "
                f"chain behind holder_pid={holder_pid}, observed waiting_count={waiting_count}"
            )

            release_gate.set()
            await asyncio.wait_for(holder, timeout=5.0)
            stage_res, archive_res = await asyncio.wait_for(
                asyncio.gather(stage_task, archive_task, return_exceptions=True),
                timeout=5.0,
            )
            assert not isinstance(stage_res, Exception), f"stage failed: {stage_res!r}"
            assert stage_res is not None
            assert stage_res["phase"] == "staged"
            assert isinstance(archive_res, ComputerBusyError)
        finally:
            release_gate.set()
            pending = [
                t for t in (holder, stage_task, archive_task) if t is not None and not t.done()
            ]
            if pending:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await _cleanup_user(patched_get_db_connection, user_id)
