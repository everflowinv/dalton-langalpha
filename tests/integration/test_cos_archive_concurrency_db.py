"""Real-PostgreSQL dual-connection concurrency tests for Batch 3 archive & move fences.

Marked ``integration`` so the lead can run them outside the sandbox against a
live PostgreSQL database:
    uv run pytest -m integration tests/integration/test_cos_archive_concurrency_db.py -v
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.server.database import computer as computer_db
from src.server.database import workspace as workspace_db
from src.server.database import workspace_move as move_db
from src.server.database.pool import get_db_connection
from src.server.database.workspace_move import MoveRefused
from src.server.services.computer_errors import (
    ArchiveScopeChangedError,
    ComputerBusyError,
)
from src.server.services.workspace_manager import WorkspaceManager


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _seed_user_and_computers():
    user_id = f"it-cos-b3-{uuid.uuid4().hex[:12]}"
    async with get_db_connection() as conn, conn.transaction():
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


async def _cleanup_user(user_id: str):
    async with get_db_connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM workspace_moves WHERE user_id = %s", (user_id,))
        await conn.execute("DELETE FROM workspaces WHERE user_id = %s", (user_id,))
        await conn.execute("DELETE FROM computers WHERE user_id = %s", (user_id,))
        await conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))


async def test_dual_conn_snapshot_drift_and_stale_staged_move_block_archive():
    user_id, comp_a, comp_b, ws_1 = await _seed_user_and_computers()
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
        async with get_db_connection() as conn, conn.transaction():
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
        async with get_db_connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE workspace_moves SET phase = 'committed' WHERE user_id = %s",
                (user_id,),
            )
        with pytest.raises(ComputerBusyError):
            await computer_db.claim_computer_for_archive(
                comp_a, expected_workspaces=fresh_snapshot, expected_workspace_id=ws_1
            )
    finally:
        await _cleanup_user(user_id)


async def test_dual_conn_archiving_fence_blocks_create_and_move_and_survives_startup_reset():
    user_id, comp_a, comp_b, ws_1 = await _seed_user_and_computers()
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

        # Startup recovery resets status='stopping' -> 'stopped' while preserving config.archiving=true.
        wm = object.__new__(WorkspaceManager)
        recovered = await wm.recover_archiving_computers_on_startup()
        assert recovered >= 1
        row_after_restart = await computer_db.get_computer(comp_a)
        assert row_after_restart is not None
        assert row_after_restart["status"] == "stopped"
        assert (row_after_restart.get("config") or {}).get("archiving") is True

        # Fence STILL blocks new workspace creation until provider terminal state clears it.
        created_after_restart = await workspace_db.create_workspace_on_computer(
            user_id, f"GOOG-{uuid.uuid4().hex[:6]}", comp_a
        )
        assert created_after_restart is None

        # Once terminal provider state clears fence, create succeeds again.
        assert await computer_db.clear_computer_archiving(comp_a) is True
        created_after_clear = await workspace_db.create_workspace_on_computer(
            user_id, f"GOOG-{uuid.uuid4().hex[:6]}", comp_a
        )
        assert created_after_clear is not None
    finally:
        await _cleanup_user(user_id)


async def test_dual_conn_move_and_archive_serialize_without_deadlock():
    user_id, comp_a, comp_b, ws_1 = await _seed_user_and_computers()
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
        await _cleanup_user(user_id)
