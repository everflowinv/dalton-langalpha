"""Real PostgreSQL concurrency tests for Batch 1 managed automation row locking.

Run outside the sandbox against a temporary *_test PostgreSQL database:
  TEST_DB_NAME=langalpha_test uv run pytest -m integration \
      tests/integration/dalton/test_cos_settings_batch1_db.py -v
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import create_autospec

import psycopg
import pytest
from psycopg.rows import dict_row

from src.dalton.autonomy import schedule as schedule_mod
from src.server.database import automation as auto_db
from src.server.database import computer as computer_db
from src.server.database import user as user_db
from src.server.database import workspace as ws_db
from src.server.database import workspace_folders as ws_folders_db
from src.server.database import workspace_move as ws_move_db
from src.server.database.computer import create_computer
from src.server.database.user import create_user
from src.server.database.workspace_move import (
    advance_workspace_move,
    commit_workspace_move,
    stage_workspace_move,
)
from src.server.database.workspace_names import HOME_FOLDER
from src.server.services.automations import lifecycle as lifecycle_mod
from src.server.services.llm import user_models
from src.server.services.workspace_move import claim_workspace_move

# Loaded before patched_get_db_connection sweeps sys.modules so every holder uses test_db_pool.
_POOL_HOLDERS = (
    schedule_mod,
    auto_db,
    computer_db,
    user_db,
    ws_db,
    ws_folders_db,
    ws_move_db,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _setup_user_and_two_computers(user_id: str) -> tuple[str, str, str]:
    await create_user(user_id=user_id, email=f"{user_id}@test.local", name="Analyst")
    c1 = await create_computer(
        user_id,
        kind="docker",
        name="Primary",
        is_primary=True,
        status="running",
        provider_ref=f"b1-c1-{uuid.uuid4()}",
    )
    c2 = await create_computer(
        user_id,
        kind="docker",
        name="Second",
        is_primary=False,
        status="running",
        provider_ref=f"b1-c2-{uuid.uuid4()}",
    )
    ws = await ws_db.create_workspace_on_computer(user_id, "NVDA", str(c1["computer_id"]))
    assert ws is not None
    return str(c1["computer_id"]), str(c2["computer_id"]), str(ws["workspace_id"])


def _stub_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lifecycle_mod,
        "get_configured_llm_models",
        create_autospec(
            lifecycle_mod.get_configured_llm_models,
            return_value={"anthropic": ["claude-sonnet-4-6"]},
        ),
    )

    async def _classify(
        uid: str, model_name: str, _pref_cache=None, ignore_custom_models: bool = False
    ):
        if model_name == "claude-sonnet-4-6":
            return user_models.ModelSource.SYSTEM, {
                "provider": "anthropic",
                "reasoning": {"efforts": ["low", "medium", "high"], "default": "medium"},
            }
        return user_models.ModelSource.UNKNOWN, {}

    monkeypatch.setattr(
        user_models,
        "classify_model",
        create_autospec(user_models.classify_model, side_effect=_classify),
    )
    monkeypatch.setattr(
        user_models,
        "get_model_preference",
        create_autospec(user_models.get_model_preference, return_value={}),
    )
    monkeypatch.setattr(
        lifecycle_mod,
        "check_claude_code_hosted",
        create_autospec(lifecycle_mod.check_claude_code_hosted, return_value=None),
    )
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.is_dalton_auth_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)


async def test_automation_tx_for_share_blocks_concurrent_commit_workspace_move_until_commit(
    patched_get_db_connection,
    cleanup_tables,
    test_db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While schedule_dalton_automation_helper holds FOR SHARE on computers + workspaces,
    a concurrent commit_workspace_move on another connection blocks until the automation tx commits."""
    _stub_catalog(monkeypatch)
    user_id = f"b1-lock-{uuid.uuid4().hex[:10]}"
    c1_id, c2_id, ws_id = await _setup_user_and_two_computers(user_id)

    locks_held = asyncio.Event()
    allow_commit = asyncio.Event()
    orig_create = auto_db.create_automation

    async def _paused_create(*args: Any, **kwargs: Any) -> Any:
        # Called inside the open transaction AFTER _lock_and_verify_workspace_placement took FOR SHARE
        locks_held.set()
        await asyncio.wait_for(allow_commit.wait(), timeout=5.0)
        return await orig_create(*args, **kwargs)

    monkeypatch.setattr(auto_db, "create_automation", _paused_create)

    auto_task = asyncio.create_task(
        schedule_mod.schedule_dalton_automation_helper(
            "dalton-nvda-watch",
            {
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "instruction": "Monitor NVDA.",
                "llm_model": "claude-sonnet-4-6",
                "reasoning_effort": "high",
            },
            user_id=user_id,
            workspace_id=ws_id,
        )
    )
    await asyncio.wait_for(locks_held.wait(), timeout=5.0)

    # On a separate connection, insert a staged move row with valid runner_id and source_folder
    # so commit_workspace_move attempts FOR UPDATE OF w while auto_task holds FOR SHARE.
    move_id = str(uuid.uuid4())
    runner_id = str(uuid.uuid4())
    async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as aux_conn:
        async with aux_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO workspace_moves (
                    move_id, workspace_id, user_id, source_computer_id, target_computer_id,
                    phase, make_primary, runner_id, source_folder, leftovers_path,
                    staged_at, heartbeat_at
                )
                VALUES (%s, %s, %s, %s, %s, 'staged', false, %s, %s, %s, NOW(), NOW())
                """,
                (move_id, ws_id, user_id, c1_id, c2_id, runner_id, "NVDA", None),
            )

    commit_task = asyncio.create_task(commit_workspace_move(move_id, runner_id))
    # commit_workspace_move must stay blocked waiting on the FOR SHARE lock held by auto_task
    done, _pending = await asyncio.wait({commit_task}, timeout=0.3)
    assert not done, "commit_workspace_move bypassed FOR SHARE lock held by automation transaction"

    allow_commit.set()
    auto_res = await asyncio.wait_for(auto_task, timeout=5.0)
    assert auto_res["ok"] is True

    committed = await asyncio.wait_for(commit_task, timeout=5.0)
    assert committed is not None
    assert str(committed["phase"]) == "committed"


async def test_concurrent_workspace_move_before_lock_refuses_automation_save_with_workspace_changed(
    patched_get_db_connection,
    cleanup_tables,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When another connection moves the workspace between pre-lock snapshot and lock acquisition,
    schedule_dalton_automation_helper aborts with workspace_changed and commits no row."""
    _stub_catalog(monkeypatch)
    user_id = f"b1-drift-{uuid.uuid4().hex[:10]}"
    _c1_id, c2_id, ws_id = await _setup_user_and_two_computers(user_id)

    snapshot_taken = asyncio.Event()
    move_finished = asyncio.Event()
    orig_snapshot = schedule_mod._read_workspace_computer_snapshot

    async def _paused_snapshot(*args: Any, **kwargs: Any):
        snap = await orig_snapshot(*args, **kwargs)
        snapshot_taken.set()
        await asyncio.wait_for(move_finished.wait(), timeout=5.0)
        return snap

    monkeypatch.setattr(schedule_mod, "_read_workspace_computer_snapshot", _paused_snapshot)

    auto_task = asyncio.create_task(
        schedule_mod.schedule_dalton_automation_helper(
            "dalton-nvda-watch",
            {
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "instruction": "Monitor NVDA.",
                "llm_model": "claude-sonnet-4-6",
            },
            user_id=user_id,
            workspace_id=ws_id,
        )
    )
    await asyncio.wait_for(snapshot_taken.wait(), timeout=5.0)

    # Complete a real workspace move c1 -> c2 before auto_task enters its write transaction
    move = await claim_workspace_move(
        workspace_id=ws_id,
        user_id=user_id,
        target_computer_id=c2_id,
    )
    mid = str(move["move_id"])
    runner_id = str(move["runner_id"])
    source_folder = str(move.get("source_folder") or "NVDA")
    advanced = await advance_workspace_move(mid, runner_id, "backing_up")
    assert advanced is not None
    staged = await stage_workspace_move(
        mid, runner_id, source_folder=source_folder, leftovers_path=None
    )
    assert staged is not None
    committed = await commit_workspace_move(mid, runner_id)
    assert committed is not None
    move_finished.set()

    auto_res = await asyncio.wait_for(auto_task, timeout=5.0)
    assert auto_res["ok"] is False
    assert auto_res["error"] == "workspace_changed"
    assert auto_res["message"] == "情况已变化，请重新发起"
    rows = await auto_db.list_all_automations(user_id)
    assert rows == []


async def test_waiting_automation_tx_observes_just_committed_workspace_move_after_for_share(
    patched_get_db_connection,
    cleanup_tables,
    test_db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a concurrent transaction holds FOR UPDATE on workspaces and inserts a preparing move
    before committing, a waiting automation tx sees the move in its post-lock query and aborts."""
    _stub_catalog(monkeypatch)
    user_id = f"b1-postlock-{uuid.uuid4().hex[:10]}"
    c1_id, c2_id, ws_id = await _setup_user_and_two_computers(user_id)

    ws_locked_by_move = asyncio.Event()
    release_move_tx = asyncio.Event()

    async def _holding_move_claim() -> None:
        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=False) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT workspace_id FROM workspaces WHERE workspace_id = %s FOR UPDATE",
                    (ws_id,),
                )
                ws_locked_by_move.set()
                await asyncio.wait_for(release_move_tx.wait(), timeout=5.0)
                await cur.execute(
                    """
                    INSERT INTO workspace_moves (
                        move_id, workspace_id, user_id, source_computer_id, target_computer_id,
                        phase, make_primary, runner_id, source_folder, heartbeat_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 'preparing', false, %s, %s, NOW())
                    """,
                    (str(uuid.uuid4()), ws_id, user_id, c1_id, c2_id, str(uuid.uuid4()), "NVDA"),
                )
            await conn.commit()

    move_holder = asyncio.create_task(_holding_move_claim())
    await asyncio.wait_for(ws_locked_by_move.wait(), timeout=5.0)

    auto_task = asyncio.create_task(
        schedule_mod.schedule_dalton_automation_helper(
            "dalton-nvda-watch",
            {
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "instruction": "Monitor NVDA.",
                "llm_model": "claude-sonnet-4-6",
            },
            user_id=user_id,
            workspace_id=ws_id,
        )
    )
    # Give auto_task time to reach FOR SHARE on workspaces and block behind move_holder
    done, _pending = await asyncio.wait({auto_task}, timeout=0.3)
    assert not done, "automation tx should wait behind FOR UPDATE on workspaces"

    release_move_tx.set()
    await asyncio.wait_for(move_holder, timeout=5.0)

    auto_res = await asyncio.wait_for(auto_task, timeout=5.0)
    assert auto_res["ok"] is False
    assert auto_res["error"] == "workspace_changed"
    assert auto_res["message"] == "情况已变化，请重新发起"
    rows = await auto_db.list_all_automations(user_id)
    assert rows == []


async def test_home_earnings_calendar_owner_vs_other_home_job_on_real_db(
    patched_get_db_connection,
    cleanup_tables,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real DB verification of Home FOR SHARE locking + dalton-earnings-calendar owner exception."""
    _stub_catalog(monkeypatch)
    monkeypatch.setattr("src.dalton.settings.is_dalton_auth_enabled", lambda: True)
    monkeypatch.setattr("src.dalton.autonomy.system_jobs.is_pm_user", lambda _uid: False)

    user_id = f"b1-home-{uuid.uuid4().hex[:10]}"
    c1_id, _c2_id, _ws_id = await _setup_user_and_two_computers(user_id)
    home = await ws_db.get_or_create_home_workspace(user_id)
    assert home is not None
    await ws_db.bind_workspace_to_computer(
        str(home["workspace_id"]), c1_id, expected_computer_id=None, dir_name=HOME_FOLDER
    )

    ok_res = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-earnings-calendar",
        {
            "trigger_type": "cron",
            "cron_expression": "0 9 * * 6",
            "instruction": "Refresh earnings calendar.",
            "llm_model": "claude-sonnet-4-6",
        },
        user_id=user_id,
        workspace_id="home",
        role="chief_of_staff",
    )
    assert ok_res["ok"] is True

    refused_res = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-director-daily",
        {
            "trigger_type": "cron",
            "cron_expression": "30 7 * * 1-5",
            "instruction": "Daily brief.",
            "llm_model": "claude-sonnet-4-6",
        },
        user_id=user_id,
        workspace_id="home",
        role="chief_of_staff",
    )
    assert refused_res["ok"] is False
    assert refused_res["error"] == "forbidden_pm"
