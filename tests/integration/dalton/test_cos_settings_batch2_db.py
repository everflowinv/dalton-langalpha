"""Real PostgreSQL integration tests for Batch 2 workspace creation snapshot
re-verification, two-connection concurrent target drift, and cloud-only primary
bootstrap.

Run against a temporary *_test PostgreSQL database:
  TEST_DB_NAME=langalpha_test uv run pytest -m integration \
      tests/integration/dalton/test_cos_settings_batch2_db.py -v
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import uuid
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import psycopg
import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool

from ptc_agent.config.core import SandboxConfig
import src.server.database as _database_pkg
from src.server.database import computer as computer_db
from src.server.database import computer_names as computer_names_db
from src.server.database import pool as pool_mod
from src.server.database import user as user_db
from src.server.database import workspace as ws_db
from src.server.database import workspace_folders as ws_folders_db
from src.server.database import workspace_move as ws_move_db
from src.server.database.computer import (
    create_computer,
    get_computer,
    get_computers_for_user,
    get_primary_computer,
    update_computer_status,
)
from src.server.database.session_lock import close_shared_lock_session
from src.server.database.user import create_user
from src.server.services.computer_errors import ApprovalScopeChangedError
from src.server.services.computer_manager import ComputerManager

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def b2_live_pool(
    dalton_schema,
    cleanup_dalton_tables,
    test_db_uri: str,
):
    """Open an AsyncConnectionPool on the test's live event loop, matching
    ``tests/integration/dalton/test_au1_company_foundation_db.py`` so session
    locks and multi-connection queries never time out across event loops."""
    for _info in pkgutil.walk_packages(
        _database_pkg.__path__, prefix="src.server.database."
    ):
        importlib.import_module(_info.name)

    local_pool = AsyncConnectionPool(
        conninfo=test_db_uri,
        min_size=1,
        max_size=5,
        configure=pool_mod._configure_postgres_connection,
        check=AsyncConnectionPool.check_connection,
        kwargs=dict(pool_mod._CONNECTION_KWARGS),
        open=False,
    )
    await local_pool.open()
    await local_pool.wait()

    @asynccontextmanager
    async def _live_get_db_connection(conn=None):
        if conn is not None:
            yield conn
            return
        async with local_pool.connection(timeout=10) as owned:
            yield owned

    async def _live_session_connection():
        conn = await psycopg.AsyncConnection.connect(
            test_db_uri, **pool_mod._CONNECTION_KWARGS
        )
        await pool_mod._configure_postgres_connection(conn)
        return conn

    orig_gdc = pool_mod.get_db_connection
    holders = {
        pool_mod,
        computer_db,
        computer_names_db,
        user_db,
        ws_db,
        ws_folders_db,
        ws_move_db,
        *(
            module
            for name, module in list(sys.modules.items())
            if name.startswith(("src.", "ptc_agent."))
            and getattr(module, "get_db_connection", None) is orig_gdc
        ),
    }

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(pool_mod, "get_db_connection_string", lambda: test_db_uri)
        )
        stack.enter_context(
            patch.object(pool_mod, "get_or_create_pool", lambda: local_pool)
        )
        stack.enter_context(
            patch.object(pool_mod, "open_session_connection", _live_session_connection)
        )
        for module in holders:
            stack.enter_context(
                patch.object(module, "get_db_connection", _live_get_db_connection)
            )
        await close_shared_lock_session()
        try:
            yield local_pool
        finally:
            await close_shared_lock_session()
            async with local_pool.connection() as cleanup_conn:
                async with cleanup_conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM workspaces WHERE user_id LIKE 'b2-%'"
                    )
                    await cur.execute(
                        "DELETE FROM computers WHERE user_id LIKE 'b2-%'"
                    )
                    await cur.execute(
                        "DELETE FROM users WHERE user_id LIKE 'b2-%'"
                    )
            await local_pool.close()


async def test_create_workspace_on_computer_reject_unavailable_enforces_fences_under_for_share(
    b2_live_pool,
) -> None:
    """Snapshot-verified create (reject_unavailable=True) refuses error, archiving,
    retiring, and preparing-without-sandbox computers atomically inside PostgreSQL."""
    user_id = f"b2-user-{uuid.uuid4().hex[:8]}"
    await create_user(user_id=user_id, email=f"{user_id}@test.local", name="Tester")

    healthy = await create_computer(
        user_id,
        kind="docker",
        name="Healthy",
        is_primary=True,
        status="stopped",
        provider_ref=f"b2-h-{uuid.uuid4()}",
    )
    errored = await create_computer(
        user_id,
        kind="docker",
        name="Errored",
        is_primary=False,
        status="error",
        provider_ref=f"b2-e-{uuid.uuid4()}",
    )
    archiving = await create_computer(
        user_id,
        kind="docker",
        name="Archiving",
        is_primary=False,
        status="stopped",
        provider_ref=f"b2-a-{uuid.uuid4()}",
        config={"archiving": True},
    )
    preparing = await create_computer(
        user_id,
        kind="docker",
        name="Preparing",
        is_primary=False,
        status="creating",
        provider_ref=None,
    )

    ws_ok = await ws_db.create_workspace_on_computer(
        user_id,
        "Alpha",
        str(healthy["computer_id"]),
        reject_unavailable=True,
        expected_computer_name="Healthy",
        expected_computer_kind="docker",
    )
    assert ws_ok is not None
    assert str(ws_ok["computer_id"]) == str(healthy["computer_id"])

    for idx, bad in enumerate((errored, archiving, preparing)):
        ws_bad = await ws_db.create_workspace_on_computer(
            user_id, f"Bad{idx}", str(bad["computer_id"]), reject_unavailable=True
        )
        assert ws_bad is None


async def test_two_connection_concurrent_rename_or_unavailable_returns_scope_changed(
    b2_live_pool,
    test_db_uri: str,
) -> None:
    """Codex Low 9: after snapshot capture on connection A, an independent
    connection B renames the target computer or marks it unavailable; locked
    FOR SHARE verification refuses the insert (returning None -> scope_changed)."""
    user_id = f"b2-race-{uuid.uuid4().hex[:8]}"
    await create_user(user_id=user_id, email=f"{user_id}@test.local", name="Racer")

    c_rename = await create_computer(
        user_id,
        kind="docker",
        name="Alpha Box",
        is_primary=True,
        status="stopped",
        provider_ref=f"b2-r1-{uuid.uuid4()}",
    )
    c_err = await create_computer(
        user_id,
        kind="docker",
        name="Beta Box",
        is_primary=False,
        status="stopped",
        provider_ref=f"b2-r2-{uuid.uuid4()}",
    )

    # Independent second PostgreSQL connection mutates both rows after snapshot capture.
    async with await psycopg.AsyncConnection.connect(
        test_db_uri, autocommit=True
    ) as conn_b:
        async with conn_b.cursor() as cur:
            await cur.execute(
                "UPDATE computers SET name = %s WHERE computer_id = %s",
                ("Renamed Box", str(c_rename["computer_id"])),
            )
            await cur.execute(
                "UPDATE computers SET status = 'error' WHERE computer_id = %s",
                (str(c_err["computer_id"]),),
            )

    # Connection A attempts locked creation with the original snapshot fields -> refused.
    ws_renamed = await ws_db.create_workspace_on_computer(
        user_id,
        "NVDA",
        str(c_rename["computer_id"]),
        reject_unavailable=True,
        expected_computer_name="Alpha Box",
        expected_computer_kind="docker",
    )
    assert ws_renamed is None

    ws_errored = await ws_db.create_workspace_on_computer(
        user_id,
        "AMD",
        str(c_err["computer_id"]),
        reject_unavailable=True,
        expected_computer_name="Beta Box",
        expected_computer_kind="docker",
    )
    assert ws_errored is None


async def test_bootstrap_cloud_primary_creates_cloud_without_promoting_own_machine_and_aborts_if_primary_appears(
    b2_live_pool,
) -> None:
    """When a user has an unpromoted SSH own machine and no primary computer:
    1) bootstrap_cloud_primary_computer creates a new hosted docker primary and
       leaves the SSH machine non-primary.
    2) If a primary computer already exists when execution starts, it aborts
       with ApprovalScopeChangedError and creates no extra computer row.
    """
    user_id = f"b2-boot-{uuid.uuid4().hex[:8]}"
    await create_user(user_id=user_id, email=f"{user_id}@test.local", name="Founder")

    own_ssh = await create_computer(
        user_id,
        kind="ssh",
        name="MacBook",
        is_primary=False,
        status="stopped",
        provider_config={"host": "mac.local", "user": "alice"},
    )
    assert await get_primary_computer(user_id) is None

    sandbox_cfg = create_autospec(SandboxConfig, instance=True)
    sandbox_cfg.provider = "docker"
    sandbox_cfg.creatable_providers.return_value = ("docker",)

    mgr = object.__new__(ComputerManager)
    mgr.config = SimpleNamespace(
        sandbox=sandbox_cfg,
        filesystem=SimpleNamespace(working_directory="/home/workspace"),
    )

    created = await mgr.bootstrap_cloud_primary_computer(
        user_id, default_name="NVDA", expected_kind="docker"
    )
    assert created["kind"] == "docker"
    assert created["is_primary"] is True
    assert str(created["computer_id"]) != str(own_ssh["computer_id"])

    refreshed_ssh = await get_computer(str(own_ssh["computer_id"]))
    assert refreshed_ssh is not None
    assert refreshed_ssh["is_primary"] is False

    # Now a primary exists: a second bootstrap attempt must abort with scope_changed.
    before_count = len(await get_computers_for_user(user_id))
    with pytest.raises(ApprovalScopeChangedError):
        await mgr.bootstrap_cloud_primary_computer(
            user_id, default_name="AAPL", expected_kind="docker"
        )
    assert len(await get_computers_for_user(user_id)) == before_count

    await update_computer_status(str(created["computer_id"]), "stopped")
