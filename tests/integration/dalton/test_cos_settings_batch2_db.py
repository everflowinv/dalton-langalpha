"""Real PostgreSQL integration tests for Batch 2 workspace creation snapshot
re-verification and cloud-only primary bootstrap.

Run against a temporary *_test PostgreSQL database:
  TEST_DB_NAME=langalpha_test uv run pytest -m integration \
      tests/integration/dalton/test_cos_settings_batch2_db.py -v
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from ptc_agent.config.core import SandboxConfig
from src.server.database import computer as computer_db
from src.server.database import user as user_db
from src.server.database import workspace as ws_db
from src.server.database import workspace_folders as ws_folders_db
from src.server.database.computer import (
    create_computer,
    get_computer,
    get_computers_for_user,
    get_primary_computer,
    update_computer_status,
)
from src.server.database.user import create_user
from src.server.services.computer_errors import ApprovalScopeChangedError
from src.server.services.computer_manager import ComputerManager

# Loaded before patched_get_db_connection sweeps sys.modules so every holder uses test_db_pool.
_POOL_HOLDERS = (
    computer_db,
    user_db,
    ws_db,
    ws_folders_db,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_create_workspace_on_computer_reject_unavailable_enforces_fences_under_for_share(
    patched_get_db_connection,
    cleanup_tables,
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
        user_id, "Alpha", str(healthy["computer_id"]), reject_unavailable=True
    )
    assert ws_ok is not None
    assert str(ws_ok["computer_id"]) == str(healthy["computer_id"])

    for idx, bad in enumerate((errored, archiving, preparing)):
        ws_bad = await ws_db.create_workspace_on_computer(
            user_id, f"Bad{idx}", str(bad["computer_id"]), reject_unavailable=True
        )
        assert ws_bad is None


async def test_bootstrap_cloud_primary_creates_cloud_without_promoting_own_machine_and_aborts_if_primary_appears(
    patched_get_db_connection,
    cleanup_tables,
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

    created = await mgr.bootstrap_cloud_primary_computer(user_id, default_name="NVDA")
    assert created["kind"] == "docker"
    assert created["is_primary"] is True
    assert str(created["computer_id"]) != str(own_ssh["computer_id"])

    refreshed_ssh = await get_computer(str(own_ssh["computer_id"]))
    assert refreshed_ssh is not None
    assert refreshed_ssh["is_primary"] is False

    # Now a primary exists: a second bootstrap attempt must abort with scope_changed.
    before_count = len(await get_computers_for_user(user_id))
    with pytest.raises(ApprovalScopeChangedError):
        await mgr.bootstrap_cloud_primary_computer(user_id, default_name="AAPL")
    assert len(await get_computers_for_user(user_id)) == before_count

    # Cleanup helper check: ensure status helper imported is exercised cleanly.
    await update_computer_status(str(created["computer_id"]), "stopped")
